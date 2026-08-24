"""M - Market Direction.

Per William O'Neil's CANSLIM methodology:
- Only buy stocks when the general market is in a confirmed uptrend
- Count distribution days: 5+ in 25 trading days signals a market top
- Look for follow-through days: a strong rally (1.5%+) on day 4+ of an attempted
  rally with volume above the previous day confirms a new uptrend
- Use EMA alignment as supporting evidence of trend strength

O'Neil says: "Three out of four stocks follow the market's overall direction,
so you need to know the market's direction."
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Dict, Optional

import numpy as np
import pandas as pd

from config import settings
from core.data_client import (
    coerce_scalar,
    extract_float_series,
    fetch_ohlcv,
    normalize_price_dataframe,
)
from core.trading_sessions import latest_us_equity_session


class MarketRegime(Enum):
    """Discrete O'Neil market regime states."""

    CONFIRMED_UPTREND = "confirmed_uptrend"
    UNDER_PRESSURE = "under_pressure"
    CORRECTION = "correction"


class MarketRegimeTracker:
    """Stateful bar-by-bar O'Neil market regime tracker.

    Call bootstrap() once before the simulation loop, then update() once per bar.
    Consult allows_entries before evaluating new signals.
    """

    def __init__(self) -> None:
        """Initialise in CORRECTION — bootstrap() sets the real starting regime."""
        self.regime: MarketRegime = MarketRegime.CORRECTION
        self._dist_day_bars: list[int] = []  # bar indices of distribution days
        self._bar_count: int = 0             # total bars processed since bootstrap
        self._rally_day_count: int = 0
        self._rally_active: bool = False
        self._correction_low: float = float("inf")

    @property
    def allows_entries(self) -> bool:
        """True when regime permits new position entries."""
        return self.regime != MarketRegime.CORRECTION

    @property
    def distribution_days(self) -> int:
        """Number of distribution days currently in the rolling window."""
        return len(self._dist_day_bars)

    def bootstrap(self, spy_df: pd.DataFrame, start_date: pd.Timestamp) -> None:
        """Set initial regime from pre-simulation SPY data.

        Checks SPY's position relative to 200-day EMA and replays the last
        25 bars to populate the distribution day window.

        Args:
            spy_df: Full SPY OHLCV DataFrame (must include data before start_date).
            start_date: First date of the simulation window.
        """
        hist = spy_df.loc[:start_date]
        if len(hist) < 2:
            return

        closes = hist["Close"].astype(float)
        volumes = hist["Volume"].astype(float)

        # Initial regime from 200-day EMA position
        ema_200 = closes.ewm(span=200, adjust=False).mean()
        if float(closes.iloc[-1]) > float(ema_200.iloc[-1]):
            self.regime = MarketRegime.CONFIRMED_UPTREND
        else:
            self.regime = MarketRegime.CORRECTION

        # Replay last M_DISTRIBUTION_LOOKBACK bars to seed distribution day list.
        # Assign negative bar indices so they age out naturally once the live
        # simulation runs M_DISTRIBUTION_LOOKBACK more bars.
        lookback = min(settings.M_DISTRIBUTION_LOOKBACK + 1, len(hist))
        recent_closes = closes.iloc[-lookback:]
        recent_volumes = volumes.iloc[-lookback:]

        for i in range(1, len(recent_closes)):
            bar_idx = i - lookback + 1  # ranges from -(lookback-1) to 0
            close = float(recent_closes.iloc[i])
            prev_close = float(recent_closes.iloc[i - 1])
            vol = float(recent_volumes.iloc[i])
            prev_vol = float(recent_volumes.iloc[i - 1])
            if prev_close > 0:
                pct = (close - prev_close) / prev_close
                if pct <= -settings.M_DISTRIBUTION_MIN_DECLINE and vol > prev_vol:
                    self._dist_day_bars.append(bar_idx)

        # Apply distribution-day thresholds to the bootstrapped regime
        dist_count = len(self._dist_day_bars)
        if self.regime != MarketRegime.CORRECTION:
            if dist_count >= settings.M_MAX_DISTRIBUTION_DAYS:
                self.regime = MarketRegime.CORRECTION
            elif dist_count >= settings.M_REGIME_PRESSURE_DIST_DAYS:
                self.regime = MarketRegime.UNDER_PRESSURE

    def update(
        self,
        date: pd.Timestamp,
        close: float,
        prev_close: float,
        volume: float,
        prev_volume: float,
    ) -> MarketRegime:
        """Update regime for one trading bar. Call once per bar in simulation loop.

        Args:
            date: Current bar date (unused internally but accepted for signature clarity).
            close: Current bar closing price.
            prev_close: Previous bar closing price.
            volume: Current bar volume.
            prev_volume: Previous bar volume.

        Returns:
            Updated MarketRegime.
        """
        self._bar_count += 1

        # 1. Age out distribution days outside the rolling window
        cutoff = self._bar_count - settings.M_DISTRIBUTION_LOOKBACK
        self._dist_day_bars = [b for b in self._dist_day_bars if b > cutoff]

        # 2. Check for a new distribution day
        if prev_close > 0:
            pct = (close - prev_close) / prev_close
            if pct <= -settings.M_DISTRIBUTION_MIN_DECLINE and volume > prev_volume:
                self._dist_day_bars.append(self._bar_count)

        dist_count = len(self._dist_day_bars)

        # 3. Hard correction gate (5+ dist days flips to CORRECTION immediately)
        if dist_count >= settings.M_MAX_DISTRIBUTION_DAYS:
            self.regime = MarketRegime.CORRECTION

        # 4. Uptrend zone adjustments (only when NOT already in correction)
        elif self.regime != MarketRegime.CORRECTION:
            if dist_count >= settings.M_REGIME_PRESSURE_DIST_DAYS:
                self.regime = MarketRegime.UNDER_PRESSURE
            else:
                self.regime = MarketRegime.CONFIRMED_UPTREND

        # 5. Rally attempt logic (only active while in CORRECTION)
        if self.regime == MarketRegime.CORRECTION:
            if not self._rally_active:
                # Track correction low before rally begins
                self._correction_low = min(self._correction_low, close)
                if prev_close > 0 and close > prev_close:
                    self._rally_active = True
                    self._rally_day_count = 1
            else:
                # During rally: check for undercut on any bar
                if close < self._correction_low:
                    self._rally_active = False
                    self._rally_day_count = 0
                    self._correction_low = float("inf")
                elif prev_close > 0 and close > prev_close:
                    self._rally_day_count += 1

                # Follow-through day check
                if self._rally_active and self._rally_day_count >= settings.M_FOLLOW_THROUGH_MIN_DAY:
                    if prev_close > 0:
                        gain = (close - prev_close) / prev_close
                        if gain >= settings.M_FOLLOW_THROUGH_MIN_PCT and volume > prev_volume:
                            self.regime = MarketRegime.CONFIRMED_UPTREND
                            self._dist_day_bars.clear()
                            self._rally_active = False
                            self._rally_day_count = 0
                            self._correction_low = float("inf")

        return self.regime


