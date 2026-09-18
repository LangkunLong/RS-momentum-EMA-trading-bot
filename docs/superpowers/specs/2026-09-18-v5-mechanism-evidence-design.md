# V5 Mechanism Evidence: Design

**Date:** 2026-09-18  
**Revision:** 2 — complete evidence-to-next-hypothesis handoff  
**Status:** Proposed implementation design; documentation only, not implemented or runtime-verified.  
**Reviewed main:** `15ba962743da2c2ca73becdf85bc639c4f670dfa`.

This revision incorporates the [source review and GLM comparison](../reviews/2026-09-18-v5-glm-loop-review.md). It replaces the initial design text from PR #60 without changing that plan's strategy, evaluator, historical-artifact, or execution boundaries.

## Purpose and authorization

Make a candidate's claimed mechanism independently inspectable, then deliver the resulting measured finding to the next investigator. State predictions before evaluation, compare exact parent and candidate on identical causal inputs, distinguish local behavior from portfolio consequences, and preserve limitations through memory and request construction.

V5 already has hypothesis memory and a implemented critic-to-investigator feedback path. Do not build another generic memory, optimizer, or model role. The additional work is measurement quality and continuity, not adding a loop that already exists.

The current request authorizes source review and publication of revised plans. It does not authorize implementation, reading/running project tests, synthetic trials, market-dataset access, model calls, campaign resumption, or orders. The [September 9 checkpoint](../handoffs/2026-09-09-pit-optimizer-v5/README.md) remains a preserved historical execution boundary. Later implementation must resolve the relevant restrictions explicitly; a task checkbox is not an execution grant.

Read the [implementation plan](../plans/2026-09-18-v5-mechanism-evidence.md) and [Codex handoff](../handoffs/2026-09-18-v5-mechanism-evidence/README.md).

## Existing architecture to reuse

The review links exact source locations. These are source findings, not runtime verification:

- `ExperimentRecordV5` stores hypotheses, template identities, observations, statuses, and bound critic reviews.
- `project_investigator_memory_v5` provides complete selected-parent lineage, relevant feedback, and compact summaries under a budget.
- `LocalRoleRequestFactoryV5._investigator_parts` authenticates earlier role artifacts, reissues their cited evidence, and supplies experiment and campaign directions to `InvestigatorRoleInputV5`.
- `select_novel_hypothesis_v5` selects the first model-ranked controller-novel hypothesis. The runtime journals `RoundIntentPayloadV5` before requesting author code.
- The author creates a bounded template; local variants pass existing validation, semantic, quick, and discovery stages. A batch critic reviews testable outcomes before records and checkpoints are published.

The model proposes and ranks hypotheses. Engineers and trusted code define the observable quantities, safety/correctness invariants, execution limits, and evidence interpretation rules. A correctly bound citation is not proof that the model's explanation is true.

## Non-negotiable boundaries

- No strategy policies, canonical entry floors, settings, stops, sizing, orders, fills, cost assumptions, evaluator arithmetic, discovery requirements, or CAGR-ranking changes.
- No changes to `agent_loop.py`, no new general agent framework, no new model role, retries, repair calls, or automatic campaign launches. Keep `apply=false`.
- No confirmation/qualification access. Keep raw discovery rows, symbols, exact historical sessions, credentials, paths, and unapproved source content out of investigator/critic evidence. Do not alter the existing authorized author-source boundary.
- Preserve V2–V5 readers and existing canonical bytes, hashes, fingerprints, manifests, checkpoints, and resume behavior. Do not append optional fields to hashed legacy records as an informal migration.
- Do not modify prepared campaign artifacts, old grants, baseline authorities, or runtime state. A new source revision does not inherit an old source-bound execution grant.
- Execute untrusted candidate code only through an explicitly composed bounded policy worker/sandbox. AST validation is not permission to import it into the controller.
- Preserve fixed-suite admission and `disabled_development`; explanatory evidence does not create a new promotion or rejection criterion.

## 1. Bind a testable mechanism at the existing hypothesis boundary

Add an independent versioned `MechanismExperimentSpecV1`, not a replacement for `HypothesisV5`. Bind it alongside the existing selected-hypothesis round intent, before authoring and observation.

The spec references the existing hypothesis and exact parent revision. It declares the target method and permitted changed symbols, registered observables and units, direction and tolerance, aggregation, relevant-case predicate, denominator, minimum relevant cases, protected controls, and disconfirming observations. Include a controller-owned recipe ID/version and explicit synthetic/discovery-only provenance.

