"""Reminder scheduling and multi-channel dispatch (PRD 3.5, 3.6, 3.8)."""

import math
import uuid
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.i18n import t
from app.models import CallAttempt, Case, ClientCaseAccess, Notification, NotificationJob, Reminder, Tenant, User
from app.models.enums import CallStatus, Channel, JobStatus
from app.providers.messaging import ChannelError, get_email, get_sms, get_voice
from app.services.notify import case_team_ids
from app.services.usage import record_usage

TZ = ZoneInfo(settings.TIMEZONE)
FINAL_FAIL_CALL = {CallStatus.BUSY, CallStatus.NO_ANSWER, CallStatus.FAILED}


def hearing_event_at(d: date) -> datetime:
    """Courts sit from ~10:30 local; use that as the event time for date-only hearings."""
    return datetime.combine(d, time(10, 30), TZ).astimezone(UTC)


def schedule_times(reminder: Reminder) -> list[datetime]:
    event_at = reminder.event_at if reminder.event_at.tzinfo else reminder.event_at.replace(tzinfo=UTC)
    times: list[datetime] = []
    for off in sorted({int(o) for o in reminder.offsets_days or []}, reverse=True):
        local_day = (event_at.astimezone(TZ) - timedelta(days=off)).date()
        when = datetime.combine(local_day, time(settings.REMINDER_SEND_HOUR), TZ).astimezone(UTC)
        if when >= event_at:
            when = event_at - timedelta(hours=1)
        times.append(when)
    for raw in reminder.custom_times or []:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))  # noqa: FURB162
        times.append(dt if dt.tzinfo else dt.replace(tzinfo=UTC))
    return sorted(set(times))


async def cancel_pending_jobs(db: AsyncSession, reminder_id: uuid.UUID) -> None:
    await db.execute(
        update(NotificationJob)
        .where(NotificationJob.reminder_id == reminder_id, NotificationJob.status == JobStatus.PENDING)
        .values(status=JobStatus.CANCELLED)
    )


async def build_jobs(db: AsyncSession, reminder: Reminder, now: datetime | None = None) -> int:
    """(Re)generate the pending notification jobs for a reminder. Past times are skipped."""
    now = now or datetime.now(UTC)
    await cancel_pending_jobs(db, reminder.id)
    if not reminder.is_active:
        return 0
    count = 0
    for when in schedule_times(reminder):
        if when < now - timedelta(minutes=1):
            continue
        for uid in reminder.recipient_ids:
            for ch in reminder.channels:
                db.add(
                    NotificationJob(
                        tenant_id=reminder.tenant_id,
                        reminder_id=reminder.id,
                        user_id=uuid.UUID(str(uid)),
                        channel=ch,
                        scheduled_at=when,
                    )
                )
                count += 1
    return count


async def create_auto_hearing_reminder(db: AsyncSession, case: Case, hearing_date: date) -> Reminder | None:
    tenant = await db.get(Tenant, case.tenant_id)
    cfg = (tenant.settings or {}) if tenant else {}
    if not cfg.get("auto_hearing_reminders", True):
        return None
    # retire previous auto reminders for this case
    old = (
        (
            await db.execute(
                select(Reminder).where(Reminder.case_id == case.id, Reminder.auto_generated.is_(True), Reminder.is_active.is_(True))
            )
        )
        .scalars()
        .all()
    )
    for r in old:
        r.is_active = False
        await cancel_pending_jobs(db, r.id)
    recipients = await case_team_ids(db, case, include_clients=bool(cfg.get("auto_reminders_include_clients", True)))
    if not recipients:
        return None
    reminder = Reminder(
        tenant_id=case.tenant_id,
        case_id=case.id,
        reminder_type="hearing",
        title=f"Hearing: {case.title}"[:300],
        event_at=hearing_event_at(hearing_date),
        offsets_days=cfg.get("auto_reminder_offsets", settings.AUTO_REMINDER_OFFSETS),
        channels=cfg.get("auto_reminder_channels", [Channel.IN_APP.value]),
        recipient_ids=[str(r) for r in recipients],
        auto_generated=True,
    )
    db.add(reminder)
    await db.flush()
    await build_jobs(db, reminder)
    return reminder


