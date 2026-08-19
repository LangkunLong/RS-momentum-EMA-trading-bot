"""Unit tests for fill_monitor.FillMonitor — mocked TradingStream."""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from alpaca.trading.stream import TradingStream


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_order(
    *,
    broker_order_id: str = "",
    symbol: str = "NVDA",
    side: str = "buy",
    filled_qty: str = "10",
    filled_avg_price: str = "900.00",
    type: str = "limit",
    client_order_id: str = "test-order-1",
    replaces: str = "",
    replaced_by: str = "",
    legs: list | None = None,
) -> SimpleNamespace:
    """Build a minimal mock order object matching Alpaca's Order model."""
    return SimpleNamespace(
        id=broker_order_id,
        symbol=symbol,
        side=side,
        filled_qty=filled_qty,
        filled_avg_price=filled_avg_price,
        type=type,
        client_order_id=client_order_id,
        replaces=replaces,
        replaced_by=replaced_by,
        qty="10",
        legs=legs or [],
    )


def _make_event(event: str, order: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(event=event, order=order)


def _persist_pending_exit():
    """Create the durable one-share exit saga used by stream-health tests."""
    from core.execution_workflow import (
        EntryExecutionPlan,
        create_entry_workflow,
        reset_workflow_state,
    )

    reset_workflow_state()
    plan = EntryExecutionPlan(
        symbol="NVDA",
        entry_price=500.0,
        price_source="test",
        stop_price=465.0,
        stop_loss_pct=0.07,
        position_value=500.0,
        risk_amount=35.0,
        risk_per_share=35.0,
        qty=1.0,
        canslim_score=80.0,
        rs_score=90.0,
        is_breakout=True,
        has_volume_surge=True,
    )
    workflow = create_entry_workflow(plan, signal_payload={"symbol": "NVDA"})
    workflow.mark_buy_fill(
        qty=1.0,
        fill_price=500.0,
        broker_order_id="entry-filled",
    )
    stop_client_order_id = f"{workflow.workflow_id}-sl-live"
    workflow.mark_protective_stop(
        success=True,
        stop_order_id="protective-stop",
        stop_price=465.0,
        action="submitted",
        stop_client_order_id=stop_client_order_id,
    )
    exit_client_order_id = f"{workflow.workflow_id}-exit"
    workflow.mark_exit_submission_intent(
        exit_reason="supervised verification cleanup",
        client_order_id=exit_client_order_id,
    )
    workflow.mark_exit_order_submitted(
        exit_reason="supervised verification cleanup",
        broker_order_id="market-exit",
    )
    return workflow, stop_client_order_id, exit_client_order_id


def _build_monitor_with_mock_stream():
    """Construct a FillMonitor where TradingStream is replaced by a mock."""
    registered_handlers: dict[str, list] = {"trade_updates": []}
    mock_stream = MagicMock(
        spec=["subscribe_trade_updates", "run", "stop", "_running"]
    )
    mock_stream._running = False
    mock_stream.subscribe_trade_updates.side_effect = (
        lambda handler: registered_handlers["trade_updates"].append(handler)
    )

    with patch("fill_monitor.TradingStream", return_value=mock_stream), \
         patch("fill_monitor.require_paper_mode"):
        from fill_monitor import FillMonitor
        monitor = FillMonitor()

    return monitor, mock_stream, registered_handlers


# ---------------------------------------------------------------------------
# Handler registration
# ---------------------------------------------------------------------------


class TestHandlerRegistration:
    def test_pinned_sdk_subscription_contract(self):
        signature = inspect.signature(TradingStream.subscribe_trade_updates)
        assert list(signature.parameters) == ["self", "handler"]

    def test_trade_updates_handler_registered(self):
        _, _, handlers = _build_monitor_with_mock_stream()
        assert "trade_updates" in handlers, "No handler registered for trade_updates"
        assert len(handlers["trade_updates"]) == 1
        assert inspect.iscoroutinefunction(handlers["trade_updates"][0])

    def test_trade_updates_handler_runs_dispatch_outside_event_loop_thread(self):
        monitor, _, handlers = _build_monitor_with_mock_stream()
        event = SimpleNamespace(sequence=1)
        caller_thread_id = threading.get_ident()
        dispatch_thread_ids: list[int] = []

        def record_dispatch(data):
            assert data is event
            dispatch_thread_ids.append(threading.get_ident())

        monitor._dispatch = record_dispatch

        asyncio.run(handlers["trade_updates"][0](event))

        assert len(dispatch_thread_ids) == 1
        assert dispatch_thread_ids[0] != caller_thread_id

    def test_trade_updates_handler_contains_fault_and_dispatches_next_event(
        self, capsys
    ):
        monitor, mock_stream, handlers = _build_monitor_with_mock_stream()
        first_event = SimpleNamespace(sequence=1)
        second_event = SimpleNamespace(sequence=2)
        dispatched_events = []

        monitor._running = True
        monitor._thread = MagicMock()
        monitor._thread.is_alive.return_value = True
        mock_stream._running = True
        assert monitor.is_connected() is True

        def fail_first_dispatch(data):
            dispatched_events.append(data)
            if data is first_event:
                raise RuntimeError("first dispatch failed")

        monitor._dispatch = fail_first_dispatch
        handler = handlers["trade_updates"][0]

        async def dispatch_both_events():
            await handler(first_event)
            await handler(second_event)

        asyncio.run(dispatch_both_events())

        assert dispatched_events == [first_event, second_event]
        assert "first dispatch failed" in capsys.readouterr().out
        assert monitor.is_connected() is False

    def test_post_dispatch_health_reconciliation_error_is_contained(self, capsys):
        monitor, mock_stream, handlers = _build_monitor_with_mock_stream()
        monitor._running = True
        monitor._thread = MagicMock()
        monitor._thread.is_alive.return_value = True
        mock_stream._running = True
        monitor._dispatch = MagicMock()
        monitor._clear_converged_exit_fault = MagicMock(
            side_effect=RuntimeError("health reconciliation failed")
        )

        asyncio.run(handlers["trade_updates"][0](SimpleNamespace(sequence=1)))

        assert "health reconciliation failed" in capsys.readouterr().out
        assert monitor.is_connected() is False

    def test_fill_monitor_constructed_with_paper_true(self):
        with patch("fill_monitor.TradingStream") as mock_cls, \
             patch("fill_monitor.require_paper_mode"):
            from fill_monitor import FillMonitor
            FillMonitor()

        mock_cls.assert_called_once()
        _, kwargs = mock_cls.call_args
        assert kwargs.get("paper") is True or mock_cls.call_args[0][2] is True

    def test_pending_exit_fault_clears_after_exact_sell_fill(self, tmp_path):
        from core.execution_store import get_execution_store
        from core.order_execution import ProtectiveStopResult
        from core.order_manager import OrderManager

        db_path = tmp_path / "pending-exit-stream.sqlite3"
        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            workflow, stop_client_order_id, exit_client_order_id = (
                _persist_pending_exit()
            )
            monitor, mock_stream, handlers = _build_monitor_with_mock_stream()
            monitor._order_manager = OrderManager(paper=True)
            monitor._running = True
            monitor._thread = MagicMock()
            monitor._thread.is_alive.return_value = True
            mock_stream._running = True
            lagged_exit = ProtectiveStopResult(
                success=False,
                order_id="",
                symbol="NVDA",
                qty=1.0,
                stop_price=465.0,
                action="exit_outcome_unresolved",
                error="exact exit is not visible yet",
            )
            stop_cancel = _make_event(
                "canceled",
                _make_order(
                    broker_order_id="protective-stop",
                    side="sell",
                    filled_qty="0",
                    filled_avg_price="0",
                    type="stop",
                    client_order_id=stop_client_order_id,
                ),
            )
            exit_fill = _make_event(
                "fill",
                _make_order(
                    broker_order_id="market-exit",
                    side="sell",
                    filled_qty="1",
                    filled_avg_price="499",
                    type="market",
                    client_order_id=exit_client_order_id,
                ),
            )
            handler = handlers["trade_updates"][0]

            with (
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=lagged_exit,
                ),
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    return_value=(None, []),
                ),
                patch("core.order_manager.notify_sell_filled", return_value=False),
            ):
                asyncio.run(handler(stop_cancel))
                assert monitor.is_connected() is False
                asyncio.run(handler(exit_fill))

            store = get_execution_store()
            snapshot = store.load_workflow(workflow.workflow_id)

        assert monitor.is_connected() is True
        assert store.load_active_position("NVDA") is None
        assert store.load_pending_submission_intents(symbol="NVDA") == []
        assert snapshot is not None
        assert "sell_fill_received" in {
            item["event"] for item in snapshot["transitions"]
        }

    def test_sell_fill_does_not_clear_fault_while_exit_intent_remains(self, tmp_path):
        db_path = tmp_path / "still-pending-exit-stream.sqlite3"
        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            workflow, _, exit_client_order_id = _persist_pending_exit()
            monitor, mock_stream, handlers = _build_monitor_with_mock_stream()
            monitor._running = True
            monitor._thread = MagicMock()
            monitor._thread.is_alive.return_value = True
            mock_stream._running = True
            monitor._pending_exit_faults.add(("NVDA", workflow.workflow_id))
            monitor._dispatch = MagicMock()
            exit_fill = _make_event(
                "fill",
                _make_order(
                    broker_order_id="market-exit",
                    side="sell",
                    filled_qty="1",
                    filled_avg_price="499",
                    type="market",
                    client_order_id=exit_client_order_id,
                ),
            )

            asyncio.run(handlers["trade_updates"][0](exit_fill))

        assert monitor.is_connected() is False

    def test_stop_cancel_without_pending_exit_remains_permanently_faulted(
        self,
        tmp_path,
    ):
        from core.order_execution import ProtectiveStopResult
        from core.order_manager import OrderManager

        db_path = tmp_path / "stop-cancel-without-exit.sqlite3"
        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            workflow, stop_client_order_id, exit_client_order_id = (
                _persist_pending_exit()
            )
            workflow.mark_submission_intent_resolved(
                role="exit",
                client_order_id=exit_client_order_id,
                outcome="definitive_failure",
            )
            monitor, mock_stream, handlers = _build_monitor_with_mock_stream()
            monitor._order_manager = OrderManager(paper=True)
            monitor._running = True
            monitor._thread = MagicMock()
            monitor._thread.is_alive.return_value = True
            mock_stream._running = True
            unproven_stop = ProtectiveStopResult(
                success=False,
                order_id="",
                symbol="NVDA",
                qty=1.0,
                stop_price=465.0,
                action="position_not_visible",
                error="position proof is unavailable",
            )
            stop_cancel = _make_event(
                "canceled",
                _make_order(
                    broker_order_id="protective-stop",
                    side="sell",
                    filled_qty="0",
                    filled_avg_price="0",
                    type="stop",
                    client_order_id=stop_client_order_id,
                ),
            )

            with patch(
                "core.order_manager.ensure_protective_stop",
                return_value=unproven_stop,
            ):
                asyncio.run(handlers["trade_updates"][0](stop_cancel))

        assert monitor.is_connected() is False
        assert monitor._handler_fault is True

    def test_canceled_market_exit_fault_remains_latched_after_late_fill(self, tmp_path):
        from core.execution_store import get_execution_store
        from core.order_execution import ProtectiveStopResult
        from core.order_manager import OrderManager

        db_path = tmp_path / "canceled-market-exit-stream.sqlite3"
        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            workflow, _, exit_client_order_id = _persist_pending_exit()
            monitor, mock_stream, handlers = _build_monitor_with_mock_stream()
            monitor._order_manager = OrderManager(paper=True)
            monitor._running = True
            monitor._thread = MagicMock()
            monitor._thread.is_alive.return_value = True
            mock_stream._running = True
            unproven_exit = ProtectiveStopResult(
                success=False,
                order_id="",
                symbol="NVDA",
                qty=1.0,
                stop_price=465.0,
                action="exit_outcome_unresolved",
                error="exact exit is not visible yet",
            )
            market_cancel = _make_event(
                "canceled",
                _make_order(
                    broker_order_id="market-exit",
                    side="sell",
                    filled_qty="0",
                    filled_avg_price="0",
                    type="market",
                    client_order_id=exit_client_order_id,
                ),
            )
            late_fill = _make_event(
                "fill",
                _make_order(
                    broker_order_id="market-exit",
                    side="sell",
                    filled_qty="1",
                    filled_avg_price="499",
                    type="market",
                    client_order_id=exit_client_order_id,
                ),
            )
            handler = handlers["trade_updates"][0]

            with (
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=unproven_exit,
                ),
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    return_value=(None, []),
                ),
                patch("core.order_manager.notify_sell_filled", return_value=False),
            ):
                asyncio.run(handler(market_cancel))
                asyncio.run(handler(late_fill))

            store = get_execution_store()
            snapshot = store.load_workflow(workflow.workflow_id)

        assert monitor.is_connected() is False
        assert store.load_active_position("NVDA") is None
        assert store.load_pending_submission_intents(symbol="NVDA") == []
        assert snapshot is not None
        assert "sell_fill_received" in {
            item["event"] for item in snapshot["transitions"]
        }

