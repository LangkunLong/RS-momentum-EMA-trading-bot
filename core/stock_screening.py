"""Stock screening module focused on CANSLIM evaluation.

This module provides functionality to screen stocks based on CANSLIM criteria,
filtering for stocks with strong fundamentals, technical strength, and market leadership.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd

from config.settings import (
    MAX_WORKERS,
    MIN_CANSLIM_SCORE,
    MIN_RS_SCORE,
    REQUIRE_BULLISH_MARKET_FOR_BUYS,
    WATCHLIST_MIN_CANSLIM_SCORE,
)
from core.canslim import MarketTrend, evaluate_canslim, evaluate_market_direction
from core.momentum_analysis import calculate_rs_scores_for_tickers


def _classify_canslim_candidate(
    canslim_view: Dict[str, object],
    min_rs_score: float,
    min_canslim_score: float,
    watchlist_min_score: float = WATCHLIST_MIN_CANSLIM_SCORE,
    require_bullish_market: bool = REQUIRE_BULLISH_MARKET_FOR_BUYS,
    strict_breakout: bool = False,
) -> tuple[str, List[str]]:
    """Classify a scored stock into actionable buy, watchlist, or rejected."""
    notes: List[str] = []

    rs_score = float(canslim_view.get("rs_score", 0.0))
    total_score = float(canslim_view.get("total_score", 0.0))
    market = canslim_view.get("market_trend")
    metrics = canslim_view.get("metrics", {})

    market_is_bullish = bool(getattr(market, "is_bullish", False))
    has_fundamentals = bool(metrics.get("has_fundamentals", False))
    is_breakout = bool(canslim_view.get("is_breakout", False))
    has_volume_surge = bool(canslim_view.get("has_volume_surge", False))

    if rs_score < min_rs_score:
        return "rejected", ["below_rs_threshold"]

    bullish_gate_ok = market_is_bullish if require_bullish_market else True
    breakout_gate_ok = True
    if strict_breakout:
        breakout_gate_ok = is_breakout and has_volume_surge

    if total_score >= min_canslim_score and bullish_gate_ok and breakout_gate_ok:
        return "actionable_buy", []

    if total_score < watchlist_min_score:
        return "rejected", ["below_watchlist_score"]

    if total_score < min_canslim_score:
        notes.append("below_buy_score")
    if require_bullish_market and not market_is_bullish:
        notes.append("market_not_bullish")
    if not has_fundamentals:
        notes.append("missing_fundamentals")
    if strict_breakout and not is_breakout:
        notes.append("not_in_breakout")
    if strict_breakout and not has_volume_surge:
        notes.append("no_volume_surge")
    if not notes:
        notes.append("monitor_setup")

    return "watchlist_candidate", notes


def evaluate_stock_canslim(
    symbol: str,
    min_rs_score: float,
    min_canslim_score: float,
    market_trend: MarketTrend,
    rs_scores_df: pd.DataFrame,
    debug: bool = False,
    watchlist_min_score: float = WATCHLIST_MIN_CANSLIM_SCORE,
    require_bullish_market: bool = REQUIRE_BULLISH_MARKET_FOR_BUYS,
    strict_breakout: bool = False,
) -> Optional[Dict[str, object]]:
    """Evaluate a single stock against CANSLIM criteria.

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
    logs: List[str] = []

    def _debug(msg: str) -> None:
        if debug:
            logs.append(msg)

    def _fmt_opt(value: object, precision: int = 2, pct: bool = False) -> str:
        if value is None:
            return "n/a"
        if isinstance(value, float) and pd.isna(value):
            return "n/a"
        if pct:
            return f"{float(value) * 100:.{precision}f}%"
        return f"{float(value):.{precision}f}"

    def _fmt_component_map(values: Dict[str, float], pct: bool = False) -> str:
        ordered_keys = [key for key in "C A N S L I M".split() if key in values]
        formatted = []
        for key in ordered_keys:
            value = values.get(key, 0.0)
            rendered = f"{value * 100:.1f}%" if pct else f"{value:.3f}"
            formatted.append(f"{key}={rendered}")
        return " | ".join(formatted)

    def _flush_logs() -> None:
        if debug and logs:
            print("\n".join(logs))

    _debug("\n" + "-" * 60)
    _debug(f"[DEBUG] Evaluating {symbol}")

    canslim_view = evaluate_canslim(symbol, rs_scores_df=rs_scores_df, market_trend=market_trend)
    if not canslim_view:
        _debug("[DEBUG] CANSLIM evaluation unavailable.")
        _flush_logs()
        return None

    scores = canslim_view.get("scores", {})
    active_weights = canslim_view.get("active_weights", {})
    weighted_contributions = canslim_view.get("weighted_contributions", {})
    metrics = canslim_view.get("metrics", {})
    availability = canslim_view.get("data_availability", {})
    s_metrics = metrics.get("s_metrics", {})

    _debug(f"[DEBUG] Raw component scores (0-1): {_fmt_component_map(scores)}")
    _debug(f"[DEBUG] Active weights: {_fmt_component_map(active_weights, pct=True)}")
    _debug(f"[DEBUG] Weighted contributions: {_fmt_component_map(weighted_contributions, pct=False)}")
    _debug(
        "[DEBUG] Inputs: "
        f"C_growth={_fmt_opt(metrics.get('current_growth'), pct=True)} | "
        f"A_growth={_fmt_opt(metrics.get('annual_growth'), pct=True)} | "
        f"N_revenue={_fmt_opt(metrics.get('revenue_growth'), pct=True)} | "
        f"ROE={_fmt_opt(metrics.get('roe'), pct=True)} | "
        f"52w_proximity={_fmt_opt(metrics.get('proximity_to_high'))} | "
        f"Vol_ratio={_fmt_opt(s_metrics.get('volume_ratio'))} | "
        f"UpDownVol={_fmt_opt(s_metrics.get('up_down_volume_ratio'))}"
    )
    _debug(
        "[DEBUG] Data availability: "
        f"C={availability.get('C')} | "
        f"A={availability.get('A')} | "
        f"N_revenue={availability.get('N_revenue')} | "
        f"I_level={availability.get('I_level')} | "
        f"I_trend={availability.get('I_trend')}"
    )
    market = canslim_view.get("market_trend")
    if market is not None:
        latest_close = "n/a" if market.latest_close is None else f"{market.latest_close:.2f}"
        _debug(
            "[DEBUG] Market internals: "
            f"score={market.score:.3f} | bullish={market.is_bullish} | "
            f"dist_days={getattr(market, 'distribution_days', 'n/a')} | "
            f"ftd={getattr(market, 'follow_through', 'n/a')} | "
            f"close={latest_close} | "
            f"ema21={market.indicators.get('ema_21', float('nan')):.2f} | "
            f"ema50={market.indicators.get('ema_50', float('nan')):.2f} | "
            f"ema200={market.indicators.get('ema_200', float('nan')):.2f}"
        )

    rs_score = float(canslim_view["rs_score"])
    _debug(f"[DEBUG] CANSLIM RS Score: {rs_score:.1f} | Minimum Required: {min_rs_score:.1f}")
    if rs_score < min_rs_score:
        _debug("[DEBUG] Fails RS score threshold.")
        _flush_logs()
        return None

    total_score = float(canslim_view["total_score"])
    _debug(f"[DEBUG] CANSLIM Total Score: {total_score:.1f} | Minimum Required: {min_canslim_score:.1f}")
    category, notes = _classify_canslim_candidate(
        canslim_view,
        min_rs_score=min_rs_score,
        min_canslim_score=min_canslim_score,
        watchlist_min_score=watchlist_min_score,
        require_bullish_market=require_bullish_market,
        strict_breakout=strict_breakout,
    )
    canslim_view["scanner_category"] = category
    canslim_view["scanner_notes"] = notes

    if category == "rejected":
        _debug(f"[DEBUG] Rejected by scanner: {', '.join(notes)}")
        _flush_logs()
        return None

    _debug(f"[DEBUG] {symbol} cleared the RS prefilter and was classified for scanner output.")

    if strict_breakout:
        if not market_trend.is_bullish:
            _debug("[DEBUG] Fails strict entry: Market is not in confirmed uptrend (is_bullish=False).")
            _flush_logs()
            return None
        if not canslim_view.get("is_breakout"):
            _debug("[DEBUG] Fails strict entry: Not breaking out near 52-week high.")
            _flush_logs()
            return None
        if not canslim_view.get("has_volume_surge"):
            _debug("[DEBUG] Fails strict entry: No volume surge detected.")
            _flush_logs()
            return None
        _debug(f"[DEBUG] ✓ {symbol} meets strict breakout criteria!")

    note_text = ", ".join(notes) if notes else "none"
    _debug(f"[DEBUG] Scanner category: {category} | Notes: {note_text}")

    _flush_logs()
    return canslim_view


