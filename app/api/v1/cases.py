"""Cases: CNR lookup, add, manual entry with duplicate detection, list/detail, assignments, sync."""

import asyncio
import re
import uuid
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import delete, func, or_, select

from app.api.deps import (
    DB,
    Manager,
    Staff,
    TenantUser,
    audit,
    get_manageable_case,
    get_staff_case,
    get_tenant_member,
    get_visible_case,
    visible_cases_query,
)
from app.core.config import settings
from app.models import (
    Case,
    CaseAssignment,
    CaseEvent,
    CaseLookupCache,
    ClientCaseAccess,
    SyncRun,
    User,
)
from app.models.enums import CaseSource, Role
from app.providers.court import get_court_provider
from app.providers.court.base import CaseNotFound, CaseRecord, ProviderError, is_valid_cnr, normalize_cnr
from app.schemas import (
    AssignIn,
    CaseClientOut,
    CaseEventOut,
    CaseFromCnrIn,
    CaseLookupOut,
    CaseManualIn,
    CaseOut,
    CaseUpdate,
    CnrLookupIn,
    Page,
    SearchIn,
    SyncRunOut,
    UserOut,
)
from app.services.notify import notify_users
from app.services.sync import TRACKED_FIELDS, compute_next_sync, fetch_record, ingest_record, sync_case_now
from app.services.usage import record_usage

router = APIRouter(prefix="/cases", tags=["cases"])
LOOKUP_CACHE_MINUTES = 30


def _cnr_or_422(raw: str) -> str:
    cnr = normalize_cnr(raw)
    if not is_valid_cnr(cnr):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid CNR. Expected 16 characters: 4 letters + 12 digits")
    return cnr


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _provider_http_error(e: ProviderError) -> HTTPException:
    if isinstance(e, CaseFetchPending):
        return HTTPException(
            status.HTTP_404_NOT_FOUND,
            "We've asked the court website for this case. It can take 1-2 minutes for a new case - please try again shortly.",
        )
    if isinstance(e, CaseNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, "No record found for this CNR. Please check the number on the eCourts website.")
    return HTTPException(status.HTTP_502_BAD_GATEWAY, f"Court data provider error: {type(e).__name__}")


async def _existing_case_id(db, tenant_id, cnr) -> uuid.UUID | None:
    return (await db.execute(select(Case.id).where(Case.tenant_id == tenant_id, Case.cnr == cnr))).scalar_one_or_none()


async def _validate_people(db, tenant_id, ids, *, clients: bool) -> list[User]:
    people = []
    for uid in dict.fromkeys(ids):
        u = await get_tenant_member(db, tenant_id, uid)
        if clients and u.role != Role.CLIENT:
            raise HTTPException(422, f"User {uid} is not a client")
        if not clients and u.role not in (Role.LAWYER, Role.JUNIOR_ADVOCATE, Role.FIRM_ADMIN):
            raise HTTPException(422, f"User {uid} is not a staff member")
        people.append(u)
    return people


async def _grant_clients(db, case: Case, clients: list[User]) -> None:
    existing = set((await db.execute(select(ClientCaseAccess.client_id).where(ClientCaseAccess.case_id == case.id))).scalars().all())
    new = [c for c in clients if c.id not in existing]
    for c in new:
        db.add(ClientCaseAccess(tenant_id=case.tenant_id, case_id=case.id, client_id=c.id))
    await notify_users(db, [c.id for c in new], "case_shared", "case.shared.title", "case.shared.body", {"case_title": case.title}, case.id)


async def _resolve_lead(db, actor: User, lead_id: uuid.UUID | None) -> uuid.UUID | None:
    if lead_id is None:
        return actor.id if actor.role == Role.LAWYER else None
    lead = await get_tenant_member(db, actor.tenant_id, lead_id)
    if lead.role not in (Role.LAWYER, Role.FIRM_ADMIN):
        raise HTTPException(422, "Lead lawyer must be a lawyer or firm admin")
    return lead.id


