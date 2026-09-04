"""Trusted builders for the adaptive O'Neil policy interface V3 boundary."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from core.pit_feature_snapshot import EntryFeaturesV3, HoldingFeaturesV3

from .contracts import (
    AllocationSnapshot,
    CapacitySnapshot,
    EntrySnapshot,
    EvictionPosition,
    EvictionSnapshot,
    ExitSnapshot,
    MarketContextV1,
)
from .contracts_v3 import (
    AddOnSnapshotV3,
    AllocationSnapshotV3,
    CapacitySnapshotV3,
    EntrySnapshotV3,
    EvictionPositionV3,
    EvictionSnapshotV3,
    ExitSnapshotV3,
    PortfolioFeaturesV3,
)


def _finite(value: object, name: str, *, minimum: float | None = None) -> float:
    if type(value) is bool or not isinstance(value, (int, float)):
        raise ValueError(f"trusted {name} is invalid")
    number = float(value)
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        raise ValueError(f"trusted {name} is invalid")
    return number


def _positive(value: object, name: str) -> float:
    number = _finite(value, name)
    if number <= 0:
        raise ValueError(f"trusted {name} must be positive")
    return number


def _count(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"trusted {name} is invalid")
    return value


def _close(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-9)


def _amounts(values: Sequence[float], name: str) -> tuple[float, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError(f"trusted {name} is invalid")
    return tuple(_finite(value, name, minimum=0.0) for value in values)


def _exposure_amounts(
    values: Mapping[str, float],
    name: str,
) -> tuple[tuple[str, float], ...]:
    if not isinstance(values, Mapping):
        raise ValueError(f"trusted {name} is invalid")
    result: list[tuple[str, float]] = []
    for key, value in values.items():
        if (
            type(key) is not str
            or not key
            or key.strip() != key
            or len(key) > 128
        ):
            raise ValueError(f"trusted {name} key is invalid")
        amount = _finite(value, name, minimum=0.0)
        if amount > 0:
            result.append((key, amount))
    result.sort(key=lambda item: item[0])
    if len({key for key, _value in result}) != len(result):
        raise ValueError(f"trusted {name} keys are not unique")
    return tuple(result)


class StrategyPolicyAdapterV3:
    """Construct scalar V3 snapshots exclusively from authenticated engine facts.

    The adapter deliberately accepts no bundle, DataFrame, transaction row, or path.
    Monetary totals are supplied as trusted engine-owned scalars and are converted to
    fractions only after their cash, notional, risk, and classification totals reconcile.
    """

    @staticmethod
    def build_entry_snapshot(
        *,
        base: EntrySnapshot,
        features: EntryFeaturesV3,
    ) -> EntrySnapshotV3:
        """Compose trusted V2 entry facts and authenticated causal features."""
        return EntrySnapshotV3(base=base, features=features)

    @staticmethod
    def build_portfolio_features(
        *,
        equity: float,
        cash: float,
        peak_equity: float,
        position_notionals: Sequence[float],
        position_open_risks: Sequence[float],
        pending_entry_count: int,
        sector_notionals: Mapping[str, float],
        industry_notionals: Mapping[str, float],
    ) -> PortfolioFeaturesV3:
        """Recompute portfolio fractions after reconciling all trusted dollar totals."""
        trusted_equity = _positive(equity, "equity")
        trusted_cash = _finite(cash, "cash", minimum=0.0)
        trusted_peak = _positive(peak_equity, "peak equity")
        if trusted_peak < trusted_equity and not _close(trusted_peak, trusted_equity):
            raise ValueError("trusted peak equity is below current equity")
        notionals = _amounts(position_notionals, "position notionals")
        risks = _amounts(position_open_risks, "position open risks")
        if len(notionals) != len(risks):
            raise ValueError("trusted position notional/risk counts differ")
        gross = math.fsum(notionals)
        open_risk = math.fsum(risks)
        if not _close(trusted_cash + gross, trusted_equity):
            raise ValueError("trusted cash and gross exposure do not reconcile to equity")
        if gross > trusted_equity and not _close(gross, trusted_equity):
            raise ValueError("trusted gross exposure would introduce leverage")
        if open_risk > trusted_equity and not _close(open_risk, trusted_equity):
            raise ValueError("trusted open risk exceeds equity")
        sectors = _exposure_amounts(sector_notionals, "sector notionals")
        industries = _exposure_amounts(industry_notionals, "industry notionals")
        for name, exposures in (("sector", sectors), ("industry", industries)):
            total = math.fsum(value for _key, value in exposures)
            if not _close(total, gross):
                raise ValueError(
                    f"trusted {name} notionals do not reconcile to gross exposure"
                )
        return PortfolioFeaturesV3(
            gross_exposure_fraction=gross / trusted_equity,
            drawdown_fraction=(trusted_peak - trusted_equity) / trusted_peak,
            open_risk_fraction=open_risk / trusted_equity,
            pending_entry_count=_count(pending_entry_count, "pending entry count"),
            sector_exposures=tuple(
                (key, value / trusted_equity) for key, value in sectors
            ),
            industry_exposures=tuple(
                (key, value / trusted_equity) for key, value in industries
            ),
        )

    @staticmethod
    def build_capacity_snapshot(
        *,
        base: CapacitySnapshot,
        portfolio: PortfolioFeaturesV3,
    ) -> CapacitySnapshotV3:
        """Compose capacity facts after their cash fraction reconciles."""
        return CapacitySnapshotV3(base=base, portfolio=portfolio)

    @staticmethod
    def build_allocation_snapshot(
        *,
        base: AllocationSnapshot,
        candidate: EntryFeaturesV3,
        portfolio: PortfolioFeaturesV3,
    ) -> AllocationSnapshotV3:
        """Compose allocation facts after trusted cash/gross reconciliation."""
        return AllocationSnapshotV3(
            base=base,
            candidate=candidate,
            portfolio=portfolio,
        )

    @staticmethod
    def build_eviction_position(
        *,
        base: EvictionPosition,
        features: HoldingFeaturesV3,
        current_price: float,
        current_notional: float,
        equity: float,
        days_held: int,
    ) -> EvictionPositionV3:
        """Recompute one holding's return and notional fraction."""
        price = _positive(current_price, "current price")
        trusted_equity = _positive(equity, "equity")
        notional = _positive(current_notional, "current notional")
        if base.causal_execution_price is not None and not _close(
            float(base.causal_execution_price), price
        ):
            raise ValueError("trusted current price differs from causal execution price")
        return EvictionPositionV3(
            base=base,
            features=features,
            unrealized_return_fraction=price / float(base.entry_price) - 1.0,
            days_held=_count(days_held, "days held"),
            notional_fraction=notional / trusted_equity,
        )

    @staticmethod
    def build_eviction_snapshot(
        *,
        base: EvictionSnapshot,
        candidate: EntryFeaturesV3,
        positions: Sequence[EvictionPositionV3],
        portfolio: PortfolioFeaturesV3,
    ) -> EvictionSnapshotV3:
        """Compose eviction state while preserving the trusted V2 slot set."""
        if isinstance(positions, (str, bytes)) or not isinstance(positions, Sequence):
            raise ValueError("trusted eviction positions are invalid")
        return EvictionSnapshotV3(
            base=base,
            candidate=candidate,
            positions=tuple(positions),
            portfolio=portfolio,
        )

    @staticmethod
    def build_add_on_snapshot(
        *,
        market: MarketContextV1,
        entry_price: float,
        current_price: float,
        current_quantity: float,
        equity: float,
        cash: float,
        open_position_risk: float,
        days_held: int,
        add_on_count: int,
        highest_price: float,
        lowest_price: float,
        features: HoldingFeaturesV3,
    ) -> AddOnSnapshotV3:
        """Build add-on state from trusted valuation and excursion extrema."""
        trusted_entry = _positive(entry_price, "entry price")
        trusted_price = _positive(current_price, "current price")
        quantity = _positive(current_quantity, "current quantity")
        trusted_equity = _positive(equity, "equity")
        trusted_cash = _finite(cash, "cash", minimum=0.0)
        trusted_risk = _finite(
            open_position_risk, "open position risk", minimum=0.0
        )
        high = _positive(highest_price, "highest price")
        low = _positive(lowest_price, "lowest price")
        if high < max(trusted_entry, trusted_price) or low > min(
            trusted_entry, trusted_price
        ):
            raise ValueError("trusted add-on excursion extrema do not contain the position")
        current_notional = trusted_price * quantity
        if trusted_cash + current_notional > trusted_equity and not _close(
            trusted_cash + current_notional, trusted_equity
        ):
            raise ValueError("trusted add-on cash and position imply leverage")
        return AddOnSnapshotV3(
            market=market,
            entry_price=trusted_entry,
            current_price=trusted_price,
            current_quantity=quantity,
            current_notional_fraction=current_notional / trusted_equity,
            unrealized_return_fraction=trusted_price / trusted_entry - 1.0,
            days_held=_count(days_held, "days held"),
            add_on_count=_count(add_on_count, "add-on count"),
            maximum_favorable_excursion_fraction=high / trusted_entry - 1.0,
            maximum_adverse_excursion_fraction=low / trusted_entry - 1.0,
            remaining_cash_fraction=trusted_cash / trusted_equity,
            open_position_risk_fraction=trusted_risk / trusted_equity,
            features=features,
        )

    @staticmethod
    def build_exit_snapshot(
        *,
        base: ExitSnapshot,
        features: HoldingFeaturesV3,
        equity: float,
        lowest_price: float,
    ) -> ExitSnapshotV3:
        """Build exit state from trusted V2 valuation and adverse excursion facts."""
        trusted_equity = _positive(equity, "equity")
        low = _positive(lowest_price, "lowest price")
        if low > min(float(base.entry_price), float(base.current_close)):
            raise ValueError("trusted exit low does not contain the position path")
        return ExitSnapshotV3(
            base=base,
            features=features,
            unrealized_return_fraction=(
                float(base.current_close) / float(base.entry_price) - 1.0
            ),
            maximum_favorable_excursion_fraction=(
                float(base.peak_close) / float(base.entry_price) - 1.0
            ),
            maximum_adverse_excursion_fraction=low / float(base.entry_price) - 1.0,
            position_notional_fraction=(
                float(base.current_close) * float(base.remaining_qty) / trusted_equity
            ),
        )


__all__ = ["StrategyPolicyAdapterV3"]
