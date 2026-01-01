"""
CANSLIM Stock Evaluation Module

This module provides a comprehensive implementation of William O'Neil's CANSLIM
investment strategy, breaking down each component into separate, maintainable modules.

Each CANSLIM letter represents a key criterion:
- C: Current quarterly earnings growth
- A: Annual earnings growth
- N: New products, new management, new highs
- S: Supply and demand (shares outstanding, volume)
- L: Leader or laggard (relative strength)
- I: Institutional sponsorship
- M: Market direction

Usage:
    from core.canslim import evaluate_canslim, evaluate_market_direction

    # Evaluate market first
    market_trend = evaluate_market_direction()

    # Evaluate individual stock
    result = evaluate_canslim(symbol, rs_scores_df, market_trend)
"""
from .core import evaluate_canslim
from .m_market_direction import evaluate_m as evaluate_market_direction, MarketTrend

# Individual component evaluators (for advanced usage)
from .c_current_earnings import evaluate_c
from .a_annual_earnings import evaluate_a
from .n_new_products import evaluate_n
from .s_supply_demand import evaluate_s
from .l_leader_laggard import evaluate_l
from .i_institutional import evaluate_i

__all__ = [
    # Main functions
    "evaluate_canslim",
    "evaluate_market_direction",
    "MarketTrend",
    # Individual components
    "evaluate_c",
    "evaluate_a",
    "evaluate_n",
    "evaluate_s",
    "evaluate_l",
    "evaluate_i",
]