# ------------------------------------------------------------------ dispatch
def _render(reminder: Reminder, user: User, case: Case | None, firm: str, lang: str | None = None) -> tuple[str, str, str]:
    lang = lang or user.language or "en"
    event_local = reminder.event_at.astimezone(TZ)
    params = {
        "title": reminder.title,
        "event_date": event_local.strftime("%d %b %Y %I:%M %p"),
        "message": reminder.message or "",
        "name": user.full_name,
        "firm": firm,
        "case_title": case.title if case else "",
    }
    params["case_line"] = t("reminder.case_line", lang, **params) if case else ""
    return t("reminder.title", lang, **params), t("reminder.body", lang, **params), t("reminder.call", lang, **params)


def sms_template(reminder: Reminder, case: Case | None) -> tuple[str, list[str]]:
    """DLT template kind + its {#var#} values, in the order of the approved templates:
    hearing:  "Reminder: Your case {#var#} is listed on {#var#} at {#var#}. - <firm>"
    reminder: "Reminder: {#var#} on {#var#} at {#var#}. - <firm>"
    """
    event_local = reminder.event_at.astimezone(TZ)
    day, at = event_local.strftime("%d-%m-%Y"), event_local.strftime("%I:%M %p")
    if reminder.reminder_type == "hearing" and case is not None:
        ref = case.case_number or case.cnr or case.title
        return "hearing", [ref, day, at]
    return "reminder", [reminder.title, day, at]


