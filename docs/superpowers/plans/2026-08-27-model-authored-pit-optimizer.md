# Model-Authored PIT Optimizer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the one-catalog-candidate PIT canary with a bounded, evidence-guided DeepSeek R1 loop that can author pure strategy-policy code, learn from replay feedback, and emit only an inert local patch.

**Architecture:** Extract entry, risk/sizing, eviction, and exit decisions behind immutable scalar snapshot contracts while the trusted simulator retains fact construction, time, fills, accounting, and safety checks. A schema-v2 controller runs investigator, author, and critic once per complete iteration; applies a bounded policy-only diff to a disposable incumbent; evaluates independent discovery folds; selects incumbents with a quantized lexicographic objective; and opens one consumed hidden fold only after discovery freezes. Candidate code runs in a separate policy-only, network-disabled worker that receives no repository, PIT bundle, fold identity, credentials, or artifact mounts.

**Tech Stack:** Python 3.13, frozen dataclasses, Decimal, AST, hashlib/json/pathlib/subprocess, pandas, SQLite PIT bundle, the existing PortfolioSimulator, Docker CLI isolation, OpenRouter through the existing gateway, pytest, and the existing Git/audit/candidate-export infrastructure

**Spec:** docs/superpowers/specs/2026-08-27-model-authored-pit-optimizer-design.md

## Global Constraints

- Keep all repository changes and artifacts local. Do not push, upload, merge, deploy, or create a cloud task.
- Do not make a paid provider call while implementing or verifying this plan.
- The latest reconciled provider pool has only one unused call. A live feedback canary remains blocked until the operator gives a fresh authorization covering at least six calls, the exact PolicySourceScope digest, a token ceiling, and a USD ceiling.
- Keep provider retries at zero. The schema-v2 optimizer must not enable the response-healing plugin or any hidden provider retry.
- During an authorized canary, freeze one current DeepSeek R1 pricing record before opening the run lease and prove every rendered call's conservative worst-case cost is within its sealed per-call USD cap before transmission.
- Preserve schema-v1 PIT optimizer readers and existing artifacts. New runs use schema version 2 and never reinterpret v1 records.
- Candidate-editable source is limited to core/strategy_policy/entry.py, core/strategy_policy/risk.py, and core/strategy_policy/exit.py.
- Keep core/canslim fact construction, PortfolioSimulator execution/accounting, folds, metrics, objective, provider client, audit, sandbox, and promotion logic protected.
- Preserve the schema-v1 effective-engine-policy value and digest for the extracted baseline. Structural policy changes are attested separately with CandidateIdentity.
- Preserve the current uncapped backtest default max_positions=None. Do not introduce a five-position invariant.
- Keep testing focused: one small policy-contract file, existing behavior regressions, one optimizer-contract file, and one mocked two-iteration integration. Do not run the full repository suite per candidate.
- Real provider-free subset replay is limited to two checkpoints: one clean committed inline-policy reference capture after the fold-boundary change, and one clean final-HEAD policy verification after all implementation commits. Readiness reuses the final attested discovery outputs instead of replaying them again. It must not run the long/full replay until a subset winner later becomes long_replay_eligible and the operator separately approves it.

---

## File Structure and Responsibility Map

### New trusted policy boundary

- core/strategy_policy/__init__.py
  - Exports POLICY_INTERFACE_VERSION, frozen snapshot/decision types, and the trusted client protocol.
- core/strategy_policy/contracts.py
  - Owns immutable primitive-only EntrySnapshot, CapacitySnapshot, AllocationSnapshot, EvictionSnapshot, ExitSnapshot, their closed decisions, canonical JSON conversion, and engine-side output validation.
- core/strategy_policy/runtime.py
  - Owns InProcessPolicyClient and JsonLinePolicyClient. It is protected and never candidate-editable.
- core/strategy_policy/worker.py
  - Owns the closed JSON-lines policy-worker entrypoint and dispatch. It is protected and receives only candidate policy modules plus contracts.
- core/strategy_policy/entry.py
  - Candidate-editable pure evaluate_entry(snapshot) implementation matching the current entry and ranking behavior by default.
- core/strategy_policy/risk.py
  - Candidate-editable pure recommend_capacity(snapshot), recommend_allocation(snapshot), and select_eviction(snapshot) implementations matching current behavior by default.
- core/strategy_policy/exit.py
  - Candidate-editable pure evaluate_exit(snapshot) implementation matching current exit sequencing by default.

### New optimizer-v2 components

- core/pit_policy_parity.py
  - Captures and verifies canonical provider-free fold outputs before and after policy extraction.
- core/pit_optimizer_candidate.py
  - Owns allowlisted source packaging, strict incremental/cumulative diff validation, AST purity validation, changed-symbol derivation, and CandidateIdentity. It imports the one shared PatchBounds contract from core/pit_optimization_contract.py.
- core/pit_optimizer_evaluation.py
  - Owns FoldSpec/FoldManifest, independent fold evaluation, aggregate metrics, Decimal objective ordering, holdout eligibility, and ValidationLedger.
- core/pit_optimizer_authorization.py
  - Owns append-only operator grants, named authorization windows, atomic run reservations, and reconciliation against prior provider usage.
- core/pit_optimizer_artifacts.py
  - Owns the atomic local run tree and create-only canonical schema-v2 artifacts.
- core/pit_optimizer_controller.py
  - Owns readiness, incumbent state, iteration state machine, stop conditions, final deterministic repeat, hidden validation, cleanup, and terminal result.

### Existing files modified or explicitly verified

- core/backtest_engine.py
  - Separates causal history loading from the trade/equity start; builds policy snapshots; validates decisions; retains all fills, cash, quantities, transactions, and hard safety enforcement.
- core/engine_policy.py
  - Verify only. Its schema-v1 baseline policy values and source labels remain unchanged; policy interface identity lives in the new candidate/result identity.
- core/pit_optimization_contract.py
  - Retains v1 catalog contracts and adds versioned investigator/author/critic schemas plus sealed per-role budgets.
- core/pit_optimization.py
  - Retains v1 readers and aggregate helpers; delegates schema-v2 prepare/canary work to the new controller.
- agent_loop.py
  - Adds optimizer-v2 CLI routing, all-R1 role profiles, explicit iteration-aware audit records, sealed per-call maxima, strict patch bounds, and the policy-only Docker worker adapter.

### Focused tests

- tests/test_strategy_policy.py
  - Small pure-policy and decision-validation checks.
- tests/test_pit_policy_parity.py
  - Synthetic fold reset/history-start contract and canonical parity-file validation.
- tests/test_pit_optimizer_v2.py
  - Role schemas, budgets, objective, fold manifest, candidate validation, identity, and validation-ledger behavior.
- tests/test_pit_optimizer_loop.py
  - One mocked two-iteration end-to-end controller flow.
- Existing focused regressions:
  - tests/test_backtest_engine.py
  - tests/test_backtest_custom_strategy_contract.py
  - tests/test_backtest_open_causality.py
  - tests/test_task11_effective_policy_contract.py
  - tests/test_agent_loop.py

---

### Task 1: Define the Pure Strategy-Policy Contract and Baseline Functions

**Files:**

- Create: core/strategy_policy/__init__.py
- Create: core/strategy_policy/contracts.py
- Create: core/strategy_policy/entry.py
- Create: core/strategy_policy/risk.py
- Create: core/strategy_policy/exit.py
- Create: tests/test_strategy_policy.py

**Interfaces:**

- Produces: POLICY_INTERFACE_VERSION; frozen snapshot/decision dataclasses; StrategyPolicyClient protocol; five pure baseline entry points.
- Consumes: trusted precomputed scalar facts only. It performs no I/O, imports no simulator/data module, and owns no mutable state.

**Exact public interfaces:**

    POLICY_INTERFACE_VERSION = 1

    StrategyPolicyClient.interface_version: int
    StrategyPolicyClient.evaluate_entry(
        snapshot: EntrySnapshot
    ) -> EntryDecision
    StrategyPolicyClient.recommend_capacity(
        snapshot: CapacitySnapshot
    ) -> CapacityDecision
    StrategyPolicyClient.recommend_allocation(
        snapshot: AllocationSnapshot
    ) -> AllocationDecision
    StrategyPolicyClient.select_eviction(
        snapshot: EvictionSnapshot
    ) -> EvictionDecision
    StrategyPolicyClient.evaluate_exit(
        snapshot: ExitSnapshot
    ) -> ExitDecision
    StrategyPolicyClient.close() -> None
    StrategyPolicyClientFactory = Callable[[], StrategyPolicyClient]

    entry.evaluate_entry(snapshot: EntrySnapshot) -> EntryDecision
    risk.recommend_capacity(snapshot: CapacitySnapshot) -> CapacityDecision
    risk.recommend_allocation(
        snapshot: AllocationSnapshot
    ) -> AllocationDecision
    risk.select_eviction(snapshot: EvictionSnapshot) -> EvictionDecision
    exit.evaluate_exit(snapshot: ExitSnapshot) -> ExitDecision

**Exact snapshot and decision fields:**

    EntrySnapshot(
        technical_only: bool,
        require_proper_base: bool,
        c_score: float | None,
        a_score: float | None,
        n_score: float | None,
        s_score: float | None,
        l_score: float | None,
        i_score: float | None,
        m_score: float | None,
        current_growth: float | None,
        annual_growth: float | None,
        rs_score: float | None,
        canslim_score: float | None,
        entry_composite_score: float | None,
        technical_score: float | None,
        institutional_data_available: bool,
        event_close: float | None,
        prior_close: float | None,
        event_volume: float | None,
        prior_average_volume_50: float | None,
        pivot: float | None,
        volume_ratio: float | None,
        extension: float | None,
        price_advanced: bool,
        has_volume_surge: bool,
        in_buy_zone: bool,
        technical_eligible: bool,
        technical_blocking_reasons: tuple[str, ...],
        has_power_gap_today: bool,
        require_bullish_market: bool,
        market_is_bullish: bool,
        cash_deployment_override: bool,
        use_stateful_regime_gate: bool,
        regime_allows_entries: bool,
        market_regime: str,
        distribution_days: int,
        follow_through: bool,
    )
    EntryDecision(
        qualified: bool,
        market_permitted: bool,
        rank: tuple[float | None, float | None],
        blocking_codes: tuple[str, ...],
    )
    CapacitySnapshot(
        configured_max_positions: int | None,
        maximum_policy_positions: int,
        open_position_count: int,
        eligible_signal_count: int,
        cash_fraction: float,
        configured_eviction_enabled: bool,
    )
    CapacityDecision(
        max_positions: int | None,
        eviction_enabled: bool,
    )
    AllocationSnapshot(
        portfolio_equity_at_entry_open: float,
        cash_before_transition: float,
        projected_cash_after_eviction: float,
        gross_exposure_before: float,
        projected_gross_exposure_after_eviction: float,
        entry_open: float,
        pending_entries_remaining: int,
        capacity_is_uncapped: bool,
        configured_position_risk_pct: float,
        configured_stop_loss_pct: float,
        maximum_position_risk_fraction: float,
        maximum_stop_fraction: float,
        canslim_score: float | None,
        rs_score: float | None,
    )
    AllocationDecision(
        risk_fraction: float,
        stop_distance_fraction: float,
        notional_fraction_cap: float | None,
    )
    EvictionPosition(
        slot: int,
        entry_price: float,
        causal_execution_price: float | None,
        rs_score: float,
    )
    EvictionSnapshot(
        capacity_is_finite: bool,
        capacity_is_full: bool,
        eviction_enabled: bool,
        candidate_rs_score: float,
        positions: tuple[EvictionPosition, ...],
    )
    EvictionDecision(slot: int | None)
    ExitSnapshot(
        entry_price: float,
        original_qty: float,
        remaining_qty: float,
        stop_price: float,
        realized_pnl: float,
        canslim_score: float,
        rs_score: float,
        days_held: int,
        peak_close: float,
        breakeven_armed: bool,
        ema_trailing_active: bool,
        scale_out_tier: int,
        early_winner_hold: bool,
        current_high: float,
        current_low: float,
        current_close: float,
        history_session_count: int,
        ema_today: float | None,
        consecutive_closes_below_ema: bool,
        protective_stop_candidates: tuple[float, ...],
        stop_loss_pct: float,
        breakeven_trigger_pct: float,
        ema_period: int,
        ema_consecutive: int,
        stagnation_days: int,
        stagnation_threshold_pct: float,
        scale_out_tiers: tuple[tuple[float, float], ...],
        early_winner_gain_pct: float,
        early_winner_trigger_days: int,
        early_winner_release_days: int,
    )
    ExitAction(
        kind: str,
        trigger_gain_fraction: float | None,
        fraction_of_original_quantity: float | None,
        reason: str,
    )
    ExitDecision(
        actions: tuple[ExitAction, ...],
        next_stop_price: float | None,
        early_winner_hold: bool,
        scale_out_tier: int,
        breakeven_armed: bool,
        ema_trailing_active: bool,
    )

No Trade object, date, symbol, path, DataFrame, history, bundle, or callback crosses this boundary. CapacityDecision.max_positions is either None or an exact positive int no greater than maximum_policy_positions; returning a finite value below the current holding count blocks new admission but never forces liquidation. Baseline capacity returns configured_max_positions, preserving max_positions=None. A non-None notional_fraction_cap is finite and in (0, 1]. ExitAction.kind is exactly scale_out or close: scale_out permits only take_profit_scale_out, requires a positive finite trigger fraction present in scale_out_tiers, requires trusted current_high to have crossed entry_price * (1 + trigger_gain_fraction), and requires a fraction of original quantity in (0, 1]; the validated sum cannot exceed remaining quantity. close permits time_stop, ma_violation, or policy_exit and has no trigger/fraction. Scale-out actions precede at most one final close. The engine derives every execution price. Hard-stop detection and fill remain mandatory engine work before the policy call. protective_stop_candidates is the sorted unique tuple of trusted current stop plus the engine-derived breakeven and EMA candidates that are causally available; next_stop_price is either None or exactly one member no lower than current stop. This preserves the existing case where a causal EMA ratchet is above current close without allowing a candidate to invent a price.

- [ ] **Step 1: Write failing closed-contract tests.** Instantiate every snapshot/decision, round-trip canonical JSON, and reject unknown keys, bool-as-number, NaN/Infinity, mutable collections, invalid market-regime/action/reason enums, candidate-supplied fill prices, and oversized tuples. Assert no contract field can carry a date, symbol, path, DataFrame, callback, or mutable collection.

  Start with this literal closed-type anchor and extend the same table across all ten contracts:

  ```python
  def test_entry_decision_is_closed_and_bool_strict():
      with pytest.raises(TypeError):
          EntryDecision(
              qualified=True,
              market_permitted=True,
              rank=(1.0, 2.0),
              blocking_codes=(),
              symbol="AAPL",
          )
      with pytest.raises(ValueError, match="qualified"):
          EntryDecision(
              qualified=1,
              market_permitted=True,
              rank=(1.0, 2.0),
              blocking_codes=(),
          )
  ```
- [ ] **Step 2: Run the focused test and confirm import failures.**

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_strategy_policy.py

  Expected: collection fails because core.strategy_policy does not exist.
- [ ] **Step 3: Implement only contracts.py and exports.** Use frozen=True and slots=True dataclasses, closed enum validators, tuple-only collection fields, and canonical serializers. Run `python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_strategy_policy.py`; expected failures now name the five missing pure functions rather than contract imports.
- [ ] **Step 4: Implement entry.py.** Reproduce full-mode C/A/RS/composite/technical gating, technical-only bypass semantics, institutional-unavailable score handling, market/regime permission, and baseline rank (canslim_score, rs_score). Add parity cases for technical_only and missing institutional data, then run `python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_strategy_policy.py -k "entry"`; expected: entry cases pass while unimplemented risk/exit cases still fail.
- [ ] **Step 5: Implement risk.py capacity and eviction.** Baseline recommend_capacity returns the configured limit, including None; finite limits must be exact positive ints within maximum_policy_positions. Baseline select_eviction ignores missing causal prices, requires lower candidate RS, prefers underwater positions, then selects minimum RS with stable slot-order ties. Run `python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_strategy_policy.py -k "capacity or eviction"`; expected: those cases pass.

  ```python
  def recommend_capacity(snapshot: CapacitySnapshot) -> CapacityDecision:
      return CapacityDecision(
          max_positions=snapshot.configured_max_positions,
          eviction_enabled=snapshot.configured_eviction_enabled,
      )

  def select_eviction(snapshot: EvictionSnapshot) -> EvictionDecision:
      eligible = tuple(
          position
          for position in snapshot.positions
          if position.causal_execution_price is not None
          and position.rs_score < snapshot.candidate_rs_score
      )
      underwater = tuple(
          position
          for position in eligible
          if position.causal_execution_price < position.entry_price
      )
      pool = underwater or eligible
      if not snapshot.capacity_is_finite or not snapshot.capacity_is_full:
          return EvictionDecision(slot=None)
      if not snapshot.eviction_enabled or not pool:
          return EvictionDecision(slot=None)
      selected = min(pool, key=lambda position: (position.rs_score, position.slot))
      return EvictionDecision(slot=selected.slot)
  ```
