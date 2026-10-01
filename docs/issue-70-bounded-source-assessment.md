# Historical-05 / issue #70: bounded retained-source assessment

**Assessment date:** 2026-10-01

**Branch:** `codex/issue-70-bounded-source-assessment`

**Assessment base:** commit `ab385d792e19ff6db39d87f1123f47f660fc1e1d`, tree `46a4b97a29f5fe05b395fc4ff5e36d6451db9bdc`

**Issue state used:** the lead-captured #70 body is **OPEN**, SHA-256 `32e2f756803887dd23baad10fa92a85f59941ca9334ffaba9c3574b9099c2a89`.

This report measures one retained SEC-fundamentals export generation offline. It is partial preparation evidence; it does not close #70 or qualify the export as the downstream accepted production input.

## Lead-review correction

The first receipt and its `1,946/1,946` trace result used an origin-key match that did not require the CompanyFacts period end, a compatible duration/statement family, or the metric's expected unit. The [original receipt](issue-70-bounded-source-assessment-receipt.json) is preserved unchanged as the record of that weaker definition. The corrected result is in a separate [corrected receipt](issue-70-bounded-source-assessment-receipt-corrected.json).

The corrected trace still keys facts by the recorded accession, concept, form, filed date, fiscal year, and fiscal period, and also requires exact equality with the export `period_end`. EPS, revenue, and net income require the matching quarterly (70–115 days) or annual (300–430 days) duration family; balance-sheet metrics require an instant fact and a balance statement row. Expected units are `USD/shares` for EPS, `USD` for amount fields, and `shares` for shares outstanding. Only one fully qualifying fact with a scalar value match counts as `matched_unique`; multiple qualifying facts remain explicitly ambiguous. Other outcomes remain in the nonempty-value denominator under their own dispositions.

The corrected bounded run used the same three issuers (`A`, `AMZN`, `MSFT`) and reopened only their named CompanyFacts and submissions members. All **1,946/1,946** nonempty source-linked sample values had exactly one qualifying match; no period, family, unit, value, or ambiguity disposition remained in this sample. The corrected receipt reused archive SHA-256 attestations from the committed original receipt, checked current paths, byte sizes, expected hashes, and sidecars, and records that the ZIP bytes were not rehashed. It read the six selected members again. The assumption is that the current archives at the same paths and sizes are unchanged.

Fifteen focused tests pass. They exercise both the qualification helper and the bounded archive trace with wrong-period, wrong-duration, wrong-statement-family, wrong-unit, ambiguous, mismatched-value, and missing-origin cases. Each public-trace negative case remains counted as a nonempty exported value without being called a source match.

## Four project assessment fields

| Field | Status | Assessment |
|---|---|---|
| **Implementation** | Partial | Added a dedicated offline assessment script and this report/receipt. The shared SEC exporter, bundle builder, financial calculator, and #71 implementation were not edited. |
| **Required inputs** | Partial | Nine selected files, including both SEC ZIPs, match the alternate export’s declared digests. The alternate export is a separate generation from the acquisition-cache CSV used by the earlier material-only packet; the bridge between generations is not retained. The declared identity-manifest CSV is absent beside this export. |
| **Acceptance evidence** | Partial | Field/lookback coverage is measured for two dates, source-to-audit alignment is exact, and the corrected three-issuer trace requires exact period, compatible family, and expected unit. Foreign coverage and universe-wide Q4 unit/currency/accounting-basis reconciliation remain unmeasured. |
| **Dependencies** | Start condition met; acceptance still gated | The accepted #66 scope supplies the start contract. Production acceptance still needs the eligible #68 membership/security identities and data lineage; the old S&P-only seed used here does not satisfy that gate. |

## Acceptance criteria status

