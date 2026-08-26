# Task 11 Engine-Fidelity Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the sealed Task 11 replay's exact C/A provenance and the engine's actual strategy policy so a later optimizer can safely loosen or tighten CANSLIM-derived rules without confusing inert settings with real behavior.

**Architecture:** Legacy C/A evaluators delegate to shared trace-producing cores, while the PIT bundle optionally attaches public-date provenance without changing normal frames. A Task 11-only offline command reconciles those traces to the sealed signal ledger, and a separate canonical policy builder publishes each engine rule as a causal invariant, active tunable policy, active fixed policy, or advisory/unsupported setting. This plan adds observability and contract enforcement only; it does not optimize or change strategy behavior.

**Tech Stack:** Python 3.13, pandas, SQLite, standard-library `csv`, `dataclasses`, `datetime`, `enum`, `hashlib`, `json`, `pathlib`, and `tempfile`

**Spec:** `docs/superpowers/specs/2026-08-26-task11-engine-fidelity-observability-design.md`

## Global Constraints

- Work only on branch `codex/pit-canslim-strategy-diagnosis`; do not switch branches or create another worktree.
- Preserve the existing uncommitted Task 11A/11B changes in `core/pit_diagnosis/baseline.py`, `pit_diagnosis.py`, and `core/pit_diagnosis/task11_artifact_diagnosis.py`.
- Do not commit, push, merge, reset, clean, stash, or delete files.
- Do not touch optimizer PID `18196` or any other optimization session/process.
- Do not access the network, install a provider/plugin, or download any dataset. Use only the existing local hash-bound Task 11 replay, PIT bundle, and fundamental sidecars.
- Do not create, modify, or run pytest/unit-test files until the user declares the build phase finished. Direct read-only parity scripts, CLI reconciliation, import/compile checks, and `git diff --check` are allowed.
- Do not run a new backtest, refresh a cache, alter a sealed artifact, or write into the sealed Task 11 run directory.
- Do not alter C/A scores, growth values, metric priority, fiscal matching, technical/base logic, entry gates, thresholds, order timing, sizing, exits, or portfolio state.
- CANSLIM is the strategy basis and initial policy family, not an immutable optimization target. Later optimization may loosen or tighten supported CANSLIM-derived thresholds, weights, and gates.
- The non-negotiable constraints are point-in-time causality, no-lookahead ordering, realistic next-session execution, immutable source identity, deterministic accounting, and truthful observability.
- Publish aggregate-only Task 11 provenance output. Never emit ticker symbols, CIKs, accessions, source URLs, raw SEC values, filing text, or raw sidecar rows.
- The canonical Task 11 authority is profile `strict-proper-base-task11`, manifest SHA-256 `f99eb6aade8b567b319accb95dadd80789064aacf1cb8b85d0cc31379caf6382`, PIT bundle SHA-256 `1af306ef1e46797473cd186fc48938ed6694ae25f5943c9f1905b528307cc2eb`, replay Git head `515cb1e50d051e2ee4253603608f2fd3920004bc`, and evaluation window `2021-01-01` through `2025-12-31`.
- The fixed provenance focus window is `2023-01-01` through `2025-12-31`, with 8,439 technical setups, 1,926 C passes, 5,712 finite C failures, 801 C unavailable, 627 A passes, 1,136 finite A failures, and 163 A unavailable.
- Task 11 remains labeled `fidelity_incomplete` because M is disabled and I/L evidence is incomplete. Do not call it full or optimal CANSLIM.

---

### Task 1: Shared C/A Trace Cores With Legacy Parity

**Files:**
- Create: `core/canslim/earnings_trace.py`
- Modify: `core/canslim/c_current_earnings.py`
- Modify: `core/canslim/a_annual_earnings.py`

