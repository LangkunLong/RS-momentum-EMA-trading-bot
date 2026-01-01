"""
N - New Products, New Management, New Highs

Evaluates innovative products/services through revenue growth and price leadership
(proximity to 52-week highs). Companies making new highs often have new catalysts.
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
    """Convert revenue growth into a 0-1 score."""
    if growth is None:
        return 0.0

    import numpy as np
    return float(np.clip(growth / target, 0, 2) / 2)


def _score_from_ratio(value: Optional[float], cap: float) -> float:
    """Normalize ratio between 0 and provided max-limit cap."""
    if value is None:
        return 0.0

    import numpy as np
    return float(np.clip(value / cap, 0, 1))


def evaluate_n(
    quarterly_income: pd.DataFrame,
    proximity_to_high: float,
    n_revenue_weight: Optional[float] = None,
    n_proximity_weight: Optional[float] = None
) -> tuple[float, Optional[float]]:
    """
    Evaluate N (New Products/Price Leadership) score.

    Args:
        quarterly_income: Quarterly income statement DataFrame
        proximity_to_high: Current price / 52-week high (0-1+)
        n_revenue_weight: Weight for revenue growth component
        n_proximity_weight: Weight for proximity to high component

    Returns:
        tuple: (score, revenue_growth) where score is 0-1 and revenue_growth is decimal
    """
    n_revenue_weight = n_revenue_weight or settings.N_REVENUE_GROWTH_WEIGHT
    n_proximity_weight = n_proximity_weight or settings.N_PROXIMITY_TO_HIGH_WEIGHT
    revenue_growth = None

    # Calculate revenue growth
    if not quarterly_income.empty:
        try:
            revenue_row = quarterly_income.index[
                quarterly_income.index.str.contains('Revenue|Total Revenue', case=False, regex=True)
            ]

            if not revenue_row.empty:
                revs = quarterly_income.loc[revenue_row[0]].sort_index()
                if len(revs) >= 4:  # YoY Quarterly
                    revenue_growth = _safe_growth(revs.iloc[-1], revs.iloc[-4])
        except Exception:
            pass

    # Combine revenue growth and price proximity
    revenue_score = _score_from_growth(revenue_growth, 0.2)  # 20% revenue growth target
    proximity_score = _score_from_ratio(proximity_to_high, 1.05)  # Within 5% of high gets full score

    score = float(n_revenue_weight * revenue_score + n_proximity_weight * proximity_score)
    return score, revenue_growth
