# V5 Mechanism Evidence: Design

**Date:** 2026-09-18  
**Status:** Proposed implementation design; documentation only, not implemented or runtime-verified.  
**Inspected base:** `9aa52976f898c27f78c1857ceecde6a7b22a5aab` on `main`.

## Purpose and authorization

Make a candidate's claimed mechanism independently inspectable: state the prediction before the
change is evaluated, compare parent and candidate on identical causal inputs, and distinguish
mechanism observations from portfolio consequences. Improve experimental information, not the
strategy or its score in this workstream.

The current request authorizes publishing plans for Codex. It does not authorize implementation,
reading/running tests, synthetic trials, model calls, dataset access, campaign resumption, or orders.
The [September 9 handoff](../handoffs/2026-09-09-pit-optimizer-v5/README.md) and its execution/test
restrictions remain in effect. A later implementation request must resolve those restrictions
explicitly before the affected work begins. Do not interpret a task checkbox as execution permission.

See the [implementation plan](../plans/2026-09-18-v5-mechanism-evidence.md) and
[Codex handoff](../handoffs/2026-09-18-v5-mechanism-evidence/README.md).

## Verified starting points and limits

Paths below were inspected directly or through indexed source excerpts at the inspected base.
They are integration starting points, not a claim of complete source review or passing tests.

| Source | Relevant existing responsibility |
| --- | --- |
| `core/pit_optimizer_v5/contracts.py` | `HypothesisV5`, directional `MetricPredictionV5`, and `CriticReviewV5` prediction-versus-observation fields. |
| `core/pit_optimizer_v5/probes.py` | Versioned fixed snapshots, six policy methods, canonical probe identities, fingerprints, and typed failures. |
| `core/pit_optimizer_v5/runtime.py` | Investigator/author/critic stages, semantic/quick/discovery stages, candidate outcomes and cleanup. |
| `core/pit_optimizer_v5/diagnostics.py` | Signal and entry outcomes, fill-cost reconciliation, portfolio observations and report inputs. |
| `core/pit_optimizer_v5/production_runtime.py` | Parent-delta and role-evidence projection. |
| `core/pit_optimizer_v5/provider.py` | Closed provider contracts and evidence vocabulary. |
| `core/pit_optimizer_v5/search.py` | Authenticated four-episode evidence and base-cost campaign CAGR. |
| `core/pit_optimizer_v5/development_preparation.py` | Development preparation explicitly selects `semantic_mode="disabled_development"`. |

Re-read exact adapter seams, `memory.py`, and `artifacts.py` before implementation. Existing test
filenames and coverage have not been established by this planning change. Proposed new test files
in the plan are not assertions that those files already exist.

The saved development campaign is paused and has semantic checks disabled. Consequently, fixed
probe limitations are a design risk, not an established explanation for that campaign's results.
The older [V5 architecture](2026-09-04-pit-optimizer-v5-hybrid-architecture-design.md) remains the
baseline design. This extension does not supersede its search or execution rules.

## Non-negotiable boundaries

- Do not change strategy policies, canonical entry floors, settings, stops, sizing, orders, fills,
  cost assumptions, evaluator arithmetic, four-episode requirements, or CAGR ranking.
- Do not alter `agent_loop.py` or add another general-purpose agent framework.
- Do not read confirmation/qualification data or expose it to search, probes, or role memory.
  Keep raw discovery rows, symbols, exact historical sessions, source text, credentials, and local
  paths out of provider-facing evidence as required by the current sanitization boundary.
- Preserve V2-V5 readers and existing canonical bytes, hashes, fingerprints, manifests, checkpoints,
  and resume behavior. Never append optional fields to hashed legacy records as an informal migration.
- Do not edit prepared campaign artifacts, old grants, baseline authorities, or runtime state.
  A new source revision is not authorization to reuse a source-bound old grant.
- Keep untrusted candidate execution inside existing bounded policy-worker/sandbox facilities.
  AST parsing is not permission to execute a candidate in the controller process.
- Keep `apply=false`, all trading entry points untouched, and no new retries, schema-repair calls,
  model calls, or automatic campaign launches.

## Architecture: an evidence extension, not a new optimizer

