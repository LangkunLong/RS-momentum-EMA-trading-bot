# PIT Optimization Cycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and offline-verify one bounded, full-PIT, three-role optimization cycle that evaluates exactly one canonical entry-threshold change and exports only an inert result.

**Architecture:** A focused contract module owns the 12-candidate catalog, strict role payloads, aggregate observations, policy-delta validation, and deterministic acceptance. `agent_loop.py` adds an isolated `pit_optimization` prepare/canary route; prepare authenticates the sealed Task 11 authority without a provider, while canary reuses the existing source preflight, budget ledger, audit trail, and disposable candidate export to run one full-window PIT evaluation and derive the 2025 holdout.

**Tech Stack:** Python 3.13, standard-library dataclasses/hashlib/json/pathlib/subprocess/tempfile, pandas, the existing PIT SQLite bundle, PortfolioSimulator, pytest, and the existing OpenRouter gateway/audit infrastructure

**Spec:** `docs/superpowers/specs/2026-08-26-pit-optimization-cycle-design.md`

## Global Constraints

- Work only in `.worktrees/pit-optimization-cycle` on `codex/pit-optimization-cycle`.
- Do not commit, merge, push, switch branches, mutate Git state, or edit a sibling worktree.
- Do not download data, access a live market/fundamental source, install dependencies, or alter sealed artifacts.
- Keep `test`, `backtest`, and `pit_diagnosis` behavior unchanged.
- The fixed full window is `2021-01-01` through `2025-12-31`; the holdout is the 2025 slice of that run.
- Bind the exact PIT bundle and Task 11 manifest hashes from the spec.
- The only editable candidate file is `core/canslim/entry_contract.py`; allow exactly one of 12 one-line alternatives.
- Preserve all effective-policy causal invariants and require exactly one active policy leaf delta.
- One canary means one sample, one iteration, at most three calls, at most USD 0.50, no retry, and `apply=false`.
- Write only inert diffs and aggregate artifacts. Never modify the source worktree.

---

### Task 1: Closed Optimization Contract

**Files:**
- Create: `core/pit_optimization_contract.py`
- Test: `tests/test_pit_optimization_contract.py`

**Interfaces:**
- Produces: `CandidateDefinition`, `OptimizationWindowMetrics`, `OptimizationObservation`, `OptimizationComparison`, `PitOptimizationRoute`, `PitOptimizationReasoning`, `PitOptimizationCoding`, `candidate_catalog()`, `verify_catalog_source()`, `build_comparison()`, `validate_policy_delta()`, and strict JSON/schema helpers.
- Consumes: canonical JSON mappings and the live `core/canslim/entry_contract.py` bytes; it performs no I/O except explicit source verification.

- [ ] **Step 1: Write failing catalog and role-schema tests.** Assert exactly 12 stable IDs, six source constants with two alternatives each, no duplicate replacements, exact closed JSON keys, an orchestrator that cannot select a candidate, a reasoner that selects exactly one catalog ID, and a coder that must reproduce the controller replacement.
- [ ] **Step 2: Run the focused tests and verify the expected import/attribute failures.** Run `python -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimization_contract.py`.
- [ ] **Step 3: Implement the frozen catalog and strict payload parsers.** Normalize all strings and numbers, reject duplicate JSON keys and non-finite values, and return immutable tuples/mappings.
- [ ] **Step 4: Write failing objective/policy tests.** Cover both full and holdout thresholds at their exact boundaries, exactly one leaf change, and rejection when any causal invariant changes.
- [ ] **Step 5: Implement deterministic comparison and policy validation.** Compute `annualized_return_pct - abs(min(max_drawdown_pct, 0))` and return every named acceptance check plus the conjunction.
- [ ] **Step 6: Run the focused contract tests to green.**

### Task 2: Sealed Baseline Readiness

**Files:**
- Create: `core/pit_optimization.py`
- Modify: `agent_loop.py`
- Test: `tests/test_pit_optimization_contract.py`

