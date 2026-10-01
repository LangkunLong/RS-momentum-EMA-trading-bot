# Issue 69 final bounded assessment review

Review date: 2026-10-01. Reviewer independent of the implementation owner. Scope: specification and quality review of frozen owner commit `d24dc9ccc7fe6b08f7e07a1f7f38a3772194849c`, base `ab385d792e19ff6db39d87f1123f47f660fc1e1d`, worktree `C:/Users/llong/.codex/worktrees/3d87/RS-momentum-EMA-trading-bot`. HEAD and clean worktree independently confirmed. Exactly three added files are in the diff; product code and previous reports are unchanged.

## Verdict

**Accept the bounded retained-source assessment. No remaining material specification or quality finding. Full issue #69 is not ready for closure.** Complete production inputs, authenticated identity/action reconciliation and source-use basis remain missing. This verdict is limited to the owner commit; the lead must retain identity equivalence when integrating it.

## Exact reviewed artifacts

| File | Independently measured SHA-256 |
| --- | --- |
| `scripts/assess_issue_69_retained_prices.py` | `70746f8d683b21c453b7bccbc16e4c5ba2616182013e3d49476e2b9bcfd4d903` |
| `docs/issue-69-bounded-source-assessment.md` | `3f43188f8071e93fef9be88e2c7bbf11484d79675dbed5aa4d3cc1829a45af2e` |
| `docs/issue-69-bounded-source-assessment-receipt.json` | `efe5cbe05902a655d8511889537bb167af3f9b0da607e0b4e9d14587d3e1612c` |

The executed script hash in the receipt equals the final reviewed script bytes. The receipt truthfully retains execution HEAD `ab385d792e19ff6db39d87f1123f47f660fc1e1d`, tree `46a4b97a29f5fe05b395fc4ff5e36d6451db9bdc`, staged/worktree status before receipt write, command, Python 3.13.14 executable and Windows runtime. It does not falsely claim the final owner commit was the pre-commit execution HEAD. The dedicated script has only standard-library runtime dependencies and pins all thirteen inputs before parsing. Frozen script compiled in memory successfully; no worker file was changed.

## Specification and criterion checks

Exact launch issue body `.artifacts/coordination/issue-69-launch-body.json`, accepted #66 scope and retained #68 boundaries reviewed.

1. **Coverage:** denominator is retained membership event replay projected through the dated investigation map onto retained SPY sessions, independent of price presence. Independently summed per-ticker and per-date receipt partitions each yield 631965 expected, 631782 present, 183 missing. Explicit missing pairs and ranges are retained. The report correctly calls 606 a projected ticker-label count, not a stable lineage count. Production denominator validity remains unclaimed.
2. **Warm-up and prior rows:** 505 labels from the first 2021 snapshot multiplied by 253 earlier sessions gives 127765 pairs. This is explicitly a lookback cohort, not authenticated 2020 membership. Prior-row threshold keys and prose now state that counts include zero volume, do not require an evaluation-date row and do not establish contiguous bars or feature/evaluator readiness. The initial 260-row deficit and January 13 threshold date are explicit. Earlier findings resolved without redefining measured observations.
3. **Identity/action treatment:** renamed/merged/reused-symbol cases are separately classified with exact interval/copy/boundary evidence. Static descriptive classifications are visibly bounded investigation-map interpretations; dynamic row equality, overlap and sampled OHLCV evidence are separately measured. FISV is unresolved, COG/CTRA bounded map/copy support only, PEAK/DOC price path unresolved despite map support, PSKY predecessor continuity excluded. Membership removals do not stand in for delisting or spin-off facts.
4. **Adjustment/reference history:** source declarations are distinguished from fresh reproduction. SPY/QQQ/IWM have calendar-complete retained histories, with the initial warm-up deficit explicit. Identical raw/split/cutoff bytes cannot independently prove adjustment reconciliation. Source-use rights, original acquisition chunks and full union remain gaps.
5. **Four statuses:** bounded implementation/evidence completion is distinct from incomplete full required inputs and #68 dependency. The report explicitly keeps #69 open. No provider activity, accepted #67 rerun, product change, production admission or strategy qualification is claimed.

## Targeted independent reproduction

To resolve the material retained audit conflict, independently rehashed the 43650461-byte price CSV as `dd18e38d14356df2be9aea79bc777407d40750305dcf327a2f3552815c39c376` and streamed only FI/FISV records into the targeted comparison for 2023-06-07 through 2025-11-10. The 610 overlapping sessions contain **zero** exact five-field OHLCV matches. The small retained provenance declares `successor_audits.FI.exact_overlap_row_count = 610` with those same bounds. Thus the final report's conflict is independently reproduced, rather than inferred from the worker's conclusion. This targeted check did not rerun the full coverage or feature report.

Receipt quality partitions explicitly preserve 1430 zero-volume rows, 23 covered evaluation rows with zero volume, 610 identity-overlap covered rows and separate sensitivity calculations. They are not silently removed from the original row-presence numerator or promoted to usable prices.

## Remaining limits

This review does not authenticate secondary membership, clear data rights, acquire missing price/action sources, resolve FISV discrepancies, or establish complete production-union coverage. Source data gaps are report outcomes, not defects in this bounded assessment. Original #67/#71/#82 definitions and evidence remain intact. Full #69 criteria remain partial/unresolved exactly as recorded in the report.
