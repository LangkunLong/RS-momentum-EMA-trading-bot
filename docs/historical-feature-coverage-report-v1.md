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

The accepted-bundle measurement and its exact local paths, hashes, counts, and four-status assessment are recorded below after the final reporter/calculator source is committed. The full row artifact is retained with the task-local evidence bundle; it is not checked into the repository.
