"""Persist fixed after-close exit policy state for the offline paper holding."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from typing import Literal

from core.fake_policy_exit_broker import FakeProtectedExitBroker
from core.policy_execution_state import DecisionCategory, DecisionIdentity, DecisionSubjectType, HoldingEpisode, StopUpdateIntent
from core.policy_execution_store import PolicyExecutionStateStore, StopUpdateProposalAlreadyClaimedError
from core.strategy_policy.contracts import ExitDecision, ExitSnapshot, validate_exit_decision
from core.strategy_policy.exit import evaluate_exit


def _flag(holding: HoldingEpisode, name: str) -> bool:
    value = dict(holding.policy_flags).get(name, "false")
    if value not in {"true", "false"}:
        raise ValueError(f"durable {name} flag is invalid")
    return value == "true"


def record_fixed_exit_management(
    store: PolicyExecutionStateStore,
    *,
    decision: DecisionIdentity,
    holding_episode_id: str,
    snapshot: ExitSnapshot,
    outcome: ExitDecision,
    expected_holding_version: int,
) -> HoldingEpisode:
    """Atomically retain winner/trailing state under one qualified session decision.

    The selected policy's stop price remains a proposal in its immutable record.
    Broker protection changes only through the separately confirmed stop path.
    """
    if type(snapshot) is not ExitSnapshot or type(outcome) is not ExitDecision:
        raise TypeError("fixed exit management requires typed policy facts")
    validate_exit_decision(snapshot, outcome)
    if evaluate_exit(snapshot) != outcome:
        raise ValueError("fixed exit outcome differs from the selected baseline evaluator")
    if (
        decision.category is not DecisionCategory.EXIT
        or decision.subject_type is not DecisionSubjectType.HOLDING
        or decision.subject_id != holding_episode_id
    ):
        raise ValueError("fixed exit management decision must identify its holding")
    holding = store.load_holding_episode(holding_episode_id)
    if holding.deployment_generation_id != decision.deployment_generation_id:
        raise ValueError("fixed exit management differs from the holding generation")
    original = (
        holding.initial_filled_quantity
        + holding.opening_later_fills_quantity
        + holding.completed_additions_quantity
    )
    prior = dict(holding.policy_flags)
    replay = prior.get("exit_management_decision_id") == decision.decision_id
    if not replay and (
        Decimal(str(snapshot.original_qty)) != original
        or Decimal(str(snapshot.remaining_qty)) != holding.remaining_quantity
        or holding.entry_price is None
        or Decimal(str(snapshot.entry_price)) != holding.entry_price
        or holding.confirmed_protective_stop_price is None
        or Decimal(str(snapshot.stop_price)) != holding.confirmed_protective_stop_price
        or snapshot.scale_out_tier != holding.last_exit_tier
        or snapshot.early_winner_hold != _flag(holding, "early_winner_hold")
        or snapshot.breakeven_armed != _flag(holding, "breakeven_armed")
        or snapshot.ema_trailing_active != _flag(holding, "ema_trailing_active")
    ):
        raise ValueError("fixed exit snapshot differs from durable holding facts")
    snapshot_sha = sha256(snapshot.to_canonical_json().encode("utf-8")).hexdigest()
    policy_payload = {"kind": "fixed_exit_policy_state_v1", "decision": outcome.to_primitive()}
    guard_payload = {
        "snapshot_sha256": snapshot_sha,
        "original_quantity": str(Decimal(str(snapshot.original_qty))),
        "remaining_quantity": str(Decimal(str(snapshot.remaining_qty))),
        "confirmed_stop_price": str(Decimal(str(snapshot.stop_price))),
        "expected_holding_version": expected_holding_version,
    }
    effective_payload = {
        "kind": "exit_holding_management_state_v1",
        "early_winner_hold": outcome.early_winner_hold,
        "breakeven_armed": outcome.breakeven_armed,
        "ema_trailing_active": outcome.ema_trailing_active,
        "selected_scale_out_tier": outcome.scale_out_tier,
        "next_stop_price": outcome.next_stop_price,
        "action_count": len(outcome.actions),
    }
    return store.record_exit_management_state(
        decision,
        holding_episode_id=holding_episode_id,
        policy_payload=policy_payload,
        guard_payload=guard_payload,
        effective_action_payload=effective_payload,
        early_winner_hold=outcome.early_winner_hold,
        breakeven_armed=outcome.breakeven_armed,
        ema_trailing_active=outcome.ema_trailing_active,
        policy_scale_out_tier=outcome.scale_out_tier,
        peak_price=Decimal(str(snapshot.peak_close)),
        expected_holding_version=expected_holding_version,
        observed_at=decision.clock.account_valuation_at,
    )


_WORKING = frozenset({"accepted", "new", "partially_filled", "pending_new", "submitted"})


@dataclass(frozen=True, slots=True)
class FixedExitStopResult:
    disposition: Literal["protected", "unchanged", "reconcile", "blocked"]
    holding: HoldingEpisode
    reason: str = ""


def _selected_stop(store: PolicyExecutionStateStore, decision: DecisionIdentity, holding: HoldingEpisode) -> Decimal | None:
    record = store.load_decision_record(decision.decision_id)
    if (
        record.decision != decision
        or record.policy_payload.get("kind") != "fixed_exit_policy_state_v1"
        or record.effective_action_payload.get("kind") != "exit_holding_management_state_v1"
        or dict(holding.policy_flags).get("exit_management_decision_id") != decision.decision_id
    ):
        raise ValueError("selected stop lacks its exact fixed exit management decision")
    value = record.effective_action_payload.get("next_stop_price")
    policy_decision = record.policy_payload.get("decision")
    if not isinstance(policy_decision, dict) or policy_decision.get("next_stop_price") != value:
        raise ValueError("selected stop differs from its immutable policy outcome")
    return None if value is None else Decimal(str(value))


def _observed_stop(broker: FakeProtectedExitBroker, decision: DecisionIdentity, holding: HoldingEpisode):
    account = broker.snapshot()
    if (
        account.paper_account_environment_id != decision.deployment_identity.paper_account_environment_id
        or account.decision_slot_id != decision.decision_slot_id
        or account.decision_id != decision.decision_id
        or account.clock.completed_session != decision.clock.decision_session
        or account.clock.next_execution_session != decision.clock.next_execution_session
        or not account.account_snapshot_id
        or account.positions is None
        or account.open_orders is None
    ):
        return None
    positions = tuple(row for row in account.positions if row.symbol == holding.broker_symbol)
    stops = tuple(
        row for row in account.open_orders
        if row.symbol == holding.broker_symbol and row.purpose == "protective_stop"
        and row.holding_episode_id == holding.holding_episode_id and row.status in _WORKING
    )
    other_sells = tuple(
        row for row in account.open_orders
        if row.symbol == holding.broker_symbol and row.side == "sell"
        and row.purpose != "protective_stop" and row.status in _WORKING
    )
    if (
        len(positions) != 1 or len(stops) != 1 or other_sells
        or Decimal(str(positions[0].quantity)) != holding.remaining_quantity
        or stops[0].side != "sell"
        or Decimal(str(stops[0].requested_quantity)) != holding.remaining_quantity
        or Decimal(str(stops[0].cumulative_filled_quantity)) != 0
    ):
        return None
    return stops[0]


def confirm_fixed_exit_stop(
    store: PolicyExecutionStateStore,
    *,
    decision: DecisionIdentity,
    broker: FakeProtectedExitBroker,
    provider_id: str,
    observed_at: datetime,
) -> FixedExitStopResult:
    """Confirm only an exact physical stop from the same fake broker after restart."""
    if type(broker) is not FakeProtectedExitBroker or broker.receipt_store_path is None:
        raise TypeError("fixed exit stop requires a durable atomic fake-paper broker")
    holding = store.load_holding_episode(decision.subject_id)
    selected = _selected_stop(store, decision, holding)
    if selected is None:
        return FixedExitStopResult("unchanged", holding)
    stop = _observed_stop(broker, decision, holding)
    if stop is None:
        return FixedExitStopResult("reconcile", holding, "current fake-broker position and stop are incomplete")
    action_id = holding.proposed_stop_action_id
    if action_id is None:
        return FixedExitStopResult("blocked", holding, "selected stop has no durable stop-update intent")
    intent = store.load_stop_update_intent(action_id)
    if intent.decision_id != decision.decision_id or intent.requested_stop_price != selected:
        return FixedExitStopResult("blocked", holding, "stop update is not bound to this fixed policy decision")
    if not broker.confirms_stop_replacement(
        intent,
        old_broker_order_id=(
            None if holding.confirmed_stop_action_id == action_id else holding.confirmed_stop_broker_order_id
        ),
        new_client_order_id=stop.client_order_id,
        new_broker_order_id=stop.broker_order_id,
        symbol=holding.broker_symbol,
        quantity=holding.remaining_quantity,
    ):
        return FixedExitStopResult("reconcile", holding, "exact fake-broker stop replacement receipt is unavailable")
    if holding.confirmed_stop_action_id == action_id:
        if (
            stop.client_order_id == holding.confirmed_stop_client_order_id
            and stop.broker_order_id == holding.confirmed_stop_broker_order_id
            and stop.stop_price is not None
            and Decimal(str(stop.stop_price)) == selected
        ):
            return FixedExitStopResult("protected", holding)
        return FixedExitStopResult("reconcile", holding, "confirmed stop differs from the fake broker")
    new_client = "policy-stop-" + intent.logical_action_id.rsplit(":", 1)[-1][:24]
    if (
        stop.client_order_id != new_client
        or stop.broker_order_id == holding.confirmed_stop_broker_order_id
        or stop.stop_price is None
        or Decimal(str(stop.stop_price)) != selected
    ):
        return FixedExitStopResult("reconcile", holding, "proposed stop has no exact physical replacement")
    confirmed = store.confirm_protective_stop(
        intent,
        stop_price=selected,
        client_order_id=stop.client_order_id,
        broker_order_id=stop.broker_order_id,
        observed_at=observed_at,
        expected_holding_version=holding.state_version,
        provider_id=provider_id,
        requested_quantity=holding.remaining_quantity,
    )
    return FixedExitStopResult("protected", confirmed)


def apply_fixed_exit_stop(
    store: PolicyExecutionStateStore,
    *,
    decision: DecisionIdentity,
    broker: FakeProtectedExitBroker,
    provider_id: str,
    observed_at: datetime,
) -> FixedExitStopResult:
    """Claim a selected stop once, then swap and confirm it through fake paper."""
    if type(broker) is not FakeProtectedExitBroker or broker.receipt_store_path is None:
        raise TypeError("fixed exit stop requires a durable atomic fake-paper broker")
    holding = store.load_holding_episode(decision.subject_id)
    selected = _selected_stop(store, decision, holding)
    if selected is None:
        return FixedExitStopResult("unchanged", holding)
    if holding.confirmed_stop_action_id is not None:
        confirmed_intent = store.load_stop_update_intent(holding.confirmed_stop_action_id)
        if confirmed_intent.decision_id == decision.decision_id:
            return confirm_fixed_exit_stop(
                store, decision=decision, broker=broker, provider_id=provider_id, observed_at=observed_at
            )
    if holding.proposed_stop_action_id != holding.confirmed_stop_action_id:
        return confirm_fixed_exit_stop(
            store, decision=decision, broker=broker, provider_id=provider_id, observed_at=observed_at
        )
    if holding.confirmed_protective_stop_price is None or selected <= holding.confirmed_protective_stop_price:
        return FixedExitStopResult("blocked", holding, "selected stop is not above confirmed protection")
    if holding.pending_action_ids:
        return FixedExitStopResult("blocked", holding, "holding has a pending action before stop replacement")
    old = _observed_stop(broker, decision, holding)
    if (
        old is None
        or old.client_order_id != holding.confirmed_stop_client_order_id
        or old.broker_order_id != holding.confirmed_stop_broker_order_id
        or old.stop_price is None
        or Decimal(str(old.stop_price)) != holding.confirmed_protective_stop_price
    ):
        return FixedExitStopResult("blocked", holding, "fake-broker stop differs from confirmed protection")
    try:
        intent: StopUpdateIntent = store.propose_stop_update(
            holding.holding_episode_id,
            decision=decision,
            stop_price=selected,
            expected_holding_version=holding.state_version,
            observed_at=observed_at,
        )
    except StopUpdateProposalAlreadyClaimedError:
        return confirm_fixed_exit_stop(
            store, decision=decision, broker=broker, provider_id=provider_id, observed_at=observed_at
        )
    new_client = "policy-stop-" + intent.logical_action_id.rsplit(":", 1)[-1][:24]
    try:
        broker.replace_stop(
            old_order=old, symbol=holding.broker_symbol,
            quantity=holding.remaining_quantity, stop_price=selected,
            new_client_order_id=new_client,
        )
    except Exception as exc:
        return FixedExitStopResult(
            "reconcile", store.load_holding_episode(holding.holding_episode_id),
            f"selected stop replacement outcome uncertain: {exc}",
        )
    return confirm_fixed_exit_stop(
        store, decision=decision, broker=broker, provider_id=provider_id, observed_at=observed_at
    )
