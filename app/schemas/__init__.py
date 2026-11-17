import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.i18n import LANGUAGE_CODES
from app.models.enums import (
    Channel,
    DocumentCategory,
    NoteVisibility,
    Plan,
    ReminderType,
    Role,
    TaskStatus,
    TenantStatus,
)

PHONE_PATTERN = r"^\+?[1-9]\d{9,14}$"


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


def normalize_phone(v):
    """Accept 9812345678, 09812345678, 919812345678, +91 98123-45678 ... -> +919812345678."""
    if v is None or not isinstance(v, str):
        return v
    raw = v.strip()
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    if raw.startswith("+"):
        return "+" + digits
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10:
        return "+91" + digits
    if len(digits) == 12 and digits.startswith("91"):
        return "+" + digits
    return "+" + digits if digits else raw


def _phone(v):
    return normalize_phone(v)


def _lang(v: str | None) -> str | None:
    if v is not None and v not in LANGUAGE_CODES:
        raise ValueError(f"unsupported language '{v}'")
    return v


# ------------------------------------------------------------------ auth
class LoginIn(BaseModel):
    email: EmailStr
    password: str


class FirmRegisterIn(BaseModel):
    firm_name: str = Field(min_length=2, max_length=200)
    full_name: str = Field(min_length=2, max_length=200)
    phone: str = Field(pattern=PHONE_PATTERN)
    code: str = Field(min_length=4, max_length=8)
    email: EmailStr | None = None
    default_language: str = "en"
    bar_council_id: str | None = None

    _v = field_validator("default_language")(_lang)

    _p = field_validator("phone", mode="before")(_phone)


class OtpRequestIn(BaseModel):
    phone: str = Field(pattern=PHONE_PATTERN)
    purpose: Literal["login", "register"] = "login"

    _p = field_validator("phone", mode="before")(_phone)


class OtpVerifyIn(BaseModel):
    phone: str = Field(pattern=PHONE_PATTERN)
    code: str = Field(min_length=6, max_length=6)

    _p = field_validator("phone", mode="before")(_phone)


class RefreshIn(BaseModel):
    refresh_token: str


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: "UserOut"


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


# ------------------------------------------------------------------ tenants
class TenantIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    slug: str = Field(pattern=r"^[a-z0-9-]{3,80}$")
    plan: Plan = Plan.SMALL_FIRM
    default_language: str = "en"
    contact_email: EmailStr | None = None
    contact_phone: str | None = None
    address: str | None = None
    monthly_api_allowance: int = Field(5000, ge=0)
    monthly_call_allowance: int = Field(500, ge=0)
    subscription_ends_at: date | None = None
    admin_name: str
    admin_phone: str | None = Field(None, pattern=PHONE_PATTERN)
    admin_email: EmailStr | None = None
    admin_password: str | None = Field(None, min_length=8)

    _v = field_validator("default_language")(_lang)
    _ap = field_validator("admin_phone", mode="before")(_phone)

    @model_validator(mode="after")
    def _admin_contact(self):
        if not self.admin_phone and not (self.admin_email and self.admin_password):
            raise ValueError("admin_phone is required (users sign in with mobile OTP)")
        return self


class TenantUpdate(BaseModel):
    name: str | None = None
    plan: Plan | None = None
    status: TenantStatus | None = None
    default_language: str | None = None
    contact_email: EmailStr | None = None
    contact_phone: str | None = None
    address: str | None = None
    monthly_api_allowance: int | None = Field(None, ge=0)
    monthly_call_allowance: int | None = Field(None, ge=0)
    subscription_ends_at: date | None = None
    settings: dict | None = None

    _v = field_validator("default_language")(_lang)


class FirmUpdate(BaseModel):
    """Fields a Law Firm Admin can change on their own firm."""

    name: str | None = None
    default_language: str | None = None
    contact_email: EmailStr | None = None
    contact_phone: str | None = None
    address: str | None = None
    settings: dict | None = None

    _v = field_validator("default_language")(_lang)


class TenantOut(ORM):
    id: uuid.UUID
    name: str
    slug: str
    plan: str
    status: str
    default_language: str
    contact_email: str | None
    contact_phone: str | None
    address: str | None
    monthly_api_allowance: int
    monthly_call_allowance: int
    subscription_ends_at: date | None
    settings: dict
    created_at: datetime


# ------------------------------------------------------------------ users
class UserCreate(BaseModel):
    full_name: str = Field(min_length=2, max_length=200)
    email: EmailStr | None = None
    phone: str | None = Field(None, pattern=PHONE_PATTERN)
    password: str | None = Field(None, min_length=8)
    role: Literal["firm_admin", "lawyer", "junior_advocate", "client"]
    language: str = "en"
    bar_council_id: str | None = None

    _v = field_validator("language")(_lang)

    @model_validator(mode="after")
    def _contact(self):
        if not self.phone:
            raise ValueError("phone is required — users sign in with mobile number + OTP")
        return self

    _p = field_validator("phone", mode="before")(_phone)


