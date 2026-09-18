# V5 Mechanism Evidence: Codex Implementation Plan

**Date:** 2026-09-18  
**Revision:** 2 — current-source review and evidence-to-hypothesis testing.  
**Status:** Planning only; all implementation tasks and proposed execution checks are pending.  
**Latest source inspected:** `15ba962743da2c2ca73becdf85bc639c4f670dfa` on `main`.  
**Original planning base:** `9aa52976f898c27f78c1857ceecde6a7b22a5aab`.

**Goal:** Add precommitted mechanism evidence and bounded, qualified reuse through the existing V5
learning loop. Preserve strategy-policy code, evaluator semantics, CAGR ranking, and legacy artifacts.

**Design:** [Original mechanism design](../specs/2026-09-18-v5-mechanism-evidence-design.md), supplemented
by the [current-source review and design addendum](../handoffs/2026-09-18-v5-mechanism-evidence/source-review.md).  
**Start here:** [Codex handoff](../handoffs/2026-09-18-v5-mechanism-evidence/README.md).  
**Preserved checkpoint:** [September 9 paused handoff](../handoffs/2026-09-09-pit-optimizer-v5/README.md).

## What this revision changes

V5 already stores experiment evidence and reuses authenticated hypotheses and critic directions in
`LocalRoleRequestFactoryV5._investigator_parts`. Do not add another generic memory system.

The original report/probe work remains useful. This revision extends its acceptance criteria through
the actual next investigator request and selected-hypothesis handoff. Task 5 now has explicit critic,
conditional-lesson, and two-round integration checks. The review's verification matrix is normative
for that extension, but none of its checks has been executed by this documentation change.

## Authorization and compatibility boundaries

This request authorizes source review and documentation updates, not executable implementation or
campaign execution. The September 9 checkpoint records a restriction on reading/running tests and
synthetic trials. Obtain explicit authorization for any affected implementation and bounded checks.
Do not treat old grants, task checkboxes, proposed commands, or a merged plan as execution permission.

No orders or broker calls; no real provider/model calls; no confirmation/qualification access; no
raw dataset extraction; no campaign launch/resume; no prepared `.artifacts` edits; no old grant reuse.
Keep the saved campaign and worktree untouched. Do not read test contents merely to estimate coverage.

Preserve V2–V5 canonical serializers and identities. Do not append defaulted fields to hashed legacy
records, change fixed-probe admission, alter the evaluator or `search.py`, introduce another model
role, or add retries/hidden work. Ordinary disabled operation must retain its exact requests, records,
outputs, decisions, fingerprint values, call counts, and deadline allocation.

Enabled qualified evidence is intended to inform later hypotheses; it need not produce the same
future candidate proposals. The invariants are unchanged scoring/evaluation/admission rules for the
same inputs, and byte-identical legacy-off behavior—not identical LLM outputs after changing context.

## Deliverables and ownership

All new filenames and type names below are proposals; reconcile collisions before creating them.

| Proposed file | Responsibility |
| --- | --- |
| `core/pit_optimizer_v5/mechanism_contracts.py` | Frozen spec, binding, report, and hypothesis-basis contracts; closed statuses and identities. |
| `core/pit_optimizer_v5/mechanism_probes.py` | Bounded recipe/case planning and paired observations through an injected worker port. |
| `core/pit_optimizer_v5/mechanism_reports.py` | Registered measurements, deterministic assessment, and derived conditional lesson views. |
| `core/pit_optimizer_v5/mechanism_artifacts.py` | Create-only sidecars, indexes, and local retrieval manifests using existing primitives. |
| `tests/test_pit_optimizer_v5_mechanism_evidence.py` | Proposed contract, probe, report, and lesson checks after test authorization. |
| `tests/test_pit_optimizer_v5_mechanism_integration.py` | Proposed mode, persistence, request, compaction, and two-round checks after authorization. |

