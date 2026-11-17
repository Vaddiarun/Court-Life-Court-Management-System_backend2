"""SQLAlchemy models. Every tenant-owned table carries tenant_id for isolation."""

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class IdMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


TENANT_FK = "tenants.id"


class Tenant(IdMixin, TimestampMixin, Base):
    __tablename__ = "tenants"
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    plan: Mapped[str] = mapped_column(String(30), default="small_firm")
    status: Mapped[str] = mapped_column(String(20), default="active")
    default_language: Mapped[str] = mapped_column(String(10), default="en")
    contact_email: Mapped[str | None] = mapped_column(String(255))
    contact_phone: Mapped[str | None] = mapped_column(String(20))
    address: Mapped[str | None] = mapped_column(Text)
    # Allowances (PRD section 8: defined allowance, charge excess separately)
    monthly_api_allowance: Mapped[int] = mapped_column(Integer, default=5000)
    monthly_call_allowance: Mapped[int] = mapped_column(Integer, default=500)
    subscription_ends_at: Mapped[date | None] = mapped_column(Date)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(TENANT_FK, ondelete="CASCADE"), index=True)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(20), index=True)
    full_name: Mapped[str] = mapped_column(String(200))
    hashed_password: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(30), index=True)
    language: Mapped[str] = mapped_column(String(10), default="en")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    bar_council_id: Mapped[str | None] = mapped_column(String(50))
    # Communication preferences & consent (PRD 3.8, 11)
    notify_in_app: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_email: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_sms: Mapped[bool] = mapped_column(Boolean, default=False)
    notify_call: Mapped[bool] = mapped_column(Boolean, default=False)
    call_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sms_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    tenant: Mapped[Tenant | None] = relationship(lazy="joined")


class OtpCode(IdMixin, Base):
    __tablename__ = "otp_codes"
    phone: Mapped[str] = mapped_column(String(20), index=True)
    code_hash: Mapped[str] = mapped_column(String(128))
    provider: Mapped[str] = mapped_column(String(30), default="local")
    provider_ref: Mapped[str | None] = mapped_column(String(100))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Case(IdMixin, TimestampMixin, Base):
    __tablename__ = "cases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "cnr", name="uq_case_tenant_cnr"),
        Index("ix_cases_sync_due", "sync_enabled", "next_sync_at"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(TENANT_FK, ondelete="CASCADE"), index=True)
    cnr: Mapped[str | None] = mapped_column(String(16), index=True)
    title: Mapped[str] = mapped_column(String(500))
    case_number: Mapped[str | None] = mapped_column(String(100))
    case_type: Mapped[str | None] = mapped_column(String(100))
    case_year: Mapped[int | None] = mapped_column(Integer)
    court_name: Mapped[str | None] = mapped_column(String(300))
    court_level: Mapped[str | None] = mapped_column(String(50))  # district / high_court / supreme_court / tribunal
    state: Mapped[str | None] = mapped_column(String(100))
    district: Mapped[str | None] = mapped_column(String(100))
    judge: Mapped[str | None] = mapped_column(String(300))
    petitioners: Mapped[list] = mapped_column(JSON, default=list)
    respondents: Mapped[list] = mapped_column(JSON, default=list)
    advocates: Mapped[list] = mapped_column(JSON, default=list)
    filing_date: Mapped[date | None] = mapped_column(Date)
    registration_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str | None] = mapped_column(String(100))
    stage: Mapped[str | None] = mapped_column(String(200))
    next_hearing_date: Mapped[date | None] = mapped_column(Date, index=True)
    is_disposed: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(20), default="manual")
    provider_name: Mapped[str | None] = mapped_column(String(50))
    provider_modified_at: Mapped[str | None] = mapped_column(String(64))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_snapshot_hash: Mapped[str | None] = mapped_column(String(64))
    sync_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    next_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refresh_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_manual_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_error: Mapped[str | None] = mapped_column(Text)
    lead_lawyer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    description: Mapped[str | None] = mapped_column(Text)


