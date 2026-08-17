# Operations Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the stabilized branch reproducible, reviewable, and safe to validate under supervision in Alpaca paper mode.

**Architecture:** Offline verification is a hard gate before any broker mutation. The README and operational commands expose one paper-trading path through the canonical execution workflow, and provider-side credential rotation/history rewrite are coordinated separately from ordinary code commits.

**Tech Stack:** Python 3.11+, pytest, Ruff, pip, Alpaca paper API, Windows Task Scheduler, Git/GitHub

**Spec:** `docs/superpowers/specs/2026-08-16-paper-trading-stabilization-design.md`

## Global Constraints

- Python support is `>=3.11`.
- Tests and dry runs must not submit broker orders.
- Live trading is out of scope.
- The active defaults are an 8% stop, 1% portfolio risk, and 12.5% maximum position weight.
- A broker order, scheduled-task installation, public push, or public-history rewrite is an external state change and must be explicitly authorized at its execution gate.

---

### Task 1: Document the Canonical Paper-Trading System

**Files:**
- Modify: `README.md`
- Modify: `auto_trader.py`
- Modify: `core/order_execution.py`
- Modify: `core/notifier.py`
- Modify: `docs/superpowers/plans/2026-04-12-portfolio-exit-engine.md`

**Interfaces:**
- Consumes: environment-backed settings, dry-run CLI, execution SQLite path, scheduler CLI, and active risk defaults.
- Produces: one operator-facing setup and recovery runbook.

- [x] **Step 1: Correct stale risk prose in production modules**

Change active-default descriptions from 7%/10% to the actual settings contract: 8% hard stop and 12.5% maximum position weight derived from 1% portfolio risk. Preserve examples that explicitly pass a non-default value.

- [x] **Step 2: Replace the placeholder README with an operational guide**

Document:

- Python 3.11+ setup and dependency installation;
- required Alpaca and FMP environment variable names without values;
- the `ALPACA_PAPER=true` safety boundary and explicit warning that live trading is out of scope;
- scanner, deterministic backtest, auto-trader dry-run, paper console doctor, scheduler dry-run, and test/lint commands;
- execution workflow/store ownership and restart reconciliation;
- 8% stop, 1% risk, 12.5% maximum weight, staged exits, and eviction behavior;
- generated artifact locations and `.gitignore` behavior;
- credential rotation and public-history cleanup requirements.

- [x] **Step 3: Verify every documented command locally where it is non-mutating**

Run CLI `--help` for `enhanced_scanner.py`, `backtest_pnl.py`, `auto_trader.py`, `paper_trading_console.py`, and `scheduler.py`. Run the documented lint and test commands. Do not run commands that submit an order or install a task.

- [x] **Step 4: Commit operator documentation**

```bash
git add README.md auto_trader.py core/order_execution.py core/notifier.py docs/superpowers/plans/2026-04-12-portfolio-exit-engine.md
git commit -m "docs: add paper-trading operations runbook"
```

### Task 2: Record a Reproducible Verified Dependency Set

**Files:**
- Create: `requirements-lock.txt`
- Modify: `README.md`

**Interfaces:**
- Consumes: the aligned runtime/development manifests and the environment used for verification.
- Produces: an exact constraints file usable with `pip install -r requirements-lock.txt`.

- [x] **Step 1: Generate the lock from a clean supported environment**

Install the aligned manifests into an isolated virtual environment, run `python -m pip check`, and write exact installed versions to `requirements-lock.txt`. Exclude unrelated globally installed packages by generating from the isolated environment only.

- [x] **Step 2: Verify a second clean install**

Create a fresh temporary virtual environment, install `requirements-lock.txt`, run Ruff, import all production modules, and execute the non-integration pytest suite.

- [x] **Step 3: Document lock usage**

Explain that `requirements.txt` expresses supported ranges while `requirements-lock.txt` records the verified operational/CI set.

- [x] **Step 4: Commit reproducibility artifacts**

```bash
git add requirements-lock.txt README.md
git commit -m "build: lock verified Python dependencies"
```

### Task 3: Run the Full Offline Release Gate

**Files:**
- No source file changes expected.

**Interfaces:**
- Consumes: the complete stabilized tree.
- Produces: fresh evidence for lint, compilation, tests, secret hygiene, dependency integrity, and CLI startup.

- [x] **Step 1: Verify repository state and diff scope**

Run `git status --short --branch`, `git diff --check`, `git diff --stat main...HEAD`, and inspect every commit since `c526fea`. Confirm no generated artifact, local settings file, execution database, or credential value is tracked.

- [x] **Step 2: Run static gates**

Run: `python -m ruff check . --no-cache --exclude .artifacts`

Run: `python -m compileall -q -x "[\\/](\.git|\.worktrees|\.artifacts|\.venv)[\\/]" .`

Run: `python -m pip check`

Expected: all exit zero.

- [x] **Step 3: Run the complete offline suite with coverage**

Run: `python -m pytest -q -m "not integration"`

Expected: every selected test passes; coverage report is freshly generated under ignored artifacts.

- [x] **Step 4: Run safe operational diagnostics**

Run: `python paper_trading_console.py doctor`

Run: `python auto_trader.py --dry-run`

Run: `python scheduler.py --dry-run --now`

If a command can submit an order despite its flag, stop and fix that safety defect before retrying. Network/provider unavailability is reported separately from code failure.

- [x] **Step 5: Review the final branch as a pull request**

Inspect `git diff main...HEAD` for correctness, architecture drift, secret exposure, and documentation accuracy. Correct any finding through its own failing test and focused commit before claiming readiness.

### Task 4: Coordinate FMP Credential Rotation and Public-History Cleanup

