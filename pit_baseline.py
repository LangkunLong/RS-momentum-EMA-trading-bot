"""Run the immutable five-year PIT CANSLIM diagnostic and leader benchmark."""

from __future__ import annotations

import argparse
from bisect import bisect_right
import inspect
import json
import math
import os
import stat
import subprocess
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import pandas as pd

from core.backtest_engine import (
    DEFAULT_MIN_C_A_GROWTH,
    ENTRY_ATTEMPT_OUTCOME_SCHEMA_VERSION,
    EntryAttemptOutcome,
    PortfolioSimulator,
    SimulationResult,
)
from core.canslim.a_annual_earnings import evaluate_a
from core.canslim.c_current_earnings import evaluate_c
from core.canslim.entry_contract import (
    MAX_BUY_ZONE_EXTENSION,
    MIN_ANNUAL_GROWTH,
    MIN_COMPOSITE_SCORE,
    MIN_CURRENT_GROWTH,
    MIN_RS_SCORE,
    MIN_VOLUME_RATIO,
)
from core.leader_basket import LeaderBasketConfig, LeaderBasketResult, LeaderBasketSimulator
from core.leader_evaluation import (
    LeaderIdentityContract,
    label_five_year_leaders,
    label_rolling_leaders,
)
from core.pit_baseline_report import (
    build_leader_recall_frame,
    five_year_leaders_frame,
    five_year_leader_recall_summary,
    reconcile_signals_to_transactions,
    rolling_label_recall_summary,
    rolling_leaders_frame,
)
from core.pit_data import PITDataBundle, PriceIdentityTransitionContract, sha256_file

_START = "2021-01-01"
_END = "2025-12-31"
_WARMUP = "2020-01-01"
_BENCHMARK = "SPY"
_LEADERS = 100
_REBALANCE = 20
_PUBLIC_DATE_RULE = (
    "first supplied SPY trading day strictly after SEC acceptance calendar date; "
    "filed date fallback only"
)
_SAME_ISSUER_KINDS = {
    "same_issuer_rename",
    "same_issuer_ticker_reuse",
    "legacy_survivor_rename",
    "accounting_acquirer_rename",
}
_MASTER_COLUMNS = (
    "ticker", "cik", "company_name", "first_membership_date",
    "last_membership_date", "mapping_basis",
)
_EXCLUSION_COLUMNS = (
    "ticker", "company_name", "first_membership_date", "last_membership_date",
    "reason", "details",
)
_RESUME_JOURNAL_FILENAMES = frozenset({
    "portfolio_checkpoint.json",
    "portfolio_progress.jsonl",
    "portfolio_state.jsonl",
})


class CoverageGateError(ValueError):
    """A complete coverage artifact was written, but publication is forbidden."""


def _date(value: str) -> str:
    return date.fromisoformat(value).isoformat()


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _regular_file(path: str | Path) -> Path:
    value = Path(path)
    if not value.is_file() or value.is_symlink():
        raise ValueError(f"input must be a regular non-link file: {value}")
    return value.resolve()


def _lexical_path(path: str | Path, *, field: str) -> Path:
    """Return an absolute path after rejecting every lexical link boundary."""

    value = Path(path).absolute()
    current = Path(value.anchor)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    components = [current]
    for part in value.parts[1:]:
        current = current / part
        components.append(current)
    for current in components:
        try:
            metadata = os.lstat(current)
        except OSError as exc:
            raise ValueError(f"{field} path cannot be inspected: {current}") from exc
        attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
        if stat.S_ISLNK(metadata.st_mode) or (reparse_flag and attributes & reparse_flag):
            raise ValueError(
                f"{field} must not traverse a symlink or Windows reparse point: {current}"
            )
    return value


def _lexical_regular_file(path: str | Path, *, field: str) -> Path:
    value = _lexical_path(path, field=field)
    if not value.is_file():
        raise ValueError(f"{field} must be a regular non-link file: {value}")
    return value


def _lexical_regular_directory(path: str | Path, *, field: str) -> Path:
    value = _lexical_path(path, field=field)
    if not value.is_dir():
        raise ValueError(f"{field} must be a regular non-link directory: {value}")
    return value


def _resume_run_paths(
    *,
    resume_checkpoint: str | Path,
    output_root: str | Path,
    expected_bundle_sha: str,
) -> tuple[Path, Path, Path]:
    """Validate and return the single run directory that owns a resume journal."""

    lexical_output_root = _lexical_regular_directory(output_root, field="--output-root")
    lexical_checkpoint = _lexical_regular_file(
        resume_checkpoint, field="resume checkpoint"
    )
    lexical_run_dir = _lexical_regular_directory(
        lexical_checkpoint.parent, field="resume checkpoint parent"
    )
    if lexical_checkpoint.name != "portfolio_checkpoint.json":
        raise ValueError("resume checkpoint must be named portfolio_checkpoint.json")
    if lexical_run_dir.parent != lexical_output_root:
        raise ValueError("resume checkpoint must belong directly to --output-root")
    expected_suffix = f"-{expected_bundle_sha[:12]}"
    if not lexical_run_dir.name.endswith(expected_suffix):
        raise ValueError("resume run directory name does not match the PIT bundle prefix")
    run_name = lexical_run_dir.name[:-len(expected_suffix)]
    try:
        parsed_run_name = datetime.strptime(run_name, "run-%Y%m%dT%H%M%SZ")
    except ValueError as exc:
        raise ValueError("resume run directory name is not a canonical UTC run name") from exc
    if parsed_run_name.strftime("run-%Y%m%dT%H%M%SZ") != run_name:
        raise ValueError("resume run directory name is not a canonical UTC run name")
    entries = {item.name: item for item in lexical_run_dir.iterdir()}
    for name, item in entries.items():
        _lexical_path(item, field=f"resume run entry {name!r}")
    terminal_markers = tuple(
        name for name in ("run_manifest.json", "run_failed.json") if name in entries
    )
    if terminal_markers:
        raise ValueError(
            "resume run is terminal; preserve its marker and start a fresh run: "
            + ", ".join(terminal_markers)
        )
    if set(entries) != _RESUME_JOURNAL_FILENAMES:
        missing = sorted(_RESUME_JOURNAL_FILENAMES.difference(entries))
        extra = sorted(set(entries).difference(_RESUME_JOURNAL_FILENAMES))
        raise ValueError(
            "resume run directory must contain exactly the checkpoint/progress/state journals; "
            f"missing={missing}, extra={extra}"
        )
    if any(not item.is_file() or item.is_symlink() for item in entries.values()):
        raise ValueError("resume journals must be regular non-link files")
    resolved_output_root = lexical_output_root.resolve()
    resolved_run_dir = lexical_run_dir.resolve()
    resolved_checkpoint = lexical_checkpoint.resolve()
    resolved_entries = {name: item.resolve() for name, item in entries.items()}
    if resolved_run_dir.parent != resolved_output_root:
        raise ValueError("resume checkpoint resolves outside --output-root")
    if resolved_checkpoint.parent != resolved_run_dir:
        raise ValueError("resume checkpoint resolves outside its run directory")
    if any(item.parent != resolved_run_dir for item in resolved_entries.values()):
        raise ValueError("resume journal resolves outside its run directory")
    return (
        resolved_run_dir,
        resolved_checkpoint,
        resolved_entries["portfolio_progress.jsonl"],
    )


def _git_identity(worktree: Path, *, require_clean: bool) -> str:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=worktree, check=True,
        capture_output=True, text=True, timeout=10,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=worktree,
        check=True, capture_output=True, text=True, timeout=10,
    ).stdout
    if require_clean and status:
        raise ValueError("PIT baseline requires a clean Git worktree")
    return head


def _json_bytes(value: object) -> bytes:
    def default(item: object) -> object:
        if isinstance(item, (date, datetime, pd.Timestamp)):
            return item.isoformat()
        if hasattr(item, "item"):
            return item.item()
        raise TypeError(f"value is not JSON serializable: {type(item).__name__}")

    return (
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False, default=default) + "\n"
    ).encode()


def _write_bytes(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()


def _write_frame(path: Path, frame: pd.DataFrame) -> None:
    _write_bytes(path, frame.to_csv(index=False, lineterminator="\n").encode())


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON input must contain an object: {path}")
    return value


def _snapshots(paths: Mapping[str, Path]) -> dict[str, str]:
    return {name: sha256_file(path) for name, path in sorted(paths.items())}


def _recheck_inputs(paths: Mapping[str, Path], expected: Mapping[str, str]) -> None:
    if _snapshots(paths) != dict(expected):
        raise ValueError("a hash-bound baseline input changed during the run")


def _digest(value: object, *, field: str) -> str:
    text = str(value).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"required SHA-256 is invalid: {field}")
    return text


def _finite(value: object, *, field: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"required metric is not finite: {field}")
    return number


