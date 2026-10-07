from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from core.policy_execution_state import (
    ActionAttemptStatus,
    ActionRole,
    ActionStatus,
    DecisionCategory,
    DecisionIdentity,
    DecisionSubjectType,
    OrderSide,
    build_action_intent,
)
from core.policy_execution_store import PolicyExecutionStateStore
from core.order_manager import AdditionExecutionPorts, OrderManager
from core.strategy_policy.account_reconciliation import (
    BrokerOrderFact,
    policy_execution_state_to_projection,
    reconcile_account_snapshot,
)
from core.strategy_policy.contracts_v3 import AddOnDecisionV3
from core.pit_feature_snapshot import HoldingFeaturesV3
from core.strategy_policy.adapter_v3 import StrategyPolicyAdapterV3
from tests.test_paper_policy_chain import read_chain, seed_pending_chain
from tests.test_strategy_policy_contract_fixtures import _fixture as _policy_fixture, _market_context
from core.policy_addition import (
    AdditionCancelResult,
    AdditionPlan,
    AdditionStepKind,
    AdditionSubmissionResult,
    cancel_addition_remainder,
    confirm_addition_cancel,
    confirm_addition_protection,
    load_addition_intention,
    record_addition_fill,
    replace_addition_protection,
    start_addition,
    submit_addition,
)


_ADD_PROVIDER = "offline-addition-fake-broker"
_ADD_CLIENT_ID = "client:policy-addition:CCC"
_ADD_BROKER_ID = "broker:policy-addition:CCC"
_STOP_CLIENT_ID = "client:policy-addition-stop:CCC"
_STOP_BROKER_ID = "broker:policy-addition-stop:CCC"


def _addition_fixture(tmp_path):
    features, path, deployment, account, portfolio, _, _ = seed_pending_chain(tmp_path)
    store = PolicyExecutionStateStore(path, store_identity=deployment.store_identity)
    original_chain = read_chain(store, deployment, portfolio)
    holding = next(item for item in original_chain.holding_episodes if item.broker_symbol == "CCC")
    mark_time = account.clock.valuation_time
    account = replace(
        account,
        equity=10060,
        account_snapshot_id="combined-account-addition-latest-mark",
        positions=tuple(
            replace(position, mark_price=110, mark_observed_at=mark_time)
            if position.symbol == "CCC"
            else position
            for position in account.positions
        ),
    )
    # The shared seed already has one addition decision in this session. Start
    # this independent fixed policy decision in the next session slot.
    from core.policy_execution_state import DecisionClock

    completed_session = account.clock.completed_session + timedelta(days=1)
    next_execution_session = account.clock.next_execution_session + timedelta(days=1)
    valuation_time = account.clock.valuation_time + timedelta(days=1)
    cutoff = account.clock.as_of_cutoff + timedelta(days=1)
    account = replace(
        account,
        clock=replace(
            account.clock,
            completed_session=completed_session,
            as_of_cutoff=cutoff,
            next_execution_session=next_execution_session,
            valuation_time=valuation_time,
        ),
        balance_observed_at=valuation_time,
        positions=tuple(
            replace(position, mark_observed_at=valuation_time)
            for position in account.positions
        ),
    )
    portfolio = replace(
        portfolio,
        clock=DecisionClock(
            exchange_id=portfolio.clock.exchange_id,
            decision_session=completed_session,
            as_of_cutoff_at=cutoff,
            next_execution_session=next_execution_session,
            account_valuation_session=next_execution_session,
            account_valuation_at=valuation_time,
        ),
        account_snapshot_id=account.account_snapshot_id,
        equity=Decimal("10060"),
        gross_exposure=Decimal("1060"),
        open_risk=Decimal("160"),
    )
    store.record_portfolio_snapshot(portfolio)

    decision = DecisionIdentity.build(
        deployment=deployment,
        clock=portfolio.clock,
        snapshot_sha256=hashlib.sha256((features.recorded_input_manifest_sha256 + ":m3-p2-addition").encode()).hexdigest(),
        category=DecisionCategory.ADDITION,
        subject_type=DecisionSubjectType.HOLDING,
        subject_id=holding.holding_episode_id,
    )
    raw = _policy_fixture()
    market = _market_context(raw["market_context"])
    market = replace(market, session=decision.clock.decision_session.isoformat())
    snapshot = StrategyPolicyAdapterV3.build_add_on_snapshot(
        market=market,
        entry_price=float(holding.entry_price),
        current_price=110,
        current_quantity=float(holding.remaining_quantity),
        equity=10060,
        cash=9000,
        open_position_risk=60,
        days_held=10,
        add_on_count=holding.addition_count,
        highest_price=120,
        lowest_price=95,
        features=HoldingFeaturesV3(
            current_rs_score=78,
            industry_group_rs=72,
            atr_20_fraction=0.04,
            volume_ratio=1.3,
        ),
    )
    add_on_decision = AddOnDecisionV3(
        add=True,
        risk_fraction=0.005,
        notional_fraction_cap=0.10,
        reason_code="fixture_momentum_add",
    )
    plan = AdditionPlan(
        decision=decision,
        add_on_snapshot=snapshot,
        policy_decision=add_on_decision,
        holding_episode_id=holding.holding_episode_id,
        source_account_snapshot_id=account.account_snapshot_id,
        source_portfolio_snapshot_id=portfolio.portfolio_snapshot_id,
        expected_holding_version=holding.state_version,
    )
    return store, plan, deployment, account, portfolio, holding


