from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.models import ApiUsage, CallAttempt, NotificationJob
from app.providers.messaging import MockEmailProvider, MockSmsProvider, MockVoiceProvider
from app.services.reminders import dispatch_due
from tests.conftest import API, add_case, make_firm

IST = ZoneInfo("Asia/Kolkata")


def _future(days=10, hour=11):
    d = (datetime.now(IST) + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return d.isoformat()


async def _jobs(db, reminder_id):
    return (await db.execute(select(NotificationJob).where(NotificationJob.reminder_id == reminder_id))).scalars().all()


async def test_create_reminder_expands_jobs(client, firm, db):
    body = {
        "title": "File reply",
        "reminder_type": "filing",
        "event_at": _future(10),
        "offsets_days": [7, 3, 1, 0],
        "channels": ["in_app", "email"],
    }
    r = await client.post(f"{API}/reminders", headers=firm.h("lawyer"), json=body)
    assert r.status_code == 201, r.text
    rem = r.json()
    assert rem["recipient_ids"] == [firm.lawyer["id"]]  # defaults to creator
    jobs = (await client.get(f"{API}/reminders/{rem['id']}/jobs", headers=firm.h("lawyer"))).json()
    assert len(jobs) == 8  # 4 times x 2 channels x 1 recipient
    # "N days before" reminders go out at 08:00 IST
    first = datetime.fromisoformat(jobs[0]["scheduled_at"]).astimezone(IST)
    assert first.hour == 8


async def test_past_offsets_are_skipped(client, firm):
    r = await client.post(
        f"{API}/reminders", headers=firm.h("lawyer"), json={"title": "Soon", "event_at": _future(2), "offsets_days": [7, 3, 1]}
    )
    jobs = (await client.get(f"{API}/reminders/{r.json()['id']}/jobs", headers=firm.h("lawyer"))).json()
    assert len(jobs) == 1  # only the 1-day-before one is in the future


async def test_custom_times_and_validation(client, firm):
    t = (datetime.now(UTC) + timedelta(hours=5)).isoformat()
    r = await client.post(
        f"{API}/reminders",
        headers=firm.h("lawyer"),
        json={"title": "Call client", "event_at": _future(1), "offsets_days": [], "custom_times": [t]},
    )
    assert r.status_code == 201
    jobs = (await client.get(f"{API}/reminders/{r.json()['id']}/jobs", headers=firm.h("lawyer"))).json()
    assert len(jobs) == 1
    bad = [
        {"title": "No event", "offsets_days": [1]},
        {"title": "Neg", "event_at": _future(), "offsets_days": [-1]},
        {"title": "Nothing", "event_at": _future(), "offsets_days": []},
        {"title": "Bad chan", "event_at": _future(), "channels": ["pigeon"]},
    ]
    for b in bad:
        assert (await client.post(f"{API}/reminders", headers=firm.h("lawyer"), json=b)).status_code == 422


async def test_hearing_based_reminder_with_case_clients(client, firm):
    case = await add_case(client, firm, clients=[firm.client["id"]])
    hearing = (await client.get(f"{API}/hearings/case/{case['id']}", headers=firm.h("lawyer"))).json()[0]
    r = await client.post(
        f"{API}/reminders",
        headers=firm.h("lawyer"),
        json={
            "title": "Prepare arguments",
            "reminder_type": "preparation",
            "hearing_id": hearing["id"],
            "offsets_days": [1],
            "include_case_clients": True,
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["case_id"] == case["id"]
    assert set(body["recipient_ids"]) == {firm.lawyer["id"], firm.client["id"]} or firm.client["id"] in body["recipient_ids"]


async def test_junior_restrictions(client, firm):
    r = await client.post(
        f"{API}/reminders",
        headers=firm.h("junior"),
        json={"title": "For lawyer", "event_at": _future(), "recipient_ids": [firm.lawyer["id"]]},
    )
    assert r.status_code == 403
    r = await client.post(f"{API}/reminders", headers=firm.h("junior"), json={"title": "Mine", "event_at": _future()})
    assert r.status_code == 201
    case = await add_case(client, firm)
    r = await client.post(
        f"{API}/reminders", headers=firm.h("junior"), json={"title": "Case", "event_at": _future(), "case_id": case["id"]}
    )
    assert r.status_code == 404  # not assigned
    assert (await client.post(f"{API}/reminders", headers=firm.h("client"), json={"title": "x", "event_at": _future()})).status_code == 403


async def test_client_recipient_requires_case(client, firm):
    r = await client.post(
        f"{API}/reminders", headers=firm.h("lawyer"), json={"title": "x", "event_at": _future(), "recipient_ids": [firm.client["id"]]}
    )
    assert r.status_code == 422


async def test_dispatch_in_app_localised(client, super_token, db):
    hf = await make_firm(client, super_token, "hindi", client_lang="hi")
    case = await add_case(client, hf, clients=[hf.client["id"]])
    r = await client.post(
        f"{API}/reminders",
        headers=hf.h("lawyer"),
        json={"title": "Hearing", "case_id": case["id"], "event_at": _future(3), "offsets_days": [1], "recipient_ids": [hf.client["id"]]},
    )
    rid = r.json()["id"]
    stats = await dispatch_due(db, datetime.now(UTC) + timedelta(days=5))
    await db.commit()
    assert stats["processed"] >= 1
    jobs = await _jobs(db, rid)
    assert all(j.status == "sent" for j in jobs)
    notes = (await client.get(f"{API}/notifications?unread=true", headers=hf.h("client"))).json()
    reminder_notes = [n for n in notes["items"] if n["kind"] == "reminder"]
    assert reminder_notes and reminder_notes[0]["title"].startswith("अनुस्मारक")


async def test_email_and_sms_respect_preferences_and_consent(client, firm, db):
    r = await client.post(
        f"{API}/reminders",
        headers=firm.h("lawyer"),
        json={"title": "Pay fee", "event_at": _future(2), "offsets_days": [1], "channels": ["email", "sms"]},
    )
    rid = r.json()["id"]
    await dispatch_due(db, datetime.now(UTC) + timedelta(days=3))
    await db.commit()
    jobs = {j.channel: j for j in await _jobs(db, rid)}
    assert jobs["email"].status == "sent" and MockEmailProvider.sent[0]["to"] == "lawyer@alpha.in"
    assert jobs["sms"].status == "skipped" and "consent" in jobs["sms"].last_error
    assert MockSmsProvider.sent == []
    # after opting in, SMS is delivered
    await client.patch(f"{API}/auth/me", headers=firm.h("lawyer"), json={"notify_sms": True, "sms_consent": True})
    r = await client.post(
        f"{API}/reminders",
        headers=firm.h("lawyer"),
        json={"title": "Pay fee 2", "event_at": _future(2), "offsets_days": [1], "channels": ["sms"]},
    )
    await dispatch_due(db, datetime.now(UTC) + timedelta(days=3))
    await db.commit()
    assert MockSmsProvider.sent and MockSmsProvider.sent[-1]["to"] == firm.lawyer["phone"]


async def _call_setup(client, firm, db):
    await client.patch(f"{API}/auth/me", headers=firm.h("lawyer"), json={"notify_call": True, "call_consent": True})
    r = await client.post(
        f"{API}/reminders",
        headers=firm.h("lawyer"),
        json={"title": "Hearing", "event_at": _future(3), "offsets_days": [1], "channels": ["call"]},
    )
    job = (await _jobs(db, r.json()["id"]))[0]
    return r.json()["id"], job


def _ist(hour):
    d = datetime.now(IST) + timedelta(days=5)
    return d.replace(hour=hour, minute=0, second=0, microsecond=0).astimezone(UTC)


async def test_call_without_consent_is_skipped(client, firm, db):
    r = await client.post(
        f"{API}/reminders",
        headers=firm.h("lawyer"),
        json={"title": "Hearing call", "event_at": _future(3), "offsets_days": [1], "channels": ["call"]},
    )
    await dispatch_due(db, _ist(10))
    await db.commit()
    assert (await _jobs(db, r.json()["id"]))[0].status == "skipped"
    assert MockVoiceProvider.sent == []


async def test_call_outside_window_is_deferred(client, firm, db):
    rid, _ = await _call_setup(client, firm, db)
    stats = await dispatch_due(db, _ist(22))
    await db.commit()
    assert stats.get("deferred") == 1 and MockVoiceProvider.sent == []
    job = (await _jobs(db, rid))[0]
    await db.refresh(job)
    assert job.status == "pending" and job.scheduled_at.astimezone(IST).hour == 9


async def test_call_retry_on_no_answer_then_fail(client, firm, db):
    rid, _ = await _call_setup(client, firm, db)
    now = _ist(10)
    for attempt in range(1, 4):
        await dispatch_due(db, now)
        await db.commit()
        call_id = MockVoiceProvider.sent[-1]["id"]
        assert MockVoiceProvider.sent[-1]["language"] == "en"
        r = await client.post(
            f"{API}/webhooks/exotel/call-status?token=test-webhook-secret", data={"CallSid": call_id, "Status": "no-answer"}
        )
        assert r.json()["matched"]
        now += timedelta(minutes=11)
    job = (await _jobs(db, rid))[0]
    await db.refresh(job)
    assert job.status == "failed" and job.attempts == 3
    attempts = (await db.execute(select(CallAttempt).where(CallAttempt.job_id == job.id))).scalars().all()
    assert [a.status for a in attempts] == ["no-answer"] * 3


async def test_call_completed_records_billable_minutes(client, firm, db):
    await _call_setup(client, firm, db)
    await dispatch_due(db, _ist(10))
    await db.commit()
    call_id = MockVoiceProvider.sent[-1]["id"]
    await client.post(
        f"{API}/webhooks/exotel/call-status?token=test-webhook-secret",
        data={"CallSid": call_id, "Status": "completed", "ConversationDuration": "75"},
    )
    usage = (await db.execute(select(ApiUsage).where(ApiUsage.operation == "call"))).scalars().all()
    assert usage[0].units == 2  # 75s -> 2 billable minutes
    report = (await client.get(f"{API}/reports/firm", headers=firm.h("admin"))).json()
    assert report["usage"]["call_minutes"] == 2
    assert report["call_attempts_by_status"] == {"completed": 1}


async def test_call_provider_error_retries(client, firm, db):
    rid, _ = await _call_setup(client, firm, db)
    MockVoiceProvider.fail_numbers.add(firm.lawyer["phone"])
    stats = await dispatch_due(db, _ist(10))
    await db.commit()
    assert stats.get("retry") == 1
    job = (await _jobs(db, rid))[0]
    await db.refresh(job)
    assert job.status == "pending" and "simulated" in job.last_error


async def test_webhook_auth(client):
    r = await client.post(f"{API}/webhooks/exotel/call-status?token=wrong", data={"CallSid": "x", "Status": "completed"})
    assert r.status_code == 401


async def test_update_rebuilds_and_delete_cancels(client, firm, db):
    r = await client.post(
        f"{API}/reminders", headers=firm.h("lawyer"), json={"title": "Meet", "event_at": _future(10), "offsets_days": [3]}
    )
    rid = r.json()["id"]
    r = await client.patch(
        f"{API}/reminders/{rid}", headers=firm.h("lawyer"), json={"offsets_days": [7, 3, 1], "channels": ["in_app", "email"]}
    )
    assert r.status_code == 200
    jobs = (await client.get(f"{API}/reminders/{rid}/jobs", headers=firm.h("lawyer"))).json()
    assert sum(j["status"] == "pending" for j in jobs) == 6
    assert sum(j["status"] == "cancelled" for j in jobs) == 1
    assert (await client.patch(f"{API}/reminders/{rid}", headers=firm.h("lawyer"), json={"offsets_days": [999]})).status_code == 422
    assert (await client.delete(f"{API}/reminders/{rid}", headers=firm.h("lawyer"))).status_code == 204
    assert (await client.get(f"{API}/reminders/{rid}", headers=firm.h("lawyer"))).status_code == 404


async def test_inactive_reminder_jobs_skipped(client, firm, db):
    r = await client.post(f"{API}/reminders", headers=firm.h("lawyer"), json={"title": "Meet", "event_at": _future(3), "offsets_days": [1]})
    rid = r.json()["id"]
    jobs = await _jobs(db, rid)
    await client.patch(f"{API}/reminders/{rid}", headers=firm.h("lawyer"), json={"is_active": False})
    await db.refresh(jobs[0])
    assert jobs[0].status == "cancelled"


async def test_reminder_listing_scopes(client, firm):
    await client.post(f"{API}/reminders", headers=firm.h("lawyer"), json={"title": "Lawyer only", "event_at": _future()})
    await client.post(f"{API}/reminders", headers=firm.h("junior"), json={"title": "Junior only", "event_at": _future()})
    assert (await client.get(f"{API}/reminders", headers=firm.h("admin"))).json()["total"] == 2
    j = (await client.get(f"{API}/reminders", headers=firm.h("junior"))).json()
    assert [i["title"] for i in j["items"]] == ["Junior only"]
    mine = (await client.get(f"{API}/reminders?mine=true", headers=firm.h("lawyer"))).json()
    assert [i["title"] for i in mine["items"]] == ["Lawyer only"]
    stats = (await client.get(f"{API}/reminders/stats/summary", headers=firm.h("admin"))).json()
    assert stats["in_app"]["pending"] >= 2


async def test_cross_tenant_reminder_404(client, firm, other_firm):
    r = await client.post(f"{API}/reminders", headers=firm.h("lawyer"), json={"title": "Xtra item", "event_at": _future()})
    assert (await client.get(f"{API}/reminders/{r.json()['id']}", headers=other_firm.h("admin"))).status_code == 404
    r = await client.post(
        f"{API}/reminders",
        headers=firm.h("lawyer"),
        json={"title": "Xtra item", "event_at": _future(), "recipient_ids": [other_firm.lawyer["id"]]},
    )
    assert r.status_code == 404
