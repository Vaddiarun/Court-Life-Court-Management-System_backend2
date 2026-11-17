from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select, update

from app.core.config import settings
from app.core.timeutil import today_local
from app.models import Case, NotificationJob, Reminder
from app.providers.court import set_court_provider
from app.providers.court.base import CaseRecord, HearingRecord, OrderRecord, ProviderError
from app.providers.court.mock import MockCourtProvider
from app.services.sync import collect_refreshed, compute_next_sync, request_refreshes
from tests.conftest import API, add_case

CNR = "DLHC010001232024"


def _change_upstream(**changes):
    rec = MockCourtProvider._store[CNR]
    for k, v in changes.items():
        setattr(rec, k, v)


async def _clear_cooldown(db, case_id):
    await db.execute(update(Case).where(Case.id == case_id).values(last_manual_sync_at=None))
    await db.commit()


async def test_sync_now_unchanged(client, firm):
    case = await add_case(client, firm)
    r = await client.post(f"{API}/cases/{case['id']}/sync", headers=firm.h("lawyer"))
    assert r.status_code == 200
    assert r.json()["run"]["status"] == "unchanged" and r.json()["run"]["changes"] == 0
    # Sync Now also queues an async provider refresh
    assert ("refresh", CNR) in MockCourtProvider.calls


async def test_sync_detects_changes_events_alerts_and_new_hearing(client, firm, db):
    case = await add_case(client, firm)
    await client.post(f"{API}/cases/{case['id']}/assignments", headers=firm.h("lawyer"), json={"user_ids": [firm.junior["id"]]})
    new_date = today_local() + timedelta(days=20)
    _change_upstream(
        status="Pending - Reserved",
        stage="Orders reserved",
        next_hearing_date=new_date,
        orders=[OrderRecord(date(2024, 3, 1), "Notice issued"), OrderRecord(today_local(), "Interim order")],
    )
    MockCourtProvider._store[CNR].hearings.append(HearingRecord(new_date, "Orders"))
    r = await client.post(f"{API}/cases/{case['id']}/sync", headers=firm.h("junior"))
    run = r.json()["run"]
    assert run["status"] == "success" and run["changes"] == 4  # status, stage, next_hearing_date, new_order
    assert r.json()["case"]["next_hearing_date"] == new_date.isoformat()

    events = (await client.get(f"{API}/cases/{case['id']}/events", headers=firm.h("lawyer"))).json()
    kinds = {e["event_type"] for e in events}
    assert {"status_changed", "stage_changed", "next_hearing_date_changed", "new_order"} <= kinds

    hearings = (await client.get(f"{API}/hearings/case/{case['id']}", headers=firm.h("lawyer"))).json()
    assert new_date.isoformat() in {h["hearing_date"] for h in hearings}

    # lead lawyer, assigned junior and firm admin were all alerted; clients were not
    for who in ("lawyer", "junior", "admin"):
        n = (await client.get(f"{API}/notifications", headers=firm.h(who))).json()
        assert any(i["kind"] == "case_update" for i in n["items"]), who

    # old auto reminder retired, a new one created for the new date
    reminders = (await db.execute(select(Reminder).where(Reminder.case_id == case["id"]))).scalars().all()
    active = [r for r in reminders if r.is_active]
    assert len(reminders) == 2 and len(active) == 1
    assert active[0].event_at.date() == new_date
    cancelled = (await db.execute(select(NotificationJob).where(NotificationJob.status == "cancelled"))).scalars().all()
    assert cancelled  # pending jobs of the old reminder were cancelled


async def test_sync_now_cooldown(client, firm):
    case = await add_case(client, firm)
    assert (await client.post(f"{API}/cases/{case['id']}/sync", headers=firm.h("lawyer"))).status_code == 200
    r = await client.post(f"{API}/cases/{case['id']}/sync", headers=firm.h("lawyer"))
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0


async def test_clients_cannot_sync(client, firm):
    case = await add_case(client, firm, clients=[firm.client["id"]])
    assert (await client.post(f"{API}/cases/{case['id']}/sync", headers=firm.h("client"))).status_code == 403


async def test_provider_failure_is_recorded_not_raised(client, firm, db):
    case = await add_case(client, firm)

    class Broken(MockCourtProvider):
        async def get_case(self, cnr):
            raise ProviderError("upstream timeout")

    set_court_provider(Broken())
    r = await client.post(f"{API}/cases/{case['id']}/sync", headers=firm.h("lawyer"))
    assert r.status_code == 200 and r.json()["run"]["status"] == "failed"
    assert "upstream timeout" in r.json()["case"]["last_sync_error"]
    runs = (await client.get(f"{API}/cases/{case['id']}/sync-runs", headers=firm.h("lawyer"))).json()
    assert runs[0]["status"] == "failed"
    dash = (await client.get(f"{API}/dashboard", headers=firm.h("lawyer"))).json()
    assert dash["cases_with_sync_errors"] == 1


