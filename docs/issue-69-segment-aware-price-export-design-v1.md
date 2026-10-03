# Issue 69 segment-aware price exporter design v1

Date: 2026-10-02

## Decision

Prepare an opt-in exporter path that consumes the already validated #68
`PriceIdentityTransitionContract` segment view. Keep the current legacy exporter
path byte-for-byte unchanged when no segment contract is supplied. Do not infer
segments from `pit_price_identity_map.csv`, add request symbols, modify the
607-key request contract, or admit the candidate FISV dates to production.

This document records design and bounded implementation decisions. It is not
an accepted production segment manifest or admission decision. The design was
committed before its follow-up exporter and shared-core changes.

## Baseline and intended behavior

| Area | Current behavior | Required segment-aware behavior |
| --- | --- | --- |
| Request identities | `export_pit_prices.py` loads one `PriceIdentity` per canonical ticker and builds `price_identity_request_contracts` in `export()`. | Keep the same key set and parent digest. A segment object is a separately hashed child of that parent. |
| Provider rows | The backfill asks for the existing membership symbols; rows are normalized to canonical ticker names. | For segmented lineages, admit only the provider symbol resolved for that lineage and date. Keep ordinary lineages on the existing path. |
| Cache rows | `_clip_cache_to_admitted_identities` checks the broad parent interval. | Apply the segment path after parent clipping so an inactive reused ticker episode cannot be mistaken for active symbol coverage. |
| Warm-up | `_build_price_identity_warmup` follows one `warmup_predecessor` per ticker and copies predecessor history to the successor label. | Traverse segment edges, including both FISV→FI and FI→FISV. Preserve copied bars only as warm-up input for the successor series; do not count them as admitted observations for that symbol/date. |
| Published validation | `_validate_published_price_identity_continuity` audits legacy ticker predecessor overlaps. | Audit provider rows against active segments and separately audit segment-edge warm-up rows. Exact price overlap remains a price-series check; it does not authenticate identity or replace source assertions. |
| Contract validation | `PITDataBundle.load_price_identity_transition_contract()` verifies bundle-bound provenance and invokes the segment parser. The parser is private and is not an exporter input loader. | Reuse the #68 validation rules. Do not duplicate the parser in the exporter. A narrowly scoped shared-core loader API needs lead review before any shared-core edit. |

Relevant implementation areas are the exporter request construction, source
projection, warm-up and publication checks; segment validation and resolution
in `core/pit_data.py`; and V3 date-to-symbol resolution in
`core/pit_universe_v3.py`.

## Proposed input boundary

The exporter should accept a validated segment contract object, not a raw
dictionary or a caller-supplied map of dates to symbols. Its fields are the
existing #68 objects: `segments`, `segment_transitions`, the parent request
contract digest, the segment digest, and verified source assertions. The
segment parent digest must equal the digest of the exact request-contract bytes
the exporter is already preparing.

The bundle validator is attached to a loaded bundle and verifies that bundle's
real `prices_provenance_sha256`. The exporter runs before a bundle exists, so it
must not invent that digest or construct a full
`PriceIdentityTransitionContract` with a placeholder. Lead reviewed and
authorized this narrow shared-core API:

```python
@dataclass(frozen=True)
class ValidatedPriceIdentitySegmentInput:
    parent_request_contracts_sha256: str
    segment_contract_sha256: str
    segments: Mapping[str, PriceIdentitySegment]
    transitions: tuple[PriceIdentitySegmentTransition, ...]
    source_assertions: Mapping[str, PriceIdentitySourceAssertion]
    source_evidence_root: Path

def validate_price_identity_segments_v1(
    raw_contract: object,
    *,
    declared_sha256: object,
    parent_request_contracts: Mapping[str, Mapping[str, object]],
    identities: Mapping[str, Mapping[str, object]],
    source_evidence_root: Path,
    data_cutoff: date,
    ticker_transitions: tuple[IdentityTransition, ...] = (),
) -> ValidatedPriceIdentitySegmentInput:
    ...
```

The function computes the canonical digest from `parent_request_contracts`,
requires its rows to equal `identities`, then delegates all segment, evidence,
graph, and date validation to `_parse_price_identity_segments_v1`. It returns
the parent digest, declared segment digest, and parsed segment state. It has no
`prices_provenance_sha256` field because no final provenance file exists yet.
The existing bundle loader keeps its current full-provenance digest checks and
semantics; the new function is an ingestion result for the pre-bundle exporter,
not a substitute bundle identity contract.

For this increment, `main()` exposes no segment CLI flags. A direct `export()`
composition accepts a segment sidecar only when its caller explicitly sets
`allow_nonproduction_price_identity_segment_fixture=True`; the output records
`price_identity_segments_admission_status: nonproduction_fixture` and
`price_identity_segments_source_use_status:
source_bytes_hash_verified_rights_not_adjudicated`. This is fixture plumbing,
not a production admission path. Do not add a production CLI option until
source-use rights and the production admission receipt are separately reviewed.

