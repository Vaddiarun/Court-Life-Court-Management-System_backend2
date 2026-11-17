"""Message Central VerifyNow OTP provider (HTTP mocked) and the end-to-end OTP flow using it."""

import httpx
import pytest

from app.core.config import settings
from app.providers import otp as otp_mod
from app.providers.otp import MessageCentralOtp, OtpProviderError, split_phone
from tests.conftest import API


def test_split_phone():
    assert split_phone("+919812345678") == ("91", "9812345678")
    assert split_phone("+14155550123") == ("1", "4155550123")
    assert split_phone("9812345678") == ("91", "9812345678")


class FakeMC:
    """Simulates cpaas.messagecentral.com: token, send, validateOtp."""

    def __init__(self):
        self.calls = []
        self.code = "482913"

    def __call__(self, req: httpx.Request):
        p, q = req.url.path, dict(req.url.params)
        self.calls.append((req.method, p, q, req.headers.get("authToken")))
        if p == "/auth/v1/authentication/token":
            return httpx.Response(200, json={"status": 200, "token": "TOKEN123"})
        if p == "/verification/v3/send":
            assert req.headers["authToken"] == "TOKEN123"
            return httpx.Response(
                200, json={"responseCode": 200, "message": "SUCCESS", "data": {"verificationId": "7001", "mobileNumber": q["mobileNumber"]}}
            )
        if p == "/verification/v3/validateOtp":
            if q.get("code") == self.code and q.get("verificationId") == "7001":
                return httpx.Response(
                    200, json={"responseCode": 200, "message": "SUCCESS", "data": {"verificationStatus": "VERIFICATION_COMPLETED"}}
                )
            return httpx.Response(400, json={"responseCode": 702, "message": "WRONG_OTP_PROVIDED"})
        return httpx.Response(404)


@pytest.fixture
def fake_mc(monkeypatch):
    fake = FakeMC()
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    MessageCentralOtp._token = None
    monkeypatch.setattr(settings, "OTP_PROVIDER", "messagecentral")
    monkeypatch.setattr(settings, "MESSAGECENTRAL_CUSTOMER_ID", "C-TEST")
    monkeypatch.setattr(settings, "MESSAGECENTRAL_PASSWORD", "secret")
    monkeypatch.setattr(otp_mod, "get_remote_otp_provider", lambda: MessageCentralOtp(client=client))
    from app.api.v1 import auth as auth_mod

    monkeypatch.setattr(auth_mod, "get_remote_otp_provider", lambda: MessageCentralOtp(client=client))
    yield fake
    MessageCentralOtp._token = None


async def test_provider_send_and_verify(fake_mc):
    p = MessageCentralOtp(client=httpx.AsyncClient(transport=httpx.MockTransport(fake_mc)))
    sent = await p.send("+919822222222")
    assert sent.reference == "7001"
    token_call = fake_mc.calls[0]
    assert token_call[2]["customerId"] == "C-TEST" and token_call[2]["key"] == "c2VjcmV0"  # base64("secret")
    send_call = fake_mc.calls[1]
    assert send_call[2]["countryCode"] == "91" and send_call[2]["mobileNumber"] == "9822222222" and send_call[2]["flowType"] == "SMS"
    assert await p.verify("+919822222222", "482913", "7001") is True
    assert await p.verify("+919822222222", "000000", "7001") is False
    # token is cached: only one token request
    assert sum(1 for c in fake_mc.calls if c[1] == "/auth/v1/authentication/token") == 1


async def test_provider_requires_credentials(monkeypatch):
    monkeypatch.setattr(settings, "MESSAGECENTRAL_CUSTOMER_ID", "")
    with pytest.raises(OtpProviderError):
        MessageCentralOtp()


async def test_otp_login_through_message_central(client, firm, fake_mc):
    local = firm.client2["phone"][-10:]
    r = await client.post(f"{API}/auth/otp/request", json={"phone": local})
    assert r.status_code == 202
    assert "dev_code" not in r.json()  # real provider: the code is never exposed
    bad = await client.post(f"{API}/auth/otp/verify", json={"phone": local, "code": "111111"})
    assert bad.status_code == 401
    ok = await client.post(f"{API}/auth/otp/verify", json={"phone": local, "code": "482913"})
    assert ok.status_code == 200 and ok.json()["user"]["role"] == "client"
    # single use
    again = await client.post(f"{API}/auth/otp/verify", json={"phone": local, "code": "482913"})
    assert again.status_code == 401


async def test_provider_send_failure_is_502(client, firm, monkeypatch):
    def broken(req):
        return httpx.Response(500, json={"message": "down"})

    from app.api.v1 import auth as auth_mod

    monkeypatch.setattr(settings, "OTP_PROVIDER", "messagecentral")
    monkeypatch.setattr(
        auth_mod, "get_remote_otp_provider", lambda: MessageCentralOtp(client=httpx.AsyncClient(transport=httpx.MockTransport(broken)))
    )
    MessageCentralOtp._token = None
    r = await client.post(f"{API}/auth/otp/request", json={"phone": firm.client2["phone"]})
    assert r.status_code == 502
