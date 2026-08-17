"""High-level order manager for entries, exits, fills, and protection.

This service is the authoritative orchestration layer above broker primitives.
It keeps state-machine transitions, notifications, and broker operations aligned
under a single workflow id.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import threading
import time
from typing import Any, Optional

from core.execution_store import (
    ConcurrentWorkflowTransitionError,
    get_execution_store,
)
from core.execution_workflow import (
    ClosedSellCheckpoint,
    EntryExecutionPlan,
    WorkflowState,
    build_exit_client_order_id,
    create_entry_workflow,
    get_active_workflow_for_symbol,
    get_or_create_exit_workflow,
    get_or_recover_workflow,
    get_workflow,
    normalize_workflow_id,
    recover_active_position_workflow,
    resolve_workflow,
)
from core.notifier import notify_buy_filled, notify_entry_submitted, notify_sell_filled
from core.order_execution import (
    OrderResult,
    ProtectiveStopResult,
    _get_trading_client,
    _is_paper_mode,
    _sample_stable_symbol_state,
    _wait_for_terminal_buy_order_chain,
    cancel_open_orders_verified,
    close_position,
    ensure_protective_stop,
    get_open_positions,
    reconcile_symbol_after_exit_failure,
    require_paper_mode,
    submit_bracket_buy,
)


_FILL_HANDLING_LOCK = threading.RLock()
_SUBMISSION_WORKING_STATUSES = {
    "accepted",
    "accepted_for_bidding",
    "new",
    "partially_filled",
    "pending_new",
}
_SUBMISSION_ZERO_FILL_TERMINAL_STATUSES = {
    "calculated",
    "canceled",
    "done_for_day",
    "expired",
    "rejected",
    "stopped",
    "suspended",
}
_CLOSED_REPLAY_ORDER_STATUSES = {
    *_SUBMISSION_ZERO_FILL_TERMINAL_STATUSES,
    "filled",
    "replaced",
}


@dataclass(frozen=True)
class EntrySubmissionOutcome:
    """Outcome of an entry workflow submission attempt."""

    symbol: str
    workflow_id: str
    success: bool
    dry_run: bool
    order_id: str = ""
    error: str = ""


class OrderManager:
    """Own the end-to-end execution lifecycle for orders."""

    def __init__(self, *, paper: Optional[bool] = None) -> None:
        resolved_paper = _is_paper_mode() if paper is None else paper
        require_paper_mode(resolved_paper)
        self._paper = True

    def submit_entry(
        self,
        plan: EntryExecutionPlan,
        *,
        signal_payload: Optional[dict[str, Any]] = None,
        dry_run: bool = False,
    ) -> EntrySubmissionOutcome:
        """Create a workflow and submit the entry order if not dry-running."""
        pending = get_execution_store().load_pending_submission_intents(
            symbol=plan.symbol.strip().upper()
        )
        if pending:
            raise RuntimeError(
                f"Cannot submit {plan.symbol}: an unresolved submission intent exists"
            )
        workflow = create_entry_workflow(plan, signal_payload=signal_payload)

        if dry_run:
            workflow.mark_dry_run_skipped()
            return EntrySubmissionOutcome(
                symbol=plan.symbol,
                workflow_id=workflow.workflow_id,
                success=True,
                dry_run=True,
            )

        with _FILL_HANDLING_LOCK:
            workflow.mark_order_submission_intent(
                client_order_id=workflow.workflow_id,
                qty=plan.qty,
                limit_price=plan.entry_price,
            )
            result = submit_bracket_buy(
                symbol=plan.symbol,
                qty=plan.qty,
                stop_loss_pct=plan.stop_loss_pct,
                limit_price=plan.entry_price,
                client_order_id=workflow.workflow_id,
            )
            if result.success:
                # Persist the accepted broker id before a stream callback can
                # resolve and process its fill under this same lock.
                workflow.mark_order_submitted(broker_order_id=result.order_id)
            elif not result.outcome_uncertain:
                workflow.mark_order_submit_failed(
                    error=result.error or "unknown order submission error"
                )
                workflow.mark_submission_intent_resolved(
                    role="entry",
                    client_order_id=workflow.workflow_id,
                    outcome="definitive_failure",
                )

        if result.success:
            notification_sent = notify_entry_submitted(
                symbol=plan.symbol,
                qty=plan.qty,
                entry_price=plan.entry_price,
                stop_price=plan.stop_price,
                position_value=plan.position_value,
                risk_amount=plan.risk_amount,
                price_source=plan.price_source,
                order_id=result.order_id,
                workflow_id=workflow.workflow_id,
                stop_loss_pct=plan.stop_loss_pct,
                paper=self._paper,
            )
            with _FILL_HANDLING_LOCK:
                workflow.mark_entry_notification(sent=notification_sent)
            return EntrySubmissionOutcome(
                symbol=plan.symbol,
                workflow_id=workflow.workflow_id,
                success=True,
                dry_run=False,
                order_id=result.order_id,
            )

        return EntrySubmissionOutcome(
            symbol=plan.symbol,
            workflow_id=workflow.workflow_id,
            success=False,
            dry_run=False,
            error=result.error,
        )

    def submit_exit(self, symbol: str, *, exit_reason: str) -> OrderResult:
        """Cancel open orders and submit a market exit under the active workflow."""
        with _FILL_HANDLING_LOCK:
            return self._submit_exit_locked(symbol, exit_reason=exit_reason)

    def _submit_exit_locked(self, symbol: str, *, exit_reason: str) -> OrderResult:
        """Submit an exit while serialized against in-process fill reconciliation."""
        normalized_symbol = symbol.strip().upper()
        pending = get_execution_store().load_pending_submission_intents(
            symbol=normalized_symbol
        )
        if pending:
            pending_client_order_id = str(
                pending[-1].get("details", {}).get("client_order_id", "") or ""
            )
            return OrderResult(
                success=False,
                order_id="",
                symbol=normalized_symbol,
                side="sell",
                qty=0.0,
                error=(
                    f"Cannot submit {normalized_symbol} exit: an unresolved "
                    "submission intent exists"
                ),
                client_order_id=pending_client_order_id,
                outcome_uncertain=True,
            )
        workflow = get_or_create_exit_workflow(symbol, exit_reason=exit_reason)
        exit_client_order_id = build_exit_client_order_id(workflow.workflow_id)
        workflow.mark_exit_submission_intent(
            exit_reason=exit_reason,
            client_order_id=exit_client_order_id,
        )
        try:
            cancel_open_orders_verified(symbol)
        except Exception as exc:  # noqa: BLE001
            error = f"could not safely clear open orders: {exc}"
            workflow.mark_submission_intent_resolved(
                role="exit",
                client_order_id=exit_client_order_id,
                outcome="pre_submit_cancel_failed",
            )
            error = self._recover_exit_safety(
                symbol,
                workflow,
                error=error,
            )
            workflow.mark_exit_order_submit_failed(
                exit_reason=exit_reason,
                error=error,
            )
            return OrderResult(
                success=False,
                order_id="",
                symbol=symbol,
                side="sell",
                qty=0,
                error=error,
                client_order_id=exit_client_order_id,
            )
        result = close_position(
            symbol,
            client_order_id=exit_client_order_id,
        )
        if not result.client_order_id:
            result.client_order_id = exit_client_order_id
        if result.success and result.qty > 0:
            workflow.mark_exit_order_submitted(
                exit_reason=exit_reason,
                broker_order_id=result.order_id,
            )
        elif not result.success:
            if result.outcome_uncertain:
                unresolved_error = result.error or "unknown exit submission error"
                result.error = (
                    f"{unresolved_error}; exact broker outcome remains unresolved; "
                    "safety remains unproven"
                )
                return result
            workflow.mark_submission_intent_resolved(
                role="exit",
                client_order_id=exit_client_order_id,
                outcome="definitive_failure",
            )
            result.error = self._recover_exit_safety(
                symbol,
                workflow,
                error=result.error or "unknown exit submission error",
            )
            workflow.mark_exit_order_submit_failed(
                exit_reason=exit_reason,
                error=result.error or "unknown exit submission error",
            )
        else:
            try:
                safety = self._restore_owned_exit_safety(
                    normalized_symbol,
                    workflow,
                    action="exit_no_position",
                )
            except RuntimeError as exc:
                result.success = False
                result.error = str(exc)
                result.outcome_uncertain = True
                return result
            if safety.action != "flat":
                result.success = False
                result.error = (
                    "broker reported no open position to exit; durable position "
                    "remains stop-protected"
                )
            workflow.mark_exit_order_submit_failed(
                exit_reason=exit_reason,
                error="broker reported no open position to exit",
            )
            workflow.mark_submission_intent_resolved(
                role="exit",
                client_order_id=exit_client_order_id,
                outcome="already_flat",
            )
        return result

    def handle_fill(
        self,
        *,
        symbol: str,
        broker_order_id: str,
        client_order_id: str,
        side: str,
        filled_qty: float,
        fill_price: float,
        order_type: str,
        replaces: str = "",
        replaced_by: str = "",
    ) -> None:
        """Serialize and durably converge a broker final-fill update."""
        with _FILL_HANDLING_LOCK:
            self._handle_fill_locked(
                symbol=symbol,
                broker_order_id=broker_order_id,
                client_order_id=client_order_id,
                side=side,
                filled_qty=filled_qty,
                fill_price=fill_price,
                order_type=order_type,
                replaces=replaces,
                replaced_by=replaced_by,
            )

    def _handle_fill_locked(
        self,
        *,
        symbol: str,
        broker_order_id: str,
        client_order_id: str,
        side: str,
        filled_qty: float,
        fill_price: float,
        order_type: str,
        replaces: str = "",
        replaced_by: str = "",
    ) -> None:
        """Handle one final fill while the process-wide saga lock is held."""
        self._require_event_references(
            broker_order_id=broker_order_id,
            client_order_id=client_order_id,
        )
        if replaces or replaced_by:
            self._record_trusted_order_ancestry(
                symbol=symbol,
                side=side,
                broker_order_id=broker_order_id,
                client_order_id=client_order_id,
                replaces=replaces,
                replaced_by=replaced_by,
            )
        workflow = self._resolve_event_workflow(
            symbol=symbol,
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            side=side,
            filled_qty=filled_qty,
            fill_price=fill_price,
            order_type=order_type,
        )
        if side in {"buy", "sell"} and workflow is None:
            raise RuntimeError(
                f"Unable to resolve {symbol} {side} fill to one workflow"
            )

        workflow_id = workflow.workflow_id if workflow is not None else ""
        recorded_fill_index = self._find_recorded_fill_index(
            workflow,
            side=side,
            broker_order_id=broker_order_id,
            filled_qty=filled_qty,
            fill_price=fill_price,
        )

        if side == "buy":
            self._handle_buy_checkpoint_locked(
                symbol=symbol,
                broker_order_id=broker_order_id,
                workflow_id=workflow_id,
                workflow=workflow,
                filled_qty=filled_qty,
                fill_price=fill_price,
                partial=False,
            )
            return
        if side != "sell":
            return

        self._handle_sell_checkpoint_locked(
            symbol=symbol,
            broker_order_id=broker_order_id,
            client_order_id=client_order_id,
            workflow_id=workflow_id,
            workflow=workflow,
            filled_qty=filled_qty,
            fill_price=fill_price,
            order_type=order_type,
            partial=False,
            recorded_fill_index=recorded_fill_index,
        )

    @staticmethod
    def _require_event_references(
        *,
        broker_order_id: str,
        client_order_id: str,
    ) -> None:
        """Reject broker events that cannot be tied to any explicit order id."""
        if not str(broker_order_id or "").strip() and not str(
            client_order_id or ""
        ).strip():
            raise RuntimeError("Broker stream event is missing all order references")

    @staticmethod
    def _resolve_event_workflow(
        *,
        symbol: str,
        client_order_id: str,
        broker_order_id: str,
        side: str,
        filled_qty: float,
        fill_price: float,
        order_type: str,
    ) -> Any | None:
        """Strictly resolve, or atomically claim one exact pending submission."""
        workflow = resolve_workflow(
            symbol=symbol,
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
        )
        if workflow is not None:
            return workflow

        if not client_order_id:
            return None

        candidate = resolve_workflow(
            symbol=symbol,
            client_order_id=client_order_id,
        )
        if candidate is None or not broker_order_id:
            return None

        normalized_side = str(side).split(".")[-1].strip().lower()
        normalized_order_type = str(order_type).split(".")[-1].strip().lower()
        if normalized_side == "buy":
            plan = getattr(candidate, "entry_plan", None)
            if (
                client_order_id != candidate.workflow_id
                or plan is None
                or filled_qty <= 0
                or filled_qty > float(plan.qty) + 0.0001
                or fill_price <= 0
            ):
                return None
            order_role = "entry_order"
            intent_event = "entry_submission_intent"
        elif normalized_side == "sell":
            active = get_execution_store().load_active_position(symbol)
            if (
                client_order_id
                != build_exit_client_order_id(candidate.workflow_id)
                or normalized_order_type != "market"
                or str((active or {}).get("workflow_id", "") or "")
                != candidate.workflow_id
                or filled_qty <= 0
                or fill_price <= 0
            ):
                return None
            order_role = "exit_order"
            intent_event = "exit_submission_intent"
        else:
            return None

        try:
            candidate.claim_order_reference_from_submission_intent(
                broker_order_id=broker_order_id,
                client_order_id=client_order_id,
                order_role=order_role,
                intent_event=intent_event,
                side=normalized_side,
            )
        except ValueError:
            return None
        return resolve_workflow(
            symbol=symbol,
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
        )

    @staticmethod
    def _record_trusted_order_ancestry(
        *,
        symbol: str,
        side: str,
        broker_order_id: str,
        client_order_id: str,
        replaces: str,
        replaced_by: str,
    ) -> Any:
        """Persist new broker ids only when a replacement chain has one known owner."""
        store = get_execution_store()
        broker_ids = {
            str(order_id).strip()
            for order_id in (broker_order_id, replaces, replaced_by)
            if str(order_id or "").strip()
        }
        candidate_ids: set[str] = set()
        anchored = False

        if client_order_id:
            client_owner_ids = store.find_workflow_ids_by_client_order_id(
                client_order_id
            )
            if len(client_owner_ids) > 1:
                raise RuntimeError("Replacement client order reference is ambiguous")
            candidate_ids.update(client_owner_ids)
            normalized = normalize_workflow_id(client_order_id)
            normalized_workflow = get_workflow(normalized) if normalized else None
            if normalized_workflow is not None:
                candidate_ids.add(normalized_workflow.workflow_id)

        for order_id in broker_ids:
            owner_ids = store.find_workflow_ids_by_broker_order_id(order_id)
            if len(owner_ids) > 1:
                raise RuntimeError(
                    f"Replacement broker order reference {order_id!r} is ambiguous"
                )
            if owner_ids:
                anchored = True
                candidate_ids.update(owner_ids)

        if not anchored or len(candidate_ids) != 1:
            raise RuntimeError(
                f"Unable to resolve trusted {symbol} replacement ancestry"
            )
        workflow = get_workflow(next(iter(candidate_ids)))
        if workflow is None or workflow.symbol.strip().upper() != symbol.strip().upper():
            raise RuntimeError(
                f"Replacement ancestry does not belong to {symbol}"
            )

        normalized_side = str(side).split(".")[-1].strip().lower()
        if normalized_side == "buy":
            order_role = "entry_order"
        elif str(client_order_id).endswith("-exit"):
            order_role = "exit_order"
        elif normalize_workflow_id(client_order_id) != str(client_order_id):
            order_role = "protective_stop"
        else:
            order_role = "sell_order"

        for order_id in broker_ids:
            workflow.repair_order_reference(
                broker_order_id=order_id,
                client_order_id=client_order_id,
                order_role=order_role,
            )
        return workflow

    @staticmethod
    def _resolve_entry_price(symbol: str, workflow: Any | None) -> float | None:
        """Resolve cost basis before a sell transition clears active ownership."""
        entry_plan = getattr(workflow, "entry_plan", None) if workflow is not None else None
        if entry_plan is not None and float(entry_plan.entry_price) > 0:
            return float(entry_plan.entry_price)

        active_position = get_execution_store().load_active_position(symbol)
        if active_position is not None and float(active_position["entry_price"]) > 0:
            return float(active_position["entry_price"])
        return None

    @staticmethod
    def _find_recorded_fill_index(
        workflow: Any | None,
        *,
        side: str,
        broker_order_id: str,
        filled_qty: float,
        fill_price: float,
    ) -> int | None:
        """Return the exact cumulative fill checkpoint index when already recorded."""
        if workflow is None or not broker_order_id:
            return None

        expected_event = "buy_fill_received" if side == "buy" else "sell_fill_received"
        for index in range(len(workflow.transitions) - 1, -1, -1):
            transition = workflow.transitions[index]
            if transition.event != expected_event:
                continue
            details = transition.details
            if (
                str(details.get("broker_order_id", "")) == broker_order_id
                and float(details.get("qty", -1.0)) == filled_qty
                and float(details.get("fill_price", -1.0)) == fill_price
            ):
                return index
        return None

    @classmethod
    def _has_recorded_fill(
        cls,
        workflow: Any | None,
        *,
        side: str,
        broker_order_id: str,
        filled_qty: float,
        fill_price: float,
    ) -> bool:
        """Return True when this cumulative broker fill was already processed."""
        return cls._find_recorded_fill_index(
            workflow,
            side=side,
            broker_order_id=broker_order_id,
            filled_qty=filled_qty,
            fill_price=fill_price,
        ) is not None

    def handle_partial_fill(
        self,
        *,
        symbol: str,
        broker_order_id: str,
        client_order_id: str,
        side: str,
        filled_qty: float,
        fill_price: float,
        order_type: str,
        replaces: str = "",
        replaced_by: str = "",
    ) -> None:
        """Serialize a partial fill against final-fill and replay handling."""
        with _FILL_HANDLING_LOCK:
            self._handle_partial_fill_locked(
                symbol=symbol,
                broker_order_id=broker_order_id,
                client_order_id=client_order_id,
                side=side,
                filled_qty=filled_qty,
                fill_price=fill_price,
                order_type=order_type,
                replaces=replaces,
                replaced_by=replaced_by,
            )

    def handle_order_failure(
        self,
        *,
        symbol: str,
        broker_order_id: str,
        client_order_id: str,
        side: str,
        order_type: str,
        status: str,
        filled_qty: float = 0.0,
        fill_price: float = 0.0,
        replaces: str = "",
        replaced_by: str = "",
    ) -> None:
        """Recover protection after a sell order terminates without a full fill."""
        with _FILL_HANDLING_LOCK:
            self._handle_order_failure_locked(
                symbol=symbol,
                broker_order_id=broker_order_id,
                client_order_id=client_order_id,
                side=side,
                order_type=order_type,
                status=status,
                filled_qty=filled_qty,
                fill_price=fill_price,
                replaces=replaces,
                replaced_by=replaced_by,
            )

    def _handle_order_failure_locked(
        self,
        *,
        symbol: str,
        broker_order_id: str,
        client_order_id: str,
        side: str,
        order_type: str,
        status: str,
        filled_qty: float = 0.0,
        fill_price: float = 0.0,
        replaces: str = "",
        replaced_by: str = "",
    ) -> None:
        """Strictly converge terminal and structurally changed broker orders."""
        self._require_event_references(
            broker_order_id=broker_order_id,
            client_order_id=client_order_id,
        )
        normalized_side = str(side).split(".")[-1].strip().lower()
        normalized_status = str(status).split(".")[-1].strip().lower() or "failed"
        if normalized_side not in {"buy", "sell"}:
            return

        structural_change = normalized_status in {"replaced", "restated"}
        if normalized_side == "buy" and filled_qty <= 0 and not (
            structural_change or replaces or replaced_by
        ):
            return

        if structural_change or replaces or replaced_by:
            self._record_trusted_order_ancestry(
                symbol=symbol,
                side=normalized_side,
                broker_order_id=broker_order_id,
                client_order_id=client_order_id,
                replaces=replaces,
                replaced_by=replaced_by,
            )
        workflow = self._resolve_event_workflow(
            symbol=symbol,
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            side=normalized_side,
            filled_qty=filled_qty,
            fill_price=fill_price,
            order_type=order_type,
        )
        if workflow is None:
            raise RuntimeError(
                f"Unable to resolve {symbol} {normalized_side} order event to one workflow"
            )

        if normalized_side == "buy":
            if filled_qty <= 0:
                return
            resolved_fill_price = float(fill_price or 0.0)
            if resolved_fill_price <= 0:
                raise RuntimeError(
                    f"Safety remains unproven for {symbol}: positive terminal BUY "
                    "has no fill price"
                )
            # A terminal broker event with positive filled_qty is causal fill
            # evidence.  The positions REST endpoint can lag that event, so do
            # not let an eager flat snapshot erase it.  Persist ownership first;
            # ensure_protective_stop owns the bounded position-sync proof.
            self._handle_buy_checkpoint_locked(
                symbol=symbol,
                broker_order_id=broker_order_id,
                workflow_id=workflow.workflow_id,
                workflow=workflow,
                filled_qty=filled_qty,
                fill_price=resolved_fill_price,
                partial=False,
            )
            return

        if filled_qty > 0:
            resolved_fill_price = float(fill_price or 0.0)
            if resolved_fill_price <= 0:
                raise RuntimeError(
                    f"Safety remains unproven for {symbol}: positive terminal SELL "
                    "has no fill price"
                )
            recorded_fill_index = self._find_recorded_fill_index(
                workflow,
                side="sell",
                broker_order_id=broker_order_id,
                filled_qty=filled_qty,
                fill_price=resolved_fill_price,
            )
            self._handle_sell_checkpoint_locked(
                symbol=symbol,
                broker_order_id=broker_order_id,
                client_order_id=client_order_id,
                workflow_id=workflow.workflow_id,
                workflow=workflow,
                filled_qty=filled_qty,
                fill_price=resolved_fill_price,
                order_type=order_type,
                partial=True,
                recorded_fill_index=recorded_fill_index,
            )
            return

        if structural_change or replaces or replaced_by:
            return

        active = get_execution_store().load_active_position(symbol)
        if active is not None and str(active.get("workflow_id", "") or "") == (
            workflow.workflow_id
        ):
            self._restore_owned_exit_safety(
                symbol,
                workflow,
                action=f"sell_{normalized_status}",
            )
            return

        position, open_orders = _sample_stable_symbol_state(symbol)
        if position is None and open_orders:
            cancel_open_orders_verified(symbol)
            position, open_orders = _sample_stable_symbol_state(symbol)
        if position is None:
            if open_orders:
                raise RuntimeError(
                    f"Safety remains unproven for {symbol}: working orders remain while flat"
                )
            # Broker truth is flat. Clear only this failed order's workflow so
            # a stale event cannot erase a newer owner.
            workflow._clear_active_position()  # noqa: SLF001
            return
        if position.qty <= 0:
            raise RuntimeError(
                f"Safety remains unproven for {symbol}: unsupported short exposure"
            )

        current_workflow = self._repair_broker_position_owner(
            symbol=symbol,
            source_workflow=workflow,
            position=position,
        )
        cumulative = self._cumulative_workflow_buy_fill(current_workflow)
        protection = ensure_protective_stop(
            symbol=symbol,
            qty=position.qty,
            fill_price=position.avg_entry_price,
            workflow_id=current_workflow.workflow_id,
            entry_order_id=cumulative[3] or None,
            entry_order_ids=cumulative[2] or None,
            durable_sell_fill_qty=self._cumulative_workflow_sell_fill_qty(
                current_workflow
            ),
        )
        self._record_protective_stop(
            current_workflow,
            protection,
            action=f"sell_{normalized_status}_{protection.action}",
        )
        if protection.success:
            return

        if protection.action == "submission_unknown":
            raise RuntimeError(
                f"Safety remains unproven for {symbol}: "
                f"{protection.error or protection.action}"
            )

        normalized_order_type = (
            str(order_type).split(".")[-1].strip().lower() or "sell"
        )
        exit_result = self._submit_exit_locked(
            symbol,
            exit_reason=(
                f"{normalized_order_type} order {normalized_status}; "
                "residual position protection failed"
            ),
        )
        if not exit_result.success:
            self._prove_symbol_safe_or_raise(symbol, current_workflow)

    def _handle_partial_fill_locked(
        self,
        *,
        symbol: str,
        broker_order_id: str,
        client_order_id: str,
        side: str,
        filled_qty: float,
        fill_price: float,
        order_type: str,
        replaces: str = "",
        replaced_by: str = "",
    ) -> None:
        """Handle a partial fill while the process-wide saga lock is held."""
        self._require_event_references(
            broker_order_id=broker_order_id,
            client_order_id=client_order_id,
        )
        if side not in {"buy", "sell"}:
            return

        if replaces or replaced_by:
            self._record_trusted_order_ancestry(
                symbol=symbol,
                side=side,
                broker_order_id=broker_order_id,
                client_order_id=client_order_id,
                replaces=replaces,
                replaced_by=replaced_by,
            )
        workflow = self._resolve_event_workflow(
            symbol=symbol,
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            side=side,
            filled_qty=filled_qty,
            fill_price=fill_price,
            order_type=order_type,
        )
        if workflow is None:
            raise RuntimeError(
                f"Unable to resolve {symbol} {side} partial fill to one workflow"
            )
        workflow_id = workflow.workflow_id
        if side == "sell":
            self._handle_sell_checkpoint_locked(
                symbol=symbol,
                broker_order_id=broker_order_id,
                client_order_id=client_order_id,
                workflow_id=workflow_id,
                workflow=workflow,
                filled_qty=filled_qty,
                fill_price=fill_price,
                order_type=order_type,
                partial=True,
                recorded_fill_index=None,
            )
            return
        self._handle_buy_checkpoint_locked(
            symbol=symbol,
            broker_order_id=broker_order_id,
            workflow_id=workflow_id,
            workflow=workflow,
            filled_qty=filled_qty,
            fill_price=fill_price,
            partial=True,
        )

    def _handle_buy_checkpoint_locked(
        self,
        *,
        symbol: str,
        broker_order_id: str,
        workflow_id: str,
        workflow: Any | None,
        filled_qty: float,
        fill_price: float,
        partial: bool,
        broker_state_proven: bool = False,
        broker_position: Any | None = None,
    ) -> None:
        """Retry a full checkpoint after a cross-process durable CAS conflict."""
        for attempt in range(4):
            try:
                self._handle_buy_checkpoint_once_locked(
                    symbol=symbol,
                    broker_order_id=broker_order_id,
                    workflow_id=workflow_id,
                    workflow=workflow,
                    filled_qty=filled_qty,
                    fill_price=fill_price,
                    partial=partial,
                    broker_state_proven=broker_state_proven,
                    broker_position=broker_position,
                )
                return
            except ConcurrentWorkflowTransitionError:
                if attempt >= 3 or not workflow_id:
                    raise
                workflow = get_workflow(workflow_id)
                if workflow is None:
                    raise RuntimeError(
                        f"Workflow {workflow_id} disappeared during fill retry"
                    ) from None

    def _handle_buy_checkpoint_once_locked(
        self,
        *,
        symbol: str,
        broker_order_id: str,
        workflow_id: str,
        workflow: Any | None,
        filled_qty: float,
        fill_price: float,
        partial: bool,
        broker_state_proven: bool = False,
        broker_position: Any | None = None,
    ) -> None:
        """Persist monotonic cumulative fills and protect the current owner."""
        if workflow is None and workflow_id:
            workflow = get_or_recover_workflow(
                workflow_id,
                symbol=symbol,
                broker_order_id=broker_order_id,
            )
        if workflow is None:
            return

        active_before = get_execution_store().load_active_position(symbol)
        active_before_workflow_id = str(
            (active_before or {}).get("workflow_id", "") or ""
        )
        foreign_owner = bool(
            active_before_workflow_id
            and active_before_workflow_id != workflow.workflow_id
        )
        workflow_has_sell = any(
            item.event in {"sell_partial_fill_received", "sell_fill_received"}
            for item in workflow.transitions
        )

        checkpoint = self._latest_cumulative_buy_fill(workflow, broker_order_id)
        checkpoint_qty = self._transition_float(checkpoint, "qty")
        new_checkpoint = checkpoint is None or filled_qty > checkpoint_qty + 0.0001
        if new_checkpoint:
            workflow.mark_buy_fill(
                qty=filled_qty,
                fill_price=fill_price,
                broker_order_id=broker_order_id,
                # Persist causal fill evidence and its exact order identity
                # before relying on a lag-prone positions snapshot.
                restore_active=False,
            )

        pre_fence_buy_qty = self._cumulative_workflow_buy_fill(workflow)[0]
        if partial and foreign_owner and workflow_has_sell and broker_order_id:
            _wait_for_terminal_buy_order_chain(
                symbol,
                {broker_order_id},
                workflow_id=workflow.workflow_id,
                persist_fill_checkpoints=True,
            )
            refreshed_source_workflow = get_workflow(workflow.workflow_id)
            if refreshed_source_workflow is None:
                raise RuntimeError(
                    f"Safety remains unproven for {symbol}: source workflow "
                    "disappeared after the late BUY fence"
                )
            workflow = refreshed_source_workflow
            if (
                self._cumulative_workflow_buy_fill(workflow)[0]
                > pre_fence_buy_qty + 0.0001
            ):
                new_checkpoint = True

        cumulative = self._cumulative_workflow_buy_fill(workflow)
        source_sell_qty = self._cumulative_workflow_sell_fill_qty(workflow)
        source_net_qty = max(0.0, cumulative[0] - source_sell_qty)
        required_active_floor = 0.0
        if workflow_has_sell and source_net_qty > 0:
            if new_checkpoint:
                workflow.mark_late_buy_exposure_recovered(
                    qty=source_net_qty,
                    fill_price=fill_price,
                    broker_order_id=broker_order_id,
                )
            if foreign_owner:
                active_workflow = get_or_recover_workflow(
                    active_before_workflow_id,
                    symbol=symbol,
                )
                try:
                    active_before_qty = float(
                        (active_before or {}).get("qty", 0.0) or 0.0
                    )
                    active_before_price = float(
                        (active_before or {}).get("entry_price", 0.0) or 0.0
                    )
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        f"Safety remains unproven for {symbol}: durable ownership "
                        "values are invalid"
                    ) from exc
                if active_before_qty <= 0 or active_before_price <= 0:
                    raise RuntimeError(
                        f"Safety remains unproven for {symbol}: durable ownership "
                        "values are invalid"
                    )
                applied_source_net, persisted_floor = (
                    self._latest_foreign_late_buy_floor(
                        active_workflow,
                        source_workflow_id=workflow.workflow_id,
                    )
                )
                required_active_floor = max(active_before_qty, persisted_floor)
                if source_net_qty > applied_source_net + 0.0001:
                    required_active_floor += source_net_qty - applied_source_net
                    active_workflow.transition(
                        WorkflowState.RECOVERED_FROM_BROKER,
                        event="late_buy_exposure_recovered",
                        details={
                            "qty": required_active_floor,
                            "fill_price": active_before_price,
                            "broker_order_id": "",
                            "source_workflow_id": workflow.workflow_id,
                            "source_broker_order_id": broker_order_id,
                            "source_net_qty": source_net_qty,
                        },
                    )
                active_workflow.repair_buy_fill_storage(
                    qty=required_active_floor,
                    fill_price=active_before_price,
                    broker_order_id="",
                    restore_active=True,
                    preserve_higher_qty=True,
                )
                durable_active = get_execution_store().load_active_position(symbol)
                if (
                    durable_active is None
                    or str(durable_active.get("workflow_id", "") or "")
                    != active_workflow.workflow_id
                    or float(durable_active.get("qty", 0.0) or 0.0)
                    + 0.0001
                    < required_active_floor
                ):
                    raise RuntimeError(
                        f"Safety remains unproven for {symbol}: durable ownership "
                        "changed while recording late BUY exposure"
                    )
            else:
                try:
                    active_before_qty = float(
                        (active_before or {}).get("qty", 0.0) or 0.0
                    )
                except (TypeError, ValueError):
                    active_before_qty = 0.0
                required_active_floor = max(active_before_qty, source_net_qty)
                workflow.repair_buy_fill_storage(
                    qty=required_active_floor,
                    fill_price=fill_price,
                    broker_order_id=broker_order_id,
                    restore_active=True,
                    preserve_higher_qty=True,
                )

        if (
            workflow_has_sell
            and (active_before is None or foreign_owner)
            and not broker_state_proven
        ):
            broker_position, _open_orders = _sample_stable_symbol_state(symbol)
            broker_state_proven = True
        if broker_state_proven and broker_position is None:
            if not (workflow_has_sell and source_net_qty > 0):
                workflow._clear_active_position()  # noqa: SLF001
                return
        if broker_position is not None and (
            float(broker_position.qty) <= 0
            or float(broker_position.avg_entry_price) <= 0
        ):
            raise RuntimeError(f"Broker position for {symbol} is invalid")
        if broker_position is not None and fill_price <= 0:
            fill_price = float(broker_position.avg_entry_price)
        if cumulative[0] > 0:
            workflow.repair_buy_fill_storage(
                qty=cumulative[0],
                fill_price=cumulative[1],
                broker_order_id=broker_order_id,
                restore_active=(
                    not workflow_has_sell
                    and not foreign_owner
                    and not broker_state_proven
                ),
            )
        elif workflow_has_sell and broker_order_id:
            workflow.repair_buy_fill_storage(
                qty=max(0.0, filled_qty),
                fill_price=max(0.0, fill_price),
                broker_order_id=broker_order_id,
                restore_active=False,
            )

        if (
            broker_position is not None
            and broker_state_proven
            and float(broker_position.qty) + 0.0001 >= required_active_floor
        ):
            self._repair_broker_position_owner(
                symbol=symbol,
                source_workflow=workflow,
                position=broker_position,
            )
        active = get_execution_store().load_active_position(symbol)
        if active is None:
            return
        active_workflow_id = str(active.get("workflow_id", "") or "")
        if not active_workflow_id:
            return
        active_workflow = (
            workflow
            if active_workflow_id == workflow.workflow_id
            else get_or_recover_workflow(active_workflow_id, symbol=symbol)
        )
        try:
            active_qty = float(active.get("qty", 0.0) or 0.0)
            active_entry_price = float(active.get("entry_price", 0.0) or 0.0)
        except (TypeError, ValueError):
            return
        if active_qty <= 0 or active_entry_price <= 0:
            return

        active_cumulative = self._cumulative_workflow_buy_fill(active_workflow)
        active_has_sell = any(
            item.event in {"sell_fill_received", "sell_partial_fill_received"}
            for item in active_workflow.transitions
        )
        if not active_has_sell and active_cumulative[0] > active_qty + 0.0001:
            active_qty = active_cumulative[0]
            active_entry_price = active_cumulative[1]
            active_workflow.repair_buy_fill_storage(
                qty=active_qty,
                fill_price=active_entry_price,
                broker_order_id="",
                restore_active=True,
            )
        active_entry_order_ids = active_cumulative[2]
        active_entry_order_id = active_cumulative[3]
        protection_events = [
            item
            for item in active_workflow.transitions
            if item.event == "protective_stop_reconciled"
        ]
        latest_protection = protection_events[-1] if protection_events else None
        protection = ensure_protective_stop(
            symbol=symbol,
            qty=active_qty,
            fill_price=active_entry_price,
            workflow_id=active_workflow.workflow_id,
            entry_order_id=active_entry_order_id or None,
            entry_order_ids=active_entry_order_ids or None,
            durable_sell_fill_qty=self._cumulative_workflow_sell_fill_qty(
                active_workflow
            ),
        )
        if self._same_persisted_protection(latest_protection, protection):
            active_workflow.repair_protective_stop_reference(
                stop_order_id=protection.order_id,
                stop_client_order_id=str(
                    getattr(protection, "client_order_id", "") or ""
                ),
            )
        else:
            self._record_protective_stop(
                active_workflow,
                protection,
                action=(f"partial_{protection.action}" if partial else protection.action),
            )

        if not protection.success:
            if protection.action in {
                "position_not_visible",
                "position_sync_pending",
                "submission_unknown",
            }:
                raise RuntimeError(
                    f"Safety remains unproven for {symbol}: "
                    f"{protection.error or protection.action}"
                )
            exit_result = self._submit_exit_locked(
                symbol,
                exit_reason="protective stop reconciliation failed",
            )
            if not exit_result.success:
                self._prove_symbol_safe_or_raise(symbol, active_workflow)
            return

        if (
            not partial
            and not workflow_has_sell
            and active_workflow.workflow_id == workflow.workflow_id
            and workflow.claim_buy_fill_notification()
        ):
            sent = notify_buy_filled(
                symbol=symbol,
                qty=active_qty,
                fill_price=active_entry_price,
                stop_price=protection.stop_price,
                workflow_id=active_workflow.workflow_id,
                paper=self._paper,
            )
            workflow.mark_buy_fill_notification(sent=sent)
        if not partial:
            workflow.mark_submission_intent_resolved(
                role="entry",
                client_order_id=workflow.workflow_id,
                outcome="final_fill_reconciled",
                broker_order_id=broker_order_id,
            )

    @staticmethod
    def _repair_broker_position_owner(
        *,
        symbol: str,
        source_workflow: Any,
        position: Any,
    ) -> Any:
        """Converge durable ownership to strict broker quantity and cost basis."""
        active = get_execution_store().load_active_position(symbol)
        current_workflow_id = str((active or {}).get("workflow_id", "") or "")
        current_workflow = (
            get_or_recover_workflow(current_workflow_id, symbol=symbol)
            if current_workflow_id
            and current_workflow_id != source_workflow.workflow_id
            else source_workflow
        )
        current_workflow.repair_buy_fill_storage(
            qty=position.qty,
            fill_price=position.avg_entry_price,
            broker_order_id="",
            restore_active=True,
            preserve_higher_qty=False,
        )
        return current_workflow

    def _handle_sell_checkpoint_locked(
        self,
        *,
        symbol: str,
        broker_order_id: str,
        client_order_id: str,
        workflow_id: str,
        workflow: Any | None,
        filled_qty: float,
        fill_price: float,
        order_type: str,
        partial: bool,
        recorded_fill_index: int | None,
    ) -> None:
        """Persist a sell checkpoint only after broker residual exposure converges."""
        if workflow is None:
            return

        entry_price = self._resolve_entry_price(symbol, workflow)
        active_before = get_execution_store().load_active_position(symbol)
        active_before_workflow_id = str(
            (active_before or {}).get("workflow_id", "") or ""
        )
        owns_position = active_before_workflow_id == workflow.workflow_id
        previous_filled_qty = self._latest_cumulative_sell_fill_qty(
            workflow,
            broker_order_id,
        )
        incremental_filled_qty = max(0.0, filled_qty - previous_filled_qty)
        if recorded_fill_index is not None and active_before is not None:
            recorded_at = workflow.transitions[recorded_fill_index].timestamp_utc
            active_updated_at = str(active_before.get("updated_at_utc", "") or "")
            if active_updated_at and active_updated_at < recorded_at:
                incremental_filled_qty = filled_qty
        expected_remaining_qty: float | None = None
        if owns_position:
            try:
                owned_qty = float((active_before or {}).get("qty", 0.0) or 0.0)
            except (TypeError, ValueError):
                owned_qty = 0.0
            if owned_qty > 0:
                expected_remaining_qty = max(0.0, owned_qty - incremental_filled_qty)

        position, open_orders = self._wait_for_sell_position_sync(
            symbol,
            expected_remaining_qty=expected_remaining_qty,
        )
        if position is None and open_orders:
            cancel_open_orders_verified(symbol)
            position, open_orders = _sample_stable_symbol_state(symbol)
        if position is None and open_orders:
            raise RuntimeError(
                f"Cannot finalize {symbol} sell while symbol orders remain open"
            )
        if position is not None and position.qty <= 0:
            raise RuntimeError(
                f"Cannot finalize {symbol} sell with unsupported short exposure"
            )

        if partial:
            if filled_qty > previous_filled_qty + 0.0001:
                workflow.mark_sell_partial_fill(
                    qty=filled_qty,
                    fill_price=fill_price,
                    broker_order_id=broker_order_id,
                    client_order_id=client_order_id,
                )
        else:
            exit_reason = self._infer_exit_reason(
                order_type=order_type,
                client_order_id=client_order_id,
            )
            if recorded_fill_index is None:
                workflow.mark_sell_fill(
                    qty=filled_qty,
                    fill_price=fill_price,
                    exit_reason=exit_reason,
                    broker_order_id=broker_order_id,
                    client_order_id=client_order_id,
                    clear_active=False,
                )
            workflow.repair_sell_fill_storage(
                broker_order_id=broker_order_id,
                client_order_id=client_order_id,
                clear_active=False,
            )
            if position is None and not open_orders and active_before:
                store = get_execution_store()
                try:
                    observed_qty = float(active_before.get("qty", 0.0) or 0.0)
                    observed_entry_price = float(
                        active_before.get("entry_price", 0.0) or 0.0
                    )
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        f"Cannot finalize {symbol} sell with invalid durable ownership"
                    ) from exc
                flat_owner_cleared = store.clear_active_position_if_unchanged(
                    symbol=symbol,
                    workflow_id=active_before_workflow_id,
                    qty=observed_qty,
                    entry_price=observed_entry_price,
                    opened_at_utc=str(active_before.get("opened_at_utc", "") or ""),
                    updated_at_utc=str(active_before.get("updated_at_utc", "") or ""),
                )
                if (
                    not flat_owner_cleared
                    and store.load_active_position(symbol) is not None
                ):
                    raise RuntimeError(
                        f"Cannot finalize {symbol} sell because durable ownership "
                        "changed during the broker-flat proof"
                    )

        if position is not None:
            current_workflow = self._repair_broker_position_owner(
                symbol=symbol,
                source_workflow=workflow,
                position=position,
            )
            cumulative = self._cumulative_workflow_buy_fill(current_workflow)
            protection = ensure_protective_stop(
                symbol=symbol,
                qty=position.qty,
                fill_price=position.avg_entry_price,
                workflow_id=current_workflow.workflow_id,
                entry_order_id=cumulative[3] or None,
                entry_order_ids=cumulative[2] or None,
                durable_sell_fill_qty=self._cumulative_workflow_sell_fill_qty(
                    current_workflow
                ),
            )
            self._record_protective_stop(
                current_workflow,
                protection,
                action=(
                    f"sell_partial_{protection.action}"
                    if partial
                    else f"sell_residual_{protection.action}"
                ),
            )
            if not protection.success:
                if protection.action == "submission_unknown":
                    raise RuntimeError(
                        f"Safety remains unproven for {symbol}: "
                        f"{protection.error or protection.action}"
                    )
                exit_result = self._submit_exit_locked(
                    symbol,
                    exit_reason="residual position protection failed",
                )
                if not exit_result.success:
                    self._prove_symbol_safe_or_raise(symbol, current_workflow)

        if not partial and workflow.claim_sell_notification():
            exit_reason = self._infer_exit_reason(
                order_type=order_type,
                client_order_id=client_order_id,
            )
            sent = notify_sell_filled(
                symbol=symbol,
                qty=filled_qty,
                fill_price=fill_price,
                entry_price=entry_price,
                exit_reason=exit_reason,
                workflow_id=workflow_id or workflow.workflow_id,
                paper=self._paper,
            )
            workflow.mark_sell_notification(sent=sent)
        if not partial:
            workflow.mark_submission_intent_resolved(
                role="exit",
                client_order_id=build_exit_client_order_id(workflow.workflow_id),
                outcome="final_fill_reconciled",
                broker_order_id=broker_order_id,
            )

    @staticmethod
    def _wait_for_sell_position_sync(
        symbol: str,
        *,
        expected_remaining_qty: float | None,
        timeout: float = 5.0,
    ) -> tuple[Any | None, list[Any]]:
        """Wait until broker position quantity reflects the cumulative sell fill."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            position, orders = _sample_stable_symbol_state(symbol)
            if expected_remaining_qty is None:
                return position, orders
            if position is None and expected_remaining_qty <= 0.0001:
                return position, orders
            if (
                position is not None
                and position.qty <= expected_remaining_qty + 0.0001
            ):
                return position, orders
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"Broker position for {symbol} did not reflect terminal sell quantity"
                )
            time.sleep(min(0.1, remaining))

    @staticmethod
    def _latest_cumulative_sell_fill_qty(workflow: Any, broker_order_id: str) -> float:
        """Return the largest persisted cumulative fill for one sell order."""
        quantities: list[float] = []
        for transition in workflow.transitions:
            if transition.event not in {
                "sell_partial_fill_received",
                "sell_fill_received",
            }:
                continue
            if str(transition.details.get("broker_order_id", "") or "") != broker_order_id:
                continue
            try:
                quantities.append(float(transition.details.get("qty", 0.0) or 0.0))
            except (TypeError, ValueError):
                continue
        return max(quantities, default=0.0)

    @staticmethod
    def _cumulative_workflow_sell_fill_qty(workflow: Any) -> float:
        """Aggregate sell checkpoints once per identified replacement order."""
        by_order: dict[str, float] = {}
        for transition in workflow.transitions:
            if transition.event not in {
                "sell_partial_fill_received",
                "sell_fill_received",
            }:
                continue
            order_id = str(transition.details.get("broker_order_id", "") or "")
            if not order_id:
                continue
            try:
                quantity = float(transition.details.get("qty", 0.0) or 0.0)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Durable sell fill quantity is invalid") from exc
            if not math.isfinite(quantity) or quantity < 0:
                raise RuntimeError("Durable sell fill quantity is invalid")
            by_order[order_id] = max(by_order.get(order_id, 0.0), quantity)
        return sum(by_order.values())

    @staticmethod
    def _latest_cumulative_buy_fill(
        workflow: Any,
        broker_order_id: str,
    ) -> tuple[int, Any] | None:
        """Return the highest cumulative buy checkpoint for a broker order."""
        candidates: list[tuple[float, int, Any]] = []
        for index, transition in enumerate(workflow.transitions):
            if transition.event != "buy_fill_received":
                continue
            transition_order_id = str(
                transition.details.get("broker_order_id", "") or ""
            )
            if broker_order_id and transition_order_id != broker_order_id:
                continue
            try:
                quantity = float(transition.details.get("qty", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            candidates.append((quantity, index, transition))
        if not candidates:
            return None
        _, index, transition = max(candidates, key=lambda item: (item[0], item[1]))
        return index, transition

    @staticmethod
    def _cumulative_workflow_buy_fill(
        workflow: Any,
    ) -> tuple[float, float, set[str], str]:
        """Aggregate maximum cumulative fills across an entry replacement chain."""
        by_order: dict[str, tuple[float, float, int]] = {}
        for index, transition in enumerate(workflow.transitions):
            if transition.event != "buy_fill_received":
                continue
            order_id = str(transition.details.get("broker_order_id", "") or "")
            key = order_id or "__unidentified_entry__"
            try:
                quantity = float(transition.details.get("qty", 0.0) or 0.0)
                price = float(transition.details.get("fill_price", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            if quantity <= 0 or price <= 0:
                continue
            previous = by_order.get(key)
            if previous is None or quantity > previous[0] + 0.0001 or (
                abs(quantity - previous[0]) <= 0.0001 and index > previous[2]
            ):
                by_order[key] = (quantity, price, index)

        if not by_order:
            return 0.0, 0.0, set(), ""
        total_qty = sum(item[0] for item in by_order.values())
        weighted_price = sum(
            quantity * price for quantity, price, _ in by_order.values()
        ) / total_qty
        order_ids = {key for key in by_order if key != "__unidentified_entry__"}
        latest_order_id = max(
            by_order,
            key=lambda key: by_order[key][2],
        )
        if latest_order_id == "__unidentified_entry__":
            latest_order_id = ""
        return total_qty, weighted_price, order_ids, latest_order_id

    @staticmethod
    def _latest_foreign_late_buy_floor(
        workflow: Any,
        *,
        source_workflow_id: str,
    ) -> tuple[float, float]:
        """Return the durable source-net checkpoint and owner floor already applied."""
        source_net_qty = 0.0
        owner_floor = 0.0
        for transition in workflow.transitions:
            if transition.event != "late_buy_exposure_recovered":
                continue
            details = transition.details
            if str(details.get("source_workflow_id", "") or "") != (
                source_workflow_id
            ):
                continue
            try:
                candidate_source_net = float(
                    details.get("source_net_qty", 0.0) or 0.0
                )
                candidate_floor = float(details.get("qty", 0.0) or 0.0)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Durable late BUY recovery checkpoint is invalid") from exc
            if (
                not math.isfinite(candidate_source_net)
                or not math.isfinite(candidate_floor)
                or candidate_source_net < 0
                or candidate_floor < 0
            ):
                raise RuntimeError("Durable late BUY recovery checkpoint is invalid")
            if candidate_source_net >= source_net_qty:
                source_net_qty = candidate_source_net
                owner_floor = max(owner_floor, candidate_floor)
        return source_net_qty, owner_floor

    @staticmethod
    def _transition_float(checkpoint: tuple[int, Any] | None, key: str) -> float:
        if checkpoint is None:
            return 0.0
        try:
            return float(checkpoint[1].details.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def reconcile_startup_stops(self, symbol: str | None = None) -> list[ProtectiveStopResult]:
        """Strictly reconcile workflow-linked protection for broker positions."""
        with _FILL_HANDLING_LOCK:
            return self._reconcile_startup_stops_locked(symbol)

    @staticmethod
    def _load_submission_replacement_chain(
        *,
        client: Any,
        root_order: Any,
        workflow: Any,
        symbol: str,
        side: str,
        order_role: str,
    ) -> list[Any]:
        """Load and durably anchor one exact broker replacement chain."""
        chain: list[Any] = []
        seen: set[str] = set()
        current = root_order
        expected_parent_id = ""
        store = get_execution_store()

        while True:
            order_id = str(getattr(current, "id", "") or "").strip()
            order_symbol = str(getattr(current, "symbol", "") or "").strip().upper()
            order_side = str(getattr(current, "side", "")).split(".")[-1].lower()
            if not order_id or order_id in seen:
                raise RuntimeError("Submission replacement chain is missing or cyclic")
            if order_symbol != symbol or order_side != side:
                raise RuntimeError("Submission replacement chain identity does not match")
            if expected_parent_id:
                replaces = str(getattr(current, "replaces", "") or "").strip()
                if replaces != expected_parent_id:
                    raise RuntimeError("Submission replacement ancestry is not reciprocal")

            durable_owners = store.find_workflow_ids_by_broker_order_id(order_id)
            if durable_owners and durable_owners != {workflow.workflow_id}:
                raise RuntimeError("Submission replacement order has a conflicting owner")

            if chain:
                client_order_id = str(
                    getattr(current, "client_order_id", "") or ""
                ).strip()
                if order_role == "entry_order":
                    workflow.repair_entry_order_reference(
                        broker_order_id=order_id,
                        client_order_id=client_order_id,
                    )
                else:
                    workflow.repair_order_reference(
                        broker_order_id=order_id,
                        client_order_id=client_order_id,
                        order_role=order_role,
                    )

            chain.append(current)
            seen.add(order_id)
            status = str(getattr(current, "status", "")).split(".")[-1].lower()
            child_id = str(getattr(current, "replaced_by", "") or "").strip()
            if status == "replaced" and not child_id:
                raise RuntimeError("Replaced submission order has no traversable child")
            if not child_id:
                break
            if child_id == order_id or child_id in seen:
                raise RuntimeError("Submission replacement chain is cyclic")
            try:
                current = client.get_order_by_id(child_id)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"Cannot load submission replacement order {child_id}: {exc}"
                ) from exc
            expected_parent_id = order_id

        return chain

    def _reconcile_pending_entry_intents_locked(self, symbol: str | None) -> None:
        """Recover exact BUY orders accepted while the previous process was dying."""
        store = get_execution_store()
        for intent in store.load_pending_submission_intents(symbol=symbol):
            if intent["event"] != "entry_submission_intent":
                continue
            workflow_id = str(intent["workflow_id"])
            intent_symbol = str(intent["symbol"]).strip().upper()
            details = dict(intent.get("details", {}))
            client_order_id = str(details.get("client_order_id", "") or "")
            if (
                not workflow_id
                or client_order_id != workflow_id
                or str(details.get("symbol", "") or "").strip().upper()
                != intent_symbol
                or str(details.get("side", "") or "").strip().lower() != "buy"
            ):
                raise RuntimeError("Pending entry submission intent is malformed")
            workflow = get_workflow(workflow_id)
            if workflow is None or workflow.symbol.strip().upper() != intent_symbol:
                raise RuntimeError("Pending entry submission workflow is missing")
            client = _get_trading_client()
            try:
                order = client.get_order_by_client_id(client_order_id)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"Cannot resolve pending {intent_symbol} entry intent: {exc}"
                ) from exc

            broker_order_id = str(getattr(order, "id", "") or "").strip()
            order_client_id = str(
                getattr(order, "client_order_id", "") or ""
            ).strip()
            order_symbol = str(getattr(order, "symbol", "") or "").strip().upper()
            order_side = str(getattr(order, "side", "")).split(".")[-1].lower()
            if (
                not broker_order_id
                or order_client_id != client_order_id
                or order_symbol != intent_symbol
                or order_side != "buy"
            ):
                raise RuntimeError("Broker order does not match pending entry intent")

            workflow.claim_order_reference_from_submission_intent(
                broker_order_id=broker_order_id,
                client_order_id=client_order_id,
                order_role="entry_order",
                intent_event="entry_submission_intent",
                side="buy",
            )
            chain = self._load_submission_replacement_chain(
                client=client,
                root_order=order,
                workflow=workflow,
                symbol=intent_symbol,
                side="buy",
                order_role="entry_order",
            )
            checkpoints: list[tuple[Any, str, str, float, float]] = []
            total_filled = 0.0
            for chain_order in chain:
                chain_order_id = str(getattr(chain_order, "id", "") or "").strip()
                chain_status = (
                    str(getattr(chain_order, "status", "")).split(".")[-1].lower()
                )
                try:
                    chain_qty = float(getattr(chain_order, "filled_qty", 0) or 0)
                    chain_price = float(
                        getattr(chain_order, "filled_avg_price", 0) or 0
                    )
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("Pending entry broker fill data is invalid") from exc
                if chain_qty < 0 or (chain_qty > 0 and chain_price <= 0):
                    raise RuntimeError("Pending entry broker fill data is unsafe")
                total_filled += chain_qty
                checkpoints.append(
                    (chain_order, chain_order_id, chain_status, chain_qty, chain_price)
                )

            planned_qty = float(getattr(workflow.entry_plan, "qty", 0.0) or 0.0)
            if planned_qty <= 0 or total_filled > planned_qty + 0.0001:
                raise RuntimeError("Pending entry replacement fills exceed the execution plan")

            if total_filled > 0:
                for chain_order, chain_order_id, chain_status, chain_qty, chain_price in checkpoints:
                    if chain_qty <= 0:
                        continue
                    self._handle_buy_checkpoint_locked(
                        symbol=intent_symbol,
                        broker_order_id=chain_order_id,
                        workflow_id=workflow.workflow_id,
                        workflow=workflow,
                        filled_qty=chain_qty,
                        fill_price=chain_price,
                        partial=(
                            chain_status != "filled" or chain_order is not chain[-1]
                        ),
                    )
                leaf_order_id = str(getattr(chain[-1], "id", "") or "").strip()
                leaf_status = checkpoints[-1][2]
                if leaf_status in _SUBMISSION_WORKING_STATUSES:
                    workflow.mark_order_submitted(broker_order_id=leaf_order_id)
                    continue
                unsafe_statuses = {
                    chain_status
                    for (
                        _chain_order,
                        _chain_order_id,
                        chain_status,
                        _chain_qty,
                        _chain_price,
                    ) in checkpoints
                    if chain_status not in _CLOSED_REPLAY_ORDER_STATUSES
                }
                if unsafe_statuses:
                    raise RuntimeError(
                        "Pending entry replacement chain has unsafe statuses: "
                        f"{sorted(unsafe_statuses)}"
                    )
                pending_still_unresolved = any(
                    item["workflow_id"] == workflow.workflow_id
                    and item["event"] == "entry_submission_intent"
                    for item in store.load_pending_submission_intents(
                        symbol=intent_symbol
                    )
                )
                if pending_still_unresolved:
                    workflow.mark_submission_intent_resolved(
                        role="entry",
                        client_order_id=client_order_id,
                        outcome="fill_replayed",
                        broker_order_id=leaf_order_id,
                    )
                continue

            leaf = chain[-1]
            leaf_order_id = str(getattr(leaf, "id", "") or "").strip()
            leaf_status = str(getattr(leaf, "status", "")).split(".")[-1].lower()
            if leaf_status in _SUBMISSION_ZERO_FILL_TERMINAL_STATUSES:
                workflow.mark_order_submit_failed(
                    error=f"recovered broker order is {leaf_status} with zero fill"
                )
                workflow.mark_submission_intent_resolved(
                    role="entry",
                    client_order_id=client_order_id,
                    outcome="zero_fill_terminal",
                    broker_order_id=leaf_order_id,
                )
                continue
            if leaf_status not in _SUBMISSION_WORKING_STATUSES:
                raise RuntimeError(
                    f"Pending entry broker status is unsafe: {leaf_status or 'missing'}"
                )
            workflow.mark_order_submitted(broker_order_id=leaf_order_id)

    def _reconcile_pending_exit_intents_locked(self, symbol: str | None) -> None:
        """Recover exact SELL orders accepted before their identity was durable."""
        store = get_execution_store()
        for intent in store.load_pending_submission_intents(symbol=symbol):
            if intent["event"] != "exit_submission_intent":
                continue
            workflow_id = str(intent["workflow_id"])
            intent_symbol = str(intent["symbol"]).strip().upper()
            details = dict(intent.get("details", {}))
            client_order_id = str(details.get("client_order_id", "") or "")
            exit_reason = str(details.get("exit_reason", "") or "restart recovery")
            if (
                not workflow_id
                or client_order_id != build_exit_client_order_id(workflow_id)
                or str(details.get("symbol", "") or "").strip().upper()
                != intent_symbol
                or str(details.get("side", "") or "").strip().lower() != "sell"
            ):
                raise RuntimeError("Pending exit submission intent is malformed")
            workflow = get_workflow(workflow_id)
            if workflow is None or workflow.symbol.strip().upper() != intent_symbol:
                raise RuntimeError("Pending exit submission workflow is missing")
            active = store.load_active_position(intent_symbol)
            if active is not None and str(active.get("workflow_id", "")) != workflow_id:
                raise RuntimeError("Pending exit intent conflicts with active ownership")

            client = _get_trading_client()
            try:
                order = client.get_order_by_client_id(client_order_id)
            except Exception as exc:  # noqa: BLE001
                try:
                    safety = reconcile_symbol_after_exit_failure(
                        intent_symbol,
                        workflow_id=workflow_id,
                    )
                    if safety.action not in {"flat", "pending_exit"}:
                        self._record_protective_stop(
                            workflow,
                            safety,
                            action=f"pending_exit_lookup_{safety.action}",
                        )
                    if not safety.success:
                        raise RuntimeError(safety.error or safety.action)
                except Exception as safety_exc:  # noqa: BLE001
                    raise RuntimeError(
                        f"Cannot resolve pending {intent_symbol} exit intent and "
                        f"safety remains unproven: {safety_exc}"
                    ) from safety_exc
                raise RuntimeError(
                    f"Cannot resolve pending {intent_symbol} exit intent: {exc}"
                ) from exc

            broker_order_id = str(getattr(order, "id", "") or "").strip()
            order_client_id = str(
                getattr(order, "client_order_id", "") or ""
            ).strip()
            order_symbol = str(getattr(order, "symbol", "") or "").strip().upper()
            order_side = str(getattr(order, "side", "")).split(".")[-1].lower()
            order_type = str(getattr(order, "type", "")).split(".")[-1].lower()
            if (
                not broker_order_id
                or order_client_id != client_order_id
                or order_symbol != intent_symbol
                or order_side != "sell"
                or order_type != "market"
            ):
                raise RuntimeError("Broker order does not match pending exit intent")

            workflow.claim_order_reference_from_submission_intent(
                broker_order_id=broker_order_id,
                client_order_id=client_order_id,
                order_role="exit_order",
                intent_event="exit_submission_intent",
                side="sell",
            )
            chain = self._load_submission_replacement_chain(
                client=client,
                root_order=order,
                workflow=workflow,
                symbol=intent_symbol,
                side="sell",
                order_role="exit_order",
            )
            checkpoints: list[tuple[Any, str, str, str, float, float]] = []
            total_filled = 0.0
            for chain_order in chain:
                chain_order_id = str(getattr(chain_order, "id", "") or "").strip()
                chain_client_id = str(
                    getattr(chain_order, "client_order_id", "") or ""
                ).strip()
                chain_status = (
                    str(getattr(chain_order, "status", "")).split(".")[-1].lower()
                )
                chain_type = (
                    str(getattr(chain_order, "type", "")).split(".")[-1].lower()
                )
                if chain_type != "market":
                    raise RuntimeError("Pending exit replacement order is not market")
                try:
                    chain_qty = float(getattr(chain_order, "filled_qty", 0) or 0)
                    chain_price = float(
                        getattr(chain_order, "filled_avg_price", 0) or 0
                    )
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("Pending exit broker fill data is invalid") from exc
                if chain_qty < 0 or (chain_qty > 0 and chain_price <= 0):
                    raise RuntimeError("Pending exit broker fill data is unsafe")
                total_filled += chain_qty
                checkpoints.append(
                    (
                        chain_order,
                        chain_order_id,
                        chain_client_id,
                        chain_status,
                        chain_qty,
                        chain_price,
                    )
                )

            if total_filled > 0:
                position, open_orders = _sample_stable_symbol_state(intent_symbol)
                if position is None and not open_orders:
                    unsafe_statuses = {
                        chain_status
                        for (
                            _chain_order,
                            _chain_order_id,
                            _chain_client_id,
                            chain_status,
                            _chain_qty,
                            _chain_price,
                        ) in checkpoints
                        if chain_status not in _CLOSED_REPLAY_ORDER_STATUSES
                    }
                    if unsafe_statuses:
                        raise RuntimeError(
                            "Cannot replay a broker-flat exit chain with nonterminal "
                            f"statuses: {sorted(unsafe_statuses)}"
                        )
                    try:
                        required_qty = float((active or {}).get("qty", 0.0) or 0.0)
                    except (TypeError, ValueError) as exc:
                        raise RuntimeError(
                            "Pending exit durable quantity is invalid"
                        ) from exc
                    if required_qty > 0 and total_filled + 0.0001 < required_qty:
                        raise RuntimeError(
                            "Broker is flat but the pending exit replacement chain "
                            "does not cover the durable position"
                        )
                    replay_checkpoints = [
                        ClosedSellCheckpoint(
                            broker_order_id=chain_order_id,
                            client_order_id=chain_client_id,
                            qty=chain_qty,
                            fill_price=chain_price,
                        )
                        for (
                            _chain_order,
                            chain_order_id,
                            chain_client_id,
                            _chain_status,
                            chain_qty,
                            chain_price,
                        ) in checkpoints
                        if chain_qty > 0
                    ]
                    workflow.replay_closed_sell_chain(
                        replay_checkpoints,
                        exit_reason=exit_reason,
                    )
                    workflow.mark_submission_intent_resolved(
                        role="exit",
                        client_order_id=client_order_id,
                        outcome="fill_replayed",
                        broker_order_id=str(getattr(chain[-1], "id", "") or ""),
                    )
                    continue
                for (
                    chain_order,
                    chain_order_id,
                    chain_client_id,
                    chain_status,
                    chain_qty,
                    chain_price,
                ) in checkpoints:
                    if chain_qty <= 0:
                        continue
                    recorded_fill_index = self._find_recorded_fill_index(
                        workflow,
                        side="sell",
                        broker_order_id=chain_order_id,
                        filled_qty=chain_qty,
                        fill_price=chain_price,
                    )
                    self._handle_sell_checkpoint_locked(
                        symbol=intent_symbol,
                        broker_order_id=chain_order_id,
                        client_order_id=chain_client_id,
                        workflow_id=workflow.workflow_id,
                        workflow=workflow,
                        filled_qty=chain_qty,
                        fill_price=chain_price,
                        order_type="market",
                        partial=(
                            chain_status != "filled" or chain_order is not chain[-1]
                        ),
                        recorded_fill_index=recorded_fill_index,
                    )
                leaf_order_id = str(getattr(chain[-1], "id", "") or "").strip()
                leaf_status = checkpoints[-1][3]
                if leaf_status in _SUBMISSION_WORKING_STATUSES:
                    workflow.mark_exit_order_submitted(
                        exit_reason=exit_reason,
                        broker_order_id=leaf_order_id,
                    )
                    continue
                unsafe_statuses = {
                    chain_status
                    for (
                        _chain_order,
                        _chain_order_id,
                        _chain_client_id,
                        chain_status,
                        _chain_qty,
                        _chain_price,
                    ) in checkpoints
                    if chain_status not in _CLOSED_REPLAY_ORDER_STATUSES
                }
                if unsafe_statuses:
                    raise RuntimeError(
                        "Pending exit replacement chain has unsafe statuses: "
                        f"{sorted(unsafe_statuses)}"
                    )
                pending_still_unresolved = any(
                    item["workflow_id"] == workflow.workflow_id
                    and item["event"] == "exit_submission_intent"
                    for item in store.load_pending_submission_intents(
                        symbol=intent_symbol
                    )
                )
                if pending_still_unresolved:
                    workflow.mark_submission_intent_resolved(
                        role="exit",
                        client_order_id=client_order_id,
                        outcome="fill_replayed",
                        broker_order_id=leaf_order_id,
                    )
                continue

            leaf = chain[-1]
            leaf_order_id = str(getattr(leaf, "id", "") or "").strip()
            leaf_status = str(getattr(leaf, "status", "")).split(".")[-1].lower()
            if leaf_status in _SUBMISSION_ZERO_FILL_TERMINAL_STATUSES:
                workflow.mark_exit_order_submit_failed(
                    exit_reason=exit_reason,
                    error=f"recovered broker order is {leaf_status} with zero fill",
                )
                workflow.mark_submission_intent_resolved(
                    role="exit",
                    client_order_id=client_order_id,
                    outcome="zero_fill_terminal",
                    broker_order_id=leaf_order_id,
                )
                self._restore_owned_exit_safety(
                    intent_symbol,
                    workflow,
                    action=f"pending_exit_{leaf_status}",
                )
                continue
            if leaf_status not in _SUBMISSION_WORKING_STATUSES:
                raise RuntimeError(
                    f"Pending exit broker status is unsafe: {leaf_status or 'missing'}"
                )
            workflow.mark_exit_order_submitted(
                exit_reason=exit_reason,
                broker_order_id=leaf_order_id,
            )
            safety = reconcile_symbol_after_exit_failure(
                intent_symbol,
                workflow_id=workflow.workflow_id,
            )
            if not safety.success or safety.action != "pending_exit":
                raise RuntimeError(
                    f"Pending exit proof failed: {safety.error or safety.action}"
                )

    def _reconcile_startup_stops_locked(
        self,
        symbol: str | None,
    ) -> list[ProtectiveStopResult]:
        """Run startup reconciliation serialized against fill processing."""
        target_symbol = str(symbol or "").strip().upper()
        self._reconcile_pending_entry_intents_locked(target_symbol or None)
        self._reconcile_pending_exit_intents_locked(target_symbol or None)
        positions = get_open_positions(raise_on_error=True)
        results: list[ProtectiveStopResult] = []
        for position in positions:
            if target_symbol and position.symbol != target_symbol:
                continue
            if position.qty <= 0 or position.avg_entry_price <= 0:
                raise ValueError(
                    f"Invalid broker position values for {position.symbol}: "
                    f"qty={position.qty}, avg_entry_price={position.avg_entry_price}"
                )
            workflow = get_active_workflow_for_symbol(position.symbol)
            if workflow is None:
                workflow = recover_active_position_workflow(
                    position.symbol,
                    qty=position.qty,
                    avg_entry_price=position.avg_entry_price,
                )
            else:
                workflow.repair_buy_fill_storage(
                    qty=position.qty,
                    fill_price=position.avg_entry_price,
                    broker_order_id="",
                    restore_active=True,
                    preserve_higher_qty=False,
                )
            result = reconcile_symbol_after_exit_failure(
                position.symbol,
                workflow_id=workflow.workflow_id,
                minimum_position_qty=position.qty,
            )
            results.append(result)
            if result.action in {"flat", "pending_exit"}:
                continue
            self._record_protective_stop(
                workflow,
                result,
                action=f"startup_{result.action}",
            )
        return results

    @staticmethod
    def _record_protective_stop(
        workflow: Any,
        protection: ProtectiveStopResult,
        *,
        action: str,
    ) -> None:
        kwargs: dict[str, Any] = {
            "success": protection.success,
            "stop_order_id": protection.order_id,
            "stop_price": protection.stop_price,
            "action": action,
            "error": protection.error,
        }
        stop_client_order_id = str(getattr(protection, "client_order_id", "") or "")
        if stop_client_order_id:
            kwargs["stop_client_order_id"] = stop_client_order_id
        workflow.mark_protective_stop(**kwargs)

    def _restore_owned_exit_safety(
        self,
        symbol: str,
        workflow: Any,
        *,
        action: str,
    ) -> ProtectiveStopResult:
        """Protect a durable long after an exit produced no trustworthy fill."""
        normalized_symbol = symbol.strip().upper()
        store = get_execution_store()
        active = store.load_active_position(normalized_symbol)
        if active is None:
            position, open_orders = _sample_stable_symbol_state(normalized_symbol)
            if position is None and not open_orders:
                return ProtectiveStopResult(
                    success=True,
                    order_id="",
                    symbol=normalized_symbol,
                    qty=0.0,
                    stop_price=0.0,
                    action="flat",
                )
            if position is None or open_orders:
                raise RuntimeError(
                    f"Safety remains unproven for {normalized_symbol}: "
                    "broker state is not flat and order-free"
                )
            workflow = self._repair_broker_position_owner(
                symbol=normalized_symbol,
                source_workflow=workflow,
                position=position,
            )
            active = store.load_active_position(normalized_symbol)

        active_workflow_id = str((active or {}).get("workflow_id", "") or "")
        if active_workflow_id != workflow.workflow_id:
            raise RuntimeError(
                f"Safety remains unproven for {normalized_symbol}: "
                "durable ownership conflicts with the exit workflow"
            )
        try:
            active_qty = float((active or {}).get("qty", 0.0) or 0.0)
            active_entry_price = float(
                (active or {}).get("entry_price", 0.0) or 0.0
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Safety remains unproven for {normalized_symbol}: "
                "durable ownership values are invalid"
            ) from exc
        if active_qty <= 0 or active_entry_price <= 0:
            raise RuntimeError(
                f"Safety remains unproven for {normalized_symbol}: "
                "durable ownership values are invalid"
            )

        cumulative = self._cumulative_workflow_buy_fill(workflow)
        protection = ensure_protective_stop(
            symbol=normalized_symbol,
            qty=active_qty,
            fill_price=active_entry_price,
            workflow_id=workflow.workflow_id,
            entry_order_id=cumulative[3] or None,
            entry_order_ids=cumulative[2] or None,
            durable_sell_fill_qty=self._cumulative_workflow_sell_fill_qty(workflow),
        )
        self._record_protective_stop(
            workflow,
            protection,
            action=f"{action}_{protection.action}",
        )
        if not protection.success:
            raise RuntimeError(
                f"Safety remains unproven for {normalized_symbol}: "
                f"{protection.error or protection.action}"
            )
        return protection

    @staticmethod
    def _same_persisted_protection(
        transition: Any | None,
        protection: ProtectiveStopResult,
    ) -> bool:
        """Return True when a fresh broker proof matches the persisted stop."""
        if transition is None or not protection.success or not protection.order_id:
            return False
        details = transition.details
        try:
            persisted_price = float(details.get("stop_price", 0.0) or 0.0)
        except (TypeError, ValueError):
            return False
        return bool(
            details.get("success")
            and str(details.get("stop_order_id", "")) == protection.order_id
            and abs(persisted_price - protection.stop_price) <= 0.01
        )

    def _prove_symbol_safe_or_raise(self, symbol: str, workflow: Any) -> None:
        """Require a final broker proof after both protection and exit failed."""
        try:
            safety = reconcile_symbol_after_exit_failure(
                symbol,
                workflow_id=workflow.workflow_id,
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"Safety remains unproven for {symbol}: {exc}"
            ) from exc
        if safety.action not in {"flat", "pending_exit"}:
            self._record_protective_stop(
                workflow,
                safety,
                action=f"emergency_{safety.action}",
            )
        if not safety.success:
            raise RuntimeError(
                f"Safety remains unproven for {symbol}: "
                f"{safety.error or safety.action}"
            )

    def _recover_exit_safety(
        self,
        symbol: str,
        workflow: Any,
        *,
        error: str,
    ) -> str:
        """Immediately restore or prove protection after an exit-path failure."""
        try:
            safety = reconcile_symbol_after_exit_failure(
                symbol,
                workflow_id=workflow.workflow_id,
            )
            if safety.action not in {"flat", "pending_exit"}:
                self._record_protective_stop(
                    workflow,
                    safety,
                    action=f"exit_failure_{safety.action}",
                )
            if not safety.success:
                return (
                    f"{error}; protection recovery failed: "
                    f"{safety.error or safety.action}"
                )
            return error
        except Exception as exc:  # noqa: BLE001
            workflow.mark_protective_stop(
                success=False,
                stop_order_id="",
                stop_price=0.0,
                action="exit_failure_inspection_failed",
                error=str(exc),
            )
            return f"{error}; protection recovery failed: {exc}"

    @staticmethod
    def _infer_exit_reason(*, order_type: str, client_order_id: str) -> str:
        """Infer a human-readable exit reason from broker metadata."""
        normalized_type = str(order_type).split(".")[-1].strip().lower()
        normalized_client_order_id = str(client_order_id).lower()
        if "stop" in normalized_type:
            return "stop-loss triggered"
        if "ma" in normalized_client_order_id or "ema" in normalized_client_order_id:
            return "MA violation exit"
        return "exit order filled"
