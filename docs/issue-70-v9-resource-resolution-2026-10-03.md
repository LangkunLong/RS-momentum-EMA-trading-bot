# Issue 70 v9 resource resolution package

**Owner recommendation:** retain the existing plain JSON comparison shape and reject the current v9 run under the existing budgets. Request one report-only, pre-stage resource census before anyone considers changing an operative cap. This is a concrete resource decision and a finite next action; it is not permission to read SEC archives, run the generator, change caps, or publish v9.

The prior resource package, accepted source/test work, and immutable design proposal `a47b3cac549223fef5e0412b66b036abeb5b42af` remain preserved. This package supersedes the *candidate preference* in the a47 proposal only: the new recommendation is plain JSON for compatibility. It does not rewrite the a47 artifact or change production code.

The prior resource package is commit `1e1dd2c152822bf3e149d9a3e3ff715e2a23e42a`, file SHA-256 `e6365373d1277a2e7d63e64d0455974d2ef4f0b376dad80c23d75075080892a3`. The a47 proposal is preserved at commit `a47b3cac549223fef5e0412b66b036abeb5b42af`, file SHA-256 `c222ed9d54c60aa6ff306b2b582646b907064607b22af09ec44d5f9cee6a5213`. Principal-owned PR126 merged as commit `78f5babdf51ea6cf757be51fc6746a91d66cba53`; this package does not duplicate or modify that PR.

## Four status dimensions

| Dimension | Current status |
|---|---|
| **Implementation** | Accepted source corrections and public-contract packaging remain intact. The production v9 writer remains unchanged and has no pre-allocation resource gate. This package adds only a standalone synthetic fixture check and this decision document. |
| **Required inputs** | The retained four-issuer sample, adopted calendar metadata, and 14-member identity receipt remain bounded research inputs. The accepted full #68 eligible universe, source/use-rights evidence, and production financial-history inputs are not available for acceptance. |
| **Acceptance evidence** | Accepted source evidence is reused without rerunning it. The new isolated fixture check passed. No SEC archive/member was read, no financial generator was run, no v9 output exists, and no financial policy replay occurred. All four original #70 criteria remain open. |
| **Dependencies** | Principal must decide whether to authorize the single report-only sizing operation below before any cap proposal or source operation. Full #68/#69 production inputs and acceptance remain separate requirements. The #82 evaluator-image gate is a separate project gate and does not supply financial history or change this resource result. |

The four original #70 acceptance criteria remain: (1) field and historical-lookback coverage by security/date; (2) supported forms/concepts, Q4 treatment, and missing inputs; (3) public availability versus period end and required prehistory; and (4) implemented and measured filing/fiscal policy. This resource decision does not close any of them.

## Consumer inventory and representation decision

A tracked-source search at the preserved a47 revision found these repository references:

| File | Role |
|---|---|
| `tools/generate_issue70_q4_source_sample.py` | Constructs the in-memory comparison, validates evidence caps, and writes `financial_coverage_summary.json` plus `financial_coverage_slots.csv.gz`. It does not read a published v9 summary. |
| `tools/issue70_v9_coverage.py` | Builds the in-memory comparison and serializes fixed-column coverage CSV. It does not read a published v9 summary. |
| `tests/test_issue70_v9_public_contract.py` | Public contract fixtures call the builder and check accepted behavior. |
| `tests/retained_issue70_v9_source_contract.py` | Retained source-contract checks call the builder and check accepted behavior. |
| `tools/assess_issue70_retained_source.py` | Reads the older `fundamentals_coverage.json`; it is not a consumer of `financial_coverage_summary.json` or its `v8_source_window_comparison` value. |
| Preparation/source-contract documents | Describe the accepted schema and its accounting rules; they are not runtime readers. |

No tracked production consumer of the v9 summary was found. This is a repository inventory only; it cannot establish whether downstream users, notebooks, or external integrations read the published artifact.

**Decision: keep the current plain JSON object at `v8_source_window_comparison`.** This preserves the accepted logical and on-disk value shape, avoids an envelope migration, and retains compatibility for unknown external readers. The a47 compressed envelope is lossless in the tiny fixture, but it changes the summary value shape and does not reduce logical records or canonical logical evidence bytes. Its physical savings on the real summary are unmeasured. The synthetic fixture's smaller encoded example is not a real-output compression estimate.

