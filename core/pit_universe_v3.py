"""Closed point-in-time membership domain for the three-universe PIT bundle."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import TYPE_CHECKING, Iterable, Mapping

if TYPE_CHECKING:
    from core.pit_data import PriceIdentityTransitionContract


UNIVERSE_IDS = frozenset({"sp500", "nasdaq100", "russell2000"})

_LINEAGE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_REFERENCE_TICKERS = frozenset({"SPY", "QQQ", "IWM"})


def _as_of_date(value: str | date) -> date:
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("membership date is invalid") from exc
    if type(value) is not date:
        raise ValueError("membership date is invalid")
    return value


def _lineage_id(value: object) -> str:
    if not isinstance(value, str) or _LINEAGE_ID_RE.fullmatch(value) is None:
        raise ValueError("security_lineage_id is not canonical")
    return value


@dataclass(frozen=True, slots=True)
class UniverseMembershipEventV3:
    """One effective-dated affiliation transition for one security lineage."""

    effective_date: date
    security_lineage_id: str
    universe_id: str
    member: bool

    def __post_init__(self) -> None:
        if type(self.effective_date) is not date:
            raise ValueError("effective_date must be a date")
        _lineage_id(self.security_lineage_id)
        if self.universe_id not in UNIVERSE_IDS:
            raise ValueError("universe_id is not in the closed universe domain")
        if type(self.member) is not bool:
            raise ValueError("member must be boolean")


@dataclass(frozen=True, slots=True)
class PointInTimeUniverseV3:
    """Reconstruct a deduplicated lineage union while preserving affiliations."""

    events: tuple[UniverseMembershipEventV3, ...]
    identity_transition_contract: PriceIdentityTransitionContract

    def __post_init__(self) -> None:
        # Import lazily to keep the domain usable by pit_data without a module cycle.
        from core.pit_data import PriceIdentityTransitionContract

        if type(self.events) is not tuple:
            raise ValueError("membership events must be a tuple")
        if type(self.identity_transition_contract) is not PriceIdentityTransitionContract:
            raise ValueError(
                "membership requires the exact authenticated price identity transition contract"
            )

        previous: tuple[date, str, str] | None = None
        seen: set[tuple[date, str, str]] = set()
        active: set[tuple[str, str]] = set()
        membership_lineages: set[str] = set()
        for event in self.events:
            if type(event) is not UniverseMembershipEventV3:
                raise ValueError("membership events have the wrong type")
            key = (
                event.effective_date,
                event.security_lineage_id,
                event.universe_id,
            )
            if key in seen:
                raise ValueError("duplicate membership transition")
            if previous is not None and key <= previous:
                raise ValueError("membership events must be canonical-sorted")
            affiliation = (event.security_lineage_id, event.universe_id)
            if event.member:
                if affiliation in active:
                    raise ValueError("membership addition is already active")
                active.add(affiliation)
            else:
                if affiliation not in active:
                    raise ValueError("membership removal has no active affiliation")
                active.remove(affiliation)
            membership_lineages.add(event.security_lineage_id)
            seen.add(key)
            previous = key

        identities = self.identity_transition_contract.identities
        identity_lineages: dict[str, set[str]] = {}
        for ticker, identity in identities.items():
            lineage = _lineage_id(identity.get("chain_id"))
            identity_lineages.setdefault(lineage, set()).add(ticker)
        missing = membership_lineages.difference(identity_lineages)
        if missing:
            raise ValueError(
                f"membership lineages have no authenticated price identity: {sorted(missing)}"
            )
        tradable_identity_lineages = {
            lineage
            for lineage, tickers in identity_lineages.items()
            if tickers.difference(_REFERENCE_TICKERS)
        }
        if tradable_identity_lineages != membership_lineages:
            raise ValueError("membership lineage bindings and price identities disagree")
        for lineage in membership_lineages:
            if identity_lineages[lineage].intersection(_REFERENCE_TICKERS):
                raise ValueError("market references cannot be membership identities")

        previous_transition: tuple[date, str, str] | None = None
        predecessor_boundaries: set[str] = set()
        successor_boundaries: set[str] = set()
        for transition in self.identity_transition_contract.transitions:
            transition_key = (
                transition.effective_date,
                transition.predecessor,
                transition.successor,
            )
            if previous_transition is not None and transition_key <= previous_transition:
                raise ValueError("price identity transitions must be canonical-sorted")
            if (
                transition.predecessor in predecessor_boundaries
                or transition.successor in successor_boundaries
            ):
                raise ValueError("price identity transition graph is ambiguous")
            predecessor = identities[transition.predecessor]
            successor = identities[transition.successor]
            predecessor_lineage = str(predecessor["chain_id"])
            successor_lineage = str(successor["chain_id"])
            if (
                transition.chain_id != predecessor_lineage
                or transition.chain_id != successor_lineage
                or transition.chain_id not in membership_lineages
            ):
                raise ValueError("price identity transition crosses membership lineages")
            predecessor_boundaries.add(transition.predecessor)
            successor_boundaries.add(transition.successor)
            previous_transition = transition_key

    @classmethod
    def from_rows(
        cls,
        rows: Iterable[Mapping[str, object]],
        identity_transition_contract: PriceIdentityTransitionContract,
    ) -> PointInTimeUniverseV3:
        events: list[UniverseMembershipEventV3] = []
        expected_fields = {
            "effective_date",
            "security_lineage_id",
            "universe_id",
            "member",
        }
        for row in rows:
            if not isinstance(row, Mapping) or set(row) != expected_fields:
                raise ValueError("membership row shape is invalid")
            raw_member = row["member"]
            if type(raw_member) is not bool:
                raise ValueError("membership member must be boolean")
            try:
                effective = date.fromisoformat(str(row["effective_date"]))
            except ValueError as exc:
                raise ValueError("membership effective_date is invalid") from exc
            events.append(
                UniverseMembershipEventV3(
                    effective_date=effective,
                    security_lineage_id=_lineage_id(row["security_lineage_id"]),
                    universe_id=str(row["universe_id"]),
                    member=raw_member,
                )
            )
        return cls(tuple(events), identity_transition_contract)

    def lineage_affiliations_at(
        self, as_of: str | date
    ) -> Mapping[str, frozenset[str]]:
        when = _as_of_date(as_of)
        state: dict[tuple[str, str], bool] = {}
        for event in self.events:
            if event.effective_date > when:
                break
            state[(event.security_lineage_id, event.universe_id)] = event.member
        affiliations: dict[str, set[str]] = {}
        for (lineage, universe_id), member in state.items():
            if member:
                affiliations.setdefault(lineage, set()).add(universe_id)
        return MappingProxyType(
            {
                lineage: frozenset(universe_ids)
                for lineage, universe_ids in sorted(affiliations.items())
            }
        )

    def lineages_at(self, as_of: str | date) -> frozenset[str]:
        return frozenset(self.lineage_affiliations_at(as_of))

    def ticker_for_lineage_at(self, lineage_id: str, as_of: str | date) -> str:
        lineage = _lineage_id(lineage_id)
        if lineage not in self.all_lineage_ids():
            raise ValueError("security lineage is not in membership")
        when = _as_of_date(as_of)
        identities = self.identity_transition_contract.identities
        successors = {
            item.successor: item.effective_date
            for item in self.identity_transition_contract.transitions
            if item.chain_id == lineage
        }
        retired = {
            item.predecessor: item.effective_date
            for item in self.identity_transition_contract.transitions
            if item.chain_id == lineage
        }
        active: list[str] = []
        for ticker, identity in identities.items():
            if identity.get("chain_id") != lineage:
                continue
            admitted_start = date.fromisoformat(str(identity["admitted_start"]))
            admitted_end = date.fromisoformat(str(identity["admitted_end"]))
            if not admitted_start <= when <= admitted_end:
                continue
            if ticker in successors and when < successors[ticker]:
                continue
            if ticker in retired and when >= retired[ticker]:
                continue
            active.append(ticker)
        if len(active) != 1:
            raise ValueError(
                f"security lineage must have exactly one active price identity: {lineage}"
            )
        resolved = self.identity_transition_contract.resolve_open_holding_v3(
            active[0], when
        )
        if resolved != active[0]:
            raise ValueError("active price identity disagrees with its transition contract")
        return resolved

    def affiliations_at(self, as_of: str | date) -> Mapping[str, frozenset[str]]:
        lineage_affiliations = self.lineage_affiliations_at(as_of)
        result: dict[str, frozenset[str]] = {}
        for lineage, universe_ids in lineage_affiliations.items():
            ticker = self.ticker_for_lineage_at(lineage, as_of)
            if ticker in result:
                raise ValueError("active ticker is ambiguous across security lineages")
            result[ticker] = universe_ids
        return MappingProxyType(dict(sorted(result.items())))

    def members_at(self, as_of: str | date) -> frozenset[str]:
        return frozenset(self.affiliations_at(as_of))

    def all_lineage_ids(self) -> frozenset[str]:
        return frozenset(event.security_lineage_id for event in self.events)

    def all_tickers(self) -> frozenset[str]:
        lineages = self.all_lineage_ids()
        return frozenset(
            ticker
            for ticker, identity in self.identity_transition_contract.identities.items()
            if identity.get("chain_id") in lineages
        )
