# Issue 70 expected-quarter-slot independent review

Review date: 2026-10-01. Frozen owner revision **d0bb52f94ae68aa26fd90e8c0a295456850516c2**, clean worktree `C:/Users/llong/.codex/worktrees/f1ba/RS-momentum-EMA-trading-bot`, atop `8f389406f1c7339662387d446cbbffd997b810e8`; original bounded base `ab385d792e19ff6db39d87f1123f47f660fc1e1d`.

## Verdict

**Accept the expected-quarter-slot correction and the agreed bounded two-snapshot/three-issuer assessment for integration, with the documentation qualifications below. Principal F1 is resolved. Full #70 data-delivery acceptance remains NOT READY; keep the issue open.**

This is the final delta review against the retained plan and principal finding, together with the original issue-body acceptance mapping. It preserves the prior reviews and receipts. The previous PASS at 8f389406 was superseded for full-window readiness; its independently verified scalar-origin correction remains valid. This review does not reclassify that previous readiness result as correct.

The lead must preserve its reproduction clarification and explicitly document the inherited archive-reuse wording conflict. These are report corrections; preserve measured v2 receipt bytes. No additional source measurement is required for those editorial corrections. Typed missing reasons and per-slot dates are retained for A, AMZN and MSFT only, while aggregate missing counts are derivable from the fixed denominators and histograms. This is adequate for the agreed bounded scope, not complete per-security production evidence.

## Source and execution identity

Independently verified the frozen source, receipt bytes, Git blobs and arithmetic in `issue70-quarter-slot-review-identity-counts.json` alongside this report.

| Artifact | SHA-256 of reviewed owner checkout bytes |
| --- | --- |
| Script `tools/assess_issue70_retained_source.py` | `290155b6ea16c330ad00785501610a637d151cd2c1637e2fe7a80960484b165d` |
| Tests `tests/test_assess_issue70_retained_source.py` | `ee0823a50061083694eb1844935595bcc1bdf368f7a107838ffabbd24192260d` |
| Report | `79720870cc265ddb755741ce6fcb97b4756badd63863c2646eaac771a56e9b73` |
| Original receipt, unchanged | `1fc74971cee7c30430ea8efbecdf615f3d8c113849a41c66ec52445d0e3c5733` |
| Strict scalar corrected receipt, unchanged | `7e690f3a6e0dd116f5a0b20267fcf6e8eebdd37ba9271eafbb8675c62819c5a8` |
| New quarterly-lookback-v2 receipt, 34,505 bytes | `86b6f4c152b2ebbaf090e6dee9a75abc231751b3b402cbf44f99614b64f3ba71` |

The v2 receipt truthfully records generation at `2026-10-01T20:01:29.028817+00:00` while HEAD was 8f389406, together with the final executed script hash. Its execution is not falsely attributed to the later packaging commit d0bb52f. Original and strict scalar corrected receipts are unchanged both as owner checkout bytes and as Git blobs.

The accepted helper module Git blob is `431f95230ca18438cfce3e442e83ecc7c87c2d56`; fiscal matcher `14c3c994f70a06f7897288a0d397672dbf897d89`; V3 calculator `71e562ffc00dc4e617f1cd625e3f13ea5fe3f81a`. These agree with the receipt and are unchanged from ab385d7. No accepted calculator, exporter, bundle builder or #67 measurement was modified.

All nine input byte sizes and expected/actual digest fields match the strict scalar corrected receipt. This review did not independently rehash the input CSVs or ZIPs. Inspection of the actual revision path confirms non-ZIP inputs are rehashed and checked against the committed prior receipt; archive digests are reused explicitly. The source trace is bound to the previous receipt and canonical JSON digest `2f961a64067cf290bfaf36319c496be4941fdafc9441d025a43e760e323170e6`, independently recomputed here. Its 1,946 unique qualifying scalar matches and six member identities are reused, not newly executed.

Runtime qualification: unlike the prior corrected receipt, the v2 receipt omits a runtime object. The prior receipt and an independent current probe from the same frozen worker agree on Python 3.13.14, pandas 3.0.1, Windows-11-10.0.26200-SP0. The probe records the Python executable. Actual owner command events show the `python` invocation, but do not print measurement-time dependency versions. Thus current and prior runtime identity is corroborated; contemporaneous v2 version capture is unavailable and must not be claimed.

## Principal F1 and accepted semantics

The correction calls the existing accepted `core.pit_coverage._quarterly_eps_full_window` using four EPS slots and two revenue slots. It does not replace an observed-count label while retaining the old computation.

Quarterly series retain represented nulls and tied-value ambiguity. The accepted helper preserves missing intervening quarters, annual-anchored unavailable terminal quarters, short-cadence placeholders, absent prior-year comparators and invalid growth denominators. The 28-day fiscal YoY matcher and inclusive 84–105-day endpoint cadence remain unchanged.

