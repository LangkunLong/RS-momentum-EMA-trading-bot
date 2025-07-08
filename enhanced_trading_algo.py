import pandas as pd
import numpy as np
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator
from datetime import datetime, timedelta
from core.trend_analysis import analyze_trend_strength
import warnings
warnings.filterwarnings('ignore')

START_DATE = '2025-01-01'

def calculate_rs_momentum(symbol, benchmark_symbol='SPY', period_days=63):
    """
    Calculate Relative Strength momentum score comparing stock to benchmark
    RS = (Stock performance / Benchmark performance) over period
    """
    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=period_days + 30)  # Extra buffer for data
        
        # Fetch stock and benchmark data
        stock_data = yf.download(symbol, start=start_date, end=end_date, progress=False)
        benchmark_data = yf.download(benchmark_symbol, start=start_date, end=end_date, progress=False)
        
        if len(stock_data) < period_days or len(benchmark_data) < period_days:
            return 0.0
        
        # Calculate performance over period - ENSURE SCALAR VALUES
        stock_start = stock_data['Close'].iloc[-period_days]
        if isinstance(stock_start, pd.Series):
            stock_start = stock_start.iloc[0]
        stock_start = float(stock_start)
        
        stock_current = stock_data['Close'].iloc[-1]
        if isinstance(stock_current, pd.Series):
            stock_current = stock_current.iloc[0]
        stock_current = float(stock_current)
        
        stock_performance = (stock_current / stock_start - 1) * 100
        
        benchmark_start = benchmark_data['Close'].iloc[-period_days]
        if isinstance(benchmark_start, pd.Series):
            benchmark_start = benchmark_start.iloc[0]
        benchmark_start = float(benchmark_start)
        
        benchmark_current = benchmark_data['Close'].iloc[-1]
        if isinstance(benchmark_current, pd.Series):
            benchmark_current = benchmark_current.iloc[0]
        benchmark_current = float(benchmark_current)
        
        benchmark_performance = (benchmark_current / benchmark_start - 1) * 100
        
        # RS Score (relative strength) - ensure Python float
        rs_score = float(stock_performance - benchmark_performance)
        return rs_score
        
    except Exception as e:
        print(f"Error calculating RS for {symbol}: {e}")
        return 0.0

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

"""
Identify entry signals based on pullback patterns:
1. Pullback to retest 8EMA or 21EMA
2. Broke 8EMA but held 21EMA and reclaimed 8EMA
"""
def identify_pullback_entries(df, lookback_days=10):
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
        # Ensure 1D data: convert to list of bool values
        broke_8ema_recently = False
        if not above_8ema_tail.empty:
            bool_values = [bool(x) for x in above_8ema_tail.values.flatten()]
            broke_8ema_recently = any(not x for x in bool_values)

        above_21ema_tail = previous_rows['Above_21EMA'].tail(5)
        # Ensure 1D data
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
                'date': date_val,  # Store as string instead of Timestamp object
                'close': float(current_row['Close']),
                'signals': entry_signals,
                'rsi': float(current_row['RSI']),
                'distance_8ema': dist_8ema,
                'distance_21ema': dist_21ema
            })

    return signals

