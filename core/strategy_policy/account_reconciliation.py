"""Pure reconciliation of synthetic broker facts and durable policy-state projections.

The #100 value types are the only persistence-facing dependency. This module makes no
store, provider, or broker calls; conversion and accounting remain deterministic and
side-effect free.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from core.policy_execution_state import ActionStateProjection, HoldingEpisode, PortfolioStateSnapshot

from .adapter_v3 import StrategyPolicyAdapterV3
from .contracts_v3 import PortfolioFeaturesV3


_ACTIVE_STATES = frozenset(
    {"intended", "submitted", "partially_filled", "cancel_requested", "remainder_ready"}
)
_TERMINAL_STATES = frozenset({"filled", "cancelled", "rejected", "expired", "resolved"})
_UNCERTAIN_STATES = frozenset({"reconciliation_required", "partial_incomplete"})
_BROKER_ACTIVE_STATES = frozenset({"submitted", "partially_filled", "cancel_requested"})
_BROKER_TERMINAL_STATES = frozenset({"filled", "cancelled", "rejected", "expired"})
_BUY_ROLES = frozenset({"entry", "addition", "replacement"})
_SELL_ROLES = frozenset({"scale_out", "close"})
_ACTION_ROLES = _BUY_ROLES | _SELL_ROLES
_HOLDING_SPECIFIC_ROLES = frozenset({"addition", "scale_out", "close"})
_CLASSIFICATION_STATES = frozenset(
    {
        "observed",
        "not_yet_public",
        "absent",
        "unsupported_scope",
        "insufficient_history",
        "stale_by_declared_rule",
        "invalid",
    }
)


def _aware(value: object, name: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _amount(
    value: object,
    name: str,
    *,
    minimum: float | None = None,
    nullable: bool = False,
) -> None:
    if value is None and nullable:
        return
    if type(value) is bool or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        raise ValueError(f"{name} must be a finite number at or above {minimum}")


def _identifier(value: object, name: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if type(value) is not str or not value or value.strip() != value or len(value) > 256:
        raise ValueError(f"{name} must be a non-empty trimmed string")


def _close(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-8)


@dataclass(frozen=True, slots=True)
class AccountValuationClock:
    """Explicit completed-session, cutoff, next-session, and valuation identity."""

    completed_session: date
    as_of_cutoff: datetime
    next_execution_session: date
    valuation_time: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.completed_session, date) or isinstance(self.completed_session, datetime):
            raise ValueError("completed_session must be a date")
        if not isinstance(self.next_execution_session, date) or isinstance(
            self.next_execution_session, datetime
        ):
            raise ValueError("next_execution_session must be a date")
        if self.next_execution_session <= self.completed_session:
            raise ValueError("next_execution_session must follow completed_session")
        _aware(self.as_of_cutoff, "as_of_cutoff")
        _aware(self.valuation_time, "valuation_time")
        if self.valuation_time < self.as_of_cutoff:
            raise ValueError("valuation_time must not precede the input cutoff")
        if self.valuation_time.date() > self.next_execution_session:
            raise ValueError("valuation_time is after the next execution session")


@dataclass(frozen=True, slots=True)
class ClassificationFact:
    """Dated classification with original source state and normalized visibility."""

    code: str | None
    source_state: str
    available_from_session: date | None
    effective_from: date | None
    effective_through: date | None
    source_public_at: datetime | None

    def __post_init__(self) -> None:
        if self.source_state not in _CLASSIFICATION_STATES:
            raise ValueError("classification source_state is unsupported")
        _identifier(self.code, "classification code", nullable=True)
        for name in ("available_from_session", "effective_from", "effective_through"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, date) or isinstance(value, datetime)):
                raise ValueError(f"{name} must be a date")
        _aware(self.source_public_at, "classification source_public_at", nullable=True)


@dataclass(frozen=True, slots=True)
class OrderReference:
    broker_order_id: str | None = None
    client_order_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.broker_order_id, "broker_order_id", nullable=True)
        _identifier(self.client_order_id, "client_order_id", nullable=True)
        if self.broker_order_id is None and self.client_order_id is None:
            raise ValueError("an order reference requires a broker or client order id")


@dataclass(frozen=True, slots=True)
class OrderAttemptProjection:
    """One canonical #100 order attempt, retaining its own references and fill watermark."""

    attempt_number: int
    requested_quantity: float
    confirmed_quantity: float
    status: str
    client_order_id: str | None
    broker_order_id: str | None
    terminal_status: str | None = None
    client_order_aliases: tuple[str, ...] = ()
    broker_order_aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.attempt_number not in {1, 2}:
            raise ValueError("order attempt number must be 1 or 2")
        _amount(self.requested_quantity, "attempt requested_quantity", minimum=0.0)
        if self.requested_quantity == 0:
            raise ValueError("attempt requested_quantity must be positive")
        _amount(self.confirmed_quantity, "attempt confirmed_quantity", minimum=0.0)
        _identifier(self.status, "attempt status")
        _identifier(self.terminal_status, "attempt terminal_status", nullable=True)
        if self.confirmed_quantity > self.requested_quantity and not (
            self.terminal_status is not None or self.status == "reconciliation_required"
        ):
            raise ValueError("attempt confirmed_quantity exceeds requested_quantity")
        _identifier(self.client_order_id, "attempt client_order_id", nullable=True)
        _identifier(self.broker_order_id, "attempt broker_order_id", nullable=True)
        for name, primary in (
            ("client_order_aliases", self.client_order_id),
            ("broker_order_aliases", self.broker_order_id),
        ):
            aliases = getattr(self, name)
            if type(aliases) is not tuple:
                raise ValueError(f"attempt {name} must be a tuple")
            for alias in aliases:
                _identifier(alias, f"attempt {name} item")
            if len(aliases) != len(set(aliases)) or (primary is not None and primary in aliases):
                raise ValueError(f"attempt {name} must be unique and distinct from its primary reference")


@dataclass(frozen=True, slots=True)
class HoldingProjection:
    holding_episode_id: str
    deployment_generation_id: str
    security_id: str
    remaining_quantity: float
    stop_price: float | None
    stop_observed_at: datetime | None
    protective_stop_order_references: tuple[OrderReference, ...]
    source_state_version: int | None = None

    def __post_init__(self) -> None:
        _identifier(self.holding_episode_id, "holding_episode_id")
        _identifier(self.deployment_generation_id, "holding deployment_generation_id")
        _identifier(self.security_id, "holding security_id")
        _amount(self.remaining_quantity, "holding remaining_quantity", minimum=0.0)
        _amount(self.stop_price, "holding stop_price", minimum=0.0, nullable=True)
        _aware(self.stop_observed_at, "holding stop_observed_at", nullable=True)
        if type(self.protective_stop_order_references) is not tuple or any(
            type(reference) is not OrderReference
            for reference in self.protective_stop_order_references
        ):
            raise ValueError("protective_stop_order_references must be a tuple of OrderReference")
        if self.source_state_version is not None and (
            not isinstance(self.source_state_version, int)
            or isinstance(self.source_state_version, bool)
            or self.source_state_version < 0
        ):
            raise ValueError("holding source_state_version must be a non-negative integer when available")


