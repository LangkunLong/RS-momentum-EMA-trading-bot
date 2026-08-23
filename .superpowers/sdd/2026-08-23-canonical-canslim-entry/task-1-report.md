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

## Independent-review fix round 1

The original scope and probe record above describes commit `ee82414`. This fix
round supersedes its `BUY_SIGNAL` compatibility-alias wording without rewriting
that historical record.

### Important findings closed

- `build_entry_facts` now fails closed before all other blockers when close and
  volume lengths differ. Series-like inputs also require both inputs to carry
  exactly equal indexes; positional data with a one-session index shift cannot
  qualify. The new ordered reasons are `close_volume_length_mismatch` followed
  by `close_volume_index_mismatch` when both apply.
- Scanner RS and non-M composite overrides now use the effective floors
  `max(canonical floor, caller floor)`. The same effective RS floor is used by
  the bulk prefilter and candidate classification. Lower caller values cannot
  weaken 80/70; higher values tighten. Legacy fundamental/breakout strictness
  arguments remain accepted but inert.
- `backtest.py` retains `TECHNICAL_SETUP` as the completed-session diagnostic,
  computes a separate non-M entry composite using the existing active weights
  renormalized after excluding M, and derives `BUY_SIGNAL` from
  `evaluate_entry_contract` with point-in-time current/annual growth and RS.
  Market remains a separate reported fact and PEG remains diagnostic only.
  Existing columns remain, with additive `Entry_Composite_Score` and
  `Entry_Blockers` audit columns.

### Direct adversarial and boundary probes

The final direct probe exited `0` and reported:

```text
boundaries exact=True epsilons=False prior_volume=True prior_pivot=True plus5=True market_independent=True peg_non_bypass=True
alignment ('close_volume_length_mismatch', 'close_volume_index_mismatch', 'insufficient_prior_volume_history') ('close_volume_index_mismatch',)
scanner canonical_floor=True tightening=True bulk_consistent=True
after_close True False
backtest [{'TECHNICAL_SETUP': True, 'BUY_SIGNAL': False, 'Entry_Blockers': 'current_growth_below_threshold'}]
```

The scanner probe separately exercised lower 0/0 caller floors, stricter 90 RS
and 90 composite floors, and both lower/stricter bulk RS prefilters. The
backtest helper separately rejected epsilon-low C, A, RS, and composite inputs
while ignoring bearish market and refusing a PEG bypass. No unit tests or broad
suite were run, per the required sequencing.

### Fix-round static verification

- Relevant imports: `relevant imports: ok` (exit `0`).
- Relevant `py_compile`: exit `0`.
- Relevant Ruff check: `All checks passed!` (exit `0`).
- `git diff --check`: exit `0` (line-ending conversion warnings only).
