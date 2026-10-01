"""Apply bounded HTTP settings to Alpaca REST clients."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from collections.abc import Iterator
from typing import Any

from config import settings
from core.scheduler_observation import record_resource_denial


class AlpacaHttpRequestBudgetExceeded(RuntimeError):
    """Raised before an Alpaca HTTP request would exceed its active cap."""


_budget_lock = threading.Lock()
_remaining_requests: int | None = None
_configured_cap: int | None = None
_attempts = 0
_cap_denials = 0


@contextmanager
def alpaca_http_request_budget(max_requests: int) -> Iterator[None]:
    """Limit all configured Alpaca HTTP attempts, including SDK pagination."""
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
    """Disable Alpaca SDK retries for a bounded provider probe or dry run."""
    previous_attempts = settings.ALPACA_SDK_RETRY_ATTEMPTS
    previous_wait = settings.ALPACA_SDK_RETRY_WAIT_SECONDS
    settings.ALPACA_SDK_RETRY_ATTEMPTS = 0
    settings.ALPACA_SDK_RETRY_WAIT_SECONDS = 0
    try:
        yield
    finally:
        settings.ALPACA_SDK_RETRY_ATTEMPTS = previous_attempts
        settings.ALPACA_SDK_RETRY_WAIT_SECONDS = previous_wait


def _reserve_request() -> None:
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

    if not getattr(client, "_codex_timeout_wrapped", False):
        session = client._session
        original_request = session.request

        def request_with_timeout(*args: Any, **kwargs: Any) -> Any:
            _reserve_request()
            kwargs.setdefault("timeout", settings.ALPACA_HTTP_TIMEOUT_SECONDS)
            return original_request(*args, **kwargs)

        session.request = request_with_timeout
        client._codex_timeout_wrapped = True

    return client
