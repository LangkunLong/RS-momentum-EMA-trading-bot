"""Tests for I/O boundary code — API calls are mocked to prevent network hits."""

import os
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import requests
from alpaca.data.enums import DataFeed

import enhanced_scanner
from core import data_client
from core.data_client import fetch_bulk_close_prices, fetch_bulk_ohlcv, fetch_ohlcv, validate_ticker
from core.data_client import fmp_request_budget, reset_fmp_request_context
from core.scheduler_observation import SchedulerObservation, activate_scheduler_observation

# ─── export_results_to_csv ────────────────────────────────────────────────────

EXPECTED_CSV_COLUMNS = {
    "Symbol",
    "RS_Score",
    "CANSLIM_Score",
    "C_Score",
    "A_Score",
    "N_Score",
    "S_Score",
    "L_Score",
    "I_Score",
    "M_Score",
    "Current_Growth",
    "Annual_Growth",
    "Revenue_Growth",
    "Shares_Outstanding",
    "Proximity_to_High",
}


def test_export_creates_csv_file(mock_opportunity: dict, tmp_path: Path) -> None:
    """export_results_to_csv must create a CSV file at the specified path."""
    out_file = str(tmp_path / "test_export.csv")
    enhanced_scanner.export_results_to_csv([mock_opportunity], filename=out_file)
    assert os.path.exists(out_file), "CSV file was not created"


def test_export_csv_has_correct_columns(mock_opportunity: dict, tmp_path: Path) -> None:
    """Exported CSV must contain all expected column headers.

    Guards against column renames in export_results_to_csv breaking downstream
    consumers or dashboards that expect stable column names.
    """
    out_file = str(tmp_path / "test_columns.csv")
    enhanced_scanner.export_results_to_csv([mock_opportunity], filename=out_file)

    df = pd.read_csv(out_file)
    missing = EXPECTED_CSV_COLUMNS - set(df.columns)
    assert not missing, f"CSV is missing expected columns: {missing}"


def test_export_csv_contains_symbol_data(mock_opportunity: dict, tmp_path: Path) -> None:
    """The exported CSV must contain the correct symbol value from the input."""
    out_file = str(tmp_path / "test_symbol.csv")
    enhanced_scanner.export_results_to_csv([mock_opportunity], filename=out_file)

    df = pd.read_csv(out_file)
    assert "AAPL" in df["Symbol"].values


def test_export_empty_input_does_not_create_file(tmp_path: Path) -> None:
    """Calling export_results_to_csv with an empty list must not create a file."""
    out_file = str(tmp_path / "should_not_exist.csv")
    enhanced_scanner.export_results_to_csv([], filename=out_file)
    assert not os.path.exists(out_file), "CSV file must not be created when the opportunities list is empty"


# ─── validate_ticker ─────────────────────────────────────────────────────────


def test_validate_ticker_returns_false_on_api_exception() -> None:
    """validate_ticker must return False (not raise) when fetch_ohlcv raises.

    This verifies the defensive exception boundary in data_client.validate_ticker.
    """
    with patch("core.data_client.fetch_ohlcv", side_effect=Exception("simulated network error")):
        result = validate_ticker("FAKE")
    assert result is False


def test_validate_ticker_returns_false_on_empty_dataframe() -> None:
    """validate_ticker must return False when fetch_ohlcv returns an empty DataFrame."""
    with patch("core.data_client.fetch_ohlcv", return_value=pd.DataFrame()):
        result = validate_ticker("EMPTY")
    assert result is False


def test_fetch_ohlcv_uses_configured_alpaca_stock_feed() -> None:
    """Daily Alpaca bar requests must use the configured stock feed explicitly."""
    with (
        patch("core.data_client._get_alpaca_client") as mock_client,
        patch("core.data_client._cache_get", return_value=None),
        patch("core.data_client._cache_set"),
        patch("core.data_client.settings.ALPACA_STOCK_FEED", "sip"),
    ):
        barset = mock_client.return_value.get_stock_bars.return_value
        barset.df = pd.DataFrame(
            {
                "open": [100.0, 101.0],
                "high": [101.0, 102.0],
                "low": [99.0, 100.0],
                "close": [100.5, 101.5],
                "volume": [1_000_000, 1_100_000],
            },
            index=pd.DatetimeIndex(["2024-01-02", "2024-01-03"], tz="UTC"),
        )

        with patch("core.data_client._ALPACA_FEED_WARNING_EMITTED", False):
            fetch_ohlcv("NVDA", period="5d")

    request = mock_client.return_value.get_stock_bars.call_args[0][0]
    assert request.feed == DataFeed.SIP


