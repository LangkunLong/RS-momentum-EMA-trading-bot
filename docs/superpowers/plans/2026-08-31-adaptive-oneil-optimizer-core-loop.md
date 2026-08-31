# Adaptive O'Neil Optimizer Core Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a durable, model-authored O'Neil strategy feedback loop that ranks candidates by absolute annualized portfolio return, preserves a discovery champion and one refinable branch across bounded runs, and proves the complete investigator-author-evaluator-critic path on a small local S&P-based canary before any full replay.

**Architecture:** Introduce schema-v4 optimizer contracts alongside read-only schema-v3 audit compatibility. The trusted engine creates one causal `MarketContextV1`, evaluates continuous constituent panels with the production CAGR formula, and keeps fills, accounting, data, and metric authority local. DeepSeek R1 receives the selected parent's three policy files and returns all three complete files as one candidate. The controller validates the atomic source bundle in a disposable checkout, evaluates it, promotes only the highest discovery CAGR, persists a champion plus one critic-selected exploratory branch, and treats fully accounted schema-invalid role output as recoverable without retrying it.

**Tech Stack:** Python 3.13, frozen dataclasses, Decimal, pandas, SQLite PIT bundles, canonical JSON and SHA-256 identities, Git-backed disposable workspaces, the existing network-disabled Docker policy worker, the existing OpenRouter/OpenAI gateway, and existing focused pytest/Ruff/compile checks.

**Spec:** `docs/superpowers/specs/2026-08-31-adaptive-oneil-optimizer-core-strategy-design.md`

## Global Constraints

- Keep `apply=false`; model-authored code remains a local candidate artifact and never changes the operator checkout.
- Candidate-editable source remains exactly `core/strategy_policy/entry.py`, `core/strategy_policy/risk.py`, and `core/strategy_policy/exit.py`.
- Remove single-file, diff-byte, changed-line, and hunk limits from the schema-v4 authoring path. Keep exact allowed paths, closed source contracts, syntax checks, AST purity, deterministic imports, no I/O, no reflection, and disposable-worker isolation.
- The only ranking objective is absolute `portfolio_annualized_return_pct`, computed with `((ending_equity / starting_equity) ** (365 / elapsed_calendar_days) - 1) * 100` and quantized to `0.01` percentage point for comparisons. Baseline deltas and other metrics are diagnostics only.
- Do not add liquidity, slippage, transaction-cost, drawdown, Sharpe, turnover, trade-count, or composite-score acceptance gates.
- A zero-trade or low-return executable quick-panel result is feedback, not rejection. Only a runtime-invalid candidate stops before discovery evaluation.
- Preserve the engine's existing no-leverage behavior, 8% maximum initial stop distance, maximum position risk, completed-session entry causality, next-eligible-open entry, and current exit timing.
- Preserve schema-v3 manifests, receipts, artifacts, and authorization ledgers as read-only audit history. Never reinterpret or resume them as schema v4.
- Every live run has a finite declared role plan, zero automatic provider retries, complete call/token/cost accounting, and no enforcing USD ceiling. An HTTP-accepted schema-invalid response consumes one provider call; a skipped unstarted plan slot consumes none.
- Keep credentials, raw PIT rows, local paths, qualification data, raw provider content, provider accounting internals, and secrets out of role inputs and public summaries.
- Keep the quick/discovery canary local and S&P-membership based. It proves the core architecture but cannot be described as final three-universe qualification.
- Do not acquire Nasdaq-100 or Russell 2000 constituent history in this plan. That is a separate provenance project after the core loop works; the schema-v4 qualifier must fail closed on `full_replay_ready` until the approved three-universe bundle exists.
- Do not add test files or new test functions. Update existing fixtures and existing assertions only where an interface changes, then use focused compilation, lint, parity, and live-canary evidence.
- Do not run the full replay, upload artifacts or market data, push strategy candidates, or make OpenRouter calls before the final canary task.

## File Structure and Responsibility Map

### PIT bundle and market observations

- `core/pit_provenance.py`
  - Owns the canonical non-tradable reference set `("IWM", "QQQ", "SPY")` and its identity contract.
- `core/pit_data.py`
  - Loads bundle schema v2; distinguishes tradable membership, reference prices, and all price symbols; exposes authenticated price-identity lineages; rejects any reference in membership/fundamentals.
- `export_pit_prices.py` and `core/alpaca_pit_backfill.py`
  - Export and authenticate membership prices plus all three references; remove SPY-only count/calendar assumptions.
- `build_pit_bundle.py` and `verify_pit_bundle.py`
  - Seal and verify the exact reference set, reference calendars, provenance, and membership/reference separation.
- `core/strategy_policy/contracts.py`
  - Owns `BenchmarkContextV1`, `MarketContextV1`, and the five policy snapshot contracts that contain `market`.
- `core/strategy_policy/market_context.py`
  - New protected module that computes one immutable causal context per completed session from trusted prices, PIT membership, and RS data.
- `core/strategy_policy/__init__.py`, `runtime.py`, and `worker.py`
  - Export interface version 2, serialize nested market contracts, and retain the closed worker protocol.
- `core/backtest_engine.py`
  - Loads references separately from tradable panel symbols, builds/caches current market context, carries the signal context in `PendingEntry`, and supplies it at the correct decision time.

### Continuous-panel evaluation

- `core/pit_optimizer_evaluation.py`
  - Adds `AnnualizedReturnTarget`, `PanelSecurityLineage`, `EvaluationPanelSpec`, `DiscoveryPanelPlan`, `QualificationPanelPlan`, `PanelAggregateSummary`, `DiscoveryPanelComparison`, `QualificationDecision`, and qualification-only one-use exposure identities.
- `core/pit_policy_parity.py`
  - Produces continuous-panel evidence containing production annualized return while retaining legacy fold readers for old artifacts.
- `core/pit_optimization.py`
  - Exposes schema-v4 readiness and continuous-panel evaluation entry points without removing schema-v3 audit loaders.

### Model source, search state, and accounting

- `core/pit_optimization_contract.py`
  - Adds schema-v4 campaign/run/source/parent/role contracts and response formats; removes patch bounds from the v4 path; binds every author response to the selected parent and exact three-file source map.
- `core/pit_optimizer_candidate.py`
  - Validates all three replacement sources as one candidate, derives the cumulative Git diff locally, and issues the candidate identity after the full bundle is valid.
- `core/pit_optimizer_authorization.py`
  - Records recoverable schema-invalid attempts and authenticated zero-charge plan skips while retaining complete accounting and sequential call-plan integrity.
- `core/pit_optimizer_artifacts.py`
  - Persists selected-parent, candidate-source, evaluation, decision, champion, branch, skip, and campaign-checkpoint artifacts in durability order.
- `core/pit_optimizer_controller.py`
  - Selects baseline/champion/branch parents, calls the three roles, handles candidate-level failures, ranks discovery CAGR, commits state transitions, and resumes authenticated checkpoints.
- `agent_loop.py`
  - Composes schema-v4 CLI/readiness/evaluator/gateway services, loads panel-specific universes, keeps role calls one-shot, and publishes content-free summaries.

### Detached qualification

- `core/pit_optimizer_holdout.py` and `pit_optimizer_holdout.py`
  - Load a frozen champion from an authenticated checkpoint, consume a qualification panel once, recompute same-panel baseline and candidate CAGR provider-free, and stop before full replay.

### Existing checks updated in place

