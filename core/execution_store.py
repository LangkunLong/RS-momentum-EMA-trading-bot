"""Durable SQLite store for execution workflows and order references."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from config import settings


class ExecutionStore:
    """SQLite-backed repository for workflow snapshots, transitions, and order refs."""

    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self._ensure_schema()

    def append_transition(
        self,
        *,
        timestamp_utc: str,
        workflow_id: str,
        symbol: str,
        from_state: str | None,
        to_state: str,
        event: str,
        details: dict[str, Any],
    ) -> None:
        """Persist one immutable workflow transition."""
        payload = json.dumps(details, sort_keys=True)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO workflow_transitions (
                    timestamp_utc,
                    workflow_id,
                    symbol,
                    from_state,
                    to_state,
                    event,
                    details_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (timestamp_utc, workflow_id, symbol, from_state, to_state, event, payload),
            )
            conn.commit()

    def upsert_workflow_snapshot(
        self,
        *,
        workflow_id: str,
        symbol: str,
        state: str | None,
        broker_order_id: str,
        entry_plan: Optional[dict[str, Any]],
        created_at_utc: str,
        updated_at_utc: str,
    ) -> None:
        """Upsert the latest workflow snapshot for fast recovery."""
        entry_plan_json = json.dumps(entry_plan, sort_keys=True) if entry_plan is not None else None
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO workflow_snapshots (
                    workflow_id,
                    symbol,
                    state,
                    broker_order_id,
                    entry_plan_json,
                    created_at_utc,
                    updated_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow_id) DO UPDATE SET
                    symbol = excluded.symbol,
                    state = excluded.state,
                    broker_order_id = excluded.broker_order_id,
                    entry_plan_json = excluded.entry_plan_json,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    workflow_id,
                    symbol,
                    state,
                    broker_order_id,
                    entry_plan_json,
                    created_at_utc,
                    updated_at_utc,
                ),
            )
            conn.commit()

    def record_order_reference(
        self,
        *,
        workflow_id: str,
        symbol: str,
        broker_order_id: str = "",
        client_order_id: str = "",
        order_role: str,
        created_at_utc: str,
    ) -> None:
        """Persist a broker/client order id mapping for later recovery."""
        broker_value = broker_order_id.strip()
        client_value = client_order_id.strip()
        if not broker_value and not client_value:
            return

        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO workflow_order_refs (
                    workflow_id,
                    symbol,
                    broker_order_id,
                    client_order_id,
                    order_role,
                    created_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow_id, broker_order_id, client_order_id, order_role) DO NOTHING
                """,
                (
                    workflow_id,
                    symbol,
                    broker_value,
                    client_value,
                    order_role,
                    created_at_utc,
                ),
            )
            conn.commit()

    def load_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        """Load a workflow snapshot with full transition history."""
        with self._lock, self._connect() as conn:
            snapshot = conn.execute(
                """
                SELECT workflow_id, symbol, state, broker_order_id, entry_plan_json, created_at_utc, updated_at_utc
                FROM workflow_snapshots
                WHERE workflow_id = ?
                """,
                (workflow_id,),
            ).fetchone()
            if snapshot is None:
                return None
            transitions = conn.execute(
                """
                SELECT timestamp_utc, workflow_id, symbol, from_state, to_state, event, details_json
                FROM workflow_transitions
                WHERE workflow_id = ?
                ORDER BY id
                """,
                (workflow_id,),
            ).fetchall()
            order_refs = conn.execute(
                """
                SELECT broker_order_id, client_order_id, order_role, created_at_utc
                FROM workflow_order_refs
                WHERE workflow_id = ?
                ORDER BY id
                """,
                (workflow_id,),
            ).fetchall()

        return {
            "workflow_id": snapshot["workflow_id"],
            "symbol": snapshot["symbol"],
            "state": snapshot["state"],
            "broker_order_id": snapshot["broker_order_id"] or "",
            "entry_plan": json.loads(snapshot["entry_plan_json"]) if snapshot["entry_plan_json"] else None,
            "created_at_utc": snapshot["created_at_utc"],
            "updated_at_utc": snapshot["updated_at_utc"],
            "transitions": [
                {
                    "timestamp_utc": row["timestamp_utc"],
                    "workflow_id": row["workflow_id"],
                    "symbol": row["symbol"],
                    "from_state": row["from_state"],
                    "to_state": row["to_state"],
                    "event": row["event"],
                    "details": json.loads(row["details_json"] or "{}"),
                }
                for row in transitions
            ],
            "order_refs": [
                {
                    "broker_order_id": row["broker_order_id"] or "",
                    "client_order_id": row["client_order_id"] or "",
                    "order_role": row["order_role"],
                    "created_at_utc": row["created_at_utc"],
                }
                for row in order_refs
            ],
        }

    def load_latest_workflow_for_symbol(self, symbol: str) -> dict[str, Any] | None:
        """Load the most recently updated workflow for a symbol."""
        workflow_id = self._lookup_single_value(
            """
            SELECT workflow_id
            FROM workflow_snapshots
            WHERE symbol = ?
            ORDER BY updated_at_utc DESC, created_at_utc DESC
            LIMIT 1
            """,
            (symbol,),
        )
        return self.load_workflow(workflow_id) if workflow_id else None

    def list_recent_workflows(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return the most recently updated workflow snapshots."""
        safe_limit = max(1, int(limit))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT workflow_id, symbol, state, broker_order_id, entry_plan_json, created_at_utc, updated_at_utc
                FROM workflow_snapshots
                ORDER BY updated_at_utc DESC, created_at_utc DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()

        return [
            {
                "workflow_id": row["workflow_id"],
                "symbol": row["symbol"],
                "state": row["state"],
                "broker_order_id": row["broker_order_id"] or "",
                "entry_plan": json.loads(row["entry_plan_json"]) if row["entry_plan_json"] else None,
                "created_at_utc": row["created_at_utc"],
                "updated_at_utc": row["updated_at_utc"],
            }
            for row in rows
        ]

    def upsert_active_position(
        self,
        *,
        symbol: str,
        workflow_id: str,
        qty: float,
        entry_price: float,
        opened_at_utc: str,
        updated_at_utc: str,
    ) -> None:
        """Persist the workflow that currently owns the live position."""
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO active_positions (
                    symbol,
                    workflow_id,
                    qty,
                    entry_price,
                    opened_at_utc,
                    updated_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    workflow_id = excluded.workflow_id,
                    qty = excluded.qty,
                    entry_price = excluded.entry_price,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (symbol, workflow_id, qty, entry_price, opened_at_utc, updated_at_utc),
            )
            conn.commit()

    def clear_active_position(self, symbol: str) -> None:
        """Remove the active position mapping for a symbol."""
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                DELETE FROM active_positions
                WHERE symbol = ?
                """,
                (symbol,),
            )
            conn.commit()

    def load_active_position(self, symbol: str) -> dict[str, Any] | None:
        """Load the active position ownership record for a symbol."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT symbol, workflow_id, qty, entry_price, opened_at_utc, updated_at_utc
                FROM active_positions
                WHERE symbol = ?
                """,
                (symbol,),
            ).fetchone()
        if row is None:
            return None
        return {
            "symbol": row["symbol"],
            "workflow_id": row["workflow_id"],
            "qty": float(row["qty"]),
            "entry_price": float(row["entry_price"]),
            "opened_at_utc": row["opened_at_utc"],
            "updated_at_utc": row["updated_at_utc"],
        }

    def load_active_workflow_for_symbol(self, symbol: str) -> dict[str, Any] | None:
        """Load the workflow that currently owns the open position for a symbol."""
        active_position = self.load_active_position(symbol)
        if active_position is None:
            return None
        workflow_id = str(active_position["workflow_id"])
        return self.load_workflow(workflow_id) if workflow_id else None

    def load_workflow_by_broker_order_id(self, broker_order_id: str) -> dict[str, Any] | None:
        """Load a workflow by any known broker order id."""
        workflow_id = self._lookup_single_value(
            """
            SELECT workflow_id
            FROM workflow_order_refs
            WHERE broker_order_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (broker_order_id,),
        )
        return self.load_workflow(workflow_id) if workflow_id else None

    def load_workflow_by_client_order_id(self, client_order_id: str) -> dict[str, Any] | None:
        """Load a workflow by any known client order id."""
        workflow_id = self._lookup_single_value(
            """
            SELECT workflow_id
            FROM workflow_order_refs
            WHERE client_order_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (client_order_id,),
        )
        return self.load_workflow(workflow_id) if workflow_id else None

    def reset(self) -> None:
        """Remove all workflow data. Intended for tests only."""
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM active_positions")
            conn.execute("DELETE FROM workflow_order_refs")
            conn.execute("DELETE FROM workflow_transitions")
            conn.execute("DELETE FROM workflow_snapshots")
            conn.commit()

    def _lookup_single_value(self, query: str, params: tuple[Any, ...]) -> str:
        with self._lock, self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        return str(row[0]) if row and row[0] else ""

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Yield one transactional connection and always close it."""
        db_file = Path(self.db_path)
        db_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_file, timeout=30, check_same_thread=False)
        try:
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA synchronous=NORMAL")
            with conn:
                yield conn
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS workflow_snapshots (
                    workflow_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    state TEXT,
                    broker_order_id TEXT NOT NULL DEFAULT '',
                    entry_plan_json TEXT,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workflow_transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_utc TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT NOT NULL,
                    event TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workflow_order_refs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    broker_order_id TEXT NOT NULL DEFAULT '',
                    client_order_id TEXT NOT NULL DEFAULT '',
                    order_role TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    UNIQUE(workflow_id, broker_order_id, client_order_id, order_role)
                );

                CREATE TABLE IF NOT EXISTS active_positions (
                    symbol TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    qty REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    opened_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_workflow_snapshots_symbol
                ON workflow_snapshots(symbol, updated_at_utc DESC);

                CREATE INDEX IF NOT EXISTS idx_workflow_snapshots_broker_order
                ON workflow_snapshots(broker_order_id);

                CREATE INDEX IF NOT EXISTS idx_workflow_transitions_workflow
                ON workflow_transitions(workflow_id, id);

                CREATE INDEX IF NOT EXISTS idx_workflow_order_refs_broker
                ON workflow_order_refs(broker_order_id);

                CREATE INDEX IF NOT EXISTS idx_workflow_order_refs_client
                ON workflow_order_refs(client_order_id);

                CREATE INDEX IF NOT EXISTS idx_workflow_order_refs_symbol
                ON workflow_order_refs(symbol, created_at_utc DESC);

                CREATE INDEX IF NOT EXISTS idx_active_positions_workflow
                ON active_positions(workflow_id);
                """
            )
            conn.commit()


_STORE_LOCK = threading.Lock()
_STORE: ExecutionStore | None = None


def get_execution_store() -> ExecutionStore:
    """Return the process-wide execution store for the configured DB path."""
    global _STORE
    desired_path = str(settings.EXECUTION_STORE_DB_PATH)
    with _STORE_LOCK:
        if _STORE is None or _STORE.db_path != desired_path:
            _STORE = ExecutionStore(desired_path)
        return _STORE


def reset_execution_store() -> None:
    """Clear the configured execution store. Intended for tests."""
    get_execution_store().reset()
