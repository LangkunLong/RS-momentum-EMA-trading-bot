from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from core.policy_execution_state import (
    ActionRole,
    ActionStatus,
    ActionAttemptStatus,
    DecisionClock,
    DecisionCategory,
    DecisionConflictError,
    DecisionIdentity,
    DecisionSubjectType,
    HoldingEpisode,
    IdentityConflictError,
    MissedExecutionSessionError,
    OrderSide,
    PolicyDeploymentIdentity,
    PortfolioStateSnapshot,
    advance_holding_exit_tier,
    apply_action_fill_to_holding,
    apply_attempt_cumulative_fill,
    apply_cumulative_fill,
    assert_execution_session,
    assert_same_decision_slot_consistent,
    assert_same_logical_action,
    bind_attempt_order_refs,
    build_action_intent as _build_action_intent,
    confirm_attempt_terminal,
    confirm_protective_stop,
    create_single_remainder_attempt,
    project_action_state,
    propose_stop_update,
    request_attempt_cancel,
    resolve_action,
    update_holding_marks,
)


UTC = timezone.utc


def build_action_intent(**kwargs):
    kwargs.setdefault("broker_symbol", "ACME")
    return _build_action_intent(**kwargs)


def _clock() -> DecisionClock:
    return DecisionClock(
        exchange_id="XNYS",
        decision_session=date(2026, 9, 30),
        as_of_cutoff_at=datetime(2026, 9, 30, 20, 0, tzinfo=UTC),
        next_execution_session=date(2026, 10, 1),
        account_valuation_session=date(2026, 10, 1),
        account_valuation_at=datetime(2026, 10, 1, 13, 30, tzinfo=UTC),
    )


def _deployment() -> PolicyDeploymentIdentity:
    return PolicyDeploymentIdentity(
        policy_artifact_id="fixed-policy:sha256:policy",
        capability_manifest_id="sha256:manifest",
        policy_interface_version="3",
        feature_contract_id="feature-contract-v1",
        feature_calculator_id="calculator-v1",
        source_revision="ab385d792e19ff6db39d87f1123f47f660fc1e1d",
        runtime_identity="runtime:recorded-fixture",
        execution_profile_id="paper-profile-v1",
        paper_account_environment_id="synthetic-paper-account",
        store_identity="temporary-db-fixture",
    )


def _decision(
    snapshot_digest: str = "a" * 64,
    *,
    clock: DecisionClock | None = None,
    category: DecisionCategory = DecisionCategory.ENTRY,
    subject_type: DecisionSubjectType = DecisionSubjectType.SECURITY,
    subject_id: str = "FIGI-BB1234",
) -> DecisionIdentity:
    return DecisionIdentity.build(
        deployment=_deployment(),
        clock=clock or _clock(),
        snapshot_sha256=snapshot_digest,
        category=category,
        subject_type=subject_type,
        subject_id=subject_id,
    )


def test_decision_and_action_identity_survive_reconstruction_and_conflicts_are_explicit() -> None:
    first = _decision()
    restarted = _decision()
    assert first.decision_slot_id == restarted.decision_slot_id
    assert first.decision_id == restarted.decision_id

    action_a = build_action_intent(
        decision=first,
        security_id="FIGI-BB1234",
        role=ActionRole.ENTRY,
        side=OrderSide.BUY,
        requested_quantity=Decimal("10"),
    )
    action_b = build_action_intent(
        decision=restarted,
        security_id="FIGI-BB1234",
        role=ActionRole.ENTRY,
        side=OrderSide.BUY,
        requested_quantity=Decimal("10"),
    )
    assert action_a.logical_action_id == action_b.logical_action_id
    assert_same_logical_action(action_a, action_b)
    linked_opening_action = replace(action_a, holding_episode_id="holding-created-after-first-fill")
    assert linked_opening_action.logical_action_id == action_a.logical_action_id
    assert linked_opening_action.immutable_payload() == action_a.immutable_payload()

    conflicting_snapshot = _decision("b" * 64)
    with pytest.raises(DecisionConflictError, match="decision slot"):
        assert_same_decision_slot_consistent(first, conflicting_snapshot)

    conflicting_action = build_action_intent(
        decision=restarted,
        security_id="FIGI-BB1234",
        role=ActionRole.ENTRY,
        side=OrderSide.BUY,
        requested_quantity=Decimal("11"),
    )
    with pytest.raises(IdentityConflictError, match="logical action"):
        assert_same_logical_action(action_a, conflicting_action)


