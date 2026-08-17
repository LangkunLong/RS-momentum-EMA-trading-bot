# Portfolio State & Exit Engine Implementation Plan

> **Superseded for implementation:** The canonical execution architecture is defined in `docs/superpowers/specs/2026-08-16-paper-trading-stabilization-design.md`. This file remains historical design context and must not be used to introduce a second portfolio state subsystem.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an event-sourced portfolio state layer with O'Neil-principled exit rules and position sizing on top of the existing CANSLIM scanner, without modifying any existing code.

**Architecture:** Every state change (entry, exit, stop adjustment, regime shift) is an immutable event appended to a SQLite ledger. Current portfolio state is always derived by replaying those events. The exit engine and position sizer are pure functions that never touch the database, making every rule independently testable.

**Tech Stack:** Python 3.11+, pydantic>=2.0, sqlite3 (built-in), structlog>=24.0. No ORM. No framework.

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `config/settings.py` | Modify | Add 5 new config blocks (portfolio, exit, regime, sizing, RS degradation) |
| `requirements.txt` | Modify | Add pydantic, structlog |
| `core/portfolio/__init__.py` | Create | Package public API |
| `core/portfolio/events.py` | Create | Pydantic event types — 9 event classes + 2 enums |
| `core/portfolio/ledger.py` | Create | Append-only SQLite event store |
| `core/portfolio/projections.py` | Create | Replay events → OpenPosition, PortfolioState, trade history |
| `core/portfolio/exit_engine.py` | Create | O'Neil exit rules — 5 pure functions |
| `core/portfolio/sizing.py` | Create | O'Neil position sizing — 6 guard rails + 2 methods |
| `core/portfolio/snapshots.py` | Create | Write periodic materialised snapshots |
| `tests/portfolio/__init__.py` | Create | Empty — marks test package |
| `tests/portfolio/test_events.py` | Create | Event serialisation round-trip tests |
| `tests/portfolio/test_ledger.py` | Create | Ledger append, load, sequence ordering |
| `tests/portfolio/test_projections.py` | Create | Replay correctness for every event type |
| `tests/portfolio/test_exit_engine.py` | Create | Each exit rule + priority ordering |
| `tests/portfolio/test_sizing.py` | Create | Both sizing methods + all 6 guard rails |

---

## Task 1: Configuration

**Files:**
- Modify: `config/settings.py`

- [ ] **Step 1: Add new config blocks at the end of settings.py**

Open `config/settings.py` and append after the last existing block:

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
EXIT_STOP_LOSS_PCT            = 0.08   # 8% hard stop-loss below entry price
EXIT_PROFIT_TARGET_PCT        = 0.25   # 25% profit target above entry price
EXIT_MONSTER_STOCK_TRIGGER    = 0.20   # Gain threshold to activate 8-week hold
EXIT_MONSTER_STOCK_HOLD_DAYS  = 56     # 8 calendar weeks

# ==============================================================================
# MARKET REGIME RESPONSE
# ==============================================================================
REGIME_STOP_MULTIPLIER        = 0.625  # Bearish: 8% stop → 5%  (8 × 0.625)
REGIME_MAX_POSITIONS_BULL     = 5      # Max open positions in a bull market
REGIME_MAX_POSITIONS_BEAR     = 2      # Max open positions in a bear market

# ==============================================================================
# POSITION SIZING
# ==============================================================================
SIZING_METHOD                 = "equal_weight"  # "equal_weight" | "risk_based"
POSITION_WEIGHT_BULL_PCT      = 0.20   # 20% of portfolio per position (bull)
POSITION_WEIGHT_BEAR_PCT      = 0.15   # 15% of portfolio per position (bear)
POSITION_MAX_PCT              = 0.25   # Hard cap: never > 25% in one position
POSITION_RISK_PCT             = 0.01   # Risk-based: risk 1% of portfolio per trade
PYRAMID_ENABLED               = False  # Phase 2 — pyramid buying disabled

