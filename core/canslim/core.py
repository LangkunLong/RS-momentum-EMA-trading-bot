"""
Core CANSLIM evaluation that combines all seven components.

This module orchestrates the evaluation of all CANSLIM criteria:
C - Current quarterly earnings growth (YoY, with acceleration)
A - Annual earnings growth (multi-year consistency, ROE)
N - New products/price leadership (new highs emphasis)
S - Supply and demand dynamics (float, up/down volume, breakouts)
L - Leader or laggard (relative strength)
I - Institutional sponsorship (sweet-spot ownership, trend)
M - Market direction (distribution days, follow-through, EMA trend)

Composite scoring uses O'Neil-weighted averages, not equal weights.
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

    Uses O'Neil's weighted scoring:
    - C (20%), A (15%), L (20%) are weighted highest (earnings + leadership)
    - M (15%) is also critical (3/4 stocks follow the market)
    - N (10%), S (10%), I (10%) provide supporting evidence

    Args:
        symbol: Stock ticker symbol
        rs_scores_df: DataFrame containing pre-calculated RS scores
        market_trend: Pre-calculated market trend (or will evaluate SPY)
        period: Historical data period to analyze
        c_growth_target: Target for current quarterly earnings growth
        a_growth_target: Target for annual earnings growth
        n_revenue_weight: Weight for revenue growth in N score
        n_proximity_weight: Weight for price proximity in N score
        s_turnover_cap: Legacy parameter (unused)
        i_institutional_cap: Legacy parameter (unused)

    Returns:
        Dict containing CANSLIM scores and metrics, or None if evaluation fails
    """
    # Load defaults from configuration
    period = period or settings.CANSLIM_DATA_PERIOD
    c_growth_target = c_growth_target or settings.C_GROWTH_TARGET
    a_growth_target = a_growth_target or settings.A_GROWTH_TARGET
    n_revenue_weight = n_revenue_weight or settings.N_REVENUE_GROWTH_WEIGHT
    n_proximity_weight = n_proximity_weight or settings.N_PROXIMITY_TO_HIGH_WEIGHT

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

        # Fetch balance sheet for ROE calculation (A criterion)
        try:
            balance_sheet = ticker.balance_sheet
        except Exception:
            balance_sheet = pd.DataFrame()
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

    # Volume
    volume_series = extract_float_series(price_history, "Volume")
    avg_volume_50 = float(volume_series.tail(50).mean()) if not volume_series.empty else 0.0

    # Robust Shares Outstanding Fetch
    shares_outstanding = None
    if hasattr(info, 'shares_outstanding'):
        shares_outstanding = info.shares_outstanding
    elif hasattr(ticker, 'info') and 'sharesOutstanding' in ticker.info:
        shares_outstanding = ticker.info['sharesOutstanding']

    # 4. Evaluate Each CANSLIM Component

    # C - Current Quarterly Earnings (YoY with acceleration)
    score_c, current_growth = evaluate_c(quarterly_income, c_growth_target)

    # A - Annual Earnings (multi-year consistency + ROE)
    score_a, annual_growth, roe = evaluate_a(annual_income, a_growth_target, balance_sheet)

    # N - New Products/Price Leadership (emphasis on new highs)
    score_n, revenue_growth = evaluate_n(
        quarterly_income,
        proximity_to_high,
        n_revenue_weight,
        n_proximity_weight
    )

    # S - Supply and Demand (float, up/down volume, breakout, power gap)
    score_s, s_metrics = evaluate_s(
        price_history,
        avg_volume_50,
        latest_close,
        high_52,
        shares_outstanding
    )

    # L - Leader or Laggard
    score_l, rs_score = evaluate_l(symbol, rs_scores_df)

    # I - Institutional Sponsorship (sweet-spot + trend)
    held_percent_institutions = None
    num_institutional_holders = None
    if hasattr(info, 'held_percent_institutions'):
        held_percent_institutions = info.held_percent_institutions
    # Try to get number of institutional holders for trend analysis
    try:
        full_info = ticker.info
        if 'heldPercentInstitutions' in full_info and held_percent_institutions is None:
            held_percent_institutions = full_info['heldPercentInstitutions']
        if 'institutionCount' in full_info:
            num_institutional_holders = full_info['institutionCount']
    except Exception:
        pass

    score_i = evaluate_i(
        held_percent_institutions,
        num_institutional_holders=num_institutional_holders
    )

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

    # 6. WEIGHTED SCORING per O'Neil's methodology
    # C, A, and L are the most critical factors
    has_fundamentals = (current_growth is not None or annual_growth is not None)

    if has_fundamentals:
        # Full CANSLIM score with O'Neil-weighted components
        total_score = (
            settings.CANSLIM_WEIGHT_C * score_c
            + settings.CANSLIM_WEIGHT_A * score_a
            + settings.CANSLIM_WEIGHT_N * score_n
            + settings.CANSLIM_WEIGHT_S * score_s
            + settings.CANSLIM_WEIGHT_L * score_l
            + settings.CANSLIM_WEIGHT_I * score_i
            + settings.CANSLIM_WEIGHT_M * score_m
        ) * 100
    else:
        # Technical-only score: redistribute C and A weights to L and M
        # When fundamentals are missing, leadership and market direction matter more
        tech_weight_sum = (
            settings.CANSLIM_WEIGHT_N
            + settings.CANSLIM_WEIGHT_S
            + settings.CANSLIM_WEIGHT_L
            + settings.CANSLIM_WEIGHT_I
            + settings.CANSLIM_WEIGHT_M
        )
        total_score = (
            (settings.CANSLIM_WEIGHT_N / tech_weight_sum) * score_n
            + (settings.CANSLIM_WEIGHT_S / tech_weight_sum) * score_s
            + (settings.CANSLIM_WEIGHT_L / tech_weight_sum) * score_l
            + (settings.CANSLIM_WEIGHT_I / tech_weight_sum) * score_i
            + (settings.CANSLIM_WEIGHT_M / tech_weight_sum) * score_m
        ) * 100

    total_score = float(total_score)

    # 7. Compile metrics for reporting
    metrics = {
        "current_growth": current_growth,
        "annual_growth": annual_growth,
        "revenue_growth": revenue_growth,
        "roe": roe,
        "s_metrics": s_metrics,
        "proximity_to_high": proximity_to_high,
        "avg_volume_50": avg_volume_50,
        "has_fundamentals": has_fundamentals,
        "shares_outstanding": shares_outstanding,
    }

    return {
        "symbol": symbol,
        "scores": scores,
        "metrics": metrics,
        "total_score": total_score,
        "rs_score": rs_score,
        "market_trend": market_trend,
    }