def test_decision_slots_separate_subjects_but_reject_refreshes_of_one_subject() -> None:
    first = _decision()
    another_security = _decision(subject_id="FIGI-CC5678")
    another_category = _decision(
        category=DecisionCategory.CAPACITY,
        subject_type=DecisionSubjectType.PORTFOLIO,
        subject_id="portfolio-1",
    )
    assert first.decision_slot_id != another_security.decision_slot_id
    assert first.decision_slot_id != another_category.decision_slot_id

    refresh_clock = DecisionClock(
        exchange_id="XNYS",
        decision_session=date(2026, 9, 30),
        as_of_cutoff_at=datetime(2026, 9, 30, 20, 0, tzinfo=UTC),
        next_execution_session=date(2026, 10, 1),
        account_valuation_session=date(2026, 10, 1),
        account_valuation_at=datetime(2026, 10, 1, 13, 31, tzinfo=UTC),
    )
    refreshed = _decision(clock=refresh_clock)
    assert first.decision_slot_id == refreshed.decision_slot_id
    with pytest.raises(DecisionConflictError, match="decision slot"):
        assert_same_decision_slot_consistent(first, refreshed)


def test_decision_clock_requires_aware_times_and_missed_actions_are_not_retargeted() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        DecisionClock(
            exchange_id="XNYS",
            decision_session=date(2026, 9, 30),
            as_of_cutoff_at=datetime(2026, 9, 30, 20, 0),
            next_execution_session=date(2026, 10, 1),
            account_valuation_session=date(2026, 10, 1),
            account_valuation_at=datetime(2026, 10, 1, 13, 30, tzinfo=UTC),
        )

    action = build_action_intent(
        decision=_decision(),
        security_id="FIGI-BB1234",
        role=ActionRole.ENTRY,
        side=OrderSide.BUY,
        requested_quantity=Decimal("10"),
    )
    assert_execution_session(action, date(2026, 10, 1))
    with pytest.raises(MissedExecutionSessionError, match="missed"):
        assert_execution_session(action, date(2026, 10, 2))


def test_a_later_decision_cannot_create_a_second_intent_for_an_unresolved_tier() -> None:
    holding = HoldingEpisode.open(
        deployment_generation_id=_deployment().deployment_generation_id,
        security_id="FIGI-BB1234",
        symbol="ACME",
        opening_action_id="entry-logical-action",
        initial_filled_quantity=Decimal("125"),
        entry_price=Decimal("50"),
    )
    first = build_action_intent(
        decision=_decision("e" * 64),
        security_id="FIGI-BB1234",
        holding_episode_id=holding.holding_episode_id,
        role=ActionRole.SCALE_OUT,
        side=OrderSide.SELL,
        requested_quantity=Decimal("50"),
        exit_tier=1,
        snapshot_original_quantity=Decimal("125"),
        fraction_of_original_quantity=Decimal("0.4"),
        rounding_rule_id="whole_share_floor_v1",
    )
    next_day_clock = DecisionClock(
        exchange_id="XNYS",
        decision_session=date(2026, 10, 1),
        as_of_cutoff_at=datetime(2026, 10, 1, 20, 0, tzinfo=UTC),
        next_execution_session=date(2026, 10, 2),
        account_valuation_session=date(2026, 10, 2),
        account_valuation_at=datetime(2026, 10, 2, 13, 30, tzinfo=UTC),
    )
    duplicate = build_action_intent(
        decision=_decision("f" * 64, clock=next_day_clock),
        security_id="FIGI-BB1234",
        holding_episode_id=holding.holding_episode_id,
        role=ActionRole.SCALE_OUT,
        side=OrderSide.SELL,
        requested_quantity=Decimal("40"),
        exit_tier=1,
        snapshot_original_quantity=Decimal("100"),
        fraction_of_original_quantity=Decimal("0.4"),
        rounding_rule_id="whole_share_floor_v1",
    )

    assert first.logical_action_id == duplicate.logical_action_id
    with pytest.raises(IdentityConflictError, match="logical action"):
        assert_same_logical_action(first, duplicate)


