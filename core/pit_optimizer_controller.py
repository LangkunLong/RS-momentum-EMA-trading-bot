"""Provider-free orchestration for the schema-v2 PIT optimizer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Callable, Mapping
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
from core.pit_optimizer_artifacts import canonical_json_bytes, write_create_only_json
from core.pit_optimizer_authorization import (
    AuthorizationRunLease,
    FrozenModelPricing,
    PitOptimizerProviderFacts,
    PitOptimizerRoleCall,
)
from core.pit_optimizer_candidate import (
    CandidateIdentity,
    build_policy_source_bundle,
    require_source_context_fit,
    validate_author_manifest,
    validate_candidate_identity,
)
from core.pit_optimizer_evaluation import (
    AggregateMetric,
    DeterminismAttestation,
    DiscoveryEvaluation,
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
    _source_identity,
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
        if self.schema_version != 2:
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


@dataclass(frozen=True, slots=True)
class OptimizerBudgetSummary:
    api_calls: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    authoritative_usd: float
    retained_reservation_tokens: int
    retained_reservation_usd: float
    incomplete_accounting_calls: int


@dataclass(frozen=True, slots=True)
class _ProviderAttemptRecord:
    """Closed facts for one authorized attempt, with a payload only on acceptance."""

    plan: PitOptimizerCallBudget
    facts: PitOptimizerProviderFacts
    payload_sha256: str | None


@dataclass(frozen=True, slots=True)
class PitOptimizerServices:
    freeze_pricing: Callable[[str], FrozenModelPricing]
    open_run_lease: Callable[[PitOptimizerReadiness, FrozenModelPricing], AuthorizationRunLease]
    close_run_lease: Callable[[AuthorizationRunLease, str], None]
    call_role: Callable[
        [
            PitOptimizerCallBudget,
            InvestigatorInput | AuthorInput | CriticInput,
            Callable[[str], object],
            AuthorizationRunLease,
            FrozenModelPricing,
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
    frozen_pricing: FrozenModelPricing | None
    authorization_lease: AuthorizationRunLease | None
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
        self._entries: dict[str, tuple[CandidateWorkspace, object]] = {}

    def create_candidate(self, cumulative_diff: str | None) -> CandidateWorkspace:
        capability = self._create_capability(cumulative_diff)
        root = getattr(capability, "root", None)
        if not isinstance(root, Path) or not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise RuntimeError("candidate capability root is invalid")
        workspace = CandidateWorkspace(
            workspace_id=f"workspace_{uuid.uuid4().hex}",
            root=root.resolve(),
        )
        self._entries[workspace.workspace_id] = (workspace, capability)
        return workspace

    def _resolve(self, workspace: CandidateWorkspace) -> object:
        if not isinstance(workspace, CandidateWorkspace):
            raise RuntimeError("unknown candidate workspace")
        entry = self._entries.get(workspace.workspace_id)
        if entry is None or entry[0] != workspace:
            raise RuntimeError("unknown candidate workspace")
        return entry[1]

    def validate_and_apply(
        self,
        workspace: CandidateWorkspace,
        artifact: AuthorArtifact,
        cumulative_diff: str | None,
    ) -> CandidateValidationOutcome:
        capability = self._resolve(workspace)
        return self._validate_capability(capability, artifact, cumulative_diff)

    def dispose_candidate(self, workspace: CandidateWorkspace) -> PitOptimizerCleanup:
        capability = self._resolve(workspace)
        # Revoke before invoking cleanup so even a failing disposer cannot be retried
        # through a stale root reference.
        self._entries.pop(workspace.workspace_id)
        return self._dispose_capability(capability)


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
        "schema_version": 2,
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
    fold = manifest.fold_manifest.discovery_folds[fold_index]
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


def prepare_pit_optimizer_v2(
    config: PitOptimizerGateConfig,
    *,
    source_root: Path,
    artifact_root: Path,
    permanent_runtime_root: Path,
    source_head: str,
    source_fingerprint_sha256: str,
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
    actual_source_head, actual_source_fingerprint = _source_identity(source)
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
        schema_version=2,
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
    state.frozen_pricing = pricing
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
    if state.authorization_lease is None or state.frozen_pricing is None:
        raise RuntimeError("provider capability is not initialized")
    folds = (*manifest.fold_manifest.discovery_folds, manifest.fold_manifest.hidden_fold)
    return {
        "schema_version": 2,
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
        "frozen_pricing_sha256": state.frozen_pricing.pricing_sha256,
        "status": "initialized",
    }


def _baseline_artifact(readiness: PitOptimizerReadiness) -> dict[str, object]:
    manifest = readiness.manifest
    return {
        "schema_version": 2,
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
    lease = state.authorization_lease or state.lease_snapshot
    return {
        "schema_version": 2,
        "call_records": [],
        "authorized_totals": {
            "calls": 0 if lease is None else lease.max_calls,
            "tokens": 0 if lease is None else lease.max_tokens,
            "usd": 0.0 if lease is None else lease.max_usd,
        },
        "reserved_totals": {"calls": 0, "tokens": 0, "usd": 0.0},
        "actual_totals": {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "usd": 0.0,
        },
        "incomplete_exposure": {
            "calls": 0,
            "retained_tokens": 0,
            "retained_usd": 0.0,
        },
        "audit_chain_head": "0" * 64,
    }


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
        "schema_version": 2,
        "call_index": call.plan.call_index,
        "iteration": call.plan.iteration,
        "role": call.plan.role,
        "payload_sha256": _payload_sha256(payload),
        "payload": primitive(),
    }


def _budget_summary_from_calls(
    calls: list[PitOptimizerRoleCall] | list[_ProviderAttemptRecord],
) -> OptimizerBudgetSummary:
    facts = [item.facts for item in calls]
    return OptimizerBudgetSummary(
        api_calls=sum(1 for item in facts if item.request_started),
        prompt_tokens=sum(item.prompt_tokens or 0 for item in facts),
        completion_tokens=sum(item.completion_tokens or 0 for item in facts),
        total_tokens=sum(item.total_tokens or 0 for item in facts),
        authoritative_usd=float(sum(Decimal(str(item.cost_usd or 0)) for item in facts)),
        retained_reservation_tokens=sum(item.retained_reservation_tokens for item in facts),
        retained_reservation_usd=float(
            sum(Decimal(str(item.retained_reservation_usd)) for item in facts)
        ),
        incomplete_accounting_calls=sum(1 for item in facts if not item.accounting_complete),
    )


def _current_accounting_artifact(state: _RunState) -> dict[str, object]:
    attempts = state.provider_attempts
    summary = _budget_summary_from_calls(attempts)
    lease = state.authorization_lease or state.lease_snapshot
    reserved_tokens = sum(
        item.plan.max_input_tokens + item.plan.max_output_tokens
        for item in attempts
    )
    reserved_usd = float(
        sum(Decimal(str(item.plan.max_usd)) for item in attempts)
    )
    return {
        "schema_version": 2,
        "call_records": [
            {
                "plan": item.plan.to_primitive(),
                "payload_sha256": item.payload_sha256,
                "facts": asdict(item.facts),
            }
            for item in attempts
        ],
        "authorized_totals": {
            "calls": 0 if lease is None else lease.max_calls,
            "tokens": 0 if lease is None else lease.max_tokens,
            "usd": 0.0 if lease is None else lease.max_usd,
        },
        "reserved_totals": {
            "calls": len(attempts),
            "tokens": reserved_tokens,
            "usd": reserved_usd,
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
            "retained_usd": summary.retained_reservation_usd,
        },
        "audit_chain_head": (
            "0" * 64 if not attempts else attempts[-1].facts.audit_sha256
        ),
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
    pricing = state.frozen_pricing
    if pricing is None or facts.frozen_pricing_sha256 != pricing.pricing_sha256:
        raise AuditFailure("provider attempt facts differ from frozen pricing")


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
    if state.authorization_lease is None or state.frozen_pricing is None:
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
            state.frozen_pricing,
        )
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
        try:
            validate_author_manifest(author.payload, outcome.identity)  # type: ignore[arg-type]
        except ValueError as exc:
            raise IdentityDrift("author manifest differs from candidate identity") from exc
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
        "schema_version": 2,
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
    supplied = services.evaluate_discovery(state.iteration_workspace, validation.identity)
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
    if any(item.closed_trades < 1 for item in candidate_folds):
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
                    "schema_version": 2,
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
        return None
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
            incumbent_score = discovery_score_from_folds(
                baseline_folds,
                baseline_folds,
                original_baseline_sha256=baseline_sha256,
                expected_original_baseline_sha256=baseline_sha256,
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
                "schema_version": 2,
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


def _critic_comparison(
    discovery: DiscoveryEvaluation,
    *,
    fixed: bool,
) -> CandidateComparisonSummary:
    score = (
        discovery.comparison.candidate_vs_fixed_baseline
        if fixed
        else discovery.comparison.candidate_vs_incumbent_diagnostics
    )
    return CandidateComparisonSummary(
        folds=tuple(item.aggregate_metrics for item in discovery.folds),
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
    role_input = CriticInput(
        schema_version=2,
        iteration=state.next_iteration,
        run_manifest_sha256=readiness.manifest_sha256,
        immutable_constraint_ids=readiness.manifest.immutable_constraint_ids,
        hypothesis_id=investigator.payload.hypothesis_id,
        investigator_summary=investigator.payload,
        author_manifest=AuthorManifestSummary(
            hypothesis_id=author.payload.hypothesis_id,
            behavioral_summary=author.payload.behavioral_summary,
            changed_paths=author.payload.changed_paths,
            changed_symbols=author.payload.changed_symbols,
        ),
        validation=_validation_summary(validation, state.evaluation_failure_code),
        candidate_vs_baseline=(
            None if discovery is None else _critic_comparison(discovery, fixed=True)
        ),
        candidate_vs_incumbent=(
            None if discovery is None else _critic_comparison(discovery, fixed=False)
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
            evidence_ids=tuple(
                hashlib.sha256(
                    canonical_json_bytes({"aggregate": asdict(item)})
                ).hexdigest()
                for item in candidate_folds
            ),
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
                "schema_version": 2,
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
        services.dispose_candidate(prior_workspace)
    elif (
        not improves
        and candidate_workspace is not None
        and candidate_workspace != state.incumbent_workspace
    ):
        services.dispose_candidate(candidate_workspace)
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
    if _budget_summary_from_calls(state.provider_attempts).incomplete_accounting_calls:
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
    spent = _budget_summary_from_calls(state.provider_attempts)
    if lease.max_calls - spent.api_calls < 3:
        return "budget_exhausted", "call_budget_exhausted"
    required_tokens = sum(
        item.max_input_tokens + item.max_output_tokens for item in next_plans
    )
    charged_tokens = spent.total_tokens + spent.retained_reservation_tokens
    if lease.max_tokens - charged_tokens < required_tokens:
        return "budget_exhausted", "token_budget_exhausted"
    required_usd = sum(Decimal(str(item.max_usd)) for item in next_plans)
    charged_usd = Decimal(str(spent.authoritative_usd)) + Decimal(
        str(spent.retained_reservation_usd)
    )
    if Decimal(str(lease.max_usd)) - charged_usd < required_usd:
        return "budget_exhausted", "cost_budget_exhausted"
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
    if isinstance(exc, ContextBudgetExhausted):
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
        frozen_pricing=None,
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
                    "schema_version": 2,
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
    for workspace in (state.iteration_workspace, state.incumbent_workspace):
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
    state.iteration_source_bundle = None
    state.prospective_source_bundle = None
    return PitOptimizerCleanup(
        candidate_removed=all(item.candidate_removed for item in observations),
        worker_stopped=all(item.worker_stopped for item in observations),
        source_modified=any(item.source_modified for item in observations),
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
    budget = _budget_summary_from_calls(state.provider_attempts)
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
        schema_version=2,
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


def run_pit_optimizer_v2(
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
        repeated = _finish_discovery(readiness, state, services)
        if repeated is not None:
            _run_hidden_once(readiness, state, services)
    except BaseException as exc:
        terminal_code, terminal_detail = _terminal_from_exception(exc)
        state.terminal_detail = terminal_detail
    return _finalize_result(readiness, state, services, terminal_code)
