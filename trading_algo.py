import pandas as pd
import numpy as np
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator

def calculate_indicators(df):
    # Calculate 8 EMA and 21 EMA
    df['8_EMA'] = EMAIndicator(df['Close'], window=8).ema_indicator()
    df['21_EMA'] = EMAIndicator(df['Close'], window=21).ema_indicator()
    
    # Calculate RSI (14-period default)
    df['RSI'] = RSIIndicator(df['Close'], window=14).rsi()
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
    df['Signal'] = (df['Uptrend'] & df['Pullback'] & (df['RSI'] > 70))

    # Return rows with signals
    return df[df['Signal']]

