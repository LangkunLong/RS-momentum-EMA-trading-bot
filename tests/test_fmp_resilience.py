"""Tests for FMP API resilience: graceful degradation on quota errors and disk cache."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import requests

from core.data_client import (
    _fund_cache_get,
    _fund_cache_set,
    _fmp_get,
    fetch_quarterly_income_statement,
)
from core.canslim.c_current_earnings import evaluate_c


# ─── _fmp_get error handling ─────────────────────────────────────────────────


def test_fmp_get_returns_empty_on_402() -> None:
    """_fmp_get must return [] when the server responds with HTTP 402 (free-tier gate)."""
    mock_resp = MagicMock()
    mock_resp.status_code = 402

    with patch("core.data_client._fmp_session") as mock_session:
        mock_session.get.return_value = mock_resp
        result = _fmp_get("income-statement", {"symbol": "FAKE"})

    assert result == [], "Expected [] on HTTP 402, got non-empty result"


def test_fmp_get_returns_empty_on_403() -> None:
    """_fmp_get must return [] when the server responds with HTTP 403 (forbidden)."""
    mock_resp = MagicMock()
    mock_resp.status_code = 403

    with patch("core.data_client._fmp_session") as mock_session:
        mock_session.get.return_value = mock_resp
        result = _fmp_get("income-statement", {"symbol": "FAKE"})

    assert result == [], "Expected [] on HTTP 403, got non-empty result"


def test_fmp_get_returns_empty_on_retry_error() -> None:
    """_fmp_get must return [] when the retry adapter exhausts all 429 attempts.

    RetryError is raised before a response object exists, so it cannot be caught
    via status_code inspection — it must be caught at the requests.get() call site.
    """
    with patch("core.data_client._fmp_session") as mock_session:
        mock_session.get.side_effect = requests.exceptions.RetryError("too many 429s")
        result = _fmp_get("income-statement", {"symbol": "FAKE"})

    assert result == [], "Expected [] on RetryError, got non-empty result"


def test_fmp_get_returns_empty_on_connection_error() -> None:
    """_fmp_get must return [] on network-level ConnectionError."""
    with patch("core.data_client._fmp_session") as mock_session:
        mock_session.get.side_effect = requests.exceptions.ConnectionError("network down")
        result = _fmp_get("income-statement", {"symbol": "FAKE"})

    assert result == [], "Expected [] on ConnectionError, got non-empty result"


# ─── Disk fundamentals cache ─────────────────────────────────────────────────


def test_fund_cache_round_trip(tmp_path: Path) -> None:
    """A DataFrame stored via _fund_cache_set must be retrievable via _fund_cache_get."""
    df = pd.DataFrame({"Diluted EPS": [1.0, 2.0]}, index=["2024-01-01", "2024-04-01"])
    key = ("quarterly_income", "TESTCACHE", 5)

    with patch("core.data_client._FUND_CACHE_DIR", str(tmp_path)):
        _fund_cache_set(key, df)
        result = _fund_cache_get(key)

    assert result is not None, "Expected cache HIT after writing"
    pd.testing.assert_frame_equal(result, df)


def test_fund_cache_miss_on_nonexistent_key(tmp_path: Path) -> None:
    """_fund_cache_get must return None when no cached file exists."""
    key = ("quarterly_income", "NOCACHE", 5)

    with patch("core.data_client._FUND_CACHE_DIR", str(tmp_path)):
        result = _fund_cache_get(key)

    assert result is None, "Expected cache MISS for key that was never written"


def test_fund_cache_miss_on_expired_file(tmp_path: Path) -> None:
    """_fund_cache_get must return None when the cached file is older than the TTL."""
    df = pd.DataFrame({"col": [1, 2]})
    key = ("quarterly_income", "EXPIREDCACHE", 5)

    # Write cache file then backdate its mtime past the 24-hour TTL
    with patch("core.data_client._FUND_CACHE_DIR", str(tmp_path)):
        _fund_cache_set(key, df)
        # Find the file and set its mtime to 25 hours ago
        from core.data_client import _fund_cache_path
        with patch("core.data_client._FUND_CACHE_DIR", str(tmp_path)):
            cache_file = Path(str(tmp_path)) / Path(_fund_cache_path(key)).name
        if cache_file.exists():
            stale_mtime = time.time() - (25 * 3600)
            import os
            os.utime(str(cache_file), (stale_mtime, stale_mtime))
            with patch("core.data_client._FUND_CACHE_DIR", str(tmp_path)):
                result = _fund_cache_get(key)
            assert result is None, "Expected cache MISS for stale file (>24h old)"


def test_fetch_quarterly_uses_disk_cache_on_second_call(tmp_path: Path) -> None:
    """Second call to fetch_quarterly_income_statement must use disk cache, not the API."""
    sample_records = [
        {
            "date": "2024-12-31",
            "epsDiluted": 2.5,
            "eps": 2.5,
            "revenue": 100_000_000,
            "netIncome": 20_000_000,
            "grossProfit": 50_000_000,
            "operatingIncome": 30_000_000,
            "costOfRevenue": 50_000_000,
        }
    ]

    with (
        patch("core.data_client._FUND_CACHE_DIR", str(tmp_path)),
        patch("core.data_client._fmp_get", return_value=sample_records) as mock_api,
    ):
        # First call: hits API and populates disk cache
        df1 = fetch_quarterly_income_statement("TESTSTOCK", limit=5)
        assert mock_api.call_count == 1

        # Clear in-memory session cache to force disk cache lookup
        from core.data_client import clear_session_cache
        clear_session_cache()

        # Second call: should hit disk cache, NOT the API
        df2 = fetch_quarterly_income_statement("TESTSTOCK", limit=5)
        assert mock_api.call_count == 1, "API was called again despite disk cache being populated"
        pd.testing.assert_frame_equal(df1, df2)


# ─── C score 4-quarter YoY fallback ─────────────────────────────────────────


def _make_quarterly_df(eps_values: list[float], dates: list[str]) -> pd.DataFrame:
    """Build a minimal quarterly income DataFrame matching the parser's output format."""
    data = {pd.Timestamp(d): {"Diluted EPS": v} for d, v in zip(dates, eps_values, strict=True)}
    return pd.DataFrame(data)


