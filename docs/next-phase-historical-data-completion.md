# Lead A acceptance packet — historical integration

Prepared 2026-10-01. **Both principal findings are corrected and independently reviewed on the final integrated source below. The local focused verification passes. PR #117 remains held for required checks on the corrective published head and principal final acceptance/normal merge.** Full #68/#69/#70 production data delivery remains incomplete. No issue closure, merge or achieved original four-issue goal is claimed.

## Exact source and integration

- Launch base: `ab385d792e19ff6db39d87f1123f47f660fc1e1d`.
- Protected main integrated: `68b5358e55df1d8a93851550f421e594541dc74e`, through merge `255c6bfcbfe2f9bec1530d2eeb8b2feaacde3a6a`.
- Latest integrated implementation and verification source: **`5c6816e56dbd3149b6d81d645d03c5d0c9687482`**. #72 owner `ad628594b7708568bb97cb7d69e53745d96fdf35` integrated as `1c331f4`; #70 owner `d0bb52f94ae68aa26fd90e8c0a295456850516c2` integrated as `5c6816e`. Later packet commits change documentation only and retain these actual execution identities. Earlier `40f6b689` verification remains dated evidence.
- #69 owner `d24dc9ccc7fe6b08f7e07a1f7f38a3772194849c`, integrated unchanged as `0200146e5a8511a2a0c235ea8a04564ce514c7ea`.
- #70 owner initial `174faca79cecfa0a710bbc4112fd2c81f225f281`, corrected `8f389406f1c7339662387d446cbbffd997b810e8`; integrated as `b675399` and `f6d7474`. Lead clarified historical/current reproduction commands to write scratch outputs; independent review accepted this report-only change.
- #72 owner initial `df3892234ad4b95408b9354c445e9b3a61985328`, corrected **`32a52915853cfe1a240e7898796213b405c05c66`**; integrated unchanged as `d54255a` and `40f6b68`.

[Ownership/authority ledger](next-phase-historical-data-ledger.md) retains preserved task IDs, exact issue captures, review findings and integration history. The latest review-stage reads found #69/#70/#72 open with original acceptance contracts unchanged. Subsequently the principal updated #69's four published statuses, preserving all three unchecked production criteria.

## Four statuses and recommendation

| Issue/scope | Implementation | Required inputs | Acceptance evidence | Dependencies | Recommendation |
| --- | --- | --- | --- | --- | --- |
| #72 bridge | Complete, independently reviewed at ad62859; integrated at 5c6816e | Eligible synthetic graph and original counterexample retained | Date binding corrected; independent original-case rejection plus final 100-test result; principal final acceptance pending | #66 met; V3 interface consumed; production acquisition separate | **Ready for principal acceptance; merge gate remains** |
| #69 bounded / full | Bounded assessment complete / full production delivery incomplete | Retained S&P sample verified / full eligible union, price/action sources and use basis missing | Independent lead and principal bounded acceptance / full criteria partial and unresolved | Full production acceptance requires eligible #68 | **Accept bounded report; keep full issue open** |
| #70 bounded / full | Bounded assessment complete and independently reviewed / production delivery incomplete | Retained sample eligible for bounded assessment / complete histories, identity and generation lineage missing | Strict scalar trace retained; expected-quarter counts corrected and reconciled; explicit receipt/runtime limits | #66 start met; production acceptance requires eligible #68 | **Accept bounded assessment; keep full issue open** |
| #68 preserved obligation | Existing identity/admission code retained; acquisition incomplete | Full source-backed historical S&P 500/Nasdaq-100/Russell 2000 union and authenticated transitions missing | Partial receipts only; no production admission | Remains a full-data dependency | **Not ready; keep open** |

A successful bridge does not authenticate a production dataset. Measured price row presence does not establish feature readiness, and scalar source tracing does not establish full financial coverage. No policy-consumption, strategy qualification or paper-runtime claim follows.

## #72 criterion-to-evidence map

