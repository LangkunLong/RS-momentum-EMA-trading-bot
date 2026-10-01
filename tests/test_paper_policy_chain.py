"""Combined feature, durable state, and account reconciliation regressions.

Uses the existing controlled V3 fixture builder to avoid maintaining a second
copy of its provenance and complete-universe construction. All durable state
uses explicit temporary databases and recorded broker/account facts.
"""

from tests.paper_policy_fixtures import build_feature_fixture, build_chain_identity
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
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
    PortfolioStateSnapshot,
    bind_attempt_order_refs,
    build_action_intent,
)
from core.policy_execution_store import PolicyExecutionStateStore
from core.strategy_policy.account_reconciliation import (
    AccountValuationClock,
    BrokerAccountSnapshot,
    BrokerOrderFact,
    BrokerPositionFact,
    ClassificationFact,
    policy_execution_state_to_projection,
    reconcile_account_snapshot,
)


def build_entry(features, deployment, clock, symbol, quantity, price):
    decision = DecisionIdentity.build(
        deployment=deployment,
        clock=clock,
        snapshot_sha256=features.recorded_input_manifest_sha256,
        category=DecisionCategory.ENTRY,
        subject_type=DecisionSubjectType.SECURITY,
        subject_id=f"fixture:{symbol}",
    )
    action = build_action_intent(
        decision=decision,
        security_id=f"fixture:{symbol}",
        broker_symbol=symbol,
        role=ActionRole.ENTRY,
        side=OrderSide.BUY,
        requested_quantity=Decimal(quantity),
        status=ActionStatus.SUBMITTED,
        reservation_price=Decimal(price),
        reservation_price_basis="recorded_limit",
        reservation_stop_price=Decimal(price) - Decimal("10"),
        risk_per_unit=Decimal("10"),
        risk_basis="recorded_limit_minus_stop",
    )
    return bind_attempt_order_refs(
        action, attempt_number=1, client_order_id=f"client:{symbol}", broker_order_id=f"broker:{symbol}"
    )


