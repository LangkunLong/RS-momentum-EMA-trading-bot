"""Provider-independent value types for durable qualified-policy execution state.

Persistence and runtime integration are deliberately owned by the execution
store/workflow layer. This module defines stable identities and pure state
transitions so those layers can be implemented and tested independently.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Iterable


STATE_CONTRACT_VERSION = 1


class DecisionConflictError(ValueError):
    """Raised when one deployment/session slot has competing snapshots."""


class IdentityConflictError(ValueError):
    """Raised when an existing stable identity is presented with new facts."""


class ActionNotDueError(ValueError):
    """Raised when a session-bound action is evaluated before its session."""


class MissedExecutionSessionError(ValueError):
    """Raised when an action's exact execution session has passed."""


class PendingActionConflictError(ValueError):
    """Raised when a holding already has a competing pending action."""


class ActionRole(StrEnum):
    ENTRY = "entry"
    ADDITION = "addition"
    REPLACEMENT = "replacement"
    SCALE_OUT = "scale_out"
    CLOSE = "close"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class ActionStatus(StrEnum):
    INTENDED = "intended"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    CANCEL_REQUESTED = "cancel_requested"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    REMAINDER_READY = "remainder_ready"
    PARTIAL_INCOMPLETE = "partial_incomplete"
    FILLED = "filled"
    RESOLVED = "resolved"


class ActionAttemptStatus(StrEnum):
    INTENDED = "intended"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    CANCEL_REQUESTED = "cancel_requested"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class DecisionCategory(StrEnum):
    CANDIDATE_EVALUATION = "candidate_evaluation"
    ENTRY = "entry"
    CAPACITY = "capacity"
    ALLOCATION = "allocation"
    REPLACEMENT = "replacement"
    ADDITION = "addition"
    EXIT = "exit"


class DecisionSubjectType(StrEnum):
    CANDIDATE = "candidate"
    SECURITY = "security"
    HOLDING = "holding"
    PORTFOLIO = "portfolio"


_OPEN_STATUSES = frozenset(
    {
        ActionStatus.INTENDED,
        ActionStatus.SUBMITTED,
        ActionStatus.PARTIALLY_FILLED,
        ActionStatus.CANCEL_REQUESTED,
        ActionStatus.RECONCILIATION_REQUIRED,
        ActionStatus.REMAINDER_READY,
        ActionStatus.PARTIAL_INCOMPLETE,
    }
)
_TIER_COMPLETION_STATUSES = frozenset({ActionStatus.FILLED, ActionStatus.RESOLVED})
_TERMINAL_STATUSES = frozenset({ActionStatus.FILLED, ActionStatus.RESOLVED})


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _decimal(value: Decimal | int | str, name: str, *, optional: bool = False) -> Decimal | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{name} must use Decimal, int, or string precision")
    try:
        normalized = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not normalized.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return normalized


def _positive_decimal(value: Decimal | int | str, name: str) -> Decimal:
    normalized = _decimal(value, name)
    assert normalized is not None
    if normalized <= 0:
        raise ValueError(f"{name} must be positive")
    return normalized


def _nonnegative_decimal(value: Decimal | int | str, name: str) -> Decimal:
    normalized = _decimal(value, name)
    assert normalized is not None
    if normalized < 0:
        raise ValueError(f"{name} must be non-negative")
    return normalized


def _aware_datetime(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _utc_identity_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class DecisionClock:
    """Explicit completed-session and next-execution clock for one decision."""

    exchange_id: str
    decision_session: date
    as_of_cutoff_at: datetime
    next_execution_session: date
    account_valuation_session: date
    account_valuation_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchange_id", _text(self.exchange_id, "exchange_id"))
        if not isinstance(self.decision_session, date) or isinstance(self.decision_session, datetime):
            raise ValueError("decision_session must be a date")
        if not isinstance(self.next_execution_session, date) or isinstance(
            self.next_execution_session, datetime
        ):
            raise ValueError("next_execution_session must be a date")
        if not isinstance(self.account_valuation_session, date) or isinstance(
            self.account_valuation_session, datetime
        ):
            raise ValueError("account_valuation_session must be a date")
        if self.next_execution_session <= self.decision_session:
            raise ValueError("next_execution_session must follow decision_session")
        if self.account_valuation_session != self.next_execution_session:
            raise ValueError("account valuation session must match the declared execution opportunity")
        _aware_datetime(self.as_of_cutoff_at, "as_of_cutoff_at")
        _aware_datetime(self.account_valuation_at, "account_valuation_at")

    def identity_payload(self) -> dict[str, str]:
        return {
            "exchange_id": self.exchange_id,
            "decision_session": self.decision_session.isoformat(),
            "as_of_cutoff_at": _utc_identity_time(self.as_of_cutoff_at),
            "next_execution_session": self.next_execution_session.isoformat(),
            "account_valuation_session": self.account_valuation_session.isoformat(),
            "account_valuation_at": _utc_identity_time(self.account_valuation_at),
        }

    @property
    def clock_id(self) -> str:
        return f"clock:sha256:{_canonical_digest(self.identity_payload())}"


@dataclass(frozen=True, slots=True)
class PolicyDeploymentIdentity:
    """Immutable, separate identity dimensions for one paper deployment."""

    policy_artifact_id: str
    capability_manifest_id: str
    policy_interface_version: str
    feature_contract_id: str
    feature_calculator_id: str
    source_revision: str
    runtime_identity: str
    execution_profile_id: str
    paper_account_environment_id: str
    store_identity: str

    def __post_init__(self) -> None:
        for name in (
            "policy_artifact_id",
            "capability_manifest_id",
            "policy_interface_version",
            "feature_contract_id",
            "feature_calculator_id",
            "source_revision",
            "runtime_identity",
            "execution_profile_id",
            "paper_account_environment_id",
            "store_identity",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))

    def identity_payload(self) -> dict[str, str]:
        return {
            "policy_artifact_id": self.policy_artifact_id,
            "capability_manifest_id": self.capability_manifest_id,
            "policy_interface_version": self.policy_interface_version,
            "feature_contract_id": self.feature_contract_id,
            "feature_calculator_id": self.feature_calculator_id,
            "source_revision": self.source_revision,
            "runtime_identity": self.runtime_identity,
            "execution_profile_id": self.execution_profile_id,
            "paper_account_environment_id": self.paper_account_environment_id,
            "store_identity": self.store_identity,
        }

    @property
    def deployment_generation_id(self) -> str:
        return f"deployment:sha256:{_canonical_digest(self.identity_payload())}"


