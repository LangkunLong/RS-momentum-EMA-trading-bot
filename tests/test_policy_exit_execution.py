from dataclasses import replace
from decimal import Decimal

import pytest

from core.order_manager import ExitExecutionPorts, OrderManager
from core.policy_execution_state import (
    ActionNotDueError,
    ActionStatus,
    DecisionCategory,
    DecisionIdentity,
    DecisionSubjectType,
)
from core.policy_execution_store import PolicyExecutionStateStore
from core.policy_exit_execution import PolicyExitSubmissionResult
from core.policy_exits import start_full_exit, start_scale_out
from tests.test_paper_policy_chain import read_chain, seed_pending_chain
from tests.test_policy_exits import _exit_decision


def _case(tmp_path, *, full_exit=False):
    features, path, deployment, account, portfolio, _, _ = seed_pending_chain(tmp_path)
    store = PolicyExecutionStateStore(path, store_identity=deployment.store_identity)
    chain = read_chain(store, deployment, portfolio)
    holding = next(item for item in chain.holding_episodes if item.broker_symbol == "CCC")
    decision = _exit_decision(features, deployment, portfolio, holding)
    if full_exit:
        action = start_full_exit(
            store,
            decision=decision,
            holding_episode_id=holding.holding_episode_id,
            expected_holding_version=holding.state_version,
            policy_payload={"reason": "fixed_full_exit"},
            guard_payload={"outcome": "allow_offline_fixture"},
        )
    else:
        action = start_scale_out(
            store,
            decision=decision,
            holding_episode_id=holding.holding_episode_id,
            fraction_of_original_quantity=Decimal("0.5"),
            quantity_increment=Decimal("1"),
            rounding_rule_id="whole_share_floor_v1",
            expected_holding_version=holding.state_version,
            policy_payload={"tier": 1},
            guard_payload={"outcome": "allow_offline_fixture"},
        )
    account = replace(
        account,
        decision_slot_id=decision.decision_slot_id,
        decision_id=decision.decision_id,
    )
    return store, account, decision, holding, action


def _ports(store, account, decision, submit):
    return ExitExecutionPorts(
        store=store,
        provider_id="offline-fake-broker",
        get_account_snapshot=lambda: account,
        execution_session=lambda: decision.clock.next_execution_session,
        observed_at=lambda: decision.clock.account_valuation_at,
        submit_order=submit,
    )


def _linked_success(broker_order_id, **kwargs):
    return PolicyExitSubmissionResult(
        success=True,
        broker_order_id=broker_order_id,
        linked_stop_broker_order_id=kwargs["confirmed_stop_broker_order_id"],
        shared_sell_quantity_limit=kwargs["protected_position_quantity"],
        shared_sell_group_id="fake-paper-shared-position-cap",
    )


@pytest.mark.parametrize("full_exit, expected_quantity", [(False, Decimal("3")), (True, Decimal("6"))])
def test_public_policy_exit_submits_fixed_quantity_once(tmp_path, full_exit, expected_quantity):
    store, account, decision, holding, action = _case(tmp_path, full_exit=full_exit)
    calls = []

    def submit(**kwargs):
        calls.append(kwargs)
        return _linked_success("broker:policy-exit", **kwargs)

    manager = OrderManager(paper=True, policy_store=store)
    ports = _ports(store, account, decision, submit)
    submitted = manager.submit_policy_exit(action.logical_action_id, ports=ports)

    assert submitted.disposition == "submitted", submitted.reason
    assert submitted.action.status is ActionStatus.SUBMITTED
    assert len(calls) == 1
    assert calls[0]["quantity"] == expected_quantity
    assert calls[0]["confirmed_stop_client_order_id"] == holding.confirmed_stop_client_order_id
    assert calls[0]["confirmed_stop_broker_order_id"] == holding.confirmed_stop_broker_order_id
    assert submitted.action.order_attempts[0].client_order_id == calls[0]["client_order_id"]
    assert submitted.action.order_attempts[0].broker_order_id == "broker:policy-exit"

    restarted = PolicyExecutionStateStore(store.db_path, store_identity=store.store_identity)
    replay = OrderManager(paper=True, policy_store=restarted).submit_policy_exit(
        action.logical_action_id,
        ports=_ports(restarted, account, decision, submit),
    )
    assert replay.disposition == "reconcile"
    assert len(calls) == 1