def find_high_momentum_entries(symbol, start_date, end_date, min_rs_score=10):
    """
    Main function to find high momentum stocks with pullback entries
    """
    try:
        # Calculate RS momentum score
        rs_score = calculate_rs_momentum(symbol)
        
        # Skip if RS score is below threshold
        if float(rs_score) < float(min_rs_score):
            return None
        
        # Fetch historical data with extra buffer for indicators
        extended_start = pd.to_datetime(start_date) - timedelta(days=100)
        df = yf.download(symbol, start=extended_start, end=end_date, progress=False)
        
        if df.empty or len(df) < 100:
            return None
        
        # Calculate indicators
        df = calculate_indicators(df)
        
        # Analyze trend strength - no debug prints!
        is_strong_trend, trend_score, trend_details = analyze_trend_strength(df)
        
        # Immediately convert all trend_details values to Python primitives
        safe_trend_details = {
            'ema_8_adherence': float(trend_details.get('ema_8_adherence', 0)),
            'ema_21_adherence': float(trend_details.get('ema_21_adherence', 0)),
            'higher_highs': bool(trend_details.get('higher_highs', False)),
            'higher_lows': bool(trend_details.get('higher_lows', False)),
            'strong_ema_adherence': bool(trend_details.get('strong_ema_adherence', False))
        }
        
        if not bool(is_strong_trend):
            return None
        
        # Identify pullback entries in the specified date range
        start_date_dt = pd.to_datetime(start_date)
        analysis_df = df[df.index >= start_date_dt]
        entry_signals = identify_pullback_entries(analysis_df)
        
        if len(entry_signals) == 0:
            return None
        
        # Handle DataFrame access safely
        try:
            current_price = df['Close'].iloc[-1]
            if isinstance(current_price, pd.Series):
                current_price = current_price.iloc[0]
            current_price = float(current_price)
            
            current_rsi = df['RSI'].iloc[-1]
            if isinstance(current_rsi, pd.Series):
                current_rsi = current_rsi.iloc[0]
            current_rsi = float(current_rsi)
            
            return {
                'symbol': symbol,
                'rs_score': float(rs_score),
                'trend_score': float(trend_score),
                'trend_details': safe_trend_details,  # Use the safe version!
                'entry_signals': entry_signals,
                'current_price': current_price,
                'current_rsi': current_rsi
            }
        except Exception as e:
            print(f"ERROR converting values for {symbol}: {e}")
            return None
        
    except Exception as e:
        print(f"Error analyzing {symbol}: {e}")
        return None

def screen_stocks_advanced(symbols, start_date=START_DATE, end_date=None, min_rs_score=10):
    """
    Screen multiple stocks for high momentum pullback opportunities
    """
    if end_date is None:
        end_date = datetime.now().strftime('%Y-%m-%d')
    
    results = []
    
    for symbol in symbols:
        print(f"Analyzing {symbol}...")
        result = find_high_momentum_entries(symbol, start_date, end_date, min_rs_score)
        if result:
            results.append(result)
    
    # Sort by RS score descending
    results.sort(key=lambda x: x['rs_score'], reverse=True)
    
    return results

def print_analysis_results(results):
    """Print formatted analysis results"""
    if not results:
        print("No stocks found matching criteria.")
        return
    
    print(f"\n{'='*80}")
    print(f"HIGH MOMENTUM PULLBACK OPPORTUNITIES ({len(results)} stocks found)")
    print(f"{'='*80}")
    
    for i, result in enumerate(results, 1):
        print(f"\n{i}. {result['symbol']}")
        print(f"   RS Score: {result['rs_score']:.1f}")
        print(f"   Trend Score: {result['trend_score']:.1f}")
        print(f"   Current Price: ${result['current_price']:.2f}")
        print(f"   Current RSI: {result['current_rsi']:.1f}")
        
        trend = result['trend_details']
        print(f"   8EMA Adherence: {trend['ema_8_adherence']:.1f}%")
        print(f"   21EMA Adherence: {trend['ema_21_adherence']:.1f}%")
        print(f"   Higher Highs: {trend['higher_highs']}")
        print(f"   Higher Lows: {trend['higher_lows']}")
        
        print(f"   Entry Signals ({len(result['entry_signals'])}):")
        for signal in result['entry_signals'][-3:]:  # Show last 3 signals
            # Handle date whether it's a string or Timestamp
            date_str = signal['date'] if isinstance(signal['date'], str) else signal['date'].strftime('%Y-%m-%d')
            print(f"     {date_str}: {', '.join(signal['signals'])}")
            print(f"       Price: ${signal['close']:.2f}, RSI: {signal['rsi']:.1f}")

# Example usage
if __name__ == "__main__":
    # Test with some large cap stocks
    test_symbols = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'TSLA', 'NVDA', 'META', 'NFLX']
    
    print("Screening for high momentum pullback opportunities...")
    results = screen_stocks_advanced(test_symbols, min_rs_score=5)
    print_analysis_results(results)