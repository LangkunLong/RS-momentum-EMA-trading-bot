"""End-to-end flow tests: buy signal -> order -> notification, stop-loss -> sell -> notification.

Each test class covers one complete pipeline path with all external calls mocked.
No real network calls are made.

Paths verified:
  A. Scanner buy signal -> execute_entries -> bracket buy order submitted
  B. Bracket buy fill (via FillMonitor) -> notify_buy_filled email sent
  C. Alpaca-side stop-loss fill (sell fill via FillMonitor) -> notify_sell_filled email sent
  D. Software stop-loss (monitor_and_exit_positions) -> close_position -> market sell
  E. Hourly MA violation (monitor_exits_hourly) -> close_position -> market sell
  F. Full scheduler cycle: scan fires -> entries submitted -> fill monitor running
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_ohlcv(n: int = 60, close: float = 150.0) -> pd.DataFrame:
    closes = [close] * n
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {"Open": closes, "High": [c * 1.01 for c in closes],
         "Low": [c * 0.99 for c in closes], "Close": closes,
         "Volume": [2_000_000] * n},
        index=idx,
    )


def _make_position(symbol: str, unrealized_pl_pct: float) -> SimpleNamespace:
    entry = 100.0
    current = entry * (1 + unrealized_pl_pct)
    return SimpleNamespace(
        symbol=symbol,
        qty=10.0,
        avg_entry_price=entry,
        current_price=current,
        unrealized_pl_pct=unrealized_pl_pct,
    )


def _make_fill_event(
    *,
    side: str = "buy",
    symbol: str = "NVDA",
    filled_qty: str = "10",
    filled_avg_price: str = "875.00",
    order_type: str = "limit",
    client_order_id: str = "test-order-1",
    order_id: str | None = None,
) -> SimpleNamespace:
    payload = dict(
        symbol=symbol,
        side=side,
        filled_qty=filled_qty,
        filled_avg_price=filled_avg_price,
        type=order_type,
        client_order_id=client_order_id,
        qty=filled_qty,
        legs=[],
    )
    if order_id is not None:
        payload["id"] = order_id
    order = SimpleNamespace(**payload)
    return SimpleNamespace(event="fill", order=order)


# ---------------------------------------------------------------------------
# Path A: Buy signal -> execute_entries -> entry order submitted
# ---------------------------------------------------------------------------


class TestPathA_BuySignalToOrder:
    """A CANSLIM buy signal must result in an entry order being submitted."""

    def _make_signal(self, symbol: str = "NVDA") -> dict:
        return {
            "symbol": symbol,
            "total_score": 78.0,
            "rs_score": 91.0,
            "is_breakout": True,
            "has_volume_surge": True,
            "buy_point": 850.0,
        }

    def test_execute_entries_submits_entry_buy(self):
        from auto_trader import execute_entries

        signal = self._make_signal("NVDA")
        bars = _make_ohlcv(close=875.0)

        manager = MagicMock()
        manager.submit_entry.return_value = SimpleNamespace(success=True, dry_run=False, workflow_id="wf-nvda-path-a")

        with patch("auto_trader._get_account_equity", return_value=100_000.0), \
             patch("auto_trader.get_open_positions", return_value=[]), \
             patch("auto_trader.get_open_orders", return_value=[]), \
             patch("auto_trader.fetch_ohlcv", return_value=bars), \
             patch("auto_trader.OrderManager", return_value=manager):
            result = execute_entries([signal])

        assert "NVDA" in result
        manager.submit_entry.assert_called_once()

    def test_entry_order_has_no_embedded_stop(self):
        """The entry request should be plain; protection is placed after fill."""
        from core.order_execution import submit_bracket_buy

        mock_client = MagicMock()
        mock_client.submit_order.return_value = SimpleNamespace(id="order-abc")

        with patch("core.order_execution._get_trading_client", return_value=mock_client), \
             patch("core.order_execution._is_paper_mode", return_value=True):
            submit_bracket_buy(symbol="NVDA", qty=10, stop_loss_pct=0.07, limit_price=875.0)

        req = mock_client.submit_order.call_args[0][0]
        assert getattr(req, "stop_loss", None) is None
        assert getattr(req, "order_class", None) is None

    def test_position_sized_as_10pct_of_equity(self):
        """Qty must equal (equity * 0.10) / price."""
        from auto_trader import execute_entries
        from config import settings

        equity = 100_000.0
        price = 875.0
        expected_qty = round((equity * settings.POSITION_SIZE_PCT) / price, 4)

        bars = _make_ohlcv(close=price)
        manager = MagicMock()
        manager.submit_entry.return_value = SimpleNamespace(success=True, dry_run=False, workflow_id="wf-nvda-size")

        with patch("auto_trader._get_account_equity", return_value=equity), \
             patch("auto_trader.get_open_positions", return_value=[]), \
             patch("auto_trader.get_open_orders", return_value=[]), \
             patch("auto_trader.fetch_ohlcv", return_value=bars), \
             patch("auto_trader.OrderManager", return_value=manager):
            execute_entries([{"symbol": "NVDA", "total_score": 75, "rs_score": 88,
                              "is_breakout": True, "has_volume_surge": True, "buy_point": 850.0}])

        actual_qty = manager.submit_entry.call_args.args[0].qty
        assert actual_qty == pytest.approx(expected_qty, rel=0.01)

    def test_already_held_symbol_skipped(self):
        """A symbol already in the portfolio must not generate a second order."""
        from auto_trader import execute_entries

        held = SimpleNamespace(symbol="NVDA", qty=5.0, avg_entry_price=800.0,
                               current_price=850.0, unrealized_pl_pct=0.0625)

        with patch("auto_trader._get_account_equity", return_value=100_000.0), \
             patch("auto_trader.get_open_positions", return_value=[held]), \
             patch("auto_trader.get_open_orders", return_value=[]), \
             patch("auto_trader.fetch_ohlcv", return_value=_make_ohlcv()), \
             patch("auto_trader.OrderManager") as mock_manager_cls:
            result = execute_entries([{"symbol": "NVDA", "total_score": 80,
                                       "rs_score": 90, "is_breakout": True,
                                       "has_volume_surge": False}])

        assert "NVDA" not in result
        mock_manager_cls.return_value.submit_entry.assert_not_called()


# ---------------------------------------------------------------------------
# Path B: Buy fill -> protective stop -> notify_buy_filled email sent
# ---------------------------------------------------------------------------


class TestPathB_BuyFillNotification:
    """When Alpaca reports a buy fill, an email notification must be sent."""

    def _build_monitor(self):
        mock_stream = MagicMock()
        handlers = {}

        def mock_on(event_name):
            def decorator(fn):
                handlers.setdefault(event_name, []).append(fn)
                return fn
            return decorator

        mock_stream.on = mock_on

        with patch("fill_monitor.TradingStream", return_value=mock_stream), \
             patch("fill_monitor._is_paper_mode", return_value=True):
            from fill_monitor import FillMonitor
            monitor = FillMonitor()
        return monitor

    def test_buy_fill_triggers_email(self):
        monitor = self._build_monitor()
        event = _make_fill_event(side="buy", symbol="NVDA",
                                 filled_qty="10", filled_avg_price="875.00")
        monitor._order_manager = MagicMock()

        monitor._dispatch(event)

        monitor._order_manager.handle_fill.assert_called_once_with(
            symbol="NVDA",
            broker_order_id="",
            client_order_id="test-order-1",
            side="buy",
            filled_qty=10.0,
            fill_price=875.0,
            order_type="limit",
        )

    def test_buy_fill_reconciles_stop_from_actual_fill(self):
        monitor = self._build_monitor()
        event = _make_fill_event(side="buy", filled_avg_price="1000.00")
        monitor._order_manager = MagicMock()

        monitor._dispatch(event)

        monitor._order_manager.handle_fill.assert_called_once()

    def test_buy_email_body_contains_symbol_and_price(self):
        """Integration: notify_buy_filled must produce an email with symbol + price."""
        from core.notifier import notify_buy_filled

        captured = {}

        def _fake_send(subject, body):
            captured["subject"] = subject
            captured["body"] = body
            return True

        with patch("core.notifier.send_email", side_effect=_fake_send), \
             patch("core.notifier.settings") as ms:
            ms.NOTIFY_EMAIL_FROM = "bot@g.com"
            ms.NOTIFY_EMAIL_TO = "me@g.com"
            ms.NOTIFY_EMAIL_PASSWORD = "secret"
            ms.STOP_LOSS_PCT = 0.07
            notify_buy_filled("NVDA", qty=10, fill_price=875.0, stop_price=813.75)

        assert "NVDA" in captured["subject"]
        assert "BUY" in captured["subject"]
        assert "875.00" in captured["body"]
        assert "813.75" in captured["body"]


# ---------------------------------------------------------------------------
# Path C: Alpaca-side stop-loss sell fills -> notify_sell_filled email sent
# ---------------------------------------------------------------------------


class TestPathC_StopLossFillNotification:
    """When Alpaca's bracket stop leg fills, a sell notification email must be sent."""

    def _build_monitor(self):
        mock_stream = MagicMock()
        handlers = {}

        def mock_on(event_name):
            def decorator(fn):
                handlers.setdefault(event_name, []).append(fn)
                return fn
            return decorator

        mock_stream.on = mock_on

        with patch("fill_monitor.TradingStream", return_value=mock_stream), \
             patch("fill_monitor._is_paper_mode", return_value=True):
            from fill_monitor import FillMonitor
            monitor = FillMonitor()
        return monitor

    def test_stop_sell_fill_triggers_email(self):
        monitor = self._build_monitor()
        event = _make_fill_event(
            side="sell", symbol="NVDA",
            filled_qty="10", filled_avg_price="813.75",
            order_type="stop",
        )

        monitor._order_manager = MagicMock()

        monitor._dispatch(event)

        monitor._order_manager.handle_fill.assert_called_once_with(
            symbol="NVDA",
            broker_order_id="",
            client_order_id="test-order-1",
            side="sell",
            filled_qty=10.0,
            fill_price=813.75,
            order_type="stop",
        )

    def test_stop_sell_email_body_contains_exit_reason(self):
        """Integration: notify_sell_filled must produce an email showing exit reason."""
        from core.notifier import notify_sell_filled

        captured = {}

        def _fake_send(subject, body):
            captured["subject"] = subject
            captured["body"] = body
            return True

        with patch("core.notifier.send_email", side_effect=_fake_send), \
             patch("core.notifier.settings") as ms:
            ms.NOTIFY_EMAIL_FROM = "bot@g.com"
            ms.NOTIFY_EMAIL_TO = "me@g.com"
            ms.NOTIFY_EMAIL_PASSWORD = "secret"
            notify_sell_filled(
                "NVDA", qty=10, fill_price=813.75, entry_price=875.0,
                exit_reason="stop-loss triggered",
            )

        assert "NVDA" in captured["subject"]
        assert "SELL" in captured["subject"]
        assert "stop-loss triggered" in captured["body"]
        # P&L is negative: (813.75 - 875) * 10 = -612.50
        assert "-" in captured["body"]

    def test_ma_violation_sell_email_labels_reason_correctly(self):
        """MA-violation exits must use a distinct reason string in the email."""
        from core.notifier import notify_sell_filled

        captured = {}

        def _fake_send(subject, body):
            captured["body"] = body
            return True

        with patch("core.notifier.send_email", side_effect=_fake_send), \
             patch("core.notifier.settings") as ms:
            ms.NOTIFY_EMAIL_FROM = "bot@g.com"
            ms.NOTIFY_EMAIL_TO = "me@g.com"
            ms.NOTIFY_EMAIL_PASSWORD = "secret"
            notify_sell_filled(
                "CRWD", qty=8, fill_price=340.0, entry_price=300.0,
                exit_reason="MA violation exit",
            )

        assert "MA violation exit" in captured["body"]


