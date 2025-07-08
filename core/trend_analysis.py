import pandas as pd
import numpy as np

def analyze_trend_strength(df, min_days=60):
    """
    Analyze if stock is in a steady uptrend with higher highs and higher lows
    Must hold 8EMA or 21EMA for at least 2 months (60 trading days)
    """
    if len(df) < min_days:
        return False, 0, {}

    recent_data = df.tail(min_days).copy()

    # Check EMA adherence - ensure these are Python floats
    ema_8_adherence = float((recent_data['Above_8EMA'].sum() / len(recent_data)) * 100)
    ema_21_adherence = float((recent_data['Above_21EMA'].sum() / len(recent_data)) * 100)

    # Strong trend requires holding either 8EMA (>70%) or 21EMA (>80%)
    strong_ema_adherence = bool(ema_8_adherence > 70 or ema_21_adherence > 80)

    # Check for higher highs and higher lows pattern
    segment_size = min_days // 4
    segments = []

    # Create a completely flat structure with just Python floats
    segment_highs = []
    segment_lows = []

    for i in range(4):
        start_idx = i * segment_size
        end_idx = (i + 1) * segment_size if i < 3 else len(recent_data)
        segment = recent_data.iloc[start_idx:end_idx]

        if segment.empty:
            high = float('nan')
            low = float('nan')
            close_avg = float('nan')
        else:
            # Convert directly to Python float
            try:
                high_val = segment['High'].max()
                high = float(high_val) if not pd.isna(high_val) else float('nan')
                
                low_val = segment['Low'].min()
                low = float(low_val) if not pd.isna(low_val) else float('nan')
                
                close_val = segment['Close'].mean()
                close_avg = float(close_val) if not pd.isna(close_val) else float('nan')
            except:
                high = float('nan')
                low = float('nan')
                close_avg = float('nan')
        
        segment_highs.append(high)
        segment_lows.append(low)
        
        segments.append({
            'high': high,
            'low': low,
            'close_avg': close_avg
        })
    
    # Directly compare Python floats
    higher_highs = False
    higher_lows = False
    
    try:
        # Completely avoid any Series comparison by using the flat lists
        if not (pd.isna(segment_highs[0]) or pd.isna(segment_highs[1]) or pd.isna(segment_highs[2])):
            higher_highs = bool(segment_highs[0] <= segment_highs[1] <= segment_highs[2])
        
        if not (pd.isna(segment_lows[0]) or pd.isna(segment_lows[1]) or pd.isna(segment_lows[2])):
            higher_lows = bool(segment_lows[0] <= segment_lows[1] <= segment_lows[2])
    except Exception as e:
        print(f"Failed to compare segments: {e}")
        higher_highs = higher_lows = False

    trend_strength = {
        'ema_8_adherence': ema_8_adherence,
        'ema_21_adherence': ema_21_adherence,
        'higher_highs': higher_highs,
        'higher_lows': higher_lows,
        'strong_ema_adherence': strong_ema_adherence
    }

    is_strong_trend = bool(strong_ema_adherence and (higher_highs or higher_lows))
    trend_score = (ema_8_adherence + ema_21_adherence) / 2

    return is_strong_trend, trend_score, trend_strength