Use small, pure modules alongside V5 and narrow adapters into the existing pipeline. The first
vertical slice is a deterministic report built from supplied, authenticated evidence; runtime
integration follows only after its contract is reviewed. Runtime enablement must be opt-in for a
new authorized diagnostic context, never inferred from a nonempty directory or environment flag.

### 1. Commit the question before observing candidate results

Propose an independent versioned `MechanismExperimentSpecV1`, not a replacement for `HypothesisV5`.
It references the existing hypothesis and contains:

- parent policy revision and hypothesis IDs;
- target policy method and permitted changed symbols;
- registered observables, units, direction, comparison tolerance, aggregation, denominator, and
  minimum relevant-case count;
- a controller-owned case-selection predicate and probe recipe ID/version;
- protected same-input behaviors and disconfirming observations;
- discovery/synthetic provenance restrictions and an explicit no-heldout scope.

Predicates and recipes are closed declarative values validated against registered field paths;
never accept model-authored Python, `eval`, arbitrary imports, or executable assertions. Ambiguous
units, unsupported metrics, non-finite values, and invalid combinations fail spec validation.
Do not infer an expected outcome from a candidate's observed behavior or a critic's later prose.

Freeze and hash the spec after selecting the hypothesis and before authoring/evaluation. Candidate
source does not exist yet: after rendering, create a separate binding to the exact candidate,
parent, spec, evaluator, input corpus, suite/recipe, and scenario identities before measurement.
This two-step binding avoids requiring an impossible pre-authoring candidate SHA.

A human/operator-supplied typed spec is sufficient for the first slice. Reuse existing structured
predictions where their meaning is unambiguous. Do not add a paid role or silently translate free
text into authoritative expected results. Historical explanations without a precommitted spec may
be labeled retrospective, but cannot be upgraded to preregistered evidence.

### 2. Compare like with like

Use the exact parent revision that the author changed as the control. A campaign baseline may be
reported separately, but must not be mislabeled as that parent.

Both policies receive byte-identical validated snapshots under the same evaluator and worker
conditions. Preserve canonical inputs and full local decision records; role summaries carry only
approved aggregates and issued evidence IDs. Repeatability must use the existing deterministic
worker contract, including the declared reset/isolation behavior.

Separate three questions:

1. Did the relevant input condition occur, and did the intended decision change?
2. Did protected decisions remain unchanged on identical inputs?
3. What happened after the changed decisions interacted with the portfolio?

The third is a separately matched replay comparison, not a same-input unit comparison. Different
exits can change subsequent cash and entries even when entry code is untouched. Do not require
whole-portfolio traces to remain identical after an intentional strategy intervention.

Input-condition coverage is not branch coverage. Unless trusted worker instrumentation establishes
that a specific branch executed, report branch coverage as unavailable. Never infer it solely from
a source diff, a predicate match, or a changed final decision.

### 3. Add bounded change-aware probes without changing admission rules

Retain the trusted fixed suite and its version unchanged. Supplemental case recipes can cover
validated numeric boundaries, an explicit missing-value case when legal, and matched negative
controls. Derive cases deterministically from allowed source changes and the frozen recipe before
execution; do not search for favorable cases after seeing results.

Example for a synthetic fraction-valued test policy only: thresholds 0.40 and 0.60 produce the same
decisions at 0.20 and 0.80, but differ at 0.50. This is a witness to fixed-suite incompleteness, not a
proposed edit to the bot's canonical entry thresholds.

Validate field types, units, coherent snapshot constraints, finite values, and bounds. If a generic
perturbation cannot create a valid snapshot, record it as unsupported rather than manufacture a
market state. Use controller-owned adapters for compound fields. Bound case count, repetitions,
CPU, memory, output, and time; no Cartesian sweep or new symbolic executor.

Discovery snapshots must be selected by a committed rule from authorized discovery inputs, not
winning trades or future outcomes. No discovery-data extraction is part of the first slice.

A finite suite supports only "no difference observed on these cases." It does not prove universal
policy equivalence. Nevertheless, this workstream does not weaken current early rejection,
fingerprint deduplication, or archive eligibility. Supplemental evidence may reveal a missed
witness while the existing outcome remains unchanged. A rescue path would require a separate
search-semantics proposal with versioned identities and equal-budget evaluation.

### 4. Keep validity, observability, and mechanism support distinct

Propose a `MechanismEvidenceReportV1` with separate sections:

| Section | Contents |
| --- | --- |
| Identity/provenance | Spec and candidate binding, parent, corpus, evaluator, scenario, and recipe digests. |
| Execution | `completed`, `not_run`, or `failed`, plus typed reason and bounded resource usage. |
| Coverage | Total cases, relevant cases, changed decisions, protected controls, unsupported cases, and branch-coverage availability. |
| Predictions | Parent and candidate measurements, paired delta, units, denominator, tolerance, and status for each preregistered observable. |
| Consequences | References to matched existing quick/discovery reports; per-episode/scenario deltas only when available. |
| Limitations | Missing measurements, unsupported cases, local scope, and whether evidence is prospective or retrospective. |

Prediction statuses are `supported_on_cases`, `contradicted_on_cases`, or `insufficient_evidence`.
A whole report may summarize mixed outcomes but must retain every individual outcome and its
reason. Zero relevant cases, missing denominators, or too few cases are insufficient evidence,
not support. Policy crashes, timeouts, protocol violations and corrupt artifacts are execution or
validity failures, not statistical evidence against the hypothesis.

These are observations within evaluated cases, not proof of market causality or future returns.
A higher portfolio score with unsupported mechanism evidence remains an unexplained score change.
Do not add a confidence score, weighted reward, new risk limit, or promotion gate.

### 5. Persist additively and project conservatively

Use versioned, content-addressed sidecar records under a new campaign-owned subdirectory, with a
create-only index bound to the exact existing experiment identity. Paths are canonical relative
paths; reject traversal, symlinks, digest mismatches, mismatched parents, and unknown references.
Reuse existing artifact primitives where possible, rather than duplicate a storage framework.

The sidecar has its own immutable execution/capability binding and resume cursor. Existing V5
record/checkpoint serializers remain byte-identical. Disabled legacy runs must not create sidecars,
request new fields, or change decisions. A missing optional legacy sidecar means unavailable; a
missing or corrupt sidecar required by an enabled extension fails that extension closed.

A resumed diagnostic reuses a completed matching sidecar without rerunning candidate code.
Interrupted writes cannot be advertised as completed evidence. Reconciliation is local and
idempotent; it never triggers another provider call or rewrites old campaign state.

Project only registered, sanitized aggregates through the existing evidence-ID mechanism. Do not
invent unregistered metric prefixes, repurpose unrelated existing metrics, or smuggle extra keys
through a closed provider schema. Review the prefix registry, schema, request digest, context size,
and campaign admission/token bounds together before wiring any new role input.

The existing critic's prediction-versus-observation and evidence-ID fields remain sufficient.
Critic prose cannot change measured statuses. Persist failed and inconclusive experiments locally,
but retain only bounded, approved aggregates in provider context. Qualification evidence remains
absent from both.

### 6. Make enablement and semantic mode explicit

Propose a separate authenticated extension capability referencing the exact manifest/source/spec
and bounded resources for a new diagnostic context. Review its composition before implementation;
an unbound optional flag is not an acceptable shortcut. Existing manifests and prepared authorities
must not be edited in place.

If `semantic_mode="disabled_development"`, do not call the supplemental policy runner, fabricate a
fingerprint, or silently turn semantics back on. Report `not_run` with the explicit mode reason in
an enabled report context; ordinary disabled legacy runs remain unchanged. Re-enabling semantic
checks for development is a separate decision outside this plan.

Where required-mode integration is later authorized, supplemental observations may be collected in
the existing bounded validation/semantic stage before its terminal decision, without changing that
decision or its ordering. Do not rerun a previously rejected candidate behind the controller's back.
Run costs count against the original deadline; timeouts must not purchase new time or retries.

## Initial acceptance and deferred work

The first slice succeeds when a reviewer can reconstruct whether a declared mechanism was exercised
and supported using deterministic local evidence, without trusting critic prose. It must also show
legacy-off equivalence, no heldout/provider leakage, truthful unsupported/failed outcomes, and
unchanged ranking. No strategy improvement or campaign result is required or claimed.

Deferred: new strategy hypotheses, strategy tuning, changed semantic admission, qualification
consumption ledgers, statistical selection corrections, new test frameworks, full-market replay,
agent-model comparisons, infrastructure performance work, and live/paper deployment. These are
separate workstreams, not implied implementation tasks.