@dataclass(frozen=True, slots=True)
class DecisionIdentity:
    """Stable decision slot plus the snapshot-specific decision identity."""

    deployment_identity: PolicyDeploymentIdentity
    clock: DecisionClock
    snapshot_sha256: str
    category: DecisionCategory
    subject_type: DecisionSubjectType
    subject_id: str
    sequence: int | None = None
    deployment_generation_id: str = field(init=False)
    decision_slot_id: str = field(init=False)
    decision_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "deployment_generation_id", self.deployment_identity.deployment_generation_id)
        digest = _text(self.snapshot_sha256, "snapshot_sha256").lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("snapshot_sha256 must be a 64-character SHA-256 digest")
        object.__setattr__(self, "snapshot_sha256", digest)
        object.__setattr__(self, "category", DecisionCategory(self.category))
        object.__setattr__(self, "subject_type", DecisionSubjectType(self.subject_type))
        object.__setattr__(self, "subject_id", _text(self.subject_id, "subject_id"))
        if self.sequence is not None and (
            not isinstance(self.sequence, int) or isinstance(self.sequence, bool) or self.sequence < 1
        ):
            raise ValueError("sequence must be a positive integer when present")
        slot_payload = {
            "deployment_generation_id": self.deployment_generation_id,
            "exchange_id": self.clock.exchange_id,
            "decision_session": self.clock.decision_session.isoformat(),
            "category": self.category.value,
            "subject_type": self.subject_type.value,
            "subject_id": self.subject_id,
            "sequence": self.sequence,
        }
        slot_id = f"decision-slot:sha256:{_canonical_digest(slot_payload)}"
        object.__setattr__(self, "decision_slot_id", slot_id)
        object.__setattr__(
            self,
            "decision_id",
            f"decision:sha256:{_canonical_digest({**slot_payload, 'clock': self.clock.identity_payload(), 'snapshot_sha256': digest})}",
        )

    @classmethod
    def build(
        cls,
        *,
        deployment: PolicyDeploymentIdentity,
        clock: DecisionClock,
        snapshot_sha256: str,
        category: DecisionCategory,
        subject_type: DecisionSubjectType,
        subject_id: str,
        sequence: int | None = None,
    ) -> DecisionIdentity:
        return cls(
            deployment_identity=deployment,
            clock=clock,
            snapshot_sha256=snapshot_sha256,
            category=category,
            subject_type=subject_type,
            subject_id=subject_id,
            sequence=sequence,
        )


def assert_same_decision_slot_consistent(
    existing: DecisionIdentity,
    proposed: DecisionIdentity,
) -> None:
    """Reject a second snapshot for the same deployment/session decision slot."""
    if existing.decision_slot_id == proposed.decision_slot_id and existing.decision_id != proposed.decision_id:
        raise DecisionConflictError(
            "decision slot already has a different snapshot identity"
        )


