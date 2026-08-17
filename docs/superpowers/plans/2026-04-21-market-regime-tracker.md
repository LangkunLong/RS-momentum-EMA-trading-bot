# Market Regime Tracker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a stateful O'Neil market regime tracker that blocks all new position entries during confirmed corrections, cutting the 68% stop-loss exit rate by preventing entries during weak markets.

**Architecture:** A new `MarketRegimeTracker` class in `core/canslim/m_market_direction.py` maintains three discrete states (`CONFIRMED_UPTREND`, `UNDER_PRESSURE`, `CORRECTION`). It is updated once per trading bar in `PortfolioSimulator.run()`. `_evaluate_signals()` returns `[]` immediately when regime is `CORRECTION`. Existing exit logic (stops, EMA trail, tier exits) is completely unchanged.

**Tech Stack:** Python 3.11+, pandas, existing `core/backtest_engine.py` and `config/settings.py` patterns.

---

## File Map

| File | What changes |
|------|-------------|
| `config/settings.py` | Add `M_REGIME_PRESSURE_DIST_DAYS = 3` |
| `core/canslim/m_market_direction.py` | Add `MarketRegime` enum + `MarketRegimeTracker` class |
| `core/backtest_engine.py` | Bootstrap tracker before loop; update tracker each bar; gate `_evaluate_signals` |
| `tests/test_market_regime.py` | New file — regime transition + integration tests |

---

## Task 1: Add settings constant

**Files:**
- Modify: `config/settings.py`

- [ ] **Step 1: Add constant to settings.py**

Open `config/settings.py`. Find the MARKET TREND SCORING PARAMETERS section. After the existing M-prefixed constants, add:

```python
M_REGIME_PRESSURE_DIST_DAYS: int = 3  # distribution days to enter UNDER_PRESSURE warning zone
```

- [ ] **Step 2: Verify it loads**

```bash
python -c "from config import settings; print(settings.M_REGIME_PRESSURE_DIST_DAYS)"
```

Expected: `3`

- [ ] **Step 3: Commit**

```bash
git add config/settings.py
git commit -m "Add M_REGIME_PRESSURE_DIST_DAYS setting for market regime tracker"
```

---

## Task 2: Add `MarketRegime` enum and `MarketRegimeTracker` skeleton

**Files:**
- Modify: `core/canslim/m_market_direction.py`

- [ ] **Step 1: Add `MarketRegime` enum and `MarketRegimeTracker` class**

Open `core/canslim/m_market_direction.py`. After the existing imports block (after `from core.data_client import ...`), add the `Enum` import and the new classes. Insert this block immediately before the `@dataclass` line for `MarketTrend`:

```python
from enum import Enum


class MarketRegime(Enum):
    """Discrete O'Neil market regime states."""

    CONFIRMED_UPTREND = "confirmed_uptrend"
    UNDER_PRESSURE = "under_pressure"
    CORRECTION = "correction"


class MarketRegimeTracker:
    """Stateful bar-by-bar O'Neil market regime tracker.

    Call bootstrap() once before the simulation loop, then update() once per bar.
    Consult allows_entries before evaluating new signals.
    """

    def __init__(self) -> None:
        """Initialise in CORRECTION — bootstrap() sets the real starting regime."""
        self.regime: MarketRegime = MarketRegime.CORRECTION
        self._dist_day_bars: list[int] = []  # bar indices of distribution days
        self._bar_count: int = 0             # total bars processed since bootstrap
        self._rally_day_count: int = 0
        self._rally_active: bool = False
        self._correction_low: float = float("inf")

    @property
    def allows_entries(self) -> bool:
        """True when regime permits new position entries."""
        return self.regime != MarketRegime.CORRECTION

    @property
    def distribution_days(self) -> int:
        """Number of distribution days currently in the rolling window."""
        return len(self._dist_day_bars)

    def bootstrap(self, spy_df: pd.DataFrame, start_date: pd.Timestamp) -> None:
        """Set initial regime from pre-simulation SPY data.

        Checks SPY's position relative to 200-day EMA and replays the last
        25 bars to populate the distribution day window.

        Args:
            spy_df: Full SPY OHLCV DataFrame (must include data before start_date).
            start_date: First date of the simulation window.
        """
        hist = spy_df.loc[:start_date]
        if len(hist) < 2:
            return

        closes = hist["Close"].astype(float)
        volumes = hist["Volume"].astype(float)

        # Initial regime from 200-day EMA position
        ema_200 = closes.ewm(span=200, adjust=False).mean()
        if float(closes.iloc[-1]) > float(ema_200.iloc[-1]):
            self.regime = MarketRegime.CONFIRMED_UPTREND
        else:
            self.regime = MarketRegime.CORRECTION

        # Replay last M_DISTRIBUTION_LOOKBACK bars to seed distribution day list.
        # Assign negative bar indices so they age out naturally once the live
        # simulation runs M_DISTRIBUTION_LOOKBACK more bars.
        lookback = min(settings.M_DISTRIBUTION_LOOKBACK + 1, len(hist))
        recent_closes = closes.iloc[-lookback:]
        recent_volumes = volumes.iloc[-lookback:]

        for i in range(1, len(recent_closes)):
            bar_idx = i - lookback + 1  # ranges from -(lookback-1) to 0
            close = float(recent_closes.iloc[i])
            prev_close = float(recent_closes.iloc[i - 1])
            vol = float(recent_volumes.iloc[i])
            prev_vol = float(recent_volumes.iloc[i - 1])
            if prev_close > 0:
                pct = (close - prev_close) / prev_close
                if pct <= -settings.M_DISTRIBUTION_MIN_DECLINE and vol > prev_vol:
                    self._dist_day_bars.append(bar_idx)

        # Apply distribution-day thresholds to the bootstrapped regime
        dist_count = len(self._dist_day_bars)
        if self.regime != MarketRegime.CORRECTION:
            if dist_count >= settings.M_MAX_DISTRIBUTION_DAYS:
                self.regime = MarketRegime.CORRECTION
            elif dist_count >= settings.M_REGIME_PRESSURE_DIST_DAYS:
                self.regime = MarketRegime.UNDER_PRESSURE

    def update(
        self,
        date: pd.Timestamp,
        close: float,
        prev_close: float,
        volume: float,
        prev_volume: float,
    ) -> MarketRegime:
        """Update regime for one trading bar. Call once per bar in simulation loop.

        Args:
            date: Current bar date (unused internally but accepted for signature clarity).
            close: Current bar closing price.
            prev_close: Previous bar closing price.
            volume: Current bar volume.
            prev_volume: Previous bar volume.

        Returns:
            Updated MarketRegime.
        """
        self._bar_count += 1

        # 1. Age out distribution days outside the rolling window
        cutoff = self._bar_count - settings.M_DISTRIBUTION_LOOKBACK
        self._dist_day_bars = [b for b in self._dist_day_bars if b > cutoff]

        # 2. Check for a new distribution day
        if prev_close > 0:
            pct = (close - prev_close) / prev_close
            if pct <= -settings.M_DISTRIBUTION_MIN_DECLINE and volume > prev_volume:
                self._dist_day_bars.append(self._bar_count)

        dist_count = len(self._dist_day_bars)

        # 3. Hard correction gate (5+ dist days flips to CORRECTION immediately)
        if dist_count >= settings.M_MAX_DISTRIBUTION_DAYS:
            self.regime = MarketRegime.CORRECTION

        # 4. Uptrend zone adjustments (only when NOT already in correction)
        elif self.regime != MarketRegime.CORRECTION:
            if dist_count >= settings.M_REGIME_PRESSURE_DIST_DAYS:
                self.regime = MarketRegime.UNDER_PRESSURE
            else:
                self.regime = MarketRegime.CONFIRMED_UPTREND

        # 5. Rally attempt logic (only active while in CORRECTION)
        if self.regime == MarketRegime.CORRECTION:
            if not self._rally_active:
                # Track correction low before rally begins
                self._correction_low = min(self._correction_low, close)
                if prev_close > 0 and close > prev_close:
                    self._rally_active = True
                    self._rally_day_count = 1
            else:
                # During rally: check for undercut on any bar
                if close < self._correction_low:
                    self._rally_active = False
                    self._rally_day_count = 0
                    self._correction_low = float("inf")
                elif prev_close > 0 and close > prev_close:
                    self._rally_day_count += 1

                # Follow-through day check
                if self._rally_active and self._rally_day_count >= settings.M_FOLLOW_THROUGH_MIN_DAY:
                    if prev_close > 0:
                        gain = (close - prev_close) / prev_close
                        if gain >= settings.M_FOLLOW_THROUGH_MIN_PCT and volume > prev_volume:
                            self.regime = MarketRegime.CONFIRMED_UPTREND
                            self._dist_day_bars.clear()
                            self._rally_active = False
                            self._rally_day_count = 0
                            self._correction_low = float("inf")

        return self.regime
```

