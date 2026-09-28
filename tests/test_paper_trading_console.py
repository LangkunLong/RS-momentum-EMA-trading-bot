"""Tests for the paper-trading deployment and observation console."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import paper_trading_console as console


def test_doctor_fails_when_not_in_paper_mode() -> None:
    checks = [console.ReadinessCheck("Configuration", console.EvidenceStatus.FAIL, "paper mode is false")]
    with patch("paper_trading_console._collect_readiness_checks", return_value=checks):
        assert console.run_doctor() == 1


def test_doctor_does_not_claim_readiness_when_old_checks_are_clean(capsys) -> None:
    checks = [
        console.ReadinessCheck("Configuration", console.EvidenceStatus.PASS, "paper mode configured"),
        console.ReadinessCheck("Alpaca connectivity", console.EvidenceStatus.UNVERIFIED, "probe not requested"),
        console.ReadinessCheck("FMP statement entitlement", console.EvidenceStatus.UNVERIFIED, "probe not requested"),
        console.ReadinessCheck("FMP price entitlement", console.EvidenceStatus.UNVERIFIED, "probe not requested"),
        console.ReadinessCheck("Service health", console.EvidenceStatus.UNVERIFIED, "not observed"),
        console.ReadinessCheck("Strategy dry run", console.EvidenceStatus.UNVERIFIED, "not observed"),
    ]
    with patch("paper_trading_console._collect_readiness_checks", return_value=checks):
        assert console.run_doctor() == 2

    output = capsys.readouterr().out
    assert "Paper deployment is ready" not in output
    assert "[UNVERIFIED]" in output
    assert "Readiness is incomplete" in output


def test_default_doctor_does_not_contact_provider_endpoints() -> None:
    with (
        patch("paper_trading_console._runtime_identity_record", return_value={}),
        patch("paper_trading_console._check_execution_store_read_only", return_value=console.ReadinessCheck("Persistence", console.EvidenceStatus.UNVERIFIED, "absent")),
        patch("paper_trading_console._check_scheduler_installation", return_value=console.ReadinessCheck("Scheduler", console.EvidenceStatus.UNVERIFIED, "unknown")),
        patch("paper_trading_console._check_external_access") as external_check,
    ):
        console.run_doctor()

    external_check.assert_not_called()


def test_external_provider_probes_are_fixed_and_cover_strategy_fmp_inputs(tmp_path: Path) -> None:
    calls: list[tuple[str, dict]] = []
    positions_calls = 0
    orders_calls = 0

    def fake_fmp_get(endpoint: str, params: dict) -> list[dict[str, object]]:
        calls.append((endpoint, params))
        if endpoint == "income-statement":
            return [{
                "symbol": "AAPL", "date": "2026-06-30", "revenue": 100,
                "epsDiluted": 2.5, "netIncome": 50,
            }]
        if endpoint == "balance-sheet-statement":
            return [{"symbol": "AAPL", "date": "2026-06-30", "totalStockholdersEquity": 500}]
        return [{"symbol": "AAPL", "date": "2026-09-25", "close": 250.0}]

    def read_positions() -> list[object]:
        nonlocal positions_calls
        positions_calls += 1
        return []

    def read_orders() -> list[object]:
        nonlocal orders_calls
        orders_calls += 1
        return []

    with patch.object(console.settings, "EXECUTION_STORE_DB_PATH", str(tmp_path / "missing.sqlite3")):
        checks = console._check_external_access(
            alpaca_account_reader=lambda: SimpleNamespace(id="account-123"),
            alpaca_positions_reader=read_positions,
            alpaca_orders_reader=read_orders,
            fmp_get=fake_fmp_get,
            paper_mode=True,
            fmp_key_present=True,
            fmp_ledger_ready=True,
            symbol="AAPL",
            today=date(2026, 9, 27),
        )

    assert [check.name for check in checks] == [
        "Alpaca connectivity", "Paper account identity", "Broker positions",
        "Broker open orders", "Broker/local reconciliation", "FMP quarterly income",
        "FMP annual income", "FMP annual balance sheet", "FMP price entitlement",
    ]
    assert [check.status for check in checks[5:]] == [console.EvidenceStatus.PASS] * 4
    assert checks[4].status is console.EvidenceStatus.UNVERIFIED
    assert checks[2].status is console.EvidenceStatus.PASS
    assert checks[3].status is console.EvidenceStatus.PASS
    assert checks[0].name == "Alpaca connectivity"
    assert checks[1].name == "Paper account identity"
    assert "account-123" not in checks[1].detail
    assert "sha256:" in checks[1].detail
    assert positions_calls == 1
    assert orders_calls == 1
    assert calls == [
        ("income-statement", {"symbol": "AAPL", "period": "quarter", "limit": 5}),
        ("income-statement", {"symbol": "AAPL", "period": "annual", "limit": 5}),
        ("balance-sheet-statement", {"symbol": "AAPL", "limit": 5}),
        ("historical-price-eod/full", {"symbol": "AAPL", "from": "2026-09-17", "to": "2026-09-27"}),
    ]


def test_external_probe_refuses_all_provider_reads_when_paper_mode_is_false() -> None:
    account_reader = MagicMock(side_effect=AssertionError("live account probe must not run"))
    positions_reader = MagicMock(side_effect=AssertionError("live positions read must not run"))
    orders_reader = MagicMock(side_effect=AssertionError("live open-orders read must not run"))
    fmp_get = MagicMock(side_effect=AssertionError("FMP read must not run when paper mode is false"))
    checks = console._check_external_access(
        alpaca_account_reader=account_reader,
        alpaca_positions_reader=positions_reader,
        alpaca_orders_reader=orders_reader,
        fmp_get=fmp_get,
        paper_mode=False,
        fmp_key_present=True,
        fmp_ledger_ready=True,
        today=date(2026, 9, 27),
    )

    assert checks[0].status is console.EvidenceStatus.FAIL
    assert checks[1].status is console.EvidenceStatus.UNVERIFIED
    assert all(check.status is console.EvidenceStatus.UNVERIFIED for check in checks[2:])
    account_reader.assert_not_called()
    positions_reader.assert_not_called()
    orders_reader.assert_not_called()
    fmp_get.assert_not_called()


def test_doctor_wiring_refuses_every_provider_when_paper_mode_is_false() -> None:
    account_reader = MagicMock(side_effect=AssertionError("account call must be blocked"))
    positions_reader = MagicMock(side_effect=AssertionError("positions call must be blocked"))
    orders_reader = MagicMock(side_effect=AssertionError("open-orders call must be blocked"))
    fmp_get = MagicMock(side_effect=AssertionError("FMP call must be blocked"))
    with (
        patch("paper_trading_console._is_paper_mode", return_value=False),
        patch("paper_trading_console._get_trading_client", return_value=SimpleNamespace(get_account=account_reader)),
        patch("paper_trading_console.get_open_positions", positions_reader),
        patch("paper_trading_console.get_open_orders", orders_reader),
        patch("paper_trading_console._fmp_get", fmp_get),
        patch("paper_trading_console._check_execution_store_binding", return_value=console.ReadinessCheck("Execution store binding", console.EvidenceStatus.UNVERIFIED, "unknown")),
        patch("paper_trading_console._check_execution_store_read_only", return_value=console.ReadinessCheck("Persistence", console.EvidenceStatus.UNVERIFIED, "unknown")),
        patch("paper_trading_console._check_scheduler_installation", return_value=console.ReadinessCheck("Scheduler installation", console.EvidenceStatus.UNVERIFIED, "unknown")),
        patch("paper_trading_console._runtime_identity_record", return_value={}),
    ):
        console._collect_readiness_checks(probe_external=True)

    account_reader.assert_not_called()
    positions_reader.assert_not_called()
    orders_reader.assert_not_called()
    fmp_get.assert_not_called()


def test_external_probe_skips_broker_inventory_when_account_read_fails(tmp_path: Path) -> None:
    positions_reader = MagicMock(side_effect=AssertionError("inventory reads require connected account"))
    orders_reader = MagicMock(side_effect=AssertionError("inventory reads require connected account"))
    fmp_get = MagicMock(return_value=[])
    with patch.object(console.settings, "EXECUTION_STORE_DB_PATH", str(tmp_path / "missing.sqlite3")):
        checks = console._check_external_access(
            alpaca_account_reader=MagicMock(side_effect=TimeoutError),
            alpaca_positions_reader=positions_reader,
            alpaca_orders_reader=orders_reader,
            fmp_get=fmp_get,
            paper_mode=True,
            fmp_key_present=True,
            fmp_ledger_ready=True,
            today=date(2026, 9, 27),
        )

    assert checks[0].status is console.EvidenceStatus.FAIL
    assert all(check.status is console.EvidenceStatus.UNVERIFIED for check in checks[2:5])
    positions_reader.assert_not_called()
    orders_reader.assert_not_called()
    assert fmp_get.call_count == 4


def test_empty_fmp_probe_does_not_claim_endpoint_entitlement() -> None:
    checks = console._check_external_access(
        alpaca_account_reader=lambda: SimpleNamespace(id="account-123"),
        alpaca_positions_reader=lambda: [],
        alpaca_orders_reader=lambda: [],
        fmp_get=lambda endpoint, params: [],
        paper_mode=True,
        fmp_key_present=True,
        fmp_ledger_ready=True,
        today=date(2026, 9, 27),
    )

    assert all(check.status is console.EvidenceStatus.UNVERIFIED for check in checks[5:])
    assert "not established" in checks[5].detail


def test_missing_fmp_ledger_blocks_all_fmp_calls() -> None:
    fmp_get = MagicMock(side_effect=AssertionError("must not create or call without existing ledger"))
    checks = console._check_external_access(
        alpaca_account_reader=lambda: SimpleNamespace(id="account-123"),
        alpaca_positions_reader=lambda: [],
        alpaca_orders_reader=lambda: [],
        fmp_get=fmp_get,
        paper_mode=True,
        fmp_key_present=True,
        fmp_ledger_ready=False,
    )

    assert all(check.status is console.EvidenceStatus.UNVERIFIED for check in checks[5:])
    fmp_get.assert_not_called()


def _create_reconciliation_store(path: Path, *, position: tuple[str, str, float] | None = None) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE workflow_snapshots (
                workflow_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, state TEXT,
                broker_order_id TEXT NOT NULL DEFAULT '', entry_plan_json TEXT,
                created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL
            );
            CREATE TABLE workflow_transitions (
                id INTEGER PRIMARY KEY, timestamp_utc TEXT NOT NULL, workflow_id TEXT NOT NULL,
                symbol TEXT NOT NULL, from_state TEXT, to_state TEXT NOT NULL,
                event TEXT NOT NULL, details_json TEXT NOT NULL
            );
            CREATE TABLE workflow_order_refs (
                id INTEGER PRIMARY KEY, workflow_id TEXT NOT NULL, symbol TEXT NOT NULL,
                broker_order_id TEXT NOT NULL DEFAULT '', client_order_id TEXT NOT NULL DEFAULT '',
                order_role TEXT NOT NULL, created_at_utc TEXT NOT NULL
            );
            CREATE TABLE active_positions (
                symbol TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, qty REAL NOT NULL,
                entry_price REAL NOT NULL, opened_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL
            );
            """
        )
        if position:
            symbol, workflow_id, qty = position
            connection.execute(
                "INSERT INTO active_positions VALUES (?, ?, ?, 100, 'now', 'now')",
                (symbol, workflow_id, qty),
            )
            connection.execute(
                "INSERT INTO workflow_snapshots VALUES (?, ?, 'protective_stop_active', '', NULL, 'now', 'now')",
                (workflow_id, symbol),
            )
            connection.execute(
                "INSERT INTO workflow_order_refs VALUES (1, ?, ?, 'order-1', 'client-1', 'protective_stop', 'now')",
                (workflow_id, symbol),
            )
            connection.execute(
                """
                INSERT INTO workflow_transitions
                    (id, timestamp_utc, workflow_id, symbol, from_state, to_state, event, details_json)
                VALUES (1, 'now', ?, ?, 'buy_fill_received', 'protective_stop_active',
                    'protective_stop_reconciled', ?)
                """,
                (
                    workflow_id,
                    symbol,
                    json.dumps({
                        "success": True,
                        "stop_order_id": "order-1",
                        "client_order_id": "client-1",
                        "stop_price": 90,
                    }),
                ),
            )


