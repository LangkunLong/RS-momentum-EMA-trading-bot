"""
I - Institutional Sponsorship

Per William O'Neil's CANSLIM methodology:
- Want stocks owned by at least a few quality institutional investors
- Look for INCREASING institutional ownership (quarter over quarter)
- Too little ownership means no professional validation
- Too much ownership (>90%) means overcrowded — no one left to buy
- The sweet spot is moderate ownership with an increasing trend

O'Neil says: "Buy stocks with at least a few institutional sponsors with
better-than-average recent performance records... and with increasing
total institutional ownership in recent quarters."
"""
from __future__ import annotations
from typing import Optional
import numpy as np
from config import settings


def _score_ownership_level(held_percent: float) -> float:
    """Score institutional ownership level using O'Neil's sweet-spot model.

    O'Neil's logic:
    - < 10%: Too few institutions interested — risky
    - 10-30%: Growing institutional interest — good
    - 30-60%: Strong institutional backing — ideal
    - 60-80%: Well-owned but still room for accumulation
    - 80-90%: Heavily owned, limited upside from new buyers
    - > 90%: Overcrowded — risk of mass selling

    Args:
        held_percent: Institutional ownership as decimal (0.0-1.0).

    Returns:
        Score 0-1 based on ownership level attractiveness.
    """
    if held_percent < 0.10:
        # Under 10%: minimal institutional interest
        return held_percent / 0.10 * 0.3
    elif held_percent < 0.30:
        # 10-30%: building interest
        return 0.3 + (held_percent - 0.10) / 0.20 * 0.4
    elif held_percent < 0.60:
        # 30-60%: sweet spot per O'Neil
        return 0.7 + (held_percent - 0.30) / 0.30 * 0.3
    elif held_percent < 0.80:
        # 60-80%: still good but leveling off
        return 1.0 - (held_percent - 0.60) / 0.20 * 0.15
    elif held_percent < 0.90:
        # 80-90%: getting overcrowded
        return 0.85 - (held_percent - 0.80) / 0.10 * 0.25
    else:
        # > 90%: overcrowded — O'Neil warns against this
        return max(0.6 - (held_percent - 0.90) / 0.10 * 0.3, 0.3)


def _score_ownership_trend(
    current_holders: Optional[int],
    previous_holders: Optional[int]
) -> float:
    """Score the trend in number of institutional holders.

    O'Neil emphasizes INCREASING institutional sponsorship — more institutions
    adding positions quarter over quarter is bullish. Decreasing is bearish.

    Args:
        current_holders: Current number of institutional holders.
        previous_holders: Previous quarter's number of institutional holders.

    Returns:
        Score 0-1 based on ownership trend.
    """
    if current_holders is None or previous_holders is None:
        return 0.5  # Neutral if data unavailable

    if previous_holders <= 0:
        return 0.5

    change_pct = (current_holders - previous_holders) / previous_holders

    if change_pct >= 0.10:
        # 10%+ increase in holders — strong accumulation
        return 1.0
    elif change_pct >= 0.03:
        # 3-10% increase — moderate accumulation
        return 0.7 + (change_pct - 0.03) / 0.07 * 0.3
    elif change_pct >= 0:
        # 0-3% increase — slight accumulation
        return 0.5 + change_pct / 0.03 * 0.2
    elif change_pct >= -0.05:
        # 0-5% decrease — slight distribution
        return 0.5 + change_pct / 0.05 * 0.2
    else:
        # > 5% decrease — significant distribution
        return max(0.3 + (change_pct + 0.05) / 0.15 * 0.0, 0.1)


def evaluate_i(
    held_percent_institutions: Optional[float],
    num_institutional_holders: Optional[int] = None,
    prev_num_institutional_holders: Optional[int] = None,
    i_institutional_cap: Optional[float] = None
) -> float:
    """
    Evaluate I (Institutional Sponsorship) score.

    Per O'Neil's methodology:
    1. Moderate institutional ownership is ideal (30-60% sweet spot)
    2. INCREASING number of institutional holders is bullish
    3. Too high (>90%) is overcrowded and risky
    4. Too low (<10%) means no professional validation

    Scoring breakdown:
    - 60% weight: Ownership level (sweet-spot curve, not linear)
    - 40% weight: Ownership trend (increasing vs decreasing holders)

    Args:
        held_percent_institutions: Percentage of shares held by institutions (0-1)
        num_institutional_holders: Current number of institutional holders
        prev_num_institutional_holders: Previous quarter's holder count
        i_institutional_cap: Unused (kept for backward compatibility)

    Returns:
        float: Score 0-1
    """
    # If no institutional data at all, return low score
    if held_percent_institutions is None:
        return 0.1

    # Component 1 (60%): Ownership level (sweet-spot curve)
    level_score = _score_ownership_level(held_percent_institutions)

    # Component 2 (40%): Ownership trend
    trend_score = _score_ownership_trend(
        num_institutional_holders, prev_num_institutional_holders
    )

    # Weighted combination
    score = (
        settings.I_LEVEL_WEIGHT * level_score
        + settings.I_TREND_WEIGHT * trend_score
    )

    return float(np.clip(score, 0, 1))
