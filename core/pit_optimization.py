"""Offline authority and deterministic evaluation for one PIT optimization cycle."""

from __future__ import annotations

import hashlib
import difflib
import json
import math
import os
import re
import secrets
import shutil
import stat
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean, median
from types import MappingProxyType
from typing import Callable, Iterable, Mapping, Sequence

import pandas as pd

from core.pit_optimization_contract import (
    BASELINE_MANIFEST_SHA256,
    BASELINE_SOURCE_COMMIT,
    ENTRY_CONTRACT_PATH,
    FULL_END_DATE,
    FULL_START_DATE,
    HOLDOUT_END_DATE,
    HOLDOUT_START_DATE,
    MAX_CANARY_CALLS,
    MAX_CANARY_USD,
    PIT_BUNDLE_SHA256,
    OptimizationComparison,
    OptimizationWindowMetrics,
    PitOptimizationCoding,
    PitOptimizationReasoning,
    PitOptimizationRoute,
    build_comparison,
    candidate_catalog,
    validate_coding_selection,
    validate_policy_delta,
    verify_catalog_source,
)


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_EXPECTED_BASELINE_METRICS = MappingProxyType(
    {
        "total_return_pct": 10.466720690872911,
        "annualized_return_pct": 2.014176669879464,
        "max_drawdown_pct": -12.039961219836394,
        "sharpe_ratio": 0.3429824494606095,
        "closed_trades": 176,
        "average_cash_pct": 74.84827271765748,
    }
)
_EXPECTED_BASELINE_FUNNEL = MappingProxyType(
    {
        "evaluated": 598696,
        "qualified": 233,
        "attempted": 233,
        "executed": 176,
        "rejected": 57,
        "next_open_buy_zone_rejections": 57,
    }
)
_INVARIANT_IDS = (
    "invariant.completed_session_facts",
    "invariant.deterministic_accounting",
    "invariant.immutable_input_identity",
    "invariant.next_session_execution",
    "invariant.no_leverage",
    "invariant.point_in_time",
)
_EVIDENCE_IDS = (
    "metric.full.cash",
    "metric.full.entry_funnel",
    "metric.full.objective",
    "metric.full.trade_quality",
    "metric.holdout.objective",
)
_ENTRY_OUTCOMES = (
    "entries_executed",
    "entry_rejected_already_open",
    "entry_rejected_capacity",
    "entry_rejected_missing_data",
    "entry_rejected_invalid_price",
    "entry_rejected_next_open_buy_zone",
    "entry_rejected_invalid_risk",
    "entry_rejected_no_cash",
)
_VERIFICATION_SESSION_COUNT = 60
_VERIFICATION_SYMBOL_COUNT = 25


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _absolute_path(value: Path, field: str) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise ValueError(f"{field} must be an explicit absolute path")
    return value.resolve(strict=False)


def _paths_overlap(first: Path, second: Path) -> bool:
    first = first.resolve(strict=False)
    second = second.resolve(strict=False)
    try:
        first.relative_to(second)
        return True
    except ValueError:
        pass
    try:
        second.relative_to(first)
        return True
    except ValueError:
        return False


def _regular_file(path: Path, field: str) -> Path:
    source = Path(path)
    try:
        info = source.lstat()
    except OSError as exc:
        raise ValueError(f"{field} is unavailable") from exc
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    if (
        not stat.S_ISREG(info.st_mode)
        or source.is_symlink()
        or reparse
        or info.st_nlink != 1
    ):
        raise ValueError(f"{field} must be a regular non-reparse single-link file")
    return source.resolve(strict=True)


def _regular_directory(path: Path, field: str) -> Path:
    source = Path(path)
    try:
        info = source.lstat()
    except OSError as exc:
        raise ValueError(f"{field} is unavailable") from exc
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    if not stat.S_ISDIR(info.st_mode) or source.is_symlink() or reparse:
        raise ValueError(f"{field} must be a regular non-link directory")
    return source.resolve(strict=True)


@dataclass(frozen=True, slots=True)
class PitOptimizationGateConfig:
    """Exact operator-controlled scope for offline prepare or one paid canary."""

    phase: str
    baseline_run: Path
    baseline_manifest_sha256: str
    pit_bundle: Path
    pit_bundle_sha256: str
    effective_policy_sha256: str
    max_usd: float
    max_api_calls: int
    max_iterations: int
    apply: bool
    verification_subset: bool = False
    readiness_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.phase not in {"prepare", "canary"}:
            raise ValueError("optimization phase must be prepare or canary")
        object.__setattr__(
            self, "baseline_run", _absolute_path(self.baseline_run, "baseline run")
        )
        object.__setattr__(
            self, "pit_bundle", _absolute_path(self.pit_bundle, "PIT bundle")
        )
        if self.baseline_manifest_sha256 != BASELINE_MANIFEST_SHA256:
            raise ValueError("baseline manifest differs from the sealed Task 11 authority")
        if self.pit_bundle_sha256 != PIT_BUNDLE_SHA256:
            raise ValueError("PIT bundle differs from the sealed Task 11 authority")
        if (
            not isinstance(self.effective_policy_sha256, str)
            or _SHA256_RE.fullmatch(self.effective_policy_sha256) is None
        ):
            raise ValueError("effective policy SHA-256 is invalid")
        if self.max_api_calls != MAX_CANARY_CALLS:
            raise ValueError("PIT optimization requires exactly three calls")
        if (
            isinstance(self.max_usd, bool)
            or type(self.max_usd) not in {int, float}
            or not math.isfinite(float(self.max_usd))
            or float(self.max_usd) != MAX_CANARY_USD
        ):
            raise ValueError("PIT optimization requires an exact USD 0.50 ceiling")
        if self.max_iterations != 1:
            raise ValueError("PIT optimization requires exactly one iteration")
        if self.apply is not False:
            raise ValueError("PIT optimization is apply=false only")
        if type(self.verification_subset) is not bool:
            raise ValueError("optimization verification-subset flag must be boolean")
        if self.phase == "prepare" and self.readiness_sha256 is not None:
            raise ValueError("prepare cannot trust a prior readiness identity")
        if self.phase == "canary" and (
            not isinstance(self.readiness_sha256, str)
            or _SHA256_RE.fullmatch(self.readiness_sha256) is None
        ):
            raise ValueError("canary requires the exact readiness SHA-256")


def _numeric_series(frame: pd.DataFrame, field: str, *, positive: bool = False) -> pd.Series:
    if field not in frame:
        raise ValueError(f"frame lacks {field}")
    result = pd.to_numeric(frame[field], errors="raise").astype(float)
    if result.empty or not result.map(math.isfinite).all():
        raise ValueError(f"{field} contains non-finite values")
    if positive and (result <= 0.0).any():
        raise ValueError(f"{field} must be positive")
    return result


