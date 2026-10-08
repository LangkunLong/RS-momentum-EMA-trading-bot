from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import sqlite3
from datetime import timedelta

import pytest

from core.fake_policy_replacement_buy_broker import FakeProtectedReplacementBuyBroker
from core.fake_policy_exit_broker import FakeProtectedExitBroker
from core.order_manager import ExitExecutionPorts, OrderManager
from core.policy_execution_state import ActionStatus, DecisionClock
from core.policy_execution_store import PolicyExecutionStateStore
from core.policy_replacement import (
    ReplacementStepKind,
    advance_replacement,
    load_replacement_intention,
    start_replacement,
)
from core.policy_replacement_execution import (
    ReplacementBuyPorts,
    _client_order_id,
    _holding_id,
)
from core.policy_protection_bridge import PolicyProtectionBridge
from core.strategy_policy.account_reconciliation import AccountValuationClock, BrokerOrderFact
from tests.test_policy_replacement import (
    _record_fake_sell_fill,
    _refreshed_after_full_sale,
    _replacement_fixture,
    _retire_fake_flat_stop,
    _submit_fake_sell,
)


def _ready_buy(tmp_path, *, response_mode="accepted"):
    store, plan, deployment, account, portfolio, original_holding = _replacement_fixture(tmp_path)
    start_replacement(store, plan)
    _submit_fake_sell(store, plan, plan.decision.clock.account_valuation_at)
    _record_fake_sell_fill(
        store,
        plan,
        str(plan.sell_action.requested_quantity),
        observed_at=plan.decision.clock.account_valuation_at,
    )
    fresh_account, fresh_portfolio = _refreshed_after_full_sale(store, account, portfolio, plan)
    _retire_fake_flat_stop(store, plan, original_holding, fresh_account.account_snapshot_id)
    receipt_path = Path(str(store.db_path) + ".fake-replacement-buy.sqlite")
    broker = FakeProtectedReplacementBuyBroker(
        fresh_account,
        quote_symbol=plan.candidate_symbol,
        quote_price=Decimal("110"),
        receipt_store_path=receipt_path,
        response_mode=response_mode,
    )
    ports = ReplacementBuyPorts(
        store=store,
        provider_id="offline-fake-replacement-buy",
        broker=broker,
        observed_at=lambda: fresh_account.clock.valuation_time,
    )
    return store, plan, deployment, fresh_account, fresh_portfolio, broker, ports, receipt_path


