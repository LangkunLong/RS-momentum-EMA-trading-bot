# Task 3B Report — Explicit Alpaca SIP Price Backfill

## Status

Complete. The operator-explicit cache-primary plus Alpaca SIP export passed the
strict coverage, SPY, identity, adjustment-basis, overlap, cache-integrity,
atomic-publication, and cleanup gates. No strategy, trading, order, position,
or account API was changed or called.

## Functional export

- Membership: 711 events, 606 union symbols.
- Membership SHA-256: `a12413a9134623b51350ad4671d4a678eeed8edf2241a9985d0d7d51957f5389`.
- Membership-map SHA-256: `6284214a6a4cefd766b3c52e84be57ac7e087cbf76d642d22abad131d61d8fa4`.
- Price-identity manifest: 30 mapped identities across 16 chains.
- Price-identity manifest SHA-256: `6a9ec69bc0fe05decea1b832cac8e26a611d706cce831d5687fa5424f9544955`.
- Alpaca SIP/SPLIT: 607/607 requested identities returned, 21 grouped chunks,
  855,330 admitted rows.
- Alpaca SIP/RAW calibration: 593 terminal anchors, 7 requests, 844,888 rows.
- Cutoff factors: 607; non-unity for `AMCR`, `BKNG`, `CRWD`, `CVNA`, `DD`,
  `HON`, `KLAC`, and `MNST`.
- Cache identity clipping: 577,527 admitted rows retained; 4,647 out-of-interval
  rows discarded.
- Cache basis: 498 already cutoff-aligned, 2 transformed from current-SPLIT
  (`AMCR`, `BKNG`), 107 with no cache overlap.
- Same-issuer warmup: 10,695 normalized predecessor rows copied across 14
  successors. FISV/FI agreed exactly on all 610 admitted overlap rows; 863 FISV
  rows were copied under FI for warmup. PSKY remained a reset with no warmup.
- Merge: 577,527 cache-primary rows plus 288,498 SIP fills = 866,025 rows.
- Active-member coverage: 631,782 / 631,965 = 99.97104270%.
- Remaining gaps: 183 across `ANSS:1`, `ATVI:2`, `BBWI:146`, `CTLT:3`,
  `CTXS:1`, `CXO:2`, `FBHS:2`, `FRC:3`, `HES:3`, `JNPR:4`, `MRO:2`,
  `MXIM:2`, `PXD:3`, `SIVB:3`, `TWTR:2`, `VAR:3`, `XLNX:1`.
- Symbols with no active prices: none.
- SPY: 1,508 / 1,508 sessions, 2020-01-02 through 2025-12-31.

The normalized-cache/cutoff-SIP overlap contains 577,527 rows and no
incompatible symbol. Maximum relative differences were open
0.446577243293, high 0.437596361394, low 0.797381298770, close
0.441296638915, and volume 1.0. The bounded incompatibility gate remains based
on repeated OHLC scale divergence; it was not relaxed.

## Published artifacts

- `exports/pit/prices.csv`: 43,650,461 bytes; SHA-256
  `dd18e38d14356df2be9aea79bc777407d40750305dcf327a2f3552815c39c376`.
- `exports/pit/spy_trading_days.csv`: 16,599 bytes; SHA-256
  `93d8ef415bd6be516fb32ebfa5986ad45cbc2077e5beaa9615943db8890be5b9`.
- `exports/pit/alpaca_sip_snapshot.csv`: 44,211,252 bytes; SHA-256
  `8d0ab3c8f72538ee0167d999b754ed5e4bb6594da2d9d169cb84e5bc705a536d`.
- `exports/pit/prices_provenance.json`: 265,269 bytes; SHA-256
  `7fca2de6d408e232bca560f1df10aeae88433f3a58e1bb64f7bef20944f95ca5`.

The source cache SHA-256 was unchanged before and after:
`1ac1a08341e103d594a14f8ba53f628925a45c3e2362864da710a22d7d2ae850`.
Publication left zero staging files and zero owned worker containers.

## Focused verification

- `python -B -m pytest tests/test_export_pit_prices.py -q`:
  20 passed, 1 skipped. The skip is the platform-specific POSIX access test on
  Windows. Pytest emitted only the environment's known cache-permission and
  websockets deprecation warnings.
- Compile, Ruff, `git diff --check`, no-trading-import inspection, artifact
  digest checks, cache re-hash, staging scan, and owned-container scan were run
  after the functional export.

Broad repository tests remain deferred to Task 7 as directed.

## Residual concern

Volume overlap differences are recorded transparently and can be large because
the frozen admitted cache remains row-authoritative while SIP is consolidated.
No volume threshold or source-precedence rule was weakened. Downstream Task 5
must also implement the already-recorded holding-transfer/fail-closed rule for
same-issuer ticker changes; PSKY must never receive an implicit transfer.