def build_account(deployment, clock, decision):

    def classification(code):
        return ClassificationFact(
            code=code,
            source_state="observed",
            available_from_session=date(2026, 1, 2),
            effective_from=date(2026, 1, 1),
            effective_through=None,
            source_public_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    account_clock = AccountValuationClock(
        completed_session=clock.decision_session,
        as_of_cutoff=clock.as_of_cutoff_at,
        next_execution_session=clock.next_execution_session,
        valuation_time=clock.account_valuation_at,
    )
    return BrokerAccountSnapshot(
        paper_account_environment_id=deployment.paper_account_environment_id,
        decision_slot_id=decision.decision_slot_id,
        decision_id=decision.decision_id,
        clock=account_clock,
        equity=10000,
        cash=9000,
        peak_equity=11000,
        balance_observed_at=clock.account_valuation_at,
        peak_observed_at=clock.as_of_cutoff_at,
        positions=tuple(
            (
                BrokerPositionFact(
                    symbol=symbol,
                    quantity=quantity,
                    mark_price=100,
                    mark_observed_at=clock.account_valuation_at,
                    sector=classification("Technology"),
                    industry=classification("Software"),
                )
                for symbol, quantity in (("AAA", 4), ("CCC", 6))
            )
        ),
        open_orders=(
            BrokerOrderFact("broker:AAA", "client:AAA", "AAA", "buy", "partially_filled", 10, 4),
            BrokerOrderFact("broker:BBB", "client:BBB", "BBB", "buy", "submitted", 5, 0),
        ),
        source_namespace="synthetic-recorded-account",
        account_snapshot_id="combined-account-v1",
    )


def assert_pending_reconciliation(account, portfolio, actions, holdings):
    projection = policy_execution_state_to_projection(
        account=account, portfolio_snapshot=portfolio, action_projections=actions, holding_episodes=holdings
    )
    result = reconcile_account_snapshot(
        account=account,
        projection=projection,
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )
    assert result.ready, result.findings
    assert result.reserved_buy_cash == 1600
    assert result.reserved_buy_risk == 110
    assert result.total_committed_risk == 210
    assert result.pending_entry_count == 2
    assert result.settled_cash == 9000
    assert result.available_cash == 7400
    assert result.gross_exposure == 1000
    assert result.open_position_risk == 100
    assert result.equity == result.settled_cash + result.gross_exposure
    assert result.portfolio_features.drawdown_fraction == pytest.approx(1 / 11)
    assert result.sector_exposures == (("Technology", 0.1),)
    assert result.industry_exposures == (("Software", 0.1),)
    return result


def record_action(store, action):
    store.record_decision(
        action.decision,
        policy_payload={"role": action.role.value},
        guard_payload={"outcome": "allow_synthetic_fixture"},
        effective_action_payload={"logical_action_id": action.logical_action_id},
    )
    store.record_action_intent(action, expected_version=None)


def record_fill(store, action, quantity, *, holding=None, event_id=None):
    projection = store.load_action_projection(action.logical_action_id)
    return store.record_cumulative_fill(
        action.logical_action_id,
        1,
        provider_id="synthetic-recorded-fill",
        fill_event_id=event_id or f"fill:{action.logical_action_id}",
        cumulative_quantity=Decimal(quantity),
        cumulative_notional=Decimal(quantity) * Decimal("100"),
        cumulative_fees=Decimal("0"),
        payload_sha256=None,
        observed_at=action.decision.clock.account_valuation_at,
        expected_action_version=projection.state_version,
        expected_holding_version=None if holding is None else holding.state_version,
    )


def protect_holding(store, action):
    holding = store.load_holding_episode_for_action(action.logical_action_id)
    observed_at = action.decision.clock.account_valuation_at
    stop = store.propose_stop_update(
        holding.holding_episode_id,
        decision=action.decision,
        stop_price=Decimal("90"),
        expected_holding_version=holding.state_version,
        observed_at=observed_at,
    )
    proposed = store.load_holding_episode(holding.holding_episode_id)
    return store.confirm_protective_stop(
        stop,
        stop_price=Decimal("90"),
        client_order_id=f"stop-client:{holding.security_id}",
        broker_order_id=f"stop-broker:{holding.security_id}",
        observed_at=observed_at,
        expected_holding_version=proposed.state_version,
    )


def seed_pending_chain(tmp_path):
    features = build_feature_fixture(tmp_path)
    deployment, clock = build_chain_identity(features)
    path = tmp_path / "combined-policy.sqlite3"
    store = PolicyExecutionStateStore(path, store_identity=deployment.store_identity)
    store.migrate()
    store.register_deployment_identity(
        deployment, lifecycle="prepared", handler_identity="synthetic-handler", guard_id="fixed-offline-guard"
    )
    store.set_active_generation(
        deployment.paper_account_environment_id,
        expected_generation_id=None,
        new_generation_id=deployment.deployment_generation_id,
        readiness_evidence_ref="synthetic-fixture-only",
        outgoing_entries_reconciled=True,
    )
    entry_c = build_entry(features, deployment, clock, "CCC", "4", "100")
    record_action(store, entry_c)
    record_fill(store, entry_c, "4")
    holding_c = store.load_holding_episode_for_action(entry_c.logical_action_id)
    addition_decision = DecisionIdentity.build(
        deployment=deployment,
        clock=clock,
        snapshot_sha256=features.recorded_input_manifest_sha256,
        category=DecisionCategory.ADDITION,
        subject_type=DecisionSubjectType.HOLDING,
        subject_id=holding_c.holding_episode_id,
    )
    addition = build_action_intent(
        decision=addition_decision,
        security_id="fixture:CCC",
        broker_symbol="CCC",
        holding_episode_id=holding_c.holding_episode_id,
        role=ActionRole.ADDITION,
        side=OrderSide.BUY,
        requested_quantity=Decimal("2"),
        status=ActionStatus.SUBMITTED,
        reservation_price=Decimal("100"),
        reservation_price_basis="recorded_limit",
        reservation_stop_price=Decimal("90"),
        risk_per_unit=Decimal("10"),
        risk_basis="recorded_limit_minus_stop",
    )
    addition = bind_attempt_order_refs(
        addition, attempt_number=1, client_order_id="client:add:CCC", broker_order_id="broker:add:CCC"
    )
    record_action(store, addition)
    holding_c = store.load_holding_episode(holding_c.holding_episode_id)
    record_fill(store, addition, "2", holding=holding_c)
    protect_holding(store, entry_c)
    entry_a = build_entry(features, deployment, clock, "AAA", "10", "100")
    entry_b = build_entry(features, deployment, clock, "BBB", "5", "200")
    record_action(store, entry_a)
    record_action(store, entry_b)
    record_fill(store, entry_a, "4", event_id="fill:A:4")
    protect_holding(store, entry_a)
    account = build_account(deployment, clock, entry_a.decision)
    protected_holdings = tuple(
        (store.load_holding_episode_for_action(action.logical_action_id) for action in (entry_a, entry_c))
    )
    account = replace(
        account,
        open_orders=account.open_orders
        + tuple(
            (
                BrokerOrderFact(
                    holding.confirmed_stop_broker_order_id,
                    holding.confirmed_stop_client_order_id,
                    holding.broker_symbol,
                    "sell",
                    "submitted",
                    float(holding.remaining_quantity),
                    0,
                    purpose="protective_stop",
                    holding_episode_id=holding.holding_episode_id,
                    stop_price=90,
                )
                for holding in protected_holdings
            )
        ),
    )
    portfolio = PortfolioStateSnapshot(
        deployment_identity=deployment,
        clock=clock,
        source_namespace=account.source_namespace,
        account_snapshot_id=account.account_snapshot_id,
        equity=Decimal("10000"),
        cash=Decimal("9000"),
        gross_exposure=Decimal("1000"),
        open_risk=Decimal("100"),
        portfolio_peak_equity=Decimal("11000"),
        last_accepted_session=clock.decision_session,
    )
    store.record_portfolio_snapshot(portfolio)
    return (features, path, deployment, account, portfolio, entry_a, entry_b)


def read_chain(store, deployment, portfolio):
    return store.load_policy_execution_snapshot(
        deployment_generation_id=deployment.deployment_generation_id,
        portfolio_snapshot_id=portfolio.portfolio_snapshot_id,
    )


def test_feature_identity_pending_cash_risk_restart_and_resolution(tmp_path):
    features, path, deployment, account, portfolio, action_a, action_b = seed_pending_chain(tmp_path)
    store = PolicyExecutionStateStore(path, store_identity=deployment.store_identity)
    before = read_chain(store, deployment, portfolio)
    result = assert_pending_reconciliation(
        account, before.portfolio_snapshot, before.action_projections, before.holding_episodes
    )
    assert result.as_of_cutoff == features.decision_clock.as_of_cutoff
    assert result.valuation_time > result.as_of_cutoff
    for action in before.action_projections:
        assert action.snapshot_sha256 == features.recorded_input_manifest_sha256
        assert action.deployment_identity.feature_calculator_id == features.feature_calculator_identity
    holding_c = next((item for item in before.holding_episodes if item.broker_symbol == "CCC"))
    assert holding_c.initial_filled_quantity == Decimal("4")
    assert holding_c.remaining_quantity == Decimal("6")
    assert holding_c.addition_count == 1
    assert holding_c.completed_additions_quantity == Decimal("2")
    restarted = PolicyExecutionStateStore(path, store_identity=deployment.store_identity)
    restored = read_chain(restarted, deployment, portfolio)
    assert restored == before
    current_a = restarted.load_action_projection(action_a.logical_action_id)
    holding_a = restarted.load_holding_episode_for_action(action_a.logical_action_id)
    restarted.record_action_intent(action_a, expected_version=current_a.state_version)
    replay = record_fill(restarted, action_a, "4", holding=holding_a, event_id="fill:A:4")
    assert replay == current_a
    assert read_chain(restarted, deployment, portfolio) == before
    assert_pending_reconciliation(account, portfolio, restored.action_projections, restored.holding_episodes)
    cancelling = restarted.request_order_cancel(
        action_a.logical_action_id,
        1,
        expected_action_version=current_a.state_version,
        observed_at=account.clock.valuation_time,
    )
    with pytest.raises(ValueError, match="terminal"):
        restarted.record_explicit_action_resolution(
            action_a.logical_action_id,
            resolution_reason="cancellation not yet confirmed",
            expected_action_version=cancelling.state_version,
            expected_holding_version=holding_a.state_version,
            observed_at=account.clock.valuation_time,
        )
    terminal = restarted.confirm_order_terminal(
        action_a.logical_action_id,
        1,
        terminal_status=ActionAttemptStatus.CANCELLED,
        expected_action_version=cancelling.state_version,
        expected_holding_version=holding_a.state_version,
        observed_at=account.clock.valuation_time,
    )
    holding_a = restarted.load_holding_episode(holding_a.holding_episode_id)
    reason = "recorded cancellation terminal; account confirms exactly four acquired shares"
    resolved = restarted.record_explicit_action_resolution(
        action_a.logical_action_id,
        resolution_reason=reason,
        expected_action_version=terminal.state_version,
        expected_holding_version=holding_a.state_version,
        observed_at=account.clock.valuation_time,
    )
    assert resolved.resolution_reason == reason
    account = replace(
        account,
        account_snapshot_id="combined-account-after-resolution",
        open_orders=(replace(account.open_orders[0], status="cancelled"), *account.open_orders[1:]),
    )
    portfolio = replace(portfolio, account_snapshot_id=account.account_snapshot_id)
    restarted.record_portfolio_snapshot(portfolio)
    after = read_chain(restarted, deployment, portfolio)
    projection = policy_execution_state_to_projection(
        account=account,
        portfolio_snapshot=after.portfolio_snapshot,
        action_projections=after.action_projections,
        holding_episodes=after.holding_episodes,
    )
    reconciled = reconcile_account_snapshot(
        account=account,
        projection=projection,
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )
    assert reconciled.ready, reconciled.findings
    assert reconciled.reserved_buy_cash == 1000
    assert reconciled.reserved_buy_risk == 50
    assert reconciled.pending_entry_count == 1
    assert reconciled.total_committed_risk == 150
    assert restarted.load_action_projection(action_b.logical_action_id).confirmed_quantity == 0


def test_canonical_store_chain_rejects_unknown_and_conflicting_account_facts(tmp_path):
    _, path, deployment, account, portfolio, action_a, _ = seed_pending_chain(tmp_path)
    store = PolicyExecutionStateStore(path, store_identity=deployment.store_identity)
    snapshot = read_chain(store, deployment, portfolio)
    variants = (
        (
            "missing classification",
            replace(account, positions=(replace(account.positions[0], sector=None), *account.positions[1:])),
            snapshot.action_projections,
            snapshot.holding_episodes,
        ),
        (
            "missing stop",
            account,
            snapshot.action_projections,
            tuple(
                (
                    replace(holding, confirmed_protective_stop_price=None)
                    if holding.broker_symbol == "AAA"
                    else holding
                    for holding in snapshot.holding_episodes
                )
            ),
        ),
        (
            "missing pending risk",
            account,
            tuple(
                (
                    replace(action, risk_per_unit=None, risk_basis=None, residual_committed_risk=None)
                    if action.logical_action_id == action_a.logical_action_id
                    else action
                    for action in snapshot.action_projections
                )
            ),
            snapshot.holding_episodes,
        ),
        (
            "conflicting order reference",
            replace(
                account,
                open_orders=(
                    replace(account.open_orders[0], client_order_id="unrecognized-client"),
                    *account.open_orders[1:],
                ),
            ),
            snapshot.action_projections,
            snapshot.holding_episodes,
        ),
        (
            "stale mark",
            replace(
                account,
                positions=(
                    replace(account.positions[0], mark_observed_at=account.clock.valuation_time - timedelta(hours=1)),
                    *account.positions[1:],
                ),
            ),
            snapshot.action_projections,
            snapshot.holding_episodes,
        ),
    )
    for label, broker, actions, holdings in variants:
        projection = policy_execution_state_to_projection(
            account=broker, portfolio_snapshot=portfolio, action_projections=actions, holding_episodes=holdings
        )
        result = reconcile_account_snapshot(
            account=broker,
            projection=projection,
            maximum_balance_age=timedelta(minutes=15),
            maximum_mark_age=timedelta(minutes=15),
        )
        assert not result.ready, label
        assert result.findings, label
        assert result.portfolio_features is None, label
    assert read_chain(store, deployment, portfolio) == snapshot


def test_new_generation_account_keeps_old_holding_and_pending_exit_ancestry(tmp_path):
    _, path, deployment_a, account, portfolio, entry_a, entry_b = seed_pending_chain(tmp_path)
    store = PolicyExecutionStateStore(path, store_identity=deployment_a.store_identity)
    snapshot = read_chain(store, deployment_a, portfolio)
    for entry in (entry_a, entry_b):
        action = store.load_action_projection(entry.logical_action_id)
        holding = (
            store.load_holding_episode(action.holding_episode_id) if action.holding_episode_id is not None else None
        )
        cancelling = store.request_order_cancel(
            entry.logical_action_id,
            1,
            expected_action_version=action.state_version,
            observed_at=account.clock.valuation_time,
        )
        terminal = store.confirm_order_terminal(
            entry.logical_action_id,
            1,
            terminal_status=ActionAttemptStatus.CANCELLED,
            expected_action_version=cancelling.state_version,
            expected_holding_version=None if holding is None else holding.state_version,
            observed_at=account.clock.valuation_time,
        )
        if holding is not None:
            holding = store.load_holding_episode(holding.holding_episode_id)
        store.record_explicit_action_resolution(
            entry.logical_action_id,
            resolution_reason="recorded terminal cancellation and account fill reconciliation",
            expected_action_version=terminal.state_version,
            expected_holding_version=None if holding is None else holding.state_version,
            observed_at=account.clock.valuation_time,
        )
    holding_c = next((item for item in snapshot.holding_episodes if item.broker_symbol == "CCC"))
    exit_decision = DecisionIdentity.build(
        deployment=deployment_a,
        clock=portfolio.clock,
        snapshot_sha256=entry_a.decision.snapshot_sha256,
        category=DecisionCategory.EXIT,
        subject_type=DecisionSubjectType.HOLDING,
        subject_id=holding_c.holding_episode_id,
    )
    exit_action = build_action_intent(
        decision=exit_decision,
        security_id=holding_c.security_id,
        broker_symbol="CCC",
        holding_episode_id=holding_c.holding_episode_id,
        role=ActionRole.SCALE_OUT,
        side=OrderSide.SELL,
        requested_quantity=Decimal("3"),
        status=ActionStatus.SUBMITTED,
        exit_tier=1,
        snapshot_original_quantity=Decimal("6"),
        fraction_of_original_quantity=Decimal("0.5"),
        rounding_rule_id="whole_share_floor_v1",
    )
    exit_action = bind_attempt_order_refs(
        exit_action, attempt_number=1, client_order_id="client:exit:CCC", broker_order_id="broker:exit:CCC"
    )
    record_action(store, exit_action)
    deployment_b = replace(deployment_a, policy_artifact_id="fixed-policy:second-generation")
    store.register_deployment_identity(
        deployment_b, lifecycle="prepared", handler_identity="synthetic-handler", guard_id="fixed-offline-guard"
    )
    store.set_active_generation(
        deployment_a.paper_account_environment_id,
        expected_generation_id=deployment_a.deployment_generation_id,
        new_generation_id=deployment_b.deployment_generation_id,
        readiness_evidence_ref="synthetic-switch-with-pinned-holdings",
        outgoing_entries_reconciled=True,
    )
    new_session = portfolio.clock.next_execution_session
    next_opportunity = new_session + timedelta(days=1)
    new_clock = replace(
        portfolio.clock,
        decision_session=new_session,
        as_of_cutoff_at=datetime.combine(new_session, datetime.min.time(), tzinfo=timezone.utc).replace(hour=20),
        next_execution_session=next_opportunity,
        account_valuation_session=next_opportunity,
        account_valuation_at=datetime.combine(next_opportunity, datetime.min.time(), tzinfo=timezone.utc).replace(
            hour=13, minute=31
        ),
    )
    account_decision = DecisionIdentity.build(
        deployment=deployment_b,
        clock=new_clock,
        snapshot_sha256="b" * 64,
        category=DecisionCategory.CAPACITY,
        subject_type=DecisionSubjectType.PORTFOLIO,
        subject_id="synthetic-account-capacity",
    )
    account = replace(
        account,
        account_snapshot_id="mixed-generation-account",
        decision_slot_id=account_decision.decision_slot_id,
        decision_id=account_decision.decision_id,
        clock=AccountValuationClock(
            completed_session=new_clock.decision_session,
            as_of_cutoff=new_clock.as_of_cutoff_at,
            next_execution_session=new_clock.next_execution_session,
            valuation_time=new_clock.account_valuation_at,
        ),
        balance_observed_at=new_clock.account_valuation_at,
        positions=tuple(
            (replace(position, mark_observed_at=new_clock.account_valuation_at) for position in account.positions)
        ),
        open_orders=(
            *account.open_orders[2:],
            BrokerOrderFact("broker:exit:CCC", "client:exit:CCC", "CCC", "sell", "submitted", 3, 0),
        ),
    )
    portfolio_b = replace(
        portfolio, deployment_identity=deployment_b, clock=new_clock, account_snapshot_id=account.account_snapshot_id
    )
    store.record_portfolio_snapshot(portfolio_b)
    restarted = PolicyExecutionStateStore(path, store_identity=deployment_a.store_identity)
    mixed = read_chain(restarted, deployment_b, portfolio_b)
    assert len(mixed.holding_episodes) == 2
    assert all(
        (
            holding.deployment_generation_id == deployment_a.deployment_generation_id
            for holding in mixed.holding_episodes
        )
    )
    persisted_exit = next(
        (action for action in mixed.action_projections if action.logical_action_id == exit_action.logical_action_id)
    )
    assert persisted_exit.decision_id == exit_decision.decision_id
    assert persisted_exit.decision_session < new_clock.decision_session
    assert persisted_exit.snapshot_original_quantity == Decimal("6")
    projection = policy_execution_state_to_projection(
        account=account,
        portfolio_snapshot=mixed.portfolio_snapshot,
        action_projections=mixed.action_projections,
        holding_episodes=mixed.holding_episodes,
    )
    result = reconcile_account_snapshot(
        account=account,
        projection=projection,
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )
    assert result.ready, result.findings
    assert result.pending_sell_count == 1
    assert result.pending_entry_count == 0
    assert result.reserved_buy_cash == 0
    assert result.gross_exposure == 1000
    assert result.open_position_risk == 100
