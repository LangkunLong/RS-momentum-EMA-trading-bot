"""Closed, finite evidence records for offline PIT diagnosis runs."""

from __future__ import annotations

from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Mapping

from .models import PartitionName


_EXIT_REASONS = frozenset({"stop_loss", "ma_violation", "time_stop", "end_of_test", "profit_zone", "structural_sell", "eight_week_hold"})
_REJECTION_CODES = frozenset({"next_open_buy_zone", "no_cash", "already_open", "capacity", "missing_data", "invalid_price", "invalid_risk"})


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{field} must be finite")
    return float(value)


def _count(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _pct(value: object, field: str, *, lower: float = -100.0, upper: float = 100.0) -> float:
    number = _finite(value, field)
    if not lower <= number <= upper:
        raise ValueError(f"{field} is outside its percentage domain")
    return number


def _evidence(values: tuple[str, ...], field: str) -> tuple[str, ...]:
    if not isinstance(values, tuple) or any(not isinstance(item, str) or not item for item in values):
        raise ValueError(f"{field} must be a tuple of non-empty IDs")
    if values != tuple(sorted(values)) or len(set(values)) != len(values):
        raise ValueError(f"{field} must be sorted and unique")
    return values


@dataclass(frozen=True)
class RuleAttribution:
    rule_id: str
    evaluated: int
    survivors: int
    passed: int
    failed: int
    unavailable: int
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.rule_id, str) or not self.rule_id:
            raise ValueError("rule_id must be non-empty")
        for name in ("evaluated", "survivors", "passed", "failed", "unavailable"):
            _count(getattr(self, name), name)
        if self.survivors > self.evaluated or self.passed + self.failed + self.unavailable != self.evaluated:
            raise ValueError("rule attribution totals are inconsistent")
        _evidence(self.evidence_ids, "evidence_ids")


@dataclass(frozen=True)
class EntryFunnel:
    evaluated: int
    qualified: int
    attempted: int
    executed: int
    rejections: Mapping[str, int]

    def __post_init__(self) -> None:
        for name in ("evaluated", "qualified", "attempted", "executed"):
            _count(getattr(self, name), name)
        if not self.executed <= self.attempted <= self.qualified <= self.evaluated:
            raise ValueError("entry funnel is not monotone")
        if not isinstance(self.rejections, Mapping) or set(self.rejections) - _REJECTION_CODES:
            raise ValueError("entry funnel has an unknown rejection code")
        frozen = {str(key): _count(value, f"rejections.{key}") for key, value in self.rejections.items()}
        if sum(frozen.values()) != self.attempted - self.executed:
            raise ValueError("entry funnel rejection totals are inconsistent")
        object.__setattr__(self, "rejections", MappingProxyType(dict(sorted(frozen.items()))))


@dataclass(frozen=True)
class ExitReasonAttribution:
    reason: str
    closed_positions: int
    wins: int
    win_rate_pct: float
    average_completed_position_return_pct: float
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.reason not in _EXIT_REASONS:
            raise ValueError("unknown exit reason")
        _count(self.closed_positions, "closed_positions")
        _count(self.wins, "wins")
        if self.wins > self.closed_positions:
            raise ValueError("exit wins exceed closed positions")
        _pct(self.win_rate_pct, "win_rate_pct", lower=0.0)
        expected = 0.0 if not self.closed_positions else self.wins * 100.0 / self.closed_positions
        if not math.isclose(self.win_rate_pct, expected, abs_tol=0.005):
            raise ValueError("exit win rate is inconsistent")
        _pct(self.average_completed_position_return_pct, "average_completed_position_return_pct")
        _evidence(self.evidence_ids, "evidence_ids")


@dataclass(frozen=True)
class ExitAttribution:
    by_reason: Mapping[str, ExitReasonAttribution]

    def __post_init__(self) -> None:
        if not isinstance(self.by_reason, Mapping) or not self.by_reason:
            raise ValueError("exit attribution must be non-empty")
        values: dict[str, ExitReasonAttribution] = {}
        for reason, record in self.by_reason.items():
            if reason not in _EXIT_REASONS or not isinstance(record, ExitReasonAttribution) or record.reason != reason:
                raise ValueError("exit attribution has an invalid reason record")
            values[reason] = record
        object.__setattr__(self, "by_reason", MappingProxyType(dict(sorted(values.items()))))


@dataclass(frozen=True)
class TradeStatistics:
    completed_positions: int
    wins: int
    losses: int
    win_rate_pct: float
    mean_return_pct: float
    median_return_pct: float
    mean_winner_pct: float
    mean_loser_pct: float
    expectancy_pct: float
    mean_calendar_hold_days: float
    median_calendar_hold_days: float
    mean_trading_session_hold_days: float
    median_trading_session_hold_days: float

    def __post_init__(self) -> None:
        for name in ("completed_positions", "wins", "losses"):
            _count(getattr(self, name), name)
        if self.wins + self.losses != self.completed_positions:
            raise ValueError("trade totals are inconsistent")
        expected = 0.0 if not self.completed_positions else self.wins * 100.0 / self.completed_positions
        _pct(self.win_rate_pct, "win_rate_pct", lower=0.0)
        if not math.isclose(self.win_rate_pct, expected, abs_tol=0.005):
            raise ValueError("trade win rate is inconsistent")
        for name in ("mean_return_pct", "median_return_pct", "mean_winner_pct", "mean_loser_pct", "expectancy_pct"):
            # Position returns are finite percentages but are not bounded at
            # +100%: a multi-bagger can legitimately exceed that value.
            _finite(getattr(self, name), name)
        for name in ("mean_calendar_hold_days", "median_calendar_hold_days", "mean_trading_session_hold_days", "median_trading_session_hold_days"):
            if _finite(getattr(self, name), name) < 0.0:
                raise ValueError(f"{name} must be non-negative")


@dataclass(frozen=True)
class PerformanceEvidence:
    partition: PartitionName | str
    total_return_pct: float
    annualized_return_pct: float
    sharpe_ratio: float
    max_drawdown_pct: float
    average_cash_pct: float
    closed_positions: int
    benchmark_total_return_delta_pct: float
    benchmark_annualized_return_delta_pct: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "partition", PartitionName(self.partition))
        for name in ("total_return_pct", "annualized_return_pct", "max_drawdown_pct", "benchmark_total_return_delta_pct", "benchmark_annualized_return_delta_pct"):
            _pct(getattr(self, name), name)
        _pct(self.average_cash_pct, "average_cash_pct", lower=0.0)
        _finite(self.sharpe_ratio, "sharpe_ratio")
        _count(self.closed_positions, "closed_positions")


@dataclass(frozen=True)
class LeaderRecallEvidence:
    labelled_leaders: int
    pit_exposed_leaders: int
    recalled_leaders: int
    recall_pct: float
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("labelled_leaders", "pit_exposed_leaders", "recalled_leaders"):
            _count(getattr(self, name), name)
        if self.pit_exposed_leaders > self.labelled_leaders or self.recalled_leaders > self.pit_exposed_leaders:
            raise ValueError("leader recall totals are inconsistent")
        expected = 0.0 if not self.pit_exposed_leaders else self.recalled_leaders * 100.0 / self.pit_exposed_leaders
        _pct(self.recall_pct, "recall_pct", lower=0.0)
        if not math.isclose(self.recall_pct, expected, abs_tol=0.005):
            raise ValueError("leader recall percentage is inconsistent")
        _evidence(self.evidence_ids, "evidence_ids")
