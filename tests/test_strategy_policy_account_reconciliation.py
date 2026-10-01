from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.policy_execution_state import (
    ActionAttemptStatus,
    ActionRole,
    ActionStatus,
    DecisionCategory,
    DecisionClock,
    DecisionIdentity,
    DecisionSubjectType,
    HoldingEpisode,
    OrderSide,
    PolicyDeploymentIdentity,
    PortfolioStateSnapshot,
    apply_cumulative_fill,
    apply_action_fill_to_holding,
    add_attempt_order_aliases,
    bind_attempt_order_refs,
    build_action_intent,
    confirm_protective_stop,
    confirm_attempt_terminal,
    create_single_remainder_attempt,
    project_action_state,
    propose_stop_update,
    request_attempt_cancel,
    resolve_action,
)
from core.policy_execution_store import PolicyExecutionStateStore
from core.strategy_policy.account_reconciliation import (
    AccountValuationClock,
    BrokerAccountSnapshot,
    BrokerOrderFact,
    BrokerPositionFact,
    ClassificationFact,
    HoldingProjection,
    OrderReference,
    PolicyActionProjection,
    PolicyStateProjection,
    SecuritySymbolMapping,
    policy_execution_state_to_projection,
    reconcile_account_snapshot,
)


UTC = timezone.utc
VALUATION_TIME = datetime(2026, 9, 30, 20, 5, tzinfo=UTC)


def _clock(**changes: object) -> AccountValuationClock:
    values: dict[str, object] = {
        "completed_session": date(2026, 9, 30),
        "as_of_cutoff": VALUATION_TIME,
        "next_execution_session": date(2026, 10, 1),
        "valuation_time": VALUATION_TIME,
    }
    values.update(changes)
    return AccountValuationClock(**values)  # type: ignore[arg-type]


def _classification(*, code: str = "Technology") -> ClassificationFact:
    return ClassificationFact(
        code=code,
        source_state="observed",
        available_from_session=date(2026, 9, 1),
        effective_from=date(2026, 1, 1),
        effective_through=None,
        source_public_at=datetime(2026, 8, 31, 12, 0, tzinfo=UTC),
    )


def _projection(
    *,
    clock: AccountValuationClock | None = None,
    holdings: tuple[HoldingProjection, ...] | None = None,
    actions: tuple[PolicyActionProjection, ...] | None = None,
    mappings: tuple[SecuritySymbolMapping, ...] | None = None,
) -> PolicyStateProjection:
    if holdings is None:
        holdings = (
            HoldingProjection(
                holding_episode_id="holding-1",
                deployment_generation_id="generation-1",
                security_id="sec:xyz",
                remaining_quantity=24,
                stop_price=90,
                stop_observed_at=datetime(2026, 9, 30, 19, 55, tzinfo=UTC),
                protective_stop_order_references=(),
            ),
        )
    if actions is None:
        actions = (
            PolicyActionProjection(
                logical_action_id="action-1",
                deployment_generation_id="generation-1",
                holding_episode_id=None,
                security_id="sec:xyz",
                role="entry",
                side="buy",
                status="partially_filled",
                requested_quantity=10,
                confirmed_quantity=4,
                residual_quantity=6,
                reservation_amount=600,
                reservation_price=100,
                reservation_price_basis="limit_price",
                reservation_stop_price=90,
                client_order_refs=("client-1",),
                broker_order_refs=("broker-1",),
                resolution_reason=None,
            ),
        )
    return PolicyStateProjection(
        snapshot_id="snapshot-1",
        decision_clock_id="clock-1",
        decision_slot_id="slot-1",
        decision_id="decision-1",
        decision_session=(clock or _clock()).completed_session,
        input_cutoff_at=(clock or _clock()).as_of_cutoff,
        next_execution_session=(clock or _clock()).next_execution_session,
        active_deployment_generation_id="generation-1",
        paper_account_environment_id="synthetic-account",
        store_identity="synthetic-store",
        holdings=holdings,
        pending_actions=actions,
        security_symbol_mappings=mappings or (
            SecuritySymbolMapping(
                security_id="sec:xyz",
                broker_symbol="XYZ",
                mapping_contract_id="synthetic-identity-contract",
                effective_from=date(2026, 1, 1),
                effective_through=None,
                validated_at=VALUATION_TIME,
            ),
        ),
    )


def _account(
    *,
    clock: AccountValuationClock | None = None,
    cash: float | None = 7_600,
    equity: float | None = 10_000,
    peak_equity: float | None = 12_500,
    positions: tuple[BrokerPositionFact, ...] | None = None,
    open_orders: tuple[BrokerOrderFact, ...] | None = None,
) -> BrokerAccountSnapshot:
    if positions is None:
        positions = (
            BrokerPositionFact(
                symbol="XYZ",
                quantity=24,
                mark_price=100,
                mark_observed_at=VALUATION_TIME,
                sector=_classification(),
                industry=_classification(code="Software"),
            ),
        )
    if open_orders is None:
        open_orders = (
            BrokerOrderFact(
                broker_order_id="broker-1",
                client_order_id="client-1",
                symbol="XYZ",
                side="buy",
                status="partially_filled",
                requested_quantity=10,
                cumulative_filled_quantity=4,
            ),
        )
    return BrokerAccountSnapshot(
        paper_account_environment_id="synthetic-account",
        decision_slot_id="slot-1",
        decision_id="decision-1",
        clock=clock or _clock(),
        equity=equity,
        cash=cash,
        peak_equity=peak_equity,
        balance_observed_at=VALUATION_TIME,
        peak_observed_at=datetime(2026, 9, 29, 20, 0, tzinfo=UTC),
        positions=positions,
        open_orders=open_orders,
        source_namespace="synthetic-broker",
        account_snapshot_id="account-snapshot-1",
    )


