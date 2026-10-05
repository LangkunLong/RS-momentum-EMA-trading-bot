"""Apply finite request limits to Alpaca REST clients."""

from __future__ import annotations

import json
import math
import re
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any
from urllib.parse import parse_qs, urlsplit

from config import settings
from core.operation_limits import (
    current_operation_budget,
    is_market_trend_data_request,
)
from core.scheduler_observation import record_resource_denial


class AlpacaHttpRequestBudgetExceeded(RuntimeError):
    """Raised before an Alpaca HTTP request would exceed its active cap."""


_budget_lock = threading.Lock()
_alpaca_operation_request_lock = threading.RLock()
_remaining_requests: int | None = None
_configured_cap: int | None = None
_attempts = 0
_cap_denials = 0


@contextmanager
def alpaca_http_request_budget(max_requests: int) -> Iterator[None]:
    """Limit configured Alpaca HTTP attempts, including SDK pagination."""
    global _remaining_requests, _configured_cap, _attempts, _cap_denials
    safe_limit = max(0, int(max_requests))
    with _budget_lock:
        if _remaining_requests is not None:
            raise RuntimeError("An Alpaca HTTP request budget is already active")
        _remaining_requests = safe_limit
        _configured_cap = safe_limit
        _attempts = 0
        _cap_denials = 0
    try:
        yield
    finally:
        with _budget_lock:
            _remaining_requests = None


@contextmanager
def single_attempt_alpaca_requests() -> Iterator[None]:
    """Disable Alpaca SDK retries for one bounded operation."""
    previous_attempts = settings.ALPACA_SDK_RETRY_ATTEMPTS
    previous_wait = settings.ALPACA_SDK_RETRY_WAIT_SECONDS
    settings.ALPACA_SDK_RETRY_ATTEMPTS = 0
    settings.ALPACA_SDK_RETRY_WAIT_SECONDS = 0
    try:
        yield
    finally:
        settings.ALPACA_SDK_RETRY_ATTEMPTS = previous_attempts
        settings.ALPACA_SDK_RETRY_WAIT_SECONDS = previous_wait


def classify_alpaca_request(
    method: str,
    url: str,
    kwargs: Mapping[str, Any] | None = None,
) -> str:
    """Map a physical REST request to one finite contract category.

    Unknown paths remain explicitly unclassified so an admitted operation
    rejects them instead of silently spending an unrelated allowance.
    """
    verb = str(method).upper()
    path = urlsplit(str(url)).path.rstrip("/").lower()
    options = kwargs or {}
    query = parse_qs(urlsplit(str(url)).query)
    params = options.get("params")
    if isinstance(params, Mapping):
        for key, value in params.items():
            query[str(key).lower()] = [str(value)]
    status = ",".join(query.get("status", [])).lower().split(".")[-1]

    if verb == "GET" and path.endswith("/account"):
        return "account_read"
    if verb == "GET" and path.endswith("/clock"):
        return "clock_read"
    if verb == "GET" and (path.endswith("/positions") or "/positions/" in path):
        return "position_read"
    if verb == "GET" and path.endswith("/orders:by_client_order_id"):
        return "exact_order_lookup"
    if path.endswith("/orders"):
        if verb == "GET":
            return "closed_order_read" if status.endswith("closed") else "open_order_read"
        if verb == "POST":
            body = _request_body(options)
            side = _enum_text(body.get("side"))
            order_type = _enum_text(body.get("type"))
            if side == "buy":
                return "entry_submit"
            if side == "sell" and "stop" in order_type:
                return "stop_submit"
            if side == "sell":
                return "exit_submit"
        return "unclassified_http"

    if "/orders/" in path:
        if verb == "GET":
            return "exact_order_lookup"
        if verb == "DELETE":
            return "order_cancel"
        if verb in {"PATCH", "PUT"}:
            return "stop_replace"
        return "unclassified_http"

    if verb == "GET" and (
        "/stocks/" in path
        or "/crypto/" in path
        or "/options/" in path
        or "/forex/" in path
    ):
        return "market_data_read"
    return "unclassified_http"