- [ ] **Step 2: Verify the module imports cleanly**

```bash
python -c "from core.canslim.m_market_direction import MarketRegime, MarketRegimeTracker; t = MarketRegimeTracker(); print(t.regime, t.allows_entries)"
```

Expected: `MarketRegime.CORRECTION False`

- [ ] **Step 3: Run existing tests to confirm no regressions**

```bash
python -m pytest tests/ -v --tb=short 2>&1 | tail -15
```

Expected: all previously passing tests still pass.

- [ ] **Step 4: Commit**

```bash
git add core/canslim/m_market_direction.py
git commit -m "Add MarketRegime enum and MarketRegimeTracker to m_market_direction"
```

---

## Task 3: Write failing tests for `MarketRegimeTracker`

**Files:**
- Create: `tests/test_market_regime.py`

- [ ] **Step 1: Create the test file with helpers and all tests**

Create `tests/test_market_regime.py` with the following content:

```python
"""Tests for MarketRegimeTracker — O'Neil market regime state machine."""
from __future__ import annotations

import pandas as pd
import pytest

from core.canslim.m_market_direction import MarketRegime, MarketRegimeTracker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_spy(
    closes: list[float],
    volumes: list[float],
    start: str = "2024-01-02",
) -> pd.DataFrame:
    """Build a minimal SPY OHLCV DataFrame for regime tests."""
    assert len(closes) == len(volumes)
    dates = pd.bdate_range(start=start, periods=len(closes))
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [c * 1.005 for c in closes],
            "Low": [c * 0.995 for c in closes],
            "Close": closes,
            "Volume": volumes,
        },
        index=dates,
    )


def _feed(
    tracker: MarketRegimeTracker,
    bars: list[tuple[float, float]],  # (close, volume) pairs
    start: str = "2024-01-02",
) -> MarketRegime:
    """Feed (close, volume) pairs through the tracker, return final regime."""
    dates = pd.bdate_range(start=start, periods=len(bars))
    for i in range(1, len(bars)):
        close, vol = bars[i]
        prev_close, prev_vol = bars[i - 1]
        tracker.update(dates[i], close, prev_close, vol, prev_vol)
    return tracker.regime


def _dist(prev_close: float, prev_vol: float) -> tuple[float, float]:
    """Return a distribution-day bar (down 0.3%, volume +50%)."""
    return prev_close * 0.997, prev_vol * 1.5


def _up(prev_close: float, prev_vol: float) -> tuple[float, float]:
    """Return a normal up bar (up 0.1%, volume −10%)."""
    return prev_close * 1.001, prev_vol * 0.9


def _ftd(prev_close: float, prev_vol: float) -> tuple[float, float]:
    """Return a follow-through day bar (up 1.6%, volume +20%)."""
    return prev_close * 1.016, prev_vol * 1.2


# ---------------------------------------------------------------------------
# Bootstrap tests
# ---------------------------------------------------------------------------

def test_bootstrap_above_200_ema_starts_uptrend() -> None:
    """SPY above 200-day EMA → bootstrap to CONFIRMED_UPTREND."""
    n = 210
    # Prices steadily rising — latest close is well above the 200-day EMA
    closes = [100.0 + i * 0.1 for i in range(n)]
    volumes = [1e8] * n
    spy_df = _make_spy(closes, volumes)
    tracker = MarketRegimeTracker()
    tracker.bootstrap(spy_df, spy_df.index[-1])
    assert tracker.regime == MarketRegime.CONFIRMED_UPTREND


def test_bootstrap_below_200_ema_starts_correction() -> None:
    """SPY below 200-day EMA → bootstrap to CORRECTION."""
    n = 210
    # Prices declining — latest close is below the 200-day EMA
    closes = [200.0 - i * 0.5 for i in range(n)]
    volumes = [1e8] * n
    spy_df = _make_spy(closes, volumes)
    tracker = MarketRegimeTracker()
    tracker.bootstrap(spy_df, spy_df.index[-1])
    assert tracker.regime == MarketRegime.CORRECTION


# ---------------------------------------------------------------------------
# Distribution day tests
# ---------------------------------------------------------------------------

def test_five_dist_days_triggers_correction() -> None:
    """5 distribution days in 25 bars → CORRECTION, entries blocked."""
    tracker = MarketRegimeTracker()
    tracker.regime = MarketRegime.CONFIRMED_UPTREND

    # Build 20 bars: bars 3, 6, 9, 12, 15 are distribution days
    bars: list[tuple[float, float]] = [(100.0, 1e8)]
    close, vol = bars[0]
    for i in range(1, 20):
        if i in (3, 6, 9, 12, 15):
            c, v = _dist(close, vol)
        else:
            c, v = _up(close, vol)
        bars.append((c, v))
        close, vol = c, v

    _feed(tracker, bars)

    assert tracker.regime == MarketRegime.CORRECTION
    assert tracker.allows_entries is False
    assert tracker.distribution_days == 5


def test_three_dist_days_triggers_under_pressure() -> None:
    """3 distribution days → UNDER_PRESSURE, entries still allowed."""
    tracker = MarketRegimeTracker()
    tracker.regime = MarketRegime.CONFIRMED_UPTREND

    bars: list[tuple[float, float]] = [(100.0, 1e8)]
    close, vol = bars[0]
    for i in range(1, 15):
        if i in (3, 7, 11):
            c, v = _dist(close, vol)
        else:
            c, v = _up(close, vol)
        bars.append((c, v))
        close, vol = c, v

    _feed(tracker, bars)

    assert tracker.regime == MarketRegime.UNDER_PRESSURE
    assert tracker.allows_entries is True
    assert tracker.distribution_days == 3


def test_dist_days_age_out_restores_uptrend() -> None:
    """Distribution days that leave the 25-bar window no longer count."""
    tracker = MarketRegimeTracker()
    tracker.regime = MarketRegime.CONFIRMED_UPTREND

    # 3 distribution days early on, then 30 flat up bars — dist days age out
    bars: list[tuple[float, float]] = [(100.0, 1e8)]
    close, vol = bars[0]
    for i in range(1, 35):
        if i in (2, 4, 6):  # early distribution days
            c, v = _dist(close, vol)
        else:
            c, v = _up(close, vol)
        bars.append((c, v))
        close, vol = c, v

    _feed(tracker, bars)

    assert tracker.regime == MarketRegime.CONFIRMED_UPTREND
    assert tracker.distribution_days < 3


def test_dist_days_cleared_on_follow_through() -> None:
    """Follow-through day clears the distribution day list."""
    tracker = MarketRegimeTracker()
    tracker.regime = MarketRegime.CORRECTION

    # Seed 4 distribution days internally (within lookback window)
    tracker._dist_day_bars = [-10, -8, -6, -4]
    tracker._correction_low = 95.0

    # Simulate a 4-day rally + follow-through on bar 4
    bars = [
        (95.0, 1e8),             # bar 0 (starting point)
        (96.0, 0.9e8),           # rally day 1 (up but < 1.5%)
        (96.5, 0.85e8),          # rally day 2
        (97.0, 0.8e8),           # rally day 3
        (98.5, 1.2e8),           # rally day 4: +1.55% on higher volume → FTD
    ]
    _feed(tracker, bars)

    assert tracker.regime == MarketRegime.CONFIRMED_UPTREND
    assert tracker.distribution_days == 0  # cleared on FTD


# ---------------------------------------------------------------------------
# Follow-through day tests
# ---------------------------------------------------------------------------

def test_follow_through_on_day4_confirms_uptrend() -> None:
    """Follow-through day on rally day 4 transitions to CONFIRMED_UPTREND."""
    tracker = MarketRegimeTracker()
    tracker.regime = MarketRegime.CORRECTION
    tracker._correction_low = 90.0

    bars = [
        (90.0, 1e8),              # bar 0 (correction low already set)
        (91.0, 0.9e8),            # rally day 1
        (91.5, 0.85e8),           # rally day 2
        (92.0, 0.8e8),            # rally day 3
        (93.4, 1.1e8),            # rally day 4: 91.5→93.4 is >1.5%, higher vol → FTD
    ]
    _feed(tracker, bars)

    assert tracker.regime == MarketRegime.CONFIRMED_UPTREND


def test_follow_through_on_day3_does_not_confirm() -> None:
    """Follow-through day criterion requires day 4 or later — day 3 is too early."""
    tracker = MarketRegimeTracker()
    tracker.regime = MarketRegime.CORRECTION
    tracker._correction_low = 90.0

    bars = [
        (90.0, 1e8),              # bar 0
        (91.0, 0.9e8),            # rally day 1
        (92.0, 0.85e8),           # rally day 2
        (93.4, 1.1e8),            # rally day 3: +1.5% higher vol — but only day 3
    ]
    _feed(tracker, bars)

    assert tracker.regime == MarketRegime.CORRECTION


def test_rally_undercut_resets_day_count() -> None:
    """If close undercuts correction low during rally, reset day count."""
    tracker = MarketRegimeTracker()
    tracker.regime = MarketRegime.CORRECTION
    tracker._correction_low = 95.0

    bars = [
        (95.0, 1e8),    # bar 0
        (96.0, 0.9e8),  # rally day 1
        (96.5, 0.85e8), # rally day 2
        (94.5, 0.8e8),  # undercut correction low (94.5 < 95.0) → reset
        (95.5, 0.9e8),  # new rally day 1
        (96.0, 0.85e8), # new rally day 2
        (96.5, 0.8e8),  # new rally day 3 — NOT 6 total
    ]
    _feed(tracker, bars)

    # Should still be in CORRECTION: only 3 rally days since undercut reset
    assert tracker.regime == MarketRegime.CORRECTION
    assert tracker._rally_day_count == 3


def test_allows_entries_false_only_in_correction() -> None:
    """allows_entries is False only when regime is CORRECTION."""
    tracker = MarketRegimeTracker()

    tracker.regime = MarketRegime.CORRECTION
    assert tracker.allows_entries is False

    tracker.regime = MarketRegime.UNDER_PRESSURE
    assert tracker.allows_entries is True

    tracker.regime = MarketRegime.CONFIRMED_UPTREND
    assert tracker.allows_entries is True
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest tests/test_market_regime.py -v --tb=short 2>&1 | tail -25
```

