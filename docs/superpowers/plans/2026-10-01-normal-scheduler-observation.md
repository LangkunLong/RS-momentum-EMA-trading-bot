# Normal Scheduler Health Observation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Add an opt-in, offline-reviewable observation mode for one ordinary full-universe dry-run scheduler session, with truthful work outcomes and bounded process-wide resources.

**Architecture:** Keep existing scheduler cadence, full `large_cap` scan, strategy decisions, and exit logic. Add a process-wide observation context shared by scheduler and worker threads; instrument Alpaca, FMP, index and execution-store boundaries; expose strict work and coverage outcomes; and use an ignored PowerShell launcher for notification suppression and durable child capture.

**Tech Stack:** Python 3.13, pytest, Alpaca SDK session wrapper, `requests`, SQLite, PowerShell 7, existing FMP ledger/cache and execution-store schema.

**Spec:** [docs/superpowers/specs/2026-10-01-normal-scheduler-observation.md](C:/Users/llong/.codex/worktrees/8799/RS-momentum-EMA-trading-bot/docs/superpowers/specs/2026-10-01-normal-scheduler-observation.md)

## Global Constraints

- Base branch: `codex/issue-107-normal-scheduler-observation` from `f15d3db17eb1e2f1202b534e4f58e0fa1d6a06c4`; preserve `codex/issue-107-readiness` and ignored AAPL artifacts.
- Only edit paths in the approved file list in this plan. Stop and request approval before adding another tracked or new path.
- `--observe-health` is opt-in; when absent, ordinary scheduler behavior remains unchanged.
- Preserve the full `large_cap` universe, six extra symbols, strategy thresholds, entry/exit rules, max 5 open positions, max 2 new entries per cycle, and all existing schedule times.
- Enforce at most 256 actual Alpaca HTTP attempts across the process, zero Alpaca SDK retries, at most 6 actual index-source attempts, 15-second request inactivity timeouts, and a separate 45-minute watchdog.
- Before Python imports settings, the launcher sets `ALPACA_HTTP_TIMEOUT_SECONDS=15`, `FMP_HTTP_TIMEOUT_SECONDS=15`, `INDEX_TICKER_HTTP_TIMEOUT_SECONDS=15`, and `ALPACA_SDK_RETRY_ATTEMPTS=0`. The child CLI independently verifies those exact settings and rejects direct `--observe-health` invocation when any value differs.
- FMP logical requests are capped at `min(198, existing local reset-window remaining allowance)`. Keep the existing ledger and FMP free-plan transport retries at zero. Never reset or replace the ledger, warm caches to force coverage, or retry a denied request.
- The existing FMP ledger is required in observation mode and must remain readable. A missing or unreadable ledger is a latched service failure even if the generic non-observation code would initialize a new ledger. A present ledger from an earlier reset window may roll over normally.
- A local FMP-ledger deferral is degraded required-input coverage, not by itself a service crash. Provider refusal, process-cap denial, unknown required clock, failed expected work, or missed due work remains failed/unverified.
- Use the existing C/#100 durable SQLite schema and action-identity contract. Cap this observation at 6 appended workflow transitions and 2 appended snapshots; add no table, persistence schema, or identity policy.
- Snapshot row counts and deterministic content hashes for every existing execution-store table before and after the child: `workflow_snapshots`, `workflow_transitions`, `workflow_order_refs`, `active_positions`, and `workflow_notification_claims`. Only up to 2 added snapshots and 6 added transitions are within this observation's write envelope; any other table-content change or out-of-envelope delta makes service health failed or unverified. Preserve the existing schema and identity policy.
- Receipt fields are separate: `service_health`, `required_input_coverage`, and issue-level `overall_readiness`. The observer alone must never emit `overall_readiness=pass`; it defaults to `unverified` unless an out-of-scope reviewed evidence aggregator supplies every eligible receipt at the exact compatible source.
- The only provider-clock preflight occurs inside the Python child after the 256-attempt Alpaca budget is active, and counts toward 256. The launcher makes no provider reads.
- Notification sender/recipient/password are blanked before Python imports settings; verify the child sees blank sender/recipient. No external notification transmission, fill stream, order submission, installed-task change, scheduler activation, selected-runtime/store operation, quota reset, provider session, or source rollout is part of implementation or testing.
- In observation mode, explicitly skip both scheduler `notify_cycle_summary` call sites. Offline tests place credentials in the parent environment and fail if either call site, SMTP, or Gmail delivery is invoked; launcher environment blanking is defense in depth.
- When child termination is unobserved, keep files labeled raw, write a truthful surviving-child receipt, and skip all post-run DB, ledger, and cache reads.

