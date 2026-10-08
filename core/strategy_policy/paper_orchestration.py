"""Selected V3 paper-policy dispatch, guards, durable intent, and handoff."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP
import hashlib
import json
import math
from pathlib import Path
from typing import Callable, Protocol

from config import settings
from core.current_policy_inputs import CurrentFeatureContextSnapshotV1
from core.execution_workflow import EntryExecutionPlan
from core.policy_execution_state import (
    ActionAttemptStatus,
    ActionRole,
    ActionStatus,
    DecisionCategory,
    DecisionClock,
    DecisionIdentity,
    DecisionSubjectType,
    OrderSide,
    PolicyDeploymentIdentity,
    build_action_intent,
)
from core.policy_execution_store import PolicyExecutionStateStore
from core.strategy_policy.account_reconciliation import AccountReconciliation
from core.strategy_policy.contracts import (
    AllocationDecision,
    AllocationSnapshot,
    CapacitySnapshot,
    EntryDecision,
    EntrySnapshot,
)
from core.strategy_policy.contracts_v3 import AllocationSnapshotV3, CapacitySnapshotV3, EntrySnapshotV3
from core.strategy_policy.frozen_bundle import (
    FrozenPolicyBundleDescriptor,
    FrozenPolicyBundleError,
    inspect_frozen_policy_bundle,
    load_frozen_policy_bundle,
)
from core.strategy_policy.runtime_identity import current_paper_runtime_identity


ENTRY_GUARD_PRECEDENCE = (
    "deployment_identity",
    "runtime_identity",
    "execution_profile",
    "execution_readiness",
    "market_hours",
    "account_reconciliation",
    "policy_entry",
    "existing_symbol",
    "capacity",
    "account_cash",
    "portfolio_risk",
    "policy_allocation",
    "broker_precision",
    "action_deduplication",
)
_OPEN_ACTION_STATUSES = frozenset(
    {
        ActionStatus.INTENDED,
        ActionStatus.SUBMITTED,
        ActionStatus.PARTIALLY_FILLED,
        ActionStatus.CANCEL_REQUESTED,
        ActionStatus.RECONCILIATION_REQUIRED,
        ActionStatus.REMAINDER_READY,
        ActionStatus.PARTIAL_INCOMPLETE,
    }
)


class PaperPolicyOrchestrationError(RuntimeError):
    """Selected policy or reconciled inputs cannot safely reach execution."""


class PolicyEntryConsumer(Protocol):
    """The #102 durable action consumer boundary."""

    def submit_policy_entry(
        self,
        plan: EntryExecutionPlan,
        *,
        logical_action_id: str,
        dry_run: bool = False,
        execution_ready: Callable[[], bool] | None = None,
        market_open_check: Callable[[], bool] | None = None,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class PaperPolicyCandidate:
    """Trusted base facts joined to #98 features and one recorded price/precision quote."""

    security_id: str
    symbol: str
    base_snapshot: EntrySnapshot
    reference_price: Decimal
    price_source: str
    tick_size: Decimal
    lot_size: Decimal
    minimum_quantity: Decimal
    is_breakout: bool = False

    def __post_init__(self) -> None:
        if type(self.security_id) is not str or not self.security_id.strip():
            raise ValueError("security_id is required")
        if type(self.symbol) is not str or not self.symbol or self.symbol != self.symbol.upper():
            raise ValueError("symbol must be uppercase")
        if type(self.base_snapshot) is not EntrySnapshot:
            raise TypeError("base_snapshot must be an EntrySnapshot")
        for name in ("reference_price", "tick_size", "lot_size", "minimum_quantity"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be a positive finite Decimal")
        if type(self.price_source) is not str or not self.price_source.strip():
            raise ValueError("price_source is required")
        if type(self.is_breakout) is not bool:
            raise ValueError("is_breakout must be boolean")


@dataclass(frozen=True, slots=True)
class PaperPolicyGuardProfile:
    """Host-owned caps; any permitted override is retained with the decision."""

    guard_id: str
    maximum_positions: int
    maximum_policy_positions: int
    maximum_new_entries_per_cycle: int
    maximum_position_risk_fraction: float
    maximum_stop_distance_fraction: float
    maximum_notional_fraction: float
    maximum_total_risk_fraction: float
    maximum_notional_amount: Decimal | None = None
    allow_capacity_capping: bool = True
    allow_risk_capping: bool = True
    allow_stop_capping: bool = True
    allow_notional_capping: bool = True

    def __post_init__(self) -> None:
        if type(self.guard_id) is not str or not self.guard_id.strip():
            raise ValueError("guard_id is required")
        for name in ("maximum_positions", "maximum_policy_positions", "maximum_new_entries_per_cycle"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.maximum_positions > self.maximum_policy_positions:
            raise ValueError("maximum_positions exceeds the policy-position bound")
        for name in (
            "maximum_position_risk_fraction",
            "maximum_stop_distance_fraction",
            "maximum_notional_fraction",
            "maximum_total_risk_fraction",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                raise ValueError(f"{name} must be a finite fraction in [0, 1]")
        for name in ("allow_capacity_capping", "allow_risk_capping", "allow_stop_capping", "allow_notional_capping"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be boolean")
        if self.maximum_notional_amount is not None and (
            not isinstance(self.maximum_notional_amount, Decimal)
            or not self.maximum_notional_amount.is_finite()
            or self.maximum_notional_amount <= 0
        ):
            raise ValueError("maximum_notional_amount must be positive")

    @property
    def content_addressed_guard_id(self) -> str:
        payload = {
            "maximum_positions": self.maximum_positions,
            "maximum_policy_positions": self.maximum_policy_positions,
            "maximum_new_entries_per_cycle": self.maximum_new_entries_per_cycle,
            "maximum_position_risk_fraction": float(self.maximum_position_risk_fraction).hex(),
            "maximum_stop_distance_fraction": float(self.maximum_stop_distance_fraction).hex(),
            "maximum_notional_fraction": float(self.maximum_notional_fraction).hex(),
            "maximum_total_risk_fraction": float(self.maximum_total_risk_fraction).hex(),
            "maximum_notional_amount": None if self.maximum_notional_amount is None else str(self.maximum_notional_amount),
            "allow_capacity_capping": self.allow_capacity_capping,
            "allow_risk_capping": self.allow_risk_capping,
            "allow_stop_capping": self.allow_stop_capping,
            "allow_notional_capping": self.allow_notional_capping,
        }
        return f"paper-guard:sha256:{hashlib.sha256(_canonical_bytes(payload)).hexdigest()}"

    @property
    def execution_profile_id(self) -> str:
        payload = {"algorithm": "paper-entry-execution-v1", "guard_id": self.content_addressed_guard_id}
        return f"paper-execution-profile:sha256:{hashlib.sha256(_canonical_bytes(payload)).hexdigest()}"


@dataclass(frozen=True, slots=True)
class PaperPolicyCycleResult:
    entered: tuple[str, ...] = ()
    exited: tuple[str, ...] = ()
    ranked_symbols: tuple[str, ...] = ()
    selected_symbols: tuple[str, ...] = ()
    guard_outcomes: tuple[tuple[str, str], ...] = ()
    effective_position_limit: int | None = None


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise PaperPolicyOrchestrationError("decision identity payload is not canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _decimal(value: object, name: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:
        raise PaperPolicyOrchestrationError(f"{name} is invalid") from exc
    if not result.is_finite():
        raise PaperPolicyOrchestrationError(f"{name} is not finite")
    return result


def _round_step(value: Decimal, step: Decimal, rounding: str) -> Decimal:
    return (value / step).to_integral_value(rounding=rounding) * step


def _clock(features: CurrentFeatureContextSnapshotV1) -> DecisionClock:
    value = features.decision_clock
    return DecisionClock(
        exchange_id=value.completion_evidence.exchange_timezone,
        decision_session=value.completed_session,
        as_of_cutoff_at=value.as_of_cutoff,
        next_execution_session=value.next_eligible_session,
        account_valuation_session=value.next_eligible_session,
        account_valuation_at=value.valuation_time,
    )


def _decision(
    deployment: PolicyDeploymentIdentity,
    clock: DecisionClock,
    snapshot: object,
    category: DecisionCategory,
    subject_type: DecisionSubjectType,
    subject_id: str,
) -> DecisionIdentity:
    return DecisionIdentity.build(
        deployment=deployment,
        clock=clock,
        snapshot_sha256=_digest(snapshot),
        category=category,
        subject_type=subject_type,
        subject_id=subject_id,
    )


def _feature_value(features: CurrentFeatureContextSnapshotV1, symbol: str, name: str) -> object:
    if name.startswith("market."):
        return getattr(features.market_context, name.removeprefix("market."), None)
    row = features.entry_features.get(symbol)
    return None if row is None else getattr(row, name, None)


class PaperPolicyOrchestrator:
    """Fail-closed V3 paper entry path over persisted #100 identity and #98/#99 facts."""

    def __init__(
        self,
        *,
        policy_store: PolicyExecutionStateStore,
        bundle_path: str | Path,
        paper_account_environment_id: str,
        feature_context: CurrentFeatureContextSnapshotV1,
        account: AccountReconciliation,
        candidates: tuple[PaperPolicyCandidate, ...],
        guard_profile: PaperPolicyGuardProfile,
        order_manager: PolicyEntryConsumer,
        open_position_count: int,
        active_symbols: tuple[str, ...],
    ) -> None:
        if type(feature_context) is not CurrentFeatureContextSnapshotV1:
            raise TypeError("feature_context must be a CurrentFeatureContextSnapshotV1")
        if type(account) is not AccountReconciliation:
            raise TypeError("account must be the reconciled #99 AccountReconciliation value")
        if type(candidates) is not tuple or any(type(item) is not PaperPolicyCandidate for item in candidates):
            raise TypeError("candidates must be a tuple of PaperPolicyCandidate values")
        if type(guard_profile) is not PaperPolicyGuardProfile:
            raise TypeError("guard_profile must be a PaperPolicyGuardProfile")
        if type(paper_account_environment_id) is not str or not paper_account_environment_id.strip():
            raise ValueError("paper_account_environment_id is required")
        if type(open_position_count) is not int or open_position_count < 0:
            raise ValueError("open_position_count must be a non-negative integer")
        if type(active_symbols) is not tuple or any(type(item) is not str for item in active_symbols):
            raise TypeError("active_symbols must be a tuple of reconciled symbols")
        if len(active_symbols) != len(set(active_symbols)):
            raise ValueError("active_symbols cannot contain duplicates")
        if not callable(getattr(order_manager, "submit_policy_entry", None)):
            raise TypeError("order_manager must implement submit_policy_entry")
        self.policy_store = policy_store
        self.bundle_path = Path(bundle_path)
        self.paper_account_environment_id = paper_account_environment_id
        self.feature_context = feature_context
        self.account = account
        self.candidates = candidates
        self.guard_profile = guard_profile
        self.order_manager = order_manager
        self.open_position_count = open_position_count
        self.active_symbols = frozenset(active_symbols)

    def run(
        self,
        *,
        dry_run: bool = False,
        skip_entries: bool = False,
        skip_exits: bool = False,
        execution_ready: Callable[[], bool] | None = None,
        market_open_check: Callable[[], bool] | None = None,
        market_hours_required: bool = False,
    ) -> PaperPolicyCycleResult:
        if any(type(flag) is not bool for flag in (dry_run, skip_entries, skip_exits)):
            raise ValueError("cycle flags must be booleans")
        if type(market_hours_required) is not bool:
            raise ValueError("market_hours_required must be boolean")
        if skip_entries:
            return PaperPolicyCycleResult()
        # There is intentionally no scanner or generic exit fallback on this route.
        del skip_exits
        clock = _clock(self.feature_context)
        deployment, descriptor = self._selected_policy(clock)
        if not dry_run:
            required_market_hours = bool(settings.ENTRY_MARKET_HOURS_ONLY or market_hours_required)
            veto_reason = self._host_entry_veto(
                execution_ready=execution_ready,
                market_open_check=market_open_check,
                market_hours_required=required_market_hours,
            )
            if veto_reason is not None:
                self._record_host_veto(deployment, clock, veto_reason)
                if veto_reason in {
                    "execution_readiness_callback_missing",
                    "execution_readiness_check_failed",
                    "market_hours_callback_missing",
                    "market_hours_check_failed",
                }:
                    raise PaperPolicyOrchestrationError(veto_reason.replace("_", " "))
                return PaperPolicyCycleResult(guard_outcomes=(("HOST", veto_reason),))
            recoveries = self._pending_entry_recoveries(deployment)
            if recoveries is not None:
                pending_actions, blocked = recoveries
                if blocked:
                    return PaperPolicyCycleResult(
                        selected_symbols=tuple(symbol for symbol, _ in blocked),
                        guard_outcomes=tuple(blocked),
                    )
                entered: list[str] = []
                outcomes: list[tuple[str, str]] = []
                for projection, plan in pending_actions:
                    symbol = projection.broker_symbol
                    result = self.order_manager.submit_policy_entry(
                        plan,
                        logical_action_id=projection.logical_action_id,
                        dry_run=False,
                        execution_ready=execution_ready,
                        market_open_check=market_open_check,
                    )
                    if not bool(getattr(result, "success", False)):
                        outcomes.append((symbol, "consumer_not_accepted_uncertain_action"))
                        break
                    entered.append(symbol)
                    outcomes.append((symbol, "recovered_consumer_accepted"))
                return PaperPolicyCycleResult(
                    entered=tuple(entered),
                    selected_symbols=tuple(projection.broker_symbol for projection, _ in pending_actions),
                    guard_outcomes=tuple(outcomes),
                )
        self._validate_inputs(deployment, descriptor)
        required_actions = {"entry", "capacity", "allocation"}
        if not required_actions.issubset(set(descriptor.capabilities["supported_actions"])):
            raise PaperPolicyOrchestrationError("selected policy lacks required entry capabilities")

        try:
            bundle = load_frozen_policy_bundle(self.bundle_path, expected_identity=deployment)
        except FrozenPolicyBundleError as exc:
            self._record_host_veto(deployment, clock, "policy_bundle_changed_before_load")
            raise PaperPolicyOrchestrationError("selected policy artifact failed identity validation") from exc

        rows = self._evaluate_entries(bundle)
        capacity_snapshot = self._capacity_snapshot(rows)
        capacity_decision = bundle.client.recommend_capacity(capacity_snapshot)
        capacity_identity, effective_limit, capacity_veto, capacity_guard = self._capacity_decision(
            deployment, clock, capacity_snapshot, capacity_decision, rows
        )
        eligible = [row for row in rows if row["decision"].qualified and row["decision"].market_permitted]
        ranked = sorted(
            eligible,
            key=lambda row: (
                -float("-inf" if row["decision"].rank[0] is None else row["decision"].rank[0]),
                -float("-inf" if row["decision"].rank[1] is None else row["decision"].rank[1]),
                row["candidate"].security_id,
            ),
        )
        not_held = [row for row in ranked if row["candidate"].symbol not in self.active_symbols]
        pending = int(self.account.pending_entry_count or 0)
        slots = max(0, effective_limit - self.open_position_count - pending)
        slots = min(slots, self.guard_profile.maximum_new_entries_per_cycle)
        selected = [] if capacity_veto else not_held[:slots]
        selected_security_ids = {row["candidate"].security_id for row in selected}

        self._record_entry_decisions(deployment, clock, rows, ranked, selected_security_ids, capacity_veto)
        self.policy_store.record_decision(
            capacity_identity,
            policy_payload={
                "capacity_snapshot": capacity_snapshot.to_primitive(),
                "capacity_decision": capacity_decision.to_primitive(),
            },
            guard_payload=capacity_guard,
            effective_action_payload={"maximum_positions": effective_limit, "new_entry_slots": slots},
        )

        plans, allocation_records, outcomes = self._allocate(deployment, bundle, clock, selected, effective_limit)
        for identity, policy, guard, effective in allocation_records:
            self.policy_store.record_decision(
                identity,
                policy_payload=policy,
                guard_payload=guard,
                effective_action_payload=effective,
            )

        entered: list[str] = []
        for row, plan, allocation_identity in plans:
            candidate = row["candidate"]
            if dry_run:
                outcomes.append((candidate.symbol, "dry_run_no_action_intent"))
                continue
            intent = build_action_intent(
                decision=allocation_identity,
                security_id=candidate.security_id,
                broker_symbol=candidate.symbol,
                role=ActionRole.ENTRY,
                side=OrderSide.BUY,
                requested_quantity=_decimal(plan.qty, "entry quantity"),
                status=ActionStatus.INTENDED,
                reservation_price=_decimal(plan.entry_price, "entry reference price"),
                reservation_price_basis="policy_entry_reference_price",
                reservation_stop_price=_decimal(plan.stop_price, "policy stop price"),
                risk_per_unit=_decimal(plan.risk_per_share, "policy risk per share"),
                risk_basis="policy_stop_distance_after_broker_precision",
            )
            try:
                projection = self.policy_store.load_action_projection(intent.logical_action_id)
            except KeyError:
                self.policy_store.record_action_intent(intent, expected_version=None)
                projection = None
            if projection is not None and projection.status not in _OPEN_ACTION_STATUSES:
                outcomes.append((candidate.symbol, "terminal_logical_action_blocks_resubmission"))
                continue
            result = self.order_manager.submit_policy_entry(
                plan,
                logical_action_id=intent.logical_action_id,
                dry_run=False,
                execution_ready=execution_ready,
                market_open_check=market_open_check,
            )
            if not bool(getattr(result, "success", False)):
                outcomes.append((candidate.symbol, "consumer_not_accepted_uncertain_action"))
                break
            entered.append(candidate.symbol)
            outcomes.append((candidate.symbol, "consumer_accepted"))

        return PaperPolicyCycleResult(
            entered=tuple(entered),
            ranked_symbols=tuple(row["candidate"].symbol for row in ranked),
            selected_symbols=tuple(row["candidate"].symbol for row in selected),
            guard_outcomes=tuple(outcomes),
            effective_position_limit=effective_limit,
        )

    def _pending_entry_recoveries(
        self, deployment: PolicyDeploymentIdentity
    ) -> tuple[list[tuple[object, EntryExecutionPlan]], list[tuple[str, str]]] | None:
        """Resume authenticated, never-handed-off entry intents before fresh selection."""
        account = self.account
        if not account.ready or any(
            value is None
            for value in (
                account.equity,
                account.settled_cash,
                account.available_cash,
                account.gross_exposure,
                account.open_position_risk,
                account.total_committed_risk,
                account.pending_entry_count,
                account.portfolio_features,
            )
        ):
            raise PaperPolicyOrchestrationError("reconciled account is not ready for pending-entry recovery")
        feature_clock = self.feature_context.decision_clock
        if (
            feature_clock.completed_session != account.completed_session
            or feature_clock.as_of_cutoff != account.as_of_cutoff
            or feature_clock.next_eligible_session != account.next_execution_session
            or feature_clock.valuation_time != account.valuation_time
        ):
            raise PaperPolicyOrchestrationError("#98 feature and #99 account clocks differ during pending-entry recovery")
        if account.equity <= 0 or account.available_cash < 0 or account.settled_cash < 0:
            raise PaperPolicyOrchestrationError("reconciled account sizing facts are invalid during pending-entry recovery")
        if account.portfolio_features.pending_entry_count != account.pending_entry_count:
            raise PaperPolicyOrchestrationError("portfolio pending-entry count differs from account reconciliation")

        try:
            snapshot = self.policy_store.load_policy_execution_snapshot(
                deployment_generation_id=deployment.deployment_generation_id,
                portfolio_snapshot_id=account.snapshot_id,
            )
        except Exception as exc:
            raise PaperPolicyOrchestrationError(
                "persisted account snapshot is unavailable for safe pending-entry recovery"
            ) from exc
        if snapshot.deployment_identity != deployment:
            raise PaperPolicyOrchestrationError("pending-entry snapshot belongs to a different active deployment")
        portfolio = snapshot.portfolio_snapshot
        if (
            portfolio.portfolio_snapshot_id != account.snapshot_id
            or portfolio.deployment_identity != deployment
            or portfolio.clock != _clock(self.feature_context)
            or portfolio.equity != _decimal(account.equity, "equity")
            or portfolio.cash != _decimal(account.settled_cash, "settled cash")
            or portfolio.gross_exposure != _decimal(account.gross_exposure, "gross exposure")
            or portfolio.open_risk != _decimal(account.open_position_risk, "open position risk")
        ):
            raise PaperPolicyOrchestrationError("pending-entry snapshot differs from reconciled account facts")

        active_generation = deployment.deployment_generation_id
        projections = tuple(
            projection
            for projection in snapshot.action_projections
            if projection.deployment_generation_id == active_generation
            and projection.role is ActionRole.ENTRY
            and projection.side is OrderSide.BUY
            and projection.status in _OPEN_ACTION_STATUSES
        )
        decision_session = _clock(self.feature_context).decision_session
        prior_session_open = tuple(
            projection
            for projection in projections
            if projection.decision_session != decision_session
        )
        if prior_session_open:
            return [], [
                (projection.broker_symbol, "pending_entry_requires_reconciliation")
                for projection in prior_session_open
            ]

        intended = tuple(projection for projection in projections if projection.status is ActionStatus.INTENDED)
        same_session_progressed = tuple(
            projection
            for projection in projections
            if projection.status is not ActionStatus.INTENDED
            and projection.decision_session == decision_session
        )
        if same_session_progressed:
            for projection in same_session_progressed:
                self._load_original_entry_plan(projection, deployment)
            submitted_matches_account = self._submitted_actions_match_reconciliation(
                same_session_progressed, snapshot, deployment
            )
            reason = (
                "pending_entry_already_submitted"
                if submitted_matches_account
                else "pending_entry_requires_reconciliation"
            )
            blocked = [(projection.broker_symbol, reason) for projection in same_session_progressed]
            blocked.extend(
                (projection.broker_symbol, "pending_entry_waits_for_existing_action")
                for projection in intended
            )
            return [], blocked
        if not intended:
            return None

        held_security_ids = {
            holding.security_id
            for holding in snapshot.holding_episodes
            if holding.remaining_quantity > 0
        }
        held_symbols = {
            holding.broker_symbol or holding.symbol
            for holding in snapshot.holding_episodes
            if holding.remaining_quantity > 0
        }
        by_security: dict[str, list[object]] = {}
        by_symbol: dict[str, list[object]] = {}
        for projection in projections:
            by_security.setdefault(projection.security_id, []).append(projection)
            by_symbol.setdefault(projection.broker_symbol, []).append(projection)

        blocked: list[tuple[str, str]] = []
        eligible = []
        for projection in intended:
            attempts = projection.order_attempts
            current_attempt = attempts[0] if len(attempts) == 1 else None
            has_unsubmitted_initial_attempt = (
                current_attempt is not None
                and current_attempt.attempt_number == 1
                and current_attempt.status is ActionAttemptStatus.INTENDED
                and current_attempt.terminal_status is None
                and current_attempt.requested_quantity == projection.requested_quantity
                and current_attempt.confirmed_filled_quantity == 0
                and current_attempt.client_order_id is None
                and current_attempt.broker_order_id is None
                and not current_attempt.client_order_aliases
                and not current_attempt.broker_order_aliases
                and not projection.provider_order_references
            )
            if not has_unsubmitted_initial_attempt:
                blocked.append((projection.broker_symbol, "pending_entry_requires_reconciliation"))
            elif (
                projection.confirmed_quantity != 0
                or projection.residual_quantity != projection.requested_quantity
                or projection.holding_episode_id is not None
            ):
                blocked.append((projection.broker_symbol, "pending_entry_requires_reconciliation"))
            elif projection.security_id in held_security_ids or projection.broker_symbol in held_symbols:
                blocked.append((projection.broker_symbol, "pending_entry_conflicts_with_reconciled_holding"))
            elif len(by_security.get(projection.security_id, ())) != 1 or len(by_symbol.get(projection.broker_symbol, ())) != 1:
                blocked.append((projection.broker_symbol, "multiple_open_actions_require_reconciliation"))
            elif (
                projection.decision_category is not DecisionCategory.ALLOCATION
                or projection.decision_subject_type is not DecisionSubjectType.SECURITY
                or projection.decision_subject_id != projection.security_id
                or projection.reservation_price is None
                or projection.reservation_stop_price is None
                or projection.risk_per_unit is None
                or projection.reservation_price_basis is None
                or projection.risk_basis is None
            ):
                blocked.append((projection.broker_symbol, "pending_entry_identity_requires_reconciliation"))
            else:
                eligible.append(projection)

        if blocked:
            return [], blocked
        return ([(projection, self._load_original_entry_plan(projection, deployment)) for projection in eligible], [])

    def _submitted_actions_match_reconciliation(
        self,
        projections: tuple[object, ...],
        snapshot: object,
        deployment: PolicyDeploymentIdentity,
    ) -> bool:
        """Recognize already-submitted same-session buys without handing them off again."""
        account = self.account
        if account.pending_entry_count < len(projections):
            return False
        if account.reserved_buy_cash is None or account.reserved_buy_risk is None:
            return False
        holding_security_ids = {
            holding.security_id
            for holding in snapshot.holding_episodes
            if holding.remaining_quantity > 0
        }
        holding_symbols = {
            holding.broker_symbol or holding.symbol
            for holding in snapshot.holding_episodes
            if holding.remaining_quantity > 0
        }
        required_cash = Decimal("0")
        required_risk = Decimal("0")
        for projection in projections:
            if (
                projection.status is not ActionStatus.SUBMITTED
                or projection.broker_symbol not in self.active_symbols
                or projection.security_id in holding_security_ids
                or projection.broker_symbol in holding_symbols
                or projection.reservation_amount is None
                or projection.residual_committed_risk is None
                or projection.residual_quantity <= 0
                or projection.reservation_price is None
                or projection.risk_per_unit is None
                or len(projection.order_attempts) == 0
            ):
                return False
            active_attempts = tuple(
                attempt
                for attempt in projection.order_attempts
                if attempt.status in {
                    ActionAttemptStatus.SUBMITTED,
                    ActionAttemptStatus.PARTIALLY_FILLED,
                    ActionAttemptStatus.CANCEL_REQUESTED,
                    ActionAttemptStatus.RECONCILIATION_REQUIRED,
                }
            )
            if len(active_attempts) != 1:
                return False
            attempt = active_attempts[0]
            if (
                attempt.status is not ActionAttemptStatus.SUBMITTED
                or attempt.terminal_status is not None
                or attempt.requested_quantity != projection.residual_quantity
                or attempt.confirmed_filled_quantity != 0
                or attempt.client_order_id is None
                or attempt.broker_order_id is None
            ):
                return False
            references = {
                (reference.attempt_number, reference.reference_kind, reference.external_order_id)
                for reference in projection.provider_order_references
                if reference.paper_account_environment_id == deployment.paper_account_environment_id
                and reference.store_identity == deployment.store_identity
            }
            if (
                (attempt.attempt_number, "client_order_id", attempt.client_order_id) not in references
                or (attempt.attempt_number, "broker_order_id", attempt.broker_order_id) not in references
            ):
                return False
            required_cash += projection.reservation_amount
            required_risk += projection.residual_committed_risk

        epsilon = _decimal(account.equity, "equity") * Decimal(str(math.ulp(1.0)))
        return (
            _decimal(account.reserved_buy_cash, "reserved buy cash") + epsilon >= required_cash
            and _decimal(account.reserved_buy_risk, "reserved buy risk") + epsilon >= required_risk
        )

    def _load_original_entry_plan(
        self, projection: object, deployment: PolicyDeploymentIdentity
    ) -> EntryExecutionPlan:
        """Read and authenticate the immutable plan associated with one action."""
        try:
            with self.policy_store._transaction(write=False) as connection:
                row = connection.execute(
                    """SELECT decision_slot_id, deployment_generation_id, paper_account_environment_id,
                              store_identity, decision_category, subject_type, subject_id,
                              decision_json, effective_action_payload_json
                       FROM policy_state_decisions WHERE decision_id=?""",
                    (projection.decision_id,),
                ).fetchone()
            if row is None:
                raise ValueError("allocation decision was not found")
            decision_payload = json.loads(row["decision_json"])
            clock_payload = decision_payload["clock"]
            persisted_identity = decision_payload["deployment_identity"]
            if persisted_identity != deployment.identity_payload():
                raise ValueError("allocation decision deployment identity differs")
            decision = DecisionIdentity.build(
                deployment=deployment,
                clock=DecisionClock(
                    exchange_id=str(clock_payload["exchange_id"]),
                    decision_session=date.fromisoformat(str(clock_payload["decision_session"])),
                    as_of_cutoff_at=datetime.fromisoformat(str(clock_payload["as_of_cutoff_at"])),
                    next_execution_session=date.fromisoformat(str(clock_payload["next_execution_session"])),
                    account_valuation_session=date.fromisoformat(str(clock_payload["account_valuation_session"])),
                    account_valuation_at=datetime.fromisoformat(str(clock_payload["account_valuation_at"])),
                ),
                snapshot_sha256=str(decision_payload["snapshot_sha256"]),
                category=str(decision_payload["decision_category"]),
                subject_type=str(decision_payload["subject_type"]),
                subject_id=str(decision_payload["subject_id"]),
                sequence=None if decision_payload.get("sequence") is None else int(decision_payload["sequence"]),
            )
            if (
                decision.decision_id != projection.decision_id
                or decision.decision_slot_id != projection.decision_slot_id
                or decision_payload.get("decision_id") != decision.decision_id
                or decision_payload.get("decision_slot_id") != decision.decision_slot_id
                or decision_payload.get("deployment_generation_id") != deployment.deployment_generation_id
                or row["decision_slot_id"] != decision.decision_slot_id
                or row["deployment_generation_id"] != deployment.deployment_generation_id
                or row["paper_account_environment_id"] != deployment.paper_account_environment_id
                or row["store_identity"] != deployment.store_identity
                or row["decision_category"] != DecisionCategory.ALLOCATION.value
                or row["subject_type"] != DecisionSubjectType.SECURITY.value
                or row["subject_id"] != projection.security_id
                or decision.subject_type is not DecisionSubjectType.SECURITY
                or decision.category is not DecisionCategory.ALLOCATION
                or decision.subject_id != projection.security_id
            ):
                raise ValueError("allocation decision identity differs from pending action")

            payload = json.loads(row["effective_action_payload_json"])
            plan_payload = payload["entry_plan"]
            if not isinstance(plan_payload, dict):
                raise ValueError("original entry plan is missing")
            if "order_type" in plan_payload and plan_payload["order_type"] != "limit":
                raise ValueError("original entry plan has an unsupported fixed order type")
            symbol = plan_payload["symbol"]
            price_source = plan_payload["price_source"]
            if type(symbol) is not str or not symbol or type(price_source) is not str or not price_source:
                raise ValueError("original entry plan identity fields are invalid")
            plan = EntryExecutionPlan(
                symbol=symbol,
                entry_price=self._plan_number(plan_payload, "entry_price"),
                price_source=price_source,
                stop_price=self._plan_number(plan_payload, "stop_price"),
                stop_loss_pct=self._plan_number(plan_payload, "stop_loss_pct"),
                position_value=self._plan_number(plan_payload, "notional"),
                risk_amount=self._plan_number(plan_payload, "risk_amount"),
                risk_per_share=self._plan_number(plan_payload, "risk_per_share"),
                qty=self._plan_number(plan_payload, "quantity"),
                canslim_score=self._plan_number(plan_payload, "canslim_score"),
                rs_score=self._plan_number(plan_payload, "rs_score"),
                is_breakout=plan_payload["is_breakout"],
                has_volume_surge=plan_payload["has_volume_surge"],
            )
            if type(plan.is_breakout) is not bool or type(plan.has_volume_surge) is not bool:
                raise ValueError("original plan boolean fields are invalid")
            if (
                plan.symbol != projection.broker_symbol
                or _decimal(plan.qty, "entry quantity") != projection.requested_quantity
                or _decimal(plan.entry_price, "entry price") != projection.reservation_price
                or _decimal(plan.stop_price, "stop price") != projection.reservation_stop_price
                or _decimal(plan.risk_per_share, "risk per share") != projection.risk_per_unit
                or _decimal(plan.position_value, "position value") != projection.reservation_amount
                or _decimal(plan.risk_amount, "risk amount")
                != projection.requested_quantity * projection.risk_per_unit
                or _decimal(plan.entry_price, "entry price") - _decimal(plan.stop_price, "stop price")
                != _decimal(plan.risk_per_share, "risk per share")
                or _decimal(plan.qty, "entry quantity") * _decimal(plan.entry_price, "entry price")
                != _decimal(plan.position_value, "position value")
                or _decimal(plan.qty, "entry quantity") * _decimal(plan.risk_per_share, "risk per share")
                != _decimal(plan.risk_amount, "risk amount")
                or not 0 < plan.stop_loss_pct < 1
                or not math.isclose(
                    plan.stop_loss_pct,
                    plan.risk_per_share / plan.entry_price,
                    rel_tol=1e-12,
                    abs_tol=1e-12,
                )
            ):
                raise ValueError("original entry plan differs from its durable action reservation")
            expected_intent = build_action_intent(
                decision=decision,
                security_id=projection.security_id,
                broker_symbol=projection.broker_symbol,
                role=ActionRole.ENTRY,
                side=OrderSide.BUY,
                requested_quantity=projection.requested_quantity,
                status=ActionStatus.INTENDED,
                reservation_price=projection.reservation_price,
                reservation_price_basis=projection.reservation_price_basis,
                reservation_stop_price=projection.reservation_stop_price,
                risk_per_unit=projection.risk_per_unit,
                risk_basis=projection.risk_basis,
            )
            if expected_intent.logical_action_id != projection.logical_action_id:
                raise ValueError("pending action identity cannot be reproduced")
            return plan
        except Exception as exc:
            raise PaperPolicyOrchestrationError(
                "persisted pending-entry plan failed authentication"
            ) from exc

    @staticmethod
    def _plan_number(payload: dict[str, object], name: str) -> float:
        value = payload[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError(f"original entry plan field {name} is invalid")
        return float(value)

    def _selected_policy(self, clock: DecisionClock) -> tuple[PolicyDeploymentIdentity, FrozenPolicyBundleDescriptor]:
        pointer = self.policy_store.load_active_generation_pointer(self.paper_account_environment_id)
        if pointer.active_generation_id is None:
            raise PaperPolicyOrchestrationError("no active policy generation is selected")
        if pointer.readiness_state.startswith("degraded"):
            raise PaperPolicyOrchestrationError("active policy generation is degraded")
        chain = self.policy_store.load_deployment_chain(pointer.active_generation_id)
        if not chain.active_for_account or chain.lifecycle != "active":
            raise PaperPolicyOrchestrationError("active policy generation changed or is not active")
        identity = chain.deployment_identity
        if identity.store_identity != self.policy_store.store_identity:
            raise PaperPolicyOrchestrationError("active policy store identity differs")
        if identity.paper_account_environment_id != self.paper_account_environment_id:
            raise PaperPolicyOrchestrationError("active policy account identity differs")
        if (
            self.guard_profile.guard_id != self.guard_profile.content_addressed_guard_id
            or identity.execution_profile_id != self.guard_profile.execution_profile_id
            or chain.guard_id != self.guard_profile.content_addressed_guard_id
        ):
            self._record_host_veto(identity, clock, "execution_profile_identity_mismatch")
            raise PaperPolicyOrchestrationError("active policy guard profile differs")
        if chain.handler_identity not in {identity.policy_artifact_id, f"handler:{identity.policy_artifact_id}"}:
            raise PaperPolicyOrchestrationError("active policy handler identity differs")
        try:
            descriptor = inspect_frozen_policy_bundle(self.bundle_path)
        except FrozenPolicyBundleError as exc:
            self._record_host_veto(identity, clock, "policy_bundle_identity_unavailable")
            raise PaperPolicyOrchestrationError("selected policy artifact failed identity validation") from exc
        if (
            descriptor.policy_artifact_id != identity.policy_artifact_id
            or descriptor.capability_manifest_id != identity.capability_manifest_id
            or descriptor.interface_version != identity.policy_interface_version
            or descriptor.feature_contract_id != identity.feature_contract_id
            or descriptor.feature_calculator_identity != identity.feature_calculator_id
            or descriptor.runtime_identity != identity.runtime_identity
            or identity.runtime_identity != current_paper_runtime_identity()
        ):
            self._record_host_veto(identity, clock, "deployment_runtime_or_policy_identity_mismatch")
            raise PaperPolicyOrchestrationError("selected policy artifact failed identity validation")
        return identity, descriptor

    def _host_entry_veto(
        self,
        *,
        execution_ready: Callable[[], bool] | None,
        market_open_check: Callable[[], bool] | None,
        market_hours_required: bool,
    ) -> str | None:
        if execution_ready is None:
            return "execution_readiness_callback_missing"
        try:
            if not bool(execution_ready()):
                return "execution_not_ready"
        except Exception:
            return "execution_readiness_check_failed"
        if market_hours_required:
            if market_open_check is None:
                return "market_hours_callback_missing"
            try:
                if not bool(market_open_check()):
                    return "market_closed"
            except Exception:
                return "market_hours_check_failed"
        return None

    def _record_host_veto(self, deployment: PolicyDeploymentIdentity, clock: DecisionClock, reason: str) -> None:
        snapshot = {
            "reason": reason,
            "runtime_identity": deployment.runtime_identity,
            "execution_profile_id": deployment.execution_profile_id,
            "guard_id": self.guard_profile.content_addressed_guard_id,
            "feature_manifest": self.feature_context.recorded_input_manifest_sha256,
            "account_snapshot": self.account.snapshot_id,
        }
        identity = _decision(
            deployment,
            clock,
            snapshot,
            DecisionCategory.CANDIDATE_EVALUATION,
            DecisionSubjectType.PORTFOLIO,
            f"paper-host-guard:{reason}",
        )
        self.policy_store.record_decision(
            identity,
            policy_payload={"host_control_check": reason},
            guard_payload={
                "outcome": "veto",
                "veto_reason": reason,
                "precedence": list(ENTRY_GUARD_PRECEDENCE),
                "runtime_identity": deployment.runtime_identity,
                "execution_profile_id": deployment.execution_profile_id,
                "guard_id": self.guard_profile.content_addressed_guard_id,
            },
            effective_action_payload={"action": "none"},
        )

    def _validate_inputs(self, deployment, bundle_descriptor) -> None:
        feature_clock = self.feature_context.decision_clock
        account = self.account
        required_account = (
            account.equity,
            account.settled_cash,
            account.available_cash,
            account.gross_exposure,
            account.open_position_risk,
            account.total_committed_risk,
            account.pending_entry_count,
            account.portfolio_features,
        )
        if not account.ready or any(value is None for value in required_account):
            raise PaperPolicyOrchestrationError("reconciled account is not ready or lacks required facts")
        if account.equity <= 0 or account.available_cash < 0 or account.settled_cash < 0:
            raise PaperPolicyOrchestrationError("reconciled account sizing facts are invalid")
        if (
            feature_clock.completed_session != account.completed_session
            or feature_clock.as_of_cutoff != account.as_of_cutoff
            or feature_clock.next_eligible_session != account.next_execution_session
            or feature_clock.valuation_time != account.valuation_time
        ):
            raise PaperPolicyOrchestrationError("#98 feature and #99 account clocks differ")
        features = self.feature_context
        if (
            features.source_revision != deployment.source_revision
            or features.feature_contract_id != deployment.feature_contract_id
            or features.feature_calculator_identity != deployment.feature_calculator_id
            or bundle_descriptor.feature_contract_id != deployment.feature_contract_id
            or bundle_descriptor.feature_calculator_identity != deployment.feature_calculator_id
        ):
            raise PaperPolicyOrchestrationError("policy, deployment, and feature identities differ")
        if features.market_context.session != feature_clock.completed_session.isoformat():
            raise PaperPolicyOrchestrationError("market context session differs from the completed feature session")
        if tuple(candidate.symbol for candidate in self.candidates) != features.candidate_symbols:
            raise PaperPolicyOrchestrationError("candidate set differs from the complete #98 adapter output")
        if len({candidate.security_id for candidate in self.candidates}) != len(self.candidates):
            raise PaperPolicyOrchestrationError("candidate security identities are not unique")
        if account.portfolio_features.pending_entry_count != account.pending_entry_count:
            raise PaperPolicyOrchestrationError("portfolio pending-entry count differs from account reconciliation")
        for candidate in self.candidates:
            if candidate.base_snapshot.market != features.market_context:
                raise PaperPolicyOrchestrationError("candidate base snapshot uses a different market context")
            if candidate.symbol not in features.entry_features:
                raise PaperPolicyOrchestrationError("candidate lacks an authenticated #98 entry feature row")
            for required in bundle_descriptor.capabilities["required_entry_features"]:
                if _feature_value(features, candidate.symbol, required) is None:
                    raise PaperPolicyOrchestrationError(
                        f"required policy feature is unavailable: {candidate.symbol}.{required}"
                    )

    def _evaluate_entries(self, bundle):
        rows = []
        for candidate in self.candidates:
            snapshot = EntrySnapshotV3(
                base=candidate.base_snapshot,
                features=self.feature_context.entry_features[candidate.symbol],
            )
            decision = bundle.client.evaluate_entry(snapshot)
            if type(decision) is not EntryDecision:
                raise PaperPolicyOrchestrationError("policy entry output type is incompatible")
            rows.append({"candidate": candidate, "snapshot": snapshot, "decision": decision})
        return rows

    def _capacity_snapshot(self, rows) -> CapacitySnapshotV3:
        portfolio = self.account.portfolio_features
        base = CapacitySnapshot(
            market=self.feature_context.market_context,
            configured_max_positions=self.guard_profile.maximum_positions,
            maximum_policy_positions=self.guard_profile.maximum_policy_positions,
            open_position_count=self.open_position_count + int(self.account.pending_entry_count),
            eligible_signal_count=sum(
                row["decision"].qualified and row["decision"].market_permitted for row in rows
            ),
            cash_fraction=1.0 - portfolio.gross_exposure_fraction,
            configured_eviction_enabled=False,
        )
        return CapacitySnapshotV3(base=base, portfolio=portfolio)

    def _capacity_decision(self, deployment, clock, snapshot, decision, rows):
        invalid = decision.max_positions is not None and decision.max_positions > self.guard_profile.maximum_policy_positions
        requested = decision.max_positions
        host_limit = self.guard_profile.maximum_positions
        capped = requested is None or requested > host_limit
        veto = bool(decision.eviction_enabled or invalid or (capped and not self.guard_profile.allow_capacity_capping))
        effective = 0 if invalid else host_limit if requested is None else min(requested, host_limit)
        overrides = []
        if capped and self.guard_profile.allow_capacity_capping:
            overrides.append(
                {
                    "field": "max_positions",
                    "requested": "uncapped" if requested is None else requested,
                    "effective": host_limit,
                    "reason": "host_maximum_positions",
                }
            )
        reason = (
            "unsupported_replacement_requested"
            if decision.eviction_enabled
            else "invalid_policy_capacity"
            if invalid
            else "host_capacity_cap_not_permitted"
            if capped and not self.guard_profile.allow_capacity_capping
            else None
        )
        identity = _decision(
            deployment,
            clock,
            {
                "feature_manifest": self.feature_context.recorded_input_manifest_sha256,
                "account_snapshot": self.account.snapshot_id,
                "capacity_snapshot": snapshot.to_primitive(),
                "entry_outputs": [row["decision"].to_primitive() for row in rows],
            },
            DecisionCategory.CAPACITY,
            DecisionSubjectType.PORTFOLIO,
            deployment.paper_account_environment_id,
        )
        guard = {
            "precedence": list(ENTRY_GUARD_PRECEDENCE),
            "runtime_identity": deployment.runtime_identity,
            "execution_profile_id": deployment.execution_profile_id,
            "guard_id": self.guard_profile.content_addressed_guard_id,
            "outcome": "veto" if veto else "pass_with_override" if overrides else "pass",
            "veto_reason": reason,
            "requested_max_positions": requested,
            "effective_max_positions": effective,
            "host_maximum_positions": host_limit,
            "overrides": overrides,
        }
        return identity, effective, veto, guard

    def _record_entry_decisions(self, deployment, clock, rows, ranked, selected_ids, capacity_veto) -> None:
        ranks = {row["candidate"].security_id: index for index, row in enumerate(ranked)}
        for row in rows:
            candidate = row["candidate"]
            snapshot = row["snapshot"]
            output = row["decision"]
            eligible = output.qualified and output.market_permitted
            if not output.market_permitted:
                veto = "policy_market_veto"
            elif not output.qualified:
                veto = "policy_entry_ineligible"
            elif candidate.symbol in self.active_symbols:
                veto = "existing_holding_or_pending_order"
            elif capacity_veto:
                veto = "capacity_or_unsupported_replacement_veto"
            elif candidate.security_id not in selected_ids:
                veto = "capacity_rank_or_cycle_limit"
            else:
                veto = None
            identity = _decision(
                deployment,
                clock,
                {
                    "feature_manifest": self.feature_context.recorded_input_manifest_sha256,
                    "account_snapshot": self.account.snapshot_id,
                    "entry_snapshot": snapshot.to_primitive(),
                    "security_id": candidate.security_id,
                },
                DecisionCategory.ENTRY,
                DecisionSubjectType.SECURITY,
                candidate.security_id,
            )
            self.policy_store.record_decision(
                identity,
                policy_payload={"entry_snapshot": snapshot.to_primitive(), "entry_decision": output.to_primitive()},
                guard_payload={
                    "precedence": list(ENTRY_GUARD_PRECEDENCE),
                    "runtime_identity": deployment.runtime_identity,
                    "execution_profile_id": deployment.execution_profile_id,
                    "guard_id": self.guard_profile.content_addressed_guard_id,
                    "outcome": "veto" if veto else "pass",
                    "veto_reason": veto,
                    "policy_qualified": output.qualified,
                    "policy_market_permitted": output.market_permitted,
                    "policy_blocking_codes": list(output.blocking_codes),
                    "rank_index": ranks.get(candidate.security_id),
                    "selected_under_capacity": eligible and candidate.security_id in selected_ids,
                    "already_active": candidate.symbol in self.active_symbols,
                },
                effective_action_payload={"action": "none", "candidate_symbol": candidate.symbol},
            )

    def _allocate(self, deployment, bundle, clock, selected, effective_limit):
        plans = []
        records = []
        outcomes = []
        equity = _decimal(self.account.equity, "equity")
        remaining_cash = _decimal(self.account.available_cash, "available cash")
        portfolio_ceiling = equity * _decimal(self.guard_profile.maximum_total_risk_fraction, "total risk ceiling")
        remaining_risk = max(
            Decimal("0"),
            portfolio_ceiling - _decimal(self.account.total_committed_risk, "committed risk"),
        )
        reserved_cash = Decimal("0")
        reserved_risk = Decimal("0")
        epsilon_amount = equity * Decimal(str(math.ulp(1.0)))
        actual_gross = _decimal(self.account.gross_exposure, "gross exposure")
        account_reserved_cash = _decimal(self.account.reserved_buy_cash, "reserved buy cash")
        for allocation_index, row in enumerate(selected):
            candidate = row["candidate"]
            price = _round_step(candidate.reference_price, candidate.tick_size, ROUND_HALF_UP)
            # The selected queue is pending for allocation purposes, as in the historical adapter.
            # Keep the reconciled account object unchanged; this is a per-candidate projection.
            portfolio = replace(
                self.account.portfolio_features,
                pending_entry_count=max(
                    int(self.account.pending_entry_count or 0), len(selected) - allocation_index
                ),
            )
            legacy_gross = max(actual_gross, epsilon_amount)
            legacy_cash = max(equity - legacy_gross, epsilon_amount)
            allocation_base = AllocationSnapshot(
                market=self.feature_context.market_context,
                portfolio_equity_at_entry_open=float(equity),
                cash_before_transition=float(legacy_cash),
                projected_cash_after_eviction=float(max(remaining_cash, epsilon_amount)),
                gross_exposure_before=float(legacy_gross),
                projected_gross_exposure_after_eviction=float(
                    max(actual_gross + account_reserved_cash + reserved_cash, epsilon_amount)
                ),
                entry_open=float(price),
                pending_entries_remaining=len(selected) - allocation_index,
                capacity_is_uncapped=False,
                configured_position_risk_pct=self.guard_profile.maximum_position_risk_fraction,
                configured_stop_loss_pct=self.guard_profile.maximum_stop_distance_fraction,
                maximum_position_risk_fraction=float(
                    min(self.guard_profile.maximum_position_risk_fraction, remaining_risk / equity)
                ),
                maximum_stop_fraction=self.guard_profile.maximum_stop_distance_fraction,
                canslim_score=candidate.base_snapshot.canslim_score,
                rs_score=candidate.base_snapshot.rs_score,
            )
            allocation_snapshot = AllocationSnapshotV3(
                base=allocation_base,
                candidate=self.feature_context.entry_features[candidate.symbol],
                portfolio=portfolio,
            )
            allocation = bundle.client.recommend_allocation(allocation_snapshot)
            if type(allocation) is not AllocationDecision:
                raise PaperPolicyOrchestrationError("policy allocation output type is incompatible")
            requested_risk = _decimal(allocation.risk_fraction, "requested risk fraction")
            requested_stop = _decimal(allocation.stop_distance_fraction, "requested stop distance")
            requested_notional = Decimal("1") if allocation.notional_fraction_cap is None else _decimal(
                allocation.notional_fraction_cap, "requested notional fraction"
            )
            position_risk_cap = _decimal(self.guard_profile.maximum_position_risk_fraction, "position risk cap")
            total_risk_cap = max(Decimal("0"), remaining_risk / equity)
            effective_risk = min(requested_risk, position_risk_cap, total_risk_cap)
            effective_stop = min(requested_stop, _decimal(self.guard_profile.maximum_stop_distance_fraction, "stop distance cap"))
            effective_notional = min(requested_notional, _decimal(self.guard_profile.maximum_notional_fraction, "notional fraction cap"))
            requested_notional_amount = equity * requested_notional
            fraction_capped_notional_amount = equity * effective_notional
            absolute_notional_cap = self.guard_profile.maximum_notional_amount
            absolute_notional_binding = (
                absolute_notional_cap is not None
                and absolute_notional_cap < fraction_capped_notional_amount
            )
            pre_cash_notional_budget = (
                min(fraction_capped_notional_amount, absolute_notional_cap)
                if absolute_notional_cap is not None
                else fraction_capped_notional_amount
            )
            cash_cap_binding = remaining_cash < pre_cash_notional_budget
            overrides = []
            veto = None
            risk_cap_reason = (
                "host_total_risk_ceiling" if total_risk_cap < position_risk_cap else "host_position_risk_ceiling"
            )
            for field_name, requested, effective, allowed, reason in (
                ("risk_fraction", requested_risk, effective_risk, self.guard_profile.allow_risk_capping, risk_cap_reason),
                ("stop_distance_fraction", requested_stop, effective_stop, self.guard_profile.allow_stop_capping, "host_stop_distance_ceiling"),
                ("notional_fraction", requested_notional, effective_notional, self.guard_profile.allow_notional_capping, "host_notional_ceiling"),
            ):
                if effective < requested:
                    if not allowed:
                        veto = veto or f"{field_name}_cap_not_permitted"
                    else:
                        overrides.append({"field": field_name, "requested": str(requested), "effective": str(effective), "reason": reason})
            if absolute_notional_binding:
                if not self.guard_profile.allow_notional_capping:
                    veto = veto or "maximum_notional_amount_cap_not_permitted"
                else:
                    overrides.append(
                        {
                            "field": "maximum_notional_amount",
                            "requested": str(fraction_capped_notional_amount),
                            "effective": str(absolute_notional_cap),
                            "reason": "host_absolute_notional_ceiling",
                        }
                    )
            if effective_risk <= 0 or effective_stop <= 0:
                veto = veto or "non_executable_policy_allocation"
            stop_price = _round_step(price * (Decimal("1") - effective_stop), candidate.tick_size, ROUND_CEILING)
            risk_per_share = price - stop_price
            if stop_price <= 0 or risk_per_share <= 0:
                veto = veto or "broker_stop_precision_veto"
            risk_budget = min(equity * effective_risk, remaining_risk)
            if risk_budget <= 0:
                veto = veto or "portfolio_risk_ceiling_exhausted"
            notional_budget = min(pre_cash_notional_budget, remaining_cash)
            if notional_budget <= 0:
                veto = veto or "account_cash_or_notional_cap_exhausted"

            plan = None
            quantity = notional = risk_amount = None
            if veto is None:
                quantity = _round_step(
                    min(risk_budget / risk_per_share, notional_budget / price),
                    candidate.lot_size,
                    ROUND_DOWN,
                )
                if quantity < candidate.minimum_quantity:
                    veto = "broker_minimum_quantity_veto"
                else:
                    notional = quantity * price
                    risk_amount = quantity * risk_per_share
                    if notional > remaining_cash or risk_amount > remaining_risk:
                        veto = "account_cash_or_portfolio_risk_veto_after_precision"
            if veto is None:
                plan = EntryExecutionPlan(
                    symbol=candidate.symbol,
                    entry_price=float(price),
                    price_source=candidate.price_source,
                    stop_price=float(stop_price),
                    stop_loss_pct=float(risk_per_share / price),
                    position_value=float(notional),
                    risk_amount=float(risk_amount),
                    risk_per_share=float(risk_per_share),
                    qty=float(quantity),
                    canslim_score=float(candidate.base_snapshot.canslim_score or 0.0),
                    rs_score=float(candidate.base_snapshot.rs_score or 0.0),
                    is_breakout=candidate.is_breakout,
                    has_volume_surge=candidate.base_snapshot.has_volume_surge,
                )
                remaining_cash -= notional
                remaining_risk -= risk_amount
                reserved_cash += notional
                reserved_risk += risk_amount

            identity = _decision(
                deployment,
                clock,
                {
                    "feature_manifest": self.feature_context.recorded_input_manifest_sha256,
                    "account_snapshot": self.account.snapshot_id,
                    "entry_decision": row["decision"].to_primitive(),
                    "allocation_snapshot": allocation_snapshot.to_primitive(),
                    "quote": {
                        "price": str(candidate.reference_price),
                        "source": candidate.price_source,
                        "tick": str(candidate.tick_size),
                        "lot": str(candidate.lot_size),
                    },
                },
                DecisionCategory.ALLOCATION,
                DecisionSubjectType.SECURITY,
                candidate.security_id,
            )
            guard = {
                "precedence": list(ENTRY_GUARD_PRECEDENCE),
                "runtime_identity": deployment.runtime_identity,
                "execution_profile_id": deployment.execution_profile_id,
                "guard_id": self.guard_profile.content_addressed_guard_id,
                "outcome": "veto" if veto else "pass_with_overrides" if overrides else "pass",
                "veto_reason": veto,
                "requested": {
                    "risk_fraction": str(requested_risk),
                    "stop_distance_fraction": str(requested_stop),
                    "notional_fraction_cap": None if allocation.notional_fraction_cap is None else str(allocation.notional_fraction_cap),
                    "notional_amount": str(requested_notional_amount),
                },
                "effective": {
                    "risk_fraction": str(effective_risk),
                    "stop_distance_fraction": str(effective_stop),
                    "notional_fraction_cap": str(effective_notional),
                    "fraction_capped_notional_amount": str(fraction_capped_notional_amount),
                    "maximum_notional_amount": None if absolute_notional_cap is None else str(absolute_notional_cap),
                    "notional_amount": str(notional_budget),
                    "actual_notional_after_precision": None if veto else str(notional),
                    "price": None if veto else str(price),
                    "stop_price": None if veto else str(stop_price),
                    "quantity": None if veto else str(quantity),
                    "notional": None if veto else str(notional),
                    "risk_amount": None if veto else str(risk_amount),
                },
                "overrides": overrides,
                "amount_constraints": {
                    "absolute_notional_ceiling": {
                        "configured_amount": None if absolute_notional_cap is None else str(absolute_notional_cap),
                        "binding": absolute_notional_binding,
                        "reason": "host_absolute_notional_ceiling" if absolute_notional_binding else None,
                    },
                    "available_cash": {
                        "available_amount": str(remaining_cash),
                        "binding": cash_cap_binding,
                        "effective_amount": str(notional_budget),
                        "reason": "account_cash_solvency_ceiling" if cash_cap_binding else None,
                    },
                },
                "cycle_reserved_before": {"cash": str(reserved_cash - (Decimal("0") if veto else notional)), "risk": str(reserved_risk - (Decimal("0") if veto else risk_amount))},
                "broker_precision": {
                    "tick_size": str(candidate.tick_size),
                    "lot_size": str(candidate.lot_size),
                    "minimum_quantity": str(candidate.minimum_quantity),
                    "price_rounding": "nearest_half_up",
                    "stop_rounding": "up_toward_entry_to_preserve_distance_cap",
                    "quantity_rounding": "down",
                },
            }
            effective = {
                "entry_plan": None if plan is None else {
                    "order_type": "limit",
                    "symbol": plan.symbol,
                    "entry_price": plan.entry_price,
                    "stop_price": plan.stop_price,
                    "quantity": plan.qty,
                    "notional": plan.position_value,
                    "risk_amount": plan.risk_amount,
                    "risk_per_share": plan.risk_per_share,
                    "stop_loss_pct": plan.stop_loss_pct,
                    "price_source": plan.price_source,
                    "canslim_score": plan.canslim_score,
                    "rs_score": plan.rs_score,
                    "is_breakout": plan.is_breakout,
                    "has_volume_surge": plan.has_volume_surge,
                }
            }
            records.append(
                (
                    identity,
                    {"allocation_snapshot": allocation_snapshot.to_primitive(), "allocation_decision": allocation.to_primitive()},
                    guard,
                    effective,
                )
            )
            outcomes.append((candidate.symbol, veto or ("allocation_guard_capped" if overrides else "allocation_guard_passed")))
            if plan is not None:
                plans.append((row, plan, identity))
        return plans, records, outcomes


__all__ = [
    "ENTRY_GUARD_PRECEDENCE",
    "PaperPolicyCandidate",
    "PaperPolicyCycleResult",
    "PaperPolicyGuardProfile",
    "PaperPolicyOrchestrationError",
    "PaperPolicyOrchestrator",
    "PolicyEntryConsumer",
]
