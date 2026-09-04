# PIT Optimizer V5 Campaign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove V5 provider-free on a small subset, then run a bounded model-guided discovery campaign and detached qualification before any full replay.

**Architecture:** Build deterministic chronological episode plans from the authenticated three-universe bundle. Validate mechanics and local variants without a provider, then let each authorized investigator-author-critic round evaluate several local candidates. Freeze the best discovery candidate, run one provider-free confirmation, and expose the untouched qualification only through the existing one-use retirement discipline.

**Tech Stack:** Python 3.13, V5 CLI and contracts, SQLite PIT bundle, local ignored artifacts, Git disposable workspaces, network-disabled Docker evaluator, OpenRouter adapter behind an explicit run manifest, and content-free summaries.

**Spec:** `docs/superpowers/specs/2026-09-04-pit-optimizer-v5-hybrid-architecture-design.md`

## Global Constraints

- This plan does not itself authorize any provider call, qualification opening, or full replay.
- Discovery remains `apply=false`; no model-authored policy changes the tracked checkout.
- Every provider attempt must be declared in an operator-approved manifest and fully accounted.
- Provider retries and schema repair default to zero.
- Keep raw market data, provider content, local paths, qualification composition, and candidate source out of public summaries.
- Keep CAGR as the primary objective. Do not introduce a weighted robustness score.
- Use support metrics to explain results and to reject only invalid, behaviorally inert, non-causal,
  accounting-broken, or zero-activity complete-campaign evaluations. Zero trades on the quick screen
  alone is not a terminal gate.
- Require provider-free mechanics evidence before requesting discovery authorization.
- Require a separate operator decision before one-use qualification and another before full replay.
- Preserve every retired qualification outcome and clean up disposable workers after every run.
- Run portfolio evaluation with provider networking absent, read-only source/data mounts, bounded
  output, one CPU and 1 GiB memory per worker by default; resource values remain manifest-configurable
  for local capacity, while network and read-only boundaries do not.

## File Structure and Responsibility Map

- `core/pit_optimizer_v5/panels.py`: deterministic chronological episode and security-cohort plans.
- `core/pit_optimizer_v5/manifest.py`: campaign, provider, resource, target, and stage contracts.
- `core/pit_optimizer_v5/runtime.py`: run composition and lifecycle.
- `core/pit_optimizer_v5/qualification.py`: detached one-use qualification adapter.
- `core/pit_optimizer_v5/summary.py`: content-free operator summaries.
- `core/pit_optimizer_v5/cli.py`: build, verify, render, run, qualify, and summarize commands.
- `agent_loop.py`: thin backwards-compatible command delegation only.
- `.artifacts/pit-optimizer-v5/`: ignored local plans, data, candidates, evidence, and audit records.

---

### Task 1: Build Deterministic V5 Episode Plans

**Files:**

- Create: `core/pit_optimizer_v5/panels.py`
- Modify: `core/pit_optimizer_v5/contracts.py`
- Modify: `core/pit_optimizer_v5/cli.py`
- Modify: `tests/test_pit_optimizer_subset_reference.py`
- Create locally only: `.artifacts/pit-optimizer-v5/panels/discovery-plan.json`
- Create locally only: `.artifacts/pit-optimizer-v5/panels/confirmation-plan.json`
- Create locally only: `.artifacts/pit-optimizer-v5/panels/qualification-plan.json`
- Create locally only: `.artifacts/pit-optimizer-v5/panels/specs/`
- Create locally only: `.artifacts/pit-optimizer-v5/panels/confirmation-retirement.json`
- Create locally only: `.artifacts/pit-optimizer-v5/panels/qualification-retirement.json`

**Interfaces:**

- Consumes: authenticated bundle identity, security lineages and affiliations, session calendar, seed label, counts, and configured target.
- Produces: controller-local `CampaignPanelPlanV5` with mechanics and four discovery episodes plus
  separate controller-only, create-only confirmation and qualification plans whose exact digests are
  committed by the discovery plan. Provider roles receive only a content-free discovery evidence
  projection; artifact paths and lineage membership never enter role input.

- [ ] **Step 1: Add deterministic partition assertions**

Extend the existing subset-plan tests to require the same seed to produce byte-identical plans,
different seeds to change allocations, lineages not to cross cohorts, and affiliation overlaps to
remain one security.

- [ ] **Step 2: Define the stage contracts**

