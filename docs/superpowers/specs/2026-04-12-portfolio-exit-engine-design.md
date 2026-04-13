# Portfolio State & Exit Engine — Design Spec

**Date:** 2026-04-12
**Status:** Approved
**Scope:** Event-sourced portfolio state layer + O'Neil-principled exit engine + position sizing

---

## 1. Context and Goal

The CANSLIM scanner is a complete, backtested signal generator. It produces ranked buy
candidates but has no concept of portfolio state, exit discipline, or position sizing.

This spec covers the next evolutionary layer: a **paper trading substrate** that sits on
top of the existing scanner without modifying it. The goal is to build the infrastructure
correctly from day one so that transitioning to live execution requires adding an execution
adapter, not rewriting the portfolio layer.

**Out of scope for this spec:** the event loop / scheduler, Alpaca paper order placement,
and the live trading adapter. Those are the next sub-project.

---

## 2. Architecture

### 2.1 Core principle

The SQLite event ledger is the **only source of truth**. No row is ever updated or
deleted. Every state change — entry, exit, stop adjustment, regime shift — is a new row
appended to the `events` table. Current portfolio state is always derived by replaying
events, optionally accelerated by a periodic snapshot.

```
Scanner signal fires
        │
        ▼
PositionSizer.calculate()   ←── portfolio_cash, open_count, MarketTrend
        │
        ▼ SizingRecommendation
        │
        ▼
Ledger.append(OrderPlaced)
        │
        ▼  (paper fill: immediate at last close)
Ledger.append(OrderFilled)
Ledger.append(StopPriceUpdated, reason=INITIAL)
        │
        ▼  (every scan cycle, per open position)
ExitEngine.evaluate()       ←── OpenPosition, current_price, rs_score, MarketTrend
        │
        ▼ ExitSignal | None
        │
        ▼
Ledger.append(ExitSignalGenerated)
Ledger.append(PositionClosed)
        │
        ▼
projections.py replays all events → current OpenPositions, PortfolioState, P&L
```

### 2.2 Module layout

```
core/portfolio/
├── __init__.py
├── events.py        # Pydantic event types — vocabulary of the system
├── ledger.py        # Append-only SQLite event store
├── projections.py   # Replay events → OpenPosition, PortfolioState, trade history
├── exit_engine.py   # O'Neil exit rules — pure functions, no DB access
├── sizing.py        # O'Neil position sizing — pure functions, no DB access
└── snapshots.py     # Periodic materialised state for query performance
```

### 2.3 SQLite schema (3 tables)

```sql
-- Append-only event ledger. Never UPDATE or DELETE.
CREATE TABLE events (
    event_id     TEXT    PRIMARY KEY,
    event_type   TEXT    NOT NULL,
    portfolio_id TEXT    NOT NULL,
    timestamp    TEXT    NOT NULL,          -- ISO-8601 UTC
    payload      TEXT    NOT NULL,          -- JSON blob
    sequence_num INTEGER NOT NULL           -- monotonic, for replay ordering
);
CREATE INDEX idx_events_portfolio ON events (portfolio_id, sequence_num);
CREATE INDEX idx_events_symbol    ON events (
    portfolio_id,
    json_extract(payload, '$.symbol')
);

-- Periodic materialised snapshots (performance optimisation).
-- Written by snapshots.py every N events or once per trading day.
CREATE TABLE snapshots (
    snapshot_id  TEXT    PRIMARY KEY,
    portfolio_id TEXT    NOT NULL,
    timestamp    TEXT    NOT NULL,
    sequence_num INTEGER NOT NULL,          -- events replayed up to this point
    state        TEXT    NOT NULL           -- JSON: cash, positions, pnl
);

-- Schema version tracking.
CREATE TABLE schema_migrations (
    version      INTEGER PRIMARY KEY,
    applied_at   TEXT    NOT NULL,
    description  TEXT    NOT NULL
);
```

---

## 3. Event Types

All events share a common envelope via a Pydantic base class. The `event_type` field
is the discriminator for deserialisation.

### 3.1 Base envelope

```python
class PortfolioEvent(BaseModel):
    event_id:     UUID     = Field(default_factory=uuid4)
    event_type:   str
    timestamp:    datetime                  # UTC, always
    portfolio_id: str
```

### 3.2 Event catalogue

