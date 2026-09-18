"""Focused pure-contract checks for V5 mechanism evidence."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, localcontext
import json

import pytest

from core.pit_optimizer_v5.contracts import HypothesisV5, MetricPredictionV5
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
    bind_mechanism_observation_v1,
    validate_mechanism_spec_hypothesis_v1,
    validate_mechanism_observation_binding_v1,
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
        MechanismPredictionResultV1.from_measurement(
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
        MechanismPredictionResultV1.from_measurement(
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