def test_confirmed_fake_sale_proceeds_fund_only_the_later_protected_buy(tmp_path):
    store, plan, _, account, portfolio, original_holding = _replacement_fixture(tmp_path)
    sell_account = replace(
        account,
        decision_slot_id=plan.decision.decision_slot_id,
        decision_id=plan.decision.decision_id,
    )
    exit_broker = FakeProtectedExitBroker(
        sell_account,
        receipt_store_path=Path(str(store.db_path) + ".replacement-exit-receipts.sqlite"),
    )
    exit_ports = ExitExecutionPorts(
        store=store,
        provider_id="offline-fake-broker",
        broker=exit_broker,
        execution_session=lambda: plan.decision.clock.next_execution_session,
        observed_at=lambda: plan.decision.clock.account_valuation_at,
    )
    manager = OrderManager(paper=True, policy_store=store)
    sell = manager.submit_replacement_sell(plan, ports=exit_ports)
    assert sell.dispatch is not None and sell.dispatch.disposition == "submitted"
    sell_order = sell.dispatch.action.order_attempts[0]

    partial = exit_broker.fill_order(
        sell_order.broker_order_id, Decimal("3"), fill_price=Decimal("100")
    )
    manager.record_policy_cumulative_fill(
        plan.sell_action.logical_action_id,
        attempt_number=1,
        side="sell",
        broker_order_id=sell_order.broker_order_id,
        client_order_id=sell_order.client_order_id,
        cumulative_quantity=Decimal("3"),
        average_fill_price=Decimal("100"),
        observed_at=plan.decision.clock.account_valuation_at,
    )
    assert Decimal(str(partial.cash)) == Decimal(str(account.cash)) + Decimal("300")
    assert advance_replacement(store, plan.decision.decision_id).kind is ReplacementStepKind.WAITING_FOR_SELL
    assert store.load_holding_episode(original_holding.holding_episode_id).remaining_quantity == 3

    flat = exit_broker.fill_order(
        sell_order.broker_order_id, Decimal("3"), fill_price=Decimal("100")
    )
    manager.record_policy_cumulative_fill(
        plan.sell_action.logical_action_id,
        attempt_number=1,
        side="sell",
        broker_order_id=sell_order.broker_order_id,
        client_order_id=sell_order.client_order_id,
        cumulative_quantity=Decimal("6"),
        average_fill_price=Decimal("100"),
        observed_at=plan.decision.clock.account_valuation_at,
    )
    assert Decimal(str(flat.cash)) == Decimal(str(account.cash)) + Decimal("600")
    protection = manager.confirm_policy_exit_protection(
        plan.sell_action.logical_action_id,
        broker=exit_broker,
        provider_id=exit_ports.provider_id,
        observed_at=plan.decision.clock.account_valuation_at,
    )
    assert protection.disposition == "flat"

    valuation_time = account.clock.valuation_time + timedelta(minutes=2)
    next_clock = AccountValuationClock(
        completed_session=account.clock.completed_session,
        as_of_cutoff=account.clock.as_of_cutoff,
        next_execution_session=account.clock.next_execution_session,
        valuation_time=valuation_time,
    )
    terminal_sale = BrokerOrderFact(
        sell_order.broker_order_id,
        sell_order.client_order_id,
        original_holding.broker_symbol,
        "sell",
        "filled",
        float(original_holding.remaining_quantity),
        float(original_holding.remaining_quantity),
    )
    fresh_account = replace(
        flat,
        clock=next_clock,
        balance_observed_at=valuation_time,
        peak_observed_at=valuation_time,
        open_orders=flat.open_orders + (terminal_sale,),
        account_snapshot_id="fake-broker-confirmed-sale-fresh-account",
    )
    fresh_decision_clock = DecisionClock(
        exchange_id=portfolio.clock.exchange_id,
        decision_session=next_clock.completed_session,
        as_of_cutoff_at=next_clock.as_of_cutoff,
        next_execution_session=next_clock.next_execution_session,
        account_valuation_session=next_clock.next_execution_session,
        account_valuation_at=next_clock.valuation_time,
    )
    fresh_portfolio = replace(
        portfolio,
        clock=fresh_decision_clock,
        account_snapshot_id=fresh_account.account_snapshot_id,
        cash=Decimal(str(fresh_account.cash)),
        gross_exposure=Decimal("400"),
        open_risk=Decimal("40"),
    )
    buy_broker = FakeProtectedReplacementBuyBroker(
        fresh_account,
        quote_symbol=plan.candidate_symbol,
        quote_price=Decimal("110"),
        receipt_store_path=Path(str(store.db_path) + ".joined-buy-receipts.sqlite"),
    )
    buy_ports = ReplacementBuyPorts(
        store=store,
        provider_id="offline-fake-replacement-buy",
        broker=buy_broker,
        observed_at=lambda: valuation_time,
    )
    buy = manager.submit_replacement_buy(
        plan.decision.decision_id, portfolio_snapshot=fresh_portfolio, ports=buy_ports
    )
    assert buy.submission is not None and buy.submission.success
    assert buy_broker.submissions[0]["source_account_snapshot_id"] == fresh_account.account_snapshot_id
    buy_broker.fill_buy(
        buy.submission.broker_order_id,
        Decimal("64"),
        fill_price=Decimal("110"),
        observed_at=valuation_time,
    )
    complete = manager.record_replacement_buy_fill(plan.decision.decision_id, ports=buy_ports)
    assert complete.kind is ReplacementStepKind.COMPLETE
    assert complete.action is not None
    assert store.load_holding_episode_for_action(
        complete.action.logical_action_id
    ).confirmed_stop_broker_order_id is not None