| #70 criterion | Status | Evidence and remaining limit |
|---|---|---|
| Actual field and historical lookback coverage by security/date | **Partial** | Two historical member dates are measured below, including each requested field’s lookback readiness. The universe is the retained S&P seed, not the production three-universe set, and this is not a daily all-session matrix. |
| Supported forms, Q4 treatment, concepts, and unavailable inputs explicit | **Partial** | Exported forms and observed concepts are counted; two quarterly rows carry an explicit `Q4` tag. Most rows tagged `Q4` are annual/balance statement rows, and same-concept FY/Q1–Q3 groups are only candidates. Foreign forms and currency/unit reconciliation are unavailable at universe scale. No Q4 values were derived. |
| Public availability separate from period end; retain necessary lookback | **Partial** | All 145,010 rows have separate `period_end` and `public_date`; all 145,010 dates reproduce the supplied-calendar rule and all satisfy `public_date > period_end`. Periods reach 2006 while availability begins at the first supplied session in 2020. These are filing-availability dates, not earliest earnings-release dates. |
| Filing/fiscal policy implemented and measured, including Q4/foreign treatment | **Partial** | The accepted quarterly matcher and #71 cadence are measured without changing them. Filing-time evidence and Q4 tags are explicit. This export emits no 20-F/6-K, carries no country field, and does not establish foreign treatment. It cannot be called complete policy coverage. |

## Source generations and identity

The SEC archives are present under `C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.artifacts\data\sec-fundamentals`. Their full-file hashes and byte sizes match the current alternate `fundamentals_provenance.json` and both archive sidecars. The assessment opens no archive until those digests match, then reads only the single CIK JSON member for each of `A`, `AMZN`, and `MSFT` from each ZIP. It performs no network/provider call, re-acquisition, archive-wide extraction, order evaluation, or strategy-score calculation.

| Input | Bytes | SHA-256 |
|---|---:|---|
| `companyfacts.zip` | 1,407,131,132 | `d7b4b3c5f2fe014a203bdaef2197d2cba5683f434e965fc9bced1023a43c82ca` |
| `submissions.zip` | 1,559,612,838 | `928d67221c6e6183bc343e7234c1391448c15cd1dd644d36b425db2f99ba4350` |
| Alternate `fundamentals.csv` | 10,319,164 | `3cb2ecfcaa75cf9195d2ca91e159e62a283092f223fa2d3ed73d2e139456aab2` |
| Alternate `fundamentals_audit.csv` | 147,626,063 | `bfcde58871671bcd77317a868c460c5a410de0fb3aff6fba25868939002f5424` |
| Alternate `fundamentals_coverage.json` | 1,757 | `3532f0fbd2d76729f3131cfdac600693051b0316fbbc1fc18e08246555f5d52b` |
| Alternate `security_master.csv` | 41,914 | `6f2a632b43a6dca4cdd046d6c3ae06815f9f1e000fb828b53dca25e6eeed5c42` |
| `security_master_exclusions.csv` | 4,370 | `a0862ae2f259651c5627ccb34d40ee420175ab048dab5eb7da7cb655c1d90529` |
| Membership seed | 12,284 | `a12413a9134623b51350ad4671d4a678eeed8edf2241a9985d0d7d51957f5389` |
| Supplied SPY trading days | 16,599 | `93d8ef415bd6be516fb32ebfa5986ad45cbc2077e5beaa9615943db8890be5b9` |

The previous acquisition cache under `...\.artifacts\data\acquisition\acquisition\raw\local-cache` contains a different `fundamentals.csv`: 10,031,683 bytes, SHA-256 `2f653eb5b91b4be79a2b33cd5b43e01749a25a9e5f74581a7f212bb6bd6bd56b`, bound by the acquisition import manifest. Its retained provenance declares 142,329 rows; the alternate provenance declares 145,010 (**+2,681 declared**). This script counted the alternate rows directly but did not recount the older CSV. The older provenance JSON itself is present in the import manifest with `bound_by_original_provenance=false`; its row count remains a metadata claim. The ZIP bytes in the alternate directory match the SEC archive hashes declared by the older provenance; the older original archive directory itself was absent. Membership and trading-day files are retained at both locations and their hashes match. The fundamentals CSV and security-master CSV do not match across generations. The earlier import manifest explicitly labels its bundle `authenticated_previous_SP500_material_only_not_complete_V5_inputs`; this report does not claim the alternate export was consumed by #67.

The alternate security master has 568 rows representing 567 distinct tickers; `FISV` has two nonoverlapping membership intervals with the same CIK. That matches the coverage manifest’s 567 resolved symbols. Those 567 plus 39 distinct exclusions account for the 606-symbol membership union, with no overlap, orphan, or unaccounted ticker. The older and alternate security-master hashes differ. The earlier cache has no audit/coverage bytes even though its retained provenance declares their hashes; the alternate generation has both artifacts. No transformation ledger connecting the CSV generations was found.

