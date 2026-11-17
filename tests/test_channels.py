"""Fast2SMS DLT SMS, SMTP email, deployment-friendly settings and the channel status / test-email endpoints."""

import json
import re
from datetime import UTC, datetime

import httpx
import pytest

from app.core.config import Settings, settings
from app.models import Reminder
from app.models.enums import JobStatus
from app.providers import messaging
from app.providers.messaging import ChannelError, Fast2SmsProvider, MockEmailProvider, dlt_var
from app.services.reminders import sms_template
from tests.conftest import API, add_case


class FakeFast2Sms:
    def __init__(self, ok=True):
        self.requests: list[dict] = []
        self.ok = ok

    def __call__(self, req: httpx.Request):
        self.requests.append({"auth": req.headers.get("authorization"), "body": json.loads(req.content)})
        if not self.ok:
            return httpx.Response(400, json={"return": False, "status_code": 412, "message": "Invalid Authentication"})
        return httpx.Response(200, json={"return": True, "request_id": "REQ123", "message": ["SMS sent successfully."]})


@pytest.fixture
def fast2sms(monkeypatch):
    fake = FakeFast2Sms()
    real = httpx.AsyncClient
    monkeypatch.setattr(messaging.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(fake)))
    monkeypatch.setattr(settings, "SMS_PROVIDER", "fast2sms")
    monkeypatch.setattr(settings, "FAST2SMS_API_KEY", "KEY")
    monkeypatch.setattr(settings, "SMS_SENDER_ID", "GSKCPL")
    monkeypatch.setattr(settings, "FAST2SMS_TEMPLATES", {"otp": "1001", "hearing": "1002", "reminder": "1003"})
    return fake


def test_dlt_var_trims_and_strips_separator():
    assert dlt_var("a|b") == "a/b"
    assert len(dlt_var("x" * 80)) == 30
    assert dlt_var("  many   spaces ") == "many spaces"


async def test_fast2sms_send_uses_dlt_route(fast2sms):
    ref = await Fast2SmsProvider().send("+919876543210", "ignored", template="hearing", variables=["OS 12/2024", "20-11-2026", "10:30 AM"])
    assert ref == "REQ123"
    req = fast2sms.requests[0]
    assert req["auth"] == "KEY"
    assert req["body"] == {
        "route": "dlt",
        "sender_id": "GSKCPL",
        "message": "1002",
        "variables_values": "OS 12/2024|20-11-2026|10:30 AM",
        "numbers": "9876543210",
        "flash": "0",
    }


async def test_fast2sms_errors(fast2sms, monkeypatch):
    with pytest.raises(ChannelError, match="No DLT template"):
        await Fast2SmsProvider().send("+919876543210", "x", template="unknown")
    with pytest.raises(ChannelError, match="Indian mobile"):
        await Fast2SmsProvider().send("+14155550123", "x", template="otp", variables=["1"])
    fast2sms.ok = False
    with pytest.raises(ChannelError, match="Invalid Authentication"):
        await Fast2SmsProvider().send("+919876543210", "x", template="otp", variables=["123456"])
    monkeypatch.setattr(settings, "FAST2SMS_API_KEY", "")
    with pytest.raises(ChannelError, match="API_KEY"):
        await Fast2SmsProvider().send("+919876543210", "x", template="otp", variables=["1"])


async def test_local_otp_goes_through_fast2sms_template(client, firm, fast2sms):
    phone = firm.client2["phone"]
    r = await client.post(f"{API}/auth/otp/request", json={"phone": phone})
    assert r.status_code == 202
    assert "dev_code" not in r.json() or r.json()["dev_code"] is None  # real SMS -> no dev echo
    body = fast2sms.requests[-1]["body"]
    assert body["message"] == "1001" and re.fullmatch(r"\d{6}", body["variables_values"])
    r = await client.post(f"{API}/auth/otp/verify", json={"phone": phone, "code": body["variables_values"]})
    assert r.status_code == 200


def test_sms_template_choice():
    ev = datetime(2026, 11, 20, 5, 0, tzinfo=UTC)  # 10:30 IST

    class C:
        case_number, cnr, title = "OS 12/2024", "DLHC010001232024", "A vs B"

    hearing = Reminder(reminder_type="hearing", title="Hearing: A vs B", event_at=ev)
    assert sms_template(hearing, C()) == ("hearing", ["OS 12/2024", "20-11-2026", "10:30 AM"])
    other = Reminder(reminder_type="deadline", title="File reply", event_at=ev)
    assert sms_template(other, None) == ("reminder", ["File reply", "20-11-2026", "10:30 AM"])