def test_broker_reconciliation_never_passes_without_store(tmp_path: Path, monkeypatch) -> None:
    missing_store = tmp_path / "missing.sqlite3"
    monkeypatch.setenv("EXECUTION_STORE_DB_PATH", str(missing_store))
    monkeypatch.setattr(console.settings, "EXECUTION_STORE_DB_PATH", str(missing_store))

    _, _, reconciliation = console._check_broker_inventory(
        positions_reader=lambda: [],
        orders_reader=lambda: [],
        paper_mode=True,
        account_identity_verified=True,
        store_ready=True,
    )

    assert reconciliation.status is console.EvidenceStatus.UNVERIFIED


def test_broker_reconciliation_never_passes_when_inventory_read_fails(tmp_path: Path, monkeypatch) -> None:
    store = tmp_path / "execution.sqlite3"
    _create_reconciliation_store(store)
    monkeypatch.setenv("EXECUTION_STORE_DB_PATH", str(store))
    monkeypatch.setattr(console.settings, "EXECUTION_STORE_DB_PATH", str(store))

    positions, orders, reconciliation = console._check_broker_inventory(
        positions_reader=lambda: (_ for _ in ()).throw(TimeoutError()),
        orders_reader=lambda: [],
        paper_mode=True,
        account_identity_verified=True,
        store_ready=True,
    )

    assert positions.status is console.EvidenceStatus.FAIL
    assert orders.status is console.EvidenceStatus.PASS
    assert reconciliation.status is console.EvidenceStatus.UNVERIFIED


