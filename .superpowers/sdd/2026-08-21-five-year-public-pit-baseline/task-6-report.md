# Task 6 report — immutable PIT baseline runner

Date: 2026-08-22

## Outcome

The Task 6 orchestration/reporting seam and its first independent-review fix
round are implemented. A local, provider-free smoke exercised successful
immutable publication, reconciliation failure closure, and coverage-gate
failure closure. No real five-year result was generated or claimed because the
official SEC fundamentals artifacts and production bundle are still absent.

## Implemented contract

- The CLI enforces exactly 2021-01-01 through 2025-12-31, SPY, top 100, and a
  20-session basket rebalance; bundle evaluation/warm-up/cutoff dates must match.
- The runner requires the exact hash-bound membership, price, and Task 2
  provenance plus `fundamentals_coverage.json`, `security_master.csv`, and
  `security_master_exclusions.csv`. It verifies all artifact and SEC archive
  digests against the bundle before use and again before manifest publication.
- Coverage includes membership counts, full symbol/CIK mappings, closed
  exclusions/reasons, public-date fallback counts, price facts, and evaluated
  strict-PIT symbol/date quarterly-plus-annual usability independently recomputed
  from the bundle through the unchanged C/A evaluators. Logged growth must exactly
  agree with those hash-bound results. The 495–510 member,
  complete SPY, >=98% price, >=95% resolved-or-closed CIK, and >=90% evaluated
  fundamental gates are mandatory. A failure writes complete `coverage.json`
  and `run_failed.json`, then stops without summary, report, or complete manifest.
- `PortfolioSimulator(pit_bundle=bundle)` uses production defaults without
  overrides. The independent basket uses exactly 100/20/252/60/100,000.
- Entry reconciliation pairs each BUY with one unique qualifying signal from
  the immediately prior benchmark session. Attempted entries must equal
  executions plus mutually exclusive rejection counters; market/regime blocks,
  capacity truncation, and final-session pending signals reconcile separately.
  Per-symbol cash/capacity attribution is emitted only where aggregate artifact
  evidence is unambiguous; any unattributed block fails closed, and the fixed
  uncapped baseline rejects every capacity block. No ticker-level reason is invented.
- Missing fundamentals are counted only as missing data. Numeric gates fail only
  on present values below threshold, and boolean gates fail only on explicit
  false values.
- Five-year and rolling recall aggregate only approved same-issuer chain aliases;
  successor resets remain separate.
- Transactions replay chronologically against the hash-bound identity contract.
  A predecessor transaction after transition, an open cross-boundary position
  without an implemented re-key, or an unsupported/reset identity fails closed.
- Empty, non-finite, non-positive, misaligned, or schema-incomplete engine/basket
  artifacts are rejected. The manifest records full source provenance, exact
  input hashes, date contract, active CANSLIM config, execution diagnostics, Git
  HEAD, and reconciliation. Git/input/config/diagnostic state is rechecked just
  before the manifest-last commit marker.

## Synthetic smoke evidence

An ignored local fixture created a schema-v1 bundle with 495 synthetic members,
2021–2025 daily sessions, hash-bound standalone identities, complete synthetic
Task 2 audit inputs, and inert finite fundamental availability. Fake simulator
factories returned nonempty daily curves, holdings, one signal/next-session
entry, complete production-shaped config/diagnostics, and one basket entry.

- Success: PASS — 14 required artifacts, all coverage gates passed, exact config
  and diagnostics in the manifest, manifest written last, no failure marker.
- Reconciliation failure: PASS — `entry_attempts` mismatch prevented publication
  and wrote one closed `run_failed.json`.
- Coverage failure: PASS — missing quarterly/annual evaluation data wrote only
  complete `coverage.json` plus `run_failed.json`; no summary, report, or manifest.
- Bundle/log cross-check: PASS — fabricated finite logged growth was rejected
  when it differed from the unchanged evaluators over hash-bound as-of frames.
- Non-finite growth: PASS — non-null infinite logged/evaluator growth is rejected
  before missing-value or equality comparisons.
- Capacity attribution: PASS — one truncated signal with no exact per-symbol
  attribution was rejected instead of publishing a zero leader block count.
- Rename recall probe: PASS — FI rolling label recalled an approved prior FISV
  signal while successor resets remain excluded by the alias-map construction.
- Missing-data probe: PASS — one missing row produced no gate failures and one
  missing-fundamentals count; a present below-threshold/false row produced gates.
- Identity probe: PASS — a BUY of a predecessor after its reviewed transition was
  rejected.
- `python -B -m py_compile pit_baseline.py core/pit_baseline_report.py`: PASS.
- `python -m ruff check pit_baseline.py core/pit_baseline_report.py`: PASS.
- `git diff --check`: PASS.

Final independent re-review passed with no findings on exact runner/helper hashes
`835F34DC...` / `9DC38315...`. Real Task 6 functional acceptance remains blocked
on the official SEC extraction and production PIT bundle; the smoke is
orchestration evidence, not a performance result.