Potential existing seams: `runtime.py`, `production_runtime.py`, `provider.py`, `memory.py`,
`artifacts.py`, `summary.py`, and `probes.py` under `core/pit_optimizer_v5/`. Inspect only necessary
seams; this is not permission to edit them all. Review adjacent schema/admission adapters if needed,
but add them to an explicit allowlist before implementation. No new dependencies or agent framework.

One integration owner controls shared adapters. Dependencies remain **0 → 1 → (2 and 3) → 4 → 5 → 6**.
Task 5 comprises **5A → 5B → 5C**. A report-only slice can be delivered after Task 4, but must not be
reported as completion of investigator learning integration.

## Task 0: Reconcile source, scope, and authorization

- [ ] Record actual commit/branch and worktree cleanliness without altering another agent's checkout.
- [ ] Read repository guidance and the paused handoff. Separate permissions for implementation,
  test-file inspection, synthetic checks, discovery input access, and campaign/provider execution.
  The latter two are unnecessary for the initial supplied-evidence slice.
- [ ] Reconcile the active source against the pinned review. Trace `ExperimentRecordV5` →
  `project_investigator_memory_v5` → `_investigator_parts` → `InvestigatorRoleInputV5` →
  `RoleRequestV5` → `select_novel_hypothesis_v5` → `author_request`.
- [ ] Record source drift, actual symbol locations, legacy hashes, semantic modes, provider schema,
  evidence vocabulary, request bounds, admission requirements, and sidecar storage primitives.
- [ ] Distinguish active source from archived `.reference` snapshots. Do not infer code changes from
  a truncated repository-wide comparison or infer test coverage from filenames.
- [ ] Select a separate implementation branch and a narrow file allowlist; check for concurrent work.

**Acceptance:** source/authorization map, agreed interfaces and owners, and a statement that tests,
campaigns, trading, and data access remain unavailable unless separately authorized. Any unresolved
prerequisite is recorded, not worked around. This review does not complete the implementer's Task 0.

## Task 1: Freeze experiment and hypothesis-basis contracts

- [ ] Define `MechanismExperimentSpecV1` separately from `HypothesisV5`: hypothesis and exact parent,
  target method, allowed symbols, protected behavior, recipe/version, provenance, and predictions.
- [ ] Define registered metric ID, units, direction, tolerance, aggregation, relevant-case predicate,
  denominator, minimum cases, and disconfirming observation. Reject non-finite or ambiguous values.
- [ ] Record the motivating issued evidence and, when applicable, a plausible competing explanation
  and the control that distinguishes it. A new hypothesis may originate from current diagnostics;
  it need not inherit a previously supported lesson.
- [ ] Define a separate versioned hypothesis-basis sidecar binding the selected hypothesis to its
  actual investigator request, current issued evidence, and any source lesson/report digests. Keep
  current observations, prior measured findings, and model interpretations separately labeled.
- [ ] Freeze predictions after hypothesis selection and before authoring. After rendering, bind exact
  candidate/parent bytes, spec, evaluator, corpus, recipe, scenario, order, and resource conditions
  before observations. Do not require a nonexistent candidate SHA at the pre-authoring step.
- [ ] Use closed declarative predicates/recipes. No arbitrary Python, `eval`, imports, generated
  assertions, or retrospective choice of an expected answer. A human-supplied typed spec is adequate
  for the first slice; do not add an extra model call or silently convert prose into ground truth.
- [ ] Keep execution validity, observability, and prediction support distinct. Use
  `supported_on_cases`, `contradicted_on_cases`, and `insufficient_evidence` without a confidence score.
- [ ] Preserve legacy record signatures and serializers; historical narrative cannot become a
  precommitted observation. Historical interpretations may remain hypothesis inputs with that label.

**Proposed checks:** canonical round-trip/digest changes; wrong parent/request/evidence binding;
invalid units, predicates, NaN, or tolerance; zero denominator; unexercised cases; after-the-fact
spec mutation; novel hypothesis without prior lessons; interpretation incorrectly promoted to fact.