def test_public_fake_replacement_buy_fills_and_confirms_new_stop(tmp_path):
    store, plan, _, account, portfolio, broker, ports, _ = _ready_buy(tmp_path)
    manager = OrderManager(paper=True, policy_store=store)
    dispatched = manager.submit_replacement_buy(
        plan.decision.decision_id, portfolio_snapshot=portfolio, ports=ports
    )
    assert dispatched.submission is not None and dispatched.submission.success
    assert dispatched.step.kind is ReplacementStepKind.WAITING_FOR_BUY
    assert len(broker.submissions) == 1
    assert broker.submissions[0]["source_account_snapshot_id"] == account.account_snapshot_id
    assert broker.submissions[0]["quantity"] == Decimal("64")
    assert broker.submissions[0]["limit_price"] == Decimal("125")
    assert broker.snapshot().cash == account.cash

    broker.fill_buy(
        dispatched.submission.broker_order_id,
        Decimal("64"),
        fill_price=Decimal("110"),
        observed_at=account.clock.valuation_time,
    )
    complete = manager.record_replacement_buy_fill(plan.decision.decision_id, ports=ports)
    assert complete.kind is ReplacementStepKind.COMPLETE
    assert complete.action is not None and complete.action.status is ActionStatus.FILLED
    holding = store.load_holding_episode_for_action(complete.action.logical_action_id)
    assert holding.remaining_quantity == Decimal("64")
    assert holding.confirmed_protective_stop_price == Decimal("100")
    assert holding.confirmed_stop_client_order_id is not None
    assert holding.confirmed_stop_broker_order_id is not None
    assert broker.snapshot().cash == 2560


def test_uncertain_buy_acceptance_recovers_once_after_restart(tmp_path):
    store, plan, deployment, account, portfolio, broker, ports, receipt_path = _ready_buy(
        tmp_path, response_mode="timeout_after_accept"
    )
    first = OrderManager(paper=True, policy_store=store).submit_replacement_buy(
        plan.decision.decision_id, portfolio_snapshot=portfolio, ports=ports
    )
    assert first.submission is not None and first.submission.outcome_uncertain
    assert len(broker.submissions) == 1
    restarted_store = PolicyExecutionStateStore(store.db_path, store_identity=deployment.store_identity)
    restarted_broker = FakeProtectedReplacementBuyBroker(
        broker.snapshot(),
        quote_symbol=plan.candidate_symbol,
        quote_price=Decimal("110"),
        receipt_store_path=receipt_path,
    )
    restarted_ports = replace(ports, store=restarted_store, broker=restarted_broker)
    manager = OrderManager(paper=True, policy_store=restarted_store)
    recovered = manager.reconcile_replacement_buy(plan.decision.decision_id, ports=restarted_ports)
    assert recovered.kind is ReplacementStepKind.WAITING_FOR_BUY
    assert recovered.action is not None
    assert recovered.action.order_attempts[0].broker_order_id is not None
    replay = manager.submit_replacement_buy(
        plan.decision.decision_id, portfolio_snapshot=portfolio, ports=restarted_ports
    )
    assert replay.submission is None
    assert restarted_broker.submissions == []
    assert account.cash == restarted_broker.snapshot().cash


