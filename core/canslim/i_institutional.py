"""
I - Institutional Sponsorship

Evaluates institutional ownership as a proxy for professional investor confidence.
High institutional ownership indicates quality, but too high may limit upside potential.
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


def evaluate_i(
    held_percent_institutions: Optional[float],
    turnover_ratio: Optional[float] = None,
    i_institutional_cap: Optional[float] = None
) -> float:
    """
    Evaluate I (Institutional Sponsorship) score.

    Args:
        held_percent_institutions: Percentage of shares held by institutions (0-1)
        turnover_ratio: Fallback proxy using turnover if institutional data unavailable
        i_institutional_cap: Maximum institutional holding for scoring

    Returns:
        float: Score 0-1
    """
    i_institutional_cap = i_institutional_cap or settings.I_INSTITUTIONAL_CAP

    # Prefer institutional holding data if available
    if held_percent_institutions is not None:
        return _score_from_ratio(held_percent_institutions, i_institutional_cap)

    # Fallback to turnover ratio as proxy
    if turnover_ratio is not None:
        return _score_from_ratio(turnover_ratio, settings.S_TURNOVER_CAP)

    # Default to neutral if no data available
    return 0.0
