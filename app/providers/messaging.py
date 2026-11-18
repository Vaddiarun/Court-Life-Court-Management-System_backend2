"""Outbound channels: voice calls, SMS and email. Each has a mock (dev/test) and a real adapter."""

import asyncio
import smtplib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr
from xml.sax.saxutils import escape

import httpx

from app.core.config import settings


class ChannelError(Exception):
    pass


@dataclass
class CallResult:
    call_id: str
    status: str  # queued | failed


# ---------------------------------------------------------------- voice
class VoiceProvider(ABC):
    name = "base"

    def supports_language(self, language: str) -> bool:
        return True

    @abstractmethod
    async def place_call(self, to_phone: str, message: str, language: str, reference: str) -> CallResult: ...


class MockVoiceProvider(VoiceProvider):
    name = "mock"
    sent: list[dict] = []
    fail_numbers: set[str] = set()

    async def place_call(self, to_phone, message, language, reference):
        if to_phone in self.fail_numbers:
            raise ChannelError("simulated call failure")
        call_id = f"mockcall-{len(self.sent) + 1}"
        self.sent.append({"to": to_phone, "message": message, "language": language, "ref": reference, "id": call_id})
        return CallResult(call_id, "queued")


# Twilio <Say> voices per app language. Languages not listed here are spoken in English
# (the reminder text is rendered in English for them so voice and words always match).
TWILIO_VOICES: dict[str, tuple[str, str]] = {
    "en": ("Polly.Aditi", "en-IN"),
    "hi": ("Google.hi-IN-Standard-E", "hi-IN"),
    "te": ("Google.te-IN-Standard-C", "te-IN"),
    "ta": ("Google.ta-IN-Standard-A", "ta-IN"),
}