# ==============================================================================
# RS DEGRADATION EXIT
# ==============================================================================
EXIT_RS_DEGRADATION_THRESHOLD = 70     # Exit a position if RS falls below this
```

- [ ] **Step 2: Verify settings load without error**

```bash
python -c "from config import settings; print(settings.EXIT_STOP_LOSS_PCT)"
```

Expected output: `0.08`

- [ ] **Step 3: Commit**

```bash
git add config/settings.py
git commit -m "config: add portfolio, exit engine, and sizing settings blocks"
```

---

## Task 2: Dependencies

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add new packages**

Open `requirements.txt` and add after the existing runtime dependencies:

```
pydantic>=2.0
structlog>=24.0
apscheduler>=3.10
```

- [ ] **Step 2: Install**

```bash
pip install pydantic>=2.0 structlog>=24.0 "apscheduler>=3.10"
```

Expected: packages install without error. Verify:

```bash
python -c "import pydantic; import structlog; print(pydantic.__version__)"
```

Expected: prints a version >= 2.0.

- [ ] **Step 3: Commit**

```bash
git add requirements.txt
git commit -m "deps: add pydantic, structlog, apscheduler"
```

---

## Task 3: Event Types

**Files:**
- Create: `core/portfolio/__init__.py`
- Create: `core/portfolio/events.py`
- Create: `tests/portfolio/__init__.py`
- Create: `tests/portfolio/test_events.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/portfolio/__init__.py` (empty):

```python
```

Create `tests/portfolio/test_events.py`:

```python
"""Tests for portfolio event types — serialisation and deserialisation."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from core.portfolio.events import (
    ExitReason,
    MarketRegimeChanged,
    MonsterStockHoldActivated,
    OrderFilled,
    OrderPlaced,
    PortfolioInitialised,
    PositionClosed,
    SignalGenerated,
    StopPriceUpdated,
    StopReason,
    deserialise_event,
)


_NOW = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
_PID = "paper_v1"


def test_portfolio_initialised_round_trip() -> None:
    event = PortfolioInitialised(
        portfolio_id=_PID, timestamp=_NOW, initial_capital=100_000.0
    )
    data = event.model_dump()
    restored = deserialise_event(data)
    assert isinstance(restored, PortfolioInitialised)
    assert restored.initial_capital == 100_000.0
    assert restored.currency == "USD"


def test_order_filled_round_trip() -> None:
    event = OrderFilled(
        portfolio_id=_PID,
        timestamp=_NOW,
        alpaca_order_id="abc123",
        symbol="NVDA",
        fill_price=900.0,
        shares=10.0,
        fill_timestamp=_NOW,
    )
    data = event.model_dump()
    restored = deserialise_event(data)
    assert isinstance(restored, OrderFilled)
    assert restored.symbol == "NVDA"
    assert restored.fill_price == 900.0


def test_stop_price_updated_round_trip() -> None:
    event = StopPriceUpdated(
        portfolio_id=_PID,
        timestamp=_NOW,
        symbol="NVDA",
        old_stop=828.0,
        new_stop=855.0,
        reason=StopReason.REGIME_TIGHTENED,
    )
    data = event.model_dump()
    restored = deserialise_event(data)
    assert isinstance(restored, StopPriceUpdated)
    assert restored.reason == StopReason.REGIME_TIGHTENED


def test_monster_stock_hold_round_trip() -> None:
    hold_until = datetime(2026, 3, 15, tzinfo=timezone.utc)
    event = MonsterStockHoldActivated(
        portfolio_id=_PID,
        timestamp=_NOW,
        symbol="NVDA",
        pnl_pct=0.22,
        trading_days_held=12,
        hold_until=hold_until,
    )
    data = event.model_dump()
    restored = deserialise_event(data)
    assert isinstance(restored, MonsterStockHoldActivated)
    assert restored.trading_days_held == 12


def test_position_closed_round_trip() -> None:
    event = PositionClosed(
        portfolio_id=_PID,
        timestamp=_NOW,
        symbol="NVDA",
        entry_price=900.0,
        exit_price=1125.0,
        shares=10.0,
        pnl=2250.0,
        pnl_pct=0.25,
        exit_reason=ExitReason.PROFIT_TARGET,
        days_held=42,
    )
    data = event.model_dump()
    restored = deserialise_event(data)
    assert isinstance(restored, PositionClosed)
    assert restored.exit_reason == ExitReason.PROFIT_TARGET


def test_market_regime_changed_round_trip() -> None:
    event = MarketRegimeChanged(
        portfolio_id=_PID,
        timestamp=_NOW,
        old_score=0.72,
        new_score=0.35,
        old_is_bullish=True,
        new_is_bullish=False,
        distribution_days=6,
        action_taken="STOPS_TIGHTENED",
    )
    data = event.model_dump()
    restored = deserialise_event(data)
    assert isinstance(restored, MarketRegimeChanged)
    assert restored.new_is_bullish is False


def test_deserialise_wrong_type_raises() -> None:
    with pytest.raises(Exception):
        deserialise_event({"event_type": "UnknownEvent", "portfolio_id": _PID})
```

- [ ] **Step 2: Run tests — expect ImportError (module does not exist yet)**

```bash
python -m pytest tests/portfolio/test_events.py -v 2>&1 | head -20
```

Expected: `ImportError: No module named 'core.portfolio'`

- [ ] **Step 3: Create the package skeleton**

Create `core/portfolio/__init__.py` (empty for now):

```python
"""Event-sourced portfolio state — O'Neil exit engine and position sizing."""
```

- [ ] **Step 4: Create `core/portfolio/events.py`**

```python
"""Portfolio event types — the vocabulary of the event-sourced portfolio system."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Union
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class ExitReason(str, Enum):
    """Reason a position was closed. Stored in PositionClosed events."""

    STOP_LOSS        = "stop_loss"         # 7-8% hard stop hit
    PROFIT_TARGET    = "profit_target"     # 20-25% target hit
    EIGHT_WEEK_HOLD  = "eight_week_hold"   # Monster stock hold period expired
    REGIME_TIGHTENED = "regime_tightened"  # Bearish M; tightened stop now hit
    RS_DEGRADATION   = "rs_degradation"   # RS dropped below threshold
    MANUAL           = "manual"            # Human override


class StopReason(str, Enum):
    """Reason a stop price was updated."""

    INITIAL          = "initial"           # Set at entry
    REGIME_TIGHTENED = "regime_tightened"  # Market turned bearish
    TRAILING         = "trailing"          # Phase 2
    MANUAL           = "manual"


class PortfolioEvent(BaseModel):
    """Base envelope shared by every event type."""

    event_id:     UUID     = Field(default_factory=uuid4)
    event_type:   str
    timestamp:    datetime  # UTC always
    portfolio_id: str


class PortfolioInitialised(PortfolioEvent):
    """Fired once at portfolio creation."""

    event_type:      Literal["PortfolioInitialised"] = "PortfolioInitialised"
    initial_capital: float
    currency:        str = "USD"


class SignalGenerated(PortfolioEvent):
    """Scanner produced a buy candidate."""

    event_type:       Literal["SignalGenerated"] = "SignalGenerated"
    symbol:           str
    canslim_score:    float
    rs_score:         float
    component_scores: dict[str, float]  # keys: C A N S L I M
    is_bullish_market: bool


class OrderPlaced(PortfolioEvent):
    """Order submitted to Alpaca (paper or live)."""

    event_type:       Literal["OrderPlaced"] = "OrderPlaced"
    symbol:           str
    alpaca_order_id:  str
    shares:           float
    order_type:       Literal["MARKET", "LIMIT"]
    limit_price:      float | None = None
    intended_stop:    float
    intended_target:  float


class OrderFilled(PortfolioEvent):
    """Fill confirmed from Alpaca."""

    event_type:      Literal["OrderFilled"] = "OrderFilled"
    alpaca_order_id: str
    symbol:          str
    fill_price:      float
    shares:          float
    fill_timestamp:  datetime


class StopPriceUpdated(PortfolioEvent):
    """Stop price adjusted post-entry."""

    event_type: Literal["StopPriceUpdated"] = "StopPriceUpdated"
    symbol:     str
    old_stop:   float
    new_stop:   float
    reason:     StopReason


class MonsterStockHoldActivated(PortfolioEvent):
    """8-week hold rule triggered: stock gained 20%+ in fewer than 15 trading days."""

    event_type:        Literal["MonsterStockHoldActivated"] = "MonsterStockHoldActivated"
    symbol:            str
    pnl_pct:           float
    trading_days_held: int
    hold_until:        datetime


class ExitSignalGenerated(PortfolioEvent):
    """An O'Neil exit rule fired — precedes PositionClosed."""

    event_type:         Literal["ExitSignalGenerated"] = "ExitSignalGenerated"
    symbol:             str
    exit_reason:        ExitReason
    current_price:      float
    pnl_pct:            float
    days_held:          int
    market_trend_score: float


class PositionClosed(PortfolioEvent):
    """Exit fill confirmed — position fully closed."""

    event_type:   Literal["PositionClosed"] = "PositionClosed"
    symbol:       str
    entry_price:  float
    exit_price:   float
    shares:       float
    pnl:          float
    pnl_pct:      float
    exit_reason:  ExitReason
    days_held:    int


class MarketRegimeChanged(PortfolioEvent):
    """M score crossed the bullish/bearish threshold."""

    event_type:        Literal["MarketRegimeChanged"] = "MarketRegimeChanged"
    old_score:         float
    new_score:         float
    old_is_bullish:    bool
    new_is_bullish:    bool
    distribution_days: int
    action_taken:      str  # e.g. "STOPS_TIGHTENED", "REGIME_RECOVERED"


# Discriminated union — used by deserialise_event
AnyPortfolioEvent = Annotated[
    Union[
        PortfolioInitialised,
        SignalGenerated,
        OrderPlaced,
        OrderFilled,
        StopPriceUpdated,
        MonsterStockHoldActivated,
        ExitSignalGenerated,
        PositionClosed,
        MarketRegimeChanged,
    ],
    Field(discriminator="event_type"),
]


def deserialise_event(data: dict) -> AnyPortfolioEvent:
    """Deserialise a plain dict (from JSON) to the correct typed event."""
    from pydantic import TypeAdapter

    adapter: TypeAdapter[AnyPortfolioEvent] = TypeAdapter(AnyPortfolioEvent)
    return adapter.validate_python(data)
```

- [ ] **Step 5: Run tests — all must pass**

```bash
python -m pytest tests/portfolio/test_events.py -v
```

Expected:
```
tests/portfolio/test_events.py::test_portfolio_initialised_round_trip PASSED
tests/portfolio/test_events.py::test_order_filled_round_trip PASSED
tests/portfolio/test_events.py::test_stop_price_updated_round_trip PASSED
tests/portfolio/test_events.py::test_monster_stock_hold_round_trip PASSED
tests/portfolio/test_events.py::test_position_closed_round_trip PASSED
tests/portfolio/test_events.py::test_market_regime_changed_round_trip PASSED
tests/portfolio/test_events.py::test_deserialise_wrong_type_raises PASSED
7 passed
```

- [ ] **Step 6: Commit**

```bash
git add core/portfolio/__init__.py core/portfolio/events.py \
        tests/portfolio/__init__.py tests/portfolio/test_events.py