def test_open_orders_at_fixed_cap_cannot_prove_complete_inventory() -> None:
    _, orders, reconciliation = console._check_broker_inventory(
        positions_reader=lambda: [],
        orders_reader=lambda: [SimpleNamespace()] * 2,
        paper_mode=True,
        account_identity_verified=True,
        open_orders_limit=2,
    )

    assert orders.status is console.EvidenceStatus.UNVERIFIED
    assert "full inventory is unknown" in orders.detail
    assert reconciliation.status is console.EvidenceStatus.UNVERIFIED


def test_broker_reconciliation_requires_owned_workflow_stop_of_matching_remaining_qty(
    tmp_path: Path, monkeypatch
) -> None:
    store = tmp_path / "execution.sqlite3"
    _create_reconciliation_store(store, position=("AAPL", "wf-1", 2))
    monkeypatch.setenv("EXECUTION_STORE_DB_PATH", str(store))
    monkeypatch.setattr(console.settings, "EXECUTION_STORE_DB_PATH", str(store))
    before = store.read_bytes()
    position = SimpleNamespace(symbol="AAPL", qty=2, avg_entry_price=100)
    stop = SimpleNamespace(
        symbol="AAPL", id="order-1", client_order_id="client-1", side="sell", type="stop",
        qty="2.5", filled_qty="0.5", status="new", time_in_force="gtc",
    )

    positions, orders, reconciliation = console._check_broker_inventory(
        positions_reader=lambda: [position],
        orders_reader=lambda: [stop],
        paper_mode=True,
        account_identity_verified=True,
        store_ready=True,
    )

    assert positions.status is console.EvidenceStatus.PASS
    assert orders.status is console.EvidenceStatus.PASS
    assert reconciliation.status is console.EvidenceStatus.PASS
    assert store.read_bytes() == before


def test_broker_reconciliation_fails_for_wrong_workflow_state_or_orphan_stop(
    tmp_path: Path, monkeypatch
) -> None:
    store = tmp_path / "execution.sqlite3"
    _create_reconciliation_store(store, position=("AAPL", "wf-1", 2))
    with sqlite3.connect(store) as connection:
        connection.execute("UPDATE workflow_snapshots SET state='protective_stop_failed'")
    monkeypatch.setenv("EXECUTION_STORE_DB_PATH", str(store))
    monkeypatch.setattr(console.settings, "EXECUTION_STORE_DB_PATH", str(store))
    stop = SimpleNamespace(
        symbol="AAPL", id="order-1", client_order_id="client-1", side="sell", type="stop",
        qty="2", filled_qty="0", status="new", time_in_force="gtc",
    )

    _, _, reconciliation = console._check_broker_inventory(
        positions_reader=lambda: [SimpleNamespace(symbol="AAPL", qty=2)],
        orders_reader=lambda: [stop],
        paper_mode=True,
        account_identity_verified=True,
        store_ready=True,
    )

    assert reconciliation.status is console.EvidenceStatus.FAIL
    assert "workflow_state:AAPL" in reconciliation.detail


