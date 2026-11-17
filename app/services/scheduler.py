"""In-process background scheduler for single-server / local deployments (no Celery/Redis needed).

Every SCHEDULER_INTERVAL_SECONDS it dispatches due reminders (in-app, email, SMS, calls);
every SYNC_INTERVAL_MINUTES it runs the two-phase court sync. Disable with
RUN_SCHEDULER_IN_PROCESS=false when Celery beat is used instead.
"""

import asyncio
import logging
import time

from app.core.config import settings
from app.core.db import SessionLocal
from app.services.reminders import dispatch_due
from app.services.sync import collect_refreshed, request_refreshes

log = logging.getLogger("courtlife.scheduler")


async def _run(fn, label: str):
    try:
        async with SessionLocal() as db:
            result = await fn(db)
            await db.commit()
        if result and (not isinstance(result, dict) or result.get("processed") or result.get("checked")):
            log.info("%s: %s", label, result)
    except Exception:  # never let the loop die
        log.exception("%s failed", label)


async def scheduler_loop(stop: asyncio.Event) -> None:
    log.info("In-process scheduler started (every %ss)", settings.SCHEDULER_INTERVAL_SECONDS)
    last_sync = 0.0
    while not stop.is_set():
        await _run(dispatch_due, "reminders")
        if time.monotonic() - last_sync >= settings.SYNC_INTERVAL_MINUTES * 60:
            await _run(request_refreshes, "sync.request")
            last_sync = time.monotonic()
        await _run(collect_refreshed, "sync.collect")
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.SCHEDULER_INTERVAL_SECONDS)
        except TimeoutError:
            pass