**Interfaces:**
- Consumes: the existing quarterly and annual pandas frames and the unchanged `match_fiscal_year_over_year_periods()` result.
- Produces: `MetricFamily`, `TraceReason`, frozen `CTrace`, frozen `ATrace`, `evaluate_c_with_trace(quarterly_income: pd.DataFrame, c_growth_target: float | None = None) -> CTrace`, and `evaluate_a_with_trace(annual_income: pd.DataFrame, a_growth_target: float | None = None, balance_sheet: pd.DataFrame | None = None) -> ATrace`.
- Preserves: `evaluate_c(quarterly_income: pd.DataFrame, c_growth_target: float | None = None) -> tuple[float, float | None]` and `evaluate_a(annual_income: pd.DataFrame, a_growth_target: float | None = None, balance_sheet: pd.DataFrame | None = None) -> tuple[float, float | None, float | None]` exactly.

- [ ] **Step 1: Define the closed trace vocabulary.**

Create `core/canslim/earnings_trace.py` with these public enums and dataclass fields:

```python
from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class MetricFamily(StrEnum):
    DILUTED_EPS = "diluted_eps"
    BASIC_EPS = "basic_eps"
    NET_INCOME = "net_income"
    UNAVAILABLE = "unavailable"


class TraceReason(StrEnum):
    COMPLETE = "complete"
    NO_VISIBLE_OBSERVATION = "no_visible_observation"
    NO_COMPARABLE_PRIOR_PERIOD = "no_comparable_prior_period"
    INSUFFICIENT_ANNUAL_HISTORY = "insufficient_annual_history"
    NONFINITE_CURRENT_VALUE = "nonfinite_current_value"
    NONFINITE_PRIOR_VALUE = "nonfinite_prior_value"
    ZERO_PRIOR_VALUE = "zero_prior_value"
    NEGATIVE_PRIOR_VALUE = "negative_prior_value"
    EVALUATOR_EXCEPTION = "evaluator_exception"


@dataclass(frozen=True, slots=True)
class CTrace:
    score: float
    current_growth: float | None
    metric_family: MetricFamily
    terminal_reason: TraceReason
    current_period_end: date | None
    prior_period_end: date | None
    current_public_date: date | None
    prior_public_date: date | None
    current_value: float | None
    prior_value: float | None


@dataclass(frozen=True, slots=True)
class ATrace:
    score: float
    annual_growth: float | None
    roe: float | None
    metric_family: MetricFamily
    terminal_reason: TraceReason
    current_period_end: date | None
    prior_period_end: date | None
    current_public_date: date | None
    prior_public_date: date | None
    current_value: float | None
    prior_value: float | None
```

Add `PIT_PUBLIC_DATES_ATTR = "pit_public_date_by_period"` and a helper that reads an ISO-date mapping from `DataFrame.attrs[PIT_PUBLIC_DATES_ATTR]` without mutating the frame. Unknown or malformed provenance must raise in trace mode; the legacy path remains decision-compatible through its existing broad exception result.

- [ ] **Step 2: Refactor C into one trace-producing evaluator core.**

Implement `evaluate_c_with_trace(quarterly_income, c_growth_target=None) -> CTrace`. Preserve the exact metric selection order `Diluted EPS -> Basic EPS -> Net Income`, the newest-first fiscal matching, the 28-day unique comparator rule, `_get_quarterly_yoy_growths`, consistency scoring, acceleration scoring, settings weights, clipping, and broad exception fallback.

Classify only the selected latest pair:

```python
if no candidate row is visible:
    reason = TraceReason.NO_VISIBLE_OBSERVATION
elif the newest selected observation has no unique prior-year comparator:
    reason = TraceReason.NO_COMPARABLE_PRIOR_PERIOD
elif current cannot be converted to a finite float:
    reason = TraceReason.NONFINITE_CURRENT_VALUE
elif prior cannot be converted to a finite float:
    reason = TraceReason.NONFINITE_PRIOR_VALUE
elif prior is zero or numpy-is-close to zero:
    reason = TraceReason.ZERO_PRIOR_VALUE
elif prior is negative:
    reason = TraceReason.NEGATIVE_PRIOR_VALUE
else:
    reason = TraceReason.COMPLETE
```

Do not turn a finite growth below 25% into an unavailable reason. It remains `complete`; the Task 11 aggregation layer owns pass/below-threshold classification.

- [ ] **Step 3: Make legacy C delegate to the shared core.**

