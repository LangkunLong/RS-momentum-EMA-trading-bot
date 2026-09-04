# PIT Optimizer V5 Learning and Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a provider-neutral V5 loop that preserves complete critic learning, turns one model-authored strategy template into several local candidates, and retains a diverse top-K archive ranked by transparent campaign CAGR.

**Architecture:** Implement V5 as a focused package beside the V4 monolith. A pure search domain consumes typed experiment results and emits deterministic archive transitions; separate adapters own source rendering, behavior probes, provider accounting, artifacts, workspaces, and evaluation. Every quick survivor completes all four chronological discovery episodes in the same round, and the critic reviews the resulting candidate batch once.

**Tech Stack:** Python 3.13, frozen dataclasses and Protocols, Decimal, AST validation, canonical JSON, existing Git/Docker/provider adapters behind interfaces, atomic local artifacts, pytest, and Ruff.

**Spec:** `docs/superpowers/specs/2026-09-04-pit-optimizer-v5-hybrid-architecture-design.md`

## Global Constraints

- Do not append V5 types and control flow to the existing V4 contract/controller monoliths.
- The pure domain must not import `agent_loop`, OpenRouter, Git, Docker, subprocess, or filesystem implementations.
- Preserve all V2-V4 source, manifest, checkpoint, authorization, and artifact readers.
- Keep annualized portfolio CAGR as the primary selection objective; do not create a weighted composite score.
- Require complete evidence from four non-overlapping chronological discovery episodes before a candidate can enter the archive.
- Rotate episode execution order deterministically by round, but canonicalize evidence by episode ordinal before scoring.
- Use pairwise-disjoint stratified security cohorts across the four discovery episodes.
- Default to three ranked investigator hypotheses, one authored structural template, one to four literal axes, at most twelve local variants, at most six discovery survivors per template, and archive capacity eight. Validate every variable-length role artifact against its manifest capability instead of baking the default into its Python type.
- Reject invalid, exact-duplicate, and behaviorally equivalent candidates before portfolio ranking;
  require non-zero activity across the complete four-episode campaign before archive admission. A
  zero-trade quick screen alone is not a terminal rejection.
- Keep full-source authoring as an explicit escape mode; use changed functions/constants by default.
- Keep provider retry and schema-repair capabilities explicit and defaulted to zero.
- Persist complete learning locally even when provider-facing context is compacted.
- Keep qualification evidence out of search memory.
- Add two focused V5 test modules; do not expand the broad legacy optimizer suites.
- Do not make live provider calls, open qualification, or run a replay in this plan.

## File Structure and Responsibility Map

- `core/pit_optimizer_v5/contracts.py`: V5 closed contracts and configurable target/search/provider values.
- `core/pit_optimizer_v5/candidate_ir.py`: templates, literal axes, assignments, and source-only policy identity.
- `core/pit_optimizer_v5/rendering.py`: deterministic changed-symbol and full-source rendering.
- `core/pit_optimizer_v5/probes.py`: versioned semantic behavior suite for all six V3 policy methods.
- `core/pit_optimizer_v5/memory.py`: immutable experiment journal and bounded role-context projection.
- `core/pit_optimizer_v5/artifacts.py`: V5 path grammar, create-only evidence, and atomic state.
- `core/pit_optimizer_v5/search.py`: campaign CAGR and pure archive transition.
- `core/pit_optimizer_v5/selection.py`: parent/hypothesis scheduling and archive policy.
- `core/pit_optimizer_v5/provider.py`: provider-neutral calls, typed failures, and authorization/accounting adapter.
- `core/pit_optimizer_v5/sandbox.py`: digest-pinned, network-disabled Docker evaluation adapter.
- `core/pit_optimizer_v5/workspace.py`: campaign-owned disposable materialization and cleanup adapter.
- `core/pit_optimizer_v5/runtime.py`: dependency-injected round orchestration.
- `core/pit_optimizer_v5/cli.py`: V5 command composition.
- `core/pit_optimizer_v5/summary.py`: content-free status and evidence projection.
- `agent_loop.py`: backwards-compatible dispatch into the V5 CLI.
- `tests/test_pit_optimizer_v5_domain.py`: focused contracts/rendering/probes/search/memory checks.
- `tests/test_pit_optimizer_v5_runtime.py`: focused provider/runtime/restart checks.

---

### Task 1: Establish the V5 Package and Closed Contracts

**Files:**

- Modify: `core/pit_optimizer_v5/__init__.py`
- Modify: `core/pit_optimizer_v5/contracts.py`
- Create: `tests/test_pit_optimizer_v5_domain.py`

**Interfaces:**

- Consumes: `EvaluationReportV5` and evaluator identities from the evaluator-truth phase.
- Produces: configurable campaign, role, hypothesis, template, experiment, and critic contracts.

- [ ] **Step 1: Add failing contract round-trip checks**

Require targets `10.00`, `20.00`, `50.00`, and another positive two-decimal target to round-trip.
Require defaults of three hypotheses, twelve variants, six discovery survivors, archive capacity
eight, ten feedback rounds, four discovery episodes, two parallel evaluations, the declared stage
and wall timeouts, zero retries, and zero schema repairs.

- [ ] **Step 2: Define target and search configuration**

