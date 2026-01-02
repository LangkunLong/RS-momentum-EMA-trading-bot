"""
CANSLIM Stock Scanner

Main scanning function for CANSLIM stock opportunities.
This simplified scanner focuses purely on CANSLIM criteria without pullback entries.

Args:
    min_rs_score (float): Minimum relative strength score
    min_canslim_score (float): Minimum CANSLIM composite score
    sectors (list): Specific sectors to scan (e.g., ['mega_cap_tech', 'healthcare'])
    custom_list (list): Custom list of stock symbols to scan
    start_date (str): Start date for analysis
    debug (bool): Enable verbose output
"""
import pandas as pd
from datetime import datetime
from typing import Optional
import yfinance as yf

from core.yahoo_finance_helper import normalize_price_dataframe
from core.stock_screening import screen_stocks_canslim, print_analysis_results
from quality_stocks import get_quality_stock_list, get_index_tickers
from config import settings


def is_valid_ticker(symbol, retries=1):
    """Preprocess ticker symbol, check if available on yahoo finance."""
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


def scan_for_canslim_stocks(
    min_rs_score=None,
    min_canslim_score=None,
    sectors=None,
    custom_list=None,
    start_date=None,
    debug=None
):

    # Load defaults from configuration
    min_rs_score = min_rs_score if min_rs_score is not None else settings.MIN_RS_SCORE
    min_canslim_score = min_canslim_score if min_canslim_score is not None else settings.MIN_CANSLIM_SCORE
    sectors = sectors if sectors is not None else settings.SECTORS
    custom_list = custom_list if custom_list is not None else settings.CUSTOM_LIST
    start_date = start_date if start_date is not None else settings.START_DATE
    debug = debug if debug is not None else settings.DEBUG

    print("=" * 60)
    print("CANSLIM STOCK SCANNER")
    print("=" * 60)

    # Get stock list
    if custom_list:
        print("Using custom stock list...")
        symbols = custom_list
    elif sectors:
        print(f"Using curated stock list for sectors: {sectors}")
        symbols = get_index_tickers(index_name = sectors)
    else:
        print("Using default curated quality stock list...")
        symbols = get_quality_stock_list()

    print(f"Scanning {len(symbols)} stocks for CANSLIM opportunities...")
    print(f"Minimum RS Score: {min_rs_score}")
    print(f"Minimum CANSLIM Score: {min_canslim_score}")

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
    """Export CANSLIM results to CSV file."""
    if not opportunities:
        print("No opportunities to export.")
        return

    if filename is None:
        filename = f"canslim_opportunities_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    # Flatten the data for CSV export
    csv_data = []
    for opp in opportunities:
        row = {
            'Symbol': opp['symbol'],
            'RS_Score': opp['rs_score'],
            'CANSLIM_Score': opp['total_score'],
            'C_Score': opp['scores']['C'] * 100,
            'A_Score': opp['scores']['A'] * 100,
            'N_Score': opp['scores']['N'] * 100,
            'S_Score': opp['scores']['S'] * 100,
            'L_Score': opp['scores']['L'] * 100,
            'I_Score': opp['scores']['I'] * 100,
            'M_Score': opp['scores']['M'] * 100,
            'Current_Growth': opp['metrics']['current_growth'],
            'Annual_Growth': opp['metrics']['annual_growth'],
            'Revenue_Growth': opp['metrics']['revenue_growth'],
            'Turnover_Ratio': opp['metrics']['turnover_ratio'],
            'Proximity_to_High': opp['metrics']['proximity_to_high'],
        }
        csv_data.append(row)

    df = pd.DataFrame(csv_data)
    df.to_csv(filename, index=False)
    print(f"Results exported to {filename}")


if __name__ == "__main__":
    # You can override default settings here or edit config/settings.py

    # Example: Override specific settings (uncomment to use)
    # MIN_RS_SCORE = 10
    # MIN_CANSLIM_SCORE = 75
    # SECTORS = ['growth_high_beta', 'crypto_fintech']
    # CUSTOM_LIST = None
    # DEBUG = True

    # Or just use defaults from config/settings.py
    MIN_RS_SCORE = None  # Uses settings.MIN_RS_SCORE
    MIN_CANSLIM_SCORE = None  # Uses settings.MIN_CANSLIM_SCORE
    # Available indices: 'sp500', 'nasdaq100', 'russell2000', 'large_cap', 'small_cap', 'all'
    # Set to None to scan all major indices (S&P 500 + Nasdaq 100 + Russell 2000)
    SECTORS = 'nasdaq100'  # Uses all indices from major markets
    CUSTOM_LIST = None
    DEBUG = True  # Override default

    print("CANSLIM Stock Scanner Configuration:")
    print(f"- Min RS Score: {MIN_RS_SCORE or settings.MIN_RS_SCORE}")
    print(f"- Min CANSLIM Score: {MIN_CANSLIM_SCORE or settings.MIN_CANSLIM_SCORE}")
    print(f"- Indices: {SECTORS or settings.SECTORS or 'All (S&P 500 + Nasdaq 100 + Russell 2000)'}")
    print(f"- Custom List: {'Yes' if CUSTOM_LIST else 'No'}")
    print(f"- Debug Mode: {DEBUG or settings.DEBUG}")

    # Run the scan
    opportunities, market_trend = scan_for_canslim_stocks(
        min_rs_score=MIN_RS_SCORE,
        min_canslim_score=MIN_CANSLIM_SCORE,
        sectors=SECTORS,
        custom_list=CUSTOM_LIST,
        debug=DEBUG
    )

    print_analysis_results(opportunities, market_trend)

    # Optionally export results to CSV
    # if opportunities:
    #     export_results_to_csv(opportunities)

    print("\nScan completed!")
