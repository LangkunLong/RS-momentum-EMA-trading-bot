# Five-Year Public PIT Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce one reproducible 2021-2025 point-in-time CANSLIM baseline that identifies the top leaders, measures signal and execution recall, explains missed entries, and compares the unchanged engine with SPY and a mechanical leader basket.

**Architecture:** Public membership history and SEC filings are normalized into the CSV contracts already consumed by `build_pit_bundle.py`; a confined exporter converts the existing hash-pinned price cache to the price CSV for this first baseline. A new orchestration/report layer runs the existing `PITDataBundle`, `PortfolioSimulator`, leader-label helpers, and `LeaderBasketSimulator` without changing strategy behavior.

**Tech Stack:** Python 3.11+, pandas, SQLite, urllib, zipfile, csv, json, hashlib, pytest

**Spec:** `docs/superpowers/specs/2026-08-21-five-year-public-pit-baseline-design.md`

## Global Constraints

- Evaluation dates are exactly `2021-01-01` through `2025-12-31`; price warm-up begins `2020-01-01`.
- The strict trading universe is historical S&P 500 membership as of each signal date.
- Future membership is diagnostic label data only and cannot authorize a pre-addition trade.
- Do not change strategy thresholds, entry/exit rules, sizing, risk controls, or paper/live trading code.
- Institutional ownership is absent in version one; preserve the existing missing-institutional-data scoring behavior.
- Normalized inputs, the PIT bundle, and all result artifacts are immutable and SHA-256 bound.
- After normalized CSV creation, bundle construction and replay run with network access disabled.
- Honor the requested workflow: obtain the functional report first; add focused regression tests and run the broader suite only after the end-to-end path works.
- Never overwrite an existing source export, bundle, or run directory.

---

### Task 1: Pin and Normalize Five-Year S&P Membership

**Files:**
- Create: `core/public_membership.py`
- Create: `fetch_sp500_membership.py`
- Create: `exports/pit/.gitkeep`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: a pinned public page revision containing the current S&P 500 constituent table and dated changes table.
- Produces: `fetch_membership(revision_url: str, start_date: date, end_date: date) -> MembershipExport` and strict `effective_date,ticker,member` CSV rows accepted by `build_pit_bundle.py`.
- Produces: `membership_provenance.json` with `source_url`, `revision_id`, `retrieved_at_utc`, `raw_sha256`, `first_effective_date`, `last_effective_date`, `event_count`, `symbol_count`, and explicit exclusions.

- [ ] **Step 1: Define immutable result types and canonical symbol mapping**

Add these public types to `core/public_membership.py`:

```python
@dataclass(frozen=True)
class MembershipChange:
    effective_date: date
    added_ticker: str | None
    removed_ticker: str | None
    added_company: str | None
    removed_company: str | None

@dataclass(frozen=True)
class MembershipExport:
    seed_date: date
    events: tuple[MembershipEvent, ...]
    company_names: Mapping[str, str]
    source_sha256: str
    source_url: str
    revision_id: str
    exclusions: tuple[Mapping[str, str], ...]
```

Canonicalize source tickers with an explicit alias table for punctuation differences such as `BRK.B -> BRK-B` and `BF.B -> BF-B`. Reject unknown punctuation transformations rather than guessing.

- [ ] **Step 2: Fetch exactly one pinned revision**

Implement `fetch_revision(url: str, *, timeout_seconds: float = 30.0) -> bytes` using `urllib.request` with a descriptive user agent, HTTPS-only URL validation, a 10 MiB response cap, and no redirects to a different host. The CLI requires a revision URL containing an immutable revision identifier and refuses an unpinned current page URL.

- [ ] **Step 3: Reconstruct the 2021 seed and dated transitions**

Parse the constituent and changes tables with `pandas.read_html`. Starting with the revision's current constituents, reverse all changes later than `2021-01-01` to derive the seed state, then replay changes forward. Emit the complete seed as `member=1` events dated `2021-01-01`, followed by additions/removals through `2025-12-31`.

At every transition, assert that an addition is not already active and a removal is active. Record mergers, share-class changes, or unresolvable aliases in `exclusions` and stop unless the CLI receives an explicit reviewed mapping CSV with header:

```text
source_ticker,canonical_ticker,effective_start,effective_end,reason
```

- [ ] **Step 4: Write immutable membership artifacts**

`fetch_sp500_membership.py` accepts:

```text
--revision-url URL
--start-date 2021-01-01
--end-date 2025-12-31
--symbol-map-csv PATH
--output-dir PATH
```

It writes `membership.csv`, `security_names.csv`, `membership_provenance.json`, and `membership_raw.html` through temporary files followed by atomic renames. Refuse an existing output directory.

- [ ] **Step 5: Perform the first functional membership run**

Run the CLI against the selected pinned revision. Then run:

```powershell
python convert_membership_snapshots.py --help
python -c "from core.leader_evaluation import PointInTimeUniverse; from pathlib import Path; u=PointInTimeUniverse.from_csv(Path('exports/pit/membership.csv')); print(len(u.events), len(u.members_at('2021-01-01')), len(u.members_at('2025-12-31')))"
```

Expected: both membership counts are between 495 and 510; no duplicate transition error occurs.

- [ ] **Step 6: Record official spot checks**

Add `membership_spot_checks.json` with one checked change from each of 2021, 2022, 2023, 2024, and 2025. Each item contains the effective date, addition, removal, official S&P announcement URL, and `matched=true`. Do not continue if any selected event disagrees.

- [ ] **Step 7: Commit membership acquisition**

```powershell
git add core/public_membership.py fetch_sp500_membership.py exports/pit/.gitkeep .gitignore
git commit -m "feat: acquire five-year point-in-time membership"
```

Do not commit downloaded raw data or generated CSV/JSON artifacts.

### Task 2: Build the SEC Security Master and Filing-Time Fundamentals

**Files:**
- Create: `core/sec_pit_fundamentals.py`
- Create: `fetch_sec_pit_fundamentals.py`

**Interfaces:**
- Consumes: SEC `submissions.zip`, `companyfacts.zip`, `security_names.csv`, and `membership.csv`.
- Produces: `build_security_master(...) -> SecurityMasterResult` and `extract_fundamentals(...) -> FundamentalExportResult`.
- Produces: strict `fundamentals.csv` with the exact header already required by `build_pit_bundle.py`.
- Produces: `security_master.csv`, `fundamentals_provenance.json`, and `fundamentals_coverage.json`.

- [ ] **Step 1: Add SEC archive acquisition with provenance**

Download only these official URLs:

```text
https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip
https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip
```

Use a user-agent containing the project name and operator contact configured through `--sec-user-agent`. Enforce at most five requests per second, a 30-second request timeout, and a caller-supplied maximum byte limit. Write each archive once and record URL, retrieval UTC, byte length, and SHA-256.

- [ ] **Step 2: Resolve historical tickers to CIKs**

Scan submissions metadata for current and former ticker symbols and names. Produce `security_master.csv` with:

```text
ticker,cik,company_name,first_membership_date,last_membership_date,mapping_basis
```

Require one CIK per ticker/date interval. If two issuers reused a ticker, split the intervals by membership dates. Unresolved or ambiguous mappings are written to `security_master_exclusions.csv` with a closed reason; they are never assigned by fuzzy name matching.

- [ ] **Step 3: Extract accession-aware facts**

For each resolved CIK, collect these US-GAAP concepts where present:

```text
EarningsPerShareBasic
EarningsPerShareDiluted
Revenues
RevenueFromContractWithCustomerExcludingAssessedTax
NetIncomeLoss
CommonStockValue
StockholdersEquity
EntityCommonStockSharesOutstanding
```

Keep accession number, form, period end, filed date, fiscal year, and fiscal period while normalizing. Resolve alternative revenue concepts by a fixed priority list, not by taking the largest value.

- [ ] **Step 4: Derive conservative public dates**

Join company facts to submission acceptance timestamps by accession number. Set the normalized `public_date` to the first SPY trading day strictly after `acceptanceDateTime`; when acceptance time is unavailable, use the first SPY trading day after `filed` and increment `filed_date_fallback_count` in provenance.

Retain amended facts as later rows with later public dates. Never rewrite the earlier record. Convert 10-Q facts to `quarterly`, 10-K facts to `annual`, equity/share facts to `balance`, and leave institutional fields blank.

- [ ] **Step 5: Write strict fundamentals and coverage outputs**

The CLI accepts:

```text
--membership-csv PATH
--security-names-csv PATH
--spy-trading-days-csv PATH
--start-date 2020-01-01
--end-date 2025-12-31
--sec-user-agent TEXT
--output-dir PATH
```

