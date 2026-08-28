"""Behavioral tests for the closed, pure strategy-policy boundary."""

from __future__ import annotations

import json
import math
from dataclasses import FrozenInstanceError, fields
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from core.backtest_engine import PortfolioSimulator
from core.canslim.entry_contract import CanslimEntryFacts
from core.strategy_policy import (
    POLICY_INTERFACE_VERSION,
    AllocationDecision,
    AllocationSnapshot,
    CapacityDecision,
    CapacitySnapshot,
    EntryDecision,
    EntrySnapshot,
    EvictionDecision,
    EvictionPosition,
    EvictionSnapshot,
    ExitAction,
    ExitDecision,
    ExitSnapshot,
    validate_exit_decision,
)
from core.strategy_policy.entry import evaluate_entry
from core.strategy_policy.exit import evaluate_exit
from core.strategy_policy.risk import (
    recommend_allocation,
    recommend_capacity,
    select_eviction,
)
from core.strategy_policy.runtime import InProcessPolicyClient


def _entry_snapshot(**changes: object) -> EntrySnapshot:
    values: dict[str, object] = {
        "technical_only": False,
        "require_proper_base": False,
        "c_score": 0.8,
        "a_score": 0.8,
        "n_score": 0.8,
        "s_score": 0.8,
        "l_score": 0.9,
        "i_score": 0.5,
        "m_score": 0.8,
        "current_growth": 0.30,
        "annual_growth": 0.30,
        "rs_score": 85.0,
        "canslim_score": 80.0,
        "entry_composite_score": 75.0,
        "technical_score": 75.0,
        "institutional_data_available": True,
        "event_close": 105.0,
        "prior_close": 100.0,
        "event_volume": 150.0,
        "prior_average_volume_50": 100.0,
        "pivot": 100.0,
        "volume_ratio": 1.5,
        "extension": 0.05,
        "price_advanced": True,
        "has_volume_surge": True,
        "in_buy_zone": True,
        "technical_eligible": True,
        "technical_blocking_reasons": (),
        "has_power_gap_today": False,
        "require_bullish_market": True,
        "market_is_bullish": True,
        "cash_deployment_override": False,
        "use_stateful_regime_gate": False,
        "regime_allows_entries": True,
        "market_regime": "confirmed_uptrend",
        "distribution_days": 2,
        "follow_through": False,
    }
    values.update(changes)
    return EntrySnapshot(**values)  # type: ignore[arg-type]


def _capacity_snapshot(**changes: object) -> CapacitySnapshot:
    values: dict[str, object] = {
        "configured_max_positions": 4,
        "maximum_policy_positions": 25,
        "open_position_count": 2,
        "eligible_signal_count": 3,
        "cash_fraction": 0.5,
        "configured_eviction_enabled": True,
    }
    values.update(changes)
    return CapacitySnapshot(**values)  # type: ignore[arg-type]


def _allocation_snapshot(**changes: object) -> AllocationSnapshot:
    values: dict[str, object] = {
        "portfolio_equity_at_entry_open": 10_000.0,
        "cash_before_transition": 5_000.0,
        "projected_cash_after_eviction": 5_000.0,
        "gross_exposure_before": 5_000.0,
        "projected_gross_exposure_after_eviction": 5_000.0,
        "entry_open": 100.0,
        "pending_entries_remaining": 2,
        "capacity_is_uncapped": True,
        "configured_position_risk_pct": 0.01,
        "configured_stop_loss_pct": 0.08,
        "maximum_position_risk_fraction": 0.01,
        "maximum_stop_fraction": 0.08,
        "canslim_score": 80.0,
        "rs_score": 85.0,
    }
    values.update(changes)
    return AllocationSnapshot(**values)  # type: ignore[arg-type]


