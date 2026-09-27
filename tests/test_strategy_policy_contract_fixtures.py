"""Small deterministic evidence fixtures for the shared #80 policy contract."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from core.backtest_engine import PortfolioSimulator, Trade
from core.pit_feature_snapshot import HoldingFeaturesV3
from core.pit_optimizer_v5.policy_scope import (
    EDITABLE_POLICY_PATHS_V5,
    POLICY_INTERFACE_VERSION_V5,
)
from core.strategy_policy import (
    POLICY_INTERFACE_VERSION_V3,
    BenchmarkContextV1,
    MarketContextV1,
)
from core.strategy_policy.adapter_v3 import StrategyPolicyAdapterV3
from core.strategy_policy.contracts_v3 import (
    AddOnDecisionV3,
    validate_add_on_decision,
)
from core.strategy_policy.v3.position import evaluate_add_on


_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "strategy_policy_contract_v1.json"
_CONTRACT_PATH = Path(__file__).parents[1] / "docs" / "strategy-policy-contract-v1.md"


def _fixture() -> dict[str, object]:
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _market_context(raw: dict[str, object]) -> MarketContextV1:
    values = dict(raw)
    values["benchmarks"] = tuple(
        BenchmarkContextV1(**benchmark)
        for benchmark in values["benchmarks"]  # type: ignore[index,union-attr]
    )
    return MarketContextV1(**values)  # type: ignore[arg-type]


def test_fixture_versions_and_v5_policy_authoring_scope_match_source() -> None:
    identity = _fixture()["identity"]
    assert isinstance(identity, dict)
    assert identity["optimizer_version"] == 5
    assert identity["policy_interface_version"] == POLICY_INTERFACE_VERSION_V3 == 3
    assert POLICY_INTERFACE_VERSION_V5 == POLICY_INTERFACE_VERSION_V3
    assert identity["v5_editable_policy_paths"] == list(EDITABLE_POLICY_PATHS_V5)
    assert identity["historical_data_format_version"] in {2, 3}


def test_source_availability_fixture_does_not_double_shift_normalized_date() -> None:
    availability = _fixture()["source_availability"]
    assert isinstance(availability, dict)
    source_date = date.fromisoformat(availability["source_public_date"])
    eligible_date = date.fromisoformat(availability["first_eligible_session"])
    assert eligible_date > source_date
    assert availability["normalized_public_date_is_already_shifted"] is True
    assert availability["expected_policy_session"] == availability["first_eligible_session"]


def test_missingness_fixture_matches_contract_and_retains_detailed_reasons() -> None:
    example = _fixture()
    cases = example["missingness_projection"]
    assert isinstance(cases, list)
    expected_pairs = [
        (case["detail_state"], case["capability_state"])
        for case in cases
    ]
    contract = _CONTRACT_PATH.read_text(encoding="utf-8")
    mapping_section = contract.split("**#80 missingness projection after #66 owner review:**", 1)[1]
    mapping_section = mapping_section.split("### 2.2", 1)[0]
    contract_pairs = []
    for line in mapping_section.splitlines():
        if not line.startswith("| `"):
            continue
        columns = [column.strip() for column in line.strip("|").split("|")]
        if len(columns) >= 2:
            contract_pairs.append(
                (columns[0].strip("`"), columns[1].strip("`").strip())
            )
    assert expected_pairs == contract_pairs
    assert [state for state, capability in expected_pairs if capability == "unavailable"] == [
        "absent",
        "unsupported_scope",
        "insufficient_history",
        "invalid",
    ]
    assert "not_applicable" not in [capability for _state, capability in expected_pairs]
    additional = example["additional_boundary_outcomes"]
    assert isinstance(additional, list)
    applicability = next(item for item in additional if item["capability_state"] == "not_applicable")
    assert "explicit versioned applicability rule" in applicability["precondition"]
    calculation = next(item for item in additional if item["capability_state"] == "calculation_failure")
    assert "valid source inputs" in calculation["precondition"]
    assert calculation["detail"]["source_state"] == "observed"
    observed = next(case for case in cases if case["detail_state"] == "observed")
    assert observed["value"] == 0.0
    assert observed["detail"]["source_id"]
    not_yet_public = next(case for case in cases if case["detail_state"] == "not_yet_public")
    assert not_yet_public["detail"]["applies_to"] == "expected_source_observation"
    assert not_yet_public["detail"]["as_of_feature_may_remain_present_from_prior_observation"]


def test_current_v3_portfolio_fixture_reconciles_with_trusted_adapter() -> None:
    example = _fixture()["current_v3_portfolio_reconciliation"]
    assert isinstance(example, dict)
    inputs = example["inputs"]
    expected = example["expected"]
    assert isinstance(inputs, dict) and isinstance(expected, dict)

    result = StrategyPolicyAdapterV3.build_portfolio_features(**inputs)

    assert result.gross_exposure_fraction == pytest.approx(expected["gross_exposure_fraction"])
    assert result.drawdown_fraction == pytest.approx(expected["drawdown_fraction"])
    assert result.open_risk_fraction == pytest.approx(expected["open_risk_fraction"])
    assert result.pending_entry_count == expected["pending_entry_count"]
    for actual, wanted in zip(result.sector_exposures, expected["sector_exposures"], strict=True):
        assert actual[0] == wanted[0]
        assert actual[1] == pytest.approx(wanted[1])
    for actual, wanted in zip(result.industry_exposures, expected["industry_exposures"], strict=True):
        assert actual[0] == wanted[0]
        assert actual[1] == pytest.approx(wanted[1])
    assert type(result).from_canonical_json(result.to_canonical_json()) == result

    inconsistent = dict(inputs)
    inconsistent["cash"] = inputs["cash"] - 1.0
    with pytest.raises(ValueError, match="cash and gross"):
        StrategyPolicyAdapterV3.build_portfolio_features(**inconsistent)


def test_current_v3_add_on_fixture_keeps_baseline_choice_separate_from_host_validation() -> None:
    example = _fixture()
    case = example["current_v3_add_on_baseline"]
    assert isinstance(case, dict)
    raw = case["inputs"]
    expected = case["expected"]
    assert isinstance(raw, dict) and isinstance(expected, dict)
    inputs = dict(raw)
    inputs["market"] = _market_context(example["market_context"])
    inputs["features"] = HoldingFeaturesV3(**inputs["features"])

    snapshot = StrategyPolicyAdapterV3.build_add_on_snapshot(**inputs)
    assert snapshot.current_notional_fraction == pytest.approx(
        expected["current_notional_fraction"]
    )
    assert snapshot.remaining_cash_fraction == pytest.approx(
        expected["remaining_cash_fraction"]
    )
    baseline_decision = evaluate_add_on(snapshot)
    assert baseline_decision.to_primitive() == expected["baseline_decision"]
    assert validate_add_on_decision(snapshot, baseline_decision) == baseline_decision

    host_validated_add = validate_add_on_decision(
        snapshot,
        AddOnDecisionV3(True, 0.005, 0.30, "fixture_add"),
    )
    assert host_validated_add.add is True


def test_current_v5_exit_quantity_fixture_includes_prior_completed_add_ons() -> None:
    case = _fixture()["current_v5_exit_quantity_basis"]
    assert isinstance(case, dict)
    simulator = PortfolioSimulator(stagnation_days=999)
    trade = Trade(
        "AAA",
        "2026-01-02",
        105.0,
        case["trade_quantity_at_exit_decision"],
        97.0,
    )
    base = simulator._build_exit_snapshot(
        trade=trade,
        current_high=120.0,
        current_low=100.0,
        current_close=110.0,
        history_session_count=50,
        ema_today=107.0,
        consecutive_closes_below_ema=False,
        protective_stop_candidates=(97.0,),
        market=_market_context(_fixture()["market_context"]),
    )
    trade.peak_close = 120.0
    v3_snapshot = StrategyPolicyAdapterV3.build_exit_snapshot(
        base=simulator._build_exit_snapshot(
            trade=trade,
            current_high=120.0,
            current_low=100.0,
            current_close=110.0,
            history_session_count=50,
            ema_today=107.0,
            consecutive_closes_below_ema=False,
            protective_stop_candidates=(97.0,),
            market=_market_context(_fixture()["market_context"]),
        ),
        features=HoldingFeaturesV3(78.0, 72.0, 0.04, 1.3),
        equity=10000.0,
        lowest_price=100.0,
    )
    assert base.original_qty == case["exit_snapshot_original_qty"]
    assert v3_snapshot.base.original_qty == case["exit_snapshot_original_qty"]
    assert base.original_qty == (
        case["first_entry_filled_shares"]
        + case["later_completed_add_on_fill_shares"]
    )
    assert base.original_qty * case["tier_fraction_of_exit_snapshot_quantity"] == (
        case["simulator_scale_out_shares"]
    )


def test_proposed_paper_lifecycle_fixture_is_arithmetically_complete_not_runtime_evidence() -> None:
    examples = _fixture()["proposed_paper_lifecycle_examples"]
    assert isinstance(examples, dict)
    assert examples["status"] == (
        "deterministic_contract_examples_only_not_current_v3_or_paper_adapter_support"
    )

    reservation = examples["pending_entry_partial_fill_cancel_restart"]
    commitment = reservation["commitment"]
    partial = reservation["after_partial_fill"]
    restarted = reservation["after_restart_reconciliation"]
    cancelled = reservation["after_terminal_cancel"]
    assert partial["accounted_total"]["notional"] == commitment["notional"]
    assert partial["accounted_total"]["risk"] == commitment["risk"]
    assert restarted == {
        "confirmed_filled": partial["confirmed_filled"],
        "remaining_reservation": partial["remaining_reservation"],
    }
    assert cancelled["confirmed_filled"]["notional"] + cancelled["released"]["notional"] == commitment["notional"]
    assert cancelled["confirmed_filled"]["risk"] + cancelled["released"]["risk"] == commitment["risk"]

    scale_out = examples["original_quantity_scale_out_partial_fill_restart"]
    assert scale_out["status"] == "proposed_snapshot_quantity_basis_pending_paper_adapter_evidence"
    assert scale_out["decision_snapshot_filled_shares_including_prior_add_ons"] * scale_out[
        "tier_fraction_of_snapshot_quantity"
    ] == scale_out["target_shares"]
    assert (
        scale_out["first_confirmed_fill_shares"]
        + scale_out["residual_shares_after_restart_reconciliation"]
        == scale_out["target_shares"]
    )
    assert scale_out["final_confirmed_fill_shares"] == scale_out["target_shares"]
