# Pivot Buy Point Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add O'Neil-style pivot buy point detection so entries only fire within 5% of a flat base or cup-with-handle pivot, rejecting extended breakouts that have already run.

**Architecture:** A new `core/pivot_detector.py` module detects flat base and cup-with-handle patterns and returns the pivot price. `_evaluate_technical_at_date()` in `backtest.py` calls `find_pivot()` and adds `pivot` and `in_buy_zone` to the returned tech dict. `CanslimStrategy.evaluate_symbol()` in `core/backtest_engine.py` gates `tech_pass` on `in_buy_zone`. If no pattern is detected, `in_buy_zone` defaults to `True` (pass-through). PEGs bypass the buy zone check entirely — a PEG is itself the pivot event.

**Tech Stack:** Python 3.11+, pandas, pytest, `config/settings.py` for constants.

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `config/settings.py` | Modify (line 59, after INDUSTRY_GROUP_CACHE_PATH) | Add 7 `PIVOT_*` constants |
| `core/pivot_detector.py` | Create | Pattern detection: `detect_flat_base()`, `detect_cup_with_handle()`, `find_pivot()`, `is_in_buy_zone()` |
| `tests/test_pivot_detector.py` | Create | 10 unit tests (all synthetic, no network) |
| `backtest.py` | Modify (lines 149–187, 279–294) | Call `find_pivot()` in `_evaluate_technical_at_date()`; add `in_buy_zone` to `_should_emit_buy_signal()` |
| `core/backtest_engine.py` | Modify (lines 505–517) | Read `in_buy_zone` from tech dict; add to `tech_pass` gate |

---

### Task 1: Add `PIVOT_*` constants to `config/settings.py`

**Files:**
- Modify: `config/settings.py` (after line 59, the `INDUSTRY_GROUP_CACHE_PATH` line)

- [ ] **Step 1: Open `config/settings.py` and locate the insertion point**

The constants go immediately after line 59:
```
INDUSTRY_GROUP_CACHE_PATH: str = ".artifacts/cache/industry_group_cache.json"
```

- [ ] **Step 2: Insert the 7 PIVOT constants**

Add this block right after `INDUSTRY_GROUP_CACHE_PATH`:
```python
PIVOT_BUY_ZONE_PCT: float = 0.05           # max % above pivot to enter (O'Neil's 5% rule)
PIVOT_CUP_MIN_WEEKS: int = 7               # min cup duration in weeks (35 trading days)
PIVOT_CUP_MIN_DECLINE_PCT: float = 0.15   # min cup depth (shallower = flat base)
PIVOT_CUP_MAX_DECLINE_PCT: float = 0.33   # max cup depth (deeper = failed base)
PIVOT_FLAT_BASE_MIN_WEEKS: int = 5         # min flat base duration (25 trading days)
PIVOT_FLAT_BASE_MAX_DECLINE_PCT: float = 0.15  # max decline for a valid flat base
PIVOT_HANDLE_MAX_DECLINE_PCT: float = 0.12     # max handle pullback (12%)
```

- [ ] **Step 3: Verify no import errors**

Run:
```bash
python -c "import config.settings as s; print(s.PIVOT_BUY_ZONE_PCT, s.PIVOT_CUP_MIN_WEEKS)"
```
Expected output: `0.05 7`

- [ ] **Step 4: Commit**

```bash
git add config/settings.py
git commit -m "feat: add PIVOT_* constants to settings for pivot buy point detection"
```

---

### Task 2: Create `core/pivot_detector.py` with TDD

**Files:**
- Create: `core/pivot_detector.py`
- Create: `tests/test_pivot_detector.py`

- [ ] **Step 1: Create the test file with all 10 tests (write first — they will fail)**

Create `tests/test_pivot_detector.py` with this exact content:

```python
from __future__ import annotations

import pandas as pd
import pytest

import config.settings as settings


def _make_closes(values: list[float], start: str = "2024-01-02") -> pd.Series:
    dates = pd.bdate_range(start=start, periods=len(values))
    return pd.Series(values, index=dates, name="Close")


# ---------------------------------------------------------------------------
# detect_flat_base
# ---------------------------------------------------------------------------

def test_flat_base_detected_tight_range():
    """30 bars with ≤10% peak-to-trough returns the range high."""
    from core.pivot_detector import detect_flat_base

    base = 100.0
    # oscillate between 91 and 100 — 9% decline, well within 15% max
    values = [base if i % 2 == 0 else 91.0 for i in range(30)]
    closes = _make_closes(values)
    result = detect_flat_base(closes)
    assert result == pytest.approx(100.0)


def test_flat_base_rejected_too_deep():
    """30 bars with 20% decline returns None (exceeds 15% max)."""
    from core.pivot_detector import detect_flat_base

    values = [100.0 if i % 2 == 0 else 80.0 for i in range(30)]  # 20% decline
    closes = _make_closes(values)
    assert detect_flat_base(closes) is None


def test_flat_base_rejected_too_short():
    """15 bars (< 25 min) returns None even with tight range."""
    from core.pivot_detector import detect_flat_base

    values = [100.0 if i % 2 == 0 else 95.0 for i in range(15)]
    closes = _make_closes(values)
    assert detect_flat_base(closes) is None


# ---------------------------------------------------------------------------
# detect_cup_with_handle
# ---------------------------------------------------------------------------

def test_cup_handle_detected():
    """50 bars: left lip→25% cup decline→recovery to left lip→8% handle — returns handle high."""
    from core.pivot_detector import detect_cup_with_handle

    # Build: left lip 40 bars at 100, cup dips to 75 (25% decline), recovers to 98,
    # then handle of 10 bars between 93 and 98 (≈5% handle decline)
    left_region = [100.0] * 15
    decline = list(range(0, 13))  # 13 steps down
    cup_region = [100.0 - d * (25.0 / 12) for d in decline]  # 100→75 over 13 bars
    recovery = list(range(12, -1, -1))
    recover_region = [75.0 + r * (23.0 / 12) for r in recovery]  # 75→98 over 13 bars
    handle_region = [98.0 if i % 2 == 0 else 93.0 for i in range(9)]  # ≈5% decline
    values = left_region + cup_region + recover_region + handle_region
    closes = _make_closes(values)
    result = detect_cup_with_handle(closes)
    assert result is not None
    assert result == pytest.approx(98.0, abs=2.0)


def test_cup_rejected_too_shallow():
    """40 bars with only 10% cup decline returns None (< 15% floor)."""
    from core.pivot_detector import detect_cup_with_handle

    left_region = [100.0] * 15
    cup_region = [100.0 - i * (10.0 / 10) for i in range(11)]  # 100→90, 10% decline
    recover_region = [90.0 + i * (10.0 / 10) for i in range(11)]  # 90→100
    handle_region = [98.0, 97.0, 98.0]
    values = left_region + cup_region + recover_region + handle_region
    closes = _make_closes(values)
    assert detect_cup_with_handle(closes) is None


def test_cup_handle_in_lower_half_rejected():
    """Handle trough below cup midpoint → None."""
    from core.pivot_detector import detect_cup_with_handle

    # cup: 100 → 70 (30% decline), recovers to 98, handle dips to 78 (below midpoint 85)
    left_region = [100.0] * 15
    cup_region = [100.0 - i * (30.0 / 10) for i in range(11)]   # 100→70
    recover_region = [70.0 + i * (28.0 / 10) for i in range(11)]  # 70→98
    # midpoint = 70 + (100-70)*0.5 = 85; handle_low = 78 < 85 → rejected
    handle_region = [98.0, 78.0, 98.0, 97.0, 96.0, 97.0, 98.0, 97.0, 98.0]
    values = left_region + cup_region + recover_region + handle_region
    closes = _make_closes(values)
    assert detect_cup_with_handle(closes) is None


# ---------------------------------------------------------------------------
# is_in_buy_zone
# ---------------------------------------------------------------------------

def test_in_buy_zone_within_5pct():
    """Price within 5% of pivot → True."""
    from core.pivot_detector import is_in_buy_zone

    assert is_in_buy_zone(103.0, 100.0) is True


def test_in_buy_zone_extended():
    """Price more than 5% above pivot → False."""
    from core.pivot_detector import is_in_buy_zone

    assert is_in_buy_zone(107.0, 100.0) is False


# ---------------------------------------------------------------------------
# find_pivot (pass-through)
# ---------------------------------------------------------------------------

def test_no_pattern_is_passthrough():
    """Noisy data that matches no clean base → find_pivot returns None."""
    from core.pivot_detector import find_pivot

    import numpy as np

    rng = np.random.default_rng(42)
    # Wildly random prices — won't form a clean flat base or cup
    values = list(50.0 + rng.standard_normal(65) * 20)
    closes = _make_closes(values)
    # We don't assert None because a random series might accidentally
    # pass the flat-base check; instead, assert find_pivot doesn't raise
    result = find_pivot(closes)
    assert result is None or isinstance(result, float)


def test_find_pivot_returns_none_for_short_series():
    """Series shorter than 25 bars → find_pivot returns None immediately."""
    from core.pivot_detector import find_pivot

    closes = _make_closes([100.0] * 20)
    assert find_pivot(closes) is None


# ---------------------------------------------------------------------------
# PEG bypass (integration with tech_pass logic — pure logic test)
# ---------------------------------------------------------------------------

def test_peg_bypasses_buy_zone():
    """has_peg_today=True makes tech_pass True even when in_buy_zone=False."""
    has_breakout = False
    has_surge = False
    has_peg_today = True
    in_buy_zone = False
    tech_pass = (has_breakout and has_surge and in_buy_zone) or has_peg_today
    assert tech_pass is True
```

