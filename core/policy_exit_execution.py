"""One-use dispatch of a durable policy exit through an injected paper broker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Callable, Literal

from core.policy_execution_state import (
    ActionAttemptStatus,
    ActionIntent,
    ActionRole,
    ActionStatus,
    DecisionCategory,
    DecisionIdentity,
    DecisionSubjectType,
    OrderSide,
    assert_execution_session,
)
from core.policy_execution_store import PolicyExecutionStateStore
from core.policy_exits import propose_protection_resize
from core.strategy_policy.account_reconciliation import BrokerAccountSnapshot, BrokerOrderFact

if TYPE_CHECKING:
    from core.fake_policy_exit_broker import FakeProtectedExitBroker


_WORKING_STATUSES = frozenset(
    {"accepted", "accepted_for_bidding", "new", "partially_filled", "pending_new", "submitted"}
)


@dataclass(frozen=True, slots=True)
class PolicyExitSubmissionResult:
    """Typed response from the explicitly supplied paper-broker operation."""

    success: bool
    broker_order_id: str | None = None
    linked_stop_broker_order_id: str | None = None
    shared_sell_quantity_limit: Decimal | None = None
    shared_sell_group_id: str | None = None
    outcome_uncertain: bool = False
    error: str = ""

    def __post_init__(self) -> None:
        if type(self.success) is not bool or type(self.outcome_uncertain) is not bool:
            raise TypeError("exit submission outcome flags must be boolean")
        if self.broker_order_id is not None and (
            type(self.broker_order_id) is not str or not self.broker_order_id.strip()
        ):
            raise ValueError("broker_order_id must be non-empty when supplied")
        for name in ("linked_stop_broker_order_id", "shared_sell_group_id"):
            value = getattr(self, name)
            if value is not None and (type(value) is not str or not value.strip()):
                raise ValueError(f"{name} must be non-empty when supplied")
        if self.shared_sell_quantity_limit is not None and (
            not isinstance(self.shared_sell_quantity_limit, Decimal)
            or not self.shared_sell_quantity_limit.is_finite()
            or self.shared_sell_quantity_limit <= 0
        ):
            raise ValueError("shared_sell_quantity_limit must be a positive finite Decimal")
        if type(self.error) is not str:
            raise TypeError("exit submission error must be text")


@dataclass(frozen=True, slots=True)
class PolicyExitDispatch:
    disposition: Literal["submitted", "reconcile", "rejected", "complete", "blocked"]
    action: ActionIntent
    reason: str = ""


def _client_order_id(action: ActionIntent) -> str:
    return "policy-exit-" + action.logical_action_id.rsplit(":", 1)[-1][:24]


def _account_preflight(
    account: BrokerAccountSnapshot,
    action: ActionIntent,
    holding,
) -> str | None:
    clock = action.decision.clock
    if (
        account.paper_account_environment_id
        != action.decision.deployment_identity.paper_account_environment_id
        or account.decision_slot_id != action.decision.decision_slot_id
        or account.decision_id != action.decision.decision_id
        or account.clock.completed_session != clock.decision_session
        or account.clock.next_execution_session != clock.next_execution_session
    ):
        return "account observation does not belong to the fixed policy decision"
    if account.positions is None or account.open_orders is None or not account.account_snapshot_id:
        return "complete broker positions, orders and snapshot identity are required"
    positions = tuple(row for row in account.positions if row.symbol == action.broker_symbol)
    if len(positions) != 1 or Decimal(str(positions[0].quantity)) != holding.remaining_quantity:
        return "broker position does not match the durable remaining quantity"
    if holding.proposed_stop_action_id not in {None, holding.confirmed_stop_action_id}:
        return "a protective stop replacement is unresolved"
    if not (
        holding.confirmed_stop_client_order_id
        and holding.confirmed_stop_broker_order_id
        and holding.confirmed_protective_stop_price is not None
    ):
        return "a broker-confirmed protective stop is required"
    stops = tuple(
        row
        for row in account.open_orders
        if row.purpose == "protective_stop"
        and row.client_order_id == holding.confirmed_stop_client_order_id
        and row.broker_order_id == holding.confirmed_stop_broker_order_id
    )
    if len(stops) != 1:
        return "the confirmed protective stop is not uniquely observed at the broker"
    stop = stops[0]
    if (
        stop.symbol != action.broker_symbol
        or stop.side != "sell"
        or stop.holding_episode_id != holding.holding_episode_id
        or stop.status not in _WORKING_STATUSES
        or Decimal(str(stop.requested_quantity)) != holding.remaining_quantity
        or Decimal(str(stop.cumulative_filled_quantity)) != 0
        or stop.stop_price is None
        or Decimal(str(stop.stop_price)) != holding.confirmed_protective_stop_price
    ):
        return "broker protective-stop facts differ from confirmed durable protection"
    if _other_working_sell(account, action, stop):
        return "another sell is already working for the held symbol"
    return None


def dispatch_policy_exit(
    store: PolicyExecutionStateStore,
    logical_action_id: str,
    *,
    account: BrokerAccountSnapshot,
    execution_session: date,
    provider_id: str,
    observed_at: datetime,
    broker: FakeProtectedExitBroker,
) -> PolicyExitDispatch:
    """Persist a client reference before one fake-broker sell; replay reconciles.

    This route has no default broker. The injected operation receives the exact
    confirmed stop references so its paper-broker implementation can enforce
    the sell/stop relationship. A later broker observation must confirm fills,
    cancellation, and protection replacement; submission alone does not.
    """
    if not isinstance(store, PolicyExecutionStateStore):
        raise TypeError("policy exit dispatch requires an explicit state store")
    if type(account) is not BrokerAccountSnapshot:
        raise TypeError("policy exit dispatch requires a BrokerAccountSnapshot")
    if type(provider_id) is not str or not provider_id.strip():
        raise ValueError("provider_id must be non-empty")
    if not isinstance(observed_at, datetime) or observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    from core.fake_policy_exit_broker import FakeProtectedExitBroker

    if type(broker) is not FakeProtectedExitBroker:
        raise TypeError("policy exit dispatch requires the atomic fake-paper broker")
    if broker.receipt_store_path is None:
        raise ValueError("policy exit dispatch requires a durable fake-broker receipt store")

    action = store.load_action_intent(logical_action_id)
    if action.role not in {ActionRole.SCALE_OUT, ActionRole.CLOSE} or action.side is not OrderSide.SELL:
        raise ValueError("policy exit dispatch requires a durable scale-out or close sell")
    assert_execution_session(action, execution_session)
    if action.status is ActionStatus.FILLED:
        return PolicyExitDispatch("complete", action)
    if action.status is ActionStatus.RESOLVED:
        return PolicyExitDispatch("rejected", action, action.resolution_reason or "resolved without dispatch")
    if action.status is not ActionStatus.INTENDED:
        return PolicyExitDispatch("reconcile", action, "issued policy sell requires broker reconciliation")

    holding = store.load_holding_episode(action.holding_episode_id)
    if (
        holding.deployment_generation_id != action.deployment_generation_id
        or holding.security_id != action.security_id
        or holding.broker_symbol != action.broker_symbol
        or action.requested_quantity > holding.remaining_quantity
        or action.logical_action_id not in holding.pending_action_ids
        or any(item != action.logical_action_id for item in holding.pending_action_ids)
    ):
        return PolicyExitDispatch("blocked", action, "holding ownership or quantity changed before sell")
    guard = _account_preflight(account, action, holding)
    if guard is not None:
        return PolicyExitDispatch("blocked", action, guard)

    projection = store.load_action_projection(logical_action_id)
    client_id = _client_order_id(action)
    store.bind_attempt_order_refs(
        logical_action_id,
        1,
        provider_id=provider_id,
        client_order_id=client_id,
        expected_action_version=projection.state_version,
        observed_at=observed_at,
    )
    try:
        outcome = broker.submit_protected_exit(
            symbol=action.broker_symbol,
            quantity=action.requested_quantity,
            client_order_id=client_id,
            confirmed_stop_client_order_id=holding.confirmed_stop_client_order_id,
            confirmed_stop_broker_order_id=holding.confirmed_stop_broker_order_id,
            protected_position_quantity=holding.remaining_quantity,
        )
    except Exception as exc:  # Broker call may have happened; never resubmit this attempt.
        return PolicyExitDispatch(
            "reconcile", store.load_action_intent(logical_action_id), f"sell outcome uncertain: {exc}"
        )
    if type(outcome) is not PolicyExitSubmissionResult:
        return PolicyExitDispatch(
            "reconcile", store.load_action_intent(logical_action_id), "sell outcome is untyped"
        )
    if outcome.broker_order_id:
        current = store.load_action_projection(logical_action_id)
        store.bind_attempt_order_refs(
            logical_action_id,
            1,
            provider_id=provider_id,
            broker_order_id=outcome.broker_order_id,
            expected_action_version=current.state_version,
            observed_at=observed_at,
        )
    if outcome.outcome_uncertain or (outcome.success and not outcome.broker_order_id) or (
        not outcome.success and outcome.broker_order_id is not None
    ):
        return PolicyExitDispatch(
            "reconcile", store.load_action_intent(logical_action_id), outcome.error or "sell outcome uncertain"
        )
    if outcome.success:
        if (
            outcome.linked_stop_broker_order_id != holding.confirmed_stop_broker_order_id
            or outcome.shared_sell_quantity_limit != holding.remaining_quantity
            or not outcome.shared_sell_group_id
        ):
            return PolicyExitDispatch(
                "reconcile",
                store.load_action_intent(logical_action_id),
                "broker did not confirm a shared position cap with the existing stop",
            )
        return PolicyExitDispatch("submitted", store.load_action_intent(logical_action_id))

    current = store.load_action_projection(logical_action_id)
    terminal = store.confirm_order_terminal(
        logical_action_id,
        1,
        terminal_status=ActionAttemptStatus.REJECTED,
        expected_action_version=current.state_version,
        observed_at=observed_at,
    )
    holding = store.load_holding_episode(action.holding_episode_id)
    store.record_explicit_action_resolution(
        logical_action_id,
        resolution_reason=outcome.error or "broker definitively rejected the sell without a fill",
        expected_action_version=terminal.state_version,
        expected_holding_version=holding.state_version,
        observed_at=observed_at,
    )
    return PolicyExitDispatch("rejected", store.load_action_intent(logical_action_id), outcome.error)


@dataclass(frozen=True, slots=True)
class PolicyExitCancel:
    disposition: Literal["cancelled", "reconcile", "blocked"]
    action: ActionIntent
    reason: str = ""


def confirm_policy_exit_cancel(
    store: PolicyExecutionStateStore,
    logical_action_id: str,
    *,
    broker: FakeProtectedExitBroker,
    observed_at: datetime,
) -> PolicyExitCancel:
    """Resolve a partial sell only after exact terminal broker and position evidence."""
    from core.fake_policy_exit_broker import FakeProtectedExitBroker

    if type(broker) is not FakeProtectedExitBroker or broker.receipt_store_path is None:
        raise TypeError("policy exit cancel requires a durable atomic fake-paper broker")
    action = store.load_action_intent(logical_action_id)
    if action.role not in {ActionRole.SCALE_OUT, ActionRole.CLOSE} or action.side is not OrderSide.SELL:
        raise ValueError("policy exit cancel requires a fixed sell")
    if action.status is ActionStatus.RESOLVED:
        return PolicyExitCancel("cancelled", action)
    if action.status not in {ActionStatus.CANCEL_REQUESTED, ActionStatus.REMAINDER_READY}:
        return PolicyExitCancel("blocked", action, "sell has no pending partial remainder cancel")
    holding = store.load_holding_episode(action.holding_episode_id)
    account = broker.snapshot()
    if not broker.confirms_cancelled_remainder(action):
        return PolicyExitCancel("reconcile", action, "exact terminal fake-broker cancel evidence is unavailable")
    stops = tuple(
        row for row in account.open_orders
        if row.broker_order_id == holding.confirmed_stop_broker_order_id
        and row.client_order_id == holding.confirmed_stop_client_order_id
        and row.status in _WORKING_STATUSES
    )
    if (
        len(stops) != 1
        or stops[0].holding_episode_id != holding.holding_episode_id
        or Decimal(str(stops[0].requested_quantity)) < holding.remaining_quantity
        or action.logical_action_id not in holding.pending_action_ids
        or any(item != action.logical_action_id for item in holding.pending_action_ids)
        or _other_working_sell(account, action, stops[0])
        or any(row.symbol == action.broker_symbol and Decimal(str(row.quantity)) != holding.remaining_quantity
               for row in account.positions)
        or len(tuple(row for row in account.positions if row.symbol == action.broker_symbol)) != 1
    ):
        return PolicyExitCancel("reconcile", action, "broker position or confirmed stop differs from partial sell")
    if action.status is ActionStatus.CANCEL_REQUESTED:
        projection = store.load_action_projection(logical_action_id)
        store.confirm_order_terminal(
            logical_action_id,
            1,
            terminal_status=ActionAttemptStatus.CANCELLED,
            expected_action_version=projection.state_version,
            expected_holding_version=holding.state_version,
            observed_at=observed_at,
        )
        holding = store.load_holding_episode(action.holding_episode_id)
    projection = store.load_action_projection(logical_action_id)
    store.record_explicit_action_resolution(
        logical_action_id,
        resolution_reason=f"partial exit terminal cancel and position confirmed by {account.account_snapshot_id}",
        expected_action_version=projection.state_version,
        expected_holding_version=holding.state_version,
        observed_at=observed_at,
    )
    return PolicyExitCancel("cancelled", store.load_action_intent(logical_action_id))


def cancel_policy_exit_remainder(
    store: PolicyExecutionStateStore,
    logical_action_id: str,
    *,
    broker: FakeProtectedExitBroker,
    observed_at: datetime,
) -> PolicyExitCancel:
    """Persist the cancel intent before a one-use fake-broker cancel call."""
    from core.fake_policy_exit_broker import FakeProtectedExitBroker

    if type(broker) is not FakeProtectedExitBroker or broker.receipt_store_path is None:
        raise TypeError("policy exit cancel requires a durable atomic fake-paper broker")
    action = store.load_action_intent(logical_action_id)
    if action.role not in {ActionRole.SCALE_OUT, ActionRole.CLOSE} or action.side is not OrderSide.SELL:
        raise ValueError("policy exit cancel requires a fixed sell")
    if action.status in {ActionStatus.CANCEL_REQUESTED, ActionStatus.REMAINDER_READY, ActionStatus.RESOLVED}:
        return confirm_policy_exit_cancel(store, logical_action_id, broker=broker, observed_at=observed_at)
    if action.status is not ActionStatus.PARTIALLY_FILLED or action.confirmed_filled_quantity <= 0:
        return PolicyExitCancel("blocked", action, "only a confirmed partial sell can cancel its remainder")
    attempt = action.order_attempts[0]
    if not attempt.broker_order_id or not attempt.client_order_id:
        return PolicyExitCancel("blocked", action, "partial sell lacks fixed broker order references")
    projection = store.load_action_projection(logical_action_id)
    store.request_order_cancel(
        logical_action_id, 1,
        expected_action_version=projection.state_version,
        observed_at=observed_at,
    )
    try:
        broker.cancel_sell_remainder(attempt.broker_order_id)
    except Exception as exc:
        return PolicyExitCancel("reconcile", store.load_action_intent(logical_action_id), f"sell cancel outcome uncertain: {exc}")
    return confirm_policy_exit_cancel(store, logical_action_id, broker=broker, observed_at=observed_at)


@dataclass(frozen=True, slots=True)
class PolicyExitProtection:
    disposition: Literal["protected", "reconcile", "blocked", "flat"]
    action: ActionIntent
    reason: str = ""


def _position_matches(account: BrokerAccountSnapshot, action: ActionIntent, remaining: Decimal) -> bool:
    if account.positions is None or account.open_orders is None or not account.account_snapshot_id:
        return False
    positions = tuple(row for row in account.positions if row.symbol == action.broker_symbol)
    return len(positions) == 1 and Decimal(str(positions[0].quantity)) == remaining


def _active_stops(account: BrokerAccountSnapshot, holding_episode_id: str) -> tuple[BrokerOrderFact, ...]:
    if account.open_orders is None:
        return ()
    return tuple(
        row
        for row in account.open_orders
        if row.purpose == "protective_stop"
        and row.holding_episode_id == holding_episode_id
        and row.status in _WORKING_STATUSES
    )


def _other_working_sell(account: BrokerAccountSnapshot, action: ActionIntent, allowed: BrokerOrderFact) -> bool:
    return any(
        row is not allowed
        and row.symbol == action.broker_symbol
        and row.side == "sell"
        and row.status in _WORKING_STATUSES
        for row in account.open_orders
    )


def _flat_exit_converged(account: BrokerAccountSnapshot, action: ActionIntent) -> bool:
    if (
        account.paper_account_environment_id
        != action.decision.deployment_identity.paper_account_environment_id
        or account.positions is None
        or account.open_orders is None
        or not account.account_snapshot_id
    ):
        return False
    if any(row.symbol == action.broker_symbol and Decimal(str(row.quantity)) != 0 for row in account.positions):
        return False
    return not any(
        row.symbol == action.broker_symbol
        and row.side == "sell"
        and row.status in _WORKING_STATUSES
        for row in account.open_orders
    )


def _retire_flat_protection(
    store: PolicyExecutionStateStore,
    action: ActionIntent,
    holding,
    account: BrokerAccountSnapshot,
    observed_at: datetime,
    broker: FakeProtectedExitBroker | None,
) -> PolicyExitProtection:
    if not _flat_exit_converged(account, action):
        return PolicyExitProtection("reconcile", action, "flat holding still requires broker stop cancellation evidence")
    flags = dict(holding.policy_flags)
    client = holding.confirmed_stop_client_order_id or flags.get("flat_stop_retired_client_order_id")
    stop_broker_id = holding.confirmed_stop_broker_order_id or flags.get("flat_stop_retired_broker_order_id")
    if not client or not stop_broker_id:
        return PolicyExitProtection("blocked", action, "flat holding has no durable stop references to retire")
    if broker is None or not broker.confirms_flat_exit(action, stop_broker_id):
        return PolicyExitProtection("reconcile", action, "flat account lacks this broker's terminal sell/stop evidence")
    try:
        store.retire_flat_holding_protection(
            action.logical_action_id,
            client_order_id=client,
            broker_order_id=stop_broker_id,
            account_snapshot_id=account.account_snapshot_id,
            expected_holding_version=holding.state_version,
            observed_at=observed_at,
        )
    except ValueError as exc:
        return PolicyExitProtection("reconcile", action, str(exc))
    return PolicyExitProtection("flat", action, "broker confirms no position or working sell")


def confirm_policy_exit_protection(
    store: PolicyExecutionStateStore,
    logical_action_id: str,
    *,
    account: BrokerAccountSnapshot | None = None,
    broker: FakeProtectedExitBroker | None = None,
    provider_id: str,
    observed_at: datetime,
) -> PolicyExitProtection:
    """Confirm a proposed resized stop only from a unique current broker order."""
    if broker is not None:
        from core.fake_policy_exit_broker import FakeProtectedExitBroker

        if type(broker) is not FakeProtectedExitBroker:
            raise TypeError("flat policy exit confirmation requires the atomic fake-paper broker")
        account = broker.snapshot()
    if type(account) is not BrokerAccountSnapshot:
        raise TypeError("protection confirmation requires a typed broker account observation")
    action = store.load_action_intent(logical_action_id)
    if action.role not in {ActionRole.SCALE_OUT, ActionRole.CLOSE} or action.side is not OrderSide.SELL:
        raise ValueError("protection confirmation requires a durable policy sell")
    if action.confirmed_filled_quantity <= 0 or action.status not in {ActionStatus.FILLED, ActionStatus.RESOLVED}:
        return PolicyExitProtection("reconcile", action, "source sell is not terminal with a confirmed fill")
    holding = store.load_holding_episode(action.holding_episode_id)
    if holding.remaining_quantity <= 0:
        return _retire_flat_protection(store, action, holding, account, observed_at, broker)
    if (
        account.paper_account_environment_id
        != action.decision.deployment_identity.paper_account_environment_id
        or not _position_matches(account, action, holding.remaining_quantity)
    ):
        return PolicyExitProtection("reconcile", action, "broker position has not converged to confirmed exit fills")
    active = _active_stops(account, holding.holding_episode_id)
    if holding.proposed_stop_action_id == holding.confirmed_stop_action_id:
        if (
            len(active) == 1
            and not _other_working_sell(account, action, active[0])
            and active[0].client_order_id == holding.confirmed_stop_client_order_id
            and active[0].broker_order_id == holding.confirmed_stop_broker_order_id
            and Decimal(str(active[0].requested_quantity)) == holding.remaining_quantity
            and active[0].stop_price is not None
            and Decimal(str(active[0].stop_price)) == holding.confirmed_protective_stop_price
        ):
            return PolicyExitProtection("protected", action)
        return PolicyExitProtection("reconcile", action, "confirmed stop does not cover exactly the remaining shares")
    if holding.proposed_stop_action_id is None:
        return PolicyExitProtection("blocked", action, "no durable exit stop replacement exists")
    intent = store.load_stop_update_intent(holding.proposed_stop_action_id)
    record = store.load_decision_record(intent.decision_id)
    if (
        record.effective_action_payload.get("kind") != "policy_protective_resize_v1"
        or record.policy_payload.get("source_action_id") != logical_action_id
        or Decimal(str(record.guard_payload.get("remaining_quantity", "-1"))) != holding.remaining_quantity
        or intent.holding_episode_id != holding.holding_episode_id
        or intent.requested_stop_price != holding.confirmed_protective_stop_price
    ):
        return PolicyExitProtection("blocked", action, "proposed stop is not bound to this exit and quantity")
    if len(active) != 1:
        return PolicyExitProtection("reconcile", action, "replacement is not the unique active protective stop")
    stop = active[0]
    if _other_working_sell(account, action, stop):
        return PolicyExitProtection("reconcile", action, "another sell is working beside the replacement stop")
    if (
        stop.side != "sell"
        or stop.symbol != holding.broker_symbol
        or Decimal(str(stop.requested_quantity)) != holding.remaining_quantity
        or Decimal(str(stop.cumulative_filled_quantity)) != 0
        or stop.stop_price is None
        or Decimal(str(stop.stop_price)) != intent.requested_stop_price
        or not stop.client_order_id
        or not stop.broker_order_id
        or stop.client_order_id == holding.confirmed_stop_client_order_id
        or stop.broker_order_id == holding.confirmed_stop_broker_order_id
    ):
        return PolicyExitProtection("reconcile", action, "replacement stop facts are incomplete or changed")
    store.confirm_protective_stop(
        intent,
        stop_price=intent.requested_stop_price,
        client_order_id=stop.client_order_id,
        broker_order_id=stop.broker_order_id,
        observed_at=observed_at,
        expected_holding_version=holding.state_version,
        provider_id=provider_id,
        requested_quantity=holding.remaining_quantity,
    )
    return PolicyExitProtection("protected", action)


def replace_policy_exit_protection(
    store: PolicyExecutionStateStore,
    logical_action_id: str,
    *,
    decision: DecisionIdentity,
    account: BrokerAccountSnapshot,
    provider_id: str,
    observed_at: datetime,
    replace_stop: Callable[..., BrokerAccountSnapshot | None],
) -> PolicyExitProtection:
    """Request an atomic stop resize after a confirmed terminal policy sell."""
    action = store.load_action_intent(logical_action_id)
    if action.role not in {ActionRole.SCALE_OUT, ActionRole.CLOSE} or action.side is not OrderSide.SELL:
        raise ValueError("protection resize requires a durable policy sell")
    if (
        decision.category is not DecisionCategory.EXIT
        or decision.subject_type is not DecisionSubjectType.HOLDING
        or decision.subject_id != action.holding_episode_id
        or decision.deployment_identity != action.decision.deployment_identity
        or decision.clock != action.decision.clock
        or decision.snapshot_sha256 != action.decision.snapshot_sha256
    ):
        return PolicyExitProtection("blocked", action, "stop decision differs from the source sell")
    if (
        account.decision_slot_id != decision.decision_slot_id
        or account.decision_id != decision.decision_id
        or account.clock.completed_session != decision.clock.decision_session
        or account.clock.next_execution_session != decision.clock.next_execution_session
    ):
        return PolicyExitProtection("blocked", action, "stop observation differs from its fixed decision")
    if action.confirmed_filled_quantity <= 0:
        return PolicyExitProtection("blocked", action, "no confirmed sell fill exists")
    if action.status not in {ActionStatus.FILLED, ActionStatus.RESOLVED}:
        return PolicyExitProtection("reconcile", action, "sell remainder is not terminal")
    holding = store.load_holding_episode(action.holding_episode_id)
    if holding.remaining_quantity <= 0:
        return _retire_flat_protection(store, action, holding, account, observed_at, None)
    if holding.pending_action_ids:
        return PolicyExitProtection("blocked", action, "another holding action is pending")
    if holding.proposed_stop_action_id != holding.confirmed_stop_action_id:
        return PolicyExitProtection("reconcile", action, "prior stop replacement is unresolved")
    if holding.confirmed_protective_stop_price is None:
        return PolicyExitProtection("blocked", action, "holding has no confirmed stop price")
    if (
        account.paper_account_environment_id
        != action.decision.deployment_identity.paper_account_environment_id
        or not _position_matches(account, action, holding.remaining_quantity)
    ):
        return PolicyExitProtection("blocked", action, "complete current broker position is required")
    active = _active_stops(account, holding.holding_episode_id)
    if len(active) != 1:
        return PolicyExitProtection("blocked", action, "one active old protective stop is required")
    old = active[0]
    if (
        old.client_order_id != holding.confirmed_stop_client_order_id
        or old.broker_order_id != holding.confirmed_stop_broker_order_id
        or old.symbol != holding.broker_symbol
        or old.side != "sell"
        or old.stop_price is None
        or Decimal(str(old.stop_price)) != holding.confirmed_protective_stop_price
        or Decimal(str(old.requested_quantity)) < holding.remaining_quantity
        or Decimal(str(old.cumulative_filled_quantity)) != 0
    ):
        return PolicyExitProtection("blocked", action, "old broker stop differs from durable protection")
    if _other_working_sell(account, action, old):
        return PolicyExitProtection("reconcile", action, "another sell is still working")
    if Decimal(str(old.requested_quantity)) == holding.remaining_quantity:
        return PolicyExitProtection("protected", action)
    try:
        plan = propose_protection_resize(
            store,
            decision=decision,
            holding_episode_id=holding.holding_episode_id,
            stop_price=holding.confirmed_protective_stop_price,
            expected_holding_version=holding.state_version,
            policy_payload={"source_action_id": logical_action_id, "reason": "confirmed_exit_fill"},
            guard_payload={
                "remaining_quantity": str(holding.remaining_quantity),
                "account_snapshot_id": account.account_snapshot_id,
            },
            observed_at=observed_at,
        )
    except (KeyError, ValueError) as exc:
        return PolicyExitProtection("blocked", action, str(exc))
    if plan.disposition == "confirmed":
        return PolicyExitProtection("protected", action)
    if plan.disposition == "reconcile":
        return PolicyExitProtection("reconcile", action, "stop replacement already issued")
    new_client = "policy-stop-" + plan.stop_intent.logical_action_id.rsplit(":", 1)[-1][:24]
    try:
        refreshed = replace_stop(
            old_order=old,
            symbol=holding.broker_symbol,
            quantity=plan.remaining_quantity,
            stop_price=plan.stop_price,
            new_client_order_id=new_client,
        )
    except Exception as exc:
        return PolicyExitProtection("reconcile", action, f"stop exchange outcome uncertain: {exc}")
    if refreshed is None:
        return PolicyExitProtection("reconcile", action, "stop exchange outcome uncertain")
    if type(refreshed) is not BrokerAccountSnapshot:
        return PolicyExitProtection("reconcile", action, "stop exchange returned no typed broker observation")
    return confirm_policy_exit_protection(
        store,
        logical_action_id,
        account=refreshed,
        provider_id=provider_id,
        observed_at=observed_at,
    )
