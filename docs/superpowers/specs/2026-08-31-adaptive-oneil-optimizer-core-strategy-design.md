# Adaptive O'Neil Optimizer Core-Strategy Design

**Date:** 2026-08-31
**Status:** Approved for implementation

## Objective

Build a durable model-driven feedback loop that improves one adaptive O'Neil/CAN SLIM trading
strategy. The first readiness milestone is an absolute **10% annualized portfolio return** on a
disjoint retrospective constituent panel. The same architecture must support later search
campaigns aimed at 20% and 50% annualized return without another redesign.

The optimizer's job is strategy discovery. DeepSeek R1 may inspect the three editable strategy
policy files, choose a focused trading hypothesis, author the exact policy code, study the
resulting portfolio behavior, and refine or replace its hypothesis in later iterations. The
trusted local engine supplies point-in-time facts and evaluates every candidate. Candidate code
remains an artifact under `apply=false`; the optimizer does not edit the operator's checkout or
start the full replay.

Annualized-return milestones are objectives, not promises. A simulated result qualifies a
candidate for the next evaluation stage; it does not establish future live performance.

## Deliberately narrow scope

This upgrade focuses on the strategy and feedback loop. It does not add a new portfolio-risk or
execution-model framework.

In particular, this design does not introduce new:

- liquidity or average-daily-volume gates;
- slippage or transaction-cost gates;
- drawdown, Sharpe, turnover, or minimum-trade acceptance gates;
- weighted or continuous "robustness" score;
- year-by-year train/test regime assumptions;
- USD cost ceiling for the authorized optimizer campaign.

The existing simulator continues to report its normal diagnostics. Those values help R1 explain
what happened, but they do not compete with annualized return in a new composite score.

## Core O'Neil strategy contract

The system contains one strategy whose behavior adapts to current market evidence. It is not a
collection of separately trained bull, bear, and sideways strategies.

The trusted engine preserves the identity of the strategy:

- candidates originate from the existing CAN SLIM fact and technical-setup pipeline;
- leadership and relative strength remain central to ranking;
- bases, pivots, breakouts, buy zones, and volume confirmation remain entry concepts;
- market direction remains part of the decision;
- policy inputs remain engine-supplied point-in-time facts;
- entry signals use completed-session evidence and execute no earlier than the next eligible
  session, while this upgrade leaves the simulator's existing exit timing unchanged;
- leverage remains unavailable;
- the policy may select a tighter initial stop, but the engine caps its stop distance at 8% and
  retains final enforcement of the protective stop.

Within that strategy family, R1 is free to change the actual logic. It may coordinate changes
across:

- `core/strategy_policy/entry.py`: qualification thresholds, signal combinations, buy-zone
  selectivity, market permission, and candidate ranking;
- `core/strategy_policy/risk.py`: position capacity, allocation, exposure, and replacement of
  weaker holdings by stronger leaders;
- `core/strategy_policy/exit.py`: profit handling, winner retention, stagnation rules, moving
  average behavior, and protective-stop progression.

The model may change one, two, or all three files in a single candidate. The old small diff,
line-count, hunk-count, and single-file authoring restrictions are removed. The source boundary
is the three policy files and their trusted deterministic interface. Syntax, allowed-file,
pure-function, deterministic-import, and no-I/O validation remain because they keep strategy
decisions reproducible; they do not prescribe trading behavior. Full-source responses are sized
from the actual files and call token budget rather than an artificial strategy-change envelope.

## Adaptive market context

The local engine produces one immutable `MarketContextV1` for each decision session. Its lean
typed contents are:

- the ISO completed-session date, existing O'Neil market regime, distribution-day count, and
  follow-through flag;
- for each of SPY, QQQ, and IWM: close relative to the 50- and 200-session moving averages,
  expressed as decimal fractions, plus annualized 20-session close-to-close realized volatility;
- the fractions of active constituents above their 50- and 200-session moving averages; and
- median active-constituent RS score and the fraction with RS score at or above 80; and
- active-constituent count plus coverage fractions for the breadth and leadership measures.

Close-to-average values are `close / simple_moving_average - 1`. Realized volatility is the
sample standard deviation of the last 20 close-to-close returns multiplied by `sqrt(252)`.
Cross-sectional fractions use only active constituents with enough causal history for that
measure. Coverage is `eligible_constituents / active_constituents`; the excluded share is
`1 - coverage`. The three benchmark fields must be complete after a 200-session warmup, and an
empty eligible cross-section invalidates the evaluation plan rather than creating a synthetic
value.

