import pandas as pd
import numpy as np
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator
from datetime import datetime, timedelta
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
            return 0
        
        # Calculate performance over period
        stock_start = stock_data['Close'].iloc[-period_days]
        stock_current = stock_data['Close'].iloc[-1]
        stock_performance = (stock_current / stock_start - 1) * 100
        
        benchmark_start = benchmark_data['Close'].iloc[-period_days]
        benchmark_current = benchmark_data['Close'].iloc[-1]
        benchmark_performance = (benchmark_current / benchmark_start - 1) * 100
        
        # RS Score (relative strength)
        rs_score = stock_performance - benchmark_performance
        return rs_score
        
    except Exception as e:
        print(f"Error calculating RS for {symbol}: {e}")
        return 0

def calculate_indicators(df):
    """Calculate technical indicators"""
    if len(df) < 21:
        return df
    
    # Calculate EMAs
    df['8_EMA'] = EMAIndicator(df['Close'], window=8).ema_indicator()
    df['21_EMA'] = EMAIndicator(df['Close'], window=21).ema_indicator()
    
    # Calculate RSI
    df['RSI'] = RSIIndicator(df['Close'], window=14).rsi()
    
    # Calculate price vs EMA relationships
    df['Above_8EMA'] = df['Close'] > df['8_EMA']
    df['Above_21EMA'] = df['Close'] > df['21_EMA']
    df['Distance_8EMA'] = ((df['Close'] - df['8_EMA']) / df['8_EMA']) * 100
    df['Distance_21EMA'] = ((df['Close'] - df['21_EMA']) / df['21_EMA']) * 100
    
    return df

def analyze_trend_strength(df, min_days=60):
    """
    Analyze if stock is in a steady uptrend with higher highs and higher lows
    Must hold 8EMA or 21EMA for at least 2 months (60 trading days)
    """
    if len(df) < min_days:
        return False, 0, {}
    
    recent_data = df.tail(min_days).copy()
    
    # Check EMA adherence
    ema_8_adherence = (recent_data['Above_8EMA'].sum() / len(recent_data)) * 100
    ema_21_adherence = (recent_data['Above_21EMA'].sum() / len(recent_data)) * 100
    
    # Strong trend requires holding either 8EMA (>70%) or 21EMA (>80%)
    strong_ema_adherence = ema_8_adherence > 70 or ema_21_adherence > 80
    
    # Check for higher highs and higher lows pattern
    # Divide period into segments and check progression
    segment_size = min_days // 4
    segments = []
    
    for i in range(4):
        start_idx = i * segment_size
        end_idx = (i + 1) * segment_size if i < 3 else len(recent_data)
        segment = recent_data.iloc[start_idx:end_idx]
        segments.append({
            'high': segment['High'].max(),
            'low': segment['Low'].min(),
            'close_avg': segment['Close'].mean()
        })
    
    # Check for progression in highs and lows
    # Check for progression in highs and lows
    higher_highs = all(float(segments[i]['high']) <= float(segments[i+1]['high']) for i in range(2))
    higher_lows = all(float(segments[i]['low']) <= float(segments[i+1]['low']) for i in range(2))
    
    trend_strength = {
        'ema_8_adherence': ema_8_adherence,
        'ema_21_adherence': ema_21_adherence,
        'higher_highs': higher_highs,
        'higher_lows': higher_lows,
        'strong_ema_adherence': strong_ema_adherence
    }
    
    is_strong_trend = strong_ema_adherence and (higher_highs or higher_lows)
    trend_score = (ema_8_adherence + ema_21_adherence) / 2
    
    return is_strong_trend, trend_score, trend_strength

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
        
        # Entry conditions
        entry_signals = []
        
        # Signal 1: Pullback to 8EMA retest
        if (abs(current_row['Distance_8EMA']) < 2.0 and  # Within 2% of 8EMA
            current_row['Above_8EMA'] and
            previous_rows['Above_8EMA'].mean() > 0.7):  # Was mostly above 8EMA recently
            entry_signals.append('8EMA_Retest')
        
        # Signal 2: Pullback to 21EMA retest
        if (abs(current_row['Distance_21EMA']) < 3.0 and  # Within 3% of 21EMA
            current_row['Above_21EMA'] and
            previous_rows['Above_21EMA'].mean() > 0.8):  # Was mostly above 21EMA recently
            entry_signals.append('21EMA_Retest')
        
        # Signal 3: Broke 8EMA but held 21EMA and reclaimed 8EMA
        broke_8ema_recently = any(not bool(above) for above in previous_rows['Above_8EMA'].tail(5))
        held_21ema = bool(current_row['Above_21EMA']) and bool(previous_rows['Above_21EMA'].tail(5).all())
        reclaimed_8ema = bool(current_row['Above_8EMA'])
        
        if broke_8ema_recently and held_21ema and reclaimed_8ema:
            entry_signals.append('8EMA_Reclaim')
        
        if entry_signals:
            signals.append({
                'date': current_row.name,
                'close': current_row['Close'],
                'signals': entry_signals,
                'rsi': current_row['RSI'],
                'distance_8ema': current_row['Distance_8EMA'],
                'distance_21ema': current_row['Distance_21EMA']
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
        if rs_score < min_rs_score:
            return None
        
        # Fetch historical data with extra buffer for indicators
        extended_start = pd.to_datetime(start_date) - timedelta(days=100)
        df = yf.download(symbol, start=extended_start, end=end_date, progress=False)
        
        if df.empty or len(df) < 100:
            return None
        
        # Calculate indicators
        df = calculate_indicators(df)
        
        # Analyze trend strength
        is_strong_trend, trend_score, trend_details = analyze_trend_strength(df)
        
        if not is_strong_trend:
            return None
        
        # Identify pullback entries in the specified date range
        analysis_df = df[df.index >= start_date]
        entry_signals = identify_pullback_entries(analysis_df)
        
        if not entry_signals:
            return None
        
        return {
            'symbol': symbol,
            'rs_score': rs_score,
            'trend_score': trend_score,
            'trend_details': trend_details,
            'entry_signals': entry_signals,
            'current_price': df['Close'].iloc[-1],
            'current_rsi': df['RSI'].iloc[-1]
        }
        
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
            print(f"     {signal['date'].strftime('%Y-%m-%d')}: {', '.join(signal['signals'])}")
            print(f"       Price: ${signal['close']:.2f}, RSI: {signal['rsi']:.1f}")

# Example usage
if __name__ == "__main__":
    # Test with some large cap stocks
    test_symbols = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'TSLA', 'NVDA', 'META', 'NFLX']
    
    print("Screening for high momentum pullback opportunities...")
    results = screen_stocks_advanced(test_symbols, min_rs_score=5)
    print_analysis_results(results)