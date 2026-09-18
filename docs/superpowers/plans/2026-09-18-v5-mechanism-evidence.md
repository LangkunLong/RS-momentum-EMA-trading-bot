# V5 Mechanism Evidence: Codex Implementation Plan

**Date:** 2026-09-18  
**Revision:** 2 — verify the complete evidence-to-next-hypothesis cycle  
**Status:** Planning only. Every implementation task remains pending.  
**Reviewed main:** `15ba962743da2c2ca73becdf85bc639c4f670dfa`.

**Goal:** Add precommitted, independently checkable mechanism evidence and deliver its scoped findings to the next investigator without changing strategy logic, evaluator semantics, search selection, or legacy artifacts.

Read the [source review](../reviews/2026-09-18-v5-glm-loop-review.md), [design](../specs/2026-09-18-v5-mechanism-evidence-design.md), [Codex handoff](../handoffs/2026-09-18-v5-mechanism-evidence/README.md), and [preserved checkpoint](../handoffs/2026-09-09-pit-optimizer-v5/README.md).

## What this revision changes

V5 already persists evidence, retains critic learning, and uses it in later investigator requests. Reuse `ExperimentRecordV5`, `project_investigator_memory_v5`, `_investigator_parts`, and the existing pre-authoring `RoundIntentPayloadV5`. Do not create a separate hypothesis-memory system.

The main change from PR #60 is Task 5: completion now requires the measured lesson to reach the next investigator's exact bounded request, including after compaction and restart. A critic report alone is not sufficient. Task 3 also requires coverage of named low-frequency measurements rather than relying on generic top-count summaries.

## Authorization and scope

This revision authorizes no execution. The owner requested source review and documentation updates. No project tests, test-file inspection, synthetic experiments, backtests, raw datasets, paid calls, or campaigns were used to prepare it.

The September 9 checkpoint records restrictions on tests/synthetic trials and automatic campaign resumption. Obtain the necessary bounded implementation and verification authorization before those activities. Do not reuse an old grant for new source or a changed execution context. Approval of a documentation PR is not a campaign grant.

Preserve all design boundaries: no broker actions, qualification/confirmation access, prepared `.artifacts` mutation, strategy/evaluator/ranking changes, hidden retries, or new model roles. Preserve legacy canonical bytes and semantic modes. New explanatory evidence must not alter archive eligibility or candidate admission.

## Files and ownership

These are proposed filenames, not assertions of existing APIs or coverage. Reconcile current source before creating them.

| Proposed file under `core/pit_optimizer_v5/` | Responsibility |
| --- | --- |
| `mechanism_contracts.py` | Frozen spec, candidate binding, report and learning-projection contracts; closed validation and identities. |
| `mechanism_probes.py` | Bounded case recipes and paired reduction; candidate execution only through an injected bounded worker port. |
| `mechanism_reports.py` | Deterministic assessments, hypothesis-specific measurement selection, conditional findings, local and role-safe projections. |
| `mechanism_artifacts.py` | Thin create-only sidecar/index adapter using existing artifact primitives. |

Proposed focused tests, only after authorization:

- `tests/test_pit_optimizer_v5_mechanism_evidence.py`: contracts, probes, measurement and report reduction.
- `tests/test_pit_optimizer_v5_mechanism_integration.py`: persistence, modes, reissuance, next-request continuity, budgets and restart.

Potential integration seams are `runtime.py`, `production_runtime.py`, `provider.py`, `memory.py`, `artifacts.py`, `probes.py`, and `summary.py`. Modify only the necessary seams; this is not a blanket file allowlist. Do not change `agent_loop.py`, strategy-policy sources, settings, evaluator arithmetic, or `search.py`. No new dependencies or memory service.

Dependencies: **0 -> 1 -> (2 and 3) -> 4 -> 5 -> 6**. One integration owner controls existing shared adapters. Parallel probe/report agents may work on separate new files only after contract review. A separate reviewer inspects the final diff.

## Task 0: Reconcile source, existing feedback, and permissions

