"""
L - Leader or Laggard

Evaluates whether a stock is a market leader or laggard using Relative Strength (RS).
RS compares a stock's performance against the market benchmark over multiple quarters.

O'Neil's rule: only buy stocks with RS >= 80 (top 20% of the market).
Stocks below RS 70 are laggards and should receive minimal credit.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from core.momentum_analysis import calculate_rs_momentum


def evaluate_l(symbol: str, rs_scores_df: pd.DataFrame) -> tuple[float, float]:
    """
    Evaluate L (Leader or Laggard) score based on Relative Strength.

    Scoring uses a power curve that rewards leaders and penalizes laggards:
    - RS >= 80: score 0.80 – 1.00 (leaders, full credit zone)
    - RS 60-79: score 0.20 – 0.79 (moderate, scaled down)
    - RS < 60:  score near 0       (laggards, minimal credit)

    Args:
        symbol: Stock ticker symbol
        rs_scores_df: DataFrame containing pre-calculated RS scores for all symbols

    Returns:
        tuple: (score, rs_score) where score is 0-1 and rs_score is 1-99
    """
    rs_score = calculate_rs_momentum(symbol, rs_scores_df)

    # Power curve: (rs/100)^2 penalizes laggards, rewards leaders
    # RS=80 → 0.64, RS=90 → 0.81, RS=50 → 0.25, RS=30 → 0.09
    normalized = rs_score / 100.0
    score = float(np.clip(normalized ** 2, 0.0, 1.0))

    return score, rs_score