```python
@dataclass(frozen=True, slots=True)
class AnnualizedReturnTargetV5:
    target_pct: Decimal
    metric_id: Literal["portfolio_annualized_return_pct"]
    basis: Literal["absolute"]

@dataclass(frozen=True, slots=True)
class SearchCapabilitiesV5:
    hypotheses_per_investigator: int = 3
    max_tunable_axes: int = 4
    max_variants_per_template: int = 12
    max_discovery_survivors_per_template: int = 6
    archive_capacity: int = 8
    max_feedback_rounds: int = 10
    allow_full_source_escape: bool = True

@dataclass(frozen=True, slots=True)
class ProviderCapabilitiesV5:
    model: str
    maximum_role_calls: int
    maximum_total_tokens: int
    maximum_output_tokens_per_role: int
    maximum_usd: Decimal | None = None
    automatic_retries: int = 0
    schema_repair_calls: int = 0

@dataclass(frozen=True, slots=True)
class ArtifactRefV5:
    relative_path: str
    sha256: str

@dataclass(frozen=True, slots=True)
class EpisodePlanV5:
    episode_id: str
    episode_ordinal: int | None
    purpose: str
    start_date: str
    end_date: str
    lineage_ids: tuple[str, ...]
    panel_ref: ArtifactRefV5

@dataclass(frozen=True, slots=True)
class CampaignPanelPlanV5:
    schema_version: Literal[5]
    pit_bundle_ref: ArtifactRefV5
    prices_provenance_ref: ArtifactRefV5
    partition_seed_sha256: str
    target_sha256: str
    mechanics: EpisodePlanV5
    quick: EpisodePlanV5
    discovery: tuple[EpisodePlanV5, ...]
    confirmation_plan_sha256: str
    qualification_plan_sha256: str

@dataclass(frozen=True, slots=True)
class ResourceCapabilitiesV5:
    max_parallel_evaluations: int = 2
    evaluation_cpu_limit: Decimal = Decimal("1")
    evaluation_memory_mib: int = 1024
    evaluation_output_limit_bytes: int = 67_108_864
    policy_method_timeout_seconds: int = 1
    worker_startup_timeout_seconds: int = 30
    role_call_timeout_seconds: int = 180
    mechanics_timeout_seconds: int = 60
    quick_timeout_seconds: int = 180
    discovery_episode_timeout_seconds: int = 600
    round_wall_timeout_seconds: int = 1800
    campaign_wall_timeout_seconds: int = 18000
    cleanup_timeout_seconds: int = 60

@dataclass(frozen=True, slots=True)
class CampaignManifestV5:
    schema_version: Literal[5]
    campaign_id: str
    target: AnnualizedReturnTargetV5
    search: SearchCapabilitiesV5
    provider: ProviderCapabilitiesV5 | None
    resources: ResourceCapabilitiesV5
    artifact_root: Literal[".artifacts/pit-optimizer-v5"]
    execution_profile_ref: ArtifactRefV5
    evaluator_contract_ref: ArtifactRefV5
    baseline_authority_ref: ArtifactRefV5
    panel_plan_ref: ArtifactRefV5
    policy_scope_ref: ArtifactRefV5
    baseline_policy_revision_ref: ArtifactRefV5
    sandbox_profile_ref: ArtifactRefV5
    source_commit: str
    apply: Literal[False]
    qualification_allowed: Literal[False]
    full_replay_allowed: Literal[False]
```

Do not reference `AnnualizedReturnTarget.production()` anywhere in the V5 package.

Define these shared measured-result records in the same module:

```python
@dataclass(frozen=True, slots=True)
class MetricPredictionV5:
    metric_id: str
    direction: Literal["increase", "decrease", "unchanged"]
    rationale: str

@dataclass(frozen=True, slots=True)
class ValidationResultV5:
    valid: bool
    failure_code: str | None
    changed_symbols: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class EpisodeEvaluationV5:
    episode_id: str
    episode_ordinal: int
    start_date: str
    end_date: str
    evaluation: PanelEvaluationV5

@dataclass(frozen=True, slots=True)
class CampaignEvidenceV5:
    discovery_plan_sha256: str
    episodes: tuple[EpisodeEvaluationV5, ...]
    campaign_cagr_pct: Decimal
    closed_trades: int
```

Every `ArtifactRefV5.relative_path` names one regular file using a canonical POSIX path relative to
the authenticated repository's `.artifacts/pit-optimizer-v5` root. Reject absolute paths, `..`,
alternate separators, directories, symlinks, and targets outside that root; authenticate bytes
before deserializing them. Multi-file evidence uses a canonical index whose own child references are
walked recursively. Manifest verification walks the complete reference graph and reports a typed
missing, relocated, cycle, or digest-mismatch failure, so `run` and `resume` need only the manifest
path and never rely on hidden controller paths.

`EpisodePlanV5.panel_ref` resolves the complete serialized `EvaluationPanelSpec` (sessions and their
digest, dates, ordered security-lineage records, reference series, and causal feature scope).
Validation requires the episode's duplicated ID/date/lineage fields to equal the authenticated panel
bytes exactly. Require four discovery plans with ordinals `(1, 2, 3, 4)`. Campaign Task 1 later
implements their deterministic builder and the controller-only held-out plan types; search contracts
do not depend on that later implementation.

`EpisodeEvaluationV5` validates that its ordinal/date fields equal the authenticated panel it wraps.
`CampaignEvidenceV5` requires ordinals `(1, 2, 3, 4)`, pairwise-disjoint date intervals and panel
identities, one common evaluator/policy identity, the selected scenario in every episode, and
`closed_trades` equal to the selected-scenario sum. It also binds the canonical discovery-plan digest;
each episode ID, ordinal, date range, and panel digest must resolve exactly to that plan before scoring.

- [ ] **Step 3: Define role artifacts**

