import pandas as pd
import requests
import yfinance as yf

# Scrapes the Wikipedia page for the list of S&P 500 tickers.
def get_sp500_tickers():
    print("Fetching S&P 500 ticker list...")
    
    # Set a User-Agent header to mimic a web browser to avoid 403 error accessing wikipedia
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
        
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()  # This will raise an error for bad responses (4xx or 5xx)
    except requests.exceptions.RequestException as e:
        print(f"Error fetching Wikipedia page: {e}")
        return []

    try:
        table = pd.read_html(response.text, flavor='lxml')[0]
    except ImportError:
        # Fallback if lxml is not installed, though it's recommended
        table = pd.read_html(response.text)[0]
    except Exception as e:
        print(f"Error parsing HTML table: {e}")
        return []
    
    tickers = table['Symbol'].str.replace('.', '-', regex=False).tolist()
    print(f"Found {len(tickers)} S&P 500 tickers.")
    return tickers

# Calculates the 12-month weighted performance for a single stock's data. 
# Weighting: 40% for the most recent 3-month period, and 20% for each of the three previous 3-month periods.
def calculate_weighted_performance(data_series):
    try:
        # Define the approximate number of trading days in a quarter (13 weeks * 5 days)
        days_per_q = 65 
        
        # Calculate performance for each discrete quarter
        perf_q1 = (data_series.iloc[-1] / data_series.iloc[-days_per_q]) - 1
        perf_q2 = (data_series.iloc[-days_per_q] / data_series.iloc[-2 * days_per_q]) - 1
        perf_q3 = (data_series.iloc[-2 * days_per_q] / data_series.iloc[-3 * days_per_q]) - 1
        perf_q4 = (data_series.iloc[-3 * days_per_q] / data_series.iloc[-4 * days_per_q]) - 1

        # Apply the weighting
        weighted_performance = (
            (0.40 * perf_q1) +
            (0.20 * perf_q2) +
            (0.20 * perf_q3) +
            (0.20 * perf_q4)
        )
        return weighted_performance
    except (IndexError, TypeError, ZeroDivisionError):
        # Handle cases where there isn't enough data (e.g., new IPO) or other errors
        return None


my_stock_list = ['GOOGL', 'MSFT', 'AMZN', 'TSLA', 'META']


sp500_tickers = get_sp500_tickers()
all_tickers_to_check = list(set(my_stock_list + sp500_tickers))

print(f"Downloading 14 months of data for {len(all_tickers_to_check)} total tickers...")
try:
    all_data = yf.download(all_tickers_to_check, period='14mo')['Close']
    print("Download complete.")
except Exception as e:
    print(f"Error downloading data: {e}")
    exit()

print("Calculating weighted performance for all stocks...")
rs_scores = all_data.apply(calculate_weighted_performance)

rs_df = rs_scores.reset_index()
rs_df.columns = ['Ticker', 'Weighted_Perf']
rs_df = rs_df.dropna()

# CANSLIM RS Score: Rank all stocks based on their performance, scale from 1 (worst) to 99 (best)
rs_df['RS_Score'] = rs_df['Weighted_Perf'].rank(pct=True) * 98 + 1
rs_df = rs_df.sort_values(by='RS_Score', ascending=False).reset_index(drop=True)

print("\n--- RS Scores ---")
my_stocks_df = rs_df[rs_df['Ticker'].isin(my_stock_list)]
print(my_stocks_df)

print("\n--- Top 10 Stocks in S&P 500 by RS Score ---")
print(rs_df.head(10))