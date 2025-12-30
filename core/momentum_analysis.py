from __future__ import annotations
from datetime import datetime
import pandas as pd
import yfinance as yf
import os
from datetime import datetime
import time

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

# REPLACEMENT: Robust download with Chunking
# Downloads in small batches (50 stocks) to avoid the "curl: (28)" timeout.
def calculate_rs_scores_for_tickers(tickers: list[str], cache_file='rs_score_cache/rs_scores_cache.csv') -> pd.DataFrame:
    
    # 1. Check Cache
    if os.path.exists(cache_file):
        file_time = datetime.fromtimestamp(os.path.getmtime(cache_file))
        if file_time.date() == datetime.now().date():
            print(f"Loading cached RS scores from {cache_file}...")
            return pd.read_csv(cache_file)

    # 2. Add S&P 500 context
    sp500 = get_sp500_tickers()
    all_tickers = list(set(tickers + sp500))
    
    print(f"Downloading data for {len(all_tickers)} tickers in batches...")
    
    all_data_frames = []
    chunk_size = 50  # Download 50 stocks at a time to prevent Timeouts
    
    for i in range(0, len(all_tickers), chunk_size):
        chunk = all_tickers[i:i + chunk_size]
        print(f"Downloading batch {i//chunk_size + 1}/{(len(all_tickers)//chunk_size)+1} ({len(chunk)} tickers)...")
        
        try:
            data = yf.download(chunk, period='14mo', progress=False, auto_adjust=True)['Close']
            if isinstance(data, pd.Series):
                data = data.to_frame(name=chunk[0])
                
            all_data_frames.append(data)
            time.sleep(1) 
            
        except Exception as e:
            print(f"Batch failed: {e}")
            continue

    if not all_data_frames:
        print("All downloads failed.")
        return pd.DataFrame()

    # Combine all batches
    full_data = pd.concat(all_data_frames, axis=1)
    full_data = full_data.dropna(axis=1, how='all')

    print("Calculating weighted performance...")
    rs_scores = full_data.apply(calculate_weighted_performance)
    
    rs_df = rs_scores.reset_index()
    rs_df.columns = ['Ticker', 'Weighted_Perf']
    rs_df = rs_df.dropna()
    
    rs_df['RS_Score'] = rs_df['Weighted_Perf'].rank(pct=True) * 98 + 1
    rs_df = rs_df.sort_values(by='RS_Score', ascending=False).reset_index(drop=True)
    
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
