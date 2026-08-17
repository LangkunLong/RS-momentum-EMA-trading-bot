# Paper Execution Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make cycle outcomes explicit and ensure sell-fill notifications use durable entry cost basis after restarts or missing in-memory workflows.

**Architecture:** `OrderManager` remains the only fill-transition coordinator. The auto-trader returns an immutable cycle value object to the scheduler, while cost basis is resolved from the workflow first and the persisted active-position ownership record second; unknown cost basis is represented honestly rather than fabricated.

**Tech Stack:** Python 3.11+, dataclasses, SQLite, Alpaca paper API, pytest

**Spec:** `docs/superpowers/specs/2026-08-16-paper-trading-stabilization-design.md`

## Global Constraints

- Python support is `>=3.11`.
- Tests and dry runs must not submit broker orders.
- Alpaca is execution truth; SQLite is the local audit/recovery view.
- Workflow transitions remain centralized in `core.order_manager.OrderManager` and `core.execution_workflow.ExecutionWorkflow`.
- Unknown P&L must be shown as unavailable, never as a false zero.

---

### Task 1: Return Exact Auto-Trader Cycle Outcomes

**Files:**
- Modify: `auto_trader.py`
- Modify: `tests/test_regression.py`
- Modify: `tests/test_e2e_flow.py`

**Interfaces:**
- Produces: `AutoTraderCycleResult(entered: tuple[str, ...], exited: tuple[str, ...])`.
- Produces: `run_auto_trader(...) -> AutoTraderCycleResult`.
- Consumes: existing `monitor_and_exit_positions() -> list[str]` and `execute_entries(...) -> list[str]`.

- [ ] **Step 1: Write failing cycle-result tests**

Add tests with the scanner and order boundaries patched so no network call is made. Prove that:

- entry and exit symbols are returned exactly once in immutable tuples;
- `skip_entries=True` produces an empty `entered` tuple;
- `skip_exits=True` produces an empty `exited` tuple;
- a market-closed early return produces an empty result rather than `None`.

Use literal expected values such as `AutoTraderCycleResult(entered=("NVDA",), exited=("AAPL",))`.

- [ ] **Step 2: Run the tests and observe the current `None` result**

Run: `python -m pytest tests/test_regression.py tests/test_e2e_flow.py -k "cycle_result" -q --no-cov`

Expected: failure because `run_auto_trader()` returns `None`.

- [ ] **Step 3: Add the immutable result type**

```python
@dataclass(frozen=True)
class AutoTraderCycleResult:
    entered: tuple[str, ...] = ()
    exited: tuple[str, ...] = ()
```

Initialize local `entered` and `exited` lists before conditional phases, convert them to tuples at the single normal return, and return the empty value object from the market-closed guard.

- [ ] **Step 4: Verify the auto-trader contract**

Run: `python -m pytest tests/test_regression.py tests/test_e2e_flow.py -k "cycle_result or auto_trader" -q --no-cov`

Expected: every selected test passes without broker calls.

- [ ] **Step 5: Commit the cycle result**

```bash
git add auto_trader.py tests/test_regression.py tests/test_e2e_flow.py
git commit -m "feat: return auto-trader cycle outcomes"
```

### Task 2: Propagate Cycle Outcomes to the Scheduler Notification

**Files:**
- Modify: `scheduler.py`
- Modify: `core/notifier.py`
- Modify: `tests/test_hourly_monitor.py`
- Modify: `tests/test_notifier.py`
- Modify: `tests/test_regression.py`

**Interfaces:**
- Consumes: `run_auto_trader(...) -> AutoTraderCycleResult`.
- Produces: `notify_cycle_summary(entered: Sequence[str], exited: Sequence[str], paper: bool) -> bool` called with real outcomes.

- [ ] **Step 1: Write the failing scheduler propagation test**

Patch `scheduler.run_auto_trader` to return `AutoTraderCycleResult(entered=("NVDA",), exited=("AAPL",))`. Patch the email boundary and call `_run_cycle(dry_run=True)`. Assert the real notifier body contains `NVDA` and `AAPL`; do not assert only that a mock was called.

- [ ] **Step 2: Run the test and observe the empty-summary behavior**

Run: `python -m pytest tests/test_regression.py -k "cycle_summary" -q --no-cov`

Expected: no email/body because `_run_cycle()` passes two empty lists and the notifier returns `False`.

- [ ] **Step 3: Pass the returned outcomes through unchanged**

```python
result = run_auto_trader(dry_run=dry_run)
notify_cycle_summary(
    entered=result.entered,
    exited=result.exited,
    paper=_is_paper_mode(),
)
```

Type notifier inputs as `Sequence[str]` so immutable tuples are accepted without copying. Retain best-effort notification failure handling.

- [ ] **Step 4: Verify scheduler and notifier behavior**

Run: `python -m pytest tests/test_hourly_monitor.py tests/test_notifier.py tests/test_regression.py -k "summary or cycle" -q --no-cov`

