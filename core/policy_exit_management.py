"""Persist fixed after-close exit policy state for the offline paper holding."""

from __future__ import annotations

from decimal import Decimal
from hashlib import sha256

from core.policy_execution_state import DecisionCategory, DecisionIdentity, DecisionSubjectType, HoldingEpisode
from core.policy_execution_store import PolicyExecutionStateStore
from core.strategy_policy.contracts import ExitDecision, ExitSnapshot, validate_exit_decision


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
