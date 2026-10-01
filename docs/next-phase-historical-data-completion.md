# Lead A acceptance packet — historical integration

Prepared 2026-10-01. **Implementation and bounded assessments are ready for final principal acceptance and normal merge, subject to required CI. Publication/merge are pending. No achieved-goal notification has been sent.** Full #68/#69/#70 production data delivery remains incomplete.

## Exact source and integration

- Launch base: `ab385d792e19ff6db39d87f1123f47f660fc1e1d`.
- Protected main integrated: `68b5358e55df1d8a93851550f421e594541dc74e`, through merge `255c6bfcbfe2f9bec1530d2eeb8b2feaacde3a6a`.
- Final integrated implementation and verification source: **`40f6b68983f4520ca811f33d0078947771d0ba02`**. Later packet commits change documentation only; they are not relabelled as measurement or test executions.
- #69 owner `d24dc9ccc7fe6b08f7e07a1f7f38a3772194849c`, integrated unchanged as `0200146e5a8511a2a0c235ea8a04564ce514c7ea`.
- #70 owner initial `174faca79cecfa0a710bbc4112fd2c81f225f281`, corrected `8f389406f1c7339662387d446cbbffd997b810e8`; integrated as `b675399` and `f6d7474`. Lead clarified historical/current reproduction commands to write scratch outputs; independent review accepted this report-only change.
- #72 owner initial `df3892234ad4b95408b9354c445e9b3a61985328`, corrected **`32a52915853cfe1a240e7898796213b405c05c66`**; integrated unchanged as `d54255a` and `40f6b68`.

[Ownership/authority ledger](next-phase-historical-data-ledger.md) retains preserved task IDs, exact issue captures, review findings and integration history. The latest review-stage reads found #69/#70/#72 open with original acceptance contracts unchanged. Subsequently the principal updated #69's four published statuses, preserving all three unchecked production criteria.

## Four statuses and recommendation

| Issue/scope | Implementation | Required inputs | Acceptance evidence | Dependencies | Recommendation |
| --- | --- | --- | --- | --- | --- |
| #72 bridge | Complete, independently reviewed and integrated | Actual deterministic synthetic membership/identity/filing inputs supplied for this implementation issue | All three criteria reviewed; real parser-to-builder case, negatives and final 86-test integration pass | #66 met; settled V3 interface consumed; production acquisition separate | **Ready for principal acceptance/merge after required CI** |
| #69 bounded / full | Bounded assessment complete / full production delivery incomplete | Retained S&P sample verified / full eligible union, price/action sources and use basis missing | Independent lead and principal bounded acceptance / full criteria partial and unresolved | Full production acceptance requires eligible #68 | **Accept bounded report; keep full issue open** |
| #70 bounded / full | Corrected bounded assessment complete / full production delivery incomplete | Alternate export/archives verified within stated sample / complete histories, identity and generation lineage missing | Independently accepted two-snapshot/three-issuer trace / full criteria partial | #66 start met; production acceptance requires eligible #68 | **Accept bounded report; keep full issue open** |
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

## Final integrated verification and runtime continuity

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

No issue is closed by this package. Ready for principal final review and normal eligible merge after required CI. The principal owns final acceptance/merge and may then accept #72's implementation criteria; full #68/#69/#70 remain open. Record PR/head, hosted checks, actual merge and the single completion-message receipt in the ledger when they occur. Until then this is a reviewed acceptance-ready package, not a claim that the phase has been merged or its goal achieved.