- `tests/test_pit_data.py`, `tests/test_export_pit_prices.py`
- `tests/test_strategy_policy.py`, `tests/test_backtest_engine.py`, `tests/test_backtest_open_causality.py`
- `tests/test_pit_policy_parity.py`, `tests/test_pit_optimizer_v2.py`, `tests/test_pit_optimizer_loop.py`
- `tests/test_pit_optimizer_holdout.py`, `tests/test_pit_optimizer_subset_reference.py`, `tests/test_agent_loop.py`

---

### Task 1: Seal the Three Non-Tradable Market References

**Files:**

- Modify: `core/pit_provenance.py`
- Modify: `core/pit_data.py`
- Modify: `export_pit_prices.py`
- Modify: `core/alpaca_pit_backfill.py`
- Modify: `build_pit_bundle.py`
- Modify: `verify_pit_bundle.py`
- Modify: `tests/test_pit_data.py`
- Modify: `tests/test_export_pit_prices.py`

**Interfaces:**

- Produces: `PIT_NON_TRADABLE_REFERENCE_SYMBOLS = ("IWM", "QQQ", "SPY")`; `PITDataBundle.tradable_symbols()`; `reference_symbols()`; `price_symbols()`; authenticated `security_lineage_id` access derived from the price-identity `chain_id`; bundle kind `canslim_pit_v2`; schema version `2`.
- Preserves: `PITDataBundle.symbols()` only as a compatibility alias for `price_symbols()` until every strategic caller is migrated in Task 2.

- [ ] **Step 1: Replace every SPY-only provenance invariant with the exact sealed reference set.**

  Add the shared constant and canonical JSON/digest helpers in `core/pit_provenance.py`. In the exporter and backfill path, require price-identity request contracts for membership symbols plus all three references, require an identical expected trading-session calendar for SPY/QQQ/IWM, and compute membership counts by set difference rather than `len(symbols) - 1`.

- [ ] **Step 2: Add bundle-schema-v2 metadata and APIs.**

  Seal `non_tradable_reference_symbols_json`, `non_tradable_reference_symbols_sha256`, and the current source-universe label `sp500`. During `PITDataBundle._initialize_connection`, materialize separate immutable tradable, reference, and price-symbol sets. Reject references in membership or fundamentals, reject prices outside `tradables ∪ references`, and require provenance identities to equal that union exactly. Expose each tradable ticker's authenticated price-identity `chain_id` canonically as `security_lineage_id` so the panel builder groups renames as one lineage; the S&P-only canary assigns affiliation `sp500` from sealed bundle metadata.

- [ ] **Step 3: Make manifest and verification output prove reference completeness.**

  `PITDataBundle.manifest()` must report the exact reference list and first date, last date, and session count for each reference. `verify_pit_bundle.py` must compare these fields to the rebuilt source/provenance graph before accepting the manifest-last marker.

- [ ] **Step 4: Update existing bundle/export fixtures in place.**

  Extend the current SPY fixtures in `tests/test_pit_data.py` and `tests/test_export_pit_prices.py` with QQQ/IWM rows and assertions. Rewrite existing SPY-only count assertions; do not create new test functions.

- [ ] **Step 5: Run the focused static and existing checks.**

  ```powershell
  python -B -m compileall -q core/pit_provenance.py core/pit_data.py core/alpaca_pit_backfill.py export_pit_prices.py build_pit_bundle.py verify_pit_bundle.py
  python -B -m ruff check core/pit_provenance.py core/pit_data.py core/alpaca_pit_backfill.py export_pit_prices.py build_pit_bundle.py verify_pit_bundle.py --no-cache
  python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_data.py tests/test_export_pit_prices.py
  ```

  Expected: compilation/lint pass; the two existing test modules pass; a v1 bundle remains readable only through the explicit legacy path and cannot satisfy v4 readiness.

- [ ] **Step 6: Commit the reference boundary.**

  ```powershell
  git add core/pit_provenance.py core/pit_data.py core/alpaca_pit_backfill.py export_pit_prices.py build_pit_bundle.py verify_pit_bundle.py tests/test_pit_data.py tests/test_export_pit_prices.py
  git commit -m "feat: seal PIT market reference symbols"
  ```

### Task 2: Prevent Reference Symbols from Becoming Holdings

**Files:**

- Modify: `core/backtest_engine.py`
- Modify: `pit_baseline.py`
- Modify: `core/pit_optimization.py`
- Modify: `agent_loop.py`
- Modify: `pit_optimizer_holdout.py`
- Modify: `core/leader_basket.py`
- Modify: `core/pit_diagnosis/fact_cache.py`
- Modify: `tests/test_backtest_engine.py`
- Modify: `tests/test_pit_optimizer_subset_reference.py`

**Interfaces:**

- Consumes: `PITDataBundle.tradable_symbols()`, `reference_symbols()`, and `price_symbols()` from Task 1.
- Produces: explicit separation between `tradable_tickers`, `market_reference_tickers`, and `market_context_universe` in simulator/evaluator setup.

- [ ] **Step 1: Migrate every trading-universe construction to `tradable_symbols()`.**

  Replace `bundle.symbols()` and `ticker != "SPY"` filters in the listed files. Explicit user ticker lists must fail closed if they contain SPY, QQQ, or IWM. Candidate panels and holdings may contain only tradable membership symbols.

- [ ] **Step 2: Load reference prices only as observations.**

  In `PortfolioSimulator.run`, include all three references in `ticker_ohlcv` regardless of panel, keep them out of `tickers`, include full tradable membership plus references in `all_closes`, and bind both sets into checkpoint identity. Record tradable/reference/context counts in result configuration.

- [ ] **Step 3: Remove evaluator-side SPY arithmetic.**

  Update `_build_verification_scope` and the v3/v4 evaluator compositions so panel symbols are selected from `tradable_symbols()` and the separate full active membership universe is available for market context. Do not select v4 panel members from baseline trading activity.

- [ ] **Step 4: Rewrite existing reference-exclusion assertions.**

  Expand current subset-reference and bundle-backed simulator cases to prove none of SPY/QQQ/IWM can enter a panel, signal list, holding, or trade. Do not add a new test function.

- [ ] **Step 5: Verify and commit.**

  ```powershell
  python -B -m compileall -q core/backtest_engine.py core/pit_optimization.py core/leader_basket.py core/pit_diagnosis/fact_cache.py pit_baseline.py pit_optimizer_holdout.py agent_loop.py
  python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_backtest_engine.py tests/test_pit_optimizer_subset_reference.py
  git add core/backtest_engine.py core/pit_optimization.py core/leader_basket.py core/pit_diagnosis/fact_cache.py pit_baseline.py pit_optimizer_holdout.py agent_loop.py tests/test_backtest_engine.py tests/test_pit_optimizer_subset_reference.py
  git commit -m "fix: isolate PIT references from tradable universes"
  ```

  Expected: focused checks pass and every strategic caller has explicit reference/tradable semantics.

### Task 3: Add `MarketContextV1` and Preserve Causal Timing

**Files:**

- Create: `core/strategy_policy/market_context.py`
- Modify: `core/strategy_policy/contracts.py`
- Modify: `core/strategy_policy/__init__.py`
- Modify: `core/strategy_policy/runtime.py`
- Modify: `core/strategy_policy/worker.py`
- Modify: `core/strategy_policy/entry.py`
- Modify: `core/strategy_policy/risk.py`
- Modify: `core/strategy_policy/exit.py`
- Modify: `core/backtest_engine.py`
- Modify: `tests/test_strategy_policy.py`
- Modify: `tests/test_backtest_engine.py`
- Modify: `tests/test_backtest_open_causality.py`

