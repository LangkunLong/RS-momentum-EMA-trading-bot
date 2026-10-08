"""Guard-owned public paper lifecycle controls with fixed policies and fake brokers."""
from datetime import timedelta
from decimal import Decimal

import pytest

from tests import test_issue_101_paper_policy as owner101


def _orchestrator(api, *, store, bundle_path, features, account, guard, consumer, active_symbols):
    """Pass the actual #99 result through unchanged, including its snapshot ID."""
    return api.PaperPolicyOrchestrator(
        policy_store=store, bundle_path=bundle_path, paper_account_environment_id="offline-paper-account-v1",
        feature_context=features, account=account, candidates=owner101._candidate_inputs(features),
        guard_profile=guard, order_manager=consumer, open_position_count=0, active_symbols=active_symbols,
    )


def _setup(tmp_path, monkeypatch, *, store_id):
    from core.execution_workflow import clear_workflow_registry
    from core.order_manager import OrderManager
    from core.strategy_policy.frozen_bundle import inspect_frozen_policy_bundle
    from core.policy_execution_state import DecisionClock
    from tests.paper_verification.parity_data import reconcile_actual

    api = owner101._policy_api()
    features = owner101._feature_fixture(tmp_path / "inputs")
    guard = owner101._guard_profile(api, maximum_positions=1)
    bundle = owner101._write_policy_bundle(
        tmp_path / "policy",
        feature_contract_id=features.feature_contract_id,
        feature_calculator_identity=features.feature_calculator_identity,
    )
    deployment = owner101._identity(
        inspect_frozen_policy_bundle(bundle), features, guard,
        account_id="offline-paper-account-v1", store_id=store_id,
    )
    store = owner101._active_store(tmp_path, deployment, guard.guard_id)
    source_clock = features.decision_clock
    clock = DecisionClock(features.decision_clock.completion_evidence.exchange_timezone, source_clock.completed_session, source_clock.as_of_cutoff,
                          source_clock.next_eligible_session, source_clock.next_eligible_session, source_clock.valuation_time)
    account = reconcile_actual(store, deployment, clock, label="delivery-flat-pretrade")
    monkeypatch.setattr(
        "core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(store.db_path)
    )
    clear_workflow_registry()
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setattr("auto_trader.settings.ENTRY_MARKET_HOURS_ONLY", True)
    monkeypatch.setattr("auto_trader._is_market_open", lambda: True)
    manager = OrderManager(paper=True, policy_store=store)
    return api, features, account, guard, bundle, deployment, store, manager


def _fresh_flat_account(store, action, *, snapshot_id, broker_orders=()):
    """Read actual durable reservations through the accepted #99/#100 conversion."""
    from core.policy_execution_state import PortfolioStateSnapshot
    from core.strategy_policy.account_reconciliation import (
        AccountValuationClock,
        BrokerAccountSnapshot,
        policy_execution_state_to_projection,
        reconcile_account_snapshot,
    )

    deployment = action.decision.deployment_identity
    clock = action.decision.clock
    portfolio = PortfolioStateSnapshot(
        deployment_identity=deployment, clock=clock,
        source_namespace="synthetic-delivery-broker", account_snapshot_id=snapshot_id,
        equity=Decimal("10000"), cash=Decimal("10000"),
        gross_exposure=Decimal("0"), open_risk=Decimal("0"),
        portfolio_peak_equity=Decimal("10000"),
        last_accepted_session=clock.decision_session,
    )
    store.record_portfolio_snapshot(portfolio)
    canonical = store.load_policy_execution_snapshot(
        deployment_generation_id=deployment.deployment_generation_id,
        portfolio_snapshot_id=portfolio.portfolio_snapshot_id,
    )
    broker = BrokerAccountSnapshot(
        paper_account_environment_id=deployment.paper_account_environment_id,
        decision_slot_id=action.decision.decision_slot_id,
        decision_id=action.decision.decision_id,
        clock=AccountValuationClock(
            completed_session=clock.decision_session,
            as_of_cutoff=clock.as_of_cutoff_at,
            next_execution_session=clock.next_execution_session,
            valuation_time=clock.account_valuation_at,
        ),
        equity=10000.0, cash=10000.0, peak_equity=10000.0,
        balance_observed_at=clock.account_valuation_at,
        peak_observed_at=clock.account_valuation_at,
        positions=(), open_orders=tuple(broker_orders),
        source_namespace="synthetic-delivery-broker", account_snapshot_id=snapshot_id,
    )
    projection = policy_execution_state_to_projection(
        account=broker, portfolio_snapshot=canonical.portfolio_snapshot,
        action_projections=canonical.action_projections,
        holding_episodes=canonical.holding_episodes,
    )
    reconciled = reconcile_account_snapshot(
        account=broker, projection=projection,
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )
    assert reconciled.ready, reconciled.findings
    assert reconciled.pending_entry_count == 1
    assert reconciled.reserved_buy_cash == pytest.approx(
        float(action.requested_quantity * action.reservation_price)
    )
    return reconciled


