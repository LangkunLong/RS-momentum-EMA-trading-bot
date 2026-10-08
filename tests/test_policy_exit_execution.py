from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal

import pytest

from core.fake_policy_exit_broker import FakeProtectedExitBroker
from core.order_manager import ExitExecutionPorts, OrderManager
from core.policy_execution_state import (
    ActionNotDueError,
    ActionStatus,
    DecisionCategory,
    DecisionIdentity,
    DecisionSubjectType,
)
from core.policy_execution_store import PolicyExecutionStateStore
from core.policy_exit_execution import dispatch_policy_exit
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


def _ports(store, broker, decision):
    return ExitExecutionPorts(
        store=store,
        provider_id="offline-fake-broker",
        broker=broker,
        execution_session=lambda: decision.clock.next_execution_session,
        observed_at=lambda: decision.clock.account_valuation_at,
    )


@pytest.mark.parametrize("full_exit, expected_quantity", [(False, Decimal("3")), (True, Decimal("6"))])
def test_public_policy_exit_submits_fixed_quantity_once(tmp_path, full_exit, expected_quantity):
    store, account, decision, holding, action = _case(tmp_path, full_exit=full_exit)
    broker = FakeProtectedExitBroker(account)
    manager = OrderManager(paper=True, policy_store=store)
    ports = _ports(store, broker, decision)
    submitted = manager.submit_policy_exit(action.logical_action_id, ports=ports)

    assert submitted.disposition == "submitted", submitted.reason
    assert submitted.action.status is ActionStatus.SUBMITTED
    assert len(broker.submissions) == 1
    assert broker.submissions[0]["quantity"] == expected_quantity
    assert broker.submissions[0]["confirmed_stop_client_order_id"] == holding.confirmed_stop_client_order_id
    assert broker.submissions[0]["confirmed_stop_broker_order_id"] == holding.confirmed_stop_broker_order_id
    assert submitted.action.order_attempts[0].client_order_id == broker.submissions[0]["client_order_id"]
    assert submitted.action.order_attempts[0].broker_order_id in {
        row.broker_order_id for row in broker.snapshot().open_orders if row.purpose == "strategy" and row.side == "sell"
    }

    restarted = PolicyExecutionStateStore(store.db_path, store_identity=store.store_identity)
    replay = OrderManager(paper=True, policy_store=restarted).submit_policy_exit(
        action.logical_action_id,
        ports=_ports(restarted, broker, decision),
    )
    assert replay.disposition == "reconcile"
    assert len(broker.submissions) == 1


def test_uncertain_sell_result_never_dispatches_again_after_restart(tmp_path):
    store, account, decision, _, action = _case(tmp_path)
    broker = FakeProtectedExitBroker(account, response_mode="timeout_after_accept")
    manager = OrderManager(paper=True, policy_store=store)
    first = manager.submit_policy_exit(action.logical_action_id, ports=_ports(store, broker, decision))
    assert first.disposition == "reconcile"
    assert first.action.status is ActionStatus.SUBMITTED
    assert first.action.order_attempts[0].client_order_id == broker.submissions[0]["client_order_id"]

    restarted = PolicyExecutionStateStore(store.db_path, store_identity=store.store_identity)
    again = OrderManager(paper=True, policy_store=restarted).submit_policy_exit(
        action.logical_action_id,
        ports=_ports(restarted, broker, decision),
    )
    assert again.disposition == "reconcile"
    assert len(broker.submissions) == 1


def test_public_ports_reject_callback_only_broker(tmp_path):
    store, account, decision, _, action = _case(tmp_path)
    with pytest.raises(TypeError, match="atomic fake-paper broker"):
        ExitExecutionPorts(
            store=store,
            provider_id="offline-fake-broker",
            broker=lambda **_: None,
            execution_session=lambda: decision.clock.next_execution_session,
            observed_at=lambda: decision.clock.account_valuation_at,
        )
    assert store.load_action_intent(action.logical_action_id).status is ActionStatus.INTENDED
    with pytest.raises(TypeError, match="atomic fake-paper broker"):
        dispatch_policy_exit(
            store,
            action.logical_action_id,
            account=account,
            execution_session=decision.clock.next_execution_session,
            provider_id="offline-fake-broker",
            observed_at=decision.clock.account_valuation_at,
            broker=lambda **_: None,
        )


