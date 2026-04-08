"""Tests for scanner classification between actionable buys and watchlist names."""

from core.canslim.m_market_direction import MarketTrend
from core.stock_screening import _classify_canslim_candidate


def _make_view(
    *,
    rs_score: float = 80.0,
    total_score: float = 70.0,
    is_bullish: bool = True,
    has_fundamentals: bool = True,
    is_breakout: bool = False,
    has_volume_surge: bool = False,
) -> dict:
    return {
        "rs_score": rs_score,
        "total_score": total_score,
        "metrics": {"has_fundamentals": has_fundamentals},
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