def test_fresh_account_recovers_original_intent_through_real_consumer(tmp_path, monkeypatch):
    from auto_trader import run_auto_trader
    from core.execution_workflow import clear_workflow_registry
    from core.order_execution import OrderResult
    from core.order_manager import OrderManager
    from core.policy_execution_store import PolicyExecutionStateStore
    from core.strategy_policy.account_reconciliation import BrokerOrderFact

    api, features, account, guard, bundle, _, store, manager = _setup(
        tmp_path, monkeypatch, store_id="delivery-fresh-recovery"
    )
    submissions = []

    def submit(**kwargs):
        submissions.append(kwargs)
        return OrderResult(
            True, "delivery-recovered-entry", kwargs["symbol"], "buy", kwargs["qty"],
            client_order_id=kwargs["client_order_id"],
        )

    monkeypatch.setattr("core.order_manager.submit_bracket_buy", submit)
    original_record = store.record_action_intent
    persisted = []

    def persist_then_interrupt(action, *args, **kwargs):
        original_record(action, *args, **kwargs)
        persisted.append(action)
        raise RuntimeError("delivery interruption after durable intent")

    monkeypatch.setattr(store, "record_action_intent", persist_then_interrupt)
    first = _orchestrator(
        api, store=store, bundle_path=bundle, features=features,
        account=account, guard=guard, consumer=manager, active_symbols=(),
    )
    with pytest.raises(RuntimeError, match="delivery interruption"):
        run_auto_trader(
            dry_run=False, skip_exits=True, execution_ready=lambda: True,
            policy_orchestrator=first,
        )
    assert len(persisted) == 1 and submissions == []
    action = persisted[0]
    clear_workflow_registry()
    reopened = PolicyExecutionStateStore(store.db_path, store_identity=store.store_identity)
    fresh = _fresh_flat_account(reopened, action, snapshot_id="after-crash-fresh")
    assert fresh.snapshot_id != account.snapshot_id
    recovered = _orchestrator(
        api, store=reopened, bundle_path=bundle, features=features,
        account=fresh, guard=guard, consumer=OrderManager(paper=True, policy_store=reopened),
        active_symbols=(action.broker_symbol,),
    )
    run_auto_trader(
        dry_run=False, skip_exits=True, execution_ready=lambda: True,
        policy_orchestrator=recovered,
    )
    assert len(submissions) == 1
    assert Decimal(str(submissions[0]["qty"])) == action.requested_quantity
    hydrated = reopened.load_action_intent(action.logical_action_id)
    assert hydrated.decision == action.decision
    assert hydrated.logical_action_id == action.logical_action_id
    assert hydrated.requested_quantity == action.requested_quantity
    assert hydrated.reservation_price == action.reservation_price
    assert hydrated.reservation_stop_price == action.reservation_stop_price
    projection = reopened.load_action_projection(action.logical_action_id)
    attempt = projection.order_attempts[0]
    assert attempt.broker_order_id == "delivery-recovered-entry"
    assert attempt.client_order_id == submissions[0]["client_order_id"]
    clear_workflow_registry()
    replay_store = PolicyExecutionStateStore(store.db_path, store_identity=store.store_identity)
    replay_account = _fresh_flat_account(
        replay_store, action, snapshot_id="after-submit-fresh",
        broker_orders=(BrokerOrderFact(
            attempt.broker_order_id, attempt.client_order_id, action.broker_symbol,
            "buy", "submitted", float(action.requested_quantity), 0,
        ),),
    )
    replay = _orchestrator(
        api, store=replay_store, bundle_path=bundle, features=features,
        account=replay_account, guard=guard,
        consumer=OrderManager(paper=True, policy_store=replay_store),
        active_symbols=(action.broker_symbol,),
    )
    run_auto_trader(
        dry_run=False, skip_exits=True, execution_ready=lambda: True,
        policy_orchestrator=replay,
    )
    assert len(submissions) == 1