| Original criterion | Completed evidence |
| --- | --- |
| Bind exact original inputs and record lineage-to-ticker transformation | Deterministic V3 membership projection and ledger; separate identity extraction history; hashes of original membership, identity, SEC export/master/audit and destination inputs; builder rederives projection/history and checks consumed bytes. [Implementation report](issue-72-financial-lineage-bridge.md), [corrected receipt](issue-72-financial-lineage-bridge-followup-receipt.json). |
| Builder accepts coherent output and rejects foreign/relabelled membership | Deterministic synthetic SEC ZIPs through actual submission parser, security-master construction, CompanyFacts extraction, publisher and SQLite builder. Foreign membership/provenance, tampered financial/audit/master/ledger and fixture-hash negatives; production guard unchanged. [Independent review](issue-72-corrected-independent-review.md). |
| Preserve financial values and publication timing | Actual SQLite has both OLD and NEW aliases with EPS 0.75/public date 2020-01-02 and EPS 1.25/public date 2020-01-06, period end 2019-12-31. Pre-membership lookbacks retained without a second date shift. Repeated FISV→FI→FISV, mixed ordinary/segmented histories and multi-CIK reuse controls pass. |

Corrected synthetic bundle SHA-256: **`85d1a599794d4a4d2e0f5808cc605336c42c80373937af60e0952acba01be487`**, independently reproduced. Synthetic submissions ZIP `a2b9c3a38212642239acd53ce5ac654cf1cf27e19595b83844e9c71367d9ad24`; CompanyFacts ZIP `594f3d2b93968ad952cb27a48429496e9bb908535db5214602c627a483f33361`. Corrected receipt binds all other input/output and five executed source-file hashes. The original two-row receipt is preserved and explicitly superseded for acceptance. The builder preserves exporter-declared archive identities; it does not claim to rehash upstream ZIPs.

Three independent review findings were corrected: same-issuer lookback loss, omission of ordinary identities from mixed segment sets, and foreign fixture membership acceptance. Original reproductions and [initial review](issue-72-initial-independent-review.md) remain retained. Final review ran the original counterexamples and five regression functions, independently reproduced the exact bundle, and checked actual historical 68-test and corrected 71-test/Ruff/compile outputs.

## #69 criterion-to-evidence map

| Original criterion | Bounded evidence and full-production limit |
| --- | --- |
| Warm-up/evaluation range with per-security/date coverage | 631,965 expected S&P-only member/session pairs, 631,782 present, 183 missing; 1,255 evaluation sessions and 606 projected ticker labels. Warm-up is a 505-label first-2021 cohort × 253 sessions = 127,765 pairs, 126,908 present, 857 missing; not authenticated 2020 membership. |
| Explicit rename/delisting/merger/spin-off treatment | FISV/FI unresolved; COG/CTRA bounded map support; PEAK/DOC price path unresolved; PSKY predecessor continuity excluded. No complete source-backed action ledger. |
| Adjustment/identity reconciliation and sufficient references | SPY/QQQ/IWM calendar coverage measured. Three labelled raw/split/cutoff files are byte-identical. All 610 admitted FI/FISV overlap sessions disagree on exact OHLCV, contradicting retained audit claims; this conflict is independently reproduced. Full adjustment/source-use reconciliation remains absent. |

[Report](issue-69-bounded-source-assessment.md), [receipt](issue-69-bounded-source-assessment-receipt.json), [lead review](issue-69-bounded-independent-review.md). All 13 source hashes, exact missing pairs and measured denominators are retained. Script SHA-256 `70746f8d683b21c453b7bccbc16e4c5ba2616182013e3d49476e2b9bcfd4d903`; receipt `efe5cbe05902a655d8511889537bb167af3f9b0da607e0b4e9d14587d3e1612c`.

Principal independently accepted exact owner d24/integrated020; report `2026-10-01-issue69-d24-bounded-independent-review.md` in the principal review directory hashes to `94ebe413fcf7ae948c6757b4f96f6fe3c6e95701677dfcf324b004eb67e4f5bc`. Published #69 remains OPEN; none of its three full-production criteria is cleared.

## #70 criterion-to-evidence map