- [ ] **Step 6: Implement risk.py allocation recommendation.** Baseline returns configured risk/stop and no extra notional cap. Test the uncapped multi-signal input and finite-capacity input; actual cash, gross exposure, rounded stop, and loss-at-stop enforcement remain trusted engine work in Task 3. Run `python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_strategy_policy.py -k "allocation"`; expected: allocation cases pass.

  ```python
  def recommend_allocation(snapshot: AllocationSnapshot) -> AllocationDecision:
      return AllocationDecision(
          risk_fraction=snapshot.configured_position_risk_pct,
          stop_distance_fraction=snapshot.configured_stop_loss_pct,
          notional_fraction_cap=None,
      )
  ```
- [ ] **Step 7: Implement exit.py.** Reproduce early-winner activation/release, crossed tiers, stagnation, protective-stop ratchet, and EMA close ordering after the engine-owned hard-stop precheck. Test multiple tiers in one bar, reject an uncrossed tier, test a final close, and require next_stop_price to be one trusted protective_stop_candidates member without any policy-provided fill price. Run `python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_strategy_policy.py -k "exit"`; expected: exit cases pass.
- [ ] **Step 8: Run the focused policy file to green.**

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_strategy_policy.py

- [ ] **Step 9: Commit the isolated policy boundary.**

      git add core/strategy_policy tests/test_strategy_policy.py
      git commit -m "feat: define pure strategy policy boundary"

---

### Task 2: Add Independent Fold Warmup and Capture the Pre-Extraction Reference

**Files:**

- Modify: core/backtest_engine.py
- Create: core/pit_optimizer_evaluation.py
- Create: core/pit_policy_parity.py
- Create: tests/test_pit_policy_parity.py
- Modify: tests/test_backtest_open_causality.py

**Interfaces:**

- Produces: FoldSpec/FoldManifest; the PortfolioSimulator.run history_start_date keyword; canonical parity capture/verify commands; authenticated reference and final-attestation schemas.
- Consumes: one authenticated legacy readiness artifact, the sealed PIT bundle, a clean committed source identity, and the fixed local discovery/hidden date contract. It may read the bundle's benchmark session calendar to seal all FoldSpec.sessions, but evaluates only the two discovery folds and performs no provider call.

**Fold and parity contracts created before first use:**

    @dataclass(frozen=True, slots=True)
    class FoldSpec:
        fold_id: str
        purpose: str
        start_date: str
        end_date: str
        sessions: tuple[str, ...]

    @dataclass(frozen=True, slots=True)
    class FoldManifest:
        data_identity_sha256: str
        universe_sha256: str
        benchmark: str
        warmup_start_date: str
        discovery_folds: tuple[FoldSpec, ...]
        hidden_fold: FoldSpec

        @property
        def sha256(self) -> str

    AggregateMetric(metric_id: str, value: float | int)
    FoldAggregateSummary(
        fold_id: str,
        total_return_pct: float,
        excess_total_return_pp: float | None,
        max_drawdown_pct: float,
        sharpe_ratio: float,
        closed_trades: int,
        turnover_pct: float,
        average_exposure_pct: float,
        entry_funnel: tuple[AggregateMetric, ...],
        exit_attribution: tuple[AggregateMetric, ...],
    )

    ParityTransaction(
        date: str,
        symbol: str,
        from_symbol: str | None,
        action: str,
        price: float,
        quantity: float,
        value: float,
        reason: str,
    )
    ParityEntryOutcome(
        symbol: str,
        signal_date: str,
        entry_date: str,
        pivot: float | None,
        buy_zone_lower: float | None,
        buy_zone_upper: float | None,
        entry_open: float | None,
        outcome: str,
    )
    ParityEquityPoint(session: str, equity: float)
    ParityFoldEvidence(
        fold_id: str,
        transactions: tuple[ParityTransaction, ...],
        entry_outcomes: tuple[ParityEntryOutcome, ...],
        equity: tuple[ParityEquityPoint, ...],
        funnel: tuple[AggregateMetric, ...],
        aggregate: FoldAggregateSummary,
        effective_policy_sha256: str,
        evidence_sha256: str,
    )

    @dataclass(frozen=True, slots=True)
    class ParityReference:
        schema_version: int
        reference_source_head: str
        reference_source_fingerprint_sha256: str
        readiness_sha256: str
        pit_bundle_sha256: str
        baseline_manifest_sha256: str
        effective_policy_sha256: str
        fold_manifest: FoldManifest
        universe: tuple[str, ...]
        discovery_evidence: tuple[ParityFoldEvidence, ...]
        discovery_output_sha256s: tuple[tuple[str, str], ...]
        artifact_path: Path
        artifact_sha256: str

    @dataclass(frozen=True, slots=True)
    class ParityAttestation:
        schema_version: int
        reference_artifact_sha256: str
        reference_source_head: str
        final_source_head: str
        final_source_fingerprint_sha256: str
        pit_bundle_sha256: str
        baseline_manifest_sha256: str
        effective_policy_sha256: str
        discovery_fold_manifest_sha256: str
        policy_interface_version: int
        reference_output_sha256s: tuple[tuple[str, str], ...]
        final_output_sha256s: tuple[tuple[str, str], ...]
        final_discovery_evidence: tuple[ParityFoldEvidence, ...]
        transactions_equal: bool
        entry_outcomes_equal: bool
        equity_equal: bool
        funnels_equal: bool
        effective_policy_equal: bool
        artifact_path: Path
        artifact_sha256: str

For every canonical artifact/identity in this plan, externally supplied identity fields such as pit_bundle_sha256 remain inside the hashed content. Only the value's own self-digest and runtime artifact_path are excluded from its preimage. ParityFoldEvidence.evidence_sha256, CandidateIdentity.identity_sha256, PolicySourceScope.sha256, and FrozenModelPricing.pricing_sha256 hash their preceding fields; FoldManifest/PitOptimizerRunManifest/AuthorizationRequirement sha256 properties hash all dataclass fields; ParityReference/ParityAttestation artifact_sha256 hash their persisted JSON without artifact_path/artifact_sha256; PitOptimizerReadiness.readiness_sha256 hashes its persisted JSON without artifact_path/readiness_sha256. The artifact byte hash and logical self-digest are identical whenever they cover the same canonical content. Tests use one shared canonical_json_bytes_without_self_digest helper so a digest never recursively includes itself.

**Engine signature change:**

    PortfolioSimulator.run(
        tickers,
        lookback_weeks=DEFAULT_LOOKBACK_WEEKS,
        *,
        start_date=None,
        end_date=None,
        history_start_date=None,
        benchmark_symbol=None,
        checkpoint_path=None,
        progress_log_path=None,
        resume=False,
        checkpoint_every_days=20,
        checkpoint_code_identity=None,
    ) -> SimulationResult

history_start_date controls only data loaded for causal indicator prehistory. start_date remains the first session allowed to create pending entries, trades, equity observations, or benchmark normalization. The fingerprint and result config bind both dates.

- [ ] **Step 1: Write failing synthetic fold tests.** Prove prehistory is visible to indicators, no pre-fold trade/equity/transaction is emitted, the first in-fold signal still executes only at the next in-fold open, the last-session signal does not execute, open positions are closed by the existing deterministic final-fold liquidation rule, and two consecutive runs on the same simulator reset cash, positions, and pending entries. In Task 3, extend this test with a counting policy_client_factory and require one new client plus one close per run; no policy instance is reused across folds.
- [ ] **Step 2: Run only the new fold tests.**

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_policy_parity.py tests/test_backtest_open_causality.py

  Expected: the new history-start tests fail because run() cannot load prehistory separately.
- [ ] **Step 3: Implement history_start_date without changing daily ordering.** Fetch PIT prices/RS closes from history_start_date through end_date, but iterate and normalize equity only from start_date through end_date. Add a private reset-at-run-start helper covering cash, open positions, pending entries, transaction/equity/diagnostic collections, regime state, and injected policy-client state. Run the synthetic tests; expected failures now concern missing FoldManifest/parity serialization only.

  Keep the existing fetch and daily-loop bodies unchanged around this exact separation:

  ```python
  trade_start, end_ts = _resolve_window(
      start_date=start_date,
      end_date=end_date,
      lookback_weeks=lookback_weeks,
  )
  history_start = (
      trade_start
      if history_start_date is None
      else pd.Timestamp(history_start_date).normalize()
  )
  if history_start > trade_start:
      raise ValueError("history_start_date must not follow start_date")
  ticker_ohlcv = self.data_fetcher.fetch_price_data(
      all_tickers, history_start, end_ts
  )
  all_closes = self.data_fetcher.fetch_rs_universe_closes(
      universe, history_start, end_ts
  )
  evaluation_sessions = tuple(
      session for session in benchmark_sessions
      if trade_start <= session <= end_ts
  )
  ```
- [ ] **Step 4: Implement FoldSpec, FoldManifest, AggregateMetric, and FoldAggregateSummary in core/pit_optimizer_evaluation.py.** Require exact 60-session discovery/discovery/hidden windows, one benchmark, one universe/warmup/data identity, non-overlap, chronology, and hidden strictly after discovery. Build sessions from the authenticated bundle calendar; do not read hidden prices, returns, trades, or baseline metrics.

  Add this contract test before the dataclass validation:

  ```python
  def _fold(fold_id: str, purpose: str, start: str) -> FoldSpec:
      sessions = tuple(
          value.date().isoformat()
          for value in pd.bdate_range(start=start, periods=60)
      )
      return FoldSpec(
          fold_id=fold_id,
          purpose=purpose,
          start_date=sessions[0],
          end_date=sessions[-1],
          sessions=sessions,
      )


  def test_fold_manifest_rejects_reused_discovery_sessions():
      first = _fold("discovery_1", "discovery", "2021-01-04")
      second = replace(first, fold_id="discovery_2")
      hidden = _fold("hidden_1", "hidden", "2022-01-03")
      with pytest.raises(ValueError, match="overlap"):
          FoldManifest(
              data_identity_sha256="a" * 64,
              universe_sha256="b" * 64,
              benchmark="SPY",
              warmup_start_date="2020-01-02",
              discovery_folds=(first, second),
              hidden_fold=hidden,
          )
  ```
- [ ] **Step 5: Implement canonical ParityReference capture and ParityAttestation verification in core/pit_policy_parity.py.** Reference output embeds one exact ParityFoldEvidence per discovery fold: canonical transaction rows, EntryAttemptOutcome primitives, exact equity index/values, funnel counts, aggregate metrics, and schema-v1 effective-policy digest. Verification recomputes and embeds the final ParityFoldEvidence values at a later clean HEAD, verifies every nested evidence hash against the reference, and writes one create-only attestation only when every equality flag is true. Hashes never stand in for retrievable evidence.
- [ ] **Step 6: Re-run the synthetic fold/parity tests to green.**

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_policy_parity.py tests/test_backtest_open_causality.py

- [ ] **Step 7: Commit the fold boundary, fold contract, and parity harness before capture.** Do not add a parity artifact.

      git add core/backtest_engine.py core/pit_optimizer_evaluation.py core/pit_policy_parity.py tests/test_pit_policy_parity.py tests/test_backtest_open_causality.py
      git commit -m "feat: isolate causal warmup from fold trading"

- [ ] **Step 8: Require a clean committed source, then capture the authenticated inline-policy reference once before Task 3 switches authority.**

      python -B -m core.pit_policy_parity capture --readiness .artifacts/pit-optimizer-subset-performance-20260827T200714Z/pit-optimization-readiness-d3cbfcb22900/readiness.json --pit-bundle .artifacts/task-4-regeneration-20260823T223000Z/pit-bundle/pit_baseline.sqlite3 --output .artifacts/pit-policy-parity-v2/reference.json

  Expected: PIT_POLICY_PARITY_REFERENCE with the two evaluated discovery folds 2021-06-25..2021-09-20 and 2021-09-21..2021-12-14, plus a sealed but unevaluated hidden FoldSpec for 2021-12-15..2022-03-11. Each evaluated fold starts from normalized capital with no carryover. No provider or Docker process starts.

---

### Task 3: Route the Simulator Through the Extracted Baseline Policy

**Files:**

- Create: core/strategy_policy/runtime.py
- Modify: core/backtest_engine.py
- Verify: core/engine_policy.py
- Modify: tests/test_strategy_policy.py
- Modify: tests/test_pit_policy_parity.py
- Modify: tests/test_backtest_engine.py
- Modify: tests/test_backtest_custom_strategy_contract.py
- Modify: tests/test_task11_effective_policy_contract.py only if an assertion is needed for the separate interface identity

**Interfaces:**

- Produces: InProcessPolicyClient; simulator policy_client_factory injection with one fresh client per run; engine-side closed decision validators; projected capacity/eviction/allocation transition.
- Consumes: the Task 1 pure modules and trusted entry/market/portfolio/current-bar facts. Candidate code never receives an engine object. The Task 2 reference remains sealed until final-HEAD verification in Task 9.

**Simulator constructor addition:**

    policy_client_factory: StrategyPolicyClientFactory | None = None

None constructs an InProcessPolicyClient factory from the three baseline policy modules. At the start of every run(), the simulator calls the factory exactly once; a finally block closes that client and clears the run-local reference. Injected tests/workers also supply factories, never a reusable client instance. The client never receives the bundle, provider, history frame, date, symbol, Trade, or simulator.

**Trusted adapter skeleton:**

    @dataclass(slots=True)
    class PendingEntry:
        signal: dict[str, object]
        capacity: CapacityDecision

    @dataclass(frozen=True, slots=True)
    class ProjectedEntryTransition:
        eviction_slot: int | None
        eviction_symbol: str | None
        eviction_price: float | None
        entry_open: float
        portfolio_equity_at_entry_open: float
        projected_cash: float
        projected_gross_long_notional: float

    @dataclass(frozen=True, slots=True)
    class ValidatedEntryTransition:
        projection: ProjectedEntryTransition
        quantity: float
        buy_notional: float
        stop_price: float

    PortfolioSimulator._build_entry_snapshot(
        *,
        row: Mapping[str, object],
        facts: CanslimEntryFacts,
        market_allowed: bool,
        market_state: Mapping[str, object],
    ) -> EntrySnapshot
    PortfolioSimulator._resolve_capacity(
        *,
        eligible_signal_count: int,
        cash_fraction: float,
    ) -> CapacityDecision
    PortfolioSimulator._build_eviction_snapshot(
        *,
        pending: PendingEntry,
        ticker_ohlcv: Mapping[str, pd.DataFrame],
        entry_date: pd.Timestamp,
    ) -> EvictionSnapshot
    PortfolioSimulator._project_entry_transition(
        *,
        pending: PendingEntry,
        ticker_ohlcv: Mapping[str, pd.DataFrame],
        entry_date: pd.Timestamp,
        eviction: EvictionDecision,
    ) -> ProjectedEntryTransition | None
    PortfolioSimulator._validate_entry_transition(
        projection: ProjectedEntryTransition,
        recommendation: AllocationDecision,
    ) -> ValidatedEntryTransition
    PortfolioSimulator._apply_entry_transition(
        transition: ValidatedEntryTransition,
        pending: PendingEntry,
        entry_date: pd.Timestamp,
    ) -> None
    PortfolioSimulator._build_exit_snapshot(
        *,
        trade: Trade,
        current_high: float,
        current_low: float,
        current_close: float,
        history_session_count: int,
        ema_today: float | None,
        consecutive_closes_below_ema: bool,
        protective_stop_candidates: tuple[float, ...],
    ) -> ExitSnapshot
    PortfolioSimulator._apply_exit_decision(
        symbol: str,
        trade: Trade,
        decision: ExitDecision,
        close: float,
        date_str: str,
    ) -> None

- [ ] **Step 1: Add failing factory-lifecycle and entry/ranking tests.** Require one fresh policy client and one close per run, including an injected simulator failure and two consecutive parity-fold runs in tests/test_pit_policy_parity.py. Cover full and technical-only entry re-canonicalization, institutional-data reweighting, nullable finite rank validation, stable rank order, and rejection of a policy decision containing a date, symbol, fill price, nonfinite score, or unknown field.

  Add this lifecycle anchor to `tests/test_backtest_engine.py`; the entry/rank table stays in the single parameterized `tests/test_strategy_policy.py` matrix from Task 1:

  ```python
  class _ExplodingFetcher:
      def fetch_price_data(self, *_args, **_kwargs):
          raise RuntimeError("injected fetch failure")


  class _CountingClient(InProcessPolicyClient):
      def __init__(self, closed: list[int]) -> None:
          self._closed_events = closed

      def close(self) -> None:
          self._closed_events.append(1)


  def test_policy_factory_creates_and_closes_once_per_run():
      made: list[_CountingClient] = []
      closed: list[int] = []

      def factory() -> _CountingClient:
          client = _CountingClient(closed)
          made.append(client)
          return client

      simulator = PortfolioSimulator(
          data_fetcher=_ExplodingFetcher(),
          policy_client_factory=factory,
      )
      for _ in range(2):
          with pytest.raises(RuntimeError, match="injected fetch failure"):
              simulator.run(["AAPL"], start_date="2021-06-25", end_date="2021-09-20")
      assert len({id(client) for client in made}) == 2
      assert closed == [1, 1]
      assert simulator._policy_client is None
  ```