def _request_body(kwargs: Mapping[str, Any]) -> Mapping[str, Any]:
    body = kwargs.get("json", kwargs.get("data"))
    if isinstance(body, Mapping):
        return body
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError:
            return {}
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


def _enum_text(value: object) -> str:
    return str(value or "").split(".")[-1].strip().lower()


def _reserve_legacy_request() -> None:
    global _remaining_requests, _attempts, _cap_denials
    with _budget_lock:
        if _remaining_requests is None:
            return
        if _remaining_requests <= 0:
            _cap_denials += 1
            denied = True
        else:
            _remaining_requests -= 1
            _attempts += 1
            return

    if denied:
        record_resource_denial(
            "alpaca_cap_denied",
            {"attempt": _attempts + 1, "cap": _configured_cap},
        )
        raise AlpacaHttpRequestBudgetExceeded(
            "Alpaca HTTP request limit reached; stopping the bounded run"
        )


def _reserve_request(method: str, url: str, kwargs: Mapping[str, Any]) -> None:
    """Reserve local and manifest counters before crossing the HTTP boundary."""
    _reserve_legacy_request()
    budget = current_operation_budget()
    if budget is not None:
        category = classify_alpaca_request(method, url, kwargs)
        _validate_operation_request_scope(budget, category, url, kwargs)
        if category == "exact_order_lookup" and budget.manifest.operation == "lifecycle":
            client_order_id = _alpaca_client_order_id(url, kwargs)
            if client_order_id:
                budget.consume_client_order_lookup(client_order_id)
            else:
                order_id = urlsplit(str(url)).path.rstrip("/").rsplit("/", 1)[-1]
                budget.consume_order_attempt("lookup", order_id)
        if category == "clock_read" and "clock_reads" in budget.manifest.dimensions:
            budget.consume("clock_reads")
        if category in {"open_order_read", "closed_order_read"} and (
            "order_pages" in budget.manifest.dimensions
        ):
            if budget.manifest.operation == "lifecycle":
                requested_limit = _alpaca_request_integer_param(url, kwargs, "limit")
                page_limit = budget.manifest.dimensions["order_page_size"]
                if requested_limit is None or requested_limit < 1 or requested_limit > page_limit:
                    budget.reject_incomplete(
                        "order_page_size",
                        f"order page limit must be explicit and no greater than {page_limit}",
                    )
            budget.consume("order_pages")
        if category == "market_data_read":
            if "market_data_pages" in budget.manifest.dimensions:
                budget.consume("market_data_pages")
            elif "market_data_pages_per_symbol" in budget.manifest.dimensions:
                symbols = _alpaca_request_symbols(url, kwargs)
                if not symbols:
                    budget.reject_incomplete(
                        "market_data_pages_per_symbol",
                        "market-data request has no attributable symbol",
                    )
                if is_market_trend_data_request():
                    trend_symbol = str(
                        budget.manifest.scope["market_trend_symbol"]
                    ).strip().upper()
                    if symbols != [trend_symbol]:
                        budget.reject_incomplete(
                            "market_trend_pages_per_scan",
                            "benchmark fetch does not match the bound trend symbol",
                        )
                    budget.consume_scaled(
                        "market_trend_pages_per_scan",
                        multiplier=budget.manifest.dimensions["scan_cycles"],
                    )
                else:
                    allowed = {
                        str(item).strip().upper()
                        for item in budget.manifest.scope["symbols"]
                    }
                    if any(symbol not in allowed for symbol in symbols):
                        budget.reject_incomplete(
                            "market_data_read",
                            "market-data request is outside explicit observer symbol scope",
                        )
                    for symbol in symbols:
                        budget.consume_symbol_dimension(
                            "market_data_pages_per_symbol",
                            symbol,
                            multiplier=budget.manifest.dimensions["scan_cycles"],
                        )
        budget.reserve_http(category)


