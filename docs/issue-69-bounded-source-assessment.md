# Issue #69 — bounded retained-source price assessment

## Scope and disposition

This assessment uses only byte-pinned local artifacts retained under:

`C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.artifacts\data\acquisition\acquisition\raw`

It measures an S&P 500-only development sample, inspects the retained identity and acquisition declarations, and records exact missing sessions and action samples. It does not claim the S&P 500 + Nasdaq-100 + Russell 2000 production union, a stable issuer count, licensed production-use rights, or strategy readiness. It did not make network/provider calls, change product code, or rerun the completed #67 reporter.

The work is on `codex/issue-69-bounded-source-assessment`, based on `ab385d792e19ff6db39d87f1123f47f660fc1e1d` (tree `46a4b97a29f5fe05b395fc4ff5e36d6451db9bdc`). The original #67 branch was preserved.

| Status dimension | Result |
| --- | --- |
| Implementation | **Complete for this bounded offline assessment**: dedicated script, report, and receipt. |
| Required inputs | **Insufficient for a production claim**: retained material is prior S&P-only input, not the eligible three-index union and complete price/action source lineage. |
| Acceptance evidence | **Bounded evidence complete; full #69 acceptance not satisfied.** |
| Dependencies | **#68 remains open** for the complete eligible union; #72 owns the separate exporter/identity bridge. |

## Reproduction

The script reads and hashes all 13 declared input files before parsing or measuring them. It stops if any digest differs. It reads only local files and Git metadata.

```powershell
python scripts/assess_issue_69_retained_prices.py --data-root 'C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.artifacts\data\acquisition\acquisition\raw' --output 'docs/issue-69-bounded-source-assessment-receipt.json'
```

Measured with Python 3.13.14 on Windows 11. The receipt records the command arguments, exact Python runtime, script SHA-256, Git HEAD/tree, and the exact pre-write Git status at measurement time.

```json
{
  "covered_evaluation_pairs_with_zero_volume_row": 23,
  "covered_pairs_on_price_identity_overlap_dates": 610,
  "duplicate_membership_event_rows": 0,
  "duplicate_price_pairs": 0,
  "evaluation_coverage_pct": 99.9710427,
  "evaluation_covered_pairs": 631782,
  "evaluation_member_security_session_denominator": 631965,
  "evaluation_missing_pairs": 183,
  "evaluation_sessions": 1255,
  "initial_2021_snapshot_ticker_label_count": 505,
  "initial_warmup_pairs_with_zero_volume_row": 1,
  "initial_warmup_sessions": 253,
  "inputs_hash_verified": 13,
  "invalid_price_rows": 0,
  "price_rows": 866025,
  "price_symbols": 607,
  "receipt": "C:\\Users\\llong\\.codex\\worktrees\\3d87\\RS-momentum-EMA-trading-bot\\docs\\issue-69-bounded-source-assessment-receipt.json",
  "reference_files_byte_identical": true,
  "zero_volume_rows": 1430
}
```

The executed script SHA-256 is `70746f8d683b21c453b7bccbc16e4c5ba2616182013e3d49476e2b9bcfd4d903`. It ran against Git HEAD `ab385d792e19ff6db39d87f1123f47f660fc1e1d`, tree `46a4b97a29f5fe05b395fc4ff5e36d6451db9bdc`; the receipt preserves the status snapshot (staged baseline with the script correction still in the worktree) before final restaging and commit.

The coverage denominator is derived from the retained dated membership events and SPY calendar, never from price-present rows. The membership projection uses only the dated rows in `pit_membership_symbol_map.csv`; missing identity transitions are not invented. A membership event is applied on its effective date. A row counts as present only when its exact projected ticker/date exists and, where a retained price-identity segment exists, that date falls within that segment's declared interval.

The evaluation range is the SPY-session interval **2021-01-04 through 2025-12-31** (1,255 sessions). This follows the retained local S&P 500 snapshot effective 2021-01-01 through the final input session. The warm-up interval is **2020-01-02 through 2020-12-31** (253 sessions). Its 505-ticker denominator is the 2021-01-04 snapshot projected backward only to inspect available price rows; it is not evidence of 2020 constituent membership. Across the evaluation, 606 canonical ticker labels appear; that is not a deduplicated stable-security or issuer-lineage count.

Prior-price-row counts at thresholds 60, 200, 252, and 260 are diagnostic only. They include zero-volume rows and do not require a current-session price or a consecutive session window; they do not mean feature or evaluator readiness. The complete per-ticker, per-session coverage, missing ranges, price-identity overlap dates, and benchmark records are in the JSON receipt.

## Usable retained input inventory

