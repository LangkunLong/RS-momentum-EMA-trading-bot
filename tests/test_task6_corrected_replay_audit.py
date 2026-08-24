from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pandas as pd
import pytest


AUDITOR = (
    Path(__file__).resolve().parents[1]
    / ".superpowers"
    / "sdd"
    / "2026-08-23-canonical-canslim-entry"
    / "task-6-corrected-replay-audit.py"
)


def _load_auditor():
    spec = importlib.util.spec_from_file_location("task6_corrected_replay_audit", AUDITOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_normalized_numeric_comparison_allows_csv_float_roundtrip_only() -> None:
    audit = _load_auditor()

    assert audit._normalized_values_equal(
        ("number", "400.23900000000003"),
        ("number", "400.23899999999998"),
    )
    assert not audit._normalized_values_equal(
        ("number", "400.239"),
        ("number", "400.24"),
    )


def test_independent_quarter_match_accepts_28_days_but_rejects_29_and_ties() -> None:
    audit = _load_auditor()

    accepted = pd.Series(
        [10.0, 15.0],
        index=pd.to_datetime(["2023-03-03", "2024-03-31"]),
    )
    rejected = pd.Series(
        [10.0, 15.0],
        index=pd.to_datetime(["2023-03-02", "2024-03-31"]),
    )
    tied = pd.Series(
        [10.0, 11.0, 15.0],
        index=pd.to_datetime(["2023-03-30", "2023-04-01", "2024-03-31"]),
    )

    assert audit._independent_latest_yoy_pair(accepted) == (15.0, 10.0)
    assert audit._independent_latest_yoy_pair(rejected) is None
    assert audit._independent_latest_yoy_pair(tied) is None


def test_independent_c_does_not_call_production_evaluate_c(monkeypatch) -> None:
    audit = _load_auditor()
    import core.canslim.c_current_earnings as production_c

    def explode(*_args, **_kwargs):
        raise AssertionError("production evaluate_c must not be called")

    monkeypatch.setattr(production_c, "evaluate_c", explode)
    quarterly = pd.DataFrame(
        {
            pd.Timestamp("2023-03-03"): [10.0],
            pd.Timestamp("2024-03-31"): [15.0],
        },
        index=["Diluted EPS"],
    )

    _score, growth = audit._independent_evaluate_c(quarterly)

    assert growth == pytest.approx(0.5)


def test_earnings_priority_is_diluted_then_basic_then_net_income() -> None:
    audit = _load_auditor()
    frame = pd.DataFrame(
        {
            pd.Timestamp("2023-12-31"): [100.0, 10.0, 1.0],
            pd.Timestamp("2024-12-31"): [200.0, 12.0, 1.5],
        },
        index=["Net Income", "Basic EPS", "Diluted EPS"],
    )

    selected = audit._independent_earnings_series(frame)

    assert selected.name == "Diluted EPS"
    assert selected.iloc[-1] == pytest.approx(1.5)
    assert audit._independent_earnings_series(frame.drop(index="Diluted EPS")).name == "Basic EPS"
    assert audit._independent_earnings_series(
        frame.drop(index=["Diluted EPS", "Basic EPS"])
    ).name == "Net Income"

    _score, growth, _roe = audit._independent_evaluate_a(frame, pd.DataFrame())
    assert growth == pytest.approx(0.5)


def test_cah_invariant_requires_rows_and_reviewed_statement_facts() -> None:
    audit = _load_auditor()
    empty_snapshot = {
        "quarterly_income": pd.DataFrame(),
        "annual_income": pd.DataFrame(),
        "balance_sheet": pd.DataFrame(),
    }

    with pytest.raises(AssertionError, match="CAH corrected fundamental rows"):
        audit._require_reviewed_cah_invariant(pd.DataFrame(), empty_snapshot)

    cah_rows = pd.DataFrame(
        {
            "statement_type": (
                ["quarterly"] * 70 + ["annual"] * 30 + ["balance"] * 157
            )
        }
    )
    snapshot = {
        "quarterly_income": pd.DataFrame(
            {pd.Timestamp("2024-03-31"): [1.0]}, index=["Diluted EPS"]
        ),
        "annual_income": pd.DataFrame(
            {
                pd.Timestamp("2023-12-31"): [1.0, 10.0],
                pd.Timestamp("2024-12-31"): [1.5, 15.0],
            },
            index=["Diluted EPS", "Net Income"],
        ),
        "balance_sheet": pd.DataFrame(
            {
                pd.Timestamp("2024-12-31"): [100.0],
            },
            index=["Stockholders Equity"],
        ),
    }

    audit._require_reviewed_cah_invariant(cah_rows, snapshot)


def test_checkpoint_fingerprint_is_bound_to_exact_builtin_strategy_identity() -> None:
    audit = _load_auditor()
    payload = {
        "schema_version": 3,
        "strategy_identity": dict(audit._BUILTIN_STRATEGY_IDENTITY),
        "code_identity": "a" * 40,
    }

    expected = audit._stable_digest(payload)
    mutated = dict(payload)
    mutated["strategy_identity"] = {
        **payload["strategy_identity"],
        "version": 2,
    }

    assert expected != audit._stable_digest(mutated)
    audit._require_builtin_strategy_identity(payload["strategy_identity"])
    with pytest.raises(AssertionError, match="strategy identity"):
        audit._require_builtin_strategy_identity(mutated["strategy_identity"])


def _regeneration_fixture(audit):
    manifest = {
        "bundle_sha256": audit._BUNDLE_SHA256,
        "metadata": {
            "warmup_start": "2020-01-01",
            "evaluation_start": "2021-01-01",
            "data_cutoff": "2025-12-31",
            "fundamentals_submissions_archive_sha256": "1" * 64,
            "fundamentals_companyfacts_archive_sha256": "2" * 64,
            "fundamentals_identity_manifest_csv_sha256": "3" * 64,
            "fundamentals_source_sha256": "4" * 64,
            "fundamentals_provenance_sha256": "5" * 64,
        },
    }
    manifest_sha = audit._stable_digest(manifest)
    regeneration = {
        "schema_version": 3,
        "status": "complete",
        "correction_git_head": audit._REGENERATION_GIT_SHA,
        "date_contract": {
            "warmup_start": "2020-01-01",
            "evaluation_start": "2021-01-01",
            "data_cutoff": "2025-12-31",
        },
        "source_archives_sha256": {
            "submissions": "1" * 64,
            "companyfacts": "2" * 64,
        },
        "correction_producer_identity_manifest_csv_sha256": "3" * 64,
        "normalized_files_sha256": {
            "fundamentals.csv": "4" * 64,
            "fundamentals_provenance.json": "5" * 64,
        },
        "validated_counts": {
            "xom": 209,
            "xom_quarterly": 71,
            "xom_annual": 30,
            "xom_balance": 108,
        },
        "validations": {
            "xom_reviewed_cik": "0000034088",
            "xom_mapping_basis": "reviewed_baseline_cik",
            "bundle": "verify_pit_bundle_passed",
        },
        "bundle_sha256": audit._BUNDLE_SHA256,
        "bundle_manifest_sha256": manifest_sha,
    }
    return regeneration, manifest, manifest_sha


def test_regeneration_binding_rejects_mutated_bundle_or_audit_sha() -> None:
    audit = _load_auditor()
    regeneration, manifest, manifest_sha = _regeneration_fixture(audit)
    audit._require_regeneration_binding(
        regeneration,
        manifest,
        actual_bundle_sha256=audit._BUNDLE_SHA256,
        actual_bundle_manifest_sha256=manifest_sha,
    )

    mutated_audit = json.loads(json.dumps(regeneration))
    mutated_audit["bundle_sha256"] = "f" * 64
    with pytest.raises(AssertionError, match="bundle SHA"):
        audit._require_regeneration_binding(
            mutated_audit,
            manifest,
            actual_bundle_sha256=audit._BUNDLE_SHA256,
            actual_bundle_manifest_sha256=manifest_sha,
        )

    mutated_manifest = json.loads(json.dumps(manifest))
    mutated_manifest["bundle_sha256"] = "e" * 64
    with pytest.raises(AssertionError, match="bundle SHA"):
        audit._require_regeneration_binding(
            regeneration,
            mutated_manifest,
            actual_bundle_sha256=audit._BUNDLE_SHA256,
            actual_bundle_manifest_sha256=manifest_sha,
        )


def test_state_layout_rejects_any_resumed_publication(tmp_path: Path) -> None:
    audit = _load_auditor()
    for name in (
        "portfolio_checkpoint.json",
        "portfolio_progress.jsonl",
        "portfolio_state.jsonl",
    ):
        (tmp_path / name).write_text("{}\n", encoding="utf-8")

    with pytest.raises(AssertionError, match="fresh single-directory"):
        audit._state_layout(
            run_dir=tmp_path,
            manifest={"arguments": {"resume_checkpoint": "elsewhere/checkpoint.json"}},
        )


def _physical_set_fixture(audit, tmp_path: Path):
    expected_files = audit._MANIFEST_ARTIFACTS | audit._RUN_FILES
    for name in expected_files - {"run_manifest.json"}:
        (tmp_path / name).write_bytes(b"")
    manifest = {
        "artifacts": {
            name: audit._sha256(tmp_path / name)
            for name in audit._HASHED_ARTIFACTS
        }
    }
    (tmp_path / "run_manifest.json").write_text("{}\n", encoding="utf-8")
    layout = audit._StateLayout(
        state_dir=tmp_path,
        checkpoint_path=tmp_path / "portfolio_checkpoint.json",
        state_path=tmp_path / "portfolio_state.jsonl",
        progress_path=tmp_path / "portfolio_progress.jsonl",
    )
    return manifest, layout


def test_physical_set_rejects_any_extra_run_artifact(tmp_path: Path) -> None:
    audit = _load_auditor()
    manifest, layout = _physical_set_fixture(audit, tmp_path)

    audit._audit_physical_set(tmp_path, manifest, layout)
    (tmp_path / "unexpected-resume-cache.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(AssertionError, match="unexpected physical run artifact set"):
        audit._audit_physical_set(tmp_path, manifest, layout)


def test_final_revalidation_rejects_post_audit_artifact_mutation(
    tmp_path: Path,
) -> None:
    audit = _load_auditor()
    manifest, layout = _physical_set_fixture(audit, tmp_path)
    manifest_sha256 = audit._sha256(tmp_path / "run_manifest.json")
    audit._audit_physical_set(tmp_path, manifest, layout)

    (tmp_path / "summary.json").write_text("mutated after audit\n", encoding="utf-8")

    with pytest.raises(AssertionError, match="artifact SHA-256 mismatch"):
        audit._final_revalidate_run(
            tmp_path,
            manifest,
            layout,
            expected_manifest_sha256=manifest_sha256,
        )


def test_next_open_outcome_is_bound_to_exact_bundle_open() -> None:
    audit = _load_auditor()
    outcome = {
        "symbol": "NVDA",
        "signal_date": "2024-01-02",
        "entry_date": "2024-01-03",
        "pivot": 100.0,
        "buy_zone_lower": 100.0,
        "buy_zone_upper": 105.0,
        "entry_open": 102.0,
        "outcome": "entries_executed",
    }
    audit._require_exact_next_open_outcome(
        outcome,
        expected_entry_date=pd.Timestamp("2024-01-03"),
        exact_bundle_open=102.0,
        buy_transaction_price=102.0,
    )

    mutated = dict(outcome, entry_open=101.0)
    with pytest.raises(AssertionError, match="exact next-session Open"):
        audit._require_exact_next_open_outcome(
            mutated,
            expected_entry_date=pd.Timestamp("2024-01-03"),
            exact_bundle_open=102.0,
            buy_transaction_price=102.0,
        )


def test_exact_session_facts_reject_a_stale_event_bar() -> None:
    audit = _load_auditor()
    dates = pd.bdate_range("2023-09-01", periods=52)
    frame = pd.DataFrame(
        {
            "Close": [100.0 + index for index in range(len(dates))],
            "Volume": [100.0] * 51 + [200.0],
        },
        index=dates,
    )
    facts = audit._independent_entry_facts(frame, dates[-1])
    assert facts.event_close == pytest.approx(frame.iloc[-1]["Close"])

    with pytest.raises(AssertionError, match="exact completed-session bar"):
        audit._independent_entry_facts(frame.iloc[:-1], dates[-1])


def test_rs_snapshot_excludes_a_peer_without_current_session_close() -> None:
    audit = _load_auditor()
    dates = pd.bdate_range("2024-01-02", periods=60)
    closes = pd.DataFrame(
        {
            "FRESH": [100.0 + index for index in range(60)],
            "STALE": [100.0 + 3 * index for index in range(59)] + [float("nan")],
        },
        index=dates,
    )

    snapshot = audit._independent_rs_snapshot(
        closes,
        dates[-1],
        eligible_tickers={"FRESH", "STALE"},
    )

    assert set(snapshot) == {"FRESH"}
    assert snapshot["FRESH"] == pytest.approx(99.0)


def test_n_uses_quarterly_revenue_and_peg_is_today_only() -> None:
    audit = _load_auditor()
    quarterly = pd.DataFrame(
        {
            pd.Timestamp("2023-03-03"): [100.0],
            pd.Timestamp("2024-03-31"): [150.0],
        },
        index=["Total Revenue"],
    )
    n_score, revenue_growth = audit._independent_evaluate_n(quarterly, 0.94)
    empty_score, empty_growth = audit._independent_evaluate_n(pd.DataFrame(), 0.94)
    assert revenue_growth == pytest.approx(0.5)
    assert empty_growth is None
    assert n_score > empty_score

    dates = pd.bdate_range("2024-01-02", periods=61)
    closes = [100.0] * 60 + [103.0]
    opens = [100.0] * 60 + [103.0]
    volumes = [100.0] * 60 + [200.0]
    today_gap = pd.DataFrame(
        {"Open": opens, "Close": closes, "Volume": volumes}, index=dates
    )
    has_gap, details = audit._independent_power_gap(today_gap)
    assert has_gap is True
    assert details["days_ago"] == 0
    _s_score, s_has_gap, s_details = audit._independent_evaluate_s(
        today_gap,
        prior_average_volume_50=100.0,
        shares_outstanding=None,
    )
    assert s_has_gap is True
    assert s_details["days_ago"] == 0

    prior_gap = today_gap.copy()
    prior_gap.iloc[-1] = [103.0, 103.0, 100.0]
    prior_gap.iloc[-2] = [103.0, 103.0, 200.0]
    has_gap, details = audit._independent_power_gap(prior_gap)
    assert has_gap is True
    assert details["days_ago"] == 1


def test_institutional_pair_must_be_atomic() -> None:
    audit = _load_auditor()
    score, available = audit._independent_evaluate_i(
        {
            "held_percent_institutions": None,
            "institution_count": 110,
            "prev_institution_count": 100,
        }
    )
    assert available is True
    assert score > 0.5

    with pytest.raises(AssertionError, match="atomic"):
        audit._independent_evaluate_i(
            {
                "held_percent_institutions": None,
                "institution_count": 110,
                "prev_institution_count": None,
            }
        )


def test_recall_must_be_an_exact_nested_copy_in_summary_manifest_and_report() -> None:
    audit = _load_auditor()
    recomputed = {
        "five_year": {
            "raw_denominator": 100,
            "raw_signaled_numerator": 40,
            "raw_executed_numerator": 30,
            "raw_signal_recall_pct": 40.0,
            "raw_execution_recall_pct": 30.0,
            "pit_exposed_denominator": 80,
            "pit_exposed_signaled_numerator": 36,
            "pit_exposed_executed_numerator": 24,
            "pit_exposed_signal_recall_pct": 45.0,
            "pit_exposed_execution_recall_pct": 30.0,
        },
        "rolling": {
            "raw_denominator": 4800,
            "raw_recalled_numerator": 1200,
            "raw_recall_pct": 25.0,
            "pit_exposed_denominator": 4000,
            "pit_exposed_recalled_numerator": 1200,
            "pit_exposed_recall_pct": 30.0,
        },
    }
    expected = audit._expected_leader_recall_publication(recomputed)
    report = audit._expected_recall_report_text(expected)
    audit._require_recall_publication(
        recomputed=recomputed,
        summary_recall=expected,
        manifest_recall=json.loads(json.dumps(expected)),
        report=report,
    )

    mutated = json.loads(json.dumps(expected))
    mutated["rolling"]["pit_exposed_member_at_evaluation"]["signaled_count"] += 1
    with pytest.raises(AssertionError, match="summary leader recall"):
        audit._require_recall_publication(
            recomputed=recomputed,
            summary_recall=mutated,
            manifest_recall=expected,
            report=report,
        )

    with pytest.raises(AssertionError, match="report recall"):
        audit._require_recall_publication(
            recomputed=recomputed,
            summary_recall=expected,
            manifest_recall=expected,
            report=report.replace("1200/4000", "1199/4000"),
        )