- [ ] **Step 2: Run the entry/factory slice and confirm the new assertions fail.**

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_strategy_policy.py tests/test_backtest_custom_strategy_contract.py tests/test_pit_policy_parity.py -k "policy_client_factory or entry_policy or policy_rank"

  Expected: failures name the missing InProcessPolicyClient/factory adapter; unrelated existing entry cases stay green.
- [ ] **Step 3: Implement runtime.py plus only the factory, entry, and ranking adapters.** Construct the baseline client factory when the constructor argument is None. Create one run-local client at run() entry, close it in finally, and clear it before returning or re-raising. CanslimStrategy and _canonicalize_signal_row construct the same complete trusted EntrySnapshot and invoke that client; remove duplicate direct evaluate_entry_contract authority only after both seams agree. Map only validated nullable finite ranks back to signal rows. Re-run the Step 2 command; expected: green.

  Add this exact baseline adapter, then wrap the existing run body in a private `_run_with_policy_client_active` method without reordering it:

  ```python
  class InProcessPolicyClient:
      interface_version = POLICY_INTERFACE_VERSION

      def evaluate_entry(self, snapshot):
          return entry.evaluate_entry(snapshot)

      def recommend_capacity(self, snapshot):
          return risk.recommend_capacity(snapshot)

      def recommend_allocation(self, snapshot):
          return risk.recommend_allocation(snapshot)

      def select_eviction(self, snapshot):
          return risk.select_eviction(snapshot)

      def evaluate_exit(self, snapshot):
          return exit.evaluate_exit(snapshot)

      def close(self) -> None:
          return None
  ```

  ```python
  client = self._policy_client_factory()
  if client.interface_version != POLICY_INTERFACE_VERSION:
      raise ValueError("policy interface version mismatch")
  self._policy_client = client
  try:
      return self._run_with_policy_client_active(
          tickers,
          lookback_weeks,
          start_date=start_date,
          end_date=end_date,
          history_start_date=history_start_date,
          benchmark_symbol=benchmark_symbol,
          checkpoint_path=checkpoint_path,
          progress_log_path=progress_log_path,
          resume=resume,
          checkpoint_every_days=checkpoint_every_days,
          checkpoint_code_identity=checkpoint_code_identity,
      )
  finally:
      try:
          client.close()
      finally:
          self._policy_client = None
  ```
- [ ] **Step 4: Add failing capacity-carrier tests.** Cover uncapped None, finite free slots, full capacity with eviction disabled, exactly one replacement candidate with eviction enabled, invalid finite capacity outside 1..maximum_policy_positions, and a capacity lowered below current holdings that blocks new entries without liquidating anything. Assert _evaluate_signals returns PendingEntry values carrying the validated signal-session CapacityDecision, checkpoint round-trip preserves it, and next-session _enter_position enforces that carried decision even when simulator defaults differ.

  ```python
  def test_pending_entry_round_trip_and_carried_capacity():
      pending = PendingEntry(
          signal={"symbol": "NEW", "rs_score": 90.0},
          capacity=CapacityDecision(max_positions=1, eviction_enabled=False),
      )
      assert PendingEntry.from_primitive(pending.to_primitive()) == pending
      simulator = PortfolioSimulator(max_positions=None, enable_eviction=True)
      simulator._open_positions = {"OLD": object()}
      assert simulator._capacity_state(pending) == (True, False)
  ```
- [ ] **Step 5: Run the capacity slice and confirm it fails at _resolve_capacity.**

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_backtest_engine.py -k "policy_capacity"

- [ ] **Step 6: Implement _resolve_capacity and PendingEntry before signal truncation.** Call recommend_capacity exactly once per eligible signal batch. None preserves every eligible signal. A finite limit uses trusted current holdings to calculate open slots; a full portfolio admits at most one replacement candidate only when eviction is enabled. Return and checkpoint PendingEntry(signal copy, validated CapacityDecision), then make next-session _enter_position revalidate and enforce that carried max_positions/eviction_enabled rather than self.max_positions/self.enable_eviction. Re-run the Step 5 command; expected: green.

  ```python
  @dataclass(slots=True)
  class PendingEntry:
      signal: dict[str, object]
      capacity: CapacityDecision

      def to_primitive(self) -> dict[str, object]:
          return {
              "signal": dict(self.signal),
              "capacity": {
                  "max_positions": self.capacity.max_positions,
                  "eviction_enabled": self.capacity.eviction_enabled,
              },
          }

      @classmethod
      def from_primitive(cls, raw: Mapping[str, object]) -> "PendingEntry":
          signal = raw.get("signal")
          capacity = raw.get("capacity")
          if not isinstance(signal, Mapping) or not isinstance(capacity, Mapping):
              raise ValueError("pending entry checkpoint is invalid")
          max_positions = capacity.get("max_positions")
          eviction_enabled = capacity.get("eviction_enabled")
          if max_positions is not None and (
              type(max_positions) is not int or max_positions < 1
          ):
              raise ValueError("pending capacity is invalid")
          if type(eviction_enabled) is not bool:
              raise ValueError("pending eviction flag is invalid")
          return cls(
              signal=dict(signal),
              capacity=CapacityDecision(
                  max_positions=max_positions,
                  eviction_enabled=eviction_enabled,
              ),
          )

  def _capacity_state(self, pending: PendingEntry) -> tuple[bool, bool]:
      limit = pending.capacity.max_positions
      full = limit is not None and len(self._open_positions) >= limit
      return full, bool(full and pending.capacity.eviction_enabled)
  ```
- [ ] **Step 7: Add failing projected eviction/allocation tests.** Build stable opaque slots in insertion order and reject an unknown slot, missing causal open, candidate fill price, or mutation during projection. Reject nonpositive/nonfinite risk, stop distance, notional cap, derived quantity, entry price, stop, or notional; risk_fraction above 0.01; stop_distance_fraction above 0.08; projected cash below buy notional; projected gross long notional above equity; and actual rounded loss-at-stop above either the 1% engine ceiling or the recommended risk budget. Assert a failed validation leaves cash, positions, trades, and transactions byte-for-byte unchanged.
- [ ] **Step 8: Run the projected-transition slice and confirm it fails before any mutation.**

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_backtest_engine.py -k "policy_eviction or policy_allocation or projected_entry_transition"

- [ ] **Step 9: Implement select, project, validate, and apply as separate helpers.** Let policy select only an opaque eviction slot. The engine resolves its symbol and causal open, projects sell proceeds/cash/gross exposure without calling the mutating _try_evict path, then builds AllocationSnapshot. From the validated recommendation, derive target notional, quantity, and a cent-rounded stop. Prove every Step 7 invariant before either sell or buy, use that exact stop in Trade, and then apply the prevalidated sell-plus-buy transition. Re-run the Step 8 command; expected: green.
- [ ] **Step 10: Add failing exit-adapter tests.** Cover engine-owned hard stop before any policy call, early-winner activation/release, multiple crossed scale-out tiers in one bar, rejection of an uncrossed tier before mutation, stagnation close, EMA close, and an existing-parity EMA stop above current close. Require next_stop_price to equal one trusted protective_stop_candidates value no lower than current stop, validate the complete action plan before mutation, and reject candidate fill prices or invalid action order.
- [ ] **Step 11: Run the exit slice and confirm the policy adapter is missing.**

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_backtest_engine.py -k "policy_exit"

- [ ] **Step 12: Implement the exit adapter without moving accounting authority.** Increment days/peak in trusted code, execute the existing hard-stop precheck, and build the sorted unique protective_stop_candidates from current stop plus causally enabled breakeven/EMA values. Construct ExitSnapshot from causal scalars and active thresholds, then validate the complete plan before mutation. For every scale-out, require the trigger to be configured and current_high to have crossed the engine-derived tier price; fill close actions only at trusted current close; apply only a selected trusted stop candidate. Re-run the Step 11 command; expected: green.
- [ ] **Step 13: Preserve and verify policy attestation.** The schema-v1 effective-engine-policy bytes and digest remain unchanged; POLICY_INTERFACE_VERSION is recorded separately in result/candidate identity and old policy source labels are untouched.

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_task11_effective_policy_contract.py

- [ ] **Step 14: Run the focused integration set and commit the extracted policy.** Final real-fold parity waits until every implementation commit is complete, so its clean source identity cannot become stale.

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_strategy_policy.py tests/test_pit_policy_parity.py tests/test_backtest_engine.py tests/test_backtest_custom_strategy_contract.py tests/test_backtest_open_causality.py tests/test_task11_effective_policy_contract.py
      git add core/strategy_policy/runtime.py core/backtest_engine.py tests/test_strategy_policy.py tests/test_pit_policy_parity.py tests/test_backtest_engine.py tests/test_backtest_custom_strategy_contract.py tests/test_task11_effective_policy_contract.py
      git commit -m "refactor: execute strategy through pure policy"

---

### Task 4: Add Schema-v2 Roles, Manifest, Objective, and Validation Ledger

**Files:**

- Modify: core/pit_optimization_contract.py
- Modify: core/pit_optimizer_evaluation.py
- Create: tests/test_pit_optimizer_v2.py

**Interfaces:**

- Produces: versioned closed role input/output schemas; PitOptimizerCallBudget; one shared PatchBounds; AuthorizationRequirement; identity-complete PitOptimizerRunManifest/GateConfig; DiscoveryScore; HoldoutDecision; exposure-aware ValidationLedger; canonical manifest writer.
- Consumes: Task 2 FoldManifest/ParityAttestation, closed canonical JSON, authenticated legacy readiness, sealed local paths/input identities, operator call budgets, permanent runtime root, and aggregate fold results. It never imports or executes candidate source.

**New closed types:**

    OPTIMIZER_V2_ROLES = ("investigator", "author", "critic")
    MAX_ROLE_TEXT_BYTES = 4 * 1024
    MAX_ROLE_LIST_ITEMS = 16
    MAX_AUTHOR_DIFF_BYTES = 64 * 1024
    MAX_POLICY_SOURCE_BUNDLE_BYTES = 64 * 1024
    MAX_DISCOVERY_EVIDENCE_BYTES = 8 * 1024
    MAX_INVESTIGATOR_ARTIFACT_BYTES = 8 * 1024
    MAX_AUTHOR_NON_DIFF_ARTIFACT_BYTES = 8 * 1024
    MAX_CRITIC_ARTIFACT_BYTES = 8 * 1024
    MAX_ITERATION_FEEDBACK_BYTES = 4 * 1024
    MAX_ITERATION_HISTORY_BYTES = 32 * 1024
    MAX_INVESTIGATOR_DYNAMIC_BYTES = 112 * 1024
    MAX_AUTHOR_DYNAMIC_BYTES = 96 * 1024
    MAX_CRITIC_DYNAMIC_BYTES = 24 * 1024

    @dataclass(frozen=True, slots=True)
    class PitOptimizerCallBudget:
        call_index: int
        iteration: int
        role: str
        model: str
        max_static_input_bytes: int
        max_dynamic_input_bytes: int
        max_input_tokens: int
        max_output_tokens: int
        max_response_bytes: int
        max_usd: float

    @dataclass(frozen=True, slots=True)
    class PatchBounds:
        max_files: int
        max_hunks: int
        max_changed_lines: int
        max_diff_bytes: int

    @dataclass(frozen=True, slots=True)
    class PolicySourceScope:
        schema_version: int
        policy_interface_version: int
        initial_policy_source_sha256s: tuple[tuple[str, str], ...]
        editable_paths: tuple[str, ...]
        max_policy_source_bundle_bytes: int
        max_iteration_feedback_bytes: int
        max_iteration_history_bytes: int
        hard_patch_bounds: PatchBounds
        candidate_bounds: PatchBounds
        max_iterations: int
        allowed_descendant_rule: str

        @property
        def sha256(self) -> str

    @dataclass(frozen=True, slots=True)
    class AuthorizationRequirement:
        window_id: str
        max_calls: int
        max_tokens: int
        max_usd: float
        policy_source_scope_sha256: str
        provider_retries: int
        apply: bool

        @property
        def sha256(self) -> str

    @dataclass(frozen=True, slots=True)
    class DiscoveryScore:
        median_excess_return_pp: Decimal
        worst_excess_return_pp: Decimal
        max_drawdown_magnitude_pp: Decimal

        @property
        def ordering_key(self) -> tuple[Decimal, Decimal, Decimal]:
            return (
                self.median_excess_return_pp,
                self.worst_excess_return_pp,
                -self.max_drawdown_magnitude_pp,
            )

    @dataclass(frozen=True, slots=True)
    class HoldoutDecision:
        excess_total_return_pp: Decimal
        closed_trades: int
        safety_complete: bool
        integrity_complete: bool
        accounting_complete: bool
        long_replay_eligible: bool

    @dataclass(frozen=True, slots=True)
    class ValidationWindowIdentity:
        pit_bundle_sha256: str
        universe_sha256: str
        benchmark: str
        warmup_contract_sha256: str
        sessions_sha256: str
        session_count: int
        first_session: str
        last_session: str

    @dataclass(frozen=True, slots=True)
    class ValidationExposureMetadata:
        run_id: str
        source_head: str
        baseline_policy_sha256: str
        candidate_identity_sha256: str | None
        exposure_kind: str

    @dataclass(frozen=True, slots=True)
    class ValidationReservation:
        consumption_key_sha256: str
        reservation_record_sha256: str

    class ValidationLedger:
        def mark_discovery(
            self,
            identity: ValidationWindowIdentity,
            metadata: ValidationExposureMetadata,
        ) -> ValidationReservation
        def reserve_hidden(
            self,
            identity: ValidationWindowIdentity,
            metadata: ValidationExposureMetadata,
        ) -> ValidationReservation
        def record_outcome(
            self,
            reservation: ValidationReservation,
            *,
            attempted: bool,
            completed: bool,
            failure_code: str | None,
        ) -> None

    @dataclass(frozen=True, slots=True)
    class PitOptimizerRunManifest:
        schema_version: int
        run_id: str
        run_kind: str
        model: str
        source_head: str
        source_fingerprint_sha256: str
        legacy_readiness_sha256: str
        pit_bundle_sha256: str
        baseline_manifest_sha256: str
        effective_policy_sha256: str
        policy_interface_version: int
        policy_source_sha256s: tuple[tuple[str, str], ...]
        editable_paths: tuple[str, ...]
        policy_source_scope: PolicySourceScope
        immutable_constraints_sha256: str
        fold_manifest: FoldManifest
        parity_attestation_sha256: str
        sandbox_image: str
        validation_ledger_name: str
        immutable_constraint_ids: tuple[str, ...]
        candidate_bounds: PatchBounds
        call_budgets: tuple[PitOptimizerCallBudget, ...]
        max_iterations: int
        non_improving_limit: int
        authorization_requirement: AuthorizationRequirement

        @property
        def sha256(self) -> str

    @dataclass(frozen=True, slots=True)
    class PitOptimizerGateConfig:
        phase: str
        baseline_run: Path
        baseline_manifest_sha256: str
        pit_bundle: Path
        pit_bundle_sha256: str
        effective_policy_sha256: str
        optimizer_manifest: Path
        optimizer_manifest_sha256: str
        verified_parity_artifact: Path
        verified_parity_sha256: str
        readiness_artifact: Path | None
        readiness_sha256: str | None
        authorization_window_id: str | None
        authorization_requirement_sha256: str
        source_transmission_authorized: bool
        max_usd: float
        max_api_calls: int
        max_tokens: int
        max_iterations: int
        apply: bool

        def validate(self) -> None

    def build_subset_manifest(
        *,
        legacy_readiness: Mapping[str, object],
        legacy_readiness_path: Path,
        parity_attestation: ParityAttestation,
        verified_parity_path: Path,
        pit_bundle: Path,
        baseline_run: Path,
        source_root: Path,
        permanent_runtime_root: Path,
        controller_temp_parent: Path,
        artifact_root: Path,
        sandbox_image: str,
        call_budgets: tuple[PitOptimizerCallBudget, ...],
        candidate_bounds: PatchBounds,
        max_iterations: int,
    ) -> PitOptimizerRunManifest

    def write_optimizer_manifest(
        manifest: PitOptimizerRunManifest,
        output: Path,
    ) -> tuple[Path, str]

    def build_prepare_command(
        manifest: PitOptimizerRunManifest,
        *,
        manifest_path: Path,
        legacy_readiness_path: Path,
        verified_parity_path: Path,
        pit_bundle_path: Path,
        baseline_run_path: Path,
        repo_root: Path,
        permanent_runtime_root: Path,
        controller_temp_parent: Path,
        artifact_root: Path,
        git_executable: Path,
        docker_executable: Path,
        sandbox_image: str,
    ) -> str

    PolicySourceRecord(path: str, sha256: str, declared_symbols: tuple[str, ...], text: str)
    PolicySourceBundle(
        policy_interface_version: int,
        cumulative_diff_sha256: str,
        cumulative_diff: str,
        files: tuple[PolicySourceRecord, ...],
    )
    RuleSummaryRecord(rule_id: str, text: str)
    StrategyRuleSummary(records: tuple[RuleSummaryRecord, ...])
    DiscoveryEvidenceSummary(
        folds: tuple[FoldAggregateSummary, ...],
        score: DiscoveryScore | None,
        evidence_ids: tuple[str, ...],
    )
    ProviderSeed(
        rule_summary: StrategyRuleSummary,
        baseline_discovery: DiscoveryEvidenceSummary,
    )
    IncumbentSummary(
        candidate_identity_sha256: str | None,
        accepted_iteration: int | None,
        behavioral_summary: str,
        discovery: DiscoveryEvidenceSummary,
    )
    AuthorManifestSummary(
        hypothesis_id: str,
        behavioral_summary: str,
        changed_paths: tuple[str, ...],
        changed_symbols: tuple[str, ...],
    )
    CandidateValidationSummary(
        failure_code: str | None,
        syntax_ok: bool,
        imports_ok: bool,
        purity_ok: bool,
        deterministic_ok: bool,
        worker_ok: bool,
        replay_attempted: bool,
    )
    CandidateComparisonSummary(
        folds: tuple[FoldAggregateSummary, ...],
        score: DiscoveryScore | None,
        diagnostics: tuple[AggregateMetric, ...],
    )
    IterationFeedbackSummary(
        iteration: int,
        hypothesis_id: str,
        family: str,
        author_summary: str,
        validation_code: str,
        discovery_score: DiscoveryScore | None,
        critic_disposition: str,
        critic_next_direction: str,
        incumbent_changed: bool,
    )
    InvestigatorInput(
        schema_version: int,
        iteration: int,
        policy_interface_version: int,
        immutable_constraint_ids: tuple[str, ...],
        candidate_bounds: PatchBounds,
        rule_summary: StrategyRuleSummary,
        source_bundle: PolicySourceBundle,
        baseline_discovery: DiscoveryEvidenceSummary,
        incumbent_summary: IncumbentSummary,
        prior_iterations: tuple[IterationFeedbackSummary, ...],
    )
    AuthorInput(
        schema_version: int,
        iteration: int,
        policy_interface_version: int,
        immutable_constraint_ids: tuple[str, ...],
        candidate_bounds: PatchBounds,
        investigator: InvestigatorArtifact,
        source_bundle: PolicySourceBundle,
    )
    CriticInput(
        schema_version: int,
        iteration: int,
        immutable_constraint_ids: tuple[str, ...],
        hypothesis_id: str,
        investigator_summary: InvestigatorArtifact,
        author_manifest: AuthorManifestSummary,
        validation: CandidateValidationSummary,
        candidate_vs_baseline: CandidateComparisonSummary | None,
        candidate_vs_incumbent: CandidateComparisonSummary | None,
    )

    InvestigatorArtifact(
        hypothesis_id: str,
        family: str,
        evidence_ids: tuple[str, ...],
        causal_rationale: str,
        target_paths: tuple[str, ...],
        target_symbols: tuple[str, ...],
        expected_diagnostic_changes: tuple[str, ...],
        known_risks: tuple[str, ...],
        author_instructions: tuple[str, ...],
    )
    AuthorArtifact(
        hypothesis_id: str,
        behavioral_summary: str,
        changed_paths: tuple[str, ...],
        changed_symbols: tuple[str, ...],
        unified_diff: str,
        assumptions: tuple[str, ...],
        validation_suggestions: tuple[str, ...],
    )
    CriticArtifact(
        hypothesis_id: str,
        prediction_vs_observation: str,
        causal_explanation: str,
        evidence_ids: tuple[str, ...],
        disposition: str,
        next_direction: str,
    )
    InvestigatorArtifact.from_json(
        raw: str,
        *,
        max_total_bytes: int,
    ) -> InvestigatorArtifact
    AuthorArtifact.from_json(
        raw: str,
        *,
        max_diff_bytes: int,
        max_total_bytes: int,
    ) -> AuthorArtifact
    CriticArtifact.from_json(
        raw: str,
        *,
        max_total_bytes: int,
    ) -> CriticArtifact
    pit_optimizer_response_format(role: str) -> dict[str, object]