@dataclass(frozen=True, slots=True)
class ActionOrderAttempt:
    """One broker submission attempt under one durable logical action."""

    attempt_number: int
    requested_quantity: Decimal
    confirmed_filled_quantity: Decimal = Decimal("0")
    status: ActionAttemptStatus = ActionAttemptStatus.INTENDED
    client_order_id: str | None = None
    broker_order_id: str | None = None
    terminal_status: ActionAttemptStatus | None = None
    client_order_aliases: tuple[str, ...] = ()
    broker_order_aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.attempt_number not in {1, 2}:
            raise ValueError("attempt_number must be 1 or the single remainder attempt 2")
        requested = _positive_decimal(self.requested_quantity, "attempt requested_quantity")
        filled = _nonnegative_decimal(self.confirmed_filled_quantity, "attempt confirmed_filled_quantity")
        status = ActionAttemptStatus(self.status)
        terminal = None if self.terminal_status is None else ActionAttemptStatus(self.terminal_status)
        if terminal is not None and terminal not in {
            ActionAttemptStatus.CANCELLED,
            ActionAttemptStatus.REJECTED,
            ActionAttemptStatus.FILLED,
        }:
            raise ValueError("terminal_status must preserve a broker-confirmed terminal result")
        if status in {ActionAttemptStatus.CANCELLED, ActionAttemptStatus.REJECTED, ActionAttemptStatus.FILLED}:
            if terminal is not None and terminal is not status:
                raise ValueError("terminal attempt status conflicts with its preserved terminal result")
            terminal = status
        if filled > requested and status is not ActionAttemptStatus.RECONCILIATION_REQUIRED and terminal is None:
            raise ValueError("attempt confirmed fill above its target requires terminal or reconciliation evidence")
        object.__setattr__(self, "requested_quantity", requested)
        object.__setattr__(self, "confirmed_filled_quantity", filled)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "terminal_status", terminal)
        for name in ("client_order_id", "broker_order_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _text(value, name))
        for primary_name, aliases_name in (
            ("client_order_id", "client_order_aliases"),
            ("broker_order_id", "broker_order_aliases"),
        ):
            primary = getattr(self, primary_name)
            aliases = tuple(_text(value, aliases_name) for value in getattr(self, aliases_name))
            if len(aliases) != len(set(aliases)) or (primary is not None and primary in aliases):
                raise ValueError(f"{aliases_name} must contain unique aliases distinct from the primary reference")
            object.__setattr__(self, aliases_name, aliases)
        if status is ActionAttemptStatus.FILLED and filled < requested:
            raise ValueError("filled attempt must meet or exceed its requested quantity")

    @property
    def all_client_order_ids(self) -> tuple[str, ...]:
        return (() if self.client_order_id is None else (self.client_order_id,)) + self.client_order_aliases

    @property
    def all_broker_order_ids(self) -> tuple[str, ...]:
        return (() if self.broker_order_id is None else (self.broker_order_id,)) + self.broker_order_aliases


@dataclass(frozen=True, slots=True)
class ActionIntent:
    """Stable logical action intent with cumulative, replay-safe fill state."""

    decision: DecisionIdentity
    security_id: str
    broker_symbol: str
    role: ActionRole
    side: OrderSide
    requested_quantity: Decimal
    holding_episode_id: str | None = None
    confirmed_filled_quantity: Decimal = Decimal("0")
    status: ActionStatus = ActionStatus.INTENDED
    reservation_price: Decimal | None = None
    reservation_price_basis: str | None = None
    reservation_stop_price: Decimal | None = None
    risk_per_unit: Decimal | None = None
    risk_basis: str | None = None
    exit_tier: int | None = None
    snapshot_original_quantity: Decimal | None = None
    fraction_of_original_quantity: Decimal | None = None
    rounding_rule_id: str | None = None
    order_attempts: tuple[ActionOrderAttempt, ...] = ()
    resolution_reason: str | None = None
    deployment_generation_id: str = field(init=False)
    logical_action_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "deployment_generation_id", self.decision.deployment_generation_id)
        object.__setattr__(self, "security_id", _text(self.security_id, "security_id"))
        object.__setattr__(self, "broker_symbol", _text(self.broker_symbol, "broker_symbol").upper())
        if self.holding_episode_id is not None:
            object.__setattr__(self, "holding_episode_id", _text(self.holding_episode_id, "holding_episode_id"))
        try:
            object.__setattr__(self, "role", ActionRole(self.role))
            object.__setattr__(self, "side", OrderSide(self.side))
            object.__setattr__(self, "status", ActionStatus(self.status))
        except ValueError as exc:
            raise ValueError("action role, side, or status is unsupported") from exc

        requested = _positive_decimal(self.requested_quantity, "requested_quantity")
        confirmed = _nonnegative_decimal(self.confirmed_filled_quantity, "confirmed_filled_quantity")
        has_resolution_evidence = (
            self.status is ActionStatus.RESOLVED
            and isinstance(self.resolution_reason, str)
            and bool(self.resolution_reason.strip())
        )
        if confirmed > requested and self.status is not ActionStatus.RECONCILIATION_REQUIRED and not has_resolution_evidence:
            raise ValueError("confirmed fill quantity cannot exceed requested quantity")
        object.__setattr__(self, "requested_quantity", requested)
        object.__setattr__(self, "confirmed_filled_quantity", confirmed)

        price = _decimal(self.reservation_price, "reservation_price", optional=True)
        if price is not None:
            if price <= 0 or self.side is not OrderSide.BUY:
                raise ValueError("only buy actions may carry a positive reservation price")
            object.__setattr__(self, "reservation_price_basis", _text(self.reservation_price_basis, "reservation_price_basis"))
        elif self.reservation_price_basis is not None:
            raise ValueError("reservation_price_basis requires a reservation_price")
        object.__setattr__(self, "reservation_price", price)

        stop = _decimal(self.reservation_stop_price, "reservation_stop_price", optional=True)
        if stop is not None and (stop <= 0 or self.side is not OrderSide.BUY):
            raise ValueError("reservation_stop_price must be positive and belong to a buy action")
        object.__setattr__(self, "reservation_stop_price", stop)
        risk = _decimal(self.risk_per_unit, "risk_per_unit", optional=True)
        if risk is not None and risk <= 0:
            raise ValueError("risk_per_unit must be positive when available")
        if risk is not None:
            if self.side is not OrderSide.BUY:
                raise ValueError("only risk-increasing buy actions carry incremental risk")
            object.__setattr__(self, "risk_basis", _text(self.risk_basis, "risk_basis"))
        elif self.risk_basis is not None:
            raise ValueError("risk_basis requires a risk_per_unit")
        object.__setattr__(self, "risk_per_unit", risk)

        if self.role in {ActionRole.ADDITION, ActionRole.SCALE_OUT, ActionRole.CLOSE} and not self.holding_episode_id:
            raise ValueError(f"{self.role.value} actions require a holding_episode_id")
        expected_side = (
            OrderSide.BUY
            if self.role in {ActionRole.ENTRY, ActionRole.ADDITION, ActionRole.REPLACEMENT}
            else OrderSide.SELL
        )
        if self.side is not expected_side:
            raise ValueError(f"{self.role.value} actions require side={expected_side.value}")

        if self.role is ActionRole.SCALE_OUT:
            if not isinstance(self.exit_tier, int) or isinstance(self.exit_tier, bool) or self.exit_tier < 1:
                raise ValueError("scale-out actions require a positive exit_tier")
            original = _positive_decimal(self.snapshot_original_quantity, "snapshot_original_quantity")
            fraction = _positive_decimal(self.fraction_of_original_quantity, "fraction_of_original_quantity")
            if fraction > 1 or requested > original:
                raise ValueError("scale-out fraction and rounded target must fit the frozen snapshot quantity")
            object.__setattr__(self, "snapshot_original_quantity", original)
            object.__setattr__(self, "fraction_of_original_quantity", fraction)
            object.__setattr__(self, "rounding_rule_id", _text(self.rounding_rule_id, "rounding_rule_id"))
        elif any(
            value is not None
            for value in (
                self.exit_tier,
                self.snapshot_original_quantity,
                self.fraction_of_original_quantity,
                self.rounding_rule_id,
            )
        ):
            raise ValueError("only scale-out actions may carry tier quantity fields")

        attempts = tuple(self.order_attempts)
        if not attempts:
            first_status = _attempt_status_for_action(self.status)
            first = ActionOrderAttempt(
                attempt_number=1,
                requested_quantity=requested,
                confirmed_filled_quantity=confirmed,
                status=first_status,
            )
            attempts = (first,)
        if tuple(attempt.attempt_number for attempt in attempts) not in {(1,), (1, 2)}:
            raise ValueError("action attempts must be the initial attempt and at most one remainder")
        if attempts[0].requested_quantity != requested:
            raise ValueError("first attempt target must match the immutable logical action target")
        if len(attempts) == 2:
            if attempts[1].requested_quantity > requested:
                raise ValueError("remainder attempt cannot exceed the immutable logical action target")
            if attempts[0].terminal_status not in {ActionAttemptStatus.CANCELLED, ActionAttemptStatus.REJECTED}:
                raise ValueError("remainder attempt requires a broker-confirmed terminal first attempt")
        aggregate_fills = sum((attempt.confirmed_filled_quantity for attempt in attempts), Decimal("0"))
        if aggregate_fills != confirmed:
            raise ValueError("action cumulative fill must equal the sum of its attempt fills")
        if aggregate_fills > requested and self.status is not ActionStatus.RECONCILIATION_REQUIRED and not has_resolution_evidence:
            raise ValueError("aggregate fills above target require reconciliation-required action state")
        object.__setattr__(self, "order_attempts", attempts)

        if self.status is ActionStatus.FILLED and confirmed != requested:
            raise ValueError("filled action must match its target quantity")
        if self.status is ActionStatus.RESOLVED:
            object.__setattr__(self, "resolution_reason", _text(self.resolution_reason, "resolution_reason"))
        elif self.resolution_reason is not None:
            object.__setattr__(self, "resolution_reason", _text(self.resolution_reason, "resolution_reason"))

        identity = {
            "deployment_generation_id": self.deployment_generation_id,
            "security_id": self.security_id,
            "broker_symbol": self.broker_symbol,
            "role": self.role.value,
        }
        if self.role is ActionRole.SCALE_OUT:
            identity["holding_episode_id"] = self.holding_episode_id
            identity["exit_tier"] = self.exit_tier
        else:
            if self.role is not ActionRole.ENTRY:
                identity["holding_episode_id"] = self.holding_episode_id
            identity["decision_id"] = self.decision.decision_id
        object.__setattr__(self, "logical_action_id", f"action:sha256:{_canonical_digest(identity)}")

    @property
    def residual_quantity(self) -> Decimal:
        return max(Decimal("0"), self.requested_quantity - self.confirmed_filled_quantity)

    @property
    def is_open(self) -> bool:
        return self.status in _OPEN_STATUSES

    def immutable_payload(self) -> dict[str, object]:
        return {
            "deployment_generation_id": self.deployment_generation_id,
            "decision_id": self.decision.decision_id,
            "security_id": self.security_id,
            "holding_episode_id": None if self.role is ActionRole.ENTRY else self.holding_episode_id,
            "role": self.role.value,
            "side": self.side.value,
            "requested_quantity": str(self.requested_quantity),
            "reservation_price": None if self.reservation_price is None else str(self.reservation_price),
            "reservation_price_basis": self.reservation_price_basis,
            "reservation_stop_price": None if self.reservation_stop_price is None else str(self.reservation_stop_price),
            "risk_per_unit": None if self.risk_per_unit is None else str(self.risk_per_unit),
            "risk_basis": self.risk_basis,
            "exit_tier": self.exit_tier,
            "snapshot_original_quantity": (
                None if self.snapshot_original_quantity is None else str(self.snapshot_original_quantity)
            ),
            "fraction_of_original_quantity": (
                None if self.fraction_of_original_quantity is None else str(self.fraction_of_original_quantity)
            ),
            "rounding_rule_id": self.rounding_rule_id,
        }


def _attempt_status_for_action(status: ActionStatus) -> ActionAttemptStatus:
    return {
        ActionStatus.INTENDED: ActionAttemptStatus.INTENDED,
        ActionStatus.SUBMITTED: ActionAttemptStatus.SUBMITTED,
        ActionStatus.PARTIALLY_FILLED: ActionAttemptStatus.PARTIALLY_FILLED,
        ActionStatus.CANCEL_REQUESTED: ActionAttemptStatus.CANCEL_REQUESTED,
        ActionStatus.RECONCILIATION_REQUIRED: ActionAttemptStatus.RECONCILIATION_REQUIRED,
        ActionStatus.REMAINDER_READY: ActionAttemptStatus.CANCELLED,
        ActionStatus.PARTIAL_INCOMPLETE: ActionAttemptStatus.CANCELLED,
        ActionStatus.FILLED: ActionAttemptStatus.FILLED,
        ActionStatus.RESOLVED: ActionAttemptStatus.CANCELLED,
    }[status]


def _unique_refs(values: Iterable[str], name: str) -> tuple[str, ...]:
    normalized = tuple(_text(value, name) for value in values)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{name} cannot contain duplicates")
    return normalized


