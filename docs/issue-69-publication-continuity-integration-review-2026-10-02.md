# Issue 69 publication continuity — lead independent integration review

Reviewed implementation: `d1662b80ddf541e70b1794f153ab0cf5d1caa6e6`. Normal integration source actually tested: `4f1f0c63e90370a03e78efbb5ffbdd18d63a2e3c`. The two modified Python files and worker report were inspected at that immutable implementation revision. The original bounded reports and inputs remain unchanged.

**Disposition:** the exporter correction is supported as a bounded safety increment. Full #69 remains unaccepted. The documentation finding about offline publication authority was corrected in `013250b73032ba808874e737007d12aa73d60fb7` and integrated after testing; that commit changes only the worker report. Eligible versioned offline publication is authorized; missing authenticated inputs and any uncovered provider operation are separate constraints.

## Source and failure mechanism

The prior exporter audited continuity in an intermediate adjusted provider snapshot, normalized cached prices independently and then preferred cache rows during the final merge. It did not recheck continuity on that merged publication. The correction clips cached rows to their declared admission intervals and checks linked identities in the final merged CSV before validation/publication completes. It retains the intermediate audit separately and adds explicitly scoped final-output provenance. It does not select which conflicting price source is true or rewrite the retained export.

There is no required overlap at a ticker transition. The zero-overlap test records zero observed rows and null bounds; this is not positive proof of continuity. The exporter remains a legacy one-interval-per-ticker consumer. Its separate missing segment-aware admission/normalization integration must preserve both FISV episodes once authenticated #68 segments are supplied.

## Independent verification on integrated source

`python -B -m pytest -q --no-cov -p no:cacheprovider tests/test_export_pit_prices.py` completed with **27 passed, 1 skipped, 2 warnings** in 1.75 seconds. The skipped test requires native POSIX mode bits and is conditionally skipped on Windows. Disabling the cache plugin produced an unknown `cache_dir` configuration warning; the other warning was the existing websockets deprecation. Neither is represented as an application failure. Ruff passed on both changed Python files; `git diff --check` passed.

Tests use an explicit temporary directory outside source under the lead's authorized visualization root. This is the complete focused exporter module, not the full offline suite or a credentialed provider workflow.

Runtime: CPython 3.13.14, Windows 11 `10.0.26200-SP0`, pandas 3.0.1, pytest 9.0.2, Ruff 0.15.9. Executable: `C:/Users/llong/AppData/Local/Microsoft/WindowsApps/PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0/python.exe`. Logs, exact command source and independent retained-input receipt are under `.artifacts/evidence/issue69-lead-integrated-d166/`.

## Actual retained-input counterexample

The independent read-only script checked input hashes, independently compared numeric OHLCV tuples and invoked the integrated validator. Exporter checkout SHA-256 before and after was `e32ac50e776f7dff9839ec283b35632fac407e8e871ce1c5a42eda214e610e48`; script SHA-256 was `f88601de61bfdb903d46526c7fc1f6925bbbe88a7c702913fab2b2f75f80d673`.

| Input or measurement | Identity/result |
| --- | --- |
| Retained `prices.csv` | SHA-256 `dd18e38d14356df2be9aea79bc777407d40750305dcf327a2f3552815c39c376` |
| Retained `pit_price_identity_map.csv` | SHA-256 `6a9ec69bc0fe05decea1b832cac8e26a611d706cce831d5687fa5424f9544955` |
| Entire FI/FISV overlap in retained file | 1,473 sessions from 2020-01-02 through 2025-11-10; 328 exact matches |
| Overlap inside declared FI admission | 610 sessions from 2023-06-07 through 2025-11-10; **zero** exact matches |
| Integrated validator on the entire retained file | Rejected: 1,145/1,473 shared rows differ, first mismatch 2021-04-22 |

The 610-session admitted-window contradiction and the 1,145-mismatch whole-file rejection have different denominators. Neither count is substituted for the other. No input files, operational stores or source price values were written. The independent counterexample is retained in `retained-review.json`; it is not a corrected price dataset.

The worker's final receipt at `8e3c9325ec5545b3f996da0f538cf1cd50ec0b21` has raw Git SHA-256 `24f1bd456da1008015de6c2942b80d2eb3a0c147509d718d71dae45dcb41aabb`. A separate bounded verification matched all **12 named source files** to their exact Git revisions and all **13 retained input files** to declared hashes and sizes. Its source revisions distinguish the implementation, documentation correction and receipt parent. This verifies current byte identities; it does not authenticate historical provider retrieval timestamps or imply a provider rerun. The check is retained in `worker-receipt-verification.json` with its independent script.

## Acceptance mapping and remaining work

| Original #69 criterion | Increment and remaining evidence |
| --- | --- |
| Warm-up/evaluation coverage per security/date | Prior explicit sample missingness retained; this correction prevents some inadmissible cached history entering later exports. Complete three-index coverage remains absent. |
| Corporate actions supported/excluded/unresolved | Worker report retains bounded named classifications. Full action ledger and source-backed resolution of missing/zero-volume cases remain incomplete. |
| Adjustment and identity reconciliation with reference evidence | Final merged-output audit gap corrected and actual conflict reproduced. Reference outputs' equal bytes are consistent with declared unity factors, not proof of independent original retrieval or rights. Correct source prices and segment-aware production binding remain pending. |

- **Implementation:** bounded exporter correction independently verified after integration; segment-aware production path remains missing.
- **Required inputs:** exact original intermediate provider snapshot/cache and full union/action/use evidence remain unavailable or unverified.
- **Acceptance evidence:** focused tests and actual retained-data rejection available at exact source; no full production delivery or repaired export accepted.
- **Dependencies:** #66 start prerequisite met; #68 authenticated identities required for final binding. Independent correction work continues.

Recommendation: retain this code increment for principal review with the owner's immutable receipt; the documentation correction is resolved. Keep #69 open. No image rebuild is needed for this exporter-only change, and no previously accepted evaluator source identity is relabeled.
