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

    closes = data["Close"].astype(float)
    ema_21 = closes.ewm(span=21).mean()
    ema_50 = closes.ewm(span=50).mean()
    ema_200 = closes.ewm(span=200).mean()

    latest_close = float(closes.iloc[-1])
    latest_ema_21 = float(ema_21.iloc[-1])
    latest_ema_50 = float(ema_50.iloc[-1])
    latest_ema_200 = float(ema_200.iloc[-1])

    trend_score = 0.0

    if latest_close > latest_ema_200:
        trend_score += 0.4

    if latest_ema_21 > latest_ema_50 > latest_ema_200:
        trend_score += 0.3

    if latest_ema_50 > float(ema_50.iloc[-20]):
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

