from __future__ import annotations
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import yfinance as yf
import requests
import os
from datetime import datetime

# from core.yahoo_finance_helper import (
#     coerce_scalar,
#     extract_float_series,
#     normalize_price_dataframe,
# )

# use github csv instead 
def get_sp500_tickers() -> list[str]:
    print("Fetching S&P 500 ticker list from GitHub (reliable source)...")
    url = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
    try:
        # Read directly into pandas
        df = pd.read_csv(url)
        tickers = df['Symbol'].tolist()
        print(f"Found {len(tickers)} S&P 500 tickers.")
        return tickers
    except Exception as e:
        print(f"Error fetching S&P 500 list: {e}")
        # Fallback to a small list if everything fails, so code doesn't crash
        return ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA']

# Calculates the 12-month weighted performance for a single stock's data.
def calculate_weighted_performance(data_series: pd.Series) -> float | None:
    try:
        days_per_q = 65 
        
        if len(data_series) < 4 * days_per_q:
            return None

        perf_q1 = (data_series.iloc[-1] / data_series.iloc[-days_per_q]) - 1
        perf_q2 = (data_series.iloc[-days_per_q] / data_series.iloc[-2 * days_per_q]) - 1
        perf_q3 = (data_series.iloc[-2 * days_per_q] / data_series.iloc[-3 * days_per_q]) - 1
        perf_q4 = (data_series.iloc[-3 * days_per_q] / data_series.iloc[-4 * days_per_q]) - 1

        weighted_performance = (
            (0.40 * perf_q1) +
            (0.20 * perf_q2) +
            (0.20 * perf_q3) +
            (0.20 * perf_q4)
        )
        return weighted_performance
    except (IndexError, TypeError, ZeroDivisionError):
        return None

# Calculates the RS Score for a list of tickers, add caching to avoid recalculation
def calculate_rs_scores_for_tickers(tickers: list[str], cache_file='rs_scores_cache.csv') -> pd.DataFrame:
    # 1. Check if valid cache exists (calculated today)
    if os.path.exists(cache_file):
        file_time = datetime.fromtimestamp(os.path.getmtime(cache_file))
        if file_time.date() == datetime.now().date():
            print(f"Loading cached RS scores from {cache_file}...")
            # Load and ensure it returns the expected format
            df = pd.read_csv(cache_file)
            return df

    # 2. If no cache, perform the heavy calculation
    print("Cache outdated or missing. Recalculating RS scores (this takes time)...")
    sp500_tickers = get_sp500_tickers()
    
    # Combine your watchlist with S&P 500 to ensure valid ranking
    all_tickers_to_check = list(set(tickers + sp500_tickers))

    print(f"Downloading 14 months of data for {len(all_tickers_to_check)} total tickers...")
    try:
        # Batch download is faster; auto_adjust=True fixes split issues
        all_data = yf.download(all_tickers_to_check, period='14mo', progress=True, auto_adjust=True)['Close']
        
        # Drop columns with all NaNs (failed downloads)
        all_data = all_data.dropna(axis=1, how='all')
        print("Download complete.")
    except Exception as e:
        print(f"Error downloading data: {e}")
        return pd.DataFrame()

    print("Calculating weighted performance for all stocks...")
    rs_scores = all_data.apply(calculate_weighted_performance)

    rs_df = rs_scores.reset_index()
    rs_df.columns = ['Ticker', 'Weighted_Perf']
    rs_df = rs_df.dropna()

    # Calculate Percentile Rank (1-99)
    rs_df['RS_Score'] = rs_df['Weighted_Perf'].rank(pct=True) * 98 + 1
    rs_df = rs_df.sort_values(by='RS_Score', ascending=False).reset_index(drop=True)
    
    # 3. Save to cache for next time
    rs_df.to_csv(cache_file, index=False)
    print(f"RS Scores saved to {cache_file}")
    
    return rs_df

# This function is kept for compatibility but will now use the new ranking method.
def calculate_rs_momentum(symbol: str, rs_scores_df: pd.DataFrame) -> float:
    try:
        score = rs_scores_df[rs_scores_df['Ticker'] == symbol]['RS_Score'].iloc[0]
        return float(score)
    except (IndexError, KeyError):
        return 0.0
