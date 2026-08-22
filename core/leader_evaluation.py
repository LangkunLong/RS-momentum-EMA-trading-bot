"""Point-in-time leader labels and CANSLIM recall diagnostics.

This module deliberately keeps *membership* separate from *outcome labels*.
An index addition can be used as ex-post confirmation that a company became a
leader, but it must not silently expand the tradable universe on dates before
that addition.  The helpers here are pure and operate on an approved price
frame plus an explicit membership-event file.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from statistics import median
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence

import pandas as pd

_MEMBERSHIP_HEADER = ("effective_date", "ticker", "member")
_MAX_EVENTS = 100_000
_MAX_SYMBOLS = 10_000
_MAX_FORWARD_DAYS = 1_825
_SAME_ISSUER_CONTINUITIES = {
    "same_issuer_rename",
    "same_issuer_ticker_reuse",
    "legacy_survivor_rename",
    "accounting_acquirer_rename",
}
_IDENTITY_FIELDS = {
    "provider_symbol",
    "identity_asof",
    "admitted_start",
    "admitted_end",
    "chain_id",
    "continuity_kind",
    "warmup_predecessor",
    "factor_anchor",
}


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


@dataclass(frozen=True)
class FiveYearLeader:
    """One top total-return security from the fixed five-year window."""

    ticker: str
    first_price_date: date
    last_price_date: date
    total_return_pct: float
    rank: int
    member_at_start: bool
    first_membership_date: date | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ticker", _symbol(self.ticker))
        if not isinstance(self.first_price_date, date) or not isinstance(self.last_price_date, date):
            raise ValueError("five-year leader price dates are invalid")
        if self.first_price_date > self.last_price_date:
            raise ValueError("five-year leader price date range is invalid")
        if type(self.total_return_pct) not in {int, float} or not math.isfinite(self.total_return_pct):
            raise ValueError("five-year leader return is invalid")
        if type(self.rank) is not int or self.rank < 1:
            raise ValueError("five-year leader rank is invalid")
        if type(self.member_at_start) is not bool:
            raise ValueError("member_at_start must be boolean")
        if self.first_membership_date is not None and not isinstance(self.first_membership_date, date):
            raise ValueError("first_membership_date is invalid")


@dataclass(frozen=True)
class RollingLeaderObservation:
    """One ex-post one-year leader observation and its PIT membership facts."""

    evaluation_date: date
    horizon_date: date
    ticker: str
    forward_return_pct: float
    rank: int
    member_at_evaluation: bool
    member_at_horizon: bool

    def __post_init__(self) -> None:
        if not isinstance(self.evaluation_date, date) or not isinstance(self.horizon_date, date):
            raise ValueError("rolling leader dates are invalid")
        if self.evaluation_date >= self.horizon_date:
            raise ValueError("rolling leader horizon must follow evaluation")
        object.__setattr__(self, "ticker", _symbol(self.ticker))
        if type(self.forward_return_pct) not in {int, float} or not math.isfinite(self.forward_return_pct):
            raise ValueError("rolling leader return is invalid")
        if type(self.rank) is not int or self.rank < 1:
            raise ValueError("rolling leader rank is invalid")
        if type(self.member_at_evaluation) is not bool or type(self.member_at_horizon) is not bool:
            raise ValueError("rolling leader membership facts must be boolean")


@dataclass(frozen=True)
class LeaderPriceIdentity:
    ticker: str
    admitted_start: date
    admitted_end: date
    chain_id: str
    continuity_kind: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "ticker", _symbol(self.ticker))
        if not isinstance(self.admitted_start, date) or not isinstance(self.admitted_end, date):
            raise ValueError("leader price identity dates are invalid")
        if self.admitted_end < self.admitted_start:
            raise ValueError("leader price identity range is invalid")
        if not isinstance(self.chain_id, str) or not self.chain_id.strip():
            raise ValueError("leader price identity chain is invalid")
        if not isinstance(self.continuity_kind, str) or not self.continuity_kind.strip():
            raise ValueError("leader price identity continuity is invalid")


@dataclass(frozen=True)
class LeaderIdentityContract:
    """Reviewed price identities used to deduplicate same-issuer label chains."""

    provenance_sha256: str
    request_contracts_sha256: str
    identities: Mapping[str, LeaderPriceIdentity]

    def __post_init__(self) -> None:
        for digest in (self.provenance_sha256, self.request_contracts_sha256):
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError("leader identity contract digest is invalid")
        frozen = dict(self.identities)
        if not frozen or any(key != value.ticker for key, value in frozen.items()):
            raise ValueError("leader identity contract mapping is invalid")
        object.__setattr__(self, "identities", MappingProxyType(frozen))

    @classmethod
    def from_prices_provenance(
        cls,
        path: Path,
        *,
        expected_sha256: str,
    ) -> "LeaderIdentityContract":
        if not isinstance(path, Path) or not path.is_file() or path.is_symlink():
            raise ValueError("prices provenance must be a regular non-link file")
        payload = path.read_bytes()
        provenance_sha = hashlib.sha256(payload).hexdigest()
        if provenance_sha != expected_sha256:
            raise ValueError("prices provenance digest does not match")
        try:
            provenance = json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("prices provenance JSON is invalid") from exc
        if not isinstance(provenance, dict):
            raise ValueError("prices provenance must contain an object")
        raw_contracts = provenance.get("price_identity_request_contracts")
        if not isinstance(raw_contracts, dict) or not raw_contracts:
            raise ValueError("prices provenance has no identity request contracts")
        canonical = (json.dumps(raw_contracts, sort_keys=True, separators=(",", ":")) + "\n").encode()
        contracts_sha = hashlib.sha256(canonical).hexdigest()
        if provenance.get("price_identity_request_contracts_sha256") != contracts_sha:
            raise ValueError("price identity request contract digest is inconsistent")
        identities: dict[str, LeaderPriceIdentity] = {}
        for raw_ticker, raw_identity in raw_contracts.items():
            ticker = _symbol(raw_ticker)
            if ticker != raw_ticker or not isinstance(raw_identity, dict) or set(raw_identity) != _IDENTITY_FIELDS:
                raise ValueError("price identity request contract row is invalid")
            try:
                date.fromisoformat(str(raw_identity["identity_asof"]))
                if not isinstance(raw_identity["chain_id"], str) or not isinstance(
                    raw_identity["continuity_kind"], str
                ):
                    raise ValueError("price identity chain fields must be text")
                identity = LeaderPriceIdentity(
                    ticker=ticker,
                    admitted_start=date.fromisoformat(str(raw_identity["admitted_start"])),
                    admitted_end=date.fromisoformat(str(raw_identity["admitted_end"])),
                    chain_id=str(raw_identity["chain_id"]),
                    continuity_kind=str(raw_identity["continuity_kind"]),
                )
            except ValueError as exc:
                raise ValueError("price identity request contract row is invalid") from exc
            identities[ticker] = identity
        return cls(provenance_sha, contracts_sha, identities)


def _date(value: date | str, *, field: str) -> date:
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{field} is invalid") from exc
    if not isinstance(value, date):
        raise ValueError(f"{field} is invalid")
    return value


def _leader_closes(closes: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(closes, pd.DataFrame) or closes.empty:
        raise ValueError("closes must be a non-empty DataFrame")
    frame = closes.copy()
    try:
        frame.index = pd.to_datetime(frame.index, errors="raise").normalize()
    except (TypeError, ValueError) as exc:
        raise ValueError("closes index contains invalid dates") from exc
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    columns = [_symbol(str(value)) for value in frame.columns]
    if len(columns) != len(set(columns)):
        raise ValueError("closes has duplicate canonical ticker columns")
    frame.columns = columns
    if "SPY" not in frame.columns:
        raise ValueError("closes must contain SPY trading sessions")
    return frame.apply(pd.to_numeric, errors="coerce")


def _valid_price(value: object) -> bool:
    if pd.isna(value):
        return False
    number = float(value)
    return math.isfinite(number) and number > 0.0


def _membership_union(membership: PointInTimeUniverse) -> tuple[str, ...]:
    if not isinstance(membership, PointInTimeUniverse):
        raise ValueError("membership must be a PointInTimeUniverse")
    return tuple(sorted({event.ticker for event in membership.events}))


def _leader_chains(
    membership: PointInTimeUniverse,
    identity_contract: LeaderIdentityContract,
) -> tuple[tuple[str, ...], ...]:
    if not isinstance(identity_contract, LeaderIdentityContract):
        raise ValueError("identity_contract must be a LeaderIdentityContract")
    membership_symbols = _membership_union(membership)
    missing = set(membership_symbols).difference(identity_contract.identities)
    if missing:
        raise ValueError(f"leader identity contract omits membership tickers: {sorted(missing)}")
    grouped: dict[str, list[str]] = {}
    for ticker in membership_symbols:
        identity = identity_contract.identities[ticker]
        key = (
            f"chain:{identity.chain_id}"
            if identity.continuity_kind in _SAME_ISSUER_CONTINUITIES
            else f"ticker:{ticker}"
        )
        grouped.setdefault(key, []).append(ticker)
    chains = tuple(tuple(sorted(aliases)) for _, aliases in sorted(grouped.items()))
    if sum(len(aliases) for aliases in chains) != len(membership_symbols):
        raise ValueError("leader identity chains do not partition membership")
    return chains


def _reporting_ticker(
    aliases: tuple[str, ...],
    when: date,
    membership: PointInTimeUniverse,
    identity_contract: LeaderIdentityContract,
) -> str:
    active = sorted(set(aliases).intersection(membership.members_at(when)))
    if len(active) > 1:
        raise ValueError(f"same-issuer aliases are simultaneously active: {active}")
    if active:
        return active[0]
    admitted = [
        identity_contract.identities[ticker]
        for ticker in aliases
        if identity_contract.identities[ticker].admitted_start
        <= when
        <= identity_contract.identities[ticker].admitted_end
    ]
    if admitted:
        admitted.sort(key=lambda identity: (-identity.admitted_start.toordinal(), identity.ticker))
        return admitted[0].ticker
    prior = [
        identity_contract.identities[ticker]
        for ticker in aliases
        if identity_contract.identities[ticker].admitted_start <= when
    ]
    if prior:
        prior.sort(key=lambda identity: (-identity.admitted_start.toordinal(), identity.ticker))
        return prior[0].ticker
    return min(aliases, key=lambda ticker: (identity_contract.identities[ticker].admitted_start, ticker))


def _chain_close_series(
    frame: pd.DataFrame,
    membership: PointInTimeUniverse,
    identity_contract: LeaderIdentityContract,
    chains: tuple[tuple[str, ...], ...],
) -> dict[tuple[str, ...], pd.Series]:
    values: dict[tuple[str, ...], list[float]] = {aliases: [] for aliases in chains}
    for timestamp in frame.index:
        when = timestamp.date()
        members = membership.members_at(when)
        for aliases in chains:
            available = [ticker for ticker in aliases if ticker in frame.columns and _valid_price(frame.at[timestamp, ticker])]
            if not available:
                values[aliases].append(float("nan"))
                continue
            active = sorted(set(available).intersection(members))
            if len(active) > 1:
                raise ValueError(f"same-issuer aliases are simultaneously active: {active}")
            if active:
                selected = active[0]
            else:
                admitted = [
                    ticker
                    for ticker in available
                    if identity_contract.identities[ticker].admitted_start
                    <= when
                    <= identity_contract.identities[ticker].admitted_end
                ]
                selected = (
                    sorted(
                        admitted,
                        key=lambda ticker: (
                            -identity_contract.identities[ticker].admitted_start.toordinal(),
                            ticker,
                        ),
                    )[0]
                    if admitted
                    else sorted(available)[0]
                )
            values[aliases].append(float(frame.at[timestamp, selected]))
    return {
        aliases: pd.Series(series, index=frame.index, dtype=float)
        for aliases, series in values.items()
    }


def _chain_first_membership_dates(
    membership: PointInTimeUniverse,
    chains: tuple[tuple[str, ...], ...],
) -> dict[tuple[str, ...], date | None]:
    first_by_ticker: dict[str, date] = {}
    for event in membership.events:
        if event.member and event.ticker not in first_by_ticker:
            first_by_ticker[event.ticker] = event.effective_date
    return {
        aliases: min(
            (first_by_ticker[ticker] for ticker in aliases if ticker in first_by_ticker),
            default=None,
        )
        for aliases in chains
    }


def label_five_year_leaders(
    closes: pd.DataFrame,
    membership: PointInTimeUniverse,
    *,
    start_date: date | str,
    end_date: date | str,
    identity_contract: LeaderIdentityContract,
    top_n: int = 100,
) -> tuple[FiveYearLeader, ...]:
    """Rank membership-union returns across the fixed window without long start backfills."""
    start = _date(start_date, field="start_date")
    end = _date(end_date, field="end_date")
    if start >= end:
        raise ValueError("five-year leader date range is invalid")
    if type(top_n) is not int or not 1 <= top_n <= _MAX_SYMBOLS:
        raise ValueError("top_n is invalid")
    frame = _leader_closes(closes).loc[pd.Timestamp(start):pd.Timestamp(end)]
    spy_sessions = frame.index[frame["SPY"].map(_valid_price)]
    if len(spy_sessions) < 756:
        raise ValueError("five-year window has fewer than 756 SPY trading sessions")
    frame = frame.reindex(spy_sessions)
    allowed_start_sessions = set(spy_sessions[:21])
    member_at_start = membership.members_at(start)
    chains = _leader_chains(membership, identity_contract)
    chain_frame = _chain_close_series(frame, membership, identity_contract, chains)
    first_membership = _chain_first_membership_dates(membership, chains)
    candidates: list[tuple[str, date, date, float, bool, date | None]] = []
    for aliases in chains:
        series = chain_frame[aliases]
        valid = series[series.map(_valid_price)]
        if len(valid) < 756 or valid.empty or valid.index[0] not in allowed_start_sessions:
            continue
        first_value = float(valid.iloc[0])
        last_value = float(valid.iloc[-1])
        total_return = (last_value / first_value - 1.0) * 100.0
        if not math.isfinite(total_return):
            continue
        ticker = _reporting_ticker(aliases, end, membership, identity_contract)
        candidates.append(
            (
                ticker,
                valid.index[0].date(),
                valid.index[-1].date(),
                total_return,
                bool(set(aliases).intersection(member_at_start)),
                first_membership[aliases],
            )
        )
    candidates.sort(key=lambda item: (-item[3], item[0]))
    return tuple(
        FiveYearLeader(
            ticker=ticker,
            first_price_date=first_date,
            last_price_date=last_date,
            total_return_pct=total_return,
            rank=rank,
            member_at_start=was_member,
            first_membership_date=first_member_date,
        )
        for rank, (ticker, first_date, last_date, total_return, was_member, first_member_date)
        in enumerate(candidates[:top_n], start=1)
    )


def label_rolling_leaders(
    closes: pd.DataFrame,
    membership: PointInTimeUniverse,
    *,
    start_date: date | str,
    end_date: date | str,
    identity_contract: LeaderIdentityContract,
    forward_trading_days: int = 252,
    top_n: int = 100,
) -> tuple[RollingLeaderObservation, ...]:
    """Rank next-year returns on each first monthly SPY session from 2021 through 2024."""
    start = _date(start_date, field="start_date")
    end = _date(end_date, field="end_date")
    if start >= end:
        raise ValueError("rolling leader date range is invalid")
    if type(forward_trading_days) is not int or not 1 <= forward_trading_days <= _MAX_FORWARD_DAYS:
        raise ValueError("forward_trading_days is invalid")
    if type(top_n) is not int or not 1 <= top_n <= _MAX_SYMBOLS:
        raise ValueError("top_n is invalid")
    frame = _leader_closes(closes).loc[:pd.Timestamp(end)]
    spy_sessions = frame.index[frame["SPY"].map(_valid_price)]
    evaluation_floor = max(start, date(2021, 1, 1))
    evaluation_ceiling = min(end, date(2024, 12, 31))
    monthly_first: dict[tuple[int, int], pd.Timestamp] = {}
    for session in spy_sessions:
        session_date = session.date()
        if evaluation_floor <= session_date <= evaluation_ceiling:
            monthly_first.setdefault((session.year, session.month), session)
    positions = {session: index for index, session in enumerate(spy_sessions)}
    chains = _leader_chains(membership, identity_contract)
    chain_frame = _chain_close_series(frame, membership, identity_contract, chains)
    observations: list[RollingLeaderObservation] = []
    for evaluation in sorted(monthly_first.values()):
        horizon_position = positions[evaluation] + forward_trading_days
        if horizon_position >= len(spy_sessions):
            continue
        horizon = spy_sessions[horizon_position]
        if horizon.date() > end:
            continue
        members_at_evaluation = membership.members_at(evaluation.date())
        members_at_horizon = membership.members_at(horizon.date())
        returns: list[tuple[str, float, tuple[str, ...]]] = []
        for aliases in chains:
            first_value = chain_frame[aliases].at[evaluation]
            last_value = chain_frame[aliases].at[horizon]
            if not _valid_price(first_value) or not _valid_price(last_value):
                continue
            forward_return = (float(last_value) / float(first_value) - 1.0) * 100.0
            if math.isfinite(forward_return):
                ticker = _reporting_ticker(
                    aliases,
                    evaluation.date(),
                    membership,
                    identity_contract,
                )
                returns.append((ticker, forward_return, aliases))
        returns.sort(key=lambda item: (-item[1], item[0]))
        observations.extend(
            RollingLeaderObservation(
                evaluation_date=evaluation.date(),
                horizon_date=horizon.date(),
                ticker=ticker,
                forward_return_pct=forward_return,
                rank=rank,
                member_at_evaluation=bool(set(aliases).intersection(members_at_evaluation)),
                member_at_horizon=bool(set(aliases).intersection(members_at_horizon)),
            )
            for rank, (ticker, forward_return, aliases) in enumerate(returns[:top_n], start=1)
        )
    return tuple(observations)


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
