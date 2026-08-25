"""Point-in-time supplemental evidence contracts for diagnosis fact caches."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Protocol

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ZERO_DIGEST = "0" * 64


def _date_or_none(value: str | None, field: str) -> None:
    if value is not None:
        from datetime import date

        try:
            date.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be an ISO date or None") from exc


def _finite_or_none(value: float | None, field: str) -> None:
    if value is not None and (isinstance(value, bool) or not math.isfinite(float(value))):
        raise ValueError(f"{field} must be finite or None")


@dataclass(frozen=True)
class InstitutionalSnapshot:
    """Institutional ownership facts available at one exact as-of date."""

    as_of_date: str | None
    ownership_percent: float | None
    holder_count: int | None
    previous_holder_count: int | None
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _date_or_none(self.as_of_date, "as_of_date")
        _finite_or_none(self.ownership_percent, "ownership_percent")
        for field in ("holder_count", "previous_holder_count"):
            value = getattr(self, field)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{field} must be a non-negative integer or None")
        if not isinstance(self.evidence_ids, tuple) or any(not isinstance(item, str) or not item for item in self.evidence_ids):
            raise ValueError("evidence_ids must be a tuple of non-empty strings")
        values = (self.ownership_percent, self.holder_count, self.previous_holder_count)
        if self.as_of_date is None:
            if any(value is not None for value in values) or self.evidence_ids:
                raise ValueError("unavailable institutional snapshots must not contain evidence")
        elif any(value is None for value in values) or not self.evidence_ids:
            raise ValueError("available institutional snapshots require ownership, holder counts, and evidence")

    @property
    def available(self) -> bool:
        return self.as_of_date is not None


@dataclass(frozen=True)
class IndustryGroupSnapshot:
    """Industry membership/rank facts available at one exact as-of date."""

    as_of_date: str | None
    group_id: str | None
    group_rank: int | None
    group_members: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _date_or_none(self.as_of_date, "as_of_date")
        if self.group_id is not None and (not isinstance(self.group_id, str) or not self.group_id):
            raise ValueError("group_id must be a non-empty string or None")
        if self.group_rank is not None and (type(self.group_rank) is not int or self.group_rank <= 0):
            raise ValueError("group_rank must be a positive integer or None")
        for field in ("group_members", "evidence_ids"):
            values = getattr(self, field)
            if not isinstance(values, tuple) or any(not isinstance(item, str) or not item for item in values):
                raise ValueError(f"{field} must be a tuple of non-empty strings")
        if self.as_of_date is None:
            if self.group_id is not None or self.group_rank is not None or self.group_members or self.evidence_ids:
                raise ValueError("unavailable industry snapshots must not contain evidence")
        elif self.group_id is None or self.group_rank is None or not self.group_members or not self.evidence_ids:
            raise ValueError("available industry snapshots require group membership, rank, and evidence")

    @property
    def available(self) -> bool:
        return self.as_of_date is not None


class SupplementalPITProvider(Protocol):
    """Offline-only supplemental facts with a stable content identity."""

    @property
    def content_identity_sha256(self) -> str: ...

    def institutional_snapshot(self, symbol: str, session: str) -> InstitutionalSnapshot: ...

    def industry_group_snapshot(self, symbol: str, session: str) -> IndustryGroupSnapshot: ...


class UnavailableSupplementalPITProvider:
    """Explicitly records unavailable I/group evidence; it never fills from live data."""

    content_identity_sha256 = _ZERO_DIGEST

    def institutional_snapshot(self, symbol: str, session: str) -> InstitutionalSnapshot:
        del symbol, session
        return InstitutionalSnapshot(None, None, None, None)

    def industry_group_snapshot(self, symbol: str, session: str) -> IndustryGroupSnapshot:
        del symbol, session
        return IndustryGroupSnapshot(None, None, None)
