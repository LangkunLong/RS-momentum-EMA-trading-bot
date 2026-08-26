"""Offline regressions for the immutable corrected PIT baseline authority."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

import core.pit_diagnosis.baseline as baseline_module
from core.pit_diagnosis.baseline import (
    BaselineAuthority,
    canonical_authority,
    compare_reproduction,
    verify_baseline_run,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, sort_keys=True, allow_nan=False), encoding="utf-8")


def _replace_artifact_identity(
    run_dir: Path, authority: BaselineAuthority, name: str
) -> BaselineAuthority:
    manifest_path = run_dir / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifacts = dict(authority.artifact_sha256)
    artifacts[name] = _sha256(run_dir / name)
    manifest["artifacts"] = artifacts
    _write_json(manifest_path, manifest)
    return replace(
        authority,
        artifact_sha256=artifacts,
        manifest_sha256=_sha256(manifest_path),
    )


@pytest.fixture
def mini_verified_run(tmp_path: Path) -> tuple[Path, BaselineAuthority]:
    """A manifest-complete offline run whose identities are locally hash-bound."""

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    signals = pd.DataFrame(
        {
            "symbol": ["AAA", "BBB"],
            "signal_date": ["2021-01-04", "2021-01-05"],
            "entry_contract_eligible": [True, True],
        }
    )
    outcomes = pd.DataFrame(
        {
            "symbol": ["AAA", "BBB"],
            "signal_date": ["2021-01-04", "2021-01-05"],
            "entry_date": ["2021-01-05", "2021-01-06"],
            "pivot": [100.0, 100.0],
            "buy_zone_lower": [100.0, 100.0],
            "buy_zone_upper": [105.0, 105.0],
            "entry_open": [101.0, 106.0],
            "outcome": ["entries_executed", "entry_rejected_next_open_buy_zone"],
        }
    )
    transactions = pd.DataFrame(
        {
            "Ticker": ["AAA"],
            "Date": ["2021-01-05"],
            "Action": ["BUY"],
            "Price": [101.0],
            "Quantity": [1.0],
            "Reason": ["entry"],
        }
    )
    funnel = pd.DataFrame(
        {
            "signal_date": ["2021-01-04", "2021-01-05"],
            "evaluated_count": [1, 1],
            "qualified_count": [1, 1],
            "attempted_count": [1, 1],
            "executed_count": [1, 0],
            "rejected_count": [0, 1],
        }
    )
    equity = pd.DataFrame(
        {
            "date": ["2021-01-04", "2021-01-05"],
            "portfolio": [100.0, 90.0],
            "benchmark": [100.0, 101.0],
        }
    )
    recall = pd.DataFrame({"ticker": ["AAA"], "buy_signal_count": [1], "entry_count": [1]})
    for name, frame in {
        "canslim_signals.csv": signals,
        "entry_attempt_outcomes.csv": outcomes,
        "transactions.csv": transactions,
        "daily_entry_funnel.csv": funnel,
        "equity_curve.csv": equity,
        "leader_recall.csv": recall,
    }.items():
        frame.to_csv(run_dir / name, index=False)
    summary = {
        "canslim": {
            "total_return_pct": -10.0,
            "annualized_return_pct": -10.0,
            "sharpe_ratio": -1.0,
            "max_drawdown_pct": -10.0,
            "closed_trades": 1,
            "win_rate_pct": 0.0,
            "average_cash_pct": 50.0,
        },
        "entry_contract": {
            "qualified_signals": 2,
            "executed_attempts": 1,
            "next_open_buy_zone_rejections": 1,
            "rejection_counts": {
                "entry_rejected_capacity": 0,
                "entry_rejected_invalid_price": 0,
                "entry_rejected_invalid_risk": 0,
                "entry_rejected_missing_data": 0,
                "entry_rejected_no_cash": 0,
            },
        },
    }
    _write_json(run_dir / "summary.json", summary)
    artifacts = {
        path.name: _sha256(path)
        for path in run_dir.iterdir()
        if path.is_file()
    }
    manifest = {
        "schema_version": 1,
        "status": "complete",
        "git_head": canonical_authority().replay_git_head,
        "bundle_sha256": "a" * 64,
        "date_contract": {
            "warmup_start": "2020-01-01",
            "evaluation_start": "2021-01-01",
            "data_cutoff": "2025-12-31",
        },
        "entry_attempt_outcome_schema_version": 1,
        "entry_attempt_outcome_count": 2,
        "artifacts": artifacts,
    }
    _write_json(run_dir / "run_manifest.json", manifest)
    authority = replace(
        canonical_authority(),
        source_commit=canonical_authority().source_commit,
        replay_git_head=canonical_authority().replay_git_head,
        bundle_sha256="a" * 64,
        manifest_sha256=_sha256(run_dir / "run_manifest.json"),
        artifact_sha256=artifacts,
        entry_outcome_row_sha256=baseline_module._normalized_ordered_row_sha256(
            pd.read_csv(run_dir / "entry_attempt_outcomes.csv", keep_default_na=False)
        ),
        transaction_row_sha256=baseline_module._normalized_ordered_row_sha256(
            pd.read_csv(run_dir / "transactions.csv", keep_default_na=False)
        ),
        total_return_pct=-10.0,
        annualized_return_pct=-10.0,
        sharpe_ratio=-1.0,
        max_drawdown_pct=-10.0,
        closed_trades=1,
        win_rate_pct=0.0,
        average_cash_pct=50.0,
        qualified_entries=2,
        executed_entries=1,
        next_open_buy_zone_rejections=1,
        cash_rejections=0,
    )
    return run_dir, authority


def test_corrected_baseline_authority_is_exact() -> None:
    authority = canonical_authority()
    assert authority.bundle_sha256 == "1af306ef1e46797473cd186fc48938ed6694ae25f5943c9f1905b528307cc2eb"
    assert authority.total_return_pct == pytest.approx(-9.994717769465932)
    assert authority.closed_trades == 225
    assert authority.qualified_entries == 286
    assert authority.executed_entries == 225
    assert authority.next_open_buy_zone_rejections == 51
    assert authority.average_cash_pct == pytest.approx(67.31359377429541)
    assert authority.entry_outcome_row_sha256 == (
        "8b479ef13e693a2fc101dc3c8b1bdb0204e71122c6701fc5a7e23cd67cf3f3aa"
    )
    assert authority.transaction_row_sha256 == (
        "603ccc01141cf55447412d1caa40e9942c5f59745c73183644be8d9b65ab72c5"
    )


def test_baseline_verifier_rejects_one_changed_transaction(
    mini_verified_run: tuple[Path, BaselineAuthority],
) -> None:
    run_dir, authority = mini_verified_run
    frame = pd.read_csv(run_dir / "transactions.csv")
    frame.loc[0, "Price"] += 0.01
    frame.to_csv(run_dir / "transactions.csv", index=False)
    with pytest.raises(ValueError, match="SHA-256"):
        verify_baseline_run(run_dir, authority)


def test_baseline_verifier_reconciles_ledgers_and_reproduction_is_exact(
    mini_verified_run: tuple[Path, BaselineAuthority],
) -> None:
    run_dir, authority = mini_verified_run

    snapshot = verify_baseline_run(run_dir, authority)
    reproduction = compare_reproduction(snapshot, snapshot)

    assert snapshot.transaction_row_sha256
    assert reproduction.passed is True
    assert reproduction.mismatch_codes == ()


def test_reproduction_rejects_different_replay_and_manifest_identity(
    mini_verified_run: tuple[Path, BaselineAuthority],
) -> None:
    run_dir, authority = mini_verified_run
    snapshot = verify_baseline_run(run_dir, authority)
    reproduced = replace(
        snapshot,
        replay_git_head="b" * 40,
        manifest_sha256="c" * 64,
    )

    result = compare_reproduction(snapshot, reproduced)

    assert result.passed is False
    assert result.mismatch_codes == (
        "identity.replay_git_head",
        "identity.manifest_sha256",
    )


def test_baseline_verifier_rejects_changed_transaction_row_after_artifact_rehash(
    mini_verified_run: tuple[Path, BaselineAuthority],
) -> None:
    run_dir, authority = mini_verified_run
    frame = pd.read_csv(run_dir / "transactions.csv")
    frame.loc[0, "Price"] += 0.01
    frame.to_csv(run_dir / "transactions.csv", index=False)
    rehashed_authority = _replace_artifact_identity(
        run_dir, authority, "transactions.csv"
    )

    with pytest.raises(ValueError, match="transaction row hash"):
        verify_baseline_run(run_dir, rehashed_authority)


def test_baseline_verifier_rejects_daily_qualified_count_shift(
    mini_verified_run: tuple[Path, BaselineAuthority],
) -> None:
    run_dir, authority = mini_verified_run
    funnel = pd.read_csv(run_dir / "daily_entry_funnel.csv")
    funnel.loc[0, "qualified_count"] += 1
    funnel.loc[1, "qualified_count"] -= 1
    funnel.to_csv(run_dir / "daily_entry_funnel.csv", index=False)
    rehashed_authority = _replace_artifact_identity(
        run_dir, authority, "daily_entry_funnel.csv"
    )

    with pytest.raises(ValueError, match="daily funnel qualified_count"):
        verify_baseline_run(run_dir, rehashed_authority)


def test_baseline_verifier_rejects_nonfinite_summary_value(
    mini_verified_run: tuple[Path, BaselineAuthority],
) -> None:
    run_dir, authority = mini_verified_run
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    summary["canslim"]["total_return_pct"] = float("nan")
    (run_dir / "summary.json").write_text(
        json.dumps(summary, sort_keys=True, allow_nan=True), encoding="utf-8"
    )
    rehashed_authority = _replace_artifact_identity(
        run_dir, authority, "summary.json"
    )

    with pytest.raises(ValueError, match="finite JSON"):
        verify_baseline_run(run_dir, rehashed_authority)
