"""Durable paper-policy additions for an existing protected holding.

This module produces a persisted ActionIntent. Broker effects are injected by
callers; no live provider or runtime is selected here. A partial fill must be
terminally reconciled before the accepted stop-update API can resize protection.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import timedelta
from decimal import Decimal, ROUND_DOWN
from enum import StrEnum
from hashlib import sha256
from typing import Callable, Mapping

from core.policy_execution_state import (
    ActionAttemptStatus,
    ActionIntent,
    ActionRole,
    ActionStatus,
    DecisionCategory,
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
    BrokerOrderFact,
    BrokerPositionFact,
    policy_execution_state_to_projection,
    reconcile_account_snapshot,
)
from core.strategy_policy.contracts_v3 import (
    AddOnDecisionV3,
    AddOnSnapshotV3,
    validate_add_on_decision,
)


_ACTION_KIND = "policy_holding_addition_v1"
_PROTECTION_KIND = "policy_addition_protection_v1"
_MAXIMUM_ADD_ONS_PER_HOLDING = 2
_MAX_POSITION_RISK_FRACTION = Decimal("0.01")
_ACTIVE_ORDER_STATUSES = frozenset({"submitted", "partially_filled", "cancel_requested"})
_TERMINAL_ORDER_STATUSES = frozenset({"cancelled", "canceled", "rejected", "filled"})


def _source_fingerprint(account: BrokerAccountSnapshot, portfolio_snapshot) -> str:
    """Bind an intended addition to complete authorization-time paper facts."""
    canonical = json.dumps(
        {"account": asdict(account), "portfolio": asdict(portfolio_snapshot)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


class AdditionStepKind(StrEnum):
    BUY_DUE = "buy_due"
    WAITING_FOR_BUY = "waiting_for_buy"
    PROTECTION_DUE = "protection_due"
    WAITING_FOR_PROTECTION = "waiting_for_protection"
    PROTECTED = "protected"
    DECLINED = "declined"
    BLOCKED = "blocked"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class AdditionStep:
    kind: AdditionStepKind
    decision_id: str
    action: ActionIntent | None = None
    reason: str | None = None
    reconciliation_findings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AdditionPlan:
    """One fixed V3 add-on decision bound to its holding and source account."""

    decision: DecisionIdentity
    add_on_snapshot: AddOnSnapshotV3
    policy_decision: AddOnDecisionV3
    holding_episode_id: str
    source_account_snapshot_id: str
    source_portfolio_snapshot_id: str
    expected_holding_version: int

    def __post_init__(self) -> None:
        if self.decision.category is not DecisionCategory.ADDITION:
            raise ValueError("addition plan requires an addition decision")
        if (
            self.decision.subject_type is not DecisionSubjectType.HOLDING
            or self.decision.subject_id != self.holding_episode_id
        ):
            raise ValueError("addition decision must identify its holding episode")
        if self.add_on_snapshot.market.session != self.decision.clock.decision_session.isoformat():
            raise ValueError("add-on snapshot session differs from the decision session")
        validate_add_on_decision(self.add_on_snapshot, self.policy_decision)
        for name in ("holding_episode_id", "source_account_snapshot_id", "source_portfolio_snapshot_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        if (
            not isinstance(self.expected_holding_version, int)
            or isinstance(self.expected_holding_version, bool)
            or self.expected_holding_version < 0
        ):
            raise ValueError("expected_holding_version must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class AdditionSubmissionResult:
    success: bool
    broker_order_id: str | None = None
    outcome_uncertain: bool = False
    error: str = ""


@dataclass(frozen=True, slots=True)
class AdditionCancelResult:
    outcome_uncertain: bool
    terminal_status: ActionAttemptStatus | None = None
    cumulative_quantity: Decimal | None = None
    cumulative_notional: Decimal | None = None
    cumulative_fees: Decimal | None = None
    fill_event_id: str | None = None
    account: BrokerAccountSnapshot | None = None


def _action_payload(action: ActionIntent) -> dict[str, object]:
    return {**action.immutable_payload(), "broker_symbol": action.broker_symbol}


def _action_from_payload(decision: DecisionIdentity, payload: Mapping[str, object]) -> ActionIntent:
    return build_action_intent(
        decision=decision,
        security_id=str(payload["security_id"]),
        broker_symbol=str(payload["broker_symbol"]),
        role=ActionRole(str(payload["role"])),
        side=OrderSide(str(payload["side"])),
        requested_quantity=Decimal(str(payload["requested_quantity"])),
        holding_episode_id=str(payload["holding_episode_id"]),
        reservation_price=Decimal(str(payload["reservation_price"])),
        reservation_price_basis=str(payload["reservation_price_basis"]),
        reservation_stop_price=Decimal(str(payload["reservation_stop_price"])),
        risk_per_unit=Decimal(str(payload["risk_per_unit"])),
        risk_basis=str(payload["risk_basis"]),
    )


def _ensure_action(
    store: PolicyExecutionStateStore,
    proposed: ActionIntent,
    *,
    expected_holding_version: int,
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


def _plan_from_record(record) -> tuple[AdditionPlan, dict[str, object]]:
    payload = record.effective_action_payload
    if payload.get("kind") != _ACTION_KIND or payload.get("version") != 1:
        raise ValueError("decision does not contain a supported durable addition intention")
    if "order_type" in payload and payload["order_type"] != "limit":
        raise ValueError("addition intention has an unsupported fixed order type")
    policy = record.policy_payload
    snapshot_payload = policy.get("add_on_snapshot")
    decision_payload = policy.get("add_on_decision")
    if not isinstance(snapshot_payload, Mapping) or not isinstance(decision_payload, Mapping):
        raise ValueError("addition intention is missing its fixed V3 decision")
    snapshot = AddOnSnapshotV3.from_canonical_json(
        json.dumps(dict(snapshot_payload), sort_keys=True, separators=(",", ":"))
    )
    add_on_decision = AddOnDecisionV3.from_canonical_json(
        json.dumps(dict(decision_payload), sort_keys=True, separators=(",", ":"))
    )
    plan = AdditionPlan(
        decision=record.decision,
        add_on_snapshot=snapshot,
        policy_decision=add_on_decision,
        holding_episode_id=str(payload["holding_episode_id"]),
        source_account_snapshot_id=str(payload["source_account_snapshot_id"]),
        source_portfolio_snapshot_id=str(payload["source_portfolio_snapshot_id"]),
        expected_holding_version=int(payload["expected_holding_version"]),
    )
    return plan, payload


def _confirmed_protection_matches_addition(
    store: PolicyExecutionStateStore,
    plan: AdditionPlan,
    *,
    quantity: Decimal,
    stop_price: Decimal,
) -> bool:
    holding = store.load_holding_episode(plan.holding_episode_id)
    action_id = holding.confirmed_stop_action_id
    if action_id is None:
        return False
    try:
        stop = store.load_stop_update_intent(action_id)
        decision = store.load_decision_record(stop.decision_id)
    except KeyError:
        return False
    guard = decision.guard_payload
    effective = decision.effective_action_payload
    matches = (
        effective.get("kind") == _PROTECTION_KIND
        and effective.get("parent_decision_id") == plan.decision.decision_id
        and stop.holding_episode_id == plan.holding_episode_id
        and stop.requested_stop_price == stop_price
        and Decimal(str(guard.get("holding_quantity", "-1"))) == quantity
    )
    if not matches:
        return False
    provider_id = guard.get("provider_id")
    if not isinstance(provider_id, str) or not provider_id:
        return False
    try:
        return store.confirmed_protective_order_quantity(
            action_id, provider_id=provider_id
        ) == quantity
    except (KeyError, ValueError):
        return False


def _step_for_action(store: PolicyExecutionStateStore, plan: AdditionPlan, action: ActionIntent) -> AdditionStep:
    if action.status is ActionStatus.INTENDED:
        return AdditionStep(AdditionStepKind.BUY_DUE, plan.decision.decision_id, action)
    if action.status is ActionStatus.FILLED or (
        action.status is ActionStatus.RESOLVED and action.confirmed_filled_quantity > 0
    ):
        holding = store.load_holding_episode(plan.holding_episode_id)
        if holding.proposed_stop_action_id != holding.confirmed_stop_action_id:
            return AdditionStep(
                AdditionStepKind.WAITING_FOR_PROTECTION,
                plan.decision.decision_id,
                action,
                reason="protective-stop replacement remains unresolved",
            )
        stop_price = holding.confirmed_protective_stop_price or Decimal("0")
        if _confirmed_protection_matches_addition(
            store,
            plan,
            quantity=holding.remaining_quantity,
            stop_price=stop_price,
        ):
            return AdditionStep(AdditionStepKind.PROTECTED, plan.decision.decision_id, action)
        return AdditionStep(AdditionStepKind.PROTECTION_DUE, plan.decision.decision_id, action)
    if action.status is ActionStatus.RESOLVED and action.confirmed_filled_quantity == 0:
        if all(
            attempt.client_order_id is None and attempt.broker_order_id is None
            for attempt in action.order_attempts
        ):
            return AdditionStep(AdditionStepKind.BLOCKED, plan.decision.decision_id, action)
        return AdditionStep(AdditionStepKind.REJECTED, plan.decision.decision_id, action)
    return AdditionStep(
        AdditionStepKind.WAITING_FOR_BUY,
        plan.decision.decision_id,
        action,
        reason=f"addition action remains {action.status.value}",
    )


def load_addition_intention(store: PolicyExecutionStateStore, decision_id: str) -> AdditionStep:
    """Reload one fixed decision and recover an action after an interrupted write."""
    record = store.load_decision_record(decision_id)
    plan, payload = _plan_from_record(record)
    outcome = str(payload.get("outcome"))
    if outcome == "declined":
        return AdditionStep(AdditionStepKind.DECLINED, decision_id, reason=str(payload.get("reason") or "policy declined"))
    if outcome == "blocked":
        return AdditionStep(AdditionStepKind.BLOCKED, decision_id, reason=str(payload.get("reason") or "addition was blocked"))
    if outcome != "buy":
        raise ValueError("addition intention has an unsupported outcome")
    raw_action = payload.get("action")
    if not isinstance(raw_action, Mapping):
        raise ValueError("authorized addition is missing its immutable action")
    action_template = _action_from_payload(record.decision, raw_action)
    if action_template.logical_action_id != payload.get("action_id"):
        raise ValueError("addition action identity differs from its durable decision")
    action = _ensure_action(store, action_template, expected_holding_version=plan.expected_holding_version)
    return _step_for_action(store, plan, action)


def _account_clock_matches(plan: AdditionPlan, account: BrokerAccountSnapshot) -> bool:
    clock = plan.decision.clock
    return (
        account.clock.completed_session == clock.decision_session
        and account.clock.as_of_cutoff == clock.as_of_cutoff_at
        and account.clock.next_execution_session == clock.next_execution_session
        and account.clock.valuation_time == clock.account_valuation_at
    )


def _position_for_holding(account: BrokerAccountSnapshot, symbol: str) -> BrokerPositionFact:
    if account.positions is None:
        raise ValueError("broker positions are unavailable")
    matches = tuple(row for row in account.positions if row.symbol.strip().upper() == symbol.upper())
    if len(matches) != 1:
        raise ValueError("account snapshot must contain one unambiguous position for the holding")
    return matches[0]


def _bound_reconciliation(
    store: PolicyExecutionStateStore,
    plan: AdditionPlan,
    account: BrokerAccountSnapshot,
    portfolio,
    *,
    maximum_balance_age: timedelta,
    maximum_mark_age: timedelta,
) -> AccountReconciliation:
    bound_account = replace(
        account,
        decision_id=plan.decision.decision_id,
        decision_slot_id=plan.decision.decision_slot_id,
    )
    store.record_portfolio_snapshot(portfolio)
    snapshot = store.load_policy_execution_snapshot(
        deployment_generation_id=plan.decision.deployment_generation_id,
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


def _decision_payload(plan: AdditionPlan) -> dict[str, object]:
    return {
        "add_on_snapshot": json.loads(plan.add_on_snapshot.to_canonical_json()),
        "add_on_decision": json.loads(plan.policy_decision.to_canonical_json()),
    }


def _persist_outcome(
    store: PolicyExecutionStateStore,
    plan: AdditionPlan,
    *,
    outcome: str,
    reason: str | None,
    guard: Mapping[str, object],
    action: ActionIntent | None = None,
) -> AdditionStep:
    payload: dict[str, object] = {
        "kind": _ACTION_KIND,
        "version": 1,
        "outcome": outcome,
        "reason": reason,
        "holding_episode_id": plan.holding_episode_id,
        "source_account_snapshot_id": plan.source_account_snapshot_id,
        "source_portfolio_snapshot_id": plan.source_portfolio_snapshot_id,
        "expected_holding_version": plan.expected_holding_version,
        "action_id": None if action is None else action.logical_action_id,
        "action": None if action is None else _action_payload(action),
    }
    if action is not None:
        payload["order_type"] = "limit"
    store.record_decision(
        plan.decision,
        policy_payload=_decision_payload(plan),
        guard_payload=dict(guard),
        effective_action_payload=payload,
    )
    if action is not None:
        durable = _ensure_action(
            store,
            action,
            expected_holding_version=plan.expected_holding_version,
        )
        return _step_for_action(store, plan, durable)
    kind = AdditionStepKind.DECLINED if outcome == "declined" else AdditionStepKind.BLOCKED
    return AdditionStep(kind, plan.decision.decision_id, reason=reason)


def _blocked(
    store: PolicyExecutionStateStore,
    plan: AdditionPlan,
    reason: str,
    *,
    guard: Mapping[str, object] | None = None,
) -> AdditionStep:
    return _persist_outcome(
        store,
        plan,
        outcome="blocked",
        reason=reason,
        guard=guard or {"outcome": "blocked", "reason": reason},
    )


def start_addition(
    store: PolicyExecutionStateStore,
    plan: AdditionPlan,
    *,
    account: BrokerAccountSnapshot,
    portfolio_snapshot,
    maximum_balance_age: timedelta = timedelta(minutes=15),
    maximum_mark_age: timedelta = timedelta(minutes=15),
) -> AdditionStep:
    """Persist one whole-share addition sized from reconciled account commitments."""
    try:
        existing = store.load_decision_record(plan.decision.decision_id)
    except KeyError:
        existing = None
    if existing is not None:
        recorded_plan, _ = _plan_from_record(existing)
        if recorded_plan != plan:
            raise ValueError("fixed addition decision was replayed with different policy or source facts")
        step = load_addition_intention(store, plan.decision.decision_id)
        if step.kind is AdditionStepKind.BUY_DUE and step.action is not None:
            if existing.guard_payload.get("source_fingerprint_sha256") != _source_fingerprint(
                account, portfolio_snapshot
            ):
                projection = store.load_action_projection(step.action.logical_action_id)
                holding = store.load_holding_episode(plan.holding_episode_id)
                store.record_explicit_action_resolution(
                    step.action.logical_action_id,
                    resolution_reason="account or portfolio facts changed after addition authorization",
                    expected_action_version=projection.state_version,
                    expected_holding_version=holding.state_version,
                    observed_at=account.clock.valuation_time,
                )
                return load_addition_intention(store, plan.decision.decision_id)
        return step

    if plan.policy_decision != validate_add_on_decision(plan.add_on_snapshot, plan.policy_decision):
        return _blocked(store, plan, "add-on decision failed canonical host validation")
    if not plan.policy_decision.add:
        return _persist_outcome(
            store,
            plan,
            outcome="declined",
            reason=plan.policy_decision.reason_code,
            guard={"outcome": "policy_declined", "reason_code": plan.policy_decision.reason_code},
        )

    persistence_started = False
    try:
        deployment = plan.decision.deployment_identity
        if (
            portfolio_snapshot.deployment_identity != deployment
            or portfolio_snapshot.clock != plan.decision.clock
            or portfolio_snapshot.account_snapshot_id != plan.source_account_snapshot_id
            or portfolio_snapshot.portfolio_snapshot_id != plan.source_portfolio_snapshot_id
            or account.account_snapshot_id != plan.source_account_snapshot_id
            or account.source_namespace != portfolio_snapshot.source_namespace
            or account.paper_account_environment_id != deployment.paper_account_environment_id
            or not _account_clock_matches(plan, account)
            or account.positions is None
            or account.open_orders is None
        ):
            raise ValueError("addition account or portfolio facts do not match the fixed decision")
        holding = store.load_holding_episode(plan.holding_episode_id)
        if holding.state_version != plan.expected_holding_version:
            raise ValueError("holding changed after the fixed add-on decision")
        if holding.deployment_generation_id != deployment.deployment_generation_id:
            raise ValueError("holding belongs to a different deployment generation")
        if holding.remaining_quantity <= 0 or holding.addition_count >= _MAXIMUM_ADD_ONS_PER_HOLDING:
            raise ValueError("holding is flat or has reached its add-on count limit")
        if holding.pending_action_ids:
            raise ValueError("holding already has a pending action or stop update")
        if dict(holding.policy_flags).get("position_reconciliation_required"):
            raise ValueError("holding requires position reconciliation")
        if (
            holding.confirmed_protective_stop_price is None
            or holding.confirmed_stop_action_id is None
            or (holding.confirmed_stop_client_order_id is None and holding.confirmed_stop_broker_order_id is None)
        ):
            raise ValueError("addition requires confirmed protective-stop evidence")
        if holding.entry_price is None or holding.cost_basis is None:
            raise ValueError("addition requires known opening entry and aggregate cost basis")
        position = _position_for_holding(account, holding.broker_symbol)
        if Decimal(str(position.quantity)) != holding.remaining_quantity:
            raise ValueError("broker position quantity differs from the durable holding")
        price = Decimal(str(position.mark_price))
        stop_price = holding.confirmed_protective_stop_price
        if not price.is_finite() or price <= 0:
            raise ValueError("latest decision-time price is missing or invalid")
        if not stop_price.is_finite() or stop_price <= 0 or stop_price >= price:
            raise ValueError("confirmed protective stop is missing, invalid, or not below the decision-time price")
        if price < holding.entry_price:
            raise ValueError("addition price is below the original opening entry")
        if plan.add_on_snapshot.current_quantity != float(holding.remaining_quantity):
            raise ValueError("add-on snapshot quantity differs from the durable holding")
        if plan.add_on_snapshot.add_on_count != holding.addition_count:
            raise ValueError("add-on snapshot count differs from the durable holding")
        if plan.add_on_snapshot.entry_price != float(holding.entry_price):
            raise ValueError("add-on snapshot entry differs from the durable opening entry")
        if plan.add_on_snapshot.current_price != float(price):
            raise ValueError("add-on snapshot price is not the latest valid account decision-time price")
        if account.equity is None or account.equity <= 0:
            raise ValueError("account equity is unavailable for addition sizing")
        if account.cash is None or account.cash < 0:
            raise ValueError("account cash is unavailable for addition sizing")
        mark_age = account.clock.valuation_time - position.mark_observed_at
        if mark_age < timedelta(0) or mark_age > maximum_mark_age:
            raise ValueError("decision-time position price is stale")

        persistence_started = True
        reconciliation = _bound_reconciliation(
            store,
            plan,
            account,
            portfolio_snapshot,
            maximum_balance_age=maximum_balance_age,
            maximum_mark_age=maximum_mark_age,
        )
        persistence_started = False
        if not reconciliation.ready:
            persistence_started = True
            findings = tuple(f"{item.path}: {item.state} ({item.detail})" for item in reconciliation.findings)
            return _blocked(
                store,
                plan,
                "account commitments do not reconcile",
                guard={"outcome": "blocked", "findings": list(findings)},
            )
        if (
            reconciliation.equity is None
            or reconciliation.available_cash is None
            or reconciliation.total_committed_risk is None
        ):
            raise ValueError("reconciled equity, cash, or committed risk is unavailable")

        equity = Decimal(str(reconciliation.equity))
        available_cash = max(Decimal("0"), Decimal(str(reconciliation.available_cash)))
        committed_risk = max(Decimal("0"), Decimal(str(reconciliation.total_committed_risk)))
        risk_per_share = price - stop_price
        policy_risk_cap = equity * Decimal(str(plan.policy_decision.risk_fraction))
        current_position_risk = max(
            Decimal("0"), holding.cost_basis - stop_price
        ) * holding.remaining_quantity
        snapshot_position_risk = (
            Decimal(str(plan.add_on_snapshot.open_position_risk_fraction)) * equity
        )
        if abs(snapshot_position_risk - current_position_risk) > Decimal("0.000001"):
            raise ValueError("add-on snapshot stop risk differs from the aggregate cost basis and confirmed stop")
        position_risk_remaining = max(
            Decimal("0"),
            equity * _MAX_POSITION_RISK_FRACTION - current_position_risk,
        )
        remaining_stop_risk = min(policy_risk_cap, position_risk_remaining)
        current_notional = price * holding.remaining_quantity
        snapshot_notional = Decimal(str(plan.add_on_snapshot.current_notional_fraction)) * equity
        if abs(snapshot_notional - current_notional) > Decimal("0.000001"):
            raise ValueError("add-on snapshot notional differs from the current decision-time mark")
        remaining_notional: Decimal | None = None
        if plan.policy_decision.notional_fraction_cap is not None:
            remaining_notional = max(
                Decimal("0"),
                equity * Decimal(str(plan.policy_decision.notional_fraction_cap)) - current_notional,
            )
        quantity_caps = [available_cash / price, remaining_stop_risk / risk_per_share]
        if remaining_notional is not None:
            quantity_caps.append(remaining_notional / price)
        whole_shares = min(quantity_caps).to_integral_value(rounding=ROUND_DOWN)
        guard = {
            "outcome": "allow_addition" if whole_shares > 0 else "blocked_no_funded_share",
            "account_snapshot_id": account.account_snapshot_id,
            "portfolio_snapshot_id": portfolio_snapshot.portfolio_snapshot_id,
            "decision_time_price": str(price),
            "confirmed_stop_price": str(stop_price),
            "available_cash": str(available_cash),
            "total_committed_risk": str(committed_risk),
            "current_position_stop_risk": str(current_position_risk),
            "remaining_position_stop_risk": str(position_risk_remaining),
            "remaining_stop_risk": str(remaining_stop_risk),
            "current_position_notional": str(current_notional),
            "remaining_notional": None if remaining_notional is None else str(remaining_notional),
            "whole_share_quantity": str(whole_shares),
            "reserved_buy_cash": str(whole_shares * price),
            "reserved_buy_risk": str(whole_shares * risk_per_share),
            "source_fingerprint_sha256": _source_fingerprint(account, portfolio_snapshot),
        }
        if whole_shares <= 0:
            persistence_started = True
            return _blocked(store, plan, "cash, stop-risk, and notional caps do not fund one whole share", guard=guard)
        action = build_action_intent(
            decision=plan.decision,
            security_id=holding.security_id,
            broker_symbol=holding.broker_symbol,
            holding_episode_id=holding.holding_episode_id,
            role=ActionRole.ADDITION,
            side=OrderSide.BUY,
            requested_quantity=whole_shares,
            reservation_price=price,
            reservation_price_basis=f"decision_time_mark:{account.account_snapshot_id}",
            reservation_stop_price=stop_price,
            risk_per_unit=risk_per_share,
            risk_basis=f"confirmed_stop:{holding.confirmed_stop_action_id}",
        )
        persistence_started = True
        return _persist_outcome(store, plan, outcome="buy", reason=None, guard=guard, action=action)
    except (KeyError, TypeError, ValueError) as exc:
        if persistence_started:
            raise
        return _blocked(store, plan, str(exc))


def _execution_context(
    store: PolicyExecutionStateStore,
    decision_id: str,
) -> tuple[AdditionPlan, ActionIntent | None, AdditionStep]:
    step = load_addition_intention(store, decision_id)
    record = store.load_decision_record(decision_id)
    plan, payload = _plan_from_record(record)
    action = None
    if payload.get("outcome") == "buy":
        action = store.load_action_intent(str(payload["action_id"]))
    return plan, action, step


def _client_order_id(action: ActionIntent) -> str:
    return "policy-add-" + action.logical_action_id.rsplit(":", 1)[-1][:24]


def submit_addition(
    store: PolicyExecutionStateStore,
    decision_id: str,
    *,
    provider_id: str,
    observed_at,
    submit: Callable[..., AdditionSubmissionResult],
) -> AdditionStep:
    """Persist the client identity before the injected paper-broker submit call."""
    plan, action, step = _execution_context(store, decision_id)
    if action is None or action.status is not ActionStatus.INTENDED:
        return step
    effective = store.load_decision_record(decision_id).effective_action_payload
    if effective.get("action_id") != action.logical_action_id:
        raise ValueError("addition submission differs from its durable action")
    order_type = effective.get("order_type")
    if order_type not in {None, "limit"}:
        raise ValueError("addition submission has an unsupported fixed order type")
    current = store.load_action_projection(action.logical_action_id)
    client_id = _client_order_id(action)
    bound = store.bind_attempt_order_refs(
        action.logical_action_id,
        1,
        provider_id=provider_id,
        client_order_id=client_id,
        expected_action_version=current.state_version,
        observed_at=observed_at,
    )
    try:
        request = dict(
            symbol=action.broker_symbol,
            quantity=action.requested_quantity,
            limit_price=action.reservation_price,
            client_order_id=client_id,
        )
        if order_type is not None:
            request["order_type"] = order_type
        outcome = submit(**request)
    except Exception as exc:
        return AdditionStep(
            AdditionStepKind.WAITING_FOR_BUY,
            decision_id,
            store.load_action_intent(action.logical_action_id),
            reason=f"addition submit outcome is uncertain: {exc}",
        )
    if type(outcome) is not AdditionSubmissionResult:
        return AdditionStep(AdditionStepKind.WAITING_FOR_BUY, decision_id, action, reason="addition submit outcome is untyped")
    if outcome.outcome_uncertain or (outcome.success and not outcome.broker_order_id):
        return AdditionStep(
            AdditionStepKind.WAITING_FOR_BUY,
            decision_id,
            store.load_action_intent(action.logical_action_id),
            reason=outcome.error or "addition submit outcome is uncertain",
        )
    if outcome.success:
        current = store.load_action_projection(action.logical_action_id)
        store.bind_attempt_order_refs(
            action.logical_action_id,
            1,
            provider_id=provider_id,
            broker_order_id=outcome.broker_order_id,
            expected_action_version=current.state_version,
            observed_at=observed_at,
        )
        return AdditionStep(
            AdditionStepKind.WAITING_FOR_BUY,
            decision_id,
            store.load_action_intent(action.logical_action_id),
        )

    if outcome.broker_order_id:
        current = store.load_action_projection(action.logical_action_id)
        bound = store.bind_attempt_order_refs(
            action.logical_action_id,
            1,
            provider_id=provider_id,
            broker_order_id=outcome.broker_order_id,
            expected_action_version=current.state_version,
            observed_at=observed_at,
        )
    terminal = store.confirm_order_terminal(
        action.logical_action_id,
        1,
        terminal_status=ActionAttemptStatus.REJECTED,
        expected_action_version=bound.state_version,
        observed_at=observed_at,
    )
    holding = store.load_holding_episode(plan.holding_episode_id)
    store.record_explicit_action_resolution(
        action.logical_action_id,
        resolution_reason=outcome.error or "broker definitively rejected the addition without a fill",
        expected_action_version=terminal.state_version,
        expected_holding_version=holding.state_version,
        observed_at=observed_at,
    )
    return AdditionStep(
        AdditionStepKind.REJECTED,
        decision_id,
        store.load_action_intent(action.logical_action_id),
        reason=outcome.error,
    )


def record_addition_fill(
    store: PolicyExecutionStateStore,
    decision_id: str,
    *,
    provider_id: str,
    fill_event_id: str,
    cumulative_quantity: Decimal,
    cumulative_notional: Decimal,
    cumulative_fees: Decimal,
    observed_at,
) -> AdditionStep:
    plan, action, step = _execution_context(store, decision_id)
    if action is None or action.role is not ActionRole.ADDITION:
        return step
    projection = store.load_action_projection(action.logical_action_id)
    holding = store.load_holding_episode(plan.holding_episode_id)
    store.record_cumulative_fill(
        action.logical_action_id,
        1,
        provider_id=provider_id,
        fill_event_id=fill_event_id,
        cumulative_quantity=cumulative_quantity,
        cumulative_notional=cumulative_notional,
        cumulative_fees=cumulative_fees,
        payload_sha256=None,
        observed_at=observed_at,
        expected_action_version=projection.state_version,
        expected_holding_version=holding.state_version,
    )
    return _step_for_action(store, plan, store.load_action_intent(action.logical_action_id))


def _position_quantity_matches(account: BrokerAccountSnapshot, holding) -> bool:
    try:
        position = _position_for_holding(account, holding.broker_symbol)
    except ValueError:
        return False
    return Decimal(str(position.quantity)) == holding.remaining_quantity


def _resolve_terminal_addition(
    store: PolicyExecutionStateStore,
    plan: AdditionPlan,
    action: ActionIntent,
    *,
    account: BrokerAccountSnapshot,
    observed_at,
) -> AdditionStep:
    if account.positions is None or account.open_orders is None or not account.account_snapshot_id:
        return AdditionStep(AdditionStepKind.WAITING_FOR_BUY, plan.decision.decision_id, action, reason="terminal addition lacks a complete account observation")
    holding = store.load_holding_episode(plan.holding_episode_id)
    if not _position_quantity_matches(account, holding):
        return AdditionStep(AdditionStepKind.WAITING_FOR_BUY, plan.decision.decision_id, action, reason="broker position has not converged to confirmed addition fills")
    attempt = next(item for item in action.order_attempts if item.attempt_number == 1)
    client_refs = set((() if attempt.client_order_id is None else (attempt.client_order_id,)) + attempt.client_order_aliases)
    broker_refs = set((() if attempt.broker_order_id is None else (attempt.broker_order_id,)) + attempt.broker_order_aliases)
    matching = tuple(
        order for order in account.open_orders
        if (order.client_order_id in client_refs if order.client_order_id else False)
        or (order.broker_order_id in broker_refs if order.broker_order_id else False)
    )
    if len(matching) != 1:
        return AdditionStep(AdditionStepKind.WAITING_FOR_BUY, plan.decision.decision_id, action, reason="broker snapshot does not uniquely confirm the terminal addition order")
    order = matching[0]
    if (
        order.side != "buy"
        or order.symbol.strip().upper() != action.broker_symbol.upper()
        or order.holding_episode_id != plan.holding_episode_id
        or order.status not in _TERMINAL_ORDER_STATUSES
        or Decimal(str(order.requested_quantity)) != attempt.requested_quantity
        or Decimal(str(order.cumulative_filled_quantity)) != attempt.confirmed_filled_quantity
    ):
        return AdditionStep(AdditionStepKind.WAITING_FOR_BUY, plan.decision.decision_id, action, reason="terminal broker order facts differ from the durable addition attempt")
    if action.status not in {ActionStatus.REMAINDER_READY, ActionStatus.RECONCILIATION_REQUIRED}:
        return _step_for_action(store, plan, action)
    current = store.load_action_projection(action.logical_action_id)
    store.record_explicit_action_resolution(
        action.logical_action_id,
        resolution_reason=f"terminal addition and position reconciled by account snapshot {account.account_snapshot_id}",
        expected_action_version=current.state_version,
        expected_holding_version=holding.state_version,
        observed_at=observed_at,
    )
    return _step_for_action(store, plan, store.load_action_intent(action.logical_action_id))


def cancel_addition_remainder(
    store: PolicyExecutionStateStore,
    decision_id: str,
    *,
    provider_id: str,
    observed_at,
    cancel: Callable[..., AdditionCancelResult],
) -> AdditionStep:
    """Cancel a partial remainder before resizing protection; uncertain cancel freezes the holding."""
    plan, action, step = _execution_context(store, decision_id)
    if action is None:
        return step
    if action.status in {ActionStatus.FILLED, ActionStatus.RESOLVED}:
        return _step_for_action(store, plan, action)
    if action.status in {ActionStatus.CANCEL_REQUESTED, ActionStatus.RECONCILIATION_REQUIRED}:
        return AdditionStep(AdditionStepKind.WAITING_FOR_BUY, decision_id, action, reason="addition cancel or fill reconciliation remains unresolved")
    if action.status is ActionStatus.REMAINDER_READY:
        return AdditionStep(AdditionStepKind.WAITING_FOR_BUY, decision_id, action, reason="terminal addition awaits a matching account position and order observation")
    if action.status is not ActionStatus.PARTIALLY_FILLED:
        return AdditionStep(AdditionStepKind.WAITING_FOR_BUY, decision_id, action, reason="only a confirmed partial addition can be cancelled for protection resizing")
    current = store.load_action_projection(action.logical_action_id)
    requested = store.request_order_cancel(
        action.logical_action_id,
        1,
        expected_action_version=current.state_version,
        observed_at=observed_at,
    )
    attempt = next(item for item in requested.order_attempts if item.attempt_number == 1)
    try:
        outcome = cancel(
            symbol=action.broker_symbol,
            client_order_id=attempt.client_order_id,
            broker_order_id=attempt.broker_order_id,
            residual_quantity=requested.residual_quantity,
        )
    except Exception as exc:
        return AdditionStep(AdditionStepKind.WAITING_FOR_BUY, decision_id, store.load_action_intent(action.logical_action_id), reason=f"addition cancel outcome is uncertain: {exc}")
    if type(outcome) is not AdditionCancelResult or outcome.outcome_uncertain:
        return AdditionStep(AdditionStepKind.WAITING_FOR_BUY, decision_id, store.load_action_intent(action.logical_action_id), reason="addition cancel outcome remains uncertain")
    if outcome.terminal_status not in {ActionAttemptStatus.CANCELLED, ActionAttemptStatus.REJECTED}:
        return AdditionStep(AdditionStepKind.WAITING_FOR_BUY, decision_id, store.load_action_intent(action.logical_action_id), reason="broker did not confirm a terminal addition cancel")
    return confirm_addition_cancel(
        store,
        decision_id,
        provider_id=provider_id,
        result=outcome,
        observed_at=observed_at,
    )


def confirm_addition_cancel(
    store: PolicyExecutionStateStore,
    decision_id: str,
    *,
    provider_id: str,
    result: AdditionCancelResult,
    observed_at,
) -> AdditionStep:
    """Apply a later authoritative cancel/fill observation without resending cancel."""
    plan, action, step = _execution_context(store, decision_id)
    if action is None:
        return step
    if action.status in {ActionStatus.FILLED, ActionStatus.RESOLVED}:
        return _step_for_action(store, plan, action)
    if action.status not in {
        ActionStatus.PARTIALLY_FILLED,
        ActionStatus.CANCEL_REQUESTED,
        ActionStatus.RECONCILIATION_REQUIRED,
        ActionStatus.REMAINDER_READY,
    }:
        return AdditionStep(AdditionStepKind.WAITING_FOR_BUY, decision_id, action, reason="addition is not awaiting terminal broker facts")
    if type(result) is not AdditionCancelResult or result.outcome_uncertain:
        return AdditionStep(AdditionStepKind.WAITING_FOR_BUY, decision_id, action, reason="addition terminal observation remains uncertain")
    if result.terminal_status not in {
        ActionAttemptStatus.CANCELLED,
        ActionAttemptStatus.REJECTED,
        ActionAttemptStatus.FILLED,
    }:
        return AdditionStep(AdditionStepKind.WAITING_FOR_BUY, decision_id, action, reason="broker did not provide a terminal addition status")
    if (
        result.cumulative_quantity is None
        or result.cumulative_notional is None
        or result.cumulative_fees is None
        or result.account is None
    ):
        return AdditionStep(AdditionStepKind.WAITING_FOR_BUY, decision_id, action, reason="terminal addition result lacks final fills or account facts")
    current_action = store.load_action_projection(action.logical_action_id)
    holding = store.load_holding_episode(plan.holding_episode_id)
    store.record_cumulative_fill(
        action.logical_action_id,
        1,
        provider_id=provider_id,
        fill_event_id=result.fill_event_id or f"terminal:{action.logical_action_id}:{result.cumulative_quantity}",
        cumulative_quantity=result.cumulative_quantity,
        cumulative_notional=result.cumulative_notional,
        cumulative_fees=result.cumulative_fees,
        payload_sha256=None,
        observed_at=observed_at,
        expected_action_version=current_action.state_version,
        expected_holding_version=holding.state_version,
    )
    current = store.load_action_intent(action.logical_action_id)
    attempt = next(item for item in current.order_attempts if item.attempt_number == 1)
    if attempt.terminal_status is None:
        projection = store.load_action_projection(action.logical_action_id)
        current = store.confirm_order_terminal(
            action.logical_action_id,
            1,
            terminal_status=result.terminal_status,
            expected_action_version=projection.state_version,
            expected_holding_version=store.load_holding_episode(plan.holding_episode_id).state_version,
            observed_at=observed_at,
        )
    elif attempt.terminal_status is not result.terminal_status:
        return AdditionStep(AdditionStepKind.WAITING_FOR_BUY, decision_id, current, reason="broker terminal status conflicts with prior durable evidence")
    return _resolve_terminal_addition(
        store,
        plan,
        store.load_action_intent(action.logical_action_id),
        account=result.account,
        observed_at=observed_at,
    )


def resolve_addition_after_terminal(
    store: PolicyExecutionStateStore,
    decision_id: str,
    *,
    account: BrokerAccountSnapshot,
    observed_at,
) -> AdditionStep:
    """Finish reconciliation after a terminal order when the first account snapshot was stale."""
    plan, action, step = _execution_context(store, decision_id)
    if action is None:
        return step
    return _resolve_terminal_addition(store, plan, action, account=account, observed_at=observed_at)


def _protection_decision(plan: AdditionPlan, holding) -> DecisionIdentity:
    return DecisionIdentity.build(
        deployment=plan.decision.deployment_identity,
        clock=plan.decision.clock,
        snapshot_sha256=plan.decision.snapshot_sha256,
        category=DecisionCategory.ADDITION,
        subject_type=DecisionSubjectType.HOLDING,
        subject_id=holding.holding_episode_id,
        sequence=int(holding.state_version or 0) + 1,
    )


def _check_position_and_old_stop(
    plan: AdditionPlan,
    holding,
    account: BrokerAccountSnapshot,
) -> BrokerOrderFact:
    if (
        account.paper_account_environment_id != plan.decision.deployment_identity.paper_account_environment_id
        or account.positions is None
        or account.open_orders is None
        or not account.account_snapshot_id
    ):
        raise ValueError("protection replacement requires a complete current account snapshot")
    if not _position_quantity_matches(account, holding):
        raise ValueError("account position does not match the reconciled durable holding quantity")
    if holding.confirmed_protective_stop_price is None:
        raise ValueError("holding has no confirmed stop price to preserve")
    position = _position_for_holding(account, holding.broker_symbol)
    if Decimal(str(position.mark_price)) <= holding.confirmed_protective_stop_price:
        raise ValueError("latest holding price is not above the confirmed protective stop")
    old_refs = {
        value for value in (holding.confirmed_stop_client_order_id, holding.confirmed_stop_broker_order_id)
        if value
    }
    active = tuple(
        order for order in account.open_orders
        if order.purpose == "protective_stop"
        and order.holding_episode_id == holding.holding_episode_id
        and order.status in _ACTIVE_ORDER_STATUSES
    )
    if len(active) != 1:
        raise ValueError("account must show one active old protective stop before atomic replacement")
    old = active[0]
    if (
        old.side != "sell"
        or old.symbol.strip().upper() != holding.broker_symbol.upper()
        or old.stop_price is None
        or Decimal(str(old.stop_price)) != holding.confirmed_protective_stop_price
        or not old_refs.intersection({value for value in (old.client_order_id, old.broker_order_id) if value})
    ):
        raise ValueError("active protective stop differs from the durable confirmed stop")
    return old


def confirm_addition_protection(
    store: PolicyExecutionStateStore,
    decision_id: str,
    *,
    account: BrokerAccountSnapshot,
    observed_at,
    provider_id: str,
) -> AdditionStep:
    """Confirm a replacement only from exact broker position and stop-order facts."""
    if type(provider_id) is not str or not provider_id.strip():
        raise ValueError("addition protection requires a provider identity")
    plan, action, step = _execution_context(store, decision_id)
    if action is None or action.confirmed_filled_quantity <= 0:
        return step
    holding = store.load_holding_episode(plan.holding_episode_id)
    stop_action_id = holding.proposed_stop_action_id
    if stop_action_id is None or stop_action_id == holding.confirmed_stop_action_id:
        return _step_for_action(store, plan, action)
    intent = store.load_stop_update_intent(stop_action_id)
    decision_record = store.load_decision_record(intent.decision_id)
    effective = decision_record.effective_action_payload
    guarded_provider = decision_record.guard_payload.get("provider_id")
    expected_quantity = Decimal(str(decision_record.guard_payload.get("holding_quantity", "-1")))
    if (
        effective.get("kind") != _PROTECTION_KIND
        or effective.get("parent_decision_id") != plan.decision.decision_id
        or expected_quantity != holding.remaining_quantity
        or intent.holding_episode_id != holding.holding_episode_id
        or intent.requested_stop_price != holding.confirmed_protective_stop_price
        or guarded_provider != provider_id
    ):
        return AdditionStep(AdditionStepKind.WAITING_FOR_PROTECTION, decision_id, action, reason="proposed stop does not match this addition and current quantity")
    if account.positions is None or account.open_orders is None or not _position_quantity_matches(account, holding):
        return AdditionStep(AdditionStepKind.WAITING_FOR_PROTECTION, decision_id, action, reason="broker position has not converged to the addition quantity")
    active = tuple(
        order for order in account.open_orders
        if order.purpose == "protective_stop"
        and order.holding_episode_id == holding.holding_episode_id
        and order.status in _ACTIVE_ORDER_STATUSES
    )
    if len(active) != 1:
        return AdditionStep(AdditionStepKind.WAITING_FOR_PROTECTION, decision_id, action, reason="replacement is not the unique active protective stop")
    order = active[0]
    if (
        order.side != "sell"
        or order.symbol.strip().upper() != holding.broker_symbol.upper()
        or Decimal(str(order.requested_quantity)) != holding.remaining_quantity
        or Decimal(str(order.cumulative_filled_quantity)) != 0
        or order.stop_price is None
        or Decimal(str(order.stop_price)) != intent.requested_stop_price
        or not order.client_order_id
        or not order.broker_order_id
        or order.client_order_id == holding.confirmed_stop_client_order_id
        or order.broker_order_id == holding.confirmed_stop_broker_order_id
    ):
        return AdditionStep(AdditionStepKind.WAITING_FOR_PROTECTION, decision_id, action, reason="replacement stop facts do not match the exact holding quantity and preserved stop")
    store.confirm_protective_stop(
        intent,
        stop_price=intent.requested_stop_price,
        client_order_id=order.client_order_id,
        broker_order_id=order.broker_order_id,
        observed_at=observed_at,
        expected_holding_version=holding.state_version,
        provider_id=provider_id,
        requested_quantity=holding.remaining_quantity,
    )
    return _step_for_action(store, plan, store.load_action_intent(action.logical_action_id))


def replace_addition_protection(
    store: PolicyExecutionStateStore,
    decision_id: str,
    *,
    account: BrokerAccountSnapshot,
    observed_at,
    replace_stop: Callable[..., BrokerAccountSnapshot | None],
    provider_id: str,
) -> AdditionStep:
    """Replace protection atomically and confirm it from a fresh account snapshot.

    The injected callback must keep the prior stop active until the replacement is
    accepted. It returns a complete broker snapshot only after that atomic exchange
    is confirmed; exceptions or ``None`` leave the durable old stop unchanged and
    freeze the holding until ``confirm_addition_protection`` receives later facts.
    """
    if type(provider_id) is not str or not provider_id.strip():
        raise ValueError("addition protection requires a provider identity")
    plan, action, step = _execution_context(store, decision_id)
    if action is None or action.confirmed_filled_quantity <= 0:
        return step
    if action.status not in {ActionStatus.FILLED, ActionStatus.RESOLVED}:
        return AdditionStep(AdditionStepKind.WAITING_FOR_BUY, decision_id, action, reason="addition must be filled or terminally resolved before stop resize")
    holding = store.load_holding_episode(plan.holding_episode_id)
    if _confirmed_protection_matches_addition(
        store,
        plan,
        quantity=holding.remaining_quantity,
        stop_price=holding.confirmed_protective_stop_price or Decimal("0"),
    ):
        return AdditionStep(AdditionStepKind.PROTECTED, decision_id, action)
    if holding.pending_action_ids:
        return AdditionStep(AdditionStepKind.WAITING_FOR_PROTECTION, decision_id, action, reason="another holding action or stop update is pending")
    if holding.proposed_stop_action_id != holding.confirmed_stop_action_id:
        return AdditionStep(AdditionStepKind.WAITING_FOR_PROTECTION, decision_id, action, reason="prior stop replacement is unresolved")
    try:
        old_order = _check_position_and_old_stop(plan, holding, account)
        decision = _protection_decision(plan, holding)
        store.record_decision(
            decision,
            policy_payload={
                "parent_decision_id": decision_id,
                "action_id": action.logical_action_id,
                "source_action_id": action.logical_action_id,
            },
            guard_payload={
                "holding_quantity": str(holding.remaining_quantity),
                "stop_price": str(holding.confirmed_protective_stop_price),
                "account_snapshot_id": account.account_snapshot_id,
                "provider_id": provider_id,
            },
            effective_action_payload={
                "kind": _PROTECTION_KIND,
                "parent_decision_id": decision_id,
                "action_id": action.logical_action_id,
            },
        )
        intent = store.propose_stop_update(
            holding.holding_episode_id,
            decision=decision,
            stop_price=holding.confirmed_protective_stop_price,
            expected_holding_version=holding.state_version,
            observed_at=observed_at,
        )
    except (KeyError, ValueError) as exc:
        return AdditionStep(AdditionStepKind.WAITING_FOR_PROTECTION, decision_id, action, reason=str(exc))
    new_client = "policy-stop-" + intent.logical_action_id.rsplit(":", 1)[-1][:24]
    try:
        refreshed = replace_stop(
            old_order=old_order,
            symbol=holding.broker_symbol,
            quantity=holding.remaining_quantity,
            stop_price=intent.requested_stop_price,
            new_client_order_id=new_client,
        )
    except Exception as exc:
        return AdditionStep(AdditionStepKind.WAITING_FOR_PROTECTION, decision_id, action, reason=f"protective-stop replacement outcome is uncertain: {exc}")
    if refreshed is None:
        return AdditionStep(AdditionStepKind.WAITING_FOR_PROTECTION, decision_id, action, reason="protective-stop replacement outcome is uncertain")
    return confirm_addition_protection(
        store, decision_id, account=refreshed, observed_at=observed_at,
        provider_id=provider_id,
    )


__all__ = (
    "AdditionCancelResult",
    "AdditionPlan",
    "AdditionStep",
    "AdditionStepKind",
    "AdditionSubmissionResult",
    "cancel_addition_remainder",
    "confirm_addition_cancel",
    "confirm_addition_protection",
    "load_addition_intention",
    "record_addition_fill",
    "replace_addition_protection",
    "resolve_addition_after_terminal",
    "start_addition",
    "submit_addition",
)
