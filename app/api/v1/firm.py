"""Law Firm Admin: firm profile and member management."""

import uuid

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select

from app.api.deps import DB, FirmAdmin, Manager, Staff, audit, get_tenant_member
from app.core.security import hash_password
from app.models import AuditLog, Tenant, User
from app.models.enums import Role
from app.schemas import AuditOut, FirmUpdate, Page, TenantOut, UserCreate, UserOut, UserUpdate

router = APIRouter(prefix="/firm", tags=["firm"])


@router.get("", response_model=TenantOut)
async def get_firm(user: Staff, db: DB):
    return await db.get(Tenant, user.tenant_id)


@router.patch("", response_model=TenantOut)
async def update_firm(body: FirmUpdate, admin: FirmAdmin, db: DB, request: Request):
    tenant = await db.get(Tenant, admin.tenant_id)
    changes = body.model_dump(exclude_unset=True)
    if "settings" in changes:
        changes["settings"] = {**(tenant.settings or {}), **changes["settings"]}
    for k, v in changes.items():
        setattr(tenant, k, v)
    await audit(db, admin, "firm.updated", "tenant", tenant.id, {"fields": sorted(changes)}, request)
    await db.flush()
    return tenant


@router.post("/users", response_model=UserOut, status_code=201)
async def create_member(body: UserCreate, actor: Manager, db: DB, request: Request):
    # Lawyers may add clients; only firm admins may add staff.
    if actor.role == Role.LAWYER and body.role != Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Lawyers can only add clients")
    from app.api.v1.auth import phone_taken

    if body.email and (await db.execute(select(User.id).where(User.email == body.email.lower()))).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    if await phone_taken(db, body.phone):
        raise HTTPException(status.HTTP_409_CONFLICT, "This mobile number is already registered to another user")
    user = User(
        tenant_id=actor.tenant_id,
        full_name=body.full_name,
        email=body.email.lower() if body.email else None,
        phone=body.phone,
        hashed_password=hash_password(body.password) if body.password else None,
        role=body.role,
        language=body.language,
        bar_council_id=body.bar_council_id,
    )
    db.add(user)
    await db.flush()
    await audit(db, actor, "user.created", "user", user.id, {"role": user.role}, request)
    return user


@router.get("/users", response_model=Page[UserOut])
async def list_members(
    actor: Staff,
    db: DB,
    role: str | None = None,
    q: str | None = None,
    active: bool | None = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
):
    stmt = select(User).where(User.tenant_id == actor.tenant_id)
    if actor.role == Role.JUNIOR_ADVOCATE:
        stmt = stmt.where(User.role != Role.CLIENT)
    if role:
        stmt = stmt.where(User.role == role)
    if q:
        stmt = stmt.where(or_(User.full_name.ilike(f"%{q}%"), User.email.ilike(f"%{q}%"), User.phone.ilike(f"%{q}%")))
    if active is not None:
        stmt = stmt.where(User.is_active.is_(active))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    items = (await db.execute(stmt.order_by(User.full_name).limit(limit).offset(offset))).scalars().all()
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/users/{user_id}", response_model=UserOut)
async def get_member(user_id: uuid.UUID, actor: Staff, db: DB):
    return await get_tenant_member(db, actor.tenant_id, user_id)


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_member(user_id: uuid.UUID, body: UserUpdate, actor: Manager, db: DB, request: Request):
    member = await get_tenant_member(db, actor.tenant_id, user_id)
    if actor.role == Role.LAWYER and member.role != Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Lawyers can only edit clients")
    changes = body.model_dump(exclude_unset=True)
    if "role" in changes and actor.role != Role.FIRM_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only firm admins can change roles")
    if "phone" in changes:
        from app.api.v1.auth import phone_taken

        if not changes["phone"]:
            raise HTTPException(422, "A mobile number is required to sign in")
        if await phone_taken(db, changes["phone"], exclude_id=member.id):
            raise HTTPException(status.HTTP_409_CONFLICT, "This mobile number is already registered to another user")
    if member.id == actor.id and (changes.get("is_active") is False or ("role" in changes and changes["role"] != actor.role)):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot deactivate or demote yourself")
    for k, v in changes.items():
        setattr(member, k, v)
    await audit(db, actor, "user.updated", "user", member.id, {"fields": sorted(changes)}, request)
    await db.flush()
    return member


@router.get("/audit-logs", response_model=Page[AuditOut])
async def firm_audit(
    admin: FirmAdmin, db: DB, action: str | None = None, entity_id: str | None = None, limit: int = Query(50, le=500), offset: int = 0
):
    stmt = select(AuditLog).where(AuditLog.tenant_id == admin.tenant_id)
    if action:
        stmt = stmt.where(AuditLog.action.like(f"{action}%"))
    if entity_id:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    items = (await db.execute(stmt.order_by(AuditLog.created_at.desc()).limit(limit).offset(offset))).scalars().all()
    return Page(items=items, total=total, limit=limit, offset=offset)
