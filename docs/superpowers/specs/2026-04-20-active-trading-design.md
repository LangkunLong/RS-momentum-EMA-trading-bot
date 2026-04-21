# Active Trading: Scale-Out, Eviction, and 8-Week Hold Rule — Design Spec

**Date:** 2026-04-20
**Status:** Approved

---

## Goal

Transform the CANSLIM bot from a buy-and-hold screener into an active position manager that takes staged profits, rotates into stronger setups, and holds super-winners for maximum gain — targeting significant outperformance vs SPY in a bull market.

---

## Feature 1: 4-Tier Staged Scale-Out

### Problem

The current engine exits 50% of a position at a single 40% gain threshold. This misses both quick gains (20% in 3 weeks) and multi-hundred-percent super-winners (SNDK +284%, LITE +142%).

### Design

Replace the single exit with three fixed tiers plus an EMA trailing stop for the residual:

| Tier | Gain Target | Fraction of Original Qty Sold |
|------|-------------|-------------------------------|
| 1    | +10%        | 25%                           |
| 2    | +15%        | 25%                           |
| 3    | +20%        | 25%                           |
| 4    | Remaining   | EMA trailing stop (existing)  |

**`Trade` dataclass additions:**
- `scale_out_tier: int = 0` — next tier index to process (0–3)

**`_check_exits` logic (before trailing stop):**
```python
SCALE_OUT_TIERS = settings.SCALE_OUT_TIERS  # [(0.10, 0.25), (0.15, 0.25), (0.20, 0.25)]
while trade.scale_out_tier < len(SCALE_OUT_TIERS):
    gain_target, fraction = SCALE_OUT_TIERS[trade.scale_out_tier]
    if gain_pct < gain_target:
        break
    sell_qty = round(trade.qty * fraction)
    if sell_qty > 0:
        _scale_out_trade(trade, sell_qty, close_price, date)
    trade.scale_out_tier += 1
```

- `trade.qty` = original quantity (immutable reference point for tier fractions)
- `trade.remaining_qty` = currently held shares (decremented at each tier)
- EMA trailing stop only activates after tier 3 completes (unchanged from current `_update_protective_stop`)

**`config/settings.py` addition:**
```python
SCALE_OUT_TIERS: list[tuple[float, float]] = [
    (0.10, 0.25),  # sell 25% of original at +10%
    (0.15, 0.25),  # sell 25% of original at +15%
    (0.20, 0.25),  # sell 25% of original at +20%
]
```

**Gap-up handling:** If a single bar's close clears multiple tier thresholds, the while-loop fires all eligible tiers in sequence on that bar. Sells execute at `close_price` (conservative vs. gap price).

---

## Feature 2: Two-Pass Eviction Strategy

### Problem

When 5 positions are open, new high-RS signals are discarded for lack of cash. This leaves money in weaker setups while better opportunities pass.

### Design

When `_enter_position` is called and `max_positions` is full, execute a two-pass eviction before rejecting the signal:

**Pass 1 — Losers First:**
- Find all open positions where `current_close < trade.entry_price` (underwater) AND `trade.rs_score < new_signal.rs_score`
- Among these, evict the one with the lowest `rs_score`

**Pass 2 — RS Fallback:**
- If Pass 1 finds nothing, find all open positions where `trade.rs_score < new_signal.rs_score`
- Among these, evict the one with the lowest `rs_score`

**No eviction:** If neither pass finds a candidate, the new signal is skipped.

**Data gap guard:** If OHLCV for the eviction target is unavailable on the current bar, skip eviction (do not guess at close price).

**`Trade` dataclass additions:**
- `rs_score: float = 0.0` — captured from signal at entry, used for eviction comparison

**Exit reason:** Evicted positions use `exit_reason = "evicted"` in `SimulationResult`.

**`PortfolioSimulator` parameter:**
- `enable_eviction: bool = True` — set to `False` to disable eviction (for ablation testing)

**`config/settings.py` addition:**
```python
ENABLE_EVICTION: bool = True
```

---

## Feature 3: 8-Week Hold Rule (Super-Winner Protection)

### Problem

The 4-tier scale-out would prematurely liquidate the biggest winners. O'Neil's rule: if a stock gains 20%+ within 3 weeks of entry, it signals exceptional momentum — hold the full position for at least 8 weeks to capture the full move (50–300%+).

### Design

**Detection (3-week window = 15 trading days):**
- Each bar within 15 trading days of entry: if `gain_pct >= 0.20`, set `trade.eight_week_hold = True`

**Effect while active:**
- Suppress all scale-out tier checks in `_check_exits` — no tiers fire
- Hard stop-loss still applies (8% protection is never suspended)
- Eviction can still override the hold (a new signal with higher RS can evict a held position)

**Release (8 weeks = 40 trading days from entry):**
- On the bar where `trading_days_held >= 40`, set `eight_week_hold = False`
- Reset `scale_out_tier = 0` so tiers can fire normally from that point
- Resume standard scale-out + EMA trailing logic

**`Trade` dataclass additions:**
- `eight_week_hold: bool = False`
- `entry_bar_index: int = 0` — bar index at entry, for counting elapsed trading days

**`_check_exits` structure:**
```python
# 1. Release 8-week hold if expired
if trade.eight_week_hold and bars_held >= 40:
    trade.eight_week_hold = False
    trade.scale_out_tier = 0

# 2. Detect 8-week trigger in 3-week window
if not trade.eight_week_hold and bars_held <= 15 and gain_pct >= 0.20:
    trade.eight_week_hold = True

# 3. Scale-out tiers (suppressed during 8-week hold)
if not trade.eight_week_hold:
    while trade.scale_out_tier < len(SCALE_OUT_TIERS):
        ...

# 4. EMA trailing stop (always active after tier 3 or 8-week release)
_update_protective_stop(trade, close_price, ema_short)
```

---

## Affected Files

| File | Change |
|------|--------|
| `config/settings.py` | Add `SCALE_OUT_TIERS`, `ENABLE_EVICTION` |
| `core/backtest_engine.py` | Modify `Trade`, `_check_exits`, `_scale_out_trade`, `_enter_position`, `_update_protective_stop` |
| `backtest_pnl.py` | Pass `enable_eviction` through to engine; update `_make_simulator` helper in tests |
| `tests/test_backtest_engine.py` | New tests for tier sequencing, 8-week trigger, eviction passes |
| `tests/test_backtest_pnl.py` | Update existing tests if Trade fields change |

---

## Testing Requirements

### Scale-out tiers
- Tier 1 fires alone when gain hits exactly 10%
- All 3 tiers fire in sequence when price gaps past 20% in a single bar
- `remaining_qty` after all tiers = 25% of original `qty`
- EMA trailing stop still activates after tier 3

### Eviction
- Pass 1: underwater + lower RS → evicted; higher RS position not evicted
- Pass 2: profitable but lower RS → evicted when no pass-1 candidate
- New signal with RS lower than all positions → no eviction
- Data gap guard: eviction skipped when OHLCV unavailable
- `enable_eviction=False` → no eviction occurs

### 8-Week hold rule
- 20% gain on day 14 → `eight_week_hold = True`, all scale-out tiers suppressed
- 20% gain on day 16 → hold NOT triggered (outside 3-week window), tiers fire normally
- Hold releases on bar 40 → `scale_out_tier` resets to 0, tiers resume
- Hard stop-loss fires even with `eight_week_hold = True`
- Eviction overrides hold (evicted position exits regardless)