```python
@dataclass(frozen=True, slots=True)
class HypothesisV5:
    hypothesis_id: str
    rank: int
    primary_mechanism: Literal[
        "entry", "risk_sizing", "position_management", "exit", "cross_policy"
    ]
    causal_claim: str
    predicted_changes: tuple[MetricPredictionV5, ...]
    evidence_ids: tuple[str, ...]
    author_instructions: str

@dataclass(frozen=True, slots=True)
class InvestigatorArtifactV5:
    hypotheses: tuple[HypothesisV5, ...]

@dataclass(frozen=True, slots=True)
class CriticReviewV5:
    experiment_id: str
    prediction_vs_observation: str
    causal_explanation: str
    evidence_ids: tuple[str, ...]
    disposition: Literal["promote", "refine", "abandon"]
    next_direction: str

@dataclass(frozen=True, slots=True)
class CriticArtifactV5:
    reviews: tuple[CriticReviewV5, ...]
    comparative_assessment: str
    next_campaign_direction: str
    evidence_ids: tuple[str, ...]
```

`InvestigatorArtifactV5` must contain exactly `manifest.search.hypotheses_per_investigator` unique,
contiguously ranked hypotheses; the strict provider schema uses the same value for `minItems` and
`maxItems`. `CriticArtifactV5` contains exactly one review for every testable experiment in the
batch. Evidence IDs must resolve against issued role evidence.

- [ ] **Step 4: Run focused contract checks**

```powershell
python -B -m pytest tests/test_pit_optimizer_v5_domain.py -k "contract or target or role" -q
python -B -m ruff check core/pit_optimizer_v5/contracts.py tests/test_pit_optimizer_v5_domain.py
```

- [ ] **Step 5: Commit V5 contracts**

```powershell
git add core/pit_optimizer_v5/__init__.py core/pit_optimizer_v5/contracts.py tests/test_pit_optimizer_v5_domain.py
git commit -m "feat: define optimizer v5 contracts"
```

### Task 2: Separate Stable Policy Identity from Experiment Identity

**Files:**

- Create: `core/pit_optimizer_v5/candidate_ir.py`
- Modify: `tests/test_pit_optimizer_v5_domain.py`

**Interfaces:**

- Consumes: parent source identity, interface version, immutable constraints, hypothesis, template, assignment, round, and episode-plan identity.
- Produces: `PolicyRevisionIdentityV5` stable across panels and `ExperimentIdentityV5` bound to one evaluation.

- [ ] **Step 1: Add identity separation assertions**

Assert that evaluating identical source on a different panel retains the policy revision identity but
changes the experiment identity. Assert that any source-byte change changes the policy revision.

- [ ] **Step 2: Define identities and template IR**

```python
@dataclass(frozen=True, slots=True)
class PolicyRevisionIdentityV5:
    policy_interface_version: Literal[3]
    trusted_policy_runtime_sha256: str
    immutable_constraints_sha256: str
    editable_source_sha256: tuple[tuple[str, str], ...]

@dataclass(frozen=True, slots=True)
class LiteralAxisV5:
    name: str
    default: bool | int | float | str
    values: tuple[bool | int | float | str, ...]

@dataclass(frozen=True, slots=True)
class SourceFileV5:
    path: str
    source: str

@dataclass(frozen=True, slots=True)
class SourceBundleV5:
    files: tuple[SourceFileV5, ...]

@dataclass(frozen=True, slots=True)
class SourceOperationV5:
    path: str
    symbol: str
    kind: Literal["replace_function", "replace_constant"]
    replacement_source: str

@dataclass(frozen=True, slots=True)
class VariantAssignmentV5:
    values: tuple[tuple[str, bool | int | float | str], ...]

@dataclass(frozen=True, slots=True)
class StructuralTemplateV5:
    hypothesis_id: str
    parent_revision_sha256: str
    changed_symbols: tuple[str, ...]
    source_operations: tuple[SourceOperationV5, ...]
    axes: tuple[LiteralAxisV5, ...]
    full_source_escape: tuple[SourceFileV5, ...] | None

@dataclass(frozen=True, slots=True)
class RenderedVariantV5:
    assignment: VariantAssignmentV5
    source_bundle: SourceBundleV5
    policy_revision: PolicyRevisionIdentityV5

@dataclass(frozen=True, slots=True)
class ExperimentIdentityV5:
    policy_revision_sha256: str | None
    parent_revision_sha256: str
    hypothesis_sha256: str
    template_sha256: str
    assignment_sha256: str
    round_index: int
    discovery_plan_sha256: str
```

`SourceOperationV5` replaces one authorized module constant or complete function body. It does not
carry arbitrary patch hunks. Exactly one of `source_operations` or `full_source_escape` is populated.
`policy_revision_sha256=None` is valid only for a rendered variant that fails before a valid policy
revision can be derived. Every post-validation status requires the exact non-null revision digest.

- [ ] **Step 3: Reuse V4 validation without its panel-bound identity**

Extract a small panel-independent AST/source validation service from
`core/pit_optimizer_candidate.py`. Keep `CandidateIdentityV4` byte-compatible and wrap the extracted
service only from V5.

- [ ] **Step 4: Run identity checks**

```powershell
python -B -m pytest tests/test_pit_optimizer_v5_domain.py -k "identity or template" -q
python -B -m ruff check core/pit_optimizer_v5/candidate_ir.py
```

- [ ] **Step 5: Commit identity separation**

```powershell
git add core/pit_optimizer_v5/candidate_ir.py core/pit_optimizer_candidate.py tests/test_pit_optimizer_v5_domain.py
git commit -m "refactor: separate v5 policy and experiment identity"
```

### Task 3: Render Deterministic Local Variants

**Files:**

- Create: `core/pit_optimizer_v5/rendering.py`
- Modify: `tests/test_pit_optimizer_v5_domain.py`

**Interfaces:**

