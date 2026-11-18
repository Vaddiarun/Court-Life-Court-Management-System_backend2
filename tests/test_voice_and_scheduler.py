"""Twilio voice provider, test-call endpoint, call language fallback, Twilio webhook and in-process scheduler."""

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models import CallAttempt, NotificationJob
from app.providers import messaging
from app.providers.messaging import ChannelError, MockVoiceProvider, TwilioVoiceProvider
from tests.conftest import API

IST = ZoneInfo("Asia/Kolkata")


class FakeTwilio:
    def __init__(self, status=201):
        self.requests, self.status = [], status

    def __call__(self, req: httpx.Request):
        form = dict(httpx.QueryParams(req.content.decode()))
        self.requests.append((req.url.path, form, req.headers.get("authorization")))
        if self.status >= 400:
            return httpx.Response(self.status, json={"code": 21215, "message": "Geo permission not enabled for India"})
        return httpx.Response(201, json={"sid": f"CA{len(self.requests):032d}", "status": "queued"})


@pytest.fixture
def twilio(monkeypatch):
    fake = FakeTwilio()
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    monkeypatch.setattr(settings, "VOICE_PROVIDER", "twilio")
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setattr(settings, "TWILIO_FROM_NUMBER", "+12025550123")
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://abc.trycloudflare.com")
    monkeypatch.setattr(messaging, "get_voice", lambda: TwilioVoiceProvider(client=client))
    import app.api.v1.auth as auth_mod
    import app.services.reminders as rem_mod

    monkeypatch.setattr(auth_mod, "get_voice", lambda: TwilioVoiceProvider(client=client))
    monkeypatch.setattr(rem_mod, "get_voice", lambda: TwilioVoiceProvider(client=client))
    return fake


def test_twiml_languages_and_escaping():
    x = TwilioVoiceProvider.twiml("Rao & Sons <hearing>", "te")
    assert 'voice="Google.te-IN-Standard-C"' in x and "Rao &amp; Sons &lt;hearing&gt;" in x and x.count("<Say") == 2
    assert 'voice="Polly.Aditi" language="en-IN"' in TwilioVoiceProvider.twiml("hi", "sat")  # unsupported -> English voice
    p = TwilioVoiceProvider(client=httpx.AsyncClient())
    assert p.supports_language("hi") and not p.supports_language("kn")


def test_twilio_requires_config(monkeypatch):
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "")
    with pytest.raises(ChannelError):
        TwilioVoiceProvider()


async def test_test_call_endpoint_places_real_call(client, firm, twilio):
    await client.patch(f"{API}/auth/me", headers=firm.h("lawyer"), json={"language": "hi"})
    r = await client.post(f"{API}/auth/me/test-call", headers=firm.h("lawyer"))
    assert r.status_code == 200, r.text
    assert r.json()["provider"] == "twilio" and r.json()["call_id"].startswith("CA")
    path, form, authz = twilio.requests[0]
    assert path == "/2010-04-01/Accounts/ACtest/Calls.json" and authz.startswith("Basic ")
    assert form["To"] == firm.lawyer["phone"] and form["From"] == "+12025550123"
    assert "Google.hi-IN-Standard-E" in form["Twiml"] and "नमस्ते" in form["Twiml"]
    assert form["StatusCallback"].startswith("https://abc.trycloudflare.com/api/v1/webhooks/twilio/call-status?token=")


async def test_test_call_reports_provider_error(client, firm, monkeypatch):
    fake = FakeTwilio(status=400)
    import app.api.v1.auth as auth_mod

    monkeypatch.setattr(auth_mod, "get_voice", lambda: TwilioVoiceProvider(client=httpx.AsyncClient(transport=httpx.MockTransport(fake))))
    r = await client.post(f"{API}/auth/me/test-call", headers=firm.h("lawyer"))
    assert r.status_code == 502 and "Geo permission" in r.json()["detail"]


