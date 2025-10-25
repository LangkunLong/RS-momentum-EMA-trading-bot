from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator

#Calculate EMA and RSI indicators required for the screen."""
def calculate_indicators(df):
    if len(df) < 21:
        return df
    
    df['8_EMA'] = EMAIndicator(df['Close'], window=8).ema_indicator()
    df['21_EMA'] = EMAIndicator(df['Close'], window=21).ema_indicator()
    
    df['RSI'] = RSIIndicator(df['Close'], window=14).rsi()
    
    df['Above_8EMA'] = (df['Close'] > df['8_EMA']).astype(bool)
    df['Above_21EMA'] = (df['Close'] > df['21_EMA']).astype(bool)
    
    df['Distance_8EMA'] = ((df['Close'] - df['8_EMA']) / df['8_EMA']) * 100
    df['Distance_21EMA'] = ((df['Close'] - df['21_EMA']) / df['21_EMA']) * 100
    
    return df