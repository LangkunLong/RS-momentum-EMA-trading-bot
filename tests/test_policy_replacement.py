from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from core.policy_execution_state import (
    ActionAttemptStatus,
    ActionRole,
    DecisionClock,
    DecisionCategory,
    DecisionIdentity,
    DecisionSubjectType,
    OrderSide,
    build_action_intent,
)
from core.policy_execution_store import PolicyExecutionStateStore
from core.strategy_policy.account_reconciliation import AccountValuationClock, BrokerOrderFact
import core.policy_replacement as replacement_module
from core.policy_replacement import (
    ReplacementPlan,
    ReplacementStepKind,
    advance_replacement,
    load_replacement_intention,
    start_replacement,
)
from tests.test_paper_policy_chain import read_chain, seed_pending_chain


def _replacement_fixture(tmp_path):
    features, path, deployment, account, portfolio, _, _ = seed_pending_chain(tmp_path)
    store = PolicyExecutionStateStore(path, store_identity=deployment.store_identity)
    chain = read_chain(store, deployment, portfolio)
    held = next(item for item in chain.holding_episodes if item.broker_symbol == "CCC")
    decision = DecisionIdentity.build(
        deployment=deployment,
        clock=portfolio.clock,
        snapshot_sha256=features.recorded_input_manifest_sha256,
        category=DecisionCategory.REPLACEMENT,
        subject_type=DecisionSubjectType.SECURITY,
        subject_id="fixture:DDD",
    )
    sell = build_action_intent(
        decision=decision,
        security_id=held.security_id,
        broker_symbol=held.broker_symbol,
        role=ActionRole.CLOSE,
        side=OrderSide.SELL,
        requested_quantity=held.remaining_quantity,
        holding_episode_id=held.holding_episode_id,
    )
    plan = ReplacementPlan(
        decision=decision,
        sell_action=sell,
        candidate_security_id="fixture:DDD",
        candidate_symbol="DDD",
        maximum_buy_quantity=Decimal("100"),
        maximum_buy_price=Decimal("125"),
        reservation_stop_price=Decimal("100"),
        risk_per_unit=Decimal("25"),
        risk_basis="fixed_candidate_limit_minus_stop",
        quantity_increment=Decimal("1"),
        maximum_position_count=4,
        maximum_total_risk=Decimal("10000"),
        source_portfolio_snapshot_id=portfolio.portfolio_snapshot_id,
        source_account_snapshot_id=account.account_snapshot_id,
        expected_holding_version=held.state_version,
        policy_payload={"selected_holding_episode_id": held.holding_episode_id},
        guard_payload={"outcome": "allow_offline_fixture"},
    )
    return store, plan, deployment, account, portfolio, held


def test_replacement_intention_restarts_with_only_the_sell_action_due(tmp_path):
    store, plan, deployment, _, portfolio, original_holding = _replacement_fixture(tmp_path)

    step = start_replacement(store, plan)

    assert step.kind is ReplacementStepKind.SELL_DUE
    assert step.action is not None
    assert step.action.logical_action_id == plan.sell_action.logical_action_id
    assert step.action.role is ActionRole.CLOSE
    restarted = PolicyExecutionStateStore(store.db_path, store_identity=deployment.store_identity)
    recovered = load_replacement_intention(restarted, plan.decision.decision_id)
    assert recovered.sell_action.logical_action_id == plan.sell_action.logical_action_id
    assert recovered.buy_action is None
    persisted_holding = restarted.load_holding_episode(original_holding.holding_episode_id)
    assert persisted_holding.remaining_quantity == original_holding.remaining_quantity
    assert persisted_holding.confirmed_protective_stop_price == original_holding.confirmed_protective_stop_price
    assert persisted_holding.pending_action_ids == (plan.sell_action.logical_action_id,)
    assert portfolio.portfolio_snapshot_id == plan.source_portfolio_snapshot_id


_SELL_CLIENT_ID = "client:replacement:sell:CCC"
_SELL_BROKER_ID = "broker:replacement:sell:CCC"


def _submit_fake_sell(store, plan, observed_at):
    current = store.load_action_projection(plan.sell_action.logical_action_id)
    return store.bind_attempt_order_refs(
        plan.sell_action.logical_action_id,
        1,
        provider_id="offline-fake-broker",
        client_order_id=_SELL_CLIENT_ID,
        broker_order_id=_SELL_BROKER_ID,
        expected_action_version=current.state_version,
        observed_at=observed_at,
    )