**Interfaces:**

- Produces:

  ```python
  @dataclass(frozen=True, slots=True)
  class BenchmarkContextV1:
      symbol: str
      close_to_sma_50_fraction: float
      close_to_sma_200_fraction: float
      realized_volatility_20d_fraction: float

  @dataclass(frozen=True, slots=True)
  class MarketContextV1:
      schema_version: int
      session: str
      oneil_regime: str
      distribution_days: int
      follow_through: bool
      benchmarks: tuple[BenchmarkContextV1, ...]
      active_constituent_count: int
      breadth_above_50_fraction: float
      breadth_50_coverage_fraction: float
      breadth_above_200_fraction: float
      breadth_200_coverage_fraction: float
      median_rs_score: float
      rs_at_least_80_fraction: float
      rs_coverage_fraction: float
  ```

- Changes: `POLICY_INTERFACE_VERSION = 2`; `market: MarketContextV1` in `EntrySnapshot`, `CapacitySnapshot`, `AllocationSnapshot`, `EvictionSnapshot`, and `ExitSnapshot`; `market` in `PendingEntry`; portfolio checkpoint schema 4.

- [ ] **Step 1: Add closed nested context contracts without weakening risk validators.**

  Add a zero-allowing unit-fraction validator for breadth/coverage fields; keep `_fraction` behavior used by positive risk/stop fields unchanged. Require benchmark order `("SPY", "QQQ", "IWM")`, unique symbols, `schema_version == 1`, finite values, ISO session text, and coverage/count consistency. Remove `market_regime`, `distribution_days`, and `follow_through` only from `EntrySnapshot`.

- [ ] **Step 2: Build one trusted context per completed session.**

  Implement `build_market_context(...) -> MarketContextV1` in the new protected module. Use `close / SMA - 1`; use 21 closes for 20 close-to-close returns, sample standard deviation (`ddof=1`), and `sqrt(252)`; use full active PIT membership for breadth and RS; compute independent 50/200/RS coverage denominators; fail after warmup if any reference window or eligible cross-section is absent. Cache the result by session inside the simulator.

- [ ] **Step 3: Reorder the daily loop to respect decision time.**

  After the regime tracker consumes the just-completed benchmark bar, build the current context. Process pending next-open eviction/allocation using only `pending.market`. Process exits using the current completed-session context. Process entry qualification and capacity with that same current context, then store it in every returned `PendingEntry`.

- [ ] **Step 4: Update every snapshot builder and checkpoint round trip.**

  Thread `market` through `_entry_policy_snapshot`, `_build_entry_snapshot`, `_resolve_capacity`, `_evaluate_signals`, `_build_eviction_snapshot`, `_project_entry_transition`, allocation construction, `_build_exit_snapshot`, and `_check_exits`. Retire the `_enter_position` fallback that constructs a `PendingEntry` without explicit market context. Serialize/deserialize `PendingEntry.market` canonically and reject checkpoint schemas below 4 for interface-v2 resumes.

- [ ] **Step 5: Preserve baseline policy behavior.**

  Change `entry.py` to read regime fields from `snapshot.market`; let the baseline risk and exit policies ignore their new market field. Update existing nested-contract, pending-entry, allocation, exit, and causality fixtures in place. Do not alter trading thresholds in this migration.

- [ ] **Step 6: Verify and commit.**

  ```powershell
  python -B -m compileall -q core/strategy_policy core/backtest_engine.py
  python -B -m ruff check core/strategy_policy core/backtest_engine.py --no-cache
  python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_strategy_policy.py tests/test_backtest_engine.py tests/test_backtest_open_causality.py
  git add core/strategy_policy core/backtest_engine.py tests/test_strategy_policy.py tests/test_backtest_engine.py tests/test_backtest_open_causality.py
  git commit -m "feat: add causal adaptive market context"
  ```

  Expected: policy interface v2 round-trips exact nested values, pending decisions retain the signal-session context, and existing baseline behavior remains unchanged on equivalent fixture data.

### Task 4: Replace Fold Ranking with Continuous-Panel CAGR

**Files:**

- Modify: `core/pit_optimizer_evaluation.py`
- Modify: `core/pit_policy_parity.py`
- Modify: `core/pit_optimization.py`
- Modify: `tests/test_pit_policy_parity.py`
- Modify: `tests/test_pit_optimizer_v2.py`

**Interfaces:**

- Produces:

  ```python
  AnnualizedReturnTarget(
      metric_id="portfolio_annualized_return_pct",
      formula_id="production_equity_cagr_365_calendar_days_v1",
      basis="absolute",
      target_pct=Decimal("10.00"),
      milestones_pct=(Decimal("10.00"), Decimal("20.00"), Decimal("50.00")),
      precision_pct=Decimal("0.01"),
  )
  ```

- Produces: `PanelSecurityLineage`, `PanelStratumAllocation`, `PanelAllocationRuleV1`, `EvaluationPanelSpec`, `DiscoveryPanelPlan`, `QualificationPanelPlan`, `QualificationRetirementLedger`, `PanelAggregateSummary`, `DiscoveryPanelComparison`, and `QualificationDecision`.
- Preserves: legacy `FoldSpec`, `FoldManifest`, `DiscoveryScore`, and `HoldoutDecision` readers for schema-v3 artifacts only.

- [ ] **Step 1: Define closed panel and objective contracts.**

  `PanelSecurityLineage.security_lineage_id` is exactly the stable value derived from the price-identity `chain_id`; it also binds executable ticker history and source-affiliation labels. A panel spec binds its sorted unique lineages, continuous start/end sessions, session digest, and panel purpose. A discovery plan contains only quick/discovery panels plus the SHA-256 commitment of a separately stored qualification plan; role-visible serialization must not include qualification members.

- [ ] **Step 2: Implement deterministic current-bundle panel allocation.**

  Add `sha256_lineage_stratified_v1`: hash `partition_seed_sha256 + security_lineage_id`, sort within the sealed affiliation stratum, assign exact declared counts, and reject aliases crossing panels. `PanelAllocationRuleV1` seals total quick/discovery/qualification counts, `remainder="unallocated"`, affiliation-bitset ordering, fixed panel-allocation order `("qualification", "discovery", "quick")`, and `largest_remainder_residual_capacity_v1` apportionment.

  For each panel in that fixed order, compute quotas from each stratum's current residual capacity, floor them, distribute the remaining slots by descending fractional remainder with affiliation key as tie-break, cap every assignment by residual capacity, and decrement residual capacity before allocating the next panel. Reject total requested demand above total eligible capacity or any per-stratum sum above its eligible count. Persist `PanelStratumAllocation` with eligible, quick, discovery, qualification, and unallocated counts for every stratum. The manifest binds the rule, inputs, resulting allocations, and the proof that assigned plus unallocated equals eligible in every stratum.

  The core canary's single `sp500` stratum receives 12 quick, 48 discovery, and 24 unopened retrospective-qualification lineages; all remaining lineages are explicitly `unallocated`. Future three-universe plans reuse the same closed allocator with the seven possible S&P/Nasdaq/Russell affiliation bitsets.

  Add the provider-free `python -m core.pit_optimizer_evaluation build-panel-plans` parser branch with required bundle path/digest, prices provenance, continuous start/end dates, partition seed, exact quick/discovery/qualification counts, target, permanent qualification-retirement ledger, and one output root. `QualificationRetirementLedger` initializes one empty canonical schema-v4 ledger atomically when the requested permanent path is absent, authenticates its hash chain when present, and exposes an immutable snapshot. Under its lock, exclude every retired `security_lineage_id` before allocation and bind the ledger snapshot digest into both plans. Task 9 adds reservation/outcome mutations to this same ledger contract.

  Publish `qualification-plan.json`, `discovery-plan.json`, and `publication.json` under the output root, with `publication.json` written last as the commit marker. The command creates the missing parent and a private staging root. If publication stops after either plan file is installed, a retry may resume only after authenticating every existing partial file against the exact requested inputs and ledger snapshot; conflicting partials fail closed. A completed publication root is immutable. Contract or I/O failure returns nonzero, and search readiness refuses any root without the final marker. Print only content-free plan digests/counts. The discovery artifact contains only quick/discovery lineages plus the qualification artifact digest; the qualification artifact is never loaded by the search controller.

