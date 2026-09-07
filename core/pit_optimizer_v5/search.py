"""Pure campaign scoring, archive, and search-state rules for optimizer V5."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, DecimalException, ROUND_HALF_EVEN, localcontext
from typing import Literal

from core.pit_optimizer_v5.candidate_ir import (
    PolicyRevisionIdentityV5,
    SourceBundleV5,
)
from core.pit_optimizer_v5.contracts import (
    AnnualizedReturnTargetV5,
    ArtifactRefV5,
    CampaignEvidenceV5,
    CampaignPanelPlanV5,
    EpisodeEvaluationV5,
    EvaluatorContractV5,
    HypothesisV5,
    canonical_primitive_v5,
    canonical_sha256_v5,
    selected_scenario,
    validate_campaign_evidence_v5,
)
from core.pit_optimizer_v5.memory import ExperimentRecordV5, StoredExperimentRecordV5
from core.pit_optimizer_v5.probes import SemanticFingerprintV5


CAMPAIGN_CAGR_QUANTUM_V5 = Decimal("0.000001")
TARGET_GAP_QUANTUM_V5 = Decimal("0.01")
DEFAULT_ARCHIVE_CAPACITY_V5 = 8
PRIMARY_MECHANISMS_V5 = (
    "entry",
    "risk_sizing",
    "position_management",
    "exit",
    "cross_policy",
)
PrimaryMechanismV5 = Literal[
    "entry",
    "risk_sizing",
    "position_management",
    "exit",
    "cross_policy",
]
ArchiveAdmissionOutcomeV5 = Literal[
    "ineligible_status",
    "zero_activity",
    "duplicate",
    "admitted",
    "not_admitted",
]


def _digest(value: object, label: str) -> str:
    if type(value) is not str or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _positive_count(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _finite_decimal(value: object, label: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError(f"{label} must be a finite Decimal")
    return value


class SearchFailureV5(ValueError):
    """Base class for stable pure-search failures."""

    code = "search_failure"


class CampaignScoringFailureV5(SearchFailureV5):
    """Campaign evidence cannot be scored under its declared authorities."""

    code = "campaign_scoring_failure"


class IncompleteCampaignEvidence(CampaignScoringFailureV5):
    """The exact four discovery episode ordinals are unavailable."""

    code = "incomplete_campaign_evidence"


IncompleteCampaignEvidenceV5 = IncompleteCampaignEvidence


class PanelCommitmentMismatch(CampaignScoringFailureV5):
    """An episode differs from the panel committed for its ordinal."""

    code = "panel_commitment_mismatch"

    def __init__(self, episode_id: str | None = None) -> None:
        self.episode_id = episode_id
        super().__init__(self.code)


PanelCommitmentMismatchV5 = PanelCommitmentMismatch


class CampaignScenarioMismatch(CampaignScoringFailureV5):
    """Discovery evidence does not carry the exact declared scenario authority."""

    code = "campaign_scenario_mismatch"


class CampaignAuthorityMismatch(CampaignScoringFailureV5):
    """Campaign evidence differs from its evaluator, plan, or policy authority."""

    code = "campaign_authority_mismatch"


class CampaignScoreMismatch(CampaignScoringFailureV5):
    """A stored campaign score differs from transparent recomputation."""

    code = "campaign_score_mismatch"


class InvalidAnnualizationEvidence(CampaignScoringFailureV5):
    """Equity endpoints or elapsed time cannot produce a finite CAGR."""

    code = "invalid_annualization_evidence"


class ArchiveFailureV5(SearchFailureV5):
    """Base class for archive transition failures."""

    code = "archive_failure"


class ArchiveAuthorityMismatchV5(ArchiveFailureV5):
    """An archive candidate lacks an exact authenticated source/record binding."""

    code = "archive_authority_mismatch"


class ArchiveCapacityInsufficientV5(ArchiveFailureV5):
    """The configured capacity cannot retain every required family leader."""

    code = "archive_capacity_insufficient"


class ArchiveRevisionConflictV5(ArchiveFailureV5):
    """One immutable policy revision has conflicting archive evidence."""

    code = "archive_revision_conflict"


def annualized_return_pct(
    *,
    starting_equity: Decimal,
    ending_equity: Decimal,
    days: int,
) -> Decimal:
    """Annualize exact Decimal endpoints at the V5 evaluator metric precision."""

    try:
        start = _finite_decimal(starting_equity, "starting equity")
        end = _finite_decimal(ending_equity, "ending equity")
        elapsed = _positive_count(days, "elapsed calendar days")
    except ValueError as exc:
        raise InvalidAnnualizationEvidence() from exc
    if start <= 0 or end <= 0:
        raise InvalidAnnualizationEvidence()
    try:
        with localcontext() as context:
            context.prec = 40
            context.rounding = ROUND_HALF_EVEN
            result = ((end / start) ** (Decimal(365) / Decimal(elapsed)) - Decimal(1)) * Decimal(100)
            if not result.is_finite():
                raise InvalidAnnualizationEvidence()
            return result.quantize(
                CAMPAIGN_CAGR_QUANTUM_V5,
                rounding=ROUND_HALF_EVEN,
            )
    except (DecimalException, OverflowError, ValueError) as exc:
        if isinstance(exc, InvalidAnnualizationEvidence):
            raise
        raise InvalidAnnualizationEvidence() from exc


def _canonical_campaign_episodes(
    episodes: tuple[EpisodeEvaluationV5, ...],
) -> tuple[EpisodeEvaluationV5, ...]:
    if (
        type(episodes) is not tuple
        or len(episodes) != 4
        or any(type(item) is not EpisodeEvaluationV5 for item in episodes)
    ):
        raise IncompleteCampaignEvidence()
    ordered = tuple(sorted(episodes, key=lambda item: item.episode_ordinal))
    if tuple(item.episode_ordinal for item in ordered) != (1, 2, 3, 4):
        raise IncompleteCampaignEvidence()
    return ordered


def campaign_cagr_pct(
    *,
    episodes: tuple[EpisodeEvaluationV5, ...],
    discovery_plan: CampaignPanelPlanV5,
    evaluator_contract: EvaluatorContractV5,
) -> Decimal:
    """Recompute four-panel CAGR from exact plan/evaluator commitments.

    Input execution order is irrelevant. Only the evaluator contract's selected
    base-cost scenario contributes to the score; the complete declared grid is
    nevertheless required so discovery evidence remains critic-complete.
    """

    if type(discovery_plan) is not CampaignPanelPlanV5:
        raise CampaignAuthorityMismatch()
    if type(evaluator_contract) is not EvaluatorContractV5:
        raise CampaignAuthorityMismatch()
    if (
        discovery_plan.pit_bundle_ref.sha256 != evaluator_contract.pit_bundle_sha256
        or discovery_plan.prices_provenance_ref.sha256 != evaluator_contract.prices_provenance_sha256
    ):
        raise CampaignAuthorityMismatch()
    ordered = _canonical_campaign_episodes(episodes)
    committed = tuple(sorted(discovery_plan.discovery, key=lambda item: item.episode_ordinal or 0))
    expected_scenarios = tuple(item.scenario_id for item in evaluator_contract.friction_grid)
    evaluator_digests: set[str] = set()
    sandbox_digests: set[str] = set()
    policy_digests: set[str] = set()
    selected_evidence = []
    for evidence, plan in zip(ordered, committed, strict=True):
        if (
            evidence.episode_id != plan.episode_id
            or evidence.episode_ordinal != plan.episode_ordinal
            or evidence.start_date != plan.start_date
            or evidence.end_date != plan.end_date
            or evidence.evaluation.panel_sha256 != plan.panel_ref.sha256
        ):
            raise PanelCommitmentMismatch(evidence.episode_id)
        evaluation = evidence.evaluation
        evaluator_digests.add(evaluation.evaluator_contract_sha256)
        sandbox_digests.add(evaluation.sandbox_profile_sha256)
        policy_digests.add(evaluation.policy_identity_sha256)
        if (
            evaluation.evaluator_contract_sha256 != evaluator_contract.sha256
            or evaluation.sandbox_profile_sha256 != evaluator_contract.sandbox_profile_sha256
            or evaluation.selection_scenario_id != evaluator_contract.selection_scenario_id
            or evaluation.selection_scenario_id != "base"
            or frozenset(item.scenario_id for item in evaluation.scenarios) != frozenset(expected_scenarios)
            or len(evaluation.scenarios) != len(expected_scenarios)
        ):
            raise CampaignScenarioMismatch()
        try:
            selected_evidence.append(selected_scenario(evaluation))
        except ValueError as exc:
            raise CampaignScenarioMismatch() from exc
    if len(evaluator_digests) != 1 or len(sandbox_digests) != 1 or len(policy_digests) != 1:
        raise CampaignAuthorityMismatch()

    try:
        with localcontext() as context:
            context.prec = 40
            context.rounding = ROUND_HALF_EVEN
            growth = Decimal(1)
            for scenario in selected_evidence:
                start = _finite_decimal(
                    scenario.starting_equity,
                    "campaign starting equity",
                )
                end = _finite_decimal(
                    scenario.ending_equity,
                    "campaign ending equity",
                )
                if start <= 0 or end <= 0:
                    raise InvalidAnnualizationEvidence()
                growth *= end / start
            days = sum(item.evaluation.elapsed_calendar_days for item in ordered)
            return annualized_return_pct(
                starting_equity=Decimal(1),
                ending_equity=growth,
                days=days,
            )
    except (DecimalException, OverflowError, ValueError) as exc:
        if isinstance(exc, CampaignScoringFailureV5):
            raise
        raise InvalidAnnualizationEvidence() from exc


def verified_campaign_cagr_pct(
    *,
    campaign: CampaignEvidenceV5,
    discovery_plan: CampaignPanelPlanV5,
    evaluator_contract: EvaluatorContractV5,
    policy_identity_sha256: str,
) -> Decimal:
    """Authenticate and transparently rederive one stored campaign score."""

    if type(campaign) is not CampaignEvidenceV5:
        raise CampaignAuthorityMismatch()
    try:
        validate_campaign_evidence_v5(
            campaign,
            panel_plan=discovery_plan,
            evaluator_contract=evaluator_contract,
            policy_identity_sha256=policy_identity_sha256,
        )
    except (TypeError, ValueError) as exc:
        raise CampaignAuthorityMismatch() from exc
    recomputed = campaign_cagr_pct(
        episodes=campaign.episodes,
        discovery_plan=discovery_plan,
        evaluator_contract=evaluator_contract,
    )
    if campaign.campaign_cagr_pct != recomputed:
        raise CampaignScoreMismatch()
    return recomputed


def _source_bundle_matches_revision(
    bundle: SourceBundleV5,
    revision: PolicyRevisionIdentityV5,
) -> bool:
    return tuple((item.path, item.sha256) for item in bundle.files) == (revision.editable_source_sha256)


@dataclass(frozen=True, slots=True)
class ArchiveEntryV5:
    policy_revision: PolicyRevisionIdentityV5
    primary_mechanism: PrimaryMechanismV5
    admitted_round: int
    campaign: CampaignEvidenceV5
    source_bundle_ref: ArtifactRefV5
    experiment_record_ref: ArtifactRefV5

    def __post_init__(self) -> None:
        if type(self.policy_revision) is not PolicyRevisionIdentityV5:
            raise ValueError("archive policy revision is invalid")
        if type(self.primary_mechanism) is not str or self.primary_mechanism not in PRIMARY_MECHANISMS_V5:
            raise ValueError("archive primary mechanism is invalid")
        _positive_count(self.admitted_round, "archive admission round")
        if type(self.campaign) is not CampaignEvidenceV5:
            raise ValueError("archive campaign evidence is invalid")
        if self.campaign.policy_identity_sha256 != self.policy_revision.sha256:
            raise ValueError("archive campaign differs from its policy revision")
        if self.campaign.closed_trades <= 0:
            raise ValueError("archive campaign must contain closed-trade activity")
        score = _finite_decimal(
            self.campaign.campaign_cagr_pct,
            "archive campaign CAGR",
        )
        if score != score.quantize(
            CAMPAIGN_CAGR_QUANTUM_V5,
            rounding=ROUND_HALF_EVEN,
        ):
            raise ValueError("archive campaign CAGR is not at evaluator precision")
        if type(self.source_bundle_ref) is not ArtifactRefV5 or type(self.experiment_record_ref) is not ArtifactRefV5:
            raise ValueError("archive artifact references are invalid")
        if self.experiment_record_ref.relative_path.rsplit("/", 1)[-1].endswith(
            ".json"
        ) is False or not self.experiment_record_ref.relative_path.startswith("records/"):
            raise ValueError("archive experiment record path is invalid")

    @property
    def campaign_cagr_pct(self) -> Decimal:
        return self.campaign.campaign_cagr_pct

    @property
    def policy_identity_sha256(self) -> str:
        return self.policy_revision.sha256

    def to_primitive(self) -> dict[str, object]:
        return {
            "policy_revision": self.policy_revision.to_primitive(),
            "primary_mechanism": self.primary_mechanism,
            "admitted_round": self.admitted_round,
            "campaign": canonical_primitive_v5(self.campaign),
            "source_bundle_ref": self.source_bundle_ref.to_primitive(),
            "experiment_record_ref": self.experiment_record_ref.to_primitive(),
        }


def _archive_rank(entry: ArchiveEntryV5) -> tuple[Decimal, int, str]:
    return (
        -entry.campaign_cagr_pct,
        entry.admitted_round,
        entry.policy_identity_sha256,
    )


@dataclass(frozen=True, slots=True)
class CandidateArchiveV5:
    capacity: int = DEFAULT_ARCHIVE_CAPACITY_V5
    entries: tuple[ArchiveEntryV5, ...] = ()

    def __post_init__(self) -> None:
        _positive_count(self.capacity, "archive capacity")
        if (
            type(self.entries) is not tuple
            or any(type(item) is not ArchiveEntryV5 for item in self.entries)
            or len(self.entries) > self.capacity
        ):
            raise ValueError("archive entries are invalid or exceed capacity")
        identities = tuple(item.policy_identity_sha256 for item in self.entries)
        if len(set(identities)) != len(identities):
            raise ValueError("archive policy revisions must be unique")
        if self.entries != tuple(sorted(self.entries, key=_archive_rank)):
            raise ValueError("archive entries are not in canonical rank order")

    def to_primitive(self) -> dict[str, object]:
        return {
            "capacity": self.capacity,
            "entries": [item.to_primitive() for item in self.entries],
        }


@dataclass(frozen=True, slots=True)
class SearchStateV5:
    next_round_index: int
    archive: CandidateArchiveV5
    attempted_novelty_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        _positive_count(self.next_round_index, "next search round")
        if type(self.archive) is not CandidateArchiveV5:
            raise ValueError("search archive is invalid")
        if type(self.attempted_novelty_keys) is not tuple:
            raise ValueError("attempted novelty keys are invalid")
        for key in self.attempted_novelty_keys:
            _digest(key, "attempted novelty key")
        if self.attempted_novelty_keys != tuple(sorted(set(self.attempted_novelty_keys))):
            raise ValueError("attempted novelty keys must be unique and canonical")

    def to_primitive(self) -> dict[str, object]:
        return {
            "next_round_index": self.next_round_index,
            "archive": self.archive.to_primitive(),
            "attempted_novelty_keys": list(self.attempted_novelty_keys),
        }


@dataclass(frozen=True, slots=True)
class ArchiveTransitionV5:
    archive: CandidateArchiveV5
    outcome: ArchiveAdmissionOutcomeV5
    experiment_id: str
    considered_revision_sha256: str | None = None
    evicted_revision_sha256: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.archive) is not CandidateArchiveV5:
            raise ValueError("archive transition result is invalid")
        if self.outcome not in {
            "ineligible_status",
            "zero_activity",
            "duplicate",
            "admitted",
            "not_admitted",
        }:
            raise ValueError("archive transition outcome is invalid")
        _digest(self.experiment_id, "archive transition experiment")
        if self.considered_revision_sha256 is not None:
            _digest(
                self.considered_revision_sha256,
                "archive transition considered revision",
            )
        if type(self.evicted_revision_sha256) is not tuple:
            raise ValueError("archive transition evictions are invalid")
        tuple(_digest(item, "archive transition evicted revision") for item in self.evicted_revision_sha256)
        if self.evicted_revision_sha256 != tuple(sorted(set(self.evicted_revision_sha256))):
            raise ValueError("archive transition evictions must be unique and canonical")
        if self.outcome == "admitted" and self.considered_revision_sha256 is None:
            raise ValueError("admitted archive transition requires a revision")
        if self.outcome != "admitted" and self.evicted_revision_sha256:
            raise ValueError("only admission may evict archive revisions")

    def to_primitive(self) -> dict[str, object]:
        return {
            "archive": self.archive.to_primitive(),
            "outcome": self.outcome,
            "experiment_id": self.experiment_id,
            "considered_revision_sha256": self.considered_revision_sha256,
            "evicted_revision_sha256": list(self.evicted_revision_sha256),
        }


@dataclass(frozen=True, slots=True)
class ArchiveRecordAuthorityV5:
    """Authenticated pure inputs omitted from the generic Task-5 reducer record."""

    experiment_id: str
    experiment_record_ref: ArtifactRefV5
    source_bundle: SourceBundleV5
    source_bundle_ref: ArtifactRefV5

    def __post_init__(self) -> None:
        _digest(self.experiment_id, "archive authority experiment")
        if (
            type(self.experiment_record_ref) is not ArtifactRefV5
            or type(self.source_bundle_ref) is not ArtifactRefV5
            or type(self.source_bundle) is not SourceBundleV5
        ):
            raise ValueError("archive record authority is invalid")
        if self.experiment_record_ref.relative_path != (f"records/{self.experiment_id}.json"):
            raise ValueError("archive record authority path differs from experiment")
        if self.source_bundle_ref.sha256 != self.source_bundle.sha256:
            raise ValueError("archive source reference differs from exact source bundle")


def _family_leaders(
    entries: tuple[ArchiveEntryV5, ...],
) -> tuple[ArchiveEntryV5, ...]:
    leaders: list[ArchiveEntryV5] = []
    for mechanism in PRIMARY_MECHANISMS_V5:
        matching = tuple(item for item in entries if item.primary_mechanism == mechanism)
        if matching:
            leaders.append(min(matching, key=_archive_rank))
    return tuple(leaders)


def _select_archive_entries(
    *,
    entries: tuple[ArchiveEntryV5, ...],
    capacity: int,
) -> tuple[ArchiveEntryV5, ...]:
    ranked = tuple(sorted(entries, key=_archive_rank))
    protected: list[ArchiveEntryV5] = []
    protected_ids: set[str] = set()
    for entry in (ranked[0], *_family_leaders(ranked)):
        identity = entry.policy_identity_sha256
        if identity not in protected_ids:
            protected.append(entry)
            protected_ids.add(identity)
    if len(protected) > capacity:
        raise ArchiveCapacityInsufficientV5()
    selected = list(protected)
    selected_ids = set(protected_ids)
    for entry in ranked:
        if len(selected) == capacity:
            break
        identity = entry.policy_identity_sha256
        if identity not in selected_ids:
            selected.append(entry)
            selected_ids.add(identity)
    return tuple(sorted(selected, key=_archive_rank))


def _admit_archive_entry_v5(
    archive: CandidateArchiveV5,
    entry: ArchiveEntryV5,
    *,
    experiment_id: str,
) -> ArchiveTransitionV5:
    """Apply deterministic global/family protected top-K admission."""

    if type(archive) is not CandidateArchiveV5 or type(entry) is not ArchiveEntryV5:
        raise ValueError("archive admission inputs are invalid")
    _digest(experiment_id, "archive admission experiment")
    existing = tuple(item for item in archive.entries if item.policy_identity_sha256 == entry.policy_identity_sha256)
    if existing:
        prior = existing[0]
        if (
            prior.primary_mechanism != entry.primary_mechanism
            or prior.campaign != entry.campaign
            or prior.source_bundle_ref != entry.source_bundle_ref
        ):
            raise ArchiveRevisionConflictV5()
        return ArchiveTransitionV5(
            archive=archive,
            outcome="duplicate",
            experiment_id=experiment_id,
            considered_revision_sha256=entry.policy_identity_sha256,
        )

    combined = (*archive.entries, entry)
    selected = _select_archive_entries(entries=combined, capacity=archive.capacity)
    result = CandidateArchiveV5(capacity=archive.capacity, entries=selected)
    selected_ids = {item.policy_identity_sha256 for item in selected}
    if entry.policy_identity_sha256 not in selected_ids:
        return ArchiveTransitionV5(
            archive=result,
            outcome="not_admitted",
            experiment_id=experiment_id,
            considered_revision_sha256=entry.policy_identity_sha256,
        )
    prior_ids = {item.policy_identity_sha256 for item in archive.entries}
    evicted = tuple(sorted(prior_ids - selected_ids))
    return ArchiveTransitionV5(
        archive=result,
        outcome="admitted",
        experiment_id=experiment_id,
        considered_revision_sha256=entry.policy_identity_sha256,
        evicted_revision_sha256=evicted,
    )


def expected_target_gap_pct_v5(
    *,
    target: AnnualizedReturnTargetV5,
    campaign_cagr: Decimal,
) -> Decimal:
    return (target.target_pct - campaign_cagr).quantize(
        TARGET_GAP_QUANTUM_V5,
        rounding=ROUND_HALF_EVEN,
    )


def transition_archive_from_record_v5(
    *,
    archive: CandidateArchiveV5,
    record: ExperimentRecordV5,
    authority: ArchiveRecordAuthorityV5 | None,
    discovery_plan: CampaignPanelPlanV5,
    evaluator_contract: EvaluatorContractV5,
    target: AnnualizedReturnTargetV5,
) -> ArchiveTransitionV5:
    """Admit one final record only through exact record/source/campaign authority."""

    if type(archive) is not CandidateArchiveV5 or type(record) is not ExperimentRecordV5:
        raise ValueError("archive transition inputs are invalid")
    if record.status not in {"evaluated", "zero_trade"}:
        return ArchiveTransitionV5(
            archive=archive,
            outcome="ineligible_status",
            experiment_id=record.experiment_id,
        )
    if (
        type(target) is not AnnualizedReturnTargetV5
        or target.sha256 != discovery_plan.target_sha256
        or discovery_plan.pit_bundle_ref.sha256 != evaluator_contract.pit_bundle_sha256
        or discovery_plan.prices_provenance_ref.sha256 != evaluator_contract.prices_provenance_sha256
    ):
        raise ArchiveAuthorityMismatchV5()
    if authority is None or type(authority) is not ArchiveRecordAuthorityV5:
        raise ArchiveAuthorityMismatchV5()
    if authority.experiment_id != record.experiment_id:
        raise ArchiveAuthorityMismatchV5()
    try:
        StoredExperimentRecordV5(
            reference=authority.experiment_record_ref,
            record=record,
        )
    except ValueError as exc:
        raise ArchiveAuthorityMismatchV5() from exc
    if (
        authority.source_bundle_ref not in record.artifact_refs
        or record.policy_revision is None
        or not _source_bundle_matches_revision(
            authority.source_bundle,
            record.policy_revision,
        )
        or record.campaign_evidence is None
        or record.target_gap_pct is None
    ):
        raise ArchiveAuthorityMismatchV5()
    campaign_score = verified_campaign_cagr_pct(
        campaign=record.campaign_evidence,
        discovery_plan=discovery_plan,
        evaluator_contract=evaluator_contract,
        policy_identity_sha256=record.policy_revision.sha256,
    )
    if record.target_gap_pct != expected_target_gap_pct_v5(
        target=target,
        campaign_cagr=campaign_score,
    ):
        raise ArchiveAuthorityMismatchV5()
    if record.campaign_evidence.closed_trades == 0:
        return ArchiveTransitionV5(
            archive=archive,
            outcome="zero_activity",
            experiment_id=record.experiment_id,
            considered_revision_sha256=record.policy_revision.sha256,
        )
    entry = ArchiveEntryV5(
        policy_revision=record.policy_revision,
        primary_mechanism=record.hypothesis.primary_mechanism,
        admitted_round=record.round_index,
        campaign=record.campaign_evidence,
        source_bundle_ref=authority.source_bundle_ref,
        experiment_record_ref=authority.experiment_record_ref,
    )
    return _admit_archive_entry_v5(
        archive,
        entry,
        experiment_id=record.experiment_id,
    )


def hypothesis_novelty_key_v5(
    *,
    parent_revision_sha256: str,
    hypothesis: HypothesisV5,
) -> str:
    """Derive novelty from controller-selected semantic fields, never model IDs."""

    _digest(parent_revision_sha256, "novelty parent revision")
    if type(hypothesis) is not HypothesisV5:
        raise ValueError("novelty hypothesis must use the V5 schema")
    predictions = tuple(
        sorted(
            (
                {
                    "metric_id": item.metric_id,
                    "direction": item.direction,
                    "rationale": item.rationale,
                }
                for item in hypothesis.predicted_changes
            ),
            key=lambda item: (
                item["metric_id"],
                item["direction"],
                item["rationale"],
            ),
        )
    )
    return canonical_sha256_v5(
        {
            "domain": "pit-optimizer-v5-novelty-v1",
            "parent_revision_sha256": parent_revision_sha256,
            "primary_mechanism": hypothesis.primary_mechanism,
            "causal_claim": hypothesis.causal_claim,
            "author_instructions": hypothesis.author_instructions,
            "predicted_changes": predictions,
        }
    )


@dataclass(frozen=True, slots=True)
class CandidateArchiveReducerV5:
    """Task-5-compatible deterministic reconstruction of complete search state."""

    discovery_plan: CampaignPanelPlanV5
    evaluator_contract: EvaluatorContractV5
    target: AnnualizedReturnTargetV5
    authorities: tuple[ArchiveRecordAuthorityV5, ...]
    capacity: int = DEFAULT_ARCHIVE_CAPACITY_V5

    def __post_init__(self) -> None:
        if (
            type(self.discovery_plan) is not CampaignPanelPlanV5
            or type(self.evaluator_contract) is not EvaluatorContractV5
            or type(self.target) is not AnnualizedReturnTargetV5
        ):
            raise ValueError("archive reducer authorities are invalid")
        if (
            self.target.sha256 != self.discovery_plan.target_sha256
            or self.discovery_plan.pit_bundle_ref.sha256 != self.evaluator_contract.pit_bundle_sha256
            or self.discovery_plan.prices_provenance_ref.sha256 != self.evaluator_contract.prices_provenance_sha256
        ):
            raise ValueError("archive reducer authorities are inconsistent")
        _positive_count(self.capacity, "archive reducer capacity")
        if type(self.authorities) is not tuple or any(
            type(item) is not ArchiveRecordAuthorityV5 for item in self.authorities
        ):
            raise ValueError("archive reducer record authorities are invalid")
        experiment_ids = tuple(item.experiment_id for item in self.authorities)
        if experiment_ids != tuple(sorted(set(experiment_ids))):
            raise ValueError("archive reducer record authorities must be unique and canonical")

    def initial(self) -> SearchStateV5:
        return SearchStateV5(
            next_round_index=1,
            archive=CandidateArchiveV5(capacity=self.capacity),
            attempted_novelty_keys=(),
        )

    def apply(
        self,
        state: SearchStateV5,
        record: ExperimentRecordV5,
    ) -> SearchStateV5:
        if type(state) is not SearchStateV5 or type(record) is not ExperimentRecordV5:
            raise ValueError("archive reducer inputs are invalid")
        if state.archive.capacity != self.capacity:
            raise ValueError("archive reducer state capacity changed")
        if record.experiment_identity.discovery_plan_sha256 != self.discovery_plan.discovery_plan_sha256:
            raise ArchiveAuthorityMismatchV5()
        authority = next(
            (item for item in self.authorities if item.experiment_id == record.experiment_id),
            None,
        )
        transition = transition_archive_from_record_v5(
            archive=state.archive,
            record=record,
            authority=authority,
            discovery_plan=self.discovery_plan,
            evaluator_contract=self.evaluator_contract,
            target=self.target,
        )
        novelty_key = hypothesis_novelty_key_v5(
            parent_revision_sha256=record.parent_revision_sha256,
            hypothesis=record.hypothesis,
        )
        return SearchStateV5(
            next_round_index=max(state.next_round_index, record.round_index + 1),
            archive=transition.archive,
            attempted_novelty_keys=tuple(sorted({*state.attempted_novelty_keys, novelty_key})),
        )

    def to_primitive(self, state: SearchStateV5) -> object:
        if type(state) is not SearchStateV5:
            raise ValueError("archive reducer state is invalid")
        return state.to_primitive()


@dataclass(frozen=True, slots=True)
class BaselineParentAuthorityV5:
    """Authenticated external baseline, deliberately outside archive capacity."""

    policy_revision: PolicyRevisionIdentityV5
    policy_revision_ref: ArtifactRefV5
    semantic_fingerprint: SemanticFingerprintV5
    campaign: CampaignEvidenceV5
    source_bundle: SourceBundleV5
    source_bundle_ref: ArtifactRefV5
    pit_data_scope: Literal["production", "development_sp500_v2"] = "production"

    def __post_init__(self) -> None:
        if (
            type(self.policy_revision) is not PolicyRevisionIdentityV5
            or type(self.policy_revision_ref) is not ArtifactRefV5
            or type(self.semantic_fingerprint) is not SemanticFingerprintV5
            or type(self.campaign) is not CampaignEvidenceV5
            or type(self.source_bundle) is not SourceBundleV5
            or type(self.source_bundle_ref) is not ArtifactRefV5
        ):
            raise ValueError("baseline parent authority is invalid")
        if self.pit_data_scope not in {"production", "development_sp500_v2"}:
            raise ValueError("baseline parent PIT data scope is invalid")
        if (
            self.policy_revision_ref.sha256 != self.policy_revision.sha256
            or self.source_bundle_ref.sha256 != self.source_bundle.sha256
            or not _source_bundle_matches_revision(
                self.source_bundle,
                self.policy_revision,
            )
            or self.campaign.policy_identity_sha256 != self.policy_revision.sha256
        ):
            raise ValueError("baseline parent authority bindings differ")


ParentOriginV5 = Literal["baseline", "archive"]


@dataclass(frozen=True, slots=True)
class ParentCandidateV5:
    """One reconstructible scheduling parent with explicit ancestry authority."""

    origin: ParentOriginV5
    policy_revision: PolicyRevisionIdentityV5
    semantic_fingerprint_sha256: str
    campaign_cagr_pct: Decimal
    source_bundle_ref: ArtifactRefV5
    experiment_record_ref: ArtifactRefV5 | None
    primary_mechanism: PrimaryMechanismV5 | None
    admitted_round: int | None

    def __post_init__(self) -> None:
        if self.origin not in {"baseline", "archive"}:
            raise ValueError("parent candidate origin is invalid")
        if type(self.policy_revision) is not PolicyRevisionIdentityV5:
            raise ValueError("parent candidate policy revision is invalid")
        _digest(
            self.semantic_fingerprint_sha256,
            "parent semantic fingerprint",
        )
        score = _finite_decimal(self.campaign_cagr_pct, "parent campaign CAGR")
        if score != score.quantize(
            CAMPAIGN_CAGR_QUANTUM_V5,
            rounding=ROUND_HALF_EVEN,
        ):
            raise ValueError("parent campaign CAGR is not at evaluator precision")
        if type(self.source_bundle_ref) is not ArtifactRefV5:
            raise ValueError("parent source reference is invalid")
        if self.origin == "baseline":
            if (
                self.experiment_record_ref is not None
                or self.primary_mechanism is not None
                or self.admitted_round is not None
            ):
                raise ValueError("baseline parent cannot carry archive ancestry")
        elif (
            type(self.experiment_record_ref) is not ArtifactRefV5
            or self.primary_mechanism not in PRIMARY_MECHANISMS_V5
            or type(self.admitted_round) is not int
            or self.admitted_round <= 0
        ):
            raise ValueError("archive parent ancestry is incomplete")

    @property
    def policy_identity_sha256(self) -> str:
        return self.policy_revision.sha256

    @property
    def selected_parent_record_ref(self) -> ArtifactRefV5 | None:
        return self.experiment_record_ref

    def to_primitive(self) -> dict[str, object]:
        return {
            "origin": self.origin,
            "policy_revision": self.policy_revision.to_primitive(),
            "semantic_fingerprint_sha256": self.semantic_fingerprint_sha256,
            "campaign_cagr_pct": canonical_primitive_v5(self.campaign_cagr_pct),
            "source_bundle_ref": self.source_bundle_ref.to_primitive(),
            "experiment_record_ref": (
                None if self.experiment_record_ref is None else self.experiment_record_ref.to_primitive()
            ),
            "primary_mechanism": self.primary_mechanism,
            "admitted_round": self.admitted_round,
        }


def baseline_parent_candidate_v5(
    *,
    authority: BaselineParentAuthorityV5,
    discovery_plan: CampaignPanelPlanV5,
    evaluator_contract: EvaluatorContractV5,
    pit_data_scope: Literal["production", "development_sp500_v2"],
) -> ParentCandidateV5:
    if type(authority) is not BaselineParentAuthorityV5:
        raise ValueError("baseline authority is invalid")
    if authority.pit_data_scope != pit_data_scope:
        raise CampaignAuthorityMismatch()
    if authority.source_bundle.sha256 != evaluator_contract.baseline_source_bundle_sha256:
        raise CampaignAuthorityMismatch()
    score = verified_campaign_cagr_pct(
        campaign=authority.campaign,
        discovery_plan=discovery_plan,
        evaluator_contract=evaluator_contract,
        policy_identity_sha256=authority.policy_revision.sha256,
    )
    return ParentCandidateV5(
        origin="baseline",
        policy_revision=authority.policy_revision,
        semantic_fingerprint_sha256=(authority.semantic_fingerprint.fingerprint_sha256),
        campaign_cagr_pct=score,
        source_bundle_ref=authority.source_bundle_ref,
        experiment_record_ref=None,
        primary_mechanism=None,
        admitted_round=None,
    )


def archive_parent_from_record_v5(
    *,
    entry: ArchiveEntryV5,
    stored_record: StoredExperimentRecordV5,
) -> ParentCandidateV5:
    """Resolve an archive parent through its checkpoint-authorized record."""

    if type(entry) is not ArchiveEntryV5 or type(stored_record) is not StoredExperimentRecordV5:
        raise ValueError("archive parent inputs are invalid")
    record = stored_record.record
    if (
        stored_record.reference != entry.experiment_record_ref
        or record.status != "evaluated"
        or record.policy_revision != entry.policy_revision
        or record.semantic_fingerprint is None
        or record.campaign_evidence != entry.campaign
        or record.hypothesis.primary_mechanism != entry.primary_mechanism
        or entry.source_bundle_ref not in record.artifact_refs
    ):
        raise ArchiveAuthorityMismatchV5()
    return ParentCandidateV5(
        origin="archive",
        policy_revision=entry.policy_revision,
        semantic_fingerprint_sha256=(record.semantic_fingerprint.fingerprint_sha256),
        campaign_cagr_pct=entry.campaign_cagr_pct,
        source_bundle_ref=entry.source_bundle_ref,
        experiment_record_ref=entry.experiment_record_ref,
        primary_mechanism=entry.primary_mechanism,
        admitted_round=entry.admitted_round,
    )


__all__ = [
    "ArchiveAdmissionOutcomeV5",
    "ArchiveAuthorityMismatchV5",
    "ArchiveCapacityInsufficientV5",
    "ArchiveEntryV5",
    "ArchiveFailureV5",
    "ArchiveRecordAuthorityV5",
    "ArchiveRevisionConflictV5",
    "ArchiveTransitionV5",
    "BaselineParentAuthorityV5",
    "CAMPAIGN_CAGR_QUANTUM_V5",
    "CandidateArchiveReducerV5",
    "CandidateArchiveV5",
    "CampaignAuthorityMismatch",
    "CampaignScenarioMismatch",
    "CampaignScoreMismatch",
    "CampaignScoringFailureV5",
    "DEFAULT_ARCHIVE_CAPACITY_V5",
    "IncompleteCampaignEvidence",
    "IncompleteCampaignEvidenceV5",
    "InvalidAnnualizationEvidence",
    "PRIMARY_MECHANISMS_V5",
    "PanelCommitmentMismatch",
    "PanelCommitmentMismatchV5",
    "ParentCandidateV5",
    "PrimaryMechanismV5",
    "SearchFailureV5",
    "SearchStateV5",
    "TARGET_GAP_QUANTUM_V5",
    "annualized_return_pct",
    "archive_parent_from_record_v5",
    "baseline_parent_candidate_v5",
    "campaign_cagr_pct",
    "expected_target_gap_pct_v5",
    "hypothesis_novelty_key_v5",
    "transition_archive_from_record_v5",
    "verified_campaign_cagr_pct",
]