def _record_fake_sell_fill(store, plan, quantity, *, notional=None, observed_at):
    action = store.load_action_projection(plan.sell_action.logical_action_id)
    holding = store.load_holding_episode(plan.sell_action.holding_episode_id)
    filled = Decimal(quantity)
    return store.record_cumulative_fill(
        plan.sell_action.logical_action_id,
        1,
        provider_id="offline-fake-broker",
        fill_event_id=f"replacement-sell-cumulative-{quantity}",
        cumulative_quantity=filled,
        cumulative_notional=Decimal(notional) if notional is not None else filled * Decimal("100"),
        cumulative_fees=Decimal("0"),
        payload_sha256=None,
        observed_at=observed_at,
        expected_action_version=action.state_version,
        expected_holding_version=holding.state_version,
    )


def _confirm_fake_sell_terminal(store, plan, status, observed_at):
    action = store.load_action_projection(plan.sell_action.logical_action_id)
    holding = store.load_holding_episode(plan.sell_action.holding_episode_id)
    return store.confirm_order_terminal(
        plan.sell_action.logical_action_id,
        1,
        terminal_status=status,
        expected_action_version=action.state_version,
        expected_holding_version=holding.state_version,
        observed_at=observed_at,
    )


def _retire_fake_flat_stop(store, plan, holding, account_snapshot_id):
    return store.retire_flat_holding_protection(
        plan.sell_action.logical_action_id,
        client_order_id=holding.confirmed_stop_client_order_id,
        broker_order_id=holding.confirmed_stop_broker_order_id,
        account_snapshot_id=account_snapshot_id,
        expected_holding_version=store.load_holding_episode(holding.holding_episode_id).state_version,
        observed_at=plan.decision.clock.account_valuation_at,
    )


def _refreshed_after_full_sale(store, account, portfolio, plan):
    value_time = account.clock.valuation_time + timedelta(minutes=2)
    account_clock = AccountValuationClock(
        completed_session=account.clock.completed_session,
        as_of_cutoff=account.clock.as_of_cutoff,
        next_execution_session=account.clock.next_execution_session,
        valuation_time=value_time,
    )
    holding = store.load_holding_episode(plan.sell_action.holding_episode_id)
    retained_orders = tuple(
        row
        for row in account.open_orders
        if row.broker_order_id != holding.confirmed_stop_broker_order_id
        and row.client_order_id != holding.confirmed_stop_client_order_id
    )
    sale_order = BrokerOrderFact(
        _SELL_BROKER_ID,
        _SELL_CLIENT_ID,
        "CCC",
        "sell",
        "filled",
        float(plan.sell_action.requested_quantity),
        float(plan.sell_action.requested_quantity),
    )
    refreshed_account = replace(
        account,
        clock=account_clock,
        cash=9600,
        balance_observed_at=value_time,
        peak_observed_at=value_time,
        positions=tuple(row for row in account.positions if row.symbol != "CCC"),
        open_orders=retained_orders + (sale_order,),
        account_snapshot_id="combined-account-after-replacement-sell",
    )
    decision_clock = DecisionClock(
        exchange_id=portfolio.clock.exchange_id,
        decision_session=account_clock.completed_session,
        as_of_cutoff_at=account_clock.as_of_cutoff,
        next_execution_session=account_clock.next_execution_session,
        account_valuation_session=account_clock.next_execution_session,
        account_valuation_at=account_clock.valuation_time,
    )
    refreshed_portfolio = replace(
        portfolio,
        clock=decision_clock,
        account_snapshot_id=refreshed_account.account_snapshot_id,
        cash=Decimal("9600"),
        gross_exposure=Decimal("400"),
        open_risk=Decimal("40"),
    )
    return refreshed_account, refreshed_portfolio


def test_partial_sell_and_delayed_cancel_never_release_a_buy(tmp_path):
    store, plan, _, _, _, original_holding = _replacement_fixture(tmp_path)
    start_replacement(store, plan)
    _submit_fake_sell(store, plan, plan.decision.clock.account_valuation_at)
    partial = _record_fake_sell_fill(
        store,
        plan,
        "3",
        observed_at=plan.decision.clock.account_valuation_at,
    )
    assert partial.status.value == "partially_filled"
    step = advance_replacement(store, plan.decision.decision_id)
    assert step.kind is ReplacementStepKind.WAITING_FOR_SELL
    holding = store.load_holding_episode(original_holding.holding_episode_id)
    assert holding.remaining_quantity == Decimal("3")
    assert holding.confirmed_protective_stop_price == original_holding.confirmed_protective_stop_price
    assert holding.confirmed_stop_broker_order_id == original_holding.confirmed_stop_broker_order_id

    cancel_requested = store.request_order_cancel(
        plan.sell_action.logical_action_id,
        1,
        expected_action_version=partial.state_version,
        observed_at=plan.decision.clock.account_valuation_at,
    )
    assert cancel_requested.status.value == "cancel_requested"
    delayed = advance_replacement(store, plan.decision.decision_id)
    assert delayed.kind is ReplacementStepKind.WAITING_FOR_SELL
    assert delayed.action is None

    _confirm_fake_sell_terminal(
        store, plan, ActionAttemptStatus.CANCELLED, plan.decision.clock.account_valuation_at
    )
    terminal = advance_replacement(store, plan.decision.decision_id)
    assert terminal.kind is ReplacementStepKind.BLOCKED
    assert terminal.action is None
    recovered = load_replacement_intention(store, plan.decision.decision_id)
    assert recovered.buy_action is None
    assert store.load_holding_episode(original_holding.holding_episode_id).remaining_quantity == Decimal("3")