def _alpaca_request_symbols(url: str, kwargs: Mapping[str, Any]) -> list[str]:
    query = parse_qs(urlsplit(str(url)).query)
    params = kwargs.get("params")
    values: list[object] = []
    if isinstance(params, Mapping):
        values.extend(
            params.get(name)
            for name in ("symbol", "symbols", "symbol_or_symbols")
            if name in params
        )
    elif params is not None:
        values.extend(
            getattr(params, name)
            for name in ("symbol", "symbols", "symbol_or_symbols")
            if getattr(params, name, None) is not None
        )
    values.extend(query.get("symbol", []))
    values.extend(query.get("symbols", []))
    pieces: list[str] = []
    for value in values:
        if isinstance(value, (list, tuple, set)):
            pieces.extend(str(item) for item in value)
        else:
            pieces.extend(str(value).split(","))
    if not pieces:
        parts = [part for part in urlsplit(str(url)).path.split("/") if part]
        try:
            stock_index = [part.lower() for part in parts].index("stocks")
            candidate = parts[stock_index + 1]
            if candidate.lower() not in {"bars", "quotes", "trades", "snapshots", "latest"}:
                pieces.append(candidate)
        except (ValueError, IndexError):
            pass
    return list(dict.fromkeys(item.strip().upper() for item in pieces if item.strip()))


def _alpaca_client_order_id(url: str, kwargs: Mapping[str, Any]) -> str:
    """Extract a single exact client-order identity from query parameters."""
    values = parse_qs(urlsplit(str(url)).query).get("client_order_id", [])
    params = kwargs.get("params")
    if isinstance(params, Mapping) and "client_order_id" in params:
        value = params["client_order_id"]
        if isinstance(value, (list, tuple)):
            values.extend(str(item) for item in value)
        elif value is not None:
            values.append(str(value))
    unique = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
    return unique[0] if len(unique) == 1 else ""


def _alpaca_request_integer_param(
    url: str,
    kwargs: Mapping[str, Any],
    name: str,
) -> int | None:
    """Read one unambiguous integer query value from URL or request params."""
    values = parse_qs(urlsplit(str(url)).query).get(name.lower(), [])
    params = kwargs.get("params")
    if isinstance(params, Mapping):
        value = params.get(name)
        if isinstance(value, (list, tuple)):
            values.extend(str(item) for item in value)
        elif value is not None:
            values.append(str(value))
    unique = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
    if len(unique) != 1:
        return None
    try:
        return int(unique[0])
    except ValueError:
        return None


def _validate_operation_request_scope(
    budget: Any,
    category: str,
    url: str,
    kwargs: Mapping[str, Any],
) -> None:
    """Reject any request whose identity cannot be proven inside the manifest."""
    manifest = budget.manifest
    if category == "unclassified_http":
        budget.reject_incomplete(category, "request path is not assigned a contract category")
    if manifest.operation == "observer" and category == "market_data_read":
        symbols = _alpaca_request_symbols(url, kwargs)
        allowed = {str(item).strip().upper() for item in manifest.scope["symbols"]}
        trend_symbol = str(manifest.scope["market_trend_symbol"]).strip().upper()
        if is_market_trend_data_request():
            if symbols != [trend_symbol]:
                budget.reject_incomplete(
                    "market_trend_pages_per_scan",
                    "benchmark fetch does not match the bound trend symbol",
                )
            return
        if not symbols or any(symbol not in allowed for symbol in symbols):
            budget.reject_incomplete(
                "market_data_read",
                "market-data request is outside explicit observer symbol scope",
            )
    if manifest.operation != "lifecycle" or category not in {
        "entry_submit",
        "stop_submit",
        "exit_submit",
        "stop_replace",
        "order_cancel",
        "exact_order_lookup",
    }:
        return

    path = urlsplit(str(url)).path.rstrip("/")
    order_id = path.rsplit("/", 1)[-1] if "/orders/" in path.lower() else ""
    if category == "exact_order_lookup" and path.lower().endswith(
        "/orders:by_client_order_id"
    ):
        client_order_id = _alpaca_client_order_id(url, kwargs)
        symbol = str(manifest.scope["symbol"]).strip().upper()
        symbol_part = re.escape(symbol.lower().replace("/", "")[:6])
        pattern = (
            rf"cslm-{symbol_part}-\d{{14}}-[0-9a-f]{{6}}"
            rf"(?:-exit|-sl(?:-[0-9a-f]{{1,8}})?)?"
        )
        if not re.fullmatch(pattern, client_order_id, flags=re.IGNORECASE):
            budget.reject_incomplete(
                category,
                "client-order lookup identity is missing or outside the lifecycle symbol scope",
            )
        return
    if category in {"stop_replace", "order_cancel", "exact_order_lookup"}:
        if not order_id or not budget.order_id_is_known(order_id):
            budget.reject_incomplete(
                category,
                "request references an order identity not admitted by the lifecycle",
            )
        return

    body = _request_body(kwargs)
    symbol = str(body.get("symbol", "")).strip().upper()
    expected_symbol = str(manifest.scope["symbol"]).strip().upper()
    if not symbol or symbol != expected_symbol:
        budget.reject_incomplete(
            category,
            f"order symbol must be the manifest-scoped {expected_symbol}",
        )
    try:
        qty = float(body.get("qty", 0))
    except (TypeError, ValueError):
        qty = math.nan
    if not math.isfinite(qty) or qty <= 0 or qty > float(manifest.scope["quantity"]):
        budget.reject_incomplete(category, "order quantity exceeds the manifest scope")
    if category == "entry_submit":
        try:
            price = float(body.get("limit_price", body.get("notional", 0)))
        except (TypeError, ValueError):
            price = math.nan
        notional = price * qty if "notional" not in body else price
        if (
            not math.isfinite(notional)
            or notional <= 0
            or notional > float(manifest.scope["max_entry_notional"])
        ):
            budget.reject_incomplete(
                category,
                "entry order notional exceeds or cannot be proven under the manifest",
            )