| Input | Bytes | SHA-256 |
| --- | ---: | --- |
| `local-cache/prices/prices.csv` | 43,650,461 | `dd18e38d14356df2be9aea79bc777407d40750305dcf327a2f3552815c39c376` |
| `local-cache/prices/prices_provenance.json` | 265,269 | `7fca2de6d408e232bca560f1df10aeae88433f3a58e1bb64f7bef20944f95ca5` |
| `local-cache/prices/spy_trading_days.csv` | 16,599 | `93d8ef415bd6be516fb32ebfa5986ad45cbc2077e5beaa9615943db8890be5b9` |
| `local-cache/prices/pit_price_identity_map.csv` | 5,383 | `6a9ec69bc0fe05decea1b832cac8e26a611d706cce831d5687fa5424f9544955` |
| `local-cache/prices/membership.csv` | 12,284 | `a12413a9134623b51350ad4671d4a678eeed8edf2241a9985d0d7d51957f5389` |
| `local-sp500/membership.csv` | 12,284 | `a12413a9134623b51350ad4671d4a678eeed8edf2241a9985d0d7d51957f5389` |
| `local-sp500/membership_provenance.json` | 14,755 | `16fdda33dc102c3e8bfbcd2f683812666a892a0fb28bdc5347ba90c1e9206518` |
| `local-sp500/pit_membership_symbol_map.csv` | 9,645 | `6284214a6a4cefd766b3c52e84be57ac7e087cbf76d642d22abad131d61d8fa4` |
| `local-cache/import-provenance.json` | 4,737 | `357fd624fb7087b0c97bc180cc1f4bc3c70aeba5c644d8d5ebaff12e246a6bd7` |
| `alpaca-references/acquisition.json` | 2,367 | `e9397323b511e91200509013ba54f1fca537c5d3bed9fb7b266b7d3602026f50` |
| `alpaca-references/sip-raw.csv` | 244,771 | `4991b8f524986a8e7d5e956c04920b06a63a9d1d5dffe3a942a170c798a33f9f` |
| `alpaca-references/sip-split.csv` | 244,771 | `4991b8f524986a8e7d5e956c04920b06a63a9d1d5dffe3a942a170c798a33f9f` |
| `alpaca-references/references-cutoff.csv` | 244,771 | `4991b8f524986a8e7d5e956c04920b06a63a9d1d5dffe3a942a170c798a33f9f` |

The two retained membership CSVs are byte-identical. The three SIP/reference CSVs are also byte-identical; filenames and adjustment labels do not make them independent raw, split-adjusted, and cutoff-adjusted evidence.

## Measured coverage and row quality

| Measure | Result |
| --- | ---: |
| Price rows / unique ticker-session pairs | 866,025 / 866,025 |
| Provider ticker labels | 607 |
| Price range | 2020-01-02 to 2025-12-31 |
| Duplicate ticker-session rows | 0 |
| Missing/non-finite numeric fields, nonpositive OHLC, negative volume, invalid OHLC order, or off-calendar rows | 0 |
| Zero-volume rows with flat OHLC | 1,430 across 19 tickers |
| Evaluation denominator | 631,965 member-security sessions |
| Evaluation pairs present in the projected dated segment | 631,782 |
| Evaluation pairs missing | 183 |
| Row-presence coverage | 99.97104270% |
| Covered evaluation pairs on overlapping price-identity map dates | 610 |
| Coverage with those 610 pairs excluded (identity-conflict sensitivity only) | 99.87451837% |
| Covered evaluation pairs with a zero-volume record | 23 |
| Coverage if zero-volume rows are excluded (sensitivity only) | 99.96740326% |

The measured evaluation denominator, covered count, missing count, and percentage exactly reproduce `prices_provenance.json`'s declared 631,965 / 631,782 / 183 / 99.9710427%. This confirms that its S&P-only coverage arithmetic is reproducible from the retained membership and price rows; it does not promote those inputs to a full-universe or identity-authenticated source.

Row-presence coverage retains zero-volume rows rather than silently discarding them. The receipt reports a separate positive-volume sensitivity, not an alternate accepted coverage policy. The 1,430 zero-volume records all have flat OHLC; the largest ticker counts are INFO (662), SBNY (509), and EXE (156). They need source-level adjudication before treating all present rows as usable prices.

The 2020 warm-up cohort is 505 ticker labels × 253 SPY sessions = **127,765** expected pairs. **126,908** are present within the price identity map's declared date intervals (99.32923727%); **857** are missing. The missing warm-up ranges are BBWI (253 sessions), VTRS (219), VNT (195), CARR (64), OTIS (64), and HWM (62). These figures assess histories for the 2021 snapshot labels; they do not infer 2020 index membership.

At the first evaluation session, 2021-01-04, the 505-label cohort has 502 labels with at least 60 prior price rows, 499 with at least 200, 499 with at least 252, and none with 260. The SPY/QQQ/IWM calendar has 253 prior sessions then; its prior-row count reaches 260 on 2021-01-13. These counts do not validate the price on an evaluation date, consecutive bars, or a feature calculation.

