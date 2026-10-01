"""Tests for bounded Alpaca HTTP calls."""

from types import SimpleNamespace
from unittest.mock import Mock

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