def screen_stocks_canslim_detailed(
    symbols: Iterable[str],
    start_date: str,
    end_date: Optional[str] = None,
    min_rs_score: float = MIN_RS_SCORE,
    min_canslim_score: float = MIN_CANSLIM_SCORE,
    debug: bool = False,
    watchlist_min_score: float = WATCHLIST_MIN_CANSLIM_SCORE,
    require_bullish_market: bool = REQUIRE_BULLISH_MARKET_FOR_BUYS,
    strict_breakout: bool = False,
) -> Tuple[List[Dict[str, object]], List[Dict[str, object]], MarketTrend]:
    """Screen multiple stocks for CANSLIM characteristics.

    Args:
        symbols: List of stock ticker symbols to screen
        start_date: Start date for analysis (unused but kept for compatibility)
        end_date: End date for analysis (unused but kept for compatibility)
        min_rs_score: Minimum relative strength score threshold
        min_canslim_score: Minimum composite CANSLIM score threshold
        debug: Enable verbose output

    Returns:
        Tuple of (actionable_buys, watchlist_candidates, market_trend)

    """
    market_trend = evaluate_market_direction()
    results: List[Dict[str, object]] = []

    # Calculate RS scores for all symbols at once
    symbols_list = list(symbols)
    rs_scores_df = calculate_rs_scores_for_tickers(symbols_list)

    if debug and not rs_scores_df.empty:
        rs_series = rs_scores_df["RS_Score"].astype(float)
        print(
            "[DEBUG] RS universe stats: "
            f"count={len(rs_scores_df)} | "
            f"min={rs_series.min():.1f} | "
            f"median={rs_series.median():.1f} | "
            f"p80={rs_series.quantile(0.80):.1f} | "
            f"max={rs_series.max():.1f}"
        )

    # Pre-filter: discard symbols whose RS score is already below the threshold
    # to avoid wasting yfinance API calls on weak stocks
    filtered_symbols = []
    for symbol in symbols_list:
        try:
            match = rs_scores_df[rs_scores_df["Ticker"] == symbol]
            if not match.empty:
                rs_val = float(match.iloc[0]["RS_Score"])
            else:
                rs_val = 0
        except Exception:
            rs_val = 0

        if rs_val >= min_rs_score:
            filtered_symbols.append(symbol)
        elif debug:
            print(f"[DEBUG] Pre-filter: {symbol} RS={rs_val:.1f} < {min_rs_score}, skipped")

    if debug:
        print(f"[DEBUG] Pre-filter kept {len(filtered_symbols)}/{len(symbols_list)} symbols")

    # Evaluate remaining symbols in parallel
    def _evaluate(sym: str) -> Optional[Dict[str, object]]:
        try:
            return evaluate_stock_canslim(
                symbol=sym,
                min_rs_score=min_rs_score,
                min_canslim_score=min_canslim_score,
                market_trend=market_trend,
                rs_scores_df=rs_scores_df,
                debug=debug,
                watchlist_min_score=watchlist_min_score,
                require_bullish_market=require_bullish_market,
                strict_breakout=strict_breakout,
            )
        except Exception as exc:
            print(f"Error analyzing {sym}: {exc}")
            return None

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_evaluate, sym): sym for sym in filtered_symbols}
        for future in as_completed(futures):
            evaluation = future.result()
            if evaluation:
                results.append(evaluation)

    actionable_buys = [
        result for result in results if result.get("scanner_category") == "actionable_buy"
    ]
    watchlist_candidates = [
        result for result in results if result.get("scanner_category") == "watchlist_candidate"
    ]

    actionable_buys.sort(key=lambda x: x["total_score"], reverse=True)
    watchlist_candidates.sort(key=lambda x: x["total_score"], reverse=True)
    return actionable_buys, watchlist_candidates, market_trend