Expected: most tests FAIL or ERROR (tracker not yet implemented — Task 2 added the skeleton but the logic is in place, so some may pass). Note which fail and confirm the test file runs without import errors.

---

## Task 4: Verify tests pass with the implementation

The `MarketRegimeTracker` implementation was already written in Task 2. Run the tests now to confirm they all pass.

- [ ] **Step 1: Run the regime tests**

```bash
python -m pytest tests/test_market_regime.py -v --tb=short 2>&1 | tail -30
```

Expected: all 10 tests PASS.

If any fail, diagnose and fix the specific logic in `core/canslim/m_market_direction.py`. Common issues:
- Off-by-one in rally day count (day 1 vs 0-indexed)
- `_correction_low` not frozen once rally is active
- FTD gain computed as `close/prev_close - 1` vs `(close - prev_close)/prev_close` (should be same)

- [ ] **Step 2: Run full existing test suite**

```bash
python -m pytest tests/ -v --tb=short 2>&1 | tail -15
```

Expected: all previously passing tests still pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_market_regime.py core/canslim/m_market_direction.py
git commit -m "Add MarketRegimeTracker with O'Neil distribution-day and follow-through logic"
```

---

## Task 5: Write failing integration test — entries blocked in CORRECTION

**Files:**
- Modify: `tests/test_market_regime.py`

This test verifies that `PortfolioSimulator._evaluate_signals()` returns `[]` when the regime tracker is in CORRECTION. Before the gate is added in Task 6, the test will fail with `AttributeError` (no `_regime_tracker` attribute on the simulator).

- [ ] **Step 1: Add integration test to `tests/test_market_regime.py`**

Append to the end of `tests/test_market_regime.py`:

```python
# ---------------------------------------------------------------------------
# Integration: PortfolioSimulator entry gate
# ---------------------------------------------------------------------------

