# Five-year public PIT baseline: provenance and decision record

## Decision

The first public 2021--2025 PIT baseline is complete and preserved as a
**logic-verification baseline with a declared data limitation**. It is not a
fully coverage-gated strategy-performance verdict, because the run explicitly
used `--allow-incomplete-fundamentals` after the evaluated strict-PIT
quarterly-plus-annual coverage gate measured 80.20768935%, below its 90% target.

No strategy parameters were changed or applied for this run. Do not begin
parameter optimization until the data-coverage finding and execution
reconciliation remain satisfactory on the preserved comparison point.

## Preserved comparison point

- Bundle SHA-256:
  `8ca8242dd67db30d456a2b1861f7e7399f8ca418079738ab150d4e44865763c5`
- Bundle:
  `.artifacts/pit-baseline/pit_baseline.sqlite3`
- Completed run directory:
  `.artifacts/pit-baseline/run-20260823e/run-20260823T071322Z-8ca8242dd67d`
- Ground-truth run records: `run_manifest.json`, `coverage.json`,
  `summary.json`, and `report.md` in that directory.

Preserve the bundle, its manifest, normalized input exports, and the completed
run directory unchanged. Any later data repair, reconciliation repair, or
strategy experiment must use this digest and run as its before-comparison.

## Recorded coverage and provenance

The run is complete. Its price coverage is 99.97104270% (631,782 of 631,965
active-member/trading-day pairs); SPY is complete from 2020-01-02 through
2025-12-31. Membership contains 503--506 securities at each evaluation
session. CIK resolution-or-explicitly-closed exclusion is 100%; 39 exclusions
are closed rather than silently assigned. The strict-PIT quarterly-plus-annual
fundamental availability is 80.20768935% (96,007 of 119,698 evaluated
symbol/date rows), versus the 90% target.

Membership was reconstructed from the immutable Wikipedia revision
`1347775889` with official S&P spot checks, not a licensed constituent master.
SEC fundamentals came from the official EDGAR `submissions.zip` and
`companyfacts.zip` archives. Prices are cache-primary from the existing
hash-pinned cache, supplemented only by the explicitly requested Alpaca SIP
snapshot where admitted by the reviewed identity contract. The published price
source kind is `existing_hash_pinned_cache_plus_alpaca_sip_snapshot`.

## Reproducible operator workflow

All commands below are PowerShell commands from the repository root. Each
publication target must be new: the acquisition, bundle, and runner commands
refuse overwrites.

### Network-required acquisition and recorded source procedure

The membership fetch requires network access to the pinned public revision.
The completed run did **not** acquire SEC archives through the CLI: its manifest
records both `submissions.zip` and `companyfacts.zip` with acquisition
`manual_browser_download`. An operator downloaded the two official URLs in a
browser, placed the resulting regular files at
`.artifacts/sec-pit/submissions.zip` and `.artifacts/sec-pit/companyfacts.zip`,
and the normalized output provenance bound their exact bytes. Before use,
verify both byte length and SHA-256; a matching filename or URL alone is not
sufficient:

- `https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip`
- `https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip`

```powershell
(Get-Item .artifacts/sec-pit/submissions.zip).Length
# 1559612838
Get-FileHash .artifacts/sec-pit/submissions.zip -Algorithm SHA256
# 928d67221c6e6183bc343e7234c1391448c15cd1dd644d36b425db2f99ba4350

(Get-Item .artifacts/sec-pit/companyfacts.zip).Length
# 1407131132
Get-FileHash .artifacts/sec-pit/companyfacts.zip -Algorithm SHA256
# d7b4b3c5f2fe014a203bdaef2197d2cba5683f434e965fc9bced1023a43c82ca
```

The price command is offline with cache-only inputs, but the explicit
`--alpaca-sip-backfill` used for this baseline requires licensed Alpaca market
data access and credentials. It does not call any trading or account endpoint.

