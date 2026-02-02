"""
S - Supply and Demand

Per William O'Neil's CANSLIM methodology:
- Prefer stocks with a reasonable number of shares outstanding (tighter supply)
- Look for big volume increases on price advances (institutional accumulation)
- Volume should be above average on up days and below average on down days
- Heavy volume breakouts near 52-week highs are bullish
- Power Earnings Gaps (gap-ups with heavy volume) are strong signals

O'Neil says: "A stock with 5 billion shares outstanding is hard to move.
Prefer companies with reasonable float that show increasing demand."
"""
from __future__ import annotations
from typing import Optional, Dict
import numpy as np
import pandas as pd
from config import settings


def _detect_volume_surge(
    recent_volume: float,
    avg_volume: float,
    surge_threshold: float
) -> tuple[bool, float]:
    """
    Detect if recent volume is significantly above average.

    Args:
        recent_volume: Most recent day's volume
        avg_volume: Average volume baseline
        surge_threshold: Multiplier threshold (e.g., 1.5 = 50% above avg)

    Returns:
        tuple: (is_surge, volume_ratio)
    """
    if avg_volume == 0:
        return False, 0.0

    volume_ratio = recent_volume / avg_volume
    is_surge = volume_ratio >= surge_threshold

    return is_surge, volume_ratio


def _detect_breakout(
    current_price: float,
    high_52week: float,
    proximity_threshold: float
) -> tuple[bool, float]:
    """
    Detect if price is breaking out to new highs.

    Args:
        current_price: Current closing price
        high_52week: 52-week high price
        proximity_threshold: How close to 52-week high (e.g., 0.98 = within 2%)

    Returns:
        tuple: (is_breakout, proximity_ratio)
    """
    if high_52week == 0:
        return False, 0.0

    proximity = current_price / high_52week
    is_breakout = proximity >= proximity_threshold

    return is_breakout, proximity


def _detect_power_earnings_gap(
    price_history: pd.DataFrame,
    lookback_days: int = 10,
    gap_threshold: float = 0.02,
    volume_threshold: float = 1.5
) -> tuple[bool, Optional[Dict[str, float]]]:
    """
    Detect Power Earnings Gap: significant gap-up with heavy volume.

    A Power Earnings Gap occurs when:
    1. Stock gaps up significantly (typically after earnings)
    2. Volume is substantially above average
    3. Price holds above the gap

    Args:
        price_history: DataFrame with OHLCV data
        lookback_days: Days to look back for gaps
        gap_threshold: Minimum gap size as percentage (e.g., 0.02 = 2%)
        volume_threshold: Minimum volume multiplier (e.g., 1.5 = 50% above avg)

    Returns:
        tuple: (has_power_gap, gap_details)
    """
    if len(price_history) < lookback_days + 50:
        return False, None

    # Get recent data
    recent = price_history.tail(lookback_days + 1).copy()

    # Calculate average volume before lookback period
    avg_volume = price_history['Volume'].iloc[-(lookback_days + 50):-lookback_days].mean()

    if avg_volume == 0:
        return False, None

    # Look for gaps in recent data
    for i in range(1, len(recent)):
        current = recent.iloc[i]
        previous = recent.iloc[i - 1]

        # Gap up detection: today's low > yesterday's high
        gap_size = (current['Low'] - previous['High']) / previous['Close']

        if gap_size >= gap_threshold:
            volume_ratio = current['Volume'] / avg_volume

            # Check if it's a power gap (heavy volume)
            if volume_ratio >= volume_threshold:
                gap_details = {
                    'gap_size': gap_size,
                    'volume_ratio': volume_ratio,
                    'gap_price': float(current['Open']),
                    'days_ago': len(recent) - i - 1
                }
                return True, gap_details

    return False, None


def _calculate_up_down_volume_ratio(price_history: pd.DataFrame, lookback: int = 50) -> float:
    """
    Calculate the ratio of volume on up days vs down days.

    O'Neil emphasizes that healthy accumulation shows heavy volume on up days
    and lighter volume on down days. This is a key sign of institutional buying.

    Args:
        price_history: DataFrame with OHLCV data.
        lookback: Number of days to analyze.

    Returns:
        Ratio of average up-day volume to average down-day volume.
        Values > 1.0 indicate accumulation, < 1.0 indicate distribution.
    """
    if len(price_history) < lookback:
        lookback = len(price_history)

    recent = price_history.tail(lookback).copy()
    closes = recent['Close']
    volumes = recent['Volume']

    daily_changes = closes.diff()

    up_days = daily_changes > 0
    down_days = daily_changes < 0

    up_volume = volumes[up_days].mean() if up_days.any() else 0
    down_volume = volumes[down_days].mean() if down_days.any() else 1

    if down_volume == 0:
        return 2.0  # Cap at 2.0 if no down volume

    return min(up_volume / down_volume, 3.0)  # Cap at 3.0


