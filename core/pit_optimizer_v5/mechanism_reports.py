"""Deterministic reduction of bounded V5 mechanism observations.

This module consumes only typed offline observations and already-authenticated
V5 evaluator values.  It does not execute a policy, replay a portfolio, call a
provider, or infer a measurement from an unrelated aggregate.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import re
from typing import Literal

from .candidate_ir import PolicyRevisionIdentityV5, SourceBundleV5
from .contracts import (
    EpisodePlanV5,
    EpisodeEvaluationV5,
    EvaluationReportV5,
    PanelEvaluationV5,
    canonical_sha256_v5,
)
from .mechanism_contracts import (
    MechanismEvaluatorContextGroupV1,
    MechanismEvaluatorContextV1,
    MechanismEvidenceReportV1,
    MechanismExperimentSpecV1,
    MechanismMetricSpecV1,
    MechanismObservationBindingV1,
    MechanismPredictionResultV1,
    aggregate_mechanism_evaluator_predictions_v1,
    mechanism_evaluator_context_key_v1,
    validate_mechanism_report_against_spec_v1,
)
from .mechanism_probes import (
    MechanismObservationRunV1,
    MechanismPairedObservationV1,
    validate_mechanism_observation_corpus_v1,
)


_TEXT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
MechanismEvaluatorStageV1 = Literal["quick", "discovery"]
_CONSEQUENCE_AGGREGATION_V1 = "sum_contexts_v1"


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _TEXT_RE.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _positive_integer(value: object, label: str) -> int:
    if type(value) is not int or value < 1 or value > 100_000:
        raise ValueError(f"{label} must be a bounded positive integer")
    return value


def _validate_revision_source_mapping(
    revision: PolicyRevisionIdentityV5,
    source_bundle: SourceBundleV5,
    label: str,
) -> None:
    if type(revision) is not PolicyRevisionIdentityV5:
        raise ValueError(f"{label} policy revision is invalid")
    if type(source_bundle) is not SourceBundleV5:
        raise ValueError(f"{label} source bundle is invalid")
    actual = tuple((item.path, item.sha256) for item in source_bundle.files)
    if revision.editable_source_sha256 != actual:
        raise ValueError(f"{label} policy revision does not authenticate its source bundle")


@dataclass(frozen=True, slots=True)
class MechanismEvaluatorMatchV1:
    """Controller-supplied parent/candidate values for one matched context."""

    stage: MechanismEvaluatorStageV1
    episode_id: str
    episode_ordinal: int | None
    scenario_id: str
    parent: PanelEvaluationV5 | EpisodeEvaluationV5
    candidate: PanelEvaluationV5 | EpisodeEvaluationV5
    parent_revision: PolicyRevisionIdentityV5
    parent_source_bundle: SourceBundleV5
    candidate_revision: PolicyRevisionIdentityV5
    candidate_source_bundle: SourceBundleV5
    episode_plan: EpisodePlanV5 | None = None

    def __post_init__(self) -> None:
        if self.stage not in {"quick", "discovery"}:
            raise ValueError("evaluator match stage is unsupported")
        _identifier(self.episode_id, "evaluator match episode ID")
        if self.episode_ordinal is None:
            if self.stage != "quick":
                raise ValueError("only quick evaluator matches may be unnumbered")
        else:
            _positive_integer(self.episode_ordinal, "evaluator match episode ordinal")
            if self.stage == "quick":
                raise ValueError("quick evaluator matches must be unnumbered")
        _identifier(self.scenario_id, "evaluator match scenario ID")
        if type(self.parent) not in {PanelEvaluationV5, EpisodeEvaluationV5}:
            raise ValueError("evaluator match parent must be PanelEvaluationV5 or EpisodeEvaluationV5")
        if type(self.candidate) is not type(self.parent):
            raise ValueError("evaluator match parent and candidate types must match")
        _validate_revision_source_mapping(self.parent_revision, self.parent_source_bundle, "parent")
        _validate_revision_source_mapping(self.candidate_revision, self.candidate_source_bundle, "candidate")
        parent_panel = _panel(self.parent)
        candidate_panel = _panel(self.candidate)
        if parent_panel.policy_identity_sha256 != self.parent_revision.sha256:
            raise ValueError("evaluator match parent policy differs from the supplied revision")
        if candidate_panel.policy_identity_sha256 != self.candidate_revision.sha256:
            raise ValueError("evaluator match candidate policy differs from the supplied revision")
        if parent_panel.start_date != candidate_panel.start_date or parent_panel.end_date != candidate_panel.end_date:
            raise ValueError("evaluator match parent and candidate date ranges differ")
        if parent_panel.evaluator_contract_sha256 != candidate_panel.evaluator_contract_sha256:
            raise ValueError("evaluator match evaluator identities differ")
        if parent_panel.sandbox_profile_sha256 != candidate_panel.sandbox_profile_sha256:
            raise ValueError("evaluator match sandbox identities differ")
        if parent_panel.panel_sha256 != candidate_panel.panel_sha256:
            raise ValueError("evaluator match panel identities differ")
        if (
            parent_panel.selection_scenario_id != self.scenario_id
            or candidate_panel.selection_scenario_id != self.scenario_id
        ):
            raise ValueError("evaluator match scenario is not the selected scenario")
        if parent_panel.policy_identity_sha256 == candidate_panel.policy_identity_sha256:
            raise ValueError("evaluator match parent and candidate policies must differ")
        if isinstance(self.parent, EpisodeEvaluationV5):
            if self.stage != "discovery" or self.episode_ordinal is None:
                raise ValueError("episode evaluator matches must be numbered discovery contexts")
            if (
                self.parent.episode_id != self.episode_id
                or self.parent.episode_ordinal != self.episode_ordinal
                or self.candidate.episode_id != self.episode_id
                or self.candidate.episode_ordinal != self.episode_ordinal
            ):
                raise ValueError("evaluator match episode identity differs")
            if self.episode_plan is not None:
                raise ValueError("discovery episode evaluator matches do not accept a quick episode plan")
        else:
            if self.stage != "quick" or self.episode_ordinal is not None:
                raise ValueError("panel-only evaluator matches require stage=quick and an unnumbered quick context")
            if type(self.episode_plan) is not EpisodePlanV5:
                raise ValueError("panel-only quick evaluator matches require their typed episode plan")
            if (
                self.episode_plan.episode_id != self.episode_id
                or self.episode_plan.episode_id != "quick"
                or self.episode_plan.episode_ordinal is not None
                or self.episode_plan.purpose != "quick"
                or self.episode_plan.start_date != parent_panel.start_date
                or self.episode_plan.end_date != parent_panel.end_date
                or self.episode_plan.panel_ref.sha256 != parent_panel.panel_sha256
            ):
                raise ValueError("panel-only quick evaluator match differs from its typed episode plan")


def _panel(value: PanelEvaluationV5 | EpisodeEvaluationV5) -> PanelEvaluationV5:
    if type(value) is PanelEvaluationV5:
        return value
    if type(value) is EpisodeEvaluationV5:
        return value.evaluation
    raise ValueError("evaluator value must use the V5 panel or episode schema")


def _report_for_match(value: PanelEvaluationV5 | EpisodeEvaluationV5, scenario_id: str) -> EvaluationReportV5:
    panel = _panel(value)
    matches = tuple(item for item in panel.scenarios if item.scenario_id == scenario_id)
    if len(matches) != 1:
        raise ValueError("evaluator match scenario does not resolve exactly once")
    return matches[0].report


def match_mechanism_evaluator_context_v1(
    binding: MechanismObservationBindingV1,
    match: MechanismEvaluatorMatchV1,
) -> tuple[MechanismEvaluatorContextV1, EvaluationReportV5, EvaluationReportV5]:
    """Authenticate one supplied parent/candidate evaluator context."""

    if type(binding) is not MechanismObservationBindingV1:
        raise ValueError("evaluator match binding is invalid")
    if type(match) is not MechanismEvaluatorMatchV1:
        raise ValueError("evaluator match is invalid")
    parent_panel = _panel(match.parent)
    candidate_panel = _panel(match.candidate)
    if parent_panel.evaluator_contract_sha256 != binding.evaluator_contract_sha256:
        raise ValueError("evaluator match differs from the bound evaluator")
    if parent_panel.policy_identity_sha256 != binding.parent_revision_sha256:
        raise ValueError("evaluator match parent differs from the authored parent")
    if match.parent_revision.sha256 != binding.parent_revision_sha256:
        raise ValueError("evaluator match parent revision differs from the authored parent")
    if match.candidate_source_bundle.sha256 != binding.candidate_bytes_sha256:
        raise ValueError("evaluator match candidate source bundle differs from the rendered candidate bytes")
    if match.scenario_id != binding.scenario_id:
        raise ValueError("evaluator match scenario differs from the observation binding")
    parent_report = _report_for_match(match.parent, match.scenario_id)
    candidate_report = _report_for_match(match.candidate, match.scenario_id)
    context = MechanismEvaluatorContextV1(
        stage=match.stage,
        episode_id=match.episode_id,
        episode_ordinal=match.episode_ordinal,
        scenario_id=match.scenario_id,
        evaluator_contract_sha256=parent_panel.evaluator_contract_sha256,
        panel_sha256=parent_panel.panel_sha256,
        parent_policy_identity_sha256=parent_panel.policy_identity_sha256,
        candidate_policy_identity_sha256=candidate_panel.policy_identity_sha256,
        parent_source_bundle_sha256=match.parent_source_bundle.sha256,
        candidate_source_bundle_sha256=match.candidate_source_bundle.sha256,
        parent_report_sha256=canonical_sha256_v5(parent_report),
        candidate_report_sha256=canonical_sha256_v5(candidate_report),
    )
    return context, parent_report, candidate_report


def _first_observation_by_case(run: MechanismObservationRunV1) -> dict[int, MechanismPairedObservationV1]:
    result: dict[int, MechanismPairedObservationV1] = {}
    for observation in run.observations:
        result.setdefault(observation.case_order, observation)
    return result


def _selected_cases(
    run: MechanismObservationRunV1,
    denominator: str,
) -> tuple[MechanismPairedObservationV1, ...]:
    first = _first_observation_by_case(run)
    observations = tuple(first[order] for order in sorted(first))
    if denominator == "relevant_cases":
        return tuple(item for item in observations if item.applicable)
    if denominator == "control_cases":
        # The protected control population is declared by the observation
        # corpus, not by applicability.  It includes exercised and negative
        # cases so an applicable control violation cannot disappear.
        return observations
    if denominator == "all_cases":
        return observations
    raise ValueError("paired metric cannot use evaluator_cases")


def _local_prediction(
    metric: MechanismMetricSpecV1,
    *,
    run: MechanismObservationRunV1,
    minimum_relevant_cases: int,
) -> MechanismPredictionResultV1:
    if run.execution.status != "completed":
        reason = "not_run" if run.execution.status == "not_run" else "execution_failed"
        return MechanismPredictionResultV1.unavailable(
            metric,
            minimum_relevant_cases=minimum_relevant_cases,
            reason=reason,  # type: ignore[arg-type]
        )
    cases = _selected_cases(run, metric.denominator)
    denominator = len(cases)
    if metric.metric_id == "exit.decision_changed_count":
        parent_value = Decimal("0")
        candidate_value = Decimal(sum(item.decision_changed for item in cases))
    elif metric.metric_id == "exit.protected_control_unchanged_count":
        parent_value = Decimal(denominator)
        candidate_value = Decimal(sum(item.protected_control_unchanged for item in cases))
    else:
        raise ValueError("local reducer received an evaluator metric")
    return MechanismPredictionResultV1.from_measurement(
        metric,
        parent_value=parent_value,
        candidate_value=candidate_value,
        numerator=candidate_value,
        denominator=denominator,
        minimum_relevant_cases=minimum_relevant_cases,
    )


def _selector_count(report: EvaluationReportV5, metric_id: str) -> int | None:
    matches = tuple(item.count for item in report.exit_attribution if item.metric_id == metric_id)
    if len(matches) > 1:
        raise ValueError("evaluator diagnostic selector resolves more than once")
    return matches[0] if matches else None


def _context_group(
    metric: MechanismMetricSpecV1,
    *,
    context: MechanismEvaluatorContextV1,
    parent_report: EvaluationReportV5,
    candidate_report: EvaluationReportV5,
    minimum_relevant_cases: int,
) -> MechanismEvaluatorContextGroupV1:
    if metric.selector is None or metric.selector.section != "exit_attribution":
        raise ValueError("evaluator metric selector is not registered")
    selector_id = metric.selector.metric_id
    parent_value = _selector_count(parent_report, selector_id)
    candidate_value = _selector_count(candidate_report, selector_id)
    if parent_value is None or candidate_value is None:
        result = MechanismPredictionResultV1.unavailable(
            metric,
            minimum_relevant_cases=minimum_relevant_cases,
            reason="evaluator_metric_missing",
        )
    else:
        # A matched report pair is one evaluator case.  Report trade counts
        # remain observed diagnostic values, not denominator substitutes.
        result = MechanismPredictionResultV1.from_measurement(
            metric,
            parent_value=Decimal(parent_value),
            candidate_value=Decimal(candidate_value),
            numerator=Decimal(candidate_value),
            denominator=1,
            minimum_relevant_cases=minimum_relevant_cases,
        )
    return MechanismEvaluatorContextGroupV1(
        context=context,
        predictions=(result,),
        aggregate_formula=_CONSEQUENCE_AGGREGATION_V1,
    )


def _aggregate_contexts(
    metric: MechanismMetricSpecV1,
    groups: tuple[MechanismEvaluatorContextGroupV1, ...],
    *,
    minimum_relevant_cases: int,
) -> MechanismPredictionResultV1:
    return aggregate_mechanism_evaluator_predictions_v1(
        metric,
        tuple(group.predictions[0] for group in groups),
        minimum_relevant_cases=minimum_relevant_cases,
    )


def _limitations(
    run: MechanismObservationRunV1,
    *,
    consequence_contexts: tuple[MechanismEvaluatorContextGroupV1, ...],
    evaluator_metric: MechanismPredictionResultV1 | None,
) -> tuple[str, ...]:
    values = list(run.limitations)
    values.append(
        "Paired count denominators use the registered relevant, control, or all-case population; repetitions do not add cases."
    )
    values.append(
        "Each available evaluator context represents one matched parent/candidate report observation; aggregate evaluator_cases count available matched contexts, while diagnostic counts may exceed that denominator."
    )
    if consequence_contexts:
        values.append(
            "Matched evaluator contexts are retained by stage, episode, scenario, evaluator, panel, and report identities."
        )
    if evaluator_metric is not None and evaluator_metric.unavailable_reason == "mixed_context":
        values.append(
            "Matched evaluator contexts have mixed or incomplete results; no aggregate consequence is asserted."
        )
    elif evaluator_metric is not None and evaluator_metric.unavailable_reason == "evaluator_metric_missing":
        values.append("The registered evaluator diagnostic selector was absent from one or more supplied reports.")
    return tuple(dict.fromkeys(values))


def build_mechanism_evidence_report_v1(
    spec: MechanismExperimentSpecV1,
    binding: MechanismObservationBindingV1,
    run: MechanismObservationRunV1,
    *,
    matched_evaluations: tuple[MechanismEvaluatorMatchV1, ...] = (),
) -> MechanismEvidenceReportV1:
    """Reduce a trusted observation run into every declared prediction row."""

    if type(spec) is not MechanismExperimentSpecV1 or type(binding) is not MechanismObservationBindingV1:
        raise ValueError("mechanism reduction spec/binding is invalid")
    if type(run) is not MechanismObservationRunV1:
        raise ValueError("mechanism reduction run is invalid")
    if run.binding != binding:
        raise ValueError("mechanism reduction run binding differs from the requested binding")
    if type(matched_evaluations) is not tuple or any(
        type(item) is not MechanismEvaluatorMatchV1 for item in matched_evaluations
    ):
        raise ValueError("matched evaluator contexts are invalid")
    # This is intentionally the first trust decision after type/binding checks:
    # a self-consistent rebound corpus must not influence any denominator.
    validate_mechanism_observation_corpus_v1(spec, binding, run.corpus)

    context_groups: list[MechanismEvaluatorContextGroupV1] = []
    evaluator_metric = next(
        (item for item in spec.metrics if item.metric_id == "evaluator.exit_attribution_count"),
        None,
    )
    if evaluator_metric is not None:
        for match in matched_evaluations:
            context, parent_report, candidate_report = match_mechanism_evaluator_context_v1(binding, match)
            context_groups.append(
                _context_group(
                    evaluator_metric,
                    context=context,
                    parent_report=parent_report,
                    candidate_report=candidate_report,
                    minimum_relevant_cases=spec.minimum_relevant_cases,
                )
            )
    elif matched_evaluations:
        raise ValueError("matched evaluator contexts require the declared evaluator metric")

    groups = tuple(
        sorted(
            context_groups,
            key=lambda group: (
                group.context.stage,
                group.context.episode_ordinal if group.context.episode_ordinal is not None else 0,
                group.context.episode_id,
                group.context.scenario_id,
                group.context.panel_sha256,
                group.context.parent_report_sha256,
                group.context.candidate_report_sha256,
            ),
        )
    )
    context_keys = tuple(mechanism_evaluator_context_key_v1(group.context) for group in groups)
    if len(set(context_keys)) != len(context_keys):
        raise ValueError("matched evaluator contexts must be semantically unique")
    if run.execution.status != "completed":
        unavailable_reason = "not_run" if run.execution.status == "not_run" else "execution_failed"
        predictions = [
            MechanismPredictionResultV1.unavailable(
                metric,
                minimum_relevant_cases=spec.minimum_relevant_cases,
                reason=unavailable_reason,  # type: ignore[arg-type]
            )
            for metric in spec.metrics
        ]
    else:
        predictions = []
        for metric in spec.metrics:
            if metric.metric_id == "evaluator.exit_attribution_count":
                predictions.append(
                    _aggregate_contexts(
                        metric,
                        groups,
                        minimum_relevant_cases=spec.minimum_relevant_cases,
                    )
                )
            else:
                predictions.append(
                    _local_prediction(
                        metric,
                        run=run,
                        minimum_relevant_cases=spec.minimum_relevant_cases,
                    )
                )

    evaluator_prediction = next(
        (item for item in predictions if item.metric_id == "evaluator.exit_attribution_count"),
        None,
    )

    report = MechanismEvidenceReportV1(
        binding=binding,
        execution=run.execution,
        coverage=run.coverage,
        predictions=tuple(predictions),
        limitations=_limitations(
            run,
            consequence_contexts=groups,
            evaluator_metric=evaluator_prediction,
        ),
        consequence_contexts=groups,
    )
    validate_mechanism_report_against_spec_v1(spec, report)
    return report


reduce_mechanism_observations_v1 = build_mechanism_evidence_report_v1
reduce_mechanism_evidence_v1 = build_mechanism_evidence_report_v1


__all__ = [
    "MechanismEvaluatorMatchV1",
    "MechanismEvaluatorStageV1",
    "build_mechanism_evidence_report_v1",
    "match_mechanism_evaluator_context_v1",
    "reduce_mechanism_evidence_v1",
    "reduce_mechanism_observations_v1",
]
