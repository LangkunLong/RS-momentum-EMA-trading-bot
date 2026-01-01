"""
C - Current Quarterly Earnings Growth

Evaluates the growth in current quarterly earnings compared to the same quarter last year.
Strong earnings growth (25%+ quarter-over-quarter) is a key indicator of company momentum.
"""
from __future__ import annotations
from typing import Optional
import pandas as pd
from config import settings


def _safe_growth(current: float, previous: float) -> Optional[float]:
    """Calculate YoY revenue growth as a decimal."""
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


def evaluate_c(quarterly_income: pd.DataFrame, c_growth_target: Optional[float] = None) -> tuple[float, Optional[float]]:
    """
    Evaluate C (Current Quarterly Earnings Growth) score.

    Args:
        quarterly_income: Quarterly income statement DataFrame
        c_growth_target: Target growth rate (defaults to settings.C_GROWTH_TARGET)

    Returns:
        tuple: (score, current_growth) where score is 0-1 and current_growth is decimal
    """
    c_growth_target = c_growth_target or settings.C_GROWTH_TARGET
    current_growth = None

    if quarterly_income.empty:
        return 0.0, None

    try:
        # Flexible label search for Net Income
        net_income_row = quarterly_income.index[
            quarterly_income.index.str.contains('Net Income', case=False, regex=True)
        ]

        if not net_income_row.empty:
            earnings = quarterly_income.loc[net_income_row[0]].sort_index()
            if len(earnings) >= 2:
                current_growth = _safe_growth(earnings.iloc[-1], earnings.iloc[-2])
    except Exception:
        pass

    score = _score_from_growth(current_growth, c_growth_target)
    return score, current_growth
