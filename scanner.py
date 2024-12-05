# scan for all stocks with above 50B market cap:
# use concurrent.futures for parallel search

from concurrent.futures import ThreadPoolExecutor
from trading_algo import find_trade_signals
import pandas as pd
import requests
from dotenv import load_dotenv
import os
from functools import lru_cache
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()
api_key = os.getenv("FINNHUB_API_KEY")

def requests_with_retries():
    session = requests.Session()
    retry_strategy = Retry(
        total=5,
        status_forcelist=[429, 500, 502, 503, 504],
        backoff_factor=1
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

# Cache the results to avoid redundant API calls and improve performance
@lru_cache(maxsize=100)
def fetch_large_cap_stocks(min_market_cap=50e9):
    base_url = "https://finnhub.io/api/v1/stock/symbol"
    market_cap_url = "https://finnhub.io/api/v1/stock/metric"
    exchange = "US"

    try:
        session = requests_with_retries()
        response = session.get(f"{base_url}?exchange={exchange}&token={api_key}")
        response.raise_for_status()
        stocks = response.json()

        def fetch_market_cap(stock):
            ticker = stock.get("symbol")
            if not ticker:
                return None
            try:
                metric_response = session.get(f"{market_cap_url}?symbol={ticker}&metric=all&token={api_key}")
                metric_response.raise_for_status()
                metrics = metric_response.json()
                market_cap = metrics.get("metric", {}).get("marketCapitalization")
                return ticker if market_cap and market_cap >= min_market_cap else None
            except Exception as e:
                print(f"Error fetching {ticker}: {e}")
                return None
        
        # use parallel processing to fetch market cap 
        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(fetch_market_cap, stocks))
        return [r for r in results if r]
    
    except Exception as e:
        print(f"Error fetching stocks: {e}")
        return []

def find_trade_signals_for_all(stocks, start_date, end_date):
    all_signals = []
    
    def process_stock(stock):
        signals = find_trade_signals(stock, start_date, end_date)
        if not signals.empty:
            return stock, signals
        return None

    # Process stocks in parallel
    with ThreadPoolExecutor(max_workers=5) as executor:
        results = executor.map(process_stock, stocks)
    
    for result in results:
        if result:
            all_signals.append(result)
    
    return all_signals

def summarize_signals(all_signals):
    summary = []
    for stock, signals in all_signals:
        for _, row in signals.iterrows():
            summary.append({'Stock': stock, 'Date': row.name, 'Close': row['Close'], 'RSI': row['RSI']})
    return pd.DataFrame(summary)

if __name__ == "__main__":
    # confirm api key:
    print(f"API key: {api_key}")
    
    stocks = fetch_large_cap_stocks()
    print(f"stocks: {stocks}")

    all_signals = find_trade_signals_for_all(stocks, '2023-01-01', '2023-12-31')

    for stock, signals in all_signals:
        print(f"Signals for {stock}:\n{signals[['Close', 'RSI', 'Signal']]}\n")
