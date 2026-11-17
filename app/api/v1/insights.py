"""Notifications inbox, dashboards, firm reports and usage (PRD 3.8, 3.10, 8)."""

import uuid
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select, update

from app.api.deps import DB, CurrentUser, FirmAdmin, TenantUser, visible_cases_query
from app.core.timeutil import today_local
from app.models import CallAttempt, Case, Hearing, Notification, NotificationJob, Reminder, Task, Tenant, User
from app.models.enums import JobStatus, Role
from app.schemas import NotificationOut, Page
from app.services.usage import usage_summary

router = APIRouter(tags=["dashboard & notifications"])


# ------------------------------------------------------------------ notifications
@router.get("/notifications", response_model=Page[NotificationOut])
async def my_notifications(user: CurrentUser, db: DB, unread: bool = False, limit: int = Query(30, le=100), offset: int = 0):
    stmt = select(Notification).where(Notification.user_id == user.id)
    if unread:
        stmt = stmt.where(Notification.read_at.is_(None))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    items = (await db.execute(stmt.order_by(Notification.created_at.desc()).limit(limit).offset(offset))).scalars().all()
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/notifications/unread-count")
async def unread_count(user: CurrentUser, db: DB):
    n = (
        await db.execute(select(func.count(Notification.id)).where(Notification.user_id == user.id, Notification.read_at.is_(None)))
    ).scalar_one()
    return {"unread": n}


@router.post("/notifications/{notification_id}/read", response_model=NotificationOut)
async def mark_read(notification_id: uuid.UUID, user: CurrentUser, db: DB):
    n = await db.get(Notification, notification_id)
    if n is None or n.user_id != user.id:
        raise HTTPException(404, "Notification not found")
    n.read_at = n.read_at or datetime.now(UTC)
    await db.flush()
    return n


@router.post("/notifications/read-all")
async def mark_all_read(user: CurrentUser, db: DB):
    res = await db.execute(
        update(Notification).where(Notification.user_id == user.id, Notification.read_at.is_(None)).values(read_at=datetime.now(UTC))
    )
    return {"updated": res.rowcount}


# ------------------------------------------------------------------ dashboard
@router.get("/dashboard")
async def dashboard(user: TenantUser, db: DB):
    today = today_local()
    visible = visible_cases_query(user).subquery()
    case_ids = select(visible.c.id)
    active = (await db.execute(select(func.count()).select_from(visible).where(visible.c.is_disposed.is_(False)))).scalar_one()
    disposed = (await db.execute(select(func.count()).select_from(visible).where(visible.c.is_disposed.is_(True)))).scalar_one()
    by_status = dict((await db.execute(select(func.coalesce(visible.c.status, "Unknown"), func.count()).group_by(visible.c.status))).all())
    upcoming_rows = (
        await db.execute(
            select(Hearing.id, Hearing.case_id, Hearing.hearing_date, Hearing.purpose, Hearing.source, Case.title)
            .join(Case, Case.id == Hearing.case_id)
            .where(Hearing.case_id.in_(case_ids), Hearing.hearing_date >= today, Hearing.hearing_date <= today + timedelta(days=7))
            .order_by(Hearing.hearing_date)
            .limit(20)
        )
    ).all()
    data = {
        "role": user.role,
        "active_cases": active,
        "disposed_cases": disposed,
        "case_status_distribution": by_status,
        "hearings_today": sum(1 for r in upcoming_rows if r.hearing_date == today),
        "upcoming_hearings_7d": [
            {
                "hearing_id": r.id,
                "case_id": r.case_id,
                "case_title": r.title,
                "date": r.hearing_date,
                "purpose": r.purpose,
                "source": r.source,
            }
            for r in upcoming_rows
        ],
        "unread_notifications": (
            await db.execute(select(func.count(Notification.id)).where(Notification.user_id == user.id, Notification.read_at.is_(None)))
        ).scalar_one(),
    }
    if user.role != Role.CLIENT:
        data["pending_reminder_jobs"] = (
            await db.execute(
                select(func.count(NotificationJob.id)).where(
                    NotificationJob.user_id == user.id, NotificationJob.status == JobStatus.PENDING
                )
            )
        ).scalar_one()
        data["my_open_tasks"] = (
            await db.execute(select(func.count(Task.id)).where(Task.assigned_to_id == user.id, Task.status != "done"))
        ).scalar_one()
        data["overdue_tasks"] = (
            await db.execute(
                select(func.count(Task.id)).where(
                    Task.tenant_id == user.tenant_id, Task.assigned_to_id == user.id, Task.status != "done", Task.due_at < datetime.now(UTC)
                )
            )
        ).scalar_one()
        data["cases_with_sync_errors"] = (
            await db.execute(select(func.count()).select_from(visible).where(visible.c.last_sync_error.is_not(None)))
        ).scalar_one()
    return data