def build_action_intent(
    *,
    decision: DecisionIdentity,
    security_id: str,
    broker_symbol: str,
    role: ActionRole,
    side: OrderSide,
    requested_quantity: Decimal,
    holding_episode_id: str | None = None,
    status: ActionStatus = ActionStatus.INTENDED,
    reservation_price: Decimal | None = None,
    reservation_price_basis: str | None = None,
    reservation_stop_price: Decimal | None = None,
    risk_per_unit: Decimal | None = None,
    risk_basis: str | None = None,
    exit_tier: int | None = None,
    snapshot_original_quantity: Decimal | None = None,
    fraction_of_original_quantity: Decimal | None = None,
    rounding_rule_id: str | None = None,
) -> ActionIntent:
    return ActionIntent(
        decision=decision,
        security_id=security_id,
        broker_symbol=broker_symbol,
        role=role,
        side=side,
        requested_quantity=requested_quantity,
        holding_episode_id=holding_episode_id,
        status=status,
        reservation_price=reservation_price,
        reservation_price_basis=reservation_price_basis,
        reservation_stop_price=reservation_stop_price,
        risk_per_unit=risk_per_unit,
        risk_basis=risk_basis,
        exit_tier=exit_tier,
        snapshot_original_quantity=snapshot_original_quantity,
        fraction_of_original_quantity=fraction_of_original_quantity,
        rounding_rule_id=rounding_rule_id,
    )


def assert_same_logical_action(existing: ActionIntent, proposed: ActionIntent) -> None:
    if existing.logical_action_id != proposed.logical_action_id:
        raise IdentityConflictError("action identities do not match")
    if existing.immutable_payload() != proposed.immutable_payload():
        raise IdentityConflictError("logical action has a conflicting immutable payload")


def apply_cumulative_fill(intent: ActionIntent, cumulative_filled_quantity: Decimal) -> ActionIntent:
    """Apply an attempt-1 watermark through the numbered API's uncertainty rules."""
    return apply_attempt_cumulative_fill(intent, 1, cumulative_filled_quantity)


def apply_attempt_cumulative_fill(
    intent: ActionIntent,
    attempt_number: int,
    cumulative_filled_quantity: Decimal,
) -> ActionIntent:
    cumulative = _nonnegative_decimal(cumulative_filled_quantity, "cumulative_filled_quantity")
    attempt = _get_attempt(intent, attempt_number)
    if cumulative < attempt.confirmed_filled_quantity:
        raise ValueError("attempt cumulative fill cannot decrease")
    if cumulative == attempt.confirmed_filled_quantity:
        return intent
    terminal_conflict = attempt.status in {
        ActionAttemptStatus.CANCELLED,
        ActionAttemptStatus.REJECTED,
        ActionAttemptStatus.FILLED,
    }
    preserve_attempt_uncertainty = attempt.status in {
        ActionAttemptStatus.CANCEL_REQUESTED,
        ActionAttemptStatus.RECONCILIATION_REQUIRED,
    }
    requires_reconciliation = (
        terminal_conflict
        or cumulative > attempt.requested_quantity
        or intent.status is ActionStatus.RESOLVED
    )
    if requires_reconciliation:
        attempt_status = ActionAttemptStatus.RECONCILIATION_REQUIRED
    elif preserve_attempt_uncertainty:
        attempt_status = attempt.status
    else:
        attempt_status = (
            ActionAttemptStatus.FILLED
            if cumulative == attempt.requested_quantity
            else ActionAttemptStatus.PARTIALLY_FILLED
        )
    updated_attempt = replace(
        attempt,
        confirmed_filled_quantity=cumulative,
        status=attempt_status,
    )
    attempts = _replace_attempt(intent, updated_attempt)
    aggregate = sum((item.confirmed_filled_quantity for item in attempts), Decimal("0"))
    if requires_reconciliation or aggregate > intent.requested_quantity:
        action_status = ActionStatus.RECONCILIATION_REQUIRED
    elif intent.status in {ActionStatus.CANCEL_REQUESTED, ActionStatus.RECONCILIATION_REQUIRED}:
        action_status = intent.status
    else:
        action_status = ActionStatus.FILLED if aggregate == intent.requested_quantity else ActionStatus.PARTIALLY_FILLED
    return replace(
        intent,
        confirmed_filled_quantity=aggregate,
        status=action_status,
        order_attempts=attempts,
    )


def request_attempt_cancel(intent: ActionIntent, attempt_number: int) -> ActionIntent:
    attempt = _get_attempt(intent, attempt_number)
    if attempt.status is ActionAttemptStatus.CANCEL_REQUESTED:
        return intent
    if attempt.status in {ActionAttemptStatus.CANCELLED, ActionAttemptStatus.REJECTED, ActionAttemptStatus.FILLED}:
        raise ValueError("terminal attempt cannot receive a cancel request")
    updated_attempt = replace(attempt, status=ActionAttemptStatus.CANCEL_REQUESTED)
    return replace(
        intent,
        status=ActionStatus.CANCEL_REQUESTED,
        order_attempts=_replace_attempt(intent, updated_attempt),
    )


def confirm_attempt_terminal(
    intent: ActionIntent,
    attempt_number: int,
    *,
    status: ActionAttemptStatus,
) -> ActionIntent:
    status = ActionAttemptStatus(status)
    if status not in {ActionAttemptStatus.CANCELLED, ActionAttemptStatus.REJECTED, ActionAttemptStatus.FILLED}:
        raise ValueError("broker terminal attempt status must be cancelled, rejected, or filled")
    attempt = _get_attempt(intent, attempt_number)
    if attempt.status in {ActionAttemptStatus.CANCELLED, ActionAttemptStatus.REJECTED, ActionAttemptStatus.FILLED}:
        if attempt.status is status:
            return intent
        raise IdentityConflictError("order attempt already has a different terminal status")
    if status is ActionAttemptStatus.FILLED and attempt.confirmed_filled_quantity < attempt.requested_quantity:
        raise ValueError("broker cannot confirm filled before the requested attempt quantity is confirmed")
    updated_attempt = replace(attempt, status=status, terminal_status=status)
    attempts = _replace_attempt(intent, updated_attempt)
    aggregate = sum((item.confirmed_filled_quantity for item in attempts), Decimal("0"))
    live_attempts = any(
        item.status not in {ActionAttemptStatus.CANCELLED, ActionAttemptStatus.REJECTED, ActionAttemptStatus.FILLED}
        for item in attempts
    )
    if aggregate > intent.requested_quantity:
        action_status = ActionStatus.RECONCILIATION_REQUIRED
    elif live_attempts and intent.status is ActionStatus.RECONCILIATION_REQUIRED:
        action_status = ActionStatus.RECONCILIATION_REQUIRED
    elif live_attempts and aggregate == intent.requested_quantity:
        action_status = ActionStatus.RECONCILIATION_REQUIRED
    elif aggregate == intent.requested_quantity:
        action_status = ActionStatus.FILLED
    elif len(attempts) == 2 and intent.status is ActionStatus.RECONCILIATION_REQUIRED:
        action_status = ActionStatus.RECONCILIATION_REQUIRED
    elif attempt_number == 1:
        action_status = ActionStatus.REMAINDER_READY
    else:
        action_status = ActionStatus.PARTIAL_INCOMPLETE
    return replace(
        intent,
        confirmed_filled_quantity=aggregate,
        status=action_status,
        order_attempts=attempts,
    )