## Review Focus

1. FMP process cap and local daily ledger both deny work: the candidate reason and counters distinguish the two; local deferral degrades input coverage without disguising a provider or hard-cap failure.
2. Alpaca pagination, failed HTTP attempts, index fallback, and a request exactly at the ceiling: all attempts are counted before I/O; denials latch even if caught; no extra request escapes the cap.
3. The market clock and due-work schedule across 15:xx, 16:01, fallback intervals, close, and an unknown clock: observation mode records every due result and never substitutes local time for an unknown provider clock.
4. A full `large_cap` pass with cached, missing, and quota-deferred candidate data: requested/validated/analyzed coverage and unchanged action identity are preserved; there is no narrower substitute scan.
5. Child timeout or kill failure while output files are open: the receipt reflects a potentially surviving child, raw files are never mislabeled sanitized, and post-run DB/cache inspection is skipped.

---

## Task 1: Add the shared observation state and outcome contract

**Files:**
- Create: `core/scheduler_observation.py`
- Create: `tests/test_scheduler_observation.py`

**Interfaces:**
- `SchedulerObservation` owns a thread-safe event list, service-health state, required-input coverage state, resource denial flags, and run identity. Worker threads share the same active instance.
- `activate_scheduler_observation(observation)` is a context manager that installs one process-wide active observation and clears it on exit.
- `current_scheduler_observation()` returns the active instance or `None`.
- `record_event(kind, key, status, details)` records expected work without credentials or raw account identifiers.
- `latch_service_issue(code, details, *, unverified=False)` records a failure that cannot be cleared by a later caught exception.
- `record_input_gap(symbol, endpoint, reason)` records degraded or incomplete coverage independently from service health.
- `to_receipt()` returns sanitized JSON-compatible fields. In this scope, `overall_readiness` is `fail` only for an established failed criterion and otherwise `unverified`; it never returns `pass`.

- [ ] **Step 1: Write the failing outcome tests**

Add cases that prove a local quota deferral changes input coverage without marking the service crashed, a hard-cap denial remains latched after later work, and a healthy observation still cannot claim #107 readiness.

```python
def test_local_ledger_deferral_is_degraded_input_not_service_failure():
    observation = SchedulerObservation(run_id="test")
    observation.record_input_gap("AAPL", "income-quarterly", "local_ledger_exhausted")
    receipt = observation.to_receipt()
    assert receipt["service_health"] == "healthy"
    assert receipt["required_input_coverage"] == "degraded"
    assert receipt["overall_readiness"] == "unverified"


def test_latched_cap_denial_survives_later_work_and_never_passes_readiness():
    observation = SchedulerObservation(run_id="test")
    observation.latch_service_issue("alpaca_cap_denied", {"attempt": 257})
    observation.record_event("scan", "large_cap", "completed", {})
    receipt = observation.to_receipt()
    assert receipt["service_health"] == "failed"
    assert receipt["overall_readiness"] != "pass"
```

- [ ] **Step 2: Run the new test and confirm it fails for missing observation APIs**

Run: `python -m pytest -q tests/test_scheduler_observation.py -k 'local_ledger_deferral or latched_cap_denial'`

Expected: FAIL because `core.scheduler_observation` and its outcomes are not implemented.

- [ ] **Step 3: Implement the thread-safe observation contract**

Use a process-level lock and one active observation so executor threads report into the same state. Keep service status, required-input coverage, and issue readiness separate.

