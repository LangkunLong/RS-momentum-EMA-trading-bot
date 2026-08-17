"""Auditable order-execution workflow state machine.

This module centralizes the lifecycle for a single trade workflow so that
signal intake, planning, broker submission, fill handling, stop protection,
and notifications all share one authoritative, durable transition trail.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Optional

from core.execution_store import get_execution_store, reset_execution_store


class WorkflowState(StrEnum):
    """Lifecycle stages for a long-entry workflow."""

    SIGNAL_ACCEPTED = "signal_accepted"
    PLAN_BUILT = "plan_built"
    DRY_RUN_SKIPPED = "dry_run_skipped"
    ORDER_SUBMITTED = "order_submitted"
    ORDER_SUBMIT_FAILED = "order_submit_failed"
    EXIT_ORDER_SUBMITTED = "exit_order_submitted"
    EXIT_ORDER_SUBMIT_FAILED = "exit_order_submit_failed"
    RECOVERED_FROM_BROKER = "recovered_from_broker"
    BUY_FILL_RECEIVED = "buy_fill_received"
    PROTECTIVE_STOP_ACTIVE = "protective_stop_active"
    PROTECTIVE_STOP_FAILED = "protective_stop_failed"
    ENTRY_NOTIFICATION_SENT = "entry_notification_sent"
    ENTRY_NOTIFICATION_FAILED = "entry_notification_failed"
    BUY_FILL_NOTIFICATION_SENT = "buy_fill_notification_sent"
    BUY_FILL_NOTIFICATION_FAILED = "buy_fill_notification_failed"
    SELL_FILL_RECEIVED = "sell_fill_received"
    SELL_NOTIFICATION_SENT = "sell_notification_sent"
    SELL_NOTIFICATION_FAILED = "sell_notification_failed"


@dataclass(frozen=True)
class EntryExecutionPlan:
    """Deterministic entry plan derived from a scanner signal."""

    symbol: str
    entry_price: float
    price_source: str
    stop_price: float
    stop_loss_pct: float
    position_value: float
    risk_amount: float
    risk_per_share: float
    qty: float
    canslim_score: float
    rs_score: float
    is_breakout: bool
    has_volume_surge: bool


@dataclass(frozen=True)
class WorkflowTransition:
    """Immutable transition record for audit logging."""

    timestamp_utc: str
    workflow_id: str
    symbol: str
    from_state: str | None
    to_state: str
    event: str
    details: dict[str, Any]


@dataclass
class ExecutionWorkflow:
    """Stateful execution workflow with an append-only transition history."""

    workflow_id: str
    symbol: str
    state: WorkflowState | None = None
    transitions: list[WorkflowTransition] = field(default_factory=list)
    broker_order_id: str = ""
    entry_plan: EntryExecutionPlan | None = None
    created_at_utc: str = field(default_factory=lambda: _timestamp_utc())
    updated_at_utc: str = field(default_factory=lambda: _timestamp_utc())

    def transition(
        self,
        to_state: WorkflowState,
        *,
        event: str,
        details: Optional[dict[str, Any]] = None,
    ) -> WorkflowTransition:
        """Append a state transition and persist it to the durable store."""
        payload = dict(details or {})
        timestamp_utc = _timestamp_utc()
        record = WorkflowTransition(
            timestamp_utc=timestamp_utc,
            workflow_id=self.workflow_id,
            symbol=self.symbol,
            from_state=self.state.value if self.state is not None else None,
            to_state=to_state.value,
            event=event,
            details=payload,
        )
        self.transitions.append(record)
        self.state = to_state
        self.updated_at_utc = timestamp_utc
        _persist_workflow_state(self)
        get_execution_store().append_transition(
            timestamp_utc=record.timestamp_utc,
            workflow_id=record.workflow_id,
            symbol=record.symbol,
            from_state=record.from_state,
            to_state=record.to_state,
            event=record.event,
            details=record.details,
        )
        return record

    def mark_signal_accepted(self, *, signal_payload: Optional[dict[str, Any]] = None) -> None:
        self.transition(
            WorkflowState.SIGNAL_ACCEPTED,
            event="signal_accepted",
            details={"signal": dict(signal_payload or {})},
        )

    def mark_plan_built(self, plan: EntryExecutionPlan) -> None:
        self.entry_plan = plan
        self.transition(
            WorkflowState.PLAN_BUILT,
            event="plan_built",
            details={"plan": asdict(plan)},
        )

    def mark_dry_run_skipped(self) -> None:
        self.transition(
            WorkflowState.DRY_RUN_SKIPPED,
            event="dry_run_skipped",
            details={"reason": "dry_run_mode"},
        )

    def mark_order_submitted(self, *, broker_order_id: str) -> None:
        self.broker_order_id = broker_order_id
        self.transition(
            WorkflowState.ORDER_SUBMITTED,
            event="order_submitted",
            details={"broker_order_id": broker_order_id},
        )
        self._record_order_reference(
            broker_order_id=broker_order_id,
            client_order_id=self.workflow_id,
            order_role="entry_order",
        )

    def mark_order_submit_failed(self, *, error: str) -> None:
        self.transition(
            WorkflowState.ORDER_SUBMIT_FAILED,
            event="order_submit_failed",
            details={"error": error},
        )

    def mark_exit_order_submitted(self, *, exit_reason: str, broker_order_id: str) -> None:
        self.broker_order_id = broker_order_id
        self.transition(
            WorkflowState.EXIT_ORDER_SUBMITTED,
            event="exit_order_submitted",
            details={
                "exit_reason": exit_reason,
                "broker_order_id": broker_order_id,
            },
        )
        self._record_order_reference(
            broker_order_id=broker_order_id,
            client_order_id=self.workflow_id,
            order_role="exit_order",
        )

    def mark_exit_order_submit_failed(self, *, exit_reason: str, error: str) -> None:
        self.transition(
            WorkflowState.EXIT_ORDER_SUBMIT_FAILED,
            event="exit_order_submit_failed",
            details={"exit_reason": exit_reason, "error": error},
        )

    def mark_recovered_from_broker(self, *, broker_order_id: str | None = None) -> None:
        if broker_order_id:
            self.broker_order_id = broker_order_id
        self.transition(
            WorkflowState.RECOVERED_FROM_BROKER,
            event="workflow_recovered_from_broker_event",
            details={"broker_order_id": broker_order_id or ""},
        )
        if broker_order_id:
            self._record_order_reference(
                broker_order_id=broker_order_id,
                client_order_id=self.workflow_id,
                order_role="broker_recovery",
            )

    def mark_buy_fill(self, *, qty: float, fill_price: float, broker_order_id: str | None = None) -> None:
        if broker_order_id:
            self.broker_order_id = broker_order_id
        self.transition(
            WorkflowState.BUY_FILL_RECEIVED,
            event="buy_fill_received",
            details={
                "qty": qty,
                "fill_price": fill_price,
                "broker_order_id": broker_order_id or self.broker_order_id,
            },
        )
        if broker_order_id:
            self._record_order_reference(
                broker_order_id=broker_order_id,
                client_order_id=self.workflow_id,
                order_role="buy_fill",
            )
        self._mark_active_position(qty=qty, fill_price=fill_price)

    def mark_protective_stop(self, *, success: bool, stop_order_id: str, stop_price: float, action: str, error: str = "") -> None:
        self.transition(
            WorkflowState.PROTECTIVE_STOP_ACTIVE if success else WorkflowState.PROTECTIVE_STOP_FAILED,
            event="protective_stop_reconciled",
            details={
                "success": success,
                "stop_order_id": stop_order_id,
                "stop_price": stop_price,
                "action": action,
                "error": error,
            },
        )
        if success and stop_order_id:
            self._record_order_reference(
                broker_order_id=stop_order_id,
                client_order_id=build_stop_client_order_id(self.workflow_id),
                order_role="protective_stop",
            )

    def mark_entry_notification(self, *, sent: bool) -> None:
        self.transition(
            WorkflowState.ENTRY_NOTIFICATION_SENT if sent else WorkflowState.ENTRY_NOTIFICATION_FAILED,
            event="entry_submission_notified",
            details={"channel": "email", "sent": sent},
        )

    def mark_buy_fill_notification(self, *, sent: bool) -> None:
        self.transition(
            WorkflowState.BUY_FILL_NOTIFICATION_SENT if sent else WorkflowState.BUY_FILL_NOTIFICATION_FAILED,
            event="buy_fill_notified",
            details={"channel": "email", "sent": sent},
        )

    def mark_sell_fill(self, *, qty: float, fill_price: float, exit_reason: str, broker_order_id: str | None = None) -> None:
        if broker_order_id:
            self.broker_order_id = broker_order_id
        self.transition(
            WorkflowState.SELL_FILL_RECEIVED,
            event="sell_fill_received",
            details={
                "qty": qty,
                "fill_price": fill_price,
                "exit_reason": exit_reason,
                "broker_order_id": broker_order_id or self.broker_order_id,
            },
        )
        if broker_order_id:
            self._record_order_reference(
                broker_order_id=broker_order_id,
                client_order_id=self.workflow_id,
                order_role="sell_fill",
            )
        self._clear_active_position()

    def mark_sell_notification(self, *, sent: bool) -> None:
        self.transition(
            WorkflowState.SELL_NOTIFICATION_SENT if sent else WorkflowState.SELL_NOTIFICATION_FAILED,
            event="sell_fill_notified",
            details={"channel": "email", "sent": sent},
        )

    def _record_order_reference(
        self,
        *,
        broker_order_id: str = "",
        client_order_id: str = "",
        order_role: str,
    ) -> None:
        get_execution_store().record_order_reference(
            workflow_id=self.workflow_id,
            symbol=self.symbol,
            broker_order_id=broker_order_id,
            client_order_id=client_order_id,
            order_role=order_role,
            created_at_utc=self.updated_at_utc,
        )

    def _mark_active_position(self, *, qty: float, fill_price: float) -> None:
        get_execution_store().upsert_active_position(
            symbol=self.symbol,
            workflow_id=self.workflow_id,
            qty=qty,
            entry_price=fill_price,
            opened_at_utc=self.created_at_utc,
            updated_at_utc=self.updated_at_utc,
        )
        with _REGISTRY_LOCK:
            _ACTIVE_WORKFLOW_BY_SYMBOL[self.symbol] = self.workflow_id

    def _clear_active_position(self) -> None:
        get_execution_store().clear_active_position(self.symbol)
        with _REGISTRY_LOCK:
            _ACTIVE_WORKFLOW_BY_SYMBOL.pop(self.symbol, None)


_REGISTRY_LOCK = threading.Lock()
_WORKFLOW_REGISTRY: dict[str, ExecutionWorkflow] = {}
_LATEST_WORKFLOW_BY_SYMBOL: dict[str, str] = {}
_ACTIVE_WORKFLOW_BY_SYMBOL: dict[str, str] = {}


def generate_workflow_id(symbol: str) -> str:
    """Create a broker-safe workflow id that can also serve as client_order_id."""
    symbol_part = symbol.lower().replace("/", "")[:6]
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    return f"cslm-{symbol_part}-{timestamp}-{suffix}"


def create_entry_workflow(plan: EntryExecutionPlan, signal_payload: Optional[dict[str, Any]] = None) -> ExecutionWorkflow:
    """Create and register a new workflow for an actionable entry signal."""
    workflow = ExecutionWorkflow(workflow_id=generate_workflow_id(plan.symbol), symbol=plan.symbol)
    workflow.mark_signal_accepted(signal_payload=signal_payload)
    workflow.mark_plan_built(plan)
    register_workflow(workflow)
    return workflow


def register_workflow(workflow: ExecutionWorkflow) -> None:
    """Register a workflow in the hot cache for fast lookup."""
    with _REGISTRY_LOCK:
        _WORKFLOW_REGISTRY[workflow.workflow_id] = workflow
        _LATEST_WORKFLOW_BY_SYMBOL[workflow.symbol] = workflow.workflow_id


def get_workflow(workflow_id: str) -> ExecutionWorkflow | None:
    """Return a workflow by id, loading it from the durable store if needed."""
    with _REGISTRY_LOCK:
        cached = _WORKFLOW_REGISTRY.get(workflow_id)
        if cached is not None:
            return cached
    return _load_workflow_into_cache(workflow_id=workflow_id)


def get_latest_workflow_for_symbol(symbol: str) -> ExecutionWorkflow | None:
    """Return the most recent workflow for a symbol."""
    with _REGISTRY_LOCK:
        workflow_id = _LATEST_WORKFLOW_BY_SYMBOL.get(symbol)
        if workflow_id is not None:
            cached = _WORKFLOW_REGISTRY.get(workflow_id)
            if cached is not None:
                return cached
    payload = get_execution_store().load_latest_workflow_for_symbol(symbol)
    return _cache_payload(payload)


def get_active_workflow_for_symbol(symbol: str) -> ExecutionWorkflow | None:
    """Return the workflow that currently owns the open position for a symbol."""
    with _REGISTRY_LOCK:
        workflow_id = _ACTIVE_WORKFLOW_BY_SYMBOL.get(symbol)
        if workflow_id is not None:
            cached = _WORKFLOW_REGISTRY.get(workflow_id)
            if cached is not None:
                return cached
    payload = get_execution_store().load_active_workflow_for_symbol(symbol)
    workflow = _cache_payload(payload)
    if workflow is not None:
        with _REGISTRY_LOCK:
            _ACTIVE_WORKFLOW_BY_SYMBOL[symbol] = workflow.workflow_id
    return workflow


def get_workflow_by_broker_order_id(broker_order_id: str) -> ExecutionWorkflow | None:
    """Return a workflow resolved via a broker order id."""
    if not broker_order_id:
        return None
    payload = get_execution_store().load_workflow_by_broker_order_id(broker_order_id)
    return _cache_payload(payload)


def get_workflow_by_client_order_id(client_order_id: str) -> ExecutionWorkflow | None:
    """Return a workflow resolved via a client order id."""
    if not client_order_id:
        return None
    normalized_workflow_id = normalize_workflow_id(client_order_id)
    workflow = get_workflow(normalized_workflow_id) if normalized_workflow_id else None
    if workflow is not None:
        return workflow
    payload = get_execution_store().load_workflow_by_client_order_id(client_order_id)
    return _cache_payload(payload)


def get_or_recover_workflow(
    workflow_id: str,
    *,
    symbol: str,
    broker_order_id: str | None = None,
) -> ExecutionWorkflow:
    """Return a workflow, recovering it from a broker event if needed."""
    existing = get_workflow(workflow_id)
    if existing is not None:
        return existing

    recovered = ExecutionWorkflow(workflow_id=workflow_id, symbol=symbol)
    recovered.mark_recovered_from_broker(broker_order_id=broker_order_id)
    register_workflow(recovered)
    return recovered


def resolve_workflow(
    *,
    symbol: str,
    workflow_id: str = "",
    client_order_id: str = "",
    broker_order_id: str = "",
) -> ExecutionWorkflow | None:
    """Resolve a workflow from the strongest available broker references."""
    if workflow_id:
        workflow = get_workflow(workflow_id)
        if workflow is not None:
            return workflow
    if client_order_id:
        workflow = get_workflow_by_client_order_id(client_order_id)
        if workflow is not None:
            return workflow
    if broker_order_id:
        workflow = get_workflow_by_broker_order_id(broker_order_id)
        if workflow is not None:
            return workflow
    workflow = get_active_workflow_for_symbol(symbol)
    if workflow is not None:
        return workflow
    return get_latest_workflow_for_symbol(symbol)


def clear_workflow_registry() -> None:
    """Clear the in-memory cache. Intended for tests and recovery simulations."""
    with _REGISTRY_LOCK:
        _WORKFLOW_REGISTRY.clear()
        _LATEST_WORKFLOW_BY_SYMBOL.clear()
        _ACTIVE_WORKFLOW_BY_SYMBOL.clear()


def reset_workflow_state() -> None:
    """Clear both cache and durable execution data. Intended for tests."""
    clear_workflow_registry()
    reset_execution_store()


def normalize_workflow_id(raw_client_order_id: object) -> str:
    """Extract the base workflow id from an Alpaca client_order_id."""
    value = str(raw_client_order_id or "").strip()
    if not value:
        return ""
    for suffix in ("-sl", "-stop"):
        if value.endswith(suffix):
            return value[: -len(suffix)]
    return value


def build_stop_client_order_id(workflow_id: str) -> str:
    """Return the protective-stop client order id derived from the workflow id."""
    return f"{workflow_id}-sl"


def get_or_create_exit_workflow(symbol: str, *, exit_reason: str) -> ExecutionWorkflow:
    """Return the latest symbol workflow, or bootstrap one for a software exit."""
    existing = get_active_workflow_for_symbol(symbol)
    if existing is not None:
        return existing
    existing = get_latest_workflow_for_symbol(symbol)
    if existing is not None:
        return existing

    workflow = ExecutionWorkflow(workflow_id=generate_workflow_id(symbol), symbol=symbol)
    workflow.transition(
        WorkflowState.RECOVERED_FROM_BROKER,
        event="manual_exit_workflow_bootstrapped",
        details={"exit_reason": exit_reason},
    )
    register_workflow(workflow)
    return workflow


def _persist_workflow_state(workflow: ExecutionWorkflow) -> None:
    get_execution_store().upsert_workflow_snapshot(
        workflow_id=workflow.workflow_id,
        symbol=workflow.symbol,
        state=workflow.state.value if workflow.state is not None else None,
        broker_order_id=workflow.broker_order_id,
        entry_plan=asdict(workflow.entry_plan) if workflow.entry_plan is not None else None,
        created_at_utc=workflow.created_at_utc,
        updated_at_utc=workflow.updated_at_utc,
    )


def _load_workflow_into_cache(*, workflow_id: str) -> ExecutionWorkflow | None:
    payload = get_execution_store().load_workflow(workflow_id)
    return _cache_payload(payload)


def _cache_payload(payload: dict[str, Any] | None) -> ExecutionWorkflow | None:
    if payload is None:
        return None
    workflow = _workflow_from_payload(payload)
    register_workflow(workflow)
    return workflow


def _workflow_from_payload(payload: dict[str, Any]) -> ExecutionWorkflow:
    entry_plan_payload = payload.get("entry_plan")
    state_value = payload.get("state")
    return ExecutionWorkflow(
        workflow_id=payload["workflow_id"],
        symbol=payload["symbol"],
        state=WorkflowState(state_value) if state_value else None,
        transitions=[
            WorkflowTransition(
                timestamp_utc=record["timestamp_utc"],
                workflow_id=record["workflow_id"],
                symbol=record["symbol"],
                from_state=record["from_state"],
                to_state=record["to_state"],
                event=record["event"],
                details=dict(record["details"]),
            )
            for record in payload.get("transitions", [])
        ],
        broker_order_id=payload.get("broker_order_id", "") or "",
        entry_plan=EntryExecutionPlan(**entry_plan_payload) if entry_plan_payload else None,
        created_at_utc=payload.get("created_at_utc") or _timestamp_utc(),
        updated_at_utc=payload.get("updated_at_utc") or _timestamp_utc(),
    )




def _timestamp_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