- [ ] Read guidance and the saved checkpoint; record branch, commit, and working-tree state without changing another agent's work.
- [ ] Trace the current record -> memory projection -> authenticated role reissuance -> investigator request path against the review. Distinguish active source from `docs/superpowers/local-history` snapshots.
- [ ] Locate the pre-authoring round-intent hook, exact parent comparisons, declared semantic mode, concrete worker adapter, provider schemas, and admission bounds.
- [ ] Record authorization separately for implementation, test inspection, synthetic verification, supplied evidence, market datasets, and provider/campaign execution. The latter two are unnecessary for the first slice.
- [ ] Select a fresh branch and a scoped file allowlist; check for other agents' implementation changes or filename collisions.

**Acceptance:** an exact source/permission map and explicit non-goals. Existing memory is a dependency to reuse, not missing infrastructure to reimplement. Any missing prerequisite remains a reported blocker, not a reason to fabricate evidence.

## Task 1: Operationalize the existing hypothesis before authoring

- [ ] Define separate `MechanismExperimentSpecV1` and observation bindings without changing `HypothesisV5` or old serializers.
- [ ] Bind hypothesis, exact parent, target method, allowed symbols, applicability predicate, controls, registered metrics/units, directions, tolerances, denominators, aggregation, minimum cases, and disconfirming observations.
- [ ] Freeze the spec alongside the existing round-intent boundary before authoring; bind candidate bytes, corpus, evaluator/scenario, recipe, and budgets before measurements.
- [ ] Validate closed predicates and recipes. Reject arbitrary executable conditions, unknown metrics, invalid units, non-finite numerics, unknown fields, and heldout provenance.
- [ ] Separate execution status, coverage, and prediction assessment. Preserve `supported_on_cases`, `contradicted_on_cases`, and `insufficient_evidence` without confidence scores.
- [ ] Define a bounded conditional-finding projection over existing records plus the report. Do not add another memory database or treat critic text as an authoritative observation.

**Focused checks:** canonical round-trip and digest changes; candidate/parent mismatch; spec recorded too late; unsupported metric/units; zero denominator; too few relevant cases; retrospective evidence cannot become precommitted evidence.

**Acceptance:** pure contracts and explicit status/units tables, with no transport, CLI, filesystem implementation, or candidate-code imports.

## Task 2: Implement bounded paired observations

- [ ] Reuse canonical probe encoding and trusted snapshot validators; generate only controller-owned recipes with valid compound invariants.
- [ ] Cover allowed changed-value boundaries, legal missingness, and negative controls; bind case order, repetitions, reset semantics, and resource limits.
- [ ] Run exact parent and candidate on identical inputs only through the registered bounded worker port. No unrestricted `exec`, `eval`, import, or controller fallback.
- [ ] Record relevant-case counts and action differences; report branch coverage unavailable unless independently instrumented.
- [ ] Preserve fixed-suite identity, fingerprint computation, early rejection, and candidate selection. Supplemental evidence cannot rescue a rejected candidate.

**Focused checks:** synthetic thresholds 0.40/0.60 that match at 0.20/0.80 but differ at 0.50; exact boundaries; invalid compound snapshots; changed decisions and protected controls; stable ordering; wrong-parent binding; nondeterminism; exceptions/timeouts are execution failures rather than contradicted hypotheses.

**Acceptance:** reproducible bounded witness observations. These values are fixtures, not trading settings. No generic market-data extractor or new execution command is introduced.

## Task 3: Assess every declared prediction and preserve context

- [ ] Reduce observations with registered formulas. Record source identities, units, counts, denominator, tolerance, and paired deltas for independent recomputation.
- [ ] Preserve every prediction, including mixed, unsupported, absent and unexercised results. Never choose only whichever metric improved.
- [ ] Separate identical-input behavior from downstream portfolio consequences; bind matched parent/candidate episode, scenario, evaluator, and report identities before deriving deltas.
- [ ] Reuse actual available diagnostic measurements. A missing evaluator metric is explicitly unavailable, not an estimate from unrelated totals.
- [ ] Resolve a hypothesis's named measurement by registered identity even when it is outside a generic top-four summary. Use bounded priority for prediction/comparator/controls/limitations, then optional context.
- [ ] Build the conditional finding from measured facts and tested applicability. Label critic explanation and next direction as interpretation. Keep authored parent separate from campaign baseline.

