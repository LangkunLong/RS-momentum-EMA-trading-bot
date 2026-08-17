"""Unified data access layer for CANSLIM Trading Bot.

Provides all price data via Alpaca and all fundamental data via
Financial Modeling Prep (FMP).  Every function returns data in the
exact pandas structure that the existing CANSLIM evaluation modules
expect, so NO downstream math changes are needed.

Session cache prevents redundant API calls within the same scan run.
"""

from __future__ import annotations

import math
import threading
import time
from datetime import datetime, time as dtime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from config import settings
from core.fmp_provider import (
    company_info_from_inst_history as _company_info_from_inst_history,
    fetch_company_profile as _fetch_company_profile,
    fetch_institutional_ownership_history as _fetch_inst_ownership_history,
)

# ═══════════════════════════════════════════════════════════════════════════════
# Session Cache (in-memory, per-run)
# ═══════════════════════════════════════════════════════════════════════════════

import hashlib
import os
import pickle

from cachetools import LRUCache

_session_cache = LRUCache(maxsize=500)
_cache_lock = threading.Lock()
_fmp_unavailable_endpoints: dict[str, str] = {}
_fmp_reported_endpoint_failures: set[str] = set()


def clear_session_cache() -> None:
    """Reset the in-memory session cache between scan runs."""
    global _fmp_quota_exhausted
    with _cache_lock:
        _session_cache.clear()
    _fmp_unavailable_endpoints.clear()
    _fmp_reported_endpoint_failures.clear()
    _fmp_quota_exhausted = False


def _cache_get(key: tuple) -> Any:
    with _cache_lock:
        return _session_cache.get(key)


def _cache_set(key: tuple, value: Any) -> None:
    with _cache_lock:
        _session_cache[key] = value


# ═══════════════════════════════════════════════════════════════════════════════
# Fundamentals Disk Cache (72-hour TTL — saves FMP quota across runs)
# ═══════════════════════════════════════════════════════════════════════════════

_FUND_CACHE_DIR = settings.FUNDAMENTALS_CACHE_DIR
_FUND_CACHE_TTL_HOURS = 72  # 3 days — quarterly statements rarely change


def _fund_cache_path(key: tuple) -> str:
    safe = hashlib.md5(str(key).encode()).hexdigest()
    return os.path.join(_FUND_CACHE_DIR, f"{safe}.pkl")


def _fund_cache_get(key: tuple) -> Any:
    """Load a cached fundamental DataFrame if it exists and is fresh."""
    path = _fund_cache_path(key)
    if not os.path.exists(path):
        return None
    age_hours = (time.time() - os.path.getmtime(path)) / 3600
    if age_hours > _FUND_CACHE_TTL_HOURS:
        return None
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _fund_cache_set(key: tuple, value: Any) -> None:
    """Persist a fundamental DataFrame to disk."""
    os.makedirs(_FUND_CACHE_DIR, exist_ok=True)
    path = _fund_cache_path(key)
    try:
        with open(path, "wb") as f:
            pickle.dump(value, f)
    except Exception:
        pass  # Cache write failure is non-fatal


# ═══════════════════════════════════════════════════════════════════════════════
# Client Singletons
# ═══════════════════════════════════════════════════════════════════════════════

_local = threading.local()
_ALPACA_FEED_WARNING_EMITTED = False


def _get_alpaca_client() -> StockHistoricalDataClient:
    if not hasattr(_local, "alpaca_client"):
        api_key = settings.ALPACA_API_KEY
        secret_key = settings.ALPACA_SECRET_KEY
        if not api_key or not secret_key:
            raise EnvironmentError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set. See .env.example for details.")
        _local.alpaca_client = StockHistoricalDataClient(api_key, secret_key)
    return _local.alpaca_client


def _get_alpaca_stock_feed() -> DataFeed:
    """Return the configured Alpaca stock feed, defaulting safely to IEX."""
    global _ALPACA_FEED_WARNING_EMITTED

    raw_value = str(getattr(settings, "ALPACA_STOCK_FEED", "iex") or "iex").strip().lower()
    try:
        return DataFeed(raw_value)
    except ValueError:
        if not _ALPACA_FEED_WARNING_EMITTED:
            print(f"[ALPACA] Unknown ALPACA_STOCK_FEED={raw_value!r}; defaulting to 'iex'.")
            _ALPACA_FEED_WARNING_EMITTED = True
        return DataFeed.IEX


def _fmp_api_key() -> str:
    key = settings.FMP_API_KEY
    if not key:
        raise EnvironmentError("FMP_API_KEY must be set. See .env.example for details.")
    return key


# ═══════════════════════════════════════════════════════════════════════════════
# Period Helpers
# ═══════════════════════════════════════════════════════════════════════════════

_PERIOD_MAP: Dict[str, int] = {
    "5d": 7,
    "1mo": 35,
    "3mo": 100,
    "6mo": 200,
    "1y": 370,
    "14mo": 435,
    "2y": 740,
    "3y": 1100,
    "5y": 1825,
    "7y": 2555,
}