@dataclass(frozen=True, slots=True)
class PolicyActionProjection:
    logical_action_id: str
    deployment_generation_id: str
    holding_episode_id: str | None
    security_id: str
    role: str
    side: Literal["buy", "sell"]
    status: str
    requested_quantity: float
    confirmed_quantity: float
    residual_quantity: float
    reservation_amount: float | None
    reservation_price: float | None
    reservation_price_basis: str | None
    reservation_stop_price: float | None
    client_order_refs: tuple[str, ...]
    broker_order_refs: tuple[str, ...]
    resolution_reason: str | None
    reservation_risk_per_unit: float | None = None
    reservation_risk_basis: str | None = None
    residual_committed_risk: float | None = None
    order_attempts: tuple[OrderAttemptProjection, ...] = ()
    source_decision_slot_id: str | None = None
    source_decision_id: str | None = None
    source_snapshot_sha256: str | None = None
    source_clock_id: str | None = None
    source_paper_account_environment_id: str | None = None
    source_store_identity: str | None = None
    source_decision_session: date | None = None
    source_as_of_cutoff_at: datetime | None = None
    source_next_execution_session: date | None = None
    source_account_valuation_session: date | None = None
    source_account_valuation_at: datetime | None = None
    source_state_version: int | None = None

    def __post_init__(self) -> None:
        _identifier(self.logical_action_id, "logical_action_id")
        _identifier(self.deployment_generation_id, "action deployment_generation_id")
        _identifier(self.holding_episode_id, "action holding_episode_id", nullable=True)
        _identifier(self.security_id, "action security_id")
        if self.role not in _ACTION_ROLES:
            raise ValueError("action role is unsupported")
        if self.side not in {"buy", "sell"}:
            raise ValueError("action side must be buy or sell")
        if (self.role in _BUY_ROLES) != (self.side == "buy"):
            raise ValueError("action role and side do not reconcile")
        if self.role in {"addition", "scale_out", "close"} and self.holding_episode_id is None:
            raise ValueError("holding-specific action requires a holding_episode_id")
        _amount(self.requested_quantity, "action requested_quantity", minimum=0.0)
        if self.requested_quantity == 0:
            raise ValueError("action requested_quantity must be positive")
        _amount(self.confirmed_quantity, "action confirmed_quantity", minimum=0.0)
        has_explicit_resolution = self.status == "resolved" and bool(self.resolution_reason)
        if (
            self.confirmed_quantity > self.requested_quantity
            and not _close(float(self.confirmed_quantity), float(self.requested_quantity))
            and self.status != "reconciliation_required"
            and not has_explicit_resolution
        ):
            raise ValueError("action confirmed_quantity exceeds requested_quantity")
        _amount(self.residual_quantity, "action residual_quantity", minimum=0.0)
        expected_residual = max(
            float(self.requested_quantity) - float(self.confirmed_quantity), 0.0
        )
        if not _close(float(self.residual_quantity), expected_residual):
            raise ValueError("action residual_quantity does not reconcile to cumulative fills")
        _amount(self.reservation_amount, "action reservation_amount", minimum=0.0, nullable=True)
        _amount(self.reservation_price, "action reservation_price", minimum=0.0, nullable=True)
        _amount(
            self.reservation_stop_price,
            "action reservation_stop_price",
            minimum=0.0,
            nullable=True,
        )
        if self.reservation_price == 0:
            raise ValueError("action reservation_price must be positive")
        if self.reservation_stop_price == 0:
            raise ValueError("action reservation_stop_price must be positive")
        _identifier(self.reservation_price_basis, "reservation_price_basis", nullable=True)
        _identifier(self.resolution_reason, "action resolution_reason", nullable=True)
        _amount(
            self.reservation_risk_per_unit,
            "action reservation_risk_per_unit",
            minimum=0.0,
            nullable=True,
        )
        _amount(
            self.residual_committed_risk,
            "action residual_committed_risk",
            minimum=0.0,
            nullable=True,
        )
        _identifier(self.reservation_risk_basis, "action reservation_risk_basis", nullable=True)
        if type(self.order_attempts) is not tuple or any(
            type(item) is not OrderAttemptProjection for item in self.order_attempts
        ):
            raise ValueError("order_attempts must be a tuple of OrderAttemptProjection")
        if any(
            self.order_attempts[index].attempt_number >= self.order_attempts[index + 1].attempt_number
            for index in range(len(self.order_attempts) - 1)
        ):
            raise ValueError("order attempts must be ordered by increasing attempt number")
        if len({item.client_order_id for item in self.order_attempts if item.client_order_id}) != sum(
            item.client_order_id is not None for item in self.order_attempts
        ) or len({item.broker_order_id for item in self.order_attempts if item.broker_order_id}) != sum(
            item.broker_order_id is not None for item in self.order_attempts
        ):
            raise ValueError("order attempt references must be unique within a logical action")
        for name in (
            "source_decision_slot_id",
            "source_decision_id",
            "source_snapshot_sha256",
            "source_clock_id",
            "source_paper_account_environment_id",
            "source_store_identity",
        ):
            _identifier(getattr(self, name), name, nullable=True)
        for name in (
            "source_decision_session",
            "source_next_execution_session",
            "source_account_valuation_session",
        ):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, date) or isinstance(value, datetime)):
                raise ValueError(f"{name} must be a date")
        _aware(self.source_as_of_cutoff_at, "source_as_of_cutoff_at", nullable=True)
        _aware(self.source_account_valuation_at, "source_account_valuation_at", nullable=True)
        if self.source_state_version is not None and (
            not isinstance(self.source_state_version, int)
            or isinstance(self.source_state_version, bool)
            or self.source_state_version < 0
        ):
            raise ValueError("source_state_version must be a non-negative integer when available")
        for name in ("client_order_refs", "broker_order_refs"):
            references = getattr(self, name)
            if type(references) is not tuple or any(
                type(reference) is not str or not reference for reference in references
            ):
                raise ValueError(f"action {name} must be a tuple of non-empty strings")
        if self.role not in _BUY_ROLES and any(
            value is not None
            for value in (
                self.reservation_price,
                self.reservation_price_basis,
                self.reservation_amount,
                self.reservation_risk_per_unit,
                self.reservation_risk_basis,
                self.residual_committed_risk,
            )
        ):
            raise ValueError("only buy actions may carry reservation fields")


@dataclass(frozen=True, slots=True)
class SecuritySymbolMapping:
    """Evidence-bound mapping from a stable security identity to a broker symbol."""

    security_id: str
    broker_symbol: str
    mapping_contract_id: str
    effective_from: date
    effective_through: date | None
    validated_at: datetime

    def __post_init__(self) -> None:
        _identifier(self.security_id, "mapping security_id")
        _identifier(self.broker_symbol, "mapping broker_symbol")
        _identifier(self.mapping_contract_id, "mapping contract identity")
        if not isinstance(self.effective_from, date) or isinstance(self.effective_from, datetime):
            raise ValueError("mapping effective_from must be a date")
        if self.effective_through is not None and (
            not isinstance(self.effective_through, date)
            or isinstance(self.effective_through, datetime)
        ):
            raise ValueError("mapping effective_through must be a date")
        _aware(self.validated_at, "mapping validated_at")


@dataclass(frozen=True, slots=True)
class CanonicalPortfolioFacts:
    """Optional monetary facts retained from the canonical portfolio snapshot."""

    equity: float | None
    cash: float | None
    gross_exposure: float | None
    open_risk: float | None
    peak_equity: float | None

    def __post_init__(self) -> None:
        for name in ("equity", "cash", "gross_exposure", "open_risk", "peak_equity"):
            _amount(getattr(self, name), f"canonical portfolio {name}", minimum=0.0, nullable=True)


@dataclass(frozen=True, slots=True)
class PolicyStateProjection:
    """Pure input shape for the #100 read-only holding/action projection."""

    snapshot_id: str
    decision_clock_id: str
    decision_slot_id: str
    decision_id: str
    decision_session: date
    input_cutoff_at: datetime
    next_execution_session: date
    active_deployment_generation_id: str
    paper_account_environment_id: str
    store_identity: str
    holdings: tuple[HoldingProjection, ...] | None
    pending_actions: tuple[PolicyActionProjection, ...] | None
    security_symbol_mappings: tuple[SecuritySymbolMapping, ...] | None
    source_namespace: str | None = None
    account_snapshot_id: str | None = None
    canonical_portfolio_facts: CanonicalPortfolioFacts | None = None

    def __post_init__(self) -> None:
        for name in (
            "snapshot_id",
            "decision_clock_id",
            "decision_slot_id",
            "decision_id",
            "active_deployment_generation_id",
            "paper_account_environment_id",
            "store_identity",
        ):
            _identifier(getattr(self, name), name)
        if not isinstance(self.decision_session, date) or isinstance(self.decision_session, datetime):
            raise ValueError("projection decision_session must be a date")
        if not isinstance(self.next_execution_session, date) or isinstance(
            self.next_execution_session, datetime
        ):
            raise ValueError("projection next_execution_session must be a date")
        if self.next_execution_session <= self.decision_session:
            raise ValueError("projection next_execution_session must follow decision_session")
        _aware(self.input_cutoff_at, "projection input_cutoff_at")
        _identifier(self.source_namespace, "projection source_namespace", nullable=True)
        _identifier(self.account_snapshot_id, "projection account_snapshot_id", nullable=True)
        if self.canonical_portfolio_facts is not None and type(
            self.canonical_portfolio_facts
        ) is not CanonicalPortfolioFacts:
            raise ValueError("canonical_portfolio_facts must be CanonicalPortfolioFacts or None")
        if self.holdings is not None and (
            type(self.holdings) is not tuple
            or any(type(item) is not HoldingProjection for item in self.holdings)
        ):
            raise ValueError("projection holdings must be None or a tuple of HoldingProjection")
        if self.pending_actions is not None and (
            type(self.pending_actions) is not tuple
            or any(type(item) is not PolicyActionProjection for item in self.pending_actions)
        ):
            raise ValueError("projection pending_actions must be None or a tuple of PolicyActionProjection")
        if self.security_symbol_mappings is not None and (
            type(self.security_symbol_mappings) is not tuple
            or any(type(item) is not SecuritySymbolMapping for item in self.security_symbol_mappings)
        ):
            raise ValueError("security_symbol_mappings must be None or a tuple of SecuritySymbolMapping")


@dataclass(frozen=True, slots=True)
class BrokerPositionFact:
    symbol: str
    quantity: float
    mark_price: float
    mark_observed_at: datetime
    sector: ClassificationFact | None
    industry: ClassificationFact | None

    def __post_init__(self) -> None:
        _identifier(self.symbol, "position symbol")
        _amount(self.quantity, "position quantity", minimum=0.0)
        if self.quantity == 0:
            raise ValueError("zero-quantity broker positions must be omitted")
        _amount(self.mark_price, "position mark_price", minimum=0.0)
        if self.mark_price == 0:
            raise ValueError("position mark_price must be positive")
        _aware(self.mark_observed_at, "position mark_observed_at")
        for name in ("sector", "industry"):
            value = getattr(self, name)
            if value is not None and type(value) is not ClassificationFact:
                raise ValueError(f"position {name} classification is invalid")