Write the exact `build_pit_bundle.py` fundamentals header and sort by ticker, statement type, public date, and period end. Report resolved CIK percentage, symbols with quarterly/annual/balance data, accession joins, fallback dates, and exclusions.

- [ ] **Step 6: Perform the first functional SEC extraction**

Run the CLI once against the membership union. Inspect `fundamentals_coverage.json` before proceeding. Required result: at least 95% of membership-union tickers resolve to a CIK or have an explicit exclusion reason; no record has `public_date <= period_end` unless the form legitimately reports an instantaneous balance fact after the period end normalization rule.

- [ ] **Step 7: Commit SEC extraction**

```powershell
git add core/sec_pit_fundamentals.py fetch_sec_pit_fundamentals.py
git commit -m "feat: extract SEC point-in-time fundamentals"
```

### Task 3: Export the Hash-Pinned Price Cache into Plain PIT Bars

**Files:**
- Create: `export_pit_prices.py`
- Create: `tools/export_price_cache_worker.py`

**Interfaces:**
- Consumes: the existing `dataset_cache` SQLite file, its required SHA-256, `membership.csv`, and the requested 2020-2025 date range.
- Produces: exact `trade_date,ticker,open,high,low,close,volume` CSV, `spy_trading_days.csv`, and `prices_provenance.json`.

- [ ] **Step 1: Validate the cache before deserialization**

The host CLI requires `--cache-sha256`, verifies the regular non-link file and exact digest, checks that SQLite contains only the expected `dataset_cache` table/columns, and enumerates only `price`/`closes` cache keys. It copies the cache read-only into a fresh temporary worker directory.

- [ ] **Step 2: Run the converter in a disposable offline worker**

Launch `tools/export_price_cache_worker.py` with network disabled and read-only source/cache mounts. The worker is the only process allowed to unpickle cache payloads. It accepts a canonical JSON request naming the membership-union tickers plus SPY and the inclusive date range.

The worker validates each price frame has a monotonic date index and finite positive OHLC, nonnegative volume, and no duplicate ticker/date rows. It emits only CSV and a content-free completion envelope.

- [ ] **Step 3: Enforce price coverage**

Compute:

```text
member_trading_day_pairs
covered_member_trading_day_pairs
coverage_pct
symbols_with_no_prices
symbols_with_partial_prices
spy_first_date
spy_last_date
```

Require complete SPY coverage from 2020-01-01 through 2025-12-31 and at least 98% member/trading-day close coverage. Fail before bundle construction if either condition is false.

- [ ] **Step 4: Perform the first functional cache export**

Run the exporter with the known cache digest. Confirm `prices.csv` is ordinary UTF-8 CSV, the source cache remains byte-identical, and `prices_provenance.json` labels the source kind exactly `existing_hash_pinned_cache`.

- [ ] **Step 5: Commit the confined exporter**

```powershell
git add export_pit_prices.py tools/export_price_cache_worker.py
git commit -m "feat: export validated PIT price bars"
```

### Task 4: Build and Audit the Five-Year PIT Bundle

**Files:**
- Modify: `build_pit_bundle.py`
- Modify: `verify_pit_bundle.py`
- Modify: `core/pit_data.py`

**Interfaces:**
- Consumes: normalized membership, price, and fundamental CSVs plus their provenance JSON files.
- Produces: immutable `canslim_pit_2021_2025.sqlite3`, bundle manifest, and coverage audit.

- [ ] **Step 1: Bind the date contract and provenance into metadata**

Extend the builder CLI with:

```text
--evaluation-start 2021-01-01
--warmup-start 2020-01-01
--membership-provenance PATH
--prices-provenance PATH
--fundamentals-provenance PATH
```

Store only content-free values in `dataset_metadata`: evaluation/warm-up dates, source kinds, archive/revision identifiers, and SHA-256 values. Keep `schema_version=1`; the table schemas do not change.

- [ ] **Step 2: Add cross-table integrity checks**

Before creating the bundle, require:

- every membership ticker has either price rows or an explicit price exclusion;
- every fundamental ticker appears in membership;
- all rows lie on or before `2025-12-31`;
- the earliest price date is on or before `2020-01-02`;
- SPY exists in prices but not as an S&P membership event;
- every membership state on an SPY trading day has 495 through 510 members.

- [ ] **Step 3: Build without overwriting**

