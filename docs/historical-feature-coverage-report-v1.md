# Historical feature coverage report v1

`core.pit_coverage` produces a versioned, point-in-time coverage inventory for one authenticated historical bundle. It measures whether a feature's inputs exist, are public by the decision session, have enough lookback, calculate successfully, and are exposed to a policy input. It also identifies the current consumer path recorded in the report. The inventory is descriptive; it does not change feature calculators, strategy gates, policy weights, or the accepted feature contract.

## Run

Run from a clean repository root with the matching bundle, manifest, price provenance, feature specification, and a full lowercase source commit SHA. The reporter verifies that the supplied SHA is the checked-out `HEAD` and rejects a dirty source tree. The report binds the financial calculator ID to this commit and to the canonical Git blob ID for `core/pit_feature_snapshot.py`, which is stable across checkout line endings.

```powershell
python -m core.pit_coverage `
  --bundle .artifacts/data/development-sp500-v2/pit_bundle.sqlite3 `
  --bundle-manifest .artifacts/data/development-sp500-v2/bundle_manifest.json `
  --prices-provenance .artifacts/data/development-sp500-v2/prices_provenance.json `
  --feature-spec docs/historical-feature-specification-v1.md `
  --output-dir .artifacts/evidence/issue-67 `
  --source-revision <full-40-character-git-sha>
```

The output directory contains `coverage_report.json` and a deterministic gzip-compressed JSON Lines file named `security_session_coverage.jsonl.gz`. The latter has one row for every active dated member on every SPY decision session. It includes ticker identity, the identity basis, source/session dates, market context and per-feature detail. The report records input hashes, output row/hash, contract/schema version, and calculator identity. The bundle is hash-checked against both its manifest and prices provenance before measurement.

## Coverage stages and denominators

Each feature has five independent stage fields:

| Field | `observed` means |
| --- | --- |
| `source_stage` | At least one matching source observation exists in the authenticated bundle, regardless of its public date. |
| `publication_stage` | At least one matching observation is available by the decision session. Bundle public dates are already normalized to first eligible sessions and are not shifted again. |
| `lookback_stage` | The feature's required observations/history are available. The cell records required and available history. |
| `calculation_stage` | The feature calculator returns a finite usable value. Missing inputs, insufficient history, and invalid comparisons retain explicit reason codes. |
| `policy_input_stage` | The calculated value is an input to the stated policy interface. `intentionally_not_exposed` denotes an explicit scope decision, not missing data. |

The declared policy consumer is stated separately as `declared_policy_consumer_status` and `consumer_paths`; these are source-path mappings, not measured feature reads for each decision. `actual_policy_consumption` is explicitly `not_measured_no_policy_replay`. The policy-input stage records whether the calculated value belongs to the stated interface, not whether a particular strategy decision consumed it. For example, annual revenue growth is a report-only calculation and is explicitly excluded from the current policy interface. Price features and V3-only fields retain their documented consumer scope.

Overall feature fractions use active member-security-sessions for that feature as the denominator; year buckets use the same denominator restricted to the calendar year. Unique ticker counts are separate from session counts. `reason_counts` gives unavailable security-session counts, and `unique_tickers_by_reason` gives distinct tickers per reason. `policy_input_reason_counts` and `unique_tickers_by_policy_input_reason` separately account for intentionally unexposed values and other policy-input gaps.

The market context reports dated-member security-session denominators, 50/200-session breadth coverage and above-average numerators, RS readiness, and SPY/QQQ/IWM benchmark readiness. Benchmark readiness is a separate benchmark-session denominator. Nonmember price symbols and reference benchmarks are reported as exclusions rather than added to the equity denominator. Missing member price histories are reported explicitly.

## Interpretation limits

The schema V2 bundle used for the development measurement has no stable security lineage ID and no industry snapshots; its denominator is the S&P 500 ticker membership present in that bundle, not a production union of domestic and foreign securities. The bundle also does not retain row-level accession, currency, accounting concept, or accounting-basis identity for every fundamental observation. Its public-date semantics follow the retained SEC provenance. Annual revenue growth is a bounded development diagnostic under the accepted #66 rule (latest and immediately preceding reported annual observations), not evidence of production source-metric parity or a baseline policy input.

An exact report is evidence only for the hashes and source revision it names. It makes no production coverage, model acceptance, future return, or live-trading claim. After changing a calculator, data source, or feature contract, regenerate the report against the new committed source and bundle.

## Issue #67 measurement

The integrated reporter at source revision `4fdadd285a87c1dd9b1f5f2c27d8fa696d5e9bfb`
measured the retained S&P-only development bundle. The calculator identity is
`pit-financial-features-v2`; its canonical `core/pit_feature_snapshot.py` Git
blob is `8e4020316d13c3de36dac8995fe4743bf888e70c`. This revision includes
the #71 corrected acceleration/freshness semantics and the #67 coverage-age
alignment. The report does not infer production-universe coverage.