def create_single_remainder_attempt(intent: ActionIntent) -> ActionIntent:
    if intent.role is not ActionRole.SCALE_OUT:
        raise ValueError("the bounded remainder rule applies to a scale-out tier")
    if len(intent.order_attempts) != 1:
        raise ValueError("only one remainder attempt is permitted")
    if intent.status is not ActionStatus.REMAINDER_READY:
        raise ValueError("remainder requires a broker-confirmed terminal first attempt")
    residual = intent.residual_quantity
    if residual <= 0:
        raise ValueError("fully filled action has no remainder")
    first = intent.order_attempts[0]
    if first.status not in {ActionAttemptStatus.CANCELLED, ActionAttemptStatus.REJECTED}:
        raise ValueError("remainder requires a broker-confirmed terminal first attempt")
    second = ActionOrderAttempt(
        attempt_number=2,
        requested_quantity=residual,
    )
    return replace(
        intent,
        status=ActionStatus.INTENDED,
        order_attempts=(first, second),
    )


def bind_attempt_order_refs(
    intent: ActionIntent,
    *,
    attempt_number: int,
    client_order_id: str | None = None,
    broker_order_id: str | None = None,
) -> ActionIntent:
    attempt = _get_attempt(intent, attempt_number)
    client = attempt.client_order_id if client_order_id is None else _text(client_order_id, "client_order_id")
    broker = attempt.broker_order_id if broker_order_id is None else _text(broker_order_id, "broker_order_id")
    if attempt.client_order_id is not None and client != attempt.client_order_id:
        raise IdentityConflictError("attempt client order identity cannot change")
    if attempt.broker_order_id is not None and broker != attempt.broker_order_id:
        raise IdentityConflictError("attempt broker order identity cannot change")
    for other in intent.order_attempts:
        if other.attempt_number == attempt_number:
            continue
        if client is not None and client in other.all_client_order_ids:
            raise IdentityConflictError("client order identity already belongs to another attempt")
        if broker is not None and broker in other.all_broker_order_ids:
            raise IdentityConflictError("broker order identity already belongs to another attempt")
    return replace(
        intent,
        order_attempts=_replace_attempt(
            intent,
            replace(attempt, client_order_id=client, broker_order_id=broker),
        ),
    )


def add_attempt_order_aliases(
    intent: ActionIntent,
    *,
    attempt_number: int,
    client_order_id: str | None = None,
    broker_order_id: str | None = None,
) -> ActionIntent:
    """Retain additional provider-scoped aliases without replacing attempt identity."""
    attempt = _get_attempt(intent, attempt_number)
    client_aliases = list(attempt.client_order_aliases)
    broker_aliases = list(attempt.broker_order_aliases)
    if client_order_id is not None:
        client = _text(client_order_id, "client_order_id")
        if client != attempt.client_order_id and client not in client_aliases:
            client_aliases.append(client)
    if broker_order_id is not None:
        broker = _text(broker_order_id, "broker_order_id")
        if broker != attempt.broker_order_id and broker not in broker_aliases:
            broker_aliases.append(broker)
    if tuple(client_aliases) == attempt.client_order_aliases and tuple(broker_aliases) == attempt.broker_order_aliases:
        return intent
    return replace(
        intent,
        order_attempts=_replace_attempt(
            intent,
            replace(
                attempt,
                client_order_aliases=tuple(client_aliases),
                broker_order_aliases=tuple(broker_aliases),
            ),
        ),
    )


def _get_attempt(intent: ActionIntent, attempt_number: int) -> ActionOrderAttempt:
    for attempt in intent.order_attempts:
        if attempt.attempt_number == attempt_number:
            return attempt
    raise ValueError(f"action has no order attempt {attempt_number}")


def _replace_attempt(
    intent: ActionIntent,
    replacement_attempt: ActionOrderAttempt,
) -> tuple[ActionOrderAttempt, ...]:
    return tuple(
        replacement_attempt if attempt.attempt_number == replacement_attempt.attempt_number else attempt
        for attempt in intent.order_attempts
    )


def resolve_action(
    intent: ActionIntent,
    *,
    status: ActionStatus,
    resolution_reason: str | None = None,
) -> ActionIntent:
    """Record an explicit resolution after broker/order/position reconciliation."""
    status = ActionStatus(status)
    if status is not ActionStatus.RESOLVED:
        raise ValueError("only an explicit reconciled resolution can resolve an action")
    if intent.status is ActionStatus.FILLED:
        return intent
    reason = _text(resolution_reason, "resolution_reason")
    if any(
        attempt.status not in {ActionAttemptStatus.CANCELLED, ActionAttemptStatus.REJECTED, ActionAttemptStatus.FILLED}
        for attempt in intent.order_attempts
    ):
        raise ValueError("all issued order attempts must be terminal before action reconciliation")
    if intent.status is ActionStatus.RESOLVED:
        if status is ActionStatus.RESOLVED and reason == intent.resolution_reason:
            return intent
        raise ValueError("explicitly resolved action cannot be changed")
    return replace(intent, status=status, resolution_reason=reason)


def assert_execution_session(intent: ActionIntent, session: date) -> None:
    """Require the exact next session recorded by the decision clock."""
    if not isinstance(session, date) or isinstance(session, datetime):
        raise ValueError("execution session must be a date")
    target = intent.decision.clock.next_execution_session
    if session < target:
        raise ActionNotDueError(f"action is bound to execution session {target.isoformat()}")
    if session > target:
        raise MissedExecutionSessionError(
            f"action missed execution session {target.isoformat()} and cannot be retargeted"
        )