def test_cancel_request_is_pending_and_only_one_remainder_attempt_can_follow() -> None:
    holding = HoldingEpisode.open(
        deployment_generation_id=_deployment().deployment_generation_id,
        security_id="FIGI-BB1234",
        symbol="ACME",
        opening_action_id="entry-logical-action",
        initial_filled_quantity=Decimal("100"),
        entry_price=Decimal("50"),
    )
    intent = build_action_intent(
        decision=_decision(),
        security_id="FIGI-BB1234",
        holding_episode_id=holding.holding_episode_id,
        role=ActionRole.SCALE_OUT,
        side=OrderSide.SELL,
        requested_quantity=Decimal("50"),
        exit_tier=1,
        snapshot_original_quantity=Decimal("100"),
        fraction_of_original_quantity=Decimal("0.5"),
        rounding_rule_id="whole_share_floor_v1",
    )
    partial = apply_attempt_cumulative_fill(intent, 1, Decimal("20"))
    cancelling = request_attempt_cancel(partial, 1)

    assert cancelling.status is ActionStatus.CANCEL_REQUESTED
    assert cancelling.order_attempts[0].status is ActionAttemptStatus.CANCEL_REQUESTED
    assert project_action_state(cancelling).residual_quantity == Decimal("30")
    with pytest.raises(ValueError, match="terminal"):
        create_single_remainder_attempt(cancelling)
    with pytest.raises(ValueError, match="fully confirmed|explicitly resolved"):
        advance_holding_exit_tier(holding, cancelling)

    first_terminal = confirm_attempt_terminal(
        cancelling,
        1,
        status=ActionAttemptStatus.CANCELLED,
    )
    assert first_terminal.status is ActionStatus.REMAINDER_READY
    with pytest.raises(ValueError, match="one remainder"):
        twice = create_single_remainder_attempt(first_terminal)
        create_single_remainder_attempt(twice)

    remainder = create_single_remainder_attempt(first_terminal)
    assert remainder.logical_action_id == intent.logical_action_id
    assert len(remainder.order_attempts) == 2
    assert remainder.order_attempts[1].requested_quantity == Decimal("30")
    remainder = bind_attempt_order_refs(
        remainder,
        attempt_number=2,
        client_order_id="client-remainder",
        broker_order_id="broker-remainder",
    )
    remainder = apply_attempt_cumulative_fill(remainder, 2, Decimal("20"))
    remainder = request_attempt_cancel(remainder, 2)
    second_terminal = confirm_attempt_terminal(
        remainder,
        2,
        status=ActionAttemptStatus.CANCELLED,
    )
    assert second_terminal.status is ActionStatus.PARTIAL_INCOMPLETE

    reconciled_holding = apply_action_fill_to_holding(holding, second_terminal)
    resolved = resolve_action(
        second_terminal,
        status=ActionStatus.RESOLVED,
        resolution_reason="Recorded synthetic terminal-underfill reconciliation",
    )
    assert advance_holding_exit_tier(reconciled_holding, resolved).last_exit_tier == 1


def test_stop_proposal_is_distinct_from_broker_confirmed_protection() -> None:
    holding = HoldingEpisode.open(
        deployment_generation_id=_deployment().deployment_generation_id,
        security_id="FIGI-BB1234",
        symbol="ACME",
        opening_action_id="entry-logical-action",
        initial_filled_quantity=Decimal("100"),
        entry_price=Decimal("50"),
    )
    marked = update_holding_marks(
        holding,
        valuation_at=datetime(2026, 10, 1, 13, 30, tzinfo=UTC),
        peak_price=Decimal("60"),
    )
    proposed, stop_intent = propose_stop_update(
        marked,
        decision=_decision(category=DecisionCategory.EXIT, subject_type=DecisionSubjectType.HOLDING),
        stop_price=Decimal("48"),
    )
    assert proposed.proposed_stop_price == Decimal("48")
    assert proposed.confirmed_protective_stop_price is None

    confirmed = confirm_protective_stop(
        proposed,
        intent=stop_intent,
        stop_price=Decimal("48"),
        client_order_id="stop-client-1",
        broker_order_id="stop-broker-1",
        observed_at=datetime(2026, 10, 1, 13, 31, tzinfo=UTC),
    )
    assert confirmed.confirmed_protective_stop_price == Decimal("48")
    assert confirmed.confirmed_stop_broker_order_id == "stop-broker-1"
    assert confirmed.confirmed_stop_observed_at == datetime(2026, 10, 1, 13, 31, tzinfo=UTC)


