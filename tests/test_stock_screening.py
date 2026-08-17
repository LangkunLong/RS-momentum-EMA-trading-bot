"""Tests for scanner classification between actionable buys and watchlist names."""

from core.canslim.m_market_direction import MarketTrend
from core.stock_screening import _classify_canslim_candidate


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