def _eviction_snapshot(**changes: object) -> EvictionSnapshot:
    values: dict[str, object] = {
        "capacity_is_finite": True,
        "capacity_is_full": True,
        "eviction_enabled": True,
        "candidate_rs_score": 90.0,
        "positions": (
            EvictionPosition(0, 100.0, 95.0, 70.0),
            EvictionPosition(1, 100.0, 105.0, 60.0),
        ),
    }
    values.update(changes)
    return EvictionSnapshot(**values)  # type: ignore[arg-type]


def _exit_snapshot(**changes: object) -> ExitSnapshot:
    values: dict[str, object] = {
        "entry_price": 100.0,
        "original_qty": 10.0,
        "remaining_qty": 10.0,
        "stop_price": 92.0,
        "realized_pnl": 0.0,
        "canslim_score": 80.0,
        "rs_score": 85.0,
        "days_held": 5,
        "peak_close": 110.0,
        "breakeven_armed": False,
        "ema_trailing_active": False,
        "scale_out_tier": 0,
        "early_winner_hold": False,
        "current_high": 125.0,
        "current_low": 105.0,
        "current_close": 110.0,
        "history_session_count": 50,
        "ema_today": 106.0,
        "consecutive_closes_below_ema": False,
        "protective_stop_candidates": (92.0, 100.0, 106.0),
        "stop_loss_pct": 0.08,
        "breakeven_trigger_pct": 0.05,
        "ema_period": 21,
        "ema_consecutive": 2,
        "stagnation_days": 20,
        "stagnation_threshold_pct": 0.03,
        "scale_out_tiers": ((0.10, 0.25), (0.20, 0.25)),
        "early_winner_gain_pct": 0.20,
        "early_winner_trigger_days": 15,
        "early_winner_release_days": 40,
    }
    values.update(changes)
    return ExitSnapshot(**values)  # type: ignore[arg-type]


def _entry_facts() -> CanslimEntryFacts:
    return CanslimEntryFacts(
        event_close=102.0,
        prior_close=100.0,
        event_volume=130.0,
        prior_average_volume_50=100.0,
        pivot=100.0,
        volume_ratio=1.3,
        extension=0.02,
        price_advanced=True,
        has_volume_surge=True,
        in_buy_zone=True,
        eligible=True,
        blocking_reasons=(),
    )


class _EntryDecisionClient(InProcessPolicyClient):
    def __init__(self, decision: object) -> None:
        self.decision = decision
        self.snapshots: list[EntrySnapshot] = []

    def evaluate_entry(self, snapshot: EntrySnapshot) -> object:
        self.snapshots.append(snapshot)
        return self.decision


@pytest.mark.parametrize(
    ("technical_only", "decision", "expected"),
    [
        (
            False,
            EntryDecision(False, True, (91.0, 81.0), ("policy_block",)),
            (False, 91.0, 81.0, "policy_block"),
        ),
        (
            True,
            EntryDecision(True, True, (None, None), ()),
            (True, None, None, ""),
        ),
    ],
)
def test_entry_policy_recanonicalizes_full_and_technical_rows(
    technical_only: bool,
    decision: EntryDecision,
    expected: tuple[bool, float | None, float | None, str],
) -> None:
    """Break caught: the engine could retain duplicate inline entry/rank authority."""
    simulator = PortfolioSimulator(technical_only=technical_only)
    client = _EntryDecisionClient(decision)
    simulator._policy_client = client
    facts = _entry_facts()
    row = {
        "current_growth": 0.30,
        "annual_growth": 0.30,
        "rs_score": 85.0,
        "entry_composite_score": 75.0,
        "canslim_score": 80.0,
        "c_score": 0.8,
        "a_score": 0.8,
        "n_score": 0.8,
        "s_score": 0.8,
        "i_score": 0.5,
        "m_score": 0.8,
        "technical_score": 80.0,
        "institutional_data_available": False,
        "has_peg_today": False,
    }
    history = pd.DataFrame(
        {
            "Close": [100.0] * 59 + [102.0],
            "Volume": [100.0] * 59 + [130.0],
        },
        index=pd.bdate_range("2026-01-01", periods=60),
    )

    canonical = simulator._canonicalize_signal_row(
        row=row,
        ticker="AAA",
        ticker_history=history,
        eval_date=history.index[-1],
        market_allowed=True,
        market_state={"market_is_bullish": True},
        entry_facts=facts,
    )

    assert (
        canonical["buy_signal"],
        canonical["canslim_score"],
        canonical["rs_score"],
        canonical["entry_blocking_reasons"],
    ) == expected
    assert len(client.snapshots) == 1
    assert client.snapshots[0].technical_only is technical_only
    assert client.snapshots[0].institutional_data_available is False


