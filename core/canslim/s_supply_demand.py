"""
S - Supply and Demand

Evaluates the balance of supply and demand through volume analysis:
- Volume surges (especially on up days)
- Heavy volume on breakouts
- Power Earnings Gaps (gap-ups with heavy volume)

Based on William O'Neil's CANSLIM methodology focusing on institutional accumulation.
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


def evaluate_s(
    price_history: pd.DataFrame,
    avg_volume_50: float,
    current_price: float,
    high_52week: float,
    s_volume_surge_threshold: Optional[float] = None,
    s_breakout_proximity: Optional[float] = None,
    s_power_gap_lookback: Optional[int] = None
) -> tuple[float, Dict[str, object]]:
    """
    Evaluate S (Supply and Demand) score based on volume analysis.

    Scoring components:
    1. Volume surge detection (40%)
    2. Breakout with heavy volume (40%)
    3. Power Earnings Gap (20% bonus)

    Args:
        price_history: DataFrame with OHLCV data
        avg_volume_50: Average daily volume over 50 days
        current_price: Current closing price
        high_52week: 52-week high price
        s_volume_surge_threshold: Volume surge multiplier (default from settings)
        s_breakout_proximity: Proximity to 52-week high (default from settings)
        s_power_gap_lookback: Days to look back for power gaps (default from settings)

    Returns:
        tuple: (score, metrics_dict)
            score: 0-1 based on volume and breakout characteristics
            metrics_dict: Dictionary containing detection results and ratios
    """
    # Load defaults from settings
    s_volume_surge_threshold = s_volume_surge_threshold or settings.S_VOLUME_SURGE_THRESHOLD
    s_breakout_proximity = s_breakout_proximity or settings.S_BREAKOUT_PROXIMITY
    s_power_gap_lookback = s_power_gap_lookback or settings.S_POWER_GAP_LOOKBACK

    # Get most recent volume
    recent_volume = float(price_history['Volume'].iloc[-1]) if len(price_history) > 0 else 0.0

    # Detect volume surge
    has_volume_surge, volume_ratio = _detect_volume_surge(
        recent_volume,
        avg_volume_50,
        s_volume_surge_threshold
    )

    # Detect breakout
    is_breakout, proximity = _detect_breakout(
        current_price,
        high_52week,
        s_breakout_proximity
    )

    # Detect Power Earnings Gap
    has_power_gap, gap_details = _detect_power_earnings_gap(
        price_history,
        lookback_days=s_power_gap_lookback
    )

    # Calculate composite score
    # Base score from volume surge and breakout
    volume_score = 0.4 if has_volume_surge else min(volume_ratio / s_volume_surge_threshold * 0.4, 0.4)
    breakout_score = 0.4 if is_breakout else max((proximity - 0.85) / (s_breakout_proximity - 0.85) * 0.4, 0)

    # Bonus for Power Earnings Gap (can push score above 1.0)
    power_gap_bonus = 0.2 if has_power_gap else 0.0

    # Combine scores (cap at 1.0)
    score = min(volume_score + breakout_score + power_gap_bonus, 1.0)

    # Compile metrics for reporting
    metrics = {
        'recent_volume': recent_volume,
        'avg_volume_50': avg_volume_50,
        'volume_ratio': volume_ratio,
        'has_volume_surge': has_volume_surge,
        'proximity_to_high': proximity,
        'is_breakout': is_breakout,
        'has_power_gap': has_power_gap,
        'power_gap_details': gap_details
    }

    return score, metrics