def _curve_index(series: pd.Series, *, field: str) -> pd.DatetimeIndex:
    if not isinstance(series, pd.Series) or series.empty:
        raise ValueError(f"{field} is empty")
    index = pd.DatetimeIndex(pd.to_datetime(series.index, errors="raise")).normalize()
    values = pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)
    if index.has_duplicates or not index.is_monotonic_increasing:
        raise ValueError(f"{field} index is not unique and monotonic")
    if any(not math.isfinite(value) or value <= 0 for value in values):
        raise ValueError(f"{field} contains a non-finite or non-positive value")
    return index


def _average_cash_pct(result: SimulationResult) -> float:
    frame = result.weekly_holdings
    required = {"Week_Ending", "Cash", "Total_Equity"}
    if not isinstance(frame, pd.DataFrame) or frame.empty or not required.issubset(frame):
        raise ValueError("weekly holdings lack required cash/equity observations")
    equity = pd.to_numeric(frame["Total_Equity"], errors="raise")
    cash = pd.to_numeric(frame["Cash"], errors="raise")
    if any(not math.isfinite(float(value)) for value in [*equity, *cash]):
        raise ValueError("weekly holdings contain non-finite cash/equity values")
    if (equity <= 0).any() or (cash < 0).any():
        raise ValueError("weekly holdings contain invalid cash/equity values")
    return _finite((cash / equity).mean() * 100.0, field="average cash")


def _strict_boolean_series(frame: pd.DataFrame, field: str) -> pd.Series:
    values: list[bool] = []
    for value in frame[field]:
        if isinstance(value, bool) or value.__class__.__name__ == "bool_":
            values.append(bool(value))
        else:
            raise ValueError(f"CANSLIM signal field is not boolean: {field}")
    return pd.Series(values, index=frame.index, dtype=bool)


def _validated_signal_frame(
    result: SimulationResult,
    sessions: pd.DatetimeIndex,
) -> pd.DataFrame:
    if not isinstance(result.signal_log, pd.DataFrame) or result.signal_log.empty:
        raise ValueError("CANSLIM signal log is empty")
    signals = result.signal_log.copy()
    session_index = pd.DatetimeIndex(sessions).normalize()
    if session_index.has_duplicates:
        raise ValueError("benchmark sessions are not unique")
    signals["signal_date"] = pd.to_datetime(
        signals["signal_date"], errors="raise"
    ).dt.normalize()
    normalized_symbols: list[str] = []
    for value in signals["symbol"]:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or value != value.upper()
        ):
            raise ValueError("CANSLIM signal symbol is not nonblank uppercase text")
        normalized_symbols.append(value)
    signals["symbol"] = normalized_symbols
    if signals.duplicated(["symbol", "signal_date"]).any():
        raise ValueError("CANSLIM signal evaluations are not unique by symbol/session")
    if not set(signals["signal_date"]).issubset(set(session_index)):
        raise ValueError("CANSLIM signal evaluation is off the benchmark calendar")
    return signals


def _nonnegative_diagnostic(result: SimulationResult, field: str) -> int:
    raw = result.execution_diagnostics[field]
    try:
        number = int(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"CANSLIM execution diagnostic is invalid: {field}") from exc
    if isinstance(raw, bool) or number != raw or number < 0:
        raise ValueError(f"CANSLIM execution diagnostic is invalid: {field}")
    return number


def _validate_portfolio(
    result: SimulationResult, sessions: pd.DatetimeIndex, bundle_sha256: str,
) -> None:
    if not _curve_index(result.equity_curve, field="CANSLIM equity").equals(sessions):
        raise ValueError("CANSLIM equity does not exactly cover benchmark sessions")
    if not _curve_index(result.benchmark_curve, field="CANSLIM benchmark").equals(sessions):
        raise ValueError("CANSLIM benchmark does not exactly cover benchmark sessions")
    required_signals = {
        "symbol", "signal_date", "buy_signal", "current_growth", "annual_growth",
        "rs_score", "has_breakout", "has_volume_surge", "in_buy_zone", "canslim_score",
        "entry_composite_score", "entry_contract_eligible", "entry_blocking_reasons",
        "pivot", "prior_close", "event_volume", "prior_average_volume_50",
        "entry_volume_ratio", "entry_extension", "price_advanced",
        "technical_setup_eligible", "technical_blocking_reasons",
    }
    if not isinstance(result.signal_log, pd.DataFrame) or result.signal_log.empty:
        raise ValueError("CANSLIM signal log is empty")
    if not required_signals.issubset(result.signal_log):
        raise ValueError("CANSLIM signal log lacks required baseline fields")
    signals = _validated_signal_frame(result, sessions)
    _average_cash_pct(result)
    holding_dates = pd.DatetimeIndex(
        pd.to_datetime(result.weekly_holdings["Week_Ending"], errors="raise")
    ).normalize()
    if holding_dates.has_duplicates or not set(holding_dates).issubset(sessions):
        raise ValueError("weekly holdings dates are not unique benchmark sessions")
    if not isinstance(result.config, dict) or not isinstance(result.execution_diagnostics, dict):
        raise ValueError("CANSLIM config/diagnostics artifacts are missing")
    expected = {
        "benchmark_symbol": _BENCHMARK, "start_date": _START, "end_date": _END,
        "data_mode": "point_in_time", "pit_bundle_sha256": bundle_sha256,
        "technical_only": False, "max_positions": None,
        "require_bullish_market": False, "use_stateful_regime_gate": False,
        "signal_every_n_days": 1,
        "entry_contract_min_current_growth": MIN_CURRENT_GROWTH,
        "entry_contract_min_annual_growth": MIN_ANNUAL_GROWTH,
        "entry_contract_min_rs_score": MIN_RS_SCORE,
        "entry_contract_min_composite_score": MIN_COMPOSITE_SCORE,
        "entry_contract_min_volume_ratio": MIN_VOLUME_RATIO,
        "entry_contract_max_buy_zone_extension": MAX_BUY_ZONE_EXTENSION,
    }
    for key, value in expected.items():
        if result.config.get(key) != value:
            raise ValueError(f"CANSLIM result is not the fixed production baseline: {key}")

    buy_signal = _strict_boolean_series(signals, "buy_signal")
    entry_eligible = _strict_boolean_series(signals, "entry_contract_eligible")
    technical_eligible = _strict_boolean_series(signals, "technical_setup_eligible")
    if not buy_signal.equals(entry_eligible):
        raise ValueError(
            "fixed no-market-gate baseline buy signals disagree with entry eligibility"
        )
    if bool((entry_eligible & ~technical_eligible).any()):
        raise ValueError("qualified CANSLIM signal lacks a technical setup")
    qualifying_pivots = pd.to_numeric(
        signals.loc[entry_eligible, "pivot"], errors="coerce"
    )
    if any(
        not math.isfinite(float(value)) or float(value) <= 0
        for value in qualifying_pivots
    ):
        raise ValueError("qualified CANSLIM signal lacks a finite positive pivot")
    required_diagnostics = {
        "signal_days", "entries_allowed_days", "blocked_by_regime_days",
        "blocked_by_market_days", "cash_deployment_override_days", "buy_signal_rows",
        "potential_buy_signal_rows", "potential_buy_signal_rows_blocked_by_market",
        "buy_signal_rows_when_entries_allowed", "buy_signal_rows_blocked_by_regime",
        "buy_signal_rows_blocked_by_market", "buy_signal_rows_blocked_by_both",
        "buy_signal_rows_when_cash_override", "capacity_truncated_signals",
        "entry_attempts", "entries_executed", "entry_rejected_already_open",
        "entry_rejected_capacity", "entry_rejected_missing_data",
        "entry_rejected_invalid_price", "entry_rejected_next_open_buy_zone",
        "entry_rejected_invalid_risk",
        "entry_rejected_no_cash", "eviction_attempts", "evictions_executed",
        "eviction_rejections",
    }
    if not required_diagnostics.issubset(result.execution_diagnostics):
        raise ValueError("CANSLIM execution diagnostics schema is incomplete")
    if not isinstance(result.entry_outcomes, tuple) or any(
        not isinstance(outcome, EntryAttemptOutcome) for outcome in result.entry_outcomes
    ):
        raise ValueError("CANSLIM entry outcomes use an invalid immutable schema")
    if result.execution_diagnostics["entry_attempts"] != len(result.entry_outcomes):
        raise ValueError("CANSLIM entry outcomes do not cover every attempt")
    _json_bytes([outcome.to_primitive() for outcome in result.entry_outcomes])

    qualified_count = int(entry_eligible.sum())
    attempted_count = _nonnegative_diagnostic(result, "entry_attempts")
    executed_count = _nonnegative_diagnostic(result, "entries_executed")
    capacity_truncated = _nonnegative_diagnostic(result, "capacity_truncated_signals")
    capacity_rejected = _nonnegative_diagnostic(result, "entry_rejected_capacity")
    if capacity_truncated != 0 or capacity_rejected != 0:
        raise ValueError("fixed uncapped baseline reported an impossible capacity limit")
    rejected_count = sum(
        _nonnegative_diagnostic(result, field)
        for field in (
            "entry_rejected_already_open",
            "entry_rejected_capacity",
            "entry_rejected_missing_data",
            "entry_rejected_invalid_price",
            "entry_rejected_next_open_buy_zone",
            "entry_rejected_invalid_risk",
            "entry_rejected_no_cash",
        )
    )
    if _nonnegative_diagnostic(result, "buy_signal_rows") != qualified_count:
        raise ValueError("qualified count disagrees with buy-signal diagnostics")
    if (
        _nonnegative_diagnostic(result, "buy_signal_rows_when_entries_allowed")
        != qualified_count
    ):
        raise ValueError("fixed baseline did not admit every qualifying signal")
    if any(
        _nonnegative_diagnostic(result, field) != 0
        for field in (
            "buy_signal_rows_blocked_by_regime",
            "buy_signal_rows_blocked_by_market",
            "buy_signal_rows_blocked_by_both",
        )
    ):
        raise ValueError("fixed no-market-gate baseline blocked a qualifying signal")
    if executed_count + rejected_count != attempted_count:
        raise ValueError("entry attempts do not equal executions plus rejections")
    final_pending = qualified_count - attempted_count
    if final_pending < 0:
        raise ValueError("attempted and truncated entries exceed qualifying signals")
    last_session_qualified = int(
        entry_eligible.loc[signals["signal_date"] == sessions[-1]].sum()
    )
    if final_pending > last_session_qualified:
        raise ValueError("final pending entries exceed final-session qualifications")
    qualifying_keys = set(
        zip(
            signals.loc[entry_eligible, "symbol"],
            signals.loc[entry_eligible, "signal_date"],
            strict=True,
        )
    )
    expected_outcome_keys = {
        key for key in qualifying_keys if key[1] != sessions[-1]
    }
    actual_outcome_keys = {
        (outcome.symbol, pd.Timestamp(outcome.signal_date).normalize())
        for outcome in result.entry_outcomes
    }
    if actual_outcome_keys != expected_outcome_keys:
        raise ValueError(
            "entry outcomes do not exactly cover every non-final qualifying signal"
        )


