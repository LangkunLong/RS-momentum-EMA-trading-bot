# Pivot Buy Point Detection — Design Spec

**Date:** 2026-04-22
**Status:** Approved

---

## Goal

Add O'Neil-style pivot buy point detection that enforces the "buy within 5% of the pivot, never chase" rule. Stocks breaking out from a valid flat base or cup-with-handle pattern are entered only when current price is within 5% of the pattern's pivot price. Stocks that have already run beyond the buy zone are rejected as extended.

**Expected impact:** Higher average win (fewer chasing entries), lower average loss (entries at lower-risk pivot points), improved Sharpe on top of the existing 1.69 baseline.

---

## Approach

A new `core/pivot_detector.py` module detects flat base and cup-with-handle patterns from OHLCV data and returns the pattern's pivot price. `_evaluate_technical_at_date()` in `backtest.py` calls `find_pivot()` and adds `"pivot"` and `"in_buy_zone"` to the returned tech dict. `CanslimStrategy.evaluate_symbol()` in `core/backtest_engine.py` adds `in_buy_zone` to the `tech_pass` gate.

**Pass-through rule:** if no pattern is detectable, `in_buy_zone` defaults to `True` — the signal falls through to the existing proximity-to-52-week-high logic unchanged.

**PEG exemption:** Power Earnings Gaps bypass the buy zone check — a PEG is itself the breakout event and is always a valid entry.

No changes to exit logic, stop-loss, eviction, sizing, or the market regime tracker.

---

## Feature 1: Settings Constants

**Location:** `config/settings.py`

```python
PIVOT_BUY_ZONE_PCT: float = 0.05           # max % above pivot to enter (O'Neil's 5% rule)
PIVOT_CUP_MIN_WEEKS: int = 7               # min cup duration in weeks (35 trading days)
PIVOT_CUP_MIN_DECLINE_PCT: float = 0.15   # min cup depth (shallower = flat base)
PIVOT_CUP_MAX_DECLINE_PCT: float = 0.33   # max cup depth (deeper = failed base)
PIVOT_FLAT_BASE_MIN_WEEKS: int = 5         # min flat base duration (25 trading days)
PIVOT_FLAT_BASE_MAX_DECLINE_PCT: float = 0.15  # max decline for a valid flat base
PIVOT_HANDLE_MAX_DECLINE_PCT: float = 0.12     # max handle pullback (12%)
```

---

## Feature 2: `core/pivot_detector.py` — New Module

### `detect_flat_base(closes: pd.Series) -> float | None`

Detects a flat base in the most recent bars of a closing price series.

**Algorithm:**
1. Look at the last `PIVOT_FLAT_BASE_MIN_WEEKS * 5` to `13 * 5` trading bars (25–65 bars)
2. `base_high = closes.max()`, `base_low = closes.min()`
3. Decline = `(base_high - base_low) / base_high`
4. If `decline > PIVOT_FLAT_BASE_MAX_DECLINE_PCT` → not a flat base, return `None`
5. If fewer than `PIVOT_FLAT_BASE_MIN_WEEKS * 5` bars available → return `None`
6. **Pivot** = `base_high` (the resistance level of the consolidation range)
7. Return `base_high`

### `detect_cup_with_handle(closes: pd.Series) -> float | None`

Detects a cup-with-handle pattern in the recent close series.

**Algorithm:**
1. Require at least `PIVOT_CUP_MIN_WEEKS * 5` bars (35 bars minimum); look back up to `13 * 5` bars (65 bars)
2. **Left lip**: `left_lip = closes.iloc[0]` (start of the window — the prior high)
3. **Cup low**: `cup_low_idx = closes.argmin()` within the first 80% of the window
4. **Cup decline**: `cup_decline = (left_lip - cup_low) / left_lip`
   - If `cup_decline < PIVOT_CUP_MIN_DECLINE_PCT` or `cup_decline > PIVOT_CUP_MAX_DECLINE_PCT` → return `None`
5. **Right lip recovery**: the close after `cup_low_idx` must recover to within 5% of `left_lip` before the handle begins
   - `right_lip = closes.iloc[cup_low_idx:].max()`
   - If `right_lip < left_lip * 0.95` → cup not fully formed, return `None`
6. **Handle**: the last `5–20` bars of the window (after right lip recovery)
   - `handle_high = closes.iloc[cup_low_idx:].max()`
   - `handle_low = closes.iloc[-20:].min()` (minimum of last 20 bars)
   - Handle decline: `(handle_high - handle_low) / handle_high`
   - If `handle_decline > PIVOT_HANDLE_MAX_DECLINE_PCT` → handle too deep, return `None`
   - Handle must form in the upper half of the cup range: `handle_low > cup_low + (left_lip - cup_low) * 0.5`
   - If not → return `None`