# ---------------------------------------------------------------------------
# Fill event → notify_buy_filled
# ---------------------------------------------------------------------------


class TestBuyFillDispatch:
    def test_buy_fill_delegates_to_order_manager(self):
        monitor, _, _ = _build_monitor_with_mock_stream()
        order = _make_order(
            side="buy",
            filled_qty="12",
            filled_avg_price="875.40",
            replaces="entry-parent",
            replaced_by="entry-child",
        )
        monitor._order_manager = MagicMock()

        monitor._dispatch(_make_event("fill", order))

        monitor._order_manager.handle_fill.assert_called_once_with(
            symbol="NVDA",
            broker_order_id="",
            client_order_id="test-order-1",
            side="buy",
            filled_qty=12.0,
            fill_price=875.40,
            order_type="limit",
            replaces="entry-parent",
            replaced_by="entry-child",
        )


# ---------------------------------------------------------------------------
# Fill event → notify_sell_filled
# ---------------------------------------------------------------------------


class TestSellFillDispatch:
    def test_sell_fill_delegates_to_order_manager(self):
        monitor, _, _ = _build_monitor_with_mock_stream()
        order = _make_order(
            symbol="CRWD",
            side="sell",
            filled_qty="8",
            filled_avg_price="350.00",
            replaces="exit-parent",
            replaced_by="exit-child",
        )
        monitor._order_manager = MagicMock()

        monitor._dispatch(_make_event("fill", order))

        monitor._order_manager.handle_fill.assert_called_once_with(
            symbol="CRWD",
            broker_order_id="",
            client_order_id="test-order-1",
            side="sell",
            filled_qty=8.0,
            fill_price=350.0,
            order_type="limit",
            replaces="exit-parent",
            replaced_by="exit-child",
        )


