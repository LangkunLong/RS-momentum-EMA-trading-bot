# Task 5 report — five-year and rolling leader labels

Date: 2026-08-22

## Outcome

Task 5's leader-labeling implementation is functionally exercised against the real normalized membership, price, and SPY-session artifacts. These labels are ex-post evaluation outputs only; no trading eligibility, strategy, or execution behavior changed.

## Implemented contract

- `FiveYearLeader` records the first/last usable price dates, total return, deterministic rank, membership at the fixed start, and first membership date.
- Five-year ranking is confined to the 606-symbol membership union and consumes the hash-bound Task 3B price-identity contract. Reviewed same-issuer aliases form one economic chain with aggregated closes and PIT membership; successor resets remain separate. A chain needs at least 756 positive finite closes on supplied SPY sessions, and its first usable close must be within the first 21 sessions (offset 0 through 20) from the start.
- `RollingLeaderObservation` records evaluation/horizon membership independently of rank eligibility. It never injects ex-post membership into the tradable universe.
- Rolling observations use the first supplied SPY session of each month from January 2021 through December 2024 and the close exactly 252 SPY sessions later. Returns rank each approved same-issuer chain once with deterministic return-descending/ticker-ascending ties; the reporting ticker is the PIT-active alias at evaluation.
- CSV-ready frames have fixed columns and stable five-year `rank,ticker` or rolling `evaluation_date,rank,ticker` order.

## Real functional evidence

The functional run loaded `exports/pit/membership.csv`, pivoted `exports/pit/prices.csv`, and proved the price frame's SPY dates exactly equal `exports/pit/spy_trading_days.csv` before labeling.

- Five-year output: 100 leaders.
- Rolling output: 4,800 observations across 48 monthly evaluation dates (100 each).
- Five-year CSV-frame SHA-256: `534dc047dff8379bda774d1e718b713c326be3553d48442ef80ad835b7e38f96`.
- Rolling CSV-frame SHA-256: `c4463e9d3a5c37e4a7d7db6ebb514b36f0c1fbff0fef3c9f394e1a99bec674df`.
- NVDA: rank 2; usable prices 2021-01-04 through 2025-12-31.
- PLTR: rank 8; usable prices 2021-01-04 through 2025-12-31.
- MU: rank 31; usable prices 2021-01-04 through 2025-12-31.
- SNDK: excluded by the minimum-history gate; 222 valid sessions from 2025-02-13 through 2025-12-31.
- Same-issuer audit: zero duplicate `(evaluation_date, price_chain)` rows after aggregation. COR rank 38 and META rank 98 correctly inherit start membership/first membership date `2021-01-01` from ABC and FB respectively.

These positions/exclusions were computed from the source artifacts and are not hard-coded.

## Focused verification

- `python -B -m py_compile core/leader_evaluation.py core/pit_baseline_report.py`: PASS.
- `python -m ruff check core/leader_evaluation.py core/pit_baseline_report.py`: PASS.
- `git diff --check`: PASS.

The initial independent review found same-issuer aliases could be double-ranked because Task 3B intentionally replicated predecessor warmup. The implementation now requires the hash-bound identity contract, aggregates aliases, and keeps successor resets separate. Fix-round re-review: PASS with no findings. Stable SHA-256 values were `500F9F1A...` for `leader_evaluation.py` and `EE8C73E8...` for `pit_baseline_report.py`.
