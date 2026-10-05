from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from enum import Enum, StrEnum
from typing import Mapping

from core.policy_execution_state import (
    ActionIntent,
    ActionRole,
    ActionStatus,
    DecisionCategory,
    DecisionClock,
    DecisionIdentity,
    DecisionSubjectType,
    OrderSide,
    assert_same_logical_action,
    build_action_intent,
)
from core.policy_execution_store import PolicyExecutionStateStore
from core.strategy_policy.account_reconciliation import (
    AccountReconciliation,
    BrokerAccountSnapshot,
    PortfolioStateSnapshot,
    policy_execution_state_to_projection,
    reconcile_account_snapshot,
)


_PARENT_KIND = "policy_portfolio_replacement_v1"
_BUY_KIND = "policy_replacement_buy_v1"


class ReplacementStepKind(StrEnum):
    SELL_DUE = "sell_due"
    SELL_REMAINDER_READY = "sell_remainder_ready"
    WAITING_FOR_SELL = "waiting_for_sell"
    WAITING_FOR_RECONCILIATION = "waiting_for_reconciliation"
    BUY_DUE = "buy_due"
    WAITING_FOR_BUY = "waiting_for_buy"
    BLOCKED = "blocked"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class ReplacementStep:
    kind: ReplacementStepKind
    decision_id: str
    action: ActionIntent | None = None
    reason: str | None = None
    reconciliation_findings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReplacementPlan:
    """Fixed replacement choice and later buy limits, bound to existing policy state."""

    decision: DecisionIdentity
    sell_action: ActionIntent
    candidate_security_id: str
    candidate_symbol: str
    maximum_buy_quantity: Decimal
    maximum_buy_price: Decimal
    reservation_stop_price: Decimal
    risk_per_unit: Decimal
    risk_basis: str
    quantity_increment: Decimal
    maximum_position_count: int
    maximum_total_risk: Decimal
    source_portfolio_snapshot_id: str
    source_account_snapshot_id: str
    expected_holding_version: int
    policy_payload: Mapping[str, object]
    guard_payload: Mapping[str, object]

    def __post_init__(self) -> None:
        if self.decision.category is not DecisionCategory.REPLACEMENT:
            raise ValueError("replacement plan requires a replacement decision")
        if self.decision.subject_id != self.candidate_security_id:
            raise ValueError("replacement decision subject must identify the selected candidate")
        if self.sell_action.decision != self.decision:
            raise ValueError("sell action must use the fixed replacement decision identity")
        if self.sell_action.role is not ActionRole.CLOSE or self.sell_action.side is not OrderSide.SELL:
            raise ValueError("replacement sell leg must be a close action")
        if self.sell_action.holding_episode_id is None:
            raise ValueError("replacement sell leg must identify the evicted holding")
        if self.sell_action.security_id == self.candidate_security_id:
            raise ValueError("replacement candidate must differ from the evicted security")
        if self.sell_action.status is not ActionStatus.INTENDED or self.sell_action.confirmed_filled_quantity:
            raise ValueError("replacement sell leg must begin as an unissued action")
        if any(
            attempt.client_order_id is not None or attempt.broker_order_id is not None
            for attempt in self.sell_action.order_attempts
        ):
            raise ValueError("replacement sell leg cannot contain prior order references")
        for name in (
            "maximum_buy_quantity",
            "maximum_buy_price",
            "reservation_stop_price",
            "risk_per_unit",
            "quantity_increment",
            "maximum_total_risk",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be a finite positive Decimal")
        if self.reservation_stop_price >= self.maximum_buy_price:
            raise ValueError("replacement protective stop must be below its maximum buy price")
        if self.quantity_increment > self.maximum_buy_quantity:
            raise ValueError("quantity increment cannot exceed the maximum buy quantity")
        if (
            not isinstance(self.maximum_position_count, int)
            or isinstance(self.maximum_position_count, bool)
            or self.maximum_position_count < 1
        ):
            raise ValueError("maximum_position_count must be a positive integer")
        if (
            not isinstance(self.expected_holding_version, int)
            or isinstance(self.expected_holding_version, bool)
            or self.expected_holding_version < 0
        ):
            raise ValueError("expected_holding_version must be a non-negative integer")
        for name in (
            "candidate_security_id",
            "candidate_symbol",
            "risk_basis",
            "source_portfolio_snapshot_id",
            "source_account_snapshot_id",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} must be non-empty")
        object.__setattr__(self, "candidate_symbol", self.candidate_symbol.strip().upper())
        object.__setattr__(self, "risk_basis", self.risk_basis.strip())


@dataclass(frozen=True, slots=True)
class ReplacementExecution:
    plan: ReplacementPlan
    sell_action: ActionIntent
    buy_decision: DecisionIdentity | None
    buy_action: ActionIntent | None
    buy_outcome: str | None
    buy_block_reason: str | None


def _expected_buy_slot(decision: DecisionIdentity) -> str:
    return DecisionIdentity.build(
        deployment=decision.deployment_identity,
        clock=decision.clock,
        snapshot_sha256=decision.snapshot_sha256,
        category=DecisionCategory.REPLACEMENT,
        subject_type=DecisionSubjectType.PORTFOLIO,
        subject_id=decision.decision_id,
        sequence=1,
    ).decision_slot_id


def _stored_action_payload(action: ActionIntent) -> dict[str, object]:
    return {**action.immutable_payload(), "broker_symbol": action.broker_symbol}


def _action_from_payload(decision: DecisionIdentity, payload: Mapping[str, object]) -> ActionIntent:
    return build_action_intent(
        decision=decision,
        security_id=str(payload["security_id"]),
        broker_symbol=str(payload["broker_symbol"]),
        role=ActionRole(str(payload["role"])),
        side=OrderSide(str(payload["side"])),
        requested_quantity=Decimal(str(payload["requested_quantity"])),
        holding_episode_id=(
            None if payload.get("holding_episode_id") is None else str(payload["holding_episode_id"])
        ),
        reservation_price=(
            None if payload.get("reservation_price") is None else Decimal(str(payload["reservation_price"]))
        ),
        reservation_price_basis=(
            None if payload.get("reservation_price_basis") is None else str(payload["reservation_price_basis"])
        ),
        reservation_stop_price=(
            None if payload.get("reservation_stop_price") is None else Decimal(str(payload["reservation_stop_price"]))
        ),
        risk_per_unit=(
            None if payload.get("risk_per_unit") is None else Decimal(str(payload["risk_per_unit"]))
        ),
        risk_basis=None if payload.get("risk_basis") is None else str(payload["risk_basis"]),
        exit_tier=None if payload.get("exit_tier") is None else int(payload["exit_tier"]),
        snapshot_original_quantity=(
            None if payload.get("snapshot_original_quantity") is None
            else Decimal(str(payload["snapshot_original_quantity"]))
        ),
        fraction_of_original_quantity=(
            None if payload.get("fraction_of_original_quantity") is None
            else Decimal(str(payload["fraction_of_original_quantity"]))
        ),
        rounding_rule_id=None if payload.get("rounding_rule_id") is None else str(payload["rounding_rule_id"]),
    )


def _ensure_action(
    store: PolicyExecutionStateStore,
    proposed: ActionIntent,
    *,
    expected_holding_version: int | None = None,
) -> ActionIntent:
    try:
        current = store.load_action_intent(proposed.logical_action_id)
    except KeyError:
        store.record_action_intent(
            proposed,
            expected_version=None,
            expected_holding_version=expected_holding_version,
        )
        return store.load_action_intent(proposed.logical_action_id)
    assert_same_logical_action(current, proposed)
    return current


def _parent_payload(plan: ReplacementPlan) -> dict[str, object]:
    return {
        "kind": _PARENT_KIND,
        "version": 1,
        "sell_action_id": plan.sell_action.logical_action_id,
        "sell_action": _stored_action_payload(plan.sell_action),
        "candidate_security_id": plan.candidate_security_id,
        "candidate_symbol": plan.candidate_symbol,
        "maximum_buy_quantity": str(plan.maximum_buy_quantity),
        "maximum_buy_price": str(plan.maximum_buy_price),
        "reservation_stop_price": str(plan.reservation_stop_price),
        "risk_per_unit": str(plan.risk_per_unit),
        "risk_basis": plan.risk_basis,
        "quantity_increment": str(plan.quantity_increment),
        "maximum_position_count": plan.maximum_position_count,
        "maximum_total_risk": str(plan.maximum_total_risk),
        "source_portfolio_snapshot_id": plan.source_portfolio_snapshot_id,
        "source_account_snapshot_id": plan.source_account_snapshot_id,
        "expected_holding_version": plan.expected_holding_version,
        "buy_execution_decision_slot_id": _expected_buy_slot(plan.decision),
    }


def _plan_from_record(decision, payload, policy_payload, guard_payload) -> ReplacementPlan:
    if payload.get("kind") != _PARENT_KIND or payload.get("version") != 1:
        raise ValueError("decision does not contain a supported durable replacement intention")
    sell_payload = payload.get("sell_action")
    if not isinstance(sell_payload, Mapping):
        raise ValueError("replacement intention is missing its immutable sell action")
    sell = _action_from_payload(decision, sell_payload)
    if sell.logical_action_id != payload.get("sell_action_id"):
        raise ValueError("replacement sell action identity differs from its stored intention")
    plan = ReplacementPlan(
        decision=decision,
        sell_action=sell,
        candidate_security_id=str(payload["candidate_security_id"]),
        candidate_symbol=str(payload["candidate_symbol"]),
        maximum_buy_quantity=Decimal(str(payload["maximum_buy_quantity"])),
        maximum_buy_price=Decimal(str(payload["maximum_buy_price"])),
        reservation_stop_price=Decimal(str(payload["reservation_stop_price"])),
        risk_per_unit=Decimal(str(payload["risk_per_unit"])),
        risk_basis=str(payload["risk_basis"]),
        quantity_increment=Decimal(str(payload["quantity_increment"])),
        maximum_position_count=int(payload["maximum_position_count"]),
        maximum_total_risk=Decimal(str(payload["maximum_total_risk"])),
        source_portfolio_snapshot_id=str(payload["source_portfolio_snapshot_id"]),
        source_account_snapshot_id=str(payload["source_account_snapshot_id"]),
        expected_holding_version=int(payload["expected_holding_version"]),
        policy_payload=policy_payload,
        guard_payload=guard_payload,
    )
    if payload.get("buy_execution_decision_slot_id") != _expected_buy_slot(decision):
        raise ValueError("replacement buy authorization slot does not match its parent intention")
    return plan



def load_replacement_intention(store: PolicyExecutionStateStore, decision_id: str) -> ReplacementExecution:
    """Reload one linked intention and recover an action if the process stopped mid-write."""
    parent = store.load_decision_record(decision_id)
    plan = _plan_from_record(
        parent.decision,
        parent.effective_action_payload,
        parent.policy_payload,
        parent.guard_payload,
    )
    sell = _ensure_action(
        store,
        plan.sell_action,
        expected_holding_version=plan.expected_holding_version,
    )
    buy_decision = None
    buy_action = None
    buy_outcome = None
    buy_block_reason = None
    try:
        child = store.load_decision_record_by_slot(_expected_buy_slot(plan.decision))
    except KeyError:
        child = None
    if child is not None:
        payload = child.effective_action_payload
        if payload.get("kind") != _BUY_KIND or payload.get("parent_decision_id") != plan.decision.decision_id:
            raise ValueError("replacement child decision does not identify its parent intention")
        buy_decision = child.decision
        buy_outcome = str(payload.get("outcome"))
        buy_block_reason = None if payload.get("reason") is None else str(payload["reason"])
        buy_payload = payload.get("buy_action")
        if buy_outcome == "buy":
            if not isinstance(buy_payload, Mapping):
                raise ValueError("authorized replacement buy is missing its immutable action")
            template = _action_from_payload(child.decision, buy_payload)
            if template.logical_action_id != payload.get("buy_action_id"):
                raise ValueError("replacement buy action identity differs from its stored decision")
            buy_action = _ensure_action(store, template)
        elif buy_outcome != "blocked":
            raise ValueError("replacement child decision has an unsupported outcome")
    return ReplacementExecution(plan, sell, buy_decision, buy_action, buy_outcome, buy_block_reason)


def _seller_step(plan: ReplacementPlan, sell: ActionIntent) -> ReplacementStep:
    if sell.status is ActionStatus.INTENDED:
        return ReplacementStep(ReplacementStepKind.SELL_DUE, plan.decision.decision_id, sell)
    if sell.status is ActionStatus.REMAINDER_READY:
        return ReplacementStep(
            ReplacementStepKind.BLOCKED,
            plan.decision.decision_id,
            reason="sell was rejected or cancelled; this replacement will not issue a duplicate sell leg",
        )
    if sell.status in {
        ActionStatus.SUBMITTED,
        ActionStatus.PARTIALLY_FILLED,
        ActionStatus.CANCEL_REQUESTED,
        ActionStatus.RECONCILIATION_REQUIRED,
    }:
        return ReplacementStep(
            ReplacementStepKind.WAITING_FOR_SELL,
            plan.decision.decision_id,
            reason=f"sell action remains {sell.status.value}",
        )
    if sell.status is ActionStatus.FILLED and sell.confirmed_filled_quantity == sell.requested_quantity:
        return ReplacementStep(
            ReplacementStepKind.WAITING_FOR_RECONCILIATION,
            plan.decision.decision_id,
            reason="full sale is recorded; a fresh account snapshot is required before buying",
        )
    return ReplacementStep(
        ReplacementStepKind.BLOCKED,
        plan.decision.decision_id,
        reason="sell did not fully close the selected holding; remaining shares stay protected",
    )


def _json_default(value: object) -> object:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"unsupported replacement snapshot value: {type(value).__name__}")


