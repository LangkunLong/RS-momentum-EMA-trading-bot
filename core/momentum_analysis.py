import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta

def calculate_rs_momentum(symbol, benchmark_symbol='SPY', period_days=63):
    """
    Calculate Relative Strength momentum score comparing stock to benchmark
    RS = (Stock performance / Benchmark performance) over period
    """
    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=period_days + 30)  # Extra buffer for data
        
        # Fetch stock and benchmark data
        stock_data = yf.download(symbol, start=start_date, end=end_date, progress=False)
        benchmark_data = yf.download(benchmark_symbol, start=start_date, end=end_date, progress=False)
        
        if len(stock_data) < period_days or len(benchmark_data) < period_days:
            return 0.0
        
        # Calculate performance over period - ENSURE SCALAR VALUES
        stock_start = stock_data['Close'].iloc[-period_days]
        if isinstance(stock_start, pd.Series):
            stock_start = stock_start.iloc[0]
        stock_start = float(stock_start)
        
        stock_current = stock_data['Close'].iloc[-1]
        if isinstance(stock_current, pd.Series):
            stock_current = stock_current.iloc[0]
        stock_current = float(stock_current)
        
        stock_performance = (stock_current / stock_start - 1) * 100
        
        benchmark_start = benchmark_data['Close'].iloc[-period_days]
        if isinstance(benchmark_start, pd.Series):
            benchmark_start = benchmark_start.iloc[0]
        benchmark_start = float(benchmark_start)
        
        benchmark_current = benchmark_data['Close'].iloc[-1]
        if isinstance(benchmark_current, pd.Series):
            benchmark_current = benchmark_current.iloc[0]
        benchmark_current = float(benchmark_current)
        
        benchmark_performance = (benchmark_current / benchmark_start - 1) * 100
        
        # RS Score (relative strength) - ensure Python float
        rs_score = float(stock_performance - benchmark_performance)
        return rs_score
        
    except Exception as e:
        print(f"Error calculating RS for {symbol}: {e}")
        return 0.0