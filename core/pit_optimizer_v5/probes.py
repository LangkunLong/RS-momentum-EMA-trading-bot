"""Versioned, deterministic semantic probes for optimizer V5 policy clients."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal
from enum import Enum
import hashlib
import json
import math
import re
from typing import Literal

from core.pit_feature_snapshot import EntryFeaturesV3, HoldingFeaturesV3
from core.strategy_policy import POLICY_INTERFACE_VERSION_V3, StrategyPolicyClientV3
from core.strategy_policy.contracts import (
    AllocationDecision,
    AllocationSnapshot,
    BenchmarkContextV1,
    CapacityDecision,
    CapacitySnapshot,
    EntryDecision,
    EntrySnapshot,
    EvictionDecision,
    EvictionPosition,
    EvictionSnapshot,
    ExitDecision,
    ExitSnapshot,
    MarketContextV1,
    validate_allocation_decision,
    validate_capacity_decision,
    validate_eviction_decision,
    validate_exit_decision,
)
from core.strategy_policy.contracts_v3 import (
    AddOnDecisionV3,
    AddOnSnapshotV3,
    AllocationSnapshotV3,
    CapacitySnapshotV3,
    EntrySnapshotV3,
    EvictionPositionV3,
    EvictionSnapshotV3,
    ExitSnapshotV3,
    PortfolioFeaturesV3,
    validate_add_on_decision,
)


PROBE_SUITE_ID_V5 = "pit-policy-v3-probes-v1"
PROBE_METHODS_V5 = (
    "evaluate_entry",
    "recommend_capacity",
    "recommend_allocation",
    "select_eviction",
    "evaluate_add_on",
    "evaluate_exit",
)
BEHAVIORAL_EQUIVALENT_ON_SUITE_V1 = "behavioral_equivalent_on_suite_v1"
BEHAVIORALLY_DISTINCT_ON_SUITE_V1 = "behaviorally_distinct_on_suite_v1"

ProbeSuiteIdV5 = Literal["pit-policy-v3-probes-v1"]
SemanticClassificationV5 = Literal[
    "behavioral_equivalent_on_suite_v1",
    "behaviorally_distinct_on_suite_v1",
]
ProbeOutcomeRoleV5 = Literal[
    "client_interface_lookup",
    "client_interface_value",
    "client_method_lookup",
    "client_method_surface",
    "client_close_lookup",
    "client_close_surface",
    "decision",
    "policy_timeout",
    "policy_execution",
    "decision_protocol",
]

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_PROBE_ID_RE = re.compile(r"[a-z][a-z0-9_]{0,95}\Z")
_MAX_DECISION_JSON_BYTES = 16 * 1024
_PROBE_OUTCOME_ROLES = frozenset(
    {
        "client_interface_lookup",
        "client_interface_value",
        "client_method_lookup",
        "client_method_surface",
        "client_close_lookup",
        "client_close_surface",
        "decision",
        "policy_timeout",
        "policy_execution",
        "decision_protocol",
    }
)

_METHOD_CONTRACTS: dict[str, tuple[type[object], type[object]]] = {
    "evaluate_entry": (EntrySnapshotV3, EntryDecision),
    "recommend_capacity": (CapacitySnapshotV3, CapacityDecision),
    "recommend_allocation": (AllocationSnapshotV3, AllocationDecision),
    "select_eviction": (EvictionSnapshotV3, EvictionDecision),
    "evaluate_add_on": (AddOnSnapshotV3, AddOnDecisionV3),
    "evaluate_exit": (ExitSnapshotV3, ExitDecision),
}


def _digest(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _probe_id(value: object) -> str:
    if type(value) is not str or _PROBE_ID_RE.fullmatch(value) is None:
        raise ValueError("probe ID must be canonical public text")
    return value


def _qualified_type(value_type: type[object]) -> str:
    return f"{value_type.__module__}.{value_type.__qualname__}"


def _lossless_primitive(value: object) -> object:
    """Encode supported Python values without collapsing their concrete types."""

    if value is None:
        return ["none"]
    if isinstance(value, Enum):
        return [
            "enum",
            _qualified_type(type(value)),
            value.name,
            _lossless_primitive(value.value),
        ]
    if type(value) is bool:
        return ["bool", value]
    if type(value) is int:
        return ["int", str(value)]
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("probe canonical value must be finite")
        return ["float", value.hex()]
    if type(value) is Decimal:
        if not value.is_finite():
            raise ValueError("probe canonical decimal must be finite")
        decimal_tuple = value.as_tuple()
        return [
            "decimal",
            decimal_tuple.sign,
            "".join(str(item) for item in decimal_tuple.digits),
            decimal_tuple.exponent,
        ]
    if type(value) is str:
        return ["str", value]
    if type(value) is bytes:
        return ["bytes", value.hex()]
    if is_dataclass(value) and not isinstance(value, type):
        return [
            "dataclass",
            _qualified_type(type(value)),
            [[item.name, _lossless_primitive(getattr(value, item.name))] for item in fields(value)],
        ]
    if type(value) is tuple:
        return ["tuple", [_lossless_primitive(item) for item in value]]
    if type(value) is list:
        return ["list", [_lossless_primitive(item) for item in value]]
    if isinstance(value, Mapping):
        encoded_pairs = [(_lossless_primitive(key), _lossless_primitive(item)) for key, item in value.items()]
        encoded_pairs.sort(key=lambda pair: _canonical_json_bytes(pair[0]))
        rendered_keys = tuple(_canonical_json_bytes(key) for key, _ in encoded_pairs)
        if len(rendered_keys) != len(set(rendered_keys)):
            raise ValueError("probe canonical mapping keys are ambiguous")
        return ["mapping", [[key, item] for key, item in encoded_pairs]]
    raise ValueError("probe canonical value uses an unsupported type")


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("probe value has no canonical JSON encoding") from exc


def canonical_probe_json_v5(value: object) -> bytes:
    """Return the lossless canonical encoding used by V5 probe input identities."""

    return _canonical_json_bytes(_lossless_primitive(value))


class PolicyProbeFailureV5(ValueError):
    """Base class for sanitized, typed semantic-probe failures."""

    failure_code = "policy_probe_failure"

    def __init__(
        self,
        *,
        probe_id: str | None = None,
        method: str | None = None,
        outcome_roles: tuple[ProbeOutcomeRoleV5, ...] = (),
    ) -> None:
        if probe_id is not None:
            _probe_id(probe_id)
        if method is not None and method not in PROBE_METHODS_V5:
            raise ValueError("probe failure method is invalid")
        if (
            type(outcome_roles) is not tuple
            or len(outcome_roles) > 2
            or any(type(item) is not str or item not in _PROBE_OUTCOME_ROLES for item in outcome_roles)
        ):
            raise ValueError("probe failure outcome roles are invalid")
        self.probe_id = probe_id
        self.method = method
        self.outcome_roles = outcome_roles
        super().__init__(self.failure_code)


class PolicyProbeSuiteFailureV5(PolicyProbeFailureV5):
    """The fixed suite or its collected observation set was incomplete."""

    failure_code = "policy_probe_suite_failure"


class PolicyProbeTimeoutV5(PolicyProbeFailureV5):
    """The policy client boundary reported a bounded call timeout."""

    failure_code = "policy_probe_timeout"


class PolicyProbeProtocolFailureV5(PolicyProbeFailureV5):
    """The client surface or one returned decision violated its closed protocol."""

    failure_code = "policy_probe_protocol_failure"


class PolicyProbeExecutionFailureV5(PolicyProbeFailureV5):
    """A policy method deterministically raised instead of returning a decision."""

    failure_code = "policy_probe_execution_failure"


class PolicyProbeExceptionMismatchV5(PolicyProbeFailureV5):
    """Repeated execution disagreed about whether or how the method failed."""

    failure_code = "policy_probe_exception_mismatch"


class PolicyProbeNondeterminismV5(PolicyProbeFailureV5):
    """Repeated execution returned different canonical decisions."""

    failure_code = "candidate_nondeterminism"


@dataclass(frozen=True, slots=True)
class PolicyProbeCaseV5:
    """One immutable input in the fixed V1 semantic probe suite."""

    probe_id: str
    method: str
    snapshot: object

    def __post_init__(self) -> None:
        _probe_id(self.probe_id)
        contracts = _METHOD_CONTRACTS.get(self.method)
        if contracts is None or type(self.snapshot) is not contracts[0]:
            raise ValueError("probe case method and snapshot type differ")

    @property
    def input_sha256(self) -> str:
        return hashlib.sha256(canonical_probe_json_v5(self.snapshot)).hexdigest()


def _market(
    *,
    session: str,
    regime: Literal["confirmed_uptrend", "correction"],
    broad: bool,
) -> MarketContextV1:
    if broad:
        benchmark_values = (
            ("SPY", 0.05, 0.12, 0.14),
            ("QQQ", 0.07, 0.16, 0.18),
            ("IWM", 0.02, 0.06, 0.21),
        )
        breadth_50, breadth_200, median_rs, high_rs = (0.70, 0.60, 82.0, 0.30)
        distribution_days, follow_through = (3, True)
    else:
        benchmark_values = (
            ("SPY", -0.04, -0.08, 0.24),
            ("QQQ", -0.06, -0.12, 0.29),
            ("IWM", -0.08, -0.16, 0.32),
        )
        breadth_50, breadth_200, median_rs, high_rs = (0.30, 0.20, 48.0, 0.10)
        distribution_days, follow_through = (7, False)
    return MarketContextV1(
        schema_version=1,
        session=session,
        oneil_regime=regime,
        distribution_days=distribution_days,
        follow_through=follow_through,
        benchmarks=tuple(
            BenchmarkContextV1(
                symbol=symbol,
                close_to_sma_50_fraction=above_50,
                close_to_sma_200_fraction=above_200,
                realized_volatility_20d_fraction=volatility,
            )
            for symbol, above_50, above_200, volatility in benchmark_values
        ),
        active_constituent_count=100,
        breadth_above_50_fraction=breadth_50,
        breadth_50_coverage_fraction=1.0,
        breadth_above_200_fraction=breadth_200,
        breadth_200_coverage_fraction=1.0,
        median_rs_score=median_rs,
        rs_at_least_80_fraction=high_rs,
        rs_coverage_fraction=1.0,
    )


def _entry_features(*, strong: bool) -> EntryFeaturesV3:
    if strong:
        return EntryFeaturesV3(
            affiliations=("nasdaq100", "sp500"),
            industry_group_rs=91.0,
            sector_rs=None,
            earnings_growth_acceleration=0.10,
            sales_growth_acceleration=0.08,
            fundamental_age_days=30,
            atr_20_fraction=0.03,
            breakout_gap_fraction=0.02,
            average_dollar_volume_50=75_000_000.0,
            distance_from_52_week_high_fraction=-0.01,
        )
    return EntryFeaturesV3(
        affiliations=(),
        industry_group_rs=35.0,
        sector_rs=None,
        earnings_growth_acceleration=None,
        sales_growth_acceleration=None,
        fundamental_age_days=None,
        atr_20_fraction=0.08,
        breakout_gap_fraction=-0.03,
        average_dollar_volume_50=2_000_000.0,
        distance_from_52_week_high_fraction=-0.30,
    )


def _holding_features(*, winner: bool) -> HoldingFeaturesV3:
    return HoldingFeaturesV3(
        current_rs_score=92.0 if winner else 42.0,
        industry_group_rs=88.0 if winner else 38.0,
        atr_20_fraction=0.025 if winner else 0.075,
        volume_ratio=1.50 if winner else 0.65,
    )


def _entry_snapshot(market: MarketContextV1, *, strong: bool) -> EntrySnapshotV3:
    return EntrySnapshotV3(
        base=EntrySnapshot(
            market=market,
            technical_only=False,
            require_proper_base=True,
            c_score=80.0,
            a_score=80.0,
            n_score=70.0,
            s_score=70.0,
            l_score=80.0,
            i_score=70.0,
            m_score=70.0,
            current_growth=0.25,
            annual_growth=0.25,
            rs_score=80.0,
            canslim_score=70.0,
            entry_composite_score=70.0,
            technical_score=70.0,
            institutional_data_available=True,
            event_close=101.0,
            prior_close=99.0,
            event_volume=2_000_000.0,
            prior_average_volume_50=1_000_000.0,
            pivot=100.0,
            volume_ratio=2.0,
            extension=0.01,
            price_advanced=True,
            has_volume_surge=True,
            in_buy_zone=True,
            technical_eligible=True,
            technical_blocking_reasons=(),
            has_power_gap_today=False,
            require_bullish_market=True,
            market_is_bullish=market.oneil_regime == "confirmed_uptrend",
            cash_deployment_override=False,
            use_stateful_regime_gate=True,
            regime_allows_entries=market.oneil_regime != "correction",
        ),
        features=_entry_features(strong=strong),
    )


def _portfolio(
    *,
    gross: float,
    drawdown: float,
    pending: int,
) -> PortfolioFeaturesV3:
    if gross == 0.0:
        sector_exposures: tuple[tuple[str, float], ...] = ()
        industry_exposures: tuple[tuple[str, float], ...] = ()
    else:
        sector_exposures = (("technology", gross),)
        industry_exposures = (("software", gross),)
    return PortfolioFeaturesV3(
        gross_exposure_fraction=gross,
        drawdown_fraction=drawdown,
        open_risk_fraction=min(gross, 0.04),
        pending_entry_count=pending,
        sector_exposures=sector_exposures,
        industry_exposures=industry_exposures,
    )


def _capacity_snapshot(
    market: MarketContextV1,
    *,
    gross: float,
    configured_maximum: int | None,
    open_positions: int,
) -> CapacitySnapshotV3:
    return CapacitySnapshotV3(
        base=CapacitySnapshot(
            market=market,
            configured_max_positions=configured_maximum,
            maximum_policy_positions=10,
            open_position_count=open_positions,
            eligible_signal_count=2,
            cash_fraction=1.0 - gross,
            configured_eviction_enabled=True,
        ),
        portfolio=_portfolio(gross=gross, drawdown=0.0, pending=2),
    )


def _allocation_snapshot(
    market: MarketContextV1,
    *,
    gross: float,
    configured_risk: float,
    configured_stop: float,
    strong: bool,
) -> AllocationSnapshotV3:
    equity = 100_000.0
    return AllocationSnapshotV3(
        base=AllocationSnapshot(
            market=market,
            portfolio_equity_at_entry_open=equity,
            cash_before_transition=equity * (1.0 - gross),
            projected_cash_after_eviction=equity * (1.0 - gross + 0.10),
            gross_exposure_before=equity * gross,
            projected_gross_exposure_after_eviction=equity * (gross - 0.10),
            entry_open=100.0,
            pending_entries_remaining=1,
            capacity_is_uncapped=False,
            configured_position_risk_pct=configured_risk,
            configured_stop_loss_pct=configured_stop,
            maximum_position_risk_fraction=0.02,
            maximum_stop_fraction=0.08,
            canslim_score=70.0,
            rs_score=80.0,
        ),
        candidate=_entry_features(strong=strong),
        portfolio=_portfolio(gross=gross, drawdown=0.05, pending=1),
    )


def _eviction_snapshot(market: MarketContextV1) -> EvictionSnapshotV3:
    losing_base = EvictionPosition(
        slot=0,
        entry_price=100.0,
        causal_execution_price=90.0,
        rs_score=60.0,
    )
    winning_base = EvictionPosition(
        slot=1,
        entry_price=100.0,
        causal_execution_price=120.0,
        rs_score=80.0,
    )
    return EvictionSnapshotV3(
        base=EvictionSnapshot(
            market=market,
            capacity_is_finite=True,
            capacity_is_full=True,
            eviction_enabled=True,
            candidate_rs_score=80.0,
            positions=(losing_base, winning_base),
        ),
        candidate=_entry_features(strong=True),
        positions=(
            EvictionPositionV3(
                base=losing_base,
                features=_holding_features(winner=False),
                unrealized_return_fraction=-0.10,
                days_held=15,
                notional_fraction=0.40,
            ),
            EvictionPositionV3(
                base=winning_base,
                features=_holding_features(winner=True),
                unrealized_return_fraction=0.20,
                days_held=15,
                notional_fraction=0.40,
            ),
        ),
        portfolio=_portfolio(gross=0.80, drawdown=0.10, pending=1),
    )


def _add_on_snapshot(
    market: MarketContextV1,
    *,
    winner: bool,
) -> AddOnSnapshotV3:
    current_price = 102.5 if winner else 93.0
    return AddOnSnapshotV3(
        market=market,
        entry_price=100.0,
        current_price=current_price,
        current_quantity=100.0,
        current_notional_fraction=current_price * 100.0 / 100_000.0,
        unrealized_return_fraction=current_price / 100.0 - 1.0,
        days_held=5 if winner else 15,
        add_on_count=0 if winner else 1,
        maximum_favorable_excursion_fraction=0.025 if winner else 0.05,
        maximum_adverse_excursion_fraction=-0.05 if winner else -0.10,
        remaining_cash_fraction=0.50,
        open_position_risk_fraction=0.005,
        features=_holding_features(winner=winner),
    )


def _exit_snapshot(
    market: MarketContextV1,
    *,
    winner: bool,
) -> ExitSnapshotV3:
    if winner:
        current_high, current_low, current_close, peak_close = (120.0, 98.0, 115.0, 120.0)
        days_held, below_ema, ema_today, lowest_price = (5, False, 110.0, 95.0)
        canslim_score, rs_score = (85.0, 92.0)
    else:
        current_high, current_low, current_close, peak_close = (95.0, 90.0, 92.0, 105.0)
        days_held, below_ema, ema_today, lowest_price = (15, True, 96.0, 90.0)
        canslim_score, rs_score = (45.0, 42.0)
    base = ExitSnapshot(
        market=market,
        entry_price=100.0,
        original_qty=100.0,
        remaining_qty=100.0,
        stop_price=92.0,
        realized_pnl=0.0,
        canslim_score=canslim_score,
        rs_score=rs_score,
        days_held=days_held,
        peak_close=peak_close,
        breakeven_armed=False,
        ema_trailing_active=False,
        scale_out_tier=0,
        early_winner_hold=False,
        current_high=current_high,
        current_low=current_low,
        current_close=current_close,
        history_session_count=50,
        ema_today=ema_today,
        consecutive_closes_below_ema=below_ema,
        protective_stop_candidates=(92.0, 100.0),
        stop_loss_pct=0.08,
        breakeven_trigger_pct=0.10,
        ema_period=21,
        ema_consecutive=2,
        stagnation_days=10,
        stagnation_threshold_pct=0.05,
        scale_out_tiers=((0.20, 0.50), (0.25, 0.50)),
        early_winner_gain_pct=0.20,
        early_winner_trigger_days=3,
        early_winner_release_days=10,
    )
    return ExitSnapshotV3(
        base=base,
        features=_holding_features(winner=winner),
        unrealized_return_fraction=current_close / 100.0 - 1.0,
        maximum_favorable_excursion_fraction=peak_close / 100.0 - 1.0,
        maximum_adverse_excursion_fraction=lowest_price / 100.0 - 1.0,
        position_notional_fraction=current_close * 100.0 / 200_000.0,
    )


def _build_probe_suite_v1() -> tuple[PolicyProbeCaseV5, ...]:
    bull_broad = _market(
        session="2024-06-03",
        regime="confirmed_uptrend",
        broad=True,
    )
    correction_narrow = _market(
        session="2024-06-04",
        regime="correction",
        broad=False,
    )
    return (
        PolicyProbeCaseV5(
            "entry_bull_broad_threshold_boundary",
            "evaluate_entry",
            _entry_snapshot(bull_broad, strong=True),
        ),
        PolicyProbeCaseV5(
            "entry_correction_narrow_threshold_boundary",
            "evaluate_entry",
            _entry_snapshot(correction_narrow, strong=False),
        ),
        PolicyProbeCaseV5(
            "capacity_bull_full_boundary",
            "recommend_capacity",
            _capacity_snapshot(
                bull_broad,
                gross=0.80,
                configured_maximum=10,
                open_positions=10,
            ),
        ),
        PolicyProbeCaseV5(
            "capacity_correction_zero_exposure_boundary",
            "recommend_capacity",
            _capacity_snapshot(
                correction_narrow,
                gross=0.0,
                configured_maximum=None,
                open_positions=0,
            ),
        ),
        PolicyProbeCaseV5(
            "allocation_bull_broad_standard",
            "recommend_allocation",
            _allocation_snapshot(
                bull_broad,
                gross=0.70,
                configured_risk=0.01,
                configured_stop=0.07,
                strong=True,
            ),
        ),
        PolicyProbeCaseV5(
            "allocation_correction_narrow_risk_boundary",
            "recommend_allocation",
            _allocation_snapshot(
                correction_narrow,
                gross=0.50,
                configured_risk=0.02,
                configured_stop=0.08,
                strong=False,
            ),
        ),
        PolicyProbeCaseV5(
            "eviction_winner_loser_rs_boundary",
            "select_eviction",
            _eviction_snapshot(correction_narrow),
        ),
        PolicyProbeCaseV5(
            "add_on_winner_exact_boundary",
            "evaluate_add_on",
            _add_on_snapshot(bull_broad, winner=True),
        ),
        PolicyProbeCaseV5(
            "add_on_loser_holding",
            "evaluate_add_on",
            _add_on_snapshot(correction_narrow, winner=False),
        ),
        PolicyProbeCaseV5(
            "exit_winner_scale_boundary",
            "evaluate_exit",
            _exit_snapshot(bull_broad, winner=True),
        ),
        PolicyProbeCaseV5(
            "exit_loser_ma_boundary",
            "evaluate_exit",
            _exit_snapshot(correction_narrow, winner=False),
        ),
    )


_PROBE_SUITE_V1 = _build_probe_suite_v1()


def policy_probe_suite_v1() -> tuple[PolicyProbeCaseV5, ...]:
    """Return the immutable, canonically ordered V3 suite used by optimizer V5."""

    return _PROBE_SUITE_V1


def _canonical_decision_bytes(method: str, decision: object) -> bytes:
    decision_type = _METHOD_CONTRACTS[method][1]
    if type(decision) is not decision_type:
        raise TypeError("policy probe decision type is invalid")
    try:
        raw = decision.to_canonical_json()  # type: ignore[attr-defined]
        if type(raw) is not str or "\r" in raw or "\n" in raw:
            raise TypeError("policy probe decision JSON is invalid")
        encoded = raw.encode("utf-8")
        if not encoded or len(encoded) > _MAX_DECISION_JSON_BYTES:
            raise TypeError("policy probe decision JSON is invalid")
        canonical = decision_type.from_canonical_json(raw)  # type: ignore[attr-defined]
        if canonical != decision or canonical.to_canonical_json().encode("utf-8") != encoded:  # type: ignore[attr-defined]
            raise TypeError("policy probe decision is not canonical")
    except (AttributeError, TypeError, ValueError, OverflowError, UnicodeError) as exc:
        raise TypeError("policy probe decision protocol is invalid") from exc
    return encoded


def _validate_decision_for_snapshot(case: PolicyProbeCaseV5, decision: object) -> None:
    try:
        if case.method == "recommend_capacity":
            validate_capacity_decision(case.snapshot.base, decision)  # type: ignore[union-attr,arg-type]
        elif case.method == "recommend_allocation":
            validate_allocation_decision(case.snapshot.base, decision)  # type: ignore[union-attr,arg-type]
        elif case.method == "select_eviction":
            validate_eviction_decision(case.snapshot.base, decision)  # type: ignore[union-attr,arg-type]
        elif case.method == "evaluate_add_on":
            validate_add_on_decision(case.snapshot, decision)  # type: ignore[arg-type]
        elif case.method == "evaluate_exit":
            validate_exit_decision(case.snapshot.base, decision)  # type: ignore[union-attr,arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise TypeError("policy probe decision violates its snapshot contract") from exc


@dataclass(frozen=True, slots=True)
class ProbeObservationV5:
    """One canonical decision observed for one exact fixed probe input."""

    probe_id: str
    method: str
    input_sha256: str
    decision_json: bytes

    def __post_init__(self) -> None:
        _probe_id(self.probe_id)
        if self.method not in _METHOD_CONTRACTS:
            raise ValueError("probe observation method is invalid")
        _digest(self.input_sha256, "probe observation input")
        if type(self.decision_json) is not bytes:
            raise ValueError("probe observation decision JSON must be bytes")
        try:
            raw = self.decision_json.decode("utf-8")
            decision_type = _METHOD_CONTRACTS[self.method][1]
            decision = decision_type.from_canonical_json(raw)  # type: ignore[attr-defined]
            canonical = decision.to_canonical_json().encode("utf-8")  # type: ignore[attr-defined]
        except (AttributeError, TypeError, ValueError, OverflowError, UnicodeError) as exc:
            raise ValueError("probe observation decision JSON is invalid") from exc
        if (
            not self.decision_json
            or len(self.decision_json) > _MAX_DECISION_JSON_BYTES
            or canonical != self.decision_json
        ):
            raise ValueError("probe observation decision JSON is not canonical")

    def to_primitive(self) -> dict[str, str]:
        return {
            "probe_id": self.probe_id,
            "method": self.method,
            "input_sha256": self.input_sha256,
            "decision_json_utf8": self.decision_json.decode("utf-8"),
        }


def _fingerprint_sha256(
    suite_id: str,
    observations: tuple[ProbeObservationV5, ...],
) -> str:
    payload = {
        "suite_id": suite_id,
        "observations": [item.to_primitive() for item in observations],
    }
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


@dataclass(frozen=True, slots=True)
class SemanticFingerprintV5:
    """Complete ordered decisions for exactly the versioned V1 probe suite."""

    suite_id: ProbeSuiteIdV5
    observations: tuple[ProbeObservationV5, ...]
    fingerprint_sha256: str

    def __post_init__(self) -> None:
        if type(self.suite_id) is not str or self.suite_id != PROBE_SUITE_ID_V5:
            raise ValueError("semantic fingerprint suite is invalid")
        if type(self.observations) is not tuple or any(
            type(item) is not ProbeObservationV5 for item in self.observations
        ):
            raise ValueError("semantic fingerprint observations are invalid")
        expected_bindings = tuple((case.probe_id, case.method, case.input_sha256) for case in _PROBE_SUITE_V1)
        actual_bindings = tuple((item.probe_id, item.method, item.input_sha256) for item in self.observations)
        if actual_bindings != expected_bindings or len({item.probe_id for item in self.observations}) != len(
            self.observations
        ):
            raise PolicyProbeSuiteFailureV5()
        try:
            for case, observation in zip(
                _PROBE_SUITE_V1,
                self.observations,
                strict=True,
            ):
                decision_type = _METHOD_CONTRACTS[case.method][1]
                decision = decision_type.from_canonical_json(  # type: ignore[attr-defined]
                    observation.decision_json.decode("utf-8")
                )
                _validate_decision_for_snapshot(case, decision)
        except (AttributeError, TypeError, ValueError, OverflowError, UnicodeError) as exc:
            raise ValueError("semantic fingerprint decision violates its fixed probe input") from exc
        _digest(self.fingerprint_sha256, "semantic fingerprint")
        if self.fingerprint_sha256 != _fingerprint_sha256(
            self.suite_id,
            self.observations,
        ):
            raise ValueError("semantic fingerprint digest differs from observations")

    def to_primitive(self) -> dict[str, object]:
        return {
            "suite_id": self.suite_id,
            "observations": tuple(item.to_primitive() for item in self.observations),
            "fingerprint_sha256": self.fingerprint_sha256,
        }


@dataclass(frozen=True, slots=True)
class SemanticFingerprintComparisonV5:
    """A suite-local comparison that deliberately makes no global-equivalence claim."""

    suite_id: ProbeSuiteIdV5
    classification: SemanticClassificationV5
    differing_probe_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.suite_id) is not str or self.suite_id != PROBE_SUITE_ID_V5:
            raise ValueError("semantic comparison suite is invalid")
        if type(self.classification) is not str or self.classification not in {
            BEHAVIORAL_EQUIVALENT_ON_SUITE_V1,
            BEHAVIORALLY_DISTINCT_ON_SUITE_V1,
        }:
            raise ValueError("semantic comparison classification is invalid")
        if type(self.differing_probe_ids) is not tuple or any(
            type(item) is not str for item in self.differing_probe_ids
        ):
            raise ValueError("semantic comparison differences are invalid")
        suite_ids = tuple(case.probe_id for case in _PROBE_SUITE_V1)
        expected_order = tuple(item for item in suite_ids if item in set(self.differing_probe_ids))
        if self.differing_probe_ids != expected_order or len(set(self.differing_probe_ids)) != len(
            self.differing_probe_ids
        ):
            raise ValueError("semantic comparison differences are not canonical")
        equivalent = not self.differing_probe_ids
        if equivalent != (self.classification == BEHAVIORAL_EQUIVALENT_ON_SUITE_V1):
            raise ValueError("semantic comparison classification differs from decisions")


@dataclass(frozen=True, slots=True)
class _CallOutcome:
    role: ProbeOutcomeRoleV5
    decision_json: bytes | None
    exception_class: type[Exception] | None = None

    def __post_init__(self) -> None:
        if type(self.role) is not str or self.role not in _PROBE_OUTCOME_ROLES:
            raise ValueError("probe call outcome role is invalid")
        if self.role == "decision":
            if type(self.decision_json) is not bytes or self.exception_class is not None:
                raise ValueError("successful probe outcome is invalid")
            return
        if self.decision_json is not None:
            raise ValueError("failed probe outcome cannot carry a decision")
        exception_required = self.role in {
            "client_method_lookup",
            "policy_timeout",
            "policy_execution",
            "decision_protocol",
        }
        if exception_required != (self.exception_class is not None):
            raise ValueError("failed probe outcome exception identity is invalid")
        if self.exception_class is not None and (
            not isinstance(self.exception_class, type) or not issubclass(self.exception_class, Exception)
        ):
            raise ValueError("failed probe outcome exception identity is invalid")


def _call_case(client: StrategyPolicyClientV3, case: PolicyProbeCaseV5) -> _CallOutcome:
    try:
        method = getattr(client, case.method)
    except Exception as exc:
        return _CallOutcome("client_method_lookup", None, type(exc))
    if not callable(method):
        return _CallOutcome("client_method_surface", None)

    try:
        decision = method(case.snapshot)
    except TimeoutError as exc:
        return _CallOutcome("policy_timeout", None, type(exc))
    except Exception as exc:
        return _CallOutcome("policy_execution", None, type(exc))

    try:
        decision_json = _canonical_decision_bytes(case.method, decision)
        _validate_decision_for_snapshot(case, decision)
        return _CallOutcome("decision", decision_json)
    except Exception as exc:
        return _CallOutcome("decision_protocol", None, type(exc))


def _validate_client(client: object) -> StrategyPolicyClientV3:
    interface_lookup_failed = False
    try:
        interface_version = client.interface_version  # type: ignore[attr-defined]
    except Exception:
        interface_lookup_failed = True
        interface_version = None
    if interface_lookup_failed:
        raise PolicyProbeProtocolFailureV5(
            outcome_roles=("client_interface_lookup",),
        )
    if type(interface_version) is not int or interface_version != POLICY_INTERFACE_VERSION_V3:
        raise PolicyProbeProtocolFailureV5(
            outcome_roles=("client_interface_value",),
        )
    for member_name in (*PROBE_METHODS_V5, "close"):
        method = member_name if member_name in PROBE_METHODS_V5 else None
        lookup_role: ProbeOutcomeRoleV5 = "client_method_lookup" if method is not None else "client_close_lookup"
        surface_role: ProbeOutcomeRoleV5 = "client_method_surface" if method is not None else "client_close_surface"
        lookup_failed = False
        try:
            member = getattr(client, member_name)
        except Exception:
            lookup_failed = True
            member = None
        if lookup_failed:
            raise PolicyProbeProtocolFailureV5(
                method=method,
                outcome_roles=(lookup_role,),
            )
        if not callable(member):
            raise PolicyProbeProtocolFailureV5(
                method=method,
                outcome_roles=(surface_role,),
            )
    return client  # type: ignore[return-value]


def _execution_schedule() -> tuple[PolicyProbeCaseV5, ...]:
    if len(_PROBE_SUITE_V1) < 3:
        raise PolicyProbeSuiteFailureV5()
    second_pass = _PROBE_SUITE_V1[1:] + _PROBE_SUITE_V1[:1]
    schedule = _PROBE_SUITE_V1 + second_pass
    if any(left.probe_id == right.probe_id for left, right in zip(schedule, schedule[1:], strict=False)):
        raise PolicyProbeSuiteFailureV5()
    return schedule


def _raise_failed_outcome(
    case: PolicyProbeCaseV5,
    first: _CallOutcome,
    second: _CallOutcome,
) -> None:
    outcome_roles = (first.role, second.role)
    signatures = tuple((item.role, item.exception_class) for item in (first, second))
    if signatures[0] != signatures[1]:
        raise PolicyProbeExceptionMismatchV5(
            probe_id=case.probe_id,
            method=case.method,
            outcome_roles=outcome_roles,
        )
    if first.role == "policy_timeout":
        raise PolicyProbeTimeoutV5(
            probe_id=case.probe_id,
            method=case.method,
            outcome_roles=outcome_roles,
        )
    if first.role in {
        "client_method_lookup",
        "client_method_surface",
        "decision_protocol",
    }:
        raise PolicyProbeProtocolFailureV5(
            probe_id=case.probe_id,
            method=case.method,
            outcome_roles=outcome_roles,
        )
    if first.role == "policy_execution":
        raise PolicyProbeExecutionFailureV5(
            probe_id=case.probe_id,
            method=case.method,
            outcome_roles=outcome_roles,
        )
    raise PolicyProbeSuiteFailureV5(
        probe_id=case.probe_id,
        method=case.method,
        outcome_roles=outcome_roles,
    )


def fingerprint_policy_client_v5(
    client: StrategyPolicyClientV3,
) -> SemanticFingerprintV5:
    """Fingerprint one V3 client through twice-run, interleaved fixed probes."""

    validated_client = _validate_client(client)
    outcomes: dict[str, list[_CallOutcome]] = {case.probe_id: [] for case in _PROBE_SUITE_V1}
    for case in _execution_schedule():
        outcomes[case.probe_id].append(_call_case(validated_client, case))
    if tuple(outcomes) != tuple(case.probe_id for case in _PROBE_SUITE_V1) or any(
        len(items) != 2 for items in outcomes.values()
    ):
        raise PolicyProbeSuiteFailureV5()

    observations: list[ProbeObservationV5] = []
    for case in _PROBE_SUITE_V1:
        first, second = outcomes[case.probe_id]
        if first.role != "decision" or second.role != "decision":
            _raise_failed_outcome(case, first, second)
        if first.decision_json != second.decision_json:
            raise PolicyProbeNondeterminismV5(
                probe_id=case.probe_id,
                method=case.method,
                outcome_roles=(first.role, second.role),
            )
        if first.decision_json is None:
            raise PolicyProbeSuiteFailureV5(
                probe_id=case.probe_id,
                method=case.method,
            )
        observations.append(
            ProbeObservationV5(
                probe_id=case.probe_id,
                method=case.method,
                input_sha256=case.input_sha256,
                decision_json=first.decision_json,
            )
        )
    canonical_observations = tuple(observations)
    return SemanticFingerprintV5(
        suite_id=PROBE_SUITE_ID_V5,
        observations=canonical_observations,
        fingerprint_sha256=_fingerprint_sha256(
            PROBE_SUITE_ID_V5,
            canonical_observations,
        ),
    )


def classify_semantic_fingerprints_v5(
    parent: SemanticFingerprintV5,
    candidate: SemanticFingerprintV5,
) -> SemanticFingerprintComparisonV5:
    """Classify only equality or divergence on the exact versioned V1 suite."""

    if type(parent) is not SemanticFingerprintV5 or type(candidate) is not SemanticFingerprintV5:
        raise ValueError("semantic comparison requires V5 fingerprints")
    if parent.suite_id != candidate.suite_id or parent.suite_id != PROBE_SUITE_ID_V5:
        raise ValueError("semantic comparison suites differ")
    differing = tuple(
        parent_observation.probe_id
        for parent_observation, candidate_observation in zip(
            parent.observations,
            candidate.observations,
            strict=True,
        )
        if parent_observation.decision_json != candidate_observation.decision_json
    )
    classification: SemanticClassificationV5 = (
        BEHAVIORAL_EQUIVALENT_ON_SUITE_V1 if not differing else BEHAVIORALLY_DISTINCT_ON_SUITE_V1
    )
    return SemanticFingerprintComparisonV5(
        suite_id=PROBE_SUITE_ID_V5,
        classification=classification,
        differing_probe_ids=differing,
    )


__all__ = [
    "BEHAVIORALLY_DISTINCT_ON_SUITE_V1",
    "BEHAVIORAL_EQUIVALENT_ON_SUITE_V1",
    "PROBE_METHODS_V5",
    "PROBE_SUITE_ID_V5",
    "ProbeOutcomeRoleV5",
    "PolicyProbeCaseV5",
    "PolicyProbeExceptionMismatchV5",
    "PolicyProbeExecutionFailureV5",
    "PolicyProbeFailureV5",
    "PolicyProbeNondeterminismV5",
    "PolicyProbeProtocolFailureV5",
    "PolicyProbeSuiteFailureV5",
    "PolicyProbeTimeoutV5",
    "ProbeObservationV5",
    "SemanticFingerprintComparisonV5",
    "SemanticFingerprintV5",
    "canonical_probe_json_v5",
    "classify_semantic_fingerprints_v5",
    "fingerprint_policy_client_v5",
    "policy_probe_suite_v1",
]