Replace the body of `evaluate_c` with delegation only:

```python
trace = evaluate_c_with_trace(quarterly_income, c_growth_target)
return trace.score, trace.current_growth
```

The output tuple and numeric types must match the pre-change evaluator for empty frames, valid frames, unmatched latest periods, zero/negative priors, non-finite values, and evaluator exceptions.

- [ ] **Step 4: Refactor A into one trace-producing evaluator core.**

Implement `evaluate_a_with_trace(annual_income, a_growth_target=None, balance_sheet=None) -> ATrace`. Preserve the exact metric order, `sort_index().dropna()`, latest-two-non-null annual comparison, `_get_annual_growths`, consistency scoring, ROE calculation, settings weights, clipping, and broad exception fallback.

Use `insufficient_annual_history` when a visible selected family has fewer than two usable annual observations. Do not describe A as fiscal-year matching: `current_period_end` and `prior_period_end` are the actual latest two non-null columns used by production.

- [ ] **Step 5: Make legacy A delegate to the shared core.**

Replace the body of `evaluate_a` with:

```python
trace = evaluate_a_with_trace(annual_income, a_growth_target, balance_sheet)
return trace.score, trace.annual_growth, trace.roe
```

- [ ] **Step 6: Perform direct non-test verification for the touched modules.**

Run only:

```powershell
python -B -m py_compile core/canslim/earnings_trace.py core/canslim/c_current_earnings.py core/canslim/a_annual_earnings.py
git diff --check -- core/canslim/earnings_trace.py core/canslim/c_current_earnings.py core/canslim/a_annual_earnings.py
```

Then run one inline import/parity probe over small constructed frames. It must compare legacy tuples to the fields returned by the trace APIs and print `trace parity probe: PASS`; it must not create a test file or write an artifact.

---

### Task 2: Opt-In PIT Public-Date Provenance

**Files:**
- Modify: `core/pit_data.py`

**Interfaces:**
- Consumes: `PIT_PUBLIC_DATES_ATTR` from `core.canslim.earnings_trace`.
- Produces: optional `include_provenance: bool = False` on `fundamentals_as_of`, `_fundamental_snapshot`, `_statement_frame`, and `iter_fundamental_state_boundaries`.
- Preserves: the default engine/provider frame shape, values, index, columns, dtypes, and cache behavior.

- [ ] **Step 1: Thread an opt-in flag through snapshot construction.**

Use these exact declarations; their bodies retain the current query/snapshot logic and thread the
new keyword to `_fundamental_snapshot` and `_statement_frame`:

- `fundamentals_as_of(self, symbol: str, as_of_date: pd.Timestamp | datetime, *, include_provenance: bool = False) -> dict[str, Any]`
- `iter_fundamental_state_boundaries(self, date_bounds: Mapping[str, tuple[pd.Timestamp | datetime | date, pd.Timestamp | datetime | date]], *, include_provenance: bool = False) -> Iterable[tuple[str, date, dict[str, Any]]]`
- `_fundamental_snapshot(cls, records: list[dict[str, Any]], *, include_provenance: bool = False) -> dict[str, Any]`
- `_statement_frame(records: list[dict[str, Any]], statement_type: str, *, include_provenance: bool = False) -> pd.DataFrame`

Every existing caller that omits the flag must follow the current path unchanged.

- [ ] **Step 2: Attach provenance after amendment selection without changing cells.**

Continue sorting by `period_end, public_date` and retaining the latest visible row per period. Before transposition, capture the selected row's dates. When `include_provenance=True`, attach this mapping to the final statement frame:

```python
frame.attrs[PIT_PUBLIC_DATES_ATTR] = {
    pd.Timestamp(period_end).date().isoformat():
        pd.Timestamp(public_date).date().isoformat()
    for period_end, public_date in selected_period_public_dates
}
```

Do not add a row or column. Do not attach tickers, accessions, concepts, or values. Empty frames receive an empty mapping only in provenance mode.

- [ ] **Step 3: Keep the production provider cache provenance-free.**