**Acceptance:** import-safe contracts, identity graph, metric registry, and complete status table.
No CLI, transport, Docker, filesystem implementation, or untrusted source execution in pure contracts.

## Task 2: Implement bounded paired probes

- [ ] Reuse trusted snapshot validators and canonical probe encoding; no competing snapshot schema.
- [ ] Build frozen controller-owned recipes for permitted boundaries, legal missing values, and
  matched negative controls. Validate types, units, and compound snapshot invariants.
- [ ] Compare exact parent and candidate on identical snapshots through the existing bounded worker
  via an injected port. Never import or execute untrusted candidate code in the controller.
- [ ] Commit case ordering, repetitions, isolation/reset behavior, CPU/memory/output/time budgets,
  and input bytes. No Cartesian search, post-result favorable-case selection, or unbounded retries.
- [ ] Preserve typed exceptions, timeouts, numeric types, unsupported cases, and relevant-case counts.
  Input-condition coverage is not branch coverage; report the latter unavailable unless measured.
- [ ] Retain the fixed suite/version/fingerprint and terminal admission result. Supplemental witnesses
  provide evidence only, not a rescue path for rejected candidates.

**Proposed checks:** a synthetic fraction threshold pair 0.40/0.60 agrees at 0.20/0.80 but differs at
0.50; exact boundaries; coherent snapshots; controls; wrong parent; determinism; exception/timeout
as execution failure, not contradiction. These values are fixtures, not trading parameter changes.

**Acceptance:** bounded reproducible observations and truthful coverage, with no ranking/admission
side effects. A finite match is never described as universal policy equivalence.

## Task 3: Build measurements, reports, and conditional lesson views

- [ ] Reduce observations with registered formulas and publish enough counts, denominators, units,
  tolerances, and deltas to independently recompute every prediction assessment.
- [ ] Assess every preregistered prediction. Keep mixed, contradicted, missing, unsupported, failed,
  unexercised, and skipped outcomes rather than selecting whichever metric improved.
- [ ] Match exact parent/candidate episode, scenario, evaluator, and corpus before portfolio deltas.
  Label campaign-baseline comparisons separately. Same-input controls are not portfolio equivalence.
- [ ] Reuse available diagnostics/report fields. Document a prediction-to-observable coverage map;
  absent metrics are unavailable, not zero. Do not invent missing observations from top-count
  summaries or add evaluator instrumentation/data extraction in this slice.
- [ ] Derive a `MechanismLessonViewV1` from authenticated reports within the existing sidecar design.
  Include report/spec/source experiment identities, scoped conditions, intervention, protected
  behavior, outcomes, case counts, counterevidence, limitations, and prospective/retrospective status.
- [ ] Keep lesson interpretation separate from measured fields. Do not treat one local observation
  as a portable optimization rule. New parent, recipe, corpus, or evaluator requires an explicit
  applicability check; unknown applicability remains unknown. No cross-campaign reuse by default.
- [ ] Emit complete local and allowlisted role-safe representations. Do not add another database,
  generic vector memory, executable lesson, opaque score, or performance/promotion gate.

**Proposed checks:** recomputable deltas; tolerance/denominator boundaries; mixed predictions;
missing low-frequency metrics; mismatched scenarios; protected local decisions with downstream
portfolio divergence; contradictory lesson evidence; inappropriate transfer; stable report identity.

**Acceptance:** a reviewer reconstructs the observation and its scope without a critic or real replay.
Critic prose and higher CAGR cannot override a measured unsupported or contradicted mechanism.

## Task 4: Persist and integrate opt-in local evidence

- [ ] Review an authenticated extension capability bound to exact source/manifest/spec/corpus and
  bounded resources. No directory-presence or unbound environment/boolean enablement.
- [ ] Keep the first runnable slice limited to supplied, separately authorized evidence. No market
  extractor, campaign launcher, or historical replay CLI.
