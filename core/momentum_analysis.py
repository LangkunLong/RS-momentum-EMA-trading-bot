from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

import pandas as pd

from config import settings
from core.data_client import fetch_bulk_close_prices
from core.index_ticker_fetcher import get_sp500_tickers
from core.trading_sessions import (
    exact_session_row,
    history_through_exact_session,
    latest_us_equity_session,
    normalize_us_equity_session,
)


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
    if days_per_q is None:
        days_per_q = settings.TRADING_DAYS_PER_QUARTER
    if q1_weight is None:
        q1_weight = settings.RS_Q1_WEIGHT
    if q2_weight is None:
        q2_weight = settings.RS_Q2_WEIGHT
    if q3_weight is None:
        q3_weight = settings.RS_Q3_WEIGHT
    if q4_weight is None:
        q4_weight = settings.RS_Q4_WEIGHT

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
    as_of_session: object = None,
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

    if chunk_size is None:
        chunk_size = settings.CHUNK_SIZE
    if period is None:
        period = settings.RS_CALCULATION_PERIOD
    if percentile_multiplier is None:
        percentile_multiplier = settings.RS_PERCENTILE_MULTIPLIER
    if percentile_min is None:
        percentile_min = settings.RS_PERCENTILE_MIN

    # Check cache
    if os.path.exists(cache_file):
        file_time = datetime.fromtimestamp(os.path.getmtime(cache_file))
        if file_time.date() == datetime.now().date():
            try:
                print(f"Loading cached RS scores from {cache_file}...")
                cached_df = pd.read_csv(cache_file)
                cache_matches_session = as_of_session is None or (
                    "As_Of_Session" in cached_df.columns
                    and not cached_df.empty
                    and {
                        normalize_us_equity_session(value).date()
                        for value in cached_df["As_Of_Session"].dropna()
                    }
                    == {normalize_us_equity_session(as_of_session).date()}
                )
                if cache_matches_session and _cache_covers_requested_universe(cached_df, tickers):
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

    resolved_session = as_of_session
    if resolved_session is None:
        resolved_session = latest_us_equity_session(full_data)
    if resolved_session is not None:
        sliced = history_through_exact_session(full_data, resolved_session)
        event_row = exact_session_row(full_data, resolved_session)
        if sliced is None or event_row is None:
            return pd.DataFrame(columns=["Ticker", "Weighted_Perf", "RS_Score", "As_Of_Session"])
        fresh_columns = []
        for column in sliced.columns:
            try:
                value = float(event_row[column])
            except (TypeError, ValueError, OverflowError):
                continue
            if pd.notna(value) and value not in (float("inf"), float("-inf")):
                fresh_columns.append(column)
        full_data = sliced.loc[:, fresh_columns]

    if full_data.empty:
        print("No symbols have an exact completed-session close.")
        return pd.DataFrame(columns=["Ticker", "Weighted_Perf", "RS_Score", "As_Of_Session"])

    print("Calculating weighted performance...")

    def _wp_with_fallback(series: pd.Series) -> float | None:
        """Weighted performance with annualized fallback for short-history stocks (IPOs).

        Stocks with < 4 quarters of data (e.g. recent IPOs) cannot use the
        standard 4-quarter weighted formula.  For these, we annualize the raw
        return over the available history so they are comparable to 1-year
        peers in the cross-sectional ranking.  Minimum 21 bars (~1 month).

        We strip NaNs before calling calculate_weighted_performance so that a
        DataFrame aligned to a longer universe (e.g. 299-row S&P 500 frame)
        doesn't fool the length guard into attempting a calculation it will
        produce NaN from (dividing by a missing quarterly anchor price).
        """
        clean = series.dropna()
        wp = calculate_weighted_performance(clean)
        if wp is not None:
            return wp
        n = len(clean)
        if n < 21:
            return None
        raw_return = (clean.iloc[-1] - clean.iloc[0]) / clean.iloc[0]
        return (1 + raw_return) ** (252 / n) - 1

    rs_scores = full_data.apply(_wp_with_fallback)

    rs_df = rs_scores.reset_index()
    rs_df.columns = ["Ticker", "Weighted_Perf"]
    rs_df = rs_df.dropna()

    rs_df["RS_Score"] = rs_df["Weighted_Perf"].rank(pct=True) * percentile_multiplier + percentile_min
    if resolved_session is not None:
        rs_df["As_Of_Session"] = normalize_us_equity_session(resolved_session).date().isoformat()
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