`fundamentals_provider` and `_fundamentals_provider_state` must continue calling
`self._fundamental_snapshot(candidate_records, include_provenance=False)` and
`self._fundamental_snapshot([], include_provenance=False)`, respectively. The optimizer/backtest
engine must not pay for trace metadata or observe a changed frame contract.

- [ ] **Step 4: Perform direct non-test verification.**

Open the existing bundle read-only with its exact SHA. For at least one local symbol/date, obtain default and provenance snapshots and verify in an inline script that every statement frame has equal index, columns, and cell values; only the provenance frame has `PIT_PUBLIC_DATES_ATTR`. Print `PIT provenance frame parity: PASS`, close the bundle, and do not write output files.

Run:

```powershell
python -B -m py_compile core/pit_data.py
git diff --check -- core/pit_data.py
```

---

### Task 3: Sealed Task 11 C/A Provenance Diagnostic

**Files:**
- Create: `core/pit_diagnosis/task11_ca_provenance.py`
- Modify: `pit_diagnosis.py`

**Interfaces:**
- Consumes: canonical Task 11 profile verification, the verified run manifest and signal ledger, the exact local PIT bundle, the hash-bound `fundamentals_provenance.json`, `fundamentals.csv`, and `fundamentals_audit.csv`, PIT provenance snapshots, and both trace APIs.
- Produces: `diagnose_task11_ca_provenance(run_dir: Path, profile: BaselineAuthorityProfile) -> dict[str, object]` and CLI command `diagnose-task11-ca-provenance`.

- [ ] **Step 1: Establish a closed source chain before parsing.**

At the top of `diagnose_task11_ca_provenance`, require `type(profile) is BaselineAuthorityProfile`, resolve `strict-proper-base-task11` internally, compare every profile/authority field exactly, and call `verify_baseline_run` before reading a ledger.

Use the verified manifest's `arguments.pit_bundle` and `arguments.fundamentals_provenance` only as file locations. The expected bundle and provenance digests come from the canonical authority and verified Task 11 input identities, not caller data. Require regular non-reparse files, snapshot JSON/CSV bytes into `tempfile.SpooledTemporaryFile`, hash before parsing, and reject a digest mismatch.

Resolve `fundamentals.csv` and `fundamentals_audit.csv` as siblings of the verified provenance file. Their expected hashes come from the already verified provenance JSON fields `fundamentals_sha256` and `fundamentals_audit_sha256`. Require these exact identities:

```python
TASK11_PROVENANCE_SHA256 = "52fb676f64f279f6f7b6f119df1fd367e569658b98e313b450b0dee1f5aeb0b6"
TASK11_FUNDAMENTALS_SHA256 = "527769d437b1f29b8aac46543fd65c8017d798f9ee6a27a350c448cfe1242b00"
TASK11_FUNDAMENTALS_AUDIT_SHA256 = "2983e2538de39392e25df99fea93a26073f6bf5d21d87e5411847569b38619b1"
```

Never open the 1.5 GB SEC archives. Never request a provider fallback.

- [ ] **Step 2: Stream only the fixed conditional cohorts.**

Read the authenticated `canslim_signals.csv` and select rows where `2023-01-01 <= signal_date <= 2025-12-31` and `technical_setup_eligible` is true. Require exactly 8,439 rows. C evaluates all 8,439. A evaluates only the 1,926 rows whose reconciled C growth is finite and at least `MIN_CURRENT_GROWTH`.

Build per-symbol date bounds once, call
`bundle.iter_fundamental_state_boundaries(date_bounds, include_provenance=True)`, and perform
right-inclusive state lookup. Do not execute portfolio code or recompute technical setups.

- [ ] **Step 3: Reconcile every trace to the sealed scalar ledger.**

For each C cohort row, require trace `score` to match `c_score` and trace `current_growth` availability/value to match `current_growth`. For the conditional A cohort, require trace `score` to match `a_score` and trace `annual_growth` availability/value to match `annual_growth`. Use exact `None`/finite classification and absolute tolerance `1e-12` for finite floats. Reject NaN, infinity, duplicate `(symbol, signal_date)` rows, off-window rows in the selected cohort, unknown enum values, or a single mismatch.