```python
def to_receipt(self) -> dict[str, object]:
    with self._lock:
        service_health = self._service_health
        coverage = self._required_input_coverage
        if service_health == "failed":
            overall = "fail"
        else:
            overall = "unverified"
        return {
            "service_health": service_health,
            "required_input_coverage": coverage,
            "overall_readiness": overall,
            "events": list(self._events),
            "resource_denials": dict(self._resource_denials),
        }
```

- [ ] **Step 4: Run the observation unit tests**

Run: `python -m pytest -q tests/test_scheduler_observation.py -k 'local_ledger_deferral or latched_cap_denial'`

Expected: PASS; later tasks extend this file with full scheduler integration.

- [ ] **Step 5: Commit the observation contract**

```powershell
git add core/scheduler_observation.py tests/test_scheduler_observation.py
git commit -m "feat: add scheduler observation outcome contract"
```

## Task 2: Count and latch whole-process Alpaca HTTP attempts

**Files:**
- Modify: `core/alpaca_client_policy.py`
- Modify: `tests/test_alpaca_client_policy.py`

**Interfaces:**
- Preserve `alpaca_http_request_budget(max_requests)` and `single_attempt_alpaca_requests()`.
- Add `alpaca_http_request_snapshot()` with `attempts`, `cap_denials`, `cap`, and `sdk_retries` fields for the active budget.
- On refusal, increment the denial count and call `latch_service_issue("alpaca_cap_denied", ...)` before raising `AlpacaHttpRequestBudgetExceeded`.

- [ ] **Step 1: Add failing tests for attempts, failures, and denials**

Extend the fake session tests so every call to `session.request`, including one that raises, consumes one attempt; the 257th reservation must be denied and latch the observation.

```python
from types import SimpleNamespace
from unittest.mock import Mock

def test_alpaca_budget_counts_failed_request_and_latches_next_denial():
    session = SimpleNamespace(request=Mock(side_effect=TimeoutError("offline")))
    client = SimpleNamespace(_session=session, _retry=3, _retry_wait=1)
    observation = SchedulerObservation("test")
    with alpaca_http_request_budget(1), activate_scheduler_observation(observation):
        configure_alpaca_rest_client(client)
        with pytest.raises(TimeoutError):
            session.request("GET", "/one")
        with pytest.raises(AlpacaHttpRequestBudgetExceeded):
            session.request("GET", "/two")
        assert alpaca_http_request_snapshot()["attempts"] == 1
        assert alpaca_http_request_snapshot()["cap_denials"] == 1
```

- [ ] **Step 2: Run the focused policy tests and confirm the new assertions fail**

Run: `python -m pytest -q tests/test_alpaca_client_policy.py`

Expected: FAIL because the current budget exposes neither counts nor a denial latch.

- [ ] **Step 3: Add lock-protected attempt and denial accounting**

Reserve immediately before the wrapped session call, preserve zero-retry restoration, and expose a copied snapshot under the existing budget lock. Do not count a cap refusal as an actual HTTP request.

```python
def request_with_timeout(*args: Any, **kwargs: Any) -> Any:
    _reserve_request()  # records/latches a denial before any network I/O
    kwargs.setdefault("timeout", settings.ALPACA_HTTP_TIMEOUT_SECONDS)
    return original_request(*args, **kwargs)
```

- [ ] **Step 4: Run the policy tests**

Run: `python -m pytest -q tests/test_alpaca_client_policy.py`

Expected: PASS, including existing pagination and timeout checks; an observation-mode paginated fake client records `sdk_retries == 0` and every page attempt against the same process cap.

- [ ] **Step 5: Commit the Alpaca accounting**

```powershell
git add core/alpaca_client_policy.py tests/test_alpaca_client_policy.py
git commit -m "feat: expose bounded Alpaca attempt evidence"
```

## Task 3: Separate FMP ledger deferral from FMP process-cap and provider failures

**Files:**
- Modify: `core/data_client.py`
- Modify: `tests/test_data_client.py`
- Modify: `tests/test_stock_screening.py`