def _aggregate_cost_basis_fixture(tmp_path):
    store, base_plan, deployment, account, portfolio, _ = _addition_fixture(tmp_path)
    prior_clock = portfolio.clock
    observed_at = prior_clock.account_valuation_at
    prior_entry_decision = DecisionIdentity.build(
        deployment=deployment,
        clock=prior_clock,
        snapshot_sha256=base_plan.decision.snapshot_sha256,
        category=DecisionCategory.ENTRY,
        subject_type=DecisionSubjectType.SECURITY,
        subject_id="fixture:DDD",
    )
    entry = build_action_intent(
        decision=prior_entry_decision,
        security_id="fixture:DDD",
        broker_symbol="DDD",
        role=ActionRole.ENTRY,
        side=OrderSide.BUY,
        requested_quantity=Decimal("1"),
        reservation_price=Decimal("100"),
        reservation_price_basis="prior_entry_limit",
        reservation_stop_price=Decimal("90"),
        risk_per_unit=Decimal("10"),
        risk_basis="prior_entry_stop",
    )
    store.record_decision(
        entry.decision,
        policy_payload={"role": entry.role.value},
        guard_payload={"outcome": "fake_prior_entry"},
        effective_action_payload={"logical_action_id": entry.logical_action_id},
    )
    store.record_action_intent(entry, expected_version=None)
    projection = store.load_action_projection(entry.logical_action_id)
    projection = store.bind_attempt_order_refs(
        entry.logical_action_id,
        1,
        provider_id="offline-prior-add-fake",
        client_order_id="client:DDD:entry",
        broker_order_id="broker:DDD:entry",
        expected_action_version=projection.state_version,
        observed_at=observed_at,
    )
    store.record_cumulative_fill(
        entry.logical_action_id,
        1,
        provider_id="offline-prior-add-fake",
        fill_event_id="fill:DDD:entry:one-share",
        cumulative_quantity=Decimal("1"),
        cumulative_notional=Decimal("100"),
        cumulative_fees=Decimal("0"),
        payload_sha256=None,
        observed_at=observed_at,
        expected_action_version=projection.state_version,
        expected_holding_version=None,
    )
    holding = store.load_holding_episode_for_action(entry.logical_action_id)
    stop = store.propose_stop_update(
        holding.holding_episode_id,
        decision=entry.decision,
        stop_price=Decimal("90"),
        expected_holding_version=holding.state_version,
        observed_at=observed_at,
    )
    proposed_holding = store.load_holding_episode(holding.holding_episode_id)
    store.confirm_protective_stop(
        stop,
        stop_price=Decimal("90"),
        client_order_id="client:DDD:entry-stop",
        broker_order_id="broker:DDD:entry-stop",
        observed_at=observed_at,
        expected_holding_version=proposed_holding.state_version,
    )
    holding = store.load_holding_episode(holding.holding_episode_id)

    prior_add_decision = DecisionIdentity.build(
        deployment=deployment,
        clock=prior_clock,
        snapshot_sha256=base_plan.decision.snapshot_sha256,
        category=DecisionCategory.ADDITION,
        subject_type=DecisionSubjectType.HOLDING,
        subject_id=holding.holding_episode_id,
    )
    prior_add = build_action_intent(
        decision=prior_add_decision,
        security_id=holding.security_id,
        broker_symbol=holding.broker_symbol,
        holding_episode_id=holding.holding_episode_id,
        role=ActionRole.ADDITION,
        side=OrderSide.BUY,
        requested_quantity=Decimal("1"),
        status=ActionStatus.INTENDED,
        reservation_price=Decimal("120"),
        reservation_price_basis="prior_add_limit",
        reservation_stop_price=Decimal("90"),
        risk_per_unit=Decimal("30"),
        risk_basis="prior_add_stop",
    )
    store.record_decision(
        prior_add.decision,
        policy_payload={"role": prior_add.role.value},
        guard_payload={"outcome": "fake_confirmed_prior_add"},
        effective_action_payload={"logical_action_id": prior_add.logical_action_id},
    )
    store.record_action_intent(
        prior_add,
        expected_version=None,
        expected_holding_version=holding.state_version,
    )
    projection = store.load_action_projection(prior_add.logical_action_id)
    projection = store.bind_attempt_order_refs(
        prior_add.logical_action_id,
        1,
        provider_id="offline-prior-add-fake",
        client_order_id="client:DDD:prior-add",
        broker_order_id="broker:DDD:prior-add",
        expected_action_version=projection.state_version,
        observed_at=observed_at,
    )
    holding = store.load_holding_episode(holding.holding_episode_id)
    store.record_cumulative_fill(
        prior_add.logical_action_id,
        1,
        provider_id="offline-prior-add-fake",
        fill_event_id="fill:DDD:prior-add:one-share",
        cumulative_quantity=Decimal("1"),
        cumulative_notional=Decimal("120"),
        cumulative_fees=Decimal("0"),
        payload_sha256=None,
        observed_at=observed_at,
        expected_action_version=projection.state_version,
        expected_holding_version=holding.state_version,
    )
    holding = store.load_holding_episode(holding.holding_episode_id)
    assert holding.entry_price == Decimal("100")
    assert holding.cost_basis == Decimal("110")
    assert holding.remaining_quantity == Decimal("2")
    assert holding.addition_count == 1
    stop = store.propose_stop_update(
        holding.holding_episode_id,
        decision=prior_add.decision,
        stop_price=Decimal("90"),
        expected_holding_version=holding.state_version,
        observed_at=observed_at,
    )
    proposed_holding = store.load_holding_episode(holding.holding_episode_id)
    store.confirm_protective_stop(
        stop,
        stop_price=Decimal("90"),
        client_order_id="client:DDD:prior-add-stop",
        broker_order_id="broker:DDD:prior-add-stop",
        observed_at=observed_at,
        expected_holding_version=proposed_holding.state_version,
    )
    holding = store.load_holding_episode(holding.holding_episode_id)

    from core.policy_execution_state import DecisionClock

    completed_session = account.clock.completed_session + timedelta(days=1)
    next_execution_session = account.clock.next_execution_session + timedelta(days=1)
    valuation_time = account.clock.valuation_time + timedelta(days=1)
    cutoff = account.clock.as_of_cutoff + timedelta(days=1)
    ccc_position = next(position for position in account.positions if position.symbol == "CCC")
    positions = []
    for position in account.positions:
        mark = 110.0 if position.symbol == "CCC" else position.mark_price
        positions.append(
            replace(position, mark_price=mark, mark_observed_at=valuation_time)
        )
    positions.append(
        replace(
            ccc_position,
            symbol="DDD",
            quantity=2,
            mark_price=110,
            mark_observed_at=valuation_time,
        )
    )
    prior_stop_order = BrokerOrderFact(
        holding.confirmed_stop_broker_order_id,
        holding.confirmed_stop_client_order_id,
        holding.broker_symbol,
        "sell",
        "submitted",
        2,
        0,
        purpose="protective_stop",
        holding_episode_id=holding.holding_episode_id,
        stop_price=90,
    )
    source_account_id = "aggregate-cost-basis-prior-add-account"
    account = replace(
        account,
        account_snapshot_id=source_account_id,
        clock=replace(
            account.clock,
            completed_session=completed_session,
            as_of_cutoff=cutoff,
            next_execution_session=next_execution_session,
            valuation_time=valuation_time,
        ),
        equity=10060,
        cash=8780,
        balance_observed_at=valuation_time,
        positions=tuple(positions),
        open_orders=account.open_orders + (prior_stop_order,),
    )
    portfolio = replace(
        portfolio,
        clock=DecisionClock(
            exchange_id=portfolio.clock.exchange_id,
            decision_session=completed_session,
            as_of_cutoff_at=cutoff,
            next_execution_session=next_execution_session,
            account_valuation_session=next_execution_session,
            account_valuation_at=valuation_time,
        ),
        account_snapshot_id=source_account_id,
        equity=Decimal("10060"),
        cash=Decimal("8780"),
        gross_exposure=Decimal("1280"),
        open_risk=Decimal("200"),
    )
    store.record_portfolio_snapshot(portfolio)

    decision = DecisionIdentity.build(
        deployment=deployment,
        clock=portfolio.clock,
        snapshot_sha256=hashlib.sha256(
            (base_plan.decision.snapshot_sha256 + ":aggregate-cost-basis-regression").encode()
        ).hexdigest(),
        category=DecisionCategory.ADDITION,
        subject_type=DecisionSubjectType.HOLDING,
        subject_id=holding.holding_episode_id,
    )
    raw = _policy_fixture()
    market = replace(
        _market_context(raw["market_context"]),
        session=decision.clock.decision_session.isoformat(),
    )
    add_on_snapshot = StrategyPolicyAdapterV3.build_add_on_snapshot(
        market=market,
        entry_price=float(holding.entry_price),
        current_price=110,
        current_quantity=2,
        equity=10060,
        cash=8780,
        open_position_risk=40,
        days_held=10,
        add_on_count=holding.addition_count,
        highest_price=120,
        lowest_price=95,
        features=HoldingFeaturesV3(
            current_rs_score=78,
            industry_group_rs=72,
            atr_20_fraction=0.04,
            volume_ratio=1.3,
        ),
    )
    add_on_decision = AddOnDecisionV3(
        add=True,
        risk_fraction=0.009,
        notional_fraction_cap=0.10,
        reason_code="fixture_aggregate_basis_add",
    )
    plan = AdditionPlan(
        decision=decision,
        add_on_snapshot=add_on_snapshot,
        policy_decision=add_on_decision,
        holding_episode_id=holding.holding_episode_id,
        source_account_snapshot_id=account.account_snapshot_id,
        source_portfolio_snapshot_id=portfolio.portfolio_snapshot_id,
        expected_holding_version=holding.state_version,
    )
    return store, plan, account, portfolio, holding