def test_uncertain_sell_result_never_dispatches_again_after_restart(tmp_path):
    store, account, decision, _, action = _case(tmp_path)
    calls = []

    def submit(**kwargs):
        calls.append(kwargs)
        raise TimeoutError("response lost after submit")

    manager = OrderManager(paper=True, policy_store=store)
    first = manager.submit_policy_exit(action.logical_action_id, ports=_ports(store, account, decision, submit))
    assert first.disposition == "reconcile"
    assert first.action.status is ActionStatus.SUBMITTED
    assert first.action.order_attempts[0].client_order_id == calls[0]["client_order_id"]

    restarted = PolicyExecutionStateStore(store.db_path, store_identity=store.store_identity)
    again = OrderManager(paper=True, policy_store=restarted).submit_policy_exit(
        action.logical_action_id,
        ports=_ports(restarted, account, decision, submit),
    )
    assert again.disposition == "reconcile"
    assert len(calls) == 1


def test_sell_without_confirmed_shared_stop_cap_requires_reconciliation(tmp_path):
    store, account, decision, _, action = _case(tmp_path)
    calls = []

    def submit(**kwargs):
        calls.append(kwargs)
        return PolicyExitSubmissionResult(success=True, broker_order_id="broker:unlinked-sell")

    first = OrderManager(paper=True, policy_store=store).submit_policy_exit(
        action.logical_action_id,
        ports=_ports(store, account, decision, submit),
    )
    assert first.disposition == "reconcile"
    assert "shared position cap" in first.reason
    restarted = PolicyExecutionStateStore(store.db_path, store_identity=store.store_identity)
    replay = OrderManager(paper=True, policy_store=restarted).submit_policy_exit(
        action.logical_action_id,
        ports=_ports(restarted, account, decision, submit),
    )
    assert replay.disposition == "reconcile"
    assert len(calls) == 1


def test_confirmed_partial_sell_reduces_only_remaining_holding_and_never_resubmits(tmp_path):
    store, account, decision, holding, action = _case(tmp_path)
    calls = []

    def submit(**kwargs):
        calls.append(kwargs)
        return _linked_success("broker:partial-sell", **kwargs)

    manager = OrderManager(paper=True, policy_store=store)
    dispatched = manager.submit_policy_exit(action.logical_action_id, ports=_ports(store, account, decision, submit))
    client_id = dispatched.action.order_attempts[0].client_order_id
    fill = manager.record_policy_cumulative_fill(
        action.logical_action_id,
        attempt_number=1,
        side="sell",
        broker_order_id="broker:partial-sell",
        client_order_id=client_id,
        cumulative_quantity=Decimal("2"),
        average_fill_price=Decimal("110"),
        observed_at=decision.clock.account_valuation_at,
    )
    assert fill.status is ActionStatus.PARTIALLY_FILLED
    current = store.load_holding_episode(holding.holding_episode_id)
    assert current.remaining_quantity == Decimal("4")
    assert current.confirmed_stop_broker_order_id == holding.confirmed_stop_broker_order_id
    assert current.confirmed_protective_stop_price == holding.confirmed_protective_stop_price

    restarted = PolicyExecutionStateStore(store.db_path, store_identity=store.store_identity)
    replay = OrderManager(paper=True, policy_store=restarted).submit_policy_exit(
        action.logical_action_id,
        ports=_ports(restarted, account, decision, submit),
    )
    assert replay.disposition == "reconcile"
    assert len(calls) == 1


def test_definite_rejection_preserves_holding_and_confirmed_stop(tmp_path):
    store, account, decision, holding, action = _case(tmp_path)
    manager = OrderManager(paper=True, policy_store=store)
    result = manager.submit_policy_exit(
        action.logical_action_id,
        ports=_ports(
            store,
            account,
            decision,
            lambda **_: PolicyExitSubmissionResult(success=False, error="definitively rejected"),
        ),
    )
    assert result.disposition == "rejected"
    assert result.action.status is ActionStatus.RESOLVED
    current = store.load_holding_episode(holding.holding_episode_id)
    assert current.remaining_quantity == holding.remaining_quantity
    assert current.confirmed_stop_broker_order_id == holding.confirmed_stop_broker_order_id


