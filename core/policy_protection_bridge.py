"""Bridge broker-confirmed cumulative fills into durable policy state."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
import math

from core.order_execution import ProtectiveStopResult
from core.policy_execution_state import (
    ActionRole,
    ActionStateProjection,
    DecisionCategory,
    DecisionIdentity,
    DecisionSubjectType,
    HoldingEpisode,
)
from core.policy_execution_store import PolicyExecutionStateStore


_UNCERTAIN_PROTECTION_ACTIONS = frozenset(
    {
        "position_not_visible",
        "position_sync_pending",
        "submission_unknown",
        "pending_stop_target_conflict",
        "stop_replace_cancel_pending",
        "stop_replace_cancel_failed",
        "submission_intent_persist_failed",
        "pending_buy",
        "unsafe_orders",
        "snapshot_unstable",
        "orders_transitioning",
        "reconciliation_failed",
        "pending_exit",
        "exit_outcome_unresolved",
        "flat_with_open_orders",
    }
)
_DEFINITE_PROTECTION_FAILURE_ACTIONS = frozenset(
    {"submit_failed", "postcondition_failed", "missing_workflow"}
)


def _protection_outcome_flags(action: str) -> tuple[bool, bool]:
    normalized = str(action or "").split(".")[-1].strip().lower()
    if normalized.startswith("policy_"):
        normalized = normalized[len("policy_") :]
    return (
        normalized in _UNCERTAIN_PROTECTION_ACTIONS
        or (
            normalized != "stronger_existing_stop_unadopted"
            and normalized not in _DEFINITE_PROTECTION_FAILURE_ACTIONS
        ),
        normalized == "stronger_existing_stop_unadopted",
    )


class PolicyProtectionBridge:
    """Translate broker fill checkpoints into the accepted #100 store API."""

    def __init__(self, store: PolicyExecutionStateStore, *, provider_id: str) -> None:
        self._store = store
        self._provider_id = provider_id.strip()
        if not self._provider_id:
            raise ValueError("provider_id must be non-empty")

    def record_cumulative_fill(
        self,
        logical_action_id: str,
        *,
        attempt_number: int,
        broker_order_id: str,
        client_order_id: str | None,
        cumulative_quantity: Decimal,
        average_fill_price: Decimal | None,
        cumulative_fees: Decimal | None = None,
        observed_at: datetime | None = None,
    ) -> ActionStateProjection:
        """Record one broker watermark; identical checkpoint replays are idempotent."""
        if not isinstance(cumulative_quantity, Decimal) or not cumulative_quantity.is_finite() or cumulative_quantity < 0:
            raise ValueError("cumulative_quantity must be a finite non-negative Decimal")
        if average_fill_price is not None and (
            not isinstance(average_fill_price, Decimal)
            or not average_fill_price.is_finite()
            or average_fill_price <= 0
        ):
            raise ValueError("average_fill_price must be a positive finite Decimal when known")
        if cumulative_quantity > 0 and average_fill_price is None:
            raise ValueError("positive fills require a broker-confirmed average fill price")
        if cumulative_fees is not None and (
            not isinstance(cumulative_fees, Decimal)
            or not cumulative_fees.is_finite()
            or cumulative_fees < 0
        ):
            raise ValueError("cumulative_fees must be a finite non-negative Decimal when known")
        timestamp = datetime.now(UTC) if observed_at is None else observed_at
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("observed_at must include a timezone")
        broker = broker_order_id.strip()
        client = None if client_order_id is None else client_order_id.strip()
        if not broker or (client_order_id is not None and not client):
            raise ValueError("broker and supplied client order references must be non-empty")

        projection = self._store.load_action_projection(logical_action_id)
        attempt = next(
            (item for item in projection.order_attempts if item.attempt_number == attempt_number),
            None,
        )
        if attempt is None:
            raise ValueError(f"policy action has no attempt {attempt_number}")
        add_client = client if client is not None and client not in attempt.all_client_order_ids else None
        add_broker = broker if broker not in attempt.all_broker_order_ids else None
        if add_client is not None or add_broker is not None:
            projection = self._store.bind_attempt_order_refs(
                logical_action_id,
                attempt_number,
                provider_id=self._provider_id,
                client_order_id=add_client,
                broker_order_id=add_broker,
                expected_action_version=int(projection.state_version or 0),
                observed_at=timestamp,
            )

        cumulative_notional = (
            None
            if average_fill_price is None
            else cumulative_quantity * average_fill_price
        )
        event_payload = {
            "logical_action_id": logical_action_id,
            "attempt_number": attempt_number,
            "broker_order_id": broker,
            "client_order_id": client,
            "cumulative_quantity": str(cumulative_quantity),
            "cumulative_notional": None if cumulative_notional is None else str(cumulative_notional),
            "cumulative_fees": None if cumulative_fees is None else str(cumulative_fees),
        }
        canonical_payload = json.dumps(event_payload, sort_keys=True, separators=(",", ":"))
        event_id = "cumulative-fill:" + hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()

        current = self._store.load_action_projection(logical_action_id)
        holding_version = None
        if current.holding_episode_id is not None:
            holding = self._store.load_holding_episode(current.holding_episode_id)
            holding_version = holding.state_version
        return self._store.record_cumulative_fill(
            logical_action_id,
            attempt_number,
            provider_id=self._provider_id,
            fill_event_id=event_id,
            cumulative_quantity=cumulative_quantity,
            cumulative_notional=cumulative_notional,
            cumulative_fees=cumulative_fees,
            payload_sha256=None,
            observed_at=timestamp,
            expected_action_version=int(current.state_version or 0),
            expected_holding_version=holding_version,
        )

    def record_protective_sell_fill(
        self,
        *,
        broker_order_id: str,
        client_order_id: str,
        cumulative_quantity: Decimal,
        average_fill_price: Decimal | None,
        cumulative_fees: Decimal | None,
        expected_holding_version: int,
        observed_at: datetime | None = None,
    ) -> ActionStateProjection:
        """Record one physical protective STOP watermark through the canonical store path."""
        if (
            not isinstance(cumulative_quantity, Decimal)
            or not cumulative_quantity.is_finite()
            or cumulative_quantity <= 0
        ):
            raise ValueError("protective cumulative quantity must be positive and finite")
        if average_fill_price is not None and (
            not isinstance(average_fill_price, Decimal)
            or not average_fill_price.is_finite()
            or average_fill_price <= 0
        ):
            raise ValueError("average_fill_price must be a positive finite Decimal when known")
        if cumulative_fees is not None and (
            not isinstance(cumulative_fees, Decimal)
            or not cumulative_fees.is_finite()
            or cumulative_fees < 0
        ):
            raise ValueError("cumulative_fees must be a finite non-negative Decimal when known")
        if not isinstance(expected_holding_version, int) or isinstance(
            expected_holding_version, bool
        ) or expected_holding_version < 0:
            raise ValueError("expected_holding_version must be a non-negative integer")
        timestamp = datetime.now(UTC) if observed_at is None else observed_at
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("observed_at must include a timezone")
        broker = broker_order_id.strip()
        client = client_order_id.strip()
        if not broker or not client:
            raise ValueError("broker and client order references must be non-empty")

        cumulative_notional = (
            None
            if average_fill_price is None
            else cumulative_quantity * average_fill_price
        )
        event_payload = {
            "provider_id": self._provider_id,
            "store_identity": self._store.store_identity,
            "broker_order_id": broker,
            "client_order_id": client,
            "cumulative_quantity": str(cumulative_quantity),
            "cumulative_notional": (
                None if cumulative_notional is None else str(cumulative_notional)
            ),
            "cumulative_fees": None if cumulative_fees is None else str(cumulative_fees),
        }
        event_id = "protective-fill:sha256:" + hashlib.sha256(
            json.dumps(event_payload, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        return self._store.record_protective_sell_fill(
            provider_id=self._provider_id,
            broker_order_id=broker,
            client_order_id=client,
            fill_event_id=event_id,
            cumulative_quantity=cumulative_quantity,
            cumulative_notional=cumulative_notional,
            cumulative_fees=cumulative_fees,
            observed_at=timestamp,
            expected_holding_version=expected_holding_version,
        )

    def record_execution_guard_sell_fill(
        self,
        source_action_id: str,
        *,
        workflow_id: str,
        broker_order_id: str,
        client_order_id: str,
        order_type: str,
        execution_cause: str,
        requested_quantity: Decimal,
        cumulative_quantity: Decimal,
        average_fill_price: Decimal,
        expected_holding_version: int,
        observed_at: datetime | None = None,
    ) -> ActionStateProjection:
        """Record a non-STOP SELL with authenticated legacy guard provenance."""
        for value, name in (
            (requested_quantity, "requested_quantity"),
            (cumulative_quantity, "cumulative_quantity"),
            (average_fill_price, "average_fill_price"),
        ):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be a positive finite Decimal")
        timestamp = datetime.now(UTC) if observed_at is None else observed_at
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("observed_at must include a timezone")
        source_id = source_action_id.strip()
        workflow = workflow_id.strip()
        broker = broker_order_id.strip()
        client = client_order_id.strip()
        normalized_type = str(order_type).split(".")[-1].strip().lower()
        cause = execution_cause.strip()
        if not all((source_id, workflow, broker, client, normalized_type, cause)):
            raise ValueError("guard SELL provenance fields must be non-empty")

        event_payload = {
            "provider_id": self._provider_id,
            "store_identity": self._store.store_identity,
            "source_action_id": source_id,
            "workflow_id": workflow,
            "broker_order_id": broker,
            "client_order_id": client,
            "order_type": normalized_type,
            "execution_cause": cause,
            "requested_quantity": str(requested_quantity),
            "cumulative_quantity": str(cumulative_quantity),
            "cumulative_notional": str(cumulative_quantity * average_fill_price),
            "cumulative_fees": None,
        }
        event_id = "guard-sell-fill:sha256:" + hashlib.sha256(
            json.dumps(event_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return self._store.record_execution_guard_sell_fill(
            source_action_id=source_id,
            provider_id=self._provider_id,
            workflow_id=workflow,
            broker_order_id=broker,
            client_order_id=client,
            order_type=normalized_type,
            execution_cause=cause,
            requested_quantity=requested_quantity,
            fill_event_id=event_id,
            cumulative_quantity=cumulative_quantity,
            cumulative_notional=cumulative_quantity * average_fill_price,
            cumulative_fees=None,
            observed_at=timestamp,
            expected_holding_version=expected_holding_version,
        )

    def record_unresolved_execution_sell_fact(
        self,
        source_action_id: str,
        *,
        workflow_id: str,
        broker_order_id: str,
        client_order_id: str,
        order_type: str,
        cumulative_quantity: Decimal,
        average_fill_price: Decimal,
        unresolved_cause: str,
        expected_holding_version: int | None = None,
        observed_at: datetime | None = None,
    ) -> HoldingEpisode:
        """Retain broker SELL facts and block reconciliation when linkage is incomplete."""
        for value, name in (
            (cumulative_quantity, "cumulative_quantity"),
            (average_fill_price, "average_fill_price"),
        ):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be a positive finite Decimal")
        timestamp = datetime.now(UTC) if observed_at is None else observed_at
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("observed_at must include a timezone")
        source_id = source_action_id.strip()
        workflow = workflow_id.strip()
        broker = broker_order_id.strip()
        client = client_order_id.strip()
        normalized_type = str(order_type).split(".")[-1].strip().lower()
        cause = unresolved_cause.strip()
        if not all((source_id, workflow, broker, client, normalized_type, cause)):
            raise ValueError("unresolved SELL facts must be explicit")
        holding = self._store.load_holding_episode_for_action(source_id)
        event_payload = {
            "provider_id": self._provider_id,
            "store_identity": self._store.store_identity,
            "source_action_id": source_id,
            "workflow_id": workflow,
            "broker_order_id": broker,
            "client_order_id": client,
            "order_type": normalized_type,
            "cumulative_quantity": str(cumulative_quantity),
            "average_fill_price": str(average_fill_price),
            "unresolved_cause": cause,
        }
        event_id = "unresolved-sell-fill:sha256:" + hashlib.sha256(
            json.dumps(event_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return self._store.record_unresolved_execution_sell_fact(
            source_action_id=source_id,
            provider_id=self._provider_id,
            workflow_id=workflow,
            broker_order_id=broker,
            client_order_id=client,
            order_type=normalized_type,
            cumulative_quantity=cumulative_quantity,
            average_fill_price=average_fill_price,
            unresolved_cause=cause,
            fill_event_id=event_id,
            observed_at=timestamp,
            expected_holding_version=(
                int(holding.state_version or 0)
                if expected_holding_version is None
                else expected_holding_version
            ),
        )

    def reconcile_protective_stop(
        self,
        logical_action_id: str,
        *,
        workflow: object,
        average_entry_price: Decimal,
        entry_order_id: str | None = None,
        entry_order_ids: set[str] | None = None,
        durable_sell_fill_qty: Decimal = Decimal("0"),
        observed_at: datetime | None = None,
    ) -> ProtectiveStopResult:
        """Persist a stop proposal, reconcile a broker order, then confirm its facts."""
        if not isinstance(average_entry_price, Decimal) or not average_entry_price.is_finite() or average_entry_price <= 0:
            raise ValueError("average_entry_price must be a positive finite Decimal")
        if not isinstance(durable_sell_fill_qty, Decimal) or not durable_sell_fill_qty.is_finite() or durable_sell_fill_qty < 0:
            raise ValueError("durable_sell_fill_qty must be a finite non-negative Decimal")
        timestamp = datetime.now(UTC) if observed_at is None else observed_at
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("observed_at must include a timezone")

        action = self._store.load_action_intent(logical_action_id)
        if action.role is ActionRole.ENTRY and action.holding_episode_id is None:
            raise ValueError("an opening policy action needs a confirmed fill before protection")
        holding_id = action.holding_episode_id
        if holding_id is None:
            raise ValueError("policy action has no durable holding episode")
        holding = self._store.load_holding_episode(holding_id)
        if holding.remaining_quantity <= 0:
            return ProtectiveStopResult(
                success=True,
                order_id="",
                symbol=holding.broker_symbol,
                qty=0.0,
                stop_price=0.0,
                action="flat",
            )

        candidate_targets = [
            target
            for target in (
                action.reservation_stop_price,
                holding.proposed_stop_price,
                holding.confirmed_protective_stop_price,
            )
            if target is not None
        ]
        if not candidate_targets:
            raise ValueError("policy holding has no durable protective-stop target")
        target = max(candidate_targets)

        proposal_pending = (
            holding.proposed_stop_action_id is not None
            and holding.proposed_stop_action_id != holding.confirmed_stop_action_id
        )
        if proposal_pending:
            stop_intent = self._store.load_stop_update_intent(holding.proposed_stop_action_id)
            if stop_intent.requested_stop_price < target:
                return self._unconfirmed_result(
                    holding.broker_symbol,
                    float(holding.remaining_quantity),
                    float(target),
                    action="pending_stop_target_conflict",
                    error="a weaker stop proposal is unresolved and cannot be silently superseded",
                    workflow=workflow,
                )
        else:
            decision = self._stop_reconciliation_decision(
                action.decision,
                holding_id=holding.holding_episode_id,
                source_action_id=action.logical_action_id,
                confirmed_stop_action_id=holding.confirmed_stop_action_id,
                remaining_quantity=holding.remaining_quantity,
                target=target,
            )
            self._store.record_decision(
                decision,
                policy_payload={"source_action_id": action.logical_action_id},
                guard_payload={
                    "reason": "reconcile_recorded_protective_target",
                    "target_stop_price": str(target),
                    "remaining_quantity": str(holding.remaining_quantity),
                },
                effective_action_payload={
                    "holding_episode_id": holding.holding_episode_id,
                    "stop_price": str(target),
                    "source_action_id": action.logical_action_id,
                },
            )
            stop_intent = self._store.propose_stop_update(
                holding.holding_episode_id,
                decision=decision,
                stop_price=target,
                expected_holding_version=holding.state_version,
                observed_at=timestamp,
            )
            holding = self._store.load_holding_episode(holding.holding_episode_id)

        self._record_workflow_event(
            workflow,
            event="policy_stop_update_intent",
            details={
                "logical_action_id": action.logical_action_id,
                "stop_update_action_id": stop_intent.logical_action_id,
                "decision_id": stop_intent.decision_id,
                "holding_episode_id": holding.holding_episode_id,
                "target_stop_price": str(stop_intent.requested_stop_price),
                "remaining_quantity": str(holding.remaining_quantity),
            },
        )

        from core.order_execution import ensure_protective_stop

        stop_order_target_quantity = holding.remaining_quantity
        stop_result = ensure_protective_stop(
            symbol=holding.broker_symbol,
            qty=float(stop_order_target_quantity),
            fill_price=float(average_entry_price),
            workflow_id=str(getattr(workflow, "workflow_id", "")),
            entry_order_id=entry_order_id,
            entry_order_ids=entry_order_ids,
            durable_sell_fill_qty=float(durable_sell_fill_qty),
            stop_price_override=float(stop_intent.requested_stop_price),
        )
        evidence_error = ""
        if not stop_result.success:
            evidence_error = stop_result.error or stop_result.action
        elif not math.isclose(
            float(stop_result.qty),
            float(holding.remaining_quantity),
            rel_tol=0.0,
            abs_tol=0.0001,
        ):
            evidence_error = "broker stop remaining quantity differs from the durable holding quantity"
        elif float(stop_result.stop_price) + 0.0001 < float(stop_intent.requested_stop_price):
            evidence_error = "broker-confirmed stop price is weaker than the durable proposal"
        elif not stop_result.order_id or not stop_result.client_order_id:
            evidence_error = "broker stop evidence is missing client or broker order identity"

        if evidence_error:
            outcome_uncertain, prior_protection_retained = _protection_outcome_flags(
                stop_result.action
            )
            outcome_uncertain = (
                outcome_uncertain
                or bool(getattr(stop_result, "outcome_uncertain", False))
                or stop_result.success
            )
            prior_protection_retained = (
                prior_protection_retained
                or bool(getattr(stop_result, "prior_protection_retained", False))
            )
            if bool(getattr(workflow, "workflow_id", "")):
                workflow.mark_protective_stop(
                    success=False,
                    stop_order_id=stop_result.order_id,
                    stop_price=stop_result.stop_price,
                    action=f"policy_{stop_result.action}",
                    error=evidence_error,
                    stop_client_order_id=stop_result.client_order_id,
                )
            return ProtectiveStopResult(
                success=False,
                order_id=stop_result.order_id,
                symbol=holding.broker_symbol,
                qty=stop_result.qty,
                stop_price=stop_result.stop_price,
                action=f"policy_{stop_result.action}",
                error=evidence_error,
                client_order_id=stop_result.client_order_id,
                outcome_uncertain=outcome_uncertain,
                prior_protection_retained=prior_protection_retained,
            )

        proposed_holding = self._store.load_holding_episode(holding.holding_episode_id)
        confirmed = self._store.confirm_protective_stop(
            stop_intent,
            stop_price=Decimal(str(stop_result.stop_price)),
            client_order_id=stop_result.client_order_id,
            broker_order_id=stop_result.order_id,
            observed_at=timestamp,
            expected_holding_version=proposed_holding.state_version,
            provider_id=self._provider_id,
            requested_quantity=stop_order_target_quantity,
        )
        if bool(getattr(workflow, "workflow_id", "")):
            workflow.mark_protective_stop(
                success=True,
                stop_order_id=stop_result.order_id,
                stop_price=stop_result.stop_price,
                action=f"policy_{stop_result.action}",
                stop_client_order_id=stop_result.client_order_id,
            )
            self._record_workflow_event(
                workflow,
                event="policy_protective_stop_confirmed",
                details={
                    "logical_action_id": action.logical_action_id,
                    "stop_update_action_id": stop_intent.logical_action_id,
                    "holding_episode_id": confirmed.holding_episode_id,
                    "remaining_quantity": str(confirmed.remaining_quantity),
                    "stop_price": str(confirmed.confirmed_protective_stop_price),
                    "client_order_id": stop_result.client_order_id,
                    "broker_order_id": stop_result.order_id,
                },
            )
        return stop_result

    @staticmethod
    def _stop_reconciliation_decision(
        source_decision: DecisionIdentity,
        *,
        holding_id: str,
        source_action_id: str,
        confirmed_stop_action_id: str | None,
        remaining_quantity: Decimal,
        target: Decimal,
    ) -> DecisionIdentity:
        facts = {
            "source_action_id": source_action_id,
            "confirmed_stop_action_id": confirmed_stop_action_id,
            "remaining_quantity": str(remaining_quantity),
            "target_stop_price": str(target),
        }
        digest = hashlib.sha256(
            json.dumps(facts, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        sequence = int(digest[:8], 16) + 1
        return DecisionIdentity.build(
            deployment=source_decision.deployment_identity,
            clock=source_decision.clock,
            snapshot_sha256=source_decision.snapshot_sha256,
            category=DecisionCategory.EXIT,
            subject_type=DecisionSubjectType.HOLDING,
            subject_id=f"{holding_id}:policy-protection",
            sequence=sequence,
        )

    @staticmethod
    def _record_workflow_event(workflow: object, *, event: str, details: dict[str, object]) -> None:
        transition = getattr(workflow, "transition", None)
        state = getattr(workflow, "state", None)
        if callable(transition) and state is not None:
            transition(state, event=event, details=details)

    @staticmethod
    def _unconfirmed_result(
        symbol: str,
        quantity: float,
        stop_price: float,
        *,
        action: str,
        error: str,
        workflow: object,
    ) -> ProtectiveStopResult:
        outcome_uncertain, prior_protection_retained = _protection_outcome_flags(action)
        if bool(getattr(workflow, "workflow_id", "")):
            workflow.mark_protective_stop(
                success=False,
                stop_order_id="",
                stop_price=stop_price,
                action=action,
                error=error,
            )
        return ProtectiveStopResult(
            success=False,
            order_id="",
            symbol=symbol,
            qty=quantity,
            stop_price=stop_price,
            action=action,
            error=error,
            outcome_uncertain=outcome_uncertain,
            prior_protection_retained=prior_protection_retained,
        )
