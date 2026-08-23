# SDD ledger — plan: docs/superpowers/plans/2026-08-23-canonical-canslim-entry.md

Workspace: `C:/Projects/trading_bot/RS-momentum-EMA-trading-bot/.worktrees/canonical-canslim-entry`
Branch: `codex/canonical-canslim-entry`
Starting commit: `a022867c4cb666ab2401c9caa06efc065256ec32`
Spec: `docs/superpowers/specs/2026-08-23-canonical-canslim-entry-design.md`

## Preflight

- Root `main` and the isolated feature worktree were clean at exact starting commit `a022867`.
- `.worktrees` is ignored; implementation is isolated from the operator's main checkout.
- Workspace runtime: `C:/Projects/trading_bot/RS-momentum-EMA-trading-bot/.agent-venv/Scripts/python.exe`, Python 3.13.14.
- Known pre-change broad baseline at the same commit: `1284 passed, 9 skipped, 2 deselected`. Per the operator's explicit workflow, do not rerun the broad suite until functional replays complete.
- Preserved PIT bundle SHA: `8ca8242dd67db30d456a2b1861f7e7399f8ca418079738ab150d4e44865763c5`; first replay must be fresh and must not resume the preserved five-day checkpoint.

## Binding rulings

- Ruling: keep all strategy thresholds unchanged for the first aligned replay. Cost if wrong: the run may still have low recall, but the measurement remains causal and does not conflate correctness with optimization.
- Ruling: completed-session volume uses exactly 50 prior bars and excludes the event bar; pivot uses only prior closes. Cost if wrong: the first corrected run may differ from current live approximations, but all callers converge on one auditable definition.
- Ruling: the current power-gap detector is not earnings-grounded and cannot bypass the executable CANSLIM contract. Cost if wrong: a genuine earnings-gap setup may be missed until a separate PIT earnings-event feature is designed; no false earnings claim enters this baseline.
- Ruling: market regime is execution permission, not setup qualification. Cost if wrong: bearish-market qualified setups remain visible but unexecuted, improving diagnostic separation.
- Ruling: because the legacy total score includes M, full entry qualification uses a separately named non-M composite built from the same active component weights and renormalized to 100%; the M-inclusive total remains unchanged for reporting. Cost if wrong: the entry score can differ from historical reports, but market cannot silently veto setup qualification twice.
- Ruling: bind daily cadence only in `pit_baseline.py`; keep the generic simulator default, technical-only mode, and simple-backtest CLI compatible. Cost if wrong: non-PIT workflows retain historical cadence, but the canonical baseline proves its own daily setting.
- Ruling: next-open zone failures need per-attempt audit rows, not aggregate-only inference. Cost if wrong: a modest artifact/checkpoint schema increase; reconciliation remains causal.
- Ruling: functional probes and real replays precede broad tests, following the operator's explicit instruction. Cost if wrong: some test regressions surface later; per-task functional probes and independent reviews mitigate that risk.
- Ruling: CAH is a finite-value evaluator bug; do not alter EPS fallback policy. XOM is an exact reviewed CIK correction and requires fresh SEC outputs/bundle. Cost if wrong: a narrower fix may leave unrelated missing-fact gaps, but it avoids silently changing scoring policy.
- Ruling: report both raw and historically exposed denominators. Cost if wrong: slightly larger output schema; recall claims become explicit and reproducible.

## Task self-consistency scan

| Task | Files/interfaces checked | Result |
|---|---|---|
| 1 | Shared pure facts -> live, after-close, simple callers | Compatible; technical-only consumers use setup facts, full callers use C/A/RS/composite decision. |
| 2 | Shared decision -> PIT signals -> next-open outcomes/checkpoints/reconciliation | Compatible; cadence and outcome schema require a fresh checkpoint. |
| 3 | Existing immutable bundle -> daily baseline artifacts | Compatible; known fundamentals coverage requires explicit `--allow-incomplete-fundamentals`. |
| 4 | C/A evaluators and reviewed SEC CIK -> regenerated SEC outputs/bundle | Compatible; XOM requires new immutable publications, CAH does not require source regeneration. |
| 5 | Leader label models -> raw/PIT-exposed report fields | Compatible; denominators already exist as `member_at_start` and `member_at_evaluation`. |
| 6 | Rebuilt bundle/report schema -> corrected baseline | Compatible after Tasks 4-5. |
| 7 | Completed implementation/results -> focused/full tests/docs | Intentionally last per operator instruction. |

