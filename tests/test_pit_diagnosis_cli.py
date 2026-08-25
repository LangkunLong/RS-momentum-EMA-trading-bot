from __future__ import annotations

import hashlib
from pathlib import Path
from types import MappingProxyType

import pytest

from core.pit_diagnosis.baseline import BaselineReproduction
from core.pit_diagnosis.catalog import fixed_partitions, load_experiment_catalog
from core.pit_diagnosis.experiments import DiagnosisContext, run_experiment
from core.pit_diagnosis.fact_cache import SessionFact
from core.pit_diagnosis.rulebook import load_rulebook


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

    facts_path = tmp_path / "diagnosis_facts.sqlite3"
    facts_path.write_bytes(b"offline deterministic fact cache")
    rulebook = load_rulebook(Path("config/pit_canslim_rulebook_v1.json"))
    catalog = load_experiment_catalog(Path("config/pit_diagnosis_experiments_v1.json"), rulebook)
    baseline_root = tmp_path / "baseline"
    baseline_root.mkdir()
    snapshot = _baseline_snapshot(baseline_root)
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