def test_policy_execution_state_conversion_preserves_decimal_residual_attempts_and_identity() -> None:
    feature_cutoff = datetime(2026, 9, 30, 20, 0, tzinfo=UTC)
    valuation_time = datetime(2026, 10, 1, 13, 30, tzinfo=UTC)
    policy_clock = DecisionClock(
        exchange_id="XNYS",
        decision_session=date(2026, 9, 30),
        as_of_cutoff_at=feature_cutoff,
        next_execution_session=date(2026, 10, 1),
        account_valuation_session=date(2026, 10, 1),
        account_valuation_at=valuation_time,
    )
    account_clock = AccountValuationClock(
        completed_session=date(2026, 9, 30),
        as_of_cutoff=feature_cutoff,
        next_execution_session=date(2026, 10, 1),
        valuation_time=valuation_time,
    )
    deployment = PolicyDeploymentIdentity(
        policy_artifact_id="policy:sha256:synthetic",
        capability_manifest_id="manifest:synthetic",
        policy_interface_version="3",
        feature_contract_id="features-v3",
        feature_calculator_id="calculator-v3",
        source_revision="ab385d792e19ff6db39d87f1123f47f660fc1e1d",
        runtime_identity="synthetic-runtime",
        execution_profile_id="synthetic-paper-profile",
        paper_account_environment_id="synthetic-paper-account",
        store_identity="synthetic-policy-store",
    )
    decision = DecisionIdentity.build(
        deployment=deployment,
        clock=policy_clock,
        snapshot_sha256="a" * 64,
        category=DecisionCategory.ENTRY,
        subject_type=DecisionSubjectType.SECURITY,
        subject_id="sec:xyz",
    )
    intent = build_action_intent(
        decision=decision,
        security_id="sec:xyz",
        broker_symbol="XYZ",
        role=ActionRole.ENTRY,
        side=OrderSide.BUY,
        requested_quantity=Decimal("10"),
        status=ActionStatus.SUBMITTED,
        reservation_price=Decimal("100"),
        reservation_price_basis="limit_price",
        reservation_stop_price=Decimal("90"),
        risk_per_unit=Decimal("12"),
        risk_basis="strategy_risk",
    )
    intent = bind_attempt_order_refs(
        intent,
        attempt_number=1,
        client_order_id="client-1",
        broker_order_id="broker-1",
    )
    intent = add_attempt_order_aliases(
        intent,
        attempt_number=1,
        client_order_id="client-alias-1",
        broker_order_id="broker-alias-1",
    )
    intent = apply_cumulative_fill(intent, Decimal("4"))
    action_projection = replace(project_action_state(intent), state_version=7)

    holding = HoldingEpisode.open(
        deployment_generation_id=deployment.deployment_generation_id,
        security_id="sec:xyz",
        symbol="XYZ",
        broker_symbol="XYZ",
        opening_action_id="opening-action-1",
        initial_filled_quantity=Decimal("24"),
        entry_price=Decimal("100"),
    )
    holding, stop_intent = propose_stop_update(
        holding,
        decision=decision,
        stop_price=Decimal("90"),
    )
    holding = confirm_protective_stop(
        holding,
        intent=stop_intent,
        stop_price=Decimal("90"),
        client_order_id="protective-client-1",
        broker_order_id="protective-broker-1",
        observed_at=valuation_time,
    )
    holding = replace(holding, state_version=4)
    portfolio_snapshot = PortfolioStateSnapshot(
        deployment_identity=deployment,
        clock=policy_clock,
        source_namespace="synthetic-broker",
        account_snapshot_id="account-snapshot-1",
        equity=Decimal("10000"),
        cash=Decimal("7600"),
        gross_exposure=Decimal("2400"),
        open_risk=Decimal("240"),
        portfolio_peak_equity=Decimal("12500"),
        last_accepted_session=date(2026, 9, 29),
    )
    account = replace(
        _account(clock=account_clock),
        paper_account_environment_id=deployment.paper_account_environment_id,
        decision_slot_id=action_projection.decision_slot_id,
        decision_id=action_projection.decision_id,
        balance_observed_at=valuation_time,
        positions=(replace(_account().positions[0], mark_observed_at=valuation_time),),  # type: ignore[index]
        open_orders=(
            BrokerOrderFact("broker-alias-1", "client-alias-1", "XYZ", "buy", "partially_filled", 10, 4),
            BrokerOrderFact(
                "protective-broker-1",
                "protective-client-1",
                "XYZ",
                "sell",
                "submitted",
                24,
                0,
                purpose="protective_stop",
                holding_episode_id=holding.holding_episode_id,
                stop_price=90,
            ),
        ),
    )

    projection = policy_execution_state_to_projection(
        account=account,
        portfolio_snapshot=portfolio_snapshot,
        action_projections=(action_projection,),
        holding_episodes=(holding,),
    )
    result = reconcile_account_snapshot(
        account=account,
        projection=projection,
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )

    assert projection.snapshot_id == portfolio_snapshot.portfolio_snapshot_id
    assert projection.store_identity == deployment.store_identity
    assert projection.pending_actions[0].client_order_refs == ("client-1", "client-alias-1")  # type: ignore[index]
    assert projection.pending_actions[0].broker_order_refs == ("broker-1", "broker-alias-1")  # type: ignore[index]
    assert projection.pending_actions[0].order_attempts[0].status == "partially_filled"  # type: ignore[index]
    assert projection.pending_actions[0].order_attempts[0].terminal_status is None  # type: ignore[index]
    assert projection.pending_actions[0].order_attempts[0].client_order_aliases == ("client-alias-1",)  # type: ignore[index]
    assert projection.pending_actions[0].order_attempts[0].broker_order_aliases == ("broker-alias-1",)  # type: ignore[index]
    assert projection.pending_actions[0].source_decision_session == date(2026, 9, 30)  # type: ignore[index]
    assert projection.pending_actions[0].source_as_of_cutoff_at == feature_cutoff  # type: ignore[index]
    assert projection.pending_actions[0].source_next_execution_session == date(2026, 10, 1)  # type: ignore[index]
    assert projection.pending_actions[0].source_account_valuation_session == date(2026, 10, 1)  # type: ignore[index]
    assert projection.pending_actions[0].source_account_valuation_at == valuation_time  # type: ignore[index]
    assert projection.pending_actions[0].source_state_version == 7  # type: ignore[index]
    assert projection.holdings[0].source_state_version == 4  # type: ignore[index]
    assert projection.pending_actions[0].source_decision_slot_id == action_projection.decision_slot_id  # type: ignore[index]
    assert projection.pending_actions[0].source_decision_id == action_projection.decision_id  # type: ignore[index]
    assert projection.security_symbol_mappings[0].broker_symbol == "XYZ"  # type: ignore[index]
    assert result.ready is True, result.findings
    assert result.as_of_cutoff == feature_cutoff
    assert result.valuation_time == valuation_time
    assert result.reserved_buy_cash == 600
    assert result.reserved_buy_risk == 72
    assert result.total_committed_risk == 312

    current_cutoff = datetime(2026, 10, 1, 20, 0, tzinfo=UTC)
    current_valuation = datetime(2026, 10, 2, 13, 30, tzinfo=UTC)
    current_policy_clock = DecisionClock(
        exchange_id="XNYS",
        decision_session=date(2026, 10, 1),
        as_of_cutoff_at=current_cutoff,
        next_execution_session=date(2026, 10, 2),
        account_valuation_session=date(2026, 10, 2),
        account_valuation_at=current_valuation,
    )
    current_account_clock = AccountValuationClock(
        completed_session=date(2026, 10, 1),
        as_of_cutoff=current_cutoff,
        next_execution_session=date(2026, 10, 2),
        valuation_time=current_valuation,
    )
    current_deployment = replace(
        deployment,
        source_revision="current-policy-revision",
        runtime_identity="current-paper-runtime",
    )
    current_decision = DecisionIdentity.build(
        deployment=current_deployment,
        clock=current_policy_clock,
        snapshot_sha256="c" * 64,
        category=DecisionCategory.ENTRY,
        subject_type=DecisionSubjectType.SECURITY,
        subject_id="sec:xyz",
    )
    current_portfolio_snapshot = replace(
        portfolio_snapshot,
        deployment_identity=current_deployment,
        clock=current_policy_clock,
        account_snapshot_id="account-snapshot-current",
        last_accepted_session=date(2026, 10, 1),
    )
    current_holding = replace(holding, valuation_at=current_valuation)
    current_account = replace(
        account,
        decision_slot_id=current_decision.decision_slot_id,
        decision_id=current_decision.decision_id,
        clock=current_account_clock,
        balance_observed_at=current_valuation,
        peak_observed_at=current_valuation,
        positions=(replace(account.positions[0], mark_observed_at=current_valuation),),  # type: ignore[index]
        account_snapshot_id="account-snapshot-current",
    )
    carried_projection = policy_execution_state_to_projection(
        account=current_account,
        portfolio_snapshot=current_portfolio_snapshot,
        action_projections=(action_projection,),
        holding_episodes=(current_holding,),
    )
    carried_action = carried_projection.pending_actions[0]  # type: ignore[index]
    assert carried_action.source_decision_id == action_projection.decision_id
    assert carried_action.source_clock_id == action_projection.clock_id
    assert carried_action.source_clock_id != carried_projection.decision_clock_id
    assert carried_action.source_next_execution_session == date(2026, 10, 1)
    carried_result = reconcile_account_snapshot(
        account=current_account,
        projection=carried_projection,
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )
    assert carried_result.ready is True, carried_result.findings
    assert carried_result.reserved_buy_cash == 600
    assert carried_result.reserved_buy_risk == 72

    for malformed_action in (
        replace(
            action_projection,
            decision_session=date(2026, 10, 2),
            as_of_cutoff_at=datetime(2026, 10, 2, 20, 0, tzinfo=UTC),
            next_execution_session=date(2026, 10, 5),
            account_valuation_session=date(2026, 10, 5),
            account_valuation_at=datetime(2026, 10, 5, 13, 30, tzinfo=UTC),
        ),
        replace(action_projection, account_valuation_session=date(2026, 10, 2)),
    ):
        malformed_projection = policy_execution_state_to_projection(
            account=current_account,
            portfolio_snapshot=current_portfolio_snapshot,
            action_projections=(malformed_action,),
            holding_episodes=(current_holding,),
        )
        malformed_result = reconcile_account_snapshot(
            account=current_account,
            projection=malformed_projection,
            maximum_balance_age=timedelta(minutes=15),
            maximum_mark_age=timedelta(minutes=15),
        )
        assert malformed_result.ready is False
        assert any(
            finding.path == f"pending_actions.{action_projection.logical_action_id}.source_clock"
            and finding.state == "invalid"
            for finding in malformed_result.findings
        )

    missed_account = replace(current_account, open_orders=(current_account.open_orders[1],))  # type: ignore[index]
    for sequence, attempt_status in ((2, "intended"), (3, "remainder_ready")):
        missed_decision = DecisionIdentity.build(
            deployment=deployment,
            clock=policy_clock,
            snapshot_sha256=hex(sequence)[2:] * 64,
            category=DecisionCategory.ENTRY,
            subject_type=DecisionSubjectType.SECURITY,
            subject_id="sec:xyz",
            sequence=sequence,
        )
        missed_intent = build_action_intent(
            decision=missed_decision,
            security_id="sec:xyz",
            broker_symbol="XYZ",
            role=ActionRole.ENTRY,
            side=OrderSide.BUY,
            requested_quantity=Decimal("10"),
            status=ActionStatus.INTENDED if attempt_status == "intended" else ActionStatus.SUBMITTED,
            reservation_price=Decimal("100"),
            reservation_price_basis="limit_price",
            reservation_stop_price=Decimal("90"),
            risk_per_unit=Decimal("12"),
            risk_basis="strategy_risk",
        )
        if attempt_status == "remainder_ready":
            missed_intent = bind_attempt_order_refs(
                missed_intent,
                attempt_number=1,
                client_order_id=f"missed-client-{sequence}",
                broker_order_id=f"missed-broker-{sequence}",
            )
            missed_intent = request_attempt_cancel(missed_intent, 1)
            missed_intent = confirm_attempt_terminal(
                missed_intent,
                1,
                status=ActionAttemptStatus.CANCELLED,
            )
            assert missed_intent.status is ActionStatus.REMAINDER_READY
        missed_source = replace(project_action_state(missed_intent), state_version=sequence)
        missed_projection = policy_execution_state_to_projection(
            account=missed_account,
            portfolio_snapshot=current_portfolio_snapshot,
            action_projections=(missed_source,),
            holding_episodes=(current_holding,),
        )
        missed_result = reconcile_account_snapshot(
            account=missed_account,
            projection=missed_projection,
            maximum_balance_age=timedelta(minutes=15),
            maximum_mark_age=timedelta(minutes=15),
        )
        missed_action = missed_projection.pending_actions[0]  # type: ignore[index]
        assert missed_result.ready is False
        assert missed_result.reserved_buy_cash is None
        assert missed_result.reserved_buy_risk is None
        assert missed_action.source_next_execution_session == date(2026, 10, 1)
        assert missed_projection.next_execution_session == date(2026, 10, 2)
        assert any(
            finding.path == f"pending_actions.{missed_intent.logical_action_id}.execution_session"
            and finding.state == "stale_by_declared_rule"
            for finding in missed_result.findings
        )

    inconsistent_snapshot = replace(
        portfolio_snapshot,
        equity=Decimal("10100"),
        cash=Decimal("7500"),
        gross_exposure=Decimal("2600"),
        open_risk=Decimal("999"),
        portfolio_peak_equity=Decimal("13000"),
    )
    inconsistent_projection = policy_execution_state_to_projection(
        account=account,
        portfolio_snapshot=inconsistent_snapshot,
        action_projections=(action_projection,),
        holding_episodes=(holding,),
    )
    inconsistent_result = reconcile_account_snapshot(
        account=account,
        projection=inconsistent_projection,
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )
    assert inconsistent_result.ready is False
    assert {
        finding.path
        for finding in inconsistent_result.findings
        if finding.state == "conflicting"
    } >= {
        "portfolio_snapshot.equity",
        "portfolio_snapshot.cash",
        "portfolio_snapshot.gross_exposure",
        "portfolio_snapshot.open_risk",
        "portfolio_snapshot.peak_equity",
    }

    for source_name, finding_name in (
        ("equity", "equity"),
        ("cash", "cash"),
        ("gross_exposure", "gross_exposure"),
        ("open_risk", "open_risk"),
        ("portfolio_peak_equity", "peak_equity"),
    ):
        unknown_snapshot = replace(portfolio_snapshot, **{source_name: None})
        unknown_fact_projection = policy_execution_state_to_projection(
            account=account,
            portfolio_snapshot=unknown_snapshot,
            action_projections=(action_projection,),
            holding_episodes=(holding,),
        )
        unknown_fact_result = reconcile_account_snapshot(
            account=account,
            projection=unknown_fact_projection,
            maximum_balance_age=timedelta(minutes=15),
            maximum_mark_age=timedelta(minutes=15),
        )
        assert unknown_fact_result.ready is False
        assert any(
            finding.path == f"portfolio_snapshot.{finding_name}"
            and finding.state == "absent"
            for finding in unknown_fact_result.findings
        )

    unknown_risk = replace(intent, risk_per_unit=None, risk_basis=None)
    unknown_risk_projection = policy_execution_state_to_projection(
        account=account,
        portfolio_snapshot=portfolio_snapshot,
        action_projections=(project_action_state(unknown_risk),),
        holding_episodes=(holding,),
    )
    unknown_risk_result = reconcile_account_snapshot(
        account=account,
        projection=unknown_risk_projection,
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )
    assert unknown_risk_result.ready is False
    assert unknown_risk_result.reserved_buy_cash == 600
    assert unknown_risk_result.reserved_buy_risk is None

    for action_status, attempt_status in (
        (ActionStatus.RECONCILIATION_REQUIRED, ActionAttemptStatus.RECONCILIATION_REQUIRED),
        (ActionStatus.PARTIAL_INCOMPLETE, ActionAttemptStatus.CANCELLED),
    ):
        uncertain_intent = replace(
            intent,
            status=action_status,
            order_attempts=(replace(intent.order_attempts[0], status=attempt_status),),
        )
        uncertain_projection = policy_execution_state_to_projection(
            account=account,
            portfolio_snapshot=portfolio_snapshot,
            action_projections=(project_action_state(uncertain_intent),),
            holding_episodes=(holding,),
        )
        uncertain_result = reconcile_account_snapshot(
            account=account,
            projection=uncertain_projection,
            maximum_balance_age=timedelta(minutes=15),
            maximum_mark_age=timedelta(minutes=15),
        )
        assert uncertain_result.ready is False
        assert uncertain_result.reserved_buy_cash is None
        assert uncertain_result.pending_entry_count is None

    resolution_decision = DecisionIdentity.build(
        deployment=deployment,
        clock=policy_clock,
        snapshot_sha256="d" * 64,
        category=DecisionCategory.ENTRY,
        subject_type=DecisionSubjectType.SECURITY,
        subject_id="sec:xyz",
        sequence=2,
    )
    submitted_for_resolution = build_action_intent(
        decision=resolution_decision,
        security_id="sec:xyz",
        broker_symbol="XYZ",
        role=ActionRole.ENTRY,
        side=OrderSide.BUY,
        requested_quantity=Decimal("10"),
        status=ActionStatus.SUBMITTED,
        reservation_price=Decimal("100"),
        reservation_price_basis="limit_price",
        reservation_stop_price=Decimal("90"),
        risk_per_unit=Decimal("12"),
        risk_basis="strategy_risk",
    )
    submitted_for_resolution = bind_attempt_order_refs(
        submitted_for_resolution,
        attempt_number=1,
        client_order_id="resolved-client-1",
        broker_order_id="resolved-broker-1",
    )
    submitted_for_resolution = request_attempt_cancel(submitted_for_resolution, 1)
    submitted_for_resolution = confirm_attempt_terminal(
        submitted_for_resolution,
        1,
        status=ActionAttemptStatus.CANCELLED,
    )
    explicitly_resolved = resolve_action(
        submitted_for_resolution,
        status=ActionStatus.RESOLVED,
        resolution_reason="broker cancellation confirmed; no fill occurred",
    )
    resolved_account = replace(account, open_orders=(account.open_orders[1],))  # type: ignore[index]
    resolved_projection = policy_execution_state_to_projection(
        account=resolved_account,
        portfolio_snapshot=portfolio_snapshot,
        action_projections=(project_action_state(explicitly_resolved),),
        holding_episodes=(holding,),
    )
    resolved_result = reconcile_account_snapshot(
        account=resolved_account,
        projection=resolved_projection,
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )
    assert resolved_projection.pending_actions[0].resolution_reason == explicitly_resolved.resolution_reason  # type: ignore[index]
    assert resolved_projection.pending_actions[0].order_attempts[0].terminal_status == "cancelled"  # type: ignore[index]
    assert resolved_result.ready is True, resolved_result.findings
    assert resolved_result.reserved_buy_cash == 0
    assert resolved_result.reserved_buy_risk == 0
    assert resolved_result.pending_entry_count == 0

    for field, wrong_reference in (
        ("client_order_id", "wrong-client"),
        ("broker_order_id", "wrong-broker"),
    ):
        inconsistent_reference_account = replace(
            account,
            open_orders=(
                replace(account.open_orders[0], **{field: wrong_reference}),  # type: ignore[index]
                account.open_orders[1],  # type: ignore[index]
            ),
        )
        inconsistent_reference_result = reconcile_account_snapshot(
            account=inconsistent_reference_account,
            projection=projection,
            maximum_balance_age=timedelta(minutes=15),
            maximum_mark_age=timedelta(minutes=15),
        )
        assert inconsistent_reference_result.ready is False
        assert inconsistent_reference_result.reserved_buy_cash is None
        assert any(finding.state == "conflicting" for finding in inconsistent_reference_result.findings)