## Cross-task interface scan

| Producer | Consumer | Shared interface | Finding |
|---|---|---|---|
| Task 1 | Task 2 | `CanslimEntryFacts` / `CanslimEntryDecision` | One definition; no legacy executable fallback permitted. |
| Task 2 | Tasks 3/6 | daily config, entry outcomes, diagnostics | Run validation and reconciliation must require all fields. |
| Task 3 | Task 4 | first functional measurements | Data fixes remain separate from entry-contract measurements. |
| Task 4 | Task 6 | fresh SEC outputs and PIT bundle | All source/output hashes must be recomputed and verified. |
| Task 5 | Task 6 | explicit recall schema | Corrected replay must publish both denominator views. |
| Tasks 1-6 | Task 7 | stable production APIs and artifacts | Tests/documentation bind working behavior rather than drive speculative policy. |

Execution order: `1, 2, 3, 4, 5, 6, 7`.

## Task 3 read-only preflight

- `verify_pit_bundle.py` passed against the preserved bundle manifest and all six bound normalized source/provenance files.
- Independent SHA-256 recomputation matched `8ca8242dd67db30d456a2b1861f7e7399f8ca418079738ab150d4e44865763c5`.
- The actual preserved manifest path is `.artifacts/pit-baseline/bundle_manifest.json`.
- The SQLite reader uses a read-only URI, and the replay performs pre/post source hashing. Windows ACLs still allow the current principal to modify the source files, so Task 3 will additionally run the exact verifier before and after the replay. A soft `READONLY` attribute is not treated as a security boundary.
- The first replay uses a fresh UTC/digest-qualified output root, no `--resume-checkpoint`, and the explicit `--allow-incomplete-fundamentals` flag for the known `80.20768935%` C/A coverage result. Any other coverage failure remains terminal.

## Task 1 — shared completed-session entry contract

- Added immutable shared technical facts/full decisions with prior-only 252-close pivot, exactly 50 prior volumes, price advance, `1.30x` volume, inclusive pivot-through-`+5%` zone, finite handling, and ordered blockers.
- Live CANSLIM now exposes both the unchanged M-inclusive `total_score` and the non-M renormalized `entry_composite_score`; screening consumes the fixed full decision and layers market permission afterward.
- After-close and the simple backtest consume shared technical setup eligibility only. The simple backtest retains its public shape and explicitly labels `BUY_SIGNAL` as a technical compatibility alias; PEG remains diagnostic and cannot bypass qualification.
- Direct boundary/caller probes passed, including market independence and PEG non-bypass. Relevant imports, compile, Ruff, and diff checks passed. Unit tests/broad suite were intentionally deferred per operator sequencing.
- Detailed evidence and compatibility notes: `.superpowers/sdd/2026-08-23-canonical-canslim-entry/task-1-report.md`.

### Task 1 independent review — fix round 1

- NOT PASS: 0 Critical, 3 Important, 0 Minor.
- Shared fact construction must reject unequal lengths and mismatched pandas indexes; a one-day-shifted close/volume pair incorrectly qualified.
- Scanner override parameters must use `max(canonical floor, caller floor)` consistently; stricter operator RS/composite thresholds were ignored.
- `backtest.py` must keep `TECHNICAL_SETUP` diagnostic but derive public `BUY_SIGNAL` from the full shared decision using its already available point-in-time C/A, RS, and non-M composite inputs. A technical-only alias cannot be consumed as a CANSLIM buy.
- Task 2 remains blocked until all three findings close and a fresh independent re-review passes.

### Task 1 fix-round re-review

- All 3 Important findings are closed; no Critical or Important issue remains.
- One Minor remains: `_tightened_floor` rejects `NaN` but not infinite caller values despite its finite-floor contract. A `+inf` override can reject every finite symbol. Close this one-line fail-closed edge before marking Task 1 complete.

### Task 1 completion

- Minor fixed at `0d051ed3d7aa759f7d229941c4daea6371714389`: every non-finite override resolves to the canonical floor; finite exact/stricter floors remain effective.
- Final independent re-review PASS: no Critical, Important, or Minor findings; worktree and diff check clean.
- Task 1 complete. Task 2 may begin.