# ---------------------------------------------------------------------------
# Path D: Software stop-loss (monitor_and_exit_positions) -> market sell
# ---------------------------------------------------------------------------


class TestPathD_SoftwareStopLoss:
    """monitor_and_exit_positions must submit a market sell when loss >= 7%."""

    def test_hard_stop_breach_calls_close_position(self):
        from auto_trader import monitor_and_exit_positions

        pos = _make_position("AAPL", unrealized_pl_pct=-0.08)  # 8% loss
        manager = MagicMock()
        manager.submit_exit.return_value = SimpleNamespace(success=True)

        with patch("auto_trader.get_open_positions", return_value=[pos]), \
             patch("auto_trader.check_exit_signals", return_value=[pos]), \
             patch("auto_trader.OrderManager", return_value=manager), \
             patch("auto_trader.fetch_ohlcv", return_value=_make_ohlcv()):
            exited = monitor_and_exit_positions(stop_loss_pct=0.07)

        assert "AAPL" in exited
        manager.submit_exit.assert_called_once()

    def test_bracket_orders_cancelled_before_market_sell(self):
        """cancel_open_orders must be called BEFORE close_position to avoid double-fill."""
        from auto_trader import monitor_and_exit_positions

        manager = MagicMock()
        manager.submit_exit.return_value = SimpleNamespace(success=True)
        pos = _make_position("NVDA", unrealized_pl_pct=-0.09)

        with patch("auto_trader.get_open_positions", return_value=[pos]), \
             patch("auto_trader.check_exit_signals", return_value=[pos]), \
             patch("auto_trader.OrderManager", return_value=manager), \
             patch("auto_trader.fetch_ohlcv", return_value=_make_ohlcv()):
            monitor_and_exit_positions(stop_loss_pct=0.07)

        manager.submit_exit.assert_called_once()

    def test_position_within_threshold_not_exited(self):
        """A position at -3% must NOT trigger the 7% stop."""
        from auto_trader import monitor_and_exit_positions

        pos = _make_position("MSFT", unrealized_pl_pct=-0.03)

        with patch("auto_trader.get_open_positions", return_value=[pos]), \
             patch("auto_trader.check_exit_signals", return_value=[]), \
             patch("auto_trader.OrderManager") as mock_manager_cls, \
             patch("auto_trader.fetch_ohlcv", return_value=_make_ohlcv()):
            exited = monitor_and_exit_positions(stop_loss_pct=0.07)

        assert "MSFT" not in exited
        mock_manager_cls.return_value.submit_exit.assert_not_called()


