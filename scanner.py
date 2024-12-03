# scan for all stocks with above 50B market cap:
# use concurrent.futures for parallel search

from concurrent.futures import ThreadPoolExecutor
from trading_algo import find_trade_signals
import pandas as pd
import requests
from dotenv import load_dotenv
import os
from functools import lru_cache


load_dotenv()
api_key = os.getenv("FINNHUB_API_KEY")

@lru_cache(maxsize=100)
def get_large_cap_stocks_from_api(min_market_cap=50e9):
    """
    Fetch large-cap stocks with a market capitalization above the specified threshold using the Finnhub API.
    
    Args:
        min_market_cap (float): Minimum market capitalization (default is $50B).

    Returns:
        List[str]: A list of tickers for large-cap stocks.
    """
    base_url = "https://finnhub.io/api/v1/stock/symbol"
    market_cap_url = "https://finnhub.io/api/v1/stock/metric"
    exchange = "US"  # US exchange; modify for other exchanges if needed
    large_cap_stocks = []

    try:
        # Fetch all US stocks
        response = requests.get(f"{base_url}?exchange={exchange}&token={api_key}")
        response.raise_for_status()  # Raise exception for HTTP errors
        stocks = response.json()

        # Check market cap for each stock
        for stock in stocks:
            ticker = stock.get("symbol")
            if not ticker:
                continue

            # Fetch stock metrics to get market cap
            metric_response = requests.get(f"{market_cap_url}?symbol={ticker}&metric=all&token={api_key}")
            metric_response.raise_for_status()
            metrics = metric_response.json()
            
            # Extract market capitalization
            market_cap = metrics.get("metric", {}).get("marketCapitalization")
            if market_cap and market_cap >= min_market_cap:
                large_cap_stocks.append(ticker)

    except Exception as e:
        print(f"Error fetching data: {e}")

    return large_cap_stocks

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
    
    stocks = get_large_cap_stocks_from_api()
    print(f"stocks: {stocks}")

    all_signals = find_trade_signals_for_all(stocks, '2023-01-01', '2023-12-31')

    for stock, signals in all_signals:
        print(f"Signals for {stock}:\n{signals[['Close', 'RSI', 'Signal']]}\n")