```python
@dataclass(frozen=True, slots=True)
class ConfirmationPanelPlanV5:
    schema_version: Literal[5]
    pit_bundle_sha256: str
    partition_seed_sha256: str
    target_sha256: str
    confirmation_retirement_domain_id: str
    confirmation_ledger_snapshot_sha256: str
    episode: EpisodePlanV5

@dataclass(frozen=True, slots=True)
class QualificationPanelPlanV5:
    schema_version: Literal[5]
    pit_bundle_sha256: str
    partition_seed_sha256: str
    target: AnnualizedReturnTargetV5
    qualification_retirement_domain_id: str
    qualification_ledger_snapshot_sha256: str
    episode: EpisodePlanV5

@dataclass(frozen=True, slots=True)
class RetirementLedgerLocatorV5:
    relative_path: str
    preopen_snapshot_ref: ArtifactRefV5

@dataclass(frozen=True, slots=True)
class ConfirmationAttemptCommitmentV5:
    schema_version: Literal[5]
    attempt_id: str
    confirmation_plan_ref: ArtifactRefV5
    discovery_manifest_ref: ArtifactRefV5
    discovery_champion_policy_ref: ArtifactRefV5
    discovery_champion_experiment_ref: ArtifactRefV5
    pit_bundle_ref: ArtifactRefV5
    prices_provenance_ref: ArtifactRefV5
    execution_profile_ref: ArtifactRefV5
    evaluator_contract_ref: ArtifactRefV5
    scenario_grid_ref: ArtifactRefV5
    baseline_authority_ref: ArtifactRefV5
    sandbox_profile_ref: ArtifactRefV5
    retirement_domain_id: str
    retirement_ledger: RetirementLedgerLocatorV5

@dataclass(frozen=True, slots=True)
class QualificationAttemptCommitmentV5:
    schema_version: Literal[5]
    attempt_id: str
    qualification_plan_ref: ArtifactRefV5
    confirmation_outcome_ref: ArtifactRefV5
    confirmed_policy_ref: ArtifactRefV5
    pit_bundle_ref: ArtifactRefV5
    prices_provenance_ref: ArtifactRefV5
    execution_profile_ref: ArtifactRefV5
    evaluator_contract_ref: ArtifactRefV5
    scenario_grid_ref: ArtifactRefV5
    baseline_authority_ref: ArtifactRefV5
    sandbox_profile_ref: ArtifactRefV5
    retirement_domain_id: str
    retirement_ledger: RetirementLedgerLocatorV5

@dataclass(frozen=True, slots=True)
class ConfirmationOutcomeV5:
    schema_version: Literal[5]
    attempt_ref: ArtifactRefV5
    status: Literal["completed", "failed", "timed_out", "cancelled"]
    confirmed_policy_ref: ArtifactRefV5
    baseline_evidence_ref: ArtifactRefV5 | None
    candidate_evidence_ref: ArtifactRefV5 | None
    baseline_cagr_pct: Decimal | None
    candidate_cagr_pct: Decimal | None
    candidate_excess_cagr_pct: Decimal | None
    behaviorally_active: bool | None
    eligible_to_request_qualification: bool
    retirement_terminal_ref: ArtifactRefV5
    cleanup_evidence_ref: ArtifactRefV5
    provider_calls: Literal[0]

@dataclass(frozen=True, slots=True)
class QualificationOutcomeV5:
    schema_version: Literal[5]
    attempt_ref: ArtifactRefV5
    status: Literal["completed", "failed", "timed_out", "cancelled"]
    qualified_policy_ref: ArtifactRefV5
    baseline_evidence_ref: ArtifactRefV5 | None
    candidate_evidence_ref: ArtifactRefV5 | None
    target_pct: Decimal
    baseline_cagr_pct: Decimal | None
    candidate_cagr_pct: Decimal | None
    candidate_excess_cagr_pct: Decimal | None
    target_reached: bool
    baseline_beaten: bool
    qualified: bool
    retirement_terminal_ref: ArtifactRefV5
    cleanup_evidence_ref: ArtifactRefV5
    provider_calls: Literal[0]
```

Reuse the canonical `EpisodePlanV5` and `CampaignPanelPlanV5` contracts established in
learning/search Task 1; this task implements their builder and adds only the controller-held stage
and attempt contracts shown above.

Require exactly four non-overlapping discovery episodes. The initial five-year schedule uses two
2021 half-year episodes, 2022, and 2023 for discovery, 2024 for confirmation, and 2025 for sealed
qualification. Discovery ordinals are exactly `(1, 2, 3, 4)` regardless of execution order.
Mechanics, quick, confirmation, and qualification plans use `episode_ordinal=None` because they do
not participate in compounded discovery CAGR.

