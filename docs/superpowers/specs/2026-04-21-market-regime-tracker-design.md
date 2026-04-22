# Market Regime Tracker — Design Spec

**Date:** 2026-04-21
**Status:** Approved

---

## Goal

Replace the current continuous M-score entry gate with a stateful, discrete O'Neil market regime tracker that blocks all new position entries during confirmed corrections, while leaving existing position exit rules (stop-loss, EMA trail, tier exits) completely unchanged.

**Expected impact:** Dramatically reduce the 68% stop-loss exit rate by preventing entries during weak/deteriorating markets. Sharpe ratio and total return should both improve in the backtest.

---

## Approach

A new `MarketRegimeTracker` class in `core/canslim/m_market_direction.py` maintains regime state across the entire simulation. It is updated **once per trading bar** (not just on signal days) so distribution day counts and rally attempt day counts are precise. The `PortfolioSimulator` holds one instance, bootstraps it before the main loop, and consults `allows_entries` before evaluating any signals.

The existing `CanslimStrategy.evaluate_market()` M-score is kept — it still feeds into the composite CANSLIM score. The regime tracker is an **additional hard gate** on top of it, not a replacement.

---

## Feature 1: `MarketRegime` Enum

**Location:** `core/canslim/m_market_direction.py`

```python
from enum import Enum

class MarketRegime(Enum):
    CONFIRMED_UPTREND = "confirmed_uptrend"
    UNDER_PRESSURE    = "under_pressure"
    CORRECTION        = "correction"
```

| Regime | Entries allowed | Meaning |
|--------|----------------|---------|
| `CONFIRMED_UPTREND` | Yes | Follow-through confirmed, < 3 distribution days |
| `UNDER_PRESSURE` | Yes | 3–4 distribution days — warning zone |
| `CORRECTION` | **No** | 5+ distribution days, or no follow-through yet confirmed |

---

## Feature 2: `MarketRegimeTracker` Class

**Location:** `core/canslim/m_market_direction.py`

### State

```python
class MarketRegimeTracker:
    regime: MarketRegime                 # current regime
    _dist_day_dates: list[pd.Timestamp]  # timestamps of distribution days in rolling window
    _rally_day_count: int                # bars elapsed since rally attempt began
    _rally_active: bool                  # whether a rally attempt is currently in progress
    _correction_low: float               # lowest close recorded during current correction
```

### `bootstrap(spy_df, start_date)` — call once before the simulation loop

Determines the initial regime from data before `start_date`:

1. Compute 200-day EMA of SPY closes up to `start_date`
2. If latest close > 200 EMA → initial regime = `CONFIRMED_UPTREND`; else → `CORRECTION`
3. Replay the last 25 bars before `start_date` through the distribution-day counter to populate `_dist_day_dates` with any distribution days already in the window

### `update(date, close, prev_close, volume, prev_volume) → MarketRegime` — call once per bar

Execute in this exact order:

1. **Age out** — remove from `_dist_day_dates` any timestamp more than `M_DISTRIBUTION_LOOKBACK` (25) trading bars old
2. **Check distribution day** — if `(close - prev_close) / prev_close <= -M_DISTRIBUTION_MIN_DECLINE` (−0.2%) AND `volume > prev_volume` → append `date` to `_dist_day_dates`
3. **Hard correction gate** — if `len(_dist_day_dates) >= M_MAX_DISTRIBUTION_DAYS` (5): `regime = CORRECTION`
4. **Uptrend zone** — elif regime is NOT `CORRECTION`:
   - `len(_dist_day_dates) >= M_REGIME_PRESSURE_DIST_DAYS` (3) → `UNDER_PRESSURE`
   - else → `CONFIRMED_UPTREND`
5. **Rally attempt logic** — only runs when `regime == CORRECTION`:
   - **Before rally starts** (`not _rally_active`):
     - `_correction_low = min(_correction_low, close)` — track the bottom
     - If `close > prev_close` (up day): set `_rally_active = True`, `_rally_day_count = 1`
   - **During rally** (`_rally_active`):
     - If `close < _correction_low` → undercut on any bar: reset `_rally_active = False`, `_rally_day_count = 0`, `_correction_low = float("inf")`
     - Elif `close > prev_close` (up day, not undercut): `_rally_day_count += 1`
   - **Follow-through check** (runs after the above, only if still `_rally_active`): if `_rally_day_count >= M_FOLLOW_THROUGH_MIN_DAY` (4) AND `(close - prev_close) / prev_close >= M_FOLLOW_THROUGH_MIN_PCT` (1.5%) AND `volume > prev_volume`:
     - `regime = CONFIRMED_UPTREND`
     - `_dist_day_dates.clear()` — fresh count after confirmed uptrend
     - `_rally_active = False`, `_rally_day_count = 0`, `_correction_low = float("inf")`
