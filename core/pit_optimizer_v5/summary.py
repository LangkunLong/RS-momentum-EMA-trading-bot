"""Content-free operator summaries for PIT optimizer V5."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, get_args

from core.pit_optimizer_v5.artifacts import LocalArtifactRepositoryV5
from core.pit_optimizer_v5.contracts import (
    CampaignManifestV5,
    CampaignPanelPlanV5,
    EvaluatorContractV5,
    canonical_primitive_v5,
)
from core.pit_optimizer_v5.memory import (
    CleanupResultPayloadV5,
    RoleCompletionPayloadV5,
    RoundOutcomePayloadV5,
    RuntimeFailureAuthorityV5,
    StoredExperimentRecordV5,
)
from core.pit_optimizer_v5.production_runtime import LocalArchiveReducerFactoryV5
from core.pit_optimizer_v5.provider import sum_cost_usd_v5
from core.pit_optimizer_v5.search import BaselineParentAuthorityV5
from core.pit_optimizer_v5.runtime import RuntimeFailureCodeV5, RuntimeStageV5
from core.pit_optimizer_v5.selection import reported_champion_v5


V5SummaryStatus = Literal["ready", "running", "completed", "failed", "unavailable"]
_READINESS_CODES = frozenset(
    {
        "artifact_graph_invalid",
        "artifact_root_unavailable",
        "campaign_authority_invalid",
        "completed",
        "import_campaign_authority_unavailable",
        "internal_failure",
        "invalid_request",
        "production_authority_mismatch",
        "production_config_invalid",
        "production_config_missing",
        "production_dependency_invalid",
        "production_provider_invalid",
        "ready",
        "resume_state_missing",
        "run_not_fresh",
        "runtime_failed",
        "runtime_result_invalid",
        "verified",
    }
)


@dataclass(frozen=True, slots=True)
class OptimizerSummaryV5:
    schema_version: Literal[5]
    command: Literal["run", "run-fixture", "resume", "verify-run", "summarize", "import-v4-candidate"]
    status: V5SummaryStatus
    readiness_code: str
    rounds_seen: int
    terminal_rounds: int
    role_calls: int
    total_tokens: int
    cost_usd: Decimal
    checkpoint_generation: int
    experiments: int
    evaluated_experiments: int
    best_campaign_cagr_pct: Decimal | None
    target_gap_pct: Decimal | None
    cleanup_complete: bool | None
    execution_profile_sha256: str | None = None
    target_pct: Decimal | None = None
    behaviorally_distinct_variants: int = 0
    archive_families: int = 0
    typed_failures: tuple[str, ...] = ()
    source_unchanged: bool | None = None
    qualification_started: bool = False
    replay_started: bool = False
    evaluation_mode: Literal["synthetic_fixture", "production"] = "production"

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 5:
            raise ValueError("optimizer summary schema is invalid")
        if self.command not in {"run", "run-fixture", "resume", "verify-run", "summarize", "import-v4-candidate"}:
            raise ValueError("optimizer summary command is invalid")
        if self.status not in {"ready", "running", "completed", "failed", "unavailable"}:
            raise ValueError("optimizer summary status is invalid")
        if self.readiness_code not in _READINESS_CODES:
            raise ValueError("optimizer summary readiness code is invalid")
        for value in (
            self.rounds_seen,
            self.terminal_rounds,
            self.role_calls,
            self.total_tokens,
            self.checkpoint_generation,
            self.experiments,
            self.evaluated_experiments,
            self.behaviorally_distinct_variants,
            self.archive_families,
        ):
            if type(value) is not int or value < 0:
                raise ValueError("optimizer summary count is invalid")
        if type(self.cost_usd) is not Decimal or not self.cost_usd.is_finite() or self.cost_usd < 0:
            raise ValueError("optimizer summary cost is invalid")
        for value in (self.best_campaign_cagr_pct, self.target_gap_pct, self.target_pct):
            if value is not None and (type(value) is not Decimal or not value.is_finite()):
                raise ValueError("optimizer summary metric is invalid")
        if self.cleanup_complete is not None and type(self.cleanup_complete) is not bool:
            raise ValueError("optimizer summary cleanup state is invalid")
        if self.source_unchanged is not None and type(self.source_unchanged) is not bool:
            raise ValueError("optimizer summary source state is invalid")
        if type(self.qualification_started) is not bool or type(self.replay_started) is not bool:
            raise ValueError("optimizer summary stage state is invalid")
        if self.evaluation_mode not in {"synthetic_fixture", "production"}:
            raise ValueError("optimizer summary evaluation mode is invalid")
        if self.execution_profile_sha256 is not None and (
            type(self.execution_profile_sha256) is not str
            or len(self.execution_profile_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.execution_profile_sha256)
        ):
            raise ValueError("optimizer summary execution profile identity is invalid")
        permitted_failures = {
            f"{stage}:{code}" for stage in get_args(RuntimeStageV5) for code in get_args(RuntimeFailureCodeV5)
        }
        if type(self.typed_failures) is not tuple or any(
            item not in permitted_failures for item in self.typed_failures
        ):
            raise ValueError("optimizer summary failure is outside the closed taxonomy")

    def to_primitive(self) -> dict[str, object]:
        return canonical_primitive_v5(self)  # type: ignore[return-value]


def unavailable_summary_v5(
    command: Literal["run", "run-fixture", "resume", "verify-run", "summarize", "import-v4-candidate"],
    readiness_code: str,
) -> OptimizerSummaryV5:
    return OptimizerSummaryV5(
        5,
        command,
        "unavailable",
        readiness_code,
        0,
        0,
        0,
        0,
        Decimal("0"),
        0,
        0,
        0,
        None,
        None,
        None,
    )


def summarize_repository_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    manifest: CampaignManifestV5,
    command: Literal["run", "run-fixture", "resume", "verify-run", "summarize", "import-v4-candidate"],
    readiness_code: str = "ready",
) -> OptimizerSummaryV5:
    """Project only non-sensitive counts and aggregate performance metrics."""

    if type(repository) is not LocalArtifactRepositoryV5 or type(manifest) is not CampaignManifestV5:
        raise ValueError("optimizer summary authority is invalid")
    rounds_seen = 0
    terminal_rounds = 0
    role_calls = 0
    total_tokens = 0
    cost_usd = Decimal("0")
    cleanups: dict[int, CleanupResultPayloadV5] = {}
    runtime_failed = False
    failures = set()
    for round_index in range(1, manifest.search.max_feedback_rounds + 1):
        events = repository.load_round_events(campaign_id=manifest.campaign_id, round_index=round_index)
        if not events:
            continue
        rounds_seen += 1
        for event in events:
            payload = repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
            if type(payload) is RoleCompletionPayloadV5:
                package = repository.load_role_invocation(payload)
                role_calls += package.attempt.usage.external_attempt_count
                total_tokens += package.attempt.usage.total_tokens
                cost_usd = sum_cost_usd_v5(cost_usd, package.attempt.usage.cost_usd)
            elif type(payload) is RoundOutcomePayloadV5:
                terminal_rounds += 1
                runtime_failed |= type(payload.authority) is RuntimeFailureAuthorityV5
                if type(payload.authority) is RuntimeFailureAuthorityV5:
                    failures.add(f"{payload.authority.stage}:{payload.authority.failure_code}")
            elif type(payload) is CleanupResultPayloadV5:
                cleanups[round_index] = payload
    checkpoint = repository.load_checkpoint()
    records = () if checkpoint is None else tuple(repository.load_experiment(ref) for ref in checkpoint.record_refs)
    evaluated = tuple(item for item in records if item.status == "evaluated" and item.campaign_evidence is not None)
    panel_plan = repository.load_typed_artifact(manifest.panel_plan_ref, value_type=CampaignPanelPlanV5)
    evaluator_contract = repository.load_typed_artifact(manifest.evaluator_contract_ref, value_type=EvaluatorContractV5)
    baseline = repository.load_typed_artifact(manifest.baseline_authority_ref, value_type=BaselineParentAuthorityV5)
    state = LocalArchiveReducerFactoryV5(repository).verify_projection(
        manifest=manifest,
        panel_plan=panel_plan,
        evaluator_contract=evaluator_contract,
    )
    stored = (
        ()
        if checkpoint is None
        else tuple(
            StoredExperimentRecordV5(ref, record) for ref, record in zip(checkpoint.record_refs, records, strict=True)
        )
    )
    best = reported_champion_v5(
        state=state,
        baseline=baseline,
        discovery_plan=panel_plan,
        evaluator_contract=evaluator_contract,
        stored_records=stored,
    ).campaign_cagr_pct
    gap = None if best is None else manifest.target.target_pct - best
    cleanup_complete = (
        None
        if not cleanups
        else len(cleanups) == rounds_seen and all(item.cleanup_complete for item in cleanups.values())
    )
    source_unchanged = None
    if manifest.provider is None and rounds_seen:
        from core.pit_optimizer_v5.fixture_runtime import verify_fixture_run_v5
        from core.pit_optimizer_v5.candidate_ir import SourceBundleV5

        verify_fixture_run_v5(repository=repository, manifest=manifest)
        source_unchanged = (
            repository.load_typed_artifact(baseline.source_bundle_ref, value_type=SourceBundleV5)
            == baseline.source_bundle
        )
    status: V5SummaryStatus = (
        "failed"
        if runtime_failed
        else "completed"
        if (checkpoint is not None or terminal_rounds > 0) and cleanup_complete is True
        else "failed"
        if terminal_rounds > 0 and cleanup_complete is False
        else "running"
        if rounds_seen
        else "ready"
    )
    return OptimizerSummaryV5(
        5,
        command,
        status,
        "runtime_failed" if runtime_failed else readiness_code,
        rounds_seen,
        terminal_rounds,
        role_calls,
        total_tokens,
        cost_usd,
        0 if checkpoint is None else checkpoint.generation,
        len(records),
        len(evaluated),
        best,
        gap,
        cleanup_complete,
        manifest.execution_profile_ref.sha256,
        manifest.target.target_pct,
        len(
            {
                record.semantic_fingerprint.fingerprint_sha256
                for record in records
                if record.semantic_fingerprint is not None
                and record.status not in {"invalid", "exact_duplicate", "behavioral_equivalent", "sibling_equivalent"}
            }
        ),
        len({entry.primary_mechanism for entry in state.archive.entries}),
        tuple(sorted(failures)),
        source_unchanged,
        False,
        False,
        "synthetic_fixture" if manifest.provider is None else "production",
    )


__all__ = ["OptimizerSummaryV5", "summarize_repository_v5", "unavailable_summary_v5"]