The alternate `fundamentals_publication.json` SHA-256 is `14be96843407c0a2f2bee40274929b99e08b1e5f2384ba5df47a4aaa273606d7`. Its hashes for the CSV, audit, coverage, security master, and exclusions match the actual files. It expects provenance SHA-256 `529ed088a5558c777ed935e6dda0951998d038993110a4974b98352d2f99a6d1`; the current provenance hashes to `79a4ff05e9ee18422200a9b1ebdbf28573b1e7d11f7b9c6268b69e710d7873d0`. Removing only `net_income_concept_priority` from the current provenance object and serializing it with the exporter’s sorted, indented JSON format reconstructs the marker-bound SHA exactly. This narrows the mismatch to that metadata field; it does not establish when, by whom, or why it was added, nor does it establish a transaction history linking the alternate files to the acquisition generation.

The alternate provenance declares identity-manifest SHA-256 `2dbf5357a98d2deca9a08b27fbbb7de01f4294d987e19065d7660266e1aeada3`; that CSV is not present beside the alternate export. The produced security-master file itself is hash-bound, but the identity manifest cannot be independently replayed from this directory.

## Method and pinned meanings

- The snapshots are `2021-01-04` and `2025-12-31`, using the retained 606-ticker membership-event seed. The script takes the latest member state on or before each date and the latest `public_date` vintage per ticker/statement/period, as of that date.
- Quarterly EPS uses four latest YoY slots; quarterly revenue uses two. The exact shared #66 matcher is used with its 28-day prior-calendar-year tolerance. A blank latest quarterly field stays as an unmatched slot; it cannot silently expose an older value.
- Annual EPS, revenue, and net-income profiles use adjacent available annual observations, with missing values dropped as in the accepted annual growth helper. Three growth slots require four available observations. A prior value must be positive for a slot to count. These are input/readiness counts, not CANSLIM scores.
- The quarterly acceleration candidate counts require the latest two matched growth pairs and the pinned #71 84–105-day quarter-end gap. They do not calculate an acceleration magnitude or claim strategy readiness.
- ROE input availability combines the latest nonmissing annual net income with the latest nonmissing balance-sheet equity by period end, as of the date. It reports availability only; it does not calculate ROE.
- Filing availability stays separate from `period_end`. `public_date` is already normalized to the first supplied SPY session strictly after acceptance calendar date, with filed-date fallback only. No second shift is applied.
- The audit has 144,997 rows using `acceptance_datetime` and 13 using `filed_date_fallback`. All 145,010 reproduce the first supplied SPY session from the recorded source day; none are missing/invalid or mismatched. All 145,010 exact audit rows align with their export rows by `(ticker, statement_type, period_end, public_date)` in sequence. No public date is at or before period end.
- The corrected three-issuer source trace compares each exact CSV row key to `metric_sources` and then to CompanyFacts using the recorded accession, concept, form, filed date, fiscal year, fiscal period, exact `period_end`, compatible statement duration, and metric-specific unit. Matched source units and periods are retained, but numeric source values are not written to the receipt. All **1,946/1,946** nonempty scalar values in this bounded sample had one fully qualifying fact; ambiguity and mismatch are explicit dispositions and stay in the denominator. For the 25 most recent audit accessions per issuer, 68/75 were present in the primary submissions member; all 68 matched form, filed date, and acceptance timestamp/date. Older fragment members were not opened.

Implementation identities are recorded in the receipt. Assessment base `ab385d7` has source blobs `6622b03` for the SEC extractor, `75cc910` for the bundle builder, `14c3c99` for the fiscal matcher, and `71e562f` for the #71 V3 calculator. Calculator identity remains `pit-financial-features-v3`. No #71 or #67 full rerun was performed.

## Coverage by field and date

The alternate export contains 145,010 rows for 566 tickers: 42,000 quarterly, 15,668 annual, and 87,342 balance rows. `period_end` spans 2006-09-24 through 2025-12-19; `public_date` spans 2020-01-02 through 2025-12-31. The 2020 first date is the first supplied session in the calendar, so older filings retained for lookback become visible at that first modeled session; the maximum source-day-to-supplied-session span is not a publication delay. There are 22,790 ticker/statement/period keys with multiple public-date vintages and no same-public-date duplicate keys; vintage multiplicity alone is not called an amendment.

