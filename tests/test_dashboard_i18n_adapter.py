import os
import subprocess
import sys

import httpx
import pytest

from app.i18n import LANGUAGES, t
from app.providers.court.base import CaseNotFound, ProviderAuthError
from app.providers.court.ecourtsindia import ECourtsIndiaProvider, _parse_case
from tests.conftest import API, add_case


# ------------------------------------------------------------------ dashboard / reports / notifications
async def test_dashboard_per_role(client, firm):
    await add_case(client, firm, clients=[firm.client["id"]])
    await add_case(client, firm, cnr="MHMU030007892022")
    await client.post(
        f"{API}/tasks",
        headers=firm.h("lawyer"),
        json={"title": "Mine task", "assigned_to_id": firm.lawyer["id"], "due_at": "2020-01-01T00:00:00Z"},
    )
    d = (await client.get(f"{API}/dashboard", headers=firm.h("lawyer"))).json()
    assert d["active_cases"] == 1 and d["disposed_cases"] == 1
    assert d["case_status_distribution"] == {"Pending": 1, "Disposed": 1}
    assert len(d["upcoming_hearings_7d"]) == 1 and d["my_open_tasks"] == 1 and d["overdue_tasks"] == 1
    c = (await client.get(f"{API}/dashboard", headers=firm.h("client"))).json()
    assert c["active_cases"] == 1 and "my_open_tasks" not in c


async def test_firm_report_allowances_and_overage(client, firm, super_token):
    from tests.conftest import auth

    await client.patch(f"{API}/admin/tenants/{firm.tenant_id}", headers=auth(super_token), json={"monthly_api_allowance": 1})
    await add_case(client, firm)
    await client.post(f"{API}/cases/lookup", headers=firm.h("lawyer"), json={"cnr": "TSHY020004562023"})
    rep = (await client.get(f"{API}/reports/firm", headers=firm.h("admin"))).json()
    assert rep["allowances"]["api_requests_used"] == 2 and rep["allowances"]["api_overage"] == 1
    assert rep["active_cases_by_lawyer"][0]["active_cases"] >= 0
    assert (await client.get(f"{API}/reports/firm", headers=firm.h("lawyer"))).status_code == 403
    assert (await client.get(f"{API}/usage?month=13", headers=firm.h("admin"))).status_code == 422


async def test_notification_inbox(client, firm):
    case = await add_case(client, firm)
    await client.post(f"{API}/cases/{case['id']}/clients", headers=firm.h("lawyer"), json={"user_ids": [firm.client["id"]]})
    h = firm.h("client")
    assert (await client.get(f"{API}/notifications/unread-count", headers=h)).json()["unread"] == 1
    nid = (await client.get(f"{API}/notifications", headers=h)).json()["items"][0]["id"]
    assert (await client.post(f"{API}/notifications/{nid}/read", headers=h)).json()["read_at"]
    assert (await client.get(f"{API}/notifications/unread-count", headers=h)).json()["unread"] == 0
    # someone else's notification is 404
    assert (await client.post(f"{API}/notifications/{nid}/read", headers=firm.h("lawyer"))).status_code == 404
    await client.post(f"{API}/notifications/read-all", headers=firm.h("lawyer"))
    assert (await client.get(f"{API}/notifications/unread-count", headers=firm.h("lawyer"))).json()["unread"] == 0


# ------------------------------------------------------------------ i18n
async def test_languages_endpoint(client):
    langs = (await client.get(f"{API}/meta/languages")).json()
    assert len(langs) == 23  # English + 22 Eighth Schedule languages
    rtl = {lang["code"] for lang in langs if lang["rtl"]}
    assert rtl == {"ur", "ks", "sd"}
    assert {lang["code"] for lang in langs if lang["notification_templates"]} >= {"en", "hi", "te", "ta", "ur"}
    enums = (await client.get(f"{API}/meta/enums")).json()
    assert "junior_advocate" in enums["roles"] and enums["default_reminder_offsets_days"] == [7, 3, 1, 0]


def test_translation_fallbacks():
    assert len(LANGUAGES) == 23
    assert t("reminder.title", "hi", title="X").startswith("अनुस्मारक")
    assert t("reminder.title", "sat", title="X") == "Reminder: X"  # no Santali file yet -> English
    assert t("case.updated.body", "ta", count=2, summary="s") == "2 change(s) detected: s"  # missing key -> English
    assert t("unknown.key", "hi") == "unknown.key"


