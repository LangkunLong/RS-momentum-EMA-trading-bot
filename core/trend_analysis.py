import pandas as pd
import numpy as np

# identify if uptrend: higher highs with higher lows
def analyze_trend_strength(df: pd.DataFrame, min_days: int = 60):

    if len(df) < min_days:
        return False, 0.0, {}

    recent_data = df.tail(min_days).copy()

    ema_8_adherence = float((recent_data['Above_8EMA'].sum() / len(recent_data)) * 100)
    ema_21_adherence = float((recent_data['Above_21EMA'].sum() / len(recent_data)) * 100)

    strong_ema_adherence = bool(ema_8_adherence > 70 or ema_21_adherence > 80)

    segment_size = max(min_days // 4, 1)
    segment_highs = []
    segment_lows = []

    for idx in range(4):
        start_idx = idx * segment_size
        end_idx = (idx + 1) * segment_size if idx < 3 else len(recent_data)
        segment = recent_data.iloc[start_idx:end_idx]

        if segment.empty:
            segment_highs.append(np.nan)
            segment_lows.append(np.nan)
            continue
        
        segment_highs.append(float(segment['High'].max()))
        segment_lows.append(float(segment['Low'].min()))
    
    higher_highs = False
    higher_lows = False
    
    try:
        if not any(pd.isna(segment_highs[i]) for i in range(3)):
            higher_highs = bool(segment_highs[0] <= segment_highs[1] <= segment_highs[2])
        
        if not any(pd.isna(segment_lows[i]) for i in range(3)):
            higher_lows = bool(segment_lows[0] <= segment_lows[1] <= segment_lows[2])
    except Exception:
        higher_highs = higher_lows = False

    trend_strength = {
        'ema_8_adherence': ema_8_adherence,
        'ema_21_adherence': ema_21_adherence,
        'higher_highs': higher_highs,
        'higher_lows': higher_lows,
        'strong_ema_adherence': strong_ema_adherence,
    }

    is_strong_trend = bool(strong_ema_adherence and (higher_highs or higher_lows))
    trend_score = float((ema_8_adherence + ema_21_adherence) / 2)

    return is_strong_trend, trend_score, trend_strength