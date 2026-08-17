# Scheduler Paper Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Progress from the passed supervised SPY lifecycle to a safely runnable weekday Alpaca paper scheduler, beginning with a quota-zero dry run.

**Architecture:** Keep the existing scanner and durable execution workflow. Add a process singleton, require an authenticated/fault-free fill stream plus successful startup reconciliation before any broker mutation, require explicit order enablement at every CLI boundary, and let the scheduled process exit after the market session. The direct-Python Windows task remains dry-run and FMP-budget-zero until its behavior is observed.

**Tech Stack:** Python 3.11+, alpaca-py 0.44.0, SQLite, pytest, Windows Task Scheduler.

**Spec:** `docs/superpowers/specs/2026-08-16-paper-trading-stabilization-design.md`

---

## Task 1: Fail-closed scheduler runtime

**Files:**
- Modify: `scheduler.py`
- Modify: `fill_monitor.py`
- Create: `tests/test_scheduler_runtime.py`
- Modify only directly affected scheduler fixtures in `tests/test_regression.py`, `tests/test_e2e_flow.py`, and `tests/test_paper_mode_boundary.py`

- [x] Add focused RED tests proving that a second scheduler instance is rejected, live execution waits for `FillMonitor.is_connected()`, failed startup reconciliation blocks all entry/exit calls, and an unavailable Alpaca clock cannot authorize live orders.
- [x] Implement a process-wide singleton lock with automatic crash release.
- [x] Start/restart the fill monitor with a bounded connected-health wait and stop/join it deterministically.
- [x] Make startup reconciliation raise on broker errors or unsuccessful safety results.
- [x] Gate immediate scan, daily scan, hourly exits, and fallback exits on connected + reconciled readiness.
- [x] Add an optional weekday-session exit after 16:05 ET for Task Scheduler.
- [x] Run the focused scheduler tests, Ruff, compile, and `git diff --check` with `FMP_DAILY_REQUEST_BUDGET=0` and dead network proxies.

## Task 2: Make every operator CLI safe by default

**Files:**
- Modify: `scheduler.py`
- Modify: `paper_trading_console.py`
- Modify: `setup_windows_task.py`
- Modify: `tests/test_setup_windows_task.py`
- Modify: `tests/test_paper_trading_console.py`

- [x] Add RED tests proving `scheduler.py`, console `run-now`, and task installation default to dry run and require `--enable-orders` for broker mutation.
- [x] Preserve `--dry-run` as an explicit compatibility flag, but make omission safe.
- [x] Make the direct XML task action use unbuffered Python, session mode, an eight-hour cap, reliable process termination, and an FMP budget of zero while dry-run.
- [x] Keep the command-execution quoting probe Windows-only; retain cross-platform structural assertions instead of deleting the safety contract.
- [x] Run only the focused CLI/task tests, then Ruff, compile, and diff checks.

## Task 3: Prove the control plane without provider or broker mutation

**Files:**
- Inspect: `.artifacts/cache/fmp_request_usage.json`
- Inspect: execution SQLite state and Alpaca paper account state

- [x] Capture FMP ledger hash/count plus broker positions and open orders.
- [x] Run one unbuffered manual `paper_trading_console.py run-now` in its new default dry-run mode with `FMP_DAILY_REQUEST_BUDGET=0`.
- [x] Confirm the FMP ledger, broker positions/orders, and durable execution state are unchanged.

## Task 4: Install and observe the dry-run Windows task

**Files:**
- Inspect: `.artifacts/logs/scheduler.log`
- Inspect: Windows Task Scheduler XML/status

- [x] Display the exact action, interpreter, working directory, trigger, and mode.
- [x] Install `CANSLIM-Scheduler` in default dry-run/session mode.
- [x] Query the installed task and verify the action contains `--dry-run --session`, unbuffered Python, and the zero FMP budget override.
- [x] Trigger one bounded dry-run session, observe a full cycle sentinel in the log, then confirm zero broker/FMP mutation.

## Task 5: Graduate to paper orders deliberately

- [x] Review the dry-run shortlist, sizing, current broker account, and FMP headroom.
- [x] Run exactly one monitored order-enabled scheduler cycle with all other order-enabled processes stopped.
- [x] Verify any resulting fills and workflows; when no signal qualifies, verify zero positions, orders, active owners, and pending intents.
- [x] Only after that evidence, replace the scheduled task with its explicit `--enable-orders` action and re-inspect the installed configuration.