def _bind_fake_addition(store, step, observed_at, *, broker_id=_ADD_BROKER_ID):
    assert step.action is not None
    current = store.load_action_projection(step.action.logical_action_id)
    return store.bind_attempt_order_refs(
        step.action.logical_action_id,
        1,
        provider_id=_ADD_PROVIDER,
        client_order_id=_ADD_CLIENT_ID,
        broker_order_id=broker_id,
        expected_action_version=current.state_version,
        observed_at=observed_at,
    )


def _addition_account_after_fill(account, plan, *, quantity, cash, order_status, cumulative):
    observed_at = account.clock.valuation_time + timedelta(minutes=2)
    account_clock = replace(account.clock, valuation_time=observed_at)
    positions = tuple(
        replace(position, quantity=quantity, mark_observed_at=observed_at)
        if position.symbol == "CCC"
        else replace(position, mark_observed_at=observed_at)
        for position in account.positions
    )
    buy = BrokerOrderFact(
        _ADD_BROKER_ID,
        _ADD_CLIENT_ID,
        "CCC",
        "buy",
        order_status,
        2,
        cumulative,
        purpose="strategy",
        holding_episode_id=plan.holding_episode_id,
    )
    prior_add_orders = tuple(
        order
        for order in account.open_orders
        if order.client_order_id != _ADD_CLIENT_ID and order.broker_order_id != _ADD_BROKER_ID
    )
    return replace(
        account,
        decision_id=plan.decision.decision_id,
        decision_slot_id=plan.decision.decision_slot_id,
        clock=account_clock,
        equity=10060,
        cash=cash,
        balance_observed_at=observed_at,
        positions=positions,
        open_orders=prior_add_orders + (buy,),
        account_snapshot_id=f"addition-account-{order_status}-{cumulative}",
    )