**Interfaces:**
- Preserve the existing ledger format and `fmp_request_budget(max_requests)` API.
- Add `fmp_request_deferral_reason()` for `local_ledger`, `process_cap`, `ledger_missing`, `ledger_unreadable`, `provider_refusal`, and `transport_error`; reset it only at the existing per-worker request-context boundary. Provider outcomes retain exact HTTP status (402/403/404/429) in observation events.
- Require the existing ledger while observation is active. Missing or unreadable ledger state latches service failure from both observation preflight and `_reserve_fmp_request`; only an existing file whose `window_start` belongs to an older reset window may roll over to a fresh count.
- Each permitted logical request increments process observation evidence before transport. Free-plan transport retries remain zero.

- [ ] **Step 1: Write failing tests for the three distinct outcomes**

Use a temporary ledger path and fixed ET time. Cover these cases independently: local allowance exhausted with process budget available; process cap at zero with local allowance remaining; ledger absent at observation start; ledger deleted or made unreadable after observation preflight; existing prior-window ledger rolling over; each synthetic HTTP status 402, 403, 404, and 429; and a `requests` transport exception. Assert no denied request reaches transport, only an existing prior-window file may roll over, local exhaustion degrades coverage without service failure, and missing/unreadable/provider/transport outcomes are separately identified and latched as failures. Requests and the existing ledger must remain unchanged outside the temporary test directory.

- [ ] **Step 2: Run the relevant FMP and screening tests and confirm failure**

Run: `python -m pytest -q tests/test_data_client.py tests/test_stock_screening.py -k 'fmp or quota'`

Expected: FAIL because the current thread-local boolean merges local and run-budget denial.

- [ ] **Step 3: Preserve the reason through the worker result and observation**

Differentiate local ledger remaining from process-local allowance in `_reserve_fmp_request`; in observation mode, treat `FileNotFoundError` and unreadable-ledger errors as failures instead of a zero-count new ledger. Preserve prior-window rollover only when an existing valid ledger file is read. Record provider refusal or transport failure only after the actual attempt. Aggregate per-symbol local deferrals across scanner workers without changing existing entry classification or thresholds.

```python
if local_remaining <= 0:
    _set_fmp_deferral_reason("local_ledger")
    return False
if process_remaining <= 0:
    _set_fmp_deferral_reason("process_cap")
    return False
```

`_fmp_get(endpoint, params)` consumes that reason immediately after a denied reservation, where it has the endpoint and requested symbol: record a local-ledger input gap there, or latch a process-cap failure there. `_reserve_fmp_request()` does not receive endpoint or symbol arguments.

- [ ] **Step 4: Run the focused FMP and candidate-classification tests**

Run: `python -m pytest -q tests/test_data_client.py tests/test_stock_screening.py -k 'fmp or quota'`

Expected: PASS; the existing quota-deferred candidate remains excluded from buys and is not relabeled as provider entitlement success.

- [ ] **Step 5: Commit the FMP outcome distinction**

```powershell
git add core/data_client.py tests/test_data_client.py tests/test_stock_screening.py
git commit -m "feat: distinguish FMP quota deferral and refusal"
```

## Task 4: Count all index-source attempts, including fallbacks

**Files:**
- Modify: `core/index_ticker_fetcher.py`
- Modify: `tests/test_index_ticker_fetcher.py`

**Interfaces:**
- Add one request helper used by Wikipedia, iShares page, CSV, and fallback calls.
- The helper reserves one of 6 process-wide index attempts before `requests.get`; it preserves the current timeout, parsing, fallback order, and cache semantics.

- [ ] **Step 1: Write failing page/CSV/fallback count tests**

Patch `requests.get` with synthetic responses and assert every page, CSV, Wikipedia fallback, and failed call consumes one slot; the next call is denied without network access and latches an issue.

- [ ] **Step 2: Run index tests and confirm the counter assertions fail**

Run: `python -m pytest -q tests/test_index_ticker_fetcher.py`

Expected: FAIL because direct `requests.get` calls are not counted.

- [ ] **Step 3: Route all index requests through the capped helper**

Replace each direct call at the page, CSV, and Wikipedia/fallback boundaries with the shared request helper. Do not alter source priority, cache TTL, universe validation, or fallback ticker behavior outside an observation cap denial.