def test_policy_execution_state_conversion_keeps_order_references_per_attempt() -> None:
    decision_session = date(2026, 9, 30)
    cutoff = datetime(2026, 9, 30, 20, 0, tzinfo=UTC)
    valuation_time = datetime(2026, 10, 1, 13, 30, tzinfo=UTC)
    policy_clock = DecisionClock(
        exchange_id="XNYS",
        decision_session=decision_session,
        as_of_cutoff_at=cutoff,
        next_execution_session=date(2026, 10, 1),
        account_valuation_session=date(2026, 10, 1),
        account_valuation_at=valuation_time,
    )
    account_clock = AccountValuationClock(
        completed_session=decision_session,
        as_of_cutoff=cutoff,
        next_execution_session=date(2026, 10, 1),
        valuation_time=valuation_time,
    )
    deployment = PolicyDeploymentIdentity(
        policy_artifact_id="policy:sha256:synthetic",
        capability_manifest_id="manifest:synthetic",
        policy_interface_version="3",
        feature_contract_id="features-v3",
        feature_calculator_id="calculator-v3",
        source_revision="ab385d792e19ff6db39d87f1123f47f660fc1e1d",
        runtime_identity="synthetic-runtime",
        execution_profile_id="synthetic-paper-profile",
        paper_account_environment_id="synthetic-paper-account",
        store_identity="synthetic-policy-store",
    )
    holding = HoldingEpisode.open(
        deployment_generation_id=deployment.deployment_generation_id,
        security_id="sec:xyz",
        symbol="XYZ",
        broker_symbol="XYZ",
        opening_action_id="opening-action-1",
        initial_filled_quantity=Decimal("24"),
        entry_price=Decimal("100"),
    )
    decision = DecisionIdentity.build(
        deployment=deployment,
        clock=policy_clock,
        snapshot_sha256="b" * 64,
        category=DecisionCategory.EXIT,
        subject_type=DecisionSubjectType.HOLDING,
        subject_id=holding.holding_episode_id,
    )
    holding, stop_update = propose_stop_update(
        holding,
        decision=decision,
        stop_price=Decimal("90"),
    )
    holding = confirm_protective_stop(
        holding,
        intent=stop_update,
        stop_price=Decimal("90"),
        client_order_id="protective-client-2",
        broker_order_id="protective-broker-2",
        observed_at=valuation_time,
    )
    intent = build_action_intent(
        decision=decision,
        security_id="sec:xyz",
        broker_symbol="XYZ",
        holding_episode_id=holding.holding_episode_id,
        role=ActionRole.SCALE_OUT,
        side=OrderSide.SELL,
        requested_quantity=Decimal("10"),
        status=ActionStatus.SUBMITTED,
        exit_tier=1,
        snapshot_original_quantity=Decimal("24"),
        fraction_of_original_quantity=Decimal("0.42"),
        rounding_rule_id="whole_share_floor_v1",
    )
    intent = bind_attempt_order_refs(
        intent,
        attempt_number=1,
        client_order_id="scale-client-1",
        broker_order_id="scale-broker-1",
    )
    intent = add_attempt_order_aliases(
        intent,
        attempt_number=1,
        client_order_id="scale-client-alias-1",
        broker_order_id="scale-broker-alias-1",
    )
    intent = apply_cumulative_fill(intent, Decimal("4"))
    holding = apply_action_fill_to_holding(
        holding,
        intent,
        incremental_fill_notional=Decimal("400"),
        incremental_fees=Decimal("0"),
    )
    intent = request_attempt_cancel(intent, 1)
    intent = confirm_attempt_terminal(intent, 1, status=ActionAttemptStatus.CANCELLED)
    remainder_ready_projection = project_action_state(intent)
    intent = create_single_remainder_attempt(intent)
    intent = bind_attempt_order_refs(
        intent,
        attempt_number=2,
        client_order_id="scale-client-2",
        broker_order_id="scale-broker-2",
    )
    intent = add_attempt_order_aliases(
        intent,
        attempt_number=2,
        client_order_id="scale-client-alias-2",
        broker_order_id="scale-broker-alias-2",
    )
    intent = replace(
        intent,
        status=ActionStatus.SUBMITTED,
        order_attempts=(
            intent.order_attempts[0],
            replace(intent.order_attempts[1], status=ActionAttemptStatus.SUBMITTED),
        ),
    )
    portfolio_snapshot = PortfolioStateSnapshot(
        deployment_identity=deployment,
        clock=policy_clock,
        source_namespace="synthetic-broker",
        account_snapshot_id="account-snapshot-1",
        equity=Decimal("10000"),
        cash=Decimal("8000"),
        gross_exposure=Decimal("2000"),
        open_risk=Decimal("200"),
        portfolio_peak_equity=Decimal("12500"),
        last_accepted_session=decision_session,
    )
    protective_order = BrokerOrderFact(
        "protective-broker-2",
        "protective-client-2",
        "XYZ",
        "sell",
        "submitted",
        20,
        0,
        purpose="protective_stop",
        holding_episode_id=holding.holding_episode_id,
        stop_price=90,
    )
    account = replace(
        _account(clock=account_clock),
        paper_account_environment_id=deployment.paper_account_environment_id,
        decision_slot_id=decision.decision_slot_id,
        decision_id=decision.decision_id,
        cash=8_000,
        balance_observed_at=valuation_time,
        positions=(replace(_account().positions[0], quantity=20, mark_observed_at=valuation_time),),  # type: ignore[index]
        open_orders=(
            protective_order,
            BrokerOrderFact(
                "scale-broker-alias-2",
                "scale-client-alias-2",
                "XYZ",
                "sell",
                "submitted",
                6,
                0,
            ),
        ),
    )

    projection = policy_execution_state_to_projection(
        account=account,
        portfolio_snapshot=portfolio_snapshot,
        action_projections=(project_action_state(intent),),
        holding_episodes=(holding,),
    )
    remainder_ready_adapter_projection = policy_execution_state_to_projection(
        account=replace(account, open_orders=(protective_order,)),
        portfolio_snapshot=portfolio_snapshot,
        action_projections=(remainder_ready_projection,),
        holding_episodes=(holding,),
    )
    active_result = reconcile_account_snapshot(
        account=account,
        projection=projection,
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )
    remainder_ready_result = reconcile_account_snapshot(
        account=replace(account, open_orders=(protective_order,)),
        projection=remainder_ready_adapter_projection,
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )
    attempts = projection.pending_actions[0].order_attempts  # type: ignore[index]
    first_attempt = remainder_ready_adapter_projection.pending_actions[0].order_attempts[0]  # type: ignore[index]

    assert [(item.attempt_number, item.client_order_id, item.broker_order_id) for item in attempts] == [
        (1, "scale-client-1", "scale-broker-1"),
        (2, "scale-client-2", "scale-broker-2"),
    ]
    assert [item.client_order_aliases for item in attempts] == [
        ("scale-client-alias-1",),
        ("scale-client-alias-2",),
    ]
    assert [item.broker_order_aliases for item in attempts] == [
        ("scale-broker-alias-1",),
        ("scale-broker-alias-2",),
    ]
    assert remainder_ready_adapter_projection.pending_actions[0].status == "remainder_ready"  # type: ignore[index]
    assert first_attempt.status == "cancelled"
    assert first_attempt.client_order_id == "scale-client-1"
    assert first_attempt.broker_order_id == "scale-broker-1"
    assert active_result.ready is True, active_result.findings
    assert active_result.pending_sell_count == 1
    assert active_result.open_position_risk == 200
    assert remainder_ready_result.ready is True, remainder_ready_result.findings
    assert remainder_ready_result.pending_sell_count == 1


