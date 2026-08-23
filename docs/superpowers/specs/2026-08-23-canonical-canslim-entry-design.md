# Canonical CANSLIM Entry Contract Design

## Purpose

Make the five-year point-in-time replay measure one coherent CANSLIM entry
contract instead of the three divergent contracts currently used by the PIT
engine, simple backtest, and live scanner. The first corrected replay must
measure strategy logic, not optimize thresholds.

The change is intentionally narrow: align completed-session technical facts,
scan the PIT universe daily, enforce next-open price actionability, remove the
unvalidated power-gap bypass from executable CANSLIM entries, correct two known
fundamental identity/value defects, and report both raw and historically exposed
leader recall.

## Non-goals

- Do not tune the 25% C/A, RS 80, composite 70, 1.30x volume, or +5% buy-zone
  thresholds.
- Do not add breakout persistence, lower volume requirements, change exits,
  sizing, cash policy, or market-regime policy.
- Do not solve the 2020 warm-up limitation in this change.
- Do not silently turn the simple technical-only backtest into a full-fundamental
  CANSLIM backtest. It remains available and must be labeled as an approximation.
- Do not give the optimization loop permission to alter the canonical contract.

## Canonical entry facts

One shared pure module owns the completed-session technical facts used by PIT,
the simple backtest, the after-close snapshot, and live screening.

For an event session `t`:

1. Require the event close and at least 50 prior completed volume observations.
2. Compute average volume from exactly `t-50` through `t-1`; the event volume is
   excluded from its own baseline.
3. Compute the pivot solely from closes before `t`, using the greatest close in
   the prior 252 available sessions.
4. Require `close[t] > close[t-1]`.
5. Require `volume[t] / prior_average_volume_50 >= 1.30`.
6. Require `pivot <= close[t] <= pivot * 1.05`.

The full CANSLIM decision additionally requires:

- current quarterly growth at least 25%;
- annual growth at least 25%;
- relative-strength score at least 80; and
- composite score at least 70.

Missing or non-finite inputs are unavailable, never successful comparisons.
The decision carries ordered machine-readable blocking reasons plus the exact
pivot, prior-volume baseline, volume ratio, and extension.

## Market and PEG semantics

Market regime is execution permission layered after entry-contract
qualification. It must not change whether a setup itself satisfies CANSLIM.

The existing `power earnings gap` detector is price/volume-only and is not tied
to a filing or earnings event. It remains available as a diagnostic/scoring fact
but cannot bypass C, A, RS, composite, pivot, volume, or buy-zone requirements.
Any future executable PEG feature requires its own point-in-time earnings-event
contract and review.

## Caller integration

### PIT portfolio simulator

`CanslimStrategy` consumes the shared completed-session facts and full decision.
`pit_baseline.py` explicitly constructs the simulator with
`signal_every_n_days=1`; the generic simulator default remains unchanged for
compatibility. Every qualifying close is queued at most once for the next
session.

At the next open, the engine revalidates `pivot <= open <= pivot * 1.05` before
cash/risk sizing. A rejection is recorded as
`entry_rejected_next_open_buy_zone` and in an immutable per-attempt outcome row
with symbol, signal date, entry date, pivot, open, zone bounds, and outcome. This
keeps reconciliation causal instead of inferring symbol failures from aggregate
counters.

The new cadence and outcome schema cannot resume the prior five-day checkpoint.
Fresh corrected replays never pass the old checkpoint path.

### Live scanner and after-close snapshot

Live screening consumes the same technical facts and full C/A/RS/composite
decision. Market status only controls actionable execution classification.

The after-close snapshot does not fetch fundamentals; it therefore reports the
shared technical setup decision and must not call it a full CANSLIM entry.

### Simple backtest

The simple backtest consumes the same completed-session technical facts and no
longer treats PEG as a technical bypass. Its existing technical-only/public CLI
remains supported. Where it lacks point-in-time fundamentals it is explicitly a
technical approximation, not evidence that the full CANSLIM contract passed.

## First functional replay

Before broad tests, run a fresh immutable 2021-01-01 through 2025-12-31 replay
against the existing SHA-bound PIT bundle. Do not resume the preserved five-day
checkpoint. The run must complete its manifest/reconciliation gates and report:

- daily evaluated symbol-days;
- contract-qualified signals;
- next-open executions and buy-zone rejections;
- cash/capacity/missing-data rejections;
- raw top-100 recall; and
- PIT-exposed top-100 recall.

This first replay isolates entry-contract/cadence effects on the existing data.

## Fundamental corrections and second replay

After the first replay:

1. Treat non-finite C/A inputs as unavailable so CAH cannot emit `NaN` growth or
   scores.
2. Bind XOM to reviewed historical issuer CIK `0000034088`, regenerate the SEC
   normalized outputs and PIT SQLite bundle from the already downloaded pinned
   archives, and publish only to fresh paths.
3. Rerun the corrected daily baseline against the regenerated bundle.

The XOM correction is an identity fix, not a fuzzy lookup. Existing immutable
SEC and bundle artifacts remain unchanged.

## Recall reporting

Five-year recall has two explicit denominators:

- `raw_*`: all 100 ex-post five-year leader labels;
- `pit_exposed_*`: the subset whose `member_at_start` is true.

Rolling recall similarly reports all labels and the subset with
`member_at_evaluation=true`. Percentages are computed as
`100 * numerator / denominator`, with zero denominator producing `0.0`.
Ambiguous count-as-percent fields may remain only as deprecated raw aliases.

## Acceptance criteria

- All four signal-producing/reporting paths use the same prior-bar pivot and
  prior-50 volume fact builder.
- PIT baseline config proves daily cadence.
- No full CANSLIM entry is executable solely through the old PEG branch.
- Every queued finite-pivot signal is next-open zone checked and reconciled.
- A fresh existing-bundle replay completes before data regeneration.
- CAH never emits non-finite C/A values.
- Rebuilt security master maps XOM to `0000034088` and emits XOM fundamentals.
- A fresh rebuilt-bundle replay completes.
- Reports distinguish raw from PIT-exposed five-year and rolling recall.
- Focused regressions, full offline suite, Ruff, compile, diff checks, and
  independent code review pass before branch completion.
