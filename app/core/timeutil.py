"""Court dates are Indian calendar dates: always derive 'today' in the configured local timezone."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.core.config import settings

LOCAL_TZ = ZoneInfo(settings.TIMEZONE)


def local_date(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(LOCAL_TZ).date()


def today_local() -> date:
    return local_date()