Reuse the existing create-only retirement-domain and append-only ledger machinery through a V5
adapter for both confirmation and qualification; do not reuse the schema-V4 plan/target type. The
discovery plan contains only the canonical digests of `ConfirmationPanelPlanV5` and
`QualificationPanelPlanV5`. Search/runtime composition receives no path or deserializer for either
held-out plan. After discovery closes, create a `ConfirmationAttemptCommitmentV5` before opening the
confirmation plan; it binds the frozen discovery champion and every evaluator dependency by
resolvable authenticated reference. Create `QualificationAttemptCommitmentV5` only after a
completed `ConfirmationOutcomeV5` whose qualification-eligibility gate recomputes true, plus a
separate operator decision. A failed or ineligible confirmation cannot be overridden by approval.
Both attempt commitments bind an authenticated immutable friction/scenario-grid identity alongside
the execution profile and evaluator contract. Their exact lineage compositions are opened only by
the later `confirm` or `qualify` command.

`RetirementLedgerLocatorV5.relative_path` follows the same containment and canonical-path rules as
an artifact reference but names the one mutable append-only ledger. Its create-only pre-open
snapshot remains immutable. Before opening the held-out plan, execution requires the live ledger's
canonical state to equal that snapshot, then appends opened and terminal events with a chained
prior-state digest. Post-run verification follows the chain from the immutable snapshot; it never
expects the mutated ledger bytes to retain their pre-open digest.

Both outcome contracts are closed, create-only terminal records. Their Boolean gates are recomputed
from authenticated evidence rather than trusted as caller input. A completed confirmation is
eligible only when the candidate is active and its same-panel CAGR exceeds the baseline; a completed
qualification is successful only when candidate CAGR reaches the exact target and strictly exceeds
the same-panel baseline. Non-completed outcomes carry null evaluation metrics and false gates while
still binding terminal retirement and cleanup evidence.

- [ ] **Step 3: Implement deterministic stratified lineage allocation**

Allocate 24 mechanics, 96 quick, four 150-lineage discovery cohorts, 400 confirmation lineages,
and 500 qualification lineages by canonical affiliation bitset and deterministic seed. Refuse a
bundle too small to satisfy disjoint allocations or a lineage without sufficient causal price and
feature coverage for its assigned episode. Serialize every complete `EvaluationPanelSpec` as a
create-only file under `panels/specs/`, then store its authenticated `ArtifactRefV5` in the owning
`EpisodePlanV5`; no evaluator reconstructs sessions or feature scope from defaults.

All stages may compute declared point-in-time breadth and industry leadership from the complete
active union, matching information available on that session. Provider/search evidence must never
include held-out lineage-level signals, trades, outcomes, membership, or future returns; held-out
lineages are excluded from tradable cohorts until their stage is opened.

- [ ] **Step 4: Add panel build and verification commands**

`init-stage-ledgers` creates distinct local confirmation and qualification retirement domains.
`build-panels` accepts the bundle, date range, seed, target, both retirement ledgers, and three
distinct create-only output paths.
`verify-panels` authenticates all three files, proves pairwise time/security separation and
commitment bindings, and prints only content-free counts and identities.

- [ ] **Step 5: Run focused panel checks**

```powershell
python -B -m pytest tests/test_pit_optimizer_subset_reference.py -q
python -B -m core.pit_optimizer_v5.cli init-stage-ledgers --help
python -B -m core.pit_optimizer_v5.cli build-panels --help
python -B -m core.pit_optimizer_v5.cli verify-panels --help
python -B -m ruff check core/pit_optimizer_v5/panels.py core/pit_optimizer_v5/contracts.py core/pit_optimizer_v5/cli.py
```

- [ ] **Step 6: Commit episode planning**

```powershell
git add core/pit_optimizer_v5/panels.py core/pit_optimizer_v5/contracts.py core/pit_optimizer_v5/cli.py tests/test_pit_optimizer_subset_reference.py
git commit -m "feat: add optimizer v5 episode plans"
```

### Task 2: Add V5 Campaign and Resource Manifests

**Files:**

- Create: `core/pit_optimizer_v5/manifest.py`
- Modify: `core/pit_optimizer_v5/cli.py`
- Modify: `tests/test_pit_optimizer_v5_runtime.py`

**Interfaces:**