@dataclass(frozen=True, slots=True)
class BrokerOrderFact:
    broker_order_id: str | None
    client_order_id: str | None
    symbol: str
    side: Literal["buy", "sell"]
    status: str
    requested_quantity: float
    cumulative_filled_quantity: float
    purpose: str = "strategy"
    holding_episode_id: str | None = None
    stop_price: float | None = None

    def __post_init__(self) -> None:
        OrderReference(self.broker_order_id, self.client_order_id)
        _identifier(self.symbol, "broker order symbol")
        if self.purpose not in {"strategy", "protective_stop"}:
            raise ValueError("broker order purpose must be strategy or protective_stop")
        _identifier(self.holding_episode_id, "broker order holding_episode_id", nullable=True)
        _amount(self.stop_price, "broker order stop_price", minimum=0.0, nullable=True)
        if self.stop_price == 0:
            raise ValueError("broker order stop_price must be positive")
        if self.side not in {"buy", "sell"}:
            raise ValueError("broker order side must be buy or sell")
        _amount(self.requested_quantity, "broker requested_quantity", minimum=0.0)
        if self.requested_quantity == 0:
            raise ValueError("broker requested_quantity must be positive")
        _amount(
            self.cumulative_filled_quantity,
            "broker cumulative_filled_quantity",
            minimum=0.0,
        )
        if self.cumulative_filled_quantity > self.requested_quantity and not _close(
            float(self.cumulative_filled_quantity), float(self.requested_quantity)
        ):
            raise ValueError("broker cumulative fills exceed requested_quantity")


@dataclass(frozen=True, slots=True)
class BrokerAccountSnapshot:
    """Explicit broker account observation; None means unavailable, empty tuple means none."""

    paper_account_environment_id: str
    decision_slot_id: str
    decision_id: str
    clock: AccountValuationClock
    equity: float | None
    cash: float | None
    peak_equity: float | None
    balance_observed_at: datetime | None
    peak_observed_at: datetime | None
    positions: tuple[BrokerPositionFact, ...] | None
    open_orders: tuple[BrokerOrderFact, ...] | None
    source_namespace: str | None = None
    account_snapshot_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.paper_account_environment_id, "paper_account_environment_id")
        _identifier(self.decision_slot_id, "account decision_slot_id")
        _identifier(self.decision_id, "account decision_id")
        if type(self.clock) is not AccountValuationClock:
            raise ValueError("account clock is invalid")
        _amount(self.equity, "equity", minimum=0.0, nullable=True)
        _amount(self.cash, "cash", minimum=0.0, nullable=True)
        _amount(self.peak_equity, "peak_equity", minimum=0.0, nullable=True)
        _aware(self.balance_observed_at, "balance_observed_at", nullable=True)
        _aware(self.peak_observed_at, "peak_observed_at", nullable=True)
        _identifier(self.source_namespace, "account source_namespace", nullable=True)
        _identifier(self.account_snapshot_id, "account account_snapshot_id", nullable=True)
        if self.positions is not None and (
            type(self.positions) is not tuple
            or any(type(item) is not BrokerPositionFact for item in self.positions)
        ):
            raise ValueError("positions must be None or a tuple of BrokerPositionFact")
        if self.open_orders is not None and (
            type(self.open_orders) is not tuple
            or any(type(item) is not BrokerOrderFact for item in self.open_orders)
        ):
            raise ValueError("open_orders must be None or a tuple of BrokerOrderFact")


def _source_member(source: object, name: str) -> object:
    try:
        return getattr(source, name)
    except AttributeError as exc:
        raise TypeError(f"policy execution source is missing {name}") from exc


def _source_number(value: object, name: str, *, nullable: bool = False) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float)):
        raise ValueError(f"{name} must be a finite Decimal or number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite Decimal or number")
    return number


def _source_enum(value: object, name: str) -> str:
    member = getattr(value, "value", value)
    if type(member) is not str or not member:
        raise ValueError(f"{name} must be a non-empty string or enum")
    return member


