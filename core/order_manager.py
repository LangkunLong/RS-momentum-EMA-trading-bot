"""High-level order manager for entries, exits, fills, and protection.

This service is the authoritative orchestration layer above broker primitives.
It keeps state-machine transitions, notifications, and broker operations aligned
under a single workflow id.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from core.execution_store import get_execution_store
from core.execution_workflow import (
    EntryExecutionPlan,
    create_entry_workflow,
    get_active_workflow_for_symbol,
    get_or_create_exit_workflow,
    get_or_recover_workflow,
    normalize_workflow_id,
    resolve_workflow,
)
from core.notifier import notify_buy_filled, notify_entry_submitted, notify_sell_filled
from core.order_execution import (
    OrderResult,
    ProtectiveStopResult,
    _is_paper_mode,
    cancel_open_orders,
    close_position,
    ensure_protective_stop,
    reconcile_open_position_stops,
    require_paper_mode,
    submit_bracket_buy,
)


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
        workflow = create_entry_workflow(plan, signal_payload=signal_payload)

        if dry_run:
            workflow.mark_dry_run_skipped()
            return EntrySubmissionOutcome(
                symbol=plan.symbol,
                workflow_id=workflow.workflow_id,
                success=True,
                dry_run=True,
            )

        result = submit_bracket_buy(
            symbol=plan.symbol,
            qty=plan.qty,
            stop_loss_pct=plan.stop_loss_pct,
            limit_price=plan.entry_price,
            client_order_id=workflow.workflow_id,
        )
        if result.success:
            workflow.mark_order_submitted(broker_order_id=result.order_id)
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
            workflow.mark_entry_notification(sent=notification_sent)
            return EntrySubmissionOutcome(
                symbol=plan.symbol,
                workflow_id=workflow.workflow_id,
                success=True,
                dry_run=False,
                order_id=result.order_id,
            )

        workflow.mark_order_submit_failed(error=result.error or "unknown order submission error")
        return EntrySubmissionOutcome(
            symbol=plan.symbol,
            workflow_id=workflow.workflow_id,
            success=False,
            dry_run=False,
            error=result.error,
        )

    def submit_exit(self, symbol: str, *, exit_reason: str) -> OrderResult:
        """Cancel open orders and submit a market exit under the active workflow."""
        workflow = get_or_create_exit_workflow(symbol, exit_reason=exit_reason)
        cancel_open_orders(symbol)
        result = close_position(symbol, client_order_id=workflow.workflow_id)
        if result.success and result.qty > 0:
            workflow.mark_exit_order_submitted(
                exit_reason=exit_reason,
                broker_order_id=result.order_id,
            )
        elif not result.success:
            workflow.mark_exit_order_submit_failed(
                exit_reason=exit_reason,
                error=result.error or "unknown exit submission error",
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
    ) -> None:
        """Handle a broker fill update and advance the workflow."""
        workflow_id = normalize_workflow_id(client_order_id)
        workflow = resolve_workflow(
            symbol=symbol,
            workflow_id=workflow_id,
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
        )

        if self._has_recorded_fill(
            workflow,
            side=side,
            broker_order_id=broker_order_id,
            filled_qty=filled_qty,
            fill_price=fill_price,
        ):
            return

        if side == "buy":
            if workflow is None and workflow_id:
                workflow = get_or_recover_workflow(workflow_id, symbol=symbol, broker_order_id=broker_order_id)
            if workflow is not None:
                workflow.mark_buy_fill(
                    qty=filled_qty,
                    fill_price=fill_price,
                    broker_order_id=broker_order_id,
                )
            protection = ensure_protective_stop(
                symbol=symbol,
                qty=filled_qty,
                fill_price=fill_price,
                workflow_id=workflow_id or None,
            )
            if workflow is not None:
                workflow.mark_protective_stop(
                    success=protection.success,
                    stop_order_id=protection.order_id,
                    stop_price=protection.stop_price,
                    action=protection.action,
                    error=protection.error,
                )
            sent = notify_buy_filled(
                symbol=symbol,
                qty=filled_qty,
                fill_price=fill_price,
                stop_price=protection.stop_price,
                workflow_id=workflow_id or None,
                paper=self._paper,
            )
            if workflow is not None:
                workflow.mark_buy_fill_notification(sent=sent)
            return

        exit_reason = self._infer_exit_reason(order_type=order_type, client_order_id=client_order_id)
        entry_price = self._resolve_entry_price(symbol, workflow)
        if workflow is not None:
            workflow.mark_sell_fill(
                qty=filled_qty,
                fill_price=fill_price,
                exit_reason=exit_reason,
                broker_order_id=broker_order_id,
            )
        else:
            get_execution_store().clear_active_position(symbol)
        sent = notify_sell_filled(
            symbol=symbol,
            qty=filled_qty,
            fill_price=fill_price,
            entry_price=entry_price,
            exit_reason=exit_reason,
            workflow_id=workflow_id or None,
            paper=self._paper,
        )
        if workflow is not None:
            workflow.mark_sell_notification(sent=sent)

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
    def _has_recorded_fill(
        workflow: Any | None,
        *,
        side: str,
        broker_order_id: str,
        filled_qty: float,
        fill_price: float,
    ) -> bool:
        """Return True when this cumulative broker fill was already processed."""
        if workflow is None or not broker_order_id:
            return False

        expected_event = "buy_fill_received" if side == "buy" else "sell_fill_received"
        for transition in reversed(workflow.transitions):
            if transition.event != expected_event:
                continue
            details = transition.details
            return (
                str(details.get("broker_order_id", "")) == broker_order_id
                and float(details.get("qty", -1.0)) == filled_qty
                and float(details.get("fill_price", -1.0)) == fill_price
            )
        return False

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
    ) -> None:
        """Handle a partial fill update without sending final-fill notifications."""
        if side != "buy":
            return

        workflow_id = normalize_workflow_id(client_order_id)
        workflow = resolve_workflow(
            symbol=symbol,
            workflow_id=workflow_id,
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
        )

        if self._has_recorded_fill(
            workflow,
            side=side,
            broker_order_id=broker_order_id,
            filled_qty=filled_qty,
            fill_price=fill_price,
        ):
            return

        if workflow is None and workflow_id:
            workflow = get_or_recover_workflow(workflow_id, symbol=symbol, broker_order_id=broker_order_id)

        if workflow is not None:
            workflow.mark_buy_fill(
                qty=filled_qty,
                fill_price=fill_price,
                broker_order_id=broker_order_id,
            )

        protection = ensure_protective_stop(
            symbol=symbol,
            qty=filled_qty,
            fill_price=fill_price,
            workflow_id=workflow_id or None,
        )
        if workflow is not None:
            workflow.mark_protective_stop(
                success=protection.success,
                stop_order_id=protection.order_id,
                stop_price=protection.stop_price,
                action=f"partial_{protection.action}",
                error=protection.error,
            )

    def reconcile_startup_stops(self) -> list[ProtectiveStopResult]:
        """Repair missing or stale protective stops for open positions."""
        results = reconcile_open_position_stops()
        for result in results:
            if not result.success or result.action == "skipped_pending_exit":
                continue
            workflow = get_active_workflow_for_symbol(result.symbol)
            if workflow is None:
                continue
            workflow.mark_protective_stop(
                success=result.success,
                stop_order_id=result.order_id,
                stop_price=result.stop_price,
                action=f"startup_{result.action}",
                error=result.error,
            )
        return results

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
