"""
CANSLIM Backtest Script

Evaluates specific tickers at monthly intervals over the past 2 years
using the existing CANSLIM criteria. Records buy signals and closing prices.

Fundamental data (C, A, I criteria) is now fetched from Financial Modeling
Prep with point-in-time filtering based on SEC filing acceptance dates,
eliminating the look-ahead bias present in the old yfinance approach.
Technical criteria (N, S, L, M) are evaluated using historical Alpaca
price data sliced to each evaluation date.
"""
from __future__ import annotations

import sys
import os
import time
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import settings
from core.data_client import (
    coerce_scalar,
    extract_float_series,
    fetch_ohlcv,
    fetch_bulk_close_prices,
    fetch_fundamental_data_as_of,
    fetch_company_info,
    clear_session_cache,
)
from core.index_ticker_fetcher import get_sp500_tickers
from core.canslim.c_current_earnings import evaluate_c
from core.canslim.a_annual_earnings import evaluate_a
from core.canslim.n_new_products import evaluate_n
from core.canslim.s_supply_demand import evaluate_s
from core.canslim.i_institutional import evaluate_i
from core.momentum_analysis import calculate_weighted_performance


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BACKTEST_TICKERS = ["CRWD", "GE", "GEV", "VST", "VRT"]
BENCHMARK = "SPY"
LOOKBACK_YEARS = 2
EVAL_INTERVAL_WEEKS = 4  # Evaluate every ~4 weeks


def _download_price_data(
    tickers: List[str], period: str = "3y"
) -> Dict[str, pd.DataFrame]:
    """Download OHLCV data for each ticker individually via Alpaca."""
    data = {}
    for ticker in tickers:
        print(f"  Downloading {ticker}...")
        try:
            df = fetch_ohlcv(ticker, period=period)
            if not df.empty and "Close" in df.columns:
                data[ticker] = df
            else:
                print(f"    WARNING: No data for {ticker}")
        except Exception as e:
            print(f"    ERROR downloading {ticker}: {e}")
        time.sleep(0.3)
    return data


def _download_bulk_closes(tickers: List[str], period: str = "3y") -> pd.DataFrame:
    """Download close prices for many tickers in bulk via Alpaca."""
    print(f"  Downloading bulk close data for {len(tickers)} tickers...")
    return fetch_bulk_close_prices(tickers, period=period, chunk_size=settings.CHUNK_SIZE)


def _calculate_rs_at_date(
    all_closes: pd.DataFrame, ticker: str, eval_date: pd.Timestamp
) -> float:
    """Calculate RS score for a ticker as-of a specific date."""
    # Slice data up to eval_date
    sliced = all_closes.loc[:eval_date].dropna(axis=1, how="all")
    if ticker not in sliced.columns:
        return 0.0

    # Calculate weighted performance for all tickers
    perfs = {}
    for col in sliced.columns:
        series = sliced[col].dropna()
        wp = calculate_weighted_performance(series)
        if wp is not None:
            perfs[col] = wp

    if ticker not in perfs or len(perfs) < 10:
        return 0.0

    # Rank
    perf_series = pd.Series(perfs)
    ranks = perf_series.rank(pct=True)
    rs_score = ranks[ticker] * settings.RS_PERCENTILE_MULTIPLIER + settings.RS_PERCENTILE_MIN
    return float(rs_score)