@pytest.mark.parametrize(
    "decision",
    [
        {"qualified": True, "market_permitted": True, "rank": [80.0, 90.0], "blocking_codes": [], "date": "2026-01-01"},
        SimpleNamespace(qualified=True, market_permitted=True, rank=(80.0, 90.0), blocking_codes=(), symbol="AAA"),
        SimpleNamespace(qualified=True, market_permitted=True, rank=(80.0, 90.0), blocking_codes=(), fill_price=100.0),
        SimpleNamespace(qualified=True, market_permitted=True, rank=(float("inf"), 90.0), blocking_codes=()),
        SimpleNamespace(qualified=True, market_permitted=True, rank=(80.0, 90.0), blocking_codes=(), unknown=True),
    ],
)
def test_entry_policy_rejects_identity_fill_nonfinite_and_unknown_fields(
    decision: object,
) -> None:
    """Break caught: an injected client could acquire identity or fill authority."""
    simulator = PortfolioSimulator()
    simulator._policy_client = _EntryDecisionClient(decision)
    facts = _entry_facts()
    history = pd.DataFrame(
        {"Close": [100.0] * 59 + [102.0], "Volume": [100.0] * 59 + [130.0]},
        index=pd.bdate_range("2026-01-01", periods=60),
    )
    with pytest.raises(ValueError, match="entry policy decision"):
        simulator._canonicalize_signal_row(
            row={
                "current_growth": 0.30,
                "annual_growth": 0.30,
                "rs_score": 85.0,
                "entry_composite_score": 75.0,
                "canslim_score": 80.0,
            },
            ticker="AAA",
            ticker_history=history,
            eval_date=history.index[-1],
            market_allowed=True,
            market_state={"market_is_bullish": True},
            entry_facts=facts,
        )


@pytest.mark.parametrize(
    ("rank_mode", "ticker_order", "expected_order"),
    [
        ("reverse", ("LOW", "BEST"), ("LOW", "BEST")),
        ("nullable_tie", ("BEST", "LOW"), ("BEST", "LOW")),
    ],
)
def test_policy_rank_uses_valid_nullable_decisions_and_stable_order(
    rank_mode: str,
    ticker_order: tuple[str, str],
    expected_order: tuple[str, str],
) -> None:
    """Break caught: engine sorting could ignore policy ranks or destabilize ties."""

    class Client(InProcessPolicyClient):
        def evaluate_entry(self, snapshot: EntrySnapshot) -> EntryDecision:
            rank = (
                (snapshot.rs_score, snapshot.canslim_score)
                if rank_mode == "reverse"
                else (None, None)
            )
            return EntryDecision(True, True, rank, ())

    history = pd.DataFrame(
        {
            "Open": [100.0] * 60,
            "High": [103.0] * 60,
            "Low": [99.0] * 60,
            "Close": [100.0] * 59 + [102.0],
            "Volume": [100.0] * 59 + [130.0],
        },
        index=pd.bdate_range("2026-01-01", periods=60),
    )
    rows = {
        "LOW": {
            "symbol": "LOW",
            "current_growth": 0.30,
            "annual_growth": 0.30,
            "rs_score": 99.0,
            "entry_composite_score": 75.0,
            "canslim_score": 70.0,
        },
        "BEST": {
            "symbol": "BEST",
            "current_growth": 0.30,
            "annual_growth": 0.30,
            "rs_score": 80.0,
            "entry_composite_score": 75.0,
            "canslim_score": 90.0,
        },
    }
    simulator = PortfolioSimulator()
    simulator._policy_client = Client()
    simulator._regime_tracker = SimpleNamespace(allows_entries=True)
    simulator._ticker_industry = {}
    simulator.strategy = SimpleNamespace(
        evaluate_symbol=lambda **kwargs: rows[kwargs["ticker"]]
    )

    pending = simulator._evaluate_signals(
        tickers=list(ticker_order),
        ticker_ohlcv={ticker: history for ticker in ticker_order},
        all_closes=pd.DataFrame(index=history.index),
        eval_date=history.index[-1],
        market_state={"market_is_bullish": True},
    )

    assert tuple(item.signal["symbol"] for item in pending) == expected_order


