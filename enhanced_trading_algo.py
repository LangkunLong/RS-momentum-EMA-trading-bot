import pandas as pd
import numpy as np
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator
from datetime import datetime, timedelta
from core.trend_analysis import analyze_trend_strength
from core.indicators import calculate_indicators
from core.pullback_entries import identify_pullback_entries
from core.stock_screening import screen_stocks_advanced, print_analysis_results
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

if __name__ == "__main__":
    # Test with some large cap stocks
    test_symbols = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'TSLA', 'NVDA', 'META', 'NFLX']
    
    print("Screening for high momentum pullback opportunities...")
    results = screen_stocks_advanced(test_symbols, min_rs_score=5)
    print_analysis_results(results)