The receipt lists all 183 missing evaluation ticker/session pairs and a per-session expected/covered count. It also reports each ticker's expected member sessions, covered sessions, exact missing ranges, and prior-row count thresholds. The 37 evaluation gaps beyond BBWI are not collapsed into a single “other” bucket in the receipt.

The per-security evaluation gaps are:

| Ticker label | Missing sessions | Inclusive missing SPY-session range(s) |
| --- | ---: | --- |
| ANSS | 1 | 2025-07-17 |
| ATVI | 2 | 2023-10-16 to 2023-10-17 |
| BBWI | 146 | 2021-01-04 to 2021-08-02 |
| CTLT | 3 | 2024-12-18 to 2024-12-20 |
| CTXS | 1 | 2022-09-30 |
| CXO | 2 | 2021-01-19 to 2021-01-20 |
| FBHS | 2 | 2022-12-15 to 2022-12-16 |
| FRC | 3 | 2023-05-01 to 2023-05-03 |
| HES | 3 | 2025-07-18 to 2025-07-22 |
| JNPR | 4 | 2025-07-02 to 2025-07-08 |
| MRO | 2 | 2024-11-22 to 2024-11-25 |
| MXIM | 2 | 2021-08-26 to 2021-08-27 |
| PXD | 3 | 2024-05-03 to 2024-05-07 |
| SIVB | 3 | 2023-03-10 to 2023-03-14 |
| TWTR | 2 | 2022-10-28 to 2022-10-31 |
| VAR | 3 | 2021-04-15 to 2021-04-19 |
| XLNX | 1 | 2022-02-14 |

Ranges are inclusive SPY sessions; they do not imply gaps on every calendar day within a range.

## References, adjustment, and source/use basis

SPY, QQQ, and IWM each have 1,508 unique rows covering every retained SPY-calendar session from 2020-01-02 through 2025-12-31. That is enough calendar history for later 260-session diagnostics; it does not supply 260 prior rows on the first 2021 evaluation session.

`prices_provenance.json` declares a composite `existing_hash_pinned_cache_plus_alpaca_sip_snapshot` source, SIP feed, `SPLIT` adjustment, 2020-01-01 to 2025-12-31 request range, and 2025-12-31 cutoff factors. It declares 577,527 cache rows, 866,025 SIP source rows, 855,330 split-source rows, and 288,498 SIP fill rows. Its sampled basis audit records 6 incompatible symbols before cutoff normalization, 2 before normalization, and 0 in a 100,000-row sample after normalization. These remain provenance claims: the original raw acquisition chunks are not in this verified inventory, so those audits cannot be independently recomputed here.

The three reference CSVs have the same SHA-256 and the same 4,524 OHLCV rows. Their pairwise value difference count is zero. Consequently, the retained bytes cannot independently reconcile the acquisition's `raw`, `split`, and `cutoff` labels. The price declaration supports only a split-adjusted claim; no retained input reviewed here declares dividend/total-return treatment. The acquisition marks the benchmark files “reference material only,” with credentials not persisted. The local files do not establish data-license or production-use rights.

The import admission record says `authenticated_previous_SP500_material_only_not_complete_V5_inputs`; it expressly limits the prior S&P material and does not bind the full three-index lineage union. The reference acquisition record says `reference_material_only_pending_complete_tradable_union_and_price_provenance_composition`.

## Identity and corporate-action samples

These categories describe only what the retained maps and prices support in this bounded review. The maps are investigation artifacts, not authenticated production transitions.