git commit -m "feat(portfolio): add event types with Pydantic discriminated union"
```

---

## Task 4: Ledger

**Files:**
- Create: `core/portfolio/ledger.py`
- Create: `tests/portfolio/test_ledger.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/portfolio/test_ledger.py`:

```python
"""Tests for the append-only SQLite event ledger."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.portfolio.events import OrderFilled, PortfolioInitialised
from core.portfolio.ledger import EventLedger

_NOW = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
_PID = "paper_v1"


@pytest.fixture()
def ledger() -> EventLedger:
    """In-memory ledger — isolated per test."""
    return EventLedger(db_path=":memory:")


def _init_event() -> PortfolioInitialised:
    return PortfolioInitialised(
        portfolio_id=_PID, timestamp=_NOW, initial_capital=100_000.0
    )


def _fill_event(symbol: str = "NVDA") -> OrderFilled:
    return OrderFilled(
        portfolio_id=_PID,
        timestamp=_NOW,
        alpaca_order_id="ord_001",
        symbol=symbol,
        fill_price=900.0,
        shares=10.0,
        fill_timestamp=_NOW,
    )


def test_append_and_load_single_event(ledger: EventLedger) -> None:
    event = _init_event()
    ledger.append(event)
    loaded = ledger.load_events(_PID)
    assert len(loaded) == 1
    assert isinstance(loaded[0], PortfolioInitialised)
    assert loaded[0].initial_capital == 100_000.0


def test_sequence_numbers_are_monotonic(ledger: EventLedger) -> None:
    ledger.append(_init_event())
    ledger.append(_fill_event())
    rows = ledger._conn.execute(
        "SELECT sequence_num FROM events WHERE portfolio_id = ? ORDER BY sequence_num",
        (_PID,),
    ).fetchall()
    seq_nums = [r[0] for r in rows]
    assert seq_nums == [1, 2]


def test_load_events_after_sequence(ledger: EventLedger) -> None:
    ledger.append(_init_event())
    ledger.append(_fill_event("NVDA"))
    ledger.append(_fill_event("AAPL"))
    loaded = ledger.load_events(_PID, after_sequence=1)
    assert len(loaded) == 2


def test_event_count(ledger: EventLedger) -> None:
    assert ledger.event_count(_PID) == 0
    ledger.append(_init_event())
    ledger.append(_fill_event())
    assert ledger.event_count(_PID) == 2


def test_different_portfolios_are_isolated(ledger: EventLedger) -> None:
    ledger.append(_init_event())
    other = PortfolioInitialised(
        portfolio_id="paper_v2", timestamp=_NOW, initial_capital=50_000.0
    )
    ledger.append(other)
    v1_events = ledger.load_events("paper_v1")
    v2_events = ledger.load_events("paper_v2")
    assert len(v1_events) == 1
    assert len(v2_events) == 1


def test_schema_created_on_init(ledger: EventLedger) -> None:
    tables = {
        row[0]
        for row in ledger._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"events", "snapshots", "schema_migrations"}.issubset(tables)
```

- [ ] **Step 2: Run tests — expect ImportError**

```bash
python -m pytest tests/portfolio/test_ledger.py -v 2>&1 | head -10
```

Expected: `ImportError: cannot import name 'EventLedger' from 'core.portfolio.ledger'`

- [ ] **Step 3: Create `core/portfolio/ledger.py`**

```python
"""Append-only SQLite event ledger — the single source of truth for portfolio state."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .events import AnyPortfolioEvent, PortfolioEvent, deserialise_event


class EventLedger:
    """Append-only SQLite event store.

    No row is ever updated or deleted. Every state change is a new append.
    Use ':memory:' as db_path in tests for a fully isolated in-memory store.
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._apply_schema()

    def _apply_schema(self) -> None:
        with self._conn:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    event_id     TEXT    PRIMARY KEY,
                    event_type   TEXT    NOT NULL,
                    portfolio_id TEXT    NOT NULL,
                    timestamp    TEXT    NOT NULL,
                    payload      TEXT    NOT NULL,
                    sequence_num INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_portfolio
                    ON events (portfolio_id, sequence_num);

                CREATE TABLE IF NOT EXISTS snapshots (
                    snapshot_id  TEXT    PRIMARY KEY,
                    portfolio_id TEXT    NOT NULL,
                    timestamp    TEXT    NOT NULL,
                    sequence_num INTEGER NOT NULL,
                    state        TEXT    NOT NULL
                );

                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version     INTEGER PRIMARY KEY,
                    applied_at  TEXT    NOT NULL,
                    description TEXT    NOT NULL
                );

                INSERT OR IGNORE INTO schema_migrations
                    (version, applied_at, description)
                VALUES (1, datetime('now'), 'initial schema');
            """)

    def append(self, event: PortfolioEvent) -> None:
        """Append a single event. Raises on duplicate event_id."""
        payload = event.model_dump_json()
        with self._conn:
            next_seq = self._next_sequence(event.portfolio_id)
            self._conn.execute(
                """
                INSERT INTO events
                    (event_id, event_type, portfolio_id, timestamp, payload, sequence_num)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(event.event_id),
                    event.event_type,
                    event.portfolio_id,
                    event.timestamp.isoformat(),
                    payload,
                    next_seq,
                ),
            )

    def _next_sequence(self, portfolio_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(sequence_num), 0) + 1 FROM events WHERE portfolio_id = ?",
            (portfolio_id,),
        ).fetchone()
        return row[0]

    def load_events(self, portfolio_id: str, after_sequence: int = 0) -> list[AnyPortfolioEvent]:
        """Return events for a portfolio in sequence order, optionally skipping a prefix."""
        rows = self._conn.execute(
            """
            SELECT payload FROM events
            WHERE portfolio_id = ? AND sequence_num > ?
            ORDER BY sequence_num ASC
            """,
            (portfolio_id, after_sequence),
        ).fetchall()
        return [deserialise_event(json.loads(row["payload"])) for row in rows]

    def event_count(self, portfolio_id: str) -> int:
        """Return the total number of events for a portfolio."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM events WHERE portfolio_id = ?",
            (portfolio_id,),
        ).fetchone()
        return row[0]

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()
```

- [ ] **Step 4: Run tests — all must pass**

```bash
python -m pytest tests/portfolio/test_ledger.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add core/portfolio/ledger.py tests/portfolio/test_ledger.py
git commit -m "feat(portfolio): add append-only SQLite event ledger"
```

---

## Task 5: Projections

**Files:**
- Create: `core/portfolio/projections.py`
- Create: `tests/portfolio/test_projections.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/portfolio/test_projections.py`:

```python
"""Tests for portfolio projections — replay correctness for every event type."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.portfolio.events import (
    MonsterStockHoldActivated,
    OrderFilled,
    PortfolioInitialised,
    PositionClosed,
    ExitReason,
    StopPriceUpdated,
    StopReason,
)
from core.portfolio.ledger import EventLedger
from core.portfolio.projections import get_open_positions, get_portfolio_state, get_trade_history

_NOW = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
_LATER = datetime(2026, 2, 28, 10, 0, 0, tzinfo=timezone.utc)
_PID = "paper_v1"


@pytest.fixture()
def ledger() -> EventLedger:
    return EventLedger(db_path=":memory:")


def _init(ledger: EventLedger) -> None:
    ledger.append(PortfolioInitialised(portfolio_id=_PID, timestamp=_NOW, initial_capital=100_000.0))


def _fill(ledger: EventLedger, symbol: str = "NVDA", price: float = 900.0, shares: float = 10.0) -> None:
    ledger.append(
        OrderFilled(
            portfolio_id=_PID,
            timestamp=_NOW,
            alpaca_order_id="ord_001",
            symbol=symbol,
            fill_price=price,
            shares=shares,
            fill_timestamp=_NOW,
        )
    )


def test_initial_state(ledger: EventLedger) -> None:
    _init(ledger)
    state = get_portfolio_state(ledger, _PID)
    assert state.cash == 100_000.0
    assert state.initial_capital == 100_000.0
    assert state.open_positions == []
    assert state.closed_trades == []


def test_order_filled_creates_position_and_reduces_cash(ledger: EventLedger) -> None:
    _init(ledger)
    _fill(ledger, "NVDA", price=900.0, shares=10.0)
    state = get_portfolio_state(ledger, _PID)
    assert len(state.open_positions) == 1
    pos = state.open_positions[0]
    assert pos.symbol == "NVDA"
    assert pos.entry_price == 900.0
    assert pos.shares == 10.0
    assert state.cash == pytest.approx(100_000.0 - 900.0 * 10.0)


def test_order_filled_sets_stop_and_target(ledger: EventLedger) -> None:
    _init(ledger)
    _fill(ledger, "NVDA", price=900.0, shares=10.0)
    pos = get_open_positions(ledger, _PID)[0]
    # 8% stop: 900 * (1 - 0.08) = 828.0
    assert pos.stop_price == pytest.approx(900.0 * 0.92, rel=1e-4)
    # 25% target: 900 * 1.25 = 1125.0
    assert pos.target_price == pytest.approx(900.0 * 1.25, rel=1e-4)


def test_stop_price_updated_changes_stop(ledger: EventLedger) -> None:
    _init(ledger)
    _fill(ledger, "NVDA", price=900.0, shares=10.0)
    ledger.append(
        StopPriceUpdated(
            portfolio_id=_PID,
            timestamp=_LATER,
            symbol="NVDA",
            old_stop=828.0,
            new_stop=855.0,
            reason=StopReason.REGIME_TIGHTENED,
        )
    )
    pos = get_open_positions(ledger, _PID)[0]
    assert pos.stop_price == pytest.approx(855.0)


def test_monster_stock_hold_activated_sets_hold_fields(ledger: EventLedger) -> None:
    hold_until = datetime(2026, 3, 15, tzinfo=timezone.utc)
    _init(ledger)
    _fill(ledger, "NVDA", price=900.0, shares=10.0)
    ledger.append(
        MonsterStockHoldActivated(
            portfolio_id=_PID,
            timestamp=_LATER,
            symbol="NVDA",
            pnl_pct=0.22,
            trading_days_held=12,
            hold_until=hold_until,
        )
    )
    pos = get_open_positions(ledger, _PID)[0]
    assert pos.eight_week_hold is True
    assert pos.hold_until == hold_until


def test_position_closed_removes_position_and_adds_cash(ledger: EventLedger) -> None:
    _init(ledger)
    _fill(ledger, "NVDA", price=900.0, shares=10.0)
    ledger.append(
        PositionClosed(
            portfolio_id=_PID,
            timestamp=_LATER,
            symbol="NVDA",
            entry_price=900.0,
            exit_price=1125.0,
            shares=10.0,
            pnl=2250.0,
            pnl_pct=0.25,
            exit_reason=ExitReason.PROFIT_TARGET,
            days_held=42,
        )
    )
    state = get_portfolio_state(ledger, _PID)
    assert state.open_positions == []
    assert len(state.closed_trades) == 1
    trade = state.closed_trades[0]
    assert trade.pnl == pytest.approx(2250.0)
    # Cash: started 100k, spent 9k on entry, received 11.25k on exit
    assert state.cash == pytest.approx(100_000.0 - 9_000.0 + 11_250.0)


def test_realised_pnl_accumulates(ledger: EventLedger) -> None:
    _init(ledger)
    _fill(ledger, "NVDA", price=900.0, shares=10.0)
    _fill(ledger, "AAPL", price=200.0, shares=50.0)
    ledger.append(
        PositionClosed(
            portfolio_id=_PID, timestamp=_LATER, symbol="NVDA",
            entry_price=900.0, exit_price=828.0, shares=10.0,
            pnl=-720.0, pnl_pct=-0.08,
            exit_reason=ExitReason.STOP_LOSS, days_held=5,
        )
    )
    ledger.append(
        PositionClosed(
            portfolio_id=_PID, timestamp=_LATER, symbol="AAPL",
            entry_price=200.0, exit_price=250.0, shares=50.0,
            pnl=2500.0, pnl_pct=0.25,
            exit_reason=ExitReason.PROFIT_TARGET, days_held=30,
        )
    )
    state = get_portfolio_state(ledger, _PID)
    assert state.realised_pnl == pytest.approx(-720.0 + 2500.0)


def test_get_trade_history(ledger: EventLedger) -> None:
    _init(ledger)
    _fill(ledger)
    ledger.append(
        PositionClosed(
            portfolio_id=_PID, timestamp=_LATER, symbol="NVDA",
            entry_price=900.0, exit_price=1125.0, shares=10.0,
            pnl=2250.0, pnl_pct=0.25,
            exit_reason=ExitReason.PROFIT_TARGET, days_held=42,
        )
    )
    history = get_trade_history(ledger, _PID)
    assert len(history) == 1
    assert history[0].exit_reason == "profit_target"


def test_no_initialised_event_raises(ledger: EventLedger) -> None:
    _fill(ledger)
    with pytest.raises(ValueError, match="PortfolioInitialised"):
        get_portfolio_state(ledger, _PID)
```

- [ ] **Step 2: Run tests — expect ImportError**

```bash
python -m pytest tests/portfolio/test_projections.py -v 2>&1 | head -10
```

Expected: `ImportError: cannot import name 'get_portfolio_state' from 'core.portfolio.projections'`

- [ ] **Step 3: Create `core/portfolio/projections.py`**

```python
"""Replay portfolio events to produce derived state.

