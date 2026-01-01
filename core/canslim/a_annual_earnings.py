"""
A - Annual Earnings Growth

Evaluates the year-over-year growth in annual earnings.
Consistent annual earnings growth (25%+) demonstrates sustainable business performance.
"""
from __future__ import annotations
from typing import Optional
import pandas as pd
from config import settings


def _safe_growth(current: float, previous: float) -> Optional[float]:
    """Calculate YoY growth as a decimal."""
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


def _score_from_growth(growth: Optional[float], target: float) -> float:
    """Convert earnings growth into a 0-1 score."""
    if growth is None:
        return 0.0

    import numpy as np
    return float(np.clip(growth / target, 0, 2) / 2)


def evaluate_a(annual_income: pd.DataFrame, a_growth_target: Optional[float] = None) -> tuple[float, Optional[float]]:
    """
    Evaluate A (Annual Earnings Growth) score.

    Args:
        annual_income: Annual income statement DataFrame
        a_growth_target: Target growth rate (defaults to settings.A_GROWTH_TARGET)

    Returns:
        tuple: (score, annual_growth) where score is 0-1 and annual_growth is decimal
    """
    a_growth_target = a_growth_target or settings.A_GROWTH_TARGET
    annual_growth = None

    if annual_income.empty:
        return 0.0, None

    try:
        # Flexible label search for Net Income
        net_income_row = annual_income.index[
            annual_income.index.str.contains('Net Income', case=False, regex=True)
        ]

        if not net_income_row.empty:
            earnings = annual_income.loc[net_income_row[0]].sort_index()
            if len(earnings) >= 2:
                annual_growth = _safe_growth(earnings.iloc[-1], earnings.iloc[-2])
    except Exception:
        pass

    score = _score_from_growth(annual_growth, a_growth_target)
    return score, annual_growth