def test_partial_fill_reserves_only_residual_buy_once_and_builds_v3_portfolio() -> None:
    result = reconcile_account_snapshot(
        account=_account(),
        projection=_projection(),
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )

    assert result.ready is True
    assert result.settled_cash == 7_600
    assert result.reserved_buy_cash == 600
    assert result.available_cash == 7_000
    assert result.reserved_buy_risk == 60
    assert result.pending_entry_count == 1
    assert result.gross_exposure == 2_400
    assert result.open_position_risk == 240
    assert result.portfolio_features is not None
    assert result.portfolio_features.gross_exposure_fraction == pytest.approx(0.24)
    assert result.portfolio_features.drawdown_fraction == pytest.approx(0.20)
    assert result.portfolio_features.open_risk_fraction == pytest.approx(0.024)
    assert result.portfolio_features.sector_exposures == (("Technology", 0.24),)
    assert result.portfolio_features.industry_exposures == (("Software", 0.24),)


def test_same_partial_fill_projection_is_idempotent_across_restart() -> None:
    inputs = {
        "account": _account(),
        "projection": _projection(),
        "maximum_balance_age": timedelta(minutes=15),
        "maximum_mark_age": timedelta(minutes=15),
    }

    before_restart = reconcile_account_snapshot(**inputs)
    after_restart = reconcile_account_snapshot(**inputs)

    assert after_restart == before_restart
    assert after_restart.reserved_buy_cash == 600
    assert after_restart.pending_entry_count == 1