**Files:**
- Public Git history; no ordinary working-tree file is the source of truth for this task.

**Interfaces:**
- Consumes: a newly rotated FMP credential stored outside Git and a verified clean tracked tree.
- Produces: a revoked exposed credential and, after coordinated approval, a public history that no longer contains the generated scan CSV secret.

- [x] **Step 1: Obtain confirmation that the exposed FMP key was rotated**

The operator confirmed on 2026-08-17 that the old key was revoked and a replacement was stored locally. Neither value was pasted into chat, logs, commands, or tracked files.

- [x] **Step 2: Prepare history cleanup locally**

From a fresh clone or disposable mirror, use a history-filtering tool to remove `scan_results/canslim_scan_20260408_003511.csv` and credential-bearing generated scan artifacts from every ref intended for publication. Do not alter the preserved recovery bundle.

- [x] **Step 3: Verify the rewritten object database**

Search every rewritten commit and reachable blob by filename and credential fingerprint. Confirm normal source/tests remain, then run the full offline release gate on the rewritten branch.

- [ ] **Step 4: Request explicit coordinated force-push approval**

Explain that collaborators must re-clone or rebase onto rewritten history. Do not force-push until the user approves the exact remote and refs.

- [ ] **Step 5: Publish cleanup and invalidate cached copies where supported**

After approval, force-push only the verified rewritten refs with lease protection where applicable, then verify the public repository no longer exposes the removed blob. Contact the hosting provider if cached views remain accessible.

### Task 5: Supervised Alpaca Paper Validation

**Files:**
- No source changes expected unless validation reveals a reproducible defect.

**Interfaces:**
- Consumes: rotated provider credentials, `ALPACA_PAPER=true`, green offline release gate, and the canonical execution workflow.
- Produces: evidence that one paper buy/fill/protective-stop/sell lifecycle is auditable and recoverable.

- [ ] **Step 1: Verify the external-operation preconditions**

Confirm `ALPACA_PAPER=true`, account endpoint is the paper endpoint, symbol and one-share quantity are displayed, market status is known, notifications are configured as intended, and no live-account credential is active.

- [ ] **Step 2: Request explicit approval for the one-share paper order**

State the exact symbol, quantity, order type, and cleanup behavior. Do not submit before approval.

- [ ] **Step 3: Run the supervised verification lifecycle**

Run `verify_paper_trading.py` with its documented confirmation gate. Observe the buy submission/fill, actual-fill protective stop, durable transitions, restart resolution, and cleanup sell/cancel behavior.

- [ ] **Step 4: Inspect broker and local audit state**

Compare Alpaca orders/positions with SQLite workflow snapshots, order references, active-position ownership, and append-only transition history. Verify no orphan position or order remains.

- [ ] **Step 5: Diagnose any mismatch before repeating**

If validation fails, capture broker ids and sanitized state, reproduce offline with a failing test, implement one root-cause fix, rerun the full offline gate, and request approval again before another paper order.

### Task 6: Install the Windows Schedule

**Files:**
- No tracked file changes expected.

**Interfaces:**
- Consumes: green supervised paper validation and `setup_windows_task.py`.
- Produces: one inspectable Windows scheduled task running paper mode.

- [x] **Step 1: Display the exact proposed task configuration**

Show task name, interpreter path, repository path, arguments, trigger time/time zone, working directory, user identity, and log destination.

- [ ] **Step 2: Request explicit installation approval**

Do not create or replace a scheduled task until the user approves the displayed configuration.

- [ ] **Step 3: Install and inspect the task**

Run the setup script, query the resulting Windows task, and verify command, trigger, working directory, and paper-mode environment.

- [ ] **Step 4: Run one supervised scheduled dry run**

Trigger the task with dry-run arguments, inspect exit status and logs, and confirm no broker mutation occurred.

- [ ] **Step 5: Enable the validated paper schedule**

Replace the dry-run arguments only after the user explicitly approves automated paper order submission. Inspect the final task once more and record rollback/removal instructions.

### Task 7: Finalize Branch and Worktree Hygiene

**Files:**
- Git refs and worktrees; no production source change expected.

**Interfaces:**
- Consumes: the recovery commit `c526fea`, the verified stabilization commits, and the stale `claude/portfolio-exit-engine` worktree.
- Produces: a reviewable branch/PR and an explicit disposition for obsolete local worktrees.

- [x] **Step 1: Verify recovery and stabilization refs**

Run `git log --oneline --decorate --graph --all`, `git worktree list --porcelain`, and `git branch -vv`. Confirm `main` still points at the pre-stabilization commit, the recovery branch contains `c526fea`, and the stabilization branch contains only focused follow-up commits.

- [x] **Step 2: Preserve the stale worktree's remaining change**

Inspect `C:\Users\llong\.config\superpowers\worktrees\RS-momentum-EMA-trading-bot\portfolio-exit-engine\config\settings.py`, export its diff into the ignored recovery area, and verify the archive before proposing retirement.

- [ ] **Step 3: Request approval before retiring the stale worktree**

Display the exact worktree path, branch, commit, and preserved diff artifact. Remove the worktree and delete its local branch only if the user explicitly approves those destructive Git operations.

- [ ] **Step 4: Request approval to publish the stabilization branch**

Display the remote, branch name, commit list, full test/lint results, and note whether public-history cleanup has already occurred. Do not push if doing so would complicate the coordinated secret-history rewrite.

- [ ] **Step 5: Push and open a reviewable pull request after approval**

Push `codex/stabilize-paper-trading`, open a pull request against the intended cleaned base branch, and include architecture, security, tests, operational gates, and still-pending supervised paper/scheduler steps in the description.