## Independently checked mandatory lower bounds

The checks below use the retained adopted calendar and four-row security master plus static source contracts. They do not read financial CSVs, import the financial helper, or touch an SEC archive.

### Slot and coverage-CSV floor

The adopted calendar has 1,508 evaluation sessions from 2020-01-01 through 2025-12-31. The retained sample security master marks A, AMZN, and MSFT eligible from 2021-01-01 and KDP from 2022-06-21 through 2025-12-31. Counting those dates against the retained calendar yields 4,652 eligible issuer-sessions inside the maximum 6,032-cell (4 × 1,508) grid.

The fixed non-Q4 expected slots per eligible issuer-session are 24:

```text
quarterly EPS growth       4
quarterly revenue growth   2
annual EPS growth          3 × 2 metrics = 6
annual EPS level           4 × 2 metrics = 8
annual revenue growth      3
annual ROE                  1
                            --
                            24
```

Therefore the minimum fixed expected-slot count is `4,652 × 24 = 111,648`. Q4 availability adds three slots per distinct annual period at each eligible session; those slots are excluded from this floor.

The builder also emits seven raw-field rows for every ticker and evaluation session before checking membership eligibility. Thus the field-session floor is `6,032 × 7 = 42,224`, including pre-membership rows. Together these require at least `111,648 + 42,224 = 153,872` coverage CSV rows before any source-origin rows, Q4 slots, or gap evidence.

`CSV_COLUMNS` contains 39 columns: 38 comma separators plus LF contribute 39 bytes per row. A lower-bound row width of 214 bytes counts only these guaranteed non-empty values: `field_session` (13), SHA-256 slot ID (64), minimum ticker (1), ten-digit CIK (10), `sample-cik:` lineage (21), ISO date (10), `eligible_sample_membership` (26), one-digit slot number (1), and `not_measured_no_policy_replay` (29), plus those 39 delimiters/LF. It deliberately omits `status`, feature/metric strings, and all other non-empty fields. Therefore:

```text
153,872 rows × 214 bytes = 32,928,608 uncompressed CSV bytes (minimum)
```

### Plain-summary and combined canonical-evidence floor

The current `_json_bytes` serializer is sorted, two-space-indented JSON with a trailing LF. A standalone one/two-slot fixture with the exact comparison-slot key structure independently reproduces a **1,055-byte minimum for one slot** and a **987-byte increment for each additional slot**. For `N ≥ 1`, the empty-value minimum is `1,055 + (N − 1) × 987`, equivalently `68 + N × 987`. The 64-byte empty-map wrapper is not the affine intercept for a nonempty map. The real financial summary has additional required fields, and real slot scalars/origin maps are non-empty, so this intentionally understates its size.

```text
1,055 bytes + (111,648 − 1) × 987 bytes
= 110,196,644 bytes minimum financial summary

110,196,644 summary bytes + 32,928,608 uncompressed CSV bytes
= 143,125,252 bytes minimum canonical logical evidence
```

An independent principal review of immutable source blob `0024e6c079ed1dd5a8391e7ca22473e355cd4f1b` confirmed the affine arithmetic as lower-bound-only. The review receipt is `C:/Projects/trading_bot/RS-momentum-EMA-trading-bot/.artifacts/coordination/offline-wave-2026-10-03/principal-static-70-lower-bound-review.json`, SHA-256 `6ce120671652f037c826436485eeaac7677422bed352ffd5b1583ae8ab9bcda8`. The Git LF source SHA-256 is `d63abc37cb702a1b5321e0017bda2db03d37f309f42ed67f2e3aebb7c082c103`; the lead's working-tree CRLF source SHA-256 is `a3bd7ee4da0fd5ae29c2e49f906feed7d55de2fb0879cbf81a8a3d2c6d27789a`. The principal independently confirmed they match after 1,662 CRLF-to-LF conversions; these hashes identify different byte representations. The receipt also reports the compact comparison floor as 71,789,712 bytes. The earlier resource package recorded 71,789,665 bytes, 47 bytes lower; that historical package remains immutable. This package uses the independently reproduced 71,789,712 compact bound and 110,196,644 pretty-summary bound. The principal review explicitly does not establish a complete upper bound or authorize a cap change/run.