def _snapshot_sha256(
    plan: ReplacementPlan,
    account: BrokerAccountSnapshot,
    portfolio: PortfolioStateSnapshot,
    candidate_price: Decimal,
    sell: ActionIntent,
    sell_version: int | None,
) -> str:
    facts = {
        "parent_decision_id": plan.decision.decision_id,
        "candidate_security_id": plan.candidate_security_id,
        "candidate_symbol": plan.candidate_symbol,
        "candidate_price": str(candidate_price),
        "account": {
            "paper_account_environment_id": account.paper_account_environment_id,
            "clock": asdict(account.clock),
            "equity": account.equity,
            "cash": account.cash,
            "peak_equity": account.peak_equity,
            "balance_observed_at": account.balance_observed_at,
            "peak_observed_at": account.peak_observed_at,
            "positions": sorted(
                (asdict(item) for item in account.positions or ()),
                key=lambda item: str(item.get("symbol", "")),
            ),
            "open_orders": sorted(
                (asdict(item) for item in account.open_orders or ()),
                key=lambda item: str(item.get("broker_order_id") or item.get("client_order_id") or ""),
            ),
            "source_namespace": account.source_namespace,
            "account_snapshot_id": account.account_snapshot_id,
        },
        "portfolio": {
            "portfolio_snapshot_id": portfolio.portfolio_snapshot_id,
            "equity": portfolio.equity,
            "cash": portfolio.cash,
            "gross_exposure": portfolio.gross_exposure,
            "open_risk": portfolio.open_risk,
            "portfolio_peak_equity": portfolio.portfolio_peak_equity,
        },
        "sell": {
            "logical_action_id": sell.logical_action_id,
            "status": sell.status.value,
            "requested_quantity": str(sell.requested_quantity),
            "confirmed_filled_quantity": str(sell.confirmed_filled_quantity),
            "state_version": sell_version,
            "attempts": [
                {
                    "number": item.attempt_number,
                    "status": item.status.value,
                    "requested": str(item.requested_quantity),
                    "filled": str(item.confirmed_filled_quantity),
                    "terminal": None if item.terminal_status is None else item.terminal_status.value,
                    "client_order_id": item.client_order_id,
                    "broker_order_id": item.broker_order_id,
                }
                for item in sell.order_attempts
            ],
        },
    }
    canonical = json.dumps(
        facts, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False, default=_json_default,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _child_decision(
    plan: ReplacementPlan,
    account: BrokerAccountSnapshot,
    portfolio: PortfolioStateSnapshot,
    candidate_price: Decimal,
    sell: ActionIntent,
    sell_version: int | None,
) -> DecisionIdentity:
    clock = DecisionClock(
        exchange_id=plan.decision.clock.exchange_id,
        decision_session=account.clock.completed_session,
        as_of_cutoff_at=account.clock.as_of_cutoff,
        next_execution_session=account.clock.next_execution_session,
        account_valuation_session=account.clock.next_execution_session,
        account_valuation_at=account.clock.valuation_time,
    )
    if portfolio.clock != clock:
        raise ValueError("refreshed portfolio clock differs from its broker account snapshot")
    return DecisionIdentity.build(
        deployment=plan.decision.deployment_identity,
        clock=clock,
        snapshot_sha256=_snapshot_sha256(
            plan, account, portfolio, candidate_price, sell, sell_version
        ),
        category=DecisionCategory.REPLACEMENT,
        subject_type=DecisionSubjectType.PORTFOLIO,
        subject_id=plan.decision.decision_id,
        sequence=1,
    )



def _positive_decimal(value: Decimal | int | str, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite positive Decimal")
    result = value if isinstance(value, Decimal) else Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{name} must be a finite positive Decimal")
    return result


def _build_reconciliation(
    store: PolicyExecutionStateStore,
    decision: DecisionIdentity,
    account: BrokerAccountSnapshot,
    portfolio: PortfolioStateSnapshot,
    *,
    maximum_balance_age: timedelta,
    maximum_mark_age: timedelta,
) -> AccountReconciliation:
    bound_account = replace(
        account,
        decision_id=decision.decision_id,
        decision_slot_id=decision.decision_slot_id,
    )
    store.record_portfolio_snapshot(portfolio)
    snapshot = store.load_policy_execution_snapshot(
        deployment_generation_id=decision.deployment_generation_id,
        portfolio_snapshot_id=portfolio.portfolio_snapshot_id,
    )
    projection = policy_execution_state_to_projection(
        account=bound_account,
        portfolio_snapshot=snapshot.portfolio_snapshot,
        action_projections=snapshot.action_projections,
        holding_episodes=snapshot.holding_episodes,
    )
    return reconcile_account_snapshot(
        account=bound_account,
        projection=projection,
        maximum_balance_age=maximum_balance_age,
        maximum_mark_age=maximum_mark_age,
    )


def _active_position_count(account: BrokerAccountSnapshot) -> int:
    return 0 if account.positions is None else sum(1 for row in account.positions if row.quantity > 0)


def _candidate_is_held(account: BrokerAccountSnapshot, plan: ReplacementPlan) -> bool:
    return bool(
        account.positions is not None
        and any(
            row.symbol.strip().upper() == plan.candidate_symbol and row.quantity > 0
            for row in account.positions
        )
    )


def start_replacement(store: PolicyExecutionStateStore, plan: ReplacementPlan) -> ReplacementStep:
    """Persist one fixed sell-to-buy dependency before returning its first sell action."""
    portfolio = store.load_portfolio_snapshot(plan.source_portfolio_snapshot_id)
    if portfolio.deployment_identity != plan.decision.deployment_identity:
        raise ValueError("replacement portfolio does not match the decision deployment")
    if portfolio.clock != plan.decision.clock:
        raise ValueError("replacement decision clock differs from its source portfolio snapshot")
    if portfolio.account_snapshot_id != plan.source_account_snapshot_id:
        raise ValueError("replacement source account identity differs from its portfolio snapshot")
    held = store.load_holding_episode(plan.sell_action.holding_episode_id or "")
    if held.state_version != plan.expected_holding_version:
        raise ValueError("selected holding changed after replacement sizing")
    if held.remaining_quantity != plan.sell_action.requested_quantity:
        raise ValueError("sell quantity must match the selected holding's current remainder")
    if held.pending_action_ids:
        raise ValueError("selected holding has an unresolved strategy action")
    if dict(held.policy_flags).get("position_reconciliation_required"):
        raise ValueError("selected holding requires position reconciliation")
    if held.confirmed_protective_stop_price is None or (
        held.confirmed_stop_client_order_id is None and held.confirmed_stop_broker_order_id is None
    ):
        raise ValueError("selected holding must retain confirmed protective-stop evidence")

    store.record_decision(
        plan.decision,
        policy_payload=plan.policy_payload,
        guard_payload=plan.guard_payload,
        effective_action_payload=_parent_payload(plan),
    )
    sell = _ensure_action(
        store, plan.sell_action, expected_holding_version=plan.expected_holding_version
    )
    return _seller_step(plan, sell)


def _valid_refreshed_context(
    plan: ReplacementPlan,
    account: BrokerAccountSnapshot,
    portfolio: PortfolioStateSnapshot,
) -> str | None:
    deployment = plan.decision.deployment_identity
    if account.paper_account_environment_id != deployment.paper_account_environment_id:
        return "refreshed account belongs to a different paper account environment"
    if portfolio.deployment_identity != deployment:
        return "refreshed portfolio belongs to a different deployment generation or store"
    if (
        account.account_snapshot_id is None
        or account.account_snapshot_id == plan.source_account_snapshot_id
        or portfolio.account_snapshot_id != account.account_snapshot_id
        or portfolio.source_namespace != account.source_namespace
    ):
        return "buy requires a new account snapshot bound to the same broker source and portfolio facts"
    if (
        account.clock.completed_session != plan.decision.clock.decision_session
        or account.clock.as_of_cutoff != plan.decision.clock.as_of_cutoff_at
        or account.clock.next_execution_session != plan.decision.clock.next_execution_session
        or account.clock.valuation_time <= plan.decision.clock.account_valuation_at
    ):
        return "refreshed account clock is stale or outside the fixed replacement decision session"
    if account.positions is None or account.open_orders is None:
        return None
    return None


def advance_replacement(
    store: PolicyExecutionStateStore,
    decision_id: str,
    *,
    account: BrokerAccountSnapshot | None = None,
    portfolio_snapshot: PortfolioStateSnapshot | None = None,
    candidate_price: Decimal | int | str | None = None,
    maximum_balance_age: timedelta = timedelta(minutes=15),
    maximum_mark_age: timedelta = timedelta(minutes=15),
) -> ReplacementStep:
    """Release the buy only after a full sale and fresh account, cash, risk, and price checks."""
    execution = load_replacement_intention(store, decision_id)
    if execution.buy_decision is not None:
        buy_record = store.load_decision_record(execution.buy_decision.decision_id)
        buy = execution.buy_action
        if execution.buy_outcome == "blocked":
            return ReplacementStep(
                ReplacementStepKind.BLOCKED,
                decision_id,
                reason=execution.buy_block_reason or "replacement buy was durably blocked",
            )
        if buy is None:
            return ReplacementStep(
                ReplacementStepKind.BLOCKED, decision_id, reason="buy decision has no recoverable action"
            )
        if buy.status is ActionStatus.INTENDED:
            if account is None or candidate_price is None:
                return ReplacementStep(
                    ReplacementStepKind.WAITING_FOR_RECONCILIATION,
                    decision_id,
                    buy,
                    reason="account snapshot and candidate price are required to release the authorized buy",
                )
            if account.account_snapshot_id is None or account.positions is None or account.open_orders is None:
                return ReplacementStep(
                    ReplacementStepKind.WAITING_FOR_RECONCILIATION,
                    decision_id,
                    buy,
                    reason="account identity, positions, and open orders must be observed before buy submission",
                )
            authorized_snapshot = buy_record.effective_action_payload.get("account_snapshot_id")
            authorized_environment = buy_record.effective_action_payload.get("paper_account_environment_id")
            authorized_source = buy_record.effective_action_payload.get("source_namespace")
            authorized_portfolio = buy_record.effective_action_payload.get("portfolio_snapshot_id")
            authorized_price_raw = buy_record.effective_action_payload.get("candidate_price")
            current_price = _positive_decimal(candidate_price, "candidate_price")
            authorized_price = (
                None if authorized_price_raw is None else Decimal(str(authorized_price_raw))
            )
            portfolio_matches = (
                portfolio_snapshot is None
                or portfolio_snapshot.portfolio_snapshot_id == authorized_portfolio
            )
            if (
                account.account_snapshot_id != authorized_snapshot
                or account.paper_account_environment_id != authorized_environment
                or account.source_namespace != authorized_source
                or not portfolio_matches
                or current_price != authorized_price
            ):
                reason = (
                    "account snapshot or candidate price changed after buy authorization; "
                    "the unsubmitted buy was resolved and requires a new replacement decision"
                )
                projection = store.load_action_projection(buy.logical_action_id)
                store.record_explicit_action_resolution(
                    buy.logical_action_id,
                    resolution_reason=reason,
                    expected_action_version=projection.state_version,
                    observed_at=account.clock.valuation_time,
                )
                resolved = store.load_action_intent(buy.logical_action_id)
                return ReplacementStep(
                    ReplacementStepKind.BLOCKED,
                    decision_id,
                    resolved,
                    reason=reason,
                )
            return ReplacementStep(ReplacementStepKind.BUY_DUE, decision_id, buy)
        if buy.status is ActionStatus.FILLED:
            return ReplacementStep(ReplacementStepKind.COMPLETE, decision_id, buy)
        if buy.status in {
            ActionStatus.SUBMITTED, ActionStatus.PARTIALLY_FILLED, ActionStatus.CANCEL_REQUESTED,
            ActionStatus.RECONCILIATION_REQUIRED, ActionStatus.REMAINDER_READY,
        }:
            return ReplacementStep(
                ReplacementStepKind.WAITING_FOR_BUY,
                decision_id,
                buy,
                reason=f"buy action remains {buy.status.value}",
            )
        return ReplacementStep(
            ReplacementStepKind.BLOCKED, decision_id, buy, reason=f"buy action ended in {buy.status.value}"
        )
    plan = execution.plan
    sell = execution.sell_action
    if sell.status is ActionStatus.INTENDED:
        return ReplacementStep(ReplacementStepKind.SELL_DUE, decision_id, sell)
    if sell.status is ActionStatus.REMAINDER_READY:
        return ReplacementStep(
            ReplacementStepKind.BLOCKED,
            decision_id,
            reason="sell was rejected or cancelled; this replacement will not issue a duplicate sell leg",
        )
    if sell.status in {
        ActionStatus.SUBMITTED, ActionStatus.PARTIALLY_FILLED,
        ActionStatus.CANCEL_REQUESTED, ActionStatus.RECONCILIATION_REQUIRED,
    }:
        return ReplacementStep(
            ReplacementStepKind.WAITING_FOR_SELL,
            decision_id,
            reason=f"sell action remains {sell.status.value}",
        )
    if sell.status is not ActionStatus.FILLED or sell.confirmed_filled_quantity != sell.requested_quantity:
        return ReplacementStep(
            ReplacementStepKind.BLOCKED,
            decision_id,
            reason="sell did not fully close the selected holding; remaining shares stay protected",
        )
    holding = store.load_holding_episode(sell.holding_episode_id or "")
    if holding.remaining_quantity != 0 or holding.pending_action_ids:
        return ReplacementStep(
            ReplacementStepKind.WAITING_FOR_RECONCILIATION,
            decision_id,
            reason="sell fill and durable holding quantity have not reached the same flat state",
        )
    if account is None or portfolio_snapshot is None or candidate_price is None:
        return ReplacementStep(
            ReplacementStepKind.WAITING_FOR_RECONCILIATION,
            decision_id,
            reason="fresh broker account, portfolio snapshot, and candidate price are required",
        )
    price = _positive_decimal(candidate_price, "candidate_price")
    invalid = _valid_refreshed_context(plan, account, portfolio_snapshot)
    if invalid is not None:
        return ReplacementStep(ReplacementStepKind.BLOCKED, decision_id, reason=invalid)
    if account.positions is None or account.open_orders is None:
        return ReplacementStep(
            ReplacementStepKind.WAITING_FOR_RECONCILIATION,
            decision_id,
            reason="positions and open orders must be explicitly observed before the buy",
        )
    if _candidate_is_held(account, plan):
        return ReplacementStep(
            ReplacementStepKind.BLOCKED, decision_id, reason="candidate is already present in the account snapshot"
        )

    try:
        child = _child_decision(
            plan, account, portfolio_snapshot, price, sell,
            store.load_action_projection(sell.logical_action_id).state_version,
        )
        reconciliation = _build_reconciliation(
            store, child, account, portfolio_snapshot,
            maximum_balance_age=maximum_balance_age,
            maximum_mark_age=maximum_mark_age,
        )
    except (ValueError, KeyError) as exc:
        return ReplacementStep(
            ReplacementStepKind.WAITING_FOR_RECONCILIATION,
            decision_id,
            reason=f"refreshed account did not reconcile: {exc}",
        )
    if not reconciliation.ready:
        return ReplacementStep(
            ReplacementStepKind.WAITING_FOR_RECONCILIATION,
            decision_id,
            reason="refreshed account reconciliation is not ready",
            reconciliation_findings=tuple(
                f"{f.path}: {f.state} ({f.detail})" for f in reconciliation.findings
            ),
        )
    if price > plan.maximum_buy_price:
        return _persist_veto(
            store, execution, child, account, portfolio_snapshot, price,
            "candidate price exceeds the fixed replacement buy limit",
            {"candidate_price": str(price), "maximum_buy_price": str(plan.maximum_buy_price)},
            reconciliation,
        )
    if reconciliation.available_cash is None or reconciliation.total_committed_risk is None:
        return ReplacementStep(
            ReplacementStepKind.WAITING_FOR_RECONCILIATION,
            decision_id,
            reason="reconciled available cash or committed risk is unavailable",
        )
    if reconciliation.pending_entry_count is None:
        return ReplacementStep(
            ReplacementStepKind.WAITING_FOR_RECONCILIATION,
            decision_id,
            reason="reconciled pending entry count is unavailable",
        )
    occupied_slots = _active_position_count(account) + reconciliation.pending_entry_count
    if occupied_slots >= plan.maximum_position_count:
        return _persist_veto(
            store, execution, child, account, portfolio_snapshot, price,
            "replacement candidate would exceed the fixed position capacity",
            {"occupied_slots": occupied_slots, "maximum_position_count": plan.maximum_position_count},
            reconciliation,
        )

    available_cash = Decimal(str(reconciliation.available_cash))
    current_risk = Decimal(str(reconciliation.total_committed_risk))
    if current_risk >= plan.maximum_total_risk:
        return _persist_veto(
            store, execution, child, account, portfolio_snapshot, price,
            "no portfolio risk capacity remains for the replacement buy",
            {"total_committed_risk": str(current_risk), "maximum_total_risk": str(plan.maximum_total_risk)},
            reconciliation,
        )
    cash_quantity = (
        available_cash / plan.maximum_buy_price / plan.quantity_increment
    ).to_integral_value(rounding=ROUND_DOWN) * plan.quantity_increment
    risk_quantity = (
        (plan.maximum_total_risk - current_risk) / plan.risk_per_unit / plan.quantity_increment
    ).to_integral_value(rounding=ROUND_DOWN) * plan.quantity_increment
    quantity_cap = (
        plan.maximum_buy_quantity / plan.quantity_increment
    ).to_integral_value(rounding=ROUND_DOWN) * plan.quantity_increment
    quantity = min(quantity_cap, cash_quantity, risk_quantity)
    if quantity <= 0:
        return _persist_veto(
            store, execution, child, account, portfolio_snapshot, price,
            "reconciled cash and risk do not fund one replacement share increment",
            {"available_cash": str(available_cash), "total_committed_risk": str(current_risk)},
            reconciliation,
        )
    return _persist_buy(
        store, execution, child, account, portfolio_snapshot, price, quantity, reconciliation
    )



def _child_payload(
    execution: ReplacementExecution,
    account: BrokerAccountSnapshot,
    portfolio: PortfolioStateSnapshot,
    candidate_price: Decimal,
    outcome: str,
    reason: str | None,
    buy_action: ActionIntent | None,
) -> dict[str, object]:
    return {
        "kind": _BUY_KIND,
        "parent_decision_id": execution.plan.decision.decision_id,
        "sell_action_id": execution.sell_action.logical_action_id,
        "outcome": outcome,
        "reason": reason,
        "account_snapshot_id": account.account_snapshot_id,
        "portfolio_snapshot_id": portfolio.portfolio_snapshot_id,
        "paper_account_environment_id": account.paper_account_environment_id,
        "source_namespace": account.source_namespace,
        "candidate_price": str(candidate_price),
        "buy_action_id": None if buy_action is None else buy_action.logical_action_id,
        "buy_action": None if buy_action is None else _stored_action_payload(buy_action),
    }


def _persist_veto(
    store: PolicyExecutionStateStore,
    execution: ReplacementExecution,
    decision: DecisionIdentity,
    account: BrokerAccountSnapshot,
    portfolio: PortfolioStateSnapshot,
    candidate_price: Decimal,
    reason: str,
    guard_facts: Mapping[str, object],
    reconciliation: AccountReconciliation,
) -> ReplacementStep:
    store.record_decision(
        decision,
        policy_payload={
            "parent_decision_id": execution.plan.decision.decision_id,
            "candidate_security_id": execution.plan.candidate_security_id,
            "candidate_price": str(candidate_price),
        },
        guard_payload={
            **dict(guard_facts),
            "reconciliation_ready": reconciliation.ready,
            "available_cash": reconciliation.available_cash,
            "total_committed_risk": reconciliation.total_committed_risk,
            "pending_entry_count": reconciliation.pending_entry_count,
        },
        effective_action_payload=_child_payload(
            execution, account, portfolio, candidate_price, "blocked", reason, None
        ),
    )
    return ReplacementStep(ReplacementStepKind.BLOCKED, execution.plan.decision.decision_id, reason=reason)


def _persist_buy(
    store: PolicyExecutionStateStore,
    execution: ReplacementExecution,
    decision: DecisionIdentity,
    account: BrokerAccountSnapshot,
    portfolio: PortfolioStateSnapshot,
    candidate_price: Decimal,
    quantity: Decimal,
    reconciliation: AccountReconciliation,
) -> ReplacementStep:
    plan = execution.plan
    buy = build_action_intent(
        decision=decision,
        security_id=plan.candidate_security_id,
        broker_symbol=plan.candidate_symbol,
        role=ActionRole.REPLACEMENT,
        side=OrderSide.BUY,
        requested_quantity=quantity,
        reservation_price=plan.maximum_buy_price,
        reservation_price_basis=(
            f"fixed_limit:{plan.decision.decision_id};account_snapshot:{account.account_snapshot_id}"
        ),
        reservation_stop_price=plan.reservation_stop_price,
        risk_per_unit=plan.risk_per_unit,
        risk_basis=plan.risk_basis,
    )
    store.record_decision(
        decision,
        policy_payload={
            "parent_decision_id": plan.decision.decision_id,
            "candidate_security_id": plan.candidate_security_id,
            "candidate_price": str(candidate_price),
        },
        guard_payload={
            "reconciliation_ready": True,
            "account_snapshot_id": account.account_snapshot_id,
            "available_cash": str(reconciliation.available_cash),
            "total_committed_risk": str(reconciliation.total_committed_risk),
            "occupied_slots": (
                _active_position_count(account) + int(reconciliation.pending_entry_count or 0)
            ),
            "maximum_position_count": plan.maximum_position_count,
            "candidate_price": str(candidate_price),
            "maximum_buy_price": str(plan.maximum_buy_price),
            "requested_buy_quantity": str(quantity),
            "reserved_buy_cash": str(quantity * plan.maximum_buy_price),
            "risk_per_unit": str(plan.risk_per_unit),
            "maximum_total_risk": str(plan.maximum_total_risk),
            "reserved_buy_risk": str(quantity * plan.risk_per_unit),
        },
        effective_action_payload=_child_payload(
            execution, account, portfolio, candidate_price, "buy", None, buy
        ),
    )
    durable_buy = _ensure_action(store, buy)
    return ReplacementStep(ReplacementStepKind.BUY_DUE, plan.decision.decision_id, durable_buy)