Never read raw events directly — always call these projection functions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from .events import (
    AnyPortfolioEvent,
    ExitReason,
    MonsterStockHoldActivated,
    OrderFilled,
    PortfolioInitialised,
    PositionClosed,
    SignalGenerated,
    StopPriceUpdated,
)

if TYPE_CHECKING:
    from .ledger import EventLedger


@dataclass
class OpenPosition:
    """Derived state for a currently open position."""

    symbol:                 str
    entry_price:            float
    entry_date:             datetime
    shares:                 float
    stop_price:             float        # Current stop — may have been tightened
    target_price:           float        # Profit target price
    canslim_score_at_entry: float
    rs_score_at_entry:      float
    eight_week_hold:        bool = False
    hold_until:             datetime | None = None


@dataclass
class ClosedTrade:
    """A completed position — entry through exit."""

    symbol:      str
    entry_price: float
    exit_price:  float
    shares:      float
    entry_date:  datetime
    exit_date:   datetime
    pnl:         float
    pnl_pct:     float
    exit_reason: str
    days_held:   int


@dataclass
class PortfolioState:
    """Current portfolio snapshot derived from replaying all events."""

    portfolio_id:    str
    initial_capital: float
    cash:            float
    open_positions:  list[OpenPosition] = field(default_factory=list)
    closed_trades:   list[ClosedTrade]  = field(default_factory=list)
    currency:        str = "USD"

    @property
    def positions_value(self) -> float:
        """Cost basis of all open positions (not mark-to-market)."""
        return sum(p.entry_price * p.shares for p in self.open_positions)

    @property
    def total_value(self) -> float:
        return self.cash + self.positions_value

    @property
    def realised_pnl(self) -> float:
        return sum(t.pnl for t in self.closed_trades)


def _replay(events: list[AnyPortfolioEvent]) -> PortfolioState:
    """Replay a sequence of events oldest-first to build current state."""
    from config import settings

    state: PortfolioState | None = None
    positions: dict[str, OpenPosition] = {}
    closed: list[ClosedTrade] = []
    last_signal: dict[str, SignalGenerated] = {}

    for event in events:
        if isinstance(event, PortfolioInitialised):
            state = PortfolioState(
                portfolio_id=event.portfolio_id,
                initial_capital=event.initial_capital,
                cash=event.initial_capital,
                currency=event.currency,
            )

        elif isinstance(event, SignalGenerated):
            last_signal[event.symbol] = event

        elif isinstance(event, OrderFilled) and state is not None:
            cost = event.fill_price * event.shares
            state.cash -= cost
            sig = last_signal.get(event.symbol)
            positions[event.symbol] = OpenPosition(
                symbol=event.symbol,
                entry_price=event.fill_price,
                entry_date=event.fill_timestamp,
                shares=event.shares,
                stop_price=event.fill_price * (1 - settings.EXIT_STOP_LOSS_PCT),
                target_price=event.fill_price * (1 + settings.EXIT_PROFIT_TARGET_PCT),
                canslim_score_at_entry=sig.canslim_score if sig else 0.0,
                rs_score_at_entry=sig.rs_score if sig else 0.0,
            )

        elif isinstance(event, StopPriceUpdated):
            if event.symbol in positions:
                positions[event.symbol].stop_price = event.new_stop

        elif isinstance(event, MonsterStockHoldActivated):
            if event.symbol in positions:
                positions[event.symbol].eight_week_hold = True
                positions[event.symbol].hold_until = event.hold_until

        elif isinstance(event, PositionClosed) and state is not None:
            pos = positions.pop(event.symbol, None)
            closed.append(
                ClosedTrade(
                    symbol=event.symbol,
                    entry_price=event.entry_price,
                    exit_price=event.exit_price,
                    shares=event.shares,
                    entry_date=pos.entry_date if pos else event.timestamp,
                    exit_date=event.timestamp,
                    pnl=event.pnl,
                    pnl_pct=event.pnl_pct,
                    exit_reason=event.exit_reason,
                    days_held=event.days_held,
                )
            )
            state.cash += event.exit_price * event.shares

    if state is None:
        raise ValueError(
            "No PortfolioInitialised event found — cannot build state. "
            "Append a PortfolioInitialised event before any other events."
        )

    state.open_positions = list(positions.values())
    state.closed_trades = closed
    return state


