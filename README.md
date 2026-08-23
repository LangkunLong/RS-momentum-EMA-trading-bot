# CANSLIM Trading Bot

A CANSLIM stock scanner, historical portfolio backtester, and Alpaca paper-trading workflow. Market prices and broker state come from Alpaca; financial statements come from Financial Modeling Prep (FMP). Company profiles and institutional data are intentionally skipped in the default free-plan mode.

The project is being stabilized for supervised paper trading. Live-account trading is out of scope.

## Strategy and risk defaults

- New entries require the configured CANSLIM, relative-strength, breakout, and market-regime gates.
- Portfolio risk is 1% of equity per trade.
- The hard stop is 8% below the actual fill price.
- Maximum position weight is 12.5%, derived from 1% risk divided by an 8% stop.
- Maximum simultaneous positions is five.
- The backtester supports staged profit-taking, an eight-week hold rule, time/stagnation exits, break-even stops, and bounded relative-strength eviction.

The active values are in `config/settings.py`. Treat older design plans as historical unless the 2026-08-16 stabilization spec explicitly adopts them.

## Requirements

- Python 3.11 or newer
- An Alpaca account and API credentials
- An FMP API key with access to the stable income-statement and balance-sheet endpoints used by the configured CANSLIM fundamental gate; institutional scoring is optional and disabled in the default free-plan mode
- Windows only for the optional Task Scheduler integration; scanning, backtesting, tests, and manual paper operation are ordinary Python workflows

