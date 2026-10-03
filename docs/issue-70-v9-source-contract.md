# V9 financial coverage source contract

This document describes the accepted bounded historical-financial source implementation and its test packaging. It does not report a completed V9 dataset, production coverage, actual policy consumption, or evaluator-image qualification.

## Scope and source

The accepted source correction is `96afe6d3b6dc60668fc308c179699274d77767c0`, integrated at `7b124f68deec8468c5b6b3341f4c1b966321f43f`. A proposed public overlay starts from accepted public source `99fa44902fc3621338a5a1e19ea5b4a643c88187`; any overlay revision must be recorded separately after publication review. A local source revision does not prove that a public branch contains it.

The implementation measures seven normalized fields, expected fiscal lookbacks, and retained source origins for a four-issuer development sample. The declared extraction history starts in 2010; evaluation covers 2020–2025. Pre-membership rows are explicitly excluded from eligible denominators. The sample does not satisfy the full historical S&P 500, Nasdaq-100, and Russell 2000 membership/security-lineage prerequisite.

## Financial and timing semantics

- Preserve each metric's own acceptance timestamp or explicitly empty acceptance with its filed-date fallback. Keep raw source-public dates separate from fiscal period ends and once-normalized availability sessions.
- Preserve adjacent-reported annual growth. A skipped fiscal year is a separate diagnostic; it does not automatically invalidate the observed comparison. Retain invalid-prior and insufficient-history reasons and both fiscal endpoints.
- Preserve the latest-income/latest-equity ROE model. A changed selected income or equity fiscal endpoint is a natural-anchor change; a changed vintage of the same fiscal periods remains a different classification.
- Distinguish direct Q4 observations, additive revenue derivations with all four retained FY/Q1/Q2/Q3 basis origins, and unavailable observations. Do not derive quarterly EPS by subtracting annual EPS.
- A declared policy consumer or available policy input is not actual policy consumption. The output label remains `not_measured_no_policy_replay`.

## Input admission and comparison evidence

The bounded generator requires exact adopted calendar/provenance inputs and a hash-bound prior source manifest. It compares all fourteen measured selected SEC members by archive namespace, member name, raw SHA-256, and expanded length. Read counts remain measured execution metadata. Duplicate, missing, extra, malformed, or changed identities fail before publication.

The existing financial summary contains a deterministic `v8_source_window_comparison` section. It projects the original raw 2020–2025 source window over the same in-memory origins and accepted calculators onto frozen V9 expected-slot IDs and the same eligible denominator. Each inherited origin and each derived-Q4 basis input must pass its own raw-date gate. The projection reselects admitted alternatives and retains typed status, missingness, fiscal endpoints, and origin-admission decisions. It does not claim regenerated historical V8 results.

Combined evidence accounting counts coverage CSV rows, slot-comparison entries, per-slot origin-admission decisions, and annual-gap diagnostics. Canonical byte accounting includes canonical CSV plus the complete serialized financial summary once. Full origin references, protocol definitions, and source/result hashes remain in the evidence even where a repeated reference string is not a separate origin record.

## Public test selection

The proposed self-contained public verification uses this exact selection:

```text
python -B -m pytest tests/test_issue70_q4_source.py tests/test_sec_pit_fundamentals.py tests/test_issue70_v9_public_contract.py -q -p no:cacheprovider --no-cov
```

The proposed public path inventory is:

| Role | Path | Identity/status |
|---|---|---|
| Production source | `core/sec_pit_fundamentals.py` | Accepted blob `c5bc1bf2d57339cc9088dc592eeb767123c2911d`; unchanged. |
| Production source | `tools/generate_issue70_q4_source_sample.py` | Accepted blob `daa002d6f5af7daf22678555880dc692d5ee357d`; unchanged. |
| Production source | `tools/issue70_v9_coverage.py` | Accepted blob `0024e6c079ed1dd5a8391e7ca22473e355cd4f1b`; unchanged. |
| Public test | `tests/test_issue70_q4_source.py` | Proposed blob `fcf9250f9b3edd1d7ef6369fab2a6e52d26d2d3a`; both builder calls in one Q4 coverage case supply an eight-date synthetic calendar, with its assertions unchanged. |
| Public test | `tests/test_sec_pit_fundamentals.py` | Accepted blob `17acde3ce8615a12c8128237e421f8b40d84f7bd`. |
| Public test | `tests/test_issue70_v9_public_contract.py` | Current blob `7e80f3419ce561636fb1094a430cd1f00728504e`; synthetic controls import the unchanged retained module only as a helper for 18 self-contained assertions and two synthetic CLI controls. |
| Test helper, not collected directly | `tests/retained_issue70_v9_source_contract.py` | Exact accepted blob `d77a303392a8032066106d32b5480048d4f77698`; two CLI assertion bodies are reused only with synthetic admission stubs, not retained inputs. |
| Retained-input preflight only | `tests/run_issue70_v9_retained_inputs.ps1` | Copied into the isolated export to verify missing-input exit behavior; not part of the public pytest selection. |
| Public fixture | `tests/fixtures/issue70_v9_public/README.md` | Explains synthetic scope and limits. |
| Public fixture | `tests/fixtures/issue70_v9_public/synthetic_calendar.csv` | Pinned 44-byte date-parser input. |
| Public fixture | `tests/fixtures/issue70_v9_public/synthetic_selected_member_manifest.json` | Pinned 2,339-byte synthetic identity manifest. |
| Sanitized explanation | `docs/issue-70-v9-source-contract.md` | This document; no private receipt, local handoff path, or raw financial data. |

