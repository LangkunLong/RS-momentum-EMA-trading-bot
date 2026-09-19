"""Focused pure-contract checks for V5 mechanism evidence."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path

import pytest

from core.pit_optimizer_v5.contracts import (
    ArtifactRefV5,
    EpisodeEvaluationV5,
    EpisodePlanV5,
    EvaluationReportV5,
    HypothesisV5,
    MetricCountV5,
    MetricPredictionV5,
    PanelEvaluationV5,
    ScenarioPanelEvaluationV5,
)
from core.pit_optimizer_v5.probes import canonical_probe_json_v5, policy_probe_suite_v1
from core.pit_optimizer_v5.candidate_ir import (
    PolicyRevisionIdentityV5,
    SourceBundleV5,
    SourceFileV5,
    derive_policy_revision_identity_v5,
)
from core.pit_optimizer_v5.policy_scope import EDITABLE_POLICY_PATHS_V5
from core.pit_optimizer_v5.mechanism_contracts import (
    MECHANISM_MAX_CANONICAL_JSON_BYTES_V1,
    MechanismControlV1,
    MechanismDiagnosticSelectorV1,
    MechanismDisconfirmingObservationV1,
    MechanismExperimentSpecV1,
    MechanismEvidenceReportV1,
    MechanismExecutionV1,
    MechanismLearningProjectionV1,
    MechanismMetricSpecV1,
    MechanismObservationBindingV1,
    MechanismPredicateV1,
    MechanismResourceUsageV1,
    MechanismRecipeV1,
    MechanismResourceBudgetV1,
    MechanismCoverageV1,
    MechanismPredictionResultV1,
    MechanismEvaluatorContextV1,
    bind_mechanism_observation_v1,
    validate_mechanism_report_against_spec_v1,
    validate_mechanism_spec_hypothesis_v1,
    validate_mechanism_observation_binding_v1,
)
from core.pit_optimizer_v5.mechanism_probes import (
    MechanismObservationCorpusV1,
    MechanismObservationRunV1,
    MechanismPairedCaseV1,
    MechanismPairedObservationV1,
    MechanismWorkerRegistrationV1,
    SyntheticFixtureWorkerV1,
    build_mechanism_observation_corpus_v1,
    collect_mechanism_observations_v1,
    validate_mechanism_observation_corpus_v1,
)
from core.pit_optimizer_v5.mechanism_reports import (
    MechanismEvaluatorMatchV1,
    build_mechanism_evidence_report_v1,
)


def _hypothesis() -> HypothesisV5:
    return HypothesisV5(
        hypothesis_id="hyp.exit.atr",
        rank=1,
        primary_mechanism="exit",
        causal_claim="ATR-aware exit behavior changes exit decisions on applicable holdings.",
        predicted_changes=(
            MetricPredictionV5(
                metric_id="exit.decision_changed_count",
                direction="increase",
                rationale="The mechanism should alter the exit decision on applicable cases.",
            ),
        ),
        evidence_ids=("v5.hypothesis.evidence",),
        author_instructions="Use the registered exit mechanism recipe.",
    )


def _spec(*, frozen_before_authoring: bool = True) -> MechanismExperimentSpecV1:
    hypothesis = _hypothesis()
    return MechanismExperimentSpecV1(
        precommitment_id="v5.precommit.exit.atr",
        hypothesis_id=hypothesis.hypothesis_id,
        hypothesis_sha256=hypothesis.sha256,
        parent_revision_sha256="a" * 64,
        round_intent_sha256="b" * 64,
        target_method="evaluate_exit",
        allowed_symbols=("core.strategy_policy.v3.exit.evaluate_exit",),
        applicability=MechanismPredicateV1(
            field="features.atr_20_fraction",
            operator="is_present",
            value=None,
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
                denominator="relevant_cases",
            ),
            MechanismMetricSpecV1(
                metric_id="evaluator.exit_attribution_count",
                unit="count",
                direction="increase",
                tolerance=Decimal("0"),
                denominator="evaluator_cases",
                selector=MechanismDiagnosticSelectorV1(
                    section="exit_attribution",
                    metric_id="ma_violation",
                ),
            ),
        ),
        aggregation="paired_case_delta",
        minimum_relevant_cases=2,
        recipe=MechanismRecipeV1(
            recipe_id="evaluate_exit_atr20_fraction_v1",
            input_field="features.atr_20_fraction",
            input_values=(Decimal("0.20"), Decimal("0.50"), Decimal("0.80"), None),
        ),
        disconfirming_observations=(
            MechanismDisconfirmingObservationV1(
                observation_id="protected_control_changed",
                metric_id="exit.protected_control_unchanged_count",
            ),
        ),
        provenance="synthetic_offline",
        frozen_before_authoring=frozen_before_authoring,
    )


def _binding(spec: MechanismExperimentSpecV1) -> MechanismObservationBindingV1:
    return bind_mechanism_observation_v1(
        spec,
        experiment_id="1" * 64,
        candidate_bytes_sha256="c" * 64,
        corpus_sha256="d" * 64,
        evaluator_contract_sha256="e" * 64,
        scenario_id="base",
        resource_budget=MechanismResourceBudgetV1(
            max_cases=16,
            max_repetitions=1,
            timeout_ms=1000,
            cpu_seconds=Decimal("2"),
            memory_mib=128,
            output_bytes=4096,
        ),
    )


def _exit_seed() -> object:
    return next(case.snapshot for case in policy_probe_suite_v1() if case.method == "evaluate_exit")


def _observation_spec(
    *, values: tuple[Decimal | None, ...] = (Decimal("0.40"), Decimal("0.50"), Decimal("0.60"), None)
) -> MechanismExperimentSpecV1:
    return replace(
        _spec(),
        applicability=MechanismPredicateV1(
            field="features.atr_20_fraction",
            operator="gte",
            value=Decimal("0.50"),
        ),
        recipe=MechanismRecipeV1(
            recipe_id="evaluate_exit_atr20_fraction_v1",
            input_field="features.atr_20_fraction",
            input_values=values,
        ),
    )


def _observation_binding(
    spec: MechanismExperimentSpecV1,
    corpus: MechanismObservationCorpusV1,
    *,
    budget: MechanismResourceBudgetV1 | None = None,
) -> MechanismObservationBindingV1:
    selected_budget = budget or MechanismResourceBudgetV1(
        max_cases=16,
        max_repetitions=2,
        timeout_ms=1000,
        cpu_seconds=Decimal("2"),
        memory_mib=128,
        output_bytes=64 * 1024,
    )
    return bind_mechanism_observation_v1(
        spec,
        experiment_id="1" * 64,
        candidate_bytes_sha256="c" * 64,
        corpus_sha256=corpus.sha256,
        evaluator_contract_sha256="e" * 64,
        scenario_id="base",
        resource_budget=selected_budget,
    )


def _identity_fixture() -> tuple[
    SourceBundleV5,
    PolicyRevisionIdentityV5,
    SourceBundleV5,
    PolicyRevisionIdentityV5,
    SourceBundleV5,
    PolicyRevisionIdentityV5,
]:
    root = Path(__file__).resolve().parents[1]

    def bundle(*, suffix: str = "") -> SourceBundleV5:
        files = []
        for path in EDITABLE_POLICY_PATHS_V5:
            source = (root / path).read_text(encoding="utf-8")
            if path == EDITABLE_POLICY_PATHS_V5[-1] and suffix:
                source = source.rstrip("\n") + f"\n# {suffix}\n"
            files.append(SourceFileV5(path=path, source=source))
        return SourceBundleV5(files=tuple(files))

    parent_bundle = bundle()
    candidate_bundle = bundle(suffix="candidate fixture")
    foreign_bundle = bundle(suffix="foreign fixture")
    parent_revision = derive_policy_revision_identity_v5(
        source_bundle=parent_bundle,
        trusted_policy_runtime_sha256="1" * 64,
        immutable_constraints_sha256="2" * 64,
    )
    candidate_revision = derive_policy_revision_identity_v5(
        source_bundle=candidate_bundle,
        trusted_policy_runtime_sha256="1" * 64,
        immutable_constraints_sha256="2" * 64,
    )
    foreign_revision = derive_policy_revision_identity_v5(
        source_bundle=foreign_bundle,
        trusted_policy_runtime_sha256="1" * 64,
        immutable_constraints_sha256="2" * 64,
    )
    return (
        parent_bundle,
        parent_revision,
        candidate_bundle,
        candidate_revision,
        foreign_bundle,
        foreign_revision,
    )


def _identity_run() -> tuple[
    MechanismExperimentSpecV1,
    MechanismObservationBindingV1,
    MechanismObservationRunV1,
    SourceBundleV5,
    PolicyRevisionIdentityV5,
    SourceBundleV5,
    PolicyRevisionIdentityV5,
]:
    parent_bundle, parent_revision, candidate_bundle, candidate_revision, _, _ = _identity_fixture()
    spec = replace(_observation_spec(), parent_revision_sha256=parent_revision.sha256)
    corpus = build_mechanism_observation_corpus_v1(spec, seed_snapshot=_exit_seed())
    binding = replace(_observation_binding(spec, corpus), candidate_bytes_sha256=candidate_bundle.sha256)
    parent_worker, candidate_worker = _fixture_workers(binding, corpus)
    run = collect_mechanism_observations_v1(
        spec,
        binding,
        corpus,
        parent_worker=parent_worker,
        candidate_worker=candidate_worker,
    )
    return spec, binding, run, parent_bundle, parent_revision, candidate_bundle, candidate_revision


def _worker_registration(
    binding: MechanismObservationBindingV1,
    *,
    role: str,
    execution_kind: str = "synthetic_fixture",
    cpu_memory_enforced: bool = False,
) -> MechanismWorkerRegistrationV1:
    return MechanismWorkerRegistrationV1(
        experiment_id=binding.experiment_id,
        spec_sha256=binding.spec_sha256,
        role=role,
        parent_revision_sha256=binding.parent_revision_sha256,
        candidate_bytes_sha256=binding.candidate_bytes_sha256,
        corpus_sha256=binding.corpus_sha256,
        resource_budget=binding.resource_budget,
        execution_kind=execution_kind,
        reset_semantics="reset_per_case",
        cpu_memory_enforced=cpu_memory_enforced,
    )


def _neutral_exit(snapshot: object) -> object:
    from core.strategy_policy.contracts import ExitDecision

    return ExitDecision(
        actions=(),
        next_stop_price=None,
        early_winner_hold=snapshot.base.early_winner_hold,
        scale_out_tier=snapshot.base.scale_out_tier,
        breakeven_armed=snapshot.base.breakeven_armed,
        ema_trailing_active=snapshot.base.ema_trailing_active,
    )


def _fixture_workers(
    binding: MechanismObservationBindingV1,
    corpus: MechanismObservationCorpusV1,
    *,
    changed_inputs: tuple[str, ...] = (),
) -> tuple[SyntheticFixtureWorkerV1, SyntheticFixtureWorkerV1]:
    from core.strategy_policy.contracts import ExitDecision

    parent_decisions = {case.input_identity_sha256: _neutral_exit(case.snapshot) for case in corpus.cases}
    candidate_decisions = dict(parent_decisions)
    for case in corpus.cases:
        if case.input_identity_sha256 in changed_inputs:
            candidate_decisions[case.input_identity_sha256] = ExitDecision(
                actions=(),
                next_stop_price=case.snapshot.base.protective_stop_candidates[-1],
                early_winner_hold=case.snapshot.base.early_winner_hold,
                scale_out_tier=case.snapshot.base.scale_out_tier,
                breakeven_armed=case.snapshot.base.breakeven_armed,
                ema_trailing_active=case.snapshot.base.ema_trailing_active,
            )
    return (
        SyntheticFixtureWorkerV1(
            registration=_worker_registration(binding, role="parent"),
            decisions_by_input_identity=parent_decisions,
        ),
        SyntheticFixtureWorkerV1(
            registration=_worker_registration(binding, role="candidate"),
            decisions_by_input_identity=candidate_decisions,
        ),
    )


def test_spec_round_trips_canonically_and_digest_tracks_meaningful_changes() -> None:
    spec = _spec()

    encoded = spec.to_canonical_json()
    decoded = MechanismExperimentSpecV1.from_canonical_json(encoded)

    assert decoded == spec
    assert decoded.sha256 == spec.sha256
    assert replace(spec, minimum_relevant_cases=3).sha256 != spec.sha256


def test_canonical_decode_rejects_unknown_duplicate_wrong_numeric_and_nonfinite_fields() -> None:
    predicate = MechanismPredicateV1(field="features.atr_20_fraction", operator="is_present", value=None)
    encoded = predicate.to_canonical_json()

    unknown = json.loads(encoded)
    unknown["unexpected"] = True
    with pytest.raises(ValueError, match="unknown"):
        MechanismPredicateV1.from_canonical_json(json.dumps(unknown, separators=(",", ":")))
    with pytest.raises(ValueError, match="duplicate"):
        MechanismPredicateV1.from_canonical_json(
            '{"field":"features.atr_20_fraction","field":"features.atr_20_fraction",'
            '"operator":"is_present","value":null}'
        )
    with pytest.raises(ValueError, match="numeric"):
        MechanismPredicateV1.from_canonical_json('{"field":"features.atr_20_fraction","operator":"gte","value":0.5}')
    with pytest.raises(ValueError, match="canonical JSON"):
        MechanismPredicateV1.from_canonical_json('{"field":"features.atr_20_fraction","operator":"gte","value":NaN}')


def test_closed_vocab_rejects_unsupported_metric_unit_recipe_and_predicate() -> None:
    with pytest.raises(ValueError, match="metric"):
        MechanismMetricSpecV1(
            metric_id="made_up.metric",
            unit="count",
            direction="increase",
            tolerance=Decimal("0"),
            denominator="relevant_cases",
        )
    with pytest.raises(ValueError, match="unit"):
        MechanismMetricSpecV1(
            metric_id="exit.decision_changed_count",
            unit="percent",
            direction="increase",
            tolerance=Decimal("0"),
            denominator="relevant_cases",
        )
    with pytest.raises(ValueError, match="recipe"):
        MechanismRecipeV1(
            recipe_id="arbitrary_python_recipe",
            input_field="features.atr_20_fraction",
            input_values=(Decimal("0.5"),),
        )
    with pytest.raises(ValueError, match="field"):
        MechanismPredicateV1(field="snapshot.anything", operator="is_present", value=None)
    with pytest.raises(ValueError, match="unique"):
        MechanismRecipeV1(
            recipe_id="evaluate_exit_atr20_fraction_v1",
            input_field="features.atr_20_fraction",
            input_values=(Decimal("0.2"), Decimal("0.20")),
        )


def test_conditional_decision_metric_requires_relevant_case_denominator() -> None:
    for denominator in ("control_cases", "all_cases"):
        with pytest.raises(ValueError, match="relevant_cases"):
            MechanismMetricSpecV1(
                metric_id="exit.decision_changed_count",
                unit="count",
                direction="increase",
                tolerance=Decimal("0"),
                denominator=denominator,
            )


def test_binding_requires_exact_parent_candidate_and_pre_measurement_boundary() -> None:
    spec = _spec()
    binding = _binding(spec)

    validate_mechanism_observation_binding_v1(
        spec,
        binding,
        expected_candidate_bytes_sha256="c" * 64,
    )
    with pytest.raises(ValueError, match="parent"):
        validate_mechanism_observation_binding_v1(
            spec,
            replace(binding, parent_revision_sha256="f" * 64),
            expected_candidate_bytes_sha256="c" * 64,
        )
    with pytest.raises(ValueError, match="candidate"):
        validate_mechanism_observation_binding_v1(
            spec,
            binding,
            expected_candidate_bytes_sha256="f" * 64,
        )
    with pytest.raises(ValueError, match="before authoring"):
        _spec(frozen_before_authoring=False)


def test_spec_hypothesis_binding_rejects_a_non_exit_hypothesis() -> None:
    spec = _spec()
    wrong_hypothesis = replace(_hypothesis(), primary_mechanism="entry")

    with pytest.raises(ValueError, match="primary mechanism"):
        validate_mechanism_spec_hypothesis_v1(
            spec,
            wrong_hypothesis,
            parent_revision_sha256=spec.parent_revision_sha256,
            round_intent_sha256=spec.round_intent_sha256,
        )


def test_spec_hypothesis_and_disconfirming_metrics_must_match_declared_meaning() -> None:
    spec = _spec()
    wrong_direction = replace(
        _hypothesis(),
        predicted_changes=(
            MetricPredictionV5(
                metric_id="exit.decision_changed_count",
                direction="decrease",
                rationale="Contradictory direction.",
            ),
        ),
    )
    with pytest.raises(ValueError, match="direction"):
        validate_mechanism_spec_hypothesis_v1(
            spec,
            wrong_direction,
            parent_revision_sha256=spec.parent_revision_sha256,
            round_intent_sha256=spec.round_intent_sha256,
        )
    with pytest.raises(ValueError, match="disconfirming"):
        replace(
            spec,
            metrics=spec.metrics[:2],
            disconfirming_observations=(
                MechanismDisconfirmingObservationV1(
                    observation_id="diagnostic_metric_unavailable",
                    metric_id="evaluator.exit_attribution_count",
                ),
            ),
        )


def test_assessment_keeps_zero_and_too_small_denominators_insufficient() -> None:
    metric = _spec().metrics[0]

    zero = MechanismPredictionResultV1.from_measurement(
        metric,
        parent_value=Decimal("0"),
        candidate_value=Decimal("0"),
        numerator=Decimal("0"),
        denominator=0,
        minimum_relevant_cases=2,
    )
    too_small = MechanismPredictionResultV1.from_measurement(
        metric,
        parent_value=Decimal("0"),
        candidate_value=Decimal("1"),
        numerator=Decimal("1"),
        denominator=1,
        minimum_relevant_cases=2,
    )

    assert zero.assessment == "insufficient_evidence"
    assert too_small.assessment == "insufficient_evidence"
    with pytest.raises(ValueError, match="denominator"):
        MechanismPredictionResultV1(
            metric_id=metric.metric_id,
            unit=metric.unit,
            direction=metric.direction,
            denominator_kind=metric.denominator,
            selector=metric.selector,
            minimum_relevant_cases=2,
            parent_value=Decimal("0"),
            candidate_value=Decimal("1"),
            numerator=Decimal("1"),
            denominator=0,
            tolerance=metric.tolerance,
            paired_delta=Decimal("1"),
            assessment="supported_on_cases",
        )


def test_unavailable_measurements_preserve_declared_rows_without_fabricated_zeroes() -> None:
    metric = _spec().metrics[2]

    unavailable = MechanismPredictionResultV1.unavailable(
        metric,
        minimum_relevant_cases=2,
        reason="evaluator_metric_missing",
    )

    assert unavailable.availability == "unavailable"
    assert unavailable.unavailable_reason == "evaluator_metric_missing"
    assert unavailable.parent_value is None
    assert unavailable.candidate_value is None
    assert unavailable.numerator is None
    assert unavailable.paired_delta is None
    assert unavailable.assessment == "insufficient_evidence"
    with pytest.raises(ValueError, match="integral"):
        replace(unavailable, tolerance=Decimal("0.5"))


def test_prediction_recomputes_direction_and_keeps_complete_metric_meaning() -> None:
    metric = _spec().metrics[0]

    with pytest.raises(ValueError, match="assessment"):
        MechanismPredictionResultV1(
            metric_id=metric.metric_id,
            unit=metric.unit,
            direction="decrease",
            denominator_kind=metric.denominator,
            selector=metric.selector,
            minimum_relevant_cases=2,
            parent_value=Decimal("0"),
            candidate_value=Decimal("1"),
            numerator=Decimal("1"),
            denominator=2,
            tolerance=metric.tolerance,
            paired_delta=Decimal("1"),
            assessment="supported_on_cases",
        )


def test_count_semantics_allow_signed_integral_delta_and_evaluator_overrun_only() -> None:
    metric = _spec().metrics[0]
    with pytest.raises(ValueError, match="integral"):
        MechanismPredictionResultV1.from_measurement(
            metric,
            parent_value=Decimal("0.5"),
            candidate_value=Decimal("1.5"),
            numerator=Decimal("1.5"),
            denominator=2,
            minimum_relevant_cases=2,
        )
    with pytest.raises(ValueError, match="denominator"):
        MechanismPredictionResultV1.from_measurement(
            metric,
            parent_value=Decimal("0"),
            candidate_value=Decimal("3"),
            numerator=Decimal("3"),
            denominator=2,
            minimum_relevant_cases=2,
        )
    evaluator = _spec().metrics[2]
    overrun = MechanismPredictionResultV1.from_measurement(
        evaluator,
        parent_value=Decimal("0"),
        candidate_value=Decimal("5"),
        numerator=Decimal("5"),
        denominator=2,
        minimum_relevant_cases=2,
    )
    assert overrun.assessment == "supported_on_cases"
    assert overrun.paired_delta == Decimal("5")


def test_predicate_rejects_invalid_observed_fraction_before_operator_handling() -> None:
    present = MechanismPredicateV1(field="features.atr_20_fraction", operator="is_present", value=None)
    with pytest.raises(ValueError, match="observed"):
        present.matches(float("nan"))
    with pytest.raises(ValueError, match="observed"):
        present.matches(Decimal("2"))


def test_numeric_and_canonical_payload_bounds_are_conservative() -> None:
    with pytest.raises(ValueError, match="numeric"):
        MechanismPredicateV1(
            field="features.atr_20_fraction",
            operator="gte",
            value=Decimal("1e-65"),
        )
    with pytest.raises(ValueError, match="canonical JSON"):
        MechanismPredicateV1.from_canonical_json(" " * (MECHANISM_MAX_CANONICAL_JSON_BYTES_V1 + 1))


def test_decimal_bounds_are_closed_under_fixed_canonical_encoding() -> None:
    with pytest.raises(ValueError, match="precision"):
        MechanismMetricSpecV1(
            metric_id="exit.decision_changed_count",
            unit="count",
            direction="increase",
            tolerance=Decimal("1E+64"),
            denominator="relevant_cases",
        )
    boundary = MechanismMetricSpecV1(
        metric_id="exit.decision_changed_count",
        unit="count",
        direction="increase",
        tolerance=Decimal("1E+63"),
        denominator="relevant_cases",
    )
    assert MechanismMetricSpecV1.from_canonical_json(boundary.to_canonical_json()) == boundary


def test_large_evaluator_count_delta_is_exact_and_context_stable() -> None:
    metric = _spec().metrics[2]
    with localcontext() as context:
        context.prec = 5
        low_precision = MechanismPredictionResultV1.from_measurement(
            metric,
            parent_value=Decimal("1"),
            candidate_value=Decimal("1E+30"),
            numerator=Decimal("1E+30"),
            denominator=2,
            minimum_relevant_cases=2,
        )
        low_precision_json = low_precision.to_canonical_json()
    with localcontext() as context:
        context.prec = 100
        high_precision = MechanismPredictionResultV1.from_measurement(
            metric,
            parent_value=Decimal("1"),
            candidate_value=Decimal("1E+30"),
            numerator=Decimal("1E+30"),
            denominator=2,
            minimum_relevant_cases=2,
        )

    expected_delta = Decimal("999999999999999999999999999999")
    assert low_precision.paired_delta == expected_delta
    assert high_precision.paired_delta == expected_delta
    assert low_precision.assessment == "supported_on_cases"
    assert low_precision_json == high_precision.to_canonical_json()


def test_report_separates_execution_coverage_and_assessment_and_projection_labels_criticism() -> None:
    spec = _spec()
    binding = _binding(spec)
    predictions = tuple(
        MechanismPredictionResultV1.unavailable(
            metric,
            minimum_relevant_cases=2,
            reason="evaluator_metric_missing",
        )
        if metric.metric_id == "evaluator.exit_attribution_count"
        else MechanismPredictionResultV1.from_measurement(
            metric,
            parent_value=(
                Decimal("2") if metric.metric_id == "exit.protected_control_unchanged_count" else Decimal("0")
            ),
            candidate_value=(
                Decimal("2") if metric.metric_id == "exit.protected_control_unchanged_count" else Decimal("1")
            ),
            numerator=(Decimal("2") if metric.metric_id == "exit.protected_control_unchanged_count" else Decimal("1")),
            denominator=2,
            minimum_relevant_cases=2,
        )
        for metric in spec.metrics
    )
    report = MechanismEvidenceReportV1(
        binding=binding,
        execution=MechanismExecutionV1(
            status="completed",
            reason="completed",
            resource_usage=MechanismResourceUsageV1(
                cases=2,
                repetitions=1,
                elapsed_ms=100,
                cpu_seconds=Decimal("1"),
                peak_memory_mib=64,
                output_bytes=1024,
            ),
        ),
        coverage=MechanismCoverageV1(
            total_cases=2,
            relevant_cases=2,
            decision_changed_cases=1,
            protected_control_cases=2,
            protected_control_unchanged_cases=2,
            unsupported_cases=0,
        ),
        predictions=predictions,
        limitations=("Branch coverage is unavailable.",),
    )

    projection = MechanismLearningProjectionV1.from_report(
        spec,
        report,
        critic_interpretation="Retrospective interpretation only.",
        critic_next_direction="Retrospective next direction only.",
    )
    assert report.execution.status == "completed"
    assert report.coverage.relevant_cases == 2
    assert projection.predictions == report.predictions
    assert projection.execution == report.execution
    assert projection.binding == report.binding
    assert projection.controls == spec.controls
    assert projection.controls[0].control_id == "protected_next_stop_price"
    assert projection.controls[0].observed_field == "next_stop_price"
    assert projection.critic_interpretation_is_retrospective is True
    assert projection.critic_interpretation not in {result.metric_id for result in projection.predictions}
    assert MechanismEvidenceReportV1.from_canonical_json(report.to_canonical_json()) == report
    round_tripped_projection = MechanismLearningProjectionV1.from_canonical_json(projection.to_canonical_json())
    assert round_tripped_projection == projection
    assert round_tripped_projection.controls[0].control_id == "protected_next_stop_price"
    assert round_tripped_projection.controls[0].observed_field == "next_stop_price"
    with pytest.raises(ValueError, match="declared mechanism metrics"):
        MechanismLearningProjectionV1.from_report(
            spec,
            replace(report, predictions=predictions[:-1]),
        )
    with pytest.raises(ValueError, match="execution"):
        MechanismEvidenceReportV1(
            binding=binding,
            execution=MechanismExecutionV1(status="failed", reason="timeout"),
            coverage=report.coverage,
            predictions=predictions,
            limitations=report.limitations,
        )

    unavailable = MechanismPredictionResultV1.unavailable(
        spec.metrics[2],
        minimum_relevant_cases=2,
        reason="evaluator_metric_missing",
    )
    unavailable_report = replace(report, predictions=(*predictions[:2], unavailable))
    unavailable_projection = MechanismLearningProjectionV1.from_report(spec, unavailable_report)
    assert unavailable_projection.predictions[-1].unavailable_reason == "evaluator_metric_missing"


def test_failed_execution_preserves_overrun_and_requires_unavailable_predictions() -> None:
    spec = _spec()
    binding = _binding(spec)
    unavailable = tuple(
        MechanismPredictionResultV1.unavailable(
            metric,
            minimum_relevant_cases=2,
            reason="execution_failed",
        )
        for metric in spec.metrics
    )
    report = MechanismEvidenceReportV1(
        binding=binding,
        execution=MechanismExecutionV1(
            status="failed",
            reason="resource_limit",
            resource_usage=MechanismResourceUsageV1(
                cases=2,
                repetitions=1,
                elapsed_ms=1500,
                cpu_seconds=Decimal("3"),
                peak_memory_mib=256,
                output_bytes=8192,
            ),
        ),
        coverage=MechanismCoverageV1(
            total_cases=2,
            relevant_cases=0,
            decision_changed_cases=0,
            protected_control_cases=0,
            protected_control_unchanged_cases=0,
            unsupported_cases=2,
        ),
        predictions=unavailable,
        limitations=("The worker exceeded its declared CPU limit.",),
    )
    projection = MechanismLearningProjectionV1.from_report(spec, report)
    assert projection.execution.reason == "resource_limit"


def test_resource_limits_and_schema_types_are_strict() -> None:
    with pytest.raises(ValueError, match="CPU"):
        MechanismResourceBudgetV1(
            max_cases=1,
            max_repetitions=1,
            timeout_ms=1000,
            cpu_seconds=Decimal("86401"),
            memory_mib=1,
            output_bytes=1,
        )
    with pytest.raises(ValueError, match="schema"):
        replace(_spec(), schema_version=True)
    with pytest.raises(ValueError, match="version"):
        MechanismRecipeV1(
            recipe_id="evaluate_exit_atr20_fraction_v1",
            input_field="features.atr_20_fraction",
            input_values=(Decimal("0.5"),),
            version=True,
        )


def test_projection_requires_digest_shaped_experiment_identity() -> None:
    spec = _spec()
    binding = _binding(spec)
    predictions = tuple(
        MechanismPredictionResultV1.unavailable(
            metric,
            minimum_relevant_cases=2,
            reason="evaluator_metric_missing",
        )
        if metric.metric_id == "evaluator.exit_attribution_count"
        else MechanismPredictionResultV1.from_measurement(
            metric,
            parent_value=Decimal("0"),
            candidate_value=Decimal("1"),
            numerator=Decimal("1"),
            denominator=2,
            minimum_relevant_cases=2,
        )
        for metric in spec.metrics
    )
    report = MechanismEvidenceReportV1(
        binding=binding,
        execution=MechanismExecutionV1(status="completed", reason="completed"),
        coverage=MechanismCoverageV1(
            total_cases=2,
            relevant_cases=2,
            decision_changed_cases=1,
            protected_control_cases=2,
            protected_control_unchanged_cases=2,
            unsupported_cases=0,
        ),
        predictions=predictions,
        limitations=(),
    )
    projection = MechanismLearningProjectionV1.from_report(spec, report)
    with pytest.raises(ValueError, match="digest"):
        replace(projection, experiment_id="v5.exp.exit.atr")


def test_coverage_rejects_overlapping_unsupported_cases() -> None:
    controls_outside_applicability = MechanismCoverageV1(
        total_cases=3,
        relevant_cases=1,
        decision_changed_cases=1,
        protected_control_cases=2,
        protected_control_unchanged_cases=2,
        unsupported_cases=0,
    )
    assert controls_outside_applicability.protected_control_cases == 2
    with pytest.raises(ValueError, match="supported cases"):
        MechanismCoverageV1(
            total_cases=3,
            relevant_cases=1,
            decision_changed_cases=1,
            protected_control_cases=3,
            protected_control_unchanged_cases=0,
            unsupported_cases=1,
        )
    with pytest.raises(ValueError, match="coverage cases"):
        MechanismCoverageV1(
            total_cases=2,
            relevant_cases=2,
            decision_changed_cases=0,
            protected_control_cases=0,
            protected_control_unchanged_cases=0,
            unsupported_cases=1,
        )


def test_retrospective_measurement_cannot_be_marked_precommitted() -> None:
    metric = _spec().metrics[0]
    with pytest.raises(ValueError, match="retrospective"):
        MechanismPredictionResultV1(
            metric_id=metric.metric_id,
            unit=metric.unit,
            direction=metric.direction,
            denominator_kind=metric.denominator,
            selector=metric.selector,
            minimum_relevant_cases=2,
            parent_value=Decimal("0"),
            candidate_value=Decimal("1"),
            numerator=Decimal("1"),
            denominator=2,
            tolerance=metric.tolerance,
            paired_delta=Decimal("1"),
            assessment="supported_on_cases",
            evidence_origin="retrospective",
        )


def test_observation_corpus_converts_decimal_inputs_and_binds_applicability_order() -> None:
    spec = _observation_spec()
    corpus = build_mechanism_observation_corpus_v1(spec, seed_snapshot=_exit_seed())

    assert isinstance(corpus, MechanismObservationCorpusV1)
    assert tuple(case.converted_value for case in corpus.cases) == (0.4, 0.5, 0.6, None)
    assert tuple(case.applicable for case in corpus.cases) == (False, True, True, False)
    assert tuple(case.order for case in corpus.cases) == (0, 1, 2, 3)
    assert all(case.input_canonical_bytes for case in corpus.cases)
    assert len({case.input_identity_sha256 for case in corpus.cases}) == 4
    assert len({case.snapshot_sha256 for case in corpus.cases}) == 4
    assert corpus.cases[-1].converted_value is None


def test_observation_corpus_rejects_decimal_values_that_alias_after_float_conversion() -> None:
    spec = _observation_spec(values=(Decimal("0.2"), Decimal.from_float(0.2)))

    with pytest.raises(ValueError, match="converted float"):
        build_mechanism_observation_corpus_v1(spec, seed_snapshot=_exit_seed())


def test_bounded_fixture_observations_are_paired_and_count_unique_relevant_cases() -> None:
    spec = _observation_spec()
    corpus = build_mechanism_observation_corpus_v1(spec, seed_snapshot=_exit_seed())
    binding = _observation_binding(spec, corpus)
    changed = tuple(case.input_identity_sha256 for case in (corpus.cases[1], corpus.cases[3]))
    parent, candidate = _fixture_workers(binding, corpus, changed_inputs=changed)

    run = collect_mechanism_observations_v1(
        spec,
        binding,
        corpus,
        parent_worker=parent,
        candidate_worker=candidate,
        repetitions=2,
    )

    assert isinstance(run, MechanismObservationRunV1)
    assert run.execution.status == "completed"
    assert run.execution.reason == "completed"
    assert run.coverage.total_cases == 4
    assert run.coverage.relevant_cases == 2
    assert run.coverage.decision_changed_cases == 1
    assert run.coverage.protected_control_cases == 4
    assert run.coverage.protected_control_unchanged_cases == 2
    assert len(run.observations) == 8
    assert run.repetitions == 2
    assert run.reset_semantics == "reset_per_case"
    assert run.decision_changed_count == 1
    assert run.protected_control_unchanged_count == 2
    assert parent.closed and candidate.closed
    assert parent.reset_count == 8 and candidate.reset_count == 8


def test_observation_binding_rejects_worker_with_foreign_corpus_before_opening() -> None:
    spec = _observation_spec()
    corpus = build_mechanism_observation_corpus_v1(spec, seed_snapshot=_exit_seed())
    binding = _observation_binding(spec, corpus)
    parent, candidate = _fixture_workers(binding, corpus)
    candidate.registration = replace(candidate.registration, corpus_sha256="f" * 64)

    with pytest.raises(ValueError, match="corpus"):
        collect_mechanism_observations_v1(
            spec,
            binding,
            corpus,
            parent_worker=parent,
            candidate_worker=candidate,
        )

    assert not parent.opened and not candidate.opened

    parent, candidate = _fixture_workers(binding, corpus)
    parent.registration = replace(parent.registration, parent_revision_sha256="f" * 64)
    with pytest.raises(ValueError, match="parent"):
        collect_mechanism_observations_v1(
            spec,
            binding,
            corpus,
            parent_worker=parent,
            candidate_worker=candidate,
        )
    assert not parent.opened and not candidate.opened


def test_nonfixture_worker_fails_closed_without_sandbox_enforcement() -> None:
    spec = _observation_spec()
    corpus = build_mechanism_observation_corpus_v1(spec, seed_snapshot=_exit_seed())
    binding = _observation_binding(spec, corpus)
    parent, candidate = _fixture_workers(binding, corpus)
    parent.registration = replace(
        parent.registration,
        execution_kind="registered_sandbox",
        cpu_memory_enforced=False,
    )
    candidate.registration = replace(
        candidate.registration,
        execution_kind="registered_sandbox",
        cpu_memory_enforced=False,
    )

    run = collect_mechanism_observations_v1(
        spec,
        binding,
        corpus,
        parent_worker=parent,
        candidate_worker=candidate,
    )

    assert run.execution.status == "not_run"
    assert run.execution.reason == "worker_unavailable"
    assert run.observations == ()
    assert not parent.opened and not candidate.opened
    assert any("sandbox" in limitation.lower() for limitation in run.limitations)


def test_resource_case_bound_is_execution_failure_before_worker_open() -> None:
    spec = _observation_spec()
    corpus = build_mechanism_observation_corpus_v1(spec, seed_snapshot=_exit_seed())
    budget = MechanismResourceBudgetV1(
        max_cases=2,
        max_repetitions=2,
        timeout_ms=1000,
        cpu_seconds=Decimal("2"),
        memory_mib=128,
        output_bytes=64 * 1024,
    )
    binding = _observation_binding(spec, corpus, budget=budget)
    parent, candidate = _fixture_workers(binding, corpus)

    run = collect_mechanism_observations_v1(
        spec,
        binding,
        corpus,
        parent_worker=parent,
        candidate_worker=candidate,
    )

    assert run.execution.status == "failed"
    assert run.execution.reason == "resource_limit"
    assert run.observations == ()
    assert not parent.opened and not candidate.opened

    small_repetition_budget = replace(budget, max_cases=16, max_repetitions=1)
    binding = _observation_binding(spec, corpus, budget=small_repetition_budget)
    parent, candidate = _fixture_workers(binding, corpus)
    run = collect_mechanism_observations_v1(
        spec,
        binding,
        corpus,
        parent_worker=parent,
        candidate_worker=candidate,
        repetitions=2,
    )
    assert run.execution.status == "failed"
    assert run.execution.reason == "resource_limit"
    assert run.repetitions == 2
    assert not parent.opened and not candidate.opened


def test_protocol_timeout_and_cleanup_failures_are_execution_failures() -> None:
    spec = _observation_spec(values=(Decimal("0.50"),))
    corpus = build_mechanism_observation_corpus_v1(spec, seed_snapshot=_exit_seed())
    binding = _observation_binding(spec, corpus)
    parent, candidate = _fixture_workers(binding, corpus)

    parent.decisions_by_input_identity = {corpus.cases[0].input_identity_sha256: object()}
    protocol_run = collect_mechanism_observations_v1(
        spec,
        binding,
        corpus,
        parent_worker=parent,
        candidate_worker=candidate,
    )
    assert protocol_run.execution.status == "failed"
    assert protocol_run.execution.reason == "protocol_failure"
    assert protocol_run.coverage.decision_changed_cases == 0
    assert protocol_run.observations == ()
    assert parent.closed and candidate.closed

    parent, candidate = _fixture_workers(binding, corpus)
    parent.failure = TimeoutError("fixture timeout")
    timeout_run = collect_mechanism_observations_v1(
        spec,
        binding,
        corpus,
        parent_worker=parent,
        candidate_worker=candidate,
    )
    assert timeout_run.execution.status == "failed"
    assert timeout_run.execution.reason == "timeout"
    assert timeout_run.coverage.decision_changed_cases == 0
    assert parent.closed and candidate.closed

    parent, candidate = _fixture_workers(binding, corpus)
    parent.cleanup_failure = RuntimeError("close failed")
    cleanup_run = collect_mechanism_observations_v1(
        spec,
        binding,
        corpus,
        parent_worker=parent,
        candidate_worker=candidate,
    )
    assert cleanup_run.execution.status == "failed"
    assert cleanup_run.execution.reason == "execution_failed"
    assert any("cleanup" in limitation.lower() for limitation in cleanup_run.limitations)


def test_repeated_nondeterminism_is_execution_failure_without_contradicting_evidence() -> None:
    class _FlippingFixtureWorker(SyntheticFixtureWorkerV1):
        def evaluate(self, session: object, case: MechanismPairedCaseV1, *, deadline_monotonic: float | None) -> object:
            decision = super().evaluate(session, case, deadline_monotonic=deadline_monotonic)
            if self.call_count > 1:
                from core.strategy_policy.contracts import ExitDecision

                return ExitDecision(
                    actions=decision.actions,
                    next_stop_price=case.snapshot.base.protective_stop_candidates[-1],
                    early_winner_hold=decision.early_winner_hold,
                    scale_out_tier=decision.scale_out_tier,
                    breakeven_armed=decision.breakeven_armed,
                    ema_trailing_active=decision.ema_trailing_active,
                )
            return decision

    spec = _observation_spec(values=(Decimal("0.50"),))
    corpus = build_mechanism_observation_corpus_v1(spec, seed_snapshot=_exit_seed())
    binding = _observation_binding(spec, corpus)
    parent, candidate = _fixture_workers(binding, corpus)
    candidate = _FlippingFixtureWorker(
        registration=candidate.registration,
        decisions_by_input_identity=candidate.decisions_by_input_identity,
    )

    run = collect_mechanism_observations_v1(
        spec,
        binding,
        corpus,
        parent_worker=parent,
        candidate_worker=candidate,
        repetitions=2,
    )

    assert run.execution.status == "failed"
    assert run.execution.reason == "execution_failed"
    assert any("nondeterministic" in limitation for limitation in run.limitations)
    assert run.coverage.decision_changed_cases == 0


def test_deadline_is_caller_owned_and_does_not_retry_worker_calls() -> None:
    spec = _observation_spec(values=(Decimal("0.50"),))
    corpus = build_mechanism_observation_corpus_v1(spec, seed_snapshot=_exit_seed())
    binding = _observation_binding(spec, corpus)
    parent, candidate = _fixture_workers(binding, corpus)

    run = collect_mechanism_observations_v1(
        spec,
        binding,
        corpus,
        parent_worker=parent,
        candidate_worker=candidate,
        deadline_monotonic=0.0,
    )

    assert run.execution.status == "failed"
    assert run.execution.reason == "timeout"
    assert parent.call_count == 0 and candidate.call_count == 0
    assert not parent.opened and not candidate.opened


def test_paired_observation_preserves_canonical_decision_bytes_and_case_identity() -> None:
    spec = _observation_spec(values=(Decimal("0.50"),))
    corpus = build_mechanism_observation_corpus_v1(spec, seed_snapshot=_exit_seed())
    binding = _observation_binding(spec, corpus)
    parent, candidate = _fixture_workers(binding, corpus)

    run = collect_mechanism_observations_v1(
        spec,
        binding,
        corpus,
        parent_worker=parent,
        candidate_worker=candidate,
    )
    observation = run.observations[0]

    assert isinstance(observation, MechanismPairedObservationV1)
    assert observation.input_identity_sha256 == corpus.cases[0].input_identity_sha256
    assert observation.parent_decision_json == observation.parent_decision_json.rstrip()
    assert observation.candidate_decision_json == observation.candidate_decision_json.rstrip()
    assert observation.parent_decision_sha256 != ""
    assert observation.candidate_decision_sha256 != ""
    assert observation.case_snapshot_json == corpus.cases[0].snapshot_canonical_json
    assert isinstance(corpus.cases[0], MechanismPairedCaseV1)


def test_spec_bound_corpus_validation_rejects_rebound_applicability_before_open() -> None:
    spec = _observation_spec()
    corpus = build_mechanism_observation_corpus_v1(spec, seed_snapshot=_exit_seed())
    forged_case = replace(corpus.cases[0], applicable=True)
    forged_corpus = MechanismObservationCorpusV1(
        recipe_sha256=corpus.recipe_sha256,
        cases=(forged_case, *corpus.cases[1:]),
    )
    binding = _observation_binding(spec, forged_corpus)
    parent, candidate = _fixture_workers(binding, forged_corpus)

    with pytest.raises(ValueError, match="applicability"):
        validate_mechanism_observation_corpus_v1(spec, binding, forged_corpus)
    with pytest.raises(ValueError, match="applicability"):
        collect_mechanism_observations_v1(
            spec,
            binding,
            forged_corpus,
            parent_worker=parent,
            candidate_worker=candidate,
        )
    assert not parent.opened and not candidate.opened


def test_spec_bound_corpus_validation_rejects_numeric_equal_recipe_representation() -> None:
    spec = _observation_spec()
    corpus = build_mechanism_observation_corpus_v1(spec, seed_snapshot=_exit_seed())
    original_case = corpus.cases[1]
    forged_input = Decimal("0.5")
    forged_input_bytes = canonical_probe_json_v5(forged_input)
    forged_case = replace(
        original_case,
        input_value=forged_input,
        input_canonical_bytes=forged_input_bytes,
        input_identity_sha256=hashlib.sha256(forged_input_bytes).hexdigest(),
    )
    forged_corpus = MechanismObservationCorpusV1(
        recipe_sha256=corpus.recipe_sha256,
        cases=(corpus.cases[0], forged_case, *corpus.cases[2:]),
    )
    forged_binding = _observation_binding(spec, forged_corpus)
    parent, candidate = _fixture_workers(forged_binding, forged_corpus)

    with pytest.raises(ValueError, match="canonical|recipe"):
        validate_mechanism_observation_corpus_v1(spec, forged_binding, forged_corpus)
    with pytest.raises(ValueError, match="canonical|recipe"):
        collect_mechanism_observations_v1(
            spec,
            forged_binding,
            forged_corpus,
            parent_worker=parent,
            candidate_worker=candidate,
        )
    assert not parent.opened and not candidate.opened

    original_binding = _observation_binding(spec, corpus)
    parent, candidate = _fixture_workers(original_binding, corpus)
    run = collect_mechanism_observations_v1(
        spec,
        original_binding,
        corpus,
        parent_worker=parent,
        candidate_worker=candidate,
    )
    assert run.execution.status == "completed"


def test_run_requires_exact_repetition_case_prefix_and_product() -> None:
    spec = _observation_spec(values=(Decimal("0.50"), Decimal("0.60")))
    corpus = build_mechanism_observation_corpus_v1(spec, seed_snapshot=_exit_seed())
    binding = _observation_binding(spec, corpus)
    parent, candidate = _fixture_workers(binding, corpus)
    run = collect_mechanism_observations_v1(
        spec,
        binding,
        corpus,
        parent_worker=parent,
        candidate_worker=candidate,
        repetitions=2,
    )

    shifted = replace(run.observations[1], repetition=2)
    with pytest.raises(ValueError, match="repetition|lattice"):
        replace(run, observations=(run.observations[0], shifted, *run.observations[2:]))

    failed = replace(run, execution=MechanismExecutionV1(status="failed", reason="execution_failed"))
    with pytest.raises(ValueError, match="prefix|lattice"):
        replace(failed, observations=(run.observations[0], run.observations[1], run.observations[3]))


def test_run_constructor_rejects_forged_repeated_decision_bytes() -> None:
    from core.strategy_policy.contracts import ExitDecision

    spec = _observation_spec(values=(Decimal("0.50"),))
    corpus = build_mechanism_observation_corpus_v1(spec, seed_snapshot=_exit_seed())
    binding = _observation_binding(spec, corpus)
    parent, candidate = _fixture_workers(binding, corpus)
    run = collect_mechanism_observations_v1(
        spec,
        binding,
        corpus,
        parent_worker=parent,
        candidate_worker=candidate,
        repetitions=2,
    )
    original = ExitDecision.from_canonical_json(run.observations[1].candidate_decision_json.decode("utf-8"))
    changed = replace(original, next_stop_price=corpus.cases[0].snapshot.base.protective_stop_candidates[-1])
    changed_json = changed.to_canonical_json().encode("utf-8")
    forged_observation = replace(
        run.observations[1],
        candidate_decision_json=changed_json,
        candidate_decision_sha256=hashlib.sha256(changed_json).hexdigest(),
        decision_changed=True,
        protected_control_unchanged=False,
    )

    with pytest.raises(ValueError, match="nondetermin|repeat"):
        replace(run, observations=(run.observations[0], forged_observation))


def test_not_run_has_no_observed_coverage_or_observations() -> None:
    spec = _observation_spec()
    corpus = build_mechanism_observation_corpus_v1(spec, seed_snapshot=_exit_seed())
    binding = _observation_binding(spec, corpus)
    parent, candidate = _fixture_workers(binding, corpus)
    parent.registration = replace(parent.registration, execution_kind="registered_sandbox")
    candidate.registration = replace(candidate.registration, execution_kind="registered_sandbox")
    run = collect_mechanism_observations_v1(
        spec,
        binding,
        corpus,
        parent_worker=parent,
        candidate_worker=candidate,
    )

    assert run.execution.status == "not_run"
    assert run.observations == ()
    assert run.coverage == MechanismCoverageV1(
        total_cases=0,
        relevant_cases=0,
        decision_changed_cases=0,
        protected_control_cases=0,
        protected_control_unchanged_cases=0,
        unsupported_cases=0,
        branch_coverage="unavailable",
    )
    assert run.relevant_case_count == 0
    assert run.decision_changed_count == 0
    assert run.protected_control_unchanged_count == 0

    parent, candidate = _fixture_workers(binding, corpus)
    completed = collect_mechanism_observations_v1(
        spec,
        binding,
        corpus,
        parent_worker=parent,
        candidate_worker=candidate,
    )
    with pytest.raises(ValueError, match="not_run"):
        replace(
            completed,
            execution=MechanismExecutionV1(status="not_run", reason="worker_unavailable"),
        )


def _evaluator_report(*, ma_violation: int, closed_trades: int) -> EvaluationReportV5:
    zero = Decimal("0")
    return EvaluationReportV5(
        portfolio_annualized_return_pct=zero,
        portfolio_total_return_pct=zero,
        gross_annualized_return_pct=zero,
        benchmark_annualized_return_pct=zero,
        benchmark_total_return_pct=zero,
        max_drawdown_pct=zero,
        sharpe_ratio=zero,
        closed_trades=closed_trades,
        average_exposure_pct=zero,
        average_cash_pct=zero,
        turnover_pct=zero,
        total_friction_usd=zero,
        friction_drag_pct=zero,
        win_rate_pct=None,
        average_win_pct=None,
        average_loss_pct=None,
        payoff_ratio=None,
        expectancy_pct=None,
        median_holding_sessions=None,
        invested_sleeve_annualized_return_pct=None,
        estimated_idle_cash_drag_pct=zero,
        stop_gap_shortfall_usd=zero,
        identity_transition_count=0,
        scale_out_opportunity_cost_pct=None,
        maximum_favorable_excursion_pct=None,
        maximum_adverse_excursion_pct=None,
        entry_funnel=(),
        exit_attribution=(MetricCountV5(metric_id="ma_violation", count=ma_violation),),
        policy_intent_outcomes=(),
        regime_slices=(),
        episode_slices=(),
        calendar_year_slices=(),
        rolling_returns=(),
    )


def _panel_evaluation(
    *,
    policy_identity_sha256: str,
    panel_sha256: str,
    ma_violation: int,
    closed_trades: int,
    evaluator_contract_sha256: str = "e" * 64,
) -> PanelEvaluationV5:
    return PanelEvaluationV5(
        evaluator_contract_sha256=evaluator_contract_sha256,
        sandbox_profile_sha256="d" * 64,
        panel_sha256=panel_sha256,
        policy_identity_sha256=policy_identity_sha256,
        start_date="2026-01-01",
        end_date="2026-01-03",
        elapsed_calendar_days=2,
        selection_scenario_id="base",
        scenarios=(
            ScenarioPanelEvaluationV5(
                scenario_id="base",
                starting_equity=Decimal("100"),
                ending_equity=Decimal("100"),
                report=_evaluator_report(ma_violation=ma_violation, closed_trades=closed_trades),
            ),
        ),
    )


def _episode_evaluation(
    *,
    policy_identity_sha256: str,
    panel_sha256: str,
    episode_id: str,
    episode_ordinal: int,
    ma_violation: int,
    closed_trades: int,
    evaluator_contract_sha256: str = "e" * 64,
) -> EpisodeEvaluationV5:
    panel = _panel_evaluation(
        policy_identity_sha256=policy_identity_sha256,
        panel_sha256=panel_sha256,
        ma_violation=ma_violation,
        closed_trades=closed_trades,
        evaluator_contract_sha256=evaluator_contract_sha256,
    )
    return EpisodeEvaluationV5(
        episode_id=episode_id,
        episode_ordinal=episode_ordinal,
        start_date=panel.start_date,
        end_date=panel.end_date,
        evaluation=panel,
    )


def test_reducer_emits_every_declared_metric_and_retrieves_named_evaluator_metric() -> None:
    spec = _observation_spec()
    corpus = build_mechanism_observation_corpus_v1(spec, seed_snapshot=_exit_seed())
    binding = _observation_binding(spec, corpus)
    parent_worker, candidate_worker = _fixture_workers(
        binding,
        corpus,
        changed_inputs=(corpus.cases[1].input_identity_sha256,),
    )
    run = collect_mechanism_observations_v1(
        spec,
        binding,
        corpus,
        parent_worker=parent_worker,
        candidate_worker=candidate_worker,
    )

    report = build_mechanism_evidence_report_v1(spec, binding, run)
    by_metric = {item.metric_id: item for item in report.predictions}

    assert set(by_metric) == {item.metric_id for item in spec.metrics}
    assert by_metric["exit.decision_changed_count"].denominator == 2
    assert by_metric["exit.decision_changed_count"].candidate_value == Decimal("1")
    assert by_metric["exit.decision_changed_count"].paired_delta == Decimal("1")
    assert by_metric["exit.protected_control_unchanged_count"].denominator == 2
    assert by_metric["exit.protected_control_unchanged_count"].candidate_value == Decimal("1")
    assert by_metric["evaluator.exit_attribution_count"].availability == "unavailable"
    assert by_metric["evaluator.exit_attribution_count"].unavailable_reason == "evaluator_metric_missing"
    assert report.consequence_contexts == ()


def test_reducer_keeps_zero_applicability_insufficient_while_counting_valid_controls() -> None:
    base = _observation_spec()
    spec = replace(
        base,
        applicability=MechanismPredicateV1(
            field="features.atr_20_fraction",
            operator="gte",
            value=Decimal("1"),
        ),
        metrics=(
            base.metrics[0],
            replace(base.metrics[1], denominator="control_cases"),
            base.metrics[2],
        ),
    )
    corpus = build_mechanism_observation_corpus_v1(spec, seed_snapshot=_exit_seed())
    binding = _observation_binding(spec, corpus)
    parent_worker, candidate_worker = _fixture_workers(binding, corpus)
    run = collect_mechanism_observations_v1(
        spec,
        binding,
        corpus,
        parent_worker=parent_worker,
        candidate_worker=candidate_worker,
    )
    report = build_mechanism_evidence_report_v1(spec, binding, run)
    by_metric = {item.metric_id: item for item in report.predictions}

    assert report.coverage.relevant_cases == 0
    assert by_metric["exit.decision_changed_count"].denominator == 0
    assert by_metric["exit.decision_changed_count"].assessment == "insufficient_evidence"
    assert by_metric["exit.protected_control_unchanged_count"].denominator == 4
    assert by_metric["exit.protected_control_unchanged_count"].assessment == "supported_on_cases"
    assert by_metric["exit.protected_control_unchanged_count"].candidate_value == Decimal("4")


def test_reducer_counts_protected_controls_on_applicable_and_negative_cases() -> None:
    base = _observation_spec()
    spec = replace(
        base,
        metrics=(
            base.metrics[0],
            replace(base.metrics[1], denominator="control_cases"),
            base.metrics[2],
        ),
    )
    corpus = build_mechanism_observation_corpus_v1(spec, seed_snapshot=_exit_seed())
    binding = _observation_binding(spec, corpus)
    parent_worker, candidate_worker = _fixture_workers(
        binding,
        corpus,
        changed_inputs=(corpus.cases[1].input_identity_sha256,),
    )
    run = collect_mechanism_observations_v1(
        spec,
        binding,
        corpus,
        parent_worker=parent_worker,
        candidate_worker=candidate_worker,
    )

    report = build_mechanism_evidence_report_v1(spec, binding, run)
    protected = next(item for item in report.predictions if item.metric_id == "exit.protected_control_unchanged_count")
    assert protected.denominator == 4
    assert protected.parent_value == Decimal("4")
    assert protected.candidate_value == Decimal("3")
    assert protected.assessment == "contradicted_on_cases"


def test_matched_episode_context_binds_exact_parent_candidate_and_direct_selector() -> None:
    spec, binding, run, parent_bundle, parent_revision, candidate_bundle, candidate_revision = _identity_run()
    parent = _episode_evaluation(
        policy_identity_sha256=parent_revision.sha256,
        panel_sha256="f" * 64,
        episode_id="episode-3",
        episode_ordinal=3,
        ma_violation=1,
        closed_trades=2,
    )
    candidate = _episode_evaluation(
        policy_identity_sha256=candidate_revision.sha256,
        panel_sha256="f" * 64,
        episode_id="episode-3",
        episode_ordinal=3,
        ma_violation=3,
        closed_trades=2,
    )
    match = MechanismEvaluatorMatchV1(
        stage="discovery",
        episode_id="episode-3",
        episode_ordinal=3,
        scenario_id="base",
        parent=parent,
        candidate=candidate,
        parent_revision=parent_revision,
        parent_source_bundle=parent_bundle,
        candidate_revision=candidate_revision,
        candidate_source_bundle=candidate_bundle,
    )

    report = build_mechanism_evidence_report_v1(
        spec,
        binding,
        run,
        matched_evaluations=(match,),
    )
    evaluator = next(item for item in report.predictions if item.metric_id == "evaluator.exit_attribution_count")
    assert evaluator.availability == "measured"
    assert evaluator.parent_value == Decimal("1")
    assert evaluator.candidate_value == Decimal("3")
    assert evaluator.denominator == 1
    assert evaluator.assessment == "insufficient_evidence"
    assert evaluator.paired_delta == Decimal("2")
    assert len(report.consequence_contexts) == 1
    context = report.consequence_contexts[0].context
    assert isinstance(context, MechanismEvaluatorContextV1)
    assert context.parent_report_sha256 != context.candidate_report_sha256
    assert context.parent_policy_identity_sha256 == parent_revision.sha256
    assert context.candidate_policy_identity_sha256 == candidate_revision.sha256
    assert context.parent_source_bundle_sha256 == parent_bundle.sha256
    assert context.candidate_source_bundle_sha256 == candidate_bundle.sha256 == binding.candidate_bytes_sha256
    assert context.candidate_policy_identity_sha256 != context.candidate_source_bundle_sha256
    assert report.consequence_contexts[0].predictions[0].paired_delta == Decimal("2")
    assert MechanismEvidenceReportV1.from_canonical_json(report.to_canonical_json()) == report
    projection = MechanismLearningProjectionV1.from_report(spec, report)
    assert projection.consequence_contexts == report.consequence_contexts
    assert MechanismLearningProjectionV1.from_canonical_json(projection.to_canonical_json()) == projection


def test_evaluator_denominator_counts_matched_reports_even_when_report_has_many_exits() -> None:
    spec, binding, run, parent_bundle, parent_revision, candidate_bundle, candidate_revision = _identity_run()
    match = MechanismEvaluatorMatchV1(
        stage="discovery",
        episode_id="episode-many-exits",
        episode_ordinal=4,
        scenario_id="base",
        parent=_episode_evaluation(
            policy_identity_sha256=parent_revision.sha256,
            panel_sha256="7" * 64,
            episode_id="episode-many-exits",
            episode_ordinal=4,
            ma_violation=11,
            closed_trades=99,
        ),
        candidate=_episode_evaluation(
            policy_identity_sha256=candidate_revision.sha256,
            panel_sha256="7" * 64,
            episode_id="episode-many-exits",
            episode_ordinal=4,
            ma_violation=13,
            closed_trades=101,
        ),
        parent_revision=parent_revision,
        parent_source_bundle=parent_bundle,
        candidate_revision=candidate_revision,
        candidate_source_bundle=candidate_bundle,
    )

    report = build_mechanism_evidence_report_v1(spec, binding, run, matched_evaluations=(match,))
    evaluator = next(item for item in report.predictions if item.metric_id == "evaluator.exit_attribution_count")
    context_prediction = report.consequence_contexts[0].predictions[0]
    assert evaluator.denominator == 1
    assert evaluator.assessment == "insufficient_evidence"
    assert context_prediction.denominator == 1
    assert context_prediction.parent_value == Decimal("11")
    assert context_prediction.candidate_value == Decimal("13")


def test_evaluator_aggregate_uses_context_count_without_promoting_context_insufficiency_to_mixed() -> None:
    spec, binding, run, parent_bundle, parent_revision, candidate_bundle, candidate_revision = _identity_run()

    def make_match(ordinal: int, panel_sha256: str) -> MechanismEvaluatorMatchV1:
        return MechanismEvaluatorMatchV1(
            stage="discovery",
            episode_id=f"episode-aggregate-{ordinal}",
            episode_ordinal=ordinal,
            scenario_id="base",
            parent=_episode_evaluation(
                policy_identity_sha256=parent_revision.sha256,
                panel_sha256=panel_sha256,
                episode_id=f"episode-aggregate-{ordinal}",
                episode_ordinal=ordinal,
                ma_violation=ordinal,
                closed_trades=50,
            ),
            candidate=_episode_evaluation(
                policy_identity_sha256=candidate_revision.sha256,
                panel_sha256=panel_sha256,
                episode_id=f"episode-aggregate-{ordinal}",
                episode_ordinal=ordinal,
                ma_violation=ordinal + 1,
                closed_trades=75,
            ),
            parent_revision=parent_revision,
            parent_source_bundle=parent_bundle,
            candidate_revision=candidate_revision,
            candidate_source_bundle=candidate_bundle,
        )

    report = build_mechanism_evidence_report_v1(
        spec,
        binding,
        run,
        matched_evaluations=(make_match(1, "a" * 64), make_match(2, "b" * 64)),
    )
    evaluator = next(item for item in report.predictions if item.metric_id == "evaluator.exit_attribution_count")
    assert evaluator.availability == "measured"
    assert evaluator.denominator == 2
    assert evaluator.assessment == "supported_on_cases"
    assert tuple(group.predictions[0].assessment for group in report.consequence_contexts) == (
        "insufficient_evidence",
        "insufficient_evidence",
    )


def test_reducer_rejects_semantic_duplicate_contexts_even_with_different_reports() -> None:
    spec, binding, run, parent_bundle, parent_revision, candidate_bundle, candidate_revision = _identity_run()

    def make_match(parent_count: int, candidate_count: int) -> MechanismEvaluatorMatchV1:
        return MechanismEvaluatorMatchV1(
            stage="discovery",
            episode_id="episode-duplicate",
            episode_ordinal=6,
            scenario_id="base",
            parent=_episode_evaluation(
                policy_identity_sha256=parent_revision.sha256,
                panel_sha256="6" * 64,
                episode_id="episode-duplicate",
                episode_ordinal=6,
                ma_violation=parent_count,
                closed_trades=10,
            ),
            candidate=_episode_evaluation(
                policy_identity_sha256=candidate_revision.sha256,
                panel_sha256="6" * 64,
                episode_id="episode-duplicate",
                episode_ordinal=6,
                ma_violation=candidate_count,
                closed_trades=10,
            ),
            parent_revision=parent_revision,
            parent_source_bundle=parent_bundle,
            candidate_revision=candidate_revision,
            candidate_source_bundle=candidate_bundle,
        )

    with pytest.raises(ValueError, match="duplicate|semantic"):
        build_mechanism_evidence_report_v1(
            spec,
            binding,
            run,
            matched_evaluations=(make_match(1, 3), make_match(2, 5)),
        )


def test_report_validator_recomputes_evaluator_aggregate_and_requires_retained_contexts() -> None:
    spec, binding, run, parent_bundle, parent_revision, candidate_bundle, candidate_revision = _identity_run()
    report = build_mechanism_evidence_report_v1(spec, binding, run)
    evaluator_metric = spec.metrics[2]
    forged_result = MechanismPredictionResultV1.from_measurement(
        evaluator_metric,
        parent_value=Decimal("0"),
        candidate_value=Decimal("1"),
        numerator=Decimal("1"),
        denominator=1,
        minimum_relevant_cases=spec.minimum_relevant_cases,
    )
    forged = replace(report, predictions=(*report.predictions[:2], forged_result))
    with pytest.raises(ValueError, match="aggregate|context|evaluator"):
        validate_mechanism_report_against_spec_v1(spec, forged)

    match = MechanismEvaluatorMatchV1(
        stage="discovery",
        episode_id="episode-forged-aggregate",
        episode_ordinal=7,
        scenario_id="base",
        parent=_episode_evaluation(
            policy_identity_sha256=parent_revision.sha256,
            panel_sha256="7" * 64,
            episode_id="episode-forged-aggregate",
            episode_ordinal=7,
            ma_violation=1,
            closed_trades=3,
        ),
        candidate=_episode_evaluation(
            policy_identity_sha256=candidate_revision.sha256,
            panel_sha256="7" * 64,
            episode_id="episode-forged-aggregate",
            episode_ordinal=7,
            ma_violation=3,
            closed_trades=3,
        ),
        parent_revision=parent_revision,
        parent_source_bundle=parent_bundle,
        candidate_revision=candidate_revision,
        candidate_source_bundle=candidate_bundle,
    )
    measured = build_mechanism_evidence_report_v1(spec, binding, run, matched_evaluations=(match,))
    forged_measured = MechanismPredictionResultV1.from_measurement(
        evaluator_metric,
        parent_value=Decimal("1"),
        candidate_value=Decimal("4"),
        numerator=Decimal("4"),
        denominator=1,
        minimum_relevant_cases=spec.minimum_relevant_cases,
    )
    with pytest.raises(ValueError, match="aggregate|context|evaluator"):
        validate_mechanism_report_against_spec_v1(
            spec,
            replace(measured, predictions=(*measured.predictions[:2], forged_measured)),
        )


def test_noncompleted_reduction_keeps_execution_unavailable_reason() -> None:
    spec, binding, run, *_ = _identity_run()
    failed_run = replace(run, execution=MechanismExecutionV1(status="failed", reason="execution_failed"))
    report = build_mechanism_evidence_report_v1(spec, binding, failed_run)
    evaluator = next(item for item in report.predictions if item.metric_id == "evaluator.exit_attribution_count")
    assert evaluator.availability == "unavailable"
    assert evaluator.unavailable_reason == "execution_failed"


def test_projection_rejects_foreign_context_at_direct_and_canonical_boundaries() -> None:
    spec, binding, run, parent_bundle, parent_revision, candidate_bundle, candidate_revision = _identity_run()
    match = MechanismEvaluatorMatchV1(
        stage="discovery",
        episode_id="episode-projection-boundary",
        episode_ordinal=8,
        scenario_id="base",
        parent=_episode_evaluation(
            policy_identity_sha256=parent_revision.sha256,
            panel_sha256="8" * 64,
            episode_id="episode-projection-boundary",
            episode_ordinal=8,
            ma_violation=1,
            closed_trades=2,
        ),
        candidate=_episode_evaluation(
            policy_identity_sha256=candidate_revision.sha256,
            panel_sha256="8" * 64,
            episode_id="episode-projection-boundary",
            episode_ordinal=8,
            ma_violation=2,
            closed_trades=2,
        ),
        parent_revision=parent_revision,
        parent_source_bundle=parent_bundle,
        candidate_revision=candidate_revision,
        candidate_source_bundle=candidate_bundle,
    )
    report = build_mechanism_evidence_report_v1(spec, binding, run, matched_evaluations=(match,))
    projection = MechanismLearningProjectionV1.from_report(spec, report)
    foreign_context = replace(projection.consequence_contexts[0].context, evaluator_contract_sha256="9" * 64)
    foreign_group = replace(projection.consequence_contexts[0], context=foreign_context)
    with pytest.raises(ValueError, match="evaluator|context"):
        replace(projection, predictions=projection.predictions[:2])
    with pytest.raises(ValueError, match="context|binding|evaluator"):
        replace(projection, consequence_contexts=(foreign_group,))

    primitive = json.loads(projection.to_canonical_json())
    primitive["consequence_contexts"][0]["context"]["scenario_id"] = "foreign"
    with pytest.raises(ValueError, match="context|binding|scenario"):
        MechanismLearningProjectionV1.from_canonical_json(json.dumps(primitive, separators=(",", ":")))


def test_evaluator_match_binds_stage_to_typed_panel_or_episode_context() -> None:
    _, parent_revision, _, candidate_revision, _, _ = _identity_fixture()
    parent_bundle, _, candidate_bundle, _, _, _ = _identity_fixture()
    parent_panel = _panel_evaluation(
        policy_identity_sha256=parent_revision.sha256,
        panel_sha256="9" * 64,
        ma_violation=1,
        closed_trades=1,
    )
    candidate_panel = _panel_evaluation(
        policy_identity_sha256=candidate_revision.sha256,
        panel_sha256="9" * 64,
        ma_violation=2,
        closed_trades=1,
    )
    with pytest.raises(ValueError, match="stage|discovery|episode"):
        MechanismEvaluatorMatchV1(
            stage="discovery",
            episode_id="arbitrary-discovery",
            episode_ordinal=9,
            scenario_id="base",
            parent=parent_panel,
            candidate=candidate_panel,
            parent_revision=parent_revision,
            parent_source_bundle=parent_bundle,
            candidate_revision=candidate_revision,
            candidate_source_bundle=candidate_bundle,
        )


def test_panel_only_quick_context_requires_registered_plan_identity() -> None:
    _, parent_revision, _, candidate_revision, _, _ = _identity_fixture()
    parent_bundle, _, candidate_bundle, _, _, _ = _identity_fixture()
    parent_panel = _panel_evaluation(
        policy_identity_sha256=parent_revision.sha256,
        panel_sha256="a" * 64,
        ma_violation=1,
        closed_trades=1,
    )
    candidate_panel = _panel_evaluation(
        policy_identity_sha256=candidate_revision.sha256,
        panel_sha256="a" * 64,
        ma_violation=2,
        closed_trades=1,
    )
    quick_plan = EpisodePlanV5(
        episode_id="quick",
        episode_ordinal=None,
        purpose="quick",
        start_date=parent_panel.start_date,
        end_date=parent_panel.end_date,
        lineage_ids=("lineage-quick",),
        panel_ref=ArtifactRefV5("panels/quick.json", parent_panel.panel_sha256),
    )
    match = MechanismEvaluatorMatchV1(
        stage="quick",
        episode_id="quick",
        episode_ordinal=None,
        scenario_id="base",
        parent=parent_panel,
        candidate=candidate_panel,
        parent_revision=parent_revision,
        parent_source_bundle=parent_bundle,
        candidate_revision=candidate_revision,
        candidate_source_bundle=candidate_bundle,
        episode_plan=quick_plan,
    )
    assert match.episode_plan == quick_plan
    with pytest.raises(ValueError, match="quick|episode"):
        replace(match, episode_id="relabeled-quick", episode_plan=replace(quick_plan, episode_id="relabeled-quick"))


def test_quick_context_constructor_and_canonical_decode_require_registered_label() -> None:
    spec, binding, run, parent_bundle, parent_revision, candidate_bundle, candidate_revision = _identity_run()
    parent_panel = _panel_evaluation(
        policy_identity_sha256=parent_revision.sha256,
        panel_sha256="b" * 64,
        ma_violation=1,
        closed_trades=1,
    )
    candidate_panel = _panel_evaluation(
        policy_identity_sha256=candidate_revision.sha256,
        panel_sha256="b" * 64,
        ma_violation=2,
        closed_trades=1,
    )
    quick_plan = EpisodePlanV5(
        episode_id="quick",
        episode_ordinal=None,
        purpose="quick",
        start_date=parent_panel.start_date,
        end_date=parent_panel.end_date,
        lineage_ids=("lineage-quick",),
        panel_ref=ArtifactRefV5("panels/quick.json", parent_panel.panel_sha256),
    )
    match = MechanismEvaluatorMatchV1(
        stage="quick",
        episode_id="quick",
        episode_ordinal=None,
        scenario_id="base",
        parent=parent_panel,
        candidate=candidate_panel,
        parent_revision=parent_revision,
        parent_source_bundle=parent_bundle,
        candidate_revision=candidate_revision,
        candidate_source_bundle=candidate_bundle,
        episode_plan=quick_plan,
    )
    report = build_mechanism_evidence_report_v1(spec, binding, run, matched_evaluations=(match,))
    context = report.consequence_contexts[0].context
    assert context.episode_id == "quick"
    assert MechanismEvidenceReportV1.from_canonical_json(report.to_canonical_json()) == report

    with pytest.raises(ValueError, match="quick"):
        replace(context, episode_id="quick-alias")

    primitive = json.loads(report.to_canonical_json())
    primitive["consequence_contexts"][0]["context"]["episode_id"] = "quick-alias"
    with pytest.raises(ValueError, match="quick"):
        MechanismEvidenceReportV1.from_canonical_json(json.dumps(primitive, separators=(",", ":")))


def test_local_only_projection_accepts_no_evaluator_channel_and_round_trips() -> None:
    base_spec = _observation_spec()
    local_spec = replace(
        base_spec,
        metrics=base_spec.metrics[:2],
        disconfirming_observations=(base_spec.disconfirming_observations[0],),
    )
    corpus = build_mechanism_observation_corpus_v1(local_spec, seed_snapshot=_exit_seed())
    binding = _observation_binding(local_spec, corpus)
    parent_worker, candidate_worker = _fixture_workers(binding, corpus)
    run = collect_mechanism_observations_v1(
        local_spec,
        binding,
        corpus,
        parent_worker=parent_worker,
        candidate_worker=candidate_worker,
    )

    report = build_mechanism_evidence_report_v1(local_spec, binding, run)
    assert tuple(item.metric_id for item in report.predictions) == (
        "exit.decision_changed_count",
        "exit.protected_control_unchanged_count",
    )
    assert report.consequence_contexts == ()
    projection = MechanismLearningProjectionV1.from_report(local_spec, report)
    assert projection.controls == local_spec.controls
    assert projection.predictions == report.predictions
    assert projection.consequence_contexts == ()
    assert MechanismLearningProjectionV1.from_canonical_json(projection.to_canonical_json()) == projection


def test_evaluator_identity_domains_reject_foreign_bundle_revision_and_policy() -> None:
    spec, binding, run, parent_bundle, parent_revision, candidate_bundle, candidate_revision = _identity_run()
    foreign_bundle, foreign_revision = _identity_fixture()[-2:]

    def make_match(
        *,
        candidate_policy: str,
        candidate_identity: PolicyRevisionIdentityV5,
        candidate_source: SourceBundleV5,
        evaluator_contract_sha256: str = "e" * 64,
    ) -> MechanismEvaluatorMatchV1:
        return MechanismEvaluatorMatchV1(
            stage="discovery",
            episode_id="episode-identity",
            episode_ordinal=5,
            scenario_id="base",
            parent=_episode_evaluation(
                policy_identity_sha256=parent_revision.sha256,
                panel_sha256="8" * 64,
                episode_id="episode-identity",
                episode_ordinal=5,
                ma_violation=1,
                closed_trades=1,
            ),
            candidate=_episode_evaluation(
                policy_identity_sha256=candidate_policy,
                panel_sha256="8" * 64,
                episode_id="episode-identity",
                episode_ordinal=5,
                ma_violation=2,
                closed_trades=1,
                evaluator_contract_sha256=evaluator_contract_sha256,
            ),
            parent_revision=parent_revision,
            parent_source_bundle=parent_bundle,
            candidate_revision=candidate_identity,
            candidate_source_bundle=candidate_source,
        )

    with pytest.raises(ValueError, match="source bundle|candidate bytes"):
        build_mechanism_evidence_report_v1(
            spec,
            binding,
            run,
            matched_evaluations=(
                make_match(
                    candidate_policy=foreign_revision.sha256,
                    candidate_identity=foreign_revision,
                    candidate_source=foreign_bundle,
                ),
            ),
        )

    with pytest.raises(ValueError, match="revision|policy"):
        build_mechanism_evidence_report_v1(
            spec,
            binding,
            run,
            matched_evaluations=(
                make_match(
                    candidate_policy=candidate_revision.sha256,
                    candidate_identity=foreign_revision,
                    candidate_source=candidate_bundle,
                ),
            ),
        )

    with pytest.raises(ValueError, match="evaluator"):
        build_mechanism_evidence_report_v1(
            spec,
            binding,
            run,
            matched_evaluations=(
                make_match(
                    candidate_policy=candidate_revision.sha256,
                    candidate_identity=candidate_revision,
                    candidate_source=candidate_bundle,
                    evaluator_contract_sha256="9" * 64,
                ),
            ),
        )


def test_reducer_preserves_mixed_context_rows_and_does_not_emit_favorable_aggregate() -> None:
    spec, binding, run, parent_bundle, parent_revision, candidate_bundle, candidate_revision = _identity_run()
    matches = (
        MechanismEvaluatorMatchV1(
            stage="discovery",
            episode_id="episode-1",
            episode_ordinal=1,
            scenario_id="base",
            parent=_episode_evaluation(
                policy_identity_sha256=parent_revision.sha256,
                panel_sha256="f" * 64,
                episode_id="episode-1",
                episode_ordinal=1,
                ma_violation=0,
                closed_trades=2,
            ),
            candidate=_episode_evaluation(
                policy_identity_sha256=candidate_revision.sha256,
                panel_sha256="f" * 64,
                episode_id="episode-1",
                episode_ordinal=1,
                ma_violation=2,
                closed_trades=2,
            ),
            parent_revision=parent_revision,
            parent_source_bundle=parent_bundle,
            candidate_revision=candidate_revision,
            candidate_source_bundle=candidate_bundle,
        ),
        MechanismEvaluatorMatchV1(
            stage="discovery",
            episode_id="episode-2",
            episode_ordinal=2,
            scenario_id="base",
            parent=_episode_evaluation(
                policy_identity_sha256=parent_revision.sha256,
                panel_sha256="2" * 64,
                episode_id="episode-2",
                episode_ordinal=2,
                ma_violation=4,
                closed_trades=4,
            ),
            candidate=_episode_evaluation(
                policy_identity_sha256=candidate_revision.sha256,
                panel_sha256="2" * 64,
                episode_id="episode-2",
                episode_ordinal=2,
                ma_violation=1,
                closed_trades=4,
            ),
            parent_revision=parent_revision,
            parent_source_bundle=parent_bundle,
            candidate_revision=candidate_revision,
            candidate_source_bundle=candidate_bundle,
        ),
    )

    report = build_mechanism_evidence_report_v1(
        spec,
        binding,
        run,
        matched_evaluations=matches,
    )
    evaluator = next(item for item in report.predictions if item.metric_id == "evaluator.exit_attribution_count")
    assert evaluator.availability == "unavailable"
    assert evaluator.unavailable_reason == "mixed_context"
    assert len(report.consequence_contexts) == 2
    context_assessments = tuple(group.predictions[0].assessment for group in report.consequence_contexts)
    assert context_assessments == ("insufficient_evidence", "insufficient_evidence")
    assert tuple(group.context.episode_ordinal for group in report.consequence_contexts) == (1, 2)


def test_reducer_revalidates_corpus_before_trusting_a_run() -> None:
    spec = _observation_spec()
    corpus = build_mechanism_observation_corpus_v1(spec, seed_snapshot=_exit_seed())
    binding = _observation_binding(spec, corpus)
    parent_worker, candidate_worker = _fixture_workers(binding, corpus)
    run = collect_mechanism_observations_v1(
        spec,
        binding,
        corpus,
        parent_worker=parent_worker,
        candidate_worker=candidate_worker,
    )
    forged_case = replace(corpus.cases[1], applicable=not corpus.cases[1].applicable)
    forged_corpus = replace(corpus, cases=(*corpus.cases[:1], forged_case, *corpus.cases[2:]))
    forged_run = run
    object.__setattr__(forged_run, "corpus", forged_corpus)

    with pytest.raises(ValueError, match="corpus|applicability"):
        build_mechanism_evidence_report_v1(spec, binding, forged_run)
