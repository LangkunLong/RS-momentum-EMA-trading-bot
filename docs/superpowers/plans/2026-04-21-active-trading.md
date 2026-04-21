# Active Trading: Scale-Out, Eviction, and 8-Week Hold Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single 40% take-profit with 3-tier staged exits, add two-pass eviction for portfolio rotation, and implement the O'Neil 8-week hold rule to protect super-winners.

**Architecture:** All logic lives in `core/backtest_engine.py`'s `PortfolioSimulator`. Two new `Trade` fields (`scale_out_tier`, `eight_week_hold`) drive the new exit logic. A new `_try_evict()` method handles portfolio rotation. Settings constants make thresholds configurable without code changes.

**Tech Stack:** Python 3.11+, pandas, pytest, existing `core/backtest_engine.py` and `config/settings.py` patterns.

---

## File Map

| File | What changes |
|------|-------------|
| `config/settings.py` | Add `SCALE_OUT_TIERS`, `ENABLE_EVICTION` |
| `core/backtest_engine.py` | Add `Trade` fields; refactor `_scale_out_trade`; replace single exit with tier loop; add 8-week hold; add `_try_evict`; wire `enable_eviction` into `__init__` and `_enter_position` |
| `tests/test_backtest_engine.py` | Update `test_take_profit_scale_out_records_partial_sell`; add new tier, hold, and eviction tests |

---

## Task 1: Add settings constants

**Files:**
- Modify: `config/settings.py` (ORDER EXECUTION SETTINGS section, after `BUY_ZONE_UNDERCUT_TOLERANCE_PCT`)

- [ ] **Step 1: Add constants to settings.py**

Open `config/settings.py`. After the `BUY_ZONE_UNDERCUT_TOLERANCE_PCT` line (currently the last line of ORDER EXECUTION SETTINGS), add:

```python
# Staged scale-out tiers — (gain_target_pct, fraction_of_original_qty_to_sell)
# Remaining 25% after tier 3 is held on EMA trailing stop.
SCALE_OUT_TIERS: list[tuple[float, float]] = [
    (0.10, 0.25),
    (0.15, 0.25),
    (0.20, 0.25),
]

# Two-pass eviction: when portfolio is full, compare new signal RS vs open positions.
ENABLE_EVICTION: bool = True
```

- [ ] **Step 2: Verify settings load cleanly**

```bash
python -c "from config import settings; print(settings.SCALE_OUT_TIERS); print(settings.ENABLE_EVICTION)"
```

Expected output:
```
[(0.1, 0.25), (0.15, 0.25), (0.2, 0.25)]
True
```

- [ ] **Step 3: Commit**

```bash
git add config/settings.py
git commit -m "Add SCALE_OUT_TIERS and ENABLE_EVICTION to settings"
```

---

## Task 2: Add new fields to `Trade` dataclass

**Files:**
- Modify: `core/backtest_engine.py` lines 126–147 (`Trade` dataclass)

The `Trade` dataclass already has `rs_score: float = 0.0` (needed for eviction). We only need two new fields.

- [ ] **Step 1: Add fields to `Trade`**

In `core/backtest_engine.py`, find the `Trade` dataclass. After `ema_trailing_active: bool = False`, add:

```python
    scale_out_tier: int = 0
    eight_week_hold: bool = False
```

The full dataclass body should end with:

```python
    days_held: int = 0
    breakeven_armed: bool = False
    ema_trailing_active: bool = False
    scale_out_tier: int = 0
    eight_week_hold: bool = False
```

- [ ] **Step 2: Verify no import errors**

```bash
python -c "from core.backtest_engine import Trade; t = Trade('X','2026-01-01',100.0,10.0,92.0); print(t.scale_out_tier, t.eight_week_hold)"
```

Expected: `0 False`

- [ ] **Step 3: Run full test suite to confirm no regressions**

```bash
python -m pytest tests/test_backtest_engine.py tests/test_backtest_pnl.py -v
```

Expected: all currently-passing tests still pass.

- [ ] **Step 4: Commit**

```bash
git add core/backtest_engine.py
git commit -m "Add scale_out_tier and eight_week_hold fields to Trade dataclass"
```

---

## Task 3: Refactor `_scale_out_trade` to accept explicit quantity