The retained-input module is imported by the public contract module as an assertion helper, but is not collected directly by pytest. Its five input-bound definitions and real-input pins remain unchanged. The public selection does not execute them against retained inputs; it reuses two CLI assertion bodies with a synthetic manifest and a stubbed calendar-admission boundary. The other three input-bound bodies remain uncalled in the public selection. The explicit PowerShell runner is also outside the public pytest selection. The six adopted-calendar/V8 input files, SEC archives, private coordination artifacts, and generated financial exports are not in the proposed public path selection.

The omission-counter module keeps its accepted test blob. The Q4/source-origin module has the narrow synthetic-calendar change listed above; its original assertions are unchanged. The added public contract module uses only two pinned test fixtures under `tests/fixtures/issue70_v9_public/`:

| Fixture | Controlled identity and scope |
|---|---|
| `synthetic_calendar.csv` | 44 canonical UTF-8/LF bytes; SHA-256 `45e19b621f09da731abea705a2ba1f15b3cc06d186afd771f76f437d42e33728`. Three ordered test dates exercise `_validate_bound_file` and `_calendar_dates`; they are not an exchange calendar. |
| `synthetic_selected_member_manifest.json` | 2,339 canonical UTF-8/LF bytes; SHA-256 `bbf91de41e372c70b65c9940f3ed887c5642d69ce1fb72254020a6082fd53750`. Fourteen fabricated namespace/name/digest/length rows exercise identity matching only; they are not selected SEC members. |

The public tests assert that imported implementation modules and the retained test helper resolve inside the selected checkout, and guard against access to the six retained calendar/V8 files listed below. They call the unchanged assertion bodies for all 18 original self-contained cases, covering fallback timing, annual-gap variants, missingness and window dates, vintage selection, ROE anchoring, derived-Q4 origin gates, once-mapped availability, same-session ordering, output/CAP1 boundaries, and CLI routing. The selected Q4 coverage test keeps its original assertions and passes the same eight synthetic session dates to both coverage-builder calls, covering the test fixture's strict-next-session mappings and evaluation dates while avoiding its helper's retained-calendar default. Two additional controls reuse the original CLI assertion bodies with an injected synthetic member manifest and stub only the retained manifest/calendar admission boundary; they cover measured-mismatch-before-coverage/no-write and manifest-before-legacy/archive ordering, not authentic retained-input admission. Synthetic member tests also cover expected and measured malformed, duplicate, name, digest, length, and namespace mismatches, while generic raw-byte hash and date validation use normalized synthetic fixture copies. No synthetic calendar is represented as an adopted 4,024-session calendar. The positive full-calendar adoption gate and actual retained V8 member binding remain proved only by the retained exact-input suite.

The earlier **21 passed, 2 environment warnings** result came from the private worktree and is preliminary only; it is not clean-public-base proof. Final verification used a temporary export of accepted public base `99fa44902fc3621338a5a1e19ea5b4a643c88187`, then overlaid only the listed source, test, fixture, and sanitized documentation paths. The six retained inputs listed below were absent, `PYTHONPATH` was cleared, and the public module's checkout guard passed.

The exact selector passed **41 tests, with 2 environment warnings, exit 0** on the earlier public-control module blob `cb703002dde0a3ef80e27d439bbf9d21604bb53c`. The warnings were pytest's unrecognized `cache_dir` option and the installed `websockets.legacy` deprecation. A follow-up strengthened the manifest-before-legacy test and was run separately on the current public-control blob listed above; that focused result is recorded in the receipt. The explicit retained-input runner was also invoked from the export and exited with code 2 before pytest because all six required files were absent. These results are separate from the historical 23-test retained-suite receipt and the earlier 87-test result; the counts are not combined. A sanitized run receipt is `docs/issue-70-v9-public-test-receipt-2026-10-02.md`.

