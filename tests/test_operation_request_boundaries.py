"""Offline regressions for operation limits at physical HTTP boundaries."""

from unittest.mock import Mock
from types import SimpleNamespace

import pytest

from core import data_client, index_ticker_fetcher
from core.operation_limits import OperationCapExceeded, activate_operation_budget


class _DeniedBudget:
    def __init__(self, category: str) -> None:
        self.category = category
        self.calls: list[str] = []
        self.manifest = SimpleNamespace(
            operation="observer",
            scope={"symbols": ["SPY"]},
            dimensions={
                "index_requests_per_scan": 1,
                "fmp_requests_per_symbol": 1,
                "scan_cycles": 1,
            },
            deadlines_seconds={"request_timeout_seconds": 15},
        )

    def consume(self, dimension: str) -> None:
        self.calls.append(dimension)

    def consume_symbol_dimension(
        self, dimension: str, symbol: str, *, multiplier: int = 1
    ) -> None:
        self.calls.append(f"{dimension}:{symbol}")

    def reserve_http(self, category: str) -> None:
        self.calls.append(category)
        if category == self.category:
            raise OperationCapExceeded(f"{category} cap exceeded")


def test_fmp_budget_denial_precedes_physical_session_get(monkeypatch) -> None:
    session = Mock()
    budget = _DeniedBudget("fmp_read")
    monkeypatch.setattr(data_client, "_fmp_session", session)
    monkeypatch.setattr(data_client, "_fmp_api_key", lambda: "offline-key")
    monkeypatch.setattr(data_client, "_reserve_fmp_request", lambda: True)

    with activate_operation_budget(budget):
        with pytest.raises(OperationCapExceeded, match="fmp_read"):
            data_client._fmp_get("profile", {"symbol": "SPY"})

    session.get.assert_not_called()
    assert budget.calls[-1] == "fmp_read"


def test_index_budget_denial_precedes_physical_requests_get(monkeypatch) -> None:
    request = Mock()
    budget = _DeniedBudget("index_read")
    monkeypatch.setattr(index_ticker_fetcher.requests, "get", request)

    with activate_operation_budget(budget):
        with pytest.raises(OperationCapExceeded, match="index_read"):
            index_ticker_fetcher._index_get("offline", "https://example.test/index")

    request.assert_not_called()
    assert budget.calls[-1] == "index_read"
