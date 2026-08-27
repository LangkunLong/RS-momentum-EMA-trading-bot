"""Small sealed-fixture contracts for the Task 11 diagnostic readers."""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import date
import hashlib
from itertools import product
import json
from pathlib import Path
import sqlite3
import string
from typing import Mapping

import pandas as pd
import pytest

from core.canslim.earnings_trace import ATrace, CTrace, MetricFamily, TraceReason
from core.pit_diagnosis import task11_artifact_diagnosis as artifacts
from core.pit_diagnosis import task11_ca_provenance as ca
from core.pit_diagnosis.baseline import (
    BaselineAuthority,
    BaselineAuthorityProfile,
    BaselineSnapshot,
    DEFAULT_BASELINE_PROFILE_ID,
    STRICT_PROPER_BASE_TASK11_PROFILE_ID,
)
from core.pit_provenance import PIT_PUBLIC_DATES_ATTR


_PRIVATE_SENTINELS = {
    "symbol": "SECRET",
    "ticker": "PRIVATE-TICKER",
    "cik": "0000123456",
    "accession": "0000123456-24-000001",
    "path": "C:/private/sec/filing.json",
    "url": "https://private.invalid/sec/filing",
    "private_filing": "PRIVATE-FILING-ID",
    "private_date": "2099-12-31",
}
_PRIVATE_SYMBOL = _PRIVATE_SENTINELS["symbol"]
_REJECTION_NAMES = (
    "entry_rejected_already_open",
    "entry_rejected_capacity",
    "entry_rejected_invalid_price",
    "entry_rejected_invalid_risk",
    "entry_rejected_missing_data",
    "entry_rejected_next_open_buy_zone",
    "entry_rejected_no_cash",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _member_symbols() -> tuple[str, ...]:
    symbols = [_PRIVATE_SYMBOL]
    for letters in product(string.ascii_uppercase, repeat=3):
        ticker = "".join(letters)
        if ticker != "SPY":
            symbols.append(ticker)
        if len(symbols) == 495:
            return tuple(symbols)
    raise AssertionError("synthetic membership generator did not produce 495 symbols")


def _write_pit_bundle(
    path: Path,
    *,
    fundamentals_sha256: str,
    provenance_sha256: str,
) -> None:
    digest = "0" * 64
    metadata = {
        "bundle_kind": "canslim_pit_v1",
        "schema_version": "1",
        "data_cutoff": "2025-12-31",
        "evaluation_start": "2021-01-01",
        "warmup_start": "2020-01-01",
        "membership_source_sha256": digest,
        "prices_source_sha256": digest,
        "fundamentals_source_sha256": fundamentals_sha256,
        "membership_provenance_sha256": digest,
        "prices_provenance_sha256": digest,
        "fundamentals_provenance_sha256": provenance_sha256,
        "membership_source_kind": "offline_test_fixture",
        "membership_revision_id": "fixture-v1",
        "membership_raw_sha256": digest,
        "membership_symbol_map_sha256": digest,
        "membership_security_names_sha256": digest,
        "prices_source_kind": "offline_test_fixture",
        "prices_upstream_source_sha256": digest,
        "spy_trading_days_sha256": digest,
        "price_identity_map_sha256": digest,
        "price_identity_request_contracts_sha256": digest,
        "price_exclusion_count": "0",
        "price_exclusions_sha256": digest,
        "fundamentals_source_kind": "offline_test_fixture",
        "fundamentals_submissions_archive_sha256": digest,
        "fundamentals_companyfacts_archive_sha256": digest,
        "fundamentals_identity_manifest_csv_sha256": digest,
    }
    members = _member_symbols()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "CREATE TABLE dataset_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE membership (effective_date TEXT NOT NULL, ticker TEXT NOT NULL, member INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE price (trade_date TEXT NOT NULL, ticker TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE fundamentals (ticker TEXT NOT NULL, statement_type TEXT NOT NULL, period_end TEXT NOT NULL, public_date TEXT NOT NULL, basic_eps REAL, diluted_eps REAL, total_revenue REAL, net_income REAL, common_stock REAL, total_stockholders_equity REAL, shares_outstanding REAL, held_percent_institutions REAL, institution_count INTEGER, prev_institution_count INTEGER)"
        )
        connection.executemany(
            "INSERT INTO dataset_metadata VALUES (?, ?)", metadata.items()
        )
        connection.executemany(
            "INSERT INTO membership VALUES (?, ?, ?)",
            (("2021-01-01", symbol, 1) for symbol in members),
        )
        connection.executemany(
            "INSERT INTO price VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ("2020-01-02", "SPY", 100.0, 101.0, 99.0, 100.0, 1_000.0),
                ("2024-06-03", "SPY", 500.0, 501.0, 499.0, 500.0, 2_000.0),
            ),
        )
        connection.executemany(
            "INSERT INTO fundamentals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (_PRIVATE_SYMBOL, "quarterly", "2023-03-31", "2023-05-01", None, 1.0, None, None, None, None, None, None, None, None),
                (_PRIVATE_SYMBOL, "quarterly", "2023-06-30", "2023-07-15", None, 1.0, None, None, None, None, None, None, None, None),
                (_PRIVATE_SYMBOL, "quarterly", "2024-03-31", "2024-05-01", None, 1.4, None, None, None, None, None, None, None, None),
                (_PRIVATE_SYMBOL, "quarterly", "2024-06-30", "2024-07-15", None, 1.5, None, None, None, None, None, None, None, None),
                (_PRIVATE_SYMBOL, "annual", "2021-12-31", "2022-02-15", None, 1.0, None, 100.0, None, None, None, None, None, None),
                (_PRIVATE_SYMBOL, "annual", "2022-12-31", "2023-02-15", None, 1.2, None, 120.0, None, None, None, None, None, None),
                (_PRIVATE_SYMBOL, "annual", "2023-12-31", "2024-02-15", None, 1.5, None, 150.0, None, None, None, None, None, None),
                (_PRIVATE_SYMBOL, "balance", "2023-12-31", "2024-02-15", None, None, None, None, None, 500.0, None, None, None, None),
            ),
        )
        connection.commit()


@dataclass(frozen=True)
class _SyntheticPublication:
    run_dir: Path
    profile: BaselineAuthorityProfile
    snapshot: BaselineSnapshot