def test_entry_decision_is_closed_and_bool_strict() -> None:
    """Break caught: permissive schemas could admit hidden identity or bool coercion."""
    with pytest.raises(TypeError):
        EntryDecision(
            qualified=True,
            market_permitted=True,
            rank=(1.0, 2.0),
            blocking_codes=(),
            symbol="AAPL",
        )
    with pytest.raises(ValueError, match="qualified"):
        EntryDecision(
            qualified=1,  # type: ignore[arg-type]
            market_permitted=True,
            rank=(1.0, 2.0),
            blocking_codes=(),
        )


def test_contract_instances_have_no_mutable_instance_dictionary() -> None:
    """Break caught: inherited instance storage could admit hidden policy fields."""
    contracts = (
        _entry_snapshot(),
        EntryDecision(True, True, (80.0, 85.0), ()),
        _capacity_snapshot(),
        CapacityDecision(4, True),
        _allocation_snapshot(),
        AllocationDecision(0.01, 0.08, None),
        EvictionPosition(0, 100.0, 95.0, 70.0),
        _eviction_snapshot(),
        EvictionDecision(0),
        _exit_snapshot(),
        ExitAction("scale_out", 0.10, 0.25, "take_profit_scale_out"),
        ExitDecision((), 100.0, False, 0, True, True),
    )
    assert all(not hasattr(contract, "__dict__") for contract in contracts)


def test_every_contract_round_trips_canonical_json_and_is_frozen() -> None:
    """Break caught: policy transport could change a validated primitive contract."""
    contracts = (
        _entry_snapshot(),
        EntryDecision(True, True, (80.0, 85.0), ()),
        _capacity_snapshot(),
        CapacityDecision(4, True),
        _allocation_snapshot(),
        AllocationDecision(0.01, 0.08, None),
        EvictionPosition(0, 100.0, 95.0, 70.0),
        _eviction_snapshot(),
        EvictionDecision(0),
        _exit_snapshot(),
        ExitAction("scale_out", 0.10, 0.25, "take_profit_scale_out"),
        ExitDecision((), 100.0, False, 0, True, True),
    )
    for value in contracts:
        encoded = value.to_canonical_json()
        assert encoded == json.dumps(json.loads(encoded), sort_keys=True, separators=(",", ":"), allow_nan=False)
        assert type(value).from_canonical_json(encoded) == value
        first_field = fields(value)[0].name
        with pytest.raises(FrozenInstanceError):
            setattr(value, first_field, getattr(value, first_field))


@pytest.mark.parametrize(
    ("factory", "field", "value"),
    [
        (_entry_snapshot, "technical_blocking_reasons", ["bad"]),
        (_eviction_snapshot, "positions", []),
        (_exit_snapshot, "protective_stop_candidates", [92.0]),
        (_exit_snapshot, "scale_out_tiers", [(0.1, 0.25)]),
    ],
)
def test_contract_collections_must_be_tuples(factory, field: str, value: object) -> None:
    """Break caught: caller mutation could alter a policy fact after validation."""
    with pytest.raises(ValueError, match=field):
        factory(**{field: value})