class CaseLookupCache(IdMixin, Base):
    """Result of a CNR lookup kept briefly so 'add to workspace' doesn't pay for a second detail call."""

    __tablename__ = "case_lookup_cache"
    __table_args__ = (UniqueConstraint("tenant_id", "cnr"),)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(TENANT_FK, ondelete="CASCADE"), index=True)
    cnr: Mapped[str] = mapped_column(String(16))
    provider_name: Mapped[str] = mapped_column(String(50))
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CaseAssignment(IdMixin, Base):
    """Lawyers / junior advocates assigned to a case."""

    __tablename__ = "case_assignments"
    __table_args__ = (UniqueConstraint("case_id", "user_id"),)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(TENANT_FK, ondelete="CASCADE"), index=True)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ClientCaseAccess(IdMixin, Base):
    __tablename__ = "client_case_access"
    __table_args__ = (UniqueConstraint("case_id", "client_id"),)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(TENANT_FK, ondelete="CASCADE"), index=True)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    client_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CaseSnapshot(IdMixin, Base):
    __tablename__ = "case_snapshots"
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(TENANT_FK, ondelete="CASCADE"), index=True)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    provider_name: Mapped[str] = mapped_column(String(50))
    data: Mapped[dict] = mapped_column(JSON)
    hash: Mapped[str] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CaseEvent(IdMixin, Base):
    """Change history produced by the sync diff engine (and manual edits)."""

    __tablename__ = "case_events"
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(TENANT_FK, ondelete="CASCADE"), index=True)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(50))  # status_changed, hearing_date_changed, new_order, ...
    field: Mapped[str | None] = mapped_column(String(100))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20), default="sync")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class SyncRun(IdMixin, Base):
    __tablename__ = "sync_runs"
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(TENANT_FK, ondelete="CASCADE"), index=True)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    trigger: Mapped[str] = mapped_column(String(20))  # manual | scheduled | webhook
    status: Mapped[str] = mapped_column(String(20))  # success | unchanged | failed
    changes: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    triggered_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Hearing(IdMixin, TimestampMixin, Base):
    __tablename__ = "hearings"
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(TENANT_FK, ondelete="CASCADE"), index=True)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    hearing_date: Mapped[date] = mapped_column(Date, index=True)
    start_time: Mapped[str | None] = mapped_column(String(10))
    purpose: Mapped[str | None] = mapped_column(String(300))
    judge: Mapped[str | None] = mapped_column(String(300))
    court_room: Mapped[str | None] = mapped_column(String(100))
    business: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20), default="manual")
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class Document(IdMixin, TimestampMixin, Base):
    __tablename__ = "documents"
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(TENANT_FK, ondelete="CASCADE"), index=True)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(300))
    content_type: Mapped[str] = mapped_column(String(150))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    storage_key: Mapped[str] = mapped_column(String(500))
    sha256: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(30), default="other")
    description: Mapped[str | None] = mapped_column(Text)
    shared_with_client: Mapped[bool] = mapped_column(Boolean, default=False)
    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class Task(IdMixin, TimestampMixin, Base):
    __tablename__ = "tasks"
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(TENANT_FK, ondelete="CASCADE"), index=True)
    case_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    assigned_to_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="open")
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class Note(IdMixin, TimestampMixin, Base):
    __tablename__ = "notes"
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(TENANT_FK, ondelete="CASCADE"), index=True)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    body: Mapped[str] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(String(20), default="internal")
    author_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class Reminder(IdMixin, TimestampMixin, Base):
    __tablename__ = "reminders"
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(TENANT_FK, ondelete="CASCADE"), index=True)
    case_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    hearing_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("hearings.id", ondelete="CASCADE"))
    reminder_type: Mapped[str] = mapped_column(String(30), default="custom")
    title: Mapped[str] = mapped_column(String(300))
    message: Mapped[str | None] = mapped_column(Text)
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    offsets_days: Mapped[list] = mapped_column(JSON, default=list)  # e.g. [7, 3, 1, 0]
    custom_times: Mapped[list] = mapped_column(JSON, default=list)  # ISO datetimes
    channels: Mapped[list] = mapped_column(JSON, default=lambda: ["in_app"])
    recipient_ids: Mapped[list] = mapped_column(JSON, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_generated: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class NotificationJob(IdMixin, Base):
    """One scheduled delivery: (reminder, recipient, channel, time)."""

    __tablename__ = "notification_jobs"
    __table_args__ = (Index("ix_jobs_due", "status", "scheduled_at"),)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(TENANT_FK, ondelete="CASCADE"), index=True)
    reminder_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("reminders.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(20))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    provider_ref: Mapped[str | None] = mapped_column(String(200))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CallAttempt(IdMixin, Base):
    __tablename__ = "call_attempts"
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(TENANT_FK, ondelete="CASCADE"), index=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("notification_jobs.id", ondelete="CASCADE"), index=True)
    attempt_no: Mapped[int] = mapped_column(Integer)
    to_phone: Mapped[str] = mapped_column(String(20))
    language: Mapped[str] = mapped_column(String(10))
    provider_call_id: Mapped[str | None] = mapped_column(String(200), index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Notification(IdMixin, Base):
    """In-app notification inbox."""

    __tablename__ = "notifications"
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(TENANT_FK, ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str | None] = mapped_column(Text)
    case_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ApiUsage(IdMixin, Base):
    __tablename__ = "api_usage"
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(TENANT_FK, ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(50))
    operation: Mapped[str] = mapped_column(String(50))  # search | detail | refresh | order_pdf | call | sms | email
    cnr: Mapped[str | None] = mapped_column(String(16))
    case_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cases.id", ondelete="SET NULL"))
    units: Mapped[float] = mapped_column(Float, default=1)
    unit_cost: Mapped[float] = mapped_column(Float, default=0)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class AuditLog(IdMixin, Base):
    __tablename__ = "audit_logs"
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(TENANT_FK, ondelete="CASCADE"), index=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    action: Mapped[str] = mapped_column(String(80), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(50))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