class UserUpdate(BaseModel):
    full_name: str | None = None
    phone: str | None = Field(None, pattern=PHONE_PATTERN)
    role: Literal["firm_admin", "lawyer", "junior_advocate", "client"] | None = None
    language: str | None = None
    is_active: bool | None = None
    bar_council_id: str | None = None

    _v = field_validator("language")(_lang)

    _p = field_validator("phone", mode="before")(_phone)


class PreferencesUpdate(BaseModel):
    full_name: str | None = None
    phone: str | None = Field(None, pattern=PHONE_PATTERN)
    language: str | None = None
    notify_in_app: bool | None = None
    notify_email: bool | None = None
    notify_sms: bool | None = None
    notify_call: bool | None = None
    sms_consent: bool | None = None
    call_consent: bool | None = None

    _v = field_validator("language")(_lang)

    _p = field_validator("phone", mode="before")(_phone)


class UserOut(ORM):
    id: uuid.UUID
    tenant_id: uuid.UUID | None
    email: str | None
    phone: str | None
    full_name: str
    role: str
    language: str
    is_active: bool
    bar_council_id: str | None
    notify_in_app: bool
    notify_email: bool
    notify_sms: bool
    notify_call: bool
    call_consent_at: datetime | None
    sms_consent_at: datetime | None
    created_at: datetime


# ------------------------------------------------------------------ cases
class CnrLookupIn(BaseModel):
    cnr: str


class CaseFromCnrIn(BaseModel):
    cnr: str
    lead_lawyer_id: uuid.UUID | None = None
    client_ids: list[uuid.UUID] = []
    sync_enabled: bool = True


class CaseManualIn(BaseModel):
    title: str = Field(min_length=3, max_length=500)
    cnr: str | None = None
    case_number: str | None = None
    case_type: str | None = None
    case_year: int | None = Field(None, ge=1900, le=2100)
    court_name: str | None = None
    court_level: str | None = None
    state: str | None = None
    district: str | None = None
    judge: str | None = None
    petitioners: list[str] = []
    respondents: list[str] = []
    advocates: list[str] = []
    filing_date: date | None = None
    registration_date: date | None = None
    status: str | None = None
    stage: str | None = None
    next_hearing_date: date | None = None
    description: str | None = None
    lead_lawyer_id: uuid.UUID | None = None
    client_ids: list[uuid.UUID] = []
    sync_enabled: bool = False
    force: bool = False  # create even if a possible duplicate exists


class CaseUpdate(BaseModel):
    title: str | None = None
    case_number: str | None = None
    case_type: str | None = None
    court_name: str | None = None
    judge: str | None = None
    status: str | None = None
    stage: str | None = None
    next_hearing_date: date | None = None
    description: str | None = None
    lead_lawyer_id: uuid.UUID | None = None
    sync_enabled: bool | None = None
    is_disposed: bool | None = None


class CaseOut(ORM):
    id: uuid.UUID
    tenant_id: uuid.UUID
    cnr: str | None
    title: str
    case_number: str | None
    case_type: str | None
    case_year: int | None
    court_name: str | None
    court_level: str | None
    state: str | None
    district: str | None
    judge: str | None
    petitioners: list
    respondents: list
    advocates: list
    filing_date: date | None
    registration_date: date | None
    status: str | None
    stage: str | None
    next_hearing_date: date | None
    is_disposed: bool
    source: str
    provider_name: str | None
    last_synced_at: datetime | None
    next_sync_at: datetime | None
    last_sync_error: str | None
    sync_enabled: bool
    lead_lawyer_id: uuid.UUID | None
    description: str | None
    created_at: datetime


class CaseClientOut(ORM):
    """Restricted view for clients: no internal sync diagnostics."""

    id: uuid.UUID
    cnr: str | None
    title: str
    case_number: str | None
    court_name: str | None
    petitioners: list
    respondents: list
    status: str | None
    stage: str | None
    next_hearing_date: date | None
    is_disposed: bool
    source: str
    last_synced_at: datetime | None


class CaseLookupOut(BaseModel):
    cnr: str
    title: str
    case_number: str | None
    case_type: str | None
    court_name: str | None
    court_level: str | None
    state: str | None
    district: str | None
    judge: str | None
    petitioners: list[str]
    respondents: list[str]
    advocates: list[str]
    filing_date: date | None
    registration_date: date | None
    status: str | None
    stage: str | None
    next_hearing_date: date | None
    is_disposed: bool
    hearings: list[dict]
    orders: list[dict]
    provider: str
    already_in_workspace: uuid.UUID | None = None


class AssignIn(BaseModel):
    user_ids: list[uuid.UUID]


class CaseEventOut(ORM):
    id: uuid.UUID
    event_type: str
    field: str | None
    old_value: str | None
    new_value: str | None
    source: str
    created_at: datetime


class SyncRunOut(ORM):
    id: uuid.UUID
    case_id: uuid.UUID
    trigger: str
    status: str
    changes: int
    error: str | None
    created_at: datetime


class SearchIn(BaseModel):
    query: str | None = None
    court_code: str | None = None
    case_type: str | None = None
    year: int | None = None
    litigant: str | None = None
    page: int = 1
    page_size: int = Field(20, le=100)


