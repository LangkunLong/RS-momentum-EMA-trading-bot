"""
I - Institutional Sponsorship

Evaluates institutional ownership as a proxy for professional investor confidence.
O'Neil's rule: Look for stocks with increasing institutional sponsorship, but
avoid stocks that are *over-owned* (>85-90%) as this limits upside potential
and creates selling pressure when institutions exit.

Ideal range: 20-80% institutional ownership.
"""
from __future__ import annotations
from typing import Optional
import numpy as np
from config import settings

# Ownership thresholds for O'Neil-style bell-curve scoring
_IDEAL_LOW = 0.20    # Below this, insufficient sponsorship
_IDEAL_HIGH = 0.80   # Above this, over-owned (diminishing returns)
_OVER_OWNED = 0.95   # Severely over-owned


def evaluate_i(
    held_percent_institutions: Optional[float],
    turnover_ratio: Optional[float] = None,
    i_institutional_cap: Optional[float] = None
) -> float:
    """
    Evaluate I (Institutional Sponsorship) score.

    O'Neil scoring: institutional ownership in the 20-80% sweet spot earns full
    credit. Below 20% scales linearly. Above 80% is penalized — excessive
    institutional ownership limits upside and creates crowded-trade risk.

    Args:
        held_percent_institutions: Percentage of shares held by institutions (0-1)
        turnover_ratio: Fallback proxy using turnover if institutional data unavailable
        i_institutional_cap: Maximum institutional holding for scoring (unused, kept for API compat)

    Returns:
        float: Score 0-1
    """
    pct = held_percent_institutions

    # Fallback to turnover ratio as a rough proxy if no institutional data
    if pct is None and turnover_ratio is not None:
        pct = float(np.clip(turnover_ratio, 0.0, 1.0))

    if pct is None:
        return 0.0

    pct = float(pct)

    if pct <= 0.0:
        return 0.0

    # Below ideal range: linear ramp from 0 → 1
    if pct < _IDEAL_LOW:
        return float(pct / _IDEAL_LOW)

    # Sweet spot: full credit
    if pct <= _IDEAL_HIGH:
        return 1.0

    # Over-owned: penalize linearly from 1.0 down to 0.3
    if pct <= _OVER_OWNED:
        penalty = (pct - _IDEAL_HIGH) / (_OVER_OWNED - _IDEAL_HIGH)
        return float(1.0 - 0.7 * penalty)

    # Severely over-owned
    return 0.3
