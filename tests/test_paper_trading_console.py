"""Tests for the paper-trading deployment and observation console."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import paper_trading_console as console


def test_doctor_fails_when_not_in_paper_mode() -> None:
    with (
        patch("paper_trading_console._check_paper_mode", return_value=console.CheckResult("Paper mode", False, "bad")),
        patch("paper_trading_console._check_api_keys_present", return_value=console.CheckResult("API keys", True, "ok")),
        patch("paper_trading_console._check_execution_store_path", return_value=console.CheckResult("Store", True, "ok")),
        patch("paper_trading_console._check_scan_results_dir", return_value=console.CheckResult("Scan dir", True, "ok")),
        patch("paper_trading_console._check_email_configuration", return_value=console.CheckResult("Email", True, "ok")),
        patch("paper_trading_console._check_alpaca_connectivity", return_value=console.CheckResult("Alpaca", True, "ok")),
    ):
        assert console.run_doctor() == 1


def test_doctor_passes_when_all_checks_are_clean() -> None:
    with (
        patch("paper_trading_console._check_paper_mode", return_value=console.CheckResult("Paper mode", True, "ok")),
        patch("paper_trading_console._check_api_keys_present", return_value=console.CheckResult("API keys", True, "ok")),
        patch("paper_trading_console._check_execution_store_path", return_value=console.CheckResult("Store", True, "ok")),
        patch("paper_trading_console._check_scan_results_dir", return_value=console.CheckResult("Scan dir", True, "ok")),
        patch("paper_trading_console._check_email_configuration", return_value=console.CheckResult("Email", True, "ok")),
        patch("paper_trading_console._check_alpaca_connectivity", return_value=console.CheckResult("Alpaca", True, "ok")),
    ):
        assert console.run_doctor() == 0


def test_checklist_returns_warning_only_success(capsys) -> None:
    with (
        patch("paper_trading_console._check_paper_mode", return_value=console.CheckResult("Paper mode", True, "ok")),
        patch("paper_trading_console._check_api_keys_present", return_value=console.CheckResult("API keys", True, "ok")),
        patch("paper_trading_console._check_execution_store_path", return_value=console.CheckResult("Store", True, "ok")),
        patch(
            "paper_trading_console._check_execution_store_health",
            return_value=console.CheckResult("Store health", True, "empty store", severity="warn"),
        ),
        patch("paper_trading_console._check_scan_results_dir", return_value=console.CheckResult("Scan dir", True, "ok")),
        patch(
            "paper_trading_console._check_recent_signal_quality",
            return_value=console.CheckResult("Signals", True, "no actionable buys", severity="warn"),
        ),
        patch("paper_trading_console._check_email_configuration", return_value=console.CheckResult("Email", True, "ok")),
        patch("paper_trading_console._check_alpaca_connectivity", return_value=console.CheckResult("Alpaca", True, "ok")),
    ):
        rc = console.run_checklist(limit=5)

    assert rc == 0
    output = capsys.readouterr().out
    assert "PAPER TRADING CHECKLIST" in output
    assert "[WARN] Store health: empty store" in output
    assert "Safe for supervised paper testing" in output


def test_checklist_fails_on_hard_failure(capsys) -> None:
    with (
        patch("paper_trading_console._check_paper_mode", return_value=console.CheckResult("Paper mode", True, "ok")),
        patch("paper_trading_console._check_api_keys_present", return_value=console.CheckResult("API keys", False, "missing key", severity="fail")),
        patch("paper_trading_console._check_execution_store_path", return_value=console.CheckResult("Store", True, "ok")),
        patch("paper_trading_console._check_execution_store_health", return_value=console.CheckResult("Store health", True, "ok")),
        patch("paper_trading_console._check_scan_results_dir", return_value=console.CheckResult("Scan dir", True, "ok")),
        patch("paper_trading_console._check_recent_signal_quality", return_value=console.CheckResult("Signals", True, "ok")),
        patch("paper_trading_console._check_email_configuration", return_value=console.CheckResult("Email", True, "ok")),
        patch("paper_trading_console._check_alpaca_connectivity", return_value=console.CheckResult("Alpaca", True, "ok")),
    ):
        rc = console.run_checklist(limit=5)

    assert rc == 1
    output = capsys.readouterr().out
    assert "[FAIL] API keys: missing key" in output
    assert "Do not enable paper automation yet." in output


def test_checklist_fails_for_any_not_ok_result_even_with_default_severity(capsys) -> None:
    """The summary must agree with a row rendered as FAIL."""
    with (
        patch(
            "paper_trading_console._check_paper_mode",
            return_value=console.CheckResult("Paper mode", False, "live mode"),
        ),
        patch("paper_trading_console._check_api_keys_present", return_value=console.CheckResult("API keys", True, "ok")),
        patch("paper_trading_console._check_execution_store_path", return_value=console.CheckResult("Store", True, "ok")),
        patch("paper_trading_console._check_execution_store_health", return_value=console.CheckResult("Store health", True, "ok")),
        patch("paper_trading_console._check_scan_results_dir", return_value=console.CheckResult("Scan dir", True, "ok")),
        patch("paper_trading_console._check_recent_signal_quality", return_value=console.CheckResult("Signals", True, "ok")),
        patch("paper_trading_console._check_email_configuration", return_value=console.CheckResult("Email", True, "ok")),
        patch("paper_trading_console._check_alpaca_connectivity", return_value=console.CheckResult("Alpaca", True, "ok")),
    ):
        rc = console.run_checklist(limit=5)

    assert rc == 1
    output = capsys.readouterr().out
    assert "[FAIL] Paper mode: live mode" in output
    assert "Do not enable paper automation yet." in output


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
    mock_checklist.assert_called_once_with(limit=7)