def _account_after_stop_replacement(account, plan, *, holding_quantity):
    observed_at = account.clock.valuation_time + timedelta(minutes=1)
    account_clock = replace(account.clock, valuation_time=observed_at)
    prior = tuple(
        order
        for order in account.open_orders
        if not (
            order.purpose == "protective_stop"
            and order.holding_episode_id == plan.holding_episode_id
        )
    )
    new_stop = BrokerOrderFact(
        _STOP_BROKER_ID,
        _STOP_CLIENT_ID,
        "CCC",
        "sell",
        "submitted",
        float(holding_quantity),
        0,
        purpose="protective_stop",
        holding_episode_id=plan.holding_episode_id,
        stop_price=90,
    )
    positions = tuple(
        replace(position, mark_observed_at=observed_at)
        for position in account.positions
    )
    return replace(
        account,
        clock=account_clock,
        equity=10060,
        cash=8890,
        balance_observed_at=observed_at,
        positions=positions,
        open_orders=prior + (new_stop,),
        account_snapshot_id="addition-account-after-stop-replacement",
    )


def _updated_portfolio(portfolio, account):
    valuation = account.clock.valuation_time
    from core.policy_execution_state import DecisionClock

    clock = DecisionClock(
        exchange_id=portfolio.clock.exchange_id,
        decision_session=account.clock.completed_session,
        as_of_cutoff_at=account.clock.as_of_cutoff,
        next_execution_session=account.clock.next_execution_session,
        account_valuation_session=account.clock.next_execution_session,
        account_valuation_at=valuation,
    )
    return replace(
        portfolio,
        clock=clock,
        account_snapshot_id=account.account_snapshot_id,
        equity=Decimal("10060"),
        cash=Decimal("8890"),
        gross_exposure=Decimal("1170"),
        open_risk=Decimal("180"),
    )


def test_addition_risk_uses_aggregate_cost_basis_after_confirmed_prior_add(tmp_path):
    store, plan, account, portfolio, holding = _aggregate_cost_basis_fixture(tmp_path)

    assert holding.entry_price == Decimal("100")
    assert holding.cost_basis == Decimal("110")
    assert holding.remaining_quantity == Decimal("2")
    assert holding.completed_additions_quantity == Decimal("1")
    assert holding.addition_count == 1

    step = start_addition(store, plan, account=account, portfolio_snapshot=portfolio)

    assert step.kind is AdditionStepKind.BUY_DUE
    assert step.action is not None
    assert step.action.requested_quantity == Decimal("3")
    record = store.load_decision_record(plan.decision.decision_id)
    assert Decimal(str(record.guard_payload["current_position_stop_risk"])) == Decimal("40")
    assert Decimal(str(record.guard_payload["remaining_position_stop_risk"])) == Decimal("60.6")
    assert Decimal(str(record.guard_payload["remaining_stop_risk"])) == Decimal("60.6")
    assert Decimal(str(record.guard_payload["reserved_buy_risk"])) == Decimal("60")


def test_addition_sizes_whole_shares_from_reconciled_cash_risk_and_notional(tmp_path):
    store, plan, deployment, account, portfolio, holding = _addition_fixture(tmp_path)

    step = start_addition(store, plan, account=account, portfolio_snapshot=portfolio)

    assert step.kind is AdditionStepKind.BUY_DUE
    assert step.action is not None
    assert step.action.role is ActionRole.ADDITION
    assert step.action.requested_quantity == Decimal("2")
    assert step.action.reservation_price == Decimal("110")
    assert step.action.reservation_stop_price == Decimal("90")
    assert step.action.risk_per_unit == Decimal("20")
    projection = store.load_action_projection(step.action.logical_action_id)
    assert projection.reservation_amount == Decimal("220")
    assert projection.residual_committed_risk == Decimal("40")
    record = store.load_decision_record(plan.decision.decision_id)
    assert Decimal(str(record.guard_payload["available_cash"])) == Decimal("7400")
    assert Decimal(str(record.guard_payload["current_position_stop_risk"])) == Decimal("60")
    assert Decimal(str(record.guard_payload["remaining_position_stop_risk"])) == Decimal("40.6")
    assert Decimal(str(record.guard_payload["remaining_stop_risk"])) == Decimal("40.6")
    assert Decimal(str(record.guard_payload["remaining_notional"])) == Decimal("346")

    restarted = PolicyExecutionStateStore(store.db_path, store_identity=deployment.store_identity)
    replay = start_addition(restarted, plan, account=account, portfolio_snapshot=portfolio)
    assert replay.kind is AdditionStepKind.BUY_DUE
    assert replay.action is not None
    assert replay.action.logical_action_id == step.action.logical_action_id
    assert restarted.load_holding_episode(holding.holding_episode_id).pending_action_ids == (
        step.action.logical_action_id,
    )


