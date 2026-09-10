# PIT optimizer V5 campaign admission implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development task by task. The user already selected subagent development. One production writer, fresh implementer per increment, independent spec and quality review after every freeze. This is a subplan of the existing transition; retain its ledger and evidence.

**Goal:** Enforce two-evaluated-round stopping and a finite prospective role reserve in the native development campaign before preparing its real paid proposal.

**Architecture:** New immutable policy and create-only enrollment preserve every existing canonical V5 shape and identity. Shared pure budget predicates and immutable admission readers feed native request/ledger guards, campaign operations and historical compliance. Historical claims cover logical eligibility and stored epochs, without inventing cross-file publication timing.

**Tech Stack:** Python 3.13, existing frozen dataclasses/canonical codec/local artifact repository, Decimal, existing native campaign/runtime/ledger. No dependencies.

**Spec:** `docs/superpowers/specs/2026-09-08-pit-optimizer-v5-campaign-admission-design.md`; independent design PASS in `.artifacts/pit-optimizer-v5/development/increment-c-campaign-proposal/campaign-admission-independent-design-review.md`.

## Global constraints

- Work only in the existing `codex/pit-optimizer-v5-architecture` worktree. Do not reset, stash, clean, rebase, switch, commit or push.
- No tests may be read, created, modified or run. No synthetic requests, outcomes, records, probes, mocks or fake future coverage.
- No credentials, environment value dumps, provider/API/model calls, Docker/evaluation execution, image rebuild, campaign launch or new historical bindings during this subplan.
- Preserve all 579 runtime-04 and 83 actual06 archive files and the 56-file evaluator closure `005064e57e80840f3b2a97d7db694b176832b8f982996e63efc8a35064505ed4`.
- Historical typed-state reads use `repair=False` transitively. No operational adapter, capability, nonce, clock or migration in completed-history verification.
- Preserve existing dataclass fields/defaults, canonical digest domains, default legacy behavior, original owners/configs/deadlines and source newline bytes outside approved spans.
- Static verification only until full source review: scoped Ruff, in-memory compile, source/AST/diff/hash inspection. No py_compile, imports for probes or test discovery.
- C-c limits: target 2 evaluated rounds, 3 attempts, 8 calls, 600000 tokens, 75000 prospective total tokens per role, 8192 max output, zero retries/repair, one hypothesis/axis/variant/survivor, no source escape.
- Preserve honest provider overage/failure accounting. No invented denied package or round outcome. Distinct operational stop reasons do not alter summary schemas/count formulas.
- Each implementer saves exact preimages and narrow diffs under the proposal evidence directory; freezes source before independent review. Full actual summaries are deferred until P4 review, once each in a fresh package.

## Source map and interfaces

Create `core/pit_optimizer_v5/campaign_admission.py` for immutable policy, selected-state authentication and pure decisions. Extend it with data-only admission facts/readers; keep private helpers here instead of duplicating reserve arithmetic in live/history consumers. Use local imports where existing operations/development/provider imports would otherwise cycle.

Modify only native request return sites and ledger reservation in P2, operations campaign enrollment/boundaries/CLI rendering in P3, and completed-history entry in P4. No unrelated refactor.

New frozen types (no fields added to old types):

```python
@dataclass(frozen=True, slots=True)
class CampaignAdmissionPolicyV5:
    schema_version: int
    policy_revision: int
    manifest_ref: ArtifactRefV5
    adapter_config_ref: ArtifactRefV5
    owner_token_sha256: str
    repository_root_identity_sha256: str
    target_evaluated_feedback_rounds: int
    prospective_total_tokens_per_role: int

@dataclass(frozen=True, slots=True)
class CampaignAdmissionBindingV5:
    schema_version: int
    policy_ref: ArtifactRefV5
    manifest_ref: ArtifactRefV5
    adapter_config_ref: ArtifactRefV5
    owner_token_sha256: str
    repository_root_identity_sha256: str

@dataclass(frozen=True, slots=True)
class AuthenticatedCampaignAdmissionV5:
    binding: CampaignAdmissionBindingV5
    policy: CampaignAdmissionPolicyV5
```