- Consumes: parent source bundle and `StructuralTemplateV5`.
- Produces: default plus deterministic local `RenderedVariantV5` values, capped by the manifest.

- [ ] **Step 1: Add rendering and cap assertions**

Exercise one through four axes, duplicate placeholders, unauthorized symbols, unsupported literal
types, and a Cartesian product larger than twelve. Repeated rendering must be byte-identical.

- [ ] **Step 2: Implement constrained placeholder rendering**

Axes are referenced by the syntactically valid marker call `PIT_AXIS("<name>")`, which must appear
exactly once inside an authorized replacement function or module constant. The renderer replaces the
marker's source span with the canonical Python literal for the assignment before compilation and
rejects any rendered bundle that still contains `PIT_AXIS`. Do not use `ast.unparse`, which would
churn unrelated formatting.

- [ ] **Step 3: Implement deterministic assignment selection**

Generate the declared default assignment, then one-axis-at-a-time values, then remaining Cartesian assignments
ordered by canonical assignment identity. Stop at `max_variants_per_template`. Run the complete
source validator after every render.

```python
def render_variants(
    *, parent: SourceBundleV5, template: StructuralTemplateV5,
    maximum: int,
) -> tuple[RenderedVariantV5, ...]: ...
```

- [ ] **Step 4: Run focused rendering checks**

```powershell
python -B -m pytest tests/test_pit_optimizer_v5_domain.py -k "render or variant or placeholder" -q
python -B -m ruff check core/pit_optimizer_v5/rendering.py
```

- [ ] **Step 5: Commit local rendering**

```powershell
git add core/pit_optimizer_v5/rendering.py tests/test_pit_optimizer_v5_domain.py
git commit -m "feat: render optimizer v5 local variants"
```

### Task 4: Detect Behavioral Equivalence Before Portfolio Evaluation

**Files:**

- Create: `core/pit_optimizer_v5/probes.py`
- Modify: `tests/test_pit_optimizer_v5_domain.py`

**Interfaces:**

- Consumes: parent and candidate V3 policy clients plus a versioned fixed snapshot suite.
- Produces: `SemanticFingerprintV5`, decision differences, or typed nondeterminism failure.

- [ ] **Step 1: Add complete-method probe assertions**

Require suite V1 to exercise entry, capacity, allocation, eviction, add-on, and exit across bull,
correction, broad, narrow, boundary, winner, and loser snapshots. Formatting-only source changes must
match the parent fingerprint; one decision change must differ.

- [ ] **Step 2: Implement the probe suite**

```python
@dataclass(frozen=True, slots=True)
class ProbeObservationV5:
    probe_id: str
    method: str
    input_sha256: str
    decision_json: bytes

@dataclass(frozen=True, slots=True)
class SemanticFingerprintV5:
    suite_id: Literal["pit-policy-v3-probes-v1"]
    observations: tuple[ProbeObservationV5, ...]
    fingerprint_sha256: str
```

Call every method twice, interleaving unrelated calls, and reject state-dependent or nondeterministic
output. Matching fingerprints mean equivalent on suite V1 and are recorded as untestable, not as a
claim of global equivalence.

- [ ] **Step 3: Run focused probe checks**

```powershell
python -B -m pytest tests/test_pit_optimizer_v5_domain.py -k "probe or equivalent or nondetermin" -q
python -B -m ruff check core/pit_optimizer_v5/probes.py
```

- [ ] **Step 4: Commit semantic probes**

```powershell
git add core/pit_optimizer_v5/probes.py tests/test_pit_optimizer_v5_domain.py
git commit -m "feat: reject behaviorally inert v5 candidates"
```

### Task 5: Persist Complete Experiment Memory

**Files:**

- Create: `core/pit_optimizer_v5/artifacts.py`
- Create: `core/pit_optimizer_v5/memory.py`
- Modify: `core/pit_optimizer_v5/contracts.py`
- Modify: `tests/test_pit_optimizer_v5_domain.py`

**Interfaces:**

- Consumes: every intended variant, validation/probe/evaluation outcome, and complete critic review.
- Produces: immutable `ExperimentRecordV5`, rebuildable archive journal, atomic checkpoint, and bounded investigator context.

- [ ] **Step 1: Add crash, tamper, and round-trip assertions**

Assert that every critic field survives write/load/restart, a changed record fails authentication, a
crash before checkpoint cannot promote an incomplete experiment, and rebuilding from the journal
matches the checkpoint archive.

- [ ] **Step 2: Define the experiment record**

```python
@dataclass(frozen=True, slots=True)
class ExperimentRecordV5:
    experiment_id: str
    experiment_identity: ExperimentIdentityV5
    round_index: int
    parent_revision_sha256: str
    parent_semantic_fingerprint_sha256: str
    hypothesis: HypothesisV5
    template: StructuralTemplateV5
    template_sha256: str
    variant_assignment: VariantAssignmentV5
    policy_revision: PolicyRevisionIdentityV5 | None
    semantic_fingerprint: SemanticFingerprintV5 | None
    status: Literal[
        "invalid", "exact_duplicate", "behavioral_equivalent", "zero_trade",
        "quick_rejected", "timed_out", "cancelled", "evaluation_failed", "evaluated"
    ]
    validation: ValidationResultV5
    quick_evidence: PanelEvaluationV5 | None
    discovery_episodes: tuple[EpisodeEvaluationV5, ...]
    campaign_evidence: CampaignEvidenceV5 | None
    target_gap_pct: Decimal | None
    critic_review: CriticReviewV5 | None
    artifact_refs: tuple[ArtifactRefV5, ...]

@dataclass(frozen=True, slots=True)
class RoundEventV5:
    campaign_id: str
    round_index: int
    sequence: int
    prior_event_sha256: str | None
    event_kind: Literal[
        "round_intent", "rendered_variant", "quick_evidence", "episode_evidence",
        "resource_lease", "cleanup_result",
    ]
    experiment_id: str | None
    payload_ref: ArtifactRefV5
```