**Files:**
- Modify: `core/backtest_engine.py` — `_scale_out_trade` method (lines 913–932)

The current method computes `scale_qty = trade.remaining_qty * self.scale_out_fraction`. The new tier logic passes an explicit qty per tier. We make `sell_qty` an optional parameter so existing callers still work.

- [ ] **Step 1: Update `_scale_out_trade` signature and body**

Find `_scale_out_trade` in `core/backtest_engine.py`. Replace the full method:

```python
    def _scale_out_trade(
        self,
        symbol: str,
        exit_price: float,
        date_str: str,
        reason: str,
        sell_qty: Optional[float] = None,
    ) -> None:
        trade = self._open_positions.get(symbol)
        if trade is None or not trade.remaining_qty:
            return

        scale_qty = sell_qty if sell_qty is not None else trade.remaining_qty * self.scale_out_fraction
        if scale_qty <= 0:
            return
        trade.remaining_qty = (trade.remaining_qty or 0.0) - scale_qty
        trade.scaled_out_qty += scale_qty
        trade.scale_out_price = exit_price
        proceeds = exit_price * scale_qty
        trade.realized_pnl += (exit_price - trade.entry_price) * scale_qty
        self._equity += proceeds
        self._record_transaction(
            date=date_str,
            ticker=symbol,
            action="SELL",
            price=exit_price,
            quantity=scale_qty,
            reason=reason,
        )
```

- [ ] **Step 2: Run tests to confirm no regressions**

```bash
python -m pytest tests/test_backtest_engine.py tests/test_backtest_pnl.py -v
```

Expected: all currently-passing tests still pass.

- [ ] **Step 3: Commit**

```bash
git add core/backtest_engine.py
git commit -m "Make _scale_out_trade accept explicit sell_qty for tier-based exits"
```

---

## Task 4: Write failing tests for tiered scale-out

**Files:**
- Modify: `tests/test_backtest_engine.py`

- [ ] **Step 1: Update existing scale-out test to reflect new 3-tier behavior**

Find `test_take_profit_scale_out_records_partial_sell` in `tests/test_backtest_engine.py`. Replace it entirely:

```python
def test_take_profit_scale_out_fires_all_three_tiers_on_gap_up() -> None:
    """When high clears all 3 tier thresholds in one bar, all 3 tiers fire."""
    sim = PortfolioSimulator(initial_capital=100_000.0, stagnation_days=999)
    trade = Trade(symbol="NVDA", entry_date="2026-04-01", entry_price=100.0, qty=10.0, stop_price=92.0)
    sim._open_positions["NVDA"] = trade

    # high=121 clears tier1(110), tier2(115), tier3(120)
    ohlcv = _make_ohlcv(n=5, close_value=121.0, high_value=121.0, low_value=109.0)
    sim._check_exits("NVDA", ohlcv, ohlcv.index[-1])

    assert "NVDA" in sim._open_positions
    result = sim._open_positions["NVDA"]
    assert result.scale_out_tier == 3
    assert result.remaining_qty == pytest.approx(2.5)  # 25% of 10 remains
    # pnl: (110-100)*2.5 + (115-100)*2.5 + (120-100)*2.5 = 25+37.5+50 = 112.5
    assert result.realized_pnl == pytest.approx(112.5)
    sell_reasons = [tx["Reason"] for tx in sim._transactions if tx["Action"] == "SELL"]
    assert sell_reasons.count("take_profit_scale_out") == 3
```

- [ ] **Step 2: Add test — only tier 1 fires when gain is between 10% and 15%**

Add after the updated test above:

```python
def test_scale_out_tier1_only_when_gain_between_10_and_15_pct() -> None:
    """Only tier 1 fires when high is between 10% and 15% above entry."""
    sim = PortfolioSimulator(initial_capital=100_000.0, stagnation_days=999)
    trade = Trade(symbol="AAPL", entry_date="2026-04-01", entry_price=100.0, qty=8.0, stop_price=92.0)
    sim._open_positions["AAPL"] = trade

    # high=112 clears tier1(110) but NOT tier2(115)
    ohlcv = _make_ohlcv(n=3, close_value=112.0, high_value=112.0, low_value=109.0)
    sim._check_exits("AAPL", ohlcv, ohlcv.index[-1])

    assert "AAPL" in sim._open_positions
    result = sim._open_positions["AAPL"]
    assert result.scale_out_tier == 1
    assert result.remaining_qty == pytest.approx(6.0)  # sold 25% of 8 = 2 shares
    assert result.realized_pnl == pytest.approx(20.0)  # (110-100)*2
    sell_txns = [tx for tx in sim._transactions if tx["Action"] == "SELL"]
    assert len(sell_txns) == 1
```

