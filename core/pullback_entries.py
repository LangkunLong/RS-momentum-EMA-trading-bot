import pandas as pd

def identify_pullback_entries(df, lookback_days=10):
    """
    Identify entry signals based on pullback patterns:
    1. Pullback to retest 8EMA or 21EMA
    2. Broke 8EMA but held 21EMA and reclaimed 8EMA
    """
    if len(df) < lookback_days + 5:
        return []

    signals = []
    recent_data = df.tail(lookback_days + 5)

    for i in range(lookback_days, len(recent_data)):
        current_row = recent_data.iloc[i]
        previous_rows = recent_data.iloc[i-lookback_days:i]

        entry_signals = []

        # Signal 1: Pullback to 8EMA retest
        dist_8ema = float(current_row['Distance_8EMA'])
        above_8ema = bool(current_row['Above_8EMA'])
        prev_above_8ema_mean = float(previous_rows['Above_8EMA'].mean()) if not previous_rows['Above_8EMA'].empty else 0.0
        if (
            abs(dist_8ema) < 2.0 and
            above_8ema and
            prev_above_8ema_mean > 0.7
        ):
            entry_signals.append('8EMA_Retest')

        # Signal 2: Pullback to 21EMA retest
        dist_21ema = float(current_row['Distance_21EMA'])
        above_21ema = bool(current_row['Above_21EMA'])
        prev_above_21ema_mean = float(previous_rows['Above_21EMA'].mean()) if not previous_rows['Above_21EMA'].empty else 0.0
        if (
            abs(dist_21ema) < 3.0 and
            above_21ema and
            prev_above_21ema_mean > 0.8
        ):
            entry_signals.append('21EMA_Retest')

        # Signal 3: Broke 8EMA but held 21EMA and reclaimed 8EMA
        above_8ema_tail = previous_rows['Above_8EMA'].tail(5)
        broke_8ema_recently = False
        if not above_8ema_tail.empty:
            bool_values = [bool(x) for x in above_8ema_tail.values.flatten()]
            broke_8ema_recently = any(not x for x in bool_values)

        above_21ema_tail = previous_rows['Above_21EMA'].tail(5)
        held_21ema = bool(above_21ema) and not above_21ema_tail.empty
        if held_21ema:
            bool_values = [bool(x) for x in above_21ema_tail.values.flatten()]
            held_21ema = held_21ema and all(bool_values)

        reclaimed_8ema = above_8ema

        if broke_8ema_recently and held_21ema and reclaimed_8ema:
            entry_signals.append('8EMA_Reclaim')

        if entry_signals:
            # Convert the date to string format to avoid serialization issues
            date_val = current_row.name
            if isinstance(date_val, pd.Timestamp):
                date_val = date_val.strftime('%Y-%m-%d')
            
            signals.append({
                'date': date_val,
                'close': float(current_row['Close']),
                'signals': entry_signals,
                'rsi': float(current_row['RSI']),
                'distance_8ema': dist_8ema,
                'distance_21ema': dist_21ema
            })

    return signals