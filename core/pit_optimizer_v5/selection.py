"""Pure quick-screen and deterministic scheduling rules for optimizer V5."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from core.pit_optimizer_v5.candidate_ir import (
    RenderedVariantV5,
    StructuralTemplateV5,
)
from core.pit_optimizer_v5.contracts import (
    CampaignPanelPlanV5,
    EpisodeEvaluationV5,
    EpisodePlanV5,
    EvaluatorContractV5,
    HypothesisV5,
    InvestigatorArtifactV5,
    PanelEvaluationV5,
    SearchCapabilitiesV5,
    ValidationResultV5,
    selected_scenario,
)
from core.pit_optimizer_v5.memory import StoredExperimentRecordV5
from core.pit_optimizer_v5.probes import SemanticFingerprintV5
from core.pit_optimizer_v5.search import (
    PRIMARY_MECHANISMS_V5,
    ArchiveEntryV5,
    BaselineParentAuthorityV5,
    CandidateArchiveV5,
    ParentCandidateV5,
    SearchStateV5,
    annualized_return_pct,
    archive_parent_from_record_v5,
    baseline_parent_candidate_v5,
    hypothesis_novelty_key_v5,
    verified_campaign_cagr_pct,
)


NoveltyOutcomeKindV5 = Literal[
    "scheduled_hypothesis",
    "no_novel_hypothesis",
    "novelty_exhausted",
]


def _digest(value: object, label: str) -> str:
    if type(value) is not str or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _positive_count(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


class SelectionFailureV5(ValueError):
    """Base class for stable pure-selection failures."""

    code = "selection_failure"


class QuickSelectionFailureV5(SelectionFailureV5):
    """Quick-screen inputs are inconsistent with their fixed authority."""

    code = "quick_selection_failure"


class QuickEvidenceMismatchV5(QuickSelectionFailureV5):
    """Quick evidence differs from the declared quick panel/evaluator."""

    code = "quick_evidence_mismatch"


class QuickCandidateConflictV5(QuickSelectionFailureV5):
    """One quick candidate identity has conflicting evidence."""

    code = "quick_candidate_conflict"


class ParentSelectionFailureV5(SelectionFailureV5):
    """A scheduled parent is not reconstructible from authenticated authority."""

    code = "parent_selection_failure"


class NoveltySelectionFailureV5(SelectionFailureV5):
    """Ranked hypotheses or exhaustion evidence are malformed."""

    code = "novelty_selection_failure"


def _strict_literal_equal(left: object, right: object) -> bool:
    return type(left) is type(right) and left == right


def _assignment_matches_template(candidate: "QuickScreenCandidateV5") -> bool:
    axes = candidate.template.axes
    values = candidate.variant.assignment.values
    if tuple(name for name, _ in values) != tuple(axis.name for axis in axes):
        return False
    return all(
        any(_strict_literal_equal(value, allowed) for allowed in axis.values)
        for axis, (_, value) in zip(axes, values, strict=True)
    )


@dataclass(frozen=True, slots=True)
class QuickScreenCandidateV5:
    """Pre-critic candidate evidence used only for compute-budget selection."""

    template: StructuralTemplateV5
    variant: RenderedVariantV5
    validation: ValidationResultV5
    semantic_fingerprint: SemanticFingerprintV5 | None
    parent_semantic_fingerprint_sha256: str
    quick_evidence: PanelEvaluationV5 | None

    def __post_init__(self) -> None:
        if (
            type(self.template) is not StructuralTemplateV5
            or type(self.variant) is not RenderedVariantV5
            or type(self.validation) is not ValidationResultV5
        ):
            raise ValueError("quick-screen candidate components are invalid")
        if self.semantic_fingerprint is not None and type(self.semantic_fingerprint) is not SemanticFingerprintV5:
            raise ValueError("quick-screen semantic fingerprint is invalid")
        if self.quick_evidence is not None and type(self.quick_evidence) is not PanelEvaluationV5:
            raise ValueError("quick-screen panel evidence is invalid")
        _digest(
            self.parent_semantic_fingerprint_sha256,
            "quick-screen parent fingerprint",
        )
        if not _assignment_matches_template(self):
            raise ValueError("quick-screen assignment differs from its template")
        if self.validation.valid and (self.validation.changed_symbols != self.template.changed_symbols):
            raise ValueError("quick-screen validation differs from its template")
        if self.quick_evidence is not None and (
            self.quick_evidence.policy_identity_sha256 != self.variant.policy_revision.sha256
        ):
            raise ValueError("quick-screen evidence differs from its policy")

    @property
    def is_declared_default(self) -> bool:
        return all(
            _strict_literal_equal(value, axis.default)
            for axis, (_, value) in zip(
                self.template.axes,
                self.variant.assignment.values,
                strict=True,
            )
        )

    @property
    def is_behaviorally_distinct(self) -> bool:
        return (
            self.semantic_fingerprint is not None
            and self.semantic_fingerprint.fingerprint_sha256 != self.parent_semantic_fingerprint_sha256
        )

    @property
    def is_eligible(self) -> bool:
        return (
            self.validation.valid
            and self.variant.policy_revision.sha256 != self.template.parent_revision_sha256
            and self.is_behaviorally_distinct
            and self.quick_evidence is not None
        )

    @property
    def policy_identity_sha256(self) -> str:
        return self.variant.policy_revision.sha256


def _quick_cagr_pct(
    *,
    candidate: QuickScreenCandidateV5,
    panel_plan: CampaignPanelPlanV5,
    evaluator_contract: EvaluatorContractV5,
) -> Decimal:
    evidence = candidate.quick_evidence
    if evidence is None:
        raise QuickEvidenceMismatchV5()
    if (
        evidence.evaluator_contract_sha256 != evaluator_contract.sha256
        or evidence.sandbox_profile_sha256 != evaluator_contract.sandbox_profile_sha256
        or evidence.policy_identity_sha256 != candidate.policy_identity_sha256
        or evidence.panel_sha256 != panel_plan.quick.panel_ref.sha256
        or evidence.start_date != panel_plan.quick.start_date
        or evidence.end_date != panel_plan.quick.end_date
        or evidence.selection_scenario_id != evaluator_contract.selection_scenario_id
        or evidence.selection_scenario_id != "base"
        or tuple(item.scenario_id for item in evidence.scenarios) != ("base",)
    ):
        raise QuickEvidenceMismatchV5()
    try:
        scenario = selected_scenario(evidence)
        recomputed = annualized_return_pct(
            starting_equity=scenario.starting_equity,
            ending_equity=scenario.ending_equity,
            days=evidence.elapsed_calendar_days,
        )
    except ValueError as exc:
        raise QuickEvidenceMismatchV5() from exc
    if scenario.report.portfolio_annualized_return_pct != recomputed:
        raise QuickEvidenceMismatchV5()
    return recomputed


def _validate_quick_authorities_v5(
    *,
    panel_plan: CampaignPanelPlanV5,
    evaluator_contract: EvaluatorContractV5,
) -> None:
    if (
        panel_plan.pit_bundle_ref.sha256 != evaluator_contract.pit_bundle_sha256
        or panel_plan.prices_provenance_ref.sha256 != evaluator_contract.prices_provenance_sha256
    ):
        raise QuickEvidenceMismatchV5()


def _deduplicate_quick_candidates(
    candidates: tuple[QuickScreenCandidateV5, ...],
) -> tuple[QuickScreenCandidateV5, ...]:
    by_policy: dict[str, QuickScreenCandidateV5] = {}
    for candidate in candidates:
        identity = candidate.policy_identity_sha256
        prior = by_policy.get(identity)
        if prior is not None and prior != candidate:
            raise QuickCandidateConflictV5()
        by_policy[identity] = candidate

    # A suite-local behavior is screened only once. Prefer the declared default
    # within its behavior group, otherwise the canonical policy identity.
    by_fingerprint: dict[str, QuickScreenCandidateV5] = {}
    for candidate in sorted(
        by_policy.values(),
        key=lambda item: (
            not item.is_declared_default,
            item.policy_identity_sha256,
        ),
    ):
        assert candidate.semantic_fingerprint is not None
        fingerprint = candidate.semantic_fingerprint.fingerprint_sha256
        by_fingerprint.setdefault(fingerprint, candidate)
    return tuple(by_fingerprint.values())


def _select_discovery_survivors_v5(
    *,
    candidates: tuple[QuickScreenCandidateV5, ...],
    maximum: int,
    panel_plan: CampaignPanelPlanV5,
    evaluator_contract: EvaluatorContractV5,
) -> tuple[QuickScreenCandidateV5, ...]:
    _positive_count(maximum, "discovery survivor maximum")
    if (
        type(candidates) is not tuple
        or any(type(item) is not QuickScreenCandidateV5 for item in candidates)
        or type(panel_plan) is not CampaignPanelPlanV5
        or type(evaluator_contract) is not EvaluatorContractV5
    ):
        raise ValueError("quick survivor inputs are invalid")
    _validate_quick_authorities_v5(
        panel_plan=panel_plan,
        evaluator_contract=evaluator_contract,
    )
    eligible = tuple(item for item in candidates if item.is_eligible)
    if not eligible:
        return ()
    template_ids = {item.template.sha256 for item in eligible}
    parent_fingerprints = {item.parent_semantic_fingerprint_sha256 for item in eligible}
    if len(template_ids) != 1 or len(parent_fingerprints) != 1:
        raise QuickCandidateConflictV5()
    unique = _deduplicate_quick_candidates(eligible)
    scored = tuple(
        (
            item,
            _quick_cagr_pct(
                candidate=item,
                panel_plan=panel_plan,
                evaluator_contract=evaluator_contract,
            ),
        )
        for item in unique
    )
    defaults = tuple(item for item, _score in scored if item.is_declared_default)
    if len(defaults) > 1:
        raise QuickCandidateConflictV5()
    selected: list[QuickScreenCandidateV5] = list(defaults)
    selected_ids = {item.policy_identity_sha256 for item in selected}
    remaining = sorted(
        ((item, score) for item, score in scored if item.policy_identity_sha256 not in selected_ids),
        key=lambda pair: (-pair[1], pair[0].policy_identity_sha256),
    )
    for item, _score in remaining:
        if len(selected) == maximum:
            break
        selected.append(item)
    return tuple(selected[:maximum])


def select_manifest_discovery_survivors_v5(
    *,
    candidates: tuple[QuickScreenCandidateV5, ...],
    capabilities: SearchCapabilitiesV5,
    panel_plan: CampaignPanelPlanV5,
    evaluator_contract: EvaluatorContractV5,
) -> tuple[QuickScreenCandidateV5, ...]:
    """Select manifest-bounded quick survivors under one authenticated authority."""

    if type(capabilities) is not SearchCapabilitiesV5:
        raise ValueError("search capabilities are invalid")
    return _select_discovery_survivors_v5(
        candidates=candidates,
        maximum=capabilities.max_discovery_survivors_per_template,
        panel_plan=panel_plan,
        evaluator_contract=evaluator_contract,
    )


def canonicalize_discovery_evidence_v5(
    episodes: tuple[EpisodeEvaluationV5, ...],
) -> tuple[EpisodeEvaluationV5, ...]:
    if type(episodes) is not tuple or any(type(item) is not EpisodeEvaluationV5 for item in episodes):
        raise ValueError("discovery evidence is invalid")
    ordered = tuple(sorted(episodes, key=lambda item: item.episode_ordinal))
    if len(ordered) != 4 or tuple(item.episode_ordinal for item in ordered) != (1, 2, 3, 4):
        raise ValueError("discovery evidence must contain ordinals 1 through 4")
    return ordered


def discovery_episode_execution_order_v5(
    *,
    discovery_plan: CampaignPanelPlanV5,
    round_index: int,
) -> tuple[EpisodePlanV5, ...]:
    """Rotate execution order while leaving persisted evidence canonically ordered."""

    if type(discovery_plan) is not CampaignPanelPlanV5:
        raise ValueError("discovery plan is invalid")
    _positive_count(round_index, "discovery round")
    canonical = discovery_plan.discovery
    offset = (round_index - 1) % len(canonical)
    return canonical[offset:] + canonical[:offset]


def _stored_record_for_entry(
    *,
    entry: ArchiveEntryV5,
    stored_records: tuple[StoredExperimentRecordV5, ...],
) -> StoredExperimentRecordV5:
    matches = tuple(item for item in stored_records if item.reference == entry.experiment_record_ref)
    if len(matches) != 1:
        raise ParentSelectionFailureV5()
    return matches[0]


def _verify_archive_scores(
    *,
    archive: CandidateArchiveV5,
    discovery_plan: CampaignPanelPlanV5,
    evaluator_contract: EvaluatorContractV5,
) -> None:
    for entry in archive.entries:
        verified_campaign_cagr_pct(
            campaign=entry.campaign,
            discovery_plan=discovery_plan,
            evaluator_contract=evaluator_contract,
            policy_identity_sha256=entry.policy_identity_sha256,
        )


def _archive_parent_order(
    archive: CandidateArchiveV5,
) -> tuple[ArchiveEntryV5, ...]:
    if not archive.entries:
        return ()
    ordered: list[ArchiveEntryV5] = []
    seen: set[str] = set()

    def include(entry: ArchiveEntryV5) -> None:
        identity = entry.policy_identity_sha256
        if identity not in seen:
            ordered.append(entry)
            seen.add(identity)

    include(archive.entries[0])
    for mechanism in PRIMARY_MECHANISMS_V5:
        family = next(
            (item for item in archive.entries if item.primary_mechanism == mechanism),
            None,
        )
        if family is not None:
            include(family)
    for entry in archive.entries:
        include(entry)
    return tuple(ordered)


def parent_schedule_v5(
    *,
    state: SearchStateV5,
    baseline: BaselineParentAuthorityV5,
    discovery_plan: CampaignPanelPlanV5,
    evaluator_contract: EvaluatorContractV5,
    stored_records: tuple[StoredExperimentRecordV5, ...],
) -> tuple[ParentCandidateV5, ...]:
    """Return the round-rotated reconstructible parent pool."""

    if (
        type(state) is not SearchStateV5
        or type(stored_records) is not tuple
        or any(type(item) is not StoredExperimentRecordV5 for item in stored_records)
    ):
        raise ValueError("parent scheduling inputs are invalid")
    references = tuple((item.reference.relative_path, item.reference.sha256) for item in stored_records)
    if len(set(references)) != len(references):
        raise ParentSelectionFailureV5()
    baseline_parent = baseline_parent_candidate_v5(
        authority=baseline,
        discovery_plan=discovery_plan,
        evaluator_contract=evaluator_contract,
    )
    if not state.archive.entries:
        return (baseline_parent,)
    _verify_archive_scores(
        archive=state.archive,
        discovery_plan=discovery_plan,
        evaluator_contract=evaluator_contract,
    )
    if state.archive.entries[0].campaign_cagr_pct <= baseline_parent.campaign_cagr_pct:
        return (baseline_parent,)

    archive_order = _archive_parent_order(state.archive)
    parents = tuple(
        archive_parent_from_record_v5(
            entry=entry,
            stored_record=_stored_record_for_entry(
                entry=entry,
                stored_records=stored_records,
            ),
        )
        for entry in archive_order
    )
    offset = (state.next_round_index - 1) % len(parents)
    return parents[offset:] + parents[:offset]


def select_parent_v5(
    *,
    state: SearchStateV5,
    baseline: BaselineParentAuthorityV5,
    discovery_plan: CampaignPanelPlanV5,
    evaluator_contract: EvaluatorContractV5,
    stored_records: tuple[StoredExperimentRecordV5, ...],
) -> ParentCandidateV5:
    return parent_schedule_v5(
        state=state,
        baseline=baseline,
        discovery_plan=discovery_plan,
        evaluator_contract=evaluator_contract,
        stored_records=stored_records,
    )[0]


def reported_champion_v5(
    *,
    state: SearchStateV5,
    baseline: BaselineParentAuthorityV5,
    discovery_plan: CampaignPanelPlanV5,
    evaluator_contract: EvaluatorContractV5,
    stored_records: tuple[StoredExperimentRecordV5, ...],
) -> ParentCandidateV5:
    """Return baseline unless an archive candidate strictly improves on it."""

    if type(state) is not SearchStateV5:
        raise ValueError("champion search state is invalid")
    schedule = parent_schedule_v5(
        state=SearchStateV5(
            next_round_index=1,
            archive=state.archive,
            attempted_novelty_keys=state.attempted_novelty_keys,
        ),
        baseline=baseline,
        discovery_plan=discovery_plan,
        evaluator_contract=evaluator_contract,
        stored_records=stored_records,
    )
    return schedule[0]


@dataclass(frozen=True, slots=True)
class ScheduledHypothesisV5:
    parent: ParentCandidateV5
    hypothesis: HypothesisV5
    novelty_key: str
    outcome: Literal["scheduled_hypothesis"] = "scheduled_hypothesis"

    def __post_init__(self) -> None:
        if (
            type(self.parent) is not ParentCandidateV5
            or type(self.hypothesis) is not HypothesisV5
            or self.outcome != "scheduled_hypothesis"
        ):
            raise ValueError("scheduled hypothesis is invalid")
        _digest(self.novelty_key, "scheduled novelty key")
        if self.novelty_key != hypothesis_novelty_key_v5(
            parent_revision_sha256=self.parent.policy_identity_sha256,
            hypothesis=self.hypothesis,
        ):
            raise ValueError("scheduled novelty key differs from controller derivation")

    def to_primitive(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "parent": self.parent.to_primitive(),
            "hypothesis_sha256": self.hypothesis.sha256,
            "novelty_key": self.novelty_key,
        }


@dataclass(frozen=True, slots=True)
class NoNovelHypothesisV5:
    parent: ParentCandidateV5
    ranked_novelty_keys: tuple[str, ...]
    outcome: Literal["no_novel_hypothesis"] = "no_novel_hypothesis"

    def __post_init__(self) -> None:
        if type(self.parent) is not ParentCandidateV5 or self.outcome != ("no_novel_hypothesis"):
            raise ValueError("no-novel hypothesis outcome is invalid")
        if type(self.ranked_novelty_keys) is not tuple or not self.ranked_novelty_keys:
            raise ValueError("no-novel keys are invalid")
        tuple(_digest(item, "no-novel hypothesis key") for item in self.ranked_novelty_keys)
        if len(set(self.ranked_novelty_keys)) != len(self.ranked_novelty_keys):
            raise ValueError("no-novel hypothesis keys must be unique")

    def to_primitive(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "parent": self.parent.to_primitive(),
            "ranked_novelty_keys": list(self.ranked_novelty_keys),
        }


@dataclass(frozen=True, slots=True)
class NoveltyExhaustedV5:
    parents: tuple[ParentCandidateV5, ...]
    parent_outcomes: tuple[NoNovelHypothesisV5, ...]
    outcome: Literal["novelty_exhausted"] = "novelty_exhausted"

    def __post_init__(self) -> None:
        if (
            type(self.parents) is not tuple
            or not self.parents
            or any(type(item) is not ParentCandidateV5 for item in self.parents)
            or type(self.parent_outcomes) is not tuple
            or any(type(item) is not NoNovelHypothesisV5 for item in self.parent_outcomes)
            or self.outcome != "novelty_exhausted"
        ):
            raise ValueError("novelty-exhausted outcome is invalid")
        if tuple(item.parent for item in self.parent_outcomes) != self.parents:
            raise ValueError("novelty exhaustion does not cover every parent in order")
        parent_ids = tuple(item.policy_identity_sha256 for item in self.parents)
        if len(set(parent_ids)) != len(parent_ids):
            raise ValueError("novelty exhaustion parents must be unique")

    def to_primitive(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "parents": [item.to_primitive() for item in self.parents],
            "parent_outcomes": [item.to_primitive() for item in self.parent_outcomes],
        }


NoveltyDecisionV5 = ScheduledHypothesisV5 | NoNovelHypothesisV5


def select_novel_hypothesis_v5(
    *,
    state: SearchStateV5,
    parent: ParentCandidateV5,
    artifact: InvestigatorArtifactV5,
    capabilities: SearchCapabilitiesV5,
) -> NoveltyDecisionV5:
    """Select the first ranked controller-novel hypothesis for one parent."""

    if (
        type(state) is not SearchStateV5
        or type(parent) is not ParentCandidateV5
        or type(artifact) is not InvestigatorArtifactV5
        or type(capabilities) is not SearchCapabilitiesV5
    ):
        raise ValueError("novelty selection inputs are invalid")
    expected = capabilities.hypotheses_per_investigator
    ranked = tuple(sorted(artifact.hypotheses, key=lambda item: item.rank))
    if len(ranked) != expected or tuple(item.rank for item in ranked) != tuple(range(1, expected + 1)):
        raise NoveltySelectionFailureV5()
    keys = tuple(
        hypothesis_novelty_key_v5(
            parent_revision_sha256=parent.policy_identity_sha256,
            hypothesis=hypothesis,
        )
        for hypothesis in ranked
    )
    attempted = set(state.attempted_novelty_keys)
    for hypothesis, key in zip(ranked, keys, strict=True):
        if key not in attempted:
            return ScheduledHypothesisV5(
                parent=parent,
                hypothesis=hypothesis,
                novelty_key=key,
            )
    unique_keys = tuple(dict.fromkeys(keys))
    return NoNovelHypothesisV5(
        parent=parent,
        ranked_novelty_keys=unique_keys,
    )


def record_novelty_attempt_v5(
    state: SearchStateV5,
    decision: ScheduledHypothesisV5,
) -> SearchStateV5:
    """Record a selected key; final record reduction advances its round."""

    if type(state) is not SearchStateV5 or type(decision) is not ScheduledHypothesisV5:
        raise ValueError("novelty-attempt transition inputs are invalid")
    if decision.novelty_key in state.attempted_novelty_keys:
        raise NoveltySelectionFailureV5()
    return SearchStateV5(
        next_round_index=state.next_round_index,
        archive=state.archive,
        attempted_novelty_keys=tuple(sorted({*state.attempted_novelty_keys, decision.novelty_key})),
    )


def advance_no_novel_parent_v5(
    state: SearchStateV5,
    outcome: NoNovelHypothesisV5,
    *,
    artifact: InvestigatorArtifactV5,
    capabilities: SearchCapabilitiesV5,
) -> SearchStateV5:
    """Advance rotation after a typed no-author/no-critic parent outcome."""

    if type(state) is not SearchStateV5 or type(outcome) is not NoNovelHypothesisV5:
        raise ValueError("no-novel transition inputs are invalid")
    expected = select_novel_hypothesis_v5(
        state=state,
        parent=outcome.parent,
        artifact=artifact,
        capabilities=capabilities,
    )
    if expected != outcome:
        raise NoveltySelectionFailureV5()
    return SearchStateV5(
        next_round_index=state.next_round_index + 1,
        archive=state.archive,
        attempted_novelty_keys=state.attempted_novelty_keys,
    )


def classify_novelty_exhaustion_v5(
    *,
    state: SearchStateV5,
    baseline: BaselineParentAuthorityV5,
    discovery_plan: CampaignPanelPlanV5,
    evaluator_contract: EvaluatorContractV5,
    stored_records: tuple[StoredExperimentRecordV5, ...],
    artifacts: tuple[InvestigatorArtifactV5, ...],
    capabilities: SearchCapabilitiesV5,
    outcomes: tuple[NoNovelHypothesisV5, ...],
) -> NoveltyExhaustedV5:
    """Issue terminal exhaustion only after every reconstructible parent failed."""

    if type(state) is not SearchStateV5:
        raise ValueError("novelty exhaustion state is invalid")
    parents = parent_schedule_v5(
        state=state,
        baseline=baseline,
        discovery_plan=discovery_plan,
        evaluator_contract=evaluator_contract,
        stored_records=stored_records,
    )
    if (
        type(artifacts) is not tuple
        or len(artifacts) != len(parents)
        or any(type(item) is not InvestigatorArtifactV5 for item in artifacts)
        or type(capabilities) is not SearchCapabilitiesV5
    ):
        raise NoveltySelectionFailureV5()
    attempted = set(state.attempted_novelty_keys)
    if any(
        not outcome.ranked_novelty_keys or not set(outcome.ranked_novelty_keys).issubset(attempted)
        for outcome in outcomes
    ):
        raise NoveltySelectionFailureV5()
    expected_outcomes = tuple(
        select_novel_hypothesis_v5(
            state=state,
            parent=parent,
            artifact=artifact,
            capabilities=capabilities,
        )
        for parent, artifact in zip(parents, artifacts, strict=True)
    )
    if expected_outcomes != outcomes or any(type(item) is not NoNovelHypothesisV5 for item in expected_outcomes):
        raise NoveltySelectionFailureV5()
    try:
        return NoveltyExhaustedV5(
            parents=parents,
            parent_outcomes=outcomes,
        )
    except ValueError as exc:
        raise NoveltySelectionFailureV5() from exc


__all__ = [
    "NoNovelHypothesisV5",
    "NoveltyDecisionV5",
    "NoveltyExhaustedV5",
    "NoveltyOutcomeKindV5",
    "NoveltySelectionFailureV5",
    "ParentSelectionFailureV5",
    "QuickCandidateConflictV5",
    "QuickEvidenceMismatchV5",
    "QuickScreenCandidateV5",
    "QuickSelectionFailureV5",
    "ScheduledHypothesisV5",
    "SelectionFailureV5",
    "advance_no_novel_parent_v5",
    "canonicalize_discovery_evidence_v5",
    "classify_novelty_exhaustion_v5",
    "discovery_episode_execution_order_v5",
    "parent_schedule_v5",
    "record_novelty_attempt_v5",
    "reported_champion_v5",
    "select_manifest_discovery_survivors_v5",
    "select_novel_hypothesis_v5",
    "select_parent_v5",
]
