# V5 Mechanism Evidence: Codex Implementation Plan

**Date:** 2026-09-18  
**Status:** Planning handoff only; every implementation task below is pending.  
**Base inspected:** `9aa52976f898c27f78c1857ceecde6a7b22a5aab`.

**Goal:** Attach precommitted, independently checkable mechanism evidence to a V5 candidate without
changing strategy behavior, evaluator semantics, CAGR ranking, or legacy artifacts.

**Design:** [V5 mechanism evidence design](../specs/2026-09-18-v5-mechanism-evidence-design.md).  
**Start here:** [Codex handoff](../handoffs/2026-09-18-v5-mechanism-evidence/README.md).  
**Preserved checkpoint:** [September 9 paused handoff](../handoffs/2026-09-09-pit-optimizer-v5/README.md).

## Read this before doing any task

The owner requested repository publication of these plans. No code, tests, campaigns, strategy,
provider configuration, or data was changed for this handoff. No tests or backtests were run.

The saved September 9 checkpoint prohibits automatic resumption and records a restriction on
reading/running tests and synthetic trials. This plan describes proposed future verification; it
does not lift that restriction. Before later implementation, obtain explicit authorization for the
bounded implementation and any restricted verification. Do not revive a saved campaign or assume
an older grant covers new source, altered contexts, or these diagnostics.

Preserve all design boundaries. In particular: no broker actions; no real model/provider calls;
no confirmation/qualification access; no edits to prepared `.artifacts` trees; no new defaulted
fields in hashed V5 records; no broad legacy-suite expansion; no change to search acceptance.

## Deliverables and file ownership

Keep the extension small. These new paths and symbols are proposals, not existing APIs.
Reconcile names with the current checkout before creating them.

| Proposed file | Responsibility |
| --- | --- |
| `core/pit_optimizer_v5/mechanism_contracts.py` | Frozen spec/binding/report types, closed statuses, validation and canonical identities. |
| `core/pit_optimizer_v5/mechanism_probes.py` | Pure bounded case planning and paired observation reduction; execution via an injected worker port only. |
| `core/pit_optimizer_v5/mechanism_reports.py` | Deterministic prediction assessment and local/role-safe report projection. |
| `core/pit_optimizer_v5/mechanism_artifacts.py` | Thin sidecar/index adapter using existing artifact primitives and explicit identities. |
| `tests/test_pit_optimizer_v5_mechanism_evidence.py` | Proposed focused contract/probe/report checks, only after test authorization. |
| `tests/test_pit_optimizer_v5_mechanism_integration.py` | Proposed focused artifact/mode/resume/projection checks, only after test authorization. |

Potential existing integration files: `probes.py`, `runtime.py`, `production_runtime.py`,
`provider.py`, `memory.py`, `artifacts.py`, and `summary.py` within `core/pit_optimizer_v5/`.
Inspect and modify only the necessary seams; this table is not permission to edit every listed file.
Prefer independent sidecar bindings over modifying core contracts. Do not add dependencies or change
`agent_loop.py`, strategy-policy sources, `config/settings.py`, the evaluator, or `search.py`.

Task dependencies: **0 -> 1 -> (2 and 3) -> 4 -> 5 -> 6**. One integration owner controls all
existing shared adapters. Domain and report work may proceed independently only after the contracts
are agreed. No concurrent agent should edit a shared runtime/provider/memory file.

## Task 0: Reconcile the current branch and authorization

- [ ] Read repository guidance and the preserved handoff; record current commit, branch and clean/
  dirty status without changing another agent's worktree.
- [ ] Compare current V5 contracts, semantic modes, role evidence projection, artifact primitives,
  and admission bounds against the inspected base. Record actual symbol locations and drift.
- [ ] Record permission separately for implementation, test-file inspection, synthetic verification,
  discovery inputs, and campaign/provider execution. The last two are not needed for the first slice.
- [ ] Select a new implementation branch. Keep the original prepared campaign/worktree unchanged.
- [ ] Confirm new filenames do not collide with work already performed by another agent.

**Acceptance:** a short source/authorization map, a scoped file allowlist, and an explicit statement
that campaign execution and trading remain out of scope. If a prerequisite is missing, document it
without claiming the corresponding task complete or executing a substitute.

## Task 1: Define and freeze the experiment contract

**Create:** `mechanism_contracts.py` and the contract section of the proposed focused tests.

- [ ] Define `MechanismExperimentSpecV1` separately from `HypothesisV5`, referencing the hypothesis,
  parent revision, target method, permitted symbols, protected behaviors, and registered predictions.
- [ ] Define a two-step identity: hash the spec before authoring; bind exact rendered candidate,
  parent, evaluator, case corpus, recipe and scenario before observations exist.
- [ ] Define prediction fields for metric ID, units, direction, tolerance, aggregation, relevant-case
  predicate, denominator, minimum cases, and disconfirming observation. Use typed finite numerics.
