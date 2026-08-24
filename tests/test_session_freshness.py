from __future__ import annotations

from datetime import date
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

import backtest
from config import settings
from core.backtest_engine import CanslimStrategy, _calculate_rs_snapshot
from core.canslim import evaluate_canslim
from core.canslim.m_market_direction import MarketTrend, evaluate_m
from core.momentum_analysis import calculate_rs_scores_for_tickers
from core.stock_screening import screen_stocks_canslim_detailed
from core.trading_sessions import (
    exact_session_row,
    history_through_exact_session,
    latest_us_equity_session,
)


def _eligible_history(*, end: str | pd.Timestamp, periods: int = 61) -> pd.DataFrame:
    dates = pd.bdate_range(end=end, periods=periods)
    closes = [100.0] * (periods - 1) + [102.0]
    volumes = [1_000.0] * (periods - 1) + [1_300.0]
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [value + 1.0 for value in closes],
            "Low": [value - 1.0 for value in closes],
            "Close": closes,
            "Volume": volumes,
        },
        index=dates,
    )


def _fundamentals() -> dict[str, object]:
    return {
        "quarterly_income": pd.DataFrame(),
        "annual_income": pd.DataFrame(),
        "balance_sheet": pd.DataFrame(),
        "company_info": {
            "shares_outstanding": None,
            "held_percent_institutions": None,
            "institution_count": None,
            "prev_institution_count": None,
        },
    }


def _market(*, as_of_session: date | None = None) -> MarketTrend:
    market = MarketTrend(
        symbol="SPY",
        score=0.9,
        is_bullish=True,
        latest_close=500.0,
        indicators={},
    )
    # Keep this regression importable before MarketTrend grows the advisory field.
    market.as_of_session = as_of_session
    return market


def test_session_helpers_sort_and_collapse_duplicate_daily_labels() -> None:
    """Break caught: unsorted duplicate daily rows became false prior-session facts."""
    history = pd.DataFrame(
        {"Close": [300.0, 301.0, 100.0]},
        index=pd.DatetimeIndex(
            [
                "2026-08-24 10:00:00",
                "2026-08-24 16:00:00",
                "2026-08-21 16:00:00",
            ]
        ),
    )

    canonical = history_through_exact_session(history, date(2026, 8, 24))
    exact = exact_session_row(history, date(2026, 8, 24))

    assert canonical is not None
    assert canonical.index.tolist() == [pd.Timestamp("2026-08-21"), pd.Timestamp("2026-08-24")]
    assert canonical["Close"].tolist() == [100.0, 301.0]
    assert exact is not None and exact["Close"] == 301.0
    assert latest_us_equity_session(history) == date(2026, 8, 24)


def test_session_helpers_keep_latest_intraday_bar_when_duplicates_are_reversed() -> None:
    """Break caught: normalizing before sorting retained an older same-session bar."""
    history = pd.DataFrame(
        {"Close": [301.0, 300.0, 100.0]},
        index=pd.DatetimeIndex(
            [
                "2026-08-24 16:00:00",
                "2026-08-24 10:00:00",
                "2026-08-21 16:00:00",
            ],
            tz="America/New_York",
        ),
    )

    canonical = history_through_exact_session(history, date(2026, 8, 24))
    exact = exact_session_row(history, date(2026, 8, 24))

    assert canonical is not None
    assert canonical.index.tolist() == [pd.Timestamp("2026-08-21"), pd.Timestamp("2026-08-24")]
    assert canonical["Close"].tolist() == [100.0, 301.0]
    assert exact is not None and exact["Close"] == 301.0
    assert history.index[0] == pd.Timestamp("2026-08-24 16:00:00", tz="America/New_York")


