# Price identity transition chronology review

Status: evidence review and fixture proposal only. No shared reader, provenance input, identity map, or price export was modified. This note does not emit a `price_identity_transitions` list or mark issue #68 accepted.

## Evidence-backed Fiserv chronology

The retained V3 request contract has 607 identity keys and its declared canonical digest is `273727c248f57b7376b6cf269312325cdd059287e1f4ed16ab5ce63617ceabc7`. The tracked identity map is `config/pit_price_identity_map.csv`; its FISV row spans 2020-01-01 through 2025-12-31, while its FI row spans 2023-06-07 through 2025-11-10. Those two admitted ranges overlap from 2023-06-07 through 2025-11-10.

Primary issuer and SEC records establish two separate listing changes for Fiserv common stock:

| Effective date | Ticker | Evidence |
| --- | --- | --- |
| Through 2023-06-06 | FISV, Nasdaq | Fiserv's [May 25, 2023 Form 8-K Exhibit 99.1](https://www.sec.gov/Archives/edgar/data/798354/000119312523154199/d470900dex991.htm), paragraphs 2–3, identifies Fiserv common stock as Nasdaq: FISV and says it will begin NYSE trading under FI on June 7. |
| 2023-06-07 through 2025-11-10 | FI, NYSE | Same 2023 notice; the later SEC filing confirms NYSE trading ceases at the close on/about November 10, 2025. |
| Starting 2025-11-11 | FISV, Nasdaq | Fiserv's [October 29, 2025 issuer release](https://investors.fiserv.com/news-releases/news-release-details/fiserv-announces-transfer-stock-exchange-listing-nasdaq), ticker-change paragraph, says its Class A common stock will trade on Nasdaq under its original FISV symbol beginning November 11. Fiserv's [November 10, 2025 Form 8-A](https://www.sec.gov/Archives/edgar/data/798354/000119312525274207/d53898d8a12b.htm), Explanatory Note, confirms the same common stock and the NYSE close/Nasdaq open dates. |

Accordingly, FISV→FI effective 2023-06-07 and FI→FISV effective 2025-11-11 are both same-security ticker transitions. The retained one-row-per-ticker map does not represent these three dated episodes: it overlaps FISV with FI and gives the FISV symbol a single continuous interval. Truncating FISV at 2023-06-06 would remove its 2025 reappearance. Extending FI past 2025-11-10 or treating all FISV dates as one uninterrupted ticker identity would also contradict the primary listing notices. The existing price export review reports rows for both symbols across 2020–2025, but that fact does not repair the identity contract or prove interval coverage on its own.

## Contract and reader gap

`PriceIdentityTransitionContract` and the V3 normalizer identify nodes by ticker string. The transition shape contains `predecessor` and `successor` ticker strings but no identity-segment identifier. A valid dated chain here repeats the ticker symbol after two transitions, so it cannot be faithfully represented by truncating or extending the existing two identity rows. The current reader also validates successor admission at the edge but does not require the predecessor interval to end before the edge; the normalizer's authenticated transition-exit exception can bypass predecessor bounds. The source contract does not permit this overlap.

Preferred contract design for a coordinated follow-up: assign each continuous symbol episode a stable, unique identity-segment ID, and make transitions reference those IDs. Keep `provider_symbol` as a separate field, allowing the same ticker to appear in more than one non-overlapping segment. A Fiserv example would have three nodes—FISV through 2023-06-06, FI from 2023-06-07 through 2025-11-10, and FISV from 2025-11-11—connected by the two effective-dated transitions. The implementation must preserve the current hash binding to the request contract and define how an open holding resolves when the displayed ticker is reused. Until the schema and resolver support this, quarantine the Fiserv transition chain from accepted continuity joins and fail closed for this chain; do not emit either edge as an integrated transition.

Before integration, the validator should at minimum enforce `predecessor.admitted_start <= predecessor.admitted_end < effective_date` and `successor.admitted_start <= effective_date <= successor.admitted_end`. A continuous transition must additionally prove the predecessor and successor coverage is adjacent on the relevant exchange sessions, or record a source-supported gap. Membership-affiliation continuity remains a separate validation from price identity chronology.

## Proposed fixtures for a coordinated reader change

These are specifications only; no test files were added or run.

1. Accept a single rename when the predecessor ends before the effective date, the successor is admitted on that date, the edge metadata matches both identities, and the relevant exchange-session boundary is adjacent.
2. Reject a transition when the predecessor's `admitted_end` is on or after the effective date, even if the successor is admitted on that date.
3. Reject a transition when the predecessor's `admitted_start` is after the transition date.
4. Reject a membership removal outside the predecessor identity bounds even when a matching successor addition exists on the effective date.
5. Represent FISV→FI→FISV as three unique segment IDs with non-overlapping date ranges; verify both effective-date edges and verify that ticker-symbol reuse does not create a graph cycle or ambiguous holding resolution.
6. Reject a two-row FISV/FI representation that truncates the first FISV interval and therefore loses the 2025-11-11 FISV segment.

Acceptance remains blocked for the Fiserv transition chain until the identity-segment schema, the retained request-contract mapping, and resolver semantics are jointly reviewed and the existing price export is reconciled against the dated episodes. No historical index membership rows were acquired or added in this review.
