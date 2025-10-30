from __future__ import annotations
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import yfinance as yf
import requests

# from core.yahoo_finance_helper import (
#     coerce_scalar,
#     extract_float_series,
#     normalize_price_dataframe,
# )

# Scrapes the Wikipedia page for the list of S&P 500 tickers.
def get_sp500_tickers() -> list[str]:
    print("Fetching S&P 500 ticker list...")
    url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
        
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching Wikipedia page: {e}")
        return []

    try:
        table = pd.read_html(response.text, flavor='lxml')[0]
    except ImportError:
        table = pd.read_html(response.text)[0]
    except Exception as e:
        print(f"Error parsing HTML table: {e}")
        return []
    
    tickers = table['Symbol'].str.replace('.', '-', regex=False).tolist()
    print(f"Found {len(tickers)} S&P 500 tickers.")
    return tickers

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

# Calculates the RS Score for a list of tickers
def calculate_rs_scores_for_tickers(tickers: list[str]) -> pd.DataFrame:
    sp500_tickers = get_sp500_tickers()
    all_tickers_to_check = list(set(tickers + sp500_tickers))

    print(f"Downloading 14 months of data for {len(all_tickers_to_check)} total tickers...")
    try:
        all_data = yf.download(all_tickers_to_check, period='14mo')['Close']
        print("Download complete.")
    except Exception as e:
        print(f"Error downloading data: {e}")
        return pd.DataFrame()

    print("Calculating weighted performance for all stocks...")
    rs_scores = all_data.apply(calculate_weighted_performance)

    rs_df = rs_scores.reset_index()
    rs_df.columns = ['Ticker', 'Weighted_Perf']
    rs_df = rs_df.dropna()

    rs_df['RS_Score'] = rs_df['Weighted_Perf'].rank(pct=True) * 98 + 1
    rs_df = rs_df.sort_values(by='RS_Score', ascending=False).reset_index(drop=True)
    
    return rs_df

# This function is kept for compatibility but will now use the new ranking method.
def calculate_rs_momentum(symbol: str, rs_scores_df: pd.DataFrame) -> float:
    try:
        score = rs_scores_df[rs_scores_df['Ticker'] == symbol]['RS_Score'].iloc[0]
        return float(score)
    except (IndexError, KeyError):
        return 0.0