@dataclass(frozen=True, slots=True)
class HoldingEpisode:
    """Generation-pinned holding quantity, risk marks, and action watermarks."""

    deployment_generation_id: str
    security_id: str
    symbol: str
    opening_action_id: str
    initial_filled_quantity: Decimal
    remaining_quantity: Decimal
    opening_later_fills_quantity: Decimal = Decimal("0")
    completed_additions_quantity: Decimal = Decimal("0")
    addition_count: int = 0
    entry_price: Decimal | None = None
    cost_basis: Decimal | None = None
    realized_pnl: Decimal | None = None
    committed_risk: Decimal | None = None
    committed_risk_basis: str | None = None
    proposed_stop_price: Decimal | None = None
    proposed_stop_action_id: str | None = None
    confirmed_protective_stop_price: Decimal | None = None
    confirmed_stop_action_id: str | None = None
    confirmed_stop_client_order_id: str | None = None
    confirmed_stop_broker_order_id: str | None = None
    confirmed_stop_observed_at: datetime | None = None
    peak_price: Decimal | None = None
    valuation_at: datetime | None = None
    last_accepted_session: date | None = None
    policy_flags: tuple[tuple[str, str], ...] = ()
    broker_symbol: str | None = None
    last_exit_tier: int = 0
    pending_action_ids: tuple[str, ...] = ()
    applied_action_fill_watermarks: tuple[tuple[str, Decimal], ...] = ()
    completed_addition_action_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("deployment_generation_id", "security_id", "opening_action_id"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        symbol = _text(self.symbol, "symbol").upper()
        object.__setattr__(self, "symbol", symbol)
        broker_symbol = symbol if self.broker_symbol is None else _text(self.broker_symbol, "broker_symbol").upper()
        object.__setattr__(self, "broker_symbol", broker_symbol)
        initial = _positive_decimal(self.initial_filled_quantity, "initial_filled_quantity")
        remaining = _nonnegative_decimal(self.remaining_quantity, "remaining_quantity")
        opening_later = _nonnegative_decimal(self.opening_later_fills_quantity, "opening_later_fills_quantity")
        additions = _nonnegative_decimal(self.completed_additions_quantity, "completed_additions_quantity")
        if remaining > initial + opening_later + additions:
            raise ValueError("remaining_quantity cannot exceed opening fills plus confirmed additions")
        object.__setattr__(self, "initial_filled_quantity", initial)
        object.__setattr__(self, "remaining_quantity", remaining)
        object.__setattr__(self, "opening_later_fills_quantity", opening_later)
        object.__setattr__(self, "completed_additions_quantity", additions)
        if not isinstance(self.addition_count, int) or isinstance(self.addition_count, bool) or self.addition_count < 0:
            raise ValueError("addition_count must be a non-negative integer")
        for name in (
            "entry_price",
            "cost_basis",
            "proposed_stop_price",
            "confirmed_protective_stop_price",
            "peak_price",
        ):
            value = _decimal(getattr(self, name), name, optional=True)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive when available")
            object.__setattr__(self, name, value)
        object.__setattr__(self, "realized_pnl", _decimal(self.realized_pnl, "realized_pnl", optional=True))
        committed_risk = _decimal(self.committed_risk, "committed_risk", optional=True)
        if committed_risk is not None and committed_risk < 0:
            raise ValueError("committed_risk must be non-negative when available")
        object.__setattr__(self, "committed_risk", committed_risk)
        if self.committed_risk is not None:
            if self.committed_risk_basis is None:
                raise ValueError("committed_risk_basis is required when committed risk is known")
            object.__setattr__(self, "committed_risk_basis", _text(self.committed_risk_basis, "committed_risk_basis"))
        elif self.committed_risk_basis is not None:
            raise ValueError("committed_risk_basis requires a committed_risk value")
        for name in (
            "proposed_stop_action_id",
            "confirmed_stop_action_id",
            "confirmed_stop_client_order_id",
            "confirmed_stop_broker_order_id",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _text(value, name))
        if self.confirmed_stop_observed_at is not None:
            _aware_datetime(self.confirmed_stop_observed_at, "confirmed_stop_observed_at")
        if self.valuation_at is not None:
            _aware_datetime(self.valuation_at, "valuation_at")
        if self.last_accepted_session is not None and (
            not isinstance(self.last_accepted_session, date) or isinstance(self.last_accepted_session, datetime)
        ):
            raise ValueError("last_accepted_session must be a date")
        if not isinstance(self.last_exit_tier, int) or isinstance(self.last_exit_tier, bool) or self.last_exit_tier < 0:
            raise ValueError("last_exit_tier must be a non-negative integer")
        flags = tuple(sorted((_text(key, "policy flag name"), _text(value, "policy flag value")) for key, value in self.policy_flags))
        if len({key for key, _ in flags}) != len(flags):
            raise ValueError("policy flag names must be unique")
        object.__setattr__(self, "policy_flags", flags)
        pending = _unique_refs(self.pending_action_ids, "pending_action_ids")
        object.__setattr__(self, "pending_action_ids", tuple(sorted(pending)))
        watermarks: dict[str, Decimal] = {}
        for action_id, quantity in self.applied_action_fill_watermarks:
            normalized_id = _text(action_id, "applied action identity")
            normalized_qty = _nonnegative_decimal(quantity, "applied action fill watermark")
            if normalized_id in watermarks:
                raise ValueError("applied action fill watermarks cannot contain duplicates")
            watermarks[normalized_id] = normalized_qty
        object.__setattr__(self, "applied_action_fill_watermarks", tuple(sorted(watermarks.items())))
        additions = _unique_refs(self.completed_addition_action_ids, "completed_addition_action_ids")
        object.__setattr__(self, "completed_addition_action_ids", tuple(sorted(additions)))

    @classmethod
    def open(
        cls,
        *,
        deployment_generation_id: str,
        security_id: str,
        symbol: str,
        opening_action_id: str,
        initial_filled_quantity: Decimal,
        entry_price: Decimal | None,
        broker_symbol: str | None = None,
    ) -> HoldingEpisode:
        return cls(
            deployment_generation_id=deployment_generation_id,
            security_id=security_id,
            symbol=symbol,
            opening_action_id=opening_action_id,
            initial_filled_quantity=initial_filled_quantity,
            remaining_quantity=initial_filled_quantity,
            entry_price=entry_price,
            cost_basis=entry_price,
            realized_pnl=Decimal("0"),
            broker_symbol=broker_symbol,
            applied_action_fill_watermarks=((
                opening_action_id,
                _positive_decimal(initial_filled_quantity, "initial_filled_quantity"),
            ),),
        )

    @property
    def holding_episode_id(self) -> str:
        identity = {
            "deployment_generation_id": self.deployment_generation_id,
            "security_id": self.security_id,
            "opening_action_id": self.opening_action_id,
        }
        return f"holding:sha256:{_canonical_digest(identity)}"


def register_pending_action(holding: HoldingEpisode, intent: ActionIntent) -> HoldingEpisode:
    if intent.holding_episode_id != holding.holding_episode_id:
        raise ValueError("action does not belong to this holding episode")
    if not intent.is_open:
        raise ValueError("only an open action can be registered as pending")
    if intent.logical_action_id in holding.pending_action_ids:
        return holding
    if holding.pending_action_ids:
        raise PendingActionConflictError("holding already has a pending logical action")
    return replace(holding, pending_action_ids=(intent.logical_action_id,))


def clear_terminal_pending_action(holding: HoldingEpisode, intent: ActionIntent) -> HoldingEpisode:
    if intent.holding_episode_id != holding.holding_episode_id:
        raise ValueError("action does not belong to this holding episode")
    if intent.status not in _TERMINAL_STATUSES:
        raise ValueError("pending action cannot be cleared before terminal reconciliation")
    return replace(
        holding,
        pending_action_ids=tuple(
            action_id for action_id in holding.pending_action_ids if action_id != intent.logical_action_id
        ),
    )


def _apply_action_fill_to_holding(
    holding: HoldingEpisode,
    intent: ActionIntent,
    *,
    incremental_fill_notional: Decimal | None,
    incremental_fees: Decimal | None,
) -> HoldingEpisode:
    opening_fill = intent.role is ActionRole.ENTRY and intent.logical_action_id == holding.opening_action_id
    if not opening_fill and intent.holding_episode_id != holding.holding_episode_id:
        raise ValueError("action does not belong to this holding episode")
    if intent.role not in {ActionRole.ENTRY, ActionRole.ADDITION, ActionRole.SCALE_OUT, ActionRole.CLOSE}:
        raise ValueError("action role does not change holding quantity")
    watermarks = dict(holding.applied_action_fill_watermarks)
    prior = watermarks.get(intent.logical_action_id, Decimal("0"))
    if intent.confirmed_filled_quantity < prior:
        raise ValueError("holding fill watermark cannot decrease")
    delta = intent.confirmed_filled_quantity - prior
    if delta == 0:
        return holding
    notional = _decimal(incremental_fill_notional, "incremental_fill_notional", optional=True)
    fees = _decimal(incremental_fees, "incremental_fees", optional=True)
    if notional is not None and notional < 0:
        raise ValueError("incremental_fill_notional must be non-negative")
    if fees is not None and fees < 0:
        raise ValueError("incremental_fees must be non-negative")

    if intent.role in {ActionRole.ENTRY, ActionRole.ADDITION}:
        remaining = holding.remaining_quantity + delta
        additions = holding.completed_additions_quantity + (delta if intent.role is ActionRole.ADDITION else 0)
        opening_later = holding.opening_later_fills_quantity + (delta if intent.role is ActionRole.ENTRY else 0)
        old_cost = holding.cost_basis
        if notional is None or fees is None or old_cost is None:
            cost_basis = None
        else:
            cost_basis = (
                holding.remaining_quantity * old_cost + notional + fees
            ) / remaining
        addition_ids = set(holding.completed_addition_action_ids)
        is_first_add_fill = intent.role is ActionRole.ADDITION and intent.logical_action_id not in addition_ids
        if is_first_add_fill:
            addition_ids.add(intent.logical_action_id)
        addition_count = holding.addition_count + int(is_first_add_fill)
        realized_pnl = holding.realized_pnl
    else:
        if delta > holding.remaining_quantity:
            raise ValueError("confirmed sell fills cannot exceed remaining holding quantity")
        remaining = holding.remaining_quantity - delta
        additions = holding.completed_additions_quantity
        opening_later = holding.opening_later_fills_quantity
        cost_basis = holding.cost_basis
        addition_ids = set(holding.completed_addition_action_ids)
        addition_count = holding.addition_count
        if notional is None or fees is None or cost_basis is None or holding.realized_pnl is None:
            realized_pnl = None
        else:
            realized_pnl = holding.realized_pnl + notional - fees - cost_basis * delta
    watermarks[intent.logical_action_id] = intent.confirmed_filled_quantity
    return replace(
        holding,
        remaining_quantity=remaining,
        completed_additions_quantity=additions,
        opening_later_fills_quantity=opening_later,
        addition_count=addition_count,
        cost_basis=cost_basis,
        realized_pnl=realized_pnl,
        applied_action_fill_watermarks=tuple(sorted(watermarks.items())),
        completed_addition_action_ids=tuple(sorted(addition_ids)),
    )


def apply_action_fill_to_holding(
    holding: HoldingEpisode,
    intent: ActionIntent,
    *,
    incremental_fill_notional: Decimal | None = None,
    incremental_fees: Decimal | None = None,
) -> HoldingEpisode:
    """Apply confirmed shares and known execution value; preserve unavailable accounting as None."""
    return _apply_action_fill_to_holding(
        holding,
        intent,
        incremental_fill_notional=incremental_fill_notional,
        incremental_fees=incremental_fees,
    )


def update_holding_marks(
    holding: HoldingEpisode,
    *,
    valuation_at: datetime,
    peak_price: Decimal | None,
) -> HoldingEpisode:
    """Keep observed peak and valuation time monotonic and explicit."""
    _aware_datetime(valuation_at, "valuation_at")
    if holding.valuation_at is not None and valuation_at < holding.valuation_at:
        raise ValueError("valuation_at cannot decrease")
    peak = _decimal(peak_price, "peak_price", optional=True)
    if peak is not None and peak <= 0:
        raise ValueError("peak_price must be positive when available")
    if holding.peak_price is not None and peak is not None and peak < holding.peak_price:
        raise ValueError("portfolio peak must evolve monotonically")
    return replace(
        holding,
        valuation_at=valuation_at,
        peak_price=peak if peak is not None else holding.peak_price,
    )


@dataclass(frozen=True, slots=True)
class StopUpdateIntent:
    deployment_generation_id: str
    decision_id: str
    holding_episode_id: str
    requested_stop_price: Decimal
    logical_action_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "deployment_generation_id", _text(self.deployment_generation_id, "deployment_generation_id"))
        object.__setattr__(self, "decision_id", _text(self.decision_id, "decision_id"))
        object.__setattr__(self, "holding_episode_id", _text(self.holding_episode_id, "holding_episode_id"))
        object.__setattr__(self, "requested_stop_price", _positive_decimal(self.requested_stop_price, "requested_stop_price"))
        identity = {
            "deployment_generation_id": self.deployment_generation_id,
            "decision_id": self.decision_id,
            "holding_episode_id": self.holding_episode_id,
            "role": "stop_update",
        }
        object.__setattr__(self, "logical_action_id", f"action:sha256:{_canonical_digest(identity)}")


