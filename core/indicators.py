from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator

def calculate_indicators(df):
    """Calculate technical indicators"""
    if len(df) < 21:
        return df
    
    # Calculate EMAs
    df['8_EMA'] = EMAIndicator(df['Close'], window=8).ema_indicator()
    df['21_EMA'] = EMAIndicator(df['Close'], window=21).ema_indicator()
    
    # Calculate RSI
    df['RSI'] = RSIIndicator(df['Close'], window=14).rsi()
    
    # Calculate price vs EMA relationships - ENSURE BOOLEAN CONVERSION
    df['Above_8EMA'] = (df['Close'] > df['8_EMA']).astype(bool)
    df['Above_21EMA'] = (df['Close'] > df['21_EMA']).astype(bool)
    
    # Calculate distances as float
    df['Distance_8EMA'] = ((df['Close'] - df['8_EMA']) / df['8_EMA']) * 100
    df['Distance_21EMA'] = ((df['Close'] - df['21_EMA']) / df['21_EMA']) * 100
    
    return df