def test_uncertain_addition_submission_keeps_reservation_and_is_not_reissued_after_restart(tmp_path):
    store, plan, deployment, account, portfolio, _ = _addition_fixture(tmp_path)
    step = start_addition(store, plan, account=account, portfolio_snapshot=portfolio)
    calls = []

    def uncertain_submit(**request):
        calls.append(request)
        return AdditionSubmissionResult(
            success=False,
            outcome_uncertain=True,
            error="fake transport lost response",
        )

    first = submit_addition(
        store,
        plan.decision.decision_id,
        provider_id=_ADD_PROVIDER,
        observed_at=account.clock.valuation_time,
        submit=uncertain_submit,
    )
    assert first.kind is AdditionStepKind.WAITING_FOR_BUY
    assert len(calls) == 1
    assert step.action is not None
    action = store.load_action_projection(step.action.logical_action_id)
    assert action.status is ActionStatus.SUBMITTED
    assert action.reservation_amount == Decimal("220")
    assert action.residual_committed_risk == Decimal("40")

    restarted = PolicyExecutionStateStore(store.db_path, store_identity=deployment.store_identity)
    replay = load_addition_intention(restarted, plan.decision.decision_id)
    assert replay.kind is AdditionStepKind.WAITING_FOR_BUY
    again = submit_addition(
        restarted,
        plan.decision.decision_id,
        provider_id=_ADD_PROVIDER,
        observed_at=account.clock.valuation_time,
        submit=uncertain_submit,
    )
    assert again.kind is AdditionStepKind.WAITING_FOR_BUY
    assert len(calls) == 1


