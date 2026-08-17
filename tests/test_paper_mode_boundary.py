"""System-wide safety tests for the paper-account-only execution boundary."""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import patch

import pytest

import core.order_execution as order_execution
from auto_trader import run_auto_trader
from core.notifier import notify_buy_filled
from core.order_manager import OrderManager
from scheduler import run_scheduler


def test_trading_client_refuses_live_account_before_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Removing the low-level paper guard must make this test fail."""
    monkeypatch.setenv("ALPACA_PAPER", "false")
    monkeypatch.setattr(order_execution, "_trading_client", None)

    with patch("core.order_execution.TradingClient") as client:
        with pytest.raises(RuntimeError, match="Live-account trading is disabled"):
            order_execution._get_trading_client()

    client.assert_not_called()


def test_fill_monitor_refuses_live_stream_before_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    """A false paper flag must never construct an Alpaca live trade-update stream."""
    monkeypatch.setenv("ALPACA_PAPER", "false")

    with (
        patch("fill_monitor.TradingStream") as stream,
        patch("fill_monitor.OrderManager") as manager,
    ):
        from fill_monitor import FillMonitor

        with pytest.raises(RuntimeError, match="Live-account trading is disabled"):
            FillMonitor()

    stream.assert_not_called()
    manager.assert_not_called()


@pytest.mark.parametrize("dry_run", [True, False])
def test_auto_trader_refuses_live_account_before_provider_work(
    monkeypatch: pytest.MonkeyPatch,
    dry_run: bool,
) -> None:
    """The orchestration boundary must reject live mode before any scan starts."""
    monkeypatch.setenv("ALPACA_PAPER", "false")

    with (
        patch("auto_trader._is_market_open") as market_clock,
        patch("auto_trader.monitor_and_exit_positions") as exits,
        patch("auto_trader.scan_for_canslim_stocks") as scan,
    ):
        with pytest.raises(RuntimeError, match="Live-account trading is disabled"):
            run_auto_trader(dry_run=dry_run)

    market_clock.assert_not_called()
    exits.assert_not_called()
    scan.assert_not_called()


def test_scheduler_refuses_live_account_before_monitor_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scheduler must fail before creating a potentially live fill monitor."""
    monkeypatch.setenv("ALPACA_PAPER", "false")

    with (
        patch("scheduler.FillMonitor") as monitor,
        patch("scheduler._run_startup_stop_reconciliation"),
        patch("scheduler.time.sleep", side_effect=KeyboardInterrupt),
    ):
        with pytest.raises(RuntimeError, match="Live-account trading is disabled"):
            run_scheduler(dry_run=False)

    monitor.assert_not_called()


def test_order_manager_refuses_explicit_live_mode() -> None:
    """A caller must not construct execution orchestration in live mode."""
    with pytest.raises(RuntimeError, match="Live-account trading is disabled"):
        OrderManager(paper=False)


def test_notification_refuses_live_mode_before_email() -> None:
    """Audit notifications must never describe an unsupported live execution."""
    with patch("core.notifier.send_email") as send:
        with pytest.raises(RuntimeError, match="Live-account trading is disabled"):
            notify_buy_filled("AAPL", qty=1, fill_price=100.0, stop_price=92.0, paper=False)

    send.assert_not_called()


def test_auto_trader_cli_exposes_paper_order_language_only() -> None:
    """A live-account enablement flag must not reappear in the operator CLI."""
    result = subprocess.run(
        [sys.executable, "auto_trader.py", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--enable-orders" in result.stdout
    assert "--live" not in result.stdout
