"""eCourtsIndia partner API adapter (https://webapi.ecourtsindia.com, Bearer auth).

Documented endpoints used:
  GET  /api/partner/case/{cnr}          full case record (parties, advocates, judges, hearings, IAs, orders)
  POST /api/partner/case/{cnr}/refresh  async re-scrape; poll detail until dateModified advances
  GET  /api/partner/search              CourtCodes, CaseTypes, FilingYears, Query, Litigants, Page, PageSize

The public docs do not publish the full response schema, so `_parse_case` reads fields
defensively with several candidate keys. VERIFY the mapping against a live response
during the PRD proof-of-concept and tighten it (see tests/test_ecourtsindia_adapter.py).
"""

from datetime import date, datetime
from typing import Any

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.providers.court.base import (
    CaseNotFound,
    CaseRecord,
    CourtDataProvider,
    HearingRecord,
    OrderRecord,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimited,
)


def _pick(d: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        cur: Any = d
        for part in k.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                cur = None
                break
        if cur not in (None, "", []):
            return cur
    return default


def _date(v: Any) -> date | None:
    if not v:
        return None
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(s[:10] if fmt.startswith("%Y-") else s, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _names(v: Any) -> list[str]:
    if not v:
        return []
    if isinstance(v, str):
        return [v]
    out = []
    for item in v:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            n = _pick(item, "name", "partyName", "advocateName", "judgeName")
            if n:
                out.append(str(n))
    return out


_WRAPPERS = ("data", "result", "case", "courtCaseData", "caseDetails", "case_detail", "record")


def _unwrap(payload: Any) -> dict:
    """Merge nested wrapper objects ({"data": {"courtCaseData": {...}}}) into one flat-ish dict."""
    if not isinstance(payload, dict):
        return {}
    data = dict(payload)
    for _ in range(3):
        merged = False
        for key in _WRAPPERS:
            inner = data.get(key)
            if isinstance(inner, dict):
                data = {**{k: v for k, v in data.items() if k != key}, **inner}
                merged = True
        if not merged:
            break
    return data


def _side(v: Any, side: str) -> Any:
    """advocates may be a list or {"petitioner": [...], "respondent": [...]}."""
    if isinstance(v, dict):
        return _pick(v, side, f"{side}s", f"{side}Advocates", f"{side}_advocates")
    return None


def _humanize(v: Any) -> Any:
    """SUMMONS_ISSUED -> Summons issued (leave mixed-case text alone)."""
    if isinstance(v, str) and v.isupper() and len(v) > 3:
        return v.replace("_", " ").capitalize()
    return v


def _court_level(cnr: str, data: dict) -> str | None:
    given = _pick(data, "courtLevel", "court_level", "courtType", "court_type", "court.type")
    if given:
        return given
    if cnr[2:4] == "HC":
        return "high_court"
    if cnr.startswith("SC"):
        return "supreme_court"
    return "district" if _pick(data, "district", "districtName") else None


def _parse_case(cnr: str, payload: dict) -> CaseRecord:
    """Mapping verified against a live eCourtsIndia reply (data.courtCaseData + data.entityInfo);
    alternative keys are kept so older/other shapes still parse."""
    data = _unwrap(payload)
    entity = _pick(data, "entityInfo", default={}) or {}
    lookup = _pick(data, "descriptions.enumLookup", "enumLookup", default={}) or {}

    parties = _pick(data, "parties", default={})
    parties = parties if isinstance(parties, dict) else {}
    petitioners = _names(
        _pick(data, "petitioners", "petitioner", "petitionerName", "petitioner_name") or _pick(parties, "petitioners", "petitioner")
    )
    respondents = _names(
        _pick(data, "respondents", "respondent", "respondentName", "respondent_name") or _pick(parties, "respondents", "respondent")
    )

    hearings = []
    history = _pick(data, "historyOfCaseHearings", "hearings", "hearingHistory", "hearing_history", "history", "caseHistory", default=[])
    for h in history or []:
        if not isinstance(h, dict):
            continue
        # eCourts history row: business happened on businessOnDate; hearingDate is the next date that was set
        hd = _date(_pick(h, "businessOnDate", "business_on_date", "hearingDate", "hearing_date", "date", "businessDate"))
        if hd:
            hearings.append(
                HearingRecord(
                    hd,
                    _humanize(_pick(h, "purposeOfListing", "purpose", "purposeOfHearing", "purpose_of_hearing", "stage")),
                    _pick(h, "judge", "judgeName", "judge_name"),
                    _pick(h, "business"),
                )
            )

    orders = []
    order_items = (
        (_pick(data, "interimOrders", default=[]) or []) + (_pick(data, "judgmentOrders", default=[]) or [])
        or _pick(data, "orders", "finalOrders", default=None)
        or _pick(data, "enrichment.orders", default=None)
        or _pick(data, "files.files", default=[])
        or []
    )
    for o in order_items:
        if isinstance(o, dict):
            orders.append(
                OrderRecord(
                    _date(_pick(o, "orderDate", "order_date", "date")),
                    _pick(o, "title", "orderType", "kind", "fileName", "order_type", "description"),
                    _pick(o, "url", "pdfUrl", "true_copy_pdf", "pdf_url", "fileUrl"),
                )
            )

    status_code = _pick(data, "caseStatus", "case_status", "status_normalized", "status")
    if isinstance(status_code, dict):
        status_code = _pick(status_code, "name", "label", "value")
    status = _pick(lookup, f"caseStatus.{status_code}", default=status_code) if isinstance(status_code, str) else status_code
    disposed = bool(_pick(data, "isDisposed", "is_disposed", default=False)) or str(status or "").lower().startswith("dispos")

    pet, res = (petitioners[0] if petitioners else None), (respondents[0] if respondents else None)
    title = _pick(data, "title", "caseTitle", "case_title") or (f"{pet} vs {res}" if pet and res else cnr)

    advocates_raw = _pick(data, "advocates", default=[])
    if isinstance(advocates_raw, dict):
        advocates = _names(_side(advocates_raw, "petitioner")) + _names(_side(advocates_raw, "respondent"))
    else:
        advocates = _names(advocates_raw)
    if not advocates:
        advocates = _names(_pick(data, "petitionerAdvocates", "petitionerAdvocate", "petitioner_advocate")) + _names(
            _pick(data, "respondentAdvocates", "respondentAdvocate", "respondent_advocate")
        )

    court = _pick(data, "court", default=None)
    court_name = _pick(data, "courtName", "court_name", "court.name", "establishmentName", "establishment_name", "courtComplex")
    if not court_name and isinstance(court, str):
        court_name = court

    # Human case number: "MC 4/2026" (type + registration no.) rather than the internal 15-digit id
    reg_no = _pick(data, "registrationNumber", "registration_number")
    type_raw = _pick(data, "caseTypeRaw", "case_type_raw")
    case_number = (f"{type_raw} {reg_no}" if type_raw and reg_no else reg_no) or _pick(
        data, "caseNumber", "case_number", "filingNumber", "filing_number"
    )
    type_code = _pick(data, "caseType", "case_type", "caseTypeName", "case_type_name")
    case_type = _pick(lookup, f"caseType.{type_code}", default=type_code) if isinstance(type_code, str) else type_code
    year = _pick(data, "caseYear", "case_year", "cnrYear", "filingYear", "filing_year", "registrationYear", "registration_year")
    judges = _names(_pick(data, "judges", default=[]))

    return CaseRecord(
        cnr=cnr,
        title=str(title),
        case_number=case_number,
        case_type=case_type,
        case_year=int(year) if str(year or "").isdigit() else (int(cnr[-4:]) if cnr[-4:].isdigit() else None),
        court_name=court_name,
        court_level=_court_level(cnr, data),
        state=_pick(data, "state", "stateName", "state_name", "court.state"),
        district=_pick(data, "district", "districtName", "district_name", "court.district"),
        judge=(judges or [None])[0] or _pick(data, "judge", "judgeName", "courtNumberAndJudge", "court_number_and_judge"),
        petitioners=petitioners,
        respondents=respondents,
        advocates=advocates,
        filing_date=_date(_pick(data, "filingDate", "filing_date", "dateOfFiling", "date_of_filing")),
        registration_date=_date(_pick(data, "registrationDate", "registration_date", "dateOfRegistration")),
        status=str(status) if status is not None else None,
        stage=_humanize(_pick(data, "stageOfCase", "stage", "caseStage", "case_stage", "stage_of_case")),
        next_hearing_date=_date(
            _pick(data, "nextHearingDate", "next_hearing_date", "nextDate", "next_date") or _pick(entity, "nextDateOfHearing")
        ),
        is_disposed=disposed,
        hearings=hearings,
        orders=orders,
        provider_modified_at=_pick(entity, "dateModified")
        or _pick(data, "dateModified", "date_modified", "lastModified", "updatedAt", "updated_at"),
        raw=payload if isinstance(payload, dict) else {},
    )


def _dump_raw(cnr: str, payload: Any) -> None:
    """Development aid: keep the provider's raw reply so field mapping can be checked."""
    if settings.ENVIRONMENT == "production":
        return
    import json
    from pathlib import Path

    try:
        folder = Path(settings.LOCAL_STORAGE_DIR) / "provider_debug"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{cnr}.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    except OSError:
        pass


class ECourtsIndiaProvider(CourtDataProvider):
    name = "ecourtsindia"

    def __init__(self, client: httpx.AsyncClient | None = None):
        if not settings.ECOURTSINDIA_API_KEY and client is None:
            raise ProviderAuthError("ECOURTSINDIA_API_KEY is not configured")
        self._client = client or httpx.AsyncClient(
            base_url=settings.ECOURTSINDIA_BASE_URL,
            headers={"Authorization": f"Bearer {settings.ECOURTSINDIA_API_KEY}", "Accept": "application/json"},
            timeout=settings.COURT_PROVIDER_TIMEOUT_SECONDS,
        )

    async def _request(self, method: str, url: str, **kw) -> httpx.Response:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.5, max=8),
            retry=retry_if_exception_type((httpx.TransportError, ProviderRateLimited)),
            reraise=True,
        ):
            with attempt:
                resp = await self._client.request(method, url, **kw)
                if resp.status_code == 429:
                    raise ProviderRateLimited("rate limited")
        if resp.status_code in (401, 403):
            raise ProviderAuthError(f"auth failed ({resp.status_code})")
        if resp.status_code == 404:
            raise CaseNotFound(url)
        if resp.status_code >= 400:
            raise ProviderError(f"{resp.status_code}: {resp.text[:300]}")
        return resp

    async def get_case(self, cnr: str) -> CaseRecord:
        resp = await self._request("GET", f"/api/partner/case/{cnr}")
        payload = resp.json()
        _dump_raw(cnr, payload)
        return _parse_case(cnr, payload)

    async def request_refresh(self, cnr: str) -> None:
        await self._request("POST", f"/api/partner/case/{cnr}/refresh")

    async def search(self, **filters) -> list[dict]:
        params = {
            "Query": filters.get("query"),
            "CourtCodes": filters.get("court_code"),
            "CaseTypes": filters.get("case_type"),
            "FilingYears": filters.get("year"),
            "Litigants": filters.get("litigant"),
            "Page": filters.get("page", 1),
            "PageSize": min(int(filters.get("page_size", 20)), 100),
        }
        resp = await self._request("GET", "/api/partner/search", params={k: v for k, v in params.items() if v})
        body = resp.json()
        items = _pick(body, "data.results", "data.items", "results", "items", "data", default=[]) or []
        return [
            {
                "cnr": _pick(i, "cnr", "cnrNumber"),
                "title": _pick(i, "title", "caseTitle"),
                "court_name": _pick(i, "courtName"),
                "case_number": _pick(i, "caseNumber"),
            }
            for i in items
            if isinstance(i, dict)
        ]