### Three independent cap conflicts

| Measure | Independently checked lower bound | Current limit | Minimum shortfall |
|---|---:|---:|---:|
| Combined logical evidence records | 265,520 (retained static assessment; actual final count can be higher) | 250,000 | 15,520 records |
| Canonical logical evidence bytes | 143,125,252 | 134,217,728 (128 MiB) | 8,907,524 bytes |
| Physical publication bytes | 110,196,644 for the plain summary alone | 20,971,520 | 89,225,124 bytes |

The compact comparison floor and pretty-summary floor are distinct byte identities. The physical publication contains 19 files; the other 18 mandatory files only increase the physical total. The gzip CSV output length is unknown and does not reduce the uncompressed CSV contribution to canonical evidence accounting.

The 265,520 figure is a lower bound, not a padded estimate of the final count. The Q4 coverage loop adds three rows for every annual period at every eligible issuer-session; origin rows, per-origin comparison decisions, and annual-gap diagnostics also add records. No complete count is inferred from the lower bound.

The retained metadata count independently yields 4,652 eligible issuer-sessions, 6,032 grid cells, and seven fields per cell. The standalone fixture then checks the arithmetic from those fixed counts: 111,648 fixed slots, 42,224 field rows, 153,872 minimum CSV rows, 32,928,608 minimum uncompressed CSV bytes, compact one-slot/increment values 691/643, pretty one-slot/increment values 1,055/987, and the 143,125,252-byte canonical floor. The fixture hard-codes the retained metadata counts; it does not read or recount repository inputs.

## Accounting and static upper-bound limits

Keep the accepted accounting definitions exactly:

1. **Logical record count:** every coverage CSV record + every `by_slot_id` entry + every `window_candidates_by_origin_id` decision + every annual fiscal-year-gap diagnostic. Count repeated ID references and aggregate descriptors only as specified by the accepted plan; the envelope is never one substitute record.
2. **Canonical logical evidence bytes:** uncompressed coverage CSV bytes + `_json_bytes` of the full logical `financial_coverage_summary.json` exactly once, with the complete plain comparison in its existing key.
3. **Physical publication bytes:** exact on-disk byte sizes of all 19 staged files, including the actual plain summary, gzip CSV, copied calendar/input evidence, and `fundamentals_publication.json` marker.

The current v9 writer's 19-file plan is: `security_master.csv`, `fundamentals.csv`, `fundamentals_audit.csv`, `fundamentals_coverage.json`, `sample_q4_findings.json`, `generation_reconciliation.json`, `fundamentals_provenance.json`, `financial_coverage_summary.json`, `financial_coverage_slots.csv.gz`, `exchange_sessions.csv`, `calendar_provenance.json`, `candidate-publication.json`, `principal-adoption-decision.json`, `membership.csv`, `security_names.csv`, `spy_trading_days.csv`, `identity_manifest.csv`, `source_archive_attestation.json`, and `fundamentals_publication.json`.

The retained selected-member metadata distinguishes **21,457,031 unique expanded bytes across 14 identities** from the historical **22,120,271 inclusive preflight receipt**. The latter includes a repeated Submissions pass. Both are retained historical evidence, not measurements of a new invocation. Existing guards remain 64 MiB/member and 512 MiB combined selected-member expansion; other source guards remain 10,000 paired financial/audit rows, 11,000,000 bytes for legacy fundamentals CSV, and 148,000,000 bytes for the alternate audit input. These input limits do not bound Python object expansion or output size.

A deliberately coarse shape bound illustrates why those guards do not produce a practical output cap: 10,000 paired audit rows × seven normalized source fields permits up to 70,000 normalized origins; annual-period count is no greater than that origin count. Under the 1,508-session per-ticker limit, the Q4 loop could add up to `3 × 70,000 × 1,508 = 316,680,000` expected slots for one ticker. Adding at most `24 × 6,032 = 144,768` fixed slots gives at most 316,824,768 comparison entries under this loose bound. Even bounding each slot's candidates by 70,000 normalized origins plus four current Q4 basis references gives a Cartesian comparison-decision ceiling of `316,824,768 × 70,004 = 22,179,001,059,072` decisions.