def _period_to_days(period: str) -> int:
    """Convert a yfinance-style period string to calendar days."""
    if period in _PERIOD_MAP:
        return _PERIOD_MAP[period]
    raise ValueError(f"Unknown period string: {period!r}")


# ═══════════════════════════════════════════════════════════════════════════════
# FMP Generic Helper
# ═══════════════════════════════════════════════════════════════════════════════


from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


_US_EASTERN = ZoneInfo("America/New_York")
_REGULAR_SESSION_START = dtime(9, 30)
_REGULAR_SESSION_END = dtime(16, 0)


def _drop_incomplete_daily_bar(df: pd.DataFrame) -> pd.DataFrame:
    """Drop today's bar when the regular session has not closed in US/Eastern."""
    if df.empty:
        return df

    now_et = datetime.now(tz=_US_EASTERN)
    if now_et.weekday() >= 5 or now_et.hour >= 16:
        return df

    idx = df.index
    if idx.tz is not None:
        latest_bar_date_et = idx[-1].tz_convert(_US_EASTERN).date()
    else:
        latest_bar_date_et = idx[-1].tz_localize("UTC").tz_convert(_US_EASTERN).date()

    if latest_bar_date_et == now_et.date():
        return df.iloc[:-1]
    return df


def _to_eastern_index(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Convert a DatetimeIndex to US/Eastern."""
    if index.tz is None:
        return index.tz_localize("UTC").tz_convert(_US_EASTERN)
    return index.tz_convert(_US_EASTERN)


def _filter_regular_session(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only regular-session bars using US/Eastern timestamps."""
    if df.empty:
        return df

    result = df.copy()
    result.index = _to_eastern_index(result.index)
    return result.between_time(
        _REGULAR_SESSION_START.strftime("%H:%M"),
        _REGULAR_SESSION_END.strftime("%H:%M"),
        inclusive="left",
    )


def _get_fmp_session() -> requests.Session:
    """Create a requests session with built-in retry logic."""
    session = requests.Session()
    retries = Retry(
        total=settings.HTTP_RETRY_TOTAL,
        backoff_factor=settings.HTTP_RETRY_BACKOFF,
        status_forcelist=settings.HTTP_RETRY_STATUS_CODES,
    )
    pool_size = max(settings.HTTP_MAX_WORKERS, settings.MAX_WORKERS, 10)
    session.mount(
        "https://",
        HTTPAdapter(
            max_retries=retries,
            pool_connections=pool_size,
            pool_maxsize=pool_size,
        ),
    )
    return session


_fmp_session = _get_fmp_session()

# Session-level flag: once FMP is unreachable (all retries exhausted), skip
# further calls rather than burning time on guaranteed failures.
_fmp_quota_exhausted: bool = False


def _fmp_get(endpoint: str, params: Optional[dict] = None) -> Any:
    """Execute a GET request against the FMP API with retries.

    Returns an empty list on any unrecoverable error (quota, auth, network)
    so callers always receive a consistent type.

    Material failures (auth errors, plan restrictions) are printed so they
    are visible in logs rather than silently degrading data quality.
    """
    global _fmp_quota_exhausted

    if _fmp_quota_exhausted:
        return []
    if settings.FMP_SUPPRESS_REPEATED_ENDPOINT_ERRORS and endpoint in _fmp_unavailable_endpoints:
        return []

    url = f"{settings.FMP_BASE_URL}/{endpoint}"
    request_params = dict(params or {})
    request_params["apikey"] = _fmp_api_key()

    try:
        resp = _fmp_session.get(url, params=request_params, timeout=30)
    except requests.exceptions.RetryError:
        # Retry adapter exhausted all attempts — treat as a persistent failure.
        _fmp_quota_exhausted = True
        print("[FMP] All retries exhausted. Skipping FMP for remainder of session.")
        return []
    except requests.exceptions.RequestException:
        return []

    # 402: endpoint not included in the current plan.
    if resp.status_code == 402:
        _mark_fmp_endpoint_unavailable(endpoint, f"HTTP 402 on '{endpoint}': endpoint not available in current plan tier.")
        return []

    # 403: authentication or permission failure — surface it clearly.
    if resp.status_code == 403:
        _mark_fmp_endpoint_unavailable(
            endpoint,
            f"HTTP 403 on '{endpoint}': access denied — verify FMP_API_KEY and plan permissions.",
        )
        return []

    # 404: endpoint unavailable on current base URL / plan tier. Suppress repeats.
    if resp.status_code == 404:
        _mark_fmp_endpoint_unavailable(
            endpoint,
            f"HTTP 404 on '{endpoint}': endpoint unavailable on the current FMP base URL or plan tier.",
        )
        return []

    # 429: quota/rate limit reached. Stop hammering the provider for this run.
    if resp.status_code == 429:
        _fmp_quota_exhausted = True
        print(f"[FMP] HTTP 429 on '{endpoint}': rate limit or quota reached. Skipping FMP for remainder of session.")
        return []

    try:
        resp.raise_for_status()
    except requests.RequestException:
        print(f"[FMP] HTTP {resp.status_code} on '{endpoint}'.")
        return []
    data = resp.json()

    if isinstance(data, dict):
        error_msg = data.get("Error Message") or data.get("error") or data.get("message")
        if error_msg:
            quota_keywords = ("limit reached", "too many request", "quota", "upgrade", "subscribe")
            if any(kw in str(error_msg).lower() for kw in quota_keywords):
                _fmp_quota_exhausted = True
                print(f"[FMP] Quota/limit error: {error_msg}. Skipping FMP for remainder of session.")
            else:
                print(f"[FMP] Error on '{endpoint}': {error_msg}")
            return []

    return data


def _mark_fmp_endpoint_unavailable(endpoint: str, message: str) -> None:
    """Record a session-scoped unavailable endpoint and log it once."""
    if settings.FMP_SUPPRESS_REPEATED_ENDPOINT_ERRORS:
        _fmp_unavailable_endpoints[endpoint] = message
    if endpoint not in _fmp_reported_endpoint_failures:
        print(f"[FMP] {message}")
        _fmp_reported_endpoint_failures.add(endpoint)


def fetch_company_profile(symbol: str) -> dict[str, str]:
    """Fetch normalized company industry and sector labels from FMP."""
    return _fetch_company_profile(symbol, _fmp_get)


# ═══════════════════════════════════════════════════════════════════════════════
# Alpaca — Price / OHLCV Functions
# ═══════════════════════════════════════════════════════════════════════════════


def fetch_ohlcv(
    symbol: str,
    period: str = "1y",
    end_date: Optional[datetime] = None,
) -> pd.DataFrame:
    """Fetch daily OHLCV bars for a single ticker via Alpaca.

    Returns a DataFrame matching the structure downstream code expects:
        Index : DatetimeIndex (tz-naive)
        Columns : Open, High, Low, Close, Volume  (capitalized, float64)
    """
    cache_key = ("ohlcv", symbol, period, str(end_date))
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    client = _get_alpaca_client()
    days = _period_to_days(period)
    end = end_date or datetime.now()
    start = end - timedelta(days=days)

    request_params = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        feed=_get_alpaca_stock_feed(),
        adjustment=Adjustment.SPLIT,  # Normalize historical prices across stock splits
    )

    barset = client.get_stock_bars(request_params)
    df = barset.df

    if df.empty:
        empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        _cache_set(cache_key, empty)
        return empty

    # Flatten MultiIndex (symbol, timestamp) → plain DatetimeIndex
    if isinstance(df.index, pd.MultiIndex):
        df = df.droplevel("symbol")

    # Rename lowercase Alpaca columns → capitalized yfinance convention
    df = df.rename(
        columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
    )
    df = df[["Open", "High", "Low", "Close", "Volume"]]

    # Strip timezone to match yfinance tz-naive output
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)

    df = _drop_incomplete_daily_bar(df)

    df = df.astype(
        {
            "Open": float,
            "High": float,
            "Low": float,
            "Close": float,
            "Volume": float,
        }
    )

    _cache_set(cache_key, df)
    return df