def test_fetch_ohlcv_invalid_feed_falls_back_to_iex() -> None:
    """Invalid Alpaca feed config must degrade safely to IEX instead of crashing."""
    with (
        patch("core.data_client._get_alpaca_client") as mock_client,
        patch("core.data_client._cache_get", return_value=None),
        patch("core.data_client._cache_set"),
        patch("core.data_client.settings.ALPACA_STOCK_FEED", "not-a-feed"),
        patch("core.data_client._ALPACA_FEED_WARNING_EMITTED", False),
    ):
        barset = mock_client.return_value.get_stock_bars.return_value
        barset.df = pd.DataFrame(
            {
                "open": [100.0],
                "high": [101.0],
                "low": [99.0],
                "close": [100.5],
                "volume": [1_000_000],
            },
            index=pd.DatetimeIndex(["2024-01-02"], tz="UTC"),
        )

        fetch_ohlcv("AAPL", period="5d")

    request = mock_client.return_value.get_stock_bars.call_args[0][0]
    assert request.feed == DataFeed.IEX


def test_fetch_bulk_ohlcv_returns_symbol_frames() -> None:
    """Bulk daily bar downloads should split a multi-symbol Alpaca response into per-symbol OHLCV frames."""
    multi_index = pd.MultiIndex.from_product(
        [["NVDA", "MSFT"], pd.DatetimeIndex(["2024-01-02", "2024-01-03"], tz="UTC")],
        names=["symbol", "timestamp"],
    )
    bars_df = pd.DataFrame(
        {
            "open": [100.0, 101.0, 200.0, 201.0],
            "high": [101.0, 102.0, 201.0, 202.0],
            "low": [99.0, 100.0, 199.0, 200.0],
            "close": [100.5, 101.5, 200.5, 201.5],
            "volume": [1_000_000, 1_100_000, 2_000_000, 2_100_000],
        },
        index=multi_index,
    )

    with (
        patch("core.data_client._get_alpaca_client") as mock_client,
        patch("core.data_client._cache_get", return_value=None),
        patch("core.data_client._cache_set"),
        patch("core.data_client.time.sleep"),
    ):
        barset = mock_client.return_value.get_stock_bars.return_value
        barset.df = bars_df
        result = fetch_bulk_ohlcv(["NVDA", "MSFT"], period="5d", chunk_size=50)

    assert set(result.keys()) == {"NVDA", "MSFT"}
    assert list(result["NVDA"].columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert result["MSFT"]["Close"].iloc[-1] == 201.5


def _mock_bulk_barset_for_request(request):
    symbols = request.symbol_or_symbols
    if isinstance(symbols, str):
        symbols = [symbols]
    if "BAD" in symbols:
        raise RuntimeError("invalid symbol: BAD")

    index = pd.MultiIndex.from_product(
        [symbols, pd.DatetimeIndex(["2024-01-02"], tz="UTC")],
        names=["symbol", "timestamp"],
    )
    return SimpleNamespace(
        df=pd.DataFrame(
            {
                "open": [100.0] * len(index),
                "high": [101.0] * len(index),
                "low": [99.0] * len(index),
                "close": [100.5] * len(index),
                "volume": [1_000_000] * len(index),
            },
            index=index,
        )
    )


def test_bulk_close_prices_isolate_one_invalid_symbol() -> None:
    with (
        patch("core.data_client._get_alpaca_client") as mock_client,
        patch("core.data_client._cache_get", return_value=None),
        patch("core.data_client._cache_set"),
        patch("core.data_client.time.sleep"),
    ):
        mock_client.return_value.get_stock_bars.side_effect = _mock_bulk_barset_for_request
        result = enhanced_scanner.validate_tickers_bulk(["GOOD1", "BAD", "GOOD2"])

    assert set(result) == {"GOOD1", "GOOD2"}


def test_bulk_close_prices_does_not_split_failed_chunk_when_retries_disabled() -> None:
    with (
        patch("core.data_client._get_alpaca_client") as mock_client,
        patch("core.data_client._cache_get", return_value=None),
        patch("core.data_client._cache_set"),
        patch("core.data_client.time.sleep"),
    ):
        mock_client.return_value.get_stock_bars.side_effect = RuntimeError("provider unavailable")

        result = fetch_bulk_close_prices(
            ["AAPL", "MSFT", "NVDA"],
            period="5d",
            chunk_size=3,
            retry_failed_chunks=False,
        )

    assert result.empty
    mock_client.return_value.get_stock_bars.assert_called_once()


def test_bulk_ohlcv_isolates_one_invalid_symbol() -> None:
    with (
        patch("core.data_client._get_alpaca_client") as mock_client,
        patch("core.data_client._cache_get", return_value=None),
        patch("core.data_client._cache_set"),
        patch("core.data_client.time.sleep"),
    ):
        mock_client.return_value.get_stock_bars.side_effect = _mock_bulk_barset_for_request
        result = fetch_bulk_ohlcv(["GOOD1", "BAD", "GOOD2"], period="5d", chunk_size=3)

    assert set(result) == {"GOOD1", "GOOD2"}


_FMP_TEST_NOW = datetime(2026, 10, 1, 15, 30, tzinfo=ZoneInfo("America/New_York"))
_FMP_TEST_WINDOW = "2026-10-01T15:00:00-04:00"


def _prepare_observed_fmp_ledger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    count: int = 0,
    allowance: int = 5,
    window_start: str = _FMP_TEST_WINDOW,
    exists: bool = True,
) -> Path:
    ledger_path = tmp_path / "fmp-usage.json"
    if exists:
        ledger_path.write_text(
            json.dumps({"window_start": window_start, "count": count}),
            encoding="utf-8",
        )
    monkeypatch.setattr(data_client.settings, "FMP_REQUEST_LEDGER_PATH", str(ledger_path))
    monkeypatch.setattr(data_client.settings, "FMP_DAILY_REQUEST_BUDGET", allowance)
    monkeypatch.setattr(data_client.settings, "FMP_API_KEY", "test-key")
    monkeypatch.setattr(data_client.settings, "FMP_PLAN", "free")
    monkeypatch.setattr(data_client.settings, "FMP_SUPPRESS_REPEATED_ENDPOINT_ERRORS", True)
    monkeypatch.setattr(data_client, "_fmp_now_et", lambda: _FMP_TEST_NOW)
    monkeypatch.setattr(data_client, "_fmp_quota_exhausted", False)
    data_client._fmp_unavailable_endpoints.clear()
    data_client._fmp_reported_endpoint_failures.clear()
    reset_fmp_request_context()
    return ledger_path