- [ ] **Step 3: Build continuous panel evidence from production metrics.**

  `PanelAggregateSummary` records starting/ending equity, elapsed calendar days, annualized return, total return, benchmark return, drawdown, Sharpe, closed trades, turnover, exposure, entry funnel, and exit attribution. Recompute the annualized value from endpoints/dates and reject a mismatch. Zero trades are valid when the equity curve is valid.

  Extend the existing `core.pit_policy_parity` parser/main dispatch with `capture-v4` and `verify-v4`. Both commands require the discovery-plan path, bundle path/digest, prices provenance, sandbox image, and create-only output root; create their missing parent only; refuse an existing run root; return nonzero on identity/parity/cleanup failure; and print only the final content-free attestation summary. They evaluate quick/discovery continuously and bind every output to policy interface 2; they never load the qualification artifact or call a provider.

- [ ] **Step 4: Make annualized return the entire discovery score.**

  `DiscoveryPanelComparison` stores candidate CAGR, fixed-baseline CAGR, their diagnostic delta, target gap, and whether the candidate strictly improves the current champion. Compare only the quantized candidate CAGR; retain the existing champion on a tie.

- [ ] **Step 5: Define qualification without auxiliary return gates.**

  `QualificationDecision.from_result` passes only when evaluation/integrity completed, candidate CAGR is at least the active target, and candidate CAGR is strictly above the baseline CAGR from the same qualification panel. Do not inspect trades, drawdown, Sharpe, turnover, or transaction costs in this return decision.

- [ ] **Step 6: Update existing parity/objective assertions and verify.**

  Rewrite the current fold/objective/holdout cases in place to assert the new v4 contracts while retaining explicit legacy-reader cases. Do not add test functions.

  ```powershell
  python -B -m compileall -q core/pit_optimizer_evaluation.py core/pit_policy_parity.py core/pit_optimization.py
  python -B -m ruff check core/pit_optimizer_evaluation.py core/pit_policy_parity.py core/pit_optimization.py --no-cache
  python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_policy_parity.py tests/test_pit_optimizer_v2.py -k "annualized or panel or objective or qualification or legacy"
  git add core/pit_optimizer_evaluation.py core/pit_policy_parity.py core/pit_optimization.py tests/test_pit_policy_parity.py tests/test_pit_optimizer_v2.py
  git commit -m "feat: rank optimizer panels by portfolio CAGR"
  ```

### Task 5: Add Schema-v4 Parent and Atomic Author Contracts

**Files:**

- Modify: `core/pit_optimization_contract.py`
- Modify: `core/pit_optimizer_evaluation.py`
- Modify: `tests/test_pit_optimizer_v2.py`
- Modify: `tests/test_pit_optimizer_loop.py`

**Interfaces:**

- Produces: `PolicyAuthoringScopeV4`, `SelectedParentIdentity`, `SelectedParentSummary`, `AuthorSourceFile`, `AuthorArtifactV4`, `RoleOutputInvalidSummary`, `InvestigatorInputV4`, `AuthorInputV4`, `CriticInputV4`, and `PitOptimizerRunManifest(schema_version=4)`.
- Changes critic dispositions to exactly `promote`, `refine`, and `abandon`.

- [ ] **Step 1: Add a schema-v4 manifest beside the v3 loader.**

  Bind campaign ID/sequence, clean source identity, policy interface 2, exact bundle/discovery-plan identities, qualification commitment, `AnnualizedReturnTarget`, optional seed checkpoint identity, exact ordered editable paths, sandbox identity, `apply=false`, `provider_retries=0`, and one call budget per planned role. Keep exactly three calls per complete iteration but allow finite run lengths beyond two iterations.

- [ ] **Step 2: Replace patch bounds with source-context sizing.**

  `PolicyAuthoringScopeV4` contains the exact three initial file hashes, actual canonical source-bundle byte count, finite iteration/history input byte caps, finite response/token caps from each call plan, and descendant rule `authenticated_parent_plus_atomic_full_sources`. It contains no file-count choice, hunk count, changed-line count, diff-byte limit, or USD ceiling.

- [ ] **Step 3: Make parent selection explicit in every role input.**

  Baseline, champion, and branch each receive a canonical `SelectedParentIdentity`; baseline is not represented by an unbound null. Investigator and author inputs include the same selected-parent identity/source digest. Investigator returns one to three canonical focus areas (`entry`, `risk_sizing`, `exit`) rather than one controller-enforced family/file.

- [ ] **Step 4: Define one closed three-file author response.**

  The response contains `hypothesis_id`, `parent_identity_sha256`, `behavioral_summary`, a `policy_sources` object with exactly the three required repository-relative keys, `assumptions`, and `validation_suggestions`. Require valid UTF-8 text, LF only, no NUL/CR, final newline, unchanged files included, and at least one changed file. Parse all sources before exposing any candidate workspace.

- [ ] **Step 5: Redesign comparison and failure feedback.**

  Replace fold arrays in v4 feedback with quick/discovery `PanelAggregateSummary` values, target progress, baseline/champion diagnostics, compact prior hypotheses, and validation status. `CriticInputV4` accepts exactly one of a valid author manifest or a content-free `RoleOutputInvalidSummary` for an invalid author.

- [ ] **Step 6: Update prompts and response schemas.**

  Tell investigator to diagnose O'Neil behavior and propose a focused mechanism; tell author to implement the plan across any/all three complete files; tell critic to explain measured CAGR behavior and choose promote/refine/abandon. Keep local closed parsing authoritative and size response bytes from the real source bundle plus declared output headroom.

- [ ] **Step 7: Update existing contract/role fixtures and verify.**

  ```powershell
  python -B -m compileall -q core/pit_optimization_contract.py core/pit_optimizer_evaluation.py
  python -B -m ruff check core/pit_optimization_contract.py core/pit_optimizer_evaluation.py --no-cache
  python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_v2.py tests/test_pit_optimizer_loop.py -k "manifest or role or source or response or context"
  git add core/pit_optimization_contract.py core/pit_optimizer_evaluation.py tests/test_pit_optimizer_v2.py tests/test_pit_optimizer_loop.py
  git commit -m "feat: add atomic schema v4 optimizer roles"
  ```

### Task 6: Materialize All Three Policy Sources as One Candidate

**Files:**

