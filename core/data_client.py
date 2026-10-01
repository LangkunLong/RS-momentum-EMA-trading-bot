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
from contextlib import contextmanager
from collections.abc import Iterator
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
from core.alpaca_client_policy import configure_alpaca_rest_client
from core.scheduler_observation import (
    current_scheduler_observation,
    latch_service_issue,
    record_event,
    record_input_gap,
    record_provider_event,
    record_resource_denial,
)


def _fetch_company_profile(symbol: str, fmp_get_fn):
    """Lazily load the optional FMP profile provider."""
    from core.fmp_provider import fetch_company_profile

    return fetch_company_profile(symbol, fmp_get_fn)


def _fetch_inst_ownership_history(symbol: str, *, fmp_get_fn, limit: int, as_of_date=None):
    """Lazily load the optional FMP institutional-history provider."""
    from core.fmp_provider import fetch_institutional_ownership_history

    return fetch_institutional_ownership_history(
        symbol,
        fmp_get_fn=fmp_get_fn,
        limit=limit,
        as_of_date=as_of_date,
    )


def _company_info_from_inst_history(history, shares_outstanding=None):
    """Lazily load the optional FMP institutional-history normalizer."""
    from core.fmp_provider import company_info_from_inst_history

    return company_info_from_inst_history(history, shares_outstanding=shares_outstanding)
# ═══════════════════════════════════════════════════════════════════════════════
# Session Cache (in-memory, per-run)
# ═══════════════════════════════════════════════════════════════════════════════

import hashlib
import json
import os
import pickle

from cachetools import LRUCache

_session_cache = LRUCache(maxsize=500)
_cache_lock = threading.Lock()
_fmp_unavailable_endpoints: dict[str, str] = {}
_fmp_unavailable_outcomes: dict[str, dict[str, Any]] = {}
_fmp_reported_endpoint_failures: set[str] = set()
_fmp_session_failure: dict[str, Any] | None = None


def clear_session_cache() -> None:
    """Reset the in-memory session cache between scan runs."""
    global _fmp_budget_warning_emitted, _fmp_quota_exhausted, _fmp_session_failure
    with _cache_lock:
        _session_cache.clear()
    _fmp_unavailable_endpoints.clear()
    _fmp_unavailable_outcomes.clear()
    _fmp_reported_endpoint_failures.clear()
    _fmp_quota_exhausted = False
    _fmp_session_failure = None
    _fmp_budget_warning_emitted = False
    reset_fmp_request_context()


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
    if age_hours > settings.FMP_FUND_CACHE_TTL_HOURS:
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
    return configure_alpaca_rest_client(_local.alpaca_client)


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
_fmp_budget_lock = threading.Lock()
_fmp_request_context = threading.local()
_fmp_budget_warning_emitted = False
_fmp_run_budget_remaining: int | None = None
_fmp_run_budget_cap: int | None = None


@contextmanager
def fmp_request_budget(max_requests: int) -> Iterator[None]:
    """Cap logical FMP requests for one doctor or bounded scanner run."""
    global _fmp_run_budget_remaining, _fmp_run_budget_cap
    with _fmp_budget_lock:
        if _fmp_run_budget_remaining is not None:
            raise RuntimeError("An FMP run request budget is already active")
        _fmp_run_budget_remaining = max(0, int(max_requests))
        _fmp_run_budget_cap = _fmp_run_budget_remaining
    try:
        yield
    finally:
        with _fmp_budget_lock:
            _fmp_run_budget_remaining = None
            _fmp_run_budget_cap = None


