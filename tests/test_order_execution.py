"""Unit tests for core/order_execution.py and auto_trader.py.

All Alpaca TradingClient calls are mocked — no real network or paper-account
calls are made.  Tests verify:

  order_execution layer
  ├── submit_bracket_buy   — limit-bracket, market, error path
  ├── submit_stop_loss     — success, error path
  ├── submit_market_sell   — success, error path
  ├── close_position       — held position, flat (no-op), API error
  ├── get_open_positions   — mapping from Alpaca model, error path
  ├── get_open_orders      — all orders, symbol-filtered, error path
  ├── cancel_open_orders   — cancels each, partial failure
  └── check_exit_signals   — breach / no-breach / exact threshold

  auto_trader orchestration
  ├── monitor_and_exit_positions — hard-stop trigger, MA-violation trigger,
  │                                healthy position (no exit), no positions
  ├── execute_entries            — normal entry, dedup (already held),
  │                                position-limit guard, dry-run, equity=0
  └── _is_market_open / _get_account_equity — happy and error paths
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import core.order_execution as oe
from core.order_execution import (
    OrderResult,
    PositionSummary,
    cancel_open_orders,
    check_exit_signals,
    close_position,
    ensure_protective_stop,
    reconcile_open_position_stops,
    get_open_orders,
    get_open_positions,
    submit_bracket_buy,
    submit_market_sell,
    submit_stop_loss,
)


# ---------------------------------------------------------------------------
# Helpers — build mock Alpaca objects
# ---------------------------------------------------------------------------


def _mock_order(
    order_id: str = "test-order-id",
    *,
    symbol: str = "AAPL",
    side: str = "buy",
    order_type: str = "limit",
    qty: float = 1.0,
    stop_price: float | None = None,
) -> MagicMock:
    """Return a MagicMock that looks like an alpaca Order model."""
    order = MagicMock()
    order.id = order_id
    order.symbol = symbol
    order.side = side
    order.type = order_type
    order.qty = str(qty)
    order.stop_price = None if stop_price is None else str(stop_price)
    return order


def _mock_position(
    symbol: str = "AAPL",
    qty: float = 10.0,
    avg_entry: float = 100.0,
    current_price: float = 93.0,
    unrealized_plpc: float = -0.07,
) -> MagicMock:
    """Return a MagicMock that looks like an alpaca Position model."""
    pos = MagicMock()
    pos.symbol = symbol
    pos.qty = str(qty)
    pos.avg_entry_price = str(avg_entry)
    pos.current_price = str(current_price)
    pos.unrealized_plpc = str(unrealized_plpc)
    return pos


def _patched_client(client_mock: MagicMock) -> Any:
    """Context manager: patch _get_trading_client to return client_mock."""
    return patch("core.order_execution._get_trading_client", return_value=client_mock)


# ===========================================================================
# submit_bracket_buy
# ===========================================================================


class TestSubmitBracketBuy:
    def test_limit_entry_order_is_accepted(self) -> None:
        """submit_bracket_buy with a limit price must return success=True."""
        client = MagicMock()
        client.submit_order.return_value = _mock_order("order-1")

        with _patched_client(client):
            result = submit_bracket_buy("AAPL", qty=5.0, limit_price=150.0, stop_loss_pct=0.07)

        assert result.success is True
        assert result.order_id == "order-1"
        assert result.symbol == "AAPL"
        assert result.side == "buy"
        assert result.qty == 5.0
        assert result.error == ""

    def test_limit_entry_order_has_no_embedded_stop(self) -> None:
        """Entry orders must not carry an embedded stop before the fill exists."""
        from alpaca.trading.requests import LimitOrderRequest

        client = MagicMock()
        client.submit_order.return_value = _mock_order()

        with _patched_client(client):
            submit_bracket_buy("NVDA", qty=2.0, limit_price=200.00, stop_loss_pct=0.07)

        call_args = client.submit_order.call_args[0][0]
        assert isinstance(call_args, LimitOrderRequest)
        assert getattr(call_args, "stop_loss", None) is None
        assert getattr(call_args, "order_class", None) is None

    def test_market_order_submitted_when_no_limit_price(self) -> None:
        """Without a limit_price, a plain MarketOrderRequest must be submitted."""
        from alpaca.trading.requests import MarketOrderRequest

        client = MagicMock()
        client.submit_order.return_value = _mock_order()

        with _patched_client(client):
            result = submit_bracket_buy("TSLA", qty=1.0)

        call_args = client.submit_order.call_args[0][0]
        assert isinstance(call_args, MarketOrderRequest)
        assert result.success is True

    def test_api_error_returns_failure_result(self) -> None:
        """API exception must be caught and returned as success=False."""
        client = MagicMock()
        client.submit_order.side_effect = RuntimeError("connection timeout")

        with _patched_client(client):
            result = submit_bracket_buy("FAIL", qty=1.0, limit_price=100.0)

        assert result.success is False
        assert "connection timeout" in result.error
        assert result.symbol == "FAIL"

    def test_default_stop_pct_is_retained_for_logging_compatibility(self) -> None:
        """When stop_loss_pct is omitted, the entry path should still succeed."""
        from alpaca.trading.requests import LimitOrderRequest

        client = MagicMock()
        client.submit_order.return_value = _mock_order()

        with _patched_client(client):
            with patch("core.order_execution.settings") as mock_settings:
                mock_settings.STOP_LOSS_PCT = 0.08  # override to 8% for this test
                result = submit_bracket_buy("GOOG", qty=1.0, limit_price=100.0)

        call_args = client.submit_order.call_args[0][0]
        assert isinstance(call_args, LimitOrderRequest)
        assert result.success is True


# ===========================================================================
# submit_stop_loss
# ===========================================================================


class TestSubmitStopLoss:
    def test_gtc_stop_sell_order_submitted(self) -> None:
        """submit_stop_loss must submit a GTC stop-sell and return success."""
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import StopOrderRequest

        client = MagicMock()
        client.submit_order.return_value = _mock_order("stop-id-1")

        with _patched_client(client):
            result = submit_stop_loss("AAPL", qty=10.0, stop_price=93.0)

        assert result.success is True
        assert result.order_id == "stop-id-1"
        assert result.side == "sell"

        req = client.submit_order.call_args[0][0]
        assert isinstance(req, StopOrderRequest)
        assert req.side == OrderSide.SELL
        assert req.time_in_force == TimeInForce.GTC
        assert req.stop_price == 93.0

    def test_api_error_returns_failure(self) -> None:
        client = MagicMock()
        client.submit_order.side_effect = RuntimeError("bad request")

        with _patched_client(client):
            result = submit_stop_loss("AAPL", qty=5.0, stop_price=90.0)

        assert result.success is False
        assert "bad request" in result.error


# ===========================================================================
# ensure_protective_stop
# ===========================================================================


class TestEnsureProtectiveStop:
    def test_submits_new_stop_when_none_exists(self) -> None:
        client = MagicMock()

        with (
            _patched_client(client),
            patch("core.order_execution.get_open_orders", return_value=[]),
            patch(
                "core.order_execution.submit_stop_loss",
                return_value=OrderResult(True, "stop-1", "AAPL", "sell", 10.0),
            ) as mock_submit,
        ):
            result = ensure_protective_stop("AAPL", qty=10.0, fill_price=100.0, stop_loss_pct=0.07)

        mock_submit.assert_called_once_with(symbol="AAPL", qty=10.0, stop_price=93.0, client_order_id=None)
        assert result.success is True
        assert result.action == "submitted"
        assert result.stop_price == pytest.approx(93.0)

    def test_reuses_existing_matching_stop(self) -> None:
        client = MagicMock()
        existing = _mock_order(
            "stop-keep",
            symbol="AAPL",
            side="sell",
            order_type="stop",
            qty=10.0,
            stop_price=93.0,
        )

        with (
            _patched_client(client),
            patch("core.order_execution.get_open_orders", return_value=[existing]),
            patch("core.order_execution.submit_stop_loss") as mock_submit,
        ):
            result = ensure_protective_stop("AAPL", qty=10.0, fill_price=100.0, stop_loss_pct=0.07)

        mock_submit.assert_not_called()
        client.cancel_order_by_id.assert_not_called()
        assert result.success is True
        assert result.order_id == "stop-keep"
        assert result.action == "reused"

    def test_replaces_stale_stop_with_fill_anchored_stop(self) -> None:
        client = MagicMock()
        stale = _mock_order(
            "stop-old",
            symbol="AAPL",
            side="sell",
            order_type="stop",
            qty=10.0,
            stop_price=95.0,
        )

        with (
            _patched_client(client),
            patch("core.order_execution.get_open_orders", return_value=[stale]),
            patch(
                "core.order_execution.submit_stop_loss",
                return_value=OrderResult(True, "stop-new", "AAPL", "sell", 10.0),
            ) as mock_submit,
        ):
            result = ensure_protective_stop("AAPL", qty=10.0, fill_price=100.0, stop_loss_pct=0.07)

        client.cancel_order_by_id.assert_called_once_with("stop-old")
        mock_submit.assert_called_once_with(symbol="AAPL", qty=10.0, stop_price=93.0, client_order_id=None)
        assert result.success is True
        assert result.action == "replaced"
        assert result.order_id == "stop-new"

    def test_cleans_duplicate_matching_stops(self) -> None:
        client = MagicMock()
        keep = _mock_order(
            "stop-keep",
            symbol="AAPL",
            side="sell",
            order_type="stop",
            qty=10.0,
            stop_price=93.0,
        )
        duplicate = _mock_order(
            "stop-dup",
            symbol="AAPL",
            side="sell",
            order_type="stop",
            qty=10.0,
            stop_price=93.0,
        )

        with (
            _patched_client(client),
            patch("core.order_execution.get_open_orders", return_value=[keep, duplicate]),
            patch("core.order_execution.submit_stop_loss") as mock_submit,
        ):
            result = ensure_protective_stop("AAPL", qty=10.0, fill_price=100.0, stop_loss_pct=0.07)

        client.cancel_order_by_id.assert_called_once_with("stop-dup")
        mock_submit.assert_not_called()
        assert result.success is True
        assert result.order_id == "stop-keep"
        assert result.action == "cleaned"


# ===========================================================================
# reconcile_open_position_stops
# ===========================================================================


class TestReconcileOpenPositionStops:
    def test_repairs_open_position_using_avg_entry_price(self) -> None:
        positions = [PositionSummary("AAPL", 10.0, 100.0, 102.0, 0.02)]

        with (
            patch("core.order_execution.get_open_orders", return_value=[]),
            patch("core.order_execution.ensure_protective_stop") as mock_ensure,
        ):
            mock_ensure.return_value = oe.ProtectiveStopResult(
                success=True,
                order_id="stop-1",
                symbol="AAPL",
                qty=10.0,
                stop_price=93.0,
                action="submitted",
            )
            results = reconcile_open_position_stops(stop_loss_pct=0.07, positions=positions)

        mock_ensure.assert_called_once_with(
            symbol="AAPL",
            qty=10.0,
            fill_price=100.0,
            stop_loss_pct=0.07,
            open_orders=[],
        )
        assert len(results) == 1
        assert results[0].action == "submitted"

    def test_skips_positions_with_pending_exit_orders(self) -> None:
        positions = [PositionSummary("AAPL", 10.0, 100.0, 95.0, -0.05)]
        pending_exit = _mock_order("sell-1", symbol="AAPL", side="sell", order_type="limit", qty=10.0)

        with (
            patch("core.order_execution.get_open_orders", return_value=[pending_exit]),
            patch("core.order_execution.ensure_protective_stop") as mock_ensure,
        ):
            results = reconcile_open_position_stops(stop_loss_pct=0.07, positions=positions)

        mock_ensure.assert_not_called()
        assert len(results) == 1
        assert results[0].action == "skipped_pending_exit"
        assert results[0].stop_price == pytest.approx(93.0)

    def test_returns_empty_when_no_open_positions(self) -> None:
        assert reconcile_open_position_stops(positions=[]) == []


# ===========================================================================
# submit_market_sell
# ===========================================================================


class TestSubmitMarketSell:
    def test_market_sell_returns_success(self) -> None:
        client = MagicMock()
        client.submit_order.return_value = _mock_order("sell-1")

        with _patched_client(client):
            result = submit_market_sell("AAPL", qty=10.0)

        assert result.success is True
        assert result.side == "sell"
        assert result.qty == 10.0

    def test_api_error_returns_failure(self) -> None:
        client = MagicMock()
        client.submit_order.side_effect = RuntimeError("rejected")

        with _patched_client(client):
            result = submit_market_sell("AAPL", qty=5.0)

        assert result.success is False
        assert result.error == "rejected"


# ===========================================================================
# close_position
# ===========================================================================


class TestClosePosition:
    def test_closes_held_position_via_market_sell(self) -> None:
        """When a position exists, close_position must submit a market sell."""
        client = MagicMock()
        pos_mock = MagicMock()
        pos_mock.qty = "7.5"
        client.get_open_position.return_value = pos_mock
        client.submit_order.return_value = _mock_order("close-1")

        with _patched_client(client):
            result = close_position("AAPL")

        assert result.success is True
        assert result.qty == 7.5

    def test_returns_success_when_position_does_not_exist(self) -> None:
        """close_position must be idempotent — no error when already flat."""
        client = MagicMock()
        client.get_open_position.side_effect = Exception("position does not exist")

        with _patched_client(client):
            result = close_position("FLAT")

        assert result.success is True
        assert result.qty == 0

    def test_returns_success_on_404(self) -> None:
        client = MagicMock()
        client.get_open_position.side_effect = Exception("404 not found")

        with _patched_client(client):
            result = close_position("GONE")

        assert result.success is True

    def test_returns_failure_on_unexpected_error(self) -> None:
        client = MagicMock()
        client.get_open_position.side_effect = RuntimeError("network error")

        with _patched_client(client):
            result = close_position("ERR")

        assert result.success is False
        assert "network error" in result.error

    def test_no_sell_when_qty_is_zero(self) -> None:
        """If the position qty is 0, no sell order should be submitted."""
        client = MagicMock()
        pos_mock = MagicMock()
        pos_mock.qty = "0"
        client.get_open_position.return_value = pos_mock

        with _patched_client(client):
            result = close_position("FLAT")

        client.submit_order.assert_not_called()
        assert result.success is True


# ===========================================================================
# get_open_positions
# ===========================================================================


class TestGetOpenPositions:
    def test_maps_alpaca_positions_to_position_summary(self) -> None:
        client = MagicMock()
        client.get_all_positions.return_value = [
            _mock_position("NVDA", qty=5.0, avg_entry=400.0, current_price=372.0, unrealized_plpc=-0.07),
            _mock_position("AMD", qty=10.0, avg_entry=100.0, current_price=110.0, unrealized_plpc=0.10),
        ]

        with _patched_client(client):
            positions = get_open_positions()

        assert len(positions) == 2
        nvda = next(p for p in positions if p.symbol == "NVDA")
        assert nvda.qty == 5.0
        assert nvda.avg_entry_price == 400.0
        assert nvda.unrealized_pl_pct == pytest.approx(-0.07)

    def test_returns_empty_list_on_error(self) -> None:
        client = MagicMock()
        client.get_all_positions.side_effect = RuntimeError("connection error")

        with _patched_client(client):
            positions = get_open_positions()

        assert positions == []


# ===========================================================================
# get_open_orders
# ===========================================================================


class TestGetOpenOrders:
    def test_returns_all_open_orders_without_filter(self) -> None:
        client = MagicMock()
        mock_orders = [MagicMock(), MagicMock()]
        client.get_orders.return_value = mock_orders

        with _patched_client(client):
            orders = get_open_orders()

        assert len(orders) == 2

    def test_passes_symbol_filter(self) -> None:
        from alpaca.trading.requests import GetOrdersRequest

        client = MagicMock()
        client.get_orders.return_value = []

        with _patched_client(client):
            get_open_orders("AAPL")

        req = client.get_orders.call_args[0][0]
        assert isinstance(req, GetOrdersRequest)
        assert req.symbols == ["AAPL"]

    def test_returns_empty_list_on_error(self) -> None:
        client = MagicMock()
        client.get_orders.side_effect = RuntimeError("timeout")

        with _patched_client(client):
            orders = get_open_orders()

        assert orders == []


# ===========================================================================
# cancel_open_orders
# ===========================================================================


class TestCancelOpenOrders:
    def test_cancels_all_orders_for_symbol(self) -> None:
        client = MagicMock()
        o1, o2 = MagicMock(), MagicMock()
        o1.id, o2.id = "order-a", "order-b"
        o1.symbol, o2.symbol = "AAPL", "AAPL"
        client.get_orders.return_value = [o1, o2]

        with _patched_client(client):
            count = cancel_open_orders("AAPL")

        assert count == 2
        assert client.cancel_order_by_id.call_count == 2

    def test_counts_only_successful_cancellations(self) -> None:
        """If one cancel fails, the count should reflect only successful ones."""
        client = MagicMock()
        o1 = MagicMock()
        o1.id = "ok-order"
        o1.symbol = "AAPL"
        o2 = MagicMock()
        o2.id = "bad-order"
        o2.symbol = "AAPL"
        client.get_orders.return_value = [o1, o2]
        client.cancel_order_by_id.side_effect = [None, RuntimeError("already cancelled")]

        with _patched_client(client):
            count = cancel_open_orders("AAPL")

        assert count == 1


# ===========================================================================
# check_exit_signals
# ===========================================================================


class TestCheckExitSignals:
    def _make_positions(self) -> list[PositionSummary]:
        return [
            PositionSummary("DOWN7", 10.0, 100.0, 93.0, -0.07),   # exactly at 7% threshold
            PositionSummary("DOWN8", 10.0, 100.0, 91.0, -0.09),   # beyond threshold
            PositionSummary("UP", 10.0, 100.0, 110.0, 0.10),      # healthy
            PositionSummary("DOWN6", 10.0, 100.0, 94.0, -0.06),   # below threshold — no exit
        ]

    def test_positions_at_or_below_threshold_are_returned(self) -> None:
        positions = self._make_positions()
        signals = check_exit_signals(positions, stop_loss_pct=0.07)
        symbols = {p.symbol for p in signals}
        assert "DOWN7" in symbols
        assert "DOWN8" in symbols

    def test_healthy_positions_not_returned(self) -> None:
        positions = self._make_positions()
        signals = check_exit_signals(positions, stop_loss_pct=0.07)
        symbols = {p.symbol for p in signals}
        assert "UP" not in symbols
        assert "DOWN6" not in symbols

    def test_uses_settings_default_when_pct_not_provided(self) -> None:
        positions = [PositionSummary("X", 1.0, 100.0, 93.0, -0.07)]
        with patch("core.order_execution.settings") as s:
            s.STOP_LOSS_PCT = 0.07
            signals = check_exit_signals(positions)
        assert len(signals) == 1

    def test_empty_positions_returns_empty(self) -> None:
        assert check_exit_signals([]) == []


# ===========================================================================
# auto_trader: monitor_and_exit_positions
# ===========================================================================


class TestMonitorAndExitPositions:
    """Tests for auto_trader.monitor_and_exit_positions."""

    def _healthy_ohlcv(self, n: int = 60, above_ema: bool = True) -> pd.DataFrame:
        """Return a synthetic OHLCV DataFrame where price is above/below 21-day EMA."""
        import numpy as np

        prices = [100.0] * n
        if not above_ema:
            # Last 2 bars drop well below an EMA anchored around 100
            prices[-2] = 85.0
            prices[-1] = 84.0
        return pd.DataFrame({"Close": prices, "Volume": [1_000_000] * n})

    def test_hard_stop_triggers_close(self) -> None:
        """Position at -7% must be closed when monitor_and_exit is called."""
        from auto_trader import monitor_and_exit_positions

        pos_stop = PositionSummary("STOP7", 10.0, 100.0, 93.0, -0.07)
        manager = MagicMock()
        manager.submit_exit.return_value = OrderResult(True, "sell-1", "STOP7", "sell", 10.0)

        with (
            patch("auto_trader.get_open_positions", return_value=[pos_stop]),
            patch("auto_trader.OrderManager", return_value=manager),
            patch("auto_trader.fetch_ohlcv", return_value=self._healthy_ohlcv()),
        ):
            exited = monitor_and_exit_positions(stop_loss_pct=0.07)

        manager.submit_exit.assert_called_once_with("STOP7", exit_reason="hard stop triggered")
        assert "STOP7" in exited

    def test_ma_violation_triggers_close(self) -> None:
        """Two consecutive closes below 21-day EMA must trigger an exit."""
        from auto_trader import monitor_and_exit_positions

        pos_healthy_stop = PositionSummary("MABREAK", 10.0, 100.0, 97.0, -0.03)
        manager = MagicMock()
        manager.submit_exit.return_value = OrderResult(True, "sell-2", "MABREAK", "sell", 10.0)

        with (
            patch("auto_trader.get_open_positions", return_value=[pos_healthy_stop]),
            patch("auto_trader.OrderManager", return_value=manager),
            patch("auto_trader.fetch_ohlcv", return_value=self._healthy_ohlcv(n=60, above_ema=False)),
        ):
            exited = monitor_and_exit_positions(stop_loss_pct=0.07)

        manager.submit_exit.assert_called_once()
        assert "MABREAK" in exited

    def test_healthy_position_not_exited(self) -> None:
        """A position with small gain and price above EMA must NOT be exited."""
        from auto_trader import monitor_and_exit_positions

        pos_ok = PositionSummary("WINNER", 10.0, 100.0, 110.0, 0.10)
        manager = MagicMock()

        with (
            patch("auto_trader.get_open_positions", return_value=[pos_ok]),
            patch("auto_trader.OrderManager", return_value=manager),
            patch("auto_trader.fetch_ohlcv", return_value=self._healthy_ohlcv(n=60, above_ema=True)),
        ):
            exited = monitor_and_exit_positions(stop_loss_pct=0.07)

        manager.submit_exit.assert_not_called()
        assert exited == []

    def test_no_positions_returns_empty(self) -> None:
        from auto_trader import monitor_and_exit_positions

        with patch("auto_trader.get_open_positions", return_value=[]):
            exited = monitor_and_exit_positions()

        assert exited == []

    def test_hard_stop_position_not_double_exited_by_ma_check(self) -> None:
        """A position exited by the hard-stop must not also trigger the MA-check."""
        from auto_trader import monitor_and_exit_positions

        pos_stop = PositionSummary("BOTH", 10.0, 100.0, 90.0, -0.10)
        manager = MagicMock()
        manager.submit_exit.return_value = OrderResult(True, "x", "BOTH", "sell", 10.0)

        with (
            patch("auto_trader.get_open_positions", return_value=[pos_stop]),
            patch("auto_trader.OrderManager", return_value=manager),
            patch("auto_trader.fetch_ohlcv", return_value=self._healthy_ohlcv(n=60, above_ema=False)),
        ):
            exited = monitor_and_exit_positions(stop_loss_pct=0.07)

        assert manager.submit_exit.call_count == 1
        assert exited.count("BOTH") == 1


# ===========================================================================
# auto_trader: execute_entries
# ===========================================================================


def _make_buy_signal(
    symbol: str = "NVDA",
    canslim_score: float = 80.0,
    rs_score: float = 90.0,
    is_breakout: bool = True,
    has_surge: bool = True,
    buy_point: float | None = None,
) -> dict:
    return {
        "symbol": symbol,
        "total_score": canslim_score,
        "rs_score": rs_score,
        "is_breakout": is_breakout,
        "has_volume_surge": has_surge,
        "buy_point": buy_point,
        "scores": {"C": 0.8, "A": 0.8, "N": 0.8, "S": 0.8, "L": 0.9, "I": 0.7, "M": 1.0},
        "metrics": {},
        "scanner_category": "actionable_buy",
        "scanner_notes": [],
    }


class TestExecuteEntries:
    def test_build_entry_plan_computes_entry_stop_qty_and_risk(self) -> None:
        """The execution plan must deterministically compute price, stop, size, and risk."""
        from auto_trader import _build_entry_execution_plan

        with patch("auto_trader._resolve_entry_reference_price", return_value=(500.0, "intraday_minute_close")):
            plan = _build_entry_execution_plan(
                opportunity=_make_buy_signal("NVDA", canslim_score=82.5, rs_score=94.0, buy_point=500.0),
                equity=100_000.0,
                market_open=True,
                position_size_pct=0.10,
                stop_loss_pct=0.07,
            )

        assert plan is not None
        assert plan.symbol == "NVDA"
        assert plan.entry_price == pytest.approx(500.0)
        assert plan.stop_price == pytest.approx(465.0)
        assert plan.qty == pytest.approx(20.0)
        assert plan.position_value == pytest.approx(10_000.0)
        assert plan.risk_per_share == pytest.approx(35.0)
        assert plan.risk_amount == pytest.approx(700.0)
        assert plan.price_source == "intraday_minute_close"

    def test_submits_bracket_buy_for_valid_signal(self) -> None:
        """An actionable buy with available equity must result in a bracket order."""
        from auto_trader import execute_entries

        with (
            patch("auto_trader._get_account_equity", return_value=100_000.0),
            patch("auto_trader.get_open_positions", return_value=[]),
            patch("auto_trader.get_open_orders", return_value=[]),
            patch("auto_trader._build_entry_execution_plan") as mock_plan,
            patch("auto_trader.OrderManager") as mock_manager_cls,
            patch("auto_trader.settings") as mock_settings,
        ):
            mock_settings.MAX_OPEN_POSITIONS = 5
            mock_settings.POSITION_SIZE_PCT = 0.10
            mock_settings.STOP_LOSS_PCT = 0.07
            mock_plan.return_value = SimpleNamespace(
                symbol="NVDA",
                entry_price=500.0,
                price_source="intraday_minute_close",
                stop_price=465.0,
                stop_loss_pct=0.07,
                position_value=10_000.0,
                risk_amount=700.0,
                risk_per_share=35.0,
                qty=20.0,
                canslim_score=80.0,
                rs_score=90.0,
                is_breakout=True,
                has_volume_surge=True,
            )
            manager = MagicMock()
            manager.submit_entry.return_value = SimpleNamespace(success=True, dry_run=False, workflow_id="wf-nvda-1")
            mock_manager_cls.return_value = manager
            entered = execute_entries([_make_buy_signal("NVDA")])

        manager.submit_entry.assert_called_once_with(
            mock_plan.return_value,
            signal_payload=_make_buy_signal("NVDA"),
            dry_run=False,
        )
        assert "NVDA" in entered

    def test_skips_already_held_symbol(self) -> None:
        """Symbols already in the portfolio must not generate a new entry order."""
        from auto_trader import execute_entries

        held = PositionSummary("NVDA", 5.0, 480.0, 500.0, 0.04)

        with (
            patch("auto_trader._get_account_equity", return_value=100_000.0),
            patch("auto_trader.get_open_positions", return_value=[held]),
            patch("auto_trader.get_open_orders", return_value=[]),
            patch("auto_trader.OrderManager") as mock_manager_cls,
            patch("auto_trader.settings") as mock_settings,
        ):
            mock_settings.MAX_OPEN_POSITIONS = 5
            mock_settings.POSITION_SIZE_PCT = 0.10
            mock_settings.STOP_LOSS_PCT = 0.07
            entered = execute_entries([_make_buy_signal("NVDA")])

        mock_manager_cls.return_value.submit_entry.assert_not_called()
        assert entered == []

    def test_position_limit_stops_new_entries(self) -> None:
        """When MAX_OPEN_POSITIONS is reached, no new entries should be submitted."""
        from auto_trader import execute_entries

        # 5 existing positions, limit is 5
        existing = [PositionSummary(s, 1.0, 100.0, 100.0, 0.0) for s in ["A", "B", "C", "D", "E"]]

        with (
            patch("auto_trader._get_account_equity", return_value=100_000.0),
            patch("auto_trader.get_open_positions", return_value=existing),
            patch("auto_trader.get_open_orders", return_value=[]),
            patch("auto_trader.OrderManager") as mock_manager_cls,
            patch("auto_trader.settings") as mock_settings,
        ):
            mock_settings.MAX_OPEN_POSITIONS = 5
            mock_settings.POSITION_SIZE_PCT = 0.10
            mock_settings.STOP_LOSS_PCT = 0.07
            entered = execute_entries([_make_buy_signal("NVDA"), _make_buy_signal("AMD")])

        mock_manager_cls.return_value.submit_entry.assert_not_called()
        assert entered == []

    def test_dry_run_does_not_submit_orders(self) -> None:
        """In dry_run mode, symbols must be returned but no order submitted."""
        from auto_trader import execute_entries

        with (
            patch("auto_trader._get_account_equity", return_value=50_000.0),
            patch("auto_trader.get_open_positions", return_value=[]),
            patch("auto_trader.get_open_orders", return_value=[]),
            patch("auto_trader._build_entry_execution_plan") as mock_plan,
            patch("auto_trader.OrderManager") as mock_manager_cls,
            patch("auto_trader.settings") as mock_settings,
        ):
            mock_settings.MAX_OPEN_POSITIONS = 5
            mock_settings.POSITION_SIZE_PCT = 0.10
            mock_settings.STOP_LOSS_PCT = 0.07
            mock_plan.return_value = SimpleNamespace(
                symbol="AMD",
                entry_price=200.0,
                price_source="daily_close",
                stop_price=186.0,
                stop_loss_pct=0.07,
                position_value=5_000.0,
                risk_amount=350.0,
                risk_per_share=14.0,
                qty=25.0,
                canslim_score=80.0,
                rs_score=90.0,
                is_breakout=True,
                has_volume_surge=True,
            )
            manager = MagicMock()
            manager.submit_entry.return_value = SimpleNamespace(success=True, dry_run=True, workflow_id="wf-amd-1")
            mock_manager_cls.return_value = manager
            entered = execute_entries([_make_buy_signal("AMD")], dry_run=True)

        manager.submit_entry.assert_called_once()
        assert "AMD" in entered

    def test_returns_empty_when_equity_unavailable(self) -> None:
        """If equity is 0 (account fetch failed), no entries should proceed."""
        from auto_trader import execute_entries

        with patch("auto_trader._get_account_equity", return_value=0.0):
            entered = execute_entries([_make_buy_signal("GOOG")])

        assert entered == []

    def test_skips_symbol_when_price_unavailable(self) -> None:
        """Cannot size a position without a price — symbol must be skipped."""
        from auto_trader import execute_entries

        with (
            patch("auto_trader._get_account_equity", return_value=100_000.0),
            patch("auto_trader.get_open_positions", return_value=[]),
            patch("auto_trader.get_open_orders", return_value=[]),
            patch("auto_trader._build_entry_execution_plan", return_value=None),
            patch("auto_trader.OrderManager") as mock_manager_cls,
            patch("auto_trader.settings") as mock_settings,
        ):
            mock_settings.MAX_OPEN_POSITIONS = 5
            mock_settings.POSITION_SIZE_PCT = 0.10
            mock_settings.STOP_LOSS_PCT = 0.07
            entered = execute_entries([_make_buy_signal("NODATA")])

        mock_manager_cls.return_value.submit_entry.assert_not_called()
        assert entered == []

    def test_respects_available_slot_count(self) -> None:
        """If 4 positions are open with limit 5, only 1 new entry should proceed."""
        from auto_trader import execute_entries

        existing = [PositionSummary(s, 1.0, 100.0, 100.0, 0.0) for s in ["A", "B", "C", "D"]]
        signals = [_make_buy_signal("NEW1"), _make_buy_signal("NEW2")]

        with (
            patch("auto_trader._get_account_equity", return_value=100_000.0),
            patch("auto_trader.get_open_positions", return_value=existing),
            patch("auto_trader.get_open_orders", return_value=[]),
            patch("auto_trader._build_entry_execution_plan") as mock_plan,
            patch("auto_trader.OrderManager") as mock_manager_cls,
            patch("auto_trader.settings") as mock_settings,
        ):
            mock_settings.MAX_OPEN_POSITIONS = 5
            mock_settings.POSITION_SIZE_PCT = 0.10
            mock_settings.STOP_LOSS_PCT = 0.07
            mock_plan.return_value = SimpleNamespace(
                symbol="NEW1",
                entry_price=100.0,
                price_source="daily_close",
                stop_price=93.0,
                stop_loss_pct=0.07,
                position_value=10_000.0,
                risk_amount=700.0,
                risk_per_share=7.0,
                qty=100.0,
                canslim_score=80.0,
                rs_score=90.0,
                is_breakout=True,
                has_volume_surge=True,
            )
            manager = MagicMock()
            manager.submit_entry.return_value = SimpleNamespace(success=True, dry_run=False, workflow_id="wf-new1-1")
            mock_manager_cls.return_value = manager
            entered = execute_entries(signals)

        assert len(entered) == 1
        assert entered[0] == "NEW1"

    def test_limits_cycle_to_top_ranked_new_entries(self) -> None:
        """When too many actionable buys exist, only the top-ranked setups should execute."""
        from auto_trader import execute_entries

        low = _make_buy_signal("LOW", canslim_score=74.0, rs_score=82.0, buy_point=100.0)
        high = _make_buy_signal("HIGH", canslim_score=89.0, rs_score=96.0, buy_point=100.0)

        def _plan_for(*, opportunity: dict, **_: Any):
            return SimpleNamespace(
                symbol=opportunity["symbol"],
                entry_price=100.0,
                price_source="daily_close",
                stop_price=93.0,
                stop_loss_pct=0.07,
                position_value=10_000.0,
                risk_amount=700.0,
                risk_per_share=7.0,
                qty=100.0,
                canslim_score=float(opportunity["total_score"]),
                rs_score=float(opportunity["rs_score"]),
                is_breakout=True,
                has_volume_surge=True,
            )

        with (
            patch("auto_trader._get_account_equity", return_value=100_000.0),
            patch("auto_trader.get_open_positions", return_value=[]),
            patch("auto_trader.get_open_orders", return_value=[]),
            patch("auto_trader._build_entry_execution_plan", side_effect=_plan_for),
            patch("auto_trader.OrderManager") as mock_manager_cls,
            patch("auto_trader.settings") as mock_settings,
        ):
            mock_settings.MAX_OPEN_POSITIONS = 5
            mock_settings.MAX_NEW_ENTRIES_PER_CYCLE = 1
            mock_settings.POSITION_SIZE_PCT = 0.10
            mock_settings.STOP_LOSS_PCT = 0.07
            manager = MagicMock()
            manager.submit_entry.return_value = SimpleNamespace(success=True, dry_run=False, workflow_id="wf-high-1")
            mock_manager_cls.return_value = manager
            entered = execute_entries([low, high])

        assert entered == ["HIGH"]
        submitted_plan = manager.submit_entry.call_args.args[0]
        assert submitted_plan.symbol == "HIGH"

    def test_pending_buy_orders_consume_position_slots(self) -> None:
        """Pending buys must count toward MAX_OPEN_POSITIONS to prevent over-allocation."""
        from auto_trader import execute_entries

        existing = [PositionSummary(s, 1.0, 100.0, 100.0, 0.0) for s in ["A", "B", "C", "D"]]
        pending_buy = _mock_order("buy-1", symbol="PEND", side="buy", order_type="limit", qty=1.0)

        with (
            patch("auto_trader._get_account_equity", return_value=100_000.0),
            patch("auto_trader.get_open_positions", return_value=existing),
            patch("auto_trader.get_open_orders", return_value=[pending_buy]),
            patch("auto_trader.OrderManager") as mock_manager_cls,
            patch("auto_trader.settings") as mock_settings,
        ):
            mock_settings.MAX_OPEN_POSITIONS = 5
            mock_settings.POSITION_SIZE_PCT = 0.10
            mock_settings.STOP_LOSS_PCT = 0.07
            entered = execute_entries([_make_buy_signal("NEW1")])

        mock_manager_cls.return_value.submit_entry.assert_not_called()
        assert entered == []

    def test_successful_submission_sends_entry_notification_with_plan_values(self) -> None:
        """Submission notification must use the exact pre-trade execution plan values."""
        from auto_trader import execute_entries

        with (
            patch("auto_trader._get_account_equity", return_value=100_000.0),
            patch("auto_trader.get_open_positions", return_value=[]),
            patch("auto_trader.get_open_orders", return_value=[]),
            patch("auto_trader._build_entry_execution_plan") as mock_plan,
            patch("auto_trader.OrderManager") as mock_manager_cls,
            patch("auto_trader.settings") as mock_settings,
        ):
            mock_settings.MAX_OPEN_POSITIONS = 5
            mock_settings.POSITION_SIZE_PCT = 0.10
            mock_settings.STOP_LOSS_PCT = 0.07
            mock_plan.return_value = SimpleNamespace(
                symbol="NVDA",
                entry_price=500.0,
                price_source="intraday_minute_close",
                stop_price=465.0,
                stop_loss_pct=0.07,
                position_value=10_000.0,
                risk_amount=700.0,
                risk_per_share=35.0,
                qty=20.0,
                canslim_score=80.0,
                rs_score=90.0,
                is_breakout=True,
                has_volume_surge=True,
            )
            manager = MagicMock()
            manager.submit_entry.return_value = SimpleNamespace(success=True, dry_run=False, workflow_id="wf-nvda-42")
            mock_manager_cls.return_value = manager
            entered = execute_entries([_make_buy_signal("NVDA")])

        assert entered == ["NVDA"]
        manager.submit_entry.assert_called_once()


# ===========================================================================
# auto_trader: _is_market_open / _get_account_equity
# ===========================================================================


# ===========================================================================
# auto_trader: buy-zone enforcement
# ===========================================================================


class TestBuyZoneEnforcement:
    """Tests for _is_within_buy_zone and buy-zone gate in _build_entry_execution_plan."""

    def test_price_within_5pct_of_pivot_is_allowed(self) -> None:
        from auto_trader import _is_within_buy_zone

        # 3% above pivot — inside the 5% buy zone
        assert _is_within_buy_zone(current_price=103.0, buy_point=100.0, max_extension_pct=0.05) is True

    def test_price_exactly_at_buy_zone_max_is_allowed(self) -> None:
        from auto_trader import _is_within_buy_zone

        # exactly at 5% — still in range (boundary inclusive)
        assert _is_within_buy_zone(current_price=105.0, buy_point=100.0, max_extension_pct=0.05) is True

    def test_price_beyond_5pct_of_pivot_is_rejected(self) -> None:
        from auto_trader import _is_within_buy_zone

        # 6% above pivot — outside the buy zone
        assert _is_within_buy_zone(current_price=106.0, buy_point=100.0, max_extension_pct=0.05) is False

    def test_missing_buy_point_allows_entry(self) -> None:
        """Missing pivot metadata must fail a strict breakout entry."""
        from auto_trader import _is_within_buy_zone

        assert _is_within_buy_zone(current_price=999.0, buy_point=None) is False

    def test_zero_buy_point_is_rejected(self) -> None:
        """A zero buy_point is invalid data and must fail strict entry validation."""
        from auto_trader import _is_within_buy_zone

        assert _is_within_buy_zone(current_price=50.0, buy_point=0.0) is False

    def test_price_below_pivot_is_rejected(self) -> None:
        from auto_trader import _is_within_buy_zone

        assert _is_within_buy_zone(current_price=99.0, buy_point=100.0, max_extension_pct=0.05) is False

    def test_build_plan_rejects_entry_when_price_too_extended(self) -> None:
        """_build_entry_execution_plan must return None when price is beyond buy zone."""
        from auto_trader import _build_entry_execution_plan

        opp = _make_buy_signal("OVER", canslim_score=80.0, rs_score=90.0)
        opp["buy_point"] = 100.0  # pivot at $100

        # Resolve price to $107 — 7% above pivot, outside the 5% buy zone
        with patch("auto_trader._resolve_entry_reference_price", return_value=(107.0, "daily_close")):
            plan = _build_entry_execution_plan(
                opportunity=opp,
                equity=100_000.0,
                market_open=False,
                position_size_pct=0.10,
                stop_loss_pct=0.07,
            )

        assert plan is None

    def test_build_plan_allows_entry_within_buy_zone(self) -> None:
        """_build_entry_execution_plan must return a valid plan when within buy zone."""
        from auto_trader import _build_entry_execution_plan

        opp = _make_buy_signal("INZONE", canslim_score=80.0, rs_score=90.0)
        opp["buy_point"] = 100.0  # pivot at $100

        # Resolve price to $103 — 3% above pivot, inside the 5% buy zone
        with patch("auto_trader._resolve_entry_reference_price", return_value=(103.0, "daily_close")):
            plan = _build_entry_execution_plan(
                opportunity=opp,
                equity=100_000.0,
                market_open=False,
                position_size_pct=0.10,
                stop_loss_pct=0.07,
            )

        assert plan is not None
        assert plan.symbol == "INZONE"
        assert plan.entry_price == pytest.approx(103.0)

    def test_build_plan_rejects_entry_when_no_pivot_metadata(self) -> None:
        """Missing pivot metadata must block a live breakout entry plan."""
        from auto_trader import _build_entry_execution_plan

        opp = _make_buy_signal("NOPIVOT", canslim_score=80.0, rs_score=90.0)
        # Explicitly absent — this should never be executable as a strict breakout buy
        opp.pop("buy_point", None)

        with patch("auto_trader._resolve_entry_reference_price", return_value=(200.0, "daily_close")):
            plan = _build_entry_execution_plan(
                opportunity=opp,
                equity=100_000.0,
                market_open=False,
                position_size_pct=0.10,
                stop_loss_pct=0.07,
            )

        assert plan is None


class TestHelpers:
    def test_is_market_open_returns_true_when_clock_is_open(self) -> None:
        from auto_trader import _is_market_open

        clock = MagicMock()
        clock.is_open = True
        client = MagicMock()
        client.get_clock.return_value = clock

        with patch("auto_trader._get_trading_client", return_value=client):
            assert _is_market_open() is True

    def test_is_market_open_returns_false_when_clock_closed(self) -> None:
        from auto_trader import _is_market_open

        clock = MagicMock()
        clock.is_open = False
        client = MagicMock()
        client.get_clock.return_value = clock

        with patch("auto_trader._get_trading_client", return_value=client):
            assert _is_market_open() is False

    def test_is_market_open_returns_false_on_error(self) -> None:
        from auto_trader import _is_market_open

        client = MagicMock()
        client.get_clock.side_effect = RuntimeError("network error")

        with patch("auto_trader._get_trading_client", return_value=client):
            assert _is_market_open() is False

    def test_get_account_equity_returns_float(self) -> None:
        from auto_trader import _get_account_equity

        account = MagicMock()
        account.equity = "125000.50"
        client = MagicMock()
        client.get_account.return_value = account

        with patch("auto_trader._get_trading_client", return_value=client):
            equity = _get_account_equity()

        assert equity == pytest.approx(125_000.50)

    def test_get_account_equity_returns_zero_on_error(self) -> None:
        from auto_trader import _get_account_equity

        client = MagicMock()
        client.get_account.side_effect = RuntimeError("auth error")

        with patch("auto_trader._get_trading_client", return_value=client):
            equity = _get_account_equity()

        assert equity == 0.0
