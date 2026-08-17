"""Safety tests for the supervised paper-account lifecycle check."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import verify_paper_trading as verification
from core.order_execution import OrderResult


def test_preflight_blocks_existing_symbol_state() -> None:
    with (
        patch(
            "verify_paper_trading.get_open_positions",
            return_value=[SimpleNamespace(symbol="SPY")],
        ) as mock_positions,
        patch(
            "verify_paper_trading.get_open_orders",
            return_value=[SimpleNamespace(symbol="SPY", id="existing-order")],
        ) as mock_orders,
    ):
        assert verification._preflight_symbol_clear("SPY") is False

    mock_positions.assert_called_once_with(raise_on_error=True)
    mock_orders.assert_called_once_with("SPY", raise_on_error=True)


def test_closed_market_never_runs_cleanup_or_submits() -> None:
    client = MagicMock()
    client.get_account.return_value = SimpleNamespace(equity="100000", buying_power="50000")

    with (
        patch("verify_paper_trading._is_paper_mode", return_value=True),
        patch.object(verification.settings, "ALPACA_API_KEY", "present"),
        patch.object(verification.settings, "ALPACA_SECRET_KEY", "present"),
        patch("verify_paper_trading._get_trading_client", return_value=client),
        patch("verify_paper_trading._preflight_symbol_clear", return_value=True),
        patch("verify_paper_trading._check_market_open", return_value=False),
        patch("verify_paper_trading._cleanup_test_symbol") as mock_cleanup,
        patch("verify_paper_trading.submit_bracket_buy") as mock_submit,
    ):
        assert verification.main() == 1

    mock_cleanup.assert_not_called()
    mock_submit.assert_not_called()


def test_stop_preview_uses_configured_risk_default() -> None:
    assert verification._calculate_stop_price(100.0, 0.08) == 92.0


def test_alpaca_enum_side_is_normalized_for_fill_detection() -> None:
    assert verification._normalize_side("OrderSide.BUY") == "buy"


def test_price_provider_failure_stops_monitor_without_cleanup() -> None:
    client = MagicMock()
    client.get_account.return_value = SimpleNamespace(equity="100000", buying_power="50000")
    monitor = MagicMock()

    with (
        patch("verify_paper_trading._is_paper_mode", return_value=True),
        patch.object(verification.settings, "ALPACA_API_KEY", "present"),
        patch.object(verification.settings, "ALPACA_SECRET_KEY", "present"),
        patch("verify_paper_trading._get_trading_client", return_value=client),
        patch("verify_paper_trading._preflight_symbol_clear", return_value=True),
        patch("verify_paper_trading._check_market_open", return_value=True),
        patch("verify_paper_trading.FillMonitor", return_value=monitor),
        patch("verify_paper_trading.notify_configured", return_value=False),
        patch(
            "core.data_client.fetch_latest_intraday_price",
            side_effect=RuntimeError("price provider unavailable"),
        ),
        patch("verify_paper_trading._cleanup_test_symbol") as mock_cleanup,
    ):
        assert verification.main() == 1

    monitor.start.assert_called_once()
    monitor.stop.assert_called_once()
    mock_cleanup.assert_not_called()


def test_interrupt_after_submission_still_cleans_up_and_stops_monitor() -> None:
    client = MagicMock()
    client.get_account.return_value = SimpleNamespace(equity="100000", buying_power="50000")
    monitor = MagicMock()
    submitted = OrderResult(True, "order-1", "SPY", "buy", 1.0)

    with (
        patch("verify_paper_trading._is_paper_mode", return_value=True),
        patch.object(verification.settings, "ALPACA_API_KEY", "present"),
        patch.object(verification.settings, "ALPACA_SECRET_KEY", "present"),
        patch("verify_paper_trading._get_trading_client", return_value=client),
        patch("verify_paper_trading._preflight_symbol_clear", return_value=True),
        patch("verify_paper_trading._check_market_open", return_value=True),
        patch("verify_paper_trading.FillMonitor", return_value=monitor),
        patch("verify_paper_trading.notify_configured", return_value=False),
        patch("core.data_client.fetch_latest_intraday_price", return_value=100.0),
        patch("verify_paper_trading.submit_bracket_buy", return_value=submitted),
        patch("verify_paper_trading.time.time", side_effect=[0.0, 0.0]),
        patch("verify_paper_trading.time.sleep", side_effect=KeyboardInterrupt),
        patch("verify_paper_trading._cleanup_test_symbol", return_value=True) as cleanup,
    ):
        with pytest.raises(KeyboardInterrupt):
            verification.main()

    cleanup.assert_called_once_with("SPY")
    monitor.stop.assert_called_once()
