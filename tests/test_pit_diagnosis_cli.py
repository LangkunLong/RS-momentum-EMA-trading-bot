from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
import sqlite3
from types import MappingProxyType

import pytest

from core.pit_diagnosis.baseline import BaselineReproduction
from core.pit_diagnosis.catalog import fixed_partitions, load_experiment_catalog
from core.pit_diagnosis.experiments import DiagnosisContext, run_experiment
from core.pit_diagnosis.fact_cache import _HASHED_ROW_COLUMNS, SessionFact
from core.pit_diagnosis.rulebook import canonical_sha256, load_rulebook


class _Facts:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.content_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        self.schema_sha256 = "b" * 64
        self._rows = (
            SessionFact(MappingProxyType({
                "symbol": "AAA", "session": "2021-01-04", "row_sha256": "a" * 64,
                "bundle_sha256": "c" * 64, "current_eps_yoy": 0.30, "sales_yoy": 0.30,
                "annual_eps_1": 4.0, "annual_eps_2": 3.0, "annual_eps_3": 2.0,
                "annual_eps_4": 1.0, "roe": 0.20, "base_kind": "flat", "pivot": 100.0,
                "close": 103.0, "open": 103.0, "event_volume_ratio": 1.5,
                "rs_rating": 90.0, "extension_pct": 0.03, "market_regime": "uptrend",
                "distribution_count": 0, "institutional_evidence_ids": "[]",
                "industry_evidence_ids": "[]",
            })),
            SessionFact(MappingProxyType({
                "symbol": "AAA", "session": "2024-01-02", "row_sha256": "d" * 64,
                "bundle_sha256": "c" * 64, "current_eps_yoy": 0.30, "sales_yoy": 0.30,
                "annual_eps_1": 4.0, "annual_eps_2": 3.0, "annual_eps_3": 2.0,
                "annual_eps_4": 1.0, "roe": 0.20, "base_kind": "flat", "pivot": 100.0,
                "close": 103.0, "open": 103.0, "event_volume_ratio": 1.5,
                "rs_rating": 90.0, "extension_pct": 0.03, "market_regime": "uptrend",
                "distribution_count": 0, "institutional_evidence_ids": "[]",
                "industry_evidence_ids": "[]",
            })),
        )

    def session_facts(self, start: str, end: str) -> tuple[SessionFact, ...]:
        return tuple(row for row in self._rows if start <= str(row.session) <= end)


@pytest.fixture
def mini_completed_context(tmp_path: Path) -> tuple[DiagnosisContext, tuple[object, ...]]:
    from tests.test_pit_diagnosis_experiments import _baseline_snapshot

    from tests.test_pit_diagnosis_fact_cache import _MiniPITBundle, build_cache

    facts_path = tmp_path / "diagnosis_facts.sqlite3"
    build_cache(_MiniPITBundle(), (facts_path, tmp_path / "facts.checkpoint.json", tmp_path / "facts.progress.jsonl"), resume=False)
    rulebook = load_rulebook(Path("config/pit_canslim_rulebook_v1.json"))
    catalog = load_experiment_catalog(Path("config/pit_diagnosis_experiments_v1.json"), rulebook)
    baseline_root = tmp_path / "baseline"
    baseline_root.mkdir()
    snapshot = replace(_baseline_snapshot(baseline_root), bundle_sha256="a" * 64)
    context = DiagnosisContext(
        rulebook=rulebook, catalog=catalog, fact_cache=_Facts(facts_path),
        partitions=fixed_partitions(), diagnostic_leader_labels=("AAA",),
        source_commit="d" * 40, source_fingerprint_sha256="e" * 64,
        strategy_identity="cached-diagnosis-v1", baseline_snapshot=snapshot,
        reproduced_baseline=snapshot,
    )
    d0 = run_experiment(context, "D0.BASELINE_REPRODUCTION", "discovery")
    verified = context.with_verified_baseline_reproduction(
        BaselineReproduction(True, (), snapshot.manifest_sha256, snapshot.manifest_sha256)
    )
    return verified, (
        d0,
        run_experiment(verified, "D2.RULE_STAGE_FUNNEL", "discovery"),
        run_experiment(verified, "D4.CURRENT_EXIT_PACKAGE", "discovery"),
    )