Keep source assertion paths valid relative to the final `prices_provenance.json`
directory, as required by the existing loader. In fixture mode the staged
evidence root is the existing export directory; tests reload the published
segment object from that exact root. Do not emit provenance whose assertions
point outside that root.

## Export composition

1. Build the current parent request identities and canonical parent digest
   exactly as today. Reject the segment input if its parent digest differs.
2. Validate the segment object and each retained assertion through the shared
   contract parser. Keep the segment object and its canonical digest in the
   published prices provenance without changing the parent request keys.
3. After mapping provider aliases to canonical ticker labels, project provider
   cache and SIP rows through the resolver. For a ticker belonging to a
   segmented chain, retain its provider observation only when that ticker is
   the active symbol for the chain on that date. Discard inactive duplicate
   provider-symbol observations and report active counts by segment plus
   inactive and out-of-window counts. Keep unsegmented rows unchanged.
4. Use segment transition edges for warm-up. Copy predecessor history under the
   successor ticker only for dates before that successor segment's admission
   date. Track copied rows separately from active provider rows in the
   in-memory audit. Repeated ticker names require internal segment IDs during
   this step; grouping only by ticker would conflate the first and third FISV
   episodes. Do not require a shared provider date at a handoff: absence of
   overlap is not a continuity finding.
5. Merge cache and SIP data using the existing source-priority rule, then check
   that each active-date row has the resolved ticker and every pre-admission
   row is accounted for as segment-edge warm-up. Preserve the public
   `prices.csv` columns and the V3 membership schema.
6. Record the validated segment digest, parent digest, active-row counts,
   discarded inactive and out-of-window row counts, per-edge warm-up counts,
   and exact-overlap audit in `prices_provenance.json`. Leave the current legacy audit fields
   intact for nonsegmented identities.

The helper should operate on already parsed CSVs and validated contract
objects. It should not fetch prices, request credentials, rewrite identity
history, or decide whether an issuer/security link is true.

## Synthetic fixture and bounded tests

`tests/test_pit_identity_segments.py::_bundle_with_segments` constructs the
candidate three-segment shape with generated bytes explicitly labeled
“Synthetic test fixture; not source evidence.” The existing
`test_fiserv_segments_resolve_both_effective_dated_handoffs_and_v3_ticker`
checks the 2023-06-07 FISV→FI and 2025-11-11 FI→FISV resolver boundaries. It
passed in this worktree on 2026-10-02 (`python -m pytest -q
tests/test_pit_identity_segments.py::test_fiserv_segments_resolve_both_effective_dated_handoffs_and_v3_ticker`).
Those test bytes validate shape only; they do not authenticate the Fiserv
filings or grant production admission.

The exporter fixture tests in `tests/test_export_pit_prices.py` use the same
explicit synthetic status and assert:

1. The resolved provider symbol is unique before, on, and after each handoff;
   inactive duplicate `FISV`/`FI` provider rows are excluded from active-row
   counts.
2. A normal unsegmented ticker passes through without a row change.
3. Warm-up copies follow each dated segment edge, remain under the successor
   ticker only before its admitted start, and never become active-row evidence.
4. Conflicting preexisting cache values still follow the current
   cache-preference and published-row audit rules; segment warm-up cannot hide
   a cache/provider conflict.
5. The parent request-contract keys and digest are identical with and without
   the optional segment object; no new request key is created.
6. Wrong parent/digest, missing or mismatched source bytes, and ambiguous
   symbol-to-lineage binding fail closed. Synthetic fixture files stay in the
   test's temporary root and are never passed as production inputs.
7. Legacy exporter fixtures without a segment object retain their current
   output and audit structure.

Do not test by calling a provider or loading credentials. The original
resolver-only test passed before this implementation. The exporter fixture
composition is explicitly nonproduction and uses mocked cache/provider
boundaries with synthetic source bytes.

## Bounded file and review plan

| File | Scope |
| --- | --- |
| `export_pit_prices.py` | Add programmatic nonproduction fixture composition, segment-aware source projection, edge-based warm-up, and provenance audit. Keep segment CLI flags absent and the no-segment path unchanged. |
| `tests/test_export_pit_prices.py` | Exercise fixture composition, parent/request digest stability, warm-up, conflict rejection, staged source-byte paths, and legacy composition. |
| `core/pit_data.py` | Add only `ValidatedPriceIdentitySegmentInput` and `validate_price_identity_segments_v1`, delegating to the existing parser. Keep `PITDataBundle.load_price_identity_transition_contract()` checks and results unchanged. |
| `config/pit_price_identity_map.csv` and retained production provenance | No change. Do not convert the overlapping FI/FISV rows into admitted segments. |
| `core/pit_universe_v3.py`, V3 membership schema, and the 607 request keys | No change. |

## Gate

Keep production admission closed until the real segment object binds the exact
unchanged parent request digest, each assertion has retained and hash-matching
source bytes and an accepted source-use basis, and the row/segment mapping is
reviewed. Synthetic fixture files remain isolated under test temporary roots
and are never production inputs. No credentialed provider request or
price-bundle publication is part of this work.
