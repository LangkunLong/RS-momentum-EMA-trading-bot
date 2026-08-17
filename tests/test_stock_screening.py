"""Tests for scanner classification between actionable buys and watchlist names."""

from unittest.mock import patch

import pandas as pd

from core.canslim import evaluate_canslim
from core.canslim.m_market_direction import MarketTrend
from core.stock_screening import (
    _classify_canslim_candidate,
    evaluate_stock_canslim,
    screen_stocks_canslim_detailed,
)


def _make_view(
    *,
    rs_score: float = 80.0,
    total_score: float = 70.0,
    is_bullish: bool = True,
    has_fundamentals: bool = True,
    is_breakout: bool = True,
    has_volume_surge: bool = True,
    buy_point: float | None = 100.0,
    latest_close_price: float | None = 103.0,
    fmp_quota_deferred: bool = False,
) -> dict:
    return {
        "rs_score": rs_score,
        "total_score": total_score,
        "metrics": {
            "has_fundamentals": has_fundamentals,
            "fmp_quota_deferred": fmp_quota_deferred,
        },
        "market_trend": MarketTrend(
            symbol="SPY",
            score=0.8 if is_bullish else 0.1,
            is_bullish=is_bullish,
            latest_close=500.0,
            indicators={"ema_21": 495.0, "ema_50": 490.0, "ema_200": 470.0},
            distribution_days=1 if is_bullish else 6,
            follow_through=is_bullish,
        ),
        "is_breakout": is_breakout,
        "has_volume_surge": has_volume_surge,
        "buy_point": buy_point,
        "latest_close_price": latest_close_price,
    }


def test_classifier_marks_bullish_high_score_name_as_actionable_buy() -> None:
    category, notes = _classify_canslim_candidate(
        _make_view(),
        min_rs_score=75,
        min_canslim_score=65,
        watchlist_min_score=45,
        require_bullish_market=True,
    )

    assert category == "actionable_buy"
    assert notes == []


def test_classifier_marks_bearish_market_name_as_watchlist() -> None:
    category, notes = _classify_canslim_candidate(
        _make_view(is_bullish=False, has_fundamentals=False),
        min_rs_score=75,
        min_canslim_score=65,
        watchlist_min_score=45,
        require_bullish_market=True,
    )

    assert category == "watchlist_candidate"
    assert "market_not_bullish" in notes
    assert "missing_fundamentals" in notes


def test_classifier_marks_bullish_missing_fundamentals_name_as_watchlist() -> None:
    category, notes = _classify_canslim_candidate(
        _make_view(is_bullish=True, has_fundamentals=False),
        min_rs_score=75,
        min_canslim_score=65,
        watchlist_min_score=45,
        require_bullish_market=True,
    )

    assert category == "watchlist_candidate"
    assert "missing_fundamentals" in notes


def test_classifier_marks_budget_skipped_name_as_quota_deferred() -> None:
    """A budget skip is explicit and can never become actionable or watchlisted."""
    category, notes = _classify_canslim_candidate(
        _make_view(fmp_quota_deferred=True, has_fundamentals=False),
        min_rs_score=75,
        min_canslim_score=65,
        watchlist_min_score=45,
        require_bullish_market=True,
    )

    assert category == "quota_deferred"
    assert notes == ["quota_deferred"]


def test_classifier_rejects_name_below_watchlist_floor() -> None:
    category, notes = _classify_canslim_candidate(
        _make_view(total_score=40.0, is_bullish=False, has_fundamentals=False),
        min_rs_score=75,
        min_canslim_score=65,
        watchlist_min_score=45,
        require_bullish_market=True,
    )

    assert category == "rejected"
    assert notes == ["below_watchlist_score"]


# ---------------------------------------------------------------------------
# RS threshold boundary tests
# ---------------------------------------------------------------------------


def test_rs_exactly_at_threshold_is_not_rejected() -> None:
    """RS equal to the minimum threshold must pass the pre-filter."""
    category, notes = _classify_canslim_candidate(
        _make_view(rs_score=75.0),
        min_rs_score=75,
        min_canslim_score=65,
        watchlist_min_score=30,
        require_bullish_market=False,
    )
    assert category == "actionable_buy"
    assert "below_rs_threshold" not in notes


def test_rs_one_tenth_below_threshold_is_rejected() -> None:
    """RS fractionally below the minimum must be rejected immediately."""
    category, notes = _classify_canslim_candidate(
        _make_view(rs_score=74.9),
        min_rs_score=75,
        min_canslim_score=65,
        watchlist_min_score=30,
        require_bullish_market=False,
    )
    assert category == "rejected"
    assert notes == ["below_rs_threshold"]


def test_rs_well_above_threshold_passes() -> None:
    """RS comfortably above the minimum (e.g. 90) must not be rejected on RS."""
    category, _ = _classify_canslim_candidate(
        _make_view(rs_score=90.0),
        min_rs_score=80,
        min_canslim_score=65,
        watchlist_min_score=30,
        require_bullish_market=False,
    )
    assert category == "actionable_buy"


def test_strict_breakout_blocks_non_breakout_name_from_actionable_buys() -> None:
    category, notes = _classify_canslim_candidate(
        _make_view(is_breakout=False),
        min_rs_score=75,
        min_canslim_score=65,
        watchlist_min_score=30,
        require_bullish_market=True,
    )

    assert category == "watchlist_candidate"
    assert "not_in_breakout" in notes