Create an isolated environment and install the verified dependency set:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-lock.txt
```

`requirements.txt` contains supported dependency ranges. `requirements-lock.txt` records the exact set used for verification.

## Configuration

Copy `.env.example` to a local `.env` file or set equivalent environment variables. Real `.env*` files are ignored by Git; the credential-free example is tracked.

Required:

```text
ALPACA_API_KEY
ALPACA_SECRET_KEY
ALPACA_PAPER=true
FMP_API_KEY
```

Optional:

```text
ALPACA_STOCK_FEED=iex
FMP_PLAN=free
FMP_DAILY_REQUEST_BUDGET=198
OPENROUTER_API_KEY (or OPENROUTER; required only for agent_loop.py)
EXECUTION_STORE_DB_PATH
NOTIFY_EMAIL_FROM
NOTIFY_EMAIL_TO
NOTIFY_EMAIL_PROVIDER=gmail_oauth
```

Keep `ALPACA_PAPER=true`. Every execution entry point rejects a false value before constructing an Alpaca trading client or fill stream; `auto_trader.py --enable-orders` enables paper-account orders only. Notification credentials are optional; when absent, email delivery is skipped without blocking execution.

### Gmail notifications without a password

Gmail notifications use Google's installed-app OAuth flow and the narrow `gmail.send`
permission. The reusable authorization is stored in Windows Credential Manager through
`keyring`; neither a Gmail password nor an OAuth refresh token belongs in `.env`.

1. In Google Cloud, enable the Gmail API, configure the OAuth consent screen, and create an
   OAuth client of type **Desktop app**.
2. Download its client JSON to an ignored local path such as
   `.artifacts/secrets/gmail-oauth-client.json`. Do not commit or paste this file into chat.
3. Set only the provider and addresses in `.env`:

   ```text
   NOTIFY_EMAIL_PROVIDER=gmail_oauth
   NOTIFY_EMAIL_FROM=langkunlong@gmail.com
   NOTIFY_EMAIL_TO=langkunlong@gmail.com
   ```

4. Authorize the exact account in the browser and send a test message:

   ```powershell
   python paper_trading_console.py email-auth --client-secrets .artifacts/secrets/gmail-oauth-client.json
   python paper_trading_console.py email-test
   ```

Google shows the requested account and permission in the browser; select
`langkunlong@gmail.com` and confirm. Revoke both the Google grant and the local vault entry with
`python paper_trading_console.py email-revoke`. If the OAuth consent screen remains in Google's
**Testing** publishing status, refresh tokens for external users generally expire after seven
days, so publish the app for durable unattended notifications or reauthorize when prompted.

`NOTIFY_EMAIL_PASSWORD` remains supported only for the legacy SMTP backend. OAuth delivery never
falls back to SMTP after an authorization or Gmail API failure.

Never commit credentials, generated scan results, execution databases, `.claude/settings.local.json`, or recovery artifacts. A previously exposed FMP key must be rotated at FMP even if the generated CSV is deleted locally; deletion does not revoke a key or remove it from Git history.

### FMP free-plan controls

`FMP_PLAN=free` is the default. [FMP documents 250 requests per day on that tier](https://site.financialmodelingprep.com/how-to/how-to-create-a-financial-modeling-prep-account); the bot persists a hard ceiling of 198 requests per 3 p.m. Eastern reset window, preserving 52 requests for manual or administrative use. Lower values are allowed through `FMP_DAILY_REQUEST_BUDGET`; higher values are capped at 198.

Free mode enforces `limit=5`, disables automatic HTTP retries, caches successful statements for seven days, and makes only three requests for each uncached live candidate: quarterly income, annual income, and balance sheet. Candidates are submitted in descending RS order. Once the local budget is exhausted, affected candidates are labeled `quota_deferred` and excluded from both actionable buys and watchlists rather than being treated as ordinary missing data. The persisted counter is `.artifacts/cache/fmp_request_usage.json`; deleting or editing it can invalidate the safety guarantee.

Profile and institutional-ownership calls are skipped in free mode. Unknown shares/float score neutrally, and the unavailable institutional weight is redistributed. `REQUIRE_FUNDAMENTALS_FOR_BUYS=true` remains in force, so missing statement evidence never becomes a free pass into an order.

## System layout

- `enhanced_scanner.py` evaluates the configured stock universe and exports ignored scan artifacts.
- `core/backtest_engine.py` is the canonical historical simulator; `backtest_pnl.py` is its compatibility CLI.
- `auto_trader.py` runs exit monitoring, the scanner, and entry submission.
- `core/order_manager.py` owns order and fill orchestration.
- `core/execution_workflow.py` owns lifecycle transitions.
- `core/execution_store.py` persists workflow snapshots, append-only transitions, broker references, and active-position cost basis in SQLite.
- `fill_monitor.py` routes Alpaca trade-update events into the order manager.
- `scheduler.py` runs the 09:31 ET daily cycle and hourly exit checks.
- `paper_trading_console.py` provides deployment diagnostics and supervised operations.
- `agent_loop.py` runs the separate Docker-confined, proposal-only OpenRouter backtest controller.

Alpaca is the execution source of truth. SQLite is the audit and recovery view. With no explicit reference, workflow resolution uses active symbol ownership and then latest symbol history. When a workflow id, client order id, or broker order id is supplied but unknown, resolution fails closed instead of falling back to another workflow.

## Offline quality gates

These commands do not submit broker orders:

```powershell
python -m ruff check . --no-cache --exclude .artifacts
python -m pytest -q --no-cov -m "not integration"
python backtest_pnl.py --help
python auto_trader.py --help
python paper_trading_console.py --help
python scheduler.py --help
```

CI runs Ruff, compilation, and the non-integration test suite on Python 3.11 and 3.13. Tests use synthetic or mocked provider data unless explicitly marked `integration`.

### Isolated multi-agent backtest proposals

`agent_loop.py` is not part of paper trading or the scheduler. It runs one sealed technical-only
backtest in an attested, network-disabled Docker worker, then asks a fixed three-role model set for
an inert proposal: Qwen3 Next 80B A3B orchestrator, DeepSeek R1 reasoner, and Qwen3 Coder Next coder. The coder returns
typed exact-line replacements; the controller verifies immutable source coordinates and old text,
rejects invalid Python and newly introduced defaulted parameters, renders the unified diff itself,
and never writes it to this checkout. Broker, FMP, mail, Git, and OpenRouter credentials are absent
from worker containers.

The first paid rollout step is always one explicitly authorized sample. Use fresh empty controller
and audit roots, an operator-approved historical-data bundle and SHA-256, a digest-pinned sandbox
image, and operator-selected failing thresholds that are meant to produce diagnostic evidence.
Omit `--apply`:

```powershell
$controllerRoot = Join-Path $env:TEMP 'agent-loop-canary-controller'
$auditRoot = Join-Path $env:TEMP 'agent-loop-canary-audit'
New-Item -ItemType Directory -Force $controllerRoot, $auditRoot | Out-Null