def _in_call_window(now: datetime) -> tuple[bool, datetime]:
    local = now.astimezone(TZ)
    start = local.replace(hour=settings.CALL_WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
    end = local.replace(hour=settings.CALL_WINDOW_END_HOUR, minute=0, second=0, microsecond=0)
    if start <= local < end:
        return True, now
    nxt = start if local < start else start + timedelta(days=1)
    return False, nxt.astimezone(UTC)


async def _user_can_see_case(db: AsyncSession, user: User, case: Case | None) -> bool:
    if case is None or user.role != "client":
        return True
    q = select(ClientCaseAccess.id).where(ClientCaseAccess.case_id == case.id, ClientCaseAccess.client_id == user.id)
    return (await db.execute(q)).first() is not None


async def dispatch_job(db: AsyncSession, job: NotificationJob, now: datetime) -> str:
    user = await db.get(User, job.user_id)
    reminder = await db.get(Reminder, job.reminder_id) if job.reminder_id else None
    if user is None or not user.is_active or reminder is None or not reminder.is_active:
        job.status, job.last_error = JobStatus.SKIPPED, "recipient or reminder inactive"
        return job.status
    case = await db.get(Case, reminder.case_id) if reminder.case_id else None
    if not await _user_can_see_case(db, user, case):
        job.status, job.last_error = JobStatus.SKIPPED, "client has no access to case"
        return job.status
    tenant = await db.get(Tenant, job.tenant_id)
    title, body, call_text = _render(reminder, user, case, tenant.name if tenant else "your law firm")

    try:
        if job.channel == Channel.IN_APP:
            if not user.notify_in_app:
                job.status, job.last_error = JobStatus.SKIPPED, "in-app disabled by user"
                return job.status
            db.add(
                Notification(tenant_id=user.tenant_id, user_id=user.id, kind="reminder", title=title, body=body, case_id=reminder.case_id)
            )
        elif job.channel == Channel.EMAIL:
            if not (user.notify_email and user.email):
                job.status, job.last_error = JobStatus.SKIPPED, "email disabled or missing"
                return job.status
            job.provider_ref = await get_email().send(user.email, title, body)
            record_usage(db, job.tenant_id, get_email().name, "email")
        elif job.channel == Channel.SMS:
            if not (user.notify_sms and user.phone and user.sms_consent_at):
                job.status, job.last_error = JobStatus.SKIPPED, "sms not consented or phone missing"
                return job.status
            kind, variables = sms_template(reminder, case)
            job.provider_ref = await get_sms().send(user.phone, body, template=kind, variables=variables)
            record_usage(db, job.tenant_id, get_sms().name, "sms")
        elif job.channel == Channel.CALL:
            if not (user.notify_call and user.phone and user.call_consent_at):
                job.status, job.last_error = JobStatus.SKIPPED, "call not consented or phone missing"
                return job.status
            ok, next_window = _in_call_window(now)
            if not ok:
                job.scheduled_at = next_window
                return "deferred"
            voice = get_voice()
            call_lang = user.language if voice.supports_language(user.language or "en") else "en"
            if call_lang != user.language:
                call_text = _render(reminder, user, case, tenant.name if tenant else "your law firm", lang=call_lang)[2]
            job.attempts += 1
            attempt = CallAttempt(tenant_id=job.tenant_id, job_id=job.id, attempt_no=job.attempts, to_phone=user.phone, language=call_lang)
            db.add(attempt)
            result = await voice.place_call(user.phone, call_text, call_lang, str(job.id))
            attempt.provider_call_id, attempt.status = result.call_id, result.status
            job.provider_ref = result.call_id
        else:
            job.status, job.last_error = JobStatus.FAILED, f"unknown channel {job.channel}"
            return job.status
    except ChannelError as e:
        job.last_error = str(e)
        if job.channel == Channel.CALL and job.attempts < settings.CALL_MAX_ATTEMPTS:
            job.scheduled_at = now + timedelta(minutes=settings.CALL_RETRY_MINUTES)
            return "retry"
        job.status = JobStatus.FAILED
        return job.status

    job.status, job.sent_at = JobStatus.SENT, now
    return job.status


async def dispatch_due(db: AsyncSession, now: datetime | None = None, limit: int = 200) -> dict:
    now = now or datetime.now(UTC)
    jobs = (
        (
            await db.execute(
                select(NotificationJob)
                .where(NotificationJob.status == JobStatus.PENDING, NotificationJob.scheduled_at <= now)
                .order_by(NotificationJob.scheduled_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    stats: dict[str, int] = {}
    for job in jobs:
        outcome = await dispatch_job(db, job, now)
        stats[outcome] = stats.get(outcome, 0) + 1
    await db.flush()
    return {"processed": len(jobs), **stats}


async def handle_call_status(
    db: AsyncSession, call_id: str, status: str, duration_seconds: int | None, now: datetime | None = None
) -> bool:
    """Exotel StatusCallback. Retries unanswered calls up to CALL_MAX_ATTEMPTS."""
    now = now or datetime.now(UTC)
    attempt = (await db.execute(select(CallAttempt).where(CallAttempt.provider_call_id == call_id))).scalar_one_or_none()
    if attempt is None:
        return False
    status = status.lower()
    attempt.status = status
    attempt.duration_seconds = duration_seconds
    job = await db.get(NotificationJob, attempt.job_id)
    if duration_seconds:
        record_usage(db, attempt.tenant_id, get_voice().name, "call", units=math.ceil(duration_seconds / 60))
    elif status in FINAL_FAIL_CALL:
        record_usage(db, attempt.tenant_id, get_voice().name, "call", units=0, success=False)
    if job and status in FINAL_FAIL_CALL:
        if job.attempts < settings.CALL_MAX_ATTEMPTS:
            job.status = JobStatus.PENDING
            job.scheduled_at = now + timedelta(minutes=settings.CALL_RETRY_MINUTES)
        else:
            job.status, job.last_error = JobStatus.FAILED, f"call {status} after {job.attempts} attempts"
    elif job and status == CallStatus.COMPLETED:
        job.status = JobStatus.SENT
    return True