class TwilioVoiceProvider(VoiceProvider):
    """Twilio Programmable Voice: places a call that reads the reminder aloud (twice) with <Say>.

    Needs TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER. Call status is posted to
    /api/v1/webhooks/twilio/call-status when PUBLIC_BASE_URL is set (needed for no-answer retries).
    """

    name = "twilio"

    def __init__(self, client: httpx.AsyncClient | None = None):
        if not (settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN and settings.TWILIO_FROM_NUMBER) and client is None:
            raise ChannelError("Twilio is not configured: set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER")
        self._client = client

    @staticmethod
    def voice_for(language: str) -> tuple[str, str] | None:
        override = settings.TWILIO_VOICE_MAP.get(language)
        if override:
            return override, TWILIO_VOICES.get(language, (None, f"{language}-IN"))[1]
        return TWILIO_VOICES.get(language)

    def supports_language(self, language: str) -> bool:
        return self.voice_for(language) is not None

    @classmethod
    def twiml(cls, message: str, language: str) -> str:
        voice, locale = cls.voice_for(language) or TWILIO_VOICES["en"]
        say = f'<Say voice="{voice}" language="{locale}">{escape(message)}</Say>'
        return f'<Response><Pause length="1"/>{say}<Pause length="1"/>{say}</Response>'

    async def place_call(self, to_phone, message, language, reference):
        url = f"https://api.twilio.com/2010-04-01/Accounts/{settings.TWILIO_ACCOUNT_SID}/Calls.json"
        data = {"To": to_phone, "From": settings.TWILIO_FROM_NUMBER, "Twiml": self.twiml(message, language)}
        if settings.PUBLIC_BASE_URL:
            data["StatusCallback"] = (
                f"{settings.PUBLIC_BASE_URL.rstrip('/')}/api/v1/webhooks/twilio/call-status?token={settings.WEBHOOK_SECRET}"
            )
            data["StatusCallbackMethod"] = "POST"
        auth = (settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        try:
            if self._client is not None:
                resp = await self._client.post(url, data=data, auth=auth)
            else:
                async with httpx.AsyncClient(timeout=20) as client:
                    resp = await client.post(url, data=data, auth=auth)
        except httpx.HTTPError as e:
            raise ChannelError(f"Twilio unreachable: {e}") from e
        if resp.status_code >= 400:
            try:
                err = resp.json()
                detail = f"{err.get('code')}: {err.get('message')}"
            except ValueError:
                detail = resp.text[:200]
            raise ChannelError(f"Twilio {resp.status_code} — {detail}")
        return CallResult(resp.json().get("sid", ""), "queued")


class ExotelVoiceProvider(VoiceProvider):
    """Exotel Calls/connect: connects the number to an Exotel flow (app) that plays the reminder.

    The reminder text and language are passed in CustomField; the flow should use a
    'Greeting' applet with dynamic TTS or pick a prerecorded audio per language.
    Call status arrives at /api/v1/webhooks/exotel/call-status.
    """

    name = "exotel"

    async def place_call(self, to_phone, message, language, reference):
        from app.core.db import SessionLocal
        from app.models import VoiceMessage

        # Exotel limits CustomField (128 chars), so the script is stored and only its id is sent;
        # the Greeting applet's URL (/webhooks/exotel/greeting) reads it back during the call.
        async with SessionLocal() as s:
            vm = VoiceMessage(language=language or "en", text=message)
            s.add(vm)
            await s.commit()
            vm_id = vm.id

        url = (
            f"https://{settings.EXOTEL_API_KEY}:{settings.EXOTEL_API_TOKEN}@{settings.EXOTEL_SUBDOMAIN}"
            f"/v1/Accounts/{settings.EXOTEL_ACCOUNT_SID}/Calls/connect.json"
        )
        data = {
            "From": to_phone,
            "CallerId": settings.EXOTEL_CALLER_ID,
            "Url": settings.EXOTEL_FLOW_URL,
            "CallType": "trans",
            "CustomField": f"vm:{vm_id}",
        }
        if settings.PUBLIC_BASE_URL:
            data["StatusCallback"] = (
                f"{settings.PUBLIC_BASE_URL.rstrip('/')}/api/v1/webhooks/exotel/call-status?token={settings.WEBHOOK_SECRET}"
            )
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, data=data)
        if resp.status_code >= 400:
            raise ChannelError(f"Exotel {resp.status_code}: {resp.text[:300]}")
        sid = resp.json().get("Call", {}).get("Sid", "")
        if sid:
            async with SessionLocal() as s:
                row = await s.get(VoiceMessage, vm_id)
                if row:
                    row.call_sid = sid
                    await s.commit()
        return CallResult(sid, "queued")


# ---------------------------------------------------------------- sms
class SmsProvider(ABC):
    """`template` names the DLT template kind (otp / hearing / reminder) and `variables`
    fills its {#var#} slots in order. Providers that send free text use `body`."""

    name = "base"

    @abstractmethod
    async def send(self, to_phone: str, body: str, template: str | None = None, variables: list[str] | None = None) -> str: ...


class MockSmsProvider(SmsProvider):
    name = "mock"
    sent: list[dict] = []

    async def send(self, to_phone, body, template=None, variables=None):
        self.sent.append({"to": to_phone, "body": body, "template": template, "variables": variables})
        return f"mocksms-{len(self.sent)}"


DLT_VAR_MAX = 30  # TRAI: each {#var#} carries at most 30 characters


def dlt_var(value) -> str:
    text = " ".join(str(value or "").replace("|", "/").split())
    return text[:DLT_VAR_MAX]


class Fast2SmsProvider(SmsProvider):
    """Fast2SMS DLT route. The message must be an approved DLT template: we send its
    Fast2SMS Message ID plus the variable values; Fast2SMS fills the template."""

    name = "fast2sms"

    async def send(self, to_phone, body, template=None, variables=None):
        if not settings.FAST2SMS_API_KEY:
            raise ChannelError("FAST2SMS_API_KEY is not set")
        message_id = settings.FAST2SMS_TEMPLATES.get(template or "")
        if not message_id:
            raise ChannelError(f"No DLT template configured for '{template}' (set FAST2SMS_TEMPLATES)")
        digits = "".join(ch for ch in to_phone if ch.isdigit())
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        if len(digits) != 10:
            raise ChannelError("Fast2SMS sends to Indian mobile numbers only")
        payload = {
            "route": "dlt",
            "sender_id": settings.SMS_SENDER_ID,
            "message": message_id,
            "variables_values": "|".join(dlt_var(v) for v in (variables or [])),
            "numbers": digits,
            "flash": "0",
        }
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(settings.FAST2SMS_BASE_URL, json=payload, headers={"authorization": settings.FAST2SMS_API_KEY})
        except httpx.HTTPError as e:
            raise ChannelError(f"Fast2SMS unreachable: {e}") from e
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400 or not data.get("return"):
            msg = data.get("message") if isinstance(data, dict) else None
            raise ChannelError(f"Fast2SMS {resp.status_code}: {msg or resp.text[:200]}")
        return str(data.get("request_id", ""))