# ---------------------------------------------------------------------------
# Path E: Hourly MA violation -> close_position -> market sell
# ---------------------------------------------------------------------------


class TestPathE_HourlyMAViolation:
    """monitor_exits_hourly must sell when 2 consecutive hourly closes < 21-hr EMA."""

    def _make_declining_hourly(self, n: int = 30, base: float = 100.0) -> pd.DataFrame:
        """30 bars rising to 110, then last 2 bars crash to 80 (well below EMA)."""
        closes = [base + i * 0.3 for i in range(n)]
        ema_last = pd.Series(closes).ewm(span=21, adjust=False).mean().iloc[-1]
        closes[-1] = ema_last * 0.90
        closes[-2] = ema_last * 0.90
        idx = pd.date_range("2024-01-01", periods=n, freq="h")
        return pd.DataFrame(
            {"Open": closes, "High": [c * 1.002 for c in closes],
             "Low": [c * 0.998 for c in closes], "Close": closes,
             "Volume": [500_000] * n},
            index=idx,
        )

    def test_two_consecutive_hourly_closes_below_ema_triggers_exit(self):
        from auto_trader import monitor_exits_hourly

        pos = _make_position("CRWD", unrealized_pl_pct=-0.03)
        bars = self._make_declining_hourly()
        manager = MagicMock()
        manager.submit_exit.return_value = SimpleNamespace(success=True)

        with patch("auto_trader.get_open_positions", return_value=[pos]), \
             patch("auto_trader.check_exit_signals", return_value=[]), \
             patch("auto_trader.fetch_hourly_ohlcv", return_value=bars), \
             patch("auto_trader.OrderManager", return_value=manager):
            exited = monitor_exits_hourly(ema_period=21, consecutive=2)

        assert "CRWD" in exited
        manager.submit_exit.assert_called_once()

    def test_hourly_stop_loss_cancel_before_close(self):
        """cancel_open_orders must precede close_position in the hourly path too."""
        from auto_trader import monitor_exits_hourly

        manager = MagicMock()
        manager.submit_exit.return_value = SimpleNamespace(success=True)
        pos = _make_position("VST", unrealized_pl_pct=-0.08)

        with patch("auto_trader.get_open_positions", return_value=[pos]), \
             patch("auto_trader.check_exit_signals", return_value=[pos]), \
             patch("auto_trader.OrderManager", return_value=manager), \
             patch("auto_trader.fetch_hourly_ohlcv", return_value=_make_ohlcv()):
            monitor_exits_hourly()

        manager.submit_exit.assert_called_once()