- Modify: `core/pit_optimizer_candidate.py`
- Modify: `core/pit_optimization_contract.py`
- Modify: `tests/test_pit_optimizer_v2.py`
- Modify: `tests/test_pit_optimizer_loop.py`

**Interfaces:**

- Produces:

  ```python
  def validate_candidate_sources(
      *,
      authenticated_base_root: Path,
      candidate_root: Path,
      replacement_sources: Mapping[str, str],
      git: object,
      source_commit: str,
      policy_interface_version: int,
      immutable_constraints_sha256: str,
      discovery_panel_plan_sha256: str,
      parent_identity_sha256: str,
  ) -> tuple[CandidateIdentity, str]:
      ...
  ```

- [ ] **Step 1: Retain the AST/no-I/O validator and extend only its typed-contract allowlist.**

  Permit imports of `MarketContextV1`/`BenchmarkContextV1` through the established contract module. Do not permit new filesystem, network, process, time, randomness, reflection, dynamic import, mutation, or unordered-set behavior.

- [ ] **Step 2: Validate the full replacement map before writing.**

  Require the exact canonical key set, validate text encoding/line endings/final newline, parse/compile each file, snapshot all prior bytes, and reject a no-op bundle. No candidate worker or evaluator may see partially replaced files.

- [ ] **Step 3: Publish the candidate only after all checks pass.**

  Write only inside the disposable candidate checkout, validate all three ASTs and declared exports, run `git diff --check`, reject file-mode/out-of-scope changes, derive the cumulative diff locally, and build identities from all three file hashes, parent identity, source commit, interface, immutable constraints, and discovery panel plan. Restore all original bytes on every failure.

- [ ] **Step 4: Keep old diff ingestion only for old artifact authentication.**

  Remove `materialize_author_candidate_diff`, patch reanchoring, `PatchBounds`, and diff-size enforcement from the v4 call path. Do not delete helpers still needed to read/reconstruct schema-v3 evidence.

- [ ] **Step 5: Rewrite existing candidate cases in place and verify.**

  Convert current full-source/diff-bound tests to exact three-path atomicity, parent binding, rollback, local diff derivation, and no-I/O assertions. Do not add test functions.

  ```powershell
  python -B -m compileall -q core/pit_optimizer_candidate.py core/pit_optimization_contract.py
  python -B -m ruff check core/pit_optimizer_candidate.py core/pit_optimization_contract.py --no-cache
  python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_v2.py tests/test_pit_optimizer_loop.py -k "candidate or author or atomic or rollback or ast"
  git add core/pit_optimizer_candidate.py core/pit_optimization_contract.py tests/test_pit_optimizer_v2.py tests/test_pit_optimizer_loop.py
  git commit -m "feat: validate atomic three-file strategy candidates"
  ```

### Task 7: Make Accounted Role-Output Failures Recoverable

**Files:**

- Modify: `core/pit_optimizer_authorization.py`
- Modify: `core/pit_optimization_contract.py`
- Modify: `core/pit_optimizer_artifacts.py`
- Modify: `agent_loop.py`
- Modify: `tests/test_pit_optimizer_v2.py`
- Modify: `tests/test_pit_optimizer_loop.py`
- Modify: `tests/test_agent_loop.py`

**Interfaces:**

- Produces: `PitOptimizerRoleAttempt`; `AuthorizationPlanSkip`; `AuthorizationLedger.skip_unstarted_plans(...)`; content-free invalid/skip artifacts.

- [ ] **Step 1: Represent an accounted invalid response without calling it accepted.**

  `PitOptimizerRoleAttempt` contains the plan, complete provider facts, and an optional parsed payload. The recoverable variant requires response received, `outcome == "schema_invalid"`, an allowed response-validation code, complete authoritative usage/cost accounting, and `payload is None`. Transport or incomplete/uncertain accounting remains terminal.

- [ ] **Step 2: Add hash-chained settlement for unused plan slots.**

  `AuthorizationPlanSkip` binds call index, iteration, role, triggering invalid call index, reason `predecessor_role_output_invalid`, prior ledger digest, and its own digest. `skip_unstarted_plans` may settle only contiguous, unreserved roles later in the same iteration. It creates no provider reservation and no call/token usage.

- [ ] **Step 3: Advance by settled plan index, not accepted-call count.**

  Change reservation, controller-input binding, lease completion, recovery, and audit verification so every earlier plan is covered by one fully reconciled attempt or one authenticated skip. Schema-invalid attempts count as calls. Skips count as zero calls. Never fabricate an accepted receipt or role artifact.

- [ ] **Step 4: Preserve one-shot gateway behavior.**

  Keep `request_pit_optimizer_once` at zero retries. After durable schema-invalid finalization, surface the content-free facts so the controller can construct `PitOptimizerRoleAttempt`. Provider content is neither retried nor copied into a failure summary.

- [ ] **Step 5: Rewrite existing malformed-output/accounting cases.**

  Update current invalid response, receipt lineage, lease completion, and two-iteration cases in place to prove: invalid investigator skips author/critic and reaches the next investigator; invalid author still reaches critic; invalid critic defaults locally; transport/accounting failures stop. Do not add test functions.

- [ ] **Step 6: Verify and commit.**

  ```powershell
  python -B -m compileall -q core/pit_optimizer_authorization.py core/pit_optimization_contract.py core/pit_optimizer_artifacts.py agent_loop.py
  python -B -m ruff check core/pit_optimizer_authorization.py core/pit_optimization_contract.py core/pit_optimizer_artifacts.py agent_loop.py --no-cache
  python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_v2.py tests/test_pit_optimizer_loop.py tests/test_agent_loop.py -k "schema_invalid or malformed or skip or receipt or accounting or retry"
  git add core/pit_optimizer_authorization.py core/pit_optimization_contract.py core/pit_optimizer_artifacts.py agent_loop.py tests/test_pit_optimizer_v2.py tests/test_pit_optimizer_loop.py tests/test_agent_loop.py
  git commit -m "fix: recover from accounted optimizer role output"
  ```

### Task 8: Persist Champion, Exploratory Branch, and Cross-Run Checkpoint

**Files:**

- Modify: `core/pit_optimizer_artifacts.py`
- Modify: `core/pit_optimizer_controller.py`
- Modify: `core/pit_optimization.py`
- Modify: `agent_loop.py`
- Modify: `tests/test_pit_optimizer_loop.py`
- Modify: `tests/test_agent_loop.py`

**Interfaces:**

- Produces: `SearchCandidateState`, `CampaignCheckpoint`, `prepare_pit_optimizer_v4`, `run_pit_optimizer_v4`, `--campaign-checkpoint`, and `--campaign-checkpoint-sha256`.

- [ ] **Step 1: Replace single-incumbent state with explicit search states.**

  `SearchCandidateState` binds candidate identity, cumulative-diff artifact, source-bundle artifact, discovery evidence, hypothesis, behavioral summary, originating run/iteration, and all referenced digests. `_RunState` owns baseline quick/discovery evidence, optional champion, optional active branch, selected parent, call attempts, skips, and feedback tail.

- [ ] **Step 2: Select one parent deterministically before each iteration.**

  Use active branch after the previous effective `refine`; otherwise champion; otherwise baseline. Reconstruct that parent's cumulative diff into a fresh disposable checkout and authenticate its three-file source bundle before building any role input.

- [ ] **Step 3: Recompute baseline on the exact panels.**

  Replace parity-derived baseline performance with `evaluate_baseline(panel)`. Recompute and persist quick/discovery baseline evidence for the exact bundle, continuous dates, symbols, engine policy, and panel identities before the first role call. Parity remains an engine-equivalence attestation only.

