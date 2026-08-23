# Task 2 report — daily PIT evaluation and next-open outcomes

## Scope delivered

- Full `CanslimStrategy` evaluation now consumes Task 1's canonical
  `CanslimEntryFacts`, applies the shared C/A/RS/non-M entry-composite decision,
  keeps the legacy M-inclusive `canslim_score`, and applies market permission
  afterward. PEG remains diagnostic only. Technical-only mode performs no
  fundamental call and uses only canonical technical setup eligibility.
- PIT signals expose only JSON-safe scalar facts and decisions, including the
  legacy recall fields plus `entry_composite_score`,
  `entry_contract_eligible`, ordered comma-separated blockers, pivot,
  prior-volume baseline, volume ratio, extension, and price-advance facts.
- Only `pit_baseline.py` binds `signal_every_n_days=1`; the generic simulator
  and CLI default remain five sessions. PIT publication rejects any result whose
  config does not prove daily cadence and the fixed canonical thresholds.
- Added frozen/slotted `EntryAttemptOutcome` rows with stable primitive
  serialization. Every normal `_enter_position` terminal path records one
  execution or exact rejection outcome. Finite positive pivots require the next
  session open to be inclusively between pivot and `pivot * 1.05` before cash
  or risk sizing. Missing/non-positive/non-finite pivots keep legacy pass-through.
- Checkpoint schema is now v2. Outcomes are persisted in both checkpoint state
  and the append-only state journal, compared on resume, and restored through
  completed-cache reads. Schema v1 fails closed. Pending signal date and pivot
  remain flattened primitives.
- Reconciliation consumes concrete outcomes, requires one unique outcome per
  attempt, matches executions exactly to BUY rows, forbids BUYs for rejections,
  matches every rejection diagnostic exactly, and attributes next-open, cash,
  and attempted-capacity failures by symbol. Capacity truncation and the final
  pending session remain separate aggregates.
- PIT publication adds immutable `entry_attempt_outcomes.csv` and
  `daily_entry_funnel.csv`, includes both in artifact hashes, records the
  explicit outcome schema/count in the manifest, and includes aggregate daily
  evaluated/qualified/attempted/executed/rejected counts in summary/report.

## Direct offline functional probes

All probes used the workspace Python 3.13 runtime and exited `0` in their final
form.

```text
strategy_probe=passed {'full_buy': True, 'peg_non_bypass': True, 'market_separate': True, 'technical_fundamental_free': True, 'entry_composite': 95.29411764705881, 'legacy_canslim': 81.0}
outcome_probe=passed {'lower': 'entries_executed', 'upper': 'entries_executed', 'below': 'entry_rejected_next_open_buy_zone', 'above': 'entry_rejected_next_open_buy_zone', 'missing_pivot': 'entries_executed', 'missing_bar': 'entry_rejected_missing_data', 'missing_open': 'entry_rejected_missing_data', 'invalid_open': 'entry_rejected_invalid_price'}
checkpoint_probe=passed schema_v1_rejected=True schema_v2_round_trip=True journal_round_trip=True completed_cache=True pending_signal_flattened=True
actual_resume_probe=passed {'schema': 2, 'outcomes': 1, 'cached_transactions': 2}
reconciliation_probe=passed {'attempts': 2, 'executions': 1, 'rejections': 1, 'next_open_by_symbol': {'BBB': 1}, 'capacity_attempted': 1, 'capacity_truncated': 1, 'final_pending': 1, 'adversarial_rejections': 3}
daily_simulator_probe=passed {'sessions': 31, 'evaluations': 31, 'signal_days': 31}
artifact_probe=passed {'generic_default': 5, 'runner_binding': 1, 'outcome_columns': ['symbol', 'signal_date', 'entry_date', 'pivot', 'buy_zone_lower', 'buy_zone_upper', 'entry_open', 'outcome'], 'daily_totals': {'evaluated_count': 1, 'qualified_count': 1, 'attempted_count': 1, 'executed_count': 1, 'rejected_count': 0}, 'non_daily_rejected': True}
```

The reconciliation adversarial cases rejected a duplicate outcome, an outcome
counter mismatch, and a BUY paired with a rejection. A separate capacity case
proved attempted-capacity attribution does not absorb capacity truncation, and
a final-session signal remained explicitly pending without an outcome.

The first checkpoint probe attempted to use the Windows user temp directory,
which the workspace sandbox denied. The same production checkpoint functions
then passed against a fresh workspace-local probe directory; all temporary
probe files were removed afterward.

## Verification and self-review

- Relevant imports and `py_compile`: exit `0`.
- Ruff on the three production files: `All checks passed!` (exit `0`).
- `git diff --check`: exit `0` (line-ending conversion warnings only).
- No unit tests or broad test suite were run, per the operator-required
  functional-first sequencing and Task 7 ownership.
- Self-review confirmed rejection precedence is unchanged through invalid
  price, the new zone check precedes cash/risk sizing, no finite-pivot attempt
  falls back to a prior close, outcomes are mutually exclusive, and generic
  cadence/exits/sizing/cash/capacity behavior is otherwise unchanged.

