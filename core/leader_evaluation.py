"""Point-in-time leader labels and CANSLIM recall diagnostics.

This module deliberately keeps *membership* separate from *outcome labels*.
An index addition can be used as ex-post confirmation that a company became a
leader, but it must not silently expand the tradable universe on dates before
that addition.  The helpers here are pure and operate on an approved price
frame plus an explicit membership-event file.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from statistics import median
from typing import Iterable, Mapping, Sequence

import pandas as pd

_MEMBERSHIP_HEADER = ("effective_date", "ticker", "member")
_MAX_EVENTS = 100_000
_MAX_SYMBOLS = 10_000
_MAX_FORWARD_DAYS = 1_825


def _symbol(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("ticker must be text")
    value = value.strip().upper()
    if not value or len(value) > 8 or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ.-" for ch in value):
        raise ValueError("ticker is not canonical")
    return value


def _parse_member(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if not isinstance(value, str):
        raise ValueError("member must be boolean")
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "add", "added", "member"}:
        return True
    if normalized in {"0", "false", "no", "remove", "removed", "nonmember"}:
        return False
    raise ValueError("member must be boolean")


@dataclass(frozen=True)
class MembershipEvent:
    """One point-in-time membership transition."""

    effective_date: date
    ticker: str
    member: bool

    def __post_init__(self) -> None:
        if not isinstance(self.effective_date, date):
            raise ValueError("effective_date must be a date")
        normalized = _symbol(self.ticker)
        if type(self.member) is not bool:
            raise ValueError("member must be boolean")
        object.__setattr__(self, "ticker", normalized)


@dataclass(frozen=True)
class PointInTimeUniverse:
    """Membership state reconstructed from dated transitions."""

    events: tuple[MembershipEvent, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.events, tuple) or len(self.events) > _MAX_EVENTS:
            raise ValueError("membership event count is invalid")
        seen: set[tuple[date, str]] = set()
        symbols: set[str] = set()
        previous: tuple[date, str] | None = None
        for event in self.events:
            if not isinstance(event, MembershipEvent):
                raise ValueError("membership events have the wrong type")
            key = (event.effective_date, event.ticker)
            if key in seen:
                raise ValueError("duplicate membership event")
            if previous is not None and key < previous:
                raise ValueError("membership events must be canonical-sorted")
            seen.add(key)
            symbols.add(event.ticker)
            previous = key
        if len(symbols) > _MAX_SYMBOLS:
            raise ValueError("membership symbol count is invalid")

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, object]]) -> "PointInTimeUniverse":
        events: list[MembershipEvent] = []
        for index, row in enumerate(rows):
            if index >= _MAX_EVENTS:
                raise ValueError("membership event count exceeds limit")
            if not isinstance(row, Mapping) or set(row) != set(_MEMBERSHIP_HEADER):
                raise ValueError("membership row shape is invalid")
            try:
                effective = date.fromisoformat(str(row["effective_date"]))
                events.append(
                    MembershipEvent(
                        effective_date=effective,
                        ticker=_symbol(row["ticker"]),
                        member=_parse_member(row["member"]),
                    )
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("membership row is invalid") from exc
        return cls(tuple(sorted(events, key=lambda item: (item.effective_date, item.ticker))) )

    @classmethod
    def from_csv(cls, path: Path) -> "PointInTimeUniverse":
        if not isinstance(path, Path) or not path.is_file() or path.is_symlink():
            raise ValueError("membership CSV must be a regular file")
        with path.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != _MEMBERSHIP_HEADER:
                raise ValueError("membership CSV header is invalid")
            return cls.from_rows(reader)

    def members_at(self, when: date | str) -> frozenset[str]:
        if isinstance(when, str):
            when = date.fromisoformat(when)
        if not isinstance(when, date):
            raise ValueError("membership date is invalid")
        state: dict[str, bool] = {}
        for event in self.events:
            if event.effective_date > when:
                break
            state[event.ticker] = event.member
        return frozenset(ticker for ticker, member in state.items() if member)


@dataclass(frozen=True)
class LeaderLabel:
    """One ex-post leader label for a historical evaluation date."""

    evaluation_date: date
    horizon_date: date
    ticker: str
    forward_return_pct: float
    rank: int
    future_index_member: bool

    def __post_init__(self) -> None:
        if not isinstance(self.evaluation_date, date) or not isinstance(self.horizon_date, date):
            raise ValueError("leader label dates are invalid")
        if self.evaluation_date >= self.horizon_date:
            raise ValueError("leader horizon must be after evaluation date")
        object.__setattr__(self, "ticker", _symbol(self.ticker))
        if type(self.forward_return_pct) not in {int, float} or not pd.notna(self.forward_return_pct):
            raise ValueError("leader return is invalid")
        if type(self.rank) is not int or self.rank < 1:
            raise ValueError("leader rank is invalid")
        if type(self.future_index_member) is not bool:
            raise ValueError("future_index_member must be boolean")


def label_future_leaders(
    closes: pd.DataFrame,
    evaluation_date: date | str,
    *,
    forward_trading_days: int = 252 * 3,
    top_n: int = 100,
    membership: PointInTimeUniverse | None = None,
    require_member_at_evaluation: bool = False,
) -> tuple[LeaderLabel, ...]:
    """Rank future returns without using prices after the label horizon.

    ``membership`` is optional so a broad historical universe can be labeled
    by future index inclusion.  When ``require_member_at_evaluation`` is true,
    only names that were members on the evaluation date are eligible, which is
    the live-style, no-survivorship version of the study.
    """
    if not isinstance(closes, pd.DataFrame) or closes.empty:
        raise ValueError("closes must be a non-empty DataFrame")
    if type(forward_trading_days) is not int or not 1 <= forward_trading_days <= _MAX_FORWARD_DAYS:
        raise ValueError("forward_trading_days is invalid")
    if type(top_n) is not int or not 1 <= top_n <= _MAX_SYMBOLS:
        raise ValueError("top_n is invalid")
    if isinstance(evaluation_date, str):
        evaluation_date = date.fromisoformat(evaluation_date)
    if not isinstance(evaluation_date, date):
        raise ValueError("evaluation_date is invalid")
    frame = closes.copy()
    frame.index = pd.to_datetime(frame.index).normalize()
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    before = frame.loc[:pd.Timestamp(evaluation_date)]
    if before.empty:
        return ()
    start_index = len(before) - 1
    horizon_index = start_index + forward_trading_days
    if horizon_index >= len(frame.index):
        return ()
    horizon_date = frame.index[horizon_index].date()
    eligible = (
        membership.members_at(evaluation_date)
        if membership is not None and require_member_at_evaluation
        else None
    )
    future_members = membership.members_at(horizon_date) if membership is not None else frozenset()
    observations: list[tuple[str, float]] = []
    for raw_symbol in frame.columns:
        ticker = _symbol(str(raw_symbol))
        if eligible is not None and ticker not in eligible:
            continue
        start_value = frame.iloc[start_index][raw_symbol]
        end_value = frame.iloc[horizon_index][raw_symbol]
        if pd.isna(start_value) or pd.isna(end_value) or float(start_value) <= 0:
            continue
        observations.append((ticker, (float(end_value) / float(start_value) - 1.0) * 100.0))
    observations.sort(key=lambda item: (-item[1], item[0]))
    return tuple(
        LeaderLabel(
            evaluation_date=evaluation_date,
            horizon_date=horizon_date,
            ticker=ticker,
            forward_return_pct=forward_return,
            rank=rank,
            future_index_member=ticker in future_members,
        )
        for rank, (ticker, forward_return) in enumerate(observations[:top_n], start=1)
    )


@dataclass(frozen=True)
class LeaderRecallReport:
    labels_considered: int
    future_index_leaders: int
    leaders_recalled: int
    recall_rate_pct: float
    qualifying_signals: int
    false_positive_signals: int
    median_lead_days: float | None


def score_leader_recall(
    signal_log: pd.DataFrame,
    labels: Sequence[LeaderLabel],
    *,
    lookback_days: int = 20,
) -> LeaderRecallReport:
    """Measure whether signals preceded labeled leaders in the same replay."""
    if not isinstance(signal_log, pd.DataFrame):
        raise ValueError("signal_log must be a DataFrame")
    if type(lookback_days) is not int or not 0 <= lookback_days <= 365:
        raise ValueError("lookback_days is invalid")
    required = {"signal_date", "symbol", "buy_signal"}
    if not required.issubset(signal_log.columns):
        raise ValueError("signal_log lacks leader-recall columns")
    signals: list[tuple[date, str]] = []
    for row in signal_log.loc[signal_log["buy_signal"].fillna(False).astype(bool)].itertuples():
        try:
            signals.append((pd.Timestamp(row.signal_date).date(), _symbol(str(row.symbol))))
        except (TypeError, ValueError):
            continue
    future_labels = [label for label in labels if label.future_index_member]
    recalled: list[float] = []
    for label in future_labels:
        matches = [
            (label.evaluation_date - when).days
            for when, ticker in signals
            if ticker == label.ticker
            and label.evaluation_date - timedelta(days=lookback_days) <= when <= label.evaluation_date
        ]
        if matches:
            recalled.append(float(min(matches)))
    leader_keys = {(label.evaluation_date, label.ticker) for label in future_labels}
    qualifying_signals = sum(
        1
        for when, ticker in signals
        if any(
            label_ticker == ticker
            and label_date - timedelta(days=lookback_days) <= when <= label_date
            for label_date, label_ticker in leader_keys
        )
    )
    return LeaderRecallReport(
        labels_considered=len(labels),
        future_index_leaders=len(future_labels),
        leaders_recalled=len(recalled),
        recall_rate_pct=(len(recalled) / len(future_labels) * 100.0) if future_labels else 0.0,
        qualifying_signals=qualifying_signals,
        false_positive_signals=max(len(signals) - qualifying_signals, 0),
        median_lead_days=median(recalled) if recalled else None,
    )
