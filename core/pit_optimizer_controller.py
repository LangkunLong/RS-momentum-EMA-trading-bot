"""Provider-free orchestration for the schema-v3 PIT optimizer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
from typing import Callable, Mapping, Sequence
import uuid

from core.pit_optimization_contract import (
    AuthorArtifact,
    AuthorInput,
    AuthorManifestSummary,
    CandidateComparisonSummary,
    CandidateValidationSummary,
    CriticArtifact,
    CriticInput,
    DiscoveryEvidenceSummary,
    IncumbentSummary,
    InvestigatorArtifact,
    InvestigatorInput,
    IterationFeedbackSummary,
    PitOptimizerCallBudget,
    PitOptimizerGateConfig,
    PitOptimizerRunManifest,
    ProviderSeed,
    RuleSummaryRecord,
    StrategyRuleSummary,
    _CANDIDATE_COMPARISON_SEAL,
    _VALIDATION_FAILURE_FLAGS,
    _pit_optimizer_manifest_from_primitive,
    render_worst_iteration_two_role_inputs,
)
from core.pit_optimizer_artifacts import (
    CampaignCheckpoint,
    SearchCandidateState,
    canonical_json_bytes,
    write_create_only_json,
)
from core.pit_optimizer_authorization import (
    AuthorizationRunLease,
    OptimizerPricingSnapshot,
    PitOptimizerProviderFacts,
    PitOptimizerRoleCall,
)
from core.pit_optimizer_candidate import (
    CandidateIdentity,
    CandidateIdentityV4,
    build_policy_source_bundle,
    require_source_context_fit,
    validate_candidate_identity,
)
from core.pit_optimizer_evaluation import (
    AggregateMetric,
    DeterminismAttestation,
    DiscoveryEvaluation,
    DiscoveryScore,
    FoldAggregateSummary,
    HiddenEvaluation,
    HiddenEvaluationAttestation,
    HoldoutDecision,
    PitOptimizerCleanup,
    ValidationExposureMetadata,
    ValidationLedger,
    ValidationOutcomeProof,
    ValidationReservation,
    ValidationWindowIdentity,
    discovery_score_from_folds,
    strictly_improves_discovery,
)
from core.pit_policy_parity import (
    ParityAttestation,
    ParityEntryOutcome,
    ParityEquityPoint,
    ParityFoldEvidence,
    ParityTransaction,
)

from core.pit_optimization_contract import (
    AuthorArtifactV4,
    AuthorInputV4,
    AuthorManifestSummaryV4,
    AuthorSourceFile,
    CandidateValidationStatusV4,
    CriticArtifactV4,
    CriticInputV4,
    InvestigatorArtifactV4,
    InvestigatorInputV4,
    PitOptimizerRunManifestV4,
    PriorHypothesisSummaryV4,
    RoleContextBudgetExceeded,
    RoleOutputInvalidSummary,
    SelectedParentIdentity,
    SelectedParentSummary,
    TargetProgressV4,
)
from core.pit_optimizer_authorization import (
    AuthorizationPlanSkip,
    PitOptimizerRoleAttempt,
)
from core.pit_optimizer_evaluation import (
    AnnualizedReturnTarget,
    DiscoveryPanelPlan,
    EvaluationPanelSpec,
    PanelAggregateSummary,
    QualificationPanelPlan,
    QualificationRetirementSnapshot,
    _panel_plan_pair_is_consistent,
)


@dataclass(frozen=True, slots=True)
class PitOptimizerReadiness:
    schema_version: int
    manifest: PitOptimizerRunManifest
    manifest_sha256: str
    readiness_sha256: str
    artifact_path: Path
    parity: ParityAttestation
    baseline_discovery: DiscoveryEvidenceSummary
    provider_seed: ProviderSeed

    def __post_init__(self) -> None:
        if self.schema_version != 3:
            raise ValueError("optimizer readiness schema is unsupported")
        if not isinstance(self.manifest, PitOptimizerRunManifest):
            raise ValueError("optimizer readiness manifest is invalid")
        if self.manifest.sha256 != self.manifest_sha256:
            raise ValueError("optimizer readiness manifest digest differs")
        for value in (self.manifest_sha256, self.readiness_sha256):
            if not isinstance(value, str) or len(value) != 64:
                raise ValueError("optimizer readiness digest is invalid")
        if not isinstance(self.artifact_path, Path) or not self.artifact_path.is_absolute():
            raise ValueError("optimizer readiness artifact path is invalid")
        if not isinstance(self.parity, ParityAttestation):
            raise ValueError("optimizer readiness parity is invalid")
        if not isinstance(self.baseline_discovery, DiscoveryEvidenceSummary):
            raise ValueError("optimizer readiness discovery baseline is invalid")
        if not isinstance(self.provider_seed, ProviderSeed):
            raise ValueError("optimizer readiness provider seed is invalid")
        if self.provider_seed.baseline_discovery != self.baseline_discovery:
            raise ValueError("optimizer readiness provider baseline differs")


@dataclass(frozen=True, slots=True)
class CandidateWorkspace:
    workspace_id: str
    root: Path


@dataclass(frozen=True, slots=True)
class CandidateValidationOutcome:
    valid: bool
    failure_code: str | None
    incremental_diff: str
    cumulative_diff: str
    identity: CandidateIdentity | None
    changed_paths: tuple[str, ...]
    changed_symbols: tuple[str, ...]


def _canonical_decimal_text(value: Decimal) -> str:
    """Render one finite non-negative amount as canonical decimal text."""

    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError("optimizer USD amount is invalid")
    if value.is_zero():
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


@dataclass(frozen=True, slots=True)
class OptimizerBudgetSummary:
    api_calls: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    authoritative_usd: str
    projected_plan_usd: str | None
    pricing_status: str
    retained_reservation_tokens: int
    incomplete_accounting_calls: int
    accounting_complete: bool

    def __post_init__(self) -> None:
        for name in (
            "api_calls",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "retained_reservation_tokens",
            "incomplete_accounting_calls",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"optimizer budget {name} is invalid")
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError("optimizer authoritative token total is inconsistent")
        try:
            authoritative = Decimal(self.authoritative_usd)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("optimizer authoritative USD is invalid") from exc
        if (
            not isinstance(self.authoritative_usd, str)
            or _canonical_decimal_text(authoritative) != self.authoritative_usd
        ):
            raise ValueError("optimizer authoritative USD is invalid")
        if self.projected_plan_usd is not None:
            try:
                projected = Decimal(self.projected_plan_usd)
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValueError("optimizer projected plan USD is invalid") from exc
            if (
                not isinstance(self.projected_plan_usd, str)
                or _canonical_decimal_text(projected) != self.projected_plan_usd
            ):
                raise ValueError("optimizer projected plan USD is invalid")
        if self.pricing_status not in {
            "available",
            "unavailable",
            "not_initialized",
        }:
            raise ValueError("optimizer pricing status is invalid")
        if (self.pricing_status == "available") != (
            self.projected_plan_usd is not None
        ):
            raise ValueError("optimizer pricing projection is inconsistent")
        if self.incomplete_accounting_calls > self.api_calls:
            raise ValueError("optimizer incomplete accounting calls are invalid")
        if self.accounting_complete is not (
            self.incomplete_accounting_calls == 0
        ):
            raise ValueError("optimizer accounting completeness is inconsistent")
        if self.accounting_complete and self.retained_reservation_tokens:
            raise ValueError("complete optimizer accounting retained tokens")
        if (
            not self.accounting_complete
            and self.retained_reservation_tokens == 0
        ):
            raise ValueError("incomplete optimizer accounting lacks retained tokens")


@dataclass(frozen=True, slots=True)
class _ProviderAttemptRecord:
    """Closed facts for one authorized attempt, with a payload only on acceptance."""

    plan: PitOptimizerCallBudget
    facts: PitOptimizerProviderFacts
    payload_sha256: str | None


@dataclass(frozen=True, slots=True)
class PitOptimizerServices:
    freeze_pricing: Callable[[str], OptimizerPricingSnapshot]
    open_run_lease: Callable[
        [PitOptimizerReadiness, OptimizerPricingSnapshot],
        AuthorizationRunLease,
    ]
    close_run_lease: Callable[[AuthorizationRunLease, str], None]
    call_role: Callable[
        [
            PitOptimizerCallBudget,
            InvestigatorInput | AuthorInput | CriticInput,
            Callable[[str], object],
            AuthorizationRunLease,
            OptimizerPricingSnapshot,
        ],
        PitOptimizerRoleCall,
    ]
    recover_role_attempt: Callable[
        [PitOptimizerCallBudget, AuthorizationRunLease],
        PitOptimizerProviderFacts,
    ]
    create_candidate: Callable[[str | None], CandidateWorkspace]
    validate_and_apply: Callable[
        [CandidateWorkspace, AuthorArtifact, str | None], CandidateValidationOutcome
    ]
    evaluate_discovery: Callable[[CandidateWorkspace, CandidateIdentity], DiscoveryEvaluation]
    confirm_discovery: Callable[
        [CandidateWorkspace, CandidateIdentity, str], DeterminismAttestation
    ]
    reserve_hidden_validation: Callable[[CandidateIdentity], ValidationReservation]
    evaluate_hidden: Callable[
        [CandidateWorkspace, CandidateIdentity, ValidationReservation],
        HiddenEvaluationAttestation,
    ]
    record_hidden_outcome: Callable[
        [ValidationReservation, bool, bool, str | None],
        ValidationOutcomeProof,
    ]
    dispose_candidate: Callable[[CandidateWorkspace], PitOptimizerCleanup]
    verify_inputs: Callable[[PitOptimizerReadiness], None]
    cancellation_requested: Callable[[], bool]
    prepare_iteration_artifacts: Callable[[int], Path]
    write_json_artifact: Callable[[str, Mapping[str, object]], tuple[Path, str]]
    write_diff_artifact: Callable[[str, str], tuple[Path, str]]


@dataclass(frozen=True, slots=True)
class PitOptimizerResult:
    schema_version: int
    phase: str
    status: str
    terminal_code: str
    terminal_detail: str | None
    exit_code: int
    run_id: str
    readiness_sha256: str
    manifest_sha256: str
    iterations_started: int
    iterations_completed: int
    valid_evaluations: int
    incumbent_updates: int
    non_improving_streak: int
    discovery_winner: CandidateIdentity | None
    hidden_validation_opened: bool
    validation_reservation_sha256: str | None
    long_replay_eligible: bool | None
    budget: OptimizerBudgetSummary
    artifact_root: Path
    artifact_paths: tuple[tuple[Path, str], ...]
    source_modified: bool
    cleanup_complete: bool

    def to_public_artifact(self) -> Mapping[str, object]:
        root = self.artifact_root.resolve(strict=False)
        artifact_digests: list[dict[str, str]] = []
        for path, digest in self.artifact_paths:
            resolved = path.resolve(strict=False)
            try:
                relative = resolved.relative_to(root)
            except ValueError as exc:
                raise ValueError("optimizer artifact escaped its run root") from exc
            # summary.json cannot contain its own digest.  It is still retained in
            # ``artifact_paths`` on the returned runtime result.
            if relative.as_posix() == "summary.json":
                continue
            artifact_digests.append(
                {"path": relative.as_posix(), "sha256": digest}
            )
        winner = self.discovery_winner
        return {
            "schema_version": self.schema_version,
            "phase": self.phase,
            "status": self.status,
            "terminal": {
                "code": self.terminal_code,
                "detail": self.terminal_detail,
                "exit_code": self.exit_code,
            },
            "run_id": self.run_id,
            "readiness_sha256": self.readiness_sha256,
            "manifest_sha256": self.manifest_sha256,
            "iterations": {
                "started": self.iterations_started,
                "completed": self.iterations_completed,
                "valid_evaluations": self.valid_evaluations,
                "incumbent_updates": self.incumbent_updates,
                "non_improving_streak": self.non_improving_streak,
            },
            "incumbent": None if winner is None else winner.to_primitive(),
            "discovery_outcome": {
                "winner_identity_sha256": (
                    None if winner is None else winner.identity_sha256
                ),
            },
            "hidden_outcome": {
                "opened": self.hidden_validation_opened,
                "validation_reservation_sha256": (
                    self.validation_reservation_sha256
                ),
                "long_replay_eligible": self.long_replay_eligible,
            },
            "accounting": asdict(self.budget),
            "cleanup": {
                "complete": self.cleanup_complete,
                "source_modified": self.source_modified,
            },
            "artifact_digests": artifact_digests,
        }


@dataclass(slots=True)
class _RunState:
    run_id: str
    next_iteration: int
    incumbent_workspace: CandidateWorkspace | None
    incumbent_identity: CandidateIdentity | None
    incumbent_cumulative_diff: str
    incumbent_discovery: DiscoveryEvidenceSummary
    prior_iterations: tuple[IterationFeedbackSummary, ...]
    valid_evaluations: int
    incumbent_updates: int
    non_improving_streak: int
    provider_enabled: bool
    pricing_snapshot: OptimizerPricingSnapshot | None
    authorization_lease: AuthorizationRunLease | None
    baseline_quick: PanelAggregateSummary | None = None
    baseline_discovery_v4: PanelAggregateSummary | None = None
    champion: SearchCandidateState | None = None
    active_branch: SearchCandidateState | None = None
    selected_parent: SelectedParentIdentity | None = None
    call_attempts: list[PitOptimizerRoleAttempt] = field(default_factory=list)
    skips: list[AuthorizationPlanSkip] = field(default_factory=list)
    feedback_tail: list[PriorHypothesisSummaryV4] = field(default_factory=list)
    artifact_paths: list[tuple[Path, str]] = field(default_factory=list)
    artifact_root: Path | None = None
    call_records: list[PitOptimizerRoleCall] = field(default_factory=list)
    provider_attempts: list[_ProviderAttemptRecord] = field(default_factory=list)
    iteration_workspace: CandidateWorkspace | None = None
    iteration_source_bundle: object | None = None
    prospective_source_bundle: object | None = None
    last_investigator: PitOptimizerRoleCall | None = None
    last_author: PitOptimizerRoleCall | None = None
    iterations_started: int = 0
    iterations_completed: int = 0
    terminal_detail: str | None = None
    lease_snapshot: AuthorizationRunLease | None = None
    hidden_validation_opened: bool = False
    validation_reservation: ValidationReservation | None = None
    hidden_evaluation: HiddenEvaluation | None = None
    hidden_attestation: HiddenEvaluationAttestation | None = None
    validation_outcome_proof: ValidationOutcomeProof | None = None
    evaluation_failure_code: str | None = None
    cleanup_observations: list[PitOptimizerCleanup] = field(default_factory=list)
    pending_retired_workspaces: list[CandidateWorkspace] = field(default_factory=list)
    finalization_failures: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _IterationOutcome:
    completed: bool
    terminal_code: str | None
    feedback: IterationFeedbackSummary | None
    candidate_workspace: CandidateWorkspace | None
    candidate_identity: CandidateIdentity | None
    discovery: DiscoveryEvaluation | None
    incumbent_changed: bool


class ProviderProtocolFailure(RuntimeError):
    pass


class ProviderCallNotStarted(RuntimeError):
    """A locally rejected role input that never reached provider reservation."""


class ProviderAccountingFailure(RuntimeError):
    pass


class AuditFailure(RuntimeError):
    pass


class AuthorizationExhausted(RuntimeError):
    pass


class IdentityDrift(RuntimeError):
    pass


class SandboxIntegrityFailure(RuntimeError):
    pass


class CandidateEvaluationFailure(RuntimeError):
    """A candidate-scoped discovery failure that remains safe for critic feedback."""

    def __init__(self, failure_code: str) -> None:
        if failure_code not in {
            "allocation_constraints_failed",
            "evaluation_failed",
            "replay_failed",
            "worker_failed",
        }:
            raise ValueError("candidate evaluation failure code is invalid")
        super().__init__(failure_code)
        self.failure_code = failure_code


class EvidenceTampering(RuntimeError):
    pass


class TrustedEvaluatorNondeterminism(RuntimeError):
    pass


class ContextBudgetExhausted(RuntimeError):
    pass


OptimizerTerminalCode = frozenset(
    {
        "iteration_limit",
        "budget_exhausted",
        "stagnation_limit",
        "cancelled",
        "provider_protocol_failure",
        "provider_accounting_failure",
        "audit_failure",
        "authorization_exhausted",
        "identity_drift",
        "trusted_evaluator_nondeterminism",
        "sandbox_integrity_failure",
        "evidence_tampering",
    }
)


class _CandidateCapabilityRegistry:
    """Keep live Candidate authority private and address it only by opaque ID."""

    def __init__(
        self,
        *,
        create_capability: Callable[[str | None], object],
        validate_capability: Callable[
            [object, AuthorArtifact, str | None], CandidateValidationOutcome
        ],
        dispose_capability: Callable[[object], PitOptimizerCleanup],
    ) -> None:
        self._create_capability = create_capability
        self._validate_capability = validate_capability
        self._dispose_capability = dispose_capability
        self._validation_entries: dict[
            str,
            tuple[CandidateWorkspace, object],
        ] = {}
        self._cleanup_entries: dict[
            str,
            tuple[CandidateWorkspace, object],
        ] = {}

    def create_candidate(self, cumulative_diff: str | None) -> CandidateWorkspace:
        capability = self._create_capability(cumulative_diff)
        root = getattr(capability, "root", None)
        if not isinstance(root, Path) or not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise RuntimeError("candidate capability root is invalid")
        workspace = CandidateWorkspace(
            workspace_id=f"workspace_{uuid.uuid4().hex}",
            root=root.resolve(),
        )
        entry = (workspace, capability)
        self._validation_entries[workspace.workspace_id] = entry
        self._cleanup_entries[workspace.workspace_id] = entry
        return workspace

    @staticmethod
    def _resolve(
        entries: Mapping[str, tuple[CandidateWorkspace, object]],
        workspace: CandidateWorkspace,
    ) -> object:
        if not isinstance(workspace, CandidateWorkspace):
            raise RuntimeError("unknown candidate workspace")
        entry = entries.get(workspace.workspace_id)
        if entry is None or entry[0] != workspace:
            raise RuntimeError("unknown candidate workspace")
        return entry[1]

    def validate_and_apply(
        self,
        workspace: CandidateWorkspace,
        artifact: AuthorArtifact,
        cumulative_diff: str | None,
    ) -> CandidateValidationOutcome:
        capability = self._resolve(self._validation_entries, workspace)
        return self._validate_capability(
            capability,
            artifact,
            cumulative_diff,
        )

    def dispose_candidate(self, workspace: CandidateWorkspace) -> PitOptimizerCleanup:
        capability = self._resolve(self._cleanup_entries, workspace)
        # Cleanup begins with one-way validation revocation.  The internal
        # cleanup-only capability remains reachable until disposal is complete.
        self._validation_entries.pop(workspace.workspace_id, None)
        cleanup = self._dispose_capability(capability)
        if not isinstance(cleanup, PitOptimizerCleanup):
            raise RuntimeError("candidate cleanup result is invalid")
        if cleanup.candidate_removed and cleanup.worker_stopped:
            self._cleanup_entries.pop(workspace.workspace_id)
        return cleanup


def _parity_primitive(parity: ParityAttestation) -> dict[str, object]:
    value = asdict(parity)
    value.pop("artifact_path")
    return value


def _readiness_primitive(
    *,
    manifest: PitOptimizerRunManifest,
    parity: ParityAttestation,
    baseline: DiscoveryEvidenceSummary,
    provider_seed: ProviderSeed,
) -> dict[str, object]:
    return {
        "schema_version": 3,
        "manifest": manifest.to_primitive(),
        "manifest_sha256": manifest.sha256,
        "parity": _parity_primitive(parity),
        "baseline_discovery": baseline.to_primitive(),
        "provider_seed": provider_seed.to_primitive(),
    }


def _aggregate_from_primitive(value: object) -> FoldAggregateSummary:
    if not isinstance(value, dict):
        raise ValueError("parity aggregate is invalid")
    primitive = dict(value)
    try:
        primitive["entry_funnel"] = tuple(
            AggregateMetric(**item) for item in primitive["entry_funnel"]
        )
        primitive["exit_attribution"] = tuple(
            AggregateMetric(**item) for item in primitive["exit_attribution"]
        )
        return FoldAggregateSummary(**primitive)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("parity aggregate is invalid") from exc


def _evidence_from_primitive(value: object) -> ParityFoldEvidence:
    if not isinstance(value, dict):
        raise ValueError("parity evidence is invalid")
    primitive = dict(value)
    try:
        primitive["transactions"] = tuple(
            ParityTransaction(**item) for item in primitive["transactions"]
        )
        primitive["entry_outcomes"] = tuple(
            ParityEntryOutcome(**item) for item in primitive["entry_outcomes"]
        )
        primitive["equity"] = tuple(
            ParityEquityPoint(**item) for item in primitive["equity"]
        )
        primitive["funnel"] = tuple(
            AggregateMetric(**item) for item in primitive["funnel"]
        )
        primitive["aggregate"] = _aggregate_from_primitive(primitive["aggregate"])
        return ParityFoldEvidence(**primitive)
    except (KeyError, TypeError, ValueError) as exc:
        if "evidence digest" in str(exc):
            raise ValueError("parity evidence digest mismatch") from exc
        raise ValueError("parity evidence is invalid") from exc


def _load_manifest(path: Path) -> PitOptimizerRunManifest:
    raw = Path(path).read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("optimizer manifest is invalid JSON") from exc
    if not isinstance(value, dict) or raw != canonical_json_bytes(value):
        raise ValueError("optimizer manifest is not canonical JSON")
    return _pit_optimizer_manifest_from_primitive(value)


def _load_parity(path: Path) -> ParityAttestation:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError("verified parity artifact must be a regular non-link file")
    resolved = candidate.resolve()
    raw = resolved.read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("verified parity artifact is invalid JSON") from exc
    expected_keys = {
        field.name
        for field in fields(ParityAttestation)
        if field.name not in {"artifact_path", "artifact_sha256"}
    }
    if not isinstance(value, dict) or set(value) != expected_keys or raw != canonical_json_bytes(value):
        raise ValueError("verified parity artifact is not canonical")
    primitive = dict(value)
    try:
        primitive["reference_output_sha256s"] = tuple(
            tuple(item) for item in primitive["reference_output_sha256s"]
        )
        primitive["final_output_sha256s"] = tuple(
            tuple(item) for item in primitive["final_output_sha256s"]
        )
        primitive["final_discovery_evidence"] = tuple(
            _evidence_from_primitive(item)
            for item in primitive["final_discovery_evidence"]
        )
        return ParityAttestation(
            **primitive,
            artifact_path=resolved,
            artifact_sha256=hashlib.sha256(raw).hexdigest(),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if "evidence digest" in str(exc):
            raise
        raise ValueError("verified parity closed contract is invalid") from exc


def _validate_parity_graph(
    parity: ParityAttestation,
    manifest: PitOptimizerRunManifest,
) -> None:
    evidence = parity.final_discovery_evidence
    expected_fold_ids = tuple(item.fold_id for item in manifest.fold_manifest.discovery_folds)
    expected_outputs = tuple((item.fold_id, item.evidence_sha256) for item in evidence)
    if (
        len(evidence) != 2
        or tuple(item.fold_id for item in evidence) != expected_fold_ids
        or parity.final_output_sha256s != expected_outputs
        or parity.reference_output_sha256s != expected_outputs
        or any(item.effective_policy_sha256 != manifest.effective_policy_sha256 for item in evidence)
    ):
        raise ValueError("verified parity discovery evidence graph differs")
    for fold, item in zip(manifest.fold_manifest.discovery_folds, evidence, strict=True):
        if (
            item.aggregate.fold_id != fold.fold_id
            or item.funnel != item.aggregate.entry_funnel
            or tuple(point.session for point in item.equity) != fold.sessions
        ):
            raise ValueError("verified parity nested evidence differs from fold manifest")
        sessions = frozenset(fold.sessions)
        if any(value.date not in sessions for value in item.transactions) or any(
            value.signal_date not in sessions or value.entry_date not in sessions
            for value in item.entry_outcomes
        ):
            raise ValueError("verified parity evidence lies outside its discovery fold")
    if (
        parity.artifact_sha256 != manifest.parity_attestation_sha256
        or parity.final_source_head != manifest.source_head
        or parity.final_source_fingerprint_sha256 != manifest.source_fingerprint_sha256
        or parity.pit_bundle_sha256 != manifest.pit_bundle_sha256
        or parity.baseline_manifest_sha256 != manifest.baseline_manifest_sha256
        or parity.effective_policy_sha256 != manifest.effective_policy_sha256
        or parity.discovery_fold_manifest_sha256 != manifest.fold_manifest.sha256
        or parity.policy_interface_version != manifest.policy_interface_version
    ):
        raise ValueError("verified parity differs from optimizer manifest")


def _baseline_from_parity(parity: ParityAttestation) -> DiscoveryEvidenceSummary:
    evidence = parity.final_discovery_evidence
    return DiscoveryEvidenceSummary(
        folds=tuple(item.aggregate for item in evidence),
        score=None,
        evidence_ids=tuple(item.evidence_sha256 for item in evidence),
    )


def _provider_seed_from_baseline(
    baseline: DiscoveryEvidenceSummary,
) -> ProviderSeed:
    entry_ids = sorted(
        {metric.metric_id for fold in baseline.folds for metric in fold.entry_funnel}
    )
    exit_ids = sorted(
        {metric.metric_id for fold in baseline.folds for metric in fold.exit_attribution}
    )
    summary = StrategyRuleSummary(
        records=(
            RuleSummaryRecord(
                "attested.discovery_objective",
                "Compare candidates only with the authenticated fixed discovery aggregates.",
            ),
            RuleSummaryRecord(
                "attested.entry_funnel",
                "Attested discovery entry metrics: " + ", ".join(entry_ids) + ".",
            ),
            RuleSummaryRecord(
                "attested.entry_blockers",
                "technical_block_* counts are precomputed technical gates; entry_block_* counts are final policy blockers.",
            ),
            RuleSummaryRecord(
                "attested.entry_intersection",
                "A buy requires every retained blocker to clear; relax blockers selectively from raw EntrySnapshot facts.",
            ),
            RuleSummaryRecord(
                "attested.market_gate",
                "When market_pass equals evaluated_rows, market permission is not the entry bottleneck.",
            ),
            RuleSummaryRecord(
                "attested.exit_attribution",
                "Attested discovery exit metrics: " + ", ".join(exit_ids) + ".",
            ),
        )
    )
    return ProviderSeed(rule_summary=summary, baseline_discovery=baseline)


def _window_identity(
    manifest: PitOptimizerRunManifest,
    fold_index: int,
) -> ValidationWindowIdentity:
    folds = (
        *manifest.fold_manifest.discovery_folds,
        manifest.fold_manifest.hidden_fold,
    )
    fold = folds[fold_index]
    warmup_sha256 = hashlib.sha256(
        canonical_json_bytes(
            {
                "warmup_start_date": manifest.fold_manifest.warmup_start_date,
                "policy_interface_version": manifest.policy_interface_version,
            }
        )
    ).hexdigest()
    sessions_sha256 = hashlib.sha256(
        canonical_json_bytes({"sessions": list(fold.sessions)})
    ).hexdigest()
    # ValidationLedger uses the digest of the bare session list.
    sessions_sha256 = hashlib.sha256(
        (
            json.dumps(
                list(fold.sessions),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
    ).hexdigest()
    return ValidationWindowIdentity(
        pit_bundle_sha256=manifest.pit_bundle_sha256,
        universe_sha256=manifest.fold_manifest.universe_sha256,
        benchmark=manifest.fold_manifest.benchmark,
        warmup_contract_sha256=warmup_sha256,
        sessions_sha256=sessions_sha256,
        session_count=len(fold.sessions),
        first_session=fold.start_date,
        last_session=fold.end_date,
    )


def prepare_pit_optimizer_v3(
    config: PitOptimizerGateConfig,
    *,
    source_root: Path,
    artifact_root: Path,
    permanent_runtime_root: Path,
    source_head: str,
    source_fingerprint_sha256: str,
    source_identity: Callable[[Path], tuple[str, str]],
) -> PitOptimizerReadiness:
    """Authenticate and persist provider-projectable inputs without provider effects."""

    if not isinstance(config, PitOptimizerGateConfig) or config.phase != "prepare":
        raise ValueError("optimizer prepare requires a prepare gate")
    config.validate()
    source = Path(source_root)
    artifacts = Path(artifact_root)
    runtime = Path(permanent_runtime_root)
    for path, label in ((artifacts, "artifact root"), (runtime, "permanent runtime root")):
        if not path.is_absolute() or path.is_symlink() or not path.is_dir():
            raise ValueError(f"optimizer {label} is invalid")
    if not callable(source_identity):
        raise ValueError("optimizer source identity capability is invalid")
    actual_source_head, actual_source_fingerprint = source_identity(source)
    if (source_head, source_fingerprint_sha256) != (
        actual_source_head,
        actual_source_fingerprint,
    ):
        raise ValueError("optimizer supplied source identity differs from clean source")
    manifest = _load_manifest(config.optimizer_manifest)
    parity = _load_parity(config.verified_parity_artifact)
    if manifest.sha256 != config.optimizer_manifest_sha256:
        raise ValueError("optimizer manifest digest differs")
    if (manifest.source_head, manifest.source_fingerprint_sha256) != (
        actual_source_head,
        actual_source_fingerprint,
    ):
        raise ValueError("optimizer manifest source identity differs")
    _validate_parity_graph(parity, manifest)

    metadata = ValidationExposureMetadata(
        run_id=manifest.run_id,
        source_head=manifest.source_head,
        baseline_policy_sha256=manifest.effective_policy_sha256,
        candidate_identity_sha256=None,
        exposure_kind="provider_context",
    )
    ledger = ValidationLedger(runtime / manifest.validation_ledger_name)
    reservations = tuple(
        ledger.mark_discovery(_window_identity(manifest, index), metadata)
        for index in range(2)
    )
    ledger.seal_discovery_folds(manifest.fold_manifest, reservations)

    baseline = _baseline_from_parity(parity)
    provider_seed = _provider_seed_from_baseline(baseline)
    primitive = _readiness_primitive(
        manifest=manifest,
        parity=parity,
        baseline=baseline,
        provider_seed=provider_seed,
    )
    output = artifacts.resolve() / f"{manifest.run_id}.readiness.json"
    artifact_path, readiness_sha256 = write_create_only_json(output, primitive)
    return PitOptimizerReadiness(
        schema_version=3,
        manifest=manifest,
        manifest_sha256=manifest.sha256,
        readiness_sha256=readiness_sha256,
        artifact_path=artifact_path,
        parity=parity,
        baseline_discovery=baseline,
        provider_seed=provider_seed,
    )


def _initialize_provider(
    readiness: PitOptimizerReadiness,
    state: _RunState,
    services: PitOptimizerServices,
) -> None:
    pricing = services.freeze_pricing(readiness.manifest.model)
    lease = services.open_run_lease(readiness, pricing)
    state.pricing_snapshot = pricing
    state.authorization_lease = lease
    state.lease_snapshot = lease


def _record_artifact(
    state: _RunState,
    artifact: tuple[Path, str],
) -> None:
    path, digest = artifact
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError("optimizer artifact path is invalid")
    if not isinstance(digest, str) or len(digest) != 64:
        raise ValueError("optimizer artifact digest is invalid")
    root = path.parent if path.name in {"run.json", "baseline.json", "accounting.json"} else None
    if state.artifact_root is None and root is not None:
        state.artifact_root = root
    if state.artifact_root is not None and state.artifact_root not in (path.parent, *path.parents):
        raise ValueError("optimizer artifact escaped its run root")
    state.artifact_paths[:] = [
        item for item in state.artifact_paths if item[0] != path
    ]
    state.artifact_paths.append((path, digest))


def _run_artifact(
    readiness: PitOptimizerReadiness,
    state: _RunState,
) -> dict[str, object]:
    manifest = readiness.manifest
    if state.authorization_lease is None or state.pricing_snapshot is None:
        raise RuntimeError("provider capability is not initialized")
    folds = (*manifest.fold_manifest.discovery_folds, manifest.fold_manifest.hidden_fold)
    return {
        "schema_version": 3,
        "run_id": manifest.run_id,
        "manifest_sha256": readiness.manifest_sha256,
        "readiness_sha256": readiness.readiness_sha256,
        "source": {
            "head": manifest.source_head,
            "fingerprint_sha256": manifest.source_fingerprint_sha256,
            "policy_source_sha256s": [list(item) for item in manifest.policy_source_sha256s],
            "policy_source_scope_sha256": manifest.policy_source_scope.sha256,
            "immutable_constraints_sha256": manifest.immutable_constraints_sha256,
        },
        "pit_bundle_sha256": manifest.pit_bundle_sha256,
        "baseline_manifest_sha256": manifest.baseline_manifest_sha256,
        "effective_policy_sha256": manifest.effective_policy_sha256,
        "parity_attestation_sha256": manifest.parity_attestation_sha256,
        "fold_manifest_sha256": manifest.fold_manifest.sha256,
        "fold_identities": [
            {
                "fold_id": fold.fold_id,
                "purpose": fold.purpose,
                "sessions_sha256": hashlib.sha256(
                    (
                        json.dumps(
                            list(fold.sessions),
                            sort_keys=True,
                            separators=(",", ":"),
                            allow_nan=False,
                        ).encode("utf-8")
                        + b"\n"
                    )
                ).hexdigest(),
            }
            for fold in folds
        ],
        "policy_interface_version": manifest.policy_interface_version,
        "candidate_bounds": manifest.candidate_bounds.to_primitive(),
        "authorization": {
            "requirement_sha256": manifest.authorization_requirement.sha256,
            "window_id": manifest.authorization_requirement.window_id,
            "lease_id": state.authorization_lease.lease_id,
        },
        "pricing_snapshot_sha256": (
            state.pricing_snapshot.pricing_payload_sha256
        ),
        "status": "initialized",
    }


def _baseline_artifact(readiness: PitOptimizerReadiness) -> dict[str, object]:
    manifest = readiness.manifest
    return {
        "schema_version": 3,
        "fold_ids": [item.fold_id for item in readiness.baseline_discovery.folds],
        "fold_aggregates": [
            item.to_primitive() if hasattr(item, "to_primitive") else asdict(item)
            for item in readiness.baseline_discovery.folds
        ],
        "evidence_ids": list(readiness.baseline_discovery.evidence_ids),
        "universe_sha256": manifest.fold_manifest.universe_sha256,
        "warmup_start_date": manifest.fold_manifest.warmup_start_date,
        "engine_policy_sha256": manifest.effective_policy_sha256,
        "parity_attestation_sha256": manifest.parity_attestation_sha256,
    }


def _accounting_artifact(
    state: _RunState,
) -> dict[str, object]:
    return _current_accounting_artifact(state)


def _initialize_run_artifacts(
    readiness: PitOptimizerReadiness,
    state: _RunState,
    services: PitOptimizerServices,
) -> None:
    """Acquire one lease, then durably establish the three root run artifacts."""

    _initialize_provider(readiness, state, services)
    try:
        for name, primitive in (
            ("run.json", _run_artifact(readiness, state)),
            ("baseline.json", _baseline_artifact(readiness)),
            ("accounting.json", _accounting_artifact(state)),
        ):
            _record_artifact(
                state,
                services.write_json_artifact(name, primitive),
            )
    except BaseException:
        state.provider_enabled = False
        lease = state.authorization_lease
        if lease is not None:
            try:
                services.close_run_lease(lease, "audit_failure")
            finally:
                state.authorization_lease = None
        raise


def _iteration_name(state: _RunState, filename: str) -> str:
    return f"iterations/{state.next_iteration:03d}/{filename}"


def _plan_for(
    readiness: PitOptimizerReadiness,
    state: _RunState,
    role: str,
) -> PitOptimizerCallBudget:
    match = next(
        (
            item
            for item in readiness.manifest.call_budgets
            if item.iteration == state.next_iteration and item.role == role
        ),
        None,
    )
    if match is None:
        raise AuthorizationExhausted("authorization call plan is exhausted")
    return match


def _payload_sha256(payload: object) -> str:
    canonical = getattr(payload, "canonical_json_bytes", None)
    if not callable(canonical):
        raise ProviderProtocolFailure("provider payload is not a closed role artifact")
    return hashlib.sha256(canonical()).hexdigest()


def _role_artifact(call: PitOptimizerRoleCall) -> dict[str, object]:
    payload = call.payload
    primitive = getattr(payload, "to_primitive", None)
    if not callable(primitive):
        raise ProviderProtocolFailure("provider payload is not serializable")
    return {
        "schema_version": 3,
        "call_index": call.plan.call_index,
        "iteration": call.plan.iteration,
        "role": call.plan.role,
        "payload_sha256": _payload_sha256(payload),
        "payload": primitive(),
    }


def _budget_summary_from_calls(
    calls: Sequence[PitOptimizerRoleCall | _ProviderAttemptRecord],
    lease: AuthorizationRunLease | None,
) -> OptimizerBudgetSummary:
    if lease is not None and not isinstance(lease, AuthorizationRunLease):
        raise ValueError("optimizer accounting lease is invalid")
    facts = [item.facts for item in calls]
    authoritative_usd = sum(
        (
            Decimal(str(item.cost_usd))
            for item in facts
            if item.accounting_complete and item.cost_usd is not None
        ),
        Decimal("0"),
    )
    incomplete_calls = sum(1 for item in facts if not item.accounting_complete)
    return OptimizerBudgetSummary(
        api_calls=sum(1 for item in facts if item.request_started),
        prompt_tokens=sum(item.prompt_tokens or 0 for item in facts),
        completion_tokens=sum(item.completion_tokens or 0 for item in facts),
        total_tokens=sum(item.total_tokens or 0 for item in facts),
        authoritative_usd=_canonical_decimal_text(authoritative_usd),
        projected_plan_usd=(
            None if lease is None else lease.projected_plan_usd
        ),
        pricing_status=(
            "not_initialized" if lease is None else lease.pricing_status
        ),
        retained_reservation_tokens=sum(item.retained_reservation_tokens for item in facts),
        incomplete_accounting_calls=incomplete_calls,
        accounting_complete=incomplete_calls == 0,
    )


def _accounting_facts_primitive(
    facts: PitOptimizerProviderFacts,
) -> dict[str, object]:
    """Return provider accounting facts without raw evidence identities."""

    primitive = asdict(facts)
    primitive.pop("pricing_snapshot_sha256")
    primitive.pop("audit_sha256")
    return primitive


def _current_accounting_artifact(state: _RunState) -> dict[str, object]:
    attempts = state.provider_attempts
    lease = state.authorization_lease or state.lease_snapshot
    summary = _budget_summary_from_calls(attempts, lease)
    reserved_tokens = sum(
        item.plan.max_input_tokens + item.plan.max_output_tokens
        for item in attempts
    )
    return {
        "schema_version": 3,
        "call_records": [
            {
                "plan": item.plan.to_primitive(),
                "facts": _accounting_facts_primitive(item.facts),
            }
            for item in attempts
        ],
        "authorized_totals": {
            "calls": 0 if lease is None else lease.max_calls,
            "tokens": 0 if lease is None else lease.max_tokens,
        },
        "reserved_totals": {
            "calls": len(attempts),
            "tokens": reserved_tokens,
        },
        "pricing_advisory": {
            "status": summary.pricing_status,
            "projected_plan_usd": summary.projected_plan_usd,
        },
        "actual_totals": {
            "calls": summary.api_calls,
            "prompt_tokens": summary.prompt_tokens,
            "completion_tokens": summary.completion_tokens,
            "total_tokens": summary.total_tokens,
            "usd": summary.authoritative_usd,
        },
        "incomplete_exposure": {
            "calls": summary.incomplete_accounting_calls,
            "retained_tokens": summary.retained_reservation_tokens,
        },
        "accounting_complete": summary.accounting_complete,
    }


def _validate_attempt_facts(
    state: _RunState,
    plan: PitOptimizerCallBudget,
    facts: PitOptimizerProviderFacts,
) -> None:
    if not isinstance(facts, PitOptimizerProviderFacts):
        raise AuditFailure("provider attempt recovery did not return closed facts")
    if (
        facts.call_index,
        facts.iteration,
        facts.role,
        facts.requested_model,
    ) != (plan.call_index, plan.iteration, plan.role, plan.model):
        raise AuditFailure("provider attempt facts differ from the authorized plan")
    pricing = state.pricing_snapshot
    if (
        pricing is None
        or facts.pricing_snapshot_sha256 != pricing.pricing_payload_sha256
    ):
        raise AuditFailure("provider attempt facts differ from pricing snapshot")


def _record_provider_attempt(
    state: _RunState,
    services: PitOptimizerServices,
    plan: PitOptimizerCallBudget,
    facts: PitOptimizerProviderFacts,
    *,
    payload_sha256: str | None,
) -> None:
    _validate_attempt_facts(state, plan, facts)
    if any(item.plan.call_index == plan.call_index for item in state.provider_attempts):
        raise AuditFailure("provider attempt was recorded more than once")
    state.provider_attempts.append(_ProviderAttemptRecord(plan, facts, payload_sha256))
    _replace_accounting_artifact(state, services)


def _raise_failed_attempt(facts: PitOptimizerProviderFacts) -> None:
    if not facts.accounting_complete:
        raise ProviderAccountingFailure("provider accounting is incomplete")
    if facts.outcome == "budget_exceeded":
        raise AuthorizationExhausted("provider exceeded the authorized call budget")
    raise ProviderProtocolFailure("provider role attempt failed closed validation")


def _replace_accounting_artifact(
    state: _RunState,
    services: PitOptimizerServices,
) -> None:
    _record_artifact(
        state,
        services.write_json_artifact(
            "accounting.json",
            _current_accounting_artifact(state),
        ),
    )


def _call_role(
    readiness: PitOptimizerReadiness,
    state: _RunState,
    services: PitOptimizerServices,
    plan: PitOptimizerCallBudget,
    role_input: InvestigatorInput | AuthorInput | CriticInput,
    parser: Callable[[str], object],
) -> PitOptimizerRoleCall:
    if not state.provider_enabled:
        raise RuntimeError("provider capability is closed")
    if state.authorization_lease is None or state.pricing_snapshot is None:
        raise RuntimeError("provider capability is not initialized")
    try:
        require_source_context_fit(role_input=role_input, role_budget=plan)
    except ValueError as exc:
        raise ContextBudgetExhausted("context_budget_exhausted") from exc
    try:
        call = services.call_role(
            plan,
            role_input,
            parser,
            state.authorization_lease,
            state.pricing_snapshot,
        )
    except ProviderCallNotStarted as exc:
        raise ProviderProtocolFailure(
            "provider role input failed local provenance validation"
        ) from exc
    except BaseException:
        facts = services.recover_role_attempt(plan, state.authorization_lease)
        _record_provider_attempt(
            state,
            services,
            plan,
            facts,
            payload_sha256=None,
        )
        _raise_failed_attempt(facts)
    if not isinstance(call, PitOptimizerRoleCall) or call.plan != plan:
        raise ProviderProtocolFailure("provider returned a mismatched role call")
    try:
        validator = getattr(role_input, "validate_artifact", None)
        if callable(validator):
            validator(call.payload)
        elif not isinstance(call.payload, InvestigatorArtifact):
            raise ValueError("investigator payload type differs")
    except (AttributeError, TypeError, ValueError) as exc:
        raise ProviderProtocolFailure("provider role artifact failed closed validation") from exc
    state.call_records.append(call)
    _record_provider_attempt(
        state,
        services,
        plan,
        call.facts,
        payload_sha256=_payload_sha256(call.payload),
    )
    return call


def _incumbent_summary(state: _RunState) -> IncumbentSummary:
    accepted_iteration = None
    behavioral_summary = "Authenticated fixed baseline."
    if state.incumbent_identity is not None:
        accepted = next(
            (item for item in reversed(state.prior_iterations) if item.incumbent_changed),
            None,
        )
        accepted_iteration = None if accepted is None else accepted.iteration
        behavioral_summary = (
            "Validated discovery incumbent."
            if accepted is None
            else accepted.author_summary
        )
    return IncumbentSummary(
        candidate_identity_sha256=(
            None if state.incumbent_identity is None else state.incumbent_identity.identity_sha256
        ),
        accepted_iteration=accepted_iteration,
        behavioral_summary=behavioral_summary,
        discovery=state.incumbent_discovery,
    )


def _require_complete_iteration_context(
    readiness: PitOptimizerReadiness,
    source_bundle: object,
) -> None:
    """Prove all three worst-case role inputs fit before any iteration call."""

    files = getattr(source_bundle, "files", None)
    if type(files) is not tuple:
        raise IdentityDrift("iteration source bundle is not closed")
    source_texts = {
        getattr(item, "path", ""): getattr(item, "text", None) for item in files
    }
    if any(not isinstance(value, str) for value in source_texts.values()):
        raise IdentityDrift("iteration source bundle text is invalid")
    try:
        render_worst_iteration_two_role_inputs(
            scope=readiness.manifest.policy_source_scope,
            source_texts=source_texts,  # type: ignore[arg-type]
            immutable_constraint_ids=readiness.manifest.immutable_constraint_ids,
            call_budgets=readiness.manifest.call_budgets,
            prospective_source_bundle=source_bundle,  # type: ignore[arg-type]
        )
    except ValueError as exc:
        raise ContextBudgetExhausted("context_budget_exhausted") from exc


def _prepare_iteration_source(
    readiness: PitOptimizerReadiness,
    state: _RunState,
    services: PitOptimizerServices,
) -> None:
    if state.iteration_workspace is not None or state.iteration_source_bundle is not None:
        raise SandboxIntegrityFailure("candidate workspace is already active")
    state.evaluation_failure_code = None
    workspace = services.create_candidate(state.incumbent_cumulative_diff or None)
    if not isinstance(workspace, CandidateWorkspace):
        raise SandboxIntegrityFailure("candidate service returned an invalid workspace")
    state.iteration_workspace = workspace
    try:
        bundle = build_policy_source_bundle(
            candidate_root=workspace.root,
            cumulative_diff=state.incumbent_cumulative_diff,
            policy_interface_version=readiness.manifest.policy_interface_version,
        )
        _require_complete_iteration_context(readiness, bundle)
    except ValueError as exc:
        if str(exc) == "next_context_oversize":
            raise ContextBudgetExhausted("context_budget_exhausted") from exc
        raise IdentityDrift("candidate source bundle could not be authenticated") from exc
    state.iteration_source_bundle = bundle


def _run_investigator(
    readiness: PitOptimizerReadiness,
    state: _RunState,
    services: PitOptimizerServices,
) -> PitOptimizerRoleCall:
    if state.iteration_workspace is None and state.iteration_source_bundle is None:
        _prepare_iteration_source(readiness, state, services)
    workspace = state.iteration_workspace
    bundle = state.iteration_source_bundle
    if workspace is None or bundle is None:
        raise SandboxIntegrityFailure("candidate iteration context is incomplete")
    role_input = InvestigatorInput(
        schema_version=2,
        iteration=state.next_iteration,
        run_manifest_sha256=readiness.manifest_sha256,
        policy_interface_version=readiness.manifest.policy_interface_version,
        immutable_constraint_ids=readiness.manifest.immutable_constraint_ids,
        candidate_bounds=readiness.manifest.candidate_bounds,
        rule_summary=readiness.provider_seed.rule_summary,
        source_bundle=bundle,
        baseline_discovery=readiness.baseline_discovery,
        incumbent_summary=_incumbent_summary(state),
        prior_iterations=state.prior_iterations,
    )
    plan = _plan_for(readiness, state, "investigator")
    call = _call_role(
        readiness,
        state,
        services,
        plan,
        role_input,
        lambda raw: InvestigatorArtifact.from_json(
            raw,
            max_total_bytes=plan.max_response_bytes,
        ),
    )
    _record_artifact(
        state,
        services.write_json_artifact(
            _iteration_name(state, "investigator.json"),
            _role_artifact(call),
        ),
    )
    state.last_investigator = call
    return call


def _run_author(
    readiness: PitOptimizerReadiness,
    state: _RunState,
    investigator: PitOptimizerRoleCall,
    services: PitOptimizerServices,
) -> PitOptimizerRoleCall:
    bundle = state.iteration_source_bundle
    if bundle is None or not isinstance(investigator.payload, InvestigatorArtifact):
        raise ProviderProtocolFailure("author predecessor is invalid")
    role_input = AuthorInput(
        schema_version=2,
        iteration=state.next_iteration,
        run_manifest_sha256=readiness.manifest_sha256,
        policy_interface_version=readiness.manifest.policy_interface_version,
        immutable_constraint_ids=readiness.manifest.immutable_constraint_ids,
        candidate_bounds=readiness.manifest.candidate_bounds,
        investigator=investigator.payload,
        source_bundle=bundle,  # type: ignore[arg-type]
    )
    plan = _plan_for(readiness, state, "author")
    call = _call_role(
        readiness,
        state,
        services,
        plan,
        role_input,
        lambda raw: AuthorArtifact.from_json(
            raw,
            max_diff_bytes=readiness.manifest.candidate_bounds.max_diff_bytes,
            max_total_bytes=plan.max_response_bytes,
            controller_paths=investigator.payload.target_paths,
            controller_symbols=investigator.payload.target_symbols,
        ),
    )
    _record_artifact(
        state,
        services.write_json_artifact(
            _iteration_name(state, "author.json"),
            _role_artifact(call),
        ),
    )
    state.last_author = call
    return call


def _focused_checks(changed_symbols: tuple[str, ...]) -> list[str]:
    selected = ["syntax_import", "purity_determinism"]
    if any(".entry." in item for item in changed_symbols):
        selected.append("entry")
    if any(".risk." in item for item in changed_symbols):
        selected.append("risk")
    if any(".exit." in item for item in changed_symbols):
        selected.append("exit")
    return selected


def _require_identity_graph(
    readiness: PitOptimizerReadiness,
    outcome: CandidateValidationOutcome,
) -> None:
    identity = outcome.identity
    if identity is None:
        raise IdentityDrift("valid candidate identity is absent")
    try:
        validate_candidate_identity(identity)
    except ValueError as exc:
        raise IdentityDrift("candidate identity is not authenticated") from exc
    manifest = readiness.manifest
    if (
        identity.source_commit != manifest.source_head
        or identity.policy_interface_version != manifest.policy_interface_version
        or identity.cumulative_diff_sha256
        != hashlib.sha256(outcome.cumulative_diff.encode("utf-8")).hexdigest()
        or identity.immutable_constraints_sha256 != manifest.immutable_constraints_sha256
        or identity.discovery_manifest_sha256 != manifest.fold_manifest.sha256
        or identity.changed_paths != outcome.changed_paths
        or identity.changed_symbols != outcome.changed_symbols
    ):
        raise IdentityDrift("candidate identity differs from the optimizer identity graph")


def _validate_iteration_candidate(
    readiness: PitOptimizerReadiness,
    state: _RunState,
    author: PitOptimizerRoleCall,
    services: PitOptimizerServices,
) -> CandidateValidationOutcome:
    workspace = state.iteration_workspace
    if workspace is None:
        raise SandboxIntegrityFailure("candidate workspace is absent")
    outcome = services.validate_and_apply(
        workspace,
        author.payload,  # type: ignore[arg-type]
        state.incumbent_cumulative_diff or None,
    )
    if not isinstance(outcome, CandidateValidationOutcome):
        raise SandboxIntegrityFailure("candidate validation outcome is invalid")
    author_manifest_matches = False
    if outcome.valid:
        if outcome.failure_code is not None or outcome.identity is None:
            raise SandboxIntegrityFailure("valid candidate outcome is inconsistent")
        _require_identity_graph(readiness, outcome)
        # Candidate identity is derived from the authenticated Git state, not
        # from advisory fields echoed by the author response.  Once that
        # identity graph is valid, the controller-owned manifest is exact.
        author_manifest_matches = True
        try:
            prospective_bundle = build_policy_source_bundle(
                candidate_root=workspace.root,
                cumulative_diff=outcome.cumulative_diff,
                policy_interface_version=readiness.manifest.policy_interface_version,
            )
            _require_complete_iteration_context(readiness, prospective_bundle)
        except ValueError as exc:
            if str(exc) == "next_context_oversize":
                outcome = replace(
                    outcome,
                    valid=False,
                    failure_code="next_context_oversize",
                    cumulative_diff=state.incumbent_cumulative_diff,
                    identity=None,
                )
            else:
                raise IdentityDrift("validated candidate source bundle differs") from exc
        except ContextBudgetExhausted:
            outcome = replace(
                outcome,
                valid=False,
                failure_code="next_context_oversize",
                cumulative_diff=state.incumbent_cumulative_diff,
                identity=None,
            )
        else:
            state.prospective_source_bundle = prospective_bundle
    elif outcome.failure_code is None or outcome.identity is not None:
        raise SandboxIntegrityFailure("invalid candidate outcome is inconsistent")
    _record_artifact(
        state,
        services.write_diff_artifact(
            _iteration_name(state, "candidate.diff"),
            outcome.incremental_diff,
        ),
    )
    primitive = {
        "schema_version": 3,
        "failure_code": outcome.failure_code,
        "candidate_identity": (
            None if outcome.identity is None else outcome.identity.to_primitive()
        ),
        "author_manifest_matches": author_manifest_matches,
        "focused_checks": _focused_checks(outcome.changed_symbols),
        "worker_attestation": {
            "attempted": outcome.valid or outcome.failure_code in {"worker_failed", "replay_failed"},
            "complete": outcome.valid,
        },
        "changed_paths": list(outcome.changed_paths),
        "changed_symbols": list(outcome.changed_symbols),
    }
    _record_artifact(
        state,
        services.write_json_artifact(
            _iteration_name(state, "validation.json"),
            primitive,
        ),
    )
    return outcome


def _folds_digest(folds: tuple[FoldAggregateSummary, ...]) -> str:
    return hashlib.sha256(
        (
            json.dumps(
                [asdict(item) for item in folds],
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
    ).hexdigest()


def _score_primitive(score: object) -> dict[str, object]:
    return {
        "median_excess_return_pp": str(score.median_excess_return_pp),
        "worst_excess_return_pp": str(score.worst_excess_return_pp),
        "max_drawdown_magnitude_pp": str(score.max_drawdown_magnitude_pp),
    }


def _unrankable_incumbent_score() -> DiscoveryScore:
    """Return the neutral starting objective for a zero-trade baseline."""

    return DiscoveryScore(
        median_excess_return_pp=Decimal("0.00"),
        worst_excess_return_pp=Decimal("0.00"),
        max_drawdown_magnitude_pp=Decimal("0.00"),
    )


def _evaluate_iteration_candidate(
    readiness: PitOptimizerReadiness,
    state: _RunState,
    validation: CandidateValidationOutcome,
    services: PitOptimizerServices,
) -> DiscoveryEvaluation | None:
    if not validation.valid:
        return None
    if state.iteration_workspace is None or validation.identity is None:
        raise SandboxIntegrityFailure("valid candidate evaluation workspace is absent")
    try:
        supplied = services.evaluate_discovery(
            state.iteration_workspace,
            validation.identity,
        )
    except CandidateEvaluationFailure as exc:
        state.evaluation_failure_code = exc.failure_code
        _record_artifact(
            state,
            services.write_json_artifact(
                _iteration_name(state, "discovery.json"),
                {
                    "schema_version": 3,
                    "failure_code": exc.failure_code,
                    "fixed_baseline_comparison": None,
                    "incumbent_diagnostics": None,
                    "rankable": False,
                    "strictly_improves_incumbent": False,
                    "folds": [],
                    "engine_policy_sha256": readiness.manifest.effective_policy_sha256,
                    "candidate_identity_sha256": validation.identity.identity_sha256,
                },
            ),
        )
        return None
    if not isinstance(supplied, DiscoveryEvaluation):
        raise EvidenceTampering("discovery evaluation is not closed")
    manifest = readiness.manifest
    if any(
        item.engine_policy_sha256 != manifest.effective_policy_sha256
        or item.candidate_identity_sha256 != validation.identity.identity_sha256
        for item in supplied.folds
    ):
        raise IdentityDrift("discovery evaluation identity differs")
    candidate_folds = tuple(item.aggregate_metrics for item in supplied.folds)
    if sum(item.closed_trades for item in candidate_folds) < 1:
        if (
            supplied.comparison.rankable
            or supplied.comparison.strictly_improves_incumbent
        ):
            raise EvidenceTampering("zero-trade discovery evaluation is rankable")
        state.evaluation_failure_code = "no_discovery_trades"
        _record_artifact(
            state,
            services.write_json_artifact(
                _iteration_name(state, "discovery.json"),
                {
                    "schema_version": 3,
                    "failure_code": "no_discovery_trades",
                    "fixed_baseline_comparison": None,
                    "incumbent_diagnostics": None,
                    "rankable": False,
                    "strictly_improves_incumbent": False,
                    "folds": [
                        {
                            "fold_id": item.fold_id,
                            "aggregate": asdict(item.aggregate_metrics),
                        }
                        for item in supplied.folds
                    ],
                    "engine_policy_sha256": manifest.effective_policy_sha256,
                    "candidate_identity_sha256": validation.identity.identity_sha256,
                },
            ),
        )
        # The candidate is ineligible for selection, but its aggregate folds are
        # still the critic's only evidence for why tradeability disappeared.
        # Preserve the unrankable evaluation for feedback while the decision
        # path continues to require rankable=True before any acceptance.
        return supplied
    baseline_folds = readiness.baseline_discovery.folds
    baseline_sha256 = _folds_digest(baseline_folds)
    try:
        fixed_score = discovery_score_from_folds(
            candidate_folds,
            baseline_folds,
            original_baseline_sha256=baseline_sha256,
            expected_original_baseline_sha256=baseline_sha256,
        )
        incumbent_folds = state.incumbent_discovery.folds
        incumbent_sha256 = _folds_digest(incumbent_folds)
        incumbent_diagnostics = discovery_score_from_folds(
            candidate_folds,
            incumbent_folds,
            original_baseline_sha256=incumbent_sha256,
            expected_original_baseline_sha256=incumbent_sha256,
        )
        incumbent_score = state.incumbent_discovery.score
        if incumbent_score is None:
            incumbent_score = (
                _unrankable_incumbent_score()
                if sum(item.closed_trades for item in incumbent_folds) < 1
                else discovery_score_from_folds(
                    baseline_folds,
                    baseline_folds,
                    original_baseline_sha256=baseline_sha256,
                    expected_original_baseline_sha256=baseline_sha256,
                )
            )
    except ValueError as exc:
        raise EvidenceTampering("discovery fixed-baseline objective is invalid") from exc
    expected_improvement = strictly_improves_discovery(fixed_score, incumbent_score)
    comparison = supplied.comparison
    if (
        comparison.candidate_vs_fixed_baseline != fixed_score
        or comparison.candidate_vs_incumbent_diagnostics != incumbent_diagnostics
        or comparison.strictly_improves_incumbent
        is not (comparison.rankable and expected_improvement)
    ):
        raise EvidenceTampering("discovery comparison differs from fixed-baseline objective")
    normalized_folds = tuple(
        replace(
            item,
            aggregate_metrics=replace(
                item.aggregate_metrics,
                excess_total_return_pp=(
                    float(item.aggregate_metrics.total_return_pct)
                    - float(baseline.total_return_pct)
                ),
            ),
        )
        for item, baseline in zip(supplied.folds, baseline_folds, strict=True)
    )
    discovery = replace(supplied, folds=normalized_folds)
    _record_artifact(
        state,
        services.write_json_artifact(
            _iteration_name(state, "discovery.json"),
            {
                "schema_version": 3,
                "fixed_baseline_comparison": _score_primitive(fixed_score),
                "incumbent_diagnostics": _score_primitive(incumbent_diagnostics),
                "rankable": comparison.rankable,
                "strictly_improves_incumbent": comparison.strictly_improves_incumbent,
                "folds": [
                    {
                        "fold_id": item.fold_id,
                        "aggregate": asdict(item.aggregate_metrics),
                    }
                    for item in discovery.folds
                ],
                "engine_policy_sha256": manifest.effective_policy_sha256,
                "candidate_identity_sha256": validation.identity.identity_sha256,
            },
        ),
    )
    return discovery


def _validation_summary(
    validation: CandidateValidationOutcome,
    evaluation_failure_code: str | None = None,
) -> CandidateValidationSummary:
    if evaluation_failure_code is not None:
        if evaluation_failure_code not in _VALIDATION_FAILURE_FLAGS:
            raise EvidenceTampering("discovery failure code is invalid")
        return CandidateValidationSummary(
            evaluation_failure_code,
            *_VALIDATION_FAILURE_FLAGS[evaluation_failure_code],
        )
    if validation.valid:
        return CandidateValidationSummary(None, True, True, True, True, True, True)
    code = validation.failure_code
    if code not in _VALIDATION_FAILURE_FLAGS:
        code = "author_diff_invalid"
    return CandidateValidationSummary(code, *_VALIDATION_FAILURE_FLAGS[code])


_FEEDBACK_FUNNEL_IDS = frozenset(
    {
        "evaluated_rows",
        "buy_signal_count",
        "market_pass",
        "breakout_pass",
        "volume_surge_pass",
        "buy_zone_pass",
        "technical_score_pass",
    }
)


def _bounded_feedback_folds(
    candidate_folds: tuple[FoldAggregateSummary, ...],
    comparison_folds: tuple[FoldAggregateSummary, ...],
) -> tuple[FoldAggregateSummary, ...]:
    """Keep core funnel stages plus every metric changed by the candidate."""

    bounded: list[FoldAggregateSummary] = []
    for candidate, comparison in zip(
        candidate_folds,
        comparison_folds,
        strict=True,
    ):
        comparison_metrics = {
            metric.metric_id: metric.value for metric in comparison.entry_funnel
        }
        selected = tuple(
            metric
            for metric in candidate.entry_funnel
            if metric.metric_id in _FEEDBACK_FUNNEL_IDS
            or comparison_metrics.get(metric.metric_id) != metric.value
        )
        bounded.append(replace(candidate, entry_funnel=selected))
    return tuple(bounded)


def _critic_comparison(
    discovery: DiscoveryEvaluation,
    comparison_folds: tuple[FoldAggregateSummary, ...],
    *,
    fixed: bool,
) -> CandidateComparisonSummary:
    score = (
        discovery.comparison.candidate_vs_fixed_baseline
        if fixed
        else discovery.comparison.candidate_vs_incumbent_diagnostics
    )
    return CandidateComparisonSummary(
        folds=_bounded_feedback_folds(
            tuple(item.aggregate_metrics for item in discovery.folds),
            comparison_folds,
        ),
        score=score,
        diagnostics=(),
        _controller_seal=_CANDIDATE_COMPARISON_SEAL,
    )


def _run_critic(
    readiness: PitOptimizerReadiness,
    state: _RunState,
    investigator: PitOptimizerRoleCall,
    author: PitOptimizerRoleCall,
    validation: CandidateValidationOutcome,
    discovery: DiscoveryEvaluation | None,
    services: PitOptimizerServices,
) -> PitOptimizerRoleCall:
    if not isinstance(investigator.payload, InvestigatorArtifact) or not isinstance(
        author.payload,
        AuthorArtifact,
    ):
        raise ProviderProtocolFailure("critic predecessors are invalid")
    # Critic provenance binds to this iteration's incremental author action.
    # The cumulative candidate identity is authenticated separately by the
    # validation outcome and may include files changed by earlier incumbents.
    changed_paths = author.payload.changed_paths
    changed_symbols = author.payload.changed_symbols
    role_input = CriticInput(
        schema_version=2,
        iteration=state.next_iteration,
        run_manifest_sha256=readiness.manifest_sha256,
        immutable_constraint_ids=readiness.manifest.immutable_constraint_ids,
        hypothesis_id=investigator.payload.hypothesis_id,
        investigator_summary=investigator.payload,
        author_manifest=AuthorManifestSummary(
            hypothesis_id=investigator.payload.hypothesis_id,
            behavioral_summary=author.payload.behavioral_summary,
            changed_paths=changed_paths,
            changed_symbols=changed_symbols,
        ),
        validation=_validation_summary(validation, state.evaluation_failure_code),
        candidate_vs_baseline=(
            None
            if discovery is None
            else _critic_comparison(
                discovery,
                readiness.baseline_discovery.folds,
                fixed=True,
            )
        ),
        candidate_vs_incumbent=(
            None
            if discovery is None
            else _critic_comparison(
                discovery,
                state.incumbent_discovery.folds,
                fixed=False,
            )
        ),
    )
    plan = _plan_for(readiness, state, "critic")
    call = _call_role(
        readiness,
        state,
        services,
        plan,
        role_input,
        lambda raw: CriticArtifact.from_json(
            raw,
            max_total_bytes=plan.max_response_bytes,
        ),
    )
    _record_artifact(
        state,
        services.write_json_artifact(
            _iteration_name(state, "critic.json"),
            _role_artifact(call),
        ),
    )
    return call


def _retain_pending_retired_workspace(
    state: _RunState,
    workspace: CandidateWorkspace,
) -> None:
    for retained in state.pending_retired_workspaces:
        if retained.workspace_id == workspace.workspace_id:
            if retained != workspace:
                raise SandboxIntegrityFailure(
                    "retired candidate workspace identity differs"
                )
            return
    state.pending_retired_workspaces.append(workspace)


def _dispose_intermediate_candidate(
    state: _RunState,
    services: PitOptimizerServices,
    workspace: CandidateWorkspace,
) -> None:
    """Revoke one retired workspace and fail closed on any cleanup doubt."""

    failed = PitOptimizerCleanup(False, False, False)
    try:
        cleanup = services.dispose_candidate(workspace)
    except BaseException as exc:
        state.cleanup_observations.append(failed)
        _retain_pending_retired_workspace(state, workspace)
        raise SandboxIntegrityFailure(
            "intermediate candidate cleanup failed"
        ) from exc
    if not isinstance(cleanup, PitOptimizerCleanup):
        state.cleanup_observations.append(failed)
        _retain_pending_retired_workspace(state, workspace)
        raise SandboxIntegrityFailure(
            "intermediate candidate cleanup result is invalid"
        )
    state.cleanup_observations.append(cleanup)
    if not cleanup.candidate_removed or not cleanup.worker_stopped:
        _retain_pending_retired_workspace(state, workspace)
        raise SandboxIntegrityFailure(
            "intermediate candidate cleanup is incomplete"
        )
    if cleanup.source_modified:
        raise SandboxIntegrityFailure(
            "intermediate candidate cleanup observed source modification"
        )


def _persist_iteration_decision(
    readiness: PitOptimizerReadiness,
    state: _RunState,
    validation: CandidateValidationOutcome,
    discovery: DiscoveryEvaluation | None,
    critic: PitOptimizerRoleCall,
    services: PitOptimizerServices,
) -> _IterationOutcome:
    investigator_call = state.last_investigator
    author_call = state.last_author
    investigator = None if investigator_call is None else investigator_call.payload
    author = None if author_call is None else author_call.payload
    critic_payload = getattr(critic, "payload", None)
    if not isinstance(investigator, InvestigatorArtifact) or not isinstance(
        author,
        AuthorArtifact,
    ) or not isinstance(critic_payload, CriticArtifact):
        raise ProviderProtocolFailure("iteration decision predecessors are invalid")
    expected_critic = _iteration_name(state, "critic.json")
    if state.artifact_paths and not any(
        path.as_posix().endswith(expected_critic) for path, _digest_value in state.artifact_paths
    ):
        raise AuditFailure("critic artifact is not durable before decision")

    rankable = discovery is not None and discovery.comparison.rankable
    improves = bool(
        validation.valid
        and rankable
        and discovery is not None
        and discovery.comparison.strictly_improves_incumbent
    )
    prior_identity = (
        None if state.incumbent_identity is None else state.incumbent_identity.identity_sha256
    )
    candidate_workspace = state.iteration_workspace
    candidate_identity = validation.identity
    score = (
        None
        if discovery is None
        else discovery.comparison.candidate_vs_fixed_baseline
    )

    prospective_valid_evaluations = state.valid_evaluations + int(rankable)
    prospective_non_improving_streak = state.non_improving_streak
    if rankable:
        prospective_non_improving_streak = (
            0 if improves else state.non_improving_streak + 1
        )
    prospective_incumbent_discovery = state.incumbent_discovery
    prospective_incumbent_workspace = state.incumbent_workspace
    prospective_incumbent_identity = state.incumbent_identity
    prospective_incumbent_diff = state.incumbent_cumulative_diff
    prospective_incumbent_updates = state.incumbent_updates
    prior_workspace = state.incumbent_workspace
    if improves:
        if candidate_identity is None or candidate_workspace is None:
            raise IdentityDrift("improving candidate identity or workspace is absent")
        try:
            validate_candidate_identity(candidate_identity)
        except ValueError as exc:
            raise IdentityDrift("candidate identity drifted before incumbent replacement") from exc
        bundle = state.prospective_source_bundle
        if bundle is None:
            raise IdentityDrift("prospective incumbent source bundle is absent")
        if isinstance(candidate_identity, CandidateIdentity):
            files = getattr(bundle, "files", ())
            actual_file_sha256s = tuple(
                (item.path, item.sha256) for item in files
            )
            if (
                candidate_identity.cumulative_diff_sha256
                != hashlib.sha256(validation.cumulative_diff.encode("utf-8")).hexdigest()
                or actual_file_sha256s != candidate_identity.editable_file_sha256s
            ):
                raise IdentityDrift("prospective incumbent bundle differs from identity")
        assert discovery is not None
        candidate_folds = tuple(item.aggregate_metrics for item in discovery.folds)
        prospective_incumbent_discovery = DiscoveryEvidenceSummary(
            folds=candidate_folds,
            score=discovery.comparison.candidate_vs_fixed_baseline,
            evidence_ids=tuple(item.evidence_sha256 for item in discovery.folds),
        )
        prospective_incumbent_workspace = candidate_workspace
        prospective_incumbent_identity = candidate_identity
        prospective_incumbent_diff = validation.cumulative_diff
        prospective_incumbent_updates += 1

    feedback = IterationFeedbackSummary(
        iteration=state.next_iteration,
        hypothesis_id=investigator.hypothesis_id,
        family=investigator.family,
        author_summary=author.behavioral_summary,
        validation_code=(
            state.evaluation_failure_code
            or (
                "valid"
                if validation.valid and rankable
                else validation.failure_code or "unrankable"
            )
        ),
        candidate_folds=(
            ()
            if discovery is None
            else _bounded_feedback_folds(
                tuple(item.aggregate_metrics for item in discovery.folds),
                readiness.baseline_discovery.folds,
            )
        ),
        discovery_score=score if rankable else None,
        critic_disposition=critic_payload.disposition,
        critic_next_direction=critic_payload.next_direction,
        incumbent_changed=improves,
    )
    new_identity = (
        None
        if prospective_incumbent_identity is None
        else prospective_incumbent_identity.identity_sha256
    )
    _record_artifact(
        state,
        services.write_json_artifact(
            _iteration_name(state, "decision.json"),
            {
                "schema_version": 3,
                "rankable": bool(rankable),
                "quantized_score": None if score is None else _score_primitive(score),
                "prior_incumbent_identity_sha256": prior_identity,
                "new_incumbent_identity_sha256": new_identity,
                "decision": "accept" if improves else "retain",
            },
        ),
    )

    # ``decision.json`` is the authoritative transition record.  No live
    # incumbent state or capability ownership changes before it is durable.
    if improves:
        _record_artifact(
            state,
            services.write_diff_artifact(
                "incumbent.diff",
                validation.cumulative_diff,
            ),
        )
    state.valid_evaluations = prospective_valid_evaluations
    state.non_improving_streak = prospective_non_improving_streak
    state.incumbent_discovery = prospective_incumbent_discovery
    state.incumbent_workspace = prospective_incumbent_workspace
    state.incumbent_identity = prospective_incumbent_identity
    state.incumbent_cumulative_diff = prospective_incumbent_diff
    state.incumbent_updates = prospective_incumbent_updates
    if (
        improves
        and prior_workspace is not None
        and prior_workspace != candidate_workspace
    ):
        _dispose_intermediate_candidate(state, services, prior_workspace)
    elif (
        not improves
        and candidate_workspace is not None
        and candidate_workspace != state.incumbent_workspace
    ):
        _dispose_intermediate_candidate(state, services, candidate_workspace)
    state.prior_iterations = (*state.prior_iterations, feedback)
    state.iterations_completed += 1
    state.next_iteration += 1
    state.iteration_workspace = None
    state.iteration_source_bundle = None
    state.prospective_source_bundle = None
    state.evaluation_failure_code = None
    return _IterationOutcome(
        completed=True,
        terminal_code=None,
        feedback=feedback,
        candidate_workspace=candidate_workspace if improves else None,
        candidate_identity=candidate_identity if improves else None,
        discovery=discovery,
        incumbent_changed=improves,
    )


def _run_iteration(
    readiness: PitOptimizerReadiness,
    state: _RunState,
    services: PitOptimizerServices,
) -> _IterationOutcome:
    investigator = _run_investigator(readiness, state, services)
    author = _run_author(readiness, state, investigator, services)
    validation = _validate_iteration_candidate(readiness, state, author, services)
    discovery = _evaluate_iteration_candidate(readiness, state, validation, services)
    critic = _run_critic(
        readiness,
        state,
        investigator,
        author,
        validation,
        discovery,
        services,
    )
    return _persist_iteration_decision(
        readiness,
        state,
        validation,
        discovery,
        critic,
        services,
    )


def _pre_iteration_stop(
    readiness: PitOptimizerReadiness,
    state: _RunState,
    services: PitOptimizerServices,
) -> tuple[str, str | None] | None:
    if services.cancellation_requested():
        return "cancelled", None
    if state.non_improving_streak >= readiness.manifest.non_improving_limit:
        return "stagnation_limit", None
    if state.next_iteration > min(readiness.manifest.max_iterations, 8):
        return "iteration_limit", None
    lease = state.authorization_lease
    if lease is None:
        return "authorization_exhausted", None
    spent = _budget_summary_from_calls(state.provider_attempts, lease)
    if spent.incomplete_accounting_calls:
        raise ProviderAccountingFailure("provider accounting is incomplete")
    next_plans = tuple(
        item
        for item in readiness.manifest.call_budgets
        if item.iteration == state.next_iteration
    )
    if tuple(item.role for item in next_plans) != (
        "investigator",
        "author",
        "critic",
    ):
        return "authorization_exhausted", None
    if lease.max_calls - spent.api_calls < 3:
        return "budget_exhausted", "call_budget_exhausted"
    required_tokens = sum(
        item.max_input_tokens + item.max_output_tokens for item in next_plans
    )
    charged_tokens = spent.total_tokens + spent.retained_reservation_tokens
    if lease.max_tokens - charged_tokens < required_tokens:
        return "budget_exhausted", "token_budget_exhausted"
    return None


def _finish_discovery(
    readiness: PitOptimizerReadiness,
    state: _RunState,
    services: PitOptimizerServices,
) -> DeterminismAttestation | None:
    if state.incumbent_identity is None:
        return None
    if state.incumbent_workspace is None:
        raise IdentityDrift("discovery incumbent workspace is absent")
    fold_id = readiness.manifest.fold_manifest.discovery_folds[0].fold_id
    expected = state.incumbent_discovery.evidence_ids[0]
    attestation = services.confirm_discovery(
        state.incumbent_workspace,
        state.incumbent_identity,
        fold_id,
    )
    if not isinstance(attestation, DeterminismAttestation):
        raise TrustedEvaluatorNondeterminism("discovery repeat is not attested")
    if (
        attestation.fold_id != fold_id
        or attestation.expected_evidence_sha256 != expected
        or attestation.repeated_evidence_sha256 != expected
        or not attestation.matched
    ):
        raise TrustedEvaluatorNondeterminism(
            "trusted discovery repeat differs from incumbent evidence"
        )
    return attestation


def _terminal_from_exception(exc: BaseException) -> tuple[str, str | None]:
    mapping: tuple[tuple[type[BaseException], str], ...] = (
        (ProviderProtocolFailure, "provider_protocol_failure"),
        (ProviderAccountingFailure, "provider_accounting_failure"),
        (AuditFailure, "audit_failure"),
        (AuthorizationExhausted, "authorization_exhausted"),
        (IdentityDrift, "identity_drift"),
        (TrustedEvaluatorNondeterminism, "trusted_evaluator_nondeterminism"),
        (SandboxIntegrityFailure, "sandbox_integrity_failure"),
        (EvidenceTampering, "evidence_tampering"),
    )
    if isinstance(exc, (ContextBudgetExhausted, RoleContextBudgetExceeded)):
        return "budget_exhausted", "context_budget_exhausted"
    if isinstance(exc, KeyboardInterrupt):
        return "cancelled", None
    for kind, code in mapping:
        if isinstance(exc, kind):
            return code, None
    if isinstance(exc, OSError):
        return "audit_failure", None
    return "sandbox_integrity_failure", None


def _new_run_state(readiness: PitOptimizerReadiness) -> _RunState:
    return _RunState(
        run_id=readiness.manifest.run_id,
        next_iteration=1,
        incumbent_workspace=None,
        incumbent_identity=None,
        incumbent_cumulative_diff="",
        incumbent_discovery=readiness.baseline_discovery,
        prior_iterations=(),
        valid_evaluations=0,
        incumbent_updates=0,
        non_improving_streak=0,
        provider_enabled=True,
        pricing_snapshot=None,
        authorization_lease=None,
    )


def _require_hidden_outcome_proof(
    proof: ValidationOutcomeProof,
    reservation: ValidationReservation,
    *,
    attempted: bool,
    completed: bool,
    failure_code: str | None,
) -> None:
    if not isinstance(proof, ValidationOutcomeProof):
        raise AuditFailure("hidden validation outcome proof is invalid")
    if (
        proof.reservation_record_sha256 != reservation.reservation_record_sha256
        or proof.attempted is not attempted
        or proof.completed is not completed
        or proof.failure_code != failure_code
        or proof.outcome_record_sha256 != proof.ledger_head_sha256
    ):
        raise AuditFailure("hidden validation outcome proof differs")


def _run_hidden_once(
    readiness: PitOptimizerReadiness,
    state: _RunState,
    services: PitOptimizerServices,
) -> HiddenEvaluation:
    if state.hidden_validation_opened:
        raise EvidenceTampering("hidden validation was already opened")
    identity = state.incumbent_identity
    workspace = state.incumbent_workspace
    if identity is None or workspace is None:
        raise IdentityDrift("hidden validation incumbent is absent")
    identity_sha256 = getattr(identity, "identity_sha256", None)
    if not isinstance(identity_sha256, str) or len(identity_sha256) != 64:
        raise IdentityDrift("hidden validation incumbent identity is invalid")

    reservation = services.reserve_hidden_validation(identity)
    if not isinstance(reservation, ValidationReservation):
        raise EvidenceTampering("hidden validation reservation is invalid")
    state.validation_reservation = reservation
    state.hidden_validation_opened = True

    # The shared role-call path begins with this guard, so no later provider
    # request can observe hidden-window identity, aggregates, or outcome.
    state.provider_enabled = False
    attempted = False
    outcome_recorded = False
    failure_code = "not_attempted"
    try:
        attempted = True
        failure_code = "replay_failed"
        attestation = services.evaluate_hidden(workspace, identity, reservation)
        failure_code = "integrity_failed"
        if not isinstance(attestation, HiddenEvaluationAttestation):
            raise EvidenceTampering("hidden evaluation attestation is not closed")
        manifest = readiness.manifest
        hidden_fold_id = manifest.fold_manifest.hidden_fold.fold_id
        if attestation.reservation_record_sha256 != reservation.reservation_record_sha256:
            raise EvidenceTampering("hidden attestation reservation differs")
        if (
            attestation.source_head != manifest.source_head
            or attestation.source_fingerprint_sha256
            != manifest.source_fingerprint_sha256
            or attestation.baseline_policy_sha256 != manifest.effective_policy_sha256
            or attestation.candidate_identity_sha256 != identity_sha256
        ):
            raise IdentityDrift("hidden attestation identity differs")
        if attestation.fold_id != hidden_fold_id:
            raise IdentityDrift("hidden attestation fold identity differs")
        baseline_reset = attestation.baseline_reset
        candidate_reset = attestation.candidate_reset
        if (
            baseline_reset.fold_id != hidden_fold_id
            or baseline_reset.subject != "baseline"
            or baseline_reset.subject_identity_sha256
            != manifest.effective_policy_sha256
            or candidate_reset.fold_id != hidden_fold_id
            or candidate_reset.subject != "candidate"
            or candidate_reset.subject_identity_sha256 != identity_sha256
        ):
            raise IdentityDrift("hidden reset receipt identity differs")
        if baseline_reset.reset_receipt_sha256 == candidate_reset.reset_receipt_sha256:
            raise EvidenceTampering("hidden baseline and candidate reused one reset")

        hidden = attestation.evaluation
        if (
            hidden.baseline_aggregate.fold_id != hidden_fold_id
            or hidden.candidate_aggregate.fold_id != hidden_fold_id
        ):
            raise IdentityDrift("hidden evaluation fold identity differs")

        expected_decision = HoldoutDecision.from_result(
            excess_total_return_pp=(
                Decimal(str(hidden.candidate_aggregate.total_return_pct))
                - Decimal(str(hidden.baseline_aggregate.total_return_pct))
            ),
            closed_trades=hidden.candidate_aggregate.closed_trades,
            safety_complete=hidden.decision.safety_complete,
            integrity_complete=hidden.decision.integrity_complete,
            accounting_complete=hidden.decision.accounting_complete,
        )
        if hidden.decision != expected_decision:
            raise EvidenceTampering("hidden eligibility differs from trusted aggregates")
        supplied_excess = hidden.candidate_aggregate.excess_total_return_pp
        if supplied_excess is None or HoldoutDecision.from_result(
            excess_total_return_pp=supplied_excess,
            closed_trades=hidden.candidate_aggregate.closed_trades,
            safety_complete=hidden.decision.safety_complete,
            integrity_complete=hidden.decision.integrity_complete,
            accounting_complete=hidden.decision.accounting_complete,
        ).excess_total_return_pp != expected_decision.excess_total_return_pp:
            raise EvidenceTampering("hidden candidate excess differs from trusted aggregates")

        _record_artifact(
            state,
            services.write_json_artifact(
                "holdout.json",
                {
                    "schema_version": 3,
                    "consumed_validation_key_sha256": (
                        reservation.consumption_key_sha256
                    ),
                    "validation_reservation_sha256": (
                        reservation.reservation_record_sha256
                    ),
                    "baseline_aggregate": asdict(hidden.baseline_aggregate),
                    "candidate_aggregate": asdict(hidden.candidate_aggregate),
                    "baseline_identity_sha256": (
                        manifest.effective_policy_sha256
                    ),
                    "candidate_identity_sha256": identity_sha256,
                    "eligibility_checks": {
                        "excess_total_return_pp": str(
                            expected_decision.excess_total_return_pp
                        ),
                        "closed_trades": expected_decision.closed_trades,
                        "safety_complete": expected_decision.safety_complete,
                        "integrity_complete": expected_decision.integrity_complete,
                        "accounting_complete": expected_decision.accounting_complete,
                        "long_replay_eligible": (
                            expected_decision.long_replay_eligible
                        ),
                        "hidden_attestation_sha256": (
                            attestation.attestation_sha256
                        ),
                        "baseline_reset_receipt_sha256": (
                            baseline_reset.reset_receipt_sha256
                        ),
                        "candidate_reset_receipt_sha256": (
                            candidate_reset.reset_receipt_sha256
                        ),
                    },
                },
            ),
        )
        outcome_recorded = True
        try:
            proof = services.record_hidden_outcome(
                reservation,
                True,
                True,
                None,
            )
        except BaseException as exc:
            raise AuditFailure("hidden validation outcome was not durably closed") from exc
        _require_hidden_outcome_proof(
            proof,
            reservation,
            attempted=True,
            completed=True,
            failure_code=None,
        )
        state.validation_outcome_proof = proof
        state.hidden_attestation = attestation
        state.hidden_evaluation = hidden
        return hidden
    except BaseException as original:
        if not outcome_recorded:
            outcome_recorded = True
            closed_failure_code = failure_code if attempted else "not_attempted"
            try:
                proof = services.record_hidden_outcome(
                    reservation,
                    attempted,
                    False,
                    closed_failure_code,
                )
                _require_hidden_outcome_proof(
                    proof,
                    reservation,
                    attempted=attempted,
                    completed=False,
                    failure_code=closed_failure_code,
                )
                state.validation_outcome_proof = proof
            except BaseException as outcome_error:
                raise AuditFailure(
                    "hidden validation failure outcome was not durably closed"
                ) from outcome_error
        raise original


def _dispose_all_candidates_and_workers(
    state: _RunState,
    services: PitOptimizerServices,
) -> PitOptimizerCleanup:
    """Attempt every unique capability disposal and aggregate closed cleanup facts."""

    workspaces: list[CandidateWorkspace] = []
    seen: set[str] = set()
    for workspace in (
        state.iteration_workspace,
        state.incumbent_workspace,
        *state.pending_retired_workspaces,
    ):
        if workspace is not None and workspace.workspace_id not in seen:
            seen.add(workspace.workspace_id)
            workspaces.append(workspace)

    observations: list[PitOptimizerCleanup] = []
    for workspace in workspaces:
        try:
            cleanup = services.dispose_candidate(workspace)
            if not isinstance(cleanup, PitOptimizerCleanup):
                raise SandboxIntegrityFailure(
                    "candidate cleanup result is invalid"
                )
            observations.append(cleanup)
        except BaseException:
            state.finalization_failures.append("candidate_cleanup")
            observations.append(PitOptimizerCleanup(False, False, False))

    state.cleanup_observations.extend(observations)
    state.iteration_workspace = None
    state.incumbent_workspace = None
    state.pending_retired_workspaces.clear()
    state.iteration_source_bundle = None
    state.prospective_source_bundle = None
    all_observations = state.cleanup_observations
    return PitOptimizerCleanup(
        candidate_removed=all(
            item.candidate_removed for item in all_observations
        ),
        worker_stopped=all(item.worker_stopped for item in all_observations),
        source_modified=any(item.source_modified for item in all_observations),
    )


def _build_final_result(
    *,
    readiness: PitOptimizerReadiness,
    state: _RunState,
    terminal_code: str,
    cleanup: PitOptimizerCleanup,
) -> PitOptimizerResult:
    """Construct the terminal value only from already-finalized local facts."""

    if terminal_code not in OptimizerTerminalCode:
        terminal_code = "sandbox_integrity_failure"
    normal_codes = {
        "iteration_limit",
        "budget_exhausted",
        "stagnation_limit",
        "cancelled",
    }
    budget = _budget_summary_from_calls(
        state.provider_attempts,
        state.authorization_lease or state.lease_snapshot,
    )
    cleanup_complete = (
        cleanup.candidate_removed
        and cleanup.worker_stopped
        and not cleanup.source_modified
        and not state.finalization_failures
    )
    if terminal_code in normal_codes and budget.incomplete_accounting_calls:
        terminal_code = "provider_accounting_failure"
        state.terminal_detail = None
    if terminal_code in normal_codes and not cleanup_complete:
        terminal_code = "sandbox_integrity_failure"
        state.terminal_detail = None

    normal = terminal_code in normal_codes
    hidden = state.hidden_evaluation
    long_replay_eligible = (
        None
        if not state.hidden_validation_opened or hidden is None
        else hidden.decision.long_replay_eligible
    )
    status = (
        "aborted"
        if not normal
        else (
            "long_replay_eligible"
            if long_replay_eligible is True
            else "loop_verified_no_long_replay_candidate"
        )
    )
    root = state.artifact_root
    if root is None:
        root = (readiness.artifact_path.parent / state.run_id).resolve()
    reservation = state.validation_reservation
    return PitOptimizerResult(
        schema_version=3,
        phase="run",
        status=status,
        terminal_code=terminal_code,
        terminal_detail=state.terminal_detail,
        exit_code=0 if normal else 1,
        run_id=state.run_id,
        readiness_sha256=readiness.readiness_sha256,
        manifest_sha256=readiness.manifest_sha256,
        iterations_started=state.iterations_started,
        iterations_completed=state.iterations_completed,
        valid_evaluations=state.valid_evaluations,
        incumbent_updates=state.incumbent_updates,
        non_improving_streak=state.non_improving_streak,
        discovery_winner=state.incumbent_identity,
        hidden_validation_opened=state.hidden_validation_opened,
        validation_reservation_sha256=(
            None if reservation is None else reservation.reservation_record_sha256
        ),
        long_replay_eligible=long_replay_eligible,
        budget=budget,
        artifact_root=root,
        artifact_paths=tuple(state.artifact_paths),
        source_modified=cleanup.source_modified,
        cleanup_complete=cleanup_complete,
    )


def _finalize_result(
    readiness: PitOptimizerReadiness,
    state: _RunState,
    services: PitOptimizerServices,
    terminal_code: str,
) -> PitOptimizerResult:
    state.provider_enabled = False
    effective_terminal = terminal_code
    lease = state.authorization_lease
    if lease is not None:
        try:
            services.close_run_lease(lease, terminal_code)
        except BaseException:
            state.finalization_failures.append("lease_close")
            effective_terminal = "audit_failure"
            state.terminal_detail = None
        finally:
            state.authorization_lease = None

    cleanup = _dispose_all_candidates_and_workers(state, services)
    if (
        effective_terminal in {
            "iteration_limit",
            "budget_exhausted",
            "stagnation_limit",
            "cancelled",
        }
        and (
            not cleanup.candidate_removed
            or not cleanup.worker_stopped
            or cleanup.source_modified
        )
    ):
        effective_terminal = "sandbox_integrity_failure"
        state.terminal_detail = None

    try:
        services.verify_inputs(readiness)
    except BaseException as exc:
        state.finalization_failures.append("input_verification")
        if effective_terminal in {
            "iteration_limit",
            "budget_exhausted",
            "stagnation_limit",
            "cancelled",
        }:
            effective_terminal, _detail = _terminal_from_exception(exc)
            if effective_terminal in {
                "iteration_limit",
                "budget_exhausted",
                "stagnation_limit",
                "cancelled",
            }:
                effective_terminal = "sandbox_integrity_failure"
        state.terminal_detail = None

    if state.incumbent_cumulative_diff and not any(
        path.name == "incumbent.diff" for path, _digest in state.artifact_paths
    ):
        try:
            _record_artifact(
                state,
                services.write_diff_artifact(
                    "incumbent.diff",
                    state.incumbent_cumulative_diff,
                ),
            )
        except BaseException:
            state.finalization_failures.append("incumbent_artifact")
            effective_terminal = "audit_failure"
            state.terminal_detail = None

    try:
        _replace_accounting_artifact(state, services)
    except BaseException:
        state.finalization_failures.append("accounting_artifact")
        effective_terminal = "audit_failure"
        state.terminal_detail = None

    result = _build_final_result(
        readiness=readiness,
        state=state,
        terminal_code=effective_terminal,
        cleanup=cleanup,
    )
    try:
        summary_artifact = services.write_json_artifact(
            "summary.json",
            result.to_public_artifact(),
        )
        _record_artifact(state, summary_artifact)
    except BaseException:
        state.finalization_failures.append("summary_artifact")
        return replace(
            result,
            status="aborted",
            terminal_code="audit_failure",
            terminal_detail=None,
            exit_code=1,
            cleanup_complete=False,
        )
    return replace(result, artifact_paths=tuple(state.artifact_paths))


def run_pit_optimizer_v3(
    *,
    readiness: PitOptimizerReadiness,
    services: PitOptimizerServices,
) -> PitOptimizerResult:
    if not isinstance(readiness, PitOptimizerReadiness):
        raise ValueError("optimizer run readiness is invalid")
    state = _new_run_state(readiness)
    terminal_code = "iteration_limit"
    try:
        _initialize_run_artifacts(readiness, state, services)
        while True:
            stop = _pre_iteration_stop(readiness, state, services)
            if stop is not None:
                terminal_code, terminal_detail = stop
                state.terminal_detail = terminal_detail
                break
            _prepare_iteration_source(readiness, state, services)
            iteration_directory = services.prepare_iteration_artifacts(
                state.next_iteration
            )
            if (
                not isinstance(iteration_directory, Path)
                or not iteration_directory.is_absolute()
                or not iteration_directory.is_dir()
            ):
                raise AuditFailure("iteration artifact directory is not durable")
            state.iterations_started += 1
            outcome = _run_iteration(readiness, state, services)
            if outcome.terminal_code is not None:
                terminal_code = outcome.terminal_code
                break
        _finish_discovery(readiness, state, services)
    except BaseException as exc:
        terminal_code, terminal_detail = _terminal_from_exception(exc)
        state.terminal_detail = terminal_detail
    return _finalize_result(readiness, state, services, terminal_code)


# Schema v4 deliberately lives beside the read-only v3 audit path.  It does not
# reuse v3 fold ranking or hidden-validation state.


@dataclass(frozen=True, slots=True)
class PitOptimizerReadinessV4:
    schema_version: int
    manifest: PitOptimizerRunManifestV4
    discovery_panel_plan: DiscoveryPanelPlan
    qualification_panel_plan: QualificationPanelPlan
    qualification_ledger_head_sha256: str
    readiness_sha256: str
    artifact_path: Path
    baseline_sources: tuple[AuthorSourceFile, ...]
    baseline_quick: PanelAggregateSummary
    baseline_discovery: PanelAggregateSummary
    seed_champion: SearchCandidateState | None = None
    seed_active_branch: SearchCandidateState | None = None

    def __post_init__(self) -> None:
        if self.schema_version != 4:
            raise ValueError("optimizer v4 readiness schema is unsupported")
        if not isinstance(self.manifest, PitOptimizerRunManifestV4):
            raise ValueError("optimizer v4 readiness manifest is invalid")
        if not isinstance(self.discovery_panel_plan, DiscoveryPanelPlan):
            raise ValueError("optimizer v4 readiness panel plan is invalid")
        self.manifest.validate_discovery_plan(self.discovery_panel_plan)
        if not isinstance(self.qualification_panel_plan, QualificationPanelPlan):
            raise ValueError("optimizer v4 readiness qualification plan is invalid")
        _panel_plan_pair_is_consistent(
            self.qualification_panel_plan,
            self.discovery_panel_plan,
        )
        if (
            not isinstance(self.artifact_path, Path)
            or not self.artifact_path.is_absolute()
            or len(self.readiness_sha256) != 64
            or len(self.qualification_ledger_head_sha256) != 64
        ):
            raise ValueError("optimizer v4 readiness artifact is invalid")
        baseline_identity = SelectedParentIdentity.issue(
            parent_kind="baseline",
            parent_id="baseline_policy",
            source_head=self.manifest.source_head,
            policy_sources=self.baseline_sources,
        )
        baseline_identity.validate_sources(self.baseline_sources)
        expected_hashes = tuple(
            (item.path, item.source_sha256) for item in self.baseline_sources
        )
        if (
            expected_hashes
            != self.manifest.policy_authoring_scope.initial_policy_source_sha256s
            or self.baseline_quick.panel_id != "quick"
            or self.baseline_quick.panel_sha256 != self.manifest.quick_panel_sha256
            or self.baseline_discovery.panel_id != "discovery"
            or self.baseline_discovery.panel_sha256
            != self.manifest.discovery_panel_sha256
        ):
            raise ValueError("optimizer v4 readiness baseline differs")
        for candidate in (self.seed_champion, self.seed_active_branch):
            if candidate is not None and (
                not isinstance(candidate, SearchCandidateState)
                or candidate.candidate_identity.source_commit
                != self.manifest.source_head
                or candidate.candidate_identity.discovery_panel_plan_sha256
                != self.manifest.discovery_panel_plan_sha256
                or candidate.discovery_evidence.panel_sha256
                != self.manifest.discovery_panel_sha256
                or candidate.quick_evidence is None
                or candidate.quick_evidence.panel_sha256
                != self.manifest.quick_panel_sha256
            ):
                raise ValueError("optimizer v4 resumed candidate was not reminted")


@dataclass(frozen=True, slots=True)
class CandidateParentV4:
    workspace: CandidateWorkspace
    cumulative_diff: str
    policy_sources: tuple[AuthorSourceFile, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, CandidateWorkspace):
            raise ValueError("optimizer v4 parent workspace is invalid")
        if not isinstance(self.cumulative_diff, str) or "\x00" in self.cumulative_diff:
            raise ValueError("optimizer v4 parent diff is invalid")
        if type(self.policy_sources) is not tuple or len(self.policy_sources) != 3:
            raise ValueError("optimizer v4 parent sources are invalid")


@dataclass(frozen=True, slots=True)
class CandidateValidationOutcomeV4:
    valid: bool
    failure_code: str | None
    cumulative_diff: str
    identity: CandidateIdentityV4 | None

    def __post_init__(self) -> None:
        if type(self.valid) is not bool or not isinstance(self.cumulative_diff, str):
            raise ValueError("optimizer v4 candidate validation is invalid")
        if self.valid != (self.identity is not None):
            raise ValueError("optimizer v4 candidate validation identity differs")
        if self.valid:
            if self.failure_code is not None or not isinstance(
                self.identity, CandidateIdentityV4
            ):
                raise ValueError("optimizer v4 valid candidate is inconsistent")
        elif not isinstance(self.failure_code, str) or not self.failure_code:
            raise ValueError("optimizer v4 invalid candidate lacks a failure code")


@dataclass(frozen=True, slots=True)
class PitOptimizerServicesV4:
    call_role: Callable[
        [
            PitOptimizerCallBudget,
            InvestigatorInputV4 | AuthorInputV4 | CriticInputV4,
            Callable[[str], object],
        ],
        PitOptimizerRoleAttempt,
    ]
    materialize_parent: Callable[
        [str, SearchCandidateState | None], CandidateParentV4
    ]
    validate_and_apply: Callable[
        [CandidateWorkspace, AuthorArtifactV4, SelectedParentIdentity],
        CandidateValidationOutcomeV4,
    ]
    evaluate_candidate: Callable[
        [CandidateWorkspace, CandidateIdentityV4, EvaluationPanelSpec],
        PanelAggregateSummary,
    ]
    dispose_candidate: Callable[[CandidateWorkspace], PitOptimizerCleanup]
    settle_invalid_investigator: Callable[
        [PitOptimizerCallBudget, tuple[PitOptimizerCallBudget, ...]],
        tuple[AuthorizationPlanSkip, ...],
    ]
    verify_inputs: Callable[[PitOptimizerReadinessV4], None]
    cancellation_requested: Callable[[], bool]
    prepare_iteration_artifacts: Callable[[int], Path]
    write_json_artifact: Callable[[str, Mapping[str, object]], tuple[Path, str]]
    write_diff_artifact: Callable[[str, str], tuple[Path, str]]
    finalize_run: Callable[
        [tuple[PitOptimizerRoleAttempt, ...], tuple[AuthorizationPlanSkip, ...], str],
        None,
    ]


@dataclass(frozen=True, slots=True)
class PitOptimizerResultV4:
    schema_version: int
    status: str
    terminal_code: str
    campaign_id: str
    target_cagr_pct: str
    baseline_cagr_pct: str
    champion_cagr_pct: str
    branch_cagr_pct: str | None
    iterations_started: int
    iterations_completed: int
    calls: int
    tokens: int
    cost_usd: str
    checkpoint_present: bool
    apply: bool
    cleanup_complete: bool
    source_modified: bool
    checkpoint: CampaignCheckpoint | None
    artifact_paths: tuple[tuple[Path, str], ...]

    def to_public_artifact(self) -> Mapping[str, object]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "terminal_reason": self.terminal_code,
            "campaign_id": self.campaign_id,
            "target_cagr_pct": self.target_cagr_pct,
            "baseline_cagr_pct": self.baseline_cagr_pct,
            "champion_cagr_pct": self.champion_cagr_pct,
            "branch_cagr_pct": self.branch_cagr_pct,
            "iterations": {
                "started": self.iterations_started,
                "completed": self.iterations_completed,
            },
            "accounting": {
                "calls": self.calls,
                "tokens": self.tokens,
                "cost_usd": self.cost_usd,
            },
            "checkpoint_present": self.checkpoint_present,
            "apply": self.apply,
            "cleanup": {
                "complete": self.cleanup_complete,
                "source_modified": self.source_modified,
            },
        }


def _panel_v4_artifact(value: PanelAggregateSummary) -> dict[str, object]:
    primitive = asdict(value)
    primitive["portfolio_annualized_return_pct"] = format(
        value.portfolio_annualized_return_pct,
        "f",
    )
    return primitive


def _target_v4_artifact(value: AnnualizedReturnTarget) -> dict[str, object]:
    primitive = asdict(value)
    primitive["target_pct"] = format(primitive["target_pct"], "f")
    primitive["milestones_pct"] = [
        format(item, "f") for item in primitive["milestones_pct"]
    ]
    primitive["precision_pct"] = format(primitive["precision_pct"], "f")
    return primitive


def _read_checkpoint_primitive(path: Path, expected_sha256: str) -> Mapping[str, object]:
    candidate = Path(path)
    if not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_file():
        raise ValueError("campaign checkpoint must be an absolute regular file")
    if len(expected_sha256) != 64:
        raise ValueError("campaign checkpoint SHA-256 is invalid")
    raw = candidate.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("campaign checkpoint digest differs")
    try:
        primitive = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("campaign checkpoint is invalid JSON") from exc
    if (
        not isinstance(primitive, dict)
        or raw != canonical_json_bytes(primitive)
        or primitive.get("schema_version") != 4
        or primitive.get("artifact_type") != "campaign_checkpoint"
    ):
        raise ValueError("campaign checkpoint is not canonical schema v4")
    return primitive


def prepare_pit_optimizer_v4(
    *,
    manifest: PitOptimizerRunManifestV4,
    discovery_panel_plan: DiscoveryPanelPlan,
    qualification_panel_plan: QualificationPanelPlan,
    qualification_ledger_snapshot: QualificationRetirementSnapshot,
    qualification_readiness_head_sha256: str | None = None,
    baseline_sources: tuple[AuthorSourceFile, ...],
    artifact_path: Path,
    evaluate_baseline: Callable[[EvaluationPanelSpec], PanelAggregateSummary],
    verify_inputs: Callable[[PitOptimizerRunManifestV4, DiscoveryPanelPlan], None],
    campaign_checkpoint_path: Path | None = None,
    campaign_checkpoint_sha256: str | None = None,
    restore_seed: Callable[
        [str, Mapping[str, object], Path, DiscoveryPanelPlan], SearchCandidateState
    ]
    | None = None,
) -> PitOptimizerReadinessV4:
    """Evaluate the unchanged policy on the exact quick/discovery panels."""

    if not isinstance(manifest, PitOptimizerRunManifestV4):
        raise ValueError("optimizer v4 manifest is invalid")
    manifest.validate_discovery_plan(discovery_panel_plan)
    _panel_plan_pair_is_consistent(
        qualification_panel_plan,
        discovery_panel_plan,
    )
    if (
        not isinstance(
            qualification_ledger_snapshot,
            QualificationRetirementSnapshot,
        )
        or qualification_ledger_snapshot.qualification_retirement_domain_id
        != discovery_panel_plan.qualification_retirement_domain_id
    ):
        raise ValueError("optimizer v4 qualification ledger snapshot differs")
    retired = frozenset(
        qualification_ledger_snapshot.retired_security_lineage_ids
    )
    committed_lineages = {
        item.security_lineage_id
        for panel in (
            discovery_panel_plan.quick_panel,
            discovery_panel_plan.discovery_panel,
            qualification_panel_plan.qualification_panel,
        )
        for item in panel.lineages
    }
    if retired.intersection(committed_lineages):
        raise ValueError("optimizer v4 panel contains a retired lineage")
    readiness_ledger_head = (
        qualification_ledger_snapshot.ledger_head_sha256
        if qualification_readiness_head_sha256 is None
        else qualification_readiness_head_sha256
    )
    if (
        not isinstance(readiness_ledger_head, str)
        or len(readiness_ledger_head) != 64
    ):
        raise ValueError("optimizer v4 readiness ledger head is invalid")
    verify_inputs(manifest, discovery_panel_plan)
    baseline_identity = SelectedParentIdentity.issue(
        parent_kind="baseline",
        parent_id="baseline_policy",
        source_head=manifest.source_head,
        policy_sources=baseline_sources,
    )
    baseline_identity.validate_sources(baseline_sources)
    quick = evaluate_baseline(discovery_panel_plan.quick_panel)
    discovery = evaluate_baseline(discovery_panel_plan.discovery_panel)
    if (
        quick.panel_id != "quick"
        or quick.panel_sha256 != manifest.quick_panel_sha256
        or discovery.panel_id != "discovery"
        or discovery.panel_sha256 != manifest.discovery_panel_sha256
    ):
        raise ValueError("optimizer v4 baseline evidence differs from exact panels")

    checkpoint_supplied = campaign_checkpoint_path is not None
    if checkpoint_supplied != (campaign_checkpoint_sha256 is not None):
        raise ValueError("campaign checkpoint path and digest must be supplied together")
    if manifest.seed_checkpoint_sha256 != campaign_checkpoint_sha256:
        raise ValueError("optimizer v4 manifest seed checkpoint differs")
    champion = None
    branch = None
    if campaign_checkpoint_path is not None:
        primitive = _read_checkpoint_primitive(
            campaign_checkpoint_path,
            campaign_checkpoint_sha256 or "",
        )
        if restore_seed is None:
            raise ValueError("campaign checkpoint restore capability is absent")
        for kind, key in (("champion", "champion"), ("branch", "active_branch")):
            candidate = primitive.get(key)
            if candidate is None:
                continue
            if not isinstance(candidate, Mapping):
                raise ValueError("campaign checkpoint candidate state is invalid")
            restored = restore_seed(
                kind,
                candidate,
                campaign_checkpoint_path.parent,
                discovery_panel_plan,
            )
            if (
                restored.candidate_identity.source_commit != manifest.source_head
                or restored.candidate_identity.discovery_panel_plan_sha256
                != manifest.discovery_panel_plan_sha256
                or restored.discovery_evidence.panel_sha256
                != manifest.discovery_panel_sha256
                or restored.quick_evidence is None
                or restored.quick_evidence.panel_sha256 != manifest.quick_panel_sha256
            ):
                raise ValueError("restored checkpoint candidate was not reminted and reevaluated")
            if kind == "champion":
                champion = restored
            else:
                branch = restored

    primitive = {
        "schema_version": 4,
        "artifact_type": "optimizer_readiness",
        "manifest_sha256": manifest.sha256,
        "discovery_panel_plan_sha256": discovery_panel_plan.sha256,
        "qualification_panel_plan_sha256": qualification_panel_plan.sha256,
        "qualification_ledger_head_sha256": (
            readiness_ledger_head
        ),
        "baseline_source_sha256s": [
            [item.path, item.source_sha256] for item in baseline_sources
        ],
        "baseline_quick": _panel_v4_artifact(quick),
        "baseline_discovery": _panel_v4_artifact(discovery),
        "seed_checkpoint_sha256": campaign_checkpoint_sha256,
        "seed_champion_identity": (
            None if champion is None else champion.candidate_identity.identity_sha256
        ),
        "seed_branch_identity": (
            None if branch is None else branch.candidate_identity.identity_sha256
        ),
        # Preparation has already reconstructed and evaluated a retained seed on
        # these exact panels.  Carry its canonical, digest-bound state in the
        # readiness artifact so the canary can reauthenticate the source and
        # remint its diff without needlessly evaluating the same seed again.
        "seed_champion_state": (
            None if champion is None else champion.to_primitive()
        ),
        "seed_branch_state": (
            None if branch is None else branch.to_primitive()
        ),
    }
    try:
        output, digest = write_create_only_json(Path(artifact_path), primitive)
    except FileExistsError:
        output = Path(artifact_path)
        raw = output.read_bytes()
        if raw != canonical_json_bytes(primitive):
            raise ValueError("optimizer v4 readiness artifact differs") from None
        digest = hashlib.sha256(raw).hexdigest()
    return PitOptimizerReadinessV4(
        schema_version=4,
        manifest=manifest,
        discovery_panel_plan=discovery_panel_plan,
        qualification_panel_plan=qualification_panel_plan,
        qualification_ledger_head_sha256=(
            readiness_ledger_head
        ),
        readiness_sha256=digest,
        artifact_path=output,
        baseline_sources=baseline_sources,
        baseline_quick=quick,
        baseline_discovery=discovery,
        seed_champion=champion,
        seed_active_branch=branch,
    )


@dataclass(slots=True)
class _V4RunState:
    champion: SearchCandidateState | None
    active_branch: SearchCandidateState | None
    feedback_tail: list[PriorHypothesisSummaryV4]
    selected_parent: SelectedParentIdentity | None = None
    call_attempts: list[PitOptimizerRoleAttempt] = field(default_factory=list)
    skips: list[AuthorizationPlanSkip] = field(default_factory=list)
    artifact_paths: list[tuple[Path, str]] = field(default_factory=list)
    iterations_started: int = 0
    iterations_completed: int = 0
    cleanup_observations: list[PitOptimizerCleanup] = field(default_factory=list)


def _v4_plan(
    readiness: PitOptimizerReadinessV4,
    *,
    iteration: int,
    role: str,
) -> PitOptimizerCallBudget:
    role_offset = {"investigator": 1, "author": 2, "critic": 3}[role]
    call_index = (iteration - 1) * 3 + role_offset
    plans = readiness.manifest.call_budgets
    if call_index > len(plans):
        raise AuthorizationExhausted("optimizer v4 call plan is exhausted")
    plan = plans[call_index - 1]
    if (plan.call_index, plan.iteration, plan.role) != (
        call_index,
        iteration,
        role,
    ):
        raise AuthorizationExhausted("optimizer v4 plan slot differs")
    return plan


def _record_v4_artifact(
    state: _V4RunState,
    artifact: tuple[Path, str],
) -> tuple[Path, str]:
    path, digest = artifact
    if not isinstance(path, Path) or not path.is_absolute() or len(digest) != 64:
        raise AuditFailure("optimizer v4 artifact writer returned invalid evidence")
    state.artifact_paths.append((path, digest))
    return path, digest


def _v4_attempt_artifact(attempt: PitOptimizerRoleAttempt) -> dict[str, object]:
    payload = attempt.payload
    return {
        "schema_version": 4,
        "artifact_type": (
            "role_output_invalid" if payload is None else "role_output"
        ),
        "plan": attempt.plan.to_primitive(),
        "provider_facts": asdict(attempt.facts),
        "payload": None if payload is None else payload.to_primitive(),
    }


def _write_v4_attempt(
    *,
    state: _V4RunState,
    services: PitOptimizerServicesV4,
    attempt: PitOptimizerRoleAttempt,
) -> None:
    state.call_attempts.append(attempt)
    suffix = (
        f"{attempt.plan.role}_output_invalid.json"
        if attempt.payload is None
        else f"{attempt.plan.role}.json"
    )
    _record_v4_artifact(
        state,
        services.write_json_artifact(
            f"iterations/{attempt.plan.iteration:03d}/{suffix}",
            _v4_attempt_artifact(attempt),
        ),
    )
    _record_v4_artifact(
        state,
        services.write_json_artifact(
            "accounting.json",
            _v4_accounting_artifact(state),
        ),
    )


def _v4_accounting_artifact(state: _V4RunState) -> dict[str, object]:
    facts = tuple(attempt.facts for attempt in state.call_attempts)
    return {
        "schema_version": 4,
        "artifact_type": "optimizer_accounting",
        "attempts": [
            {
                "call_index": attempt.plan.call_index,
                "iteration": attempt.plan.iteration,
                "role": attempt.plan.role,
                "outcome": attempt.facts.outcome,
                "request_started": attempt.facts.request_started,
                "accounting_complete": attempt.facts.accounting_complete,
                "prompt_tokens": attempt.facts.prompt_tokens,
                "completion_tokens": attempt.facts.completion_tokens,
                "total_tokens": attempt.facts.total_tokens,
                "cost_usd": attempt.facts.cost_usd,
            }
            for attempt in state.call_attempts
        ],
        "skips": [skip.to_record() for skip in state.skips],
        "totals": {
            "calls": sum(1 for item in facts if item.request_started),
            "prompt_tokens": sum(item.prompt_tokens or 0 for item in facts),
            "completion_tokens": sum(item.completion_tokens or 0 for item in facts),
            "total_tokens": sum(item.total_tokens or 0 for item in facts),
            "cost_usd": format(
                sum(
                    (
                        Decimal(str(item.cost_usd or 0))
                        for item in facts
                        if item.accounting_complete
                    ),
                    Decimal("0"),
                ),
                "f",
            ),
        },
        "accounting_complete": all(item.accounting_complete for item in facts),
    }


def _call_v4_role(
    *,
    state: _V4RunState,
    services: PitOptimizerServicesV4,
    plan: PitOptimizerCallBudget,
    role_input: InvestigatorInputV4 | AuthorInputV4 | CriticInputV4,
    parser: Callable[[str], object],
) -> PitOptimizerRoleAttempt:
    attempt = services.call_role(plan, role_input, parser)
    if not isinstance(attempt, PitOptimizerRoleAttempt) or attempt.plan != plan:
        raise ProviderProtocolFailure("optimizer v4 provider attempt differs from plan")
    if attempt.payload is not None:
        validator = getattr(role_input, "validate_artifact", None)
        if callable(validator):
            validator(attempt.payload)
        elif not isinstance(attempt.payload, InvestigatorArtifactV4):
            raise ProviderProtocolFailure("optimizer v4 investigator payload differs")
    _write_v4_attempt(state=state, services=services, attempt=attempt)
    return attempt


def _baseline_parent_summary(
    readiness: PitOptimizerReadinessV4,
) -> SelectedParentSummary:
    identity = SelectedParentIdentity.issue(
        parent_kind="baseline",
        parent_id="baseline_policy",
        source_head=readiness.manifest.source_head,
        policy_sources=readiness.baseline_sources,
    )
    return SelectedParentSummary(
        identity=identity,
        hypothesis_id="baseline_policy",
        behavioral_summary="Authenticated unchanged baseline policy.",
        quick_panel=readiness.baseline_quick,
        discovery_panel=readiness.baseline_discovery,
    )


def _candidate_parent_summary(
    *,
    readiness: PitOptimizerReadinessV4,
    kind: str,
    candidate: SearchCandidateState,
    sources: tuple[AuthorSourceFile, ...],
) -> SelectedParentSummary:
    if candidate.quick_evidence is None:
        raise IdentityDrift("retained candidate lacks exact quick evidence")
    candidate_identity = candidate.candidate_identity
    try:
        validate_candidate_identity(candidate_identity)
    except ValueError as exc:
        raise IdentityDrift("retained candidate identity is unauthenticated") from exc
    actual_source_sha256s = tuple(
        (item.path, item.source_sha256) for item in sources
    )
    if actual_source_sha256s != candidate_identity.editable_file_sha256s:
        raise IdentityDrift("retained candidate sources differ from identity")
    identity = SelectedParentIdentity.issue(
        parent_kind=kind,
        parent_id=f"candidate_{candidate.candidate_identity.identity_sha256}",
        source_head=readiness.manifest.source_head,
        policy_sources=sources,
    )
    identity.validate_sources(sources)
    return SelectedParentSummary(
        identity=identity,
        hypothesis_id=candidate.hypothesis,
        behavioral_summary=candidate.behavioral_summary,
        quick_panel=candidate.quick_evidence,
        discovery_panel=candidate.discovery_evidence,
    )


def _selected_parent_kind(state: _V4RunState) -> str:
    if state.active_branch is not None:
        return "branch"
    if state.champion is not None:
        return "champion"
    return "baseline"


def _dispose_v4(
    services: PitOptimizerServicesV4,
    state: _V4RunState,
    workspace: CandidateWorkspace | None,
) -> None:
    if workspace is None:
        return
    cleanup = services.dispose_candidate(workspace)
    if not isinstance(cleanup, PitOptimizerCleanup):
        raise SandboxIntegrityFailure("optimizer v4 cleanup evidence is invalid")
    state.cleanup_observations.append(cleanup)


def _materialize_v4_context(
    *,
    readiness: PitOptimizerReadinessV4,
    state: _V4RunState,
    services: PitOptimizerServicesV4,
) -> tuple[
    str,
    SearchCandidateState | None,
    CandidateParentV4,
    SelectedParentSummary,
    SelectedParentSummary,
    SelectedParentSummary | None,
    SelectedParentSummary | None,
]:
    kind = _selected_parent_kind(state)
    selected_candidate = (
        state.active_branch
        if kind == "branch"
        else state.champion if kind == "champion" else None
    )
    selected_workspace: CandidateWorkspace | None = None
    try:
        selected = services.materialize_parent(kind, selected_candidate)
        if not isinstance(selected, CandidateParentV4):
            raise SandboxIntegrityFailure("optimizer v4 parent materialization is invalid")
        selected_workspace = selected.workspace
        baseline = _baseline_parent_summary(readiness)
        if kind == "baseline":
            if selected.policy_sources != readiness.baseline_sources:
                raise IdentityDrift("optimizer v4 baseline sources differ")
            selected_summary = baseline
        else:
            assert selected_candidate is not None
            if (
                hashlib.sha256(selected.cumulative_diff.encode("utf-8")).hexdigest()
                != selected_candidate.candidate_identity.cumulative_diff_sha256
            ):
                raise IdentityDrift("retained candidate diff differs from identity")
            selected_summary = _candidate_parent_summary(
                readiness=readiness,
                kind=kind,
                candidate=selected_candidate,
                sources=selected.policy_sources,
            )
        champion_summary: SelectedParentSummary | None = None
        if state.champion is not None:
            if kind == "champion":
                champion_summary = selected_summary
            else:
                champion_parent = services.materialize_parent("champion", state.champion)
                if not isinstance(champion_parent, CandidateParentV4):
                    raise SandboxIntegrityFailure(
                        "optimizer v4 champion materialization is invalid"
                    )
                try:
                    if (
                        hashlib.sha256(
                            champion_parent.cumulative_diff.encode("utf-8")
                        ).hexdigest()
                        != state.champion.candidate_identity.cumulative_diff_sha256
                    ):
                        raise IdentityDrift(
                            "retained champion diff differs from identity"
                        )
                    champion_summary = _candidate_parent_summary(
                        readiness=readiness,
                        kind="champion",
                        candidate=state.champion,
                        sources=champion_parent.policy_sources,
                    )
                finally:
                    _dispose_v4(services, state, champion_parent.workspace)
        branch_summary = selected_summary if kind == "branch" else None
        state.selected_parent = selected_summary.identity
        return (
            kind,
            selected_candidate,
            selected,
            selected_summary,
            baseline,
            champion_summary,
            branch_summary,
        )
    except BaseException:
        _dispose_v4(services, state, selected_workspace)
        raise


def _checkpoint_v4(
    *,
    readiness: PitOptimizerReadinessV4,
    completed_iterations: int,
    champion: SearchCandidateState | None,
    active_branch: SearchCandidateState | None,
    feedback_tail: Sequence[PriorHypothesisSummaryV4],
) -> CampaignCheckpoint:
    return CampaignCheckpoint(
        schema_version=4,
        artifact_type="campaign_checkpoint",
        campaign_id=readiness.manifest.campaign_id,
        campaign_sequence=readiness.manifest.campaign_sequence,
        source_head=readiness.manifest.source_head,
        source_fingerprint_sha256=readiness.manifest.source_fingerprint_sha256,
        discovery_panel_plan_sha256=readiness.manifest.discovery_panel_plan_sha256,
        completed_iterations=completed_iterations,
        champion=champion,
        active_branch=active_branch,
        feedback_tail=tuple(item.to_primitive() for item in feedback_tail),
    )


def _persist_v4_transition(
    *,
    readiness: PitOptimizerReadinessV4,
    state: _V4RunState,
    services: PitOptimizerServicesV4,
    iteration: int,
    decision: Mapping[str, object],
    champion: SearchCandidateState | None,
    active_branch: SearchCandidateState | None,
    feedback_tail: Sequence[PriorHypothesisSummaryV4],
) -> CampaignCheckpoint:
    checkpoint = _checkpoint_v4(
        readiness=readiness,
        completed_iterations=iteration,
        champion=champion,
        active_branch=active_branch,
        feedback_tail=feedback_tail,
    )
    # This ordering is the crash boundary: the complete prospective decision,
    # then the atomically replaced checkpoint, and only then live state.
    _record_v4_artifact(
        state,
        services.write_json_artifact(
            f"iterations/{iteration:03d}/decision.json",
            {"schema_version": 4, **dict(decision)},
        ),
    )
    _record_v4_artifact(
        state,
        services.write_json_artifact("checkpoint.json", checkpoint.to_primitive()),
    )
    return checkpoint


def _invalid_investigator_feedback(iteration: int) -> PriorHypothesisSummaryV4:
    return PriorHypothesisSummaryV4(
        iteration=iteration,
        hypothesis_id=f"invalid_investigator_{iteration}",
        focus_areas=("entry",),
        behavioral_summary="Investigator output was rejected by the closed schema.",
        validation=CandidateValidationStatusV4(
            status="not_evaluated",
            failure_code=None,
        ),
        discovery_cagr_pct=None,
        critic_disposition="abandon",
    )


def _candidate_failure_code(value: str | None) -> str:
    allowed = {
        "author_output_invalid",
        "source_invalid",
        "syntax_failed",
        "imports_failed",
        "purity_failed",
        "determinism_failed",
        "allocation_constraints_failed",
        "worker_failed",
        "typed_decision_invalid",
        "runtime_invalid",
        "evaluation_failed",
    }
    return value if value in allowed else "source_invalid"


def _v4_role_common(
    readiness: PitOptimizerReadinessV4,
    *,
    iteration: int,
    parent_identity: SelectedParentIdentity,
) -> dict[str, object]:
    manifest = readiness.manifest
    return {
        "schema_version": 4,
        "iteration": iteration,
        "run_manifest_sha256": manifest.sha256,
        "policy_authoring_scope_sha256": manifest.policy_authoring_scope.sha256,
        "policy_interface_version": manifest.policy_interface_version,
        "immutable_constraint_ids": manifest.immutable_constraint_ids,
        "annualized_return_target": manifest.annualized_return_target,
        "discovery_panel_plan_sha256": manifest.discovery_panel_plan_sha256,
        "quick_panel_sha256": manifest.quick_panel_sha256,
        "discovery_panel_sha256": manifest.discovery_panel_sha256,
        "selected_parent_identity": parent_identity,
    }


def _write_validation_v4(
    *,
    state: _V4RunState,
    services: PitOptimizerServicesV4,
    iteration: int,
    status: CandidateValidationStatusV4,
    identity: CandidateIdentityV4 | None,
) -> None:
    _record_v4_artifact(
        state,
        services.write_json_artifact(
            f"iterations/{iteration:03d}/validation.json",
            {
                "schema_version": 4,
                "artifact_type": "candidate_validation",
                "status": status.to_primitive(),
                "candidate_identity": (
                    None if identity is None else identity.to_primitive()
                ),
            },
        ),
    )


def _run_v4_iteration(
    *,
    readiness: PitOptimizerReadinessV4,
    state: _V4RunState,
    services: PitOptimizerServicesV4,
    iteration: int,
) -> CampaignCheckpoint:
    manifest = readiness.manifest
    selected_workspace: CandidateWorkspace | None = None
    (
        selected_kind,
        selected_candidate,
        parent,
        selected_summary,
        baseline_summary,
        champion_summary,
        branch_summary,
    ) = _materialize_v4_context(
        readiness=readiness,
        state=state,
        services=services,
    )
    selected_workspace = parent.workspace
    try:
        common = _v4_role_common(
            readiness,
            iteration=iteration,
            parent_identity=selected_summary.identity,
        )
        champion_panel = (
            readiness.baseline_discovery
            if champion_summary is None
            else champion_summary.discovery_panel
        )
        investigator_input = InvestigatorInputV4(
            **common,  # type: ignore[arg-type]
            selected_parent_source_bundle_sha256=(
                selected_summary.identity.source_bundle_sha256
            ),
            selected_parent_sources=parent.policy_sources,
            selected_parent_summary=selected_summary,
            baseline_summary=baseline_summary,
            champion_summary=champion_summary,
            branch_summary=branch_summary,
            target_progress=TargetProgressV4.from_summaries(
                target=manifest.annualized_return_target,
                baseline=readiness.baseline_discovery,
                selected_parent=selected_summary.discovery_panel,
                champion=champion_panel,
            ),
            prior_hypotheses=tuple(state.feedback_tail),
            validation_status=CandidateValidationStatusV4(
                status="not_evaluated",
                failure_code=None,
            ),
        )
        investigator_plan = _v4_plan(
            readiness,
            iteration=iteration,
            role="investigator",
        )
        investigator_input.validate_budget(
            investigator_plan,
            scope=manifest.policy_authoring_scope,
            manifest=manifest,
        )
        investigator_attempt = _call_v4_role(
            state=state,
            services=services,
            plan=investigator_plan,
            role_input=investigator_input,
            parser=lambda raw: InvestigatorArtifactV4.from_json(
                raw,
                max_total_bytes=investigator_plan.max_response_bytes,
            ),
        )
        if investigator_attempt.payload is None:
            remaining = (
                _v4_plan(readiness, iteration=iteration, role="author"),
                _v4_plan(readiness, iteration=iteration, role="critic"),
            )
            skips = services.settle_invalid_investigator(
                investigator_plan,
                remaining,
            )
            if (
                type(skips) is not tuple
                or tuple(skip.call_index for skip in skips)
                != tuple(plan.call_index for plan in remaining)
            ):
                raise AuditFailure("optimizer v4 invalid-investigator skips differ")
            state.skips.extend(skips)
            _record_v4_artifact(
                state,
                services.write_json_artifact(
                    "accounting.json",
                    _v4_accounting_artifact(state),
                ),
            )
            _record_v4_artifact(
                state,
                services.write_json_artifact(
                    f"iterations/{iteration:03d}/authorization_skips.json",
                    {
                        "schema_version": 4,
                        "artifact_type": "authorization_plan_skips",
                        "iteration": iteration,
                        "skips": [skip.to_record() for skip in skips],
                    },
                ),
            )
            feedback = _invalid_investigator_feedback(iteration)
            prospective_feedback = (*state.feedback_tail, feedback)
            checkpoint = _persist_v4_transition(
                readiness=readiness,
                state=state,
                services=services,
                iteration=iteration,
                decision={
                    "artifact_type": "search_transition",
                    "selected_parent_kind": selected_kind,
                    "candidate_valid": False,
                    "candidate_promoted": False,
                    "critic_disposition": "abandon",
                    "effective_disposition": "unchanged",
                    "prospective_champion": (
                        None
                        if state.champion is None
                        else state.champion.to_primitive()
                    ),
                    "prospective_active_branch": (
                        None
                        if state.active_branch is None
                        else state.active_branch.to_primitive()
                    ),
                },
                champion=state.champion,
                active_branch=state.active_branch,
                feedback_tail=prospective_feedback,
            )
            state.feedback_tail = list(prospective_feedback)
            state.iterations_completed = iteration
            return checkpoint

        investigator = investigator_attempt.payload
        if not isinstance(investigator, InvestigatorArtifactV4):
            raise ProviderProtocolFailure("optimizer v4 investigator type differs")
        author_plan = _v4_plan(readiness, iteration=iteration, role="author")
        author_input = AuthorInputV4(
            **common,  # type: ignore[arg-type]
            selected_parent_source_bundle_sha256=(
                selected_summary.identity.source_bundle_sha256
            ),
            selected_parent_sources=parent.policy_sources,
            investigator=investigator,
        )
        author_input.validate_budget(
            author_plan,
            scope=manifest.policy_authoring_scope,
            manifest=manifest,
        )
        author_attempt = _call_v4_role(
            state=state,
            services=services,
            plan=author_plan,
            role_input=author_input,
            parser=lambda raw: AuthorArtifactV4.from_json(
                raw,
                selected_parent=selected_summary.identity,
                max_total_bytes=author_plan.max_response_bytes,
            ),
        )

        candidate_state: SearchCandidateState | None = None
        candidate_identity: CandidateIdentityV4 | None = None
        quick: PanelAggregateSummary | None = None
        discovery: PanelAggregateSummary | None = None
        source_artifact: tuple[Path, str] | None = None
        diff_artifact: tuple[Path, str] | None = None
        quick_artifact: tuple[Path, str] | None = None
        discovery_artifact: tuple[Path, str] | None = None
        if author_attempt.payload is None:
            validation_status = CandidateValidationStatusV4(
                status="invalid",
                failure_code="author_output_invalid",
            )
            author_manifest = None
            validation_code = author_attempt.facts.response_validation_code
            if validation_code is None:
                raise AuditFailure("invalid author attempt lacks validation code")
            author_invalid = RoleOutputInvalidSummary(
                iteration=iteration,
                call_index=author_plan.call_index,
                role="author",
                validation_code=validation_code,
            )
            behavioral_summary = "Author output was rejected by the closed schema."
        else:
            author = author_attempt.payload
            if not isinstance(author, AuthorArtifactV4):
                raise ProviderProtocolFailure("optimizer v4 author type differs")
            author_manifest = AuthorManifestSummaryV4.from_artifact(
                author,
                selected_parent=selected_summary.identity,
            )
            author_invalid = None
            behavioral_summary = author.behavioral_summary
            outcome = services.validate_and_apply(
                parent.workspace,
                author,
                selected_summary.identity,
            )
            if not isinstance(outcome, CandidateValidationOutcomeV4):
                raise SandboxIntegrityFailure("optimizer v4 validation evidence is invalid")
            if not outcome.valid:
                validation_status = CandidateValidationStatusV4(
                    status="invalid",
                    failure_code=_candidate_failure_code(outcome.failure_code),
                )
            else:
                candidate_identity = outcome.identity
                assert candidate_identity is not None
                if (
                    candidate_identity.parent_identity_sha256
                    != selected_summary.identity.parent_identity_sha256
                    or candidate_identity.discovery_panel_plan_sha256
                    != manifest.discovery_panel_plan_sha256
                ):
                    raise IdentityDrift("optimizer v4 candidate identity differs")
                diff_artifact = _record_v4_artifact(
                    state,
                    services.write_diff_artifact(
                        f"iterations/{iteration:03d}/candidate.diff",
                        outcome.cumulative_diff,
                    ),
                )
                source_artifact = _record_v4_artifact(
                    state,
                    services.write_json_artifact(
                        f"iterations/{iteration:03d}/candidate-source.json",
                        {
                            "schema_version": 4,
                            "artifact_type": "candidate_source_bundle",
                            "candidate_identity_sha256": candidate_identity.identity_sha256,
                            "policy_sources": {
                                item.path: item.source for item in author.policy_sources
                            },
                        },
                    ),
                )
                try:
                    quick = services.evaluate_candidate(
                        parent.workspace,
                        candidate_identity,
                        readiness.discovery_panel_plan.quick_panel,
                    )
                    if (
                        quick.panel_id != "quick"
                        or quick.panel_sha256 != manifest.quick_panel_sha256
                    ):
                        raise CandidateEvaluationFailure("replay_failed")
                    quick_artifact = _record_v4_artifact(
                        state,
                        services.write_json_artifact(
                            f"iterations/{iteration:03d}/quick.json",
                            {
                                "schema_version": 4,
                                "artifact_type": "candidate_panel_evidence",
                                "candidate_identity_sha256": candidate_identity.identity_sha256,
                                "evidence": _panel_v4_artifact(quick),
                            },
                        ),
                    )
                    # Quick return/trades never gate discovery.
                    discovery = services.evaluate_candidate(
                        parent.workspace,
                        candidate_identity,
                        readiness.discovery_panel_plan.discovery_panel,
                    )
                    if (
                        discovery.panel_id != "discovery"
                        or discovery.panel_sha256 != manifest.discovery_panel_sha256
                    ):
                        raise CandidateEvaluationFailure("replay_failed")
                    discovery_artifact = _record_v4_artifact(
                        state,
                        services.write_json_artifact(
                            f"iterations/{iteration:03d}/discovery.json",
                            {
                                "schema_version": 4,
                                "artifact_type": "candidate_panel_evidence",
                                "candidate_identity_sha256": candidate_identity.identity_sha256,
                                "evidence": _panel_v4_artifact(discovery),
                            },
                        ),
                    )
                    validation_status = CandidateValidationStatusV4(
                        status="valid",
                        failure_code=None,
                    )
                except CandidateEvaluationFailure as exc:
                    validation_status = CandidateValidationStatusV4(
                        status="invalid",
                        failure_code=exc.failure_code,
                    )
                    # Preserve a completed quick panel for critic feedback when
                    # only the larger discovery evaluation failed operationally.
                    if quick_artifact is None:
                        quick = None
                    discovery = None

        _write_validation_v4(
            state=state,
            services=services,
            iteration=iteration,
            status=validation_status,
            identity=(
                candidate_identity if validation_status.status == "valid" else None
            ),
        )
        critic_plan = _v4_plan(readiness, iteration=iteration, role="critic")
        critic_input = CriticInputV4(
            **common,  # type: ignore[arg-type]
            selected_parent_summary=selected_summary,
            hypothesis_id=investigator.hypothesis_id,
            investigator_summary=investigator,
            author_manifest=author_manifest,
            author_output_invalid=author_invalid,
            validation_status=validation_status,
            candidate_quick=quick,
            candidate_discovery=discovery,
            baseline_quick=readiness.baseline_quick,
            baseline_discovery=readiness.baseline_discovery,
            champion_discovery=champion_panel,
            target_progress=TargetProgressV4.from_summaries(
                target=manifest.annualized_return_target,
                baseline=readiness.baseline_discovery,
                selected_parent=selected_summary.discovery_panel,
                champion=champion_panel,
            ),
        )
        critic_input.validate_budget(
            critic_plan,
            scope=manifest.policy_authoring_scope,
            manifest=manifest,
        )
        critic_attempt = _call_v4_role(
            state=state,
            services=services,
            plan=critic_plan,
            role_input=critic_input,
            parser=lambda raw: CriticArtifactV4.from_json(
                raw,
                max_total_bytes=critic_plan.max_response_bytes,
            ),
        )
        critic = critic_attempt.payload
        critic_valid = isinstance(critic, CriticArtifactV4)
        disposition = critic.disposition if critic_valid else "abandon"

        if validation_status.status == "valid":
            assert (
                candidate_identity is not None
                and quick is not None
                and discovery is not None
                and source_artifact is not None
                and diff_artifact is not None
                and quick_artifact is not None
                and discovery_artifact is not None
            )
            candidate_state = SearchCandidateState(
                candidate_identity=candidate_identity,
                cumulative_diff_artifact=(
                    f"iterations/{iteration:03d}/candidate.diff"
                ),
                cumulative_diff_sha256=diff_artifact[1],
                source_bundle_artifact=(
                    f"iterations/{iteration:03d}/candidate-source.json"
                ),
                source_bundle_sha256=source_artifact[1],
                discovery_evidence_artifact=(
                    f"iterations/{iteration:03d}/discovery.json"
                ),
                discovery_evidence_sha256=discovery_artifact[1],
                discovery_evidence=discovery,
                hypothesis=investigator.hypothesis_id,
                behavioral_summary=behavioral_summary,
                originating_run_id=manifest.campaign_id,
                originating_iteration=iteration,
                quick_evidence_artifact=f"iterations/{iteration:03d}/quick.json",
                quick_evidence_sha256=quick_artifact[1],
                quick_evidence=quick,
            )

        current_winner_cagr = (
            readiness.baseline_discovery.portfolio_annualized_return_pct
            if state.champion is None
            else state.champion.discovery_evidence.portfolio_annualized_return_pct
        )
        promoted = bool(
            candidate_state is not None
            and candidate_state.discovery_evidence.portfolio_annualized_return_pct
            > current_winner_cagr
        )
        if promoted:
            prospective_champion = candidate_state
            prospective_branch = None
            effective_disposition = "promote"
        elif candidate_state is not None and disposition == "refine":
            prospective_champion = state.champion
            prospective_branch = candidate_state
            effective_disposition = "refine"
        elif candidate_state is None and disposition == "refine" and selected_kind == "branch":
            prospective_champion = state.champion
            prospective_branch = selected_candidate
            effective_disposition = "refine_existing_branch"
        else:
            prospective_champion = state.champion
            prospective_branch = None
            effective_disposition = "abandon"

        feedback = PriorHypothesisSummaryV4(
            iteration=iteration,
            hypothesis_id=investigator.hypothesis_id,
            focus_areas=investigator.focus_areas,
            behavioral_summary=behavioral_summary,
            validation=validation_status,
            discovery_cagr_pct=(
                None
                if candidate_state is None
                else candidate_state.discovery_evidence.portfolio_annualized_return_pct
            ),
            critic_disposition=disposition,
        )
        prospective_feedback = (*state.feedback_tail, feedback)
        checkpoint = _persist_v4_transition(
            readiness=readiness,
            state=state,
            services=services,
            iteration=iteration,
            decision={
                "artifact_type": "search_transition",
                "selected_parent_kind": selected_kind,
                "candidate_valid": candidate_state is not None,
                "candidate_promoted": promoted,
                "critic_output_valid": critic_valid,
                "critic_disposition": disposition,
                "effective_disposition": effective_disposition,
                "prospective_champion": (
                    None
                    if prospective_champion is None
                    else prospective_champion.to_primitive()
                ),
                "prospective_active_branch": (
                    None
                    if prospective_branch is None
                    else prospective_branch.to_primitive()
                ),
            },
            champion=prospective_champion,
            active_branch=prospective_branch,
            feedback_tail=prospective_feedback,
        )
        state.champion = prospective_champion
        state.active_branch = prospective_branch
        state.feedback_tail = list(prospective_feedback)
        state.iterations_completed = iteration
        return checkpoint
    finally:
        _dispose_v4(services, state, selected_workspace)


def _copy_seed_v4(
    *,
    kind: str,
    candidate: SearchCandidateState,
    state: _V4RunState,
    services: PitOptimizerServicesV4,
) -> SearchCandidateState:
    parent = services.materialize_parent(kind, candidate)
    label = "champion" if kind == "champion" else "branch"
    try:
        diff = _record_v4_artifact(
            state,
            services.write_diff_artifact(
                f"seed-{label}.diff",
                parent.cumulative_diff,
            ),
        )
        source = _record_v4_artifact(
            state,
            services.write_json_artifact(
                f"seed-{label}-source.json",
                {
                    "schema_version": 4,
                    "artifact_type": "seed_candidate_source_bundle",
                    "candidate_identity_sha256": (
                        candidate.candidate_identity.identity_sha256
                    ),
                    "policy_sources": {
                        item.path: item.source for item in parent.policy_sources
                    },
                },
            ),
        )
        quick = _record_v4_artifact(
            state,
            services.write_json_artifact(
                f"seed-{label}-quick.json",
                {
                    "schema_version": 4,
                    "artifact_type": "seed_candidate_panel_evidence",
                    "evidence": _panel_v4_artifact(candidate.quick_evidence),
                },
            ),
        )
        discovery = _record_v4_artifact(
            state,
            services.write_json_artifact(
                f"seed-{label}-discovery.json",
                {
                    "schema_version": 4,
                    "artifact_type": "seed_candidate_panel_evidence",
                    "evidence": _panel_v4_artifact(candidate.discovery_evidence),
                },
            ),
        )
        return replace(
            candidate,
            cumulative_diff_artifact=f"seed-{label}.diff",
            cumulative_diff_sha256=diff[1],
            source_bundle_artifact=f"seed-{label}-source.json",
            source_bundle_sha256=source[1],
            discovery_evidence_artifact=f"seed-{label}-discovery.json",
            discovery_evidence_sha256=discovery[1],
            quick_evidence_artifact=f"seed-{label}-quick.json",
            quick_evidence_sha256=quick[1],
        )
    finally:
        _dispose_v4(services, state, parent.workspace)


def run_pit_optimizer_v4(
    *,
    readiness: PitOptimizerReadinessV4,
    services: PitOptimizerServicesV4,
) -> PitOptimizerResultV4:
    """Run the bounded schema-v4 search without qualification or replay."""

    if not isinstance(readiness, PitOptimizerReadinessV4) or not isinstance(
        services,
        PitOptimizerServicesV4,
    ):
        raise ValueError("optimizer v4 run composition is invalid")
    state = _V4RunState(
        champion=None,
        active_branch=None,
        feedback_tail=[],
    )
    terminal_code = "iteration_limit"
    checkpoint: CampaignCheckpoint | None = None
    durable_checkpoint_written = False
    try:
        services.verify_inputs(readiness)
        seed_champion: SearchCandidateState | None = None
        seed_active_branch: SearchCandidateState | None = None
        if readiness.seed_champion is not None:
            seed_champion = _copy_seed_v4(
                kind="champion",
                candidate=readiness.seed_champion,
                state=state,
                services=services,
            )
        if readiness.seed_active_branch is not None:
            seed_active_branch = _copy_seed_v4(
                kind="branch",
                candidate=readiness.seed_active_branch,
                state=state,
                services=services,
            )
        for name, primitive in (
            (
                "run.json",
                {
                    "schema_version": 4,
                    "artifact_type": "optimizer_run",
                    "campaign_id": readiness.manifest.campaign_id,
                    "campaign_sequence": readiness.manifest.campaign_sequence,
                    "manifest_sha256": readiness.manifest.sha256,
                    "readiness_sha256": readiness.readiness_sha256,
                    "target": _target_v4_artifact(
                        readiness.manifest.annualized_return_target
                    ),
                    "apply": False,
                    "provider_retries": 0,
                    "qualification_started": False,
                    "full_replay_started": False,
                },
            ),
            (
                "baseline.json",
                {
                    "schema_version": 4,
                    "artifact_type": "exact_panel_baseline",
                    "quick": _panel_v4_artifact(readiness.baseline_quick),
                    "discovery": _panel_v4_artifact(readiness.baseline_discovery),
                    "parity_use": "engine_equivalence_only",
                },
            ),
            ("accounting.json", _v4_accounting_artifact(state)),
        ):
            _record_v4_artifact(
                state,
                services.write_json_artifact(name, primitive),
            )
        initial_checkpoint = _checkpoint_v4(
            readiness=readiness,
            completed_iterations=0,
            champion=seed_champion,
            active_branch=seed_active_branch,
            feedback_tail=state.feedback_tail,
        )
        _record_v4_artifact(
            state,
            services.write_json_artifact(
                "checkpoint.json", initial_checkpoint.to_primitive()
            ),
        )
        checkpoint = initial_checkpoint
        durable_checkpoint_written = True
        # The imported seed becomes live only after all create-only copies and the
        # initial atomic checkpoint are durable.
        state.champion = seed_champion
        state.active_branch = seed_active_branch
        for iteration in range(1, readiness.manifest.max_iterations + 1):
            if services.cancellation_requested():
                terminal_code = "cancelled"
                break
            directory = services.prepare_iteration_artifacts(iteration)
            if (
                not isinstance(directory, Path)
                or not directory.is_absolute()
                or not directory.is_dir()
            ):
                raise AuditFailure("optimizer v4 iteration directory is not durable")
            state.iterations_started = iteration
            checkpoint = _run_v4_iteration(
                readiness=readiness,
                state=state,
                services=services,
                iteration=iteration,
            )
            durable_checkpoint_written = True
    except BaseException as exc:
        terminal_code, _detail = _terminal_from_exception(exc)
        if terminal_code in {
            "iteration_limit",
            "stagnation_limit",
            "cancelled",
        }:
            terminal_code = "audit_failure"
    try:
        services.verify_inputs(readiness)
    except BaseException:
        terminal_code = "identity_drift"
    facts = tuple(attempt.facts for attempt in state.call_attempts)
    cleanup_complete = all(
        item.candidate_removed and item.worker_stopped and not item.source_modified
        for item in state.cleanup_observations
    )
    source_modified = any(item.source_modified for item in state.cleanup_observations)
    accounting_complete = all(item.accounting_complete for item in facts)
    if not accounting_complete:
        terminal_code = "provider_accounting_failure"
    successful_terminal = terminal_code in {"iteration_limit", "cancelled"}
    if successful_terminal and not cleanup_complete:
        terminal_code = "sandbox_integrity_failure"
        successful_terminal = False
    champion_cagr = (
        readiness.baseline_discovery.portfolio_annualized_return_pct
        if state.champion is None
        else state.champion.discovery_evidence.portfolio_annualized_return_pct
    )
    branch_cagr = (
        None
        if state.active_branch is None
        else state.active_branch.discovery_evidence.portfolio_annualized_return_pct
    )
    result = PitOptimizerResultV4(
        schema_version=4,
        status="completed" if successful_terminal else "aborted",
        terminal_code=terminal_code,
        campaign_id=readiness.manifest.campaign_id,
        target_cagr_pct=format(
            readiness.manifest.annualized_return_target.target_pct,
            "f",
        ),
        baseline_cagr_pct=format(
            readiness.baseline_discovery.portfolio_annualized_return_pct,
            "f",
        ),
        champion_cagr_pct=format(champion_cagr, "f"),
        branch_cagr_pct=None if branch_cagr is None else format(branch_cagr, "f"),
        iterations_started=state.iterations_started,
        iterations_completed=state.iterations_completed,
        calls=sum(1 for item in facts if item.request_started),
        tokens=sum(item.total_tokens or 0 for item in facts),
        cost_usd=format(
            sum(
                (
                    Decimal(str(item.cost_usd or 0))
                    for item in facts
                    if item.accounting_complete
                ),
                Decimal("0"),
            ),
            "f",
        ),
        checkpoint_present=durable_checkpoint_written,
        apply=False,
        cleanup_complete=cleanup_complete,
        source_modified=source_modified,
        checkpoint=checkpoint,
        artifact_paths=tuple(state.artifact_paths),
    )
    try:
        _record_v4_artifact(
            state,
            services.write_json_artifact("summary.json", result.to_public_artifact()),
        )
    except BaseException:
        result = replace(
            result,
            status="aborted",
            terminal_code="audit_failure",
            cleanup_complete=False,
        )
    try:
        services.finalize_run(
            tuple(state.call_attempts),
            tuple(state.skips),
            result.terminal_code,
        )
    except BaseException:
        result = replace(
            result,
            status="aborted",
            terminal_code="audit_failure",
            cleanup_complete=False,
        )
    return replace(result, artifact_paths=tuple(state.artifact_paths))