```python
def _index_get(url: str, **kwargs: object) -> requests.Response:
    observation = current_scheduler_observation()
    if observation is not None:
        observation.reserve_index_attempt(url)
    return requests.get(url, **kwargs)
```

- [ ] **Step 4: Run the index suite**

Run: `python -m pytest -q tests/test_index_ticker_fetcher.py`

Expected: PASS with existing parsing/fallback tests unchanged and new attempt accounting tests passing.

- [ ] **Step 5: Commit index attempt accounting**

```powershell
git add core/index_ticker_fetcher.py tests/test_index_ticker_fetcher.py
git commit -m "feat: cap scheduler index-source attempts"
```

## Task 5: Enforce workflow-write ceilings in the existing store

**Files:**
- Modify: `core/execution_store.py`
- Modify: `tests/test_execution_workflow.py`

**Interfaces:**
- Add observation counters at existing append/upsert transaction boundaries for transitions and snapshots.
- On the seventh transition or third snapshot, refuse before `INSERT`/`COMMIT`, latch `workflow_write_cap_denied`, and leave C/#100 schema and action identity unchanged.

- [ ] **Step 1: Add temporary-database cap tests**

Exercise existing store methods with a 6-transition/2-snapshot allowance; assert allowed writes retain their original workflow IDs and the next write is denied without a new table or altered identity record.

- [ ] **Step 2: Run the focused workflow test and confirm it fails**

Run: `python -m pytest -q tests/test_execution_workflow.py -k 'observation_write_cap'`

Expected: FAIL because store methods currently have no observation counter.

- [ ] **Step 3: Reserve each durable write before its existing transaction**

Instrument `append_transition`, `persist_transition_and_snapshot`, and `upsert_workflow_snapshot` using the active observation. Count the transition and snapshot in `persist_transition_and_snapshot` atomically; do not add a second store, schema, identity registry, or table.

- [ ] **Step 4: Run workflow tests and confirm schema/identity stability**

Run: `python -m pytest -q tests/test_execution_workflow.py`

Expected: PASS; cap-refused operations leave row counts unchanged and all existing C/#100 identity checks pass.

- [ ] **Step 5: Commit store write accounting**

```powershell
git add core/execution_store.py tests/test_execution_workflow.py
git commit -m "feat: bound observation workflow store writes"
```

## Task 6: Make ordinary exit reads and outcomes explicit

**Files:**
- Modify: `auto_trader.py`
- Modify: `tests/test_hourly_monitor.py`

**Interfaces:**
- In observation mode, daily and hourly monitors call the existing `get_open_positions(raise_on_error=True)` API.
- Record a successful empty inventory separately from failed inventory. Record failed or insufficient daily/hourly bars as unavailable work; keep return values and signal rules unchanged outside observation mode.

- [ ] **Step 1: Write failing inventory and bar-outcome tests**

Add synthetic cases for successful empty positions, an inventory exception, positions with a failed daily bar read, and positions with insufficient hourly bars.

```python
def test_observation_does_not_turn_failed_inventory_into_zero_positions():
    observation = SchedulerObservation("test")
    with activate_scheduler_observation(observation), patch(
        "auto_trader.get_open_positions", side_effect=RuntimeError("offline")
    ):
        with pytest.raises(RuntimeError):
            monitor_exits_hourly(dry_run=True)
    assert observation.to_receipt()["service_health"] == "failed"
```

- [ ] **Step 2: Run the exit-monitor tests and confirm failure**

Run: `python -m pytest -q tests/test_hourly_monitor.py`

Expected: FAIL because both monitors currently call `get_open_positions()` without strict error propagation and skip unusable bars.

- [ ] **Step 3: Add observation-only strict outcomes**

Use the existing strict broker-read API only while an observation is active. Preserve current behavior when it is absent. Record one outcome per position and interval; an insufficient bar frame is `unavailable`, not `no_exit`.

- [ ] **Step 4: Run monitor tests**

Run: `python -m pytest -q tests/test_hourly_monitor.py`

Expected: PASS; ordinary exit calculations and order boundaries remain unchanged.

- [ ] **Step 5: Commit strict exit outcomes**

