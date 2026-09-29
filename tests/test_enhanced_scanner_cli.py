"""CLI safety tests for the provider-backed scanner."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

import enhanced_scanner


def test_help_exits_without_starting_provider_scan(capsys) -> None:
    with patch("enhanced_scanner.scan_for_canslim_stocks") as scan:
        with pytest.raises(SystemExit) as exc_info:
            enhanced_scanner.main(["--help"])

    assert exc_info.value.code == 0
    assert "CANSLIM" in capsys.readouterr().out
    scan.assert_not_called()


def test_explicit_bounded_scan_excludes_configured_extra_symbols() -> None:
    market = SimpleNamespace(
        is_bullish=True,
        score=0.8,
        distribution_days=0,
        follow_through=False,
    )
    with (
        patch.object(enhanced_scanner.settings, "EXTRA_SYMBOLS", ["MSFT"]),
        patch.object(
            enhanced_scanner,
            "validate_tickers_bulk",
            return_value=["AAPL"],
        ) as validate,
        patch.object(
            enhanced_scanner,
            "screen_stocks_canslim_detailed",
            return_value=([], [], market),
        ) as screen,
    ):
        enhanced_scanner.scan_for_canslim_stocks(
            custom_list=["AAPL"],
            include_extra_symbols=False,
            retry_failed_market_data_chunks=False,
        )

    validate.assert_called_once_with(["AAPL"], retry_failed_chunks=False)
    assert screen.call_args.kwargs["symbols"] == ["AAPL"]
    assert screen.call_args.kwargs["retry_failed_market_data_chunks"] is False