| Event type | When it fires | Key payload fields |
|---|---|---|
| `PortfolioInitialised` | Once, at first startup | `initial_capital: float`, `currency: str = "USD"` |
| `SignalGenerated` | Scanner produces a buy candidate | `symbol`, `canslim_score`, `rs_score`, `component_scores {C,A,N,S,L,I,M}`, `is_bullish_market` |
| `OrderPlaced` | Order submitted to Alpaca paper | `symbol`, `alpaca_order_id`, `shares`, `order_type`, `intended_stop`, `intended_target` |
| `OrderFilled` | Fill confirmed from Alpaca | `alpaca_order_id`, `symbol`, `fill_price`, `shares`, `fill_timestamp` |
| `StopPriceUpdated` | Stop adjusted post-entry | `symbol`, `old_stop`, `new_stop`, `reason: StopReason` |
| `MonsterStockHoldActivated` | 8-week hold rule triggered (20%+ gain in <15 trading days) | `symbol`, `pnl_pct`, `trading_days_held`, `hold_until: datetime` |
| `ExitSignalGenerated` | O'Neil exit rule triggered | `symbol`, `exit_reason`, `current_price`, `pnl_pct`, `days_held`, `market_trend_score` |
| `PositionClosed` | Exit fill confirmed | `symbol`, `entry_price`, `exit_price`, `shares`, `pnl`, `pnl_pct`, `exit_reason`, `days_held` |
| `MarketRegimeChanged` | M score crosses bullish/bearish | `old_score`, `new_score`, `old_is_bullish`, `new_is_bullish`, `distribution_days`, `action_taken` |

### 3.3 Enumerations

```python
class ExitReason(str, Enum):
    STOP_LOSS       = "stop_loss"        # 7-8% hard stop hit
    PROFIT_TARGET   = "profit_target"    # 20-25% target hit
    EIGHT_WEEK_HOLD = "eight_week_hold"  # Monster stock hold period expired
    REGIME_TIGHTENED = "regime_tightened" # Bearish M, tightened stop now hit
    RS_DEGRADATION  = "rs_degradation"   # RS dropped below threshold
    MANUAL          = "manual"           # Human override

class StopReason(str, Enum):
    INITIAL          = "initial"         # Set at entry
    REGIME_TIGHTENED = "regime_tightened"
    TRAILING         = "trailing"        # Phase 2
    MANUAL           = "manual"
```

---

## 4. Exit Engine

The exit engine is a **pure function module**. It accepts position state and market data,
applies O'Neil's rules in priority order, and returns a signal or None. It never touches
the database. Every rule is independently unit-testable.

### 4.1 Derived state required from projections

```python
class OpenPosition(BaseModel):
    symbol:                 str
    entry_price:            float
    entry_date:             datetime
    shares:                 float
    stop_price:             float       # current stop (may have been tightened)
    target_price:           float       # profit target price
    canslim_score_at_entry: float
    rs_score_at_entry:      float
    eight_week_hold:        bool        # True if monster-stock rule was triggered
    hold_until:             datetime | None  # None if eight_week_hold is False
```

### 4.2 Rule priority

When multiple rules trigger simultaneously, priority determines which fires:

```
Priority 1 — Stop-Loss         (IMMEDIATE — act before next bar)
Priority 2 — 8-Week Hold Check (suppresses profit target during hold window)
Priority 3 — Profit Target     (NEXT_OPEN — sell at open)
Priority 4 — RS Degradation    (NEXT_OPEN — no longer a leader)
Priority 5 — Regime Shift      (tighten stops; only exits if tightened stop is hit)
```

### 4.3 Rule specifications

**Rule 1 — Hard Stop-Loss**

O'Neil: *"The most important rule. Cut every loss at 7-8%. No exceptions."*

```
TRIGGER:  current_price <= position.stop_price
ACTION:   ExitSignal(reason=STOP_LOSS, urgency=IMMEDIATE)
```

The stop price may have been tightened by a prior `StopPriceUpdated(REGIME_TIGHTENED)`
event. The rule always checks against `position.stop_price`, not the original 8%.

**Rule 2 — 8-Week Hold (Monster Stock Rule)**

O'Neil: *"If a stock rises 20%+ in under 3 weeks, hold at least 8 weeks."*

Evaluated at fill time, not at exit time:

```
ON EACH SCAN CYCLE (while position is open):
  IF pnl_pct >= EXIT_MONSTER_STOCK_TRIGGER
     AND trading_days_held < 15
     AND position.eight_week_hold is False:
      hold_until = entry_date + EXIT_MONSTER_STOCK_HOLD_DAYS (56 calendar days)
      emit MonsterStockHoldActivated(symbol, pnl_pct, trading_days_held, hold_until)
      -- projection sets position.eight_week_hold = True, position.hold_until

DURING HOLD WINDOW (now < position.hold_until):
  Profit target rule is suppressed.
  Stop-loss and RS degradation remain active.

AFTER HOLD WINDOW:
  All rules re-activate normally.
```

**Rule 3 — Profit Target**

O'Neil: *"When a stock rises 20-25% from a proper buy point, sell it."*

```
TRIGGER:  current_price >= position.target_price
          AND NOT (position.eight_week_hold AND now < position.hold_until)
ACTION:   ExitSignal(reason=PROFIT_TARGET, urgency=NEXT_OPEN)
```