The following commands describe a future acquisition/normalization workflow.
They are not asserted to be the literal historical shell transcript: the SEC
contact string, local cache location, and credential file are sensitive or
operator-local substitutions. The immutable source revision, source hashes,
and sandbox-image digest are the reproducibility anchors.

```powershell
python fetch_sp500_membership.py `
  --revision-url 'https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid=1347775889' `
  --start-date 2021-01-01 --end-date 2025-12-31 `
  --symbol-map-csv config/pit_membership_symbol_map.csv `
  --output-dir exports/pit

python fetch_sec_pit_fundamentals.py `
  --membership-csv exports/pit/membership.csv `
  --security-names-csv exports/pit/security_names.csv `
  --spy-trading-days-csv exports/pit/spy_trading_days.csv `
  --start-date 2020-01-01 --end-date 2025-12-31 `
  --sec-user-agent '<approved-project-and-operator-contact>' `
  --max-archive-bytes 30000000000 `
  --identity-manifest-csv config/pit_price_identity_map.csv `
  --output-dir .artifacts/sec-pit

python export_pit_prices.py `
  --cache '<existing-hash-pinned-dataset-cache.sqlite3>' `
  --cache-sha256 1ac1a08341e103d594a14f8ba53f628925a45c3e2362864da710a22d7d2ae850 `
  --membership-csv exports/pit/membership.csv `
  --symbol-history-map config/pit_membership_symbol_map.csv `
  --symbol-history-map-sha256 6284214a6a4cefd766b3c52e84be57ac7e087cbf76d642d22abad131d61d8fa4 `
  --price-identity-map config/pit_price_identity_map.csv `
  --price-identity-map-sha256 6a9ec69bc0fe05decea1b832cac8e26a611d706cce831d5687fa5424f9544955 `
  --start-date 2020-01-01 --end-date 2025-12-31 `
  --sandbox-image 'localhost/rs-agent-loop@sha256:7ecfb4ebb3b327940bef347e4c82e82fb4a0e8b40fc63b92b2536fe8c83acf1c' `
  --alpaca-sip-backfill --alpaca-env-file .env `
  --output-dir exports/pit
```

The precise committed acquisition inputs are bound by their output provenance:
membership revision `1347775889`; membership CSV SHA-256
`a12413a9134623b51350ad4671d4a678eeed8edf2241a9985d0d7d51957f5389`;
EDGAR `submissions.zip` SHA-256
`928d67221c6e6183bc343e7234c1391448c15cd1dd644d36b425db2f99ba4350`; and
`companyfacts.zip` SHA-256
`d7b4b3c5f2fe014a203bdaef2197d2cba5683f434e965fc9bced1023a43c82ca`.

### Offline-only build, verification, and replay

After normalized inputs exist, disconnect provider access. These commands are
offline-only and must not be used to refresh source data.

```powershell
python build_pit_bundle.py `
  --membership-csv exports/pit/membership.csv `
  --prices-csv exports/pit/prices.csv `
  --fundamentals-csv .artifacts/sec-pit/fundamentals.csv `
  --data-cutoff 2025-12-31 `
  --evaluation-start 2021-01-01 --warmup-start 2020-01-01 `
  --membership-provenance exports/pit/membership_provenance.json `
  --prices-provenance exports/pit/prices_provenance.json `
  --fundamentals-provenance .artifacts/sec-pit/fundamentals_provenance.json `
  --output .artifacts/pit-baseline/pit_baseline.sqlite3 `
  --manifest-output .artifacts/pit-baseline/pit_baseline.manifest.json