| Export scalar presence (all statement types) | Nonempty rows / tickers | 2021-01-04 full lookback | 2025-12-31 full lookback |
|---|---:|---:|---:|
| Quarterly basic EPS, 4 YoY slots | 54,423 / 557 | 366 / 505 | 394 / 503 |
| Quarterly diluted EPS, 4 YoY slots | 54,519 / 557 | 366 / 505 | 394 / 503 |
| Quarterly revenue, 2 YoY slots | 46,453 / 553 | 426 / 505 | 463 / 503 |
| Annual basic EPS, 3 adjacent growth slots | 54,423 / 557 | 382 / 505 | 411 / 503 |
| Annual diluted EPS, 3 adjacent growth slots | 54,519 / 557 | 383 / 505 | 411 / 503 |
| Annual revenue, 3 adjacent growth slots | 46,453 / 553 | 421 / 505 | 475 / 503 |
| Annual net income, 3 adjacent growth slots | 57,632 / 566 | 390 / 505 | 422 / 503 |
| ROE inputs: annual net income plus positive balance equity | 57,632 NI rows; 59,138 equity rows | 427 / 505 | 455 / 503 |
| Shares outstanding (separate optional proxy) | 31,225 / 535 | 447 / 505 have a value | 470 / 503 have a value |

The snapshot membership denominators are 505 and 503 securities. Members with any exported fact are 465 and 498 respectively; members without a security-master ticker are 36 and 5. These values are coverage diagnostics for this seed, not a claim that every member has usable inputs. Other scalar presence: `common_stock` 47,605 rows / 501 tickers; `total_stockholders_equity` 59,138 / 558; `net_income` 57,632 / 566.

The latest-two matched quarterly-growth pairs with the pinned 84–105-day period gap occur for:

| Candidate field | 2021-01-04 | 2025-12-31 |
|---|---:|---:|
| Basic EPS | 369 / 505 | 403 / 503 |
| Diluted EPS | 369 / 505 | 404 / 503 |
| Revenue | 395 / 505 | 430 / 503 |

These only establish input/cadence availability. They are not acceleration values, scores, or full-issue acceptance evidence.

## Forms, concepts, Q4, and foreign coverage

| Filing form in retained audit | Rows |
|---|---:|
| 10-K | 36,140 |
| 10-K/A | 443 |
| 10-Q | 107,757 |
| 10-Q/A | 670 |

No `8-K`, `10-KT`, `10-QT`, `20-F`, or `6-K` rows are emitted. The source selector’s usable statements are 10-K/10-Q (including `/A` amendments); an 8-K is not treated as the filing feed. The retained security master and fundamental export have no country/domicile field, and the source trace intentionally samples only three US issuers. Thus foreign availability is **unknown**, not zero. 20-F/6-K financial histories are not supported by this measured export.

The recorded concept priorities include basic/diluted EPS; revenue `RevenueFromContractWithCustomerExcludingAssessedTax`, then `Revenues`; net income `NetIncomeLoss`, `ProfitLoss`, then `NetIncomeLossAvailableToCommonStockholdersBasic`; balance `CommonStockValue` and `StockholdersEquity`; and DEI `EntityCommonStockSharesOutstanding` for shares. In the three raw issuer members, source-unit traces were `USD`, `USD/shares`, or `shares`, as appropriate. This sample does not prove unit consistency for the full issuer universe.

The audit has 131 rows with a source `fiscal_period=Q4` tag, but those span annual (57), balance (72), and quarterly (2) output statement types. Only two are quarterly export rows; both are KDP rows with basic EPS, diluted EPS, and net-income source values. Both have fiscal-year label 2017; their period ends are 2016-12-24 and 2017-12-31. No revenue value appears on those two rows. The remaining fiscal-period tags are not counted as direct quarterly Q4 values.

