from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from core.fake_policy_exit_broker import FakeProtectedExitBroker
from core.order_manager import ExitExecutionPorts, OrderManager
from core.policy_execution_state import ActionStatus
from core.policy_execution_store import PolicyExecutionStateStore
from core.policy_replacement import (
    ReplacementStepKind,
    advance_replacement,
    load_replacement_intention,
)
from tests.test_policy_replacement import _replacement_fixture


def _case(tmp_path, *, response_mode="accepted"):
    store, plan, deployment, account, _, holding = _replacement_fixture(tmp_path)
    account = replace(
        account,
        decision_slot_id=plan.decision.decision_slot_id,
        decision_id=plan.decision.decision_id,
    )
    broker = FakeProtectedExitBroker(
        account,
        receipt_store_path=Path(str(store.db_path) + ".replacement-receipts.sqlite"),
        response_mode=response_mode,
    )
    ports = ExitExecutionPorts(
        store=store,
        provider_id="offline-fake-broker",
        broker=broker,
        execution_session=lambda: plan.decision.clock.next_execution_session,
        observed_at=lambda: plan.decision.clock.account_valuation_at,
    )
    return store, plan, deployment, broker, ports, holding


def test_public_replacement_sell_is_one_use_across_restart(tmp_path):
    store, plan, deployment, broker, ports, holding = _case(tmp_path)
    first = OrderManager(paper=True, policy_store=store).submit_replacement_sell(plan, ports=ports)

    assert first.dispatch is not None and first.dispatch.disposition == "submitted"
    assert first.step.kind is ReplacementStepKind.WAITING_FOR_SELL
    assert len(broker.submissions) == 1
    assert broker.submissions[0]["quantity"] == holding.remaining_quantity
    assert store.load_holding_episode(holding.holding_episode_id).confirmed_stop_broker_order_id == (
        holding.confirmed_stop_broker_order_id
    )
    assert load_replacement_intention(store, plan.decision.decision_id).buy_action is None

    restarted = PolicyExecutionStateStore(store.db_path, store_identity=deployment.store_identity)
    replay_ports = replace(ports, store=restarted)
    replay = OrderManager(paper=True, policy_store=restarted).submit_replacement_sell(
        plan, ports=replay_ports
    )
    assert replay.step.kind is ReplacementStepKind.WAITING_FOR_SELL
    assert replay.dispatch is None
    assert len(broker.submissions) == 1


def test_partial_replacement_sale_keeps_buy_blocked_and_stop_owned(tmp_path):
    store, plan, _, broker, ports, holding = _case(tmp_path)
    manager = OrderManager(paper=True, policy_store=store)
    first = manager.submit_replacement_sell(plan, ports=ports)
    assert first.dispatch is not None and first.dispatch.action.order_attempts[0].broker_order_id
    order_id = first.dispatch.action.order_attempts[0].broker_order_id
    broker.fill_order(order_id, Decimal("2"))
    partial = manager.record_policy_cumulative_fill(
        plan.sell_action.logical_action_id,
        attempt_number=1,
        side="sell",
        broker_order_id=order_id,
        client_order_id=first.dispatch.action.order_attempts[0].client_order_id,
        cumulative_quantity=Decimal("2"),
        average_fill_price=Decimal("110"),
        observed_at=plan.decision.clock.account_valuation_at,
    )
    assert partial.status is ActionStatus.PARTIALLY_FILLED
    again = manager.submit_replacement_sell(plan, ports=ports)
    assert again.step.kind is ReplacementStepKind.WAITING_FOR_SELL
    assert again.dispatch is None
    assert len(broker.submissions) == 1
    assert load_replacement_intention(store, plan.decision.decision_id).buy_action is None
    current = store.load_holding_episode(holding.holding_episode_id)
    assert current.remaining_quantity == holding.remaining_quantity - Decimal("2")
    assert current.confirmed_stop_broker_order_id == holding.confirmed_stop_broker_order_id


def test_fixed_replacement_cannot_change_on_replay(tmp_path):
    store, plan, _, broker, ports, _ = _case(tmp_path)
    manager = OrderManager(paper=True, policy_store=store)
    manager.submit_replacement_sell(plan, ports=ports)
    with pytest.raises(ValueError, match="different facts"):
        manager.submit_replacement_sell(replace(plan, maximum_buy_price=Decimal("126")), ports=ports)
    assert len(broker.submissions) == 1


def test_full_sale_retires_old_stop_before_fresh_buy_reconciliation(tmp_path):
    store, plan, _, broker, ports, holding = _case(tmp_path)
    manager = OrderManager(paper=True, policy_store=store)
    dispatched = manager.submit_replacement_sell(plan, ports=ports)
    assert dispatched.dispatch is not None
    action = dispatched.dispatch.action
    order = action.order_attempts[0]
    broker.fill_order(order.broker_order_id, holding.remaining_quantity)
    filled = manager.record_policy_cumulative_fill(
        action.logical_action_id,
        attempt_number=1,
        side="sell",
        broker_order_id=order.broker_order_id,
        client_order_id=order.client_order_id,
        cumulative_quantity=holding.remaining_quantity,
        average_fill_price=Decimal("110"),
        observed_at=plan.decision.clock.account_valuation_at,
    )
    assert filled.status is ActionStatus.FILLED
    assert store.load_holding_episode(holding.holding_episode_id).remaining_quantity == 0

    protection = manager.confirm_policy_exit_protection(
        action.logical_action_id,
        broker=broker,
        provider_id=ports.provider_id,
        observed_at=plan.decision.clock.account_valuation_at,
    )
    assert protection.disposition == "flat"
    assert store.load_holding_episode(holding.holding_episode_id).confirmed_stop_broker_order_id is None
    wait = advance_replacement(store, plan.decision.decision_id)
    assert wait.kind is ReplacementStepKind.WAITING_FOR_RECONCILIATION
    assert load_replacement_intention(store, plan.decision.decision_id).buy_action is None


def test_rejected_replacement_sell_does_not_authorize_buy(tmp_path):
    store, plan, _, broker, ports, holding = _case(tmp_path, response_mode="reject_before_accept")
    result = OrderManager(paper=True, policy_store=store).submit_replacement_sell(plan, ports=ports)
    assert result.dispatch is not None and result.dispatch.disposition == "rejected"
    assert result.step.kind is ReplacementStepKind.BLOCKED
    assert load_replacement_intention(store, plan.decision.decision_id).buy_action is None
    current = store.load_holding_episode(holding.holding_episode_id)
    assert current.remaining_quantity == holding.remaining_quantity
    assert current.confirmed_stop_broker_order_id == holding.confirmed_stop_broker_order_id
    assert len(broker.submissions) == 0