def test_help_has_no_data_or_provider_side_effects(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from pit_diagnosis import main

    def forbidden_call(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("PIT bundle must not load for help")

    monkeypatch.setattr("core.pit_data.PITDataBundle", forbidden_call)
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "OpenRouter" not in capsys.readouterr().out


def test_publication_is_complete_hash_bound_and_refuses_reuse(
    mini_completed_context: tuple[DiagnosisContext, tuple[object, ...]], tmp_path: Path,
) -> None:
    from core.pit_diagnosis.publication import publish_diagnosis, verify_diagnosis_run

    context, results = mini_completed_context
    run_dir = publish_diagnosis(context, results, tmp_path)
    verified = verify_diagnosis_run(run_dir)
    assert verified["status"] == "complete"
    assert verified["fidelity_label"] == "fidelity_incomplete"
    assert set(verified["artifact_sha256"]) == {
        "rulebook.json", "diagnosis_facts.sqlite3", "baseline_reproduction.json",
        "experiment_catalog.json", "rule_attribution.csv", "entry_funnel.csv",
        "execution_outcomes.csv", "exit_attribution.csv", "trade_statistics.json",
        "leader_recall.json", "performance.json", "ablation_results.csv",
        "agent_events.jsonl", "checkpoint.json", "report.md",
    }
    with pytest.raises(FileExistsError):
        publish_diagnosis(context, results, run_dir)


def test_publication_verifier_permits_only_explicitly_unavailable_member_prices(
    mini_completed_context: tuple[DiagnosisContext, tuple[object, ...]], tmp_path: Path,
) -> None:
    from core.pit_diagnosis.publication import publish_diagnosis, verify_diagnosis_run
    from tests.test_pit_diagnosis_fact_cache import _SuccessorWithoutAdmissionPriceBundle, build_cache

    context, results = mini_completed_context
    facts_path = tmp_path / "successor_facts.sqlite3"
    build_cache(_SuccessorWithoutAdmissionPriceBundle(), (facts_path, tmp_path / "successor.checkpoint.json", tmp_path / "successor.progress.jsonl"), resume=False)
    published = publish_diagnosis(replace(context, fact_cache=_Facts(facts_path)), results, tmp_path)

    assert verify_diagnosis_run(published)["status"] == "complete"


def _rehash_manifest(run_dir: Path, artifact: str) -> None:
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifact_sha256"][artifact] = hashlib.sha256((run_dir / artifact).read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def _rehash_fact_cache_rows(path: Path) -> None:
    """Model an attacker who can rehash SQLite rows and the logical digest."""
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        records = tuple(connection.execute("SELECT rowid,* FROM session_facts ORDER BY session,symbol"))
        row_hashes: list[str] = []
        for record in records:
            row = dict(record)
            row_sha = canonical_sha256({field: row[field] for field in _HASHED_ROW_COLUMNS})
            connection.execute("UPDATE session_facts SET row_sha256=? WHERE rowid=?", (row_sha, row["rowid"]))
            row_hashes.append(row_sha)
        logical = hashlib.sha256("".join(row_hashes).encode("ascii")).hexdigest()
        connection.execute("UPDATE metadata SET value=? WHERE key='content_sha256'", (logical,))
        connection.commit()
    finally:
        connection.close()


def _rehash_fact_cache_artifact(run_dir: Path) -> None:
    facts = run_dir / "diagnosis_facts.sqlite3"
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    facts_sha = hashlib.sha256(facts.read_bytes()).hexdigest()
    manifest["fact_cache_sha256"] = facts_sha
    manifest["artifact_sha256"][facts.name] = facts_sha
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def _inject_nan(path: Path) -> None:
    rows = path.read_text(encoding="utf-8").splitlines()
    values = rows[1].split(",")
    values[4] = "nan"
    path.write_text("\n".join((rows[0], ",".join(values))) + "\n", encoding="utf-8")


def _inject_leader_label(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["results"][0]["leader_label"] = "ex-post"
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize(
    ("artifact", "mutate", "message"),
    (
        ("rule_attribution.csv", lambda path: path.write_text("Ticker\nAAA\n", encoding="utf-8"), "schema"),
        ("entry_funnel.csv", _inject_nan, "non-finite"),
        ("report.md", lambda path: path.write_text("transaction price provider payload\n", encoding="utf-8"), "raw"),
        ("leader_recall.json", _inject_leader_label, "schema|raw"),
    ),
)
def test_publication_verifier_rejects_rehashed_raw_or_malformed_text_artifacts(
    mini_completed_context: tuple[DiagnosisContext, tuple[object, ...]], tmp_path: Path, artifact: str, mutate: object, message: str,
) -> None:
    from core.pit_diagnosis.publication import publish_diagnosis, verify_diagnosis_run

    context, results = mini_completed_context
    run_dir = publish_diagnosis(context, results, tmp_path)
    mutate(run_dir / artifact)
    _rehash_manifest(run_dir, artifact)
    with pytest.raises(ValueError, match=message):
        verify_diagnosis_run(run_dir)


def test_publication_verifier_rejects_rehashed_sqlite_extra_raw_field(
    mini_completed_context: tuple[DiagnosisContext, tuple[object, ...]], tmp_path: Path,
) -> None:
    from core.pit_diagnosis.publication import publish_diagnosis, verify_diagnosis_run

    context, results = mini_completed_context
    run_dir = publish_diagnosis(context, results, tmp_path)
    path = run_dir / "diagnosis_facts.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("ALTER TABLE session_facts ADD COLUMN provider_payload TEXT")
    connection.commit()
    connection.close()
    _rehash_manifest(run_dir, "diagnosis_facts.sqlite3")
    with pytest.raises(ValueError, match="schema|raw"):
        verify_diagnosis_run(run_dir)


def test_publication_verifier_requires_exact_manifest_schema(
    mini_completed_context: tuple[DiagnosisContext, tuple[object, ...]], tmp_path: Path,
) -> None:
    from core.pit_diagnosis.publication import publish_diagnosis, verify_diagnosis_run

    context, results = mini_completed_context
    run_dir = publish_diagnosis(context, results, tmp_path)
    path = run_dir / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["undeclared"] = True
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest schema"):
        verify_diagnosis_run(run_dir)


@pytest.mark.parametrize(
    ("artifact", "mutate"),
    (
        ("entry_funnel.csv", lambda path: path.write_text(path.read_text(encoding="utf-8").splitlines()[0] + "\nD0.BASELINE_REPRODUCTION,discovery\n", encoding="utf-8")),
        ("entry_funnel.csv", lambda path: path.write_text(path.read_text(encoding="utf-8").replace(",0,", ",1e309,", 1), encoding="utf-8")),
        ("trade_statistics.json", lambda path: path.write_text(json.dumps({"results": [{**json.loads(path.read_text(encoding="utf-8"))["results"][0], "mean_return_pct": "1e309"}]}), encoding="utf-8")),
    ),
)
def test_publication_verifier_rejects_rehashed_incomplete_or_nonfinite_typed_artifacts(
    mini_completed_context: tuple[DiagnosisContext, tuple[object, ...]], tmp_path: Path, artifact: str, mutate: object,
) -> None:
    from core.pit_diagnosis.publication import publish_diagnosis, verify_diagnosis_run

    context, results = mini_completed_context
    run_dir = publish_diagnosis(context, results, tmp_path)
    mutate(run_dir / artifact)
    _rehash_manifest(run_dir, artifact)
    with pytest.raises(ValueError, match="CSV|finite|integer"):
        verify_diagnosis_run(run_dir)


@pytest.mark.parametrize(
    ("key", "value"),
    (("result_count", True), ("result_count", -1), ("fidelity_label", "anything_goes"), ("promotion_eligible_candidates", True), ("promotion_eligible_candidates", 1)),
)
def test_publication_verifier_rejects_manifest_type_and_domain_tampering(
    mini_completed_context: tuple[DiagnosisContext, tuple[object, ...]], tmp_path: Path, key: str, value: object,
) -> None:
    from core.pit_diagnosis.publication import publish_diagnosis, verify_diagnosis_run

    context, results = mini_completed_context
    run_dir = publish_diagnosis(context, results, tmp_path)
    path = run_dir / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest[key] = value
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="result_count|fidelity|promotion"):
        verify_diagnosis_run(run_dir)


@pytest.mark.parametrize(
    ("artifact", "field", "value"),
    (
        ("trade_statistics.json", "completed_positions", "1"),
        ("trade_statistics.json", "mean_return_pct", "1.0"),
    ),
)
def test_publication_verifier_rejects_native_json_metric_strings(
    mini_completed_context: tuple[DiagnosisContext, tuple[object, ...]], tmp_path: Path,
    artifact: str, field: str, value: str,
) -> None:
    from core.pit_diagnosis.publication import publish_diagnosis, verify_diagnosis_run

    context, results = mini_completed_context
    run_dir = publish_diagnosis(context, results, tmp_path)
    path = run_dir / artifact
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["results"][0][field] = value
    path.write_text(json.dumps(payload), encoding="utf-8")
    _rehash_manifest(run_dir, artifact)
    with pytest.raises(ValueError, match="integer|finite"):
        verify_diagnosis_run(run_dir)


def test_publication_verifier_rejects_manifest_numeric_string(
    mini_completed_context: tuple[DiagnosisContext, tuple[object, ...]], tmp_path: Path,
) -> None:
    from core.pit_diagnosis.publication import publish_diagnosis, verify_diagnosis_run

    context, results = mini_completed_context
    run_dir = publish_diagnosis(context, results, tmp_path)
    path = run_dir / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["result_count"] = "3"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="result_count.*integer"):
        verify_diagnosis_run(run_dir)


@pytest.mark.parametrize(
    "mutation",
    (
        "UPDATE session_facts SET close='inf'",
        "UPDATE metadata SET value='provider_payload' WHERE key='identity'",
    ),
)
def test_publication_verifier_rejects_rehashed_sqlite_nonfinite_and_raw_content(
    mini_completed_context: tuple[DiagnosisContext, tuple[object, ...]], tmp_path: Path, mutation: str,
) -> None:
    from core.pit_diagnosis.publication import publish_diagnosis, verify_diagnosis_run

    context, results = mini_completed_context
    run_dir = publish_diagnosis(context, results, tmp_path)
    path = run_dir / "diagnosis_facts.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(mutation)
    connection.commit()
    connection.close()
    _rehash_manifest(run_dir, "diagnosis_facts.sqlite3")
    with pytest.raises(ValueError, match="fact-cache"):
        verify_diagnosis_run(run_dir)


def test_publication_verifier_rejects_rehashed_sqlite_stale_logical_integrity(
    mini_completed_context: tuple[DiagnosisContext, tuple[object, ...]], tmp_path: Path,
) -> None:
    from core.pit_diagnosis.publication import publish_diagnosis, verify_diagnosis_run

    context, results = mini_completed_context
    run_dir = publish_diagnosis(context, results, tmp_path)
    facts = run_dir / "diagnosis_facts.sqlite3"
    connection = sqlite3.connect(facts)
    connection.execute("UPDATE session_facts SET close=close+1.0")
    connection.commit()
    connection.close()
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    facts_sha = hashlib.sha256(facts.read_bytes()).hexdigest()
    manifest["fact_cache_sha256"] = facts_sha
    manifest["artifact_sha256"]["diagnosis_facts.sqlite3"] = facts_sha
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="fact-cache"):
        verify_diagnosis_run(run_dir)