# ------------------------------------------------------------------ hearings
class HearingIn(BaseModel):
    case_id: uuid.UUID
    hearing_date: date
    start_time: str | None = Field(None, pattern=r"^\d{2}:\d{2}$")
    purpose: str | None = None
    court_room: str | None = None
    judge: str | None = None
    notes: str | None = None


class HearingUpdate(BaseModel):
    hearing_date: date | None = None
    start_time: str | None = Field(None, pattern=r"^\d{2}:\d{2}$")
    purpose: str | None = None
    court_room: str | None = None
    notes: str | None = None


class HearingOut(ORM):
    id: uuid.UUID
    case_id: uuid.UUID
    hearing_date: date
    start_time: str | None
    purpose: str | None
    judge: str | None
    court_room: str | None
    business: str | None
    source: str
    notes: str | None
    case_title: str | None = None
    created_at: datetime


# ------------------------------------------------------------------ reminders
class ReminderIn(BaseModel):
    title: str = Field(min_length=2, max_length=300)
    reminder_type: ReminderType = ReminderType.CUSTOM
    message: str | None = None
    case_id: uuid.UUID | None = None
    hearing_id: uuid.UUID | None = None
    event_at: datetime | None = None
    offsets_days: list[int] = Field(default_factory=lambda: [7, 3, 1, 0])
    custom_times: list[datetime] = []
    channels: list[Channel] = [Channel.IN_APP]
    recipient_ids: list[uuid.UUID] = []
    include_case_clients: bool = False

    @field_validator("offsets_days")
    @classmethod
    def _offsets(cls, v):
        if any(o < 0 or o > 365 for o in v):
            raise ValueError("offsets must be between 0 and 365 days")
        return sorted(set(v), reverse=True)

    @model_validator(mode="after")
    def _event(self):
        if self.event_at is None and self.hearing_id is None:
            raise ValueError("event_at or hearing_id is required")
        if not self.offsets_days and not self.custom_times:
            raise ValueError("at least one offset or custom time is required")
        return self


class ReminderUpdate(BaseModel):
    title: str | None = None
    message: str | None = None
    event_at: datetime | None = None
    offsets_days: list[int] | None = None
    custom_times: list[datetime] | None = None
    channels: list[Channel] | None = None
    recipient_ids: list[uuid.UUID] | None = None
    is_active: bool | None = None


class ReminderOut(ORM):
    id: uuid.UUID
    case_id: uuid.UUID | None
    hearing_id: uuid.UUID | None
    reminder_type: str
    title: str
    message: str | None
    event_at: datetime
    offsets_days: list
    custom_times: list
    channels: list
    recipient_ids: list
    is_active: bool
    auto_generated: bool
    created_at: datetime


class JobOut(ORM):
    id: uuid.UUID
    user_id: uuid.UUID
    channel: str
    scheduled_at: datetime
    status: str
    attempts: int
    last_error: str | None
    sent_at: datetime | None


# ------------------------------------------------------------------ docs / tasks / notes
class DocumentOut(ORM):
    id: uuid.UUID
    case_id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    category: str
    description: str | None
    shared_with_client: bool
    uploaded_by_id: uuid.UUID | None
    created_at: datetime


class DocumentUpdate(BaseModel):
    category: DocumentCategory | None = None
    description: str | None = None
    shared_with_client: bool | None = None


class TaskIn(BaseModel):
    title: str = Field(min_length=2, max_length=300)
    description: str | None = None
    case_id: uuid.UUID | None = None
    assigned_to_id: uuid.UUID | None = None
    due_at: datetime | None = None


class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    assigned_to_id: uuid.UUID | None = None
    due_at: datetime | None = None
    status: TaskStatus | None = None


class TaskOut(ORM):
    id: uuid.UUID
    case_id: uuid.UUID | None
    title: str
    description: str | None
    assigned_to_id: uuid.UUID | None
    due_at: datetime | None
    status: str
    created_by_id: uuid.UUID | None
    created_at: datetime


class NoteIn(BaseModel):
    body: str = Field(min_length=1, max_length=20000)
    visibility: NoteVisibility = NoteVisibility.INTERNAL


class NoteUpdate(BaseModel):
    body: str | None = Field(None, min_length=1, max_length=20000)
    visibility: NoteVisibility | None = None


class NoteOut(ORM):
    id: uuid.UUID
    case_id: uuid.UUID
    body: str
    visibility: str
    author_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class NotificationOut(ORM):
    id: uuid.UUID
    kind: str
    title: str
    body: str | None
    case_id: uuid.UUID | None
    read_at: datetime | None
    created_at: datetime


class AuditOut(ORM):
    id: uuid.UUID
    tenant_id: uuid.UUID | None
    actor_id: uuid.UUID | None
    action: str
    entity_type: str | None
    entity_id: str | None
    details: dict
    ip_address: str | None
    created_at: datetime


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


TokenOut.model_rebuild()

__all__ = [n for n in dir() if not n.startswith("_")] + ["Role"]