def test_partial_and_duplicate_fill_resolves_once_then_resizes_protection_to_exact_quantity(tmp_path):
    store, plan, deployment, account, portfolio, original_holding = _addition_fixture(tmp_path)
    due = start_addition(store, plan, account=account, portfolio_snapshot=portfolio)
    assert due.action is not None
    _bind_fake_addition(store, due, account.clock.valuation_time)

    first = record_addition_fill(
        store,
        plan.decision.decision_id,
        provider_id=_ADD_PROVIDER,
        fill_event_id="add-fill-one-share",
        cumulative_quantity=Decimal("1"),
        cumulative_notional=Decimal("110"),
        cumulative_fees=Decimal("0"),
        observed_at=account.clock.valuation_time,
    )
    assert first.kind is AdditionStepKind.WAITING_FOR_BUY
    projection_after_first = store.load_action_projection(due.action.logical_action_id)
    holding_after_first = store.load_holding_episode(original_holding.holding_episode_id)
    assert holding_after_first.remaining_quantity == Decimal("7")
    assert holding_after_first.initial_filled_quantity == original_holding.initial_filled_quantity
    assert holding_after_first.cost_basis == Decimal("710") / Decimal("7")
    assert holding_after_first.confirmed_protective_stop_price == Decimal("90")
    assert holding_after_first.confirmed_stop_broker_order_id == original_holding.confirmed_stop_broker_order_id
    assert holding_after_first.addition_count == original_holding.addition_count + 1
    assert projection_after_first.reservation_amount == Decimal("110")
    assert projection_after_first.residual_committed_risk == Decimal("20")

    record_addition_fill(
        store,
        plan.decision.decision_id,
        provider_id=_ADD_PROVIDER,
        fill_event_id="add-fill-one-share",
        cumulative_quantity=Decimal("1"),
        cumulative_notional=Decimal("110"),
        cumulative_fees=Decimal("0"),
        observed_at=account.clock.valuation_time,
    )
    record_addition_fill(
        store,
        plan.decision.decision_id,
        provider_id=_ADD_PROVIDER,
        fill_event_id="duplicate-add-watermark-one-share",
        cumulative_quantity=Decimal("1"),
        cumulative_notional=Decimal("110"),
        cumulative_fees=Decimal("0"),
        observed_at=account.clock.valuation_time,
    )
    assert store.load_action_projection(due.action.logical_action_id).state_version == projection_after_first.state_version
    assert store.load_holding_episode(original_holding.holding_episode_id).remaining_quantity == Decimal("7")
    assert store.load_action_projection(due.action.logical_action_id).reservation_amount == Decimal("110")

    after_fill_account = _addition_account_after_fill(
        account, plan, quantity=7, cash=8890, order_status="cancelled", cumulative=1
    )

    cancel_calls = []

    def uncertain_cancel(**request):
        cancel_calls.append(request)
        return AdditionCancelResult(outcome_uncertain=True)

    waiting_cancel = cancel_addition_remainder(
        store,
        plan.decision.decision_id,
        provider_id=_ADD_PROVIDER,
        observed_at=after_fill_account.clock.valuation_time,
        cancel=uncertain_cancel,
    )
    assert waiting_cancel.kind is AdditionStepKind.WAITING_FOR_BUY
    pending_cancel = store.load_action_projection(due.action.logical_action_id)
    assert pending_cancel.status is ActionStatus.CANCEL_REQUESTED
    assert pending_cancel.reservation_amount == Decimal("110")
    assert pending_cancel.residual_committed_risk == Decimal("20")

    restarted = PolicyExecutionStateStore(store.db_path, store_identity=deployment.store_identity)
    replay_cancel = cancel_addition_remainder(
        restarted,
        plan.decision.decision_id,
        provider_id=_ADD_PROVIDER,
        observed_at=after_fill_account.clock.valuation_time,
        cancel=lambda **_request: pytest.fail("uncertain cancel must not be resent"),
    )
    assert replay_cancel.kind is AdditionStepKind.WAITING_FOR_BUY
    assert len(cancel_calls) == 1

    store = restarted
    terminal = confirm_addition_cancel(
        store,
        plan.decision.decision_id,
        provider_id=_ADD_PROVIDER,
        result=AdditionCancelResult(
            outcome_uncertain=False,
            terminal_status=ActionAttemptStatus.CANCELLED,
            cumulative_quantity=Decimal("1"),
            cumulative_notional=Decimal("110"),
            cumulative_fees=Decimal("0"),
            fill_event_id="terminal-addition-watermark",
            account=after_fill_account,
        ),
        observed_at=after_fill_account.clock.valuation_time,
    )
    assert terminal.kind is AdditionStepKind.PROTECTION_DUE
    resolved = store.load_action_projection(due.action.logical_action_id)
    assert resolved.status is ActionStatus.RESOLVED
    assert resolved.confirmed_quantity == Decimal("1")
    assert resolved.reservation_amount == Decimal("0")
    assert resolved.residual_committed_risk == Decimal("0")
    assert store.load_holding_episode(original_holding.holding_episode_id).pending_action_ids == ()

    calls = []

    def uncertain_stop_replace(**request):
        calls.append(request)
        old_order = request["old_order"]
        assert old_order.broker_order_id == original_holding.confirmed_stop_broker_order_id
        assert request["quantity"] == Decimal("7")
        assert request["stop_price"] == Decimal("90")
        # The durable old stop remains confirmed while the atomic replacement is uncertain.
        assert any(
            order.broker_order_id == old_order.broker_order_id and order.status == "submitted"
            for order in after_fill_account.open_orders
        )
        return None

    waiting = replace_addition_protection(
        store,
        plan.decision.decision_id,
        account=after_fill_account,
        observed_at=after_fill_account.clock.valuation_time,
        replace_stop=uncertain_stop_replace,
    )
    assert waiting.kind is AdditionStepKind.WAITING_FOR_PROTECTION
    assert len(calls) == 1
    pending_holding = store.load_holding_episode(original_holding.holding_episode_id)
    assert pending_holding.confirmed_stop_client_order_id == original_holding.confirmed_stop_client_order_id
    assert pending_holding.confirmed_stop_broker_order_id == original_holding.confirmed_stop_broker_order_id
    assert pending_holding.pending_action_ids

    replay = replace_addition_protection(
        store,
        plan.decision.decision_id,
        account=after_fill_account,
        observed_at=after_fill_account.clock.valuation_time,
        replace_stop=uncertain_stop_replace,
    )
    assert replay.kind is AdditionStepKind.WAITING_FOR_PROTECTION
    assert len(calls) == 1

    refreshed_account = _account_after_stop_replacement(
        after_fill_account, plan, holding_quantity=7
    )
    protected = confirm_addition_protection(
        store,
        plan.decision.decision_id,
        account=refreshed_account,
        observed_at=refreshed_account.clock.valuation_time,
    )
    assert protected.kind is AdditionStepKind.PROTECTED
    final_holding = store.load_holding_episode(original_holding.holding_episode_id)
    assert final_holding.remaining_quantity == Decimal("7")
    assert final_holding.cost_basis == Decimal("710") / Decimal("7")
    assert final_holding.confirmed_protective_stop_price == Decimal("90")
    assert final_holding.confirmed_stop_client_order_id == _STOP_CLIENT_ID
    assert final_holding.confirmed_stop_broker_order_id == _STOP_BROKER_ID
    assert final_holding.completed_additions_quantity == original_holding.completed_additions_quantity + 1
    assert final_holding.addition_count == original_holding.addition_count + 1

    refreshed_account = _account_after_stop_replacement(
        after_fill_account, plan, holding_quantity=7
    )
    refreshed_portfolio = _updated_portfolio(portfolio, refreshed_account)
    store.record_portfolio_snapshot(refreshed_portfolio)
    snapshot = store.load_policy_execution_snapshot(
        deployment_generation_id=deployment.deployment_generation_id,
        portfolio_snapshot_id=refreshed_portfolio.portfolio_snapshot_id,
    )
    bound_account = replace(
        refreshed_account,
        decision_id=plan.decision.decision_id,
        decision_slot_id=plan.decision.decision_slot_id,
    )
    projection = policy_execution_state_to_projection(
        account=bound_account,
        portfolio_snapshot=snapshot.portfolio_snapshot,
        action_projections=snapshot.action_projections,
        holding_episodes=snapshot.holding_episodes,
    )
    reconciliation = reconcile_account_snapshot(
        account=bound_account,
        projection=projection,
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )
    assert reconciliation.ready, reconciliation.findings
    assert reconciliation.reserved_buy_cash == 1600
    assert reconciliation.reserved_buy_risk == 110
    assert reconciliation.available_cash == 7290
    assert reconciliation.total_committed_risk == 290


