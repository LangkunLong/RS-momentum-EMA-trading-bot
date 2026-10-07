from decimal import Decimal

from core.pit_policy_parity import ActionParityDisposition, compare_action_parity_cases
from core.policy_addition import AdditionStepKind, start_addition
from core.policy_execution_store import PolicyExecutionStateStore
from core.policy_exits import start_full_exit, start_scale_out
from tests.test_policy_addition import _addition_fixture
from tests.test_policy_exits import _exit_decision, _fixture


def _policy_identity(decision):
    deployment = decision.deployment_identity
    return {
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
    }


def _decision_fields(decision, *, action_family, **policy_fields):
    clock = decision.clock
    fields = {
        "action_family": action_family,
        "decision_id": decision.decision_id,
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
    }
    fields.update(policy_fields)
    return fields


def _case(decision, *, facts, decision_fields, intent, pending_state):
    return {
        "policy_identity": _policy_identity(decision),
        "facts": facts,
        "decision": decision_fields,
        "intent": intent,
        "execution": {
            "fill_price": None,
            "fees": None,
            "pending_state": pending_state,
        },
    }


def _paper_intent(action, *, action_family):
    values = {
        "action_family": action_family,
        "role": action.role.value,
        "side": action.side.value,
        "security_id": action.security_id,
        "broker_symbol": action.broker_symbol,
        "requested_quantity": action.requested_quantity,
        "holding_episode_id": action.holding_episode_id,
        "reservation_price": action.reservation_price,
        "reservation_price_basis": action.reservation_price_basis,
        "reservation_stop_price": action.reservation_stop_price,
        "risk_per_unit": action.risk_per_unit,
        "risk_basis": action.risk_basis,
        "snapshot_original_quantity": action.snapshot_original_quantity,
        "fraction_of_original_quantity": action.fraction_of_original_quantity,
        "exit_tier": action.exit_tier,
        "rounding_rule_id": action.rounding_rule_id,
    }
    return {key: value for key, value in values.items() if value is not None}


def _holding_facts(holding):
    values = {
        "holding_episode_id": holding.holding_episode_id,
        "security_id": holding.security_id,
        "broker_symbol": holding.broker_symbol,
        "original_quantity": (
            holding.initial_filled_quantity
            + holding.opening_later_fills_quantity
            + holding.completed_additions_quantity
        ),
        "remaining_quantity": holding.remaining_quantity,
        "holding_state_version": holding.state_version,
        "last_exit_tier": holding.last_exit_tier,
        "confirmed_stop_price": holding.confirmed_protective_stop_price,
    }
    return {key: value for key, value in values.items() if value is not None}


def test_recorded_addition_decision_matches_persisted_paper_intent_after_restart(tmp_path):
    store, plan, deployment, account, portfolio, holding = _addition_fixture(tmp_path)
    first = start_addition(store, plan, account=account, portfolio_snapshot=portfolio)
    assert first.kind is AdditionStepKind.BUY_DUE
    assert first.action is not None

    restarted = PolicyExecutionStateStore(store.db_path, store_identity=deployment.store_identity)
    replay = start_addition(restarted, plan, account=account, portfolio_snapshot=portfolio)
    assert replay.kind is AdditionStepKind.BUY_DUE
    assert replay.action is not None
    assert replay.action.logical_action_id == first.action.logical_action_id

    snapshot = plan.add_on_snapshot
    features = snapshot.features
    facts = {
        **_holding_facts(holding),
        "entry_price": Decimal(str(snapshot.entry_price)),
        "current_price": Decimal(str(snapshot.current_price)),
        "current_quantity": Decimal(str(snapshot.current_quantity)),
        "current_notional_fraction": Decimal(str(snapshot.current_notional_fraction)),
        "days_held": snapshot.days_held,
        "add_on_count": snapshot.add_on_count,
        "remaining_cash_fraction": Decimal(str(snapshot.remaining_cash_fraction)),
        "open_position_risk_fraction": Decimal(str(snapshot.open_position_risk_fraction)),
        "current_rs_score": Decimal(str(features.current_rs_score)),
        "industry_group_rs": Decimal(str(features.industry_group_rs)),
        "atr_20_fraction": Decimal(str(features.atr_20_fraction)),
        "volume_ratio": Decimal(str(features.volume_ratio)),
    }
    decision_fields = _decision_fields(
        plan.decision,
        action_family="addition",
        add=plan.policy_decision.add,
        risk_fraction=Decimal(str(plan.policy_decision.risk_fraction)),
        notional_fraction_cap=Decimal(str(plan.policy_decision.notional_fraction_cap)),
        reason_code=plan.policy_decision.reason_code,
    )
    expected_intent = {
        "action_family": "addition",
        "role": "addition",
        "side": "buy",
        "security_id": holding.security_id,
        "broker_symbol": holding.broker_symbol,
        "requested_quantity": Decimal("2"),
        "holding_episode_id": holding.holding_episode_id,
        "reservation_price": Decimal("110"),
        "reservation_price_basis": f"decision_time_mark:{account.account_snapshot_id}",
        "reservation_stop_price": Decimal("90"),
        "risk_per_unit": Decimal("20"),
        "risk_basis": f"confirmed_stop:{holding.confirmed_stop_action_id}",
    }

    result = compare_action_parity_cases(
        _case(
            plan.decision,
            facts=facts,
            decision_fields=decision_fields,
            intent=expected_intent,
            pending_state="buy_due",
        ),
        _case(
            plan.decision,
            facts=facts,
            decision_fields=decision_fields,
            intent=_paper_intent(replay.action, action_family="addition"),
            pending_state=replay.kind.value,
        ),
    )

    assert result.disposition is ActionParityDisposition.INCOMPLETE
    assert result.mismatches == ()
    assert result.unknowns == ("execution.fees", "execution.fill_price")


