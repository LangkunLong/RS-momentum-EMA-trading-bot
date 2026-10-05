from dataclasses import replace
from decimal import Decimal

import pytest

from core.policy_execution_state import DecisionIdentity
from core.policy_execution_store import PolicyExecutionStateStore
from core.policy_replacement import (
    ReplacementStepKind,
    advance_replacement,
    load_replacement_intention,
    start_replacement,
)
from tests.issue106_replacement_parity import (
    ParityDisposition,
    compare_replacement_cases,
)
from tests.paper_policy_fixtures import build_chain_identity, build_feature_fixture
from tests.test_policy_replacement import (
    _record_fake_sell_fill,
    _refreshed_after_full_sale,
    _replacement_fixture,
    _submit_fake_sell,
)


def _comparison_case():
    return {
        "policy_identity": {
            "policy_artifact_id": "recorded-policy:replacement-fixture-v1",
            "feature_contract_id": "recorded-feature-contract:v1",
        },
        "facts": {
            "evicted_security_id": "fixture:CCC",
            "evicted_quantity": Decimal("6"),
            "candidate_security_id": "fixture:DDD",
        },
        "decision": {
            "decision_session": "2025-02-04",
            "snapshot_sha256": "a" * 64,
        },
        "intent": {
            "sell_side": "sell",
            "sell_quantity": Decimal("6"),
            "buy_price_limit": Decimal("125"),
            "sell_order_type": "market",
        },
        "execution": {
            "fill_price": Decimal("100"),
            "fees": Decimal("0"),
            "valuation_time": "2025-02-05T14:31:00+00:00",
            "pending_state": "waiting_for_sell",
        },
    }


def _recorded_history_case(features, clock):
    return {
        "policy_identity": {
            "policy_artifact_id": "fixed-policy:combined-offline-v1",
            "capability_manifest_id": "manifest:combined-offline-v1",
            "policy_interface_version": "3",
            "feature_contract_id": features.feature_contract_id,
            "feature_calculator_id": features.feature_calculator_identity,
            "source_revision": "ab385d792e19ff6db39d87f1123f47f660fc1e1d",
            "runtime_identity": "offline-test-only",
            "execution_profile_id": "synthetic-paper-profile",
            "paper_account_environment_id": "synthetic-chain-account",
            "store_identity": "synthetic-chain-store",
        },
        "facts": {
            "evicted_security_id": "fixture:CCC",
            "evicted_symbol": "CCC",
            "evicted_quantity": Decimal("6"),
            "candidate_security_id": "fixture:DDD",
            "candidate_symbol": "DDD",
        },
        "decision": {
            "exchange_id": "XNYS",
            "decision_session": clock.decision_session.isoformat(),
            "as_of_cutoff_at": clock.as_of_cutoff_at.isoformat(),
            "next_execution_session": clock.next_execution_session.isoformat(),
            "account_valuation_session": clock.account_valuation_session.isoformat(),
            "account_valuation_at": clock.account_valuation_at.isoformat(),
            "snapshot_sha256": features.recorded_input_manifest_sha256,
            "category": "replacement",
            "subject_type": "security",
            "subject_id": "fixture:DDD",
        },
        "intent": {
            "sell_role": "close",
            "sell_side": "sell",
            "sell_security_id": "fixture:CCC",
            "sell_symbol": "CCC",
            "sell_quantity": Decimal("6"),
            "buy_candidate_security_id": "fixture:DDD",
            "buy_candidate_symbol": "DDD",
            "buy_quantity_cap": Decimal("100"),
            "buy_price_limit": Decimal("125"),
            "protective_stop": Decimal("100"),
            "risk_per_unit": Decimal("25"),
            "risk_basis": "fixed_candidate_limit_minus_stop",
            "sell_order_type": None,
        },
        "execution": {
            "fill_price": None,
            "fees": None,
            "pending_state": "sell_due",
        },
    }