**First subset manifest:**

- discovery_1: 2021-06-25 through 2021-09-20, 60 SPY sessions
- discovery_2: 2021-09-21 through 2021-12-14, 60 SPY sessions
- hidden_1: 2021-12-15 through 2022-03-11, 60 SPY sessions
- same sealed 25-symbol universe and warmup convention for all folds

The canonical run manifest has schema_version=2, run_kind=subset_canary, model=deepseek/deepseek-r1, the complete source/readiness/bundle/baseline/effective-policy/policy-files/constraints/fold/final-parity identity graph, one freshly generated AuthorizationRequirement, the complete local FoldManifest, immutable constraint IDs, candidate bounds, and one PitOptimizerCallBudget entry for every planned call. PolicySourceScope.sha256 is the canonical hash of every field shown above with only its own sha256 omitted; allowed_descendant_rule is exactly authenticated_initial_sources_plus_validated_cumulative_diff. An outbound source bundle is authorized only when its three files descend from those initial hashes by the controller-derived, bounds-valid cumulative diff and every scope cap still holds. Any initial hash/path/cap/bound/iteration/rule change produces a different scope and requires a new authorization. AuthorizationRequirement.policy_source_scope_sha256 must equal manifest.policy_source_scope.sha256. A two-iteration canary seals six entries in investigator/author/critic order. The complete manifest is controller-private; provider payload builders omit the hidden FoldSpec, exact dates, universe members, local paths, and validation-ledger facts. Generating the requirement/window ID grants no spending authority.

The first proposed six-call manifest uses the same caps in both iterations:

- investigator: 8,000 static bytes + 80,000 dynamic bytes, 88,000 input tokens, 4,000 output tokens, 8-KiB canonical response, USD 0.05;
- author: 12,000 static bytes + 76,000 dynamic bytes, 88,000 input tokens, 8,000 output tokens, 16-KiB canonical response including at most an 8-KiB diff, USD 0.10;
- critic: 8,000 static bytes + 24,000 dynamic bytes, 32,000 input tokens, 4,000 output tokens, 8-KiB canonical response, USD 0.05.

The proposed cumulative maxima are six calls, 448,000 tokens, and USD 0.40. The first canary lowers candidate bounds to three files, 12 hunks, 80 changed lines, and 8 KiB. Before writing the manifest, measure the canonical initial policy-file text and exact source-bundle envelope and prove `initial_policy_bytes + (2 * max_diff_bytes) + envelope_bytes <= 64 KiB`: one max_diff term bounds worst-case source growth, and one binds the cumulative diff carried beside final source. Render the worst prospective iteration-2 bundle as a test fixture. Before accepting any incumbent, materialize and size its exact next-iteration PolicySourceBundle; an oversize candidate receives the closed next_context_oversize validation result and cannot become incumbent. These are preparation values, not spend authorization. The conservative input-token upper bound is the exact canonical UTF-8 byte length of all system messages, response schemas, and dynamic JSON; manifest construction proves each role's worst declared section sizes fit its input cap. If the operator later authorizes different ceilings, regenerate and reauthenticate the manifest/readiness before canary.

- [ ] **Step 1: Write failing closed input/output schema tests.** Cover all exact role-input fields above, matching hypothesis IDs, family/disposition enums, unique bounded lists, source-file records, per-summary/history caps, total canonical response-byte caps, and unknown/duplicate-key rejection. Assert no input schema admits hidden identity/date/metrics, ledger facts, raw trades/holdings, credentials, local paths, or provider-audit bodies. Prove an oversized InvestigatorArtifact cannot overflow AuthorInput and an oversized Author/Critic artifact cannot enter a later envelope.
- [ ] **Step 2: Implement versioned role contracts only.** Retain every v1 class/reader. Add v2 canonical serializers/parsers and response formats, then run `python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_v2.py -k "role_schema"`; expected: role-schema cases pass.
- [ ] **Step 3: Write failing manifest/identity/budget tests.** Require all identity fields above, sandbox image and exact candidate bounds, exactly three iteration-major R1 call records per declared iteration, call_index=3*(iteration-1)+role ordinal, positive caps, sums within AuthorizationRequirement, apply=false, retries=0, and PatchBounds within the hard 3-file/200-line/64-KiB ceiling. Require PolicySourceScope to bind the exact initial hashes, paths, bundle/history caps, hard/sealed bounds, iterations, and descendant rule; any expansion changes its digest and fails an existing authorization. Measure actual initial policy/envelope bytes and reject any bound for which `initial + 2*max_diff + envelope` exceeds the source cap. Render the complete worst-case iteration-2 static+dynamic messages from the total artifact caps and prove UTF-8 bytes fit max_input_tokens.

  ```python
  def test_policy_source_scope_digest_changes_on_expansion(v2_manifest):
      scope = v2_manifest.policy_source_scope
      expanded = replace(scope, max_iterations=scope.max_iterations + 1)
      assert expanded.sha256 != scope.sha256
      assert (
          v2_manifest.authorization_requirement.policy_source_scope_sha256
          == scope.sha256
      )
      assert (
          v2_manifest.authorization_requirement.policy_source_scope_sha256
          != expanded.sha256
      )
  ```
- [ ] **Step 4: Implement GateConfig, PatchBounds, PolicySourceScope, AuthorizationRequirement, and manifest construction.** Authenticate the parity/fold/readiness/bundle/baseline/source graph, derive hidden sessions from the supplied sealed bundle calendar, validate all explicit local paths, write canonical create-only JSON, and emit its digest plus exact prepare command. Require every prospective source bundle to prove the sealed descendant rule before it can be rendered. It may seal hidden session identity but never evaluates hidden prices, baseline, candidate, or metrics. Run `python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_v2.py -k "manifest or gate or input_budget or policy_source_scope"`; expected: identity/budget cases pass.
- [ ] **Step 5: Write failing objective/holdout tests.** Cover Decimal(str(value)).quantize(Decimal("0.01"), ROUND_HALF_EVEN), two-fold median midpoint, worst fold, drawdown sign inversion in ordering_key, strict improvement only, at least one closed discovery trade per fold, and holdout eligibility at 0.10 point/three trades with safety/integrity/accounting and no Sharpe gate.

  ```python
  def test_discovery_score_is_quantized_lexicographic():
      score = DiscoveryScore(
          median_excess_return_pp=Decimal("1.235").quantize(
              Decimal("0.01"), rounding=ROUND_HALF_EVEN
          ),
          worst_excess_return_pp=Decimal("0.50"),
          max_drawdown_magnitude_pp=Decimal("4.00"),
      )
      assert score.ordering_key == (
          Decimal("1.24"),
          Decimal("0.50"),
          Decimal("-4.00"),
      )


  def test_hidden_gate_has_no_sharpe_condition():
      decision = HoldoutDecision(
          excess_total_return_pp=Decimal("0.10"),
          closed_trades=3,
          safety_complete=True,
          integrity_complete=True,
          accounting_complete=True,
          long_replay_eligible=True,
      )
      assert decision.long_replay_eligible is True
      assert "sharpe" not in {field.name for field in fields(HoldoutDecision)}
  ```
- [ ] **Step 6: Implement DiscoveryScore and HoldoutDecision.** Candidate excess is always against the original authenticated fold baseline; incumbent deltas remain diagnostics. Run `python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_v2.py -k "objective or holdout"`; expected: objective/holdout cases pass.
- [ ] **Step 7: Write failing permanent-exposure ledger tests.** Key uniqueness uses only immutable bundle/universe/benchmark/warmup/session identity. Source, baseline policy, candidate, run, and exposure kind are stored metadata but never change uniqueness. Mark both discovery folds before baseline/reference aggregates can reach a role; assert a later run cannot reserve either as hidden. Reserve hidden before any baseline/candidate evaluation and leave it permanently consumed after injected failure.
- [ ] **Step 8: Implement the hash-chained locked ValidationLedger.** Exposure kinds are exactly candidate_validation, provider_context, and hidden_validation. mark_discovery/reserve_hidden append a permanent consumed record under exclusive lock; record_outcome appends only attempted/completed and a closed failure code, never metrics. The ledger is never provider-projected. Run `python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_v2.py -k "validation_ledger"`; expected: exposure/reuse cases pass.
- [ ] **Step 9: Run the focused v2 tests and commit.**

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_v2.py
      git add core/pit_optimization_contract.py core/pit_optimizer_evaluation.py tests/test_pit_optimizer_v2.py
      git commit -m "feat: define PIT optimizer v2 contracts"

---

### Task 5: Confine Model-Authored Policy Code and Attest Every Candidate

**Files:**

- Modify: core/strategy_policy/runtime.py
- Create: core/strategy_policy/worker.py
- Create: core/pit_optimizer_candidate.py
- Modify: agent_loop.py
- Modify: tests/test_pit_optimizer_v2.py
- Modify: tests/test_agent_loop.py

**Interfaces:**

- Produces: PatchBounds-aware parse/validate/apply path; PolicyWorkerRunner; JsonLinePolicyClient; authenticated JSON-lines policy protocol; CandidateIdentity; author-manifest validator; bounded provider source package.
- Consumes: authenticated baseline candidate export, author unified diff, incumbent cumulative diff, three policy paths, FoldManifest digest, constraint digest, and injected Docker/Git capabilities.

**Candidate limits:**

    editable_paths = (
        "core/strategy_policy/entry.py",
        "core/strategy_policy/risk.py",
        "core/strategy_policy/exit.py",
    )
    max_files = 3
    max_changed_lines = 200
    max_diff_bytes = 64 * 1024

**Candidate identity fields:**

- authenticated original source commit
- POLICY_INTERFACE_VERSION
- cumulative diff SHA-256
- sorted final editable-file SHA-256 map
- controller-derived changed paths and qualified function/constant symbols
- immutable-constraint digest
- discovery-fold-manifest digest

**Exact candidate interfaces:**

    # Import the sole PatchBounds definition from core.pit_optimization_contract.
    LEGACY_PATCH_BOUNDS = PatchBounds(4, 25, 400, 256 * 1024)
    PIT_OPTIMIZER_PATCH_BOUNDS = PatchBounds(3, 12, 200, 64 * 1024)

    @dataclass(frozen=True, slots=True)
    class CandidateIdentity:
        source_commit: str
        policy_interface_version: int
        cumulative_diff_sha256: str
        editable_file_sha256s: tuple[tuple[str, str], ...]
        changed_paths: tuple[str, ...]
        changed_symbols: tuple[str, ...]
        immutable_constraints_sha256: str
        discovery_manifest_sha256: str
        identity_sha256: str

    def validate_candidate_diff(
        *,
        authenticated_base_root: Path,
        candidate_root: Path,
        incremental_diff: str,
        git: GitCapability,
        bounds: PatchBounds,
        source_commit: str,
        policy_interface_version: int,
        immutable_constraints_sha256: str,
        discovery_manifest_sha256: str,
    ) -> tuple[CandidateIdentity, str]

    def validate_author_manifest(
        author: AuthorArtifact,
        candidate: CandidateIdentity,
    ) -> None

    WorkerBootstrap(
        schema_version: int,
        interface_version: int,
        nonce_b64: str,
        hmac_key_b64: str,
    )
    PolicyRequestEnvelope(
        sequence: int,
        previous_hmac_sha256: str,
        method: str,
        payload_sha256: str,
        payload: Mapping[str, object],
        hmac_sha256: str,
    )
    PolicyResponseEnvelope(
        sequence: int,
        request_hmac_sha256: str,
        method: str,
        payload_sha256: str,
        payload: Mapping[str, object],
        hmac_sha256: str,
    )
    PolicyWorkerRunner.start(
        *,
        candidate_root: Path,
        interface_version: int,
        fold_run_id: str,
    ) -> PolicyWorkerSession
    PolicyWorkerSession.call(
        method: str,
        snapshot: EntrySnapshot | CapacitySnapshot | AllocationSnapshot | EvictionSnapshot | ExitSnapshot,
    ) -> EntryDecision | CapacityDecision | AllocationDecision | EvictionDecision | ExitDecision
    PolicyWorkerSession.close() -> None
    JsonLinePolicyClient(
        session: PolicyWorkerSession,
        interface_version: int,
    )
    PolicyWorkerRunner.client_factory(
        *,
        candidate_root: Path,
        interface_version: int,
        fold_run_id: str,
    ) -> StrategyPolicyClientFactory

**Candidate helper skeleton:**

    def validate_policy_ast(*, path: str, source: str) -> None

    def derive_changed_symbols(
        *,
        before_sources: Mapping[str, str],
        after_sources: Mapping[str, str],
    ) -> tuple[str, ...]

    def derive_authenticated_cumulative_diff(
        *,
        git: GitCapability,
        authenticated_base_root: Path,
        candidate_root: Path,
        editable_paths: tuple[str, ...],
    ) -> str

    def build_policy_source_bundle(
        *,
        candidate_root: Path,
        cumulative_diff: str,
        policy_interface_version: int,
    ) -> PolicySourceBundle

    def require_source_context_fit(
        *,
        source_bundle: PolicySourceBundle,
        prior_iterations: tuple[IterationFeedbackSummary, ...],
        role_budget: PitOptimizerCallBudget,
    ) -> None

