"""Super Admin: tenants, platform users, reports, audit logs, usage."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import func, select

from app.api.deps import DB, SuperAdmin, audit
from app.core.security import hash_password
from app.models import ApiUsage, AuditLog, Case, NotificationJob, SyncRun, Tenant, User
from app.models.enums import Role
from app.schemas import AuditOut, Page, TenantIn, TenantOut, TenantUpdate, UserOut
from app.services.usage import usage_summary

router = APIRouter(prefix="/admin", tags=["super-admin"])


@router.post("/tenants", response_model=TenantOut, status_code=201)
async def create_tenant(body: TenantIn, admin: SuperAdmin, db: DB, request: Request):
    if (await db.execute(select(Tenant.id).where(Tenant.slug == body.slug))).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "Slug already in use")
    from app.api.v1.auth import phone_taken

    if body.admin_email and (await db.execute(select(User.id).where(User.email == body.admin_email.lower()))).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "Admin email already registered")
    if await phone_taken(db, body.admin_phone):
        raise HTTPException(status.HTTP_409_CONFLICT, "Admin mobile number already registered")
    data = body.model_dump(exclude={"admin_email", "admin_name", "admin_password", "admin_phone"})
    tenant = Tenant(**data, settings={})
    db.add(tenant)
    await db.flush()
    db.add(
        User(
            tenant_id=tenant.id,
            email=body.admin_email.lower() if body.admin_email else None,
            phone=body.admin_phone,
            full_name=body.admin_name,
            hashed_password=hash_password(body.admin_password) if body.admin_password else None,
            role=Role.FIRM_ADMIN,
            language=body.default_language,
        )
    )
    await audit(db, admin, "tenant.created", "tenant", tenant.id, {"slug": tenant.slug}, request, tenant_id=tenant.id)
    await db.flush()
    return tenant


@router.get("/tenants", response_model=Page[TenantOut])
async def list_tenants(
    admin: SuperAdmin,
    db: DB,
    q: str | None = None,
    status_: str | None = Query(None, alias="status"),
    limit: int = Query(50, le=200),
    offset: int = 0,
):
    stmt = select(Tenant)
    if q:
        stmt = stmt.where(Tenant.name.ilike(f"%{q}%") | Tenant.slug.ilike(f"%{q}%"))
    if status_:
        stmt = stmt.where(Tenant.status == status_)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    items = (await db.execute(stmt.order_by(Tenant.created_at.desc()).limit(limit).offset(offset))).scalars().all()
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/tenants/{tenant_id}", response_model=TenantOut)
async def get_tenant(tenant_id: uuid.UUID, admin: SuperAdmin, db: DB):
    tenant = await db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    return tenant


@router.patch("/tenants/{tenant_id}", response_model=TenantOut)
async def update_tenant(tenant_id: uuid.UUID, body: TenantUpdate, admin: SuperAdmin, db: DB, request: Request):
    tenant = await db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    changes = body.model_dump(exclude_unset=True)
    for k, v in changes.items():
        setattr(tenant, k, v)
    await audit(db, admin, "tenant.updated", "tenant", tenant.id, {"fields": sorted(changes)}, request, tenant_id=tenant.id)
    await db.flush()
    return tenant


@router.get("/tenants/{tenant_id}/users", response_model=list[UserOut])
async def tenant_users(tenant_id: uuid.UUID, admin: SuperAdmin, db: DB):
    return (await db.execute(select(User).where(User.tenant_id == tenant_id).order_by(User.created_at))).scalars().all()


@router.get("/reports/overview")
async def platform_overview(admin: SuperAdmin, db: DB):
    now = datetime.now(UTC)
    tenants_by_status = dict((await db.execute(select(Tenant.status, func.count()).group_by(Tenant.status))).all())
    tenants_by_plan = dict((await db.execute(select(Tenant.plan, func.count()).group_by(Tenant.plan))).all())
    users_by_role = dict((await db.execute(select(User.role, func.count()).group_by(User.role))).all())
    total_cases = (await db.execute(select(func.count(Case.id)))).scalar_one()
    sync_stats = dict((await db.execute(select(SyncRun.status, func.count()).group_by(SyncRun.status))).all())
    provider = (
        await db.execute(
            select(
                ApiUsage.provider,
                func.count(ApiUsage.id),
                func.count(ApiUsage.id).filter(ApiUsage.success.is_(False)),
                func.avg(ApiUsage.latency_ms),
            ).group_by(ApiUsage.provider)
        )
    ).all()
    jobs = dict((await db.execute(select(NotificationJob.status, func.count()).group_by(NotificationJob.status))).all())
    expiring = (
        await db.execute(
            select(func.count(Tenant.id)).where(Tenant.subscription_ends_at.is_not(None), Tenant.subscription_ends_at <= now.date())
        )
    ).scalar_one()
    return {
        "tenants_by_status": tenants_by_status,
        "tenants_by_plan": tenants_by_plan,
        "subscriptions_expired": expiring,
        "users_by_role": users_by_role,
        "total_cases": total_cases,
        "sync_runs_by_status": sync_stats,
        "notification_jobs_by_status": jobs,
        "integrations": [
            {
                "provider": p,
                "requests": n,
                "failures": f,
                "success_rate": round(1 - f / n, 4) if n else None,
                "avg_latency_ms": round(float(lat), 1) if lat is not None else None,
            }
            for p, n, f, lat in provider
        ],
    }


@router.get("/usage")
async def platform_usage(
    admin: SuperAdmin, db: DB, year: int | None = None, month: int | None = Query(None, ge=1, le=12), tenant_id: uuid.UUID | None = None
):
    now = datetime.now(UTC)
    return await usage_summary(db, tenant_id, year or now.year, month or now.month)


@router.get("/audit-logs", response_model=Page[AuditOut])
async def audit_logs(
    admin: SuperAdmin,
    db: DB,
    tenant_id: uuid.UUID | None = None,
    action: str | None = None,
    limit: int = Query(50, le=500),
    offset: int = 0,
):
    stmt = select(AuditLog)
    if tenant_id:
        stmt = stmt.where(AuditLog.tenant_id == tenant_id)
    if action:
        stmt = stmt.where(AuditLog.action.like(f"{action}%"))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    items = (await db.execute(stmt.order_by(AuditLog.created_at.desc()).limit(limit).offset(offset))).scalars().all()
    return Page(items=items, total=total, limit=limit, offset=offset)