@pytest.mark.parametrize(
    ("factory", "field", "value"),
    [
        (_entry_snapshot, "market_regime", "moonshot"),
        (_entry_snapshot, "canslim_score", math.nan),
        (_entry_snapshot, "rs_score", math.inf),
        (_capacity_snapshot, "cash_fraction", True),
        (_allocation_snapshot, "entry_open", True),
        (_exit_snapshot, "current_close", math.inf),
    ],
)
def test_contracts_reject_invalid_enums_nonfinite_and_bool_numbers(factory, field: str, value: object) -> None:
    """Break caught: malformed scalar facts could cross the trusted-policy boundary."""
    with pytest.raises(ValueError, match=field):
        factory(**{field: value})


def test_closed_deserializer_rejects_unknown_keys_and_transport_types() -> None:
    """Break caught: JSON transport could smuggle non-contract data into policy code."""
    raw = json.loads(_entry_snapshot().to_canonical_json())
    raw["symbol"] = "AAPL"
    with pytest.raises(ValueError, match="unknown"):
        EntrySnapshot.from_canonical_json(json.dumps(raw))
    for hidden in (date(2026, 8, 27), Path("secret"), lambda: None, {"bad": "mutable"}):
        with pytest.raises(ValueError):
            _entry_snapshot(c_score=hidden)


def test_contracts_reject_oversized_tuples_and_candidate_fill_prices() -> None:
    """Break caught: unbounded or executable-price payloads could escape sandbox limits."""
    with pytest.raises(ValueError, match="technical_blocking_reasons"):
        _entry_snapshot(technical_blocking_reasons=tuple("x" for _ in range(33)))
    with pytest.raises(TypeError):
        ExitAction(
            "close",
            None,
            None,
            "time_stop",
            execution_price=100.0,
        )
    with pytest.raises(ValueError, match="reason"):
        ExitAction("close", None, None, "stop_loss")
    with pytest.raises(ValueError, match="kind"):
        ExitAction("fill", None, None, "policy_exit")


def test_entry_full_mode_requires_canonical_fundamental_gates_and_market_permission() -> None:
    """Break caught: a policy could admit an entry below a fixed CANSLIM floor."""
    rejected = evaluate_entry(_entry_snapshot(current_growth=0.24, market_is_bullish=False))
    assert rejected.qualified is False
    assert rejected.market_permitted is False
    assert rejected.rank == (80.0, 85.0)
    assert rejected.blocking_codes == ("current_growth_below_threshold",)


def test_entry_technical_only_bypasses_fundamentals_and_institutional_unavailability() -> None:
    """Break caught: technical-only mode could accidentally require unavailable fundamentals."""
    decision = evaluate_entry(
        _entry_snapshot(
            technical_only=True,
            institutional_data_available=False,
            current_growth=None,
            annual_growth=None,
            rs_score=None,
            entry_composite_score=None,
            canslim_score=None,
            require_bullish_market=False,
        )
    )
    assert decision.qualified is True
    assert decision.market_permitted is True
    assert decision.rank == (None, None)
    assert decision.blocking_codes == ()


def test_entry_stateful_regime_gate_blocks_only_when_enabled() -> None:
    """Break caught: a disabled regime gate could still suppress valid entries."""
    enabled = evaluate_entry(_entry_snapshot(use_stateful_regime_gate=True, regime_allows_entries=False))
    disabled = evaluate_entry(_entry_snapshot(use_stateful_regime_gate=False, regime_allows_entries=False))
    assert enabled.market_permitted is False
    assert disabled.market_permitted is True


