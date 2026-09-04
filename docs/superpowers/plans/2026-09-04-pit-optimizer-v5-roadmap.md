# PIT Optimizer V5 Hybrid Architecture Implementation Roadmap

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the brittle single-candidate V4 loop with a truthful, durable, multi-candidate hybrid optimizer that can research adaptive O'Neil strategies against configurable annualized portfolio-return targets.

**Architecture:** Build V5 beside the legacy optimizer. First establish the versioned evaluator
kernel, then expand the causal O'Neil policy interface and point-in-time universe, implement the pure
search domain with durable critic memory and local variants, commit the campaign panels, and only
then capture one baseline authority. Finally prove the system through staged provider-free and
authorized campaigns. Annualized portfolio CAGR remains the transparent primary objective;
supporting metrics explain behavior and prevent invalid measurements rather than forming a weighted
score.

**Tech Stack:** Python 3.13, frozen dataclasses and Protocols, Decimal, pandas, SQLite PIT bundles, canonical JSON, Git-backed disposable workspaces, network-disabled Docker evaluation, existing OpenRouter/OpenAI gateway adapters, pytest, Ruff, and local ignored `.artifacts` evidence.

**Spec:** `docs/superpowers/specs/2026-09-04-pit-optimizer-v5-hybrid-architecture-design.md`

## Global Constraints

- Preserve V2-V4 manifests, artifacts, ledgers, checkpoints, and holdout results as read-only history.
- Add V5 through new schema identities and new modules; do not reinterpret a legacy artifact as V5.
- Keep market data, role artifacts, qualification composition, and replay outputs local and ignored. Never upload them to GitHub.
- Keep discovery `apply=false`. Candidate source changes only disposable workers and local artifacts.
- Preserve point-in-time causality, authenticated source identity, exact accounting, no leverage, and network-disabled candidate evaluation.
- Keep annualized portfolio CAGR as the primary selection objective. Do not add an opaque weighted score.
- Make 10%, 20%, and 50% ordinary manifest values. Do not hard-code a production target into V5 contracts.
- Permit broad model-authored strategy logic inside the versioned policy interface, including a full-source escape hatch.
- Use provider-free local variants for numeric and enumerated exploration around model-authored structures.
- Keep provider retries and schema repair explicit and disabled unless a run manifest and operator authorization enable them.
- Use focused contract checks and subset evidence. Do not build another broad legacy test program.
- Do not open qualification or start a full replay during architecture implementation.

## Deliverables and Dependency Order

| Phase | Detailed plan slice | Independently usable result | Depends on |
|---|---|---|---|
| 1 | Evaluator-truth Tasks 1-5 | Canonical execution-profile identity, truthful fills, diagnostics, and evaluator contracts | None |
| 2 | Strategy/universe Tasks 1-7 | Policy interface V3 plus authenticated three-universe PIT data | Phase 1 execution contract |
| 3 | Learning/search Tasks 1-9 | Pure multi-candidate search engine with durable feedback and local variants | Phases 1-2 contracts |
| 4 | Campaign Task 1, then evaluator-truth Tasks 6-7 | Committed discovery/held-out panels and one authenticated V5 baseline authority | Phases 1-3 |
| 5 | Campaign Tasks 2-4 | Provider-free end-to-end subset proof and authorization-ready discovery manifest | Phase 4 |
| 6 | Campaign Tasks 5-8 | Authorized discovery, detached confirmation/qualification, and replay readiness | Phase 5 plus explicit stage authorization |

---

### Task 1: Land the V5 Design and Planning Boundary

**Files:**

- Create: `docs/superpowers/specs/2026-09-04-pit-optimizer-v5-hybrid-architecture-design.md`
- Create: `docs/superpowers/plans/2026-09-04-pit-optimizer-v5-roadmap.md`
- Create: `docs/superpowers/plans/2026-09-04-pit-optimizer-v5-evaluator-truth.md`
- Create: `docs/superpowers/plans/2026-09-04-pit-optimizer-v5-learning-search.md`
- Create: `docs/superpowers/plans/2026-09-04-pit-optimizer-v5-strategy-universe.md`
- Create: `docs/superpowers/plans/2026-09-04-pit-optimizer-v5-campaign.md`

**Interfaces:**

- Consumes: approved V5 architecture findings and the legacy V4 design.
- Produces: one implementation order, shared names, shared constraints, and four executable plans.

- [ ] **Step 1: Verify all five plans point to the V5 specification**

Run:

```powershell
rg -n -g "2026-09-04-pit-optimizer-v5-*.md" '\*\*Spec:\*\* `docs/superpowers/specs/2026-09-04-pit-optimizer-v5-hybrid-architecture-design.md`' docs/superpowers/plans
```

Expected: one match in the roadmap and each detailed plan.

- [ ] **Step 2: Verify the plans do not authorize live research implicitly**

Run:

```powershell
rg -n -g "2026-09-04-pit-optimizer-v5-*.md" "OpenRouter|qualification|full replay|apply=false" docs/superpowers/plans
```

Expected: provider calls, qualification, and full replay are explicitly deferred to authorized campaign tasks; discovery remains `apply=false`.

- [ ] **Step 3: Commit the approved design and plans**

```powershell
git add docs/superpowers/specs/2026-09-04-pit-optimizer-v5-hybrid-architecture-design.md docs/superpowers/plans/2026-09-04-pit-optimizer-v5-roadmap.md docs/superpowers/plans/2026-09-04-pit-optimizer-v5-evaluator-truth.md docs/superpowers/plans/2026-09-04-pit-optimizer-v5-learning-search.md docs/superpowers/plans/2026-09-04-pit-optimizer-v5-strategy-universe.md docs/superpowers/plans/2026-09-04-pit-optimizer-v5-campaign.md
git commit -m "docs: plan PIT optimizer v5 architecture"
```

### Task 2: Implement the Evaluator Kernel (Tasks 1-5 Only)

**Files:**

- Read: `docs/superpowers/plans/2026-09-04-pit-optimizer-v5-evaluator-truth.md`

**Interfaces:**

- Consumes: no V5 implementation dependency.
- Produces: canonical `ExecutionProfileV5` identity, `EvaluationReportV5`, fill/diagnostic
  primitives, and the evaluator service. It deliberately does not capture a baseline yet.

- [ ] **Step 1: Complete evaluator-truth Tasks 1-5 in order**

Stop before `### Task 6: Capture One V5 Baseline Authority`; that task requires artifacts created by
later phases.

- [ ] **Step 2: Verify the kernel boundary**

```powershell
python -B -m pytest tests/test_backtest_fills.py tests/test_pit_optimizer_evaluator_v5.py -q
python -B -m ruff check core/backtest_fills.py core/pit_optimizer_v5/contracts.py core/pit_optimizer_v5/evaluator.py core/pit_optimizer_v5/diagnostics.py
```

Expected: the V5 profile digest changes with any semantic field, evaluator evidence reconciles, and
no baseline/data/provider artifact is required.

### Task 3: Execute the Strategy-and-Universe Plan

**Files:**

- Read: `docs/superpowers/plans/2026-09-04-pit-optimizer-v5-strategy-universe.md`

**Interfaces:**

- Consumes: the V5 execution profile and evaluator contracts.
- Produces: policy interface V3, add-on decisions, causal leadership features, stable security
  lineages, and bundle schema V3 for the three-universe pool.

- [ ] **Step 1: Complete strategy/universe source Tasks 1-6 in order**

- [ ] **Step 2: Run Task 7 only when every authenticated local input passes its preflight**

An absent local input blocks only bundle capture. Do not fetch, upload, or synthesize confidential
market data. Resume Task 7 after the input is staged locally.

- [ ] **Step 3: Confirm all market data remains outside Git**

```powershell
git status --short
git check-ignore .artifacts/pit-optimizer-v5/data
```

Expected: only source/documentation changes are listed; the local data root is ignored.

### Task 4: Execute the Learning-and-Search Plan

**Files:**

- Read: `docs/superpowers/plans/2026-09-04-pit-optimizer-v5-learning-search.md`

**Interfaces:**

- Consumes: canonical execution/report contracts and policy interface V3 from Tasks 2-3.
- Produces: pure V5 search state, durable experiment memory, reconstructible archive parents,
  candidate rendering, local variants, selection, and neutral provider adapters.

- [ ] **Step 1: Complete every learning/search checkbox in order**

```powershell
rg -n "^- \[ \]" docs/superpowers/plans/2026-09-04-pit-optimizer-v5-learning-search.md
```

Expected: no output after the phase is complete.

- [ ] **Step 2: Confirm pure modules have no infrastructure imports**

```powershell
rg -n "agent_loop|OpenRouter|docker|subprocess|git" core/pit_optimizer_v5/contracts.py core/pit_optimizer_v5/candidate_ir.py core/pit_optimizer_v5/probes.py core/pit_optimizer_v5/memory.py core/pit_optimizer_v5/search.py core/pit_optimizer_v5/selection.py
```

Expected: no imports or direct infrastructure calls.

### Task 5: Build Panel Commitments, Then Capture the Baseline Authority

**Files:**

- Read: campaign Task 1 in `docs/superpowers/plans/2026-09-04-pit-optimizer-v5-campaign.md`
- Read: evaluator-truth Tasks 6-7 in `docs/superpowers/plans/2026-09-04-pit-optimizer-v5-evaluator-truth.md`

**Interfaces:**

- Consumes: authenticated schema-V3 bundle, V3 baseline policy, panel builder, evaluator kernel, and
  distinct local confirmation/qualification retirement ledgers.
- Produces: provider-visible discovery plan, separately committed confirmation/qualification plans,
  and the one V5 baseline authority used by later stages.

