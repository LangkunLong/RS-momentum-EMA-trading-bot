"""Tests for bounded Alpaca HTTP calls."""

from types import SimpleNamespace
from unittest.mock import Mock
from datetime import datetime, timedelta

import pytest

from config import settings
from core import alpaca_client_policy
from core.alpaca_client_policy import (
    AlpacaHttpRequestBudgetExceeded,
    alpaca_http_request_budget,
    configure_alpaca_rest_client,
    single_attempt_alpaca_requests,
)
from core.scheduler_observation import SchedulerObservation, activate_scheduler_observation


def _tiny_observer_limits() -> dict[str, object]:
    from core.operation_limits import (
        COMMON_HTTP_CATEGORIES,
        OBSERVER_DEADLINES,
        OBSERVER_DIMENSIONS,
    )

    now = datetime.now().astimezone()
    dimensions = {name: 2 for name in OBSERVER_DIMENSIONS}
    dimensions.update(
        {
            "scheduler_ticks": 2,
            "clock_reads": 3,
            "scan_cycles": 1,
            "exit_check_cycles": 0,
            "symbols_per_scan": 1,
            "market_data_pages_per_symbol": 2,
            "market_trend_pages_per_scan": 1,
            "fmp_requests_per_symbol": 1,
            "index_requests_per_scan": 0,
            "stream_connections": 1,
            "stream_events": 1,
            "receipt_events": 20,
            "output_bytes": 4096,
        }
    )
    deadlines = {name: 30.0 for name in OBSERVER_DEADLINES}
    deadlines.update({"request_timeout_seconds": 1.0, "session_seconds": 30.0})
    categories = {name: 0 for name in COMMON_HTTP_CATEGORIES}
    categories.update(
        {"account_read": 1, "clock_read": 3, "market_data_read": 3, "fmp_read": 1}
    )
    return {
        "schema": "paper-operation-limits/v1",
        "operation": "observer",
        "admission_status": "admitted",
        "admission_ref": "offline-test-approval",
        "binding": {
            "source_commit": "4" * 40,
            "runtime_id": "offline-test-runtime",
            "store_id": "offline-test-store",
            "lockfile_sha256": "a" * 64,
            "account_id": "paper-test-account",
            "allowance_owner": "offline-test-owner",
            "allowance_remaining_http_attempts": 100,
            "session_start": (now - timedelta(seconds=1)).isoformat(),
            "session_stop": (now + timedelta(seconds=29)).isoformat(),
        },
        "scope": {"symbols": ["SPY"], "market_trend_symbol": "SPY"},
        "dimensions": dimensions,
        "deadlines_seconds": deadlines,
        "http_category_limits": categories,
    }


def test_alpaca_http_budget_counts_pages_and_adds_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ALPACA_SDK_RETRY_ATTEMPTS", 0)
    monkeypatch.setattr(settings, "ALPACA_SDK_RETRY_WAIT_SECONDS", 0)
    monkeypatch.setattr(settings, "ALPACA_HTTP_TIMEOUT_SECONDS", 15)
    request = Mock(return_value="response")
    client = SimpleNamespace(
        _retry=3,
        _retry_wait=3,
        _session=SimpleNamespace(request=request),
    )
    configure_alpaca_rest_client(client)

    assert client._retry == 0
    assert client._retry_wait == 0
    with alpaca_http_request_budget(1):
        assert client._session.request("GET", "https://example.test/page") == "response"
        with pytest.raises(AlpacaHttpRequestBudgetExceeded):
            client._session.request("GET", "https://example.test/next-page")

    request.assert_called_once_with(
        "GET",
        "https://example.test/page",
        timeout=15,
    )


def test_failed_alpaca_attempt_and_caught_cap_denial_are_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ALPACA_SDK_RETRY_ATTEMPTS", 0)
    monkeypatch.setattr(settings, "ALPACA_SDK_RETRY_WAIT_SECONDS", 0)
    request = Mock(side_effect=TimeoutError("offline"))
    client = SimpleNamespace(_retry=3, _retry_wait=3, _session=SimpleNamespace(request=request))
    observation = SchedulerObservation("test")

    with alpaca_http_request_budget(1), activate_scheduler_observation(observation):
        configure_alpaca_rest_client(client)
        with pytest.raises(TimeoutError):
            client._session.request("GET", "/one")
        with pytest.raises(AlpacaHttpRequestBudgetExceeded):
            client._session.request("GET", "/two")
        snapshot = alpaca_client_policy.alpaca_http_request_snapshot()

    assert snapshot == {"attempts": 1, "cap_denials": 1, "cap": 1, "sdk_retries": 0}
    assert observation.to_receipt()["resource_denials"] == {"alpaca_cap_denied": 1}


