# Industry Group RS Filter — Design Spec

**Date:** 2026-04-22
**Status:** Approved

---

## Goal

Add a hard-gate industry group RS filter to `PortfolioSimulator` that rejects any entry signal whose industry group is not among the top N highest-RS groups in the current universe. This implements O'Neil's principle that leaders come from leading groups — 50% of a stock's move is driven by its industry group.

**Expected impact:** Higher win rate, fewer stop-loss exits from weak-sector entries, improved Sharpe and total return on top of the existing +1.43 Sharpe baseline.

---

## Approach

A new `core/industry_group.py` module handles two responsibilities:
1. Loading and caching a `ticker → industry` label map (from yfinance, 7-day TTL)
2. Computing the top-N group set from the current RS snapshot

`PortfolioSimulator` loads the industry map once at simulation start and passes it to a gate check inside `_evaluate_signals()` — after the regime gate, before individual signal scoring. No exit logic, stop-loss, eviction, or sizing is changed.

---

## Feature 1: Settings Constants

**Location:** `config/settings.py` — Stock Screening section

```python
INDUSTRY_GROUP_TOP_N: int = 20        # number of top industry groups allowed for entries
INDUSTRY_GROUP_MIN_SIZE: int = 3      # min stocks in a group to include it in ranking
INDUSTRY_GROUP_CACHE_PATH: str = ".artifacts/cache/industry_group_cache.json"
```

---

## Feature 2: `core/industry_group.py` — New Module

### `load_industry_map(tickers: list[str]) -> dict[str, str]`

Returns `{ticker: industry_label}` for every ticker in the list.

**Implementation:**
1. Load cache from `INDUSTRY_GROUP_CACHE_PATH` if it exists and is < 7 days old
2. For each ticker not in cache: call `yfinance.Ticker(sym).info`
   - Use `info["industry"]` if present and non-empty
   - Fallback to `info["sector"]` if industry is missing
   - If both missing: omit the ticker from the map (no entry in returned dict)
3. Write updated cache to disk as JSON: `{"fetched_at": "<ISO timestamp>", "map": {ticker: label}}`
4. Return the complete map

**Cache format:**
```json
{
  "fetched_at": "2026-04-22T01:00:00",
  "map": {
    "NVDA": "Semiconductors",
    "AAPL": "Consumer Electronics",
    "MSFT": "Software - Infrastructure"
  }
}
```

### `get_top_groups(rs_snapshot: dict[str, float], ticker_industry: dict[str, str]) -> set[str]`

Returns the set of industry group names that rank in the top `INDUSTRY_GROUP_TOP_N` by average RS.

**Implementation:**
1. Build `group_rs_totals: dict[str, list[float]]` — for each ticker in `rs_snapshot` that has an entry in `ticker_industry`, append its RS score to its group's list
2. Compute average RS per group: `avg_rs = sum(scores) / len(scores)`
3. Exclude groups with fewer than `INDUSTRY_GROUP_MIN_SIZE` members
4. Sort groups by avg_rs descending, take top `INDUSTRY_GROUP_TOP_N`
5. Return as `set[str]`

**Example with `TOP_N=2`, `MIN_SIZE=3`:**

| Group | Members | Avg RS |
|-------|---------|--------|
| Semiconductors | NVDA(95), AMD(88), AVGO(91) | 91.3 → **top** |
| Software | MSFT(82), CRM(79), NOW(85) | 82.0 → **top** |
| Banks | JPM(71), BAC(65) | 68.0 → excluded (< MIN_SIZE) |
| Energy | XOM(60), CVX(58), COP(62) | 60.0 → outside top 2 |

Result: `{"Semiconductors", "Software"}`

---

## Feature 3: Integration into `PortfolioSimulator`

**Location:** `core/backtest_engine.py`

### In `run()` — after regime tracker bootstrap

```python
from core.industry_group import load_industry_map

self._ticker_industry = load_industry_map(tickers)
```

`tickers` is the full universe list already available at this point in `run()`.

### In `_evaluate_signals()` — after regime gate, before signal loop

```python
from core.industry_group import get_top_groups

top_groups = get_top_groups(rs_snapshot, self._ticker_industry)
```

Then inside the per-ticker signal evaluation loop, immediately after the ticker is selected and before any scoring:

```python
ticker_group = self._ticker_industry.get(ticker)
if ticker_group is not None and ticker_group not in top_groups:
    continue  # hard gate — industry group not in top N
```

**Pass-through rule:** if `ticker_group is None` (no industry label in map), the gate is skipped and the ticker proceeds to full signal evaluation. Data gaps should not silently block valid signals.

---

## Feature 4: `SimulationResult` reporting

Add to the config dict in `run()`:

```python
"industry_group_top_n": settings.INDUSTRY_GROUP_TOP_N,
```

---

## Testing

**New file:** `tests/test_industry_group.py`

All tests use synthetic `rs_snapshot` dicts and manually constructed `ticker_industry` maps — no yfinance calls, no network.

### Test cases

| Test | Scenario | Expected |
|------|----------|----------|
| `test_top_groups_returns_top_n` | 5 groups, `TOP_N=2`, clear RS ordering | exactly the 2 highest-avg-RS group names returned |
| `test_min_size_excludes_small_groups` | one group has 2 stocks, `MIN_SIZE=3` | that group absent from result even if high RS |
| `test_missing_industry_ticker_passthrough` | ticker absent from `ticker_industry` map | `ticker_group is None` → gate skipped, not blocked |
| `test_group_rs_is_average_of_member_scores` | 3 stocks in group with RS 80, 90, 100 | group avg RS = 90.0 |
| `test_ticker_in_top_group_passes_gate` | ticker's group is in top N | ticker not filtered |
| `test_ticker_not_in_top_group_is_blocked` | ticker's group ranks outside top N | ticker skipped in signal loop |

### Integration test (network, excluded from default run)

```python
@pytest.mark.integration
def test_load_industry_map_fetches_real_data():
    """yfinance returns industry labels for known tickers."""
    result = load_industry_map(["NVDA", "AAPL", "JPM"])
    assert result["NVDA"] == "Semiconductors"
    assert "AAPL" in result
    assert "JPM" in result
```

---

## Affected Files

| File | Change |
|------|--------|
| `config/settings.py` | Add `INDUSTRY_GROUP_TOP_N`, `INDUSTRY_GROUP_MIN_SIZE`, `INDUSTRY_GROUP_CACHE_PATH` |
| `core/industry_group.py` | New module — `load_industry_map()` + `get_top_groups()` |
| `core/backtest_engine.py` | Load map in `run()`; gate check in `_evaluate_signals()` |
| `tests/test_industry_group.py` | New file — 6 unit tests + 1 integration test |

---

## Sample yfinance Industry Labels (from universe)

```
NVDA  → Semiconductors
AAPL  → Consumer Electronics
MSFT  → Software - Infrastructure
JPM   → Banks - Diversified
XOM   → Oil & Gas Integrated
UNH   → Healthcare Plans
AMZN  → Internet Retail
META  → Internet Content & Information
GOOGL → Internet Content & Information
TSLA  → Auto Manufacturers
```