- [ ] **Step 3: Add test — remaining_qty is 25% of original after all 3 tiers**

```python
def test_scale_out_remaining_qty_is_25_pct_of_original_after_tier3() -> None:
    """After all 3 tiers, exactly 25% of original qty remains."""
    sim = PortfolioSimulator(initial_capital=100_000.0, stagnation_days=999)
    qty = 20.0
    trade = Trade(symbol="MSFT", entry_date="2026-04-01", entry_price=50.0, qty=qty, stop_price=46.0)
    sim._open_positions["MSFT"] = trade

    ohlcv = _make_ohlcv(n=3, close_value=62.0, high_value=62.0, low_value=51.0)
    sim._check_exits("MSFT", ohlcv, ohlcv.index[-1])

    result = sim._open_positions["MSFT"]
    assert result.scale_out_tier == 3
    assert result.remaining_qty == pytest.approx(qty * 0.25)
```

- [ ] **Step 4: Run tests to confirm they fail (not yet implemented)**

```bash
python -m pytest tests/test_backtest_engine.py::test_take_profit_scale_out_fires_all_three_tiers_on_gap_up tests/test_backtest_engine.py::test_scale_out_tier1_only_when_gain_between_10_and_15_pct tests/test_backtest_engine.py::test_scale_out_remaining_qty_is_25_pct_of_original_after_tier3 -v
```

Expected: FAIL (old single-tier logic still in place).

---

## Task 5: Implement tiered scale-out in `_check_exits`

**Files:**
- Modify: `core/backtest_engine.py` — `_check_exits` method (lines 833–888)

- [ ] **Step 1: Replace the single take-profit block with the tier while-loop**

In `_check_exits`, find and replace the entire single-exit block:

```python
        target_price = trade.entry_price * (1 + self.take_profit_pct)
        if (
            trade.scale_out_price is None
            and trade.remaining_qty
            and trade.remaining_qty > 0
            and high >= target_price
        ):
            self._scale_out_trade(symbol, target_price, date_str, "take_profit_scale_out")
            trade = self._open_positions.get(symbol)
            if trade is None:
                return
```

Replace with:

```python
        if not trade.eight_week_hold and (trade.remaining_qty or 0.0) > 0:
            tiers = settings.SCALE_OUT_TIERS
            while trade.scale_out_tier < len(tiers):
                gain_target, fraction = tiers[trade.scale_out_tier]
                tier_price = trade.entry_price * (1 + gain_target)
                if high < tier_price:
                    break
                sell_qty = trade.qty * fraction
                if sell_qty > 0 and (trade.remaining_qty or 0.0) >= sell_qty:
                    self._scale_out_trade(symbol, tier_price, date_str, "take_profit_scale_out", sell_qty=sell_qty)
                trade.scale_out_tier += 1
                trade = self._open_positions.get(symbol)
                if trade is None:
                    return
```

- [ ] **Step 2: Run the three new tier tests**

```bash
python -m pytest tests/test_backtest_engine.py::test_take_profit_scale_out_fires_all_three_tiers_on_gap_up tests/test_backtest_engine.py::test_scale_out_tier1_only_when_gain_between_10_and_15_pct tests/test_backtest_engine.py::test_scale_out_remaining_qty_is_25_pct_of_original_after_tier3 -v
```

Expected: all 3 PASS.

- [ ] **Step 3: Run full test suite**

```bash
python -m pytest tests/test_backtest_engine.py tests/test_backtest_pnl.py -v
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add core/backtest_engine.py tests/test_backtest_engine.py
git commit -m "Replace single take-profit with 3-tier staged scale-out"
```

---

## Task 6: Write failing tests for 8-week hold rule