@pytest.mark.parametrize(
    ("workflow_state", "notification_sent"),
    [
        ("buy_fill_notification_pending", None),
        ("buy_fill_notification_sent", True),
        ("buy_fill_notification_failed", False),
    ],
)
def test_broker_reconciliation_accepts_notification_state_after_durable_stop(
    tmp_path: Path,
    monkeypatch,
    workflow_state: str,
    notification_sent: bool | None,
) -> None:
    store = tmp_path / "execution.sqlite3"
    _create_reconciliation_store(store, position=("AAPL", "wf-1", 2))
    with sqlite3.connect(store) as connection:
        connection.execute("UPDATE workflow_snapshots SET state=?", (workflow_state,))
        connection.execute(
            """
            INSERT INTO workflow_transitions
                (id, timestamp_utc, workflow_id, symbol, from_state, to_state, event, details_json)
            VALUES (2, 'later', 'wf-1', 'AAPL', 'protective_stop_active',
                'buy_fill_notification_pending', 'buy_fill_notification_claimed', '{"channel":"email"}')
            """
        )
        if notification_sent is not None:
            connection.execute(
                """
                INSERT INTO workflow_transitions
                    (id, timestamp_utc, workflow_id, symbol, from_state, to_state, event, details_json)
                VALUES (3, 'later', 'wf-1', 'AAPL', 'buy_fill_notification_pending', ?, 'buy_fill_notified', ?)
                """,
                (
                    workflow_state,
                    json.dumps({"channel": "email", "sent": notification_sent}),
                ),
            )
    monkeypatch.setenv("EXECUTION_STORE_DB_PATH", str(store))
    monkeypatch.setattr(console.settings, "EXECUTION_STORE_DB_PATH", str(store))
    stop = SimpleNamespace(
        symbol="AAPL", id="order-1", client_order_id="client-1", side="sell", type="stop",
        qty="2", filled_qty="0", status="new", time_in_force="gtc",
    )

    _, _, reconciliation = console._check_broker_inventory(
        positions_reader=lambda: [SimpleNamespace(symbol="AAPL", qty=2)],
        orders_reader=lambda: [stop],
        paper_mode=True,
        account_identity_verified=True,
        store_ready=True,
    )

    assert reconciliation.status is console.EvidenceStatus.PASS


def test_broker_reconciliation_requires_durable_stop_transition_matching_current_stop(
    tmp_path: Path, monkeypatch
) -> None:
    store = tmp_path / "execution.sqlite3"
    _create_reconciliation_store(store, position=("AAPL", "wf-1", 2))
    with sqlite3.connect(store) as connection:
        connection.execute(
            "UPDATE workflow_snapshots SET state='buy_fill_notification_sent'"
        )
        connection.execute(
            "UPDATE workflow_transitions SET details_json=? WHERE event='protective_stop_reconciled'",
            (json.dumps({"success": True, "stop_order_id": "old-order", "client_order_id": "old-client"}),),
        )
    monkeypatch.setenv("EXECUTION_STORE_DB_PATH", str(store))
    monkeypatch.setattr(console.settings, "EXECUTION_STORE_DB_PATH", str(store))
    stop = SimpleNamespace(
        symbol="AAPL", id="order-1", client_order_id="client-1", side="sell", type="stop",
        qty="2", filled_qty="0", status="new", time_in_force="gtc",
    )

    _, _, reconciliation = console._check_broker_inventory(
        positions_reader=lambda: [SimpleNamespace(symbol="AAPL", qty=2)],
        orders_reader=lambda: [stop],
        paper_mode=True,
        account_identity_verified=True,
        store_ready=True,
    )

    assert reconciliation.status is console.EvidenceStatus.FAIL
    assert "protective_stop_history:AAPL" in reconciliation.detail


def test_broker_reconciliation_rejects_owned_open_sell_alongside_protective_stop(
    tmp_path: Path, monkeypatch
) -> None:
    store = tmp_path / "execution.sqlite3"
    _create_reconciliation_store(store, position=("AAPL", "wf-1", 2))
    with sqlite3.connect(store) as connection:
        connection.execute(
            "INSERT INTO workflow_order_refs VALUES (2, 'wf-1', 'AAPL', 'exit-1', 'wf-1-exit', 'exit_order', 'now')"
        )
    monkeypatch.setenv("EXECUTION_STORE_DB_PATH", str(store))
    monkeypatch.setattr(console.settings, "EXECUTION_STORE_DB_PATH", str(store))
    stop = SimpleNamespace(
        symbol="AAPL", id="order-1", client_order_id="client-1", side="sell", type="stop",
        qty="2", filled_qty="0", status="new", time_in_force="gtc",
    )
    exit_order = SimpleNamespace(
        symbol="AAPL", id="exit-1", client_order_id="wf-1-exit", side="sell", type="limit",
        qty="2", filled_qty="0", status="new", time_in_force="day",
    )

    _, _, reconciliation = console._check_broker_inventory(
        positions_reader=lambda: [SimpleNamespace(symbol="AAPL", qty=2)],
        orders_reader=lambda: [stop, exit_order],
        paper_mode=True,
        account_identity_verified=True,
        store_ready=True,
    )

    assert reconciliation.status is console.EvidenceStatus.FAIL
    assert "open_sell_order:AAPL" in reconciliation.detail


@pytest.mark.parametrize(
    ("workflow_state", "expected_status"),
    [
        ("signal_accepted", console.EvidenceStatus.FAIL),
        ("order_submitted", console.EvidenceStatus.PASS),
    ],
)
def test_broker_reconciliation_checks_open_entry_workflow_state(
    tmp_path: Path,
    monkeypatch,
    workflow_state: str,
    expected_status: console.EvidenceStatus,
) -> None:
    store = tmp_path / "execution.sqlite3"
    _create_reconciliation_store(store)
    with sqlite3.connect(store) as connection:
        connection.execute(
            "INSERT INTO workflow_snapshots VALUES ('wf-entry', 'MSFT', ?, '', NULL, 'now', 'now')",
            (workflow_state,),
        )
        connection.execute(
            "INSERT INTO workflow_order_refs VALUES (1, 'wf-entry', 'MSFT', 'entry-1', 'wf-entry', 'entry_order', 'now')"
        )
    monkeypatch.setenv("EXECUTION_STORE_DB_PATH", str(store))
    monkeypatch.setattr(console.settings, "EXECUTION_STORE_DB_PATH", str(store))
    entry_order = SimpleNamespace(
        symbol="MSFT", id="entry-1", client_order_id="wf-entry", side="buy", type="limit",
        qty="2", filled_qty="0", status="new", time_in_force="day",
    )

    _, _, reconciliation = console._check_broker_inventory(
        positions_reader=lambda: [],
        orders_reader=lambda: [entry_order],
        paper_mode=True,
        account_identity_verified=True,
        store_ready=True,
    )

    assert reconciliation.status is expected_status
    if expected_status is console.EvidenceStatus.FAIL:
        assert "entry_order_state:MSFT" in reconciliation.detail


