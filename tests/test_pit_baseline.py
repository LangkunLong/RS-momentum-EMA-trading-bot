"""Offline publication integration regression for the immutable PIT runner."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import pandas as pd
import pytest

from core.backtest_engine import SimulationResult
from core.leader_basket import LeaderBasketResult
from core.leader_evaluation import PointInTimeUniverse
import pit_baseline


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return cls(2026, 8, 23, 12, 0, tzinfo=tz or timezone.utc)


def test_main_publishes_hashed_artifacts_and_refuses_same_run_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Break caught: the production runner published unhashed artifacts or overwrote a run."""
    inputs = {name: tmp_path / f"{name}.json" for name in ("membership", "prices", "fundamentals", "coverage", "master", "exclusions")}
    for path in inputs.values():
        path.write_text("{}\n", encoding="utf-8")
    bundle_path = tmp_path / "pit.sqlite3"
    bundle_path.write_bytes(b"synthetic full-contract PIT fixture")
    bundle_sha = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    source_hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in inputs.items()}
    membership = PointInTimeUniverse.from_rows([
        {"effective_date": "2021-01-01", "ticker": "AAA", "member": True},
    ])

    class FakeBundle:
        sha256 = bundle_sha
        data_cutoff = pd.Timestamp(pit_baseline._END)
        metadata = {
            "evaluation_start": pit_baseline._START,
            "data_cutoff": pit_baseline._END,
            "warmup_start": pit_baseline._WARMUP,
            "membership_provenance_sha256": source_hashes["membership"],
            "prices_provenance_sha256": source_hashes["prices"],
            "fundamentals_provenance_sha256": source_hashes["fundamentals"],
        }

        def __init__(self, *_args, **_kwargs) -> None:
            self.membership = membership

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def load_price_identity_transition_contract(self, _path):
            return object()

        def symbols(self):
            return ("AAA", "SPY")

        def members_at(self, _when):
            return frozenset({"AAA"})

        def fetch_closes(self, _symbols, _start, _end):
            return pd.DataFrame({"AAA": [100.0, 101.0], "SPY": [100.0, 101.0]}, index=pd.to_datetime(["2021-01-01", "2021-01-04"]))

        def manifest(self):
            return {"coverage": {"price": {"first_date": "2020-01-02", "last_date": pit_baseline._END}}}

    class FakePortfolio:
        def __init__(self, **_kwargs) -> None:
            self.identity_transition_contract = None

        def run(self, _tickers, **_kwargs):
            return SimulationResult(
                config={"min_rs_score": 80.0, "min_canslim_score": 75.0},
                signal_log=pd.DataFrame([{"symbol": "AAA", "signal_date": "2021-01-01", "buy_signal": True}]),
                transaction_log=pd.DataFrame([{"Ticker": "AAA", "Date": "2021-01-04", "Action": "BUY"}]),
                weekly_holdings=pd.DataFrame(),
                execution_diagnostics={
                    "buy_signal_rows": 1, "entries_executed": 1, "entry_attempts": 1,
                    "entry_rejected_already_open": 0, "entry_rejected_capacity": 0,
                    "entry_rejected_missing_data": 0, "entry_rejected_invalid_price": 0,
                    "entry_rejected_invalid_risk": 0, "entry_rejected_no_cash": 0,
                    "buy_signal_rows_when_entries_allowed": 1,
                    "buy_signal_rows_blocked_by_regime": 0, "buy_signal_rows_blocked_by_market": 0,
                    "buy_signal_rows_blocked_by_both": 0, "capacity_truncated_signals": 0,
                },
            )

    class FakeBasket:
        def __init__(self, *_args, **_kwargs) -> None:
            self.identity_transition_contract = None

        def run(self, **_kwargs):
            return LeaderBasketResult(pd.Series([100.0, 101.0]), pd.Series([100.0, 101.0]), pd.DataFrame(), pd.DataFrame())

    real_run_baseline = pit_baseline.run_baseline
    monkeypatch.setattr(pit_baseline, "PITDataBundle", FakeBundle)
    monkeypatch.setattr(pit_baseline, "datetime", _FrozenDateTime)
    monkeypatch.setattr(pit_baseline, "_git_identity", lambda *_args, **_kwargs: "f" * 40)
    monkeypatch.setattr(pit_baseline, "LeaderIdentityContract", type("FakeIdentities", (), {"from_prices_provenance": staticmethod(lambda *_args, **_kwargs: object())}))
    monkeypatch.setattr(pit_baseline, "label_five_year_leaders", lambda *_args, **_kwargs: (object(),) * 100)
    monkeypatch.setattr(pit_baseline, "label_rolling_leaders", lambda *_args, **_kwargs: (object(),) * 4_800)
    monkeypatch.setattr(pit_baseline, "five_year_leaders_frame", lambda _leaders: pd.DataFrame({"ticker": ["AAA"]}))
    monkeypatch.setattr(pit_baseline, "rolling_leaders_frame", lambda _labels: pd.DataFrame({"ticker": ["AAA"]}))
    monkeypatch.setattr(pit_baseline, "_alias_map", lambda _identities: {"AAA": ("AAA",)})
    monkeypatch.setattr(pit_baseline, "build_leader_recall_frame", lambda *_args, **_kwargs: pd.DataFrame([{"ticker": "AAA", "rank": 1, "total_return_pct": 1.0, "buy_signal_count": 1, "entry_count": 1}]))
    monkeypatch.setattr(pit_baseline, "rolling_label_recall_pct", lambda *_args, **_kwargs: 100.0)
    monkeypatch.setattr(pit_baseline, "_load_task2_audit", lambda **_kwargs: ({}, {"resolved_or_closed_exclusion_percentage": 100.0, "membership_union_symbol_count": 1}))
    monkeypatch.setattr(pit_baseline, "_coverage", lambda **_kwargs: {"all_gates_passed": True, "prices": {"coverage_pct": 100.0}, "cik_and_exclusions": {"resolved_cik_percentage": 100.0}, "evaluated_fundamentals": {"current_quarterly_and_annual_pct": 100.0}, "gates": {}})
    monkeypatch.setattr(pit_baseline, "_validate_portfolio", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pit_baseline, "_validate_basket", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pit_baseline, "_validate_holding_identities", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pit_baseline, "_metrics", lambda _result: {"total_return_pct": 1.0})
    monkeypatch.setattr(pit_baseline, "_basket_metrics", lambda _result: {"total_return_pct": 1.0})
    monkeypatch.setattr(pit_baseline, "run_baseline", lambda args: real_run_baseline(args, portfolio_factory=FakePortfolio, basket_factory=FakeBasket, require_clean_git=False))
    real_write_bytes = pit_baseline._write_bytes
    publication_writes: list[Path] = []

    def record_publication_write(path: Path, payload: bytes) -> None:
        publication_writes.append(path)
        real_write_bytes(path, payload)

    monkeypatch.setattr(pit_baseline, "_write_bytes", record_publication_write)
    argv = [
        "pit_baseline.py", "--pit-bundle", str(bundle_path), "--bundle-sha256", bundle_sha,
        "--membership-provenance", str(inputs["membership"]), "--prices-provenance", str(inputs["prices"]),
        "--fundamentals-provenance", str(inputs["fundamentals"]), "--fundamentals-coverage", str(inputs["coverage"]),
        "--security-master", str(inputs["master"]), "--security-master-exclusions", str(inputs["exclusions"]),
        "--output-root", str(tmp_path / "runs"),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    assert pit_baseline.main() == 0
    run_dir = tmp_path / "runs" / f"run-20260823T120000Z-{bundle_sha[:12]}"
    required = {"five_year_leaders.csv", "rolling_leader_labels.csv", "canslim_signals.csv", "transactions.csv", "weekly_holdings.csv", "equity_curve.csv", "leader_basket_holdings.csv", "leader_basket_transactions.csv", "leader_basket_equity.csv", "leader_recall.csv", "coverage.json", "summary.json", "report.md", "run_manifest.json"}
    assert required == {path.name for path in run_dir.iterdir()}
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"
    assert set(manifest["artifacts"]) == required - {"run_manifest.json"}
    assert {name: hashlib.sha256((run_dir / name).read_bytes()).hexdigest() for name in manifest["artifacts"]} == manifest["artifacts"]
    before_collision = {path.name: path.read_bytes() for path in run_dir.iterdir()}
    writes_before_collision = len(publication_writes)
    assert pit_baseline.main() == 1
    assert "PIT baseline failed closed" in capsys.readouterr().out
    assert {path.name: path.read_bytes() for path in run_dir.iterdir()} == before_collision
    assert len(publication_writes) == writes_before_collision