def policy_execution_state_to_projection(
    *,
    account: BrokerAccountSnapshot,
    portfolio_snapshot: PortfolioStateSnapshot,
    action_projections: tuple[ActionStateProjection, ...],
    holding_episodes: tuple[HoldingEpisode, ...],
) -> PolicyStateProjection:
    """Convert #100's immutable projections into the synthetic reconciliation boundary.

    Convert the closed #100 value types directly, retaining source identities and each
    attempt's independent reference and fill watermark.
    """
    if type(account) is not BrokerAccountSnapshot:
        raise TypeError("account must be a BrokerAccountSnapshot")
    if type(portfolio_snapshot) is not PortfolioStateSnapshot:
        raise TypeError("portfolio_snapshot must be a PortfolioStateSnapshot")
    if type(action_projections) is not tuple or type(holding_episodes) is not tuple:
        raise TypeError("action_projections and holding_episodes must be tuples")
    if any(type(item) is not ActionStateProjection for item in action_projections):
        raise TypeError("action_projections must contain ActionStateProjection records")
    if any(type(item) is not HoldingEpisode for item in holding_episodes):
        raise TypeError("holding_episodes must contain HoldingEpisode records")

    deployment = _source_member(portfolio_snapshot, "deployment_identity")
    source_clock = _source_member(portfolio_snapshot, "clock")
    snapshot_id = _source_member(portfolio_snapshot, "portfolio_snapshot_id")
    generation_id = _source_member(deployment, "deployment_generation_id")
    account_environment_id = _source_member(deployment, "paper_account_environment_id")
    store_identity = _source_member(deployment, "store_identity")
    source_namespace = _source_member(portfolio_snapshot, "source_namespace")
    account_snapshot_id = _source_member(portfolio_snapshot, "account_snapshot_id")
    canonical_portfolio_facts = CanonicalPortfolioFacts(
        equity=_source_number(
            _source_member(portfolio_snapshot, "equity"), "portfolio snapshot equity", nullable=True
        ),
        cash=_source_number(
            _source_member(portfolio_snapshot, "cash"), "portfolio snapshot cash", nullable=True
        ),
        gross_exposure=_source_number(
            _source_member(portfolio_snapshot, "gross_exposure"),
            "portfolio snapshot gross_exposure",
            nullable=True,
        ),
        open_risk=_source_number(
            _source_member(portfolio_snapshot, "open_risk"),
            "portfolio snapshot open_risk",
            nullable=True,
        ),
        peak_equity=_source_number(
            _source_member(portfolio_snapshot, "portfolio_peak_equity"),
            "portfolio snapshot portfolio_peak_equity",
            nullable=True,
        ),
    )
    clock_id = _source_member(source_clock, "clock_id")
    completed_session = _source_member(source_clock, "decision_session")
    cutoff = _source_member(source_clock, "as_of_cutoff_at")
    next_execution_session = _source_member(source_clock, "next_execution_session")
    valuation_session = _source_member(source_clock, "account_valuation_session")
    valuation_time = _source_member(source_clock, "account_valuation_at")
    if account_environment_id != account.paper_account_environment_id:
        raise ValueError("portfolio and broker account environments differ")
    if (
        completed_session != account.clock.completed_session
        or cutoff != account.clock.as_of_cutoff
        or next_execution_session != account.clock.next_execution_session
        or valuation_session != account.clock.next_execution_session
        or valuation_time != account.clock.valuation_time
    ):
        raise ValueError("portfolio snapshot clock differs from broker account valuation clock")
    if account.source_namespace is not None and account.source_namespace != source_namespace:
        raise ValueError("portfolio and broker source namespaces differ")
    if account.account_snapshot_id is not None and account.account_snapshot_id != account_snapshot_id:
        raise ValueError("portfolio and broker account snapshot identities differ")

    converted_actions: list[PolicyActionProjection] = []
    mapping_facts: dict[str, SecuritySymbolMapping] = {}
    mapping_session = valuation_session
    mapping_time = valuation_time

    def add_mapping(source: object, *, identity: str) -> None:
        security_id = _source_member(source, "security_id")
        broker_symbol = _source_member(source, "broker_symbol")
        _identifier(security_id, "source security_id")
        _identifier(broker_symbol, "source broker_symbol")
        mapping = SecuritySymbolMapping(
            security_id=security_id,
            broker_symbol=broker_symbol,
            mapping_contract_id=f"portfolio:{snapshot_id}:{security_id}:{broker_symbol}",
            effective_from=mapping_session,
            effective_through=mapping_session,
            validated_at=mapping_time,
        )
        existing = mapping_facts.get(security_id)
        if existing is not None and existing != mapping:
            # Preserve both facts so the normal reconciliation emits a conflict.
            mapping_facts[f"{security_id}:{identity}"] = mapping
        else:
            mapping_facts[security_id] = mapping

    for source in action_projections:
        source_deployment = _source_member(source, "deployment_identity")
        source_identity = _source_member(source_deployment, "deployment_generation_id")
        source_environment = _source_member(source_deployment, "paper_account_environment_id")
        source_store = _source_member(source_deployment, "store_identity")
        if _source_member(source, "deployment_generation_id") != source_identity:
            raise ValueError("ActionStateProjection deployment identity is inconsistent")
        order_attempts_source = _source_member(source, "order_attempts")
        if type(order_attempts_source) is not tuple:
            raise TypeError("ActionStateProjection.order_attempts must be a tuple")
        attempts = tuple(
            OrderAttemptProjection(
                attempt_number=_source_member(attempt, "attempt_number"),
                requested_quantity=_source_number(
                    _source_member(attempt, "requested_quantity"), "attempt requested_quantity"
                ),
                confirmed_quantity=_source_number(
                    _source_member(attempt, "confirmed_filled_quantity"), "attempt confirmed quantity"
                ),
                status=_source_enum(_source_member(attempt, "status"), "attempt status"),
                client_order_id=_source_member(attempt, "client_order_id"),
                broker_order_id=_source_member(attempt, "broker_order_id"),
                terminal_status=(
                    None
                    if _source_member(attempt, "terminal_status") is None
                    else _source_enum(
                        _source_member(attempt, "terminal_status"), "attempt terminal_status"
                    )
                ),
                client_order_aliases=_source_member(attempt, "client_order_aliases"),
                broker_order_aliases=_source_member(attempt, "broker_order_aliases"),
            )
            for attempt in order_attempts_source
        )
        role = _source_enum(_source_member(source, "role"), "action role")
        side = _source_enum(_source_member(source, "side"), "action side")
        converted_actions.append(
            PolicyActionProjection(
                logical_action_id=_source_member(source, "logical_action_id"),
                deployment_generation_id=source_identity,
                holding_episode_id=_source_member(source, "holding_episode_id"),
                security_id=_source_member(source, "security_id"),
                role=role,
                side=side,
                status=_source_enum(_source_member(source, "status"), "action status"),
                requested_quantity=_source_number(
                    _source_member(source, "requested_quantity"), "action requested_quantity"
                ),
                confirmed_quantity=_source_number(
                    _source_member(source, "confirmed_quantity"), "action confirmed_quantity"
                ),
                residual_quantity=_source_number(
                    _source_member(source, "residual_quantity"), "action residual_quantity"
                ),
                reservation_amount=(
                    _source_number(_source_member(source, "reservation_amount"), "reservation_amount", nullable=True)
                    if side == "buy"
                    else None
                ),
                reservation_price=(
                    _source_number(_source_member(source, "reservation_price"), "reservation_price", nullable=True)
                    if side == "buy"
                    else None
                ),
                reservation_price_basis=(
                    _source_member(source, "reservation_price_basis") if side == "buy" else None
                ),
                reservation_stop_price=(
                    _source_number(
                        _source_member(source, "reservation_stop_price"),
                        "reservation_stop_price",
                        nullable=True,
                    )
                    if side == "buy"
                    else None
                ),
                client_order_refs=tuple(
                    dict.fromkeys(
                        order_id
                        for attempt in attempts
                        for order_id in (
                            (() if attempt.client_order_id is None else (attempt.client_order_id,))
                            + attempt.client_order_aliases
                        )
                    )
                ),
                broker_order_refs=tuple(
                    dict.fromkeys(
                        order_id
                        for attempt in attempts
                        for order_id in (
                            (() if attempt.broker_order_id is None else (attempt.broker_order_id,))
                            + attempt.broker_order_aliases
                        )
                    )
                ),
                resolution_reason=_source_member(source, "resolution_reason"),
                reservation_risk_per_unit=(
                    _source_number(_source_member(source, "risk_per_unit"), "risk_per_unit", nullable=True)
                    if side == "buy"
                    else None
                ),
                reservation_risk_basis=(
                    _source_member(source, "risk_basis") if side == "buy" else None
                ),
                residual_committed_risk=(
                    _source_number(
                        _source_member(source, "residual_committed_risk"),
                        "residual_committed_risk",
                        nullable=True,
                    )
                    if side == "buy"
                    else None
                ),
                order_attempts=attempts,
                source_decision_slot_id=_source_member(source, "decision_slot_id"),
                source_decision_id=_source_member(source, "decision_id"),
                source_snapshot_sha256=_source_member(source, "snapshot_sha256"),
                source_clock_id=_source_member(source, "clock_id"),
                source_paper_account_environment_id=source_environment,
                source_store_identity=source_store,
                source_decision_session=_source_member(source, "decision_session"),
                source_as_of_cutoff_at=_source_member(source, "as_of_cutoff_at"),
                source_next_execution_session=_source_member(source, "next_execution_session"),
                source_account_valuation_session=_source_member(source, "account_valuation_session"),
                source_account_valuation_at=_source_member(source, "account_valuation_at"),
                source_state_version=_source_member(source, "state_version"),
            )
        )
        add_mapping(source, identity=_source_member(source, "logical_action_id"))

    converted_holdings: list[HoldingProjection] = []
    for source in holding_episodes:
        references: tuple[OrderReference, ...] = ()
        client_ref = _source_member(source, "confirmed_stop_client_order_id")
        broker_ref = _source_member(source, "confirmed_stop_broker_order_id")
        if client_ref is not None or broker_ref is not None:
            references = (OrderReference(broker_order_id=broker_ref, client_order_id=client_ref),)
        converted_holdings.append(
            HoldingProjection(
                holding_episode_id=_source_member(source, "holding_episode_id"),
                deployment_generation_id=_source_member(source, "deployment_generation_id"),
                security_id=_source_member(source, "security_id"),
                remaining_quantity=_source_number(
                    _source_member(source, "remaining_quantity"), "holding remaining_quantity"
                ),
                stop_price=_source_number(
                    _source_member(source, "confirmed_protective_stop_price"),
                    "confirmed protective stop price",
                    nullable=True,
                ),
                stop_observed_at=_source_member(source, "confirmed_stop_observed_at"),
                protective_stop_order_references=references,
                source_state_version=_source_member(source, "state_version"),
            )
        )
        add_mapping(source, identity=_source_member(source, "holding_episode_id"))

    return PolicyStateProjection(
        snapshot_id=snapshot_id,
        decision_clock_id=clock_id,
        decision_slot_id=account.decision_slot_id,
        decision_id=account.decision_id,
        decision_session=completed_session,
        input_cutoff_at=cutoff,
        next_execution_session=next_execution_session,
        active_deployment_generation_id=generation_id,
        paper_account_environment_id=account_environment_id,
        store_identity=store_identity,
        holdings=tuple(converted_holdings),
        pending_actions=tuple(converted_actions),
        security_symbol_mappings=tuple(mapping_facts.values()),
        source_namespace=source_namespace,
        account_snapshot_id=account_snapshot_id,
        canonical_portfolio_facts=canonical_portfolio_facts,
    )


@dataclass(frozen=True, slots=True)
class ReconciliationFinding:
    path: str
    state: str
    detail: str


@dataclass(frozen=True, slots=True)
class AccountReconciliation:
    snapshot_id: str
    completed_session: date
    as_of_cutoff: datetime
    next_execution_session: date
    valuation_time: datetime
    ready: bool
    findings: tuple[ReconciliationFinding, ...]
    equity: float | None
    settled_cash: float | None
    reserved_buy_cash: float | None
    available_cash: float | None
    reserved_buy_risk: float | None
    pending_entry_count: int | None
    pending_sell_count: int | None
    gross_exposure: float | None
    open_position_risk: float | None
    sector_exposures: tuple[tuple[str, float], ...] | None
    industry_exposures: tuple[tuple[str, float], ...] | None
    portfolio_features: PortfolioFeaturesV3 | None
    total_committed_risk: float | None


def _finding(
    findings: list[ReconciliationFinding], path: str, state: str, detail: str
) -> None:
    finding = ReconciliationFinding(path=path, state=state, detail=detail)
    if finding not in findings:
        findings.append(finding)


def _fresh_timestamp(
    observed_at: datetime | None,
    *,
    clock: AccountValuationClock,
    max_age: timedelta,
    path: str,
    findings: list[ReconciliationFinding],
    stale_state: bool = True,
) -> bool:
    if observed_at is None:
        _finding(findings, path, "absent", "observation time is missing")
        return False
    if observed_at > clock.valuation_time:
        _finding(findings, path, "invalid", "observation is after the declared valuation time")
        return False
    age = clock.valuation_time - observed_at
    if stale_state and age > max_age:
        _finding(findings, path, "stale_by_declared_rule", "observation exceeds its declared age")
        return False
    return True


def _deduplicate(
    items: tuple[object, ...],
    *,
    key_of: object,
    path: str,
    findings: list[ReconciliationFinding],
) -> dict[str, object] | None:
    result: dict[str, object] = {}
    key_function = key_of
    assert callable(key_function)
    for item in items:
        key = key_function(item)
        existing = result.get(key)
        if existing is not None and existing != item:
            _finding(findings, f"{path}.{key}", "conflicting", "duplicate identity has different facts")
        else:
            result[key] = item
    return result