- Consumes: panel plan, execution profile, baseline authority, editable policy scope, target, and optional provider authorization.
- Produces: canonical `CampaignManifestV5` values using the already-defined
  `SearchCapabilitiesV5`, `ProviderCapabilitiesV5`, and `ResourceCapabilitiesV5`, plus rendered
  commands.

- [ ] **Step 1: Add manifest round-trip and target-configuration assertions**

Exercise targets `10.00`, `20.00`, and `50.00`. Assert that no V5 constructor reads
`AnnualizedReturnTarget.production()`, provider-free manifests carry no provider plan, every
dependency reference resolves from the canonical artifact root, and missing/relocated/mismatched
artifacts fail before adapter construction.

- [ ] **Step 2: Serialize and validate the canonical manifest contracts**

Do not redefine search/provider/resource dataclasses in `manifest.py`. Canonically serialize the
contracts from `core/pit_optimizer_v5/contracts.py`. `CampaignManifestV5` binds the exact target,
resolvable data/evaluator/execution/baseline/panel/policy/sandbox references, source identity, `apply=False`,
and stage permissions. Qualification and full replay permissions must be false in a discovery
manifest. Walk and authenticate the complete `ArtifactRefV5` graph before parsing any referenced
artifact; no command may consult environment defaults or hard-coded local paths to fill a missing
dependency. The manifest's evaluator, execution, sandbox, panel, and baseline-policy references
must equal the corresponding references nested in `BaselineAuthorityV5`; duplicate declarations may
not drift.

`ProviderCapabilitiesV5.maximum_usd=None` explicitly means no USD ceiling; it does not remove the
authorized call or token ceilings. Resource capabilities default to two parallel evaluations and
stage deadlines of 60 seconds for mechanics, 180 seconds for quick screens, and 600 seconds per
discovery episode, with a 180-second role-call timeout, 30-minute round wall time, five-hour campaign
wall time, and 60-second cleanup bound. Evaluation defaults are one CPU, 1 GiB memory, and 64 MiB of
output. Values are manifest-configurable, positive and finite, exactly match the authenticated
`SandboxProfileV5`, and are never raised to the legacy two-hour minimum at runtime. Network absence,
read-only root/source/data mounts, and a digest-pinned image/runtime are fixed invariants.

- [ ] **Step 3: Add `build-manifest`, `verify-manifest`, and `render-command` commands**

Rendered commands must come only from authenticated manifest fields. Do not ask operators to
retype model, token, call, retry, target, or source-scope arguments. `build-manifest` writes
create-only policy-scope and baseline-policy-revision descriptor artifacts under the canonical
artifact root, then stores their `ArtifactRefV5` values. The descriptor binds the clean source
commit, exact editable paths and byte digests; later materialization reconstructs tracked baseline
bytes from that commit rather than assuming the current working tree still matches.

- [ ] **Step 4: Run focused manifest checks**

```powershell
python -B -m pytest tests/test_pit_optimizer_v5_runtime.py -q
python -B -m ruff check core/pit_optimizer_v5/manifest.py core/pit_optimizer_v5/cli.py
```

- [ ] **Step 5: Commit campaign manifests**

```powershell
git add core/pit_optimizer_v5/manifest.py core/pit_optimizer_v5/cli.py tests/test_pit_optimizer_v5_runtime.py
git commit -m "feat: add optimizer v5 campaign manifests"
```

### Task 3: Prove the Entire Loop Provider-Free

**Files:**

- Modify: `core/pit_optimizer_v5/runtime.py`
- Modify: `core/pit_optimizer_v5/summary.py`
- Modify: `core/pit_optimizer_v5/cli.py`
- Modify: `tests/test_pit_optimizer_v5_runtime.py`
- Create locally only: `.artifacts/pit-optimizer-v5/subset/`

**Interfaces:**

- Consumes: one `FixtureRoleRunnerV5`, the V3 baseline parent, declared tunable axes, mechanics,
  quick, and all four synthetic discovery episodes.
- Produces: a complete investigator-author-render-variants-evaluate-critic-checkpoint-restore cycle
  with zero external calls.

- [ ] **Step 1: Add one fixture-provider end-to-end case**

Reuse `tests/test_pit_optimizer_v5_runtime.py`. The fixture investigator returns three hypotheses; the
fixture author changes one entry constant and declares three values; the fixture critic returns a
distinct next direction. Assert three behaviorally distinct variants are evaluated and the next
investigator input contains the exact critic explanation and next direction after checkpoint
restore. All three variants run the base-cost quick screen, and every selected survivor completes all
four discovery episodes under the gross/base/stress grid.