**Rule 4 — RS Degradation**

O'Neil: *"A stock that was a leader and stops leading is telling you something."*

```
TRIGGER:  current_rs_score < EXIT_RS_DEGRADATION_THRESHOLD
ACTION:   ExitSignal(reason=RS_DEGRADATION, urgency=NEXT_OPEN)
```

Checked once per scan cycle. Not intraday.

**Rule 5 — Market Regime Shift**

O'Neil: *"3 out of 4 stocks follow the market. When the market turns, get defensive."*

This rule does not directly exit positions. It tightens stops and blocks new entries.
A tightened stop may subsequently trigger Rule 1.

```
ON M SCORE CROSSING BEARISH THRESHOLD:
  FOR each open position:
      new_stop = entry_price × (1 - EXIT_STOP_LOSS_PCT × REGIME_STOP_MULTIPLIER)
      IF new_stop > position.stop_price:  # never loosen a stop
          emit StopPriceUpdated(reason=REGIME_TIGHTENED, old=stop, new=new_stop)
  SET effective_max_positions = REGIME_MAX_POSITIONS_BEAR
  emit MarketRegimeChanged(action_taken="STOPS_TIGHTENED")

ON M SCORE RECOVERING BULLISH:
  SET effective_max_positions = REGIME_MAX_POSITIONS_BULL
  emit MarketRegimeChanged(action_taken="REGIME_RECOVERED")
  NOTE: stops are NOT loosened. Tighten on the way down; let price recover upward.
```

### 4.4 Public interface

```python
def evaluate_exit(
    position: OpenPosition,
    current_price: float,
    current_rs_score: float,
    market_trend: MarketTrend,
) -> ExitSignal | None:
    """Pure function. No DB access. Returns None if no exit rule fires."""

def evaluate_regime_shift(
    old_trend: MarketTrend,
    new_trend: MarketTrend,
    open_positions: list[OpenPosition],
) -> list[StopPriceUpdated]:
    """Returns StopPriceUpdated events for all positions that need tightening.
    Returns empty list if regime has not changed or no tightening is needed."""
```

---

## 5. Position Sizing

The position sizer is also a **pure function module**. Every guard rail is checked before
any order is placed. Returning `None` means the entry is blocked — the caller must not
place an order.

### 5.1 Sizing methods