# ------------------------------------------------------------------ eCourtsIndia adapter (HTTP mocked)
SAMPLE = {
    "data": {
        "cnr": "DLHC010001232024",
        "caseTitle": "Ramesh Kumar vs State",
        "caseNumber": "W.P.(C) 123/2024",
        "caseType": "W.P.(C)",
        "courtName": "High Court of Delhi",
        "state": "Delhi",
        "caseStatus": "Pending",
        "nextHearingDate": "2026-12-01",
        "filingDate": "10-01-2024",
        "parties": {"petitioners": [{"name": "Ramesh Kumar"}], "respondents": ["State"]},
        "advocates": [{"advocateName": "P. Rao"}],
        "judges": [{"judgeName": "A. Sharma"}],
        "hearings": [{"hearingDate": "2024-03-01", "purpose": "Admission"}, {"date": "bad-date"}],
        "files": {"files": [{"fileName": "order_1.pdf", "orderDate": "2024-03-01", "markdownContent": "..."}]},
        "dateModified": "2026-11-01T10:00:00Z",
    }
}


def test_parse_case_defensively():
    rec = _parse_case("DLHC010001232024", SAMPLE)
    assert rec.title == "Ramesh Kumar vs State" and rec.court_name == "High Court of Delhi"
    assert rec.petitioners == ["Ramesh Kumar"] and rec.respondents == ["State"]
    assert rec.advocates == ["P. Rao"] and rec.judge == "A. Sharma"
    assert str(rec.next_hearing_date) == "2026-12-01" and str(rec.filing_date) == "2024-01-10"
    assert len(rec.hearings) == 1 and len(rec.orders) == 1
    assert rec.provider_modified_at == "2026-11-01T10:00:00Z" and not rec.is_disposed
    minimal = _parse_case("ABCD123456789012", {"petitioner": "A", "respondent": "B", "status": "Disposed"})
    assert minimal.title == "A vs B" and minimal.is_disposed


def _provider(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://webapi.ecourtsindia.com")
    return ECourtsIndiaProvider(client=client)


async def test_adapter_endpoints_and_errors():
    seen = []

    def handler(req: httpx.Request):
        seen.append((req.method, req.url.path, dict(req.url.params)))
        if req.url.path.endswith("/refresh"):
            return httpx.Response(200, json={"queued": True})
        if req.url.path == "/api/partner/search":
            return httpx.Response(200, json={"data": {"results": [{"cnr": "DLHC010001232024", "caseTitle": "R vs S"}]}})
        if "ZZZZ" in req.url.path:
            return httpx.Response(404)
        return httpx.Response(200, json=SAMPLE)

    p = _provider(handler)
    rec = await p.get_case("DLHC010001232024")
    assert rec.cnr == "DLHC010001232024"
    await p.request_refresh("DLHC010001232024")
    res = await p.search(query="138/2024", court_code="DLHC01", page_size=500)
    assert res == [{"cnr": "DLHC010001232024", "title": "R vs S", "court_name": None, "case_number": None}]
    assert seen[1][0] == "POST" and seen[1][1] == "/api/partner/case/DLHC010001232024/refresh"
    assert seen[2][2]["PageSize"] == "100" and seen[2][2]["CourtCodes"] == "DLHC01"
    with pytest.raises(CaseNotFound):
        await p.get_case("ZZZZ999999999999")


async def test_adapter_auth_and_retry_on_429():
    calls = {"n": 0}

    def flaky(req):
        calls["n"] += 1
        return httpx.Response(429) if calls["n"] < 3 else httpx.Response(200, json=SAMPLE)

    rec = await _provider(flaky).get_case("DLHC010001232024")
    assert rec.title and calls["n"] == 3
    with pytest.raises(ProviderAuthError):
        await _provider(lambda r: httpx.Response(401)).get_case("DLHC010001232024")
    with pytest.raises(ProviderAuthError):
        ECourtsIndiaProvider()  # no API key configured


# ------------------------------------------------------------------ migrations
def test_alembic_upgrade_on_fresh_database():
    url = "postgresql+asyncpg://postgres@localhost:5432/cms_migration_check"
    subprocess.run(["psql", "-h", "localhost", "-U", "postgres", "-qc", "DROP DATABASE IF EXISTS cms_migration_check"], check=True)
    subprocess.run(["psql", "-h", "localhost", "-U", "postgres", "-qc", "CREATE DATABASE cms_migration_check"], check=True)
    env = {**os.environ, "DATABASE_URL": url}
    root = os.path.dirname(os.path.dirname(__file__))
    up = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=root, env=env, capture_output=True, text=True, check=False
    )
    assert up.returncode == 0, up.stderr
    check = subprocess.run([sys.executable, "-m", "alembic", "check"], cwd=root, env=env, capture_output=True, text=True, check=False)
    assert check.returncode == 0, check.stdout + check.stderr  # models and migrations are in sync


