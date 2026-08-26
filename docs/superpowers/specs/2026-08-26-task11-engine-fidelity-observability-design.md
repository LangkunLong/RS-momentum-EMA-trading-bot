# Task 11 Engine-Fidelity Observability Design

**Date:** 2026-08-26
**Branch:** `codex/pit-canslim-strategy-diagnosis`
**Status:** Approved for implementation

## Objective

Make the Task 11 strict-proper-base replay auditable without changing its entry thresholds,
exit semantics, portfolio logic, source data, or optimizer behavior. The implementation has two
linked deliverables:

1. a point-in-time C/A provenance diagnostic that explains the exact production evaluator's
   source, period pair, public-date visibility, and terminal unavailable reason; and
2. an immutable effective-engine-policy record that distinguishes the engine's actual behavior
   from advisory, derived, or inert configuration fields.

This design intentionally does not decide whether the intended profit-taking policy is the
recorded `take_profit_pct=0.40`/`scale_out_fraction=0.50` pair or the currently effective global
`SCALE_OUT_TIERS`. It reports the latter truthfully and makes unsupported tuning requests fail
clearly. Choosing a replacement exit policy remains a separate human-approved strategy change.

Task 11 is a reference baseline, not the optimization objective. CANSLIM defines the strategy's
feature vocabulary and initial policy family; later optimization may loosen or tighten supported
CANSLIM-derived thresholds, weights, and gates to maximize risk-adjusted performance. The durable
hard constraints are point-in-time causality, realistic execution, source/data integrity, and
truthful observability—not exact adherence to every O'Neil rule.

## Strategy doctrine and optimizer boundary

The observability contract classifies every exposed policy field into one of four categories:

1. `causal_invariant`: cannot be optimized, including point-in-time visibility, no-lookahead
   ordering, next-session execution, immutable input identity, and deterministic accounting;
2. `active_tunable_policy`: currently controls engine behavior and may be offered to a later
   optimizer with an explicit domain, including supported CANSLIM-derived thresholds, weights,
   gates, and portfolio/exit parameters;
3. `active_fixed_policy`: currently controls behavior but is not yet connected to the optimizer;
   it must be reported truthfully before a later implementation makes it tunable; and
4. `advisory_or_unsupported`: accepted only at its compatibility value or rejected when changed,
   because it does not control the current engine path.

This work reports those categories and their actual values. It does not select parameter ranges,
run a search, compare candidate strategies, or promote any profile as optimal. A later optimizer
may diverge from the strict Task 11 profile while retaining the CANSLIM feature family and all
causal invariants.

## Evidence motivating the work

The sealed Task 11 replay already proves that cash/capacity did not suppress valid buys. In the
fixed 2023-01-01 through 2025-12-31 focus window, it reconciles:

```text
375,703 evaluated symbol-days
8,439 technical setups
1,926 C passes / 5,712 finite C failures / 801 C unavailable
627 A passes / 1,136 finite A failures / 163 A unavailable
238 RS passes / 173 composite-qualified
173 attempted = 131 executed + 42 next-open buy-zone rejections
```

The scalar C/A values and entry reasons exactly match the existing evaluator, but current APIs
discard the selected metric family, fiscal-period pair, source public dates, and detailed reason
for an unavailable result. The supporting local, hash-bound PIT bundle and SEC audit sidecar exist
and are sufficient to expose them without a network request or a portfolio replay.

Separately, Task 11 records `position_size_pct=0.125`, `take_profit_pct=0.40`, and
`scale_out_fraction=0.50`; the engine actually sizes positions from `position_risk_pct /
stop_loss_pct` and uses global 25%-at-10/15/20% scale-out tiers. Those reported fields must not be
treated as optimizer controls.

## Non-goals and invariants

- Do not change C/A scoring, C/A growth values, metric-family priority, fiscal matching, entry
  gates, RSI/RS logic, proper-base logic, buy-zone handling, sizing, exits, order timing, or
  portfolio state.
- Do not add external data access, provider fallback, a new backtest, a portfolio replay, or raw
  ticker/SEC-payload output.
- Do not label Task 11 as full strict CANSLIM. Its M gate is disabled and I/L evidence remains
  incomplete.
- Do not make `take_profit_pct` or `scale_out_fraction` effective in this work; expose actual
  global tiers instead. A future exit-policy decision owns that behavioral change.
