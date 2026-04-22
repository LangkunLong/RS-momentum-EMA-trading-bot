# Industry Group RS Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a hard-gate industry group RS filter to `PortfolioSimulator` that rejects entry signals from stocks whose yfinance industry group is not among the top 20 highest-RS groups in the current universe.

**Architecture:** A new `core/industry_group.py` module provides two pure functions: `load_industry_map()` (fetches + caches yfinance industry labels per ticker) and `get_top_groups()` (ranks groups by average member RS, returns top-N set). `PortfolioSimulator.run()` loads the map once; `_evaluate_signals()` calls `get_top_groups()` per signal day and hard-gates each ticker against it.

**Tech Stack:** Python 3.11+, yfinance, pandas, existing `config/settings.py` and `core/backtest_engine.py` patterns.

---

## File Map

| File | What changes |
|------|-------------|
| `config/settings.py` | Add `INDUSTRY_GROUP_TOP_N`, `INDUSTRY_GROUP_MIN_SIZE`, `INDUSTRY_GROUP_CACHE_PATH` |
| `core/industry_group.py` | New module — `get_top_groups()` + `load_industry_map()` |
| `core/backtest_engine.py` | Load map in `run()` after regime bootstrap; gate check in `_evaluate_signals()` |
| `tests/test_industry_group.py` | New file — 6 unit tests + 1 integration test |

---

## Task 1: Add settings constants

**Files:**
- Modify: `config/settings.py`

- [ ] **Step 1: Add the three new constants**

Open `config/settings.py`. Find the block containing `M_REGIME_PRESSURE_DIST_DAYS` (around line 269). Add the three new constants in the Stock Screening section — search for the line `MIN_CANSLIM_SCORE = 70` and add below the nearby screening constants:

```python
INDUSTRY_GROUP_TOP_N: int = 20        # number of top industry groups allowed for entries
INDUSTRY_GROUP_MIN_SIZE: int = 3      # min stocks in a group to include it in ranking
INDUSTRY_GROUP_CACHE_PATH: str = ".artifacts/cache/industry_group_cache.json"
```

- [ ] **Step 2: Verify all three load correctly**

```bash
python -c "from config import settings; print(settings.INDUSTRY_GROUP_TOP_N, settings.INDUSTRY_GROUP_MIN_SIZE, settings.INDUSTRY_GROUP_CACHE_PATH)"
```

Expected: `20 3 .artifacts/cache/industry_group_cache.json`

- [ ] **Step 3: Commit**

```bash
git add config/settings.py
git commit -m "Add INDUSTRY_GROUP_TOP_N/MIN_SIZE/CACHE_PATH settings"
```

---

## Task 2: Implement `get_top_groups()` with TDD

**Files:**
- Create: `core/industry_group.py`
- Create: `tests/test_industry_group.py`

`get_top_groups()` is a pure function with no network calls — write tests first, then implement.

- [ ] **Step 1: Create the test file with 6 unit tests**

Create `tests/test_industry_group.py`:

```python
"""Tests for industry group RS ranking logic."""
from __future__ import annotations

from core.industry_group import get_top_groups


def test_top_groups_returns_top_n() -> None:
    """Returns exactly top N groups by average RS score."""
    rs = {
        "NVDA": 95.0, "AMD": 88.0, "AVGO": 91.0,  # Semiconductors avg=91.3
        "MSFT": 82.0, "CRM": 79.0, "NOW": 85.0,   # Software avg=82.0
        "JPM": 71.0, "BAC": 65.0, "GS": 68.0,      # Banks avg=68.0
        "XOM": 60.0, "CVX": 58.0, "COP": 62.0,     # Energy avg=60.0
        "UNH": 55.0, "CVS": 52.0, "CI": 57.0,      # Healthcare avg=54.7
    }
    industry = {
        "NVDA": "Semiconductors", "AMD": "Semiconductors", "AVGO": "Semiconductors",
        "MSFT": "Software", "CRM": "Software", "NOW": "Software",
        "JPM": "Banks", "BAC": "Banks", "GS": "Banks",
        "XOM": "Energy", "CVX": "Energy", "COP": "Energy",
        "UNH": "Healthcare", "CVS": "Healthcare", "CI": "Healthcare",
    }
    result = get_top_groups(rs, industry, top_n=2, min_size=3)
    assert result == {"Semiconductors", "Software"}


def test_min_size_excludes_small_groups() -> None:
    """Groups with fewer than min_size members are excluded from ranking."""
    rs = {
        "NVDA": 99.0, "AMD": 98.0,                  # Semiconductors: 2 members
        "MSFT": 82.0, "CRM": 79.0, "NOW": 85.0,    # Software: 3 members
        "JPM": 71.0, "BAC": 65.0, "GS": 68.0,       # Banks: 3 members
    }
    industry = {
        "NVDA": "Semiconductors", "AMD": "Semiconductors",
        "MSFT": "Software", "CRM": "Software", "NOW": "Software",
        "JPM": "Banks", "BAC": "Banks", "GS": "Banks",
    }
    # Semiconductors has highest RS (98.5 avg) but only 2 members < min_size=3
    result = get_top_groups(rs, industry, top_n=1, min_size=3)
    assert "Semiconductors" not in result
    assert "Software" in result


def test_group_rs_is_average_of_member_scores() -> None:
    """Group RS equals the arithmetic mean of its members' RS scores."""
    rs = {"A": 80.0, "B": 90.0, "C": 100.0}
    industry = {"A": "Tech", "B": "Tech", "C": "Tech"}
    result = get_top_groups(rs, industry, top_n=1, min_size=3)
    assert "Tech" in result  # avg = 90.0, only group → top 1


def test_missing_industry_ticker_is_ignored() -> None:
    """Tickers absent from ticker_industry map are simply ignored in group computation."""
    rs = {
        "NVDA": 95.0, "AMD": 88.0, "AVGO": 91.0,  # Semiconductors avg=91.3
        "UNKNOWN": 99.0,                             # no industry label — ignored
        "JPM": 71.0, "BAC": 65.0, "GS": 68.0,      # Banks avg=68.0
    }
    industry = {
        "NVDA": "Semiconductors", "AMD": "Semiconductors", "AVGO": "Semiconductors",
        "JPM": "Banks", "BAC": "Banks", "GS": "Banks",
        # UNKNOWN intentionally omitted
    }
    result = get_top_groups(rs, industry, top_n=1, min_size=3)
    assert "Semiconductors" in result
    assert "Banks" not in result


def test_ticker_in_top_group_not_blocked() -> None:
    """A ticker whose group is in top_groups passes the gate (not in the filtered-out set)."""
    rs = {
        "NVDA": 95.0, "AMD": 88.0, "AVGO": 91.0,
        "JPM": 71.0, "BAC": 65.0, "GS": 68.0,
    }
    industry = {
        "NVDA": "Semiconductors", "AMD": "Semiconductors", "AVGO": "Semiconductors",
        "JPM": "Banks", "BAC": "Banks", "GS": "Banks",
    }
    top_groups = get_top_groups(rs, industry, top_n=1, min_size=3)
    # NVDA's group should be in top_groups → gate passes
    ticker_group = industry.get("NVDA")
    assert ticker_group in top_groups


def test_ticker_not_in_top_group_is_blocked() -> None:
    """A ticker whose group is outside top_groups is blocked by the gate."""
    rs = {
        "NVDA": 95.0, "AMD": 88.0, "AVGO": 91.0,
        "JPM": 71.0, "BAC": 65.0, "GS": 68.0,
    }
    industry = {
        "NVDA": "Semiconductors", "AMD": "Semiconductors", "AVGO": "Semiconductors",
        "JPM": "Banks", "BAC": "Banks", "GS": "Banks",
    }
    top_groups = get_top_groups(rs, industry, top_n=1, min_size=3)
    # JPM's group (Banks) should NOT be in top_groups → gate blocks
    ticker_group = industry.get("JPM")
    assert ticker_group not in top_groups
```

- [ ] **Step 2: Run to confirm tests fail**

```bash
python -m pytest tests/test_industry_group.py -v --tb=short 2>&1 | tail -15
```

Expected: all 6 FAIL with `ModuleNotFoundError: No module named 'core.industry_group'`

- [ ] **Step 3: Create `core/industry_group.py` with `get_top_groups()`**

Create `core/industry_group.py`:

```python
"""Industry group RS ranking for O'Neil-style group-strength filtering."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

from config import settings

logger = logging.getLogger(__name__)


def get_top_groups(
    rs_snapshot: dict[str, float],
    ticker_industry: dict[str, str],
    top_n: int = settings.INDUSTRY_GROUP_TOP_N,
    min_size: int = settings.INDUSTRY_GROUP_MIN_SIZE,
) -> set[str]:
    """Return the top-N industry groups by average RS score.

    Groups with fewer than min_size members are excluded from ranking.
    Tickers absent from ticker_industry are silently ignored.

    Args:
        rs_snapshot: Mapping of ticker → current RS score (0–100).
        ticker_industry: Mapping of ticker → industry label string.
        top_n: Number of top groups to return.
        min_size: Minimum member count for a group to qualify.

    Returns:
        Set of industry label strings that rank in the top N.
    """
    group_scores: dict[str, list[float]] = {}
    for ticker, rs in rs_snapshot.items():
        group = ticker_industry.get(ticker)
        if group is None:
            continue
        group_scores.setdefault(group, []).append(rs)

    ranked = [
        (group, sum(scores) / len(scores))
        for group, scores in group_scores.items()
        if len(scores) >= min_size
    ]
    ranked.sort(key=lambda x: x[1], reverse=True)
    return {group for group, _ in ranked[:top_n]}


def load_industry_map(tickers: list[str]) -> dict[str, str]:
    """Load ticker → industry label map, fetching from yfinance and caching to disk.

    Uses info["industry"] with fallback to info["sector"]. Tickers with neither
    are omitted from the returned map. Cache TTL is 7 days.

    Args:
        tickers: List of ticker symbols to look up.

    Returns:
        Mapping of ticker → industry label string (partial — missing tickers omitted).
    """
    cache_path = Path(settings.INDUSTRY_GROUP_CACHE_PATH)
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    cached_map: dict[str, str] = {}
    if cache_path.exists():
        try:
            payload = json.loads(cache_path.read_text())
            fetched_at = datetime.fromisoformat(payload["fetched_at"])
            age_days = (datetime.now(timezone.utc) - fetched_at.replace(tzinfo=timezone.utc)).days
            if age_days < 7:
                cached_map = payload.get("map", {})
        except Exception:
            cached_map = {}

    missing = [t for t in tickers if t not in cached_map]
    if missing:
        logger.info("Fetching industry labels for %d tickers from yfinance", len(missing))
        for sym in missing:
            try:
                info = yf.Ticker(sym).info
                label = (info.get("industry") or "").strip() or (info.get("sector") or "").strip()
                if label:
                    cached_map[sym] = label
            except Exception:
                pass

        cache_path.write_text(
            json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(), "map": cached_map})
        )

    return {t: cached_map[t] for t in tickers if t in cached_map}
```

- [ ] **Step 4: Run tests to confirm all 6 pass**

```bash
python -m pytest tests/test_industry_group.py -v --tb=short 2>&1 | tail -15
```

Expected: 6 PASSED

- [ ] **Step 5: Run full suite to confirm no regressions**

```bash
python -m pytest tests/ --tb=short 2>&1 | tail -5
```

Expected: 319+ passed, 3 pre-existing failures only.

- [ ] **Step 6: Commit**

```bash
git add core/industry_group.py tests/test_industry_group.py
git commit -m "Add industry_group module with get_top_groups() + 6 unit tests"
```

---

## Task 3: Add integration test for `load_industry_map()`

**Files:**
- Modify: `tests/test_industry_group.py`

- [ ] **Step 1: Append integration test to `tests/test_industry_group.py`**

First add `import pytest` to the imports at the top of the file (after the existing `from core.industry_group import get_top_groups` line):

```python
import pytest
```

Then add the integration test at the end of the file:

```python
@pytest.mark.integration
def test_load_industry_map_fetches_real_data() -> None:
    """yfinance returns industry labels for known large-cap tickers."""
    from core.industry_group import load_industry_map

    result = load_industry_map(["NVDA", "AAPL", "JPM"])
    assert result.get("NVDA") == "Semiconductors"
    assert "AAPL" in result
    assert "JPM" in result
```