def test_projection_reserves_only_unfilled_buy_quantity_and_preserves_unknown_price() -> None:
    intent = build_action_intent(
        decision=_decision(),
        security_id="FIGI-BB1234",
        role=ActionRole.ENTRY,
        side=OrderSide.BUY,
        requested_quantity=Decimal("10"),
        reservation_price=Decimal("100"),
        reservation_price_basis="limit_price",
        risk_per_unit=Decimal("2.5"),
        risk_basis="entry_to_policy_stop",
        reservation_stop_price=Decimal("97.5"),
    )
    partially_filled = apply_cumulative_fill(intent, Decimal("4"))
    partially_filled = bind_attempt_order_refs(
        partially_filled,
        attempt_number=1,
        client_order_id="client-1",
        broker_order_id="broker-1",
    )
    projection = project_action_state(partially_filled)

    assert projection.requested_quantity == Decimal("10")
    assert projection.confirmed_quantity == Decimal("4")
    assert projection.residual_quantity == Decimal("6")
    assert projection.reservation_amount == Decimal("600")
    assert projection.residual_committed_risk == Decimal("15.0")
    assert projection.risk_basis == "entry_to_policy_stop"
    assert projection.reservation_stop_price == Decimal("97.5")
    assert projection.order_attempts[0].client_order_id == "client-1"
    assert projection.order_attempts[0].broker_order_id == "broker-1"
    assert projection.decision_session == date(2026, 9, 30)
    assert projection.next_execution_session == date(2026, 10, 1)
    assert projection.account_valuation_session == date(2026, 10, 1)
    assert projection.account_valuation_at == datetime(2026, 10, 1, 13, 30, tzinfo=UTC)
    assert projection.deployment_identity == _deployment()

    unknown_price = project_action_state(
        build_action_intent(
            decision=_decision(),
            security_id="FIGI-BB1234",
            role=ActionRole.ENTRY,
            side=OrderSide.BUY,
            requested_quantity=Decimal("10"),
        )
    )
    assert unknown_price.residual_quantity == Decimal("10")
    assert unknown_price.reservation_amount is None
    assert unknown_price.residual_committed_risk is None
    assert unknown_price.risk_basis is None


def test_additions_partial_scale_outs_and_fill_replays_preserve_the_frozen_tier_basis() -> None:
    opening = build_action_intent(
        decision=_decision(),
        security_id="FIGI-BB1234",
        role=ActionRole.ENTRY,
        side=OrderSide.BUY,
        requested_quantity=Decimal("100"),
    )
    holding = HoldingEpisode.open(
        deployment_generation_id=_deployment().deployment_generation_id,
        security_id="FIGI-BB1234",
        symbol="ACME",
        opening_action_id=opening.logical_action_id,
        initial_filled_quantity=Decimal("100"),
        entry_price=Decimal("50"),
    )

    addition = build_action_intent(
        decision=_decision("c" * 64),
        security_id="FIGI-BB1234",
        holding_episode_id=holding.holding_episode_id,
        role=ActionRole.ADDITION,
        side=OrderSide.BUY,
        requested_quantity=Decimal("25"),
        risk_per_unit=Decimal("2"),
        risk_basis="confirmed_stop_distance",
    )
    addition_fill = apply_cumulative_fill(addition, Decimal("25"))
    holding = apply_action_fill_to_holding(
        holding,
        addition_fill,
        incremental_fill_notional=Decimal("1375"),
        incremental_fees=Decimal("0"),
    )
    replayed_holding = apply_action_fill_to_holding(holding, addition_fill)
    assert replayed_holding.remaining_quantity == Decimal("125")
    assert replayed_holding.completed_additions_quantity == Decimal("25")
    assert replayed_holding.initial_filled_quantity == Decimal("100")
    assert replayed_holding.addition_count == 1
    assert replayed_holding.cost_basis == Decimal("51")

    tier_one = build_action_intent(
        decision=_decision("d" * 64),
        security_id="FIGI-BB1234",
        holding_episode_id=holding.holding_episode_id,
        role=ActionRole.SCALE_OUT,
        side=OrderSide.SELL,
        requested_quantity=Decimal("50"),
        exit_tier=1,
        snapshot_original_quantity=Decimal("125"),
        fraction_of_original_quantity=Decimal("0.4"),
        rounding_rule_id="whole_share_floor_v1",
    )
    partial_tier = apply_cumulative_fill(tier_one, Decimal("20"))
    partially_sold = apply_action_fill_to_holding(holding, partial_tier)
    assert partially_sold.remaining_quantity == Decimal("105")
    assert partially_sold.last_exit_tier == 0
    with pytest.raises(ValueError, match="fully confirmed|explicitly resolved"):
        advance_holding_exit_tier(partially_sold, partial_tier)

    completed_tier = apply_cumulative_fill(partial_tier, Decimal("50"))
    after_fill = apply_action_fill_to_holding(partially_sold, completed_tier)
    replayed_after_fill = apply_action_fill_to_holding(after_fill, completed_tier)
    assert after_fill.remaining_quantity == Decimal("75")
    assert replayed_after_fill.remaining_quantity == Decimal("75")
    advanced = advance_holding_exit_tier(after_fill, completed_tier)
    assert advanced.last_exit_tier == 1