def _window_frame(
    frame: pd.DataFrame,
    date_field: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    if date_field not in frame:
        raise ValueError(f"frame lacks {date_field}")
    result = frame.copy()
    result[date_field] = pd.to_datetime(result[date_field], errors="raise").dt.normalize()
    if result[date_field].duplicated().any() or not result[date_field].is_monotonic_increasing:
        raise ValueError(f"{date_field} must be unique and monotonic")
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    result = result.loc[(result[date_field] >= start) & (result[date_field] <= end)].copy()
    if result.empty:
        raise ValueError("window contains no observations")
    return result


def aggregate_equity_window(
    frame: pd.DataFrame,
    *,
    start_date: str,
    end_date: str,
    closed_trades: int,
) -> dict[str, float | int]:
    """Return strategy and benchmark performance using production report semantics."""

    if type(closed_trades) is not int or closed_trades < 0:
        raise ValueError("closed trades must be a nonnegative integer")
    selected = _window_frame(frame, "date", start_date, end_date)
    portfolio_values = _numeric_series(selected, "portfolio", positive=True)
    benchmark_values = _numeric_series(selected, "benchmark", positive=True)
    if len(selected) < 2:
        raise ValueError("equity window requires at least two observations")
    index = pd.DatetimeIndex(selected["date"])
    portfolio = pd.Series(portfolio_values.to_numpy(), index=index)
    benchmark = pd.Series(benchmark_values.to_numpy(), index=index)
    from core.backtest_engine import PerformanceReport

    strategy = PerformanceReport.compute_metrics(portfolio)
    reference = PerformanceReport.compute_metrics(benchmark)
    values: dict[str, float | int] = {
        "total_return_pct": float(strategy["total_return_pct"]),
        "annualized_return_pct": float(strategy["annualized_return_pct"]),
        "max_drawdown_pct": float(strategy["max_drawdown_pct"]),
        "sharpe_ratio": float(strategy["sharpe_ratio"]),
        "benchmark_total_return_pct": float(reference["total_return_pct"]),
        "benchmark_annualized_return_pct": float(reference["annualized_return_pct"]),
        "benchmark_max_drawdown_pct": float(reference["max_drawdown_pct"]),
        "benchmark_sharpe_ratio": float(reference["sharpe_ratio"]),
        "excess_return_pct": float(strategy["total_return_pct"] - reference["total_return_pct"]),
        "excess_annualized_return_pct": float(
            strategy["annualized_return_pct"] - reference["annualized_return_pct"]
        ),
        "starting_equity": float(portfolio.iloc[0]),
        "ending_equity": float(portfolio.iloc[-1]),
        "total_pnl": float(portfolio.iloc[-1] - portfolio.iloc[0]),
        "equity_observations": int(len(portfolio)),
        "closed_trades": closed_trades,
    }
    values["objective"] = float(
        values["annualized_return_pct"]
        - abs(min(float(values["max_drawdown_pct"]), 0.0))
    )
    if any(
        isinstance(value, float) and not math.isfinite(value) for value in values.values()
    ):
        raise ValueError("equity aggregation produced a non-finite metric")
    return values


@dataclass(slots=True)
class _OpenLot:
    entry_date: pd.Timestamp
    entry_price: float
    quantity: float
    sold: float = 0.0
    proceeds: float = 0.0


@dataclass(frozen=True, slots=True)
class _CompletedLot:
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    return_pct: float
    exit_reason: str
    holding_days: int
    holding_sessions: int


def _quantity_tolerance(quantity: float) -> float:
    return max(5e-6, abs(quantity) * 1e-8)


def _completed_lots(
    transactions: pd.DataFrame,
    sessions: pd.DatetimeIndex,
    *,
    end_date: str,
) -> tuple[tuple[_CompletedLot, ...], int, dict[str, float | int]]:
    required = {"Date", "Ticker", "Action", "Price", "Quantity", "Reason"}
    if not isinstance(transactions, pd.DataFrame) or not required.issubset(transactions):
        raise ValueError("transactions lack the optimizer ledger schema")
    frame = transactions.copy()
    frame["Date"] = pd.to_datetime(frame["Date"], errors="raise").dt.normalize()
    if not frame["Date"].is_monotonic_increasing:
        raise ValueError("transactions are not chronological")
    frame = frame.loc[frame["Date"] <= pd.Timestamp(end_date)]
    prices = pd.to_numeric(frame["Price"], errors="raise").astype(float)
    quantities = pd.to_numeric(frame["Quantity"], errors="raise").astype(float)
    if (
        not prices.map(math.isfinite).all()
        or not quantities.map(math.isfinite).all()
        or (prices <= 0.0).any()
        or (quantities <= 0.0).any()
    ):
        raise ValueError("transactions contain invalid prices or quantities")
    frame["Price"] = prices
    frame["Quantity"] = quantities
    session_index = pd.DatetimeIndex(pd.to_datetime(sessions, errors="raise")).normalize()
    if session_index.has_duplicates or not session_index.is_monotonic_increasing:
        raise ValueError("equity sessions must be unique and monotonic")
    open_lots: dict[str, list[_OpenLot]] = {}
    completed: list[_CompletedLot] = []
    scale_count = 0
    scale_quantity = 0.0
    scale_proceeds = 0.0
    for row in frame.itertuples(index=False):
        symbol = str(row.Ticker).strip().upper()
        action = str(row.Action).strip().upper()
        reason = str(row.Reason).strip()
        when = pd.Timestamp(row.Date).normalize()
        price = float(row.Price)
        quantity = float(row.Quantity)
        if not symbol or action not in {"BUY", "SELL"} or not reason:
            raise ValueError("transaction identity/action/reason is invalid")
        if action == "BUY":
            open_lots.setdefault(symbol, []).append(_OpenLot(when, price, quantity))
            continue
        if reason == "take_profit_scale_out":
            scale_count += 1
            scale_quantity += quantity
            scale_proceeds += quantity * price
        lots = open_lots.get(symbol)
        if not lots:
            raise ValueError("SELL transaction has no open lot")
        remaining = quantity
        while remaining > 0.0 and lots:
            lot = lots[0]
            available = max(lot.quantity - lot.sold, 0.0)
            if available <= _quantity_tolerance(lot.quantity):
                raise ValueError("transaction ledger contains a residual lot")
            sold_now = min(remaining, available)
            lot.proceeds += sold_now * price
            lot.sold += sold_now
            remaining -= sold_now
            if lot.quantity - lot.sold <= _quantity_tolerance(lot.quantity):
                return_pct = (lot.proceeds / (lot.entry_price * lot.quantity) - 1.0) * 100.0
                holding_sessions = max(
                    int(((session_index >= lot.entry_date) & (session_index <= when)).sum()) - 1,
                    0,
                )
                completed.append(
                    _CompletedLot(
                        entry_date=lot.entry_date,
                        exit_date=when,
                        return_pct=return_pct,
                        exit_reason=reason,
                        holding_days=max((when - lot.entry_date).days, 0),
                        holding_sessions=holding_sessions,
                    )
                )
                lots.pop(0)
        if remaining > _quantity_tolerance(quantity):
            raise ValueError("SELL quantity exceeds the open lots")
        if not lots:
            del open_lots[symbol]
    open_count = sum(
        1
        for lots in open_lots.values()
        for lot in lots
        if lot.quantity - lot.sold > _quantity_tolerance(lot.quantity)
    )
    return (
        tuple(completed),
        open_count,
        {
            "sell_count": scale_count,
            "quantity": scale_quantity,
            "proceeds": scale_proceeds,
        },
    )


def aggregate_transaction_window(
    transactions: pd.DataFrame,
    *,
    sessions: pd.DatetimeIndex,
    start_date: str,
    end_date: str,
) -> dict[str, object]:
    """Reconstruct position-level outcomes while separating scale-out SELL rows."""

    all_completed, open_count, scale_all = _completed_lots(
        transactions, sessions, end_date=end_date
    )
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    completed = tuple(lot for lot in all_completed if start <= lot.exit_date <= end)
    frame = transactions.copy()
    frame["Date"] = pd.to_datetime(frame["Date"], errors="raise").dt.normalize()
    selected_scale = frame.loc[
        (frame["Date"] >= start)
        & (frame["Date"] <= end)
        & frame["Action"].astype(str).str.upper().eq("SELL")
        & frame["Reason"].astype(str).eq("take_profit_scale_out")
    ]
    if len(selected_scale) == int(scale_all["sell_count"]):
        scale = scale_all
    else:
        selected_qty = pd.to_numeric(selected_scale["Quantity"], errors="raise").astype(float)
        selected_price = pd.to_numeric(selected_scale["Price"], errors="raise").astype(float)
        scale = {
            "sell_count": int(len(selected_scale)),
            "quantity": float(selected_qty.sum()),
            "proceeds": float((selected_qty * selected_price).sum()),
        }
    returns = [lot.return_pct for lot in completed]
    wins = [value for value in returns if value > 0.0]
    losses = [value for value in returns if value <= 0.0]
    holding_days = [lot.holding_days for lot in completed]
    holding_sessions = [lot.holding_sessions for lot in completed]
    exits: dict[str, dict[str, float | int]] = {}
    for reason in sorted({lot.exit_reason for lot in completed}):
        values = [lot.return_pct for lot in completed if lot.exit_reason == reason]
        reason_wins = sum(value > 0.0 for value in values)
        exits[reason] = {
            "closed_trades": len(values),
            "wins": reason_wins,
            "win_rate_pct": 0.0 if not values else reason_wins * 100.0 / len(values),
            "average_return_pct": mean(values) if values else 0.0,
        }
    return {
        "closed_trades": len(completed),
        "open_trades": open_count,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": 0.0 if not returns else len(wins) * 100.0 / len(returns),
        "average_return_pct": mean(returns) if returns else 0.0,
        "median_return_pct": median(returns) if returns else 0.0,
        "average_win_pct": mean(wins) if wins else 0.0,
        "average_loss_pct": mean(losses) if losses else 0.0,
        "expectancy_pct": mean(returns) if returns else 0.0,
        "average_holding_days": mean(holding_days) if holding_days else 0.0,
        "median_holding_days": median(holding_days) if holding_days else 0.0,
        "average_holding_sessions": mean(holding_sessions) if holding_sessions else 0.0,
        "median_holding_sessions": median(holding_sessions) if holding_sessions else 0.0,
        "exit_attribution": exits,
        "scale_out_attribution": scale,
    }


def aggregate_weekly_window(
    weekly: pd.DataFrame,
    *,
    start_date: str,
    end_date: str,
) -> dict[str, float | int]:
    required = {"Week_Ending", "Cash", "Market_Value", "Total_Equity", "Holding_Count"}
    if not isinstance(weekly, pd.DataFrame) or not required.issubset(weekly):
        raise ValueError("weekly holdings lack the optimizer schema")
    selected = _window_frame(weekly, "Week_Ending", start_date, end_date)
    cash = _numeric_series(selected, "Cash")
    market = _numeric_series(selected, "Market_Value")
    equity = _numeric_series(selected, "Total_Equity", positive=True)
    holdings = pd.to_numeric(selected["Holding_Count"], errors="raise")
    if (
        (cash < 0.0).any()
        or (market < 0.0).any()
        or (cash > equity + 1e-6).any()
        or any(isinstance(value, bool) or int(value) != value or value < 0 for value in holdings)
    ):
        raise ValueError("weekly holdings contain invalid accounting")
    cash_pct = cash / equity * 100.0
    exposure_pct = market / equity * 100.0
    return {
        "observations": int(len(selected)),
        "average_cash_pct": float(cash_pct.mean()),
        "minimum_cash_pct": float(cash_pct.min()),
        "maximum_cash_pct": float(cash_pct.max()),
        "average_exposure_pct": float(exposure_pct.mean()),
        "average_holding_count": float(holdings.mean()),
        "maximum_holding_count": int(holdings.max()),
    }


def _strict_bool_count(frame: pd.DataFrame, field: str) -> pd.Series:
    if field not in frame:
        raise ValueError(f"signal frame lacks {field}")
    values: list[bool] = []
    for value in frame[field]:
        if isinstance(value, bool) or value.__class__.__name__ == "bool_":
            values.append(bool(value))
        elif isinstance(value, str) and value in {"True", "False"}:
            values.append(value == "True")
        else:
            raise ValueError(f"signal field {field} is not strict boolean data")
    return pd.Series(values, index=frame.index, dtype=bool)


def aggregate_signal_funnel(
    signals: pd.DataFrame,
    outcomes: pd.DataFrame,
    *,
    start_date: str,
    end_date: str,
    min_current_growth: float,
    min_annual_growth: float,
    min_rs_score: float,
    min_composite_score: float,
) -> dict[str, object]:
    required = {
        "signal_date",
        "technical_setup_eligible",
        "current_growth",
        "annual_growth",
        "rs_score",
        "entry_composite_score",
        "entry_contract_eligible",
    }
    if not isinstance(signals, pd.DataFrame) or not required.issubset(signals):
        raise ValueError("signal frame lacks optimizer funnel fields")
    selected = signals.copy()
    selected["signal_date"] = pd.to_datetime(selected["signal_date"], errors="raise").dt.normalize()
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    selected = selected.loc[
        (selected["signal_date"] >= start) & (selected["signal_date"] <= end)
    ]
    technical = _strict_bool_count(selected, "technical_setup_eligible")
    current = pd.to_numeric(selected["current_growth"], errors="coerce")
    annual = pd.to_numeric(selected["annual_growth"], errors="coerce")
    rs = pd.to_numeric(selected["rs_score"], errors="coerce")
    composite = pd.to_numeric(selected["entry_composite_score"], errors="coerce")
    current_pass = technical & current.ge(min_current_growth).fillna(False)
    annual_pass = current_pass & annual.ge(min_annual_growth).fillna(False)
    rs_pass = annual_pass & rs.ge(min_rs_score).fillna(False)
    composite_pass = rs_pass & composite.ge(min_composite_score).fillna(False)
    qualified = _strict_bool_count(selected, "entry_contract_eligible")
    outcome_frame = outcomes.copy()
    required_outcomes = {"signal_date", "outcome"}
    if not required_outcomes.issubset(outcome_frame):
        raise ValueError("entry outcomes lack optimizer funnel fields")
    outcome_frame["signal_date"] = pd.to_datetime(
        outcome_frame["signal_date"], errors="raise"
    ).dt.normalize()
    outcome_frame = outcome_frame.loc[
        (outcome_frame["signal_date"] >= start)
        & (outcome_frame["signal_date"] <= end)
    ]
    observed = outcome_frame["outcome"].astype(str)
    if not set(observed).issubset(_ENTRY_OUTCOMES):
        raise ValueError("entry outcomes contain an unknown category")
    outcome_counts = {name: int(observed.eq(name).sum()) for name in _ENTRY_OUTCOMES}
    executed = outcome_counts.get("entries_executed", 0)
    rejected = len(outcome_frame) - executed
    return {
        "evaluated": int(len(selected)),
        "technical_setup": int(technical.sum()),
        "current_growth_pass": int(current_pass.sum()),
        "annual_growth_pass": int(annual_pass.sum()),
        "rs_pass": int(rs_pass.sum()),
        "composite_pass": int(composite_pass.sum()),
        "qualified": int(qualified.sum()),
        "attempted": int(len(outcome_frame)),
        "executed": int(executed),
        "rejected": int(rejected),
        "outcomes": outcome_counts,
    }


def _aggregate_signal_file(
    path: Path,
    outcomes: pd.DataFrame,
    *,
    start_date: str,
    end_date: str,
    thresholds: Mapping[str, float],
) -> dict[str, object]:
    columns = [
        "signal_date",
        "technical_setup_eligible",
        "current_growth",
        "annual_growth",
        "rs_score",
        "entry_composite_score",
        "entry_contract_eligible",
    ]
    totals = {
        "evaluated": 0,
        "technical_setup": 0,
        "current_growth_pass": 0,
        "annual_growth_pass": 0,
        "rs_pass": 0,
        "composite_pass": 0,
        "qualified": 0,
    }
    for chunk in pd.read_csv(path, usecols=columns, chunksize=100_000, keep_default_na=True):
        empty_outcomes = pd.DataFrame(columns=["signal_date", "outcome"])
        result = aggregate_signal_funnel(
            chunk,
            empty_outcomes,
            start_date=start_date,
            end_date=end_date,
            min_current_growth=thresholds["min_current_growth"],
            min_annual_growth=thresholds["min_annual_growth"],
            min_rs_score=thresholds["min_rs_score"],
            min_composite_score=thresholds["min_entry_composite_score"],
        )
        for field in totals:
            totals[field] += int(result[field])
    outcome_frame = outcomes.copy()
    outcome_frame["signal_date"] = pd.to_datetime(
        outcome_frame["signal_date"], errors="raise"
    ).dt.normalize()
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    outcome_frame = outcome_frame.loc[
        (outcome_frame["signal_date"] >= start)
        & (outcome_frame["signal_date"] <= end)
    ]
    observed = outcome_frame["outcome"].astype(str)
    if not set(observed).issubset(_ENTRY_OUTCOMES):
        raise ValueError("entry outcomes contain an unknown category")
    counts = {name: int(observed.eq(name).sum()) for name in _ENTRY_OUTCOMES}
    totals.update(
        {
            "attempted": int(len(outcome_frame)),
            "executed": counts.get("entries_executed", 0),
            "rejected": int(len(outcome_frame) - counts.get("entries_executed", 0)),
            "outcomes": counts,
        }
    )
    return totals


def _policy_projection(policy: Mapping[str, object]) -> dict[str, object]:
    entry = policy.get("entry_policy")
    causal = policy.get("causal_invariants")
    if not isinstance(entry, Mapping) or not isinstance(causal, Mapping):
        raise ValueError("effective policy lacks optimizer projection")
    fields = sorted({candidate.policy_field for candidate in candidate_catalog().values()})
    projected_entry: dict[str, object] = {}
    for field in fields:
        value = entry.get(field)
        if not isinstance(value, Mapping):
            raise ValueError(f"effective policy lacks candidate field {field}")
        projected_entry[field] = dict(value)
    return {
        "entry_policy": projected_entry,
        "causal_invariants": json.loads(json.dumps(causal, allow_nan=False)),
    }


def _policy_thresholds(policy: Mapping[str, object]) -> dict[str, float]:
    projection = _policy_projection(policy)
    entry = projection["entry_policy"]
    assert isinstance(entry, Mapping)
    values: dict[str, float] = {}
    for field, record in entry.items():
        if not isinstance(record, Mapping):
            raise ValueError("effective entry policy is malformed")
        raw = record.get("value")
        if isinstance(raw, bool) or type(raw) not in {int, float} or not math.isfinite(float(raw)):
            raise ValueError("effective entry policy contains a non-finite value")
        values[str(field)] = float(raw)
    return values


def _verify_policy_catalog(policy: Mapping[str, object]) -> None:
    projection = _policy_projection(policy)
    entry = projection["entry_policy"]
    assert isinstance(entry, Mapping)
    for definition in candidate_catalog().values():
        record = entry[definition.policy_field]
        if not isinstance(record, Mapping) or (
            record.get("classification") != "active_fixed_policy"
            or record.get("optimizer_candidate") is not True
            or record.get("source")
            != f"core.canslim.entry_contract.{definition.constant_name}"
            or float(record.get("value")) != definition.old_value
        ):
            raise ValueError("effective policy does not authorize the candidate catalog")


def _baseline_observation(
    baseline_run: Path,
    *,
    policy: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, str]]:
    from core.pit_diagnosis.baseline import (
        resolve_baseline_authority_profile,
        verify_baseline_run,
    )

    profile = resolve_baseline_authority_profile("strict-proper-base-task11")
    snapshot = verify_baseline_run(baseline_run, profile.authority)
    if (
        snapshot.manifest_sha256 != BASELINE_MANIFEST_SHA256
        or snapshot.bundle_sha256 != PIT_BUNDLE_SHA256
        or snapshot.source_commit != BASELINE_SOURCE_COMMIT
    ):
        raise ValueError("verified baseline identity differs from Task 11")
    run = snapshot.run_dir
    equity = pd.read_csv(run / "equity_curve.csv", keep_default_na=False)
    transactions = pd.read_csv(run / "transactions.csv", keep_default_na=False)
    weekly = pd.read_csv(run / "weekly_holdings.csv", keep_default_na=False)
    outcomes = pd.read_csv(run / "entry_attempt_outcomes.csv", keep_default_na=False)
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    sessions = pd.DatetimeIndex(pd.to_datetime(equity["date"], errors="raise")).normalize()
    full_trades = aggregate_transaction_window(
        transactions,
        sessions=sessions,
        start_date=FULL_START_DATE,
        end_date=FULL_END_DATE,
    )
    holdout_trades = aggregate_transaction_window(
        transactions,
        sessions=sessions,
        start_date=HOLDOUT_START_DATE,
        end_date=HOLDOUT_END_DATE,
    )
    full_performance = aggregate_equity_window(
        equity,
        start_date=FULL_START_DATE,
        end_date=FULL_END_DATE,
        closed_trades=int(full_trades["closed_trades"]),
    )
    holdout_performance = aggregate_equity_window(
        equity,
        start_date=HOLDOUT_START_DATE,
        end_date=HOLDOUT_END_DATE,
        closed_trades=int(holdout_trades["closed_trades"]),
    )
    for field, expected in _EXPECTED_BASELINE_METRICS.items():
        actual = (
            aggregate_weekly_window(
                weekly, start_date=FULL_START_DATE, end_date=FULL_END_DATE
            )["average_cash_pct"]
            if field == "average_cash_pct"
            else full_performance[field]
        )
        if isinstance(expected, int):
            matches = actual == expected
        else:
            matches = math.isclose(float(actual), float(expected), rel_tol=1e-12, abs_tol=1e-12)
        if not matches:
            raise ValueError(f"sealed baseline metric differs: {field}")
    thresholds = _policy_thresholds(policy)
    signals_path = _regular_file(run / "canslim_signals.csv", "baseline signal ledger")
    if _sha256_file(signals_path) != profile.authority.artifact_sha256["canslim_signals.csv"]:
        raise ValueError("baseline signal ledger hash changed before funnel aggregation")
    full_funnel = _aggregate_signal_file(
        signals_path,
        outcomes,
        start_date=FULL_START_DATE,
        end_date=FULL_END_DATE,
        thresholds=thresholds,
    )
    holdout_funnel = _aggregate_signal_file(
        signals_path,
        outcomes,
        start_date=HOLDOUT_START_DATE,
        end_date=HOLDOUT_END_DATE,
        thresholds=thresholds,
    )
    for field, expected in _EXPECTED_BASELINE_FUNNEL.items():
        if field == "next_open_buy_zone_rejections":
            actual = full_funnel["outcomes"].get("entry_rejected_next_open_buy_zone", 0)
        else:
            actual = full_funnel[field]
        if actual != expected:
            raise ValueError(f"sealed baseline funnel differs: {field}")
    full_weekly = aggregate_weekly_window(
        weekly, start_date=FULL_START_DATE, end_date=FULL_END_DATE
    )
    holdout_weekly = aggregate_weekly_window(
        weekly, start_date=HOLDOUT_START_DATE, end_date=HOLDOUT_END_DATE
    )
    leader_basket = summary.get("leader_basket")
    if not isinstance(leader_basket, dict):
        raise ValueError("sealed baseline lacks leader-basket metrics")
    observation = {
        "full": {
            "performance": full_performance,
            "trades": full_trades,
            "weekly": full_weekly,
            "funnel": full_funnel,
        },
        "holdout": {
            "performance": holdout_performance,
            "trades": holdout_trades,
            "weekly": holdout_weekly,
            "funnel": holdout_funnel,
        },
        "leader_basket": leader_basket,
    }
    sealed = {"run_manifest.json": snapshot.manifest_sha256}
    sealed.update(
        {str(name): str(digest) for name, digest in snapshot.artifact_sha256.items()}
    )
    return observation, sealed