def _evaluate_market_at_date(spy_data: pd.DataFrame, eval_date: pd.Timestamp) -> Tuple[float, bool, int, bool]:
    """Evaluate M (Market Direction) as-of a specific date using SPY data.

    Returns: (score, is_bullish, distribution_days, follow_through)
    """
    sliced = spy_data.loc[:eval_date].copy()
    if len(sliced) < 50:
        return 0.4, False, 0, False

    closes = extract_float_series(sliced, "Close")
    volumes = extract_float_series(sliced, "Volume")

    ema_21 = closes.ewm(span=21).mean()
    ema_50 = closes.ewm(span=50).mean()
    ema_200 = closes.ewm(span=200).mean()

    latest_close = coerce_scalar(closes.iloc[-1])
    latest_ema_21 = coerce_scalar(ema_21.iloc[-1])
    latest_ema_50 = coerce_scalar(ema_50.iloc[-1])
    latest_ema_200 = coerce_scalar(ema_200.iloc[-1])

    # Distribution days
    lookback = min(settings.M_DISTRIBUTION_LOOKBACK, len(closes) - 1)
    recent_closes = closes.iloc[-(lookback + 1):]
    recent_volumes = volumes.iloc[-(lookback + 1):]
    dist_count = 0
    for i in range(1, len(recent_closes)):
        pct_change = (recent_closes.iloc[i] - recent_closes.iloc[i - 1]) / recent_closes.iloc[i - 1]
        if pct_change <= -settings.M_DISTRIBUTION_MIN_DECLINE and recent_volumes.iloc[i] > recent_volumes.iloc[i - 1]:
            dist_count += 1
    dist_score = max(1.0 - dist_count / settings.M_MAX_DISTRIBUTION_DAYS, 0.0)

    # Follow-through day
    ftd = False
    if len(closes) >= 30:
        rc = closes.tail(30)
        rv = volumes.tail(30)
        rally_count = 0
        in_rally = False
        for i in range(1, len(rc)):
            daily_chg = (rc.iloc[i] - rc.iloc[i - 1]) / rc.iloc[i - 1]
            if daily_chg > 0:
                if not in_rally:
                    in_rally = True
                    rally_count = 1
                else:
                    rally_count += 1
                if (rally_count >= settings.M_FOLLOW_THROUGH_MIN_DAY
                        and daily_chg >= settings.M_FOLLOW_THROUGH_MIN_PCT
                        and rv.iloc[i] > rv.iloc[i - 1]):
                    ftd = True
                    break
            elif daily_chg < -0.01:
                in_rally = False
                rally_count = 0
    ftd_score = 1.0 if ftd else 0.0

    # EMA trend
    trend_score = 0.0
    if latest_close > latest_ema_200:
        trend_score += settings.M_PRICE_ABOVE_200EMA_WEIGHT
    if latest_ema_21 > latest_ema_50 > latest_ema_200:
        trend_score += settings.M_EMA_ALIGNMENT_WEIGHT
    if len(ema_50) > settings.M_50EMA_RISING_LOOKBACK:
        ema_50_lb = coerce_scalar(ema_50.iloc[-settings.M_50EMA_RISING_LOOKBACK])
        if latest_ema_50 > ema_50_lb:
            trend_score += settings.M_50EMA_RISING_WEIGHT
    if latest_close > latest_ema_21:
        trend_score += settings.M_PRICE_ABOVE_21EMA_WEIGHT

    combined = (
        settings.M_DISTRIBUTION_WEIGHT * dist_score
        + settings.M_FOLLOW_THROUGH_WEIGHT * ftd_score
        + (1.0 - settings.M_DISTRIBUTION_WEIGHT - settings.M_FOLLOW_THROUGH_WEIGHT) * trend_score
    )
    combined = float(np.clip(combined, 0, 1))
    is_bullish = combined >= settings.M_BULLISH_THRESHOLD

    return combined, is_bullish, dist_count, ftd


def _evaluate_technical_at_date(
    ticker_data: pd.DataFrame,
    eval_date: pd.Timestamp,
    shares_outstanding: Optional[float],
) -> Dict[str, float]:
    """Evaluate N and S technical criteria as-of a specific date."""
    sliced = ticker_data.loc[:eval_date].copy()
    if len(sliced) < 60:
        return {"n_score": 0.0, "s_score": 0.0, "proximity": 0.0}

    closes = extract_float_series(sliced, "Close")
    volumes = extract_float_series(sliced, "Volume")

    latest_close = coerce_scalar(closes.iloc[-1])
    # 52-week high: use last 252 trading days or all available
    lookback_252 = min(252, len(closes))
    high_52 = coerce_scalar(closes.iloc[-lookback_252:].max())
    proximity = latest_close / high_52 if high_52 else 0.0
    avg_vol_50 = float(volumes.tail(50).mean()) if len(volumes) >= 50 else float(volumes.mean())

    # N score (proximity only — we don't have historical quarterly revenue)
    if proximity >= 0.98:
        proximity_score = 1.0
    elif proximity >= 0.90:
        proximity_score = (proximity - 0.90) / (0.98 - 0.90)
    elif proximity >= 0.75:
        proximity_score = (proximity - 0.75) / (0.90 - 0.75) * 0.3
    else:
        proximity_score = 0.0
    # Use proximity as full N score since we can't get historical revenue
    n_score = float(np.clip(proximity_score, 0, 1))

    # S score
    score_s, _ = evaluate_s(
        sliced, avg_vol_50, latest_close, high_52, shares_outstanding
    )

    return {
        "n_score": n_score,
        "s_score": score_s,
        "proximity": proximity,
        "close": latest_close,
        "high_52": high_52,
        "avg_vol_50": avg_vol_50,
    }


