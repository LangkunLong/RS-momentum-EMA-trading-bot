"""
Core CANSLIM evaluation that combines all seven components.

This module orchestrates the evaluation of all CANSLIM criteria:
C - Current quarterly earnings growth
A - Annual earnings growth
N - New products/price leadership
S - Supply and demand dynamics
L - Leader or laggard (relative strength)
I - Institutional sponsorship
M - Market direction
"""
from __future__ import annotations
from typing import Dict, Optional
import pandas as pd
import yfinance as yf

from config import settings
from core.yahoo_finance_helper import (
    coerce_scalar,
    extract_float_series,
    normalize_price_dataframe,
)

from .c_current_earnings import evaluate_c
from .a_annual_earnings import evaluate_a
from .n_new_products import evaluate_n
from .s_supply_demand import evaluate_s
from .l_leader_laggard import evaluate_l
from .i_institutional import evaluate_i
from .m_market_direction import evaluate_m, MarketTrend


def evaluate_canslim(
    symbol: str,
    rs_scores_df: pd.DataFrame,
    market_trend: Optional[MarketTrend] = None,
    period: Optional[str] = None,
    c_growth_target: Optional[float] = None,
    a_growth_target: Optional[float] = None,
    n_revenue_weight: Optional[float] = None,
    n_proximity_weight: Optional[float] = None,
    s_turnover_cap: Optional[float] = None,
    i_institutional_cap: Optional[float] = None
) -> Optional[Dict[str, object]]:
    """
    Evaluate all CANSLIM components for a given stock.

    Args:
        symbol: Stock ticker symbol
        rs_scores_df: DataFrame containing pre-calculated RS scores
        market_trend: Pre-calculated market trend (or will evaluate SPY)
        period: Historical data period to analyze
        c_growth_target: Target for current quarterly earnings growth
        a_growth_target: Target for annual earnings growth
        n_revenue_weight: Weight for revenue growth in N score
        n_proximity_weight: Weight for price proximity in N score
        s_turnover_cap: Maximum turnover ratio for S score
        i_institutional_cap: Maximum institutional holding for I score

    Returns:
        Dict containing CANSLIM scores and metrics, or None if evaluation fails
    """
    # Load defaults from configuration
    period = period or settings.CANSLIM_DATA_PERIOD
    c_growth_target = c_growth_target or settings.C_GROWTH_TARGET
    a_growth_target = a_growth_target or settings.A_GROWTH_TARGET
    n_revenue_weight = n_revenue_weight or settings.N_REVENUE_GROWTH_WEIGHT
    n_proximity_weight = n_proximity_weight or settings.N_PROXIMITY_TO_HIGH_WEIGHT
    s_turnover_cap = s_turnover_cap or settings.S_TURNOVER_CAP
    i_institutional_cap = i_institutional_cap or settings.I_INSTITUTIONAL_CAP

    ticker = yf.Ticker(symbol)

    # 1. Fetch Data with Error Handling
    try:
        # Use fast_info for market cap/shares if available
        info = ticker.fast_info
        # Attempt to get financials, handle empty frames
        try:
            quarterly_income = ticker.quarterly_income_stmt
            annual_income = ticker.income_stmt
        except Exception:
            quarterly_income = pd.DataFrame()
            annual_income = pd.DataFrame()
    except Exception as e:
        print(f"Data fetch error for {symbol}: {e}")
        return None

    # 2. Market Trend & Price History
    market_trend = market_trend or evaluate_m()
    try:
        price_history = ticker.history(period=period, interval="1d", auto_adjust=True)
    except Exception:
        return None

    if price_history.empty or len(price_history) < 30:
        return None

    # 3. Extract price and volume metrics
    price_history = normalize_price_dataframe(price_history)
    closes = extract_float_series(price_history, "Close")
    latest_close = coerce_scalar(closes.iloc[-1])
    high_52 = coerce_scalar(closes.max())
    proximity_to_high = latest_close / high_52 if high_52 else 0.0

    # Volume & Turnover
    volume_series = extract_float_series(price_history, "Volume")
    avg_volume_50 = float(volume_series.tail(50).mean()) if not volume_series.empty else 0.0

    # Robust Shares Outstanding Fetch
    shares_outstanding = None
    if hasattr(info, 'shares_outstanding'):
        shares_outstanding = info.shares_outstanding
    elif hasattr(ticker, 'info') and 'sharesOutstanding' in ticker.info:
        shares_outstanding = ticker.info['sharesOutstanding']

    # 4. Evaluate Each CANSLIM Component

    # C - Current Quarterly Earnings
    score_c, current_growth = evaluate_c(quarterly_income, c_growth_target)

    # A - Annual Earnings
    score_a, annual_growth = evaluate_a(annual_income, a_growth_target)

    # N - New Products/Price Leadership
    score_n, revenue_growth = evaluate_n(
        quarterly_income,
        proximity_to_high,
        n_revenue_weight,
        n_proximity_weight
    )

    # S - Supply and Demand
    score_s, s_metrics = evaluate_s(
        price_history,
        avg_volume_50,
        latest_close,
        high_52
    )

    # L - Leader or Laggard
    score_l, rs_score = evaluate_l(symbol, rs_scores_df)

    # I - Institutional Sponsorship
    held_percent_institutions = None
    if hasattr(info, 'held_percent_institutions'):
        held_percent_institutions = info.held_percent_institutions
    score_i = evaluate_i(held_percent_institutions, None, i_institutional_cap)

    # M - Market Direction
    score_m = market_trend.score

    # 5. Compile scores
    scores = {
        "C": score_c,
        "A": score_a,
        "N": score_n,
        "S": score_s,
        "L": score_l,
        "I": score_i,
        "M": score_m
    }

    # 6. DYNAMIC SCORING: Calculate weighted average based on available data
    # We always have L, M, and usually S and N (price based).
    # If C and A are 0.0 due to missing data (not bad data), we re-weight.

    has_fundamentals = (current_growth is not None or annual_growth is not None)

    if has_fundamentals:
        # Full CANSLIM score with all 7 components
        total_score = float(sum(scores.values()) / 7 * 100)
    else:
        # Speculative/Technical Score: Normalize based on 5 components (exclude C and A)
        partial_sum = scores["L"] + scores["M"] + scores["N"] + scores["S"] + scores["I"]
        total_score = float(partial_sum / 5 * 100)

    # 7. Compile metrics for reporting
    metrics = {
        "current_growth": current_growth,
        "annual_growth": annual_growth,
        "revenue_growth": revenue_growth,
        "s_metrics": s_metrics,  # New: volume surge, breakout, power gap details
        "proximity_to_high": proximity_to_high,
        "avg_volume_50": avg_volume_50,
        "has_fundamentals": has_fundamentals
    }

    return {
        "symbol": symbol,
        "scores": scores,
        "metrics": metrics,
        "total_score": total_score,
        "rs_score": rs_score,
        "market_trend": market_trend,
    }