```powershell
git add auto_trader.py tests/test_hourly_monitor.py
git commit -m "feat: record strict scheduler exit outcomes"
```

## Task 7: Retain full-scan coverage and candidate reasons

**Files:**
- Modify: `enhanced_scanner.py`
- Modify: `core/stock_screening.py`
- Modify: `tests/test_enhanced_scanner_cli.py`
- Modify: `tests/test_stock_screening.py`

**Interfaces:**
- Observation adds requested, validated, analyzed, RS-covered, and fundamental-covered counts without changing the existing three-value `scan_for_canslim_stocks` return contract.
- Per-symbol FMP local deferral remains a `quota_deferred` candidate reason. Process-cap/provider errors stay separate and latch through the observation.

- [ ] **Step 1: Add failing full-universe and candidate-reason tests**

Use synthetic S&P 500 and Nasdaq 100 constituents plus the six configured extras. Assert the existing `large_cap` selection is used, duplicates are handled by the existing logic, and every deferred candidate carries endpoint coverage and an exact reason.

- [ ] **Step 2: Run scanner/screening tests and confirm the new coverage assertions fail**

Run: `python -m pytest -q tests/test_enhanced_scanner_cli.py tests/test_stock_screening.py`

Expected: FAIL because the scanner prints counts but does not expose the complete observation coverage record.

- [ ] **Step 3: Attach coverage to the active observation**

Record requested symbols before validation, validated symbols after `validate_tickers_bulk`, and analyzed/candidate outcomes after `screen_stocks_canslim_detailed`. Preserve all scoring, filters, retry settings, and result tuple semantics.

- [ ] **Step 4: Run focused scanner tests**

Run: `python -m pytest -q tests/test_enhanced_scanner_cli.py tests/test_stock_screening.py`

Expected: PASS with no narrowed universe or changed entry decision.

- [ ] **Step 5: Commit coverage accounting**

```powershell
git add enhanced_scanner.py core/stock_screening.py tests/test_enhanced_scanner_cli.py tests/test_stock_screening.py
git commit -m "feat: record ordinary scan data coverage"
```

## Task 8: Wire observation into the normal scheduler loop

**Files:**
- Modify: `scheduler.py`
- Modify: `tests/test_scheduler_runtime.py`

**Interfaces:**
- Add `--observe-health`; require dry-run, `--now`, and `--session` when enabled. Keep existing `--fmp-daily-budget` argument.
- Validate `ALPACA_HTTP_TIMEOUT_SECONDS == 15`, `FMP_HTTP_TIMEOUT_SECONDS == 15`, `INDEX_TICKER_HTTP_TIMEOUT_SECONDS == 15`, and `ALPACA_SDK_RETRY_ATTEMPTS == 0` before any observation work; refuse direct CLI invocation with any other setting.
- Activate the Alpaca, FMP, index, and store budgets before the one provider-clock preflight. The preflight must report open before `_run_cycle`; it is included in the 256 Alpaca attempt ceiling.
- Wrap the same `_run_cycle`, `monitor_exits_hourly`, and `monitor_and_exit_positions` calls used by ordinary scheduling. Record expected/due/start/outcome events; never replace unknown clock with local time in observation mode.
- Fill monitor remains disabled. Skip both `notify_cycle_summary` call sites in observation mode. A caught scan/exit exception latches failure even if the CLI returns zero.

- [ ] **Step 1: Add failing parser and clock/schedule tests**

Test that observation mode rejects order-enabled or unbounded invocation and refuses each incorrect timeout/retry setting; that the clock preflight occurs after budget activation; that unknown clock prevents the scan; that both scheduler notification sites are skipped with parent notification credentials present; and that existing non-observation CLI defaults remain unchanged.

```python
def test_observation_clock_read_occurs_inside_alpaca_budget_before_scan():
    # In the test, append "budget_enter" from the fake Alpaca budget context
    # manager and "clock" from the patched _market_clock_is_open. Run the
    # bounded observation CLI with due scheduler work stubbed; assert the
    # budget event comes first. Repeat with a None clock and assert _run_cycle
    # is never reached.
```