- [ ] **Step 1: Complete campaign Task 1**

- [ ] **Step 2: Initialize both V5 held-out retirement domains and build all plans once**

```powershell
python -B -m core.pit_optimizer_v5.cli init-stage-ledgers --confirmation-output .artifacts/pit-optimizer-v5/panels/confirmation-retirement.json --qualification-output .artifacts/pit-optimizer-v5/panels/qualification-retirement.json
python -B -m core.pit_optimizer_v5.cli build-panels --bundle .artifacts/pit-optimizer-v5/data/pit_three_universe.sqlite3 --start-date 2021-01-04 --end-date 2025-12-31 --seed pit-v5-first-campaign --target-pct 10.00 --confirmation-ledger .artifacts/pit-optimizer-v5/panels/confirmation-retirement.json --qualification-ledger .artifacts/pit-optimizer-v5/panels/qualification-retirement.json --discovery-output .artifacts/pit-optimizer-v5/panels/discovery-plan.json --confirmation-output .artifacts/pit-optimizer-v5/panels/confirmation-plan.json --qualification-output .artifacts/pit-optimizer-v5/panels/qualification-plan.json
python -B -m core.pit_optimizer_v5.cli verify-panels --discovery-plan .artifacts/pit-optimizer-v5/panels/discovery-plan.json --confirmation-commitment .artifacts/pit-optimizer-v5/panels/confirmation-plan.json --qualification-commitment .artifacts/pit-optimizer-v5/panels/qualification-plan.json --keep-held-out-sealed
```

Expected: lineage cohorts and dates are pairwise separated; the discovery plan exposes only held-out
digests; all outputs are create-only.

- [ ] **Step 3: Complete evaluator-truth Tasks 6-7**

Expected: baseline gross/base/stress evidence covers mechanics, quick, and all four discovery
episodes twice, while confirmation and qualification remain unevaluated.

- [ ] **Step 4: Confirm evidence roots are ignored**

```powershell
git check-ignore .artifacts/pit-optimizer-v5/evaluator
git check-ignore .artifacts/pit-optimizer-v5/panels
```

### Task 6: Execute the Campaign Plan Through Provider-Free Proof and Readiness

**Files:**

- Read: campaign Tasks 2-4 in `docs/superpowers/plans/2026-09-04-pit-optimizer-v5-campaign.md`

**Interfaces:**

- Consumes: completed V5 evaluator, search engine, policy interface, bundle, panels, and baseline.
- Produces: provider-free complete-loop evidence and an authorization-ready discovery manifest.

- [ ] **Step 1: Complete campaign Tasks 2-4, stopping at Task 4 Step 4**

Do not execute the rendered live-provider command.

- [ ] **Step 2: Inspect subset evidence**

```powershell
python -B -m core.pit_optimizer_v5.cli summarize --artifact-root .artifacts/pit-optimizer-v5/subset
```

Expected: investigator/author/variants/all-four-episodes/critic/restart completed with a fixture
provider; provider calls are zero; source is unchanged; cleanup is complete.

### Task 7: Run Authorized Discovery, Then Detached Qualification

**Files:**

- Read: campaign Tasks 5-8 in `docs/superpowers/plans/2026-09-04-pit-optimizer-v5-campaign.md`

**Interfaces:**

- Consumes: an explicitly operator-authorized manifest and completed provider-free proof.
- Produces: a frozen discovery champion; then separately authorized provider-free confirmation,
  one-use qualification, and full-replay readiness.

- [ ] **Step 1: Run only the authenticated command rendered from the approved manifest**

Do not hand-edit call, token, retry, model, cost, source, or resource fields.

- [ ] **Step 2: Review discovery evidence without opening held-out plans**

```powershell
python -B -m core.pit_optimizer_v5.cli summarize --artifact-root .artifacts/pit-optimizer-v5/discovery
```

Expected: content-free candidate-family metrics, best campaign CAGR, target gap, episode consistency,
provider accounting, and `confirmation_started=false`, `qualification_started=false`.

- [ ] **Step 3: Run confirmation and one-use qualification only at their separate decision points**

Expected: neither stage makes provider calls or mutates search memory; qualification is permanently
retired whether it succeeds or fails; full replay remains stopped unless qualification reaches the
configured target and beats its same-panel V5 baseline.

## Roadmap Completion Criteria

- All detailed plans are complete and their focused checks pass.
- One V5 baseline authority and execution profile are used everywhere.
- Full critic feedback survives restart and reaches the next investigator.
- One hypothesis produces multiple behaviorally distinct provider-free variants.
- The archive preserves multiple strategy families without an opaque weighted score.
- The target is configurable and no V5 code calls `AnnualizedReturnTarget.production()`.
- The local three-universe bundle validates with explicit overlaps and non-tradable references.
- A provider-free subset completes before any authorized model campaign.
- A discovery result cannot be described as double-digit success until untouched qualification
  independently reports at least the configured annualized portfolio return.