**Method A — Equal-Weight (default, O'Neil canon)**

```
allocation = portfolio_cash × POSITION_WEIGHT_PCT(regime)
shares     = floor(allocation / entry_price)
```

- Bull market: `POSITION_WEIGHT_PCT = 0.20` → up to 5 positions × 20% = fully deployed
- Bear market: `POSITION_WEIGHT_PCT = 0.15` → up to 2 positions × 15% = 70% cash

**Method B — Risk-Based (configurable alternative)**

```
max_loss     = portfolio_value × POSITION_RISK_PCT        # e.g. 1% of $100k = $1,000
risk_p_share = entry_price - stop_price                   # e.g. $8 on $100 entry with 8% stop
shares       = floor(max_loss / risk_p_share)             # e.g. 125 shares
position_val = shares × entry_price                       # e.g. $12,500
```

Wider stops automatically produce smaller positions. Hard-capped at `POSITION_MAX_PCT`.

### 5.2 Guard rails (evaluated in order)

All six must pass. First failure blocks the entry.

```
1. open_positions < max_positions(regime)     # Never exceed position cap
2. available_cash >= allocation               # Never invest money unavailable
3. current_rs_score >= MIN_RS_SCORE           # Still a market leader at entry time
4. market_trend.is_bullish OR flag override   # O'Neil: don't buy into bad markets
5. position_value <= portfolio_value × POSITION_MAX_PCT   # Hard cap per position
6. symbol NOT in open_positions               # No averaging down, ever
```

Guard rail 6 is O'Neil's most emphatic rule. It is enforced structurally — not by
configuration. There is no setting to disable it.

### 5.3 Pyramid buying (Phase 2, disabled by default)

When `PYRAMID_ENABLED = True`, initial entry uses 50% of the intended allocation.
Subsequent adds (30%, then 20%) fire only if the position is profitable at each add point.
The event types already support this — no schema change required to enable it.

### 5.4 Public interface

```python
def calculate_position_size(
    symbol: str,
    entry_price: float,
    stop_price: float,
    portfolio_cash: float,
    portfolio_value: float,
    open_position_count: int,
    open_position_symbols: set[str],
    market_trend: MarketTrend,
    current_rs_score: float,
) -> SizingRecommendation | None:
    """Pure function. Returns None if any guard rail blocks the entry."""
```

---

## 6. Projections

`projections.py` replays the event ledger to produce derived views. These are the only
way callers read portfolio state — never query the raw events table directly.

| Projection | Returns | Derived from |
|---|---|---|
| `get_open_positions(portfolio_id)` | `list[OpenPosition]` | All `OrderFilled` minus `PositionClosed`; `MonsterStockHoldActivated` sets hold fields |
| `get_portfolio_state(portfolio_id)` | `PortfolioState` | Cash, positions, total value, P&L |
| `get_trade_history(portfolio_id)` | `list[ClosedTrade]` | All `PositionClosed` events |
| `get_regime_history(portfolio_id)` | `list[MarketRegimeChanged]` | All `MarketRegimeChanged` events |
| `get_state_as_of(portfolio_id, dt)` | `PortfolioState` | Replay up to nearest snapshot before `dt`, then tail |

Snapshots accelerate `get_state_as_of` — without them, full replay is required for every
point-in-time query. The snapshot writer runs at end-of-day or every 100 events.

---

## 7. Configuration Changes

All new parameters are added to `config/settings.py` under clearly labelled sections.
All are configurable; none are hardcoded in the logic modules.

```python
# ==============================================================================
# PAPER TRADING — PORTFOLIO
# ==============================================================================
PAPER_PORTFOLIO_ID       = "paper_v1"
PAPER_INITIAL_CAPITAL    = 100_000.0       # USD
PORTFOLIO_DB_PATH        = "portfolio.db"  # SQLite file path

# ==============================================================================
# EXIT ENGINE (O'Neil defaults)
# ==============================================================================
EXIT_STOP_LOSS_PCT            = 0.08   # 8% hard stop-loss
EXIT_PROFIT_TARGET_PCT        = 0.25   # 25% profit target
EXIT_MONSTER_STOCK_TRIGGER    = 0.20   # Gain threshold for 8-week hold rule
EXIT_MONSTER_STOCK_HOLD_DAYS  = 56     # 8 calendar weeks

# ==============================================================================
# MARKET REGIME RESPONSE
# ==============================================================================
REGIME_STOP_MULTIPLIER        = 0.625  # Bearish: 8% stop → 5% (8 × 0.625)
REGIME_MAX_POSITIONS_BULL     = 5      # Max open positions in bull market
REGIME_MAX_POSITIONS_BEAR     = 2      # Max open positions in bear market

# ==============================================================================
# POSITION SIZING
# ==============================================================================
SIZING_METHOD                 = "equal_weight"   # "equal_weight" | "risk_based"
POSITION_WEIGHT_BULL_PCT      = 0.20   # 20% per position in bull market
POSITION_WEIGHT_BEAR_PCT      = 0.15   # 15% per position in bear market
POSITION_MAX_PCT              = 0.25   # Hard cap: never > 25% single position
POSITION_RISK_PCT             = 0.01   # Risk-based: risk 1% of portfolio per trade
PYRAMID_ENABLED               = False  # Enable pyramid buying (Phase 2)

# ==============================================================================
# RS DEGRADATION EXIT
# ==============================================================================
EXIT_RS_DEGRADATION_THRESHOLD = 70     # Exit if RS falls below this post-entry
```

---

## 8. Dependencies

Add to `requirements.txt`:

```
pydantic>=2.0       # Validated data models for events, positions, signals
apscheduler>=3.10   # Event loop scheduler (next sub-project)
structlog>=24.0     # Structured JSON audit logging
```

`sqlite3` is Python built-in — no new dependency.

---

## 9. Testing Requirements

Per project conventions (CLAUDE.md), every new module requires:

1. **Happy path** — valid inputs produce correct output
2. **Guard rail tests** — each of the 6 position sizing guards blocks correctly
3. **Exit rule isolation** — each of the 5 exit rules fires independently
4. **Priority ordering** — when stop-loss and profit target both trigger, stop-loss wins
5. **8-week hold suppression** — profit target is suppressed during hold window
6. **Regime shift** — stop tightening produces correct `StopPriceUpdated` events
7. **Projection correctness** — replaying a known event sequence produces expected state
8. **No averaging down** — guard rail 6 is structurally untestable to bypass

All tests are pure (no SQLite, no Alpaca calls). The `ledger.py` module uses an
in-memory SQLite connection (`:memory:`) in tests.

---

## 10. What This Does Not Cover

Deliberately excluded from this spec:

- **Alpaca paper order placement** — `TradingClient` integration is the next sub-project
- **Event loop / scheduler** — APScheduler wiring is the next sub-project
- **Live trading adapter** — requires compliance review before implementation
- **Trailing stops** — referenced as Phase 2; event types already support them
- **Partial exits** — referenced as Phase 2
- **Multi-portfolio support** — `portfolio_id` field exists; UI not designed yet
- **Reporting / dashboard** — out of scope for this phase
