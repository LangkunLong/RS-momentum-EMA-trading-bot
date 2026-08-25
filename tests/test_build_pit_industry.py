"""Focused tests for the offline PIT industry rank builder."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tools import build_pit_industry as ranker


class _FakeBundle:
    """Small bundle-shaped fixture with enough history for canonical PIT RS."""

    def __init__(self, *, future_noise: float = 0.0, include_future: bool = False) -> None:
        self.metadata = {
            "data_cutoff": "2025-12-31",
            "warmup_start": "2023-01-02",
        }
        self.sha256 = "a" * 64
        self._symbols = tuple(f"S{index:02d}" for index in range(12))
        self._sessions = pd.bdate_range("2023-01-02", periods=560)
        values: dict[str, np.ndarray] = {}
        for index, symbol in enumerate(self._symbols):
            # The first six members have a higher causal trend, making their
            # group score/rank deterministic without hard-coding RS values.
            slope = 0.0015 if index < 6 else 0.0003
            values[symbol] = 100.0 + (index * 0.5) + slope * np.arange(len(self._sessions))
        values["SPY"] = 400.0 + 0.0005 * np.arange(len(self._sessions))
        if include_future:
            target = self._sessions >= pd.Timestamp("2024-07-01")
            for index, symbol in enumerate(self._symbols):
                values[symbol][target] += future_noise * (1 if index < 6 else -1)
        self._closes = pd.DataFrame(values, index=self._sessions)

    def symbols(self) -> tuple[str, ...]:
        return (*self._symbols, "SPY")

    def members_at(self, when: object) -> frozenset[str]:
        del when
        return frozenset(self._symbols)

    def fetch_closes(self, tickers: object, start_date: object, end_date: object) -> pd.DataFrame:
        selected = [str(symbol) for symbol in tickers]
        return self._closes.loc[pd.Timestamp(start_date):pd.Timestamp(end_date), selected].copy()


def _classifications(*, dates: tuple[str, ...] = ("2024-07-01",)) -> tuple[ranker.ClassificationRow, ...]:
    rows: list[ranker.ClassificationRow] = []
    for as_of in dates:
        for index in range(12):
            rows.append(
                ranker.ClassificationRow(
                    symbol=f"S{index:02d}",
                    as_of_date=as_of,
                    group_id="LEADERS" if index < 6 else "LAGGARDS",
                    evidence_ids=(f"classification:{as_of}",),
                )
            )
    return tuple(rows)


def test_ranker_emits_supplemental_contract_rows_with_deterministic_ranks() -> None:
    rows = ranker._snapshot_rows(_FakeBundle(), _classifications())

    assert len(rows) == 12
    assert {row["group_rank"] for row in rows if row["group_id"] == "LEADERS"} == {"1"}
    assert {row["group_rank"] for row in rows if row["group_id"] == "LAGGARDS"} == {"2"}
    leader = next(row for row in rows if row["symbol"] == "S00")
    assert json.loads(leader["group_members"]) == [f"S{index:02d}" for index in range(6)]
    assert json.loads(leader["evidence_ids"]) == ["classification:2024-07-01"]
    assert tuple(rows[0]) == ranker._INDUSTRY_FIELDS


def test_ranker_passes_only_causal_history_to_existing_pit_rs(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    original = ranker.calculate_pit_rs_snapshot

    def wrapped(closes: pd.DataFrame, eval_date: pd.Timestamp, eligible_tickers: object = None):
        seen.append((eval_date, closes.index.max()))
        return original(closes, eval_date, eligible_tickers)

    monkeypatch.setattr(ranker, "calculate_pit_rs_snapshot", wrapped)
    rows = ranker._snapshot_rows(
        _FakeBundle(include_future=True, future_noise=1_000_000.0),
        _classifications(dates=("2024-07-01", "2024-07-02")),
    )

    assert rows
    assert seen
    assert all(eval_date == max_seen for eval_date, max_seen in seen)


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (
            "symbol,as_of_date,group_id,evidence_ids\nS00,2026-01-02,G,[\"source\"]\n",
            "after PIT bundle",
        ),
        (
            "symbol,as_of_date,group_id,evidence_ids\nS00,2024-07-01,G,[]\n",
            "non-empty JSON",
        ),
        (
            "symbol,as_of_date,group_id,evidence_ids\nS00,2024-07-01,G,[\"source\"]\nS00,2024-07-01,H,[\"source\"]\n",
            "ambiguous",
        ),
    ],
)
def test_classification_contract_rejects_future_missing_evidence_and_ambiguity(
    tmp_path: Path, rows: str, message: str,
) -> None:
    path = tmp_path / "classification.csv"
    path.write_text(rows, encoding="utf-8", newline="")
    with pytest.raises(ValueError, match=message):
        ranker._read_classifications(path, data_cutoff="2025-12-31")


def test_ranker_rejects_unmapped_and_incomplete_historical_membership() -> None:
    unmapped = _classifications()[:-1] + (
        ranker.ClassificationRow("UNKNOWN", "2024-07-01", "LAGGARDS", ("source",)),
    )
    with pytest.raises(ValueError, match="unmapped"):
        ranker._snapshot_rows(_FakeBundle(), unmapped)

    incomplete = _classifications()[:-1]
    with pytest.raises(ValueError, match="incomplete"):
        ranker._snapshot_rows(_FakeBundle(), incomplete)


def test_build_industry_csv_writes_exact_output_header(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    classification_path = tmp_path / "classification.csv"
    with classification_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=ranker._CLASSIFICATION_FIELDS, lineterminator="\n")
        writer.writeheader()
        for row in _classifications():
            writer.writerow(
                {
                    "symbol": row.symbol,
                    "as_of_date": row.as_of_date,
                    "group_id": row.group_id,
                    "evidence_ids": json.dumps(row.evidence_ids),
                }
            )
    bundle_path = tmp_path / "bundle.sqlite3"
    bundle_path.write_bytes(b"fixture")
    output = tmp_path / "industry.csv"

    class _Context:
        def __enter__(self):
            return _FakeBundle()

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(ranker, "PITDataBundle", lambda *_args, **_kwargs: _Context())
    result = ranker.build_industry_csv(
        pit_bundle=bundle_path,
        bundle_sha256="a" * 64,
        classification_csv=classification_path,
        output=output,
    )
    assert result.industry_rows == 12
    with output.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        assert tuple(reader.fieldnames or ()) == ranker._INDUSTRY_FIELDS
        assert len(tuple(reader)) == 12