def _paper_replacement_case(execution, pending_state):
    plan = execution.plan
    decision = plan.decision
    deployment = decision.deployment_identity
    clock = decision.clock
    sell = execution.sell_action
    return {
        "policy_identity": {
            "policy_artifact_id": deployment.policy_artifact_id,
            "capability_manifest_id": deployment.capability_manifest_id,
            "policy_interface_version": deployment.policy_interface_version,
            "feature_contract_id": deployment.feature_contract_id,
            "feature_calculator_id": deployment.feature_calculator_id,
            "source_revision": deployment.source_revision,
            "runtime_identity": deployment.runtime_identity,
            "execution_profile_id": deployment.execution_profile_id,
            "paper_account_environment_id": deployment.paper_account_environment_id,
            "store_identity": deployment.store_identity,
        },
        "facts": {
            "evicted_security_id": sell.security_id,
            "evicted_symbol": sell.broker_symbol,
            "evicted_quantity": sell.requested_quantity,
            "candidate_security_id": plan.candidate_security_id,
            "candidate_symbol": plan.candidate_symbol,
        },
        "decision": {
            "exchange_id": clock.exchange_id,
            "decision_session": clock.decision_session.isoformat(),
            "as_of_cutoff_at": clock.as_of_cutoff_at.isoformat(),
            "next_execution_session": clock.next_execution_session.isoformat(),
            "account_valuation_session": clock.account_valuation_session.isoformat(),
            "account_valuation_at": clock.account_valuation_at.isoformat(),
            "snapshot_sha256": decision.snapshot_sha256,
            "category": decision.category.value,
            "subject_type": decision.subject_type.value,
            "subject_id": decision.subject_id,
        },
        "intent": {
            "sell_role": sell.role.value,
            "sell_side": sell.side.value,
            "sell_security_id": sell.security_id,
            "sell_symbol": sell.broker_symbol,
            "sell_quantity": sell.requested_quantity,
            "buy_candidate_security_id": plan.candidate_security_id,
            "buy_candidate_symbol": plan.candidate_symbol,
            "buy_quantity_cap": plan.maximum_buy_quantity,
            "buy_price_limit": plan.maximum_buy_price,
            "protective_stop": plan.reservation_stop_price,
            "risk_per_unit": plan.risk_per_unit,
            "risk_basis": plan.risk_basis,
            "sell_order_type": None,
        },
        "execution": {
            "fill_price": None,
            "fees": None,
            "pending_state": pending_state,
        },
    }


def test_recorded_replacement_decision_and_durable_sell_intent_match_after_restart(tmp_path):
    store, plan, deployment, _, _, _ = _replacement_fixture(tmp_path)
    first = start_replacement(store, plan)
    assert first.kind is ReplacementStepKind.SELL_DUE

    restarted = PolicyExecutionStateStore(store.db_path, store_identity=deployment.store_identity)
    recovered = load_replacement_intention(restarted, plan.decision.decision_id)
    replay = advance_replacement(restarted, plan.decision.decision_id)
    assert replay.kind is ReplacementStepKind.SELL_DUE
    assert replay.action is not None
    assert recovered.sell_action.logical_action_id == replay.action.logical_action_id

    history_dir = tmp_path / "history"
    history_dir.mkdir()
    history_features = build_feature_fixture(history_dir)
    _, history_clock = build_chain_identity(history_features)
    result = compare_replacement_cases(
        _recorded_history_case(history_features, history_clock),
        _paper_replacement_case(recovered, replay.kind.value),
    )

    assert result.disposition is ParityDisposition.INCOMPLETE
    assert result.mismatches == ()
    assert set(result.unknowns) == {
        "intent.sell_order_type",
        "execution.fill_price",
        "execution.fees",
    }
    assert "decision.snapshot_sha256" in result.matched_fields
    assert "intent.sell_quantity" in result.matched_fields
    assert {
        field for field in result.matched_fields if field.startswith("intent.")
    } == {
        "intent.sell_role",
        "intent.sell_side",
        "intent.sell_security_id",
        "intent.sell_symbol",
        "intent.sell_quantity",
        "intent.buy_candidate_security_id",
        "intent.buy_candidate_symbol",
        "intent.buy_quantity_cap",
        "intent.buy_price_limit",
        "intent.protective_stop",
        "intent.risk_per_unit",
        "intent.risk_basis",
    }


def test_missing_historical_quantity_is_unknown_not_zero():
    history = _comparison_case()
    paper = _comparison_case()
    history["facts"]["evicted_quantity"] = None
    paper["facts"]["evicted_quantity"] = Decimal("0")

    result = compare_replacement_cases(history, paper)

    assert result.disposition is ParityDisposition.INCOMPLETE
    assert result.mismatches == ()
    assert result.unknowns == ("facts.evicted_quantity",)