| Original criterion | Bounded evidence and full-production limit |
| --- | --- |
| Field/lookback coverage by security/date | Two snapshots, 2021-01-04 and 2025-12-31, with 505/503 member denominators; 465/498 have any fact. Field-specific quarterly/annual/ROE availability and missingness retained. Not a daily full-union matrix. |
| Forms/Q4/concepts/unavailable inputs explicit | Forms and concepts measured; two quarterly rows carry Q4 tags. Calendar Q4 frames separated from fiscal tags; FY/Q1–Q3 candidates are not derived Q4. No 20-F/6-K export rows, foreign coverage unknown. |
| Separate public availability from period end and retain earlier observations | All 145,010 export rows join audit rows and reproduce the supplied-calendar rule with public date after period end. Earlier period ends retained. SEC filing availability is not earliest earnings-release time. |
| Adopted fiscal/form policy implemented and measured | Accepted annual adjacency, latest missing quarterly slots, #71 cadence, balance-equity selection and DEI shares semantics used. Corrected raw-source trace qualifies all 1,946 sampled nonempty scalars uniquely by exact origin, period, duration/family, unit and value across A/AMZN/MSFT. Full-unit/foreign/Q4 accounting reconciliation remains outside this bounded result. |

[Report](issue-70-bounded-source-assessment.md), [corrected receipt](issue-70-bounded-source-assessment-receipt-corrected.json), [review](issue-70-bounded-independent-review.md), [integration review](issue-69-70-integration-review.md). Corrected script SHA-256 `c26a9de43bcf12c11f9f802df12c273301553dfa3f06f21affcec0beaaae3c46`; corrected receipt `7e690f3a6e0dd116f5a0b20267fcf6e8eebdd37ba9271eafbb8675c62819c5a8`. Original weaker trace receipt `1fc74971cee7c30430ea8efbecdf615f3d8c113849a41c66ec52445d0e3c5733` stays unchanged.

The 145,010-row alternate export is a different generation from the prior acquisition export declaring 142,329 rows. The current provenance mismatches its publication marker; removing only net_income_concept_priority in memory exactly reconstructs marker-bound metadata but does not prove generation history. Corrected trace reuses the earlier full ZIP digest attestation and rereads only six selected members; this reuse and its unchanged-path/size assumption are explicit. The independent reviewer verified identical selected-member hashes and unchanged non-trace measurements. No acquisition occurred.

## Earlier integrated verification and runtime continuity (40f6b689)

At **40f6b68983f4520ca811f33d0078947771d0ba02**, Windows 11 / Python 3.13.14:

- Combined bridge, SEC extractor, V3 normalization/segments, bundle verification and #70 regression tests: **86 passed, 2 warnings, 7.29s**. Actual full command is retained in [verification receipt](lead-a-historical-integration-verification.json).
- `python -m ruff check .`: **passed**.
- `compileall`: exit 0, with listing warnings for inaccessible ignored pytest cache directories. Separately compiled **all 306 Git-tracked Python files in memory successfully**, avoiding ignored-directory traversal.
- First combined attempt used an invalid temporary-root override inside the source tree: 18 passed and 68 fixture-setup errors. Existing fixture guard rejected it before those tests. Retained failure; corrected to the authorized visualization root outside source; no product change.
- Owner #72 broad suite was stopped without final summary after failure markers; partial output remains informational. No claim about full-suite success or failure causation is made.

Warnings in the successful focused run are the existing cache_dir configuration warning and a websockets legacy deprecation. Required hosted Python 3.11/3.13 Ruff/compile checks still must pass before merge.

[Source equivalence receipt](lead-a-historical-source-equivalence.json) verifies all **61 image-context files / 58 runtime files** are exactly unchanged in raw Git bytes from B's tested source `a8662e85c4c06142183c707926e7c302c75e233d`. Runtime map remains `ea44827dc78dcc3f4a4293929e4897345d1f0daa7faef6fbd50cdbb00757d2af`. B's image execution retains its original source and image identities; no new Docker run is claimed. #72 source is byte-identical in Git to its corrected owner commit. Windows CRLF checkout identities are separately documented, not conflated with executed or raw Git identities.

Accepted #71 calculator blob remains **`71e562ffc00dc4e617f1cd625e3f13ea5fe3f81a`**, calculator `pit-financial-features-v3`. No mapped module gained an import. The bridge changes ingestion/provenance, not financial formulas, historical evaluator accounting or policy consumption. Peer B owns image closure; peer C consumes unchanged calculation/availability semantics.

## Preserved earlier wave and remaining boundaries

[Original four-issue acceptance packet](four-issue-acceptance-packet-2026-10-01.md) remains under its original identities. #67/#71/#82 were accepted and closed. #67 is a development coverage measurement, not production coverage or replayed policy consumption. #71's three broader parity failures were baseline-reproduced and remain informational. #82's actual container evidence is retained without substitution by a different source image. #68 remains open and the original persistent goal is not complete.