def _evaluate_fundamentals_at_date(
    symbol: str, eval_date: pd.Timestamp
) -> Dict[str, object]:
    """Fetch and evaluate C, A, I fundamental scores as-of a specific date.

    Uses FMP's acceptedDate to filter only data that was publicly
    available by the evaluation date, eliminating look-ahead bias.
    """
    try:
        fdata = fetch_fundamental_data_as_of(symbol, eval_date.to_pydatetime())
        qi = fdata["quarterly_income"]
        ai = fdata["annual_income"]
        bs = fdata["balance_sheet"]
        info = fdata["company_info"]

        score_c, current_growth = evaluate_c(qi)
        score_a, annual_growth, roe = evaluate_a(ai, balance_sheet=bs)

        held_pct = info.get("held_percent_institutions")
        num_holders = info.get("institution_count")
        score_i = evaluate_i(held_pct, num_institutional_holders=num_holders)
        shares = info.get("shares_outstanding")

        return {
            "c_score": score_c,
            "a_score": score_a,
            "i_score": score_i,
            "current_growth": current_growth,
            "annual_growth": annual_growth,
            "roe": roe,
            "shares_outstanding": shares,
        }
    except Exception as e:
        print(f"    ERROR fetching fundamentals for {symbol} @ {eval_date.date()}: {e}")
        return {
            "c_score": 0.0,
            "a_score": 0.0,
            "i_score": 0.1,
            "current_growth": None,
            "annual_growth": None,
            "roe": None,
            "shares_outstanding": None,
        }


def _compute_canslim_score(
    c: float, a: float, n: float, s: float, l: float, i: float, m: float,
    has_fundamentals: bool = True,
) -> float:
    """Compute weighted CANSLIM composite score (0-100)."""
    if has_fundamentals:
        score = (
            settings.CANSLIM_WEIGHT_C * c
            + settings.CANSLIM_WEIGHT_A * a
            + settings.CANSLIM_WEIGHT_N * n
            + settings.CANSLIM_WEIGHT_S * s
            + settings.CANSLIM_WEIGHT_L * l
            + settings.CANSLIM_WEIGHT_I * i
            + settings.CANSLIM_WEIGHT_M * m
        ) * 100
    else:
        tw = (
            settings.CANSLIM_WEIGHT_N
            + settings.CANSLIM_WEIGHT_S
            + settings.CANSLIM_WEIGHT_L
            + settings.CANSLIM_WEIGHT_I
            + settings.CANSLIM_WEIGHT_M
        )
        score = (
            (settings.CANSLIM_WEIGHT_N / tw) * n
            + (settings.CANSLIM_WEIGHT_S / tw) * s
            + (settings.CANSLIM_WEIGHT_L / tw) * l
            + (settings.CANSLIM_WEIGHT_I / tw) * i
            + (settings.CANSLIM_WEIGHT_M / tw) * m
        ) * 100
    return float(score)