def _synthetic_publication(tmp_path: Path) -> _SyntheticPublication:
    run_dir = tmp_path / "sealed-run"
    run_dir.mkdir()
    bundle_path = run_dir / "pit_bundle.sqlite3"
    fundamentals_path = run_dir / "fundamentals.csv"
    fundamentals_path.write_text(
        "symbol,private_filing,private_value\n"
        f"{_PRIVATE_SYMBOL},PRIVATE-SIDECAR-FILING,314159265.3589\n",
        encoding="utf-8",
        newline="",
    )
    audit_path = run_dir / "fundamentals_audit.csv"
    audit_path.write_text(
        "symbol,cik,accession,url\n"
        f"{_PRIVATE_SYMBOL},{_PRIVATE_SENTINELS['cik']},"
        f"{_PRIVATE_SENTINELS['accession']},{_PRIVATE_SENTINELS['url']}\n",
        encoding="utf-8",
        newline="",
    )
    fundamentals_sha256 = _sha256(fundamentals_path)
    audit_sha256 = _sha256(audit_path)
    provenance_path = run_dir / "fundamentals_provenance.json"
    provenance_path.write_text(
        json.dumps(
            {
                "fundamentals_sha256": fundamentals_sha256,
                "fundamentals_audit_sha256": audit_sha256,
                "private_filing": "PRIVATE-PROVENANCE-FILING",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
        newline="",
    )
    provenance_sha256 = _sha256(provenance_path)
    _write_pit_bundle(
        bundle_path,
        fundamentals_sha256=fundamentals_sha256,
        provenance_sha256=provenance_sha256,
    )
    bundle_sha256 = _sha256(bundle_path)
    manifest = {
        "arguments": {
            "pit_bundle": str(bundle_path),
            "fundamentals_provenance": str(provenance_path),
        },
        "input_sha256": {
            "pit_bundle": bundle_sha256,
            "fundamentals_provenance": provenance_sha256,
        },
        "bundle_metadata": {
            "fundamentals_provenance_sha256": provenance_sha256,
            "fundamentals_source_sha256": fundamentals_sha256,
        },
        "canslim_config": {
            "entry_contract_min_current_growth": 0.25,
            "entry_contract_min_annual_growth": 0.25,
            "entry_contract_min_rs_score": 80.0,
            "entry_contract_min_composite_score": 70.0,
            "signal_every_n_days": 1,
            "require_bullish_market": False,
            "use_stateful_regime_gate": False,
            "max_positions": None,
            "cash_deployment_threshold_pct": None,
            "position_risk_pct": 0.01,
            "position_size_pct": 0.10,
            "stop_loss_pct": 0.07,
            "take_profit_pct": 0.20,
        }
    }
    summary = {
        "entry_contract": {
            "evaluated_symbol_days": 2,
            "qualified_signals": 2,
            "attempted_signals": 2,
            "executed_attempts": 2,
            "rejected_attempts": 0,
            "next_open_buy_zone_rejections": 0,
            "rejection_counts": {name: 0 for name in _REJECTION_NAMES},
        },
        "canslim": {"average_cash_pct": 50.0},
    }
    extra_private = {
        key: value for key, value in _PRIVATE_SENTINELS.items() if key != "symbol"
    }
    private_header = ",".join(extra_private)
    private_values = ",".join(extra_private.values())
    text_files = {
        "run_manifest.json": json.dumps(manifest, sort_keys=True),
        "summary.json": json.dumps(summary, sort_keys=True),
        "canslim_signals.csv": (
            "symbol,signal_date,technical_setup_eligible,c_score,current_growth,"
            "a_score,annual_growth,"
            f"rs_score,entry_composite_score,entry_contract_eligible,{private_header}\n"
            f"{_PRIVATE_SYMBOL},2024-06-03,True,0.78,0.40,"
            f"0.5764705882352941,0.25,90.0,75.0,True,{private_values}\n"
            f"{_PRIVATE_SYMBOL},2024-08-05,True,1.0,0.50,"
            f"0.5764705882352941,0.25,90.0,75.0,True,{private_values}\n"
        ),
        "daily_entry_funnel.csv": (
            "signal_date,evaluated_count,qualified_count,attempted_count,executed_count,rejected_count\n"
            "2024-06-03,1,1,1,1,0\n"
            "2024-08-05,1,1,1,1,0\n"
        ),
        "entry_attempt_outcomes.csv": (
            f"signal_date,entry_date,outcome,{private_header}\n"
            f"2024-06-03,2024-06-04,entries_executed,{private_values}\n"
            f"2024-08-05,2024-08-06,entries_executed,{private_values}\n"
        ),
        "weekly_holdings.csv": (
            f"Week_Ending,Holding_Count,Cash,Total_Equity,{private_header}\n"
            f"2024-06-07,1,50.0,100.0,{private_values}\n"
            f"2024-08-09,1,50.0,100.0,{private_values}\n"
        ),
        "transactions.csv": (
            f"Date,Action,Reason,{private_header}\n"
            f"2024-06-04,BUY,entry,{private_values}\n"
            f"2024-06-11,SELL,ema,{private_values}\n"
            f"2024-08-06,BUY,entry,{private_values}\n"
            f"2024-08-13,SELL,ema,{private_values}\n"
        ),
        "equity_curve.csv": "Date,Equity\n2024-06-04,100.0\n",
        "leader_recall.csv": "date,recall\n2024-06-04,0.0\n",
    }
    for name, text in text_files.items():
        (run_dir / name).write_text(text, encoding="utf-8", newline="")

    artifact_sha256 = {
        name: _sha256(run_dir / name)
        for name in text_files
        if name != "run_manifest.json"
    }
    manifest_sha256 = _sha256(run_dir / "run_manifest.json")
    authority = BaselineAuthority(
        source_commit="a" * 40,
        replay_git_head="b" * 40,
        bundle_sha256=bundle_sha256,
        manifest_sha256=manifest_sha256,
        artifact_sha256=artifact_sha256,
        entry_outcome_row_sha256="d" * 64,
        transaction_row_sha256="e" * 64,
        total_return_pct=0.0,
        annualized_return_pct=0.0,
        sharpe_ratio=0.0,
        max_drawdown_pct=0.0,
        closed_trades=2,
        win_rate_pct=100.0,
        average_cash_pct=50.0,
        qualified_entries=2,
        executed_entries=2,
        next_open_buy_zone_rejections=0,
        cash_rejections=0,
    )
    profile = BaselineAuthorityProfile(
        profile_id=STRICT_PROPER_BASE_TASK11_PROFILE_ID,
        authority=authority,
        scope="strict_proper_base",
        fidelity_label="fidelity_incomplete",
        fidelity_reason="synthetic offline diagnostic fixture",
    )
    snapshot = BaselineSnapshot(
        run_dir=run_dir,
        manifest_sha256=manifest_sha256,
        source_commit=authority.source_commit,
        replay_git_head=authority.replay_git_head,
        bundle_sha256=authority.bundle_sha256,
        artifact_sha256=artifact_sha256,
        signal_row_sha256="f" * 64,
        entry_outcome_row_sha256=authority.entry_outcome_row_sha256,
        transaction_row_sha256=authority.transaction_row_sha256,
        total_return_pct=0.0,
        annualized_return_pct=0.0,
        sharpe_ratio=0.0,
        max_drawdown_pct=0.0,
        closed_trades=2,
        win_rate_pct=100.0,
        average_cash_pct=50.0,
        qualified_entries=2,
        executed_entries=2,
        next_open_buy_zone_rejections=0,
        cash_rejections=0,
    )
    return _SyntheticPublication(run_dir, profile, snapshot)


def _bind_synthetic_authority(
    monkeypatch: pytest.MonkeyPatch,
    publication: _SyntheticPublication,
    events: list[str] | None = None,
) -> None:
    original_load_json = artifacts._load_json
    original_verified_snapshot = artifacts._verified_byte_snapshot
    csv_readers = {
        "_diagnose_signals": "canslim_signals.csv",
        "_diagnose_daily_funnel": "daily_entry_funnel.csv",
        "_diagnose_entry_outcomes": "entry_attempt_outcomes.csv",
        "_diagnose_weekly_holdings": "weekly_holdings.csv",
        "_diagnose_transactions": "transactions.csv",
    }

    def resolve_profile(profile_id: str) -> BaselineAuthorityProfile:
        if events is not None:
            events.append(f"resolve:{profile_id}")
        return publication.profile

    def verify_run(
        run_dir: Path, authority: BaselineAuthority
    ) -> BaselineSnapshot:
        if events is not None:
            events.append("verify")
        assert Path(run_dir) == publication.run_dir
        assert authority is publication.profile.authority
        return publication.snapshot

    def load_json(source: artifacts._SealedSource) -> Mapping[str, object]:
        value = original_load_json(source)
        if events is not None:
            events.append(f"read-json:{source.name}")
        return value

    @contextmanager
    def verified_snapshot(
        source: artifacts._SealedSource,
    ) -> object:
        with original_verified_snapshot(source) as snapshot_handle:
            if events is not None:
                events.append(f"verified:{source.name}")
            yield snapshot_handle

    monkeypatch.setattr(
        artifacts,
        "resolve_baseline_authority_profile",
        resolve_profile,
    )
    monkeypatch.setattr(
        artifacts,
        "verify_baseline_run",
        verify_run,
    )
    monkeypatch.setattr(artifacts, "_verified_byte_snapshot", verified_snapshot)
    monkeypatch.setattr(artifacts, "_load_json", load_json)
    for function_name, source_name in csv_readers.items():
        original_reader = getattr(artifacts, function_name)

        def read_csv(
            *args: object,
            _original: object = original_reader,
            _source_name: str = source_name,
            **kwargs: object,
        ) -> object:
            result = _original(*args, **kwargs)  # type: ignore[operator]
            if events is not None:
                events.append(f"read-csv:{_source_name}")
            return result

        monkeypatch.setattr(artifacts, function_name, read_csv)


def _walk_keys(value: object) -> tuple[str, ...]:
    keys: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            keys.append(str(key).lower())
            keys.extend(_walk_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.extend(_walk_keys(child))
    return tuple(keys)


def test_artifact_diagnosis_authenticates_and_publishes_only_aggregates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break caught: diagnostic output leaks a row identity or skips reconciliation."""
    publication = _synthetic_publication(tmp_path)
    events: list[str] = []
    _bind_synthetic_authority(monkeypatch, publication, events)

    with monkeypatch.context() as guarded:
        reads = _install_diagnosis_read_guards(guarded, publication)
        payload = artifacts.diagnose_task11_artifacts(
            publication.run_dir, publication.profile
        )

    encoded = json.dumps(payload, sort_keys=True)
    assert events == [
        f"resolve:{STRICT_PROPER_BASE_TASK11_PROFILE_ID}",
        "verify",
        "verified:run_manifest.json",
        "read-json:run_manifest.json",
        "verified:canslim_signals.csv",
        "read-csv:canslim_signals.csv",
        "verified:daily_entry_funnel.csv",
        "read-csv:daily_entry_funnel.csv",
        "verified:entry_attempt_outcomes.csv",
        "read-csv:entry_attempt_outcomes.csv",
        "verified:weekly_holdings.csv",
        "read-csv:weekly_holdings.csv",
        "verified:transactions.csv",
        "read-csv:transactions.csv",
        "verified:summary.json",
        "read-json:summary.json",
    ]
    assert set(payload) == {
        "schema_version",
        "diagnosis_scope",
        "profile",
        "contract",
        "execution_verdict",
        "windows",
    }
    assert set(payload["execution_verdict"]) == {
        "source",
        "qualified_signals",
        "attempted_signals",
        "executed_attempts",
        "next_open_buy_zone_rejections",
        "rejection_counts",
        "execution_timing",
        "qualified_to_attempted_reconciled",
    }
    assert set(payload["windows"]) == {"full_2021_2025", "focus_2023_2025"}
    assert set(payload["profile"]) == {
        "profile_id",
        "scope",
        "fidelity_label",
        "fidelity_reason",
        "manifest_sha256",
        "bundle_sha256",
        "replay_git_head",
        "date_contract",
    }
    assert payload["profile"]["profile_id"] == "strict-proper-base-task11"
    assert set(payload["contract"]) == {
        "source",
        "entry_thresholds",
        "daily_evaluation",
        "sizing_and_capacity",
    }
    assert set(payload["contract"]["entry_thresholds"]) == {
        "current_growth",
        "annual_growth",
        "rs_score",
        "entry_composite_score",
    }
    assert payload["contract"]["entry_thresholds"]["current_growth"] == 0.25
    assert set(payload["contract"]["daily_evaluation"]) == {
        "signal_every_n_days",
        "require_bullish_market",
        "use_stateful_regime_gate",
    }
    assert set(payload["contract"]["sizing_and_capacity"]) == {
        "max_positions",
        "cash_deployment_threshold_pct",
        "position_risk_pct",
        "position_size_pct",
        "stop_loss_pct",
        "take_profit_pct",
    }
    assert set(payload["execution_verdict"]["rejection_counts"]) == set(
        _REJECTION_NAMES
    )
    for window_payload in payload["windows"].values():
        assert set(window_payload) == {
            "window",
            "reconciled",
            "funnel",
            "per_year",
            "signal_day_concentration",
            "weekly_open_holdings_and_cash",
            "transactions",
        }
        assert set(window_payload["window"]) == {"start", "end"}
        assert set(window_payload["funnel"]) == {
            "evaluated_symbol_days",
            "technical_setup_eligible",
            "current_growth_gate",
            "annual_growth_gate",
            "rs_score_gate",
            "entry_composite_score_gate",
            "qualified",
            "attempted",
            "executed",
            "next_open_buy_zone_rejections",
        }
        for gate_name in (
            "current_growth_gate",
            "annual_growth_gate",
            "rs_score_gate",
            "entry_composite_score_gate",
        ):
            assert set(window_payload["funnel"][gate_name]) == {
                "threshold",
                "evaluated_after_prior_gate",
                "passed",
                "below_threshold",
                "unavailable",
            }
        assert set(window_payload["per_year"]) == {"2024"}
        assert set(window_payload["per_year"]["2024"]) == {
            "qualified",
            "attempted",
            "executed",
            "next_open_buy_zone_rejected",
        }
        assert set(window_payload["signal_day_concentration"]) == {
            "active_days",
            "mean_qualified_per_active_day",
            "maximum_qualified_per_active_day",
        }
        assert set(window_payload["weekly_open_holdings_and_cash"]) == {
            "week_count",
            "average_open_holdings",
            "maximum_open_holdings",
            "weeks_with_open_holdings",
            "average_cash_pct",
        }
        assert set(window_payload["transactions"]) == {
            "buy_transaction_count",
            "exit_transaction_count",
            "exit_reason_counts",
        }
        assert set(window_payload["transactions"]["exit_reason_counts"]) == {"ema"}
    assert payload["execution_verdict"]["qualified_signals"] == 2
    assert payload["execution_verdict"]["executed_attempts"] == 2
    assert payload["windows"]["focus_2023_2025"]["funnel"]["qualified"] == 2
    assert all(value not in encoded for value in _PRIVATE_SENTINELS.values())
    assert not set(_PRIVATE_SENTINELS).intersection(_walk_keys(payload))
    assert set(reads) == {
        "run_manifest.json",
        "summary.json",
        "canslim_signals.csv",
        "daily_entry_funnel.csv",
        "entry_attempt_outcomes.csv",
        "weekly_holdings.csv",
        "transactions.csv",
    }


def test_artifact_diagnosis_rejects_a_foreign_authority_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break caught: a caller substitutes another closed profile for Task 11."""
    publication = _synthetic_publication(tmp_path)
    events: list[str] = []
    _bind_synthetic_authority(monkeypatch, publication, events)
    foreign_profile = BaselineAuthorityProfile(
        profile_id=DEFAULT_BASELINE_PROFILE_ID,
        authority=publication.profile.authority,
        scope="corrected_task6",
        fidelity_label="strict_canslim",
        fidelity_reason="foreign synthetic authority",
    )

    with pytest.raises(ValueError, match="exact canonical"):
        artifacts.diagnose_task11_artifacts(
            publication.run_dir, foreign_profile
        )
    assert events == [f"resolve:{STRICT_PROPER_BASE_TASK11_PROFILE_ID}"]


def test_artifact_diagnosis_rejects_bytes_changed_after_authority_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break caught: a re-used verifier result permits a changed signal ledger."""
    publication = _synthetic_publication(tmp_path)
    _bind_synthetic_authority(monkeypatch, publication)
    signal_path = publication.run_dir / "canslim_signals.csv"
    signal_path.write_text(
        signal_path.read_text(encoding="utf-8").replace("0.78", "0.79", 1),
        encoding="utf-8",
        newline="",
    )

    with pytest.raises(ValueError, match="SHA-256 differs"):
        artifacts.diagnose_task11_artifacts(
            publication.run_dir, publication.profile
        )


def test_ca_reader_authenticates_private_rows_before_aggregation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break caught: C/A cohort rows are parsed without checking their sealed digest."""
    path = tmp_path / "canslim_signals.csv"
    path.write_text(
        "symbol,signal_date,technical_setup_eligible,c_score,current_growth,a_score,annual_growth\n"
        f"{_PRIVATE_SYMBOL},2024-06-03,True,0.8,0.3,0.7,0.3\n",
        encoding="utf-8",
        newline="",
    )
    source = ca._SealedSource("canslim_signals.csv", path, _sha256(path))
    monkeypatch.setattr(ca, "_EXPECTED_COHORT_SIZE", 1)

    rows = ca._read_fixed_cohort(source)
    assert tuple(rows) == (_PRIVATE_SYMBOL,)
    path.write_text(
        path.read_text(encoding="utf-8").replace("0.8", "0.9"),
        encoding="utf-8",
        newline="",
    )
    with pytest.raises(ValueError, match="changed before parsing"):
        ca._read_fixed_cohort(source)


def test_ca_reconciliation_accepts_one_inclusive_state_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a valid one-boundary state stream is rejected as unordered."""
    signal_date = date(2024, 6, 3)
    row = ca._SignalRow(
        symbol=_PRIVATE_SYMBOL,
        signal_date=signal_date,
        c_score=0.0,
        current_growth=None,
        a_score=0.0,
        annual_growth=None,
    )
    snapshot = {
        "quarterly_income": pd.DataFrame(),
        "annual_income": pd.DataFrame(),
        "balance_sheet": pd.DataFrame(),
        "company_info": {
            "shares_outstanding": None,
            "held_percent_institutions": None,
            "institution_count": None,
            "prev_institution_count": None,
        },
    }

    class _OneBoundaryBundle:
        def iter_fundamental_state_boundaries(
            self,
            date_bounds: Mapping[str, tuple[date, date]],
            *,
            include_provenance: bool = False,
        ) -> object:
            assert date_bounds == {_PRIVATE_SYMBOL: (signal_date, signal_date)}
            assert include_provenance is True
            yield _PRIVATE_SYMBOL, signal_date, snapshot

    monkeypatch.setattr(ca, "_EXPECTED_COHORT_SIZE", 1)
    c_aggregates = ca._new_gate_aggregates()
    a_aggregates = ca._new_gate_aggregates()

    reconciliation = ca._reconcile_boundaries(
        _OneBoundaryBundle(),  # type: ignore[arg-type]
        {_PRIVATE_SYMBOL: (signal_date, signal_date)},
        {_PRIVATE_SYMBOL: [row]},
        c_aggregates,
        a_aggregates,
    )

    assert reconciliation == {
        "c_cohort_size": 1,
        "a_cohort_size": 0,
        "c_scalar_rows_checked": 1,
        "a_scalar_rows_checked": 0,
        "c_availability_rows_checked": 1,
        "a_availability_rows_checked": 0,
        "mismatch_count": 0,
        "passed": True,
    }
    assert ca._render_gate_aggregates(c_aggregates)["unavailable_count"] == 1


def test_ca_aggregate_contract_rejects_private_keys_and_future_filings() -> None:
    """Break caught: C/A evidence leaks identities or accepts look-ahead provenance."""
    trace = CTrace(
        score=0.8,
        current_growth=0.30,
        metric_family=MetricFamily.DILUTED_EPS,
        terminal_reason=TraceReason.COMPLETE,
        current_period_end=date(2024, 3, 31),
        prior_period_end=date(2023, 3, 31),
        current_public_date=date(2024, 5, 1),
        prior_public_date=date(2023, 5, 1),
        current_value=1.3,
        prior_value=1.0,
    )
    aggregates = ca._new_gate_aggregates()
    ca._validate_trace(trace, date(2024, 6, 3), "C")
    ca._add_gate_aggregate(aggregates, date(2024, 6, 3), trace, "pass")
    rendered = ca._render_gate_aggregates(aggregates)
    payload = {"schema_version": 1, "c": rendered}

    ca._require_aggregate_only_payload(payload)
    assert set(rendered) == {
        "cohort_size",
        "pass_count",
        "finite_below_threshold_count",
        "unavailable_count",
        "by_year",
        "metric_family_by_outcome_counts",
        "unavailable_terminal_reason_counts",
        "public_date_pair_visibility_counts",
    }
    assert set(rendered["by_year"]) == {"2023", "2024", "2025"}
    assert set(rendered["public_date_pair_visibility_counts"]) == {
        "both_visible",
        "current_only",
        "prior_only",
        "neither_visible",
    }
    assert rendered["cohort_size"] == 1
    assert rendered["pass_count"] == 1
    assert "2024-05-01" not in json.dumps(rendered, sort_keys=True)
    for private_key in ("symbol", "ticker", "cik", "accession", "path", "url"):
        with pytest.raises(AssertionError, match="private field"):
            ca._require_aggregate_only_payload(
                {"c": rendered, private_key: _PRIVATE_SENTINELS[private_key]}
            )
    with pytest.raises(ValueError, match="filing after the signal date"):
        ca._validate_trace(
            replace(trace, current_public_date=date(2024, 6, 4)),
            date(2024, 6, 3),
            "C",
        )


def _legacy_ca_diagnosis_assembles_only_aggregate_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break caught: C/A assembly reads unverified inputs or publishes private data."""
    publication = _synthetic_publication(tmp_path)
    events: list[str] = []
    bundle_path = publication.run_dir / "pit_bundle.sqlite3"
    provenance_path = publication.run_dir / "fundamentals_provenance.json"
    fundamentals_path = publication.run_dir / "fundamentals.csv"
    audit_path = publication.run_dir / "fundamentals_audit.csv"
    bundle_sha256 = _sha256(bundle_path)
    provenance_sha256 = _sha256(provenance_path)
    fundamentals_sha256 = _sha256(fundamentals_path)
    audit_sha256 = _sha256(audit_path)
    signal_date = date(2024, 6, 3)

    quarterly = pd.DataFrame(
        {
            pd.Timestamp("2023-03-31"): [1.0],
            pd.Timestamp("2024-03-31"): [1.4],
        },
        index=["Diluted EPS"],
    )
    quarterly.attrs[PIT_PUBLIC_DATES_ATTR] = {
        "2023-03-31": "2023-05-01",
        "2024-03-31": "2024-05-01",
    }
    annual = pd.DataFrame(
        {
            pd.Timestamp("2021-12-31"): [1.0, 100.0],
            pd.Timestamp("2022-12-31"): [1.2, 120.0],
            pd.Timestamp("2023-12-31"): [1.5, 150.0],
        },
        index=["Diluted EPS", "Net Income"],
    )
    annual.attrs[PIT_PUBLIC_DATES_ATTR] = {
        "2021-12-31": "2022-02-15",
        "2022-12-31": "2023-02-15",
        "2023-12-31": "2024-02-15",
    }
    balance = pd.DataFrame(
        {pd.Timestamp("2023-12-31"): [500.0]},
        index=["Stockholders Equity"],
    )
    balance.attrs[PIT_PUBLIC_DATES_ATTR] = {
        "2023-12-31": "2024-02-15"
    }
    snapshot = {
        "quarterly_income": quarterly,
        "annual_income": annual,
        "balance_sheet": balance,
        "company_info": {
            "shares_outstanding": None,
            "held_percent_institutions": None,
            "institution_count": None,
            "prev_institution_count": None,
        },
    }

    class _FakeBundle:
        def __init__(self, data: bytes, expected_sha256: str) -> None:
            if type(data) is not bytes:
                raise ValueError("fake bundle requires immutable bytes")
            actual_sha256 = hashlib.sha256(data).hexdigest()
            if actual_sha256 != expected_sha256:
                raise ValueError("fake bundle digest mismatch")
            self._sha256 = actual_sha256
            self._metadata = {
                "fundamentals_provenance_sha256": provenance_sha256,
                "fundamentals_source_sha256": fundamentals_sha256,
            }

        @property
        def sha256(self) -> str:
            events.append("source-chain:bundle-sha256")
            return self._sha256

        @property
        def metadata(self) -> Mapping[str, str]:
            events.append("source-chain:bundle-metadata")
            return self._metadata

        def __enter__(self) -> _FakeBundle:
            events.append("bundle:enter")
            return self

        def __exit__(self, *_args: object) -> None:
            events.append("bundle:exit")
            return None

        def iter_fundamental_state_boundaries(
            self,
            date_bounds: Mapping[str, tuple[date, date]],
            *,
            include_provenance: bool = False,
        ) -> object:
            events.append("assemble:fundamental-boundaries")
            assert date_bounds == {_PRIVATE_SYMBOL: (signal_date, signal_date)}
            assert include_provenance is True
            yield _PRIVATE_SYMBOL, signal_date, snapshot

    original_verified_snapshot = ca._verified_byte_snapshot
    original_load_json = ca._load_json
    original_verify_snapshot = ca._verify_snapshot
    original_bundle_bytes = ca._authenticated_bundle_bytes
    original_read_cohort = ca._read_fixed_cohort

    @contextmanager
    def verified_snapshot(source: ca._SealedSource) -> object:
        with original_verified_snapshot(source) as snapshot_handle:
            events.append(f"verified:{source.name}")
            yield snapshot_handle

    def load_json(source: ca._SealedSource) -> Mapping[str, object]:
        value = original_load_json(source)
        events.append(f"read-json:{source.name}")
        return value

    def verify_sidecar(source: ca._SealedSource) -> None:
        original_verify_snapshot(source)
        events.append(f"read-sidecar:{source.name}")

    def authenticated_bundle_bytes(source: ca._SealedSource) -> bytes:
        data = original_bundle_bytes(source)
        events.append(f"read-bundle:{source.name}")
        return data

    def read_cohort(
        source: ca._SealedSource,
    ) -> dict[str, list[ca._SignalRow]]:
        rows = original_read_cohort(source)
        events.append(f"read-csv:{source.name}")
        return rows

    def resolve_profile(profile_id: str) -> BaselineAuthorityProfile:
        events.append(f"resolve:{profile_id}")
        return publication.profile

    def verify_run(
        run_dir: Path, authority: BaselineAuthority
    ) -> BaselineSnapshot:
        events.append("verify")
        assert Path(run_dir) == publication.run_dir
        assert authority is publication.profile.authority
        return publication.snapshot

    def open_bundle(
        data: bytes, *, expected_sha256: str
    ) -> _FakeBundle:
        events.append("open-bundle")
        return _FakeBundle(data, expected_sha256)

    def reconcile_boundaries(
        _bundle: object,
        date_bounds: Mapping[str, tuple[date, date]],
        rows_by_symbol: Mapping[str, list[ca._SignalRow]],
        c_aggregates: dict[str, object],
        a_aggregates: dict[str, object],
    ) -> dict[str, int | bool]:
        events.append("assemble:reconcile-boundaries")
        assert date_bounds == {_PRIVATE_SYMBOL: (signal_date, signal_date)}
        row = rows_by_symbol[_PRIVATE_SYMBOL][0]
        assert (row.c_score, row.current_growth) == (0.78, 0.40)
        assert row.a_score == pytest.approx(0.5764705882352941)
        assert row.annual_growth == pytest.approx(0.25)
        c_trace = CTrace(
            score=0.78,
            current_growth=0.40,
            metric_family=MetricFamily.DILUTED_EPS,
            terminal_reason=TraceReason.COMPLETE,
            current_period_end=date(2024, 3, 31),
            prior_period_end=date(2023, 3, 31),
            current_public_date=date(2024, 5, 1),
            prior_public_date=date(2023, 5, 1),
            current_value=1.4,
            prior_value=1.0,
        )
        a_trace = ATrace(
            score=0.5764705882352941,
            annual_growth=0.25,
            roe=0.30,
            metric_family=MetricFamily.DILUTED_EPS,
            terminal_reason=TraceReason.COMPLETE,
            current_period_end=date(2023, 12, 31),
            prior_period_end=date(2022, 12, 31),
            current_public_date=date(2024, 2, 15),
            prior_public_date=date(2023, 2, 15),
            current_value=1.5,
            prior_value=1.2,
        )
        ca._add_gate_aggregate(c_aggregates, signal_date, c_trace, "pass")
        ca._add_gate_aggregate(a_aggregates, signal_date, a_trace, "pass")
        return {
            "c_cohort_size": 1,
            "a_cohort_size": 1,
            "c_scalar_rows_checked": 1,
            "a_scalar_rows_checked": 1,
            "c_availability_rows_checked": 1,
            "a_availability_rows_checked": 1,
            "mismatch_count": 0,
            "passed": True,
        }

    monkeypatch.setattr(
        ca,
        "resolve_baseline_authority_profile",
        resolve_profile,
    )
    monkeypatch.setattr(
        ca,
        "verify_baseline_run",
        verify_run,
    )
    monkeypatch.setattr(ca, "TASK11_BUNDLE_SHA256", bundle_sha256)
    monkeypatch.setattr(ca, "TASK11_PROVENANCE_SHA256", provenance_sha256)
    monkeypatch.setattr(ca, "TASK11_FUNDAMENTALS_SHA256", fundamentals_sha256)
    monkeypatch.setattr(ca, "TASK11_FUNDAMENTALS_AUDIT_SHA256", audit_sha256)
    monkeypatch.setattr(ca, "_EXPECTED_COHORT_SIZE", 1)
    monkeypatch.setattr(
        ca,
        "_EXPECTED_COUNTS",
        {
            "c": {"pass": 1, "finite_below_threshold": 0, "unavailable": 0},
            "a": {"pass": 1, "finite_below_threshold": 0, "unavailable": 0},
        },
    )
    monkeypatch.setattr(ca, "_verified_byte_snapshot", verified_snapshot)
    monkeypatch.setattr(ca, "_load_json", load_json)
    monkeypatch.setattr(ca, "_verify_snapshot", verify_sidecar)
    monkeypatch.setattr(ca, "_authenticated_bundle_bytes", authenticated_bundle_bytes)
    monkeypatch.setattr(ca, "_read_fixed_cohort", read_cohort)
    monkeypatch.setattr(ca, "_reconcile_boundaries", reconcile_boundaries)
    monkeypatch.setattr(
        ca.PITDataBundle,
        "from_authenticated_bytes",
        staticmethod(open_bundle),
    )

    payload = ca.diagnose_task11_ca_provenance(
        publication.run_dir, publication.profile
    )

    encoded = json.dumps(payload, sort_keys=True)
    assert events == [
        f"resolve:{STRICT_PROPER_BASE_TASK11_PROFILE_ID}",
        "verify",
        "verified:run_manifest.json",
        "read-json:run_manifest.json",
        "verified:fundamentals_provenance.json",
        "read-json:fundamentals_provenance.json",
        "verified:fundamentals.csv",
        "read-sidecar:fundamentals.csv",
        "verified:fundamentals_audit.csv",
        "read-sidecar:fundamentals_audit.csv",
        "verified:pit_bundle",
        "read-bundle:pit_bundle",
        "open-bundle",
        "bundle:enter",
        "source-chain:bundle-sha256",
        "source-chain:bundle-metadata",
        "source-chain:bundle-metadata",
        "verified:canslim_signals.csv",
        "read-csv:canslim_signals.csv",
        "assemble:reconcile-boundaries",
        "bundle:exit",
    ]
    assert set(payload) == {
        "schema_version",
        "diagnosis_scope",
        "profile",
        "source_chain",
        "window",
        "reconciliation",
        "c",
        "a",
    }
    assert payload["schema_version"] == 1
    assert payload["diagnosis_scope"] == (
        "task11_ca_provenance_not_strategy_optimization"
    )
    assert set(payload["profile"]) == {
        "profile_id",
        "scope",
        "fidelity_label",
        "fidelity_reason",
        "manifest_sha256",
        "bundle_sha256",
        "replay_git_head",
        "date_contract",
    }
    assert set(payload["source_chain"]) == {
        "run_manifest.json",
        "canslim_signals.csv",
        "pit_bundle",
        "fundamentals_provenance.json",
        "fundamentals.csv",
        "fundamentals_audit.csv",
    }
    for source in payload["source_chain"].values():
        assert set(source) == {"sha256", "verified"}
        assert source["verified"] is True
    assert set(payload["window"]) == {"start", "end"}
    assert set(payload["reconciliation"]) == {
        "c_cohort_size",
        "a_cohort_size",
        "scalar_rows_checked",
        "availability_rows_checked",
        "mismatch_count",
        "passed",
    }
    assert set(payload["reconciliation"]["scalar_rows_checked"]) == {"c", "a"}
    assert set(payload["reconciliation"]["availability_rows_checked"]) == {
        "c",
        "a",
    }
    for gate_name in ("c", "a"):
        gate = payload[gate_name]
        assert set(gate) == {
            "cohort_size",
            "pass_count",
            "finite_below_threshold_count",
            "unavailable_count",
            "by_year",
            "metric_family_by_outcome_counts",
            "unavailable_terminal_reason_counts",
            "public_date_pair_visibility_counts",
        }
        assert set(gate["by_year"]) == {"2023", "2024", "2025"}
        for year in gate["by_year"].values():
            assert set(year) == {
                "cohort_size",
                "pass_count",
                "finite_below_threshold_count",
                "unavailable_count",
            }
        assert set(gate["metric_family_by_outcome_counts"]) == {
            "pass",
            "finite_below_threshold",
            "unavailable",
        }
        for families in gate["metric_family_by_outcome_counts"].values():
            assert set(families) == {
                "diluted_eps",
                "basic_eps",
                "net_income",
                "unavailable",
            }
        assert set(gate["unavailable_terminal_reason_counts"]) == {
            "no_visible_observation",
            "no_comparable_prior_period",
            "insufficient_annual_history",
            "nonfinite_current_value",
            "nonfinite_prior_value",
            "zero_prior_value",
            "negative_prior_value",
            "evaluator_exception",
        }
        assert set(gate["public_date_pair_visibility_counts"]) == {
            "both_visible",
            "current_only",
            "prior_only",
            "neither_visible",
        }
        assert gate["cohort_size"] == 1
        assert gate["pass_count"] == 1
    assert all(value not in encoded for value in _PRIVATE_SENTINELS.values())
    assert _PRIVATE_SYMBOL not in encoded
    assert str(publication.run_dir) not in encoded
    assert bundle_path.read_bytes().decode("ascii") not in encoded
    assert "PRIVATE-SIDECAR-FILING" not in encoded
    assert "PRIVATE-PROVENANCE-FILING" not in encoded
    assert "314159265.3589" not in encoded


def _bind_ca_authority(
    monkeypatch: pytest.MonkeyPatch,
    publication: _SyntheticPublication,
) -> None:
    def resolve_profile(profile_id: str) -> BaselineAuthorityProfile:
        assert profile_id == "strict-proper-base-task11"
        return publication.profile

    def verify_run(
        run_dir: Path, authority: BaselineAuthority
    ) -> BaselineSnapshot:
        assert Path(run_dir) == publication.run_dir
        assert authority is publication.profile.authority
        return publication.snapshot

    monkeypatch.setattr(ca, "resolve_baseline_authority_profile", resolve_profile)
    monkeypatch.setattr(ca, "verify_baseline_run", verify_run)
    monkeypatch.setattr(
        ca, "TASK11_BUNDLE_SHA256", _sha256(publication.run_dir / "pit_bundle.sqlite3")
    )
    monkeypatch.setattr(
        ca,
        "TASK11_PROVENANCE_SHA256",
        _sha256(publication.run_dir / "fundamentals_provenance.json"),
    )
    monkeypatch.setattr(
        ca,
        "TASK11_FUNDAMENTALS_SHA256",
        _sha256(publication.run_dir / "fundamentals.csv"),
    )
    monkeypatch.setattr(
        ca,
        "TASK11_FUNDAMENTALS_AUDIT_SHA256",
        _sha256(publication.run_dir / "fundamentals_audit.csv"),
    )
    monkeypatch.setattr(ca, "_EXPECTED_COHORT_SIZE", 2)
    monkeypatch.setattr(
        ca,
        "_EXPECTED_COUNTS",
        {
            "c": {"pass": 2, "finite_below_threshold": 0, "unavailable": 0},
            "a": {"pass": 2, "finite_below_threshold": 0, "unavailable": 0},
        },
    )


def _install_diagnosis_read_guards(
    monkeypatch: pytest.MonkeyPatch,
    publication: _SyntheticPublication,
) -> list[str]:
    allowed = {
        (publication.run_dir / name).resolve()
        for name in (
            "run_manifest.json",
            "summary.json",
            "canslim_signals.csv",
            "daily_entry_funnel.csv",
            "entry_attempt_outcomes.csv",
            "weekly_holdings.csv",
            "transactions.csv",
            "pit_bundle.sqlite3",
            "fundamentals_provenance.json",
            "fundamentals.csv",
            "fundamentals_audit.csv",
        )
    }
    original_path_open = Path.open
    reads: list[str] = []

    def guarded_path_open(
        path: Path, mode: str = "r", *args: object, **kwargs: object
    ) -> object:
        resolved = Path(path).resolve()
        if "r" in mode:
            if resolved not in allowed:
                raise AssertionError(f"diagnosis attempted an unexpected file read: {resolved}")
            reads.append(resolved.name)
        return original_path_open(path, mode, *args, **kwargs)

    def forbidden_provider(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("diagnosis attempted a live fundamental provider call")

    monkeypatch.setattr(Path, "open", guarded_path_open)
    monkeypatch.setattr(
        ca.PITDataBundle, "fundamentals_provider", forbidden_provider
    )
    return reads


def _refresh_publication(publication: _SyntheticPublication) -> _SyntheticPublication:
    authority = replace(
        publication.profile.authority,
        bundle_sha256=_sha256(publication.run_dir / "pit_bundle.sqlite3"),
        manifest_sha256=_sha256(publication.run_dir / "run_manifest.json"),
    )
    profile = replace(publication.profile, authority=authority)
    snapshot = replace(
        publication.snapshot,
        manifest_sha256=authority.manifest_sha256,
        bundle_sha256=authority.bundle_sha256,
    )
    return _SyntheticPublication(publication.run_dir, profile, snapshot)


def test_ca_public_diagnosis_reconciles_real_sqlite_state_transitions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break caught: public C/A diagnosis substitutes a fake reconciliation path."""
    publication = _synthetic_publication(tmp_path)
    _bind_ca_authority(monkeypatch, publication)
    with monkeypatch.context() as guarded:
        reads = _install_diagnosis_read_guards(guarded, publication)
        payload = ca.diagnose_task11_ca_provenance(
            publication.run_dir, publication.profile
        )

    assert payload["profile"]["profile_id"] == "strict-proper-base-task11"
    assert payload["window"] == {"start": "2023-01-01", "end": "2025-12-31"}
    assert payload["reconciliation"] == {
        "c_cohort_size": 2,
        "a_cohort_size": 2,
        "scalar_rows_checked": {"c": 2, "a": 2},
        "availability_rows_checked": {"c": 2, "a": 2},
        "mismatch_count": 0,
        "passed": True,
    }
    assert payload["c"]["pass_count"] == 2
    assert payload["a"]["pass_count"] == 2
    assert payload["c"]["metric_family_by_outcome_counts"]["pass"] == {
        "diluted_eps": 2,
        "basic_eps": 0,
        "net_income": 0,
        "unavailable": 0,
    }
    assert payload["source_chain"] == {
        "canslim_signals.csv": {
            "sha256": publication.profile.authority.artifact_sha256[
                "canslim_signals.csv"
            ],
            "verified": True,
        },
        "fundamentals.csv": {
            "sha256": _sha256(publication.run_dir / "fundamentals.csv"),
            "verified": True,
        },
        "fundamentals_audit.csv": {
            "sha256": _sha256(publication.run_dir / "fundamentals_audit.csv"),
            "verified": True,
        },
        "fundamentals_provenance.json": {
            "sha256": _sha256(
                publication.run_dir / "fundamentals_provenance.json"
            ),
            "verified": True,
        },
        "pit_bundle": {
            "sha256": publication.profile.authority.bundle_sha256,
            "verified": True,
        },
        "run_manifest.json": {
            "sha256": publication.profile.authority.manifest_sha256,
            "verified": True,
        },
    }
    assert set(reads) == {
        "run_manifest.json",
        "canslim_signals.csv",
        "pit_bundle.sqlite3",
        "fundamentals_provenance.json",
        "fundamentals.csv",
        "fundamentals_audit.csv",
    }
    encoded = json.dumps(payload, sort_keys=True)
    assert all(value not in encoded for value in _PRIVATE_SENTINELS.values())
    assert not set(_PRIVATE_SENTINELS).intersection(_walk_keys(payload))


@pytest.mark.parametrize(
    ("field", "forged_value"),
    [
        ("profile_id", "corrected-task6"),
    ],
)
def test_ca_public_diagnosis_rejects_foreign_or_forged_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    forged_value: str,
) -> None:
    """Break caught: caller-controlled profile labels cross the C/A trust boundary."""
    publication = _synthetic_publication(tmp_path)
    _bind_ca_authority(monkeypatch, publication)
    forged = replace(publication.profile, **{field: forged_value})

    with pytest.raises(ValueError, match="exact canonical"):
        ca.diagnose_task11_ca_provenance(publication.run_dir, forged)


@pytest.mark.parametrize(
    ("section", "key", "message"),
    [
        ("input_sha256", "pit_bundle", "canonical bundle"),
        ("input_sha256", "fundamentals_provenance", "bind provenance"),
        ("bundle_metadata", "fundamentals_provenance_sha256", "bind provenance"),
        ("bundle_metadata", "fundamentals_source_sha256", "bind fundamentals"),
    ],
)
def test_ca_public_diagnosis_rejects_manifest_source_binding_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    section: str,
    key: str,
    message: str,
) -> None:
    """Break caught: a reauthenticated manifest can redirect one source identity."""
    publication = _synthetic_publication(tmp_path)
    manifest_path = publication.run_dir / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[section][key] = "0" * 64
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    publication = _refresh_publication(publication)
    _bind_ca_authority(monkeypatch, publication)

    with pytest.raises(ValueError, match=message):
        ca.diagnose_task11_ca_provenance(
            publication.run_dir, publication.profile
        )


@pytest.mark.parametrize(
    ("digest_field", "message"),
    [
        ("fundamentals_sha256", "fundamentals identity"),
        ("fundamentals_audit_sha256", "audit identity"),
    ],
)
def test_ca_public_diagnosis_rejects_provenance_sidecar_digest_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    digest_field: str,
    message: str,
) -> None:
    """Break caught: authenticated provenance names a foreign private sidecar."""
    publication = _synthetic_publication(tmp_path)
    provenance_path = publication.run_dir / "fundamentals_provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance[digest_field] = "0" * 64
    provenance_path.write_text(json.dumps(provenance, sort_keys=True), encoding="utf-8")
    provenance_digest = _sha256(provenance_path)
    bundle_path = publication.run_dir / "pit_bundle.sqlite3"
    with closing(sqlite3.connect(bundle_path)) as connection:
        connection.execute(
            "UPDATE dataset_metadata SET value = ? WHERE key = 'fundamentals_provenance_sha256'",
            (provenance_digest,),
        )
        connection.commit()
    manifest_path = publication.run_dir / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["input_sha256"]["pit_bundle"] = _sha256(bundle_path)
    manifest["input_sha256"]["fundamentals_provenance"] = provenance_digest
    manifest["bundle_metadata"][
        "fundamentals_provenance_sha256"
    ] = provenance_digest
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    publication = _refresh_publication(publication)
    _bind_ca_authority(monkeypatch, publication)

    with pytest.raises(ValueError, match=message):
        ca.diagnose_task11_ca_provenance(
            publication.run_dir, publication.profile
        )


def test_ca_public_diagnosis_rejects_opened_bundle_metadata_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break caught: authenticated bundle bytes carry a foreign source digest."""
    publication = _synthetic_publication(tmp_path)
    bundle_path = publication.run_dir / "pit_bundle.sqlite3"
    with closing(sqlite3.connect(bundle_path)) as connection:
        connection.execute(
            "UPDATE dataset_metadata SET value = ? WHERE key = 'fundamentals_source_sha256'",
            ("0" * 64,),
        )
        connection.commit()
    manifest_path = publication.run_dir / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["input_sha256"]["pit_bundle"] = _sha256(bundle_path)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    publication = _refresh_publication(publication)
    _bind_ca_authority(monkeypatch, publication)

    with pytest.raises(ValueError, match="bundle metadata differs from sealed fundamentals"):
        ca.diagnose_task11_ca_provenance(
            publication.run_dir, publication.profile
        )


def test_ca_public_diagnosis_rejects_verified_bundle_identity_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break caught: verifier snapshot metadata disagrees with canonical authority."""
    publication = _synthetic_publication(tmp_path)
    publication = replace(
        publication,
        snapshot=replace(publication.snapshot, bundle_sha256="0" * 64),
    )
    _bind_ca_authority(monkeypatch, publication)

    with pytest.raises(ValueError, match="bundle differs from canonical authority"):
        ca.diagnose_task11_ca_provenance(
            publication.run_dir, publication.profile
        )
