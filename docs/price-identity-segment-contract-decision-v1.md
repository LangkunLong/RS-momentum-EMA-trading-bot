# Price identity segment contract decision v1

Assessment date: 2026-09-28

## Decision

Do not adopt or emit Fiserv price-identity transitions in the current V3 contract. The current contract has one identity row per ticker and transitions that name predecessor/successor tickers. It cannot distinguish the FISV episode before June 2023 from the FISV episode beginning in November 2025. Truncating the existing FISV row would erase the later episode; leaving it unchanged overlaps the FI row.

Record the following as a proposal for a separately versioned segment contract. No code, accepted provenance, price rows, membership rows, or V3 output schema changed for this decision. The FISV dates below are candidate coverage bounds for design discussion, not production-admitted intervals. This note does not claim #68 accepted.

## Source assertions and current evidence limit

Primary records establish the two ticker-change effective dates:

| Transition | Primary assertion |
| --- | --- |
| FISV→FI, effective 2023-06-07 | Fiserv's [Form 8-K Exhibit 99.1](https://www.sec.gov/Archives/edgar/data/798354/000119312523154199/d470900dex991.htm), accession `0001193125-23-154199`, dated 2023-05-25, identifies Fiserv common stock as Nasdaq: FISV and gives June 7 as the expected first NYSE trading date under FI. |
| FI→FISV, effective 2025-11-11 | Fiserv's [issuer release](https://investors.fiserv.com/news-releases/news-release-details/fiserv-announces-transfer-stock-exchange-listing-nasdaq), dated 2025-10-29, and [Form 8-A](https://www.sec.gov/Archives/edgar/data/798354/000119312525274207/d53898d8a12b.htm), accession `0001193125-25-274207`, dated 2025-11-10, identify the same common stock moving from NYSE FI to Nasdaq FISV. |

The issuer/SEC source bytes were not retained, so this review has no source-byte hashes for those filings. The retained provider export has 1,473 FI rows (2020-01-02–2025-11-10) and 1,508 FISV rows (2020-01-02–2025-12-31), but provider rows do not establish point-in-time ticker admission. The existing identity map admits FISV through 2025-12-31 and FI from 2023-06-07 through 2025-11-10. The exact price CSV and request-contract digests are recorded in the [evidence ledger](price-identity-transition-evidence-ledger-v1.json).

Before production adoption, the team needs rights-cleared, source-bound price-symbol coverage and a verified mapping from the retained series into date-bounded identity segments. The current pair of symbol rows is not that mapping. Do not infer it from the overlapping ticker map.

## Proposed versioned representation

Keep `price_identity_request_contracts` and its 607-key digest unchanged. In a future provenance version, add an opt-in `price_identity_segments_v1` object with its own canonical SHA-256 and a parent link to the existing request-contract digest. The object would contain:

- **segments**, each with a stable `segment_id`, `provider_symbol`, `chain_id`, `continuity_kind`, admitted start/end dates, and an anchor marker;
- **transitions**, each with an effective date, predecessor and successor segment IDs, chain and continuity kind, and one or more source-assertion IDs;
- **source assertions**, each with a stable ID, authority, official URL, filing accession or issuer release date, document date, locator, effective date asserted, short factual claim, and source-byte SHA-256 or an explicit null until bytes are retained.

The candidate Fiserv path is three distinct nodes:

| Candidate segment ID | Provider symbol | Candidate bounds |
| --- | --- | --- |
| `fiserv-fisv-pre-2023` | FISV | 2020-01-01 through 2023-06-06 |
| `fiserv-fi-2023-2025` | FI | 2023-06-07 through 2025-11-10 |
| `fiserv-fisv-post-2025` | FISV | 2025-11-11 through 2025-12-31 |

These bounds partition the retained request window. The June 2023 and November 2025 primary assertions support the handoff dates; they do not by themselves authenticate every date in the 2020–2025 price coverage window.

The future loader should require a matching parent request-contract digest and segment-object digest; unique segment IDs; provider symbols present in the parent contract; exact source assertion references; one connected acyclic segment path per segmented lineage; one anchor segment matching the existing chain anchor; predecessor end strictly before each edge; successor admission on the edge date; and no unexplained gap or overlap. Segmented and legacy ticker transitions must not both describe the same chain.

The future resolver should expose segment-aware operations that return a unique active provider symbol for a lineage/date and can follow a specific segment ID through dated edges. A ticker-only call that cannot distinguish multiple chains or segments must fail closed. Existing constructor and resolver behavior should remain unchanged for contracts with no segment object. `PointInTimeUniverseV3` may use the segment resolver only for a digest-validated segmented lineage; its membership input/output row remains `effective_date,security_lineage_id,universe_id,member`.

## Synthetic validation matrix for a future integration

These cases are specifications only; no implementation or tests were added in this decision commit.

| Case | Expected result |
| --- | --- |
| Valid three-node FISV→FI→FISV path with both effective-dated assertions | Resolve FISV to FI on 2023-06-07, FI to FISV on 2025-11-11, and exactly one symbol per Fiserv lineage/date across all three intervals. |
| Existing four-argument `PriceIdentityTransitionContract` construction with no segment object | Preserve legacy behavior and output. |
| Duplicate segment ID, unknown provider symbol, wrong parent digest, invalid segment digest, missing assertion ID, or assertion effective date that disagrees with its edge | Reject the contract. |
| Predecessor admitted through/on the edge, successor not active on the edge, segment overlap/gap, disconnected path, cycle, branch, or two anchors | Reject the contract. |
| Ticker-only resolution matches multiple chains or lacks a unique active segment | Fail closed; require a segment or lineage identifier. |
| V3 lineage membership normalized with a valid segmented identity contract | Preserve the existing four-field membership row schema; keep legacy serialization fixtures stable for non-segmented contracts. |
| The current FISV/FI overlapping two-row map is presented without a segment manifest | Reject or leave unresolved; never synthesize a transition. |

## Integration gate

The shared `core/pit_data.py` and `core/pit_universe_v3.py` files are in the closed V5 evaluator source map. Changing either requires a matched #82 exact-source image and a new parent/candidate container receipt and run. Do not integrate the proposed segment object into the accepted V3 artifact or change those shared files until the source-coverage and rights gate is resolved and the versioned contract is reviewed against that exact image.

No vendor contact, restricted capture, acquisition change, production provenance edit, transition emission, or historical membership row addition was made for this decision.

## Code-only implementation update (2026-09-29)

The opt-in resolver and normalizer support for this representation are implemented in code. A loaded segment contract must bind the unchanged request-contract digest and its own canonical digest. Every source assertion must name a retained relative source-document path; the loader rejects null hashes, missing files, symlinks, paths outside the provenance directory, and byte/hash mismatches. Synthetic tests use generated bytes explicitly labeled as test data, so they validate contract shape and byte verification only; they do not authenticate the cited filings or admit Fiserv production dates.

Segment intervals may have only Saturday/Sunday dates between them. This conservative closed-session rule accepts a Friday-to-Monday boundary and rejects weekday gaps; market holidays are not inferred. V3 lineage membership still serializes the same four columns. The existing source-evidence check for membership inputs remains aggregate-only and does not authenticate individual source events. No Fiserv segment object, transition, retained price provenance, or production membership output was created. The closed evaluator source-map gate and matched #82 exact-source image remain required before use in an evaluator candidate.
