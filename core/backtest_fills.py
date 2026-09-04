"""Pure, causal fill and friction primitives for the V5 evaluator."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Literal


Side = Literal["BUY", "SELL"]
StopKind = Literal["gap_stop", "intraday_stop"]


@dataclass(frozen=True, slots=True)
class ExecutionProfileV5:
    """Closed identity for the optimizer V5 execution semantics."""

    schema_version: Literal[5]
    close_policy_exit_timing: Literal["next_open"]
    gap_stop_rule: Literal["open_then_stop"]
    end_of_test_rule: Literal["last_session_close"]
    friction_model: Literal[
        "half_spread_plus_market_impact_plus_commission_bps"
    ]

    def __post_init__(self) -> None:
        expected = (
            5,
            "next_open",
            "open_then_stop",
            "last_session_close",
            "half_spread_plus_market_impact_plus_commission_bps",
        )
        actual = (
            self.schema_version,
            self.close_policy_exit_timing,
            self.gap_stop_rule,
            self.end_of_test_rule,
            self.friction_model,
        )
        if actual != expected or type(self.schema_version) is not int:
            raise ValueError("execution profile must be the canonical V5 profile")

    def to_primitive(self) -> dict[str, int | str]:
        """Return every profile field in its stable serialized form."""
        return {
            "schema_version": self.schema_version,
            "close_policy_exit_timing": self.close_policy_exit_timing,
            "gap_stop_rule": self.gap_stop_rule,
            "end_of_test_rule": self.end_of_test_rule,
            "friction_model": self.friction_model,
        }

    @property
    def sha256(self) -> str:
        payload = json.dumps(
            self.to_primitive(),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def _number(value: object, field: str, *, positive: bool = False) -> float:
    """Return a finite numeric value, rejecting bool and other numeric types."""
    if type(value) not in {int, float}:
        raise ValueError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0.0):
        qualifier = "positive and finite" if positive else "finite"
        raise ValueError(f"{field} must be {qualifier}")
    return number


def _bps(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _side(value: object) -> Side:
    if value not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    return value  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class FrictionScenario:
    """The explicit spread, impact, and commission assumptions for a fill."""

    scenario_id: str
    half_spread_bps: int
    market_impact_bps: int
    commission_bps: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.scenario_id, str)
            or not self.scenario_id.strip()
            or self.scenario_id != self.scenario_id.strip()
            or "\x00" in self.scenario_id
        ):
            raise ValueError("scenario_id must be non-empty canonical text")
        _bps(self.half_spread_bps, "half_spread_bps")
        _bps(self.market_impact_bps, "market_impact_bps")
        _bps(self.commission_bps, "commission_bps")


@dataclass(frozen=True, slots=True)
class StopReference:
    """The causal price at which a long stop is resolved."""

    kind: StopKind
    price: float

    def __post_init__(self) -> None:
        if self.kind not in {"gap_stop", "intraday_stop"}:
            raise ValueError("kind must be gap_stop or intraday_stop")
        _number(self.price, "price", positive=True)


@dataclass(frozen=True, slots=True)
class ExecutionFill:
    """A friction-adjusted fill and its cash-accounting evidence."""

    side: Side
    reference_price: float
    execution_price: float
    quantity: float
    gross_value: float
    commission_usd: float
    spread_cost_usd: float
    market_impact_cost_usd: float
    cash_delta: float

    def __post_init__(self) -> None:
        _side(self.side)
        _number(self.reference_price, "reference_price", positive=True)
        _number(self.execution_price, "execution_price", positive=True)
        _number(self.quantity, "quantity", positive=True)
        for field in (
            "gross_value",
            "commission_usd",
            "spread_cost_usd",
            "market_impact_cost_usd",
            "cash_delta",
        ):
            value = _number(getattr(self, field), field)
            if field != "cash_delta" and value < 0.0:
                raise ValueError(f"{field} must be non-negative")


def resolve_long_stop(
    *, open_price: float, low_price: float, stop_price: float
) -> StopReference | None:
    """Resolve a long stop using only information available in the session."""
    open_value = _number(open_price, "open_price", positive=True)
    low_value = _number(low_price, "low_price", positive=True)
    stop_value = _number(stop_price, "stop_price", positive=True)
    if open_value <= stop_value:
        return StopReference(kind="gap_stop", price=open_value)
    if low_value <= stop_value:
        return StopReference(kind="intraday_stop", price=stop_value)
    return None


def apply_friction(
    *, side: Side, reference_price: float, quantity: float, scenario: FrictionScenario
) -> ExecutionFill:
    """Apply adverse half-spread and impact, then commission, to a fill."""
    resolved_side = _side(side)
    reference_value = _number(reference_price, "reference_price", positive=True)
    quantity_value = _number(quantity, "quantity", positive=True)
    if not isinstance(scenario, FrictionScenario):
        raise ValueError("scenario must be a FrictionScenario")

    spread_rate = scenario.half_spread_bps / 10_000.0
    impact_rate = scenario.market_impact_bps / 10_000.0
    total_rate = spread_rate + impact_rate
    direction = 1.0 if resolved_side == "BUY" else -1.0
    execution_price = reference_value * (1.0 + direction * total_rate)
    if not math.isfinite(execution_price) or execution_price <= 0.0:
        raise ValueError("execution_price must be positive and finite")

    gross_unrounded = execution_price * quantity_value
    commission_unrounded = gross_unrounded * scenario.commission_bps / 10_000.0
    spread_unrounded = reference_value * quantity_value * spread_rate
    impact_unrounded = reference_value * quantity_value * impact_rate
    cash_unrounded = (
        -(gross_unrounded + commission_unrounded)
        if resolved_side == "BUY"
        else gross_unrounded - commission_unrounded
    )

    return ExecutionFill(
        side=resolved_side,
        reference_price=reference_value,
        execution_price=execution_price,
        quantity=quantity_value,
        gross_value=round(gross_unrounded, 2),
        commission_usd=round(commission_unrounded, 2),
        spread_cost_usd=round(spread_unrounded, 2),
        market_impact_cost_usd=round(impact_unrounded, 2),
        cash_delta=round(cash_unrounded, 2),
    )