def test_duplicate_local_and_broker_references_do_not_double_reserve() -> None:
    projection = _projection()
    action = projection.pending_actions[0]  # type: ignore[index]
    duplicated_action = replace(
        action,
        client_order_refs=action.client_order_refs + action.client_order_refs,
        broker_order_refs=action.broker_order_refs + action.broker_order_refs,
    )
    duplicate_projection = replace(
        projection,
        pending_actions=(duplicated_action, duplicated_action),
    )
    account = _account()
    duplicate_account = replace(
        account,
        open_orders=account.open_orders + account.open_orders,  # type: ignore[operator]
    )

    result = reconcile_account_snapshot(
        account=duplicate_account,
        projection=duplicate_projection,
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )

    assert result.ready is True
    assert result.reserved_buy_cash == 600
    assert result.pending_entry_count == 1


@pytest.mark.parametrize(
    ("broker_order_id", "client_order_id"),
    (("broker-1", "wrong-client"), ("wrong-broker", "client-1")),
)
def test_duplicate_broker_rows_with_conflicting_alias_pairs_block_reservations(
    broker_order_id: str,
    client_order_id: str,
) -> None:
    original = _account().open_orders[0]  # type: ignore[index]
    conflicting_alias = replace(
        original,
        broker_order_id=broker_order_id,
        client_order_id=client_order_id,
    )
    result = reconcile_account_snapshot(
        account=replace(_account(), open_orders=(original, conflicting_alias)),
        projection=_projection(),
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )

    assert result.ready is False
    assert result.reserved_buy_cash is None
    assert result.pending_entry_count is None
    assert any(finding.state == "conflicting" for finding in result.findings)


def test_missing_classification_is_unready_and_never_becomes_zero_exposure() -> None:
    account = _account(
        positions=(replace(_account().positions[0], sector=None),),  # type: ignore[index]
    )

    result = reconcile_account_snapshot(
        account=account,
        projection=_projection(),
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )

    assert result.ready is False
    assert result.portfolio_features is None
    assert result.sector_exposures is None
    assert any(finding.path == "positions.XYZ.sector" for finding in result.findings)


def test_stale_valuation_is_unready_and_does_not_publish_v3_features() -> None:
    stale_mark = datetime(2026, 9, 30, 19, 30, tzinfo=UTC)
    position = replace(_account().positions[0], mark_observed_at=stale_mark)  # type: ignore[index]

    result = reconcile_account_snapshot(
        account=_account(positions=(position,)),
        projection=_projection(),
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )

    assert result.ready is False
    assert result.gross_exposure is None
    assert result.portfolio_features is None
    assert any(finding.state == "stale_by_declared_rule" for finding in result.findings)


def test_unknown_stop_is_unready_and_does_not_become_zero_risk() -> None:
    projection = _projection(
        holdings=(replace(_projection().holdings[0], stop_price=None),),  # type: ignore[index]
    )

    result = reconcile_account_snapshot(
        account=_account(),
        projection=projection,
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )

    assert result.ready is False
    assert result.open_position_risk is None
    assert result.portfolio_features is None
    assert any(finding.path == "holdings.holding-1.stop_price" for finding in result.findings)


def test_missing_open_order_snapshot_is_not_treated_as_no_reservations() -> None:
    result = reconcile_account_snapshot(
        account=replace(_account(), open_orders=None),
        projection=_projection(),
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )

    assert result.ready is False
    assert result.reserved_buy_cash is None
    assert result.pending_entry_count is None
    assert any(finding.path == "open_orders" for finding in result.findings)


def test_clock_mismatch_blocks_snapshot_reconciliation() -> None:
    different_clock = _clock(next_execution_session=date(2026, 10, 2))

    result = reconcile_account_snapshot(
        account=_account(),
        projection=_projection(clock=different_clock),
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )

    assert result.ready is False
    assert result.portfolio_features is None
    assert any(finding.path == "decision_clock" for finding in result.findings)


def test_nonfinite_account_amount_is_rejected_as_invalid_input() -> None:
    with pytest.raises(ValueError, match="equity"):
        _account(equity=float("nan"))


def test_competing_entry_add_on_and_replacement_commit_cash_and_risk_once() -> None:
    projection = _projection()
    first = projection.pending_actions[0]  # type: ignore[index]
    second = replace(
        first,
        logical_action_id="action-2",
        holding_episode_id="holding-1",
        security_id="sec:xyz",
        role="addition",
        status="submitted",
        requested_quantity=8,
        confirmed_quantity=0,
        residual_quantity=8,
        reservation_amount=400,
        reservation_price=50,
        reservation_stop_price=45,
        client_order_refs=("client-2",),
        broker_order_refs=("broker-2",),
    )
    third = replace(
        second,
        logical_action_id="action-3",
        security_id="sec:def",
        role="replacement",
        requested_quantity=2,
        residual_quantity=2,
        reservation_amount=300,
        reservation_price=150,
        reservation_stop_price=130,
        client_order_refs=("client-3",),
        broker_order_refs=("broker-3",),
    )
    projection = replace(
        projection,
        pending_actions=(first, second, third),
        security_symbol_mappings=projection.security_symbol_mappings + (
            SecuritySymbolMapping(
                security_id="sec:def",
                broker_symbol="DEF",
                mapping_contract_id="synthetic-identity-contract",
                effective_from=date(2026, 1, 1),
                effective_through=None,
                validated_at=VALUATION_TIME,
            ),
        ),
    )
    account = replace(
        _account(),
        open_orders=(
            _account().open_orders[0],  # type: ignore[index]
            BrokerOrderFact("broker-2", "client-2", "XYZ", "buy", "submitted", 8, 0),
            BrokerOrderFact("broker-3", "client-3", "DEF", "buy", "submitted", 2, 0),
        ),
    )

    result = reconcile_account_snapshot(
        account=account,
        projection=projection,
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )

    assert result.ready is True
    assert result.reserved_buy_cash == 1_300
    assert result.available_cash == 6_300
    assert result.reserved_buy_risk == 140
    assert result.open_position_risk == 240
    assert result.total_committed_risk == 380
    assert result.pending_entry_count == 3