def test_c_score_four_quarter_fallback_valid_yoy() -> None:
    """evaluate_c must compute a score when exactly 4 quarters span >= 330 days (free-tier path)."""
    # Q4 2023 through Q3 2024 — spans ~270 days, then Q4 2024 makes it ~365 days
    df = _make_quarterly_df(
        eps_values=[1.00, 1.10, 1.20, 1.42],
        dates=["2023-12-31", "2024-03-31", "2024-06-30", "2024-12-31"],  # ~366-day span
    )

    score, growth = evaluate_c(df)

    assert score > 0, "Expected non-zero C score for valid 4-quarter data with 42% YoY growth"
    assert growth is not None
    assert abs(growth - 0.42) < 0.01, f"Expected ~42% growth, got {growth:.2%}"


def test_c_score_four_quarter_fallback_too_short_span() -> None:
    """evaluate_c must return 0 when 4 quarters span < 330 days (no valid YoY)."""
    # Quarterly data that only spans ~270 days — not a valid YoY comparison
    df = _make_quarterly_df(
        eps_values=[1.00, 1.10, 1.20, 1.30],
        dates=["2024-01-31", "2024-04-30", "2024-07-31", "2024-10-31"],  # ~274-day span
    )

    score, growth = evaluate_c(df)

    assert score == 0.0, "Expected 0 score when 4-quarter span is too short for valid YoY"


def test_c_score_missing_data_returns_zero() -> None:
    """evaluate_c must return (0.0, None) for an empty DataFrame."""
    score, growth = evaluate_c(pd.DataFrame())
    assert score == 0.0
    assert growth is None


def test_c_score_four_quarter_fallback_boundary_at_330_days() -> None:
    """evaluate_c should accept a span of exactly 330 days as a valid YoY."""
    import datetime

    start = datetime.date(2024, 1, 1)
    end = start + datetime.timedelta(days=330)

    df = _make_quarterly_df(
        eps_values=[1.00, 1.10, 1.20, 1.50],
        dates=[str(start), "2024-04-01", "2024-07-01", str(end)],
    )

    score, growth = evaluate_c(df)

    assert score > 0, "Expected non-zero score at exactly 330-day boundary"