python verify_pit_bundle.py `
  --bundle .artifacts/pit-baseline/pit_baseline.sqlite3 `
  --sha256 8ca8242dd67db30d456a2b1861f7e7399f8ca418079738ab150d4e44865763c5 `
  --manifest .artifacts/pit-baseline/pit_baseline.manifest.json `
  --membership-csv exports/pit/membership.csv `
  --prices-csv exports/pit/prices.csv `
  --fundamentals-csv .artifacts/sec-pit/fundamentals.csv `
  --membership-provenance exports/pit/membership_provenance.json `
  --prices-provenance exports/pit/prices_provenance.json `
  --fundamentals-provenance .artifacts/sec-pit/fundamentals_provenance.json

python pit_baseline.py `
  --pit-bundle .artifacts/pit-baseline/pit_baseline.sqlite3 `
  --bundle-sha256 8ca8242dd67db30d456a2b1861f7e7399f8ca418079738ab150d4e44865763c5 `
  --membership-provenance exports/pit/membership_provenance.json `
  --prices-provenance exports/pit/prices_provenance.json `
  --fundamentals-provenance .artifacts/sec-pit/fundamentals_provenance.json `
  --fundamentals-coverage .artifacts/sec-pit/fundamentals_coverage.json `
  --security-master .artifacts/sec-pit/security_master.csv `
  --security-master-exclusions .artifacts/sec-pit/security_master_exclusions.csv `
  --start-date 2021-01-01 --end-date 2025-12-31 `
  --benchmark SPY --leader-count 100 --rebalance-days 20 `
  --checkpoint-every-days 20 `
  --resume-checkpoint .artifacts/pit-baseline/checkpoint-migrations/portfolio-382c119/portfolio_checkpoint.json `
  --output-root .artifacts/pit-baseline/run-20260823e `
  --allow-incomplete-fundamentals
```

The final command above is the recorded completed-run invocation from the
manifest, including its resume checkpoint. It is evidence, not a command to
rerun in place: the preserved output root already exists and publication
correctly refuses an overwrite.

For a fresh offline replay, retain the same hash-bound input arguments but omit
`--resume-checkpoint` unless resuming that new replay's own checkpoint, and use
a new output root, for example:

```powershell
python pit_baseline.py <same-hash-bound-input-arguments> `
  --output-root .artifacts/pit-baseline/replays/run-<new-unique-id> `
  --allow-incomplete-fundamentals
```

## Known MVP limitations

- The price history is anchored in the existing hash-pinned cache; the licensed
  SIP snapshot is an explicit, reviewed backfill rather than an independent
  institutional-grade historical source.
- Institutional ownership history is absent: institutional fields in the public
  SEC fundamental export are blank.
- Ambiguous corporate actions and issuer transitions are excluded or represented
  only by reviewed identity intervals; no ambiguous continuity is inferred.
- Public membership is reconstructed from a pinned public table with official
  spot checks. It is not a licensed historical constituent master.

## Findings before any optimization

### data coverage defects

- The evaluated strict-PIT quarterly-plus-annual coverage is 80.20768935%,
  failing the 90% target. This is the sole non-blocking failed gate accepted by
  `--allow-incomplete-fundamentals`.
- Price coverage passes at 99.97104270%, but 183 active-member/day gaps remain
  across 17 partially covered symbols. Membership and SPY coverage pass.

### CANSLIM signal-logic gaps

- Top-100 five-year leader signal/execution recall is 40%/40% (40 signaled and
  40 executed leaders), while rolling-leader recall is 0.25%. These are
  diagnostic gaps to investigate only after the data gate is repaired.

### execution/cash-deployment gaps

- There are no reconciliation blocks: zero cash blocks, zero capacity blocks,
  174 entry attempts, and 174 entries executed.
- Average cash is 76.33736906%. It is an observed deployment outcome, not
  evidence of a cash or capacity rejection in this replay.

### strategy performance gaps

- Unchanged CANSLIM: 1.37670379% total return, 174 closed trades, and
  42.52873563% win rate.
- Independent 100-leader basket: 65.05507789% total return; SPY: 84.79009134%.
  These comparisons are recorded for diagnosis, not for tuning or promotion.

The next milestone is data coverage repair and a repeat of the same immutable
workflow, followed by execution reconciliation review. No parameter search,
threshold adjustment, or other optimization is authorized by this record.