That last number is a theoretical overbound, not a plausible estimate or a proposed cap. It shows that current row/member guards plus static code do not provide a useful complete upper bound. Exact annual-period multiplicity, the actual candidate set per frozen slot, repeated origin/basis emission, string lengths, exact gzip length, Python allocator/object overhead, and total summary size are data-dependent. End-to-end runtime and peak RSS/staging disk cannot be proven from retained metadata alone.

## Resource-gate placement and publication recovery

Static source inspection finds that current enforcement is late:

- Selected financial/audit row limits are checked after `sec.extract_fundamentals` has returned its lists.
- `_build_v9_coverage_records` appends a row before checking the record cap; annual Q4 rows iterate every annual period for every eligible session.
- `serialize_v9_coverage_records` builds a full `StringIO` CSV, encodes a canonical byte string, then checks the uncompressed-byte cap before producing the gzip byte string.
- `build_v8_source_window_comparison` constructs the complete slot map before `_validate_combined_v9_evidence_caps` checks combined record/byte caps.
- Staging begins only after those checks, but the total-output and internal 600-second checks run after all staged files and the publication marker are written. They do not cap peak RSS or staging disk.

Before any future materialization/publication, the accepted implementation should add these gates in order:

1. **Static floor gate before SEC access:** verify the exact adopted calendar/security metadata and reject if mandatory slot, row, canonical-byte, or physical-summary floors exceed approved caps. With today's values, this gate rejects before any archive/member read.
2. **Input identity and row-count gate:** verify the exact 14 hash-bound member identities and their size budgets; count paired financial/audit rows while parsing, before appending beyond the accepted row cap. Do not allocate a full row list and then discover it exceeded the cap.
3. **Compact origin-shape pass:** count distinct normalized origins, annual periods by ticker, expected slots by session, Q4 basis references, per-slot candidate decisions, and annual-gap diagnostics without retaining duplicate CSV/detail objects. Abort before constructing `records`, `by_slot_id`, or full JSON byte buffers if the conservative count bound cannot fit.
4. **Exact byte preflight:** stream the existing CSV and summary serializers into counting/hash sinks with the accepted encoding/ordering rules. Include the complete plain comparison and marker inputs. Abort before staging if any byte budget fails; do not build whole encoded CSV/JSON/gzip buffers merely to learn their size.
5. **Publication gate:** only after all exact resource gates pass, take exclusive ownership of an absent destination and create one unique sibling stage. Write with exclusive-create semantics, account for all 19 file lengths and manifest bytes, verify all hashes/input stability, then perform a no-replace atomic publication. No sidecar or cap exemption is permitted.

This is a resource-gate design requirement, not an implementation in this package. The current production cap values remain unchanged.

For a future authorized publication, the single invocation owner must hold an exclusive run lock and use the exact destination `docs/issue-70-q4-source-sample-v9`; fail if it exists or is a symlink. Each stage needs an invocation token bound to source revision, destination, and process/job identity. Normal exceptions may remove only that invocation's stage after verifying the token. A forced interrupt may leave an orphan stage; the owner must stop, confirm the job has exited, and report/quarantine that exact path for principal review. Do not auto-delete/reuse unknown stale stages or recursively clean a parent. Publication is one atomic no-replace step after complete hash manifest creation; before that step the final destination remains absent.

The current implementation uses a UUID sibling stage, exclusive file creation, a full hash marker, and exception cleanup, but a forced process termination can bypass `except Exception`; no durable stage-owner/recovery marker is established before expensive work. This gap is part of the future gate/recovery review. This package created no stage or destination.

## One finite owner decision requested

No useful complete cap ceilings can be chosen from the retained lower bounds. Raising caps to the floors alone would still not prove that the full evidence fits. I request that the principal authorize or decline **one no-publication resource census** before any production cap or representation decision.

If authorized, the operation must be a separate report-only census path, not the full generator CLI. It will reuse only the already accepted four-issuer sample, adopted calendar, and exact 14 hash-bound SEC member identities. It will produce exact record/slot/decision/gap counts and exact logical canonical/physical serialized byte counts through count/hash sinks, but no financial output files, no staging directory, and no final publication. It will not use Norgate, make a network call, or change the v9 schema.

