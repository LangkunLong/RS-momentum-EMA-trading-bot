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
from math import isfinite
from typing import Any, Optional

from core.execution_store import get_execution_store, reset_execution_store


class WorkflowState(StrEnum):
    """Lifecycle stages for a long-entry workflow."""

    SIGNAL_ACCEPTED = "signal_accepted"
    PLAN_BUILT = "plan_built"
    DRY_RUN_SKIPPED = "dry_run_skipped"
    ORDER_SUBMITTING = "order_submitting"
    ORDER_SUBMITTED = "order_submitted"
    ORDER_SUBMIT_FAILED = "order_submit_failed"
    EXIT_ORDER_SUBMITTING = "exit_order_submitting"
    EXIT_ORDER_SUBMITTED = "exit_order_submitted"
    EXIT_ORDER_SUBMIT_FAILED = "exit_order_submit_failed"
    RECOVERED_FROM_BROKER = "recovered_from_broker"
    BUY_FILL_RECEIVED = "buy_fill_received"
    PROTECTIVE_STOP_ACTIVE = "protective_stop_active"
    PROTECTIVE_STOP_FAILED = "protective_stop_failed"
    ENTRY_NOTIFICATION_SENT = "entry_notification_sent"
    ENTRY_NOTIFICATION_FAILED = "entry_notification_failed"
    BUY_FILL_NOTIFICATION_PENDING = "buy_fill_notification_pending"
    BUY_FILL_NOTIFICATION_SENT = "buy_fill_notification_sent"
    BUY_FILL_NOTIFICATION_FAILED = "buy_fill_notification_failed"
    SELL_PARTIAL_FILL_RECEIVED = "sell_partial_fill_received"
    SELL_FILL_RECEIVED = "sell_fill_received"
    SELL_NOTIFICATION_PENDING = "sell_notification_pending"
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
class ClosedSellCheckpoint:
    """One cumulative filled-quantity checkpoint in a closed sell chain."""

    broker_order_id: str
    client_order_id: str
    qty: float
    fill_price: float


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
        get_execution_store().persist_transition_and_snapshot(
            timestamp_utc=record.timestamp_utc,
            workflow_id=record.workflow_id,
            symbol=record.symbol,
            from_state=record.from_state,
            to_state=record.to_state,
            event=record.event,
            details=record.details,
            broker_order_id=self.broker_order_id,
            entry_plan=(
                asdict(self.entry_plan) if self.entry_plan is not None else None
            ),
            created_at_utc=self.created_at_utc,
            expected_transition_count=len(self.transitions),
        )
        self.transitions.append(record)
        self.state = to_state
        self.updated_at_utc = timestamp_utc
        with _REGISTRY_LOCK:
            _LATEST_WORKFLOW_BY_SYMBOL[self.symbol] = self.workflow_id
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

    def mark_order_submission_intent(
        self,
        *,
        client_order_id: str,
        qty: float,
        limit_price: float,
    ) -> None:
        """Persist the exact entry request identity before mutating the broker."""
        self.transition(
            WorkflowState.ORDER_SUBMITTING,
            event="entry_submission_intent",
            details={
                "symbol": self.symbol,
                "side": "buy",
                "client_order_id": client_order_id,
                "qty": qty,
                "limit_price": limit_price,
            },
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

    def mark_exit_submission_intent(
        self,
        *,
        exit_reason: str,
        client_order_id: str,
    ) -> None:
        """Persist the exact exit request identity before mutating the broker."""
        self.transition(
            WorkflowState.EXIT_ORDER_SUBMITTING,
            event="exit_submission_intent",
            details={
                "symbol": self.symbol,
                "side": "sell",
                "client_order_id": client_order_id,
                "exit_reason": exit_reason,
            },
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
            client_order_id=build_exit_client_order_id(self.workflow_id),
            order_role="exit_order",
        )

    def mark_exit_order_submit_failed(self, *, exit_reason: str, error: str) -> None:
        self.transition(
            WorkflowState.EXIT_ORDER_SUBMIT_FAILED,
            event="exit_order_submit_failed",
            details={"exit_reason": exit_reason, "error": error},
        )

    def mark_submission_intent_resolved(
        self,
        *,
        role: str,
        client_order_id: str,
        outcome: str,
        broker_order_id: str = "",
    ) -> None:
        """Durably close one exact submission intent after recovery is complete."""
        if self.state is None:
            raise RuntimeError("Cannot resolve a submission intent without workflow state")
        self.transition(
            self.state,
            event="submission_intent_resolved",
            details={
                "role": role,
                "client_order_id": client_order_id,
                "outcome": outcome,
                "broker_order_id": broker_order_id,
            },
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

    def mark_late_buy_exposure_recovered(
        self,
        *,
        qty: float,
        fill_price: float,
        broker_order_id: str,
    ) -> None:
        """Audit broker exposure that reappears after this workflow was sold."""
        if broker_order_id:
            self.broker_order_id = broker_order_id
        self.transition(
            WorkflowState.RECOVERED_FROM_BROKER,
            event="late_buy_exposure_recovered",
            details={
                "qty": qty,
                "fill_price": fill_price,
                "broker_order_id": broker_order_id,
            },
        )
        if broker_order_id:
            self.repair_entry_order_reference(
                broker_order_id=broker_order_id,
                client_order_id=self.workflow_id,
            )

    def mark_buy_fill(
        self,
        *,
        qty: float,
        fill_price: float,
        broker_order_id: str | None = None,
        restore_active: bool = True,
    ) -> None:
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
        if restore_active:
            self._mark_active_position(qty=qty, fill_price=fill_price)

    def mark_protective_stop(
        self,
        *,
        success: bool,
        stop_order_id: str,
        stop_price: float,
        action: str,
        error: str = "",
        stop_client_order_id: str = "",
    ) -> None:
        self.transition(
            WorkflowState.PROTECTIVE_STOP_ACTIVE if success else WorkflowState.PROTECTIVE_STOP_FAILED,
            event="protective_stop_reconciled",
            details={
                "success": success,
                "stop_order_id": stop_order_id,
                "stop_price": stop_price,
                "action": action,
                "error": error,
                "client_order_id": stop_client_order_id,
            },
        )
        if success and stop_order_id:
            self._record_order_reference(
                broker_order_id=stop_order_id,
                client_order_id=(
                    stop_client_order_id or build_stop_client_order_id(self.workflow_id)
                ),
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

    def claim_buy_fill_notification(self) -> bool:
        """Durably claim at-most-once buy-fill notification delivery."""
        claimed = get_execution_store().claim_notification(
            workflow_id=self.workflow_id,
            notification_kind="buy_fill",
            claimed_at_utc=_timestamp_utc(),
        )
        if not claimed:
            return False
        self.transition(
            WorkflowState.BUY_FILL_NOTIFICATION_PENDING,
            event="buy_fill_notification_claimed",
            details={"channel": "email"},
        )
        return True

    def mark_sell_fill(
        self,
        *,
        qty: float,
        fill_price: float,
        exit_reason: str,
        broker_order_id: str | None = None,
        client_order_id: str = "",
        clear_active: bool = True,
    ) -> None:
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
                "client_order_id": client_order_id,
            },
        )
        if broker_order_id:
            self._record_order_reference(
                broker_order_id=broker_order_id,
                client_order_id=client_order_id,
                order_role="sell_fill",
            )
        if clear_active:
            self._clear_active_position()

    def mark_sell_partial_fill(
        self,
        *,
        qty: float,
        fill_price: float,
        broker_order_id: str,
        client_order_id: str,
    ) -> None:
        """Persist a partial sell fill without clearing active ownership."""
        if broker_order_id:
            self.broker_order_id = broker_order_id
        self.transition(
            WorkflowState.SELL_PARTIAL_FILL_RECEIVED,
            event="sell_partial_fill_received",
            details={
                "qty": qty,
                "fill_price": fill_price,
                "broker_order_id": broker_order_id,
                "client_order_id": client_order_id,
            },
        )
        self._record_order_reference(
            broker_order_id=broker_order_id,
            client_order_id=client_order_id,
            order_role="sell_fill",
        )

    def replay_closed_sell_chain(
        self,
        checkpoints: list[ClosedSellCheckpoint],
        *,
        exit_reason: str,
        expected_active_position: dict[str, Any] | None = None,
    ) -> None:
        """Persist a broker-flat sell chain without historical REST snapshots."""
        if not checkpoints:
            raise ValueError("closed sell chain must contain at least one checkpoint")

        validated: list[ClosedSellCheckpoint] = []
        for checkpoint in checkpoints:
            broker_order_id = str(checkpoint.broker_order_id or "").strip()
            client_order_id = str(checkpoint.client_order_id or "").strip()
            try:
                qty = float(checkpoint.qty)
                fill_price = float(checkpoint.fill_price)
            except (TypeError, ValueError) as exc:
                raise ValueError("closed sell chain fill values must be numeric") from exc
            if (
                not broker_order_id
                or not client_order_id
                or not isfinite(qty)
                or not isfinite(fill_price)
                or qty <= 0
                or fill_price <= 0
            ):
                raise ValueError(
                    "closed sell chain requires nonempty ids and positive fill values"
                )
            validated.append(
                ClosedSellCheckpoint(
                    broker_order_id=broker_order_id,
                    client_order_id=client_order_id,
                    qty=qty,
                    fill_price=fill_price,
                )
            )

        def durable_qty_for(broker_order_id: str) -> float:
            return max(
                (
                    float(transition.details.get("qty", 0.0) or 0.0)
                    for transition in self.transitions
                    if transition.event
                    in {"sell_partial_fill_received", "sell_fill_received"}
                    and str(transition.details.get("broker_order_id", "") or "")
                    == broker_order_id
                ),
                default=0.0,
            )

        final = validated[-1]
        durable_final_qty = durable_qty_for(final.broker_order_id)
        if final.qty + 0.0001 < durable_final_qty:
            raise ValueError(
                "closed sell chain final quantity cannot regress below its "
                "durable order checkpoint"
            )

        for checkpoint in validated[:-1]:
            durable_qty = durable_qty_for(checkpoint.broker_order_id)
            if checkpoint.qty > durable_qty + 0.0001:
                self.mark_sell_partial_fill(
                    qty=checkpoint.qty,
                    fill_price=checkpoint.fill_price,
                    broker_order_id=checkpoint.broker_order_id,
                    client_order_id=checkpoint.client_order_id,
                )
            self.repair_sell_fill_storage(
                broker_order_id=checkpoint.broker_order_id,
                client_order_id=checkpoint.client_order_id,
                clear_active=False,
            )

        final_is_durable = any(
            transition.event == "sell_fill_received"
            and str(transition.details.get("broker_order_id", "") or "")
            == final.broker_order_id
            and abs(float(transition.details.get("qty", 0.0) or 0.0) - final.qty)
            <= 0.0001
            and abs(
                float(transition.details.get("fill_price", 0.0) or 0.0)
                - final.fill_price
            )
            <= 0.0001
            for transition in self.transitions
        )
        if not final_is_durable:
            self.mark_sell_fill(
                qty=final.qty,
                fill_price=final.fill_price,
                exit_reason=exit_reason,
                broker_order_id=final.broker_order_id,
                client_order_id=final.client_order_id,
                clear_active=False,
            )
        self.repair_sell_fill_storage(
            broker_order_id=final.broker_order_id,
            client_order_id=final.client_order_id,
            clear_active=False,
        )
        if expected_active_position is None:
            self._clear_active_position()
            return

        expected_symbol = str(expected_active_position.get("symbol", "") or "")
        expected_workflow_id = str(
            expected_active_position.get("workflow_id", "") or ""
        )
        if expected_symbol != self.symbol or expected_workflow_id != self.workflow_id:
            raise ValueError("expected active position does not belong to this workflow")
        try:
            expected_qty = float(expected_active_position["qty"])
            expected_entry_price = float(expected_active_position["entry_price"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("expected active position values are invalid") from exc
        expected_opened_at = str(expected_active_position.get("opened_at_utc", "") or "")
        expected_updated_at = str(expected_active_position.get("updated_at_utc", "") or "")
        if (
            not isfinite(expected_qty)
            or not isfinite(expected_entry_price)
            or expected_qty <= 0
            or expected_entry_price <= 0
            or not expected_opened_at
            or not expected_updated_at
        ):
            raise ValueError("expected active position values are invalid")

        removed = get_execution_store().clear_active_position_if_unchanged(
            symbol=self.symbol,
            workflow_id=self.workflow_id,
            qty=expected_qty,
            entry_price=expected_entry_price,
            opened_at_utc=expected_opened_at,
            updated_at_utc=expected_updated_at,
        )
        if not removed:
            raise RuntimeError("active ownership changed during closed sell replay")
        with _REGISTRY_LOCK:
            if _ACTIVE_WORKFLOW_BY_SYMBOL.get(self.symbol) == self.workflow_id:
                _ACTIVE_WORKFLOW_BY_SYMBOL.pop(self.symbol, None)

    def mark_sell_notification(self, *, sent: bool) -> None:
        self.transition(
            WorkflowState.SELL_NOTIFICATION_SENT if sent else WorkflowState.SELL_NOTIFICATION_FAILED,
            event="sell_fill_notified",
            details={"channel": "email", "sent": sent},
        )

    def claim_sell_notification(self) -> bool:
        """Durably claim at-most-once sell-fill notification delivery."""
        claimed = get_execution_store().claim_notification(
            workflow_id=self.workflow_id,
            notification_kind="sell_fill",
            claimed_at_utc=_timestamp_utc(),
        )
        if not claimed:
            return False
        self.transition(
            WorkflowState.SELL_NOTIFICATION_PENDING,
            event="sell_fill_notification_claimed",
            details={"channel": "email"},
        )
        return True

    def repair_buy_fill_storage(
        self,
        *,
        qty: float,
        fill_price: float,
        broker_order_id: str,
        restore_active: bool = True,
        preserve_higher_qty: bool = True,
    ) -> None:
        """Idempotently restore durable side effects after a recorded buy fill."""
        if broker_order_id:
            self._record_order_reference(
                broker_order_id=broker_order_id,
                client_order_id=self.workflow_id,
                order_role="buy_fill",
            )
        if restore_active:
            restored = get_execution_store().upsert_active_position_if_owner_matches(
                symbol=self.symbol,
                workflow_id=self.workflow_id,
                qty=qty,
                entry_price=fill_price,
                opened_at_utc=self.created_at_utc,
                updated_at_utc=self.updated_at_utc,
                preserve_higher_qty=preserve_higher_qty,
            )
            if restored:
                with _REGISTRY_LOCK:
                    _ACTIVE_WORKFLOW_BY_SYMBOL[self.symbol] = self.workflow_id

    def repair_entry_order_reference(
        self,
        *,
        broker_order_id: str,
        client_order_id: str = "",
    ) -> None:
        """Persist a trusted entry/replacement order discovered from broker ancestry."""
        if not broker_order_id:
            return
        self._record_order_reference(
            broker_order_id=broker_order_id,
            client_order_id=client_order_id,
            order_role="entry_order",
        )

    def repair_order_reference(
        self,
        *,
        broker_order_id: str,
        client_order_id: str = "",
        order_role: str,
    ) -> None:
        """Persist a trusted broker order relationship discovered from ancestry."""
        if not broker_order_id:
            return
        self._record_order_reference(
            broker_order_id=broker_order_id,
            client_order_id=client_order_id,
            order_role=order_role,
        )

    def claim_order_reference_from_submission_intent(
        self,
        *,
        broker_order_id: str,
        client_order_id: str,
        order_role: str,
        intent_event: str,
        side: str,
    ) -> None:
        """Atomically claim a broker id using the latest exact submission intent."""
        get_execution_store().claim_order_reference_from_submission_intent(
            workflow_id=self.workflow_id,
            symbol=self.symbol,
            broker_order_id=broker_order_id,
            client_order_id=client_order_id,
            order_role=order_role,
            intent_event=intent_event,
            side=side,
            created_at_utc=self.updated_at_utc,
        )

    def repair_sell_fill_storage(
        self,
        *,
        broker_order_id: str,
        client_order_id: str,
        clear_active: bool = True,
    ) -> None:
        """Idempotently restore a sell reference and clear only owned position state."""
        if broker_order_id:
            self._record_order_reference(
                broker_order_id=broker_order_id,
                client_order_id=client_order_id,
                order_role="sell_fill",
            )
        if clear_active:
            removed = get_execution_store().clear_active_position_for_workflow(
                self.symbol,
                self.workflow_id,
            )
            if removed:
                with _REGISTRY_LOCK:
                    if _ACTIVE_WORKFLOW_BY_SYMBOL.get(self.symbol) == self.workflow_id:
                        _ACTIVE_WORKFLOW_BY_SYMBOL.pop(self.symbol, None)

    def repair_protective_stop_reference(
        self,
        *,
        stop_order_id: str,
        stop_client_order_id: str = "",
    ) -> None:
        """Idempotently restore a durable stop-order reference."""
        if not stop_order_id:
            return
        self._record_order_reference(
            broker_order_id=stop_order_id,
            client_order_id=(
                stop_client_order_id or build_stop_client_order_id(self.workflow_id)
            ),
            order_role="protective_stop",
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
        removed = get_execution_store().clear_active_position_for_workflow(
            self.symbol,
            self.workflow_id,
        )
        if removed:
            with _REGISTRY_LOCK:
                if _ACTIVE_WORKFLOW_BY_SYMBOL.get(self.symbol) == self.workflow_id:
                    _ACTIVE_WORKFLOW_BY_SYMBOL.pop(self.symbol, None)


_REGISTRY_LOCK = threading.RLock()
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
    return _load_workflow_into_cache(workflow_id=workflow_id)


def get_latest_workflow_for_symbol(symbol: str) -> ExecutionWorkflow | None:
    """Return the most recent workflow for a symbol."""
    payload = get_execution_store().load_latest_workflow_for_symbol(symbol)
    workflow = _cache_payload(payload)
    if workflow is not None:
        with _REGISTRY_LOCK:
            _LATEST_WORKFLOW_BY_SYMBOL[symbol] = workflow.workflow_id
    return workflow


def get_active_workflow_for_symbol(symbol: str) -> ExecutionWorkflow | None:
    """Return the workflow that currently owns the open position for a symbol."""
    active = get_execution_store().load_active_position(symbol)
    if active is None:
        with _REGISTRY_LOCK:
            _ACTIVE_WORKFLOW_BY_SYMBOL.pop(symbol, None)
        return None
    workflow_id = str(active.get("workflow_id", "") or "")
    if not workflow_id:
        raise RuntimeError(f"Active {symbol} position has no workflow id")
    with _REGISTRY_LOCK:
        cached = _WORKFLOW_REGISTRY.get(workflow_id)
    payload = get_execution_store().load_workflow(workflow_id)
    workflow = _cache_payload(payload)
    if workflow is None:
        raise RuntimeError(
            f"Active {symbol} position references missing workflow {workflow_id}"
        )
    if workflow.symbol.strip().upper() != symbol.strip().upper():
        raise RuntimeError(
            f"Active {symbol} position references {workflow.symbol} workflow "
            f"{workflow.workflow_id}"
        )
    if cached is not None and cached is not workflow:
        raise RuntimeError(f"Canonical workflow identity changed for {workflow_id}")
    with _REGISTRY_LOCK:
        _ACTIVE_WORKFLOW_BY_SYMBOL[symbol] = workflow.workflow_id
    return workflow


def recover_active_position_workflow(
    symbol: str,
    *,
    qty: float,
    avg_entry_price: float,
) -> ExecutionWorkflow:
    """Create or heal durable ownership for a broker position found at startup."""
    normalized_symbol = str(symbol).strip().upper()
    resolved_qty = float(qty)
    resolved_entry_price = float(avg_entry_price)
    if not normalized_symbol or resolved_qty <= 0 or resolved_entry_price <= 0:
        raise ValueError("Recovered position symbol, quantity, and entry price must be positive")

    with _REGISTRY_LOCK:
        store = get_execution_store()
        active_record = store.load_active_position(normalized_symbol)
        active_workflow_id = str((active_record or {}).get("workflow_id", "") or "")
        existing = get_workflow(active_workflow_id) if active_workflow_id else None
        if existing is not None:
            if existing.symbol.strip().upper() != normalized_symbol:
                raise RuntimeError(
                    f"Active {normalized_symbol} position references "
                    f"{existing.symbol} workflow {existing.workflow_id}"
                )
            restored = get_execution_store().upsert_active_position_if_owner_matches(
                symbol=normalized_symbol,
                workflow_id=existing.workflow_id,
                qty=resolved_qty,
                entry_price=resolved_entry_price,
                opened_at_utc=existing.created_at_utc,
                updated_at_utc=_timestamp_utc(),
            )
            if not restored:
                raise RuntimeError(
                    f"Active {normalized_symbol} ownership changed during recovery"
                )
            _ACTIVE_WORKFLOW_BY_SYMBOL[normalized_symbol] = existing.workflow_id
            return existing

        workflow = ExecutionWorkflow(
            workflow_id=active_workflow_id or generate_workflow_id(normalized_symbol),
            symbol=normalized_symbol,
        )
        workflow.transition(
            WorkflowState.RECOVERED_FROM_BROKER,
            event="active_position_recovered_from_broker",
            details={
                "qty": resolved_qty,
                "avg_entry_price": resolved_entry_price,
            },
        )
        register_workflow(workflow)
        restored = store.upsert_active_position_if_owner_matches(
            symbol=normalized_symbol,
            workflow_id=workflow.workflow_id,
            qty=resolved_qty,
            entry_price=resolved_entry_price,
            opened_at_utc=workflow.created_at_utc,
            updated_at_utc=workflow.updated_at_utc,
        )
        if not restored:
            raise RuntimeError(
                f"Active {normalized_symbol} ownership changed during recovery"
            )
        _ACTIVE_WORKFLOW_BY_SYMBOL[normalized_symbol] = workflow.workflow_id
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
    store = get_execution_store()
    durable_owner_ids = store.find_workflow_ids_by_client_order_id(client_order_id)
    if len(durable_owner_ids) > 1:
        return None
    normalized_workflow_id = normalize_workflow_id(client_order_id)
    durable = (
        get_workflow(next(iter(durable_owner_ids)))
        if len(durable_owner_ids) == 1
        else None
    )
    if durable_owner_ids and durable is None:
        return None
    normalized = (
        get_workflow(normalized_workflow_id) if normalized_workflow_id else None
    )
    if durable is not None and normalized is not None:
        if durable.workflow_id != normalized.workflow_id:
            return None
        return durable
    return durable or normalized


def get_or_recover_workflow(
    workflow_id: str,
    *,
    symbol: str,
    broker_order_id: str | None = None,
) -> ExecutionWorkflow:
    """Return a workflow, recovering it from a broker event if needed."""
    normalized_symbol = str(symbol).strip().upper()
    with _REGISTRY_LOCK:
        existing = get_workflow(workflow_id)
        if existing is not None:
            if existing.symbol.strip().upper() != normalized_symbol:
                raise ValueError(
                    f"Workflow {workflow_id} belongs to {existing.symbol}, "
                    f"not {normalized_symbol}"
                )
            return existing

        recovered = ExecutionWorkflow(
            workflow_id=workflow_id,
            symbol=normalized_symbol,
        )
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
    """Resolve a workflow only when all supplied references agree."""
    resolved_workflows: list[ExecutionWorkflow] = []
    if workflow_id:
        workflow = get_workflow(workflow_id)
        if workflow is None:
            return None
        resolved_workflows.append(workflow)
    if client_order_id:
        workflow = get_workflow_by_client_order_id(client_order_id)
        if workflow is None:
            return None
        resolved_workflows.append(workflow)
    if broker_order_id:
        durable_owner_ids = get_execution_store().find_workflow_ids_by_broker_order_id(
            broker_order_id
        )
        if len(durable_owner_ids) > 1:
            return None
        workflow = (
            get_workflow(next(iter(durable_owner_ids)))
            if len(durable_owner_ids) == 1
            else None
        )
        if durable_owner_ids and workflow is None:
            return None
        if not durable_owner_ids:
            # A crash can persist the fill transition before its idempotent
            # order-reference write.  Accept that broker id only when the
            # already-resolved exact workflow contains durable transition
            # evidence for it; the fill handler will repair the missing ref.
            candidate_ids = {
                candidate.workflow_id for candidate in resolved_workflows
            }
            if len(candidate_ids) != 1:
                return None
            candidate = resolved_workflows[0]
            if not any(
                str(transition.details.get("broker_order_id", "") or "")
                == broker_order_id
                for transition in candidate.transitions
            ):
                return None
        else:
            assert workflow is not None
            resolved_workflows.append(workflow)
    if resolved_workflows:
        if len({workflow.workflow_id for workflow in resolved_workflows}) != 1:
            return None
        resolved = resolved_workflows[0]
        if resolved.symbol.strip().upper() != symbol.strip().upper():
            return None
        return resolved
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
    base, separator, attempt_suffix = value.rpartition("-sl-")
    if (
        separator
        and base
        and attempt_suffix
        and len(attempt_suffix) <= 8
        and attempt_suffix.isalnum()
    ):
        return base
    for suffix in ("-exit", "-sl", "-stop"):
        if value.endswith(suffix):
            return value[: -len(suffix)]
    return value


def build_exit_client_order_id(workflow_id: str) -> str:
    """Return the market-exit client order id derived from the workflow id."""
    return f"{workflow_id}-exit"


def build_stop_client_order_id(workflow_id: str, attempt_suffix: str = "") -> str:
    """Return the protective-stop client order id derived from the workflow id."""
    base = f"{workflow_id}-sl"
    normalized_suffix = str(attempt_suffix or "").strip().replace("-", "")[:8]
    return f"{base}-{normalized_suffix}" if normalized_suffix else base


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


def _load_workflow_into_cache(*, workflow_id: str) -> ExecutionWorkflow | None:
    payload = get_execution_store().load_workflow(workflow_id)
    return _cache_payload(payload)


def _cache_payload(payload: dict[str, Any] | None) -> ExecutionWorkflow | None:
    if payload is None:
        return None
    workflow_id = str(payload["workflow_id"])
    loaded = _workflow_from_payload(payload)
    with _REGISTRY_LOCK:
        cached = _WORKFLOW_REGISTRY.get(workflow_id)
        if cached is None:
            _WORKFLOW_REGISTRY[loaded.workflow_id] = loaded
            return loaded

        # Preserve canonical object identity while merging durable changes
        # written by another store/process.  Transition timestamps are unique
        # event identities; retaining cached-only events also avoids erasing an
        # in-process transition during its snapshot/append commit boundary.
        merged = {
            (item.timestamp_utc, item.event, item.to_state): item
            for item in cached.transitions
        }
        for item in loaded.transitions:
            merged[(item.timestamp_utc, item.event, item.to_state)] = item
        cached.transitions = sorted(
            merged.values(),
            key=lambda item: item.timestamp_utc,
        )
        if loaded.updated_at_utc >= cached.updated_at_utc:
            cached.symbol = loaded.symbol
            cached.state = loaded.state
            cached.broker_order_id = loaded.broker_order_id
            cached.entry_plan = loaded.entry_plan
            cached.created_at_utc = loaded.created_at_utc
            cached.updated_at_utc = loaded.updated_at_utc
        return cached


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
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