def test_missing_pending_risk_basis_preserves_known_cash_but_blocks_risk_readiness() -> None:
    projection = _projection()
    action = replace(projection.pending_actions[0], reservation_stop_price=None)  # type: ignore[index]
    projection = replace(projection, pending_actions=(action,))

    result = reconcile_account_snapshot(
        account=_account(),
        projection=projection,
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )

    assert result.ready is False
    assert result.reserved_buy_cash == 600
    assert result.reserved_buy_risk is None
    assert result.total_committed_risk is None
    assert result.pending_entry_count == 1
    assert any(finding.path == "pending_actions.action-1.reservation_stop_price" for finding in result.findings)


def test_account_valuation_at_next_opportunity_may_follow_feature_cutoff() -> None:
    feature_cutoff = datetime(2026, 9, 30, 20, 0, tzinfo=UTC)
    execution_valuation = datetime(2026, 10, 1, 13, 30, tzinfo=UTC)
    clock = _clock(as_of_cutoff=feature_cutoff, valuation_time=execution_valuation)
    account = _account(clock=clock)
    account = replace(
        account,
        balance_observed_at=execution_valuation,
        positions=(replace(account.positions[0], mark_observed_at=execution_valuation),),  # type: ignore[index]
    )

    result = reconcile_account_snapshot(
        account=account,
        projection=_projection(clock=clock),
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )

    assert result.ready is True
    assert result.as_of_cutoff == feature_cutoff
    assert result.valuation_time == execution_valuation


def test_conflicting_duplicate_broker_order_reference_blocks_reservations() -> None:
    original = _account().open_orders[0]  # type: ignore[index]
    conflict = replace(original, requested_quantity=11)
    account = replace(_account(), open_orders=(original, conflict))

    result = reconcile_account_snapshot(
        account=account,
        projection=_projection(),
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )

    assert result.ready is False
    assert result.reserved_buy_cash is None
    assert result.pending_entry_count is None
    assert any(finding.state == "conflicting" for finding in result.findings)


def test_missing_balance_is_not_reported_as_zero_cash_or_equity() -> None:
    result = reconcile_account_snapshot(
        account=_account(cash=None, equity=None),
        projection=_projection(),
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )

    assert result.ready is False
    assert result.settled_cash is None
    assert result.equity is None
    assert result.available_cash is None
    assert result.portfolio_features is None


def test_confirmed_cancel_releases_residual_cash_and_risk_reservation() -> None:
    projection = _projection()
    action = replace(
        projection.pending_actions[0],  # type: ignore[index]
        status="cancelled",
        client_order_refs=(),
        broker_order_refs=(),
    )
    projection = replace(projection, pending_actions=(action,))
    account = replace(_account(), open_orders=())

    result = reconcile_account_snapshot(
        account=account,
        projection=projection,
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )

    assert result.ready is True
    assert result.reserved_buy_cash == 0
    assert result.reserved_buy_risk == 0
    assert result.pending_entry_count == 0
    assert result.available_cash == 7_600


def test_missing_security_mapping_never_assumes_security_id_is_a_ticker() -> None:
    projection = replace(_projection(), security_symbol_mappings=())

    result = reconcile_account_snapshot(
        account=_account(),
        projection=projection,
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )

    assert result.ready is False
    assert result.reserved_buy_cash is None
    assert result.pending_entry_count is None
    assert any(finding.path == "pending_actions.action-1.security_id" for finding in result.findings)


@pytest.mark.parametrize("side", ("buy", "sell"))
def test_unavailable_security_mapping_with_matched_order_returns_unready(side: str) -> None:
    if side == "buy":
        action = _projection().pending_actions[0]  # type: ignore[index]
        account = _account()
    else:
        action = PolicyActionProjection(
            logical_action_id="scale-out-1",
            deployment_generation_id="generation-1",
            holding_episode_id="holding-1",
            security_id="sec:xyz",
            role="scale_out",
            side="sell",
            status="partially_filled",
            requested_quantity=10,
            confirmed_quantity=4,
            residual_quantity=6,
            reservation_amount=None,
            reservation_price=None,
            reservation_price_basis=None,
            reservation_stop_price=None,
            client_order_refs=("sell-client-1",),
            broker_order_refs=("sell-broker-1",),
            resolution_reason=None,
        )
        account = replace(
            _account(),
            open_orders=(BrokerOrderFact("sell-broker-1", "sell-client-1", "XYZ", "sell", "partially_filled", 10, 4),),
        )
    projection = replace(
        _projection(),
        pending_actions=(action,),
        security_symbol_mappings=None,
    )

    result = reconcile_account_snapshot(
        account=account,
        projection=projection,
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )

    assert result.ready is False
    assert result.reserved_buy_cash is None
    assert result.pending_entry_count is None
    assert result.pending_sell_count is None
    assert any(finding.path == "security_symbol_mappings" for finding in result.findings)


def test_holding_specific_action_requires_a_matching_holding_episode() -> None:
    action = replace(
        _projection().pending_actions[0],  # type: ignore[index]
        role="addition",
        holding_episode_id="missing-holding",
    )
    result = reconcile_account_snapshot(
        account=_account(),
        projection=replace(_projection(), pending_actions=(action,)),
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )

    assert result.ready is False
    assert any(
        finding.path == "pending_actions.action-1.holding_episode_id"
        and finding.state == "absent"
        for finding in result.findings
    )


def test_holding_specific_action_security_must_match_its_episode() -> None:
    action = replace(
        _projection().pending_actions[0],  # type: ignore[index]
        role="addition",
        holding_episode_id="holding-1",
        security_id="sec:other",
    )
    projection = replace(
        _projection(),
        pending_actions=(action,),
        security_symbol_mappings=_projection().security_symbol_mappings
        + (
            SecuritySymbolMapping(
                security_id="sec:other",
                broker_symbol="ABC",
                mapping_contract_id="synthetic-other-identity",
                effective_from=date(2026, 1, 1),
                effective_through=None,
                validated_at=VALUATION_TIME,
            ),
        ),  # type: ignore[operator]
    )
    account = replace(
        _account(),
        open_orders=(replace(_account().open_orders[0], symbol="ABC"),),  # type: ignore[index]
    )

    result = reconcile_account_snapshot(
        account=account,
        projection=projection,
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )

    assert result.ready is False
    assert any(
        finding.path == "pending_actions.action-1.security_id"
        and finding.state == "conflicting"
        for finding in result.findings
    )


def test_holding_specific_action_generation_must_match_its_episode() -> None:
    action = replace(
        _projection().pending_actions[0],  # type: ignore[index]
        role="addition",
        holding_episode_id="holding-1",
        deployment_generation_id="generation-2",
    )
    result = reconcile_account_snapshot(
        account=_account(),
        projection=replace(_projection(), pending_actions=(action,)),
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )

    assert result.ready is False
    assert any(
        finding.path == "pending_actions.action-1.deployment_generation_id"
        and finding.state == "conflicting"
        for finding in result.findings
    )


def test_matching_old_generation_holding_action_is_valid_under_new_active_generation() -> None:
    action = replace(
        _projection().pending_actions[0],  # type: ignore[index]
        role="addition",
        holding_episode_id="holding-1",
    )
    result = reconcile_account_snapshot(
        account=_account(),
        projection=replace(
            _projection(),
            pending_actions=(action,),
            active_deployment_generation_id="generation-2",
        ),
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )

    assert result.ready is True, result.findings
    assert result.pending_entry_count == 1


