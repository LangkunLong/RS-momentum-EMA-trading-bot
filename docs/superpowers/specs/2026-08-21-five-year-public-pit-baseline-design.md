# Five-Year Public PIT Baseline Design

## Purpose

Build one reproducible, point-in-time CANSLIM baseline for the five complete
calendar years from 2021-01-01 through 2025-12-31. The deliverable must show
whether the existing trading engine identified and executed entries in the
eventual market leaders, why it missed leaders, how much capital it deployed,
and how it compared with SPY and a mechanical leader basket.

This is a measurement project, not a strategy-optimization project. It must not
change CANSLIM thresholds, breakout logic, position sizing, exits, or the live
trading workflow.

## Date and universe contract

- Evaluation period: 2021-01-01 through 2025-12-31.
- Price warm-up: 2020-01-01 through 2020-12-31, so the first 2021 signal has
  enough history for moving averages and relative-strength calculations.
- Strict tradable universe: securities that were S&P 500 members on each
  historical evaluation date.
- Diagnostic leader universe: the union of all securities that belonged to the
  S&P 500 at any point from 2021 through 2025. Future membership may label a
  company as an eventual leader, but it must not make that company tradable
  before its effective addition date.
- Benchmark: SPY.
- Institutional ownership is omitted from version one. Existing CANSLIM score
  redistribution for unavailable institutional data remains unchanged.

## Data-source strategy

### Membership

Use a pinned, publicly accessible revision of the S&P 500 constituent and
change tables to reconstruct the complete state at 2021-01-01 and every dated
addition/removal through 2025-12-31. Preserve the revision URL, retrieval time,
raw SHA-256, effective dates, company names, and source tickers. Spot-check the
first change, last change, and at least one change in each calendar year against
the corresponding public S&P Dow Jones Indices announcement.

The normalized output is the existing strict
`effective_date,ticker,member` contract. Symbol aliases are explicit data, not
string guesses. An unresolved ticker change, acquisition, or share-class mapping
is reported and excluded rather than silently joined.

### Fundamentals

Use the SEC EDGAR `submissions.zip` and `companyfacts.zip` public bulk archives.
Build a ticker/CIK security map from submissions metadata, then extract quarterly
and annual EPS, revenue, net income, stockholders' equity, common stock, and
shares outstanding for the membership-union symbols.

Every record is keyed by its reporting period and accession. Its `public_date`
is the first trading day after the SEC acceptance timestamp. If only a filing
date is available, use the first trading day after that date and record the
fallback in the provenance report. Later amendments and restatements retain
their later availability dates; they must not overwrite the historical record
as if they were known earlier.

### Prices

For the first deliverable, export split-adjusted OHLCV from the existing local
historical cache after validating its expected SHA-256. Deserialization occurs
in an isolated, network-disabled process because the cache stores pickle
payloads. The exporter writes plain CSV; all subsequent bundle construction and
backtesting consume only the CSV or the strict read-only PIT SQLite bundle.

This price source is a deliberate MVP compromise. The report must label it as
`existing_hash_pinned_cache`, not as a public-data source. Replacing it with an
independent public or licensed price history is a later validation milestone.

## Architecture

The existing `build_pit_bundle.py`, `core.pit_data.PITDataBundle`,
`core.backtest_engine.PortfolioSimulator`, and
`core.leader_basket.LeaderBasketSimulator` remain the production boundaries.
New acquisition modules emit the existing strict CSV contracts. A new
deliverable runner composes the validated bundle, the unchanged CANSLIM replay,
leader labeling, recall analysis, and leader-basket benchmark into one immutable
run directory.

The workflow is:

1. Acquire and hash public membership and SEC bulk inputs.
2. Normalize membership, security identities, and filing-time fundamentals.
3. Export and hash the existing cached OHLCV into the strict price CSV.
4. Build and verify the PIT SQLite bundle.
5. Run the unchanged CANSLIM engine over the strict PIT universe.
6. Run the mechanical top-100 leader basket independently.
7. Label ex-post leaders and join signals, executions, and rejection gates.
8. Write one machine-readable result set and one concise Markdown report.

