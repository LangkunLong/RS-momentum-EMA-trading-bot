# Task 5 implementation report — strict PIT proper-base entries

## Status

Implemented the strict-PIT proper-base integration using only
`core.pit_diagnosis.patterns.detect_proper_base` and
`BasePolicy.canonical_v1()`.

## Changed files

- `core/canslim/entry_contract.py`: `build_entry_facts` now accepts optional,
  explicitly named `history_before_event`, `event_session`, and
  `require_proper_base` arguments.  The legacy two-argument path is unchanged.
  With `require_proper_base=True`, it calls the canonical detector and uses
  only the returned pattern pivot.  A missing, malformed, insufficient, or
  non-qualifying history appends `proper_base_unavailable`, leaves the pivot
  unavailable, and therefore fails closed without a rolling-high fallback.
- `backtest.py`: `_evaluate_technical_at_date` receives the strict switch and,
  only in strict mode, passes the exact pre-event completed-session OHLC rows
  and event session to the canonical entry-fact builder.
- `core/backtest_engine.py`: a non-technical-only simulator backed by a
  validated `pit_bundle` enables the strict switch.  The strategy evaluation
  and execution-boundary canonicalization use that same switch and pass the
  same causal pre-event history shape, preventing signal/execution pivot
  disagreement.  Technical-only and non-PIT paths retain the legacy behavior.

No thresholds, same-day 1.30x volume confirmation, buy-zone width, C/A/RS or
composite gates, sizing, cadence defaults, cash policy, optimizer, or detector
implementation were changed.  No tests or downloads were run.

## Verification

```text
python -m py_compile core/canslim/entry_contract.py backtest.py core/backtest_engine.py
```

Passed.

Synthetic read-only probe constructed a 50-session flat base and used a
separate event session with a 1.30x event-volume bar:

```text
valid 100.0 ()
no_pattern None ('proper_base_unavailable',)
legacy 105.0 ('close_below_pivot',)
```

This confirms that strict mode takes the canonical flat-base pivot, no-pattern
input fails closed with the required explicit reason, and the two-argument
legacy call still uses its prior rolling-close pivot behavior.

Bounded strict-PIT replay:

```text
python -B backtest_pnl.py --tickers NVDA --start-date 2024-01-02 --end-date 2024-04-30 --signal-days 1 --pit-bundle .artifacts/pit-baseline-roe2/pit_baseline.sqlite3 --pit-bundle-sha256 8d5bdca9ee517ccfb739e44f1245eb5b2253bd88a0fbed30c3c91ee7aaadd3e0 --no-csv
```

The strict full-CANSLIM PIT run completed in 21.6 seconds.  It reported 24
evaluated signal rows across 24 candidate signal days, zero entry attempts,
zero executions, and zero entry rejections.  It completed normally with no
`proper_base_unavailable` crash.

Completed strict-PIT replay (coordinating task): one symbol (`NVDA`),
2023-01-01 through 2025-12-31, daily signals, using the corrected PIT bundle.
The process completed successfully with 666 evaluated rows, three buy signals,
three entry attempts, two executions, and one next-open buy-zone rejection.
There were zero cash, capacity, and risk rejections.  Total return was +0.3%
versus SPY at +79.0%, and there was no `proper_base_unavailable` crash.

## Concern

The completed long-window verification is intentionally limited to one symbol;
the six-symbol attempt was previously CPU-bound for about ten minutes without
artifacts and was stopped by the coordinating task.
