# CANSLIM Trading Bot

A CANSLIM stock scanner, historical portfolio backtester, and Alpaca paper-trading workflow. Market prices and broker state come from Alpaca; fundamentals and company profiles come from Financial Modeling Prep (FMP).

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
- An FMP API key whose plan includes the income-statement and balance-sheet endpoints used by the configured CANSLIM fundamental gate; institutional scoring additionally requires the stable Positions Summary endpoint
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

Create a local `.env` file or set equivalent environment variables. `.env*` is ignored by Git.

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
EXECUTION_STORE_DB_PATH
NOTIFY_EMAIL_FROM
NOTIFY_EMAIL_TO
NOTIFY_EMAIL_PASSWORD
```

Keep `ALPACA_PAPER=true`. Every execution entry point rejects a false value before constructing an Alpaca trading client or fill stream; `auto_trader.py --enable-orders` enables paper-account orders only. Notification credentials are optional; when absent, email delivery is skipped without blocking execution.

Never commit credentials, generated scan results, execution databases, `.claude/settings.local.json`, or recovery artifacts. A previously exposed FMP key must be rotated at FMP even if the generated CSV is deleted locally; deletion does not revoke a key or remove it from Git history.

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

Alpaca is the execution source of truth. SQLite is the audit and recovery view. Workflow resolution uses explicit workflow id, client order id, broker order id, active symbol ownership, then latest symbol history.

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

Run a historical backtest and export equity/holdings artifacts:

```powershell
python backtest_pnl.py --tickers AAPL MSFT NVDA --start-date 2023-04-01 --end-date 2026-04-01 --export-equity --export-holdings
```

Generated outputs are ignored under `scan_results/`, `backtest_results*`, and `.artifacts/`.

## Paper-mode dry runs and diagnostics

Validate configuration and broker connectivity without submitting an order:

```powershell
python paper_trading_console.py doctor
python paper_trading_console.py checklist
python paper_trading_console.py run-now --dry-run
python auto_trader.py --dry-run
```

The dry run still performs provider reads and may take time. It prints intended entries/exits but does not submit them.

Treat FMP `402` responses for income statements or balance sheets as a deployment blocker for strategy entries when `REQUIRE_FUNDAMENTALS_FOR_BUYS=true`. The scan still completes and reports technical/watchlist results, but candidates with unavailable fundamentals intentionally cannot pass the buy gate. Upgrade the FMP plan or select and validate a replacement fundamental-data provider; do not weaken the gate merely to make orders appear.

Run the scheduler in dry-run mode:

```powershell
python scheduler.py --dry-run --now
```

The scheduler continues running after the immediate cycle; press Ctrl-C to stop it.

## Supervised paper validation

Do not run `verify_paper_trading.py`, `paper_trading_console.py run-now` without `--dry-run`, or task installation as an unattended first step.

Before the one-share paper lifecycle:

1. Confirm `ALPACA_PAPER=true` and verify the paper account endpoint.
2. Pass all offline quality gates and `paper_trading_console.py doctor`.
3. Display the exact symbol, quantity, order type, and cleanup behavior.
4. Obtain explicit operator approval.
5. Observe the buy fill, protective stop derived from the actual fill, durable transitions, restart recovery, and cleanup sell/cancel.
6. Confirm no orphan position or order remains in Alpaca and no active-position record remains locally.

Windows scheduled-task installation has a separate approval gate. Invoke setup through the stable project interpreter (`.venv\Scripts\python.exe`) and inspect the interpreter, repository path, arguments, trigger, working directory, user identity, and log destination before running `paper_trading_console.py install-task` or `setup_windows_task.py`. The registered action changes into the repository and writes to `.artifacts/logs/scheduler.log`. The default installed task includes `--dry-run`; enabling paper order submission requires the separate `--enable-orders` flag and a second explicit approval after the dry-run task is observed.

## Recovery and troubleshooting

- On startup, the scheduler reconciles protective stops for broker positions.
- Replayed final fills are idempotent; partial fills can still advance to a larger cumulative final quantity.
- Sell notifications recover entry cost basis from the workflow or active-position store. If neither exists, P&L is reported as unavailable instead of zero.
- FMP 402/403/404/429 and retry failures degrade without exposing the API key. `clear_session_cache()` resets the per-scan circuit breaker. Persistent `402` responses indicate a plan-entitlement mismatch, not a transient retry condition. Institutional ownership uses the current period-specific `institutional-ownership/symbol-positions-summary` endpoint; older `symbol-ownership` and `institutional-holder` routes are legacy APIs.
- If broker and SQLite state disagree, preserve broker/order ids, stop automated submission, and reconcile against Alpaca before retrying.

The stabilization design and execution plans are under `docs/superpowers/`.
