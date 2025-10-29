import pandas as pd
import numpy as np
import yfinance as yf
from __future__ import annotations

from datetime import datetime, timedelta

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
    pass

# calculate relative strength momentum score comparing stock to benchmark
# RS = (Stock performance / Benchmark performance) over period
def calculate_rs_momentum(symbol, benchmark_symbol='SPY', period_days=63):
    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=period_days + 30)  # Extra buffer for data
        
        # Fetch stock and benchmark data
        stock_data = yf.download(symbol, 
                                 start=start_date, 
                                 end=end_date, 
                                 progress=False,
                                 auto_adjust=False)
        
        benchmark_data = yf.download(benchmark_symbol, 
                                     start=start_date, 
                                     end=end_date, 
                                     progress=False,
                                     auto_adjust=False)
        stock_data = normalize_price_dataframe(stock_data)
        benchmark_data = normalize_price_dataframe(benchmark_data)
        
        if len(stock_data) < period_days or len(benchmark_data) < period_days:
            return 0.0
        
        # Calculate performance over period - ENSURE SCALAR VALUES
        stock_closes = extract_float_series(stock_data, "Close")
        benchmark_closes = extract_float_series(benchmark_data, "Close")

        stock_start = coerce_scalar(stock_closes.iloc[-period_days])
        stock_current = coerce_scalar(stock_closes.iloc[-1])
        
        stock_performance = (stock_current / stock_start - 1) * 100
        
        benchmark_start = coerce_scalar(benchmark_closes.iloc[-period_days])
        benchmark_current = coerce_scalar(benchmark_closes.iloc[-1]) 
        benchmark_performance = (benchmark_current / benchmark_start - 1) * 100
        
        # RS Score (relative strength) - ensure Python float
        rs_score = float(stock_performance - benchmark_performance)
        return rs_score
        
    except Exception as e:
        print(f"Error calculating RS for {symbol}: {e}")
        return 0.0