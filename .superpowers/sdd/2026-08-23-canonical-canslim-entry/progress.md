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