& .\.venv\Scripts\python.exe -u .\agent_loop.py `
  --repo-root (Resolve-Path .).Path `
  --permanent-runtime-root 'C:\Projects\trading_bot\paper-trading-runtime' `
  --git-executable (Get-Command git).Source `
  --controller-temp-parent $controllerRoot `
  --artifact-root $auditRoot `
  --docker-executable (Get-Command docker).Source `
  --sandbox-image '<repository>@sha256:<64-lowercase-hex>' `
  --gate backtest `
  --tickers '<approved-symbol>' `
  --benchmark '<approved-benchmark>' `
  --start-date '<YYYY-MM-DD>' `
  --end-date '<YYYY-MM-DD>' `
  --holdout-start-date '<trailing-holdout-YYYY-MM-DD>' `
  --holdout-end-date '<same-as-end-date>' `
  --historical-data-bundle '<absolute-approved-sqlite-path>' `
  --historical-data-sha256 '<64-lowercase-hex>' `
  --minimum-total-return '<operator-threshold>' `
  --minimum-annualized-return '<operator-threshold>' `
  --minimum-sharpe-ratio '<operator-threshold>' `
  --maximum-drawdown-magnitude '<operator-threshold>' `
  --minimum-closed-trades '<operator-threshold>' `
  --proposal-samples 1 `
  --canary-max-usd 0.50 `
  --max-usd 0.50 `
  --max-iterations 1 `
  --max-api-calls 3 `
  --max-tokens 200000 `
  --wall-timeout-seconds 1800