def test_matching_order_without_durable_receipt_does_not_reconcile_buy(tmp_path):
    store, plan, deployment, account, portfolio, broker, ports, receipt_path = _ready_buy(tmp_path)
    due = advance_replacement(
        store, plan.decision.decision_id, account=account,
        portfolio_snapshot=portfolio, candidate_price=Decimal("110"),
    )
    assert due.action is not None
    buy = due.action
    client_id = _client_order_id(buy.logical_action_id)
    store.bind_attempt_order_refs(
        buy.logical_action_id,
        1,
        provider_id=ports.provider_id,
        client_order_id=client_id,
        expected_action_version=store.load_action_projection(buy.logical_action_id).state_version,
        observed_at=account.clock.valuation_time,
    )
    injected = BrokerOrderFact(
        broker_order_id="unreceipted-matching-buy",
        client_order_id=client_id,
        symbol=buy.broker_symbol,
        side="buy",
        status="submitted",
        requested_quantity=float(buy.requested_quantity),
        cumulative_filled_quantity=0,
        purpose="strategy",
        holding_episode_id=_holding_id(buy),
    )
    broker.observe_account(replace(account, open_orders=account.open_orders + (injected,)))
    restarted_store = PolicyExecutionStateStore(store.db_path, store_identity=deployment.store_identity)
    restarted_broker = FakeProtectedReplacementBuyBroker(
        broker.snapshot(), quote_symbol=plan.candidate_symbol,
        quote_price=Decimal("110"), receipt_store_path=receipt_path,
    )
    restarted_ports = replace(ports, store=restarted_store, broker=restarted_broker)
    waiting = OrderManager(paper=True, policy_store=restarted_store).reconcile_replacement_buy(
        plan.decision.decision_id, ports=restarted_ports
    )
    assert waiting.kind is ReplacementStepKind.WAITING_FOR_BUY
    assert waiting.action is not None
    assert waiting.action.order_attempts[0].broker_order_id is None
    assert restarted_broker.submissions == []


def test_partial_buy_restarts_and_resizes_exact_new_holding_stop(tmp_path):
    store, plan, deployment, account, portfolio, broker, ports, receipt_path = _ready_buy(tmp_path)
    manager = OrderManager(paper=True, policy_store=store)
    submitted = manager.submit_replacement_buy(
        plan.decision.decision_id, portfolio_snapshot=portfolio, ports=ports
    )
    assert submitted.submission is not None and submitted.submission.success
    broker_id = submitted.submission.broker_order_id
    broker.fill_buy(
        broker_id, Decimal("30"), fill_price=Decimal("110"),
        observed_at=account.clock.valuation_time,
    )
    first = manager.record_replacement_buy_fill(plan.decision.decision_id, ports=ports)
    assert first.kind is ReplacementStepKind.WAITING_FOR_BUY
    assert first.action is not None and first.action.status is ActionStatus.PARTIALLY_FILLED
    first_holding = store.load_holding_episode_for_action(first.action.logical_action_id)
    assert first_holding.remaining_quantity == Decimal("30")
    first_stop_id = first_holding.confirmed_stop_broker_order_id
    assert first_stop_id is not None

    restarted_store = PolicyExecutionStateStore(store.db_path, store_identity=deployment.store_identity)
    restarted_broker = FakeProtectedReplacementBuyBroker(
        broker.snapshot(), quote_symbol=plan.candidate_symbol,
        quote_price=Decimal("110"), receipt_store_path=receipt_path,
    )
    restarted_ports = replace(ports, store=restarted_store, broker=restarted_broker)
    restarted_broker.fill_buy(
        broker_id, Decimal("34"), fill_price=Decimal("110"),
        observed_at=account.clock.valuation_time,
    )
    final = OrderManager(paper=True, policy_store=restarted_store).record_replacement_buy_fill(
        plan.decision.decision_id, ports=restarted_ports
    )
    assert final.kind is ReplacementStepKind.COMPLETE
    assert final.action is not None
    holding = restarted_store.load_holding_episode_for_action(final.action.logical_action_id)
    assert holding.remaining_quantity == Decimal("64")
    assert holding.confirmed_stop_broker_order_id != first_stop_id
    stops = tuple(
        row for row in restarted_broker.snapshot().open_orders
        if row.purpose == "protective_stop" and row.symbol == plan.candidate_symbol
    )
    assert len(stops) == 1 and stops[0].requested_quantity == 64
    assert stops[0].broker_order_id == holding.confirmed_stop_broker_order_id


