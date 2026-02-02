"""
N - New Products, New Management, New Highs

Per William O'Neil's CANSLIM methodology:
- Look for companies with a new product, new management, or new industry conditions
- The stock should be emerging from a proper chart base pattern
- Stock should be making or near NEW 52-week highs (O'Neil heavily emphasizes this)
- Revenue growth validates the "new" catalyst

O'Neil says: "It takes something new to produce a startling advance in the price of a stock."
The 52-week high proximity is the primary signal — stocks making new highs tend to go higher.
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


def evaluate_n(
    quarterly_income: pd.DataFrame,
    proximity_to_high: float,
    n_revenue_weight: Optional[float] = None,
    n_proximity_weight: Optional[float] = None
) -> tuple[float, Optional[float]]:
    """
    Evaluate N (New Products/Price Leadership) score.

    Per O'Neil's methodology:
    - Stocks making new 52-week highs are the primary signal (most stocks that
      went on to make huge gains were already at new highs when they started)
    - Revenue growth validates the catalyst driving the stock

    Scoring:
    - Proximity to 52-week high: 50% weight (O'Neil's emphasis on new highs)
    - Revenue growth (YoY quarterly): 50% weight

    Args:
        quarterly_income: Quarterly income statement DataFrame
        proximity_to_high: Current price / 52-week high (0-1+)
        n_revenue_weight: Weight for revenue growth component
        n_proximity_weight: Weight for proximity to high component

    Returns:
        tuple: (score, revenue_growth) where score is 0-1 and revenue_growth is decimal
    """
    import numpy as np

    n_revenue_weight = n_revenue_weight or settings.N_REVENUE_GROWTH_WEIGHT
    n_proximity_weight = n_proximity_weight or settings.N_PROXIMITY_TO_HIGH_WEIGHT
    revenue_growth = None

    # Calculate revenue growth (YoY quarterly)
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

    # Revenue score: 25%+ quarterly revenue growth = full score
    revenue_score = _score_from_growth(revenue_growth, settings.N_REVENUE_GROWTH_TARGET)

    # Proximity score: O'Neil wants stocks at or near new 52-week highs
    # Within 2% of high = full score, drops off steeply below 85%
    if proximity_to_high is not None and proximity_to_high > 0:
        if proximity_to_high >= 0.98:
            # At or near new highs — full score
            proximity_score = 1.0
        elif proximity_to_high >= 0.90:
            # Within 10% of high — partial credit, linear scale
            proximity_score = (proximity_to_high - 0.90) / (0.98 - 0.90)
        elif proximity_to_high >= 0.75:
            # 10-25% off high — minimal credit
            proximity_score = (proximity_to_high - 0.75) / (0.90 - 0.75) * 0.3
        else:
            # More than 25% off high — O'Neil would not be interested
            proximity_score = 0.0
    else:
        proximity_score = 0.0

    # Weighted combination
    score = float(n_revenue_weight * revenue_score + n_proximity_weight * proximity_score)
    score = float(np.clip(score, 0, 1))

    return score, revenue_growth