# ---------------------------------------------------------------------------
# Main backtest loop
# ---------------------------------------------------------------------------
def run_backtest() -> pd.DataFrame:
    """Run the CANSLIM backtest and return a DataFrame of results."""

    print("=" * 70)
    print("CANSLIM BACKTEST")
    print(f"Tickers: {', '.join(BACKTEST_TICKERS)}")
    print(f"Period: {LOOKBACK_YEARS} years, evaluated every ~{EVAL_INTERVAL_WEEKS} weeks")
    print("=" * 70)

    # Reset session cache for a clean run
    clear_session_cache()

    # --- Step 1: Download price data ---
    print("\n[1/3] Downloading price data...")
    ticker_ohlcv = _download_price_data(BACKTEST_TICKERS + [BENCHMARK], period="3y")

    if BENCHMARK not in ticker_ohlcv:
        print("FATAL: Could not download SPY data.")
        return pd.DataFrame()

    for t in BACKTEST_TICKERS:
        if t not in ticker_ohlcv:
            print(f"WARNING: No data for {t}, it will be skipped.")

    # --- Step 2: Download S&P 500 closes for RS ranking ---
    print("\n[2/3] Downloading S&P 500 universe for RS ranking...")
    sp500_tickers = get_sp500_tickers()
    all_rs_tickers = list(set(BACKTEST_TICKERS + sp500_tickers))
    all_closes = _download_bulk_closes(all_rs_tickers, period="3y")
    print(f"  Got close data for {len(all_closes.columns)} tickers")

    # --- Step 3: Run backtest (fundamentals fetched per-date for point-in-time) ---
    print("\n[3/3] Running backtest evaluations...")

    end_date = datetime.now()
    start_date = end_date - timedelta(days=LOOKBACK_YEARS * 365)
    eval_dates = []
    d = start_date
    while d <= end_date:
        eval_dates.append(pd.Timestamp(d))
        d += timedelta(weeks=EVAL_INTERVAL_WEEKS)

    print(f"  {len(eval_dates)} evaluation dates from {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")

    records = []

    for eval_date in eval_dates:
        # Market direction at this date
        spy_data = ticker_ohlcv[BENCHMARK]
        m_score, m_bullish, dist_days, ftd = _evaluate_market_at_date(spy_data, eval_date)

        for ticker in BACKTEST_TICKERS:
            if ticker not in ticker_ohlcv:
                continue

            tdata = ticker_ohlcv[ticker]
            # Check if we have data at this date
            available = tdata.loc[:eval_date]
            if len(available) < 60:
                continue

            # RS score
            rs_score = _calculate_rs_at_date(all_closes, ticker, eval_date)

            # L score (RS / 100)
            l_score = rs_score / 100.0

            # Fundamental scores (point-in-time via FMP)
            fund = _evaluate_fundamentals_at_date(ticker, eval_date)

            # Technical scores (N, S)
            tech = _evaluate_technical_at_date(
                tdata, eval_date, fund.get("shares_outstanding")
            )

            # Fundamental scores
            c_score = fund.get("c_score", 0.0)
            a_score = fund.get("a_score", 0.0)
            i_score = fund.get("i_score", 0.1)
            has_fundamentals = fund.get("current_growth") is not None or fund.get("annual_growth") is not None

            # Composite CANSLIM score
            total = _compute_canslim_score(
                c=c_score,
                a=a_score,
                n=tech["n_score"],
                s=tech["s_score"],
                l=l_score,
                i=i_score,
                m=m_score,
                has_fundamentals=has_fundamentals,
            )

            buy_signal = total >= settings.MIN_CANSLIM_SCORE and rs_score >= settings.MIN_RS_SCORE
            close_price = tech["close"]

            records.append({
                "Date": eval_date.strftime("%Y-%m-%d"),
                "Ticker": ticker,
                "Close": round(close_price, 2),
                "RS_Score": round(rs_score, 1),
                "CANSLIM_Score": round(total, 1),
                "C": round(c_score * 100, 0),
                "A": round(a_score * 100, 0),
                "N": round(tech["n_score"] * 100, 0),
                "S": round(tech["s_score"] * 100, 0),
                "L": round(l_score * 100, 0),
                "I": round(i_score * 100, 0),
                "M": round(m_score * 100, 0),
                "52w_Prox": round(tech["proximity"] * 100, 1),
                "Mkt_Bullish": m_bullish,
                "Dist_Days": dist_days,
                "FTD": ftd,
                "BUY_SIGNAL": buy_signal,
            })

    df = pd.DataFrame(records)
    return df