def test_full_buy_fill_waits_for_durable_stop_confirmation_after_restart(tmp_path):
    store, plan, deployment, account, portfolio, broker, ports, receipt_path = _ready_buy(tmp_path)
    manager = OrderManager(paper=True, policy_store=store)
    submitted = manager.submit_replacement_buy(
        plan.decision.decision_id, portfolio_snapshot=portfolio, ports=ports
    )
    assert submitted.submission is not None and submitted.submission.success
    broker_id = submitted.submission.broker_order_id
    broker.fill_buy(
        broker_id, Decimal("64"), fill_price=Decimal("110"),
        observed_at=account.clock.valuation_time,
    )
    buy = load_replacement_intention(store, plan.decision.decision_id).buy_action
    assert buy is not None
    PolicyProtectionBridge(store, provider_id=ports.provider_id).record_cumulative_fill(
        buy.logical_action_id,
        attempt_number=1,
        broker_order_id=broker_id,
        client_order_id=buy.order_attempts[0].client_order_id,
        cumulative_quantity=Decimal("64"),
        average_fill_price=Decimal("110"),
        cumulative_fees=Decimal("0"),
        observed_at=account.clock.valuation_time,
    )
    waiting = advance_replacement(store, plan.decision.decision_id)
    assert waiting.kind is ReplacementStepKind.WAITING_FOR_RECONCILIATION
    assert "stop" in (waiting.reason or "")

    restarted_store = PolicyExecutionStateStore(store.db_path, store_identity=deployment.store_identity)
    restarted_broker = FakeProtectedReplacementBuyBroker(
        broker.snapshot(), quote_symbol=plan.candidate_symbol,
        quote_price=Decimal("110"), receipt_store_path=receipt_path,
    )
    restarted_ports = replace(ports, store=restarted_store, broker=restarted_broker)
    recovered = OrderManager(paper=True, policy_store=restarted_store).record_replacement_buy_fill(
        plan.decision.decision_id, ports=restarted_ports
    )
    assert recovered.kind is ReplacementStepKind.COMPLETE
    assert recovered.action is not None
    assert restarted_store.load_holding_episode_for_action(
        recovered.action.logical_action_id
    ).confirmed_stop_broker_order_id is not None


def test_second_fill_cannot_complete_with_only_first_fill_stop_after_restart(tmp_path):
    store, plan, deployment, account, portfolio, broker, ports, receipt_path = _ready_buy(tmp_path)
    manager = OrderManager(paper=True, policy_store=store)
    submitted = manager.submit_replacement_buy(
        plan.decision.decision_id, portfolio_snapshot=portfolio, ports=ports
    )
    assert submitted.submission is not None and submitted.submission.success
    broker_id = submitted.submission.broker_order_id
    broker.fill_buy(
        broker_id, Decimal("30"), fill_price=Decimal("110"),
        observed_at=account.clock.valuation_time,
    )
    partial = manager.record_replacement_buy_fill(plan.decision.decision_id, ports=ports)
    assert partial.action is not None
    original_stop = store.load_holding_episode_for_action(
        partial.action.logical_action_id
    ).confirmed_stop_broker_order_id
    broker.fill_buy(
        broker_id, Decimal("34"), fill_price=Decimal("110"),
        observed_at=account.clock.valuation_time,
    )
    PolicyProtectionBridge(store, provider_id=ports.provider_id).record_cumulative_fill(
        partial.action.logical_action_id,
        attempt_number=1,
        broker_order_id=broker_id,
        client_order_id=partial.action.order_attempts[0].client_order_id,
        cumulative_quantity=Decimal("64"),
        average_fill_price=Decimal("110"),
        cumulative_fees=Decimal("0"),
        observed_at=account.clock.valuation_time,
    )
    waiting = advance_replacement(store, plan.decision.decision_id)
    assert waiting.kind is ReplacementStepKind.WAITING_FOR_RECONCILIATION
    assert store.load_holding_episode_for_action(
        partial.action.logical_action_id
    ).confirmed_stop_broker_order_id == original_stop

    restarted_store = PolicyExecutionStateStore(store.db_path, store_identity=deployment.store_identity)
    restarted_broker = FakeProtectedReplacementBuyBroker(
        broker.snapshot(), quote_symbol=plan.candidate_symbol,
        quote_price=Decimal("110"), receipt_store_path=receipt_path,
    )
    restarted_ports = replace(ports, store=restarted_store, broker=restarted_broker)
    recovered = OrderManager(paper=True, policy_store=restarted_store).record_replacement_buy_fill(
        plan.decision.decision_id, ports=restarted_ports
    )
    assert recovered.kind is ReplacementStepKind.COMPLETE
    assert restarted_store.load_holding_episode_for_action(
        partial.action.logical_action_id
    ).confirmed_stop_broker_order_id != original_stop


