# Historical-06 fiscal adjacency v3 addendum

This addendum records the follow-up to the Historical-06 #71 validation. It
documents the fiscal-quarter adjacency correction and its development-bundle
output comparison. The original v2 report and JSON remain historical records
and are unchanged.

## Identities and inputs

- Accepted feature contract: `historical-feature-specification-v1` (#66).
- Implementation revision:
  `0ce6561bd478bcd98ec275d28d63753bd812e114` (Fix fiscal quarter adjacency in
  acceleration).
- Calculator identity: `pit-financial-features-v3`, declared by
  `core.pit_feature_snapshot.FINANCIAL_FEATURE_CALCULATOR_ID`.
- Policy interface version remains V3; historical data format remains V2.
- Input is the same retained development bundle used by the v2 report:
  `C:/Projects/trading_bot/RS-momentum-EMA-trading-bot/.artifacts/data/development-sp500-v2/pit_bundle.sqlite3`,
  SHA-256 `cf729b47d762ae86287bb8a87c28a1664e7475c74ef1aca736fbc240111349de`.
  Its manifest SHA-256 is
  `3c9edd0158dcb93f9de5edbfcf6f281dd8b25bcdd1ded6ad21473af5fd37be7c`, and
  the manifest bundle hash matches.

## Rule and correction

The #66 fiscal year-over-year matcher is unchanged: match the closest
prior-calendar-year period within 28 calendar days; ties, conflicting
duplicates, and unmatched observations are unavailable. Acceleration remains
the newest matched YoY growth rate minus the immediately preceding matched
YoY growth rate.

The source has period-end dates but no fiscal-quarter sequence number. The old
calendar-quarter ordinal check could accept a 182-day skipped-quarter gap
(`2023-04-01` to `2023-09-30`) and reject a 91-day adjacent 52-week fiscal
quarter pair (`2023-07-01` to `2023-09-30`) because both endpoints fall in
calendar Q3. The v3 rule treats a 84–105 day endpoint gap, inclusive, as
adjacent. This 12–15 week cadence accommodates regular 13-week quarters and a
14-week 53rd-week quarter with one week of endpoint tolerance. Gaps outside
that interval make acceleration unavailable. This is an explicit endpoint
cadence proxy because the input does not provide fiscal-quarter numbers.

Fixed regression cases verify that the 91-day pair returns −0.30 for both EPS
and revenue acceleration, while the 182-day skipped-quarter pair returns
unavailable for both metrics. The fiscal YoY matching behavior and scoring
formulas were not changed.

## Retained-bundle output comparison

The v2 calendar-quarter implementation and v3 cadence implementation were
compared on the same bundle, same fiscal matcher, and same 2021-01-01 through
2025-12-31 evaluation period. Each metric has 8,696 priced quarterly-state
windows. The bundle contains 39,470 quarterly records, 565 symbols, and
720,785 priced security-sessions across all symbols.

| Feature | Windows changed | Symbols affected | Priced security-sessions changed |
| --- | ---: | ---: | ---: |
| Earnings acceleration | 68 | 47 | 6,007 |
| Revenue acceleration | 77 | 54 | 6,876 |

For EPS, 24 windows changed from available under v2 to unavailable under v3;
44 changed from unavailable under v2 to available under v3. The 68-window
total counts output changes in either direction. For revenue, 27 windows
changed from available to unavailable and 50 changed from unavailable to
available. A later hash-pinned reproduction found that the initial v3 addendum
had incorrectly recorded zero revenue changes; the table and machine-readable
counts here reflect the reproduced 77-window result. These are feature-output
comparisons only; they do not establish feature coverage, policy consumption,
strategy or portfolio impact, production-universe behavior, or retrospective
trading error.

## Reproduction recipe

The one-off scan used for the counts above was not retained with its exact
command or runtime versions. The read-only recipe is now retained at
`tools/reproduce_issue71_adjacency_comparison.py`. From the repository root,
run:

```powershell
python -m tools.reproduce_issue71_adjacency_comparison --bundle "C:/Projects/trading_bot/RS-momentum-EMA-trading-bot/.artifacts/data/development-sp500-v2/pit_bundle.sqlite3" --manifest "C:/Projects/trading_bot/RS-momentum-EMA-trading-bot/.artifacts/data/development-sp500-v2/bundle_manifest.json" --evaluation-start 2021-01-01 --evaluation-end 2025-12-31
```

The script defaults to the recorded bundle and manifest SHA-256 values. It
validates both digests and the manifest-to-bundle binding, opens the V2 bundle
read-only, evaluates quarterly snapshots at each distinct quarterly
`public_date`, and counts ticker price rows in each half-open state window up
to the next quarterly `public_date`. A window is included for a metric when it
contains at least one price row. The old side uses the v2 calendar-quarter
ordinal guard; both sides share the accepted #66 28-day fiscal matcher and
growth formula. The JSON result records the bundle hashes, code revision,
calculator identity, Python, pandas, and SQLite versions.
Before emitting JSON, the script compares quarterly-row and price-session
totals, state-window denominators, changed windows, affected symbols, direction
counts, and affected sessions against this addendum; it exits with an error on
any mismatch.

The script was added after the original comparison and this follow-up did not
retain the original scan's runtime identity. The reproduction run below
captures its own Python, pandas, and SQLite versions, exact script and
calculator source hashes, source revision, and worktree state.

The first full attempt exposed two different issues. Its all-bundle-date price
count was a script-method error; the query now clips price rows to the
evaluation interval. Revenue's 77 changed windows are an actual difference
between the v2 and v3 calculator outputs under the shared matcher. The initial
addendum's zero cannot be traced to a specific mistake because its one-off scan
code was not retained. The earlier v3 addendum at commit
`675df31e4facaa260f78a579524a713f1a38971f` (JSON SHA-256
`7324a21f4004c1f9e824179b6e5c2416b72dca52b9815a7048950bfe4109c8e1`) is
superseded for that revenue result. The original v2 report remains unchanged.

The corrected pinned run passed the report-count check in **799.531 seconds**.
It reported CPython 3.13.14, pandas 3.0.1, SQLite 3.50.4, bundle SHA-256
`cf729b47d762ae86287bb8a87c28a1664e7475c74ef1aca736fbc240111349de`, manifest
SHA-256 `3c9edd0158dcb93f9de5edbfcf6f281dd8b25bcdd1ded6ad21473af5fd37be7c`,
and reproduction-script SHA-256
`bfec250376d6e03208ee83664513911f45adda271cb1522da918efc0166a5d50`. The
complete failed-attempt receipt and successful output are retained in
[`historical-financial-validation-issue71-adjacency-v3-reproduction.json`](/C:/Users/llong/.codex/worktrees/f1ba/RS-momentum-EMA-trading-bot/docs/historical-financial-validation-issue71-adjacency-v3-reproduction.json).

The recipe CLI import/help path, four focused count and identity tests, and
Ruff check pass. No calculator implementation or semantics changed in this
reproduction correction.

## Verification and status

- `tests/test_historical06_financial_semantics.py` and
  `tests/test_task11_earnings_provenance_contract.py`: **31 passed**.
- Selected #66 fiscal matcher tests in
  `tests/test_fundamental_input_parity.py`: **11 passed, 27 deselected**.
- Ruff and `git diff --check`: passed.
- The full offline suite is not clean. An earlier full-suite run was stopped
  after unrelated failures; the first `-x` failure was
  `tests/test_agent_loop.py::test_policy_worker_command_mount_allowlist_and_cleanup_use_only_fakes`
  (`ValueError: policy interface version is unsupported`) in unchanged
  `core/strategy_policy/worker.py`. A separate broad parity run had three
  failures in unchanged `core/backtest_engine.py` and `core/pit_data.py`.

The implementation is committed as
`0ce6561bd478bcd98ec275d28d63753bd812e114`. This addendum supplies the v3
identity and output measurements for review; it does not assert issue #71
acceptance. The original
`docs/historical-financial-validation-issue71.md` and `.json` remain
unchanged.
