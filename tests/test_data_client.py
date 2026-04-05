"""Tests for I/O boundary code — API calls are mocked to prevent network hits."""

import os
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import enhanced_scanner
from core.data_client import validate_ticker

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
    assert not os.path.exists(out_file), (
        "CSV file must not be created when the opportunities list is empty"
    )


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
