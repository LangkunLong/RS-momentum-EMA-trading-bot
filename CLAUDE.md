# CLAUDE.md — AI Assistant Guide

## Project Overview

CANSLIM Stock Screening Bot — an algorithmic stock screener implementing William O'Neil's CANSLIM investment strategy. Analyzes stocks from major market indices (S&P 500, Nasdaq 100, Russell 2000) and ranks them by a composite CANSLIM score (0–100).

## Repository Structure

```
├── enhanced_scanner.py            # Main entry point — CLI scanner
├── quality_stocks.py              # Ticker retrieval interface
├── backtest.py                    # Walk-forward backtesting engine
├── pyproject.toml                 # Ruff, pytest, and coverage configuration
├── .pre-commit-config.yaml        # Pre-commit hooks (ruff-format, ruff lint)
├── config/
│   └── settings.py                # All configurable parameters (thresholds, weights, cache settings)
├── core/
│   ├── canslim/
│   │   ├── __init__.py            # Package exports
│   │   ├── core.py                # CANSLIM orchestrator — evaluate_canslim()
│   │   ├── c_current_earnings.py  # C — Current quarterly earnings (25%+ YoY)
│   │   ├── a_annual_earnings.py   # A — Annual earnings growth (25%+ YoY)
│   │   ├── n_new_products.py      # N — New products / price leadership
│   │   ├── s_supply_demand.py     # S — Supply & demand (volume surges, breakouts)
│   │   ├── l_leader_laggard.py    # L — Leader vs laggard (RS ranking)
│   │   ├── i_institutional.py     # I — Institutional sponsorship
│   │   └── m_market_direction.py  # M — Market direction (SPY trend analysis)
│   ├── data_client.py             # Unified data layer — Alpaca (price) + FMP (fundamentals)
│   ├── stock_screening.py         # Screening orchestrator — screen_stocks_canslim()
│   ├── momentum_analysis.py       # Relative Strength score calculation with quarterly weights
│   └── index_ticker_fetcher.py    # Fetches & caches tickers from iShares ETF CSVs
├── tests/
│   ├── conftest.py                # Shared fixtures (mock_opportunity, tmp_csv_path)
│   ├── test_canslim_logic.py      # Pure unit tests for CANSLIM business logic
│   └── test_data_client.py        # Unit tests for data client (mocked API calls)
```

## API Keys Required

Two external APIs are used. Both keys must be present in a `.env` file (see `.env.example`):