Run:

```powershell
python build_pit_bundle.py `
  --membership-csv exports/pit/membership.csv `
  --prices-csv exports/pit/prices.csv `
  --fundamentals-csv exports/pit/fundamentals.csv `
  --data-cutoff 2025-12-31 `
  --evaluation-start 2021-01-01 `
  --warmup-start 2020-01-01 `
  --membership-provenance exports/pit/membership_provenance.json `
  --prices-provenance exports/pit/prices_provenance.json `
  --fundamentals-provenance exports/pit/fundamentals_provenance.json `
  --output .artifacts/cache/backtest/canslim_pit_2021_2025.sqlite3 `
  --manifest-output .artifacts/cache/backtest/canslim_pit_2021_2025.manifest.json
```

- [ ] **Step 4: Verify in read-only mode**

Run `verify_pit_bundle.py` with the emitted digest. Required result: schema, digest, metadata, membership ranges, price coverage, and public-date constraints all pass without invoking a provider.

- [ ] **Step 5: Commit bundle integrity changes**

```powershell
git add build_pit_bundle.py verify_pit_bundle.py core/pit_data.py
git commit -m "feat: audit five-year PIT bundle coverage"
```

### Task 5: Produce Five-Year and Rolling Leader Labels

**Files:**
- Modify: `core/leader_evaluation.py`
- Create: `core/pit_baseline_report.py`

**Interfaces:**
- Consumes: `PITDataBundle.fetch_closes(...)`, `PointInTimeUniverse`, and the fixed date contract.
- Produces: `FiveYearLeader`, `RollingLeaderObservation`, and CSV-ready frames.

- [ ] **Step 1: Add the five-year leader model**

Define:

```python
@dataclass(frozen=True)
class FiveYearLeader:
    ticker: str
    first_price_date: date
    last_price_date: date
    total_return_pct: float
    rank: int
    member_at_start: bool
    first_membership_date: date | None
```

Implement `label_five_year_leaders(closes, membership, *, start_date, end_date, top_n=100)`. Require at least 756 valid trading days per ticker so short-lived issues do not dominate the five-year table. Do not backfill a missing start price from a date later than 20 trading days after the start.

- [ ] **Step 2: Add rolling one-year labels**

Implement `label_rolling_leaders(closes, membership, *, start_date, end_date, forward_trading_days=252, top_n=100)`. Use the first SPY trading day of each month from 2021-01 through 2024-12. Each observation records membership at evaluation and at horizon, but never changes trading eligibility.

- [ ] **Step 3: Write deterministic label frames**

Add:

```python
def five_year_leaders_frame(leaders: Sequence[FiveYearLeader]) -> pd.DataFrame
def rolling_leaders_frame(labels: Sequence[LeaderLabel]) -> pd.DataFrame
```

Use stable column order and sort by rank/ticker for the five-year table and evaluation date/rank/ticker for rolling labels.

- [ ] **Step 4: Generate the real labels from the bundle**

Run the label functions against the actual PIT bundle and inspect that the output contains 100 five-year leaders when coverage permits. Record, but do not hard-code, where NVDA, PLTR, MU, and SNDK appear or why a name is excluded.

- [ ] **Step 5: Commit leader labeling**

```powershell
git add core/leader_evaluation.py core/pit_baseline_report.py
git commit -m "feat: label five-year and rolling leaders"
```

### Task 6: Run the Unchanged CANSLIM Engine and Leader Basket

**Files:**
- Create: `pit_baseline.py`
- Modify: `core/pit_baseline_report.py`

**Interfaces:**
- Consumes: a validated bundle path/digest, `PortfolioSimulator`, `LeaderBasketSimulator`, and leader labels.
- Produces: a unique run directory containing raw engine artifacts, benchmark artifacts, `leader_recall.csv`, `summary.json`, `coverage.json`, `run_manifest.json`, and `report.md`.

- [ ] **Step 1: Define one CLI and immutable run directory**

`pit_baseline.py` accepts:

```text
--pit-bundle PATH
--bundle-sha256 HEX
--start-date 2021-01-01
--end-date 2025-12-31
--benchmark SPY
--leader-count 100
--rebalance-days 20
--output-root PATH
```

It creates `run-<UTC timestamp>-<bundle digest prefix>` and refuses any existing path. Record the Git HEAD and require a clean worktree, but do not modify Git.

- [ ] **Step 2: Run strict PIT CANSLIM without strategy overrides**

Construct `PortfolioSimulator(pit_bundle=bundle)` using existing production defaults. Pass the bundle symbol list, no maximum-position override, and the fixed dates. Do not enable `technical_only`, do not require the optional bullish-market gate, and do not alter signal cadence or thresholds.

Write the result's signal log, transaction log, weekly holdings, and equity curve directly into the run directory with deterministic filenames.

- [ ] **Step 3: Run the independent leader basket**

Construct:

```python
LeaderBasketConfig(
    leader_count=100,
    rebalance_days=20,
    lookback_days=252,
    min_history_days=60,
    initial_capital=100_000.0,
)
```

Run against the same bundle/date range and write holdings, transactions, and equity. The basket must continue to trade at the next session's open.

- [ ] **Step 4: Join leaders to signals and executions**

Implement `build_leader_recall_frame(...)` with one row per five-year leader. Include:

```text
ticker,rank,total_return_pct,first_membership_date,first_eligible_date,
first_buy_signal_date,first_entry_date,buy_signal_count,entry_count,
blocked_for_cash_count,blocked_for_capacity_count,c_fail_count,a_fail_count,
rs_fail_count,breakout_fail_count,volume_fail_count,buy_zone_fail_count,
composite_fail_count,missing_fundamentals_count
```

Derive each failed-gate count independently from signal-log fields and active thresholds. Do not force a single rejection reason when several failed.

- [ ] **Step 5: Reconcile signals to transactions**

Assert:

- every entry transaction has a prior qualifying signal for the same ticker;
- `execution_diagnostics` counts agree with transaction and signal rows;
- cash-blocked and capacity-blocked counts do not exceed otherwise-valid signals;
- signal recall and execution recall are separate percentages.

Stop and write a closed `run_failed.json` if reconciliation fails.

- [ ] **Step 6: Write summary and report**

`summary.json` contains:

```text
canslim: total_return_pct, annualized_return_pct, max_drawdown_pct,
         sharpe_ratio, win_rate_pct, closed_trades, average_cash_pct