def test_missing_position_or_stop_blocks_before_any_broker_call(tmp_path):
    store, account, decision, _, action = _case(tmp_path)
    calls = []
    account = replace(account, positions=None)
    step = OrderManager(paper=True, policy_store=store).submit_policy_exit(
        action.logical_action_id,
        ports=_ports(store, account, decision, lambda **kwargs: calls.append(kwargs)),
    )
    assert step.disposition == "blocked"
    assert store.load_action_intent(action.logical_action_id).status is ActionStatus.INTENDED
    assert calls == []


def test_wrong_stop_quantity_blocks_before_any_broker_call(tmp_path):
    store, account, decision, _, action = _case(tmp_path)
    calls = []
    wrong_stop = replace(account.open_orders[-1], requested_quantity=5)
    account = replace(account, open_orders=account.open_orders[:-1] + (wrong_stop,))
    step = OrderManager(paper=True, policy_store=store).submit_policy_exit(
        action.logical_action_id,
        ports=_ports(store, account, decision, lambda **kwargs: calls.append(kwargs)),
    )
    assert step.disposition == "blocked"
    assert "protective-stop facts" in step.reason
    assert calls == []
    assert store.load_action_intent(action.logical_action_id).status is ActionStatus.INTENDED


def test_exit_cannot_move_to_an_unqualified_hourly_or_later_session(tmp_path):
    store, account, decision, _, action = _case(tmp_path)
    calls = []
    ports = ExitExecutionPorts(
        store=store,
        provider_id="offline-fake-broker",
        get_account_snapshot=lambda: account,
        execution_session=lambda: decision.clock.decision_session,
        observed_at=lambda: decision.clock.account_valuation_at,
        submit_order=lambda **kwargs: calls.append(kwargs),
    )
    with pytest.raises(ActionNotDueError):
        OrderManager(paper=True, policy_store=store).submit_policy_exit(action.logical_action_id, ports=ports)
    assert calls == []
    assert store.load_action_intent(action.logical_action_id).status is ActionStatus.INTENDED


@pytest.mark.parametrize("uncertain", [False, True])
def test_completed_scale_out_replaces_exact_remaining_stop_once(tmp_path, uncertain):
    store, account, decision, holding, action = _case(tmp_path)
    manager = OrderManager(paper=True, policy_store=store)
    submitted = manager.submit_policy_exit(
        action.logical_action_id,
        ports=_ports(store, account, decision, lambda **kwargs: _linked_success("broker:scale-out", **kwargs)),
    )
    manager.record_policy_cumulative_fill(
        action.logical_action_id,
        attempt_number=1,
        side="sell",
        broker_order_id="broker:scale-out",
        client_order_id=submitted.action.order_attempts[0].client_order_id,
        cumulative_quantity=Decimal("3"),
        average_fill_price=Decimal("110"),
        observed_at=decision.clock.account_valuation_at,
    )
    reduced = store.load_holding_episode(holding.holding_episode_id)
    assert reduced.remaining_quantity == Decimal("3")
    resize_decision = DecisionIdentity.build(
        deployment=decision.deployment_identity,
        clock=decision.clock,
        snapshot_sha256=decision.snapshot_sha256,
        category=DecisionCategory.EXIT,
        subject_type=DecisionSubjectType.HOLDING,
        subject_id=holding.holding_episode_id,
        sequence=1,
    )
    position = next(row for row in account.positions if row.symbol == "CCC")
    post_fill = replace(
        account,
        decision_slot_id=resize_decision.decision_slot_id,
        decision_id=resize_decision.decision_id,
        account_snapshot_id="snapshot:confirmed-scale-out-fill",
        positions=tuple(replace(row, quantity=3) if row is position else row for row in account.positions),
    )
    calls = []
    refreshed_accounts = []

    def replace_stop(**kwargs):
        calls.append(kwargs)
        old = kwargs["old_order"]
        replacement = replace(
            old,
            requested_quantity=3,
            client_order_id=kwargs["new_client_order_id"],
            broker_order_id="broker:resized-stop",
        )
        refreshed = replace(
            post_fill,
            open_orders=tuple(row for row in post_fill.open_orders if row is not old) + (replacement,),
            account_snapshot_id="snapshot:after-atomic-stop-replacement",
        )
        refreshed_accounts.append(refreshed)
        return None if uncertain else refreshed

    first = manager.replace_policy_exit_protection(
        action.logical_action_id,
        decision=resize_decision,
        account=post_fill,
        provider_id="offline-fake-broker",
        observed_at=decision.clock.account_valuation_at,
        replace_stop=replace_stop,
    )
    assert first.disposition == ("reconcile" if uncertain else "protected"), first.reason
    assert len(calls) == 1
    assert calls[0]["quantity"] == Decimal("3")
    assert calls[0]["old_order"].broker_order_id == holding.confirmed_stop_broker_order_id
    if uncertain:
        pending = store.load_holding_episode(holding.holding_episode_id)
        assert pending.confirmed_stop_broker_order_id == holding.confirmed_stop_broker_order_id
        assert pending.proposed_stop_action_id != pending.confirmed_stop_action_id
        restarted = PolicyExecutionStateStore(store.db_path, store_identity=store.store_identity)
        confirmed_later = OrderManager(paper=True, policy_store=restarted).confirm_policy_exit_protection(
            action.logical_action_id,
            account=refreshed_accounts[0],
            provider_id="offline-fake-broker",
            observed_at=decision.clock.account_valuation_at,
        )
        assert confirmed_later.disposition == "protected", confirmed_later.reason

    confirmed = store.load_holding_episode(holding.holding_episode_id)
    assert confirmed.remaining_quantity == Decimal("3")
    assert confirmed.confirmed_stop_broker_order_id == "broker:resized-stop"
    assert confirmed.confirmed_stop_client_order_id == calls[0]["new_client_order_id"]

    restarted = PolicyExecutionStateStore(store.db_path, store_identity=store.store_identity)
    replay = OrderManager(paper=True, policy_store=restarted).replace_policy_exit_protection(
        action.logical_action_id,
        decision=resize_decision,
        account=refreshed_accounts[0],
        provider_id="offline-fake-broker",
        observed_at=decision.clock.account_valuation_at,
        replace_stop=replace_stop,
    )
    assert replay.disposition == "protected"
    assert len(calls) == 1