- [ ] Define execution validity, coverage, and prediction assessment as separate concepts. Support
  `supported_on_cases`, `contradicted_on_cases`, and `insufficient_evidence` without a confidence score.
- [ ] Validate all declarative predicates against an explicit registry. Reject executable conditions,
  unsupported metrics, missing units, invalid tolerance, unknown fields, and heldout provenance.
- [ ] Keep existing V2-V5 dataclass signatures and canonical serializers unchanged.

**Proposed checks:** stable canonical round-trip; digest changes when a declared experiment property
changes; invalid metric/units/NaN rejected; mismatched parent/candidate binding rejected; empty
relevant set is insufficient evidence; retrospective evidence cannot become precommitted evidence.

**Acceptance:** pure, import-safe contracts with documented units and a complete status table.
No imports of CLI, provider transport, Docker, filesystem implementation, or candidate source.

## Task 2: Implement bounded paired probes

**Create:** `mechanism_probes.py`; extend only the proposed domain test file.

- [ ] Build cases from frozen controller-owned recipes; reuse canonical probe encoding and the
  existing policy snapshot validators instead of creating a competing snapshot schema.
- [ ] Cover allowed changed-value boundaries with type-appropriate neighbors and negative controls.
  Validate compound snapshot invariants. Record unsupported recipes explicitly.
- [ ] Compare exact parent and candidate on identical validated snapshots through a dependency-
  injected port implemented by the existing bounded worker. Never import untrusted candidate code
  into the controller or run it through unrestricted `exec`/`eval`.
- [ ] Bind recipe version, all case inputs, order, repetitions, worker reset behavior and budgets.
  Repeated observations must not collapse exceptions, timeouts, booleans, integers or decimals.
- [ ] Record relevant-case and decision-difference counts. Report branch coverage unavailable unless
  independently measured; do not infer it from source changes.
- [ ] Preserve fixed-suite ID, fingerprint computation and admission classifications. Supplemental
  results are evidence only; no rescue path or change to early rejection is implemented here.

**Proposed checks:** synthetic threshold pair 0.40/0.60 identical at 0.20/0.80 but different at 0.50;
exact boundary behavior; invalid compound snapshot; deterministic ordering and budgets; protected
same-input controls; wrong-parent binding; nondeterminism; policy exception/timeout as execution
failure rather than a contradicted prediction. These numbers are test fixtures, not trading settings.

**Acceptance:** a bounded, reproducible witness report with no ranking side effects. A finite probe
suite is described only in terms of observed cases, never universal equivalence.

## Task 3: Build the mechanism report and trusted measurement adapter

**Create:** `mechanism_reports.py`; extend the proposed domain test file.

- [ ] Reduce paired observations with registered formulas. Publish counts, denominators, units,
  tolerance and deltas so the assessment can be independently recomputed.
- [ ] Assess every preregistered prediction. Preserve mixed, missing, unsupported and unexercised
  outcomes instead of selecting whichever metric improved.
- [ ] Keep identical-snapshot decision comparisons separate from portfolio-consequence comparisons.
  Match existing parent/candidate episode, scenario and evaluator identities before deriving deltas.
- [ ] Reuse actual available `diagnostics.py`/`EvaluationReportV5` measurements. If a proposed metric
  is not available, mark it unavailable; do not estimate it from unrelated aggregates or add a new
  backtest-engine measurement in this first slice.
- [ ] Keep campaign baseline and authored parent comparisons separately labeled.
- [ ] Produce a complete local report and an allowlisted symbol-neutral projection. Neither an LLM
  explanation nor higher CAGR can override an unsupported/contradicted measured mechanism.

**Proposed checks:** known paired deltas; tolerance edges; zero denominator; insufficient case count;
mixed prediction results; missing measurements; mismatched episode/cost profile; distinction between
local-control invariance and downstream portfolio divergence; deterministic report identities.

**Acceptance:** a reviewer reconstructs every assessment from recorded observations and declared
rules, without a critic or a real replay. No opaque score or new rejection criterion is introduced.

## Task 4: Persist versioned sidecars and integrate opt-in local execution

**Create:** `mechanism_artifacts.py`. One integrator owns necessary existing adapters and the
proposed integration test file.

- [ ] Review an authenticated extension capability binding the new diagnostic context to exact
  source, manifest, spec, case corpus and resource bounds. Do not enable it via an unbound flag.
- [ ] Keep the first runnable integration limited to supplied authorized evidence. Do not build a
  market-data extractor, new CLI workflow, or historical campaign launcher.
- [ ] Use a create-only sidecar and canonical index linked to the existing experiment ID; validate
  the full identity graph and reuse current safe-path/digest/atomic-write primitives.
- [ ] Make disabled legacy operation a true no-op: no changed records, provider inputs, decisions,
  file creation, fingerprint values, call counts, or timing allocation.
