from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

import pandas as pd

from config import settings
from core.data_client import fetch_bulk_close_prices
from core.index_ticker_fetcher import get_sp500_tickers


def _cache_covers_requested_universe(cached_df: pd.DataFrame, requested_tickers: list[str]) -> bool:
    """Return True when the cached RS file is broad enough for the current scan.

    The scanner ranks against the requested tickers plus a broad-market S&P 500
    context. A same-day cache generated from a much smaller run can therefore
    poison later scans. We require:
    - the standard columns to exist,
    - all requested tickers to be present, and
    - a universe size large enough to resemble the intended broad context.
    """
    required_columns = {"Ticker", "Weighted_Perf", "RS_Score"}
    if cached_df.empty or not required_columns.issubset(cached_df.columns):
        return False

    cached_tickers = {str(ticker).upper() for ticker in cached_df["Ticker"].dropna()}
    requested = {str(ticker).upper() for ticker in requested_tickers if ticker}
    if not requested.issubset(cached_tickers):
        return False

    return len(cached_tickers) >= max(400, len(requested))


def calculate_weighted_performance(
    data_series: pd.Series,
    days_per_q: Optional[int] = None,
    q1_weight: Optional[float] = None,
    q2_weight: Optional[float] = None,
    q3_weight: Optional[float] = None,
    q4_weight: Optional[float] = None,
) -> float | None:
    """Calculate the 12-month weighted RS performance for a single stock's price series.

    Args:
        data_series: Daily close price series (oldest to newest).
        days_per_q: Trading days per quarter (overrides settings).
        q1_weight: Weight for most recent quarter (overrides settings).
        q2_weight: Weight for second-most-recent quarter (overrides settings).
        q3_weight: Weight for third quarter (overrides settings).
        q4_weight: Weight for oldest quarter (overrides settings).

    Returns:
        Weighted performance decimal (e.g. 0.25 = 25% weighted gain), or None if
        insufficient data.
    """
    days_per_q = days_per_q or settings.TRADING_DAYS_PER_QUARTER
    q1_weight = q1_weight or settings.RS_Q1_WEIGHT
    q2_weight = q2_weight or settings.RS_Q2_WEIGHT
    q3_weight = q3_weight or settings.RS_Q3_WEIGHT
    q4_weight = q4_weight or settings.RS_Q4_WEIGHT

    try:
        if len(data_series) < 4 * days_per_q:
            return None

        perf_q1 = (data_series.iloc[-1] / data_series.iloc[-days_per_q]) - 1
        perf_q2 = (data_series.iloc[-days_per_q] / data_series.iloc[-2 * days_per_q]) - 1
        perf_q3 = (data_series.iloc[-2 * days_per_q] / data_series.iloc[-3 * days_per_q]) - 1
        perf_q4 = (data_series.iloc[-3 * days_per_q] / data_series.iloc[-4 * days_per_q]) - 1

        weighted_performance = (
            (q1_weight * perf_q1) + (q2_weight * perf_q2) + (q3_weight * perf_q3) + (q4_weight * perf_q4)
        )
        return weighted_performance
    except (IndexError, TypeError, ZeroDivisionError):
        return None


def calculate_rs_scores_for_tickers(
    tickers: list[str],
    cache_file: Optional[str] = None,
    chunk_size: Optional[int] = None,
    period: Optional[str] = None,
    percentile_multiplier: Optional[float] = None,
    percentile_min: Optional[float] = None,
) -> pd.DataFrame:
    """Download price data and compute RS scores for a list of tickers.

    Scores are percentile-ranked against the full S&P 500 universe to provide
    meaningful cross-sectional comparison. Results are cached daily to disk.

    Args:
        tickers: Ticker symbols to score.
        cache_file: Path to the CSV cache file (overrides settings).
        chunk_size: Batch size for Alpaca downloads (overrides settings).
        period: Look-back period string, e.g. ``'14mo'`` (overrides settings).
        percentile_multiplier: Scales the 0-1 percentile rank (overrides settings).
        percentile_min: Minimum score offset (overrides settings).

    Returns:
        DataFrame with columns ``['Ticker', 'Weighted_Perf', 'RS_Score']``,
        sorted descending by RS_Score. Empty DataFrame on total download failure.
    """
    if cache_file is None:
        cache_dir = settings.RS_CACHE_DIR
        cache_filename = settings.RS_CACHE_FILE
        cache_file = os.path.join(cache_dir, cache_filename)
        os.makedirs(cache_dir, exist_ok=True)

    chunk_size = chunk_size or settings.CHUNK_SIZE
    period = period or settings.RS_CALCULATION_PERIOD
    percentile_multiplier = percentile_multiplier or settings.RS_PERCENTILE_MULTIPLIER
    percentile_min = percentile_min or settings.RS_PERCENTILE_MIN

    # Check cache
    if os.path.exists(cache_file):
        file_time = datetime.fromtimestamp(os.path.getmtime(cache_file))
        if file_time.date() == datetime.now().date():
            try:
                print(f"Loading cached RS scores from {cache_file}...")
                cached_df = pd.read_csv(cache_file)
                if _cache_covers_requested_universe(cached_df, tickers):
                    return cached_df
                print("RS score cache does not match the requested universe, re-downloading...")
            except (pd.errors.ParserError, OSError) as exc:
                print(f"RS score cache is corrupt ({exc}), re-downloading...")

    # Add S&P 500 context tickers for cross-sectional ranking
    sp500 = get_sp500_tickers()
    all_tickers = list(set(tickers + sp500))

    print(f"Downloading data for {len(all_tickers)} tickers via Alpaca...")

    full_data = fetch_bulk_close_prices(all_tickers, period=period, chunk_size=chunk_size)

    if full_data.empty:
        print("All downloads failed.")
        return pd.DataFrame()

    print("Calculating weighted performance...")
    rs_scores = full_data.apply(calculate_weighted_performance)

    rs_df = rs_scores.reset_index()
    rs_df.columns = ["Ticker", "Weighted_Perf"]
    rs_df = rs_df.dropna()

    rs_df["RS_Score"] = rs_df["Weighted_Perf"].rank(pct=True) * percentile_multiplier + percentile_min
    rs_df = rs_df.sort_values(by="RS_Score", ascending=False).reset_index(drop=True)

    rs_df.to_csv(cache_file, index=False)
    print(f"RS Scores saved to {cache_file}")

    return rs_df


def calculate_rs_momentum(symbol: str, rs_scores_df: pd.DataFrame) -> float:
    """Look up a ticker's RS score from a pre-computed RS scores DataFrame.

    This function is kept for backward compatibility. Prefer accessing the
    DataFrame directly in new code.

    Args:
        symbol: Ticker symbol to look up.
        rs_scores_df: DataFrame produced by ``calculate_rs_scores_for_tickers()``.

    Returns:
        RS score (typically 0-100), or 0.0 if the ticker is not found.
    """
    try:
        score = rs_scores_df[rs_scores_df["Ticker"] == symbol]["RS_Score"].iloc[0]
        return float(score)
    except (IndexError, KeyError):
        return 0.0