Schema version 5, policy revision 1. Exact integer/type checks reject bool; digest validation follows existing closed grammar. Later increments can add private data-only fact types without altering these serialized contracts.

## P1: Immutable policy and budget primitives

**Files:** create `core/pit_optimizer_v5/campaign_admission.py`; evidence/report only otherwise.

**Interfaces produced:**

```python
def authenticate_prepared_campaign_policy_v5(
    *, repository: LocalArtifactRepositoryV5, manifest: CampaignManifestV5,
    policy_ref: ArtifactRefV5,
) -> CampaignAdmissionPolicyV5: ...

def load_campaign_admission_v5(
    *, repository: LocalArtifactRepositoryV5, manifest: CampaignManifestV5,
    expected_policy_ref: ArtifactRefV5 | None = None,
) -> AuthenticatedCampaignAdmissionV5 | None: ...

def require_role_envelope_v5(
    *, policy: CampaignAdmissionPolicyV5, manifest: CampaignManifestV5,
    request: RoleRequestV5,
) -> None: ...

def campaign_boundary_decision_v5(
    *, policy: CampaignAdmissionPolicyV5, manifest: CampaignManifestV5,
    evaluated_feedback_rounds: int, attempted_rounds: int,
    external_attempts: int, total_tokens: int, cost_usd: Decimal,
) -> Literal['admit', 'evaluated_feedback_target_reached', 'insufficient_milestone_reserve']: ...
```

- [x] Save preimages/source hashes and read only the necessary contract, codec, manifest, config and repository implementations.
- [x] Implement the frozen types and prepared authentication: exact paid-development manifest graph, original config type/digest/root, original policy references and finite single-candidate/zero-retry assumptions. Prepared authentication has no enrollment requirement and performs no write.
- [x] Implement selected namespace census across value/index unions, canonical manifest key and selected foreign-key detection. Missing half or corrupt/orphan selection raises; both absent returns None only without an explicit expected ref. Binding and policy must agree on every original identity. No recovery here.
- [x] Implement exact prospective envelope and conservative Decimal price bound, validating `maximum_total_tokens >= maximum_role_calls * envelope` and the analogous finite-USD capacity. Use the existing conservative arithmetic utilities/rounding instead of float conversions.

```python
missing = max(0, policy.target_evaluated_feedback_rounds - evaluated_feedback_rounds)
if missing == 0:
    return 'evaluated_feedback_target_reached'
needed_calls = 3 * missing
# Compare remaining attempts/calls/tokens and, when finite, USD against missing,
# needed_calls, needed_calls * envelope and needed_calls * worst-envelope cost.
```

- [x] Check exact type/nonnegative actual counters without executing fake inputs. Preserve failure precedence as caller responsibility; do not reject real overage history merely for exceeding a conforming-settlement invariant.
- [x] Run scoped Ruff and in-memory compile, inspect all immutable callpaths and untouched old schemas/digests. Save report, narrow diff and hashes. Independent P1 review must pass before hooks.

## P2: Native live admission authority and request guards

**Files:** extend `campaign_admission.py`; modify `production_runtime.py` LocalRoleRequestFactoryV5 and `production_provider.py` reserve_role_slot only, plus necessary imports.

**Consumes:** all P1 interfaces.

**Produces:**

```python
def require_live_role_admission_v5(
    *, repository: LocalArtifactRepositoryV5, manifest: CampaignManifestV5,
    request: RoleRequestV5, round_index: int, now_epoch_ms: int,
    owner_token_sha256: str | None = None,
) -> None: ...
```

