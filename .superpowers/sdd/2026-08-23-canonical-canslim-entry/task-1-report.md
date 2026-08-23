# Task 1 report — shared completed-session entry contract

## Scope delivered

- Added immutable `CanslimEntryFacts` and `CanslimEntryDecision` models plus
  pure fact/decision builders in `core/canslim/entry_contract.py`.
- Bound technical eligibility to exactly 50 prior volumes (event excluded), an
  up-to-252-session prior-close pivot, an advancing event close, `1.30x`
  volume, and the inclusive pivot-through-`+5%` zone. Used inputs and all full
  decision inputs must be finite.
- Added a separately named non-M, renormalized `entry_composite_score` to the
  live CANSLIM view while preserving legacy M-inclusive `total_score`.
- Made live screening consume the full shared decision and apply market status
  only when classifying actionable versus watchlist output.
- Replaced after-close local near-high/event-inclusive-volume gates with shared
  technical setup facts. RS remains a reported ranking fact; market remains an
  advisory execution blocker.
- Made the simple backtest use shared technical setup eligibility. The existing
  `BUY_SIGNAL` column remains a compatibility alias that is explicitly labeled
  as a technical approximation. Power gap remains diagnostic and cannot bypass
  the setup.

## Direct functional probes

Final boundary probe exited `0` with these results:

```text
exact_threshold_pass eligible=True pivot=100.0 prior_avg=100.0 ratio=1.30 extension=0.05000000000000004
current_growth_epsilon eligible=False reasons=('current_growth_below_threshold',)
annual_growth_epsilon eligible=False reasons=('annual_growth_below_threshold',)
rs_epsilon eligible=False reasons=('rs_score_below_threshold',)
composite_epsilon eligible=False reasons=('composite_score_below_threshold',)
event_volume_exclusion prior_avg=100.0 event_volume=10000.0 ratio=100.0
current_close_pivot_exclusion event_close=105.0 pivot=100.0
price_advance_epsilon eligible=False reasons=('close_not_above_prior_close',)
volume_epsilon eligible=False reasons=('volume_ratio_below_threshold',)
below_pivot eligible=False reasons=('close_below_pivot',)
overextension eligible=False reasons=('close_above_buy_zone',)
nonfinite_input eligible=False reasons=('non_finite_close_input',)
exact_plus_5_acceptance in_buy_zone=True
live_scanner_market_independence contract=True bull=('actionable_buy', []) bear=('watchlist_candidate', ['market_not_bullish'])
peg_non_bypass technical_setup=False despite_has_peg_today=True
after_close_shared_path technical=True tomorrow=False pivot=100.0 ratio=1.30 execution_blocker=market_not_bullish
simple_backtest_shared_path technical=True blockers=() peg_diagnostic=False
```

The live core/scanner adapter probe also exited `0`:

```text
core_market_independence bull_total=80.0 bear_total=69.5 entry_composite=80.00000000000001421 bull_contract=True bear_contract=True
live_scanner_shared_adapter bull=actionable_buy bear=watchlist_candidate bear_notes=['market_not_bullish']
```

The first exploratory form of each probe used exact equality for a derived
floating-point `0.05`/`80.0` value and failed on normal binary representation.
The final probes used `math.isclose` for reported derived values while retaining
exact inclusive eligibility assertions; no production change was needed.

## Static verification

- Relevant `py_compile`: exit `0`.
- Relevant Ruff check: `All checks passed!` (exit `0`).
- Relevant imports: exit `0`.
- `git diff --check`: exit `0`.
- Unit tests and the broad suite were intentionally not written or run, per the
  operator-requested sequencing.

## Self-review and concerns

- No executable caller retains a local pivot/volume/buy-zone fallback.
- No executable qualification path uses the diagnostic power-gap flag.
- Market is absent from the shared contract API and from the entry composite.
- Existing unit tests that encode the superseded near-high, RS-gated
  after-close, configurable live thresholds, simple-backtest score floor, or
  PEG-bypass behavior will require intentional updates in Task 7. They were not
  changed in Task 1.
- Task 2 must consume `entry_facts`/`entry_decision` in the PIT strategy; the
  PIT-local cadence, PEG branch, and next-open behavior were intentionally not
  changed here.