leader_basket: same performance fields plus rebalance_count
spy: total_return_pct
leader_recall: top100_signaled, top100_executed, signal_recall_pct,
               execution_recall_pct, rolling_label_recall_pct
coverage: price_pct, cik_pct, current_quarterly_and_annual_pct
```

`report.md` includes source provenance, coverage warnings, the three performance comparisons, top-100 recall, the 20 largest missed leaders, and aggregate failed-gate counts. It clearly labels five-year labels as ex-post diagnostics.

- [ ] **Step 7: Execute the first complete offline baseline**

Run the CLI with network disabled after the normalized inputs and bundle exist. The expected terminal result is a completed run directory with every required artifact, even if strategy performance is poor. Strategy underperformance is a finding, not a controller failure.

- [ ] **Step 8: Commit the baseline runner**

```powershell
git add pit_baseline.py core/pit_baseline_report.py
git commit -m "feat: report five-year PIT CANSLIM baseline"
```

### Task 7: Add Focused Regression Coverage After the Functional Report Exists

**Files:**
- Create: `tests/test_public_membership.py`
- Create: `tests/test_sec_pit_fundamentals.py`
- Create: `tests/test_pit_baseline.py`
- Modify: `tests/test_leader_evaluation.py`
- Modify: `tests/test_pit_data.py`
- Modify: `tests/test_leader_basket.py`

**Interfaces:**
- Consumes: all acquisition, normalization, labeling, reconciliation, and reporting interfaces from Tasks 1-6.
- Produces: deterministic offline regressions; no test performs network access.

- [ ] **Step 1: Cover membership rewind and symbol aliases**

Use a two-table HTML fixture with a three-symbol current state and two historical swaps. Assert reversing changes derives the correct seed, replaying restores the current state, duplicate additions/removals fail, and an unknown punctuation alias fails closed.

- [ ] **Step 2: Cover SEC accession and public dates**

Use minimal submissions/companyfacts ZIP fixtures. Assert a 10-Q fact is unavailable on its period end, becomes available only on the first trading day after acceptance, an amendment appears only at its later public date, and a missing acceptance timestamp increments the filed-date fallback count.

- [ ] **Step 3: Cover price export validation without unsafe host pickle loading**

Use a disposable worker fixture containing one valid SPY/AAPL cache and mutations for wrong SHA, extra SQLite tables, duplicate dates, invalid OHLC, missing SPY, and partial member coverage. Assert the host never imports/deserializes the payload.

- [ ] **Step 4: Cover five-year and rolling labels**

Extend `tests/test_leader_evaluation.py` with a stable member, later addition, delisted leader, short-history issue, and ticker with a missing start price. Assert diagnostic labels never alter `members_at()` eligibility.

- [ ] **Step 5: Cover recall and execution reconciliation**

Build a toy `SimulationResult` where one leader is signaled and entered, one is signaled but cash-blocked, one fails multiple gates, and one has missing fundamentals. Assert signal recall, execution recall, failed-gate counts, and missing-data counts exactly.

- [ ] **Step 6: Cover one offline end-to-end run**

Build a tiny strict PIT bundle with 65+ trading days, two members, SPY, and as-of fundamentals. Run `pit_baseline.main(...)` into a temporary directory. Assert all required artifacts exist, hashes reconcile, and a second run targeting the same directory refuses to overwrite it.

- [ ] **Step 7: Run focused tests**

```powershell
python -m pytest tests/test_public_membership.py tests/test_sec_pit_fundamentals.py tests/test_pit_data.py tests/test_leader_evaluation.py tests/test_leader_basket.py tests/test_pit_baseline.py -q --no-cov
```

Expected: all focused tests pass without network access.

- [ ] **Step 8: Run static checks**

```powershell
python -m ruff check core/public_membership.py core/sec_pit_fundamentals.py core/pit_data.py core/leader_evaluation.py core/leader_basket.py core/pit_baseline_report.py fetch_sp500_membership.py fetch_sec_pit_fundamentals.py export_pit_prices.py pit_baseline.py
python -m compileall -q core fetch_sp500_membership.py fetch_sec_pit_fundamentals.py export_pit_prices.py pit_baseline.py
git diff --check
```

Expected: all commands exit zero.

- [ ] **Step 9: Run the broader backtest suite once**

```powershell
python -m pytest tests/test_backtest_engine.py tests/test_backtest_pnl.py tests/test_backtest_logic.py tests/test_pit_data.py tests/test_leader_evaluation.py tests/test_leader_basket.py tests/test_pit_baseline.py -q --no-cov
```

Expected: all selected engine and PIT tests pass.

- [ ] **Step 10: Commit final verification**

```powershell
git add tests/test_public_membership.py tests/test_sec_pit_fundamentals.py tests/test_pit_baseline.py tests/test_leader_evaluation.py tests/test_pit_data.py tests/test_leader_basket.py README.md
git commit -m "test: verify five-year PIT baseline"
```

### Task 8: Document and Review the First Deliverable

**Files:**
- Modify: `README.md`
- Create: `docs/pit-baseline-data-provenance.md`

**Interfaces:**
- Consumes: the completed run manifest, coverage audit, summary, and report.
- Produces: an operator runbook and a decision record for the next milestone.

- [ ] **Step 1: Document acquisition and offline replay commands**

Add the exact commands used to fetch membership/SEC inputs, export prices, build the bundle, verify its digest, and run `pit_baseline.py`. Explicitly state which steps require network access and which must run offline.

- [ ] **Step 2: Document known MVP limitations**

Record that prices came from the existing hash-pinned cache, institutional history is absent, ambiguous corporate actions were excluded, and public membership was reconstructed from a pinned table with official spot checks rather than a licensed constituent master.

- [ ] **Step 3: Review the actual baseline before optimization**

Classify findings into exactly four groups:

```text
data coverage defects
CANSLIM signal-logic gaps
execution/cash-deployment gaps
strategy performance gaps
```

Do not start parameter optimization until data coverage and execution reconciliation are satisfactory. Preserve the baseline run directory and bundle digest as the comparison point for future changes.

- [ ] **Step 4: Commit documentation**

```powershell
git add README.md docs/pit-baseline-data-provenance.md
git commit -m "docs: record five-year PIT baseline provenance"
```

## Completion criteria

The first deliverable is complete when one immutable run directory contains a verified 2021-2025 PIT bundle manifest, coverage audit, unchanged CANSLIM replay, top-100 and rolling leader labels, signal/execution recall, leader-basket and SPY comparisons, missed-leader attribution, and a human-readable report; focused and broader offline verification are green; and no strategy or live-trading behavior changed.