- [x] Save fresh preimages. Trace actual request bindings, campaign/round records, journal/cleanup/checkpoint and shared immutable ledger reader; no operational constructors for authentication.
- [x] Extend the module with shared read-only logical-prefix facts. Authenticate original launch/config/owner/root and matching round configuration/start, exact role request round/call binding, original stored campaign/round epochs and deadlines. RoleRequestV5 has no campaign/round field: factory passes actual inputs.round_index and checks inputs.owner_token_sha256 against enrolled original owner; ledger derives round_index from the exact persisted RoleCallKeyV5 and verifies campaign/role/primary attempt position. Never infer round from request content. Reconstruct prior-round settled evidence and authentic paid usage; all prior evaluated counts require committed authenticated records/critics and successful cleanup. Reject gaps, conflicting/later unsettled state, orphan selected state and noncanonical round/request entries rather than trusting a raw final counter.
- [x] Apply P1 boundary predicate to the admitted round's prior-round prefix. Current-round author/critic continuation must not subtract its own spending as though admitting a new three-call round. No missing start is admissible in this function. Legacy absence returns without adding new requirements. Reload admission at call time, never cache absence.
- [x] Add one private guarded-return method to LocalRoleRequestFactoryV5 and wrap its three complete build results. It calls the new live helper using the real current epoch only when enrolled; legacy result bytes/identity remain unchanged.

```python
def _guarded_request(self, inputs: FeedbackRoundInputV5, request: RoleRequestV5) -> RoleRequestV5:
    # Delegate policy discovery, exact authority and envelope checks to shared reader.
    require_live_role_admission_v5(
        repository=self.repository, manifest=self.manifest, request=request,
        round_index=inputs.round_index,
        owner_token_sha256=inputs.owner_token_sha256,
        now_epoch_ms=time.time_ns() // 1_000_000,
    )
    return request
```

- [x] Repeat the guard inside the existing ledger transition after exact persisted request recovery and before constructing/appending a reservation. Preserve all old budget/owner/model/schema/accounting checks and unrelated methods. Do not nest a second ledger transition, invoke providers, or manufacture denial records.
- [x] Factory passes the actual caller owner; ledger omits it and authenticates durable original ownership plus its exact persisted call. Legacy absence returns before new owner checks. Permit current replay/recovery only for the exact current request/call and matching existing tail; never filter away other orphan requests or later foreign state. A settled current round cannot admit a new role/call, though authentic completed-request replay may remain read-only. Reuse of a completed role must not turn into another paid reservation.
- [x] Trace factory-before-`_complete_role` persistence and ledger-before-reservation append; verify direct native entry requires existing admitted round authority. Source/AST checks must demonstrate three guarded return sites and unchanged protected operations. Ruff/compile, freeze, independent P2 review.

## P3: Enrollment, native campaign stopping and command plumbing

**Files:** extend `campaign_admission.py` only for focused shared boundary support; modify `operations.py`; modify `cli.py` only if its parser owns a required option.

**Consumes:** P1 policy/binding/decisions, P2 shared immutable facts.

**Public operation extension:** `_run_campaign_v5(..., campaign_policy_ref: ArtifactRefV5 | None = None)`; same optional argument in `render_production_command_v5`, native dispatch parsing and generated resume command. Existing run wrappers already forward keyword arguments.

- [x] Save preimages and inspect real campaign launch lock, freshness checks, pre-start state and existing resume paths.
- [x] Under the existing campaign-launch transition authenticate fresh explicit policy and expected original manifest/config/owner/root. Publish the create-only binding before unchanged CampaignLaunchV5. No auto-enrollment of existing legacy launch. Resume discovers binding even with omitted arg and supplied mismatch fails.
- [x] Implement exact binding-before-launch interrupted-publication handling only when explicit expected ref, all original identities and no-start/no-spend freshness are authenticated first. Do not call repair on an unvalidated orphan. Reuse `recover_typed_state_index` with the exact expected value if its existing semantics are appropriate; never overwrite. Historical readers remain immutable.
- [x] Use shared authenticated settled-boundary facts after reconciliation, ownership and cleanup checks for both reused and new rounds. Inspect all observed starts before success; a later pending round prevents success. Run the boundary decision before another round config and again before first start for existing pre-start config. Existing already-started continuation uses original admission/deadlines.

