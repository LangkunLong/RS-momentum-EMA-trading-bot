"""Deterministic, CSV-ready frames for the public PIT baseline outputs."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import date, timedelta
import math
from typing import Any, Mapping

import pandas as pd

from core.leader_evaluation import FiveYearLeader, RollingLeaderObservation

FIVE_YEAR_LEADER_COLUMNS = (
    "ticker",
    "first_price_date",
    "last_price_date",
    "total_return_pct",
    "rank",
    "member_at_start",
    "first_membership_date",
)
ROLLING_LEADER_COLUMNS = (
    "evaluation_date",
    "horizon_date",
    "ticker",
    "forward_return_pct",
    "rank",
    "member_at_evaluation",
    "member_at_horizon",
)
LEADER_RECALL_COLUMNS = (
    "ticker",
    "rank",
    "total_return_pct",
    "first_membership_date",
    "first_eligible_date",
    "first_buy_signal_date",
    "first_entry_date",
    "buy_signal_count",
    "entry_count",
    "blocked_for_cash_count",
    "blocked_for_capacity_count",
    "c_fail_count",
    "a_fail_count",
    "rs_fail_count",
    "breakout_fail_count",
    "volume_fail_count",
    "buy_zone_fail_count",
    "composite_fail_count",
    "missing_fundamentals_count",
)


def five_year_leaders_frame(leaders: Sequence[FiveYearLeader]) -> pd.DataFrame:
    """Return five-year labels in their stable CSV schema and order."""
    if not isinstance(leaders, Sequence):
        raise ValueError("leaders must be a sequence")
    rows: list[dict[str, object]] = []
    for leader in leaders:
        if not isinstance(leader, FiveYearLeader):
            raise ValueError("five-year leaders contain the wrong model")
        rows.append(
            {
                "ticker": leader.ticker,
                "first_price_date": leader.first_price_date.isoformat(),
                "last_price_date": leader.last_price_date.isoformat(),
                "total_return_pct": leader.total_return_pct,
                "rank": leader.rank,
                "member_at_start": leader.member_at_start,
                "first_membership_date": (
                    leader.first_membership_date.isoformat()
                    if leader.first_membership_date is not None
                    else ""
                ),
            }
        )
    return pd.DataFrame(rows, columns=FIVE_YEAR_LEADER_COLUMNS).sort_values(
        ["rank", "ticker"], kind="stable", ignore_index=True
    )


def rolling_leaders_frame(labels: Sequence[RollingLeaderObservation]) -> pd.DataFrame:
    """Return rolling labels in their stable CSV schema and order."""
    if not isinstance(labels, Sequence):
        raise ValueError("labels must be a sequence")
    rows: list[dict[str, object]] = []
    for label in labels:
        if not isinstance(label, RollingLeaderObservation):
            raise ValueError("rolling labels contain the wrong model")
        rows.append(
            {
                "evaluation_date": label.evaluation_date.isoformat(),
                "horizon_date": label.horizon_date.isoformat(),
                "ticker": label.ticker,
                "forward_return_pct": label.forward_return_pct,
                "rank": label.rank,
                "member_at_evaluation": label.member_at_evaluation,
                "member_at_horizon": label.member_at_horizon,
            }
        )
    return pd.DataFrame(rows, columns=ROLLING_LEADER_COLUMNS).sort_values(
        ["evaluation_date", "rank", "ticker"], kind="stable", ignore_index=True
    )


def _ticker_rows(
    frame: pd.DataFrame,
    tickers: set[str],
    *,
    ticker_column: str,
) -> pd.DataFrame:
    if frame.empty or ticker_column not in frame:
        return frame.iloc[0:0]
    return frame.loc[frame[ticker_column].astype(str).str.upper().isin(tickers)]


def build_leader_recall_frame(
    leaders: Sequence[FiveYearLeader],
    signal_log: pd.DataFrame,
    transaction_log: pd.DataFrame,
    *,
    start_date: date,
    min_c_a_growth: float,
    min_rs_score: float,
    min_canslim_score: float,
    blocked_for_cash: Mapping[str, int] | None = None,
    blocked_for_capacity: Mapping[str, int] | None = None,
    leader_aliases: Mapping[str, Sequence[str]] | None = None,
) -> pd.DataFrame:
    """Join top leaders to independently counted signal, execution, and gate facts."""
    if not isinstance(signal_log, pd.DataFrame) or not isinstance(transaction_log, pd.DataFrame):
        raise ValueError("signal and transaction logs must be DataFrames")
    if not isinstance(start_date, date):
        raise ValueError("start_date must be a date")
    signals = signal_log.copy()
    if not signals.empty:
        required = {
            "symbol", "signal_date", "buy_signal", "current_growth", "annual_growth",
            "rs_score", "has_breakout", "has_volume_surge", "in_buy_zone", "canslim_score",
        }
        if not required.issubset(signals):
            raise ValueError(f"signal log lacks recall fields: {sorted(required.difference(signals))}")
        signals["symbol"] = signals["symbol"].astype(str).str.upper()
        signals["signal_date"] = pd.to_datetime(signals["signal_date"], errors="raise")
    transactions = transaction_log.copy()
    if not transactions.empty:
        required_tx = {"Date", "Ticker", "Action"}
        if not required_tx.issubset(transactions):
            raise ValueError("transaction log lacks recall fields")
        transactions["Ticker"] = transactions["Ticker"].astype(str).str.upper()
        transactions["Date"] = pd.to_datetime(transactions["Date"], errors="raise")
    cash = {str(key).upper(): int(value) for key, value in (blocked_for_cash or {}).items()}
    capacity = {str(key).upper(): int(value) for key, value in (blocked_for_capacity or {}).items()}
    rows: list[dict[str, Any]] = []
    for leader in leaders:
        if not isinstance(leader, FiveYearLeader):
            raise ValueError("leaders contain the wrong model")
        ticker = leader.ticker
        aliases = {
            str(value).upper()
            for value in (leader_aliases or {}).get(ticker, (ticker,))
        }
        if ticker not in aliases:
            raise ValueError("leader alias map must include the reporting ticker")
        ticker_signals = _ticker_rows(signals, aliases, ticker_column="symbol")
        ticker_entries = _ticker_rows(transactions, aliases, ticker_column="Ticker")
        if not ticker_entries.empty:
            ticker_entries = ticker_entries.loc[ticker_entries["Action"].astype(str).str.upper() == "BUY"]
        buy_rows = ticker_signals.loc[ticker_signals["buy_signal"].fillna(False).astype(bool)] if not ticker_signals.empty else ticker_signals

        def fail_numeric(column: str, threshold: float, rows: pd.DataFrame = ticker_signals) -> int:
            if column not in rows:
                return 0
            values = pd.to_numeric(rows[column], errors="coerce")
            return int((values.notna() & (values < threshold)).sum())

        def fail_bool(column: str, rows: pd.DataFrame = ticker_signals) -> int:
            if column not in rows:
                return 0
            values = rows[column]
            return int(sum(value is False or value == 0 for value in values if pd.notna(value)))

        first_eligible = max(start_date, leader.first_membership_date) if leader.first_membership_date else start_date
        rows.append(
            {
                "ticker": ticker,
                "rank": leader.rank,
                "total_return_pct": leader.total_return_pct,
                "first_membership_date": leader.first_membership_date.isoformat() if leader.first_membership_date else "",
                "first_eligible_date": first_eligible.isoformat(),
                "first_buy_signal_date": buy_rows["signal_date"].min().date().isoformat() if not buy_rows.empty else "",
                "first_entry_date": ticker_entries["Date"].min().date().isoformat() if not ticker_entries.empty else "",
                "buy_signal_count": int(len(buy_rows)),
                "entry_count": int(len(ticker_entries)),
                "blocked_for_cash_count": sum(cash.get(alias, 0) for alias in aliases),
                "blocked_for_capacity_count": sum(capacity.get(alias, 0) for alias in aliases),
                "c_fail_count": fail_numeric("current_growth", min_c_a_growth),
                "a_fail_count": fail_numeric("annual_growth", min_c_a_growth),
                "rs_fail_count": fail_numeric("rs_score", min_rs_score),
                "breakout_fail_count": fail_bool("has_breakout"),
                "volume_fail_count": fail_bool("has_volume_surge"),
                "buy_zone_fail_count": fail_bool("in_buy_zone"),
                "composite_fail_count": fail_numeric("canslim_score", min_canslim_score),
                "missing_fundamentals_count": int(
                    (
                        pd.to_numeric(ticker_signals["current_growth"], errors="coerce").isna()
                        | pd.to_numeric(ticker_signals["annual_growth"], errors="coerce").isna()
                    ).sum()
                ) if {"current_growth", "annual_growth"}.issubset(ticker_signals) else 0,
            }
        )
    return pd.DataFrame(rows, columns=LEADER_RECALL_COLUMNS).sort_values(
        ["rank", "ticker"], kind="stable", ignore_index=True
    )


def reconcile_signals_to_transactions(
    signal_log: pd.DataFrame,
    transaction_log: pd.DataFrame,
    execution_diagnostics: Mapping[str, int],
    *,
    entry_outcomes: Sequence[object] = (),
    trading_days: Sequence[date | str | pd.Timestamp],
) -> dict[str, Any]:
    """Reconcile every attempted signal to one concrete terminal outcome."""
    if not isinstance(signal_log, pd.DataFrame) or not isinstance(transaction_log, pd.DataFrame):
        raise ValueError("signal and transaction logs must be DataFrames")
    calendar = sorted({pd.Timestamp(value).normalize() for value in trading_days})
    if not calendar:
        raise ValueError("execution reconciliation requires trading days")
    next_session = {current: following for current, following in zip(calendar, calendar[1:], strict=False)}
    signals = signal_log.copy()
    transactions = transaction_log.copy()
    if not signals.empty and not {"symbol", "signal_date", "buy_signal"}.issubset(signals):
        raise ValueError("signal log lacks execution reconciliation fields")
    if not transactions.empty and not {"Ticker", "Date", "Action"}.issubset(transactions):
        raise ValueError("transaction log lacks execution reconciliation fields")
    buy_signals = signals.loc[
        signals.get("buy_signal", pd.Series(False, index=signals.index)).fillna(False).astype(bool)
    ].copy().reset_index(drop=True)
    entries = transactions.loc[
        transactions.get("Action", pd.Series("", index=transactions.index)).astype(str).str.upper() == "BUY"
    ].copy()
    if not buy_signals.empty:
        buy_signals["signal_date"] = pd.to_datetime(
            buy_signals["signal_date"], errors="raise"
        ).dt.normalize()
        buy_signals["symbol"] = buy_signals["symbol"].astype(str).str.upper()
        if buy_signals.duplicated(["symbol", "signal_date"]).any():
            raise ValueError("qualifying signal rows are not unique by symbol/session")
        if not set(buy_signals["signal_date"]).issubset(calendar):
            raise ValueError("qualifying signal date is not a benchmark trading session")
    if not entries.empty:
        entries["Date"] = pd.to_datetime(entries["Date"], errors="raise").dt.normalize()
        entries["Ticker"] = entries["Ticker"].astype(str).str.upper()
        if not set(entries["Date"]).issubset(calendar):
            raise ValueError("entry date is not a benchmark trading session")

    outcome_fields = (
        "symbol", "signal_date", "entry_date", "pivot", "buy_zone_lower",
        "buy_zone_upper", "entry_open", "outcome",
    )
    outcome_records: list[dict[str, object]] = []
    for item in entry_outcomes:
        primitive = item.to_primitive() if hasattr(item, "to_primitive") else item
        if (
            not isinstance(primitive, Mapping)
            or len(primitive) != len(outcome_fields)
            or set(primitive) != set(outcome_fields)
        ):
            raise ValueError("entry outcome schema is invalid")
        outcome_records.append({field: primitive[field] for field in outcome_fields})
    outcomes = pd.DataFrame(outcome_records, columns=outcome_fields)
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
    if not outcomes.empty:
        outcomes["symbol"] = outcomes["symbol"].astype(str).str.upper()
        outcomes["signal_date"] = pd.to_datetime(
            outcomes["signal_date"], errors="raise"
        ).dt.normalize()
        outcomes["entry_date"] = pd.to_datetime(
            outcomes["entry_date"], errors="raise"
        ).dt.normalize()
        if outcomes.duplicated(["symbol", "signal_date"]).any():
            raise ValueError("entry outcomes are not unique by attempted symbol/session")
        if (~outcomes["outcome"].isin(valid_outcomes)).any():
            raise ValueError("entry outcome contains an unsupported terminal value")
        if not set(outcomes["signal_date"]).issubset(calendar):
            raise ValueError("entry outcome signal date is not a benchmark trading session")
        if not set(outcomes["entry_date"]).issubset(calendar):
            raise ValueError("entry outcome entry date is not a benchmark trading session")
        for row in outcomes.itertuples(index=False):
            if next_session.get(row.signal_date) != row.entry_date:
                raise ValueError("entry outcome is not on the next benchmark session")
            for field in ("pivot", "buy_zone_lower", "buy_zone_upper", "entry_open"):
                value = getattr(row, field)
                if pd.notna(value) and not math.isfinite(float(value)):
                    raise ValueError(f"entry outcome contains non-finite {field}")
            if pd.notna(row.pivot):
                pivot = float(row.pivot)
                if pivot <= 0:
                    raise ValueError("entry outcome contains a non-positive pivot")
                if pd.isna(row.buy_zone_lower) or pd.isna(row.buy_zone_upper):
                    raise ValueError("entry outcome with a pivot lacks buy-zone bounds")
                if not math.isclose(float(row.buy_zone_lower), pivot, rel_tol=0, abs_tol=1e-12):
                    raise ValueError("entry outcome lower buy-zone bound disagrees with pivot")
                if not math.isclose(
                    float(row.buy_zone_upper), pivot * 1.05, rel_tol=0, abs_tol=1e-12
                ):
                    raise ValueError("entry outcome upper buy-zone bound disagrees with pivot")
            elif pd.notna(row.buy_zone_lower) or pd.notna(row.buy_zone_upper):
                raise ValueError("entry outcome without a pivot contains buy-zone bounds")
            if pd.notna(row.entry_open) and float(row.entry_open) <= 0:
                raise ValueError("entry outcome contains a non-positive entry open")
            if row.outcome == "entries_executed":
                if pd.isna(row.entry_open):
                    raise ValueError("successful entry outcome lacks an entry open")
                if pd.notna(row.pivot) and not (
                    float(row.buy_zone_lower)
                    <= float(row.entry_open)
                    <= float(row.buy_zone_upper)
                ):
                    raise ValueError("successful entry outcome opened outside its buy zone")
            if row.outcome == "entry_rejected_next_open_buy_zone":
                if pd.isna(row.pivot) or pd.isna(row.entry_open):
                    raise ValueError("next-open buy-zone rejection lacks price facts")
                if float(row.buy_zone_lower) <= float(row.entry_open) <= float(
                    row.buy_zone_upper
                ):
                    raise ValueError("next-open buy-zone rejection opened inside its buy zone")

    buy_keys = {
        (row.symbol, row.signal_date)
        for row in buy_signals.itertuples(index=False)
    }
    outcome_keys = {
        (row.symbol, row.signal_date)
        for row in outcomes.itertuples(index=False)
    }
    if not outcome_keys.issubset(buy_keys):
        raise ValueError("entry outcome has no unique qualifying signal")

    entry_keys = Counter(
        (row.Ticker, row.Date) for row in entries.itertuples(index=False)
    )
    if any(count != 1 for count in entry_keys.values()):
        raise ValueError("entry transactions are not unique by symbol/session")
    executed = outcomes.loc[outcomes["outcome"] == "entries_executed"]
    executed_keys = Counter(
        (row.symbol, row.entry_date) for row in executed.itertuples(index=False)
    )
    if executed_keys != entry_keys:
        raise ValueError("successful entry outcomes do not match BUY transactions exactly")
    rejected_keys = {
        (row.symbol, row.entry_date)
        for row in outcomes.loc[outcomes["outcome"] != "entries_executed"].itertuples(
            index=False
        )
    }
    if rejected_keys.intersection(entry_keys):
        raise ValueError("a rejected entry outcome has a matching BUY transaction")

    def diagnostic(name: str) -> int:
        raw = execution_diagnostics.get(name, 0)
        if isinstance(raw, bool) or int(raw) != raw or int(raw) < 0:
            raise ValueError(f"execution diagnostic must be a nonnegative integer: {name}")
        return int(raw)

    signal_count = int(len(buy_signals))
    entry_count = int(len(entries))
    if diagnostic("buy_signal_rows") != signal_count:
        raise ValueError("buy-signal diagnostics do not match signal log")
    if diagnostic("entries_executed") != entry_count:
        raise ValueError("entry diagnostics do not match transactions")
    rejection_names = (
        "entry_rejected_already_open",
        "entry_rejected_capacity",
        "entry_rejected_missing_data",
        "entry_rejected_invalid_price",
        "entry_rejected_next_open_buy_zone",
        "entry_rejected_invalid_risk",
        "entry_rejected_no_cash",
    )
    rejection_counts = {name: diagnostic(name) for name in rejection_names}
    attempts = diagnostic("entry_attempts")
    outcome_counts = Counter(outcomes["outcome"]) if not outcomes.empty else Counter()
    if diagnostic("entries_executed") != outcome_counts["entries_executed"]:
        raise ValueError("successful outcome count does not match execution diagnostics")
    for name, count in rejection_counts.items():
        if count != outcome_counts[name]:
            raise ValueError(f"outcome count does not match execution diagnostic: {name}")
    if attempts != len(outcomes) or attempts != entry_count + sum(rejection_counts.values()):
        raise ValueError(
            "entry attempts/outcomes do not equal executions plus mutually exclusive rejections"
        )
    blocked_market = sum(
        diagnostic(name)
        for name in (
            "buy_signal_rows_blocked_by_regime",
            "buy_signal_rows_blocked_by_market",
            "buy_signal_rows_blocked_by_both",
        )
    )
    entries_allowed = diagnostic("buy_signal_rows_when_entries_allowed")
    if signal_count != entries_allowed + blocked_market:
        raise ValueError("buy signals do not reconcile to admitted and market/regime-blocked rows")
    capacity_truncated = diagnostic("capacity_truncated_signals")
    final_pending = entries_allowed - capacity_truncated - attempts
    if final_pending < 0:
        raise ValueError("entry attempts and capacity truncation exceed admitted buy signals")
    last_session_signal_count = int((buy_signals["signal_date"] == calendar[-1]).sum())
    if final_pending > last_session_signal_count:
        raise ValueError("final pending signal count exceeds last-session qualifying signals")

    rejected_total = sum(rejection_counts.values())
    cash_by_symbol = Counter(
        outcomes.loc[outcomes["outcome"] == "entry_rejected_no_cash", "symbol"]
    )
    capacity_by_symbol = Counter(
        outcomes.loc[outcomes["outcome"] == "entry_rejected_capacity", "symbol"]
    )
    next_open_by_symbol = Counter(
        outcomes.loc[
            outcomes["outcome"] == "entry_rejected_next_open_buy_zone", "symbol"
        ]
    )
    cash_blocked = rejection_counts["entry_rejected_no_cash"]
    capacity_blocked = rejection_counts["entry_rejected_capacity"]

    return {
        "buy_signal_count": signal_count,
        "entry_count": entry_count,
        "entry_attempt_count": attempts,
        "entry_rejection_count": rejected_total,
        "next_open_buy_zone_rejected_count": rejection_counts[
            "entry_rejected_next_open_buy_zone"
        ],
        "cash_blocked_count": cash_blocked,
        "capacity_blocked_count": capacity_blocked,
        "capacity_truncated_count": capacity_truncated,
        "final_pending_count": final_pending,
        "unattributed_rejection_count": 0,
        "unattributed_cash_capacity_count": 0,
        "blocked_for_next_open_buy_zone_by_symbol": dict(
            sorted(next_open_by_symbol.items())
        ),
        "blocked_for_cash_by_symbol": dict(sorted(cash_by_symbol.items())),
        "blocked_for_capacity_by_symbol": dict(sorted(capacity_by_symbol.items())),
        "rejection_counts": rejection_counts,
    }


def rolling_label_recall_pct(
    labels: Sequence[RollingLeaderObservation],
    signal_log: pd.DataFrame,
    *,
    lookback_days: int = 20,
    label_aliases: Mapping[str, Sequence[str]] | None = None,
) -> float:
    if not labels:
        return 0.0
    signals = signal_log.copy()
    if signals.empty:
        return 0.0
    signals = signals.loc[signals["buy_signal"].fillna(False).astype(bool)]
    signals["signal_date"] = pd.to_datetime(signals["signal_date"], errors="raise").dt.date
    signals["symbol"] = signals["symbol"].astype(str).str.upper()
    recalled = 0
    for label in labels:
        aliases = {
            str(value).upper()
            for value in (label_aliases or {}).get(label.ticker, (label.ticker,))
        }
        if label.ticker not in aliases:
            raise ValueError("rolling label alias map must include the reporting ticker")
        floor = label.evaluation_date - timedelta(days=lookback_days)
        if any(
            ticker in aliases and floor <= when <= label.evaluation_date
            for when, ticker in zip(signals["signal_date"], signals["symbol"], strict=True)
        ):
            recalled += 1
    return recalled / len(labels) * 100.0