Require `experiment_id` to equal the canonical digest of `experiment_identity`, and require the
identity's discovery-plan digest, parent, hypothesis, template, assignment, optional policy revision,
and round to match the record fields. A null revision is allowed only for `status="invalid"` with
failed validation; every other status requires the identity and record to carry the same revision.
Persist the full batch critic artifact once; every experiment
carries or directly references its complete bound review.
Each `RoundEventV5.event_kind` selects one closed payload schema and rejects any mismatched payload;
the event ID is the canonical digest of the complete record.

- [ ] **Step 3: Implement durability ordering**

Experiment inputs and evidence are create-only. Write the final experiment record and full critic
feedback before atomically replacing `archive.json` and `checkpoint.json`. Make the archive fully
rebuildable from the immutable experiment journal. Define a chained `RoundEventV5` whose typed
payload union covers round intent, rendered variant, quick evidence, one episode evidence item,
resource lease, and cleanup result. Append each event create-only as work completes; its identity
binds campaign, round, sequence, prior-event digest, event kind, and canonical payload. Recovery folds
the authenticated event stream and schedules only the missing local steps.

- [ ] **Step 4: Implement deterministic context projection**

For an investigator, include complete feedback for the selected parent lineage and relevant
mechanism first. Include measured digest-backed summaries for other experiments within the role
budget. Never truncate or overwrite the durable record itself.

- [ ] **Step 5: Run focused memory checks**

```powershell
python -B -m pytest tests/test_pit_optimizer_v5_domain.py -k "memory or artifact or checkpoint or tamper" -q
python -B -m ruff check core/pit_optimizer_v5/artifacts.py core/pit_optimizer_v5/memory.py
```

- [ ] **Step 6: Commit durable memory**

```powershell
git add core/pit_optimizer_v5/artifacts.py core/pit_optimizer_v5/memory.py core/pit_optimizer_v5/contracts.py tests/test_pit_optimizer_v5_domain.py
git commit -m "feat: persist complete optimizer v5 learning"
```

### Task 6: Implement Transparent Campaign CAGR and the Top-K Archive

**Files:**

- Create: `core/pit_optimizer_v5/search.py`
- Create: `core/pit_optimizer_v5/selection.py`
- Modify: `tests/test_pit_optimizer_v5_domain.py`

**Interfaces:**

- Consumes: complete four-episode candidate evidence and prior archive.
- Produces: transparent campaign CAGR, deterministic archive transition, next parent, and novel hypothesis selection.

- [ ] **Step 1: Add scoring and archive assertions**

Require correct compounding, identical results under rotated execution order, rejection of incomplete
or zero-trade candidates, capacity eight, preservation of overall/family leaders, and deterministic
ties.

- [ ] **Step 2: Implement compounded campaign CAGR**

```python
def campaign_cagr_pct(
    *, episodes: tuple[EpisodeEvaluationV5, ...],
    discovery_plan: CampaignPanelPlanV5,
) -> Decimal:
    ordered = tuple(sorted(episodes, key=lambda item: item.episode_ordinal))
    if tuple(item.episode_ordinal for item in ordered) != (1, 2, 3, 4):
        raise IncompleteCampaignEvidence("four canonical episodes are required")
    committed = tuple(
        sorted(discovery_plan.discovery, key=lambda item: item.episode_ordinal)
    )
    for evidence, plan in zip(ordered, committed, strict=True):
        if (
            evidence.episode_id != plan.episode_id
            or evidence.episode_ordinal != plan.episode_ordinal
            or evidence.start_date != plan.start_date
            or evidence.end_date != plan.end_date
            or evidence.evaluation.panel_sha256 != plan.panel_ref.sha256
        ):
            raise PanelCommitmentMismatch(evidence.episode_id)
    selected = tuple(selected_scenario(item.evaluation) for item in ordered)
    growth = math.prod(
        (item.ending_equity / item.starting_equity for item in selected),
        start=Decimal("1"),
    )
    days = sum(item.evaluation.elapsed_calendar_days for item in ordered)
    return annualized_return_pct(
        starting_equity=Decimal("1"), ending_equity=growth, days=days,
    )
```

Use the selected base-cost scenario from the evaluator contract. Other scenarios and episode metrics
remain critic evidence. Archive admission recomputes this function from the authenticated
`CampaignPanelPlanV5`; it never accepts a caller-supplied CAGR or four merely disjoint panels.

- [ ] **Step 3: Implement the archive**

Capacity defaults to eight. Protect the global CAGR leader and the best candidate in each present
primary mechanism family. Deduplicate protected candidates, then fill remaining slots by descending
campaign CAGR. Stable tie-break is earlier admission followed by canonical policy identity. The
baseline remains outside the eight slots and is the reported champion whenever every candidate
regresses.

Before the four discovery episodes, select at most
`max_discovery_survivors_per_template` quick-screen candidates. Preserve the author's declared
default assignment whenever it is valid and behaviorally distinct, even if its quick panel has zero
trades; fill the remaining slots by descending quick-panel CAGR with stable canonical-identity ties.
Quick screening runs only the evaluator contract's selected base-cost scenario; discovery survivors
run the complete gross/base/stress grid. This is a transparent compute budget, not a composite
objective, and the cap is manifest data. Only zero activity across all four discovery episodes makes
a candidate ineligible for archive admission.

Use these pure state records:

```python
@dataclass(frozen=True, slots=True)
class ArchiveEntryV5:
    policy_revision: PolicyRevisionIdentityV5
    primary_mechanism: str
    admitted_round: int
    campaign: CampaignEvidenceV5
    source_bundle_ref: ArtifactRefV5
    experiment_record_ref: ArtifactRefV5

@dataclass(frozen=True, slots=True)
class CandidateArchiveV5:
    capacity: int
    entries: tuple[ArchiveEntryV5, ...]

@dataclass(frozen=True, slots=True)
class SearchStateV5:
    next_round_index: int
    archive: CandidateArchiveV5
    attempted_novelty_keys: tuple[str, ...]
```

- [ ] **Step 4: Implement deterministic scheduling**

Rotate parents across global champion, family champions, and remaining archive entries. Select the
first of the investigator's manifest-declared ranked hypotheses whose controller-derived novelty key has not
been attempted for that parent. Rotate discovery episode execution order by `(round_index - 1) % 4`
without changing canonical evidence order.

If all manifest-declared hypotheses are non-novel for the selected parent, persist a typed
`no_novel_hypothesis` round outcome and make no author or critic call. Advance to the next archived
parent. Terminate with `novelty_exhausted` only after every reconstructible archive parent yields no
novel hypothesis; never spin or silently reuse an old hypothesis.

- [ ] **Step 5: Run focused search checks**

```powershell
python -B -m pytest tests/test_pit_optimizer_v5_domain.py -k "campaign or archive or selection" -q
python -B -m ruff check core/pit_optimizer_v5/search.py core/pit_optimizer_v5/selection.py
```

- [ ] **Step 6: Commit pure search**

```powershell
git add core/pit_optimizer_v5/search.py core/pit_optimizer_v5/selection.py tests/test_pit_optimizer_v5_domain.py
git commit -m "feat: add optimizer v5 multi-candidate search"
```

### Task 7: Introduce a Provider-Neutral Authorized Role Boundary

**Files:**

- Create: `core/pit_optimizer_v5/provider.py`
- Create: `tests/test_pit_optimizer_v5_runtime.py`
- Modify: `core/pit_optimizer_authorization.py:2358-2420`

**Interfaces:**

- Consumes: closed role input with issued-evidence and expected-binding records, opaque authorization
  capability, and `CompletionProvider`.
- Produces: one neutral completion attempt with typed transport/schema/binding/accounting facts.

- [ ] **Step 1: Add fake-provider lifecycle assertions**

With defaults, transport or schema failure must consume exactly one attempted call and schedule no
retry. A nonzero retry or repair path must fail unless distinct manifest slots exist. Fake and
OpenRouter adapters must yield the same neutral fact shape and reject the same unknown evidence ID,
wrong parent/hypothesis, or incomplete critic experiment binding.

- [ ] **Step 2: Define neutral interfaces and typed failures**

```python
class CompletionProvider(Protocol):
    def complete_once(
        self, request: ProviderCompletionRequestV5,
    ) -> CompletionResultV5: ...

@dataclass(frozen=True, slots=True)
class IssuedEvidenceV5:
    evidence_id: str
    payload_sha256: str

@dataclass(frozen=True, slots=True)
class RoleBindingV5:
    parent_revision_sha256: str
    hypothesis_id: str | None
    experiment_ids: tuple[str, ...]
    discovery_plan_sha256: str

@dataclass(frozen=True, slots=True)
class RoleRequestV5:
    role: Literal["investigator", "author", "critic"]
    messages: tuple[Mapping[str, object], ...]
    response_schema_sha256: str
    issued_evidence: tuple[IssuedEvidenceV5, ...]
    expected_binding: RoleBindingV5
    max_output_tokens: int

@dataclass(frozen=True, slots=True)
class ProviderCompletionRequestV5:
    role_request: RoleRequestV5
    model: str

@dataclass(frozen=True, slots=True)
class CompletionResultV5:
    response_text: str
    accepted: bool
    input_tokens: int
    output_tokens: int
    provider_request_id: str | None

class RoleFailureCode(StrEnum):
    TRANSPORT = "transport"
    RESPONSE_SCHEMA = "response_schema"
    EVIDENCE_BINDING = "evidence_binding"
    AUTHORIZATION = "authorization"
    ACCOUNTING = "accounting"

type ParsedRoleArtifactV5 = (
    InvestigatorArtifactV5 | StructuralTemplateV5 | CriticArtifactV5
)

def parse_and_bind_role_artifact(
    *, request: RoleRequestV5, response_text: str,
) -> ParsedRoleArtifactV5: ...
```

`AuthorizedRoleRunnerV5` owns `ProviderCapabilitiesV5`, reserves one declared slot, supplies its
manifest-bound model when constructing `ProviderCompletionRequestV5`, invokes `complete_once`, then
calls the shared strict `parse_and_bind_role_artifact`. That function validates evidence IDs only
against `issued_evidence` and validates the complete expected parent/hypothesis/experiment/panel
binding; it never scrapes prompt text or relies on a parser closure. The runner records neutral usage
facts and settles that exact slot without inferring failure types from exception-message text.

- [ ] **Step 3: Remove V5 dependence on the concrete gateway class**

Adapt authorization to validate an opaque controller-issued lifecycle capability or Protocol rather
than importing and type-checking `agent_loop.OpenRouterGateway`. Keep the V4 wrapper behavior
unchanged.

- [ ] **Step 4: Implement adapters**

