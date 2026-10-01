# Issue 70 corrected final independent review

Review date: 2026-10-01. Owner revision **`8f389406f1c7339662387d446cbbffd997b810e8`**, clean worktree `C:/Users/llong/.codex/worktrees/f1ba/RS-momentum-EMA-trading-bot`, base `ab385d792e19ff6db39d87f1123f47f660fc1e1d`. Reviewed the complete five-file scoped addition and the correction from `174faca79cecfa0a710bbc4112fd2c81f225f281`. Product exporter/builder/calculator, source inputs and earlier accepted reports are unchanged.

## Verdict

**Accept the agreed bounded retained-source assessment and corrected scalar trace. No remaining material implementation or measured-evidence finding.** This completes the agreed two-snapshot/three-issuer assessment scope; it does not complete production data delivery or authorize closing #70. Full issue statuses remain partial as the report states.

One documentation-only clarification remains for the lead's integration: explicitly label the first reproduction command as historical for source `174faca79cecfa0a710bbc4112fd2c81f225f281`, and point current reruns to a new ignored scratch output path. Both shown commands currently target retained tracked receipts; running the historical command with the current script would overwrite weaker historical evidence under a corrected definition. Preserve both tracked receipts. This clarification needs no measurement rerun.

The original changes-needed review and executable counterexample remain preserved under `issue70-final-review.md` and `issue70-source-trace-negative-review.*`.

## Exact final artifacts

| File | Independently measured SHA-256 |
| --- | --- |
| `tools/assess_issue70_retained_source.py` | `c26a9de43bcf12c11f9f802df12c273301553dfa3f06f21affcec0beaaae3c46` |
| `tests/test_assess_issue70_retained_source.py` | `cf035706d06055217d032ad3b1d8323a9e1850ba900ab50732957a8b4de24816` |
| `docs/issue-70-bounded-source-assessment.md` | `d5bced5651894013a563ea468341d043345861a8b88e6c5b3809c81f89ff05fb` |
| Original receipt | `1fc74971cee7c30430ea8efbecdf615f3d8c113849a41c66ec52445d0e3c5733` |
| Corrected receipt | `7e690f3a6e0dd116f5a0b20267fcf6e8eebdd37ba9271eafbb8675c62819c5a8` |

Original receipt is unchanged. The corrected receipt records execution HEAD `174faca79cecfa0a710bbc4112fd2c81f225f281` plus the exact corrected script digest, which equals the final reviewed bytes. It does not falsely attribute execution to the later packaging commit. Python 3.13.14/pandas 3.0.1 and original source module identities are retained; accepted calculator remains `pit-financial-features-v3`, Git blob `71e562ffc00dc4e617f1cd625e3f13ea5fe3f81a`.

## Correction validation

The trace now requires the same origin accession/concept/form/filed/fiscal metadata plus exact export period end, compatible statement family, quarterly duration 70–115 days or annual duration 300–430 days, instant balance facts, and expected USD/shares, USD or shares units. It only reports a matched scalar for one uniquely qualifying fact. Multiple qualifying facts are explicitly ambiguous before value selection; mismatches and missing origins remain in the exported-nonempty denominator.

The formerly accepted wrong-period/wrong-unit equal number is now rejected. The focused tests exercise the actual bounded archive trace, not just a copy of helper logic, and retain failed matches in the denominator. The suite covers wrong period, duration, statement family, unit, value, ambiguity and missing origin, together with positive scalar/instant cases.

The all-statement scalar-presence column is now labeled explicitly, resolving the earlier misleading quarterly/annual row-count interpretation. The previous annual adjacency, missing quarterly slot, balance equity/period ordering, DEI shares namespace, timing partitions and Q4 fiscal-versus-calendar distinctions remain corrected.

## Independent verification

Ran from the frozen owner worktree:

```powershell
$env:AGENT_LOOP_TEST_TMP_ROOT='C:/Users/llong/.codex/worktrees/a9c1/RS-momentum-EMA-trading-bot/.artifacts/coordination/issue70-review-test-files'
python -B -m pytest tests/test_assess_issue70_retained_source.py -q -p no:cacheprovider --no-cov
```

**15 passed, 2 warnings, 1.47 seconds**, exit 0. The warnings are pytest's disabled-cache configuration option and an existing websockets deprecation. An initial attempt using pytest `--basetemp` instead of the repository's custom `AGENT_LOOP_TEST_TMP_ROOT` produced 7 passed / 8 fixture setup permission errors because that custom fixture still targeted the read-only worker directory. Rerouting the fixture to the verified writable lead artifact root resolved the environment issue without source changes. Final worker Git status remains clean. No full suite, full assessment or large archive hash rerun was performed by this reviewer.

Independently parsed and reconciled receipt counts: corrected trace denominator **1946**, unique compatible matches **1946**, no mismatches or ambiguous/missing-origin sample values. Each per-metric disposition sum equals its nonempty-value denominator. All non-trace measurements in the old and corrected receipts are byte-content-equivalent JSON values, including 145010 audit/export key pairs, timing partitions, and the 505/503 member snapshot denominators.

All six reopened member identities in the corrected receipt match the corresponding original receipt identities:

| Issuer/member | SHA-256 |
| --- | --- |
| A CompanyFacts | `a782d953c7f45eceb7e2f14e5aadf6d27f1836fb652fa32d40c0812f92dc719d` |
| A submissions | `4d73db27acd71ef749be90ed4c6a296d9516602851f77c8896f6d3f5bb396667` |
| AMZN CompanyFacts | `e6aadb0da7384597dbd78f74d1379ffb7fd0829a4d2e797b6d7e442b5e1da0d7` |
| AMZN submissions | `202c0ede2e13da021833aa987614478023b2231f3bcb850c35f5f057b4baa611` |
| MSFT CompanyFacts | `f8aae2965b20ad0df44bdf7ccbedf797d275b6b8dc030154a7a311361bb7246f` |
| MSFT submissions | `ef3d1eed787e1d32831fbebbce56e7e151dd13dcc99cf727c427627625df9cfd` |

The corrected run transparently reuses original whole-ZIP digest attestations after path/size/metadata agreement and committed-receipt identity validation. It does not claim fresh full-ZIP authentication. Same-size/path metadata alone would not cryptographically prove unchanged archives; here the selected members were reread and their newly reported hashes agree with the original trace. This is adequate for the bounded unchanged-member correction. Future different-member or production use needs its own appropriate input identity verification.

## Acceptance mapping and remaining boundary

- **Fields/lookbacks:** measured alternate export plus two named member snapshots and three named issuer histories, preserving accepted annual and quarterly semantics. No full daily production matrix is claimed.
- **Forms/Q4/concepts:** domestic supported forms and exact source concepts counted; direct fiscal tags, calendar frames, duration candidates and unproved Q4 arithmetic remain distinct. Missing/unreconciled values are not manufactured.
- **Publication/lookback:** period end, source acceptance/filed date and normalized public session remain distinct; earlier retained observations and calendar clipping are disclosed. No double shift or earliest-announcement claim.
- **Policy implementation/measurement:** current accepted semantics are measured in the bounded sample. Foreign support, full-universe Q4 reconciliation and downstream eligibility remain unverified or incomplete.

The alternate export, earlier acquisition generation and publication-marker mismatch retain separate identities. Marker-bound metadata reconstruction is qualified, with no invented transaction history. Missing full #68 membership, source-generation/identity provenance, foreign measurement and comprehensive Q4 evidence remain full-issue gaps. The report's four partial statuses describe full #70; the bounded assessment itself is complete and independently reviewed after the documentary clarification above.
