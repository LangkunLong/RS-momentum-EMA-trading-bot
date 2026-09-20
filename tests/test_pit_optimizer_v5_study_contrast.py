"""Task 5 raw case-contrast tests."""

from __future__ import annotations

from decimal import Decimal
from dataclasses import replace

import pytest

from core.pit_optimizer_v5.contracts import ArtifactRefV5, HypothesisV5, MetricPredictionV5
from core.pit_optimizer_v5.mechanism_contracts import (
    MechanismControlV1,
    MechanismExperimentSpecV1,
    MechanismMetricSpecV1,
    MechanismPredicateV1,
    MechanismRecipeV1,
    MechanismResourceBudgetV1,
    bind_mechanism_observation_v1,
)
from core.pit_optimizer_v5.mechanism_probes import (
    MechanismObservationRunV1,
    MechanismWorkerRegistrationV1,
    SyntheticFixtureWorkerV1,
    build_mechanism_observation_corpus_v1,
    collect_mechanism_observations_v1,
)
from core.pit_optimizer_v5.probes import policy_probe_suite_v1
from core.pit_optimizer_v5.two_round_study.contrast import (
    CaseContrastResultV1,
    StudyContrastV1,
    compare_case_patterns_v1,
    evaluate_case_contrast_v1,
)
from core.pit_optimizer_v5.two_round_study.contracts import RivalPatternV1, StudyContractError
from core.pit_optimizer_v5.two_round_study.registry import (
    build_study_registry_v1,
    registry_decision_v1,
)


@pytest.mark.parametrize(
    "observed,status,mismatches",
    [
        ((False, True, True, False), "matched_on_cases", ()),
        ((False, False, False, False), "contradicted_on_cases", (1, 2)),
        ((True, True, True, True), "contradicted_on_cases", (0, 3)),
        (None, "unavailable", ()),
    ],
)
def test_all_case_pattern(observed, status, mismatches) -> None:
    assert compare_case_patterns_v1(expected=(False, True, True, False), observed=observed) == (
        status,
        mismatches,
    )


def test_case_pattern_helper_rejects_length_and_non_boolean_values() -> None:
    with pytest.raises(StudyContractError):
        compare_case_patterns_v1(expected=(False, True), observed=(False,))
    with pytest.raises(StudyContractError):
        compare_case_patterns_v1(expected=(False, True), observed=(False, 1))


def _spec() -> MechanismExperimentSpecV1:
    hypothesis = HypothesisV5(
        hypothesis_id="hyp.exit.atr",
        rank=1,
        primary_mechanism="exit",
        causal_claim="The registered ATR threshold changes exit decisions.",
        predicted_changes=(
            MetricPredictionV5("exit.decision_changed_count", "increase", "Applicable cases change."),
        ),
        evidence_ids=("v5.fixture.evidence",),
        author_instructions="Use the registered exit recipe.",
    )
    return MechanismExperimentSpecV1(
        precommitment_id="v5.counterexample",
        hypothesis_id=hypothesis.hypothesis_id,
        hypothesis_sha256=hypothesis.sha256,
        parent_revision_sha256="a" * 64,
        round_intent_sha256="b" * 64,
        target_method="evaluate_exit",
        allowed_symbols=("core.strategy_policy.v3.exit.evaluate_exit",),
        applicability=MechanismPredicateV1(
            field="features.atr_20_fraction",
            operator="gte",
            value=Decimal("0.50"),
        ),
        controls=(MechanismControlV1(control_id="protected_next_stop_price"),),
        metrics=(
            MechanismMetricSpecV1(
                metric_id="exit.decision_changed_count",
                unit="count",
                direction="increase",
                tolerance=Decimal("0"),
                denominator="relevant_cases",
            ),
            MechanismMetricSpecV1(
                metric_id="exit.protected_control_unchanged_count",
                unit="count",
                direction="unchanged",
                tolerance=Decimal("0"),
                denominator="control_cases",
            ),
        ),
        aggregation="paired_case_delta",
        minimum_relevant_cases=2,
        recipe=MechanismRecipeV1(
            recipe_id="evaluate_exit_atr20_fraction_v1",
            input_field="features.atr_20_fraction",
            input_values=(Decimal("0.49"), Decimal("0.50"), Decimal("0.51"), None),
        ),
        disconfirming_observations=(),
        provenance="synthetic_offline",
    )


def _seed_snapshot():
    return next(item.snapshot for item in policy_probe_suite_v1() if item.method == "evaluate_exit")