- [ ] **Step 4: Evaluate quick then discovery without a return gate.**

  Persist quick evidence for feedback. If the candidate executes validly, always run the discovery panel even for zero trades or low return. Candidate syntax/typed-decision/runtime failures become validation feedback and leave the existing champion intact.

- [ ] **Step 5: Apply the exact champion/branch transition table.**

  A strictly higher discovery CAGR becomes champion and clears the branch regardless of critic output. A valid nonwinner becomes the active branch only after effective `refine`. `abandon` clears it. A nonwinning `promote` is discarded. A CAGR tie retains the existing champion. Invalid critic output uses `abandon` but cannot undo a metric-owned champion promotion. Invalid author reaches critic with validation failure. Invalid investigator records unchanged state, settles its two skips, and advances.

  When candidate validation or execution fails, still call the planned critic with the closed validation summary unless the evaluator itself is unavailable. Effective `refine` retains the already selected active branch when that parent was a branch; it cannot turn an invalid child into a branch. Effective `abandon`, nonwinning `promote`, or invalid critic clears the branch.

- [ ] **Step 6: Commit durable artifacts before mutating live state.**

  Persist candidate source/diff/evaluation, critic or content-free critic failure, and a complete prospective `decision.json`; atomically replace root `checkpoint.json`; then mutate `_RunState`; then dispose candidate/controller/worker resources. Persist create-only seed copies for imported champion/branch sources and diffs.

- [ ] **Step 7: Authenticate cross-run resume.**

  A later bounded run accepts an explicit checkpoint path plus digest, reauthenticates clean current source, reconstructs each retained diff, remints current candidate identities, and reevaluates old candidates under a changed bundle/panel plan. Old fold scores never seed a v4 champion directly.

- [ ] **Step 8: Compose schema-v4 production services and summaries.**

  Add v4 parser/config/readiness/live-run/summary paths in `agent_loop.py`. Index attempts by plan index, not accepted-call list position. Public summary reports target, baseline/champion/branch CAGR, iterations, calls/tokens/cost, terminal reason, checkpoint presence, `apply=false`, and cleanup; omit raw hashes/content/secrets.

- [ ] **Step 9: Rewrite existing two-iteration/durability cases and verify.**

  Update current production-composition, incumbent, multi-iteration, decision-durability, and cleanup cases in place. Do not add test functions.

  ```powershell
  python -B -m compileall -q core/pit_optimizer_artifacts.py core/pit_optimizer_controller.py core/pit_optimization.py agent_loop.py
  python -B -m ruff check core/pit_optimizer_artifacts.py core/pit_optimizer_controller.py core/pit_optimization.py agent_loop.py --no-cache
  python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_loop.py tests/test_agent_loop.py -k "iteration or champion or branch or checkpoint or decision or cleanup or summary"
  git add core/pit_optimizer_artifacts.py core/pit_optimizer_controller.py core/pit_optimization.py agent_loop.py tests/test_pit_optimizer_loop.py tests/test_agent_loop.py
  git commit -m "feat: persist adaptive optimizer search state"
  ```

### Task 9: Convert Hidden Holdout into One-Use Retrospective Qualification

**Files:**

- Modify: `core/pit_optimizer_evaluation.py`
- Modify: `core/pit_optimizer_holdout.py`
- Modify: `pit_optimizer_holdout.py`
- Modify: `core/pit_optimizer_artifacts.py`
- Modify: `core/pit_optimization.py`
- Modify: `agent_loop.py`
- Modify: `tests/test_pit_optimizer_holdout.py`
- Modify: `tests/test_pit_optimizer_v2.py`
- Modify: `tests/test_agent_loop.py`

**Interfaces:**

- Produces: `QualificationPanelIdentity`; qualification-only reservation/outcome records; `preflight-qualification` and `execute-qualification` CLI actions; `full_replay_ready` and `full_replay_started: false` summary fields.

- [ ] **Step 1: Restrict one-use consumption to qualification.**

  Bind the consumption identity to bundle, qualification-plan, panel, lineage set, sessions, warmup contract, engine policy, and target. Discovery panels remain reusable within their campaign. Reserve the qualification identity before baseline evaluation and record a terminal outcome even if evaluation fails, so every attempted qualification panel is retired.

  The permanent ledger stores the sorted stable lineage set plus a `qualification_retirement_domain_id` derived only from the canonical security-lineage namespace/provenance—not from bundle digest, panel ID, partition seed, or evaluation dates. `build-panel-plans` must exclude all retired lineages from later quick, discovery, and qualification allocation.

  Add required `--qualification-ledger` and `--qualification-ledger-snapshot-sha256` fields to the schema-v4 prepare/canary gate config in `core/pit_optimization.py` and `agent_loop.py`. `prepare_pit_optimizer_v4` authenticates the current ledger hash chain, proves the discovery plan's sealed snapshot digest is an ancestor of the current append-only ledger, and rechecks that no current retired lineage intersects quick, discovery, or committed qualification sets before opening provider capability. Record the current ledger digest in readiness without requiring it to equal an older snapshot after unrelated retirements. Thus changing the bundle, window, panel ID, seed, or ledger path cannot expose a consumed qualification lineage to a later model role or qualification.

- [ ] **Step 2: Load only a frozen authenticated champion.**

  Replace summary-inferred incumbent loading with checkpoint authentication, referenced champion diff/source authentication, clean-source verification, and disposable reconstruction. Refuse an active branch as the qualification candidate.

- [ ] **Step 3: Recompute same-panel baseline and candidate continuously.**

  Run both policies on the exact qualification panel/date range and derive `QualificationDecision` from absolute CAGR plus strict same-panel baseline improvement. Never call OpenRouter and never write qualification results into a later role checkpoint.

- [ ] **Step 4: Gate the label, not the optimizer mechanics, on universe coverage.**

  The qualifier may produce a local retrospective result for an S&P-only architecture check, but `full_replay_ready` must remain false unless the plan's authenticated coverage scope is the approved S&P 500/Nasdaq-100/Russell 2000 union. This task does not create that expanded bundle.

- [ ] **Step 5: Stop before full replay and clean resources.**

  Persist baseline, candidate, decision, one-use ledger outcome, cleanup, and content-free summary. The CLI must not import or dispatch the replay entry point.

- [ ] **Step 6: Rewrite existing holdout cases and verify.**

  Update the current loader, preflight/execute, one-use, decision, and cleanup cases in place. Do not add test functions.

  ```powershell
  python -B -m compileall -q core/pit_optimizer_evaluation.py core/pit_optimizer_holdout.py core/pit_optimizer_artifacts.py core/pit_optimization.py pit_optimizer_holdout.py agent_loop.py
  python -B -m ruff check core/pit_optimizer_evaluation.py core/pit_optimizer_holdout.py core/pit_optimizer_artifacts.py core/pit_optimization.py pit_optimizer_holdout.py agent_loop.py --no-cache
  python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_holdout.py tests/test_pit_optimizer_v2.py tests/test_agent_loop.py -k "qualification or holdout or validation or retirement or cleanup"
  git add core/pit_optimizer_evaluation.py core/pit_optimizer_holdout.py core/pit_optimizer_artifacts.py core/pit_optimization.py pit_optimizer_holdout.py agent_loop.py tests/test_pit_optimizer_holdout.py tests/test_pit_optimizer_v2.py tests/test_agent_loop.py
  git commit -m "feat: add one-use annualized qualification"
  ```