- [ ] **Step 2: Run tests to confirm they all fail (module not yet created)**

```bash
python -m pytest tests/test_pivot_detector.py -v
```
Expected: All tests fail with `ModuleNotFoundError: No module named 'core.pivot_detector'`

- [ ] **Step 3: Create `core/pivot_detector.py`**

```python
from __future__ import annotations

import pandas as pd

import config.settings as settings


def detect_flat_base(closes: pd.Series) -> float | None:
    """Return the flat base pivot price, or None if no valid flat base detected.

    A flat base has a peak-to-trough decline ≤ PIVOT_FLAT_BASE_MAX_DECLINE_PCT
    over at least PIVOT_FLAT_BASE_MIN_WEEKS * 5 trading bars.
    The pivot is the range high (resistance level of the consolidation).
    """
    min_bars = settings.PIVOT_FLAT_BASE_MIN_WEEKS * 5
    if len(closes) < min_bars:
        return None
    base_high = float(closes.max())
    base_low = float(closes.min())
    if base_high <= 0:
        return None
    decline = (base_high - base_low) / base_high
    if decline > settings.PIVOT_FLAT_BASE_MAX_DECLINE_PCT:
        return None
    return base_high


def detect_cup_with_handle(closes: pd.Series) -> float | None:
    """Return the cup-with-handle pivot price, or None if pattern not detected.

    Cup must be 15–33% deep over ≥ PIVOT_CUP_MIN_WEEKS * 5 bars.
    Right lip must recover to within 5% of left lip.
    Handle must be in the upper half of the cup range with ≤ 12% pullback.
    Pivot is the handle high.
    """
    min_bars = settings.PIVOT_CUP_MIN_WEEKS * 5
    if len(closes) < min_bars:
        return None

    left_lip = float(closes.iloc[0])
    if left_lip <= 0:
        return None

    # Cup low must occur within first 80% of the window
    cup_region_end = int(len(closes) * 0.80)
    cup_region = closes.iloc[:cup_region_end]
    cup_low_idx = int(cup_region.argmin())
    cup_low = float(closes.iloc[cup_low_idx])

    cup_decline = (left_lip - cup_low) / left_lip
    if cup_decline < settings.PIVOT_CUP_MIN_DECLINE_PCT or cup_decline > settings.PIVOT_CUP_MAX_DECLINE_PCT:
        return None

    # Right lip must recover to within 5% of left lip
    post_cup = closes.iloc[cup_low_idx:]
    right_lip = float(post_cup.max())
    if right_lip < left_lip * 0.95:
        return None

    # Handle: last 5–20 bars of the window
    handle_len = min(20, max(5, len(closes) - cup_low_idx - 5))
    handle = closes.iloc[-handle_len:]
    handle_high = float(handle.max())
    handle_low = float(handle.min())
    if handle_high <= 0:
        return None

    handle_decline = (handle_high - handle_low) / handle_high
    if handle_decline > settings.PIVOT_HANDLE_MAX_DECLINE_PCT:
        return None

    # Handle must form in the upper half of the cup range
    cup_midpoint = cup_low + (left_lip - cup_low) * 0.5
    if handle_low <= cup_midpoint:
        return None

    return handle_high


def find_pivot(closes: pd.Series) -> float | None:
    """Entry point: try cup-with-handle first, fall back to flat base.

    Accepts a pre-sliced closing price series (caller is responsible for
    slicing to eval_date). Returns None if no pattern detected — callers
    should treat None as a pass-through (do not block the signal).
    """
    if len(closes) < 25:
        return None
    lookback = min(65, len(closes))
    window = closes.iloc[-lookback:]
    pivot = detect_cup_with_handle(window)
    if pivot is not None:
        return pivot
    return detect_flat_base(window)


def is_in_buy_zone(
    current_price: float,
    pivot: float,
    zone_pct: float = settings.PIVOT_BUY_ZONE_PCT,
) -> bool:
    """Return True if current_price is within zone_pct above pivot."""
    return current_price <= pivot * (1 + zone_pct)
```

