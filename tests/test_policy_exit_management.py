from dataclasses import replace
from decimal import Decimal

import pytest

from core.order_manager import OrderManager
from core.policy_execution_state import DecisionCategory, DecisionIdentity, DecisionSubjectType
from core.policy_execution_store import DecisionConflictError, PolicyExecutionStateStore
from core.policy_exits import start_scale_out
from core.strategy_policy.exit import evaluate_exit
from tests.test_paper_policy_chain import read_chain, seed_pending_chain
from tests.test_policy_exits import _exit_decision
from tests.test_strategy_policy import _exit_snapshot


def _case(tmp_path):
    features, path, deployment, _, portfolio, _, _ = seed_pending_chain(tmp_path)
    store = PolicyExecutionStateStore(path, store_identity=deployment.store_identity)
    chain = read_chain(store, deployment, portfolio)
    holding = next(item for item in chain.holding_episodes if item.broker_symbol == "CCC")
    decision = _exit_decision(features, deployment, portfolio, holding)
    snapshot = _exit_snapshot(
        entry_price=float(holding.entry_price),
        original_qty=6.0,
        remaining_qty=6.0,
        stop_price=float(holding.confirmed_protective_stop_price),
        peak_close=125.0,
        current_close=125.0,
        current_high=125.0,
        protective_stop_candidates=(90.0, 100.0, 106.0),
    )
    return features, store, deployment, portfolio, holding, decision, snapshot


def test_fixed_winner_and_trailing_state_survives_restart_and_release(tmp_path):
    features, store, deployment, portfolio, holding, decision, snapshot = _case(tmp_path)
    outcome = evaluate_exit(snapshot)
    assert outcome.actions == ()
    assert outcome.early_winner_hold and outcome.breakeven_armed and outcome.ema_trailing_active
    manager = OrderManager(paper=True, policy_store=store)
    saved = manager.record_policy_exit_management(
        decision=decision, holding_episode_id=holding.holding_episode_id,
        snapshot=snapshot, outcome=outcome, expected_holding_version=holding.state_version,
    )
    flags = dict(saved.policy_flags)
    assert flags["early_winner_hold"] == "true"
    assert flags["breakeven_armed"] == "true"
    assert flags["ema_trailing_active"] == "true"
    assert saved.peak_price == Decimal("125.0")
    assert saved.confirmed_protective_stop_price == holding.confirmed_protective_stop_price
    assert store.load_decision_record(decision.decision_id).effective_action_payload["next_stop_price"] == 106.0

    restarted_store = PolicyExecutionStateStore(store.db_path, store_identity=store.store_identity)
    restarted = OrderManager(paper=True, policy_store=restarted_store)
    replay = restarted.record_policy_exit_management(
        decision=decision, holding_episode_id=holding.holding_episode_id,
        snapshot=snapshot, outcome=outcome, expected_holding_version=holding.state_version,
    )
    assert replay == saved
    with pytest.raises(DecisionConflictError, match="different immutable decision facts"):
        restarted.record_policy_exit_management(
            decision=decision, holding_episode_id=holding.holding_episode_id,
            snapshot=snapshot, outcome=replace(outcome, ema_trailing_active=False),
            expected_holding_version=holding.state_version,
        )

    same_session = DecisionIdentity.build(
        deployment=deployment, clock=decision.clock,
        snapshot_sha256=decision.snapshot_sha256, category=DecisionCategory.EXIT,
        subject_type=DecisionSubjectType.HOLDING, subject_id=holding.holding_episode_id,
        sequence=1,
    )
    same_session_snapshot = replace(
        snapshot, early_winner_hold=True, breakeven_armed=True, ema_trailing_active=True,
    )
    with pytest.raises(DecisionConflictError, match="already owns this session"):
        restarted.record_policy_exit_management(
            decision=same_session, holding_episode_id=holding.holding_episode_id,
            snapshot=same_session_snapshot, outcome=evaluate_exit(same_session_snapshot),
            expected_holding_version=saved.state_version,
        )

    next_decision = _exit_decision(features, deployment, portfolio, saved, sequence=1)
    release_snapshot = replace(
        same_session_snapshot, days_held=40, current_close=110.0, current_high=125.0,
    )
    release_outcome = evaluate_exit(release_snapshot)
    assert release_outcome.early_winner_hold is False
    released = restarted.record_policy_exit_management(
        decision=next_decision, holding_episode_id=holding.holding_episode_id,
        snapshot=release_snapshot, outcome=release_outcome,
        expected_holding_version=saved.state_version,
    )
    assert dict(released.policy_flags)["early_winner_hold"] == "false"
    assert dict(released.policy_flags)["exit_policy_scale_out_tier"] == "2"
    assert released.last_exit_tier == 0  # Policy selection does not pretend a paper sell filled.
    assert released.confirmed_protective_stop_price == holding.confirmed_protective_stop_price
    action_decision = DecisionIdentity.build(
        deployment=deployment, clock=next_decision.clock,
        snapshot_sha256=next_decision.snapshot_sha256, category=DecisionCategory.EXIT,
        subject_type=DecisionSubjectType.HOLDING, subject_id=holding.holding_episode_id,
        sequence=2,
    )
    first_action = release_outcome.actions[0]
    fixed_sell = start_scale_out(
        restarted_store, decision=action_decision,
        holding_episode_id=holding.holding_episode_id,
        fraction_of_original_quantity=Decimal(str(first_action.fraction_of_original_quantity)),
        quantity_increment=Decimal("1"), rounding_rule_id="whole_share_floor_v1",
        expected_holding_version=released.state_version,
        policy_payload={"reason": first_action.reason},
        guard_payload={"source_exit_management_decision_id": next_decision.decision_id},
    )
    assert fixed_sell.exit_tier == 1
    assert fixed_sell.requested_quantity == Decimal("1")


def test_fixed_exit_management_rejects_stale_holding_facts(tmp_path):
    _, store, _, _, holding, decision, snapshot = _case(tmp_path)
    manager = OrderManager(paper=True, policy_store=store)
    stale = replace(snapshot, remaining_qty=5.0)
    with pytest.raises(ValueError, match="durable holding facts"):
        manager.record_policy_exit_management(
            decision=decision, holding_episode_id=holding.holding_episode_id,
            snapshot=stale, outcome=evaluate_exit(stale),
            expected_holding_version=holding.state_version,
        )
    assert store.load_holding_episode(holding.holding_episode_id) == holding
