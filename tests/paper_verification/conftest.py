"""Keep the historical/paper fixtures fully offline in ordinary repository runs."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3

import pytest


@pytest.fixture(autouse=True)
def _offline_boundaries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from core import notifier, order_execution, order_manager
    from core.execution_workflow import clear_workflow_registry
    from core.pit_data import PITDataBundle
    import tests.paper_policy_fixtures as feature_fixtures

    clear_workflow_registry()
    unexpected_broker_calls: list[str] = []

    def forbidden_client(*args, **kwargs):
        unexpected_broker_calls.append("unmocked broker client")
        raise AssertionError("offline test reached an unmocked broker client")

    def fake_notification(*args, **kwargs):
        return True

    monkeypatch.setattr(order_execution, "_get_trading_client", forbidden_client)
    monkeypatch.setattr(order_manager, "_get_trading_client", forbidden_client)
    for module in (notifier, order_manager):
        for name in ("notify_buy_filled", "notify_entry_submitted", "notify_sell_filled"):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, fake_notification)

    def owned_synthetic_bundle(path, *, expected_sha256, prices_provenance=None):
        resolved = Path(path).resolve(strict=True)
        fixture_root = tmp_path.resolve(strict=True)
        if not resolved.is_relative_to(fixture_root) or not resolved.is_file():
            raise AssertionError("synthetic PIT database must be created by this test")
        actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
        if actual != expected_sha256:
            raise AssertionError("synthetic PIT database digest mismatch")
        bundle = PITDataBundle.__new__(PITDataBundle)
        bundle.path, bundle.sha256 = resolved, actual
        bundle._initialize_connection(
            sqlite3.connect(str(resolved)), prices_provenance=prices_provenance
        )
        return bundle

    monkeypatch.setattr(feature_fixtures, "PITDataBundle", owned_synthetic_bundle)
    yield
    clear_workflow_registry()
    assert not unexpected_broker_calls, unexpected_broker_calls
