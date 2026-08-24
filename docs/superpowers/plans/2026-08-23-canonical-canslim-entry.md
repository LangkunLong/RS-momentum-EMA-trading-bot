# Canonical CANSLIM Entry Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align PIT, simple-backtest, after-close, and live CANSLIM entry facts; run the PIT baseline daily with next-open validation; correct confirmed CAH/XOM data defects; and publish causally honest raw/PIT-exposed leader recall without optimizing strategy thresholds.

**Architecture:** A pure shared entry-contract module computes completed-session pivot, prior-50 volume, price-advance, and buy-zone facts. Full CANSLIM callers add the existing C/A/RS/composite thresholds while market regime remains a separate execution permission. The PIT runner explicitly selects daily cadence, persists per-attempt next-open outcomes, and reuses the immutable PIT data pipeline for before/after functional replays.

**Tech Stack:** Python 3.13, pandas, SQLite, dataclasses, csv/json/hashlib, pytest, Ruff

**Spec:** `docs/superpowers/specs/2026-08-23-canonical-canslim-entry-design.md`

## Global Constraints

- Keep thresholds fixed: C 25%, A 25%, RS 80, composite 70, volume 1.30x, buy zone 0% through +5%.
- Do not add breakout persistence, tune thresholds, change exits/sizing/cash policy, or solve warm-up in this plan.
- Full CANSLIM qualification never uses market regime, including through its entry composite; use the existing non-M component weights renormalized to 100%, while retaining the legacy M-inclusive total for reporting. Market regime is execution permission.
- The existing power-gap detector remains diagnostic only and cannot bypass the canonical executable contract.
- Preserve the simple technical-only/public CLI and generic simulator cadence default; label approximations honestly.
- Bind daily cadence only in the PIT baseline runner.
- Never overwrite normalized data, bundles, checkpoints, or run directories.
- Use the preserved SHA-bound PIT bundle for the first replay; do not resume its five-day checkpoint.
- Honor the operator-requested sequencing: perform functional probes/replays before adding/running broad unit tests. Run focused and full verification only after the end-to-end path works.
- Each implementation task receives an independent code-quality review and closes all Critical/Important findings before the next task.

---

### Task 1: Implement the Shared Completed-Session Entry Contract

**Files:**
- Create: `core/canslim/entry_contract.py`
- Modify: `core/canslim/core.py`
- Modify: `core/stock_screening.py`
- Modify: `core/after_close_snapshot.py`
- Modify: `backtest.py`

**Interfaces:**
- Produces immutable technical facts and decisions with pivot, prior-average-volume, volume ratio, extension, eligibility, and ordered blocking reasons.
- Full callers supply C/A/RS/composite values; technical-only callers consume only setup eligibility.

- [ ] **Step 1: Add pure fact and decision models**

Implement `CanslimEntryFacts`, `CanslimEntryDecision`,
`build_entry_facts(closes, volumes, ...)`, and
`evaluate_entry_contract(facts, ...)`. Use exact finite checks; 50 prior volumes;
prior-only pivot; price advance; 1.30x; and pivot through +5%.

- [ ] **Step 2: Move live CANSLIM qualification to the shared contract**

Have the CANSLIM core expose the shared facts/decision. Make stock screening use
that decision for entry qualification, while market status only changes
execution/watchlist classification. Remove duplicated executable pivot/volume
logic rather than retaining a silent fallback.

- [ ] **Step 3: Align the after-close technical view**

Replace its local event-inclusive volume and near-high rules with the shared
technical setup facts. Continue to label it technical/advisory because it does
not load full PIT fundamentals.

- [ ] **Step 4: Align the simple backtest technical authority**

Use the shared completed-session facts. Remove power-gap as an executable bypass.
Preserve technical-only mode and CLI compatibility, and do not claim it is the
full C/A CANSLIM contract when fundamentals are unavailable.

- [ ] **Step 5: Run functional boundary probes**

Directly exercise: exact threshold pass, each threshold epsilon failure,
event-volume exclusion, current-close pivot exclusion, below-pivot rejection,
exact +5% acceptance, overextension rejection, market independence, and PEG
non-bypass. Exercise one live-scanner and one after-close fixture through the
shared path. Do not run the broad suite yet.

- [ ] **Step 6: Independent review and commit**

