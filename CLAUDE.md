# CLAUDE.md — AI Assistant Guide

## Project Overview

CANSLIM Stock Screening Bot — an algorithmic stock screener implementing William O'Neil's CANSLIM investment strategy. Analyzes stocks from major market indices (S&P 500, Nasdaq 100, Russell 2000) and ranks them by a composite CANSLIM score (0–100).

## Repository Structure

```
├── enhanced_scanner.py            # Main entry point — CLI scanner
├── quality_stocks.py              # Ticker retrieval interface
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
│   ├── stock_screening.py         # Screening orchestrator — screen_stocks_canslim()
│   ├── momentum_analysis.py       # Relative Strength score calculation with quarterly weights
│   ├── index_ticker_fetcher.py    # Fetches & caches tickers from iShares ETF CSVs
│   └── yahoo_finance_helper.py    # yfinance DataFrame normalization utilities
```

## How to Run

```bash
# Main scanner (entry point)
python3 enhanced_scanner.py

# Test ticker retrieval
python3 quality_stocks.py

# Test ticker fetcher directly
python3 core/index_ticker_fetcher.py
```

In `enhanced_scanner.py`, you can configure before running:
- `SECTORS` — `'sp500'`, `'nasdaq100'`, `'russell2000'`, `'large_cap'`, `'small_cap'`, `'all'`
- `DEBUG` — verbose output
- `MIN_RS_SCORE` — override minimum RS score filter

## Dependencies

```
yfinance
pandas
numpy
requests
```

No API keys are required (yfinance is unauthenticated). Install with:
```bash
pip install yfinance pandas numpy requests
```

## Architecture & Data Flow

```
Fetch tickers (iShares CSVs, cached 24h)
    → Validate via Yahoo Finance
    → Calculate RS scores (bulk, cached daily)
    → For each stock: evaluate C, A, N, S, L, I, M
    → Combine into composite CANSLIM score (0–100)
    → Filter by MIN_CANSLIM_SCORE (default 70)
    → Sort and output results
```

**Key patterns:**
- Each CANSLIM criterion is an isolated module in `core/canslim/`
- `core.py` orchestrates all evaluators via `evaluate_canslim()`
- `MarketTrend` is a dataclass (from `m_market_direction.py`)
- Graceful degradation when financial data is missing — scores adjust dynamically

## Configuration

All tuneable parameters live in `config/settings.py`:

| Category | Key Parameters |
|----------|---------------|
| Scanner | `MIN_MARKET_CAP` (10B), `MIN_RS_SCORE` (5), `MIN_CANSLIM_SCORE` (70) |
| Performance | `MAX_WORKERS` (3), `CHUNK_SIZE` (50) |
| Technical | `EMA_SHORT` (8), `EMA_LONG` (21), `RSI_PERIOD` (14) |
| Growth | `C_GROWTH_TARGET` (0.25), `A_GROWTH_TARGET` (0.25) |
| RS Weights | `RS_Q1_WEIGHT` (0.4), `RS_Q2_WEIGHT`/`Q3`/`Q4` (0.2 each) |
| Caching | `TICKER_CACHE_EXPIRY_HOURS` (24), `RS_CACHE_DIR`, `TICKER_CACHE_DIR` |

## Caching

- **Ticker cache:** `ticker_cache/index_tickers_cache.json` — 24-hour TTL
- **RS score cache:** `rs_score_cache/rs_scores_cache.csv` — daily

Both directories are gitignored.

## Coding Conventions

- **Python 3.11+** with `from __future__ import annotations`
- **Type hints** on all function signatures
- **Google-style docstrings**
- **Snake_case** for functions/variables, **UPPER_CASE** for constants
- **Private functions** prefixed with underscore (`_safe_growth`, `_detect_volume_surge`)
- **Error handling:** try-except with specific exceptions; return `None` on failure rather than raising
- **Imports:** stdlib → third-party → local, each group separated by a blank line

## Testing

No formal test suite exists yet. No pytest config, no CI/CD pipeline.

## Git Workflow

- Branch naming: `claude/[feature-description]-[id]`
- PRs are used for merging features
- Commit messages are descriptive, plain-English summaries

## Known Issues

- Ticker fetching was recently refactored from Wikipedia scraping to iShares CSV endpoints — may still have data-format issues (see commit `d9d714e`)
- README is a placeholder (`# CANSLIM Bot\n- wip`)