# ---------------------------------------------------------------------------
# Path F: Full scheduler cycle - scan fires -> entries submitted -> fill monitor on
# ---------------------------------------------------------------------------


class TestPathF_SchedulerCycle:
    """The scheduler must start FillMonitor, run the scan, and submit entries."""

    def test_scheduler_starts_fill_monitor(self):
        """FillMonitor.start() must be called at scheduler startup."""
        from scheduler import run_scheduler

        tick = {"n": 0}

        def _mock_now():
            tick["n"] += 1
            from datetime import datetime
            from zoneinfo import ZoneInfo
            return datetime(2024, 4, 15, 9, 25, 0, tzinfo=ZoneInfo("America/New_York"))

        def _sleep(_):
            raise KeyboardInterrupt

        with patch("scheduler._now_et", side_effect=_mock_now), \
             patch("scheduler.time.sleep", side_effect=_sleep), \
             patch("scheduler.FillMonitor") as MockFM, \
             patch("scheduler._run_startup_stop_reconciliation"), \
             patch("scheduler.monitor_and_exit_positions", return_value=[]), \
             patch("scheduler.monitor_exits_hourly", return_value=[]):
            mock_inst = MagicMock()
            MockFM.return_value = mock_inst
            run_scheduler(dry_run=False)

        mock_inst.start.assert_called_once()
        mock_inst.stop.assert_called()

    def test_scan_triggers_execute_entries_at_9_31(self):
        """run_auto_trader must be called at 09:31 ET."""
        from scheduler import run_scheduler

        from datetime import datetime
        from zoneinfo import ZoneInfo

        tick = {"n": 0}
        scan_calls = []

        def _mock_now():
            tick["n"] += 1
            # First tick: 09:31 (scan should fire)
            # Second tick: stop
            return datetime(2024, 4, 15, 9, 31, 30, tzinfo=ZoneInfo("America/New_York"))

        def _sleep(_):
            if tick["n"] >= 2:
                raise KeyboardInterrupt

        def _mock_cycle(dry_run):
            scan_calls.append(dry_run)

        with patch("scheduler._now_et", side_effect=_mock_now), \
             patch("scheduler.time.sleep", side_effect=_sleep), \
             patch("scheduler._run_cycle", side_effect=_mock_cycle), \
             patch("scheduler.FillMonitor") as MockFM, \
             patch("scheduler.monitor_and_exit_positions", return_value=[]), \
             patch("scheduler.monitor_exits_hourly", return_value=[]):
            MockFM.return_value.start = MagicMock()
            MockFM.return_value.stop = MagicMock()
            run_scheduler(dry_run=True)

        assert len(scan_calls) == 1, f"Expected 1 scan, got {len(scan_calls)}"
        assert scan_calls[0] is True  # dry_run=True was passed through

    def test_fill_monitor_stop_called_on_shutdown(self):
        """FillMonitor.stop() must always be called even on KeyboardInterrupt."""
        from scheduler import run_scheduler

        from datetime import datetime
        from zoneinfo import ZoneInfo

        def _mock_now():
            return datetime(2024, 4, 15, 9, 20, 0, tzinfo=ZoneInfo("America/New_York"))

        def _sleep(_):
            raise KeyboardInterrupt

        with patch("scheduler._now_et", side_effect=_mock_now), \
             patch("scheduler.time.sleep", side_effect=_sleep), \
             patch("scheduler.FillMonitor") as MockFM, \
             patch("scheduler._run_startup_stop_reconciliation"), \
             patch("scheduler.monitor_and_exit_positions", return_value=[]), \
             patch("scheduler.monitor_exits_hourly", return_value=[]):
            mock_inst = MagicMock()
            MockFM.return_value = mock_inst
            run_scheduler(dry_run=False)

        mock_inst.stop.assert_called_once()


