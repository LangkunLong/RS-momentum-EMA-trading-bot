# Issue 72 window-binding independent review

## Verdict and exact source

**Specification: pass for the bounded synthetic bridge contract. Code quality/integrity: pass, no remaining actionable finding identified.** Principal W1 is corrected at frozen owner commit ad628594b7708568bb97cb7d69e53745d96fdf35 atop32a52915853cfe1a240e7898796213b405c05c66. Ready for Lead A integration and meaningful final integrated verification, followed by principal final acceptance. This does not assert a merge, production admission or completed parent goal.

Owner2c6e HEAD and clean status independently confirmed. Read the complete five-path correction, including new tests, exporter call site, documentation and new receipt. Whole-contract review carries forward unchanged portions of the complete baseab385d7-to32a5291 review, checks their connection to the new date validation, and retains all earlier dated findings/results. Diff check across baseab385d7-toad62859 passed. The unchanged extractor, builder, prior test cases and original receipts preserve the prior R1/R2/R3 fixes.

Read complete principal review2026-10-01-issue72-32a529-corrected-independent-review.md and verified SHA2567c9fb0e2d352b8cf5cfdb485ea4ad6a34d498e7b41838c5a4f126e18d089268f. It clears prior R1/R2/R3 and holds only W1. Earlier32a/40f6 PASS records stay preserved as superseded historical verdicts; this is a new exact-source review.

## W1 correction inspection

The shared date-contract helper now canonicalizes each date, validates individual extraction/membership range ordering, and enforces:

- Projection extraction start/end equal authenticated prices start/end.
- Export extraction start/end and outer bridge start/end equal those same prices dates.
- Projection membership start equals export and bridge membership start.
- Projection membership end equals prices/export cutoff.

Prices dates are read from actual prices provenance bytes already bound to the caller/source graph. Reconstruction uses authenticated price dates and the checked exporter membership start, so recomputing hashes around an altered inner window no longer changes the accepted meaning. Membership2021 start is intentionally distinct from extraction2020 start and need not equal the first event or evaluator start.

The producer validates dates before emitting projection. The exporter reads canonical projection and referenced prices with a verified prices hash, validates projection dates before bridge creation, and validates export/bridge dates before publication. The builder's pure bridge validator joins the complete graph before reconstruction. Existing later builder warm-up/cutoff gates, strict source/fixture guard, foreign input hashes, identity/segment checks, paired master/audit validation and no-clobber publication remain intact.

## Meaningful independent execution

Ran one narrow pure-validator replay against the untouched retained coherent control and the principal's already-created internally rehashed altered case. No exporter, builder, provider, data acquisition, image run or full suite was executed for this review.

- Coherent control: accepted, with the retained four-row source graph and metadata.
- Principal altered case: rejected with **financial lineage projection extraction window differs from authenticated prices**.
- Corrected source files loaded in that process exactly match all three executed-source hashes declared by the new receipt.

This tests the actual principal counterexample, not a similar fixture with an accidentally stale checksum. Principal original script/result and both input directories remained unchanged.

Retained in lead .artifacts/coordination:

| Artifact | SHA256 |
| --- | --- |
| issue72-window-binding-review-probe.py | a05da40a595f3950abae4cf3f419d6a15f44e1792bfec97b5334b180d94b1f23 |
| issue72-window-binding-review-result.json | 16444e99aaf1d1e6d147c77388f6c23c9abe4139b9000eddec8538ff3e4f1434 |

## Regression review and preserved evidence

Inspected new coherent-rehash tests: shortened extraction end with identical financial CSV, changed extraction start, projection/export/bridge changed together against original prices, event-free membership-start shift with unchanged ticker/ledger bytes, event-free membership-end shortening, exporter creation/publication window checks, missing/noncanonical/inverted dates. Their helper regenerates raw and resolved history and updates affected projection/export/publication digests. Assertions require the specific semantic-window errors. The positive actual parser/master/extractor/publisher/builder case remains and preserves distinct2020/2021 starts and all four financial value/date rows.

The new window receipt records32 focused tests, Ruff and compile success on Windows/Python3.13.14, pytest9.0.2, Ruff0.15.9. Its source hashes independently match frozen worker bytes. Raw command-output retention was requested from the lead; until that record is inspected these particular command results are owner-reported, not a fresh independent suite execution. The independent counterexample replay above establishes W1 behavior directly; Lead A still owns final integrated tests.

