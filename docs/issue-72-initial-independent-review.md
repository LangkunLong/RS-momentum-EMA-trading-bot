# Issue 72 independent final review of df389223

## Verdict

Specification: **changes required**. Code quality/integrity: **changes required**. Do not accept or merge this revision yet. Three material findings are reproduced below. This verdict applies to frozen owner commit df3892234ad4b95408b9354c445e9b3a61985328, base ab385d792e19ff6db39d87f1123f47f660fc1e1d; subsequent corrections require review.

Read the exact issue body from lead .artifacts/coordination/issue-72-launch-body.json, complete seven-file base-to-head change, issue documentation and tracked JSON receipt. Worker HEAD matched the requested revision and status was clean. git diff --check passed. No broad suite or Ruff rerun was performed.

## Findings

### R1 — P1: retain same-issuer lookbacks across renamed aliases

Location: core/sec_pit_fundamentals.py:1508-1519.

The new history branch selects facts only when their public_date falls inside the ticker identity episode. That changes the previous single-CIK behavior, which retained all issuer snapshots under a ticker. For a verified same-issuer OLD-to-NEW rename, NEW loses pre-rename observations. The downstream provider remains ticker-scoped (core/pit_data.py:2087; core/pit_feature_snapshot.py:142), so an OLD row cannot supply NEW lookbacks. Repeated FISV episodes similarly lose intervening FI snapshots. This changes available financial history despite unchanged individual numeric cells/dates and violates the criterion that normalization preserves financial meaning.

Reproduction uses the owner's actual deterministic synthetic ZIP/parser/master fixture with CIK1 for both OLD and NEW. At the reviewed commit the bridge yields NEW on 2020-01-06 EPS1.25 only. With the same archives/master and identity_extraction_rows removed, the established extractor also yields NEW on 2020-01-02 EPS0.75. This is same-issuer continuity, not permission to merge different CIKs. Preserve authenticated same-issuer lookbacks without crossing genuine issuer-reuse boundaries; add a successor-as-of-before-next-filing regression and repeated-symbol lookback case.

### R2 — P1: do not drop ordinary identities when a segmented lineage is present

Location: core/financial_lineage_bridge.py:469-490.

_identity_extraction_history_rows takes a global if segment_contract branch and emits only entries in contract.segments; ordinary/legacy lineages are handled exclusively in else. Segment support is optional per lineage, so a mixed universe silently loses all nonsegmented history windows. This can remove warm-up predecessor data for nonsegmented lineages and makes the generated extraction-history claim incomplete.

Reproduction loads the existing validated Fiserv segment contract and combines it with an ordinary ORD identity and membership in the same input map. Expected ticker history set FI,FISV,ORD; actual FI,FISV. Process segmented lineages and remaining ordinary/legacy identities in the same projection; require a mixed-universe regression.

### R3 — P2: preserve direct membership binding for synthetic fixtures

Location: build_pit_bundle.py:752-759.

The new synthetic-source exemption skips both the SEC bridge and the previous membership_csv_sha256 equality check. As a result, a foreign or arbitrarily relabelled membership digest is accepted whenever the source says synthetic fixture and explicit fixture opt-in is present. This weakens existing bounded fixture integrity, although the separate production guard remains closed.

Reproduction generates the owner actual fixture, labels fundamentals synthetic fixture, replaces membership_csv_sha256 with 64 zeroes and removes financial_lineage_bridge_v1. The builder exits0 and writes SQLite. Retain the prior direct destination membership hash check in this branch; requiring the SEC bridge for a declared non-SEC fixture is unnecessary.

## Retained independent reproduction evidence

All under lead workspace .artifacts/coordination/:

- issue72-review-counterexamples.py: SHA256 d7dcbd339c70a8f24a6323b97dfb90f087eb4cb64dd3565d412c856bda1449de.
- issue72-review-counterexamples/result.json: SHA256 fe7fd1d94c63955d6ac33c39b288af4ed02d6f33bcffc2db6d0b3e3aa35775c3.
- issue72-review-foreign-fixture.py and issue72-review-counterexamples/foreign-fixture-result.json record R3 and actual builder success.
- The counterexample directory retains the generated source fixtures, provenance and SQLite. No network/provider request, operational state, Docker or old expensive report was touched.

These targeted checks were justified by concrete source risks; they are not repeats of the 68-test suite.

## Acceptance criteria mapping

1. Exact inputs and lineage projection: the SEC path rehashes retained inputs, recomputes membership/history/ledger, validates security-master episodes and paired audit rows. Partial positive evidence, but R2 makes mixed identity history incomplete.
2. Coherent builder and foreign provenance rejection: real synthetic ZIP -> actual parser/master/extractor -> publisher -> builder test now exists, and SEC-source foreign/tamper controls are present. R3 leaves a fixture provenance regression; R1 changes the usable result.
3. Unchanged values/publication: raw values and dates survive the demonstrated rows and are asserted in SQLite; no second next-session shift was found. R1 silently removes required same-issuer observations, so acceptance is not met by those cell-equality checks alone.

## Earlier draft findings and retained verification

The predecessor omission from extraction universe is addressed structurally by an identity-history sidecar separate from true membership, and a warm-up rename-before-membership fixture exists. Actual extraction is now exercised using deterministic ZIPs. However episode filtering introduces R1, and mixed identity handling remains incomplete.

The tracked report claims 68 focused tests and Ruff passed. The JSON receipt names one builder test command and retains bundle/metadata/archive digests, but does not itself bind the 68-test or Ruff logs to source. Raw logs were not found in accessible worker .artifacts paths; one receipt temp directory denied access. Asked lead to obtain exact retained log paths from owner. Do not reinterpret missing log verification as a test failure or rerun merely to locate evidence.

## Integration and limitations

Compared latest available protected main 68b5358e55df1d8a93851550f421e594541dc74e to dispatch base: changed files are optimizer contracts, Docker ignore and B documentation/tests. No pathname overlap with this seven-file change. This is not a merge execution or final integrated test claim. Coordinate shared evaluator source-map identity if the new SEC-reader/module footprint affects it.

No full production membership/source acquisition is claimed. Production admission remains fail-closed. Upstream archive digests are correctly described as exporter-produced declarations in the builder, distinct from actual builder consumption. Final correction review must preserve these limits and verify all three findings, updated exact-source evidence, and integration before recommendation becomes ready.
