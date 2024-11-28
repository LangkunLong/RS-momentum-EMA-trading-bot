# scan for all stocks with above 50B market cap:
# use concurrent.futures for parallel search

from concurrent.futures import ThreadPoolExecutor
from trading_algo import find_trade_signals
import pandas as pd
from yahooquery import Screener

def get_large_cap_stocks_from_api(min_market_cap=50e9):
    # Use Yahoo Finance's predefined large-cap stocks screener
    screener = Screener()
    screeners = screener.get_screeners('most_actives', count=100)  # can use other screeners like Finnhub or alphavintage
    large_cap_stocks = []

    try:
        results = screeners.get('quotes', [])
        for stock in results:
            ticker = stock.get('symbol')
            market_cap = stock.get('marketCap')

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

# Example usage
stocks = get_large_cap_stocks_from_api()
print(f"stocks: {stocks}")

all_signals = find_trade_signals_for_all(stocks, '2023-01-01', '2023-12-31')

for stock, signals in all_signals:
    print(f"Signals for {stock}:\n{signals[['Close', 'RSI', 'Signal']]}\n")