def verify_sealed_baseline_artifacts(
    baseline_run: Path,
    expected: Mapping[str, str],
) -> None:
    """Reauthenticate the exhaustive sealed Task 11 artifact set."""

    run = _regular_directory(baseline_run, "sealed baseline run")
    if not isinstance(expected, Mapping) or not expected:
        raise ValueError("sealed baseline artifact identities are absent")
    for name, digest in sorted(expected.items()):
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
        ):
            raise ValueError("sealed baseline artifact identity is invalid")
        path = _regular_file(run / name, f"sealed baseline artifact {name}")
        if _sha256_file(path) != digest:
            raise ValueError(f"sealed baseline artifact changed: {name}")


_VERIFICATION_SCOPE_KEYS = frozenset(
    {
        "benchmark",
        "known_activity_symbols",
        "known_entry_attempts",
        "measurement_end",
        "measurement_start",
        "selection",
        "session_count",
        "symbol_count",
        "symbols",
        "warmup_start",
    }
)


def _validate_verification_scope(scope: Mapping[str, object]) -> dict[str, object]:
    """Return one closed, canonical verification-only evaluator scope."""

    value = _json_primitive(scope)
    if not isinstance(value, dict) or set(value) != _VERIFICATION_SCOPE_KEYS:
        raise ValueError("optimization verification scope keys are not exact")
    symbols = value.get("symbols")
    if (
        not isinstance(symbols, list)
        or len(symbols) != _VERIFICATION_SYMBOL_COUNT
        or len(set(symbols)) != len(symbols)
        or any(
            not isinstance(symbol, str)
            or re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", symbol) is None
            or symbol == "SPY"
            for symbol in symbols
        )
    ):
        raise ValueError("optimization verification symbols are invalid")
    if (
        value.get("benchmark") != "SPY"
        or value.get("warmup_start") != FULL_START_DATE
        or value.get("selection")
        != "sealed_entry_activity_then_hash_ranked_active_fill"
        or value.get("session_count") != _VERIFICATION_SESSION_COUNT
        or value.get("symbol_count") != _VERIFICATION_SYMBOL_COUNT
    ):
        raise ValueError("optimization verification scope contract changed")
    for field in ("known_activity_symbols", "known_entry_attempts"):
        count = value.get(field)
        if type(count) is not int or count < 1:
            raise ValueError(f"optimization verification {field} must be positive")
    if int(value["known_activity_symbols"]) > _VERIFICATION_SYMBOL_COUNT:
        raise ValueError("optimization verification activity symbols exceed the subset")
    try:
        measurement_start = pd.Timestamp(value["measurement_start"]).normalize()
        measurement_end = pd.Timestamp(value["measurement_end"]).normalize()
    except (TypeError, ValueError) as exc:
        raise ValueError("optimization verification dates are invalid") from exc
    if (
        measurement_start < pd.Timestamp(FULL_START_DATE)
        or measurement_end > pd.Timestamp(FULL_END_DATE)
        or measurement_start > measurement_end
        or measurement_start.date().isoformat() != value["measurement_start"]
        or measurement_end.date().isoformat() != value["measurement_end"]
    ):
        raise ValueError("optimization verification dates escape the sealed window")
    return value


def _build_verification_scope(bundle: object, baseline_run: Path) -> dict[str, object]:
    """Derive a small, active slice solely from authenticated local inputs."""

    attempts_path = _regular_file(
        baseline_run / "entry_attempt_outcomes.csv", "baseline entry attempts"
    )
    attempts = pd.read_csv(attempts_path, dtype=str, keep_default_na=False)
    expected_columns = {
        "symbol",
        "signal_date",
        "entry_date",
        "pivot",
        "buy_zone_lower",
        "buy_zone_upper",
        "entry_open",
        "outcome",
    }
    if attempts.empty or set(attempts.columns) != expected_columns:
        raise ValueError("baseline entry attempts cannot define verification activity")
    attempts["symbol"] = attempts["symbol"].str.upper()
    try:
        attempts["signal_date"] = pd.to_datetime(
            attempts["signal_date"], errors="raise"
        ).dt.normalize()
    except (TypeError, ValueError) as exc:
        raise ValueError("baseline entry-attempt dates are invalid") from exc
    first_activity = attempts["signal_date"].min()
    fetch_closes = getattr(bundle, "fetch_closes", None)
    members_at = getattr(bundle, "members_at", None)
    symbols_method = getattr(bundle, "symbols", None)
    if not all(callable(value) for value in (fetch_closes, members_at, symbols_method)):
        raise ValueError("PIT bundle cannot derive a verification scope")
    benchmark = fetch_closes(
        ["SPY"], pd.Timestamp(FULL_START_DATE), pd.Timestamp(FULL_END_DATE)
    )
    if "SPY" not in benchmark:
        raise ValueError("PIT bundle lacks the verification benchmark")
    sessions = pd.DatetimeIndex(benchmark["SPY"].dropna().index).normalize()
    measurement_sessions = sessions[sessions >= first_activity][
        :_VERIFICATION_SESSION_COUNT
    ]
    if len(measurement_sessions) != _VERIFICATION_SESSION_COUNT:
        raise ValueError("PIT bundle lacks sixty verification sessions")
    measurement_start = measurement_sessions[0]
    measurement_end = measurement_sessions[-1]
    active_attempts = attempts.loc[
        attempts["signal_date"].between(measurement_start, measurement_end)
    ]
    attempt_counts = active_attempts.groupby("symbol", sort=True).size()
    ranked_activity = sorted(
        (str(symbol) for symbol in attempt_counts.index),
        key=lambda symbol: (-int(attempt_counts[symbol]), symbol),
    )
    active_members = {
        str(symbol)
        for symbol in members_at(measurement_start)
        if str(symbol) != "SPY"
    }
    available = {
        str(symbol)
        for symbol in symbols_method()
        if str(symbol) != "SPY"
    }
    candidate_pool = sorted(active_members & available)
    closes = fetch_closes(
        candidate_pool, pd.Timestamp(FULL_START_DATE), measurement_end
    )
    minimum_warmup_bars = 100
    covered = {
        symbol
        for symbol in candidate_pool
        if symbol in closes and int(closes[symbol].count()) >= minimum_warmup_bars
    }
    if any(symbol not in covered for symbol in ranked_activity):
        raise ValueError("known verification activity lacks sufficient warm-up prices")
    fill = sorted(
        covered - set(ranked_activity),
        key=lambda symbol: _sha256_bytes(
            f"{PIT_BUNDLE_SHA256}:{symbol}".encode("ascii")
        ),
    )
    selected = [*ranked_activity, *fill][:_VERIFICATION_SYMBOL_COUNT]
    if len(selected) != _VERIFICATION_SYMBOL_COUNT:
        raise ValueError("PIT bundle lacks enough covered verification symbols")
    return _validate_verification_scope(
        {
            "benchmark": "SPY",
            "known_activity_symbols": len(ranked_activity),
            "known_entry_attempts": int(len(active_attempts)),
            "measurement_end": measurement_end.date().isoformat(),
            "measurement_start": measurement_start.date().isoformat(),
            "selection": "sealed_entry_activity_then_hash_ranked_active_fill",
            "session_count": _VERIFICATION_SESSION_COUNT,
            "symbol_count": _VERIFICATION_SYMBOL_COUNT,
            "symbols": selected,
            "warmup_start": FULL_START_DATE,
        }
    )