- [ ] **Step 2: Implement provider-free composition**

Inject `FixtureRoleRunnerV5` through the runtime's `RoleRunnerV5` Protocol. It supplies deterministic
local responses, runs the same strict role parsers and evidence-ID binding as
`AuthorizedRoleRunnerV5`, but performs no provider operation and records exactly zero external calls,
tokens, and cost. The runtime never receives a raw `CompletionProvider`.
Register `run-fixture` in `cli.py`; it accepts only an authenticated provider-free manifest and
artifact root, constructs `FixtureRoleRunnerV5`, and delegates to the normal runtime.

- [ ] **Step 3: Add content-free summaries**

`summarize` reports execution profile, target, iterations, behaviorally distinct variants, archive
families, best campaign CAGR, target gap, typed failures, call/token totals, source cleanliness,
cleanup, and whether qualification or replay started. It never prints candidate source or raw role
content.

- [ ] **Step 4: Run the focused end-to-end check**

```powershell
python -B -m pytest tests/test_pit_optimizer_v5_runtime.py -q
python -B -m core.pit_optimizer_v5.cli build-manifest --mode fixture --panel-plan .artifacts/pit-optimizer-v5/panels/discovery-plan.json --baseline-authority .artifacts/pit-optimizer-v5/evaluator/baseline-authority.json --evaluator-contract .artifacts/pit-optimizer-v5/evaluator/evaluator-contract.json --execution-profile .artifacts/pit-optimizer-v5/evaluator/execution-profile.json --sandbox-profile .artifacts/pit-optimizer-v5/evaluator/sandbox-profile.json --policy-root core/strategy_policy/v3 --target-pct 10.00 --output .artifacts/pit-optimizer-v5/subset/manifest.json
python -B -m core.pit_optimizer_v5.cli run-fixture --manifest .artifacts/pit-optimizer-v5/subset/manifest.json --artifact-root .artifacts/pit-optimizer-v5/subset
python -B -m core.pit_optimizer_v5.cli summarize --artifact-root .artifacts/pit-optimizer-v5/subset
```

Expected: a restored second round cites the first critic's full direction; multiple variants are
evaluated; provider calls are zero; source is unchanged; cleanup is complete.

- [ ] **Step 5: Commit provider-free campaign composition**

```powershell
git add core/pit_optimizer_v5/runtime.py core/pit_optimizer_v5/summary.py core/pit_optimizer_v5/cli.py tests/test_pit_optimizer_v5_runtime.py
git commit -m "feat: prove optimizer v5 provider-free loop"
```

### Task 4: Prepare an Authorization-Ready Discovery Campaign

**Files:**

- Read locally only: `.artifacts/pit-optimizer-v5/panels/discovery-plan.json`
- Read locally only: `.artifacts/pit-optimizer-v5/panels/confirmation-plan.json`
- Read locally only: `.artifacts/pit-optimizer-v5/panels/qualification-plan.json`
- Create locally only: `.artifacts/pit-optimizer-v5/discovery/manifest.json`

**Interfaces:**

- Consumes: completed provider-free proof, verified three-universe bundle, V5 baseline authority, and an operator-specified provider allowance.
- Produces: an authenticated discovery command that cannot open qualification or full replay.

- [ ] **Step 1: Reauthenticate the existing panel commitments without opening held-out stages**

```powershell
python -B -m core.pit_optimizer_v5.cli verify-panels --discovery-plan .artifacts/pit-optimizer-v5/panels/discovery-plan.json --confirmation-commitment .artifacts/pit-optimizer-v5/panels/confirmation-plan.json --qualification-commitment .artifacts/pit-optimizer-v5/panels/qualification-plan.json --keep-held-out-sealed
```

Expected: discovery composition is authenticated; confirmation and qualification return only digest,
count, date-bound, and separation facts. Their lineage sets are not loaded into the search runtime or
role evidence.

- [ ] **Step 2: Build a discovery-only manifest from the operator's explicit allowance**

Use `build-manifest` with the model, total role calls, token limit, retry count, and optional USD
limit exactly as authorized in the current user instruction. Set `apply=false`,
`qualification_allowed=false`, and `full_replay_allowed=false`.

- [ ] **Step 3: Verify readiness without making a provider call**

```powershell
python -B -m core.pit_optimizer_v5.cli verify-manifest --manifest .artifacts/pit-optimizer-v5/discovery/manifest.json
python -B -m core.pit_optimizer_v5.cli render-command --manifest .artifacts/pit-optimizer-v5/discovery/manifest.json
```

