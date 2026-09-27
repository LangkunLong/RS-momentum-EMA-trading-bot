# Paper runtime identity and readiness runbook

This runbook prepares evidence for Trading-11 (#107). It does not enable trading, promote a strategy, or establish service health by itself. Keep configuration, provider access, persistence, scheduler installation, actual service health, and strategy dry-run evidence as separate findings.

## Identity record

Record two distinct fields for each readiness evidence package:

- `source_revision`: the exact Git revision of the code being observed. Also record whether the checkout has uncommitted changes; a commit ID alone does not identify a dirty source tree.
- `runtime_identity`: the execution environment: checkout path, interpreter path and version, hashes for `requirements-lock.txt` and `pyproject.toml`, relevant installed versions, sanitized configuration profile, persistent-store path, and paper environment. A missing dependency file is recorded as `unavailable`. After a successful Alpaca read, include only a one-way account-ID fingerprint. Never include API keys, secrets, raw account IDs, or credential-bearing URLs.

The console prints these names and facts in its `Runtime identity` row. It marks canonical deployment selection unverified until an operator selects the checkout and identity record. This runtime identity is separate from the policy artifact digest, policy interface, feature contract/calculator, data format, execution profile, and cost assumptions. Keep those strategy identities under the shared #80/#97 contracts.

Current application configuration uses Alpaca for market prices and FMP for financial statements. The FMP price endpoint probe below answers the separate issue-level entitlement question; a pass does not mean the application consumes FMP prices.

## Readiness claims

| Claim | Evidence and boundary |
| --- | --- |
| Configuration | Paper mode and required setting presence. Presence proves configuration only. The report never prints credential values. |
| Alpaca connectivity | One account read, only after explicitly requesting provider probes and only when paper mode is selected. A separate hashed fingerprint binds the observed account without printing its raw identifier. |
| FMP statement entitlement | One representative quarterly income-statement request. A returned matching symbol/date/revenue record supports this endpoint probe only. |
| FMP price entitlement | One representative historical EOD request over the preceding ten calendar days. A returned matching symbol/date/close record supports this endpoint probe only. |
| Persistence | Read-only SQLite integrity and expected-schema inspection of an existing execution store. A missing store remains unverified; readiness code does not create, initialize, migrate, or write it. |
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

`doctor` does not contact Alpaca or FMP by default. It observes the configured checkout/interpreter, checks configuration presence, queries scheduler installation read-only, and inspects an existing store without mutation. An incomplete result is expected while required evidence is outstanding. Do not interpret a configured path or a present credential setting as proof of connectivity or entitlement.

Keep the historical `paper-trading-runtime` checkout and its database immutable during inventory. Do not copy its database into the selected checkout or let a readiness command initialize that historical path. If a new acceptance store is needed for a separate write test, create and identify it as an isolated store under an explicitly selected scratch path; that does not prove the existing production store can accept writes.

## Explicit provider probes

Only after the paper environment and finite request scope are authorized, run:

```powershell
& $python paper_trading_console.py doctor --probe-external
```

This performs one logical Alpaca account operation and two logical FMP endpoint operations (one `income-statement`, one `historical-price-eod/full`, each for representative symbol AAPL). The provider clients may apply their configured transport retry policy. FMP calls go through the configured data-client request accounting and may consume daily request allowance. The check does not submit orders. If FMP returns no usable sample record, the entitlement remains `UNVERIFIED`; the current data adapter collapses empty data, entitlement denial, quota exhaustion, and some transport failures, so an empty response must not be described as a confirmed entitlement failure or success.

The Alpaca probe is refused when paper mode is false. Never adapt this option to live-account credentials. Keep provider results distinct and timestamp them; an FMP statement pass says nothing about FMP prices, and neither says anything about Alpaca market-data feed coverage for the strategy universe.

## Scheduler and service observation

`doctor`'s scheduler row answers only whether the named task is registered. Inspect the exact installed task action and its executable, arguments, working directory, order mode, trigger, and log destination. Compare them with the selected checkout and interpreter; do not modify or reinstall a task as part of an inventory-only check.

For service health, record an observation from the intended scheduler session: process/session identity, recent log timestamp and outcome, and any required fill-stream and startup stop-reconciliation readiness. A dry-run scheduler does not have the same broker-stream requirements as an order-enabled session. A registered task that has not run, a stale log, a failed last result, or a missing required readiness marker is not healthy-service evidence.

## Strategy dry-run evidence

Only after provider scope and the chosen runtime are ready, run the no-order cycle under the same interpreter and checkout:

```powershell
& $python paper_trading_console.py run-now --dry-run
```

This run fetches current data and can consume provider allowance. Retain its output with the `source_revision` and `runtime_identity`, timestamp, symbol/universe scope, data freshness/coverage, and intelligible candidate decisions/rejection reasons. It must not submit orders. A no-buy outcome is valid. Preserve configured entry requirements and report unavailable statement or price data as such; do not manufacture a buy signal or loosen filters for acceptance.

Do not use `--enable-orders`, install an order-enabled task, or run a paper-order lifecycle as part of #107 readiness reporting. The separately gated baseline lifecycle belongs to #108; qualified-policy promotion belongs to #109 after its listed prerequisites.

## Issue evidence and status discipline

An acceptance package should contain separate records for identity, configuration, Alpaca account access, FMP statements, FMP prices, persistence, scheduler installation, actual service health, and the fresh strategy dry run. Include timestamps, bounded scope, outcome, and sanitized evidence references. Keep these four issue fields separate:

- **Implementation** — code and documented behavior delivered at the source revision.
- **Required inputs** — runtime configuration and authorized account/provider access available.
- **Acceptance evidence** — the actual observations retained for each acceptance criterion.
- **Dependencies** — registered start prerequisites; #107 is independent to start, with policy/runtime identity semantics aligned to #97.

Completing offline code or seeing some `PASS` rows does not automatically complete required inputs or acceptance evidence.