def test_capacity_baseline_preserves_uncapped_and_finite_configuration() -> None:
    """Break caught: baseline policy could turn the uncapped default into a hidden cap."""
    assert recommend_capacity(_capacity_snapshot(configured_max_positions=None)) == CapacityDecision(None, True)
    assert recommend_capacity(_capacity_snapshot(configured_max_positions=4)) == CapacityDecision(4, True)


def test_eviction_prefers_underwater_then_lowest_rs_and_requires_capacity() -> None:
    """Break caught: eviction could remove a stronger position or run when capacity is open."""
    assert select_eviction(_eviction_snapshot()) == EvictionDecision(0)
    assert select_eviction(_eviction_snapshot(capacity_is_full=False)) == EvictionDecision(None)
    assert select_eviction(_eviction_snapshot(positions=(EvictionPosition(3, 100.0, 90.0, 91.0),))) == EvictionDecision(None)


@pytest.mark.parametrize("uncapped", [True, False])
def test_allocation_baseline_preserves_configured_risk_and_stop(uncapped: bool) -> None:
    """Break caught: baseline allocation could change engine-owned sizing inputs."""
    result = recommend_allocation(_allocation_snapshot(capacity_is_uncapped=uncapped))
    assert result == AllocationDecision(0.01, 0.08, None)


def test_exit_scales_all_crossed_tiers_before_ratchet() -> None:
    """Break caught: one bar crossing two targets could leave an eligible tier unplanned."""
    decision = evaluate_exit(_exit_snapshot(current_high=125.0))
    assert decision.actions == (
        ExitAction("scale_out", 0.10, 0.25, "take_profit_scale_out"),
        ExitAction("scale_out", 0.20, 0.25, "take_profit_scale_out"),
    )
    assert decision.next_stop_price == 106.0
    assert decision.scale_out_tier == 2
    assert decision.breakeven_armed is True
    assert decision.ema_trailing_active is True


def test_exit_does_not_scale_an_uncrossed_tier_and_closes_stagnation() -> None:
    """Break caught: exit policy could sell before the trusted high crosses a target."""
    no_cross = evaluate_exit(_exit_snapshot(current_high=109.99))
    assert no_cross.actions == ()
    stagnant = evaluate_exit(
        _exit_snapshot(days_held=20, peak_close=102.0, current_high=102.0, current_close=101.0)
    )
    assert stagnant.actions == (ExitAction("close", None, None, "time_stop"),)


def test_exit_ema_close_is_final_after_scale_outs_and_early_winner_releases() -> None:
    """Break caught: EMA handling could bypass mandatory scale-out ordering or retain holds forever."""
    ema_close = evaluate_exit(_exit_snapshot(consecutive_closes_below_ema=True, current_high=111.0))
    assert ema_close.actions == (
        ExitAction("scale_out", 0.10, 0.25, "take_profit_scale_out"),
        ExitAction("close", None, None, "ma_violation"),
    )
    released = evaluate_exit(_exit_snapshot(early_winner_hold=True, days_held=40))
    assert released.early_winner_hold is False
    assert released.scale_out_tier == 2


def test_exit_decision_validates_action_plan_against_trusted_snapshot() -> None:
    """Break caught: a candidate could return an uncrossed tier or invented ratchet price."""
    snapshot = _exit_snapshot(current_high=109.0)
    invalid_crossing = ExitDecision(
            (ExitAction("scale_out", 0.10, 0.25, "take_profit_scale_out"),),
            None,
            False,
            1,
            False,
            False,
        )
    with pytest.raises(ValueError, match="crossed"):
        validate_exit_decision(snapshot, invalid_crossing)
    invalid_stop = ExitDecision((), 99.0, False, 0, False, False)
    with pytest.raises(ValueError, match="next_stop_price"):
        validate_exit_decision(snapshot, invalid_stop)


def test_interface_version_is_stable() -> None:
    """Break caught: incompatible policy clients could be accepted without a version boundary."""
    assert POLICY_INTERFACE_VERSION == 1
