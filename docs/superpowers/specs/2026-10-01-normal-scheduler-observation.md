# Normal scheduler health observation design

**Status:** Prepared for principal design review. This specification authorizes no provider call, scheduler launch, task change, order, selected-runtime/store operation, quota reset, or source rollout.

**Source base:** `f15d3db17eb1e2f1202b534e4f58e0fa1d6a06c4` (`origin/main`). The pending one-AAPL acceptance question at this revision is separate and remains unresolved. Any source change produces a new revision and cannot inherit an AAPL result without a separate identity and eligibility review.

## Problem

The ordinary scan-bearing scheduler performs the supported full `large_cap` scan and its scheduled exit checks, but existing evidence can overstate health. Exit monitors can interpret a failed position read as zero positions, failed bar reads can become empty data, unknown Alpaca clock state can fall back to local time, and the scheduler can catch expected-work errors and still exit zero. A scan can finish while some candidates were deferred by the local FMP ledger. Process liveness, an installed task, a clean configuration check, or exit code zero does not prove that expected work ran successfully.

The existing explicit-symbol AAPL scan has bounded request contexts. The ordinary `run_auto_trader(symbol=None)` path does not apply equivalent whole-process budgets to its full scan and scheduler loop. The observation must therefore instrument the ordinary path without narrowing the universe or changing entry, exit, or timing behavior.

## Goals

- Observe one ordinary dry-run scheduler session with the full `large_cap` universe: S&P 500, Nasdaq 100, and the six configured extra symbols.
- Preserve current strategy thresholds, candidate selection, max positions, new-entry limit, normal exit logic, and scheduler timings.
- Apply whole-process request and workflow-write ceilings. Any refused request or write must remain visible even if a lower layer catches the exception.
- Record actual expected work, source/runtime/store identity, coverage, errors, and final process termination in a sanitized, machine-readable receipt.
- Prevent external notifications and order submission during the observation.
- Allow offline review with synthetic providers, fake notification backends, and temporary stores before any separately authorized operational run.

## Non-goals

- This change does not prove health of the installed disabled task, an order-enabled fill stream, startup stop reconciliation, or the deployment as a whole.
- It does not resolve the pending AAPL run at `f15d3db`, change a quota ledger, prewarm caches, replace the full universe with a smaller sample, or change strategy policy.
- It does not submit orders, activate or edit the installed task, send email, or grant authority for a provider-backed session.
- It does not introduce a second durable schema or action-identity policy. Workflow observations and write ceilings use the existing `core/execution_store.py` schema and preserve the C/#100 durable-schema and action-identity contracts.

## Observation behavior

Add an opt-in `--observe-health` mode to the ordinary scheduler CLI. It composes with the existing dry-run, `--now`, `--session`, and `--fmp-daily-budget` options. The flag changes only observation, counters, failure reporting, and notification suppression; without the flag, ordinary scheduler behavior remains unchanged. Observation mode is accepted only for dry-run session execution. Fill monitoring remains disabled, and order mutation remains unavailable.

Before Python imports settings, the one-session launcher sets `NOTIFY_EMAIL_PROVIDER=auto` and empty sender, recipient, and password values. The child verifies the resulting notification settings are empty. Offline fake-notifier tests verify that no SMTP or Gmail transmission path is invoked even when the parent environment contains credentials. No email test is sent.

### Whole-process ceilings

The proposed run limits are:

| Resource | Maximum and counting rule |
|---|---|
| Alpaca HTTP | 256 actual request attempts, including clock, trading, data, pagination, failed attempts, and the single provider-clock preflight read. SDK retries are zero. |
| FMP | At most 198 additional logical requests, further limited to the remaining allowance in the existing reset window. Free-plan transport retries are zero. The ledger is required, retained, and never reset or replaced. |
| Index sources | 6 actual attempts covering page, CSV, fallback, and failed attempts for the two `large_cap` indices. |
| Request inactivity | 15 seconds for Alpaca, FMP, and index-source requests. This is not the outer process deadline. |
| Workflow store writes | At most 6 added transitions and 2 added snapshots, subject to preflight confirmation of the existing maximum of 2 new entries and 5 open positions. Enforce and measure these against the existing C/#100 schema and action identities; add no competing persistence schema or identity policy. |
| Process | One launch with a separate 45-minute watchdog, bounded termination observation, and no automatic retry. |

