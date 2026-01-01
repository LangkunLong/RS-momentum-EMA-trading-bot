"""
L - Leader or Laggard

Evaluates whether a stock is a market leader or laggard using Relative Strength (RS).
RS compares a stock's performance against the market benchmark over multiple quarters.
"""
from __future__ import annotations
import pandas as pd
from core.momentum_analysis import calculate_rs_momentum


def evaluate_l(symbol: str, rs_scores_df: pd.DataFrame) -> tuple[float, float]:
    """
    Evaluate L (Leader or Laggard) score based on Relative Strength.

    Args:
        symbol: Stock ticker symbol
        rs_scores_df: DataFrame containing pre-calculated RS scores for all symbols

    Returns:
        tuple: (score, rs_score) where score is 0-1 and rs_score is 1-99
    """
    rs_score = calculate_rs_momentum(symbol, rs_scores_df)
    score = rs_score / 100.0  # Normalize to 0-1 range

    return score, rs_score
