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
    fmp_request_was_deferred,
    normalize_price_dataframe,
    reset_fmp_request_context,
)

from .a_annual_earnings import evaluate_a
from .c_current_earnings import evaluate_c
from .entry_contract import build_entry_facts, evaluate_entry_contract
from .i_institutional import evaluate_i
from .l_leader_laggard import evaluate_l
from .m_market_direction import MarketTrend, evaluate_m
from .n_new_products import evaluate_n
from .s_supply_demand import evaluate_s
from core.trading_sessions import (
    history_through_exact_session,
    latest_us_equity_session,
    normalize_us_equity_session,
)


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
    as_of_session: object = None,
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
    if period is None:
        period = settings.CANSLIM_DATA_PERIOD
    if c_growth_target is None:
        c_growth_target = settings.C_GROWTH_TARGET
    if a_growth_target is None:
        a_growth_target = settings.A_GROWTH_TARGET
    if n_revenue_weight is None:
        n_revenue_weight = settings.N_REVENUE_GROWTH_WEIGHT
    if n_proximity_weight is None:
        n_proximity_weight = settings.N_PROXIMITY_TO_HIGH_WEIGHT

    # 1. Bind all live inputs to the same completed market session.  Legacy
    # callers that provide an advisory MarketTrend without session provenance
    # retain their prior behavior.
    market_trend = market_trend or evaluate_m()
    market_session = getattr(market_trend, "as_of_session", None)
    if as_of_session is not None and market_session is not None:
        if normalize_us_equity_session(as_of_session) != normalize_us_equity_session(market_session):
            return None
    expected_session = as_of_session if as_of_session is not None else market_session

    try:
        price_history = fetch_ohlcv(symbol, period=period)
    except Exception:
        return None
    if price_history.empty:
        return None
    price_history = normalize_price_dataframe(price_history)
    if expected_session is not None:
        if latest_us_equity_session(price_history) != normalize_us_equity_session(expected_session).date():
            return None
        exact_history = history_through_exact_session(price_history, expected_session)
        if exact_history is None:
            return None
        price_history = exact_history
    if len(price_history) < 30:
        return None

    # 2. Fetch Fundamental Data only after price-session freshness is proven.
    reset_fmp_request_context()
    income_statement_error = None
    balance_sheet_error = None
    try:
        company_info = fetch_company_info(symbol)

        try:
            quarterly_income = fetch_quarterly_income_statement(symbol)
            annual_income = fetch_annual_income_statement(symbol)
        except Exception as e:
            print(f"[WARN] {symbol}: Failed to fetch income statements: {e}")
            income_statement_error = str(e)
            quarterly_income = pd.DataFrame()
            annual_income = pd.DataFrame()

        try:
            balance_sheet = fetch_balance_sheet(symbol)
        except Exception as e:
            print(f"[WARN] {symbol}: Failed to fetch balance sheet: {e}")
            balance_sheet_error = str(e)
            balance_sheet = pd.DataFrame()
    except Exception as e:
        print(f"Data fetch error for {symbol}: {e}")
        return None

    if quarterly_income.empty and annual_income.empty:
        print(f"[WARN] {symbol}: No fundamental data available — C and A scores will be 0")

    # 3. Extract price and volume metrics
    closes = extract_float_series(price_history, "Close")
    latest_close = coerce_scalar(closes.iloc[-1])
    lookback_252 = min(252, len(closes))
    high_52 = coerce_scalar(closes.iloc[-lookback_252:].max())
    proximity_to_high = latest_close / high_52 if high_52 else 0.0

    # Volume
    volume_series = extract_float_series(price_history, "Volume")
    entry_facts = build_entry_facts(closes, volume_series)
    avg_volume_50 = entry_facts.prior_average_volume_50 or 0.0

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
    prev_num_institutional_holders = company_info.get("prev_institution_count")

    score_i = evaluate_i(
        held_percent_institutions,
        num_institutional_holders=num_institutional_holders,
        prev_num_institutional_holders=prev_num_institutional_holders,
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
        "M": score_m,
    }

    # 6. WEIGHTED SCORING per O'Neil's methodology
    # C and A remain part of the composite even when unavailable so that missing
    # fundamentals behave as missing evidence, not as an implicit free pass.
    # The I component is optional on the free tier, so only that weight is
    # redistributed when institutional data is unavailable.
    institutional_trend_available = (
        num_institutional_holders is not None
        and prev_num_institutional_holders is not None
    )
    institutional_data_available = (
        held_percent_institutions is not None or institutional_trend_available
    )
    has_fundamentals = current_growth is not None or annual_growth is not None
    fmp_quota_deferred = fmp_request_was_deferred() and (
        quarterly_income.empty or annual_income.empty
    )
    data_availability = {
        "C": current_growth is not None,
        "A": annual_growth is not None,
        "N_revenue": revenue_growth is not None,
        "N_price": proximity_to_high is not None and proximity_to_high > 0,
        "I_level": held_percent_institutions is not None,
        "I_trend": institutional_trend_available,
        "M": market_trend is not None,
    }
    base_weights = {
        "C": settings.CANSLIM_WEIGHT_C,
        "A": settings.CANSLIM_WEIGHT_A,
        "N": settings.CANSLIM_WEIGHT_N,
        "S": settings.CANSLIM_WEIGHT_S,
        "L": settings.CANSLIM_WEIGHT_L,
        "I": settings.CANSLIM_WEIGHT_I,
        "M": settings.CANSLIM_WEIGHT_M,
    }

    active_weights = dict(base_weights)
    if not institutional_data_available:
        removed_weight = active_weights["I"]
        active_weights["I"] = 0.0
        remaining_weight = 1.0 - removed_weight
        if remaining_weight > 0:
            for key in active_weights:
                if key != "I":
                    active_weights[key] = active_weights[key] / remaining_weight

    total_score = (
        active_weights["C"] * score_c
        + active_weights["A"] * score_a
        + active_weights["N"] * score_n
        + active_weights["S"] * score_s
        + active_weights["L"] * score_l
        + active_weights["I"] * score_i
        + active_weights["M"] * score_m
    ) * 100

    total_score = float(total_score)
    weighted_contributions = {key: active_weights[key] * scores[key] * 100 for key in scores}
    entry_weight = sum(weight for key, weight in active_weights.items() if key != "M")
    entry_composite_score = float(
        sum(active_weights[key] * scores[key] for key in scores if key != "M")
        * 100
        / entry_weight
        if entry_weight > 0
        else 0.0
    )
    entry_decision = evaluate_entry_contract(
        entry_facts,
        current_growth=current_growth,
        annual_growth=annual_growth,
        rs_score=rs_score,
        composite_score=entry_composite_score,
    )

    # 7. Compile metrics for reporting
    metrics = {
        "current_growth": current_growth,
        "annual_growth": annual_growth,
        "revenue_growth": revenue_growth,
        "roe": roe,
        "s_metrics": s_metrics,
        "proximity_to_high": proximity_to_high,
        "avg_volume_50": avg_volume_50,
        "prior_average_volume_50": entry_facts.prior_average_volume_50,
        "entry_volume_ratio": entry_facts.volume_ratio,
        "has_fundamentals": has_fundamentals,
        "fmp_quota_deferred": fmp_quota_deferred,
        "shares_outstanding": shares_outstanding,
        "quarterly_income_available": not quarterly_income.empty,
        "annual_income_available": not annual_income.empty,
        "balance_sheet_available": not balance_sheet.empty,
        "current_earnings_available": current_growth is not None,
        "annual_earnings_available": annual_growth is not None,
        "revenue_growth_available": revenue_growth is not None,
        "institutional_data_available": institutional_data_available,
        "income_statement_error": income_statement_error,
        "balance_sheet_error": balance_sheet_error,
    }

    return {
        "symbol": symbol,
        "scores": scores,
        "base_weights": base_weights,
        "active_weights": active_weights,
        "weighted_contributions": weighted_contributions,
        "data_availability": data_availability,
        "metrics": metrics,
        "total_score": total_score,
        "entry_composite_score": entry_composite_score,
        "entry_facts": entry_facts,
        "entry_decision": entry_decision,
        "rs_score": rs_score,
        "market_trend": market_trend,
        "is_breakout": entry_facts.in_buy_zone,
        "has_volume_surge": entry_facts.has_volume_surge,
        "buy_point": entry_facts.pivot,
        "latest_close_price": float(latest_close),
    }
