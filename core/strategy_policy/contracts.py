"""Closed immutable scalar contracts for pure strategy-policy code."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, fields
from typing import Any, ClassVar


_MAX_TUPLE_ITEMS = 32
_MARKET_REGIMES = frozenset({"confirmed_uptrend", "under_pressure", "correction"})
_SCALE_OUT_REASON = "take_profit_scale_out"
_CLOSE_REASONS = frozenset({"time_stop", "ma_violation", "policy_exit"})


def _fail(name: str) -> None:
    raise ValueError(f"{name} is invalid")


def _bool(value: object, name: str) -> None:
    if type(value) is not bool:
        _fail(name)


def _number(value: object, name: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if type(value) is bool or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        _fail(name)


def _positive(value: object, name: str, *, nullable: bool = False) -> None:
    _number(value, name, nullable=nullable)
    if value is not None and float(value) <= 0:
        _fail(name)


def _fraction(value: object, name: str, *, nullable: bool = False) -> None:
    _positive(value, name, nullable=nullable)
    if value is not None and float(value) > 1:
        _fail(name)


def _integer(value: object, name: str, *, minimum: int = 0, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if type(value) is not int or value < minimum:
        _fail(name)


def _string(value: object, name: str) -> None:
    if type(value) is not str or not value or len(value) > 128:
        _fail(name)


def _tuple(value: object, name: str, *, maximum: int = _MAX_TUPLE_ITEMS) -> tuple[Any, ...]:
    if type(value) is not tuple or len(value) > maximum:
        _fail(name)
    return value


class _CanonicalContract:
    """Strict canonical JSON conversion shared by closed dataclass contracts."""

    _nested_fields: ClassVar[dict[str, type["_CanonicalContract"]]] = {}

    def to_primitive(self) -> dict[str, object]:
        def encode(value: object) -> object:
            if isinstance(value, _CanonicalContract):
                return value.to_primitive()
            if type(value) is tuple:
                return [encode(item) for item in value]
            return value

        return {field.name: encode(getattr(self, field.name)) for field in fields(self)}

    def to_canonical_json(self) -> str:
        return json.dumps(self.to_primitive(), sort_keys=True, separators=(",", ":"), allow_nan=False)

    @classmethod
    def from_canonical_json(cls, raw: str) -> "_CanonicalContract":
        if type(raw) is not str:
            raise ValueError("canonical JSON must be a string")
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("canonical JSON is invalid") from exc
        if type(decoded) is not dict:
            raise ValueError("canonical JSON must be an object")
        expected = {field.name for field in fields(cls)}
        actual = set(decoded)
        if actual != expected:
            unknown = actual - expected
            if unknown:
                raise ValueError("canonical JSON has unknown keys")
            raise ValueError("canonical JSON is missing keys")
        values: dict[str, object] = dict(decoded)
        for name, nested_type in cls._nested_fields.items():
            value = values[name]
            if name in {"positions", "actions"}:
                if type(value) is not list:
                    _fail(name)
                values[name] = tuple(
                    nested_type.from_canonical_json(json.dumps(item, sort_keys=True, separators=(",", ":")))
                    for item in value
                )
            elif type(value) is dict:
                values[name] = nested_type.from_canonical_json(
                    json.dumps(value, sort_keys=True, separators=(",", ":"))
                )
        for name, value in tuple(values.items()):
            if type(value) is list:
                if name == "scale_out_tiers":
                    values[name] = tuple(tuple(item) if type(item) is list else item for item in value)
                else:
                    values[name] = tuple(value)
        return cls(**values)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class EntrySnapshot(_CanonicalContract):
    technical_only: bool
    require_proper_base: bool
    c_score: float | None
    a_score: float | None
    n_score: float | None
    s_score: float | None
    l_score: float | None
    i_score: float | None
    m_score: float | None
    current_growth: float | None
    annual_growth: float | None
    rs_score: float | None
    canslim_score: float | None
    entry_composite_score: float | None
    technical_score: float | None
    institutional_data_available: bool
    event_close: float | None
    prior_close: float | None
    event_volume: float | None
    prior_average_volume_50: float | None
    pivot: float | None
    volume_ratio: float | None
    extension: float | None
    price_advanced: bool
    has_volume_surge: bool
    in_buy_zone: bool
    technical_eligible: bool
    technical_blocking_reasons: tuple[str, ...]
    has_power_gap_today: bool
    require_bullish_market: bool
    market_is_bullish: bool
    cash_deployment_override: bool
    use_stateful_regime_gate: bool
    regime_allows_entries: bool
    market_regime: str
    distribution_days: int
    follow_through: bool

    def __post_init__(self) -> None:
        for field in (
            "technical_only", "require_proper_base", "institutional_data_available", "price_advanced",
            "has_volume_surge", "in_buy_zone", "technical_eligible", "has_power_gap_today",
            "require_bullish_market", "market_is_bullish", "cash_deployment_override",
            "use_stateful_regime_gate", "regime_allows_entries", "follow_through",
        ):
            _bool(getattr(self, field), field)
        for field in (
            "c_score", "a_score", "n_score", "s_score", "l_score", "i_score", "m_score",
            "current_growth", "annual_growth", "rs_score", "canslim_score", "entry_composite_score",
            "technical_score", "event_close", "prior_close", "event_volume", "prior_average_volume_50",
            "pivot", "volume_ratio", "extension",
        ):
            _number(getattr(self, field), field, nullable=True)
        _integer(self.distribution_days, "distribution_days")
        if self.market_regime not in _MARKET_REGIMES:
            _fail("market_regime")
        for item in _tuple(self.technical_blocking_reasons, "technical_blocking_reasons"):
            _string(item, "technical_blocking_reasons")


@dataclass(frozen=True, slots=True)
class EntryDecision(_CanonicalContract):
    qualified: bool
    market_permitted: bool
    rank: tuple[float | None, float | None]
    blocking_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        _bool(self.qualified, "qualified")
        _bool(self.market_permitted, "market_permitted")
        rank = _tuple(self.rank, "rank", maximum=2)
        if len(rank) != 2:
            _fail("rank")
        for item in rank:
            _number(item, "rank", nullable=True)
        for item in _tuple(self.blocking_codes, "blocking_codes"):
            _string(item, "blocking_codes")


@dataclass(frozen=True, slots=True)
class CapacitySnapshot(_CanonicalContract):
    configured_max_positions: int | None
    maximum_policy_positions: int
    open_position_count: int
    eligible_signal_count: int
    cash_fraction: float
    configured_eviction_enabled: bool

    def __post_init__(self) -> None:
        _integer(self.maximum_policy_positions, "maximum_policy_positions", minimum=1)
        _integer(self.configured_max_positions, "configured_max_positions", minimum=1, nullable=True)
        if self.configured_max_positions is not None and self.configured_max_positions > self.maximum_policy_positions:
            _fail("configured_max_positions")
        _integer(self.open_position_count, "open_position_count")
        _integer(self.eligible_signal_count, "eligible_signal_count")
        _fraction(self.cash_fraction, "cash_fraction")
        _bool(self.configured_eviction_enabled, "configured_eviction_enabled")


@dataclass(frozen=True, slots=True)
class CapacityDecision(_CanonicalContract):
    max_positions: int | None
    eviction_enabled: bool

    def __post_init__(self) -> None:
        _integer(self.max_positions, "max_positions", minimum=1, nullable=True)
        _bool(self.eviction_enabled, "eviction_enabled")


@dataclass(frozen=True, slots=True)
class AllocationSnapshot(_CanonicalContract):
    portfolio_equity_at_entry_open: float
    cash_before_transition: float
    projected_cash_after_eviction: float
    gross_exposure_before: float
    projected_gross_exposure_after_eviction: float
    entry_open: float
    pending_entries_remaining: int
    capacity_is_uncapped: bool
    configured_position_risk_pct: float
    configured_stop_loss_pct: float
    maximum_position_risk_fraction: float
    maximum_stop_fraction: float
    canslim_score: float | None
    rs_score: float | None

    def __post_init__(self) -> None:
        for field in (
            "portfolio_equity_at_entry_open", "cash_before_transition", "projected_cash_after_eviction",
            "gross_exposure_before", "projected_gross_exposure_after_eviction", "entry_open",
        ):
            _positive(getattr(self, field), field)
        _integer(self.pending_entries_remaining, "pending_entries_remaining", minimum=1)
        _bool(self.capacity_is_uncapped, "capacity_is_uncapped")
        for field in (
            "configured_position_risk_pct", "configured_stop_loss_pct", "maximum_position_risk_fraction",
            "maximum_stop_fraction",
        ):
            _fraction(getattr(self, field), field)
        _number(self.canslim_score, "canslim_score", nullable=True)
        _number(self.rs_score, "rs_score", nullable=True)


@dataclass(frozen=True, slots=True)
class AllocationDecision(_CanonicalContract):
    risk_fraction: float
    stop_distance_fraction: float
    notional_fraction_cap: float | None

    def __post_init__(self) -> None:
        _fraction(self.risk_fraction, "risk_fraction")
        _fraction(self.stop_distance_fraction, "stop_distance_fraction")
        _fraction(self.notional_fraction_cap, "notional_fraction_cap", nullable=True)


@dataclass(frozen=True, slots=True)
class EvictionPosition(_CanonicalContract):
    slot: int
    entry_price: float
    causal_execution_price: float | None
    rs_score: float

    def __post_init__(self) -> None:
        _integer(self.slot, "slot")
        _positive(self.entry_price, "entry_price")
        _positive(self.causal_execution_price, "causal_execution_price", nullable=True)
        _number(self.rs_score, "rs_score")


@dataclass(frozen=True, slots=True)
class EvictionSnapshot(_CanonicalContract):
    capacity_is_finite: bool
    capacity_is_full: bool
    eviction_enabled: bool
    candidate_rs_score: float
    positions: tuple[EvictionPosition, ...]

    _nested_fields: ClassVar[dict[str, type[_CanonicalContract]]] = {"positions": EvictionPosition}

    def __post_init__(self) -> None:
        _bool(self.capacity_is_finite, "capacity_is_finite")
        _bool(self.capacity_is_full, "capacity_is_full")
        _bool(self.eviction_enabled, "eviction_enabled")
        _number(self.candidate_rs_score, "candidate_rs_score")
        slots: set[int] = set()
        for position in _tuple(self.positions, "positions"):
            if type(position) is not EvictionPosition or position.slot in slots:
                _fail("positions")
            slots.add(position.slot)


@dataclass(frozen=True, slots=True)
class EvictionDecision(_CanonicalContract):
    slot: int | None

    def __post_init__(self) -> None:
        _integer(self.slot, "slot", nullable=True)


@dataclass(frozen=True, slots=True)
class ExitSnapshot(_CanonicalContract):
    entry_price: float
    original_qty: float
    remaining_qty: float
    stop_price: float
    realized_pnl: float
    canslim_score: float
    rs_score: float
    days_held: int
    peak_close: float
    breakeven_armed: bool
    ema_trailing_active: bool
    scale_out_tier: int
    early_winner_hold: bool
    current_high: float
    current_low: float
    current_close: float
    history_session_count: int
    ema_today: float | None
    consecutive_closes_below_ema: bool
    protective_stop_candidates: tuple[float, ...]
    stop_loss_pct: float
    breakeven_trigger_pct: float
    ema_period: int
    ema_consecutive: int
    stagnation_days: int
    stagnation_threshold_pct: float
    scale_out_tiers: tuple[tuple[float, float], ...]
    early_winner_gain_pct: float
    early_winner_trigger_days: int
    early_winner_release_days: int

    def __post_init__(self) -> None:
        for field in (
            "entry_price", "original_qty", "remaining_qty", "stop_price", "peak_close", "current_high",
            "current_low", "current_close",
        ):
            _positive(getattr(self, field), field)
        _number(self.realized_pnl, "realized_pnl")
        _number(self.canslim_score, "canslim_score")
        _number(self.rs_score, "rs_score")
        _number(self.ema_today, "ema_today", nullable=True)
        for field in ("days_held", "scale_out_tier", "history_session_count"):
            _integer(getattr(self, field), field)
        for field in ("breakeven_armed", "ema_trailing_active", "early_winner_hold", "consecutive_closes_below_ema"):
            _bool(getattr(self, field), field)
        for field in ("stop_loss_pct", "breakeven_trigger_pct", "stagnation_threshold_pct", "early_winner_gain_pct"):
            _fraction(getattr(self, field), field)
        for field in ("ema_period", "ema_consecutive", "stagnation_days", "early_winner_trigger_days", "early_winner_release_days"):
            _integer(getattr(self, field), field, minimum=1)
        candidates = _tuple(self.protective_stop_candidates, "protective_stop_candidates")
        if not candidates or candidates != tuple(sorted(set(candidates))) or self.stop_price not in candidates:
            _fail("protective_stop_candidates")
        for candidate in candidates:
            _positive(candidate, "protective_stop_candidates")
        tiers = _tuple(self.scale_out_tiers, "scale_out_tiers")
        if self.scale_out_tier > len(tiers):
            _fail("scale_out_tier")
        previous = 0.0
        for tier in tiers:
            if type(tier) is not tuple or len(tier) != 2:
                _fail("scale_out_tiers")
            gain, fraction = tier
            _positive(gain, "scale_out_tiers")
            _fraction(fraction, "scale_out_tiers")
            if float(gain) <= previous:
                _fail("scale_out_tiers")
            previous = float(gain)


@dataclass(frozen=True, slots=True)
class ExitAction(_CanonicalContract):
    kind: str
    trigger_gain_fraction: float | None
    fraction_of_original_quantity: float | None
    reason: str

    def __post_init__(self) -> None:
        if self.kind == "scale_out":
            _positive(self.trigger_gain_fraction, "trigger_gain_fraction")
            _fraction(self.fraction_of_original_quantity, "fraction_of_original_quantity")
            if self.reason != _SCALE_OUT_REASON:
                _fail("reason")
        elif self.kind == "close":
            if self.trigger_gain_fraction is not None or self.fraction_of_original_quantity is not None:
                _fail("close")
            if self.reason not in _CLOSE_REASONS:
                _fail("reason")
        else:
            _fail("kind")


@dataclass(frozen=True, slots=True)
class ExitDecision(_CanonicalContract):
    actions: tuple[ExitAction, ...]
    next_stop_price: float | None
    early_winner_hold: bool
    scale_out_tier: int
    breakeven_armed: bool
    ema_trailing_active: bool

    _nested_fields: ClassVar[dict[str, type[_CanonicalContract]]] = {"actions": ExitAction}

    def __post_init__(self) -> None:
        actions = _tuple(self.actions, "actions")
        saw_close = False
        for action in actions:
            if type(action) is not ExitAction or saw_close:
                _fail("actions")
            saw_close = action.kind == "close"
        _positive(self.next_stop_price, "next_stop_price", nullable=True)
        _bool(self.early_winner_hold, "early_winner_hold")
        _integer(self.scale_out_tier, "scale_out_tier")
        _bool(self.breakeven_armed, "breakeven_armed")
        _bool(self.ema_trailing_active, "ema_trailing_active")


def validate_capacity_decision(snapshot: CapacitySnapshot, decision: CapacityDecision) -> CapacityDecision:
    if type(snapshot) is not CapacitySnapshot or type(decision) is not CapacityDecision:
        raise ValueError("capacity decision contract is invalid")
    if decision.max_positions is not None and decision.max_positions > snapshot.maximum_policy_positions:
        _fail("max_positions")
    return decision


def validate_allocation_decision(snapshot: AllocationSnapshot, decision: AllocationDecision) -> AllocationDecision:
    if type(snapshot) is not AllocationSnapshot or type(decision) is not AllocationDecision:
        raise ValueError("allocation decision contract is invalid")
    if decision.risk_fraction > snapshot.maximum_position_risk_fraction:
        _fail("risk_fraction")
    if decision.stop_distance_fraction > snapshot.maximum_stop_fraction:
        _fail("stop_distance_fraction")
    return decision


def validate_eviction_decision(snapshot: EvictionSnapshot, decision: EvictionDecision) -> EvictionDecision:
    if type(snapshot) is not EvictionSnapshot or type(decision) is not EvictionDecision:
        raise ValueError("eviction decision contract is invalid")
    if decision.slot is not None and decision.slot not in {position.slot for position in snapshot.positions}:
        _fail("slot")
    return decision


def validate_exit_decision(snapshot: ExitSnapshot, decision: ExitDecision) -> ExitDecision:
    if type(snapshot) is not ExitSnapshot or type(decision) is not ExitDecision:
        raise ValueError("exit decision contract is invalid")
    scale_actions = tuple(action for action in decision.actions if action.kind == "scale_out")
    if decision.scale_out_tier != snapshot.scale_out_tier + len(scale_actions):
        _fail("scale_out_tier")
    if decision.scale_out_tier > len(snapshot.scale_out_tiers):
        _fail("scale_out_tier")
    total_fraction = 0.0
    for offset, action in enumerate(scale_actions):
        expected_gain, _configured_fraction = snapshot.scale_out_tiers[snapshot.scale_out_tier + offset]
        if action.trigger_gain_fraction != expected_gain:
            _fail("trigger_gain_fraction")
        if snapshot.current_high < snapshot.entry_price * (1 + expected_gain):
            raise ValueError("scale-out tier was not crossed")
        total_fraction += float(action.fraction_of_original_quantity)
    if total_fraction * snapshot.original_qty > snapshot.remaining_qty + 1e-12:
        _fail("fraction_of_original_quantity")
    if decision.next_stop_price is not None and (
        decision.next_stop_price not in snapshot.protective_stop_candidates
        or decision.next_stop_price < snapshot.stop_price
    ):
        _fail("next_stop_price")
    return decision