- [ ] **Step 1: Add failing diff, identity, and author-manifest tests.** Reject a fourth file, hard-limit line 201/byte 65537, first-canary line 81/byte 8193, tests, contracts/runtime/worker edits, binary/rename/symlink/submodule/generated changes, external dependencies, no-op/non-applicable patches, and an authenticated Git-derived cumulative diff that exceeds the supplied bounds even when the incremental diff fits. Prove caller-supplied cumulative text is never accepted, changed symbols come from before/after AST comparison, and controller-derived changed paths/symbols match AuthorArtifact exactly; mismatch returns author_manifest_mismatch and never reaches replay.

  ```python
  def test_changed_symbols_use_before_after_ast():
      path = "core/strategy_policy/risk.py"
      before = {path: "def recommend_capacity(snapshot):\n    return None\n"}
      after = {
          path: (
              "def recommend_capacity(snapshot):\n"
              "    return snapshot.configured_max_positions\n"
          )
      }
      assert derive_changed_symbols(
          before_sources=before,
          after_sources=after,
      ) == ("core.strategy_policy.risk.recommend_capacity",)
  ```
- [ ] **Step 2: Run the diff/identity slice and confirm the new tests fail.**

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_v2.py tests/test_agent_loop.py -k "patch_bounds or candidate_identity or author_manifest"

- [ ] **Step 3: Parameterize the existing parser and implement candidate identity.** Thread the sole PatchBounds through parse, validate, and apply. Preserve 4 files/25 hunks/400 lines/256 KiB for legacy callers. V2 always passes manifest.candidate_bounds, first proves it is within the hard 3-file/12-hunk/200-line/64-KiB ceiling, and applies the same bound to the raw author diff and a fresh Git-derived diff between authenticated_base_root and candidate_root. Derive changed paths/symbols from actual before/after sources and return the canonical cumulative diff with CandidateIdentity only after a clean apply. Re-run Step 2; expected: green.

  ```python
  _ALLOWED_PUBLIC = {
      "core/strategy_policy/entry.py": frozenset({"evaluate_entry"}),
      "core/strategy_policy/risk.py": frozenset(
          {"recommend_capacity", "recommend_allocation", "select_eviction"}
      ),
      "core/strategy_policy/exit.py": frozenset({"evaluate_exit"}),
  }

  def _symbol_nodes(path: str, source: str) -> dict[str, str]:
      tree = ast.parse(source, filename=path)
      nodes: dict[str, str] = {}
      for node in tree.body:
          if isinstance(node, ast.FunctionDef) and node.name in _ALLOWED_PUBLIC[path]:
              nodes[node.name] = ast.dump(node, include_attributes=False)
          elif (
              isinstance(node, ast.Assign)
              and len(node.targets) == 1
              and isinstance(node.targets[0], ast.Name)
              and node.targets[0].id.isupper()
          ):
              ast.literal_eval(node.value)
              nodes[node.targets[0].id] = ast.dump(node, include_attributes=False)
      return nodes

  def derive_changed_symbols(
      *,
      before_sources: Mapping[str, str],
      after_sources: Mapping[str, str],
  ) -> tuple[str, ...]:
      if tuple(sorted(before_sources)) != tuple(sorted(after_sources)):
          raise ValueError("policy source maps differ")
      changed: list[str] = []
      for path in sorted(before_sources):
          before = _symbol_nodes(path, before_sources[path])
          after = _symbol_nodes(path, after_sources[path])
          module = path.removesuffix(".py").replace("/", ".")
          changed.extend(
              f"{module}.{name}"
              for name in sorted(before.keys() | after.keys())
              if before.get(name) != after.get(name)
          )
      return tuple(changed)

  def _git_text(git: GitCapability, root: Path, *args: str) -> str:
      completed = _git(root, *args, timeout=30.0, git=git)
      return completed.stdout.decode("utf-8", errors="strict")

  def derive_authenticated_cumulative_diff(
      *,
      git: GitCapability,
      authenticated_base_root: Path,
      candidate_root: Path,
      editable_paths: tuple[str, ...],
  ) -> str:
      base_head = _git_text(
          git, authenticated_base_root, "rev-parse", "HEAD"
      ).strip()
      candidate_head = _git_text(
          git, candidate_root, "rev-parse", "HEAD"
      ).strip()
      if base_head != candidate_head:
          raise ValueError("candidate base commit mismatch")
      return _git_text(
          git,
          candidate_root,
          "diff",
          "--no-ext-diff",
          "--no-color",
          "HEAD",
          "--",
          *editable_paths,
      )
  ```

  Place `_git_text` and `derive_authenticated_cumulative_diff` beside the existing `_git`/GitCapability boundary in agent_loop.py; core/pit_optimizer_candidate.py receives only their already-derived canonical result inside the same trusted validation call. Do not add a second raw subprocess path or expose a public API that accepts caller-authored cumulative text.
- [ ] **Step 4: Add failing AST-purity tests.** Reject classes, mutable module values/defaults, global/nonlocal, input attribute/subscript writes, function attributes, reflection, clock/randomness, imports outside the contracts/math allowlist, I/O, environment, process, network, dynamic code, async/generator behavior, and unknown public symbols.
- [ ] **Step 5: Run the AST slice and confirm validate_policy_ast is missing.**

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_v2.py -k "ast_purity"

- [ ] **Step 6: Implement the closed AST allowlist plus before/after symbol derivation and rerun Step 5.** validate_policy_ast accepts only the protected contract imports, math, immutable literal constants, and declared pure policy functions. derive_changed_symbols compares canonical function/constant AST nodes in authenticated before/after maps and returns sorted qualified names. Expected: green.
- [ ] **Step 7: Add failing source-package and next-context tests.** Require exactly the three current-incumbent policy modules, declared symbols, hashes, and cumulative diff. Prove the complete bundle is at most 64 KiB and `initial_policy_bytes + 2 * max_diff_bytes + envelope_bytes <= 64 KiB` before bounds are sealed. Bound each IterationFeedbackSummary to 4 KiB, retained history to eight/32 KiB, and the first two-iteration canary to at most one prior summary. Reject raw data, trades, holdings, hidden folds, credentials, audit internals, local paths, unrelated source, truncation, and silent feedback dropping. Materialize the exact prospective next-incumbent bundle and distinguish candidate-attributable next_context_oversize from pre-call terminal context_budget_exhausted.
- [ ] **Step 8: Run the packaging slice and confirm it fails at the new helpers.**

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_v2.py -k "source_bundle or source_context or next_context"

- [ ] **Step 9: Implement build_policy_source_bundle and require_source_context_fit.** Add the protected contracts API/rule summary and immutable constraint IDs only in their dedicated role-input fields, construct all sections without truncation, render canonical UTF-8 bytes, and compare them with the manifest's section/input caps. Re-run Step 8; expected: green.
- [ ] **Step 10: Add failing worker-protocol and policy-client tests.** Cover bootstrap key/nonce lengths, request/response HMAC and payload hashes, strict sequence and chain continuity, method/payload pairing, the five-method allowlist, 16-KiB line limits, malformed/duplicate/unknown fields, repeated identical-snapshot probes around unrelated calls, and unconditional worker closure. Require JsonLinePolicyClient to implement every StrategyPolicyClient method with typed dispatch, and client_factory to start exactly one fresh session per fold run and close it through the simulator finally path.

  ```python
  class _RecordingSession:
      def __init__(self) -> None:
          self.calls: list[tuple[str, object]] = []
          self.closed = 0

      def call(self, method, snapshot):
          self.calls.append((method, snapshot))
          return CapacityDecision(max_positions=None, eviction_enabled=False)

      def close(self) -> None:
          self.closed += 1


  def test_json_line_policy_client_dispatches_and_closes_once():
      session = _RecordingSession()
      client = JsonLinePolicyClient(session=session, interface_version=1)
      snapshot = CapacitySnapshot(None, 25, 0, 3, 1.0, False)
      assert client.recommend_capacity(snapshot) == CapacityDecision(None, False)
      assert session.calls == [("recommend_capacity", snapshot)]
      client.close()
      client.close()
      assert session.closed == 1
  ```
- [ ] **Step 11: Run the protocol slice and confirm worker.py is incomplete.**

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_v2.py -k "worker_protocol or worker_nondeterminism"

- [ ] **Step 12: Implement the authenticated JSON-lines worker and JsonLinePolicyClient; rerun Step 11.** Create one fresh worker per fold. Generate a 32-byte HMAC key and 16-byte nonce per worker, deliver them once in the trusted bootstrap record, require monotonically increasing chained envelopes, and pass only the parsed snapshot to candidate functions. JsonLinePolicyClient supplies the five typed methods over PolicyWorkerSession.call and its close delegates exactly once; PolicyWorkerRunner.client_factory is the only evaluator adapter. Return the closed candidate nondeterminism failure for unequal probe output. Expected: green.

  ```python
  class JsonLinePolicyClient:
      def __init__(self, *, session, interface_version: int) -> None:
          self.interface_version = interface_version
          self._session = session
          self._closed = False

      def _call(self, method: str, snapshot, expected_type):
          if self._closed:
              raise RuntimeError("policy client is closed")
          value = self._session.call(method, snapshot)
          if not isinstance(value, expected_type):
              raise TypeError("policy response type mismatch")
          return value

      def evaluate_entry(self, snapshot):
          return self._call("evaluate_entry", snapshot, EntryDecision)

      def recommend_capacity(self, snapshot):
          return self._call("recommend_capacity", snapshot, CapacityDecision)

      def recommend_allocation(self, snapshot):
          return self._call("recommend_allocation", snapshot, AllocationDecision)

      def select_eviction(self, snapshot):
          return self._call("select_eviction", snapshot, EvictionDecision)

      def evaluate_exit(self, snapshot):
          return self._call("evaluate_exit", snapshot, ExitDecision)

      def close(self) -> None:
          if not self._closed:
              self._closed = True
              self._session.close()
  ```
- [ ] **Step 13: Add failing PolicyWorkerRunner command tests.** Assert --network none, --read-only, --user 65532:65532, --cap-drop ALL, --security-opt no-new-privileges, --pids-limit 32, --memory 256m, --cpus 1.0, --tmpfs /tmp:rw,noexec,nosuid,size=16m, Python -B, 16-KiB stdio lines, 64-KiB stderr, one-second method timeout, 900-second fold timeout, inherited-descriptor closure, and unconditional process cleanup. The mount allowlist contains one generated policy-only package with candidate policy plus trusted contracts/worker; it excludes repository, PIT bundle, baseline, artifact root, environment file, and credentials.
- [ ] **Step 14: Run the fake-process/command slice and confirm the runner assertions fail.**

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_agent_loop.py -k "policy_worker_command or policy_worker_cleanup or policy_worker_timeout"

- [ ] **Step 15: Implement PolicyWorkerRunner and rerun Step 14.** Build argv as a list, validate every host path before launch, expose only stdin/stdout/stderr, enforce byte/time/resource ceilings, and clean up on success, candidate failure, timeout, cancellation, or controller exception. No real replay or provider call is part of this step. Expected: green.
- [ ] **Step 16: Run the complete candidate-confinement slice.**

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_v2.py tests/test_agent_loop.py -k "policy_worker or patch_bounds or candidate_identity or ast_purity or source_bundle or pit_optimizer_v2"

- [ ] **Step 17: Commit candidate confinement.**

      git add core/strategy_policy/runtime.py core/strategy_policy/worker.py core/pit_optimizer_candidate.py agent_loop.py tests/test_pit_optimizer_v2.py tests/test_agent_loop.py
      git commit -m "feat: confine and attest optimizer candidates"

---

### Task 6: Extend Provider Budgeting and Audit for All-R1 Iterations

**Files:**

- Create: core/pit_optimizer_authorization.py
- Modify: agent_loop.py
- Modify: core/pit_optimization_contract.py
- Modify: tests/test_agent_loop.py
- Modify: tests/test_pit_optimizer_v2.py

**Interfaces:**

- Produces: request_pit_optimizer_once; one frozen R1 pricing identity per run; explicit grant/window recording CLI/API; non-spend exclusive run lease; exact one-call reservation/reconciliation; optimizer-v2 role/model profile; iteration-aware ProviderCallRecord and audit events.
- Consumes: one sealed PitOptimizerCallBudget, one explicit OperatorAuthorizationWindow bound to the manifest requirement, canonical role input, strict parser, BudgetLedger, AuditTrail, and wall deadline.

**Gateway interface:**

    OpenRouterGateway.request_pit_optimizer_once(
        role,
        dynamic_input,
        parser,
        *,
        call_budget,
        authorization_lease,
        frozen_pricing,
        wall_deadline,
        monotonic,
    ) -> PitOptimizerRoleCall

All three optimizer-v2 roles map to deepseek/deepseek-r1 without changing the general loop's orchestrator/reasoner/coder models.

**Authorization interfaces:**

    @dataclass(frozen=True, slots=True)
    class FrozenModelPricing:
        model: str
        prompt_per_million: Decimal
        completion_per_million: Decimal
        pricing_payload_sha256: str

        @property
        def pricing_sha256(self) -> str

    @dataclass(frozen=True, slots=True)
    class OperatorAuthorizationGrant:
        grant_id: str
        additional_calls: int
        additional_tokens: int
        additional_usd: float
        policy_source_scope_sha256: str

    @dataclass(frozen=True, slots=True)
    class OperatorAuthorizationWindow:
        window_id: str
        grant_ids: tuple[str, ...]
        authorization_requirement_sha256: str
        max_calls: int
        max_tokens: int
        max_usd: float
        policy_source_scope_sha256: str

    @dataclass(frozen=True, slots=True)
    class AuthorizationRunLease:
        lease_id: str
        one_shot_key_sha256: str
        window_id: str
        run_manifest_sha256: str
        frozen_pricing_sha256: str
        max_calls: int
        max_tokens: int
        max_usd: float

    @dataclass(frozen=True, slots=True)
    class AuthorizationCallReservation:
        reservation_id: str
        lease_id: str
        call_index: int
        iteration: int
        role: str
        reserved_tokens: int
        reserved_usd: float

    @dataclass(frozen=True, slots=True)
    class PitOptimizerProviderFacts:
        call_index: int
        iteration: int
        role: str
        requested_model: str
        returned_model: str | None
        frozen_pricing_sha256: str
        outcome: str
        request_started: bool
        response_received: bool
        finish_reason: str | None
        response_schema_valid: bool
        accounting_complete: bool
        prompt_tokens: int | None
        completion_tokens: int | None
        total_tokens: int | None
        cost_usd: float | None
        retained_reservation_tokens: int
        retained_reservation_usd: float
        audit_sha256: str

    @dataclass(frozen=True, slots=True)
    class PitOptimizerRoleCall:
        plan: PitOptimizerCallBudget
        payload: InvestigatorArtifact | AuthorArtifact | CriticArtifact
        facts: PitOptimizerProviderFacts

    AuthorizationLedger.append_grant(
        grant: OperatorAuthorizationGrant,
        *,
        operator_approval_reference: str,
    ) -> None
    AuthorizationLedger.bind_window(
        *,
        window: OperatorAuthorizationWindow,
        requirement: AuthorizationRequirement,
        operator_approval_reference: str,
    ) -> None
    AuthorizationLedger.open_run_lease(
        *,
        window_id: str,
        authorization_requirement_sha256: str,
        run_manifest_sha256: str,
        frozen_pricing_sha256: str,
    ) -> AuthorizationRunLease
    AuthorizationLedger.reserve_call(
        lease: AuthorizationRunLease,
        plan: PitOptimizerCallBudget,
    ) -> AuthorizationCallReservation
    AuthorizationLedger.reconcile_call(
        reservation: AuthorizationCallReservation,
        provider_facts: PitOptimizerProviderFacts,
    ) -> None
    AuthorizationLedger.close_run_lease(
        lease: AuthorizationRunLease,
        *,
        terminal_code: str,
    ) -> None

    record_authorized_grant(
        *,
        ledger_path: Path,
        manifest_path: Path,
        manifest_sha256: str,
        grant: OperatorAuthorizationGrant,
        operator_approval_reference: str,
    ) -> OperatorAuthorizationWindow

    OpenRouterGateway.freeze_pit_optimizer_pricing(
        *,
        model: str,
        wall_deadline: float,
        monotonic: Callable[[], float],
    ) -> FrozenModelPricing

    def freeze_pricing_record(
        model: str,
        value: Mapping[str, Any] | Pricing,
    ) -> FrozenModelPricing

    def preflight_pit_optimizer_call(
        *,
        static_bytes: bytes,
        dynamic_bytes: bytes,
        call_budget: PitOptimizerCallBudget,
        lease: AuthorizationRunLease,
        pricing: FrozenModelPricing,
    ) -> Decimal

    def conservative_call_cost_usd(
        *,
        rendered_prompt_bytes: int,
        max_output_tokens: int,
        pricing: FrozenModelPricing,
    ) -> Decimal

    def require_authorized_policy_source_scope(
        manifest: PitOptimizerRunManifest,
        requirement: AuthorizationRequirement,
        window: OperatorAuthorizationWindow,
    ) -> str

The side-effecting `python -B -m core.pit_optimizer_authorization record-grant` CLI maps one-to-one to this function, but Task 9 does not print or run that command. It can be rendered only after a later fresh authorization supplies the exact grant ID, call/token/USD ceilings, and non-secret approval reference. It reads the manifest's source scope and AuthorizationRequirement, appends exactly one named grant, and binds exactly the declared window under the ledger lock. It refuses values outside the approved ceilings or scope and never reads an OpenRouter credential.