def test_execution_store_binding_requires_absolute_existing_env_path(tmp_path: Path, monkeypatch) -> None:
    store = tmp_path / "execution.sqlite3"
    store.touch()
    monkeypatch.setattr(console.settings, "EXECUTION_STORE_DB_PATH", str(store))
    monkeypatch.delenv("EXECUTION_STORE_DB_PATH", raising=False)
    assert console._check_execution_store_binding().status is console.EvidenceStatus.UNVERIFIED

    monkeypatch.setenv("EXECUTION_STORE_DB_PATH", str(store))
    assert console._check_execution_store_binding().status is console.EvidenceStatus.PASS


def test_default_readiness_collection_preserves_existing_ledger_and_cache(tmp_path: Path, monkeypatch) -> None:
    store = tmp_path / "execution.sqlite3"
    ledger = tmp_path / "fmp_request_usage.json"
    cache = tmp_path / "fundamentals"
    ledger.write_text('{"window_start":"fixed","count":17}', encoding="utf-8")
    cache.mkdir()
    cached_sample = cache / "existing.pkl"
    cached_sample.write_bytes(b"existing-cache-bytes")
    ledger_before = ledger.read_bytes()
    cache_before = cached_sample.read_bytes()

    monkeypatch.delenv("EXECUTION_STORE_DB_PATH", raising=False)
    with (
        patch.object(console.settings, "EXECUTION_STORE_DB_PATH", str(store)),
        patch.object(console.settings, "FMP_REQUEST_LEDGER_PATH", str(ledger)),
        patch.object(console.settings, "FUNDAMENTALS_CACHE_DIR", str(cache)),
        patch("paper_trading_console._check_scheduler_installation", return_value=console.ReadinessCheck("Scheduler installation", console.EvidenceStatus.UNVERIFIED, "unknown")),
    ):
        checks = console._collect_readiness_checks(probe_external=False)

    binding = next(check for check in checks if check.name == "Execution store binding")
    assert binding.status is console.EvidenceStatus.UNVERIFIED
    assert ledger.read_bytes() == ledger_before
    assert cached_sample.read_bytes() == cache_before
    assert not store.exists()


def test_external_probe_does_not_run_broker_inventory_without_account_identity() -> None:
    positions_reader = MagicMock(return_value=[])
    orders_reader = MagicMock(return_value=[])
    checks = console._check_external_access(
        alpaca_account_reader=lambda: SimpleNamespace(),
        alpaca_positions_reader=positions_reader,
        alpaca_orders_reader=orders_reader,
        fmp_get=lambda endpoint, params: [],
        paper_mode=True,
        fmp_key_present=False,
        fmp_ledger_ready=False,
    )

    assert checks[1].status is console.EvidenceStatus.UNVERIFIED
    assert checks[2].status is console.EvidenceStatus.UNVERIFIED
    assert checks[3].status is console.EvidenceStatus.UNVERIFIED
    assert checks[4].status is console.EvidenceStatus.UNVERIFIED
    positions_reader.assert_not_called()
    orders_reader.assert_not_called()


def test_read_only_persistence_check_does_not_create_missing_store(tmp_path: Path) -> None:
    missing_store = tmp_path / "not-created" / "execution.sqlite3"
    with patch.object(console.settings, "EXECUTION_STORE_DB_PATH", str(missing_store)):
        check = console._check_execution_store_read_only()

    assert check.status is console.EvidenceStatus.UNVERIFIED
    assert not missing_store.exists()


def test_read_only_persistence_check_accepts_existing_schema_without_changing_bytes(tmp_path: Path) -> None:
    store = tmp_path / "execution.sqlite3"
    with sqlite3.connect(store) as connection:
        for table in ("workflow_snapshots", "workflow_transitions", "workflow_order_refs", "active_positions"):
            connection.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
    before = store.read_bytes()

    with patch.object(console.settings, "EXECUTION_STORE_DB_PATH", str(store)):
        check = console._check_execution_store_read_only()

    assert check.status is console.EvidenceStatus.PASS
    assert store.read_bytes() == before


def test_legacy_path_checks_leave_missing_directories_uncreated(tmp_path: Path) -> None:
    store = tmp_path / "new" / "execution.sqlite3"
    scans = tmp_path / "new" / "scans"
    with (
        patch.object(console.settings, "EXECUTION_STORE_DB_PATH", str(store)),
        patch.object(console, "SCAN_RESULTS_DIR", scans),
    ):
        console._check_execution_store_path()
        console._check_scan_results_dir()

    assert not store.parent.exists()
    assert not scans.exists()


def test_readiness_summary_returns_incomplete_for_unverified_evidence(capsys) -> None:
    result = console._print_readiness_report(
        "PAPER TRADING READINESS",
        [console.ReadinessCheck("Dry run", console.EvidenceStatus.UNVERIFIED, "not observed")],
    )

    output = capsys.readouterr().out
    assert result == 2
    assert "[UNVERIFIED] Dry run: not observed" in output
    assert "does not authorize order submission" in output


def test_runtime_identity_uses_shared_names_and_keeps_strategy_identity_separate() -> None:
    record = console._runtime_identity_record(account_fingerprint="sha256:0123456789abcdef")
    runtime = record["runtime_identity"]

    assert record["source_revision"]
    assert {
        "checkout_path",
        "interpreter_path",
        "interpreter_version",
        "dependency_profile",
        "configuration_profile",
        "persistent_store_path",
        "fmp_request_ledger_path",
        "fmp_cache_path",
        "paper_environment",
    }.issubset(runtime)
    assert runtime["paper_environment"]["account_fingerprint"] == "sha256:0123456789abcdef"
    assert "policy_digest" not in runtime
    assert "feature_contract" not in runtime