def test_confirmed_partial_sell_reduces_only_remaining_holding_and_never_resubmits(tmp_path):
    store, account, decision, holding, action = _case(tmp_path)
    broker = FakeProtectedExitBroker(account)
    manager = OrderManager(paper=True, policy_store=store)
    dispatched = manager.submit_policy_exit(action.logical_action_id, ports=_ports(store, broker, decision))
    client_id = dispatched.action.order_attempts[0].client_order_id
    broker_order_id = dispatched.action.order_attempts[0].broker_order_id
    observed = broker.fill_order(broker_order_id, Decimal("2"))
    assert next(row for row in observed.positions if row.symbol == "CCC").quantity == 4
    assert any(row.broker_order_id == holding.confirmed_stop_broker_order_id for row in observed.open_orders)
    fill = manager.record_policy_cumulative_fill(
        action.logical_action_id,
        attempt_number=1,
        side="sell",
        broker_order_id=broker_order_id,
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
        ports=_ports(restarted, broker, decision),
    )
    assert replay.disposition == "reconcile"
    assert len(broker.submissions) == 1


def test_fake_broker_atomically_caps_simultaneous_stop_and_full_exit_fills(tmp_path):
    store, account, decision, holding, action = _case(tmp_path, full_exit=True)
    broker = FakeProtectedExitBroker(account)
    result = OrderManager(paper=True, policy_store=store).submit_policy_exit(
        action.logical_action_id,
        ports=_ports(store, broker, decision),
    )
    assert result.disposition == "submitted"
    sell_id = result.action.order_attempts[0].broker_order_id
    stop_id = holding.confirmed_stop_broker_order_id
    observed = broker.snapshot()
    assert {row.broker_order_id for row in observed.open_orders if row.symbol == "CCC" and row.side == "sell"} == {
        sell_id, stop_id
    }

    def fill(order_id):
        try:
            return ("filled", broker.fill_order(order_id, Decimal("4")))
        except ValueError as exc:
            return ("blocked", str(exc))

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(fill, (sell_id, stop_id)))
    assert sorted(kind for kind, _ in outcomes) == ["blocked", "filled"]
    remaining = broker.snapshot()
    assert next(row for row in remaining.positions if row.symbol == "CCC").quantity == 2
    stop = next(row for row in remaining.open_orders if row.broker_order_id == stop_id)
    assert stop.status in {"submitted", "partially_filled"}
    with pytest.raises(ValueError):
        broker.fill_order(stop_id, Decimal("3"))
    flat = broker.fill_order(stop_id, Decimal("2"))
    assert all(row.symbol != "CCC" for row in flat.positions)
    assert all(row.symbol != "CCC" or row.side != "sell" for row in flat.open_orders)


def test_fake_broker_recovers_shared_cap_after_partial_sell_restart(tmp_path):
    store, account, decision, holding, action = _case(tmp_path, full_exit=True)
    broker = FakeProtectedExitBroker(account)
    submitted = OrderManager(paper=True, policy_store=store).submit_policy_exit(
        action.logical_action_id,
        ports=_ports(store, broker, decision),
    )
    sell_id = submitted.action.order_attempts[0].broker_order_id
    observed = broker.fill_order(sell_id, Decimal("2"))
    restarted_broker = FakeProtectedExitBroker(observed)
    with pytest.raises(ValueError, match="shared sell cap"):
        restarted_broker.fill_order(holding.confirmed_stop_broker_order_id, Decimal("5"))
    flat = restarted_broker.fill_order(holding.confirmed_stop_broker_order_id, Decimal("4"))
    assert all(row.symbol != "CCC" for row in flat.positions)
    assert all(row.symbol != "CCC" or row.side != "sell" for row in flat.open_orders)


def test_definite_rejection_preserves_holding_and_confirmed_stop(tmp_path):
    store, account, decision, holding, action = _case(tmp_path)
    broker = FakeProtectedExitBroker(account, response_mode="reject_before_accept")
    manager = OrderManager(paper=True, policy_store=store)
    result = manager.submit_policy_exit(
        action.logical_action_id,
        ports=_ports(store, broker, decision),
    )
    assert result.disposition == "rejected"
    assert result.action.status is ActionStatus.RESOLVED
    current = store.load_holding_episode(holding.holding_episode_id)
    assert current.remaining_quantity == holding.remaining_quantity
    assert current.confirmed_stop_broker_order_id == holding.confirmed_stop_broker_order_id
    assert broker.submissions == []