def test_partial_sell_restart_reconciles_position_and_keeps_sell_pending_visible() -> None:
    projection = _projection(
        holdings=(replace(_projection().holdings[0], remaining_quantity=20),),  # type: ignore[index]
    )
    sell = PolicyActionProjection(
        logical_action_id="scale-out-1",
        deployment_generation_id="generation-1",
        holding_episode_id="holding-1",
        security_id="sec:xyz",
        role="scale_out",
        side="sell",
        status="partially_filled",
        requested_quantity=10,
        confirmed_quantity=4,
        residual_quantity=6,
        reservation_amount=None,
        reservation_price=None,
        reservation_price_basis=None,
        reservation_stop_price=None,
        client_order_refs=("sell-client-1",),
        broker_order_refs=("sell-broker-1",),
        resolution_reason=None,
    )
    projection = replace(projection, pending_actions=(sell,))
    account = _account(
        cash=8_000,
        positions=(replace(_account().positions[0], quantity=20),),  # type: ignore[index]
        open_orders=(BrokerOrderFact("sell-broker-1", "sell-client-1", "XYZ", "sell", "partially_filled", 10, 4),),
    )

    after_restart = reconcile_account_snapshot(
        account=account,
        projection=projection,
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )

    assert after_restart.ready is True
    assert after_restart.gross_exposure == 2_000
    assert after_restart.open_position_risk == 200
    assert after_restart.pending_entry_count == 0
    assert after_restart.pending_sell_count == 1


def test_reconciliation_required_sell_blocks_readiness_instead_of_disappearing() -> None:
    sell = PolicyActionProjection(
        logical_action_id="uncertain-sell-1",
        deployment_generation_id="generation-1",
        holding_episode_id="holding-1",
        security_id="sec:xyz",
        role="scale_out",
        side="sell",
        status="reconciliation_required",
        requested_quantity=5,
        confirmed_quantity=0,
        residual_quantity=5,
        reservation_amount=None,
        reservation_price=None,
        reservation_price_basis=None,
        reservation_stop_price=None,
        client_order_refs=(),
        broker_order_refs=(),
        resolution_reason=None,
    )
    result = reconcile_account_snapshot(
        account=replace(_account(), open_orders=()),
        projection=replace(_projection(), pending_actions=(sell,)),
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )

    assert result.ready is False
    assert result.reserved_buy_cash is None
    assert result.pending_entry_count is None
    assert any(finding.state == "unresolved" for finding in result.findings)


def test_expired_broker_order_without_canonical_attempt_state_fails_closed() -> None:
    action = replace(_projection().pending_actions[0], status="remainder_ready")  # type: ignore[index]
    broker_order = replace(_account().open_orders[0], status="expired")  # type: ignore[index]
    result = reconcile_account_snapshot(
        account=replace(_account(), open_orders=(broker_order,)),
        projection=replace(_projection(), pending_actions=(action,)),
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )

    assert result.ready is False
    assert result.reserved_buy_cash is None
    assert any(finding.state == "unresolved" for finding in result.findings)


def test_protective_sell_is_matched_to_holding_and_not_counted_as_strategy_sell() -> None:
    holding = replace(
        _projection().holdings[0],  # type: ignore[index]
        protective_stop_order_references=(
            OrderReference(broker_order_id="protective-1", client_order_id="protective-client-1"),
        ),
    )
    projection = replace(_projection(), holdings=(holding,))
    protective_order = BrokerOrderFact(
        broker_order_id="protective-1",
        client_order_id="protective-client-1",
        symbol="XYZ",
        side="sell",
        status="submitted",
        requested_quantity=24,
        cumulative_filled_quantity=0,
        purpose="protective_stop",
        holding_episode_id="holding-1",
        stop_price=90,
    )
    account = replace(
        _account(),
        open_orders=_account().open_orders + (protective_order,),  # type: ignore[operator]
    )

    result = reconcile_account_snapshot(
        account=account,
        projection=projection,
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )

    assert result.ready is True
    assert result.pending_sell_count == 0
    assert result.open_position_risk == 240


def test_pending_sell_with_unknown_protection_identity_blocks_readiness() -> None:
    protective_order = BrokerOrderFact(
        broker_order_id="unknown-protective",
        client_order_id="unknown-protective-client",
        symbol="XYZ",
        side="sell",
        status="submitted",
        requested_quantity=24,
        cumulative_filled_quantity=0,
        purpose="protective_stop",
        holding_episode_id="holding-1",
        stop_price=90,
    )
    account = replace(_account(), open_orders=_account().open_orders + (protective_order,))  # type: ignore[operator]

    result = reconcile_account_snapshot(
        account=account,
        projection=_projection(),
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )

    assert result.ready is False
    assert result.pending_sell_count is None
    assert any("protective" in finding.detail for finding in result.findings)


