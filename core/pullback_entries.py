from __future__ import annotations
import pandas as pd
from config import settings

def identify_pullback_entries(
    df,
    lookback_days=None,
    tolerance_8ema=None,
    tolerance_21ema=None,
    min_adherence_8ema=None,
    min_adherence_21ema=None,
    reclaim_lookback=None
):
    # Load defaults from configuration
    lookback_days = lookback_days or settings.PULLBACK_LOOKBACK_DAYS
    tolerance_8ema = tolerance_8ema or settings.PULLBACK_8EMA_TOLERANCE
    tolerance_21ema = tolerance_21ema or settings.PULLBACK_21EMA_TOLERANCE
    min_adherence_8ema = min_adherence_8ema or settings.MIN_ADHERENCE_8EMA
    min_adherence_21ema = min_adherence_21ema or settings.MIN_ADHERENCE_21EMA
    reclaim_lookback = reclaim_lookback or settings.RECLAIM_LOOKBACK_DAYS

    if len(df) < lookback_days + 5:
        return []

    signals = []
    recent_data = df.tail(lookback_days + 5)

    for idx in range(lookback_days, len(recent_data)):
        current_row = recent_data.iloc[idx]
        history = recent_data.iloc[idx - lookback_days:idx]

        entry_signals = []

        # 8EMA retest
        dist_8ema = float(current_row['Distance_8EMA'])
        above_8ema = bool(current_row['Above_8EMA'])
        prev_above_8ema = history['Above_8EMA'].mean() if not history['Above_8EMA'].empty else 0.0
        if abs(dist_8ema) < tolerance_8ema and above_8ema and prev_above_8ema > min_adherence_8ema:
            entry_signals.append('8EMA_Retest')

        # 21EMA retest
        dist_21ema = float(current_row['Distance_21EMA'])
        above_21ema = bool(current_row['Above_21EMA'])
        prev_above_21ema = history['Above_21EMA'].mean() if not history['Above_21EMA'].empty else 0.0
        if abs(dist_21ema) < tolerance_21ema and above_21ema and prev_above_21ema > min_adherence_21ema:
            entry_signals.append('21EMA_Retest')

        # broke 8EMA but held 21EMA and reclaimed 8EMA
        above_8ema_tail = history['Above_8EMA'].tail(reclaim_lookback)
        broke_8ema = any(not bool(val) for val in above_8ema_tail.values) if not above_8ema_tail.empty else False

        above_21ema_tail = history['Above_21EMA'].tail(reclaim_lookback)
        held_21ema = bool(above_21ema_tail.all()) if not above_21ema_tail.empty else False

        if broke_8ema and held_21ema and above_8ema:
            entry_signals.append('8EMA_Reclaim')

        if entry_signals:
        
            date_val = current_row.name
            if isinstance(date_val, pd.Timestamp):
                date_val = date_val.strftime('%Y-%m-%d')
            
            signals.append(
                {
                    'date': date_val,
                    'close': float(current_row['Close']),
                    'signals': entry_signals,
                    'rsi': float(current_row['RSI']),
                    'distance_8ema': dist_8ema,
                    'distance_21ema': dist_21ema,
                }
            )

    return signals