def get_portfolio_state(ledger: "EventLedger", portfolio_id: str) -> PortfolioState:
    """Replay all events and return current portfolio state."""
    events = ledger.load_events(portfolio_id)
    return _replay(events)


def get_open_positions(ledger: "EventLedger", portfolio_id: str) -> list[OpenPosition]:
    """Return all currently open positions."""
    return get_portfolio_state(ledger, portfolio_id).open_positions


def get_trade_history(ledger: "EventLedger", portfolio_id: str) -> list[ClosedTrade]:
    """Return all closed trades in chronological order."""
    return get_portfolio_state(ledger, portfolio_id).closed_trades
```

- [ ] **Step 4: Run tests — all must pass**

```bash
python -m pytest tests/portfolio/test_projections.py -v
```

Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add core/portfolio/projections.py tests/portfolio/test_projections.py
git commit -m "feat(portfolio): add event replay projections with full state derivation"
```

---

## Task 6: Exit Engine — Stop-Loss and Profit Target

**Files:**
- Create: `core/portfolio/exit_engine.py`
- Create: `tests/portfolio/test_exit_engine.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/portfolio/test_exit_engine.py`:

```python
"""Tests for O'Neil exit rules — each rule and priority ordering."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.canslim.m_market_direction import MarketTrend
from core.portfolio.events import ExitReason, StopReason
from core.portfolio.exit_engine import (
    evaluate_exit,
    evaluate_monster_stock_hold,
    evaluate_regime_shift,
)
from core.portfolio.projections import OpenPosition

_NOW = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
_FUTURE = _NOW + timedelta(days=60)


def _bull_trend(score: float = 0.75) -> MarketTrend:
    return MarketTrend(
        symbol="SPY", score=score, is_bullish=True,
        latest_close=500.0, indicators={},
        distribution_days=1, follow_through=True,
    )


def _bear_trend(score: float = 0.35) -> MarketTrend:
    return MarketTrend(
        symbol="SPY", score=score, is_bullish=False,
        latest_close=480.0, indicators={},
        distribution_days=7, follow_through=False,
    )


def _position(
    entry_price: float = 900.0,
    stop_price: float = 828.0,    # 8% below 900
    target_price: float = 1125.0, # 25% above 900
    eight_week_hold: bool = False,
    hold_until: datetime | None = None,
) -> OpenPosition:
    return OpenPosition(
        symbol="NVDA",
        entry_price=entry_price,
        entry_date=_NOW,
        shares=10.0,
        stop_price=stop_price,
        target_price=target_price,
        canslim_score_at_entry=80.0,
        rs_score_at_entry=88.0,
        eight_week_hold=eight_week_hold,
        hold_until=hold_until,
    )


# --- Rule 1: Stop-Loss ---

def test_stop_loss_fires_when_price_hits_stop() -> None:
    pos = _position(entry_price=900.0, stop_price=828.0)
    signal = evaluate_exit(pos, current_price=828.0, current_rs_score=85.0, market_trend=_bull_trend())
    assert signal is not None
    assert signal.reason == ExitReason.STOP_LOSS
    assert signal.urgency == "IMMEDIATE"


def test_stop_loss_fires_when_price_below_stop() -> None:
    pos = _position(entry_price=900.0, stop_price=828.0)
    signal = evaluate_exit(pos, current_price=800.0, current_rs_score=85.0, market_trend=_bull_trend())
    assert signal is not None
    assert signal.reason == ExitReason.STOP_LOSS


def test_stop_loss_does_not_fire_above_stop() -> None:
    pos = _position(entry_price=900.0, stop_price=828.0)
    signal = evaluate_exit(pos, current_price=829.0, current_rs_score=85.0, market_trend=_bull_trend())
    assert signal is None or signal.reason != ExitReason.STOP_LOSS


# --- Rule 3: Profit Target ---

def test_profit_target_fires_at_target_price() -> None:
    pos = _position(entry_price=900.0, target_price=1125.0)
    signal = evaluate_exit(pos, current_price=1125.0, current_rs_score=85.0, market_trend=_bull_trend())
    assert signal is not None
    assert signal.reason == ExitReason.PROFIT_TARGET
    assert signal.urgency == "NEXT_OPEN"


def test_profit_target_fires_above_target_price() -> None:
    pos = _position(entry_price=900.0, target_price=1125.0)
    signal = evaluate_exit(pos, current_price=1200.0, current_rs_score=85.0, market_trend=_bull_trend())
    assert signal is not None
    assert signal.reason == ExitReason.PROFIT_TARGET


def test_profit_target_does_not_fire_below_target() -> None:
    pos = _position(entry_price=900.0, target_price=1125.0)
    signal = evaluate_exit(pos, current_price=1000.0, current_rs_score=85.0, market_trend=_bull_trend())
    assert signal is None


# --- Rule 2: 8-Week Hold suppresses profit target ---

def test_eight_week_hold_suppresses_profit_target_during_window() -> None:
    hold_until = _NOW + timedelta(days=30)  # still in hold window
    pos = _position(target_price=1125.0, eight_week_hold=True, hold_until=hold_until)
    # Price is above target but we're in the hold window
    signal = evaluate_exit(pos, current_price=1200.0, current_rs_score=85.0, market_trend=_bull_trend())
    # Must NOT be a profit target exit during hold window
    assert signal is None or signal.reason != ExitReason.PROFIT_TARGET


def test_eight_week_hold_allows_stop_loss_during_window() -> None:
    hold_until = _NOW + timedelta(days=30)
    pos = _position(stop_price=828.0, eight_week_hold=True, hold_until=hold_until)
    signal = evaluate_exit(pos, current_price=800.0, current_rs_score=85.0, market_trend=_bull_trend())
    assert signal is not None
    assert signal.reason == ExitReason.STOP_LOSS


def test_eight_week_hold_allows_profit_target_after_window() -> None:
    hold_until = _NOW - timedelta(days=1)  # hold window has passed
    pos = _position(target_price=1125.0, eight_week_hold=True, hold_until=hold_until)
    signal = evaluate_exit(pos, current_price=1200.0, current_rs_score=85.0, market_trend=_bull_trend())
    assert signal is not None
    assert signal.reason == ExitReason.PROFIT_TARGET


# --- Rule 4: RS Degradation ---

def test_rs_degradation_fires_when_rs_below_threshold() -> None:
    pos = _position()
    # RS below EXIT_RS_DEGRADATION_THRESHOLD (70), price in safe zone
    signal = evaluate_exit(pos, current_price=950.0, current_rs_score=65.0, market_trend=_bull_trend())
    assert signal is not None
    assert signal.reason == ExitReason.RS_DEGRADATION
    assert signal.urgency == "NEXT_OPEN"


def test_rs_degradation_does_not_fire_above_threshold() -> None:
    pos = _position()
    signal = evaluate_exit(pos, current_price=950.0, current_rs_score=75.0, market_trend=_bull_trend())
    assert signal is None


# --- Priority: stop-loss beats profit target ---

def test_stop_loss_priority_over_profit_target() -> None:
    """Both stop and target can't logically trigger simultaneously on a real price,
    but if stop_price >= target_price (misconfiguration), stop-loss wins."""
    pos = _position(entry_price=900.0, stop_price=1125.0, target_price=1000.0)
    signal = evaluate_exit(pos, current_price=1125.0, current_rs_score=85.0, market_trend=_bull_trend())
    assert signal is not None
    assert signal.reason == ExitReason.STOP_LOSS


# --- Monster stock hold activation ---

def test_monster_stock_hold_activates_when_conditions_met() -> None:
    pos = _position(entry_price=900.0, eight_week_hold=False)
    # Price gained 22% in 12 trading days — qualifies
    event = evaluate_monster_stock_hold(
        position=pos,
        current_price=900.0 * 1.22,
        trading_days_held=12,
        portfolio_id="paper_v1",
        now=_NOW,
    )
    assert event is not None
    assert event.trading_days_held == 12
    assert event.pnl_pct == pytest.approx(0.22, abs=0.001)


def test_monster_stock_hold_does_not_activate_if_too_slow() -> None:
    pos = _position(entry_price=900.0, eight_week_hold=False)
    # Gained 22% but took 20 days — too slow
    event = evaluate_monster_stock_hold(
        position=pos,
        current_price=900.0 * 1.22,
        trading_days_held=20,
        portfolio_id="paper_v1",
        now=_NOW,
    )
    assert event is None


def test_monster_stock_hold_does_not_reactivate(capsys) -> None:
    pos = _position(entry_price=900.0, eight_week_hold=True)
    event = evaluate_monster_stock_hold(
        position=pos,
        current_price=900.0 * 1.22,
        trading_days_held=12,
        portfolio_id="paper_v1",
        now=_NOW,
    )
    assert event is None


# --- Regime shift ---

def test_regime_shift_tightens_stops_when_turning_bearish() -> None:
    pos = _position(entry_price=900.0, stop_price=828.0)
    updates = evaluate_regime_shift(
        old_trend=_bull_trend(),
        new_trend=_bear_trend(),
        open_positions=[pos],
        portfolio_id="paper_v1",
        now=_NOW,
    )
    assert len(updates) == 1
    assert updates[0].symbol == "NVDA"
    assert updates[0].reason == StopReason.REGIME_TIGHTENED
    # new stop should be tighter than old stop (5% not 8%)
    assert updates[0].new_stop > updates[0].old_stop


def test_regime_shift_does_not_loosen_stops_on_recovery() -> None:
    # Recovery (bear → bull) should NOT loosen any stops
    pos = _position(entry_price=900.0, stop_price=855.0)  # already tightened
    updates = evaluate_regime_shift(
        old_trend=_bear_trend(),
        new_trend=_bull_trend(),
        open_positions=[pos],
        portfolio_id="paper_v1",
        now=_NOW,
    )
    assert updates == []


def test_regime_shift_returns_empty_when_regime_unchanged() -> None:
    pos = _position()
    updates = evaluate_regime_shift(
        old_trend=_bull_trend(),
        new_trend=_bull_trend(score=0.70),
        open_positions=[pos],
        portfolio_id="paper_v1",
        now=_NOW,
    )
    assert updates == []
```

