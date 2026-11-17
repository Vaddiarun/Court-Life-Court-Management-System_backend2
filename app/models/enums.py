from enum import StrEnum


class Role(StrEnum):
    SUPER_ADMIN = "super_admin"
    FIRM_ADMIN = "firm_admin"
    LAWYER = "lawyer"
    JUNIOR_ADVOCATE = "junior_advocate"
    CLIENT = "client"


STAFF_ROLES = {Role.FIRM_ADMIN, Role.LAWYER, Role.JUNIOR_ADVOCATE}
MANAGER_ROLES = {Role.FIRM_ADMIN, Role.LAWYER}


class TenantStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class Plan(StrEnum):
    INDIVIDUAL = "individual"
    SMALL_FIRM = "small_firm"
    PROFESSIONAL = "professional"
    ENTERPRISE = "enterprise"


class CaseSource(StrEnum):
    PROVIDER = "provider"
    MANUAL = "manual"


class HearingSource(StrEnum):
    OFFICIAL = "official"
    MANUAL = "manual"


class NoteVisibility(StrEnum):
    INTERNAL = "internal"  # firm admin + lawyers only
    TEAM = "team"  # + junior advocates assigned to case
    CLIENT = "client"  # + clients with access to the case


class TaskStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class ReminderType(StrEnum):
    HEARING = "hearing"
    PREPARATION = "preparation"
    FILING = "filing"
    DOCUMENT_SUBMISSION = "document_submission"
    CLIENT_MEETING = "client_meeting"
    FOLLOW_UP = "follow_up"
    PAYMENT = "payment"
    CUSTOM = "custom"


class Channel(StrEnum):
    IN_APP = "in_app"
    EMAIL = "email"
    SMS = "sms"
    CALL = "call"


class JobStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class CallStatus(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in-progress"
    COMPLETED = "completed"
    BUSY = "busy"
    NO_ANSWER = "no-answer"
    FAILED = "failed"


class DocumentCategory(StrEnum):
    PETITION = "petition"
    ORDER = "order"
    EVIDENCE = "evidence"
    AFFIDAVIT = "affidavit"
    CORRESPONDENCE = "correspondence"
    INVOICE = "invoice"
    OTHER = "other"