Breadth and leadership facts are computed against the complete active PIT union, not whichever
tradable panel is being evaluated. This keeps the market observation identical across discovery
and qualification panels. Lookbacks, units, missing-value behavior, and the session timestamp
are versioned as part of the contract.

These are observations, not controller-selected strategy parameters. The same context is
available to entry, capacity/allocation, and exit policy decisions, allowing R1 to decide how the
strategy should adapt. For example, it may demand stronger breakouts when leadership is narrow,
increase exposure when breadth and follow-through agree, or protect gains more quickly when the
market weakens. The optimizer chooses the relationships in code instead of tuning a fixed grid
of regime multipliers.

The same context value is nested as `market` in entry, capacity, allocation, eviction, and exit
snapshots. The existing regime, distribution, and follow-through fields migrate out of
`EntrySnapshot` so there is only one source of truth. The engine computes the context before the
decision and validates its type and causal timestamp. Candidate code cannot query data, inspect
future sessions, or manufacture new market facts.

For an entry lifecycle, entry qualification and capacity selection use the signal session's
context directly. That exact context is stored with `PendingEntry` and carried forward for
next-open eviction and allocation. Those next-open decisions must not read an incomplete entry-
session bar. Exit decisions use the context of the just-completed session being processed under
the simulator's existing exit timing.

## Point-in-time constituent universe

The intended local PIT bundle is the deduplicated historical union of:

- S&P 500 constituents;
- Nasdaq-100 constituents; and
- Russell 2000 constituents represented by the IWM membership universe.

A security is eligible only when it belongs to at least one source universe on that session.
The bundle preserves source-index affiliation per session as well as the deduplicated union.
Ticker aliases and renames resolve to one stable security-lineage identity. Membership source,
source revision, retrieval provenance, and symbol mappings are sealed into the local bundle.
SPY, QQQ, and IWM are market references and are not tradable strategy holdings.

The bundle schema declares the exact sealed non-tradable reference-symbol set
`{SPY, QQQ, IWM}`. Price and price-identity provenance covers membership symbols plus that set,
while membership and fundamentals exclude the references. Bundle APIs distinguish tradable
membership symbols from reference price symbols so QQQ and IWM cannot accidentally enter a
candidate panel.

The current authenticated bundle contains historical S&P 500 membership only, and the current
public-membership adapter is S&P-specific. It also lacks QQQ and IWM price series. Before the
core feedback-loop canary, the local S&P-membership bundle is regenerated with authenticated
non-tradable QQQ and IWM reference histories so `MarketContextV1` is complete. This small bundle
upgrade does not wait for constituent-universe expansion.

Before final qualification, source-specific historical Nasdaq-100 and Russell 2000 membership
adapters must acquire and authenticate their local records, and the bundle builder must preserve
their per-source affiliations. Results cannot be described as covering the approved
three-universe pool until that expanded bundle verifies successfully. This larger data project
stays separate from R1's editable strategy surface.

## Evaluation panels and annualized return

Stocks, not calendar years, separate discovery from retrospective qualification. Every panel
spans the same continuous multi-year history so each candidate experiences the same sequence of
advancing, correcting, volatile, and leadership-changing markets.

The expanded constituent pool is deterministically partitioned by stable security-lineage
identity, not ticker text, into:

1. a small quick-feedback panel used to detect runtime-invalid candidates cheaply;
2. a broader discovery panel used by all three model roles during strategy search; and
3. a disjoint retrospective qualification panel that is unavailable to the roles until a
   candidate is frozen.

Each panel draws constituents from all three source universes after deduplication. A continuous
portfolio equity curve is produced for the full evaluation range; annualized results are not
formed by averaging short-window or yearly returns.

The panel manifest seals the bundle identity, continuous date range, stable security identities,
partition algorithm version, partition seed, exact panel counts or ratios, and allocation rule.
The default allocator stratifies by the security's sealed S&P/Nasdaq/Russell affiliation bitset
and applies the declared ratios within each stratum before deduplication checks. These inputs are
fixed before the first provider call. Aliases of the same security cannot cross panel boundaries.
Quick-panel performance never rejects an executable candidate: low return and zero trades are
feedback, and every runtime-valid candidate proceeds to the discovery panel.

The authoritative metric uses the production backtest formula:

```text
annualized_return_pct =
    ((ending_equity / starting_equity) ** (365 / elapsed_calendar_days) - 1) * 100
```

