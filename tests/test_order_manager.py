"""Unit tests for the high-level OrderManager execution service."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.execution_workflow import EntryExecutionPlan
from core.order_execution import OrderResult
from core.order_manager import OrderManager


def _plan(symbol: str = "NVDA") -> EntryExecutionPlan:
    return EntryExecutionPlan(
        symbol=symbol,
        entry_price=500.0,
        price_source="intraday_minute_close",
        stop_price=465.0,
        stop_loss_pct=0.07,
        position_value=10_000.0,
        risk_amount=700.0,
        risk_per_share=35.0,
        qty=20.0,
        canslim_score=82.0,
        rs_score=94.0,
        is_breakout=True,
        has_volume_surge=True,
    )


class TestSubmitEntry:
    def test_submit_entry_creates_workflow_submits_order_and_notifies(self) -> None:
        workflow = SimpleNamespace(
            workflow_id="wf-nvda-1",
            mark_dry_run_skipped=MagicMock(),
            mark_order_submitted=MagicMock(),
            mark_entry_notification=MagicMock(),
            mark_order_submit_failed=MagicMock(),
        )
        manager = OrderManager(paper=True)

        with (
            patch("core.order_manager.create_entry_workflow", return_value=workflow),
            patch(
                "core.order_manager.submit_bracket_buy",
                return_value=OrderResult(True, "broker-1", "NVDA", "buy", 20.0, client_order_id="wf-nvda-1"),
            ) as mock_submit,
            patch("core.order_manager.notify_entry_submitted", return_value=True) as mock_notify,
        ):
            outcome = manager.submit_entry(_plan(), signal_payload={"symbol": "NVDA"})

        assert outcome.success is True
        assert outcome.workflow_id == "wf-nvda-1"
        mock_submit.assert_called_once()
        assert mock_submit.call_args.kwargs["client_order_id"] == "wf-nvda-1"
        mock_notify.assert_called_once()
        workflow.mark_order_submitted.assert_called_once_with(broker_order_id="broker-1")
        workflow.mark_entry_notification.assert_called_once_with(sent=True)

    def test_submit_entry_marks_dry_run_without_order(self) -> None:
        workflow = SimpleNamespace(
            workflow_id="wf-amd-1",
            mark_dry_run_skipped=MagicMock(),
            mark_order_submitted=MagicMock(),
            mark_entry_notification=MagicMock(),
            mark_order_submit_failed=MagicMock(),
        )
        manager = OrderManager(paper=True)

        with (
            patch("core.order_manager.create_entry_workflow", return_value=workflow),
            patch("core.order_manager.submit_bracket_buy") as mock_submit,
        ):
            outcome = manager.submit_entry(_plan("AMD"), signal_payload={"symbol": "AMD"}, dry_run=True)

        assert outcome.success is True
        assert outcome.dry_run is True
        mock_submit.assert_not_called()
        workflow.mark_dry_run_skipped.assert_called_once()


class TestHandleFill:
    def test_handle_buy_fill_reconciles_stop_and_notifies(self) -> None:
        workflow = SimpleNamespace(
            mark_buy_fill=MagicMock(),
            mark_protective_stop=MagicMock(),
            mark_buy_fill_notification=MagicMock(),
        )
        manager = OrderManager(paper=True)

        with (
            patch("core.order_manager.resolve_workflow", return_value=workflow),
            patch(
                "core.order_manager.ensure_protective_stop",
                return_value=SimpleNamespace(
                    success=True,
                    order_id="stop-1",
                    stop_price=465.0,
                    action="submitted",
                    error="",
                ),
            ) as mock_stop,
            patch("core.order_manager.notify_buy_filled", return_value=True) as mock_notify,
        ):
            manager.handle_fill(
                symbol="NVDA",
                broker_order_id="broker-1",
                client_order_id="wf-nvda-1",
                side="buy",
                filled_qty=20.0,
                fill_price=500.0,
                order_type="limit",
            )

        mock_stop.assert_called_once_with(
            symbol="NVDA",
            qty=20.0,
            fill_price=500.0,
            workflow_id="wf-nvda-1",
        )
        mock_notify.assert_called_once()
        workflow.mark_buy_fill.assert_called_once_with(
            qty=20.0,
            fill_price=500.0,
            broker_order_id="broker-1",
        )
        workflow.mark_protective_stop.assert_called_once()
        workflow.mark_buy_fill_notification.assert_called_once_with(sent=True)

    def test_handle_sell_fill_marks_workflow_and_notifies(self) -> None:
        workflow = SimpleNamespace(
            mark_sell_fill=MagicMock(),
            mark_sell_notification=MagicMock(),
        )
        manager = OrderManager(paper=True)

        with (
            patch("core.order_manager.resolve_workflow", return_value=workflow),
            patch("core.order_manager.notify_sell_filled", return_value=True) as mock_notify,
        ):
            manager.handle_fill(
                symbol="NVDA",
                broker_order_id="broker-2",
                client_order_id="wf-nvda-1",
                side="sell",
                filled_qty=20.0,
                fill_price=465.0,
                order_type="stop",
            )

        mock_notify.assert_called_once()
        workflow.mark_sell_fill.assert_called_once()
        workflow.mark_sell_notification.assert_called_once_with(sent=True)

    def test_handle_partial_buy_fill_reconciles_stop_without_notification(self) -> None:
        workflow = SimpleNamespace(
            mark_buy_fill=MagicMock(),
            mark_protective_stop=MagicMock(),
        )
        manager = OrderManager(paper=True)

        with (
            patch("core.order_manager.resolve_workflow", return_value=workflow),
            patch(
                "core.order_manager.ensure_protective_stop",
                return_value=SimpleNamespace(
                    success=True,
                    order_id="stop-1",
                    stop_price=465.0,
                    action="submitted",
                    error="",
                ),
            ) as mock_stop,
            patch("core.order_manager.notify_buy_filled") as mock_notify,
        ):
            manager.handle_partial_fill(
                symbol="NVDA",
                broker_order_id="broker-1",
                client_order_id="wf-nvda-1",
                side="buy",
                filled_qty=5.0,
                fill_price=500.0,
                order_type="limit",
            )

        mock_stop.assert_called_once_with(
            symbol="NVDA",
            qty=5.0,
            fill_price=500.0,
            workflow_id="wf-nvda-1",
        )
        mock_notify.assert_not_called()
        workflow.mark_buy_fill.assert_called_once_with(
            qty=5.0,
            fill_price=500.0,
            broker_order_id="broker-1",
        )
        workflow.mark_protective_stop.assert_called_once_with(
            success=True,
            stop_order_id="stop-1",
            stop_price=465.0,
            action="partial_submitted",
            error="",
        )


class TestSubmitExit:
    def test_submit_exit_uses_existing_workflow_and_submits_close(self) -> None:
        workflow = SimpleNamespace(
            workflow_id="wf-nvda-1",
            mark_exit_order_submitted=MagicMock(),
            mark_exit_order_submit_failed=MagicMock(),
        )
        manager = OrderManager(paper=True)

        with (
            patch("core.order_manager.get_or_create_exit_workflow", return_value=workflow),
            patch("core.order_manager.cancel_open_orders") as mock_cancel,
            patch(
                "core.order_manager.close_position",
                return_value=OrderResult(True, "sell-1", "NVDA", "sell", 20.0, client_order_id="wf-nvda-1"),
            ) as mock_close,
        ):
            result = manager.submit_exit("NVDA", exit_reason="hard stop triggered")

        assert result.success is True
        mock_cancel.assert_called_once_with("NVDA")
        mock_close.assert_called_once_with("NVDA", client_order_id="wf-nvda-1")
        workflow.mark_exit_order_submitted.assert_called_once_with(
            exit_reason="hard stop triggered",
            broker_order_id="sell-1",
        )


class TestStartupReconciliation:
    def test_startup_reconciliation_attaches_stop_repairs_to_active_workflows(self) -> None:
        workflow = SimpleNamespace(mark_protective_stop=MagicMock())
        manager = OrderManager(paper=True)

        with (
            patch(
                "core.order_manager.reconcile_open_position_stops",
                return_value=[
                    SimpleNamespace(
                        success=True,
                        action="submitted",
                        symbol="NVDA",
                        order_id="stop-1",
                        stop_price=465.0,
                        error="",
                    )
                ],
            ),
            patch("core.order_manager.get_active_workflow_for_symbol", return_value=workflow),
        ):
            results = manager.reconcile_startup_stops()

        assert len(results) == 1
        workflow.mark_protective_stop.assert_called_once_with(
            success=True,
            stop_order_id="stop-1",
            stop_price=465.0,
            action="startup_submitted",
            error="",
        )


class TestInferExitReason:
    @pytest.mark.parametrize(
        ("order_type", "client_order_id", "expected"),
        [
            ("stop", "wf-nvda-1", "stop-loss triggered"),
            ("market", "wf-nvda-ma", "MA violation exit"),
            ("market", "wf-nvda-1", "exit order filled"),
        ],
    )
    def test_infer_exit_reason(self, order_type: str, client_order_id: str, expected: str) -> None:
        assert OrderManager._infer_exit_reason(order_type=order_type, client_order_id=client_order_id) == expected