def _run(
    configuration_id: str,
    *,
    missing_parent_case: bool = False,
) -> tuple[MechanismExperimentSpecV1, object, MechanismObservationRunV1]:
    registry = build_study_registry_v1()
    # The registered S descendants are paired against their actual registered
    # S parent.  Both the threshold and always-on descendants therefore have
    # the same 2/2 relevant metric while retaining different raw patterns.
    parent_configuration_id = "S"
    spec = replace(_spec(), parent_revision_sha256=registry.configuration(parent_configuration_id).policy_revision.sha256)
    corpus = build_mechanism_observation_corpus_v1(spec, seed_snapshot=_seed_snapshot())
    binding = bind_mechanism_observation_v1(
        spec,
        experiment_id="c" * 64,
        candidate_bytes_sha256="d" * 64,
        corpus_sha256=corpus.sha256,
        evaluator_contract_sha256="e" * 64,
        scenario_id="base",
        resource_budget=MechanismResourceBudgetV1(
            max_cases=8,
            max_repetitions=2,
            timeout_ms=1000,
            cpu_seconds=Decimal("2"),
            memory_mib=128,
            output_bytes=8192,
        ),
    )
    parent_decisions = {
        case.input_identity_sha256: registry_decision_v1(
            registry=registry,
            configuration_id=parent_configuration_id,
            method="evaluate_exit",
            snapshot=case.snapshot,
        )
        for case in corpus.cases
    }
    if missing_parent_case:
        del parent_decisions[corpus.cases[1].input_identity_sha256]
    candidate_decisions = {
        case.input_identity_sha256: registry_decision_v1(
            registry=registry,
            configuration_id=configuration_id,
            method="evaluate_exit",
            snapshot=case.snapshot,
        )
        for case in corpus.cases
    }
    parent = SyntheticFixtureWorkerV1(
        registration=MechanismWorkerRegistrationV1(
            experiment_id=binding.experiment_id,
            spec_sha256=binding.spec_sha256,
            role="parent",
            parent_revision_sha256=binding.parent_revision_sha256,
            candidate_bytes_sha256=binding.candidate_bytes_sha256,
            corpus_sha256=binding.corpus_sha256,
            resource_budget=binding.resource_budget,
            execution_kind="synthetic_fixture",
            reset_semantics="reset_per_case",
            cpu_memory_enforced=False,
        ),
        decisions_by_input_identity=parent_decisions,
    )
    candidate = SyntheticFixtureWorkerV1(
        registration=replace(parent.registration, role="candidate"),
        decisions_by_input_identity=candidate_decisions,
    )
    return spec, corpus, collect_mechanism_observations_v1(
        spec,
        binding,
        corpus,
        parent_worker=parent,
        candidate_worker=candidate,
    )


def _contrast(spec: MechanismExperimentSpecV1, corpus) -> StudyContrastV1:
    binding_hash = "f" * 64
    return StudyContrastV1(
        draft_binding_ref=ArtifactRefV5("adapter-blobs/study-v1-draft-bindings/f.bin", binding_hash),
        draft_binding_sha256=binding_hash,
        parent_revision_sha256=spec.parent_revision_sha256,
        parent_source_bundle_ref=ArtifactRefV5("parents/source.json", "1" * 64),
        spec_sha256=spec.sha256,
        corpus_sha256=corpus.sha256,
        precommitment_id="v5.counterexample",
        case_ids=tuple(f"case-{index}" for index in range(len(corpus.cases))),
        input_identities=tuple(case.input_identity_sha256 for case in corpus.cases),
        expected_changed=(False, True, True, False),
        rivals=(
            RivalPatternV1("inert", (False, False, False, False)),
            RivalPatternV1("always_on", (True, True, True, True)),
        ),
        claim_kind="threshold",
    )


def test_production_reducer_counterexample_separates_threshold_from_always_on() -> None:
    threshold_spec, threshold_corpus, threshold_run = _run("S-gte-0.50")
    contrast = _contrast(threshold_spec, threshold_corpus)
    threshold_result = evaluate_case_contrast_v1(contrast=contrast, run=threshold_run)

    assert threshold_run.relevant_case_count == 2
    assert threshold_run.decision_changed_count == 2
    assert threshold_run.protected_control_unchanged_count == 4
    assert threshold_result.status == "matched_on_cases"
    assert threshold_result.observed_pattern == (False, True, True, False)

    _always_spec, _always_corpus, always_run = _run("S-always-on")
    always_result = evaluate_case_contrast_v1(contrast=contrast, run=always_run)
    assert always_run.relevant_case_count == 2
    assert always_run.decision_changed_count == 2
    assert always_run.protected_control_unchanged_count == 4
    assert always_result.status == "contradicted_on_cases"
    assert always_result.mismatched_case_ids == ("case-0", "case-3")