def test_later_opening_order_fills_change_shares_without_becoming_an_addition() -> None:
    opening = build_action_intent(
        decision=_decision(),
        security_id="FIGI-BB1234",
        role=ActionRole.ENTRY,
        side=OrderSide.BUY,
        requested_quantity=Decimal("100"),
    )
    holding = HoldingEpisode.open(
        deployment_generation_id=_deployment().deployment_generation_id,
        security_id="FIGI-BB1234",
        symbol="ACME",
        opening_action_id=opening.logical_action_id,
        initial_filled_quantity=Decimal("40"),
        entry_price=Decimal("50"),
    )
    later_entry_fill = apply_cumulative_fill(opening, Decimal("60"))
    updated = apply_action_fill_to_holding(
        holding,
        later_entry_fill,
        incremental_fill_notional=Decimal("1100"),
        incremental_fees=Decimal("0"),
    )

    assert updated.initial_filled_quantity == Decimal("40")
    assert updated.remaining_quantity == Decimal("60")
    assert updated.completed_additions_quantity == Decimal("0")
    assert updated.addition_count == 0
    assert updated.cost_basis == Decimal("51.66666666666666666666666667")


def test_cancelled_underfill_needs_explicit_resolution_before_tier_advances() -> None:
    holding = HoldingEpisode.open(
        deployment_generation_id=_deployment().deployment_generation_id,
        security_id="FIGI-BB1234",
        symbol="ACME",
        opening_action_id="entry-logical-action",
        initial_filled_quantity=Decimal("100"),
        entry_price=Decimal("50"),
    )
    action = build_action_intent(
        decision=_decision(),
        security_id="FIGI-BB1234",
        holding_episode_id=holding.holding_episode_id,
        role=ActionRole.SCALE_OUT,
        side=OrderSide.SELL,
        requested_quantity=Decimal("25"),
        exit_tier=1,
        snapshot_original_quantity=Decimal("100"),
        fraction_of_original_quantity=Decimal("0.25"),
        rounding_rule_id="whole_share_floor_v1",
    )
    underfilled = apply_cumulative_fill(action, Decimal("10"))
    cancel_requested = request_attempt_cancel(underfilled, 1)
    cancelled = confirm_attempt_terminal(
        cancel_requested,
        1,
        status=ActionAttemptStatus.CANCELLED,
    )
    holding_after_cancel = apply_action_fill_to_holding(holding, cancelled)
    with pytest.raises(ValueError, match="fully confirmed|explicitly resolved"):
        advance_holding_exit_tier(holding_after_cancel, cancelled)

    resolved = resolve_action(
        cancelled,
        status=ActionStatus.RESOLVED,
        resolution_reason="Synthetic reconciliation confirms no further fill is due",
    )
    projection = project_action_state(resolved)
    assert projection.resolution_reason == "Synthetic reconciliation confirms no further fill is due"
    advanced = advance_holding_exit_tier(holding_after_cancel, resolved)
    assert advanced.last_exit_tier == 1
    assert advanced.remaining_quantity == Decimal("90")


