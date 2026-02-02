"""
C - Current Quarterly Earnings Growth

Evaluates the growth in current quarterly earnings per share.
Per William O'Neil's CANSLIM methodology:
- Current quarter EPS should be up at least 25% vs. the SAME quarter last year (YoY)
- Look for accelerating quarterly earnings growth (each quarter better than the last)
- The more quarters of strong growth, the better

Priority: EPS (Basic or Diluted) first, then fallback to Net Income.
"""
from __future__ import annotations
from typing import Optional, List
import pandas as pd
from config import settings


def _safe_growth(current: float, previous: float) -> Optional[float]:
    """Calculate growth as a decimal, handling edge cases."""
    if current is None or previous in (None, 0):
        return None

    try:
        current = float(current)
        previous = float(previous)
    except (TypeError, ValueError):
        return None

    import numpy as np
    if np.isclose(previous, 0.0):
        return None

    try:
        return (current - previous) / abs(previous)
    except ZeroDivisionError:
        return None


def _find_earnings_row(df: pd.DataFrame) -> Optional[str]:
    """Find the best earnings row label using fuzzy matching.

    Priority:
        1. Basic EPS or Diluted EPS
        2. Net Income (fallback)

    Args:
        df: Income statement DataFrame with row labels as index.

    Returns:
        The matching index label, or None if nothing found.
    """
    # Priority 1: EPS rows
    eps_mask = df.index.str.contains(r'Basic EPS|Diluted EPS', case=False, regex=True)
    if eps_mask.any():
        return df.index[eps_mask][0]

    # Priority 2: Net Income
    ni_mask = df.index.str.contains(r'Net Income', case=False, regex=True)
    if ni_mask.any():
        return df.index[ni_mask][0]

    return None


def _get_quarterly_yoy_growths(earnings: pd.Series) -> List[Optional[float]]:
    """Calculate year-over-year growth for each quarter.

    Compares each quarter to the same quarter one year prior (4 quarters back).

    Args:
        earnings: Time-sorted earnings series (oldest to newest).

    Returns:
        List of YoY growth rates for available quarters (most recent first).
    """
    growths = []
    n = len(earnings)
    # Start from most recent, go backwards
    for i in range(n - 1, 3, -1):  # Need at least 4 quarters back
        growth = _safe_growth(earnings.iloc[i], earnings.iloc[i - 4])
        growths.append(growth)
    return growths


def _check_acceleration(growths: List[Optional[float]]) -> float:
    """Check if earnings growth is accelerating (each quarter better than prior).

    O'Neil emphasizes accelerating earnings — each quarter's YoY growth rate
    should be higher than the previous quarter's YoY growth rate.

    Args:
        growths: List of YoY growth rates, most recent first.

    Returns:
        Acceleration score 0-1. 1.0 means consistent acceleration.
    """
    valid = [g for g in growths if g is not None]
    if len(valid) < 2:
        return 0.5  # Neutral if insufficient data

    accelerating_pairs = 0
    total_pairs = 0
    # growths[0] = most recent, growths[1] = previous quarter
    # Acceleration means growths[0] > growths[1] > growths[2]...
    for i in range(len(valid) - 1):
        total_pairs += 1
        if valid[i] > valid[i + 1]:
            accelerating_pairs += 1

    return accelerating_pairs / total_pairs if total_pairs > 0 else 0.5


def evaluate_c(
    quarterly_income: pd.DataFrame,
    c_growth_target: Optional[float] = None
) -> tuple[float, Optional[float]]:
    """
    Evaluate C (Current Quarterly Earnings Growth) score.

    Per O'Neil's methodology:
    1. Current quarter EPS must be up 25%+ vs same quarter last year (YoY)
    2. Prefer accelerating growth across recent quarters
    3. Multiple quarters of 25%+ growth is ideal

    Scoring breakdown:
    - 60% weight: Most recent quarter YoY growth vs target
    - 20% weight: Consistency (how many of last 3 quarters show 25%+ growth)
    - 20% weight: Acceleration (are growth rates increasing?)

    Args:
        quarterly_income: Quarterly income statement DataFrame
        c_growth_target: Target growth rate (defaults to settings.C_GROWTH_TARGET)

    Returns:
        tuple: (score, current_growth) where score is 0-1 and current_growth is decimal
    """
    import numpy as np

    c_growth_target = c_growth_target or settings.C_GROWTH_TARGET
    current_growth = None

    if quarterly_income.empty:
        return 0.0, None

    try:
        row_label = _find_earnings_row(quarterly_income)
        if row_label is None:
            return 0.0, None

        # Sort columns by date oldest→newest so iloc[-1] is most recent
        earnings = quarterly_income.loc[row_label].sort_index()

        if len(earnings) < 5:
            # Need at least 5 quarters for YoY (current + 4 back)
            # Fallback: if we have at least 2, use what we have
            if len(earnings) >= 2:
                current_growth = _safe_growth(earnings.iloc[-1], earnings.iloc[-2])
                if current_growth is None:
                    return 0.0, None
                growth_score = float(np.clip(current_growth / c_growth_target, 0, 2) / 2)
                return growth_score, current_growth
            return 0.0, None

        # --- O'Neil's methodology: Year-over-Year comparison ---
        # Compare most recent quarter to same quarter last year (4 quarters back)
        yoy_growths = _get_quarterly_yoy_growths(earnings)

        if not yoy_growths or yoy_growths[0] is None:
            return 0.0, None

        current_growth = yoy_growths[0]  # Most recent quarter YoY

        # Component 1 (60%): Current quarter growth vs target
        # 25%+ growth = full score, scales linearly
        growth_score = float(np.clip(current_growth / c_growth_target, 0, 2) / 2)

        # Component 2 (20%): Consistency — how many recent quarters show 25%+ growth
        valid_growths = [g for g in yoy_growths[:3] if g is not None]
        if valid_growths:
            quarters_above_target = sum(1 for g in valid_growths if g >= c_growth_target)
            consistency_score = quarters_above_target / len(valid_growths)
        else:
            consistency_score = 0.0

        # Component 3 (20%): Acceleration — are growth rates increasing?
        acceleration_score = _check_acceleration(yoy_growths[:4])

        # Weighted combination
        score = (
            settings.C_GROWTH_WEIGHT * growth_score
            + settings.C_CONSISTENCY_WEIGHT * consistency_score
            + settings.C_ACCELERATION_WEIGHT * acceleration_score
        )
        score = float(np.clip(score, 0, 1))

        return score, current_growth

    except Exception:
        return 0.0, current_growth
