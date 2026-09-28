# Issue #71 parity failures: baseline audit

This audit answers whether the three failures previously observed in
`tests/test_fundamental_input_parity.py` exist before the #71 changes. The same
three exact cases were run in fresh, clean, detached worktrees at accepted
starting main and the lead-integrated source.

## Reproduction

**Accepted baseline:**
`2c01e76a878a338ab2b743c38c4f1310aab3f75b`

**Lead-integrated source:**
`4bf32a3a327421b62d8977b4f4109b0341955e29`

Both runs used the same command:

```text
python -m pytest tests/test_fundamental_input_parity.py::test_live_simple_and_pit_use_the_same_n_and_i_inputs tests/test_fundamental_input_parity.py::test_technical_only_remains_price_only_and_never_calls_fundamentals tests/test_fundamental_input_parity.py::test_pit_n_uses_revenue_only_on_and_after_its_public_date -q -p no:cacheprovider -o addopts='' --tb=short
```

Each run reported **3 failed, 2 warnings in 2.17s**, with the same failure
types and source locations:

| Test | Failure in both revisions | Origin |
| --- | --- | --- |
| `test_live_simple_and_pit_use_the_same_n_and_i_inputs` | `ValueError: entry market context is invalid` | `core/backtest_engine.py:1365` |
| `test_technical_only_remains_price_only_and_never_calls_fundamentals` | `ValueError: entry market context is invalid` | `core/backtest_engine.py:1365` |
| `test_pit_n_uses_revenue_only_on_and_after_its_public_date` | `KeyError: 'schema_version'` | `core/pit_data.py:612` |

## Runtime and source identity

Both worktrees were clean before the runs and detached at their specified
revisions. Runtime identity was identical:

- Python executable: `C:\Users\llong\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe`
- Python `3.13.14`; Windows 11 `10.0.26200-SP0`
- pytest `9.0.2`; pandas `3.0.1`; NumPy `2.4.2`

The failing test file and both failure-origin files have identical Git blob
identities at the baseline and integrated revisions:

| Path | Git blob OID at both revisions | SHA-256 of Git blob bytes |
| --- | --- | --- |
| `tests/test_fundamental_input_parity.py` | `1abf583a85e935c7633bded94f340e993e84564c` | `3166af8f35d08046854f838a86d978c3a52a76b5c1a421bb31ca30644066973e` |
| `core/backtest_engine.py` | `89fecca34430fabcb9714bfad8534872bb77a9e4` | `b61ef7de77f462512c4f3da5124d1d7f8e9c8d9f4e320695ddb0e1ba4618c929` |
| `core/pit_data.py` | `e602ae9b5e2d68d66169c1b1088ace715cf9ed3e` | `5a57be532f0fd66f8eedceff4d730ba68891513391019976271ddaac7aa40a89` |

`core/pit_feature_snapshot.py` is the changed #71 production path: its blob
changed from `6a0264d08f7a3eeac84ddf3666d459cc4af857c4` to
`8e4020316d13c3de36dac8995fe4743bf888e70c`. None of the three failure traces
enters that module. The test file and both reported failure-origin files are
unchanged, and the same failures reproduce at the accepted baseline.

## Conclusion and four statuses

- **Implementation:** The #71 correction remains integrated. This audit adds
  only evidence documentation; no production code changed.
- **Required inputs:** Both requested revisions and the same test/runtime
  environment were available. No external or vendor data was required.
- **Acceptance evidence:** Direct baseline execution confirms all three
  failures predate #71 and persist at `4bf32a3`; exact tests, errors, runtime,
  and portable Git blob identities are recorded above and in the JSON receipt.
  These failures were not fixed or represented as passing.
- **Dependencies:** The accepted #66 start prerequisite is satisfied. This
  audit creates no new dependency.

This report does not close or otherwise update the GitHub issue.
