"""Explicit-path, additive SQLite persistence for qualified-policy state.

This store is intentionally independent from the legacy execution-store
singleton and workflow entry points. Callers must supply both the database
path and its immutable store identity.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Iterator, Mapping

from core.policy_execution_state import (
    ActionAttemptStatus,
    ActionIntent,
    ActionOrderAttempt,
    ActionRole,
    ActionStateProjection,
    ActionStatus,
    DecisionClock,
    DecisionCategory,
    DecisionConflictError,
    DecisionIdentity,
    DecisionSubjectType,
    HoldingEpisode,
    IdentityConflictError,
    OrderSide,
    PolicyDeploymentIdentity,
    PortfolioStateSnapshot,
    ProviderOrderReference,
    StopUpdateIntent,
    build_action_intent,
    add_attempt_order_aliases,
    advance_holding_exit_tier,
    apply_action_fill_to_holding,
    apply_attempt_cumulative_fill,
    bind_attempt_order_refs,
    clear_terminal_pending_action,
    confirm_attempt_terminal,
    confirm_protective_stop as confirm_stop_in_state,
    create_single_remainder_attempt as create_remainder_in_state,
    project_action_state,
    propose_stop_update as propose_stop_in_state,
    register_pending_action,
    register_reconciliation_action,
    request_attempt_cancel,
    resolve_action,
    update_holding_marks,
)


SCHEMA_VERSION = 1
_PROTECTIVE_EXECUTION_RECORD_KIND = "execution_derived_protective_stop_order_v1"
_GUARD_SELL_EXECUTION_RECORD_KIND = "execution_derived_guard_sell_order_v1"


class ConcurrentStateUpdateError(ValueError):
    """Raised when a caller's expected version or pointer is stale."""


class StopUpdateProposalAlreadyClaimedError(ValueError):
    """A matching durable stop proposal exists; reconcile instead of dispatching again."""


class StoreIdentityConflictError(ValueError):
    """Raised when an explicit path is opened under a different store identity."""


class FillReceiptConflictError(ValueError):
    """Raised when a replayed external fill ID carries different immutable facts."""


class OrderReferenceConflictError(ValueError):
    """Raised when an external order reference is assigned to another attempt."""


class SchemaRollbackBlockedError(ValueError):
    """Raised when policy rows still depend on the v1 schema."""


@dataclass(frozen=True, slots=True)
class PolicyExecutionReadSnapshot:
    """One consistent account/store read selected by a portfolio generation."""

    deployment_identity: PolicyDeploymentIdentity
    portfolio_snapshot: PortfolioStateSnapshot
    action_projections: tuple[ActionStateProjection, ...]
    holding_episodes: tuple[HoldingEpisode, ...]


@dataclass(frozen=True, slots=True)
class PolicyDecisionRecord:
    """Durable immutable decision and its policy, guard, and effective-action facts."""

    decision: DecisionIdentity
    policy_payload: Mapping[str, object]
    guard_payload: Mapping[str, object]
    effective_action_payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class PolicyDeploymentChain:
    """Durable deployment identity, handler binding and pointer history."""

    deployment_identity: PolicyDeploymentIdentity
    lifecycle: str
    handler_identity: str
    guard_id: str
    active_for_account: bool
    pointer_version: int | None
    readiness_state: str | None
    events: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True)
class ActiveGenerationPointer:
    """Active generation and the version a caller must include in its next write."""

    active_generation_id: str | None
    pointer_version: int | None
    readiness_state: str | None


