"""Tests for I/O boundary code — API calls are mocked to prevent network hits."""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
from alpaca.data.enums import DataFeed

import enhanced_scanner
from core.data_client import fetch_bulk_ohlcv, fetch_ohlcv, validate_ticker

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