- [ ] Reuse safe-path/digest/atomic-write primitives for create-only sidecars and an experiment-bound
  index. Reject traversal, symlinks, foreign identities, and corrupt required evidence.
- [ ] Keep legacy-off operation a true no-op, including provider inputs and timing/call allocation.
- [ ] In an enabled reporting context with `disabled_development`, emit `not_run` with its reason and
  make zero supplemental worker calls. Do not create fabricated semantic observations.
- [ ] In separately authorized required mode, collect supplemental evidence within the existing
  bounded stage before its terminal decision without changing that decision. Charge all work to
  original deadlines; do not rerun rejected candidates outside the controller.
- [ ] Resume idempotently: reuse complete exact matches; incomplete output is not success; required
  corruption fails the extension closed. No repeated model calls or historical artifact rewrites.

**Proposed checks:** legacy-off byte equivalence; disabled mode zero calls; path/digest/identity
failures; interrupted writes; repeated resume; unchanged early rejection; deadline exhaustion;
bounded cleanup. Do not alter saved manifests or campaigns to make fixtures pass.

**Acceptance:** absent extension changes nothing, enabled evidence never misstates execution, and
all new state is versioned separately from legacy checkpoint/record serialization.

## Task 5A: Connect measured evidence to the existing critic

- [ ] Review evidence prefixes, sanitization, exact citation order, schema authority, request digest,
  full context/token bounds, and admission accounting together. Do not inject unknown keys into a
  frozen role message or relabel an unrelated metric to bypass validation.
- [ ] Issue role-local evidence IDs from authenticated report payloads; accept only IDs issued with
  that exact request. Stable lesson/report digests and role-local evidence IDs are different things.
- [ ] Preserve the existing critic shape where feasible, but supply measured status and limitations
  independently of its narrative. The critic cannot rewrite findings or qualification boundaries.
- [ ] Retain negative/inconclusive observations even when a candidate is not promoted. Invalid or
  otherwise untestable records must not acquire fabricated critic reviews.

**Acceptance:** an authorized critic request can interpret supplied observations without extra roles
or model calls. This is necessary but not sufficient for Task 5 completion.

## Task 5B: Reuse qualified findings in the next investigator request

- [ ] Extend the existing `_investigator_parts` and memory projection seams, not a parallel loop.
  Preserve loading/authentication of historical critic/investigator packages and reissued evidence.
- [ ] Define an explicitly versioned, opt-in projection of lesson outcomes, conditions, relevant
  controls, and counterevidence into the next request. Any required schema change must be reviewed
  with admission; do not mutate old role packages or their hashes.
- [ ] Record a local retrieval manifest with source identity, selection reason, complete/summary/
  omitted status, and byte accounting. It must reference only available authorized history and must
  not silently change old projection priority or legacy scheduling.
- [ ] Preserve mandatory selected-parent lineage and its budget failure behavior. Keep critical
  limitations attached to each selected lesson as an indivisible unit. An omitted contradictory
  result must not leave an apparently unconditional positive lesson. If the required qualified unit
  cannot fit, report unavailable in the enabled extension rather than claim complete evidence.
- [ ] Measure the final serialized request, not only intermediate memory bytes: include reissued
  evidence, campaign directions, schemas, and existing admission/token limits. Add no model call.
- [ ] Map the selected hypothesis's basis back to the actual current request and supplied findings.
  Record reuse/refinement/new-observation status without declaring cited evidence a proof of the
  new hypothesis. A bounded fixture can check the handoff, not infer the LLM's private reasoning.
- [ ] Leave rank ordering, novelty derivation, archive selection, qualification, and promotion rules
  unchanged. A semantic change to retrieval priority or admission needs a separate explicit proposal.

**Acceptance:** a reviewer can identify which measured findings and limits were actually available
to hypothesis N+1, and trace its cited basis to current request evidence. A digest-only local memory
entry must not be presented as full causal context seen by the model.

## Task 5C: Verify the two-round evidence round-trip

