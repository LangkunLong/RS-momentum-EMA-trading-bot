"""Tests for pure CANSLIM business logic — no API calls required.

Tests marked with @pytest.mark.integration make real HTTP requests and are
skipped by default. Run them explicitly with: pytest -m integration
"""

import inspect
import math
from unittest.mock import patch

import pytest
import pandas as pd

import quality_stocks
from core import momentum_analysis
from core.canslim import a_annual_earnings, n_new_products
from core.canslim.core import _approximate_buy_point
from core.canslim.earnings_trace import PIT_PUBLIC_DATES_ATTR, TraceReason

# ─── Index routing ───────────────────────────────────────────────────────────


def test_index_aliases_contain_large_cap() -> None:
    """large_cap alias must map to both sp500 and nasdaq100 in the routing table."""
    assert "large_cap" in quality_stocks.INDEX_ALIASES
    mapped = quality_stocks.INDEX_ALIASES["large_cap"]
    assert "sp500" in mapped
    assert "nasdaq100" in mapped


def test_index_routing_large_cap_alias_resolves() -> None:
    """The large-cap route combines and deduplicates S&P 500 and Nasdaq-100 symbols."""
    index_members = {
        "sp500": ["AAPL", "MSFT"],
        "nasdaq100": ["MSFT", "NVDA"],
    }

    with patch(
        "quality_stocks._fetch_single_index",
        side_effect=lambda index, _force_refresh: index_members[index],
    ):
        tickers = quality_stocks.get_index_tickers("large_cap")
        stocks = quality_stocks.get_quality_stock_list(sectors=["large_cap"])

    assert tickers == ["AAPL", "MSFT", "NVDA"]
    assert stocks == tickers


# ─── Module import integrity ──────────────────────────────────────────────────


def test_get_sp500_tickers_imported_from_index_fetcher() -> None:
    """momentum_analysis.get_sp500_tickers must be sourced from index_ticker_fetcher.

    This guards against the function being re-implemented inline in
    momentum_analysis, which would break the centralized caching strategy.
    """
    assert hasattr(momentum_analysis, "get_sp500_tickers"), (
        "get_sp500_tickers is missing from momentum_analysis entirely"
    )
    source_module = inspect.getmodule(momentum_analysis.get_sp500_tickers)
    assert source_module is not None
    assert "index_ticker_fetcher" in source_module.__name__, (
        f"get_sp500_tickers is defined in {source_module.__name__!r}, expected 'index_ticker_fetcher'"
    )


# ─── _safe_growth — a_annual_earnings ────────────────────────────────────────


def test_safe_growth_rejects_negative_previous_annual() -> None:
    """Transitioning from a loss to a profit must return None, not a misleading % gain."""
    assert a_annual_earnings._safe_growth(1.0, -1.0) is None
    assert a_annual_earnings._safe_growth(0.5, -0.1) is None


def test_safe_growth_returns_none_for_zero_previous_annual() -> None:
    """Zero previous earnings must return None to avoid division-by-zero growth."""
    assert a_annual_earnings._safe_growth(1.0, 0) is None
    assert a_annual_earnings._safe_growth(1.0, None) is None


def test_safe_growth_positive_control_annual() -> None:
    """Valid positive-to-positive growth must return the correct decimal rate."""
    result = a_annual_earnings._safe_growth(1.25, 1.0)
    assert result is not None
    assert abs(result - 0.25) < 1e-9, f"Expected 0.25, got {result}"


def test_annual_trace_normalizes_nonfinite_selected_value() -> None:
    """A selected non-finite observation must not escape into diagnostic JSON."""
    annual_income = pd.DataFrame(
        {
            pd.Timestamp("2023-12-31"): [1.0],
            pd.Timestamp("2024-12-31"): [float("inf")],
        },
        index=["Diluted EPS"],
    )
    annual_income.attrs[PIT_PUBLIC_DATES_ATTR] = {
        "2023-12-31": "2024-02-15",
        "2024-12-31": "2025-02-15",
    }

    trace = a_annual_earnings.evaluate_a_with_trace(annual_income)

    assert trace.terminal_reason is TraceReason.NONFINITE_CURRENT_VALUE
    assert trace.current_value is None
    assert trace.prior_value == 1.0


