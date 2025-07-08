from datetime import datetime

def screen_stocks_advanced(symbols, start_date, end_date=None, min_rs_score=10):
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