"""Celery worker + beat schedule.

Run:
  celery -A app.workers.celery_app worker -l info
  celery -A app.workers.celery_app beat -l info
"""

import asyncio

from celery import Celery
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.core.db import make_engine

celery = Celery("court_life", broker=settings.REDIS_URL, backend=settings.REDIS_URL)
celery.conf.update(
    timezone=settings.TIMEZONE,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    beat_schedule={
        "request-refreshes": {"task": "sync.request_refreshes", "schedule": 30 * 60},
        "collect-refreshed": {"task": "sync.collect_refreshed", "schedule": 5 * 60},
        "dispatch-notifications": {"task": "notify.dispatch_due", "schedule": 60},
    },
)


def _run(coro_fn):
    async def runner():
        engine = make_engine(null_pool=True)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as db:
                result = await coro_fn(db)
                await db.commit()
                return result
        finally:
            await engine.dispose()

    return asyncio.run(runner())


@celery.task(name="sync.request_refreshes")
def request_refreshes_task():
    from app.services.sync import request_refreshes

    return _run(request_refreshes)


@celery.task(name="sync.collect_refreshed")
def collect_refreshed_task():
    from app.services.sync import collect_refreshed

    return _run(collect_refreshed)


@celery.task(name="notify.dispatch_due")
def dispatch_due_task():
    from app.services.reminders import dispatch_due

    return _run(dispatch_due)
