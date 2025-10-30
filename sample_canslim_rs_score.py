import pandas as pd
import yfinance as yf

# Scrapes the Wikipedia page for the list of S&P 500 tickers.
def get_sp500_tickers():
    print("Fetching S&P 500 ticker list...")
    url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
    # pd.read_html returns a list of DataFrames. The first one [0] is the table we want.
    table = pd.read_html(url)[0]
    
    # The 'Symbol' column contains the tickers.
    # We need to replace '.' with '-' for yfinance (e.g., 'BRK.B' -> 'BRK-B')
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