- [ ] **Step 1: Write failing grant/window tests.** Seed the append-only ledger with the reconciled old 20-call grant showing 19 used. Assert its one remaining call is not silently combined with a new run. record-grant must require an authenticated manifest/requirement, exact scope, a non-secret approval reference, and fresh explicit call/token/USD values. A six-call window cannot start a seventh call, and the most restrictive remaining ceiling across named grants, window, CLI, and manifest wins.

  The Task 4 contract test creates one canonical `v2_manifest` fixture by calling `build_subset_manifest` with its synthetic files; add this exact scope assertion:

  ```python
  def test_authorized_source_scope_must_match_manifest(v2_manifest):
      requirement = v2_manifest.authorization_requirement
      window = OperatorAuthorizationWindow(
          window_id=requirement.window_id,
          grant_ids=("grant-v2",),
          authorization_requirement_sha256=requirement.sha256,
          max_calls=requirement.max_calls,
          max_tokens=requirement.max_tokens,
          max_usd=requirement.max_usd,
          policy_source_scope_sha256=requirement.policy_source_scope_sha256,
      )
      assert require_authorized_policy_source_scope(
          v2_manifest, requirement, window
      ) == v2_manifest.policy_source_scope.sha256
      with pytest.raises(AuthorizationError, match="policy source scope"):
          require_authorized_policy_source_scope(
              v2_manifest,
              requirement,
              replace(window, policy_source_scope_sha256="f" * 64),
          )
  ```
- [ ] **Step 2: Implement append_grant, bind_window, and the record-grant CLI.** Store the hash-chained ledger at permanent_runtime_root/pit_optimizer_authorization_ledger.jsonl under the existing cross-platform exclusive-file-lock pattern. Prepare never invokes these methods. A chat message is not inferred by code; only a separately operator-authorized invocation may append/bind.

  Call this helper from both bind_window and open_run_lease before a ledger append:

  ```python
  def require_authorized_policy_source_scope(manifest, requirement, window):
      expected = manifest.policy_source_scope.sha256
      if requirement.sha256 != manifest.authorization_requirement.sha256:
          raise AuthorizationError("authorization requirement mismatch")
      if window.authorization_requirement_sha256 != requirement.sha256:
          raise AuthorizationError("authorization window requirement mismatch")
      if (
          requirement.policy_source_scope_sha256 != expected
          or window.policy_source_scope_sha256 != expected
      ):
          raise AuthorizationError("policy source scope mismatch")
      return expected
  ```
- [ ] **Step 3: Write failing run-lease lifecycle and frozen-pricing tests.** freeze_pit_optimizer_pricing loads deepseek/deepseek-r1 pricing once during live canary setup, validates finite nonnegative Decimal rates, stores only the model/rates/payload digest, and makes no role call or source transmission. open_run_lease proves the complete plan fits but debits no calls/tokens/USD; it binds frozen_pricing_sha256 and atomically appends a one-shot key over window_id+run_manifest_sha256 before returning. Concurrent use fails, and a closed/failed/cancelled lease with that key can never reopen, so a provider failure cannot be retried under the same run. Cancellation or early stop closes the lease and releases unused future allowance. Authoritative spent calls remain spent; uncertain per-call reservations remain retained. Reusing any remaining grant capacity requires a newly authenticated manifest, new AuthorizationRequirement/window ID, newly frozen pricing, and separately bound run; prior spent call indexes cannot be replayed.
- [ ] **Step 4: Implement frozen pricing, the one-shot non-spend run lease, and exact call reservation types above.** Reuse the existing bounded pricing loader, freeze one canonical record before opening the lease, and refuse any later model/rate identity change. reserve_call permits only the next never-before-started planned call, rejects a second active call reservation or any reused (manifest, call_index), and reserves exactly max_input_tokens+max_output_tokens plus that call's max_usd. Never reserve future calls or the complete run spend.

  ```python
  def freeze_pricing_record(model: str, value: Mapping[str, Any] | Pricing):
      parsed = Pricing.from_value(value)
      payload = {
          "model": model,
          "prompt_per_million": str(parsed.prompt_per_million),
          "completion_per_million": str(parsed.completion_per_million),
      }
      payload_bytes = json.dumps(
          payload,
          sort_keys=True,
          separators=(",", ":"),
      ).encode("utf-8")
      return FrozenModelPricing(
          model=model,
          prompt_per_million=Decimal(payload["prompt_per_million"]),
          completion_per_million=Decimal(payload["completion_per_million"]),
          pricing_payload_sha256=hashlib.sha256(payload_bytes).hexdigest(),
      )
  ```
- [ ] **Step 5: Write failing pre-send and fake-client accounting tests.** Render the exact system messages/response schema and dynamic role JSON separately; require each byte count within max_static_input_bytes/max_dynamic_input_bytes, their combined canonical UTF-8 bytes <= max_input_tokens, and the validated response byte limit. Compute `((prompt_bytes * prompt_per_million) + (max_output_tokens * completion_per_million)) / 1_000_000` with Decimal from the lease-bound FrozenModelPricing and require it <= call_budget.max_usd before authorization reservation, SDK client construction, or transmission. Assert a one-cent excess starts no paid call. Send max_tokens=max_output_tokens. Cover authoritative accepted and schema-invalid responses, actual usage over a per-call cap, failure before send, pricing identity drift, and uncertain post-send accounting.

  ```python
  def test_call_preflight_uses_lease_bound_frozen_pricing():
      pricing = freeze_pricing_record(
          "deepseek/deepseek-r1",
          {"prompt": 2, "completion": 8},
      )
      lease = AuthorizationRunLease(
          lease_id="lease-v2",
          one_shot_key_sha256="a" * 64,
          window_id="window-v2",
          run_manifest_sha256="b" * 64,
          frozen_pricing_sha256=pricing.pricing_sha256,
          max_calls=6,
          max_tokens=448_000,
          max_usd=0.40,
      )
      budget = PitOptimizerCallBudget(
          call_index=1,
          iteration=1,
          role="investigator",
          model="deepseek/deepseek-r1",
          max_static_input_bytes=400,
          max_dynamic_input_bytes=600,
          max_input_tokens=1_000,
          max_output_tokens=1_000,
          max_response_bytes=8_192,
          max_usd=0.009999,
      )
      with pytest.raises(BudgetExceededError, match="per-call USD"):
          preflight_pit_optimizer_call(
              static_bytes=b"s" * 400,
              dynamic_bytes=b"d" * 600,
              call_budget=budget,
              lease=lease,
              pricing=pricing,
          )
  ```

  The conservative cost is exactly USD 0.010000, so the fake SDK client, both ledgers, and request-start audit list must remain untouched.
- [ ] **Step 6: Extend BudgetLedger and AuthorizationLedger preflight/reconciliation.** Before send, use the same frozen rates in BudgetLedger, reserve the conservative computed amount there, reserve the sealed max_usd in AuthorizationLedger, and persist both records. On authoritative facts, commit actual usage/cost and release unused reservation before validating caps; an overage stays committed and terminates. If a request may have reached the provider without authoritative accounting, retain the full one-call reservations permanently and terminate. Failure before reservation/send commits nothing.

  ```python
  def preflight_pit_optimizer_call(
      *,
      static_bytes,
      dynamic_bytes,
      call_budget,
      lease,
      pricing,
  ):
      if call_budget.model != pricing.model:
          raise AuthorizationError("frozen pricing model mismatch")
      if lease.frozen_pricing_sha256 != pricing.pricing_sha256:
          raise AuthorizationError("frozen pricing identity drift")
      if len(static_bytes) > call_budget.max_static_input_bytes:
          raise BudgetExceededError("static input byte cap exceeded")
      if len(dynamic_bytes) > call_budget.max_dynamic_input_bytes:
          raise BudgetExceededError("dynamic input byte cap exceeded")
      prompt_bytes = len(static_bytes) + len(dynamic_bytes)
      if prompt_bytes > call_budget.max_input_tokens:
          raise BudgetExceededError("conservative input-token cap exceeded")
      cost = conservative_call_cost_usd(
          rendered_prompt_bytes=prompt_bytes,
          max_output_tokens=call_budget.max_output_tokens,
          pricing=pricing,
      )
      if cost > Decimal(str(call_budget.max_usd)):
          raise BudgetExceededError("per-call USD cap exceeded")
      return cost
  ```

  request_pit_optimizer_once calls this helper before BudgetLedger.reserve, AuthorizationLedger.reserve_call, SDK construction, or transmission.
- [ ] **Step 7: Add the isolated optimizer-v2 role profile.** Use R1 for investigator/author/critic, max_attempts=1, SDK max_retries=0, allow_generation_recovery=False, and no response-healing plugin.
- [ ] **Step 8: Extend closed accounting roles/states and the durable wrapper.** Add CALL_INVESTIGATOR, CALL_AUTHOR, CALL_CRITIC; accept legacy/v2 role sets by schema version; pass the real iteration into ProviderCallRecord. Persist reservation/start before transmission and terminal provider facts/audit before the next call. Provider contents stay out of user-facing accounting. Malformed response or uncertain accounting is terminal and cannot become a disguised retry.
- [ ] **Step 9: Run the focused provider/audit tests.**

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_agent_loop.py tests/test_pit_optimizer_v2.py -k "pit_optimizer_v2 or call_budget or provider_call or budget_reservation"

- [ ] **Step 10: Commit provider integration.**

      git add core/pit_optimizer_authorization.py agent_loop.py core/pit_optimization_contract.py tests/test_agent_loop.py tests/test_pit_optimizer_v2.py
      git commit -m "feat: account all-R1 optimizer iterations"

---

### Task 7: Implement the Incumbent Loop, Incremental Artifacts, and Hidden Boundary

**Files:**

- Create: core/pit_optimizer_artifacts.py
- Create: core/pit_optimizer_controller.py
- Modify: core/pit_optimization.py
- Modify: core/pit_optimizer_evaluation.py
- Create: tests/test_pit_optimizer_loop.py
- Modify: tests/test_pit_optimizer_v2.py

**Interfaces:**

- Produces: PitOptimizerReadiness; PitOptimizerServices; PitOptimizerResult; incremental artifact store; schema-v2 prepare and incumbent-loop entry points.
- Consumes: authenticated fixed discovery baselines, source/bundle/fold/policy identities, authorization window, candidate/evaluation services, validation ledger, and cancellation signal.

**Controller interfaces:**

    @dataclass(frozen=True, slots=True)
    class PitOptimizerReadiness:
        schema_version: int
        manifest: PitOptimizerRunManifest
        manifest_sha256: str
        readiness_sha256: str
        artifact_path: Path
        parity: ParityAttestation
        baseline_discovery: DiscoveryEvidenceSummary
        provider_seed: ProviderSeed

    @dataclass(frozen=True, slots=True)
    class CandidateWorkspace:
        workspace_id: str
        root: Path

    @dataclass(frozen=True, slots=True)
    class CandidateValidationOutcome:
        valid: bool
        failure_code: str | None
        incremental_diff: str
        cumulative_diff: str
        identity: CandidateIdentity | None
        changed_paths: tuple[str, ...]
        changed_symbols: tuple[str, ...]

    @dataclass(frozen=True, slots=True)
    class OptimizerBudgetSummary:
        api_calls: int
        prompt_tokens: int
        completion_tokens: int
        total_tokens: int
        authoritative_usd: float
        retained_reservation_tokens: int
        retained_reservation_usd: float
        incomplete_accounting_calls: int

    @dataclass(frozen=True, slots=True)
    class PitOptimizerServices:
        freeze_pricing: Callable[[str], FrozenModelPricing]
        open_run_lease: Callable[[PitOptimizerReadiness, FrozenModelPricing], AuthorizationRunLease]
        close_run_lease: Callable[[AuthorizationRunLease, str], None]
        call_role: Callable[[PitOptimizerCallBudget, InvestigatorInput | AuthorInput | CriticInput, Callable[[str], object], AuthorizationRunLease, FrozenModelPricing], PitOptimizerRoleCall]
        create_candidate: Callable[[str | None], CandidateWorkspace]
        validate_and_apply: Callable[[CandidateWorkspace, AuthorArtifact, str | None], CandidateValidationOutcome]
        evaluate_discovery: Callable[[CandidateWorkspace, CandidateIdentity], DiscoveryEvaluation]
        confirm_discovery: Callable[[CandidateWorkspace, CandidateIdentity, str], DeterminismAttestation]
        reserve_hidden_validation: Callable[[CandidateIdentity], ValidationReservation]
        evaluate_hidden: Callable[[CandidateWorkspace, CandidateIdentity, ValidationReservation], HiddenEvaluation]
        dispose_candidate: Callable[[CandidateWorkspace], PitOptimizerCleanup]
        verify_inputs: Callable[[PitOptimizerReadiness], None]
        cancellation_requested: Callable[[], bool]
        write_json_artifact: Callable[[str, Mapping[str, object]], tuple[Path, str]]
        write_diff_artifact: Callable[[str, str], tuple[Path, str]]

    @dataclass(frozen=True, slots=True)
    class PitOptimizerResult:
        schema_version: int
        phase: str
        status: str
        terminal_code: str
        terminal_detail: str | None
        exit_code: int
        run_id: str
        readiness_sha256: str
        manifest_sha256: str
        iterations_started: int
        iterations_completed: int
        valid_evaluations: int
        incumbent_updates: int
        non_improving_streak: int
        discovery_winner: CandidateIdentity | None
        hidden_validation_opened: bool
        validation_reservation_sha256: str | None
        long_replay_eligible: bool | None
        budget: OptimizerBudgetSummary
        artifact_root: Path
        artifact_paths: tuple[tuple[Path, str], ...]
        source_modified: bool
        cleanup_complete: bool

        def to_public_artifact(self) -> Mapping[str, object]

    def prepare_pit_optimizer_v2(
        config: PitOptimizerGateConfig,
        *,
        source_root: Path,
        artifact_root: Path,
        permanent_runtime_root: Path,
        source_head: str,
        source_fingerprint_sha256: str,
    ) -> PitOptimizerReadiness

    def run_pit_optimizer_v2(
        *,
        readiness: PitOptimizerReadiness,
        services: PitOptimizerServices,
    ) -> PitOptimizerResult

    FoldEvaluationResult(
        fold_id: str,
        engine_policy_sha256: str,
        candidate_identity_sha256: str,
        aggregate_metrics: FoldAggregateSummary,
    )
    DiscoveryComparison(
        candidate_vs_fixed_baseline: DiscoveryScore,
        candidate_vs_incumbent_diagnostics: DiscoveryScore,
        rankable: bool,
        strictly_improves_incumbent: bool,
    )
    DiscoveryEvaluation(
        folds: tuple[FoldEvaluationResult, ...],
        comparison: DiscoveryComparison,
    )
    DeterminismAttestation(
        fold_id: str,
        expected_evidence_sha256: str,
        repeated_evidence_sha256: str,
        matched: bool,
    )
    HiddenEvaluation(
        baseline_aggregate: FoldAggregateSummary,
        candidate_aggregate: FoldAggregateSummary,
        decision: HoldoutDecision,
    )
    PitOptimizerCleanup(
        candidate_removed: bool,
        worker_stopped: bool,
        source_modified: bool,
    )
    OptimizerTerminalCode = {
        "iteration_limit",
        "budget_exhausted",
        "stagnation_limit",
        "cancelled",
        "provider_protocol_failure",
        "provider_accounting_failure",
        "audit_failure",
        "authorization_exhausted",
        "identity_drift",
        "trusted_evaluator_nondeterminism",
        "sandbox_integrity_failure",
        "evidence_tampering",
    }

`status` is exactly ready, long_replay_eligible, loop_verified_no_long_replay_candidate, or aborted. A pre-call context overflow uses terminal_code=budget_exhausted and terminal_detail=context_budget_exhausted; it never becomes candidate feedback or a provider call.

PitOptimizerReadiness.readiness_sha256 is the only readiness self-digest. The create-only readiness JSON serializes neither readiness_sha256 nor runtime-only artifact_path; its exact canonical file-byte digest becomes readiness_sha256 when read into the runtime dataclass. CandidateWorkspace is an inert reference: the concrete agent_loop.Candidate capability remains in a private PitOptimizerServices registry keyed by workspace_id. validate_and_apply and dispose_candidate must resolve that existing capability from the registry and can never reconstruct authority from root; disposal atomically removes the registry entry. The five evaluation dataclasses shown above live in core/pit_optimizer_evaluation.py and are committed with this task.

**State-machine invariants:**

