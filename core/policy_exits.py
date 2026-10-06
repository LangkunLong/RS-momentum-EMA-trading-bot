from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
from typing import Callable, Mapping

from core.policy_execution_state import (
    ActionIntent,
    ActionRole,
    DecisionCategory,
    DecisionIdentity,
    DecisionSubjectType,
    OrderSide,
    StopUpdateIntent,
    build_action_intent,
)
from core.policy_execution_store import ConcurrentStateUpdateError, PolicyExecutionStateStore


_EXIT_KIND = "policy_holding_exit_v1"
_RESIZE_KIND = "policy_protective_resize_v1"


@dataclass(frozen=True, slots=True)
class ProtectionResizePlan:
    """Durable request to resize the existing stop to the holding's current shares."""

    decision: DecisionIdentity
    holding_episode_id: str
    remaining_quantity: Decimal
    stop_price: Decimal
    stop_intent: StopUpdateIntent
    replaces_client_order_id: str
    replaces_broker_order_id: str


def _positive_decimal(value: Decimal, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be a finite positive Decimal")
    return value


def _validate_decision(decision: DecisionIdentity, holding, *, subject_id: str) -> None:
    if decision.category is not DecisionCategory.EXIT:
        raise ValueError("holding exit decisions must use the exit category")
    if decision.subject_type is not DecisionSubjectType.HOLDING or decision.subject_id != subject_id:
        raise ValueError("exit decision must identify the same holding episode")
    if decision.deployment_generation_id != holding.deployment_generation_id:
        raise ValueError("exit decision must use the holding's opening generation")


def _action_payload(
    action: ActionIntent,
    *,
    expected_holding_version: int,
    quantity_increment: Decimal | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": _EXIT_KIND,
        "version": 1,
        "action_id": action.logical_action_id,
        "action": {**action.immutable_payload(), "broker_symbol": action.broker_symbol},
        "expected_holding_version": expected_holding_version,
    }
    if quantity_increment is not None:
        payload["quantity_increment"] = str(quantity_increment)
    return payload


def _action_from_payload(decision: DecisionIdentity, payload: Mapping[str, object]) -> ActionIntent:
    if payload.get("kind") != _EXIT_KIND or payload.get("version") != 1:
        raise ValueError("decision does not contain a supported durable holding-exit intention")
    action_payload = payload.get("action")
    if not isinstance(action_payload, Mapping):
        raise ValueError("holding-exit intention is missing its immutable action facts")
    action = build_action_intent(
        decision=decision,
        security_id=str(action_payload["security_id"]),
        broker_symbol=str(action_payload["broker_symbol"]),
        role=ActionRole(str(action_payload["role"])),
        side=OrderSide(str(action_payload["side"])),
        requested_quantity=Decimal(str(action_payload["requested_quantity"])),
        holding_episode_id=(None if action_payload.get("holding_episode_id") is None else str(action_payload["holding_episode_id"])),
        exit_tier=None if action_payload.get("exit_tier") is None else int(action_payload["exit_tier"]),
        snapshot_original_quantity=(
            None if action_payload.get("snapshot_original_quantity") is None
            else Decimal(str(action_payload["snapshot_original_quantity"]))
        ),
        fraction_of_original_quantity=(
            None if action_payload.get("fraction_of_original_quantity") is None
            else Decimal(str(action_payload["fraction_of_original_quantity"]))
        ),
        rounding_rule_id=None if action_payload.get("rounding_rule_id") is None else str(action_payload["rounding_rule_id"]),
    )
    if action.logical_action_id != payload.get("action_id"):
        raise ValueError("stored holding-exit action identity does not match its immutable facts")
    return action


def _replay_action(
    store: PolicyExecutionStateStore,
    decision: DecisionIdentity,
    *,
    policy_payload: Mapping[str, object],
    guard_payload: Mapping[str, object],
    validate_action: Callable[[ActionIntent, Mapping[str, object]], None],
) -> ActionIntent | None:
    try:
        record = store.load_decision_record(decision.decision_id)
    except KeyError:
        return None
    if record.decision != decision:
        raise ValueError("stored decision identity differs from the requested holding exit")
    if dict(record.policy_payload) != dict(policy_payload) or dict(record.guard_payload) != dict(guard_payload):
        raise ValueError("replayed holding-exit decision changed its policy or guard facts")
    effective = record.effective_action_payload
    if not isinstance(effective, Mapping):
        raise ValueError("stored holding-exit decision has invalid effective action facts")
    action = _action_from_payload(decision, effective)
    validate_action(action, effective)
    try:
        current = store.load_action_intent(action.logical_action_id)
    except KeyError:
        expected_version = effective.get("expected_holding_version")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise ValueError("durable holding-exit intention has no valid holding version") from None
        store.record_action_intent(
            action,
            expected_version=None,
            expected_holding_version=expected_version,
        )
        return store.load_action_intent(action.logical_action_id)
    if current.immutable_payload() != action.immutable_payload() or current.broker_symbol != action.broker_symbol:
        raise ValueError("logical holding-exit action already has different immutable facts")
    return current


def _ensure_action(
    store: PolicyExecutionStateStore,
    decision: DecisionIdentity,
    *,
    effective_payload: Mapping[str, object],
    policy_payload: Mapping[str, object],
    guard_payload: Mapping[str, object],
) -> ActionIntent:
    store.record_decision(
        decision,
        policy_payload=policy_payload,
        guard_payload=guard_payload,
        effective_action_payload=effective_payload,
    )
    action = _action_from_payload(decision, effective_payload)
    try:
        current = store.load_action_intent(action.logical_action_id)
    except KeyError:
        expected_version = effective_payload.get("expected_holding_version")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise ValueError("durable holding-exit intention has no valid holding version") from None
        store.record_action_intent(
            action,
            expected_version=None,
            expected_holding_version=expected_version,
        )
        return store.load_action_intent(action.logical_action_id)
    if current.immutable_payload() != action.immutable_payload() or current.broker_symbol != action.broker_symbol:
        raise ValueError("logical holding-exit action already has different immutable facts")
    return current


def start_scale_out(
    store: PolicyExecutionStateStore,
    *,
    decision: DecisionIdentity,
    holding_episode_id: str,
    fraction_of_original_quantity: Decimal,
    quantity_increment: Decimal,
    rounding_rule_id: str,
    expected_holding_version: int,
    policy_payload: Mapping[str, object],
    guard_payload: Mapping[str, object],
) -> ActionIntent:
    """Persist one deterministic scale-out tier, capped by actual remaining shares."""
    fraction = _positive_decimal(fraction_of_original_quantity, "fraction_of_original_quantity")
    increment = _positive_decimal(quantity_increment, "quantity_increment")
    if fraction >= 1:
        raise ValueError("scale-out fractions must be below one; use a full-exit action to close all shares")
    if rounding_rule_id not in {"whole_share_floor_v1", "increment_floor_v1"}:
        raise ValueError("unsupported deterministic exit quantity rounding rule")
    if rounding_rule_id == "whole_share_floor_v1" and increment != Decimal("1"):
        raise ValueError("whole_share_floor_v1 requires a one-share quantity increment")

    def validate_replay(action: ActionIntent, payload: Mapping[str, object]) -> None:
        if (
            action.role is not ActionRole.SCALE_OUT
            or action.holding_episode_id != holding_episode_id
            or action.fraction_of_original_quantity != fraction
            or action.rounding_rule_id != rounding_rule_id
            or payload.get("quantity_increment") != str(increment)
        ):
            raise ValueError("replayed scale-out decision changed its holding, fraction, or rounding facts")

    replayed = _replay_action(
        store,
        decision,
        policy_payload=policy_payload,
        guard_payload=guard_payload,
        validate_action=validate_replay,
    )
    if replayed is not None:
        return replayed

    holding = store.load_holding_episode(holding_episode_id)
    _validate_decision(decision, holding, subject_id=holding_episode_id)
    if not isinstance(expected_holding_version, int) or isinstance(expected_holding_version, bool):
        raise ValueError("expected_holding_version must be an integer")
    if holding.state_version != expected_holding_version:
        raise ConcurrentStateUpdateError("holding changed since the scale-out decision was prepared")
    original_quantity = (
        holding.initial_filled_quantity
        + holding.opening_later_fills_quantity
        + holding.completed_additions_quantity
    )
    target_from_original = (original_quantity * fraction / increment).to_integral_value(rounding=ROUND_DOWN) * increment
    target_from_remaining = (holding.remaining_quantity / increment).to_integral_value(rounding=ROUND_DOWN) * increment
    target = min(target_from_original, target_from_remaining)
    if target <= 0:
        raise ValueError("scale-out decision rounds to no shares in the actual remaining position")
    action = build_action_intent(
        decision=decision,
        security_id=holding.security_id,
        broker_symbol=holding.broker_symbol,
        holding_episode_id=holding_episode_id,
        role=ActionRole.SCALE_OUT,
        side=OrderSide.SELL,
        requested_quantity=target,
        exit_tier=holding.last_exit_tier + 1,
        snapshot_original_quantity=original_quantity,
        fraction_of_original_quantity=fraction,
        rounding_rule_id=rounding_rule_id,
    )
    effective = _action_payload(
        action,
        expected_holding_version=expected_holding_version,
        quantity_increment=increment,
    )
    return _ensure_action(
        store,
        decision,
        effective_payload=effective,
        policy_payload=policy_payload,
        guard_payload=guard_payload,
    )


def start_full_exit(
    store: PolicyExecutionStateStore,
    *,
    decision: DecisionIdentity,
    holding_episode_id: str,
    expected_holding_version: int,
    policy_payload: Mapping[str, object],
    guard_payload: Mapping[str, object],
) -> ActionIntent:
    """Persist a full-exit action for exactly the shares currently remaining."""
    def validate_replay(action: ActionIntent, _: Mapping[str, object]) -> None:
        if action.role is not ActionRole.CLOSE or action.holding_episode_id != holding_episode_id:
            raise ValueError("replayed full-exit decision changed its holding or action role")

    replayed = _replay_action(
        store,
        decision,
        policy_payload=policy_payload,
        guard_payload=guard_payload,
        validate_action=validate_replay,
    )
    if replayed is not None:
        return replayed

    holding = store.load_holding_episode(holding_episode_id)
    _validate_decision(decision, holding, subject_id=holding_episode_id)
    if not isinstance(expected_holding_version, int) or isinstance(expected_holding_version, bool):
        raise ValueError("expected_holding_version must be an integer")
    if holding.state_version != expected_holding_version:
        raise ConcurrentStateUpdateError("holding changed since the full-exit decision was prepared")
    if holding.remaining_quantity <= 0:
        raise ValueError("a zero-share holding cannot receive a full-exit order")
    action = build_action_intent(
        decision=decision,
        security_id=holding.security_id,
        broker_symbol=holding.broker_symbol,
        holding_episode_id=holding_episode_id,
        role=ActionRole.CLOSE,
        side=OrderSide.SELL,
        requested_quantity=holding.remaining_quantity,
    )
    effective = _action_payload(action, expected_holding_version=expected_holding_version)
    return _ensure_action(
        store,
        decision,
        effective_payload=effective,
        policy_payload=policy_payload,
        guard_payload=guard_payload,
    )


def _resize_intent(decision: DecisionIdentity, holding_episode_id: str, stop_price: Decimal) -> StopUpdateIntent:
    return StopUpdateIntent(
        deployment_generation_id=decision.deployment_generation_id,
        decision_id=decision.decision_id,
        holding_episode_id=holding_episode_id,
        requested_stop_price=stop_price,
    )


def propose_protection_resize(
    store: PolicyExecutionStateStore,
    *,
    decision: DecisionIdentity,
    holding_episode_id: str,
    stop_price: Decimal,
    expected_holding_version: int,
    policy_payload: Mapping[str, object],
    guard_payload: Mapping[str, object],
    observed_at: datetime,
) -> ProtectionResizePlan:
    """Persist a stop replacement sized from the holding's current remaining quantity."""
    price = _positive_decimal(stop_price, "stop_price")
    try:
        record = store.load_decision_record(decision.decision_id)
    except KeyError:
        record = None

    if record is not None:
        if record.decision != decision or dict(record.policy_payload) != dict(policy_payload):
            raise ValueError("replayed protection resize changed its decision or policy facts")
        if dict(record.guard_payload) != dict(guard_payload):
            raise ValueError("replayed protection resize changed its guard facts")
        effective = record.effective_action_payload
        if (
            effective.get("kind") != _RESIZE_KIND
            or effective.get("version") != 1
            or effective.get("holding_episode_id") != holding_episode_id
            or effective.get("stop_price") != str(price)
        ):
            raise ValueError("replayed protection resize changed its holding or stop facts")
        quantity = Decimal(str(effective["remaining_quantity"]))
        expected_version = effective["expected_holding_version"]
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise ValueError("stored protection resize has no valid holding version")
        stop_intent = _resize_intent(decision, holding_episode_id, price)
        try:
            persisted_intent = store.load_stop_update_intent(stop_intent.logical_action_id)
        except KeyError:
            holding = store.load_holding_episode(holding_episode_id)
            _validate_decision(decision, holding, subject_id=holding_episode_id)
            if holding.state_version != expected_version or holding.remaining_quantity != quantity:
                raise ConcurrentStateUpdateError("holding changed before the durable stop resize could be proposed") from None
            persisted_intent = store.propose_stop_update(
                holding_episode_id,
                decision=decision,
                stop_price=price,
                expected_holding_version=expected_version,
                observed_at=observed_at,
            )
        current = store.load_holding_episode(holding_episode_id)
        if current.remaining_quantity != quantity:
            raise ConcurrentStateUpdateError("holding quantity changed after the protective resize was prepared")
        if persisted_intent != stop_intent:
            raise ValueError("stored protective resize intent differs from the fixed decision")
        client_id = effective.get("replaces_client_order_id")
        broker_id = effective.get("replaces_broker_order_id")
        if not isinstance(client_id, str) or not isinstance(broker_id, str):
            raise ValueError("stored protective resize has no prior stop order references")
        return ProtectionResizePlan(decision, holding_episode_id, quantity, price, persisted_intent, client_id, broker_id)

    holding = store.load_holding_episode(holding_episode_id)
    _validate_decision(decision, holding, subject_id=holding_episode_id)
    if not isinstance(expected_holding_version, int) or isinstance(expected_holding_version, bool):
        raise ValueError("expected_holding_version must be an integer")
    if holding.state_version != expected_holding_version:
        raise ConcurrentStateUpdateError("holding changed since the protective resize was prepared")
    if holding.remaining_quantity <= 0:
        raise ValueError("a zero-share holding does not need a protective stop")
    if not holding.confirmed_stop_client_order_id or not holding.confirmed_stop_broker_order_id:
        raise ValueError("a protective resize requires a confirmed existing stop order")
    effective = {
        "kind": _RESIZE_KIND,
        "version": 1,
        "holding_episode_id": holding_episode_id,
        "remaining_quantity": str(holding.remaining_quantity),
        "stop_price": str(price),
        "expected_holding_version": expected_holding_version,
        "replaces_client_order_id": holding.confirmed_stop_client_order_id,
        "replaces_broker_order_id": holding.confirmed_stop_broker_order_id,
    }
    store.record_decision(
        decision,
        policy_payload=policy_payload,
        guard_payload=guard_payload,
        effective_action_payload=effective,
    )
    stop_intent = store.propose_stop_update(
        holding_episode_id,
        decision=decision,
        stop_price=price,
        expected_holding_version=expected_holding_version,
        observed_at=observed_at,
    )
    current = store.load_holding_episode(holding_episode_id)
    if current.remaining_quantity != holding.remaining_quantity:
        raise ConcurrentStateUpdateError("holding quantity changed after the protective resize was prepared")
    return ProtectionResizePlan(
        decision=decision,
        holding_episode_id=holding_episode_id,
        remaining_quantity=holding.remaining_quantity,
        stop_price=price,
        stop_intent=stop_intent,
        replaces_client_order_id=holding.confirmed_stop_client_order_id,
        replaces_broker_order_id=holding.confirmed_stop_broker_order_id,
    )