The accepted public base `99fa44902fc3621338a5a1e19ea5b4a643c88187` does not contain these retained inputs:

- `docs/issue-70-calendar-candidate-v1/exchange_sessions.csv`
- `docs/issue-70-calendar-candidate-v1/calendar_provenance.json`
- `docs/issue-70-calendar-candidate-v1/candidate-publication.json`
- `docs/issue-70-calendar-candidate-v1/principal-adoption-decision.json`
- `docs/issue-70-q4-source-sample-v8/spy_trading_days.csv`
- `docs/issue-70-q4-source-sample-v8/fundamentals_provenance.json`

The public command above does not select or collect the retained exact-input module as a test file. It has no marker-based skip. The public CLI controls explicitly inject a synthetic manifest and stub retained calendar/manifest admission, and the standalone raw-byte checks use synthetic fixture copies; neither substitutes for missing retained evidence or claims authentic input admission.

## Retained exact-input suite

The original source-contract module's accepted Git blob is `d77a303392a8032066106d32b5480048d4f77698`. Its bytes, all 23 original test definitions, and the five input-bound definitions/pins remain unchanged at `tests/retained_issue70_v9_source_contract.py`; the non-`test_` filename keeps the module out of default pytest discovery. The historical 2026-10-02 23-passed/two-warning receipt remains tied to that accepted module content and is not claimed as a new public run.

Run the retained suite only by explicit request with:

```text
powershell -NoProfile -File tests/run_issue70_v9_retained_inputs.ps1
```

The runner checks all six retained files and the exact accepted test-module blob before invoking the full 23-test module. If any retained path is absent it exits with code 2 and lists the missing files; it never skips or replaces them. If a file exists with changed content, its original hash-pinned test fails. The five tests that require exact retained inputs are:

- `test_v9_calendar_input_is_adopted_and_matches_entire_retained_window`
- `test_v9_calendar_rejects_changed_csv_before_reading_reference`
- `test_v8_manifest_binds_exact_measured_member_names_hashes_and_lengths`
- `test_measured_member_mismatch_fails_through_cli_before_publication`
- `test_v9_cli_source_path_binds_manifest_before_retained_source_access`

This retained suite keeps the real adopted-calendar/hash and 14-member pins, malformed/duplicate/namespace/name/read-count negatives, actual CLI measured-mismatch/no-write ordering, and the other accepted R1/R2/R3/CAP1/ROE synthetic assertions byte-for-byte with their original definitions. Public synthetic identities never stand in for those real inputs.

## Verification and limits

The focused source correction was accepted with its original 23-test precommit result and two environment warnings. Ruff and compilation passed separately at that source revision. The earlier 87-test result belongs to a preceding revision; the results are not combined. The public selection above is a separate, self-contained verification. No archive read, source generation, financial run, or new evaluator image is claimed.

Current limits remain 250,000 combined detail records, 128 MiB canonical evidence, 20 MiB total published output, 600 seconds, 64 MiB per selected member, 512 MiB total expansion, 10,000 paired financial/audit rows, and at most 6,032 issuer/session cells. Complete no-clobber publication is required; output is never truncated or sampled to fit a cap. The accepted static lower bounds are at least 265,520 records and 71,789,665 bytes of mandatory comparison JSON before additional Q4/origin/decision/gap evidence. They exceed the current record and output limits. They are lower bounds, not generated totals or safe revised ceilings. Financial generation remains held for separate principal review.

## Delivery status

| Dimension | State |
|---|---|
| Implementation | Bounded source corrections and scoped integration accepted. The clean-export public selector passes; publication remains under review. |
| Required inputs | Bounded adopted inputs are identified. Full eligible historical membership/lineage and production financial inputs are incomplete. |
| Acceptance evidence | Focused source checks and exact integration bindings are retained. No accepted V9 dataset or production coverage result exists. |
| Dependencies | Resource/execution decision, full historical-universe inputs, independent publication review, and the existing #82 evaluator-image gate remain separate. |

All four full financial-data acceptance criteria remain open: actual coverage by security/date; explicit and complete form/Q4/concept treatment; publication-timed prehistory delivery; and measurement of the adopted filing/fiscal/foreign-issuer policy. Prior bounded source acceptance retains its original scope.
