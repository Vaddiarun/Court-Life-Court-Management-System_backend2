"""Provider-agnostic court data contract (PRD section 4: the integration must be replaceable)."""

import hashlib
import json
import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import date

CNR_PATTERN = re.compile(r"^[A-Z]{4}\d{12}$")


def normalize_cnr(raw: str) -> str:
    return re.sub(r"[\s\-/]", "", raw or "").upper()


def is_valid_cnr(cnr: str) -> bool:
    return bool(CNR_PATTERN.match(cnr))


class ProviderError(Exception):
    """Transient/unknown provider failure."""


class CaseNotFound(ProviderError):
    pass


class ProviderAuthError(ProviderError):
    pass


class ProviderRateLimited(ProviderError):
    pass


@dataclass
class HearingRecord:
    hearing_date: date
    purpose: str | None = None
    judge: str | None = None
    business: str | None = None


@dataclass
class OrderRecord:
    order_date: date | None
    title: str | None = None
    url: str | None = None


@dataclass
class CaseRecord:
    """Normalised case data every provider adapter must return."""

    cnr: str
    title: str
    case_number: str | None = None
    case_type: str | None = None
    case_year: int | None = None
    court_name: str | None = None
    court_level: str | None = None
    state: str | None = None
    district: str | None = None
    judge: str | None = None
    petitioners: list[str] = field(default_factory=list)
    respondents: list[str] = field(default_factory=list)
    advocates: list[str] = field(default_factory=list)
    filing_date: date | None = None
    registration_date: date | None = None
    status: str | None = None
    stage: str | None = None
    next_hearing_date: date | None = None
    is_disposed: bool = False
    hearings: list[HearingRecord] = field(default_factory=list)
    orders: list[OrderRecord] = field(default_factory=list)
    provider_modified_at: str | None = None
    raw: dict = field(default_factory=dict, repr=False)

    def comparable(self) -> dict:
        """Stable dict used for hashing / diffing (excludes raw payload)."""
        d = asdict(self)
        d.pop("raw", None)
        d.pop("provider_modified_at", None)
        return json.loads(json.dumps(d, default=str, sort_keys=True))

    @classmethod
    def from_dict(cls, d: dict) -> "CaseRecord":
        """Inverse of comparable() (used for the lookup cache)."""

        def _d(v):
            return date.fromisoformat(v) if v else None

        data = dict(d)
        for f in ("filing_date", "registration_date", "next_hearing_date"):
            data[f] = _d(data.get(f))
        data["hearings"] = [
            HearingRecord(_d(h["hearing_date"]), h.get("purpose"), h.get("judge"), h.get("business")) for h in data.get("hearings", [])
        ]
        data["orders"] = [OrderRecord(_d(o.get("order_date")), o.get("title"), o.get("url")) for o in data.get("orders", [])]
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})

    def content_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.comparable(), sort_keys=True).encode()).hexdigest()


class CourtDataProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def get_case(self, cnr: str) -> CaseRecord:
        """Full case detail (billed as 'detail')."""

    @abstractmethod
    async def request_refresh(self, cnr: str) -> None:
        """Ask the provider to re-fetch from the official source (billed as 'refresh')."""

    async def search(self, **filters) -> list[dict]:  # billed as 'search'
        raise NotImplementedError