# ---------------------------------------------------------------------------
# Non-fill events
# ---------------------------------------------------------------------------


class TestNonFillEvents:
    def test_buy_partial_fill_delegates_to_order_manager(self):
        monitor, _, _ = _build_monitor_with_mock_stream()
        order = _make_order(
            side="buy",
            filled_qty="5",
            filled_avg_price="900.00",
            replaces="entry-parent",
            replaced_by="entry-child",
        )
        monitor._order_manager = MagicMock()

        monitor._dispatch(_make_event("partial_fill", order))
        monitor._order_manager.handle_partial_fill.assert_called_once_with(
            symbol="NVDA",
            broker_order_id="",
            client_order_id="test-order-1",
            side="buy",
            filled_qty=5.0,
            fill_price=900.0,
            order_type="limit",
            replaces="entry-parent",
            replaced_by="entry-child",
        )

    def test_sell_partial_fill_delegates_to_order_manager(self):
        monitor, _, _ = _build_monitor_with_mock_stream()
        order = _make_order(
            broker_order_id="sell-partial-1",
            side="sell",
            filled_qty="5",
            filled_avg_price="900.00",
            client_order_id="workflow-1-exit",
            replaces="exit-parent",
            replaced_by="exit-child",
        )
        monitor._order_manager = MagicMock()

        monitor._dispatch(_make_event("partial_fill", order))
        monitor._order_manager.handle_partial_fill.assert_called_once_with(
            symbol="NVDA",
            broker_order_id="sell-partial-1",
            client_order_id="workflow-1-exit",
            side="sell",
            filled_qty=5.0,
            fill_price=900.0,
            order_type="limit",
            replaces="exit-parent",
            replaced_by="exit-child",
        )

    @pytest.mark.parametrize(
        ("side", "filled_qty", "fill_price"),
        [("buy", "0", "0"), ("sell", "4", "875.25")],
    )
    @pytest.mark.parametrize(
        "event", ["canceled", "expired", "rejected", "replaced", "restated"]
    )
    def test_structural_event_delegates_to_safety_recovery(
        self, event, side, filled_qty, fill_price
    ):
        monitor, _, _ = _build_monitor_with_mock_stream()
        order = _make_order(
            broker_order_id="structural-order-1",
            side=side,
            filled_qty=filled_qty,
            filled_avg_price=fill_price,
            client_order_id="workflow-1-order",
            type="market",
            replaces="structural-parent",
            replaced_by="structural-child",
        )
        monitor._order_manager = MagicMock()

        monitor._dispatch(_make_event(event, order))

        monitor._order_manager.handle_order_failure.assert_called_once_with(
            symbol="NVDA",
            broker_order_id="structural-order-1",
            client_order_id="workflow-1-order",
            side=side,
            order_type="market",
            status=event,
            filled_qty=float(filled_qty),
            fill_price=float(fill_price),
            replaces="structural-parent",
            replaced_by="structural-child",
        )

    def test_new_event_does_not_delegate_to_order_manager(self):
        monitor, _, _ = _build_monitor_with_mock_stream()
        order = _make_order(side="buy")
        monitor._order_manager = MagicMock()

        monitor._dispatch(_make_event("new", order))
        monitor._order_manager.handle_fill.assert_not_called()

    def test_event_with_no_order_object_is_ignored(self):
        monitor, _, _ = _build_monitor_with_mock_stream()
        event = SimpleNamespace(event="fill", order=None)
        monitor._order_manager = MagicMock()

        monitor._dispatch(event)
        monitor._order_manager.handle_fill.assert_not_called()


