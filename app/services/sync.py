"""Case synchronisation: snapshot, diff, change events, alerts (PRD 3.3, 5.1).

Cost-aware design:
  * Cases are refreshed on a priority schedule driven by the next hearing date,
    not all cases every day (the PRD shows Rs 300 vs Rs 16,500 per month for 1,000 cases).
  * Scheduled sync is two-phase because the provider refresh is asynchronous:
      1. request_refreshes(): POST refresh for due cases (cheap 'refresh' unit)
      2. collect_refreshed(): after SYNC_REFRESH_SETTLE_MINUTES read the detail, hash it,
         and only write snapshots / events / alerts when the hash changed.
  * "Sync Now" reads detail immediately and also queues a refresh, subject to a cooldown.
"""

import time as _time
import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.timeutil import local_date
from app.models import Case, CaseEvent, CaseSnapshot, Hearing, SyncRun, User
from app.models.enums import CaseSource, HearingSource
from app.providers.court import get_court_provider
from app.providers.court.base import CaseNotFound, CaseRecord, CourtDataProvider, ProviderError
from app.services.notify import case_team_ids, notify_users
from app.services.reminders import create_auto_hearing_reminder
from app.services.usage import record_usage

TRACKED_FIELDS = (
    "status",
    "stage",
    "next_hearing_date",
    "judge",
    "court_name",
    "case_number",
    "is_disposed",
)
COPY_FIELDS = (
    "title",
    "case_number",
    "case_type",
    "case_year",
    "court_name",
    "court_level",
    "state",
    "district",
    "judge",
    "petitioners",
    "respondents",
    "advocates",
    "filing_date",
    "registration_date",
    "status",
    "stage",
    "next_hearing_date",
    "is_disposed",
)


def compute_next_sync(case: Case, now: datetime) -> datetime | None:
    if not case.sync_enabled or not case.cnr:
        return None
    if case.is_disposed:
        return now + timedelta(days=30)
    nhd = case.next_hearing_date
    if nhd is None:
        return now + timedelta(days=3)
    days = (nhd - local_date(now)).days
    if days <= 0:  # hearing today/past: waiting for the next date to be published
        return now + timedelta(hours=12)
    if days <= 3:
        return now + timedelta(days=1)
    if days <= 30:
        return now + timedelta(days=3)
    return now + timedelta(days=7)