def test_buy_price_change_resolves_before_fake_broker_submission(tmp_path):
    store, plan, _, account, portfolio, broker, ports, _ = _ready_buy(tmp_path)
    due = advance_replacement(
        store,
        plan.decision.decision_id,
        account=account,
        portfolio_snapshot=portfolio,
        candidate_price=Decimal("110"),
    )
    assert due.kind is ReplacementStepKind.BUY_DUE
    broker.set_quote(Decimal("111"))
    result = OrderManager(paper=True, policy_store=store).submit_replacement_buy(
        plan.decision.decision_id, portfolio_snapshot=portfolio, ports=ports
    )
    assert result.step.kind is ReplacementStepKind.BLOCKED
    assert result.submission is None
    assert broker.submissions == []


def test_buy_account_change_resolves_before_fake_broker_submission(tmp_path):
    store, plan, _, account, portfolio, broker, ports, _ = _ready_buy(tmp_path)
    due = advance_replacement(
        store, plan.decision.decision_id, account=account,
        portfolio_snapshot=portfolio, candidate_price=Decimal("110"),
    )
    assert due.kind is ReplacementStepKind.BUY_DUE
    broker.observe_account(replace(account, account_snapshot_id="changed-fresh-account"))
    result = OrderManager(paper=True, policy_store=store).submit_replacement_buy(
        plan.decision.decision_id, portfolio_snapshot=portfolio, ports=ports
    )
    assert result.step.kind is ReplacementStepKind.BLOCKED
    assert result.submission is None
    assert broker.submissions == []


def test_same_account_id_with_changed_cash_resolves_authorized_buy(tmp_path):
    store, plan, _, account, portfolio, broker, ports, _ = _ready_buy(tmp_path)
    due = advance_replacement(
        store, plan.decision.decision_id, account=account,
        portfolio_snapshot=portfolio, candidate_price=Decimal("110"),
    )
    assert due.kind is ReplacementStepKind.BUY_DUE
    broker.observe_account(replace(account, cash=0))
    result = OrderManager(paper=True, policy_store=store).submit_replacement_buy(
        plan.decision.decision_id, portfolio_snapshot=portfolio, ports=ports
    )
    assert result.submission is None
    assert result.step.kind is ReplacementStepKind.BLOCKED
    assert broker.submissions == []
    assert all(row.symbol != plan.candidate_symbol for row in broker.snapshot().positions)