Annual anchoring is selected from the latest as-of-visible annual fiscal period containing any supported observed scalar, independently of whether the current target metric is present. Future publications are excluded before anchor selection. A wholly empty annual row does not establish an anchor. Annual amounts are never used as quarterly values. An already represented annual-end quarterly period is not duplicated. Missing-source members remain in the member/slot denominator.

Annual adjacent-available modeling, ROE input availability, original audit/export joins and source timing evidence remain under their earlier receipt definitions. V3 acceleration candidate availability remains a separate diagnostic; full-window readiness does not imply acceleration, a score or policy consumption. The new receipt recalculates only the three quarterly profiles and transparently reuses non-quarterly measurements.

## Measured correction and conservation

All aggregate histogram member totals equal 505 or 503; histogram-weighted matched slots equal the reported matched counts; the full-slot histogram bucket equals the ready numerator. Observed-period counts and histograms exactly match the prior receipt. Missing totals below are independently derived, not a new source scan.

| Snapshot/profile | Prior observed-period ready label | Correct expected-window ready | Matched + missing / required slots |
| --- | ---: | ---: | ---: |
| 2021-01-04 basic EPS | 366/505 | 0/505 | 1238 + 782 = 2020 |
| 2021-01-04 diluted EPS | 366/505 | 0/505 | 1237 + 783 = 2020 |
| 2021-01-04 revenue | 426/505 | 364/505 | 792 + 218 = 1010 |
| 2025-12-31 basic EPS | 394/503 | 0/503 | 1358 + 654 = 2012 |
| 2025-12-31 diluted EPS | 394/503 | 0/503 | 1361 + 651 = 2012 |
| 2025-12-31 revenue | 463/503 | 395/503 | 859 + 147 = 1006 |

Retained A now has EPS 3/4 and revenue 1/2 at both snapshots, with a typed missing-fiscal-quarter reason. The 2021 anchor is 2020-10-31, public 2020-12-21; the 2025 anchor is 2025-10-31, public 2025-12-22. Its first required slot is explicitly missing, followed by actual July/April/January periods for EPS. AMZN and MSFT sample dates/reasons are also retained. Every sample's matched count plus reason count conserves its required slots. No assertion is made that missing-reason types were retained for all 505/503 members.

## Independent tests and actual owner execution

Ran only the targeted frozen assessment tests:

```powershell
$env:AGENT_LOOP_TEST_TMP_ROOT='C:/Users/llong/.codex/worktrees/a9c1/RS-momentum-EMA-trading-bot/.artifacts/coordination/issue70-review-test-files'
python -B -m pytest tests/test_assess_issue70_retained_source.py -q -p no:cacheprovider --no-cov
```

**18 passed, 2 warnings in 1.73 seconds, exit 0.** The temporary root was checked to stay inside the lead coordination directory. Warnings are the disabled-cache configuration and existing websockets deprecation. The 15 strict scalar tests remain unchanged. Three new tests execute actual `analyze_export`: missing intervening quarter, missing terminal quarter with an ineligible future annual anchor, and coherent complete history. Negative cases retain EPS 3/4 and revenue 1/2 despite enough older observed pairs; positive history gives EPS 4/4 and revenue 2/2. Both EPS fields are checked. No broad suite, archive hash or old report rerun was performed.

Read the retained `issue70-quarter-slot-owner-command-outputs.json` in full. Two actual narrow-addendum CLI executions completed successfully, reporting nine verified inputs and 145,010 export / 145,010 audit rows across both snapshots. The final owner focused test execution reports 18 passed in 1.74 seconds. These are actual command outputs, not inferred from a receipt's presence. A separate later inspection raised `KeyError: digest_verification_mode`; its enclosing shell still returned zero because subsequent commands ran. That inspection is explicitly not counted as successful verification. Independent checks here use the actual heterogeneous input schema and pass.

## Documentation qualifications for integration

1. Preserve lead reproduction instructions: historical source is 174faca for the original measurement, 8f389406 for the strict scalar correction, and d0bb52f for the new narrow revision. Use new ignored scratch output paths for reruns; never overwrite tracked historical receipts.
2. The inherited `source_identity.archive_digest_reuse.same_path_and_same_size_assumption` sentence says selected members are read again. In the v2 addendum that sentence describes the earlier strict scalar trace only. The v2 revision's authoritative execution fields are `archive_members_reopened=0`, `origin_trace_recomputed=false`, and `archive_content_rehashed_for_this_revision=false`, consistent with inspected source and actual CLI events. Put this distinction prominently in the report/acceptance packet; retain exact original receipt bytes.
3. Preserve the runtime limitation and distinguish aggregate missing counts from the three-issuer typed reason sample. These limitations do not invalidate the corrected bounded measurements, but prohibit broader evidence claims.