def test_session_helpers_materialize_a_canonical_index_from_date_strings() -> None:
    """A parsed fast path must not return the caller's non-DatetimeIndex unchanged."""
    history = pd.DataFrame(
        {"Close": [100.0, 101.0]},
        index=pd.Index(["2026-08-21", "2026-08-24"]),
    )

    canonical = history_through_exact_session(history, date(2026, 8, 24))
    exact = exact_session_row(history, date(2026, 8, 24))

    assert canonical is not None
    assert isinstance(canonical.index, pd.DatetimeIndex)
    assert canonical.index.tolist() == [pd.Timestamp("2026-08-21"), pd.Timestamp("2026-08-24")]
    assert exact is not None and exact["Close"] == 101.0
    assert latest_us_equity_session(history) == date(2026, 8, 24)


def test_pit_evaluation_skips_stale_eligible_bar_before_fundamentals() -> None:
    """Break caught: PIT reused Friday's eligible bar as Monday and queried fundamentals."""
    event_session = pd.Timestamp("2026-08-21")
    evaluation_session = pd.Timestamp("2026-08-24")
    history = _eligible_history(end=event_session)
    provider_calls: list[tuple[str, pd.Timestamp]] = []

    def provider(symbol: str, as_of: pd.Timestamp) -> dict[str, object]:
        provider_calls.append((symbol, as_of))
        return _fundamentals()

    strategy = CanslimStrategy(fundamental_provider=provider)
    row = strategy.evaluate_symbol(
        ticker="LEAD",
        ticker_ohlcv={"LEAD": history},
        all_closes=pd.DataFrame({"LEAD": history["Close"]}, index=history.index),
        eval_date=evaluation_session,
        market_state={"m_score": 0.9, "market_is_bullish": True},
        rs_score=95.0,
    )

    assert row is None
    assert provider_calls == []


def test_simple_backtest_skips_stale_eligible_bar_before_fundamentals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: the simple loop emitted Monday from Friday's bar and fetched fundamentals."""
    evaluation_session = pd.Timestamp("2026-08-24")
    history = _eligible_history(end="2026-08-21")
    spy = pd.DataFrame(
        {"Open": [500.0], "High": [501.0], "Low": [499.0], "Close": [500.0], "Volume": [1_000.0]},
        index=pd.DatetimeIndex([evaluation_session]),
    )
    fundamental_calls: list[tuple[str, pd.Timestamp]] = []

    def fundamentals(symbol: str, as_of: pd.Timestamp) -> dict[str, object]:
        fundamental_calls.append((symbol, as_of))
        return {
            "c_score": 1.0,
            "a_score": 1.0,
            "i_score": 0.5,
            "current_growth": 0.5,
            "annual_growth": 0.5,
            "shares_outstanding": None,
            "institutional_data_available": False,
        }

    monkeypatch.setattr(backtest, "BACKTEST_TICKERS", ["LEAD"])
    monkeypatch.setattr(backtest, "clear_session_cache", lambda: None)
    monkeypatch.setattr(backtest, "_download_price_data", lambda *_args, **_kwargs: {"LEAD": history, "SPY": spy})
    monkeypatch.setattr(
        backtest,
        "_download_bulk_closes",
        lambda *_args, **_kwargs: pd.DataFrame({"LEAD": history["Close"]}, index=history.index),
    )
    monkeypatch.setattr(backtest, "get_sp500_tickers", lambda: [])
    monkeypatch.setattr(backtest, "_evaluate_market_at_date", lambda *_args: (0.9, True, 0, False))
    monkeypatch.setattr(backtest, "_calculate_rs_at_date", lambda *_args: 95.0)
    monkeypatch.setattr(backtest, "_evaluate_fundamentals_at_date", fundamentals)

    result = backtest.run_backtest()

    assert result.empty
    assert fundamental_calls == []


def _rs_closes() -> tuple[pd.DataFrame, pd.Timestamp]:
    dates = pd.bdate_range(end="2026-08-24", periods=61)
    steps = np.arange(len(dates), dtype=float)
    columns = {
        "TARGET": 100.0 * np.power(1.010, steps),
        **{
            f"PEER{index}": 100.0 * np.power(1.001 + index / 1_000, steps)
            for index in range(9)
        },
        "STALE": 100.0 * np.power(1.020, steps),
    }
    frame = pd.DataFrame(columns, index=dates)
    frame.loc[dates[-1], "STALE"] = np.nan
    return frame, dates[-1]