Both original receipts independently rehash to their unchanged recorded values:

- Initial receipt0057134631331440645cb8040c44fb8724510ef9a314c8f9806d24bd6c9187b3.
- Prior correction receipt2643716e576891a96f2326d298887a7511774ef337c100b429a72ba236e22f59.

The new correction-result path under .artifacts/coordination/principal-reviews/issue72-window-binding-correction-32a529 is relative to the owner2c6e checkout; it is not a claim that the principal authored or executed that owner receipt. The principal baseline evidence retains its actual original-checkout location and identity. No retained old result was rewritten as a new run.

## Original criteria and four statuses

1. Exact original input/transformation binding: source/projection/master/audit/history graph remains authenticated and deterministic, now including the missing cross-document date semantics.
2. Coherent builder acceptance and foreign/relabelled rejection: existing real synthetic end-to-end control and negative gates remain; W1 altered graph now independently rejects on corrected source.
3. Unchanged publication timing and financial values: correction touches date contract validation only, not extractor fiscal logic, source values, next-session calculation or alias materialization. Prior independently reproduced four-row bundle remains historical evidence at its original run identity; no new bundle run is claimed here.

Implementation complete and reviewed atad62859. Required synthetic inputs supplied; production inputs remain separate. Acceptance evidence supports the bounded contract, with integrated verification and principal final review still pending. Dependencies remain accepted#66 and settled V3 identity interface; full#68/#69/#70 remain open as appropriate.

No production-source evidence pair was enabled. Multi-CIK extraction tests remain synthetic separation evidence, not a real issuer registry. No calendar/identity modeling expansion, source purchase, provider call, operational state mutation or old67/71/82 rerun occurred. This correction changes only offline bridge/exporter imports; final integration must retain the established evaluator context-equivalence check without relabelling old image proof.

## Raw owner-command evidence confirmation

Subsequently read the recovered actual owner command-event record .artifacts/coordination/issue72-window-binding-owner-command-outputs.json, SHA256 d3e49fa0de357d695b1f54482cae5f7c40cf6fa3a33ab16e80f45ca57295a5bd, bound to owner thread01a0f857-8241-7730-9067-6b212b1b22a2 and revisionad628594b7708568bb97cb7d69e53745d96fdf35. This resolves the raw-output verification limitation above without a rerun.

- Initial focused window command exec-69064f94-19ab-4eac-aa14-98060dfcdaba: exit0,12 passed/19deselected/1warning2.26s.
- Final two-module command exec-7a75b3d7-b7ab-4de0-9036-20555564be6b: exit0,32 passed/1warning7.23s; output untruncated. Earlier32-pass7.35s event is retained separately.
- Final Ruff exec-ba32482c-6c98-47f3-9764-5d2b6b13cff0: exit0,All checks passed, output untruncated.
- Final compile exec-b60e6f64-3df1-465b-b542-42803dba6639: exit0.

The commands and worker cwd match the new receipt. Existing cache_dir warning remains explicit. This confirms the historical owner execution evidence; it does not relabel those commands as lead integration executions. Final integrated source and its verification remain the next gate.

## Evidence erratum: source-hash comparison claim corrected

Principal identified a malformed test-source digest in the new ad62859 window receipt/result. A mechanical comparison now retained in issue72-ad62859-hash-erratum-check.json confirms: declared tests/test_financial_lineage_bridge.py digest65427048f04dab8667553e970e953888dddfee7b5aa4e929bc15347a6056437 has63characters and does not equal actual64-character SHA25665427048f04dab8667553e970e953888dddefee7b5aa4e929bc15347a6056437. The other two source declarations match.

My earlier statement that all three receipt declarations matched actual bytes was incorrect: I recorded actual file hashes but missed the one-character declaration discrepancy. That comparison claim is withdrawn. The independent pure-validator probe itself retained the correct actual hash; the error is in receipt identity reporting and my comparison assertion, not a newly observed source-behavior failure. Preserve this dated error and correction. Evidence acceptance is pending the owner's tiny documentation correction and a mechanical audit of every receipt hash declaration; no test rerun is needed for correcting a digest string.

## Final mechanical evidence correction and integrated gate verdict

