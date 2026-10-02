# Issue 70 v9 clean-public selector receipt

## Source and selection

- Accepted public base: `99fa44902fc3621338a5a1e19ea5b4a643c88187`.
- A temporary source export was created from that commit. The proposed public overlay was limited to the 12 paths listed below; no retained calendar/V8 inputs, production SEC archives, private coordination files, financial exports, or V9 output were copied.
- The six retained paths required by `tests/run_issue70_v9_retained_inputs.ps1` were confirmed absent. Imports were not allowed to fall back through `PYTHONPATH`; the public test's checkout-path assertion passed.

The overlay inventory is:

| Path | Git blob | Status |
|---|---|---|
| `core/sec_pit_fundamentals.py` | `c5bc1bf2d57339cc9088dc592eeb767123c2911d` | Accepted implementation, unchanged. |
| `tools/generate_issue70_q4_source_sample.py` | `daa002d6f5af7daf22678555880dc692d5ee357d` | Accepted implementation, unchanged. |
| `tools/issue70_v9_coverage.py` | `0024e6c079ed1dd5a8391e7ca22473e355cd4f1b` | Accepted implementation, unchanged. |
| `tests/test_issue70_q4_source.py` | `fcf9250f9b3edd1d7ef6369fab2a6e52d26d2d3a` | One selected coverage case now passes synthetic dates to both builder calls; assertions are unchanged. |
| `tests/test_sec_pit_fundamentals.py` | `17acde3ce8615a12c8128237e421f8b40d84f7bd` | Accepted test module, unchanged. |
| `tests/test_issue70_v9_public_contract.py` | `cb703002dde0a3ef80e27d439bbf9d21604bb53c` | New public controls. |
| `tests/retained_issue70_v9_source_contract.py` | `d77a303392a8032066106d32b5480048d4f77698` | Original 23-test module, byte-identical and not directly collected. |
| `tests/run_issue70_v9_retained_inputs.ps1` | `da91f5f627b5499868c5f0d9373da73c04169ed4` | Explicit preflight runner. |
| `tests/fixtures/issue70_v9_public/README.md` | `f3939e271364fed0cffa1f4a2713feef0f6bca1c` | Synthetic fixture scope. |
| `tests/fixtures/issue70_v9_public/synthetic_calendar.csv` | `ee2122171676ec17a3707bb9860a6f6647b87183` | 44-byte fixture; content SHA-256 `45e19b621f09da731abea705a2ba1f15b3cc06d186afd771f76f437d42e33728`. |
| `tests/fixtures/issue70_v9_public/synthetic_selected_member_manifest.json` | `1aa9348f0bbb0470fa95c48149b994beeca162fc` | 2,339-byte fixture; content SHA-256 `bbf91de41e372c70b65c9940f3ed887c5642d69ce1fb72254020a6082fd53750`. |
| `docs/issue-70-v9-source-contract.md` | `4b84b0c60e82b9aa0c056588c08f244c9230560b` | Sanitized source/test explanation present during the 41-test run. |

The export also used `tests/conftest.py` from the accepted public base unchanged (blob `a95deeb51a28d0fd077fd63057bb321a49af7b63`) for its established isolated temporary-directory fixture.

After that full-selector run, the source contract was updated to record the result, and the public contract test was strengthened to assert manifest-request identity and manifest-before-first-legacy-JSON ordering. The 41-test count above applies to test blob `cb703002dde0a3ef80e27d439bbf9d21604bb53c`, not the strengthened current test blob. The focused follow-up below verifies that specific change; the full selector was not rerun.

## Focused result

Exact command, from the temporary public export root:

```text
python -B -m pytest tests/test_issue70_q4_source.py tests/test_sec_pit_fundamentals.py tests/test_issue70_v9_public_contract.py -q -p no:cacheprovider --no-cov
```

Result: **41 passed, 2 environment warnings, exit 0**. The warnings were pytest's unknown `cache_dir` option and the installed `websockets.legacy` deprecation.

The selected public contract module explicitly called the unchanged assertion bodies of these 18 self-contained original cases:

1. `test_v9_generation_rejects_partial_contract_before_archive_access`
2. `test_generation_refuses_existing_output_before_any_input_access`
3. `test_v9_coverage_gzip_is_reproducible_and_enforces_record_and_byte_caps`
4. `test_v9_caps_measure_complete_nested_summary_and_csv`
5. `test_v9_cli_routes_complete_contract_to_owned_source_path`
6. `test_inherited_filed_fallback_uses_origin_date_not_trigger_snapshot_date`
7. `test_annual_growth_keeps_accepted_status_and_reports_skipped_year_gap`
8. `test_annual_growth_no_gap_has_no_gap_diagnostic`
9. `test_annual_growth_partial_window_keeps_only_real_missing_slot`
10. `test_annual_diluted_eps_growth_keeps_a_positive_gap_pair`
11. `test_annual_growth_gap_preserves_invalid_prior_reason_and_period_endpoints`
12. `test_v8_source_window_projection_is_exact_slot_anchored_and_classifies_missingness`
13. `test_v8_window_excludes_raw_2019_origin_even_when_available_in_2020`
14. `test_v8_projection_reselects_an_admitted_alternative_vintage`
15. `test_roe_income_endpoint_change_with_same_equity_endpoint_is_natural_anchor`
16. `test_v8_source_window_independently_gates_each_derived_q4_basis_origin`
17. `test_v8_projection_preserves_once_mapped_availability_session`
18. `test_same_session_vintage_order_uses_acceptance_timestamp_then_direct_q4_tie`

Two synthetic CLI controls reused the original measured-mismatch/no-write and manifest-before-legacy/archive assertion bodies while injecting the synthetic manifest and stubbing only retained manifest/calendar admission. Separate synthetic controls cover member identity malformed, duplicate, name, digest, length, and namespace cases plus raw-byte hash and calendar-date validation. They do not claim authentic retained-input admission or an adopted exchange calendar.

The retained-input runner was separately invoked from the same export. It exited with code **2** before starting pytest and listed all six missing retained inputs. The five input-bound original cases and their exact pins remain unchanged in the non-collected retained module.

## Follow-up manifest-order control

The current public contract test blob is `7e80f3419ce561636fb1094a430cd1f00728504e`; the source-contract document in worker delivery `06b6c23` has blob `cb3d5cf4817db6c15f17baad59ef03eba1fadce7` (subsequent integration wording changes are documentation only). The follow-up ran from the same temporary export of the accepted `99fa44902fc3621338a5a1e19ea5b4a643c88187` base after overlaying the current public contract test. All six retained inputs remained absent, `PYTHONPATH` was cleared, and this test asserted the implementation and retained-helper modules resolved within the export.

Focused command:

```text
python -B -m pytest tests/test_issue70_v9_public_contract.py::test_public_synthetic_cli_binds_manifest_before_legacy_and_archive_access -q -p no:cacheprovider --no-cov
```

Result: **1 passed, 2 environment warnings, exit 0**. The warnings were the same pytest `cache_dir` option and `websockets.legacy` deprecation described above.

The control recorded and asserted the order: `_read_bound_json` received the V8 manifest path `docs/issue-70-q4-source-sample-v8/fundamentals_provenance.json` and production pin `c5ecb3a97639861d6c79e5ab18aed49798b38eb4c9dcd9206f6bdabe08d3073f`; the stub then returned the synthetic manifest identity with fixture digest `bbf91de41e372c70b65c9940f3ed887c5642d69ce1fb72254020a6082fd53750`. The first legacy `_json_file` attempt targeted the test's missing `missing-legacy/import-provenance.json` only after that manifest event. The original assertion confirmed the CLI stopped at that invalid/missing legacy JSON before archive access. This is a synthetic ordering control, not successful admission of the retained manifest.

The 41-test full-selector result above remains tied to the earlier public-control blob `cb703002dde0a3ef80e27d439bbf9d21604bb53c`. It was not rerun after strengthening this one control; the focused result above covers the new assertion.

## Limits

This receipt proves only the focused source/test contract from the stated temporary overlay. No retained or production SEC archive was read. Existing selected Q4 tests create and consume temporary synthetic ZIP fixtures; no V9 source sample or production financial coverage file was generated or published. The receipt does not complete the full historical membership/security-lineage prerequisite, the resource/execution decision, or the #82 evaluator-image gate. The run remains held for those separate reviews.