def test_definitive_rejection_resolves_action_without_changing_holding_or_stop(tmp_path):
    store, plan, deployment, account, portfolio, original_holding = _addition_fixture(tmp_path)
    start_addition(store, plan, account=account, portfolio_snapshot=portfolio)
    calls = []

    def rejected_submit(**request):
        calls.append(request)
        return AdditionSubmissionResult(
            success=False,
            broker_order_id="broker:rejected-addition",
            outcome_uncertain=False,
            error="synthetic buying power rejection",
        )

    rejected = submit_addition(
        store,
        plan.decision.decision_id,
        provider_id=_ADD_PROVIDER,
        observed_at=account.clock.valuation_time,
        submit=rejected_submit,
    )

    assert rejected.kind is AdditionStepKind.REJECTED
    assert len(calls) == 1
    assert rejected.action is not None
    projection = store.load_action_projection(rejected.action.logical_action_id)
    assert projection.status is ActionStatus.RESOLVED
    assert projection.confirmed_quantity == 0
    assert projection.reservation_amount == Decimal("0")
    after = store.load_holding_episode(original_holding.holding_episode_id)
    assert after.remaining_quantity == original_holding.remaining_quantity
    assert after.cost_basis == original_holding.cost_basis
    assert after.confirmed_stop_broker_order_id == original_holding.confirmed_stop_broker_order_id
    assert after.pending_action_ids == ()
    restarted = PolicyExecutionStateStore(store.db_path, store_identity=deployment.store_identity)
    replay = load_addition_intention(restarted, plan.decision.decision_id)
    assert replay.kind is AdditionStepKind.REJECTED
    assert replay.action is not None
    assert replay.action.logical_action_id == rejected.action.logical_action_id


def test_unavailable_decision_time_price_blocks_without_reserving_an_action(tmp_path):
    store, plan, _, account, portfolio, holding = _addition_fixture(tmp_path)
    missing_price = replace(account, positions=None)

    step = start_addition(store, plan, account=missing_price, portfolio_snapshot=portfolio)

    assert step.kind is AdditionStepKind.BLOCKED
    assert step.action is None
    assert store.load_holding_episode(holding.holding_episode_id).pending_action_ids == ()


def test_addition_blocks_when_plan_portfolio_snapshot_id_does_not_match(tmp_path):
    store, plan, _, account, portfolio, _ = _addition_fixture(tmp_path)
    mismatched_plan = replace(
        plan,
        source_portfolio_snapshot_id="portfolio:sha256:" + "0" * 64,
    )

    step = start_addition(
        store,
        mismatched_plan,
        account=account,
        portfolio_snapshot=portfolio,
    )

    assert step.kind is AdditionStepKind.BLOCKED
    assert step.action is None
    assert step.reason == "addition account or portfolio facts do not match the fixed decision"

def test_order_manager_addition_route_blocks_a_plan_bound_to_another_portfolio(tmp_path):
    store, plan, _, account, portfolio, _ = _addition_fixture(tmp_path)
    mismatched_plan = replace(plan, source_portfolio_snapshot_id="other-portfolio-snapshot")
    submissions = []
    ports = AdditionExecutionPorts(
        store=store,
        provider_id=_ADD_PROVIDER,
        get_account_snapshot=lambda: account,
        get_portfolio_snapshot=lambda _plan, _account: portfolio,
        observed_at=lambda: account.clock.valuation_time,
        submit_order=lambda **request: submissions.append(request)
        or AdditionSubmissionResult(success=True, broker_order_id=_ADD_BROKER_ID),
        cancel_order=lambda **_request: pytest.fail("blocked plan must not cancel"),
        replace_stop=lambda **_request: pytest.fail("blocked plan must not replace protection"),
    )

    step = OrderManager(paper=True).submit_addition(mismatched_plan, ports=ports)

    assert step.kind is AdditionStepKind.BLOCKED
    assert step.action is None
    assert submissions == []


def test_order_manager_addition_route_does_not_reissue_uncertain_submission(tmp_path):
    store, plan, _, account, portfolio, _ = _addition_fixture(tmp_path)
    submissions = []

    def uncertain_submit(**request):
        submissions.append(request)
        return AdditionSubmissionResult(
            success=False,
            outcome_uncertain=True,
            error="fake transport lost response",
        )

    ports = AdditionExecutionPorts(
        store=store,
        provider_id=_ADD_PROVIDER,
        get_account_snapshot=lambda: account,
        get_portfolio_snapshot=lambda _plan, _account: portfolio,
        observed_at=lambda: account.clock.valuation_time,
        submit_order=uncertain_submit,
        cancel_order=lambda **_request: pytest.fail("uncertain submit has no confirmed fill"),
        replace_stop=lambda **_request: pytest.fail("uncertain submit has no confirmed fill"),
    )
    manager = OrderManager(paper=True)

    first = manager.submit_addition(plan, ports=ports)
    replay = manager.submit_addition(plan, ports=ports)

    assert first.kind is AdditionStepKind.WAITING_FOR_BUY
    assert replay.kind is AdditionStepKind.WAITING_FOR_BUY
    assert first.action is not None
    assert replay.action is not None
    assert replay.action.logical_action_id == first.action.logical_action_id
    assert len(submissions) == 1