# ------------------------------------------------------------------ firm reports
@router.get("/reports/firm")
async def firm_report(admin: FirmAdmin, db: DB, year: int | None = None, month: int | None = Query(None, ge=1, le=12)):
    now = datetime.now(UTC)
    year, month = year or now.year, month or now.month
    tid = admin.tenant_id
    tenant = await db.get(Tenant, tid)
    start = date(year, month, 1)
    end = (start + timedelta(days=32)).replace(day=1)

    lawyers = (
        await db.execute(
            select(User.id, User.full_name, func.count(Case.id))
            .outerjoin(Case, (Case.lead_lawyer_id == User.id) & (Case.is_disposed.is_(False)))
            .where(User.tenant_id == tid, User.role.in_([Role.LAWYER, Role.FIRM_ADMIN]))
            .group_by(User.id, User.full_name)
        )
    ).all()
    hearings_in_month = (
        await db.execute(
            select(func.count(Hearing.id)).where(Hearing.tenant_id == tid, Hearing.hearing_date >= start, Hearing.hearing_date < end)
        )
    ).scalar_one()
    jobs = (
        await db.execute(
            select(NotificationJob.channel, NotificationJob.status, func.count())
            .where(
                NotificationJob.tenant_id == tid,
                NotificationJob.scheduled_at >= datetime(year, month, 1, tzinfo=UTC),
                NotificationJob.scheduled_at < datetime(end.year, end.month, 1, tzinfo=UTC),
            )
            .group_by(NotificationJob.channel, NotificationJob.status)
        )
    ).all()
    calls = dict(
        (await db.execute(select(CallAttempt.status, func.count()).where(CallAttempt.tenant_id == tid).group_by(CallAttempt.status))).all()
    )
    usage = await usage_summary(db, tid, year, month)
    return {
        "period": f"{year}-{month:02d}",
        "cases_by_status": dict(
            (
                await db.execute(
                    select(func.coalesce(Case.status, "Unknown"), func.count()).where(Case.tenant_id == tid).group_by(Case.status)
                )
            ).all()
        ),
        "active_cases_by_lawyer": [{"user_id": u, "name": n, "active_cases": c} for u, n, c in lawyers],
        "hearings_in_period": hearings_in_month,
        "notifications": [{"channel": c, "status": s, "count": n} for c, s, n in jobs],
        "call_attempts_by_status": calls,
        "active_reminders": (
            await db.execute(select(func.count(Reminder.id)).where(Reminder.tenant_id == tid, Reminder.is_active.is_(True)))
        ).scalar_one(),
        "usage": usage,
        "allowances": {
            "monthly_api_allowance": tenant.monthly_api_allowance,
            "api_requests_used": usage["court_api_requests"],
            "api_overage": max(0, usage["court_api_requests"] - tenant.monthly_api_allowance),
            "monthly_call_allowance": tenant.monthly_call_allowance,
            "call_minutes_used": usage["call_minutes"],
            "call_overage": max(0, usage["call_minutes"] - tenant.monthly_call_allowance),
        },
    }


@router.get("/usage")
async def firm_usage(admin: FirmAdmin, db: DB, year: int | None = None, month: int | None = Query(None, ge=1, le=12)):
    now = datetime.now(UTC)
    return await usage_summary(db, admin.tenant_id, year or now.year, month or now.month)