@dataclass
class MarketTrend:
    """Lightweight representation of the general market trend."""

    symbol: str
    score: float
    is_bullish: bool
    latest_close: Optional[float]
    indicators: Dict[str, float]
    distribution_days: int = 0
    follow_through: bool = False
    as_of_session: date | None = None


def _count_distribution_days(
    closes: pd.Series, volumes: pd.Series, lookback: int = 25, min_decline: float = 0.002
) -> int:
    """Count distribution days in the recent lookback period.

    A distribution day per O'Neil is:
    1. Index closes down ≥ 0.2% from the previous day
    2. Volume is higher than the previous day's volume
    This indicates institutional selling (heavy volume on down days).

    5 or more distribution days within 25 trading days signals a market top.

    Args:
        closes: Close price series.
        volumes: Volume series.
        lookback: Number of trading days to look back.
        min_decline: Minimum percentage decline to count (0.002 = 0.2%).

    Returns:
        Number of distribution days found.

    """
    if len(closes) < lookback + 1:
        lookback = len(closes) - 1

    recent_closes = closes.iloc[-(lookback + 1) :]
    recent_volumes = volumes.iloc[-(lookback + 1) :]

    dist_count = 0
    for i in range(1, len(recent_closes)):
        prev = recent_closes.iloc[i - 1]
        if prev <= 0 or pd.isna(prev):
            continue
        price_change = (recent_closes.iloc[i] - prev) / prev
        vol_today = recent_volumes.iloc[i]
        vol_yesterday = recent_volumes.iloc[i - 1]

        # Distribution day: decline ≥ 0.2% on higher volume
        if price_change <= -min_decline and vol_today > vol_yesterday:
            dist_count += 1

    return dist_count