def _lookup_out(record: CaseRecord, provider: str, existing: uuid.UUID | None) -> CaseLookupOut:
    d = record.comparable()
    return CaseLookupOut(**{k: d[k] for k in CaseLookupOut.model_fields if k in d}, provider=provider, already_in_workspace=existing)


async def _fetch_or_pull_live(db, provider, tenant_id, cnr: str) -> CaseRecord:
    """Read the case; if the provider has never indexed this CNR (common for new filings),
    ask it to fetch it live from the eCourts portal and poll briefly before giving up."""
    try:
        return await fetch_record(db, provider, tenant_id, cnr)
    except CaseNotFound:
        pass
    try:
        await provider.request_refresh(cnr)
        record_usage(db, tenant_id, provider.name, "refresh", cnr=cnr)
    except ProviderError:
        raise CaseNotFound(cnr) from None
    waited = 0.0
    while waited < settings.LOOKUP_REFRESH_WAIT_SECONDS:
        step = settings.LOOKUP_REFRESH_POLL_SECONDS
        await asyncio.sleep(step)
        waited += step
        try:
            return await fetch_record(db, provider, tenant_id, cnr)
        except CaseNotFound:
            continue
    raise CaseFetchPending(cnr)


class CaseFetchPending(CaseNotFound):
    """The provider accepted a live fetch but the record is not ready yet."""


# ------------------------------------------------------------------ lookup & create
@router.post("/lookup", response_model=CaseLookupOut)
async def lookup_cnr(body: CnrLookupIn, user: Manager, db: DB):
    """Fetch a case from the court data provider for review before adding it (PRD 3.1)."""
    cnr = _cnr_or_422(body.cnr)
    try:
        provider = get_court_provider()
    except ProviderError as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Court data provider is not set up: {e}") from None
    try:
        record = await _fetch_or_pull_live(db, provider, user.tenant_id, cnr)
    except ProviderError as e:
        await db.commit()  # keep the usage record
        raise _provider_http_error(e) from None
    cache = (
        await db.execute(select(CaseLookupCache).where(CaseLookupCache.tenant_id == user.tenant_id, CaseLookupCache.cnr == cnr))
    ).scalar_one_or_none()
    if cache is None:
        cache = CaseLookupCache(tenant_id=user.tenant_id, cnr=cnr, provider_name=provider.name, data={})
        db.add(cache)
    cache.data, cache.provider_name, cache.created_at = record.comparable(), provider.name, datetime.now(UTC)
    return _lookup_out(record, provider.name, await _existing_case_id(db, user.tenant_id, cnr))


@router.post("/search")
async def provider_search(body: SearchIn, user: Manager, db: DB):
    provider = get_court_provider()
    try:
        results = await provider.search(**body.model_dump(exclude_none=True))
        record_usage(db, user.tenant_id, provider.name, "search")
    except NotImplementedError:
        raise HTTPException(501, "Search not supported by the configured provider") from None
    except ProviderError as e:
        record_usage(db, user.tenant_id, provider.name, "search", success=False)
        await db.commit()
        raise _provider_http_error(e) from None
    return {"provider": provider.name, "results": results}


@router.post("/from-cnr", response_model=CaseOut, status_code=201)
async def add_case_from_cnr(body: CaseFromCnrIn, user: Manager, db: DB, request: Request):
    cnr = _cnr_or_422(body.cnr)
    if existing := await _existing_case_id(db, user.tenant_id, cnr):
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": "Case already in workspace", "case_id": str(existing)})
    provider = get_court_provider()
    cache = (
        await db.execute(select(CaseLookupCache).where(CaseLookupCache.tenant_id == user.tenant_id, CaseLookupCache.cnr == cnr))
    ).scalar_one_or_none()
    fresh = (
        cache and cache.provider_name == provider.name and cache.created_at > datetime.now(UTC) - timedelta(minutes=LOOKUP_CACHE_MINUTES)
    )
    if fresh:
        record = CaseRecord.from_dict(cache.data)
    else:
        try:
            record = await fetch_record(db, provider, user.tenant_id, cnr)
        except ProviderError as e:
            await db.commit()
            raise _provider_http_error(e) from None
    clients = await _validate_people(db, user.tenant_id, body.client_ids, clients=True)
    case = Case(
        tenant_id=user.tenant_id,
        cnr=cnr,
        title=record.title,
        source=CaseSource.PROVIDER,
        sync_enabled=body.sync_enabled,
        lead_lawyer_id=await _resolve_lead(db, user, body.lead_lawyer_id),
        created_by_id=user.id,
    )
    db.add(case)
    await db.flush()
    await _grant_clients(db, case, clients)  # before ingest so auto reminders include clients
    await db.flush()
    await ingest_record(db, case, record, provider.name, "create", user)
    if cache:
        await db.delete(cache)
    await audit(db, user, "case.created", "case", case.id, {"cnr": cnr, "source": "provider"}, request)
    await db.flush()
    return case


