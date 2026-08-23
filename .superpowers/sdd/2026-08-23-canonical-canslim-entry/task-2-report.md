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
