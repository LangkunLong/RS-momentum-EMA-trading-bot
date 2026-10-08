"""One-use fake-paper buy and fill-to-stop path for a fixed replacement."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from typing import Callable

from core.fake_policy_replacement_buy_broker import (
    FakeProtectedReplacementBuyBroker,
    ReplacementBuySubmission,
)
from core.policy_execution_state import (
    ActionAttemptStatus,
    ActionRole,
    ActionStatus,
    DecisionCategory,
    DecisionIdentity,
    DecisionSubjectType,
    HoldingEpisode,
    OrderSide,
    PortfolioStateSnapshot,
)
from core.policy_execution_store import PolicyExecutionStateStore
from core.policy_protection_bridge import PolicyProtectionBridge
from core.policy_replacement import (
    ReplacementStep,
    ReplacementStepKind,
    advance_replacement,
    load_replacement_intention,
)
from core.strategy_policy.account_reconciliation import BrokerAccountSnapshot


_ACTIVE = frozenset({"accepted", "new", "partially_filled", "pending_new", "submitted"})


@dataclass(frozen=True, slots=True)
class ReplacementBuyPorts:
    store: PolicyExecutionStateStore
    provider_id: str
    broker: FakeProtectedReplacementBuyBroker
    observed_at: Callable[[], datetime]

    def __post_init__(self) -> None:
        if not isinstance(self.store, PolicyExecutionStateStore):
            raise TypeError("replacement buy requires an explicit policy store")
        if type(self.provider_id) is not str or not self.provider_id.strip():
            raise ValueError("replacement buy provider id is required")
        if type(self.broker) is not FakeProtectedReplacementBuyBroker:
            raise TypeError("replacement buy requires the concrete fake-paper broker")
        if not callable(self.observed_at):
            raise TypeError("replacement buy observed_at must be callable")


@dataclass(frozen=True, slots=True)
class ReplacementBuyDispatch:
    step: ReplacementStep
    submission: ReplacementBuySubmission | None = None


def _observed_at(ports: ReplacementBuyPorts) -> datetime:
    value = ports.observed_at()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("replacement buy observation must be timezone-aware")
    return value


def _client_order_id(logical_action_id: str) -> str:
    return "policy-replacement-buy:" + sha256(logical_action_id.encode("utf-8")).hexdigest()[:24]


def _holding_id(action) -> str:
    return HoldingEpisode.open(
        deployment_generation_id=action.deployment_generation_id,
        security_id=action.security_id,
        symbol=action.broker_symbol,
        broker_symbol=action.broker_symbol,
        opening_action_id=action.logical_action_id,
        initial_filled_quantity=Decimal("1"),
        entry_price=None,
    ).holding_episode_id


def submit_replacement_buy(
    decision_id: str,
    *,
    portfolio_snapshot: PortfolioStateSnapshot,
    ports: ReplacementBuyPorts,
) -> ReplacementBuyDispatch:
    """Revalidate the authorized child against exact fake account and price facts."""
    if type(portfolio_snapshot) is not PortfolioStateSnapshot:
        raise TypeError("replacement buy requires a typed portfolio snapshot")
    execution = load_replacement_intention(ports.store, decision_id)
    account = ports.broker.snapshot()
    price = ports.broker.quote(execution.plan.candidate_symbol)
    step = advance_replacement(
        ports.store,
        decision_id,
        account=account,
        portfolio_snapshot=portfolio_snapshot,
        candidate_price=price,
    )
    if step.kind is not ReplacementStepKind.BUY_DUE:
        return ReplacementBuyDispatch(step)
    action = step.action
    if (
        action is None
        or action.role is not ActionRole.REPLACEMENT
        or action.side is not OrderSide.BUY
        or action.status is not ActionStatus.INTENDED
        or action.reservation_price is None
        or action.reservation_stop_price is None
    ):
        raise ValueError("replacement buy step differs from its persisted fixed action")
    client_id = _client_order_id(action.logical_action_id)
    current = ports.store.load_action_projection(action.logical_action_id)
    ports.store.bind_attempt_order_refs(
        action.logical_action_id,
        1,
        provider_id=ports.provider_id,
        client_order_id=client_id,
        expected_action_version=current.state_version,
        observed_at=_observed_at(ports),
    )
    try:
        result = ports.broker.submit_protected_buy(
            symbol=action.broker_symbol,
            quantity=action.requested_quantity,
            limit_price=action.reservation_price,
            stop_price=action.reservation_stop_price,
            candidate_price=price,
            expected_account_snapshot_id=account.account_snapshot_id,
            expected_account=account,
            client_order_id=client_id,
            holding_episode_id=_holding_id(action),
        )
    except Exception as exc:
        return ReplacementBuyDispatch(
            advance_replacement(ports.store, decision_id),
            ReplacementBuySubmission(False, outcome_uncertain=True, error=str(exc)),
        )
    if type(result) is not ReplacementBuySubmission or result.outcome_uncertain:
        return ReplacementBuyDispatch(advance_replacement(ports.store, decision_id), result)
    if result.broker_order_id:
        current = ports.store.load_action_projection(action.logical_action_id)
        ports.store.bind_attempt_order_refs(
            action.logical_action_id,
            1,
            provider_id=ports.provider_id,
            broker_order_id=result.broker_order_id,
            expected_action_version=current.state_version,
            observed_at=_observed_at(ports),
        )
    if not result.success:
        current = ports.store.load_action_projection(action.logical_action_id)
        terminal = ports.store.confirm_order_terminal(
            action.logical_action_id,
            1,
            terminal_status=ActionAttemptStatus.REJECTED,
            expected_action_version=current.state_version,
            observed_at=_observed_at(ports),
        )
        ports.store.record_explicit_action_resolution(
            action.logical_action_id,
            resolution_reason=result.error or "fake paper broker definitively rejected replacement buy",
            expected_action_version=terminal.state_version,
            observed_at=_observed_at(ports),
        )
    return ReplacementBuyDispatch(advance_replacement(ports.store, decision_id), result)


def reconcile_replacement_buy(decision_id: str, *, ports: ReplacementBuyPorts) -> ReplacementStep:
    """Recover an accepted order by the one durable client reference after restart."""
    execution = load_replacement_intention(ports.store, decision_id)
    buy = execution.buy_action
    if buy is None:
        return advance_replacement(ports.store, decision_id)
    attempt = buy.order_attempts[0]
    if buy.status is not ActionStatus.SUBMITTED or attempt.broker_order_id is not None:
        return advance_replacement(ports.store, decision_id)
    if attempt.client_order_id != _client_order_id(buy.logical_action_id):
        raise ValueError("replacement buy durable client reference differs from fixed action")
    account = ports.broker.snapshot()
    if account.open_orders is None:
        return advance_replacement(ports.store, decision_id)
    matches = tuple(
        row for row in account.open_orders
        if row.client_order_id == attempt.client_order_id
        and row.symbol == buy.broker_symbol
        and row.side == "buy"
        and row.holding_episode_id == _holding_id(buy)
        and Decimal(str(row.requested_quantity)) == buy.requested_quantity
    )
    if len(matches) != 1 or not matches[0].broker_order_id:
        return advance_replacement(ports.store, decision_id)
    projection = ports.store.load_action_projection(buy.logical_action_id)
    ports.store.bind_attempt_order_refs(
        buy.logical_action_id,
        1,
        provider_id=ports.provider_id,
        broker_order_id=matches[0].broker_order_id,
        expected_action_version=projection.state_version,
        observed_at=_observed_at(ports),
    )
    return advance_replacement(ports.store, decision_id)


def _confirmed_stop(account: BrokerAccountSnapshot, action, quantity: Decimal):
    if account.positions is None or account.open_orders is None:
        raise ValueError("replacement buy fill requires complete fake broker facts")
    positions = tuple(row for row in account.positions if row.symbol == action.broker_symbol)
    stops = tuple(
        row for row in account.open_orders
        if row.purpose == "protective_stop"
        and row.symbol == action.broker_symbol
        and row.holding_episode_id == _holding_id(action)
        and row.status in _ACTIVE
    )
    if (
        len(positions) != 1
        or Decimal(str(positions[0].quantity)) != quantity
        or len(stops) != 1
        or stops[0].side != "sell"
        or Decimal(str(stops[0].requested_quantity)) != quantity
        or Decimal(str(stops[0].cumulative_filled_quantity)) != 0
        or stops[0].stop_price is None
        or Decimal(str(stops[0].stop_price)) != action.reservation_stop_price
        or not stops[0].client_order_id
        or not stops[0].broker_order_id
    ):
        raise ValueError("fake broker position or stop differs from replacement buy fills")
    return stops[0]


def record_replacement_buy_fill(decision_id: str, *, ports: ReplacementBuyPorts) -> ReplacementStep:
    """Record the broker watermark, then confirm exact stop coverage of the new holding."""
    with ports.broker.atomic_observation():
        return _record_replacement_buy_fill_locked(decision_id, ports=ports)


def _record_replacement_buy_fill_locked(
    decision_id: str, *, ports: ReplacementBuyPorts
) -> ReplacementStep:
    execution = load_replacement_intention(ports.store, decision_id)
    action = execution.buy_action
    if action is None:
        return advance_replacement(ports.store, decision_id)
    attempt = action.order_attempts[0]
    if not attempt.broker_order_id or attempt.client_order_id != _client_order_id(action.logical_action_id):
        raise ValueError("replacement buy fill lacks the persisted fake broker references")
    account = ports.broker.snapshot()
    matching = tuple(
        row for row in account.open_orders or ()
        if row.broker_order_id == attempt.broker_order_id
        and row.client_order_id == attempt.client_order_id
        and row.symbol == action.broker_symbol
        and row.side == "buy"
        and row.holding_episode_id == _holding_id(action)
    )
    if len(matching) != 1:
        raise ValueError("replacement buy fill is not uniquely observed at the fake broker")
    order = matching[0]
    quantity, notional = ports.broker.fill_report(attempt.broker_order_id)
    if (
        quantity <= 0
        or quantity > action.requested_quantity
        or Decimal(str(order.cumulative_filled_quantity)) != quantity
        or notional <= 0
        or notional / quantity > action.reservation_price
    ):
        raise ValueError("fake broker replacement buy fill exceeds its durable reservation")
    stop = _confirmed_stop(account, action, quantity)
    PolicyProtectionBridge(ports.store, provider_id=ports.provider_id).record_cumulative_fill(
        action.logical_action_id,
        attempt_number=1,
        broker_order_id=attempt.broker_order_id,
        client_order_id=attempt.client_order_id,
        cumulative_quantity=quantity,
        average_fill_price=notional / quantity,
        cumulative_fees=Decimal("0"),
        observed_at=_observed_at(ports),
    )
    holding = ports.store.load_holding_episode_for_action(action.logical_action_id)
    if holding.holding_episode_id != _holding_id(action) or holding.remaining_quantity != quantity:
        raise ValueError("replacement buy holding did not converge to broker shares")
    if (
        holding.confirmed_stop_client_order_id == stop.client_order_id
        and holding.confirmed_stop_broker_order_id == stop.broker_order_id
        and holding.confirmed_protective_stop_price == action.reservation_stop_price
        and holding.proposed_stop_action_id == holding.confirmed_stop_action_id
    ):
        return advance_replacement(ports.store, decision_id)
    if holding.proposed_stop_action_id != holding.confirmed_stop_action_id:
        stop_intent = ports.store.load_stop_update_intent(holding.proposed_stop_action_id)
    else:
        decision = DecisionIdentity.build(
            deployment=action.decision.deployment_identity,
            clock=action.decision.clock,
            snapshot_sha256=action.decision.snapshot_sha256,
            category=DecisionCategory.REPLACEMENT,
            subject_type=DecisionSubjectType.HOLDING,
            subject_id=holding.holding_episode_id,
            sequence=holding.state_version + 1,
        )
        ports.store.record_decision(
            decision,
            policy_payload={"source_action_id": action.logical_action_id},
            guard_payload={"filled_quantity": str(quantity), "account_snapshot_id": account.account_snapshot_id},
            effective_action_payload={"stop_price": str(action.reservation_stop_price)},
        )
        stop_intent = ports.store.propose_stop_update(
            holding.holding_episode_id,
            decision=decision,
            stop_price=action.reservation_stop_price,
            expected_holding_version=holding.state_version,
            observed_at=_observed_at(ports),
        )
        holding = ports.store.load_holding_episode(holding.holding_episode_id)
    ports.store.confirm_protective_stop(
        stop_intent,
        stop_price=action.reservation_stop_price,
        client_order_id=stop.client_order_id,
        broker_order_id=stop.broker_order_id,
        observed_at=_observed_at(ports),
        expected_holding_version=holding.state_version,
        provider_id=ports.provider_id,
        requested_quantity=quantity,
    )
    return advance_replacement(ports.store, decision_id)