Write progress only to stderr every 500 reconciled technical rows in this form:

```text
task11-ca-provenance: reconciled 500/8439 technical rows
```

The final stdout remains one canonical aggregate JSON document.

- [ ] **Step 4: Aggregate without publishing private row identities.**

Return schema version 1 with exactly these top-level sections:

```python
{
    "schema_version": 1,
    "diagnosis_scope": "task11_ca_provenance_not_strategy_optimization",
    "profile": _render_profile(canonical_profile),
    "source_chain": _render_source_chain(verified_sources),
    "window": {"start": "2023-01-01", "end": "2025-12-31"},
    "reconciliation": _render_reconciliation(reconciliation),
    "c": _render_gate_aggregates(c_aggregates),
    "a": _render_gate_aggregates(a_aggregates),
}
```

Implement the named renderers in this module. `_render_profile` returns only `profile_id`, `scope`,
`fidelity_label`, `fidelity_reason`, `manifest_sha256`, `bundle_sha256`, `replay_git_head`, and the
fixed date contract. `_render_source_chain` returns one object per `run_manifest.json`,
`canslim_signals.csv`, PIT bundle, provenance JSON, fundamentals CSV, and fundamentals audit CSV;
each object contains only `sha256` and `verified: True`. `_render_reconciliation` returns C/A cohort
sizes, scalar rows checked, availability rows checked, mismatch count zero, and `passed: True`.
`_render_gate_aggregates` returns cohort size, pass/below-threshold/unavailable counts, by-year
counts, metric-family-by-outcome counts, unavailable-terminal-reason counts, and public-date-pair
visibility counts. The payload must not contain `symbol`, `ticker`, `cik`, `accession`, paths,
URLs, raw metric values, or raw filing records.

- [ ] **Step 5: Add the explicit CLI route.**

In `pit_diagnosis.py`, add `diagnose-task11-ca-provenance` with required `--baseline-run` and explicit `--baseline-profile`. Reject any profile other than `strict-proper-base-task11`. Serialize with sorted keys and `allow_nan=False`.

- [ ] **Step 6: Run the real offline reconciliation once.**

Run:

```powershell
python -B pit_diagnosis.py diagnose-task11-ca-provenance --baseline-run ".artifacts/task-11-prefix-replay-20260826T001500Z/run-20260826T002913Z-1af306ef1e46" --baseline-profile strict-proper-base-task11
```

Require final reconciliation counts of 8,439 C cohort rows; 1,926 pass, 5,712 finite below threshold, 801 unavailable; and 1,926 conditional A cohort rows; 627 pass, 1,136 finite below threshold, 163 unavailable. Save no copy inside the sealed run.

Run only these structural checks afterward:

```powershell
python -B -m py_compile core/pit_diagnosis/task11_ca_provenance.py pit_diagnosis.py
git diff --check -- core/pit_diagnosis/task11_ca_provenance.py pit_diagnosis.py
```

---

### Task 4: Immutable Effective Engine Policy and Honest Optimizer Surface

**Files:**
- Create: `core/engine_policy.py`
- Modify: `core/backtest_engine.py`
- Modify: `pit_baseline.py`

**Interfaces:**
- Consumes: a fully initialized `PortfolioSimulator`, canonical entry-contract constants, settings weights, active scale-out tiers, and existing run configuration.
- Produces: `build_effective_engine_policy(simulator) -> dict[str, object]`, `effective_engine_policy_sha256(policy) -> str`, config keys `effective_engine_policy` and `effective_engine_policy_sha256`, and explicit compatibility validation for inert request fields.

- [ ] **Step 1: Define the policy schema and field wrapper.**

Use policy schema version 1. Every behavior-bearing leaf uses this shape:

```python
{
    "value": json_safe_value,
    "classification": classification,
    "source": "module.constant_or_formula",
    "optimizer_candidate": bool,
}
```