def print_results(df: pd.DataFrame) -> None:
    """Print formatted backtest results."""
    if df.empty:
        print("No results.")
        return

    print("\n" + "=" * 100)
    print("BACKTEST RESULTS — BUY SIGNALS")
    print("=" * 100)
    print(
        "NOTE: C, A, I scores use point-in-time FMP data (filtered by SEC filing date).\n"
        "      N, S, L, M scores are calculated from historical price data.\n"
    )

    buys = df[df["BUY_SIGNAL"]]
    if buys.empty:
        print("No buy signals were generated with the default thresholds")
        print(f"(RS >= {settings.MIN_RS_SCORE}, CANSLIM >= {settings.MIN_CANSLIM_SCORE}).\n")
    else:
        print(f"Buy signals generated: {len(buys)}\n")
        print(
            f"{'Date':<12} {'Ticker':<7} {'Close':>8} {'RS':>5} {'CANSLIM':>8} "
            f"{'C':>4} {'A':>4} {'N':>4} {'S':>4} {'L':>4} {'I':>4} {'M':>4} "
            f"{'52wk%':>6} {'Mkt':>5}"
        )
        print("-" * 100)
        for _, row in buys.iterrows():
            mkt = "Bull" if row["Mkt_Bullish"] else "Bear"
            print(
                f"{row['Date']:<12} {row['Ticker']:<7} ${row['Close']:>7.2f} "
                f"{row['RS_Score']:>5.1f} {row['CANSLIM_Score']:>8.1f} "
                f"{row['C']:>4.0f} {row['A']:>4.0f} {row['N']:>4.0f} "
                f"{row['S']:>4.0f} {row['L']:>4.0f} {row['I']:>4.0f} {row['M']:>4.0f} "
                f"{row['52w_Prox']:>5.1f}% {mkt:>5}"
            )

    # Summary per ticker
    print("\n" + "=" * 100)
    print("FULL EVALUATION HISTORY (all dates, all tickers)")
    print("=" * 100)

    for ticker in BACKTEST_TICKERS:
        tdf = df[df["Ticker"] == ticker].copy()
        if tdf.empty:
            print(f"\n{ticker}: No data available")
            continue

        print(f"\n{'─' * 100}")
        print(f"  {ticker}")
        print(f"{'─' * 100}")
        print(
            f"  {'Date':<12} {'Close':>8} {'RS':>5} {'CANSLIM':>8} "
            f"{'C':>4} {'A':>4} {'N':>4} {'S':>4} {'L':>4} {'I':>4} {'M':>4} "
            f"{'52wk%':>6} {'Signal':>7}"
        )
        for _, row in tdf.iterrows():
            sig = ">>> BUY" if row["BUY_SIGNAL"] else ""
            print(
                f"  {row['Date']:<12} ${row['Close']:>7.2f} "
                f"{row['RS_Score']:>5.1f} {row['CANSLIM_Score']:>8.1f} "
                f"{row['C']:>4.0f} {row['A']:>4.0f} {row['N']:>4.0f} "
                f"{row['S']:>4.0f} {row['L']:>4.0f} {row['I']:>4.0f} {row['M']:>4.0f} "
                f"{row['52w_Prox']:>5.1f}% {sig:>7}"
            )

        # Price range
        buy_dates = tdf[tdf["BUY_SIGNAL"]]
        print(f"\n  Price range: ${tdf['Close'].min():.2f} — ${tdf['Close'].max():.2f}")
        print(f"  RS range: {tdf['RS_Score'].min():.1f} — {tdf['RS_Score'].max():.1f}")
        print(f"  CANSLIM range: {tdf['CANSLIM_Score'].min():.1f} — {tdf['CANSLIM_Score'].max():.1f}")
        if not buy_dates.empty:
            print(f"  Buy signals: {len(buy_dates)} | Buy prices: ${buy_dates['Close'].min():.2f} — ${buy_dates['Close'].max():.2f}")
        else:
            print(f"  Buy signals: 0")

    # Current prices vs first buy
    print("\n" + "=" * 100)
    print("RETURN ANALYSIS (first buy signal to latest price)")
    print("=" * 100)
    for ticker in BACKTEST_TICKERS:
        tdf = df[df["Ticker"] == ticker]
        if tdf.empty:
            continue
        buy_dates = tdf[tdf["BUY_SIGNAL"]]
        latest_close = tdf.iloc[-1]["Close"]
        if not buy_dates.empty:
            first_buy = buy_dates.iloc[0]
            buy_price = first_buy["Close"]
            ret = (latest_close - buy_price) / buy_price * 100
            print(
                f"  {ticker:<6} First buy: {first_buy['Date']} @ ${buy_price:.2f} | "
                f"Latest: ${latest_close:.2f} | Return: {ret:+.1f}%"
            )
        else:
            print(f"  {ticker:<6} No buy signals generated | Latest: ${latest_close:.2f}")


if __name__ == "__main__":
    results_df = run_backtest()
    print_results(results_df)

    # Save to CSV
    csv_file = f"backtest_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    results_df.to_csv(csv_file, index=False)
    print(f"\nResults saved to {csv_file}")
