# Issue #69 source-reconciliation follow-up

Date: 2026-10-02
Branch: `codex/issue-69-bounded-source-assessment`
Integrated base: merge commit `fe27678fb8472c8d031bcca25f24a611b91cfbdb`

This follow-up preserves the bounded assessment and its receipt. It adds source-generation tracing and a fail-closed exporter correction. It does not claim production price delivery or close #69.

## FI/FISV mismatch and source path

The retained published file is `local-cache/prices/prices.csv`, SHA-256 `dd18e38d14356df2be9aea79bc777407d40750305dcf327a2f3552815c39c376`. Its companion provenance reports `price_identity_warmup_validation.successor_audits.FI.exact_overlap_row_count = 610` for 2023-06-07 through 2025-11-10. Within that declared admission interval, the retained published rows share all 610 sessions but have **zero exact OHLCV matches**. Across the full raw-symbol overlap, there are 1,473 shared dates; 1,145 rows differ, including all 610 in-window dates and 535 of the 863 pre-admission dates.

The producer in `export_pit_prices.py` explains the scope difference:

1. `_build_price_identity_warmup` calculates the declared successor audit on the staged, cutoff-adjusted SIP file.
2. `_normalize_cache_to_cutoff_basis` separately prepares cached rows.
3. `_merge_price_sources` keeps the cache row whenever a cache/SIP `(date, ticker)` pair overlaps.
4. Before this follow-up, no identity-continuity check ran on the merged publication file.

So the retained count describes an intermediate provider snapshot, not the final merged prices. The provenance retains the intermediate snapshot's declared hash (`8d0ab3c8f72538ee0167d999b754ed5e4bb6594da2d9d169cb84e5bc705a536d`), but that snapshot is absent from the pinned assessment inputs; its 610-row claim cannot be independently remeasured from the retained set.

The retained cache also has 863 `FI` rows before the identity map's 2023-06-07 admission start. Of those, 328 equal the `FISV` row and 535 differ. Cache normalization previously retained those rows for a `same_issuer_ticker_reuse` identity because it only excluded pre-admission rows for `successor_reset` identities.

Official filings confirm the listing periods: Fiserv's 2023 Form 10-K says its common stock transferred from Nasdaq under `FISV` to NYSE under `FI` on June 7, 2023. Fiserv's October 2025 release says it would return to Nasdaq as `FISV` on November 11, 2025. The current price-identity map admits `FISV` for the full 2020–2025 range while also admitting `FI` over the intervening dates. This overlap needs reconciliation by the #68 identity owner; this follow-up does not change that contract.

## Correction in this branch

`_normalize_cache_to_cutoff_basis` now drops cache rows outside each identity's declared admitted interval. Approved warm-up rows can still enter under the successor label through the separate reviewed predecessor-copy path.

After cache/SIP merge, the exporter now compares exact OHLCV values across every declared predecessor link. A mismatch fails the export before publication and records final-output continuity in provenance when it passes. It does not select a winner between disagreeing price sources or rewrite historical data.

Running the new guard over the pinned retained CSV rejects it with `published price identity continuity mismatch for FISV/FI: 1145/1473 shared rows differ; first mismatch 2021-04-22`. The normalizer correction removes pre-admission FI cache rows before the explicit predecessor warm-up is merged, and the final-output guard then checks the composed result. This protects later exports from repeating the audit-scope gap. It does not repair the already-retained CSV: no corrected price bundle is emitted because the staged provider snapshot is missing and price identity remains unresolved.

Thirteen of the fourteen declared successor audits record zero same-date overlap. This means **no overlap was observed**, not that continuity was proved. Where the export creates predecessor warm-up rows by copying, byte equality of those copied rows does not independently authenticate security identity, action treatment, or adjustment basis.

## Segment-contract interface gap

The price exporter still loads one legacy `PriceIdentity` interval per canonical ticker from `config/pit_price_identity_map.csv`. It does not consume the opt-in V3 `price_identity_segments_v1` contract or its segment resolver. The accepted segment representation keeps the existing request-contract keys stable and can represent the two distinct FISV episodes around the intervening FI episode.

This follow-up leaves the FISV legacy interval and the request-key contract unchanged. Its new audit tolerates zero overlap and does not require two instruments to print the same row on transition dates; it reports zero overlap as unobserved continuity. With the current legacy map, however, the observed overlapping rows conflict and export fails closed. A separate segment-aware price admission/normalization increment is needed after #68 supplies authenticated source assertions and a segment object bound to the unchanged request-contract digest. That increment must select exactly one active segment per lineage/date, preserve both FISV episodes under the one FISV request key, and use date-bounded predecessor rules without fabricating or requiring transition-day overlap. The current patch is not that implementation.

## Reference-series reconciliation

The retained `sip-raw.csv`, `sip-split.csv`, and `references-cutoff.csv` all have SHA-256 `4991b8f524986a8e7d5e956c04920b06a63a9d1d5dffe3a942a170c798a33f9f` and contain 4,524 rows. Their acquisition manifest records separate RAW and SPLIT retrieval timestamps and a cutoff factor of `1.0` for each of SPY, QQQ, and IWM. The source path calls the RAW and SPLIT fetch helpers separately, then derives `references-cutoff.csv` by applying those factors to the SPLIT rows. The equal output bytes are therefore consistent with the retained unity-factor calculation; this reference sample does not show an adjustment-value mismatch.