def screen_stocks_canslim(
    symbols: Iterable[str],
    start_date: str,
    end_date: Optional[str] = None,
    min_rs_score: float = MIN_RS_SCORE,
    min_canslim_score: float = MIN_CANSLIM_SCORE,
    debug: bool = False,
    strict_breakout: bool = False,
) -> Tuple[List[Dict[str, object]], MarketTrend]:
    """Backward-compatible wrapper that returns only actionable buys."""
    actionable_buys, _watchlist_candidates, market_trend = screen_stocks_canslim_detailed(
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
        min_rs_score=min_rs_score,
        min_canslim_score=min_canslim_score,
        debug=debug,
        strict_breakout=strict_breakout,
    )
    return actionable_buys, market_trend


def print_analysis_results(
    results: List[Dict[str, object]],
    market_trend: Optional[MarketTrend] = None,
    title: str = "CANSLIM STOCK SCREENING RESULTS",
) -> None:
    """Print CANSLIM analysis results in a formatted table.

    Args:
        results: List of CANSLIM evaluation results
        market_trend: Market trend information

    """
    if not results:
        print("No stocks found matching criteria.")
        return

    print("\n" + "=" * 80)
    print(f"{title} ({len(results)} stocks found)")
    print("=" * 80)

    if market_trend is not None:
        direction = "Bullish" if market_trend.is_bullish else "Cautious"
        score_pct = market_trend.score * 100
        print(f"Market Direction ({market_trend.symbol}): {direction} | Score: {score_pct:.0f}%")
        if hasattr(market_trend, "distribution_days"):
            dist_status = "WARNING" if market_trend.distribution_days >= 5 else "OK"
            ftd_status = "Yes" if market_trend.follow_through else "No"
            print(
                f"Distribution Days (25d): {market_trend.distribution_days} [{dist_status}] | "
                f"Follow-Through Day: {ftd_status}"
            )
        if market_trend.latest_close is not None:
            print(
                f"Latest Close: ${market_trend.latest_close:.2f} | "
                f"21 EMA: ${market_trend.indicators['ema_21']:.2f} | "
                f"50 EMA: ${market_trend.indicators['ema_50']:.2f} | "
                f"200 EMA: ${market_trend.indicators['ema_200']:.2f}"
            )

    component_labels = {
        "C": "Current earnings (YoY)",
        "A": "Annual earnings (multi-yr)",
        "N": "New highs / revenue",
        "S": "Supply & demand",
        "L": "Leader vs laggard",
        "I": "Institutional sponsorship",
        "M": "Market direction",
    }

    def _fmt(value: Optional[float], precision: int = 2) -> str:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return "n/a"
        return f"{value:.{precision}f}"

    def _fmt_pct(value: Optional[float]) -> str:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return "n/a"
        return f"{value * 100:.1f}%"

    for idx, result in enumerate(results, start=1):
        print(f"\n{idx}. {result['symbol']}")
        print(f"   RS Score: {result['rs_score']:.1f} | CANSLIM Score: {result['total_score']:.1f}")
        if result.get("scanner_category"):
            print(f"   Scanner Category: {str(result['scanner_category']).replace('_', ' ').title()}")
        notes = result.get("scanner_notes") or []
        if notes:
            print("   Notes: " + ", ".join(str(note) for note in notes))

        print("   Component Breakdown:")
        for key in "C A N S L I M".split():
            score_pct = result["scores"].get(key, 0.0) * 100
            label = component_labels[key]
            print(f"     {key} - {label}: {score_pct:.0f}%")

        metrics = result["metrics"]
        print(
            "   Fundamentals: "
            f"Quarterly EPS Growth {_fmt_pct(metrics['current_growth'])} | "
            f"Annual EPS Growth {_fmt_pct(metrics['annual_growth'])} | "
            f"Revenue Growth {_fmt_pct(metrics['revenue_growth'])} | "
            f"ROE {_fmt_pct(metrics.get('roe'))}"
        )

        s_metrics = metrics.get("s_metrics", {})
        market = result.get("market_trend")
        dist_info = ""
        if market and hasattr(market, "distribution_days"):
            dist_info = f" | Dist Days: {market.distribution_days}"
            if market.follow_through:
                dist_info += " | FTD: Yes"

        print(
            "   Technicals: "
            f"Avg Volume (50d) {_fmt(metrics['avg_volume_50'], 0)} | "
            f"52w Proximity {_fmt(metrics['proximity_to_high'])} | "
            f"Up/Down Vol {_fmt(s_metrics.get('up_down_volume_ratio'))}"
            f"{dist_info}"
        )