```

A useful canary ends with `batch_complete`, exit code `10`, exactly three authoritatively
accounted calls, `completed_samples=1`, `source_modified=false`, and `cleanup_complete=true` in
`AGENT_LOOP_SUMMARY`. Exit `0` means the sealed gate already passed and no proposal was needed;
exit `22` is a fail-closed controller/batch failure. Proposal metadata deliberately remains
`security_attestation=false`; a completed proposal is marked
`verification_status=privately_backtested` only after the controller applies it to a fresh
disposable candidate, runs pytest, Ruff, compileall, `git diff --check`, and the sealed backtest,
then removes that candidate. This is evidence for review, not merge or trading authorization.

Proposal quality is evaluated against the sealed baseline, not against thresholds alone. A candidate
must pass the primary window, be non-worse on return, annualized return, Sharpe, drawdown headroom,
and closed trades, and improve at least one of those measures. When a trailing holdout window is
configured (required for proposal-batch CLI runs), the same comparison must also pass out of sample.
Accepted diffs are deduplicated and returned in deterministic quality order; rejected duplicates remain
audit-classified and are never exported as separate experiments. The orchestrator uses Qwen3 Next 80B
A3B Instruct, the reasoner uses DeepSeek R1, and the coder uses Qwen3 Coder Next.
These role slugs are fixed controller constants; alternate model selection and fallback routing are disabled.

Do not turn a canary into a 50-attempt run automatically. First inspect the payload, rendered diff,
private-evaluation artifact, audit chain, source immutability, exact cited source/configuration
facts, and whether the edit changes an existing executed strategy path.
Only a separately authorized same-commit run may use `--proposal-samples 50`,
`--max-api-calls 150`, `--canary-max-usd 0.50`, `--max-usd 2.00`,
`--max-tokens 2000000`, `--max-iterations 1`, no `--apply`, and a wall timeout large enough for
the observed model latency. The 150-call value is a hard ceiling/reservation; actual authoritative
calls may be lower because the batch fails closed on the first orchestrator abort, reasoner skip,
scope/policy rejection, duplicate, provider failure, or accounting failure. `completed_samples`
counts exported proposals; `rejected_samples` records any classified sample before the terminal
failure. A `batch_complete` result means every requested sample completed with an accepted,
privately evaluated, unique proposal; any rejection stops further model calls.

Outside proposal-batch mode, `--apply` still means apply only inside the disposable controller
candidate so another quarantined iteration can observe the change. It never applies to this
checkout. Do not use `--apply` in a canary or proposal batch; real-source application and
backtesting of an exported diff remain separate manual workflows.

The full design and implementation record is in
[`docs/superpowers/specs/2026-08-17-multi-agent-backtest-loop-design.md`](docs/superpowers/specs/2026-08-17-multi-agent-backtest-loop-design.md)
and
[`docs/superpowers/plans/2026-08-17-multi-agent-backtest-loop.md`](docs/superpowers/plans/2026-08-17-multi-agent-backtest-loop.md).

## Scanner and backtester

Run a read-only scan against configured providers:

```powershell
python enhanced_scanner.py
```

### After-close candidate preparation

Create a completed-bar, technical-only advisory snapshot for the next session:

```powershell
$env:FMP_DAILY_REQUEST_BUDGET='0'
.\.venv\Scripts\python.exe -u .\prepare_after_close.py
```

This command downloads completed daily prices once, writes full CSV and JSON artifacts under `scan_results/after_close`, never submits orders, and never consumes FMP quota. Its shortlist is advisory only: execution-time price and safety revalidation remains mandatory before any paper order.

On the free FMP plan, use technical-only mode for broad-universe historical backtests; this path makes zero FMP calls:

```powershell
python backtest_pnl.py --universe sp500 --start-date 2023-04-01 --end-date 2026-04-01 --technical-only --export-equity --export-holdings
```

For a true no-survivorship-bias replay, use a hashed point-in-time SQLite
bundle. The bundle must contain dated membership transitions, split-adjusted
OHLCV bars, and fundamentals with a public/accepted date; the normal
`historical_data.sqlite3` provider cache is intentionally rejected for this
mode because it contains only cached provider responses.

The fundamentals header is:
`ticker,statement_type,period_end,public_date,basic_eps,diluted_eps,total_revenue,net_income,common_stock,total_stockholders_equity,shares_outstanding,held_percent_institutions,institution_count,prev_institution_count`.

If the membership source is a sparse snapshot export (`as_of,ticker,...`),
convert it before building the bundle:

```powershell
python convert_membership_snapshots.py `
  --snapshots-csv exports/sp500_snapshots.csv `
  --output exports/sp500_membership.csv
```

Build that bundle offline from reviewed exports. The builder requires exact
CSV headers (`effective_date,ticker,member` for membership;
`trade_date,ticker,open,high,low,close,volume` for prices; and the documented
fundamental columns) and refuses to overwrite an existing output:

```powershell
python build_pit_bundle.py `
  --membership-csv exports/sp500_membership.csv `
  --prices-csv exports/daily_prices.csv `
  --fundamentals-csv exports/fundamentals.csv `
  --data-cutoff 2026-04-01 `
  --output .artifacts/cache/backtest/canslim_pit.sqlite3 `
  --manifest-output .artifacts/cache/backtest/canslim_pit.manifest.json
```

Verify the bundle/manifest pair before running either simulator:

```powershell
python verify_pit_bundle.py `
  --bundle .artifacts/cache/backtest/canslim_pit.sqlite3 `
  --sha256 <bundle-sha256> `
  --manifest .artifacts/cache/backtest/canslim_pit.manifest.json
```

```powershell
python backtest_pnl.py `
  --pit-bundle .artifacts/cache/backtest/canslim_pit.sqlite3 `
  --pit-bundle-sha256 <lowercase-sha256> `
  --start-date 2023-04-01 --end-date 2026-04-01 `
  --no-csv
```

