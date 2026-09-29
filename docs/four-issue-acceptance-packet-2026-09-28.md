# Four-issue acceptance packet (working review)

**Review date:** 2026-09-28. **Integrated branch:** `codex/four-issue-integration`.
This packet remains open for #68 source-backed data delivery. It records bounded
local acceptance separately from GitHub issue
closure. No issue in this packet is closed by the document.

## Exact issue and contract basis

The lead fetched the current GitHub bodies of [#67](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/issues/67), [#82](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/issues/82), [#68](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/issues/68), and [#71](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/issues/71) on 2026-09-28. All four were open, with zero issue comments. Their criterion text below is mapped to evidence without substituting a worker completion or passing fixture for acceptance. The start contracts [#66](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/issues/66) and [#80](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/issues/80), the #82 acceptance prerequisite [#81](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/issues/81), and the separate paper boundary [#97](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/issues/97) were closed when fetched; their local accepted documents are unchanged on this branch.

| Accepted contract | Git blob at this review source |
| --- | --- |
| `docs/historical-feature-specification-v1.md` (#66) | `9ab5efcd9e5ac94d1cb301bc43d20dc9edc41026` |
| `docs/strategy-policy-contract-v1.md` (#80) | `7eefe196f4de1a93ebaa84eff33684aba209edf9` |
| `docs/research-reproducibility-index.md` (#81) | `532a20fd25f01c9783db215418f7b744497dfd32` |
| `docs/qualified-policy-deployment-contract.md` (#97) | `0c9eb19a118cf5bb1ad882c42374c6fc3a7cabe1` |

The first three contracts govern historical data, policy inputs and version-five
research. #97 separates any qualified policy from paper execution; this work
does not change the operational database, scheduler, broker or paper runtime.

## #67 — historical feature availability

**Current bounded recommendation:** ready for acceptance of the reporter and
named development measurement. Production-universe coverage and actual
decision-by-decision policy consumption remain unverified claims outside that
bounded acceptance.

| Published criterion | Evidence and disposition |
| --- | --- |
| Explicit denominators and exclusions for each field/calculation | Pass. The [coverage report](historical-feature-coverage-report-v1.md) and retained `.artifacts/evidence/issue-67-lead/` summary give source, visibility, lookback, calculability and declared policy-input stages for 20 fields. The exact development bundle SHA-256 is `cf729b47d762ae86287bb8a87c28a1664e7475c74ef1aca736fbc240111349de`; 631,965 member security-sessions are the shared field denominator. |
| Missing periods, late classifications, restatements and early warm-up | Pass. Fixed-history checks include missing comparables, late publication, restatement, absent classification and warm-up; the all-missing newer-quarter freshness regression is covered after #71. |
| Newly measured development denominator | Pass. The exact-source run measured 1,255 SPY sessions, 631,965 member security-sessions and 606 ticker symbols for 2021-01-04–2025-12-31. Independent membership sweep and gzip row count matched; output gzip SHA-256 is `1f5031c52a265f0641905b094d74f1c4b4c38aa5d72fc7631528116f8135dff9`. |
| Fixed market breadth, price RS and industry denominators, including missing inputs | Pass. The report reconciles breadth 50/200, price RS, benchmark sessions, missing lookbacks and absent classifications. Industry-group RS is unavailable on every development member session because the bundle lacks dated industry snapshots. |

The report's calculator identity is `pit-financial-features-v2`. Its
canonical `core/pit_feature_snapshot.py` Git blob is
`8e4020316d13c3de36dac8995fe4743bf888e70c`, and the measured reporter
source revision is `4fdadd285a87c1dd9b1f5f2c27d8fa696d5e9bfb`.
`policy_input_stage` indicates a value is available at a declared interface;
`consumer_paths` are static mappings. Every row records
`actual_policy_consumption=not_measured_no_policy_replay`. The development
bundle is S&P-only schema V2 and has no stable lineage IDs. These limits do not
invalidate the bounded reporter result.

| Status | Current assessment |
| --- | --- |
| Implementation | Reporter, fixed cases and development measurement integrated. |
| Required inputs | Fixed histories and exact retained development bundle available; production union not required for the reporter. |
| Acceptance evidence | Four published criteria pass bounded local review; no policy replay or production coverage claim. |
| Dependencies | Accepted #66; corrected #71 calculator identity bound in report. |

## #71 — financial calculation validation

**Current bounded recommendation:** ready for acceptance of the corrected and
versioned financial semantics; the three broad parity tests remain separately
failing and pre-existing.

| Published criterion | Evidence and disposition |
| --- | --- |
| Skipped periods, adjacent-quarter acceleration, income/equity alignment, restated older periods | Pass. Fixed cases and period traces in the [validation report](historical-financial-validation-issue71.md) exercise these paths. |
| Reproducible mismatch and bounded correction or modeling decision | Pass. Acceleration now requires adjacent fiscal quarters and freshness selects the last observed period; annual missing-year growth and unmatched income/equity are explicitly documented modeling limits rather than filled values. |
| Measure old output before error claims; version changed definitions | Pass. The retained development bundle was measured under old/new semantics: 138,038 formerly non-null earnings-acceleration and 158,883 revenue-acceleration priced security-sessions became unavailable; adjacent-quarter outputs did not change. These are feature-output counts, not trade errors. Calculator identity is `pit-financial-features-v2`. |
| Explicit identities and comparable new results; preserve old reports | Pass. The new calculator/blob identity and input hashes bind the new result. Prior report definitions remain under their prior identities. The #67 report uses the corrected calculator. |

The [baseline parity audit](historical-financial-parity-baseline-audit-issue71.md)
ran the three broader failing tests on accepted starting main
`2c01e76a878a338ab2b743c38c4f1310aab3f75b` and integrated source
`4bf32a3a327421b62d8977b4f4109b0341955e29` under the same runtime. Both
had two `entry market context is invalid` failures and one `schema_version`
KeyError. The test and failure-origin file blobs were identical at both
revisions; no #71 change was on the failure traces. These failures predate #71
and are neither fixed nor silently treated as passing.

| Status | Current assessment |
| --- | --- |
| Implementation | Versioned adjacent-quarter and freshness corrections integrated; annual-growth and ROE decisions recorded. |
| Required inputs | Fixed histories and retained development bundle available. |
| Acceptance evidence | Four published criteria pass bounded local review; broad parity failures proven pre-existing and unresolved. |
| Dependencies | Accepted #66; #67 uses the corrected calculator. |

## #82 — evaluator and candidate execution

**Current bounded recommendation:** ready for synthetic evaluator and candidate
container acceptance. The selected 15 focused tests verified
the actual V5 portfolio simulator on synthetic next-open and gap-stop paths,
matched evaluator argument forwarding, fixed behavior probes and supplemental
mechanism gates as distinct stages. [The #82 receipt](research-03-simulation-verification-2026-09-28.md)
documents those tests and the earlier failed Docker startup. On 2026-09-28 the
user started Docker; the lead verified a Linux/amd64 Docker Engine 29.7.2 via a
scoped host-level check. The pinned Python base and seven historical evaluator
images are local, but none of those images matches the integrated V5 evaluator
source-map SHA-256 `a55875a2ea021ec27a9fd18d8e09b180ce9186fcaf1b3bbb14036db871e11fb4`.
A new Linux/amd64 image has been built from canonical Git-blob source bytes and
independently inspected:
`pit-optimizer-v5-evaluator@sha256:bf842ff5e0dc741f95e96129d224c24d7834feef23c4c20fdfe1d5297d54b527`,
with the same runtime-source label and UID/GID 65532. Its version-pinned lock
has no per-distribution hashes, and pip resolved two additional transitive
packages; the immutable image digest records the realized build. The lead
independently recomputed the 56-path canonical Git-blob source map at current
HEAD as `a55875a2ea021ec27a9fd18d8e09b180ce9186fcaf1b3bbb14036db871e11fb4`.

On the actual Linux Docker runtime, `DockerPanelEvaluatorV5` and
`LocalContainerExecutorV5` ran separate parent and comment-only candidate
containers from that immutable image. Both exited zero, returned 11 fixed-suite
observations and the same semantic fingerprint
`671a50c126b412b90fcaa466a034cade8d151686a90a8c1c1103c4f621ab4fc1`.
The 3,584-byte outputs have different full hashes because request and policy
identities differ. Both stayed below the 67,108,864-byte output bound, used no
scenario-data mount and no container network, and reported complete owned
cleanup; the lead's separate filtered Docker listing found no V5 containers.
The [tracked container receipt](research-03-candidate-container-evidence-2026-09-28.json)
is SHA-256 `2dc4cfce92a5996f60e636e8f13a550858d5e160d3b9916a7628dd3d2aaff1f8`
and binds the full local trace SHA-256
`691dbcf0181310c42e426729493197e0109d85ca7f7c3f04c898b006799cd672`.
The parent/candidate policy-revision identities are
`76f67d16b97d91f99d757fdbfc513f64c2b0d8f6cb09f57a45e14ff67ac217f9`
and `6d3008e8eaba6153b84cdba4c916df184eae06d5b7dbab2a872bced0595ed606`;
their four-file source-bundle hashes are
`109a61992ac79317446861c4cb2053f2704239eb1f8eacfe2b2d27ca2cf7456e`
and `7c033b6436ece98b9b0bc0b39b578ac0067e332dfde35ef791e59ea7c3ab3bc4`.
The receipt retains separate request, command, container and output hashes.
This proves a bounded fixed semantic candidate run, not a historical PIT panel
run or production/strategy performance.

| Published criterion | Evidence and disposition |
| --- | --- |
| Distinguish historical simulation, fixed probes and supplemental observations | Pass for the bounded synthetic scope. The host simulator, actual fixed-suite container probes and supplemental mechanism gates have separate receipts and stage labels. |
| Match parent/candidate assumptions; expose enabled/skipped stages | Pass for the bounded synthetic scope. The evaluator forwarding fixture and actual parent/comment-only candidate runs retain distinct policy/source identities, one image/source map, identical fixed suite and matching semantic fingerprint; no historical PIT data stage was enabled. |
| Bounded reproduction of failures and documented existing passing path | Pass for the bounded synthetic scope. Selected failure/timeout/cleanup mechanism checks, a passing simulator path, and two actual zero-exit containers with owned cleanup are documented. An earlier pre-Docker host-harness import failure is not a candidate result. |

| Status | Current assessment |
| --- | --- |
| Implementation | Existing simulator/evaluator/workers and focused tests integrated; exact-source image built and two actual candidate-path containers executed. |
| Required inputs | Synthetic bars/policies, Docker Engine and a correctly matched local evaluator image now available. |
| Acceptance evidence | 15 selected focused checks and five final-branch focused cases pass; tracked source-matched image receipt, parent/candidate zero exits, matching fingerprint and owned cleanup independently verified. |
| Dependencies | Accepted #80 and #81 satisfied; production PIT bundle and empirical costs are outside the bounded synthetic verification. |

## #68 — historical membership and lineage

**Current recommendation:** not ready. [The source receipt](historical-membership-source-receipt-v1.md)
and [acquisition/transition plan](historical-membership-acquisition-plan-v1.md)
bind the retained S&P event sample and its limits, missing admitted Nasdaq and
Russell histories, and the absent explicit V3 price-identity transitions. The
normalizer's authenticated rename-boundary fix and focused fixtures pass, but
they cannot substitute for a three-index historical artifact. The [chronology
review](price-identity-transition-chronology-review-v1.md) verifies from Fiserv
issuer and SEC records that the same common stock changed FISV→FI on 2023-06-07
and FI→FISV on 2025-11-11. The retained one-row-per-ticker price identity map
overlaps FISV and FI, and cannot express the three distinct symbol episodes.
The integrated normalizer now rejects overlapping predecessor dates; a
segment-identity contract and reconciliation of this chain remain outstanding.
The [partial transition evidence ledger](price-identity-transition-evidence-ledger-v1.json)
is SHA-256 `b4468d45277bbadeb7739f5e47884644609e36f469a479ec52c54311828735d2`.
It binds the retained 607-key request contract (digest `273727c248f57b7376b6cf269312325cdd059287e1f4ed16ab5ce63617ceabc7`),
classifies 13 one-way candidates as supported by cited primary evidence only,
and leaves Fiserv unresolved. It emits no integrated transition and adds zero
membership rows. The issuer/SEC source bytes were not retained; URL,
filing/release identity and section locators are the bounded source references.
The [segment-contract decision](price-identity-segment-contract-decision-v1.md)
records a proposed dated segment ID and resolver contract for the FISV→FI→FISV
case. It is a design and synthetic rejection matrix only: no production segment
object, V3 transition, membership row or evaluator-source change was made.

| Published criterion | Evidence and disposition |
| --- | --- |
| Every event has an effective date and retained source evidence | Pending complete dated seed/event history and source-use basis for all three indices. Retained S&P events have five official spot checks, not complete primary-source validation. |
| Overlaps, renames and short-lived events reconcile by security lineage | Focused normalizer fixtures pass; production cross-index overlap and transition reconciliation pending authenticated `price_identity_transitions` plus complete events. |
| Coverage/exclusions explicit; no fund holdings relabeled membership | Current inventory and exclusions are explicit, and fund holdings are excluded. Full three-index coverage report/artifact remains pending. |

| Status | Current assessment |
| --- | --- |
| Implementation | V3 normalizer, exact rename-boundary fix and fail-closed predecessor chronology guard integrated; source-acquisition/transition and repeated-ticker segment plans recorded. Repeated-ticker segment representation remains unimplemented. |
| Required inputs | Complete admitted S&P/Nasdaq/Russell dated source history, rights/access basis, and authenticated V3 price transitions missing. |
| Acceptance evidence | 20 focused membership checks, 13 retained acquisition-source hashes, partial ledger hash and 607-key price-contract digest verified; Fiserv primary chronology cited, but no complete production union or coverage acceptance. |
| Dependencies | Accepted #66 satisfied; later production joins depend on an accepted lineage artifact. |

### #68 source-access decision remaining

The [acquisition plan](historical-membership-acquisition-plan-v1.md) specifies
the exact deliverables and rights questions. The preferred next input is any
already licensed, rights-cleared historical constituent package the user can
provide: a complete 2020-12-31 seed (or an earlier complete snapshot), every
effective 2021-01-01–2025-12-31 addition/removal and correction, stable
security/share-class identifiers, and written permission for internal research,
local retention and a derived dated lineage artifact for each of S&P 500,
Nasdaq-100 and Russell 2000. The incremental acquisition cost is unknown until
existing entitlements are checked. If these files are unavailable, the concrete
alternative is a quote and rights inquiry to S&P DJI constituent/corporate-
action data, Nasdaq GIW/GIFFD historical NDX composition, and FTSE Russell
DDS/historical RUT constituents and daily changes. Product price, 2021
lookback and derivation rights are unknown; no purchase or license commitment
has been made. With neither input, #68 stays open. Individual public notices
and the 13 evidence-only rename leads cannot fill the complete seed/event
history or authorize a reconstructed index database.

## Integrated verification and next review

At integrated revision `cc141dc` (later revisions changed documents and a
checkout line-ending attribute, not runtime or test source),
the lead ran the #67/#71/#68 focused set: **59 passed**. The selected #82
focused set passed **15** at its recorded source revision; five key cases
passed again after the #82 receipt was integrated. The lead independently
recomputed the current canonical 56-path V5 source-map hash, matched the
tracked receipt to its raw capture hash, and inspected the immutable local
image and empty V5-owned container list. The broad offline suite remains
informational. No model/provider experiment, order, paper-runtime change, production
bundle acceptance or issue closure is inferred from these tests.