Expected: authenticated baseline, execution, bundle, policy, panels, source, resource, and
authorization identities; zero calls used; qualification and replay disabled.

- [ ] **Step 4: STOP for explicit discovery authorization**

Do not run the rendered command until the operator approves that exact manifest's role-call, token,
retry, model, editable-source, and optional cost scope.

### Task 5: Run Ten Model Feedback Rounds

**Files:**

- Create locally only: `.artifacts/pit-optimizer-v5/discovery/iterations/`
- Create locally only: `.artifacts/pit-optimizer-v5/discovery/archive/`
- Create locally only: `.artifacts/pit-optimizer-v5/discovery/memory/`

**Interfaces:**

- Consumes: the explicitly authorized discovery manifest.
- Produces: ten structured feedback rounds, up to twelve base-scenario quick screens and six
  full-grid discovery survivors per authored template by default, and one frozen discovery archive.

- [ ] **Step 1: Execute the authenticated rendered command once**

The runtime must stop when the manifest call/token allowance or ten-round campaign limit is reached.
It must not silently increase either value.

With default search settings, one completed authored round schedules at most 12 quick simulator runs
plus `6 survivors x 4 episodes x 3 friction scenarios = 72` discovery simulator runs. Run at most two
evaluations concurrently, enforce the manifest's per-stage deadlines without legacy minimum
inflation, and persist a typed timeout before cleaning that worker. A timeout does not trigger a
provider retry; it remains critic evidence.

- [ ] **Step 2: Verify accounting and cleanup**

```powershell
python -B -m core.pit_optimizer_v5.cli verify-run --artifact-root .artifacts/pit-optimizer-v5/discovery
```

Expected: every attempted call or skipped slot is reconciled, all evaluated candidates have semantic
fingerprints and execution identities, source is unchanged, and disposable resources are gone.

- [ ] **Step 3: Review learning quality**

```powershell
python -B -m core.pit_optimizer_v5.cli summarize --artifact-root .artifacts/pit-optimizer-v5/discovery
```

Expected: ten completed or explicitly accounted rounds; full critic direction reaches subsequent
investigators; several behavior families survive; the summary distinguishes model templates from
provider-free variants and reports campaign CAGR without exposing code or role content.

### Task 6: Run Provider-Free Confirmation

**Files:**

- Create: `core/pit_optimizer_v5/confirmation.py`
- Modify: `core/pit_optimizer_v5/cli.py`
- Modify: `tests/test_pit_optimizer_holdout.py`
- Create locally only: `.artifacts/pit-optimizer-v5/confirmation-attempt.json`
- Create locally only: `.artifacts/pit-optimizer-v5/confirmation/`

**Interfaces:**

- Consumes: a create-only attempt commitment binding the frozen best discovery candidate, all
  evaluator dependencies, the unopened 2024 confirmation cohort, and its retirement ledger.
- Produces: a permanently retired provider-free confirmation report; no search state is updated
  from its result.

- [ ] **Step 1: Add focused frozen-candidate and retirement assertions**

Require the attempt builder to choose exactly the authenticated final discovery champion, bind every
transitive reference, and refuse an already-used or changed ledger. Require `confirm` to append an
opened record before deserializing the episode and to retire the domain on every terminal path,
including simulated controller recovery. Require baseline and candidate evidence to share panel,
evaluator, execution, sandbox, and scenario identities, and round-trip the closed
`ConfirmationOutcomeV5` gate.

- [ ] **Step 2: Implement confirmation composition and CLI ownership**

Implement `build_confirmation_attempt` and `run_confirmation` in `confirmation.py`; register
`build-confirmation-attempt` and `confirm` in `cli.py`. Both commands resolve all dependencies only
through the attempt/reference graph. Confirmation reuses the concrete sandbox evaluator and
candidate materializer, makes no provider call, and never imports or mutates search state. It runs
the unchanged baseline policy and frozen candidate on the exact same confirmation panel, evaluator,
sandbox, execution profile, and friction grid, then writes one closed `ConfirmationOutcomeV5`.

- [ ] **Step 3: Run focused confirmation checks**

```powershell
python -B -m pytest tests/test_pit_optimizer_holdout.py -k "confirmation or retirement" -q
python -B -m ruff check core/pit_optimizer_v5/confirmation.py core/pit_optimizer_v5/cli.py
```

- [ ] **Step 4: Commit one-use confirmation**