| Variable | Service | Used For |
|----------|---------|---------|
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | [Alpaca Markets](https://alpaca.markets) | All price/OHLCV data |
| `FMP_API_KEY` | [Financial Modeling Prep](https://financialmodelingprep.com) | Earnings, balance sheets, company info |

## How to Run

```bash
# Install all dependencies (runtime + dev)
pip install -r requirements.txt

# Install pre-commit hooks (one-time setup)
python -m pre_commit install

# Main scanner
python enhanced_scanner.py

# Backtest
python backtest.py

# Ticker retrieval module (standalone)
python quality_stocks.py
```

In `enhanced_scanner.py`, configure at the top of the file:
- `SECTORS` — `'sp500'`, `'nasdaq100'`, `'russell2000'`, `'large_cap'`, `'small_cap'`, `'all'`
- `DEBUG` — verbose output
- `MIN_RS_SCORE` — override minimum RS score filter

## Architecture & Data Flow

```
Fetch tickers (iShares CSVs, cached 24h)
    → Pre-filter by RS score (percentile rank vs S&P 500 universe)
    → For each stock [parallel]: fetch OHLCV via Alpaca, fundamentals via FMP
        → Evaluate C, A, N, S, L, I, M independently
        → Combine into composite CANSLIM score (0–100)
    → Filter by MIN_CANSLIM_SCORE (default 70)
    → Sort by total_score descending and output
```

**Key patterns:**
- Each CANSLIM criterion is an isolated, pure-function module in `core/canslim/`
- `core/canslim/core.py` orchestrates all evaluators via `evaluate_canslim()`
- `MarketTrend` is a dataclass (from `m_market_direction.py`) passed into each evaluation
- `core/data_client.py` is the single source of truth for all external data — never call Alpaca or FMP directly from CANSLIM modules
- Graceful degradation: when financial data is missing, component scores fall back to neutral (0.5) rather than hard-failing
- Session cache (`_session_cache` in `data_client.py`) deduplicates API calls within a single scan run

## Backtesting & Look-Ahead Bias

The backtest engine uses `fetch_fundamental_data_as_of(symbol, as_of_date)` to retrieve only data that was *publicly available* as of a given date. This is enforced via FMP's `acceptedDate` field (SEC filing acceptance timestamp).

- **Income statements / balance sheets:** filtered on `acceptedDate <= as_of_date` — fully point-in-time.
- **Shares outstanding:** uses `_fetch_company_info_as_of()`, which filters historical enterprise-value records by date.
- **Institutional holder data:** FMP free tier only provides current snapshots — included as best-effort. Do not rely on this signal for backtest precision.

Never call `fetch_company_info()` directly from `fetch_fundamental_data_as_of()` — that function is current-only and introduces look-ahead bias.

## Configuration

All tuneable parameters live in `config/settings.py`:

| Category | Key Parameters |
|----------|---------------|
| Scanner | `MIN_MARKET_CAP` (10B), `MIN_RS_SCORE` (5), `MIN_CANSLIM_SCORE` (70) |
| Performance | `MAX_WORKERS` (3), `CHUNK_SIZE` (50) |
| Growth | `C_GROWTH_TARGET` (0.25), `A_GROWTH_TARGET` (0.25) |
| RS Weights | `RS_Q1_WEIGHT` (0.4), `RS_Q2_WEIGHT`/`Q3`/`Q4` (0.2 each) |
| Caching | `TICKER_CACHE_EXPIRY_HOURS` (24), `RS_CACHE_DIR`, `TICKER_CACHE_DIR` |

## Caching

- **Ticker cache:** `ticker_cache/index_tickers_cache.json` — 24-hour TTL, handles corruption on load
- **RS score cache:** `rs_score_cache/rs_scores_cache.csv` — daily TTL, handles corruption on load
- **Fundamentals cache:** `fundamentals_cache/*.pkl` — 24-hour TTL, pickle-based per-symbol DataFrames. Populated on the first successful FMP fetch; subsequent runs skip API calls and load from disk. Critical for staying within the FMP free-tier daily quota when scanning large universes.
- **Session cache:** in-memory LRU dict in `data_client._session_cache` — cleared between scan runs via `clear_session_cache()`

All disk cache directories are gitignored.

### FMP free-tier limits

The FMP free tier allows a limited number of API calls per day (~250–500). With `limit=5` per income-statement request, a full Nasdaq 100 scan (~100 stocks × 3 FMP calls) stays under budget on the first run; the fundamentals cache keeps all subsequent runs free. If you see widespread `missing_fundamentals` flags, your daily quota may be exhausted — the scanner degrades gracefully and the cache will rebuild on the next calendar day.

## Coding Conventions

- **Python 3.11+** with `from __future__ import annotations` at the top of every module
- **Type hints** on all public function signatures (parameters and return type)
- **Google-style docstrings** on all public functions and classes
- **Snake_case** for functions/variables, **UPPER_CASE** for constants
- **Private helpers** prefixed with `_` (`_safe_growth`, `_detect_volume_surge`)
- **Exception handling:** catch specific exceptions, not bare `Exception`; return `None` on data-fetch failure rather than raising
- **Imports:** stdlib → third-party → local, each group separated by a blank line
- **Line length:** 100 characters (enforced by ruff)

## Linting & Formatting

Ruff is the single linter/formatter. All configuration lives in `pyproject.toml`.

```bash
# Check for violations
python -m ruff check .

# Auto-fix safe violations
python -m ruff check --fix .

# Format (enforced automatically by pre-commit)
python -m ruff format .
```

**Selected rule sets:** `F` (pyflakes), `E`/`W` (pycodestyle), `B` (bugbear), `I` (isort), `D` (pydocstyle), `ANN` (annotations).

The pre-commit hook runs `ruff format` then `ruff check --fix` on every `git commit`. If a commit is blocked by a lint error, fix it — never bypass with `--no-verify`.

## Testing

pytest with pytest-cov. All configuration lives in `pyproject.toml`.

```bash
# Run unit tests (default — no network calls)
python -m pytest

# Run unit tests with verbose output
python -m pytest -v

# Run including integration tests (requires valid API keys in .env)
python -m pytest -m integration

# Coverage report (HTML)
python -m pytest --cov=. --cov-report=html
```

**Test layout:**

| File | What it covers |
|------|---------------|
| `tests/test_canslim_logic.py` | Pure business logic — `_safe_growth`, index alias routing, RS import source |
| `tests/test_data_client.py` | Data client — CSV export, `validate_ticker` (mocked), column schema |

**Rules for writing tests in this project:**

1. **No real API calls in unit tests.** Mock `core.data_client.fetch_ohlcv` and `core.data_client._fmp_get` at their definition site. Use `unittest.mock.patch("core.data_client.fetch_ohlcv", ...)`.
2. **Mark integration tests explicitly.** Any test that makes real HTTP calls (Alpaca, FMP, iShares) must be decorated `@pytest.mark.integration`. These are excluded from the default `pytest` run.
3. **Use `tmp_path` for file I/O.** Never write to project directories in tests; pytest's `tmp_path` fixture handles cleanup automatically.
4. **Test new CANSLIM criterion modifiers.** Every new scoring function added to `core/canslim/` must have at least:
   - One test for the **happy path** (expected score range for a valid input).
   - One test for the **zero/None guard** (function returns a safe default when data is missing).
   - One test for the **boundary condition** (e.g., threshold edge: value just at and just below the cutoff).
5. **Keep unit tests pure.** Tests in `test_canslim_logic.py` and `test_data_client.py` must never import `fetch_ohlcv`, `_fmp_get`, or any function that triggers a network call without mocking it first.
6. **Do not mock `config/settings.py`.** Tests rely on the real settings values so that threshold changes are automatically validated.

## Git Workflow

- Branch naming: `claude/[feature-description]-[id]`
- PRs are used for merging features
- Commit messages are descriptive, plain-English summaries
- Pre-commit hook enforces ruff on every commit — fix violations rather than bypassing