**Final verdict: PASS for the bounded source, evidence correction, integration and documentation package. Recommend publishing the corrective PR head, subject to required checks on that exact published head and principal final acceptance/normal merge.** No remaining actionable finding identified. This addendum supersedes the temporary evidence-typo hold while preserving the explicit review error above.

### Mechanical declaration audit

Frozen owner80e2bfda6c369a5f55d6cf635ad015d9c2ff1262 changes exactly two digest strings in one tracked receipt relative toad62859. There is no source or test change. It is integrated as5e97ccf1279454e828fdcfe0345576d323d5118c; integrated receipt Git content equals the owner correction.

A dedicated script recursively enumerated **all23 SHA256 declarations** across the new window receipt and its linked owner correction-result JSON, validated each as exactly64 lowercase hexadecimal characters, and compared each with the actual referenced retained/source-file bytes. It asserts the enumerated declaration set equals the checked set. **All23 match.** This replaces visual digest comparison with an exhaustive mechanical check of those two documents.

- Corrected tracked receipt SHA256:34669bd35124623638006a06be0791f904213b9de1bb4bb1b24b786d106cb671.
- Corrected linked result SHA256:a7d48d1904b19f66c19ace7f971c2de4d08c593473f8035d1b9e278bf39c6337.
- Audit script/result: lead .artifacts/coordination/issue72-hash-correction-mechanical-audit.py and issue72-hash-correction-mechanical-audit.json.

The actual tested source staysad62859; the actual integrated test source stays5c6816e56dbd3149b6d81d645d03c5d0c9687482. Neither is relabelled as80e2bfd or5e97ccf. Earlier receipts and principal baseline counterexample remain unchanged. No test was rerun to fix a declared digest.

### Integrated verification and source boundary

Independently read and mechanically rehashed the actual final focused/Ruff logs named by docs/lead-a-historical-correction-verification.json. Both hashes match. Focused result is **100 passed,2 warnings,13.14s** at5c6816e; Ruff reports All checks passed. The lead receipt records successful in-memory compilation of306 tracked Python files. No broad-suite or data rerun was performed in this integration review.

All seven owner-equivalence records in that integrated receipt match both their declared raw-Git SHA256 and exact owner bytes: #72ad62859 bridge/exporter/tests/window receipt and #70d0bb52f assessment script/tests/quarter-slot receipt. The window receipt is subsequently corrected by the document-only80e2bfd/5e97ccf change described above, so the earlier receipt's old document hash remains historical rather than overwritten.

Independently checked raw Git size/SHA256 for all61 context files against B's retained manifest: zero differences. Existing58-file runtime closure and source-map identity remain intact. The new date helper is consumed by the offline bridge/exporter; no mapped runtime module changes or new runtime import follow. Accepted calculator and old67/71/82 evidence remain under their original definitions. Machine integration check: .artifacts/coordination/lead-a-correction-integration-independent-checks.json.

### Documentation and scoped acceptance

Read the current corrective packet, current ledger and #70 qualification update. Reviewed working-byte SHA256s:

- docs/next-phase-historical-data-completion.md:e0ab9a6c344a457564879af80126a8ba677bc2800da0de831205366f8636992f.
- docs/next-phase-historical-data-ledger.md:e160bac3d1c2e115e70a8bfbfa044548de72cca1dc3488cb49a43e162c8f58a5.
- docs/issue-70-bounded-source-assessment.md:11fa23ed288ecc23ea0dd7bee94f1186f48c679fcdc211ba07990a8ae361ffa8.

Current statuses show W1 and F1 corrected locally with principal/required-CI gates remaining. Historical holds, mistaken prior claims and old execution receipts are preserved as dated records. #70 expected-slot readiness and observed-match diagnostics are distinguished; archive-read wording is explicitly corrected to zero new member reads, and missing contemporaneous package-version capture is disclosed. #69 bounded acceptance and full-production gaps remain unchanged. All relative Markdown links in the three checked documents resolve.

This review relies on the retained independent #70d0bb52f specification/quality verdict for its bounded assessment logic, independently checks its exact integrated source identity and reads its final report qualifications. It does not duplicate that data assessment or claim new source tracing. No issue closure, normal merge, full#68/#69/#70 production acceptance or completed original four-issue goal follows from this package. The recommendation is to publish reviewed corrections and continue the named final gates.