```powershell
git add core/pit_optimizer_v5/confirmation.py core/pit_optimizer_v5/cli.py tests/test_pit_optimizer_holdout.py
git commit -m "feat: add one-use optimizer v5 confirmation"
```

- [ ] **Step 5: Freeze the discovery champion before opening confirmation**

```powershell
python -B -m core.pit_optimizer_v5.cli build-confirmation-attempt --discovery-manifest .artifacts/pit-optimizer-v5/discovery/manifest.json --discovery-state .artifacts/pit-optimizer-v5/discovery/checkpoint.json --confirmation-plan .artifacts/pit-optimizer-v5/panels/confirmation-plan.json --confirmation-ledger .artifacts/pit-optimizer-v5/panels/confirmation-retirement.json --output .artifacts/pit-optimizer-v5/confirmation-attempt.json
```

Expected: a create-only `ConfirmationAttemptCommitmentV5` binds the one authenticated discovery
champion, its immutable experiment/source references, the confirmation-plan digest, evaluator,
baseline, sandbox, and the exact mutable-ledger locator plus its immutable unused pre-open snapshot.
It contains every resolvable
reference needed by `confirm`.

- [ ] **Step 6: Authenticate, open once, evaluate, and retire**

```powershell
python -B -m core.pit_optimizer_v5.cli confirm --attempt .artifacts/pit-optimizer-v5/confirmation-attempt.json --artifact-root .artifacts/pit-optimizer-v5/confirmation
```

Before deserializing the held-out composition, append an opened attempt to the confirmation ledger.
On success, evaluation failure, timeout, cancellation, identity failure after opening, or controller
crash, append or recover one terminal retirement record; that retirement domain can never evaluate
another candidate. Expected: zero provider calls, exact committed-candidate reconstruction in a
disposable worker, same-panel baseline and candidate evidence, V5 execution profile, closed
`ConfirmationOutcomeV5`, source unchanged, and cleanup complete.

- [ ] **Step 7: Decide whether a qualification attempt is warranted**

Confirmation is sufficient to request qualification only when the candidate is behaviorally active,
beats the V5 baseline on confirmation CAGR, and contains no causal/accounting failure. This is not a
weighted score and does not claim the 10% target has been met.

- [ ] **Step 8: STOP for a separate qualification decision**

Do not expose or reserve the qualification panel in this task.

### Task 7: Run Detached One-Use Qualification

**Files:**

- Create: `core/pit_optimizer_v5/qualification.py`
- Modify: `core/pit_optimizer_v5/cli.py`
- Modify: `core/pit_optimizer_holdout.py`
- Modify: `tests/test_pit_optimizer_holdout.py`
- Create locally only: `.artifacts/pit-optimizer-v5/qualification-attempt.json`
- Create locally only: `.artifacts/pit-optimizer-v5/qualification/`

**Interfaces:**

- Consumes: operator approval and a create-only qualification-attempt commitment that resolves the
  frozen confirmed candidate, sealed 2025 qualification plan, retirement ledger, and V5
  baseline/execution/sandbox identities.
- Produces: permanently retired baseline/candidate qualification evidence and a target decision.

- [ ] **Step 1: Add V5 adapter assertions to existing holdout tests**

Require retirement on success and failure, zero provider calls, exact three-universe coverage,
out-of-time and out-of-security separation, same-panel baseline/candidate identity, closed
`QualificationOutcomeV5` gate recomputation, authenticated equality of the baseline and candidate
friction/scenario-grid identity (matching the qualification attempt commitment), and no mutation of
search memory.

- [ ] **Step 2: Implement the V5 qualification adapter**

Reuse the existing create-only retirement ledger. Implement and register both
`build-qualification-attempt` and `qualify` in `core/pit_optimizer_v5/cli.py`. Reconstruct the
candidate only inside a disposable workspace, run baseline and candidate with the same V5 execution,
sandbox, and authenticated friction/scenario-grid identities, verify those identities match the
attempt commitment and each evidence record, write the closed `QualificationOutcomeV5`, and clean
up before returning.

- [ ] **Step 3: Run focused holdout checks**

```powershell
python -B -m pytest tests/test_pit_optimizer_holdout.py -q
python -B -m ruff check core/pit_optimizer_v5/qualification.py core/pit_optimizer_v5/cli.py core/pit_optimizer_holdout.py
```

- [ ] **Step 4: Commit detached V5 qualification**

```powershell
git add core/pit_optimizer_v5/qualification.py core/pit_optimizer_v5/cli.py core/pit_optimizer_holdout.py tests/test_pit_optimizer_holdout.py
git commit -m "feat: add detached optimizer v5 qualification"
```