Predicates and recipes are closed declarative values validated against registered fields. Reject arbitrary Python, `eval`, executable assertions, unsupported metrics, ambiguous units, non-finite values, and invalid combinations. Do not convert vague model prose into authoritative expected results silently.

Use two identities: freeze the specification before authoring; after rendering, bind the exact candidate, parent, spec, evaluator, scenario, case corpus, recipe, and execution limits before measurement. Do not require a candidate hash before the candidate exists.

An operator-supplied typed specification is sufficient for the first offline slice. The existing investigator's structured predictions may populate registered fields where meaning is unambiguous. Fully model-authored operational specifications need an explicit closed-schema design, not another paid role or a human required to invent every hypothesis.

Retrospective explanations may be retained as retrospective; they must never be relabeled precommitted evidence.

## 2. Separate local mechanism tests from portfolio consequences

Use the exact authored parent as control; label campaign-baseline comparisons separately. Both policies receive byte-identical validated snapshots under the same bounded worker and declared reset/repetition behavior.

Measure independently:

1. Whether the relevant condition occurred and the intended decision changed.
2. Whether protected decisions remained unchanged on identical inputs.
3. What changed after those decisions interacted with the portfolio.

The third requires a separately matched existing replay comparison, not a same-input policy comparison. Changed exits may change later cash and entries without any edit to entry logic. Do not demand identical full portfolio traces after an intentional strategy intervention.

Retain full local observations and canonical identities. Input-condition coverage is not branch coverage: report branch coverage unavailable unless trusted instrumentation measures it. A diff, predicate match, or changed final action alone does not establish that a particular branch ran.

## 3. Use bounded change-aware probes

Retain the trusted fixed suite and its version unchanged. Supplemental recipes may cover allowed numeric boundaries, legal missing values, and matched negative controls. Derive cases deterministically from the frozen recipe and allowed source changes before execution; do not search retrospectively for favorable examples.

For a synthetic fraction-valued fixture, thresholds 0.40 and 0.60 agree at 0.20 and 0.80 but differ at 0.50. This illustrates finite-suite incompleteness, not a proposed trading threshold.

Validate types, units, coherent snapshot constraints, finite values, and bounds. A perturbation that cannot yield a valid snapshot is unsupported, not a fabricated market state. Bound cases, repetitions, CPU, memory, output, and time. No Cartesian sweep, symbolic executor, or unrestricted candidate `exec`/`eval` is introduced.

Any later discovery snapshots must follow a precommitted selection rule over authorized discovery inputs, not winners or future outcomes. Market-data extraction is not part of this first slice.

Finite agreement means no difference observed on those cases, not universal equivalence. Nevertheless, this slice preserves existing early rejection and fingerprint deduplication. Supplemental witnesses cannot rescue a rejected candidate. A rescue policy is a separate versioned search-semantics decision.

## 4. Compute evidence rather than trusting critic prose

`MechanismEvidenceReportV1` is a proposed independent record with these sections:

| Section | Required content |
| --- | --- |
| Identity | Spec/binding, hypothesis, exact parent/candidate, corpus, evaluator, scenario, and recipe identities. |
| Execution | `completed`, `not_run`, or `failed`, typed reason, resource limits and usage. |
| Coverage | Total/relevant cases, changed decisions, protected controls, unsupported cases, and branch-coverage availability. |
| Predictions | Measurements, units, numerator/denominator, tolerance, paired delta, and assessment for every declared observable. |
| Consequences | Matched existing quick/discovery report references, with episode and scenario context. |
| Limitations | Missing evidence, unsupported applicability, local scope, and prospective versus retrospective status. |

Prediction assessments are `supported_on_cases`, `contradicted_on_cases`, and `insufficient_evidence`. Retain every individual result when a report is mixed. Zero relevant cases, missing denominators, or too few observations are insufficient evidence. Policy crashes, timeouts, protocol failures, and corrupt artifacts are execution/validity failures, not scientific refutations.

Reuse available `EvaluationReportV5` and diagnostic measurements. A requested metric missing from the evaluator is unavailable; do not derive it from unrelated aggregates or extend evaluator arithmetic in this slice.

For enabled evidence projection, prioritize the declared prediction, comparator, controls, and limitations over generic context. A named low-frequency exit or rejection reason must not disappear merely because it is outside a top-four display summary. Retrieve it by registered identity when available, or state why it is unavailable.

These assessments apply only to the evaluated cases. Higher CAGR cannot turn an unsupported mechanism into a confirmed finding. Do not add a confidence score, weighted reward, risk limit, or selection gate.

