from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd
import yfinance as yf

from core.canslim import MarketTrend, evaluate_canslim, evaluate_market_direction
from core.yahoo_finance_helper import extract_float_series, normalize_price_dataframe
from core.indicators import calculate_indicators
from core.pullback_entries import identify_pullback_entries
from core.trend_analysis import analyze_trend_strength
from core.momentum_analysis import calculate_rs_scores_for_tickers
from config.settings import MIN_RS_SCORE, MIN_CANSLIM_SCORE

# Download historical pricing data with a safety buffer for indicators.
def _fetch_price_history(symbol: str, start_date: datetime, end_date: Optional[str]) -> pd.DataFrame:

    extended_start = start_date - timedelta(days=120)
    data = yf.download(
        symbol,
        start=extended_start,
        end=end_date,
        progress=False,
        auto_adjust=False,
    )
    return normalize_price_dataframe(data)

# helper function to identify actionable pullback entries for each tiker
# returns None if a stock fails any of the CAN SLIM criteria, does not maintain a strong trend, or lacks recent pullback signals.
def find_high_momentum_entries(
    symbol: str,
    start_date: str,
    end_date: Optional[str],
    min_rs_score: float,
    min_canslim_score: float,
    market_trend: MarketTrend,
    rs_scores_df: pd.DataFrame,
    debug: bool = False
) -> Optional[Dict[str, object]]:
    
    if debug:
        print("\n" + "-" * 60)
        print(f"[DEBUG] Evaluating {symbol}")

    canslim_view = evaluate_canslim(symbol, rs_scores_df=rs_scores_df, market_trend=market_trend)
    if not canslim_view:
        if debug:
            print("[DEBUG] CAN SLIM evaluation unavailable.")
        return None

    rs_score = float(canslim_view["rs_score"])
    if debug:
        print(
            f"[DEBUG] CAN SLIM RS Score: {rs_score:.1f} | "
            f"Minimum Required: {min_rs_score:.1f}"
        )
    if rs_score < min_rs_score:
        if debug:
            print("[DEBUG] Fails RS score threshold.")
        return None

    total_score = float(canslim_view["total_score"])
    if debug:
        print(
            f"[DEBUG] CAN SLIM Total Score: {total_score:.1f} | "
            f"Minimum Required: {min_canslim_score:.1f}"
        )
    if total_score < min_canslim_score:
        if debug:
            print("[DEBUG] Fails CAN SLIM composite threshold.")
        return None

    start_dt = pd.to_datetime(start_date)
    price_history = _fetch_price_history(symbol, start_dt, end_date)

    if price_history.empty or len(price_history) < 120:
        return None

    price_history = calculate_indicators(price_history)
    is_strong_trend, trend_score, trend_details = analyze_trend_strength(price_history)
    if debug:
        print(
            f"[DEBUG] Trend Score: {trend_score:.1f} | "
            f"Strong Trend: {is_strong_trend}"
        )
        if trend_details:
            print(
                "[DEBUG] Trend Details -> "
                f"8EMA: {trend_details.get('ema_8_adherence', 0):.1f}% | "
                f"21EMA: {trend_details.get('ema_21_adherence', 0):.1f}% | "
                f"Higher Highs: {trend_details.get('higher_highs', False)} | "
                f"Higher Lows: {trend_details.get('higher_lows', False)}"
            )

    if not is_strong_trend:
        if debug:
            print("[DEBUG] Trend strength requirements not met.")
        return None

    analysis_df = price_history[price_history.index >= start_dt]
    entry_signals = identify_pullback_entries(analysis_df)
    
    if debug:
        if entry_signals:
            latest_signal = entry_signals[-1]
            print(
                "[DEBUG] Pullback signals found: "
                f"{', '.join(latest_signal['signals'])} on {latest_signal['date']}"
            )
        else:
            print("[DEBUG] No qualifying pullback signals in lookback window.")

    if not entry_signals:
        return None

    close_series = extract_float_series(price_history, "Close")
    current_price = float(close_series.iloc[-1])

    current_rsi = (
        float(extract_float_series(price_history, "RSI").iloc[-1])
        if "RSI" in price_history
        else float("nan")
    )

    safe_trend_details = {
        "ema_8_adherence": float(trend_details.get("ema_8_adherence", 0.0)),
        "ema_21_adherence": float(trend_details.get("ema_21_adherence", 0.0)),
        "higher_highs": bool(trend_details.get("higher_highs", False)),
        "higher_lows": bool(trend_details.get("higher_lows", False)),
        "strong_ema_adherence": bool(trend_details.get("strong_ema_adherence", False)),
    }

    return {
        "symbol": symbol,
        "rs_score": rs_score,
        "trend_score": float(trend_score),
        "trend_details": safe_trend_details,
        "entry_signals": entry_signals,
        "current_price": current_price,
        "current_rsi": current_rsi,
        "canslim": canslim_view,
    }