**Files:**
- Modify: `tests/test_backtest_engine.py`

- [ ] **Step 1: Add test — 20% gain on day 14 triggers hold, suppresses tiers**

Add to `tests/test_backtest_engine.py`:

```python
def test_eight_week_hold_triggered_by_20pct_gain_in_3_weeks() -> None:
    """20%+ gain within 15 trading days sets eight_week_hold=True and suppresses tier exits."""
    sim = PortfolioSimulator(initial_capital=100_000.0, stagnation_days=999)
    trade = Trade(symbol="CRWD", entry_date="2026-01-01", entry_price=100.0, qty=10.0, stop_price=92.0)
    trade.days_held = 13  # day 14 after increment
    sim._open_positions["CRWD"] = trade

    # close=122 → 22% gain, within 15-day window → should trigger hold
    ohlcv = _make_ohlcv(n=20, close_value=122.0, high_value=122.0, low_value=109.0)
    sim._check_exits("CRWD", ohlcv, ohlcv.index[-1])

    result = sim._open_positions["CRWD"]
    assert result.eight_week_hold is True
    assert result.scale_out_tier == 0  # no tiers fired
    assert result.remaining_qty == pytest.approx(10.0)  # nothing sold
```

- [ ] **Step 2: Add test — gain on day 16 does NOT trigger hold**

```python
def test_eight_week_hold_not_triggered_after_3_week_window() -> None:
    """20%+ gain after 15 trading days does NOT trigger the 8-week hold."""
    sim = PortfolioSimulator(initial_capital=100_000.0, stagnation_days=999)
    trade = Trade(symbol="NVDA", entry_date="2026-01-01", entry_price=100.0, qty=10.0, stop_price=92.0)
    trade.days_held = 15  # day 16 after increment
    sim._open_positions["NVDA"] = trade

    # close=122 → 22% gain, but day 16 is outside window
    ohlcv = _make_ohlcv(n=20, close_value=122.0, high_value=122.0, low_value=109.0)
    sim._check_exits("NVDA", ohlcv, ohlcv.index[-1])

    result = sim._open_positions["NVDA"]
    assert result.eight_week_hold is False
    assert result.scale_out_tier == 3  # all 3 tiers fired normally
```

- [ ] **Step 3: Add test — hold releases after 40 bars, tiers resume**

```python
def test_eight_week_hold_releases_after_40_bars_and_tiers_resume() -> None:
    """Hold expires on bar 40; scale_out_tier resets so tiers can fire."""
    sim = PortfolioSimulator(initial_capital=100_000.0, stagnation_days=999)
    trade = Trade(symbol="MU", entry_date="2026-01-01", entry_price=100.0, qty=10.0, stop_price=92.0)
    trade.days_held = 39  # will become 40 after increment
    trade.eight_week_hold = True
    trade.scale_out_tier = 0
    sim._open_positions["MU"] = trade

    # price at 25% gain — would fire tiers once hold releases
    ohlcv = _make_ohlcv(n=45, close_value=125.0, high_value=125.0, low_value=109.0)
    sim._check_exits("MU", ohlcv, ohlcv.index[-1])

    result = sim._open_positions["MU"]
    assert result.eight_week_hold is False
    assert result.scale_out_tier == 3  # all 3 tiers fired after release
    assert result.remaining_qty == pytest.approx(2.5)
```

- [ ] **Step 4: Add test — hard stop fires even during 8-week hold**

```python
def test_stop_loss_fires_during_eight_week_hold() -> None:
    """Hard stop-loss is NEVER suppressed by the 8-week hold."""
    sim = PortfolioSimulator(initial_capital=100_000.0, stagnation_days=999)
    trade = Trade(symbol="VST", entry_date="2026-01-01", entry_price=100.0, qty=10.0, stop_price=92.0)
    trade.days_held = 5
    trade.eight_week_hold = True
    sim._open_positions["VST"] = trade

    # low drops below stop
    ohlcv = _make_ohlcv(n=10, close_value=90.0, high_value=91.0, low_value=89.0)
    sim._check_exits("VST", ohlcv, ohlcv.index[-1])

    assert "VST" not in sim._open_positions
    assert sim._trades[-1].exit_reason == "stop_loss"
```