def _validate_basket(
    result: LeaderBasketResult,
    sessions: pd.DatetimeIndex,
    expected_config: Mapping[str, object],
) -> None:
    if not _curve_index(result.equity_curve, field="leader-basket equity").equals(sessions):
        raise ValueError("leader-basket equity does not exactly cover benchmark sessions")
    if not _curve_index(result.benchmark_curve, field="leader-basket benchmark").equals(sessions):
        raise ValueError("leader-basket benchmark does not exactly cover benchmark sessions")
    required = {"date", "cash", "equity", "holdings_count", "leaders"}
    if not isinstance(result.holdings, pd.DataFrame) or result.holdings.empty:
        raise ValueError("leader-basket holdings are empty")
    if not required.issubset(result.holdings):
        raise ValueError("leader-basket holdings lack required fields")
    holding_dates = pd.DatetimeIndex(
        pd.to_datetime(result.holdings["date"], errors="raise")
    ).normalize()
    if not holding_dates.equals(sessions):
        raise ValueError("leader-basket holdings do not exactly cover benchmark sessions")
    cash = pd.to_numeric(result.holdings["cash"], errors="raise")
    equity = pd.to_numeric(result.holdings["equity"], errors="raise")
    if any(not math.isfinite(float(value)) for value in [*cash, *equity]):
        raise ValueError("leader-basket holdings contain non-finite cash/equity")
    if (cash < 0).any() or (equity <= 0).any():
        raise ValueError("leader-basket holdings contain invalid cash/equity")
    if not isinstance(result.transactions, pd.DataFrame) or result.transactions.empty:
        raise ValueError("leader-basket transactions are empty")
    if not {"Date", "Ticker", "Action", "Price", "Quantity", "Reason"}.issubset(
        result.transactions
    ):
        raise ValueError("leader-basket transactions lack required fields")
    if not isinstance(result.config, dict):
        raise ValueError("leader-basket result config is missing")
    for key, value in expected_config.items():
        if result.config.get(key) != value:
            raise ValueError(f"leader-basket result config mismatch: {key}")


def _equity_frame(result: SimulationResult) -> pd.DataFrame:
    benchmark = result.benchmark_curve.reindex(result.equity_curve.index)
    return pd.DataFrame({
        "date": pd.to_datetime(result.equity_curve.index).date,
        "portfolio": result.equity_curve.astype(float).values,
        "benchmark": benchmark.astype(float).values,
    })


def _basket_equity_frame(result: LeaderBasketResult) -> pd.DataFrame:
    benchmark = result.benchmark_curve.reindex(result.equity_curve.index)
    return pd.DataFrame({
        "date": pd.to_datetime(result.equity_curve.index).date,
        "leader_basket": result.equity_curve.astype(float).values,
        "benchmark": benchmark.astype(float).values,
    })


def _entry_outcomes_frame(result: SimulationResult) -> pd.DataFrame:
    columns = (
        "symbol", "signal_date", "entry_date", "pivot", "buy_zone_lower",
        "buy_zone_upper", "entry_open", "outcome",
    )
    return pd.DataFrame(
        [outcome.to_primitive() for outcome in result.entry_outcomes],
        columns=columns,
    )


def _daily_entry_funnel_frame(
    result: SimulationResult,
    sessions: pd.DatetimeIndex,
) -> pd.DataFrame:
    signals = _validated_signal_frame(result, sessions).reset_index(drop=True)
    entry_eligible = _strict_boolean_series(signals, "entry_contract_eligible")
    buy_signal = _strict_boolean_series(signals, "buy_signal")
    if not buy_signal.equals(entry_eligible):
        raise ValueError("daily funnel contains an unqualified buy signal")
    qualified_keys = set(
        zip(
            signals.loc[entry_eligible, "symbol"],
            signals.loc[entry_eligible, "signal_date"],
            strict=True,
        )
    )
    outcome_keys: set[tuple[str, pd.Timestamp]] = set()
    outcomes = _entry_outcomes_frame(result)
    if not outcomes.empty:
        normalized_symbols: list[str] = []
        for value in outcomes["symbol"]:
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or value != value.upper()
            ):
                raise ValueError("entry outcome symbol is not nonblank uppercase text")
            normalized_symbols.append(value)
        outcomes["symbol"] = normalized_symbols
        outcomes["signal_date"] = pd.to_datetime(
            outcomes["signal_date"], errors="raise"
        ).dt.normalize()
        if not set(outcomes["signal_date"]).issubset(set(sessions)):
            raise ValueError("entry outcome signal date is off the benchmark calendar")
        if outcomes.duplicated(["symbol", "signal_date"]).any():
            raise ValueError("entry outcomes are not unique by symbol/session")
        outcome_keys = set(
            zip(outcomes["symbol"], outcomes["signal_date"], strict=True)
        )
        if not outcome_keys.issubset(qualified_keys):
            raise ValueError("daily funnel contains an attempt for an unqualified signal")
        valid_outcomes = {
            "entries_executed",
            "entry_rejected_already_open",
            "entry_rejected_capacity",
            "entry_rejected_missing_data",
            "entry_rejected_invalid_price",
            "entry_rejected_next_open_buy_zone",
            "entry_rejected_invalid_risk",
            "entry_rejected_no_cash",
        }
        if (~outcomes["outcome"].isin(valid_outcomes)).any():
            raise ValueError("daily funnel contains an unsupported entry outcome")
    session_index = pd.DatetimeIndex(sessions)
    signal_counts = (
        signals.assign(_entry_eligible=entry_eligible)
        .groupby("signal_date", sort=False)
        .agg(
            evaluated_count=("symbol", "size"),
            qualified_count=("_entry_eligible", "sum"),
        )
        .reindex(session_index, fill_value=0)
        .astype(int)
    )
    if outcomes.empty:
        outcome_counts = pd.DataFrame(
            {"attempted_count": 0, "executed_count": 0},
            index=session_index,
        )
    else:
        outcome_counts = (
            outcomes.assign(_executed=outcomes["outcome"] == "entries_executed")
            .groupby("signal_date", sort=False)
            .agg(
                attempted_count=("outcome", "size"),
                executed_count=("_executed", "sum"),
            )
            .reindex(session_index, fill_value=0)
            .astype(int)
        )
    rejected = outcome_counts["attempted_count"] - outcome_counts["executed_count"]
    if bool(
        (
            (outcome_counts["executed_count"] > outcome_counts["attempted_count"])
            | (rejected > outcome_counts["attempted_count"])
            | (outcome_counts["attempted_count"] > signal_counts["qualified_count"])
        ).any()
    ):
        raise ValueError("daily entry funnel violates qualification/attempt bounds")
    funnel = pd.DataFrame({
        "signal_date": [str(session.date()) for session in session_index],
        "evaluated_count": signal_counts["evaluated_count"].to_numpy(dtype=int),
        "qualified_count": signal_counts["qualified_count"].to_numpy(dtype=int),
        "attempted_count": outcome_counts["attempted_count"].to_numpy(dtype=int),
        "executed_count": outcome_counts["executed_count"].to_numpy(dtype=int),
        "rejected_count": rejected.to_numpy(dtype=int),
    })
    if int(funnel["evaluated_count"].sum()) != len(signals):
        raise ValueError("daily funnel evaluated total disagrees with signal log")
    qualified_total = int(funnel["qualified_count"].sum())
    attempted_total = int(funnel["attempted_count"].sum())
    if attempted_total != _nonnegative_diagnostic(result, "entry_attempts"):
        raise ValueError("daily funnel attempted total disagrees with diagnostics")
    if int(funnel["executed_count"].sum()) != _nonnegative_diagnostic(
        result, "entries_executed"
    ):
        raise ValueError("daily funnel executed total disagrees with diagnostics")
    capacity_truncated = _nonnegative_diagnostic(result, "capacity_truncated_signals")
    capacity_rejected = _nonnegative_diagnostic(result, "entry_rejected_capacity")
    if capacity_truncated != 0 or capacity_rejected != 0:
        raise ValueError("daily funnel contains an impossible uncapped capacity limit")
    expected_outcome_keys = {
        key for key in qualified_keys if key[1] != session_index[-1]
    }
    if outcome_keys != expected_outcome_keys:
        raise ValueError(
            "daily funnel attempts do not cover every non-final qualification"
        )
    final_pending = qualified_total - attempted_total
    if final_pending < 0:
        raise ValueError("daily funnel attempts/truncation exceed qualifications")
    final_qualified = int(funnel.iloc[-1]["qualified_count"])
    if final_pending > final_qualified:
        raise ValueError("daily funnel pending total exceeds final-session qualifications")
    if qualified_total != attempted_total + final_pending:
        raise ValueError("daily funnel qualification accounting is inconsistent")
    return funnel