- [ ] **Step 2: Run scheduler tests and confirm new observation assertions fail**

Run: `python -m pytest -q tests/test_scheduler_runtime.py`

Expected: FAIL because `--observe-health` and observation event wiring are absent.

- [ ] **Step 3: Add observation-only CLI and lifecycle wiring**

Create `SchedulerObservation` after argument validation, enter all whole-process budgets, perform the counted authoritative clock read, then enter the unchanged scheduler path. In each expected-work branch record a started event and a completed/failed event. Keep the ordinary branch unchanged.

- [ ] **Step 4: Run scheduler tests**

Run: `python -m pytest -q tests/test_scheduler_runtime.py`

Expected: PASS; existing mode behavior is unchanged and observation mode fails closed on unknown clock or caught expected-work errors.

- [ ] **Step 5: Commit scheduler wiring**

```powershell
git add scheduler.py tests/test_scheduler_runtime.py
git commit -m "feat: observe ordinary scheduler work outcomes"
```

## Task 9: Exercise the full ordinary scan and exit-loop control path

**Files:**
- Modify: `tests/test_scheduler_observation.py`
- Modify: `tests/test_notifier.py`

**Interfaces:**
- The integration test calls the real `scheduler.main`/`run_scheduler` control path with patched time, synthetic Alpaca/FMP/index responses, a fake notification backend, and a temporary execution database.
- It uses `SECTORS=large_cap`; it must not replace the scanner with AAPL, skip normal exits, or replace the scheduler loop with a receipt formatter.

- [ ] **Step 1: Write the failing full-path synthetic session test**

Feed synthetic constituents and bars through the ordinary scanner, then advance fake ET time to exercise the immediate scan and scan-phase exit check, 15:xx hourly check, 30-minute fallback when due, 16:01 hourly check, later fallback work when due, and 16:05 session stop. Assert each expected work event and full requested-universe coverage.

- [ ] **Step 2: Write failing notification and status dimension assertions**

Patch both scheduler `notify_cycle_summary` call sites, SMTP, and Gmail delivery functions to fail the test if called, with sender/recipient credentials present in the test parent environment. Assert both scheduler calls are skipped, child notification identity is blank from launcher environment overrides, `service_health` is independent of `required_input_coverage`, and #107 `overall_readiness` never `pass` from this session alone.

- [ ] **Step 3: Run the new integration tests and confirm they fail for missing instrumentation**

Run: `python -m pytest -q tests/test_scheduler_observation.py tests/test_notifier.py -k 'ordinary_full_scan or notification or readiness_dimensions'`

Expected: FAIL until scheduler events, scan coverage, notification suppression checks, and status fields are wired.

- [ ] **Step 4: Complete the synthetic full-session assertions**

Run the ordinary scheduler with a temporary scheduler lock and temp DB; fake every external provider boundary. Assert there are no sockets opened, no order/fill path, neither scheduler summary call nor SMTP/Gmail call occurs, exact due work is recorded, and before/after row counts plus deterministic content hashes are captured for all five existing execution-store tables. Added snapshots/transitions stay within their caps; any other table-content delta latches failed/unverified service health.

- [ ] **Step 5: Run integration, scheduler, and notifier tests**

Run: `python -m pytest -q tests/test_scheduler_observation.py tests/test_scheduler_runtime.py tests/test_notifier.py tests/test_hourly_monitor.py`

Expected: PASS with the whole ordinary path exercised and no real provider access.

- [ ] **Step 6: Commit the synthetic full-path evidence**

```powershell
git add tests/test_scheduler_observation.py tests/test_notifier.py
git commit -m "test: cover ordinary scheduler observation offline"
```

## Task 10: Build the bounded offline launcher and document the future envelope

**Files:**
- Create: `.artifacts/issue-107-normal-scheduler/run-normal-dry-run-once.ps1` (ignored local file)
- Modify: `docs/paper-runtime-readiness.md`

