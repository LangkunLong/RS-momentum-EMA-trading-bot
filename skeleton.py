import pandas as pd
import numpy as np
import yfinance as yf
from ta.trend import EMAIndicator

def calculate_indicators(df):
    # Calculate 8 EMA and 21 EMA
    df['8_EMA'] = EMAIndicator(df['Close'], window=8).ema_indicator()
    df['21_EMA'] = EMAIndicator(df['Close'], window=21).ema_indicator()
    
    # Calculate RS Rating (mock calculation: price % change over 14 days)
    # Or can use relative strength index
    df['RS_Rating'] = df['Close'].pct_change(14).apply(lambda x: (x + 1) * 10 if x > 0 else 0)
    return df

def is_uptrend(row):
    return row['Close'] > row['21_EMA']

def is_pullback(row):
    return (abs(row['Close'] - row['8_EMA']) < 0.5) or (abs(row['Close'] - row['21_EMA']) < 0.5)

def find_trade_signals(symbol, start_date, end_date):
    # Fetch historical data
    df = yf.download(symbol, start=start_date, end=end_date)
    df = calculate_indicators(df)

    # Identify trade signals
    df['Uptrend'] = df.apply(is_uptrend, axis=1)
    df['Pullback'] = df.apply(is_pullback, axis=1)
    df['Signal'] = (df['Uptrend'] & df['Pullback'] & (df['RS_Rating'] > 7))

    # Return rows with signals
    return df[df['Signal']]

# Example usage
signals = find_trade_signals('AAPL', '2023-01-01', '2023-12-31')
print(signals[['Close', '8_EMA', '21_EMA', 'RS_Rating', 'Signal']])
