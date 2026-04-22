"""Tests for MarketRegimeTracker — O'Neil market regime state machine."""
from __future__ import annotations

import pandas as pd

from core.canslim.m_market_direction import MarketRegime, MarketRegimeTracker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_spy(
    closes: list[float],
    volumes: list[float],
    start: str = "2024-01-02",
) -> pd.DataFrame:
    """Build a minimal SPY OHLCV DataFrame for regime tests."""
    assert len(closes) == len(volumes)
    dates = pd.bdate_range(start=start, periods=len(closes))
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [c * 1.005 for c in closes],
            "Low": [c * 0.995 for c in closes],
            "Close": closes,
            "Volume": volumes,
        },
        index=dates,
    )


def _feed(
    tracker: MarketRegimeTracker,
    bars: list[tuple[float, float]],  # (close, volume) pairs
    start: str = "2024-01-02",
) -> MarketRegime:
    """Feed (close, volume) pairs through the tracker, return final regime."""
    dates = pd.bdate_range(start=start, periods=len(bars))
    for i in range(1, len(bars)):
        close, vol = bars[i]
        prev_close, prev_vol = bars[i - 1]
        tracker.update(dates[i], close, prev_close, vol, prev_vol)
    return tracker.regime


def _dist(prev_close: float, prev_vol: float) -> tuple[float, float]:
    """Return a distribution-day bar (down 0.3%, volume +50%)."""
    return prev_close * 0.997, prev_vol * 1.5


def _up(prev_close: float, prev_vol: float) -> tuple[float, float]:
    """Return a normal up bar (up 0.1%, volume −10%)."""
    return prev_close * 1.001, prev_vol * 0.9


def _ftd(prev_close: float, prev_vol: float) -> tuple[float, float]:
    """Return a follow-through day bar (up 1.6%, volume +20%)."""
    return prev_close * 1.016, prev_vol * 1.2


# ---------------------------------------------------------------------------
# Bootstrap tests
# ---------------------------------------------------------------------------

def test_bootstrap_above_200_ema_starts_uptrend() -> None:
    """SPY above 200-day EMA → bootstrap to CONFIRMED_UPTREND."""
    n = 210
    # Prices steadily rising — latest close is well above the 200-day EMA
    closes = [100.0 + i * 0.1 for i in range(n)]
    volumes = [1e8] * n
    spy_df = _make_spy(closes, volumes)
    tracker = MarketRegimeTracker()
    tracker.bootstrap(spy_df, spy_df.index[-1])
    assert tracker.regime == MarketRegime.CONFIRMED_UPTREND


def test_bootstrap_below_200_ema_starts_correction() -> None:
    """SPY below 200-day EMA → bootstrap to CORRECTION."""
    n = 210
    # Prices declining — latest close is below the 200-day EMA
    closes = [200.0 - i * 0.5 for i in range(n)]
    volumes = [1e8] * n
    spy_df = _make_spy(closes, volumes)
    tracker = MarketRegimeTracker()
    tracker.bootstrap(spy_df, spy_df.index[-1])
    assert tracker.regime == MarketRegime.CORRECTION


# ---------------------------------------------------------------------------
# Distribution day tests
# ---------------------------------------------------------------------------

def test_five_dist_days_triggers_correction() -> None:
    """5 distribution days in 25 bars → CORRECTION, entries blocked."""
    tracker = MarketRegimeTracker()
    tracker.regime = MarketRegime.CONFIRMED_UPTREND

    # Build 20 bars: bars 3, 6, 9, 12, 15 are distribution days
    bars: list[tuple[float, float]] = [(100.0, 1e8)]
    close, vol = bars[0]
    for i in range(1, 20):
        if i in (3, 6, 9, 12, 15):
            c, v = _dist(close, vol)
        else:
            c, v = _up(close, vol)
        bars.append((c, v))
        close, vol = c, v

    _feed(tracker, bars)

    assert tracker.regime == MarketRegime.CORRECTION
    assert tracker.allows_entries is False
    assert tracker.distribution_days == 5


def test_three_dist_days_triggers_under_pressure() -> None:
    """3 distribution days → UNDER_PRESSURE, entries still allowed."""
    tracker = MarketRegimeTracker()
    tracker.regime = MarketRegime.CONFIRMED_UPTREND

    bars: list[tuple[float, float]] = [(100.0, 1e8)]
    close, vol = bars[0]
    for i in range(1, 15):
        if i in (3, 7, 11):
            c, v = _dist(close, vol)
        else:
            c, v = _up(close, vol)
        bars.append((c, v))
        close, vol = c, v

    _feed(tracker, bars)

    assert tracker.regime == MarketRegime.UNDER_PRESSURE
    assert tracker.allows_entries is True
    assert tracker.distribution_days == 3