def test_failed_run_is_unavailable_and_never_padded_with_false_observations() -> None:
    spec, corpus, _completed = _run("S-gte-0.50")
    # Rebuild the exact run with a fixture worker failure before its first case.
    # ``MechanismObservationRunV1`` then carries a failed execution and an
    # empty prefix, which the contrast layer must preserve as unavailable.
    registry = build_study_registry_v1()
    binding = bind_mechanism_observation_v1(
        spec,
        experiment_id="c" * 64,
        candidate_bytes_sha256="d" * 64,
        corpus_sha256=corpus.sha256,
        evaluator_contract_sha256="e" * 64,
        scenario_id="base",
        resource_budget=MechanismResourceBudgetV1(8, 2, 1000, Decimal("2"), 128, 8192),
    )
    decisions = {
        case.input_identity_sha256: registry_decision_v1(
            registry=registry, configuration_id="P0", method="evaluate_exit", snapshot=case.snapshot
        )
        for case in corpus.cases
    }
    parent = SyntheticFixtureWorkerV1(
        registration=MechanismWorkerRegistrationV1(
            binding.experiment_id,
            binding.spec_sha256,
            "parent",
            binding.parent_revision_sha256,
            binding.candidate_bytes_sha256,
            binding.corpus_sha256,
            binding.resource_budget,
            "synthetic_fixture",
            "reset_per_case",
            False,
        ),
        decisions_by_input_identity=decisions,
    )
    parent.failure = RuntimeError("fixture failure")
    candidate = SyntheticFixtureWorkerV1(
        registration=replace(parent.registration, role="candidate"),
        decisions_by_input_identity=decisions,
    )
    run = collect_mechanism_observations_v1(
        spec, binding, corpus, parent_worker=parent, candidate_worker=candidate
    )
    result = evaluate_case_contrast_v1(contrast=_contrast(spec, corpus), run=run)
    assert run.execution.status == "failed"
    assert result.status == "unavailable"
    assert result.observed_pattern is None


def test_partial_run_reports_missing_cases_and_never_pads_the_prefix() -> None:
    spec, corpus, run = _run("S-gte-0.50", missing_parent_case=True)
    result = evaluate_case_contrast_v1(contrast=_contrast(spec, corpus), run=run)
    assert run.execution.status == "failed"
    assert len(run.observations) == 1
    assert result.status == "unavailable"
    assert result.observed_pattern is None
    assert result.missing_case_ids == ("case-1", "case-2", "case-3")


def test_unavailable_result_rejects_mismatched_case_ids_without_observation() -> None:
    with pytest.raises(StudyContractError):
        CaseContrastResultV1(
            contract_ref="a" * 64,
            run_ref="b" * 64,
            status="unavailable",
            observed_changed=None,
            mismatched_case_ids=("case-0",),
            missing_case_ids=(),
            limitations=("incomplete",),
        )


def test_unavailable_result_canonical_decode_rejects_mismatched_case_ids() -> None:
    result = {
        "contract_ref": "a" * 64,
        "run_ref": "b" * 64,
        "status": "unavailable",
        "observed_changed": None,
        "mismatched_case_ids": ["case-0"],
        "missing_case_ids": [],
        "limitations": ["incomplete"],
        "schema_version": 1,
    }
    from core.pit_optimizer_v5.contracts import canonical_json_bytes_v5

    with pytest.raises(StudyContractError):
        CaseContrastResultV1.from_canonical_json(canonical_json_bytes_v5(result))


def test_foreign_contrast_graph_is_unavailable_and_result_is_canonical() -> None:
    spec, corpus, run = _run("S-gte-0.50")
    contrast = _contrast(spec, corpus)
    foreign = replace(contrast, parent_revision_sha256="9" * 64)
    result = evaluate_case_contrast_v1(contrast=foreign, run=run)
    assert result.status == "unavailable"
    assert result.observed_pattern is None
    assert result.missing_case_ids == contrast.case_ids
    assert CaseContrastResultV1.from_canonical_json(result.canonical_bytes()) == result


def test_typed_contrast_rejects_identical_claim_and_rival_patterns() -> None:
    spec, corpus, _run_value = _run("S-gte-0.50")
    with pytest.raises(StudyContractError):
        StudyContrastV1(
            draft_binding_ref=ArtifactRefV5("adapter-blobs/study-v1-draft-bindings/f.bin", "f" * 64),
            draft_binding_sha256="f" * 64,
            parent_revision_sha256=spec.parent_revision_sha256,
            parent_source_bundle_ref=ArtifactRefV5("parents/source.json", "1" * 64),
            spec_sha256=spec.sha256,
            corpus_sha256=corpus.sha256,
            precommitment_id="v5.counterexample",
            case_ids=tuple(f"case-{index}" for index in range(4)),
            input_identities=tuple(case.input_identity_sha256 for case in corpus.cases),
            expected_changed=(False, True, True, False),
            rivals=(RivalPatternV1("inert", (False, True, True, False)),),
        )


def test_threshold_contrast_requires_named_inert_and_always_on_rivals() -> None:
    spec, corpus, _run_value = _run("S-gte-0.50")
    with pytest.raises(StudyContractError):
        StudyContrastV1(
            draft_binding_ref=ArtifactRefV5("adapter-blobs/study-v1-draft-bindings/f.bin", "f" * 64),
            draft_binding_sha256="f" * 64,
            parent_revision_sha256=spec.parent_revision_sha256,
            parent_source_bundle_ref=ArtifactRefV5("parents/source.json", "1" * 64),
            spec_sha256=spec.sha256,
            corpus_sha256=corpus.sha256,
            precommitment_id="v5.counterexample",
            case_ids=tuple(f"case-{index}" for index in range(4)),
            input_identities=tuple(case.input_identity_sha256 for case in corpus.cases),
            expected_changed=(False, True, True, False),
            rivals=(RivalPatternV1("inert", (False, False, False, False)),),
            claim_kind="threshold",
        )