def test_recorded_partial_exit_matches_durable_scale_out_intent_after_restart(tmp_path):
    features, store, deployment, portfolio, holding = _fixture(tmp_path)
    decision = _exit_decision(features, deployment, portfolio, holding)
    policy_fields = {
        "action_family": "scale_out",
        "fraction_of_original_quantity": Decimal("0.5"),
        "quantity_increment": Decimal("1"),
        "rounding_rule_id": "whole_share_floor_v1",
        "exit_tier": 1,
    }
    guard_fields = {"outcome": "allow_offline_fixture"}
    action = start_scale_out(
        store,
        decision=decision,
        holding_episode_id=holding.holding_episode_id,
        fraction_of_original_quantity=Decimal("0.5"),
        quantity_increment=Decimal("1"),
        rounding_rule_id="whole_share_floor_v1",
        expected_holding_version=holding.state_version,
        policy_payload={"tier": 1},
        guard_payload=guard_fields,
    )
    restarted = PolicyExecutionStateStore(store.db_path, store_identity=deployment.store_identity)
    replay = start_scale_out(
        restarted,
        decision=decision,
        holding_episode_id=holding.holding_episode_id,
        fraction_of_original_quantity=Decimal("0.5"),
        quantity_increment=Decimal("1"),
        rounding_rule_id="whole_share_floor_v1",
        expected_holding_version=holding.state_version,
        policy_payload={"tier": 1},
        guard_payload=guard_fields,
    )
    assert replay.logical_action_id == action.logical_action_id
    facts = _holding_facts(holding)
    decision_fields = _decision_fields(decision, **policy_fields)
    expected_intent = {
        "action_family": "scale_out",
        "role": "scale_out",
        "side": "sell",
        "security_id": holding.security_id,
        "broker_symbol": holding.broker_symbol,
        "requested_quantity": Decimal("3"),
        "holding_episode_id": holding.holding_episode_id,
        "snapshot_original_quantity": Decimal("6"),
        "fraction_of_original_quantity": Decimal("0.5"),
        "exit_tier": 1,
        "rounding_rule_id": "whole_share_floor_v1",
    }

    result = compare_action_parity_cases(
        _case(
            decision,
            facts=facts,
            decision_fields=decision_fields,
            intent=expected_intent,
            pending_state="intended",
        ),
        _case(
            decision,
            facts=facts,
            decision_fields=decision_fields,
            intent=_paper_intent(replay, action_family="scale_out"),
            pending_state=replay.status.value,
        ),
    )

    assert result.disposition is ActionParityDisposition.INCOMPLETE
    assert result.mismatches == ()
    assert result.unknowns == ("execution.fees", "execution.fill_price")


def test_recorded_full_exit_matches_durable_close_intent_after_restart(tmp_path):
    features, store, deployment, portfolio, holding = _fixture(tmp_path)
    decision = _exit_decision(features, deployment, portfolio, holding)
    policy_fields = {"action_family": "close", "exit_rule": "full_exit"}
    guard_fields = {"outcome": "allow_offline_fixture"}
    action = start_full_exit(
        store,
        decision=decision,
        holding_episode_id=holding.holding_episode_id,
        expected_holding_version=holding.state_version,
        policy_payload={"reason": "full_exit"},
        guard_payload=guard_fields,
    )
    restarted = PolicyExecutionStateStore(store.db_path, store_identity=deployment.store_identity)
    replay = start_full_exit(
        restarted,
        decision=decision,
        holding_episode_id=holding.holding_episode_id,
        expected_holding_version=holding.state_version,
        policy_payload={"reason": "full_exit"},
        guard_payload=guard_fields,
    )
    assert replay.logical_action_id == action.logical_action_id
    facts = _holding_facts(holding)
    decision_fields = _decision_fields(decision, **policy_fields)
    expected_intent = {
        "action_family": "close",
        "role": "close",
        "side": "sell",
        "security_id": holding.security_id,
        "broker_symbol": holding.broker_symbol,
        "requested_quantity": Decimal("6"),
        "holding_episode_id": holding.holding_episode_id,
    }

    result = compare_action_parity_cases(
        _case(
            decision,
            facts=facts,
            decision_fields=decision_fields,
            intent=expected_intent,
            pending_state="intended",
        ),
        _case(
            decision,
            facts=facts,
            decision_fields=decision_fields,
            intent=_paper_intent(replay, action_family="close"),
            pending_state=replay.status.value,
        ),
    )

    assert result.disposition is ActionParityDisposition.INCOMPLETE
    assert result.mismatches == ()
    assert result.unknowns == ("execution.fees", "execution.fill_price")


