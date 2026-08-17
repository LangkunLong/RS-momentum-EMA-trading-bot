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
EXECUTION_STORE_DB_PATH
NOTIFY_EMAIL_FROM
NOTIFY_EMAIL_TO
NOTIFY_EMAIL_PASSWORD
```

Keep `ALPACA_PAPER=true`. Every execution entry point rejects a false value before constructing an Alpaca trading client or fill stream; `auto_trader.py --enable-orders` enables paper-account orders only. Notification credentials are optional; when absent, email delivery is skipped without blocking execution.

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