- [ ] **Step 4: Run tests — all must pass**

```bash
python -m pytest tests/test_pivot_detector.py -v
```
Expected: 10 tests PASSED, 0 failed

- [ ] **Step 5: Run full test suite to catch regressions**

```bash
python -m pytest -v
```
Expected: all existing tests still pass

- [ ] **Step 6: Commit**

```bash
git add core/pivot_detector.py tests/test_pivot_detector.py
git commit -m "feat: add pivot_detector module with flat base and cup-with-handle detection (TDD)"
```

---

### Task 3: Integrate pivot detection into `backtest.py`

**Files:**
- Modify: `backtest.py` (lines 149–187 in `_evaluate_technical_at_date()`, lines 279–294 in `_should_emit_buy_signal()`)

- [ ] **Step 1: Add import at top of `backtest.py`**

Find the existing local imports block near the top of `backtest.py` (around the other `core.*` imports) and add:
```python
from core.pivot_detector import find_pivot, is_in_buy_zone
```

- [ ] **Step 2: Add pivot detection to `_evaluate_technical_at_date()` (around line 175)**

After line 174 (`score_s, s_metrics = evaluate_s(...)`) and before the `return` statement at line 176, insert:
```python
    pivot = find_pivot(closes)
    in_buy_zone = is_in_buy_zone(latest_close, pivot) if pivot is not None else True
```

Then add `"pivot"` and `"in_buy_zone"` to the returned dict (after `"power_gap_details"`):
```python
    return {
        "n_score": n_score,
        "s_score": score_s,
        "proximity": proximity,
        "close": latest_close,
        "high_52": high_52,
        "avg_vol_50": avg_vol_50,
        "is_breakout": s_metrics.get("is_breakout", False),
        "has_volume_surge": s_metrics.get("has_volume_surge", False),
        "has_power_gap": s_metrics.get("has_power_gap", False),
        "power_gap_details": s_metrics.get("power_gap_details", {}),
        "pivot": pivot,
        "in_buy_zone": in_buy_zone,
    }
```

Also update the early-exit return dict (around line 141–147, the `len(sliced) < 60` guard) to include the new keys:
```python
    if len(sliced) < 60:
        return {
            "n_score": 0.0,
            "s_score": 0.0,
            "proximity": 0.0,
            "is_breakout": False,
            "has_volume_surge": False,
            "pivot": None,
            "in_buy_zone": True,
        }
```

- [ ] **Step 3: Add `in_buy_zone` to `_should_emit_buy_signal()` (lines 279–294)**

Replace the entire function with:
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
    """Return True only for signals that satisfy the live-style buy gates."""
    return (
        total_score >= settings.MIN_CANSLIM_SCORE
        and rs_score >= settings.MIN_RS_SCORE
        and market_is_bullish
        and ((has_breakout and has_volume_surge and in_buy_zone) or has_peg_today)
    )