SCHEMA_V1_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE policy_state_schema_migrations (
        version INTEGER PRIMARY KEY,
        migration_sha256 TEXT NOT NULL,
        applied_at_utc TEXT NOT NULL
    )""",
    """CREATE TABLE policy_state_database_identity (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        store_identity TEXT NOT NULL,
        namespace_id TEXT NOT NULL,
        binding_sha256 TEXT NOT NULL
    )""",
    """CREATE TABLE policy_state_deployments (
        deployment_generation_id TEXT PRIMARY KEY,
        policy_artifact_id TEXT NOT NULL,
        capability_manifest_id TEXT NOT NULL,
        policy_interface_version TEXT NOT NULL,
        feature_contract_id TEXT NOT NULL,
        feature_calculator_id TEXT NOT NULL,
        source_revision TEXT NOT NULL,
        runtime_identity TEXT NOT NULL,
        execution_profile_id TEXT NOT NULL,
        paper_account_environment_id TEXT NOT NULL,
        store_identity TEXT NOT NULL,
        lifecycle TEXT NOT NULL,
        handler_identity TEXT NOT NULL,
        guard_id TEXT NOT NULL,
        identity_json TEXT NOT NULL,
        created_at_utc TEXT NOT NULL,
        UNIQUE (deployment_generation_id, paper_account_environment_id, store_identity)
    )""",
    """CREATE TABLE policy_state_active_pointers (
        paper_account_environment_id TEXT NOT NULL,
        store_identity TEXT NOT NULL,
        active_generation_id TEXT NOT NULL,
        readiness_state TEXT NOT NULL,
        pointer_version INTEGER NOT NULL,
        updated_at_utc TEXT NOT NULL,
        PRIMARY KEY (paper_account_environment_id, store_identity),
        FOREIGN KEY (active_generation_id, paper_account_environment_id, store_identity)
            REFERENCES policy_state_deployments(deployment_generation_id, paper_account_environment_id, store_identity)
            ON DELETE RESTRICT
    )""",
    """CREATE TABLE policy_state_deployment_events (
        event_id TEXT PRIMARY KEY,
        paper_account_environment_id TEXT NOT NULL,
        store_identity TEXT NOT NULL,
        old_generation_id TEXT,
        new_generation_id TEXT NOT NULL,
        event_kind TEXT NOT NULL,
        readiness_evidence_ref TEXT NOT NULL,
        reason TEXT NOT NULL,
        expected_pointer_version INTEGER NOT NULL,
        resulting_pointer_version INTEGER NOT NULL,
        event_time_utc TEXT NOT NULL,
        FOREIGN KEY (old_generation_id, paper_account_environment_id, store_identity)
            REFERENCES policy_state_deployments(deployment_generation_id, paper_account_environment_id, store_identity)
            ON DELETE RESTRICT,
        FOREIGN KEY (new_generation_id, paper_account_environment_id, store_identity)
            REFERENCES policy_state_deployments(deployment_generation_id, paper_account_environment_id, store_identity)
            ON DELETE RESTRICT
    )""",
    """CREATE TABLE policy_state_decisions (
        decision_id TEXT PRIMARY KEY,
        decision_slot_id TEXT NOT NULL UNIQUE,
        deployment_generation_id TEXT NOT NULL,
        paper_account_environment_id TEXT NOT NULL,
        store_identity TEXT NOT NULL,
        exchange_id TEXT NOT NULL,
        decision_session TEXT NOT NULL,
        decision_category TEXT NOT NULL,
        subject_type TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        as_of_cutoff_at TEXT NOT NULL,
        next_execution_session TEXT NOT NULL,
        account_valuation_session TEXT NOT NULL,
        account_valuation_at TEXT NOT NULL,
        snapshot_sha256 TEXT NOT NULL,
        decision_json TEXT NOT NULL,
        policy_payload_json TEXT NOT NULL,
        guard_payload_json TEXT NOT NULL,
        effective_action_payload_json TEXT NOT NULL,
        UNIQUE (deployment_generation_id, decision_id),
        UNIQUE (deployment_generation_id, exchange_id, decision_session, decision_category, subject_type, subject_id, sequence),
        FOREIGN KEY (deployment_generation_id, paper_account_environment_id, store_identity)
            REFERENCES policy_state_deployments(deployment_generation_id, paper_account_environment_id, store_identity)
            ON DELETE RESTRICT
    )""",
    """CREATE TABLE policy_state_holdings (
        holding_episode_id TEXT PRIMARY KEY,
        deployment_generation_id TEXT NOT NULL,
        paper_account_environment_id TEXT NOT NULL,
        store_identity TEXT NOT NULL,
        security_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        broker_symbol TEXT NOT NULL,
        opening_decision_id TEXT NOT NULL,
        opening_action_id TEXT NOT NULL,
        initial_filled_quantity TEXT NOT NULL,
        opening_later_fills_quantity TEXT NOT NULL,
        remaining_quantity TEXT NOT NULL,
        completed_additions_quantity TEXT NOT NULL,
        addition_count INTEGER NOT NULL,
        entry_price TEXT,
        cost_basis TEXT,
        realized_pnl TEXT,
        committed_risk TEXT,
        committed_risk_basis TEXT,
        proposed_stop_price TEXT,
        proposed_stop_action_id TEXT,
        confirmed_protective_stop_price TEXT,
        confirmed_stop_action_id TEXT,
        confirmed_stop_client_order_id TEXT,
        confirmed_stop_broker_order_id TEXT,
        confirmed_stop_observed_at TEXT,
        peak_price TEXT,
        valuation_at TEXT,
        last_accepted_session TEXT,
        policy_flags_json TEXT NOT NULL,
        pending_action_ids_json TEXT NOT NULL,
        applied_action_fill_watermarks_json TEXT NOT NULL,
        completed_addition_action_ids_json TEXT NOT NULL,
        last_exit_tier INTEGER NOT NULL,
        state_version INTEGER NOT NULL,
        UNIQUE (deployment_generation_id, holding_episode_id, security_id),
        UNIQUE (deployment_generation_id, security_id, opening_action_id),
        FOREIGN KEY (deployment_generation_id, paper_account_environment_id, store_identity)
            REFERENCES policy_state_deployments(deployment_generation_id, paper_account_environment_id, store_identity)
            ON DELETE RESTRICT,
        FOREIGN KEY (deployment_generation_id, opening_decision_id)
            REFERENCES policy_state_decisions(deployment_generation_id, decision_id)
            ON DELETE RESTRICT,
        FOREIGN KEY (deployment_generation_id, opening_action_id)
            REFERENCES policy_state_actions(deployment_generation_id, logical_action_id)
            ON DELETE RESTRICT
    )""",
    """CREATE TABLE policy_state_actions (
        logical_action_id TEXT PRIMARY KEY,
        deployment_generation_id TEXT NOT NULL,
        paper_account_environment_id TEXT NOT NULL,
        store_identity TEXT NOT NULL,
        decision_id TEXT NOT NULL,
        holding_episode_id TEXT,
        security_id TEXT NOT NULL,
        broker_symbol TEXT NOT NULL,
        role TEXT NOT NULL,
        side TEXT NOT NULL,
        requested_quantity TEXT NOT NULL,
        confirmed_quantity TEXT NOT NULL,
        residual_quantity TEXT NOT NULL,
        reservation_price TEXT,
        reservation_price_basis TEXT,
        reservation_stop_price TEXT,
        cash_reservation_amount TEXT,
        risk_per_unit TEXT,
        risk_basis TEXT,
        residual_committed_risk TEXT,
        exit_tier INTEGER,
        snapshot_original_quantity TEXT,
        fraction_of_original_quantity TEXT,
        rounding_rule_id TEXT,
        status TEXT NOT NULL,
        resolution_reason TEXT,
        immutable_payload_json TEXT NOT NULL,
        state_version INTEGER NOT NULL,
        UNIQUE (deployment_generation_id, logical_action_id),
        UNIQUE (logical_action_id, paper_account_environment_id, store_identity),
        CHECK (role != 'scale_out' OR (holding_episode_id IS NOT NULL AND exit_tier IS NOT NULL AND snapshot_original_quantity IS NOT NULL AND fraction_of_original_quantity IS NOT NULL AND rounding_rule_id IS NOT NULL)),
        FOREIGN KEY (deployment_generation_id, paper_account_environment_id, store_identity)
            REFERENCES policy_state_deployments(deployment_generation_id, paper_account_environment_id, store_identity)
            ON DELETE RESTRICT,
        FOREIGN KEY (deployment_generation_id, decision_id)
            REFERENCES policy_state_decisions(deployment_generation_id, decision_id)
            ON DELETE RESTRICT,
        FOREIGN KEY (deployment_generation_id, holding_episode_id, security_id)
            REFERENCES policy_state_holdings(deployment_generation_id, holding_episode_id, security_id)
            ON DELETE RESTRICT
    )""",
    "CREATE UNIQUE INDEX policy_state_one_tier_intent ON policy_state_actions(deployment_generation_id, holding_episode_id, exit_tier) WHERE role = 'scale_out'",
    """CREATE TABLE policy_state_action_history (
        logical_action_id TEXT NOT NULL,
        state_version INTEGER NOT NULL,
        event_id TEXT NOT NULL UNIQUE,
        event_kind TEXT NOT NULL,
        state_sha256 TEXT NOT NULL,
        state_json TEXT NOT NULL,
        observed_at_utc TEXT NOT NULL,
        PRIMARY KEY (logical_action_id, state_version),
        FOREIGN KEY (logical_action_id) REFERENCES policy_state_actions(logical_action_id) ON DELETE RESTRICT
    )""",
    """CREATE TABLE policy_state_order_attempts (
        logical_action_id TEXT NOT NULL,
        paper_account_environment_id TEXT NOT NULL,
        store_identity TEXT NOT NULL,
        attempt_number INTEGER NOT NULL CHECK (attempt_number IN (1, 2)),
        requested_quantity TEXT NOT NULL,
        confirmed_quantity TEXT NOT NULL,
        cumulative_notional TEXT,
        cumulative_fees TEXT,
        status TEXT NOT NULL,
        terminal_status TEXT,
        client_order_id TEXT,
        broker_order_id TEXT,
        state_version INTEGER NOT NULL,
        PRIMARY KEY (logical_action_id, attempt_number),
        UNIQUE (logical_action_id, attempt_number, paper_account_environment_id, store_identity),
        FOREIGN KEY (logical_action_id, paper_account_environment_id, store_identity)
            REFERENCES policy_state_actions(logical_action_id, paper_account_environment_id, store_identity)
            ON DELETE RESTRICT
    )""",
    """CREATE TABLE policy_state_order_reference_aliases (
        provider_id TEXT NOT NULL,
        paper_account_environment_id TEXT NOT NULL,
        store_identity TEXT NOT NULL,
        reference_kind TEXT NOT NULL,
        external_order_id TEXT NOT NULL,
        logical_action_id TEXT NOT NULL,
        attempt_number INTEGER NOT NULL,
        source_payload_sha256 TEXT NOT NULL,
        first_seen_at_utc TEXT NOT NULL,
        PRIMARY KEY (provider_id, paper_account_environment_id, store_identity, reference_kind, external_order_id),
        FOREIGN KEY (logical_action_id, attempt_number, paper_account_environment_id, store_identity)
            REFERENCES policy_state_order_attempts(logical_action_id, attempt_number, paper_account_environment_id, store_identity)
            ON DELETE RESTRICT
    )""",
    """CREATE TABLE policy_state_fill_receipts (
        provider_id TEXT NOT NULL,
        paper_account_environment_id TEXT NOT NULL,
        store_identity TEXT NOT NULL,
        fill_event_id TEXT NOT NULL,
        logical_action_id TEXT NOT NULL,
        attempt_number INTEGER NOT NULL,
        payload_sha256 TEXT NOT NULL,
        cumulative_quantity TEXT NOT NULL,
        cumulative_notional TEXT,
        cumulative_fees TEXT,
        observed_at_utc TEXT NOT NULL,
        PRIMARY KEY (provider_id, paper_account_environment_id, store_identity, fill_event_id),
        FOREIGN KEY (logical_action_id, attempt_number, paper_account_environment_id, store_identity)
            REFERENCES policy_state_order_attempts(logical_action_id, attempt_number, paper_account_environment_id, store_identity)
            ON DELETE RESTRICT
    )""",
    """CREATE TABLE policy_state_holding_history (
        holding_episode_id TEXT NOT NULL,
        state_version INTEGER NOT NULL,
        event_id TEXT NOT NULL UNIQUE,
        logical_action_id TEXT,
        event_kind TEXT NOT NULL,
        state_sha256 TEXT NOT NULL,
        state_json TEXT NOT NULL,
        evidence_ref TEXT,
        observed_at_utc TEXT NOT NULL,
        PRIMARY KEY (holding_episode_id, state_version),
        FOREIGN KEY (holding_episode_id) REFERENCES policy_state_holdings(holding_episode_id) ON DELETE RESTRICT,
        FOREIGN KEY (logical_action_id) REFERENCES policy_state_actions(logical_action_id) ON DELETE RESTRICT
    )""",
    """CREATE TABLE policy_state_stop_updates (
        stop_update_action_id TEXT PRIMARY KEY,
        deployment_generation_id TEXT NOT NULL,
        paper_account_environment_id TEXT NOT NULL,
        store_identity TEXT NOT NULL,
        holding_episode_id TEXT NOT NULL,
        security_id TEXT NOT NULL,
        decision_id TEXT NOT NULL,
        requested_stop_price TEXT NOT NULL,
        status TEXT NOT NULL,
        client_order_id TEXT,
        broker_order_id TEXT,
        confirmed_stop_price TEXT,
        observed_at_utc TEXT,
        state_version INTEGER NOT NULL,
        FOREIGN KEY (deployment_generation_id, paper_account_environment_id, store_identity)
            REFERENCES policy_state_deployments(deployment_generation_id, paper_account_environment_id, store_identity)
            ON DELETE RESTRICT,
        FOREIGN KEY (deployment_generation_id, holding_episode_id, security_id)
            REFERENCES policy_state_holdings(deployment_generation_id, holding_episode_id, security_id)
            ON DELETE RESTRICT,
        FOREIGN KEY (deployment_generation_id, decision_id)
            REFERENCES policy_state_decisions(deployment_generation_id, decision_id)
            ON DELETE RESTRICT
    )""",
    """CREATE TABLE policy_state_portfolio_snapshots (
        portfolio_snapshot_id TEXT PRIMARY KEY,
        deployment_generation_id TEXT NOT NULL,
        paper_account_environment_id TEXT NOT NULL,
        store_identity TEXT NOT NULL,
        source_namespace TEXT NOT NULL,
        account_snapshot_id TEXT NOT NULL,
        completed_session TEXT NOT NULL,
        account_valuation_session TEXT NOT NULL,
        account_valuation_at TEXT NOT NULL,
        equity TEXT,
        cash TEXT,
        gross_exposure TEXT,
        open_risk TEXT,
        portfolio_peak_equity TEXT,
        last_accepted_session TEXT,
        policy_flags_json TEXT NOT NULL,
        state_sha256 TEXT NOT NULL,
        state_json TEXT NOT NULL,
        state_version INTEGER NOT NULL,
        UNIQUE (deployment_generation_id, source_namespace, account_snapshot_id),
        FOREIGN KEY (deployment_generation_id, paper_account_environment_id, store_identity)
            REFERENCES policy_state_deployments(deployment_generation_id, paper_account_environment_id, store_identity)
            ON DELETE RESTRICT
    )""",
)


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False, default=_json_default)


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _sha256_text(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _migration_sha256() -> str:
    return _sha256_text("\n".join(SCHEMA_V1_STATEMENTS))


def _expected_schema_object_names() -> set[str]:
    names: set[str] = set()
    for statement in SCHEMA_V1_STATEMENTS:
        words = statement.lstrip().split()
        if len(words) >= 3 and words[0].upper() == "CREATE":
            if words[1].upper() == "TABLE":
                names.add(words[2])
            elif words[1].upper() == "UNIQUE" and len(words) >= 4 and words[2].upper() == "INDEX":
                names.add(words[3])
    return names


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.isoformat(timespec="microseconds")


def _parse_datetime(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parse_date(value: str | None) -> date | None:
    return None if value is None else date.fromisoformat(value)


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _decimal_value(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _aware_iso(value: datetime) -> str:
    return _utc_text(value)


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _decision_payload(decision: DecisionIdentity) -> dict[str, object]:
    return {
        "deployment_identity": decision.deployment_identity.identity_payload(),
        "deployment_generation_id": decision.deployment_generation_id,
        "decision_slot_id": decision.decision_slot_id,
        "decision_id": decision.decision_id,
        "decision_category": decision.category.value,
        "subject_type": decision.subject_type.value,
        "subject_id": decision.subject_id,
        "sequence": decision.sequence,
        "snapshot_sha256": decision.snapshot_sha256,
        "clock": decision.clock.identity_payload(),
    }


def _holding_payload(holding: HoldingEpisode) -> dict[str, object]:
    return {
        "deployment_generation_id": holding.deployment_generation_id,
        "security_id": holding.security_id,
        "symbol": holding.symbol,
        "broker_symbol": holding.broker_symbol,
        "opening_action_id": holding.opening_action_id,
        "holding_episode_id": holding.holding_episode_id,
        "initial_filled_quantity": str(holding.initial_filled_quantity),
        "opening_later_fills_quantity": str(holding.opening_later_fills_quantity),
        "remaining_quantity": str(holding.remaining_quantity),
        "completed_additions_quantity": str(holding.completed_additions_quantity),
        "addition_count": holding.addition_count,
        "entry_price": _decimal_text(holding.entry_price),
        "cost_basis": _decimal_text(holding.cost_basis),
        "realized_pnl": _decimal_text(holding.realized_pnl),
        "committed_risk": _decimal_text(holding.committed_risk),
        "committed_risk_basis": holding.committed_risk_basis,
        "proposed_stop_price": _decimal_text(holding.proposed_stop_price),
        "proposed_stop_action_id": holding.proposed_stop_action_id,
        "confirmed_protective_stop_price": _decimal_text(holding.confirmed_protective_stop_price),
        "confirmed_stop_action_id": holding.confirmed_stop_action_id,
        "confirmed_stop_client_order_id": holding.confirmed_stop_client_order_id,
        "confirmed_stop_broker_order_id": holding.confirmed_stop_broker_order_id,
        "confirmed_stop_observed_at": None if holding.confirmed_stop_observed_at is None else _aware_iso(holding.confirmed_stop_observed_at),
        "peak_price": _decimal_text(holding.peak_price),
        "valuation_at": None if holding.valuation_at is None else _aware_iso(holding.valuation_at),
        "last_accepted_session": None if holding.last_accepted_session is None else holding.last_accepted_session.isoformat(),
        "policy_flags": list(holding.policy_flags),
        "pending_action_ids": list(holding.pending_action_ids),
        "applied_action_fill_watermarks": [
            (action_id, str(quantity)) for action_id, quantity in holding.applied_action_fill_watermarks
        ],
        "completed_addition_action_ids": list(holding.completed_addition_action_ids),
        "last_exit_tier": holding.last_exit_tier,
    }


def _deployment_from_payload(value: Mapping[str, object]) -> PolicyDeploymentIdentity:
    fields = (
        "policy_artifact_id",
        "capability_manifest_id",
        "policy_interface_version",
        "feature_contract_id",
        "feature_calculator_id",
        "source_revision",
        "runtime_identity",
        "execution_profile_id",
        "paper_account_environment_id",
        "store_identity",
    )
    return PolicyDeploymentIdentity(**{field: str(value[field]) for field in fields})


def _clock_from_payload(value: Mapping[str, object]) -> DecisionClock:
    return DecisionClock(
        exchange_id=str(value["exchange_id"]),
        decision_session=date.fromisoformat(str(value["decision_session"])),
        as_of_cutoff_at=datetime.fromisoformat(str(value["as_of_cutoff_at"])),
        next_execution_session=date.fromisoformat(str(value["next_execution_session"])),
        account_valuation_session=date.fromisoformat(str(value["account_valuation_session"])),
        account_valuation_at=datetime.fromisoformat(str(value["account_valuation_at"])),
    )


def _decision_from_payload(value: Mapping[str, object]) -> DecisionIdentity:
    return DecisionIdentity.build(
        deployment=_deployment_from_payload(value["deployment_identity"]),  # type: ignore[arg-type]
        clock=_clock_from_payload(value["clock"]),  # type: ignore[arg-type]
        snapshot_sha256=str(value["snapshot_sha256"]),
        category=str(value["decision_category"]),
        subject_type=str(value["subject_type"]),
        subject_id=str(value["subject_id"]),
        sequence=None if value.get("sequence") is None else int(value["sequence"]),
    )


def _holding_from_row(row: sqlite3.Row) -> HoldingEpisode:
    return HoldingEpisode(
        deployment_generation_id=str(row["deployment_generation_id"]),
        security_id=str(row["security_id"]),
        symbol=str(row["symbol"]),
        broker_symbol=str(row["broker_symbol"]),
        opening_action_id=str(row["opening_action_id"]),
        initial_filled_quantity=Decimal(row["initial_filled_quantity"]),
        opening_later_fills_quantity=Decimal(row["opening_later_fills_quantity"]),
        remaining_quantity=Decimal(row["remaining_quantity"]),
        completed_additions_quantity=Decimal(row["completed_additions_quantity"]),
        addition_count=int(row["addition_count"]),
        entry_price=_decimal_value(row["entry_price"]),
        cost_basis=_decimal_value(row["cost_basis"]),
        realized_pnl=_decimal_value(row["realized_pnl"]),
        committed_risk=_decimal_value(row["committed_risk"]),
        committed_risk_basis=row["committed_risk_basis"],
        proposed_stop_price=_decimal_value(row["proposed_stop_price"]),
        proposed_stop_action_id=row["proposed_stop_action_id"],
        confirmed_protective_stop_price=_decimal_value(row["confirmed_protective_stop_price"]),
        confirmed_stop_action_id=row["confirmed_stop_action_id"],
        confirmed_stop_client_order_id=row["confirmed_stop_client_order_id"],
        confirmed_stop_broker_order_id=row["confirmed_stop_broker_order_id"],
        confirmed_stop_observed_at=_parse_datetime(row["confirmed_stop_observed_at"]),
        peak_price=_decimal_value(row["peak_price"]),
        valuation_at=_parse_datetime(row["valuation_at"]),
        last_accepted_session=_parse_date(row["last_accepted_session"]),
        policy_flags=tuple(tuple(item) for item in json.loads(row["policy_flags_json"])),
        pending_action_ids=tuple(json.loads(row["pending_action_ids_json"])),
        applied_action_fill_watermarks=tuple(
            (str(action_id), Decimal(quantity))
            for action_id, quantity in json.loads(row["applied_action_fill_watermarks_json"])
        ),
        completed_addition_action_ids=tuple(json.loads(row["completed_addition_action_ids_json"])),
        last_exit_tier=int(row["last_exit_tier"]),
        state_version=int(row["state_version"]),
    )


def _attempt_payload(attempt: ActionOrderAttempt) -> dict[str, object]:
    return {
        "attempt_number": attempt.attempt_number,
        "requested_quantity": str(attempt.requested_quantity),
        "confirmed_filled_quantity": str(attempt.confirmed_filled_quantity),
        "status": attempt.status.value,
        "terminal_status": None if attempt.terminal_status is None else attempt.terminal_status.value,
        "client_order_id": attempt.client_order_id,
        "broker_order_id": attempt.broker_order_id,
        "client_order_aliases": list(attempt.client_order_aliases),
        "broker_order_aliases": list(attempt.broker_order_aliases),
    }


def _action_payload(intent: ActionIntent) -> dict[str, object]:
    return {
        **intent.immutable_payload(),
        "logical_action_id": intent.logical_action_id,
        "status": intent.status.value,
        "confirmed_filled_quantity": str(intent.confirmed_filled_quantity),
        "residual_quantity": str(intent.residual_quantity),
        "resolution_reason": intent.resolution_reason,
        "order_attempts": [_attempt_payload(attempt) for attempt in intent.order_attempts],
    }


def _action_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> ActionIntent:
    decision_row = conn.execute(
        "SELECT decision_json FROM policy_state_decisions WHERE decision_id=?",
        (row["decision_id"],),
    ).fetchone()
    if decision_row is None:
        raise ValueError("action decision identity is missing")
    decision = _decision_from_payload(json.loads(decision_row[0]))
    attempt_rows = conn.execute(
        "SELECT * FROM policy_state_order_attempts WHERE logical_action_id=? ORDER BY attempt_number",
        (row["logical_action_id"],),
    ).fetchall()
    alias_rows = conn.execute(
        """SELECT attempt_number, reference_kind, external_order_id
           FROM policy_state_order_reference_aliases
           WHERE logical_action_id=? ORDER BY first_seen_at_utc, provider_id, external_order_id""",
        (row["logical_action_id"],),
    ).fetchall()
    aliases_by_attempt: dict[int, dict[str, list[str]]] = {}
    for alias in alias_rows:
        refs = aliases_by_attempt.setdefault(
            int(alias["attempt_number"]),
            {"client_order_id": [], "broker_order_id": []},
        )
        external_id = str(alias["external_order_id"])
        if external_id not in refs[str(alias["reference_kind"])]:
            refs[str(alias["reference_kind"])].append(external_id)
    attempts = tuple(
        ActionOrderAttempt(
            attempt_number=int(attempt["attempt_number"]),
            requested_quantity=Decimal(attempt["requested_quantity"]),
            confirmed_filled_quantity=Decimal(attempt["confirmed_quantity"]),
            status=ActionAttemptStatus(attempt["status"]),
            client_order_id=attempt["client_order_id"],
            broker_order_id=attempt["broker_order_id"],
            terminal_status=None if attempt["terminal_status"] is None else ActionAttemptStatus(attempt["terminal_status"]),
            client_order_aliases=tuple(
                value
                for value in aliases_by_attempt.get(int(attempt["attempt_number"]), {}).get("client_order_id", [])
                if value != attempt["client_order_id"]
            ),
            broker_order_aliases=tuple(
                value
                for value in aliases_by_attempt.get(int(attempt["attempt_number"]), {}).get("broker_order_id", [])
                if value != attempt["broker_order_id"]
            ),
        )
        for attempt in attempt_rows
    )
    return ActionIntent(
        decision=decision,
        security_id=str(row["security_id"]),
        broker_symbol=str(row["broker_symbol"]),
        role=ActionRole(row["role"]),
        side=OrderSide(row["side"]),
        requested_quantity=Decimal(row["requested_quantity"]),
        holding_episode_id=row["holding_episode_id"],
        confirmed_filled_quantity=Decimal(row["confirmed_quantity"]),
        status=ActionStatus(row["status"]),
        reservation_price=_decimal_value(row["reservation_price"]),
        reservation_price_basis=row["reservation_price_basis"],
        reservation_stop_price=_decimal_value(row["reservation_stop_price"]),
        risk_per_unit=_decimal_value(row["risk_per_unit"]),
        risk_basis=row["risk_basis"],
        exit_tier=row["exit_tier"],
        snapshot_original_quantity=_decimal_value(row["snapshot_original_quantity"]),
        fraction_of_original_quantity=_decimal_value(row["fraction_of_original_quantity"]),
        rounding_rule_id=row["rounding_rule_id"],
        order_attempts=attempts,
        resolution_reason=row["resolution_reason"],
    )


def _replacement_holding_id(intent: ActionIntent) -> str:
    """Derive the only holding episode a replacement opening action may own."""
    return HoldingEpisode.open(
        deployment_generation_id=intent.deployment_generation_id,
        security_id=intent.security_id,
        symbol=intent.broker_symbol,
        broker_symbol=intent.broker_symbol,
        opening_action_id=intent.logical_action_id,
        initial_filled_quantity=Decimal("1"),
        entry_price=None,
    ).holding_episode_id


def _validate_replacement_holding(intent: ActionIntent, holding: HoldingEpisode) -> None:
    if (
        intent.role is ActionRole.REPLACEMENT
        and (
            holding.holding_episode_id != _replacement_holding_id(intent)
            or holding.opening_action_id != intent.logical_action_id
            or holding.deployment_generation_id != intent.deployment_generation_id
            or holding.security_id != intent.security_id
            or holding.broker_symbol != intent.broker_symbol
        )
    ):
        raise ValueError("replacement holding episode must be its own opening holding")


def _provider_order_references(
    conn: sqlite3.Connection,
    logical_action_id: str,
) -> tuple[ProviderOrderReference, ...]:
    rows = conn.execute(
        """SELECT provider_id, paper_account_environment_id, store_identity, reference_kind,
                  external_order_id, attempt_number, source_payload_sha256, first_seen_at_utc
           FROM policy_state_order_reference_aliases
           WHERE logical_action_id=?
           ORDER BY provider_id, reference_kind, external_order_id, first_seen_at_utc""",
        (logical_action_id,),
    ).fetchall()
    return tuple(
        ProviderOrderReference(
            provider_id=str(row["provider_id"]),
            paper_account_environment_id=str(row["paper_account_environment_id"]),
            store_identity=str(row["store_identity"]),
            reference_kind=str(row["reference_kind"]),
            external_order_id=str(row["external_order_id"]),
            attempt_number=int(row["attempt_number"]),
            source_payload_sha256=str(row["source_payload_sha256"]),
            first_seen_at_utc=datetime.fromisoformat(str(row["first_seen_at_utc"]).replace("Z", "+00:00")),
        )
        for row in rows
    )


def _action_projection_from_row(
    conn: sqlite3.Connection,
    intent: ActionIntent,
    row: sqlite3.Row,
) -> ActionStateProjection:
    return replace(
        project_action_state(intent),
        state_version=int(row["state_version"]),
        provider_order_references=_provider_order_references(conn, intent.logical_action_id),
    )


def _portfolio_payload(snapshot: PortfolioStateSnapshot) -> dict[str, object]:
    return {
        "deployment_identity": snapshot.deployment_identity.identity_payload(),
        "deployment_generation_id": snapshot.deployment_identity.deployment_generation_id,
        "clock": snapshot.clock.identity_payload(),
        "portfolio_snapshot_id": snapshot.portfolio_snapshot_id,
        "source_namespace": snapshot.source_namespace,
        "account_snapshot_id": snapshot.account_snapshot_id,
        "equity": _decimal_text(snapshot.equity),
        "cash": _decimal_text(snapshot.cash),
        "gross_exposure": _decimal_text(snapshot.gross_exposure),
        "open_risk": _decimal_text(snapshot.open_risk),
        "portfolio_peak_equity": _decimal_text(snapshot.portfolio_peak_equity),
        "last_accepted_session": None if snapshot.last_accepted_session is None else snapshot.last_accepted_session.isoformat(),
        "policy_flags": list(snapshot.policy_flags),
    }


def _portfolio_from_payload(value: Mapping[str, object]) -> PortfolioStateSnapshot:
    return PortfolioStateSnapshot(
        deployment_identity=_deployment_from_payload(value["deployment_identity"]),  # type: ignore[arg-type]
        clock=_clock_from_payload(value["clock"]),  # type: ignore[arg-type]
        source_namespace=str(value["source_namespace"]),
        account_snapshot_id=str(value["account_snapshot_id"]),
        equity=_decimal_value(value.get("equity")),  # type: ignore[arg-type]
        cash=_decimal_value(value.get("cash")),  # type: ignore[arg-type]
        gross_exposure=_decimal_value(value.get("gross_exposure")),  # type: ignore[arg-type]
        open_risk=_decimal_value(value.get("open_risk")),  # type: ignore[arg-type]
        portfolio_peak_equity=_decimal_value(value.get("portfolio_peak_equity")),  # type: ignore[arg-type]
        last_accepted_session=_parse_date(value.get("last_accepted_session")),  # type: ignore[arg-type]
        policy_flags=tuple(tuple(item) for item in value.get("policy_flags", [])),  # type: ignore[arg-type]
    )


class PolicyExecutionStateStore:
    """Policy state database whose path and identity are always explicit."""

    def __init__(self, db_path: str | Path, *, store_identity: str) -> None:
        if db_path is None or not str(db_path).strip() or str(db_path) == ":memory:":
            raise ValueError("db_path must be an explicit file path")
        if not isinstance(store_identity, str) or not store_identity.strip():
            raise ValueError("store_identity must be explicit and non-empty")
        self.db_path = str(Path(db_path).expanduser().resolve())
        self.store_identity = store_identity.strip()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        return conn

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def migrate(self) -> int:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._transaction(write=True) as conn:
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "policy_state_database_identity" in tables:
                existing = conn.execute(
                    "SELECT store_identity FROM policy_state_database_identity WHERE singleton=1"
                ).fetchone()
                if existing is not None and existing[0] != self.store_identity:
                    raise StoreIdentityConflictError("database is bound to a different store identity")
            migrations_exist = "policy_state_schema_migrations" in tables
            if migrations_exist:
                row = conn.execute(
                    "SELECT migration_sha256 FROM policy_state_schema_migrations WHERE version=?",
                    (SCHEMA_VERSION,),
                ).fetchone()
                if row is not None:
                    if row[0] != _migration_sha256():
                        raise ValueError("policy schema migration checksum conflicts with stored version")
                    present = {
                        str(item[0])
                        for item in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','index')")
                    }
                    missing = _expected_schema_object_names() - present
                    if missing:
                        raise ValueError(f"policy state schema objects are missing: {sorted(missing)}")
                    self._bind_database_identity(conn)
                    return SCHEMA_VERSION
            for statement in SCHEMA_V1_STATEMENTS:
                conn.execute(statement)
            self._bind_database_identity(conn)
            conn.execute(
                "INSERT INTO policy_state_schema_migrations(version, migration_sha256, applied_at_utc) VALUES (?, ?, ?)",
                (SCHEMA_VERSION, _migration_sha256(), _utc_text(datetime.now().astimezone())),
            )
        return SCHEMA_VERSION

    def _bind_database_identity(self, conn: sqlite3.Connection) -> None:
        namespace_id = f"db-namespace:sha256:{_sha256_text(self.store_identity)}"
        binding_sha = _sha256_text(_canonical_json({"store_identity": self.store_identity, "namespace_id": namespace_id}))
        row = conn.execute(
            "SELECT store_identity, namespace_id, binding_sha256 FROM policy_state_database_identity WHERE singleton=1"
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO policy_state_database_identity(singleton, store_identity, namespace_id, binding_sha256) VALUES (1, ?, ?, ?)",
                (self.store_identity, namespace_id, binding_sha),
            )
        elif tuple(row) != (self.store_identity, namespace_id, binding_sha):
            raise StoreIdentityConflictError("database store identity binding does not match this caller")

    def _require_ready(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT migration_sha256 FROM policy_state_schema_migrations WHERE version=?",
            (SCHEMA_VERSION,),
        ).fetchone()
        if row is None or row[0] != _migration_sha256():
            raise ValueError("policy state schema v1 is not migrated or has a checksum conflict")
        identity = conn.execute(
            "SELECT store_identity FROM policy_state_database_identity WHERE singleton=1"
        ).fetchone()
        if identity is None or identity[0] != self.store_identity:
            raise StoreIdentityConflictError("database store identity is not bound to this caller")

    def rollback_schema_v1(self) -> None:
        tables = tuple(statement for statement in SCHEMA_V1_STATEMENTS if statement.lstrip().upper().startswith("CREATE TABLE"))
        table_names = [statement.split("CREATE TABLE", 1)[1].split("(", 1)[0].strip() for statement in tables]
        droppable = [name for name in reversed(table_names) if name not in {"policy_state_schema_migrations", "policy_state_database_identity"}]
        with self._transaction(write=True) as conn:
            self._require_ready(conn)
            for name in droppable:
                count = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                if count:
                    raise SchemaRollbackBlockedError("dependent policy state prevents schema rollback")
            for name in reversed(table_names):
                conn.execute(f'DROP TABLE "{name}"')

    def register_deployment_identity(
        self,
        identity: PolicyDeploymentIdentity,
        *,
        lifecycle: str,
        handler_identity: str,
        guard_id: str,
    ) -> str:
        lifecycle = _required_text(lifecycle, "lifecycle")
        if lifecycle not in {"prepared", "eligible", "active", "draining", "retired", "rejected"}:
            raise ValueError("deployment lifecycle is unsupported")
        handler_identity = _required_text(handler_identity, "handler_identity")
        guard_id = _required_text(guard_id, "guard_id")
        if identity.store_identity != self.store_identity:
            raise StoreIdentityConflictError("deployment store identity does not match this database")
        payload_json = _canonical_json(identity.identity_payload())
        with self._transaction(write=True) as conn:
            self._require_ready(conn)
            existing = conn.execute(
                "SELECT identity_json, handler_identity, guard_id FROM policy_state_deployments WHERE deployment_generation_id=?",
                (identity.deployment_generation_id,),
            ).fetchone()
            if existing is not None:
                if (existing[0], existing[1], existing[2]) != (payload_json, handler_identity, guard_id):
                    raise IdentityConflictError("deployment generation ID has a conflicting immutable identity")
                return identity.deployment_generation_id
            conn.execute(
                """INSERT INTO policy_state_deployments(
                    deployment_generation_id, policy_artifact_id, capability_manifest_id,
                    policy_interface_version, feature_contract_id, feature_calculator_id,
                    source_revision, runtime_identity, execution_profile_id,
                    paper_account_environment_id, store_identity, lifecycle,
                    handler_identity, guard_id, identity_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    identity.deployment_generation_id,
                    identity.policy_artifact_id,
                    identity.capability_manifest_id,
                    identity.policy_interface_version,
                    identity.feature_contract_id,
                    identity.feature_calculator_id,
                    identity.source_revision,
                    identity.runtime_identity,
                    identity.execution_profile_id,
                    identity.paper_account_environment_id,
                    identity.store_identity,
                    lifecycle,
                    handler_identity,
                    guard_id,
                    payload_json,
                    _utc_text(datetime.now().astimezone()),
                ),
            )
        return identity.deployment_generation_id

    def set_active_generation(
        self,
        paper_account_environment_id: str,
        *,
        expected_generation_id: str | None,
        expected_pointer_version: int | None,
        new_generation_id: str,
        readiness_evidence_ref: str,
        outgoing_entries_reconciled: bool,
        compatible_handler_available: bool = True,
        reason: str = "offline fixture pointer transition",
    ) -> int:
        account = _required_text(paper_account_environment_id, "paper_account_environment_id")
        target_id = _required_text(new_generation_id, "new_generation_id")
        evidence = _required_text(readiness_evidence_ref, "readiness_evidence_ref")
        reason = _required_text(reason, "reason")
        if not evidence.startswith(("synthetic-", "offline-", "prepared-")):
            raise ValueError("deployment pointer evidence must identify synthetic, offline, or prepared-state evidence")
        with self._transaction(write=True) as conn:
            self._require_ready(conn)
            target = conn.execute(
                "SELECT lifecycle FROM policy_state_deployments WHERE deployment_generation_id=? AND paper_account_environment_id=? AND store_identity=?",
                (target_id, account, self.store_identity),
            ).fetchone()
            if target is None:
                raise ValueError("target deployment identity is not registered for this account/store")
            current = conn.execute(
                "SELECT active_generation_id, pointer_version FROM policy_state_active_pointers WHERE paper_account_environment_id=? AND store_identity=?",
                (account, self.store_identity),
            ).fetchone()
            current_id = None if current is None else str(current[0])
            current_version = 0 if current is None else int(current[1])
            observed_pointer_version = None if current is None else current_version
            if current_id != expected_generation_id or observed_pointer_version != expected_pointer_version:
                raise ConcurrentStateUpdateError("active deployment pointer changed since it was read")
            if current_id == target_id:
                return current_version
            if not outgoing_entries_reconciled:
                raise ValueError("outgoing entry orders must be reconciled before switching generations")
            if current_id is not None:
                pending_entry = conn.execute(
                    """SELECT logical_action_id FROM policy_state_actions
                       WHERE deployment_generation_id=? AND role IN ('entry', 'replacement')
                       AND status NOT IN ('filled', 'resolved') LIMIT 1""",
                    (current_id,),
                ).fetchone()
                if pending_entry is not None:
                    raise ValueError("outgoing generation still has an unresolved entry intention")

            next_version = current_version + 1
            readiness_state = "prepared_fixture" if compatible_handler_available else "degraded_pinned_handler"
            event_kind = "activate" if current_id is None else (
                "rollback" if self._generation_was_active(conn, account, target_id) else "activate"
            )
            event_id = "deployment-event:sha256:" + _sha256_text(
                _canonical_json(
                    {
                        "account": account,
                        "store": self.store_identity,
                        "old": current_id,
                        "new": target_id,
                        "version": next_version,
                        "evidence": evidence,
                    }
                )
            )
            if current is None:
                conn.execute(
                    "INSERT INTO policy_state_active_pointers(paper_account_environment_id, store_identity, active_generation_id, readiness_state, pointer_version, updated_at_utc) VALUES (?, ?, ?, ?, ?, ?)",
                    (account, self.store_identity, target_id, readiness_state, next_version, _utc_text(datetime.now().astimezone())),
                )
            else:
                changed = conn.execute(
                    """UPDATE policy_state_active_pointers
                       SET active_generation_id=?, readiness_state=?, pointer_version=?, updated_at_utc=?
                       WHERE paper_account_environment_id=? AND store_identity=? AND active_generation_id=? AND pointer_version=?""",
                    (
                        target_id,
                        readiness_state,
                        next_version,
                        _utc_text(datetime.now().astimezone()),
                        account,
                        self.store_identity,
                        current_id,
                        expected_pointer_version,
                    ),
                ).rowcount
                if changed != 1:
                    raise ConcurrentStateUpdateError("active deployment pointer compare-and-set failed")
            if current_id is not None:
                conn.execute(
                    "UPDATE policy_state_deployments SET lifecycle='draining' WHERE deployment_generation_id=?",
                    (current_id,),
                )
            conn.execute(
                "UPDATE policy_state_deployments SET lifecycle='active' WHERE deployment_generation_id=?",
                (target_id,),
            )
            conn.execute(
                """INSERT INTO policy_state_deployment_events(
                    event_id, paper_account_environment_id, store_identity, old_generation_id,
                    new_generation_id, event_kind, readiness_evidence_ref, reason,
                    expected_pointer_version, resulting_pointer_version, event_time_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    account,
                    self.store_identity,
                    current_id,
                    target_id,
                    event_kind if compatible_handler_available else "degraded",
                    evidence,
                    reason if compatible_handler_available else f"{reason}; pinned handler unavailable",
                    current_version,
                    next_version,
                    _utc_text(datetime.now().astimezone()),
                ),
            )
        return next_version

    @staticmethod
    def _generation_was_active(conn: sqlite3.Connection, account: str, generation_id: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM policy_state_deployment_events WHERE paper_account_environment_id=? AND new_generation_id=? LIMIT 1",
            (account, generation_id),
        ).fetchone()
        return row is not None

    def load_active_generation(self, paper_account_environment_id: str) -> str | None:
        account = _required_text(paper_account_environment_id, "paper_account_environment_id")
        with self._transaction(write=False) as conn:
            self._require_ready(conn)
            row = conn.execute(
                "SELECT active_generation_id FROM policy_state_active_pointers WHERE paper_account_environment_id=? AND store_identity=?",
                (account, self.store_identity),
            ).fetchone()
            return None if row is None else str(row[0])

    def load_active_generation_pointer(self, paper_account_environment_id: str) -> ActiveGenerationPointer:
        account = _required_text(paper_account_environment_id, "paper_account_environment_id")
        with self._transaction(write=False) as conn:
            self._require_ready(conn)
            row = conn.execute(
                """SELECT active_generation_id, pointer_version, readiness_state
                   FROM policy_state_active_pointers
                   WHERE paper_account_environment_id=? AND store_identity=?""",
                (account, self.store_identity),
            ).fetchone()
            if row is None:
                return ActiveGenerationPointer(None, None, None)
            return ActiveGenerationPointer(str(row[0]), int(row[1]), str(row[2]))

    def load_deployment_chain(self, deployment_generation_id: str) -> PolicyDeploymentChain:
        generation = _required_text(deployment_generation_id, "deployment_generation_id")
        with self._transaction(write=False) as conn:
            self._require_ready(conn)
            deployment = conn.execute(
                "SELECT * FROM policy_state_deployments WHERE deployment_generation_id=? AND store_identity=?",
                (generation, self.store_identity),
            ).fetchone()
            if deployment is None:
                raise KeyError("deployment generation was not found")
            identity = _deployment_from_payload(json.loads(deployment["identity_json"]))
            pointer = conn.execute(
                """SELECT active_generation_id, pointer_version, readiness_state
                   FROM policy_state_active_pointers
                   WHERE paper_account_environment_id=? AND store_identity=?""",
                (identity.paper_account_environment_id, self.store_identity),
            ).fetchone()
            events = tuple(
                {
                    "event_id": str(event["event_id"]),
                    "old_generation_id": event["old_generation_id"],
                    "new_generation_id": str(event["new_generation_id"]),
                    "event_kind": str(event["event_kind"]),
                    "readiness_evidence_ref": str(event["readiness_evidence_ref"]),
                    "reason": str(event["reason"]),
                    "expected_pointer_version": int(event["expected_pointer_version"]),
                    "resulting_pointer_version": int(event["resulting_pointer_version"]),
                    "event_time_utc": str(event["event_time_utc"]),
                }
                for event in conn.execute(
                    """SELECT * FROM policy_state_deployment_events
                       WHERE paper_account_environment_id=? AND store_identity=?
                         AND (old_generation_id=? OR new_generation_id=?)
                       ORDER BY resulting_pointer_version""",
                    (identity.paper_account_environment_id, self.store_identity, generation, generation),
                )
            )
            return PolicyDeploymentChain(
                deployment_identity=identity,
                lifecycle=str(deployment["lifecycle"]),
                handler_identity=str(deployment["handler_identity"]),
                guard_id=str(deployment["guard_id"]),
                active_for_account=pointer is not None and pointer["active_generation_id"] == generation,
                pointer_version=None if pointer is None else int(pointer["pointer_version"]),
                readiness_state=None if pointer is None else str(pointer["readiness_state"]),
                events=events,
            )

    def load_decision_record(self, decision_id: str) -> PolicyDecisionRecord:
        identity = _required_text(decision_id, "decision_id")
        with self._transaction(write=False) as conn:
            self._require_ready(conn)
            row = conn.execute(
                """SELECT decision_json, policy_payload_json, guard_payload_json, effective_action_payload_json
                   FROM policy_state_decisions WHERE decision_id=? AND store_identity=?""",
                (identity, self.store_identity),
            ).fetchone()
            if row is None:
                raise KeyError("decision was not found")
            return PolicyDecisionRecord(
                decision=_decision_from_payload(json.loads(row[0])),
                policy_payload=json.loads(row[1]),
                guard_payload=json.loads(row[2]),
                effective_action_payload=json.loads(row[3]),
            )

    def load_decision_record_by_slot(self, decision_slot_id: str) -> PolicyDecisionRecord:
        slot = _required_text(decision_slot_id, "decision_slot_id")
        with self._transaction(write=False) as conn:
            self._require_ready(conn)
            row = conn.execute(
                """SELECT decision_json, policy_payload_json, guard_payload_json, effective_action_payload_json
                   FROM policy_state_decisions WHERE decision_slot_id=? AND store_identity=?""",
                (slot, self.store_identity),
            ).fetchone()
            if row is None:
                raise KeyError("decision slot was not found")
            return PolicyDecisionRecord(
                decision=_decision_from_payload(json.loads(row[0])),
                policy_payload=json.loads(row[1]),
                guard_payload=json.loads(row[2]),
                effective_action_payload=json.loads(row[3]),
            )

    def record_decision(
        self,
        decision: DecisionIdentity,
        *,
        policy_payload: Mapping[str, object],
        guard_payload: Mapping[str, object],
        effective_action_payload: Mapping[str, object],
    ) -> str:
        deployment = decision.deployment_identity
        self._validate_deployment_scope(deployment)
        with self._transaction(write=True) as conn:
            self._require_ready(conn)
            self._record_decision_in_transaction(
                conn,
                decision,
                policy_payload=policy_payload,
                guard_payload=guard_payload,
                effective_action_payload=effective_action_payload,
            )
        return decision.decision_id

    def _record_decision_in_transaction(
        self,
        conn: sqlite3.Connection,
        decision: DecisionIdentity,
        *,
        policy_payload: Mapping[str, object],
        guard_payload: Mapping[str, object],
        effective_action_payload: Mapping[str, object],
    ) -> str:
        """Persist or validate an immutable decision on the caller's connection."""
        deployment = decision.deployment_identity
        self._validate_deployment_scope(deployment)
        account = deployment.paper_account_environment_id
        self._require_deployment(conn, deployment.deployment_generation_id, account)
        decision_json = _canonical_json(_decision_payload(decision))
        policy_json = _canonical_json(policy_payload)
        guard_json = _canonical_json(guard_payload)
        effective_json = _canonical_json(effective_action_payload)
        clock = decision.clock
        existing = conn.execute(
            "SELECT decision_id, decision_json, policy_payload_json, guard_payload_json, effective_action_payload_json FROM policy_state_decisions WHERE decision_slot_id=?",
            (decision.decision_slot_id,),
        ).fetchone()
        if existing is not None:
            if (
                existing[0] != decision.decision_id
                or existing[1] != decision_json
                or existing[2] != policy_json
                or existing[3] != guard_json
                or existing[4] != effective_json
            ):
                raise DecisionConflictError(
                    "decision slot already contains different immutable decision facts"
                )
            return decision.decision_id
        conn.execute(
            """INSERT INTO policy_state_decisions(
                decision_id, decision_slot_id, deployment_generation_id,
                paper_account_environment_id, store_identity, exchange_id, decision_session,
                decision_category, subject_type, subject_id, sequence, as_of_cutoff_at,
                next_execution_session, account_valuation_session, account_valuation_at,
                snapshot_sha256, decision_json, policy_payload_json, guard_payload_json,
                effective_action_payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                decision.decision_id,
                decision.decision_slot_id,
                deployment.deployment_generation_id,
                account,
                self.store_identity,
                clock.exchange_id,
                clock.decision_session.isoformat(),
                decision.category.value,
                decision.subject_type.value,
                decision.subject_id,
                decision.sequence or 0,
                _aware_iso(clock.as_of_cutoff_at),
                clock.next_execution_session.isoformat(),
                clock.account_valuation_session.isoformat(),
                _aware_iso(clock.account_valuation_at),
                decision.snapshot_sha256,
                decision_json,
                policy_json,
                guard_json,
                effective_json,
            ),
        )
        return decision.decision_id

    def _validate_deployment_scope(self, identity: PolicyDeploymentIdentity) -> None:
        if identity.store_identity != self.store_identity:
            raise StoreIdentityConflictError("deployment store identity does not match this database")

    @staticmethod
    def _require_deployment(
        conn: sqlite3.Connection,
        generation_id: str,
        account: str | None = None,
    ) -> sqlite3.Row:
        if account is None:
            row = conn.execute(
                "SELECT * FROM policy_state_deployments WHERE deployment_generation_id=?",
                (generation_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM policy_state_deployments WHERE deployment_generation_id=? AND paper_account_environment_id=?",
                (generation_id, account),
            ).fetchone()
        if row is None:
            raise ValueError("deployment identity is not registered")
        return row

    @staticmethod
    def _holding_row(conn: sqlite3.Connection, holding_episode_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM policy_state_holdings WHERE holding_episode_id=?",
            (holding_episode_id,),
        ).fetchone()

    @staticmethod
    def _append_holding_history(
        conn: sqlite3.Connection,
        holding: HoldingEpisode,
        *,
        version: int,
        event_kind: str,
        logical_action_id: str | None,
        observed_at: datetime,
        evidence_ref: str | None = None,
    ) -> None:
        state = {**_holding_payload(holding), "state_version": version}
        if evidence_ref is not None:
            state["evidence_ref"] = evidence_ref
        state_json = _canonical_json(state)
        state_sha = _sha256_text(state_json)
        event_id = "holding-event:sha256:" + _sha256_text(
            _canonical_json(
                {
                    "holding_episode_id": holding.holding_episode_id,
                    "state_version": version,
                    "event_kind": event_kind,
                    "state_sha256": state_sha,
                }
            )
        )
        conn.execute(
            """INSERT INTO policy_state_holding_history(
                holding_episode_id, state_version, event_id, logical_action_id,
                event_kind, state_sha256, state_json, evidence_ref, observed_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                holding.holding_episode_id,
                version,
                event_id,
                logical_action_id,
                event_kind,
                state_sha,
                state_json,
                evidence_ref,
                _aware_iso(observed_at),
            ),
        )

    def _save_holding(
        self,
        conn: sqlite3.Connection,
        holding: HoldingEpisode,
        *,
        expected_version: int | None,
        event_kind: str,
        logical_action_id: str | None = None,
        observed_at: datetime | None = None,
        initial_version: int = 0,
        evidence_ref: str | None = None,
    ) -> int:
        identity = self._require_deployment(conn, holding.deployment_generation_id)
        if identity["store_identity"] != self.store_identity:
            raise StoreIdentityConflictError("holding store identity does not match this database")
        account = str(identity["paper_account_environment_id"])
        opening = conn.execute(
            "SELECT decision_id, deployment_generation_id, security_id FROM policy_state_actions WHERE logical_action_id=?",
            (holding.opening_action_id,),
        ).fetchone()
        if opening is None:
            raise ValueError("holding opening action must be durably recorded first")
        if opening["deployment_generation_id"] != holding.deployment_generation_id or opening["security_id"] != holding.security_id:
            raise ValueError("holding opening action generation/security does not match")
        existing = self._holding_row(conn, holding.holding_episode_id)
        now = observed_at or datetime.now().astimezone()
        if existing is None:
            if expected_version is not None:
                raise ConcurrentStateUpdateError("holding episode does not exist at the expected version")
            version = initial_version
            values = (
                holding.holding_episode_id,
                holding.deployment_generation_id,
                account,
                self.store_identity,
                holding.security_id,
                holding.symbol,
                holding.broker_symbol,
                opening["decision_id"],
                holding.opening_action_id,
                str(holding.initial_filled_quantity),
                str(holding.opening_later_fills_quantity),
                str(holding.remaining_quantity),
                str(holding.completed_additions_quantity),
                holding.addition_count,
                _decimal_text(holding.entry_price),
                _decimal_text(holding.cost_basis),
                _decimal_text(holding.realized_pnl),
                _decimal_text(holding.committed_risk),
                holding.committed_risk_basis,
                _decimal_text(holding.proposed_stop_price),
                holding.proposed_stop_action_id,
                _decimal_text(holding.confirmed_protective_stop_price),
                holding.confirmed_stop_action_id,
                holding.confirmed_stop_client_order_id,
                holding.confirmed_stop_broker_order_id,
                None if holding.confirmed_stop_observed_at is None else _aware_iso(holding.confirmed_stop_observed_at),
                _decimal_text(holding.peak_price),
                None if holding.valuation_at is None else _aware_iso(holding.valuation_at),
                None if holding.last_accepted_session is None else holding.last_accepted_session.isoformat(),
                _canonical_json(holding.policy_flags),
                _canonical_json(holding.pending_action_ids),
                _canonical_json([(action_id, str(quantity)) for action_id, quantity in holding.applied_action_fill_watermarks]),
                _canonical_json(holding.completed_addition_action_ids),
                holding.last_exit_tier,
                version,
            )
            conn.execute(
                """INSERT INTO policy_state_holdings(
                    holding_episode_id, deployment_generation_id, paper_account_environment_id, store_identity,
                    security_id, symbol, broker_symbol, opening_decision_id, opening_action_id,
                    initial_filled_quantity, opening_later_fills_quantity, remaining_quantity,
                    completed_additions_quantity, addition_count, entry_price, cost_basis, realized_pnl,
                    committed_risk, committed_risk_basis, proposed_stop_price, proposed_stop_action_id,
                    confirmed_protective_stop_price, confirmed_stop_action_id, confirmed_stop_client_order_id,
                    confirmed_stop_broker_order_id, confirmed_stop_observed_at, peak_price, valuation_at,
                    last_accepted_session, policy_flags_json, pending_action_ids_json,
                    applied_action_fill_watermarks_json, completed_addition_action_ids_json, last_exit_tier, state_version
                ) VALUES (""" + ",".join("?" for _ in values) + ")",
                values,
            )
        else:
            current_version = int(existing["state_version"])
            if expected_version is None or current_version != expected_version:
                raise ConcurrentStateUpdateError("holding episode changed since it was read")
            immutable = (
                existing["deployment_generation_id"],
                existing["paper_account_environment_id"],
                existing["store_identity"],
                existing["security_id"],
                existing["symbol"],
                existing["broker_symbol"],
                existing["opening_decision_id"],
                existing["opening_action_id"],
                existing["initial_filled_quantity"],
                existing["entry_price"],
            )
            expected_immutable = (
                holding.deployment_generation_id,
                account,
                self.store_identity,
                holding.security_id,
                holding.symbol,
                holding.broker_symbol,
                opening["decision_id"],
                holding.opening_action_id,
                str(holding.initial_filled_quantity),
                _decimal_text(holding.entry_price),
            )
            if immutable != expected_immutable:
                raise IdentityConflictError("holding generation, account, security, or opening identity cannot change")
            version = current_version + 1
            changed = conn.execute(
                """UPDATE policy_state_holdings SET
                    symbol=?, broker_symbol=?, opening_later_fills_quantity=?, remaining_quantity=?,
                    completed_additions_quantity=?, addition_count=?, entry_price=?, cost_basis=?, realized_pnl=?,
                    committed_risk=?, committed_risk_basis=?, proposed_stop_price=?, proposed_stop_action_id=?,
                    confirmed_protective_stop_price=?, confirmed_stop_action_id=?, confirmed_stop_client_order_id=?,
                    confirmed_stop_broker_order_id=?, confirmed_stop_observed_at=?, peak_price=?, valuation_at=?,
                    last_accepted_session=?, policy_flags_json=?, pending_action_ids_json=?,
                    applied_action_fill_watermarks_json=?, completed_addition_action_ids_json=?, last_exit_tier=?,
                    state_version=? WHERE holding_episode_id=? AND state_version=?""",
                (
                    holding.symbol,
                    holding.broker_symbol,
                    str(holding.opening_later_fills_quantity),
                    str(holding.remaining_quantity),
                    str(holding.completed_additions_quantity),
                    holding.addition_count,
                    _decimal_text(holding.entry_price),
                    _decimal_text(holding.cost_basis),
                    _decimal_text(holding.realized_pnl),
                    _decimal_text(holding.committed_risk),
                    holding.committed_risk_basis,
                    _decimal_text(holding.proposed_stop_price),
                    holding.proposed_stop_action_id,
                    _decimal_text(holding.confirmed_protective_stop_price),
                    holding.confirmed_stop_action_id,
                    holding.confirmed_stop_client_order_id,
                    holding.confirmed_stop_broker_order_id,
                    None if holding.confirmed_stop_observed_at is None else _aware_iso(holding.confirmed_stop_observed_at),
                    _decimal_text(holding.peak_price),
                    None if holding.valuation_at is None else _aware_iso(holding.valuation_at),
                    None if holding.last_accepted_session is None else holding.last_accepted_session.isoformat(),
                    _canonical_json(holding.policy_flags),
                    _canonical_json(holding.pending_action_ids),
                    _canonical_json([(action_id, str(quantity)) for action_id, quantity in holding.applied_action_fill_watermarks]),
                    _canonical_json(holding.completed_addition_action_ids),
                    holding.last_exit_tier,
                    version,
                    holding.holding_episode_id,
                    current_version,
                ),
            ).rowcount
            if changed != 1:
                raise ConcurrentStateUpdateError("holding episode compare-and-set failed")
        self._append_holding_history(
            conn,
            holding,
            version=version,
            event_kind=event_kind,
            logical_action_id=logical_action_id,
            observed_at=now,
            evidence_ref=evidence_ref,
        )
        return version

    def record_holding_episode(
        self,
        holding: HoldingEpisode,
        *,
        expected_version: int,
        evidence_ref: str,
    ) -> int:
        """Record only an evidence-backed position-reconciliation flag change."""
        evidence = _required_text(evidence_ref, "evidence_ref")
        with self._transaction(write=True) as conn:
            self._require_ready(conn)
            row = self._holding_row(conn, _required_text(holding.holding_episode_id, "holding_episode_id"))
            if row is None:
                raise ValueError("holdings must be created by a confirmed entry fill")
            current = _holding_from_row(row)
            if int(row["state_version"]) != expected_version:
                raise ConcurrentStateUpdateError("holding changed since the caller read it")
            candidate_with_current_flags = replace(holding, policy_flags=current.policy_flags, state_version=current.state_version)
            if candidate_with_current_flags != current:
                raise ValueError("offline holding reconciliation may change only policy flags")
            current_flags = dict(current.policy_flags)
            proposed_flags = dict(holding.policy_flags)
            if current.policy_flags == holding.policy_flags:
                return expected_version
            if set(current_flags) | set(proposed_flags) != {"position_reconciliation_required"}:
                raise ValueError("offline holding reconciliation supports only the position reconciliation flag")
            return self._save_holding(
                conn,
                holding,
                expected_version=expected_version,
                event_kind="position_reconciliation_flag_recorded",
                evidence_ref=evidence,
            )

    def load_holding_episode(self, holding_episode_id: str) -> HoldingEpisode:
        with self._transaction(write=False) as conn:
            self._require_ready(conn)
            row = self._holding_row(conn, _required_text(holding_episode_id, "holding_episode_id"))
            if row is None:
                raise KeyError("holding episode was not found")
            return _holding_from_row(row)

    def load_holding_episode_for_action(self, logical_action_id: str) -> HoldingEpisode:
        with self._transaction(write=False) as conn:
            self._require_ready(conn)
            row = conn.execute(
                """SELECT h.* FROM policy_state_holdings h
                   JOIN policy_state_actions a ON a.holding_episode_id=h.holding_episode_id
                   WHERE a.logical_action_id=?""",
                (_required_text(logical_action_id, "logical_action_id"),),
            ).fetchone()
            if row is None:
                raise KeyError("action has no persisted holding episode")
            return _holding_from_row(row)

    def retire_flat_holding_protection(
        self,
        logical_action_id: str,
        *,
        client_order_id: str,
        broker_order_id: str,
        account_snapshot_id: str,
        expected_holding_version: int,
        observed_at: datetime,
    ) -> HoldingEpisode:
        """Retire historical stop refs after a caller observes broker flat/absence.

        The caller must independently check a complete broker position and order
        snapshot. This transaction binds that observation to the terminal sell,
        closes the holding's active stop pointer, and records the old references.
        """
        client = _required_text(client_order_id, "client_order_id")
        broker = _required_text(broker_order_id, "broker_order_id")
        snapshot = _required_text(account_snapshot_id, "account_snapshot_id")
        _aware_iso(observed_at)
        with self._transaction(write=True) as conn:
            self._require_ready(conn)
            action, _ = self._load_action(conn, logical_action_id)
            if (
                action.role not in {ActionRole.SCALE_OUT, ActionRole.CLOSE}
                or action.side is not OrderSide.SELL
                or action.status not in {ActionStatus.FILLED, ActionStatus.RESOLVED}
                or action.confirmed_filled_quantity <= 0
                or action.holding_episode_id is None
            ):
                raise ValueError("flat stop retirement requires a terminal filled policy sell")
            row = self._holding_row(conn, action.holding_episode_id)
            if row is None:
                raise ValueError("flat stop retirement holding is missing")
            holding = _holding_from_row(row)
            flags = dict(holding.policy_flags)
            if holding.remaining_quantity != 0:
                raise ValueError("flat stop retirement requires zero durable shares")
            retired_by = flags.get("flat_stop_retired_by_action_id")
            if retired_by is not None:
                if (
                    retired_by == logical_action_id
                    and flags.get("flat_stop_retired_client_order_id") == client
                    and flags.get("flat_stop_retired_broker_order_id") == broker
                ):
                    return holding
                raise IdentityConflictError("flat stop was retired under different source facts")
            if int(row["state_version"]) != expected_holding_version:
                raise ConcurrentStateUpdateError("holding changed since flat broker observation")
            if (
                holding.remaining_quantity != 0
                or holding.pending_action_ids
                or holding.proposed_stop_action_id != holding.confirmed_stop_action_id
                or holding.confirmed_stop_client_order_id != client
                or holding.confirmed_stop_broker_order_id != broker
                or holding.deployment_generation_id != action.deployment_generation_id
                or flags.get("position_reconciliation_required")
            ):
                raise ValueError("flat holding or confirmed stop differs from terminal sell")
            flags.update({
                "flat_stop_retired_by_action_id": logical_action_id,
                "flat_stop_retired_client_order_id": client,
                "flat_stop_retired_broker_order_id": broker,
                "flat_stop_absence_snapshot_id": snapshot,
            })
            updated = replace(
                holding,
                proposed_stop_price=None,
                proposed_stop_action_id=None,
                confirmed_protective_stop_price=None,
                confirmed_stop_action_id=None,
                confirmed_stop_client_order_id=None,
                confirmed_stop_broker_order_id=None,
                confirmed_stop_observed_at=None,
                policy_flags=tuple(flags.items()),
            )
            next_version = self._save_holding(
                conn,
                updated,
                expected_version=expected_holding_version,
                event_kind="flat_stop_absence_confirmed",
                logical_action_id=logical_action_id,
                evidence_ref=snapshot,
                observed_at=observed_at,
            )
            return replace(updated, state_version=next_version)

    def update_holding_marks(
        self,
        holding_episode_id: str,
        *,
        valuation_at: datetime,
        peak_price: Decimal | None,
        expected_holding_version: int,
        last_accepted_session: date | None = None,
    ) -> HoldingEpisode:
        _aware_iso(valuation_at)
        with self._transaction(write=True) as conn:
            self._require_ready(conn)
            row = self._holding_row(conn, _required_text(holding_episode_id, "holding_episode_id"))
            if row is None:
                raise KeyError("holding episode was not found")
            if int(row["state_version"]) != expected_holding_version:
                raise ConcurrentStateUpdateError("holding changed since the caller read it")
            holding = _holding_from_row(row)
            updated = update_holding_marks(holding, valuation_at=valuation_at, peak_price=peak_price)
            if last_accepted_session is not None:
                if holding.last_accepted_session is not None and last_accepted_session < holding.last_accepted_session:
                    raise ValueError("last_accepted_session cannot decrease")
                updated = replace(updated, last_accepted_session=last_accepted_session)
            next_version = self._save_holding(
                conn,
                updated,
                expected_version=expected_holding_version,
                event_kind="holding_marks_updated",
                observed_at=valuation_at,
            )
            return replace(updated, state_version=next_version)

    def record_exit_management_state(
        self,
        decision: DecisionIdentity,
        *,
        holding_episode_id: str,
        policy_payload: Mapping[str, object],
        guard_payload: Mapping[str, object],
        effective_action_payload: Mapping[str, object],
        early_winner_hold: bool,
        breakeven_armed: bool,
        ema_trailing_active: bool,
        policy_scale_out_tier: int,
        peak_price: Decimal,
        expected_holding_version: int,
        observed_at: datetime,
    ) -> HoldingEpisode:
        """Atomically bind one fixed exit decision to restartable holding flags."""
        _aware_iso(observed_at)
        if (
            decision.category is not DecisionCategory.EXIT
            or decision.subject_type is not DecisionSubjectType.HOLDING
            or decision.subject_id != holding_episode_id
        ):
            raise ValueError("exit management decision must identify its holding")
        if any(type(value) is not bool for value in (early_winner_hold, breakeven_armed, ema_trailing_active)):
            raise TypeError("exit management flags must be boolean")
        if type(policy_scale_out_tier) is not int or policy_scale_out_tier < 0:
            raise ValueError("policy scale-out tier must be non-negative")
        if not isinstance(peak_price, Decimal) or not peak_price.is_finite() or peak_price <= 0:
            raise ValueError("exit management peak must be a positive Decimal")
        if type(expected_holding_version) is not int or expected_holding_version < 0:
            raise ValueError("expected holding version must be non-negative")
        selected = policy_payload.get("decision")
        if (
            policy_payload.get("kind") != "fixed_exit_policy_state_v1"
            or not isinstance(selected, Mapping)
            or effective_action_payload.get("kind") != "exit_holding_management_state_v1"
            or guard_payload.get("expected_holding_version") != expected_holding_version
            or any(
                selected.get(name) is not value or effective_action_payload.get(name) is not value
                for name, value in (
                    ("early_winner_hold", early_winner_hold),
                    ("breakeven_armed", breakeven_armed),
                    ("ema_trailing_active", ema_trailing_active),
                )
            )
            or selected.get("scale_out_tier") != policy_scale_out_tier
            or effective_action_payload.get("selected_scale_out_tier") != policy_scale_out_tier
        ):
            raise ValueError("exit management flags differ from the fixed policy decision")
        if observed_at < decision.clock.as_of_cutoff_at:
            raise ValueError("exit management observation precedes the decision cutoff")
        with self._transaction(write=True) as conn:
            self._require_ready(conn)
            row = self._holding_row(conn, _required_text(holding_episode_id, "holding_episode_id"))
            if row is None:
                raise KeyError("exit management holding was not found")
            holding = _holding_from_row(row)
            if holding.deployment_generation_id != decision.deployment_generation_id:
                raise ValueError("exit management decision differs from the holding generation")
            self._record_decision_in_transaction(
                conn, decision,
                policy_payload=policy_payload,
                guard_payload=guard_payload,
                effective_action_payload=effective_action_payload,
            )
            flags = dict(holding.policy_flags)
            if flags.get("exit_management_decision_id") == decision.decision_id:
                return holding
            if flags.get("exit_management_session") == decision.clock.decision_session.isoformat():
                raise DecisionConflictError("a different exit management decision already owns this session")
            if int(row["state_version"]) != expected_holding_version:
                raise ConcurrentStateUpdateError("holding changed since exit management was prepared")
            if holding.pending_action_ids or holding.remaining_quantity <= 0:
                raise ValueError("exit management requires a non-flat holding without pending actions")
            if (
                bool(flags.get("position_reconciliation_required"))
                or holding.proposed_stop_action_id != holding.confirmed_stop_action_id
                or not holding.confirmed_stop_client_order_id
                or not holding.confirmed_stop_broker_order_id
            ):
                raise ValueError("exit management requires a reconciled confirmed protective stop")
            if holding.last_accepted_session is not None and decision.clock.decision_session < holding.last_accepted_session:
                raise ValueError("exit management cannot use an earlier session")
            if holding.valuation_at is not None and observed_at < holding.valuation_at:
                raise ValueError("exit management valuation cannot move backward")
            if holding.peak_price is not None and peak_price < holding.peak_price:
                raise ValueError("exit management peak cannot decrease")
            flags.update({
                "exit_management_decision_id": decision.decision_id,
                "exit_management_session": decision.clock.decision_session.isoformat(),
                "early_winner_hold": str(early_winner_hold).lower(),
                "breakeven_armed": str(breakeven_armed).lower(),
                "ema_trailing_active": str(ema_trailing_active).lower(),
                "exit_policy_scale_out_tier": str(policy_scale_out_tier),
            })
            updated = replace(
                holding,
                policy_flags=tuple(flags.items()),
                peak_price=peak_price,
                valuation_at=observed_at,
                last_accepted_session=decision.clock.decision_session,
            )
            next_version = self._save_holding(
                conn, updated,
                expected_version=expected_holding_version,
                event_kind="exit_management_state_recorded",
                evidence_ref=decision.decision_id,
                observed_at=observed_at,
            )
            return replace(updated, state_version=next_version)

    def _append_action_history(
        self,
        conn: sqlite3.Connection,
        intent: ActionIntent,
        *,
        version: int,
        event_kind: str,
        observed_at: datetime,
    ) -> None:
        provider_references = [
            {
                "provider_id": item.provider_id,
                "paper_account_environment_id": item.paper_account_environment_id,
                "store_identity": item.store_identity,
                "reference_kind": item.reference_kind,
                "external_order_id": item.external_order_id,
                "attempt_number": item.attempt_number,
                "source_payload_sha256": item.source_payload_sha256,
                "first_seen_at_utc": _aware_iso(item.first_seen_at_utc),
            }
            for item in _provider_order_references(conn, intent.logical_action_id)
        ]
        state_json = _canonical_json(
            {
                **_action_payload(intent),
                "provider_order_references": provider_references,
                "state_version": version,
            }
        )
        state_sha = _sha256_text(state_json)
        event_id = "action-event:sha256:" + _sha256_text(
            _canonical_json(
                {
                    "logical_action_id": intent.logical_action_id,
                    "state_version": version,
                    "event_kind": event_kind,
                    "state_sha256": state_sha,
                }
            )
        )
        conn.execute(
            """INSERT INTO policy_state_action_history(
                logical_action_id, state_version, event_id, event_kind, state_sha256, state_json, observed_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (intent.logical_action_id, version, event_id, event_kind, state_sha, state_json, _aware_iso(observed_at)),
        )

    @staticmethod
    def _action_dynamic_values(intent: ActionIntent) -> tuple[object, ...]:
        projection = project_action_state(intent)
        return (
            str(intent.confirmed_filled_quantity),
            str(intent.residual_quantity),
            _decimal_text(intent.reservation_price),
            intent.reservation_price_basis,
            _decimal_text(intent.reservation_stop_price),
            _decimal_text(projection.reservation_amount),
            _decimal_text(intent.risk_per_unit),
            intent.risk_basis,
            _decimal_text(projection.residual_committed_risk),
            intent.status.value,
            intent.resolution_reason,
            _canonical_json(intent.immutable_payload()),
        )

    def _store_action(
        self,
        conn: sqlite3.Connection,
        intent: ActionIntent,
        *,
        version: int,
        event_kind: str,
        observed_at: datetime | None = None,
    ) -> None:
        account = intent.decision.deployment_identity.paper_account_environment_id
        self._require_deployment(conn, intent.deployment_generation_id, account)
        row = conn.execute(
            "SELECT * FROM policy_state_actions WHERE logical_action_id=?",
            (intent.logical_action_id,),
        ).fetchone()
        dynamic = self._action_dynamic_values(intent)
        if row is None:
            if version != 0:
                raise ConcurrentStateUpdateError("new action must start at version zero")
            columns = (
                "logical_action_id, deployment_generation_id, paper_account_environment_id, store_identity, decision_id, "
                "holding_episode_id, security_id, broker_symbol, role, side, requested_quantity, confirmed_quantity, "
                "residual_quantity, reservation_price, reservation_price_basis, reservation_stop_price, "
                "cash_reservation_amount, risk_per_unit, risk_basis, residual_committed_risk, exit_tier, "
                "snapshot_original_quantity, fraction_of_original_quantity, rounding_rule_id, status, resolution_reason, "
                "immutable_payload_json, state_version"
            )
            fixed = (
                intent.logical_action_id,
                intent.deployment_generation_id,
                account,
                self.store_identity,
                intent.decision.decision_id,
                intent.holding_episode_id,
                intent.security_id,
                intent.broker_symbol,
                intent.role.value,
                intent.side.value,
                str(intent.requested_quantity),
            )
            values = fixed + dynamic[:9] + (
                intent.exit_tier,
                _decimal_text(intent.snapshot_original_quantity),
                _decimal_text(intent.fraction_of_original_quantity),
                intent.rounding_rule_id,
            ) + dynamic[9:] + (
                0,
            )
            # dynamic includes immutable JSON; this sequence follows the schema column order.
            conn.execute(
                f"INSERT INTO policy_state_actions({columns}) VALUES ({','.join('?' for _ in values)})",
                values,
            )
        else:
            immutable = (
                row["deployment_generation_id"],
                row["paper_account_environment_id"],
                row["store_identity"],
                row["decision_id"],
                row["security_id"],
                row["broker_symbol"],
                row["role"],
                row["side"],
                row["requested_quantity"],
                row["exit_tier"],
                row["snapshot_original_quantity"],
                row["fraction_of_original_quantity"],
                row["rounding_rule_id"],
            )
            expected_immutable = (
                intent.deployment_generation_id,
                account,
                self.store_identity,
                intent.decision.decision_id,
                intent.security_id,
                intent.broker_symbol,
                intent.role.value,
                intent.side.value,
                str(intent.requested_quantity),
                intent.exit_tier,
                _decimal_text(intent.snapshot_original_quantity),
                _decimal_text(intent.fraction_of_original_quantity),
                intent.rounding_rule_id,
            )
            if immutable != expected_immutable:
                raise IdentityConflictError("logical action immutable identity or target conflicts")
            current_version = int(row["state_version"])
            if current_version + 1 != version:
                raise ConcurrentStateUpdateError("action state version is stale")
            conn.execute(
                """UPDATE policy_state_actions SET holding_episode_id=?, confirmed_quantity=?, residual_quantity=?,
                    reservation_price=?, reservation_price_basis=?, reservation_stop_price=?, cash_reservation_amount=?,
                    risk_per_unit=?, risk_basis=?, residual_committed_risk=?, status=?, resolution_reason=?,
                    immutable_payload_json=?, state_version=? WHERE logical_action_id=? AND state_version=?""",
                (
                    intent.holding_episode_id,
                    *dynamic,
                    version,
                    intent.logical_action_id,
                    current_version,
                ),
            )

        existing_attempts = {
            int(attempt["attempt_number"]): attempt
            for attempt in conn.execute(
                "SELECT * FROM policy_state_order_attempts WHERE logical_action_id=?",
                (intent.logical_action_id,),
            )
        }
        for attempt in intent.order_attempts:
            old = existing_attempts.get(attempt.attempt_number)
            if old is None:
                conn.execute(
                    """INSERT INTO policy_state_order_attempts(
                        logical_action_id, paper_account_environment_id, store_identity, attempt_number,
                        requested_quantity, confirmed_quantity, cumulative_notional, cumulative_fees, status,
                        terminal_status, client_order_id, broker_order_id, state_version
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?)""",
                    (
                        intent.logical_action_id,
                        account,
                        self.store_identity,
                        attempt.attempt_number,
                        str(attempt.requested_quantity),
                        str(attempt.confirmed_filled_quantity),
                        attempt.status.value,
                        None if attempt.terminal_status is None else attempt.terminal_status.value,
                        attempt.client_order_id,
                        attempt.broker_order_id,
                        version,
                    ),
                )
            else:
                if Decimal(old["requested_quantity"]) != attempt.requested_quantity:
                    raise IdentityConflictError("order attempt quantity cannot change")
                conn.execute(
                    """UPDATE policy_state_order_attempts SET confirmed_quantity=?, status=?, terminal_status=?, client_order_id=?,
                        broker_order_id=?, state_version=? WHERE logical_action_id=? AND attempt_number=?""",
                    (
                        str(attempt.confirmed_filled_quantity),
                        attempt.status.value,
                        None if attempt.terminal_status is None else attempt.terminal_status.value,
                        attempt.client_order_id,
                        attempt.broker_order_id,
                        version,
                        intent.logical_action_id,
                        attempt.attempt_number,
                    ),
                )
        self._append_action_history(
            conn,
            intent,
            version=version,
            event_kind=event_kind,
            observed_at=observed_at or datetime.now().astimezone(),
        )

    def record_action_intent(
        self,
        intent: ActionIntent,
        *,
        expected_version: int | None,
        expected_holding_version: int | None = None,
    ) -> int:
        with self._transaction(write=True) as conn:
            self._require_ready(conn)
            self._validate_deployment_scope(intent.decision.deployment_identity)
            if (
                intent.role is ActionRole.REPLACEMENT
                and intent.holding_episode_id is not None
                and intent.holding_episode_id != _replacement_holding_id(intent)
            ):
                raise ValueError("replacement holding episode must be its own opening holding")
            existing = conn.execute(
                "SELECT * FROM policy_state_actions WHERE logical_action_id=?",
                (intent.logical_action_id,),
            ).fetchone()
            if existing is not None:
                current = _action_from_row(conn, existing)
                if current.immutable_payload() != intent.immutable_payload():
                    raise IdentityConflictError("logical action identity already has different immutable intent facts")
                if expected_version is None or int(existing["state_version"]) != expected_version:
                    raise ConcurrentStateUpdateError("action already exists at a different version")
                return int(existing["state_version"])
            if expected_version is not None:
                raise ConcurrentStateUpdateError("action does not exist at the expected version")
            pending_holding: tuple[HoldingEpisode, int] | None = None
            if intent.holding_episode_id is not None:
                holding_row = self._holding_row(conn, intent.holding_episode_id)
                if holding_row is None:
                    raise ValueError("action holding episode is not recorded")
                if expected_holding_version is not None and int(holding_row["state_version"]) != expected_holding_version:
                    raise ConcurrentStateUpdateError("holding changed since the caller read it")
                holding = _holding_from_row(holding_row)
                _validate_replacement_holding(intent, holding)
                updated_holding = register_pending_action(holding, intent)
                pending_holding = (updated_holding, int(holding_row["state_version"]))
            self._store_action(conn, intent, version=0, event_kind="action_intended")
            if pending_holding is not None:
                holding, current_version = pending_holding
                self._save_holding(
                    conn,
                    holding,
                    expected_version=current_version,
                    event_kind="action_intended",
                    logical_action_id=intent.logical_action_id,
                )
            return 0

    def _load_action(self, conn: sqlite3.Connection, logical_action_id: str) -> tuple[ActionIntent, sqlite3.Row]:
        row = conn.execute(
            "SELECT * FROM policy_state_actions WHERE logical_action_id=?",
            (_required_text(logical_action_id, "logical_action_id"),),
        ).fetchone()
        if row is None:
            raise KeyError("logical action was not found")
        return _action_from_row(conn, row), row

    def load_action_intent(self, logical_action_id: str) -> ActionIntent:
        """Load the durable action with its complete decision identity."""
        with self._transaction(write=False) as conn:
            self._require_ready(conn)
            intent, _ = self._load_action(conn, logical_action_id)
            return intent

    def load_action_projection(self, logical_action_id: str) -> ActionStateProjection:
        with self._transaction(write=False) as conn:
            self._require_ready(conn)
            intent, row = self._load_action(conn, logical_action_id)
            return _action_projection_from_row(conn, intent, row)

    def _load_protective_order_context_in_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        stop_row: sqlite3.Row,
        provider_id: str,
        client_order_id: str,
        broker_order_id: str,
    ) -> tuple[DecisionIdentity, dict[str, object], sqlite3.Row]:
        """Resolve one confirmed physical STOP order to its frozen source facts."""
        if stop_row["status"] != "confirmed":
            raise ValueError("protective SELL fill has no confirmed durable STOP order")
        if (
            stop_row["client_order_id"] != client_order_id
            or stop_row["broker_order_id"] != broker_order_id
        ):
            raise ValueError("protective SELL fill order references differ from the confirmed STOP")
        holding_row = self._holding_row(conn, str(stop_row["holding_episode_id"]))
        if holding_row is None:
            raise ValueError("protective STOP references a missing holding episode")
        if (
            holding_row["deployment_generation_id"] != stop_row["deployment_generation_id"]
            or holding_row["paper_account_environment_id"]
            != stop_row["paper_account_environment_id"]
            or holding_row["store_identity"] != self.store_identity
            or holding_row["security_id"] != stop_row["security_id"]
        ):
            raise ValueError("protective STOP and holding generation/account identities disagree")

        rows = conn.execute(
            """SELECT decision_id, decision_json, policy_payload_json, guard_payload_json,
                      effective_action_payload_json
               FROM policy_state_decisions
               WHERE deployment_generation_id=? AND paper_account_environment_id=?
                 AND store_identity=? AND decision_category=? AND subject_type=?""",
            (
                stop_row["deployment_generation_id"],
                stop_row["paper_account_environment_id"],
                self.store_identity,
                DecisionCategory.EXIT.value,
                DecisionSubjectType.HOLDING.value,
            ),
        ).fetchall()
        stop_action_id = str(stop_row["stop_update_action_id"])
        candidates: list[tuple[DecisionIdentity, dict[str, object]]] = []
        for row in rows:
            try:
                policy_payload = json.loads(row["policy_payload_json"])
                guard_payload = json.loads(row["guard_payload_json"])
                effective_payload = json.loads(row["effective_action_payload_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raw_payloads = " ".join(
                    str(row[key] or "")
                    for key in (
                        "policy_payload_json",
                        "guard_payload_json",
                        "effective_action_payload_json",
                    )
                )
                if (
                    "protective_stop" in raw_payloads
                    or "protective_stop_sell" in raw_payloads
                    or "execution-derived:protective-stop:" in str(row["decision_json"] or "")
                ):
                    raise ValueError("protective execution provenance JSON is malformed") from exc
                continue
            source_stop_id = (
                policy_payload.get("source_stop_update_action_id")
                if isinstance(policy_payload, dict)
                else None
            )
            if source_stop_id != stop_action_id:
                origin_shaped = (
                    isinstance(policy_payload, dict)
                    and policy_payload.get("execution_record_kind")
                    == _PROTECTIVE_EXECUTION_RECORD_KIND
                ) or (
                    isinstance(guard_payload, dict)
                    and guard_payload.get("origin") == "protective_stop"
                ) or (
                    isinstance(effective_payload, dict)
                    and effective_payload.get("execution_kind") == "protective_stop_sell"
                )
                if origin_shaped and (
                    not isinstance(policy_payload, dict)
                    or policy_payload.get("source_stop_update_action_id") is None
                ):
                    raise ValueError("protective execution decision has no stop-update origin")
                continue
            if (
                not isinstance(policy_payload, dict)
                or not isinstance(guard_payload, dict)
                or not isinstance(effective_payload, dict)
                or policy_payload.get("execution_record_kind")
                != _PROTECTIVE_EXECUTION_RECORD_KIND
                or policy_payload.get("policy_authored") is not False
                or guard_payload.get("origin") != "protective_stop"
                or guard_payload.get("execution_derived") is not True
                or effective_payload.get("execution_kind") != "protective_stop_sell"
            ):
                raise ValueError("protective execution origin facts are malformed")
            physical_facts = effective_payload.get("physical_order")
            if not isinstance(physical_facts, dict):
                raise ValueError("protective execution decision has no physical-order facts")
            if policy_payload.get("physical_order") != physical_facts or guard_payload.get(
                "physical_order"
            ) != physical_facts:
                raise ValueError("protective execution decision physical-order facts conflict")
            if (
                physical_facts.get("provider_id") != provider_id
                or physical_facts.get("client_order_id") != client_order_id
                or physical_facts.get("broker_order_id") != broker_order_id
                or physical_facts.get("source_stop_update_action_id") != stop_action_id
            ):
                raise ValueError("protective execution facts do not authenticate this STOP order")
            decision = _decision_from_payload(json.loads(row["decision_json"]))
            if decision.decision_id != row["decision_id"]:
                raise ValueError("protective execution decision identity is inconsistent")
            target_text = physical_facts.get("requested_quantity")
            price_text = physical_facts.get("confirmed_stop_price")
            try:
                target = Decimal(str(target_text))
                confirmed_price = Decimal(str(price_text))
            except Exception as exc:  # noqa: BLE001
                raise ValueError("protective physical-order target is malformed") from exc
            expected_decision, expected_policy, expected_guard, expected_effective = (
                self._protective_order_decision_facts(
                    conn,
                    stop_row=stop_row,
                    holding_row=holding_row,
                    provider_id=provider_id,
                    client_order_id=client_order_id,
                    broker_order_id=broker_order_id,
                    requested_quantity=target,
                    confirmed_stop_price=confirmed_price,
                )
            )
            if (
                decision != expected_decision
                or policy_payload != expected_policy
                or guard_payload != expected_guard
                or effective_payload != expected_effective
                or physical_facts.get("paper_account_environment_id")
                != stop_row["paper_account_environment_id"]
                or physical_facts.get("store_identity") != self.store_identity
                or physical_facts.get("deployment_generation_id")
                != stop_row["deployment_generation_id"]
                or physical_facts.get("security_id") != stop_row["security_id"]
                or physical_facts.get("symbol") != holding_row["broker_symbol"]
                or physical_facts.get("holding_episode_id")
                != stop_row["holding_episode_id"]
                or physical_facts.get("side") != OrderSide.SELL.value
                or not target.is_finite()
                or target <= 0
                or not confirmed_price.is_finite()
                or confirmed_price <= 0
                or confirmed_price != Decimal(stop_row["confirmed_stop_price"])
            ):
                raise ValueError("protective execution decision disagrees with its durable STOP or holding")
            candidates.append((decision, physical_facts))
        if not candidates:
            raise ValueError("protective STOP order target is missing; reconciliation is required")
        if len(candidates) > 1:
            raise ValueError("protective STOP order target is ambiguous; reconciliation is required")
        return candidates[0][0], candidates[0][1], holding_row

    def _is_confirmed_protective_execution_projection(
        self,
        conn: sqlite3.Connection,
        *,
        intent: ActionIntent,
        action_row: sqlite3.Row,
        holding_by_id: Mapping[str, HoldingEpisode],
    ) -> bool:
        """Authenticate a mechanical STOP leg before omitting its action-owner projection."""
        decision_row = conn.execute(
            """SELECT decision_json, policy_payload_json, guard_payload_json,
                      effective_action_payload_json
               FROM policy_state_decisions
               WHERE decision_id=? AND deployment_generation_id=?
                 AND paper_account_environment_id=? AND store_identity=?""",
            (
                intent.decision.decision_id,
                intent.deployment_generation_id,
                intent.decision.deployment_identity.paper_account_environment_id,
                self.store_identity,
            ),
        ).fetchone()
        if decision_row is None:
            raise ValueError("action decision identity is missing during protective projection")
        try:
            policy_payload = json.loads(decision_row["policy_payload_json"])
            guard_payload = json.loads(decision_row["guard_payload_json"])
            effective_payload = json.loads(decision_row["effective_action_payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            if intent.decision.subject_id.startswith("execution-derived:protective-stop:"):
                raise ValueError("protective execution decision JSON is malformed") from exc
            return False
        origin_marked = (
            intent.decision.subject_id.startswith("execution-derived:protective-stop:")
            or (
                isinstance(policy_payload, dict)
                and "protective" in str(policy_payload.get("execution_record_kind", ""))
            )
            or (
                isinstance(guard_payload, dict)
                and guard_payload.get("origin") == "protective_stop"
            )
            or (
                isinstance(effective_payload, dict)
                and "protective" in str(effective_payload.get("execution_kind", ""))
            )
        )
        if not origin_marked:
            # Genuine policy-authored CLOSE actions remain account-visible. Mechanical
            # STOP provenance is required only when an execution-derived origin marker
            # identifies the action as a protective leg.
            return False
        if (
            not isinstance(policy_payload, dict)
            or not isinstance(guard_payload, dict)
            or not isinstance(effective_payload, dict)
            or policy_payload.get("execution_record_kind")
            != _PROTECTIVE_EXECUTION_RECORD_KIND
            or policy_payload.get("policy_authored") is not False
            or guard_payload.get("origin") != "protective_stop"
            or guard_payload.get("execution_derived") is not True
            or effective_payload.get("execution_kind") != "protective_stop_sell"
            or effective_payload.get("role") != ActionRole.CLOSE.value
            or effective_payload.get("side") != OrderSide.SELL.value
        ):
            raise ValueError("execution-derived protective action origin is malformed")
        facts = effective_payload.get("physical_order")
        if not isinstance(facts, dict) or policy_payload.get("physical_order") != facts or guard_payload.get(
            "physical_order"
        ) != facts:
            raise ValueError("execution-derived protective action has conflicting immutable order facts")
        stop_action_id = _required_text(
            facts.get("source_stop_update_action_id"), "source_stop_update_action_id"
        )
        stop_row = conn.execute(
            """SELECT * FROM policy_state_stop_updates
               WHERE stop_update_action_id=? AND deployment_generation_id=?
                 AND paper_account_environment_id=? AND store_identity=?""",
            (
                stop_action_id,
                intent.deployment_generation_id,
                intent.decision.deployment_identity.paper_account_environment_id,
                self.store_identity,
            ),
        ).fetchone()
        if stop_row is None:
            raise ValueError("execution-derived protective action refers to a missing STOP update")
        holding = holding_by_id.get(str(intent.holding_episode_id or ""))
        if holding is None:
            raise ValueError("execution-derived protective action refers to a missing holding")
        decision, expected_facts, holding_row = self._load_protective_order_context_in_transaction(
            conn,
            stop_row=stop_row,
            provider_id=str(facts.get("provider_id", "")),
            client_order_id=str(stop_row["client_order_id"] or ""),
            broker_order_id=str(stop_row["broker_order_id"] or ""),
        )
        if (
            decision != intent.decision
            or facts != expected_facts
            or str(holding_row["holding_episode_id"]) != holding.holding_episode_id
            or str(action_row["holding_episode_id"] or "") != holding.holding_episode_id
            or str(action_row["deployment_generation_id"]) != holding.deployment_generation_id
            or str(action_row["paper_account_environment_id"])
            != facts["paper_account_environment_id"]
            or str(action_row["store_identity"]) != self.store_identity
            or intent.role is not ActionRole.CLOSE
            or intent.side is not OrderSide.SELL
            or intent.requested_quantity != Decimal(str(facts["requested_quantity"]))
            or str(facts["security_id"]) != holding.security_id
            or str(facts["symbol"]) != holding.broker_symbol
            or str(facts["holding_episode_id"]) != holding.holding_episode_id
            or str(facts["deployment_generation_id"]) != holding.deployment_generation_id
        ):
            raise ValueError("execution-derived protective action disagrees with its generation, holding, or order")
        references = _provider_order_references(conn, intent.logical_action_id)
        attempt = intent.order_attempts[0] if len(intent.order_attempts) == 1 else None
        if (
            attempt is None
            or attempt.attempt_number != 1
            or attempt.requested_quantity != intent.requested_quantity
            or attempt.client_order_id != facts["client_order_id"]
            or attempt.broker_order_id != facts["broker_order_id"]
            or not any(
                item.provider_id == facts["provider_id"]
                and item.paper_account_environment_id == facts["paper_account_environment_id"]
                and item.store_identity == facts["store_identity"]
                and item.reference_kind == "client_order_id"
                and item.external_order_id == facts["client_order_id"]
                and item.attempt_number == 1
                for item in references
            )
            or not any(
                item.provider_id == facts["provider_id"]
                and item.paper_account_environment_id == facts["paper_account_environment_id"]
                and item.store_identity == facts["store_identity"]
                and item.reference_kind == "broker_order_id"
                and item.external_order_id == facts["broker_order_id"]
                and item.attempt_number == 1
                for item in references
            )
        ):
            raise ValueError("execution-derived protective action order aliases are incomplete or inconsistent")
        if (
            intent.confirmed_filled_quantity <= 0
            or dict(holding.applied_action_fill_watermarks).get(intent.logical_action_id)
            != intent.confirmed_filled_quantity
        ):
            raise ValueError("protective execution action and holding fill watermark disagree")
        if (
            dict(holding.policy_flags).get("position_reconciliation_required")
            or intent.confirmed_filled_quantity > intent.requested_quantity
            or intent.status is ActionStatus.RECONCILIATION_REQUIRED
            or attempt.status is ActionAttemptStatus.RECONCILIATION_REQUIRED
            or intent.status
            not in {
                ActionStatus.SUBMITTED,
                ActionStatus.PARTIALLY_FILLED,
                ActionStatus.FILLED,
            }
            or attempt.status
            not in {
                ActionAttemptStatus.SUBMITTED,
                ActionAttemptStatus.PARTIALLY_FILLED,
                ActionAttemptStatus.FILLED,
            }
        ):
            return False
        if holding.confirmed_stop_action_id is None:
            return False
        owning_stop = conn.execute(
            """SELECT * FROM policy_state_stop_updates
               WHERE stop_update_action_id=? AND deployment_generation_id=?
                 AND paper_account_environment_id=? AND store_identity=?""",
            (
                holding.confirmed_stop_action_id,
                holding.deployment_generation_id,
                facts["paper_account_environment_id"],
                self.store_identity,
            ),
        ).fetchone()
        if owning_stop is None or owning_stop["status"] != "confirmed":
            raise ValueError("holding protective-order owner is not a confirmed STOP update")
        if (
            owning_stop["holding_episode_id"] != holding.holding_episode_id
            or owning_stop["security_id"] != holding.security_id
            or owning_stop["broker_order_id"] != holding.confirmed_stop_broker_order_id
            or owning_stop["client_order_id"] != holding.confirmed_stop_client_order_id
        ):
            raise ValueError("holding STOP owner references disagree with its confirmed update")
        # The broker may keep one physical STOP while policy records a later update.
        # Filter its execution leg only while that exact broker order still owns this
        # holding; a superseded fill remains visible for reconciliation.
        return (
            owning_stop["broker_order_id"] == facts["broker_order_id"]
            and holding.confirmed_stop_broker_order_id == facts["broker_order_id"]
        )

    def record_unresolved_execution_sell_fact(
        self,
        *,
        source_action_id: str,
        provider_id: str,
        workflow_id: str,
        broker_order_id: str,
        client_order_id: str,
        order_type: str,
        cumulative_quantity: Decimal,
        average_fill_price: Decimal,
        unresolved_cause: str,
        fill_event_id: str,
        observed_at: datetime,
        expected_holding_version: int,
    ) -> HoldingEpisode:
        """Retain a real SELL checkpoint when its physical guard association is incomplete."""
        provider = _required_text(provider_id, "provider_id")
        source_id = _required_text(source_action_id, "source_action_id")
        workflow = _required_text(workflow_id, "workflow_id")
        broker = _required_text(broker_order_id, "broker_order_id")
        client = _required_text(client_order_id, "client_order_id")
        normalized_type = _required_text(order_type, "order_type").lower()
        cause = _required_text(unresolved_cause, "unresolved_cause")
        event_id = _required_text(fill_event_id, "fill_event_id")
        if normalized_type in {"stop", "stop_limit"}:
            raise ValueError("protective STOP fills must use the authenticated STOP seam")
        if (
            not isinstance(cumulative_quantity, Decimal)
            or not cumulative_quantity.is_finite()
            or cumulative_quantity <= 0
            or not isinstance(average_fill_price, Decimal)
            or not average_fill_price.is_finite()
            or average_fill_price <= 0
        ):
            raise ValueError("unresolved SELL fill facts must contain positive finite quantity and price")
        if not isinstance(expected_holding_version, int) or isinstance(
            expected_holding_version, bool
        ) or expected_holding_version < 0:
            raise ValueError("expected_holding_version must be a non-negative integer")
        _aware_iso(observed_at)

        with self._transaction(write=True) as conn:
            self._require_ready(conn)
            source_action, _ = self._load_action(conn, source_id)
            if source_action.side is not OrderSide.BUY or source_action.holding_episode_id is None:
                raise ValueError("unresolved SELL source is not a BUY action with a durable holding")
            holding_row = self._holding_row(conn, source_action.holding_episode_id)
            if holding_row is None:
                raise ValueError("unresolved SELL has no durable holding target; reconciliation is required")
            holding = _holding_from_row(holding_row)
            if (
                holding.opening_action_id != source_id
                or holding.deployment_generation_id != source_action.deployment_generation_id
                or holding.security_id != source_action.security_id
                or holding.broker_symbol != source_action.broker_symbol
                or holding.state_version is None
            ):
                raise ValueError("unresolved SELL source and holding provenance disagree")

            fact = {
                "provider_id": provider,
                "store_identity": self.store_identity,
                "deployment_generation_id": source_action.deployment_generation_id,
                "source_action_id": source_id,
                "source_decision_id": source_action.decision.decision_id,
                "workflow_id": workflow,
                "holding_episode_id": holding.holding_episode_id,
                "security_id": holding.security_id,
                "symbol": holding.broker_symbol,
                "side": OrderSide.SELL.value,
                "order_type": normalized_type,
                "broker_order_id": broker,
                "client_order_id": client,
                "cumulative_quantity": str(cumulative_quantity),
                "average_fill_price": str(average_fill_price),
                "cause": cause,
                "fill_event_id": event_id,
                "observed_at": _aware_iso(observed_at),
            }
            digest = hashlib.sha256(_canonical_json(fact).encode("utf-8")).hexdigest()
            flag_key = "unresolved_execution_sell_" + hashlib.sha256(
                broker.encode("utf-8")
            ).hexdigest()[:24]
            flags = dict(holding.policy_flags)
            prior_json = flags.get(flag_key)
            if prior_json:
                try:
                    prior = json.loads(prior_json)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError("unresolved SELL execution evidence is malformed") from exc
                if not isinstance(prior, dict):
                    raise ValueError("unresolved SELL execution evidence is malformed")
                if prior.get("broker_order_id") != broker or prior.get("client_order_id") != client:
                    raise IdentityConflictError("unresolved SELL physical order identity changed")
                prior_quantity = Decimal(str(prior.get("cumulative_quantity", "0")))
                if cumulative_quantity < prior_quantity:
                    return holding
                if cumulative_quantity == prior_quantity:
                    if prior.get("fact_sha256") != digest:
                        raise FillReceiptConflictError(
                            "unresolved SELL replay changed immutable physical execution facts"
                        )
                    return holding
            if int(holding_row["state_version"]) != expected_holding_version:
                raise ConcurrentStateUpdateError("holding changed since unresolved SELL was observed")
            fact["fact_sha256"] = digest
            flags[flag_key] = _canonical_json(fact)
            prior_cause = flags.get("position_reconciliation_required", "")
            flags["position_reconciliation_required"] = (
                cause
                if not prior_cause or cause in prior_cause
                else f"{prior_cause}; {cause}"
            )
            updated = replace(
                holding,
                policy_flags=tuple(flags.items()),
                committed_risk=None,
                committed_risk_basis=None,
                realized_pnl=None,
            )
            self._save_holding(
                conn,
                updated,
                expected_version=expected_holding_version,
                event_kind="execution_sell_requires_reconciliation",
                logical_action_id=source_id,
                observed_at=observed_at,
                evidence_ref=event_id,
            )
            return _holding_from_row(self._holding_row(conn, holding.holding_episode_id))

    def record_execution_guard_sell_fill(
        self,
        *,
        source_action_id: str,
        provider_id: str,
        workflow_id: str,
        broker_order_id: str,
        client_order_id: str,
        order_type: str,
        execution_cause: str,
        requested_quantity: Decimal,
        fill_event_id: str,
        cumulative_quantity: Decimal,
        cumulative_notional: Decimal | None,
        cumulative_fees: Decimal | None,
        observed_at: datetime,
        expected_holding_version: int,
    ) -> ActionStateProjection:
        """Apply an authenticated execution-guard SELL on the source BUY holding."""
        provider = _required_text(provider_id, "provider_id")
        source_id = _required_text(source_action_id, "source_action_id")
        workflow = _required_text(workflow_id, "workflow_id")
        broker = _required_text(broker_order_id, "broker_order_id")
        client = _required_text(client_order_id, "client_order_id")
        normalized_type = _required_text(order_type, "order_type").lower()
        cause = _required_text(execution_cause, "execution_cause")
        event_id = _required_text(fill_event_id, "fill_event_id")
        if normalized_type in {"stop", "stop_limit"}:
            raise ValueError("protective STOP fills must use the authenticated STOP seam")
        if not isinstance(requested_quantity, Decimal) or not requested_quantity.is_finite() or requested_quantity <= 0:
            raise ValueError("requested_quantity must be a positive finite Decimal")
        if not isinstance(cumulative_quantity, Decimal) or not cumulative_quantity.is_finite() or cumulative_quantity <= 0:
            raise ValueError("cumulative_quantity must be a positive finite Decimal")
        for value, name in ((cumulative_notional, "cumulative_notional"), (cumulative_fees, "cumulative_fees")):
            if value is not None and (not isinstance(value, Decimal) or not value.is_finite() or value < 0):
                raise ValueError(f"{name} must be a finite non-negative Decimal when known")
        if not isinstance(expected_holding_version, int) or isinstance(expected_holding_version, bool) or expected_holding_version < 0:
            raise ValueError("expected_holding_version must be a non-negative integer")
        _aware_iso(observed_at)

        with self._transaction(write=True) as conn:
            self._require_ready(conn)
            source_action, _ = self._load_action(conn, source_id)
            if source_action.side is not OrderSide.BUY or source_action.holding_episode_id is None:
                raise ValueError("guard SELL source is not a BUY action with a durable holding")
            holding_row = self._holding_row(conn, source_action.holding_episode_id)
            if holding_row is None:
                raise ValueError("guard SELL has no durable holding target; reconciliation is required")
            holding = _holding_from_row(holding_row)
            if (
                holding.opening_action_id != source_id
                or holding.deployment_generation_id != source_action.deployment_generation_id
                or holding.security_id != source_action.security_id
                or holding.broker_symbol != source_action.broker_symbol
                or holding.state_version is None
            ):
                raise ValueError("guard SELL source and holding provenance disagree")

            facts: dict[str, object] = {
                "origin": "legacy_execution_guard",
                "provider_id": provider,
                "paper_account_environment_id": source_action.decision.deployment_identity.paper_account_environment_id,
                "store_identity": self.store_identity,
                "deployment_generation_id": source_action.deployment_generation_id,
                "security_id": holding.security_id,
                "symbol": holding.broker_symbol,
                "holding_episode_id": holding.holding_episode_id,
                "source_policy_action_id": source_action.logical_action_id,
                "source_policy_decision_id": source_action.decision.decision_id,
                "workflow_id": workflow,
                "side": OrderSide.SELL.value,
                "order_type": normalized_type,
                "execution_cause": cause,
                "requested_quantity": str(requested_quantity),
                "client_order_id": client,
                "broker_order_id": broker,
            }
            decision = DecisionIdentity.build(
                deployment=source_action.decision.deployment_identity,
                clock=source_action.decision.clock,
                snapshot_sha256=source_action.decision.snapshot_sha256,
                category=DecisionCategory.EXIT,
                subject_type=DecisionSubjectType.HOLDING,
                subject_id="execution-derived:guard-sell:" + _canonical_json(facts),
            )
            policy_payload: dict[str, object] = {
                "execution_record_kind": _GUARD_SELL_EXECUTION_RECORD_KIND,
                "policy_authored": False,
                "source_policy_action_id": source_action.logical_action_id,
                "source_policy_decision_id": source_action.decision.decision_id,
                "physical_order": facts,
            }
            guard_payload: dict[str, object] = {
                "origin": "legacy_execution_guard",
                "execution_derived": True,
                "physical_order": facts,
            }
            effective_payload: dict[str, object] = {
                "execution_kind": "guard_sell_execution",
                "role": ActionRole.CLOSE.value,
                "side": OrderSide.SELL.value,
                "holding_episode_id": holding.holding_episode_id,
                "physical_order": facts,
            }
            self._record_decision_in_transaction(
                conn,
                decision,
                policy_payload=policy_payload,
                guard_payload=guard_payload,
                effective_action_payload=effective_payload,
            )
            action = build_action_intent(
                decision=decision,
                security_id=holding.security_id,
                broker_symbol=holding.broker_symbol,
                role=ActionRole.CLOSE,
                side=OrderSide.SELL,
                requested_quantity=requested_quantity,
                holding_episode_id=holding.holding_episode_id,
                status=ActionStatus.SUBMITTED,
            )
            attempt = ActionOrderAttempt(
                attempt_number=1,
                requested_quantity=requested_quantity,
                status=ActionAttemptStatus.SUBMITTED,
                client_order_id=client,
                broker_order_id=broker,
            )
            action = replace(action, order_attempts=(attempt,))
            action_row = conn.execute(
                "SELECT * FROM policy_state_actions WHERE logical_action_id=?",
                (action.logical_action_id,),
            ).fetchone()
            if action_row is None:
                self._store_action(
                    conn,
                    action,
                    version=0,
                    event_kind="execution_guard_sell_order_associated",
                    observed_at=observed_at,
                )
            else:
                existing_action = _action_from_row(conn, action_row)
                existing_attempt = (
                    existing_action.order_attempts[0]
                    if len(existing_action.order_attempts) == 1
                    else None
                )
                if (
                    existing_action.decision != decision
                    or existing_action.role is not ActionRole.CLOSE
                    or existing_action.side is not OrderSide.SELL
                    or existing_action.holding_episode_id != holding.holding_episode_id
                    or existing_action.requested_quantity != requested_quantity
                    or existing_attempt is None
                    or existing_attempt.requested_quantity != requested_quantity
                    or broker not in existing_attempt.all_broker_order_ids
                    or client not in existing_attempt.all_client_order_ids
                ):
                    raise IdentityConflictError("execution guard SELL conflicts with its immutable physical order")
                action = existing_action

            reference_facts = {
                "provider_id": provider,
                "paper_account_environment_id": source_action.decision.deployment_identity.paper_account_environment_id,
                "store_identity": self.store_identity,
                "logical_action_id": action.logical_action_id,
                "attempt_number": 1,
                "broker_order_id": broker,
                "source_execution_decision_id": decision.decision_id,
            }
            source_digest = _sha256_text(_canonical_json(reference_facts))
            for kind, external_id in (("broker_order_id", broker), ("client_order_id", client)):
                existing_ref = conn.execute(
                    """SELECT logical_action_id, attempt_number, source_payload_sha256
                       FROM policy_state_order_reference_aliases
                       WHERE provider_id=? AND paper_account_environment_id=? AND store_identity=?
                         AND reference_kind=? AND external_order_id=?""",
                    (provider, reference_facts["paper_account_environment_id"], self.store_identity, kind, external_id),
                ).fetchone()
                if existing_ref is None:
                    conn.execute(
                        """INSERT INTO policy_state_order_reference_aliases(
                            provider_id, paper_account_environment_id, store_identity,
                            reference_kind, external_order_id, logical_action_id, attempt_number,
                            source_payload_sha256, first_seen_at_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                        (
                            provider,
                            reference_facts["paper_account_environment_id"],
                            self.store_identity,
                            kind,
                            external_id,
                            action.logical_action_id,
                            source_digest,
                            _aware_iso(observed_at),
                        ),
                    )
                elif (
                    existing_ref["logical_action_id"] != action.logical_action_id
                    or int(existing_ref["attempt_number"]) != 1
                    or existing_ref["source_payload_sha256"] != source_digest
                ):
                    raise OrderReferenceConflictError("guard SELL reference belongs to conflicting execution facts")

            action_row = conn.execute(
                "SELECT * FROM policy_state_actions WHERE logical_action_id=?",
                (action.logical_action_id,),
            ).fetchone()
            receipt = conn.execute(
                """SELECT logical_action_id, attempt_number FROM policy_state_fill_receipts
                   WHERE provider_id=? AND paper_account_environment_id=? AND store_identity=?
                     AND fill_event_id=?""",
                (provider, reference_facts["paper_account_environment_id"], self.store_identity, event_id),
            ).fetchone()
            if receipt is None and int(holding_row["state_version"]) != expected_holding_version:
                raise ConcurrentStateUpdateError("holding changed since the guard SELL caller read it")
            if receipt is not None and (
                receipt["logical_action_id"] != action.logical_action_id
                or int(receipt["attempt_number"]) != 1
            ):
                raise FillReceiptConflictError("guard SELL fill event was reused for another action")
            return self._record_cumulative_fill_in_transaction(
                conn,
                logical_action_id=action.logical_action_id,
                attempt_number=1,
                provider_id=provider,
                fill_event_id=event_id,
                cumulative_quantity=cumulative_quantity,
                cumulative_notional=cumulative_notional,
                cumulative_fees=cumulative_fees,
                payload_sha256=None,
                observed_at=observed_at,
                expected_action_version=int(action_row["state_version"]),
                expected_holding_version=(
                    expected_holding_version if receipt is None else int(holding_row["state_version"])
                ),
                allow_stale_watermark=True,
            )

    def record_protective_sell_fill(
        self,
        *,
        provider_id: str,
        broker_order_id: str,
        client_order_id: str,
        fill_event_id: str,
        cumulative_quantity: Decimal,
        cumulative_notional: Decimal | None,
        cumulative_fees: Decimal | None,
        observed_at: datetime,
        expected_holding_version: int,
    ) -> ActionStateProjection:
        """Record one execution-derived STOP SELL against its durable physical order."""
        provider = _required_text(provider_id, "provider_id")
        broker = _required_text(broker_order_id, "broker_order_id")
        client = _required_text(client_order_id, "client_order_id")
        event_id = _required_text(fill_event_id, "fill_event_id")
        if (
            not isinstance(cumulative_quantity, Decimal)
            or not cumulative_quantity.is_finite()
            or cumulative_quantity <= 0
        ):
            raise ValueError("protective SELL cumulative quantity must be positive and finite")
        for value, name in (
            (cumulative_notional, "cumulative_notional"),
            (cumulative_fees, "cumulative_fees"),
        ):
            if value is not None and (
                not isinstance(value, Decimal) or not value.is_finite() or value < 0
            ):
                raise ValueError(f"{name} must be a finite non-negative Decimal when known")
        if not isinstance(expected_holding_version, int) or isinstance(
            expected_holding_version, bool
        ) or expected_holding_version < 0:
            raise ValueError("expected_holding_version must be a non-negative integer")
        _aware_iso(observed_at)

        with self._transaction(write=True) as conn:
            self._require_ready(conn)
            stop_rows = conn.execute(
                """SELECT * FROM policy_state_stop_updates
                   WHERE store_identity=? AND broker_order_id=? AND status='confirmed'
                   ORDER BY stop_update_action_id""",
                (self.store_identity, broker),
            ).fetchall()
            contexts: dict[
                str,
                tuple[DecisionIdentity, dict[str, object], sqlite3.Row, sqlite3.Row],
            ] = {}
            for candidate_stop in stop_rows:
                candidate_client = str(candidate_stop["client_order_id"] or "")
                if not candidate_client:
                    continue
                try:
                    context = self._load_protective_order_context_in_transaction(
                        conn,
                        stop_row=candidate_stop,
                        provider_id=provider,
                        client_order_id=candidate_client,
                        broker_order_id=broker,
                    )
                except ValueError as exc:
                    if "protective STOP order target is missing" in str(exc):
                        # Reused STOP updates share the original physical-order record.
                        continue
                    if "do not authenticate this STOP order" in str(exc):
                        # Provider-scoped broker identifiers may collide across providers.
                        continue
                    raise
                decision, facts, context_holding = context
                contexts[decision.decision_id] = (
                    decision,
                    facts,
                    context_holding,
                    candidate_stop,
                )
            if len(contexts) != 1:
                raise ValueError(
                    "protective SELL broker reference is missing or ambiguous; reconciliation is required"
                )
            decision, facts, holding_row, stop_row = next(iter(contexts.values()))
            account = str(facts["paper_account_environment_id"])
            generation = str(facts["deployment_generation_id"])
            canonical_client = str(facts["client_order_id"])
            holding = _holding_from_row(holding_row)
            if holding.state_version is None:
                raise ValueError("protective SELL holding has no durable state version")
            action = build_action_intent(
                decision=decision,
                security_id=str(facts["security_id"]),
                broker_symbol=str(facts["symbol"]),
                role=ActionRole.CLOSE,
                side=OrderSide.SELL,
                requested_quantity=Decimal(str(facts["requested_quantity"])),
                holding_episode_id=holding.holding_episode_id,
                status=ActionStatus.SUBMITTED,
            )
            action_id = action.logical_action_id
            action_row = conn.execute(
                """SELECT * FROM policy_state_actions
                   WHERE logical_action_id=? AND deployment_generation_id=?
                     AND paper_account_environment_id=? AND store_identity=?""",
                (action_id, generation, account, self.store_identity),
            ).fetchone()
            if action_row is None:
                attempt = ActionOrderAttempt(
                    attempt_number=1,
                    requested_quantity=action.requested_quantity,
                    status=ActionAttemptStatus.SUBMITTED,
                    client_order_id=canonical_client,
                    broker_order_id=broker,
                )
                action = replace(action, order_attempts=(attempt,))
                self._store_action(
                    conn,
                    action,
                    version=0,
                    event_kind="protective_execution_order_associated",
                    observed_at=observed_at,
                )
                action, action_row = self._load_action(conn, action_id)
            else:
                action = _action_from_row(conn, action_row)
                attempt = action.order_attempts[0] if len(action.order_attempts) == 1 else None
                if (
                    action.decision != decision
                    or action.role is not ActionRole.CLOSE
                    or action.side is not OrderSide.SELL
                    or action.holding_episode_id != holding.holding_episode_id
                    or action.requested_quantity != Decimal(str(facts["requested_quantity"]))
                    or attempt is None
                    or attempt.attempt_number != 1
                    or attempt.requested_quantity != action.requested_quantity
                    or attempt.client_order_id != canonical_client
                    or attempt.broker_order_id != broker
                ):
                    raise IdentityConflictError("protective execution leg conflicts with its immutable physical order")

            reference_facts = {
                "provider_id": provider,
                "paper_account_environment_id": account,
                "store_identity": self.store_identity,
                "logical_action_id": action_id,
                "attempt_number": 1,
                "broker_order_id": broker,
                "source_execution_decision_id": decision.decision_id,
            }
            source_digest = _sha256_text(_canonical_json(reference_facts))
            proven_clients = {
                canonical_client,
                client,
                *(
                    str(alias_row["client_order_id"])
                    for alias_row in stop_rows
                    if alias_row["client_order_id"]
                    and alias_row["deployment_generation_id"] == generation
                    and alias_row["paper_account_environment_id"] == account
                    and alias_row["holding_episode_id"] == holding.holding_episode_id
                ),
            }
            reference_pairs = {
                ("client_order_id", value) for value in proven_clients if value
            } | {("broker_order_id", broker)}
            for reference_kind, external_order_id in sorted(reference_pairs):
                existing_reference = conn.execute(
                    """SELECT logical_action_id, attempt_number, source_payload_sha256
                       FROM policy_state_order_reference_aliases
                       WHERE provider_id=? AND paper_account_environment_id=? AND store_identity=?
                         AND reference_kind=? AND external_order_id=?""",
                    (provider, account, self.store_identity, reference_kind, external_order_id),
                ).fetchone()
                if existing_reference is None:
                    conn.execute(
                        """INSERT INTO policy_state_order_reference_aliases(
                            provider_id, paper_account_environment_id, store_identity,
                            reference_kind, external_order_id, logical_action_id, attempt_number,
                            source_payload_sha256, first_seen_at_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                        (
                            provider,
                            account,
                            self.store_identity,
                            reference_kind,
                            external_order_id,
                            action_id,
                            source_digest,
                            _aware_iso(observed_at),
                        ),
                    )
                elif (
                    existing_reference["logical_action_id"] != action_id
                    or int(existing_reference["attempt_number"]) != 1
                    or existing_reference["source_payload_sha256"] != source_digest
                ):
                    raise OrderReferenceConflictError(
                        "protective physical order reference belongs to conflicting execution facts"
                    )

            existing_receipt = conn.execute(
                """SELECT logical_action_id, attempt_number FROM policy_state_fill_receipts
                   WHERE provider_id=? AND paper_account_environment_id=? AND store_identity=?
                     AND fill_event_id=?""",
                (provider, account, self.store_identity, event_id),
            ).fetchone()
            if existing_receipt is None:
                if int(holding_row["state_version"]) != expected_holding_version:
                    raise ConcurrentStateUpdateError(
                        "holding changed since the protective fill caller read it"
                    )
                owning_stop = None
                if holding.confirmed_stop_action_id is not None:
                    owning_stop = conn.execute(
                        """SELECT * FROM policy_state_stop_updates
                           WHERE stop_update_action_id=? AND deployment_generation_id=?
                             AND paper_account_environment_id=? AND store_identity=?""",
                        (
                            holding.confirmed_stop_action_id,
                            generation,
                            account,
                            self.store_identity,
                        ),
                    ).fetchone()
                if owning_stop is not None and (
                    owning_stop["status"] != "confirmed"
                    or owning_stop["holding_episode_id"] != holding.holding_episode_id
                    or owning_stop["broker_order_id"] != holding.confirmed_stop_broker_order_id
                    or owning_stop["client_order_id"] != holding.confirmed_stop_client_order_id
                ):
                    raise ValueError("holding protective STOP owner facts are inconsistent")
                is_current_stop = bool(
                    owning_stop is not None
                    and owning_stop["broker_order_id"] == broker
                    and holding.confirmed_stop_broker_order_id == broker
                )
                reconciliation_required = (
                    not is_current_stop
                    or bool(dict(holding.policy_flags).get("position_reconciliation_required"))
                )
                action_row = conn.execute(
                    "SELECT * FROM policy_state_actions WHERE logical_action_id=?",
                    (action_id,),
                ).fetchone()
                holding_version = int(holding_row["state_version"])
                if reconciliation_required and not dict(holding.policy_flags).get(
                    "position_reconciliation_required"
                ):
                    flags = dict(holding.policy_flags)
                    flags["position_reconciliation_required"] = (
                        "a protective STOP fill was reported for a superseded or unowned physical order"
                    )
                    reconciled = replace(
                        holding,
                        policy_flags=tuple(flags.items()),
                        committed_risk=None,
                        committed_risk_basis=None,
                        realized_pnl=None,
                    )
                    holding_version = self._save_holding(
                        conn,
                        reconciled,
                        expected_version=holding_version,
                        event_kind="protective_stop_fill_requires_reconciliation",
                        logical_action_id=action_id,
                        observed_at=observed_at,
                    )
                if reconciliation_required and action.status is not ActionStatus.RECONCILIATION_REQUIRED:
                    action = replace(
                        action,
                        status=ActionStatus.RECONCILIATION_REQUIRED,
                        order_attempts=(
                            replace(
                                action.order_attempts[0],
                                status=ActionAttemptStatus.RECONCILIATION_REQUIRED,
                            ),
                        ),
                    )
                    self._store_action(
                        conn,
                        action,
                        version=int(action_row["state_version"]) + 1,
                        event_kind="protective_execution_order_requires_reconciliation",
                        observed_at=observed_at,
                    )
                    action_row = conn.execute(
                        "SELECT * FROM policy_state_actions WHERE logical_action_id=?",
                        (action_id,),
                    ).fetchone()
                return self._record_cumulative_fill_in_transaction(
                    conn,
                    logical_action_id=action_id,
                    attempt_number=1,
                    provider_id=provider,
                    fill_event_id=event_id,
                    cumulative_quantity=cumulative_quantity,
                    cumulative_notional=cumulative_notional,
                    cumulative_fees=cumulative_fees,
                    payload_sha256=None,
                    observed_at=observed_at,
                    expected_action_version=int(action_row["state_version"]),
                    expected_holding_version=holding_version,
                    allow_stale_watermark=True,
                )
            return self._record_cumulative_fill_in_transaction(
                conn,
                logical_action_id=action_id,
                attempt_number=1,
                provider_id=provider,
                fill_event_id=event_id,
                cumulative_quantity=cumulative_quantity,
                cumulative_notional=cumulative_notional,
                cumulative_fees=cumulative_fees,
                payload_sha256=None,
                observed_at=observed_at,
                expected_action_version=int(action_row["state_version"]),
                expected_holding_version=expected_holding_version,
                allow_stale_watermark=True,
            )

    def _record_cumulative_fill_in_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        logical_action_id: str,
        attempt_number: int,
        provider_id: str,
        fill_event_id: str,
        cumulative_quantity: Decimal,
        cumulative_notional: Decimal | None,
        cumulative_fees: Decimal | None,
        payload_sha256: str | None,
        observed_at: datetime,
        expected_action_version: int,
        expected_holding_version: int | None,
        allow_stale_watermark: bool = False,
    ) -> ActionStateProjection:
        """Share canonical fill receipt, accounting and CAS logic on an open transaction."""
        return self.record_cumulative_fill(
            logical_action_id,
            attempt_number,
            provider_id=provider_id,
            fill_event_id=fill_event_id,
            cumulative_quantity=cumulative_quantity,
            cumulative_notional=cumulative_notional,
            cumulative_fees=cumulative_fees,
            payload_sha256=payload_sha256,
            observed_at=observed_at,
            expected_action_version=expected_action_version,
            expected_holding_version=expected_holding_version,
            _connection=conn,
            _allow_stale_watermark=allow_stale_watermark,
        )

    def record_cumulative_fill(
        self,
        logical_action_id: str,
        attempt_number: int,
        *,
        provider_id: str,
        fill_event_id: str,
        cumulative_quantity: Decimal,
        cumulative_notional: Decimal | None,
        cumulative_fees: Decimal | None,
        payload_sha256: str | None,
        observed_at: datetime,
        expected_action_version: int,
        expected_holding_version: int | None,
        _connection: sqlite3.Connection | None = None,
        _allow_stale_watermark: bool = False,
    ) -> ActionStateProjection:
        provider = _required_text(provider_id, "provider_id")
        event_id = _required_text(fill_event_id, "fill_event_id")
        action_id = _required_text(logical_action_id, "logical_action_id")
        if not isinstance(attempt_number, int) or isinstance(attempt_number, bool) or attempt_number not in {1, 2}:
            raise ValueError("attempt_number must be 1 or 2")
        if not isinstance(cumulative_quantity, Decimal) or not cumulative_quantity.is_finite() or cumulative_quantity < 0:
            raise ValueError("cumulative_quantity must be a finite non-negative Decimal")
        for value, name in ((cumulative_notional, "cumulative_notional"), (cumulative_fees, "cumulative_fees")):
            if value is not None and (not isinstance(value, Decimal) or not value.is_finite() or value < 0):
                raise ValueError(f"{name} must be a finite non-negative Decimal when known")
        _aware_iso(observed_at)

        transaction = (
            self._transaction(write=True)
            if _connection is None
            else nullcontext(_connection)
        )
        with transaction as conn:
            self._require_ready(conn)
            intent, action_row = self._load_action(conn, action_id)
            account = intent.decision.deployment_identity.paper_account_environment_id
            generation = intent.deployment_generation_id
            attempt_row = conn.execute(
                """SELECT * FROM policy_state_order_attempts
                   WHERE logical_action_id=? AND attempt_number=? AND paper_account_environment_id=? AND store_identity=?""",
                (action_id, attempt_number, account, self.store_identity),
            ).fetchone()
            if attempt_row is None:
                raise ValueError("fill refers to an order attempt that was not created")

            facts = {
                "provider_id": provider,
                "paper_account_environment_id": account,
                "store_identity": self.store_identity,
                "fill_event_id": event_id,
                "logical_action_id": action_id,
                "attempt_number": attempt_number,
                "cumulative_quantity": str(cumulative_quantity),
                "cumulative_notional": _decimal_text(cumulative_notional),
                "cumulative_fees": _decimal_text(cumulative_fees),
            }
            digest = _sha256_text(_canonical_json(facts)) if payload_sha256 is None else _required_text(payload_sha256, "payload_sha256").lower()
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError("payload_sha256 must be a 64-character SHA-256 digest")
            receipt = conn.execute(
                """SELECT * FROM policy_state_fill_receipts
                   WHERE provider_id=? AND paper_account_environment_id=? AND store_identity=? AND fill_event_id=?""",
                (provider, account, self.store_identity, event_id),
            ).fetchone()
            if receipt is not None:
                same = (
                    receipt["logical_action_id"] == action_id
                    and int(receipt["attempt_number"]) == attempt_number
                    and receipt["payload_sha256"] == digest
                    and receipt["cumulative_quantity"] == str(cumulative_quantity)
                    and receipt["cumulative_notional"] == _decimal_text(cumulative_notional)
                    and receipt["cumulative_fees"] == _decimal_text(cumulative_fees)
                )
                if not same:
                    raise FillReceiptConflictError("fill event ID was reused with different immutable payload facts")
                return _action_projection_from_row(conn, intent, action_row)
            if int(action_row["state_version"]) != expected_action_version:
                raise ConcurrentStateUpdateError("action changed since the caller read it")

            previous_quantity = Decimal(attempt_row["confirmed_quantity"])
            previous_notional = _decimal_value(attempt_row["cumulative_notional"])
            previous_fees = _decimal_value(attempt_row["cumulative_fees"])
            stale_watermark = cumulative_quantity < previous_quantity
            if stale_watermark and not _allow_stale_watermark:
                raise ValueError("attempt cumulative fill cannot decrease")
            if cumulative_quantity == previous_quantity:
                if (
                    cumulative_notional is not None and previous_notional is not None and cumulative_notional != previous_notional
                ) or (cumulative_fees is not None and previous_fees is not None and cumulative_fees != previous_fees):
                    raise FillReceiptConflictError("cumulative notional or fees changed without a quantity watermark change")
            if stale_watermark:
                if (
                    cumulative_notional is not None
                    and previous_notional is not None
                    and cumulative_notional > previous_notional
                ) or (
                    cumulative_fees is not None
                    and previous_fees is not None
                    and cumulative_fees > previous_fees
                ):
                    raise FillReceiptConflictError(
                        "an older cumulative fill checkpoint exceeds the latest known total"
                    )
            if cumulative_quantity > previous_quantity:
                if previous_notional is not None and cumulative_notional is not None and cumulative_notional < previous_notional:
                    raise ValueError("cumulative notional cannot decrease")
                if previous_fees is not None and cumulative_fees is not None and cumulative_fees < previous_fees:
                    raise ValueError("cumulative fees cannot decrease")

            holding_id = intent.holding_episode_id
            holding_row = None if holding_id is None else self._holding_row(conn, holding_id)
            if intent.role is ActionRole.REPLACEMENT:
                expected_opening_holding_id = _replacement_holding_id(intent)
                if holding_id is not None and holding_id != expected_opening_holding_id:
                    raise ValueError("replacement holding episode must be its own opening holding")
                if holding_row is not None:
                    _validate_replacement_holding(intent, _holding_from_row(holding_row))
            if stale_watermark:
                if holding_row is None:
                    raise ValueError("a stale protective fill has no durable holding episode")
            elif holding_row is None:
                if intent.role not in {ActionRole.ENTRY, ActionRole.REPLACEMENT} or cumulative_quantity == previous_quantity:
                    if expected_holding_version is not None:
                        raise ConcurrentStateUpdateError("action has no holding at the expected version")
                elif expected_holding_version is not None:
                    raise ConcurrentStateUpdateError("opening holding must not exist before the first confirmed fill")
            elif expected_holding_version is None or int(holding_row["state_version"]) != expected_holding_version:
                raise ConcurrentStateUpdateError("holding changed since the caller read it")

            conn.execute(
                """INSERT INTO policy_state_fill_receipts(
                    provider_id, paper_account_environment_id, store_identity, fill_event_id,
                    logical_action_id, attempt_number, payload_sha256, cumulative_quantity,
                    cumulative_notional, cumulative_fees, observed_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    provider,
                    account,
                    self.store_identity,
                    event_id,
                    action_id,
                    attempt_number,
                    digest,
                    str(cumulative_quantity),
                    _decimal_text(cumulative_notional),
                    _decimal_text(cumulative_fees),
                    _aware_iso(observed_at),
                ),
            )
            if stale_watermark:
                return _action_projection_from_row(conn, intent, action_row)
            if cumulative_quantity == previous_quantity:
                return _action_projection_from_row(conn, intent, action_row)

            next_notional = previous_notional if cumulative_notional is None else cumulative_notional
            next_fees = previous_fees if cumulative_fees is None else cumulative_fees
            conn.execute(
                """UPDATE policy_state_order_attempts SET cumulative_notional=?, cumulative_fees=?
                   WHERE logical_action_id=? AND attempt_number=?""",
                (
                    _decimal_text(next_notional),
                    _decimal_text(next_fees),
                    action_id,
                    attempt_number,
                ),
            )

            updated_intent = apply_attempt_cumulative_fill(intent, attempt_number, cumulative_quantity)
            old_aggregate = intent.confirmed_filled_quantity
            delta_quantity = updated_intent.confirmed_filled_quantity - old_aggregate
            delta_notional: Decimal | None = None
            delta_fees: Decimal | None = None
            if delta_quantity > 0:
                if cumulative_notional is not None:
                    delta_notional = cumulative_notional if previous_quantity == 0 else (
                        None if previous_notional is None else cumulative_notional - previous_notional
                    )
                if cumulative_fees is not None:
                    delta_fees = cumulative_fees if previous_quantity == 0 else (
                        None if previous_fees is None else cumulative_fees - previous_fees
                    )

            new_holding: HoldingEpisode | None = None
            current_holding_version: int | None = None
            if delta_quantity > 0 and holding_row is None and intent.role in {ActionRole.ENTRY, ActionRole.REPLACEMENT}:
                fill_price = (
                    None
                    if delta_notional is None or delta_fees is None
                    else (delta_notional + delta_fees) / delta_quantity
                )
                holding_id = HoldingEpisode.open(
                    deployment_generation_id=generation,
                    security_id=intent.security_id,
                    symbol=intent.broker_symbol,
                    broker_symbol=intent.broker_symbol,
                    opening_action_id=action_id,
                    initial_filled_quantity=delta_quantity,
                    entry_price=fill_price,
                ).holding_episode_id
                updated_intent = replace(updated_intent, holding_episode_id=holding_id)
                new_holding = HoldingEpisode.open(
                    deployment_generation_id=generation,
                    security_id=intent.security_id,
                    symbol=intent.broker_symbol,
                    broker_symbol=intent.broker_symbol,
                    opening_action_id=action_id,
                    initial_filled_quantity=delta_quantity,
                    entry_price=fill_price,
                )
                if updated_intent.status is not ActionStatus.FILLED:
                    new_holding = register_pending_action(new_holding, updated_intent)
                if updated_intent.risk_per_unit is not None:
                    new_holding = replace(
                        new_holding,
                        committed_risk=delta_quantity * updated_intent.risk_per_unit,
                        committed_risk_basis=updated_intent.risk_basis,
                    )
                current_holding_version = None
            elif delta_quantity > 0 and holding_row is not None:
                current_holding = _holding_from_row(holding_row)
                if intent.role in {ActionRole.SCALE_OUT, ActionRole.CLOSE} and delta_quantity > current_holding.remaining_quantity:
                    flags = dict(current_holding.policy_flags)
                    flags["position_reconciliation_required"] = "confirmed sell fill exceeds known remaining quantity"
                    watermarks = dict(current_holding.applied_action_fill_watermarks)
                    watermarks[action_id] = updated_intent.confirmed_filled_quantity
                    new_holding = replace(
                        current_holding,
                        policy_flags=tuple(flags.items()),
                        applied_action_fill_watermarks=tuple(watermarks.items()),
                        committed_risk=None,
                        committed_risk_basis=None,
                        realized_pnl=None,
                    )
                    updated_intent = replace(updated_intent, status=ActionStatus.RECONCILIATION_REQUIRED)
                    new_holding = register_reconciliation_action(new_holding, updated_intent)
                else:
                    new_holding = apply_action_fill_to_holding(
                        current_holding,
                        updated_intent,
                        incremental_fill_notional=delta_notional,
                        incremental_fees=delta_fees,
                    )
                    if intent.role in {ActionRole.ENTRY, ActionRole.REPLACEMENT, ActionRole.ADDITION}:
                        matching_risk_basis = (
                            current_holding.committed_risk_basis is not None
                            and current_holding.committed_risk_basis == updated_intent.risk_basis
                        )
                        aggregate_risk = None
                        if (
                            matching_risk_basis
                            and current_holding.committed_risk is not None
                            and updated_intent.risk_per_unit is not None
                        ):
                            aggregate_risk = (
                                current_holding.committed_risk
                                + delta_quantity * updated_intent.risk_per_unit
                            )
                        new_holding = replace(
                            new_holding,
                            committed_risk=aggregate_risk,
                            committed_risk_basis=(
                                current_holding.committed_risk_basis if matching_risk_basis else None
                            ),
                        )
                    if updated_intent.status is ActionStatus.FILLED:
                        if intent.role is ActionRole.SCALE_OUT:
                            new_holding = advance_holding_exit_tier(new_holding, updated_intent)
                        else:
                            new_holding = clear_terminal_pending_action(new_holding, updated_intent)
                    elif updated_intent.status is ActionStatus.RECONCILIATION_REQUIRED:
                        new_holding = register_reconciliation_action(new_holding, updated_intent)
                current_holding_version = int(holding_row["state_version"])

            if new_holding is not None and current_holding_version is None:
                self._save_holding(
                    conn,
                    new_holding,
                    expected_version=None,
                    event_kind="cumulative_fill_applied",
                    logical_action_id=action_id,
                    observed_at=observed_at,
                    initial_version=1,
                )
            self._store_action(
                conn,
                updated_intent,
                version=int(action_row["state_version"]) + 1,
                event_kind="cumulative_fill_observed",
                observed_at=observed_at,
            )
            if new_holding is not None and current_holding_version is not None:
                self._save_holding(
                    conn,
                    new_holding,
                    expected_version=current_holding_version,
                    event_kind="cumulative_fill_applied" if delta_quantity > 0 else "fill_reconciliation_required",
                    logical_action_id=action_id,
                    observed_at=observed_at,
                    initial_version=0,
                )
            return replace(
                _action_projection_from_row(conn, updated_intent, action_row),
                state_version=int(action_row["state_version"]) + 1,
            )

    def _finish_holding_action(
        self,
        conn: sqlite3.Connection,
        intent: ActionIntent,
        *,
        expected_holding_version: int | None,
        event_kind: str,
        observed_at: datetime,
    ) -> None:
        if intent.holding_episode_id is None:
            return
        row = self._holding_row(conn, intent.holding_episode_id)
        if row is None:
            raise ValueError("action holding episode is missing")
        version = int(row["state_version"])
        if expected_holding_version is not None and version != expected_holding_version:
            raise ConcurrentStateUpdateError("holding changed since the caller read it")
        holding = _holding_from_row(row)
        watermark = dict(holding.applied_action_fill_watermarks).get(intent.logical_action_id, Decimal("0"))
        if watermark < intent.confirmed_filled_quantity:
            raise ValueError("holding fills must reconcile to the confirmed action quantity before completion")
        if dict(holding.policy_flags).get("position_reconciliation_required"):
            raise ValueError("holding position reconciliation must clear before action completion")
        if intent.status is ActionStatus.FILLED or intent.status is ActionStatus.RESOLVED:
            if intent.role is ActionRole.SCALE_OUT:
                if intent.exit_tier is not None and intent.exit_tier <= holding.last_exit_tier:
                    holding = clear_terminal_pending_action(holding, intent)
                else:
                    holding = advance_holding_exit_tier(holding, intent)
            else:
                holding = clear_terminal_pending_action(holding, intent)
            self._save_holding(
                conn,
                holding,
                expected_version=version,
                event_kind=event_kind,
                logical_action_id=intent.logical_action_id,
                observed_at=observed_at,
            )

    def _versioned_action_transition(
        self,
        conn: sqlite3.Connection,
        current: ActionIntent,
        action_row: sqlite3.Row,
        updated: ActionIntent,
        *,
        event_kind: str,
        observed_at: datetime,
        expected_holding_version: int | None = None,
        force_event: bool = False,
    ) -> ActionStateProjection:
        if updated == current and not force_event:
            return _action_projection_from_row(conn, current, action_row)
        new_version = int(action_row["state_version"]) + 1
        self._store_action(
            conn,
            updated,
            version=new_version,
            event_kind=event_kind,
            observed_at=observed_at,
        )
        if (
            current.status in {ActionStatus.FILLED, ActionStatus.RESOLVED}
            and updated.status is ActionStatus.RECONCILIATION_REQUIRED
            and updated.holding_episode_id is not None
        ):
            holding_row = self._holding_row(conn, updated.holding_episode_id)
            if holding_row is None:
                raise ValueError("reopened holding action has no durable holding episode")
            holding = _holding_from_row(holding_row)
            reconciled_holding = register_reconciliation_action(holding, updated)
            if reconciled_holding != holding:
                self._save_holding(
                    conn,
                    reconciled_holding,
                    expected_version=int(holding_row["state_version"]),
                    event_kind="holding_action_reopened_for_reconciliation",
                    logical_action_id=updated.logical_action_id,
                    observed_at=observed_at,
                )
        if (
            updated != current
            and updated.status in {ActionStatus.FILLED, ActionStatus.RESOLVED}
            and current.status not in {ActionStatus.FILLED, ActionStatus.RESOLVED}
        ):
            self._finish_holding_action(
                conn,
                updated,
                expected_holding_version=expected_holding_version,
                event_kind=event_kind,
                observed_at=observed_at,
            )
        return replace(_action_projection_from_row(conn, updated, action_row), state_version=new_version)

    def request_order_cancel(
        self,
        logical_action_id: str,
        attempt_number: int,
        *,
        expected_action_version: int,
        observed_at: datetime,
    ) -> ActionStateProjection:
        _aware_iso(observed_at)
        with self._transaction(write=True) as conn:
            self._require_ready(conn)
            intent, row = self._load_action(conn, logical_action_id)
            if int(row["state_version"]) != expected_action_version:
                raise ConcurrentStateUpdateError("action changed since the caller read it")
            updated = request_attempt_cancel(intent, attempt_number)
            return self._versioned_action_transition(
                conn,
                intent,
                row,
                updated,
                event_kind="cancel_requested",
                observed_at=observed_at,
            )

    def confirm_order_terminal(
        self,
        logical_action_id: str,
        attempt_number: int,
        *,
        terminal_status: ActionAttemptStatus,
        expected_action_version: int,
        expected_holding_version: int | None = None,
        observed_at: datetime,
    ) -> ActionStateProjection:
        _aware_iso(observed_at)
        with self._transaction(write=True) as conn:
            self._require_ready(conn)
            intent, row = self._load_action(conn, logical_action_id)
            if int(row["state_version"]) != expected_action_version:
                raise ConcurrentStateUpdateError("action changed since the caller read it")
            updated = confirm_attempt_terminal(intent, attempt_number, status=terminal_status)
            return self._versioned_action_transition(
                conn,
                intent,
                row,
                updated,
                event_kind="broker_terminal_confirmed",
                observed_at=observed_at,
                expected_holding_version=expected_holding_version,
            )

    def create_single_remainder_attempt(
        self,
        logical_action_id: str,
        *,
        expected_action_version: int,
        observed_at: datetime,
    ) -> ActionStateProjection:
        _aware_iso(observed_at)
        with self._transaction(write=True) as conn:
            self._require_ready(conn)
            intent, row = self._load_action(conn, logical_action_id)
            if int(row["state_version"]) != expected_action_version:
                raise ConcurrentStateUpdateError("action changed since the caller read it")
            updated = create_remainder_in_state(intent)
            return self._versioned_action_transition(
                conn,
                intent,
                row,
                updated,
                event_kind="single_remainder_created",
                observed_at=observed_at,
            )

    def record_explicit_action_resolution(
        self,
        logical_action_id: str,
        *,
        resolution_reason: str,
        expected_action_version: int,
        expected_holding_version: int | None = None,
        observed_at: datetime,
    ) -> ActionStateProjection:
        _aware_iso(observed_at)
        with self._transaction(write=True) as conn:
            self._require_ready(conn)
            intent, row = self._load_action(conn, logical_action_id)
            if int(row["state_version"]) != expected_action_version:
                raise ConcurrentStateUpdateError("action changed since the caller read it")
            updated = resolve_action(
                intent,
                status=ActionStatus.RESOLVED,
                resolution_reason=resolution_reason,
            )
            return self._versioned_action_transition(
                conn,
                intent,
                row,
                updated,
                event_kind="explicit_reconciliation_recorded",
                observed_at=observed_at,
                expected_holding_version=expected_holding_version,
            )

    def bind_attempt_order_refs(
        self,
        logical_action_id: str,
        attempt_number: int,
        *,
        provider_id: str,
        client_order_id: str | None = None,
        broker_order_id: str | None = None,
        source_payload_sha256: str | None = None,
        expected_action_version: int,
        observed_at: datetime,
    ) -> ActionStateProjection:
        provider = _required_text(provider_id, "provider_id")
        _aware_iso(observed_at)
        if client_order_id is None and broker_order_id is None:
            raise ValueError("at least one order reference is required")
        client = None if client_order_id is None else _required_text(client_order_id, "client_order_id")
        broker = None if broker_order_id is None else _required_text(broker_order_id, "broker_order_id")
        with self._transaction(write=True) as conn:
            self._require_ready(conn)
            intent, action_row = self._load_action(conn, logical_action_id)
            if int(action_row["state_version"]) != expected_action_version:
                raise ConcurrentStateUpdateError("action changed since the caller read it")
            account = intent.decision.deployment_identity.paper_account_environment_id
            refs = tuple((kind, value) for kind, value in (("client_order_id", client), ("broker_order_id", broker)) if value is not None)
            source = source_payload_sha256
            if source is None:
                source = _sha256_text(
                    _canonical_json(
                        {
                            "provider_id": provider,
                            "logical_action_id": intent.logical_action_id,
                            "attempt_number": attempt_number,
                            "references": refs,
                        }
                    )
                )
            source = _required_text(source, "source_payload_sha256").lower()
            if len(source) != 64 or any(char not in "0123456789abcdef" for char in source):
                raise ValueError("source_payload_sha256 must be a 64-character SHA-256 digest")
            attempt = next(
                (item for item in intent.order_attempts if item.attempt_number == attempt_number),
                None,
            )
            if attempt is None:
                raise ValueError(f"action has no order attempt {attempt_number}")
            alias_rows_added = False
            for kind, external_id in refs:
                for other in intent.order_attempts:
                    if other.attempt_number == attempt_number:
                        continue
                    other_refs = (
                        other.all_client_order_ids if kind == "client_order_id" else other.all_broker_order_ids
                    )
                    if external_id in other_refs:
                        raise OrderReferenceConflictError("order reference already belongs to another attempt")
                existing = conn.execute(
                    """SELECT logical_action_id, attempt_number FROM policy_state_order_reference_aliases
                       WHERE provider_id=? AND paper_account_environment_id=? AND store_identity=?
                         AND reference_kind=? AND external_order_id=?""",
                    (provider, account, self.store_identity, kind, external_id),
                ).fetchone()
                if existing is not None and (
                    existing["logical_action_id"] != intent.logical_action_id
                    or int(existing["attempt_number"]) != attempt_number
                ):
                    raise OrderReferenceConflictError("external order reference already belongs to another attempt")
                if existing is None:
                    alias_rows_added = True
                    conn.execute(
                        """INSERT INTO policy_state_order_reference_aliases(
                            provider_id, paper_account_environment_id, store_identity, reference_kind,
                            external_order_id, logical_action_id, attempt_number, source_payload_sha256, first_seen_at_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            provider,
                            account,
                            self.store_identity,
                            kind,
                            external_id,
                            intent.logical_action_id,
                            attempt_number,
                            source,
                            _aware_iso(observed_at),
                        ),
                    )
            old_attempt = attempt
            primary_client = client if old_attempt.client_order_id in {None, client} else None
            primary_broker = broker if old_attempt.broker_order_id in {None, broker} else None
            updated = bind_attempt_order_refs(
                intent,
                attempt_number=attempt_number,
                client_order_id=primary_client,
                broker_order_id=primary_broker,
            )
            updated = add_attempt_order_aliases(
                updated,
                attempt_number=attempt_number,
                client_order_id=client if primary_client is None else None,
                broker_order_id=broker if primary_broker is None else None,
            )
            attempt = next(item for item in updated.order_attempts if item.attempt_number == attempt_number)
            if attempt.status is ActionAttemptStatus.INTENDED:
                submitted = replace(attempt, status=ActionAttemptStatus.SUBMITTED)
                next_action_status = intent.status
                if intent.status is ActionStatus.INTENDED:
                    next_action_status = ActionStatus.SUBMITTED
                elif intent.status in {ActionStatus.FILLED, ActionStatus.RESOLVED}:
                    next_action_status = ActionStatus.RECONCILIATION_REQUIRED
                updated = replace(
                    updated,
                    status=next_action_status,
                    order_attempts=tuple(submitted if item.attempt_number == attempt_number else item for item in updated.order_attempts),
                )
            return self._versioned_action_transition(
                conn,
                intent,
                action_row,
                updated,
                event_kind="order_references_bound",
                observed_at=observed_at,
                force_event=alias_rows_added,
            )

    def load_order_reference_aliases(self, logical_action_id: str) -> tuple[Mapping[str, object], ...]:
        action_id = _required_text(logical_action_id, "logical_action_id")
        with self._transaction(write=False) as conn:
            self._require_ready(conn)
            if conn.execute(
                "SELECT 1 FROM policy_state_actions WHERE logical_action_id=? AND store_identity=?",
                (action_id, self.store_identity),
            ).fetchone() is None:
                raise KeyError("logical action was not found")
            return tuple(
                {
                    "provider_id": str(row["provider_id"]),
                    "paper_account_environment_id": str(row["paper_account_environment_id"]),
                    "store_identity": str(row["store_identity"]),
                    "reference_kind": str(row["reference_kind"]),
                    "external_order_id": str(row["external_order_id"]),
                    "attempt_number": int(row["attempt_number"]),
                    "source_payload_sha256": str(row["source_payload_sha256"]),
                    "first_seen_at_utc": str(row["first_seen_at_utc"]),
                }
                for row in conn.execute(
                    """SELECT * FROM policy_state_order_reference_aliases
                       WHERE logical_action_id=? ORDER BY attempt_number, provider_id, reference_kind, external_order_id""",
                    (action_id,),
                )
            )

    def record_portfolio_snapshot(self, snapshot: PortfolioStateSnapshot) -> str:
        identity = snapshot.deployment_identity
        self._validate_deployment_scope(identity)
        state_json = _canonical_json(_portfolio_payload(snapshot))
        state_sha = _sha256_text(state_json)
        with self._transaction(write=True) as conn:
            self._require_ready(conn)
            self._require_deployment(conn, identity.deployment_generation_id, identity.paper_account_environment_id)
            existing = conn.execute(
                "SELECT state_sha256, state_json FROM policy_state_portfolio_snapshots WHERE portfolio_snapshot_id=?",
                (snapshot.portfolio_snapshot_id,),
            ).fetchone()
            if existing is not None:
                if (existing["state_sha256"], existing["state_json"]) != (state_sha, state_json):
                    raise IdentityConflictError("portfolio snapshot identity already has different immutable facts")
                return snapshot.portfolio_snapshot_id
            duplicate_source = conn.execute(
                """SELECT portfolio_snapshot_id FROM policy_state_portfolio_snapshots
                   WHERE deployment_generation_id=? AND source_namespace=? AND account_snapshot_id=?""",
                (identity.deployment_generation_id, snapshot.source_namespace, snapshot.account_snapshot_id),
            ).fetchone()
            if duplicate_source is not None:
                raise IdentityConflictError("source account snapshot identity already has different clock/facts")
            conn.execute(
                """INSERT INTO policy_state_portfolio_snapshots(
                    portfolio_snapshot_id, deployment_generation_id, paper_account_environment_id, store_identity,
                    source_namespace, account_snapshot_id, completed_session, account_valuation_session,
                    account_valuation_at, equity, cash, gross_exposure, open_risk, portfolio_peak_equity,
                    last_accepted_session, policy_flags_json, state_sha256, state_json, state_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (
                    snapshot.portfolio_snapshot_id,
                    identity.deployment_generation_id,
                    identity.paper_account_environment_id,
                    self.store_identity,
                    snapshot.source_namespace,
                    snapshot.account_snapshot_id,
                    snapshot.clock.decision_session.isoformat(),
                    snapshot.clock.account_valuation_session.isoformat(),
                    _aware_iso(snapshot.clock.account_valuation_at),
                    _decimal_text(snapshot.equity),
                    _decimal_text(snapshot.cash),
                    _decimal_text(snapshot.gross_exposure),
                    _decimal_text(snapshot.open_risk),
                    _decimal_text(snapshot.portfolio_peak_equity),
                    None if snapshot.last_accepted_session is None else snapshot.last_accepted_session.isoformat(),
                    _canonical_json(snapshot.policy_flags),
                    state_sha,
                    state_json,
                ),
            )
        return snapshot.portfolio_snapshot_id

    def load_portfolio_snapshot(self, portfolio_snapshot_id: str) -> PortfolioStateSnapshot:
        snapshot_id = _required_text(portfolio_snapshot_id, "portfolio_snapshot_id")
        with self._transaction(write=False) as conn:
            self._require_ready(conn)
            row = conn.execute(
                "SELECT state_json FROM policy_state_portfolio_snapshots WHERE portfolio_snapshot_id=? AND store_identity=?",
                (snapshot_id, self.store_identity),
            ).fetchone()
            if row is None:
                raise KeyError("portfolio snapshot was not found")
            return _portfolio_from_payload(json.loads(row[0]))

    def load_policy_execution_snapshot(
        self,
        *,
        deployment_generation_id: str,
        portfolio_snapshot_id: str,
    ) -> PolicyExecutionReadSnapshot:
        """Read one portfolio plus account/store actions and holdings in one SQLite snapshot."""
        generation = _required_text(deployment_generation_id, "deployment_generation_id")
        snapshot_id = _required_text(portfolio_snapshot_id, "portfolio_snapshot_id")
        with self._transaction(write=False) as conn:
            self._require_ready(conn)
            deployment = conn.execute(
                "SELECT identity_json FROM policy_state_deployments WHERE deployment_generation_id=? AND store_identity=?",
                (generation, self.store_identity),
            ).fetchone()
            if deployment is None:
                raise KeyError("deployment generation was not found")
            identity = _deployment_from_payload(json.loads(deployment[0]))
            portfolio_row = conn.execute(
                """SELECT state_json, deployment_generation_id FROM policy_state_portfolio_snapshots
                   WHERE portfolio_snapshot_id=? AND paper_account_environment_id=? AND store_identity=?""",
                (snapshot_id, identity.paper_account_environment_id, self.store_identity),
            ).fetchone()
            if portfolio_row is None or portfolio_row["deployment_generation_id"] != generation:
                raise KeyError("portfolio snapshot is not part of the requested generation")
            portfolio = _portfolio_from_payload(json.loads(portfolio_row["state_json"]))
            if portfolio.deployment_identity != identity or portfolio.portfolio_snapshot_id != snapshot_id:
                raise IdentityConflictError("portfolio snapshot payload disagrees with its durable generation or identity")
            holding_rows = conn.execute(
                """SELECT * FROM policy_state_holdings
                   WHERE paper_account_environment_id=? AND store_identity=?
                   ORDER BY holding_episode_id""",
                (identity.paper_account_environment_id, self.store_identity),
            ).fetchall()
            holdings = tuple(_holding_from_row(row) for row in holding_rows)
            holding_ids = {holding.holding_episode_id for holding in holdings}
            holding_by_id = {holding.holding_episode_id: holding for holding in holdings}
            holding_action_ids = {
                action_id
                for holding in holdings
                for action_id in (
                    holding.opening_action_id,
                    *holding.pending_action_ids,
                    *holding.completed_addition_action_ids,
                    *(action_id for action_id, _ in holding.applied_action_fill_watermarks),
                )
            }
            action_rows = conn.execute(
                """SELECT * FROM policy_state_actions
                   WHERE paper_account_environment_id=? AND store_identity=?
                   ORDER BY logical_action_id""",
                (identity.paper_account_environment_id, self.store_identity),
            ).fetchall()
            projections_list = []
            for row in action_rows:
                intent = _action_from_row(conn, row)
                if not (
                    intent.is_open
                    or intent.logical_action_id in holding_action_ids
                    or intent.holding_episode_id in holding_ids
                ):
                    continue
                if self._is_confirmed_protective_execution_projection(
                    conn,
                    intent=intent,
                    action_row=row,
                    holding_by_id=holding_by_id,
                ):
                    continue
                projections_list.append(_action_projection_from_row(conn, intent, row))
            projections = tuple(projections_list)
            return PolicyExecutionReadSnapshot(identity, portfolio, projections, holdings)

    def propose_stop_update(
        self,
        holding_episode_id: str,
        *,
        decision: DecisionIdentity,
        stop_price: Decimal,
        expected_holding_version: int,
        observed_at: datetime,
    ) -> StopUpdateIntent:
        _aware_iso(observed_at)
        self._validate_deployment_scope(decision.deployment_identity)
        with self._transaction(write=True) as conn:
            self._require_ready(conn)
            decision_row = conn.execute(
                "SELECT decision_json FROM policy_state_decisions WHERE decision_id=?",
                (decision.decision_id,),
            ).fetchone()
            if decision_row is None or decision_row[0] != _canonical_json(_decision_payload(decision)):
                raise ValueError("stop update decision must be recorded with identical immutable facts")
            holding_row = self._holding_row(conn, _required_text(holding_episode_id, "holding_episode_id"))
            if holding_row is None:
                raise KeyError("holding episode was not found")
            holding = _holding_from_row(holding_row)
            intent = StopUpdateIntent(
                deployment_generation_id=decision.deployment_generation_id,
                decision_id=decision.decision_id,
                holding_episode_id=holding_episode_id,
                requested_stop_price=stop_price,
            )
            existing = conn.execute(
                """SELECT * FROM policy_state_stop_updates
                   WHERE stop_update_action_id=? AND store_identity=?""",
                (intent.logical_action_id, self.store_identity),
            ).fetchone()
            if existing is not None:
                immutable = (
                    existing["deployment_generation_id"],
                    existing["paper_account_environment_id"],
                    existing["store_identity"],
                    existing["holding_episode_id"],
                    existing["security_id"],
                    existing["decision_id"],
                    Decimal(existing["requested_stop_price"]),
                )
                expected = (
                    decision.deployment_generation_id,
                    decision.deployment_identity.paper_account_environment_id,
                    self.store_identity,
                    holding_episode_id,
                    holding.security_id,
                    decision.decision_id,
                    intent.requested_stop_price,
                )
                if immutable != expected:
                    raise IdentityConflictError("stop update action already has different requested facts")
                raise StopUpdateProposalAlreadyClaimedError(
                    "matching stop update proposal already exists; reconcile before dispatching again"
                )
            if int(holding_row["state_version"]) != expected_holding_version:
                raise ConcurrentStateUpdateError("holding changed since the caller read it")
            pending_intents = tuple(
                self._load_action(conn, action_id)[0]
                for action_id in holding.pending_action_ids
            )
            updated_holding, proposed = propose_stop_in_state(
                holding,
                decision=decision,
                stop_price=stop_price,
                coexisting_entry_intents=pending_intents,
            )
            identity = decision.deployment_identity
            conn.execute(
                """INSERT INTO policy_state_stop_updates(
                    stop_update_action_id, deployment_generation_id, paper_account_environment_id, store_identity,
                    holding_episode_id, security_id, decision_id, requested_stop_price, status,
                    client_order_id, broker_order_id, confirmed_stop_price, observed_at_utc, state_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'proposed', NULL, NULL, NULL, ?, 0)""",
                (
                    proposed.logical_action_id,
                    identity.deployment_generation_id,
                    identity.paper_account_environment_id,
                    self.store_identity,
                    holding.holding_episode_id,
                    holding.security_id,
                    decision.decision_id,
                    str(proposed.requested_stop_price),
                    _aware_iso(observed_at),
                ),
            )
            self._save_holding(
                conn,
                updated_holding,
                expected_version=expected_holding_version,
                event_kind="stop_update_proposed",
                observed_at=observed_at,
            )
            return proposed

    def load_stop_update_intent(self, stop_update_action_id: str) -> StopUpdateIntent:
        action_id = _required_text(stop_update_action_id, "stop_update_action_id")
        with self._transaction(write=False) as conn:
            self._require_ready(conn)
            row = conn.execute(
                "SELECT * FROM policy_state_stop_updates WHERE stop_update_action_id=? AND store_identity=?",
                (action_id, self.store_identity),
            ).fetchone()
            if row is None:
                raise KeyError("stop update intent was not found")
            return StopUpdateIntent(
                deployment_generation_id=str(row["deployment_generation_id"]),
                decision_id=str(row["decision_id"]),
                holding_episode_id=str(row["holding_episode_id"]),
                requested_stop_price=Decimal(row["requested_stop_price"]),
            )

    def confirmed_protective_order_quantity(
        self, stop_update_action_id: str, *, provider_id: str
    ) -> Decimal:
        """Read the frozen physical STOP target only while it owns its holding."""
        action_id = _required_text(stop_update_action_id, "stop_update_action_id")
        provider = _required_text(provider_id, "provider_id")
        with self._transaction(write=False) as conn:
            self._require_ready(conn)
            stop_row = conn.execute(
                "SELECT * FROM policy_state_stop_updates WHERE stop_update_action_id=? AND store_identity=?",
                (action_id, self.store_identity),
            ).fetchone()
            if stop_row is None or stop_row["status"] != "confirmed":
                raise ValueError("protective STOP has no confirmed physical target")
            holding_row = self._holding_row(conn, str(stop_row["holding_episode_id"]))
            if (
                holding_row is None
                or holding_row["confirmed_stop_action_id"] != action_id
                or holding_row["confirmed_stop_client_order_id"] != stop_row["client_order_id"]
                or holding_row["confirmed_stop_broker_order_id"] != stop_row["broker_order_id"]
            ):
                raise ValueError("protective STOP no longer owns the holding")
            _, facts, _ = self._load_protective_order_context_in_transaction(
                conn,
                stop_row=stop_row,
                provider_id=provider,
                client_order_id=str(stop_row["client_order_id"]),
                broker_order_id=str(stop_row["broker_order_id"]),
            )
            return Decimal(str(facts["requested_quantity"]))

    def _protective_order_decision_facts(
        self,
        conn: sqlite3.Connection,
        *,
        stop_row: sqlite3.Row,
        holding_row: sqlite3.Row,
        provider_id: str,
        client_order_id: str,
        broker_order_id: str,
        requested_quantity: Decimal,
        confirmed_stop_price: Decimal,
    ) -> tuple[DecisionIdentity, dict[str, object], dict[str, object], dict[str, object]]:
        """Build immutable execution provenance from an accepted stop and holding."""
        stop_decision_row = conn.execute(
            """SELECT decision_json FROM policy_state_decisions
               WHERE decision_id=? AND deployment_generation_id=?
                 AND paper_account_environment_id=? AND store_identity=?""",
            (
                stop_row["decision_id"],
                stop_row["deployment_generation_id"],
                stop_row["paper_account_environment_id"],
                self.store_identity,
            ),
        ).fetchone()
        if stop_decision_row is None:
            raise ValueError("protective stop decision identity is missing")
        stop_decision = _decision_from_payload(json.loads(stop_decision_row["decision_json"]))
        source_policy_decision_row = conn.execute(
            """SELECT policy_payload_json FROM policy_state_decisions
               WHERE decision_id=? AND deployment_generation_id=?
                 AND paper_account_environment_id=? AND store_identity=?""",
            (
                stop_decision.decision_id,
                stop_decision.deployment_generation_id,
                stop_decision.deployment_identity.paper_account_environment_id,
                self.store_identity,
            ),
        ).fetchone()
        if source_policy_decision_row is None:
            raise ValueError("protective stop source decision is missing")
        stop_payload = json.loads(source_policy_decision_row["policy_payload_json"])
        if not isinstance(stop_payload, dict):
            raise ValueError("protective stop source decision has malformed policy facts")
        source_action_id = _required_text(
            stop_payload.get("source_action_id"), "protective stop source action id"
        )
        source_action, source_action_row = self._load_action(conn, source_action_id)
        if (
            source_action.decision.deployment_generation_id != stop_decision.deployment_generation_id
            or source_action.decision.deployment_identity.paper_account_environment_id
            != stop_decision.deployment_identity.paper_account_environment_id
            or source_action.decision.deployment_identity.store_identity != self.store_identity
            or source_action.holding_episode_id != str(holding_row["holding_episode_id"])
            or source_action.security_id != str(holding_row["security_id"])
            or source_action.broker_symbol != str(holding_row["broker_symbol"])
            or source_action_row["paper_account_environment_id"]
            != stop_row["paper_account_environment_id"]
            or source_action_row["store_identity"] != self.store_identity
        ):
            raise ValueError("protective stop source action does not match its durable holding")
        if (
            stop_decision.deployment_identity != source_action.decision.deployment_identity
            or stop_decision.clock != source_action.decision.clock
        ):
            raise ValueError("protective stop decision changed its source generation or policy clock")

        facts: dict[str, object] = {
            "origin": "protective_stop",
            "provider_id": provider_id,
            "paper_account_environment_id": str(stop_row["paper_account_environment_id"]),
            "store_identity": self.store_identity,
            "deployment_generation_id": str(stop_row["deployment_generation_id"]),
            "security_id": str(holding_row["security_id"]),
            "symbol": str(holding_row["broker_symbol"]),
            "holding_episode_id": str(holding_row["holding_episode_id"]),
            "source_policy_action_id": source_action.logical_action_id,
            "source_policy_decision_id": source_action.decision.decision_id,
            "source_stop_update_action_id": str(stop_row["stop_update_action_id"]),
            "source_stop_decision_id": stop_decision.decision_id,
            "side": OrderSide.SELL.value,
            "requested_quantity": str(requested_quantity),
            "requested_stop_price": str(Decimal(str(stop_row["requested_stop_price"]))),
            "confirmed_stop_price": str(confirmed_stop_price),
            "client_order_id": client_order_id,
            "broker_order_id": broker_order_id,
        }
        subject_id = "execution-derived:protective-stop:" + _canonical_json(facts)
        decision = DecisionIdentity.build(
            deployment=source_action.decision.deployment_identity,
            clock=source_action.decision.clock,
            snapshot_sha256=source_action.decision.snapshot_sha256,
            category=DecisionCategory.EXIT,
            subject_type=DecisionSubjectType.HOLDING,
            subject_id=subject_id,
        )
        policy_payload: dict[str, object] = {
            "execution_record_kind": _PROTECTIVE_EXECUTION_RECORD_KIND,
            "policy_authored": False,
            "source_policy_action_id": source_action.logical_action_id,
            "source_policy_decision_id": source_action.decision.decision_id,
            "source_stop_update_action_id": str(stop_row["stop_update_action_id"]),
            "source_stop_decision_id": stop_decision.decision_id,
            "physical_order": facts,
        }
        guard_payload: dict[str, object] = {
            "origin": "protective_stop",
            "execution_derived": True,
            "physical_order": facts,
        }
        effective_action_payload: dict[str, object] = {
            "execution_kind": "protective_stop_sell",
            "role": ActionRole.CLOSE.value,
            "side": OrderSide.SELL.value,
            "holding_episode_id": str(holding_row["holding_episode_id"]),
            "physical_order": facts,
        }
        return decision, policy_payload, guard_payload, effective_action_payload

    def _existing_protective_order_contexts_in_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        holding_row: sqlite3.Row,
        provider_id: str,
        broker_order_id: str,
    ) -> list[tuple[DecisionIdentity, dict[str, object], sqlite3.Row]]:
        """Find existing immutable execution provenance for one physical broker order."""
        stop_rows = conn.execute(
            """SELECT * FROM policy_state_stop_updates
               WHERE store_identity=? AND deployment_generation_id=?
                 AND paper_account_environment_id=? AND holding_episode_id=?
                 AND broker_order_id=? AND status='confirmed'
               ORDER BY stop_update_action_id""",
            (
                self.store_identity,
                holding_row["deployment_generation_id"],
                holding_row["paper_account_environment_id"],
                holding_row["holding_episode_id"],
                broker_order_id,
            ),
        ).fetchall()
        contexts_by_decision: dict[
            str, tuple[DecisionIdentity, dict[str, object], sqlite3.Row]
        ] = {}
        for stop_row in stop_rows:
            client = str(stop_row["client_order_id"] or "")
            if not client:
                continue
            try:
                decision, facts, context_holding = (
                    self._load_protective_order_context_in_transaction(
                        conn,
                        stop_row=stop_row,
                        provider_id=provider_id,
                        client_order_id=client,
                        broker_order_id=broker_order_id,
                    )
                )
            except ValueError as exc:
                if "protective STOP order target is missing" in str(exc):
                    # A later confirmation may reuse the order without creating another
                    # execution record. Only the original STOP update owns its context.
                    continue
                if "do not authenticate this STOP order" in str(exc):
                    continue
                raise
            if (
                facts.get("provider_id") != provider_id
                or facts.get("broker_order_id") != broker_order_id
                or context_holding["holding_episode_id"] != holding_row["holding_episode_id"]
            ):
                raise ValueError("protective physical-order identity conflicts with its holding")
            contexts_by_decision[decision.decision_id] = (
                decision,
                facts,
                context_holding,
            )
        return list(contexts_by_decision.values())

    def _load_confirmed_protective_stop_recovery_targets(
        self,
    ) -> tuple[Mapping[str, object], ...]:
        """Return exact current STOP identities for startup watermark recovery."""
        with self._transaction(write=False) as conn:
            self._require_ready(conn)
            holding_rows = conn.execute(
                """SELECT * FROM policy_state_holdings
                   WHERE store_identity=? AND confirmed_stop_action_id IS NOT NULL
                   ORDER BY holding_episode_id""",
                (self.store_identity,),
            ).fetchall()
            targets: list[Mapping[str, object]] = []
            for holding_row in holding_rows:
                holding = _holding_from_row(holding_row)
                if holding.remaining_quantity <= 0:
                    continue
                owner = conn.execute(
                    """SELECT * FROM policy_state_stop_updates
                       WHERE stop_update_action_id=? AND deployment_generation_id=?
                         AND paper_account_environment_id=? AND store_identity=?""",
                    (
                        holding.confirmed_stop_action_id,
                        holding.deployment_generation_id,
                        holding_row["paper_account_environment_id"],
                        self.store_identity,
                    ),
                ).fetchone()
                if owner is None or owner["status"] != "confirmed":
                    raise ValueError("holding startup STOP owner is not a confirmed update")
                if (
                    owner["holding_episode_id"] != holding.holding_episode_id
                    or owner["security_id"] != holding.security_id
                    or owner["client_order_id"] != holding.confirmed_stop_client_order_id
                    or owner["broker_order_id"] != holding.confirmed_stop_broker_order_id
                ):
                    raise ValueError("holding startup STOP references disagree with its owner")

                related_stops = conn.execute(
                    """SELECT * FROM policy_state_stop_updates
                       WHERE store_identity=? AND deployment_generation_id=?
                         AND paper_account_environment_id=? AND holding_episode_id=?
                         AND broker_order_id=? AND status='confirmed'
                       ORDER BY stop_update_action_id""",
                    (
                        self.store_identity,
                        holding.deployment_generation_id,
                        holding_row["paper_account_environment_id"],
                        holding.holding_episode_id,
                        owner["broker_order_id"],
                    ),
                ).fetchall()
                contexts: dict[str, tuple[DecisionIdentity, dict[str, object]]] = {}
                for stop_row in related_stops:
                    stop_client = str(stop_row["client_order_id"] or "")
                    if not stop_client:
                        continue
                    decision_rows = conn.execute(
                        """SELECT policy_payload_json, guard_payload_json,
                                  effective_action_payload_json
                           FROM policy_state_decisions
                           WHERE deployment_generation_id=? AND paper_account_environment_id=?
                             AND store_identity=? AND decision_category=? AND subject_type=?""",
                        (
                            stop_row["deployment_generation_id"],
                            stop_row["paper_account_environment_id"],
                            self.store_identity,
                            DecisionCategory.EXIT.value,
                            DecisionSubjectType.HOLDING.value,
                        ),
                    ).fetchall()
                    providers: set[str] = set()
                    for decision_row in decision_rows:
                        try:
                            policy_payload = json.loads(decision_row["policy_payload_json"])
                            guard_payload = json.loads(decision_row["guard_payload_json"])
                            effective_payload = json.loads(
                                decision_row["effective_action_payload_json"]
                            )
                        except (TypeError, json.JSONDecodeError) as exc:
                            raw = " ".join(str(decision_row[key] or "") for key in decision_row.keys())
                            if "protective_stop" in raw or "protective_stop_sell" in raw:
                                raise ValueError("protective STOP recovery provenance is malformed") from exc
                            continue
                        payloads = (policy_payload, guard_payload, effective_payload)
                        physical = next(
                            (
                                payload.get("physical_order")
                                for payload in payloads
                                if isinstance(payload, dict)
                                and isinstance(payload.get("physical_order"), dict)
                            ),
                            None,
                        )
                        source_matches = (
                            isinstance(policy_payload, dict)
                            and policy_payload.get("source_stop_update_action_id")
                            == stop_row["stop_update_action_id"]
                        )
                        origin_shaped = (
                            isinstance(policy_payload, dict)
                            and policy_payload.get("execution_record_kind")
                            == _PROTECTIVE_EXECUTION_RECORD_KIND
                        ) or (
                            isinstance(guard_payload, dict)
                            and guard_payload.get("origin") == "protective_stop"
                        ) or (
                            isinstance(effective_payload, dict)
                            and effective_payload.get("execution_kind")
                            == "protective_stop_sell"
                        )
                        if source_matches and origin_shaped:
                            if (
                                not isinstance(physical, dict)
                                or not physical.get("provider_id")
                                or physical.get("broker_order_id") != owner["broker_order_id"]
                            ):
                                raise ValueError(
                                    "protective STOP recovery physical-order identity is malformed"
                                )
                            providers.add(str(physical["provider_id"]))
                    for provider in providers:
                        try:
                            decision, facts, _ = (
                                self._load_protective_order_context_in_transaction(
                                    conn,
                                    stop_row=stop_row,
                                    provider_id=provider,
                                    client_order_id=stop_client,
                                    broker_order_id=str(owner["broker_order_id"]),
                                )
                            )
                        except ValueError as exc:
                            if "protective STOP order target is missing" in str(exc):
                                continue
                            raise
                        contexts[decision.decision_id] = (decision, facts)

                if len(contexts) > 1:
                    raise ValueError(
                        "holding startup STOP owner has ambiguous execution provenance"
                    )
                if not contexts:
                    # Legacy confirmed stops have no canonical frozen execution target.
                    # They are not safe candidates for automatic fill recovery.
                    continue
                _, facts = next(iter(contexts.values()))
                if (
                    facts.get("broker_order_id") != owner["broker_order_id"]
                    or facts.get("holding_episode_id") != holding.holding_episode_id
                    or facts.get("deployment_generation_id") != holding.deployment_generation_id
                    or facts.get("security_id") != holding.security_id
                    or facts.get("symbol") != holding.broker_symbol
                ):
                    raise ValueError("startup STOP recovery facts disagree with their holding")
                targets.append(
                    {
                        "provider_id": str(facts["provider_id"]),
                        "logical_action_id": holding.opening_action_id,
                        "holding_episode_id": holding.holding_episode_id,
                        "symbol": holding.broker_symbol,
                        "broker_order_id": str(owner["broker_order_id"]),
                        "client_order_id": str(owner["client_order_id"]),
                        "state_version": int(holding.state_version or 0),
                    }
                )
            return tuple(targets)

    def _ensure_protective_order_decision_in_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        stop_row: sqlite3.Row,
        holding_row: sqlite3.Row,
        provider_id: str,
        client_order_id: str,
        broker_order_id: str,
        requested_quantity: Decimal,
        confirmed_stop_price: Decimal,
    ) -> DecisionIdentity:
        other_holding_order = conn.execute(
            """SELECT stop_update_action_id FROM policy_state_stop_updates
               WHERE store_identity=? AND paper_account_environment_id=?
                 AND broker_order_id=? AND status='confirmed'
                 AND holding_episode_id<>? LIMIT 1""",
            (
                self.store_identity,
                holding_row["paper_account_environment_id"],
                broker_order_id,
                holding_row["holding_episode_id"],
            ),
        ).fetchone()
        if other_holding_order is not None:
            raise IdentityConflictError(
                "broker STOP reference is already associated with another holding"
            )
        existing_contexts = self._existing_protective_order_contexts_in_transaction(
            conn,
            holding_row=holding_row,
            provider_id=provider_id,
            broker_order_id=broker_order_id,
        )
        if len(existing_contexts) > 1:
            raise ValueError(
                "one broker STOP order has multiple durable execution contexts"
            )
        if existing_contexts:
            existing_decision, existing_facts, existing_holding_row = existing_contexts[0]
            if (
                existing_facts.get("client_order_id") != client_order_id
                or existing_facts.get("broker_order_id") != broker_order_id
                or existing_facts.get("holding_episode_id")
                != holding_row["holding_episode_id"]
                or existing_facts.get("security_id") != holding_row["security_id"]
                or existing_facts.get("symbol") != holding_row["broker_symbol"]
                or existing_facts.get("deployment_generation_id")
                != holding_row["deployment_generation_id"]
                or existing_facts.get("paper_account_environment_id")
                != holding_row["paper_account_environment_id"]
                or existing_facts.get("store_identity") != self.store_identity
                or Decimal(str(existing_facts["requested_quantity"])) < requested_quantity
                or confirmed_stop_price
                != Decimal(str(existing_facts["confirmed_stop_price"]))
                or confirmed_stop_price < Decimal(stop_row["requested_stop_price"])
                or existing_holding_row["holding_episode_id"]
                != holding_row["holding_episode_id"]
            ):
                raise IdentityConflictError(
                    "reused protective STOP does not preserve its original physical-order facts"
                )
            return existing_decision

        prior_confirmed_order = conn.execute(
            """SELECT stop_update_action_id FROM policy_state_stop_updates
               WHERE store_identity=? AND deployment_generation_id=?
                 AND paper_account_environment_id=? AND broker_order_id=?
                 AND status='confirmed' LIMIT 1""",
            (
                self.store_identity,
                holding_row["deployment_generation_id"],
                holding_row["paper_account_environment_id"],
                broker_order_id,
            ),
        ).fetchone()
        if prior_confirmed_order is not None:
            raise ValueError(
                "confirmed physical STOP has no immutable target provenance; reconciliation is required"
            )

        # A first confirmation freezes the quantity submitted to the broker. If it no
        # longer equals the durable holding, the caller cannot infer a safe target from
        # this confirmation-time snapshot.
        if requested_quantity != _holding_from_row(holding_row).remaining_quantity:
            raise ValueError(
                "new protective STOP target differs from the durable holding quantity"
            )
        decision, policy_payload, guard_payload, effective_action_payload = (
            self._protective_order_decision_facts(
                conn,
                stop_row=stop_row,
                holding_row=holding_row,
                provider_id=provider_id,
                client_order_id=client_order_id,
                broker_order_id=broker_order_id,
                requested_quantity=requested_quantity,
                confirmed_stop_price=confirmed_stop_price,
            )
        )
        related_rows = conn.execute(
            """SELECT policy_payload_json, guard_payload_json, effective_action_payload_json
               FROM policy_state_decisions
               WHERE deployment_generation_id=? AND paper_account_environment_id=?
                 AND store_identity=? AND decision_category=? AND subject_type=?""",
            (
                decision.deployment_generation_id,
                decision.deployment_identity.paper_account_environment_id,
                self.store_identity,
                DecisionCategory.EXIT.value,
                DecisionSubjectType.HOLDING.value,
            ),
        ).fetchall()
        stop_action_id = str(stop_row["stop_update_action_id"])
        for related in related_rows:
            try:
                related_policy = json.loads(related["policy_payload_json"])
                related_guard = json.loads(related["guard_payload_json"])
                related_effective = json.loads(related["effective_action_payload_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            related_source = (
                related_policy.get("source_stop_update_action_id")
                if isinstance(related_policy, dict)
                else None
            )
            origin_shaped = (
                isinstance(related_policy, dict)
                and related_policy.get("execution_record_kind")
                == _PROTECTIVE_EXECUTION_RECORD_KIND
            ) or (
                isinstance(related_guard, dict)
                and related_guard.get("origin") == "protective_stop"
            ) or (
                isinstance(related_effective, dict)
                and related_effective.get("execution_kind") == "protective_stop_sell"
            )
            if related_source == stop_action_id or origin_shaped and (
                isinstance(related_policy, dict)
                and related_policy.get("source_stop_update_action_id") == stop_action_id
            ):
                if (
                    related_policy != policy_payload
                    or related_guard != guard_payload
                    or related_effective != effective_action_payload
                ):
                    raise IdentityConflictError(
                        "protective physical order facts conflict with their immutable decision"
                    )
        self._record_decision_in_transaction(
            conn,
            decision,
            policy_payload=policy_payload,
            guard_payload=guard_payload,
            effective_action_payload=effective_action_payload,
        )
        return decision

    def confirm_protective_stop(
        self,
        intent: StopUpdateIntent,
        *,
        stop_price: Decimal,
        client_order_id: str,
        broker_order_id: str,
        observed_at: datetime,
        expected_holding_version: int,
        provider_id: str | None = None,
        requested_quantity: Decimal | None = None,
    ) -> HoldingEpisode:
        _aware_iso(observed_at)
        client = _required_text(client_order_id, "client_order_id")
        broker = _required_text(broker_order_id, "broker_order_id")
        provider = None if provider_id is None else _required_text(provider_id, "provider_id")
        if (provider is None) != (requested_quantity is None):
            raise ValueError("provider_id and requested_quantity must be supplied together")
        if requested_quantity is not None and (
            not isinstance(requested_quantity, Decimal)
            or not requested_quantity.is_finite()
            or requested_quantity <= 0
        ):
            raise ValueError("requested_quantity must be a positive finite Decimal")
        with self._transaction(write=True) as conn:
            self._require_ready(conn)
            row = conn.execute(
                "SELECT * FROM policy_state_stop_updates WHERE stop_update_action_id=? AND store_identity=?",
                (intent.logical_action_id, self.store_identity),
            ).fetchone()
            if row is None or row["status"] not in {"proposed", "confirmed"}:
                raise ValueError("stop confirmation must match a recorded proposal")
            holding_row = self._holding_row(conn, intent.holding_episode_id)
            if holding_row is None:
                raise ValueError("stop update holding episode is missing")
            holding = _holding_from_row(holding_row)
            persisted_intent = StopUpdateIntent(
                deployment_generation_id=str(row["deployment_generation_id"]),
                decision_id=str(row["decision_id"]),
                holding_episode_id=str(row["holding_episode_id"]),
                requested_stop_price=Decimal(row["requested_stop_price"]),
            )
            if (
                intent.logical_action_id != persisted_intent.logical_action_id
                or intent.deployment_generation_id != persisted_intent.deployment_generation_id
                or intent.decision_id != persisted_intent.decision_id
                or intent.holding_episode_id != persisted_intent.holding_episode_id
                or intent.requested_stop_price != persisted_intent.requested_stop_price
            ):
                raise IdentityConflictError("stop confirmation intent differs from the durable proposal")
            if row["status"] == "confirmed":
                if (
                    Decimal(row["confirmed_stop_price"]) == stop_price
                    and row["client_order_id"] == client
                    and row["broker_order_id"] == broker
                    and row["observed_at_utc"] == _aware_iso(observed_at)
                ):
                    if provider is not None and requested_quantity is not None:
                        self._ensure_protective_order_decision_in_transaction(
                            conn,
                            stop_row=row,
                            holding_row=holding_row,
                            provider_id=provider,
                            client_order_id=client,
                            broker_order_id=broker,
                            requested_quantity=requested_quantity,
                            confirmed_stop_price=stop_price,
                        )
                    return holding
                raise IdentityConflictError("confirmed protective stop facts cannot change")
            if int(holding_row["state_version"]) != expected_holding_version:
                raise ConcurrentStateUpdateError("holding changed since the caller read it")
            if provider is not None and requested_quantity is not None:
                self._ensure_protective_order_decision_in_transaction(
                    conn,
                    stop_row=row,
                    holding_row=holding_row,
                    provider_id=provider,
                    client_order_id=client,
                    broker_order_id=broker,
                    requested_quantity=requested_quantity,
                    confirmed_stop_price=stop_price,
                )
            updated = confirm_stop_in_state(
                holding,
                intent=intent,
                stop_price=stop_price,
                client_order_id=client,
                broker_order_id=broker,
                observed_at=observed_at,
            )
            conn.execute(
                """UPDATE policy_state_stop_updates SET status='confirmed', client_order_id=?, broker_order_id=?,
                   confirmed_stop_price=?, observed_at_utc=?, state_version=state_version+1
                   WHERE stop_update_action_id=? AND state_version=?""",
                (client, broker, str(stop_price), _aware_iso(observed_at), intent.logical_action_id, int(row["state_version"])),
            )
            self._save_holding(
                conn,
                updated,
                expected_version=expected_holding_version,
                event_kind="stop_update_confirmed",
                observed_at=observed_at,
            )
            return replace(updated, state_version=expected_holding_version + 1)