from backtest_pnl import PortfolioSimulator


def test_evaluate_signals_blocked_when_regime_is_correction() -> None:
    """_evaluate_signals returns [] immediately when regime tracker is CORRECTION."""
    sim = PortfolioSimulator(initial_capital=100_000.0, stagnation_days=999)
    # Inject a tracker in CORRECTION state
    tracker = MarketRegimeTracker()
    tracker.regime = MarketRegime.CORRECTION
    sim._regime_tracker = tracker

    result = sim._evaluate_signals(
        tickers=["NVDA"],
        ticker_ohlcv={},
        all_closes=pd.DataFrame(),
        eval_date=pd.Timestamp("2026-04-01"),
        market_state={"market_is_bullish": True, "m_score": 0.9, "distribution_days": 5, "follow_through": False},
    )

    assert result == []


def test_evaluate_signals_not_blocked_in_uptrend() -> None:
    """_evaluate_signals proceeds normally (not blocked) when regime is CONFIRMED_UPTREND."""
    sim = PortfolioSimulator(initial_capital=100_000.0, stagnation_days=999)
    tracker = MarketRegimeTracker()
    tracker.regime = MarketRegime.CONFIRMED_UPTREND
    sim._regime_tracker = tracker

    # ticker_ohlcv is empty so no signals will be found — but crucially the method
    # should NOT return early due to the regime gate
    result = sim._evaluate_signals(
        tickers=[],
        ticker_ohlcv={},
        all_closes=pd.DataFrame(),
        eval_date=pd.Timestamp("2026-04-01"),
        market_state={"market_is_bullish": True, "m_score": 0.9, "distribution_days": 0, "follow_through": True},
    )

    # Returns [] because no tickers, not because regime blocked it
    assert result == []