def test_strict_breakout_blocks_missing_pivot_from_actionable_buys() -> None:
    category, notes = _classify_canslim_candidate(
        _make_view(buy_point=None, latest_close_price=None),
        min_rs_score=75,
        min_canslim_score=65,
        watchlist_min_score=30,
        require_bullish_market=True,
    )

    assert category == "watchlist_candidate"
    assert "missing_buy_point" in notes


def test_strict_breakout_blocks_price_above_buy_zone_from_actionable_buys() -> None:
    category, notes = _classify_canslim_candidate(
        _make_view(buy_point=100.0, latest_close_price=106.0),
        min_rs_score=75,
        min_canslim_score=65,
        watchlist_min_score=30,
        require_bullish_market=True,
    )

    assert category == "watchlist_candidate"
    assert "beyond_buy_zone" in notes


def test_quota_deferred_candidate_survives_strict_breakout_filter() -> None:
    """Deferred names must remain visible even if technical entry gates are not met."""
    view = _make_view(
        has_fundamentals=False,
        fmp_quota_deferred=True,
        is_breakout=False,
        has_volume_surge=False,
        buy_point=None,
        latest_close_price=95.0,
    )
    rs_scores = pd.DataFrame([{"Ticker": "AAPL", "RS_Score": 80.0}])

    with patch("core.stock_screening.evaluate_canslim", return_value=view):
        result = evaluate_stock_canslim(
            symbol="AAPL",
            min_rs_score=75,
            min_canslim_score=65,
            market_trend=view["market_trend"],
            rs_scores_df=rs_scores,
            strict_breakout=True,
        )

    assert result is view
    assert result["scanner_category"] == "quota_deferred"


def test_scanner_evaluates_highest_rs_candidates_first() -> None:
    """The scarce free-tier request budget goes to the strongest RS names first."""
    rs_scores = pd.DataFrame(
        [
            {"Ticker": "LOW", "RS_Score": 81.0},
            {"Ticker": "HIGH", "RS_Score": 98.0},
            {"Ticker": "MID", "RS_Score": 90.0},
        ]
    )
    market = _make_view()["market_trend"]
    evaluated: list[str] = []

    def fake_evaluate(*, symbol: str, **_kwargs) -> None:
        evaluated.append(symbol)
        return None

    with (
        patch("core.stock_screening.evaluate_market_direction", return_value=market),
        patch("core.stock_screening.calculate_rs_scores_for_tickers", return_value=rs_scores),
        patch("core.stock_screening.evaluate_stock_canslim", side_effect=fake_evaluate),
        patch("core.stock_screening.MAX_WORKERS", 1),
    ):
        screen_stocks_canslim_detailed(
            symbols=["LOW", "HIGH", "MID"],
            start_date="2026-01-01",
            min_rs_score=80,
        )

    assert evaluated == ["HIGH", "MID", "LOW"]


def test_scanner_reports_and_excludes_quota_deferred_names(capsys) -> None:
    """Deferred symbols get an explicit count and stay out of both output lists."""
    rs_scores = pd.DataFrame([{"Ticker": "AAPL", "RS_Score": 95.0}])
    market = _make_view()["market_trend"]
    deferred = _make_view(fmp_quota_deferred=True, has_fundamentals=False)
    deferred.update(
        {
            "symbol": "AAPL",
            "scanner_category": "quota_deferred",
            "scanner_notes": ["quota_deferred"],
        }
    )

    with (
        patch("core.stock_screening.evaluate_market_direction", return_value=market),
        patch("core.stock_screening.calculate_rs_scores_for_tickers", return_value=rs_scores),
        patch("core.stock_screening.evaluate_stock_canslim", return_value=deferred),
    ):
        buys, watchlist, _ = screen_stocks_canslim_detailed(
            symbols=["AAPL"],
            start_date="2026-01-01",
            min_rs_score=80,
        )

    assert buys == []
    assert watchlist == []
    assert "1 candidate(s) quota_deferred" in capsys.readouterr().out


def test_canslim_marks_missing_statements_as_quota_deferred() -> None:
    """A request-boundary denial must propagate into the scanner-facing metrics."""
    dates = pd.bdate_range("2026-01-01", periods=60)
    prices = pd.DataFrame(
        {
            "Open": [100.0] * 60,
            "High": [101.0] * 60,
            "Low": [99.0] * 60,
            "Close": [100.0] * 60,
            "Volume": [1_000_000.0] * 60,
        },
        index=dates,
    )
    market = _make_view()["market_trend"]
    rs_scores = pd.DataFrame([{"Ticker": "AAPL", "RS_Score": 90.0}])
    neutral_company = {
        "shares_outstanding": None,
        "held_percent_institutions": None,
        "institution_count": None,
        "prev_institution_count": None,
    }

    with (
        patch("core.canslim.core.fetch_company_info", return_value=neutral_company),
        patch("core.canslim.core.fetch_quarterly_income_statement", return_value=pd.DataFrame()),
        patch("core.canslim.core.fetch_annual_income_statement", return_value=pd.DataFrame()),
        patch("core.canslim.core.fetch_balance_sheet", return_value=pd.DataFrame()),
        patch("core.canslim.core.fetch_ohlcv", return_value=prices),
        patch("core.canslim.core.fmp_request_was_deferred", return_value=True, create=True),
        patch("core.canslim.core.reset_fmp_request_context", create=True),
    ):
        result = evaluate_canslim("AAPL", rs_scores_df=rs_scores, market_trend=market)

    assert result is not None
    assert result["metrics"]["fmp_quota_deferred"] is True