**Interfaces:**
- Produces: `PitOptimizationGateConfig`, `prepare_pit_optimization(config, source_root, artifact_root) -> PitOptimizationReadiness`, and `PIT_OPTIMIZATION_READY=<canonical-json>`.
- Consumes: absolute bundle/baseline paths, exact hashes, current PIT-specific effective policy, and the sealed summary/equity/transactions/weekly/funnel artifacts.

- [ ] **Step 1: Write failing readiness tests with a small authenticated fixture.** Prove wrong bundle/manifest/artifact hashes, wrong dates, wrong baseline metrics, and non-regular inputs fail; prove prepare does not construct a gateway or run a candidate.
- [ ] **Step 2: Run the readiness tests and confirm they fail for missing production interfaces.**
- [ ] **Step 3: Implement strict config and baseline authentication.** Reuse the canonical Task 11 baseline authority where possible, independently verify required artifact bytes, verify the live PIT bundle, instantiate the full PIT simulator without running it, and bind its effective-policy digest.
- [ ] **Step 4: Implement aggregate baseline metrics.** Reconstruct completed trade lots across scale-outs, slice full and 2025 curves, aggregate weekly cash/exposure/holdings and funnel stages, and reject non-finite or inconsistent values.
- [ ] **Step 5: Add `--gate pit_optimization --optimization-phase prepare|canary` without changing existing branches.** Reject legacy gate-only options in this gate, require the exact budget/apply constraints, and route prepare before Docker/OpenRouter initialization.
- [ ] **Step 6: Run readiness tests to green and run prepare on the real sealed Task 11 inputs.** Preserve the canonical readiness artifact and record its SHA-256.

### Task 3: One Disposable Full-PIT Canary

**Files:**
- Modify: `core/pit_optimization.py`
- Modify: `agent_loop.py`
- Test: `tests/test_pit_optimization_contract.py`

**Interfaces:**
- Produces: exactly three role calls, one exact candidate patch in a disposable candidate, one full-PIT aggregate result, one deterministic comparison, one inert unified diff, and a closed terminal summary.
- Consumes: the readiness identity, existing budget/audit/source-preflight capabilities, and current Python runtime plus local PIT bundle. No network is available to the evaluator.

- [ ] **Step 1: Write failing controller-flow tests with injected gateway and evaluator fakes.** Assert role order, citations, exact replacement handoff, one evaluation, zero source mutation, and useful accepted/rejected terminal results.
- [ ] **Step 2: Run those tests and confirm failure at the missing canary route.**
- [ ] **Step 3: Add PIT-optimization response formats and one-attempt gateway calls.** Keep these schemas separate from the legacy gateway family and account for exactly one call per role.
- [ ] **Step 4: Implement exact candidate validation and disposable evaluation.** Apply only the selected one-line replacement to the controller-owned candidate, verify one policy leaf and all causal invariants, run `PortfolioSimulator` once over the full fixed window, derive the holdout, and verify the source candidate did not change during evaluation.
- [ ] **Step 5: Implement inert artifacts and terminal summary.** Write aggregate baseline/candidate/comparison JSON and the inert diff under the audit root; retain provider payloads only in the private audit chain.
- [ ] **Step 6: Run controller-flow and legacy parser/config tests to green.**

### Task 4: Offline Completion Evidence

**Files:**
- Verify only: all files above and `agent_loop.py`

**Interfaces:**
- Produces: a clean offline-readiness artifact and the exact operator command for the paid canary.

- [ ] **Step 1: Run `python -B -m py_compile agent_loop.py core/pit_optimization.py core/pit_optimization_contract.py`.**
- [ ] **Step 2: Run the focused optimizer suite and the existing agent-loop gate/config regression selection.**
- [ ] **Step 3: Run `git diff --check` and confirm the diff is confined to this worktree.**
- [ ] **Step 4: Execute real `prepare` with the fixed bundle, baseline, Docker image identity, paths, three-call limit, USD 0.50, one iteration, and no `--apply`.**
- [ ] **Step 5: Verify the readiness artifact hash, the PIT-specific effective-policy digest, all 12 source replacements, and that no provider call or full candidate replay occurred.**
- [ ] **Step 6: Report the exact canary command and inputs. Do not execute it until the paid call is explicitly released.**