- [ ] **Step 5: Run all four new tests to confirm they fail**

```bash
python -m pytest tests/test_backtest_engine.py::test_eight_week_hold_triggered_by_20pct_gain_in_3_weeks tests/test_backtest_engine.py::test_eight_week_hold_not_triggered_after_3_week_window tests/test_backtest_engine.py::test_eight_week_hold_releases_after_40_bars_and_tiers_resume tests/test_backtest_engine.py::test_stop_loss_fires_during_eight_week_hold -v
```

Expected: FAIL (8-week logic not yet implemented).

---

## Task 7: Implement 8-week hold rule in `_check_exits`

**Files:**
- Modify: `core/backtest_engine.py` — `_check_exits` method

- [ ] **Step 1: Add hold detection and release logic**

In `_check_exits`, find the line `trade.days_held += 1` and the line that updates `trade.peak_close`. After both of those lines, and BEFORE the stop-loss check, add:

```python
        gain_pct = (close - trade.entry_price) / trade.entry_price if trade.entry_price > 0 else 0.0

        # Release 8-week hold after 40 trading days
        if trade.eight_week_hold and trade.days_held >= 40:
            trade.eight_week_hold = False
            trade.scale_out_tier = 0

        # Detect super-winner: 20%+ gain within first 3 weeks (15 trading days)
        if not trade.eight_week_hold and trade.days_held <= 15 and gain_pct >= 0.20:
            trade.eight_week_hold = True
```

The `_check_exits` method body after the additions should look like this (showing relevant section):

```python
        trade.days_held += 1
        trade.peak_close = max(trade.peak_close or trade.entry_price, close)

        gain_pct = (close - trade.entry_price) / trade.entry_price if trade.entry_price > 0 else 0.0

        # Release 8-week hold after 40 trading days
        if trade.eight_week_hold and trade.days_held >= 40:
            trade.eight_week_hold = False
            trade.scale_out_tier = 0

        # Detect super-winner: 20%+ gain within first 3 weeks (15 trading days)
        if not trade.eight_week_hold and trade.days_held <= 15 and gain_pct >= 0.20:
            trade.eight_week_hold = True

        if low <= trade.stop_price:
            self._close_trade(symbol, trade.stop_price, "stop_loss", date_str)
            return

        if not trade.eight_week_hold and (trade.remaining_qty or 0.0) > 0:
            tiers = settings.SCALE_OUT_TIERS
            while trade.scale_out_tier < len(tiers):
                gain_target, fraction = tiers[trade.scale_out_tier]
                tier_price = trade.entry_price * (1 + gain_target)
                if high < tier_price:
                    break
                sell_qty = trade.qty * fraction
                if sell_qty > 0 and (trade.remaining_qty or 0.0) >= sell_qty:
                    self._scale_out_trade(symbol, tier_price, date_str, "take_profit_scale_out", sell_qty=sell_qty)
                trade.scale_out_tier += 1
                trade = self._open_positions.get(symbol)
                if trade is None:
                    return
```

- [ ] **Step 2: Run the four 8-week hold tests**

```bash
python -m pytest tests/test_backtest_engine.py::test_eight_week_hold_triggered_by_20pct_gain_in_3_weeks tests/test_backtest_engine.py::test_eight_week_hold_not_triggered_after_3_week_window tests/test_backtest_engine.py::test_eight_week_hold_releases_after_40_bars_and_tiers_resume tests/test_backtest_engine.py::test_stop_loss_fires_during_eight_week_hold -v
```

Expected: all 4 PASS.

- [ ] **Step 3: Run full test suite**

```bash
python -m pytest tests/test_backtest_engine.py tests/test_backtest_pnl.py -v
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add core/backtest_engine.py tests/test_backtest_engine.py
git commit -m "Implement 8-week hold rule: suppress tiers for 20%+ gain in first 3 weeks"
```

---

## Task 8: Write failing tests for two-pass eviction

**Files:**
- Modify: `tests/test_backtest_engine.py`

The eviction tests need to call `_enter_position` when the portfolio is full and verify which position gets evicted.

- [ ] **Step 1: Add helper to build a full simulator with open positions**

Add this helper near the top of `tests/test_backtest_engine.py` (after existing helpers):

