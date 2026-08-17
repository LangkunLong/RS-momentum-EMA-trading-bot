"""Durable SQLite store for execution workflows and order references."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from config import settings


_NOTIFICATION_LEGACY_EVENTS = {
    "buy_fill": ("buy_fill_notification_claimed", "buy_fill_notified"),
    "sell_fill": ("sell_fill_notification_claimed", "sell_fill_notified"),
}
_SUBMISSION_INTENT_EVENT_BY_ROLE = {
    "entry": "entry_submission_intent",
    "exit": "exit_submission_intent",
}
_SUBMISSION_INTENT_EVENTS = tuple(_SUBMISSION_INTENT_EVENT_BY_ROLE.values())
_SUBMISSION_INTENT_RESOLVED_EVENT = "submission_intent_resolved"
_SUBMISSION_INTENT_EVENT_BY_FAILURE = {
    "order_submit_failed": "entry_submission_intent",
    "exit_order_submit_failed": "exit_submission_intent",
}
_SUBMISSION_TRACKING_EVENTS = (
    *_SUBMISSION_INTENT_EVENTS,
    _SUBMISSION_INTENT_RESOLVED_EVENT,
    *_SUBMISSION_INTENT_EVENT_BY_FAILURE,
)


def _unresolved_submission_intents(
    rows: list[sqlite3.Row],
) -> list[tuple[sqlite3.Row, dict[str, Any]]]:
    """Return each latest exact intent without a later matching resolution."""
    pending: dict[
        tuple[str, str, str],
        tuple[sqlite3.Row, dict[str, Any]],
    ] = {}
    for row in rows:
        try:
            details = json.loads(row["details_json"] or "{}")
        except (TypeError, ValueError) as exc:
            raise ValueError("Submission intent details are invalid") from exc
        event = str(row["event"] or "")
        workflow_id = str(row["workflow_id"] or "")
        client_order_id = str(details.get("client_order_id", "") or "")
        if event in _SUBMISSION_INTENT_EVENTS:
            pending[(workflow_id, event, client_order_id)] = (row, details)
            continue
        failed_intent_event = _SUBMISSION_INTENT_EVENT_BY_FAILURE.get(event, "")
        if failed_intent_event:
            matching_keys = [
                key
                for key in pending
                if key[0] == workflow_id and key[1] == failed_intent_event
            ]
            if matching_keys:
                latest_key = max(
                    matching_keys,
                    key=lambda key: int(pending[key][0]["id"]),
                )
                pending.pop(latest_key, None)
            continue
        if event == _SUBMISSION_INTENT_RESOLVED_EVENT:
            role = str(details.get("role", "") or "").strip().lower()
            intent_event = _SUBMISSION_INTENT_EVENT_BY_ROLE.get(role, "")
            pending.pop((workflow_id, intent_event, client_order_id), None)
    return sorted(pending.values(), key=lambda item: int(item[0]["id"]))


class ConcurrentWorkflowTransitionError(RuntimeError):
    """Raised when a stale workflow instance attempts to append a transition."""


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

    def persist_transition_and_snapshot(
        self,
        *,
        timestamp_utc: str,
        workflow_id: str,
        symbol: str,
        from_state: str | None,
        to_state: str,
        event: str,
        details: dict[str, Any],
        broker_order_id: str,
        entry_plan: Optional[dict[str, Any]],
        created_at_utc: str,
        expected_transition_count: int,
    ) -> None:
        """Atomically append a transition and advance its workflow snapshot."""
        details_json = json.dumps(details, sort_keys=True)
        entry_plan_json = (
            json.dumps(entry_plan, sort_keys=True) if entry_plan is not None else None
        )
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            durable_transition_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM workflow_transitions WHERE workflow_id = ?",
                    (workflow_id,),
                ).fetchone()[0]
            )
            if durable_transition_count != expected_transition_count:
                raise ConcurrentWorkflowTransitionError(
                    f"Concurrent workflow transition for {workflow_id}: expected "
                    f"{expected_transition_count} durable transitions, found "
                    f"{durable_transition_count}"
                )
            current = conn.execute(
                "SELECT state FROM workflow_snapshots WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
            current_state = current["state"] if current is not None else None
            if current is not None and current_state != from_state:
                raise ConcurrentWorkflowTransitionError(
                    f"Concurrent workflow state change for {workflow_id}: "
                    f"expected {from_state!r}, found {current_state!r}"
                )
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
                    to_state,
                    broker_order_id,
                    entry_plan_json,
                    created_at_utc,
                    timestamp_utc,
                ),
            )
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
                (
                    timestamp_utc,
                    workflow_id,
                    symbol,
                    from_state,
                    to_state,
                    event,
                    details_json,
                ),
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
            conn.execute("BEGIN IMMEDIATE")
            for column, value, label in (
                ("broker_order_id", broker_value, "broker order id"),
                ("client_order_id", client_value, "client order id"),
            ):
                if not value:
                    continue
                conflict = conn.execute(
                    f"""
                    SELECT workflow_id
                    FROM workflow_order_refs
                    WHERE {column} = ? AND workflow_id <> ?
                    LIMIT 1
                    """,
                    (value, workflow_id),
                ).fetchone()
                if conflict is not None:
                    raise ValueError(
                        f"{label} {value!r} already belongs to another workflow"
                    )
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

    def load_pending_submission_intents(
        self,
        *,
        symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return exact broker-call intents without a later resolution marker."""
        params: list[Any] = list(_SUBMISSION_TRACKING_EVENTS)
        symbol_filter = ""
        if symbol:
            symbol_filter = "AND UPPER(s.symbol) = ?"
            params.append(str(symbol).strip().upper())
        tracking_placeholders = ", ".join(
            "?" for _ in _SUBMISSION_TRACKING_EVENTS
        )
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT t.id, t.workflow_id, s.symbol, t.event, t.details_json
                FROM workflow_transitions AS t
                JOIN workflow_snapshots AS s ON s.workflow_id = t.workflow_id
                WHERE t.event IN ({tracking_placeholders})
                  {symbol_filter}
                ORDER BY t.id
                """,
                tuple(params),
            ).fetchall()
        pending = _unresolved_submission_intents(rows)
        return [
            {
                "workflow_id": str(row["workflow_id"]),
                "symbol": str(row["symbol"]),
                "event": str(row["event"]),
                "details": details,
            }
            for row, details in pending
        ]

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

    def claim_order_reference_from_submission_intent(
        self,
        *,
        workflow_id: str,
        symbol: str,
        broker_order_id: str,
        client_order_id: str,
        order_role: str,
        intent_event: str,
        side: str,
        created_at_utc: str,
    ) -> None:
        """Atomically bind one broker id to the latest pending submission intent."""
        broker_value = broker_order_id.strip()
        client_value = client_order_id.strip()
        normalized_symbol = symbol.strip().upper()
        normalized_side = side.strip().lower()
        if not broker_value or not client_value:
            raise ValueError("Submission-intent recovery requires both order ids")

        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            snapshot = conn.execute(
                "SELECT symbol FROM workflow_snapshots WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
            if (
                snapshot is None
                or str(snapshot["symbol"] or "").strip().upper() != normalized_symbol
            ):
                raise ValueError("Submission intent does not match the workflow symbol")

            transition_rows = conn.execute(
                f"""
                SELECT id, workflow_id, event, details_json
                FROM workflow_transitions
                WHERE workflow_id = ? AND event IN (
                    {", ".join("?" for _ in _SUBMISSION_TRACKING_EVENTS)}
                )
                ORDER BY id
                """,
                (workflow_id, *_SUBMISSION_TRACKING_EVENTS),
            ).fetchall()
            pending = _unresolved_submission_intents(transition_rows)
            matching = [
                (row, details)
                for row, details in pending
                if str(row["event"] or "") == intent_event
                and str(details.get("client_order_id", "") or "") == client_value
            ]
            if len(matching) != 1:
                raise ValueError("Workflow has no pending matching submission intent")
            _, details = matching[0]
            if (
                str(details.get("symbol", "") or "").strip().upper()
                != normalized_symbol
                or str(details.get("side", "") or "").strip().lower()
                != normalized_side
            ):
                raise ValueError("Broker event does not match the pending submission intent")

            for column, value, label in (
                ("broker_order_id", broker_value, "broker order id"),
                ("client_order_id", client_value, "client order id"),
            ):
                conflict = conn.execute(
                    f"""
                    SELECT workflow_id
                    FROM workflow_order_refs
                    WHERE {column} = ? AND workflow_id <> ?
                    LIMIT 1
                    """,
                    (value, workflow_id),
                ).fetchone()
                if conflict is not None:
                    raise ValueError(f"{label} already belongs to another workflow")

            existing_broker_ids = {
                str(row["broker_order_id"] or "")
                for row in conn.execute(
                    """
                    SELECT broker_order_id
                    FROM workflow_order_refs
                    WHERE workflow_id = ? AND order_role = ?
                      AND broker_order_id <> ''
                    """,
                    (workflow_id, order_role),
                ).fetchall()
            }
            if existing_broker_ids and broker_value not in existing_broker_ids:
                raise ValueError("Submission intent already claimed another broker order id")

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
                ON CONFLICT(workflow_id, broker_order_id, client_order_id, order_role)
                DO NOTHING
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

    def clear_active_position_for_workflow(self, symbol: str, workflow_id: str) -> bool:
        """Clear ownership only when it still belongs to ``workflow_id``."""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM active_positions
                WHERE symbol = ? AND workflow_id = ?
                """,
                (symbol, workflow_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def clear_active_position_if_unchanged(
        self,
        *,
        symbol: str,
        workflow_id: str,
        qty: float,
        entry_price: float,
        opened_at_utc: str,
        updated_at_utc: str,
    ) -> bool:
        """Clear the exact ownership row observed before a broker-flat proof."""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM active_positions
                WHERE symbol = ?
                  AND workflow_id = ?
                  AND qty = ?
                  AND entry_price = ?
                  AND opened_at_utc = ?
                  AND updated_at_utc = ?
                """,
                (
                    symbol,
                    workflow_id,
                    qty,
                    entry_price,
                    opened_at_utc,
                    updated_at_utc,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0

    def upsert_active_position_if_owner_matches(
        self,
        *,
        symbol: str,
        workflow_id: str,
        qty: float,
        entry_price: float,
        opened_at_utc: str,
        updated_at_utc: str,
        preserve_higher_qty: bool = False,
    ) -> bool:
        """Insert ownership, or safely update it while the same workflow owns it."""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
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
                    qty = CASE
                        WHEN ? AND active_positions.qty > excluded.qty
                        THEN active_positions.qty
                        ELSE excluded.qty
                    END,
                    entry_price = CASE
                        WHEN ? AND active_positions.qty > excluded.qty
                        THEN active_positions.entry_price
                        ELSE excluded.entry_price
                    END,
                    updated_at_utc = CASE
                        WHEN ? AND active_positions.qty > excluded.qty
                        THEN active_positions.updated_at_utc
                        ELSE excluded.updated_at_utc
                    END
                WHERE active_positions.workflow_id = excluded.workflow_id
                """,
                (
                    symbol,
                    workflow_id,
                    qty,
                    entry_price,
                    opened_at_utc,
                    updated_at_utc,
                    preserve_higher_qty,
                    preserve_higher_qty,
                    preserve_higher_qty,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0

    def claim_notification(
        self,
        *,
        workflow_id: str,
        notification_kind: str,
        claimed_at_utc: str,
    ) -> bool:
        """Atomically claim one notification kind for a workflow."""
        legacy_events = _NOTIFICATION_LEGACY_EVENTS.get(notification_kind)
        if legacy_events is None:
            raise ValueError(f"Unsupported notification kind: {notification_kind}")

        claim_values = (workflow_id, notification_kind, claimed_at_utc)
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO workflow_notification_claims (
                    workflow_id,
                    notification_kind,
                    claimed_at_utc
                )
                SELECT ?, ?, ?
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM workflow_transitions
                    WHERE workflow_id = ? AND event IN (?, ?)
                )
                """,
                (
                    *claim_values,
                    workflow_id,
                    legacy_events[0],
                    legacy_events[1],
                ),
            )
            claimed = cursor.rowcount > 0
            if not claimed:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO workflow_notification_claims (
                        workflow_id,
                        notification_kind,
                        claimed_at_utc
                    )
                    VALUES (?, ?, ?)
                    """,
                    claim_values,
                )
            conn.commit()
            return claimed

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
        workflow_ids = self.find_workflow_ids_by_broker_order_id(broker_order_id)
        workflow_id = next(iter(workflow_ids)) if len(workflow_ids) == 1 else ""
        return self.load_workflow(workflow_id) if workflow_id else None

    def find_workflow_ids_by_broker_order_id(self, broker_order_id: str) -> set[str]:
        """Return every durable owner of a broker id, including legacy conflicts."""
        if not broker_order_id:
            return set()
        return self._lookup_workflow_ids(
            """
            SELECT workflow_id
            FROM workflow_order_refs
            WHERE broker_order_id = ?
            ORDER BY id DESC
            """,
            (broker_order_id,),
        )

    def load_workflow_by_client_order_id(self, client_order_id: str) -> dict[str, Any] | None:
        """Load a workflow by any known client order id."""
        workflow_ids = self.find_workflow_ids_by_client_order_id(client_order_id)
        workflow_id = next(iter(workflow_ids)) if len(workflow_ids) == 1 else ""
        return self.load_workflow(workflow_id) if workflow_id else None

    def find_workflow_ids_by_client_order_id(self, client_order_id: str) -> set[str]:
        """Return every durable owner of a client id, including legacy conflicts."""
        if not client_order_id:
            return set()
        return self._lookup_workflow_ids(
            """
            SELECT workflow_id
            FROM workflow_order_refs
            WHERE client_order_id = ?
            ORDER BY id DESC
            """,
            (client_order_id,),
        )

    def reset(self) -> None:
        """Remove all workflow data. Intended for tests only."""
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM active_positions")
            conn.execute("DELETE FROM workflow_notification_claims")
            conn.execute("DELETE FROM workflow_order_refs")
            conn.execute("DELETE FROM workflow_transitions")
            conn.execute("DELETE FROM workflow_snapshots")
            conn.commit()

    def _lookup_single_value(self, query: str, params: tuple[Any, ...]) -> str:
        with self._lock, self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        return str(row[0]) if row and row[0] else ""

    def _lookup_workflow_ids(self, query: str, params: tuple[Any, ...]) -> set[str]:
        """Return all distinct workflow ids matching one durable reference."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return {str(row[0]) for row in rows if row[0]}

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

                CREATE TABLE IF NOT EXISTS workflow_notification_claims (
                    workflow_id TEXT NOT NULL,
                    notification_kind TEXT NOT NULL,
                    claimed_at_utc TEXT NOT NULL,
                    PRIMARY KEY(workflow_id, notification_kind)
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
