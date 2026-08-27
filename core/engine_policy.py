"""Canonical, JSON-safe description of effective backtest engine policy."""

from __future__ import annotations

import hashlib
import json
import math
from enum import StrEnum
from numbers import Real
from typing import Mapping, Protocol

from config import settings
from core.canslim.entry_contract import (
    MAX_BUY_ZONE_EXTENSION,
    MIN_ANNUAL_GROWTH,
    MIN_COMPOSITE_SCORE,
    MIN_CURRENT_GROWTH,
    MIN_RS_SCORE,
    MIN_VOLUME_RATIO,
)


class PolicyClassification(StrEnum):
    """Closed classification vocabulary for behavior-bearing policy fields."""

    CAUSAL_INVARIANT = "causal_invariant"
    ACTIVE_TUNABLE_POLICY = "active_tunable_policy"
    ACTIVE_FIXED_POLICY = "active_fixed_policy"
    ADVISORY_OR_UNSUPPORTED = "advisory_or_unsupported"


class _SimulatorPolicy(Protocol):
    max_positions: object
    position_size_pct: object
    position_risk_pct: object
    stop_loss_pct: object
    ma_exit_period: object
    ma_consecutive: object
    signal_every_n_days: object
    min_canslim_score: object
    min_rs_score: object
    min_technical_score: object
    require_bullish_market: object
    use_stateful_regime_gate: object
    cash_deployment_threshold_pct: object
    technical_only: object
    take_profit_pct: object
    scale_out_fraction: object
    stagnation_days: object
    stagnation_threshold_pct: object
    breakeven_trigger_pct: object
    require_proper_base: object
    enable_eviction: object
    industry_group_filter_enabled: object
    industry_group_top_n: object
    industry_group_min_size: object
    _strategy_was_injected: bool
    _owned_builtin_strategy: object
    pit_bundle: object
    strategy: object


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _field(
    value: object,
    classification: PolicyClassification,
    source: str,
    *,
    optimizer_candidate: bool,
) -> dict[str, object]:
    """Create one canonical policy leaf and reject unsafe values."""

    if not isinstance(classification, PolicyClassification):
        raise TypeError("classification must be a PolicyClassification")
    if not isinstance(source, str) or not source:
        raise ValueError("policy field source must be a non-empty string")
    if not isinstance(optimizer_candidate, bool):
        raise TypeError("optimizer_candidate must be a bool")
    try:
        normalized_value = json.loads(_canonical_json_bytes(value).decode("utf-8"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("policy field value must be JSON-safe") from exc
    return {
        "value": normalized_value,
        "classification": classification.value,
        "source": source,
        "optimizer_candidate": optimizer_candidate,
    }


def validate_inert_request_compatibility(
    requests: Mapping[str, object],
    compatibility_values: Mapping[str, object],
    policy_sources: Mapping[str, str],
) -> None:
    """Fail closed when a caller asks an inert field to change behavior."""

    if set(requests) != set(compatibility_values) or set(requests) != set(policy_sources):
        raise ValueError("inert request compatibility specification is incomplete")
    for name, requested in requests.items():
        expected = compatibility_values[name]
        source = policy_sources[name]
        valid_number = isinstance(requested, Real) and not isinstance(requested, bool)
        expected_number = isinstance(expected, Real) and not isinstance(expected, bool)
        compatible = bool(
            valid_number
            and expected_number
            and math.isfinite(float(requested))
            and math.isfinite(float(expected))
            and math.isclose(
                float(requested),
                float(expected),
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        )
        if not compatible:
            raise ValueError(
                f"inert field {name!r} accepts only compatibility value {expected!r}; "
                f"actual policy source is {source}"
            )


_MISSING_CUSTOM_IDENTITY = object()


def _custom_policy_identity(
    strategy: object,
) -> tuple[str | None, object | None]:
    """Return a declared JSON-safe identity without inspecting strategy internals."""

    for name in ("effective_policy_identity",):
        try:
            declared = getattr(strategy, name, _MISSING_CUSTOM_IDENTITY)
        except Exception as exc:
            raise ValueError(
                f"custom strategy {name} could not be accessed"
            ) from exc
        if declared is _MISSING_CUSTOM_IDENTITY or declared is None:
            continue
        if callable(declared):
            try:
                declared = declared()
            except Exception as exc:
                raise ValueError(
                    f"custom strategy {name} could not be evaluated"
                ) from exc
        if declared is None:
            continue
        try:
            normalized = json.loads(_canonical_json_bytes(declared).decode("utf-8"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                f"custom strategy {name} must be JSON-safe"
            ) from exc
        return name, normalized
    return None, None


def _owned_builtin_provider_mode(simulator: _SimulatorPolicy) -> str:
    strategy = simulator._owned_builtin_strategy
    if strategy is None:
        return "not_owned_builtin"
    if simulator.technical_only:
        return "not_used_in_technical_only_mode"
    provider = getattr(strategy, "fundamental_provider", _MISSING_CUSTOM_IDENTITY)
    if simulator.pit_bundle is None:
        return (
            "legacy_scored_provider_path"
            if provider is None
            else "unexpected_nonlegacy_provider"
        )
    expected = getattr(simulator.pit_bundle, "fundamentals_provider", None)
    return (
        "pit_bundle_bound_provider_path"
        if provider == expected
        else "unexpected_non_bundle_provider"
    )


def _causal_invariant_fields(
    simulator: _SimulatorPolicy,
) -> dict[str, dict[str, object]]:
    del simulator
    invariant = PolicyClassification.CAUSAL_INVARIANT
    return {
        "technical_fact_cutoff": _field(
            "completed_session_only",
            invariant,
            "core.trading_sessions.history_through_exact_session",
            optimizer_candidate=False,
        ),
        "entry_execution_timing": _field(
            "next_session_open",
            invariant,
            "core.backtest_engine.PortfolioSimulator._enter_position",
            optimizer_candidate=False,
        ),
        "next_open_buy_zone_revalidation": _field(
            True,
            invariant,
            "core.backtest_engine.PortfolioSimulator._enter_position",
            optimizer_candidate=False,
        ),
        "cash_and_no_leverage_constraint": _field(
            True,
            invariant,
            "core.backtest_engine.PortfolioSimulator._enter_position",
            optimizer_candidate=False,
        ),
    }


def _industry_group_filter_fields(
    simulator: _SimulatorPolicy,
) -> dict[str, dict[str, object]]:
    """Describe only industry policy that the active data mode can honor."""

    enabled = simulator.industry_group_filter_enabled
    if not isinstance(enabled, bool):
        raise ValueError("industry_group_filter_enabled must be a bool")

    unsupported = PolicyClassification.ADVISORY_OR_UNSUPPORTED
    if simulator.technical_only:
        if enabled:
            raise ValueError(
                "industry group filter cannot be enabled in technical-only mode; "
                "no causal industry data source is configured"
            )
        classification = unsupported
        mode = "not_used_in_technical_only_mode"
        optimizer_candidate = False
    elif simulator.pit_bundle is not None:
        if enabled:
            raise ValueError(
                "industry group filter cannot be enabled in point-in-time mode "
                "without a causal as-of industry data source"
            )
        classification = unsupported
        mode = "unavailable_in_point_in_time_mode_without_causal_as_of_source"
        optimizer_candidate = False
    else:
        classification = PolicyClassification.ACTIVE_TUNABLE_POLICY
        mode = (
            "provider_current_cached_industry_map_ranked_by_as_of_rs"
            if enabled
            else "disabled_provider_source_available"
        )
        optimizer_candidate = True

    parameter_classification = (
        PolicyClassification.ACTIVE_FIXED_POLICY
        if optimizer_candidate
        else unsupported
    )
    return {
        "industry_group_filter_enabled": _field(
            enabled,
            classification,
            "core.backtest_engine.PortfolioSimulator.industry_group_filter_enabled",
            optimizer_candidate=optimizer_candidate,
        ),
        "industry_group_filter_mode": _field(
            mode,
            classification,
            "core.industry_group.load_industry_map",
            optimizer_candidate=optimizer_candidate,
        ),
        "industry_group_top_n": _field(
            simulator.industry_group_top_n,
            parameter_classification,
            "core.industry_group.get_top_groups.top_n",
            optimizer_candidate=optimizer_candidate,
        ),
        "industry_group_min_size": _field(
            simulator.industry_group_min_size,
            parameter_classification,
            "core.industry_group.get_top_groups.min_size",
            optimizer_candidate=optimizer_candidate,
        ),
    }


def _entry_policy_fields(
    simulator: _SimulatorPolicy,
) -> dict[str, dict[str, object]]:
    fixed = PolicyClassification.ACTIVE_FIXED_POLICY
    tunable = PolicyClassification.ACTIVE_TUNABLE_POLICY
    invariant = PolicyClassification.CAUSAL_INVARIANT
    return {
        "min_current_growth": _field(
            MIN_CURRENT_GROWTH,
            fixed,
            "core.canslim.entry_contract.MIN_CURRENT_GROWTH",
            optimizer_candidate=True,
        ),
        "min_annual_growth": _field(
            MIN_ANNUAL_GROWTH,
            fixed,
            "core.canslim.entry_contract.MIN_ANNUAL_GROWTH",
            optimizer_candidate=True,
        ),
        "min_rs_score": _field(
            MIN_RS_SCORE,
            fixed,
            "core.canslim.entry_contract.MIN_RS_SCORE",
            optimizer_candidate=True,
        ),
        "min_entry_composite_score": _field(
            MIN_COMPOSITE_SCORE,
            fixed,
            "core.canslim.entry_contract.MIN_COMPOSITE_SCORE",
            optimizer_candidate=True,
        ),
        "min_volume_ratio": _field(
            MIN_VOLUME_RATIO,
            fixed,
            "core.canslim.entry_contract.MIN_VOLUME_RATIO",
            optimizer_candidate=True,
        ),
        "max_buy_zone_extension": _field(
            MAX_BUY_ZONE_EXTENSION,
            fixed,
            "core.canslim.entry_contract.MAX_BUY_ZONE_EXTENSION",
            optimizer_candidate=True,
        ),
        "technical_facts": _field(
            "completed_session_close_volume_and_pivot",
            invariant,
            "core.canslim.entry_contract.build_entry_facts",
            optimizer_candidate=False,
        ),
        "proper_base_required": _field(
            simulator.require_proper_base,
            tunable,
            "core.backtest_engine.PortfolioSimulator.require_proper_base",
            optimizer_candidate=True,
        ),
        "technical_only": _field(
            simulator.technical_only,
            tunable,
            "core.backtest_engine.PortfolioSimulator.technical_only",
            optimizer_candidate=False,
        ),
        "signal_every_n_days": _field(
            simulator.signal_every_n_days,
            tunable,
            "core.backtest_engine.PortfolioSimulator.signal_every_n_days",
            optimizer_candidate=True,
        ),
        **_industry_group_filter_fields(simulator),
    }


def _fundamental_policy_fields(
    simulator: _SimulatorPolicy,
) -> dict[str, dict[str, object]]:
    """Report fundamental use and provenance at the engine's attestation scope."""

    fixed = PolicyClassification.ACTIVE_FIXED_POLICY
    invariant = PolicyClassification.CAUSAL_INVARIANT
    unsupported = PolicyClassification.ADVISORY_OR_UNSUPPORTED

    if simulator.technical_only:
        fields = {
            "fundamental_usage": _field(
                "not_consumed_by_engine_technical_only_entry_contract",
                fixed,
                "core.backtest_engine.CanslimStrategy._evaluate_symbol_with_entry_facts+PortfolioSimulator._canonicalize_signal_row",
                optimizer_candidate=False,
            ),
            "fundamental_visibility": _field(
                "not_applicable_to_engine_technical_only_entry_contract",
                fixed,
                "core.backtest_engine.PortfolioSimulator.technical_only",
                optimizer_candidate=False,
            ),
            "fundamental_source_attestation": _field(
                (
                    "custom_strategy_internals_unattested"
                    if simulator._strategy_was_injected
                    else "no_fundamental_source_used_by_built_in_strategy"
                ),
                unsupported if simulator._strategy_was_injected else fixed,
                "core.engine_policy._fundamental_policy_fields",
                optimizer_candidate=False,
            ),
        }
        if simulator._strategy_was_injected:
            fields["custom_fundamental_internals_attestation"] = _field(
                "opaque_unattested_no_engine_verifiable_provenance_contract",
                unsupported,
                "core.engine_policy._fundamental_policy_fields",
                optimizer_candidate=False,
            )
        return fields

    if simulator._strategy_was_injected:
        return {
            "fundamental_usage": _field(
                "custom_strategy_outputs_consumed_by_engine_entry_contract",
                unsupported,
                "core.backtest_engine.PortfolioSimulator._canonicalize_signal_row",
                optimizer_candidate=False,
            ),
            "fundamental_visibility": _field(
                "unattested_custom_strategy_internals",
                unsupported,
                "core.engine_policy._fundamental_policy_fields",
                optimizer_candidate=False,
            ),
            "fundamental_source_attestation": _field(
                "opaque_unattested_no_engine_verifiable_provenance_contract",
                unsupported,
                "core.engine_policy._fundamental_policy_fields",
                optimizer_candidate=False,
            ),
            "custom_fundamental_internals_attestation": _field(
                "opaque_unattested_no_engine_verifiable_provenance_contract",
                unsupported,
                "core.engine_policy._fundamental_policy_fields",
                optimizer_candidate=False,
            ),
        }

    if simulator.pit_bundle is not None:
        return {
            "fundamental_usage": _field(
                "built_in_C_A_I_and_supply_inputs",
                fixed,
                "core.backtest_engine.CanslimStrategy._evaluate_symbol_with_entry_facts",
                optimizer_candidate=False,
            ),
            "fundamental_visibility": _field(
                "pit_bundle_public_date_on_or_before_evaluation_session",
                invariant,
                "core.pit_data.PITDataBundle.fundamentals_as_of",
                optimizer_candidate=False,
            ),
            "fundamental_source_attestation": _field(
                "authenticated_point_in_time_bundle_bound_provider",
                invariant,
                "core.backtest_engine.PortfolioSimulator._synchronize_owned_builtin_policy+core.pit_data.PITDataBundle.fundamentals_provider",
                optimizer_candidate=False,
            ),
        }

    return {
        "fundamental_usage": _field(
            "built_in_C_A_I_and_supply_inputs",
            fixed,
            "core.backtest_engine.CanslimStrategy._evaluate_symbol_with_entry_facts",
            optimizer_candidate=False,
        ),
        "fundamental_visibility": _field(
            {
                "financial_and_institutional_records": (
                    "provider_revision_timestamp_on_or_before_evaluation_session"
                ),
                "shares_outstanding": (
                    "historical_enterprise_value_when_available_else_current_profile_fallback"
                ),
            },
            fixed,
            "core.data_client.fetch_fundamental_data_as_of+_fetch_company_info_as_of",
            optimizer_candidate=False,
        ),
        "fundamental_source_attestation": _field(
            "provider_cache_as_of_filter_with_current_profile_shares_fallback_not_authenticated_pit_bundle",
            fixed,
            "backtest._evaluate_fundamentals_at_date+core.data_client.fetch_fundamental_data_as_of",
            optimizer_candidate=False,
        ),
    }


def _scoring_policy_fields(
    simulator: _SimulatorPolicy,
) -> dict[str, dict[str, object]]:
    fixed = PolicyClassification.ACTIVE_FIXED_POLICY
    tunable = PolicyClassification.ACTIVE_TUNABLE_POLICY
    invariant = PolicyClassification.CAUSAL_INVARIANT
    strategy_type = type(simulator.strategy)
    strategy_identity = {
        "module": str(strategy_type.__module__),
        "qualname": str(strategy_type.__qualname__),
    }
    contract = (
        "engine_rebuilds_completed_session_technical_facts_and_bypasses_C_A_RS_composite_gates"
        if simulator.technical_only
        else "engine_rebuilds_completed_session_technical_facts_and_reapplies_canonical_C_A_RS_non_M_composite_contract"
    )
    fields: dict[str, dict[str, object]] = {
        "strategy_mode": _field(
            "injected_custom" if simulator._strategy_was_injected else "built_in_canslim",
            tunable,
            "core.backtest_engine.PortfolioSimulator._strategy_was_injected",
            optimizer_candidate=False,
        ),
        "strategy_type_provenance": _field(
            strategy_identity,
            PolicyClassification.ADVISORY_OR_UNSUPPORTED,
            "core.engine_policy.type(strategy).__module___and___qualname__",
            optimizer_candidate=False,
        ),
        "strategy_output_contract": _field(
            contract,
            invariant,
            "core.backtest_engine.PortfolioSimulator._canonicalize_signal_row",
            optimizer_candidate=False,
        ),
        **_fundamental_policy_fields(simulator),
    }
    for name in (
        "TRADING_DAYS_PER_QUARTER",
        "RS_Q1_WEIGHT",
        "RS_Q2_WEIGHT",
        "RS_Q3_WEIGHT",
        "RS_Q4_WEIGHT",
        "RS_PERCENTILE_MULTIPLIER",
        "RS_PERCENTILE_MIN",
    ):
        fields[name.lower()] = _field(
            getattr(settings, name),
            fixed,
            f"config.settings.{name}",
            optimizer_candidate=True,
        )
    fields["rs_annualization_trading_days"] = _field(
        252,
        fixed,
        "core.momentum_analysis.calculate_rs_snapshot",
        optimizer_candidate=True,
    )
    fields["rs_minimum_history_sessions"] = _field(
        60,
        fixed,
        "core.momentum_analysis.calculate_rs_snapshot",
        optimizer_candidate=True,
    )
    fields["rs_ranked_universe_gate"] = _field(
        {"minimum_ranked_symbols": 10, "below_minimum_result": "empty_rs_snapshot"},
        fixed,
        "core.momentum_analysis.calculate_rs_snapshot",
        optimizer_candidate=True,
    )
    fields["rs_history_and_fallback_rule"] = _field(
        {
            "standard_history_sessions": "4 * TRADING_DAYS_PER_QUARTER",
            "minimum_fallback_history_sessions": 60,
            "fallback_formula": "(1 + raw_return) ** (252 / observed_trading_days) - 1",
            "fallback_annualization_sessions": 252,
        },
        fixed,
        "core.momentum_analysis.calculate_weighted_performance+calculate_rs_snapshot",
        optimizer_candidate=True,
    )
    if simulator._strategy_was_injected:
        identity_source, declared_identity = _custom_policy_identity(simulator.strategy)
        if identity_source is None:
            fields["attestation_scope"] = _field(
                "engine_owned_envelope_only",
                PolicyClassification.ADVISORY_OR_UNSUPPORTED,
                "core.engine_policy._custom_policy_identity",
                optimizer_candidate=False,
            )
            fields["custom_internals_opaque_unattested"] = _field(
                True,
                PolicyClassification.ADVISORY_OR_UNSUPPORTED,
                "core.engine_policy._custom_policy_identity",
                optimizer_candidate=False,
            )
        else:
            fields["attestation_scope"] = _field(
                "engine_owned_envelope_plus_declared_custom_identity",
                invariant,
                "core.engine_policy._custom_policy_identity",
                optimizer_candidate=False,
            )
            fields["custom_policy_identity_source"] = _field(
                identity_source,
                tunable,
                f"strategy.{identity_source}",
                optimizer_candidate=False,
            )
            fields["custom_policy_identity"] = _field(
                declared_identity,
                tunable,
                f"strategy.{identity_source}",
                optimizer_candidate=False,
            )
        return fields
    fields["attestation_scope"] = _field(
        "complete_built_in_optimizer_relevant_scoring_and_engine_policy",
        invariant,
        "core.engine_policy.build_effective_engine_policy",
        optimizer_candidate=False,
    )
    fields["owned_strategy_binding_intact"] = _field(
        simulator.strategy is simulator._owned_builtin_strategy,
        invariant,
        "core.engine_policy._scoring_policy_fields:simulator.strategy_is_simulator._owned_builtin_strategy",
        optimizer_candidate=False,
    )
    fields["owned_strategy_technical_only"] = _field(
        getattr(simulator._owned_builtin_strategy, "technical_only", None),
        tunable,
        "core.backtest_engine.CanslimStrategy.technical_only",
        optimizer_candidate=False,
    )
    fields["owned_strategy_require_bullish_market"] = _field(
        getattr(simulator._owned_builtin_strategy, "require_bullish_market", None),
        tunable,
        "core.backtest_engine.CanslimStrategy.require_bullish_market",
        optimizer_candidate=False,
    )
    fields["owned_strategy_require_proper_base"] = _field(
        getattr(simulator._owned_builtin_strategy, "require_proper_base", None),
        tunable,
        "core.backtest_engine.CanslimStrategy.require_proper_base",
        optimizer_candidate=False,
    )
    fields["owned_strategy_fundamental_provider_mode"] = _field(
        _owned_builtin_provider_mode(simulator),
        tunable,
        "core.backtest_engine.CanslimStrategy.fundamental_provider",
        optimizer_candidate=False,
    )
    fields.update({
        "current_earnings_metric_family_priority": _field(
            ["diluted_eps", "basic_eps", "net_income"],
            fixed,
            "core.canslim.c_current_earnings._find_earnings_row",
            optimizer_candidate=True,
        ),
        "annual_earnings_metric_family_priority": _field(
            ["diluted_eps", "basic_eps", "net_income"],
            fixed,
            "core.canslim.a_annual_earnings._find_earnings_row",
            optimizer_candidate=True,
        ),
        "non_m_entry_composite": _field(
            "weighted_C_A_N_S_L_I_renormalized_to_100_excluding_M",
            fixed,
            "backtest._compute_entry_composite_score",
            optimizer_candidate=True,
        ),
        "institutional_data_reweighting": _field(
            "when_I_is_unavailable_set_I_to_0_and_renormalize_remaining_weights",
            fixed,
            "backtest._active_canslim_weights",
            optimizer_candidate=True,
        ),
    })
    for component in "CANSLIM":
        fields[f"component_weight_{component.lower()}"] = _field(
            getattr(settings, f"CANSLIM_WEIGHT_{component}"),
            fixed,
            f"config.settings.CANSLIM_WEIGHT_{component}",
            optimizer_candidate=True,
        )
    for name in (
        "C_GROWTH_TARGET",
        "C_GROWTH_WEIGHT",
        "C_CONSISTENCY_WEIGHT",
        "C_ACCELERATION_WEIGHT",
        "A_GROWTH_TARGET",
        "A_ROE_TARGET",
        "A_MIN_YEARS_GROWTH",
        "A_GROWTH_WEIGHT",
        "A_CONSISTENCY_WEIGHT",
        "A_ROE_WEIGHT",
        "N_REVENUE_GROWTH_WEIGHT",
        "N_PROXIMITY_TO_HIGH_WEIGHT",
        "N_REVENUE_GROWTH_TARGET",
        "S_VOLUME_SURGE_THRESHOLD",
        "S_POWER_GAP_LOOKBACK",
        "S_PEG_MIN_PROXIMITY",
        "S_FLOAT_WEIGHT",
        "S_UP_DOWN_VOL_WEIGHT",
        "S_SURGE_BREAKOUT_WEIGHT",
        "S_POWER_GAP_WEIGHT",
        "I_LEVEL_WEIGHT",
        "I_TREND_WEIGHT",
    ):
        fields[name.lower()] = _field(
            getattr(settings, name),
            fixed,
            f"config.settings.{name}",
            optimizer_candidate=True,
        )
    fields["s_breakout_proximity"] = _field(
        0.95,
        fixed,
        "backtest._evaluate_technical_at_date",
        optimizer_candidate=True,
    )
    fields["n_growth_score_rule"] = _field(
        {
            "formula": "clip(revenue_growth / N_REVENUE_GROWTH_TARGET, 0, 2) / 2",
            "score_floor": 0.0,
            "score_cap": 1.0,
            "unavailable_score": 0.0,
            "available_components": "renormalize_active_component_weights",
            "all_components_unavailable_score": 0.0,
        },
        fixed,
        "core.canslim.n_new_products._score_from_growth",
        optimizer_candidate=True,
    )
    fields["n_proximity_score_rule"] = _field(
        {
            "full_score_at_or_above": 0.98,
            "middle_band": {
                "lower_inclusive": 0.90,
                "upper_exclusive": 0.98,
                "formula": "(proximity - 0.90) / (0.98 - 0.90)",
            },
            "lower_band": {
                "lower_inclusive": 0.75,
                "upper_exclusive": 0.90,
                "factor": 0.3,
                "formula": "(proximity - 0.75) / (0.90 - 0.75) * 0.3",
            },
            "below_lower_band_score": 0.0,
            "nonpositive_or_unavailable_score": 0.0,
        },
        fixed,
        "core.canslim.n_new_products.evaluate_n",
        optimizer_candidate=True,
    )
    fields["technical_history_rule"] = _field(
        {
            "minimum_history_sessions": 60,
            "new_high_window_sessions": 252,
        },
        fixed,
        "core.backtest_engine.CanslimStrategy._evaluate_symbol_with_entry_facts+backtest._evaluate_technical_at_date",
        optimizer_candidate=True,
    )
    fields["s_power_gap_rule"] = _field(
        {
            "default_lookback_days": 10,
            "gap_threshold": 0.02,
            "volume_threshold": 1.5,
            "minimum_history_rows": "lookback_days + 50",
            "volume_baseline_slice": "volumes.iloc[-(lookback_days + 50):-(lookback_days + 1)]",
            "default_volume_baseline_observations": 49,
            "gap_formula": "(open - prior_close) / prior_close",
            "search_order": "newest_first",
        },
        fixed,
        "core.canslim.s_supply_demand._detect_power_earnings_gap",
        optimizer_candidate=True,
    )
    fields["s_up_down_volume_rule"] = _field(
        {
            "lookback_sessions": 50,
            "no_up_days_mean_volume": 0.0,
            "no_down_days_default_mean_volume": 1.0,
            "zero_down_volume_ratio": 2.0,
            "ratio_cap": 3.0,
            "score_bands": [
                {"ratio_min": 1.5, "score": 1.0},
                {
                    "ratio_min": 1.0,
                    "ratio_max_exclusive": 1.5,
                    "formula": "(ratio - 1.0) / 0.5",
                },
                {
                    "ratio_max_exclusive": 1.0,
                    "floor": 0.5,
                    "factor": 0.3,
                    "formula": "max(ratio - 0.5, 0) / 0.5 * 0.3",
                },
            ],
        },
        fixed,
        "core.canslim.s_supply_demand._calculate_up_down_volume_ratio+evaluate_s",
        optimizer_candidate=True,
    )
    fields["s_float_supply_rule"] = _field(
        {
            "shares_unit_divisor": 1_000_000.0,
            "unknown_or_nonpositive_score": 0.5,
            "bands_millions": [
                {"upper_exclusive": 50, "score": 1.0},
                {"upper_exclusive": 200, "score": 0.85},
                {"upper_exclusive": 500, "score": 0.65},
                {"upper_exclusive": 1000, "score": 0.4},
                {"lower_inclusive": 1000, "score": 0.2},
            ],
        },
        fixed,
        "core.canslim.s_supply_demand._score_float_supply",
        optimizer_candidate=True,
    )
    fields["s_volume_breakout_rule"] = _field(
        {
            "reported_volume_surge_requires_price_advance": True,
            "volume_score": "min(volume_ratio / surge_threshold, 1.0) when baseline_positive_else_0",
            "volume_score_uses_ratio_independent_of_reported_surge_boolean": True,
            "proximity_floor": 0.85,
            "proximity_score": "clip((proximity - 0.85) / (breakout_proximity - 0.85), 0, 1)",
            "proximity_score_when_breakout_proximity_at_or_below_floor": 1.0,
            "breakout_score": "1.0_when_qualified_else_max(proximity_score, 0)",
            "volume_score_weight": 0.5,
            "breakout_score_weight": 0.5,
            "zero_high_result": {"is_breakout": False, "proximity": 0.0},
        },
        fixed,
        "core.canslim.s_supply_demand._detect_volume_surge+_detect_breakout+evaluate_s",
        optimizer_candidate=True,
    )
    fields["i_ownership_level_rule"] = _field(
        {
            "bands": [
                {"upper_exclusive": 0.10, "formula": "held / 0.10 * 0.3"},
                {"upper_exclusive": 0.30, "formula": "0.3 + (held - 0.10) / 0.20 * 0.4"},
                {"upper_exclusive": 0.60, "formula": "0.7 + (held - 0.30) / 0.30 * 0.3"},
                {"upper_exclusive": 0.80, "formula": "1.0 - (held - 0.60) / 0.20 * 0.15"},
                {"upper_exclusive": 0.90, "formula": "0.85 - (held - 0.80) / 0.10 * 0.25"},
                {"lower_inclusive": 0.90, "formula": "max(0.6 - (held - 0.90) / 0.10 * 0.3, 0.3)", "score_floor": 0.3},
            ]
        },
        fixed,
        "core.canslim.i_institutional._score_ownership_level",
        optimizer_candidate=True,
    )
    fields["i_ownership_trend_rule"] = _field(
        {
            "unavailable_or_nonpositive_previous_score": 0.5,
            "bands": [
                {"change_min": 0.10, "score": 1.0},
                {"change_min": 0.03, "change_max_exclusive": 0.10, "formula": "0.7 + (change - 0.03) / 0.07 * 0.3"},
                {"change_min": 0.0, "change_max_exclusive": 0.03, "formula": "0.5 + change / 0.03 * 0.2"},
                {"change_min": -0.05, "change_max_exclusive": 0.0, "formula": "0.5 + change / 0.05 * 0.2"},
                {"change_max_exclusive": -0.05, "formula": "max(0.3 + (change + 0.05) / 0.15 * 0.2, 0.1)", "score_floor": 0.1},
            ],
            "all_components_unavailable_score": 0.5,
            "available_components": "renormalize_active_component_weights",
        },
        fixed,
        "core.canslim.i_institutional._score_ownership_trend+evaluate_i",
        optimizer_candidate=True,
    )
    return fields


def _market_policy_fields(
    simulator: _SimulatorPolicy,
) -> dict[str, dict[str, object]]:
    tunable = PolicyClassification.ACTIVE_TUNABLE_POLICY
    fixed = PolicyClassification.ACTIVE_FIXED_POLICY
    fields = {
        "require_bullish_market": _field(
            simulator.require_bullish_market,
            tunable,
            "core.backtest_engine.PortfolioSimulator.require_bullish_market",
            optimizer_candidate=True,
        ),
        "use_stateful_regime_gate": _field(
            simulator.use_stateful_regime_gate,
            tunable,
            "core.backtest_engine.PortfolioSimulator.use_stateful_regime_gate",
            optimizer_candidate=True,
        ),
        "market_evaluator_mode": _field(
            "injected_custom" if simulator._strategy_was_injected else "built_in_evaluate_m",
            tunable,
            "core.backtest_engine.PortfolioSimulator.strategy.evaluate_market",
            optimizer_candidate=False,
        ),
        "stateful_bootstrap_rule": _field(
            {
                "constructor_default_regime": "correction",
                "short_history_rule": {
                    "fewer_than_rows": 2,
                    "effect": "return_without_changing_constructor_default_or_seeding_distribution_days",
                },
                "ema_rule": {
                    "period": 200,
                    "adjust": False,
                    "comparison": "latest_close_strictly_greater_than_latest_ema",
                    "above_result": "confirmed_uptrend",
                    "otherwise_result": "correction",
                },
                "distribution_replay": {
                    "history_selection": "spy_df.loc[:start_date]",
                    "row_count": "min(M_DISTRIBUTION_LOOKBACK + 1, len(hist))",
                    "comparison_count": "replay_row_count - 1",
                    "iteration": "i in range(1, replay_row_count)",
                    "bar_index_formula": "i - replay_row_count + 1",
                    "bar_index_range": "2 - replay_row_count through 0",
                    "daily_change_formula": "(close - previous_close) / previous_close",
                    "predicate": "previous_close > 0 and daily_change <= -M_DISTRIBUTION_MIN_DECLINE and volume > previous_volume",
                },
                "post_seed_rule": {
                    "applies_only_when_ema_regime_is_not_correction": True,
                    "correction_at_or_above": "M_MAX_DISTRIBUTION_DAYS",
                    "under_pressure_at_or_above": "M_REGIME_PRESSURE_DIST_DAYS",
                    "precedence": ["correction", "under_pressure"],
                },
            },
            fixed,
            "core.canslim.m_market_direction.MarketRegimeTracker.__init__+bootstrap",
            optimizer_candidate=True,
        ),
        "stateful_live_regime_transition_rule": _field(
            {
                "entry_allowed_regimes": ["confirmed_uptrend", "under_pressure"],
                "entry_blocked_regimes": ["correction"],
                "correction_trigger": "distribution_days_at_or_above_M_MAX_DISTRIBUTION_DAYS",
                "under_pressure_trigger": "noncorrection_and_distribution_days_at_or_above_M_REGIME_PRESSURE_DIST_DAYS",
                "correction_low_tracking": "min(previous_correction_low, close)_while_not_rallying",
                "rally_start": "correction_close_above_previous_close",
                "rally_day_increment": "close_above_previous_close",
                "rally_undercut_reset": "close_below_correction_low_clears_rally_count_and_resets_correction_low",
                "follow_through_transition": "on_or_after_M_FOLLOW_THROUGH_MIN_DAY_with_gain_at_or_above_M_FOLLOW_THROUGH_MIN_PCT_and_higher_volume",
                "follow_through_effect": "confirmed_uptrend_and_clear_distribution_days_and_rally_state",
            },
            fixed,
            "core.canslim.m_market_direction.MarketRegimeTracker.__init__+allows_entries+update",
            optimizer_candidate=True,
        ),
    }
    stateful_names = (
        "M_DISTRIBUTION_LOOKBACK",
        "M_DISTRIBUTION_MIN_DECLINE",
        "M_MAX_DISTRIBUTION_DAYS",
        "M_REGIME_PRESSURE_DIST_DAYS",
        "M_FOLLOW_THROUGH_MIN_PCT",
        "M_FOLLOW_THROUGH_MIN_DAY",
    )
    for name in stateful_names:
        fields[f"stateful_{name.lower()}"] = _field(
            getattr(settings, name),
            fixed,
            f"config.settings.{name}",
            optimizer_candidate=True,
        )
    if simulator._strategy_was_injected:
        return fields
    fields["market_short_history_fallback"] = _field(
        {
            "minimum_rows": 50,
            "score": 0.4,
            "is_bullish": False,
            "latest_close": None,
        },
        fixed,
        "core.canslim.m_market_direction.evaluate_m",
        optimizer_candidate=True,
    )
    fields["market_follow_through_search_rule"] = _field(
        {
            "minimum_input_rows": 5,
            "search_window_sessions": 30,
            "rally_reset_daily_change_below": -0.01,
            "rally_starts_on_daily_change_above": 0.0,
            "requires_higher_volume": True,
        },
        fixed,
        "core.canslim.m_market_direction._detect_follow_through_day",
        optimizer_candidate=True,
    )
    fields["market_trend_ema_rule"] = _field(
        {"periods": [21, 50, 200], "adjust": True},
        fixed,
        "core.canslim.m_market_direction.evaluate_m",
        optimizer_candidate=True,
    )
    evaluator_names = (
        "M_PRICE_ABOVE_200EMA_WEIGHT",
        "M_EMA_ALIGNMENT_WEIGHT",
        "M_50EMA_RISING_WEIGHT",
        "M_PRICE_ABOVE_21EMA_WEIGHT",
        "M_BULLISH_THRESHOLD",
        "M_50EMA_RISING_LOOKBACK",
        "M_DISTRIBUTION_WEIGHT",
        "M_FOLLOW_THROUGH_WEIGHT",
    )
    for name in evaluator_names:
        fields[name.lower()] = _field(
            getattr(settings, name),
            fixed,
            f"config.settings.{name}",
            optimizer_candidate=True,
        )
    return fields


def _capacity_and_sizing_fields(
    simulator: _SimulatorPolicy,
) -> dict[str, dict[str, object]]:
    tunable = PolicyClassification.ACTIVE_TUNABLE_POLICY
    invariant = PolicyClassification.CAUSAL_INVARIANT
    return {
        "max_positions": _field(
            simulator.max_positions,
            tunable,
            "core.backtest_engine.PortfolioSimulator.max_positions",
            optimizer_candidate=True,
        ),
        "max_positions_mode": _field(
            "uncapped" if simulator.max_positions is None else "capped",
            tunable,
            "core.backtest_engine.PortfolioSimulator.max_positions",
            optimizer_candidate=True,
        ),
        "eviction_enabled": _field(
            simulator.enable_eviction,
            tunable,
            "core.backtest_engine.PortfolioSimulator.enable_eviction",
            optimizer_candidate=True,
        ),
        "cash_deployment_threshold_pct": _field(
            simulator.cash_deployment_threshold_pct,
            tunable,
            "core.backtest_engine.PortfolioSimulator.cash_deployment_threshold_pct",
            optimizer_candidate=True,
        ),
        "effective_position_sizing_formula": _field(
            "portfolio_equity * position_risk_pct / stop_loss_pct",
            invariant,
            "core.backtest_engine.PortfolioSimulator._enter_position",
            optimizer_candidate=False,
        ),
        "position_risk_pct": _field(
            simulator.position_risk_pct,
            tunable,
            "core.backtest_engine.PortfolioSimulator.position_risk_pct",
            optimizer_candidate=True,
        ),
        "sizing_stop_loss_pct": _field(
            simulator.stop_loss_pct,
            tunable,
            "core.backtest_engine.PortfolioSimulator.stop_loss_pct",
            optimizer_candidate=True,
        ),
    }


def _exit_policy_fields(
    simulator: _SimulatorPolicy,
) -> dict[str, dict[str, object]]:
    tunable = PolicyClassification.ACTIVE_TUNABLE_POLICY
    fixed = PolicyClassification.ACTIVE_FIXED_POLICY
    return {
        "stop_loss_pct": _field(
            simulator.stop_loss_pct,
            tunable,
            "core.backtest_engine.PortfolioSimulator.stop_loss_pct",
            optimizer_candidate=True,
        ),
        "breakeven_trigger_pct": _field(
            simulator.breakeven_trigger_pct,
            tunable,
            "core.backtest_engine.PortfolioSimulator.breakeven_trigger_pct",
            optimizer_candidate=True,
        ),
        "ema_period": _field(
            simulator.ma_exit_period,
            tunable,
            "core.backtest_engine.PortfolioSimulator.ma_exit_period",
            optimizer_candidate=True,
        ),
        "ema_consecutive_closes_below": _field(
            simulator.ma_consecutive,
            tunable,
            "core.backtest_engine.PortfolioSimulator.ma_consecutive",
            optimizer_candidate=True,
        ),
        "stagnation_days": _field(
            simulator.stagnation_days,
            tunable,
            "core.backtest_engine.PortfolioSimulator.stagnation_days",
            optimizer_candidate=True,
        ),
        "stagnation_threshold_pct": _field(
            simulator.stagnation_threshold_pct,
            tunable,
            "core.backtest_engine.PortfolioSimulator.stagnation_threshold_pct",
            optimizer_candidate=True,
        ),
        "early_winner_rule": _field(
            {
                "trigger_gain_pct": 0.20,
                "trigger_within_trading_days_inclusive": 15,
                "effect": "suppress_tiered_scale_outs_only_while_flag_is_active",
                "flag_clear_timing": "start_of_processing_when_days_held_greater_than_or_equal_to_40",
                "exits_remaining_active": [
                    "hard_stop",
                    "stagnation",
                    "protective_stop",
                    "ema",
                ],
            },
            fixed,
            "core.backtest_engine.PortfolioSimulator._check_exits",
            optimizer_candidate=True,
        ),
        "scale_out_tiers": _field(
            settings.SCALE_OUT_TIERS,
            fixed,
            "config.settings.SCALE_OUT_TIERS",
            optimizer_candidate=True,
        ),
    }


def _unsupported_request_fields(
    simulator: _SimulatorPolicy,
) -> dict[str, dict[str, object]]:
    unsupported = PolicyClassification.ADVISORY_OR_UNSUPPORTED
    fields = {
        "min_rs_score": _field(
            simulator.min_rs_score,
            unsupported,
            "core.canslim.entry_contract.MIN_RS_SCORE",
            optimizer_candidate=True,
        ),
        "min_canslim_score": _field(
            simulator.min_canslim_score,
            unsupported,
            "core.canslim.entry_contract.MIN_COMPOSITE_SCORE",
            optimizer_candidate=True,
        ),
        "min_technical_score": _field(
            simulator.min_technical_score,
            unsupported,
            "core.canslim.entry_contract.evaluate_entry_contract",
            optimizer_candidate=True,
        ),
        "position_size_pct": _field(
            simulator.position_size_pct,
            unsupported,
            "core.backtest_engine.PortfolioSimulator._enter_position",
            optimizer_candidate=True,
        ),
        "take_profit_pct": _field(
            simulator.take_profit_pct,
            unsupported,
            "config.settings.SCALE_OUT_TIERS",
            optimizer_candidate=True,
        ),
        "scale_out_fraction": _field(
            simulator.scale_out_fraction,
            unsupported,
            "config.settings.SCALE_OUT_TIERS",
            optimizer_candidate=True,
        ),
    }
    if not simulator._strategy_was_injected:
        fields["min_c_a_growth"] = _field(
            simulator.strategy.min_c_a_growth,
            unsupported,
            "core.canslim.entry_contract.MIN_CURRENT_GROWTH_and_MIN_ANNUAL_GROWTH",
            optimizer_candidate=True,
        )
    return fields


def build_effective_engine_policy(simulator: _SimulatorPolicy) -> dict[str, object]:
    """Build an immutable-by-convention policy snapshot from initialized settings."""

    policy: dict[str, object] = {
        "schema_version": 1,
        "strategy_basis": "CANSLIM-derived",
        "optimization_objective_owner": "multi_agent_backtest_loop",
        "optimization_objective": "maximize_return_and_minimize_drawdown",
        "optimization_executed_by_this_build": False,
        "causal_invariants": _causal_invariant_fields(simulator),
        "entry_policy": _entry_policy_fields(simulator),
        "scoring_policy": _scoring_policy_fields(simulator),
        "market_policy": _market_policy_fields(simulator),
        "capacity_and_sizing_policy": _capacity_and_sizing_fields(simulator),
        "exit_policy": _exit_policy_fields(simulator),
        "unsupported_requests": _unsupported_request_fields(simulator),
    }
    return json.loads(_canonical_json_bytes(policy).decode("utf-8"))


def effective_engine_policy_sha256(policy: Mapping[str, object]) -> str:
    """Return the SHA-256 digest of canonical UTF-8 policy JSON."""

    return hashlib.sha256(_canonical_json_bytes(policy)).hexdigest()