def test_incompatible_policy_identity_is_not_compared_as_a_decision_match():
    history = _comparison_case()
    paper = _comparison_case()
    paper["policy_identity"]["policy_artifact_id"] = "recorded-policy:other"

    result = compare_replacement_cases(history, paper)

    assert result.disposition is ParityDisposition.INCOMPATIBLE_POLICY
    assert "policy_identity.policy_artifact_id" in result.incompatible_fields
    assert result.mismatches == ()


def test_execution_variances_are_separate_from_fixed_decision_and_intent_comparison():
    history = _comparison_case()
    paper = _comparison_case()
    history["execution"] = {
        "fill_price": Decimal("100"),
        "fees": Decimal("0"),
        "valuation_time": "2025-02-05T14:31:00+00:00",
        "pending_state": "waiting_for_sell",
    }
    paper["execution"] = {
        "fill_price": Decimal("101.5"),
        "fees": Decimal("2.25"),
        "valuation_time": "2025-02-05T14:33:00+00:00",
        "pending_state": "buy_due",
    }

    result = compare_replacement_cases(history, paper)

    assert result.disposition is ParityDisposition.MATCHED_WITH_EXECUTION_VARIANCE
    assert result.mismatches == ()
    assert set(result.execution_variances) == {
        "execution.fill_price",
        "execution.fees",
        "execution.valuation_time",
        "execution.pending_state",
    }


def test_policy_deployment_mismatch_is_rejected_before_persisting_replacement(tmp_path):
    store, plan, _, _, _, _ = _replacement_fixture(tmp_path)
    other_deployment = replace(
        plan.decision.deployment_identity,
        policy_artifact_id="fixed-policy:other",
    )
    other_decision = DecisionIdentity.build(
        deployment=other_deployment,
        clock=plan.decision.clock,
        snapshot_sha256=plan.decision.snapshot_sha256,
        category=plan.decision.category,
        subject_type=plan.decision.subject_type,
        subject_id=plan.decision.subject_id,
    )
    other_sell = replace(plan.sell_action, decision=other_decision)
    other_plan = replace(plan, decision=other_decision, sell_action=other_sell)

    with pytest.raises(ValueError, match="replacement portfolio does not match the decision deployment"):
        start_replacement(store, other_plan)
    with pytest.raises(KeyError):
        store.load_decision_record(other_decision.decision_id)


def test_missing_fresh_position_and_order_facts_keep_buy_unissued(tmp_path):
    store, plan, deployment, account, portfolio, _ = _replacement_fixture(tmp_path)
    start_replacement(store, plan)
    _submit_fake_sell(store, plan, plan.decision.clock.account_valuation_at)
    _record_fake_sell_fill(
        store,
        plan,
        str(plan.sell_action.requested_quantity),
        observed_at=plan.decision.clock.account_valuation_at,
    )
    refreshed_account, refreshed_portfolio = _refreshed_after_full_sale(
        store, account, portfolio, plan
    )
    unknown_account = replace(refreshed_account, positions=None, open_orders=None)
    restarted = PolicyExecutionStateStore(store.db_path, store_identity=deployment.store_identity)

    step = advance_replacement(
        restarted,
        plan.decision.decision_id,
        account=unknown_account,
        portfolio_snapshot=refreshed_portfolio,
        candidate_price=Decimal("100"),
    )

    assert step.kind is ReplacementStepKind.WAITING_FOR_RECONCILIATION
    assert step.action is None
    assert load_replacement_intention(restarted, plan.decision.decision_id).buy_action is None