# screen stocks for CAN SLIM characteristics and pullback setups
def screen_stocks_canslim(
    symbols: Iterable[str],
    start_date: str,
    end_date: Optional[str] = None,
    min_rs_score: float = MIN_RS_SCORE,
    min_canslim_score: float = MIN_CANSLIM_SCORE,
    debug: bool = False,
) -> Tuple[List[Dict[str, object]], MarketTrend]:

    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")
    market_trend = evaluate_market_direction()
    results: List[Dict[str, object]] = []

    rs_scores_df = calculate_rs_scores_for_tickers(list(symbols))

    for symbol in symbols:
        try:
            entry = find_high_momentum_entries(
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                min_rs_score=min_rs_score,
                min_canslim_score=min_canslim_score,
                market_trend=market_trend,
                rs_scores_df=rs_scores_df,
                debug=debug
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            print(f"Error analysing {symbol}: {exc}")
            entry = None

        if entry:
            results.append(entry)

    results.sort(key=lambda x: x["canslim"]["total_score"], reverse=True)
    return results, market_trend

# outputs CANSLIM Scores
def print_analysis_results(results: List[Dict[str, object]], market_trend: Optional[MarketTrend] = None) -> None:
    if not results:
        print("No stocks found matching criteria.")
        return
    print("\n" + "=" * 80)
    print(f"HIGH MOMENTUM CAN SLIM OPPORTUNITIES ({len(results)} stocks found)")
    print("=" * 80)

    if market_trend is not None:
        direction = "Bullish" if market_trend.is_bullish else "Cautious"
        print(
            f"Market Direction ({market_trend.symbol}): {direction} | Score: {market_trend.score * 100:.0f}%"
        )
        if market_trend.latest_close is not None:
            print(
                f"Latest Close: ${market_trend.latest_close:.2f} | "
                f"21 EMA: ${market_trend.indicators['ema_21']:.2f} | "
                f"50 EMA: ${market_trend.indicators['ema_50']:.2f} | "
                f"200 EMA: ${market_trend.indicators['ema_200']:.2f}"
            )

    component_labels = {
        "C": "Current earnings",
        "A": "Annual earnings",
        "N": "New product/price leadership",
        "S": "Supply & demand",
        "L": "Leader vs laggard",
        "I": "Institutional sponsorship",
        "M": "Market direction",
    }
    
    def _fmt(value: Optional[float], precision: int = 2) -> str:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return "n/a"
        return f"{value:.{precision}f}"
    
    for idx, result in enumerate(results, start=1):
        print(f"\n{idx}. {result['symbol']}")
        print(f"   Price: ${result['current_price']:.2f} | RSI: {result['current_rsi']:.1f}")
        print(f"   RS Score: {result['rs_score']:.1f} | Trend Score: {result['trend_score']:.1f}")

        trend = result["trend_details"]
        print(
            "   Trend Details: "
            f"8EMA {trend['ema_8_adherence']:.1f}% | "
            f"21EMA {trend['ema_21_adherence']:.1f}% | "
            f"Higher Highs {trend['higher_highs']} | Higher Lows {trend['higher_lows']}"
        )

        canslim = result["canslim"]
        print(f"   CAN SLIM Composite Score: {canslim['total_score']:.1f}")

        for key in "C A N S L I M".split():
            score_pct = canslim["scores"].get(key, 0.0) * 100
            label = component_labels[key]
            print(f"     {key} - {label}: {score_pct:.0f}%")

        metrics = canslim["metrics"]
        print(
            "   Fundamentals: "
            f"Quarterly EPS Growth {_fmt(metrics['current_growth'])} | "
            f"Annual EPS Growth {_fmt(metrics['annual_growth'])} | "
            f"Revenue Growth {_fmt(metrics['revenue_growth'])}"
        )
        print(
            "   Liquidity: "
            f"Avg Volume (50d) {_fmt(metrics['avg_volume_50'], 0)} | "
            f"Turnover Ratio {_fmt(metrics['turnover_ratio'])} | "
            f"52w High Proximity {_fmt(metrics['proximity_to_high'])}"
        )

        print("   Recent Entry Signals:")
        for signal in result["entry_signals"][-3:]:
            date_val = signal["date"]
            if not isinstance(date_val, str):
                date_val = date_val.strftime("%Y-%m-%d")
            print(
                f"     {date_val}: {', '.join(signal['signals'])} | "
                f"Close ${signal['close']:.2f} | RSI {signal['rsi']:.1f}"
            )
