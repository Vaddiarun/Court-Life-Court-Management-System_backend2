"""OTP providers.

local          - we generate the code, store its hash and send it through the SMS provider
                 (mock in development: no SMS, code shown on screen; exotel: needs your own DLT template)
messagecentral - Message Central VerifyNow generates, sends and validates the code itself.
                 Works in India without your own DLT registration (uses their approved template).
                 Docs: https://www.messagecentral.com/product/verify-now/api-india
"""

import base64
import time
from dataclasses import dataclass

import httpx

from app.core.config import settings


class OtpProviderError(Exception):
    pass


@dataclass
class SentOtp:
    reference: str | None  # provider verification id (None for local)


def split_phone(e164: str) -> tuple[str, str]:
    """'+919812345678' -> ('91', '9812345678')."""
    digits = "".join(ch for ch in e164 if ch.isdigit())
    if len(digits) <= 10:
        return "91", digits
    return digits[:-10], digits[-10:]


class MessageCentralOtp:
    name = "messagecentral"
    BASE = "https://cpaas.messagecentral.com"
    _token: str | None = None
    _token_at: float = 0.0

    def __init__(self, client: httpx.AsyncClient | None = None):
        if not (settings.MESSAGECENTRAL_CUSTOMER_ID and settings.MESSAGECENTRAL_PASSWORD) and client is None:
            raise OtpProviderError("MESSAGECENTRAL_CUSTOMER_ID / MESSAGECENTRAL_PASSWORD are not configured")
        self._client = client

    async def _request(self, method: str, path: str, **kw) -> dict:
        if self._client is not None:
            resp = await self._client.request(method, self.BASE + path, **kw)
        else:
            async with httpx.AsyncClient(timeout=20) as c:
                resp = await c.request(method, self.BASE + path, **kw)
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text[:300]}
        if resp.status_code >= 400:
            raise OtpProviderError(f"Message Central {resp.status_code}: {body}")
        return body

    async def _auth_token(self) -> str:
        # tokens are cached for 20 minutes
        if MessageCentralOtp._token and time.time() - MessageCentralOtp._token_at < 1200:
            return MessageCentralOtp._token
        key = base64.b64encode(settings.MESSAGECENTRAL_PASSWORD.encode()).decode()
        params = {
            "customerId": settings.MESSAGECENTRAL_CUSTOMER_ID,
            "key": key,
            "scope": "NEW",
            "country": "91",
        }
        if settings.MESSAGECENTRAL_EMAIL:
            params["email"] = settings.MESSAGECENTRAL_EMAIL
        body = await self._request("GET", "/auth/v1/authentication/token", params=params)
        token = body.get("token") or (body.get("data") or {}).get("token")
        if not token:
            raise OtpProviderError(f"Message Central did not return a token: {body}")
        MessageCentralOtp._token, MessageCentralOtp._token_at = token, time.time()
        return token

    async def send(self, phone: str) -> SentOtp:
        cc, mobile = split_phone(phone)
        params = {
            "countryCode": cc,
            "customerId": settings.MESSAGECENTRAL_CUSTOMER_ID,
            "flowType": "SMS",
            "mobileNumber": mobile,
            "otpLength": 6,
        }
        body = await self._request("POST", "/verification/v3/send", params=params, headers={"authToken": await self._auth_token()})
        vid = (body.get("data") or {}).get("verificationId")
        if not vid:
            raise OtpProviderError(f"Message Central send failed: {body.get('message') or body}")
        return SentOtp(reference=str(vid))

    async def verify(self, phone: str, code: str, reference: str) -> bool:
        cc, mobile = split_phone(phone)
        params = {
            "countryCode": cc,
            "mobileNumber": mobile,
            "verificationId": reference,
            "customerId": settings.MESSAGECENTRAL_CUSTOMER_ID,
            "code": code,
        }
        try:
            body = await self._request(
                "GET", "/verification/v3/validateOtp", params=params, headers={"authToken": await self._auth_token()}
            )
        except OtpProviderError:
            return False  # wrong / expired code comes back as 4xx
        status = str((body.get("data") or {}).get("verificationStatus", "")).upper()
        return status == "VERIFICATION_COMPLETED" or (not status and body.get("responseCode") == 200)


def get_remote_otp_provider():
    """Returns a provider that sends+validates codes itself, or None for the local flow."""
    if settings.OTP_PROVIDER == "messagecentral":
        return MessageCentralOtp()
    return None