Define `PolicyClassification` as a `StrEnum` with exactly
`CAUSAL_INVARIANT = "causal_invariant"`,
`ACTIVE_TUNABLE_POLICY = "active_tunable_policy"`,
`ACTIVE_FIXED_POLICY = "active_fixed_policy"`, and
`ADVISORY_OR_UNSUPPORTED = "advisory_or_unsupported"`. The `_field` helper accepts only that enum,
converts it to its string value, and rejects a non-JSON-safe `value` through canonical serialization.

The top level includes:

```python
{
    "schema_version": 1,
    "strategy_basis": "CANSLIM-derived",
    "optimization_objective_owner": "multi_agent_backtest_loop",
    "optimization_objective": "maximize_return_and_minimize_drawdown",
    "optimization_executed_by_this_build": False,
    "causal_invariants": _causal_invariant_fields(simulator),
    "entry_policy": _entry_policy_fields(simulator),
    "scoring_policy": _scoring_policy_fields(simulator),
    "market_policy": _market_policy_fields(simulator),
    "capacity_and_sizing_policy": _capacity_and_sizing_fields(simulator),
    "exit_policy": _exit_policy_fields(simulator),
    "unsupported_requests": _unsupported_request_fields(simulator),
}
```

Implement every named helper in `core/engine_policy.py`; each returns a mapping whose leaves are
created by `_field`. `build_effective_engine_policy` must not inspect mutable runtime portfolio
state, prices, trades, or artifacts, so identical initialized policy produces an identical digest.

- [ ] **Step 2: Record actual entry and scoring behavior.**

Record current/annual growth gates 0.25, RS gate 80, composite gate 70, volume ratio 1.30, buy-zone extension 0.05, completed-session technical facts, proper-base mode, metric-family priority, all CANSLIM component weights, all C/A subweights/targets, non-M entry-composite behavior, and institutional-data reweighting. Fixed canonical thresholds and settings weights are `active_fixed_policy` with `optimizer_candidate=True`; they are not causal invariants.

Record market/regime gates using the simulator's actual booleans. Because those constructor settings already alter behavior, classify them `active_tunable_policy`. Record `technical_only` as an active mode switch, not a CANSLIM fidelity claim.

- [ ] **Step 3: Record actual capacity, sizing, and exit behavior.**

Record uncapped/capped `max_positions`, eviction state, and cash-deployment override. Record the sizing formula as `portfolio_equity * position_risk_pct / stop_loss_pct`, including actual risk and stop inputs; do not describe `position_size_pct` as effective.

Record stop-loss, breakeven trigger, EMA period/consecutive rule, stagnation days/threshold, the early-winner rule (`+20% within 15 days` activates a hold through day 40), and the actual `settings.SCALE_OUT_TIERS` sequence `[[0.10, 0.25], [0.15, 0.25], [0.20, 0.25]]`. Active constructor-controlled fields are `active_tunable_policy`; global scale tiers and early-winner constants are `active_fixed_policy` with `optimizer_candidate=True`.

- [ ] **Step 4: Reject non-default inert requests before a run starts.**

Add one compatibility validator called at constructor time before data access. These requests are accepted only at their existing compatibility values and otherwise raise `ValueError` naming the inert field and its actual policy source:

```python
{
    "min_rs_score": MIN_RS_SCORE,
    "min_canslim_score": MIN_COMPOSITE_SCORE,
    "min_technical_score": DEFAULT_MIN_TECHNICAL_SCORE,
    "position_size_pct": DEFAULT_POSITION_SIZE_PCT,
    "take_profit_pct": DEFAULT_TAKE_PROFIT_PCT,
    "scale_out_fraction": DEFAULT_SCALE_OUT_FRACTION,
}
```

`CanslimStrategy.min_c_a_growth` is also inert in the production engine path. Accept only `DEFAULT_MIN_C_A_GROWTH` and report it as `advisory_or_unsupported` with `optimizer_candidate=True`; do not silently turn it into an active threshold in this task. Compare finite floats with `math.isclose(rel_tol=0.0, abs_tol=1e-12)` and reject booleans, `None`, NaN, and infinity.

- [ ] **Step 5: Canonicalize and bind the policy digest.**