def test_fixed_policy_public_entry_partial_fill_protection_and_restart(tmp_path, monkeypatch):
    from auto_trader import run_auto_trader
    from core.execution_store import get_execution_store
    from core.execution_workflow import clear_workflow_registry, get_workflow
    from core.order_execution import OrderResult, ProtectiveStopResult
    from core.order_manager import OrderManager
    from core.policy_execution_store import PolicyExecutionStateStore

    api, features, account, guard, bundle, _, store, manager = _setup(
        tmp_path, monkeypatch, store_id="delivery-public-lifecycle"
    )
    submissions, stop_calls = [], []

    def submit(**kwargs):
        submissions.append(kwargs)
        return OrderResult(
            True, "delivery-lifecycle-entry", kwargs["symbol"], "buy", kwargs["qty"],
            client_order_id=kwargs["client_order_id"],
        )

    def protect(**kwargs):
        stop_calls.append(kwargs)
        sequence = len(stop_calls)
        return ProtectiveStopResult(
            True, f"delivery-stop-{sequence}", kwargs["symbol"], kwargs["qty"],
            kwargs["stop_price_override"], "submitted",
            client_order_id=f"delivery-stop-client-{sequence}",
        )

    monkeypatch.setattr("core.order_manager.submit_bracket_buy", submit)
    monkeypatch.setattr("core.order_execution.ensure_protective_stop", protect)
    orchestrator = _orchestrator(
        api, store=store, bundle_path=bundle, features=features,
        account=account, guard=guard, consumer=manager, active_symbols=(),
    )
    outcome = run_auto_trader(
        dry_run=False, skip_exits=True, execution_ready=lambda: True,
        policy_orchestrator=orchestrator,
    )
    assert outcome.entered == ("AAA",) and len(submissions) == 1
    submitted = submissions[0]
    workflow_id = submitted["client_order_id"]
    workflow = get_workflow(workflow_id)
    action_id = next(
        transition.details["signal"]["logical_action_id"]
        for transition in workflow.transitions
        if transition.event == "signal_accepted"
    )
    action = store.load_action_intent(action_id)
    requested = float(action.requested_quantity)
    partial = requested / 2
    assert requested > 0
    manager.handle_partial_fill(
        symbol="AAA", broker_order_id="delivery-lifecycle-entry",
        client_order_id=workflow_id, side="buy", filled_qty=partial,
        fill_price=float(action.reservation_price), order_type="limit",
    )
    holding = store.load_holding_episode_for_action(action_id)
    assert holding.remaining_quantity == Decimal(str(partial))
    assert holding.confirmed_protective_stop_price == action.reservation_stop_price
    assert stop_calls[-1]["qty"] == partial
    assert stop_calls[-1]["stop_price_override"] == float(action.reservation_stop_price)
    assert get_execution_store().load_active_position("AAA")["qty"] == partial

    clear_workflow_registry()
    reopened = PolicyExecutionStateStore(store.db_path, store_identity=store.store_identity)
    manager = OrderManager(paper=True, policy_store=reopened)
    manager.handle_fill(
        symbol="AAA", broker_order_id="delivery-lifecycle-entry",
        client_order_id=workflow_id, side="buy", filled_qty=requested,
        fill_price=float(action.reservation_price), order_type="limit",
    )
    full = reopened.load_holding_episode_for_action(action_id)
    assert full.holding_episode_id == holding.holding_episode_id
    assert full.remaining_quantity == action.requested_quantity
    assert full.confirmed_protective_stop_price == action.reservation_stop_price
    assert stop_calls[-1]["qty"] == requested
    assert get_execution_store().load_active_position("AAA")["qty"] == requested
    assert len(submissions) == 1
    manager.handle_fill(
        symbol="AAA", broker_order_id="delivery-lifecycle-entry",
        client_order_id=workflow_id, side="buy", filled_qty=requested,
        fill_price=float(action.reservation_price), order_type="limit",
    )
    assert reopened.load_action_projection(action_id).confirmed_quantity == action.requested_quantity
    assert reopened.load_holding_episode_for_action(action_id).remaining_quantity == action.requested_quantity
    assert len(submissions) == 1


def test_public_policy_identity_change_rejects_before_module_or_broker_effects(tmp_path, monkeypatch):
    from contextlib import closing
    import sqlite3

    from auto_trader import run_auto_trader

    api, features, account, guard, bundle, _, store, manager = _setup(
        tmp_path, monkeypatch, store_id="delivery-public-identity-rejection"
    )
    module_effect = "UNAUTHENTICATED_POLICY_BODY_EXECUTED"
    entry_path = bundle / "entry.py"
    entry_path.write_bytes(entry_path.read_bytes() + f'\nraise AssertionError("{module_effect}")\n'.encode())
    submissions = []

    def forbidden_submission(**kwargs):
        submissions.append(kwargs)
        raise AssertionError("identity mismatch reached broker")

    monkeypatch.setattr("core.order_manager.submit_bracket_buy", forbidden_submission)
    orchestrator = _orchestrator(api, store=store, bundle_path=bundle, features=features, account=account,
                                 guard=guard, consumer=manager, active_symbols=())
    with pytest.raises(api.PaperPolicyOrchestrationError, match="selected policy artifact failed identity validation"):
        run_auto_trader(dry_run=False, skip_exits=True, execution_ready=lambda: True,
                       policy_orchestrator=orchestrator)
    assert submissions == []
    with closing(sqlite3.connect(str(store.db_path))) as connection:
        assert connection.execute("SELECT count(*) FROM policy_state_actions").fetchone()[0] == 0
