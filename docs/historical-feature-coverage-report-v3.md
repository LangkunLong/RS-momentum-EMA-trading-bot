# Historical feature coverage: corrected development measurement (issue #67)

This is the measured `historical_feature_coverage_v3` report from clean source
`af77c86ce00de0b8b89fd94900139360a7adb0e3`. It uses the integrated #71
calculator `pit-financial-features-v3` (Git blob
`71e562ffc00dc4e617f1cd625e3f13ea5fe3f81a`). The earlier
[v1 report](historical-feature-coverage-report-v1.md) and the complete
`d764c87` local report retain their original definitions. The latter predates
the terminal-Q4 full-window correction and is superseded for #67 acceptance;
it was not edited or renamed.

## Reproduce and locate the evidence

Run `python -m core.pit_coverage` from a clean checkout of the source revision
above with the same `--bundle`, `--bundle-manifest`, `--prices-provenance`, and
`--feature-spec` inputs described in the v1 report. Use
`--output-dir .artifacts/evidence/issue-67-v5-af77c86` and
`--source-revision af77c86ce00de0b8b89fd94900139360a7adb0e3`. The
reporter checks the clean source revision and input hashes before measurement.

| Input or output | SHA-256 |
| --- | --- |
| Development S&P-only V2 SQLite bundle | `cf729b47d762ae86287bb8a87c28a1664e7475c74ef1aca736fbc240111349de` |
| Bundle manifest | `3c9edd0158dcb93f9de5edbfcf6f281dd8b25bcdd1ded6ad21473af5fd37be7c` |
| Price provenance | `97dad8197d1fcc0d7a8cedb1a0bc13b76d7169b56a8b9c0a0e836c0a25c4f8a2` |
| Checked-out accepted #66 feature specification | `21fa9c9826d1ade7bebb3ccd17f8c40bfaea3136e4bab53dbd7065fa12b46c8a2` |
| Local `coverage_report.json` | `2a5a5394ae2d29c22d84a4cb63eea16339efd0bb40c03a790df49ad84788064d` |
| Local `security_session_coverage.jsonl.gz` | `1a329025fdff9db156f6d2049ce3a6318712049cb12504bd937ae4e5dcce7019` |

The two output files are retained locally in
`.artifacts/evidence/issue-67-v5-af77c86/`. The run exited zero. The local
runtime was CPython 3.13.14 at
`C:/Users/llong/AppData/Local/Microsoft/WindowsApps/PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0/python.exe`,
pandas 3.0.1, NumPy 2.4.2, SQLite 3.50.4, on Windows 11
`10.0.26200-SP0`.

## Denominator and staged coverage

The report measures **1,255 SPY decision sessions** from 2021-01-04 through
2025-12-31 and **631,965 dated member security-sessions** from **606 ticker
symbols**. Year denominators are 127,261, 126,440, 125,752, 126,760, and
125,752 for 2021–2025. Each of the 20 fields uses the 631,965 member-session
denominator; benchmark sessions are separate. The JSON report retains all
source-observed, publication-eligible, lookback-ready, calculable, and
policy-input-ready stages, reasons, unique ticker counts, and year slices.

| Feature | Member-session denominator | Calculable | Declared policy-input ready |
| --- | ---: | ---: | ---: |
| `annual_eps_growth` | 631,965 | 553,203 | 553,203 |
| `annual_revenue_growth` | 631,965 | 582,389 | 0 |
| `annual_roe` | 631,965 | 528,346 | 528,346 |
| `atr_20_fraction` | 631,965 | 631,565 | 631,565 |
| `average_dollar_volume_50` | 631,965 | 631,204 | 631,204 |
| `breadth_above_200` | 631,965 | 629,600 | 629,600 |
| `breadth_above_50` | 631,965 | 631,215 | 631,215 |
| `breakout_gap_fraction` | 631,965 | 631,772 | 631,772 |
| `distance_from_52_week_high_fraction` | 631,965 | 628,901 | 628,901 |
| `fundamental_age_days` | 631,965 | 603,178 | 603,178 |
| `industry_group_rs` | 631,965 | 0 | 0 |
| `institution_count_trend` | 631,965 | 0 | 0 |
| `institutional_ownership_fraction` | 631,965 | 0 | 0 |
| `quarterly_eps_growth` | 631,965 | 549,538 | 549,538 |
| `quarterly_eps_growth_acceleration` | 631,965 | 380,381 | 380,381 |
| `quarterly_revenue_growth` | 631,965 | 559,113 | 559,113 |
| `quarterly_revenue_growth_acceleration` | 631,965 | 419,418 | 419,418 |
| `relative_strength_score` | 631,965 | 631,104 | 631,104 |
| `shares_outstanding` | 631,965 | 580,450 | 580,450 |
| `volume_ratio_50` | 631,965 | 631,204 | 631,204 |

Annual revenue growth is deliberately report-only under the accepted #66
contract: its 582,389 calculable cells are **not** current policy inputs.
`policy_input_stage` describes interface availability. `consumer_paths` are
static source-code mappings. Every row says
`actual_policy_consumption=not_measured_no_policy_replay`; no decision-level
read, strategy effect, or policy replay is claimed.

## Full-window and market checks

Quarterly EPS has 2,527,860 required four-slot growth cells. The corrected
report has 1,634,721 matched and 893,139 missing slots; the latter include
604,048 `missing_fiscal_quarter_period` slots and 253 untrusted short-cadence
placeholders. Its full four-slot window is ready for **0/631,965** sessions.
That is an observed limitation of this development input, not a claim that
the latest quarterly EPS calculation is unavailable everywhere: its latest
value is calculable for 549,538 sessions. A read-only source sweep found
2,192 long gaps among 7,982 distinct 2021–2025 quarterly EPS period rows;
the report slot totals repeat source gaps across decision sessions.

For ticker A on 2021-01-04, the as-of-visible annual FY ended 2020-10-31
anchors an unavailable terminal Q4 slot. The next three represented slots are
2020-07-31, 2020-04-30, and 2020-01-31, all matched to the prior year. The
window is therefore three matched and one missing. The reporter never uses
annual revenue or EPS as a fabricated quarterly EPS value. The corrected
full-window totals differ from the retained `d764c87` report by 920 fewer
matched and 920 more missing slots; all non-quarterly field summaries and
market/benchmark summaries are unchanged.

The fixed-history cases cover late publication, restatement, missing prior
periods, warm-up, 50/200-session breadth, relative strength, absent dated
industry classification, missing member prices, and missing benchmarks. The
measured market context has 631,215 valid 50-session breadth cells, 629,600
valid 200-session breadth cells, and 631,104 price-RS-ready cells. SPY, QQQ,
and IWM are ready for 3,765/3,765 benchmark sessions and excluded from the
member denominator. The V2 bundle has no dated industry snapshots: all
631,965 member sessions lack a dated industry assignment. It also lacks
authenticated stable lineage IDs, so none of these counts is a three-index
production-union denominator.

## Acceptance boundary

The report supports the staged reporter and this named development input. It
does not measure production-universe coverage or actual policy consumption.
Independent row-stream reconciliation against the retained gzip file is in
progress; the four-issue packet records the final review disposition.