This only reconciles the three retained reference outputs. It does not establish provider licensing, production-use rights, total-return treatment, or coverage for the complete tradable production union.

## Row and action classification

| Case | Classification from retained evidence |
| --- | --- |
| 183 expected S&P member/session pairs across 17 ticker labels | **Missing observations**; not zero-volume rows and not filled. |
| 1,430 flat OHLC rows with volume zero across 19 tickers | **Present but unusable status unresolved**; 23 fall on covered evaluation pairs and one on the warm-up cohort. The assessment reports positive-volume sensitivity separately; it does not silently discard the rows. |
| FISV/FI | **Unresolved source and identity conflict**; all 610 published overlap rows differ. Official listing windows support keeping the map under #68 review. |
| COG/CTRA | **Bounded continuity support only**; 442 pre-admission CTRA warm-up rows equal COG. SEC filing material records the October 1, 2021 merger and CTRA trading from October 4, but that does not make the retained sample a production-admitted action ledger. |
| PEAK/DOC | **Identity/acquirer supported; price path unresolved**; Healthpeak's filing identifies Healthpeak as accounting acquirer. The retained DOC row on 2024-03-04 is flat, zero-volume, and discontinuous from the surrounding PEAK/DOC closes. |
| VIAC/PARA to PSKY | **Predecessor-price continuity excluded**; Paramount and Skydance announced completion on August 7, 2025, with PSKY beginning to trade that day. The retained map keeps PSKY in a separate successor-reset chain. |
| Other removals, delistings, and spin-offs | **Unresolved**; 104 membership removals do not state the action reason, and no complete action ledger is retained. |

Primary-source references used for those bounded identity facts:

- [Fiserv 2023 Form 10-K](https://www.sec.gov/Archives/edgar/data/798354/000079835424000037/fi-20231231.htm)
- [Fiserv October 2025 listing announcement](https://investors.fiserv.com/news-releases/news-release-details/fiserv-announces-transfer-stock-exchange-listing-nasdaq)
- [Coterra merger completion and ticker schedule, filed with the SEC](https://www.sec.gov/Archives/edgar/data/858470/000110465921122041/tm2129019d1_ex99-1.htm)
- [Healthpeak 2024 Form 10-Q](https://www.sec.gov/Archives/edgar/data/765880/000162828024033154/peak-20240630.htm)
- [Paramount/Skydance merger completion announcement](https://ir.paramount.com/news-releases/news-release-details/skydance-media-and-paramount-global-complete-merger-creating)

## Next concrete actions

1. **Offline identity contract review (#68 owner):** reconcile Fiserv's dated symbol windows against the map and decide whether `FI` and `FISV` rows represent exchange-listed symbols or a provider's historical aliases. The official window is FISV through 2023-06-06, FI from 2023-06-07 through 2025-11-10, and FISV again from 2025-11-11. Do not use the current overlapping intervals as a production identity decision.
2. **Recover exact prior inputs if retained:** look for the hash-declared `alpaca_sip_snapshot.csv` (`8d0ab3c8f72538ee0167d999b754ed5e4bb6594da2d9d169cb84e5bc705a536d`) and the cache source whose declared SHA-256 is `1ac1a08341e103d594a14f8ba53f628925a45c3e2362864da710a22d7d2ae850`. The pinned input root and the two expected prior archive roots checked in this follow-up did not contain those files. If recovered, hash-check before using and publish any corrected investigation bundle under a new versioned path.
3. **Finite provider proposal only if prior inputs are unavailable:** after confirming an allowed source and use basis, request only daily SIP/SPLIT history for `FI` and `FISV` over 2020-01-01 through 2025-12-31, plus RAW cutoff calibration only if that adjustment step is required. No provider credentials or requests were used in this follow-up.
4. **Full #69 input gap:** obtain the dated S&P 500 + Nasdaq-100 + Russell 2000 membership union, a security/action ledger for the 104 removals and merger/spin-off cases, source-use terms, and source-backed resolution of the 183 mapped S&P session gaps and 1,430 flat zero-volume rows. Re-run production coverage only after those inputs are hash-pinned and the #68 identity contract is accepted.

## Verification and remaining limits

The targeted test selection passed: **6 passed, 22 deselected**. `ruff check export_pit_prices.py tests/test_export_pit_prices.py` passed. Pytest emitted a cache-directory permission warning under the ignored `.artifacts/pytest` path; it did not affect the run.

The full exporter/provider workflow was not run. No corrected price bundle is published because the pinned inputs omit the staged SIP snapshot and original cache source, and the admitted identity segments are not authenticated. Versioned no-clobber offline publication from authenticated retained inputs is authorized when those inputs support the result. Any new credentialed provider request was not made and requires separate review. Original raw inputs and the prior assessment receipt remain unchanged. #69 remains open pending full eligible membership and identity inputs, source-use basis, source-backed gap/action resolution, and reconciliation of the mapped FISV/FI interval by the owning identity work.

The immutable execution/source/input receipt is `docs/issue-69-source-reconciliation-followup-receipt-2026-10-02.json`.