### Task 1 independent-review fix round 1 implementation

- Closed the alignment finding with ordered length/index mismatch blockers; unequal histories and equal-length one-session-shifted pandas indexes both fail closed.
- Closed the scanner override finding by applying `max(canonical, caller)` to RS and non-M entry-composite classification floors and the same effective RS floor to bulk prefiltering. Lower 0/0 floors preserved 80/70 and 90/90 tightened; legacy fundamental/breakout strictness arguments remain inert.
- Closed the simple-backtest finding: `TECHNICAL_SETUP` remains diagnostic, while `BUY_SIGNAL` now uses the full shared decision with PIT C/A, RS, and a renormalized non-M entry composite at fixed floors. Market remains separate and PEG diagnostic only. Existing public columns were preserved and entry composite/blockers added for auditability.
- Fresh adversarial probes passed for both alignment failures, lower/stricter scanner floors and bulk prefilter consistency, all four backtest full-decision threshold failures, market independence, and PEG non-bypass. The mocked public backtest row proved `TECHNICAL_SETUP=True` with `BUY_SIGNAL=False` for epsilon-low C.
- Prior Task 1 boundary and after-close probes passed again. Unit tests and the broad suite remained intentionally deferred. Detailed fix-round evidence is appended to `task-1-report.md`.
- A fresh independent re-review is still required before Task 2 begins.

### Task 1 independent-review minor fix — finite caller floors

- Updated `_tightened_floor` to use `math.isfinite`, so `NaN`, `+inf`, and `-inf` caller values all fall back to the canonical floor while finite `0` and `90` retain canonical/tightened behavior.
- Direct probe passed: `caller=nan result=80.0`, `caller=inf result=80.0`, `caller=-inf result=80.0`, `caller=0 result=80.0`, `caller=90 result=90.0`, `tightened_floor_probe=passed` (exit `0`).
- Relevant Ruff, `py_compile`, and `git diff --check` checks passed (all exit `0`). No unit or broad tests were run.

## Task 2 — daily PIT evaluation and next-open outcomes

- Full PIT qualification now consumes Task 1's canonical entry facts and shared
  full C/A/RS/non-M composite decision. Legacy M-inclusive scoring remains
  reported, market remains separate permission, PEG is diagnostic only, and
  technical-only mode remains fundamental-free using technical setup alone.
- PIT baseline construction explicitly binds and validates daily cadence while
  the generic simulator/CLI default remains five sessions.
- Added immutable terminal outcomes for every queued entry attempt and an
  inclusive finite-pivot next-open buy-zone gate before cash/risk sizing.
  Rejection precedence and legacy missing-pivot pass-through are preserved;
  finite-pivot missing bar/Open never falls back to prior close.
- Checkpoint schema v2 persists outcomes in checkpoint and state journal,
  validates them on resume/completed-cache reads, and rejects v1. Pending signal
  date/pivot remain flattened primitives.
- Outcome-backed reconciliation now proves exact attempts/executions/rejections
  and per-symbol next-open/cash/capacity causes, with capacity truncation and
  final pending kept separate.
- PIT artifacts now include `entry_attempt_outcomes.csv` and
  `daily_entry_funnel.csv`, explicit outcome schema/count metadata, hashes, and
  summary/report funnel counts.
- Direct full-vs-technical, daily evaluation, exact lower/upper, below/above,
  missing-pivot, missing-open, one-outcome, checkpoint v1/v2, actual resume,
  adversarial reconciliation, and artifact-schema probes passed. Relevant
  imports, compile, Ruff, and diff checks passed. Unit/broad tests remain
  intentionally deferred.
- Detailed evidence and deferred-fixture note:
  `.superpowers/sdd/2026-08-23-canonical-canslim-entry/task-2-report.md`.

### Task 2 independent review — fix round 1