Wrap the existing OpenRouter gateway behind `CompletionProvider`; explicitly disable hidden
transport retries. `AuthorizedRoleRunnerV5` is the only live-provider composition. Add a
`FixtureRoleRunnerV5` that implements the same `RoleRunnerV5` boundary, calls deterministic local
fixture responses, invokes the same `parse_and_bind_role_artifact`, and records zero external
calls/tokens/cost. It needs no model because provider-free manifests have `provider=None`. Every
optional repair or retry is a separate authorized, audited, and costed call.

Role inputs are explicit: the investigator receives bounded aggregate evaluator evidence, archive
family summaries, and complete relevant critic directions; the author receives the selected
hypothesis, V3 contracts, and only the exact editable parent policy files needed for the declared
scope (all four only for manifest-enabled full-source escape); the critic receives predictions,
semantic differences, typed failures, and quick/per-episode scenario metrics for the complete batch.
No role receives raw market rows, held-out composition/results, credentials, or filesystem paths.

- [ ] **Step 5: Run focused provider checks**

```powershell
python -B -m pytest tests/test_pit_optimizer_v5_runtime.py -k "provider or authorization or retry or repair" -q
python -B -m ruff check core/pit_optimizer_v5/provider.py core/pit_optimizer_authorization.py
```

- [ ] **Step 6: Commit provider neutrality**

```powershell
git add core/pit_optimizer_v5/provider.py core/pit_optimizer_authorization.py tests/test_pit_optimizer_v5_runtime.py
git commit -m "refactor: add provider-neutral v5 role lifecycle"
```

### Task 8: Orchestrate One Complete V5 Feedback Round

**Files:**

- Create: `core/pit_optimizer_v5/runtime.py`
- Create: `core/pit_optimizer_v5/sandbox.py`
- Create: `core/pit_optimizer_v5/workspace.py`
- Modify: `tests/test_pit_optimizer_v5_runtime.py`

**Interfaces:**

- Consumes: provider, evaluator, materializer, probe runner, artifact repository, search policy, and cancellation callback.
- Produces: finalized experiment records, batch critic feedback, archive transition, checkpoint, and cleanup evidence.

- [ ] **Step 1: Add one fake-provider end-to-end assertion**

The fake investigator returns three hypotheses; the scheduler selects one novel hypothesis; the
author returns one template with three values; all variants validate and probe; quick survivors run
all four episodes; one critic reviews the complete batch; the archive changes only after feedback is
durable.

- [ ] **Step 2: Implement injected runtime services**

```python
@dataclass(frozen=True, slots=True)
class RuntimeServicesV5:
    roles: RoleRunnerV5
    evaluator: PanelEvaluatorV5
    materializer: CandidateMaterializerV5
    validator: CandidateValidatorV5
    probes: PolicyProbeRunnerV5
    artifacts: ArtifactRepositoryV5
    scheduler: SearchSchedulerV5
    cancelled: Callable[[], bool]

class RoleRunnerV5(Protocol):
    def invoke_once(self, request: RoleRequestV5) -> ParsedRoleArtifactV5: ...

class PanelEvaluatorV5(Protocol):
    def evaluate_candidate(
        self, *, candidate_root: Path, panel: EvaluationPanelSpec,
        policy_identity_sha256: str, scenario_ids: tuple[str, ...],
        timeout_seconds: int,
    ) -> PanelEvaluationV5: ...

class CandidateMaterializerV5(Protocol):
    def materialize(self, variant: RenderedVariantV5) -> ContextManager[Path]: ...

class CandidateValidatorV5(Protocol):
    def validate(self, candidate_root: Path) -> ValidationResultV5: ...

class PolicyProbeRunnerV5(Protocol):
    def fingerprint(self, candidate_root: Path) -> SemanticFingerprintV5: ...

class ArtifactRepositoryV5(Protocol):
    def append_round_event(self, event: RoundEventV5) -> ArtifactRefV5: ...
    def load_round_events(
        self, *, campaign_id: str, round_index: int,
    ) -> tuple[RoundEventV5, ...]: ...
    def append_experiment(self, record: ExperimentRecordV5) -> ArtifactRefV5: ...
    def append_critic(self, artifact: CriticArtifactV5) -> ArtifactRefV5: ...
    def load_state(self) -> SearchStateV5: ...
    def replace_state(self, state: SearchStateV5) -> None: ...

class SearchSchedulerV5(Protocol):
    def select_parent(self, state: SearchStateV5) -> ArchiveEntryV5 | None: ...
    def select_novel_hypothesis(
        self, state: SearchStateV5, artifact: InvestigatorArtifactV5,
    ) -> HypothesisV5 | None: ...
    def select_discovery_survivors(
        self, records: tuple[ExperimentRecordV5, ...], maximum: int,
    ) -> tuple[str, ...]: ...

def run_feedback_round(
    *, manifest: CampaignManifestV5, state: SearchStateV5,
    services: RuntimeServicesV5,
) -> SearchStateV5: ...
```

Define `RoleRunnerV5`, `PanelEvaluatorV5`, `CandidateMaterializerV5`, `CandidateValidatorV5`,
`PolicyProbeRunnerV5`, `ArtifactRepositoryV5`, and `SearchSchedulerV5` as Protocols in `runtime.py`.
Their methods respectively invoke one typed role, evaluate a declared stage/scenario scope,
materialize/dispose one rendered source bundle, validate source, produce a semantic fingerprint,
append/load the typed immutable round event stream and atomically replace search state, and select
parents/hypotheses/survivors.
The runtime depends only on these Protocols, not concrete provider, Git, worker, Docker, or filesystem
implementations.

- [ ] **Step 3: Implement the concrete bounded local adapters**