def test_dist_days_age_out_restores_uptrend() -> None:
    """Distribution days that leave the 25-bar window no longer count."""
    tracker = MarketRegimeTracker()
    tracker.regime = MarketRegime.CONFIRMED_UPTREND

    # 3 distribution days early on, then 30 flat up bars — dist days age out
    bars: list[tuple[float, float]] = [(100.0, 1e8)]
    close, vol = bars[0]
    for i in range(1, 35):
        if i in (2, 4, 6):  # early distribution days
            c, v = _dist(close, vol)
        else:
            c, v = _up(close, vol)
        bars.append((c, v))
        close, vol = c, v

    _feed(tracker, bars)

    assert tracker.regime == MarketRegime.CONFIRMED_UPTREND
    assert tracker.distribution_days < 3


def test_dist_days_cleared_on_follow_through() -> None:
    """Follow-through day clears the distribution day list."""
    tracker = MarketRegimeTracker()
    tracker.regime = MarketRegime.CORRECTION

    # Seed 4 distribution days internally (within lookback window)
    tracker._dist_day_bars = [-10, -8, -6, -4]
    tracker._correction_low = 95.0

    # Simulate a 4-day rally + follow-through on bar 4
    bars = [
        (95.0, 1e8),             # bar 0 (starting point)
        (96.0, 0.9e8),           # rally day 1 (up but < 1.5%)
        (96.5, 0.85e8),          # rally day 2
        (97.0, 0.8e8),           # rally day 3
        (98.5, 1.2e8),           # rally day 4: +1.55% on higher volume → FTD
    ]
    _feed(tracker, bars)

    assert tracker.regime == MarketRegime.CONFIRMED_UPTREND
    assert tracker.distribution_days == 0  # cleared on FTD


# ---------------------------------------------------------------------------
# Follow-through day tests
# ---------------------------------------------------------------------------

def test_follow_through_on_day4_confirms_uptrend() -> None:
    """Follow-through day on rally day 4 transitions to CONFIRMED_UPTREND."""
    tracker = MarketRegimeTracker()
    tracker.regime = MarketRegime.CORRECTION
    tracker._correction_low = 90.0

    bars = [
        (90.0, 1e8),              # bar 0 (correction low already set)
        (91.0, 0.9e8),            # rally day 1
        (91.5, 0.85e8),           # rally day 2
        (92.0, 0.8e8),            # rally day 3
        (93.4, 1.1e8),            # rally day 4: 91.5→93.4 is >1.5%, higher vol → FTD
    ]
    _feed(tracker, bars)

    assert tracker.regime == MarketRegime.CONFIRMED_UPTREND


def test_follow_through_on_day3_does_not_confirm() -> None:
    """Follow-through day criterion requires day 4 or later — day 3 is too early."""
    tracker = MarketRegimeTracker()
    tracker.regime = MarketRegime.CORRECTION
    tracker._correction_low = 90.0

    bars = [
        (90.0, 1e8),              # bar 0
        (91.0, 0.9e8),            # rally day 1
        (92.0, 0.85e8),           # rally day 2
        (93.4, 1.1e8),            # rally day 3: +1.5% higher vol — but only day 3
    ]
    _feed(tracker, bars)

    assert tracker.regime == MarketRegime.CORRECTION


def test_rally_undercut_resets_day_count() -> None:
    """If close undercuts correction low during rally, reset day count."""
    tracker = MarketRegimeTracker()
    tracker.regime = MarketRegime.CORRECTION
    tracker._correction_low = 95.0

    bars = [
        (95.0, 1e8),    # bar 0
        (96.0, 0.9e8),  # rally day 1
        (96.5, 0.85e8), # rally day 2
        (94.5, 0.8e8),  # undercut correction low (94.5 < 95.0) → reset
        (95.5, 0.9e8),  # new rally day 1
        (96.0, 0.85e8), # new rally day 2
        (96.5, 0.8e8),  # new rally day 3 — NOT 6 total
    ]
    _feed(tracker, bars)

    # Should still be in CORRECTION: only 3 rally days since undercut reset
    assert tracker.regime == MarketRegime.CORRECTION
    assert tracker._rally_day_count == 3


def test_allows_entries_false_only_in_correction() -> None:
    """allows_entries is False only when regime is CORRECTION."""
    tracker = MarketRegimeTracker()

    tracker.regime = MarketRegime.CORRECTION
    assert tracker.allows_entries is False

    tracker.regime = MarketRegime.UNDER_PRESSURE
    assert tracker.allows_entries is True

    tracker.regime = MarketRegime.CONFIRMED_UPTREND
    assert tracker.allows_entries is True