@pytest.mark.parametrize("fill_api", [apply_cumulative_fill, lambda action, quantity: apply_attempt_cumulative_fill(action, 1, quantity)])
def test_partial_fill_preserves_cancel_and_reconciliation_uncertainty(fill_api) -> None:
    action = build_action_intent(
        decision=_decision(),
        security_id="FIGI-BB1234",
        role=ActionRole.ENTRY,
        side=OrderSide.BUY,
        requested_quantity=Decimal("10"),
    )
    partial = fill_api(action, Decimal("4"))
    cancel_pending = request_attempt_cancel(partial, 1)
    still_cancel_pending = fill_api(cancel_pending, Decimal("6"))
    assert still_cancel_pending.confirmed_filled_quantity == Decimal("6")
    assert still_cancel_pending.status is ActionStatus.CANCEL_REQUESTED
    assert still_cancel_pending.order_attempts[0].status is ActionAttemptStatus.CANCEL_REQUESTED

    attempt = still_cancel_pending.order_attempts[0]
    explicitly_reconciling = replace(
        still_cancel_pending,
        status=ActionStatus.RECONCILIATION_REQUIRED,
        order_attempts=(replace(attempt, status=ActionAttemptStatus.RECONCILIATION_REQUIRED),),
    )
    still_reconciling = fill_api(explicitly_reconciling, Decimal("7"))
    assert still_reconciling.confirmed_filled_quantity == Decimal("7")
    assert still_reconciling.status is ActionStatus.RECONCILIATION_REQUIRED
    assert still_reconciling.order_attempts[0].status is ActionAttemptStatus.RECONCILIATION_REQUIRED


