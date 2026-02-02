"""
A - Annual Earnings Growth

Evaluates the year-over-year growth in annual earnings.
Consistent annual earnings growth (25%+) demonstrates sustainable business performance.

Priority: EPS (Basic or Diluted) first, then fallback to Net Income.
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
        row_label = _find_earnings_row(annual_income)

        if row_label is not None:
            # Sort columns by date oldest→newest so iloc[-1] is most recent
            earnings = annual_income.loc[row_label].sort_index()
            if len(earnings) >= 2:
                annual_growth = _safe_growth(earnings.iloc[-1], earnings.iloc[-2])
    except Exception:
        pass

    score = _score_from_growth(annual_growth, a_growth_target)
    return score, annual_growth
