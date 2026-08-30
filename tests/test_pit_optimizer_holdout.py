from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest


def test_holdout_progress_journal_publishes_live_state_and_seals_terminal_state(
    tmp_path: Path,
) -> None:
    """Break caught: a live detached holdout could be mistaken for an orphan or reopen after closure."""
    from core.pit_optimizer_holdout import HoldoutProgressJournal

    root = (tmp_path / "holdout").resolve()
    root.mkdir()
    journal = HoldoutProgressJournal(root=root, controller_pid=os.getpid())

    journal.publish("candidate_replay")
    live = journal.read()

    assert live["state"] == "candidate_replay"
    assert live["terminal"] is False
    assert live["sequence"] == 1
    observer = HoldoutProgressJournal(root=root, controller_pid=os.getpid() + 10_000)
    assert observer.health(process_is_running=lambda pid: pid == os.getpid()) == "running"

    journal.publish("completed")
    closed = journal.read()

    assert closed["state"] == "completed"
    assert closed["terminal"] is True
    assert closed["sequence"] == 2

    with pytest.raises(RuntimeError, match="terminal"):
        journal.publish("candidate_replay")


def test_discovery_winner_evidence_accepts_only_a_closed_discovery_artifact(
    tmp_path: Path,
) -> None:
    """Break caught: a holdout could trust an incomplete canary or an altered inert winner diff."""
    from core.pit_optimizer_holdout import load_discovery_winner_evidence

    root = (tmp_path / "discovery").resolve()
    root.mkdir()
    cumulative_diff = "diff --git a/core/strategy_policy/entry.py b/core/strategy_policy/entry.py\n"
    (root / "incumbent.diff").write_text(cumulative_diff, encoding="utf-8", newline="\n")
    incumbent = {
        "changed_paths": ["core/strategy_policy/entry.py"],
        "changed_symbols": ["core.strategy_policy.entry.evaluate_entry"],
        "cumulative_diff_sha256": "a" * 64,
        "discovery_manifest_sha256": "b" * 64,
        "editable_file_sha256s": [],
        "identity_sha256": "c" * 64,
        "immutable_constraints_sha256": "d" * 64,
        "policy_interface_version": 1,
        "source_commit": "e" * 40,
    }
    summary = {
        "schema_version": 3,
        "phase": "run",
        "status": "loop_verified_no_long_replay_candidate",
        "terminal": {"code": "iteration_limit", "detail": None, "exit_code": 0},
        "run_id": "run_test",
        "readiness_sha256": "f" * 64,
        "manifest_sha256": "0" * 64,
        "iterations": {"started": 2, "completed": 2, "valid_evaluations": 2, "incumbent_updates": 1, "non_improving_streak": 0},
        "incumbent": incumbent,
        "discovery_outcome": {"winner_identity_sha256": incumbent["identity_sha256"]},
        "hidden_outcome": {"opened": False, "validation_reservation_sha256": None, "long_replay_eligible": None},
        "accounting": {"api_calls": 6, "retained_reservation_tokens": 0, "incomplete_accounting_calls": 0, "accounting_complete": True},
        "cleanup": {"complete": True, "source_modified": False},
        "artifact_digests": [{"path": "incumbent.diff", "sha256": hashlib.sha256(cumulative_diff.encode("utf-8")).hexdigest()}],
    }
    raw = json.dumps(summary, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
    (root / "summary.json").write_bytes(raw)

    evidence = load_discovery_winner_evidence(
        root=root,
        expected_summary_sha256=hashlib.sha256(raw).hexdigest(),
        expected_role_calls=6,
    )

    assert evidence.cumulative_diff == cumulative_diff
    assert evidence.incumbent == incumbent


def test_holdout_cli_requires_an_explicit_non_consuming_or_consuming_action() -> None:
    """Break caught: invoking the holdout launcher without an action could accidentally consume hidden validation."""
    from pit_optimizer_holdout import build_parser

    parser = build_parser()
    required = [
        "--repo-root", "C:/repo",
        "--permanent-runtime-root", "C:/runtime",
        "--controller-temp-parent", "C:/controller",
        "--holdout-artifact-root", "C:/artifacts/holdout",
        "--git-executable", "C:/git.exe",
        "--docker-executable", "C:/docker.exe",
        "--optimizer-manifest", "C:/artifacts/manifest.json",
        "--verified-parity", "C:/artifacts/parity.json",
        "--pit-bundle", "C:/artifacts/pit.sqlite3",
        "--baseline-run", "C:/artifacts/baseline",
        "--discovery-artifact-root", "C:/artifacts/discovery",
        "--discovery-summary-sha256", "a" * 64,
    ]

    with pytest.raises(SystemExit):
        parser.parse_args(required)

    parsed = parser.parse_args([*required, "--preflight"])
    assert parsed.preflight is True
    assert parsed.execute is False


def test_holdout_failure_marks_a_reserved_hidden_validation_as_opened() -> None:
    """Break caught: an evaluator interruption after reservation was mislabeled as preflight-only."""
    from pit_optimizer_holdout import _hidden_validation_was_opened

    class ReservedState:
        hidden_validation_opened = True

    assert _hidden_validation_was_opened(ReservedState()) is True
    assert _hidden_validation_was_opened(None) is False