Review for semantic equivalence, no duplicate fallback gates, finite handling,
and compatibility. Commit only Task 1 files plus the plan/spec/ledger updates.

### Task 2: Make PIT Evaluation Daily and Validate the Next Open

**Files:**
- Modify: `core/backtest_engine.py`
- Modify: `core/pit_baseline_report.py`
- Modify: `pit_baseline.py`

**Interfaces:**
- PIT baseline constructs `PortfolioSimulator(..., signal_every_n_days=1)`.
- A queued entry with a finite positive pivot must open between pivot and +5%.
- `SimulationResult` and checkpoints retain per-attempt outcome rows.

- [ ] **Step 1: Consume the shared full entry decision in CanslimStrategy**

Replace the PIT-local technical/PEG branch with the shared completed-session
facts and full C/A/RS/composite decision. Keep market permission layered after
contract qualification and preserve diagnostic facts needed by reporting.

- [ ] **Step 2: Bind daily cadence in the PIT baseline**

Pass `signal_every_n_days=1` from `pit_baseline.py` and fail validation unless the
result config proves cadence 1. Leave the generic simulator default unchanged.

- [ ] **Step 3: Revalidate next-session opening price**

Before cash/risk sizing, reject finite-pivot signals with open below pivot or
above +5%. Record `entry_rejected_next_open_buy_zone` and a per-attempt immutable
outcome carrying symbol, signal date, entry date, open, pivot, bounds, and result.

- [ ] **Step 4: Extend checkpoint and reconciliation contracts**

Persist outcomes in checkpoint state, make the new schema incompatible with the
preserved five-day checkpoint, and reconcile attempts exactly as executions plus
all mutually exclusive rejections. Do not infer symbol-level causes from
aggregate counters.

- [ ] **Step 5: Run a small offline simulator probe**

Prove every completed session is evaluated, exact-zone opens execute,
below-pivot/over-+5% opens reject, missing pivot preserves legacy technical-only
compatibility, and attempts reconcile. Do not run the broad suite yet.

- [ ] **Step 6: Independent review and commit**

Review cadence binding, next-open order, checkpoint compatibility, attempt
uniqueness, and result/report invariants. Commit only Task 2 files and ledger.

### Task 3: Run the First Fresh Five-Year Daily Baseline

**Files:**
- Create only immutable artifacts under `.artifacts/pit-baseline/daily-entry-contract-*` (ignored)
- Modify: `.superpowers/sdd/2026-08-23-canonical-canslim-entry/progress.md`

- [ ] **Step 1: Verify preserved immutable inputs**

Verify the bundle SHA
`8ca8242dd67db30d456a2b1861f7e7399f8ca418079738ab150d4e44865763c5`
and its manifest/provenance with `verify_pit_bundle.py`. Confirm source inputs are
not writable by the replay and choose a fresh output root.

- [ ] **Step 2: Run a fresh daily replay**

Run `pit_baseline.py` for 2021-2025 with the preserved bundle and
`--allow-incomplete-fundamentals`. Do not pass `--resume-checkpoint`. Preserve
incremental progress/checkpoint logs in the new run directory.

- [ ] **Step 3: Audit the completed artifacts**

Require manifest status complete, recomputed artifact hashes, daily cadence,
exact attempt reconciliation, no unattributed cash/capacity rejection, and
source immutability. Record returns, signals, entries, next-open rejections,
average cash, leader recall, and funnel counts without interpreting them as an
optimization verdict.

- [ ] **Step 4: Review the functional result**

Independently verify run identity, input hashes, checkpoint freshness, outcome
reconciliation, and no source mutation. Stop on a functional blocker before
data regeneration.

### Task 4: Correct CAH Non-finite Growth and XOM Issuer Identity

**Files:**
- Modify: `core/canslim/c_current_earnings.py`
- Modify: `core/canslim/a_annual_earnings.py`
- Modify: `core/sec_pit_fundamentals.py`
- Modify only generated ignored artifacts in fresh SEC/bundle directories

- [ ] **Step 1: Make C/A growth finite-or-missing**

Reject non-finite operands in both `_safe_growth` implementations. Preserve the
current EPS-row selection policy; do not introduce a new per-period Net Income
fallback in this bugfix.

- [ ] **Step 2: Bind XOM to its reviewed historical issuer**