## Exact original issue criteria and four statuses

The retained exact OPEN #70 body was read from `issue-70-launch-body.json`. This review does not claim a new GitHub refresh.

| Criterion | Bounded evidence | Full acceptance limitation |
| --- | --- | --- |
| Actual field and lookback coverage by security/date | Two named 505/503-member snapshots; corrected fixed-slot aggregates; named three-issuer detail | No daily production matrix or complete accepted three-universe identity coverage |
| Forms, Q4, concepts and unavailable inputs explicit | Domestic forms/concepts, direct fiscal tags, candidate arithmetic limits, exact scalar origins, missing slots retained | Universe-wide Q4 unit/currency/accounting-basis reconciliation remains missing; no invented Q4 values |
| Publication distinct from period end and required prehistory retained | Prior exact 145010-row audit alignment/timing, public-session definition, older observations and calendar clipping retained | Filing availability does not prove earliest earnings announcement; acquisition generation bridge remains absent |
| Adopted fiscal policy implemented/measured including foreign/Q4 | Accepted expected slots/matcher/cadence measured with negative and positive controls | Foreign form treatment and full production policy/input coverage remain unverified |

| Field | Full #70 status |
| --- | --- |
| Implementation | Partial data-delivery implementation; agreed bounded assessment and corrected readiness measurement complete |
| Required inputs | Partial: distinct export generation, marker mismatch qualification, missing identity manifest and production membership/lineage |
| Acceptance evidence | Partial: bounded assessment accepted with stated evidence limitations; production coverage, foreign and Q4 evidence incomplete |
| Dependencies | Start condition met through accepted #66; production acceptance still gated by eligible #68 membership/security identities |

Recommendation: integrate the frozen bounded correction with the report qualifications, retain all earlier reports/receipts, and keep full #70 open. No source acquisition, production state, policy consumption, qualification or closure is implied by this verdict.

## Integration and documentation acceptance addendum

Reviewed on 2026-10-01 at integrated source `5c6816e56dbd3149b6d81d645d03c5d0c9687482`, with the lead's report-only corrections in the working tree. **Documentation qualifications are resolved and accepted for publication.** The report prominently corrects the inherited ZIP-member sentence, explicitly limits typed reasons and slot dates to three issuers, and discloses the missing contemporaneous v2 runtime capture without inventing an execution-time identity. Its three reproduction recipes separately pin original 174faca, scalar-corrected 8f389406 and quarterly-v2 d0bb52f source and write new scratch outputs. No historical receipt overwrite is recommended.

Independently compared raw Git bytes at integrated 5c6816e and owner d0bb52f for the script, tests and all three receipts: all five match exactly. The v2 receipt's raw Git SHA-256 `14e3781bbb1b7cd7511556d73a25a0e86331234a3b33454904a8ab70adb6349b` differs from the preserved CRLF owner-checkout identity `86b6f4c152b2ebbaf090e6dee9a75abc231751b3b402cbf44f99614b64f3ba71`; these are distinct byte representations, not different measurements.

Read the current completion packet's #70 acceptance mapping, correction/count table, evidence limitations and four-status distinction. They agree with this bounded verdict and preserve full #70 as open/incomplete. Earlier holds/results remain dated; principal final acceptance and required hosted checks are still separate gates.

Read `docs/lead-a-historical-correction-verification.json` and authenticated both referenced actual logs by SHA-256. The focused log contains **100 passed, 2 warnings in 13.14 seconds**; Ruff contains **All checks passed**. The retained lead receipt additionally records 306 tracked Python files compiled and unchanged 61-context/58-runtime identities; no additional compilation, context scan or tests were performed by this documentation review. The owner raw-command record was read in the source review and rehashed here; its later KeyError remains classified as a failed inspection, not a successful measurement. Nothing in the publication packet relies on that failed inspection as a pass.

Reviewed byte identities before further unrelated #72 receipt packaging:

| File | SHA-256 |
| --- | --- |
| `docs/issue-70-bounded-source-assessment.md` | `11fa23ed288ecc23ea0dd7bee94f1186f48c679fcdc211ba07990a8ae361ffa8` |
| `docs/next-phase-historical-data-completion.md` | `2f796cc6ed62411dac672213b4daafbe591c057442b811382304981163caf845` |
| `docs/lead-a-historical-correction-verification.json` | `3be92061a873f5d4d17f6e07a47b1ddc5548bef7020826d597444359ce7b7daf` |
| `.artifacts/coordination/issue70-quarter-slot-owner-command-outputs.json` | `98c441c268e46dd96eed8f977ee0be84d2195bc5fea137158c87bf5992f83a1a` |

No remaining #70 publication finding. Accept the bounded corrected assessment and its documentation; keep full production #70 open.
