"""Deterministic in-memory provider for development and tests (no API credits used)."""

from copy import deepcopy
from datetime import date, timedelta

from app.core.timeutil import today_local
from app.providers.court.base import CaseNotFound, CaseRecord, CourtDataProvider, HearingRecord, OrderRecord


def _seed() -> dict[str, CaseRecord]:
    today = today_local()
    return {
        "DLHC010001232024": CaseRecord(
            cnr="DLHC010001232024",
            title="Ramesh Kumar vs State of NCT of Delhi",
            case_number="W.P.(C) 123/2024",
            case_type="W.P.(C)",
            case_year=2024,
            court_name="High Court of Delhi",
            court_level="high_court",
            state="Delhi",
            judge="Hon'ble Justice A. Sharma",
            petitioners=["Ramesh Kumar"],
            respondents=["State of NCT of Delhi"],
            advocates=["Adv. P. Rao"],
            filing_date=date(2024, 1, 10),
            registration_date=date(2024, 1, 12),
            status="Pending",
            stage="Arguments",
            next_hearing_date=today + timedelta(days=5),
            hearings=[
                HearingRecord(date(2024, 3, 1), "Admission", "Hon'ble Justice A. Sharma"),
                HearingRecord(today + timedelta(days=5), "Arguments", "Hon'ble Justice A. Sharma"),
            ],
            orders=[OrderRecord(date(2024, 3, 1), "Notice issued")],
            provider_modified_at="2026-01-01T00:00:00Z",
        ),
        "TSHY020004562023": CaseRecord(
            cnr="TSHY020004562023",
            title="Lakshmi Devi vs Venkat Rao",
            case_number="O.S. 456/2023",
            case_type="O.S.",
            case_year=2023,
            court_name="City Civil Court, Hyderabad",
            court_level="district",
            state="Telangana",
            district="Hyderabad",
            petitioners=["Lakshmi Devi"],
            respondents=["Venkat Rao"],
            filing_date=date(2023, 6, 2),
            status="Pending",
            stage="Evidence",
            next_hearing_date=today + timedelta(days=12),
            hearings=[HearingRecord(today + timedelta(days=12), "Evidence")],
            provider_modified_at="2026-01-01T00:00:00Z",
        ),
        "MHMU030007892022": CaseRecord(
            cnr="MHMU030007892022",
            title="Acme Traders vs Sunrise Exports",
            case_number="Comm. Suit 789/2022",
            court_name="Bombay City Civil Court",
            court_level="district",
            state="Maharashtra",
            status="Disposed",
            is_disposed=True,
            provider_modified_at="2025-06-01T00:00:00Z",
        ),
    }


class MockCourtProvider(CourtDataProvider):
    name = "mock"
    _store: dict[str, CaseRecord] = _seed()
    calls: list[tuple[str, str]] = []

    @classmethod
    def reset(cls) -> None:
        cls._store = _seed()
        cls.calls = []

    @classmethod
    def upsert(cls, record: CaseRecord) -> None:
        cls._store[record.cnr] = record

    async def get_case(self, cnr: str) -> CaseRecord:
        self.calls.append(("detail", cnr))
        if cnr not in self._store:
            raise CaseNotFound(cnr)
        return deepcopy(self._store[cnr])

    async def request_refresh(self, cnr: str) -> None:
        self.calls.append(("refresh", cnr))

    async def search(self, **filters) -> list[dict]:
        self.calls.append(("search", str(filters)))
        q = (filters.get("query") or "").lower()
        return [
            {"cnr": r.cnr, "title": r.title, "court_name": r.court_name, "case_number": r.case_number}
            for r in self._store.values()
            if not q or q in r.title.lower() or q in (r.case_number or "").lower()
        ]