def test_single_attempt_policy_restores_configured_retry_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ALPACA_SDK_RETRY_ATTEMPTS", 3)
    monkeypatch.setattr(settings, "ALPACA_SDK_RETRY_WAIT_SECONDS", 3)

    with single_attempt_alpaca_requests():
        assert settings.ALPACA_SDK_RETRY_ATTEMPTS == 0
        assert settings.ALPACA_SDK_RETRY_WAIT_SECONDS == 0

    assert settings.ALPACA_SDK_RETRY_ATTEMPTS == 3
    assert settings.ALPACA_SDK_RETRY_WAIT_SECONDS == 3

def test_operation_http_cap_is_reserved_before_physical_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.operation_limits import (
        OperationCapExceeded,
        activate_operation_budget,
    )

    class OneRequestBudget:
        def __init__(self) -> None:
            self.categories: list[str] = []
            self.manifest = SimpleNamespace(
                operation="observer",
                scope={"symbols": ["SPY"]},
                dimensions={},
                deadlines_seconds={"request_timeout_seconds": 15},
            )

        def reserve_http(self, category: str) -> None:
            self.categories.append(category)
            if len(self.categories) > 1:
                raise OperationCapExceeded("account_read cap exceeded")

    monkeypatch.setattr(settings, "ALPACA_SDK_RETRY_ATTEMPTS", 0)
    monkeypatch.setattr(settings, "ALPACA_SDK_RETRY_WAIT_SECONDS", 0)
    request = Mock(return_value="response")
    client = SimpleNamespace(
        _retry=0,
        _retry_wait=0,
        _session=SimpleNamespace(request=request),
    )
    configure_alpaca_rest_client(client)
    budget = OneRequestBudget()

    with activate_operation_budget(budget):
        assert client._session.request(
            "GET", "https://paper-api.alpaca.markets/v2/account"
        ) == "response"
        with pytest.raises(OperationCapExceeded, match="account_read"):
            client._session.request(
                "GET", "https://paper-api.alpaca.markets/v2/account"
            )

    request.assert_called_once()
    assert budget.categories == ["account_read", "account_read"]


def test_operation_request_counts_one_physical_attempt_and_disables_retry_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib3.util.retry import Retry

    from core.operation_limits import (
        OperationBudget,
        OperationManifest,
        activate_operation_budget,
    )

    monkeypatch.setattr(settings, "ALPACA_SDK_RETRY_ATTEMPTS", 3)
    monkeypatch.setattr(settings, "ALPACA_SDK_RETRY_WAIT_SECONDS", 3)
    monkeypatch.setattr(settings, "ALPACA_HTTP_TIMEOUT_SECONDS", 15)
    initial_retries = Retry(total=3, connect=3, read=3, redirect=3, status=3)
    adapter = SimpleNamespace(max_retries=initial_retries)
    request = Mock(side_effect=TimeoutError("offline mock"))
    session = SimpleNamespace(
        request=request,
        get_adapter=Mock(return_value=adapter),
    )

    class RetryingSdkClient:
        def __init__(self) -> None:
            self._retry = 3
            self._retry_wait = 3
            self._session = session

        def _request(self, method: str, url: str) -> object:
            for attempt in range(self._retry + 1):
                try:
                    return self._session.request(method, url)
                except TimeoutError:
                    if attempt >= self._retry:
                        raise
            raise AssertionError("SDK retry loop unexpectedly exhausted")

    client = RetryingSdkClient()
    configure_alpaca_rest_client(client)
    budget = OperationBudget(OperationManifest.from_mapping(_tiny_observer_limits()))

    with activate_operation_budget(budget):
        with pytest.raises(TimeoutError, match="offline mock"):
            client._request("GET", "https://paper-api.alpaca.markets/v2/account")

    request.assert_called_once()
    sent_kwargs = request.call_args.kwargs
    assert sent_kwargs["allow_redirects"] is False
    assert sent_kwargs["timeout"] == 1.0
    assert adapter.max_retries is initial_retries
    assert budget.snapshot()["http_attempts"] == 1
    assert budget.snapshot()["http_category_attempts"]["account_read"] == 1
    assert client._retry == 3
    assert client._retry_wait == 3