def _validate_holding_identities(
    transactions: pd.DataFrame,
    contract: PriceIdentityTransitionContract,
    *,
    end_date: date,
) -> None:
    if transactions.empty:
        return
    if not {"Date", "Ticker", "Action", "Quantity"}.issubset(transactions):
        raise ValueError("transactions lack identity reconciliation fields")
    frame = transactions.copy().reset_index(names="_order")
    frame["Date"] = pd.to_datetime(frame["Date"], errors="raise").dt.date
    frame["Ticker"] = frame["Ticker"].astype(str).str.upper()
    frame["Action"] = frame["Action"].astype(str).str.upper()
    frame["Quantity"] = pd.to_numeric(frame["Quantity"], errors="raise")
    if (~frame["Action"].isin({"BUY", "SELL", "TRANSFER"})).any():
        raise ValueError("transactions contain an unsupported identity action")
    if any(not math.isfinite(float(value)) or float(value) <= 0 for value in frame["Quantity"]):
        raise ValueError("transactions contain an invalid identity quantity")
    frame = frame.sort_values(["Date", "_order"], kind="stable")
    quantities: dict[str, float] = {}
    for current_day, day_rows in frame.groupby("Date", sort=True):
        # The simulators emit an explicit zero-cash transfer before any same-day
        # exit/rebalance activity.  Apply it first so predecessor holdings cannot
        # be mistaken for stale or unsupported identities.
        for row in day_rows.itertuples(index=False):
            if row.Action != "TRANSFER":
                continue
            predecessor = str(getattr(row, "FromTicker", "")).upper()
            successor = str(row.Ticker).upper()
            if not predecessor or contract.resolve_open_holding(predecessor, current_day) != successor:
                raise ValueError("transaction contains an unapproved identity transfer")
            prior = quantities.get(predecessor, 0.0)
            if abs(prior - float(row.Quantity)) > 1e-5 or quantities.get(successor, 0.0) > 1e-5:
                raise ValueError("identity transfer quantity does not match the open holding")
            quantities.pop(predecessor, None)
            quantities[successor] = float(row.Quantity)
        for symbol, quantity in list(quantities.items()):
            if quantity > 1e-8 and contract.resolve_open_holding(symbol, current_day) != symbol:
                raise ValueError(
                    f"open holding requires an unimplemented identity transfer: {symbol}"
                )
        for row in day_rows.itertuples(index=False):
            if row.Action == "TRANSFER":
                continue
            if contract.resolve_open_holding(row.Ticker, current_day) != row.Ticker:
                raise ValueError(f"transaction uses a predecessor at its transition: {row.Ticker}")
            prior = quantities.get(row.Ticker, 0.0)
            if row.Action == "BUY":
                quantities[row.Ticker] = prior + float(row.Quantity)
            else:
                remaining = prior - float(row.Quantity)
                # Transaction quantities are serialized to six decimal places; allow
                # only the bounded rounding residue, never a material oversell.
                if remaining < -1e-5:
                    raise ValueError(f"transaction sells more than the open identity: {row.Ticker}")
                quantities[row.Ticker] = 0.0 if remaining <= 1e-5 else remaining
    for symbol, quantity in quantities.items():
        if quantity > 1e-8 and contract.resolve_open_holding(symbol, end_date) != symbol:
            raise ValueError(f"open holding crossed an unsupported ended identity: {symbol}")


def _metrics(result: SimulationResult) -> dict[str, object]:
    return {
        "total_return_pct": _finite(result.total_return_pct, field="CANSLIM total return"),
        "annualized_return_pct": _finite(
            result.annualized_return_pct, field="CANSLIM annualized return"
        ),
        "max_drawdown_pct": _finite(result.max_drawdown_pct, field="CANSLIM drawdown"),
        "sharpe_ratio": _finite(result.sharpe_ratio, field="CANSLIM Sharpe"),
        "win_rate_pct": _finite(result.win_rate, field="CANSLIM win rate"),
        "closed_trades": len(result.closed_trades),
        "average_cash_pct": _average_cash_pct(result),
    }


def _basket_metrics(result: LeaderBasketResult) -> dict[str, object]:
    quantities: dict[str, float] = {}
    costs: dict[str, float] = {}
    closed = 0
    wins = 0
    for row in result.transactions.itertuples(index=False):
        ticker = str(row.Ticker).upper()
        quantity = _finite(row.Quantity, field="basket quantity")
        price = _finite(row.Price, field="basket price")
        if quantity <= 0 or price <= 0:
            raise ValueError("leader-basket transaction is non-positive")
        if str(row.Action).upper() == "BUY":
            quantities[ticker] = quantities.get(ticker, 0.0) + quantity
            costs[ticker] = costs.get(ticker, 0.0) + quantity * price
        elif str(row.Action).upper() == "SELL" and quantities.get(ticker, 0.0) > 0:
            average_cost = costs[ticker] / quantities[ticker]
            closed += 1
            wins += int(price > average_cost)
            sold = min(quantity, quantities[ticker])
            quantities[ticker] -= sold
            costs[ticker] -= sold * average_cost
    return {
        "total_return_pct": _finite(result.total_return_pct, field="basket total return"),
        "annualized_return_pct": _finite(
            result.annualized_return_pct, field="basket annualized return"
        ),
        "max_drawdown_pct": _finite(result.max_drawdown_pct, field="basket drawdown"),
        "sharpe_ratio": _finite(result.sharpe_ratio, field="basket Sharpe"),
        "win_rate_pct": wins / closed * 100.0 if closed else 0.0,
        "closed_trades": closed,
        "average_cash_pct": _finite(result.average_cash_pct, field="basket average cash"),
        "rebalance_count": int(result.rebalance_count),
    }


def _alias_map(contract: LeaderIdentityContract) -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    for ticker, identity in contract.identities.items():
        if identity.continuity_kind in _SAME_ISSUER_KINDS:
            result[ticker] = tuple(sorted(
                candidate_ticker
                for candidate_ticker, candidate in contract.identities.items()
                if candidate.chain_id == identity.chain_id
                and candidate.continuity_kind in _SAME_ISSUER_KINDS
            ))
        else:
            result[ticker] = (ticker,)
    return result


