"""Unit tests for fill_monitor.FillMonitor — mocked TradingStream."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_order(
    *,
    symbol: str = "NVDA",
    side: str = "buy",
    filled_qty: str = "10",
    filled_avg_price: str = "900.00",
    type: str = "limit",
    client_order_id: str = "test-order-1",
    legs: list | None = None,
) -> SimpleNamespace:
    """Build a minimal mock order object matching Alpaca's Order model."""
    return SimpleNamespace(
        symbol=symbol,
        side=side,
        filled_qty=filled_qty,
        filled_avg_price=filled_avg_price,
        type=type,
        client_order_id=client_order_id,
        qty="10",
        legs=legs or [],
    )


def _make_event(event: str, order: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(event=event, order=order)


def _build_monitor_with_mock_stream():
    """Construct a FillMonitor where TradingStream is replaced by a mock."""
    registered_handlers: dict[str, list] = {}

    mock_stream = MagicMock()

    def mock_on(event_name):
        def decorator(fn):
            registered_handlers.setdefault(event_name, []).append(fn)
            return fn
        return decorator

    mock_stream.on = mock_on

    with patch("fill_monitor.TradingStream", return_value=mock_stream), \
         patch("fill_monitor.require_paper_mode"):
        from fill_monitor import FillMonitor
        monitor = FillMonitor()

    return monitor, mock_stream, registered_handlers


# ---------------------------------------------------------------------------
# Handler registration
# ---------------------------------------------------------------------------


class TestHandlerRegistration:
    def test_trade_updates_handler_registered(self):
        _, _, handlers = _build_monitor_with_mock_stream()
        assert "trade_updates" in handlers, "No handler registered for trade_updates"
        assert len(handlers["trade_updates"]) == 1

    def test_fill_monitor_constructed_with_paper_true(self):
        with patch("fill_monitor.TradingStream") as mock_cls, \
             patch("fill_monitor.require_paper_mode"):
            mock_cls.return_value.on = lambda e: lambda fn: fn
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
        order = _make_order(side="buy", filled_qty="12", filled_avg_price="875.40")
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
        )


# ---------------------------------------------------------------------------
# Fill event → notify_sell_filled
# ---------------------------------------------------------------------------


class TestSellFillDispatch:
    def test_sell_fill_delegates_to_order_manager(self):
        monitor, _, _ = _build_monitor_with_mock_stream()
        order = _make_order(
            symbol="CRWD", side="sell", filled_qty="8", filled_avg_price="350.00"
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
        )


# ---------------------------------------------------------------------------
# Non-fill events
# ---------------------------------------------------------------------------


class TestNonFillEvents:
    def test_buy_partial_fill_delegates_to_order_manager(self):
        monitor, _, _ = _build_monitor_with_mock_stream()
        order = _make_order(side="buy", filled_qty="5", filled_avg_price="900.00")
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
        )

    def test_sell_partial_fill_does_not_delegate_to_order_manager(self):
        monitor, _, _ = _build_monitor_with_mock_stream()
        order = _make_order(side="sell", filled_qty="5", filled_avg_price="900.00")
        monitor._order_manager = MagicMock()

        monitor._dispatch(_make_event("partial_fill", order))
        monitor._order_manager.handle_partial_fill.assert_not_called()

    def test_canceled_event_does_not_delegate_to_order_manager(self):
        monitor, _, _ = _build_monitor_with_mock_stream()
        order = _make_order(side="buy")
        monitor._order_manager = MagicMock()

        monitor._dispatch(_make_event("canceled", order))
        monitor._order_manager.handle_fill.assert_not_called()

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

    def test_stop_calls_stream_stop(self):
        monitor, mock_stream, _ = _build_monitor_with_mock_stream()
        mock_stream.run = MagicMock()

        with patch.object(monitor, "_run_stream"):
            monitor.start()

        monitor.stop()
        mock_stream.stop.assert_called_once()
        assert monitor._running is False

    def test_stop_is_safe_when_not_running(self):
        monitor, mock_stream, _ = _build_monitor_with_mock_stream()
        # Should not raise even though we never called start()
        monitor.stop()
        mock_stream.stop.assert_not_called()