The audit also contains same-single-concept FY/Q1/Q2/Q3 symbol-year candidates: 6,516 basic EPS, 6,524 diluted EPS, 4,468 revenue, and 6,538 net income. These are not Q4 outputs. The assessment does not subtract values or declare these candidates reconciled: the aggregate audit does not establish consistent units, currency, or accounting basis across each group.

The bounded CompanyFacts samples make the Q4 distinctions visible: all three have zero records whose `fp` is literally `Q4`. Calendar-year frame strings containing `Q4` occur 42 times for A, 22 for AMZN, and 36 for MSFT; these are not fiscal-period proof. Selected concepts include 16 AMZN and 8 MSFT 10-K quarter-duration observations, and none for A. Those duration counts are sample facts, not a full-universe direct-Q4 count.

The audit/publication timestamp is SEC filing acceptance time or, for 13 rows, the filed-date fallback. There is no earnings-release archive or release timestamp in this input. Filing publication is therefore not relabeled as the earliest earnings announcement.

## Remaining use boundary and #72 note

This receipt is useful as bounded coverage evidence for the alternate S&P-only export. It is not a downstream-eligible V5 three-universe input and does not replace the prior acquisition generation. Issue #70 should remain open pending source-generation lineage, the declared production membership/security identities, measured foreign treatment, and a reconciled/explicit Q4 rule with eligible evidence.

The alternate `fundamentals_audit.csv` contains per-row filing and metric-origin links, but the scalar `fundamentals.csv` does not carry those source columns. For #72, preserve the audit/provenance bridge when the accepted source generation is connected to the product bundle; otherwise the product output will again expose values without their form, accession, filing-time, concept, or inherited-metric origin.

## Reproduction

Run from the repository root with Python 3.13.14 and pandas 3.0.1:

```powershell
python tools\assess_issue70_retained_source.py `
  --input-dir 'C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.artifacts\data\sec-fundamentals' `
  --membership-csv 'C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.artifacts\data\acquisition\acquisition\raw\local-cache\prices\membership.csv' `
  --trading-days-csv 'C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.artifacts\data\acquisition\acquisition\raw\local-cache\prices\spy_trading_days.csv' `
  --comparison-import-provenance 'C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.artifacts\data\acquisition\acquisition\raw\local-cache\import-provenance.json' `
  --output docs\issue-70-bounded-source-assessment-receipt.json
```

The original run verified 9 input digests, joined all 145,010 export rows to audit rows, and wrote the 96,043-byte original receipt. Receipt SHA-256: `1fc74971cee7c30430ea8efbecdf615f3d8c113849a41c66ec52445d0e3c5733`. Assessment script SHA-256 at that run: `601527fed1c6b408e3fe1057f141f2c3909c3775561814c3804d7246d1376de0`. That receipt is preserved with its original weaker match definition.

The corrected rerun used the same bounded inputs and wrote a separate 102,952-byte receipt. Its SHA-256 is `7e690f3a6e0dd116f5a0b20267fcf6e8eebdd37ba9271eafbb8675c62819c5a8`; the corrected assessment script SHA-256 is `c26a9de43bcf12c11f9f802df12c273301553dfa3f06f21affcec0beaaae3c46`. The receipt identifies repository head `174faca79cecfa0a710bbc4112fd2c81f225f281` and the reused digest-attestation details.

Reproduce the corrected run with:

```powershell
python tools\assess_issue70_retained_source.py `
  --input-dir 'C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.artifacts\data\sec-fundamentals' `
  --membership-csv 'C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.artifacts\data\acquisition\acquisition\raw\local-cache\prices\membership.csv' `
  --trading-days-csv 'C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.artifacts\data\acquisition\acquisition\raw\local-cache\prices\spy_trading_days.csv' `
  --comparison-import-provenance 'C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.artifacts\data\acquisition\acquisition\raw\local-cache\import-provenance.json' `
  --reuse-archive-digests-from docs\issue-70-bounded-source-assessment-receipt.json `
  --output docs\issue-70-bounded-source-assessment-receipt-corrected.json
```

Fifteen focused tests passed with `python -m pytest tests/test_assess_issue70_retained_source.py -q -p no:cacheprovider --no-cov`. The full suite was not run. The CLI help and corrected bounded assessment were run; a live GitHub refresh from this worktree was unavailable through the configured proxy, so the report uses the lead-captured OPEN issue body.
