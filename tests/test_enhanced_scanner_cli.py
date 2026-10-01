"""CLI safety tests for the provider-backed scanner."""

from types import SimpleNamespace
from unittest.mock import call, patch

import pytest

import enhanced_scanner
from core.scheduler_observation import SchedulerObservation, activate_scheduler_observation


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


def test_observation_records_full_large_cap_request_and_validation_counts() -> None:
    market = SimpleNamespace(is_bullish=True, score=0.8, distribution_days=0, follow_through=False)
    extras = ["SP500-A", "EX1", "EX2", "EX3", "EX4", "EX5"]
    constituents = ["SP500-A", "SHARED", "NASDAQ-A", "SHARED"]
    observation = SchedulerObservation("large-cap-coverage")
    with (
        activate_scheduler_observation(observation),
        patch.object(enhanced_scanner.settings, "EXTRA_SYMBOLS", extras),
        patch.object(enhanced_scanner, "get_index_tickers", return_value=constituents) as get_index,
        patch.object(
            enhanced_scanner,
            "validate_tickers_bulk",
            return_value=["SP500-A", "SHARED", "NASDAQ-A", "EX1", "EX2", "EX3", "EX4"],
        ) as validate,
        patch.object(
            enhanced_scanner,
            "screen_stocks_canslim_detailed",
            return_value=([], [], market),
        ) as screen,
        patch.object(
            observation,
            "record_scan_coverage",
            wraps=observation.record_scan_coverage,
        ) as record_coverage,
    ):
        result = enhanced_scanner.scan_for_canslim_stocks(sectors="large_cap")

    requested = ["SP500-A", "SHARED", "NASDAQ-A", "EX1", "EX2", "EX3", "EX4", "EX5"]
    get_index.assert_called_once_with(index_name="large_cap")
    validate.assert_called_once_with(requested, retry_failed_chunks=True)
    assert screen.call_args.kwargs["symbols"] == requested[:-1]
    assert len(result) == 3
    coverage = observation.to_receipt()["scan_coverage"]
    assert coverage["requested"] == 8
    assert coverage["validated"] == 7
    assert record_coverage.call_args_list.count(call(requested=8)) == 1
    assert record_coverage.call_args_list.count(call(validated=7)) == 1