**Interfaces:**
- The launcher sets notification and exact timeout/retry environment variables before starting Python, makes no provider read itself, invokes only the dry-run `scheduler.py --now --session --observe-health` child, and provides a 45-minute watchdog with bounded process-tree termination.
- Captured output starts in `.raw.txt` files; sanitized outputs are created only after child exit is observed and redaction completes. A surviving child prevents post-run DB/ledger/cache snapshot reads.
- The ignored helper includes an offline self-test mode with dummy Python children; it has no provider/runtime path when that mode is selected.

- [ ] **Step 1: Create the helper with a fail-closed offline-only mode**

Add argument validation, a unique evidence directory, child notification and timeout/retry-setting assertions, pre-run local identity/store/cache captures, raw stdout/stderr redirection before child start, PID/receipt persistence, watchdog, bounded kill/wait, post-stop sanitization, and before/after local evidence capture. Set `ALPACA_HTTP_TIMEOUT_SECONDS=15`, `FMP_HTTP_TIMEOUT_SECONDS=15`, `INDEX_TICKER_HTTP_TIMEOUT_SECONDS=15`, and `ALPACA_SDK_RETRY_ATTEMPTS=0` in the child environment before launch. Require and hash the existing FMP ledger before launch. Preserve the existing AAPL helper unchanged.

- [ ] **Step 2: Add offline capture cases before any operational branch**

Use a dummy Python child matching the operational argv to test normal output/exit, inherited notification credentials being blanked, all four timeout/retry variables carrying the exact values, timeout, injected post-start wrapper failure, and injected kill failure. Verify final receipts and truthful raw/sanitized file names; terminate every dummy child in test cleanup. For local store captures, assert deterministic row counts and content hashes cover all five existing tables and any unexpected delta changes the outer receipt's service health to failed/unverified.

- [ ] **Step 3: Run the helper parser and offline self-test only**

Run: `pwsh -NoProfile -File .artifacts/issue-107-normal-scheduler/run-normal-dry-run-once.ps1 -OfflineSelfTest`

Expected: all dummy-process cases pass, summary identifies `provider_calls=0`, and no runtime path is touched.

- [ ] **Step 4: Document the exact later envelope and limits**

Update `docs/paper-runtime-readiness.md` with the observation command, whole-process caps, FMP local-deferral/provider-denial distinction, separate outcome fields, evidence requirements, timing window, and the fact that this run cannot prove the installed disabled task or make #107 overall readiness PASS.

- [ ] **Step 5: Run documentation and working-tree checks**

Run: `git diff --check`

Expected: PASS; only paths in the approved list are present. Do not invoke the normal launcher mode.

- [ ] **Step 6: Commit launcher and runbook**

```powershell
git add docs/paper-runtime-readiness.md
git commit -m "docs: specify bounded scheduler health observation"
```

Keep the ignored launcher and offline evidence local; do not commit them.

## Task 11: Review the completed implementation without rollout

**Files:** Only the paths already listed above.

- [ ] **Step 1: Run the complete focused synthetic test set**

Run: `python -m pytest -q tests/test_scheduler_observation.py tests/test_scheduler_runtime.py tests/test_hourly_monitor.py tests/test_enhanced_scanner_cli.py tests/test_stock_screening.py tests/test_alpaca_client_policy.py tests/test_data_client.py tests/test_index_ticker_fetcher.py tests/test_execution_workflow.py tests/test_notifier.py`

Expected: PASS with all network clients faked and all database writes confined to temporary stores.

- [ ] **Step 2: Check formatting and exact changed-path scope**

Run: `git diff --check` and inspect `git status --short`.

Expected: no whitespace errors and no files outside the approved list.

- [ ] **Step 3: Verify base and ordinary behavior boundary**

Confirm the branch parent remains `f15d3db17eb1e2f1202b534e4f58e0fa1d6a06c4`, the new flag is opt-in, no scanner thresholds or scheduler timings changed, and the existing AAPL helper/proof are unchanged.

- [ ] **Step 4: Stop for independent source and executable review**

Provide the source diff, focused-test output, offline launcher proof, process caps, store deltas, and the explicit limits: no provider session, no scheduler/task launch, no runtime migration, no order, and no #107 overall readiness PASS. Do not roll out this revision or run the operational helper until the principal separately reviews and authorizes it.
