"""Stock screening module focused on CANSLIM evaluation.

This module provides functionality to screen stocks based on CANSLIM criteria,
filtering for stocks with strong fundamentals, technical strength, and market leadership.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import math
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd

from config.settings import (
    MAX_WORKERS,
    MIN_CANSLIM_SCORE,
    MIN_RS_SCORE,
    REQUIRE_FUNDAMENTALS_FOR_BUYS,
    REQUIRE_BULLISH_MARKET_FOR_BUYS,
    STRICT_BREAKOUT_FOR_BUYS,
    WATCHLIST_MIN_CANSLIM_SCORE,
)
from core.canslim import MarketTrend, evaluate_canslim, evaluate_market_direction
from core.canslim.entry_contract import (
    MIN_COMPOSITE_SCORE as CANONICAL_MIN_COMPOSITE_SCORE,
    MIN_RS_SCORE as CANONICAL_MIN_RS_SCORE,
    CanslimEntryDecision,
)
from core.momentum_analysis import calculate_rs_scores_for_tickers


def _tightened_floor(canonical_floor: float, caller_floor: object) -> float:
    """Return a finite caller floor only when it tightens the canonical floor."""
    try:
        requested = float(caller_floor)
    except (TypeError, ValueError, OverflowError):
        return canonical_floor
    if not math.isfinite(requested):
        return canonical_floor
    return max(canonical_floor, requested)


def _classify_canslim_candidate(
    canslim_view: Dict[str, object],
    min_rs_score: float,
    min_canslim_score: float,
    watchlist_min_score: float = WATCHLIST_MIN_CANSLIM_SCORE,
    require_bullish_market: bool = REQUIRE_BULLISH_MARKET_FOR_BUYS,
    require_fundamentals: bool = REQUIRE_FUNDAMENTALS_FOR_BUYS,
    strict_breakout: bool = STRICT_BREAKOUT_FOR_BUYS,
) -> tuple[str, List[str]]:
    """Classify a canonical entry decision without duplicating its gates.

    Caller thresholds may tighten but cannot loosen the fixed shared contract.
    The legacy fundamental and breakout strictness arguments remain accepted
    for API compatibility but are intentionally inert.
    """
    notes: List[str] = []

    entry_decision = canslim_view.get("entry_decision")
    if isinstance(entry_decision, CanslimEntryDecision):
        rs_score = float(entry_decision.rs_score or 0.0)
        entry_composite_score = float(entry_decision.composite_score or 0.0)
    else:
        rs_score = float(canslim_view.get("rs_score", 0.0))
        entry_composite_score = float(canslim_view.get("entry_composite_score", 0.0))
    effective_rs_floor = _tightened_floor(CANONICAL_MIN_RS_SCORE, min_rs_score)
    effective_composite_floor = _tightened_floor(
        CANONICAL_MIN_COMPOSITE_SCORE, min_canslim_score
    )
    market = canslim_view.get("market_trend")
    metrics = canslim_view.get("metrics", {})
    market_is_bullish = bool(getattr(market, "is_bullish", False))

    if bool(metrics.get("fmp_quota_deferred", False)):
        return "quota_deferred", ["quota_deferred"]

    market_permission = market_is_bullish if require_bullish_market else True
    contract_eligible = isinstance(entry_decision, CanslimEntryDecision) and entry_decision.eligible
    caller_thresholds_met = (
        rs_score >= effective_rs_floor and entry_composite_score >= effective_composite_floor
    )
    if contract_eligible and caller_thresholds_met and market_permission:
        return "actionable_buy", []

    if entry_composite_score < watchlist_min_score:
        return "rejected", ["below_watchlist_score"]

    if isinstance(entry_decision, CanslimEntryDecision):
        notes.extend(entry_decision.blocking_reasons)
    else:
        notes.append("entry_contract_unavailable")
    if rs_score < effective_rs_floor:
        notes.append("below_rs_threshold")
    if entry_composite_score < effective_composite_floor:
        notes.append("below_buy_score")
    if require_bullish_market and not market_is_bullish:
        notes.append("market_not_bullish")
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
    require_fundamentals: bool = REQUIRE_FUNDAMENTALS_FOR_BUYS,
    strict_breakout: bool = STRICT_BREAKOUT_FOR_BUYS,
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
    effective_rs_floor = _tightened_floor(CANONICAL_MIN_RS_SCORE, min_rs_score)
    effective_composite_floor = _tightened_floor(
        CANONICAL_MIN_COMPOSITE_SCORE, min_canslim_score
    )
    _debug(f"[DEBUG] CANSLIM RS Score: {rs_score:.1f} | Effective Minimum: {effective_rs_floor:.1f}")
    total_score = float(canslim_view["total_score"])
    entry_score = float(canslim_view.get("entry_composite_score", 0.0))
    _debug(
        f"[DEBUG] Entry Composite (non-M): {entry_score:.1f} | "
        f"Effective Minimum: {effective_composite_floor:.1f} | "
        f"Legacy M-inclusive Total: {total_score:.1f}"
    )
    category, notes = _classify_canslim_candidate(
        canslim_view,
        min_rs_score=min_rs_score,
        min_canslim_score=min_canslim_score,
        watchlist_min_score=watchlist_min_score,
        require_bullish_market=require_bullish_market,
        require_fundamentals=require_fundamentals,
        strict_breakout=strict_breakout,
    )
    canslim_view["scanner_category"] = category
    canslim_view["scanner_notes"] = notes

    if category == "quota_deferred":
        _debug(f"[DEBUG] {symbol} deferred because the local FMP request budget was reached.")
        _flush_logs()
        return canslim_view

    if category == "rejected":
        _debug(f"[DEBUG] Rejected by scanner: {', '.join(notes)}")
        _flush_logs()
        return None

    _debug(f"[DEBUG] {symbol} cleared the RS prefilter and was classified for scanner output.")

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
    require_fundamentals: bool = REQUIRE_FUNDAMENTALS_FOR_BUYS,
    strict_breakout: bool = STRICT_BREAKOUT_FOR_BUYS,
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

    # Surface the market regime clearly — this is the single most important
    # context for understanding why buy counts may be zero.
    if debug:
        bullish_label = "BULLISH" if market_trend.is_bullish else "BEARISH/CORRECTION"
        m_pct = market_trend.score * 100
        dist = getattr(market_trend, "distribution_days", "n/a")
        ftd = getattr(market_trend, "follow_through", False)
        print(
            f"[DEBUG] Market regime: {bullish_label} | M score={m_pct:.0f}% | "
            f"Distribution days={dist} | Follow-through={ftd}"
        )
        if not market_trend.is_bullish:
            print(
                "[DEBUG] NOTE: Market is not bullish — actionable buys are gated off. "
                "Only watchlist candidates will surface."
            )

    # Pre-filter: discard symbols whose RS score is already below the threshold
    # to avoid wasting API calls on weak stocks
    effective_rs_floor = _tightened_floor(CANONICAL_MIN_RS_SCORE, min_rs_score)
    filtered_symbols = []
    rs_score_by_symbol: Dict[str, float] = {}
    rs_below_threshold = 0
    rs_not_found = 0
    for symbol in symbols_list:
        try:
            match = rs_scores_df[rs_scores_df["Ticker"] == symbol]
            if not match.empty:
                rs_val = float(match.iloc[0]["RS_Score"])
            else:
                rs_val = 0
                rs_not_found += 1
        except Exception:
            rs_val = 0
            rs_not_found += 1

        if rs_val >= effective_rs_floor:
            filtered_symbols.append(symbol)
            rs_score_by_symbol[symbol] = rs_val
        else:
            rs_below_threshold += 1
            if debug:
                print(
                    f"[DEBUG] Pre-filter: {symbol} RS={rs_val:.1f} < "
                    f"{effective_rs_floor:.1f}, skipped"
                )

    if debug:
        print(
            f"[DEBUG] Pre-filter: {rs_below_threshold} stocks below RS "
            f"{effective_rs_floor:.1f} | "
            f"{rs_not_found} not found in RS universe | "
            f"{len(filtered_symbols)}/{len(symbols_list)} passed"
        )

    filtered_symbols.sort(key=lambda symbol: rs_score_by_symbol[symbol], reverse=True)

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
                require_fundamentals=require_fundamentals,
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

    actionable_buys = [result for result in results if result.get("scanner_category") == "actionable_buy"]
    watchlist_candidates = [result for result in results if result.get("scanner_category") == "watchlist_candidate"]
    quota_deferred = [result for result in results if result.get("scanner_category") == "quota_deferred"]

    if quota_deferred:
        print(
            f"[FMP] {len(quota_deferred)} candidate(s) quota_deferred; "
            "they were excluded from actionable buys and watchlists."
        )

    if debug:
        passed = len(results)
        rejected_score = len(filtered_symbols) - passed
        classified = len(actionable_buys) + len(watchlist_candidates)
        market_blocked = sum(1 for r in results if "market_not_bullish" in set(r.get("scanner_notes", [])))
        missing_fund = sum(
            1
            for result in results
            if {"current_growth_unavailable", "annual_growth_unavailable"}
            & set(result.get("scanner_notes", []))
        )
        print(
            f"[DEBUG] Post-scan summary: {len(symbols_list)} total | "
            f"{rs_below_threshold} failed RS pre-filter | "
            f"{rejected_score} passed RS but scored below watchlist floor | "
            f"{classified} reached watchlist/buy threshold | "
            f"{len(quota_deferred)} quota-deferred | "
            f"{market_blocked} watchlisted due to bearish market | "
            f"{missing_fund} flagged for missing fundamentals"
        )

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
    strict_breakout: bool = STRICT_BREAKOUT_FOR_BUYS,
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
    max_results: Optional[int] = None,
) -> None:
    """Print CANSLIM analysis results in a formatted table.

    Args:
        results: List of CANSLIM evaluation results
        market_trend: Market trend information

    """
    if not results:
        print("No stocks found matching criteria.")
        return

    display_results = results[:max_results] if max_results is not None else results

    print("\n" + "=" * 80)
    print(f"{title} ({len(results)} stocks found)")
    print("=" * 80)
    if max_results is not None and len(results) > max_results:
        print(f"Showing top {len(display_results)} rows in terminal. Full set will be available in CSV.")

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

    for idx, result in enumerate(display_results, start=1):
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
