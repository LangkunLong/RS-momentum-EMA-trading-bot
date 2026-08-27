"""Formal regression contracts for Task 11 effective-engine policy."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json

import pandas as pd
import pytest

from core.backtest_engine import CanslimStrategy, PortfolioSimulator
from core.engine_policy import (
    PolicyClassification,
    effective_engine_policy_sha256,
)


_POLICY_SECTIONS = (
    "causal_invariants",
    "entry_policy",
    "scoring_policy",
    "market_policy",
    "capacity_and_sizing_policy",
    "exit_policy",
    "unsupported_requests",
)
_EXPECTED_POLICY_SCHEMAS = {
    "causal_invariants": {
        "cash_and_no_leverage_constraint",
        "entry_execution_timing",
        "next_open_buy_zone_revalidation",
        "technical_fact_cutoff",
    },
    "entry_policy": {
        "industry_group_filter_enabled",
        "industry_group_filter_mode",
        "industry_group_min_size",
        "industry_group_top_n",
        "max_buy_zone_extension",
        "min_annual_growth",
        "min_current_growth",
        "min_entry_composite_score",
        "min_rs_score",
        "min_volume_ratio",
        "proper_base_required",
        "signal_every_n_days",
        "technical_facts",
        "technical_only",
    },
    "scoring_policy": {
        "a_consistency_weight",
        "a_growth_target",
        "a_growth_weight",
        "a_min_years_growth",
        "a_roe_target",
        "a_roe_weight",
        "annual_earnings_metric_family_priority",
        "attestation_scope",
        "c_acceleration_weight",
        "c_consistency_weight",
        "c_growth_target",
        "c_growth_weight",
        "component_weight_a",
        "component_weight_c",
        "component_weight_i",
        "component_weight_l",
        "component_weight_m",
        "component_weight_n",
        "component_weight_s",
        "current_earnings_metric_family_priority",
        "fundamental_source_attestation",
        "fundamental_usage",
        "fundamental_visibility",
        "i_level_weight",
        "i_ownership_level_rule",
        "i_ownership_trend_rule",
        "i_trend_weight",
        "institutional_data_reweighting",
        "n_growth_score_rule",
        "n_proximity_score_rule",
        "n_proximity_to_high_weight",
        "n_revenue_growth_target",
        "n_revenue_growth_weight",
        "non_m_entry_composite",
        "owned_strategy_binding_intact",
        "owned_strategy_fundamental_provider_mode",
        "owned_strategy_require_bullish_market",
        "owned_strategy_require_proper_base",
        "owned_strategy_technical_only",
        "rs_annualization_trading_days",
        "rs_history_and_fallback_rule",
        "rs_minimum_history_sessions",
        "rs_percentile_min",
        "rs_percentile_multiplier",
        "rs_q1_weight",
        "rs_q2_weight",
        "rs_q3_weight",
        "rs_q4_weight",
        "rs_ranked_universe_gate",
        "s_breakout_proximity",
        "s_float_supply_rule",
        "s_float_weight",
        "s_peg_min_proximity",
        "s_power_gap_lookback",
        "s_power_gap_rule",
        "s_power_gap_weight",
        "s_surge_breakout_weight",
        "s_up_down_vol_weight",
        "s_up_down_volume_rule",
        "s_volume_breakout_rule",
        "s_volume_surge_threshold",
        "strategy_mode",
        "strategy_output_contract",
        "strategy_type_provenance",
        "technical_history_rule",
        "trading_days_per_quarter",
    },
    "market_policy": {
        "m_50ema_rising_lookback",
        "m_50ema_rising_weight",
        "m_bullish_threshold",
        "m_distribution_weight",
        "m_ema_alignment_weight",
        "m_follow_through_weight",
        "m_price_above_200ema_weight",
        "m_price_above_21ema_weight",
        "market_evaluator_mode",
        "market_follow_through_search_rule",
        "market_short_history_fallback",
        "market_trend_ema_rule",
        "require_bullish_market",
        "stateful_bootstrap_rule",
        "stateful_live_regime_transition_rule",
        "stateful_m_distribution_lookback",
        "stateful_m_distribution_min_decline",
        "stateful_m_follow_through_min_day",
        "stateful_m_follow_through_min_pct",
        "stateful_m_max_distribution_days",
        "stateful_m_regime_pressure_dist_days",
        "use_stateful_regime_gate",
    },
    "capacity_and_sizing_policy": {
        "cash_deployment_threshold_pct",
        "effective_position_sizing_formula",
        "eviction_enabled",
        "max_positions",
        "max_positions_mode",
        "position_risk_pct",
        "sizing_stop_loss_pct",
    },
    "exit_policy": {
        "breakeven_trigger_pct",
        "early_winner_rule",
        "ema_consecutive_closes_below",
        "ema_period",
        "scale_out_tiers",
        "stagnation_days",
        "stagnation_threshold_pct",
        "stop_loss_pct",
    },
    "unsupported_requests": {
        "min_c_a_growth",
        "min_canslim_score",
        "min_rs_score",
        "min_technical_score",
        "position_size_pct",
        "scale_out_fraction",
        "take_profit_pct",
    },
}


class _PITBundlePolicyStub:
    """Small causal-provider boundary used only by policy construction."""

    def fundamentals_provider(
        self, _symbol: str, _as_of_date: pd.Timestamp
    ) -> dict[str, object]:
        return {}


def _simulator() -> PortfolioSimulator:
    return PortfolioSimulator(data_fetcher=object(), technical_only=True)  # type: ignore[arg-type]


def _result_config(simulator: PortfolioSimulator) -> dict[str, object]:
    return simulator._result_config(
        tickers=["AAA"],
        benchmark="SPY",
        all_closes=pd.DataFrame(columns=["AAA"]),
        start_ts=pd.Timestamp("2024-01-02"),
        end_ts=pd.Timestamp("2024-01-31"),
    )


def test_effective_policy_has_closed_leaf_schema_and_canonical_digest() -> None:
    """Break caught: a policy leaf becomes unclassified or hashes non-canonically."""
    simulator = _simulator()
    policy = simulator._effective_engine_policy
    allowed_classifications = {
        "causal_invariant",
        "active_tunable_policy",
        "active_fixed_policy",
        "advisory_or_unsupported",
    }
    observed_classifications: set[object] = set()

    assert set(policy) == {
        "schema_version",
        "strategy_basis",
        "optimization_objective_owner",
        "optimization_objective",
        "optimization_executed_by_this_build",
        *_POLICY_SECTIONS,
    }
    assert policy["schema_version"] == 1
    assert policy["strategy_basis"] == "CANSLIM-derived"
    assert policy["optimization_objective_owner"] == "multi_agent_backtest_loop"
    assert policy["optimization_objective"] == (
        "maximize_return_and_minimize_drawdown"
    )
    assert policy["optimization_executed_by_this_build"] is False
    for section_name in _POLICY_SECTIONS:
        section = policy[section_name]
        assert isinstance(section, dict) and section
        assert set(section) == _EXPECTED_POLICY_SCHEMAS[section_name]
        for field_name, leaf in section.items():
            assert set(leaf) == {
                "value",
                "classification",
                "source",
                "optimizer_candidate",
            }, (section_name, field_name)
            observed_classifications.add(leaf["classification"])
            assert isinstance(leaf["source"], str) and leaf["source"]
            assert type(leaf["optimizer_candidate"]) is bool
    assert observed_classifications == allowed_classifications
    assert all(
        leaf["optimizer_candidate"] is False
        for leaf in policy["causal_invariants"].values()
    )

    assert policy["entry_policy"]["min_current_growth"] == {
        "value": 0.25,
        "classification": PolicyClassification.ACTIVE_FIXED_POLICY.value,
        "source": "core.canslim.entry_contract.MIN_CURRENT_GROWTH",
        "optimizer_candidate": True,
    }
    assert policy["causal_invariants"]["entry_execution_timing"] == {
        "value": "next_session_open",
        "classification": PolicyClassification.CAUSAL_INVARIANT.value,
        "source": "core.backtest_engine.PortfolioSimulator._enter_position",
        "optimizer_candidate": False,
    }
    assert policy["unsupported_requests"]["min_rs_score"]["classification"] == (
        PolicyClassification.ADVISORY_OR_UNSUPPORTED.value
    )

    canonical_bytes = json.dumps(
        policy,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    expected_digest = hashlib.sha256(canonical_bytes).hexdigest()
    reordered = dict(reversed(tuple(policy.items())))
    assert effective_engine_policy_sha256(policy) == expected_digest
    assert effective_engine_policy_sha256(reordered) == expected_digest
    assert simulator._effective_engine_policy_sha256 == expected_digest


def test_effective_policy_reports_independently_literal_behavior_facts() -> None:
    """Break caught: policy attestation drifts from behavior-bearing literals."""
    policy = _simulator()._effective_engine_policy

    assert {
        name: policy["entry_policy"][name]["value"]
        for name in (
            "min_current_growth",
            "min_annual_growth",
            "min_rs_score",
            "min_entry_composite_score",
            "min_volume_ratio",
            "max_buy_zone_extension",
        )
    } == {
        "min_current_growth": 0.25,
        "min_annual_growth": 0.25,
        "min_rs_score": 80.0,
        "min_entry_composite_score": 70.0,
        "min_volume_ratio": 1.3,
        "max_buy_zone_extension": 0.05,
    }
    assert policy["capacity_and_sizing_policy"]["effective_position_sizing_formula"] == {
        "value": "portfolio_equity * position_risk_pct / stop_loss_pct",
        "classification": "causal_invariant",
        "source": "core.backtest_engine.PortfolioSimulator._enter_position",
        "optimizer_candidate": False,
    }
    assert policy["exit_policy"]["scale_out_tiers"] == {
        "value": [[0.10, 0.25], [0.15, 0.25], [0.20, 0.25]],
        "classification": "active_fixed_policy",
        "source": "config.settings.SCALE_OUT_TIERS",
        "optimizer_candidate": True,
    }
    assert policy["market_policy"]["market_evaluator_mode"]["classification"] == (
        "active_tunable_policy"
    )
    assert policy["market_policy"]["stateful_live_regime_transition_rule"][
        "classification"
    ] == "active_fixed_policy"
    assert policy["market_policy"]["stateful_live_regime_transition_rule"]["value"][
        "entry_allowed_regimes"
    ] == ["confirmed_uptrend", "under_pressure"]
    assert policy["market_policy"]["stateful_live_regime_transition_rule"]["value"][
        "entry_blocked_regimes"
    ] == ["correction"]
    assert policy["unsupported_requests"]["take_profit_pct"] == {
        "value": 0.40,
        "classification": "advisory_or_unsupported",
        "source": "config.settings.SCALE_OUT_TIERS",
        "optimizer_candidate": True,
    }


def test_active_policy_drift_fails_before_result_publication() -> None:
    """Break caught: a behavior-bearing field changes after the run snapshot."""
    simulator = _simulator()
    simulator.stop_loss_pct = simulator.stop_loss_pct + 0.01

    with pytest.raises(ValueError, match="effective engine policy changed after run start"):
        _result_config(simulator)


def test_inert_request_drift_fails_at_the_policy_boundary() -> None:
    """Break caught: a legacy advisory threshold is reactivated after capture."""
    simulator = _simulator()
    simulator.min_rs_score = 79.0

    with pytest.raises(ValueError, match="inert field 'min_rs_score'"):
        simulator._verify_effective_engine_policy()


@pytest.mark.parametrize(
    ("constructor", "field_name", "incompatible_value"),
    [
        ("strategy", "min_c_a_growth", 0.26),
        ("strategy", "min_rs_score", 79.0),
        ("strategy", "min_canslim_score", 69.0),
        ("strategy", "min_technical_score", 69.0),
        ("simulator", "min_rs_score", 79.0),
        ("simulator", "min_canslim_score", 69.0),
        ("simulator", "min_technical_score", 69.0),
        ("simulator", "position_size_pct", 0.11),
        ("simulator", "take_profit_pct", 0.41),
        ("simulator", "scale_out_fraction", 0.51),
    ],
)
def test_every_inert_constructor_input_fails_closed(
    constructor: str,
    field_name: str,
    incompatible_value: float,
) -> None:
    """Break caught: an inert request is accepted as if it altered behavior."""
    arguments = {field_name: incompatible_value}

    with pytest.raises(ValueError, match=rf"inert field '{field_name}'"):
        if constructor == "strategy":
            CanslimStrategy(**arguments)  # type: ignore[arg-type]
        else:
            PortfolioSimulator(
                data_fetcher=object(),
                technical_only=True,
                **arguments,  # type: ignore[arg-type]
            )


def test_pit_mode_attests_proper_base_and_bound_fundamental_provider() -> None:
    """Break caught: PIT mode is reported without its causal base/provider binding."""
    simulator = PortfolioSimulator(
        data_fetcher=object(),  # type: ignore[arg-type]
        pit_bundle=_PITBundlePolicyStub(),  # type: ignore[arg-type]
    )
    policy = simulator._effective_engine_policy

    assert policy["entry_policy"]["proper_base_required"] == {
        "value": True,
        "classification": "active_tunable_policy",
        "source": "core.backtest_engine.PortfolioSimulator.require_proper_base",
        "optimizer_candidate": True,
    }
    assert policy["entry_policy"]["technical_only"]["value"] is False
    assert policy["entry_policy"]["industry_group_filter_mode"] == {
        "value": "unavailable_in_point_in_time_mode_without_causal_as_of_source",
        "classification": "advisory_or_unsupported",
        "source": "core.industry_group.load_industry_map",
        "optimizer_candidate": False,
    }
    assert policy["scoring_policy"]["fundamental_visibility"] == {
        "value": "pit_bundle_public_date_on_or_before_evaluation_session",
        "classification": "causal_invariant",
        "source": "core.pit_data.PITDataBundle.fundamentals_as_of",
        "optimizer_candidate": False,
    }
    assert policy["scoring_policy"]["fundamental_source_attestation"] == {
        "value": "authenticated_point_in_time_bundle_bound_provider",
        "classification": "causal_invariant",
        "source": (
            "core.backtest_engine.PortfolioSimulator._synchronize_owned_builtin_policy"
            "+core.pit_data.PITDataBundle.fundamentals_provider"
        ),
        "optimizer_candidate": False,
    }
    assert policy["scoring_policy"]["owned_strategy_require_proper_base"][
        "value"
    ] is True
    assert policy["scoring_policy"]["owned_strategy_fundamental_provider_mode"][
        "value"
    ] == "pit_bundle_bound_provider_path"


def test_technical_only_mode_attests_that_no_owned_fundamental_provider_is_used() -> None:
    """Break caught: technical-only policy contradicts its no-provider attestation."""
    policy = _simulator()._effective_engine_policy

    assert policy["scoring_policy"]["fundamental_source_attestation"] == {
        "value": "no_fundamental_source_used_by_built_in_strategy",
        "classification": "active_fixed_policy",
        "source": "core.engine_policy._fundamental_policy_fields",
        "optimizer_candidate": False,
    }
    assert policy["scoring_policy"]["owned_strategy_fundamental_provider_mode"] == {
        "value": "not_used_in_technical_only_mode",
        "classification": "active_tunable_policy",
        "source": "core.backtest_engine.CanslimStrategy.fundamental_provider",
        "optimizer_candidate": False,
    }


def test_completed_checkpoint_rejects_internally_valid_foreign_policy_digest() -> None:
    """Break caught: a rehashed completed checkpoint resumes under a different policy."""
    simulator = _simulator()
    result_config = deepcopy(_result_config(simulator))
    foreign_policy = deepcopy(result_config["effective_engine_policy"])
    foreign_policy["exit_policy"]["stop_loss_pct"]["value"] = 0.123
    result_config["effective_engine_policy"] = foreign_policy
    result_config["effective_engine_policy_sha256"] = (
        effective_engine_policy_sha256(foreign_policy)
    )
    checkpoint = {
        "completed": True,
        "entry_outcome_schema_version": 1,
        "entry_outcomes": [],
        "origin_requested_min_rs_score": simulator.requested_min_rs_score,
        "origin_requested_min_canslim_score": (
            simulator.requested_min_canslim_score
        ),
        "result_config": result_config,
    }

    with pytest.raises(
        ValueError,
        match="completed checkpoint result config effective policy disagrees",
    ):
        simulator._result_from_checkpoint(checkpoint, {}, "SPY")