The unchanged baseline policy is reevaluated on every exact bundle, panel, date range, and engine
configuration; old S&P-only fold aggregates are never reused for a new panel. The candidate with
the highest valid discovery-panel annualized return is the champion.
Baseline-relative return, total return, exposure, drawdown, trade count, entry funnel, exit
attribution, and regime slices remain visible diagnostics. They explain the result but do not
form a weighted acceptance score.

The initial candidate is ready for full-replay consideration only when its frozen policy:

- produces at least 10% absolute annualized portfolio return on the retrospective qualification
  panel; and
- exceeds the unchanged baseline's annualized return on that same panel.

The optimizer stops before the full replay and reports the evidence. Future campaigns can set
the target milestone to 20% or 50% while using the same metric and evaluation path.

Qualification results never return to a model role. Every qualification panel is one-use and is
retired after evaluation regardless of pass or fail. A later 20% or 50% campaign must seal a
different disjoint constituent partition. This preserves cross-sectional separation without
claiming that historical market data is a genuinely unseen future holdout.

## Multi-agent optimizer loop

Every completed iteration retains the three distinct roles:

### Investigator

The controller selects the parent before the call from the prior critic state: continue the
active branch after `refine`, otherwise use the champion, with baseline used only when no champion
exists. The investigator receives that selected-parent policy, prior strategy hypotheses, and
aggregate portfolio feedback. It diagnoses the most important strategy weakness and writes a
concrete, focused change plan. It is asked to reason about O'Neil behavior rather than guess
isolated numeric parameters.

### Author

The author receives the investigator's plan, selected-parent identity, and complete
selected-parent source for all three policy files. It returns one atomic canonical response
mapping each of the three repository-relative paths to its complete source, including unchanged
files. The response is bound to the selected-parent identity. The controller parses all three
files, validates them as one candidate, reconstructs the complete policy bundle, and derives the
diff locally; partial multi-file application is impossible.

### Critic

The critic receives the hypothesis, validation result, annualized portfolio comparison, and
diagnostics. It explains why the change helped or hurt, then recommends one of three actions:
promote it, refine the same branch, or abandon the branch and explore a different mechanism.
That recommendation becomes structured feedback for the next investigator.

No role receives credentials, raw PIT rows, local paths, qualification-panel results, or
provider accounting internals.

## Search state and exploration

The controller keeps two simple kinds of state:

- **Champion:** the valid candidate with the highest discovery-panel annualized return.
- **Exploratory branch:** a valid non-champion candidate the critic believes is worth refining.

A candidate does not have to beat the champion to become the next exploration parent. This lets
R1 make coordinated changes that require more than one step without losing the best known
policy. The baseline, champion, and active branch summaries remain available as possible
parents. Candidate source and discovery evidence are durable local artifacts, so a closed run's
champion can seed the next bounded run instead of restarting at baseline.

The transition rules are small and deterministic. A candidate that beats the champion becomes
champion regardless of critic advice, and any active branch is cleared. A valid non-champion
replaces the active branch only when the critic says `refine`, making it the next iteration's
parent. `Abandon` clears the active branch and returns the next iteration to the champion. A
non-winning candidate labeled `promote` is discarded; promotion is metric-owned, so critic advice
cannot override the annualized-return winner.

The search objective is intentionally simple: maximize discovery-panel annualized return. There
is no secondary score that silently overrides it. When annualized returns tie at the recorded
precision, the existing champion remains selected.

Each run still declares a finite call plan so reservations, usage, and cleanup can be reconciled.
This is operational accounting, not a strategy or cost limit. The authorized campaign has no USD
ceiling, retains zero automatic provider retries, and records every attempted call. A later run
may continue from the authenticated champion without asking R1 to rediscover it.

## Recoverable candidate failures

Strategy experimentation must not repeatedly collapse the whole optimizer.

Syntax errors, invalid typed decisions, out-of-scope source, or evaluator-rejected candidate
behavior are candidate-level outcomes. The champion remains intact, the critic receives a short
validation explanation, disposable candidate state is removed, and the next planned iteration
may continue.

An HTTP-accepted but schema-invalid role response is also recoverable after its call is fully
accounted. It is never retried. An invalid investigator response closes that iteration, releases
its unused author/critic reservations, and advances to the next planned iteration with unchanged
search state. An invalid author response is summarized to the already planned critic call as a
candidate validation failure. An invalid critic response cannot undo a metric-owned champion
promotion; its branch action defaults to `abandon`. Each case is recorded as
`role_output_invalid`, distinct from provider transport failure.