Expected: real-outcome summaries pass; an empty cycle remains a deliberate no-email result.

- [ ] **Step 5: Commit scheduler result propagation**

```bash
git add scheduler.py core/notifier.py tests/test_hourly_monitor.py tests/test_notifier.py tests/test_regression.py
git commit -m "fix: report actual scheduler cycle outcomes"
```

### Task 3: Recover Sell-Fill Entry Cost Basis

**Files:**
- Modify: `core/order_manager.py`
- Modify: `core/notifier.py`
- Modify: `tests/test_order_manager.py`
- Modify: `tests/test_order_execution.py`
- Modify: `tests/test_notifier.py`
- Modify: `tests/test_e2e_flow.py`

**Interfaces:**
- Consumes: `ExecutionWorkflow.entry_plan.entry_price` and `ExecutionStore.load_active_position(symbol)`.
- Produces: `OrderManager._resolve_entry_price(symbol, workflow) -> float | None` and `notify_sell_filled(..., entry_price: float | None, ...) -> bool`.

- [ ] **Step 1: Write the failing restart-recovery test**

Using the real temporary SQLite execution store, persist an active position for `AAPL` with entry price `100.00`, clear only the in-memory workflow registry, then deliver a sell fill at `110.00` through `OrderManager.handle_fill()`. Capture the real email body at the SMTP boundary and assert it reports entry `$100.00`, P&L `+$100.00` for ten shares, and `+10.00%`.

The test catches substituting the sell price as entry price after an in-memory restart.

- [ ] **Step 2: Run the recovery test and observe false zero P&L**

Run: `python -m pytest tests/test_e2e_flow.py -k "sell_fill and cost_basis and restart" -q --no-cov`

Expected: the body uses `$110.00` as both prices and reports zero.

- [ ] **Step 3: Add focused cost-basis precedence tests**

Prove with literal values that workflow entry plan `95.00` wins over persisted active position `100.00`, persisted `100.00` is used when the workflow has no entry plan, and no record returns `None`.

- [ ] **Step 4: Implement cost-basis resolution before the sell transition**

```python
def _resolve_entry_price(self, symbol: str, workflow: ExecutionWorkflow | None) -> float | None:
    entry_plan = getattr(workflow, "entry_plan", None) if workflow is not None else None
    if entry_plan is not None and entry_plan.entry_price > 0:
        return float(entry_plan.entry_price)
    active = get_execution_store().load_active_position(symbol)
    if active is not None and float(active["entry_price"]) > 0:
        return float(active["entry_price"])
    return None
```

Call this before `workflow.mark_sell_fill()` because the transition may clear active-position ownership.

- [ ] **Step 5: Represent unknown P&L honestly**

When `entry_price is None`, render `Entry price: unavailable` and `P&L: unavailable`. Compute numeric P&L only for a positive recovered entry price.

- [ ] **Step 6: Verify fill, store, and notification behavior**

Run: `python -m pytest tests/test_order_manager.py tests/test_order_execution.py tests/test_notifier.py tests/test_e2e_flow.py -k "fill or cost_basis or pnl" -q --no-cov`

Expected: restart recovery, precedence, unknown basis, and existing fill behaviors all pass.

- [ ] **Step 7: Commit sell-fill recovery**

```bash
git add core/order_manager.py core/notifier.py tests/test_order_manager.py tests/test_order_execution.py tests/test_notifier.py tests/test_e2e_flow.py
git commit -m "fix: recover sell-fill cost basis from execution state"
```

### Task 4: Verify Workflow Recovery and Protective Stops End to End

**Files:**
- Modify: `tests/test_execution_workflow.py`
- Modify: `tests/test_fill_monitor.py`
- Modify: `tests/test_e2e_flow.py`

**Interfaces:**
- Consumes: broker/client/workflow reference resolution, active-position ownership, partial-fill handling, and protective-stop reconciliation.
- Produces: regression coverage for restart and idempotency boundaries.

- [ ] **Step 1: Add workflow-resolution precedence cases**

Persist conflicting historical and active workflows for one symbol. Assert explicit workflow id wins, then client order id, then broker order id, then active ownership, and only then latest history.

- [ ] **Step 2: Add duplicate fill/idempotency cases**

Deliver the same broker fill twice and assert durable state does not create a second active position or duplicate terminal transition. Deliver a partial buy fill followed by a final fill and assert protective stop quantity is reconciled to the final filled quantity.

- [ ] **Step 3: Run the execution recovery suite**

Run: `python -m pytest tests/test_execution_workflow.py tests/test_fill_monitor.py tests/test_e2e_flow.py -q --no-cov`

Expected: all recovery, precedence, and fill-sequence tests pass.

- [ ] **Step 4: Commit recovery coverage**

```bash
git add tests/test_execution_workflow.py tests/test_fill_monitor.py tests/test_e2e_flow.py
git commit -m "test: cover execution restart and fill idempotency"
```
