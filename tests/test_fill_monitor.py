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

    def test_fill_monitor_constructed_with_paper_true(self):
        with patch("fill_monitor.TradingStream") as mock_cls, \
             patch("fill_monitor.require_paper_mode"):
            from fill_monitor import FillMonitor
            FillMonitor()

        mock_cls.assert_called_once()
        _, kwargs = mock_cls.call_args
        assert kwargs.get("paper") is True or mock_cls.call_args[0][2] is True

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
