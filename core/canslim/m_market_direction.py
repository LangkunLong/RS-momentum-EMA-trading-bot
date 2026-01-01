"""
M - Market Direction

Evaluates the overall market trend using a benchmark (typically SPY).
CANSLIM emphasizes only buying stocks when the market is in a confirmed uptrend.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Optional
import numpy as np
import pandas as pd
import yfinance as yf
from config import settings
from core.yahoo_finance_helper import (
    coerce_scalar,
    extract_float_series,
    normalize_price_dataframe,
)


@dataclass
class MarketTrend:
    """Lightweight representation of the general market trend."""
    symbol: str
    score: float
    is_bullish: bool
    latest_close: Optional[float]
    indicators: Dict[str, float]


def evaluate_m(
    benchmark_symbol: str = "SPY",
    period: Optional[str] = None,
    price_above_200_weight: Optional[float] = None,
    ema_alignment_weight: Optional[float] = None,
    rising_50ema_weight: Optional[float] = None,
    price_above_21_weight: Optional[float] = None,
    bullish_threshold: Optional[float] = None,
    rising_lookback: Optional[int] = None
) -> MarketTrend:
    """
    Evaluate M (Market Direction) using the benchmark index.

    Args:
        benchmark_symbol: Ticker symbol for market benchmark (default: SPY)
        period: Historical period to analyze
        price_above_200_weight: Weight if price > 200-EMA
        ema_alignment_weight: Weight if EMAs are properly aligned
        rising_50ema_weight: Weight if 50-EMA is rising
        price_above_21_weight: Weight if price > 21-EMA
        bullish_threshold: Minimum score to consider market bullish
        rising_lookback: Days to check if 50-EMA is rising

    Returns:
        MarketTrend: Object containing market direction score and details
    """
    # Load defaults from configuration
    period = period or settings.MARKET_TREND_PERIOD
    price_above_200_weight = price_above_200_weight or settings.M_PRICE_ABOVE_200EMA_WEIGHT
    ema_alignment_weight = ema_alignment_weight or settings.M_EMA_ALIGNMENT_WEIGHT
    rising_50ema_weight = rising_50ema_weight or settings.M_50EMA_RISING_WEIGHT
    price_above_21_weight = price_above_21_weight or settings.M_PRICE_ABOVE_21EMA_WEIGHT
    bullish_threshold = bullish_threshold or settings.M_BULLISH_THRESHOLD
    rising_lookback = rising_lookback or settings.M_50EMA_RISING_LOOKBACK

    try:
        data = yf.download(
            benchmark_symbol,
            period=period,
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
        trend_score += price_above_200_weight

    if latest_ema_21 > latest_ema_50 > latest_ema_200:
        trend_score += ema_alignment_weight

    if len(ema_50) > rising_lookback:
        ema_50_lookback = coerce_scalar(ema_50.iloc[-rising_lookback])
        if latest_ema_50 > ema_50_lookback:
            trend_score += rising_50ema_weight

    if latest_close > latest_ema_21:
        trend_score += price_above_21_weight

    trend_score = float(np.clip(trend_score, 0, 1))

    return MarketTrend(
        symbol=benchmark_symbol,
        score=trend_score,
        is_bullish=trend_score >= bullish_threshold,
        latest_close=latest_close,
        indicators={
            "ema_21": latest_ema_21,
            "ema_50": latest_ema_50,
            "ema_200": latest_ema_200,
        },
    )