def _classification_code(
    classification: ClassificationFact | None,
    *,
    name: str,
    clock: AccountValuationClock,
    findings: list[ReconciliationFinding],
) -> str | None:
    if classification is None:
        _finding(findings, name, "absent", "classification is unavailable")
        return None
    if classification.source_state != "observed":
        _finding(findings, name, classification.source_state, "classification is not observed")
        return None
    if classification.code is None:
        _finding(findings, name, "absent", "observed classification has no code")
        return None
    if classification.source_public_at is None or classification.available_from_session is None:
        _finding(findings, name, "invalid", "classification visibility provenance is incomplete")
        return None
    if classification.source_public_at > clock.as_of_cutoff:
        _finding(findings, name, "not_yet_public", "classification was not public by the cutoff")
        return None
    if classification.available_from_session > clock.completed_session:
        _finding(findings, name, "not_yet_public", "classification is not eligible for this session")
        return None
    if classification.effective_from is None or classification.effective_from > clock.completed_session:
        _finding(findings, name, "unsupported_scope", "classification is not effective for this session")
        return None
    if (
        classification.effective_through is not None
        and classification.effective_through < clock.completed_session
    ):
        _finding(findings, name, "stale_by_declared_rule", "classification is no longer effective")
        return None
    return classification.code


def _deduplicate_positions(
    positions: tuple[BrokerPositionFact, ...], findings: list[ReconciliationFinding]
) -> dict[str, BrokerPositionFact] | None:
    result: dict[str, BrokerPositionFact] = {}
    for position in positions:
        existing = result.get(position.symbol)
        if existing is not None and existing != position:
            _finding(
                findings,
                f"positions.{position.symbol}",
                "conflicting",
                "duplicate symbol has different broker valuation facts",
            )
        else:
            result[position.symbol] = position
    return result


def _build_security_symbol_map(
    *,
    projection: PolicyStateProjection,
    valuation_session: date,
    valuation_time: datetime,
    findings: list[ReconciliationFinding],
) -> dict[str, str] | None:
    if projection.security_symbol_mappings is None:
        _finding(findings, "security_symbol_mappings", "absent", "stable security-to-symbol mapping is unavailable")
        return None
    records = _deduplicate(
        projection.security_symbol_mappings,
        key_of=lambda item: item.security_id,  # type: ignore[attr-defined]
        path="security_symbol_mappings",
        findings=findings,
    )
    assert records is not None
    by_security: dict[str, str] = {}
    by_symbol: dict[str, str] = {}
    for security_id, value in records.items():
        if not isinstance(value, SecuritySymbolMapping):
            continue
        path = f"security_symbol_mappings.{security_id}"
        session = valuation_session
        if value.validated_at > valuation_time:
            _finding(findings, path, "invalid", "mapping validation follows account valuation time")
            continue
        if value.effective_from > session or (
            value.effective_through is not None and value.effective_through < session
        ):
            _finding(findings, path, "stale_by_declared_rule", "mapping is not effective for the account valuation session")
            continue
        other_security = by_symbol.get(value.broker_symbol)
        if other_security is not None and other_security != security_id:
            _finding(findings, path, "conflicting", "one broker symbol maps to multiple stable security identities")
            continue
        by_security[security_id] = value.broker_symbol
        by_symbol[value.broker_symbol] = security_id
    return by_security


def _merge_duplicate_broker_orders(
    records: tuple[BrokerOrderFact, ...],
    findings: list[ReconciliationFinding],
) -> list[BrokerOrderFact]:
    orders: list[BrokerOrderFact] = []
    for record in records:
        matches = [
            existing
            for existing in orders
            if (
                record.broker_order_id is not None
                and record.broker_order_id == existing.broker_order_id
            )
            or (
                record.client_order_id is not None
                and record.client_order_id == existing.client_order_id
            )
        ]
        if not matches:
            orders.append(record)
            continue
        if len(matches) != 1:
            _finding(findings, "open_orders", "conflicting", "broker order references overlap multiple records")
            orders.append(record)
            continue
        existing = matches[0]
        aliases_conflict = (
            record.broker_order_id is not None
            and existing.broker_order_id is not None
            and record.broker_order_id != existing.broker_order_id
        ) or (
            record.client_order_id is not None
            and existing.client_order_id is not None
            and record.client_order_id != existing.client_order_id
        )
        if aliases_conflict:
            _finding(
                findings,
                f"open_orders.{record.broker_order_id or record.client_order_id}",
                "conflicting",
                "duplicate reference has inconsistent broker/client order aliases",
            )
            orders.append(record)
            continue
        same_fact = (
            record.symbol == existing.symbol
            and record.side == existing.side
            and record.purpose == existing.purpose
            and record.holding_episode_id == existing.holding_episode_id
            and record.status == existing.status
            and _close(record.requested_quantity, existing.requested_quantity)
            and _close(record.cumulative_filled_quantity, existing.cumulative_filled_quantity)
            and (
                record.stop_price is None
                or existing.stop_price is None
                or _close(record.stop_price, existing.stop_price)
            )
        )
        if not same_fact:
            _finding(
                findings,
                f"open_orders.{record.broker_order_id or record.client_order_id}",
                "conflicting",
                "duplicate reference has inconsistent broker facts",
            )
            orders.append(record)
            continue
        merged = BrokerOrderFact(
            broker_order_id=existing.broker_order_id or record.broker_order_id,
            client_order_id=existing.client_order_id or record.client_order_id,
            symbol=existing.symbol,
            side=existing.side,
            status=existing.status,
            requested_quantity=existing.requested_quantity,
            cumulative_filled_quantity=existing.cumulative_filled_quantity,
            purpose=existing.purpose,
            holding_episode_id=existing.holding_episode_id or record.holding_episode_id,
            stop_price=existing.stop_price if existing.stop_price is not None else record.stop_price,
        )
        orders[orders.index(existing)] = merged
    return orders


def _validate_action_origin_clock(
    *,
    action: PolicyActionProjection,
    projection: PolicyStateProjection,
    account: BrokerAccountSnapshot,
    path: str,
    findings: list[ReconciliationFinding],
) -> bool:
    origin_fields = (
        action.source_clock_id,
        action.source_decision_slot_id,
        action.source_decision_id,
        action.source_snapshot_sha256,
        action.source_decision_session,
        action.source_as_of_cutoff_at,
        action.source_next_execution_session,
        action.source_account_valuation_session,
        action.source_account_valuation_at,
    )
    if all(value is None for value in origin_fields):
        return True
    if any(value is None for value in origin_fields):
        _finding(findings, path + ".source_clock", "absent", "action origin clock identity or fields are incomplete")
        return False

    assert action.source_decision_session is not None
    assert action.source_as_of_cutoff_at is not None
    assert action.source_next_execution_session is not None
    assert action.source_account_valuation_session is not None
    assert action.source_account_valuation_at is not None
    if (
        action.source_next_execution_session <= action.source_decision_session
        or action.source_account_valuation_session != action.source_next_execution_session
        or action.source_account_valuation_at < action.source_as_of_cutoff_at
    ):
        _finding(findings, path + ".source_clock", "invalid", "action origin clock fields do not form a valid decision and execution clock")
        return False
    if (
        action.source_decision_session > projection.decision_session
        or action.source_as_of_cutoff_at > projection.input_cutoff_at
        or action.source_next_execution_session > projection.next_execution_session
        or action.source_account_valuation_at > account.clock.valuation_time
    ):
        _finding(findings, path + ".source_clock", "invalid", "action origin clock is later than the current account snapshot")
        return False
    if (
        action.status in {"intended", "remainder_ready"}
        and action.source_next_execution_session < projection.next_execution_session
    ):
        _finding(
            findings,
            path + ".execution_session",
            "stale_by_declared_rule",
            "unsubmitted action missed its originating execution session and cannot be retargeted",
        )
        return False
    return True