@pytest.mark.parametrize(
    "mutation",
    (
        "UPDATE session_facts SET availability_bitset=64",
        "UPDATE session_facts SET availability_bitset=availability_bitset & ~2",
        "UPDATE session_facts SET availability_bitset=availability_bitset | 4",
        "UPDATE session_facts SET availability_bitset=availability_bitset | 8",
        "UPDATE session_facts SET availability_bitset=availability_bitset | 16, rs_rating=NULL",
        "UPDATE session_facts SET availability_bitset=availability_bitset | 32, base_kind=NULL, base_start_session=NULL, base_end_session=NULL, base_duration_sessions=NULL, base_low=NULL, base_depth_pct=NULL, base_input_sha256=NULL, pivot=NULL, extension_pct=NULL",
    ),
)
def test_publication_verifier_rejects_rehashed_inconsistent_availability_evidence(
    mini_completed_context: tuple[DiagnosisContext, tuple[object, ...]], tmp_path: Path, mutation: str,
) -> None:
    from core.pit_diagnosis.publication import publish_diagnosis, verify_diagnosis_run

    context, results = mini_completed_context
    run_dir = publish_diagnosis(context, results, tmp_path)
    facts = run_dir / "diagnosis_facts.sqlite3"
    connection = sqlite3.connect(facts)
    try:
        connection.execute(mutation)
        connection.commit()
    finally:
        connection.close()
    _rehash_fact_cache_rows(facts)
    _rehash_fact_cache_artifact(run_dir)

    with pytest.raises(ValueError, match="fact-cache"):
        verify_diagnosis_run(run_dir)