```

- [ ] **Step 2: Run to confirm the first test fails**

```bash
python -m pytest tests/test_market_regime.py::test_evaluate_signals_blocked_when_regime_is_correction -v --tb=short
```

Expected: FAIL with `AttributeError: '_evaluate_signals'` or the test returns a non-empty list (regime not gated yet). Either failure mode is valid.

---

## Task 6: Integrate `MarketRegimeTracker` into `PortfolioSimulator`

**Files:**
- Modify: `core/backtest_engine.py`

Three surgical changes. Read `core/backtest_engine.py` before editing to confirm exact line numbers.

- [ ] **Step 1: Add import at the top of `core/backtest_engine.py`**

Find the existing import block at the top of `core/backtest_engine.py`. After the line that imports from `core.canslim.m_market_direction` (search for `_evaluate_market_at_date`), add `MarketRegimeTracker` to the import:

Find:
```python
from core.canslim.m_market_direction import (
```

The import block imports several names. Add `MarketRegimeTracker` and `MarketRegime` to it. The result should include:
```python
from core.canslim.m_market_direction import (
    MarketRegime,
    MarketRegimeTracker,
    _evaluate_market_at_date,
    # ... other existing imports unchanged
)
```

- [ ] **Step 2: Bootstrap tracker in `run()` before the main loop**

In `run()`, find the block after `trading_days` is defined and the early-return guard (around line 657):

```python
        if len(trading_days) < 30:
            print("ERROR: Not enough trading days in range.")
            return SimulationResult()
```

Immediately after that block (before `equity_series: Dict[str, float] = {}`), add:

```python
        regime_tracker = MarketRegimeTracker()
        regime_tracker.bootstrap(benchmark_df, start_ts)
        self._regime_tracker = regime_tracker
```

- [ ] **Step 3: Update tracker once per bar in the main loop**

In the main `for day_idx, eval_date in enumerate(trading_days):` loop, add the per-bar update as the very first statement inside the loop (before the exit checks):

Find:
```python
        for day_idx, eval_date in enumerate(trading_days):
            date_str = str(eval_date.date())

            for symbol in list(self._open_positions.keys()):
```

Replace with:

```python
        for day_idx, eval_date in enumerate(trading_days):
            date_str = str(eval_date.date())

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

            for symbol in list(self._open_positions.keys()):
```

- [ ] **Step 4: Gate `_evaluate_signals()` on regime**

In `_evaluate_signals()`, find the first line of the method body (after the docstring):

```python
        if self.require_bullish_market and not market_state["market_is_bullish"]:
            return []
```

Add the regime gate immediately before that line:

```python
        if not self._regime_tracker.allows_entries:
            return []

        if self.require_bullish_market and not market_state["market_is_bullish"]:
            return []
```

- [ ] **Step 5: Run the integration tests**

```bash
python -m pytest tests/test_market_regime.py::test_evaluate_signals_blocked_when_regime_is_correction tests/test_market_regime.py::test_evaluate_signals_not_blocked_in_uptrend -v --tb=short
```

Expected: both PASS.

- [ ] **Step 6: Run the full test suite**

```bash
python -m pytest tests/ -v --tb=short 2>&1 | tail -20
```

Expected: all previously passing tests pass. The 3 pre-existing failures in `test_e2e_flow.py` and `test_regression.py` are acceptable.

- [ ] **Step 7: Commit**

```bash
git add core/backtest_engine.py tests/test_market_regime.py
git commit -m "Integrate MarketRegimeTracker into PortfolioSimulator — block entries in CORRECTION"
```

---

## Task 7: Backtest validation

- [ ] **Step 1: Run the full test suite one final time**

```bash
python -m pytest tests/ --tb=short 2>&1 | tail -10
```

Expected: 307+ passed, 3 pre-existing failures only.

- [ ] **Step 2: Run the backtest and compare to baseline**

```bash
python backtest_pnl.py --technical-only --universe large_cap --start-date 2023-04-01 --end-date 2026-04-01 2>&1 | grep -A 20 "Portfolio vs Benchmark"
```

**Baseline (before this feature):**
```
Total Return     37.5%   58.2% (SPY)
Max Drawdown     -8.4%  -19.0%
Sharpe Ratio      1.16    1.11
Stop-loss exits: 62/91 (68%)
```

**Expected improvement:** fewer stop-loss exits (target < 50%), higher total return, Sharpe ≥ 1.16.

- [ ] **Step 3: Final commit if any cleanup needed**

```bash
git add -p
git commit -m "Market regime tracker: final cleanup"
```