def _reconcile_projected_actions(
    *,
    account: BrokerAccountSnapshot,
    projection: PolicyStateProjection,
    symbols: dict[str, str] | None,
    findings: list[ReconciliationFinding],
) -> tuple[float | None, float | None, int | None, int | None]:
    if account.open_orders is None:
        _finding(findings, "open_orders", "absent", "broker open-order snapshot is unavailable")
    if projection.pending_actions is None:
        _finding(findings, "pending_actions", "absent", "policy action projection is unavailable")
    if projection.holdings is None:
        _finding(findings, "holdings", "absent", "policy holding projection is unavailable")
    if account.open_orders is None or projection.pending_actions is None or projection.holdings is None:
        return None, None, None, None

    start_findings = len(findings)
    action_rows = _deduplicate(
        projection.pending_actions,
        key_of=lambda item: item.logical_action_id,  # type: ignore[attr-defined]
        path="pending_actions",
        findings=findings,
    )
    assert action_rows is not None
    actions = {
        key: item for key, item in action_rows.items() if isinstance(item, PolicyActionProjection)
    }
    holdings_by_id: dict[str, list[HoldingProjection]] = defaultdict(list)
    for holding in projection.holdings:
        holdings_by_id[holding.holding_episode_id].append(holding)
    for action_id, action in actions.items():
        if action.role not in _HOLDING_SPECIFIC_ROLES:
            continue
        path = f"pending_actions.{action_id}"
        holding_id = action.holding_episode_id
        matching_holdings = holdings_by_id.get(holding_id, []) if holding_id is not None else []
        distinct_holdings = set(matching_holdings)
        if not distinct_holdings:
            _finding(
                findings,
                path + ".holding_episode_id",
                "absent",
                "holding-specific action has no matching projected holding episode",
            )
        elif len(distinct_holdings) != 1:
            _finding(
                findings,
                path + ".holding_episode_id",
                "conflicting",
                "holding-specific action maps to conflicting projected holding episodes",
            )
        else:
            holding = next(iter(distinct_holdings))
            if action.security_id != holding.security_id:
                _finding(
                    findings,
                    path + ".security_id",
                    "conflicting",
                    "action security differs from its projected holding episode",
                )
            if action.deployment_generation_id != holding.deployment_generation_id:
                _finding(
                    findings,
                    path + ".deployment_generation_id",
                    "conflicting",
                    "action deployment generation differs from its projected holding episode",
                )
    orders = _merge_duplicate_broker_orders(account.open_orders, findings)
    fully_mapped = symbols is not None and len(findings) == start_findings

    action_owner: dict[tuple[str, str], set[str]] = defaultdict(set)
    for action_id, action in actions.items():
        for reference in action.client_order_refs:
            action_owner[("client", reference)].add(action_id)
        for reference in action.broker_order_refs:
            action_owner[("broker", reference)].add(action_id)
    holding_owner: dict[tuple[str, str], set[str]] = defaultdict(set)
    for holding in projection.holdings:
        for reference in holding.protective_stop_order_references:
            if reference.client_order_id is not None:
                holding_owner[("client", reference.client_order_id)].add(holding.holding_episode_id)
            if reference.broker_order_id is not None:
                holding_owner[("broker", reference.broker_order_id)].add(holding.holding_episode_id)

    by_action: dict[str, list[BrokerOrderFact]] = defaultdict(list)
    by_holding: dict[str, list[BrokerOrderFact]] = defaultdict(list)
    coverage_valid = fully_mapped
    for order in orders:
        name = order.broker_order_id or order.client_order_id or "unknown"
        if order.status not in _BROKER_ACTIVE_STATES | _BROKER_TERMINAL_STATES:
            _finding(findings, f"open_orders.{name}.status", "unresolved", "broker order status is unknown")
            coverage_valid = False
        elif order.status == "expired":
            _finding(findings, f"open_orders.{name}.status", "unresolved", "expired orders have no canonical local attempt state")
            coverage_valid = False
        refs: set[tuple[str, str]] = set()
        if order.broker_order_id:
            refs.add(("broker", order.broker_order_id))
        if order.client_order_id:
            refs.add(("client", order.client_order_id))
        actions_for_order = set().union(*(action_owner.get(ref, set()) for ref in refs))
        holdings_for_order = set().union(*(holding_owner.get(ref, set()) for ref in refs))
        owners = actions_for_order | holdings_for_order
        reference_owners = [
            (action_owner.get(reference, set()), holding_owner.get(reference, set()))
            for reference in refs
        ]
        if reference_owners and any(item != reference_owners[0] for item in reference_owners[1:]):
            _finding(
                findings,
                f"open_orders.{name}",
                "conflicting",
                "broker/client order aliases do not resolve to the same local owner",
            )
            coverage_valid = False
            continue
        if order.purpose == "strategy":
            if len(actions_for_order) != 1 or holdings_for_order:
                _finding(findings, f"open_orders.{name}", "conflicting" if owners else "absent", "strategy order does not map to exactly one logical action")
                coverage_valid = False
            else:
                by_action[next(iter(actions_for_order))].append(order)
        else:
            if len(holdings_for_order) != 1 or actions_for_order:
                _finding(findings, f"open_orders.{name}", "conflicting" if owners else "absent", "protective sell does not map to exactly one holding episode")
                coverage_valid = False
            else:
                by_holding[next(iter(holdings_for_order))].append(order)

    reserved_cash = 0.0
    reserved_risk = 0.0
    pending_buys = 0
    pending_sells = 0
    cash_valid = True
    risk_valid = True
    for action_id, action in actions.items():
        path = f"pending_actions.{action_id}"
        if not _validate_action_origin_clock(
            action=action,
            projection=projection,
            account=account,
            path=path,
            findings=findings,
        ):
            coverage_valid = False
        if (
            action.source_paper_account_environment_id is not None
            and action.source_paper_account_environment_id
            != projection.paper_account_environment_id
        ):
            _finding(findings, path + ".paper_account_environment_id", "conflicting", "action and portfolio account environments differ")
            coverage_valid = False
        if action.source_store_identity is not None and action.source_store_identity != projection.store_identity:
            _finding(findings, path + ".store_identity", "conflicting", "action and portfolio stores differ")
            coverage_valid = False
        if symbols is None or action.security_id not in symbols:
            _finding(findings, path + ".security_id", "absent", "stable security identity has no validated broker-symbol mapping")
            coverage_valid = False
        if action.status in _UNCERTAIN_STATES or action.status not in _ACTIVE_STATES | _TERMINAL_STATES:
            _finding(findings, path + ".status", "unresolved", "logical action state cannot release risk or reservation")
            coverage_valid = False
            continue
        if action.status == "resolved" and not action.resolution_reason:
            _finding(findings, path + ".resolution_reason", "absent", "resolved action has no explicit reconciliation reason")
            coverage_valid = False
        if action.status == "resolved":
            if not action.order_attempts:
                _finding(findings, path + ".order_attempts", "absent", "resolved action has no canonical terminal attempt evidence")
                coverage_valid = False
            for attempt in action.order_attempts:
                terminal_status = attempt.terminal_status or attempt.status
                if terminal_status not in {"filled", "cancelled", "rejected"}:
                    _finding(findings, path + ".order_attempts", "unresolved", "resolved action retains a non-terminal order attempt")
                    coverage_valid = False
        rows = by_action.get(action_id, [])
        active_rows = [row for row in rows if row.status in _BROKER_ACTIVE_STATES]
        if action.order_attempts:
            attempts = action.order_attempts
            if attempts[0].requested_quantity != action.requested_quantity:
                _finding(findings, path + ".order_attempts", "conflicting", "initial attempt target differs from the logical action")
                coverage_valid = False
            if not _close(
                math.fsum(attempt.confirmed_quantity for attempt in attempts),
                action.confirmed_quantity,
            ):
                _finding(findings, path + ".confirmed_quantity", "conflicting", "attempt fills do not sum to the action fill watermark")
                coverage_valid = False
            for row in rows:
                matching_attempts = [
                    attempt
                    for attempt in attempts
                    if (
                        row.client_order_id is None
                        or row.client_order_id
                        in {attempt.client_order_id, *attempt.client_order_aliases}
                    )
                    and (
                        row.broker_order_id is None
                        or row.broker_order_id
                        in {attempt.broker_order_id, *attempt.broker_order_aliases}
                    )
                    and (row.client_order_id is not None or row.broker_order_id is not None)
                ]
                if len(matching_attempts) != 1:
                    partial_match = any(
                        (
                            row.client_order_id is not None
                            and row.client_order_id
                            in {attempt.client_order_id, *attempt.client_order_aliases}
                        )
                        or (
                            row.broker_order_id is not None
                            and row.broker_order_id
                            in {attempt.broker_order_id, *attempt.broker_order_aliases}
                        )
                        for attempt in attempts
                    )
                    _finding(findings, path + ".order_attempts", "conflicting" if partial_match else "absent", "broker order does not map to exactly one local attempt with all supplied aliases")
                    coverage_valid = False
                    continue
                attempt = matching_attempts[0]
                if (
                    not _close(row.requested_quantity, attempt.requested_quantity)
                    or not _close(row.cumulative_filled_quantity, attempt.confirmed_quantity)
                ):
                    _finding(findings, path + ".order_attempts", "conflicting", "broker order quantities differ from the attempt fill watermark")
                    coverage_valid = False
                if row.status in _BROKER_ACTIVE_STATES and attempt.status not in {
                    "submitted",
                    "partially_filled",
                    "cancel_requested",
                }:
                    _finding(findings, path + ".order_attempts", "conflicting", "active broker order differs from the local attempt state")
                    coverage_valid = False
                terminal_pair = {
                    "filled": "filled",
                    "cancelled": "cancelled",
                    "rejected": "rejected",
                }
                terminal_attempt_status = attempt.terminal_status or attempt.status
                if row.status in terminal_pair and terminal_attempt_status != terminal_pair[row.status]:
                    _finding(findings, path + ".order_attempts", "conflicting", "terminal broker order differs from the local attempt state")
                    coverage_valid = False
            if action.status == "intended":
                expected_attempts = attempts[:-1]
                current_attempt = attempts[-1]
                if (
                    current_attempt.status != "intended"
                    or any(item.status not in {"cancelled", "rejected"} for item in expected_attempts)
                    or active_rows
                ):
                    _finding(findings, path + ".order_attempts", "conflicting", "intended action lacks one unsubmitted current attempt")
                    coverage_valid = False
            elif action.status == "remainder_ready":
                if (
                    len(attempts) != 1
                    or attempts[0].status not in {"cancelled", "rejected"}
                    or active_rows
                ):
                    _finding(findings, path + ".order_attempts", "conflicting", "remainder-ready action lacks a terminal first attempt")
                    coverage_valid = False
            elif action.status in _ACTIVE_STATES - {"remainder_ready", "intended"}:
                current_attempts = [
                    item
                    for item in attempts
                    if item.status in {"submitted", "partially_filled", "cancel_requested"}
                ]
                if len(active_rows) != 1 or len(current_attempts) != 1:
                    _finding(findings, path + ".order_attempts", "conflicting" if active_rows or current_attempts else "absent", "submitted action must have one active broker order and attempt")
                    coverage_valid = False
            elif active_rows:
                _finding(findings, path + ".status", "conflicting", "terminal local action still has an active broker order")
                coverage_valid = False
        elif action.status == "intended":
            if rows or action.client_order_refs or action.broker_order_refs:
                _finding(findings, path + ".order_refs", "conflicting", "intended action already has broker submission references")
                coverage_valid = False
        elif action.status == "remainder_ready":
            if active_rows:
                _finding(findings, path + ".order_refs", "conflicting", "remainder-ready action still has an active broker order")
                coverage_valid = False
        elif action.status in _ACTIVE_STATES:
            if len(active_rows) != 1:
                _finding(findings, path + ".order_refs", "conflicting" if len(active_rows) > 1 else "absent", "submitted action must map to exactly one active broker order")
                coverage_valid = False
            if any(row.status not in _BROKER_ACTIVE_STATES for row in rows):
                _finding(findings, path + ".status", "conflicting", "local and broker action terminal states disagree")
                coverage_valid = False
        elif any(row.status in _BROKER_ACTIVE_STATES for row in rows):
            _finding(findings, path + ".status", "conflicting", "terminal local action still has an active broker order")
            coverage_valid = False
        if rows:
            expected_symbol = symbols.get(action.security_id) if symbols is not None else None
            if any(
                row.symbol != expected_symbol or row.side != action.side
                for row in rows
            ):
                _finding(findings, path + ".order_refs", "conflicting", "broker order symbol or side differs from the logical action")
                coverage_valid = False
            active_residual = math.fsum(
                row.requested_quantity - row.cumulative_filled_quantity for row in active_rows
            )
            if active_rows and not _close(active_residual, action.residual_quantity):
                _finding(findings, path + ".residual_quantity", "conflicting", "broker residual differs from logical-action residual")
                coverage_valid = False
        elif action.status in _ACTIVE_STATES - {"intended", "remainder_ready"}:
            _finding(findings, path + ".order_refs", "absent", "submitted action has no matching broker order")
            coverage_valid = False
        if action.status == "filled" and action.residual_quantity != 0:
            _finding(findings, path + ".status", "conflicting", "filled action retains a residual quantity")
            coverage_valid = False

        if action.status in _ACTIVE_STATES and action.residual_quantity > 0:
            if action.side == "buy":
                pending_buys += 1
                if (
                    action.reservation_amount is None
                    or action.reservation_price is None
                    or action.reservation_price_basis is None
                ):
                    _finding(findings, path + ".reservation_amount", "absent", "committed buy cash amount or basis is unavailable")
                    cash_valid = False
                else:
                    calculated_amount = action.residual_quantity * float(action.reservation_price)
                    if not _close(calculated_amount, float(action.reservation_amount)):
                        _finding(findings, path + ".reservation_amount", "conflicting", "projection reservation amount differs from residual quantity times price")
                        cash_valid = False
                    else:
                        reserved_cash += float(action.reservation_amount)
                if action.source_clock_id is not None:
                    if (
                        action.reservation_risk_basis is None
                        or action.residual_committed_risk is None
                    ):
                        _finding(findings, path + ".residual_committed_risk", "absent", "canonical pending risk amount or basis is unavailable")
                        risk_valid = False
                    else:
                        expected_risk = (
                            action.residual_quantity * action.reservation_risk_per_unit
                            if action.reservation_risk_per_unit is not None
                            else None
                        )
                        if expected_risk is not None and not _close(
                            expected_risk, action.residual_committed_risk
                        ):
                            _finding(findings, path + ".residual_committed_risk", "conflicting", "canonical residual risk differs from quantity times risk per unit")
                            risk_valid = False
                        else:
                            reserved_risk += action.residual_committed_risk
                elif action.residual_committed_risk is not None:
                    reserved_risk += action.residual_committed_risk
                elif action.reservation_stop_price is None or action.reservation_price is None:
                    _finding(findings, path + ".reservation_stop_price", "absent", "committed buy risk basis is unavailable")
                    risk_valid = False
                else:
                    reserved_risk += action.residual_quantity * max(
                        float(action.reservation_price) - float(action.reservation_stop_price),
                        0.0,
                    )
            else:
                pending_sells += 1

    for holding in projection.holdings:
        path = f"holdings.{holding.holding_episode_id}"
        rows = by_holding.get(holding.holding_episode_id, [])
        if holding.protective_stop_order_references and len(
            [row for row in rows if row.status in _BROKER_ACTIVE_STATES]
        ) != 1:
            _finding(findings, path + ".protective_stop", "absent", "recorded protective stop has no unique active broker order")
            coverage_valid = False
        for row in rows:
            expected_symbol = symbols.get(holding.security_id) if symbols is not None else None
            residual = row.requested_quantity - row.cumulative_filled_quantity
            if (
                row.side != "sell"
                or row.holding_episode_id != holding.holding_episode_id
                or row.symbol != expected_symbol
                or not _close(residual, holding.remaining_quantity)
            ):
                _finding(findings, path + ".protective_stop", "conflicting", "protective sell identity or quantity differs from the holding")
                coverage_valid = False
            if row.stop_price is None or holding.stop_price is None:
                _finding(findings, path + ".protective_stop_price", "absent", "broker-confirmed protective stop price is unavailable")
                coverage_valid = False
            elif not _close(row.stop_price, holding.stop_price):
                _finding(findings, path + ".protective_stop_price", "conflicting", "broker-confirmed and projected stop prices differ")
                coverage_valid = False

    if not coverage_valid:
        return None, None, None, None
    return (
        reserved_cash if cash_valid else None,
        reserved_risk if risk_valid else None,
        pending_buys,
        pending_sells,
    )