def test_runtime_identity_binds_dependency_lock_hash() -> None:
    record = console._runtime_identity_record()
    profile = record["runtime_identity"]["dependency_profile"]

    assert profile.get("lock_manifest") == "requirements-lock.txt"
    expected = hashlib.sha256((console.PROJECT_DIR / "requirements-lock.txt").read_bytes()).hexdigest()
    assert profile.get("lock_manifest_sha256") == expected


def test_absent_dependency_manifest_has_explicit_unavailable_hash(tmp_path: Path) -> None:
    with patch.object(console, "PROJECT_DIR", tmp_path):
        record = console._runtime_identity_record()

    profile = record["runtime_identity"]["dependency_profile"]
    assert profile.get("manifest_sha256") == "unavailable"
    assert profile.get("lock_manifest_sha256") == "unavailable"


def test_external_account_fingerprint_is_bound_into_runtime_identity() -> None:
    external = [
        console.ReadinessCheck("Alpaca connectivity", console.EvidenceStatus.PASS, "connected"),
        console.ReadinessCheck("Paper account identity", console.EvidenceStatus.PASS, "paper account fingerprint=sha256:0123456789abcdef"),
        console.ReadinessCheck("FMP statement entitlement", console.EvidenceStatus.PASS, "sample"),
        console.ReadinessCheck("FMP price entitlement", console.EvidenceStatus.PASS, "sample"),
    ]
    with (
        patch("paper_trading_console._check_external_access", return_value=external),
        patch("paper_trading_console._check_execution_store_read_only", return_value=console.ReadinessCheck("Persistence", console.EvidenceStatus.UNVERIFIED, "absent")),
        patch("paper_trading_console._check_scheduler_installation", return_value=console.ReadinessCheck("Scheduler", console.EvidenceStatus.UNVERIFIED, "unknown")),
    ):
        checks = console._collect_readiness_checks(probe_external=True)

    identity = json.loads(next(check.detail for check in checks if check.name == "Runtime identity").split(";", 1)[0])
    assert identity["runtime_identity"]["paper_environment"]["account_fingerprint"] == "sha256:0123456789abcdef"


def test_task_registration_does_not_assert_service_health() -> None:
    with (
        patch("paper_trading_console._schtasks", return_value=(0, "Status: Running")),
        patch("paper_trading_console._runtime_identity_record", return_value={}),
        patch("paper_trading_console._check_execution_store_read_only", return_value=console.ReadinessCheck("Persistence", console.EvidenceStatus.UNVERIFIED, "absent")),
    ):
        checks = console._collect_readiness_checks(probe_external=False)

    scheduler = next(check for check in checks if check.name == "Scheduler installation")
    service = next(check for check in checks if check.name == "Service health")
    assert scheduler.status is console.EvidenceStatus.PASS
    assert service.status is console.EvidenceStatus.UNVERIFIED


def test_main_only_enables_provider_probes_after_explicit_flag() -> None:
    with patch("paper_trading_console.run_doctor", return_value=2) as run_doctor:
        assert console.main(["doctor"]) == 2
        run_doctor.assert_called_once_with(probe_external=False)

    with patch("paper_trading_console.run_doctor", return_value=2) as run_doctor:
        assert console.main(["doctor", "--probe-external"]) == 2
        run_doctor.assert_called_once_with(probe_external=True)


def test_checklist_returns_warning_only_success(capsys) -> None:
    readiness = [console.ReadinessCheck("Dry run", console.EvidenceStatus.UNVERIFIED, "not run")]
    with (
        patch("paper_trading_console._collect_readiness_checks", return_value=readiness),
        patch("paper_trading_console._check_recent_signal_quality", return_value=console.CheckResult("Signals", True, "no actionable buys", severity="warn")),
    ):
        rc = console.run_checklist(limit=5)

    assert rc == 2
    output = capsys.readouterr().out
    assert "PAPER TRADING CHECKLIST" in output
    assert "[UNVERIFIED] Dry run: not run" in output
    assert "[UNVERIFIED] Signals: no actionable buys" in output
    assert "Safe for supervised paper testing" not in output


def test_checklist_fails_on_hard_failure(capsys) -> None:
    with (
        patch("paper_trading_console._collect_readiness_checks", return_value=[console.ReadinessCheck("Configuration", console.EvidenceStatus.FAIL, "missing setting")]),
        patch("paper_trading_console._check_recent_signal_quality", return_value=console.CheckResult("Signals", True, "ok")),
    ):
        rc = console.run_checklist(limit=5)

    assert rc == 1
    output = capsys.readouterr().out
    assert "[FAIL] Configuration: missing setting" in output
    assert "does not authorize order submission" in output


def test_checklist_fails_for_any_not_ok_result_even_with_default_severity(capsys) -> None:
    """The summary must agree with a row rendered as FAIL."""
    with (
        patch("paper_trading_console._collect_readiness_checks", return_value=[console.ReadinessCheck("Paper mode", console.EvidenceStatus.FAIL, "live mode")]),
        patch("paper_trading_console._check_recent_signal_quality", return_value=console.CheckResult("Signals", True, "ok")),
    ):
        rc = console.run_checklist(limit=5)

    assert rc == 1
    output = capsys.readouterr().out
    assert "[FAIL] Paper mode: live mode" in output
    assert "does not authorize order submission" in output