| Identity | SHA-256 |
| --- | --- |
| Original-checkout `.artifacts/data/development-sp500-v2/pit_bundle.sqlite3` | `cf729b47d762ae86287bb8a87c28a1664e7475c74ef1aca736fbc240111349de` |
| Original-checkout `bundle_manifest.json` | `3c9edd0158dcb93f9de5edbfcf6f281dd8b25bcdd1ded6ad21473af5fd37be7c` |
| Original-checkout `prices_provenance.json` | `97dad8197d1fcc0d7a8cedb1a0bc13b76d7169b56a8b9c0a0e836c0a25c4f8a2` |
| Accepted `docs/historical-feature-specification-v1.md` checked-out bytes | `21fa9c9826d1ade7bebb3ccd17f8c40bfaea3136e4bab53dbd7065fa12b46c8a` |
| Local `coverage_report.json` | `8e1a7dd6bb2b545095fc13516a8c012af43a694800efadd304738dfc6db85a90` |
| Local `security_session_coverage.jsonl.gz` | `1f5031c52a265f0641905b094d74f1c4b4c38aa5d72fc7631528116f8135dff9` |

The two output files are retained locally under the integration worktree's
`.artifacts/evidence/issue-67-lead/` and are not checked in. The JSON summary
contains the complete per-field and per-year counts, fractions, reason codes,
unique ticker counts, source identities, and policy-input classifications.
The gzip JSON Lines artifact has **631,965 rows**, one per member security and
decision session. An independent decompression count matched both the reported
row count and the file's SHA-256. An independent sweep of the bundle's dated
membership events across SPY sessions also produced **1,255 sessions** and
**631,965 member security-sessions**. All 20 field summaries use that same
denominator, and each field's calculable count plus unavailable reason counts
equals 631,965.

The measured decision period is **2021-01-04 through 2025-12-31**, with **606
unique ticker symbols**. The year-specific member security-session denominators
are 127,261 (2021), 126,440 (2022), 125,752 (2023), 126,760 (2024), and
125,752 (2025). These are ticker identities from a schema V2 development
bundle. None of the 631,965 rows has an authenticated stable security-lineage
ID, and the denominator is not the three-index production union.

| Feature | Calculable security-sessions | Share of 631,965 |
| --- | ---: | ---: |
| Quarterly EPS growth | 549,538 | 86.96% |
| Quarterly revenue growth | 559,113 | 88.47% |
| Quarterly EPS acceleration | 377,575 | 59.75% |
| Quarterly revenue acceleration | 416,237 | 65.86% |
| Annual EPS growth | 553,203 | 87.54% |
| Annual revenue growth, report-only | 582,389 | 92.16% |
| Annual ROE | 528,346 | 83.60% |
| Shares outstanding | 580,450 | 91.85% |
| Fundamental age | 603,178 | 95.44% |
| Relative-strength score | 631,104 | 99.86% |
| Breadth above 50 sessions | 631,215 | 99.88% |
| Breadth above 200 sessions | 629,600 | 99.63% |
| Industry-group RS | 0 | 0.00% |
| Institutional ownership fraction | 0 | 0.00% |
| Institution-count trend | 0 | 0.00% |

The JSON summary also reports ATR20, breakout gap, average dollar volume,
distance from the 52-week high, and volume ratio, with their own stage counts
and reason codes. Market-context denominators are the dated member sessions:
50-session breadth is calculable for 631,215, 200-session breadth for 629,600,
and price RS for 631,104. The three reference benchmarks, SPY/QQQ/IWM, have
3,765 ready benchmark-sessions out of 3,765 expected. Benchmark rows are
excluded from the equity denominator. The fixed-history report fixture also
checks a missing IWM benchmark, stale and absent member price histories, short
50/200-session and RS lookbacks, and absent classifications; it does not
silently treat those values as ready.

The development bundle has no dated industry snapshots, so industry-group RS
is unavailable for all 631,965 member sessions. The two institutional fields
are also unavailable for all sessions. Annual revenue growth is a report-only
calculation and has **zero current-baseline policy-input-ready sessions** even
though it is calculable for 582,389; schema V2 lacks per-observation
currency/concept/accounting-basis identity needed for source-metric parity.
The summary lists declared consumer paths, while actual decision-by-decision
policy consumption is explicitly `not_measured_no_policy_replay`.

Focused verification on the integrated branch ran
`tests/test_pit_coverage.py` and `tests/test_historical06_financial_semantics.py`:
**20 passed**. The fixtures cover late publication, restatement, missing
comparable periods, warm-up, report-only annual revenue, full market/RS/industry
denominators, and an all-missing newer quarter that must not make an older fact
look fresh. `git diff --check` passed for the integrated correction. No
provider, model, broker, paper-runtime, or external data call was made for the
measurement.

| Status dimension | Disposition |
| --- | --- |
| Implementation | Stage-aware reporter, fixed-history checks and measured development report complete at the named source revision. |
| Required inputs | Artificial histories and the retained authenticated development bundle were available. A production three-index bundle is not required to implement or measure this reporter. |
| Acceptance evidence | The four published criterion checks have fixed and measured evidence above. Actual per-decision consumption was not replayed; production coverage and issue closure are not claimed. |
| Dependencies | Accepted #66 was present at the starting revision. #71's corrected calculator identity is bound in this result. |