```python
decision = campaign_boundary_decision_v5(
    policy=admission.policy, manifest=manifest,
    evaluated_feedback_rounds=facts.evaluated_feedback_rounds,
    attempted_rounds=facts.attempted_rounds,
    external_attempts=facts.external_attempts,
    total_tokens=facts.total_tokens, cost_usd=facts.cost_usd,
)
if decision != 'admit':
    reason = decision
    break
```

- [x] Preserve runtime/provider/cleanup/authority/deadline/novelty failure precedence, no callback control, and exact summary formulas. Target stop reports completed with distinct reason; reserve refusal reports stopped. Do not append fake outcomes to support either stop. Preserve default legacy behavior.
- [x] Render and parse policy artifact path plus SHA with exact reference validation; include independently pinned policy in resume rendering when enrolled. Do not create new launcher wrappers here; final C-c package uses reviewed native plumbing.
- [x] Verify failure/crash ordering and protected shapes by source/AST/diff, Ruff/compile. Freeze and obtain independent P3 review before historical integration.

## P4: Historical compliance, integrated review and actual preservation

**Files:** `campaign_admission.py`, `development_preparation.py` completed-history entry; `historical_verification.py` only if sharing authenticated data facts requires a focused extraction; evidence and readiness docs.

**Produces:** explicit expected-policy entry returning a new data-only compliance report:

```python
def authenticate_campaign_policy_compliance_v5(
    *, repository: LocalArtifactRepositoryV5, manifest: CampaignManifestV5,
    expected_policy_ref: ArtifactRefV5,
) -> CampaignPolicyComplianceV5: ...
```

- [x] Save preimages. Integrate optional policy discovery into completed-history authentication after its own original full census/resources/roles/projection verification. Legacy absent returns exactly the prior behavior, summaries unchanged.
- [x] Reuse shared immutable facts/predicates to authenticate enrolled actual requests, logical prior-round eligibility, all original launch/start epochs against deadlines, and observed pre-start publication. Reject unfinished publication and post-target/insufficient-reserve admissions. Avoid recursive public authentication by private data-only composition after the public caller establishes its own original provenance.
- [x] Expose expected-ref compliance requiring exact enrolled authority. Report actual evaluated rounds/usage/decision and explicit logical-prefix/temporal-evidence limitation. Do not rewrite provider overage or assert cleanup/checkpoint publication time that was not stored. No dynamic positive-policy claim before real C-c evidence exists.
- [x] Ruff/compile, complete callgraph/repair audit, old dataclass/protected-definition/diff checks and final hashes. Independent broad review covers P1-P4 combined, request guard placement, lower-level bypass, enrollment/crash, failure precedence and historical immutability. Fix through original implementer and scoped re-review.
- [x] After full review, prepare a fresh collector/output package based on accepted H3c collector; pin new independent-review SHA and current source hashes. Static-review collector before executing. Run one direct actual completed-history summary per archive, compare runtime04 prior23 non-command fields/counts and actual06 all27 fields/counts, preserve all662 archive files/56 evaluator files. No binding attached to old archives.
- [x] Update C-b readiness with new host-source admission changes and actual results. Independent readiness approval precedes actual C-c manifest/config/policy/initial-preview creation, complete reviewable launch and fresh paid grant. The transition goal continues through actual two evaluated rounds and Luna handoff; this subplan is not final goal completion.

## Plan self-review

Spec coverage: P1 owns compatibility/cap arithmetic/selected-state reads; P2 owns native pre-persistence and pre-spend guards plus exact admitted round; P3 owns fresh enrollment/crash/resume and settled stopping; P4 owns logical historical compliance/expected pin and actual legacy preservation. No serialized old fields or test/commit steps. Actual preparation and execution stay with original C-c/C-d/C-e transition after this source readiness gate.