No acquisition function may silently fall back to a live provider. Once the
three normalized CSVs exist, the bundle build and replay run offline.

## Leader definitions

The deliverable contains two distinct leader views:

1. `five_year_leaders.csv`: top 100 total-return securities from the first
   valid close on or after 2021-01-01 to the last valid close on or before
   2025-12-31, drawn from the membership-union universe. The table records
   whether each name was already an index member at the start and its first
   effective membership date.
2. `rolling_leader_labels.csv`: on the first trading day of each month from
   2021-01 through 2024-12, rank the next 252 trading-day return and retain the
   top 100. These labels measure whether a CANSLIM signal appeared early in a
   subsequent one-year advance without contaminating trading eligibility.

Leader labels are evaluation outputs only. The backtest may trade a label only
when that ticker was a historical member on the signal date.

## Required outputs

Each run writes to a new directory and refuses to overwrite an existing run:

- `run_manifest.json`: source URLs, retrieval timestamps, SHA-256 values,
  revision identifiers, date contract, command arguments, Git HEAD, and bundle
  digest.
- `coverage.json`: membership counts, symbol/CIK mapping, price coverage,
  fundamental coverage, fallback public dates, excluded symbols, and reasons.
- `five_year_leaders.csv` and `rolling_leader_labels.csv`.
- `canslim_signals.csv`, `transactions.csv`, `weekly_holdings.csv`, and
  `equity_curve.csv` from the unchanged engine result.
- `leader_recall.csv`: one row per five-year leader with membership date, first
  eligible date, first qualifying signal, first executed entry, most frequent
  failed gates, and whether capacity or cash blocked an otherwise-valid entry.
- `leader_basket_holdings.csv`, `leader_basket_transactions.csv`, and
  `leader_basket_equity.csv`.
- `summary.json`: CANSLIM, leader-basket, and SPY performance plus recall and
  execution metrics.
- `report.md`: human-readable conclusions and a table of the most important
  missed leaders.

## Coverage and integrity gates

The first baseline is publishable only when:

- Every normalized source file and the SQLite bundle has a recorded SHA-256.
- The reconstructed S&P membership state contains 495 through 510 securities
  on every benchmark trading day.
- SPY has complete OHLCV coverage over 2020-01-01 through 2025-12-31.
- At least 98% of historical member/trading-day pairs have a valid close.
- At least 95% of membership-union tickers have a resolved CIK or an explicit
  exclusion reason.
- At least 90% of evaluated strict-PIT symbol dates have both a usable current
  quarterly record and annual record available as of that date.
- No fundamental record is visible before its recorded public date.
- The replay runs without network access after normalized inputs are created.
- The report reconciles buy signals, attempted entries, executed entries, cash
  blocks, and transaction counts to the engine artifacts.

If a coverage gate fails, the pipeline still writes `coverage.json` and stops
before presenting strategy-performance conclusions.

## Interpretation rules

- A future S&P addition confirms an ex-post leader label; it does not authorize
  a pre-addition trade in the strict PIT replay.
- A signal is not an execution. Recall and execution rates are reported
  separately.
- Missing data is not a failed CANSLIM criterion. It is reported as a data gap.
- Multiple failed gates can be attributed to one evaluated row. The report does
  not invent a single causal rejection when several conditions failed.
- The leader-basket benchmark uses only rankings observable at each rebalance
  date and trades at the next trading day's open.
- The first baseline may reveal engine or data defects, but it must not tune the
  strategy in the same run.

## Out of scope

- Strategy parameter optimization.
- Changes to paper/live order execution.
- Institutional 13F history.
- Intraday data.
- Options, dividends as cash flows, tax modeling, or transaction-cost tuning.
- Treating the existing cached prices as the final independent source of truth.