Only controller-level failures stop the run: unverifiable source or data identity, corrupted
artifacts, incomplete provider accounting, an unavailable evaluator, loss of sandbox isolation,
provider transport failure, or cleanup failure. Provider calls are never automatically retried.
This boundary preserves an honest audit trail while allowing R1 to explore strategy code
aggressively.

## End-to-end flow

1. Authenticate the clean source, policy interface, local PIT bundle, panel definitions, and
   unchanged baseline results.
2. Restore the best authenticated champion and any active exploratory branch, or start from the
   baseline when no prior candidate exists.
3. Give the investigator source, prior hypotheses, annualized-return progress, and aggregate
   diagnostics for the selected parent.
4. Let the author return complete replacement source for any of the three policy files.
5. Validate and evaluate the candidate on the quick panel, then evaluate every runtime-valid
   candidate on the discovery panel; low quick-panel return remains feedback only.
6. Give the critic the actual outcome and preserve its recommendation as next-iteration
   feedback.
7. Promote a new champion whenever discovery-panel annualized return is higher; otherwise keep
   or discard it as an exploratory branch.
8. Continue through bounded runs while preserving the champion across run boundaries.
9. Freeze a milestone candidate and evaluate it once on a sealed disjoint retrospective
   constituent panel.
10. Preserve the candidate and evidence locally, clean all disposable resources, and stop before
    the full replay.

## Implementation surface

The expected implementation remains concentrated in the existing strategy and optimizer path:

- `core/strategy_policy/contracts.py`: add the typed causal market context used by entry, risk,
  and exit decisions;
- `core/backtest_engine.py`: construct that context, carry signal context through pending entries,
  and expose the existing production annualized return on optimizer evaluation results;
- `core/pit_optimizer_evaluation.py`: evaluate continuous constituent panels and return
  annualized portfolio summaries;
- `core/pit_optimization_contract.py`: describe the annualized objective, sealed panels, parent
  choice, atomic three-file full-source response, and compact feedback history;
- `core/pit_optimizer_candidate.py`: accept coordinated full-source changes across the three
  policy files without the obsolete small patch limits;
- `core/pit_optimizer_controller.py`: retain the champion and exploratory branch, route parent
  source to the roles, rank by annualized return, and recover from candidate-level failures;
- `core/pit_optimizer_holdout.py` and `pit_optimizer_holdout.py`: qualify the frozen candidate by
  absolute annualized return on the disjoint panel;
- `core/public_membership.py` and source-specific membership adapters: acquire the approved
  historical S&P 500, Nasdaq-100, and Russell 2000 membership sources;
- `export_pit_prices.py`, its price-provenance contract, and provider export helpers: export and
  authenticate the sealed `{SPY, QQQ, IWM}` reference set alongside membership prices; and
- `core/pit_data.py`, `build_pit_bundle.py`, `verify_pit_bundle.py`, and evaluator symbol
  selection: distinguish references from tradable members while preserving lineage and
  per-source affiliation in the verified bundle.

No model-authored strategy candidate is applied to tracked policy source by this architecture.

## Validation without new tests

No new test files are added. Implementation verification uses:

- compilation and linting of touched Python modules;
- existing targeted optimizer checks that already exercise changed interfaces;
- provider-free baseline parity on the regenerated authenticated S&P-membership bundle containing
  the sealed SPY, QQQ, and IWM reference histories;
- bundle verification and membership spot checks for the expanded universe;
- a short live canary proving investigator, author, and critic complete the feedback loop;
- inspection that invalid candidate code returns feedback without losing the champion;
- repeated small-panel iterations showing that the champion and exploration branch persist;
- one frozen-candidate run on the sealed disjoint retrospective constituent panel; and
- final source, accounting, process, container, and cleanup inspection.

The full replay remains off throughout this work.

## Completion and handoff

The architecture implementation is ready when a model-authored candidate can travel through the
complete investigator-author-evaluator-critic loop, feed its measured result into a later
iteration, and preserve the best annualized-return champion across bounded runs.

The initial optimizer campaign is ready for full-replay consideration when one frozen candidate
produces at least 10% annualized portfolio return and beats baseline on the sealed disjoint
retrospective constituent panel. The system then stops, reports the result, and waits for the
full replay decision. It does not merge strategy code, push artifacts, upload market data, or
start the replay automatically.