def fetch_hourly_ohlcv(
    symbol: str,
    days: int = 30,
) -> pd.DataFrame:
    """Fetch 1-hour OHLCV bars for a single ticker via Alpaca.

    Returns a DataFrame with the same column conventions as ``fetch_ohlcv``:
        Index   : DatetimeIndex (tz-naive, bar open timestamp)
        Columns : Open, High, Low, Close, Volume  (capitalized, float64)

    Hourly bars allow exit monitoring to react to within-day MA violations
    rather than waiting for the daily close.

    Args:
        symbol: Ticker symbol (e.g. ``'NVDA'``).
        days: Number of calendar days of history to fetch (default: 30 ≈ ~195 bars).

    Returns:
        DataFrame of 1H bars, or an empty DataFrame on error.
    """
    cache_key = ("hourly_ohlcv", symbol, days)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    client = _get_alpaca_client()
    end = datetime.now()
    start = end - timedelta(days=days)

    request_params = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Hour,
        start=start,
        end=end,
        feed=_get_alpaca_stock_feed(),
        adjustment=Adjustment.SPLIT,
    )

    try:
        barset = client.get_stock_bars(request_params)
        df = barset.df
    except Exception:  # noqa: BLE001
        empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        return empty

    if df.empty:
        empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        _cache_set(cache_key, empty)
        return empty

    if isinstance(df.index, pd.MultiIndex):
        df = df.droplevel("symbol")

    df = df.rename(
        columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
    )
    df = df[["Open", "High", "Low", "Close", "Volume"]]
    df = _filter_regular_session(df)

    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)

    df = df.astype(
        {"Open": float, "High": float, "Low": float, "Close": float, "Volume": float}
    )

    _cache_set(cache_key, df)
    return df