@router.post("", response_model=CaseOut, status_code=201)
async def create_manual_case(body: CaseManualIn, user: Manager, db: DB, request: Request):
    """Manual case entry with duplicate detection (PRD 10)."""
    cnr = _cnr_or_422(body.cnr) if body.cnr else None
    if cnr and (existing := await _existing_case_id(db, user.tenant_id, cnr)):
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": "A case with this CNR already exists", "case_id": str(existing)})
    if not body.force and body.case_number:
        candidates = (await db.execute(select(Case).where(Case.tenant_id == user.tenant_id, Case.case_number.is_not(None)))).scalars().all()
        dupes = [
            c
            for c in candidates
            if _norm(c.case_number) == _norm(body.case_number) and (not body.court_name or _norm(c.court_name) == _norm(body.court_name))
        ]
        if dupes:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {"message": "Possible duplicate case. Resend with force=true to create anyway.", "case_ids": [str(c.id) for c in dupes]},
            )
    clients = await _validate_people(db, user.tenant_id, body.client_ids, clients=True)
    data = body.model_dump(exclude={"client_ids", "force", "lead_lawyer_id", "cnr"})
    case = Case(
        **data,
        cnr=cnr,
        tenant_id=user.tenant_id,
        source=CaseSource.MANUAL,
        lead_lawyer_id=await _resolve_lead(db, user, body.lead_lawyer_id),
        created_by_id=user.id,
    )
    case.sync_enabled = bool(cnr) and body.sync_enabled
    case.next_sync_at = compute_next_sync(case, datetime.now(UTC)) if case.sync_enabled else None
    db.add(case)
    await db.flush()
    await _grant_clients(db, case, clients)
    await audit(db, user, "case.created", "case", case.id, {"cnr": cnr, "source": "manual"}, request)
    await db.flush()
    return case