- Do not encode exact CANSLIM thresholds or the strict Task 11 profile as universal optimizer
  constraints. Record them as the reference run's effective policy and distinguish supported
  tunables from causal invariants.
- Do not add or run pytest/unit tests until the user-authorized build/diagnosis phase has ended.
  Direct deterministic CLI reconciliation, import/compile checks, and `git diff --check` are
  permitted implementation evidence.
- Existing `evaluate_c()` and `evaluate_a()` outputs must remain exactly identical for every input.
  New trace APIs must call the same internal evaluator core, never a diagnostic reimplementation.
- All Task 11 diagnostics must verify the closed Task 11 authority, source hashes, regular-file
  status, and byte content before parsing. Caller-supplied profile objects or sidecar paths cannot
  become an authority source.

## Component design

### 1. Shared C/A evaluator traces

Add a focused trace model under `core/canslim/` with closed string domains and frozen dataclasses.
The common trace fields are:

```text
metric_family: diluted_eps | basic_eps | net_income | unavailable
terminal_reason:
  complete
  no_visible_observation
  no_comparable_prior_period
  insufficient_annual_history
  nonfinite_current_value
  nonfinite_prior_value
  zero_prior_value
  negative_prior_value
  evaluator_exception
current_period_end: ISO date | null
prior_period_end: ISO date | null
current_public_date: ISO date | null
prior_public_date: ISO date | null
```

`CTrace` additionally exposes the existing score and quarterly growth. `ATrace` exposes the
existing score, annual growth, and ROE. Internal trace-only numeric values remain in process for
parity validation; the initial CLI publishes aggregates only.

Refactor `evaluate_c()` and `evaluate_a()` into internal core evaluators that select the same
metric family in the existing order:

```text
Diluted EPS -> Basic EPS -> Net Income
```

The public scalar APIs continue to return their current tuples by delegating to that core.
`evaluate_c_with_trace()` and `evaluate_a_with_trace()` return the trace result from that exact
same core. Existing broad exception behavior remains decision-compatible but becomes the closed
`evaluator_exception` terminal reason in trace mode; raw exception text is never emitted.

For C, the trace captures the unique fiscal-year comparator selected by the existing 28-day
tolerance. For A, it captures the evaluator's actual latest-two-non-null annual pair; it must not
claim fiscal-year matching where the production evaluator does not perform it.

### 2. PIT-visible provenance frame

Add an opt-in provenance mode to the PIT bundle's existing fundamental-frame construction. The
default `fundamentals_as_of()` / provider path remains byte-for-byte equivalent for normal engine
calls. In provenance mode only, the returned frame retains the source row's `public_date` alongside
the existing metric and period-end data so the shared evaluator core can attach visible dates to the
selected current and prior observations.

The PIT data layer continues to admit only records with `public_date <= as_of_date`, use the latest
visible amendment for duplicate fiscal periods, and operate entirely from the local SQLite bundle.
No `fundamentals_provider` fallback to a live source is added.

The Task 11 diagnostic independently verifies the local `fundamentals_provenance.json`,
`fundamentals.csv`, and `fundamentals_audit.csv` chain before it reads provenance metadata. The
audit sidecar is used only to bind record-level SEC source provenance; it does not add facts to an
as-of frame or alter selection behavior.

### 3. Task 11-only C/A provenance command

Add an explicit `pit_diagnosis.py diagnose-task11-ca-provenance` command. It accepts only the
closed `strict-proper-base-task11` profile, verifies the sealed replay as Task 11B does, and requires
the exact Task 11 PIT bundle. It uses verified ephemeral byte snapshots for consumed replay ledger
files and verifies the PIT bundle plus provenance/audit sidecars against the sealed authority
before opening them read-only.

The command scans only `technical_setup_eligible=True` signal rows in the fixed 2023-01-01 through
2025-12-31 window. For each row it obtains the visible PIT frame for that signal session, calls
both trace APIs, and fails closed unless scalar score/growth/availability exactly reconcile to the
sealed signal ledger. It does not create pending orders, run portfolio exits, or evaluate a new
candidate universe.

Its canonical JSON output is aggregate-only and includes:

