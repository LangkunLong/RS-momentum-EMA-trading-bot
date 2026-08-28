"""Schema-v2 contracts for the model-authored PIT optimizer."""

from __future__ import annotations

import json
import hashlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, fields, replace
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_EVEN
import difflib
from itertools import product
from pathlib import Path
import sqlite3
import string
import subprocess
import threading

import pytest

import core.pit_optimization_contract as contract
import core.pit_optimizer_evaluation as evaluation
from core.pit_optimizer_evaluation import (
    AggregateMetric,
    FoldAggregateSummary,
    FoldManifest,
    FoldSpec,
)
from core.pit_policy_parity import ParityAttestation


def _canonical_text(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _investigator_payload() -> dict[str, object]:
    return {
        "hypothesis_id": "hypothesis_1",
        "family": "entry",
        "evidence_ids": ["discovery_1.excess_return"],
        "causal_rationale": "Raise selectivity where the entry funnel loses quality.",
        "target_paths": ["core/strategy_policy/entry.py"],
        "target_symbols": ["core.strategy_policy.entry.evaluate_entry"],
        "expected_diagnostic_changes": ["fewer low-quality entries"],
        "known_risks": ["lower trade count"],
        "author_instructions": ["Change only evaluate_entry."],
    }


def test_role_schema_investigator_output_is_closed_and_bounded() -> None:
    """Break caught: investigator output could widen or overflow the author envelope."""
    payload = _investigator_payload()

    artifact = contract.InvestigatorArtifact.from_json(
        _canonical_text(payload),
        max_total_bytes=contract.MAX_INVESTIGATOR_ARTIFACT_BYTES,
    )

    assert artifact.hypothesis_id == "hypothesis_1"
    assert artifact.family == "entry"
    schema = contract.pit_optimizer_response_format("investigator")
    assert schema["json_schema"]["name"] == "pit_optimizer_investigator_v2"
    body = schema["json_schema"]["schema"]
    assert body["additionalProperties"] is False
    assert set(body["required"]) == set(payload)
    assert body["properties"]["family"]["enum"] == ["entry", "exit", "risk_sizing"]

    with pytest.raises(ValueError, match="invalid keys"):
        contract.InvestigatorArtifact.from_json(
            _canonical_text({**payload, "hidden_fold": "hidden_1"}),
            max_total_bytes=contract.MAX_INVESTIGATOR_ARTIFACT_BYTES,
        )
    duplicate = _canonical_text(payload)[:-1] + ',"hypothesis_id":"hypothesis_2"}'
    with pytest.raises(ValueError, match="duplicate"):
        contract.InvestigatorArtifact.from_json(
            duplicate,
            max_total_bytes=contract.MAX_INVESTIGATOR_ARTIFACT_BYTES,
        )
    with pytest.raises(ValueError, match="family"):
        contract.InvestigatorArtifact.from_json(
            _canonical_text({**payload, "family": "hidden_validation"}),
            max_total_bytes=contract.MAX_INVESTIGATOR_ARTIFACT_BYTES,
        )
    with pytest.raises(ValueError, match="at most 16"):
        contract.InvestigatorArtifact.from_json(
            _canonical_text(
                {
                    **payload,
                    "evidence_ids": [f"evidence_{index}" for index in range(17)],
                }
            ),
            max_total_bytes=contract.MAX_INVESTIGATOR_ARTIFACT_BYTES,
        )
    with pytest.raises(ValueError, match="unique"):
        contract.InvestigatorArtifact.from_json(
            _canonical_text({**payload, "known_risks": ["same", "same"]}),
            max_total_bytes=contract.MAX_INVESTIGATOR_ARTIFACT_BYTES,
        )
    with pytest.raises(ValueError, match="bounded"):
        contract.InvestigatorArtifact.from_json(
            _canonical_text(payload),
            max_total_bytes=32,
        )


def _author_payload() -> dict[str, object]:
    return {
        "hypothesis_id": "hypothesis_1",
        "behavioral_summary": "Require a stronger entry confirmation.",
        "changed_paths": ["core/strategy_policy/entry.py"],
        "changed_symbols": ["core.strategy_policy.entry.evaluate_entry"],
        "unified_diff": (
            "--- a/core/strategy_policy/entry.py\n"
            "+++ b/core/strategy_policy/entry.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def evaluate_entry(snapshot):\n"
            "-    return None\n"
            "+    return True\n"
        ),
        "assumptions": ["The aggregate funnel is causal."],
        "validation_suggestions": ["Run the focused entry checks."],
    }


def test_role_schema_author_output_has_independent_diff_and_metadata_caps() -> None:
    """Break caught: an author response could hide oversized metadata beside a bounded diff."""
    payload = _author_payload()

    artifact = contract.AuthorArtifact.from_json(
        _canonical_text(payload),
        max_diff_bytes=8 * 1024,
        max_total_bytes=16 * 1024,
    )

    assert artifact.changed_paths == ("core/strategy_policy/entry.py",)
    assert artifact.changed_symbols == (
        "core.strategy_policy.entry.evaluate_entry",
    )
    schema = contract.pit_optimizer_response_format("author")["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(payload)

    with pytest.raises(ValueError, match="diff"):
        contract.AuthorArtifact.from_json(
            _canonical_text({**payload, "unified_diff": "x" * 33}),
            max_diff_bytes=32,
            max_total_bytes=16 * 1024,
        )
    with pytest.raises(ValueError, match="non-diff"):
        contract.AuthorArtifact.from_json(
            _canonical_text(
                {
                    **payload,
                    "behavioral_summary": "s" * 4096,
                    "assumptions": ["a" * 4096],
                }
            ),
            max_diff_bytes=8 * 1024,
            max_total_bytes=16 * 1024,
        )
    with pytest.raises(ValueError, match="invalid keys"):
        contract.AuthorArtifact.from_json(
            _canonical_text({**payload, "provider_audit_body": "forbidden"}),
            max_diff_bytes=8 * 1024,
            max_total_bytes=16 * 1024,
        )


def _critic_payload() -> dict[str, object]:
    return {
        "hypothesis_id": "hypothesis_1",
        "prediction_vs_observation": "Trade quality rose, but participation fell too far.",
        "causal_explanation": "The confirmation excluded both weak and valid entries.",
        "evidence_ids": ["candidate.discovery_1.entry_funnel"],
        "disposition": "refine",
        "next_direction": "Retain confirmation only for extended entries.",
    }


def test_role_schema_critic_output_is_advisory_closed_and_bounded() -> None:
    """Break caught: the critic could claim acceptance authority or expose hidden results."""
    payload = _critic_payload()

    artifact = contract.CriticArtifact.from_json(
        _canonical_text(payload),
        max_total_bytes=contract.MAX_CRITIC_ARTIFACT_BYTES,
    )

    assert artifact.disposition == "refine"
    schema = contract.pit_optimizer_response_format("critic")["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(payload)
    assert schema["properties"]["disposition"]["enum"] == [
        "refine",
        "abandon",
        "change_family",
    ]

    with pytest.raises(ValueError, match="disposition"):
        contract.CriticArtifact.from_json(
            _canonical_text({**payload, "disposition": "accept"}),
            max_total_bytes=contract.MAX_CRITIC_ARTIFACT_BYTES,
        )
    with pytest.raises(ValueError, match="invalid keys"):
        contract.CriticArtifact.from_json(
            _canonical_text({**payload, "hidden_metrics": {"return": 99}}),
            max_total_bytes=contract.MAX_CRITIC_ARTIFACT_BYTES,
        )
    with pytest.raises(ValueError, match="bounded"):
        contract.CriticArtifact.from_json(
            _canonical_text(payload),
            max_total_bytes=64,
        )


def _source_bundle() -> contract.PolicySourceBundle:
    sources = (
        (
            "core/strategy_policy/entry.py",
            "core.strategy_policy.entry.evaluate_entry",
            "def evaluate_entry(snapshot):\n    return None\n",
        ),
        (
            "core/strategy_policy/risk.py",
            (
                "core.strategy_policy.risk.recommend_capacity",
                "core.strategy_policy.risk.recommend_allocation",
                "core.strategy_policy.risk.select_eviction",
            ),
            "def recommend_capacity(snapshot):\n    return 1\n",
        ),
        (
            "core/strategy_policy/exit.py",
            "core.strategy_policy.exit.evaluate_exit",
            "def evaluate_exit(snapshot):\n    return None\n",
        ),
    )
    source_texts = {path: text for path, _symbol, text in sources}
    source_sha256s = tuple(
        (path, hashlib.sha256(text.encode("utf-8")).hexdigest())
        for path, text in source_texts.items()
    )
    scope = contract.PolicySourceScope(
        schema_version=2,
        policy_interface_version=1,
        initial_policy_source_sha256s=source_sha256s,
        editable_paths=tuple(source_texts),
        max_policy_source_bundle_bytes=64 * 1024,
        max_iteration_feedback_bytes=4 * 1024,
        max_iteration_history_bytes=32 * 1024,
        hard_patch_bounds=contract.PatchBounds(3, 12, 200, 64 * 1024),
        candidate_bounds=contract.PatchBounds(3, 12, 80, 8 * 1024),
        max_iterations=2,
        allowed_descendant_rule=(
            "authenticated_initial_sources_plus_validated_cumulative_diff"
        ),
    )
    return contract.initial_policy_source_bundle(
        scope=scope,
        source_texts=source_texts,
    )


def _source_scope(
    bundle: contract.PolicySourceBundle,
    *,
    bounds: contract.PatchBounds | None = None,
    bundle_cap: int = 64 * 1024,
) -> contract.PolicySourceScope:
    candidate_bounds = bounds or contract.PatchBounds(3, 12, 80, 8 * 1024)
    return contract.PolicySourceScope(
        schema_version=2,
        policy_interface_version=bundle.policy_interface_version,
        initial_policy_source_sha256s=tuple(
            (record.path, record.sha256) for record in bundle.files
        ),
        editable_paths=tuple(record.path for record in bundle.files),
        max_policy_source_bundle_bytes=bundle_cap,
        max_iteration_feedback_bytes=4 * 1024,
        max_iteration_history_bytes=32 * 1024,
        hard_patch_bounds=contract.PatchBounds(3, 12, 200, 64 * 1024),
        candidate_bounds=candidate_bounds,
        max_iterations=2,
        allowed_descendant_rule=(
            "authenticated_initial_sources_plus_validated_cumulative_diff"
        ),
    )


def _diff_for_changes(
    bundle: contract.PolicySourceBundle,
    changes: dict[str, str],
    *,
    context: int = 3,
) -> str:
    by_path = {record.path: record.text for record in bundle.files}
    return "".join(
        line
        for path in tuple(record.path for record in bundle.files)
        if path in changes
        for line in difflib.unified_diff(
            by_path[path].splitlines(keepends=True),
            changes[path].splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=context,
            lineterm="\n",
        )
    )


def _artifact_for_diff(diff: str, paths: tuple[str, ...]) -> contract.AuthorArtifact:
    symbols = tuple(
        {
            "core/strategy_policy/entry.py": "core.strategy_policy.entry.evaluate_entry",
            "core/strategy_policy/risk.py": "core.strategy_policy.risk.recommend_capacity",
            "core/strategy_policy/exit.py": "core.strategy_policy.exit.evaluate_exit",
        }[path]
        for path in paths
    )
    return contract.AuthorArtifact.from_json(
        _canonical_text(
            {
                **_author_payload(),
                "changed_paths": list(paths),
                "changed_symbols": list(symbols),
                "unified_diff": diff,
            }
        ),
        max_diff_bytes=64 * 1024,
        max_total_bytes=72 * 1024,
    )


def test_controller_materializes_only_bounds_valid_source_descendants() -> None:
    """Break caught: arbitrary source text or an over-bound patch could reach a role."""
    initial = _source_bundle()
    scope = _source_scope(initial)
    initial = contract.initial_policy_source_bundle(
        scope=scope,
        source_texts={record.path: record.text for record in initial.files},
    )
    entry_path = "core/strategy_policy/entry.py"
    risk_path = "core/strategy_policy/risk.py"
    entry_text = initial.files[0].text.replace("return None", "return True")
    valid_diff = _diff_for_changes(initial, {entry_path: entry_text})
    valid = contract.materialize_policy_source_descendant(
        scope=scope,
        initial_bundle=initial,
        current_bundle=initial,
        artifact=_artifact_for_diff(valid_diff, (entry_path,)),
    )
    assert valid.validation.failure_code is None
    assert valid.bundle is not None
    contract.validate_policy_source_bundle_descendant(
        scope=scope,
        initial_bundle=initial,
        bundle=valid.bundle,
    )

    arbitrary_record = replace(
        initial.files[0],
        text="arbitrary provider source\n",
        sha256=hashlib.sha256(b"arbitrary provider source\n").hexdigest(),
    )
    with pytest.raises(ValueError, match="controller derived"):
        replace(
            valid.bundle,
            files=(arbitrary_record, *initial.files[1:]),
        )

    risk_text = initial.files[1].text.replace("return 1", "return 2")
    two_file_diff = _diff_for_changes(
        initial,
        {entry_path: entry_text, risk_path: risk_text},
    )
    with pytest.raises(ValueError, match="max_files"):
        contract.materialize_policy_source_descendant(
            scope=replace(
                scope,
                candidate_bounds=contract.PatchBounds(1, 12, 80, 8 * 1024),
            ),
            initial_bundle=initial,
            current_bundle=initial,
            artifact=_artifact_for_diff(two_file_diff, (entry_path, risk_path)),
        )

    five_lines = "a\nb\nc\nd\ne\n"
    expanded_initial_texts = {
        record.path: (five_lines if record.path == entry_path else record.text)
        for record in initial.files
    }
    expanded_hashes = tuple(
        (path, hashlib.sha256(text.encode("utf-8")).hexdigest())
        for path, text in expanded_initial_texts.items()
    )
    expanded_scope = replace(scope, initial_policy_source_sha256s=expanded_hashes)
    expanded_initial = contract.initial_policy_source_bundle(
        scope=expanded_scope,
        source_texts=expanded_initial_texts,
    )
    two_hunk_text = "A\nb\nc\nd\nE\n"
    two_hunk_diff = _diff_for_changes(
        expanded_initial,
        {entry_path: two_hunk_text},
        context=0,
    )
    with pytest.raises(ValueError, match="max_hunks"):
        contract.materialize_policy_source_descendant(
            scope=replace(
                expanded_scope,
                candidate_bounds=contract.PatchBounds(3, 1, 80, 8 * 1024),
            ),
            initial_bundle=expanded_initial,
            current_bundle=expanded_initial,
            artifact=_artifact_for_diff(two_hunk_diff, (entry_path,)),
        )
    with pytest.raises(ValueError, match="max_changed_lines"):
        contract.materialize_policy_source_descendant(
            scope=replace(
                expanded_scope,
                candidate_bounds=contract.PatchBounds(3, 12, 3, 8 * 1024),
            ),
            initial_bundle=expanded_initial,
            current_bundle=expanded_initial,
            artifact=_artifact_for_diff(two_hunk_diff, (entry_path,)),
        )
    with pytest.raises(ValueError, match="max_diff_bytes"):
        contract.materialize_policy_source_descendant(
            scope=replace(
                expanded_scope,
                candidate_bounds=contract.PatchBounds(
                    3,
                    12,
                    80,
                    len(two_hunk_diff.encode("utf-8")) - 1,
                ),
            ),
            initial_bundle=expanded_initial,
            current_bundle=expanded_initial,
            artifact=_artifact_for_diff(two_hunk_diff, (entry_path,)),
        )

    overflow_scope = replace(
        scope,
        max_policy_source_bundle_bytes=len(initial.canonical_json_bytes()) + 32,
    )
    overflow_text = initial.files[0].text.replace("return None", "return " + "1" * 256)
    overflow_diff = _diff_for_changes(initial, {entry_path: overflow_text})
    overflow = contract.materialize_policy_source_descendant(
        scope=overflow_scope,
        initial_bundle=initial,
        current_bundle=initial,
        artifact=_artifact_for_diff(overflow_diff, (entry_path,)),
    )
    assert overflow.bundle is None
    assert overflow.validation.failure_code == "next_context_oversize"


def _fold_summary(fold_id: str, excess: float) -> FoldAggregateSummary:
    return FoldAggregateSummary(
        fold_id=fold_id,
        total_return_pct=excess + 1.0,
        excess_total_return_pp=excess,
        max_drawdown_pct=-2.0,
        sharpe_ratio=1.0,
        closed_trades=2,
        turnover_pct=10.0,
        average_exposure_pct=20.0,
        entry_funnel=(AggregateMetric("entries_executed", 2),),
        exit_attribution=(AggregateMetric("end_of_test", 2),),
    )


def _aggregate_sha256(folds: tuple[FoldAggregateSummary, ...]) -> str:
    return hashlib.sha256(
        (_canonical_text([asdict(item) for item in folds]) + "\n").encode("utf-8")
    ).hexdigest()


def _discovery_summary() -> contract.DiscoveryEvidenceSummary:
    return contract.DiscoveryEvidenceSummary(
        folds=(
            _fold_summary("discovery_1", 0.5),
            _fold_summary("discovery_2", 0.25),
        ),
        score=None,
        evidence_ids=("evidence.discovery_1", "evidence.discovery_2"),
    )


def _discovery_exposure_proof(tmp_path: Path) -> evaluation.DiscoveryExposureProof:
    ledger = evaluation.ValidationLedger(
        tmp_path / "pit_optimizer_validation_ledger.jsonl"
    )
    manifest = _fold_manifest()
    metadata = evaluation.ValidationExposureMetadata(
        run_id="run_1",
        source_head="1" * 40,
        baseline_policy_sha256="d" * 64,
        candidate_identity_sha256=None,
        exposure_kind="provider_context",
    )
    reservations = tuple(
        ledger.mark_discovery(_validation_identity(fold, fold.fold_id), metadata)
        for fold in manifest.discovery_folds
    )
    return ledger.seal_discovery_folds(manifest, reservations)


def test_candidate_comparison_structurally_rejects_hidden_fold_identity(
    tmp_path: Path,
) -> None:
    """Break caught: a caller could serialize hidden validation evidence for a role."""
    baseline = (
        _fold_summary("discovery_1", 0.0),
        _fold_summary("discovery_2", 0.0),
    )
    baseline_sha256 = _aggregate_sha256(baseline)
    with pytest.raises(ValueError, match="ledger exposure"):
        contract.candidate_comparison_from_fixed_baseline(
            candidate_folds=(
                _fold_summary("discovery_1", 0.5),
                _fold_summary("hidden_1", 99.0),
            ),
            original_baseline_folds=baseline,
            original_baseline_sha256=baseline_sha256,
            expected_original_baseline_sha256=baseline_sha256,
            discovery_exposure=_discovery_exposure_proof(tmp_path),
            diagnostics=(),
        )


def test_candidate_validation_codes_are_closed_and_match_stage_flags() -> None:
    """Break caught: arbitrary or contradictory failure labels could reach a role."""
    with pytest.raises(ValueError, match="failure code is not closed"):
        contract.CandidateValidationSummary(
            failure_code="invented_failure",
            syntax_ok=False,
            imports_ok=False,
            purity_ok=False,
            deterministic_ok=False,
            worker_ok=False,
            replay_attempted=False,
        )
    with pytest.raises(ValueError, match="syntax_failed flags"):
        contract.CandidateValidationSummary(
            failure_code="syntax_failed",
            syntax_ok=True,
            imports_ok=False,
            purity_ok=False,
            deterministic_ok=False,
            worker_ok=False,
            replay_attempted=False,
        )
    with pytest.raises(ValueError, match="successful validation flags"):
        contract.CandidateValidationSummary(
            failure_code=None,
            syntax_ok=True,
            imports_ok=True,
            purity_ok=True,
            deterministic_ok=True,
            worker_ok=True,
            replay_attempted=False,
        )


def _investigator_artifact() -> contract.InvestigatorArtifact:
    return contract.InvestigatorArtifact.from_json(
        _canonical_text(_investigator_payload()),
        max_total_bytes=contract.MAX_INVESTIGATOR_ARTIFACT_BYTES,
    )


def test_role_schema_inputs_are_exact_bounded_provider_projections(
    tmp_path: Path,
) -> None:
    """Break caught: a role input could admit hidden identity or unbounded prior context."""
    bounds = contract.PatchBounds(3, 12, 80, 8 * 1024)
    discovery = _discovery_summary()
    investigator = _investigator_artifact()
    source_bundle = _source_bundle()
    rule_summary = contract.StrategyRuleSummary(
        records=(contract.RuleSummaryRecord("rule.entry", "Use causal entry inputs."),)
    )
    incumbent = contract.IncumbentSummary(
        candidate_identity_sha256=None,
        accepted_iteration=None,
        behavioral_summary="Authenticated fixed baseline.",
        discovery=discovery,
    )
    feedback = contract.IterationFeedbackSummary(
        iteration=1,
        hypothesis_id="hypothesis_0",
        family="risk_sizing",
        author_summary="Reduced concentration.",
        validation_code="valid",
        discovery_score=None,
        critic_disposition="refine",
        critic_next_direction="Use a smaller adjustment.",
        incumbent_changed=False,
    )
    investigator_input = contract.InvestigatorInput(
        schema_version=2,
        iteration=2,
        policy_interface_version=1,
        immutable_constraint_ids=("causal_only", "no_external_io"),
        candidate_bounds=bounds,
        rule_summary=rule_summary,
        source_bundle=source_bundle,
        baseline_discovery=discovery,
        incumbent_summary=incumbent,
        prior_iterations=(feedback,),
    )
    author_input = contract.AuthorInput(
        schema_version=2,
        iteration=2,
        policy_interface_version=1,
        immutable_constraint_ids=("causal_only", "no_external_io"),
        candidate_bounds=bounds,
        investigator=investigator,
        source_bundle=source_bundle,
    )
    author_artifact = contract.AuthorArtifact.from_json(
        _canonical_text(_author_payload()),
        max_diff_bytes=8 * 1024,
        max_total_bytes=16 * 1024,
    )
    author_manifest = contract.AuthorManifestSummary(
        hypothesis_id="hypothesis_1",
        behavioral_summary=author_artifact.behavioral_summary,
        changed_paths=author_artifact.changed_paths,
        changed_symbols=author_artifact.changed_symbols,
    )
    original_baseline = (
        _fold_summary("discovery_1", 0.0),
        _fold_summary("discovery_2", 0.0),
    )
    baseline_sha256 = _aggregate_sha256(original_baseline)
    comparison = contract.candidate_comparison_from_fixed_baseline(
        candidate_folds=discovery.folds,
        original_baseline_folds=original_baseline,
        original_baseline_sha256=baseline_sha256,
        expected_original_baseline_sha256=baseline_sha256,
        discovery_exposure=_discovery_exposure_proof(tmp_path),
        diagnostics=(AggregateMetric("entry_quality_delta", 0.2),),
    )
    critic_input = contract.CriticInput(
        schema_version=2,
        iteration=2,
        immutable_constraint_ids=("causal_only", "no_external_io"),
        hypothesis_id="hypothesis_1",
        investigator_summary=investigator,
        author_manifest=author_manifest,
        validation=contract.CandidateValidationSummary(
            failure_code=None,
            syntax_ok=True,
            imports_ok=True,
            purity_ok=True,
            deterministic_ok=True,
            worker_ok=True,
            replay_attempted=True,
        ),
        candidate_vs_baseline=comparison,
        candidate_vs_incumbent=comparison,
    )

    assert tuple(field.name for field in fields(contract.InvestigatorInput)) == (
        "schema_version",
        "iteration",
        "policy_interface_version",
        "immutable_constraint_ids",
        "candidate_bounds",
        "rule_summary",
        "source_bundle",
        "baseline_discovery",
        "incumbent_summary",
        "prior_iterations",
    )
    assert tuple(field.name for field in fields(contract.AuthorInput)) == (
        "schema_version",
        "iteration",
        "policy_interface_version",
        "immutable_constraint_ids",
        "candidate_bounds",
        "investigator",
        "source_bundle",
    )
    assert tuple(field.name for field in fields(contract.CriticInput)) == (
        "schema_version",
        "iteration",
        "immutable_constraint_ids",
        "hypothesis_id",
        "investigator_summary",
        "author_manifest",
        "validation",
        "candidate_vs_baseline",
        "candidate_vs_incumbent",
    )
    for role_input in (investigator_input, author_input, critic_input):
        rendered = role_input.canonical_json_bytes().decode("utf-8")
        for forbidden in (
            "hidden_fold",
            "2021-12-15",
            "validation_ledger",
            "provider_audit_body",
            "credential",
            "C:\\\\",
        ):
            assert forbidden not in rendered

    author_input.validate_artifact(author_artifact)
    with pytest.raises(ValueError, match="hypothesis"):
        author_input.validate_artifact(replace(author_artifact, hypothesis_id="other"))
    with pytest.raises(ValueError, match="hypothesis"):
        replace(critic_input, hypothesis_id="other")
    with pytest.raises(ValueError, match="source SHA-256"):
        replace(source_bundle.files[0], sha256="0" * 64)
    with pytest.raises(ValueError, match="declared symbols"):
        replace(source_bundle.files[0], declared_symbols=("unrelated.symbol",))
    with pytest.raises(ValueError, match="target paths"):
        replace(investigator, target_paths=("C:\\private\\policy.py",))
    with pytest.raises(ValueError, match="changed paths"):
        replace(author_artifact, changed_paths=("core/backtest_engine.py",))
    with pytest.raises(ValueError, match="at most 8"):
        replace(investigator_input, prior_iterations=(feedback,) * 9)
    with pytest.raises(ValueError, match="too large"):
        replace(investigator, causal_rationale="x" * (4 * 1024 + 1))
    with pytest.raises(ValueError, match="too large"):
        replace(author_artifact, behavioral_summary="x" * (4 * 1024 + 1))
    with pytest.raises(ValueError, match="too large"):
        replace(
            contract.CriticArtifact.from_json(
                _canonical_text(_critic_payload()),
                max_total_bytes=contract.MAX_CRITIC_ARTIFACT_BYTES,
            ),
            next_direction="x" * (4 * 1024 + 1),
        )


def _sessions(start: str, end: str) -> tuple[str, ...]:
    first = date.fromisoformat(start)
    return tuple(
        [
            (first + timedelta(days=offset)).isoformat()
            for offset in range(59)
        ]
        + [end]
    )


def _fold_manifest() -> FoldManifest:
    discovery_1 = FoldSpec(
        fold_id="discovery_1",
        purpose="discovery",
        start_date="2021-06-25",
        end_date="2021-09-20",
        sessions=_sessions("2021-06-25", "2021-09-20"),
    )
    discovery_2 = FoldSpec(
        fold_id="discovery_2",
        purpose="discovery",
        start_date="2021-09-21",
        end_date="2021-12-14",
        sessions=_sessions("2021-09-21", "2021-12-14"),
    )
    hidden = FoldSpec(
        fold_id="hidden_1",
        purpose="hidden",
        start_date="2021-12-15",
        end_date="2022-03-11",
        sessions=_sessions("2021-12-15", "2022-03-11"),
    )
    return FoldManifest(
        data_identity_sha256="a" * 64,
        universe_sha256="b" * 64,
        benchmark="SPY",
        warmup_start_date="2021-01-01",
        discovery_folds=(discovery_1, discovery_2),
        hidden_fold=hidden,
    )


def _call_budgets() -> tuple[contract.PitOptimizerCallBudget, ...]:
    role_caps = {
        "investigator": (8_000, 80_000, 88_000, 4_000, 8 * 1024, 0.05),
        "author": (12_000, 76_000, 88_000, 8_000, 16 * 1024, 0.10),
        "critic": (8_000, 24_000, 32_000, 4_000, 8 * 1024, 0.05),
    }
    return tuple(
        contract.PitOptimizerCallBudget(
            call_index=(iteration - 1) * 3 + ordinal,
            iteration=iteration,
            role=role,
            model="deepseek/deepseek-r1",
            max_static_input_bytes=role_caps[role][0],
            max_dynamic_input_bytes=role_caps[role][1],
            max_input_tokens=role_caps[role][2],
            max_output_tokens=role_caps[role][3],
            max_response_bytes=role_caps[role][4],
            max_usd=role_caps[role][5],
        )
        for iteration in (1, 2)
        for ordinal, role in enumerate(contract.OPTIMIZER_V2_ROLES, start=1)
    )


def _v2_manifest() -> contract.PitOptimizerRunManifest:
    candidate_bounds = contract.PatchBounds(3, 12, 80, 8 * 1024)
    hard_bounds = contract.PatchBounds(3, 12, 200, 64 * 1024)
    paths = (
        "core/strategy_policy/entry.py",
        "core/strategy_policy/risk.py",
        "core/strategy_policy/exit.py",
    )
    source_sha256s = tuple((path, str(index) * 64) for index, path in enumerate(paths, 1))
    scope = contract.PolicySourceScope(
        schema_version=2,
        policy_interface_version=1,
        initial_policy_source_sha256s=source_sha256s,
        editable_paths=paths,
        max_policy_source_bundle_bytes=64 * 1024,
        max_iteration_feedback_bytes=4 * 1024,
        max_iteration_history_bytes=32 * 1024,
        hard_patch_bounds=hard_bounds,
        candidate_bounds=candidate_bounds,
        max_iterations=2,
        allowed_descendant_rule="authenticated_initial_sources_plus_validated_cumulative_diff",
    )
    budgets = _call_budgets()
    constraint_ids = ("causal_only", "no_external_io")
    constraints_sha256 = hashlib.sha256(
        json.dumps(
            list(constraint_ids),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    ).hexdigest()
    authorization = contract.AuthorizationRequirement(
        window_id="window_1",
        max_calls=6,
        max_tokens=448_000,
        max_usd=0.40,
        policy_source_scope_sha256=scope.sha256,
        provider_retries=0,
        apply=False,
    )
    return contract.PitOptimizerRunManifest(
        schema_version=2,
        run_id="run_1",
        run_kind="subset_canary",
        model="deepseek/deepseek-r1",
        source_head="c" * 40,
        source_fingerprint_sha256="d" * 64,
        legacy_readiness_sha256="e" * 64,
        pit_bundle_sha256="a" * 64,
        baseline_manifest_sha256="f" * 64,
        effective_policy_sha256="9" * 64,
        policy_interface_version=1,
        policy_source_sha256s=source_sha256s,
        editable_paths=paths,
        policy_source_scope=scope,
        immutable_constraints_sha256=constraints_sha256,
        fold_manifest=_fold_manifest(),
        parity_attestation_sha256="7" * 64,
        sandbox_image="example.invalid/pit-optimizer@sha256:" + "6" * 64,
        validation_ledger_name="pit_optimizer_validation_ledger.jsonl",
        immutable_constraint_ids=constraint_ids,
        candidate_bounds=candidate_bounds,
        call_budgets=budgets,
        max_iterations=2,
        non_improving_limit=3,
        authorization_requirement=authorization,
    )


def _independent_digest(value: object) -> str:
    payload = (
        json.dumps(
            asdict(value),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    return hashlib.sha256(payload).hexdigest()


def test_manifest_identity_binds_scope_budget_order_and_authorization() -> None:
    """Break caught: expanded source or reordered calls could reuse an old authorization."""
    manifest = _v2_manifest()

    assert manifest.policy_source_scope.sha256 == _independent_digest(
        manifest.policy_source_scope
    )
    assert manifest.authorization_requirement.sha256 == _independent_digest(
        manifest.authorization_requirement
    )
    assert manifest.sha256 == _independent_digest(manifest)
    assert [
        (item.call_index, item.iteration, item.role)
        for item in manifest.call_budgets
    ] == [
        (1, 1, "investigator"),
        (2, 1, "author"),
        (3, 1, "critic"),
        (4, 2, "investigator"),
        (5, 2, "author"),
        (6, 2, "critic"),
    ]
    assert sum(item.max_input_tokens for item in manifest.call_budgets) == 416_000
    assert sum(item.max_output_tokens for item in manifest.call_budgets) == 32_000
    assert sum(
        item.max_input_tokens + item.max_output_tokens
        for item in manifest.call_budgets
    ) == 448_000
    assert manifest.authorization_requirement.max_tokens == 448_000
    assert sum(item.max_usd for item in manifest.call_budgets) == pytest.approx(0.40)
    assert manifest.authorization_requirement.apply is False
    assert manifest.authorization_requirement.provider_retries == 0

    expanded = replace(
        manifest.policy_source_scope,
        max_iterations=manifest.policy_source_scope.max_iterations + 1,
    )
    assert expanded.sha256 != manifest.policy_source_scope.sha256
    assert (
        manifest.authorization_requirement.policy_source_scope_sha256
        == manifest.policy_source_scope.sha256
    )
    with pytest.raises(ValueError, match="source scope authorization"):
        replace(manifest, policy_source_scope=expanded, max_iterations=3)
    with pytest.raises(ValueError, match="call order"):
        replace(
            manifest,
            call_budgets=(
                replace(manifest.call_budgets[0], call_index=2),
                *manifest.call_budgets[1:],
            ),
        )
    with pytest.raises(ValueError, match="hard patch bounds"):
        replace(
            manifest,
            candidate_bounds=contract.PatchBounds(3, 12, 201, 8 * 1024),
        )
    with pytest.raises(ValueError, match="apply"):
        replace(
            manifest.authorization_requirement,
            apply=True,
        )
    inflated_output = replace(
        manifest.call_budgets[0],
        max_output_tokens=manifest.call_budgets[0].max_output_tokens + 1,
    )
    with pytest.raises(ValueError, match="tokens exceed authorization"):
        replace(manifest, call_budgets=(inflated_output, *manifest.call_budgets[1:]))
    with pytest.raises(ValueError, match="exactly consume authorization"):
        replace(
            manifest,
            authorization_requirement=replace(
                manifest.authorization_requirement,
                max_tokens=448_001,
            ),
        )
    with pytest.raises(ValueError, match="exactly 448000"):
        replace(
            manifest,
            call_budgets=(inflated_output, *manifest.call_budgets[1:]),
            authorization_requirement=replace(
                manifest.authorization_requirement,
                max_tokens=448_001,
            ),
        )


_PIT_METADATA = {
    "bundle_kind": "canslim_pit_v1",
    "schema_version": "1",
    "data_cutoff": "2022-03-11",
    "evaluation_start": "2021-06-25",
    "warmup_start": "2020-01-01",
    "membership_source_sha256": "0" * 64,
    "prices_source_sha256": "0" * 64,
    "fundamentals_source_sha256": "0" * 64,
    "membership_provenance_sha256": "0" * 64,
    "prices_provenance_sha256": "0" * 64,
    "fundamentals_provenance_sha256": "0" * 64,
    "membership_source_kind": "offline_test_fixture",
    "membership_revision_id": "fixture-v1",
    "membership_raw_sha256": "0" * 64,
    "membership_symbol_map_sha256": "0" * 64,
    "membership_security_names_sha256": "0" * 64,
    "prices_source_kind": "offline_test_fixture",
    "prices_upstream_source_sha256": "0" * 64,
    "spy_trading_days_sha256": "0" * 64,
    "price_identity_map_sha256": "0" * 64,
    "price_identity_request_contracts_sha256": "0" * 64,
    "price_exclusion_count": "0",
    "price_exclusions_sha256": "0" * 64,
    "fundamentals_source_kind": "offline_test_fixture",
    "fundamentals_submissions_archive_sha256": "0" * 64,
    "fundamentals_companyfacts_archive_sha256": "0" * 64,
    "fundamentals_identity_manifest_csv_sha256": "0" * 64,
}


def _member_symbols(count: int) -> tuple[str, ...]:
    symbols: list[str] = []
    for letters in product(string.ascii_uppercase, repeat=3):
        ticker = "".join(letters)
        if ticker != "SPY":
            symbols.append(ticker)
        if len(symbols) == count:
            return tuple(symbols)
    raise AssertionError("could not create synthetic tickers")


def _write_pit_bundle(path: Path) -> tuple[str, tuple[str, ...]]:
    members = _member_symbols(495)
    fold_sessions = (
        *_sessions("2021-06-25", "2021-09-20"),
        *_sessions("2021-09-21", "2021-12-14"),
        *_sessions("2021-12-15", "2022-03-11"),
    )
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
            "INSERT INTO dataset_metadata VALUES (?, ?)", _PIT_METADATA.items()
        )
        connection.executemany(
            "INSERT INTO membership VALUES (?, ?, ?)",
            (("2021-01-01", symbol, 1) for symbol in members),
        )
        connection.executemany(
            "INSERT INTO price VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                [("2020-01-02", "SPY", 100.0, 101.0, 99.0, 100.0, 1_000.0)]
                + [
                    (session, "SPY", 100.0, 101.0, 99.0, 100.0, 1_000.0)
                    for session in fold_sessions
                ]
            ),
        )
        connection.execute(
            "INSERT INTO fundamentals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                members[0],
                "quarterly",
                "2020-09-30",
                "2020-11-01",
                1.0,
                1.0,
                100.0,
                10.0,
                None,
                None,
                1_000_000.0,
                None,
                None,
                None,
            ),
        )
        connection.commit()
    return hashlib.sha256(path.read_bytes()).hexdigest(), members[:25]


def _canonical_file_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _builder_fixture(
    tmp_path: Path,
    *,
    source_padding: int = 0,
) -> tuple[dict[str, object], dict[str, object]]:
    source_root = tmp_path / "source"
    source_texts = {
        "core/strategy_policy/entry.py": (
            "raise RuntimeError('must not import')\n"
            "def evaluate_entry(snapshot):\n"
            "    return None\n"
            + ("# bounded source padding\n" * source_padding)
        ),
        "core/strategy_policy/risk.py": "raise RuntimeError('must not import')\ndef recommend_capacity(snapshot):\n    return 1\n",
        "core/strategy_policy/exit.py": "raise RuntimeError('must not import')\ndef evaluate_exit(snapshot):\n    return None\n",
    }
    for relative, text in source_texts.items():
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    for arguments in (
        ("init",),
        ("config", "user.name", "Optimizer Contract Test"),
        ("config", "user.email", "optimizer@example.invalid"),
        ("add", "."),
        ("commit", "-m", "synthetic policy source"),
    ):
        subprocess.run(
            ["git", *arguments],
            cwd=source_root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    source_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source_root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    source_tree = subprocess.run(
        ["git", "ls-tree", "-r", "--full-tree", "HEAD"],
        cwd=source_root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout
    source_fingerprint = hashlib.sha256(source_tree).hexdigest()

    pit_bundle = tmp_path / "pit.sqlite3"
    pit_sha256, universe = _write_pit_bundle(pit_bundle)
    universe_sha256 = hashlib.sha256(_canonical_file_bytes(list(universe))).hexdigest()
    fold_manifest = FoldManifest(
        data_identity_sha256=pit_sha256,
        universe_sha256=universe_sha256,
        benchmark="SPY",
        warmup_start_date="2021-01-01",
        discovery_folds=(
            FoldSpec(
                "discovery_1",
                "discovery",
                "2021-06-25",
                "2021-09-20",
                _sessions("2021-06-25", "2021-09-20"),
            ),
            FoldSpec(
                "discovery_2",
                "discovery",
                "2021-09-21",
                "2021-12-14",
                _sessions("2021-09-21", "2021-12-14"),
            ),
        ),
        hidden_fold=FoldSpec(
            "hidden_1",
            "hidden",
            "2021-12-15",
            "2022-03-11",
            _sessions("2021-12-15", "2022-03-11"),
        ),
    )

    baseline_run = tmp_path / "baseline"
    baseline_run.mkdir()
    baseline_bytes = _canonical_file_bytes({"schema_version": 1, "run": "baseline"})
    (baseline_run / "run_manifest.json").write_bytes(baseline_bytes)
    baseline_sha256 = hashlib.sha256(baseline_bytes).hexdigest()
    policy = {"schema_version": 1, "policy": "synthetic"}
    effective_policy_sha256 = hashlib.sha256(_canonical_file_bytes(policy)).hexdigest()
    constraint_ids = ["causal_only", "no_external_io"]
    readiness = {
        "schema_version": 1,
        "gate": "pit_optimization",
        "phase": "ready",
        "identities": {
            "source_head": source_head,
            "source_fingerprint_sha256": source_fingerprint,
            "pit_bundle_sha256": pit_sha256,
            "baseline_manifest_sha256": baseline_sha256,
            "effective_policy_sha256": effective_policy_sha256,
        },
        "sealed_inputs": {
            "pit_bundle_sha256": pit_sha256,
            "baseline_artifact_sha256": {"run_manifest.json": baseline_sha256},
        },
        "evaluation_contract": {
            "verification_only": True,
            "scope": {
                "benchmark": "SPY",
                "discovery_start": "2021-06-25",
                "discovery_end": "2021-09-20",
                "holdout_start": "2021-09-21",
                "holdout_end": "2021-12-14",
                "warmup_start": "2021-01-01",
                "session_count": 60,
                "symbol_count": 25,
                "symbols": list(universe),
            },
        },
        "effective_policy": policy,
        "invariant_ids": constraint_ids,
    }
    readiness_path = tmp_path / "legacy-readiness.json"
    readiness_bytes = _canonical_file_bytes(readiness)
    readiness_path.write_bytes(readiness_bytes)

    parity_path = tmp_path / "verified-parity.json"
    provisional = ParityAttestation(
        schema_version=1,
        reference_artifact_sha256="4" * 64,
        reference_source_head="1" * 40,
        final_source_head=source_head,
        final_source_fingerprint_sha256=source_fingerprint,
        pit_bundle_sha256=pit_sha256,
        baseline_manifest_sha256=baseline_sha256,
        effective_policy_sha256=effective_policy_sha256,
        discovery_fold_manifest_sha256=fold_manifest.sha256,
        policy_interface_version=1,
        reference_output_sha256s=(),
        final_output_sha256s=(),
        final_discovery_evidence=(),
        transactions_equal=True,
        entry_outcomes_equal=True,
        equity_equal=True,
        funnels_equal=True,
        effective_policy_equal=True,
        artifact_path=parity_path.resolve(),
        artifact_sha256="0" * 64,
    )
    parity_primitive = asdict(provisional)
    parity_primitive.pop("artifact_path")
    parity_primitive.pop("artifact_sha256")
    parity_bytes = _canonical_file_bytes(parity_primitive)
    parity_path.write_bytes(parity_bytes)
    parity = replace(
        provisional,
        artifact_sha256=hashlib.sha256(parity_bytes).hexdigest(),
    )

    permanent_runtime_root = tmp_path / "runtime"
    controller_temp_parent = tmp_path / "controller-temp"
    artifact_root = tmp_path / "artifacts"
    for directory in (permanent_runtime_root, controller_temp_parent, artifact_root):
        directory.mkdir()
    inputs = {
        "legacy_readiness": readiness,
        "legacy_readiness_path": readiness_path,
        "parity_attestation": parity,
        "verified_parity_path": parity_path,
        "pit_bundle": pit_bundle,
        "baseline_run": baseline_run,
        "source_root": source_root,
        "permanent_runtime_root": permanent_runtime_root,
        "controller_temp_parent": controller_temp_parent,
        "artifact_root": artifact_root,
        "sandbox_image": "example.invalid/pit-optimizer@sha256:" + "6" * 64,
        "call_budgets": _call_budgets(),
        "candidate_bounds": contract.PatchBounds(3, 12, 80, 8 * 1024),
        "max_iterations": 2,
    }
    expected = {
        "fold_manifest": fold_manifest,
        "source_texts": source_texts,
        "readiness_sha256": hashlib.sha256(readiness_bytes).hexdigest(),
    }
    return inputs, expected


def _patch_authenticated_readiness(
    monkeypatch: pytest.MonkeyPatch,
    inputs: dict[str, object],
) -> None:
    import core.pit_policy_parity as parity

    readiness = inputs["legacy_readiness"]
    readiness_path = Path(inputs["legacy_readiness_path"])
    readiness_sha256 = hashlib.sha256(readiness_path.read_bytes()).hexdigest()

    def authenticate(path: Path, *, source_root: Path) -> tuple[dict[str, object], str]:
        assert Path(path).resolve() == readiness_path.resolve()
        assert Path(source_root).resolve() == Path(inputs["source_root"]).resolve()
        assert isinstance(readiness, dict)
        return readiness, readiness_sha256

    monkeypatch.setattr(parity, "_authenticated_readiness", authenticate)


def test_manifest_builder_is_provider_free_canonical_and_source_budgeted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: preparation could execute source or seal a context that cannot fit."""
    inputs, expected = _builder_fixture(tmp_path)
    _patch_authenticated_readiness(monkeypatch, inputs)

    manifest = contract.build_subset_manifest(**inputs)
    second = contract.build_subset_manifest(**inputs)

    assert manifest.fold_manifest == expected["fold_manifest"]
    assert manifest.legacy_readiness_sha256 == expected["readiness_sha256"]
    assert manifest.run_id != second.run_id
    assert (
        manifest.authorization_requirement.window_id
        != second.authorization_requirement.window_id
    )
    assert manifest.authorization_requirement.max_calls == 6
    assert manifest.authorization_requirement.max_tokens == 448_000
    assert manifest.authorization_requirement.max_usd == pytest.approx(0.40)
    assert manifest.authorization_requirement.apply is False
    assert manifest.authorization_requirement.provider_retries == 0

    source_records = []
    declared = {
        "core/strategy_policy/entry.py": ["core.strategy_policy.entry.evaluate_entry"],
        "core/strategy_policy/risk.py": ["core.strategy_policy.risk.recommend_capacity"],
        "core/strategy_policy/exit.py": ["core.strategy_policy.exit.evaluate_exit"],
    }
    for path, text in expected["source_texts"].items():
        source_records.append(
            {
                "path": path,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "declared_symbols": declared[path],
                "text": text,
            }
        )
    initial_bundle = {
        "policy_interface_version": 1,
        "cumulative_diff_sha256": hashlib.sha256(b"").hexdigest(),
        "cumulative_diff": "",
        "files": source_records,
    }
    initial_policy_bytes = sum(
        len(text.encode("utf-8")) for text in expected["source_texts"].values()
    )
    envelope_bytes = len(_canonical_text(initial_bundle).encode("utf-8")) - initial_policy_bytes
    assert initial_policy_bytes + (2 * 8 * 1024) + envelope_bytes <= 64 * 1024
    worst_records = [dict(record) for record in source_records]
    worst_records[0]["text"] = str(worst_records[0]["text"]) + ("s" * (8 * 1024))
    worst_records[0]["sha256"] = hashlib.sha256(
        str(worst_records[0]["text"]).encode("utf-8")
    ).hexdigest()
    worst_iteration_2_bundle = {
        "policy_interface_version": 1,
        "cumulative_diff_sha256": hashlib.sha256(
            ("d" * (8 * 1024)).encode("utf-8")
        ).hexdigest(),
        "cumulative_diff": "d" * (8 * 1024),
        "files": worst_records,
    }
    assert len(_canonical_text(worst_iteration_2_bundle).encode("utf-8")) == (
        initial_policy_bytes + (2 * 8 * 1024) + envelope_bytes
    )
    assert len(_canonical_text(worst_iteration_2_bundle).encode("utf-8")) <= 64 * 1024
    assert manifest.policy_source_sha256s == tuple(
        (record["path"], record["sha256"]) for record in source_records
    )
    rendered = contract.render_worst_iteration_two_role_inputs(
        scope=manifest.policy_source_scope,
        source_texts=expected["source_texts"],
        immutable_constraint_ids=manifest.immutable_constraint_ids,
        call_budgets=manifest.call_budgets,
    )
    for budget in manifest.call_budgets:
        static_bytes = len(
            contract.PIT_OPTIMIZER_V2_SYSTEM_PROMPTS[budget.role].encode("utf-8")
        ) + len(
            _canonical_text(contract.pit_optimizer_response_format(budget.role)).encode(
                "utf-8"
            )
        )
        assert static_bytes <= budget.max_static_input_bytes
        assert static_bytes + budget.max_dynamic_input_bytes <= budget.max_input_tokens
        if budget.iteration == 2:
            assert len(rendered[budget.role]) <= budget.max_dynamic_input_bytes
            assert static_bytes + len(rendered[budget.role]) <= budget.max_input_tokens

    output = tmp_path / "optimizer-manifest.json"
    written, digest = contract.write_optimizer_manifest(manifest, output)
    assert written == output.resolve()
    assert output.read_bytes() == _canonical_file_bytes(asdict(manifest))
    assert digest == manifest.sha256 == hashlib.sha256(output.read_bytes()).hexdigest()
    with pytest.raises(FileExistsError):
        contract.write_optimizer_manifest(manifest, output)

    with pytest.raises(ValueError, match="source bundle"):
        contract.build_subset_manifest(
            **{
                **inputs,
                "candidate_bounds": contract.PatchBounds(3, 12, 80, 32 * 1024),
            }
        )


def test_manifest_builder_rejects_fabricated_non_git_source_identity(
    tmp_path: Path,
) -> None:
    """Break caught: caller-provided source hashes could authorize unrelated file bytes."""
    inputs, _expected = _builder_fixture(tmp_path)
    inputs["source_root"] = Path(inputs["source_root"]) / "core"

    with pytest.raises(ValueError, match="Git repository root"):
        contract.build_subset_manifest(**inputs)


def test_manifest_builder_requires_complete_authenticated_v1_readiness(
    tmp_path: Path,
) -> None:
    """Break caught: a caller-selected readiness subset could bypass schema-v1 identity."""
    inputs, _expected = _builder_fixture(tmp_path)

    with pytest.raises(ValueError, match="closed readiness contract"):
        contract.build_subset_manifest(**inputs)


def test_manifest_builder_renders_and_rejects_oversized_iteration_two_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: declared arithmetic could hide an oversized actual role message."""
    inputs, _expected = _builder_fixture(tmp_path, source_padding=1_750)
    _patch_authenticated_readiness(monkeypatch, inputs)

    with pytest.raises(ValueError, match="worst iteration-2 investigator"):
        contract.build_subset_manifest(**inputs)


def test_gate_and_prepare_command_authenticate_without_granting_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a prepare command could imply spending or use an unauthenticated path."""
    inputs, _expected = _builder_fixture(tmp_path)
    _patch_authenticated_readiness(monkeypatch, inputs)
    manifest = contract.build_subset_manifest(**inputs)
    manifest_path = tmp_path / "optimizer-manifest.json"
    contract.write_optimizer_manifest(manifest, manifest_path)
    gate = contract.PitOptimizerGateConfig(
        phase="prepare",
        baseline_run=inputs["baseline_run"],
        baseline_manifest_sha256=manifest.baseline_manifest_sha256,
        pit_bundle=inputs["pit_bundle"],
        pit_bundle_sha256=manifest.pit_bundle_sha256,
        effective_policy_sha256=manifest.effective_policy_sha256,
        optimizer_manifest=manifest_path,
        optimizer_manifest_sha256=manifest.sha256,
        verified_parity_artifact=inputs["verified_parity_path"],
        verified_parity_sha256=manifest.parity_attestation_sha256,
        readiness_artifact=None,
        readiness_sha256=None,
        authorization_window_id=None,
        authorization_requirement_sha256=manifest.authorization_requirement.sha256,
        source_transmission_authorized=False,
        max_usd=0.40,
        max_api_calls=6,
        max_tokens=448_000,
        max_iterations=2,
        apply=False,
    )

    gate.validate()
    with pytest.raises(ValueError, match="source transmission"):
        replace(gate, source_transmission_authorized=True).validate()
    with pytest.raises(ValueError, match="apply"):
        replace(gate, apply=True).validate()
    with pytest.raises(ValueError, match="optimizer manifest"):
        replace(gate, optimizer_manifest_sha256="0" * 64).validate()
    parity_mismatch = replace(manifest, parity_attestation_sha256="8" * 64)
    parity_mismatch_path = tmp_path / "optimizer-manifest-parity-mismatch.json"
    contract.write_optimizer_manifest(parity_mismatch, parity_mismatch_path)
    with pytest.raises(ValueError, match="parity identity differs from manifest"):
        replace(
            gate,
            optimizer_manifest=parity_mismatch_path,
            optimizer_manifest_sha256=parity_mismatch.sha256,
        ).validate()
    run_gate = replace(
        gate,
        phase="run",
        readiness_artifact=inputs["legacy_readiness_path"],
        readiness_sha256=manifest.legacy_readiness_sha256,
        authorization_window_id=manifest.authorization_requirement.window_id,
        source_transmission_authorized=True,
    )
    run_gate.validate()
    readiness_mismatch = replace(manifest, legacy_readiness_sha256="8" * 64)
    readiness_mismatch_path = tmp_path / "optimizer-manifest-readiness-mismatch.json"
    contract.write_optimizer_manifest(readiness_mismatch, readiness_mismatch_path)
    with pytest.raises(ValueError, match="readiness identity differs from manifest"):
        replace(
            run_gate,
            optimizer_manifest=readiness_mismatch_path,
            optimizer_manifest_sha256=readiness_mismatch.sha256,
        ).validate()
    with pytest.raises(ValueError, match="authorization window"):
        replace(run_gate, authorization_window_id="window_live").validate()
    with pytest.raises(ValueError, match="apply"):
        replace(run_gate, apply=True).validate()

    git_executable = tmp_path / "git.exe"
    docker_executable = tmp_path / "docker.exe"
    git_executable.write_bytes(b"synthetic executable")
    docker_executable.write_bytes(b"synthetic executable")
    command = contract.build_prepare_command(
        manifest,
        manifest_path=manifest_path,
        legacy_readiness_path=inputs["legacy_readiness_path"],
        verified_parity_path=inputs["verified_parity_path"],
        pit_bundle_path=inputs["pit_bundle"],
        baseline_run_path=inputs["baseline_run"],
        repo_root=inputs["source_root"],
        permanent_runtime_root=inputs["permanent_runtime_root"],
        controller_temp_parent=inputs["controller_temp_parent"],
        artifact_root=inputs["artifact_root"],
        git_executable=git_executable,
        docker_executable=docker_executable,
        sandbox_image=inputs["sandbox_image"],
    )

    assert "core.pit_optimization prepare-v2" in command
    for value in (
        manifest.sha256,
        manifest.legacy_readiness_sha256,
        manifest.parity_attestation_sha256,
        manifest.pit_bundle_sha256,
        manifest.baseline_manifest_sha256,
        manifest.authorization_requirement.sha256,
        str(inputs["permanent_runtime_root"].resolve()),
        str(git_executable.resolve()),
        str(docker_executable.resolve()),
    ):
        assert value in command
    assert "authorization-window" not in command
    assert "source-transmission-authorized" not in command
    assert "credential" not in command.lower()


def test_objective_is_quantized_lexicographic_strict_and_trade_eligible() -> None:
    """Break caught: floating or non-strict ranking could promote an ineligible candidate."""
    folds = (
        replace(
            _fold_summary("discovery_1", 1.235),
            max_drawdown_pct=-4.005,
            closed_trades=1,
        ),
        replace(
            _fold_summary("discovery_2", 0.505),
            max_drawdown_pct=-2.0,
            closed_trades=3,
        ),
    )

    original_baseline = (
        _fold_summary("discovery_1", 0.0),
        _fold_summary("discovery_2", 0.0),
    )
    baseline_sha256 = _aggregate_sha256(original_baseline)
    score = evaluation.discovery_score_from_folds(
        folds,
        original_baseline,
        original_baseline_sha256=baseline_sha256,
        expected_original_baseline_sha256=baseline_sha256,
    )

    expected_first = Decimal(str(1.235)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_EVEN
    )
    expected_second = Decimal(str(0.505)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_EVEN
    )
    assert score == evaluation.DiscoveryScore(
        median_excess_return_pp=((expected_first + expected_second) / 2).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_EVEN
        ),
        worst_excess_return_pp=expected_second,
        max_drawdown_magnitude_pp=Decimal(str(4.005)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_EVEN
        ),
    )
    assert score.ordering_key == (
        Decimal("0.87"),
        Decimal("0.50"),
        Decimal("-4.00"),
    )
    assert evaluation.strictly_improves_discovery(score, score) is False
    assert evaluation.strictly_improves_discovery(
        replace(score, worst_excess_return_pp=Decimal("0.51")),
        score,
    ) is True
    with pytest.raises(ValueError, match="closed discovery trade"):
        evaluation.discovery_score_from_folds(
            (replace(folds[0], closed_trades=0), folds[1]),
            original_baseline,
            original_baseline_sha256=baseline_sha256,
            expected_original_baseline_sha256=baseline_sha256,
        )


def test_discovery_objective_derives_excess_from_authenticated_fixed_baseline(
    tmp_path: Path,
) -> None:
    """Break caught: fabricated excess or a substituted incumbent could drive ranking."""
    candidate = (
        replace(
            _fold_summary("discovery_1", 0.0),
            total_return_pct=2.235,
            excess_total_return_pp=99.0,
        ),
        replace(
            _fold_summary("discovery_2", 0.0),
            total_return_pct=1.505,
            excess_total_return_pp=-99.0,
        ),
    )
    original_baseline = (
        replace(_fold_summary("discovery_1", 0.0), total_return_pct=1.0),
        replace(_fold_summary("discovery_2", 0.0), total_return_pct=1.0),
    )
    baseline_sha256 = _aggregate_sha256(original_baseline)

    score = evaluation.discovery_score_from_folds(
        candidate,
        original_baseline,
        original_baseline_sha256=baseline_sha256,
        expected_original_baseline_sha256=baseline_sha256,
    )

    assert score.median_excess_return_pp == Decimal("0.87")
    assert score.worst_excess_return_pp == Decimal("0.50")
    with pytest.raises(ValueError, match="fixed baseline identity"):
        evaluation.discovery_score_from_folds(
            candidate,
            original_baseline,
            original_baseline_sha256=baseline_sha256,
            expected_original_baseline_sha256="f" * 64,
        )
    with pytest.raises(ValueError, match="supplied score differs"):
        contract.candidate_comparison_from_fixed_baseline(
            candidate_folds=candidate,
            original_baseline_folds=original_baseline,
            original_baseline_sha256=baseline_sha256,
            expected_original_baseline_sha256=baseline_sha256,
            discovery_exposure=_discovery_exposure_proof(tmp_path),
            diagnostics=(),
            supplied_score=replace(
                score,
                worst_excess_return_pp=score.worst_excess_return_pp
                + Decimal("0.01"),
            ),
        )


def test_holdout_gate_uses_return_trades_and_completeness_without_sharpe() -> None:
    """Break caught: a Sharpe gate or permissive equality boundary could alter eligibility."""
    decision = evaluation.HoldoutDecision.from_result(
        excess_total_return_pp=0.105,
        closed_trades=3,
        safety_complete=True,
        integrity_complete=True,
        accounting_complete=True,
    )

    assert decision.excess_total_return_pp == Decimal("0.10")
    assert decision.long_replay_eligible is True
    assert "sharpe" not in {field.name for field in fields(evaluation.HoldoutDecision)}
    assert evaluation.HoldoutDecision.from_result(
        excess_total_return_pp=0.094,
        closed_trades=3,
        safety_complete=True,
        integrity_complete=True,
        accounting_complete=True,
    ).long_replay_eligible is False
    assert evaluation.HoldoutDecision.from_result(
        excess_total_return_pp=0.10,
        closed_trades=2,
        safety_complete=True,
        integrity_complete=True,
        accounting_complete=True,
    ).long_replay_eligible is False
    with pytest.raises(ValueError, match="eligibility"):
        evaluation.HoldoutDecision(
            excess_total_return_pp=Decimal("0.10"),
            closed_trades=3,
            safety_complete=True,
            integrity_complete=True,
            accounting_complete=True,
            long_replay_eligible=False,
        )


def _validation_identity(fold: FoldSpec, suffix: str) -> evaluation.ValidationWindowIdentity:
    sessions_sha256 = hashlib.sha256(
        _canonical_file_bytes(list(fold.sessions))
    ).hexdigest()
    return evaluation.ValidationWindowIdentity(
        pit_bundle_sha256="a" * 64,
        universe_sha256="b" * 64,
        benchmark="SPY",
        warmup_contract_sha256="c" * 64,
        sessions_sha256=sessions_sha256,
        session_count=60,
        first_session=fold.start_date,
        last_session=fold.end_date,
    )


def test_validation_ledger_permanently_consumes_identity_before_outcome(
    tmp_path: Path,
) -> None:
    """Break caught: metadata changes or a failed evaluation could make a window hidden again."""
    ledger_path = tmp_path / "pit_optimizer_validation_ledger.jsonl"
    ledger = evaluation.ValidationLedger(ledger_path)
    folds = _fold_manifest()
    provider_metadata = evaluation.ValidationExposureMetadata(
        run_id="run_1",
        source_head="1" * 40,
        baseline_policy_sha256="d" * 64,
        candidate_identity_sha256=None,
        exposure_kind="provider_context",
    )
    discovery_reservations = tuple(
        ledger.mark_discovery(_validation_identity(fold, fold.fold_id), provider_metadata)
        for fold in folds.discovery_folds
    )

    first_identity = _validation_identity(folds.discovery_folds[0], "discovery_1")
    expected_key = hashlib.sha256(
        _canonical_file_bytes(asdict(first_identity))
    ).hexdigest()
    assert discovery_reservations[0].consumption_key_sha256 == expected_key
    with pytest.raises(ValueError, match="consumed"):
        ledger.reserve_hidden(
            first_identity,
            evaluation.ValidationExposureMetadata(
                run_id="run_later",
                source_head="2" * 40,
                baseline_policy_sha256="e" * 64,
                candidate_identity_sha256="f" * 64,
                exposure_kind="hidden_validation",
            ),
        )

    hidden_identity = _validation_identity(folds.hidden_fold, "hidden_1")
    hidden_metadata = evaluation.ValidationExposureMetadata(
        run_id="run_1",
        source_head="1" * 40,
        baseline_policy_sha256="d" * 64,
        candidate_identity_sha256="f" * 64,
        exposure_kind="hidden_validation",
    )
    hidden_reservation = ledger.reserve_hidden(hidden_identity, hidden_metadata)
    with pytest.raises(ValueError, match="outcome failure code is not closed"):
        ledger.record_outcome(
            hidden_reservation,
            attempted=True,
            completed=False,
            failure_code="invented_failure",
        )
    ledger.record_outcome(
        hidden_reservation,
        attempted=True,
        completed=False,
        failure_code="worker_failed",
    )

    reopened = evaluation.ValidationLedger(ledger_path)
    with pytest.raises(ValueError, match="consumed"):
        reopened.reserve_hidden(
            hidden_identity,
            replace(hidden_metadata, run_id="run_later", source_head="2" * 40),
        )
    records = [json.loads(line) for line in ledger_path.read_text("utf-8").splitlines()]
    assert [record["record_type"] for record in records] == [
        "consumption",
        "consumption",
        "consumption",
        "outcome",
    ]
    assert records[1]["previous_record_sha256"] == records[0]["record_sha256"]
    outcome = records[-1]
    assert set(outcome) == {
        "schema_version",
        "record_type",
        "record_index",
        "previous_record_sha256",
        "reservation_record_sha256",
        "attempted",
        "completed",
        "failure_code",
        "record_sha256",
    }
    assert not {"return", "sharpe", "trades", "holdings", "metrics"}.intersection(
        outcome
    )


def test_validation_ledger_atomically_rejects_concurrent_duplicate_reservation(
    tmp_path: Path,
) -> None:
    """Break caught: two controllers could race the same fold past uniqueness."""
    ledger_path = tmp_path / "pit_optimizer_validation_ledger.jsonl"
    identity = _validation_identity(_fold_manifest().hidden_fold, "hidden_1")
    metadata = evaluation.ValidationExposureMetadata(
        run_id="run_1",
        source_head="1" * 40,
        baseline_policy_sha256="d" * 64,
        candidate_identity_sha256="f" * 64,
        exposure_kind="hidden_validation",
    )
    barrier = threading.Barrier(2)

    def reserve() -> str:
        ledger = evaluation.ValidationLedger(ledger_path)
        barrier.wait(timeout=5)
        try:
            ledger.reserve_hidden(identity, metadata)
        except ValueError as exc:
            return str(exc)
        return "reserved"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _index: reserve(), range(2)))

    assert results.count("reserved") == 1
    assert sum("permanently consumed" in result for result in results) == 1
    records = ledger_path.read_text("utf-8").splitlines()
    assert len(records) == 1
