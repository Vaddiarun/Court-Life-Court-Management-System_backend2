"""Reminders (PRD 3.5) - each reminder expands into scheduled per-recipient, per-channel jobs."""

import uuid

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import func, select

from app.api.deps import DB, Staff, TenantUser, audit, get_staff_case, get_tenant_member
from app.models import Hearing, NotificationJob, Reminder
from app.models.enums import Role
from app.schemas import JobOut, Page, ReminderIn, ReminderOut, ReminderUpdate
from app.services.reminders import build_jobs, cancel_pending_jobs, hearing_event_at
from app.services.sync import client_ids_for_case

router = APIRouter(prefix="/reminders", tags=["reminders"])


async def _recipients(db, user, ids, case_id, include_clients) -> list[str]:
    out: list[uuid.UUID] = []
    for uid in ids:
        member = await get_tenant_member(db, user.tenant_id, uid)
        if member.role == Role.CLIENT and case_id is None:
            raise HTTPException(422, "Client recipients require a case")
        out.append(member.id)
    if include_clients and case_id:
        out += await client_ids_for_case(db, case_id)
    if not out:
        out = [user.id]
    return [str(u) for u in dict.fromkeys(out)]


async def _get_reminder(db, user, reminder_id) -> Reminder:
    r = await db.get(Reminder, reminder_id)
    if r is None or r.tenant_id != user.tenant_id:
        raise HTTPException(404, "Reminder not found")
    if user.role == Role.JUNIOR_ADVOCATE and r.created_by_id != user.id and str(user.id) not in r.recipient_ids:
        raise HTTPException(404, "Reminder not found")
    return r


@router.post("", response_model=ReminderOut, status_code=201)
async def create_reminder(body: ReminderIn, user: Staff, db: DB, request: Request):
    case_id = body.case_id
    event_at = body.event_at
    if body.hearing_id:
        hearing = await db.get(Hearing, body.hearing_id)
        if hearing is None or hearing.tenant_id != user.tenant_id:
            raise HTTPException(404, "Hearing not found")
        case_id = case_id or hearing.case_id
        event_at = event_at or hearing_event_at(hearing.hearing_date)
    if case_id:
        await get_staff_case(db, user, case_id)
    if user.role == Role.JUNIOR_ADVOCATE and any(r != user.id for r in body.recipient_ids):
        # juniors can only remind themselves unless the firm allows otherwise
        raise HTTPException(403, "Junior advocates can only create reminders for themselves")
    reminder = Reminder(
        tenant_id=user.tenant_id,
        case_id=case_id,
        hearing_id=body.hearing_id,
        reminder_type=body.reminder_type,
        title=body.title,
        message=body.message,
        event_at=event_at,
        offsets_days=body.offsets_days,
        custom_times=[d.isoformat() for d in body.custom_times],
        channels=[c.value for c in body.channels],
        recipient_ids=await _recipients(db, user, body.recipient_ids, case_id, body.include_case_clients),
        created_by_id=user.id,
    )
    db.add(reminder)
    await db.flush()
    await build_jobs(db, reminder)
    await audit(db, user, "reminder.created", "reminder", reminder.id, {"case_id": str(case_id) if case_id else None}, request)
    return reminder


@router.get("", response_model=Page[ReminderOut])
async def list_reminders(
    user: TenantUser,
    db: DB,
    case_id: uuid.UUID | None = None,
    active: bool | None = True,
    mine: bool = False,
    limit: int = Query(50, le=200),
    offset: int = 0,
):
    stmt = select(Reminder).where(Reminder.tenant_id == user.tenant_id)
    if case_id:
        stmt = stmt.where(Reminder.case_id == case_id)
    if active is not None:
        stmt = stmt.where(Reminder.is_active.is_(active))
    rows = (await db.execute(stmt.order_by(Reminder.event_at))).scalars().all()
    if user.role in (Role.CLIENT, Role.JUNIOR_ADVOCATE) or mine:  # precise JSON membership check
        rows = [r for r in rows if r.created_by_id == user.id or str(user.id) in r.recipient_ids]
    total = len(rows)
    return Page(items=rows[offset : offset + limit], total=total, limit=limit, offset=offset)


@router.get("/{reminder_id}", response_model=ReminderOut)
async def get_reminder(reminder_id: uuid.UUID, user: Staff, db: DB):
    return await _get_reminder(db, user, reminder_id)


@router.patch("/{reminder_id}", response_model=ReminderOut)
async def update_reminder(reminder_id: uuid.UUID, body: ReminderUpdate, user: Staff, db: DB, request: Request):
    r = await _get_reminder(db, user, reminder_id)
    changes = body.model_dump(exclude_unset=True)
    if "recipient_ids" in changes:
        changes["recipient_ids"] = await _recipients(db, user, changes["recipient_ids"], r.case_id, False)
    if "channels" in changes:
        changes["channels"] = [c.value if hasattr(c, "value") else c for c in changes["channels"]]
    if "custom_times" in changes:
        changes["custom_times"] = [d.isoformat() for d in changes["custom_times"]]
    if "offsets_days" in changes and any(o < 0 or o > 365 for o in changes["offsets_days"]):
        raise HTTPException(422, "offsets must be between 0 and 365 days")
    for k, v in changes.items():
        setattr(r, k, v)
    await db.flush()
    await build_jobs(db, r)
    await audit(db, user, "reminder.updated", "reminder", r.id, {"fields": sorted(changes)}, request)
    return r


@router.delete("/{reminder_id}", status_code=204)
async def delete_reminder(reminder_id: uuid.UUID, user: Staff, db: DB, request: Request):
    r = await _get_reminder(db, user, reminder_id)
    await cancel_pending_jobs(db, r.id)
    await audit(db, user, "reminder.deleted", "reminder", r.id, request=request)
    await db.delete(r)


@router.get("/{reminder_id}/jobs", response_model=list[JobOut])
async def reminder_jobs(reminder_id: uuid.UUID, user: Staff, db: DB):
    r = await _get_reminder(db, user, reminder_id)
    q = select(NotificationJob).where(NotificationJob.reminder_id == r.id).order_by(NotificationJob.scheduled_at)
    return (await db.execute(q)).scalars().all()


@router.get("/stats/summary")
async def reminder_stats(user: Staff, db: DB):
    rows = (
        await db.execute(
            select(NotificationJob.channel, NotificationJob.status, func.count())
            .where(NotificationJob.tenant_id == user.tenant_id)
            .group_by(NotificationJob.channel, NotificationJob.status)
        )
    ).all()
    out: dict[str, dict[str, int]] = {}
    for ch, st, n in rows:
        out.setdefault(ch, {})[st] = n
    return out
