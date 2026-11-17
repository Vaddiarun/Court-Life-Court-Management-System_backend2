from app.core.config import settings
from app.providers.court.base import CourtDataProvider

_override: CourtDataProvider | None = None


def set_court_provider(provider: CourtDataProvider | None) -> None:
    """Used by tests to inject a provider."""
    global _override
    _override = provider


def get_court_provider() -> CourtDataProvider:
    if _override is not None:
        return _override
    if settings.COURT_PROVIDER == "ecourtsindia":
        from app.providers.court.ecourtsindia import ECourtsIndiaProvider

        return ECourtsIndiaProvider()
    from app.providers.court.mock import MockCourtProvider

    return MockCourtProvider()