### Proposed hard abort ceilings for that census

| Resource | One-operation ceiling | Derivation and meaning |
|---|---:|---|
| Selected-member unique bytes | 21,457,031 bytes across the exact 14 identities | Handoff-reported unique expanded-size total; all namespace/name/SHA/length identities must match the retained V8 manifest. |
| Selected-member cumulative expansion | 22,120,271 bytes | Historical inclusive receipt including the repeated Submissions preflight pass; any larger or different read total aborts this census. This is not re-labelled as unique bytes. |
| Per-member expansion | 67,108,864 bytes | Existing 64 MiB/member guard, unchanged. |
| Wall time | 600 seconds, externally enforced | Preserves the existing ten-minute limit; kill the entire child job at timeout. The generator's late internal timer is not the supervisor. |
| Job-wide committed memory | 4 GiB (4,294,967,296 bytes), hard Windows Job Object limit | Proposed fail-fast limit = 8 × the unchanged 512 MiB aggregate selected-member expansion ceiling. Enforce `JOB_OBJECT_LIMIT_JOB_MEMORY` across Python and child processes. This is a committed-memory abort threshold, **not** an RSS limit or a proven successful-run memory bound. |
| Sampled RSS/working set | 3 GiB optional early-abort threshold, sampled every 50 ms | This is a separate observed working-set/RSS threshold, not a reserve derived from the 4 GiB committed-memory limit. Polling may miss a transient peak and cannot enforce a hard RSS maximum; record the largest observed working set and mark any threshold overshoot as incomplete. |
| Active child processes | 2 | Static code has one Python process and two sequential `git` subprocess calls, so at most Python + one `git` child are active. The external supervisor is outside the job. |
| Financial staging/output files | 0 bytes | Census uses counting/hash sinks and never creates `.tmp` staging or the V9 destination. |
| Census receipt | 65,536 bytes (64 KiB) maximum | The only permitted file written by the measurement operation; contains counts, byte lengths, hashes, sampled peak working set/RSS, duration, process peak, stop reason, and input identities, not source values. |
| Count-only canonical evidence traversal | 536,870,912 bytes (512 MiB) | A separate census work ceiling equal to the existing aggregate selected-member expansion cap. It limits report-only serialization counting; it does not change the 128 MiB production evidence cap. If exceeded, stop and report an incomplete lower bound. |

These ceilings bound a proposed measurement attempt, not successful output. Host available memory is unmeasured; if the host cannot safely enforce a 4 GiB job-wide committed-memory limit, the census must not start and the principal must choose a smaller budget or decline it. The 3 GiB RSS/working-set poll is an independent observation/early-abort signal, not a hard RSS guarantee or a committed-memory reserve. If the census reaches any ceiling, it writes no financial output and reports only a partial/aborted observation. There is no silent fallback or automatic second run.

### Census verification and stop behavior

Before dispatch, an independent reviewer must approve the separate report-only code and verify that it cannot call the publication path. That implementation revision does not exist yet; this document and its fixture are design evidence only, not an executable approved census. At runtime, verify the exact census source revision, clean worktree, destination absent/no-symlink, adopted calendar and manifest identities, and 14 member identities. Assign the Python process and all children to a Windows Job Object with a 4 GiB **job-wide committed-memory** limit, active-process limit 2, and kill-on-close; separately sample working set/RSS and label it observed, not guaranteed. Use an external 600-second watchdog. Count selected decompressed bytes and abort at 22,120,271. Keep the existing 64 MiB per-member guard.

The census must not call the unchanged production builder, then serialize its complete retained objects to a counting sink. Counting sinks only bound emitted byte copies; they do not bound origin indexes, retained rows, comparison maps, sort buffers, or cached per-session projections. Instead, prepare an independent streaming enumerator that traverses compact, bounded source indexes, computes a slot/decision before retaining its record objects, reserves a conservative byte budget before each retained allocation, emits canonical CSV/JSON chunks to counting/hash sinks, and releases scratch reservations as each issuer-session is completed. Reservation estimates must account for the exact Python version, nested object/container overhead, live string payloads, sorting structures, and serializer buffers. A 4 GiB job-wide committed-memory limit is the hard fallback; if the reservation model is unavailable or exhausted, abort before retaining the next item. No full `records`, `by_slot_id`, compressed CSV, or summary byte buffer may be built on the census path.

