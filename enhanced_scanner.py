import pandas as pd
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from dotenv import load_dotenv
import os
from functools import lru_cache
from typing import Optional
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import time
import yfinance as yf

from core.yahoo_finance_helper import normalize_price_dataframe
from core.stock_screening import screen_stocks_canslim, print_analysis_results
from core.trending import get_yahoo_trending_tickers
from config.settings import MIN_CANSLIM_SCORE, START_DATE

load_dotenv()
api_key = os.getenv("FINNHUB_API_KEY")

def requests_with_retries():
    session = requests.Session()
    retry_strategy = Retry(
        total=5,
        status_forcelist=[429, 500, 502, 503, 504],
        backoff_factor=2,
        respect_retry_after_header=True
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

# preprocess ticker symbol, see if avaiable on yahoo finance
def is_valid_ticker(symbol, retries=1):
    for attempt in range(retries):
        try:
            df = yf.download(symbol, period="5d", progress=False, auto_adjust=True)
            df = normalize_price_dataframe(df)
            if not df.empty and 'Close' in df.columns:
                close_series = df['Close'].dropna()
                if len(close_series) > 0:
                    return True
        except Exception as e:
            if attempt == retries - 1:
                print(f"Failed to get ticker '{symbol}' after {retries} attempts: {e}")
    return False

@lru_cache(maxsize=1)
def fetch_large_cap_stocks(min_market_cap=10e9):
    """
    Fetch stocks with market cap above threshold (default 10B)
    """
    base_url = "https://finnhub.io/api/v1/stock/symbol"
    market_cap_url = "https://finnhub.io/api/v1/stock/metric"
    exchange = "US"

    try:
        session = requests_with_retries()
        print("Fetching stock symbols...")
        response = session.get(f"{base_url}?exchange={exchange}&token={api_key}")
        response.raise_for_status()
        stocks = response.json()
        
        print(f"Found {len(stocks)} stocks, filtering by market cap...")

        def fetch_market_cap(stock):
            ticker = stock.get("symbol")
            if not ticker or len(ticker) > 5:  # Skip complex tickers
                return None
            
            try:
                time.sleep(0.1)  # Rate limiting
                metric_response = session.get(
                    f"{market_cap_url}?symbol={ticker}&metric=all&token={api_key}",
                    timeout=10
                )
                metric_response.raise_for_status()
                metrics = metric_response.json()
                
                market_cap = metrics.get("metric", {}).get("marketCapitalization")
                if market_cap and market_cap >= min_market_cap:
                    return {
                        'symbol': ticker,
                        'market_cap': market_cap,
                        'name': stock.get('description', ticker)
                    }
                return None
                
            except Exception as e:
                print(f"Error fetching {ticker}: {e}")
                return None
        
        # Use parallel processing with rate limiting
        large_cap_stocks = []
        with ThreadPoolExecutor(max_workers=5) as executor:
            future_to_stock = {
                executor.submit(fetch_market_cap, stock): stock for stock in stocks[:500]} 
            
            for future in as_completed(future_to_stock):
                result = future.result()
                if result:
                    large_cap_stocks.append(result)
                    print(f"Found: {result['symbol']} - ${result['market_cap']/1e9:.1f}B")
        
        print(f"Filtered to {len(large_cap_stocks)} large cap stocks")
        return large_cap_stocks
    
    except Exception as e:
        print(f"Error fetching stocks: {e}")
        return []

from quality_stocks import get_quality_stock_list, get_sector_stocks, get_custom_watchlist



"""
Main scanning function for high momentum pullback opportunities

Args:
    use_api (bool): Use Finnhub API vs curated stock lists
    min_market_cap (float): Minimum market cap for API filtering
    min_rs_score (float): Minimum relative strength score
    max_workers (int): Number of concurrent analysis threads
    sectors (list): Specific sectors to scan (e.g., ['mega_cap_tech', 'healthcare'])
    custom_list (list): Custom list of stock symbols to scan
"""
def scan_for_momentum_opportunities(
    use_api=False, 
    min_market_cap=10e9, 
    min_rs_score=10, 
    min_canslim_score: Optional[float] = None,
    max_workers=3, 
    sectors=None, 
    custom_list=None,
    start_date: Optional[str] =  None,
    debug: bool = False
):

    print("=" * 60)
    print("HIGH MOMENTUM PULLBACK SCANNER")
    print("=" * 60)
    
    # Get stock list
    if custom_list:
        print("Using custom stock list...")
        symbols = custom_list
    elif use_api:
        print("Fetching trending tickers from Yahoo Finance...")
        trending = get_yahoo_trending_tickers(count=30)
        symbols = trending
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
    print(f"Minimum CAN SLIM Score: {min_canslim_score}")

    # filter stocks:
    # Filter out invalid/delisted tickers before scanning
    print("Validating tickers with Yahoo Finance...")
    valid_symbols = []
    for symbol in symbols:
        if is_valid_ticker(symbol):
            valid_symbols.append(symbol)
        else:
            print(f"Skipping invalid/delisted ticker: {symbol}")

    print(f"{len(valid_symbols)} valid tickers will be scanned.")

    opportunities, market_trend = screen_stocks_canslim(
        symbols=valid_symbols,
        start_date=start_date,
        min_rs_score=min_rs_score,
        min_canslim_score=min_canslim_score,
        debug=debug
    )
    
    print(f"\nScan complete!")
    print(f"Analyzed: {len(symbols)} stocks")
    print(f"Opportunities found: {len(opportunities)} stocks")
    
    return opportunities, market_trend

def export_results_to_csv(opportunities, filename=None):
    """Export results to CSV file"""
    if not opportunities:
        print("No opportunities to export.")
        return
    
    if filename is None:
        filename = f"momentum_opportunities_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    
    # Flatten the data for CSV export
    csv_data = []
    for opp in opportunities:
        base_row = {
            'Symbol': opp['symbol'],
            'RS_Score': opp['rs_score'],
            'Trend_Score': opp['trend_score'],
            'Current_Price': opp['current_price'],
            'Current_RSI': opp['current_rsi'],
            'EMA_8_Adherence': opp['trend_details']['ema_8_adherence'],
            'EMA_21_Adherence': opp['trend_details']['ema_21_adherence'],
            'Higher_Highs': opp['trend_details']['higher_highs'],
            'Higher_Lows': opp['trend_details']['higher_lows'],
            'Entry_Signals_Count': len(opp['entry_signals'])
        }
        
        # Add latest entry signal details
        if opp['entry_signals']:
            latest_signal = opp['entry_signals'][-1]
            latest_date = latest_signal['date']
            if isinstance(latest_date, (datetime, pd.Timestamp)):
                latest_date_str = latest_date.strftime('%Y-%m-%d')
            else:
                latest_date_str = str(latest_date)
            base_row.update({
                'Latest_Signal_Date': latest_date_str,
                'Latest_Signal_Types': ', '.join(latest_signal['signals']),
                'Latest_Signal_Price': latest_signal['close'],
                'Latest_Signal_RSI': latest_signal['rsi']
            })
        
        csv_data.append(base_row)
    
    df = pd.DataFrame(csv_data)
    df.to_csv(filename, index=False)
    print(f"Results exported to {filename}")

if __name__ == "__main__":
    # Configuration Options
    USE_API = True  # Set to True to use Finnhub API, False to use curated lists
    MIN_MARKET_CAP = 0  # 10 billion (only used if USE_API = True)
    MIN_RS_SCORE = 5  # Minimum relative strength score
    MAX_WORKERS = 2  # Concurrent analysis threads
    
    # Stock Selection Options (choose one):
    
    # Option 1: Use all quality stocks (default)
    # SECTORS = None
    # CUSTOM_LIST = None
    
    # Option 2: Focus on specific sectors
    SECTORS = ['growth_high_beta', 'crypto_fintech']
    CUSTOM_LIST = None
    
    # Option 3: Use your custom watchlist
    # SECTORS = None
    # CUSTOM_LIST = get_custom_watchlist()
    
    # Option 4: Tech stocks only
    # SECTORS = ['mega_cap_tech', 'large_cap_tech']
    # CUSTOM_LIST = None
    
    # Option 5: Conservative dividend stocks
    # SECTORS = ['dividend_defensive']
    # CUSTOM_LIST = None
    
    DEBUG = True
    
    print("Stock Scanner Configuration:")
    print(f"- Use API: {USE_API}")
    print(f"- Min Market Cap: ${MIN_MARKET_CAP/1e9:.0f}B")
    print(f"- Min RS Score: {MIN_RS_SCORE}")
    print(f"- Max Workers: {MAX_WORKERS}")
    print(f"- Sectors: {SECTORS}")
    print(f"- Custom List: {'Yes' if CUSTOM_LIST else 'No'}")
    
    # Run the scan
    opportunities, market_trend = scan_for_momentum_opportunities(
        use_api=USE_API,
        min_market_cap=MIN_MARKET_CAP,
        min_rs_score=MIN_RS_SCORE,
        max_workers=MAX_WORKERS,
        sectors=SECTORS,
        custom_list=CUSTOM_LIST,
        debug = DEBUG
    )
    
    print_analysis_results(opportunities, market_trend)
    
    # if opportunities:
    #     export_results_to_csv(opportunities)
    
    print("\nScan completed!")
