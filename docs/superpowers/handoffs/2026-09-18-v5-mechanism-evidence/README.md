# Codex Start Here: V5 Mechanism Evidence

**Closeout date:** 2026-09-19 (America/Toronto; the implementation crossed midnight from the September 18 plan)
**Plan directory:** `2026-09-18-v5-mechanism-evidence`
**Revision:** 4 — final-review fix atop the Tasks 1–5 implementation
**Status at this documentation snapshot:** The one Important current-status finding is fixed and the named provider-free verification passes. The controller still owns packaging and the same reviewer’s scoped re-review.
**Historical source baseline:** `2b1c447dc37805edcd7a4f630244ae0267f99620`
**Source verified for this closeout:** the final-review fix atop `2b1c447dc37805edcd7a4f630244ae0267f99620`; the containing commit records its exact identity.

Tasks 1–5 now carry a precommitted mechanism claim through paired observations, deterministic assessment, authenticated sidecars, persistence, compaction, restart, and the next investigator request. Task 5 also exercised the supplied-port `_Runtime.run` path and the current critic request. The evidence proves information delivery and request wiring for the bounded offline fixture. It does not prove that a model follows the evidence, that a provider or executor succeeds, or that strategy performance improves.

The final-review fix keeps candidate evaluation status separate from supplemental observation status. Admitted `quick_rejected`, `timed_out`, `cancelled`, and `evaluation_failed` candidates retain an authenticated completed local report when one exists; exact evaluator context remains absent with `evaluator_metric_missing`. Historical lookup and current projection use the existing admitted-status helper, while missing or mismatched authority still fails closed. The current runtime observes every behaviorally distinct candidate before quick rejection or discovery evaluation, so no legal candidate reaches the critic without its bound/run/report; deadline and cancellation abort the round before critic construction.

The September 18 review and planning records remain historical. Their reviewed source is `15ba962743da2c2ca73becdf85bc639c4f670dfa`; they did not constitute implementation or test execution. The September 9 saved checkpoint remains preserved and paused. The later explicit user grant authorized implementation with Luna at maximum reasoning, focused offline tests, and deterministic synthetic fixtures only.

## Read in this order

1. Read repository guidance and the [preserved September 9 checkpoint](../2026-09-09-pit-optimizer-v5/README.md).
2. Read the [historical source review and GLM comparison](../../reviews/2026-09-18-v5-glm-loop-review.md). Its source-tracing and unpublished-harness limits remain historical.
3. Read the [revision-2 design](../../specs/2026-09-18-v5-mechanism-evidence-design.md) and [implementation plan](../../plans/2026-09-18-v5-mechanism-evidence.md).
4. Read [IMPLEMENTATION.md](IMPLEMENTATION.md) for the final API surface, worked trace, verification provenance, rulings, and remaining questions.

## Scope and authorization

The original revision-2 planning authorization covered source review and planning only. The later explicit implementation grant covered the new contracts, deterministic evidence, compaction, restart, legacy compatibility, and provider-free typed fixtures. It excluded market datasets, backtests, providers, network/model calls, saved campaigns or grants, credentials and `.env`, candidate-source execution, orders, trading, deployment, and dependency changes. Those boundaries still apply after this closeout.

The code keeps strategy rules, evaluator arithmetic, ranking, fingerprints, qualification, legacy serializers, provider limits, and historical artifacts intact. Legacy-off remains a true no-op. The enabled path is an additive, authenticated extension with typed role projections and read-only historical recovery.

## Final verification evidence

The historical Task 5 fix report recorded this exact combined named-module command on the pre-fix source bytes:

```powershell
$env:PYTHON_DOTENV_DISABLED='1'; py -3.13 -B -m pytest -p no:cacheprovider --no-cov tests/test_pit_optimizer_v5_mechanism_artifacts.py tests/test_pit_optimizer_v5_mechanism_evidence.py -q
```

Result: **83 passed, 2 known warnings in 46.28s**. The warnings are the existing `cache_dir` pytest configuration warning and `websockets.legacy` deprecation warning. This remains historical evidence for source baseline `2b1c447`.

