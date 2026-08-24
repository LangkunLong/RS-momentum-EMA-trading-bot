# Task 6 — corrected five-year replay and independent audit

Status: complete. This report records the immutable replay after the Task 4
fundamental corrections and the independent raw-output/state audit. It is a
diagnostic baseline; no optimization or threshold search was run.

## Inputs and run identity

- Bundle:
  `.artifacts/task-4-regeneration-20260823T223000Z/pit-bundle/pit_baseline.sqlite3`
- Bundle SHA-256:
  `1af306ef1e46797473cd186fc48938ed6694ae25f5943c9f1905b528307cc2eb`
- Producer commit: `d555f7f4c7727d9c6a440bba50cced0fbe9f3095`
- Evaluation window: `2021-01-01` through `2025-12-31`
- Benchmark: `SPY`
- Leader basket: 100 leaders, rebalanced every 20 sessions
- Checkpoint cadence: every 20 sessions
- Explicit data exception: `--allow-incomplete-fundamentals`
- Output directory:
  `.artifacts/task-6-corrected-replay-20260824T100045Z/run-20260824T100213Z-1af306ef1e46/`
- Run manifest status: `complete`
- Final run-manifest SHA-256:
  `efeb8c3e9b0189131f04361122395f8ea89adfd0f8bcbb781e333a9359d43bd3`

The run contains the signal log, daily funnel, entry outcomes, transactions,
equity curve, checkpoint, progress journal, state journal, leader recall, and
leader-basket artifacts. The checkpoint is owned by the run directory and is
not a split-publication checkpoint.

## Entry-funnel result

The independent auditor recomputed the causal ledger from the immutable public
outputs:

| Metric | Count |
| --- | ---: |
| Daily sessions | 1,255 |
| Evaluated symbol-days | 597,764 |
| Qualified canonical entries | 286 |
| Executed entries | 225 |
| Rejections | 61 |
| Next-open buy-zone rejections | 51 |
| Cash rejections | 10 |
| Capacity/invalid/missing/already-open rejections | 0 |

Only 10 attempts lacked cash; the dominant loss of qualified entries was the
causal next-open buy-zone check. Average cash was 67.31359377429541%, so the
idle-cash result is primarily a signal/funnel result rather than a hidden
position-capacity policy.

## Performance and recall

- CANSLIM strategy total return: `-9.994717769465932%`
- Annualized return: `-2.0874097904821753%`
- Maximum drawdown: `-13.664400600134591%`
- Sharpe: `-0.2082076838233604`
- Independent 100-leader basket total return:
  `65.0550778875779%`
- Leader-basket average cash: `0.0796812749003984%`
- SPY total return: `84.79009133533889%`

Five-year raw leader recall was 50/100 signaled and 47/100 executed. Restricting
the denominator to leaders exposed to the PIT membership/data window gives
42/72 signaled and 40/72 executed. Rolling raw recall was 29/4,800; rolling
PIT-exposed recall was 29/3,700. These values describe the observed data and
canonical-entry funnel and are not optimization objectives.

## Independent audit evidence

The auditor matched the replay state and result ledgers and confirmed a fresh
single-directory state. Recomputed state hashes were:

```text
portfolio_checkpoint.json  71080a7c965a88fee48511e030b075979ea679d776bd3fd462ecc9a4a25878a
portfolio_progress.jsonl   e9398cac0b34ac9089d59e484d5a7f5261610baac3a8bd866a8ec27fea711053
portfolio_state.jsonl      a4adf20fa91cb9e525c3b742bf83b543774790bedc0bf3e426f22096c217eae7
```

The only accepted audit exception is
`evaluated_pit_quarterly_and_annual_at_least_90_pct`; strict-PIT
quarterly-plus-annual fundamentals coverage is below the 90% target. The
exception is explicit in the run invocation and does not permit future-data
fallbacks.