**Focused checks:** known deltas and tolerance edges; zero denominator; mixed outcomes; wrong scenario/evaluator/parent; low-frequency fifth-ranked reason; absent metric; two equal metric names in different episodes; valid local controls despite downstream portfolio divergence.

**Acceptance:** the report can be reconstructed without a critic or real replay. It contains no hidden weighted score, new rejection gate, or fabricated causality claim.

## Task 4: Persist sidecars and integrate explicit local execution

- [ ] Review an authenticated extension capability bound to source, manifest, spec, corpus, and limits. No unbound flag or inferred enablement.
- [ ] Use create-only sidecars and a canonical index tied to the existing experiment. Reuse safe path, digest, atomic-write and recovery primitives; keep old records/checkpoints unchanged.
- [ ] Keep the first integration limited to supplied authorized evidence. Do not build a new campaign launcher, broad extractor, or old-campaign migration.
- [ ] Preserve legacy-off behavior: identical messages/records/decisions/fingerprints/call counts, no sidecar creation or additional runtime allocation.
- [ ] In an enabled report context with disabled development semantics, emit truthful `not_run` and make zero supplemental worker calls.
- [ ] For separately authorized required mode, establish the concrete registered executor before invocation. Charge work to existing deadlines without changing terminal decisions, retries, or ordering.
- [ ] Reuse complete matching output after restart; never accept partial/corrupt output as completed evidence or repeat a provider call to repair local recovery.

**Focused checks:** byte-identical legacy-off behavior; skipped mode with zero worker calls; missing executor; safe paths and foreign digests; interrupted writes; matching/nonmatching restart; unchanged rejection; deadline exhaustion and cleanup.

**Acceptance:** no prepared campaign or historical authority changes, and no false observations in skipped or failed contexts.

## Task 5: Prove measured findings reach the next hypothesis request

This replaces the original critic-only acceptance. No new model role or call is added.

### 5A. Critic delivery and evidence grounding

- [ ] Register the extension's approved evidence vocabulary and projection schema. Review sanitization, request/schema identities, evidence counts, and admission bounds together.
- [ ] Issue IDs from authenticated report bytes. Responses may cite only IDs supplied to that exact request. Prefer existing role-input shapes; explicitly version any necessary extension and leave legacy-off unchanged.
- [ ] Deliver every declared assessment and limitation. Critic prose cannot override measured status or create new search selection rules.

### 5B. Next-investigator continuity

- [ ] Extend the existing `_investigator_parts` path through an explicit opt-in adapter, not another memory path. Reuse existing authenticated hypothesis/critic replay and checkpoint-authorized experiment selection.
- [ ] Supply relevant deterministic findings independently of whether the critic cited each measurement. Retain an unambiguous local-to-role binding for report, hypothesis, parent, experiment, episode ordinal, cost scenario, units, coverage and execution state.
- [ ] Reissue new request-local IDs without losing or conflating the original experiment/scenario grouping. Do not infer grouping from a hash, metric name, ordinal, or critic narrative.
- [ ] Keep negative and insufficient findings visible with their applicability. Missing optional evidence remains unavailable; it is not confirmation or an invitation to invent a result.
- [ ] Preserve the existing latest campaign-direction path. Do not silently reorder legacy memory selection, reinterpret templates as proven knowledge, or add cross-campaign retrieval.

### 5C. Final-packet bounds and evidence selection

- [ ] Record complete/summarized/omitted extension findings and reasons. Mandatory relevant evidence includes its qualifiers; fail explicitly if it cannot fit rather than send an unqualified claim.
- [ ] Verify actual serialized investigator and critic requests after reissuance, plus response schema and transport overhead, against existing evidence/byte/token/cost limits and admission checks.
- [ ] Keep all full records local and unchanged. No hidden context expansion, budget increase, automatic retry, or paid validation run.