def test_replay_after_fresh_reconciliation_returns_the_same_single_buy_intention(tmp_path):
    store, plan, deployment, account, portfolio, _ = _replacement_fixture(tmp_path)
    start_replacement(store, plan)
    _submit_fake_sell(store, plan, plan.decision.clock.account_valuation_at)
    _record_fake_sell_fill(
        store,
        plan,
        str(plan.sell_action.requested_quantity),
        observed_at=plan.decision.clock.account_valuation_at,
    )
    refreshed_account, refreshed_portfolio = _refreshed_after_full_sale(
        store, account, portfolio, plan
    )
    after_sale = PolicyExecutionStateStore(store.db_path, store_identity=deployment.store_identity)

    first = advance_replacement(
        after_sale,
        plan.decision.decision_id,
        account=refreshed_account,
        portfolio_snapshot=refreshed_portfolio,
        candidate_price=Decimal("100"),
    )
    assert first.kind is ReplacementStepKind.BUY_DUE
    assert first.action is not None
    assert first.action.requested_quantity == Decimal("64")

    after_buy_restart = PolicyExecutionStateStore(store.db_path, store_identity=deployment.store_identity)
    replay = advance_replacement(
        after_buy_restart,
        plan.decision.decision_id,
        account=refreshed_account,
        portfolio_snapshot=refreshed_portfolio,
        candidate_price=Decimal("100"),
    )
    recovered = load_replacement_intention(after_buy_restart, plan.decision.decision_id)

    assert replay.kind is ReplacementStepKind.BUY_DUE
    assert replay.action is not None
    assert replay.action.logical_action_id == first.action.logical_action_id
    assert replay.action.requested_quantity == first.action.requested_quantity
    assert recovered.buy_action is not None
    assert recovered.buy_action.logical_action_id == first.action.logical_action_id





def test_timezone_offset_spelling_does_not_change_the_decision_instant():
    history = _comparison_case()
    paper = _comparison_case()
    history["decision"]["as_of_cutoff_at"] = "2026-03-31T16:00:00-04:00"
    paper["decision"]["as_of_cutoff_at"] = "2026-03-31T20:00:00+00:00"

    result = compare_replacement_cases(history, paper)

    assert result.disposition is ParityDisposition.MATCHED
    assert result.mismatches == ()
    assert "decision.as_of_cutoff_at" in result.matched_fields


@pytest.mark.parametrize(
    ("field", "paper_value", "expected_mismatches"),
    [
        pytest.param(
            "sell_quantity",
            Decimal("5"),
            ("intent.sell_quantity",),
            id="sell-quantity",
        ),
        pytest.param(
            "buy_price_limit",
            Decimal("125.01"),
            ("intent.buy_price_limit",),
            id="one-cent-buy-limit-change",
        ),
    ],
)
def test_fixed_replacement_action_difference_is_a_mismatch(
    field, paper_value, expected_mismatches
):
    history = _comparison_case()
    paper = _comparison_case()
    paper["intent"][field] = paper_value

    result = compare_replacement_cases(history, paper)

    assert result.disposition is ParityDisposition.MISMATCH
    assert result.mismatches == expected_mismatches
    assert result.execution_variances == ()


def test_decimal_scale_does_not_change_replacement_action_parity():
    history = _comparison_case()
    paper = _comparison_case()
    history["intent"]["sell_quantity"] = Decimal("6.0")
    paper["intent"]["sell_quantity"] = Decimal("6.00")

    result = compare_replacement_cases(history, paper)

    assert result.disposition is ParityDisposition.MATCHED
    assert result.mismatches == ()
    assert "intent.sell_quantity" in result.matched_fields





def test_paper_only_fixed_field_is_unknown_without_a_historical_baseline():
    history = _comparison_case()
    paper = _comparison_case()
    paper["decision"]["extra_action"] = "SELL"

    result = compare_replacement_cases(history, paper)

    assert result.disposition is ParityDisposition.INCOMPLETE
    assert result.unknowns == ("decision.extra_action",)
    assert result.mismatches == ()


@pytest.mark.parametrize(
    ("history_value", "paper_value"),
    [
        pytest.param({"fixed": 1}, {"fixed": True}, id="mapping-bool-versus-number"),
        pytest.param([Decimal("1.0")], [Decimal("1.00")], id="sequence"),
    ],
)
def test_nested_parity_values_are_rejected_explicitly(history_value, paper_value):
    history = _comparison_case()
    paper = _comparison_case()
    history["facts"]["shares"] = history_value
    paper["facts"]["shares"] = paper_value

    with pytest.raises(TypeError, match="nested parity values are unsupported"):
        compare_replacement_cases(history, paper)


@pytest.mark.parametrize(
    "group",
    ("policy_identity", "facts", "decision", "intent", "execution"),
)
def test_empty_evidence_group_is_unknown_instead_of_matched(group):
    history = _comparison_case()
    paper = _comparison_case()
    history[group] = {}
    paper[group] = {}

    result = compare_replacement_cases(history, paper)

    assert result.disposition is ParityDisposition.INCOMPLETE
    assert group in result.unknowns
