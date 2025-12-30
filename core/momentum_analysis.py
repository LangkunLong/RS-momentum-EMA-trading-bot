from __future__ import annotations
import pandas as pd
import yfinance as yf
import requests
import time
import os
from datetime import datetime

def get_sp500_tickers() -> list[str]:
    print("Fetching S&P 500 ticker list from GitHub (reliable source)...")
    url = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
    try:
        df = pd.read_csv(url)
        tickers = df['Symbol'].tolist()
        # FIX: Replace dots with dashes for Yahoo Finance (e.g. BRK.B -> BRK-B)
        tickers = [t.replace('.', '-') for t in tickers]
        print(f"Found {len(tickers)} S&P 500 tickers.")
        return tickers
    except Exception as e:
        print(f"Error fetching S&P 500 list: {e}")
        return ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA']

def calculate_weighted_performance(data_series: pd.Series) -> float | None:
    try:
        days_per_q = 65 
        if len(data_series) < 4 * days_per_q:
            return None

        # Use .iloc to get positions relative to the end
        curr = data_series.iloc[-1]
        q1 = data_series.iloc[-days_per_q]
        q2 = data_series.iloc[-2 * days_per_q]
        q3 = data_series.iloc[-3 * days_per_q]
        q4 = data_series.iloc[-4 * days_per_q]

        # Check for zeros to avoid division errors
        if any(x == 0 for x in [q1, q2, q3, q4]):
            return None

        perf_q1 = (curr / q1) - 1
        perf_q2 = (q1 / q2) - 1
        perf_q3 = (q2 / q3) - 1
        perf_q4 = (q3 / q4) - 1

        weighted_performance = (
            (0.40 * perf_q1) +
            (0.20 * perf_q2) +
            (0.20 * perf_q3) +
            (0.20 * perf_q4)
        )
        return weighted_performance
    except Exception:
        return None

def calculate_rs_scores_for_tickers(tickers: list[str], cache_file='rs_scores_cache.csv') -> pd.DataFrame:
    # 1. Check Cache
    if os.path.exists(cache_file):
        try:
            file_time = datetime.fromtimestamp(os.path.getmtime(cache_file))
            if file_time.date() == datetime.now().date():
                print(f"Loading cached RS scores from {cache_file}...")
                return pd.read_csv(cache_file)
        except Exception:
            print("Cache unreadable, recalculating...")

    # 2. Add S&P 500 context
    sp500 = get_sp500_tickers()
    all_tickers = list(set(tickers + sp500))
    
    print(f"Downloading data for {len(all_tickers)} tickers in batches...")
    
    all_data_frames = []
    chunk_size = 50 
    
    for i in range(0, len(all_tickers), chunk_size):
        chunk = all_tickers[i:i + chunk_size]
        print(f"Downloading batch {i//chunk_size + 1}/{(len(all_tickers)//chunk_size)+1} ({len(chunk)} tickers)...")
        
        try:
            # Download Close prices
            data = yf.download(chunk, period='14mo', progress=False, auto_adjust=True)
            
            # Handle different return types from yfinance
            if isinstance(data, pd.DataFrame):
                if 'Close' in data.columns:
                    closes = data['Close']
                else:
                    closes = data # Sometimes returns just the closes if 1 ticker
            else:
                continue

            # Ensure it's a DataFrame
            if isinstance(closes, pd.Series):
                closes = closes.to_frame(name=chunk[0])
            
            # Drop failed columns (all NaNs)
            closes = closes.dropna(axis=1, how='all')
            
            if not closes.empty:
                all_data_frames.append(closes)
                
            time.sleep(0.5) 
            
        except Exception as e:
            print(f"Batch failed: {e}")
            continue

    if not all_data_frames:
        print("All downloads failed.")
        return pd.DataFrame()

    print("Processing data alignment...")
    # Combine all batches
    full_data = pd.concat(all_data_frames, axis=1)
    
    # CRITICAL FIX: Forward Fill to propagate Friday prices to Sunday
    # This prevents Stocks from being NaN when compared to Crypto
    full_data = full_data.ffill()
    
    # Drop tickers that are still empty
    full_data = full_data.dropna(axis=1, how='all')

    print("Calculating weighted performance...")
    rs_scores = full_data.apply(calculate_weighted_performance)
    
    rs_df = rs_scores.reset_index()
    rs_df.columns = ['Ticker', 'Weighted_Perf']
    rs_df = rs_df.dropna()
    
    if rs_df.empty:
        print("Error: No valid RS scores calculated.")
        return pd.DataFrame()

    rs_df['RS_Score'] = rs_df['Weighted_Perf'].rank(pct=True) * 98 + 1
    rs_df = rs_df.sort_values(by='RS_Score', ascending=False).reset_index(drop=True)
    
    # Save to cache
    try:
        rs_df.to_csv(cache_file, index=False)
        print(f"RS Scores saved to {cache_file}")
    except Exception as e:
        print(f"Could not save cache: {e}")

# Legacy compatibility function wrapper
def calculate_rs_momentum(symbol: str, rs_scores_df: pd.DataFrame) -> float:
    try:
        score = rs_scores_df[rs_scores_df['Ticker'] == symbol]['RS_Score'].iloc[0]
        return float(score)
    except (IndexError, KeyError):
        return 0.0