Add reviewed mapping `XOM -> 0000034088`. Prove the present-day holdings-company
CIK `0002115436` cannot win through a current-ticker match.

- [ ] **Step 3: Run narrow real-data probes**

Against the existing bundle, prove CAH at 2021-05-13 returns no non-finite value.
Against the pinned local SEC archives, prove XOM resolves with
`reviewed_baseline_cik` and yields normalized rows. Do not run broad tests yet.

- [ ] **Step 4: Regenerate immutable SEC outputs and PIT bundle**

Reuse the already downloaded exact SEC archives without network access. Publish
security master, fundamentals, audit, coverage, provenance, marker, bundle, and
bundle manifest to fresh paths. Recompute and record all hashes.

- [ ] **Step 5: Independent review and commit**

Review finite semantics, exact CIK identity, archive reuse, no-overwrite
publication, cross-hashes, and bundle verification. Commit production changes
and ledger; never commit generated archives/CSV/SQLite outputs.

### Task 5: Report Raw and PIT-Exposed Leader Recall

**Files:**
- Modify: `core/pit_baseline_report.py`
- Modify: `pit_baseline.py`

- [ ] **Step 1: Add explicit five-year denominators**

Report raw leader count/signaled/executed/percentages for all five-year labels,
and PIT-exposed equivalents restricted exactly to `member_at_start=True`.
Percentages must divide by their named denominator.

- [ ] **Step 2: Add explicit rolling denominators**

Report raw rolling recall and PIT-exposed rolling recall restricted to
`member_at_evaluation=True`, retaining same-issuer alias treatment and successor
reset separation.

- [ ] **Step 3: Update report prose and compatibility aliases**

Render denominators alongside numerators. If old `top100_*` fields remain, mark
them deprecated raw aliases and never label a count as a percentage.

- [ ] **Step 4: Functional report probe, review, and commit**

Exercise a small mixed-exposure label set and verify zero-denominator handling.
Review definitions against leader-label models, then commit only reporting files
and ledger.

### Task 6: Run the Corrected Rebuilt-Bundle Baseline

**Files:**
- Create only immutable ignored run artifacts in a fresh output root
- Modify: `.superpowers/sdd/2026-08-23-canonical-canslim-entry/progress.md`

- [ ] **Step 1: Verify rebuilt inputs**

Run exact-source bundle verification and prove XOM/CAH corrections are present
without weakening the recorded incomplete-fundamentals gate.

- [ ] **Step 2: Run a fresh corrected replay**

Use daily cadence, no resume checkpoint, and a new immutable output root. Preserve
incremental logs.

- [ ] **Step 3: Audit and compare**

Verify manifest/hashes/reconciliation/source integrity. Compare only measured
effects of contract/cadence and data corrections: funnel, entries, next-open
rejections, cash, raw/PIT-exposed recall, returns, drawdown, and benchmarks.
Do not tune thresholds from this run.

- [ ] **Step 4: Independent result review**

Require a content-based audit of the run artifacts and input identities before
using the result as the canonical baseline.

### Task 7: Complete Focused and Full Verification

**Files:**
- Create/modify focused tests under `tests/` only after Tasks 1-6 work end-to-end
- Modify relevant README/provenance docs only when commands/results are final

- [ ] **Step 1: Add focused regression coverage**

Cover shared boundaries, all caller adapters, PEG non-bypass, daily PIT binding,
next-open outcomes/reconciliation, checkpoint incompatibility, CAH finite
handling, XOM reviewed CIK, and raw/PIT-exposed recall.

- [ ] **Step 2: Run focused verification**

Run the smallest relevant test modules plus Ruff, Python compile, CLI help, and
`git diff --check`. Fix root causes; do not weaken assertions to fit behavior.

- [ ] **Step 3: Run the full offline suite**

Use the workspace Python runtime. Record exact pass/skip/deselect counts and
compare with the known pre-change baseline `1284 passed, 9 skipped, 2 deselected`.

- [ ] **Step 4: Final documentation and independent review**

Record exact immutable commands, input/output hashes, limitations, and the two
replay results. Request final code and artifact review; close every
Critical/Important finding.

- [ ] **Step 5: Finish the development branch**

Apply verification-before-completion, ensure the worktree is clean, summarize
commits and remaining non-goals, and use the branch-finishing workflow for the
operator-selected integration path.