def _score_float_supply(shares_outstanding: Optional[float]) -> float:
    """
    Score based on shares outstanding / float size.

    O'Neil prefers companies with a manageable number of shares outstanding.
    Very large floats (billions of shares) make it harder for the stock to move.
    Very small floats can be too volatile. A "sweet spot" exists.

    Args:
        shares_outstanding: Total shares outstanding.

    Returns:
        Score 0-1 based on float attractiveness.
    """
    if shares_outstanding is None or shares_outstanding <= 0:
        return 0.5  # Neutral if unknown

    # Convert to millions for readability
    shares_millions = shares_outstanding / 1e6

    # O'Neil's preference: moderate float
    # < 50M shares: excellent (tight supply)
    # 50-200M: good
    # 200-500M: acceptable
    # 500M-1B: below average
    # > 1B: low score (too much supply)
    if shares_millions < 50:
        return 1.0
    elif shares_millions < 200:
        return 0.85
    elif shares_millions < 500:
        return 0.65
    elif shares_millions < 1000:
        return 0.4
    else:
        return 0.2


def evaluate_s(
    price_history: pd.DataFrame,
    avg_volume_50: float,
    current_price: float,
    high_52week: float,
    shares_outstanding: Optional[float] = None,
    s_volume_surge_threshold: Optional[float] = None,
    s_breakout_proximity: Optional[float] = None,
    s_power_gap_lookback: Optional[int] = None
) -> tuple[float, Dict[str, object]]:
    """
    Evaluate S (Supply and Demand) score.

    Per O'Neil's methodology:
    1. Shares outstanding / float size (tighter supply = better)
    2. Volume on up days vs down days (accumulation vs distribution)
    3. Volume surges on breakouts
    4. Power Earnings Gaps

    Scoring breakdown:
    - 25% weight: Float / shares outstanding (supply tightness)
    - 25% weight: Up/down volume ratio (institutional accumulation)
    - 30% weight: Volume surge + breakout detection
    - 20% weight: Power Earnings Gap bonus

    Args:
        price_history: DataFrame with OHLCV data
        avg_volume_50: Average daily volume over 50 days
        current_price: Current closing price
        high_52week: 52-week high price
        shares_outstanding: Total shares outstanding (for float analysis)
        s_volume_surge_threshold: Volume surge multiplier (default from settings)
        s_breakout_proximity: Proximity to 52-week high (default from settings)
        s_power_gap_lookback: Days to look back for power gaps (default from settings)

    Returns:
        tuple: (score, metrics_dict)
    """
    # Load defaults from settings
    s_volume_surge_threshold = s_volume_surge_threshold or settings.S_VOLUME_SURGE_THRESHOLD
    s_breakout_proximity = s_breakout_proximity or settings.S_BREAKOUT_PROXIMITY
    s_power_gap_lookback = s_power_gap_lookback or settings.S_POWER_GAP_LOOKBACK

    # Get most recent volume
    recent_volume = float(price_history['Volume'].iloc[-1]) if len(price_history) > 0 else 0.0

    # --- Component 1: Float / Shares Outstanding ---
    float_score = _score_float_supply(shares_outstanding)

    # --- Component 2: Up/Down Volume Ratio ---
    up_down_ratio = _calculate_up_down_volume_ratio(price_history)
    # Ratio > 1.25 is good (more volume on up days), normalize to 0-1
    if up_down_ratio >= 1.5:
        ud_score = 1.0
    elif up_down_ratio >= 1.0:
        ud_score = (up_down_ratio - 1.0) / 0.5  # Linear 1.0-1.5 → 0-1
    else:
        ud_score = max(up_down_ratio - 0.5, 0.0) / 0.5 * 0.3  # Below 1.0 gets minimal credit

    # --- Component 3: Volume Surge + Breakout ---
    has_volume_surge, volume_ratio = _detect_volume_surge(
        recent_volume, avg_volume_50, s_volume_surge_threshold
    )
    is_breakout, proximity = _detect_breakout(
        current_price, high_52week, s_breakout_proximity
    )

    # Volume + breakout combined score
    volume_score = min(volume_ratio / s_volume_surge_threshold, 1.0) if avg_volume_50 > 0 else 0.0
    breakout_score = 1.0 if is_breakout else max((proximity - 0.85) / (s_breakout_proximity - 0.85), 0)
    surge_breakout_score = 0.5 * volume_score + 0.5 * breakout_score

    # --- Component 4: Power Earnings Gap ---
    has_power_gap, gap_details = _detect_power_earnings_gap(
        price_history, lookback_days=s_power_gap_lookback
    )
    power_gap_score = 1.0 if has_power_gap else 0.0

    # --- Weighted combination ---
    score = (
        settings.S_FLOAT_WEIGHT * float_score
        + settings.S_UP_DOWN_VOL_WEIGHT * ud_score
        + settings.S_SURGE_BREAKOUT_WEIGHT * surge_breakout_score
        + settings.S_POWER_GAP_WEIGHT * power_gap_score
    )
    score = float(np.clip(score, 0, 1))

    # Compile metrics for reporting
    metrics = {
        'recent_volume': recent_volume,
        'avg_volume_50': avg_volume_50,
        'volume_ratio': volume_ratio,
        'has_volume_surge': has_volume_surge,
        'proximity_to_high': proximity,
        'is_breakout': is_breakout,
        'has_power_gap': has_power_gap,
        'power_gap_details': gap_details,
        'up_down_volume_ratio': up_down_ratio,
        'shares_outstanding': shares_outstanding,
        'float_score': float_score,
    }

    return score, metrics