async def test_scheduled_two_phase_sync(client, firm, db):
    case = await add_case(client, firm)
    now = datetime.now(UTC)
    await db.execute(update(Case).where(Case.id == case["id"]).values(next_sync_at=now - timedelta(minutes=1)))
    await db.commit()
    MockCourtProvider.calls.clear()

    assert await request_refreshes(db, now) == 1
    await db.commit()
    assert MockCourtProvider.calls == [("refresh", CNR)]
    # nothing collected before the settle window
    assert (await collect_refreshed(db, now))["checked"] == 0
    _change_upstream(status="Disposed", is_disposed=True)
    later = now + timedelta(minutes=settings.SYNC_REFRESH_SETTLE_MINUTES + 1)
    stats = await collect_refreshed(db, later)
    await db.commit()
    assert stats == {"checked": 1, "changed": 1, "failed": 0}
    row = await db.get(Case, case["id"])
    await db.refresh(row)
    assert row.is_disposed and row.refresh_requested_at is None
    assert row.next_sync_at > later + timedelta(days=29)  # disposed -> monthly
    # not due again -> nothing requested
    assert await request_refreshes(db, later) == 0


async def test_unchanged_detail_writes_no_snapshot_or_events(client, firm, db):
    case = await add_case(client, firm)
    await _clear_cooldown(db, case["id"])
    await client.post(f"{API}/cases/{case['id']}/sync", headers=firm.h("lawyer"))
    events = (await client.get(f"{API}/cases/{case['id']}/events", headers=firm.h("lawyer"))).json()
    assert events == []


def _case(next_hearing, disposed=False):
    return Case(cnr=CNR, sync_enabled=True, is_disposed=disposed, next_hearing_date=next_hearing)


def test_priority_schedule():
    now = datetime(2026, 11, 15, 6, 0, tzinfo=UTC)
    today = now.date()
    assert compute_next_sync(_case(today), now) == now + timedelta(hours=12)
    assert compute_next_sync(_case(today + timedelta(days=2)), now) == now + timedelta(days=1)
    assert compute_next_sync(_case(today + timedelta(days=20)), now) == now + timedelta(days=3)
    assert compute_next_sync(_case(today + timedelta(days=90)), now) == now + timedelta(days=7)
    assert compute_next_sync(_case(None), now) == now + timedelta(days=3)
    assert compute_next_sync(_case(today, disposed=True), now) == now + timedelta(days=30)
    c = _case(today)
    c.sync_enabled = False
    assert compute_next_sync(c, now) is None


async def test_provider_webhook_marks_case_for_collection(client, firm, db):
    await add_case(client, firm)
    assert (await client.post(f"{API}/webhooks/court-provider?token=bad", json={"cnr": CNR})).status_code == 401
    r = await client.post(f"{API}/webhooks/court-provider?token=test-webhook-secret", json={"cnr": CNR})
    assert r.json()["cases_marked"] == 1
    _change_upstream(stage="Judgment")
    stats = await collect_refreshed(db)
    await db.commit()
    assert stats["changed"] == 1


async def test_operator_job_trigger(client, firm, super_token):
    await add_case(client, firm)
    from tests.conftest import auth

    r = await client.post(f"{API}/admin/jobs/sync", headers=auth(super_token))
    assert r.status_code == 200 and "refresh_requested" in r.json()
    assert (await client.post(f"{API}/admin/jobs/sync", headers=firm.h("admin"))).status_code == 403


async def test_usage_cost_matches_prd_rate_card(client, firm, db):
    """PRD 5: one search + one detail = Rs 0.70; refresh = Rs 0.05."""
    await client.post(f"{API}/cases/search", headers=firm.h("lawyer"), json={"query": "ramesh"})
    await client.post(f"{API}/cases/lookup", headers=firm.h("lawyer"), json={"cnr": CNR})
    usage = (await client.get(f"{API}/usage", headers=firm.h("admin"))).json()
    assert usage["estimated_total_inr"] == 0.70
    case = await add_case(client, firm)
    await client.post(f"{API}/cases/{case['id']}/sync", headers=firm.h("lawyer"))  # detail + refresh
    usage = (await client.get(f"{API}/usage", headers=firm.h("admin"))).json()
    assert usage["estimated_total_inr"] == round(0.70 + 0.50 + 0.05, 2)


async def test_new_case_record_from_provider_with_unknown_title():
    rec = CaseRecord(cnr=CNR, title="")
    assert rec.content_hash() == CaseRecord.from_dict(rec.comparable()).content_hash()