To benchmark a separate equal-weight basket of the top point-in-time leaders,
rebalance at the following day's open with:

```powershell
python leader_basket.py `
  --pit-bundle .artifacts/cache/backtest/canslim_pit.sqlite3 `
  --bundle-sha256 <lowercase-sha256> `
  --start-date 2023-04-01 --end-date 2026-04-01 `
  --leader-count 50 --rebalance-days 20
```

The basket ranks only members known on each rebalance date and reports its
return, benchmark return, drawdown, Sharpe, cash utilization, and rebalance
transactions independently from tactical CANSLIM entries.

### Five-year public PIT baseline

The completed, immutable 2021--2025 public point-in-time (PIT) baseline is a
logic-verification comparison point. Its bundle SHA-256 is
`8ca8242dd67db30d456a2b1861f7e7399f8ca418079738ab150d4e44865763c5` and its
completed run directory is:

```text
.artifacts/pit-baseline/run-20260823e/run-20260823T071322Z-8ca8242dd67d
```

Do not delete, overwrite, or treat that run as a fully coverage-gated strategy
performance verdict: it was intentionally published with
`--allow-incomplete-fundamentals`. The evaluated quarterly-plus-annual
fundamental coverage is 80.20768935%, below the 90% gate. The operator
workflow, exact provenance, findings, and the next-milestone decision record
are in [docs/pit-baseline-data-provenance.md](docs/pit-baseline-data-provenance.md).

Backtest execution treats the market-direction score as diagnostics by default:
an otherwise-valid signal is sized and entered whenever cash is available. Use
`--require-bullish-market` only for an explicit conservative M-gated replay.

To select a cash-deployment threshold without fitting the full period, run the
walk-forward optimizer against the approved local cache:

```powershell
python cash_utilization_optimizer.py `
  --historical-data-bundle .artifacts/cache/backtest/historical_data.sqlite3 `
  --start-date 2023-04-01 --holdout-start-date 2025-04-01 --end-date 2026-04-01 `
  --thresholds none,0.75,0.60,0.50 --target-cash-pct 60 `
  --min-holdout-sharpe-delta 0 --min-holdout-return-delta 0 `
  --max-holdout-drawdown-degradation-pct 0