def _evaluation_contract(
    *, verification_subset: bool, verification_scope: Mapping[str, object] | None
) -> dict[str, object]:
    if verification_subset:
        if verification_scope is None:
            raise ValueError("optimization verification scope is absent")
        scope = _validate_verification_scope(verification_scope)
        mode = "verification_subset"
    else:
        if verification_scope is not None:
            raise ValueError("full optimization cannot carry a verification scope")
        scope = {
            "full_end": FULL_END_DATE,
            "full_start": FULL_START_DATE,
            "holdout_end": HOLDOUT_END_DATE,
            "holdout_start": HOLDOUT_START_DATE,
        }
        mode = "full_acceptance"
    return {
        "mode": mode,
        "performance_acceptance_eligible": not verification_subset,
        "scope": scope,
        "scope_sha256": _sha256_bytes(_canonical_json_bytes(scope)),
        "verification_only": verification_subset,
    }


@dataclass(frozen=True, slots=True)
class PitOptimizationReadiness:
    readiness_sha256: str
    artifact_path: Path
    artifact_sha256: str
    effective_policy_sha256: str
    provider_payload: Mapping[str, object]
    primitive: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class PitOptimizationLoopResult:
    """Closed controller result for either preparation or one completed canary."""

    phase: str
    status: str
    exit_code: int
    run_id: str
    readiness_sha256: str
    effective_policy_sha256: str
    selected_candidate_id: str | None
    accepted: bool | None
    artifact_paths: tuple[tuple[Path, str], ...]
    provider_calls: int
    spent_usd: float
    source_modified: bool
    cleanup_complete: bool
    verification_only: bool = False
    operator_lines: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.phase not in {"prepare", "canary"}:
            raise ValueError("optimization result phase is invalid")
        if self.status not in {"ready", "verified", "accepted", "rejected", "aborted"}:
            raise ValueError("optimization result status is invalid")
        if type(self.exit_code) is not int or not 0 <= self.exit_code <= 255:
            raise ValueError("optimization result exit code is invalid")
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("optimization result run ID is invalid")
        for field in ("readiness_sha256", "effective_policy_sha256"):
            if _SHA256_RE.fullmatch(getattr(self, field) or "") is None:
                raise ValueError(f"optimization result {field} is invalid")
        if self.selected_candidate_id is not None and (
            self.selected_candidate_id not in candidate_catalog()
        ):
            raise ValueError("optimization result candidate ID is invalid")
        if self.accepted is not None and type(self.accepted) is not bool:
            raise ValueError("optimization result acceptance is invalid")
        if type(self.provider_calls) is not int or not 0 <= self.provider_calls <= 3:
            raise ValueError("optimization result provider-call count is invalid")
        if (
            isinstance(self.spent_usd, bool)
            or type(self.spent_usd) not in {int, float}
            or not math.isfinite(float(self.spent_usd))
            or not 0.0 <= float(self.spent_usd) <= MAX_CANARY_USD
        ):
            raise ValueError("optimization result spend is invalid")
        if type(self.source_modified) is not bool or type(self.cleanup_complete) is not bool:
            raise ValueError("optimization result cleanup/source facts are invalid")
        if type(self.verification_only) is not bool:
            raise ValueError("optimization result verification-only fact is invalid")
        for path, digest in self.artifact_paths:
            if not isinstance(path, Path) or not path.is_absolute() or (
                _SHA256_RE.fullmatch(digest or "") is None
            ):
                raise ValueError("optimization result artifact identity is invalid")
        if not isinstance(self.operator_lines, tuple) or any(
            not isinstance(line, str) or not line for line in self.operator_lines
        ):
            raise ValueError("optimization result operator output is invalid")
        if self.phase == "canary" and self.operator_lines:
            raise ValueError("canary result cannot emit a second operator command")
        if self.status in {"accepted", "rejected"} and (
            self.phase != "canary"
            or self.verification_only
            or self.exit_code != 0
            or self.provider_calls != MAX_CANARY_CALLS
            or self.selected_candidate_id is None
            or type(self.accepted) is not bool
            or len(self.artifact_paths) != 4
            or self.source_modified
            or not self.cleanup_complete
        ):
            raise ValueError(
                "successful optimization terminal result requires exactly three calls "
                "and the complete artifact set"
            )
        if self.status == "verified" and (
            self.phase != "canary"
            or not self.verification_only
            or self.exit_code != 0
            or self.provider_calls != MAX_CANARY_CALLS
            or self.selected_candidate_id is None
            or self.accepted is not None
            or len(self.artifact_paths) != 4
            or self.source_modified
            or not self.cleanup_complete
        ):
            raise ValueError(
                "successful verification result requires exactly three calls, no "
                "acceptance verdict, and the complete artifact set"
            )
        if self.status == "ready" and (
            self.phase != "prepare"
            or self.exit_code != 0
            or self.provider_calls != 0
            or self.spent_usd != 0.0
            or self.selected_candidate_id is not None
            or self.accepted is not None
            or len(self.artifact_paths) != 1
            or self.source_modified
            or not self.cleanup_complete
        ):
            raise ValueError("prepare result invariants are inconsistent")
        if self.status == "aborted" and self.exit_code == 0:
            raise ValueError("aborted optimization result must fail closed")


@dataclass(frozen=True, slots=True)
class PitOptimizationRoleCall:
    """One parsed provider payload with controller-verifiable call accounting."""

    role: str
    call_index: int
    payload: object
    cost_usd: float
    accounting_complete: bool
    audit_sha256: str

    def __post_init__(self) -> None:
        if self.role not in {"orchestrator", "reasoner", "coder"}:
            raise ValueError("optimization role-call role is invalid")
        if type(self.call_index) is not int or not 1 <= self.call_index <= MAX_CANARY_CALLS:
            raise ValueError("optimization role-call index is invalid")
        if (
            isinstance(self.cost_usd, bool)
            or type(self.cost_usd) not in {int, float}
            or not math.isfinite(float(self.cost_usd))
            or float(self.cost_usd) < 0.0
        ):
            raise ValueError("optimization role-call cost is invalid")
        if self.accounting_complete is not True:
            raise ValueError("optimization role-call accounting is incomplete")
        if _SHA256_RE.fullmatch(self.audit_sha256 or "") is None:
            raise ValueError("optimization role-call audit identity is invalid")


@dataclass(frozen=True, slots=True)
class PitOptimizationCleanup:
    source_modified: bool
    cleanup_complete: bool

    def __post_init__(self) -> None:
        if type(self.source_modified) is not bool or type(self.cleanup_complete) is not bool:
            raise ValueError("optimization cleanup facts must be boolean")


@dataclass(frozen=True, slots=True)
class PitOptimizationCanaryServices:
    """Injected paid-call, evaluator, input-authentication, and cleanup boundaries."""

    call_role: Callable[
        [str, dict[str, object], Callable[[str], object]], PitOptimizationRoleCall
    ]
    evaluate_candidate: Callable[[Path], Mapping[str, object]]
    verify_inputs: Callable[[], None]
    cleanup: Callable[[], PitOptimizationCleanup]

    def __post_init__(self) -> None:
        if not all(
            callable(value)
            for value in (
                self.call_role,
                self.evaluate_candidate,
                self.verify_inputs,
                self.cleanup,
            )
        ):
            raise ValueError("optimization canary services must be callable")


_DOMAIN_EVIDENCE_IDS = MappingProxyType(
    {
        "entry_funnel": frozenset({"metric.full.entry_funnel"}),
        "return_drawdown": frozenset(
            {"metric.full.objective", "metric.holdout.objective"}
        ),
        "cash_exposure": frozenset({"metric.full.cash"}),
        "trade_quality": frozenset({"metric.full.trade_quality"}),
    }
)