def fmp_observation_request_limit(max_additional_requests: int = 198) -> int:
    """Return the run cap bounded by remaining ledger allowance, without writes."""
    path = str(settings.FMP_REQUEST_LEDGER_PATH)
    expected_window = _fmp_window_start(_fmp_now_et())
    try:
        with open(path, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        if not isinstance(saved, dict):
            raise ValueError("FMP usage ledger must contain a JSON object")
        saved_window = datetime.fromisoformat(str(saved["window_start"]))
        if saved_window.tzinfo is None:
            raise ValueError("FMP usage ledger window must include a timezone")
        saved_window = saved_window.astimezone(expected_window.tzinfo)
        count = max(int(saved.get("count", 0)), 0)
        if saved_window == expected_window:
            local_remaining = max(0, int(settings.FMP_DAILY_REQUEST_BUDGET) - count)
        elif saved_window < expected_window:
            local_remaining = max(0, int(settings.FMP_DAILY_REQUEST_BUDGET))
        else:
            raise ValueError("FMP usage ledger window is in the future")
    except FileNotFoundError:
        latch_service_issue("fmp_ledger_missing", {"ledger_state": "missing"})
        return 0
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        latch_service_issue("fmp_ledger_unreadable", {"ledger_state": "unreadable"})
        return 0
    return min(max(0, int(max_additional_requests)), local_remaining)


def fmp_request_budget_snapshot() -> dict[str, int | None]:
    """Return the active process cap and its remaining logical requests."""
    with _fmp_budget_lock:
        return {"cap": _fmp_run_budget_cap, "remaining": _fmp_run_budget_remaining}


def _is_fmp_free_plan() -> bool:
    return str(getattr(settings, "FMP_PLAN", "free")).strip().lower() == "free"


def reset_fmp_request_context() -> None:
    """Clear request-defer state for the current scanner worker."""
    _fmp_request_context.quota_deferred = False
    _fmp_request_context.deferral_reason = None


def fmp_request_was_deferred() -> bool:
    """Return whether the current worker was denied by the local FMP budget."""
    return bool(getattr(_fmp_request_context, "quota_deferred", False))


def fmp_request_deferral_reason() -> str | None:
    """Return the last typed FMP denial or provider outcome for this worker."""
    return getattr(_fmp_request_context, "deferral_reason", None)


def _set_fmp_deferral_reason(reason: str) -> None:
    _fmp_request_context.deferral_reason = reason


def _fmp_now_et() -> datetime:
    """Return the current provider-accounting time in US Eastern."""
    return datetime.now(tz=_US_EASTERN)


def _fmp_window_start(now_et: datetime) -> datetime:
    """Return the 3 p.m. Eastern start of the active provider reset window."""
    if now_et.tzinfo is None:
        now_et = now_et.replace(tzinfo=_US_EASTERN)
    else:
        now_et = now_et.astimezone(_US_EASTERN)
    reset_hour = int(getattr(settings, "FMP_RESET_HOUR_EASTERN", 15))
    start = now_et.replace(hour=reset_hour, minute=0, second=0, microsecond=0)
    if now_et < start:
        start -= timedelta(days=1)
    return start


def _write_fmp_usage(path: str, usage: dict[str, Any]) -> bool:
    """Atomically persist request usage; fail closed if accounting cannot be saved."""
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        temp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(usage, handle, sort_keys=True)
        os.replace(temp_path, path)
        return True
    except OSError:
        return False


def _reserve_fmp_request() -> bool:
    """Reserve one persisted free-tier request before any network I/O."""
    global _fmp_budget_warning_emitted, _fmp_run_budget_remaining
    observation_active = current_scheduler_observation() is not None

    if not _is_fmp_free_plan():
        with _fmp_budget_lock:
            if _fmp_run_budget_remaining is None:
                return True
            if _fmp_run_budget_remaining <= 0:
                _set_fmp_deferral_reason("process_cap")
                if not observation_active:
                    _fmp_request_context.quota_deferred = True
                return False
            _fmp_run_budget_remaining -= 1
            return True

    path = str(settings.FMP_REQUEST_LEDGER_PATH)
    window_start = _fmp_window_start(_fmp_now_et()).isoformat()
    budget = int(settings.FMP_DAILY_REQUEST_BUDGET)

    with _fmp_budget_lock:
        if (
            not observation_active
            and _fmp_run_budget_remaining is not None
            and _fmp_run_budget_remaining <= 0
        ):
            _set_fmp_deferral_reason("process_cap")
            _fmp_request_context.quota_deferred = True
            return False

        usage: dict[str, Any] = {"window_start": window_start, "count": 0}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                saved = json.load(handle)
            if not isinstance(saved, dict):
                raise ValueError("FMP usage ledger must contain a JSON object")
            if saved.get("window_start") == window_start:
                usage["count"] = max(int(saved.get("count", 0)), 0)
        except FileNotFoundError:
            if observation_active:
                _set_fmp_deferral_reason("ledger_missing")
                latch_service_issue("fmp_ledger_missing", {"ledger_state": "missing"})
                return False
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            _set_fmp_deferral_reason("ledger_unreadable")
            if observation_active:
                latch_service_issue("fmp_ledger_unreadable", {"ledger_state": "unreadable"})
            else:
                _fmp_request_context.quota_deferred = True
            if not _fmp_budget_warning_emitted:
                print("[FMP] Request usage ledger is unreadable; failing closed to protect the free-plan quota.")
                _fmp_budget_warning_emitted = True
            return False

        if usage["count"] >= budget:
            _set_fmp_deferral_reason("local_ledger")
            _fmp_request_context.quota_deferred = True
            if not _fmp_budget_warning_emitted:
                print(
                    f"[FMP] Local daily request budget ({budget}) reached; "
                    "remaining uncached candidates will be quota_deferred."
                )
                _fmp_budget_warning_emitted = True
            return False

        if _fmp_run_budget_remaining is not None and _fmp_run_budget_remaining <= 0:
            _set_fmp_deferral_reason("process_cap")
            if not observation_active:
                _fmp_request_context.quota_deferred = True
            return False

        usage["count"] += 1
        if _write_fmp_usage(path, usage):
            if _fmp_run_budget_remaining is not None:
                _fmp_run_budget_remaining -= 1
            return True

        _set_fmp_deferral_reason("ledger_write_failed")
        if observation_active:
            latch_service_issue("fmp_ledger_write_failed", {"ledger_state": "write_failed"})
        else:
            _fmp_request_context.quota_deferred = True
        if not _fmp_budget_warning_emitted:
            print("[FMP] Could not persist request usage; failing closed to protect the free-plan quota.")
            _fmp_budget_warning_emitted = True
        return False


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
    retry_total = 0 if _is_fmp_free_plan() else settings.HTTP_RETRY_TOTAL
    retry_statuses = [] if _is_fmp_free_plan() else settings.HTTP_RETRY_STATUS_CODES
    retries = Retry(
        total=retry_total,
        backoff_factor=settings.HTTP_RETRY_BACKOFF,
        status_forcelist=retry_statuses,
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


def _record_suppressed_fmp_outcome(
    endpoint: str,
    params: Optional[dict],
    failure: dict[str, Any] | None,
) -> None:
    if current_scheduler_observation() is None:
        return
    details = dict(failure or {})
    reason = str(details.get("reason", "provider_refusal"))
    symbol = str((params or {}).get("symbol", "unknown"))
    _set_fmp_deferral_reason(reason)
    details.update({"endpoint": endpoint, "symbol": symbol})
    record_provider_event("fmp", "suppressed_followup", details)
    record_event("fmp_request", endpoint, "suppressed_after_failure", details)
    record_input_gap(
        symbol,
        endpoint,
        f"{reason}_suppressed",
        coverage_status="failed",
    )
    latch_service_issue(f"fmp_{reason}_suppressed", details)


def _record_fmp_transport_error(
    endpoint: str,
    symbol: str,
    error_type: str,
    *,
    suppress_followups: bool,
) -> None:
    global _fmp_quota_exhausted, _fmp_session_failure
    details = {"endpoint": endpoint, "error_type": error_type}
    _set_fmp_deferral_reason("transport_error")
    record_provider_event("fmp", "transport_error", details)
    record_event("fmp_request", endpoint, "transport_error", details)
    latch_service_issue("fmp_transport_error", details)
    record_input_gap(symbol, endpoint, "transport_error", coverage_status="failed")
    if suppress_followups and current_scheduler_observation() is not None:
        _fmp_quota_exhausted = True
        _fmp_session_failure = {"reason": "transport_error", **details}


def _fmp_get(endpoint: str, params: Optional[dict] = None) -> Any:
    """Execute a GET request against the FMP API with retries.

    Returns an empty list on any unrecoverable error (quota, auth, network)
    so callers always receive a consistent type.

    Material failures (auth errors, plan restrictions) are printed so they
    are visible in logs rather than silently degrading data quality.
    """
    global _fmp_quota_exhausted, _fmp_session_failure
    symbol = str((params or {}).get("symbol", "unknown"))

    if _fmp_quota_exhausted:
        _record_suppressed_fmp_outcome(endpoint, params, _fmp_session_failure)
        return []
    if settings.FMP_SUPPRESS_REPEATED_ENDPOINT_ERRORS and endpoint in _fmp_unavailable_endpoints:
        _record_suppressed_fmp_outcome(
            endpoint,
            params,
            _fmp_unavailable_outcomes.get(
                endpoint,
                {"reason": "provider_refusal", "endpoint": endpoint},
            ),
        )
        return []

    url = f"{settings.FMP_BASE_URL}/{endpoint}"
    request_params = dict(params or {})
    if _is_fmp_free_plan() and "limit" in request_params:
        try:
            request_params["limit"] = min(
                int(request_params["limit"]),
                int(settings.FMP_FREE_MAX_RECORDS),
            )
        except (TypeError, ValueError):
            request_params["limit"] = int(settings.FMP_FREE_MAX_RECORDS)
    request_params["apikey"] = _fmp_api_key()
    if not _reserve_fmp_request():
        reason = fmp_request_deferral_reason()
        if reason == "local_ledger":
            record_input_gap(
                symbol,
                endpoint,
                "local_ledger_exhausted",
                coverage_status="degraded",
            )
        elif reason == "process_cap":
            record_resource_denial("fmp_cap_denied", {"endpoint": endpoint})
        return []

    record_provider_event("fmp", "attempted", {"endpoint": endpoint})
    record_event(
        "fmp_request",
        endpoint,
        "reserved",
        {"symbol": symbol},
    )

    try:
        resp = _fmp_session.get(
            url,
            params=request_params,
            timeout=settings.FMP_HTTP_TIMEOUT_SECONDS,
        )
    except requests.exceptions.RetryError:
        # Retry adapter exhausted all attempts — treat as a persistent failure.
        _fmp_quota_exhausted = True
        _fmp_session_failure = {
            "reason": "transport_error",
            "endpoint": endpoint,
            "error_type": "RetryError",
        }
        _record_fmp_transport_error(
            endpoint,
            symbol,
            "RetryError",
            suppress_followups=True,
        )
        print("[FMP] All retries exhausted. Skipping FMP for remainder of session.")
        return []
    except requests.exceptions.RequestException as exc:
        _record_fmp_transport_error(
            endpoint,
            symbol,
            type(exc).__name__,
            suppress_followups=True,
        )
        return []

    def record_provider_refusal() -> None:
        global _fmp_quota_exhausted, _fmp_session_failure
        _set_fmp_deferral_reason("provider_refusal")
        status_code = int(resp.status_code)
        details = {"endpoint": endpoint, "http_status": status_code}
        record_provider_event("fmp", "provider_refusal", details)
        record_event("fmp_request", endpoint, "provider_refusal", details)
        latch_service_issue("fmp_provider_refusal", details)
        record_input_gap(symbol, endpoint, "provider_refusal", coverage_status="failed")
        if status_code in {402, 403, 404}:
            _fmp_unavailable_outcomes[endpoint] = {
                "reason": "provider_refusal",
                "http_status": status_code,
            }
        if status_code == 429:
            _fmp_quota_exhausted = True
            _fmp_session_failure = {
                "reason": "provider_refusal",
                "endpoint": endpoint,
                "http_status": status_code,
            }

    # 402: endpoint not included in the current plan.
    if resp.status_code == 402:
        record_provider_refusal()
        _mark_fmp_endpoint_unavailable(endpoint, f"HTTP 402 on '{endpoint}': endpoint not available in current plan tier.")
        return []

    # 403: authentication or permission failure — surface it clearly.
    if resp.status_code == 403:
        record_provider_refusal()
        _mark_fmp_endpoint_unavailable(
            endpoint,
            f"HTTP 403 on '{endpoint}': access denied — verify FMP_API_KEY and plan permissions.",
        )
        return []

    # 404: endpoint unavailable on current base URL / plan tier. Suppress repeats.
    if resp.status_code == 404:
        record_provider_refusal()
        _mark_fmp_endpoint_unavailable(
            endpoint,
            f"HTTP 404 on '{endpoint}': endpoint unavailable on the current FMP base URL or plan tier.",
        )
        return []

    # 429: quota/rate limit reached. Stop hammering the provider for this run.
    if resp.status_code == 429:
        _fmp_quota_exhausted = True
        record_provider_refusal()
        print(f"[FMP] HTTP 429 on '{endpoint}': rate limit or quota reached. Skipping FMP for remainder of session.")
        return []

    try:
        resp.raise_for_status()
    except requests.RequestException:
        record_provider_refusal()
        print(f"[FMP] HTTP {resp.status_code} on '{endpoint}'.")
        return []
    data = resp.json()

    if isinstance(data, dict):
        error_msg = data.get("Error Message") or data.get("error") or data.get("message")
        if error_msg:
            quota_keywords = ("limit reached", "too many request", "quota", "upgrade", "subscribe")
            quota_error = any(kw in str(error_msg).lower() for kw in quota_keywords)
            _set_fmp_deferral_reason("provider_refusal")
            details = {
                "endpoint": endpoint,
                "http_status": int(resp.status_code),
                "error_type": "api_error",
                "quota_error": quota_error,
            }
            record_provider_event("fmp", "provider_refusal", details)
            record_event("fmp_request", endpoint, "provider_error", details)
            latch_service_issue("fmp_provider_error", details)
            record_input_gap(
                symbol,
                endpoint,
                "provider_error_response",
                coverage_status="failed",
            )
            if quota_error:
                _fmp_quota_exhausted = True
                _fmp_session_failure = {"reason": "provider_refusal", **details}
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
    *,
    retry_failed_chunks: bool = True,
) -> pd.DataFrame:
    """Download close prices for many tickers in batches via Alpaca.

    Returns:
        DataFrame with DatetimeIndex and one column per ticker (float close prices).

    When ``retry_failed_chunks`` is False, the first failed or empty batch aborts
    the download and discards partial data so callers cannot rank an incomplete
    universe or trigger recursive request splitting. A nonempty batch response
    can still omit individual symbols; callers that require complete universe
    coverage must compare returned columns with the requested symbols and report
    missing coverage explicitly.

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
                if not retry_failed_chunks:
                    print("  Bulk download aborted because retries are disabled.")
                    return pd.DataFrame()
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
            if not retry_failed_chunks:
                print("  Bulk download aborted because batch retries are disabled.")
                return pd.DataFrame()
            if len(chunk) > 1:
                retry_size = max(1, len(chunk) // 2)
                print(f"  Retrying failed batch in groups of {retry_size}.")
                recovered = fetch_bulk_close_prices(
                    chunk,
                    period=period,
                    chunk_size=retry_size,
                    retry_failed_chunks=retry_failed_chunks,
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


def validate_tickers_bulk(
    symbols: List[str],
    *,
    retry_failed_chunks: bool = True,
) -> List[str]:
    """Check which tickers are valid using a bulk request to minimize API calls."""
    df = fetch_bulk_close_prices(
        symbols,
        period="5d",
        retry_failed_chunks=retry_failed_chunks,
    )
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

_FMP_FINANCIAL_FRAME_CACHE_VERSION = "fiscal-revision-v2"


def _fmp_period_end(record: dict) -> pd.Timestamp | None:
    """Normalize one fiscal period to its local, date-only representation."""
    raw_period = record.get("date")
    if not raw_period:
        return None
    try:
        timestamp = pd.Timestamp(raw_period)
    except (TypeError, ValueError, OverflowError):
        return None
    if pd.isna(timestamp):
        return None
    return pd.Timestamp(timestamp.date())


def _fmp_revision_timestamp(record: dict) -> pd.Timestamp | None:
    """Return the best provider ordering timestamp for a visible revision."""
    for field in ("acceptedDate", "filingDate", "fillingDate", "filedDate"):
        raw_value = record.get(field)
        if not raw_value:
            continue
        try:
            timestamp = pd.Timestamp(raw_value)
        except (TypeError, ValueError, OverflowError):
            continue
        if pd.isna(timestamp):
            continue
        if timestamp.tzinfo is not None:
            timestamp = timestamp.tz_convert("UTC").tz_localize(None)
        return timestamp
    return None


def _fmp_mapped_values_equal(left: dict, right: dict, field_map: dict) -> bool:
    """Compare only the financial values consumed by the target frame."""
    for key in field_map:
        left_value = left.get(key)
        right_value = right.get(key)
        try:
            left_missing = bool(pd.isna(left_value))
        except (TypeError, ValueError):
            left_missing = False
        try:
            right_missing = bool(pd.isna(right_value))
        except (TypeError, ValueError):
            right_missing = False
        if left_missing or right_missing:
            if not (left_missing and right_missing):
                return False
            continue
        try:
            if not bool(left_value == right_value):
                return False
        except (TypeError, ValueError):
            return False
    return True


def _select_fmp_visible_revision(records: List[dict], field_map: dict) -> dict | None:
    """Choose one same-period amendment or fail closed when order is unknowable."""
    if len(records) == 1:
        return records[0]
    if all(_fmp_mapped_values_equal(records[0], record, field_map) for record in records[1:]):
        return records[0]

    dated = [(_fmp_revision_timestamp(record), record) for record in records]
    if any(timestamp is None for timestamp, _record in dated):
        return None
    latest_timestamp = max(timestamp for timestamp, _record in dated if timestamp is not None)
    latest = [record for timestamp, record in dated if timestamp == latest_timestamp]
    if not all(_fmp_mapped_values_equal(latest[0], record, field_map) for record in latest[1:]):
        return None
    return latest[0]


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

    records_by_date: Dict[pd.Timestamp, List[dict]] = {}
    for rec in records:
        period_end = _fmp_period_end(rec)
        if period_end is None:
            continue
        records_by_date.setdefault(period_end, []).append(rec)

    data_by_date: Dict[pd.Timestamp, Dict[str, float]] = {}
    for period_end, revisions in records_by_date.items():
        rec = _select_fmp_visible_revision(revisions, field_map)
        if rec is None:
            continue
        row_data = {}
        for fmp_key, label in field_map.items():
            val = rec.get(fmp_key)
            if val is not None:
                row_data[label] = val
        if row_data:
            data_by_date[period_end] = row_data

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
    cache_key = (
        "quarterly_income",
        _FMP_FINANCIAL_FRAME_CACHE_VERSION,
        symbol,
        limit,
    )
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
    cache_key = (
        "annual_income",
        _FMP_FINANCIAL_FRAME_CACHE_VERSION,
        symbol,
        limit,
    )
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
    cache_key = (
        "balance_sheet",
        _FMP_FINANCIAL_FRAME_CACHE_VERSION,
        symbol,
        limit,
    )
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

    # Keep free-tier live scoring to the three statement endpoints. Missing
    # shares and institutional inputs already have neutral/redistributed scoring.
    if _is_fmp_free_plan():
        _cache_set(cache_key, result)
        return result

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

    # 2. Current stable Positions Summary snapshot. It includes the previous
    # holder count, so live scans need only one period-specific API call.
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
    profile_raw: list = []
    inst_ownership_raw: list = []
    if not _is_fmp_free_plan():
        try:
            profile_raw = _fmp_get("profile", {"symbol": symbol})
        except (requests.RequestException, ValueError, EnvironmentError):
            profile_raw = []

        # Institutional ownership history for PIT backtesting. The provider adds a
        # conservative assumed acceptedDate after the Form 13F reporting lag.
        inst_ownership_raw = _fetch_inst_ownership_history(
            symbol,
            fmp_get_fn=_fmp_get,
            limit=settings.FMP_INSTITUTIONAL_BACKTEST_LIMIT,
        )

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
    """Keep only records publicly filed or accepted by *as_of_date*.

    Returns records sorted newest-first so that ``filtered[0]`` is always the
    most recent record available as of the cutoff date.
    """
    cutoff = pd.Timestamp(as_of_date).date()
    dated: list[tuple[pd.Timestamp, dict]] = []
    for rec in records:
        public_timestamp = _fmp_revision_timestamp(rec)
        if public_timestamp is not None and public_timestamp.date() <= cutoff:
            dated.append((public_timestamp, rec))

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
            ownership_pct = latest.get("ownership_percent")
            if ownership_pct is not None:
                try:
                    result["held_percent_institutions"] = min(float(ownership_pct) / 100.0, 1.0)
                except (TypeError, ValueError):
                    pass
            investors = latest.get("institution_count")
            if investors is not None:
                result["institution_count"] = int(investors)
            prev_investors = latest.get("prev_institution_count")
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
    cache_key = (
        "fundamentals_as_of",
        _FMP_FINANCIAL_FRAME_CACHE_VERSION,
        symbol,
        as_of_date.strftime("%Y-%m-%d"),
    )
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