def test_late_terminal_fill_requires_reconciliation_before_remainder_and_keeps_issued_target() -> None:
    holding = HoldingEpisode.open(
        deployment_generation_id=_deployment().deployment_generation_id,
        security_id="FIGI-BB1234",
        symbol="ACME",
        opening_action_id="entry-logical-action",
        initial_filled_quantity=Decimal("100"),
        entry_price=Decimal("50"),
    )
    action = build_action_intent(
        decision=_decision(),
        security_id="FIGI-BB1234",
        holding_episode_id=holding.holding_episode_id,
        role=ActionRole.SCALE_OUT,
        side=OrderSide.SELL,
        requested_quantity=Decimal("50"),
        exit_tier=1,
        snapshot_original_quantity=Decimal("100"),
        fraction_of_original_quantity=Decimal("0.5"),
        rounding_rule_id="whole_share_floor_v1",
    )
    underfilled = apply_cumulative_fill(action, Decimal("20"))
    terminal = confirm_attempt_terminal(
        request_attempt_cancel(underfilled, 1),
        1,
        status=ActionAttemptStatus.CANCELLED,
    )

    before_remainder = apply_cumulative_fill(terminal, Decimal("25"))
    assert before_remainder.status is ActionStatus.RECONCILIATION_REQUIRED
    assert before_remainder.confirmed_filled_quantity == Decimal("25")
    assert before_remainder.order_attempts[0].terminal_status is ActionAttemptStatus.CANCELLED
    with pytest.raises(ValueError, match="broker-confirmed terminal"):
        create_single_remainder_attempt(before_remainder)

    already_issued = create_single_remainder_attempt(terminal)
    assert already_issued.order_attempts[1].requested_quantity == Decimal("30")
    late_fill_a = apply_cumulative_fill(already_issued, Decimal("25"))
    late_fill_b = apply_attempt_cumulative_fill(already_issued, 1, Decimal("25"))
    assert late_fill_a == late_fill_b
    assert late_fill_a.confirmed_filled_quantity == Decimal("25")
    assert late_fill_a.status is ActionStatus.RECONCILIATION_REQUIRED
    assert late_fill_a.order_attempts[0].terminal_status is ActionAttemptStatus.CANCELLED
    assert late_fill_a.order_attempts[1].requested_quantity == Decimal("30")

    holding_after_late_fill = apply_action_fill_to_holding(holding, late_fill_a)
    first_terminal_again = confirm_attempt_terminal(
        late_fill_a,
        1,
        status=ActionAttemptStatus.CANCELLED,
    )
    assert first_terminal_again.status is ActionStatus.RECONCILIATION_REQUIRED
    assert first_terminal_again.order_attempts[1].status is ActionAttemptStatus.INTENDED
    with pytest.raises(ValueError, match="order attempts must be terminal"):
        resolve_action(
            first_terminal_again,
            status=ActionStatus.RESOLVED,
            resolution_reason="Remainder submission was verified and terminal facts are needed",
        )
    with pytest.raises(ValueError, match="fully confirmed|explicitly resolved"):
        advance_holding_exit_tier(holding_after_late_fill, first_terminal_again)

    exact_target_live = apply_attempt_cumulative_fill(late_fill_a, 2, Decimal("25"))
    assert exact_target_live.confirmed_filled_quantity == Decimal("50")
    assert exact_target_live.status is ActionStatus.RECONCILIATION_REQUIRED
    assert exact_target_live.order_attempts[1].requested_quantity == Decimal("30")
    assert exact_target_live.order_attempts[1].status is ActionAttemptStatus.PARTIALLY_FILLED
    with pytest.raises(ValueError, match="order attempts must be terminal"):
        resolve_action(
            request_attempt_cancel(exact_target_live, 2),
            status=ActionStatus.RESOLVED,
            resolution_reason="A cancel request is not terminal evidence",
        )
    second_terminal = confirm_attempt_terminal(
        request_attempt_cancel(exact_target_live, 2),
        2,
        status=ActionAttemptStatus.CANCELLED,
    )
    assert second_terminal.status is ActionStatus.RECONCILIATION_REQUIRED
    final_terminal = confirm_attempt_terminal(
        second_terminal,
        1,
        status=ActionAttemptStatus.CANCELLED,
    )
    assert final_terminal.status is ActionStatus.FILLED
    holding_at_target = apply_action_fill_to_holding(holding_after_late_fill, exact_target_live)
    assert advance_holding_exit_tier(holding_at_target, final_terminal).last_exit_tier == 1

    over_target = apply_attempt_cumulative_fill(late_fill_a, 2, Decimal("30"))
    assert over_target.confirmed_filled_quantity == Decimal("55")
    assert over_target.residual_quantity == Decimal("0")
    assert over_target.status is ActionStatus.RECONCILIATION_REQUIRED
    holding_after_over_target = apply_action_fill_to_holding(holding_after_late_fill, over_target)
    terminal_over_target = confirm_attempt_terminal(
        over_target,
        1,
        status=ActionAttemptStatus.CANCELLED,
    )
    assert terminal_over_target.status is ActionStatus.RECONCILIATION_REQUIRED
    assert all(
        attempt.status in {ActionAttemptStatus.CANCELLED, ActionAttemptStatus.REJECTED, ActionAttemptStatus.FILLED}
        for attempt in terminal_over_target.order_attempts
    )
    resolved_over_target = resolve_action(
        terminal_over_target,
        status=ActionStatus.RESOLVED,
        resolution_reason="Synthetic broker terminal facts confirm 55 filled against the 50-share target",
    )
    assert resolved_over_target.requested_quantity == Decimal("50")
    assert resolved_over_target.confirmed_filled_quantity == Decimal("55")
    assert resolved_over_target.resolution_reason is not None
    assert advance_holding_exit_tier(holding_after_over_target, resolved_over_target).last_exit_tier == 1


def test_single_attempt_over_target_fill_can_resolve_without_changing_the_target() -> None:
    action = build_action_intent(
        decision=_decision(),
        security_id="FIGI-BB1234",
        role=ActionRole.ENTRY,
        side=OrderSide.BUY,
        requested_quantity=Decimal("10"),
    )
    observed = apply_cumulative_fill(action, Decimal("11"))
    assert observed.requested_quantity == Decimal("10")
    assert observed.confirmed_filled_quantity == Decimal("11")
    assert observed.status is ActionStatus.RECONCILIATION_REQUIRED

    terminal = confirm_attempt_terminal(
        observed,
        1,
        status=ActionAttemptStatus.FILLED,
    )
    assert terminal.status is ActionStatus.RECONCILIATION_REQUIRED
    resolved = resolve_action(
        terminal,
        status=ActionStatus.RESOLVED,
        resolution_reason="Synthetic terminal evidence retains 11 actual shares against a 10-share target",
    )
    assert resolved.requested_quantity == Decimal("10")
    assert resolved.confirmed_filled_quantity == Decimal("11")
    assert resolved.status is ActionStatus.RESOLVED