- [ ] **Step 2: Confirm unit tests still pass (integration test is excluded by default)**

```bash
python -m pytest tests/test_industry_group.py -v --tb=short 2>&1 | tail -10
```

Expected: 6 passed (integration test skipped unless `-m integration` flag is used).

- [ ] **Step 3: Commit**

```bash
git add tests/test_industry_group.py
git commit -m "Add integration test for load_industry_map() yfinance fetch"
```

---

## Task 4: Integrate into `PortfolioSimulator`

**Files:**
- Modify: `core/backtest_engine.py`

Three surgical edits. Read the file before editing to confirm exact line numbers.

- [ ] **Step 1: Add import at the top of `core/backtest_engine.py`**

Find the existing imports block at the top of the file (around line 29–31). After the line:

```python
from core.canslim.m_market_direction import MarketRegimeTracker
```

Add:

```python
from core.industry_group import get_top_groups, load_industry_map
```

- [ ] **Step 2: Load industry map in `run()` after regime bootstrap**

Find the regime bootstrap block in `run()` (around line 660–662):

```python
        regime_tracker = MarketRegimeTracker()
        regime_tracker.bootstrap(benchmark_df, start_ts)
        self._regime_tracker = regime_tracker
```

Immediately after that block, add:

```python
        self._ticker_industry = load_industry_map(tickers)
```

- [ ] **Step 3: Add gate check in `_evaluate_signals()`**

In `_evaluate_signals()`, find the line where `rs_snapshot` is computed (line ~774):

```python
        signals: List[dict] = []
        rs_snapshot = _calculate_rs_snapshot(all_closes, eval_date)
        for ticker in tickers:
```

Replace with:

```python
        signals: List[dict] = []
        rs_snapshot = _calculate_rs_snapshot(all_closes, eval_date)
        top_groups = get_top_groups(rs_snapshot, self._ticker_industry)
        for ticker in tickers:
```

Then inside the `for ticker in tickers:` loop, find the first `continue` statement (around line 776–777):

```python
            if ticker in self._open_positions or ticker not in ticker_ohlcv:
                continue
```

Add the industry gate immediately after that block:

```python
            ticker_group = self._ticker_industry.get(ticker)
            if ticker_group is not None and ticker_group not in top_groups:
                continue
```

- [ ] **Step 4: Add reporting to `SimulationResult` config dict**

Find the `config={` dict in the `return SimulationResult(...)` call (around line 731). Add to the dict:

```python
                "industry_group_top_n": settings.INDUSTRY_GROUP_TOP_N,
```

- [ ] **Step 5: Verify the module imports cleanly**

```bash
python -c "from core.backtest_engine import PortfolioSimulator; print('OK')"
```

Expected: `OK`

- [ ] **Step 6: Run the full test suite**

```bash
python -m pytest tests/ --tb=short 2>&1 | tail -10
```

Expected: 325+ passed, 3 pre-existing failures only.

- [ ] **Step 7: Commit**

```bash
git add core/backtest_engine.py
git commit -m "Integrate industry group RS gate into PortfolioSimulator"
```

---

## Task 5: Backtest validation

- [ ] **Step 1: Run the full test suite one final time**

```bash
python -m pytest tests/ --tb=short 2>&1 | tail -5
```

Expected: 325+ passed, 3 pre-existing failures only.

- [ ] **Step 2: Run the backtest**

```bash
python backtest_pnl.py --technical-only --universe large_cap --start-date 2023-04-01 --end-date 2026-04-01 2>&1 | grep -A 25 "Portfolio vs Benchmark"
```

**Baseline (market regime tracker, before this feature):**
```
Total Return     37.1%   58.2% (SPY)
Annualized       11.1%   16.5%
Max Drawdown     -6.2%  -19.0%
Sharpe Ratio      1.43    1.11
Win Rate         65.9%
Closed Trades:   44
Stop-loss exits: 34/44
```

**Expected improvement:** higher win rate (target > 70%), fewer stop-loss exits, Sharpe ≥ 1.43. Total return may stay similar or improve as weak-group entries are eliminated.

- [ ] **Step 3: Commit if any cleanup needed**

```bash
git add -p
git commit -m "Industry group RS filter: backtest validated"
```