def alpaca_http_request_snapshot() -> dict[str, int | None]:
    """Return counters and retry policy for the current or most recent budget."""
    with _budget_lock:
        return {
            "attempts": _attempts,
            "cap_denials": _cap_denials,
            "cap": _configured_cap,
            "sdk_retries": max(0, int(settings.ALPACA_SDK_RETRY_ATTEMPTS)),
        }


def configure_alpaca_rest_client(client: Any) -> Any:
    """Set configured retry policy and a finite timeout on an Alpaca client."""
    client._retry = max(0, int(settings.ALPACA_SDK_RETRY_ATTEMPTS))
    client._retry_wait = max(0, int(settings.ALPACA_SDK_RETRY_WAIT_SECONDS))

    if not getattr(client, "_codex_retry_wrapped", False):
        original_sdk_request = getattr(client, "_request", None)
        if callable(original_sdk_request):
            def request_without_operation_retries(*args: Any, **kwargs: Any) -> Any:
                if current_operation_budget() is None:
                    return original_sdk_request(*args, **kwargs)
                with _alpaca_operation_request_lock:
                    previous_retry = client._retry
                    previous_wait = client._retry_wait
                    client._retry = 0
                    client._retry_wait = 0
                    try:
                        return original_sdk_request(*args, **kwargs)
                    finally:
                        client._retry = previous_retry
                        client._retry_wait = previous_wait

            client._request = request_without_operation_retries
            client._codex_retry_wrapped = True

    if not getattr(client, "_codex_timeout_wrapped", False):
        session = client._session
        original_request = session.request

        def request_with_timeout(method: str, url: str, *args: Any, **kwargs: Any) -> Any:
            _reserve_request(method, url, kwargs)
            timeout = settings.ALPACA_HTTP_TIMEOUT_SECONDS
            budget = current_operation_budget()
            if budget is not None:
                timeout = min(
                    timeout,
                    budget.manifest.deadlines_seconds["request_timeout_seconds"],
                )
            kwargs.setdefault("timeout", timeout)
            if budget is None:
                return original_request(method, url, *args, **kwargs)

            kwargs["allow_redirects"] = False
            get_adapter = getattr(session, "get_adapter", None)
            if not callable(get_adapter):
                return original_request(method, url, *args, **kwargs)
            from urllib3.util.retry import Retry

            with _alpaca_operation_request_lock:
                adapter = get_adapter(url)
                previous_retries = adapter.max_retries
                adapter.max_retries = Retry(
                    total=0,
                    connect=0,
                    read=0,
                    redirect=0,
                    status=0,
                )
                try:
                    return original_request(method, url, *args, **kwargs)
                finally:
                    adapter.max_retries = previous_retries

        session.request = request_with_timeout
        client._codex_timeout_wrapped = True

    return client
