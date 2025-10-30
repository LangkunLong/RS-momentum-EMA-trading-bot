from __future__ import annotations
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import yfinance as yf

from core.yahoo_finance_helper import (
    coerce_scalar,
    extract_float_series,
    normalize_price_dataframe,
)

# allign historical closes for the stock and benchmark
def _prepare_aligned_closes(
    symbol: str,
    benchmark_symbol: str,
    start_date: datetime,
    end_date: datetime,
) -> tuple[pd.Series, pd.Series]:
    stock_data = yf.download(
        symbol,
        start=start_date,
        end=end_date,
        progress=False,
        auto_adjust=True,
    )
    benchmark_data = yf.download(
        benchmark_symbol,
        start=start_date,
        end=end_date,
        progress=False,
        auto_adjust=True,
    )

    stock_data = normalize_price_dataframe(stock_data)
    benchmark_data = normalize_price_dataframe(benchmark_data)

    if "Close" not in stock_data or "Close" not in benchmark_data:
        raise KeyError("Close column missing from downloaded price data")

    stock_closes = extract_float_series(stock_data, "Close")
    benchmark_closes = extract_float_series(benchmark_data, "Close")

    aligned = (
        pd.DataFrame({
            "stock": stock_closes,
            "benchmark": benchmark_closes,
        })
        .dropna()
        .sort_index()
    )

    if aligned.empty:
        raise ValueError("No overlapping price history between symbol and benchmark")

    return aligned["stock"], aligned["benchmark"]

# CANSLIM RS score vs benchmakr
def calculate_rs_momentum(symbol: str, benchmark_symbol: str = "SPY", period_days: int = 63) -> float:
    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=period_days + 30)  # budffer for holidays + closures
        
        stock_closes, benchmark_closes = _prepare_aligned_closes(
            symbol,
            benchmark_symbol,
            start_date,
            end_date,
        )

        if len(stock_closes) <= period_days or len(benchmark_closes) <= period_days:
            return 0.0

        window = (
            pd.DataFrame(
                {
                    "stock": stock_closes,
                    "benchmark": benchmark_closes,
                }
            )
            .tail(period_days + 1)
            .dropna()
        )

        if window.shape[0] <= period_days:
            return 0.0
        
        returns = window.pct_change().dropna()
        if returns.empty or returns.shape[0] < period_days // 2:
            return 0.0
        
        stock_growth = float((1.0 + returns["stock"]).prod())
        benchmark_growth = float((1.0 + returns["benchmark"]).prod())

        if any(np.isclose(val, 0.0) for val in (stock_growth, benchmark_growth)):
            return 0.0
        
        rs_ratio = stock_growth / benchmark_growth
        rs_score = float((rs_ratio - 1.0) * 100.0)

        return rs_score if np.isfinite(rs_score) else 0.0
        
    except Exception as exc: 
        print(f"Error calculating RS for {symbol}: {exc}")
        return 0.0