# Backtest Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make portfolio eviction reachable, preserve signal-ranking semantics, and leave one authoritative backtest engine.

**Architecture:** `core.backtest_engine.BacktestEngine` remains the production simulation engine. Candidate selection returns ranked signals that can be considered by `_enter_position()` when eviction is enabled; capacity and eviction decisions stay centralized in `_enter_position()` and `_try_evict()`.

**Tech Stack:** Python 3.11+, pandas, pytest

**Spec:** `docs/superpowers/specs/2026-08-16-paper-trading-stabilization-design.md`

## Global Constraints

- Python support is `>=3.11`.
- Tests and dry runs must not submit broker orders.
- Secrets must not appear in tracked files, prepared URLs, logs, or test output.
- Active risk defaults remain an 8% stop and 12.5% maximum position weight.
- `core.backtest_engine` is the only production engine implementation.

---

### Task 1: Prove and Fix Full-Portfolio Eviction Reachability

**Files:**
- Modify: `tests/test_backtest_pnl.py`
- Modify: `core/backtest_engine.py`

**Interfaces:**
- Consumes: `BacktestEngine._evaluate_signals(...) -> list[dict]`, `_enter_position(signal, ticker_ohlcv, entry_date) -> None`, and `_try_evict(...) -> bool`.
- Produces: ranked candidate flow that can reach `_try_evict()` only when eviction is enabled.

- [ ] **Step 1: Write a failing integration regression test**

Construct an engine with `max_positions=1` and `enable_eviction=True`, seed one open trade with RS 60 and an entry price above its current close, and make the strategy return a buy signal with RS 90 for a different symbol. Call `_evaluate_signals()` while the portfolio is full, assert the higher-RS signal is returned, pass it to `_enter_position()`, then assert the old symbol closed with reason `evicted` and the new symbol owns the slot.

The test catches this exact mutation: reintroducing `signals[:open_slots]` when `open_slots == 0`.

- [ ] **Step 2: Run the regression and observe the failure**

Run: `python -m pytest tests/test_backtest_pnl.py -k "full_portfolio and eviction" -q --no-cov`

Expected: `_evaluate_signals()` returns an empty list before `_try_evict()` can run.

- [ ] **Step 3: Add capacity behavior tests**

Add literal cases proving:

- a full portfolio with `enable_eviction=False` returns no candidates;
- a portfolio with one free slot returns only the best-ranked candidate;
- a full portfolio with eviction enabled returns ranked candidates, but `_enter_position()` admits none whose RS does not exceed an incumbent;
- a missing price bar cannot evict an incumbent.

- [ ] **Step 4: Implement the minimal candidate-limit rule**

Keep the existing score sort. Compute open slots and use this contract:

```python
open_slots = max(self.max_positions - len(self._open_positions), 0)
candidate_limit = self.max_positions if self.enable_eviction else open_slots
return signals[:candidate_limit]
```

Do not move the RS comparison or loser preference out of `_try_evict()`.

- [ ] **Step 5: Verify the eviction test matrix**

Run: `python -m pytest tests/test_backtest_pnl.py -k "evict or full_portfolio or signal" -q --no-cov`

Expected: every capacity, ranking, and data-gap case passes.

- [ ] **Step 6: Run the complete backtest suite**

Run: `python -m pytest tests/test_backtest_pnl.py tests/test_backtest_engine_pivots.py tests/test_backtest_market_regime.py -q --no-cov`

Expected: all selected tests pass.

- [ ] **Step 7: Commit eviction correctness**

```bash
git add core/backtest_engine.py tests/test_backtest_pnl.py
git commit -m "fix: allow ranked signals to reach portfolio eviction"
```

### Task 2: Consolidate the Root Backtest Compatibility Surface

**Files:**
- Modify: `backtest_pnl.py`
- Modify: `tests/test_backtest_pnl.py`

**Interfaces:**
- Consumes: `core.backtest_engine.Trade`, `SimulationResult`, `BacktestEngine`, CLI parser, and compatibility helper functions used by tests/callers.
- Produces: one root CLI/import module that delegates simulation to `core.backtest_engine` without duplicate model or engine definitions.

- [ ] **Step 1: Characterize the compatibility API**

Add a test that imports `Trade`, `SimulationResult`, and `BacktestEngine` from both modules and asserts identity with the production classes. Add a CLI parser smoke test covering the documented defaults without executing downloads.

- [ ] **Step 2: Run the characterization tests**

Run: `python -m pytest tests/test_backtest_pnl.py -k "compatibility or cli_defaults" -q --no-cov`

Expected: class identity passes only if aliases are active; the test records the public surface before cleanup.

- [ ] **Step 3: Remove unreachable duplicate engine/model code**

Delete the code guarded as legacy/unreachable that defines competing `Trade`, `SimulationResult`, or simulation behavior. Preserve root-level helper names that callers and tests patch, and keep the CLI delegating to the production engine.

- [ ] **Step 4: Verify imports and CLI help**

Run: `python -m pytest tests/test_backtest_pnl.py -q --no-cov`

Run: `python backtest_pnl.py --help`

Run: `python -m ruff check backtest_pnl.py core/backtest_engine.py --no-cache`

Expected: tests and lint pass; CLI help exits zero without network access.

- [ ] **Step 5: Commit the compatibility cleanup**

```bash
git add backtest_pnl.py tests/test_backtest_pnl.py
git commit -m "refactor: consolidate backtest engine implementation"
```

### Task 3: Align Backtest Risk Descriptions with Active Defaults

**Files:**
- Modify: `core/backtest_engine.py`
- Modify: `backtest_pnl.py`
- Modify: `docs/superpowers/plans/2026-04-12-portfolio-exit-engine.md`

**Interfaces:**
- Consumes: `settings.STOP_LOSS_PCT == 0.08` and `settings.POSITION_SIZE_PCT == 0.125`.
- Produces: accurate user-facing descriptions while preserving tests that intentionally pass alternate values such as 7%.

- [ ] **Step 1: Inventory stale default claims**

Run: `rg -n "7%|0\.07|10%|0\.10|POSITION_SIZE_PCT|STOP_LOSS_PCT" backtest_pnl.py core/backtest_engine.py docs/superpowers/plans/2026-04-12-portfolio-exit-engine.md`

Classify each occurrence as either an active-default claim or an explicit scenario/override. Only active-default claims change.

- [ ] **Step 2: Correct active-default prose**

State the active contract as: 1% portfolio risk, 8% hard stop, and a derived 12.5% maximum position weight. Leave explicit 7% unit-test scenarios unchanged because they exercise parameterization rather than document defaults.

- [ ] **Step 3: Verify behavior is unchanged**

Run: `python -m pytest tests/test_backtest_pnl.py -q --no-cov`

Run: `python -m ruff check backtest_pnl.py core/backtest_engine.py --no-cache`

Expected: all commands exit zero; no production constant changes.

- [ ] **Step 4: Commit risk-description alignment**

```bash
git add backtest_pnl.py core/backtest_engine.py docs/superpowers/plans/2026-04-12-portfolio-exit-engine.md
git commit -m "docs: align backtest risk defaults"
```