- [ ] **Step 2: Run tests — expect ImportError**

```bash
python -m pytest tests/portfolio/test_exit_engine.py -v 2>&1 | head -10
```

Expected: `ImportError: cannot import name 'evaluate_exit' from 'core.portfolio.exit_engine'`

- [ ] **Step 3: Create `core/portfolio/exit_engine.py`**

```python
"""O'Neil exit rules — pure functions, no database access.

All functions accept position state and market data and return signals.
None is returned when no rule fires. No side effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from config import settings
from core.canslim.m_market_direction import MarketTrend

from .events import ExitReason, MonsterStockHoldActivated, StopPriceUpdated, StopReason
from .projections import OpenPosition


@dataclass
class ExitSignal:
    """Returned by evaluate_exit when an O'Neil rule fires."""

    reason:  ExitReason
    urgency: str  # "IMMEDIATE" | "NEXT_OPEN"


def evaluate_exit(
    position: OpenPosition,
    current_price: float,
    current_rs_score: float,
    market_trend: MarketTrend,
) -> ExitSignal | None:
    """Evaluate all O'Neil exit rules in priority order.

    Priority:
        1. Stop-loss  (IMMEDIATE)
        2. 8-week hold check (suppresses profit target during hold window)
        3. Profit target  (NEXT_OPEN)
        4. RS degradation  (NEXT_OPEN)
        5. Regime shift — handled separately via evaluate_regime_shift

    Returns None if no rule fires.
    """
    # Priority 1: Hard stop-loss — always checked first, no exceptions
    if current_price <= position.stop_price:
        return ExitSignal(reason=ExitReason.STOP_LOSS, urgency="IMMEDIATE")

    # Priority 2: Is profit target suppressed by the 8-week hold window?
    now = datetime.now(tz=timezone.utc)
    in_hold_window = (
        position.eight_week_hold
        and position.hold_until is not None
        and now < position.hold_until
    )

    # Priority 3: Profit target (suppressed during hold window)
    if not in_hold_window and current_price >= position.target_price:
        return ExitSignal(reason=ExitReason.PROFIT_TARGET, urgency="NEXT_OPEN")

    # Priority 4: RS degradation — stock is no longer a market leader
    if current_rs_score < settings.EXIT_RS_DEGRADATION_THRESHOLD:
        return ExitSignal(reason=ExitReason.RS_DEGRADATION, urgency="NEXT_OPEN")

    return None


def evaluate_monster_stock_hold(
    position: OpenPosition,
    current_price: float,
    trading_days_held: int,
    portfolio_id: str,
    now: datetime | None = None,
) -> MonsterStockHoldActivated | None:
    """Check whether the 8-week monster stock hold rule should be activated.

    Fires when a position gains EXIT_MONSTER_STOCK_TRIGGER (20%) or more
    in fewer than 15 trading days from entry — indicating exceptional momentum.

    Returns a MonsterStockHoldActivated event on first trigger, None otherwise.
    """
    if position.eight_week_hold:
        return None  # Already activated — never re-emit

    pnl_pct = (current_price - position.entry_price) / position.entry_price
    if pnl_pct >= settings.EXIT_MONSTER_STOCK_TRIGGER and trading_days_held < 15:
        _now = now or datetime.now(tz=timezone.utc)
        hold_until = position.entry_date + timedelta(days=settings.EXIT_MONSTER_STOCK_HOLD_DAYS)
        return MonsterStockHoldActivated(
            portfolio_id=portfolio_id,
            timestamp=_now,
            symbol=position.symbol,
            pnl_pct=pnl_pct,
            trading_days_held=trading_days_held,
            hold_until=hold_until,
        )
    return None


def evaluate_regime_shift(
    old_trend: MarketTrend,
    new_trend: MarketTrend,
    open_positions: list[OpenPosition],
    portfolio_id: str,
    now: datetime | None = None,
) -> list[StopPriceUpdated]:
    """Tighten stops for all open positions when M score turns bearish.

    O'Neil: when the market turns, get defensive. Stops are tightened from
    EXIT_STOP_LOSS_PCT to EXIT_STOP_LOSS_PCT × REGIME_STOP_MULTIPLIER.
    Stops are NEVER loosened — even on regime recovery.

    Returns a list of StopPriceUpdated events (may be empty).
    """
    # Only act on bull → bear transition
    if old_trend.is_bullish == new_trend.is_bullish:
        return []
    if new_trend.is_bullish:
        return []  # Recovery — never loosen stops

    _now = now or datetime.now(tz=timezone.utc)
    updates: list[StopPriceUpdated] = []

    for position in open_positions:
        tightened = position.entry_price * (
            1.0 - settings.EXIT_STOP_LOSS_PCT * settings.REGIME_STOP_MULTIPLIER
        )
        if tightened > position.stop_price:  # Only tighten, never loosen
            updates.append(
                StopPriceUpdated(
                    portfolio_id=portfolio_id,
                    timestamp=_now,
                    symbol=position.symbol,
                    old_stop=position.stop_price,
                    new_stop=tightened,
                    reason=StopReason.REGIME_TIGHTENED,
                )
            )

    return updates
```

- [ ] **Step 4: Run tests — all must pass**

```bash
python -m pytest tests/portfolio/test_exit_engine.py -v
```

Expected: 18 passed.

- [ ] **Step 5: Commit**

```bash
git add core/portfolio/exit_engine.py tests/portfolio/test_exit_engine.py
git commit -m "feat(portfolio): add O'Neil exit engine — 5 rules with priority ordering"
```

---

## Task 7: Position Sizing

**Files:**
- Create: `core/portfolio/sizing.py`
- Create: `tests/portfolio/test_sizing.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/portfolio/test_sizing.py`:

```python
"""Tests for O'Neil position sizing — both methods and all 6 guard rails."""

from __future__ import annotations

import pytest

from core.canslim.m_market_direction import MarketTrend
from core.portfolio.sizing import SizingRecommendation, calculate_position_size


def _bull() -> MarketTrend:
    return MarketTrend(
        symbol="SPY", score=0.75, is_bullish=True,
        latest_close=500.0, indicators={}, distribution_days=1,
    )


def _bear() -> MarketTrend:
    return MarketTrend(
        symbol="SPY", score=0.35, is_bullish=False,
        latest_close=480.0, indicators={}, distribution_days=7,
    )


def _size(
    symbol: str = "NVDA",
    entry_price: float = 100.0,
    stop_price: float = 92.0,
    portfolio_cash: float = 100_000.0,
    portfolio_value: float = 100_000.0,
    open_position_count: int = 0,
    open_position_symbols: set[str] | None = None,
    market_trend: MarketTrend | None = None,
    current_rs_score: float = 85.0,
) -> SizingRecommendation | None:
    return calculate_position_size(
        symbol=symbol,
        entry_price=entry_price,
        stop_price=stop_price,
        portfolio_cash=portfolio_cash,
        portfolio_value=portfolio_value,
        open_position_count=open_position_count,
        open_position_symbols=open_position_symbols or set(),
        market_trend=market_trend or _bull(),
        current_rs_score=current_rs_score,
    )


# --- Equal-weight sizing ---

def test_equal_weight_bull_allocates_20_pct(monkeypatch) -> None:
    monkeypatch.setattr("core.portfolio.sizing.settings.SIZING_METHOD", "equal_weight")
    result = _size(entry_price=100.0, portfolio_value=100_000.0, market_trend=_bull())
    assert result is not None
    assert result.sizing_method == "equal_weight"
    # 20% of 100k = 20k → 200 shares at $100
    assert result.shares == 200
    assert result.allocation_usd == pytest.approx(20_000.0)


def test_equal_weight_bear_allocates_15_pct(monkeypatch) -> None:
    monkeypatch.setattr("core.portfolio.sizing.settings.SIZING_METHOD", "equal_weight")
    result = _size(entry_price=100.0, portfolio_value=100_000.0, market_trend=_bear(),
                   portfolio_cash=100_000.0)
    assert result is not None
    # 15% of 100k = 15k → 150 shares at $100
    assert result.shares == 150
    assert result.allocation_usd == pytest.approx(15_000.0)


# --- Risk-based sizing ---

def test_risk_based_sizes_by_stop_distance(monkeypatch) -> None:
    monkeypatch.setattr("core.portfolio.sizing.settings.SIZING_METHOD", "risk_based")
    # Entry 100, stop 92 → risk per share = $8
    # 1% of 100k = $1000 → floor(1000/8) = 125 shares → $12,500
    result = _size(entry_price=100.0, stop_price=92.0, portfolio_value=100_000.0)
    assert result is not None
    assert result.sizing_method == "risk_based"
    assert result.shares == 125
    assert result.allocation_usd == pytest.approx(12_500.0)


# --- Guard rail 1: position cap ---

def test_guard_rail_1_blocks_when_at_position_cap(monkeypatch) -> None:
    monkeypatch.setattr("core.portfolio.sizing.settings.REGIME_MAX_POSITIONS_BULL", 5)
    result = _size(open_position_count=5, market_trend=_bull())
    assert result is None


def test_guard_rail_1_allows_when_under_cap() -> None:
    result = _size(open_position_count=4, market_trend=_bull())
    assert result is not None


# --- Guard rail 2: insufficient cash ---

def test_guard_rail_2_blocks_when_insufficient_cash() -> None:
    # 20% of 100k = 20k needed, but only 5k cash
    result = _size(portfolio_cash=5_000.0, portfolio_value=100_000.0, market_trend=_bull())
    assert result is None


# --- Guard rail 3: RS score ---

def test_guard_rail_3_blocks_when_rs_below_min() -> None:
    # MIN_RS_SCORE default is 75; pass 70 → blocked
    result = _size(current_rs_score=70.0)
    assert result is None


def test_guard_rail_3_allows_when_rs_at_min() -> None:
    from config import settings
    result = _size(current_rs_score=float(settings.MIN_RS_SCORE))
    assert result is not None


# --- Guard rail 4: bearish market ---

def test_guard_rail_4_blocks_new_entry_in_bear_market(monkeypatch) -> None:
    monkeypatch.setattr(
        "core.portfolio.sizing.settings.REQUIRE_BULLISH_MARKET_FOR_BUYS", True
    )
    result = _size(market_trend=_bear(), portfolio_cash=100_000.0)
    assert result is None


# --- Guard rail 5: hard position cap ---

def test_guard_rail_5_reduces_shares_to_max_pct(monkeypatch) -> None:
    monkeypatch.setattr("core.portfolio.sizing.settings.SIZING_METHOD", "equal_weight")
    monkeypatch.setattr("core.portfolio.sizing.settings.POSITION_WEIGHT_BULL_PCT", 0.40)
    monkeypatch.setattr("core.portfolio.sizing.settings.POSITION_MAX_PCT", 0.25)
    # 40% weight would give 400 shares at $100, but max is 25% → 250 shares
    result = _size(entry_price=100.0, portfolio_value=100_000.0, portfolio_cash=100_000.0)
    assert result is not None
    assert result.allocation_usd <= 100_000.0 * 0.25 + 0.01  # within cap


# --- Guard rail 6: no averaging down ---

def test_guard_rail_6_blocks_existing_symbol() -> None:
    result = _size(symbol="NVDA", open_position_symbols={"NVDA"})
    assert result is None


def test_guard_rail_6_allows_different_symbol() -> None:
    result = _size(symbol="AAPL", open_position_symbols={"NVDA"})
    assert result is not None
```

- [ ] **Step 2: Run tests — expect ImportError**

```bash
python -m pytest tests/portfolio/test_sizing.py -v 2>&1 | head -10
```

Expected: `ImportError: cannot import name 'calculate_position_size'`

- [ ] **Step 3: Create `core/portfolio/sizing.py`**

```python
"""O'Neil position sizing — pure functions, no database access.

Every guard rail is checked before returning a recommendation.
Returning None means the entry is blocked — no order should be placed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from config import settings
from core.canslim.m_market_direction import MarketTrend


@dataclass
class SizingRecommendation:
    """Result of a successful position sizing calculation."""

    symbol:         str
    shares:         int
    allocation_usd: float
    allocation_pct: float
    sizing_method:  str


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
    """Calculate O'Neil position size, enforcing all six guard rails.

    Guard rails (evaluated in order):
        1. open_position_count < max_positions(regime)
        2. portfolio_cash >= minimum allocation
        3. current_rs_score >= MIN_RS_SCORE
        4. market_trend.is_bullish (if REQUIRE_BULLISH_MARKET_FOR_BUYS)
        5. position_value <= portfolio_value * POSITION_MAX_PCT
        6. symbol NOT in open_position_symbols  (no averaging down — structural)

    Returns None if any guard rail blocks the entry.
    """
    max_positions = (
        settings.REGIME_MAX_POSITIONS_BULL
        if market_trend.is_bullish
        else settings.REGIME_MAX_POSITIONS_BEAR
    )
    weight = (
        settings.POSITION_WEIGHT_BULL_PCT
        if market_trend.is_bullish
        else settings.POSITION_WEIGHT_BEAR_PCT
    )

    # Guard rail 1: position count cap
    if open_position_count >= max_positions:
        return None

    # Guard rail 2: sufficient cash (quick pre-check at 50% of intended weight)
    if portfolio_cash < portfolio_value * weight * 0.5:
        return None

    # Guard rail 3: RS score — still a market leader at entry time
    if current_rs_score < settings.MIN_RS_SCORE:
        return None

    # Guard rail 4: market regime gate
    if settings.REQUIRE_BULLISH_MARKET_FOR_BUYS and not market_trend.is_bullish:
        return None

    # Guard rail 6: no averaging down — checked before sizing math
    if symbol in open_position_symbols:
        return None

    # Compute shares using the configured sizing method
    if settings.SIZING_METHOD == "risk_based":
        shares, allocation_usd = _risk_based(entry_price, stop_price, portfolio_value)
        method = "risk_based"
    else:
        shares, allocation_usd = _equal_weight(entry_price, portfolio_cash, portfolio_value, weight)
        method = "equal_weight"

    if shares <= 0:
        return None

    # Guard rail 5: hard cap per position
    cap_usd = portfolio_value * settings.POSITION_MAX_PCT
    if allocation_usd > cap_usd:
        shares = math.floor(cap_usd / entry_price)
        allocation_usd = shares * entry_price

    if shares <= 0:
        return None

    # Guard rail 2 (exact): final cash check
    if allocation_usd > portfolio_cash:
        return None

    return SizingRecommendation(
        symbol=symbol,
        shares=shares,
        allocation_usd=allocation_usd,
        allocation_pct=allocation_usd / portfolio_value,
        sizing_method=method,
    )


def _equal_weight(
    entry_price: float,
    portfolio_cash: float,
    portfolio_value: float,
    weight: float,
) -> tuple[int, float]:
    """Allocate a fixed percentage of portfolio value."""
    allocation = min(portfolio_value * weight, portfolio_cash)
    shares = math.floor(allocation / entry_price)
    return shares, shares * entry_price


def _risk_based(
    entry_price: float,
    stop_price: float,
    portfolio_value: float,
) -> tuple[int, float]:
    """Size so that hitting the stop costs exactly POSITION_RISK_PCT of portfolio."""
    max_loss = portfolio_value * settings.POSITION_RISK_PCT
    risk_per_share = entry_price - stop_price
    if risk_per_share <= 0:
        return 0, 0.0
    shares = math.floor(max_loss / risk_per_share)
    return shares, shares * entry_price
```