def test_rs_rankings_exclude_peers_without_exact_evaluation_session(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break caught: stale high-return peers diluted both historical and live RS ranks."""
    closes, evaluation_session = _rs_closes()

    snapshot = _calculate_rs_snapshot(closes, evaluation_session)
    simple_score = backtest._calculate_rs_at_date(closes, "TARGET", evaluation_session)

    monkeypatch.setattr("core.momentum_analysis.get_sp500_tickers", lambda: [])
    monkeypatch.setattr("core.momentum_analysis.fetch_bulk_close_prices", lambda *_args, **_kwargs: closes)
    live_scores = calculate_rs_scores_for_tickers(
        list(closes.columns),
        cache_file=str(tmp_path / "rs.csv"),
        period="3mo",
    )

    expected_top_score = settings.RS_PERCENTILE_MIN + settings.RS_PERCENTILE_MULTIPLIER
    assert "STALE" not in snapshot
    assert snapshot["TARGET"] == pytest.approx(expected_top_score)
    assert simple_score == pytest.approx(expected_top_score)
    assert "STALE" not in set(live_scores["Ticker"])
    assert live_scores.loc[live_scores["Ticker"] == "TARGET", "RS_Score"].iloc[0] == pytest.approx(
        expected_top_score
    )


def test_market_evaluation_binds_the_latest_completed_session() -> None:
    """Break caught: live symbol checks had no SPY session provenance to compare against."""
    history = _eligible_history(end="2026-08-21", periods=260)

    market = evaluate_m(price_data=history)

    assert market.as_of_session == date(2026, 8, 21)


def test_live_weekend_scan_accepts_matching_friday_and_rejects_stale_before_fundamentals() -> None:
    """Break caught: a weekend scan paid for and evaluated a symbol whose last bar was Thursday."""
    friday_history = _eligible_history(end="2026-08-21", periods=260)
    thursday_history = _eligible_history(end="2026-08-20", periods=260)
    market = evaluate_m(price_data=friday_history)
    rs_scores = pd.DataFrame([{"Ticker": "LEAD", "RS_Score": 95.0}])
    company_info = _fundamentals()["company_info"]

    with (
        patch("core.canslim.core.fetch_ohlcv", side_effect=[friday_history, thursday_history]),
        patch("core.canslim.core.fetch_company_info", return_value=company_info) as company_fetch,
        patch("core.canslim.core.fetch_quarterly_income_statement", return_value=pd.DataFrame()),
        patch("core.canslim.core.fetch_annual_income_statement", return_value=pd.DataFrame()),
        patch("core.canslim.core.fetch_balance_sheet", return_value=pd.DataFrame()),
        patch("core.canslim.core.fmp_request_was_deferred", return_value=False),
        patch("core.canslim.core.reset_fmp_request_context"),
    ):
        matching = evaluate_canslim("LEAD", rs_scores_df=rs_scores, market_trend=market)
        stale = evaluate_canslim("LEAD", rs_scores_df=rs_scores, market_trend=market)

    assert matching is not None
    assert stale is None
    assert company_fetch.call_count == 1


def test_live_screen_propagates_market_session_to_rs_ranking() -> None:
    """Break caught: the live RS universe was ranked without the completed SPY session."""
    observed_sessions: list[object] = []
    market = _market(as_of_session=date(2026, 8, 21))

    def calculate(_symbols: list[str], **kwargs: object) -> pd.DataFrame:
        observed_sessions.append(kwargs.get("as_of_session"))
        return pd.DataFrame(columns=["Ticker", "Weighted_Perf", "RS_Score"])

    with (
        patch("core.stock_screening.evaluate_market_direction", return_value=market),
        patch("core.stock_screening.calculate_rs_scores_for_tickers", side_effect=calculate),
    ):
        screen_stocks_canslim_detailed(symbols=["LEAD"], start_date="2026-08-01")

    assert observed_sessions == [date(2026, 8, 21)]