def test_incomplete_broker_snapshot_blocks_before_any_broker_call(tmp_path):
    store, account, decision, _, action = _case(tmp_path)
    account = replace(account, positions=None)
    with pytest.raises(ValueError, match="complete paper positions"):
        FakeProtectedExitBroker(account)
    assert store.load_action_intent(action.logical_action_id).status is ActionStatus.INTENDED


def test_wrong_stop_quantity_blocks_before_any_broker_call(tmp_path):
    store, account, decision, _, action = _case(tmp_path)
    wrong_stop = replace(account.open_orders[-1], requested_quantity=5)
    account = replace(account, open_orders=account.open_orders[:-1] + (wrong_stop,))
    broker = FakeProtectedExitBroker(account)
    step = OrderManager(paper=True, policy_store=store).submit_policy_exit(
        action.logical_action_id,
        ports=_ports(store, broker, decision),
    )
    assert step.disposition == "blocked"
    assert "protective-stop facts" in step.reason
    assert broker.submissions == []
    assert store.load_action_intent(action.logical_action_id).status is ActionStatus.INTENDED


def test_exit_cannot_move_to_an_unqualified_hourly_or_later_session(tmp_path):
    store, account, decision, _, action = _case(tmp_path)
    broker = FakeProtectedExitBroker(account)
    ports = ExitExecutionPorts(
        store=store,
        provider_id="offline-fake-broker",
        broker=broker,
        execution_session=lambda: decision.clock.decision_session,
        observed_at=lambda: decision.clock.account_valuation_at,
    )
    with pytest.raises(ActionNotDueError):
        OrderManager(paper=True, policy_store=store).submit_policy_exit(action.logical_action_id, ports=ports)
    assert broker.submissions == []
    assert store.load_action_intent(action.logical_action_id).status is ActionStatus.INTENDED


@pytest.mark.parametrize("uncertain", [False, True])
def test_completed_scale_out_replaces_exact_remaining_stop_once(tmp_path, uncertain):
    store, account, decision, holding, action = _case(tmp_path)
    broker = FakeProtectedExitBroker(account)
    manager = OrderManager(paper=True, policy_store=store)
    submitted = manager.submit_policy_exit(
        action.logical_action_id,
        ports=_ports(store, broker, decision),
    )
    broker_order_id = submitted.action.order_attempts[0].broker_order_id
    broker.fill_order(broker_order_id, Decimal("3"))
    manager.record_policy_cumulative_fill(
        action.logical_action_id,
        attempt_number=1,
        side="sell",
        broker_order_id=broker_order_id,
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
    post_fill = broker.observe_for(resize_decision)
    calls = []
    refreshed_accounts = []

    def replace_stop(**kwargs):
        calls.append(kwargs)
        refreshed = broker.replace_stop(**kwargs)
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
    assert confirmed.confirmed_stop_broker_order_id.startswith("fake-stop-order:")
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
    broker = FakeProtectedExitBroker(account)
    manager = OrderManager(paper=True, policy_store=store)
    submitted = manager.submit_policy_exit(
        action.logical_action_id,
        ports=_ports(store, broker, decision),
    )
    broker_order_id = submitted.action.order_attempts[0].broker_order_id
    manager.record_policy_cumulative_fill(
        action.logical_action_id,
        attempt_number=1,
        side="sell",
        broker_order_id=broker_order_id,
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
    observation = broker.observe_for(resize_decision)
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
    cancelled = broker.fill_order(broker_order_id, Decimal("6"))
    resolved = manager.confirm_policy_exit_protection(
        action.logical_action_id,
        account=cancelled,
        provider_id="offline-fake-broker",
        observed_at=decision.clock.account_valuation_at,
    )
    assert resolved.disposition == "flat"
    assert calls == []
    retired = store.load_holding_episode(holding.holding_episode_id)
    assert retired.confirmed_stop_broker_order_id is None
    assert dict(retired.policy_flags)["flat_stop_retired_broker_order_id"] == holding.confirmed_stop_broker_order_id
    version = retired.state_version
    restarted = PolicyExecutionStateStore(store.db_path, store_identity=store.store_identity)
    replay = OrderManager(paper=True, policy_store=restarted).confirm_policy_exit_protection(
        action.logical_action_id,
        account=broker.snapshot(),
        provider_id="offline-fake-broker",
        observed_at=decision.clock.account_valuation_at,
    )
    assert replay.disposition == "flat"
    assert restarted.load_holding_episode(holding.holding_episode_id).state_version == version