After separate authorization, use temporary local fixtures and injected/mocked ports only:

- [ ] Persist a declared Round N fixture with authenticated report, critic package, and experiment
  record; reconstruct it through the existing persistence/projection path.
- [ ] Build the actual Round N+1 investigator request. Inspect messages and issued payload identities,
  not just an intermediate dataclass. Confirm the positive, contrary, and insufficient findings
  arrive with their conditions and source bindings.
- [ ] Feed a predeclared fake investigator artifact citing those current IDs through existing novelty
  selection and author-request construction. Verify the same selected hypothesis and allowed basis
  survive the handoff, without fabricated historical IDs or bypassing controller checks.
- [ ] Change one relevant prior observation in an independently constructed fixture. Confirm the
  derived assessment and request identity change while protected unrelated inputs remain fixed.
  This proves data dependency, not autonomous hypothesis quality or improved financial performance.
- [ ] Repeat across restart and context pressure. Verify no duplicate sidecars, model calls, fresh
  deadlines, dropped caveats, or silent switch to another parent/source authority.
- [ ] Exercise every applicable row in the linked review's proposed verification matrix and record
  the exact source and result. Unexecuted rows remain unverified.

**Acceptance:** an auditable two-round local evidence chain plus negative controls, preserving legacy
compatibility and all access limits. Do not substitute a fake-provider result for a real-agent study.

## Task 6: Independent review and closeout

- [ ] Review the exact diff against the allowlist. Strategy, evaluator, ranking, fingerprint,
  qualification, historical state, broker behavior, or unauthorized grant changes are out of scope.
- [ ] Record actual permissions, commands, source commit, outputs, and limitations. Separate static
  review, deterministic execution, real model evaluation, and historical performance evidence.
- [ ] Verify focused cases only when authorized; no broad test inspection or suite expansion.
- [ ] Publish changed files, versions, source bindings, remaining work, and actual verification.
  No automatic campaign launch, paid call, deployment, or claim of improved returns at closeout.

### Proposed offline commands — not executed by this documentation change

Only after the files exist and the owner authorizes the specified test/synthetic checks. Fixtures
must use `tmp_path`, supplied inputs, and mocked/injected external ports. No `.env`, saved campaigns,
real providers, or heldout access. Also lint any existing adapter actually changed.

```powershell
python -B -m pytest -p no:cacheprovider --no-cov -q -m "not integration" tests/test_pit_optimizer_v5_mechanism_evidence.py tests/test_pit_optimizer_v5_mechanism_integration.py
python -m ruff check core/pit_optimizer_v5/mechanism_contracts.py core/pit_optimizer_v5/mechanism_probes.py core/pit_optimizer_v5/mechanism_reports.py core/pit_optimizer_v5/mechanism_artifacts.py tests/test_pit_optimizer_v5_mechanism_evidence.py tests/test_pit_optimizer_v5_mechanism_integration.py
git diff --check
```

The documentation publication uses a CI-skip annotation to preserve the standing no-test boundary,
not to claim successful CI or bypass required checks. Do not carry this shortcut into an
implementation PR or treat pending checks as passing. Repository-required implementation validation
must be reconciled with explicit owner authorization.

## Definition of done and separate future work

The full extension is complete only when reports are independently reproducible, scoped findings
survive the actual investigator/author handoff and restart, final request bounds are respected,
legacy-off behavior remains unchanged, and independent review confirms the preserved boundaries.
Report-only completion is a smaller milestone. Every unperformed check stays explicitly unverified.

A later, separately approved equal-budget agent study could compare the current loop with qualified
evidence reuse using fixed model/settings/task cases and independent review of hypothesis quality,
unsupported claims, repeated contradicted ideas, useful experiments, and resource cost. That study
is not authorized here and is not required to establish wiring correctness. Infrastructure speed
work needs separate behavior-equivalence benchmarks; strategy research must not modify its own
trusted evaluator, tests, or qualification boundary. Neither is authorized by this plan.
