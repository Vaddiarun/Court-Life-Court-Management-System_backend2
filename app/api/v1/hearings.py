"""Hearing calendar: day / week / month views (PRD 3.4). Official vs manual entries are flagged."""

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import or_, select

from app.api.deps import DB, Staff, TenantUser, audit, get_staff_case, get_visible_case, visible_cases_query
from app.core.timeutil import today_local
from app.models import Case, CaseAssignment, Hearing
from app.models.enums import HearingSource
from app.schemas import HearingIn, HearingOut, HearingUpdate

router = APIRouter(prefix="/hearings", tags=["calendar"])


def view_range(view: str, anchor: date) -> tuple[date, date]:
    if view == "day":
        return anchor, anchor
    if view == "week":
        start = anchor - timedelta(days=anchor.weekday())
        return start, start + timedelta(days=6)
    start = anchor.replace(day=1)
    nxt = (start + timedelta(days=32)).replace(day=1)
    return start, nxt - timedelta(days=1)


@router.get("", response_model=list[HearingOut])
async def calendar(
    user: TenantUser,
    db: DB,
    view: str = Query("month", pattern="^(day|week|month)$"),
    on: date | None = Query(None, description="Anchor date for the view (default today)"),
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    case_id: uuid.UUID | None = None,
    lawyer_id: uuid.UUID | None = None,
    source: HearingSource | None = None,
):
    if date_from or date_to:
        start, end = date_from or date.min, date_to or date.max
    else:
        start, end = view_range(view, on or today_local())
    visible = visible_cases_query(user).with_only_columns(Case.id)
    stmt = (
        select(Hearing, Case.title)
        .join(Case, Case.id == Hearing.case_id)
        .where(Hearing.case_id.in_(visible), Hearing.hearing_date >= start, Hearing.hearing_date <= end)
    )
    if case_id:
        stmt = stmt.where(Hearing.case_id == case_id)
    if lawyer_id:
        assigned = select(CaseAssignment.case_id).where(CaseAssignment.user_id == lawyer_id)
        stmt = stmt.where(or_(Case.lead_lawyer_id == lawyer_id, Case.id.in_(assigned)))
    if source:
        stmt = stmt.where(Hearing.source == source)
    rows = (await db.execute(stmt.order_by(Hearing.hearing_date, Hearing.start_time.nulls_last()))).all()
    out = []
    for h, title in rows:
        item = HearingOut.model_validate(h)
        item.case_title = title
        out.append(item)
    return out


@router.post("", response_model=HearingOut, status_code=201)
async def add_manual_hearing(body: HearingIn, user: Staff, db: DB, request: Request):
    case = await get_staff_case(db, user, body.case_id)
    h = Hearing(tenant_id=case.tenant_id, source=HearingSource.MANUAL, created_by_id=user.id, **body.model_dump())
    db.add(h)
    await db.flush()
    await audit(db, user, "hearing.created", "hearing", h.id, {"case_id": str(case.id)}, request)
    out = HearingOut.model_validate(h)
    out.case_title = case.title
    return out


async def _manual_hearing(db, user, hearing_id) -> Hearing:
    h = await db.get(Hearing, hearing_id)
    if h is None or h.tenant_id != user.tenant_id:
        raise HTTPException(404, "Hearing not found")
    await get_staff_case(db, user, h.case_id)
    if h.source == HearingSource.OFFICIAL:
        raise HTTPException(403, "Officially sourced hearings cannot be edited; add a manual entry instead")
    return h


@router.patch("/{hearing_id}", response_model=HearingOut)
async def update_hearing(hearing_id: uuid.UUID, body: HearingUpdate, user: Staff, db: DB, request: Request):
    h = await _manual_hearing(db, user, hearing_id)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(h, k, v)
    await audit(db, user, "hearing.updated", "hearing", h.id, request=request)
    await db.flush()
    return h


@router.delete("/{hearing_id}", status_code=204)
async def delete_hearing(hearing_id: uuid.UUID, user: Staff, db: DB, request: Request):
    h = await _manual_hearing(db, user, hearing_id)
    await audit(db, user, "hearing.deleted", "hearing", h.id, request=request)
    await db.delete(h)


@router.get("/case/{case_id}", response_model=list[HearingOut])
async def case_hearings(case_id: uuid.UUID, user: TenantUser, db: DB):
    case = await get_visible_case(db, user, case_id)
    rows = (await db.execute(select(Hearing).where(Hearing.case_id == case.id).order_by(Hearing.hearing_date.desc()))).scalars().all()
    return rows