def _detect_follow_through_day(
    closes: pd.Series,
    volumes: pd.Series,
    min_rally_pct: float = 0.015,
    min_rally_day: int = 4,
    lookback: int = 30,
) -> bool:
    """Detect if a follow-through day has occurred recently.

    A follow-through day per O'Neil:
    1. Market must first have a significant decline
    2. An attempted rally begins (first day the index closes higher)
    3. On day 4 or later of the attempted rally, the index closes up ≥ 1.5%
       with volume higher than the previous day
    This confirms the rally is real and not just a bounce.

    Args:
        closes: Close price series.
        volumes: Volume series.
        min_rally_pct: Minimum percentage gain for follow-through (0.015 = 1.5%).
        min_rally_day: Earliest day of rally for follow-through (O'Neil says day 4+).
        lookback: Days to search for a follow-through.

    Returns:
        True if a recent follow-through day was detected.

    """
    if len(closes) < 5:
        return False

    recent_closes = closes.tail(lookback)
    recent_volumes = volumes.tail(lookback)

    # Find attempted rallies: sequences of up days after a decline
    rally_day_count = 0
    in_rally = False

    for i in range(1, len(recent_closes)):
        prev_close = recent_closes.iloc[i - 1]
        if prev_close <= 0 or pd.isna(prev_close):
            continue
        daily_change = (recent_closes.iloc[i] - prev_close) / prev_close

        if daily_change > 0:
            if not in_rally:
                in_rally = True
                rally_day_count = 1
            else:
                rally_day_count += 1

            # Check for follow-through: day 4+ with 1.5%+ gain on higher volume
            if (
                rally_day_count >= min_rally_day
                and daily_change >= min_rally_pct
                and recent_volumes.iloc[i] > recent_volumes.iloc[i - 1]
            ):
                return True
        else:
            # Down day resets rally count (though O'Neil allows some down days
            # within a rally attempt — here we're conservative)
            if daily_change < -0.01:  # Only reset on meaningful decline
                in_rally = False
                rally_day_count = 0

    return False


