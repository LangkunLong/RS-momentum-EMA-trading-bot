# get trending tickers
import requests
import pandas as pd

def get_yahoo_trending_tickers(count=20):
    """
    Fetches the currently trending tickers from Yahoo Finance US.
    """
    url = "https://query1.finance.yahoo.com/v1/finance/trending/US"
    
    # User-agent is required to avoid 403 Forbidden errors
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        # Parse the JSON response
        result = data['finance']['result'][0]
        quotes = result.get('quotes', [])
        
        trending_tickers = [q['symbol'] for q in quotes]
        print(f"Fetched {len(trending_tickers)} trending tickers from Yahoo Finance.")
        return trending_tickers[:count]
        
    except Exception as e:
        print(f"Error fetching trending tickers: {e}")
        return []

# Usage Example:
trending = get_yahoo_trending_tickers()
print(trending)