def test_order_manager_addition_route_reconciles_partial_fill_and_protective_stop(tmp_path):
    store, plan, _, account, portfolio, original_holding = _addition_fixture(tmp_path)
    after_fill_account = _addition_account_after_fill(
        account, plan, quantity=7, cash=8890, order_status="cancelled", cumulative=1
    )
    refreshed_account = _account_after_stop_replacement(after_fill_account, plan, holding_quantity=7)

    class FakeBroker:
        def __init__(self):
            self.account = account
            self.submissions = []
            self.cancellations = []
            self.stop_replacements = []
            self.portfolio_reads = []

        def read_account(self):
            return self.account

        def read_portfolio(self, fixed_plan, account_snapshot):
            self.portfolio_reads.append((fixed_plan, account_snapshot.account_snapshot_id))
            return portfolio

        def submit_order(self, **request):
            self.submissions.append(request)
            return AdditionSubmissionResult(success=True, broker_order_id=_ADD_BROKER_ID)

        def cancel_order(self, **request):
            self.cancellations.append(request)
            self.account = after_fill_account
            return AdditionCancelResult(outcome_uncertain=True)

        def replace_stop(self, **request):
            self.stop_replacements.append(request)
            assert request["old_order"].broker_order_id == original_holding.confirmed_stop_broker_order_id
            assert request["quantity"] == Decimal("7")
            assert request["stop_price"] == Decimal("90")
            # The fake broker accepted the atomic replacement, but its response was lost.
            self.account = refreshed_account
            return None

    broker = FakeBroker()
    ports = AdditionExecutionPorts(
        store=store,
        provider_id=_ADD_PROVIDER,
        get_account_snapshot=broker.read_account,
        get_portfolio_snapshot=broker.read_portfolio,
        observed_at=lambda: account.clock.valuation_time,
        submit_order=broker.submit_order,
        cancel_order=broker.cancel_order,
        replace_stop=broker.replace_stop,
    )
    manager = OrderManager(paper=True)

    submitted = manager.submit_addition(plan, ports=ports)
    assert submitted.kind is AdditionStepKind.WAITING_FOR_BUY
    assert submitted.action is not None
    assert submitted.action.status is ActionStatus.SUBMITTED
    assert len(broker.submissions) == 1
    assert broker.portfolio_reads == [(plan, account.account_snapshot_id)]

    waiting_for_cancel = manager.handle_addition_fill(
        plan.decision.decision_id,
        fill_event_id="manager-route-add-fill-one-share",
        cumulative_quantity=Decimal("1"),
        cumulative_notional=Decimal("110"),
        cumulative_fees=Decimal("0"),
        ports=ports,
    )
    duplicate_fill = manager.handle_addition_fill(
        plan.decision.decision_id,
        fill_event_id="manager-route-add-fill-one-share",
        cumulative_quantity=Decimal("1"),
        cumulative_notional=Decimal("110"),
        cumulative_fees=Decimal("0"),
        ports=ports,
    )
    assert waiting_for_cancel.kind is AdditionStepKind.WAITING_FOR_BUY
    assert duplicate_fill.kind is AdditionStepKind.WAITING_FOR_BUY
    assert len(broker.cancellations) == 1

    protected_pending_confirmation = manager.confirm_addition_cancel(
        plan.decision.decision_id,
        result=AdditionCancelResult(
            outcome_uncertain=False,
            terminal_status=ActionAttemptStatus.CANCELLED,
            cumulative_quantity=Decimal("1"),
            cumulative_notional=Decimal("110"),
            cumulative_fees=Decimal("0"),
            fill_event_id="manager-route-add-terminal-watermark",
            account=after_fill_account,
        ),
        ports=ports,
    )
    assert protected_pending_confirmation.kind is AdditionStepKind.WAITING_FOR_PROTECTION
    assert len(broker.stop_replacements) == 1

    protected = manager.confirm_addition_protection(plan.decision.decision_id, ports=ports)
    duplicate_after_protection = manager.handle_addition_fill(
        plan.decision.decision_id,
        fill_event_id="manager-route-add-fill-one-share",
        cumulative_quantity=Decimal("1"),
        cumulative_notional=Decimal("110"),
        cumulative_fees=Decimal("0"),
        ports=ports,
    )

    assert protected.kind is AdditionStepKind.PROTECTED
    assert duplicate_after_protection.kind is AdditionStepKind.PROTECTED
    assert len(broker.cancellations) == 1
    assert len(broker.stop_replacements) == 1
    holding = store.load_holding_episode(original_holding.holding_episode_id)
    assert holding.remaining_quantity == Decimal("7")
    assert holding.confirmed_stop_client_order_id == _STOP_CLIENT_ID
    assert holding.confirmed_stop_broker_order_id == _STOP_BROKER_ID
