"""
Stock screening module focused on CANSLIM evaluation.

This module provides functionality to screen stocks based on CANSLIM criteria,
filtering for stocks with strong fundamentals, technical strength, and market leadership.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd

from core.canslim import evaluate_canslim, evaluate_market_direction, MarketTrend
from core.momentum_analysis import calculate_rs_scores_for_tickers
from config.settings import MIN_RS_SCORE, MIN_CANSLIM_SCORE


def evaluate_stock_canslim(
    symbol: str,
    min_rs_score: float,
    min_canslim_score: float,
    market_trend: MarketTrend,
    rs_scores_df: pd.DataFrame,
    debug: bool = False
) -> Optional[Dict[str, object]]:
    """
    Evaluate a single stock against CANSLIM criteria.

    Args:
        symbol: Stock ticker symbol
        min_rs_score: Minimum RS score threshold
        min_canslim_score: Minimum CANSLIM composite score threshold
        market_trend: Pre-calculated market trend
        rs_scores_df: DataFrame with pre-calculated RS scores
        debug: Enable verbose output

    Returns:
        Dict with CANSLIM evaluation results, or None if stock doesn't meet criteria
    """
    if debug:
        print("\n" + "-" * 60)
        print(f"[DEBUG] Evaluating {symbol}")

    canslim_view = evaluate_canslim(symbol, rs_scores_df=rs_scores_df, market_trend=market_trend)
    if not canslim_view:
        if debug:
            print("[DEBUG] CANSLIM evaluation unavailable.")
        return None

    rs_score = float(canslim_view["rs_score"])
    if debug:
        print(
            f"[DEBUG] CANSLIM RS Score: {rs_score:.1f} | "
            f"Minimum Required: {min_rs_score:.1f}"
        )
    if rs_score < min_rs_score:
        if debug:
            print("[DEBUG] Fails RS score threshold.")
        return None

    total_score = float(canslim_view["total_score"])
    if debug:
        print(
            f"[DEBUG] CANSLIM Total Score: {total_score:.1f} | "
            f"Minimum Required: {min_canslim_score:.1f}"
        )
    if total_score < min_canslim_score:
        if debug:
            print("[DEBUG] Fails CANSLIM composite threshold.")
        return None

    if debug:
        print(f"[DEBUG] ✓ {symbol} meets all CANSLIM criteria!")

    return canslim_view


def screen_stocks_canslim(
    symbols: Iterable[str],
    start_date: str,
    end_date: Optional[str] = None,
    min_rs_score: float = MIN_RS_SCORE,
    min_canslim_score: float = MIN_CANSLIM_SCORE,
    debug: bool = False,
) -> Tuple[List[Dict[str, object]], MarketTrend]:
    """
    Screen multiple stocks for CANSLIM characteristics.

    Args:
        symbols: List of stock ticker symbols to screen
        start_date: Start date for analysis (unused but kept for compatibility)
        end_date: End date for analysis (unused but kept for compatibility)
        min_rs_score: Minimum relative strength score threshold
        min_canslim_score: Minimum composite CANSLIM score threshold
        debug: Enable verbose output

    Returns:
        Tuple of (results_list, market_trend) where results_list contains
        CANSLIM evaluations for stocks meeting criteria
    """
    market_trend = evaluate_market_direction()
    results: List[Dict[str, object]] = []

    # Calculate RS scores for all symbols at once
    rs_scores_df = calculate_rs_scores_for_tickers(list(symbols))

    for symbol in symbols:
        try:
            evaluation = evaluate_stock_canslim(
                symbol=symbol,
                min_rs_score=min_rs_score,
                min_canslim_score=min_canslim_score,
                market_trend=market_trend,
                rs_scores_df=rs_scores_df,
                debug=debug
            )
        except Exception as exc:
            print(f"Error analyzing {symbol}: {exc}")
            evaluation = None

        if evaluation:
            results.append(evaluation)

    # Sort by CANSLIM total score (highest first)
    results.sort(key=lambda x: x["total_score"], reverse=True)
    return results, market_trend


def print_analysis_results(results: List[Dict[str, object]], market_trend: Optional[MarketTrend] = None) -> None:
    """
    Print CANSLIM analysis results in a formatted table.

    Args:
        results: List of CANSLIM evaluation results
        market_trend: Market trend information
    """
    if not results:
        print("No stocks found matching criteria.")
        return

    print("\n" + "=" * 80)
    print(f"CANSLIM STOCK SCREENING RESULTS ({len(results)} stocks found)")
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
        print(f"   RS Score: {result['rs_score']:.1f} | CANSLIM Score: {result['total_score']:.1f}")

        print(f"   Component Breakdown:")
        for key in "C A N S L I M".split():
            score_pct = result["scores"].get(key, 0.0) * 100
            label = component_labels[key]
            print(f"     {key} - {label}: {score_pct:.0f}%")

        metrics = result["metrics"]
        print(
            "   Fundamentals: "
            f"Quarterly EPS Growth {_fmt(metrics['current_growth'])} | "
            f"Annual EPS Growth {_fmt(metrics['annual_growth'])} | "
            f"Revenue Growth {_fmt(metrics['revenue_growth'])}"
        )
        print(
            "   Technicals: "
            f"Avg Volume (50d) {_fmt(metrics['avg_volume_50'], 0)} | "
            f"Turnover Ratio {_fmt(metrics['turnover_ratio'])} | "
            f"52w High Proximity {_fmt(metrics['proximity_to_high'])}"
        )
