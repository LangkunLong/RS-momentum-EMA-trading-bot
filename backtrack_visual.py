import os
import glob
import pandas as pd
import plotly.graph_objects as go
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import Adjustment
from config.settings import ALPACA_API_KEY, ALPACA_SECRET_KEY  # Adjust import to your config


def get_latest_backtest_file(directory="."):
    """Finds the most recent backtest CSV in the specified directory."""
    # Look for files matching your specific naming convention
    search_pattern = os.path.join(directory, "backtest_results_*.csv")
    list_of_files = glob.glob(search_pattern)

    if not list_of_files:
        raise FileNotFoundError("No backtest result CSV files found in the current directory.")

    # Sort files by creation time and pick the latest one
    latest_file = max(list_of_files, key=os.path.getctime)
    print(f"Loaded latest backtest file: {latest_file}")

    return latest_file


def visualize_signals(csv_file: str):
    # 1. Load your backtest CSV
    df = pd.read_csv(csv_file)
    df["Date"] = pd.to_datetime(df["Date"]).dt.tz_localize("America/New_York")

    # 2. Get unique tickers that had at least one BUY_SIGNAL
    buy_signals = df[df["BUY_SIGNAL"] == True]
    tickers_with_buys = buy_signals["Ticker"].unique()

    client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

    for ticker in tickers_with_buys:
        ticker_df = df[df["Ticker"] == ticker]
        start_date = ticker_df["Date"].min()
        end_date = ticker_df["Date"].max()

        # 3. Fetch OHLCV data from Alpaca
        request_params = StockBarsRequest(
            symbol_or_symbols=[ticker],
            timeframe=TimeFrame.Day,
            start=start_date,
            end=end_date,
            adjustment=Adjustment.ALL,  # Adhering to your architecture
        )
        bars = client.get_stock_bars(request_params).df.reset_index()
        bars["timestamp"] = pd.to_datetime(bars["timestamp"])

        # 4. Merge Alpaca Candlesticks with your CSV Signals
        merged = pd.merge(bars, ticker_df, left_on="timestamp", right_on="Date", how="inner")

        # 5. Plotting
        fig = go.Figure()

        # Candlestick Trace
        fig.add_trace(
            go.Candlestick(
                x=merged["Date"],
                open=merged["open"],
                high=merged["high"],
                low=merged["low"],
                close=merged["close"],
                name=f"{ticker} Price",
            )
        )

        # Buy Signal Markers
        buys = merged[merged["BUY_SIGNAL"] == True]
        fig.add_trace(
            go.Scatter(
                x=buys["Date"],
                y=buys["low"] * 0.95,  # Plot slightly below the wick
                mode="markers",
                marker=dict(
                    symbol="triangle-up", size=16, color="rgba(0, 255, 0, 0.9)", line=dict(width=2, color="black")
                ),
                name="CANSLIM Buy Signal",
            )
        )

        fig.update_layout(
            title=f"CANSLIM Backtest Signals: {ticker} (Mkt Filter Active)",
            yaxis_title="Price (Split/Div Adjusted)",
            xaxis_title="Date",
            template="plotly_dark",
            xaxis_rangeslider_visible=False,
        )

        fig.show()


if __name__ == "__main__":
    latest_csv = get_latest_backtest_file()
    visualize_signals(latest_csv)