7. **Pivot** = `closes.iloc[cup_low_idx:].max()` (the handle's high)
8. Return pivot

### `find_pivot(closes: pd.Series) -> float | None`

Entry point. Accepts a pre-sliced closing price series (caller is responsible for slicing to eval_date). Tries cup-with-handle first (more precise pivot), falls back to flat base.

```python
def find_pivot(closes: pd.Series) -> float | None:
    if len(closes) < 25:
        return None
    lookback = min(65, len(closes))
    window = closes.iloc[-lookback:]
    # Try cup-with-handle first (more precise pivot)
    pivot = detect_cup_with_handle(window)
    if pivot is not None:
        return pivot
    # Fall back to flat base
    return detect_flat_base(window)
```

### `is_in_buy_zone(current_price: float, pivot: float, zone_pct: float = settings.PIVOT_BUY_ZONE_PCT) -> bool`

```python
def is_in_buy_zone(current_price: float, pivot: float, zone_pct: float = settings.PIVOT_BUY_ZONE_PCT) -> bool:
    return current_price <= pivot * (1 + zone_pct)
```

---

## Feature 3: Integration into `_evaluate_technical_at_date()`

**Location:** `backtest.py`

After computing `closes` and `latest_close` and before the return statement, add:

```python
from core.pivot_detector import find_pivot, is_in_buy_zone

pivot = find_pivot(closes)
in_buy_zone = is_in_buy_zone(latest_close, pivot) if pivot is not None else True
```

Add to the returned dict:
```python
"pivot": pivot,
"in_buy_zone": in_buy_zone,
```

---

## Feature 4: Integration into `CanslimStrategy.evaluate_symbol()`

**Location:** `core/backtest_engine.py`

After the existing tech dict reads (around line 503–505), add:
```python
in_buy_zone = bool(tech.get("in_buy_zone", True))
```

Update `tech_pass` (currently line 517):
```python
# Before:
tech_pass = (has_breakout and has_surge) or has_peg_today
# After:
tech_pass = (has_breakout and has_surge and in_buy_zone) or has_peg_today
```

Add `"in_buy_zone"` and `"pivot"` to the signal row dict for logging.

---

## Feature 5: Integration into `backtest.py`'s `_should_emit_buy_signal()`

**Location:** `backtest.py` (lines 279–294)

Add `in_buy_zone: bool = True` parameter and include in gate:
```python
def _should_emit_buy_signal(
    *,
    total_score: float,
    rs_score: float,
    market_is_bullish: bool,
    has_breakout: bool,
    has_volume_surge: bool,
    has_peg_today: bool,
    in_buy_zone: bool = True,
) -> bool:
    return (
        total_score >= settings.MIN_CANSLIM_SCORE
        and rs_score >= settings.MIN_RS_SCORE
        and market_is_bullish
        and ((has_breakout and has_volume_surge and in_buy_zone) or has_peg_today)
    )
```

---

## Testing

**New file:** `tests/test_pivot_detector.py`

All tests use synthetic `pd.Series` / `pd.DataFrame` built in-memory — no network calls.

### Helper

```python
def _make_closes(values: list[float], start: str = "2024-01-02") -> pd.Series:
    dates = pd.bdate_range(start=start, periods=len(values))
    return pd.Series(values, index=dates, name="Close")
```

### Test cases

| Test | Scenario | Expected |
|------|----------|----------|
| `test_flat_base_detected_tight_range` | 30 bars, 10% peak-to-trough decline | returns float (range high) |
| `test_flat_base_rejected_too_deep` | 30 bars, 20% decline | `None` |
| `test_flat_base_rejected_too_short` | 15 bars, 8% decline | `None` (< 25 bars) |
| `test_cup_handle_detected` | 50 bars: 25% U-decline + recovery + 8% handle | returns float (handle high) |
| `test_cup_rejected_too_shallow` | 40 bars, 10% cup decline | `None` (< 15% floor) |
| `test_cup_handle_in_lower_half_rejected` | handle trough below cup midpoint | `None` |
| `test_in_buy_zone_within_5pct` | price = pivot × 1.03 | `True` |
| `test_in_buy_zone_extended` | price = pivot × 1.07 | `False` |
| `test_no_pattern_is_passthrough` | noisy bars, no clean base | `find_pivot` returns `None` |
| `test_peg_bypasses_buy_zone` | `has_peg_today=True`, `in_buy_zone=False` | `tech_pass` is still `True` |

---

## Affected Files

| File | Change |
|------|--------|
| `config/settings.py` | Add 7 `PIVOT_*` constants |
| `core/pivot_detector.py` | New module — `detect_flat_base()`, `detect_cup_with_handle()`, `find_pivot()`, `is_in_buy_zone()` |
| `backtest.py` | Call `find_pivot()` in `_evaluate_technical_at_date()`; add `in_buy_zone` to `_should_emit_buy_signal()` |
| `core/backtest_engine.py` | Read `in_buy_zone` from tech dict; add to `tech_pass` gate in `evaluate_symbol()` |
| `tests/test_pivot_detector.py` | New file — 10 unit tests |
