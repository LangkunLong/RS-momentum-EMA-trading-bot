import pandas as pd
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from dotenv import load_dotenv
import os
import time
import yfinance as yf
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.stock_screening import screen_stocks_canslim, print_analysis_results
from config.settings import MIN_CANSLIM_SCORE, START_DATE
from quality_stocks import get_quality_stock_list, get_custom_watchlist

load_dotenv()
api_key = os.getenv("FINNHUB_API_KEY")

# --- Helper Functions ---

def get_yahoo_trending_tickers(count=20):
    """
    Fetches the currently trending tickers from Yahoo Finance US.
    """
    url = "https://query1.finance.yahoo.com/v1/finance/trending/US"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        result = data['finance']['result'][0]
        quotes = result.get('quotes', [])
        
        trending_tickers = [q['symbol'] for q in quotes]
        print(f"Fetched {len(trending_tickers)} trending tickers from Yahoo Finance.")
        return trending_tickers[:count]
        
    except Exception as e:
        print(f"Error fetching trending tickers: {e}")
        return []

def fetch_large_cap_stocks(min_market_cap=10e9):
    # (Optional: Kept for compatibility if you switch back to Finnhub)
    return []

# --- Main Scanner Logic ---

def scan_for_momentum_opportunities(
    use_api=False, 
    min_market_cap=10e9, 
    min_rs_score=10, 
    min_canslim_score=None,
    max_workers=3, 
    sectors=None, 
    custom_list=None,
    start_date=None,
    debug=False
):

    print("=" * 60)
    print("HIGH MOMENTUM PULLBACK SCANNER")
    print("=" * 60)
    
    # 1. Get Stock List
    if custom_list:
        print("Using custom stock list...")
        symbols = custom_list
    elif use_api:
        # Defaults to Yahoo Trending if 'use_api' is True
        print("Fetching trending tickers from Yahoo Finance...")
        symbols = get_yahoo_trending_tickers(count=20)
        
        # Fallback if trending fails
        if not symbols:
            print("Trending fetch failed, falling back to default quality list.")
            symbols = get_quality_stock_list()
    else:
        if sectors:
            print(f"Using curated stock list for sectors: {sectors}")
            symbols = get_quality_stock_list(sectors=sectors)
        else:
            print("Using default curated quality stock list...")
            symbols = get_quality_stock_list()
    
    if min_canslim_score is None:
        min_canslim_score = MIN_CANSLIM_SCORE

    if start_date is None:
        start_date = START_DATE
        
    print(f"Scanning {len(symbols)} stocks for momentum opportunities...")
    print(f"Minimum RS Score: {min_rs_score}")
    
    # REMOVED: The "is_valid_ticker" loop. 
    # Logic: The tickers come from Yahoo (Trending) or your curated list. 
    # Invalid tickers will be naturally filtered out during the RS Score batch download 
    # or inside the CANSLIM evaluation loop.
    
    opportunities, market_trend = screen_stocks_canslim(
        symbols=symbols,
        start_date=start_date,
        min_rs_score=min_rs_score,
        min_canslim_score=min_canslim_score,
        debug=debug
    )
    
    print(f"\nScan complete!")
    print(f"Analyzed: {len(symbols)} stocks")
    print(f"Opportunities found: {len(opportunities)} stocks")
    
    return opportunities, market_trend

if __name__ == "__main__":
    # Configuration
    USE_API = True  # Set to True to use Yahoo Trending
    MIN_RS_SCORE = 5
    MAX_WORKERS = 2
    
    DEBUG = True
    
    print("Stock Scanner Configuration:")
    print(f"- Use Trending (API): {USE_API}")
    print(f"- Min RS Score: {MIN_RS_SCORE}")
    
    opportunities, market_trend = scan_for_momentum_opportunities(
        use_api=USE_API,
        min_rs_score=MIN_RS_SCORE,
        max_workers=MAX_WORKERS,
        debug=DEBUG
    )
    
    print_analysis_results(opportunities, market_trend)