```python
def _make_full_sim(
    positions: dict[str, tuple[float, float, float]],  # symbol → (entry_price, rs_score, current_close)
    capital: float = 100_000.0,
) -> tuple[PortfolioSimulator, dict[str, pd.DataFrame]]:
    """Build a simulator with the given open positions and matching OHLCV data."""
    sim = PortfolioSimulator(initial_capital=capital, stagnation_days=999)
    ohlcv_map: dict[str, pd.DataFrame] = {}
    for sym, (entry_px, rs, _close) in positions.items():
        trade = Trade(symbol=sym, entry_date="2026-01-01", entry_price=entry_px, qty=10.0, stop_price=entry_px * 0.92)
        trade.rs_score = rs
        sim._open_positions[sym] = trade
        ohlcv_map[sym] = _make_ohlcv(n=10, close_value=_close)
    return sim, ohlcv_map
```

- [ ] **Step 2: Add test — pass 1 evicts underwater loser, not profitable position**

```python
def test_eviction_pass1_evicts_underwater_lower_rs_position() -> None:
    """Pass 1: evicts an underwater position with lower RS than new signal."""
    positions = {
        "AAPL": (100.0, 85.0, 105.0),   # profitable, rs=85
        "MSFT": (100.0, 70.0, 95.0),    # underwater (close < entry), rs=70
        "NVDA": (100.0, 88.0, 110.0),   # profitable, rs=88
        "CRWD": (100.0, 72.0, 98.0),    # underwater, rs=72
        "MU":   (100.0, 90.0, 115.0),   # profitable, rs=90
    }
    sim, ohlcv_map = _make_full_sim(positions)
    assert len(sim._open_positions) == 5

    new_signal = {"symbol": "GEV", "rs_score": 80.0, "canslim_score": 75.0, "signal_reason": "Volume Breakout"}
    ohlcv_map["GEV"] = _make_ohlcv(n=10, close_value=50.0)
    entry_date = ohlcv_map["GEV"].index[-1]

    sim._enter_position(new_signal, ohlcv_map, entry_date)

    # MSFT (rs=70, underwater) evicted; CRWD (rs=72, underwater) kept because MSFT has lower RS
    assert "MSFT" not in sim._open_positions
    assert "GEV" in sim._open_positions
    assert sim._trades[-2].exit_reason == "evicted"  # MSFT evicted first
```

- [ ] **Step 3: Add test — pass 2 fires when no underwater losers qualify**

```python
def test_eviction_pass2_evicts_lowest_rs_when_no_underwater_positions() -> None:
    """Pass 2: when no underwater positions qualify, evicts lowest RS (any position)."""
    positions = {
        "AAPL": (100.0, 85.0, 110.0),  # profitable, rs=85
        "MSFT": (100.0, 70.0, 112.0),  # profitable, rs=70 ← lowest, should be evicted
        "NVDA": (100.0, 88.0, 115.0),  # profitable, rs=88
        "CRWD": (100.0, 78.0, 105.0),  # profitable, rs=78
        "MU":   (100.0, 90.0, 120.0),  # profitable, rs=90
    }
    sim, ohlcv_map = _make_full_sim(positions)

    new_signal = {"symbol": "VRT", "rs_score": 80.0, "canslim_score": 75.0, "signal_reason": "Volume Breakout"}
    ohlcv_map["VRT"] = _make_ohlcv(n=10, close_value=60.0)
    entry_date = ohlcv_map["VRT"].index[-1]

    sim._enter_position(new_signal, ohlcv_map, entry_date)

    assert "MSFT" not in sim._open_positions
    assert "VRT" in sim._open_positions
    evicted = next(t for t in sim._trades if t.symbol == "MSFT")
    assert evicted.exit_reason == "evicted"
```

- [ ] **Step 4: Add test — no eviction when new signal RS is lower than all existing**