The final-review fix ran the same command after the source and focused-test changes:

```powershell
$env:PYTHON_DOTENV_DISABLED='1'; py -3.13 -B -m pytest -p no:cacheprovider --no-cov tests/test_pit_optimizer_v5_mechanism_artifacts.py tests/test_pit_optimizer_v5_mechanism_evidence.py -q
```

Result: **89 passed, 2 known warnings in 73.99s**. The warnings are the existing `cache_dir` pytest configuration warning and `websockets.legacy` deprecation warning. The four direct supplied-port status cases cover `quick_rejected`, `timed_out`, `cancelled`, and `evaluation_failed`; two fresh supplied-port `_Runtime.run` cases cover a mixed quick-rejection critic batch and discovery failure, including persisted/restarted investigator projection. The fixture worker factory was called once per candidate in both full runtime cases.

The final-review scoped Ruff check passed for the changed/new Python paths:

```powershell
py -3.13 -m ruff check core/pit_optimizer_v5/mechanism_artifacts.py core/pit_optimizer_v5/production_runtime.py tests/test_pit_optimizer_v5_mechanism_artifacts.py
```

Ruff reported `All checks passed!`. `git diff --check` exited `0`; Git emitted only its known LF/CRLF conversion notices. The earlier source-byte checks remain historical where paths were unchanged.

The exact command/result provenance and the compact passing fixture trace are in [IMPLEMENTATION.md](IMPLEMENTATION.md). The original Task 5 report and its 83-test fix report remain historical baseline provenance; this final-fix report and the 89-test verification are authoritative for this closeout.

## Interface and evidence boundaries

The opt-in surface adds the versioned `MechanismEvidenceRowV1`, `MechanismRoleProjectionV1`, and `MechanismRoleInputV1` wrapper, together with omission-only `MechanismMemoryDispositionV1`. Complete or summary selection is recorded by `MechanismRoleProjectionV1.memory_selection`. Omitted findings are recorded separately in `MechanismRoleInputV1.omitted` as `MechanismMemoryDispositionV1` entries. `MechanismArtifactRepositoryV5.load_existing_evidence_for_record(...)` authenticates persisted history through the checkpoint-authorized record and source; `MechanismRuntimeExtensionV1.role_request_evidence(...)` serves finalized current-round evidence in memory. `MechanismRoleRequestAdapterV1` composes those views with the unchanged investigator path, and `critic_request_with_mechanism(...)` builds the complete augmented critic request before its single final guard. The author continues to use `AuthorRoleInputV5`; legacy role constructors and the six-field `RoleRequestV5` shape remain compatible.

Rows carry bound identities/context, a `MechanismPredictionResultV1` containing metric, units, measured values, denominator, availability, and assessment, and request-local evidence IDs. The projection carries execution, controls, applicability, coverage, and limitations. Missing or non-testable omissions are separate wrapper records, not projection execution fields. The wrapper has no arbitrary metric or raw-data escape hatch, no extra retry, and no fixed eight-projection cap.

Admission checks still use canonical request bytes, wire messages, schema bytes, prospective overhead, token bounds, cost bounds, and the existing campaign/provider lifecycle authorities. The fixture’s exact synthetic accounting uses a `4096` input-overhead bound, `$0.01`/million input and output prices, and a `$10` ceiling. These are serialized-byte and prospective-bound checks; they are not measured provider usage, billed cost, a live admission grant, or a successful provider call.

## Remaining limits

No authorized check establishes model adherence, hypothesis quality, CAGR or investment improvement, provider transport, a real executor/sandbox, a live grant, candidate execution, market behavior, held-out generalization, campaign resumption, or deployment. The chronology fixture also does not establish general rejection of every hostile journal permutation. Reproducing the full unpublished GLM harness remains out of scope. Those are future empirical work with separate authorization.

The final whole-branch review found one Important current-status issue. This fix addresses that finding; the controller will request the same reviewer’s scoped re-review and this handoff does not preclaim its approval.