def test_fmp_process_cap_is_not_reported_as_local_ledger_deferral(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ledger_path = _prepare_observed_fmp_ledger(monkeypatch, tmp_path)
    get = Mock()
    monkeypatch.setattr(data_client._fmp_session, "get", get)
    observation = SchedulerObservation("test")

    with fmp_request_budget(0), activate_scheduler_observation(observation):
        result = data_client._fmp_get("income-statement", {"symbol": "AAPL"})
        reason = data_client.fmp_request_deferral_reason()

    assert result == []
    assert reason == "process_cap"
    get.assert_not_called()
    assert json.loads(ledger_path.read_text(encoding="utf-8"))["count"] == 0
    assert observation.to_receipt()["service_health"] == "failed"


def test_fmp_local_ledger_exhaustion_degrades_coverage_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ledger_path = _prepare_observed_fmp_ledger(
        monkeypatch, tmp_path, count=5, allowance=5
    )
    get = Mock()
    monkeypatch.setattr(data_client._fmp_session, "get", get)
    observation = SchedulerObservation("test")

    with fmp_request_budget(1), activate_scheduler_observation(observation):
        result = data_client._fmp_get("income-statement", {"symbol": "AAPL"})
        reason = data_client.fmp_request_deferral_reason()

    receipt = observation.to_receipt()
    assert result == []
    assert reason == "local_ledger"
    get.assert_not_called()
    assert json.loads(ledger_path.read_text(encoding="utf-8"))["count"] == 5
    assert receipt["service_health"] == "healthy"
    assert receipt["required_input_coverage"] == "degraded"
    assert receipt["input_gaps"] == [
        {
            "symbol": "AAPL",
            "endpoint": "income-statement",
            "reason": "local_ledger_exhausted",
            "coverage_status": "degraded",
        }
    ]


@pytest.mark.parametrize("ledger_state", ["missing_at_start", "deleted_after_preflight", "unreadable"])
def test_fmp_observation_fails_closed_when_ledger_is_missing_or_unreadable(
    ledger_state: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ledger_path = _prepare_observed_fmp_ledger(
        monkeypatch,
        tmp_path,
        exists=ledger_state != "missing_at_start",
    )
    if ledger_state == "deleted_after_preflight":
        ledger_path.unlink()
    elif ledger_state == "unreadable":
        ledger_path.write_text("not-json", encoding="utf-8")
    get = Mock()
    monkeypatch.setattr(data_client._fmp_session, "get", get)
    observation = SchedulerObservation("test")

    with fmp_request_budget(1), activate_scheduler_observation(observation):
        result = data_client._fmp_get("income-statement", {"symbol": "AAPL"})
        reason = data_client.fmp_request_deferral_reason()

    assert result == []
    assert reason == ("ledger_unreadable" if ledger_state == "unreadable" else "ledger_missing")
    get.assert_not_called()
    assert observation.to_receipt()["service_health"] == "failed"
    assert not ledger_path.exists() or ledger_path.read_text(encoding="utf-8") == "not-json"


def test_existing_prior_window_fmp_ledger_rolls_over(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ledger_path = _prepare_observed_fmp_ledger(
        monkeypatch, tmp_path, count=5, window_start="2026-09-30T15:00:00-04:00"
    )
    response = Mock(status_code=200)
    response.json.return_value = [{"symbol": "AAPL"}]
    response.raise_for_status.return_value = None
    get = Mock(return_value=response)
    monkeypatch.setattr(data_client._fmp_session, "get", get)
    observation = SchedulerObservation("test")

    with fmp_request_budget(1), activate_scheduler_observation(observation):
        result = data_client._fmp_get("income-statement", {"symbol": "AAPL"})

    assert result == [{"symbol": "AAPL"}]
    assert json.loads(ledger_path.read_text(encoding="utf-8")) == {
        "window_start": _FMP_TEST_WINDOW,
        "count": 1,
    }
    assert observation.to_receipt()["service_health"] == "healthy"


@pytest.mark.parametrize("status_code", [402, 403, 404, 429])
def test_fmp_provider_refusal_statuses_are_distinct_failures(
    status_code: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ledger_path = _prepare_observed_fmp_ledger(monkeypatch, tmp_path)
    response = Mock(status_code=status_code)
    get = Mock(return_value=response)
    monkeypatch.setattr(data_client._fmp_session, "get", get)
    observation = SchedulerObservation("test")

    with fmp_request_budget(1), activate_scheduler_observation(observation):
        result = data_client._fmp_get("income-statement", {"symbol": "AAPL"})
        reason = data_client.fmp_request_deferral_reason()

    receipt = observation.to_receipt()
    assert result == []
    assert reason == "provider_refusal"
    get.assert_called_once()
    assert json.loads(ledger_path.read_text(encoding="utf-8"))["count"] == 1
    assert receipt["service_health"] == "failed"
    assert receipt["issues"][-1]["details"]["http_status"] == status_code


def test_fmp_transport_error_is_not_reported_as_quota_deferral(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ledger_path = _prepare_observed_fmp_ledger(monkeypatch, tmp_path)
    get = Mock(side_effect=requests.exceptions.Timeout("offline"))
    monkeypatch.setattr(data_client._fmp_session, "get", get)
    observation = SchedulerObservation("test")

    with fmp_request_budget(1), activate_scheduler_observation(observation):
        result = data_client._fmp_get("income-statement", {"symbol": "AAPL"})
        reason = data_client.fmp_request_deferral_reason()

    assert result == []
    assert reason == "transport_error"
    get.assert_called_once()
    assert json.loads(ledger_path.read_text(encoding="utf-8"))["count"] == 1
    assert observation.to_receipt()["service_health"] == "failed"