- [ ] **Step 5: Build the exact attempt only after explicit operator approval**

```powershell
python -B -m core.pit_optimizer_v5.cli build-qualification-attempt --confirmation-outcome .artifacts/pit-optimizer-v5/confirmation/outcome.json --qualification-plan .artifacts/pit-optimizer-v5/panels/qualification-plan.json --qualification-ledger .artifacts/pit-optimizer-v5/panels/qualification-retirement.json --output .artifacts/pit-optimizer-v5/qualification-attempt.json
```

Expected: a create-only `QualificationAttemptCommitmentV5` contains authenticated relative
references for every required input, including the mutable retirement-ledger locator and exact
immutable pre-open snapshot, and binds the exact confirmed policy before the held-out plan is opened.
It also binds the immutable friction/scenario-grid reference used by both baseline and candidate.
The builder reparses candidate/baseline evidence and fails closed unless confirmation status is
`completed`, `eligible_to_request_qualification` recomputes true, and the qualification scenario-grid
identity is authenticated and equal for the attempt, baseline, and candidate; operator approval
authorizes the eligible stage but cannot bypass that evidence gate.

- [ ] **Step 6: Execute the committed attempt**

```powershell
python -B -m core.pit_optimizer_v5.cli qualify --attempt .artifacts/pit-optimizer-v5/qualification-attempt.json --artifact-root .artifacts/pit-optimizer-v5/qualification
```

Success means the candidate's net annualized portfolio return is at least the manifest target and
strictly exceeds the same-panel V5 baseline evaluated under the exact shared authenticated
friction/scenario-grid identity. Report trade count, exposure, drawdown, costs, and episode/year
evidence, but do not convert them into a weighted score.

### Task 8: Prepare Full Replay Without Starting It

**Files:**

- Create: `core/pit_optimizer_v5/readiness.py`
- Modify: `core/pit_optimizer_v5/cli.py`
- Modify: `tests/test_pit_optimizer_holdout.py`
- Create locally only: `.artifacts/pit-optimizer-v5/full-replay-readiness.json`

**Interfaces:**

- Consumes: a successful, authenticated, permanently retired qualification outcome.
- Produces: a content-free full-replay readiness record and exact local command.

- [ ] **Step 1: Add read-only readiness assertions**

Require an authenticated successful retired qualification, clean matching source, complete sandbox
and cleanup evidence, and zero side effects. A failed identity produces one typed blocker and does
not start Docker, a provider, or replay.

- [ ] **Step 2: Implement and register `full-replay-readiness`**

`readiness.py` parses the closed `QualificationOutcomeV5`, walks its complete artifact graph, and writes one create-only,
content-free readiness record. Register the command in `cli.py`; it renders but never executes the
full-replay command.

- [ ] **Step 3: Run focused readiness checks**

```powershell
python -B -m pytest tests/test_pit_optimizer_holdout.py -k "readiness" -q
python -B -m ruff check core/pit_optimizer_v5/readiness.py core/pit_optimizer_v5/cli.py
```

- [ ] **Step 4: Commit replay readiness**

```powershell
git add core/pit_optimizer_v5/readiness.py core/pit_optimizer_v5/cli.py tests/test_pit_optimizer_holdout.py
git commit -m "feat: add optimizer v5 replay readiness"
```

- [ ] **Step 5: Render readiness**

```powershell
python -B -m core.pit_optimizer_v5.cli full-replay-readiness --qualification-outcome .artifacts/pit-optimizer-v5/qualification/outcome.json --output .artifacts/pit-optimizer-v5/full-replay-readiness.json
```

Expected: candidate, source, execution, bundle, baseline, qualification, cleanliness, sandbox, and
cleanup identities all pass; no replay process starts.

- [ ] **Step 6: STOP for explicit full-replay approval**

Full replay is a separate long-running operation. Do not start it as part of campaign completion.

## Completion Criteria

- Deterministic plans separate discovery, confirmation, and qualification by both time and security lineage.
- Provider-free fixture execution proves the complete feedback and local-variant loop.
- A live discovery campaign can evaluate several local candidates per model-authored template.
- Every critic's full direction persists and reaches later investigators.
- Confirmation and qualification make zero provider calls.
- The frozen-candidate confirmation and qualification panels are each retired exactly once
  regardless of outcome.
- Double-digit success is reported only when the untouched V5 qualification reaches the configured net annualized portfolio-return target.
- Full replay remains a separately approved final step.
