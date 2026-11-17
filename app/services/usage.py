"""Metering of third-party consumption (PRD section 8: transparent usage + allowances)."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import ApiUsage

COURT_OPS = ("search", "detail", "refresh", "order_pdf")


def unit_cost(operation: str) -> float:
    return {
        "search": settings.RATE_CASE_SEARCH,
        "detail": settings.RATE_CASE_DETAIL,
        "refresh": settings.RATE_CASE_REFRESH,
        "order_pdf": settings.RATE_ORDER_PDF,
        "call": settings.RATE_CALL_MINUTE,
    }.get(operation, 0.0)


def record_usage(
    db: AsyncSession,
    tenant_id: uuid.UUID | None,
    provider: str,
    operation: str,
    *,
    cnr: str | None = None,
    case_id: uuid.UUID | None = None,
    units: float = 1,
    success: bool = True,
    latency_ms: int | None = None,
) -> None:
    db.add(
        ApiUsage(
            tenant_id=tenant_id,
            provider=provider,
            operation=operation,
            cnr=cnr,
            case_id=case_id,
            units=units,
            unit_cost=unit_cost(operation),
            success=success,
            latency_ms=latency_ms,
        )
    )


def month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1, tzinfo=UTC)
    end = datetime(year + (month == 12), (month % 12) + 1, 1, tzinfo=UTC)
    return start, end


async def usage_summary(db: AsyncSession, tenant_id: uuid.UUID | None, year: int, month: int) -> dict:
    start, end = month_bounds(year, month)
    q = (
        select(
            ApiUsage.provider,
            ApiUsage.operation,
            func.count(ApiUsage.id),
            func.coalesce(func.sum(ApiUsage.units), 0),
            func.coalesce(func.sum(ApiUsage.units * ApiUsage.unit_cost), 0),
            func.count(ApiUsage.id).filter(ApiUsage.success.is_(False)),
        )
        .where(ApiUsage.created_at >= start, ApiUsage.created_at < end)
        .group_by(ApiUsage.provider, ApiUsage.operation)
    )
    if tenant_id is not None:
        q = q.where(ApiUsage.tenant_id == tenant_id)
    rows = (await db.execute(q)).all()
    items = [
        {
            "provider": p,
            "operation": op,
            "requests": int(n),
            "units": float(u),
            "estimated_cost_inr": round(float(c), 2),
            "failures": int(f),
        }
        for p, op, n, u, c, f in rows
    ]
    court_requests = sum(i["requests"] for i in items if i["operation"] in COURT_OPS)
    call_units = sum(i["units"] for i in items if i["operation"] == "call")
    return {
        "year": year,
        "month": month,
        "items": items,
        "court_api_requests": court_requests,
        "call_minutes": call_units,
        "estimated_total_inr": round(sum(i["estimated_cost_inr"] for i in items), 2),
    }