def test_canonical_store_read_reconciles_old_generation_state_with_current_portfolio(tmp_path) -> None:
    store = PolicyExecutionStateStore(tmp_path / "issue99-canonical.sqlite3", store_identity="synthetic-policy-store")
    store.migrate()
    generation_a = PolicyDeploymentIdentity(
        policy_artifact_id="policy:canonical-test",
        capability_manifest_id="manifest:canonical-test",
        policy_interface_version="3",
        feature_contract_id="features-v3",
        feature_calculator_id="calculator-v3",
        source_revision="a" * 40,
        runtime_identity="runtime:generation-a",
        execution_profile_id="paper-profile-v1",
        paper_account_environment_id="synthetic-paper-account",
        store_identity="synthetic-policy-store",
    )
    generation_b = replace(generation_a, source_revision="b" * 40, runtime_identity="runtime:generation-b")
    for deployment in (generation_a, generation_b):
        store.register_deployment_identity(
            deployment, lifecycle="prepared", handler_identity="fixture-handler", guard_id="guard-v1"
        )

    old_clock = DecisionClock(
        exchange_id="XNYS",
        decision_session=date(2026, 9, 30),
        as_of_cutoff_at=datetime(2026, 9, 30, 20, 0, tzinfo=UTC),
        next_execution_session=date(2026, 10, 1),
        account_valuation_session=date(2026, 10, 1),
        account_valuation_at=datetime(2026, 10, 1, 13, 30, tzinfo=UTC),
    )

    def old_decision(snapshot: str, category: DecisionCategory, subject_type: DecisionSubjectType,
                     subject_id: str, *, sequence: int | None = None) -> DecisionIdentity:
        return DecisionIdentity.build(
            deployment=generation_a,
            clock=old_clock,
            snapshot_sha256=snapshot * 64,
            category=category,
            subject_type=subject_type,
            subject_id=subject_id,
            sequence=sequence,
        )

    opening_decision = old_decision("a", DecisionCategory.ENTRY, DecisionSubjectType.SECURITY, "FIGI-BB1234")
    opening = build_action_intent(
        decision=opening_decision,
        security_id="FIGI-BB1234",
        broker_symbol="XYZ",
        role=ActionRole.ENTRY,
        side=OrderSide.BUY,
        requested_quantity=Decimal("24"),
        reservation_price=Decimal("100"),
        reservation_price_basis="limit_price",
        reservation_stop_price=Decimal("90"),
        risk_per_unit=Decimal("10"),
        risk_basis="entry_to_protective_stop",
    )
    store.record_decision(opening_decision, policy_payload={}, guard_payload={}, effective_action_payload={})
    store.record_action_intent(opening, expected_version=None)
    store.record_cumulative_fill(
        opening.logical_action_id,
        1,
        provider_id="provider-a",
        fill_event_id="canonical-opening-fill",
        cumulative_quantity=Decimal("24"),
        cumulative_notional=Decimal("2400"),
        cumulative_fees=Decimal("0"),
        payload_sha256=None,
        observed_at=datetime(2026, 10, 1, 13, 31, tzinfo=UTC),
        expected_action_version=0,
        expected_holding_version=None,
    )
    holding = store.load_holding_episode_for_action(opening.logical_action_id)

    stop_decision = old_decision(
        "c", DecisionCategory.EXIT, DecisionSubjectType.HOLDING, holding.holding_episode_id, sequence=1
    )
    store.record_decision(stop_decision, policy_payload={}, guard_payload={}, effective_action_payload={})
    stop_intent = store.propose_stop_update(
        holding.holding_episode_id,
        decision=stop_decision,
        stop_price=Decimal("90"),
        expected_holding_version=holding.state_version,
        observed_at=datetime(2026, 10, 1, 13, 40, tzinfo=UTC),
    )
    holding = store.load_holding_episode(holding.holding_episode_id)
    holding = store.confirm_protective_stop(
        stop_intent,
        stop_price=Decimal("90"),
        client_order_id="stop-client-a",
        broker_order_id="stop-broker-a",
        observed_at=datetime(2026, 10, 1, 13, 41, tzinfo=UTC),
        expected_holding_version=holding.state_version,
    )

    scale_decision = old_decision(
        "d", DecisionCategory.EXIT, DecisionSubjectType.HOLDING, holding.holding_episode_id, sequence=2
    )
    store.record_decision(scale_decision, policy_payload={}, guard_payload={}, effective_action_payload={})
    scale_out = build_action_intent(
        decision=scale_decision,
        security_id="FIGI-BB1234",
        broker_symbol="XYZ",
        holding_episode_id=holding.holding_episode_id,
        role=ActionRole.SCALE_OUT,
        side=OrderSide.SELL,
        requested_quantity=Decimal("12"),
        exit_tier=1,
        snapshot_original_quantity=Decimal("24"),
        fraction_of_original_quantity=Decimal("0.5"),
        rounding_rule_id="whole_share_floor_v1",
    )
    store.record_action_intent(scale_out, expected_version=None)
    primary = store.bind_attempt_order_refs(
        scale_out.logical_action_id, 1, provider_id="provider-a", client_order_id="scale-client-a",
        broker_order_id="scale-broker-a", expected_action_version=0,
        observed_at=datetime(2026, 10, 1, 14, 0, tzinfo=UTC),
    )
    aliased = store.bind_attempt_order_refs(
        scale_out.logical_action_id, 1, provider_id="provider-b", client_order_id="scale-client-b",
        broker_order_id="scale-broker-b", expected_action_version=primary.state_version,
        observed_at=datetime(2026, 10, 1, 14, 1, tzinfo=UTC),
    )
    holding = store.load_holding_episode(holding.holding_episode_id)
    store.record_cumulative_fill(
        scale_out.logical_action_id,
        1,
        provider_id="provider-b",
        fill_event_id="canonical-scaleout-partial-fill",
        cumulative_quantity=Decimal("4"),
        cumulative_notional=Decimal("400"),
        cumulative_fees=Decimal("0"),
        payload_sha256=None,
        observed_at=datetime(2026, 10, 1, 14, 5, tzinfo=UTC),
        expected_action_version=aliased.state_version,
        expected_holding_version=holding.state_version,
    )
    holding = store.load_holding_episode(holding.holding_episode_id)

    current_cutoff = datetime(2026, 10, 1, 20, 0, tzinfo=UTC)
    current_valuation = datetime(2026, 10, 2, 13, 30, tzinfo=UTC)
    current_clock = DecisionClock(
        exchange_id="XNYS",
        decision_session=date(2026, 10, 1),
        as_of_cutoff_at=current_cutoff,
        next_execution_session=date(2026, 10, 2),
        account_valuation_session=date(2026, 10, 2),
        account_valuation_at=current_valuation,
    )
    current_decision = DecisionIdentity.build(
        deployment=generation_b,
        clock=current_clock,
        snapshot_sha256="e" * 64,
        category=DecisionCategory.ENTRY,
        subject_type=DecisionSubjectType.SECURITY,
        subject_id="FIGI-BB1234",
    )
    store.set_active_generation(
        generation_b.paper_account_environment_id,
        expected_generation_id=None,
        new_generation_id=generation_b.deployment_generation_id,
        readiness_evidence_ref="synthetic-generation-b-ready",
        outgoing_entries_reconciled=True,
    )
    portfolio = PortfolioStateSnapshot(
        deployment_identity=generation_b,
        clock=current_clock,
        source_namespace="synthetic-broker",
        account_snapshot_id="current-account-snapshot",
        equity=Decimal("10000"),
        cash=Decimal("8000"),
        gross_exposure=Decimal("2000"),
        open_risk=Decimal("200"),
        portfolio_peak_equity=Decimal("12500"),
        last_accepted_session=date(2026, 10, 1),
    )
    store.record_portfolio_snapshot(portfolio)
    holding = store.load_holding_episode(holding.holding_episode_id)
    store.update_holding_marks(
        holding.holding_episode_id,
        valuation_at=current_valuation,
        peak_price=Decimal("105"),
        expected_holding_version=holding.state_version,
    )

    # One canonical read returns pinned generation-A records with generation-B portfolio facts.
    canonical = store.load_policy_execution_snapshot(
        deployment_generation_id=generation_b.deployment_generation_id,
        portfolio_snapshot_id=portfolio.portfolio_snapshot_id,
    )
    assert canonical.deployment_identity == generation_b
    assert canonical.portfolio_snapshot == portfolio
    assert {item.deployment_generation_id for item in canonical.holding_episodes} == {
        generation_a.deployment_generation_id
    }
    actions = {item.logical_action_id: item for item in canonical.action_projections}
    source_action = actions[scale_out.logical_action_id]
    assert source_action.deployment_generation_id == generation_a.deployment_generation_id
    assert source_action.clock_id == scale_decision.clock.clock_id
    assert source_action.state_version == 3
    assert source_action.order_attempts[0].client_order_aliases == ("scale-client-b",)
    assert source_action.order_attempts[0].broker_order_aliases == ("scale-broker-b",)

    account_clock = AccountValuationClock(
        completed_session=date(2026, 10, 1),
        as_of_cutoff=current_cutoff,
        next_execution_session=date(2026, 10, 2),
        valuation_time=current_valuation,
    )
    account = BrokerAccountSnapshot(
        paper_account_environment_id=generation_b.paper_account_environment_id,
        decision_slot_id=current_decision.decision_slot_id,
        decision_id=current_decision.decision_id,
        clock=account_clock,
        equity=10000,
        cash=8000,
        peak_equity=12500,
        balance_observed_at=current_valuation,
        peak_observed_at=current_valuation,
        positions=(BrokerPositionFact(
            symbol="XYZ", quantity=20, mark_price=100, mark_observed_at=current_valuation,
            sector=_classification(), industry=_classification(code="Software"),
        ),),
        open_orders=(
            BrokerOrderFact(
                "scale-broker-b", "scale-client-b", "XYZ", "sell", "partially_filled", 12, 4
            ),
            BrokerOrderFact(
                "stop-broker-a", "stop-client-a", "XYZ", "sell", "submitted", 20, 0,
                purpose="protective_stop", holding_episode_id=holding.holding_episode_id, stop_price=90,
            ),
        ),
        source_namespace="synthetic-broker",
        account_snapshot_id="current-account-snapshot",
    )
    projection = policy_execution_state_to_projection(
        account=account,
        portfolio_snapshot=canonical.portfolio_snapshot,
        action_projections=canonical.action_projections,
        holding_episodes=canonical.holding_episodes,
    )
    result = reconcile_account_snapshot(
        account=account,
        projection=projection,
        maximum_balance_age=timedelta(minutes=15),
        maximum_mark_age=timedelta(minutes=15),
    )
    assert result.ready is True, result.findings
    assert result.pending_sell_count == 1
    assert result.pending_entry_count == 0
    assert result.total_committed_risk == 200
    assert projection.active_deployment_generation_id == generation_b.deployment_generation_id
    assert projection.pending_actions is not None
    assert next(item for item in projection.pending_actions if item.logical_action_id == scale_out.logical_action_id).source_next_execution_session == date(2026, 10, 1)