`DockerPanelEvaluatorV5` accepts only the authenticated `SandboxProfileV5` and launches a
digest-pinned image with networking absent, a read-only root filesystem, read-only source and data
mounts, no extra capabilities, the manifest CPU/memory limits, and a bounded writable output mount.
It validates the output byte count before parsing and binds the sandbox-profile digest into every
`PanelEvaluationV5`. `GitCandidateMaterializerV5` creates a campaign-owned disposable workspace,
verifies the parent/source identities before writing candidate files, and returns an ownership lease
used for exact cleanup. CLI composition wires these concrete adapters; production execution cannot
fall back to an in-process evaluator or an unbounded generic subprocess adapter.

- [ ] **Step 4: Implement exact round order**

Load restored memory; select parent; call investigator; select first novel hypothesis; call author;
render variants; validate and probe; quick-screen all distinct variants in the base scenario; select
at most the manifest's discovery-survivor cap; evaluate all four episodes and all declared scenarios
for every survivor; call critic once with bounded batch evidence; finalize every experiment;
transition archive; write checkpoint; clean workers. Persist each completed local evidence item as it
arrives. Enforce manifest concurrency and per-stage deadlines directly; do not inflate them to the
legacy two-hour minimum.

- [ ] **Step 5: Add interruption recovery**

Restart must resume local evaluation from durable evidence without duplicating provider calls or
promoting an incomplete experiment. A missing critic may use a separately authorized remaining
critic slot; otherwise finalize experiments without critic review and leave archive unchanged.

Persist a campaign/round ownership lease for every disposable workspace, policy worker, evaluator
process, and container. Startup may reclaim only resources whose authenticated owner is this
campaign and whose controller lease is no longer live; never kill by broad process name. Final
cleanup records owned resource counts and `cleanup_complete` without exposing process/container IDs
in summaries.

- [ ] **Step 6: Run focused runtime checks**

```powershell
python -B -m pytest tests/test_pit_optimizer_v5_runtime.py -k "round or restart or cleanup" -q
python -B -m ruff check core/pit_optimizer_v5/runtime.py core/pit_optimizer_v5/sandbox.py core/pit_optimizer_v5/workspace.py
```

- [ ] **Step 7: Commit runtime orchestration**

```powershell
git add core/pit_optimizer_v5/runtime.py core/pit_optimizer_v5/sandbox.py core/pit_optimizer_v5/workspace.py tests/test_pit_optimizer_v5_runtime.py
git commit -m "feat: orchestrate optimizer v5 feedback rounds"
```

### Task 9: Add V5 CLI and Thin Legacy Dispatch

**Files:**

- Create: `core/pit_optimizer_v5/cli.py`
- Create: `core/pit_optimizer_v5/summary.py`
- Modify: `agent_loop.py:19559-21957`
- Modify: `tests/test_pit_optimizer_v5_runtime.py`

**Interfaces:**

- Consumes: V5 manifest and dependency factories.
- Produces: run, resume, verify-run, summarize, and optional V4-import commands. Campaign-specific
  manifest builders are added by the campaign plan.

- [ ] **Step 1: Add command-dispatch assertions**

Assert that parsing and readiness perform no provider call, V5 execution delegates out of
`agent_loop.py`, and V4 commands retain their existing entry points.

- [ ] **Step 2: Implement CLI commands**

Provide `run`, `resume`, `verify-run`, and `summarize` for an already parsed and authenticated V5
manifest, plus fixture-only construction used by focused checks. `agent_loop.py` should contain only
backwards-compatible argument dispatch and adapter construction for V5. The campaign plan later adds
`build-manifest`, `verify-manifest`, and `render-command` after the panel and baseline authorities
exist.

- [ ] **Step 3: Add explicit V4 import**

`import-v4-candidate` authenticates old source, mints a V5 policy revision, runs V3 probes, evaluates
the complete V5 discovery campaign, and writes a migration experiment. Never copy V4 numeric
evidence or mutate V4 checkpoints.

- [ ] **Step 4: Verify module boundaries**

```powershell
rg -n "agent_loop|OpenRouter|docker|subprocess|git" core/pit_optimizer_v5/contracts.py core/pit_optimizer_v5/candidate_ir.py core/pit_optimizer_v5/probes.py core/pit_optimizer_v5/memory.py core/pit_optimizer_v5/search.py core/pit_optimizer_v5/selection.py
python -B -m pytest tests/test_pit_optimizer_v5_domain.py tests/test_pit_optimizer_v5_runtime.py -q
python -B -m compileall -q core/pit_optimizer_v5 agent_loop.py
python -B -m ruff check core/pit_optimizer_v5 agent_loop.py
```

Expected: no infrastructure imports in the pure modules; focused V5 checks pass; V4 dispatch still
imports.

- [ ] **Step 5: Commit V5 CLI composition**

```powershell
git add core/pit_optimizer_v5/cli.py core/pit_optimizer_v5/summary.py agent_loop.py tests/test_pit_optimizer_v5_domain.py tests/test_pit_optimizer_v5_runtime.py
git commit -m "feat: expose optimizer v5 commands"
```

## Completion Criteria

- Targets are fully configurable and V5 never reads the hard-coded V4 production target.
- Policy revision identity remains stable across evaluation panels.
- Full critic feedback survives checkpoint and campaign restart.
- Every investigator returns three hypotheses and only a novel one is authored per round.
- One template produces up to twelve deterministic, behaviorally probed local variants.
- Every quick survivor completes four chronological discovery episodes before ranking.
- Archive capacity eight preserves the global leader and distinct mechanism leaders.
- Selection uses transparent compounded campaign CAGR and no weighted composite.
- Provider and authorization code depend on neutral interfaces, with retries/repair explicit and default zero.
- `agent_loop.py` delegates rather than hosting new V5 control flow.
- The focused fake-provider run completes with source unchanged and no external call.