def test_rejected_sell_keeps_protection_and_never_creates_a_buy(tmp_path):
    store, plan, _, _, _, original_holding = _replacement_fixture(tmp_path)
    start_replacement(store, plan)
    _submit_fake_sell(store, plan, plan.decision.clock.account_valuation_at)
    _confirm_fake_sell_terminal(
        store, plan, ActionAttemptStatus.REJECTED, plan.decision.clock.account_valuation_at
    )

    step = advance_replacement(store, plan.decision.decision_id)

    assert step.kind is ReplacementStepKind.BLOCKED
    assert step.action is None
    recovered = load_replacement_intention(store, plan.decision.decision_id)
    assert recovered.buy_action is None
    holding = store.load_holding_episode(original_holding.holding_episode_id)
    assert holding.remaining_quantity == original_holding.remaining_quantity
    assert holding.confirmed_protective_stop_price == original_holding.confirmed_protective_stop_price
    assert holding.confirmed_stop_broker_order_id == original_holding.confirmed_stop_broker_order_id


def test_restart_between_full_sale_and_buy_releases_one_cash_bounded_action(tmp_path):
    store, plan, deployment, account, portfolio, original_holding = _replacement_fixture(tmp_path)
    start_replacement(store, plan)
    _submit_fake_sell(store, plan, plan.decision.clock.account_valuation_at)
    _record_fake_sell_fill(
        store,
        plan,
        str(plan.sell_action.requested_quantity),
        observed_at=plan.decision.clock.account_valuation_at,
    )
    wait = advance_replacement(store, plan.decision.decision_id)
    assert wait.kind is ReplacementStepKind.WAITING_FOR_RECONCILIATION
    assert wait.action is None
    assert store.load_holding_episode(original_holding.holding_episode_id).remaining_quantity == 0

    refreshed_account, refreshed_portfolio = _refreshed_after_full_sale(
        store, account, portfolio, plan
    )
    stale_stop = advance_replacement(
        store,
        plan.decision.decision_id,
        account=refreshed_account,
        portfolio_snapshot=refreshed_portfolio,
        candidate_price=Decimal("100"),
    )
    assert stale_stop.kind is ReplacementStepKind.WAITING_FOR_RECONCILIATION
    assert "stop" in (stale_stop.reason or "")
    assert load_replacement_intention(store, plan.decision.decision_id).buy_action is None
    _retire_fake_flat_stop(store, plan, original_holding, refreshed_account.account_snapshot_id)
    next_store = PolicyExecutionStateStore(store.db_path, store_identity=deployment.store_identity)
    buy_step = advance_replacement(
        next_store,
        plan.decision.decision_id,
        account=refreshed_account,
        portfolio_snapshot=refreshed_portfolio,
        candidate_price=Decimal("100"),
    )
    assert buy_step.kind is ReplacementStepKind.BUY_DUE
    assert buy_step.action is not None
    assert buy_step.action.role is ActionRole.REPLACEMENT
    assert buy_step.action.requested_quantity == Decimal("64")
    assert buy_step.action.reservation_price == Decimal("125")
    assert buy_step.action.requested_quantity * buy_step.action.reservation_price == Decimal("8000")

    restarted = PolicyExecutionStateStore(store.db_path, store_identity=deployment.store_identity)
    replay = advance_replacement(
        restarted,
        plan.decision.decision_id,
        account=refreshed_account,
        portfolio_snapshot=refreshed_portfolio,
        candidate_price=Decimal("100"),
    )
    assert replay.kind is ReplacementStepKind.BUY_DUE
    assert replay.action is not None
    assert replay.action.logical_action_id == buy_step.action.logical_action_id

    changed_snapshot_account = replace(
        refreshed_account,
        account_snapshot_id="changed-account-snapshot-after-buy-authorization",
    )
    changed = advance_replacement(
        restarted,
        plan.decision.decision_id,
        account=changed_snapshot_account,
        portfolio_snapshot=refreshed_portfolio,
        candidate_price=Decimal("100"),
    )
    assert changed.kind is ReplacementStepKind.BLOCKED
    assert changed.action is not None
    assert changed.action.status.value == "resolved"

    recovered = load_replacement_intention(restarted, plan.decision.decision_id)
    assert recovered.buy_action is not None
    assert recovered.buy_action.logical_action_id == buy_step.action.logical_action_id
    assert recovered.buy_action.status.value == "resolved"
    replay_after_change = advance_replacement(
        restarted,
        plan.decision.decision_id,
        account=refreshed_account,
        portfolio_snapshot=refreshed_portfolio,
        candidate_price=Decimal("100"),
    )
    assert replay_after_change.kind is ReplacementStepKind.BLOCKED
    assert replay_after_change.action is not None
    assert replay_after_change.action.status.value == "resolved"