def test_ecourtsindia_parser_snake_case_shape():
    from app.providers.court.ecourtsindia import _parse_case

    payload = {
        "data": {
            "courtCaseData": {
                "cnr": "APAN150001142026",
                "case_number": "OS/114/2026",
                "case_type": "OS",
                "filing_date": "2026-01-12",
                "registration_date": "12-01-2026",
                "next_hearing_date": "2026-12-03",
                "status": "Pending",
                "parties": {"petitioners": [{"name": "A Kumar"}], "respondents": [{"name": "B Rao"}]},
                "advocates": {"petitioner": [{"name": "Adv X"}], "respondent": [{"name": "Adv Y"}]},
                "judges": [{"name": "Principal Junior Civil Judge"}],
                "court": {"name": "Anantapur District Court", "state": "Andhra Pradesh"},
                "hearing_history": [{"business_on_date": "2026-11-05", "purpose_of_hearing": "Appearance"}],
            }
        }
    }
    r = _parse_case("APAN150001142026", payload)
    assert r.title == "A Kumar vs B Rao"
    assert r.case_number == "OS/114/2026" and r.case_type == "OS"
    assert r.court_name == "Anantapur District Court" and r.state == "Andhra Pradesh"
    assert r.next_hearing_date.isoformat() == "2026-12-03"
    assert r.advocates == ["Adv X", "Adv Y"] and r.judge == "Principal Junior Civil Judge"
    assert len(r.hearings) == 1 and r.hearings[0].purpose == "Appearance"


def test_ecourtsindia_parser_live_shape():
    """Shape copied from a live eCourtsIndia /api/partner/case/{cnr} reply (names changed)."""
    from app.providers.court.ecourtsindia import _parse_case

    payload = {
        "data": {
            "courtCaseData": {
                "cnr": "APAN150001142026",
                "caseNumber": "203200000042026",
                "district": "Ananthapur",
                "state": "AP",
                "courtName": "Junior Civil Judge Court, Rayadurg",
                "historyOfCaseHearings": [
                    {
                        "judge": "Principal junior civil judge",
                        "businessOnDate": "2026-03-13",
                        "hearingDate": "2026-05-07",
                        "purposeOfListing": "SUMMONS",
                    },
                    {
                        "judge": "Principal junior civil judge",
                        "businessOnDate": "2026-05-07",
                        "hearingDate": "2026-06-12",
                        "purposeOfListing": "SUMMONS",
                    },
                ],
                "stageOfCase": "SUMMONS_ISSUED",
                "interimOrders": [],
                "caseType": "CMAppl",
                "caseTypeRaw": "MC",
                "caseStatus": "PENDING",
                "filingNumber": "76/2026",
                "filingDate": "2026-02-11",
                "registrationNumber": "4/2026",
                "registrationDate": "2026-02-11",
                "nextHearingDate": "2026-11-18",
                "judges": ["Principal junior civil judge"],
                "petitioners": ["A Reddy"],
                "petitionerAdvocates": ["Adv S"],
                "respondents": ["B Latha", "C Teja"],
                "respondentAdvocates": [],
                "judgmentOrders": [],
            },
            "entityInfo": {"cnr": "APAN150001142026", "dateModified": "2026-10-18T21:31:59.250234Z"},
            "files": {"files": []},
            "descriptions": {
                "enumLookup": {
                    "caseType": {"CMAppl": "Civil Miscellaneous Application"},
                    "caseStatus": {"PENDING": "Pending"},
                }
            },
        },
        "meta": {"request_id": "x"},
    }
    r = _parse_case("APAN150001142026", payload)
    assert r.title == "A Reddy vs B Latha"
    assert r.case_number == "MC 4/2026"
    assert r.case_type == "Civil Miscellaneous Application"
    assert r.status == "Pending" and not r.is_disposed
    assert r.stage == "Summons issued"
    assert r.court_level == "district" and r.district == "Ananthapur"
    assert r.advocates == ["Adv S"]
    assert r.case_year == 2026
    assert [h.hearing_date.isoformat() for h in r.hearings] == ["2026-03-13", "2026-05-07"]
    assert r.hearings[0].purpose == "Summons"
    assert r.next_hearing_date.isoformat() == "2026-11-18"
    assert r.provider_modified_at == "2026-10-18T21:31:59.250234Z"
