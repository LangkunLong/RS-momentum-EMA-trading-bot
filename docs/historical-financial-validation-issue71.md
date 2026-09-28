# Historical-06 financial validation result

Issue #71 validates annual growth, quarterly acceleration, ROE, freshness,
fiscal matching, and revision timing against the accepted #66 contract. This is
a local implementation and evidence report. The later bounded lead review is
recorded below; GitHub publication and issue closure remain separate.

## Identities and inputs

- Starting revision: accepted upstream `main`, `2c01e76a878a338ab2b743c38c4f1310aab3f75b` (PR #110).
- Implementation commit: `3d8a0edbc793b1a435849daead96ffaa9c165d97`.
- Accepted feature contract: `historical-feature-specification-v1` (#66).
- Corrected calculator identity: `pit-financial-features-v2`, declared as
  `core.pit_feature_snapshot.FINANCIAL_FEATURE_CALCULATOR_ID`.
- Policy interface remains V3; the retained development bundle is historical
  data format V2. These version dimensions are unchanged.
- Development input: original-checkout
  `.artifacts/data/development-sp500-v2/pit_bundle.sqlite3`, SHA-256
  `cf729b47d762ae86287bb8a87c28a1664e7475c74ef1aca736fbc240111349de`.
  Its manifest declares the same bundle hash; manifest SHA-256 is
  `3c9edd0158dcb93f9de5edbfcf6f281dd8b25bcdd1ded6ad21473af5fd37be7c`.
  The recorded fundamental source is SEC EDGAR official bulk archives, source
  identity `2f653eb5b91b4be79a2b33cd5b43e01749a25a9e5f74581a7f212bb6bd6bd56b`.
- The retained SEC provenance file (SHA-256
  `79a4ff05e9ee18422200a9b1ebdbf28573b1e7d11f7b9c6268b69e710d7873d0`)
  states that normalized `public_date` is the first supplied SPY trading day
  strictly after the SEC acceptance calendar date, with filed-date fallback.
  Freshness therefore uses that stored first-usable session without another
  shift. Other adapters still need to declare and map their date convention.

The development bundle is S&P-only and does not establish production
universe coverage. The historical output counts below describe calculated
feature values on this exact bundle. They do not establish feature coverage,
policy consumption, strategy impact, or retrospective trading error. No
historical report or retained evidence was rewritten.

## Reproduced findings and dispositions

1. **Skipped quarterly period in acceleration — corrected.** The old V3
   calculator subtracted the two newest matched YoY growth observations even
   when their current fiscal quarters were not adjacent. The fixed history
   `2023-06-30=0.8`, `2024-06-30=1.0`, `2023-12-31=1.0`,
   `2024-12-31=1.5` produced `25%` and `50%` YoY rates; the old result was
   `+0.25` (25 percentage points), despite the missing Q3. The V2 calculator
   now returns unavailable unless the newest two matched current periods occupy
   consecutive calendar-quarter slots. EPS and revenue use the same guard.
   Consecutive Q3/Q4 matches still produce the expected `1/6` acceleration in
   the fixed adjacent-quarter case.
2. **Freshness with an empty newer period or missing provenance — corrected.**
   Age now comes from the newest quarterly period with at least one observed
   supported fact. An empty newer period cannot make an older observation look
   fresh; if no quarterly fact is observed, age is `None`. An observed period
   without a public-date entry fails closed. A normalized first-usable date is
   not shifted twice.
3. **Annual growth across a skipped fiscal year — accepted modeling
   disposition.** With annual EPS `2020=1.0`, `2021=1.2`, `2023=2.4`, the
   current A evaluator reports `1.0` (100%) and its trace identifies `2023`
   versus `2021`. #66 explicitly retains adjacent available annual reports and
   says the period gap must be identified when comparing to a source's
   one-year growth. The evaluator is unchanged; this value is not described as
   one-year growth when the trace periods span two fiscal years.
4. **ROE income/equity period mismatch — accepted baseline formula, explicit
   limitation.** With latest annual net income `120` for FY2024 and latest
   visible equity `800` at `2024-03-31` (while FY2023 equity was `400`), the
   existing formula returns `15%` using `120/800`. #66 preserves latest annual
   net income divided by the latest nonmissing equity observation and does not
   require period matching or averaging. The evaluator remains unchanged; the
   two periods must remain visible when interpreting ROE.
5. **Restated older period — preserved as-of behavior.** Annual EPS `2023=1.0`
   and `2024=1.4` produces `40%` before a `2025-03-01` restatement of FY2023
   to `0.8`; a snapshot after that date produces `75%`. The earlier snapshot
   and its original public date remain `40%`; only the later as-of view uses
   the restatement.

Quarterly fiscal matching itself remains the accepted #66 rule: closest
prior-calendar-year period within 28 calendar days, with unmatched, conflicting
duplicate, or tied matches unavailable. Existing fixtures for the 28/29-day
boundary, ties, duplicates, unmatched newest quarters, and 53-week periods
remain in force.

## Retained-bundle output comparison

The comparison re-evaluated each quarterly state window in the 2021-01-01 to
2025-12-31 development period using the same fiscal matcher and input bytes.
Each affected count below is a feature-output window with at least one priced
session before the next quarterly state, plus its affected priced
security-sessions.

The per-feature denominator is 8,696 priced quarterly-state windows. The
price-session domain for tickers with quarterly fundamentals contains 685,301
priced security-sessions; the bundle has 720,785 priced security-sessions
across all symbols, including reference funds.

| Feature | State windows evaluated | Windows changed | Symbols affected | Priced security-sessions changed |
| --- | ---: | ---: | ---: | ---: |
| Earnings acceleration | 8,696 | 2,222 | 529 | 138,038 |
| Revenue acceleration | 8,696 | 2,555 | 524 | 158,883 |

Every changed acceleration output was previously non-null and is now
unavailable because the two newest matched periods skip a quarter. Adjacent
period values did not change. Reproducible bundle case: ticker `A`, available
from `2021-03-03`, periods `2021-01-31` and `2020-07-31`; earnings acceleration
changed from `+0.4095238095` to `None`, and revenue acceleration from
`+0.1509557397` to `None`, across 62 priced sessions. This is a development
feature-output measurement only.

The bundle has 39,470 quarterly records, zero rows with all supported snapshot
facts missing, and zero rows missing period/public date. The freshness fix
therefore changes zero outputs on this bundle. Fixed cases exercise that
missing-observation behavior independently.

## Focused verification

- `tests/test_historical06_financial_semantics.py` plus
  `tests/test_task11_earnings_provenance_contract.py`: **29 passed**.
- Selected existing fiscal matcher checks in
  `tests/test_fundamental_input_parity.py`: **11 passed, 27 deselected**.
- Ruff and `git diff --check`: passed.
- A broader run of `tests/test_fundamental_input_parity.py`: **35 passed, 3
  failed**. Two failures raise `ValueError: entry market context is invalid`
  from unchanged `core/backtest_engine.py`; one raises `KeyError: 'schema_version'`
  from unchanged `core/pit_data.py`. These paths are outside the #71 diff and
  are not represented as passing. The full offline suite was not run.

## Four issue statuses

- **Implementation:** The bounded acceleration and freshness corrections,
  stable calculator identity, fixed cases, and this report are committed on
  `codex/issue-71-fiscal-semantics` at `3d8a0ed`. Annual growth and ROE remain
  unchanged per #66, with their modeled limitations captured above.
- **Required inputs:** The fixed histories and exact retained development
  bundle were available. Complete vendor history is not required. Production
  data and production-universe acceptance are outside #71.
- **Acceptance evidence:** Reproductions, before/after feature-output counts,
  identities, and focused checks are available for lead review. The three
  broader parity failures above remain unresolved outside this file scope;
  the original worker receipt did not assert acceptance; the later bounded
  lead review appears below.
- **Dependencies:** Satisfied for start and this scoped result. The accepted
  #66 contract is present at the starting revision; no additional unconditional
  child dependency applies.

`EntryFeaturesV3` does not itself serialize the calculator identity. New
coverage/research artifacts must bind `pit-financial-features-v2` through the
shared feature identity owned by #80; the #67 owner has been notified. Older
artifacts remain bound to their original source identities.

## Bounded acceptance review

The lead reviewed the published #71 criteria on the integrated source. The
bounded financial-semantics validation is accepted locally:

| Published criterion | Review result |
| --- | --- |
| Skipped periods, adjacent-quarter acceleration, income/equity alignment and restated older periods | Pass. Fixed cases and period traces cover each condition. |
| Each observed mismatch has a reproducible case and bounded correction or documented modeling decision | Pass. Skipped-quarter acceleration and empty-newer-quarter freshness were corrected; skipped annual years and unmatched ROE periods have explicit accepted #66 dispositions. |
| Historical impact measured before retrospective error claims; changed definitions versioned | Pass. The same retained development bundle was evaluated before and after the correction, with 138,038 earnings-acceleration and 158,883 revenue-acceleration priced security-sessions changing to unavailable. These are feature outputs, not trading errors. Calculator identity is `pit-financial-features-v2`. |
| Corrected identity and newly comparable results, with old reports preserved | Pass for the named development feature comparison. The #67 measured report binds calculator v2 and its Git blob; prior source-specific reports were not relabeled. No strategy-performance comparison is inferred. |

The three broader `tests/test_fundamental_input_parity.py` failures are
separately reproduced on accepted starting main `2c01e76` and integrated
source `4bf32a3` in the [baseline audit](historical-financial-parity-baseline-audit-issue71.md).
The failing tests and their `core/backtest_engine.py`/`core/pit_data.py` origin
blobs are identical at both revisions; none of the traces enters the changed
#71 calculator module. The failures remain real and unresolved, but they
predate #71 and do not block this bounded semantics acceptance. GitHub issue
closure remains pending publication of the integrated work.