6. Return `self.regime`

### `allows_entries → bool`

```python
@property
def allows_entries(self) -> bool:
    return self.regime != MarketRegime.CORRECTION
```

### `distribution_days → int`

```python
@property
def distribution_days(self) -> int:
    return len(self._dist_day_dates)
```

---

## Feature 3: Integration into `PortfolioSimulator`

**Location:** `core/backtest_engine.py`

### In `run()` — before the main loop

```python
from core.canslim.m_market_direction import MarketRegimeTracker

regime_tracker = MarketRegimeTracker()
regime_tracker.bootstrap(benchmark_df, start_ts)
self._regime_tracker = regime_tracker
```

### In `run()` — top of each bar iteration (after `eval_date` is set, before exits)

```python
if day_idx > 0:
    hist = benchmark_df.loc[:eval_date]
    if len(hist) >= 2:
        prev_bar = hist.iloc[-2]
        curr_bar = hist.iloc[-1]
        regime_tracker.update(
            date=eval_date,
            close=float(curr_bar["Close"]),
            prev_close=float(prev_bar["Close"]),
            volume=float(curr_bar["Volume"]),
            prev_volume=float(prev_bar["Volume"]),
        )
```

### In `_evaluate_signals()` — first line of the method body

```python
if not self._regime_tracker.allows_entries:
    return []
```

### In `SimulationResult` config dict — add for reporting

```python
"market_regime_final": self._regime_tracker.regime.value,
"final_distribution_days": self._regime_tracker.distribution_days,
```

---

## Feature 4: Settings Constant

**Location:** `config/settings.py` — ORDER EXECUTION SETTINGS section

```python
M_REGIME_PRESSURE_DIST_DAYS: int = 3  # distribution days to enter UNDER_PRESSURE warning zone
```

All other required constants already exist: `M_DISTRIBUTION_LOOKBACK = 25`, `M_MAX_DISTRIBUTION_DAYS = 5`, `M_DISTRIBUTION_MIN_DECLINE = 0.002`, `M_FOLLOW_THROUGH_MIN_PCT = 0.015`, `M_FOLLOW_THROUGH_MIN_DAY = 4`.

---

## Testing

**New file:** `tests/test_market_regime.py`

### Helper

```python
def _make_spy_bars(
    closes: list[float],
    volumes: list[float],
    start: str = "2026-01-01",
) -> pd.DataFrame:
    """Build a minimal SPY OHLCV DataFrame for regime tests."""
```

### Test cases

| Test | Scenario | Expected |
|------|----------|---------|
| `test_bootstrap_above_200_ema` | SPY close > 200-day EMA | `CONFIRMED_UPTREND` |
| `test_bootstrap_below_200_ema` | SPY close < 200-day EMA | `CORRECTION` |
| `test_five_dist_days_triggers_correction` | 5 down+heavy-vol bars | `CORRECTION`, `allows_entries=False` |
| `test_three_dist_days_triggers_under_pressure` | 3 dist days | `UNDER_PRESSURE`, `allows_entries=True` |
| `test_dist_days_age_out_restores_uptrend` | 3 dist days, 26 bars later no new dist | `CONFIRMED_UPTREND` |
| `test_follow_through_on_day4_confirms_uptrend` | correction → 4-day rally → +1.5% heavier vol | `CONFIRMED_UPTREND` |
| `test_follow_through_on_day3_not_valid` | same but rally day 3 | stays `CORRECTION` |
| `test_rally_undercut_resets_count` | rally attempt, then close < correction_low | rally resets to day 0 |
| `test_dist_days_cleared_on_follow_through` | 4 dist days → follow-through | dist list cleared, fresh count |
| `test_allows_entries_false_in_correction` | regime = CORRECTION | `allows_entries` returns `False` |

---

## Affected Files

| File | Change |
|------|--------|
| `core/canslim/m_market_direction.py` | Add `MarketRegime` enum + `MarketRegimeTracker` class |
| `core/backtest_engine.py` | Bootstrap tracker in `run()`; per-bar update in loop; entry gate in `_evaluate_signals()` |
| `config/settings.py` | Add `M_REGIME_PRESSURE_DIST_DAYS = 3` |
| `tests/test_market_regime.py` | New file — 10 regime transition tests |