```python
def test_eviction_skipped_when_new_signal_rs_lower_than_all_positions() -> None:
    """No eviction if new signal's RS is not higher than any open position."""
    positions = {
        "AAPL": (100.0, 85.0, 110.0),
        "MSFT": (100.0, 88.0, 112.0),
        "NVDA": (100.0, 90.0, 115.0),
        "CRWD": (100.0, 82.0, 105.0),
        "MU":   (100.0, 91.0, 120.0),
    }
    sim, ohlcv_map = _make_full_sim(positions)
    original_positions = set(sim._open_positions.keys())

    new_signal = {"symbol": "GEV", "rs_score": 79.0, "canslim_score": 75.0, "signal_reason": "Volume Breakout"}
    ohlcv_map["GEV"] = _make_ohlcv(n=10, close_value=40.0)
    entry_date = ohlcv_map["GEV"].index[-1]

    sim._enter_position(new_signal, ohlcv_map, entry_date)

    assert set(sim._open_positions.keys()) == original_positions
    assert "GEV" not in sim._open_positions
```

- [ ] **Step 5: Add test — eviction disabled by enable_eviction=False**

```python
def test_eviction_disabled_when_flag_is_false() -> None:
    """enable_eviction=False prevents all eviction logic."""
    positions = {
        "AAPL": (100.0, 60.0, 95.0),   # underwater, low RS — would be evicted otherwise
        "MSFT": (100.0, 65.0, 96.0),
        "NVDA": (100.0, 68.0, 97.0),
        "CRWD": (100.0, 62.0, 94.0),
        "MU":   (100.0, 55.0, 93.0),
    }
    sim, ohlcv_map = _make_full_sim(positions)
    sim.enable_eviction = False
    original_positions = set(sim._open_positions.keys())

    new_signal = {"symbol": "VST", "rs_score": 95.0, "canslim_score": 80.0, "signal_reason": "Volume Breakout"}
    ohlcv_map["VST"] = _make_ohlcv(n=10, close_value=70.0)
    entry_date = ohlcv_map["VST"].index[-1]

    sim._enter_position(new_signal, ohlcv_map, entry_date)

    assert set(sim._open_positions.keys()) == original_positions
    assert "VST" not in sim._open_positions
```

- [ ] **Step 6: Run all four eviction tests to confirm they fail**

```bash
python -m pytest tests/test_backtest_engine.py::test_eviction_pass1_evicts_underwater_lower_rs_position tests/test_backtest_engine.py::test_eviction_pass2_evicts_lowest_rs_when_no_underwater_positions tests/test_backtest_engine.py::test_eviction_skipped_when_new_signal_rs_lower_than_all_positions tests/test_backtest_engine.py::test_eviction_disabled_when_flag_is_false -v
```

Expected: FAIL (eviction not yet implemented).

---

## Task 9: Implement two-pass eviction

**Files:**
- Modify: `core/backtest_engine.py` — `PortfolioSimulator.__init__` and `_enter_position`; add new `_try_evict` method

- [ ] **Step 1: Add `enable_eviction` parameter to `__init__`**

In `PortfolioSimulator.__init__`, add `enable_eviction: bool = settings.ENABLE_EVICTION` to the parameter list after `benchmark_symbol: str = BENCHMARK`:

```python
    def __init__(
        self,
        initial_capital: float = DEFAULT_CAPITAL,
        max_positions: int = settings.MAX_OPEN_POSITIONS,
        position_size_pct: float = DEFAULT_POSITION_SIZE_PCT,
        position_risk_pct: float = DEFAULT_POSITION_RISK_PCT,
        stop_loss_pct: float = settings.STOP_LOSS_PCT,
        ma_exit_period: int = DEFAULT_MA_EXIT_PERIOD,
        ma_consecutive: int = DEFAULT_MA_CONSECUTIVE,
        signal_every_n_days: int = DEFAULT_SIGNAL_EVERY_N_DAYS,
        min_canslim_score: float = float(settings.MIN_CANSLIM_SCORE),
        min_rs_score: float = DEFAULT_MIN_RS_SCORE,
        min_technical_score: float = DEFAULT_MIN_TECHNICAL_SCORE,
        require_bullish_market: bool = True,
        technical_only: bool = False,
        take_profit_pct: float = DEFAULT_TAKE_PROFIT_PCT,
        scale_out_fraction: float = DEFAULT_SCALE_OUT_FRACTION,
        stagnation_days: int = DEFAULT_STAGNATION_DAYS,
        stagnation_threshold_pct: float = DEFAULT_STAGNATION_THRESHOLD_PCT,
        breakeven_trigger_pct: float = DEFAULT_BREAKEVEN_TRIGGER_PCT,
        data_fetcher: Optional[DataFetcher] = None,
        strategy: Optional[CanslimStrategy] = None,
        benchmark_symbol: str = BENCHMARK,
        enable_eviction: bool = settings.ENABLE_EVICTION,
    ) -> None:
```