| Case | Bounded category | Evidence and limitation |
| --- | --- | --- |
| FISV → FI → FISV | **Unresolved price identity/continuity** | The membership map projects FISV to FI from 2023-06-07 through 2025-11-10. The price identity map simultaneously admits FISV for the full 2020-01-01 to 2025-12-31 interval and FI for those same 610 sessions. Both raw ticker rows exist throughout the overlap; none of the 610 admitted-overlap OHLCV rows match exactly. The retained price provenance's successor audit declares 610 exact overlap rows for the same dates, which does not reproduce from the final CSV. FI also has 863 pre-admission rows, of which 535 do not match the FISV row. For example, on 2023-06-07 FISV closes at 115.89 with volume 102,643 while FI closes at 115.78 with volume 3,075,854. The maps document a ticker episode, but the retained price source cannot reconcile the overlapping series. |
| COG → CTRA | **Supported as a bounded legacy-survivor treatment** | The map assigns the COG legacy survivor to CTRA on 2021-10-04 and explicitly says not to substitute XEC. COG has 442 rows through 2021-10-01; the 442 pre-admission CTRA warm-up rows match COG exactly on OHLCV. The admitted intervals do not overlap; no XEC price rows are present. This is retained-map support only, not production admission. |
| PEAK → DOC | **Price path unresolved; identity treatment supported by map** | The retained map identifies PEAK as Healthpeak's accounting acquirer and excludes pre-merger DOC rows belonging to Physicians Realty. The 1,048 pre-admission DOC warm-up rows match PEAK exactly. However, the effective-date DOC record on 2024-03-04 is flat at 11.24 with zero volume; the prior PEAK close is 17.10 and the next DOC close is 17.04. The retained price path across the merger therefore remains unresolved. |
| VIAC / PARA → PSKY | **Predecessor continuity excluded** | The retained price identity map keeps VIAC/PARA in a `paramount_legacy` chain and gives PSKY a separate `successor_reset` chain beginning 2025-08-07. PSKY has 102 rows from that date through year-end and no predecessor alias or pre-reset PSKY rows. The map records membership identity only and does not assert price/position continuity. |
| Renames and other mapped chains | **Bounded map support only** | The 30-row, 16-chain price identity map includes several dated rename chains; the script checks declared successor warm-up copies against predecessor OHLCV and records the exact results. Copy equality does not itself prove issuer continuity or adjustment basis. |
| Delistings and spin-offs | **Unresolved beyond sampled cases** | The membership event file has 104 removal rows for 104 tickers; removal is not evidence of delisting. No complete corporate-action reason ledger or comprehensive spin-off treatment is among the retained inputs. |

The #72 interface consequence is specific: do not collapse the FISV/FI price records solely from the membership symbol projection while the price identity map admits both symbols on the same dates and the rows disagree. Preserve that ambiguity until source identity evidence resolves it.

## Missing-input matrix

| Required input area | Retained evidence | Missing or unresolved |
| --- | --- | --- |
| Production membership | Local S&P 500 membership, effective 2021-01-01 to 2025-12-22 | Full dated S&P 500 + Nasdaq-100 + Russell 2000 union, including earlier membership needed for the complete study range. |
| Price history | 866,025 rows / 607 ticker labels, 2020-01-02 to 2025-12-31 | Complete source-backed rows for the production union; resolution of 183 measured S&P member/session gaps and flat zero-volume records. |
| Stable security identity | 30 price-identity rows / 16 chains plus a dated S&P ticker projection map | Complete security master and issuer/action lineage; FISV overlap conflict; authenticated production transitions. |
| Corporate actions | Sampled rename, merger, and successor-reset maps | Complete delisting, merger, and spin-off event/reason evidence; membership exits alone are insufficient. |
| Reference series | Calendar-complete SPY, QQQ, and IWM histories | Distinct reproducible raw/split/cutoff artifacts and source composition; current copies are byte-identical. |
| Adjustment and use rights | Split-adjustment and source/feed declarations; cutoff metadata | Original raw source chunks needed to recompute declared audits; explicit dividend/total-return policy and data-use/license basis. |
| Exporter bridge | Existing identity and membership maps | #72's separate implementation/validation remains outside this assessment; the FISV conflict is an interface case that must remain explicit. |

## #69 acceptance criteria mapping

| Criterion | Bounded evidence | Full issue status |
| --- | --- | --- |
| Declared warm-up/evaluation ranges with per-security/date coverage | Explicit S&P-only denominators, exact missing ticker/session pairs, per-date coverage, and warm-up-cohort history counts are in the receipt. | **Partial**: production union and historical 2020 membership are absent; input-row counts do not validate features. |
| Rename, delisting, merger, and spin-off handling is supported/excluded/unresolved | FISV unresolved; COG/CTRA bounded support; PEAK/DOC identity treatment supported but price path unresolved; pre-reset-to-PSKY continuity excluded; delistings and broader spin-offs unresolved. | **Partial**: sample evidence only; no full action ledger or production-admitted identity. |
| Adjustment and identity records reconcile; reference histories are sufficient | Price file hash/count matches retained provenance; reference calendar coverage is complete. Three differently labelled reference files are identical; FISV segments overlap and disagree; source/use rights are not established. | **Partial / unresolved** pending distinct source evidence and eligible #68/#72 inputs. |

Issue #69 remains **open** pending complete, eligible #68 inputs and resolution of the price identity/source-basis gaps. This report is not an acceptance claim for the issue or for production use.

## Artifacts

- `scripts/assess_issue_69_retained_prices.py` — dedicated offline assessment.
- `docs/issue-69-bounded-source-assessment-receipt.json` — exact hashes, execution identity, full per-security/date gaps, benchmark coverage, copied warm-up checks, and deterministic boundary samples.
- `docs/issue-69-bounded-source-assessment.md` — this report.

No raw third-party data was copied into the repository.
