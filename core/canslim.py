"""Utilities for evaluating CAN SLIM components.

This module focuses on translating readily-available fundamentals and
technicals from Yahoo Finance into the seven CAN SLIM pillars:

* **C**urrent quarterly earnings growth
* **A**nnual earnings growth
* **N**ew products/price leadership (approximated via revenue growth and
  proximity to 52-week highs)
* **S**upply and demand dynamics
* **L**eader or laggard (relative strength versus the benchmark)
* **I**nstitutional sponsorship
* **M**arket direction (SPY trend proxy)

Each component is normalised to a 0-1 range so the composite score can be
expressed on a 0-100 scale
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

import numpy as np
import pandas as pd
import yfinance as yf

from core.momentum_analysis import calculate_rs_momentum
from core.yahoo_finance_helper import (
    coerce_scalar,
    extract_float_series,
    normalize_price_dataframe,
)

#Light-weight representation of the general market trend
@dataclass
class MarketTrend:

    symbol: str
    score: float
    is_bullish: bool
    latest_close: Optional[float]
    indicators: Dict[str, float]
    
#  return YoY revenue growth as a decimal
def _safe_growth(current: float, previous: float) -> Optional[float]:
    if current is None or previous in (None, 0):
        return None

    try:
        current = float(current)
        previous = float(previous)
    except (TypeError, ValueError):
        return None

    if np.isclose(previous, 0.0):
        return None

    try:
        return (current - previous) / abs(previous)
    except ZeroDivisionError:
        return None

# Convert revenue growth into a 0-1 score
def _score_from_growth(growth: Optional[float], target: float) -> float:

    if growth is None:
        return 0.0

    return float(np.clip(growth / target, 0, 2) / 2)

# Normalise ratio between 0 and provated max-limit cap
def _score_from_ratio(value: Optional[float], cap: float) -> float:

    if value is None:
        return 0.0

    return float(np.clip(value / cap, 0, 1))

# current market direction using SPY benchmark, CANSLIM range of 0-100
def evaluate_market_direction(benchmark_symbol: str = "SPY") -> MarketTrend:

    try:
        data = yf.download(
            benchmark_symbol,
            period="1y",
            interval="1d",
            progress=False,
        )
    except Exception:
        data = pd.DataFrame()

    if data.empty or len(data) < 50:
        return MarketTrend(
            symbol=benchmark_symbol,
            score=0.4,
            is_bullish=False,
            latest_close=None,
            indicators={},
        )

    data = normalize_price_dataframe(data)
    closes = extract_float_series(data, "Close")
    
    ema_21 = closes.ewm(span=21).mean()
    ema_50 = closes.ewm(span=50).mean()
    ema_200 = closes.ewm(span=200).mean()
    
    latest_close = coerce_scalar(closes.iloc[-1])
    latest_ema_21 = coerce_scalar(ema_21.iloc[-1])
    latest_ema_50 = coerce_scalar(ema_50.iloc[-1])
    latest_ema_200 = coerce_scalar(ema_200.iloc[-1])

    trend_score = 0.0

    if latest_close > latest_ema_200:
        trend_score += 0.4

    if latest_ema_21 > latest_ema_50 > latest_ema_200:
        trend_score += 0.3

    if len(ema_50) > 20:
        ema_50_lookback = coerce_scalar(ema_50.iloc[-20])
        if latest_ema_50 > ema_50_lookback:
            trend_score += 0.2

    if latest_close > latest_ema_21:
        trend_score += 0.1

    trend_score = float(np.clip(trend_score, 0, 1))

    return MarketTrend(
        symbol=benchmark_symbol,
        score=trend_score,
        is_bullish=trend_score >= 0.6,
        latest_close=latest_close,
        indicators={
            "ema_21": latest_ema_21,
            "ema_50": latest_ema_50,
            "ema_200": latest_ema_200,
        },
    )


# separate into technical and fundamental (for stocks without a year's worth of data yet)
def evaluate_canslim(symbol: str, rs_scores_df: pd.DataFrame, market_trend: Optional[MarketTrend] = None) -> Optional[Dict[str, object]]:
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
    market_trend = market_trend or evaluate_market_direction()
    try:
        price_history = ticker.history(period="1y", interval="1d", auto_adjust=True)
    except Exception:
        return None

    if price_history.empty or len(price_history) < 30:
        return None

    # ... (Keep existing normalization and volume logic) ...
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
    
    turnover_ratio = (avg_volume_50 * 252) / shares_outstanding if (shares_outstanding and avg_volume_50) else None

    # 3. Robust Earnings/Revenue Growth Calculation
    current_growth = None
    revenue_growth = None
    annual_growth = None
    
    # Check if we actually have fundamental data
    has_fundamentals = False
    
    if not quarterly_income.empty:
        try:
            # Flexible label search (Net Income, Net Income Common Stock, etc.)
            net_income_row = quarterly_income.index[quarterly_income.index.str.contains('Net Income', case=False, regex=True)]
            revenue_row = quarterly_income.index[quarterly_income.index.str.contains('Revenue|Total Revenue', case=False, regex=True)]
            
            if not net_income_row.empty:
                earnings = quarterly_income.loc[net_income_row[0]].sort_index()
                if len(earnings) >= 2:
                    current_growth = _safe_growth(earnings.iloc[-1], earnings.iloc[-2])
                    has_fundamentals = True
            
            if not revenue_row.empty:
                revs = quarterly_income.loc[revenue_row[0]].sort_index()
                if len(revs) >= 4: # YoY Quarterly
                    revenue_growth = _safe_growth(revs.iloc[-1], revs.iloc[-4])
        except Exception:
            pass

    if not annual_income.empty:
        try:
            net_income_row = annual_income.index[annual_income.index.str.contains('Net Income', case=False, regex=True)]
            if not net_income_row.empty:
                earnings = annual_income.loc[net_income_row[0]].sort_index()
                if len(earnings) >= 2:
                    annual_growth = _safe_growth(earnings.iloc[-1], earnings.iloc[-2])
                    has_fundamentals = True
        except Exception:
            pass

    # 4. Scoring
    rs_score = calculate_rs_momentum(symbol, rs_scores_df)
    
    # Component Scores
    score_c = _score_from_growth(current_growth, 0.25)
    score_a = _score_from_growth(annual_growth, 0.25)
    score_n = float(0.7 * _score_from_growth(revenue_growth, 0.2) + 0.3 * _score_from_ratio(proximity_to_high, 1.05))
    score_s = _score_from_ratio(turnover_ratio, 1.5)
    score_l = rs_score / 100.0
    score_m = market_trend.score
    
    # Institutional (I) - Default to 0.5 if missing, or use Turnover as proxy
    score_i = 0.0
    if hasattr(info, 'held_percent_institutions'):
         score_i = _score_from_ratio(info.held_percent_institutions, 1.0)
    elif turnover_ratio:
         score_i = _score_from_ratio(turnover_ratio, 1.5)

    scores = {
        "C": score_c, "A": score_a, "N": score_n, 
        "S": score_s, "L": score_l, "I": score_i, "M": score_m
    }
    
    # DYNAMIC SCORING: Calculate weighted average based on available data
    # We always have L, M, and usually S and N (price based). 
    # If C and A are 0.0 due to missing data (not bad data), we re-weight.
    
    valid_components = ["L", "M", "N", "S", "I"]
    if has_fundamentals:
        valid_components.extend(["C", "A"])
        total_score = float(sum(scores.values()) / 7 * 100)
    else:
        # Speculative/Technical Score: Normalize based on 5 components
        # We assume C and A are missing.
        partial_sum = scores["L"] + scores["M"] + scores["N"] + scores["S"] + scores["I"]
        total_score = float(partial_sum / 5 * 100)

    metrics = {
        "current_growth": current_growth,
        "annual_growth": annual_growth,
        "revenue_growth": revenue_growth,
        "turnover_ratio": turnover_ratio,
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