def test_fake_broker_will_not_share_cash_with_another_working_buy(tmp_path):
    _, plan, _, account, _, broker, _, _ = _ready_buy(tmp_path)
    other = BrokerOrderFact(
        "other-working-buy", "other-client", "XYZ", "buy", "submitted", 1, 0
    )
    broker.observe_account(replace(account, open_orders=account.open_orders + (other,)))
    result = broker.submit_protected_buy(
        symbol=plan.candidate_symbol,
        quantity=Decimal("1"),
        limit_price=Decimal("125"),
        stop_price=Decimal("100"),
        candidate_price=Decimal("110"),
        expected_account_snapshot_id=account.account_snapshot_id,
        expected_account=account,
        client_order_id="direct-fixture-client",
        holding_episode_id="direct-fixture-holding",
    )
    assert not result.success
    assert broker.submissions == []


def test_definitive_fake_buy_rejection_does_not_open_candidate(tmp_path):
    store, plan, _, _, portfolio, broker, ports, _ = _ready_buy(
        tmp_path, response_mode="reject_before_accept"
    )
    result = OrderManager(paper=True, policy_store=store).submit_replacement_buy(
        plan.decision.decision_id, portfolio_snapshot=portfolio, ports=ports
    )
    assert result.submission is not None and not result.submission.success
    assert result.step.kind is ReplacementStepKind.BLOCKED
    assert broker.submissions == []
    assert all(row.symbol != plan.candidate_symbol for row in broker.snapshot().positions)


def test_fake_buy_rejects_fill_at_or_below_its_protective_stop(tmp_path):
    store, plan, _, account, portfolio, broker, ports, _ = _ready_buy(tmp_path)
    result = OrderManager(paper=True, policy_store=store).submit_replacement_buy(
        plan.decision.decision_id, portfolio_snapshot=portfolio, ports=ports
    )
    assert result.submission is not None and result.submission.success
    with pytest.raises(ValueError, match="fixed order or cash cap"):
        broker.fill_buy(
            result.submission.broker_order_id,
            Decimal("1"),
            fill_price=Decimal("100"),
            observed_at=account.clock.valuation_time,
        )
    assert all(row.symbol != plan.candidate_symbol for row in broker.snapshot().positions)
    assert broker.snapshot().cash == account.cash


def test_fake_buy_receipt_with_missing_account_fill_fails_closed_after_restart(tmp_path):
    store, plan, _, _, portfolio, broker, ports, receipt_path = _ready_buy(tmp_path)
    submitted = OrderManager(paper=True, policy_store=store).submit_replacement_buy(
        plan.decision.decision_id, portfolio_snapshot=portfolio, ports=ports
    )
    assert submitted.submission is not None and submitted.submission.success
    before_fill = broker.snapshot()
    with sqlite3.connect(receipt_path) as conn:
        conn.execute(
            "UPDATE fake_replacement_buy_receipts SET filled_quantity='1', filled_notional='110'"
        )
        conn.commit()
    with pytest.raises(ValueError, match="did not recover together"):
        FakeProtectedReplacementBuyBroker(
            before_fill,
            quote_symbol=plan.candidate_symbol,
            quote_price=Decimal("110"),
            receipt_store_path=receipt_path,
        )


def test_fake_broker_rejects_stale_observation_after_protected_fill(tmp_path):
    store, plan, _, account, portfolio, broker, ports, _ = _ready_buy(tmp_path)
    submitted = OrderManager(paper=True, policy_store=store).submit_replacement_buy(
        plan.decision.decision_id, portfolio_snapshot=portfolio, ports=ports
    )
    assert submitted.submission is not None and submitted.submission.success
    before = broker.snapshot()
    broker.fill_buy(
        submitted.submission.broker_order_id, Decimal("64"),
        fill_price=Decimal("110"), observed_at=account.clock.valuation_time,
    )
    with pytest.raises(ValueError, match="did not recover together"):
        broker.observe_account(before)
    with pytest.raises(ValueError, match="did not recover together"):
        broker.observe_account(replace(broker.snapshot(), cash=broker.snapshot().cash + 1))
    assert broker.snapshot().account_snapshot_id != before.account_snapshot_id