def _compare_canonical_portfolio_facts(
    *,
    canonical: CanonicalPortfolioFacts | None,
    reconciled: dict[str, float | None],
    findings: list[ReconciliationFinding],
) -> None:
    if canonical is None:
        return
    for name in ("equity", "cash", "gross_exposure", "open_risk", "peak_equity"):
        path = f"portfolio_snapshot.{name}"
        expected = getattr(canonical, name)
        actual = reconciled[name]
        if expected is None:
            _finding(findings, path, "absent", "canonical portfolio fact is unavailable")
        elif actual is None:
            _finding(findings, path, "absent", "same-snapshot broker or recomputed fact is unavailable")
        elif not _close(expected, actual):
            _finding(findings, path, "conflicting", "canonical portfolio fact differs from broker or recomputed state")


def reconcile_account_snapshot(
    *,
    account: BrokerAccountSnapshot,
    projection: PolicyStateProjection,
    maximum_balance_age: timedelta,
    maximum_mark_age: timedelta,
) -> AccountReconciliation:
    """Reconcile synthetic broker observations with #100's projected state semantics."""
    if type(account) is not BrokerAccountSnapshot or type(projection) is not PolicyStateProjection:
        raise TypeError("account and projection must use the closed reconciliation contracts")
    if not isinstance(maximum_balance_age, timedelta) or maximum_balance_age <= timedelta(0):
        raise ValueError("maximum_balance_age must be positive")
    if not isinstance(maximum_mark_age, timedelta) or maximum_mark_age <= timedelta(0):
        raise ValueError("maximum_mark_age must be positive")

    clock = account.clock
    findings: list[ReconciliationFinding] = []
    if account.decision_slot_id != projection.decision_slot_id:
        _finding(findings, "decision_slot_id", "conflicting", "account facts target a different decision slot")
    if account.decision_id != projection.decision_id:
        _finding(findings, "decision_id", "conflicting", "account facts target a different decision snapshot")
    if (
        projection.decision_session != clock.completed_session
        or projection.input_cutoff_at != clock.as_of_cutoff
        or projection.next_execution_session != clock.next_execution_session
    ):
        _finding(findings, "decision_clock", "conflicting", "account valuation belongs to an incompatible policy decision clock")
    if projection.paper_account_environment_id != account.paper_account_environment_id:
        _finding(findings, "paper_account_environment_id", "conflicting", "account and policy projection identities differ")
    if projection.source_namespace is not None:
        if account.source_namespace is None:
            _finding(findings, "source_namespace", "absent", "broker source namespace is unavailable")
        elif account.source_namespace != projection.source_namespace:
            _finding(findings, "source_namespace", "conflicting", "broker and policy source namespaces differ")
    if projection.account_snapshot_id is not None:
        if account.account_snapshot_id is None:
            _finding(findings, "account_snapshot_id", "absent", "broker account snapshot identity is unavailable")
        elif account.account_snapshot_id != projection.account_snapshot_id:
            _finding(findings, "account_snapshot_id", "conflicting", "broker and policy account snapshots differ")

    symbols = _build_security_symbol_map(
        projection=projection,
        valuation_session=clock.next_execution_session,
        valuation_time=clock.valuation_time,
        findings=findings,
    )
    reserved_cash, reserved_risk, pending_buys, pending_sells = _reconcile_projected_actions(
        account=account,
        projection=projection,
        symbols=symbols,
        findings=findings,
    )

    balance_fresh = _fresh_timestamp(
        account.balance_observed_at,
        clock=clock,
        max_age=maximum_balance_age,
        path="balances.observed_at",
        findings=findings,
    )
    peak_time_valid = _fresh_timestamp(
        account.peak_observed_at,
        clock=clock,
        max_age=timedelta.max,
        path="balances.peak_observed_at",
        findings=findings,
        stale_state=False,
    )
    equity = float(account.equity) if balance_fresh and account.equity is not None else None
    cash = float(account.cash) if balance_fresh and account.cash is not None else None
    peak_equity = float(account.peak_equity) if peak_time_valid and account.peak_equity is not None else None
    if account.equity is None:
        _finding(findings, "balances.equity", "absent", "equity is unavailable")
    if account.cash is None:
        _finding(findings, "balances.cash", "absent", "settled cash is unavailable")
    if account.peak_equity is None:
        _finding(findings, "balances.peak_equity", "absent", "peak equity is unavailable")
    if equity is not None and equity <= 0:
        _finding(findings, "balances.equity", "invalid", "equity must be positive")
        equity = None
    if equity is not None and peak_equity is not None and peak_equity < equity and not _close(peak_equity, equity):
        _finding(findings, "balances.peak_equity", "conflicting", "peak equity is below current equity")
        peak_equity = None

    available_cash = cash - reserved_cash if cash is not None and reserved_cash is not None else None
    if available_cash is not None and available_cash < 0 and not _close(available_cash, 0.0):
        _finding(findings, "balances.available_cash", "conflicting", "residual buy reservations exceed settled cash")

    positions: dict[str, BrokerPositionFact] | None = None
    if account.positions is None:
        _finding(findings, "positions", "absent", "broker position snapshot is unavailable")
    else:
        positions = _deduplicate_positions(account.positions, findings)
    projected_holdings: dict[str, list[HoldingProjection]] | None = None
    if projection.holdings is None:
        _finding(findings, "holdings", "absent", "policy holding projection is unavailable")
    elif symbols is not None:
        holding_rows = _deduplicate(
            projection.holdings,
            key_of=lambda item: item.holding_episode_id,  # type: ignore[attr-defined]
            path="holdings",
            findings=findings,
        )
        projected_holdings = defaultdict(list)
        for holding in (holding_rows or {}).values():
            if not isinstance(holding, HoldingProjection):
                continue
            symbol = symbols.get(holding.security_id)
            if symbol is None:
                _finding(findings, f"holdings.{holding.holding_episode_id}.security_id", "absent", "holding security has no validated broker-symbol mapping")
                continue
            projected_holdings[symbol].append(holding)
            if holding.remaining_quantity > 0:
                if holding.stop_price is None:
                    _finding(findings, f"holdings.{holding.holding_episode_id}.stop_price", "absent", "holding stop is unavailable")
                if holding.stop_observed_at is None:
                    _finding(findings, f"holdings.{holding.holding_episode_id}.stop_observed_at", "absent", "holding stop timestamp is unavailable")
                elif holding.stop_observed_at > clock.valuation_time:
                    _finding(findings, f"holdings.{holding.holding_episode_id}.stop_observed_at", "invalid", "holding stop is after account valuation time")

    holdings_valid = positions is not None and projected_holdings is not None
    if positions is not None and projected_holdings is not None:
        for symbol in set(positions) | set(projected_holdings):
            broker_qty = positions[symbol].quantity if symbol in positions else 0.0
            projected_qty = math.fsum(
                row.remaining_quantity for row in projected_holdings.get(symbol, ())
            )
            if not _close(broker_qty, projected_qty):
                _finding(findings, f"holdings.{symbol}.quantity", "conflicting", "broker position quantity differs from durable projected quantity")
                holdings_valid = False

    valuation_valid = positions is not None
    classification_valid = positions is not None and equity is not None and equity > 0
    risk_valid = holdings_valid
    notionals: list[float] = []
    risks: list[float] = []
    sector_dollars: dict[str, float] = defaultdict(float)
    industry_dollars: dict[str, float] = defaultdict(float)
    if positions is not None:
        for symbol, position in positions.items():
            mark_valid = _fresh_timestamp(
                position.mark_observed_at,
                clock=clock,
                max_age=maximum_mark_age,
                path=f"positions.{symbol}.mark_observed_at",
                findings=findings,
            )
            valuation_valid &= mark_valid
            notional = float(position.quantity) * float(position.mark_price)
            notionals.append(notional)

            sector = _classification_code(
                position.sector,
                name=f"positions.{symbol}.sector",
                clock=clock,
                findings=findings,
            )
            industry = _classification_code(
                position.industry,
                name=f"positions.{symbol}.industry",
                clock=clock,
                findings=findings,
            )
            if sector is None or industry is None:
                classification_valid = False
            else:
                sector_dollars[sector] += notional
                industry_dollars[industry] += notional

            symbol_holds = (projected_holdings or {}).get(symbol, ())
            if not symbol_holds or not _close(
                math.fsum(row.remaining_quantity for row in symbol_holds), position.quantity
            ):
                risk_valid = False
            for holding in symbol_holds:
                if holding.remaining_quantity == 0:
                    continue
                if holding.stop_price is None or holding.stop_observed_at is None:
                    risk_valid = False
                    continue
                risks.append(
                    max(float(position.mark_price) - float(holding.stop_price), 0.0)
                    * float(holding.remaining_quantity)
                )

    gross = math.fsum(notionals) if valuation_valid else None
    open_risk = math.fsum(risks) if risk_valid and valuation_valid else None
    _compare_canonical_portfolio_facts(
        canonical=projection.canonical_portfolio_facts,
        reconciled={
            "equity": equity,
            "cash": cash,
            "gross_exposure": gross,
            "open_risk": open_risk,
            "peak_equity": peak_equity,
        },
        findings=findings,
    )
    sectors = (
        tuple(sorted((key, amount / equity) for key, amount in sector_dollars.items()))
        if classification_valid and equity is not None
        else None
    )
    industries = (
        tuple(sorted((key, amount / equity) for key, amount in industry_dollars.items()))
        if classification_valid and equity is not None
        else None
    )
    if equity is not None and cash is not None and gross is not None and not _close(equity, cash + gross):
        _finding(findings, "balances.equity", "conflicting", "settled cash plus gross exposure does not reconcile to equity")
    if equity is not None and gross is not None and gross > equity and not _close(gross, equity):
        _finding(findings, "positions.gross_exposure", "conflicting", "long-only gross exposure exceeds equity")

    portfolio: PortfolioFeaturesV3 | None = None
    if (
        not findings
        and equity is not None
        and cash is not None
        and peak_equity is not None
        and gross is not None
        and open_risk is not None
        and pending_buys is not None
        and classification_valid
    ):
        try:
            portfolio = StrategyPolicyAdapterV3.build_portfolio_features(
                equity=equity,
                cash=cash,
                peak_equity=peak_equity,
                position_notionals=notionals,
                position_open_risks=risks,
                pending_entry_count=pending_buys,
                sector_notionals=dict(sector_dollars),
                industry_notionals=dict(industry_dollars),
            )
        except ValueError as exc:
            _finding(findings, "portfolio_features", "invalid", str(exc))

    findings.sort(key=lambda item: (item.path, item.state, item.detail))
    committed_risk = (
        open_risk + reserved_risk
        if open_risk is not None and reserved_risk is not None
        else None
    )
    return AccountReconciliation(
        snapshot_id=projection.snapshot_id,
        completed_session=clock.completed_session,
        as_of_cutoff=clock.as_of_cutoff,
        next_execution_session=clock.next_execution_session,
        valuation_time=clock.valuation_time,
        ready=not findings and portfolio is not None,
        findings=tuple(findings),
        equity=equity,
        settled_cash=cash,
        reserved_buy_cash=reserved_cash,
        available_cash=available_cash,
        reserved_buy_risk=reserved_risk,
        pending_entry_count=pending_buys,
        pending_sell_count=pending_sells,
        gross_exposure=gross,
        open_position_risk=open_risk,
        sector_exposures=sectors,
        industry_exposures=industries,
        portfolio_features=portfolio,
        total_committed_risk=committed_risk,
    )


__all__ = [
    "AccountReconciliation",
    "AccountValuationClock",
    "CanonicalPortfolioFacts",
    "BrokerAccountSnapshot",
    "BrokerOrderFact",
    "BrokerPositionFact",
    "ClassificationFact",
    "HoldingProjection",
    "OrderAttemptProjection",
    "OrderReference",
    "PolicyActionProjection",
    "PolicyStateProjection",
    "ReconciliationFinding",
    "SecuritySymbolMapping",
    "policy_execution_state_to_projection",
    "reconcile_account_snapshot",
]