def fetch_latest_intraday_price(
    symbol: str,
    lookback_minutes: int = 120,
) -> Optional[float]:
    """Fetch the latest regular-session minute close for entry sizing."""
    client = _get_alpaca_client()
    end = datetime.now()
    start = end - timedelta(minutes=max(lookback_minutes, 30))

    request_params = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
        feed=_get_alpaca_stock_feed(),
        adjustment=Adjustment.SPLIT,
    )

    try:
        barset = client.get_stock_bars(request_params)
        df = barset.df
    except Exception:  # noqa: BLE001
        return None

    if df.empty:
        return None

    if isinstance(df.index, pd.MultiIndex):
        df = df.droplevel("symbol")

    df = df.rename(columns={"close": "Close"})
    if "Close" not in df.columns:
        return None

    df = df[["Close"]]
    df = _filter_regular_session(df)
    if df.empty:
        return None

    return float(df["Close"].iloc[-1])


def fetch_bulk_close_prices(
    tickers: List[str],
    period: str = "14mo",
    chunk_size: int = 100,
) -> pd.DataFrame:
    """Download close prices for many tickers in batches via Alpaca.

    Returns:
        DataFrame with DatetimeIndex and one column per ticker (float close prices).

    """
    cache_key = ("bulk_close_prices", tuple(sorted(tickers)), period)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    client = _get_alpaca_client()
    days = _period_to_days(period)
    end = datetime.now()
    start = end - timedelta(days=days)

    all_frames: List[pd.DataFrame] = []

    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i : i + chunk_size]
        batch_num = i // chunk_size + 1
        total_batches = (len(tickers) + chunk_size - 1) // chunk_size
        print(f"Downloading batch {batch_num}/{total_batches} ({len(chunk)} tickers)...")

        try:
            request_params = StockBarsRequest(
                symbol_or_symbols=chunk,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                feed=_get_alpaca_stock_feed(),
                adjustment=Adjustment.SPLIT,  # Normalize RS calculation across stock splits
            )
            barset = client.get_stock_bars(request_params)
            df = barset.df

            if df.empty:
                print(f"  Batch {batch_num} returned empty data, skipping.")
                continue

            # Pivot from MultiIndex (symbol, timestamp) to wide: date × ticker
            close_series = df["close"].unstack(level="symbol")

            if close_series.index.tz is not None:
                close_series.index = close_series.index.tz_localize(None)

            close_series = _drop_incomplete_daily_bar(close_series)

            all_frames.append(close_series)
            time.sleep(0.5)  # respect Alpaca rate limits
        except Exception as e:
            print(f"  Batch {batch_num} failed: {e}")
            if len(chunk) > 1:
                retry_size = max(1, len(chunk) // 2)
                print(f"  Retrying failed batch in groups of {retry_size}.")
                recovered = fetch_bulk_close_prices(
                    chunk,
                    period=period,
                    chunk_size=retry_size,
                )
                if not recovered.empty:
                    all_frames.append(recovered)
            else:
                print(f"  Skipping invalid/unavailable symbol: {chunk[0]}")
            continue

    if not all_frames:
        return pd.DataFrame()

    result = pd.concat(all_frames, axis=1)
    result = result.dropna(axis=1, how="all")
    _cache_set(cache_key, result)
    return result


def fetch_bulk_ohlcv(
    tickers: List[str],
    period: str = "14mo",
    chunk_size: int = 100,
) -> Dict[str, pd.DataFrame]:
    """Download daily OHLCV data for many tickers in batches via Alpaca.

    Returns:
        Dict mapping symbol -> DataFrame(Open, High, Low, Close, Volume)
    """
    cache_key = ("bulk_ohlcv", tuple(sorted(tickers)), period, chunk_size)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    client = _get_alpaca_client()
    days = _period_to_days(period)
    end = datetime.now()
    start = end - timedelta(days=days)

    result: Dict[str, pd.DataFrame] = {}

    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i : i + chunk_size]
        batch_num = i // chunk_size + 1
        total_batches = (len(tickers) + chunk_size - 1) // chunk_size
        print(f"Downloading OHLCV batch {batch_num}/{total_batches} ({len(chunk)} tickers)...")

        try:
            request_params = StockBarsRequest(
                symbol_or_symbols=chunk,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                feed=_get_alpaca_stock_feed(),
                adjustment=Adjustment.SPLIT,
            )
            barset = client.get_stock_bars(request_params)
            df = barset.df
        except Exception as e:
            print(f"  OHLCV batch {batch_num} failed: {e}")
            if len(chunk) > 1:
                retry_size = max(1, len(chunk) // 2)
                print(f"  Retrying failed OHLCV batch in groups of {retry_size}.")
                result.update(
                    fetch_bulk_ohlcv(
                        chunk,
                        period=period,
                        chunk_size=retry_size,
                    )
                )
            else:
                print(f"  Skipping invalid/unavailable symbol: {chunk[0]}")
            continue

        if df.empty:
            print(f"  OHLCV batch {batch_num} returned empty data, skipping.")
            continue

        if isinstance(df.index, pd.MultiIndex):
            symbols_in_batch = [str(sym) for sym in df.index.get_level_values("symbol").unique()]
            for symbol in symbols_in_batch:
                try:
                    symbol_df = df.xs(symbol, level="symbol").copy()
                except KeyError:
                    continue

                symbol_df = symbol_df.rename(
                    columns={
                        "open": "Open",
                        "high": "High",
                        "low": "Low",
                        "close": "Close",
                        "volume": "Volume",
                    }
                )
                symbol_df = symbol_df[["Open", "High", "Low", "Close", "Volume"]]

                if symbol_df.index.tz is not None:
                    symbol_df.index = symbol_df.index.tz_localize(None)

                symbol_df = _drop_incomplete_daily_bar(symbol_df)
                if symbol_df.empty:
                    continue

                result[symbol] = symbol_df.astype(
                    {
                        "Open": float,
                        "High": float,
                        "Low": float,
                        "Close": float,
                        "Volume": float,
                    }
                )
        else:
            single_symbol = chunk[0]
            symbol_df = df.rename(
                columns={
                    "open": "Open",
                    "high": "High",
                    "low": "Low",
                    "close": "Close",
                    "volume": "Volume",
                }
            )
            symbol_df = symbol_df[["Open", "High", "Low", "Close", "Volume"]]
            if symbol_df.index.tz is not None:
                symbol_df.index = symbol_df.index.tz_localize(None)
            symbol_df = _drop_incomplete_daily_bar(symbol_df)
            if not symbol_df.empty:
                result[single_symbol] = symbol_df.astype(
                    {
                        "Open": float,
                        "High": float,
                        "Low": float,
                        "Close": float,
                        "Volume": float,
                    }
                )

        time.sleep(0.5)

    _cache_set(cache_key, result)
    return result


def validate_ticker(symbol: str) -> bool:
    """Check whether a ticker is valid and has recent data on Alpaca."""
    try:
        df = fetch_ohlcv(symbol, period="5d")
        return not df.empty and len(df) > 0
    except Exception:
        return False


def validate_tickers_bulk(symbols: List[str]) -> List[str]:
    """Check which tickers are valid using a bulk request to minimize API calls."""
    df = fetch_bulk_close_prices(symbols, period="5d")
    if df.empty:
        return []
    valid = []
    for col in df.columns:
        if df[col].notna().any():
            valid.append(str(col))
    return valid


# ═══════════════════════════════════════════════════════════════════════════════
# FMP — Fundamental Data Functions
# ═══════════════════════════════════════════════════════════════════════════════

# Mapping: FMP JSON key → row label that matches existing regex patterns
# in c_current_earnings._find_earnings_row(), a_annual_earnings._find_earnings_row(),
# a_annual_earnings._calculate_roe(), and n_new_products.evaluate_n().

_FMP_INCOME_FIELD_MAP = {
    "epsDiluted": "Diluted EPS",  # stable API uses camelCase; old v3 used lowercase
    "eps": "Basic EPS",
    "revenue": "Total Revenue",
    "netIncome": "Net Income",
    "grossProfit": "Gross Profit",
    "operatingIncome": "Operating Income",
    "costOfRevenue": "Cost Of Revenue",
}

_FMP_BALANCE_SHEET_FIELD_MAP = {
    "totalStockholdersEquity": "Total Stockholders Equity",
    "totalAssets": "Total Assets",
    "totalLiabilities": "Total Liabilities",
    "totalCurrentAssets": "Total Current Assets",
    "totalCurrentLiabilities": "Total Current Liabilities",
    "totalDebt": "Total Debt",
    "commonStock": "Common Stock",
    "retainedEarnings": "Retained Earnings",
}


def _fmp_records_to_financial_df(
    records: List[dict],
    field_map: dict,
) -> pd.DataFrame:
    """Convert FMP JSON records into a yfinance-style financial DataFrame.

    yfinance format:
        Index  — string row labels (e.g. "Diluted EPS", "Total Revenue")
        Columns — pd.Timestamp for each fiscal period
        Values — numeric
    """
    if not records:
        return pd.DataFrame()

    data_by_date: Dict[pd.Timestamp, Dict[str, float]] = {}
    for rec in records:
        date_str = rec.get("date")
        if not date_str:
            continue
        ts = pd.Timestamp(date_str)
        row_data = {}
        for fmp_key, label in field_map.items():
            val = rec.get(fmp_key)
            if val is not None:
                row_data[label] = val
        if row_data:
            data_by_date[ts] = row_data

    if not data_by_date:
        return pd.DataFrame()

    # Rows = field labels, Columns = dates (sorted oldest → newest)
    df = pd.DataFrame(data_by_date)
    df = df.sort_index(axis=1)
    return df


def fetch_quarterly_income_statement(
    symbol: str, limit: int = settings.FMP_QUARTERLY_LIMIT
) -> pd.DataFrame:
    """Fetch quarterly income statement in yfinance-compatible format."""
    cache_key = ("quarterly_income", symbol, limit)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    disk = _fund_cache_get(cache_key)
    if disk is not None:
        _cache_set(cache_key, disk)
        return disk

    # stable API uses ?symbol= query param instead of /symbol/ path segment
    records = _fmp_get("income-statement", {"symbol": symbol, "period": "quarter", "limit": limit})
    if isinstance(records, list) and 0 < len(records) < limit:
        print(f"[INFO] {symbol}: FMP returned {len(records)}/{limit} quarterly records")
    df = _fmp_records_to_financial_df(records, _FMP_INCOME_FIELD_MAP)
    _cache_set(cache_key, df)
    if not df.empty:
        _fund_cache_set(cache_key, df)
    return df


def fetch_annual_income_statement(
    symbol: str, limit: int = settings.FMP_ANNUAL_LIMIT
) -> pd.DataFrame:
    """Fetch annual income statement in yfinance-compatible format."""
    cache_key = ("annual_income", symbol, limit)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    disk = _fund_cache_get(cache_key)
    if disk is not None:
        _cache_set(cache_key, disk)
        return disk

    records = _fmp_get("income-statement", {"symbol": symbol, "period": "annual", "limit": limit})
    if isinstance(records, list) and 0 < len(records) < limit:
        print(f"[INFO] {symbol}: FMP returned {len(records)}/{limit} annual records")
    df = _fmp_records_to_financial_df(records, _FMP_INCOME_FIELD_MAP)
    _cache_set(cache_key, df)
    if not df.empty:
        _fund_cache_set(cache_key, df)
    return df


def fetch_balance_sheet(
    symbol: str, limit: int = settings.FMP_BALANCE_SHEET_LIMIT
) -> pd.DataFrame:
    """Fetch annual balance sheet in yfinance-compatible format."""
    cache_key = ("balance_sheet", symbol, limit)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    disk = _fund_cache_get(cache_key)
    if disk is not None:
        _cache_set(cache_key, disk)
        return disk

    records = _fmp_get("balance-sheet-statement", {"symbol": symbol, "limit": limit})
    df = _fmp_records_to_financial_df(records, _FMP_BALANCE_SHEET_FIELD_MAP)
    _cache_set(cache_key, df)
    if not df.empty:
        _fund_cache_set(cache_key, df)
    return df


def fetch_company_info(symbol: str) -> dict:
    """Fetch company-level info: shares outstanding, institutional ownership.

    Returns dict with keys:
        shares_outstanding:         int | None
        held_percent_institutions:  float (0-1) | None
        institution_count:          int | None
        prev_institution_count:     int | None  (quarter-over-quarter change)
    """
    cache_key = ("company_info", symbol)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    result: Dict[str, Any] = {
        "shares_outstanding": None,
        "held_percent_institutions": None,
        "institution_count": None,
        "prev_institution_count": None,
    }

    # 1. Profile — compute shares_outstanding from marketCap / price.
    # (stable API removed enterprise-values; profile is the reliable source.)
    try:
        profile = _fmp_get("profile", {"symbol": symbol})
        if profile and isinstance(profile, list) and len(profile) > 0:
            p = profile[0]
            mkt_cap = p.get("marketCap")
            price = p.get("price")
            if mkt_cap and price and price > 0:
                result["shares_outstanding"] = int(mkt_cap / price)
    except (requests.RequestException, ValueError, EnvironmentError):
        pass

    # 2. Institutional ownership history — paid-plan endpoint providing quarterly
    # snapshots with ownership percentage and holder count trend built in.
    inst_history = _fetch_inst_ownership_history(
        symbol,
        fmp_get_fn=_fmp_get,
        limit=settings.FMP_INSTITUTIONAL_HISTORY_LIMIT,
    )
    if inst_history:
        inst_info = _company_info_from_inst_history(inst_history, shares_outstanding=result["shares_outstanding"])
        result["held_percent_institutions"] = inst_info["held_percent_institutions"]
        result["institution_count"] = inst_info["institution_count"]
        result["prev_institution_count"] = inst_info["prev_institution_count"]

    # 3. Fallback: institutional-holder list (current-only, lower tier).
    # Used when the quarterly-snapshot endpoint returns nothing (e.g. 402).
    if result["held_percent_institutions"] is None:
        try:
            holders = _fmp_get("institutional-holder", {"symbol": symbol})
            if holders and isinstance(holders, list):
                result["institution_count"] = len(holders)

                if result["shares_outstanding"] and result["shares_outstanding"] > 0:
                    total_held = sum(h.get("shares", 0) for h in holders if h.get("shares"))
                    result["held_percent_institutions"] = min(total_held / result["shares_outstanding"], 1.0)
        except (requests.RequestException, ValueError, EnvironmentError):
            pass

    _cache_set(cache_key, result)
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# FMP — Point-in-Time Fundamentals for Backtesting
# ═══════════════════════════════════════════════════════════════════════════════


def _fetch_fmp_raw_history(symbol: str) -> dict:
    """Fetch and cache the raw, full-history JSON from FMP for efficient reusing."""
    raw_cache_key = ("fmp_raw_history", symbol)
    cached_raw = _cache_get(raw_cache_key)
    if cached_raw is not None:
        return cached_raw

    try:
        qi_raw = _fmp_get("income-statement", {"symbol": symbol, "period": "quarter", "limit": 80})
    except (requests.RequestException, ValueError, EnvironmentError):
        qi_raw = []
    try:
        ai_raw = _fmp_get("income-statement", {"symbol": symbol, "period": "annual", "limit": 20})
    except (requests.RequestException, ValueError, EnvironmentError):
        ai_raw = []
    try:
        bs_raw = _fmp_get("balance-sheet-statement", {"symbol": symbol, "limit": 20})
    except (requests.RequestException, ValueError, EnvironmentError):
        bs_raw = []
    # enterprise-values endpoint not available on stable free tier; ev_raw stays empty.
    ev_raw: list = []
    try:
        profile_raw = _fmp_get("profile", {"symbol": symbol})
    except (requests.RequestException, ValueError, EnvironmentError):
        profile_raw = []

    # Institutional ownership history — quarterly snapshots for PIT backtesting.
    # Uses acceptedDate-equivalent (date field) for point-in-time filtering.
    # Note: 13F filings are due ~45 days after quarter end, so using the quarter
    # date directly is slightly optimistic; still far better than no history.
    try:
        inst_ownership_raw = _fmp_get(
            "institutional-ownership/symbol-ownership",
            {"symbol": symbol, "limit": 20},
        )
        if not isinstance(inst_ownership_raw, list):
            inst_ownership_raw = []
    except (requests.RequestException, ValueError, EnvironmentError):
        inst_ownership_raw = []

    result = {
        "qi_raw": qi_raw,
        "ai_raw": ai_raw,
        "bs_raw": bs_raw,
        "ev_raw": ev_raw,
        "profile_raw": profile_raw,
        "inst_ownership_raw": inst_ownership_raw,
    }
    _cache_set(raw_cache_key, result)
    return result


def _filter_records_as_of(records: List[dict], as_of_date: datetime) -> List[dict]:
    """Keep only records whose SEC-accepted date is on or before *as_of_date*.

    Returns records sorted newest-first so that ``filtered[0]`` is always the
    most recent record available as of the cutoff date.
    """
    cutoff = pd.Timestamp(as_of_date)
    dated: list[tuple[pd.Timestamp, dict]] = []
    for rec in records:
        # Safely catch both None and empty strings ""
        accepted = rec.get("acceptedDate")
        if not accepted:
            accepted = rec.get("date")

        if not accepted:
            continue

        # acceptedDate often looks like "2024-10-30 16:05:12"
        try:
            ts = pd.Timestamp(str(accepted).split(" ")[0])
        except ValueError:
            continue

        if ts <= cutoff:
            dated.append((ts, rec))

    # Sort newest-first so callers using [0] get the most recent record
    dated.sort(key=lambda x: x[0], reverse=True)
    return [rec for _, rec in dated]


def _fetch_company_info_as_of(symbol: str, as_of_date: datetime) -> dict:
    """Fetch company info with point-in-time filtering for backtesting.

    Uses date-filtered institutional ownership snapshots when available,
    eliminating look-ahead bias for the I-component in backtests.

    Args:
        symbol: Ticker symbol.
        as_of_date: Cutoff date — only data dated on or before this date is used.

    Returns:
        Dict with keys ``shares_outstanding``, ``held_percent_institutions``,
        ``institution_count``, ``prev_institution_count``.

    """
    cache_key = ("company_info_as_of", symbol, as_of_date.strftime("%Y-%m-%d"))
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    result: Dict[str, Any] = {
        "shares_outstanding": None,
        "held_percent_institutions": None,
        "institution_count": None,
        "prev_institution_count": None,
    }

    raw_history = _fetch_fmp_raw_history(symbol)

    # Shares outstanding: filter historical enterprise values by date
    if raw_history["ev_raw"] and isinstance(raw_history["ev_raw"], list):
        ev_filtered = _filter_records_as_of(raw_history["ev_raw"], as_of_date)
        if ev_filtered:
            shares = ev_filtered[0].get("numberOfShares")
            if shares is not None:
                result["shares_outstanding"] = int(shares)

    # Fallback: profile data is current-only; acceptable as last resort.
    if result["shares_outstanding"] is None:
        if (
            raw_history["profile_raw"]
            and isinstance(raw_history["profile_raw"], list)
            and len(raw_history["profile_raw"]) > 0
        ):
            p = raw_history["profile_raw"][0]
            mkt_cap = p.get("marketCap")
            price = p.get("price")
            if mkt_cap and price and price > 0:
                result["shares_outstanding"] = int(mkt_cap / price)

    # Institutional ownership: use quarterly snapshots filtered by date.
    if raw_history["inst_ownership_raw"] and isinstance(raw_history["inst_ownership_raw"], list):
        pit_records = _filter_records_as_of(raw_history["inst_ownership_raw"], as_of_date)
        if pit_records:
            latest = pit_records[0]
            ownership_pct = latest.get("ownershipPercent")
            if ownership_pct is not None:
                try:
                    result["held_percent_institutions"] = min(float(ownership_pct) / 100.0, 1.0)
                except (TypeError, ValueError):
                    pass
            investors = latest.get("investorsHolding")
            if investors is not None:
                result["institution_count"] = int(investors)
            prev_investors = latest.get("lastInvestorsHolding")
            if prev_investors is not None:
                result["prev_institution_count"] = int(prev_investors)

    _cache_set(cache_key, result)
    return result


def fetch_fundamental_data_as_of(symbol: str, as_of_date: datetime) -> dict:
    """Fetch fundamental data that was publicly available as of *as_of_date*.

    Returns:
        {
            "quarterly_income": pd.DataFrame,
            "annual_income":    pd.DataFrame,
            "balance_sheet":    pd.DataFrame,
            "company_info":     dict,
        }

    """
    cache_key = ("fundamentals_as_of", symbol, as_of_date.strftime("%Y-%m-%d"))
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    raw_history = _fetch_fmp_raw_history(symbol)

    qi_filtered = _filter_records_as_of(raw_history["qi_raw"], as_of_date)
    ai_filtered = _filter_records_as_of(raw_history["ai_raw"], as_of_date)
    bs_filtered = _filter_records_as_of(raw_history["bs_raw"], as_of_date)

    result = {
        "quarterly_income": _fmp_records_to_financial_df(qi_filtered, _FMP_INCOME_FIELD_MAP),
        "annual_income": _fmp_records_to_financial_df(ai_filtered, _FMP_INCOME_FIELD_MAP),
        "balance_sheet": _fmp_records_to_financial_df(bs_filtered, _FMP_BALANCE_SHEET_FIELD_MAP),
        "company_info": _fetch_company_info_as_of(symbol, as_of_date),
    }

    _cache_set(cache_key, result)
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# DataFrame Utility Functions (migrated from yahoo_finance_helper.py)
# ═══════════════════════════════════════════════════════════════════════════════


def normalize_price_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Flatten MultiIndex columns from multi-ticker downloads."""
    if df.empty:
        return df

    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        if df.columns.nlevels == 2:
            level0 = df.columns.get_level_values(0)
            level1 = df.columns.get_level_values(1)
            price_like = {"open", "high", "low", "close", "adj close", "volume"}
            if any(str(val).lower() in price_like for val in level0):
                df.columns = level0
            else:
                df.columns = level1
        else:
            df.columns = ["_".join(str(part) for part in col if part) for col in df.columns]
    return df


def ensure_series(data: pd.Series | pd.DataFrame) -> pd.Series:
    """Coerce a DataFrame (single column) into a Series."""
    if isinstance(data, pd.DataFrame):
        if data.shape[1] == 0:
            raise ValueError("Cannot coerce an empty DataFrame into a Series")
        squeezed = data.squeeze("columns")
        data = squeezed if isinstance(squeezed, pd.Series) else data.iloc[:, 0]
    if not isinstance(data, pd.Series):
        raise TypeError(f"Expected pandas Series, received {type(data)!r}")
    return data


def coerce_scalar(value: Any) -> float:
    """Extract a single Python float from a Series / DataFrame / ndarray.

    Raises ``ValueError`` if the result is NaN or infinite so that corrupted
    data surfaces immediately rather than propagating silently through scores.
    """
    if isinstance(value, pd.DataFrame):
        if value.shape[1] == 0:
            raise ValueError("Cannot extract a scalar from an empty DataFrame")
        value = value.iloc[:, 0]
    if isinstance(value, pd.Series):
        if value.empty:
            raise ValueError("Cannot extract a scalar from an empty Series")
        value = value.iloc[-1]
    if isinstance(value, np.ndarray):
        if value.size == 0:
            raise ValueError("Cannot extract a scalar from an empty ndarray")
        value = value.item()
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"coerce_scalar produced non-finite value: {result}")
    return result


def extract_float_series(df: pd.DataFrame, column: str) -> pd.Series:
    """Extract a named column from *df* as a float64 Series."""
    if column not in df:
        raise KeyError(f"Column '{column}' not found in dataframe")
    series = ensure_series(df[column])
    return series.astype(float)
