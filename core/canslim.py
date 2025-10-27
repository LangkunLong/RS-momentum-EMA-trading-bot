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

# Compute a CAN SLIM style score for ``symbol``. The function returns ``None`` if we fail to obtain enough data.
def evaluate_canslim(symbol: str, market_trend: Optional[MarketTrend] = None) -> Optional[Dict[str, object]]:

    ticker = yf.Ticker(symbol)

    try:
        quarterly_income_stmt = (
            ticker.quarterly_income_stmt if hasattr(ticker, "quarterly_income_stmt") else None
        )
        income_stmt = ticker.income_stmt if hasattr(ticker, "income_stmt") else None
        info = ticker.fast_info if hasattr(ticker, "fast_info") else {}
    except Exception:
        quarterly_income_stmt = income_stmt = None
        info = {}
    
    # derive annual and quarterly income
    annual = None
    if isinstance(income_stmt, pd.DataFrame) and not income_stmt.empty:
        net_income_label = next(
            (
                idx
                for idx in income_stmt.index
                if isinstance(idx, str) and idx.strip().lower() == "net income"
            ),
            None,
        )

        if net_income_label is not None:
            net_income = income_stmt.loc[net_income_label].dropna()
            if not net_income.empty:
                net_income = net_income.sort_index()
                annual = pd.DataFrame({"Earnings": net_income})

    quarterly = None
    if isinstance(quarterly_income_stmt, pd.DataFrame) and not quarterly_income_stmt.empty:
        net_income_label = next(
            (
                idx
                for idx in quarterly_income_stmt.index
                if isinstance(idx, str) and idx.strip().lower() == "net income"
            ),
            None,
        )
        revenue_label = next(
            (
                idx
                for idx in quarterly_income_stmt.index
                if isinstance(idx, str) and idx.strip().lower() in {"total revenue", "totalrevenue"}
            ),
            None,
        )

        net_income = (
            quarterly_income_stmt.loc[net_income_label].dropna()
            if net_income_label is not None
            else None
        )
        revenue = (
            quarterly_income_stmt.loc[revenue_label].dropna()
            if revenue_label is not None
            else None
        )

        if net_income is not None and not net_income.empty:
            net_income = net_income.sort_index()
            quarterly = pd.DataFrame({"Earnings": net_income})
            if revenue is not None and not revenue.empty:
                revenue = revenue.reindex(net_income.index, method=None).dropna()
                if not revenue.empty:
                    quarterly["Revenue"] = revenue

    
    market_trend = market_trend or evaluate_market_direction()
    price_history = pd.DataFrame()
    try:
        price_history = ticker.history(period="1y", interval="1d", auto_adjust=False)
    except Exception:
        price_history = pd.DataFrame()

    if price_history.empty or len(price_history) < 30:
        return None
    
    price_history = normalize_price_dataframe(price_history)
    closes = extract_float_series(price_history, "Close")
    latest_close = coerce_scalar(closes.iloc[-1])
    high_52 = coerce_scalar(closes.max())
    proximity_to_high = latest_close / high_52 if high_52 else 0.0

    try:
        volume_series = extract_float_series(price_history, "Volume")
    except (KeyError, TypeError):
        volume_series = pd.Series(dtype=float)

    avg_volume_50 = float(volume_series.tail(50).mean()) if not volume_series.tail(50).empty else 0.0
    shares_outstanding = None

    try:
        shares_outstanding = float(getattr(ticker, "shares_outstanding", None) or info.get("sharesOutstanding"))
    except Exception:
        shares_outstanding = None

    turnover_ratio = None
    if shares_outstanding and shares_outstanding > 0 and avg_volume_50:
        turnover_ratio = (avg_volume_50 * 252) / shares_outstanding

    # Current and annual earnings growth
    current_growth = None
    revenue_growth = None

    if isinstance(quarterly, pd.DataFrame) and not quarterly.empty and "Earnings" in quarterly:
        quarterly = quarterly.sort_index()
        if len(quarterly) >= 2:
            current_growth = _safe_growth(quarterly["Earnings"].iloc[-1], quarterly["Earnings"].iloc[-2])
        if len(quarterly) >= 4 and "Revenue" in quarterly:
            revenue_growth = _safe_growth(quarterly["Revenue"].iloc[-1], quarterly["Revenue"].iloc[-4])

    annual_growth = None
    if isinstance(annual, pd.DataFrame) and not annual.empty and "Earnings" in annual:
        annual = annual.sort_index()
        if len(annual) >= 2:
            annual_growth = _safe_growth(annual["Earnings"].iloc[-1], annual["Earnings"].iloc[-2])

    rs_score = calculate_rs_momentum(symbol)

    # Institutional sponsorship proxy
    institutional_score = 0.0
    institutional_percent = None

    try:
        holders_info = ticker.get_info() if hasattr(ticker, "get_info") else {}
        institutional_percent = holders_info.get("heldPercentInstitutions")
    except Exception:
        institutional_percent = None

    if institutional_percent is not None:
        institutional_score = _score_from_ratio(float(institutional_percent), 1.0)
    elif turnover_ratio is not None:
        institutional_score = _score_from_ratio(turnover_ratio, 1.5)

    scores = {
        "C": _score_from_growth(current_growth, 0.25),
        "A": _score_from_growth(annual_growth, 0.25),
        "N": float(
            0.7 * _score_from_growth(revenue_growth, 0.2)
            + 0.3 * _score_from_ratio(proximity_to_high, 1.05)
        ),
        "S": _score_from_ratio(turnover_ratio, 1.5),
        "L": float(np.clip((rs_score + 20) / 40, 0, 1)),
        "I": institutional_score,
        "M": market_trend.score,
    }

    total_score = float(sum(scores.values()) / len(scores) * 100)

    metrics = {
        "current_growth": current_growth,
        "annual_growth": annual_growth,
        "revenue_growth": revenue_growth,
        "turnover_ratio": turnover_ratio,
        "proximity_to_high": proximity_to_high,
        "avg_volume_50": avg_volume_50,
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