def evaluate_m(
    benchmark_symbol: str = "SPY",
    period: Optional[str] = None,
    price_above_200_weight: Optional[float] = None,
    ema_alignment_weight: Optional[float] = None,
    rising_50ema_weight: Optional[float] = None,
    price_above_21_weight: Optional[float] = None,
    bullish_threshold: Optional[float] = None,
    rising_lookback: Optional[int] = None,
    price_data: Optional[pd.DataFrame] = None,
) -> MarketTrend:
    """Evaluate M (Market Direction) using the benchmark index.

    Per O'Neil's methodology, combines:
    1. Distribution day counting (30% weight) — 5+ in 25 days = bearish
    2. Follow-through day detection (15% weight) — confirms new uptrends
    3. Price above 200-EMA (25% weight) — long-term trend
    4. EMA alignment 21>50>200 (15% weight) — trend confirmation
    5. 50-EMA rising (10% weight) — intermediate trend
    6. Price above 21-EMA (5% weight) — short-term trend

    Args:
        benchmark_symbol: Ticker symbol for market benchmark (default: SPY)
        period: Historical period to analyze
        price_above_200_weight: Weight if price > 200-EMA
        ema_alignment_weight: Weight if EMAs are properly aligned
        rising_50ema_weight: Weight if 50-EMA is rising
        price_above_21_weight: Weight if price > 21-EMA
        bullish_threshold: Minimum score to consider market bullish
        rising_lookback: Days to check if 50-EMA is rising

    Returns:
        MarketTrend: Object containing market direction score and details

    """
    # Load defaults from configuration
    if period is None:
        period = settings.MARKET_TREND_PERIOD
    if price_above_200_weight is None:
        price_above_200_weight = settings.M_PRICE_ABOVE_200EMA_WEIGHT
    if ema_alignment_weight is None:
        ema_alignment_weight = settings.M_EMA_ALIGNMENT_WEIGHT
    if rising_50ema_weight is None:
        rising_50ema_weight = settings.M_50EMA_RISING_WEIGHT
    if price_above_21_weight is None:
        price_above_21_weight = settings.M_PRICE_ABOVE_21EMA_WEIGHT
    if bullish_threshold is None:
        bullish_threshold = settings.M_BULLISH_THRESHOLD
    if rising_lookback is None:
        rising_lookback = settings.M_50EMA_RISING_LOOKBACK

    if price_data is not None:
        data = price_data
    else:
        try:
            data = fetch_ohlcv(benchmark_symbol, period=period)
        except Exception:
            data = pd.DataFrame()

    if data.empty or len(data) < 50:
        return MarketTrend(
            symbol=benchmark_symbol,
            score=0.4,
            is_bullish=False,
            latest_close=None,
            indicators={},
            as_of_session=latest_us_equity_session(data),
        )

    data = normalize_price_dataframe(data)
    closes = extract_float_series(data, "Close")
    volumes = extract_float_series(data, "Volume")

    ema_21 = closes.ewm(span=21).mean()
    ema_50 = closes.ewm(span=50).mean()
    ema_200 = closes.ewm(span=200).mean()

    latest_close = coerce_scalar(closes.iloc[-1])
    latest_ema_21 = coerce_scalar(ema_21.iloc[-1])
    latest_ema_50 = coerce_scalar(ema_50.iloc[-1])
    latest_ema_200 = coerce_scalar(ema_200.iloc[-1])

    # --- O'Neil's Distribution Day Count ---
    dist_days = _count_distribution_days(
        closes,
        volumes,
        lookback=settings.M_DISTRIBUTION_LOOKBACK,
        min_decline=settings.M_DISTRIBUTION_MIN_DECLINE,
    )
    # 0 dist days = full score, 5+ = 0 score
    max_dist = settings.M_MAX_DISTRIBUTION_DAYS
    dist_score = max(1.0 - dist_days / max_dist, 0.0)

    # --- O'Neil's Follow-Through Day Detection ---
    has_follow_through = _detect_follow_through_day(
        closes,
        volumes,
        min_rally_pct=settings.M_FOLLOW_THROUGH_MIN_PCT,
        min_rally_day=settings.M_FOLLOW_THROUGH_MIN_DAY,
    )
    ftd_score = 1.0 if has_follow_through else 0.0

    # --- EMA-based trend analysis (supporting evidence) ---
    trend_score = 0.0

    if latest_close > latest_ema_200:
        trend_score += price_above_200_weight

    if latest_ema_21 > latest_ema_50 > latest_ema_200:
        trend_score += ema_alignment_weight

    if len(ema_50) > rising_lookback:
        ema_50_lookback = coerce_scalar(ema_50.iloc[-rising_lookback])
        if latest_ema_50 > ema_50_lookback:
            trend_score += rising_50ema_weight

    if latest_close > latest_ema_21:
        trend_score += price_above_21_weight

    # --- Combine all components ---
    # Distribution days and follow-through are O'Neil's primary market timing tools
    combined_score = (
        settings.M_DISTRIBUTION_WEIGHT * dist_score
        + settings.M_FOLLOW_THROUGH_WEIGHT * ftd_score
        + (1.0 - settings.M_DISTRIBUTION_WEIGHT - settings.M_FOLLOW_THROUGH_WEIGHT) * trend_score
    )

    combined_score = float(np.clip(combined_score, 0, 1))

    return MarketTrend(
        symbol=benchmark_symbol,
        score=combined_score,
        is_bullish=combined_score >= bullish_threshold,
        latest_close=latest_close,
        indicators={
            "ema_21": latest_ema_21,
            "ema_50": latest_ema_50,
            "ema_200": latest_ema_200,
        },
        distribution_days=dist_days,
        follow_through=has_follow_through,
        as_of_session=latest_us_equity_session(data),
    )