def test_annual_trace_rejects_nonfinite_roe_without_score_credit() -> None:
    """A non-finite ROE must be absent and must not inflate the A score."""
    annual_income = pd.DataFrame(
        {
            pd.Timestamp("2023-12-31"): [1.0, 100.0],
            pd.Timestamp("2024-12-31"): [1.3, float("inf")],
        },
        index=["Diluted EPS", "Net Income"],
    )
    balance_sheet = pd.DataFrame(
        {
            pd.Timestamp("2023-12-31"): [100.0],
            pd.Timestamp("2024-12-31"): [100.0],
        },
        index=["Stockholders Equity"],
    )

    without_roe = a_annual_earnings.evaluate_a_with_trace(annual_income)
    trace = a_annual_earnings.evaluate_a_with_trace(
        annual_income, balance_sheet=balance_sheet
    )
    legacy_score, legacy_growth, legacy_roe = a_annual_earnings.evaluate_a(
        annual_income, balance_sheet=balance_sheet
    )

    assert trace.roe is None
    assert trace.score == without_roe.score
    assert math.isfinite(trace.score)
    assert legacy_score == pytest.approx(0.8)
    assert legacy_growth == pytest.approx(0.3)
    assert math.isinf(legacy_roe)


def test_annual_trace_normalizes_score_while_legacy_keeps_raw_nan() -> None:
    """Trace JSON stays finite without changing the base-compatible tuple API."""
    annual_income = pd.DataFrame(
        {
            pd.Timestamp("2023-12-31"): [1.0],
            pd.Timestamp("2024-12-31"): [1.3],
        },
        index=["Diluted EPS"],
    )

    trace = a_annual_earnings.evaluate_a_with_trace(
        annual_income, a_growth_target=float("nan")
    )
    legacy_score, legacy_growth, legacy_roe = a_annual_earnings.evaluate_a(
        annual_income, a_growth_target=float("nan")
    )

    assert trace.score == 0.0
    assert trace.annual_growth == pytest.approx(0.3)
    assert trace.roe is None
    assert math.isnan(legacy_score)
    assert legacy_growth == pytest.approx(0.3)
    assert legacy_roe is None


# ─── _safe_growth — n_new_products ───────────────────────────────────────────


def test_safe_growth_rejects_negative_previous_n() -> None:
    """n_new_products must apply the same negative-previous guard as a_annual_earnings."""
    assert n_new_products._safe_growth(1.0, -1.0) is None
    assert n_new_products._safe_growth(0.5, -0.1) is None


def test_safe_growth_returns_none_for_zero_previous_n() -> None:
    """Zero previous in n_new_products must return None."""
    assert n_new_products._safe_growth(1.0, 0) is None
    assert n_new_products._safe_growth(1.0, None) is None


def test_safe_growth_positive_control_n() -> None:
    """n_new_products positive growth must return the correct decimal rate."""
    result = n_new_products._safe_growth(1.25, 1.0)
    assert result is not None
    assert abs(result - 0.25) < 1e-9, f"Expected 0.25, got {result}"


def test_rs_cache_requires_broad_universe_and_requested_symbols() -> None:
    """A tiny same-day cache should not be reused for a broad-market RS scan."""
    broad_df = momentum_analysis.pd.DataFrame(
        {
            "Ticker": [f"T{i}" for i in range(401)],
            "Weighted_Perf": [0.1] * 401,
            "RS_Score": [50.0] * 401,
        }
    )
    broad_df.loc[0, "Ticker"] = "AAPL"
    broad_df.loc[1, "Ticker"] = "MSFT"

    tiny_df = momentum_analysis.pd.DataFrame(
        {
            "Ticker": ["AAPL", "MSFT"],
            "Weighted_Perf": [0.2, 0.1],
            "RS_Score": [90.0, 80.0],
        }
    )

    assert momentum_analysis._cache_covers_requested_universe(broad_df, ["AAPL", "MSFT"]) is True
    assert momentum_analysis._cache_covers_requested_universe(tiny_df, ["AAPL", "MSFT"]) is False


def test_buy_point_uses_prior_high_not_current_breakout_close() -> None:
    closes = pd.Series([90.0, 95.0, 100.0, 104.0])

    buy_point = _approximate_buy_point(closes, is_breakout=True, lookback_252=len(closes))

    assert buy_point == pytest.approx(100.0)


def test_buy_point_absent_without_breakout() -> None:
    closes = pd.Series([90.0, 95.0, 100.0, 104.0])

    buy_point = _approximate_buy_point(closes, is_breakout=False, lookback_252=len(closes))

    assert buy_point is None