def _json_primitive(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_primitive(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_primitive(item) for item in value]
    return value


def _readiness_identity(
    readiness: PitOptimizationReadiness,
    *,
    expected_readiness_sha256: str,
    expected_effective_policy_sha256: str,
    source_root: Path,
    candidate_root: Path | None,
) -> None:
    if not isinstance(readiness, PitOptimizationReadiness):
        raise ValueError("canary requires authenticated readiness")
    if readiness.readiness_sha256 != expected_readiness_sha256:
        raise ValueError("canary readiness identity differs from the operator identity")
    if readiness.effective_policy_sha256 != expected_effective_policy_sha256:
        raise ValueError("canary effective-policy identity differs from readiness")
    primitive = _json_primitive(readiness.primitive)
    if not isinstance(primitive, dict):
        raise ValueError("canary readiness payload is malformed")
    readiness_bytes = _canonical_json_bytes(primitive)
    if _sha256_bytes(readiness_bytes) != readiness.readiness_sha256:
        raise ValueError("canary readiness payload identity changed")
    artifact = _regular_file(readiness.artifact_path, "readiness artifact")
    artifact_bytes = artifact.read_bytes()
    if (
        artifact_bytes != readiness_bytes
        or _sha256_bytes(artifact_bytes) != readiness.artifact_sha256
        or readiness.artifact_sha256 != readiness.readiness_sha256
    ):
        raise ValueError("canary readiness artifact identity changed")
    identities = primitive.get("identities")
    budget = primitive.get("budget_contract")
    if not isinstance(identities, dict) or not isinstance(budget, dict):
        raise ValueError("canary readiness contract is incomplete")
    if identities.get("effective_policy_sha256") != expected_effective_policy_sha256:
        raise ValueError("canary readiness effective policy changed")
    effective_policy = primitive.get("effective_policy")
    if not isinstance(effective_policy, Mapping):
        raise ValueError("canary readiness effective policy is absent")
    from core.engine_policy import effective_engine_policy_sha256

    if effective_engine_policy_sha256(effective_policy) != expected_effective_policy_sha256:
        raise ValueError("canary readiness effective policy digest changed")
    if budget != {
        "samples": 1,
        "iterations": 1,
        "max_calls": MAX_CANARY_CALLS,
        "max_usd": MAX_CANARY_USD,
        "apply": False,
        "provider_retries": 0,
    }:
        raise ValueError("canary readiness budget contract changed")
    evaluation_contract = primitive.get("evaluation_contract")
    if not isinstance(evaluation_contract, dict) or set(evaluation_contract) != {
        "mode",
        "performance_acceptance_eligible",
        "scope",
        "scope_sha256",
        "verification_only",
    }:
        raise ValueError("canary readiness evaluation contract is incomplete")
    verification_only = evaluation_contract.get("verification_only")
    scope = evaluation_contract.get("scope")
    if type(verification_only) is not bool or not isinstance(scope, Mapping):
        raise ValueError("canary readiness evaluation scope is malformed")
    expected_contract = _evaluation_contract(
        verification_subset=verification_only,
        verification_scope=scope if verification_only else None,
    )
    if evaluation_contract != expected_contract:
        raise ValueError("canary readiness evaluation contract changed")
    expected_source_sha = identities.get("entry_contract_source_sha256")
    if not isinstance(expected_source_sha, str) or _SHA256_RE.fullmatch(expected_source_sha) is None:
        raise ValueError("canary readiness source identity is invalid")
    source_path = _regular_directory(source_root, "source root") / ENTRY_CONTRACT_PATH
    source_identity = verify_catalog_source(source_path)
    candidate_matches = True
    if candidate_root is not None:
        candidate_path = (
            _regular_directory(candidate_root, "candidate root") / ENTRY_CONTRACT_PATH
        )
        verify_catalog_source(candidate_path)
        source_bytes = source_path.read_bytes()
        candidate_bytes = candidate_path.read_bytes()
        candidate_matches = (
            b"\r" not in candidate_bytes
            and candidate_bytes == source_bytes.replace(b"\r\n", b"\n")
        )
    if source_identity.source_sha256 != expected_source_sha or not candidate_matches:
        raise ValueError("canary source or disposable candidate input changed")


def _closed_readiness_ids(
    readiness: PitOptimizationReadiness,
    field: str,
) -> frozenset[str]:
    primitive = _json_primitive(readiness.primitive)
    if not isinstance(primitive, dict):
        raise ValueError("canary readiness payload is malformed")
    values = primitive.get(field)
    if (
        not isinstance(values, list)
        or not values
        or any(not isinstance(value, str) for value in values)
        or values != sorted(values)
        or len(values) != len(set(values))
    ):
        raise ValueError(f"canary readiness {field} are not closed")
    return frozenset(values)


def _validate_route_citations(
    route: PitOptimizationRoute,
    readiness: PitOptimizationReadiness,
) -> None:
    if route.action == "abort":
        return
    supplied = _closed_readiness_ids(readiness, "evidence_ids")
    required_domain_evidence = _DOMAIN_EVIDENCE_IDS.get(route.domain)
    cited_evidence = set(route.evidence_ids)
    if required_domain_evidence is None or not required_domain_evidence.issubset(
        cited_evidence
    ):
        raise ValueError("orchestrator domain evidence citations are invalid")
    if not cited_evidence.issubset(supplied):
        raise ValueError("orchestrator evidence citation is outside readiness")


def _validate_reasoning_citations(
    reasoning: PitOptimizationReasoning,
    route: PitOptimizationRoute,
    readiness: PitOptimizationReadiness,
) -> None:
    if reasoning.skip:
        return
    supplied_evidence = _closed_readiness_ids(readiness, "evidence_ids")
    supplied_invariants = _closed_readiness_ids(readiness, "invariant_ids")
    if not set(reasoning.evidence_ids).issubset(
        set(route.evidence_ids) & supplied_evidence
    ):
        raise ValueError("reasoner evidence citation is outside the routed evidence")
    if not set(reasoning.invariant_ids).issubset(supplied_invariants):
        raise ValueError("reasoner invariant citation is outside readiness")


def _validate_role_call(
    receipt: PitOptimizationRoleCall,
    *,
    role: str,
    expected_index: int,
    expected_payload_type: type[object],
    prior_spend: float,
) -> float:
    if not isinstance(receipt, PitOptimizationRoleCall):
        raise ValueError("optimization role call did not return closed accounting")
    if (
        receipt.role != role
        or receipt.call_index != expected_index
        or receipt.accounting_complete is not True
        or not isinstance(receipt.payload, expected_payload_type)
    ):
        raise ValueError("optimization call accounting or payload is inconsistent")
    spent = prior_spend + float(receipt.cost_usd)
    if spent > MAX_CANARY_USD + 1e-12:
        raise ValueError("optimization call accounting exceeds the USD ceiling")
    return spent


def _apply_controller_candidate(
    candidate_root: Path,
    candidate: object,
) -> tuple[bytes, str]:
    from core.pit_optimization_contract import CandidateDefinition

    if not isinstance(candidate, CandidateDefinition):
        raise ValueError("controller candidate is invalid")
    target = _regular_file(candidate_root / candidate.path, "candidate entry contract")
    raw = target.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("candidate entry contract is not UTF-8") from exc
    if "\r" in text or "\x00" in text or not text.endswith("\n"):
        raise ValueError("candidate entry contract is not canonical LF text")
    lines = text[:-1].split("\n")
    if lines.count(candidate.old_line) != 1 or candidate.new_line in lines:
        raise ValueError("candidate replacement does not have one exact source anchor")
    index = lines.index(candidate.old_line)
    lines[index] = candidate.new_line
    rewritten = ("\n".join(lines) + "\n").encode("utf-8")
    with target.open("wb") as handle:
        handle.write(rewritten)
        handle.flush()
        os.fsync(handle.fileno())
    diff = "".join(
        difflib.unified_diff(
            text.splitlines(keepends=True),
            rewritten.decode("utf-8").splitlines(keepends=True),
            fromfile=f"a/{candidate.path}",
            tofile=f"b/{candidate.path}",
            n=0,
        )
    )
    if not diff:
        raise ValueError("controller candidate produced no inert diff")
    return rewritten, diff


def _candidate_comparison(
    readiness: PitOptimizationReadiness,
    evaluation: Mapping[str, object],
    candidate: object,
) -> OptimizationComparison:
    from core.pit_optimization_contract import CandidateDefinition

    if not isinstance(candidate, CandidateDefinition) or not isinstance(evaluation, Mapping):
        raise ValueError("candidate evaluation is malformed")
    if set(evaluation) != {
        "schema_version",
        "pit_bundle_sha256",
        "effective_policy_sha256",
        "effective_policy",
        "full",
        "holdout",
    }:
        raise ValueError("candidate evaluation has open or missing keys")
    if evaluation.get("schema_version") != 1:
        raise ValueError("candidate evaluation schema is invalid")
    primitive = _json_primitive(readiness.primitive)
    assert isinstance(primitive, dict)
    identities = primitive.get("identities")
    baseline = primitive.get("baseline")
    if not isinstance(identities, dict) or not isinstance(baseline, dict):
        raise ValueError("readiness baseline is malformed")
    if evaluation.get("pit_bundle_sha256") != identities.get("pit_bundle_sha256"):
        raise ValueError("candidate evaluation used a different PIT bundle")
    policy_sha = evaluation.get("effective_policy_sha256")
    if not isinstance(policy_sha, str) or _SHA256_RE.fullmatch(policy_sha) is None:
        raise ValueError("candidate evaluation policy identity is invalid")
    baseline_policy = primitive.get("effective_policy")
    candidate_policy = evaluation.get("effective_policy")
    if not isinstance(baseline_policy, dict) or not isinstance(candidate_policy, Mapping):
        raise ValueError("candidate evaluation effective policy is malformed")
    from core.engine_policy import effective_engine_policy_sha256

    if effective_engine_policy_sha256(candidate_policy) != policy_sha:
        raise ValueError("candidate effective policy digest is not authenticated")
    if effective_engine_policy_sha256(baseline_policy) != identities.get(
        "effective_policy_sha256"
    ):
        raise ValueError("readiness effective policy digest is not authenticated")
    validate_policy_delta(baseline_policy, candidate_policy, candidate)
    baseline_full = baseline.get("full")
    baseline_holdout = baseline.get("holdout")
    candidate_full = evaluation.get("full")
    candidate_holdout = evaluation.get("holdout")
    if not all(
        isinstance(value, Mapping)
        for value in (baseline_full, baseline_holdout, candidate_full, candidate_holdout)
    ):
        raise ValueError("candidate evaluation windows are malformed")
    for window in (baseline_full, baseline_holdout, candidate_full, candidate_holdout):
        _validate_aggregate_window(window)
    return build_comparison(
        baseline_full=closed_window_metrics(baseline_full),
        candidate_full=closed_window_metrics(candidate_full),
        baseline_holdout=closed_window_metrics(baseline_holdout),
        candidate_holdout=closed_window_metrics(candidate_holdout),
    )


def _validate_verification_candidate(
    readiness: PitOptimizationReadiness,
    evaluation: Mapping[str, object],
    candidate: object,
) -> Mapping[str, object]:
    """Authenticate a subset replay without producing an acceptance verdict."""

    from core.pit_optimization_contract import CandidateDefinition
    from core.engine_policy import effective_engine_policy_sha256

    if not isinstance(candidate, CandidateDefinition) or not isinstance(evaluation, Mapping):
        raise ValueError("verification candidate evaluation is malformed")
    if set(evaluation) != {
        "effective_policy",
        "effective_policy_sha256",
        "performance_acceptance_eligible",
        "pit_bundle_sha256",
        "schema_version",
        "scope",
        "verification",
        "verification_only",
    }:
        raise ValueError("verification candidate evaluation has open or missing keys")
    if (
        evaluation.get("schema_version") != 1
        or evaluation.get("verification_only") is not True
        or evaluation.get("performance_acceptance_eligible") is not False
    ):
        raise ValueError("verification candidate eligibility markers are invalid")
    primitive = _json_primitive(readiness.primitive)
    if not isinstance(primitive, dict):
        raise ValueError("verification readiness is malformed")
    identities = primitive.get("identities")
    contract = primitive.get("evaluation_contract")
    baseline_policy = primitive.get("effective_policy")
    candidate_policy = evaluation.get("effective_policy")
    if (
        not isinstance(identities, dict)
        or not isinstance(contract, dict)
        or contract.get("verification_only") is not True
        or not isinstance(baseline_policy, dict)
        or not isinstance(candidate_policy, Mapping)
    ):
        raise ValueError("verification readiness contract is incomplete")
    if evaluation.get("pit_bundle_sha256") != identities.get("pit_bundle_sha256"):
        raise ValueError("verification candidate used a different PIT bundle")
    policy_sha = evaluation.get("effective_policy_sha256")
    if (
        not isinstance(policy_sha, str)
        or _SHA256_RE.fullmatch(policy_sha) is None
        or effective_engine_policy_sha256(candidate_policy) != policy_sha
        or effective_engine_policy_sha256(baseline_policy)
        != identities.get("effective_policy_sha256")
    ):
        raise ValueError("verification candidate policy identity is invalid")
    validate_policy_delta(baseline_policy, candidate_policy, candidate)
    expected_scope = contract.get("scope")
    observed_scope = evaluation.get("scope")
    if (
        not isinstance(expected_scope, Mapping)
        or not isinstance(observed_scope, Mapping)
        or _validate_verification_scope(observed_scope)
        != _validate_verification_scope(expected_scope)
    ):
        raise ValueError("verification candidate scope differs from readiness")
    window = evaluation.get("verification")
    if not isinstance(window, Mapping):
        raise ValueError("verification candidate aggregate is absent")
    _validate_aggregate_window(window)
    performance = window.get("performance")
    funnel = window.get("funnel")
    if (
        not isinstance(performance, Mapping)
        or performance.get("equity_observations") != _VERIFICATION_SESSION_COUNT
        or not isinstance(funnel, Mapping)
        or type(funnel.get("attempted")) is not int
        or int(funnel["attempted"]) < 1
    ):
        raise ValueError("verification candidate did not exercise known strategy activity")
    return window


_PERFORMANCE_KEYS = frozenset(
    {
        "total_return_pct",
        "annualized_return_pct",
        "max_drawdown_pct",
        "sharpe_ratio",
        "benchmark_total_return_pct",
        "benchmark_annualized_return_pct",
        "benchmark_max_drawdown_pct",
        "benchmark_sharpe_ratio",
        "excess_return_pct",
        "excess_annualized_return_pct",
        "starting_equity",
        "ending_equity",
        "total_pnl",
        "equity_observations",
        "closed_trades",
        "objective",
    }
)
_TRADE_KEYS = frozenset(
    {
        "closed_trades",
        "open_trades",
        "wins",
        "losses",
        "win_rate_pct",
        "average_return_pct",
        "median_return_pct",
        "average_win_pct",
        "average_loss_pct",
        "expectancy_pct",
        "average_holding_days",
        "median_holding_days",
        "average_holding_sessions",
        "median_holding_sessions",
        "exit_attribution",
        "scale_out_attribution",
    }
)
_WEEKLY_KEYS = frozenset(
    {
        "observations",
        "average_cash_pct",
        "minimum_cash_pct",
        "maximum_cash_pct",
        "average_exposure_pct",
        "average_holding_count",
        "maximum_holding_count",
    }
)
_FUNNEL_KEYS = frozenset(
    {
        "evaluated",
        "technical_setup",
        "current_growth_pass",
        "annual_growth_pass",
        "rs_pass",
        "composite_pass",
        "qualified",
        "attempted",
        "executed",
        "rejected",
        "outcomes",
    }
)
_OUTCOME_KEYS = frozenset(
    {
        "entries_executed",
        "entry_rejected_already_open",
        "entry_rejected_capacity",
        "entry_rejected_missing_data",
        "entry_rejected_invalid_price",
        "entry_rejected_next_open_buy_zone",
        "entry_rejected_invalid_risk",
        "entry_rejected_no_cash",
    }
)
_EXIT_REASONS = frozenset(
    {
        "end_of_test",
        "evicted",
        "stop_loss",
        "take_profit_scale_out",
        "time_stop",
        "ma_violation",
    }
)


def _aggregate_number(value: object, field: str) -> float:
    if isinstance(value, bool) or type(value) not in {int, float}:
        raise ValueError(f"candidate aggregate schema {field} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"candidate aggregate schema {field} must be finite")
    return result


def _aggregate_count(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"candidate aggregate schema {field} must be a nonnegative integer")
    return value


def _validate_aggregate_window(window: Mapping[str, object]) -> None:
    """Reject raw/open evaluator data before it can reach provider or artifacts."""

    if set(window) != {"performance", "trades", "weekly", "funnel"}:
        raise ValueError("candidate aggregate schema window keys are not exact")
    performance = window["performance"]
    trades = window["trades"]
    weekly = window["weekly"]
    funnel = window["funnel"]
    if not all(isinstance(value, Mapping) for value in (performance, trades, weekly, funnel)):
        raise ValueError("candidate aggregate schema sections must be objects")
    assert isinstance(performance, Mapping)
    assert isinstance(trades, Mapping)
    assert isinstance(weekly, Mapping)
    assert isinstance(funnel, Mapping)
    if set(performance) != _PERFORMANCE_KEYS or set(trades) != _TRADE_KEYS:
        raise ValueError("candidate aggregate schema performance/trade keys are not exact")
    if set(weekly) != _WEEKLY_KEYS or set(funnel) != _FUNNEL_KEYS:
        raise ValueError("candidate aggregate schema weekly/funnel keys are not exact")
    for key, value in performance.items():
        if key in {"equity_observations", "closed_trades"}:
            _aggregate_count(value, f"performance.{key}")
        else:
            _aggregate_number(value, f"performance.{key}")
    closed = _aggregate_count(trades["closed_trades"], "trades.closed_trades")
    wins = _aggregate_count(trades["wins"], "trades.wins")
    losses = _aggregate_count(trades["losses"], "trades.losses")
    _aggregate_count(trades["open_trades"], "trades.open_trades")
    if closed != wins + losses or performance["closed_trades"] != closed:
        raise ValueError("candidate aggregate schema trade counts are inconsistent")
    for key in _TRADE_KEYS - {
        "closed_trades",
        "open_trades",
        "wins",
        "losses",
        "exit_attribution",
        "scale_out_attribution",
    }:
        _aggregate_number(trades[key], f"trades.{key}")
    expected_win_rate = 0.0 if closed == 0 else wins * 100.0 / closed
    if not math.isclose(
        float(trades["win_rate_pct"]), expected_win_rate, rel_tol=0.0, abs_tol=1e-9
    ):
        raise ValueError("candidate aggregate schema win rate is inconsistent")
    if not math.isclose(
        float(performance["objective"]),
        float(performance["annualized_return_pct"])
        - abs(min(float(performance["max_drawdown_pct"]), 0.0)),
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        raise ValueError("candidate aggregate schema objective is inconsistent")
    if not math.isclose(
        float(performance["total_pnl"]),
        float(performance["ending_equity"]) - float(performance["starting_equity"]),
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        raise ValueError("candidate aggregate schema P&L is inconsistent")
    exit_attribution = trades["exit_attribution"]
    if not isinstance(exit_attribution, Mapping) or not set(exit_attribution).issubset(
        _EXIT_REASONS
    ):
        raise ValueError("candidate aggregate schema exit attribution is open")
    exit_total = 0
    for reason, record in exit_attribution.items():
        if not isinstance(record, Mapping) or set(record) != {
            "closed_trades",
            "wins",
            "win_rate_pct",
            "average_return_pct",
        }:
            raise ValueError("candidate aggregate schema exit record is invalid")
        reason_closed = _aggregate_count(record["closed_trades"], f"exit.{reason}.closed")
        reason_wins = _aggregate_count(record["wins"], f"exit.{reason}.wins")
        if reason_wins > reason_closed:
            raise ValueError("candidate aggregate schema exit wins exceed trades")
        _aggregate_number(record["win_rate_pct"], f"exit.{reason}.win_rate")
        _aggregate_number(record["average_return_pct"], f"exit.{reason}.return")
        exit_total += reason_closed
    if exit_total != closed:
        raise ValueError("candidate aggregate schema exit totals are inconsistent")
    scale = trades["scale_out_attribution"]
    if not isinstance(scale, Mapping) or set(scale) != {"sell_count", "quantity", "proceeds"}:
        raise ValueError("candidate aggregate schema scale-out record is invalid")
    _aggregate_count(scale["sell_count"], "scale_out.sell_count")
    if _aggregate_number(scale["quantity"], "scale_out.quantity") < 0 or _aggregate_number(
        scale["proceeds"], "scale_out.proceeds"
    ) < 0:
        raise ValueError("candidate aggregate schema scale-out values are negative")
    for key, value in weekly.items():
        if key in {"observations", "maximum_holding_count"}:
            _aggregate_count(value, f"weekly.{key}")
        else:
            _aggregate_number(value, f"weekly.{key}")
    if not math.isclose(
        float(weekly["average_cash_pct"]) + float(weekly["average_exposure_pct"]),
        100.0,
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        raise ValueError("candidate aggregate schema cash/exposure is inconsistent")
    outcomes = funnel["outcomes"]
    if not isinstance(outcomes, Mapping) or set(outcomes) != _OUTCOME_KEYS:
        raise ValueError("candidate aggregate schema outcomes are not exact")
    for key in _FUNNEL_KEYS - {"outcomes"}:
        _aggregate_count(funnel[key], f"funnel.{key}")
    for key, value in outcomes.items():
        _aggregate_count(value, f"outcomes.{key}")
    if (
        sum(int(value) for value in outcomes.values()) != funnel["attempted"]
        or outcomes["entries_executed"] != funnel["executed"]
        or funnel["attempted"] - funnel["executed"] != funnel["rejected"]
        or funnel["qualified"] != funnel["attempted"]
        or not (
            funnel["evaluated"]
            >= funnel["technical_setup"]
            >= funnel["current_growth_pass"]
            >= funnel["annual_growth_pass"]
            >= funnel["rs_pass"]
            >= funnel["composite_pass"]
            >= funnel["qualified"]
        )
    ):
        raise ValueError("candidate aggregate schema funnel counts are inconsistent")


def _write_canary_artifacts(
    artifact_root: Path,
    *,
    run_id: str,
    candidate_id: str,
    baseline: Mapping[str, object],
    evaluation: Mapping[str, object],
    comparison: OptimizationComparison | None,
    diff: str,
    call_sha256s: Sequence[str],
    verification_only: bool = False,
    verification_scope: Mapping[str, object] | None = None,
) -> tuple[tuple[Path, str], ...]:
    root = _absolute_path(artifact_root, "artifact root")
    if not isinstance(run_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id) is None:
        raise ValueError("optimization run ID is invalid")
    root.mkdir(parents=True, exist_ok=True)
    _regular_directory(root, "artifact root")
    if verification_only:
        if comparison is not None or verification_scope is None:
            raise ValueError("verification artifact inputs are inconsistent")
        scope = _validate_verification_scope(verification_scope)
        if evaluation.get("scope") != scope:
            raise ValueError("verification artifact scope differs from evaluation")
        baseline_payload = {
            "schema_version": 1,
            "candidate_id": candidate_id,
            "performance_acceptance_eligible": False,
            "reference_baseline": baseline,
            "scope": scope,
            "verification_only": True,
        }
        candidate_payload = {
            "schema_version": 1,
            "candidate_id": candidate_id,
            "effective_policy_sha256": evaluation["effective_policy_sha256"],
            "performance_acceptance_eligible": False,
            "pit_bundle_sha256": evaluation["pit_bundle_sha256"],
            "scope": scope,
            "verification": evaluation["verification"],
            "verification_only": True,
        }
        comparison_payload = {
            "schema_version": 1,
            "accepted": None,
            "candidate_id": candidate_id,
            "performance_acceptance_eligible": False,
            "provider_call_record_sha256s": list(call_sha256s),
            "verification_checks": {
                "aggregate_schema": True,
                "candidate_policy_delta": True,
                "known_strategy_activity": True,
                "sealed_input_identity": True,
            },
            "verification_only": True,
        }
    else:
        if comparison is None or verification_scope is not None:
            raise ValueError("full optimization artifact inputs are inconsistent")
        baseline_payload = {
            "schema_version": 1,
            "candidate_id": candidate_id,
            "baseline": baseline,
        }
        candidate_payload = {
            "schema_version": 1,
            "candidate_id": candidate_id,
            "pit_bundle_sha256": evaluation["pit_bundle_sha256"],
            "effective_policy_sha256": evaluation["effective_policy_sha256"],
            "full": evaluation["full"],
            "holdout": evaluation["holdout"],
        }
        comparison_payload = {
            "schema_version": 1,
            "candidate_id": candidate_id,
            "accepted": comparison.accepted,
            "full_objective_delta": comparison.full_objective_delta,
            "holdout_objective_delta": comparison.holdout_objective_delta,
            "full_checks": dict(comparison.full_checks),
            "holdout_checks": dict(comparison.holdout_checks),
            "holdout_minimum_closed_trades": comparison.holdout_minimum_closed_trades,
            "provider_call_record_sha256s": list(call_sha256s),
        }
    final_root = root / f"pit-optimization-{run_id}"
    if final_root.exists() or final_root.is_symlink():
        raise ValueError("optimization artifact set already exists")
    stage = root / f"pit-optimization-stage-{secrets.token_hex(4)}"
    stage.mkdir(mode=0o777 if os.name == "nt" else 0o700)
    artifacts: list[tuple[str, bytes]] = [
        (
            "baseline.json",
            _canonical_json_bytes(_json_primitive(baseline_payload)),
        ),
        (
            "candidate.json",
            _canonical_json_bytes(_json_primitive(candidate_payload)),
        ),
        (
            "comparison.json",
            _canonical_json_bytes(comparison_payload),
        ),
        (
            "candidate.diff",
            diff.encode("utf-8"),
        ),
    ]
    digests: dict[str, str] = {}
    try:
        for name, payload in artifacts:
            path = stage / name
            with path.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            verified = _regular_file(path, "staged optimization artifact")
            digest = _sha256_file(verified)
            if digest != _sha256_bytes(payload):
                raise ValueError("optimization artifact identity changed after write")
            digests[name] = digest
        _publish_staged_directory(stage, final_root)
    except OSError as exc:
        if stage.exists() and not stage.is_symlink():
            shutil.rmtree(stage)
        raise ValueError("optimization artifact set could not publish atomically") from exc
    except Exception:
        if stage.exists() and not stage.is_symlink():
            shutil.rmtree(stage)
        raise
    try:
        published = _regular_directory(final_root, "published optimization artifact set")
        results: list[tuple[Path, str]] = []
        for name, _payload in artifacts:
            path = _regular_file(published / name, "published optimization artifact")
            digest = _sha256_file(path)
            if digest != digests[name]:
                raise ValueError("published optimization artifact identity changed")
            results.append((path, digest))
        return tuple(results)
    except Exception:
        if final_root.exists() and not final_root.is_symlink():
            shutil.rmtree(final_root)
        raise


def _publish_staged_directory(stage: Path, final_root: Path) -> None:
    os.replace(stage, final_root)


def run_pit_optimization_canary(
    *,
    readiness: PitOptimizationReadiness,
    expected_readiness_sha256: str,
    expected_effective_policy_sha256: str,
    source_root: Path,
    candidate_root: Path,
    artifact_root: Path,
    run_id: str,
    services: PitOptimizationCanaryServices,
) -> PitOptimizationLoopResult:
    """Run one closed three-role cycle and evaluate one disposable candidate."""

    if not isinstance(services, PitOptimizationCanaryServices):
        raise ValueError("canary requires closed controller services")
    source = _regular_directory(source_root, "source root")
    candidate_path = _regular_directory(candidate_root, "candidate root")
    spent = 0.0
    calls = 0
    selected_candidate_id: str | None = None
    accepted: bool | None = None
    status = "aborted"
    artifacts: tuple[tuple[Path, str], ...] = ()
    call_sha256s: list[str] = []
    cleanup: PitOptimizationCleanup | None = None
    readiness_primitive = _json_primitive(readiness.primitive)
    if not isinstance(readiness_primitive, dict):
        raise ValueError("canary readiness payload is malformed")
    evaluation_contract = readiness_primitive.get("evaluation_contract")
    if not isinstance(evaluation_contract, dict):
        raise ValueError("canary readiness evaluation contract is absent")
    verification_only = evaluation_contract.get("verification_only")
    verification_scope = evaluation_contract.get("scope")
    if type(verification_only) is not bool or not isinstance(verification_scope, Mapping):
        raise ValueError("canary readiness evaluation scope is malformed")
    if verification_only:
        verification_scope = _validate_verification_scope(verification_scope)

    def verify_boundary(*, candidate_is_patched: bool = False) -> None:
        _readiness_identity(
            readiness,
            expected_readiness_sha256=expected_readiness_sha256,
            expected_effective_policy_sha256=expected_effective_policy_sha256,
            source_root=source,
            candidate_root=None if candidate_is_patched else candidate_path,
        )
        services.verify_inputs()

    def call(
        role: str,
        dynamic: dict[str, object],
        parser: Callable[[str], object],
        expected_type: type[object],
    ) -> object:
        nonlocal calls, spent
        verify_boundary()
        receipt = services.call_role(role, dynamic, parser)
        expected_index = calls + 1
        spent = _validate_role_call(
            receipt,
            role=role,
            expected_index=expected_index,
            expected_payload_type=expected_type,
            prior_spend=spent,
        )
        calls = expected_index
        call_sha256s.append(receipt.audit_sha256)
        return receipt.payload

    try:
        provider = _json_primitive(readiness.provider_payload)
        if not isinstance(provider, dict):
            raise ValueError("readiness provider payload is malformed")
        route_input: dict[str, object] = {
            "role": "orchestrator",
            "observation": provider,
        }
        if verification_only:
            route_input["verification_directive"] = {
                "performance_acceptance_eligible": False,
                "route_required": True,
            }
        route = call(
            "orchestrator",
            route_input,
            PitOptimizationRoute.from_json,
            PitOptimizationRoute,
        )
        assert isinstance(route, PitOptimizationRoute)
        _validate_route_citations(route, readiness)
        if route.action != "abort":
            reasoning = call(
                "reasoner",
                {
                    "role": "reasoner",
                    "observation": provider,
                    "route": asdict(route),
                },
                PitOptimizationReasoning.from_json,
                PitOptimizationReasoning,
            )
            assert isinstance(reasoning, PitOptimizationReasoning)
            _validate_reasoning_citations(reasoning, route, readiness)
            if not reasoning.skip:
                selected_candidate_id = reasoning.candidate_id
                selected = candidate_catalog()[selected_candidate_id]
                coding = call(
                    "coder",
                    {
                        "role": "coder",
                        "candidate": {
                            "candidate_id": selected.candidate_id,
                            "path": selected.path,
                            "old_line": selected.old_line,
                            "new_line": selected.new_line,
                        },
                    },
                    PitOptimizationCoding.from_json,
                    PitOptimizationCoding,
                )
                assert isinstance(coding, PitOptimizationCoding)
                validate_coding_selection(coding, selected)
                verify_boundary()
                rewritten, diff = _apply_controller_candidate(candidate_path, selected)
                source_bytes = (source / selected.path).read_bytes()
                if _sha256_bytes(source_bytes) != _json_primitive(readiness.primitive)[
                    "identities"
                ]["entry_contract_source_sha256"]:
                    raise ValueError("source changed while applying disposable candidate")
                evaluation = services.evaluate_candidate(candidate_path)
                if not isinstance(evaluation, Mapping):
                    raise ValueError("candidate evaluator returned a non-mapping result")
                if (candidate_path / selected.path).read_bytes() != rewritten:
                    raise ValueError("candidate evaluator mutated the disposable source")
                verify_boundary(candidate_is_patched=True)
                comparison: OptimizationComparison | None
                if verification_only:
                    _validate_verification_candidate(readiness, evaluation, selected)
                    comparison = None
                    accepted = None
                    status = "verified"
                else:
                    comparison = _candidate_comparison(readiness, evaluation, selected)
                    accepted = comparison.accepted
                    status = "accepted" if accepted else "rejected"
                artifacts = _write_canary_artifacts(
                    artifact_root,
                    run_id=run_id,
                    candidate_id=selected_candidate_id,
                    baseline=_json_primitive(readiness.primitive)["baseline"],
                    evaluation=evaluation,
                    comparison=comparison,
                    diff=diff,
                    call_sha256s=call_sha256s,
                    verification_only=verification_only,
                    verification_scope=(
                        verification_scope if verification_only else None
                    ),
                )
                try:
                    verify_boundary(candidate_is_patched=True)
                except Exception:
                    publication_roots = {path.parent for path, _digest in artifacts}
                    if len(publication_roots) == 1:
                        publication = publication_roots.pop()
                        if (
                            publication.parent == Path(artifact_root).resolve(strict=False)
                            and publication.name == f"pit-optimization-{run_id}"
                            and publication.exists()
                            and not publication.is_symlink()
                        ):
                            shutil.rmtree(publication)
                    raise
    finally:
        cleanup = services.cleanup()

    if not isinstance(cleanup, PitOptimizationCleanup):
        raise ValueError("optimization cleanup did not return closed facts")
    if cleanup.source_modified or not cleanup.cleanup_complete:
        raise ValueError("optimization source cleanup failed")
    return PitOptimizationLoopResult(
        phase="canary",
        status=status,
        exit_code=1 if status == "aborted" else 0,
        run_id=run_id,
        readiness_sha256=readiness.readiness_sha256,
        effective_policy_sha256=readiness.effective_policy_sha256,
        selected_candidate_id=selected_candidate_id,
        accepted=accepted,
        artifact_paths=artifacts,
        provider_calls=calls,
        spent_usd=spent,
        source_modified=cleanup.source_modified,
        cleanup_complete=cleanup.cleanup_complete,
        verification_only=verification_only,
    )


def _provider_payload(primitive: Mapping[str, object]) -> dict[str, object]:
    baseline = primitive["baseline"]
    assert isinstance(baseline, Mapping)
    full = baseline["full"]
    holdout = baseline["holdout"]
    assert isinstance(full, Mapping) and isinstance(holdout, Mapping)
    evaluation_contract = primitive["evaluation_contract"]
    assert isinstance(evaluation_contract, Mapping)
    provider_contract = _json_primitive(evaluation_contract)
    assert isinstance(provider_contract, dict)
    provider_scope = provider_contract.get("scope")
    assert isinstance(provider_scope, dict)
    provider_scope.pop("symbols", None)
    return {
        "schema_version": 1,
        "identities": primitive["identities"],
        "baseline_metrics": {
            "full": full,
            "holdout": holdout,
            "leader_basket": baseline["leader_basket"],
        },
        "candidate_ids": list(candidate_catalog()),
        "domain_evidence_ids": {
            domain: sorted(evidence_ids)
            for domain, evidence_ids in sorted(_DOMAIN_EVIDENCE_IDS.items())
        },
        "evidence_ids": list(_EVIDENCE_IDS),
        "invariant_ids": list(_INVARIANT_IDS),
        "editable_path": ENTRY_CONTRACT_PATH,
        "evaluation_contract": provider_contract,
    }


def prepare_pit_optimization(
    config: PitOptimizationGateConfig,
    *,
    source_root: Path,
    artifact_root: Path,
    source_head: str,
    source_fingerprint_sha256: str,
) -> PitOptimizationReadiness:
    """Authenticate the real Task 11 authority and emit deterministic readiness."""

    if not isinstance(config, PitOptimizationGateConfig):
        raise ValueError("prepare requires a PIT optimization config")
    if config.phase not in {"prepare", "canary"}:
        raise ValueError("prepare phase is invalid")
    source = _regular_directory(source_root, "source root")
    artifacts = _absolute_path(Path(artifact_root), "artifact root")
    if any(
        _paths_overlap(artifacts, protected)
        for protected in (source, config.baseline_run, config.pit_bundle)
    ):
        raise ValueError(
            "artifact root must not overlap source or sealed optimization inputs"
        )
    if not isinstance(source_head, str) or re.fullmatch(r"[0-9a-f]{40}", source_head) is None:
        raise ValueError("source head is invalid")
    if _SHA256_RE.fullmatch(source_fingerprint_sha256 or "") is None:
        raise ValueError("source fingerprint is invalid")
    catalog_identity = verify_catalog_source(source / ENTRY_CONTRACT_PATH)
    bundle_path = _regular_file(config.pit_bundle, "PIT bundle")
    if _sha256_file(bundle_path) != config.pit_bundle_sha256:
        raise ValueError("PIT bundle hash differs from the operator identity")
    baseline_run = _regular_directory(config.baseline_run, "baseline run")
    manifest_path = _regular_file(
        baseline_run / "run_manifest.json", "baseline manifest"
    )
    if _sha256_file(manifest_path) != config.baseline_manifest_sha256:
        raise ValueError("baseline manifest hash differs from the operator identity")
    from core.backtest_engine import PortfolioSimulator
    from core.engine_policy import effective_engine_policy_sha256
    from core.pit_data import PITDataBundle

    with PITDataBundle(bundle_path, expected_sha256=config.pit_bundle_sha256) as bundle:
        if (
            bundle.metadata.get("evaluation_start") != FULL_START_DATE
            or str(bundle.data_cutoff.date()) != FULL_END_DATE
        ):
            raise ValueError("PIT bundle date contract differs from optimization scope")
        simulator = PortfolioSimulator(pit_bundle=bundle, signal_every_n_days=1)
        policy = json.loads(json.dumps(simulator._effective_engine_policy, allow_nan=False))
        policy_digest = effective_engine_policy_sha256(policy)
        if (
            simulator._verify_effective_engine_policy() != policy_digest
            or policy_digest != config.effective_policy_sha256
        ):
            raise ValueError("effective PIT engine policy digest differs")
        _verify_policy_catalog(policy)
        baseline, baseline_artifacts = _baseline_observation(
            baseline_run, policy=policy
        )
    verify_sealed_baseline_artifacts(baseline_run, baseline_artifacts)
    verification_scope: dict[str, object] | None = None
    if config.verification_subset:
        with PITDataBundle(bundle_path, expected_sha256=config.pit_bundle_sha256) as bundle:
            verification_scope = _build_verification_scope(bundle, baseline_run)
    evaluation_contract = _evaluation_contract(
        verification_subset=config.verification_subset,
        verification_scope=verification_scope,
    )
    catalog_payload = [
        {
            "candidate_id": item.candidate_id,
            "constant_name": item.constant_name,
            "policy_field": item.policy_field,
            "old_value": item.old_value,
            "new_value": item.new_value,
            "path": item.path,
            "old_line": item.old_line,
            "new_line": item.new_line,
        }
        for item in candidate_catalog().values()
    ]
    primitive: dict[str, object] = {
        "schema_version": 1,
        "gate": "pit_optimization",
        "phase": "ready",
        "identities": {
            "source_head": source_head,
            "source_fingerprint_sha256": source_fingerprint_sha256,
            "entry_contract_source_sha256": catalog_identity.source_sha256,
            "pit_bundle_sha256": config.pit_bundle_sha256,
            "baseline_manifest_sha256": config.baseline_manifest_sha256,
            "baseline_source_commit": BASELINE_SOURCE_COMMIT,
            "effective_policy_sha256": policy_digest,
        },
        "sealed_inputs": {
            "pit_bundle_sha256": config.pit_bundle_sha256,
            "baseline_artifact_sha256": baseline_artifacts,
        },
        "date_contract": {
            "full_start": FULL_START_DATE,
            "full_end": FULL_END_DATE,
            "holdout_start": HOLDOUT_START_DATE,
            "holdout_end": HOLDOUT_END_DATE,
        },
        "budget_contract": {
            "samples": 1,
            "iterations": 1,
            "max_calls": MAX_CANARY_CALLS,
            "max_usd": MAX_CANARY_USD,
            "apply": False,
            "provider_retries": 0,
        },
        "evaluation_contract": evaluation_contract,
        "candidate_catalog": catalog_payload,
        "effective_policy": policy,
        "baseline": baseline,
        "evidence_ids": list(_EVIDENCE_IDS),
        "invariant_ids": list(_INVARIANT_IDS),
    }
    readiness_bytes = _canonical_json_bytes(primitive)
    readiness_sha = _sha256_bytes(readiness_bytes)
    if verify_catalog_source(source / ENTRY_CONTRACT_PATH) != catalog_identity:
        raise ValueError("source changed during PIT optimization prepare")
    artifacts.mkdir(parents=True, exist_ok=True)
    _regular_directory(artifacts, "artifact root")
    publication = artifacts / f"pit-optimization-readiness-{readiness_sha[:12]}"
    artifact_path = publication / "readiness.json"
    published_new = False
    if publication.exists() or publication.is_symlink():
        existing = _regular_file(artifact_path, "existing readiness artifact")
        if existing.read_bytes() != readiness_bytes:
            raise ValueError("existing readiness artifact has different bytes")
    else:
        stage = artifacts / f"pit-optimization-readiness-stage-{secrets.token_hex(4)}"
        stage.mkdir(mode=0o777 if os.name == "nt" else 0o700)
        try:
            staged = stage / "readiness.json"
            with staged.open("xb") as handle:
                handle.write(readiness_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            if _sha256_file(_regular_file(staged, "staged readiness artifact")) != readiness_sha:
                raise ValueError("staged readiness artifact hash mismatch")
            _publish_staged_directory(stage, publication)
            published_new = True
        except OSError as exc:
            if stage.exists() and not stage.is_symlink():
                shutil.rmtree(stage)
            raise ValueError("readiness artifact could not publish atomically") from exc
        except Exception:
            if stage.exists() and not stage.is_symlink():
                shutil.rmtree(stage)
            raise
    try:
        artifact_path = _regular_file(artifact_path, "published readiness artifact")
        artifact_sha = _sha256_file(artifact_path)
        if artifact_sha != readiness_sha:
            raise ValueError("readiness artifact hash mismatch")
        if verify_catalog_source(source / ENTRY_CONTRACT_PATH) != catalog_identity:
            raise ValueError("source changed after PIT optimization prepare publication")
        verify_sealed_baseline_artifacts(baseline_run, baseline_artifacts)
        provider = _provider_payload(primitive)
    except Exception:
        if published_new and publication.exists() and not publication.is_symlink():
            shutil.rmtree(publication)
        raise
    return PitOptimizationReadiness(
        readiness_sha256=readiness_sha,
        artifact_path=artifact_path,
        artifact_sha256=artifact_sha,
        effective_policy_sha256=policy_digest,
        provider_payload=MappingProxyType(provider),
        primitive=MappingProxyType(primitive),
    )


def _entry_outcomes_frame(values: Iterable[object]) -> pd.DataFrame:
    records: list[dict[str, object]] = []
    for value in values:
        to_primitive = getattr(value, "to_primitive", None)
        if not callable(to_primitive):
            raise ValueError("candidate entry outcome is not serializable")
        record = to_primitive()
        if not isinstance(record, dict):
            raise ValueError("candidate entry outcome is malformed")
        records.append(record)
    return pd.DataFrame(records, columns=[
        "symbol",
        "signal_date",
        "entry_date",
        "pivot",
        "buy_zone_lower",
        "buy_zone_upper",
        "entry_open",
        "outcome",
    ])


def evaluate_full_pit_candidate(
    *,
    pit_bundle: Path,
    pit_bundle_sha256: str,
) -> dict[str, object]:
    """Run the production simulator once and return aggregate-only full/holdout evidence."""

    bundle_path = _regular_file(pit_bundle, "candidate PIT bundle")
    if pit_bundle_sha256 != PIT_BUNDLE_SHA256 or _sha256_file(bundle_path) != pit_bundle_sha256:
        raise ValueError("candidate PIT bundle identity differs")
    from core.backtest_engine import PortfolioSimulator
    from core.engine_policy import effective_engine_policy_sha256
    from core.pit_data import PITDataBundle

    with PITDataBundle(bundle_path, expected_sha256=pit_bundle_sha256) as bundle:
        symbols = [symbol for symbol in bundle.symbols() if symbol != "SPY"]
        simulator = PortfolioSimulator(pit_bundle=bundle, signal_every_n_days=1)
        result = simulator.run(
            symbols,
            start_date=FULL_START_DATE,
            end_date=FULL_END_DATE,
            benchmark_symbol="SPY",
        )
        if result.config.get("data_mode") != "point_in_time" or (
            result.config.get("pit_bundle_sha256") != pit_bundle_sha256
        ):
            raise ValueError("candidate result did not use the sealed PIT bundle")
        policy = result.config.get("effective_engine_policy")
        policy_sha = result.config.get("effective_engine_policy_sha256")
        if not isinstance(policy, dict) or not isinstance(policy_sha, str) or (
            effective_engine_policy_sha256(policy) != policy_sha
            or simulator._verify_effective_engine_policy() != policy_sha
        ):
            raise ValueError("candidate effective policy identity is invalid")
        equity = pd.DataFrame(
            {
                "date": pd.to_datetime(result.equity_curve.index),
                "portfolio": result.equity_curve.values,
                "benchmark": result.benchmark_curve.reindex(result.equity_curve.index).values,
            }
        )
        transactions = result.transaction_log.copy()
        weekly = result.weekly_holdings.copy()
        outcomes = _entry_outcomes_frame(result.entry_outcomes)
        sessions = pd.DatetimeIndex(pd.to_datetime(equity["date"])).normalize()
        thresholds = _policy_thresholds(policy)
        windows: dict[str, object] = {}
        for name, start_date, end_date in (
            ("full", FULL_START_DATE, FULL_END_DATE),
            ("holdout", HOLDOUT_START_DATE, HOLDOUT_END_DATE),
        ):
            trades = aggregate_transaction_window(
                transactions,
                sessions=sessions,
                start_date=start_date,
                end_date=end_date,
            )
            windows[name] = {
                "performance": aggregate_equity_window(
                    equity,
                    start_date=start_date,
                    end_date=end_date,
                    closed_trades=int(trades["closed_trades"]),
                ),
                "trades": trades,
                "weekly": aggregate_weekly_window(
                    weekly, start_date=start_date, end_date=end_date
                ),
                "funnel": aggregate_signal_funnel(
                    result.signal_log,
                    outcomes,
                    start_date=start_date,
                    end_date=end_date,
                    min_current_growth=thresholds["min_current_growth"],
                    min_annual_growth=thresholds["min_annual_growth"],
                    min_rs_score=thresholds["min_rs_score"],
                    min_composite_score=thresholds["min_entry_composite_score"],
                ),
            }
        return {
            "schema_version": 1,
            "pit_bundle_sha256": pit_bundle_sha256,
            "effective_policy_sha256": policy_sha,
            "effective_policy": policy,
            "full": windows["full"],
            "holdout": windows["holdout"],
        }


def evaluate_verification_pit_candidate(
    *,
    pit_bundle: Path,
    pit_bundle_sha256: str,
    verification_scope: Mapping[str, object],
) -> dict[str, object]:
    """Run the production simulator on one deterministic verification-only slice."""

    scope = _validate_verification_scope(verification_scope)
    bundle_path = _regular_file(pit_bundle, "verification PIT bundle")
    if pit_bundle_sha256 != PIT_BUNDLE_SHA256 or _sha256_file(bundle_path) != pit_bundle_sha256:
        raise ValueError("verification PIT bundle identity differs")
    from core.backtest_engine import PortfolioSimulator
    from core.engine_policy import effective_engine_policy_sha256
    from core.pit_data import PITDataBundle

    with PITDataBundle(bundle_path, expected_sha256=pit_bundle_sha256) as bundle:
        symbols = list(scope["symbols"])
        if not set(symbols).issubset(set(bundle.symbols()) - {"SPY"}):
            raise ValueError("verification symbols differ from the sealed PIT bundle")
        simulator = PortfolioSimulator(pit_bundle=bundle, signal_every_n_days=1)
        result = simulator.run(
            symbols,
            start_date=str(scope["warmup_start"]),
            end_date=str(scope["measurement_end"]),
            benchmark_symbol=str(scope["benchmark"]),
        )
        if result.config.get("data_mode") != "point_in_time" or (
            result.config.get("pit_bundle_sha256") != pit_bundle_sha256
        ):
            raise ValueError("verification result did not use the sealed PIT bundle")
        policy = result.config.get("effective_engine_policy")
        policy_sha = result.config.get("effective_engine_policy_sha256")
        if not isinstance(policy, dict) or not isinstance(policy_sha, str) or (
            effective_engine_policy_sha256(policy) != policy_sha
            or simulator._verify_effective_engine_policy() != policy_sha
        ):
            raise ValueError("verification effective policy identity is invalid")
        equity = pd.DataFrame(
            {
                "date": pd.to_datetime(result.equity_curve.index),
                "portfolio": result.equity_curve.values,
                "benchmark": result.benchmark_curve.reindex(result.equity_curve.index).values,
            }
        )
        transactions = result.transaction_log.copy()
        weekly = result.weekly_holdings.copy()
        outcomes = _entry_outcomes_frame(result.entry_outcomes)
        sessions = pd.DatetimeIndex(pd.to_datetime(equity["date"])).normalize()
        measurement_start = str(scope["measurement_start"])
        measurement_end = str(scope["measurement_end"])
        measured_sessions = sessions[
            (sessions >= pd.Timestamp(measurement_start))
            & (sessions <= pd.Timestamp(measurement_end))
        ]
        if len(measured_sessions) != _VERIFICATION_SESSION_COUNT:
            raise ValueError("verification replay did not produce sixty measured sessions")
        thresholds = _policy_thresholds(policy)
        trades = aggregate_transaction_window(
            transactions,
            sessions=sessions,
            start_date=measurement_start,
            end_date=measurement_end,
        )
        window = {
            "performance": aggregate_equity_window(
                equity,
                start_date=measurement_start,
                end_date=measurement_end,
                closed_trades=int(trades["closed_trades"]),
            ),
            "trades": trades,
            "weekly": aggregate_weekly_window(
                weekly, start_date=measurement_start, end_date=measurement_end
            ),
            "funnel": aggregate_signal_funnel(
                result.signal_log,
                outcomes,
                start_date=measurement_start,
                end_date=measurement_end,
                min_current_growth=thresholds["min_current_growth"],
                min_annual_growth=thresholds["min_annual_growth"],
                min_rs_score=thresholds["min_rs_score"],
                min_composite_score=thresholds["min_entry_composite_score"],
            ),
        }
        return {
            "schema_version": 1,
            "verification_only": True,
            "performance_acceptance_eligible": False,
            "pit_bundle_sha256": pit_bundle_sha256,
            "effective_policy_sha256": policy_sha,
            "effective_policy": policy,
            "scope": scope,
            "verification": window,
        }


def _worker_main(arguments: Sequence[str]) -> int:
    """Exact container-only evaluator entrypoint admitted by SandboxRunner."""

    if tuple(arguments[:1]) != ("--worker-evaluate",):
        raise ValueError("PIT optimization module is worker-only when executed")
    parser = __import__("argparse").ArgumentParser(add_help=False)
    parser.add_argument("--worker-evaluate", action="store_true", required=True)
    parser.add_argument("--pit-bundle", type=Path, required=True)
    parser.add_argument("--pit-bundle-sha256", required=True)
    parser.add_argument("--verification-subset", type=Path)
    parser.add_argument("--verification-subset-sha256")
    parser.add_argument("--output", type=Path, required=True)
    namespace = parser.parse_args(list(arguments))
    if (
        namespace.pit_bundle != Path("/workspace/data/pit-bundle.sqlite3")
        or namespace.output
        != Path("/workspace/output/pit-optimization-result.json")
        or _SHA256_RE.fullmatch(namespace.pit_bundle_sha256 or "") is None
        or (namespace.verification_subset is None)
        != (namespace.verification_subset_sha256 is None)
    ):
        raise ValueError("PIT optimization worker paths or identity are invalid")
    verification_scope: Mapping[str, object] | None = None
    if namespace.verification_subset is not None:
        if (
            namespace.verification_subset
            != Path("/workspace/data/pit-optimization-verification-subset.json")
            or _SHA256_RE.fullmatch(namespace.verification_subset_sha256 or "") is None
        ):
            raise ValueError("PIT optimization verification input is invalid")
        scope_path = _regular_file(
            namespace.verification_subset, "verification subset manifest"
        )
        if _sha256_file(scope_path) != namespace.verification_subset_sha256:
            raise ValueError("PIT optimization verification input identity differs")
        try:
            scope_value = json.loads(scope_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("PIT optimization verification input is malformed") from exc
        if not isinstance(scope_value, Mapping):
            raise ValueError("PIT optimization verification scope must be an object")
        verification_scope = _validate_verification_scope(scope_value)
    from contextlib import redirect_stderr, redirect_stdout

    with open(os.devnull, "w", encoding="utf-8") as sink:
        with redirect_stdout(sink), redirect_stderr(sink):
            if verification_scope is None:
                value = evaluate_full_pit_candidate(
                    pit_bundle=namespace.pit_bundle,
                    pit_bundle_sha256=namespace.pit_bundle_sha256,
                )
            else:
                value = evaluate_verification_pit_candidate(
                    pit_bundle=namespace.pit_bundle,
                    pit_bundle_sha256=namespace.pit_bundle_sha256,
                    verification_scope=verification_scope,
                )
    payload = _canonical_json_bytes(value)
    with namespace.output.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return 0


def closed_window_metrics(window: Mapping[str, object]) -> OptimizationWindowMetrics:
    performance = window.get("performance")
    if not isinstance(performance, Mapping):
        raise ValueError("optimization window lacks performance")
    return OptimizationWindowMetrics.from_mapping(performance)


if __name__ == "__main__":
    raise SystemExit(_worker_main(tuple(sys.argv[1:])))