Counters reserve an attempt before network I/O so failed requests count. A hard-cap refusal latches an unhealthy observation even if strategy or scheduler code catches the error. Reaching a cap exactly is conservatively unverified unless the receipt proves no further request was refused. The FMP increment is `min(198, existing local remaining allowance)`; provider-side remaining quota is not available locally and is not probed. A provider refusal is captured and does not authorize a retry.

The Python observation context activates the Alpaca budget before making any provider request. Its one authoritative clock preflight then runs inside that context and consumes one of the 256 attempts. The launcher performs no clock, account, position, order, or other provider read outside the child. The clock must report open before the immediate scan starts; a confirmed closed or unknown result ends the attempt as failed/unverified without scanning.

FMP outcomes remain distinct. A candidate deferred before network I/O because the existing local ledger has no remaining allowance is recorded as unavailable/degraded input, with candidate identity, endpoint coverage, and its rejection reason. It is not represented as a provider entitlement pass or as a service crash. A provider denial and a process-wide cap refusal have their own outcomes and latch unhealthy evidence. Any such gap prevents an overall readiness PASS because coverage is incomplete.

The receipt carries three separate outcome fields: `service_health` (`healthy`, `failed`, or `unverified`), `required_input_coverage` (`complete`, `degraded`, `failed`, or `unverified`), and `overall_readiness` (`pass`, `fail`, or `unverified`). A locally quota-deferred candidate, by itself, is not a service crash: it sets input coverage to `degraded`. An observed provider refusal, hard-cap denial, failed required read/work, unknown required clock, or missed due work sets service health to `failed` where failure is observed and `unverified` where evidence is incomplete. This scheduler observation alone never emits `overall_readiness=pass` for #107: that field defaults to `unverified` unless a separately reviewed evidence aggregator receives all eligible receipts at the exact compatible source, including the AAPL dry-run, account/FMP entitlement, runtime identity, and other required criteria. This scope adds no such aggregator. A readiness `fail` may be emitted when this observation establishes a failed required criterion; otherwise issue-level readiness remains `unverified`.

### Work outcomes and timing

At session start, an authoritative Alpaca market clock must report open. A confirmed closed clock and a failed/unknown clock are separate outcomes; unknown is not replaced by local time in observation mode. Expected scans and exit checks record scheduled/due time, actual start, completion or failure, and any reason they were missed. The ordinary schedule is preserved.

The receipt accounts for the immediate full scan and its scan-phase exit check, the immediate 15:xx hourly check, the 30-minute fallback check when due, the 16:01 hourly check, and later fallback work when due. If a long scan prevents a check from starting on time, the receipt records missed work rather than inferring success from process liveness.

Position inventory is successful only when the broker read succeeds. A successful empty result is a valid zero-position observation; a failed read is unavailable work. When positions exist, failed or insufficient daily/hourly bars are unavailable checks, not healthy no-exit results. Expected scan, inventory, bar, clock, and exit-work errors latch a failed or unverified observation even when the existing CLI catches an exception and returns exit zero.

The scanner records requested, validated, and analyzed symbol counts, RS comparison-universe coverage, fundamental endpoint coverage, quota deferrals, and candidate rejection reasons. It retains existing selection and scoring. Missing or unavailable inputs may safely withhold a candidate but cannot be presented as complete coverage. A no-buy result is valid only when its data and gate reasons are complete.

### Receipt and capture

The observer emits structured, sanitized evidence for:

- source revision and dirty-tree state, interpreter/runtime identity, selected store, and relevant configuration;
- request attempts, caps, cap-denial latches, timeouts, provider outcomes, and FMP local-ledger before/after counts;
- requested, validated, and analyzed universe counts; RS/fundamental coverage; and candidate-specific unavailability/rejection reasons;
- every expected scan, clock check, and due exit check with start and outcome;
- workflow/snapshot counts and unexpected store mutations;
- before/after SQLite integrity, file hash, and relevant table row counts; before/after hashes for the existing FMP ledger, ticker cache, RS cache, and fundamentals-cache files;
- stdout/stderr references, process ID, exit status, watchdog state, and whether child termination was observed.

The launcher retains raw output in files explicitly labeled raw until the child is observed stopped and redaction succeeds. Only then may sanitized output be written. If termination is not observed, the receipt says the child may still be running, leaves the files labeled raw, and does not claim sanitization or take a post-run store snapshot. The launcher does not retry or start a second scheduler.

The before snapshot is read-only and records SQLite `quick_check`, SHA-256, and relevant row counts plus hashes of the existing ledger and caches. The after snapshot records the same values only after child termination has been observed; it reports integrity, hash, and row-count deltas. A surviving child means all post-run store, ledger, and cache reads are skipped because writes or cache updates may still be in flight.

## Offline verification

Focused tests use synthetic provider responses, a fake clock, a fake notifier, and temporary SQLite stores. They cover:

- actual Alpaca attempts and pagination, retries disabled, and cap refusal that remains latched when caught;
- FMP local-ledger deferral versus provider denial and process-cap denial, including exhausted allowance without ledger reset;
- index page/CSV/fallback counting and index cap refusal;
- successful zero inventory versus failed inventory; sufficient versus failed/insufficient hourly and daily bars;
- authoritative open, authoritative closed, and unknown clock outcomes;
- due and missed startup/scan, 15:xx, 16:01, and fallback exit work without altering timing;
- full-universe requested/validated/analyzed coverage and unchanged scanner rules;
- workflow/snapshot limits and unexpected writes in temporary stores;
- no notification transmission through SMTP or Gmail and no fill-stream/order path in dry-run;
- captured output, timeout, wrapper failure, kill failure, final receipt, and truthful surviving-child status.

At least one integration test runs the ordinary scheduler control path through its full scan and exit loop with a synthetic clock and provider clients. It uses the normal `large_cap` symbol selection and unchanged scan/exit functions, with fake responses instead of network access; it verifies the immediate scan, 15:xx and 16:01 hourly checks, and due fallback checks. A receipt-only test or a test that replaces the full scan with an explicit symbol is insufficient.

No test contacts a real provider, uses the selected runtime/store, launches the scheduler/task, or sends a notification.

## Proposed later operational envelope

Only after independent source review, implementation approval, fresh runtime identity checks, and a separate direct authorization for this normal-session observation, the proposed command is:

```powershell
.\.venv\Scripts\python.exe -u scheduler.py --dry-run --now --session --fmp-daily-budget 198 --observe-health
```

The proposed start window is 15:44:30–15:45:30 ET on a regular weekday, with the provider clock authoritatively open. The session stops after 16:05 ET. Preflight verifies the exact source, interpreter and 71-package lock; paper configuration; `SECTORS=large_cap`; six extra symbols; limits of 5 open positions and 2 new entries; existing absolute store integrity; existing FMP ledger and remaining allowance; cache identities; disabled task; suppressed notifications; and absence of another writer. The database is backed up through SQLite's backup API and existing ledger/cache files are preserved. No separate account, positions, or open-orders probes are included.

The result establishes evidence only for the observed dry-run process at its exact source and store. A disabled installed task and service health remain separate criteria. A quota-deferred or otherwise incomplete run may provide useful negative evidence, but does not pass readiness and never triggers a retry. Any source change requires a new revision-specific eligibility review; the pending AAPL question at `f15d3db` remains distinct.