- Task 11 authority/bundle/provenance identities and `fidelity_incomplete` label;
- evaluated technical C and conditional-A cohorts by year;
- selected metric-family counts by C/A outcome;
- terminal unavailable-reason counts;
- current/prior public-date visibility category counts;
- scalar/availability reconciliation status and fixed expected funnel totals.

It omits ticker identifiers, CIKs, accession numbers, source URLs, metric values, and raw filing
content.

### 4. Effective-engine-policy contract

Create one canonical JSON-friendly policy builder owned by `PortfolioSimulator`. It emits a
versioned `effective_engine_policy` mapping and a deterministic SHA-256 over its canonical JSON.
The policy records the behavior actually used by a run:

- canonical entry-contract thresholds and proper-base mode;
- M/regime gate state and cash-deployment behavior;
- capacity and eviction behavior;
- risk-sizing formula and its actual inputs;
- stop, breakeven, EMA, stagnation, early-winner, and actual scale-out-tier policy;
- non-M composite weights and institutional-data reweighting behavior;
- explicitly advisory/unsupported parameter names.

Each recorded field also carries its optimizer classification:
`causal_invariant`, `active_tunable_policy`, `active_fixed_policy`, or
`advisory_or_unsupported`. The strict Task 11 threshold values are identified as reference-profile
values, not performance targets or immutable strategy requirements.

The normal run summary and Task 11 wrapper manifest publish the policy object and digest. This is
observability only: current default behavior and the sealed historical Task 11 artifact remain
unchanged.

For exposed settings that the engine cannot honor, non-default requests must fail before a backtest
starts with an error naming the effective policy source. This covers the currently advisory/inert
entry-score requests, `min_technical_score`, `position_size_pct`, `take_profit_pct`, and
`scale_out_fraction`. Requests equal to the existing default/canonical values remain accepted for
compatibility but are marked advisory in the effective policy. The implementation does not change
the global tiers or permit an optimizer to mutate them through an unrelated argument.

## Error handling and authority boundary

- Any profile, manifest, bundle, sidecar, frame, schema, date, non-finite scalar, or reconciliation
  mismatch raises a descriptive error before JSON output.
- The command does not trust an expected hash, source path, or fidelity label parsed from a
  mutable run file. It derives expected identities from the canonical Task 11 authority and verified
  provenance chain.
- A sidecar swap, symlink/reparse point, bad digest, unknown trace reason, missing visible
  fundamental, duplicate diagnostic input, or off-window signal fails closed.
- A finite growth result below 25% is reported as finite/below-threshold by the aggregation layer;
  it is not represented as evaluator unavailability.

## Direct verification plan

No pytest/unit-test work occurs in this phase. Instead, implementation acceptance requires:

1. direct scalar parity checks between each legacy evaluator and its trace wrapper across all
   Task 11 2023–2025 technical setup frames;
2. direct provenance CLI reconciliation to the sealed counts: 8,439 technical setups; 1,926 C
   passes / 5,712 finite C failures / 801 C unavailable; 627 A passes / 1,136 finite A failures /
   163 A unavailable;
3. explicit failure for a wrong Task 11 profile, a forged profile subclass, a wrong bundle hash,
   and a wrong provenance/audit digest;
4. direct effective-policy output inspection proving actual global scale-out tiers and risk sizing
   are recorded, while inert fields are marked advisory;
5. direct rejection of one non-default unsupported setting without running a backtest;
6. `python -B -m py_compile` for touched modules and `git diff --check`.

No assertion above establishes a performance improvement. The deliverable establishes faithful
measurement and a truthful optimizer surface before later strategy changes. It also makes clear
that later optimization is free to loosen or tighten supported CANSLIM-derived policies while
maximizing return and minimizing drawdown under the causal invariants.

## Rollout order

1. Implement and independently review shared trace models/evaluator-core parity.
2. Implement and independently review opt-in PIT provenance frames and sealed sidecar verification.
3. Implement and independently review the Task 11 aggregate provenance CLI, then run it once on
   the sealed replay.
4. Implement and independently review the effective-engine-policy builder and unsupported-setting
   rejection; inspect the output without changing exit semantics.
5. Only after these steps, hand the truthful policy surface to the separate multi-agent
   optimization loop. That loop owns parameter ranges, objective weighting (return versus
   drawdown), and any decision to loosen or tighten CANSLIM-derived policies.
