"""
C - Current Quarterly Earnings Growth

Evaluates the growth in current quarterly earnings compared to the same quarter last year.
Strong earnings growth (25%+ quarter-over-quarter) is a key indicator of company momentum.

Priority: EPS (Basic or Diluted) first, then fallback to Net Income.
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
        row_label = _find_earnings_row(quarterly_income)

        if row_label is not None:
            # Sort columns by date oldest→newest so iloc[-1] is most recent
            earnings = quarterly_income.loc[row_label].sort_index()
            if len(earnings) >= 2:
                current_growth = _safe_growth(earnings.iloc[-1], earnings.iloc[-2])
    except Exception:
        pass

    score = _score_from_growth(current_growth, c_growth_target)
    return score, current_growth
