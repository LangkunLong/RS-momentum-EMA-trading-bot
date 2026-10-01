# Historical membership source receipt v1

Assessment date: 2026-09-28

Starting main revision: `2c01e76a878a338ab2b743c38c4f1310aab3f75b` (accepted foundation PR #110)

Accepted membership contract: [historical feature specification v1](historical-feature-specification-v1.md), SHA-256 `21fa9c9826d1ade7ebb3ccd17f8c40bfaea3136e4ebab53dbd7065fa12b46c8a`

This receipt records a source inventory and preparation result for the contract window 2021-01-01 through 2025-12-31. It is **not** a complete production-universe membership artifact. Source files listed below remain in the original checkout's ignored `.artifacts` tree; this receipt binds the retained bytes without copying or relabeling incomplete history.

## Four separate statuses

| Status | Finding |
| --- | --- |
| **Implementation** | The v3 membership normalizer and lineage types exist. This change fixes a rename-boundary rejection and adds deterministic fixtures for index overlap, same-lineage ticker changes, and short-lived membership. |
| **Required inputs** | S&P 500 event rows are retained. No admissible Nasdaq-100 seed/event pair or Russell 2000 dated-membership source set is retained. The local price provenance has identity rows for all S&P event tickers but no explicit v3 identity-transition list. |
| **Acceptance evidence** | The four focused normalizer tests pass. Production-union reconciliation, complete dated coverage, and the full acceptance criteria remain unverified because the required index inputs are incomplete. |
| **Dependencies** | Start prerequisite #66 is satisfied by the accepted contract above. Work can start and the available S&P evidence can be prepared independently; full production acceptance still depends on the missing source inputs and explicit v3 identity-transition integration. |

## S&P 500

The retained source is a pinned public revision of the Wikipedia S&P 500 change table, converted to an event CSV and spot-checked against five dated S&P Dow Jones Indices announcements. The historical events are not today's membership list or fund holdings. The five spot checks do not independently verify every event against a primary announcement.

| Retained input (under original `.artifacts`) | SHA-256 | Bound facts |
| --- | --- | --- |
| `data/acquisition/acquisition/raw/local-sp500/membership.csv` | `a12413a9134623b51350ad4671d4a678eeed8edf2241a9985d0d7d51957f5389` | 711 events, 606 distinct tickers, 2021-01-01 through 2025-12-22 |
| `data/acquisition/acquisition/raw/local-sp500/membership_raw.html` | `8940a97243ad30664b2d0e606e832fec13e6bf38c250b1c999cff8a94093e14d` | Exact retained source bytes; revision 1347775889 |
| `data/acquisition/acquisition/raw/local-sp500/membership_provenance.json` | `16fdda33dc102c3e8bfbcd2f683812666a892a0fb28bdc5347ba90c1e9206518` | Source URL, revision, retrieval time, CSV/name/map digests |
| `data/acquisition/acquisition/raw/local-sp500/pit_membership_symbol_map.csv` | `6284214a6a4cefd766b3c52e84be57ac7e087cbf76d642d22abad131d61d8fa4` | Dated ticker mappings with issuer evidence links |
| `data/acquisition/acquisition/raw/local-sp500/membership_spot_checks.json` | `d2c2d8d70bd3053a497bf4d4f5654f68e06a02d74a85c40a1dfea7b4cabf8b55` | Five matched announcements spanning 2021, 2022, 2023, 2024, and 2025 |
| `data/acquisition/acquisition/raw/local-sp500/security_names.csv` | `12cf4c46a9cbcc349cf1cd79d65354a2d960c68282c8bb68203d2a5e35b2a1df` | Company-name support for the retained event export |

The source provenance declares `https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid=1347775889`, retrieved 2026-08-22. The retained raw-page hash is the `raw_sha256` in that provenance. The dated official spot checks are retained at the five URLs in `membership_spot_checks.json`.

Price identity evidence is separate from index membership:

| Retained identity input | SHA-256 | Bound facts |
| --- | --- | --- |
| `data/acquisition/acquisition/raw/local-cache/prices/prices_provenance.json` | `7fca2de6d408e232bca560f1df10aeae88433f3a58e1bb64f7bef20944f95ca5` | Identity-request contract digest `273727c248f57b7376b6cf269312325cdd059287e1f4ed16ab5ce63617ceabc7`; 607 ticker identities, including all 606 S&P event tickers |
| `data/acquisition/acquisition/raw/local-cache/prices/pit_price_identity_map.csv` | `6a9ec69bc0fe05decea1b832cac8e26a611d706cce831d5687fa5424f9544955` | 30 explicit identity-map rows across 16 lineage chains |

These bytes do **not** yet satisfy the normalizer's v3 identity-transition input: the retained prices provenance has no `price_identity_transitions` field. Ticker-key coverage is therefore not reported as complete lineage coverage. The retained price bundle also has an S&P-only scope.

## Nasdaq-100

The retained evidence contains public Nasdaq notices for specific constituent changes, plus secondary historical-list material. It does not contain an admissible complete event and seed pair for the full contract window.

| Retained evidence | SHA-256 | Disposition |
| --- | --- | --- |
| `data/acquisition/acquisition/raw/nasdaq-public/acquisition-manifest.json` | `f41e2455e037c1dd6f1f26ca868fa30c7a8a8b84f1219960158d7178db0815b7` | Records that no membership pair was created; validation was not invoked because no source pair was admitted |
| `data/acquisition/acquisition/raw/nasdaq-public/public-wikipedia-anchor-r997991401.html` | `4e478f7895cd4f6b5a1f07e876012755e35979e29ef731849ca51add937d8193` | A secondary-source anchor candidate stated as-of 2020-12-21; not admitted |
| `data/acquisition/acquisition/raw/nasdaq-public/public-wikipedia-history-r1371960136.html` | `4a67fdef4ed6bdd3f39a17f6a2e8bdf3936de89f01cc69ed57ecfee0d8f256dc` | Secondary historical table; its extraction is explicitly not admitted |
| `data/acquisition/acquisition/raw/nasdaq-public/history-table-extraction-not-admitted.json` | `07cd0820cb5317d56d1dcfd1cd3681f8b4dc718815f331c48bfd33ae3fda4620` | 61 parsed rows, 49 dated rows in 2021–2025, and 89 claimed in-window ticker events; not a verified membership timeline |
| `data/acquisition/acquisition/raw/nasdaq-public/correction-evidence-ledger.json` | `c8abbf170c54f1197607001e0dfa252093c0aae5cad7e37237dcdf953ef57d2c` | Four specific historical claims corrected using retained contemporaneous notices; does not establish complete coverage |

Useful public primary-source leads include Nasdaq's annual-change notices and its dated off-cycle announcements. Retained examples include the 2022 FB/META corporate-action notice (SHA-256 `4dc3542bfa694f8ee0d67d0e08f4125e5b83c9d2755ec2b45478f162a99e5c96`), the 2023 amended annual notice (`546b47527da186b3e7b1e65dea27cb59e1ff339e7b9c1df9b6cba1d2e0430594`), and the 2023 GE HealthCare/Fiserv notice (`ec580658c6beccb32061b38118130be654b64c4c41b14ef7acfdcb755121499b`). These support bounded event checks only.

The retained Hong Kong iShares Nasdaq-100 interim report is a fund-holdings document and is excluded as an index-membership source.

## Russell 2000

No Russell 2000 membership event file, dated constituent snapshot, or source/provenance pair was found under the retained acquisition root. FTSE Russell's public [reconstitution page](https://www.lseg.com/en/ftse-russell/russell-reconstitution) is a source lead for its published reconstitution process, not evidence of this historical constituent timeline. Current constituents or IWM holdings are not substitutes. Russell coverage is unavailable in this preparation.

## Source use and limits

- The S&P source is a pinned Wikipedia revision. Wikimedia states that most Wikipedia text is available under CC BY-SA 4.0 and requires attribution, indication of changes, and compatible share-alike terms for redistributed derivatives ([official reuse guidance](https://foundation.wikimedia.org/wiki/Legal%3AWikimedia_Developer_App_Guidelines)). This change records hashes and source identity; it does not copy the event rows into a newly distributed repository dataset.
- Nasdaq notices cited above are public first-party releases already retained with acquisition receipts. This work made no provider API call and did not acquire a complete historical series or verify broader redistribution terms.
- The FTSE Russell page is a public process reference only. No Russell data was acquired, purchased, licensed, or inferred.
- The four per-source paths and digests above bind existing bytes in `C:/Projects/trading_bot/RS-momentum-EMA-trading-bot/.artifacts`. They are not fresh downloads or evidence that every listed historical event was independently reviewed in this task.

## Focused validation

`tests/test_normalize_pit_universe_membership.py` covers:

1. One lineage affiliated with two indexes, preserving both index tags.
2. A same-issuer ticker change on the authenticated transition date while the predecessor's price identity ends the prior day.
3. A short-lived membership addition and removal, plus rejection of unreviewed swaps and out-of-range events.

These fixtures validate normalization behavior. They do not fill the missing Nasdaq/Russell inputs or establish production-universe acceptance.