def test_stop_and_peak_evolve_monotonically_without_turning_missing_values_into_zero() -> None:
    holding = HoldingEpisode.open(
        deployment_generation_id=_deployment().deployment_generation_id,
        security_id="FIGI-BB1234",
        symbol="ACME",
        opening_action_id="entry-logical-action",
        initial_filled_quantity=Decimal("100"),
        entry_price=None,
    )
    marked = update_holding_marks(
        holding,
        valuation_at=datetime(2026, 9, 30, 20, 0, tzinfo=UTC),
        peak_price=Decimal("55"),
    )
    marked, stop_intent = propose_stop_update(
        marked,
        decision=_decision(category=DecisionCategory.EXIT, subject_type=DecisionSubjectType.HOLDING),
        stop_price=Decimal("45"),
    )
    improved = update_holding_marks(
        marked,
        valuation_at=datetime(2026, 10, 1, 20, 0, tzinfo=UTC),
        peak_price=Decimal("60"),
    )

    assert improved.entry_price is None
    assert improved.proposed_stop_price == Decimal("45")
    assert improved.confirmed_protective_stop_price is None
    assert improved.peak_price == Decimal("60")
    confirmed = confirm_protective_stop(
        improved,
        intent=stop_intent,
        stop_price=Decimal("50"),
        client_order_id="stop-client-1",
        broker_order_id="stop-broker-1",
        observed_at=datetime(2026, 10, 1, 13, 31, tzinfo=UTC),
    )
    assert confirmed.confirmed_protective_stop_price == Decimal("50")
    with pytest.raises(ValueError, match="monotonic"):
        propose_stop_update(
            confirmed,
            decision=_decision("c" * 64, category=DecisionCategory.EXIT, subject_type=DecisionSubjectType.HOLDING),
            stop_price=Decimal("48"),
        )
    next_proposal, _ = propose_stop_update(
        confirmed,
        decision=_decision("b" * 64, category=DecisionCategory.EXIT, subject_type=DecisionSubjectType.HOLDING),
        stop_price=Decimal("51"),
    )
    assert next_proposal.proposed_stop_price == Decimal("51")
    with pytest.raises(ValueError, match="monotonic"):
        update_holding_marks(
            improved,
            valuation_at=datetime(2026, 10, 2, 20, 0, tzinfo=UTC),
            peak_price=Decimal("59"),
        )


def test_portfolio_state_keeps_pending_commitments_outside_cash_plus_gross_equity() -> None:
    snapshot = PortfolioStateSnapshot(
        deployment_identity=_deployment(),
        clock=_clock(),
        source_namespace="synthetic-account-snapshot-v1",
        account_snapshot_id="snapshot-2026-10-01-open",
        equity=Decimal("10000"),
        cash=Decimal("9000"),
        gross_exposure=Decimal("1000"),
        open_risk=Decimal("100"),
        portfolio_peak_equity=Decimal("10200"),
        last_accepted_session=date(2026, 9, 30),
        policy_flags=(("entry_mode", "fixture"),),
    )
    assert snapshot.equity == snapshot.cash + snapshot.gross_exposure
    assert snapshot.open_risk == Decimal("100")
    assert snapshot.policy_flags == (("entry_mode", "fixture"),)
    with pytest.raises(ValueError, match="cash plus gross exposure"):
        PortfolioStateSnapshot(
            deployment_identity=_deployment(),
            clock=_clock(),
            source_namespace="synthetic-account-snapshot-v1",
            account_snapshot_id="bad-snapshot",
            equity=Decimal("10000"),
            cash=Decimal("7400"),
            gross_exposure=Decimal("1000"),
            open_risk=None,
            portfolio_peak_equity=None,
            last_accepted_session=None,
        )