def test_full_exit_remains_unresolved_until_broker_confirms_stop_absent(tmp_path):
    store, account, decision, holding, action = _case(tmp_path, full_exit=True)
    manager = OrderManager(paper=True, policy_store=store)
    submitted = manager.submit_policy_exit(
        action.logical_action_id,
        ports=_ports(store, account, decision, lambda **kwargs: _linked_success("broker:full-exit", **kwargs)),
    )
    manager.record_policy_cumulative_fill(
        action.logical_action_id,
        attempt_number=1,
        side="sell",
        broker_order_id="broker:full-exit",
        client_order_id=submitted.action.order_attempts[0].client_order_id,
        cumulative_quantity=Decimal("6"),
        average_fill_price=Decimal("110"),
        observed_at=decision.clock.account_valuation_at,
    )
    assert store.load_holding_episode(holding.holding_episode_id).remaining_quantity == 0
    resize_decision = DecisionIdentity.build(
        deployment=decision.deployment_identity,
        clock=decision.clock,
        snapshot_sha256=decision.snapshot_sha256,
        category=DecisionCategory.EXIT,
        subject_type=DecisionSubjectType.HOLDING,
        subject_id=holding.holding_episode_id,
        sequence=1,
    )
    observation = replace(
        account,
        decision_slot_id=resize_decision.decision_slot_id,
        decision_id=resize_decision.decision_id,
        account_snapshot_id="snapshot:after-full-exit",
        positions=tuple(row for row in account.positions if row.symbol != "CCC"),
    )
    calls = []
    pending = manager.replace_policy_exit_protection(
        action.logical_action_id,
        decision=resize_decision,
        account=observation,
        provider_id="offline-fake-broker",
        observed_at=decision.clock.account_valuation_at,
        replace_stop=lambda **kwargs: calls.append(kwargs),
    )
    assert pending.disposition == "reconcile"
    assert calls == []
    cancelled = replace(
        observation,
        open_orders=tuple(row for row in observation.open_orders if row.symbol != "CCC"),
        account_snapshot_id="snapshot:after-stop-cancel",
    )
    resolved = manager.confirm_policy_exit_protection(
        action.logical_action_id,
        account=cancelled,
        provider_id="offline-fake-broker",
        observed_at=decision.clock.account_valuation_at,
    )
    assert resolved.disposition == "flat"
    assert calls == []