def test_status_reads_recent_workflows_and_latest_scan(tmp_path: Path, capsys) -> None:
    scan_dir = tmp_path / "scan_results"
    scan_dir.mkdir()
    latest_scan = scan_dir / "canslim_scan_test.csv"
    pd.DataFrame(
        [
            {"Symbol": "NVDA", "Scanner_Category": "actionable_buy", "RS_Score": 95, "CANSLIM_Score": 82, "Scanner_Notes": ""},
            {"Symbol": "AMD", "Scanner_Category": "watchlist_candidate", "RS_Score": 88, "CANSLIM_Score": 71, "Scanner_Notes": "market_not_bullish"},
        ]
    ).to_csv(latest_scan, index=False)

    account = SimpleNamespace(equity="100000", buying_power="200000")
    clock = SimpleNamespace(is_open=True)
    client = MagicMock()
    client.get_account.return_value = account
    client.get_clock.return_value = clock

    workflow_rows = [
        {
            "workflow_id": "wf-1",
            "symbol": "NVDA",
            "state": "order_submitted",
            "broker_order_id": "broker-1",
            "entry_plan": {"qty": 20.0, "entry_price": 500.0},
            "created_at_utc": "2026-04-17T09:31:00Z",
            "updated_at_utc": "2026-04-17T09:31:10Z",
        }
    ]

    with (
        patch("paper_trading_console._is_paper_mode", return_value=True),
        patch("paper_trading_console._get_trading_client", return_value=client),
        patch("paper_trading_console.get_open_positions", return_value=[SimpleNamespace(symbol="NVDA", qty=20.0, avg_entry_price=500.0, current_price=505.0, unrealized_pl_pct=0.01)]),
        patch("paper_trading_console.get_open_orders", return_value=[SimpleNamespace(symbol="NVDA", side="buy", type="limit", qty="20", client_order_id="wf-1")]),
        patch("paper_trading_console.get_execution_store") as mock_store_factory,
        patch("paper_trading_console.SCAN_RESULTS_DIR", scan_dir),
        patch("paper_trading_console.SCHEDULER_LOG", tmp_path / "scheduler_log.txt"),
    ):
        mock_store_factory.return_value.list_recent_workflows.return_value = workflow_rows
        result = console.print_status(limit=5)

    assert result == 0
    output = capsys.readouterr().out
    assert "PAPER TRADING STATUS" in output
    assert "NVDA" in output
    assert "Actionable buys: 1" in output
    assert "Recent execution workflows: 1" in output
    assert "Execution shortlist capacity this cycle" in output
    assert "Already active, so skipped: NVDA" in output


def test_execution_shortlist_mirrors_live_slot_and_skip_rules() -> None:
    actionable = pd.DataFrame(
        [
            {
                "Symbol": "MSFT",
                "RS_Score": 99,
                "CANSLIM_Score": 94,
                "Has_Volume_Surge": True,
                "Current_Growth": 55,
                "Annual_Growth": 31,
                "Proximity_to_High": 0.99,
            },
            {
                "Symbol": "NVDA",
                "RS_Score": 97,
                "CANSLIM_Score": 92,
                "Has_Volume_Surge": True,
                "Current_Growth": 48,
                "Annual_Growth": 29,
                "Proximity_to_High": 0.98,
            },
            {
                "Symbol": "AAPL",
                "RS_Score": 93,
                "CANSLIM_Score": 88,
                "Has_Volume_Surge": False,
                "Current_Growth": 35,
                "Annual_Growth": 22,
                "Proximity_to_High": 0.96,
            },
            {
                "Symbol": "AMD",
                "RS_Score": 91,
                "CANSLIM_Score": 85,
                "Has_Volume_Surge": True,
                "Current_Growth": 28,
                "Annual_Growth": 20,
                "Proximity_to_High": 0.95,
            },
        ]
    )

    shortlisted, deprioritized, skipped_active, execution_slots = console._compute_execution_shortlist_from_scan(
        actionable=actionable,
        held_symbols={"NVDA"},
        open_order_symbols={"NVDA", "TSLA"},
        pending_entry_symbols={"TSLA"},
        max_new_entries=2,
        max_open_positions=3,
    )

    assert execution_slots == 1
    assert [row["symbol"] for row in shortlisted] == ["MSFT"]
    assert [row["symbol"] for row in deprioritized] == ["AAPL", "AMD"]
    assert skipped_active == ["NVDA"]


def test_recent_signal_quality_reports_top_executable_setups(tmp_path: Path) -> None:
    scan_dir = tmp_path / "scan_results"
    scan_dir.mkdir()
    latest_scan = scan_dir / "canslim_scan_test.csv"
    pd.DataFrame(
        [
            {"Symbol": "MSFT", "Scanner_Category": "actionable_buy", "RS_Score": 99, "CANSLIM_Score": 94, "Has_Volume_Surge": True},
            {"Symbol": "AAPL", "Scanner_Category": "actionable_buy", "RS_Score": 93, "CANSLIM_Score": 88, "Has_Volume_Surge": False},
            {"Symbol": "AMD", "Scanner_Category": "watchlist_candidate", "RS_Score": 87, "CANSLIM_Score": 72, "Has_Volume_Surge": False},
        ]
    ).to_csv(latest_scan, index=False)

    with patch("paper_trading_console.SCAN_RESULTS_DIR", scan_dir):
        result = console._check_recent_signal_quality(limit=5)

    assert result.ok is True
    assert result.severity == "ok"
    assert "actionable=2" in result.detail
    assert "watchlist=1" in result.detail
    assert "top=MSFT, AAPL" in result.detail


def test_main_run_now_defaults_to_dry_run() -> None:
    with patch("paper_trading_console.run_auto_trader") as mock_run:
        rc = console.main(["run-now"])

    assert rc == 0
    mock_run.assert_called_once_with(dry_run=True)


def test_main_run_now_preserves_explicit_dry_run_flag() -> None:
    with patch("paper_trading_console.run_auto_trader") as mock_run:
        rc = console.main(["run-now", "--dry-run"])

    assert rc == 0
    mock_run.assert_called_once_with(dry_run=True)