No operational database, scheduler, broker or paper state changed. No provider/model experiment, external source acquisition, purchase, license commitment or automation was performed in this phase. Preserve the historical data → optimizer/candidate → same evaluator → separately qualified paper integration boundaries.

## Publication and final acceptance

Published [PR #117](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/pull/117), initially at packet commit `9e80525`. No issue is closed by this package. The corrective package is ready for principal final review and normal eligible merge after required CI on its published head. The principal owns final acceptance/merge and may then accept #72's implementation criteria; full #68/#69/#70 remain open. Record PR/head, hosted checks, actual merge and the single completion-message receipt in the ledger when they occur. Until then this is a reviewed acceptance-ready package, not a claim that the phase has been merged or its goal achieved.

## Historical principal correction hold - date-window consistency

After publication, principal review reproduced an additional P2 finding on owner `32a52915853cfe1a240e7898796213b405c05c66`: the validator accepts a projection/extraction history ending 2024-12-31 while independently bound prices/export/bridge retain 2025-12-31, if affected graph hashes are coherently regenerated. Financial rows remain unchanged. Previous passing reviews and 86-test results do not cover this counterexample and are not final acceptance.

The same #72 owner is correcting extraction start/end and applicable membership-window consistency against authenticated contracts, preserving the distinct warm-up and membership ranges. Required regression evidence includes both start and end mismatches, applicable membership bounds and coherent acceptance. No old report, image, data or runtime remeasurement is needed. Original principal executable/result are retained at `C:/Projects/trading_bot/RS-momentum-EMA-trading-bot/.artifacts/coordination/principal-reviews/issue72-32a529-evidence/` and copied locally under `.artifacts/coordination/principal-window-binding-*`.

Prior readiness recommendations above are superseded by this hold until the correction, independent review and final focused verification are complete. #69 bounded acceptance remains intact; #70 principal review continues. Full #68/#69/#70 production criteria remain open.

## Historical principal correction hold - #70 expected quarterly slots

A second principal finding on `8f389406f1c7339662387d446cbbffd997b810e8` clears the strict unique scalar-origin trace but rejects the quarterly full-lookback readiness measurement: selecting the newest observed matches can skip intervening missing quarters or an annual-anchored unavailable latest quarter. Both principal in-memory controls incorrectly return one ready member.

The same #70 owner is using accepted `core.pit_coverage._quarterly_eps_full_window` semantics for four EPS/two revenue slots, with as-of-visible annual anchors and explicit missingness, without changing shared calculation code. Required evidence includes missing-intervening and missing-terminal negatives plus coherent positives through actual analyze_export. Observed match counts may remain separately labelled. Prior receipts remain under their original definitions; new bounded proof will supersede only affected readiness findings. No old #67/#71/#82 run or acquisition is required.

The earlier bounded #70 acceptance recommendation is superseded pending this correction and final independent review. Its 1,946 strict scalar-origin matches remain separately valid evidence; that fact does not validate full-lookback readiness.


## Final corrective acceptance evidence (5c6816e)

### #72 W1 — authenticated date-window binding

Owner `ad628594b7708568bb97cb7d69e53745d96fdf35` binds projection extraction start/end to authenticated prices and export/bridge bounds; membership start is shared across projection/export/bridge, and membership end equals cutoff. Canonical dates and ordered windows are required, while warm-up and membership starts remain distinct. Reconstruction consumes the authenticated bounds. The unchanged original input graph, alias lookbacks, mixed identities, production guard and value/publication semantics remain reviewed.

[Independent review](issue-72-window-binding-independent-review.md) replays the principal's actual coherently rehashed shortened-window case: original control accepts; altered graph rejects with the specific semantic mismatch. Owner actual 32-test/Ruff/compile outputs are authenticated and retained under `.artifacts/coordination/issue72-window-binding-owner-command-outputs.json`; [versioned receipt](issue-72-financial-lineage-bridge-window-binding-receipt.json) preserves both earlier receipts. New integrated tests include the actual synthetic parser/master/extractor/publisher/builder path. The earlier bundle hash above remains its original run identity, not a newly asserted bundle digest.

### #70 F1 — expected quarterly slots

Owner `d0bb52f94ae68aa26fd90e8c0a295456850516c2` uses unchanged accepted `core.pit_coverage._quarterly_eps_full_window` semantics, anchored only by an as-of-visible annual period with an observed financial scalar. Annual values never fill quarterly gaps. [Versioned addendum](issue-70-bounded-source-assessment-quarterly-lookback-v2.json) binds script SHA256 `290155b6ea16c330ad00785501610a637d151cd2c1637e2fe7a80960484b165d`, actual execution HEAD `8f389406`, source inputs and prior trace; final owner packaging is a distinct revision. [Independent review](issue-70-quarter-slot-independent-review.md) checks original scope plus correction and reconciles all counts.

| Profile | 2021 ready / members | 2021 matched + missing slots | 2025 ready / members | 2025 matched + missing slots |
| --- | --- | --- | --- | --- |
| Basic EPS, 4 slots | 0 / 505 | 1238 + 782 = 2020 | 0 / 503 | 1358 + 654 = 2012 |
| Diluted EPS, 4 slots | 0 / 505 | 1237 + 783 = 2020 | 0 / 503 | 1361 + 651 = 2012 |
| Revenue, 2 slots | 364 / 505 | 792 + 218 = 1010 | 395 / 503 | 859 + 147 = 1006 |

Prior observed-period ready labels (EPS366/394, revenue426/463) remain historical diagnostics. A now retains missing terminal October at both snapshots: EPS3/4 and revenue1/2, not ready. Aggregate missing counts above are derived from the retained fixed denominators and matched histograms; typed reasons and slot dates are retained for A/AMZN/MSFT only. Annual/ROE and 1,946 strict source-origin trace results are reused, not recomputed or relabelled.

The measured addendum's generic inherited archive-reuse sentence incorrectly says members are read again. The report now explicitly corrects that sentence: this revision opened **zero** archive members and recomputed **no** source trace. Prior whole-ZIP attestation remains an unchanged-path/size assumption, not fresh cryptographic verification. Original receipt bytes are preserved. V2 did not contemporaneously capture package versions; prior and later same-worker Python3.13.14/pandas3.0.1 observations are disclosed as such. This runtime provenance limitation is distinct from the actual authenticated measurement commands and source/input identities. Reproduction commands pin each historical script and write scratch receipts.

### Final integrated checks and unchanged interfaces

At `5c6816e56dbd3149b6d81d645d03c5d0c9687482`: **100 focused tests passed, 2 existing warnings, 13.14 seconds**; whole-repository Ruff passed; all **306 tracked Python files** compiled in memory. [Exact verification receipt](lead-a-historical-correction-verification.json) contains actual commands, runtime, log hashes and owner source equivalence. No broad suite or historical study was rerun.

All **61 image-context / 58 runtime Git files** remain byte-identical to B's tested `a8662e85c4c06142183c707926e7c302c75e233d`; evaluator map `ea44827dc78dcc3f4a4293929e4897345d1f0daa7faef6fbd50cdbb00757d2af` and #71 calculator blob `71e562ffc00dc4e617f1cd625e3f13ea5fe3f81a` are unchanged. No new container execution is claimed. The earlier #67/#71/#82 acceptance evidence remains under its actual source/input/image definitions. Full #68/#69/#70 remain open; bounded acceptance does not admit production data or downstream qualification.

Fresh #70/#72 issue reads on 2026-10-01 preserve the exact original unchecked criteria and required-input scope, with the principal's current hold appended. Local corrective evidence supersedes the unresolved local work described in the two historical hold sections above; it does not remove the principal's final acceptance gate. Required hosted checks must bind the new published head before normal merge.

### Receipt-only correction after verification

Principal inspection identified a missing character in the new #72 receipt's test-source digest (63 characters rather than 64). Same owner correction `80e2bfda6c369a5f55d6cf635ad015d9c2ff1262`, integrated as `5e97ccf`, repairs that declaration and the linked correction-result hash. The correct tested working-byte SHA256 is `65427048f04dab8667553e970e953888dddefee7b5aa4e929bc15347a6056437`. Implementation and tests are unchanged from the verified `5c6816e` source; no execution is repeated or relabelled. Original df389/32a receipts and baseline counterexample remain unchanged. The independent review retains and corrects its earlier mistaken hash-match assertion.
