"""Deterministic, CSV-ready frames for the public PIT baseline outputs."""

from __future__ import annotations

from collections.abc import Sequence

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