def test_main_run_now_refuses_orders_and_directs_to_canonical_scheduler(capsys) -> None:
    with patch("paper_trading_console.run_auto_trader") as mock_run:
        with pytest.raises(SystemExit) as exc_info:
            console.main(["run-now", "--enable-orders"])

    assert exc_info.value.code == 2
    mock_run.assert_not_called()
    assert "python scheduler.py --enable-orders --now" in capsys.readouterr().err


def test_main_install_task_delegates_to_setup() -> None:
    with patch("paper_trading_console.register_task", return_value=0) as mock_register:
        rc = console.main(["install-task"])

    assert rc == 0
    mock_register.assert_called_once_with(dry_run=True)


def test_main_checklist_delegates_to_checklist_runner() -> None:
    with patch("paper_trading_console.run_checklist", return_value=0) as mock_checklist:
        rc = console.main(["checklist", "--limit", "7"])

    assert rc == 0
    mock_checklist.assert_called_once_with(limit=7, probe_external=False)


def test_main_checklist_requires_explicit_flag_to_probe_providers() -> None:
    with patch("paper_trading_console.run_checklist", return_value=2) as mock_checklist:
        assert console.main(["checklist", "--limit", "3", "--probe-external"]) == 2

    mock_checklist.assert_called_once_with(limit=3, probe_external=True)


def test_email_auth_opens_consent_for_configured_sender_without_printing_credentials(
    tmp_path: Path,
    capsys,
) -> None:
    """The operator flow must expose only the authorized address, never returned credentials."""
    client_secrets = tmp_path / "gmail-oauth-client.json"
    client_secrets.write_text('{"installed": {}}', encoding="utf-8")
    with (
        patch.object(console.settings, "NOTIFY_EMAIL_FROM", "langkunlong@gmail.com"),
        patch(
            "paper_trading_console.authorize_gmail",
            return_value=SimpleNamespace(
                email="langkunlong@gmail.com",
                refresh_token_stored=True,
            ),
        ) as authorize,
    ):
        rc = console.run_email_auth(client_secrets=client_secrets, email=None)

    output = capsys.readouterr().out
    assert rc == 0
    assert "langkunlong@gmail.com" in output
    assert "refresh_token" not in output
    authorize.assert_called_once_with("langkunlong@gmail.com", client_secrets)


def test_main_email_auth_has_no_password_or_token_cli_option(tmp_path: Path) -> None:
    """Adding a secret-valued CLI flag would leak it into shell history and process listings."""
    client_secrets = tmp_path / "gmail-oauth-client.json"
    client_secrets.write_text('{"installed": {}}', encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        console.main(
            [
                "email-auth",
                "--client-secrets",
                str(client_secrets),
                "--password",
                "must-not-be-accepted",
            ]
        )

    assert exc_info.value.code == 2


def test_email_test_sends_through_configured_notifier(capsys) -> None:
    """The smoke command must exercise the same notifier used by trading workflows."""
    with (
        patch("paper_trading_console.notify_configured_backend", return_value="gmail_oauth"),
        patch("paper_trading_console.send_email", return_value=True) as send,
    ):
        rc = console.run_email_test()

    assert rc == 0
    subject, body = send.call_args.args
    assert subject == "[CANSLIM] Gmail OAuth notification test"
    assert "browser-authorized Gmail API" in body
    assert "sent successfully" in capsys.readouterr().out


def test_email_test_refuses_a_non_oauth_backend_even_when_delivery_would_succeed(capsys) -> None:
    """SMTP and auto fallback cannot be reported as a Gmail OAuth smoke-test success."""
    with (
        patch("paper_trading_console.notify_configured_backend", return_value="smtp"),
        patch("paper_trading_console.send_email", return_value=True) as send,
    ):
        rc = console.run_email_test()

    assert rc == 1
    send.assert_not_called()
    assert "requires NOTIFY_EMAIL_PROVIDER=gmail_oauth with authorization" in capsys.readouterr().out


def test_explicit_unavailable_gmail_oauth_is_a_failed_deployment_check() -> None:
    """A deliberately selected but unauthorized OAuth backend must block doctor and checklist."""
    with (
        patch.object(console.settings, "NOTIFY_EMAIL_PROVIDER", "gmail_oauth"),
        patch("paper_trading_console.notify_configured", return_value=False),
    ):
        result = console._check_email_configuration()

    assert result.ok is False
    assert result.severity == "fail"
    assert result.detail == "Gmail OAuth is selected but unavailable; run `email-auth` again"


def test_unsupported_notification_provider_is_a_failed_deployment_check() -> None:
    """A misspelled provider must not be downgraded to a warning that disables notifications."""
    with patch.object(console.settings, "NOTIFY_EMAIL_PROVIDER", "gmial_oauth"):
        result = console._check_email_configuration()

    assert result.ok is False
    assert result.severity == "fail"
    assert result.detail == "Unsupported NOTIFY_EMAIL_PROVIDER: gmial_oauth"


def test_invalid_notification_recipient_is_a_failed_deployment_check() -> None:
    """A malformed recipient must block preflight before notification delivery is attempted."""
    with patch.object(console.settings, "NOTIFY_EMAIL_TO", "not-an-email"):
        result = console._check_email_configuration()

    assert result.ok is False
    assert result.severity == "fail"
    assert result.detail == "NOTIFY_EMAIL_TO must be a valid email address"


def test_email_revoke_removes_the_configured_sender_grant(capsys) -> None:
    """The console revoke path must target only the configured Gmail identity."""
    with (
        patch.object(console.settings, "NOTIFY_EMAIL_FROM", "langkunlong@gmail.com"),
        patch(
            "paper_trading_console.revoke_gmail_authorization",
            return_value=SimpleNamespace(
                email="langkunlong@gmail.com",
                remote_revoked=True,
            ),
        ) as revoke,
    ):
        rc = console.run_email_revoke(email=None)

    assert rc == 0
    assert "revoked" in capsys.readouterr().out.lower()
    revoke.assert_called_once_with("langkunlong@gmail.com")