# ------------------------------------------------------------------ read
@router.get("")
async def list_cases(
    user: TenantUser,
    db: DB,
    q: str | None = None,
    status_: str | None = Query(None, alias="status"),
    disposed: bool | None = None,
    lawyer_id: uuid.UUID | None = None,
    client_id: uuid.UUID | None = None,
    court: str | None = None,
    hearing_from: date | None = None,
    hearing_to: date | None = None,
    sort: str = Query("next_hearing", pattern="^(next_hearing|created|title|last_synced)$"),
    limit: int = Query(50, le=200),
    offset: int = 0,
):
    stmt = visible_cases_query(user)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Case.title.ilike(like), Case.cnr.ilike(like), Case.case_number.ilike(like)))
    if status_:
        stmt = stmt.where(Case.status.ilike(status_))
    if disposed is not None:
        stmt = stmt.where(Case.is_disposed.is_(disposed))
    if lawyer_id:
        assigned = select(CaseAssignment.case_id).where(CaseAssignment.user_id == lawyer_id)
        stmt = stmt.where(or_(Case.lead_lawyer_id == lawyer_id, Case.id.in_(assigned)))
    if client_id and user.role != Role.CLIENT:
        stmt = stmt.where(Case.id.in_(select(ClientCaseAccess.case_id).where(ClientCaseAccess.client_id == client_id)))
    if court:
        stmt = stmt.where(Case.court_name.ilike(f"%{court}%"))
    if hearing_from:
        stmt = stmt.where(Case.next_hearing_date >= hearing_from)
    if hearing_to:
        stmt = stmt.where(Case.next_hearing_date <= hearing_to)
    order = {
        "next_hearing": Case.next_hearing_date.asc().nulls_last(),
        "created": Case.created_at.desc(),
        "title": Case.title.asc(),
        "last_synced": Case.last_synced_at.desc().nulls_last(),
    }[sort]
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(order, Case.id).limit(limit).offset(offset))).scalars().all()
    schema = CaseClientOut if user.role == Role.CLIENT else CaseOut
    return Page[schema](items=[schema.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/{case_id}")
async def get_case(case_id: uuid.UUID, user: TenantUser, db: DB):
    case = await get_visible_case(db, user, case_id)
    if user.role == Role.CLIENT:
        return CaseClientOut.model_validate(case)
    return CaseOut.model_validate(case)


@router.patch("/{case_id}", response_model=CaseOut)
async def update_case(case_id: uuid.UUID, body: CaseUpdate, user: Manager, db: DB, request: Request):
    case = await get_manageable_case(db, user, case_id)
    changes = body.model_dump(exclude_unset=True)
    if "lead_lawyer_id" in changes:
        changes["lead_lawyer_id"] = await _resolve_lead(db, user, changes["lead_lawyer_id"]) if changes["lead_lawyer_id"] else None
    if changes.get("sync_enabled") and not case.cnr:
        raise HTTPException(422, "Sync requires a CNR")
    for f, new in changes.items():
        old = getattr(case, f)
        if f in TRACKED_FIELDS and old != new:
            db.add(
                CaseEvent(
                    tenant_id=case.tenant_id,
                    case_id=case.id,
                    event_type=f"{f}_changed",
                    field=f,
                    old_value=None if old is None else str(old),
                    new_value=None if new is None else str(new),
                    source="manual",
                )
            )
        setattr(case, f, new)
    case.next_sync_at = compute_next_sync(case, datetime.now(UTC))
    await audit(db, user, "case.updated", "case", case.id, {"fields": sorted(changes)}, request)
    await db.flush()
    return case


@router.delete("/{case_id}", status_code=204)
async def delete_case(case_id: uuid.UUID, user: Manager, db: DB, request: Request):
    case = await get_manageable_case(db, user, case_id)
    if user.role != Role.FIRM_ADMIN and case.lead_lawyer_id != user.id:
        raise HTTPException(403, "Only the lead lawyer or a firm admin can delete this case")
    await audit(db, user, "case.deleted", "case", case.id, {"cnr": case.cnr, "title": case.title}, request)
    await db.delete(case)


# ------------------------------------------------------------------ people on a case
@router.get("/{case_id}/assignments", response_model=list[UserOut])
async def list_assignments(case_id: uuid.UUID, user: Staff, db: DB):
    await get_visible_case(db, user, case_id)
    q = select(User).join(CaseAssignment, CaseAssignment.user_id == User.id).where(CaseAssignment.case_id == case_id)
    return (await db.execute(q)).scalars().all()


@router.post("/{case_id}/assignments", response_model=list[UserOut])
async def assign_staff(case_id: uuid.UUID, body: AssignIn, user: Manager, db: DB, request: Request):
    case = await get_manageable_case(db, user, case_id)
    people = await _validate_people(db, user.tenant_id, body.user_ids, clients=False)
    existing = set((await db.execute(select(CaseAssignment.user_id).where(CaseAssignment.case_id == case.id))).scalars().all())
    for p in people:
        if p.id not in existing:
            db.add(CaseAssignment(tenant_id=case.tenant_id, case_id=case.id, user_id=p.id))
    await audit(db, user, "case.staff_assigned", "case", case.id, {"user_ids": [str(p.id) for p in people]}, request)
    await db.flush()
    return await list_assignments(case_id, user, db)


@router.delete("/{case_id}/assignments/{user_id}", status_code=204)
async def unassign_staff(case_id: uuid.UUID, user_id: uuid.UUID, user: Manager, db: DB, request: Request):
    case = await get_manageable_case(db, user, case_id)
    await db.execute(delete(CaseAssignment).where(CaseAssignment.case_id == case.id, CaseAssignment.user_id == user_id))
    await audit(db, user, "case.staff_unassigned", "case", case.id, {"user_id": str(user_id)}, request)


@router.get("/{case_id}/clients", response_model=list[UserOut])
async def list_case_clients(case_id: uuid.UUID, user: Staff, db: DB):
    await get_visible_case(db, user, case_id)
    q = select(User).join(ClientCaseAccess, ClientCaseAccess.client_id == User.id).where(ClientCaseAccess.case_id == case_id)
    return (await db.execute(q)).scalars().all()


@router.post("/{case_id}/clients", response_model=list[UserOut])
async def grant_clients(case_id: uuid.UUID, body: AssignIn, user: Manager, db: DB, request: Request):
    case = await get_manageable_case(db, user, case_id)
    clients = await _validate_people(db, user.tenant_id, body.user_ids, clients=True)
    await _grant_clients(db, case, clients)
    await audit(db, user, "case.client_access_granted", "case", case.id, {"client_ids": [str(c.id) for c in clients]}, request)
    await db.flush()
    return await list_case_clients(case_id, user, db)


@router.delete("/{case_id}/clients/{client_id}", status_code=204)
async def revoke_client(case_id: uuid.UUID, client_id: uuid.UUID, user: Manager, db: DB, request: Request):
    case = await get_manageable_case(db, user, case_id)
    await db.execute(delete(ClientCaseAccess).where(ClientCaseAccess.case_id == case.id, ClientCaseAccess.client_id == client_id))
    await audit(db, user, "case.client_access_revoked", "case", case.id, {"client_id": str(client_id)}, request)


# ------------------------------------------------------------------ sync & history
@router.post("/{case_id}/sync")
async def sync_now(case_id: uuid.UUID, user: Staff, db: DB, request: Request):
    """'Sync Now' (PRD 3.3). Rate-limited per case to protect API credits."""
    case = await get_staff_case(db, user, case_id)
    if not case.cnr:
        raise HTTPException(422, "This case has no CNR to synchronise")
    now = datetime.now(UTC)
    cooldown = timedelta(minutes=settings.SYNC_NOW_COOLDOWN_MINUTES)
    if case.last_manual_sync_at and now - case.last_manual_sync_at < cooldown:
        retry = int((cooldown - (now - case.last_manual_sync_at)).total_seconds())
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, f"Synced recently. Try again in {retry} seconds", headers={"Retry-After": str(retry)}
        )
    case.last_manual_sync_at = now
    run = await sync_case_now(db, case, user, "manual")
    await audit(db, user, "case.synced", "case", case.id, {"status": run.status, "changes": run.changes}, request)
    await db.flush()
    await db.refresh(case)
    return {"run": SyncRunOut.model_validate(run), "case": CaseOut.model_validate(case)}


@router.get("/{case_id}/events", response_model=list[CaseEventOut])
async def case_events(case_id: uuid.UUID, user: TenantUser, db: DB, limit: int = Query(100, le=500)):
    await get_visible_case(db, user, case_id)
    q = select(CaseEvent).where(CaseEvent.case_id == case_id).order_by(CaseEvent.created_at.desc()).limit(limit)
    return (await db.execute(q)).scalars().all()


@router.get("/{case_id}/sync-runs", response_model=list[SyncRunOut])
async def sync_runs(case_id: uuid.UUID, user: Staff, db: DB, limit: int = Query(50, le=200)):
    await get_visible_case(db, user, case_id)
    q = select(SyncRun).where(SyncRun.case_id == case_id).order_by(SyncRun.created_at.desc()).limit(limit)
    return (await db.execute(q)).scalars().all()