def _complete_addition_case():
    return {
        "policy_identity": {
            "policy_artifact_id": "policy:v1",
            "capability_manifest_id": "capabilities:v1",
            "policy_interface_version": "3",
            "feature_contract_id": "features:v1",
            "feature_calculator_id": "calculator:v1",
            "source_revision": "a" * 40,
            "runtime_identity": "offline-test",
            "execution_profile_id": "paper-profile",
            "paper_account_environment_id": "paper-account",
            "store_identity": "store:v1",
        },
        "facts": {
            "holding_episode_id": "holding:1",
            "security_id": "security:ABC",
            "broker_symbol": "ABC",
            "original_quantity": Decimal("4"),
            "remaining_quantity": Decimal("4"),
            "holding_state_version": 3,
            "entry_price": Decimal("100"),
            "current_price": Decimal("110"),
            "current_quantity": Decimal("4"),
            "current_notional_fraction": Decimal("0.1"),
            "days_held": 4,
            "add_on_count": 0,
            "remaining_cash_fraction": Decimal("0.5"),
            "open_position_risk_fraction": Decimal("0.02"),
            "current_rs_score": Decimal("80"),
            "industry_group_rs": Decimal("70"),
            "atr_20_fraction": Decimal("0.04"),
            "volume_ratio": Decimal("1.5"),
        },
        "decision": {
            "action_family": "addition",
            "decision_id": "decision:1",
            "exchange_id": "XNYS",
            "decision_session": "2025-02-04",
            "as_of_cutoff_at": "2025-02-04T20:00:00+00:00",
            "next_execution_session": "2025-02-05",
            "account_valuation_session": "2025-02-04",
            "account_valuation_at": "2025-02-04T20:00:00+00:00",
            "snapshot_sha256": "b" * 64,
            "category": "addition",
            "subject_type": "security",
            "subject_id": "security:ABC",
            "add": True,
            "risk_fraction": Decimal("0.01"),
            "notional_fraction_cap": Decimal("0.2"),
            "reason_code": "relative_strength",
        },
        "intent": {
            "action_family": "addition",
            "role": "addition",
            "side": "buy",
            "security_id": "security:ABC",
            "broker_symbol": "ABC",
            "requested_quantity": Decimal("2"),
            "holding_episode_id": "holding:1",
            "reservation_price": Decimal("110"),
            "reservation_price_basis": "decision_time_mark:snapshot:1",
            "reservation_stop_price": Decimal("90"),
            "risk_per_unit": Decimal("20"),
            "risk_basis": "confirmed_stop:stop:1",
        },
        "execution": {
            "fill_price": Decimal("110"),
            "fees": Decimal("0"),
            "pending_state": "buy_due",
        },
    }


def test_equal_sparse_action_records_are_incomplete():
    sparse = {
        "policy_identity": {"policy_artifact_id": "policy:v1"},
        "facts": {"snapshot_sha256": "b" * 64},
        "decision": {"action_family": "addition"},
        "intent": {
            "action_family": "addition",
            "role": "addition",
            "side": "buy",
            "security_id": "security:ABC",
            "broker_symbol": "ABC",
            "requested_quantity": Decimal("2"),
            "holding_episode_id": "holding:1",
            "reservation_price": Decimal("110"),
            "reservation_price_basis": "decision_time_mark:snapshot:1",
            "reservation_stop_price": Decimal("90"),
            "risk_per_unit": Decimal("20"),
            "risk_basis": "confirmed_stop:stop:1",
        },
        "execution": {"pending_state": "buy_due"},
    }

    result = compare_action_parity_cases(sparse, sparse)

    assert result.disposition is ActionParityDisposition.INCOMPLETE
    assert "policy_identity.capability_manifest_id" in result.unknowns
    assert "decision.decision_id" in result.unknowns
    assert "facts.remaining_quantity" in result.unknowns
    assert "execution.fill_price" in result.unknowns


def test_missing_common_policy_field_on_both_sides_is_incomplete():
    historical = _complete_addition_case()
    paper = _complete_addition_case()
    del historical["policy_identity"]["store_identity"]
    del paper["policy_identity"]["store_identity"]

    result = compare_action_parity_cases(historical, paper)

    assert result.disposition is ActionParityDisposition.INCOMPLETE
    assert result.unknowns == ("policy_identity.store_identity",)


def test_complete_action_evidence_matches():
    case = _complete_addition_case()

    result = compare_action_parity_cases(case, case)

    assert result.disposition is ActionParityDisposition.MATCHED
    assert result.unknowns == ()


def test_execution_difference_remains_an_execution_variance():
    historical = _complete_addition_case()
    paper = _complete_addition_case()
    paper["execution"]["fill_price"] = Decimal("111")

    result = compare_action_parity_cases(historical, paper)

    assert result.disposition is ActionParityDisposition.MATCHED_WITH_EXECUTION_VARIANCE
    assert result.execution_variances == ("execution.fill_price",)
    assert result.mismatches == ()