- NOT PASS: two independent reviews found 0 Critical and 7 distinct Important issues.
- Validate finite-pivot data/open/buy-zone before any capacity eviction; an overextended new signal must not sell an existing holding and then reject.
- Preserve positional `SimulationResult` compatibility by appending `entry_outcomes` after the existing `benchmark_symbol` field.
- Gate attribution must use canonical `entry_composite_score`, never the legacy M-inclusive `canslim_score`.
- Reconciliation must bind each outcome to the exact signal pivot and each executed outcome to the exact BUY price, and enforce outcome-specific required/null facts.
- Normalize every emitted signal numeric to a finite built-in float or `None`; known pre-Task-4 non-finite fundamentals cannot leak NaN into in-memory/public rows.
- Validate the full signal log as one row per uppercase symbol/benchmark session, with no off-calendar or duplicate evaluations; daily funnel totals must equal the source log.
- For the fixed no-market-gate baseline, require `buy_signal == entry_contract_eligible`, finite positive pivot for every qualifying row, and consistent attempted/executed/qualified/pending/truncation funnel bounds.
- Task 3 remains blocked until all issues close and both independent re-review gates pass.

### Task 2 review-fix implementation

- Closed the eviction-order defect: candidate data/open/finite-pivot zone now
  validates before capacity eviction, while already-open precedence and one
  terminal outcome per attempt remain intact.
- Restored positional `SimulationResult` compatibility by appending outcomes
  after `benchmark_symbol`; changed leader failure attribution to the canonical
  non-M composite and fixed 70-point floor.
- Strengthened reconciliation with exact signal-pivot and rounded BUY-price
  joins plus outcome-specific fact/nullability rules. Valid legacy missing-pivot
  execution remains supported; impossible causal ledgers fail closed.
- Normalized every numeric signal scalar to finite built-in float or `None` and
  bound emitted C/A/RS/composite fields to the full decision's normalized
  values. A non-finite-fundamental row is JSON-safe and cannot qualify; PEG
  remains diagnostic.
- Added all-row symbol/calendar/uniqueness validation, rowwise fixed-baseline
  buy/eligibility equivalence, qualifying pivot/technical checks, exact
  evaluated totals, per-day funnel bounds, and explicit
  qualified/attempted/truncated/final-pending accounting.
- Fresh adversarial and prior Task 2 probes passed for both eviction paths,
  positional compatibility, composite attribution, pivot/BUY-price mismatch,
  all terminal outcome shapes, non-finite JSON safety, daily duplicates and
  off-calendar rows, qualification/accounting mismatches, inclusive next-open
  boundaries, market/PEG/technical separation, and checkpoint v1/v2/journal
  behavior. Relevant compile/import, Ruff, and diff checks passed; broad/unit
  tests remain deferred.
- Both independent re-review gates are still required before Task 3 may run.

### Task 2 independent review — fix round 2

- Re-review found two residual Important issues: risk-invalid replacements
  could still evict first, and impossible capacity truncation could mask a
  missing non-final attempt in the fixed uncapped baseline.
- Risk/target sizing now validates before any eviction; the released-cash path
  remains available only to a valid replacement.
- Fixed PIT validation now requires zero capacity truncation/rejection and
  exact outcome coverage for every qualifying non-final-session signal; only
  final-session qualifiers may remain pending.
- Focused invalid-risk/valid-risk eviction and impossible-capacity/control
  probes passed. Final independent re-review is required before Task 3 launch.
- Both reviewers found the same final zero-outcome funnel initialization bug;
  qualifying/outcome key sets are now initialized outside the nonempty-outcome
  branch. Zero-qualifier and final-session-only pending controls pass, while a
  missing non-final outcome rejects.

## Task 4 — non-completion status (Steps 1–3 only)

- C/A growth helpers now treat non-finite converted operands as unavailable;
  EPS-row selection and Net-Income fallback policy remain unchanged.
- XOM is bound through the existing reviewed-baseline CIK override to
  `0000034088`. Read-only archive probing proved the present-day
  `0002115436` current-ticker candidate exists but cannot win; narrow
  extraction produced 209 XOM normalized rows. The CAH `2021-05-13` bundle
  probe produced finite-or-missing C/A results.
- Compile/import, touched-file Ruff, `git diff --check`, and source-hash
  before/after checks passed. No unit/broad tests, network activity, or
  normalized regeneration ran.
- **Task 4 remains incomplete:** Step 4 immutable SEC-output/PIT-bundle
  regeneration is pending completion of the active multi-hour replay and
  release of shared-machine resources.
