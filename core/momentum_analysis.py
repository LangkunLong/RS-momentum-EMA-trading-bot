from __future__ import annotations
from datetime import datetime
import pandas as pd
import yfinance as yf
import os
import time

from config import settings
from core.index_ticker_fetcher import get_sp500_tickers as _fetch_sp500


def get_sp500_tickers() -> list[str]:
    """Fetch S&P 500 tickers via the shared IndexTickerFetcher (iShares CSV)."""
    return _fetch_sp500()

# Calculates the 12-month weighted performance for a single stock's data.
def calculate_weighted_performance(
    data_series: pd.Series,
    days_per_q=None,
    q1_weight=None,
    q2_weight=None,
    q3_weight=None,
    q4_weight=None
) -> float | None:
    # Load defaults from configuration
    days_per_q = days_per_q or settings.TRADING_DAYS_PER_QUARTER
    q1_weight = q1_weight or settings.RS_Q1_WEIGHT
    q2_weight = q2_weight or settings.RS_Q2_WEIGHT
    q3_weight = q3_weight or settings.RS_Q3_WEIGHT
    q4_weight = q4_weight or settings.RS_Q4_WEIGHT

    try:
        if len(data_series) < 4 * days_per_q:
            return None

        perf_q1 = (data_series.iloc[-1] / data_series.iloc[-days_per_q]) - 1
        perf_q2 = (data_series.iloc[-days_per_q] / data_series.iloc[-2 * days_per_q]) - 1
        perf_q3 = (data_series.iloc[-2 * days_per_q] / data_series.iloc[-3 * days_per_q]) - 1
        perf_q4 = (data_series.iloc[-3 * days_per_q] / data_series.iloc[-4 * days_per_q]) - 1

        weighted_performance = (
            (q1_weight * perf_q1) +
            (q2_weight * perf_q2) +
            (q3_weight * perf_q3) +
            (q4_weight * perf_q4)
        )
        return weighted_performance
    except (IndexError, TypeError, ZeroDivisionError):
        return None

# REPLACEMENT: Robust download with Chunking
# Downloads in small batches to avoid timeouts
def calculate_rs_scores_for_tickers(
    tickers: list[str],
    cache_file=None,
    chunk_size=None,
    period=None,
    percentile_multiplier=None,
    percentile_min=None
) -> pd.DataFrame:
    # Load defaults from configuration
    if cache_file is None:
        cache_dir = settings.RS_CACHE_DIR
        cache_filename = settings.RS_CACHE_FILE
        cache_file = os.path.join(cache_dir, cache_filename)
        # Ensure cache directory exists
        os.makedirs(cache_dir, exist_ok=True)

    chunk_size = chunk_size or settings.CHUNK_SIZE
    period = period or settings.RS_CALCULATION_PERIOD
    percentile_multiplier = percentile_multiplier or settings.RS_PERCENTILE_MULTIPLIER
    percentile_min = percentile_min or settings.RS_PERCENTILE_MIN

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

    for i in range(0, len(all_tickers), chunk_size):
        chunk = all_tickers[i:i + chunk_size]
        print(f"Downloading batch {i//chunk_size + 1}/{(len(all_tickers)//chunk_size)+1} ({len(chunk)} tickers)...")

        try:
            data = yf.download(chunk, period=period, progress=False, auto_adjust=True)['Close']
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

    rs_df['RS_Score'] = rs_df['Weighted_Perf'].rank(pct=True) * percentile_multiplier + percentile_min
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