def _load_task2_audit(
    *,
    bundle: PITDataBundle,
    union: set[str],
    provenance_path: Path,
    coverage_path: Path,
    master_path: Path,
    exclusions_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    provenance = _read_json(provenance_path)
    source_coverage = _read_json(coverage_path)
    if sha256_file(provenance_path) != bundle.metadata["fundamentals_provenance_sha256"]:
        raise ValueError("fundamentals provenance does not match the PIT bundle")
    bindings = {
        "fundamentals_coverage_sha256": coverage_path,
        "security_master_sha256": master_path,
        "security_master_exclusions_sha256": exclusions_path,
    }
    for field, path in bindings.items():
        if _digest(provenance.get(field), field=field) != sha256_file(path):
            raise ValueError(f"Task 2 provenance does not bind {field}")
    if provenance.get("start_date") != _WARMUP or provenance.get("end_date") != _END:
        raise ValueError("Task 2 provenance date contract does not match the baseline")
    if provenance.get("source") != "SEC EDGAR official bulk archives":
        raise ValueError("Task 2 provenance is not the approved SEC bulk source")
    archive_manifest = provenance.get("archive_manifest")
    if not isinstance(archive_manifest, dict) or not archive_manifest:
        raise ValueError("Task 2 provenance lacks archive source/retrieval facts")
    if provenance.get("public_date_rule") != _PUBLIC_DATE_RULE:
        raise ValueError("Task 2 public-date rule does not match the baseline")
    for field in (
        "submissions_archive_sha256", "companyfacts_archive_sha256",
        "identity_manifest_csv_sha256",
    ):
        if bundle.metadata[f"fundamentals_{field}"] != _digest(
            provenance.get(field), field=field
        ):
            raise ValueError(f"Task 2 source digest does not match the bundle: {field}")
    master = pd.read_csv(master_path, dtype=str, keep_default_na=False)
    exclusions = pd.read_csv(exclusions_path, dtype=str, keep_default_na=False)
    if tuple(master.columns) != _MASTER_COLUMNS or tuple(exclusions.columns) != _EXCLUSION_COLUMNS:
        raise ValueError("Task 2 security-master header is invalid")
    master["ticker"] = master["ticker"].str.upper()
    exclusions["ticker"] = exclusions["ticker"].str.upper()
    if master.empty or master[["ticker", "cik", "mapping_basis"]].eq("").any().any():
        raise ValueError("security master has blank required fields")
    if exclusions[["ticker", "reason"]].eq("").any().any():
        raise ValueError("security-master exclusion has a blank closed reason")
    if master.duplicated(
        ["ticker", "cik", "first_membership_date", "last_membership_date"]
    ).any():
        raise ValueError("security master has duplicate interval identities")
    resolved = set(master["ticker"])
    excluded = set(exclusions["ticker"])
    if resolved & excluded or resolved | excluded != union:
        raise ValueError("security-master resolved/excluded accounting is not exact")
    if any(len(cik) != 10 or not cik.isdigit() for cik in master["cik"]):
        raise ValueError("security master contains an invalid CIK")
    expected_counts = {
        "membership_union_symbol_count": len(union),
        "resolved_symbol_count": len(resolved),
        "explicitly_excluded_symbol_count": len(excluded),
    }
    for field, expected in expected_counts.items():
        if int(source_coverage.get(field, -1)) != expected:
            raise ValueError(f"Task 2 coverage does not match security-master rows: {field}")
    accounted_pct = len(resolved | excluded) / len(union) * 100.0
    resolved_pct = len(resolved) / len(union) * 100.0
    if not math.isclose(
        float(source_coverage.get("resolved_or_closed_exclusion_percentage", -1)),
        accounted_pct, abs_tol=1e-7,
    ):
        raise ValueError("Task 2 accounted CIK coverage is inconsistent")
    if not math.isclose(
        float(source_coverage.get("resolved_cik_percentage", -1)),
        resolved_pct, abs_tol=1e-7,
    ):
        raise ValueError("Task 2 resolved CIK coverage is inconsistent")
    audit = {
        "membership_union_symbol_count": len(union),
        "resolved_symbol_count": len(resolved),
        "resolved_cik_percentage": resolved_pct,
        "explicitly_excluded_symbol_count": len(excluded),
        "resolved_or_closed_exclusion_percentage": accounted_pct,
        "security_master": master.to_dict(orient="records"),
        "exclusions": exclusions.to_dict(orient="records"),
        "filed_date_fallback_count": int(source_coverage.get("filed_date_fallback_count", 0)),
        "filed_date_fallback_unique_count": int(
            source_coverage.get("filed_date_fallback_unique_count", 0)
        ),
        "task2_coverage": source_coverage,
        "task2_artifact_sha256": {
            field: sha256_file(path) for field, path in bindings.items()
        },
    }
    return provenance, audit


def _evaluated_coverage(signal_log: pd.DataFrame, bundle: PITDataBundle) -> dict[str, Any]:
    required = ["symbol", "signal_date", "current_growth", "annual_growth"]
    if signal_log.empty or not set(required).issubset(signal_log):
        raise ValueError("signal log cannot support evaluated PIT fundamental coverage")
    frame = signal_log[required].copy().reset_index(drop=True)
    frame["symbol"] = frame["symbol"].astype(str).str.upper()
    frame["signal_date"] = pd.to_datetime(frame["signal_date"], errors="raise").dt.date
    if frame.duplicated(["symbol", "signal_date"]).any():
        raise ValueError("evaluated PIT symbol-date rows are not unique")
    membership_by_date: dict[date, frozenset[str]] = {}
    for signal_date in frame["signal_date"]:
        if signal_date not in membership_by_date:
            membership_by_date[signal_date] = bundle.members_at(signal_date.isoformat())
    for row in frame.itertuples(index=False):
        if row.symbol not in membership_by_date[row.signal_date]:
            raise ValueError("signal log contains a symbol outside strict PIT membership")
    state_bounds = {
        str(symbol): (min(group), max(group))
        for symbol, group in frame.groupby("symbol", sort=False)["signal_date"]
    }
    state_dates: dict[str, list[date]] = {symbol: [] for symbol in state_bounds}
    state_growth: dict[str, list[tuple[float | None, float | None]]] = {
        symbol: [] for symbol in state_dates
    }
    for symbol, public_date, facts in bundle.iter_fundamental_state_boundaries(state_bounds):
        _c_score, bundle_current = evaluate_c(facts["quarterly_income"])
        _a_score, bundle_annual, _roe = evaluate_a(
            facts["annual_income"], balance_sheet=facts["balance_sheet"]
        )
        # The unchanged evaluators use NaN for an unavailable growth history.
        # Treat that sentinel as missing coverage; non-finite values emitted by
        # the signal log itself remain a hard error below.
        if bundle_current is not None and not math.isfinite(float(bundle_current)):
            bundle_current = None
        if bundle_annual is not None and not math.isfinite(float(bundle_annual)):
            bundle_annual = None
        state_dates[symbol].append(public_date)
        state_growth[symbol].append((bundle_current, bundle_annual))
    bundle_current_count = 0
    bundle_annual_count = 0
    bundle_both_count = 0
    for row in frame.itertuples(index=False):
        state_index = bisect_right(state_dates[row.symbol], row.signal_date) - 1
        if state_index < 0:
            bundle_current, bundle_annual = None, None
        else:
            bundle_current, bundle_annual = state_growth[row.symbol][state_index]
        current_ok = bundle_current is not None
        annual_ok = bundle_annual is not None

        def logged_growth(value: object, *, field: str) -> float | None:
            if pd.isna(value):
                return None
            number = float(value)
            if not math.isfinite(number):
                raise ValueError(f"signal log contains non-finite {field}")
            return number

        logged_current = logged_growth(row.current_growth, field="current growth")
        logged_annual = logged_growth(row.annual_growth, field="annual growth")
        if current_ok != (logged_current is not None):
            raise ValueError("signal-log current growth disagrees with hash-bound PIT fundamentals")
        if annual_ok != (logged_annual is not None):
            raise ValueError("signal-log annual growth disagrees with hash-bound PIT fundamentals")
        if current_ok and not math.isclose(
            float(bundle_current), logged_current, rel_tol=1e-12, abs_tol=1e-12
        ):
            raise ValueError("signal-log current growth value differs from the PIT evaluator")
        if annual_ok and not math.isclose(
            float(bundle_annual), logged_annual, rel_tol=1e-12, abs_tol=1e-12
        ):
            raise ValueError("signal-log annual growth value differs from the PIT evaluator")
        bundle_current_count += int(current_ok)
        bundle_annual_count += int(annual_ok)
        bundle_both_count += int(current_ok and annual_ok)
    return {
        "evaluated_symbol_date_count": len(frame),
        "usable_current_quarterly_count": bundle_current_count,
        "usable_annual_count": bundle_annual_count,
        "usable_current_quarterly_and_annual_count": bundle_both_count,
        "current_quarterly_and_annual_pct": bundle_both_count / len(frame) * 100.0,
        "coverage_basis": (
            "unique strict-PIT signal-log symbol/date rows independently recomputed from "
            "hash-bound as-of quarterly/annual frames with unchanged evaluate_c/evaluate_a"
        ),
    }


def _coverage(
    *,
    bundle: PITDataBundle,
    closes: pd.DataFrame,
    prices: Mapping[str, Any],
    task2: Mapping[str, Any],
    signal_log: pd.DataFrame,
) -> dict[str, Any]:
    sessions = pd.DatetimeIndex(closes.index).normalize()
    counts = [len(bundle.members_at(day)) for day in sessions]
    evaluated = _evaluated_coverage(signal_log, bundle)
    price_pct = _finite(prices.get("coverage_pct"), field="price coverage")
    price_manifest = bundle.manifest()["coverage"]["price"]
    gates = {
        "membership_495_through_510": {
            "passed": min(counts) >= 495 and max(counts) <= 510,
            "minimum": min(counts), "maximum": max(counts),
        },
        "spy_complete_2020_through_2025": {
            "passed": (
                str(prices.get("spy_first_date")) <= "2020-01-02"
                and str(prices.get("spy_last_date")) == _END
                and str(price_manifest["first_date"]) <= "2020-01-02"
                and str(price_manifest["last_date"]) == _END
            ),
            "first_date": prices.get("spy_first_date"),
            "last_date": prices.get("spy_last_date"),
        },
        "member_price_coverage_at_least_98_pct": {
            "passed": price_pct >= 98.0, "value_pct": price_pct, "threshold_pct": 98.0,
        },
        "cik_resolved_or_closed_exclusion_at_least_95_pct": {
            "passed": task2["resolved_or_closed_exclusion_percentage"] >= 95.0,
            "value_pct": task2["resolved_or_closed_exclusion_percentage"],
            "threshold_pct": 95.0,
        },
        "evaluated_pit_quarterly_and_annual_at_least_90_pct": {
            "passed": evaluated["current_quarterly_and_annual_pct"] >= 90.0,
            "value_pct": evaluated["current_quarterly_and_annual_pct"],
            "threshold_pct": 90.0,
        },
    }
    return {
        "schema_version": 1,
        "date_contract": {
            "warmup_start": _WARMUP, "evaluation_start": _START, "data_cutoff": _END,
        },
        "membership": {
            "event_count": len(bundle.membership.events),
            "evaluation_session_count": len(sessions),
            "evaluation_session_min_members": min(counts),
            "evaluation_session_max_members": max(counts),
            "union_symbol_count": task2["membership_union_symbol_count"],
        },
        "prices": {
            "coverage_pct": price_pct,
            "member_trading_day_pairs": prices.get("member_trading_day_pairs"),
            "covered_member_trading_day_pairs": prices.get("covered_member_trading_day_pairs"),
            "remaining_member_pair_gap_count": prices.get("remaining_member_pair_gap_count"),
            "symbols_with_no_prices": prices.get("symbols_with_no_prices", []),
            "symbols_with_partial_prices": prices.get("symbols_with_partial_prices", []),
            "spy_first_date": prices.get("spy_first_date"),
            "spy_last_date": prices.get("spy_last_date"),
        },
        "cik_and_exclusions": dict(task2),
        "evaluated_fundamentals": evaluated,
        "gates": gates,
        "all_gates_passed": all(item["passed"] for item in gates.values()),
        "bundle": bundle.manifest(),
    }


def _source_summary(sources: Mapping[str, Any]) -> dict[str, Any]:
    membership = sources["membership"]
    prices = sources["prices"]
    fundamentals = sources["fundamentals"]
    return {
        "membership": {
            key: membership.get(key) for key in ("source_url", "revision_id", "retrieved_at_utc")
        },
        "prices": {
            key: prices.get(key) for key in (
                "source_kind", "alpaca_retrieved_at_utc",
                "alpaca_raw_calibration_retrieved_at_utc",
            )
        },
        "fundamentals": {
            "source": fundamentals.get("source"),
            "archive_manifest": fundamentals.get("archive_manifest"),
        },
    }


def _report(
    summary: Mapping[str, Any],
    coverage: Mapping[str, Any],
    recall: pd.DataFrame,
    *,
    sources: Mapping[str, Any],
    config: Mapping[str, Any],
    diagnostics: Mapping[str, Any],
) -> str:
    missed = recall.loc[recall["entry_count"] == 0].nlargest(20, "total_return_pct")
    gate_columns = [column for column in recall if column.endswith("_fail_count")]
    gate_totals = {column: int(recall[column].sum()) for column in gate_columns}
    blocks = {
        "Coverage": coverage,
        "Source provenance": _source_summary(sources),
        "Active CANSLIM configuration": config,
        "Execution diagnostics": diagnostics,
        "Daily entry funnel": summary["entry_contract"],
        "Aggregate failed gates": gate_totals,
    }
    lines = [
        "# Five-year PIT CANSLIM baseline", "",
        "Five-year leader labels are ex-post diagnostics only.", "",
        "## Performance", "",
        f"- CANSLIM return: {summary['canslim']['total_return_pct']:.2f}%",
        f"- Leader basket return: {summary['leader_basket']['total_return_pct']:.2f}%",
        f"- SPY return: {summary['spy']['total_return_pct']:.2f}%", "",
        "## Recall", "",
        (
            "- Five-year raw/all: "
            f"{summary['leader_recall']['five_year']['raw_all']['signaled_count']}"
            f"/{summary['leader_recall']['five_year']['raw_all']['denominator_count']} "
            "signaled "
            f"({summary['leader_recall']['five_year']['raw_all']['signal_recall_pct']:.2f}%); "
            f"{summary['leader_recall']['five_year']['raw_all']['executed_count']}"
            f"/{summary['leader_recall']['five_year']['raw_all']['denominator_count']} "
            "executed "
            f"({summary['leader_recall']['five_year']['raw_all']['execution_recall_pct']:.2f}%)."
        ),
        (
            "- Five-year PIT-exposed (`member_at_start=True`): "
            f"{summary['leader_recall']['five_year']['pit_exposed_member_at_start']['signaled_count']}"
            f"/{summary['leader_recall']['five_year']['pit_exposed_member_at_start']['denominator_count']} "
            "signaled "
            f"({summary['leader_recall']['five_year']['pit_exposed_member_at_start']['signal_recall_pct']:.2f}%); "
            f"{summary['leader_recall']['five_year']['pit_exposed_member_at_start']['executed_count']}"
            f"/{summary['leader_recall']['five_year']['pit_exposed_member_at_start']['denominator_count']} "
            "executed "
            f"({summary['leader_recall']['five_year']['pit_exposed_member_at_start']['execution_recall_pct']:.2f}%)."
        ),
        (
            "- Rolling raw/all signal recall: "
            f"{summary['leader_recall']['rolling']['raw_all']['signaled_count']}"
            f"/{summary['leader_recall']['rolling']['raw_all']['denominator_count']} "
            f"({summary['leader_recall']['rolling']['raw_all']['signal_recall_pct']:.2f}%)."
        ),
        (
            "- Rolling PIT-exposed (`member_at_evaluation=True`) signal recall: "
            f"{summary['leader_recall']['rolling']['pit_exposed_member_at_evaluation']['signaled_count']}"
            f"/{summary['leader_recall']['rolling']['pit_exposed_member_at_evaluation']['denominator_count']} "
            f"({summary['leader_recall']['rolling']['pit_exposed_member_at_evaluation']['signal_recall_pct']:.2f}%)."
        ),
        "- Deprecated raw-count aliases: `top100_signaled`, `top100_executed`.", "",
        "## Canonical entry outcomes", "",
        f"- Daily evaluated symbol-days: {summary['entry_contract']['evaluated_symbol_days']}",
        f"- Contract-qualified signals: {summary['entry_contract']['qualified_signals']}",
        f"- Next-open executions: {summary['entry_contract']['executed_attempts']}",
        f"- Entry rejections: {summary['entry_contract']['rejected_attempts']}",
        "- Immutable attempt ledger: `entry_attempt_outcomes.csv`", "",
    ]
    for title, value in blocks.items():
        lines.extend([f"## {title}", "", f"```json\n{json.dumps(value, sort_keys=True)}\n```", ""])
    lines.extend(["## Largest missed leaders", ""])
    lines.append(
        "```text\n" + missed[["ticker", "rank", "total_return_pct"]].to_string(index=False)
        + "\n```" if not missed.empty else "None."
    )
    return "\n".join(lines) + "\n"


def _fixed_args(args: argparse.Namespace) -> None:
    expected = {
        "start_date": _START, "end_date": _END, "benchmark": _BENCHMARK,
        "leader_count": _LEADERS, "rebalance_days": _REBALANCE,
    }
    for field, value in expected.items():
        if getattr(args, field) != value:
            raise ValueError(f"fixed PIT baseline requires {field}={value}")


def _run_portfolio(
    simulator: Any,
    tickers: list[str],
    *,
    checkpoint_path: Path,
    progress_log_path: Path,
    resume: bool,
    checkpoint_every_days: int,
    code_identity: str,
) -> SimulationResult:
    """Run the production simulator with resumability, preserving test doubles."""

    kwargs: dict[str, Any] = {
        "start_date": _START,
        "end_date": _END,
        "benchmark_symbol": _BENCHMARK,
    }
    parameters = inspect.signature(simulator.run).parameters
    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    if accepts_kwargs or "checkpoint_path" in parameters:
        kwargs.update({
            "checkpoint_path": checkpoint_path,
            "progress_log_path": progress_log_path,
            "resume": resume,
            "checkpoint_every_days": checkpoint_every_days,
        })
        if accepts_kwargs or "checkpoint_code_identity" in parameters:
            kwargs["checkpoint_code_identity"] = code_identity
    return simulator.run(tickers, **kwargs)


def run_baseline(
    args: argparse.Namespace,
    *,
    portfolio_factory: Callable[..., Any] = PortfolioSimulator,
    basket_factory: Callable[..., Any] = LeaderBasketSimulator,
    require_clean_git: bool = True,
) -> Path:
    _fixed_args(args)
    worktree = Path(__file__).resolve().parent
    git_head = _git_identity(worktree, require_clean=require_clean_git)
    paths = {
        "pit_bundle": _regular_file(args.pit_bundle),
        "membership_provenance": _regular_file(args.membership_provenance),
        "prices_provenance": _regular_file(args.prices_provenance),
        "fundamentals_provenance": _regular_file(args.fundamentals_provenance),
        "fundamentals_coverage": _regular_file(args.fundamentals_coverage),
        "security_master": _regular_file(args.security_master),
        "security_master_exclusions": _regular_file(args.security_master_exclusions),
    }
    input_hashes = _snapshots(paths)
    expected_bundle_sha = _digest(args.bundle_sha256, field="bundle_sha256")
    if input_hashes["pit_bundle"] != expected_bundle_sha:
        raise ValueError("PIT bundle digest does not match --bundle-sha256")
    sources = {
        "membership": _read_json(paths["membership_provenance"]),
        "prices": _read_json(paths["prices_provenance"]),
        "fundamentals": _read_json(paths["fundamentals_provenance"]),
    }
    output_root = Path(args.output_root)
    resume = args.resume_checkpoint is not None
    if resume:
        run_dir, portfolio_checkpoint, portfolio_progress = _resume_run_paths(
            resume_checkpoint=args.resume_checkpoint,
            output_root=output_root,
            expected_bundle_sha=expected_bundle_sha,
        )
    else:
        output_root = output_root.resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        run_dir = output_root / (
            datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%SZ-")
            + expected_bundle_sha[:12]
        )
        run_dir.mkdir()
        portfolio_checkpoint = run_dir / "portfolio_checkpoint.json"
        portfolio_progress = run_dir / "portfolio_progress.jsonl"
    try:
        with PITDataBundle(paths["pit_bundle"], expected_sha256=expected_bundle_sha) as bundle:
            if bundle.metadata["evaluation_start"] != _START:
                raise ValueError("bundle evaluation_start is not the fixed baseline start")
            if str(bundle.data_cutoff.date()) != _END:
                raise ValueError("bundle cutoff is not the fixed baseline end")
            if bundle.metadata["warmup_start"] != _WARMUP:
                raise ValueError("bundle warmup_start is not the fixed baseline warmup")
            provenance_keys = {
                "membership_provenance": "membership_provenance_sha256",
                "prices_provenance": "prices_provenance_sha256",
                "fundamentals_provenance": "fundamentals_provenance_sha256",
            }
            for source, metadata_key in provenance_keys.items():
                if input_hashes[source] != bundle.metadata[metadata_key]:
                    raise ValueError(f"{source} does not match the PIT bundle")
            transitions = bundle.load_price_identity_transition_contract(paths["prices_provenance"])
            identities = LeaderIdentityContract.from_prices_provenance(
                paths["prices_provenance"],
                expected_sha256=bundle.metadata["prices_provenance_sha256"],
            )
            union = {event.ticker for event in bundle.membership.events}
            _, task2 = _load_task2_audit(
                bundle=bundle,
                union=union,
                provenance_path=paths["fundamentals_provenance"],
                coverage_path=paths["fundamentals_coverage"],
                master_path=paths["security_master"],
                exclusions_path=paths["security_master_exclusions"],
            )
            symbols = bundle.symbols()
            tickers = [ticker for ticker in symbols if ticker != _BENCHMARK]
            closes = bundle.fetch_closes(symbols, pd.Timestamp(_START), pd.Timestamp(_END))
            if _BENCHMARK not in closes or closes[_BENCHMARK].isna().any():
                raise ValueError("SPY close calendar is incomplete in the evaluation window")
            sessions = pd.DatetimeIndex(closes[_BENCHMARK].dropna().index).normalize()
            leaders = label_five_year_leaders(
                closes, bundle.membership, start_date=_START, end_date=_END,
                identity_contract=identities, top_n=_LEADERS,
            )
            rolling = label_rolling_leaders(
                closes, bundle.membership, start_date=_START, end_date=_END,
                identity_contract=identities, top_n=_LEADERS,
            )
            if len(leaders) != 100 or len(rolling) != 4_800:
                raise ValueError("fixed baseline leader labels are incomplete")
            portfolio = portfolio_factory(pit_bundle=bundle, signal_every_n_days=1)
            if hasattr(portfolio, "identity_transition_contract"):
                portfolio.identity_transition_contract = transitions
            result = _run_portfolio(
                portfolio,
                tickers,
                checkpoint_path=portfolio_checkpoint,
                progress_log_path=portfolio_progress,
                resume=resume,
                checkpoint_every_days=args.checkpoint_every_days,
                code_identity=git_head,
            )
            basket_config = LeaderBasketConfig(
                leader_count=100, rebalance_days=20, lookback_days=252,
                min_history_days=60, initial_capital=100_000.0,
            )
            basket = basket_factory(bundle, basket_config)
            if hasattr(basket, "identity_transition_contract"):
                basket.identity_transition_contract = transitions
            basket = basket.run(
                start_date=_START, end_date=_END, benchmark_symbol=_BENCHMARK, tickers=tickers,
            )
            _validate_portfolio(result, sessions, bundle.sha256)
            _validate_basket(
                basket, sessions,
                {**asdict(basket_config), "benchmark_symbol": _BENCHMARK,
                 "pit_bundle_sha256": bundle.sha256},
            )
            reconciliation = reconcile_signals_to_transactions(
                result.signal_log, result.transaction_log, result.execution_diagnostics,
                entry_outcomes=result.entry_outcomes,
                trading_days=sessions,
            )
            if reconciliation["capacity_blocked_count"] != 0:
                raise ValueError("uncapped fixed baseline reported a capacity block")
            if reconciliation["unattributed_cash_capacity_count"] != 0:
                raise ValueError("cash/capacity diagnostics cannot be assigned to exact symbols")
            _validate_holding_identities(
                result.transaction_log, transitions, end_date=date.fromisoformat(_END)
            )
            _validate_holding_identities(
                basket.transactions, transitions, end_date=date.fromisoformat(_END)
            )
            aliases = _alias_map(identities)
            recall = build_leader_recall_frame(
                leaders, result.signal_log, result.transaction_log,
                start_date=date.fromisoformat(_START),
                min_c_a_growth=DEFAULT_MIN_C_A_GROWTH,
                min_rs_score=float(result.config["min_rs_score"]),
                min_canslim_score=float(result.config["min_canslim_score"]),
                blocked_for_cash=reconciliation["blocked_for_cash_by_symbol"],
                blocked_for_capacity=reconciliation["blocked_for_capacity_by_symbol"],
                leader_aliases=aliases,
            )
            coverage = _coverage(
                bundle=bundle, closes=closes, prices=sources["prices"], task2=task2,
                signal_log=result.signal_log,
            )
            if not coverage["all_gates_passed"]:
                failed = [name for name, item in coverage["gates"].items() if not item["passed"]]
                fundamentals_gate = "evaluated_pit_quarterly_and_annual_at_least_90_pct"
                non_blocking = (
                    [fundamentals_gate]
                    if getattr(args, "allow_incomplete_fundamentals", False)
                    and failed == [fundamentals_gate]
                    else []
                )
                coverage["non_blocking_failed_gates"] = non_blocking
                coverage["baseline_publishable"] = not set(failed).difference(non_blocking)
                if not coverage["baseline_publishable"]:
                    _write_bytes(run_dir / "coverage.json", _json_bytes(coverage))
                    raise CoverageGateError(f"coverage gates failed: {failed}")
            else:
                coverage["non_blocking_failed_gates"] = []
                coverage["baseline_publishable"] = True
            five_year_recall = five_year_leader_recall_summary(recall)
            rolling_recall = rolling_label_recall_summary(
                rolling, result.signal_log, label_aliases=aliases
            )
            top_signaled = int(five_year_recall["raw_all"]["signaled_count"])
            top_executed = int(five_year_recall["raw_all"]["executed_count"])
            daily_entry_funnel = _daily_entry_funnel_frame(result, sessions)
            entry_outcomes = _entry_outcomes_frame(result)
            summary = {
                "canslim": _metrics(result),
                "leader_basket": _basket_metrics(basket),
                "spy": {"total_return_pct": _finite(
                    result.benchmark_return_pct, field="SPY total return"
                )},
                "leader_recall": {
                    "five_year": five_year_recall,
                    "rolling": rolling_recall,
                    "top100_signaled": top_signaled,
                    "top100_executed": top_executed,
                    "signal_recall_pct": five_year_recall["raw_all"][
                        "signal_recall_pct"
                    ],
                    "execution_recall_pct": five_year_recall["raw_all"][
                        "execution_recall_pct"
                    ],
                    "rolling_label_recall_pct": rolling_recall["raw_all"][
                        "signal_recall_pct"
                    ],
                    "deprecated_raw_count_aliases": {
                        "top100_signaled": "five_year.raw_all.signaled_count",
                        "top100_executed": "five_year.raw_all.executed_count",
                    },
                    "compatibility_aliases": {
                        "signal_recall_pct": "five_year.raw_all.signal_recall_pct",
                        "execution_recall_pct": (
                            "five_year.raw_all.execution_recall_pct"
                        ),
                        "rolling_label_recall_pct": (
                            "rolling.raw_all.signal_recall_pct"
                        ),
                    },
                },
                "coverage": {
                    "price_pct": coverage["prices"]["coverage_pct"],
                    "cik_pct": coverage["cik_and_exclusions"]["resolved_cik_percentage"],
                    "current_quarterly_and_annual_pct": coverage["evaluated_fundamentals"][
                        "current_quarterly_and_annual_pct"
                    ],
                    "all_gates_passed": coverage["all_gates_passed"],
                    "non_blocking_failed_gates": coverage["non_blocking_failed_gates"],
                },
                "entry_contract": {
                    "outcome_schema_version": ENTRY_ATTEMPT_OUTCOME_SCHEMA_VERSION,
                    "daily_session_count": int(len(daily_entry_funnel)),
                    "evaluated_symbol_days": int(daily_entry_funnel["evaluated_count"].sum()),
                    "qualified_signals": int(daily_entry_funnel["qualified_count"].sum()),
                    "attempted_signals": int(daily_entry_funnel["attempted_count"].sum()),
                    "executed_attempts": int(daily_entry_funnel["executed_count"].sum()),
                    "rejected_attempts": int(daily_entry_funnel["rejected_count"].sum()),
                    "next_open_buy_zone_rejections": reconciliation[
                        "next_open_buy_zone_rejected_count"
                    ],
                    "rejection_counts": reconciliation["rejection_counts"],
                },
            }
            active_config = json.loads(json.dumps(result.config, allow_nan=False))
            diagnostics = json.loads(json.dumps(result.execution_diagnostics, allow_nan=False))
            frames = {
                "five_year_leaders.csv": five_year_leaders_frame(leaders),
                "rolling_leader_labels.csv": rolling_leaders_frame(rolling),
                "canslim_signals.csv": result.signal_log,
                "entry_attempt_outcomes.csv": entry_outcomes,
                "daily_entry_funnel.csv": daily_entry_funnel,
                "transactions.csv": result.transaction_log,
                "weekly_holdings.csv": result.weekly_holdings,
                "equity_curve.csv": _equity_frame(result),
                "leader_basket_holdings.csv": basket.holdings,
                "leader_basket_transactions.csv": basket.transactions,
                "leader_basket_equity.csv": _basket_equity_frame(basket),
                "leader_recall.csv": recall,
            }
            for name, frame in frames.items():
                _write_frame(run_dir / name, frame)
            _write_bytes(run_dir / "coverage.json", _json_bytes(coverage))
            _write_bytes(run_dir / "summary.json", _json_bytes(summary))
            _write_bytes(
                run_dir / "report.md",
                _report(
                    summary, coverage, recall, sources=sources,
                    config=active_config, diagnostics=diagnostics,
                ).encode(),
            )
            _recheck_inputs(paths, input_hashes)
            if _git_identity(worktree, require_clean=require_clean_git) != git_head:
                raise ValueError("Git HEAD changed during the baseline run")
            if _json_bytes(result.config) != _json_bytes(active_config):
                raise ValueError("active CANSLIM configuration changed before publication")
            if _json_bytes(result.execution_diagnostics) != _json_bytes(diagnostics):
                raise ValueError("execution diagnostics changed before publication")
            artifact_items = tuple(sorted(run_dir.iterdir()))
            if any(not item.is_file() or item.is_symlink() for item in artifact_items):
                raise ValueError("run directory contains a non-regular artifact before manifest")
            artifact_hashes = {item.name: sha256_file(item) for item in artifact_items}
            if resume and not _RESUME_JOURNAL_FILENAMES.issubset(artifact_hashes):
                raise ValueError("resumed baseline is missing an execution journal before manifest")
            manifest = {
                "schema_version": 1, "status": "complete",
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
                "git_head": git_head,
                "date_contract": {
                    "warmup_start": _WARMUP, "evaluation_start": _START, "data_cutoff": _END,
                },
                "bundle_sha256": bundle.sha256,
                "bundle_metadata": bundle.metadata,
                "input_sha256": input_hashes,
                "source_provenance": sources,
                "arguments": {
                    key: str(value) if isinstance(value, Path) else value
                    for key, value in vars(args).items()
                },
                "canslim_config": active_config,
                "execution_diagnostics": diagnostics,
                "entry_attempt_outcome_schema_version": (
                    ENTRY_ATTEMPT_OUTCOME_SCHEMA_VERSION
                ),
                "entry_attempt_outcome_count": len(result.entry_outcomes),
                "execution_reconciliation": reconciliation,
                "coverage_status": {
                    "all_gates_passed": coverage["all_gates_passed"],
                    "baseline_publishable": coverage["baseline_publishable"],
                    "non_blocking_failed_gates": coverage["non_blocking_failed_gates"],
                },
                "leader_basket_config": asdict(basket_config),
                "leader_recall": summary["leader_recall"],
                "artifacts": artifact_hashes,
            }
            _write_bytes(run_dir / "run_manifest.json", _json_bytes(manifest))
    except Exception as exc:
        failure = {
            "schema_version": 1, "status": "failed",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "git_head": git_head, "input_sha256": input_hashes,
            "error_type": type(exc).__name__, "message": str(exc),
        }
        if not (run_dir / "run_failed.json").exists():
            _write_bytes(run_dir / "run_failed.json", _json_bytes(failure))
        raise
    return run_dir


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the immutable five-year PIT baseline")
    parser.add_argument("--pit-bundle", required=True)
    parser.add_argument("--bundle-sha256", required=True)
    parser.add_argument("--membership-provenance", required=True)
    parser.add_argument("--prices-provenance", required=True)
    parser.add_argument("--fundamentals-provenance", required=True)
    parser.add_argument("--fundamentals-coverage", required=True)
    parser.add_argument("--security-master", required=True)
    parser.add_argument("--security-master-exclusions", required=True)
    parser.add_argument("--start-date", type=_date, default=_START)
    parser.add_argument("--end-date", type=_date, default=_END)
    parser.add_argument("--benchmark", default=_BENCHMARK)
    parser.add_argument("--leader-count", type=_positive_int, default=_LEADERS)
    parser.add_argument("--rebalance-days", type=_positive_int, default=_REBALANCE)
    parser.add_argument("--output-root", required=True)
    parser.add_argument(
        "--resume-checkpoint",
        help=(
            "resume and publish the owning run in place; PATH must be "
            "OUTPUT_ROOT/run-<UTC>-<bundle-prefix>/portfolio_checkpoint.json, and that "
            "run directory must contain exactly portfolio_checkpoint.json, "
            "portfolio_progress.jsonl, and portfolio_state.jsonl"
        ),
    )
    parser.add_argument(
        "--checkpoint-every-days",
        type=_positive_int,
        default=20,
        help="persist a resumable portfolio checkpoint at this many trading days",
    )
    parser.add_argument(
        "--allow-incomplete-fundamentals",
        action="store_true",
        help=(
            "publish the baseline when the only failed gate is evaluated SEC "
            "quarterly+annual coverage; the shortfall remains explicit in coverage/report"
        ),
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    args.benchmark = str(args.benchmark).upper()
    try:
        run_dir = run_baseline(args)
    except Exception as exc:
        print(f"PIT baseline failed closed: {exc}")
        return 1
    print(run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
