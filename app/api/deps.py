"""Auth, tenant context and row-level access helpers."""

import uuid
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.core.db import get_db
from app.core.security import decode_token
from app.models import AuditLog, Case, CaseAssignment, ClientCaseAccess, User
from app.models.enums import Role

bearer = HTTPBearer(auto_error=False)

DB = Annotated[AsyncSession, Depends(get_db)]


async def get_current_user(db: DB, creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> User:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        payload = decode_token(creds.credentials, "access")
        user_id = uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token") from None
    user = await db.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User inactive or not found")
    if user.tenant is not None and user.tenant.status != "active":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Law firm account is suspended")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: Role):
    allowed = {r.value for r in roles}

    async def checker(user: CurrentUser) -> User:
        if user.role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
        return user

    return checker


SuperAdmin = Annotated[User, Depends(require_roles(Role.SUPER_ADMIN))]
FirmAdmin = Annotated[User, Depends(require_roles(Role.FIRM_ADMIN))]
Manager = Annotated[User, Depends(require_roles(Role.FIRM_ADMIN, Role.LAWYER))]
Staff = Annotated[User, Depends(require_roles(Role.FIRM_ADMIN, Role.LAWYER, Role.JUNIOR_ADVOCATE))]
TenantUser = Annotated[User, Depends(require_roles(Role.FIRM_ADMIN, Role.LAWYER, Role.JUNIOR_ADVOCATE, Role.CLIENT))]


def is_manager(user: User) -> bool:
    return user.role in (Role.FIRM_ADMIN, Role.LAWYER)


def visible_cases_query(user: User) -> Select:
    """Cases a user may see: managers -> whole firm; juniors -> assigned; clients -> granted."""
    q = select(Case).where(Case.tenant_id == user.tenant_id)
    if user.role == Role.JUNIOR_ADVOCATE:
        assigned = select(CaseAssignment.case_id).where(CaseAssignment.user_id == user.id)
        q = q.where(or_(Case.id.in_(assigned), Case.lead_lawyer_id == user.id))
    elif user.role == Role.CLIENT:
        granted = select(ClientCaseAccess.case_id).where(ClientCaseAccess.client_id == user.id)
        q = q.where(Case.id.in_(granted))
    elif not is_manager(user):
        q = q.where(False)
    return q


async def get_visible_case(db: AsyncSession, user: User, case_id: uuid.UUID) -> Case:
    case = (await db.execute(visible_cases_query(user).where(Case.id == case_id))).scalar_one_or_none()
    if case is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Case not found")
    return case


async def get_manageable_case(db: AsyncSession, user: User, case_id: uuid.UUID) -> Case:
    if not is_manager(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only lawyers or firm admins can modify this case")
    return await get_visible_case(db, user, case_id)


async def get_staff_case(db: AsyncSession, user: User, case_id: uuid.UUID) -> Case:
    """Staff (incl. assigned juniors) may contribute notes/tasks/docs/reminders."""
    if user.role == Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Clients cannot modify case data")
    return await get_visible_case(db, user, case_id)


async def get_tenant_member(db: AsyncSession, tenant_id: uuid.UUID | None, user_id: uuid.UUID) -> User:
    member = await db.get(User, user_id)
    if member is None or member.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found in this firm")
    return member


async def audit(
    db: AsyncSession,
    actor: User | None,
    action: str,
    entity_type: str | None = None,
    entity_id: object | None = None,
    details: dict | None = None,
    request: Request | None = None,
    tenant_id: uuid.UUID | None = None,
) -> None:
    db.add(
        AuditLog(
            tenant_id=tenant_id if tenant_id is not None else (actor.tenant_id if actor else None),
            actor_id=actor.id if actor else None,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id is not None else None,
            details=details or {},
            ip_address=request.client.host if request and request.client else None,
        )
    )