- A complete iteration is investigator, author, local validation/discovery evaluation, then critic.
- Every nonterminal author outcome receives a critic, including static rejection, syntax/import failure, worker failure, timeout, zero trades, or underperformance.
- Provider/protocol/audit/identity failure is terminal and may leave a partial iteration.
- The controller compares a valid candidate only after the critic artifact is durably persisted.
- The incumbent starts at the fixed baseline and candidates build on the current best cumulative diff.
- Every candidate excess return and DiscoveryScore uses the original authenticated fold baselines carried by the final parity attestation/readiness. Candidate-versus-incumbent deltas are diagnostics only and never become the objective reference.
- Stop at min(configured iterations, floor(authorized calls / 3), 8), or after three consecutive valid evaluated non-improvements. Invalid candidates do not increment the valid-non-improvement counter.
- Token/cost/call/context exhaustion checked before a complete iteration is a normal budget_exhausted stop. User cancellation is cancelled. Trusted evaluator nondeterminism, evidence tampering, sandbox escape/integrity failure, identity drift, audit failure, provider protocol failure, and uncertain accounting are terminal and never become critic feedback. Every terminal path closes the non-spend run lease and releases only unused future allowance; spent calls and retained uncertain reservations remain charged.

**Exact local artifact layout:**

    run.json
    baseline.json
    accounting.json
    iterations/001/investigator.json
    iterations/001/author.json
    iterations/001/candidate.diff
    iterations/001/validation.json
    iterations/001/discovery.json
    iterations/001/critic.json
    iterations/001/decision.json
    incumbent.diff
    holdout.json
    summary.json

Create holdout.json only when hidden validation is opened. Repeat the numbered iteration directory for every started iteration and retain partial contents on a terminal provider/integrity failure.

Every artifact has schema_version=2 and exact top-level keys:

- run.json: manifest-sealed run_id, complete manifest/source/readiness/bundle/baseline/effective-policy/parity/fold identities, policy_interface_version, candidate_bounds, authorization requirement/window/lease IDs, frozen pricing identity, status;
- baseline.json: discovery fold IDs, final-parity-attested per-fold aggregate metrics, universe/warmup identities, engine_policy_sha256, parity_attestation_sha256;
- accounting.json: call records, authorized/reserved/actual call-token-USD totals, incomplete exposure, audit-chain head;
- investigator.json / author.json / critic.json: the validated closed role artifact plus call index/iteration and payload digest, never raw response envelopes;
- validation.json: candidate failure code or CandidateIdentity, author_manifest_matches, focused-check results, worker attestation;
- discovery.json: fixed-baseline comparisons, incumbent diagnostics, per-fold aggregates, engine_policy_sha256, candidate_identity_sha256;
- decision.json: rankable, quantized score, prior/new incumbent identity, deterministic controller decision;
- holdout.json: consumed validation key, baseline/candidate aggregates, both identities, eligibility checks;
- summary.json: terminal status/code, iterations, incumbent, discovery/hidden outcome, exact accounting, cleanup, source_modified, artifact digests.

**Controller implementation skeleton:**

    @dataclass(slots=True)
    class _RunState:
        run_id: str
        next_iteration: int
        incumbent_workspace: CandidateWorkspace | None
        incumbent_identity: CandidateIdentity | None
        incumbent_cumulative_diff: str
        incumbent_discovery: DiscoveryEvidenceSummary
        prior_iterations: tuple[IterationFeedbackSummary, ...]
        valid_evaluations: int
        incumbent_updates: int
        non_improving_streak: int
        provider_enabled: bool
        frozen_pricing: FrozenModelPricing | None
        authorization_lease: AuthorizationRunLease | None

    @dataclass(frozen=True, slots=True)
    class _IterationOutcome:
        completed: bool
        terminal_code: str | None
        feedback: IterationFeedbackSummary | None
        candidate_workspace: CandidateWorkspace | None
        candidate_identity: CandidateIdentity | None
        discovery: DiscoveryEvaluation | None
        incumbent_changed: bool

    _initialize_run_artifacts(readiness, state, services) -> None
    _initialize_provider(readiness, state, services) -> None
    _baseline_from_parity(parity: ParityAttestation) -> DiscoveryEvidenceSummary
    _run_investigator(readiness, state, services) -> PitOptimizerRoleCall
    _run_author(readiness, state, investigator, services) -> PitOptimizerRoleCall
    _validate_iteration_candidate(readiness, state, author, services) -> CandidateValidationOutcome
    _evaluate_iteration_candidate(readiness, state, validation, services) -> DiscoveryEvaluation | None
    _run_critic(readiness, state, investigator, author, validation, discovery, services) -> PitOptimizerRoleCall
    _persist_iteration_decision(readiness, state, validation, discovery, critic, services) -> _IterationOutcome
    _call_role(readiness, state, services, plan, role_input, parser) -> PitOptimizerRoleCall
    _run_iteration(readiness, state, services) -> _IterationOutcome
    _finish_discovery(readiness, state, services) -> DeterminismAttestation | None
    _run_hidden_once(readiness, state, services) -> HiddenEvaluation
    _dispose_all_candidates_and_workers(state, services) -> PitOptimizerCleanup
    _replace_accounting_artifact(state, services) -> None
    _build_final_result(*, readiness, state, terminal_code, cleanup) -> PitOptimizerResult
    _finalize_result(readiness, state, services, terminal_code) -> PitOptimizerResult

- [ ] **Step 1: Add failing provider-free prepare tests.** Authenticate clean source, manifest, final parity, and every embedded ParityFoldEvidence self-digest; reject tracked drift or nested evidence mismatch. Require baseline_discovery/provider_seed to be derived only from attested fold aggregates/funnel/exit attribution. Spy that both discovery identities are permanently marked before any aggregate is provider-projectable. Assert prepare performs zero replay, hidden evaluation, gateway construction, authorization lease, grant mutation, or provider call, and that no hidden ID/date/metric enters ProviderSeed.

  Task 2's parity test exposes a real `final_parity` fixture built from two small ParityFoldEvidence values; add this pure projection anchor:

  ```python
  def test_prepare_baseline_comes_from_final_parity(final_parity):
      baseline = _baseline_from_parity(final_parity)
      assert baseline.folds == tuple(
          item.aggregate for item in final_parity.final_discovery_evidence
      )
      assert baseline.evidence_ids == tuple(
          item.evidence_sha256 for item in final_parity.final_discovery_evidence
      )
  ```
- [ ] **Step 2: Run the prepare slice and confirm prepare_pit_optimizer_v2 fails closed.**

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_loop.py tests/test_pit_optimizer_v2.py -k "prepare_v2 or prepare_parity or prepare_discovery_exposure"

- [ ] **Step 3: Implement provider-free prepare and rerun Step 2.** Recompute nested hashes, enforce the complete identity graph, derive the two fixed discovery baselines from final parity evidence, mark discovery exposure, and atomically write readiness. Do not instantiate or invoke any live/provider/replay service. Expected: green.

  ```python
  def _baseline_from_parity(parity: ParityAttestation):
      evidence = parity.final_discovery_evidence
      return DiscoveryEvidenceSummary(
          folds=tuple(item.aggregate for item in evidence),
          score=None,
          evidence_ids=tuple(item.evidence_sha256 for item in evidence),
      )
  ```
- [ ] **Step 4: Add failing artifact-initialization, pricing, capability, and run-lease tests.** Require one frozen R1 pricing record and then the non-spend one-shot lease before the first call; store both in _RunState and pass both to every call_role. Atomically create run.json, baseline.json, and accounting.json with complete identities; create iterations/NNN before its investigator; refuse overwrite/partial fsync; and prevent state advance after an artifact failure. Assert the initial files contain no provider bodies or hidden results. Require manifest.run_id as the sole run ID. Prove validate/dispose resolve the existing candidate capability by workspace_id, reject an unknown/removed ID, and never recreate it from root.

  ```python
  def test_initialize_freezes_pricing_before_opening_lease():
      events: list[str] = []
      pricing = freeze_pricing_record(
          "deepseek/deepseek-r1",
          {"prompt": 1, "completion": 2},
      )
      lease = Mock(spec=AuthorizationRunLease)
      readiness = Mock(spec=PitOptimizerReadiness)
      readiness.manifest.model = "deepseek/deepseek-r1"
      state = Mock(spec=_RunState)
      services = Mock(spec=PitOptimizerServices)
      services.freeze_pricing.side_effect = lambda model: events.append("pricing") or pricing
      services.open_run_lease.side_effect = lambda ready, frozen: events.append("lease") or lease
      _initialize_provider(readiness, state, services)
      assert events == ["pricing", "lease"]
      assert state.frozen_pricing is pricing
      assert state.authorization_lease is lease
  ```
- [ ] **Step 5: Run the artifact slice and confirm the store/controller seams fail.**

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_loop.py -k "artifact_initialization or run_lease or artifact_failure"

- [ ] **Step 6: Implement _initialize_run_artifacts, the private capability registry, and create/replace/fsync store primitives.** Freeze pricing, open and retain the run lease, create the three root artifacts from readiness.manifest.run_id, and expose create-only iteration paths plus atomic replacement for accounting/incumbent state. Candidate services retain the capability keyed by workspace_id until exactly one disposal. Re-run Step 5; expected: green.

  ```python
  def _initialize_provider(readiness, state, services):
      pricing = services.freeze_pricing(readiness.manifest.model)
      lease = services.open_run_lease(readiness, pricing)
      state.frozen_pricing = pricing
      state.authorization_lease = lease
  ```

  _initialize_run_artifacts calls `_initialize_provider` before its first create-only artifact and includes both identities in run.json; if any subsequent create/fsync fails, its outer finally closes the retained lease.
- [ ] **Step 7: Add failing single-iteration role/validation branches.** For a valid mocked candidate require investigator then author, fresh authenticated-base-plus-incumbent materialization, rendered-size check, exact reservation/call persistence, author-manifest match, candidate.diff, validation.json, focused checks selected only by changed symbols, two fresh discovery-fold workers, and then critic. A disallowed patch skips replay but still receives critic feedback and keeps the incumbent. A malformed author response terminates after two calls without critic because protocol integrity failed.

  ```python
  def test_one_iteration_orders_critic_before_decision(monkeypatch):
      events: list[str] = []
      investigator_call = Mock(spec=PitOptimizerRoleCall)
      author_call = Mock(spec=PitOptimizerRoleCall)
      validation = Mock(spec=CandidateValidationOutcome)
      discovery = Mock(spec=DiscoveryEvaluation)
      critic_call = Mock(spec=PitOptimizerRoleCall)
      outcome = Mock(spec=_IterationOutcome)
      monkeypatch.setattr(controller, "_run_investigator", lambda *a: events.append("investigator") or investigator_call)
      monkeypatch.setattr(controller, "_run_author", lambda *a: events.append("author") or author_call)
      monkeypatch.setattr(controller, "_validate_iteration_candidate", lambda *a: events.append("validate") or validation)
      monkeypatch.setattr(controller, "_evaluate_iteration_candidate", lambda *a: events.append("discovery") or discovery)
      monkeypatch.setattr(controller, "_run_critic", lambda *a: events.append("critic") or critic_call)
      monkeypatch.setattr(controller, "_persist_iteration_decision", lambda *a: events.append("decision") or outcome)
      assert _run_iteration(Mock(), Mock(), Mock()) is outcome
      assert events == ["investigator", "author", "validate", "discovery", "critic", "decision"]
  ```
- [ ] **Step 8: Run the single-iteration slice and confirm _run_iteration is incomplete.**

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_loop.py -k "single_iteration or invalid_author or malformed_author or focused_candidate_checks"

- [ ] **Step 9: Implement _run_iteration through durable critic feedback and rerun Step 8.** Persist investigator/author before consuming their outputs. On a safe candidate failure, build the closed sanitized CriticInput and persist critic/decision; on protocol, audit, identity, or sandbox integrity failure, terminate immediately. For valid candidates, run only syntax/import, purity/determinism, and the entry/risk/exit tests selected by changed symbols, then evaluate both discovery folds with fresh simulators/workers against the fixed baseline and persist discovery.json before critic. Expected: green.

  ```python
  def _run_iteration(readiness, state, services):
      investigator = _run_investigator(readiness, state, services)
      author = _run_author(readiness, state, investigator, services)
      validation = _validate_iteration_candidate(
          readiness, state, author, services
      )
      discovery = _evaluate_iteration_candidate(
          readiness, state, validation, services
      )
      critic = _run_critic(
          readiness,
          state,
          investigator,
          author,
          validation,
          discovery,
          services,
      )
      return _persist_iteration_decision(
          readiness, state, validation, discovery, critic, services
      )
  ```

  `_evaluate_iteration_candidate` returns None without replay for a closed candidate failure; `_run_critic` still runs. The three role helpers themselves persist their artifact before returning. Only a caught protocol/audit/identity/accounting exception exits before critic.
- [ ] **Step 10: Add failing two-iteration incumbent tests.** Require exact role order [investigator, author, critic, investigator, author, critic], exactly six calls, first critic feedback in the second investigator input, current incumbent source/diff in the second author input, no hidden sentinel in any prompt, and no source-worktree mutation. Make iteration 1 improve; require its exact prospective PolicySourceBundle to fit before atomic incumbent.diff replacement. Make iteration 2 non-improve; require its excess to remain relative to the original fixed baseline while incumbent deltas are diagnostics. Cover candidate-attributable next_context_oversize keeping the prior incumbent.
- [ ] **Step 11: Run the incumbent-loop slice and confirm the state transition fails.**

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_loop.py -k "two_iteration or incumbent_transition or next_context_oversize"

- [ ] **Step 12: Implement incumbent comparison/history state and rerun Step 11.** Compare only after critic.json is durable, use the Decimal lexicographic objective, revalidate bundle/digest/CandidateIdentity before replacement, retain evidence while disposing rejected roots, and build the exact bounded IterationFeedbackSummary for the next investigator. Expected: green.
- [ ] **Step 13: Add failing stop and terminal-boundary tests.** Cover iteration limit, three valid non-improvers, pre-iteration complete-plan call/token/cost/context exhaustion, cancellation, trusted evaluator nondeterminism, evidence tampering, sandbox integrity failure, audit write failure, identity drift, and uncertain provider accounting. Budget/context exhaustion and cancellation are normal stops; integrity/accounting failures abort. Invalid candidates do not increase the non-improving streak. Every path closes the lease, releasing only unused future allowance.
- [ ] **Step 14: Run the stop/terminal slice and confirm final-discovery handling fails.**

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_loop.py -k "stop_condition or terminal_boundary or discovery_repeat"

- [ ] **Step 15: Implement pre-iteration stop checks plus _finish_discovery and rerun Step 14.** Before investigator, require the full next investigator/author/critic plan to fit and check cancellation. If no candidate strictly beats baseline, finish without hidden validation. Otherwise repeat one discovery fold with a fresh worker and require byte-identical aggregate evidence; mismatch is trusted_evaluator_nondeterminism. Expected: green.
- [ ] **Step 16: Add failing hidden-boundary tests.** Require atomic hidden reservation with source/baseline/candidate metadata, provider_enabled=false before evaluation, hard rejection of every later call_role attempt, independent fresh resets for hidden baseline/candidate, and content-free ledger outcome. Assert all payload builders omit hidden IDs/dates/baselines/results. Cover both long_replay_eligible at quantized excess >=0.10 point with >=3 closed trades and complete safety/integrity/accounting, and loop_verified_no_long_replay_candidate otherwise.

  ```python
  def test_provider_call_is_impossible_after_hidden_boundary():
      state = Mock(spec=_RunState)
      state.provider_enabled = False
      with pytest.raises(RuntimeError, match="provider capability is closed"):
          _call_role(Mock(), state, Mock(), Mock(), Mock(), Mock())
  ```
- [ ] **Step 17: Run the hidden slice and confirm _run_hidden_once is missing.**

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_loop.py -k "hidden_boundary or hidden_qualification or provider_closed"

- [ ] **Step 18: Implement _run_hidden_once, finalization, and cleanup; rerun Step 17.** Reserve once, set provider_enabled=false, evaluate once, record the content-free outcome, and write holdout.json only after the event. Then close the lease with the terminal code, stop all workers, dispose every capability/root, verify the source fingerprint and final accounting, persist an inert cumulative incumbent.diff when present, and write summary.json last from those final facts. Never commit, apply to the operator worktree, merge, push, or deploy. Expected: green.

  The provider guard is the first statement in the shared call helper:

  ```python
  if not state.provider_enabled:
      raise RuntimeError("provider capability is closed")
  if state.authorization_lease is None or state.frozen_pricing is None:
      raise RuntimeError("provider capability is not initialized")
  ```

  The common terminal path uses this exact ordering; each close/dispose/verify write is wrapped by the existing attempt-all cleanup accumulator so one failure cannot suppress later cleanup or summary evidence. `_build_final_result` is a pure constructor over the final accounting/cleanup values and is unit-tested with the PitOptimizerResult field list above:

  ```python
  state.provider_enabled = False
  if state.authorization_lease is not None:
      services.close_run_lease(state.authorization_lease, terminal_code)
      state.authorization_lease = None
  cleanup = _dispose_all_candidates_and_workers(state, services)
  services.verify_inputs(readiness)
  _replace_accounting_artifact(state, services)
  result = _build_final_result(
      readiness=readiness,
      state=state,
      terminal_code=terminal_code,
      cleanup=cleanup,
  )
  services.write_json_artifact("summary.json", result.to_public_artifact())
  return result
  ```
- [ ] **Step 19: Run the complete controller tests.**

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_loop.py tests/test_pit_optimizer_v2.py