def test_persisted_buy_replay_waits_for_old_stop_retirement(tmp_path, monkeypatch):
    store, plan, deployment, account, portfolio, original_holding = _replacement_fixture(tmp_path)
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
    # Simulate a buy intention persisted by the earlier state machine.
    with monkeypatch.context() as patch:
        patch.setattr(replacement_module, "_flat_stop_retirement_reason", lambda *_: None)
        prior = advance_replacement(
            store,
            plan.decision.decision_id,
            account=refreshed_account,
            portfolio_snapshot=refreshed_portfolio,
            candidate_price=Decimal("100"),
        )
    assert prior.kind is ReplacementStepKind.BUY_DUE
    assert prior.action is not None

    restarted = PolicyExecutionStateStore(store.db_path, store_identity=deployment.store_identity)
    held = advance_replacement(
        restarted,
        plan.decision.decision_id,
        account=refreshed_account,
        portfolio_snapshot=refreshed_portfolio,
        candidate_price=Decimal("100"),
    )
    assert held.kind is ReplacementStepKind.WAITING_FOR_RECONCILIATION
    assert held.action is not None and held.action.logical_action_id == prior.action.logical_action_id
    assert "stop" in (held.reason or "")
    assert load_replacement_intention(restarted, plan.decision.decision_id).buy_action is not None

    _retire_fake_flat_stop(
        restarted, plan, original_holding, refreshed_account.account_snapshot_id
    )
    released = advance_replacement(
        restarted,
        plan.decision.decision_id,
        account=refreshed_account,
        portfolio_snapshot=refreshed_portfolio,
        candidate_price=Decimal("100"),
    )
    assert released.kind is ReplacementStepKind.BUY_DUE
    assert released.action is not None and released.action.logical_action_id == prior.action.logical_action_id


def test_candidate_price_change_resolves_unsubmitted_buy_and_cannot_reopen(tmp_path):
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
    _retire_fake_flat_stop(
        store,
        plan,
        store.load_holding_episode(plan.sell_action.holding_episode_id),
        refreshed_account.account_snapshot_id,
    )

    buy_due = advance_replacement(
        store,
        plan.decision.decision_id,
        account=refreshed_account,
        portfolio_snapshot=refreshed_portfolio,
        candidate_price=Decimal("100"),
    )
    assert buy_due.kind is ReplacementStepKind.BUY_DUE
    assert buy_due.action is not None
    assert buy_due.action.status.value == "intended"
    buy_action_id = buy_due.action.logical_action_id

    restarted = PolicyExecutionStateStore(store.db_path, store_identity=deployment.store_identity)
    changed_price = advance_replacement(
        restarted,
        plan.decision.decision_id,
        account=refreshed_account,
        portfolio_snapshot=refreshed_portfolio,
        candidate_price=Decimal("101"),
    )
    assert changed_price.kind is ReplacementStepKind.BLOCKED
    assert changed_price.action is not None
    assert changed_price.action.logical_action_id == buy_action_id
    assert changed_price.action.status.value == "resolved"

    resolved = load_replacement_intention(restarted, plan.decision.decision_id)
    assert resolved.buy_action is not None
    assert resolved.buy_action.logical_action_id == buy_action_id
    assert resolved.buy_action.status.value == "resolved"

    replayed_old_price = advance_replacement(
        restarted,
        plan.decision.decision_id,
        account=refreshed_account,
        portfolio_snapshot=refreshed_portfolio,
        candidate_price=Decimal("100"),
    )
    assert replayed_old_price.kind is ReplacementStepKind.BLOCKED
    assert replayed_old_price.action is not None
    assert replayed_old_price.action.logical_action_id == buy_action_id
    assert replayed_old_price.action.status.value == "resolved"

    still_resolved = load_replacement_intention(restarted, plan.decision.decision_id)
    assert still_resolved.buy_action is not None
    assert still_resolved.buy_action.logical_action_id == buy_action_id
    assert still_resolved.buy_action.status.value == "resolved"
