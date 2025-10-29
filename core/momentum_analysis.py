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

# Download and align closing prices for specific stock and the benchmark.
# Yahoo Finance occasionally returns slightly different calendars for individual tickers (for example around holidays or when a stock has a
# trading halt).  The previous implementation sampled the *n*th value from the end of each series independently which meant we could end up comparing
# different trading days.  That skewed the relative strength calculation led to surprisingly negative scores for genuinely strong stocks.
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
        auto_adjust=False,
    )
    benchmark_data = yf.download(
        benchmark_symbol,
        start=start_date,
        end=end_date,
        progress=False,
        auto_adjust=False,
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

# calculate relative strength momentum score comparing stock to benchmark
# RS = (Stock performance / Benchmark performance) over period
def calculate_rs_momentum(symbol: str, benchmark_symbol: str = "SPY", period_days: int = 63) -> float:
    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=period_days + 30)  # Extra buffer for data
        
        stock_closes, benchmark_closes = _prepare_aligned_closes(
            symbol,
            benchmark_symbol,
            start_date,
            end_date,
        )

        if len(stock_closes) <= period_days or len(benchmark_closes) <= period_days:
            return 0.0

        window = pd.DataFrame({
            "stock": stock_closes,
            "benchmark": benchmark_closes,
        }).tail(period_days + 1)

        if window.shape[0] <= period_days:
            return 0.0
        
        stock_start = coerce_scalar(window["stock"].iloc[0])
        stock_current = coerce_scalar(window["stock"].iloc[-1])
        benchmark_start = coerce_scalar(window["benchmark"].iloc[0])
        benchmark_current = coerce_scalar(window["benchmark"].iloc[-1])

        if any(np.isclose(val, 0.0) for val in (stock_start, benchmark_start)):
            return 0.0

        stock_performance = (stock_current / stock_start - 1.0) * 100.0
        benchmark_performance = (benchmark_current / benchmark_start - 1.0) * 100.0
        rs_score = float(stock_performance - benchmark_performance)
        
        if not np.isfinite(rs_score):
            return 0.0
        
        return rs_score
        
    except Exception as exc: 
        print(f"Error calculating RS for {symbol}: {exc}")
        return 0.0