- [ ] **Step 20: Commit the feedback-driven controller.**

      git add core/pit_optimizer_artifacts.py core/pit_optimizer_controller.py core/pit_optimizer_evaluation.py core/pit_optimization.py tests/test_pit_optimizer_loop.py tests/test_pit_optimizer_v2.py
      git commit -m "feat: add feedback-driven PIT incumbent loop"

---

### Task 8: Wire Schema-v2 Readiness, CLI, Summary, and Provider-Free Proof

**Files:**

- Modify: agent_loop.py
- Modify: core/pit_optimization.py
- Modify: core/pit_optimization_contract.py
- Modify: core/pit_optimizer_evaluation.py
- Modify: tests/test_agent_loop.py
- Modify: tests/test_pit_optimizer_loop.py
- Modify: tests/test_pit_optimizer_v2.py
- Modify: tests/test_pit_optimization_contract.py only for v1/v2 coexistence assertions

**Interfaces:**

- Produces: gate pit_optimizer; provider-free prepare output; exact canary command; closed user-facing schema-v2 summary.
- Consumes: explicit absolute local paths, exact manifest/readiness/parity identities, Docker image identity, operator ceilings, and source-transmission acknowledgement.

    @dataclass(frozen=True, slots=True)
    class PitOptimizerLiveRun:
        readiness: PitOptimizerReadiness
        optimizer_services: PitOptimizerServices

    _build_pit_optimizer_v2_config(
        namespace: argparse.Namespace,
    ) -> PitOptimizerGateConfig
    _dispatch_pit_optimizer_v2(
        config: PitOptimizerGateConfig,
        *,
        prepare: Callable[[PitOptimizerGateConfig], PitOptimizerReadiness],
        build_live_services: Callable[[PitOptimizerGateConfig], PitOptimizerLiveRun],
    ) -> PitOptimizerReadiness | PitOptimizerResult
    _pit_optimizer_v2_prepare_lines(
        config: PitOptimizerGateConfig,
        readiness: PitOptimizerReadiness,
    ) -> tuple[str, str]
    _pit_optimizer_v2_summary(
        result: PitOptimizerResult,
    ) -> dict[str, object]

**CLI route:**

- Add gate pit_optimizer for schema-v2 runs.
- Keep gate pit_optimization as the legacy schema-v1 route and artifact verifier.
- Add --optimizer-manifest and --optimizer-manifest-sha256.
- Add --verified-parity and --verified-parity-sha256.
- Add --optimizer-authorization-window-id and --optimizer-authorization-requirement-sha256. Prepare derives both from the manifest; canary requires exact matches.
- Add --authorize-policy-source-transmission, accepted only for phase canary.
- Reuse --max-iterations, --max-api-calls, --max-tokens, --max-usd, and require apply=false.
- The sealed manifest owns exact folds, universe identity, warmup, immutable constraints, and per-role input/output/USD caps.

- [ ] **Step 1: Write phase/config tests for both schema-v2 routes.** The Task 4 synthetic-manifest test exposes a canonical `v2_gate` fixture. Prepare rejects source transmission authorization and derives a requirement without a live window; canary requires authorization, exact identities, at least two iterations, sealed ceilings, and apply=false.

  ```python
  def test_prepare_rejects_source_authorization_and_canary_requires_it(v2_gate):
      with pytest.raises(ConfigurationError, match="prepare.*transmission"):
          replace(
              v2_gate,
              phase="prepare",
              source_transmission_authorized=True,
          ).validate()
      with pytest.raises(ConfigurationError, match="source transmission"):
          replace(
              v2_gate,
              phase="canary",
              source_transmission_authorized=False,
          ).validate()
  ```

- [ ] **Step 2: Run the config slice and verify red.**

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_agent_loop.py tests/test_pit_optimizer_v2.py -k "pit_optimizer_v2_config"

  Expected: the parser/config route is missing while legacy `pit_optimization` tests stay green.
- [ ] **Step 3: Add the parser fields and implement _build_pit_optimizer_v2_config.** Keep `pit_optimization` unchanged as schema v1. PitOptimizerGateConfig.validate performs the closed phase checks plus non-overlap/regular-file checks; it never opens a file outside the authenticated paths or constructs live services. Re-run Step 2; expected: green.
- [ ] **Step 4: Add and test build-subset-manifest with the same synthetic files.** Pass explicit readiness, final parity, PIT bundle, baseline run, source/runtime/temp/artifact roots, Git/Docker executables, digest-pinned image, two-iteration input/output/response-byte/USD caps, candidate bounds, and output. Assert six calls/448000 tokens/USD 0.40, canonical PolicySourceScope, and the complete identity graph. Spy that no pricing, replay, grant, hidden evaluation, or provider service is called.
- [ ] **Step 5: Run the manifest node and implement only its CLI adapter.**

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_v2.py -k "build_subset_manifest_cli"

  Expected red: route missing. After the adapter passes the exact parsed values to build_subset_manifest/write_optimizer_manifest/build_prepare_command, rerun; expected: green.
- [ ] **Step 6: Write the provider-free dispatch test.**

  ```python
  def test_prepare_dispatch_never_builds_live_services(v2_gate, readiness):
      events: list[str] = []
      result = _dispatch_pit_optimizer_v2(
          replace(v2_gate, phase="prepare"),
          prepare=lambda config: events.append("prepare") or readiness,
          build_live_services=lambda config: pytest.fail(
              "prepare constructed live services"
          ),
      )
      assert result is readiness
      assert events == ["prepare"]
  ```

- [ ] **Step 7: Run the dispatch node and verify red.**

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_agent_loop.py::test_prepare_dispatch_never_builds_live_services

- [ ] **Step 8: Implement phase-first dispatch and rerun Step 7.**

  ```python
  def _dispatch_pit_optimizer_v2(config, *, prepare, build_live_services):
      config.validate()
      if config.phase == "prepare":
          return prepare(config)
      if config.phase != "canary":
          raise ConfigurationError("unknown PIT optimizer phase")
      services = build_live_services(config)
      return run_pit_optimizer_v2(
          readiness=services.readiness,
          services=services.optimizer_services,
      )
  ```

  Prepare authenticates source, sealed bundle, baseline, folds, policy, parity, image, validation ledger, scope, and budgets; then reuses final-parity discovery evidence without replay. It does not construct a gateway, freeze pricing, open a lease, record a grant, evaluate hidden, or expose hidden identity.
- [ ] **Step 9: Add the canary-service order test.** Require exact scope/window authentication, one R1 pricing freeze, all six conservative preflight checks, one lease open, then controller execution. The private service registry owns existing Candidate capabilities; PolicyWorkerRunner/JsonLinePolicyClient receives only the generated policy package. Run both dispatch tests:

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_agent_loop.py -k "prepare_dispatch or canary_dispatch"

- [ ] **Step 10: Implement the live-service builder and exact prepare lines.** Reuse source lock/export, AuditTrail, both ledgers, candidate capability APIs, trusted evaluator, worker runner, and cleanup. Use subprocess.list2cmdline for a two-element tuple whose prefixes are exactly PIT_OPTIMIZER_READY= and PIT_OPTIMIZER_CANARY_COMMAND=. The command carries authenticated manifest/readiness/parity/window/requirement identities, six calls, 448000 tokens, USD 0.40, two iterations, apply=false, and --authorize-policy-source-transmission.
- [ ] **Step 11: Write the closed-summary test.** Add artifact_root: Path to PitOptimizerResult. Represent the incumbent only with changed paths/symbols, never a raw digest.

  ```python
  def test_v2_summary_excludes_provider_content_and_raw_hashes(v2_result):
      summary = _pit_optimizer_v2_summary(v2_result)
      assert set(summary) == {
          "schema_version", "phase", "status", "terminal_code", "exit_code",
          "run_id", "iterations_started", "iterations_completed",
          "valid_evaluations", "incumbent_updates", "incumbent",
          "hidden_validation_opened", "long_replay_eligible", "budget",
          "artifact_root", "source_modified", "cleanup_complete",
      }
      encoded = json.dumps(summary, sort_keys=True)
      assert v2_result.manifest_sha256 not in encoded
      assert v2_result.readiness_sha256 not in encoded
  ```

- [ ] **Step 12: Run the summary node, implement the exact dictionary, and rerun.**

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_agent_loop.py -k "pit_optimizer_v2_summary"

  The dictionary uses only the keys asserted above and excludes credentials, provider contents, raw trades, container IDs, and raw hashes.
- [ ] **Step 13: Run mocked integration and legacy coexistence.**

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_loop.py tests/test_agent_loop.py tests/test_pit_optimizer_v2.py tests/test_pit_optimization_contract.py -k "pit_optimizer or pit_optimization"

- [ ] **Step 14: Run the synthetic policy-worker Docker smoke and commit.** The smoke uses only synthetic snapshots and asserts network none, mount allowlist, deterministic output, resource limits, and cleanup; it makes no replay or provider call.

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_v2.py -k "policy_worker_docker_smoke"
      git add agent_loop.py core/pit_optimization.py core/pit_optimization_contract.py core/pit_optimizer_evaluation.py tests/test_agent_loop.py tests/test_pit_optimizer_loop.py tests/test_pit_optimizer_v2.py tests/test_pit_optimization_contract.py
      git commit -m "feat: expose model-authored PIT optimizer"

---

### Task 9: Focused Final Verification and Canary Readiness Handoff

**Files:**

- Verify only: all files above

**Interfaces:**

- Produces: focused compile/test/parity evidence and the unexecuted six-call canary handoff.
- Consumes: only committed local source, sealed local inputs, ignored local artifacts, and synthetic worker snapshots. It consumes zero provider calls.

- [ ] **Step 1: Compile only changed production modules.**

      python -B -m compileall -q core/strategy_policy core/pit_optimizer_candidate.py core/pit_optimizer_evaluation.py core/pit_optimizer_authorization.py core/pit_optimizer_artifacts.py core/pit_optimizer_controller.py core/pit_policy_parity.py
      python -B -m py_compile agent_loop.py core/backtest_engine.py core/pit_optimization.py core/pit_optimization_contract.py

- [ ] **Step 2: Run the focused architecture suite once.**

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_strategy_policy.py tests/test_pit_policy_parity.py tests/test_pit_optimizer_v2.py tests/test_pit_optimizer_loop.py tests/test_backtest_engine.py tests/test_backtest_custom_strategy_contract.py tests/test_backtest_open_causality.py tests/test_task11_effective_policy_contract.py

- [ ] **Step 3: Run only the optimizer-related agent-loop selection.**

      python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_agent_loop.py tests/test_pit_optimization_contract.py -k "pit_optimizer or pit_optimization or provider_call or budget_reservation or policy_worker"

- [ ] **Step 4: Require a clean committed final HEAD, then run the exact final-side two-fold parity once.**

      git status --porcelain
      python -B -m core.pit_policy_parity verify --reference .artifacts/pit-policy-parity-v2/reference.json --pit-bundle .artifacts/task-4-regeneration-20260823T223000Z/pit-bundle/pit_baseline.sqlite3 --output .artifacts/pit-policy-parity-v2/verified-final.json

  Expected: the status command prints nothing, then `PIT_POLICY_PARITY matched=true`. The create-only attestation binds the intermediate reference HEAD, clean final implementation HEAD/fingerprint, bundle/baseline/fold identities, policy interface, exact discovery outputs, and unchanged schema-v1 effective policy. It evaluates no hidden prices/metrics and starts no provider or Docker process.
- [ ] **Step 5: Build the real manifest from that same clean final HEAD.**

      python -B -m core.pit_optimizer_evaluation build-subset-manifest --readiness C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.artifacts\pit-optimizer-subset-performance-20260827T200714Z\pit-optimization-readiness-d3cbfcb22900\readiness.json --verified-parity C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.artifacts\pit-policy-parity-v2\verified-final.json --pit-bundle C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.artifacts\task-4-regeneration-20260823T223000Z\pit-bundle\pit_baseline.sqlite3 --baseline-run C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.artifacts\task-11-prefix-replay-20260826T001500Z\run-20260826T002913Z-1af306ef1e46 --source-root C:\Projects\trading_bot\RS-momentum-EMA-trading-bot --permanent-runtime-root C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.artifacts\pit-optimizer-v2\runtime --controller-temp-parent C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.artifacts\pit-optimizer-v2\candidates --artifact-root C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.artifacts\pit-optimizer-v2\runs --git-executable "C:\Program Files\Git\cmd\git.exe" --docker-executable C:\Users\llong\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe --sandbox-image localhost/rs-agent-loop@sha256:7ecfb4ebb3b327940bef347e4c82e82fb4a0e8b40fc63b92b2536fe8c83acf1c --iterations 2 --investigator-static-bytes 8000 --investigator-dynamic-bytes 80000 --investigator-input-tokens 88000 --investigator-output-tokens 4000 --investigator-response-bytes 8192 --investigator-max-usd 0.05 --author-static-bytes 12000 --author-dynamic-bytes 76000 --author-input-tokens 88000 --author-output-tokens 8000 --author-response-bytes 16384 --author-max-usd 0.10 --critic-static-bytes 8000 --critic-dynamic-bytes 24000 --critic-input-tokens 32000 --critic-output-tokens 4000 --critic-response-bytes 8192 --critic-max-usd 0.05 --max-files 3 --max-hunks 12 --max-changed-lines 80 --max-diff-bytes 8192 --output C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.artifacts\pit-optimizer-v2\manifest.json

  Expected: `PIT_OPTIMIZER_MANIFEST` reports six calls, 448000 maximum tokens, USD 0.40, exact source/input/output/response/fold/final-parity/image identities, PolicySourceScope, candidate bounds, and AuthorizationRequirement; `PIT_OPTIMIZER_PREPARE_COMMAND` is exact. This queries only authenticated identities/session calendars and performs no pricing lookup/replay/provider call/grant creation.
- [ ] **Step 6: Run exactly the emitted PIT_OPTIMIZER_PREPARE_COMMAND.** It authenticates verified-final.json and the entire identity graph, marks discovery exposure, reuses the attested discovery baselines, writes readiness, and prints the exact inert canary command with the requirement/window identifiers. It must not construct OpenRouterGateway, open an authorization lease, record a grant, replay a fold, or evaluate hidden_1.
- [ ] **Step 7: Inspect the provider-free readiness package.** Confirm no hidden fold result exists, no provider/accounting call exists, schema-v1 policy digest matches, candidate paths are exactly the three pure modules, discovery windows are permanently marked non-hidden, and the source worktree is unchanged.
- [ ] **Step 8: Check repository hygiene.**

      git diff --check
      git status --short --branch

  Expected: no uncommitted implementation files after task commits; only explicitly retained local .artifacts remain ignored.
- [ ] **Step 9: Report, but do not execute, the exact six-call subset canary command.** Report the exact PolicySourceScope digest/preimage summary, sealed per-role response/cost caps, and cumulative six-call/448000-token/USD-0.40 ceilings. Do not print/run a record-grant command or load live pricing. Stop for fresh explicit operator authorization of that scope plus calls/tokens/USD; live canary setup must still freeze pricing and may stop with zero calls if any conservative per-call cost exceeds its sealed cap.
- [ ] **Step 10: Defer full-window parity and the long replay.** Full-window provider-free policy parity is required immediately before a later full optimizer/long replay, not during this subset architecture proof.

### Task 9A: Enforce the Rounded-Stop Risk Ceiling and Version Local Evidence

The legacy sizing path can exceed the written risk ceiling when the derived stop rounds
down to the nearest cent. Preserve the hard safety contract: derive the rounded stop
before final sizing, cap quantity by both the recommended and engine risk budgets using
the actual rounded loss per share, and recompute the buy notional from the clamped
quantity. Preserve the existing cash, gross-exposure, and notional-cap checks. Keep the
original sealed reference and baseline artifacts immutable; corrected provider-free
subset evidence is written under a new local artifact version and must carry the new
source identity. A full baseline reseal remains part of the later long-replay gate and
must not be started by this task.

 - [x] Add adverse, exact, and favorable cent-rounding regression coverage.
 - [x] Implement and independently review the engine-owned post-rounding clamp.
 - [x] Capture and verify the corrected two-fold subset reference under a new local
       artifact version (`pit-policy-parity-v4`); do not evaluate the hidden fold.

## Completion Criteria

- Baseline strategy behavior matches exactly on both independent discovery folds after extraction.
- Candidate code cannot access raw PIT data, the repository, artifacts, credentials, dates, fold identity, or network.
- Incremental and cumulative policy diffs both satisfy the hard 3-file/200-line/64-KiB ceiling and the manifest's stricter sealed bounds; the first canary uses 3 files/12 hunks/80 lines/8 KiB.
- Every outbound incumbent source bundle proves descent from the exact authorized PolicySourceScope; no path, cap, bound, iteration, or descendant-rule expansion reuses that authorization.
- Two mocked iterations carry critic feedback into the next investigator and account for exactly six all-R1 calls.
- One run-fixed R1 pricing identity makes every sealed per-call USD ceiling a pre-send check; an over-cap price starts zero role calls.
- Discovery selection uses only the quantized lexicographic tuple; hidden validation is consumed once and never shown to a role.
- Every terminal result is an inert local evidence package. No source apply, commit by the optimizer, merge, push, deploy, or cloud upload occurs.
- The live canary remains unexecuted until a new explicit six-call/source/token/USD authorization is received.
