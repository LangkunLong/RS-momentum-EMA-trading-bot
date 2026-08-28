"""Immutable fold and aggregate contracts for the PIT optimizer."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import json
import math
import re
from itertools import pairwise


_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _date(value: str, label: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{label} is invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} is not canonical")
    return parsed


def _finite(value: float | int, label: str) -> None:
    if isinstance(value, bool) or type(value) not in {int, float} or not math.isfinite(float(value)):
        raise ValueError(f"{label} must be finite")


@dataclass(frozen=True, slots=True)
class FoldSpec:
    fold_id: str
    purpose: str
    start_date: str
    end_date: str
    sessions: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.fold_id, str) or not self.fold_id:
            raise ValueError("fold_id is invalid")
        if self.purpose not in {"discovery", "hidden"}:
            raise ValueError("fold purpose is invalid")
        if type(self.sessions) is not tuple or len(self.sessions) != 60:
            raise ValueError("fold must contain exactly 60 sessions")
        parsed = tuple(_date(value, "fold session") for value in self.sessions)
        if len(set(parsed)) != len(parsed) or any(left >= right for left, right in pairwise(parsed)):
            raise ValueError("fold sessions must be unique and chronological")
        start = _date(self.start_date, "fold start_date")
        end = _date(self.end_date, "fold end_date")
        if start != parsed[0] or end != parsed[-1]:
            raise ValueError("fold boundaries must match sessions")


@dataclass(frozen=True, slots=True)
class FoldManifest:
    data_identity_sha256: str
    universe_sha256: str
    benchmark: str
    warmup_start_date: str
    discovery_folds: tuple[FoldSpec, ...]
    hidden_fold: FoldSpec

    def __post_init__(self) -> None:
        for name in ("data_identity_sha256", "universe_sha256"):
            if _SHA256_RE.fullmatch(getattr(self, name) or "") is None:
                raise ValueError(f"{name} is invalid")
        if not isinstance(self.benchmark, str) or not self.benchmark or self.benchmark != self.benchmark.upper():
            raise ValueError("benchmark is invalid")
        warmup = _date(self.warmup_start_date, "warmup_start_date")
        if type(self.discovery_folds) is not tuple or len(self.discovery_folds) != 2:
            raise ValueError("fold manifest requires exactly two discovery folds")
        if any(not isinstance(fold, FoldSpec) or fold.purpose != "discovery" for fold in self.discovery_folds):
            raise ValueError("discovery fold purpose is invalid")
        if not isinstance(self.hidden_fold, FoldSpec) or self.hidden_fold.purpose != "hidden":
            raise ValueError("hidden fold purpose is invalid")
        folds = (*self.discovery_folds, self.hidden_fold)
        if len({fold.fold_id for fold in folds}) != len(folds):
            raise ValueError("fold IDs must be unique")
        seen: set[str] = set()
        for fold in folds:
            overlap = seen.intersection(fold.sessions)
            if overlap:
                raise ValueError("fold sessions overlap")
            seen.update(fold.sessions)
        if warmup >= _date(self.discovery_folds[0].start_date, "first discovery start"):
            raise ValueError("warmup must precede discovery")
        if any(
            _date(left.end_date, "fold end") >= _date(right.start_date, "fold start") for left, right in pairwise(folds)
        ):
            raise ValueError("folds must be chronological with hidden strictly after discovery")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(_canonical_json_bytes(asdict(self))).hexdigest()


@dataclass(frozen=True, slots=True)
class AggregateMetric:
    metric_id: str
    value: float | int

    def __post_init__(self) -> None:
        if not isinstance(self.metric_id, str) or not self.metric_id:
            raise ValueError("aggregate metric ID is invalid")
        _finite(self.value, "aggregate metric value")


@dataclass(frozen=True, slots=True)
class FoldAggregateSummary:
    fold_id: str
    total_return_pct: float
    excess_total_return_pp: float | None
    max_drawdown_pct: float
    sharpe_ratio: float
    closed_trades: int
    turnover_pct: float
    average_exposure_pct: float
    entry_funnel: tuple[AggregateMetric, ...]
    exit_attribution: tuple[AggregateMetric, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.fold_id, str) or not self.fold_id:
            raise ValueError("aggregate fold_id is invalid")
        for name in (
            "total_return_pct",
            "max_drawdown_pct",
            "sharpe_ratio",
            "turnover_pct",
            "average_exposure_pct",
        ):
            _finite(getattr(self, name), name)
        if self.excess_total_return_pp is not None:
            _finite(self.excess_total_return_pp, "excess_total_return_pp")
        if type(self.closed_trades) is not int or self.closed_trades < 0:
            raise ValueError("closed_trades is invalid")
        for name in ("entry_funnel", "exit_attribution"):
            metrics = getattr(self, name)
            if type(metrics) is not tuple or any(not isinstance(metric, AggregateMetric) for metric in metrics):
                raise ValueError(f"{name} is invalid")
            ids = tuple(metric.metric_id for metric in metrics)
            if len(set(ids)) != len(ids):
                raise ValueError(f"{name} metric IDs must be unique")
