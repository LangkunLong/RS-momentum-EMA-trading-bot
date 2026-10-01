# Issue 70 frozen-package review

Review date: 2026-10-01. Frozen owner HEAD `174faca79cecfa0a710bbc4112fd2c81f225f281`, base `ab385d792e19ff6db39d87f1123f47f660fc1e1d`; worktree `C:/Users/llong/.codex/worktrees/f1ba/RS-momentum-EMA-trading-bot`. HEAD and clean worktree independently checked. Specification/quality review only; no worker implementation or source evidence edited.

## Verdict

**Changes needed for the bounded assessment:** the source scalar trace still does not validate the exported period/unit/statement basis. Full #70 remains not ready independently of this finding. The remaining production input gaps do not prevent completion of a truthful bounded assessment after the trace finding is resolved.

## Artifact identities

| Artifact | Independently measured SHA-256 |
| --- | --- |
| `tools/assess_issue70_retained_source.py` | `601527fed1c6b408e3fe1057f141f2c3909c3775561814c3804d7246d1376de0` |
| `docs/issue-70-bounded-source-assessment.md` | `c5161bb64a4a006c2587893eb8050d21077125cdde6a4c26055d55c3088472b3` |
| `docs/issue-70-bounded-source-assessment-receipt.json` | `1fc74971cee7c30430ea8efbecdf615f3d8c113849a41c66ec52445d0e3c5733` |

The receipt execution script digest equals the frozen final script bytes. Receipt records Python 3.13.14, pandas 3.0.1, Windows, execution base ab385d7, fiscal matcher blob `14c3c994f70a06f7897288a0d397672dbf897d89`, and accepted V3 calculator blob `71e562ffc00dc4e617f1cd625e3f13ea5fe3f81a`. Earlier measurements are not claimed to have run on the later packaging commit. No large archive digest was independently repeated in this review.

## Material finding: source trace accepts wrong period and unit

Location: `tools/assess_issue70_retained_source.py`, `analyze_bounded_archives`, candidate index around lines 1211–1240 and `matching_values` around lines 1357–1363.

The source key contains accession, concept, form, filed date, fiscal year and fiscal period. Each filing can contain several comparative periods, and duration facts can share an end date while representing different spans. The current match only checks numeric equality with tolerance. It does not require the candidate's `period_end` to equal the exported period, a duration compatible with the quarterly/annual statement, an instant for balance values, or the expected USD / USD-per-share / shares unit. Recording unit and period in a few examples does not validate them for all 1946 claimed traces.

Independent in-memory negative reproduction called the frozen function with a one-issuer fake ZIP pair; no real archive was opened and no file was created. The export/audit key was annual EPS for `2023-12-31`, public `2024-02-02`, value 2.0. Its matching origin had only an annual fact for `2020-12-31`, unit `USD` rather than `USD/shares`, value 2.0. The function reported `csv_value_found_in_origin_facts: 1` and a successful one-row exact trace, while its example listed source period 2020 and export period 2023. This proves that a false period/unit match is currently counted successful.

Required bounded correction: filter/partition candidates by exact exported period end, source namespace and expected unit, and the exporter's statement-duration/instant rule; preserve ambiguity/missingness rather than selecting an arbitrary equal number. Inherited facts remain tied to the same exported statement period under the exporter, so inheritance is not permission to ignore the period. Record exact source candidate locators or sufficient stable fields to reproduce matching and distinguish numeric-only matches from compatible scalar matches. Regenerate the small bounded trace and receipt/report counts. Retain prior results under their actual semantics; an unchanged full archive hashing rerun is not needed solely to exercise this correction if an exact retained-byte/member identity check is preserved.

## Report clarity correction

The coverage table labels rows as quarterly and annual metrics while its nonempty-row column repeats totals across all statement types (e.g. 54423 basic EPS rows, exceeding the 42000 quarterly rows). Either label that column explicitly as all-statement scalar presence, or use statement-specific counts already in the receipt. This is a reporting correction, not a request to rerun coverage.

## Prior findings resolved / positive checks

- Annual profiles now use adjacent available annual observations rather than the quarterly year matcher; missing-value policy and three-growth-slot requirement are explicit.
- Quarterly profiles and acceleration preserve missing latest-period slots, retain the accepted 28-day fiscal matcher and 84–105-day cadence, and separately report EPS/revenue input availability without scores.
- ROE availability now uses balance rows and latest nonmissing equity by period end, with annual net-income presence; no ROE numeric claim is made.
- Shares CompanyFacts lookup uses `dei:EntityCommonStockSharesOutstanding`.
- Timing partitions now count missing/invalid separately, select recorded acceptance/fallback basis and avoid a second availability shift. Receipt reports 145010 matches and zero mismatches/missing timing. Calendar clipping of old filings to the first supplied 2020 session is explicitly not called a publication delay.
- Export/audit join checks every ordered `(ticker, statement_type, period_end, public_date)` key, with receipt 145010 exact pairs and no mismatch/trailing export row.
- Q4 fiscal tags, calendar frames, quarterly output rows and arithmetic candidates are now distinct. The report acknowledges source-basis reconciliation gaps rather than manufacturing Q4 values.
- Two source generations and the publication-marker mismatch/reconstructable metadata are retained distinctly. Original #67 inputs are unchanged; no alternate audit is attached to the acquisition CSV. Missing declared identity manifest and absent old audit/coverage bytes are explicit.
- Three-issuer archive sampling is bounded with member size/hash identities and recent-submission coverage limits. The 1946 numeric-origin matches are a useful intermediate measurement but need the material correction above before they can establish scalar correctness.
- Four statuses and criterion map honestly keep full #70 partial/open, including production #68 dependencies, foreign coverage unknown and no universal Q4 reconciliation.

## Completion boundary

After the narrow findings are fixed and reviewed, the agreed bounded scope can be complete even though full-production #70 remains partial. No full suite, prior #67/#71 report, provider acquisition, operational runtime or archive-wide extraction is required to resolve these review findings. Await corrected exact source/receipt before granting bounded acceptance.