def _fmt(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


async def fetch_record(db: AsyncSession, provider: CourtDataProvider, tenant_id, cnr: str, case_id=None) -> CaseRecord:
    t0 = _time.perf_counter()
    try:
        record = await provider.get_case(cnr)
    except CaseNotFound:
        record_usage(db, tenant_id, provider.name, "detail", cnr=cnr, case_id=case_id, success=False)
        raise
    except ProviderError:
        record_usage(db, tenant_id, provider.name, "detail", cnr=cnr, case_id=case_id, success=False)
        raise
    record_usage(db, tenant_id, provider.name, "detail", cnr=cnr, case_id=case_id, latency_ms=int((_time.perf_counter() - t0) * 1000))
    return record


async def upsert_official_hearings(db: AsyncSession, case: Case, record: CaseRecord) -> int:
    existing = set(
        (await db.execute(select(Hearing.hearing_date).where(Hearing.case_id == case.id, Hearing.source == HearingSource.OFFICIAL)))
        .scalars()
        .all()
    )
    dates = {h.hearing_date: h for h in record.hearings}
    if record.next_hearing_date and record.next_hearing_date not in dates:
        from app.providers.court.base import HearingRecord

        dates[record.next_hearing_date] = HearingRecord(record.next_hearing_date, record.stage, record.judge)
    added = 0
    for d, h in dates.items():
        if d in existing:
            continue
        db.add(
            Hearing(
                tenant_id=case.tenant_id,
                case_id=case.id,
                hearing_date=d,
                purpose=h.purpose,
                judge=h.judge,
                business=h.business,
                source=HearingSource.OFFICIAL,
            )
        )
        added += 1
    return added


def apply_record(case: Case, record: CaseRecord, provider_name: str) -> None:
    for f in COPY_FIELDS:
        val = getattr(record, f)
        if f == "title" and not val:
            continue
        setattr(case, f, val)
    case.cnr = record.cnr
    case.source = CaseSource.PROVIDER
    case.provider_name = provider_name
    case.provider_modified_at = record.provider_modified_at


async def ingest_record(
    db: AsyncSession,
    case: Case,
    record: CaseRecord,
    provider_name: str,
    trigger: str,
    user: User | None = None,
    now: datetime | None = None,
) -> SyncRun:
    """Compare a fetched record with the stored state, persist changes and alert the team."""
    now = now or datetime.now(UTC)
    new_hash = record.content_hash()
    first_sync = case.last_snapshot_hash is None
    changed = new_hash != case.last_snapshot_hash
    events: list[CaseEvent] = []

    if changed:
        prev_orders = 0
        if not first_sync:
            prev_snap = (
                await db.execute(
                    select(CaseSnapshot).where(CaseSnapshot.case_id == case.id).order_by(CaseSnapshot.fetched_at.desc()).limit(1)
                )
            ).scalar_one_or_none()
            prev_orders = len((prev_snap.data or {}).get("orders", [])) if prev_snap else 0
            for f in TRACKED_FIELDS:
                old, new = _fmt(getattr(case, f)), _fmt(getattr(record, f))
                if old != new:
                    events.append(
                        CaseEvent(
                            tenant_id=case.tenant_id,
                            case_id=case.id,
                            event_type=f"{f}_changed",
                            field=f,
                            old_value=old,
                            new_value=new,
                            source=trigger,
                        )
                    )
            if len(record.orders) > prev_orders:
                events.append(
                    CaseEvent(
                        tenant_id=case.tenant_id,
                        case_id=case.id,
                        event_type="new_order",
                        new_value=f"{len(record.orders) - prev_orders} new order(s)",
                        source=trigger,
                    )
                )
        old_next = case.next_hearing_date
        apply_record(case, record, provider_name)
        case.last_snapshot_hash = new_hash
        db.add(
            CaseSnapshot(tenant_id=case.tenant_id, case_id=case.id, provider_name=provider_name, data=record.comparable(), hash=new_hash)
        )
        await upsert_official_hearings(db, case, record)
        for e in events:
            db.add(e)
        if record.next_hearing_date and record.next_hearing_date != old_next and record.next_hearing_date >= local_date(now):
            await db.flush()
            await create_auto_hearing_reminder(db, case, record.next_hearing_date)

    case.last_synced_at = now
    case.last_sync_error = None
    case.next_sync_at = compute_next_sync(case, now)
    run = SyncRun(
        tenant_id=case.tenant_id,
        case_id=case.id,
        trigger=trigger,
        status="success" if changed else "unchanged",
        changes=len(events),
        triggered_by_id=user.id if user else None,
    )
    db.add(run)

    if events:
        summary = "; ".join(
            f"{e.field}: {e.old_value or '-'} -> {e.new_value or '-'}" if e.field else (e.new_value or e.event_type) for e in events
        )[:500]
        recipients = await case_team_ids(db, case, include_clients=False)
        await notify_users(
            db,
            recipients,
            "case_update",
            "case.updated.title",
            "case.updated.body",
            {"case_title": case.title, "count": len(events), "summary": summary},
            case_id=case.id,
        )
    await db.flush()
    return run


async def sync_case_now(db: AsyncSession, case: Case, user: User | None, trigger: str = "manual") -> SyncRun:
    provider = get_court_provider()
    now = datetime.now(UTC)
    try:
        record = await fetch_record(db, provider, case.tenant_id, case.cnr, case.id)
    except ProviderError as e:
        case.last_sync_error = f"{type(e).__name__}: {e}"[:1000]
        case.next_sync_at = now + timedelta(hours=6)
        run = SyncRun(
            tenant_id=case.tenant_id,
            case_id=case.id,
            trigger=trigger,
            status="failed",
            error=case.last_sync_error,
            triggered_by_id=user.id if user else None,
        )
        db.add(run)
        await db.flush()
        return run
    run = await ingest_record(db, case, record, provider.name, trigger, user, now)
    if trigger == "manual":
        try:
            await provider.request_refresh(case.cnr)
            record_usage(db, case.tenant_id, provider.name, "refresh", cnr=case.cnr, case_id=case.id)
            case.refresh_requested_at = now
        except ProviderError:
            record_usage(db, case.tenant_id, provider.name, "refresh", cnr=case.cnr, case_id=case.id, success=False)
    return run


async def request_refreshes(db: AsyncSession, now: datetime | None = None, limit: int | None = None) -> int:
    """Phase 1 of scheduled sync."""
    now = now or datetime.now(UTC)
    provider = get_court_provider()
    cases = (
        (
            await db.execute(
                select(Case)
                .where(
                    Case.sync_enabled.is_(True),
                    Case.cnr.is_not(None),
                    Case.refresh_requested_at.is_(None),
                    or_(Case.next_sync_at.is_(None), Case.next_sync_at <= now),
                )
                .order_by(Case.next_sync_at.asc().nulls_first())
                .limit(limit or settings.SYNC_BATCH_SIZE)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    n = 0
    for case in cases:
        try:
            await provider.request_refresh(case.cnr)
            record_usage(db, case.tenant_id, provider.name, "refresh", cnr=case.cnr, case_id=case.id)
            case.refresh_requested_at = now
            n += 1
        except ProviderError as e:
            record_usage(db, case.tenant_id, provider.name, "refresh", cnr=case.cnr, case_id=case.id, success=False)
            case.last_sync_error = str(e)[:1000]
            case.next_sync_at = now + timedelta(hours=6)
    await db.flush()
    return n


async def collect_refreshed(db: AsyncSession, now: datetime | None = None, limit: int | None = None) -> dict:
    """Phase 2 of scheduled sync: read detail for cases whose refresh has settled."""
    now = now or datetime.now(UTC)
    settle = now - timedelta(minutes=settings.SYNC_REFRESH_SETTLE_MINUTES)
    cases = (
        (
            await db.execute(
                select(Case)
                .where(and_(Case.refresh_requested_at.is_not(None), Case.refresh_requested_at <= settle))
                .limit(limit or settings.SYNC_BATCH_SIZE)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    stats = {"checked": 0, "changed": 0, "failed": 0}
    provider = get_court_provider()
    for case in cases:
        case.refresh_requested_at = None
        stats["checked"] += 1
        try:
            record = await fetch_record(db, provider, case.tenant_id, case.cnr, case.id)
        except ProviderError as e:
            stats["failed"] += 1
            case.last_sync_error = f"{type(e).__name__}: {e}"[:1000]
            case.next_sync_at = now + timedelta(hours=6)
            db.add(SyncRun(tenant_id=case.tenant_id, case_id=case.id, trigger="scheduled", status="failed", error=case.last_sync_error))
            continue
        run = await ingest_record(db, case, record, provider.name, "scheduled", None, now)
        if run.status == "success":
            stats["changed"] += 1
    await db.flush()
    return stats


async def mark_cnr_changed(db: AsyncSession, cnr: str, now: datetime | None = None) -> int:
    """Provider webhook: make every tenant's copy of this CNR eligible for collection."""
    now = now or datetime.now(UTC)
    cases = (await db.execute(select(Case).where(Case.cnr == cnr, Case.sync_enabled.is_(True)))).scalars().all()
    for c in cases:
        c.refresh_requested_at = now - timedelta(minutes=settings.SYNC_REFRESH_SETTLE_MINUTES)
    await db.flush()
    return len(cases)


async def client_ids_for_case(db: AsyncSession, case_id: uuid.UUID) -> list[uuid.UUID]:
    from app.models import ClientCaseAccess

    return list((await db.execute(select(ClientCaseAccess.client_id).where(ClientCaseAccess.case_id == case_id))).scalars().all())