The census must independently count the exact logical record formula and compare its enumerated logical outputs to the unchanged production builder on bounded synthetic fixtures before any archive-reading request. Equivalence coverage must include every ordered CSV row and fixed column, every expected-slot ID/value, every source-origin and Q4-basis row, each `window_candidates_by_origin_id` decision, every annual-gap diagnostic, `complete_comparison_sha256`, `canonical_detail_byte_length`, summary canonical/hash semantics, and compressed/uncompressed coverage digests. Include fixtures for empty/missing data, duplicate candidates, vintage changes, direct and derived Q4, four-basis date gating, skipped fiscal years, and prehistory. This is a future independent acceptance suite; the current toy envelope/preflight fixture does not establish production equivalence or enforcement.

The physical census must freeze the exact publication plan and source revision, input paths/identities, Python/zlib versions, timestamp format, 19 output names, and all copied-file identities. It must count each full serializer plus `fundamentals_publication.json`; if a run-specific value or copied input is not frozen or bounded, mark physical bytes `unknown` rather than treating the census receipt as a future publication bound. It may use fixed-width hash/timestamp fields only where the current serialization proves their exact width. The report must say whether each cap is `below`, `exceeded`, or `unknown`; any measurement stop is `incomplete`, never `passed`.

Since staging is zero, recovery after timeout/cancel has no data-stage cleanup: the supervisor verifies that the child job is empty, records the exit/stop state in the ≤64 KiB receipt, and leaves the final destination untouched. The single census owner controls the exact receipt path. Do not resume a partial count or automatically repeat the measurement; a new attempt needs a new principal decision.

### Cap decision after the census

The only defensible numerical minimums before census are **floors**:

- logical record ceiling must be at least 265,520, and the final count may be much higher;
- canonical evidence ceiling must exceed 143,125,252 bytes;
- physical publication ceiling must exceed 110,196,644 bytes for the summary alone, plus the other 18 files.

These are not safe requested production ceilings. The census result must establish exact totals and a deterministic bound for the exact accepted source identities. The principal can then approve a separate cap change or keep generation disabled. No cap value is changed by this package.

## Fixture-only evidence

`docs/issue-70-v9-resource-fixture-check.py` is a standalone file with no production imports and no repository/data/archive reads. It exercises a tiny synthetic summary only:

- plain and a47-style envelope decode to equal logical comparison values;
- the accepted record formula counts the decoded comparison identically (11 records in the toy fixture);
- canonical logical evidence bytes use the decoded plain summary, while physical storage lengths refer to actual stored forms;
- an over-budget fixture plan is rejected before the fixture's materialize/stage callbacks;
- the exact nested empty-slot shape is 1,055 pretty bytes for one slot and adds 987 bytes per additional slot; compact values are 691 and 643;
- arithmetic from the retained metadata constants yields 153,872 minimum CSV rows, 32,928,608 minimum uncompressed CSV bytes, 110,196,644 minimum plain-summary bytes, and 143,125,252 minimum canonical logical-evidence bytes.

Command run: `python -B docs/issue-70-v9-resource-fixture-check.py` — exit 0. The toy envelope serialized to 1,090 bytes versus 2,473 bytes for the toy plain summary; this tiny illustrative difference is not extrapolated to production compression or feasibility. No production code or settings are imported or modified.

## Exact principal choice

Please choose one:

1. **Authorize** one report-only sizing census under the exact one-operation ceilings above. It requires a separate exact authorization for the finite selected-member read and the separate census code review; it does not authorize a full generator run or cap change.
2. **Decline** the census. Keep the accepted schema and all existing caps; the static preflight must fail before archive access and no v9 run can proceed.

Either choice preserves source acceptance, the existing a47 proposal, all four #70 acceptance criteria, and the Norgate/evaluator work owned by their existing leads. No source acquisition, fixture campaign, compression benchmark, archive operation, cap change, production representation change, or publication was performed for this package.