```

The selector chooses on the training window, reports a trailing holdout
comparison against the bullish-only baseline, and emits a `promote` or
`hold_baseline` decision. Promotion requires lower holdout cash plus nonnegative
holdout return/Sharpe deltas and no drawdown deterioration by default. It does
not change the backtest execution default. Backtests execute otherwise-valid
entries whenever cash is available; pass `--require-bullish-market` to opt into
the M-gate for a conservative comparison. A threshold is an explicit backtest
experiment; it is not a live or paper-trading setting.

Backtest reports also separate final buy signals from technically valid signals
that were blocked by the M (market-direction) gate. This prevents idle cash from
being misdiagnosed as an order-entry or position-capacity failure.

On the approved 2023--2026 cache, a 75% cash trigger passed a trailing holdout
promotion check when allowing up to 3 percentage points of additional maximum
drawdown (`avg_cash_pct` 58.4% vs 76.9%, Sharpe 1.36 vs 1.26). Treat that as an
explicit research profile, not a new default: run it with
`--cash-deployment-threshold 0.75` and review the resulting risk budget before
using it outside backtesting.

Rolling one-year replays show the same cash reduction in all three windows
(roughly 12.6--18.5 percentage points), while return and Sharpe are mixed and
maximum drawdown is 1.6--2.7 points worse. This is therefore a utilization
profile with an explicit risk tradeoff, not a universally superior strategy.

Full fundamental backtests can use three FMP requests per uncached ticker and free-tier history is limited to five records. Run them only on small explicit ticker lists after checking the persisted daily budget.

Generated outputs are ignored under `scan_results/`, `backtest_results*`, and `.artifacts/`.

## Paper-mode dry runs and diagnostics

Validate configuration and broker connectivity without submitting an order:

```powershell
python paper_trading_console.py doctor
python paper_trading_console.py checklist
python paper_trading_console.py run-now
python auto_trader.py --dry-run
```

The dry run still performs provider reads and may take time. It prints intended entries/exits but does not submit them.

Treat FMP `402` responses for income statements or balance sheets as a deployment blocker for strategy entries when `REQUIRE_FUNDAMENTALS_FOR_BUYS=true`. The scan still completes and reports technical/watchlist results, but candidates with unavailable fundamentals intentionally cannot pass the buy gate. Upgrade the FMP plan or select and validate a replacement fundamental-data provider; do not weaken the gate merely to make orders appear.

Run the scheduler in dry-run mode:

```powershell
python scheduler.py --now
```

Both commands default to dry run. `paper_trading_console.py run-now` is
intentionally dry-run only and refuses `--enable-orders`. Use the canonical
scheduler path for a supervised order-enabled cycle:

```powershell
python scheduler.py --enable-orders --now
```

The scheduler continues running after the immediate cycle; press Ctrl-C to
stop it, or add `--session` to exit after 16:05 ET.

## Supervised paper validation

Do not run `verify_paper_trading.py --execute`, pass `--enable-orders` to an
operator command, or install an order-enabled task as an unattended first step.

Before the one-share paper lifecycle:

1. Confirm `ALPACA_PAPER=true` and verify the paper account endpoint.
2. Pass all offline quality gates and `paper_trading_console.py doctor`.
3. Display the exact symbol, quantity, order type, and cleanup behavior.
4. Obtain explicit operator approval.
5. Observe the buy fill, protective stop derived from the actual fill, durable transitions, restart recovery, and cleanup sell/cancel.
6. Confirm no orphan position or order remains in Alpaca and no active-position record remains locally.

Run the separately gated verifier only after those checks and explicit operator approval:

```powershell
python verify_paper_trading.py --execute
```

`--execute` authorizes one SPY paper buy/fill/protective-stop/cleanup lifecycle. Omitting it refuses before broker access.

Windows scheduled-task installation has a separate approval gate. Invoke setup
through the stable project interpreter (`.venv\Scripts\python.exe`) and inspect
the interpreter path, repository path, mode, trigger, user identity, and log
destination before running `paper_trading_console.py install-task` or
`setup_windows_task.py`. The XML task action runs the project virtual
environment directly, sets the repository as its working directory, and writes
through the scheduler's unbuffered task log. The default task runs
`--dry-run --session --fmp-daily-budget 0`; enabling paper orders requires the
separate `--enable-orders` flag after that task is observed. The order-enabled
task uses `--fmp-daily-budget 20`, a conservative per-process allowance for the
FMP free plan; it does not raise the persisted daily hard ceiling.
To stop unattended paper trading, run `python setup_windows_task.py --remove`
from this checkout.
Only one scheduler process can run on the host, and live order paths remain
disabled until the fill stream is connected and startup stop reconciliation
succeeds.

## Recovery and troubleshooting

- On startup, the scheduler reconciles protective stops for broker positions.
- Replayed final fills are idempotent; partial fills can still advance to a larger cumulative final quantity.
- Sell notifications recover entry cost basis from the workflow or active-position store. If neither exists, P&L is reported as unavailable instead of zero.
- FMP 402/403/404/429 failures degrade without exposing the API key. `clear_session_cache()` resets only the per-scan circuit breaker; it does not reset persisted daily usage. Free mode performs no automatic retries. Persistent `402` responses indicate a plan-entitlement mismatch, not a transient retry condition. Paid mode can use the current period-specific `institutional-ownership/symbol-positions-summary` endpoint; older `symbol-ownership` and `institutional-holder` routes are legacy APIs.
- If broker and SQLite state disagree, preserve broker/order ids, stop automated submission, and reconcile against Alpaca before retrying.

The stabilization design and execution plans are under `docs/superpowers/`.