class ExotelSmsProvider(SmsProvider):
    """Indian SMS requires TRAI DLT registration: entity id + approved template id.
    EXOTEL needs the exact approved text in Body; template ids come from FAST2SMS_TEMPLATES-style map."""

    name = "exotel"

    async def send(self, to_phone, body, template=None, variables=None):
        url = (
            f"https://{settings.EXOTEL_API_KEY}:{settings.EXOTEL_API_TOKEN}@{settings.EXOTEL_SUBDOMAIN}"
            f"/v1/Accounts/{settings.EXOTEL_ACCOUNT_SID}/Sms/send.json"
        )
        data = {"From": settings.SMS_SENDER_ID, "To": to_phone, "Body": body, "DltEntityId": settings.SMS_DLT_ENTITY_ID}
        if template and settings.FAST2SMS_TEMPLATES.get(template):
            data["DltTemplateId"] = settings.FAST2SMS_TEMPLATES[template]
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, data=data)
        if resp.status_code >= 400:
            raise ChannelError(f"Exotel SMS {resp.status_code}: {resp.text[:200]}")
        return resp.json().get("SMSMessage", {}).get("Sid", "")


# ---------------------------------------------------------------- email
class EmailProvider(ABC):
    name = "base"

    @abstractmethod
    async def send(self, to: str, subject: str, body: str) -> str: ...


class MockEmailProvider(EmailProvider):
    name = "mock"
    sent: list[dict] = []

    async def send(self, to, subject, body):
        self.sent.append({"to": to, "subject": subject, "body": body})
        return f"mockmail-{len(self.sent)}"


class SmtpEmailProvider(EmailProvider):
    name = "smtp"

    async def send(self, to, subject, body):
        if not settings.SMTP_HOST:
            raise ChannelError("SMTP_HOST is not set")
        msg = EmailMessage()
        msg["From"] = formataddr((settings.EMAIL_FROM_NAME, settings.EMAIL_FROM))
        msg["To"], msg["Subject"] = to, subject
        msg.set_content(body)

        def _send():
            if settings.SMTP_USE_SSL:
                conn = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20)
            else:
                conn = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20)
            with conn as s:
                if not settings.SMTP_USE_SSL:
                    s.starttls()
                if settings.SMTP_USER:
                    s.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                s.send_message(msg)

        try:
            await asyncio.to_thread(_send)
        except (smtplib.SMTPException, OSError) as e:
            raise ChannelError(str(e)) from e
        return "smtp"


def get_voice() -> VoiceProvider:
    if settings.VOICE_PROVIDER == "twilio":
        return TwilioVoiceProvider()
    if settings.VOICE_PROVIDER == "exotel":
        return ExotelVoiceProvider()
    return MockVoiceProvider()


def get_sms() -> SmsProvider:
    if settings.SMS_PROVIDER == "fast2sms":
        return Fast2SmsProvider()
    if settings.SMS_PROVIDER == "exotel":
        return ExotelSmsProvider()
    return MockSmsProvider()


def get_email() -> EmailProvider:
    return SmtpEmailProvider() if settings.EMAIL_PROVIDER == "smtp" else MockEmailProvider()


def reset_mocks() -> None:
    MockVoiceProvider.sent = []
    MockVoiceProvider.fail_numbers = set()
    MockSmsProvider.sent = []
    MockEmailProvider.sent = []
