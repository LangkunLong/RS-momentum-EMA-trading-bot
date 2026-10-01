# Paper runtime identity and readiness runbook

This runbook prepares evidence for Trading-11 (#107). It does not enable trading, promote a strategy, or establish service health by itself. Keep configuration, provider access, persistence, scheduler installation, actual service health, and strategy dry-run evidence as separate findings.

## Identity record

Record two distinct fields for each readiness evidence package:

- `source_revision`: the exact Git revision of the code being observed. Also record whether the checkout has uncommitted changes; a commit ID alone does not identify a dirty source tree.
- `runtime_identity`: the execution environment: checkout path, interpreter path and version, hashes for `requirements-lock.txt` and `pyproject.toml`, relevant installed versions, sanitized configuration profile, persistent-store path, FMP request-ledger path, FMP fundamentals-cache path, and paper environment. A missing dependency file is recorded as `unavailable`. After a successful Alpaca read, include only a one-way account-ID fingerprint. Never include API keys, secrets, raw account IDs, or credential-bearing URLs.

The console prints these names and facts in its `Runtime identity` row. It marks canonical deployment selection unverified until an operator selects the checkout and identity record. This runtime identity is separate from the policy artifact digest, policy interface, feature contract/calculator, data format, execution profile, and cost assumptions. Keep those strategy identities under the shared #80/#97 contracts.

Current application configuration uses Alpaca for market prices and FMP for financial statements. The FMP price endpoint probe below answers the separate issue-level entitlement question; a pass does not mean the application consumes FMP prices.

## Readiness claims

| Claim | Evidence and boundary |
| --- | --- |
| Configuration | Paper mode and required setting presence. Presence proves configuration only. The report never prints credential values. |
| Execution-store binding | An explicit absolute `EXECUTION_STORE_DB_PATH` must resolve to the same existing file as application settings. This identifies the store path; canonical deployment selection remains unverified until an operator accepts the identity record. |
| Alpaca connectivity and inventory | At most three bounded SDK reads (account, positions, open orders); open orders have a 500-item cap. Reads occur only after explicitly requesting provider probes and when paper mode is selected. A separate hashed fingerprint binds the observed account without printing its raw identifier. If account access or identity is unavailable, positions/orders are not read. A response reaching the order cap is incomplete and cannot support reconciliation. |
| Broker/local reconciliation | Read-only comparison with the explicitly bound store. Every broker position must match one local active position and a workflow in a protected state. The latest durable successful protective-stop transition must identify the current broker stop, whose remaining quantity must match the position. A snapshot advanced to a buy-fill notification state is accepted only with that stop evidence. Every open order must map to one local owner and compatible role/state: an additional sell order blocks reconciliation, and an entry buy must belong to a submitted workflow or a protected partial fill. Missing store/inventory, ambiguous ownership, or malformed data cannot pass. Conflicts are reported without repair. |
| FMP financial inputs | One representative request each for quarterly income, annual income, and annual balance sheet. Each is limited to five records and checks the symbol/date plus strategy-relevant sample fields. A sample supports that endpoint only; history and universe coverage still require dry-run evidence. |
| FMP price entitlement | One representative historical EOD request over the preceding ten calendar days. A matching symbol/date/close record supports this endpoint probe only. |
| Persistence | Read-only SQLite integrity and expected-schema inspection of the explicitly bound existing execution store. A missing store remains unverified; readiness code does not create, initialize, migrate, or write it. |
| Scheduler installation | Read-only Windows Task Scheduler query for the configured task name. Registration does not establish that its action points to the selected interpreter/checkout or that it is healthy. Review the task XML/action, working directory, mode, trigger, and log path against `source_revision` and `runtime_identity`. |
| Actual service health | Unverified by static `doctor` output. A task record or process alone does not prove recent successful work, stream health, or startup stop reconciliation. Observe the intended process/log/heartbeat and, for an order-enabled scheduler, the fill stream and reconciliation result. |
| Strategy dry run | Unverified until a fresh no-order strategy run is observed and retained with its source/runtime identity, bounded input scope, freshness/coverage, decisions, missing-data notes, and rejection reasons. A buy signal is not required. |

Console result meanings are `PASS`, `FAIL`, and `UNVERIFIED`. Exit status is 0 only if every reported check passes, 1 if a check fails, and 2 if evidence remains unverified. No single aggregate message authorizes order submission.

## Offline inventory and local report

From the checkout selected with the #97 deployment owner, use the intended interpreter and inspect source/runtime identity before any external validation:

```powershell
$python = (Resolve-Path '.venv\Scripts\python.exe').Path
& $python --version
git rev-parse HEAD
git status --short
& $python paper_trading_console.py doctor
```

Set `EXECUTION_STORE_DB_PATH` in both interactive and scheduled-task environments to the selected existing database's absolute path. `doctor` reports the binding and does not contact Alpaca or FMP by default. It observes the configured checkout/interpreter, checks configuration presence, queries scheduler installation read-only, and inspects that existing store without mutation. An incomplete result is expected while canonical selection or other evidence is outstanding. Do not interpret a configured path or a present credential setting as proof of connectivity or entitlement.

Preserve the selected database, FMP request ledger, and fundamentals cache at their existing paths. Do not copy them into a fresh checkout to reset usage, make a different store appear canonical, or let readiness initialize them. This readiness work uses read-only store inspection; it does not test writes or migrate a store. A separate write test would need its own explicitly identified scratch store and would not prove the operational store can accept writes.

Before a strategy dry run, preserve the execution database with SQLite's backup API so WAL state is included. Also back up the FMP request ledger, fundamentals cache, RS-score cache, and ticker-membership cache. The bounded scan can update these caches and increment the existing FMP ledger; it must not replace or reset the ledger.

## Explicit provider probes

Only after the paper environment and finite request scope are authorized, run:

```powershell
& $python paper_trading_console.py doctor --probe-external
```

This performs at most three read-only Alpaca operations (account, positions, and open orders); open orders have a fixed 500-item cap. A response at the cap is incomplete and keeps reconciliation `UNVERIFIED`. The probe also makes four logical FMP operations for AAPL: quarterly `income-statement`, annual `income-statement`, annual `balance-sheet-statement`, and `historical-price-eod/full` over the preceding ten calendar days. Statement responses are capped at five records. The FMP probe refuses to run unless the existing request-ledger file is present; it does not create a replacement ledger. FMP calls use the configured request accounting and may consume allowance. On the free plan, the data client disables transport retries, so the four logical requests make at most four HTTP attempts. On paid plans, `HTTP_RETRY_TOTAL` is five retries plus the initial attempt, so the four requests can make at most 24 HTTP attempts. The free-plan ledger counts logical requests, not retry attempts. Confirm the selected plan, finite retry bound, and remaining allowance before authorizing the probe. The check does not submit orders or repair broker state. If FMP returns no usable sample record, entitlement remains `UNVERIFIED`; empty data, entitlement denial, quota exhaustion, and some transport failures remain indistinguishable through the current adapter.

When `ALPACA_PAPER` is false, the probe refuses **all** provider calls, including FMP. Never adapt this option to live-account credentials. Keep provider results distinct and timestamp them; a statement pass says nothing about price entitlement, and neither says anything about Alpaca market-data feed coverage for the strategy universe.

## Scheduler and service observation

`doctor`'s scheduler row answers only whether the named task is registered. Inspect the exact installed task action and its executable, arguments, working directory, order mode, trigger, and log destination. Compare them with the selected checkout and interpreter; do not modify or reinstall a task as part of an inventory-only check.

For service health, record an observation from the intended scheduler session: process/session identity, recent log timestamp and outcome, and any required fill-stream and startup stop-reconciliation readiness. A dry-run scheduler does not have the same broker-stream requirements as an order-enabled session. A registered task that has not run, a stale log, a failed last result, or a missing required readiness marker is not healthy-service evidence.

### Bounded ordinary-scheduler observation for #107

The checkout-local ignored helper `.artifacts/issue-107-normal-scheduler/run-normal-dry-run-once.ps1` prepares one bounded observation of the ordinary scheduler path. Its offline self-test is safe to run without provider credentials:

```powershell
pwsh -NoProfile -File .artifacts/issue-107-normal-scheduler/run-normal-dry-run-once.ps1 -OfflineSelfTest
```

That mode launches only dummy Python children with the scheduler-shaped argument list. It checks inherited notification credentials are cleared in the child, request/retry settings are bounded, lower FMP ceilings are preserved, stdout/stderr and receipts are retained, the 45-minute watchdog path terminates a child tree, wrapper/kill failures produce truthful receipts, and missing scheduler receipts fail closed. A synthetic reviewed-preflight manifest checks the hash-bound approval gate and rejects a reviewed FMP budget that would raise the currently configured local ceiling. It also uses a scratch SQLite database to verify row-count/content-hash capture for all five execution-store tables, detect an unexpected row mutation, and verify post-run capture failures are persisted. The self-test reports `provider_calls=0` and does not inspect or touch the selected runtime, provider, scheduler task, ledger, cache, or execution-store state.

The helper's normal mode is a separate operational action and was not run as part of this implementation. Before using it, select the intended checkout and interpreter, set `EXECUTION_STORE_DB_PATH` to the existing absolute database, ensure the existing FMP request ledger is present, and obtain the separate authorization for provider reads. Normal mode requires both an external reviewed preflight JSON file and its separately approved SHA-256:

```powershell
pwsh -NoProfile -File .artifacts/issue-107-normal-scheduler/run-normal-dry-run-once.ps1 `
  -ReviewedPreflightManifest 'C:\path\outside\checkout\preflight.json' `
  -ReviewedPreflightSha256 '<approved-sha256>'
```

The manifest must be approved for `normal_dry_run_observation` within the prior 24 hours and bind the clean source revision, exact launcher hash, selected Python 3.13.14 interpreter and 71-package inventory, dependency-file hashes, bounded settings and caps, the exact six configured extra symbols, provider scope, absolute store/ledger/cache paths and hashes, all five execution-store table snapshots, and verified external backups. Immediately before capture, the launcher reads the current FMP daily ceiling without overrides and rejects any reviewed budget above it; a lower approved budget is passed through to the child. The required provider flag is `normal_observation_approved`; a separate additional AAPL allowance is not part of this observation. The helper records source/runtime identity plus before/after local captures, hashes the ledger before launch, and refuses to start if the paper setting, five execution-store tables, or ledger are unavailable. It launches only:

```text
scheduler.py --dry-run --now --session --observe-health
```

The child environment blanks notification identity and fixes Alpaca, FMP, and index request timeouts to 15 seconds with zero Alpaca SDK retries. The scheduler includes its Alpaca market-clock preflight inside a 256 HTTP-attempt ceiling, permits at most six iShares/Wikipedia requests, and sets the FMP process allowance to the lowest of 198 requests, the CLI bound, the configured daily ceiling, and the existing ledger's remaining allowance. FMP local-ledger deferrals are recorded as incomplete inputs; process-cap denials and provider/transport failures are separate service issues. Execution-store writes are capped at six workflow transitions and two snapshots. A 45-minute watchdog terminates the child process tree if needed.

Raw stdout/stderr files are captured from process start. Sanitized copies and the scheduler receipt are created only after child exit is confirmed. If the child remains alive after bounded termination, the helper does not read post-run database, ledger, or cache state. Its SQLite captures include row counts and deterministic content hashes for `workflow_snapshots`, `workflow_transitions`, `workflow_order_refs`, `active_positions`, and `workflow_notification_claims`; unexpected deltas outside the bounded snapshot/transition allowance fail the outer receipt. Raw files stay local because they may contain unsanitized diagnostics.

This observation exercises the existing full `large_cap` scan selection, normal exit checks, and scheduler cadence without submitting orders or starting the fill monitor. A valid result distinguishes service health, required-input coverage, and #107 overall readiness. A missed due check, stale or incomplete input, clock issue, failed read, resource denial, or escaped notification prevents a readiness pass. Even a clean offline or provider-backed observation cannot prove the installed disabled Task Scheduler action points at this checkout, establish canonical runtime selection, or make #107 overall readiness `PASS`; preserve it as one bounded acceptance artifact and compare its exact source/runtime identity with the separate #97 deployment record.

## Strategy dry-run evidence

Only after provider scope and the chosen runtime are ready, run the bounded no-order cycle under the same interpreter and checkout:

```powershell
& $python paper_trading_console.py run-now --dry-run --symbol AAPL --skip-exits
```

This explicit-symbol mode scans AAPL as the sole candidate, excludes configured extra symbols, skips exit monitoring, and remains dry-run only. It still computes AAPL's relative strength against the full S&P 500 comparison universe; the symbol limit does not narrow that market context. The bounded scan allows at most 43 actual Alpaca HTTP attempts across bulk prices and optional entry-price checks, with SDK retries disabled (zero retries). Pagination requests count individually toward the HTTP cap. It allows at most three incremental FMP logical requests, independently of the existing ledger count, plus at most three index-source requests. If an Alpaca batch fails or is empty in bounded mode, the scan aborts and discards partial universe data. A nonempty response can still omit individual symbols, so compare returned RS coverage with the requested universe and report missing symbols as gaps rather than implying complete coverage.

Set `ALPACA_HTTP_TIMEOUT_SECONDS`, `FMP_HTTP_TIMEOUT_SECONDS`, and `INDEX_TICKER_HTTP_TIMEOUT_SECONDS` to 15 for this process. These are per-request connect/read inactivity timeouts, not an end-to-end deadline. Run the process under a separate 20-minute watchdog that retains stdout, stderr, exit status, and a timeout receipt if terminated. The doctor probe has its own caps: at most three actual Alpaca HTTP attempts and four FMP logical requests. Across doctor plus this scan, the maximum FMP increment is seven requests.

The run makes provider reads and can consume allowance. It may refresh the RS-score and ticker-membership caches, increment the existing FMP request ledger by up to three, and add up to three fundamentals-cache entries. Although it submits no orders, an actionable candidate that is not already held or pending and passes sizing may cause the dry-run workflow path to append up to three transition rows and one workflow snapshot to the execution database; schema checks may also establish SQLite metadata/sidecars. Preserve and serialize access to the database before running it.

Retain an acceptance receipt with the exact source revision and dirty-tree state, runtime identity, command and timestamp, configured bounds/timeouts/watchdog outcome, and sanitized stdout/stderr. Record requested and returned universe counts/symbol coverage, RS comparison universe, freshness, missing-data and rejection reasons, and the no-order outcome. Capture before/after FMP ledger counts and checksum, cache backup/reference paths, and execution-store before/after integrity and row counts if the dry-run path touched it. The receipt must distinguish provider denial, quota exhaustion, missing data, incomplete coverage, timeout, and a valid no-buy result when the evidence allows that distinction. Never include credentials or raw account identifiers.

A no-buy outcome is valid. Preserve configured entry requirements and report unavailable statement or price data as such; do not manufacture a buy signal or loosen filters for acceptance.

Do not use `--enable-orders`, install an order-enabled task, or run a paper-order lifecycle as part of #107 readiness reporting. The separately gated baseline lifecycle belongs to #108; qualified-policy promotion belongs to #109 after its listed prerequisites.

## Issue evidence and status discipline

An acceptance package should contain separate records for identity, configuration, explicit store binding, Alpaca account/positions/orders access, broker/local reconciliation, quarterly and annual income, annual balance-sheet input, FMP prices, persistence, scheduler installation, actual service health, and the fresh strategy dry run. Include timestamps, bounded scope, outcome, and sanitized evidence references. Keep these four issue fields separate:

- **Implementation** — code and documented behavior delivered at the source revision.
- **Required inputs** — runtime configuration and authorized account/provider access available.
- **Acceptance evidence** — the actual observations retained for each acceptance criterion.
- **Dependencies** — registered start prerequisites; #107 is independent to start, with policy/runtime identity semantics aligned to #97.

Completing offline code or seeing some `PASS` rows does not automatically complete required inputs or acceptance evidence.
