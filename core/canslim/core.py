"""Core CANSLIM evaluation that combines all seven components.

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

from config import settings
from core.data_client import (
    coerce_scalar,
    extract_float_series,
    fetch_annual_income_statement,
    fetch_balance_sheet,
    fetch_company_info,
    fetch_ohlcv,
    fetch_quarterly_income_statement,
    normalize_price_dataframe,
)

from .a_annual_earnings import evaluate_a
from .c_current_earnings import evaluate_c
from .i_institutional import evaluate_i
from .l_leader_laggard import evaluate_l
from .m_market_direction import MarketTrend, evaluate_m
from .n_new_products import evaluate_n
from .s_supply_demand import evaluate_s


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
    i_institutional_cap: Optional[float] = None,
) -> Optional[Dict[str, object]]:
    """Evaluate all CANSLIM components for a given stock.

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

    # 1. Fetch Fundamental Data with Error Handling
    try:
        company_info = fetch_company_info(symbol)

        try:
            quarterly_income = fetch_quarterly_income_statement(symbol)
            annual_income = fetch_annual_income_statement(symbol)
        except Exception as e:
            print(f"[WARN] {symbol}: Failed to fetch income statements: {e}")
            quarterly_income = pd.DataFrame()
            annual_income = pd.DataFrame()

        try:
            balance_sheet = fetch_balance_sheet(symbol)
        except Exception as e:
            print(f"[WARN] {symbol}: Failed to fetch balance sheet: {e}")
            balance_sheet = pd.DataFrame()
    except Exception as e:
        print(f"Data fetch error for {symbol}: {e}")
        return None

    if quarterly_income.empty and annual_income.empty:
        print(f"[WARN] {symbol}: No fundamental data available — C and A scores will be 0")

    # 2. Market Trend & Price History
    market_trend = market_trend or evaluate_m()
    try:
        price_history = fetch_ohlcv(symbol, period=period)
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

    # Shares Outstanding from FMP
    shares_outstanding = company_info.get("shares_outstanding")

    # 4. Evaluate Each CANSLIM Component

    # C - Current Quarterly Earnings (YoY with acceleration)
    score_c, current_growth = evaluate_c(quarterly_income, c_growth_target)

    # A - Annual Earnings (multi-year consistency + ROE)
    score_a, annual_growth, roe = evaluate_a(annual_income, a_growth_target, balance_sheet)

    # N - New Products/Price Leadership (emphasis on new highs)
    score_n, revenue_growth = evaluate_n(quarterly_income, proximity_to_high, n_revenue_weight, n_proximity_weight)

    # S - Supply and Demand (float, up/down volume, breakout, power gap)
    score_s, s_metrics = evaluate_s(price_history, avg_volume_50, latest_close, high_52, shares_outstanding)

    # L - Leader or Laggard
    score_l, rs_score = evaluate_l(symbol, rs_scores_df)

    # I - Institutional Sponsorship (sweet-spot + trend)
    held_percent_institutions = company_info.get("held_percent_institutions")
    num_institutional_holders = company_info.get("institution_count")

    score_i = evaluate_i(held_percent_institutions, num_institutional_holders=num_institutional_holders)

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
        "M": score_m,
    }

    # 6. WEIGHTED SCORING per O'Neil's methodology
    # Dynamically re-normalize when one or both fundamentals are missing.
    institutional_data_available = held_percent_institutions is not None or num_institutional_holders is not None
    has_fundamentals = current_growth is not None or annual_growth is not None
    data_availability = {
        "C": current_growth is not None,
        "A": annual_growth is not None,
        "N_revenue": revenue_growth is not None,
        "N_price": proximity_to_high is not None and proximity_to_high > 0,
        "I_level": held_percent_institutions is not None,
        "I_trend": num_institutional_holders is not None,
        "M": market_trend is not None,
    }
    weights = {
        "C": settings.CANSLIM_WEIGHT_C if current_growth is not None else 0.0,
        "A": settings.CANSLIM_WEIGHT_A if annual_growth is not None else 0.0,
        "N": settings.CANSLIM_WEIGHT_N,
        "S": settings.CANSLIM_WEIGHT_S,
        "L": settings.CANSLIM_WEIGHT_L,
        "I": settings.CANSLIM_WEIGHT_I if institutional_data_available else 0.0,
        "M": settings.CANSLIM_WEIGHT_M,
    }
    total_active_weight = sum(weights.values())

    if total_active_weight > 0:
        active_weights = {
            key: (weight / total_active_weight if weight > 0 else 0.0)
            for key, weight in weights.items()
        }
        total_score = (
            active_weights["C"] * score_c
            + active_weights["A"] * score_a
            + active_weights["N"] * score_n
            + active_weights["S"] * score_s
            + active_weights["L"] * score_l
            + active_weights["I"] * score_i
            + active_weights["M"] * score_m
        ) * 100
    else:
        active_weights = {key: 0.0 for key in weights}
        total_score = 0.0

    total_score = float(total_score)
    weighted_contributions = {
        key: active_weights[key] * scores[key] * 100 for key in scores
    }

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
        "base_weights": weights,
        "active_weights": active_weights,
        "weighted_contributions": weighted_contributions,
        "data_availability": data_availability,
        "metrics": metrics,
        "total_score": total_score,
        "rs_score": rs_score,
        "market_trend": market_trend,
        "is_breakout": s_metrics.get("is_breakout", False),
        "has_volume_surge": s_metrics.get("has_volume_surge", False),
    }