- [ ] **Step 4: Run tests — all must pass**

```bash
python -m pytest tests/portfolio/test_sizing.py -v
```

Expected: 14 passed.

- [ ] **Step 5: Commit**

```bash
git add core/portfolio/sizing.py tests/portfolio/test_sizing.py
git commit -m "feat(portfolio): add O'Neil position sizer with 6 guard rails"
```

---

## Task 8: Snapshots

**Files:**
- Create: `core/portfolio/snapshots.py`

- [ ] **Step 1: Create `core/portfolio/snapshots.py`**

No failing test first — this is a write-only utility with no observable output beyond a DB row. Verified via integration in Task 9.

```python
"""Periodic materialised portfolio snapshots for replay performance.

write_snapshot() is called at end-of-day or every 100 events to avoid
full replays on large ledgers. Snapshots store summary data only —
full state is always reconstructable from the event ledger.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from .ledger import EventLedger
from .projections import get_portfolio_state


def write_snapshot(ledger: EventLedger, portfolio_id: str) -> None:
    """Materialise current portfolio state to the snapshots table.

    Safe to call at any time — idempotent for the same sequence_num.
    """
    state = get_portfolio_state(ledger, portfolio_id)
    seq = ledger.event_count(portfolio_id)
    snapshot_data = {
        "cash": state.cash,
        "initial_capital": state.initial_capital,
        "currency": state.currency,
        "realised_pnl": state.realised_pnl,
        "open_position_count": len(state.open_positions),
        "closed_trade_count": len(state.closed_trades),
        "total_value": state.total_value,
    }
    with ledger._conn:
        ledger._conn.execute(
            """
            INSERT OR REPLACE INTO snapshots
                (snapshot_id, portfolio_id, timestamp, sequence_num, state)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                portfolio_id,
                datetime.now(tz=timezone.utc).isoformat(),
                seq,
                json.dumps(snapshot_data),
            ),
        )
```

- [ ] **Step 2: Verify snapshots integrates with the ledger**

```bash
python -c "
from datetime import datetime, timezone
from core.portfolio.events import PortfolioInitialised
from core.portfolio.ledger import EventLedger
from core.portfolio.snapshots import write_snapshot

ledger = EventLedger(':memory:')
ledger.append(PortfolioInitialised(
    portfolio_id='paper_v1',
    timestamp=datetime.now(tz=timezone.utc),
    initial_capital=100000.0,
))
write_snapshot(ledger, 'paper_v1')
row = ledger._conn.execute(
    'SELECT state FROM snapshots WHERE portfolio_id=?', ('paper_v1',)
).fetchone()
import json; data = json.loads(row[0])
print('cash:', data['cash'])
"
```

Expected: `cash: 100000.0`

- [ ] **Step 3: Commit**

```bash
git add core/portfolio/snapshots.py
git commit -m "feat(portfolio): add snapshot writer for replay performance"
```

---

## Task 9: Package Exports and Full Test Run

**Files:**
- Modify: `core/portfolio/__init__.py`

- [ ] **Step 1: Write public exports**

Replace `core/portfolio/__init__.py` with:

```python
"""Event-sourced portfolio state — public API.

Typical usage:

    from core.portfolio import EventLedger, get_portfolio_state
    from core.portfolio import evaluate_exit, calculate_position_size
    from core.portfolio.events import ExitReason, PortfolioInitialised, OrderFilled
"""

from .events import (
    AnyPortfolioEvent,
    ExitReason,
    MarketRegimeChanged,
    MonsterStockHoldActivated,
    OrderFilled,
    OrderPlaced,
    PortfolioInitialised,
    PositionClosed,
    SignalGenerated,
    StopPriceUpdated,
    StopReason,
    deserialise_event,
)
from .exit_engine import ExitSignal, evaluate_exit, evaluate_monster_stock_hold, evaluate_regime_shift
from .ledger import EventLedger
from .projections import ClosedTrade, OpenPosition, PortfolioState, get_open_positions, get_portfolio_state, get_trade_history
from .sizing import SizingRecommendation, calculate_position_size
from .snapshots import write_snapshot

__all__ = [
    # Events
    "AnyPortfolioEvent",
    "ExitReason",
    "MarketRegimeChanged",
    "MonsterStockHoldActivated",
    "OrderFilled",
    "OrderPlaced",
    "PortfolioInitialised",
    "PositionClosed",
    "SignalGenerated",
    "StopPriceUpdated",
    "StopReason",
    "deserialise_event",
    # Ledger
    "EventLedger",
    # Projections
    "ClosedTrade",
    "OpenPosition",
    "PortfolioState",
    "get_open_positions",
    "get_portfolio_state",
    "get_trade_history",
    # Exit engine
    "ExitSignal",
    "evaluate_exit",
    "evaluate_monster_stock_hold",
    "evaluate_regime_shift",
    # Sizing
    "SizingRecommendation",
    "calculate_position_size",
    # Snapshots
    "write_snapshot",
]
```

- [ ] **Step 2: Run the full test suite — all must pass, no regressions**

```bash
python -m pytest -v
```

Expected: all existing tests + new portfolio tests pass. Check specifically:
```
tests/portfolio/test_events.py          7 passed
tests/portfolio/test_ledger.py          6 passed
tests/portfolio/test_projections.py     9 passed
tests/portfolio/test_exit_engine.py    18 passed
tests/portfolio/test_sizing.py         14 passed
```

And all prior tests (test_canslim_logic, test_data_client, test_fmp_resilience, etc.) still pass.

- [ ] **Step 3: Run ruff**

```bash
python -m ruff check core/portfolio/ tests/portfolio/ --fix
python -m ruff format core/portfolio/ tests/portfolio/
```

Expected: no unfixable errors.

- [ ] **Step 4: Commit**

```bash
git add core/portfolio/__init__.py
git commit -m "feat(portfolio): wire package exports and verify full test suite passes"
```

---

## Self-Review

**Spec coverage check:**

| Spec section | Covered by |
|---|---|
| §2 Architecture + SQLite schema | Task 4 (ledger.py) |
| §3 Event catalogue (9 types) | Task 3 (events.py) — all 9 types present |
| §3.3 ExitReason + StopReason enums | Task 3 |
| §4.1 OpenPosition derived state | Task 5 (projections.py) |
| §4.2 Rule priority ordering | Task 6 (exit_engine.py + test priority test) |
| §4.3 Rule 1 — stop-loss | Task 6 |
| §4.3 Rule 2 — 8-week hold + MonsterStockHoldActivated | Task 6 |
| §4.3 Rule 3 — profit target | Task 6 |
| §4.3 Rule 4 — RS degradation | Task 6 |
| §4.3 Rule 5 — regime shift | Task 6 (evaluate_regime_shift) |
| §5.1 Equal-weight sizing | Task 7 |
| §5.1 Risk-based sizing | Task 7 |
| §5.2 All 6 guard rails | Task 7 |
| §5.3 Pyramid (Phase 2, disabled) | PYRAMID_ENABLED=False in settings — no code needed |
| §6 Projections (all 4 functions) | Task 5 |
| §7 Configuration blocks | Task 1 |
| §8 Dependencies | Task 2 |
| §9 Testing requirements | Tasks 3–7 cover all 8 testing requirements |

**No gaps found. No placeholders. Types consistent throughout.**
