"""In-app notifications, localised per recipient."""

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.i18n import t
from app.models import Case, CaseAssignment, ClientCaseAccess, Notification, User
from app.models.enums import Role


async def notify_users(
    db: AsyncSession,
    user_ids: Iterable[uuid.UUID],
    kind: str,
    title_key: str,
    body_key: str | None,
    params: dict,
    case_id: uuid.UUID | None = None,
) -> int:
    ids = list(dict.fromkeys(user_ids))
    if not ids:
        return 0
    users = (await db.execute(select(User).where(User.id.in_(ids), User.is_active.is_(True)))).scalars().all()
    for u in users:
        if not u.notify_in_app:
            continue
        db.add(
            Notification(
                tenant_id=u.tenant_id,
                user_id=u.id,
                kind=kind,
                title=t(title_key, u.language, **params),
                body=t(body_key, u.language, **params) if body_key else None,
                case_id=case_id,
            )
        )
    return len(users)


async def case_team_ids(db: AsyncSession, case: Case, include_clients: bool = False) -> list[uuid.UUID]:
    """Lead lawyer + assigned staff + firm admins (+ clients)."""
    ids: list[uuid.UUID] = []
    if case.lead_lawyer_id:
        ids.append(case.lead_lawyer_id)
    ids += (await db.execute(select(CaseAssignment.user_id).where(CaseAssignment.case_id == case.id))).scalars().all()
    ids += (
        (await db.execute(select(User.id).where(User.tenant_id == case.tenant_id, User.role == Role.FIRM_ADMIN, User.is_active.is_(True))))
        .scalars()
        .all()
    )
    if include_clients:
        ids += (await db.execute(select(ClientCaseAccess.client_id).where(ClientCaseAccess.case_id == case.id))).scalars().all()
    return list(dict.fromkeys(ids))