Compute the digest over UTF-8 JSON using `sort_keys=True`, `separators=(",", ":")`, and `allow_nan=False`. Build the policy once from initialized state, place both object and digest in `_result_config`, and include the digest in `_portfolio_checkpoint_fingerprint` so resumed work cannot cross an effective-policy change.

- [ ] **Step 6: Publish policy in normal and Task 11 outputs.**

`BacktestResult.config` must expose `effective_engine_policy` and `effective_engine_policy_sha256`. In `pit_baseline.py`, copy both into `summary.json` and top-level `run_manifest.json` in addition to their presence in `canslim_config`. Before publication, recompute the digest and reject any mismatch. This affects future runs only; do not rewrite the sealed Task 11 artifact.

- [ ] **Step 7: Perform direct non-test policy verification.**

Instantiate the default simulator without running it and print only the policy digest plus these selected facts: strict Task 11 thresholds are active-fixed optimizer candidates; market/regime gates are active tunables; risk sizing is the effective formula; scale-out tiers are 10/15/20% at 25% each; and `take_profit_pct` is unsupported. Instantiate once with `take_profit_pct=0.41` and require a `ValueError` before any data fetch.

Run:

```powershell
python -B -m py_compile core/engine_policy.py core/backtest_engine.py pit_baseline.py
git diff --check -- core/engine_policy.py core/backtest_engine.py pit_baseline.py
```

---

### Task 5: Integrated Offline Acceptance and Exposure Audit

**Files:**
- Modify only if a narrow integration defect is found: files already listed in Tasks 1–4
- Read: `docs/superpowers/specs/2026-08-26-task11-engine-fidelity-observability-design.md`
- Read: `docs/superpowers/plans/2026-08-26-task11-engine-fidelity-observability.md`

**Interfaces:**
- Consumes: all Task 1–4 deliverables.
- Produces: direct verification evidence and an implementation report; no strategy result and no optimization result.

- [ ] **Step 1: Re-run the existing sealed artifact diagnosis.**

Run:

```powershell
python -B pit_diagnosis.py diagnose-task11-artifacts --baseline-run ".artifacts/task-11-prefix-replay-20260826T001500Z/run-20260826T002913Z-1af306ef1e46" --baseline-profile strict-proper-base-task11
```

Require the previously sealed funnel and execution reconciliation to remain unchanged. This command reads artifacts only and must not launch a backtest.

- [ ] **Step 2: Re-run the Task 11 C/A provenance command.**

Run the Task 3 command and require all fixed C/A totals, all scalar reconciliations, verified source-chain booleans, and aggregate-only output.

- [ ] **Step 3: Audit the exposed strategy surface against source behavior.**

Inspect the policy JSON and source references for every category. Confirm:

```text
causal invariants are not labeled tunable
CANSLIM thresholds/weights are not labeled universal hard constraints
currently supported gates and portfolio settings are labeled active tunable
currently hard-coded but behavior-bearing rules are labeled active fixed
inert compatibility arguments are labeled unsupported and reject changes
actual scale-out tiers and actual sizing formula are exposed
Task 11 remains fidelity_incomplete and is not called optimal
optimization objective ownership is the multi-agent backtest loop
```

- [ ] **Step 4: Run structural verification only after the build is complete.**

Run:

```powershell
python -B -m py_compile core/canslim/earnings_trace.py core/canslim/c_current_earnings.py core/canslim/a_annual_earnings.py core/pit_data.py core/pit_diagnosis/task11_ca_provenance.py core/pit_diagnosis/task11_artifact_diagnosis.py core/pit_diagnosis/baseline.py core/engine_policy.py core/backtest_engine.py pit_baseline.py pit_diagnosis.py
git diff --check
git status --short
```

Do not run pytest or add test files. Report that formal regression tests remain deliberately deferred until the user ends the build phase.

- [ ] **Step 5: Produce the handoff report without optimization claims.**

Report exact C/A funnel totals, source-chain verification, trace reason/family aggregates, effective-policy digest, category counts, inert-setting rejections, files changed, and verification commands. State explicitly that this build did not improve returns, reduce drawdown, select parameters, or run the optimizer; it made the strategy surface truthful so the multi-agent loop can do that work next.
