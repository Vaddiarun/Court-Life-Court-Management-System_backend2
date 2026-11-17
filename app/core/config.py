from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    APP_NAME: str = "Court Life - Court Management System"
    ENVIRONMENT: str = "development"
    API_V1_PREFIX: str = "/api/v1"
    # JSON list or comma-separated: https://app.example.com,http://localhost:3000
    CORS_ORIGINS: Annotated[list[str], NoDecode] = ["http://localhost:3000"]

    # Database / cache
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/cms_dev"
    REDIS_URL: str = "redis://localhost:6379/0"

    # Auth
    JWT_SECRET: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    BCRYPT_ROUNDS: int = 12
    ACCESS_TOKEN_MINUTES: int = 60
    REFRESH_TOKEN_DAYS: int = 14
    OTP_TTL_MINUTES: int = 10
    OTP_MAX_ATTEMPTS: int = 5
    # Sign-in: mobile + OTP is always on; password sign-in is off unless enabled
    PASSWORD_LOGIN_ENABLED: bool = False
    # Public "register your law firm" sign-up (creates a tenant + its first firm admin)
    ALLOW_FIRM_SELF_REGISTRATION: bool = True
    SELF_REGISTRATION_PLAN: str = "small_firm"

    # OTP delivery: "local" (our code via SMS_PROVIDER) | "messagecentral" (VerifyNow, no DLT needed)
    OTP_PROVIDER: str = "local"
    MESSAGECENTRAL_CUSTOMER_ID: str = ""
    MESSAGECENTRAL_PASSWORD: str = ""  # your Message Central account password (base64-encoded for you)
    MESSAGECENTRAL_EMAIL: str = ""

    @property
    def otp_dev_echo(self) -> bool:
        """Local OTP + mock SMS outside production: return/log the code so it can be used."""
        return self.OTP_PROVIDER == "local" and self.SMS_PROVIDER == "mock" and self.ENVIRONMENT != "production"

    # Court data provider: "mock" | "ecourtsindia"
    COURT_PROVIDER: str = "mock"
    ECOURTSINDIA_BASE_URL: str = "https://webapi.ecourtsindia.com"
    ECOURTSINDIA_API_KEY: str = ""
    COURT_PROVIDER_TIMEOUT_SECONDS: float = 20.0
    # CNR not yet in the provider's index: ask it to fetch live from eCourts, then poll this long
    LOOKUP_REFRESH_WAIT_SECONDS: float = 24.0
    LOOKUP_REFRESH_POLL_SECONDS: float = 6.0

    # Indicative Enterprise rate card (INR per request) - PRD section 5
    RATE_CASE_SEARCH: float = 0.20
    RATE_CASE_DETAIL: float = 0.50
    RATE_CASE_REFRESH: float = 0.05
    RATE_ORDER_PDF: float = 1.25
    RATE_CALL_MINUTE: float = 1.00

    # Sync behaviour
    SYNC_NOW_COOLDOWN_MINUTES: int = 15
    SYNC_BATCH_SIZE: int = 100
    # eCourtsIndia refresh is asynchronous: request it, then read detail after this delay
    SYNC_REFRESH_SETTLE_MINUTES: int = 10

    # Reminders
    TIMEZONE: str = "Asia/Kolkata"
    REMINDER_SEND_HOUR: int = 8  # local hour for "N days before" reminders
    CALL_WINDOW_START_HOUR: int = 9  # calls only between 09:00 and 21:00 local
    CALL_WINDOW_END_HOUR: int = 21
    AUTO_REMINDER_OFFSETS: list[int] = [3, 1, 0]

    # Voice: "mock" | "twilio" | "exotel"
    VOICE_PROVIDER: str = "mock"
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_FROM_NUMBER: str = ""  # your Twilio voice number, E.164 e.g. +12025550123
    TWILIO_VOICE_MAP: dict[str, str] = {}  # optional overrides, e.g. {"ta": "Google.ta-IN-Standard-B"}
    # Public https URL of this API (e.g. an ngrok / cloudflared tunnel) so providers can post call status back.
    PUBLIC_BASE_URL: str = ""
    EXOTEL_SUBDOMAIN: str = "api.exotel.com"
    EXOTEL_ACCOUNT_SID: str = ""
    EXOTEL_API_KEY: str = ""
    EXOTEL_API_TOKEN: str = ""
    EXOTEL_CALLER_ID: str = ""
    EXOTEL_FLOW_URL: str = ""  # Exotel app/flow that plays the reminder
    CALL_MAX_ATTEMPTS: int = 3
    CALL_RETRY_MINUTES: int = 10

    # SMS: "mock" | "fast2sms" | "exotel"   (real SMS in India needs TRAI DLT registration)
    SMS_PROVIDER: str = "mock"
    SMS_SENDER_ID: str = ""  # DLT header, e.g. GSKCPL
    SMS_DLT_ENTITY_ID: str = ""
    # Fast2SMS DLT route: https://www.fast2sms.com/dev/bulkV2
    FAST2SMS_API_KEY: str = ""
    FAST2SMS_BASE_URL: str = "https://www.fast2sms.com/dev/bulkV2"
    # Message kind -> Fast2SMS "Message ID" of the DLT-approved template, e.g.
    # {"otp": "171234", "hearing": "171235", "reminder": "171236"}
    FAST2SMS_TEMPLATES: dict[str, str] = {}

    # Email: "mock" | "smtp"
    EMAIL_PROVIDER: str = "mock"
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_USE_SSL: bool = False  # True for port 465; False = STARTTLS on 587
    EMAIL_FROM: str = "no-reply@courtlife.local"
    EMAIL_FROM_NAME: str = "Court Life"

    # Storage: "local" | "gcs" | "s3"
    STORAGE_BACKEND: str = "local"
    GCS_BUCKET: str = ""
    LOCAL_STORAGE_DIR: str = "./storage"
    S3_BUCKET: str = ""
    S3_REGION: str = "ap-south-1"
    MAX_UPLOAD_MB: int = 25

    # Webhooks
    WEBHOOK_SECRET: str = "change-me-webhook-secret"

    # Background jobs without Celery/Redis: run reminder dispatch + court sync inside the API process
    RUN_SCHEDULER_IN_PROCESS: bool = True
    SCHEDULER_INTERVAL_SECONDS: int = 30
    SYNC_INTERVAL_MINUTES: int = 15

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, v):
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                import json

                return json.loads(v)
            return [o.strip().rstrip("/") for o in v.split(",") if o.strip()]
        return v

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def _async_db_url(cls, v):
        """Hosts (Render, Railway, Neon) hand out postgres://... - we need the asyncpg driver."""
        if isinstance(v, str):
            for prefix in ("postgres://", "postgresql://"):
                if v.startswith(prefix):
                    v = "postgresql+asyncpg://" + v[len(prefix) :]
            # asyncpg does not understand libpq's sslmode=require; use ssl=require
            v = v.replace("sslmode=require", "ssl=require")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