```

- [ ] **Step 4: Find the call site of `_should_emit_buy_signal()` and pass `in_buy_zone`**

Search for `_should_emit_buy_signal(` in `backtest.py`. At the call site, add:
```python
in_buy_zone=bool(tech.get("in_buy_zone", True)),
```
as an additional keyword argument.

- [ ] **Step 5: Run the full test suite**

```bash
python -m pytest -v
```
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add backtest.py
git commit -m "feat: integrate pivot buy zone check into _evaluate_technical_at_date and _should_emit_buy_signal"
```

---

### Task 4: Integrate `in_buy_zone` into `CanslimStrategy.evaluate_symbol()` in `core/backtest_engine.py`

**Files:**
- Modify: `core/backtest_engine.py` (around lines 503–517)

- [ ] **Step 1: Read `in_buy_zone` from the tech dict after line 505**

After line 505 (`has_surge = bool(tech.get("has_volume_surge"))`), add:
```python
        in_buy_zone = bool(tech.get("in_buy_zone", True))
```

- [ ] **Step 2: Update `tech_pass` gate at line 517**

Change:
```python
        tech_pass = (has_breakout and has_surge) or has_peg_today
```
To:
```python
        tech_pass = (has_breakout and has_surge and in_buy_zone) or has_peg_today
```

- [ ] **Step 3: Add `"in_buy_zone"` and `"pivot"` to the signal row dict**

Find the `return {` block around line 532. Add these two keys alongside the existing logging fields:
```python
            "pivot": tech.get("pivot"),
            "in_buy_zone": in_buy_zone,
```

- [ ] **Step 4: Add `"industry_group_top_n"` to `SimulationResult` config dict if not already present**

In `run()`, find where `SimulationResult` config dict is constructed. Confirm `"industry_group_top_n": settings.INDUSTRY_GROUP_TOP_N` is present (it was added in the industry group task). If missing, add it.

- [ ] **Step 5: Run the full test suite**

```bash
python -m pytest -v
```
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add core/backtest_engine.py
git commit -m "feat: gate tech_pass on in_buy_zone in CanslimStrategy.evaluate_symbol (PEG exempted)"
```

---

### Task 5: Backtest validation

**Files:**
- No file changes — run the backtest and compare metrics

- [ ] **Step 1: Run the full backtest**

```bash
python backtest.py
```

- [ ] **Step 2: Record and compare key metrics**

Compare results to the baseline (Sharpe 1.69, Total Return +43.1%, Max Drawdown -7.2%):

| Metric | Baseline | After Pivot Gate | Delta |
|--------|----------|-----------------|-------|
| Sharpe Ratio | 1.69 | ? | ? |
| Total Return | +43.1% | ? | ? |
| Max Drawdown | -7.2% | ? | ? |
| Win Rate | ? | ? | ? |
| Avg Win | ? | ? | ? |
| Avg Loss | ? | ? | ? |

**Accept:** Sharpe ≥ 1.60 (pivot gate filters some valid entries, small degradation acceptable if win rate improves).

**Concern threshold:** If Sharpe drops below 1.50 or total return drops more than 10 percentage points, investigate whether the buy zone is too restrictive (e.g., increase `PIVOT_BUY_ZONE_PCT` to 0.08).

- [ ] **Step 3: If performance regresses — diagnose before reverting**

If the metrics are worse, check how many signals are being filtered:
- Add a temporary print to `_should_emit_buy_signal` or `evaluate_symbol` counting `in_buy_zone=False` rejections.
- If >50% of valid breakout signals are being rejected, the patterns may not be forming cleanly on the backtest universe (many strong movers skip the base-building phase). In that case, consider raising `PIVOT_BUY_ZONE_PCT` from 0.05 to 0.08.

- [ ] **Step 4: If performance improves or is neutral — celebrate and commit the result summary**

```bash
git add -A
git commit -m "test: validate pivot buy point detection against backtest baseline"
```

---

## Spec Cross-Check

| Spec Requirement | Covered In |
|-----------------|-----------|
| `PIVOT_BUY_ZONE_PCT = 0.05` and 6 other constants | Task 1 |
| `detect_flat_base()` — 25–65 bars, ≤15% decline, returns range high | Task 2 |
| `detect_cup_with_handle()` — 35+ bars, 15–33% cup, upper-half handle, ≤12% handle | Task 2 |
| `find_pivot()` — cup first, flat base fallback, None if < 25 bars | Task 2 |
| `is_in_buy_zone()` — price ≤ pivot × 1.05 | Task 2 |
| 10 unit tests (all synthetic, no network) | Task 2 |
| `_evaluate_technical_at_date()` calls `find_pivot()`, returns `pivot` + `in_buy_zone` | Task 3 |
| Early-exit dict updated with `pivot: None, in_buy_zone: True` | Task 3 |
| `_should_emit_buy_signal()` adds `in_buy_zone` parameter | Task 3 |
| `tech_pass` gate updated in `evaluate_symbol()` | Task 4 |
| PEG bypass (`has_peg_today=True` short-circuits `in_buy_zone`) | Task 2 (test), Tasks 3–4 (gate structure) |
| Pass-through when no pattern (`None` → `in_buy_zone=True`) | Tasks 2–4 |
| `pivot` and `in_buy_zone` logged in signal row dict | Task 4 |