### Task 10: Regenerate the Local S&P Canary Bundle and Prove Baseline Parity

**Files:**

- Local ignored artifacts only under the fresh `$pitCanaryRoot` created in Step 1.
- Verify tracked code from Tasks 1-9; do not modify strategy behavior in this task.

**Inputs:**

- Pinned S&P revision URL from `docs/pit-baseline-data-provenance.md`.
- Local cache `.artifacts/cache/backtest/historical_data.sqlite3`.
- Existing authenticated SEC files under `.artifacts/task-4-regeneration-20260823T223000Z/sec-pit/`.
- Existing reviewed identity maps in `config/pit_membership_symbol_map.csv` and `config/pit_price_identity_map.csv`.
- Sandbox image `localhost/rs-agent-loop@sha256:7ecfb4ebb3b327940bef347e4c82e82fb4a0e8b40fc63b92b2536fe8c83acf1c`.

- [ ] **Step 1: Create a fresh no-clobber artifact root and reacquire the pinned membership provenance.**

  ```powershell
  $pitCanaryHead = (git rev-parse HEAD).Trim()
  $pitCanaryRoot = Join-Path (Resolve-Path .artifacts) "adaptive-oneil-core-canary-$($pitCanaryHead.Substring(0, 12))"
  New-Item -ItemType Directory -Path $pitCanaryRoot
  python -B fetch_sp500_membership.py --revision-url 'https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid=1347775889' --start-date 2021-01-01 --end-date 2025-12-31 --symbol-map-csv config/pit_membership_symbol_map.csv --output-dir (Join-Path $pitCanaryRoot 'membership')
  ```

  Expected: a fresh local membership CSV/provenance pair bound to revision 1347775889. Do not reuse missing provenance by digest alone.

- [ ] **Step 2: Export membership prices plus SPY/QQQ/IWM.**

  The inspected local cache does not currently contain QQQ/IWM in its sealed request keys, so use the existing data-only Alpaca SIP backfill path for missing reference rows; it must not call trading/account endpoints.

  ```powershell
  $pitCanaryHead = (git rev-parse HEAD).Trim()
  $pitCanaryRoot = Join-Path (Resolve-Path .artifacts) "adaptive-oneil-core-canary-$($pitCanaryHead.Substring(0, 12))"
  $pitCache = (Resolve-Path '.artifacts/cache/backtest/historical_data.sqlite3').Path
  $pitCacheSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $pitCache).Hash.ToLowerInvariant()
  python -B export_pit_prices.py --cache $pitCache --cache-sha256 $pitCacheSha --membership-csv (Join-Path $pitCanaryRoot 'membership/membership.csv') --symbol-history-map config/pit_membership_symbol_map.csv --symbol-history-map-sha256 0c1187dccb6414fd84bb99a4c581250f2e3155f422f629088d5c720e89ea7483 --price-identity-map config/pit_price_identity_map.csv --price-identity-map-sha256 2dbf5357a98d2deca9a08b27fbbb7de01f4294d987e19065d7660266e1aeada3 --start-date 2020-01-01 --end-date 2025-12-31 --sandbox-image 'localhost/rs-agent-loop@sha256:7ecfb4ebb3b327940bef347e4c82e82fb4a0e8b40fc63b92b2536fe8c83acf1c' --alpaca-sip-backfill --alpaca-env-file .env --output-dir (Join-Path $pitCanaryRoot 'prices')
  ```

  Expected: exact reference set, complete common reference calendar, complete price-identity provenance, and no secret in stdout/artifacts.

- [ ] **Step 3: Build and verify the bundle with unchanged fundamentals.**

  ```powershell
  $pitCanaryHead = (git rev-parse HEAD).Trim()
  $pitCanaryRoot = Join-Path (Resolve-Path .artifacts) "adaptive-oneil-core-canary-$($pitCanaryHead.Substring(0, 12))"
  python -B build_pit_bundle.py --membership-csv (Join-Path $pitCanaryRoot 'membership/membership.csv') --prices-csv (Join-Path $pitCanaryRoot 'prices/prices.csv') --fundamentals-csv .artifacts/task-4-regeneration-20260823T223000Z/sec-pit/fundamentals.csv --data-cutoff 2025-12-31 --evaluation-start 2021-01-01 --warmup-start 2020-01-01 --membership-provenance (Join-Path $pitCanaryRoot 'membership/membership_provenance.json') --prices-provenance (Join-Path $pitCanaryRoot 'prices/prices_provenance.json') --fundamentals-provenance .artifacts/task-4-regeneration-20260823T223000Z/sec-pit/fundamentals_provenance.json --output (Join-Path $pitCanaryRoot 'bundle/pit_canary.sqlite3') --manifest-output (Join-Path $pitCanaryRoot 'bundle/bundle_manifest.json')
  $pitBundleSha = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $pitCanaryRoot 'bundle/pit_canary.sqlite3')).Hash.ToLowerInvariant()
  python -B verify_pit_bundle.py --bundle (Join-Path $pitCanaryRoot 'bundle/pit_canary.sqlite3') --sha256 $pitBundleSha --manifest (Join-Path $pitCanaryRoot 'bundle/bundle_manifest.json') --membership-csv (Join-Path $pitCanaryRoot 'membership/membership.csv') --prices-csv (Join-Path $pitCanaryRoot 'prices/prices.csv') --fundamentals-csv .artifacts/task-4-regeneration-20260823T223000Z/sec-pit/fundamentals.csv --membership-provenance (Join-Path $pitCanaryRoot 'membership/membership_provenance.json') --prices-provenance (Join-Path $pitCanaryRoot 'prices/prices_provenance.json') --fundamentals-provenance .artifacts/task-4-regeneration-20260823T223000Z/sec-pit/fundamentals_provenance.json
  ```