## 5. Persist additively and close the next-hypothesis handoff

Use create-only, versioned, content-addressed sidecars and a canonical index linked to the existing experiment identity. Reuse safe-path, digest, and atomic-write primitives. Reject traversal, symlinks, mismatched parents, corrupt bytes, unknown references, and heldout provenance.

Existing record/checkpoint serializers remain byte-identical. Disabled legacy operation creates no new files or messages and changes no decisions. An absent optional legacy sidecar means unavailable; missing or corrupt evidence required by an enabled extension fails that extension closed. Completed matching sidecars are reused on restart; partial writes are not successful evidence and never trigger provider retries.

### Conditional findings, not a second memory system

A proposed `MechanismLearningProjectionV1` is a bounded view of authenticated records and reports, implemented with the report/projection code. It is not another database or an LLM-authored knowledge base. Preserve:

- the original hypothesis, exact parent/experiment/report identities;
- applicability conditions actually tested, comparisons and protected controls;
- observed measurements and individual assessment/coverage/validity states;
- negative, mixed, unsupported, and not-run findings;
- a separately labeled critic interpretation and next direction, when one exists.

An author template is an unevaluated candidate structure until evidence exists. Do not call it a validated optimization skeleton merely because it is reusable code. A conditional finding does not become a universal strategy rule or evidence for a different data/evaluator context.

### Two consumers: critic and next investigator

Issue only registered sanitized evidence through the existing provider boundary. Review metric vocabulary, sanitization, schema authority, exact request digest, evidence count, token/cost bounds, and admission together. Prefer the existing role-input shapes when sufficient; any necessary schema change must be explicitly versioned and disabled for legacy operation.

The critic receives deterministic mechanism measurements plus limitations. The next investigator must receive the relevant measured finding independently of which measurements the critic happened to cite. Do not depend on narrative summaries to preserve a contradiction or a missing denominator.

Reissued evidence must preserve unambiguous experiment, parent, episode ordinal, scenario, units, and report linkage. Multiple observations can share a metric name. Do not infer their grouping from an evidence ordinal, hash, or prose explanation. Keep raw rows and local paths out of the role view; retain authenticated provenance locally and expose only permitted identifiers/aggregates.

Keep full records locally. For the extension's bounded projection, record complete/summarized/omitted evidence and reasons. A retained claim cannot lose its scope or negative qualifier. If mandatory relevant evidence cannot fit, return an explicit bounded-projection failure rather than truncating it into a misleading statement.

Do not silently change legacy oldest-first projection ordering or parent-lineage retention. The latest campaign direction already has a separate retention path; preserve it. Measure the final serialized request and transport/schema overhead after reissuance, not just the internal memory projection. Reuse existing admission controls; no budget increase or hidden extra call is authorized.

Supplying relevant context does not prove that an LLM uses it. This slice proves delivery and grounded evidence, not improved hypothesis quality.

## 6. Keep execution mode and timing explicit

Enable only through a new authenticated extension capability bound to exact source, manifest, spec, corpus, and resource limits. Do not infer permission from a directory, environment variable, or nonempty report.

With `semantic_mode="disabled_development"`, no supplemental worker is called and no fingerprint is invented. In an enabled report context emit `not_run` with the mode reason; legacy-off remains a true no-op. Re-enabling development semantic checks is a separate decision.

Before required-mode execution, identify the concrete registered bounded executor and its authorization. Do not assume a base adapter's fingerprint method is a runnable implementation or substitute controller-process execution.

Where separately authorized, collect supplemental observations in the existing bounded validation/semantic stage without changing its terminal decision or ordering. All work consumes the original stage/round deadline. Do not rerun a previously rejected candidate, reset deadlines, or add retries.

## Completion and deferred work

The first slice completes when a reviewer can reconstruct the mechanism assessment and inspect the same scoped finding in the next investigator's actual request, including after restart and memory pressure. Legacy-off equivalence, provider/heldout boundaries, truthful failures, and unchanged selection must be demonstrated by the authorized checks.

A separate controlled model study would be required to claim better hypothesis generation; independent qualification would be required for generalization claims. Neither is implied by wiring tests.

Deferred: new strategies, strategy tuning, new novelty/ranking rules, interactive pre-authoring diagnostic requests, fixed-probe rescue, automatic test generation, cross-campaign knowledge retrieval, qualification-consumption ledgers, statistical selection corrections, full-market replay, paid model comparisons, evaluator-performance changes, and deployment. These are separate workstreams, not hidden requirements or execution permissions.