And in the `__init__` body, after `self.benchmark_symbol = benchmark_symbol`, add:

```python
        self.enable_eviction = enable_eviction
```

- [ ] **Step 2: Add `_try_evict` method**

Add this new method to `PortfolioSimulator`, just before `_enter_position`:

```python
    def _try_evict(
        self,
        new_signal: dict,
        ticker_ohlcv: Dict[str, pd.DataFrame],
        eval_date: pd.Timestamp,
    ) -> bool:
        """Two-pass eviction: attempt to free a slot for a higher-RS new signal.

        Pass 1: evict an underwater position (close < entry) with lower RS.
        Pass 2: evict any position with lower RS (even if profitable).
        Returns True if a position was evicted, False if the slot could not be freed.
        """
        if not self.enable_eviction:
            return False

        new_rs = new_signal.get("rs_score", 0.0)

        def _current_close(symbol: str) -> Optional[float]:
            ohlcv = ticker_ohlcv.get(symbol)
            if ohlcv is None:
                return None
            bar = ohlcv.loc[eval_date:eval_date]
            if bar.empty:
                prev = ohlcv.loc[:eval_date]
                return float(prev["Close"].iloc[-1]) if not prev.empty else None
            return float(bar["Close"].iloc[0])

        losers: list[tuple[str, Trade, float]] = []
        fallback: list[tuple[str, Trade, float]] = []
        for sym, trade in self._open_positions.items():
            if trade.rs_score >= new_rs:
                continue
            cc = _current_close(sym)
            if cc is None:
                continue  # data gap guard: skip if price unavailable
            fallback.append((sym, trade, cc))
            if cc < trade.entry_price:
                losers.append((sym, trade, cc))

        pool = losers if losers else fallback
        if not pool:
            return False

        evict_sym, _, evict_close = min(pool, key=lambda x: x[1].rs_score)
        self._close_trade(evict_sym, evict_close, "evicted", str(eval_date.date()))
        return True
```

- [ ] **Step 3: Update `_enter_position` to call `_try_evict`**

In `_enter_position`, find:

```python
        if symbol in self._open_positions or len(self._open_positions) >= self.max_positions:
            return
```

Replace with:

```python
        if symbol in self._open_positions:
            return
        if len(self._open_positions) >= self.max_positions:
            if not self._try_evict(signal, ticker_ohlcv, entry_date):
                return
```

- [ ] **Step 4: Run the four eviction tests**

```bash
python -m pytest tests/test_backtest_engine.py::test_eviction_pass1_evicts_underwater_lower_rs_position tests/test_backtest_engine.py::test_eviction_pass2_evicts_lowest_rs_when_no_underwater_positions tests/test_backtest_engine.py::test_eviction_skipped_when_new_signal_rs_lower_than_all_positions tests/test_backtest_engine.py::test_eviction_disabled_when_flag_is_false -v
```

Expected: all 4 PASS.

- [ ] **Step 5: Run full test suite**

```bash
python -m pytest tests/test_backtest_engine.py tests/test_backtest_pnl.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add core/backtest_engine.py tests/test_backtest_engine.py
git commit -m "Implement two-pass eviction: losers-first then RS-fallback portfolio rotation"
```

---

## Task 10: Final validation and full suite

**Files:** None modified — verification only.

- [ ] **Step 1: Run the complete test suite**

```bash
python -m pytest -v
```

Expected: all previously-passing tests pass; the 3 pre-existing failures in `test_e2e_flow.py` and `test_regression.py` are unrelated and acceptable.

- [ ] **Step 2: Quick smoke-test the backtest**

```bash
python backtest_pnl.py 2>&1 | head -40
```

Expected: backtest runs without exceptions, prints equity curve summary.

- [ ] **Step 3: Final commit if any cleanup needed**

```bash
git add -p   # review any remaining changes
git commit -m "Final cleanup: active trading features complete"
```