## Remaining concerns and deferred work

- Existing unit fixtures encode checkpoint schema v1 and aggregate-only
  reconciliation. Task 7 must update those fixtures after the functional
  replays; production intentionally rejects the old five-day checkpoint.
- Task 3 must perform the first real five-year replay to measure artifact size,
  daily throughput, and observed next-open rejection distribution. No strategy
  thresholds were tuned in Task 2.

## Independent-review fix round 1

Closed all seven Important findings without changing strategy thresholds,
position sizing, exits, or the generic five-session cadence:

- `_enter_position` now validates the candidate bar, finite open, and finite-
  pivot next-open buy zone before capacity eviction. Already-open remains the
  first rejection. A rejected overextended candidate cannot sell an existing
  holding; a valid in-zone higher-RS candidate still follows the existing
  eviction path.
- `SimulationResult.entry_outcomes` is appended after the historical
  `benchmark_symbol` positional field.
- Leader attribution requires `entry_composite_score` and the canonical fixed
  `MIN_COMPOSITE_SCORE`; the legacy M-inclusive floor argument remains accepted
  only for caller compatibility.
- Reconciliation binds each outcome to its exact qualifying signal pivot and
  each successful outcome to one BUY at `round(entry_open, 4)`. It rejects
  pivot/price mismatches, missing or impossible per-outcome facts, duplicate
  outcomes/BUYs, and rejected outcomes with BUYs.
- Every numeric strategy row field is normalized to a built-in finite `float`
  or `None`. The full decision's normalized C/A/RS/non-M composite values are
  authoritative, so pre-Task-4 non-finite fundamentals fail closed and
  `json.dumps(row, allow_nan=False)` succeeds.
- PIT validation now covers every signal row: nonblank uppercase symbol,
  benchmark session, and one `(symbol, signal_date)` evaluation. The daily
  funnel proves its evaluated total equals the source log.
- The fixed no-market-gate baseline requires rowwise
  `buy_signal == entry_contract_eligible`, technical eligibility and a finite
  positive pivot for every qualifier, per-day
  `executed/rejected <= attempted <= qualified`, and exact global accounting
  across attempts, capacity truncation, and final-session pending signals.

Fresh narrow probes all exited `0` in their final form:

```text
engine_reconciliation_probe=passed {'no_evict_rejected': True, 'valid_evict_executed': True, 'legacy_benchmark': 'QQQ', 'composite_fail': 1, 'rounded_price': 100.1235}
nan_json_probe=passed {'current_growth': None, 'c_score': None, 'canslim_score': None, 'entry_composite_score': None, 'buy_signal': False, 'has_peg_today': True}
daily_validation_probe=passed evaluated=2 qualified=1 attempted=1 executed=1
prior_outcome_boundaries=passed
prior_strategy_contract=passed {'market_separate': True, 'peg_non_bypass': True, 'technical_fundamental_free': True}
outcome_shape_regression=passed {'terminal_shapes': 7, 'legacy_missing_pivot': True, 'final_pending': 1, 'capacity_truncated': 1}
checkpoint_regression_probe=passed {'v1_rejected': True, 'v2_loaded': True, 'journal_outcome_roundtrip': True}
```

Adversarial rejection probes covered mismatched signal pivot, mismatched BUY
price, invalid-price outcomes carrying an open, capacity outcomes missing the
validated open, duplicate/all-row/off-calendar/lowercase evaluations,
buy/eligibility mismatch, missing qualifying pivot, missing technical setup,
and attempts plus truncation exceeding qualifications.

The first checkpoint rerun used a Python-created Windows temporary directory
whose ACL denied the production atomic writer. The exact temporary directory
was removed, then the same production v1/v2/journal probe passed in a
PowerShell-precreated workspace directory, which was also removed. This was an
environmental probe-path failure, not a product-code failure.

Final relevant import/`py_compile`, Ruff, and `git diff --check` verification
passed. No unit or broad test suite was run, preserving the operator-required
functional-first sequencing; Task 7 still owns fixture migration and the full
suite.

## Independent-review fix round 2

Two residual causal-integrity defects found by the final re-review are closed:

- Replacement risk and target position value are now computed and validated
  before `_try_evict` can mutate the portfolio. A full portfolio with a
  zero-risk replacement retains its existing holding and records
  `entry_rejected_invalid_risk`; a valid in-zone, valid-risk replacement still
  evicts and executes using the released cash.
- The fixed PIT baseline already binds `max_positions=None`, so it now rejects
  any nonzero `capacity_truncated_signals` or `entry_rejected_capacity`. Entry
  outcomes must equal the complete set of qualifying non-final-session signal
  keys; only final-session qualifiers may remain pending. Generic capped
  simulator reconciliation remains unchanged.

Fresh bounded probes passed both invalid-risk/valid-risk eviction paths,
rejected the prior `AAA` non-final + `BBB` final impossible-capacity ledger in
both portfolio validation and daily-funnel generation, and accepted the exact
one-outcome-plus-one-final-pending control. No unit or broad suite was run.