# ---------------------------------------------------------------------------
# start / stop / is_running
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_is_running_false_before_start(self):
        monitor, _, _ = _build_monitor_with_mock_stream()
        assert monitor.is_running() is False

    def test_is_connected_requires_wrapper_thread_sdk_and_fault_free_handler(self):
        monitor, mock_stream, _ = _build_monitor_with_mock_stream()
        monitor._thread = MagicMock()
        monitor._thread.is_alive.return_value = True
        mock_stream._running = True

        assert monitor.is_connected() is False

        monitor._running = True
        monitor._thread.is_alive.return_value = False
        assert monitor.is_connected() is False

        monitor._thread.is_alive.return_value = True
        mock_stream._running = False
        assert monitor.is_connected() is False

        mock_stream._running = True
        assert monitor.is_connected() is True

        monitor._handler_fault = True
        assert monitor.is_connected() is False

    def test_start_launches_daemon_thread(self):
        monitor, mock_stream, _ = _build_monitor_with_mock_stream()
        mock_stream.run = MagicMock()  # prevent actual WebSocket connection

        with patch.object(monitor, "_run_stream"):
            monitor.start()
            assert monitor._running is True
            assert monitor._thread is not None
            assert monitor._thread.daemon is True

    def test_start_is_idempotent(self):
        monitor, mock_stream, _ = _build_monitor_with_mock_stream()
        mock_stream.run = MagicMock()

        with patch.object(monitor, "_run_stream"):
            monitor.start()
            thread_1 = monitor._thread
            monitor.start()  # second call — should be a no-op
            assert monitor._thread is thread_1

    def test_stop_returns_true_after_stream_worker_terminates(self):
        monitor, mock_stream, _ = _build_monitor_with_mock_stream()
        worker_release = threading.Event()

        def finish_worker():
            worker_release.wait()
            time.sleep(0.05)

        worker = threading.Thread(target=finish_worker, daemon=True)
        worker.start()
        mock_stream.stop.side_effect = worker_release.set
        monitor._thread = worker
        monitor._running = True

        try:
            stopped = monitor.stop()
        finally:
            worker_release.set()
            worker.join(timeout=1)

        mock_stream.stop.assert_called_once()
        assert stopped is True
        assert worker.is_alive() is False
        assert monitor._running is False

    def test_stop_returns_false_without_blocking_on_wedged_stream_stop(self):
        monitor, mock_stream, _ = _build_monitor_with_mock_stream()
        stop_release = threading.Event()
        stop_started = threading.Event()
        worker_release = threading.Event()
        worker = threading.Thread(target=worker_release.wait, daemon=True)
        worker.start()

        def wedged_stop():
            stop_started.set()
            stop_release.wait(timeout=1)

        mock_stream.stop.side_effect = wedged_stop
        monitor._thread = worker
        monitor._running = True

        started_at = time.monotonic()
        try:
            with patch("fill_monitor._STOP_JOIN_TIMEOUT_SECS", 0.05):
                stopped = monitor.stop()
            elapsed = time.monotonic() - started_at
        finally:
            stop_release.set()
            worker_release.set()
            worker.join(timeout=1)

        assert stop_started.is_set()
        assert elapsed < 0.4
        assert stopped is False