async def _call_reminder(client, firm, db):
    await client.patch(f"{API}/auth/me", headers=firm.h("lawyer"), json={"notify_call": True, "call_consent": True, "language": "kn"})
    soon = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    r = await client.post(
        f"{API}/reminders",
        headers=firm.h("lawyer"),
        json={
            "title": "Hearing call",
            "event_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
            "offsets_days": [],
            "custom_times": [soon],
            "channels": ["call"],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _ist_noon():
    return (datetime.now(IST) + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0).astimezone(UTC)


async def test_reminder_call_uses_english_when_language_has_no_voice(client, firm, db, twilio):
    from app.services.reminders import dispatch_due

    await _call_reminder(client, firm, db)
    await dispatch_due(db, _ist_noon())
    await db.commit()
    form = twilio.requests[0][1]
    assert 'language="en-IN"' in form["Twiml"] and "This is a reminder" in form["Twiml"]  # Kannada has no voice -> English
    attempt = (await db.execute(select(CallAttempt))).scalar_one()
    assert attempt.language == "en"


async def test_twilio_status_webhook_retries_no_answer(client, firm, db, twilio):
    from app.services.reminders import dispatch_due

    await _call_reminder(client, firm, db)
    await dispatch_due(db, _ist_noon())
    await db.commit()
    sid = (await db.execute(select(CallAttempt))).scalar_one().provider_call_id
    bad = await client.post(f"{API}/webhooks/twilio/call-status?token=nope", data={"CallSid": sid, "CallStatus": "no-answer"})
    assert bad.status_code == 401
    r = await client.post(
        f"{API}/webhooks/twilio/call-status?token={settings.WEBHOOK_SECRET}", data={"CallSid": sid, "CallStatus": "no-answer"}
    )
    assert r.json()["matched"]
    job = (await db.execute(select(NotificationJob))).scalar_one()
    await db.refresh(job)
    assert job.status == "pending"  # scheduled for retry
    r = await client.post(
        f"{API}/webhooks/twilio/call-status?token={settings.WEBHOOK_SECRET}",
        data={"CallSid": sid, "CallStatus": "completed", "CallDuration": "45"},
    )
    await db.refresh(job)
    assert job.status == "sent"


async def test_in_process_scheduler_dispatches_due_reminders(client, firm, db, monkeypatch, Session):
    import app.services.scheduler as sched

    monkeypatch.setattr(sched, "SessionLocal", Session)
    monkeypatch.setattr(settings, "SCHEDULER_INTERVAL_SECONDS", 1)
    now = datetime.now(UTC)
    r = await client.post(
        f"{API}/reminders",
        headers=firm.h("lawyer"),
        json={
            "title": "Due now",
            "event_at": (now + timedelta(hours=3)).isoformat(),
            "offsets_days": [],
            "custom_times": [(now + timedelta(seconds=1)).isoformat()],
        },
    )
    assert r.status_code == 201
    await asyncio.sleep(1.2)
    stop = asyncio.Event()
    task = asyncio.create_task(sched.scheduler_loop(stop))
    await asyncio.sleep(0.8)
    stop.set()
    await task
    notes = (await client.get(f"{API}/notifications", headers=firm.h("lawyer"))).json()["items"]
    assert any(n["kind"] == "reminder" and "Due now" in n["title"] for n in notes)
    assert MockVoiceProvider.sent == []


async def test_exotel_greeting_returns_message(client):
    from app.core.config import settings

    r = await client.get(
        "/api/v1/webhooks/exotel/greeting",
        params={"token": settings.WEBHOOK_SECRET, "CustomField": "job-1|en|Hearing for case ABC tomorrow at 10 AM."},
    )
    assert r.status_code == 200
    assert r.text == "Hearing for case ABC tomorrow at 10 AM."
    r = await client.get("/api/v1/webhooks/exotel/greeting", params={"token": "bad", "CustomField": "x"})
    assert r.status_code == 401


async def test_cron_tick_requires_token_and_runs_jobs(client):
    from app.core.config import settings

    url = "/api/v1/cron/tick"
    assert (await client.post(url)).status_code == 401
    assert (await client.post(url, headers={"X-Cron-Token": "wrong"})).status_code == 401
    r = await client.post(url, headers={"X-Cron-Token": settings.WEBHOOK_SECRET})
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"reminders", "collected"}
    r = await client.post(url, params={"jobs": "sync"}, headers={"X-Cron-Token": settings.WEBHOOK_SECRET})
    assert r.status_code == 200 and "refresh_requested" in r.json()
    r = await client.post(url, params={"jobs": "nope"}, headers={"X-Cron-Token": settings.WEBHOOK_SECRET})
    assert r.status_code == 422


async def test_exotel_call_sends_short_customfield_and_greeting_reads_it(client, Session, monkeypatch):
    """Exotel caps CustomField at 128 chars: we send 'vm:<id>' and the Greeting URL reads the text back."""
    import json as _json

    import httpx

    from app.core.config import settings
    from app.providers import messaging
    from app.providers.messaging import ExotelVoiceProvider

    import app.core.db as core_db

    monkeypatch.setattr(core_db, "SessionLocal", Session)
    sent = {}

    def handler(req: httpx.Request):
        from urllib.parse import parse_qs

        sent.update({k: v[0] for k, v in parse_qs(req.content.decode()).items()})
        return httpx.Response(200, content=_json.dumps({"Call": {"Sid": "exo-sid-123", "Status": "in-progress"}}))

    real = httpx.AsyncClient
    monkeypatch.setattr(messaging.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler)))
    for k, v in {
        "EXOTEL_API_KEY": "k",
        "EXOTEL_API_TOKEN": "t",
        "EXOTEL_ACCOUNT_SID": "acct",
        "EXOTEL_CALLER_ID": "09513886363",
        "EXOTEL_FLOW_URL": "http://my.exotel.com/acct/exoml/start_voice/1",
    }.items():
        monkeypatch.setattr(settings, k, v)
    long_msg = "Reminder: your case OS 12 of 2024 is listed tomorrow at 10:30 AM before the Principal Junior Civil Judge. " * 3
    res = await ExotelVoiceProvider().place_call("+919030158713", long_msg, "en", "job-1")
    assert res.call_id == "exo-sid-123"
    assert sent["CustomField"].startswith("vm:") and len(sent["CustomField"]) <= 128
    assert sent["From"] == "+919030158713" and sent["CallerId"] == "09513886363"

    tok = settings.WEBHOOK_SECRET
    r = await client.get("/api/v1/webhooks/exotel/greeting", params={"token": tok, "CustomField": sent["CustomField"]})
    assert r.status_code == 200 and r.text == long_msg
    r = await client.get("/api/v1/webhooks/exotel/greeting", params={"token": tok, "CallSid": "exo-sid-123"})
    assert r.text == long_msg
    r = await client.get("/api/v1/webhooks/exotel/greeting", params={"token": tok, "CustomField": "vm:not-a-uuid"})
    assert "reminder from Court Life" in r.text
