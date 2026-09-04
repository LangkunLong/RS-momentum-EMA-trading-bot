"""Closed immutable contracts for adaptive O'Neil policy interface V3."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, fields, is_dataclass
from datetime import date
from typing import ClassVar

from core.pit_feature_snapshot import EntryFeaturesV3, HoldingFeaturesV3

from .contracts import (
    AllocationSnapshot,
    CapacitySnapshot,
    EntrySnapshot,
    EvictionPosition,
    EvictionSnapshot,
    ExitSnapshot,
    MarketContextV1,
)


_MAX_COLLECTION_ITEMS = 256
_REASON_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def _fail(name: str) -> None:
    raise ValueError(f"{name} is invalid")


def _number(value: object, name: str) -> float:
    if type(value) is bool or not isinstance(value, (int, float)):
        _fail(name)
    number = float(value)
    if not math.isfinite(number):
        _fail(name)
    return number


def _positive(value: object, name: str) -> float:
    number = _number(value, name)
    if number <= 0:
        _fail(name)
    return number


def _unit_fraction(value: object, name: str, *, positive: bool = False) -> float:
    number = _number(value, name)
    if number < 0 or number > 1 or (positive and number == 0):
        _fail(name)
    return number


def _integer(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        _fail(name)
    return value


def _same(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12)


def _validate_session(market: object) -> None:
    if type(market) is not MarketContextV1:
        _fail("market")
    try:
        session = date.fromisoformat(market.session)
    except ValueError as exc:  # MarketContextV1 normally catches this first.
        raise ValueError("market session is invalid") from exc
    if session > date.today():
        raise ValueError("market session must not be in the future")


def _validate_feature(value: object, expected: type[object], name: str) -> None:
    if type(value) is not expected:
        _fail(name)


def _validate_exposures(
    value: object,
    name: str,
) -> tuple[tuple[str, float], ...]:
    if type(value) is not tuple or len(value) > _MAX_COLLECTION_ITEMS:
        _fail(name)
    keys: list[str] = []
    for item in value:
        if type(item) is not tuple or len(item) != 2:
            _fail(name)
        key, fraction = item
        if (
            type(key) is not str
            or not key
            or key.strip() != key
            or len(key) > 128
        ):
            _fail(name)
        _unit_fraction(fraction, name, positive=True)
        keys.append(key)
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        _fail(name)
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("canonical JSON contains a duplicate field")
        result[key] = value
    return result


class _CanonicalContractV3:
    """Strict scalar-only primitive and canonical JSON conversion."""

    __slots__ = ()

    _nested_fields: ClassVar[dict[str, type[object]]] = {}
    _tuple_nested_fields: ClassVar[dict[str, type[object]]] = {}
    _pair_tuple_fields: ClassVar[frozenset[str]] = frozenset()

    def to_primitive(self) -> dict[str, object]:
        def encode(value: object) -> object:
            if hasattr(value, "to_primitive") and callable(value.to_primitive):
                return value.to_primitive()
            if is_dataclass(value) and not isinstance(value, type):
                return {
                    field.name: encode(getattr(value, field.name))
                    for field in fields(value)
                }
            if type(value) is tuple:
                return [encode(item) for item in value]
            if value is None or type(value) in {bool, int, float, str}:
                return value
            raise ValueError("V3 contract contains a non-primitive value")

        return {field.name: encode(getattr(self, field.name)) for field in fields(self)}

    def to_canonical_json(self) -> str:
        return json.dumps(
            self.to_primitive(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    @classmethod
    def from_primitive(cls, primitive: object) -> "_CanonicalContractV3":
        if type(primitive) is not dict:
            raise ValueError("V3 primitive must be an object")
        expected = {field.name for field in fields(cls)}
        actual = set(primitive)
        if actual != expected:
            if actual - expected:
                raise ValueError("V3 primitive has unknown fields")
            raise ValueError("V3 primitive is missing fields")
        values: dict[str, object] = dict(primitive)
        for name, nested_type in cls._nested_fields.items():
            values[name] = _decode_nested(nested_type, values[name], name)
        for name, nested_type in cls._tuple_nested_fields.items():
            raw_items = values[name]
            if type(raw_items) is not list or len(raw_items) > _MAX_COLLECTION_ITEMS:
                _fail(name)
            values[name] = tuple(
                _decode_nested(nested_type, item, name) for item in raw_items
            )
        for name in cls._pair_tuple_fields:
            raw_items = values[name]
            if type(raw_items) is not list or len(raw_items) > _MAX_COLLECTION_ITEMS:
                _fail(name)
            pairs: list[tuple[object, ...]] = []
            for item in raw_items:
                if type(item) is not list:
                    _fail(name)
                pairs.append(tuple(item))
            values[name] = tuple(pairs)
        try:
            return cls(**values)  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("V3 primitive is invalid") from exc

    @classmethod
    def from_canonical_json(cls, raw: str) -> "_CanonicalContractV3":
        if type(raw) is not str:
            raise ValueError("canonical JSON must be a string")
        try:
            primitive = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("canonical JSON is invalid") from exc
        value = cls.from_primitive(primitive)
        if value.to_canonical_json() != raw:
            raise ValueError("canonical JSON encoding is not canonical")
        return value


def _decode_nested(nested_type: type[object], value: object, name: str) -> object:
    if type(value) is not dict:
        _fail(name)
    if issubclass(nested_type, _CanonicalContractV3):
        return nested_type.from_primitive(value)
    if hasattr(nested_type, "from_canonical_json"):
        raw = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        return nested_type.from_canonical_json(raw)  # type: ignore[attr-defined]
    expected = {field.name for field in fields(nested_type)}
    if set(value) != expected:
        if set(value) - expected:
            raise ValueError(f"{name} has unknown fields")
        raise ValueError(f"{name} is missing fields")
    converted = dict(value)
    for field in fields(nested_type):
        raw_item = converted[field.name]
        if field.name == "affiliations":
            if type(raw_item) is not list or any(
                type(item) is not str for item in raw_item
            ):
                _fail(field.name)
            converted[field.name] = tuple(raw_item)
        elif field.name == "fundamental_age_days":
            if raw_item is not None and type(raw_item) is not int:
                _fail(field.name)
        elif raw_item is not None:
            if type(raw_item) not in {int, float}:
                _fail(field.name)
            _number(raw_item, field.name)
    try:
        return nested_type(**converted)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} is invalid") from exc


@dataclass(frozen=True, slots=True)
class EntrySnapshotV3(_CanonicalContractV3):
    """V2 entry facts enriched with causal V3 entry features."""

    base: EntrySnapshot
    features: EntryFeaturesV3

    _nested_fields: ClassVar[dict[str, type[object]]] = {
        "base": EntrySnapshot,
        "features": EntryFeaturesV3,
    }

    def __post_init__(self) -> None:
        if type(self.base) is not EntrySnapshot:
            _fail("base")
        _validate_session(self.base.market)
        _validate_feature(self.features, EntryFeaturesV3, "features")


@dataclass(frozen=True, slots=True)
class PortfolioFeaturesV3(_CanonicalContractV3):
    """Reconciled, long-only portfolio facts available to policy code."""

    gross_exposure_fraction: float
    drawdown_fraction: float
    open_risk_fraction: float
    pending_entry_count: int
    sector_exposures: tuple[tuple[str, float], ...]
    industry_exposures: tuple[tuple[str, float], ...]

    _pair_tuple_fields: ClassVar[frozenset[str]] = frozenset(
        {"sector_exposures", "industry_exposures"}
    )

    def __post_init__(self) -> None:
        gross = _unit_fraction(self.gross_exposure_fraction, "gross_exposure_fraction")
        _unit_fraction(self.drawdown_fraction, "drawdown_fraction")
        risk = _unit_fraction(self.open_risk_fraction, "open_risk_fraction")
        if risk > gross and not _same(risk, gross):
            raise ValueError("open risk exceeds gross exposure")
        _integer(self.pending_entry_count, "pending_entry_count")
        for name in ("sector_exposures", "industry_exposures"):
            exposures = _validate_exposures(getattr(self, name), name)
            total = math.fsum(value for _key, value in exposures)
            if not _same(total, gross):
                raise ValueError(f"{name} do not reconcile to gross exposure")


@dataclass(frozen=True, slots=True)
class CapacitySnapshotV3(_CanonicalContractV3):
    """V2 capacity facts enriched with reconciled portfolio state."""

    base: CapacitySnapshot
    portfolio: PortfolioFeaturesV3

    _nested_fields: ClassVar[dict[str, type[object]]] = {
        "base": CapacitySnapshot,
        "portfolio": PortfolioFeaturesV3,
    }

    def __post_init__(self) -> None:
        if type(self.base) is not CapacitySnapshot:
            _fail("base")
        _validate_session(self.base.market)
        if type(self.portfolio) is not PortfolioFeaturesV3:
            _fail("portfolio")
        expected_cash = 1.0 - self.portfolio.gross_exposure_fraction
        if not _same(float(self.base.cash_fraction), expected_cash):
            raise ValueError("capacity cash fraction does not reconcile")


@dataclass(frozen=True, slots=True)
class AllocationSnapshotV3(_CanonicalContractV3):
    """V2 allocation facts enriched with candidate and portfolio features."""

    base: AllocationSnapshot
    candidate: EntryFeaturesV3
    portfolio: PortfolioFeaturesV3

    _nested_fields: ClassVar[dict[str, type[object]]] = {
        "base": AllocationSnapshot,
        "candidate": EntryFeaturesV3,
        "portfolio": PortfolioFeaturesV3,
    }

    def __post_init__(self) -> None:
        if type(self.base) is not AllocationSnapshot:
            _fail("base")
        _validate_session(self.base.market)
        _validate_feature(self.candidate, EntryFeaturesV3, "candidate")
        if type(self.portfolio) is not PortfolioFeaturesV3:
            _fail("portfolio")
        equity = float(self.base.portfolio_equity_at_entry_open)
        gross_fraction = float(self.base.gross_exposure_before) / equity
        cash_fraction = float(self.base.cash_before_transition) / equity
        if not _same(gross_fraction, self.portfolio.gross_exposure_fraction):
            raise ValueError("allocation gross exposure does not reconcile")
        if not _same(cash_fraction, 1.0 - self.portfolio.gross_exposure_fraction):
            raise ValueError("allocation cash does not reconcile")
        if self.base.pending_entries_remaining > self.portfolio.pending_entry_count:
            raise ValueError("allocation pending entries do not reconcile")


@dataclass(frozen=True, slots=True)
class EvictionPositionV3(_CanonicalContractV3):
    """V2 eviction position enriched with refreshed holding state."""

    base: EvictionPosition
    features: HoldingFeaturesV3
    unrealized_return_fraction: float
    days_held: int
    notional_fraction: float

    _nested_fields: ClassVar[dict[str, type[object]]] = {
        "base": EvictionPosition,
        "features": HoldingFeaturesV3,
    }

    def __post_init__(self) -> None:
        if type(self.base) is not EvictionPosition:
            _fail("base")
        _validate_feature(self.features, HoldingFeaturesV3, "features")
        unrealized = _number(
            self.unrealized_return_fraction, "unrealized_return_fraction"
        )
        _integer(self.days_held, "days_held")
        _unit_fraction(self.notional_fraction, "notional_fraction", positive=True)
        if self.base.causal_execution_price is not None:
            expected = (
                float(self.base.causal_execution_price) / float(self.base.entry_price) - 1.0
            )
            if not _same(unrealized, expected):
                raise ValueError("eviction unrealized return does not reconcile")


@dataclass(frozen=True, slots=True)
class EvictionSnapshotV3(_CanonicalContractV3):
    """V2 eviction facts enriched with candidate, holdings, and portfolio state."""

    base: EvictionSnapshot
    candidate: EntryFeaturesV3
    positions: tuple[EvictionPositionV3, ...]
    portfolio: PortfolioFeaturesV3

    _nested_fields: ClassVar[dict[str, type[object]]] = {
        "base": EvictionSnapshot,
        "candidate": EntryFeaturesV3,
        "portfolio": PortfolioFeaturesV3,
    }
    _tuple_nested_fields: ClassVar[dict[str, type[object]]] = {
        "positions": EvictionPositionV3
    }

    def __post_init__(self) -> None:
        if type(self.base) is not EvictionSnapshot:
            _fail("base")
        _validate_session(self.base.market)
        _validate_feature(self.candidate, EntryFeaturesV3, "candidate")
        if type(self.portfolio) is not PortfolioFeaturesV3:
            _fail("portfolio")
        if type(self.positions) is not tuple or len(self.positions) > _MAX_COLLECTION_ITEMS:
            _fail("positions")
        if any(type(position) is not EvictionPositionV3 for position in self.positions):
            _fail("positions")
        base_by_slot = {position.slot: position for position in self.base.positions}
        if (
            len(base_by_slot) != len(self.positions)
            or {position.base.slot for position in self.positions} != set(base_by_slot)
            or any(
                position.base != base_by_slot[position.base.slot]
                for position in self.positions
            )
        ):
            raise ValueError("eviction positions do not reconcile with base snapshot")
        total = math.fsum(position.notional_fraction for position in self.positions)
        if not _same(total, self.portfolio.gross_exposure_fraction):
            raise ValueError("eviction position notionals do not reconcile")


@dataclass(frozen=True, slots=True)
class AddOnSnapshotV3(_CanonicalContractV3):
    """Trusted completed-session state for an existing-position add-on decision."""

    market: MarketContextV1
    entry_price: float
    current_price: float
    current_quantity: float
    current_notional_fraction: float
    unrealized_return_fraction: float
    days_held: int
    add_on_count: int
    maximum_favorable_excursion_fraction: float
    maximum_adverse_excursion_fraction: float
    remaining_cash_fraction: float
    open_position_risk_fraction: float
    features: HoldingFeaturesV3

    _nested_fields: ClassVar[dict[str, type[object]]] = {
        "market": MarketContextV1,
        "features": HoldingFeaturesV3,
    }

    def __post_init__(self) -> None:
        _validate_session(self.market)
        entry_price = _positive(self.entry_price, "entry_price")
        current_price = _positive(self.current_price, "current_price")
        _positive(self.current_quantity, "current_quantity")
        _unit_fraction(
            self.current_notional_fraction,
            "current_notional_fraction",
            positive=True,
        )
        unrealized = _number(
            self.unrealized_return_fraction, "unrealized_return_fraction"
        )
        _integer(self.days_held, "days_held")
        _integer(self.add_on_count, "add_on_count")
        favorable = _number(
            self.maximum_favorable_excursion_fraction,
            "maximum_favorable_excursion_fraction",
        )
        adverse = _number(
            self.maximum_adverse_excursion_fraction,
            "maximum_adverse_excursion_fraction",
        )
        _unit_fraction(self.remaining_cash_fraction, "remaining_cash_fraction")
        _unit_fraction(
            self.open_position_risk_fraction, "open_position_risk_fraction"
        )
        _validate_feature(self.features, HoldingFeaturesV3, "features")
        expected = current_price / entry_price - 1.0
        if not _same(unrealized, expected):
            raise ValueError("add-on unrealized return does not reconcile")
        if favorable < 0 or favorable < unrealized:
            raise ValueError("add-on favorable excursion does not reconcile")
        if adverse < -1 or adverse > 0 or adverse > unrealized:
            raise ValueError("add-on adverse excursion does not reconcile")
        if (
            self.open_position_risk_fraction > self.current_notional_fraction
            and not _same(
                self.open_position_risk_fraction,
                self.current_notional_fraction,
            )
        ):
            raise ValueError("add-on open risk exceeds position notional")
        deployed = self.current_notional_fraction + self.remaining_cash_fraction
        if deployed > 1 and not _same(deployed, 1.0):
            raise ValueError("add-on cash and notional imply leverage")


@dataclass(frozen=True, slots=True)
class AddOnDecisionV3(_CanonicalContractV3):
    """Pure request to pyramid into an existing winner at a later open."""

    add: bool
    risk_fraction: float
    notional_fraction_cap: float | None
    reason_code: str

    def __post_init__(self) -> None:
        if type(self.add) is not bool:
            _fail("add")
        risk = _unit_fraction(self.risk_fraction, "risk_fraction")
        cap = self.notional_fraction_cap
        if cap is not None:
            _unit_fraction(cap, "notional_fraction_cap", positive=True)
        if type(self.reason_code) is not str or not _REASON_CODE.fullmatch(
            self.reason_code
        ):
            _fail("reason_code")
        if self.add:
            if risk == 0:
                raise ValueError("an add-on requires positive risk")
        elif risk != 0 or cap is not None:
            raise ValueError("a declined add-on must not reserve risk or notional")


@dataclass(frozen=True, slots=True)
class ExitSnapshotV3(_CanonicalContractV3):
    """V2 exit facts enriched with refreshed holding and excursion state."""

    base: ExitSnapshot
    features: HoldingFeaturesV3
    unrealized_return_fraction: float
    maximum_favorable_excursion_fraction: float
    maximum_adverse_excursion_fraction: float
    position_notional_fraction: float

    _nested_fields: ClassVar[dict[str, type[object]]] = {
        "base": ExitSnapshot,
        "features": HoldingFeaturesV3,
    }

    def __post_init__(self) -> None:
        if type(self.base) is not ExitSnapshot:
            _fail("base")
        _validate_session(self.base.market)
        _validate_feature(self.features, HoldingFeaturesV3, "features")
        unrealized = _number(
            self.unrealized_return_fraction, "unrealized_return_fraction"
        )
        favorable = _number(
            self.maximum_favorable_excursion_fraction,
            "maximum_favorable_excursion_fraction",
        )
        adverse = _number(
            self.maximum_adverse_excursion_fraction,
            "maximum_adverse_excursion_fraction",
        )
        _unit_fraction(
            self.position_notional_fraction,
            "position_notional_fraction",
            positive=True,
        )
        expected = float(self.base.current_close) / float(self.base.entry_price) - 1.0
        if not _same(unrealized, expected):
            raise ValueError("exit unrealized return does not reconcile")
        expected_favorable = float(self.base.peak_close) / float(self.base.entry_price) - 1.0
        if not _same(favorable, expected_favorable) or favorable < 0:
            raise ValueError("exit favorable excursion does not reconcile")
        if favorable < unrealized:
            raise ValueError("exit favorable excursion is below current return")
        if adverse < -1 or adverse > 0 or adverse > unrealized:
            raise ValueError("exit adverse excursion does not reconcile")


def validate_add_on_decision(
    snapshot: AddOnSnapshotV3,
    decision: AddOnDecisionV3,
) -> AddOnDecisionV3:
    """Validate a candidate add-on request against trusted position constraints."""
    if type(snapshot) is not AddOnSnapshotV3 or type(decision) is not AddOnDecisionV3:
        raise ValueError("add-on snapshot/decision pairing is invalid")
    canonical = AddOnDecisionV3.from_canonical_json(decision.to_canonical_json())
    if canonical != decision:
        raise ValueError("add-on decision is not canonical")
    if not decision.add:
        return decision
    cap = decision.notional_fraction_cap
    if cap is not None:
        if cap <= snapshot.current_notional_fraction:
            raise ValueError("add-on notional cap must exceed current position notional")
        maximum_funded_cap = (
            snapshot.current_notional_fraction + snapshot.remaining_cash_fraction
        )
        if cap > maximum_funded_cap and not _same(cap, maximum_funded_cap):
            raise ValueError("add-on notional cap exceeds available cash")
    if snapshot.remaining_cash_fraction <= 0:
        raise ValueError("add-on requires remaining cash")
    if snapshot.open_position_risk_fraction + decision.risk_fraction > 1:
        raise ValueError("add-on total risk exceeds equity")
    return decision


__all__ = [
    "AddOnDecisionV3",
    "AddOnSnapshotV3",
    "AllocationSnapshotV3",
    "CapacitySnapshotV3",
    "EntrySnapshotV3",
    "EvictionPositionV3",
    "EvictionSnapshotV3",
    "ExitSnapshotV3",
    "PortfolioFeaturesV3",
    "validate_add_on_decision",
]