- [ ] For an enabled report context with `disabled_development`, emit truthful `not_run` evidence
  without invoking the policy runner or manufacturing semantic observations.
- [ ] For separately authorized required-mode execution, collect supplemental evidence inside the
  existing bounded validation/semantic stage without changing its terminal decision. All work is
  charged to the existing stage/round deadline; no resets or hidden retries.
- [ ] Make restart idempotent: complete matching records are reused; incomplete output is never
  treated as success; required corrupt evidence fails closed locally, without repeating model calls.

**Proposed checks:** legacy-off byte equivalence; no output in disabled legacy mode; zero worker
calls for `disabled_development`; sidecar digest and path-traversal failures; foreign identities;
atomic interrupted write; repeated resume; unchanged early-rejection outcome; shared deadline
exhaustion; bounded cleanup. No legacy manifest or historical artifact is rewritten for a test.

**Acceptance:** the extension can be absent without changing behavior, and enabled reports cannot
misrepresent skipped or failed execution. The prepared September 9 campaign remains untouched.

## Task 5: Connect bounded evidence to critic and learning memory

**Modify only after seam review:** selected provider/production-runtime/memory/summary adapters.
No extra model role or model call is added.

- [ ] Register approved mechanism measurements in the existing role-evidence vocabulary. Review
  sanitization, supported prefixes, schema, exact request identity, token bounds and admission
  accounting as one change. Do not add unrecognized fields to a frozen provider message.
- [ ] Issue evidence IDs from authenticated report bytes; role output may cite only IDs supplied
  with that exact request. Preserve the current critic review shape where possible.
- [ ] Project counts, measurement deltas, coverage and limitations only. Keep full local evidence
  accessible through sidecars rather than copying raw snapshots or source into role memory.
- [ ] Preserve negative and inconclusive lessons with their parent, scope and identity. Truncation
  of role context must not drop qualifiers such as `not_run` or `insufficient_evidence`.
- [ ] Treat critic narrative as interpretation. Do not allow it to rewrite report statuses, claim
  market causation, alter qualification access, or promote a candidate through new scoring rules.

**Proposed checks:** unknown/foreign evidence IDs rejected; no raw rows, symbols, dates, local paths,
source text, credentials or qualification content in the provider projection; deterministic request
bounds; preserved negative results across restart; old role messages unchanged when disabled;
new evidence does not add calls or bypass admission.

**Acceptance:** an existing critic request can explain the mechanism using supplied measured IDs,
under the current bounded provider contract, without changing search selection or calling a model
for this implementation verification.

## Task 6: Independent review and closeout

- [ ] Review the exact diff against the file allowlist and all non-goals. Flag any strategy, evaluator,
  ranking, fingerprint, qualification, authorization or historical-artifact change as out of scope.
- [ ] Verify task-specific acceptance only with methods currently authorized by the owner. Record
  exact commands, result, source commit, and scope; distinguish static review from execution.
- [ ] Verify the focused checks below after explicit test authorization. Do not run broader suites
  just because an old plan names them, and do not fabricate a provider/evaluator success.
- [ ] Produce a handoff containing changed files, contract versions, remaining limitations,
  authorization state and actual verification evidence. No performance claims without a separately
  authorized matched experiment. No autonomous campaign launch after the implementation completes.

### Proposed offline verification, not executed by this planning change

The following commands apply only after the files exist and the owner explicitly permits these
specific test/synthetic checks. Test fixtures must use `tmp_path`, supplied data and mocked/injected
external ports; do not access `.env`, real providers, saved campaigns or heldout data.

```powershell
python -B -m pytest -p no:cacheprovider --no-cov -q -m "not integration" tests/test_pit_optimizer_v5_mechanism_evidence.py tests/test_pit_optimizer_v5_mechanism_integration.py
python -m ruff check core/pit_optimizer_v5/mechanism_contracts.py core/pit_optimizer_v5/mechanism_probes.py core/pit_optimizer_v5/mechanism_reports.py core/pit_optimizer_v5/mechanism_artifacts.py tests/test_pit_optimizer_v5_mechanism_evidence.py tests/test_pit_optimizer_v5_mechanism_integration.py
git diff --check
```

Also lint any existing adapter actually changed. Follow repository-required checks when authorized;
never report a skipped check as passing. The current documentation PR intentionally carries a CI
skip annotation to preserve the saved no-test boundary; do not copy that annotation into a later
implementation PR or bypass required implementation checks.

## Definition of done

The authorized first implementation slice is complete only when identity/provenance are sealed,
paired observations are reproducible, unavailable evidence is truthful, legacy-off behavior is
unchanged, heldout/provider boundaries are intact, and an independent review confirms no strategy
or ranking change. Any unperformed verification stays explicitly unverified. A campaign CAGR
increase, live deployment, or successful resumed development campaign is not required for this
workstream and must not be implied by its completion.