def propose_stop_update(
    holding: HoldingEpisode,
    *,
    decision: DecisionIdentity,
    stop_price: Decimal,
) -> tuple[HoldingEpisode, StopUpdateIntent]:
    if decision.deployment_generation_id != holding.deployment_generation_id:
        raise ValueError("stop proposal must use the holding's opening generation")
    price = _positive_decimal(stop_price, "stop_price")
    known_stops = tuple(
        value
        for value in (holding.proposed_stop_price, holding.confirmed_protective_stop_price)
        if value is not None
    )
    prior_stop = max(known_stops) if known_stops else None
    if prior_stop is not None and price < prior_stop:
        raise ValueError("proposed protective stop must evolve monotonically")
    if holding.proposed_stop_action_id != holding.confirmed_stop_action_id:
        raise PendingActionConflictError("a stop update is already awaiting broker confirmation")
    if holding.pending_action_ids:
        raise PendingActionConflictError("holding has another pending action")
    intent = StopUpdateIntent(
        deployment_generation_id=holding.deployment_generation_id,
        decision_id=decision.decision_id,
        holding_episode_id=holding.holding_episode_id,
        requested_stop_price=price,
    )
    updated = replace(
        holding,
        proposed_stop_price=price,
        proposed_stop_action_id=intent.logical_action_id,
        pending_action_ids=(intent.logical_action_id,),
    )
    return updated, intent


def confirm_protective_stop(
    holding: HoldingEpisode,
    *,
    intent: StopUpdateIntent,
    stop_price: Decimal,
    client_order_id: str,
    broker_order_id: str,
    observed_at: datetime,
) -> HoldingEpisode:
    if intent.deployment_generation_id != holding.deployment_generation_id:
        raise ValueError("confirmed stop generation does not match the holding")
    if intent.holding_episode_id != holding.holding_episode_id:
        raise ValueError("confirmed stop action does not belong to this holding")
    price = _positive_decimal(stop_price, "stop_price")
    client = _text(client_order_id, "client_order_id")
    broker = _text(broker_order_id, "broker_order_id")
    _aware_datetime(observed_at, "observed_at")
    if holding.confirmed_stop_action_id == intent.logical_action_id:
        if (
            holding.confirmed_protective_stop_price == price
            and holding.confirmed_stop_client_order_id == client
            and holding.confirmed_stop_broker_order_id == broker
            and holding.confirmed_stop_observed_at == observed_at
        ):
            return holding
        raise IdentityConflictError("confirmed stop facts conflict with the stored action")
    if holding.proposed_stop_action_id != intent.logical_action_id:
        raise IdentityConflictError("broker confirmation does not match the pending stop proposal")
    if holding.confirmed_protective_stop_price is not None and price < holding.confirmed_protective_stop_price:
        raise ValueError("confirmed protective stop cannot weaken existing protection")
    if holding.confirmed_stop_observed_at is not None and observed_at < holding.confirmed_stop_observed_at:
        raise ValueError("confirmed stop observation time cannot decrease")
    return replace(
        holding,
        confirmed_protective_stop_price=price,
        confirmed_stop_action_id=intent.logical_action_id,
        confirmed_stop_client_order_id=client,
        confirmed_stop_broker_order_id=broker,
        confirmed_stop_observed_at=observed_at,
        pending_action_ids=tuple(
            action_id for action_id in holding.pending_action_ids if action_id != intent.logical_action_id
        ),
    )