- [ ] **Step 4: Generate the 12/48 sealed discovery plan and fresh baseline/parity evidence.**

  Use the schema-v4 `build-panel-plans` command implemented in Task 4 with seed label `adaptive-oneil-core-canary-20260831`, dates 2021-01-04 through 2025-12-31, quick count 12, discovery count 48, qualification commitment count 24, and target 10.00. Run the unchanged baseline through the quick/discovery plans, capture an interface-v2 parity reference, repeat it, and require byte-identical policy decisions/trades/equity on the same tradable panels.

  ```powershell
  $pitCanaryHead = (git rev-parse HEAD).Trim()
  $pitCanaryRoot = Join-Path (Resolve-Path .artifacts) "adaptive-oneil-core-canary-$($pitCanaryHead.Substring(0, 12))"
  $pitBundleSha = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $pitCanaryRoot 'bundle/pit_canary.sqlite3')).Hash.ToLowerInvariant()
  python -B -m core.pit_optimizer_evaluation build-panel-plans --pit-bundle (Join-Path $pitCanaryRoot 'bundle/pit_canary.sqlite3') --pit-bundle-sha256 $pitBundleSha --prices-provenance (Join-Path $pitCanaryRoot 'prices/prices_provenance.json') --start-date 2021-01-04 --end-date 2025-12-31 --partition-seed adaptive-oneil-core-canary-20260831 --quick-count 12 --discovery-count 48 --qualification-count 24 --target-pct 10.00 --qualification-ledger .artifacts/pit-optimizer-qualification-ledger-v4.json --output-root (Join-Path $pitCanaryRoot 'panels')
  python -B -m core.pit_policy_parity capture-v4 --discovery-panel-plan (Join-Path $pitCanaryRoot 'panels/discovery-plan.json') --pit-bundle (Join-Path $pitCanaryRoot 'bundle/pit_canary.sqlite3') --pit-bundle-sha256 $pitBundleSha --prices-provenance (Join-Path $pitCanaryRoot 'prices/prices_provenance.json') --sandbox-image 'localhost/rs-agent-loop@sha256:7ecfb4ebb3b327940bef347e4c82e82fb4a0e8b40fc63b92b2536fe8c83acf1c' --output-root (Join-Path $pitCanaryRoot 'parity/reference')
  python -B -m core.pit_policy_parity verify-v4 --reference (Join-Path $pitCanaryRoot 'parity/reference/parity-reference.json') --discovery-panel-plan (Join-Path $pitCanaryRoot 'panels/discovery-plan.json') --pit-bundle (Join-Path $pitCanaryRoot 'bundle/pit_canary.sqlite3') --pit-bundle-sha256 $pitBundleSha --prices-provenance (Join-Path $pitCanaryRoot 'prices/prices_provenance.json') --sandbox-image 'localhost/rs-agent-loop@sha256:7ecfb4ebb3b327940bef347e4c82e82fb4a0e8b40fc63b92b2536fe8c83acf1c' --output-root (Join-Path $pitCanaryRoot 'parity/verification')
  ```

  Expected: the baseline is recomputed on each exact panel, interface-v2 parity passes, market context is complete on every evaluation session, and no OpenRouter call occurs.

- [ ] **Step 5: Run the full focused provider-free verification once.**

  ```powershell
  python -B -m compileall -q core agent_loop.py export_pit_prices.py build_pit_bundle.py verify_pit_bundle.py pit_baseline.py pit_optimizer_holdout.py
  python -B -m ruff check core agent_loop.py export_pit_prices.py build_pit_bundle.py verify_pit_bundle.py pit_baseline.py pit_optimizer_holdout.py --no-cache
  python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_data.py tests/test_export_pit_prices.py tests/test_strategy_policy.py tests/test_backtest_engine.py tests/test_backtest_open_causality.py tests/test_pit_policy_parity.py tests/test_pit_optimizer_v2.py tests/test_pit_optimizer_loop.py tests/test_pit_optimizer_holdout.py tests/test_pit_optimizer_subset_reference.py tests/test_agent_loop.py
  git diff --check
  $pitDirty = @(git status --porcelain=v1 --untracked-files=all)
  if ($pitDirty.Count -ne 0) { throw 'tracked implementation checkout is not clean' }
  $pitPythonCount = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python' -and $_.CommandLine -match 'pit_optimizer|pit-policy|full[-_ ]replay' }).Count
  if ($pitPythonCount -ne 0) { throw "PIT optimizer/replay Python processes remain: $pitPythonCount" }
  $pitContainerNames = @(& docker ps -a --filter 'name=pit-policy-' --format '{{.Names}}') + @(& docker ps -a --filter 'name=agent-loop-' --format '{{.Names}}')
  if (@($pitContainerNames | Where-Object { $_ }).Count -ne 0) { throw 'PIT worker containers remain after provider-free verification' }
  ```

  Expected: all focused checks pass, tracked source remains clean after committed implementation, no PIT optimizer/replay process remains, and no optimizer container remains.

### Task 11: Run the Authorized Six-Call Core Canary and Stop

**Files:**

- Local ignored artifacts only under: `$pitCanaryRoot\optimizer\` from Task 10.
- No tracked source modifications.

**Run contract:**

- Exactly two planned iterations and six planned roles in investigator/author/critic order.
- DeepSeek R1 for all roles.
- Maximum 448,000 aggregate input-plus-output tokens for this canary.
- No enforcing USD ceiling; record actual provider cost.
- `apply=false`; `provider_retries=0`; exact three editable policy files.
- Quick panel 12 lineages; discovery panel 48 lineages; qualification remains sealed and unopened.
- Full replay remains off.

- [ ] **Step 1: Build schema-v4 readiness and a six-call manifest provider-free.**

  Recompute `$pitCanaryRoot` from clean HEAD with the Task 10 formula. Authenticate clean HEAD, bundle, prices provenance, discovery plan, qualification commitment, parity evidence, policy interface, sandbox image, and artifact roots. Size each role's dynamic/response byte budget from the rendered actual three-file source context plus declared reasoning/output token headroom. Render the exact prepare and canary commands; verify neither command contains credentials or an USD limit.

- [ ] **Step 2: Open one finite local authorization window.**

  Bind the exact manifest/source-scope digest, six calls, 448,000 tokens, two iterations, `apply=false`, zero retries, and no USD authority field. The user's standing authorization covers these OpenRouter calls; do not pause for another authorization prompt unless the rendered scope differs from this task.

- [ ] **Step 3: Execute the emitted canary command once.**

  Do not manually reconstruct the command. Run the exact command emitted by readiness so source, panel, call-plan, authorization, and artifact identities remain bound. Do not restart the canary after a terminal transport/accounting/identity/sandbox failure.

- [ ] **Step 4: Audit the complete feedback path.**

  Require evidence that:

  - investigator received the controller-selected parent and discovery-only feedback;
  - author returned an exact three-file bundle bound to that parent;
  - the controller validated an atomic candidate and derived its diff locally;
  - every runtime-valid candidate reached discovery regardless of quick-panel return;
  - critic received actual validation/performance feedback;
  - iteration 2 received iteration 1's critic feedback and selected parent;
  - the highest discovery CAGR is the champion and any branch matches the deterministic transition table;
  - checkpoint, provider accounting, attempt/skip records, cleanup, and source-cleanliness identities reconcile.

- [ ] **Step 5: Stop before qualification and full replay.**

  Preserve the local checkpoint/champion/branch artifacts. Repeat the exact `git status --porcelain`, Python-process count, and name-only Docker checks from Task 10 Step 5 after the run finalizer. Report baseline and best discovery annualized return, improvement in percentage points, target gap to 10%, role calls/tokens/actual cost, recoverable invalid outcomes/skips, whether the branch persists, and cleanup status. Explicitly report `qualification_started=false`, `full_replay_started=false`, and that S&P-only canary performance is architecture evidence rather than final three-universe qualification.

## Completion Criteria

Implementation is ready for the separate three-universe data/qualification phase only when all of the following are true:

- the regenerated local bundle authenticates SPY, QQQ, and IWM as complete non-tradable references;
- every policy decision receives causal `MarketContextV1` with baseline parity preserved;
- schema-v4 authors can change any/all three policy files atomically without patch-size restrictions;
- a valid candidate travels through quick evaluation, continuous discovery evaluation, critic feedback, and a later iteration;
- fully accounted schema-invalid role output can advance without retry or ledger corruption;
- champion and optional branch survive a clean cross-run checkpoint reconstruction;
- discovery ranking uses only absolute annualized portfolio return;
- all provider attempts and skips reconcile, disposable resources are cleaned, and tracked source is clean;
- the canary stops with qualification unopened and the full replay off.

After this plan succeeds, write the separate `three-universe-pit-qualification` plan around exact licensed/authenticated Nasdaq-100 and Russell 2000 historical constituent sources, then run a new campaign and one-use qualification toward the 10% target. The same core loop and metric then support later 20% and 50% target campaigns without another optimizer redesign.