async def test_reminder_sms_dispatch_uses_template(client, firm, db, fast2sms):
    from sqlalchemy import select

    from app.models import NotificationJob, User
    from app.services.reminders import dispatch_due

    case = await add_case(client, firm)
    me = (await client.get(f"{API}/auth/me", headers=firm.h("lawyer"))).json()
    await client.patch(f"{API}/auth/me", headers=firm.h("lawyer"), json={"notify_sms": True, "sms_consent": True})
    user = await db.get(User, __import__("uuid").UUID(me["id"]))
    if user.sms_consent_at is None:  # consent field name differs across schema versions
        user.sms_consent_at = datetime.now(UTC)
        user.notify_sms = True
        await db.commit()
    r = await client.post(
        f"{API}/reminders",
        headers=firm.h("lawyer"),
        json={
            "case_id": case["id"],
            "reminder_type": "hearing",
            "title": "Hearing",
            "event_at": "2030-01-10T05:00:00Z",
            "offsets_days": [],
            "custom_times": ["2020-01-01T00:00:00Z"],
            "channels": ["sms"],
            "recipient_ids": [me["id"]],
        },
    )
    assert r.status_code in (200, 201), r.text
    # custom time in the past is skipped at build time -> schedule a job now
    rid = r.json()["id"]
    db.add(
        NotificationJob(
            tenant_id=user.tenant_id,
            reminder_id=__import__("uuid").UUID(rid),
            user_id=user.id,
            channel="sms",
            scheduled_at=datetime.now(UTC),
        )
    )
    await db.commit()
    await dispatch_due(db)
    await db.commit()
    job = (await db.execute(select(NotificationJob).where(NotificationJob.reminder_id == __import__("uuid").UUID(rid)))).scalars().first()
    assert job.status == JobStatus.SENT, job.last_error
    body = fast2sms.requests[-1]["body"]
    assert body["message"] == "1002"
    assert body["variables_values"].endswith("|10-01-2030|10:30 AM")


async def test_test_email_and_channels(client, firm):
    r = await client.post(f"{API}/auth/me/test-email", headers=firm.h("lawyer"))
    assert r.status_code == 200, r.text
    assert MockEmailProvider.sent[-1]["subject"] == "Court Life: test email"
    ch = (await client.get(f"{API}/auth/me/channels", headers=firm.h("lawyer"))).json()
    assert ch["email"] == "mock" and "voice" in ch and "sms" in ch


def test_settings_accept_host_style_values(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@host:5432/db?sslmode=require")
    monkeypatch.setenv("CORS_ORIGINS", "https://courtlife.vercel.app/, http://localhost:3000")
    s = Settings(_env_file=None)
    assert s.DATABASE_URL == "postgresql+asyncpg://u:p@host:5432/db?ssl=require"
    assert s.CORS_ORIGINS == ["https://courtlife.vercel.app", "http://localhost:3000"]
    monkeypatch.setenv("CORS_ORIGINS", '["https://a.example"]')
    assert Settings(_env_file=None).CORS_ORIGINS == ["https://a.example"]


async def test_gcs_storage_roundtrip(monkeypatch):
    from app.providers import storage as st

    class Blob:
        def __init__(self, store, key):
            self.store, self.key = store, key

        def upload_from_string(self, data, content_type=None):
            self.store[self.key] = (data, content_type)

        def download_as_bytes(self):
            return self.store[self.key][0]

        def delete(self):
            from google.api_core.exceptions import NotFound

            if self.key not in self.store:
                raise NotFound("gone")
            del self.store[self.key]

    class Bucket:
        def __init__(self):
            self.store = {}

        def blob(self, key):
            return Blob(self.store, key)

    class Client:
        def __init__(self):
            self.b = Bucket()

        def bucket(self, name):
            assert name == "courtlife-docs"
            return self.b

    monkeypatch.setattr(settings, "STORAGE_BACKEND", "gcs")
    monkeypatch.setattr(settings, "GCS_BUCKET", "courtlife-docs")
    monkeypatch.setattr(st.GcsStorage, "_client", Client())
    s = st.get_storage()
    assert isinstance(s, st.GcsStorage)
    await s.put("t/1/a.pdf", b"%PDF", "application/pdf")
    assert await s.get("t/1/a.pdf") == b"%PDF"
    await s.delete("t/1/a.pdf")
    await s.delete("t/1/a.pdf")  # deleting twice is fine
    monkeypatch.setattr(st.GcsStorage, "_client", None)


def test_cloud_sql_socket_url_is_accepted(monkeypatch):
    from sqlalchemy.engine import make_url

    monkeypatch.setenv("DATABASE_URL", "postgresql://courtlife:pw@/courtlife?host=/cloudsql/proj:asia-south1:courtlife-db")
    s = Settings(_env_file=None)
    url = make_url(s.DATABASE_URL)
    assert url.drivername == "postgresql+asyncpg"
    assert url.query["host"] == "/cloudsql/proj:asia-south1:courtlife-db" and url.database == "courtlife"
