"""
S - Supply and Demand

Evaluates the balance of supply and demand through trading volume and turnover ratio.
High turnover indicates strong demand, but excessive turnover may signal overtrading.
"""
from __future__ import annotations
from typing import Optional
import numpy as np
from config import settings


def _score_from_ratio(value: Optional[float], cap: float) -> float:
    """Normalize ratio between 0 and provided max-limit cap."""
    if value is None:
        return 0.0

    return float(np.clip(value / cap, 0, 1))


def evaluate_s(
    avg_volume_50: float,
    shares_outstanding: Optional[float],
    s_turnover_cap: Optional[float] = None
) -> tuple[float, Optional[float]]:
    """
    Evaluate S (Supply and Demand) score.

    Args:
        avg_volume_50: Average daily volume over 50 days
        shares_outstanding: Total shares outstanding
        s_turnover_cap: Maximum turnover ratio for scoring

    Returns:
        tuple: (score, turnover_ratio) where score is 0-1 and turnover_ratio is annualized
    """
    s_turnover_cap = s_turnover_cap or settings.S_TURNOVER_CAP
    turnover_ratio = None

    # Calculate annualized turnover ratio
    if shares_outstanding and avg_volume_50:
        turnover_ratio = (avg_volume_50 * 252) / shares_outstanding

    score = _score_from_ratio(turnover_ratio, s_turnover_cap)
    return score, turnover_ratio