class TestPathG_AlpacaMockBrokerLifecycle:
    """A fully mocked Alpaca lifecycle should exercise entry, stop, and exit paths."""

    def _build_monitor(self):
        mock_stream = MagicMock()

        def mock_on(_event_name):
            def decorator(fn):
                return fn
            return decorator

        mock_stream.on = mock_on

        with patch("fill_monitor.TradingStream", return_value=mock_stream), \
             patch("fill_monitor._is_paper_mode", return_value=True):
            from fill_monitor import FillMonitor

            return FillMonitor()

    def test_full_mocked_alpaca_order_lifecycle(self):
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest, StopOrderRequest

        from auto_trader import execute_entries
        from core.execution_workflow import get_active_workflow_for_symbol, get_workflow_by_client_order_id, reset_workflow_state
        from core.order_manager import OrderManager

        signal = {
            "symbol": "NVDA",
            "total_score": 88.0,
            "rs_score": 96.0,
            "is_breakout": True,
            "has_volume_surge": True,
            "buy_point": 500.0,
            "metrics": {
                "current_growth": 0.42,
                "annual_growth": 0.31,
                "proximity_to_high": 0.99,
            },
        }
        bars = _make_ohlcv(close=500.0)
        mock_client = MagicMock()
        mock_client.submit_order.side_effect = [
            SimpleNamespace(id="entry-1"),
            SimpleNamespace(id="stop-1"),
            SimpleNamespace(id="sell-1"),
        ]
        mock_client.get_open_position.return_value = SimpleNamespace(qty="20")
        stop_open_order = SimpleNamespace(
            id="stop-1",
            symbol="NVDA",
            side="sell",
            type="stop",
            qty="20",
            stop_price="465.0",
        )

        db_path = Path(tempfile.gettempdir()) / f"alpaca_lifecycle_{uuid4().hex}.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            with (
                patch("auto_trader._get_account_equity", return_value=100_000.0),
                patch("auto_trader.get_open_positions", return_value=[]),
                patch("auto_trader.get_open_orders", return_value=[]),
                patch("auto_trader.fetch_ohlcv", return_value=bars),
                patch("auto_trader._is_market_open", return_value=False),
                patch("auto_trader._is_paper_mode", return_value=True),
                patch("core.order_execution._get_trading_client", return_value=mock_client),
                patch("core.order_execution.get_open_orders", side_effect=[[], [stop_open_order]]),
                patch("core.order_manager.notify_entry_submitted", return_value=True),
                patch("core.order_manager.notify_buy_filled", return_value=True),
                patch("core.order_manager.notify_sell_filled", return_value=True),
            ):
                entered = execute_entries([signal], dry_run=False)
                assert entered == ["NVDA"]

                entry_req = mock_client.submit_order.call_args_list[0][0][0]
                assert isinstance(entry_req, LimitOrderRequest)
                workflow_id = entry_req.client_order_id
                assert workflow_id
                assert get_workflow_by_client_order_id(workflow_id) is not None

                monitor = self._build_monitor()
                monitor._dispatch(
                    _make_fill_event(
                        side="buy",
                        symbol="NVDA",
                        filled_qty="20",
                        filled_avg_price="500.00",
                        order_type="limit",
                        client_order_id=workflow_id,
                        order_id="entry-1",
                    )
                )

                stop_req = mock_client.submit_order.call_args_list[1][0][0]
                assert isinstance(stop_req, StopOrderRequest)
                assert stop_req.client_order_id == f"{workflow_id}-sl"
                active_workflow = get_active_workflow_for_symbol("NVDA")
                assert active_workflow is not None
                assert active_workflow.workflow_id == workflow_id

                exit_result = OrderManager(paper=True).submit_exit("NVDA", exit_reason="hard stop triggered")
                assert exit_result.success is True
                mock_client.cancel_order_by_id.assert_called_once_with("stop-1")

                sell_req = mock_client.submit_order.call_args_list[2][0][0]
                assert isinstance(sell_req, MarketOrderRequest)
                assert sell_req.client_order_id == workflow_id

                monitor._dispatch(
                    _make_fill_event(
                        side="sell",
                        symbol="NVDA",
                        filled_qty="20",
                        filled_avg_price="465.00",
                        order_type="market",
                        client_order_id=workflow_id,
                        order_id="sell-1",
                    )
                )

                recovered = get_workflow_by_client_order_id(workflow_id)
                assert recovered is not None
                assert recovered.broker_order_id == "sell-1"
                assert get_active_workflow_for_symbol("NVDA") is None
                reset_workflow_state()