**Required continuity checks, using authorized local fixtures only:**

| Case | Required observation |
| --- | --- |
| Round N report -> persisted record/sidecar -> round N+1 request | Exact measurements, assessment and scope survive; old request IDs are replaced with correctly bound new IDs. |
| Restart between those stages | Same source facts and equivalent bound next-request content; no duplicate worker/provider invocation. |
| Contradicted or insufficient mechanism with higher CAGR | Numerical improvement does not erase the mechanism result or its qualifier. |
| Critic does not cite a relevant measurement | The independently projected measured finding still reaches the next investigator when selected and within budget. |
| Same metric name across two episodes/cost scenarios | Values cannot exchange context or collapse into one unlabeled scalar. |
| Rare reason outside a generic top-four summary | The declared prediction's measurement is included by identity or explicitly unavailable. |
| Memory pressure with complete/summary/omitted records | Inclusion/omission is visible; retained claims keep scope, denominator and limitations. Latest campaign direction is preserved. |
| Failed execution or disabled semantics | `failed`/`not_run` remains distinct from hypothesis contradiction, including in compact context. |
| Final request near the budget ceiling | Bound includes newly issued evidence and schema/transport overhead; no truncation or admission bypass. |
| Extension disabled | Existing role messages, call counts, serialized records and search decisions remain unchanged. |
| Foreign report, stale parent, unknown evidence ID, or heldout provenance | Rejected before role construction/dispatch without exposing raw rows or credentials. |

**Acceptance:** an independent reader can trace one measured finding through the actual next investigator request, without running a model. This proves information delivery, not model adherence or better investment results. A later model-quality study requires a separate equal-budget experiment and authorization.

## Task 6: Independent review and closeout

- [ ] Review the exact diff and allowed seams. Reject out-of-scope strategy, evaluator, ranking, fingerprint, qualification, authorization, or historical-artifact changes.
- [ ] Review the full round-to-round evidence trace, deterministic measurements, final packet budgets, disabled modes, and restart behavior.
- [ ] Perform only currently authorized checks; record source commit, exact command, result and scope. Never substitute static inspection for execution or a mock for a real-provider success claim.
- [ ] Publish a handoff with changed files, schema versions, remaining limitations, permissions, actual verification evidence, and explicit separation of wiring from hypothesis-quality/generalization claims.

### Proposed verification commands, not executed by this planning revision

Use only after the proposed files exist and the owner authorizes these focused checks. Fixtures must use temporary paths, supplied data, and injected/mocked external ports; no `.env`, real providers, market datasets, saved campaigns, or heldout data.

```powershell
python -B -m pytest -p no:cacheprovider --no-cov -q -m "not integration" tests/test_pit_optimizer_v5_mechanism_evidence.py tests/test_pit_optimizer_v5_mechanism_integration.py
python -m ruff check core/pit_optimizer_v5/mechanism_contracts.py core/pit_optimizer_v5/mechanism_probes.py core/pit_optimizer_v5/mechanism_reports.py core/pit_optimizer_v5/mechanism_artifacts.py tests/test_pit_optimizer_v5_mechanism_evidence.py tests/test_pit_optimizer_v5_mechanism_integration.py
git diff --check
```

Also lint each existing adapter actually changed. Follow required repository gates when authorized; do not expand broad legacy suites merely because old plans mention them. The documentation-only CI skip is not a passing test and must not be copied to bypass later implementation checks.

## Definition of done

Completion requires authenticated precommitment, reproducible paired observations, independently reconstructible assessments, truthful missing/failed evidence, and demonstrated delivery of scoped findings to the next exact investigator request after persistence and restart. Legacy-off behavior, heldout boundaries, budgets, and search semantics remain intact.

All unchecked or unperformed work remains pending/unverified. No higher CAGR, live deployment, resumed campaign, model-weight improvement, or full reproduction of GLM's unpublished harness is required or implied.