def advance_holding_exit_tier(holding: HoldingEpisode, intent: ActionIntent) -> HoldingEpisode:
    """Advance only after full fill or a named explicit reconciliation result."""
    if intent.role is not ActionRole.SCALE_OUT or intent.holding_episode_id != holding.holding_episode_id:
        raise ValueError("exit tier action does not belong to this holding episode")
    if intent.status not in _TIER_COMPLETION_STATUSES:
        raise ValueError("exit tier requires a fully confirmed fill or explicitly resolved action")
    if intent.status is ActionStatus.FILLED and intent.confirmed_filled_quantity != intent.requested_quantity:
        raise ValueError("filled exit tier must match its target quantity")
    if intent.status is ActionStatus.RESOLVED and not intent.resolution_reason:
        raise ValueError("resolved exit tier requires an explicit resolution reason")
    watermarks = dict(holding.applied_action_fill_watermarks)
    if watermarks.get(intent.logical_action_id, Decimal("0")) < intent.confirmed_filled_quantity:
        raise ValueError("confirmed holding quantity must be reconciled before advancing the tier")
    assert intent.exit_tier is not None
    if intent.exit_tier != holding.last_exit_tier + 1:
        raise ValueError("exit tiers must advance one completed tier at a time")
    return replace(
        holding,
        last_exit_tier=intent.exit_tier,
        pending_action_ids=tuple(
            action_id for action_id in holding.pending_action_ids if action_id != intent.logical_action_id
        ),
    )


@dataclass(frozen=True, slots=True)
class ActionStateProjection:
    """Read-only action and residual reservation view for account adapters."""

    clock_id: str
    decision_slot_id: str
    decision_id: str
    snapshot_sha256: str
    deployment_generation_id: str
    deployment_identity: PolicyDeploymentIdentity
    holding_episode_id: str | None
    security_id: str
    broker_symbol: str
    logical_action_id: str
    decision_session: date
    as_of_cutoff_at: datetime
    next_execution_session: date
    account_valuation_session: date
    account_valuation_at: datetime
    decision_category: DecisionCategory
    decision_subject_type: DecisionSubjectType
    decision_subject_id: str
    role: ActionRole
    side: OrderSide
    status: ActionStatus
    resolution_reason: str | None
    requested_quantity: Decimal
    confirmed_quantity: Decimal
    residual_quantity: Decimal
    reservation_price: Decimal | None
    reservation_price_basis: str | None
    reservation_stop_price: Decimal | None
    reservation_amount: Decimal | None
    risk_per_unit: Decimal | None
    risk_basis: str | None
    residual_committed_risk: Decimal | None
    exit_tier: int | None
    snapshot_original_quantity: Decimal | None
    fraction_of_original_quantity: Decimal | None
    rounding_rule_id: str | None
    order_attempts: tuple[ActionOrderAttempt, ...]


@dataclass(frozen=True, slots=True)
class PortfolioStateSnapshot:
    """Immutable, identity-bound account facts; pending commitments stay separate."""

    deployment_identity: PolicyDeploymentIdentity
    clock: DecisionClock
    source_namespace: str
    account_snapshot_id: str
    equity: Decimal | None
    cash: Decimal | None
    gross_exposure: Decimal | None
    open_risk: Decimal | None
    portfolio_peak_equity: Decimal | None
    last_accepted_session: date | None
    policy_flags: tuple[tuple[str, str], ...] = ()
    portfolio_snapshot_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_namespace", _text(self.source_namespace, "source_namespace"))
        object.__setattr__(self, "account_snapshot_id", _text(self.account_snapshot_id, "account_snapshot_id"))
        for name in ("equity", "cash", "gross_exposure", "open_risk", "portfolio_peak_equity"):
            value = _decimal(getattr(self, name), name, optional=True)
            if name in {"gross_exposure", "open_risk", "portfolio_peak_equity"} and value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative when available")
            object.__setattr__(self, name, value)
        if self.equity is not None and self.cash is not None and self.gross_exposure is not None:
            if self.cash + self.gross_exposure != self.equity:
                raise ValueError("cash plus gross exposure must equal equity")
        if (
            self.portfolio_peak_equity is not None
            and self.equity is not None
            and self.portfolio_peak_equity < self.equity
        ):
            raise ValueError("portfolio peak equity cannot be below current equity")
        if self.last_accepted_session is not None and (
            not isinstance(self.last_accepted_session, date)
            or isinstance(self.last_accepted_session, datetime)
        ):
            raise ValueError("last_accepted_session must be a date")
        flags = tuple(sorted((_text(key, "policy flag name"), _text(value, "policy flag value")) for key, value in self.policy_flags))
        if len({key for key, _ in flags}) != len(flags):
            raise ValueError("policy flag names must be unique")
        object.__setattr__(self, "policy_flags", flags)
        object.__setattr__(
            self,
            "portfolio_snapshot_id",
            "portfolio:sha256:"
            + _canonical_digest(
                {
                    "deployment_generation_id": self.deployment_identity.deployment_generation_id,
                    "store_identity": self.deployment_identity.store_identity,
                    "paper_account_environment_id": self.deployment_identity.paper_account_environment_id,
                    "source_namespace": self.source_namespace,
                    "account_snapshot_id": self.account_snapshot_id,
                    "clock_id": self.clock.clock_id,
                }
            ),
        )


def project_action_state(intent: ActionIntent) -> ActionStateProjection:
    """Return the stable local reservation and reference projection for #99."""
    residual = intent.residual_quantity
    if intent.side is not OrderSide.BUY or not intent.is_open or residual == 0:
        reservation_amount: Decimal | None = Decimal("0")
    elif intent.reservation_price is None:
        reservation_amount = None
    else:
        reservation_amount = residual * intent.reservation_price
    if intent.side is not OrderSide.BUY or not intent.is_open or residual == 0:
        residual_risk: Decimal | None = Decimal("0")
    elif intent.risk_per_unit is None:
        residual_risk = None
    else:
        residual_risk = residual * intent.risk_per_unit
    return ActionStateProjection(
        clock_id=intent.decision.clock.clock_id,
        decision_slot_id=intent.decision.decision_slot_id,
        decision_id=intent.decision.decision_id,
        snapshot_sha256=intent.decision.snapshot_sha256,
        deployment_generation_id=intent.deployment_generation_id,
        deployment_identity=intent.decision.deployment_identity,
        holding_episode_id=intent.holding_episode_id,
        security_id=intent.security_id,
        broker_symbol=intent.broker_symbol,
        logical_action_id=intent.logical_action_id,
        decision_session=intent.decision.clock.decision_session,
        as_of_cutoff_at=intent.decision.clock.as_of_cutoff_at,
        next_execution_session=intent.decision.clock.next_execution_session,
        account_valuation_session=intent.decision.clock.account_valuation_session,
        account_valuation_at=intent.decision.clock.account_valuation_at,
        decision_category=intent.decision.category,
        decision_subject_type=intent.decision.subject_type,
        decision_subject_id=intent.decision.subject_id,
        role=intent.role,
        side=intent.side,
        status=intent.status,
        resolution_reason=intent.resolution_reason,
        requested_quantity=intent.requested_quantity,
        confirmed_quantity=intent.confirmed_filled_quantity,
        residual_quantity=residual,
        reservation_price=intent.reservation_price,
        reservation_price_basis=intent.reservation_price_basis,
        reservation_stop_price=intent.reservation_stop_price,
        reservation_amount=reservation_amount,
        risk_per_unit=intent.risk_per_unit,
        risk_basis=intent.risk_basis,
        residual_committed_risk=residual_risk,
        exit_tier=intent.exit_tier,
        snapshot_original_quantity=intent.snapshot_original_quantity,
        fraction_of_original_quantity=intent.fraction_of_original_quantity,
        rounding_rule_id=intent.rounding_rule_id,
        order_attempts=intent.order_attempts,
    )
