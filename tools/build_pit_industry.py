"""Build a dated point-in-time industry-group ranking CSV.

This utility is deliberately an offline boundary.  It accepts a validated PIT
bundle and a dated classification export, then emits the ``industry.csv``
contract consumed by :mod:`tools.build_pit_supplemental`.  It never resolves
symbols from a current provider, and it never uses a price after the
classification snapshot date.

The classification input has the exact header::

    symbol,as_of_date,group_id,evidence_ids

``as_of_date`` is the public/available date of the classification observation
and must be an exact SPY price session in the bundle.  The ranker uses the
existing PIT RS implementation over the members active on that date.  Group
scores are the arithmetic mean of member RS ratings; ties are broken by the
canonical group ID.  The output has the six columns required by
``build_pit_supplemental``.

Example::

    python -m tools.build_pit_industry \
        --pit-bundle pit.sqlite3 \
        --bundle-sha256 <sha256> \
        --classification-csv classifications.csv \
        --output industry.csv
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date
import json
import math
from pathlib import Path
import re
from typing import Iterable, Mapping

import pandas as pd

from core.pit_data import PITDataBundle
from core.pit_diagnosis.rs import calculate_pit_rs_snapshot


_CLASSIFICATION_FIELDS = ("symbol", "as_of_date", "group_id", "evidence_ids")
_INDUSTRY_FIELDS = (
    "symbol",
    "as_of_date",
    "group_id",
    "group_rank",
    "group_members",
    "evidence_ids",
)
_MAX_ROWS = 1_000_000
_INTEGER_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,7}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class ClassificationRow:
    """One dated, externally evidenced symbol-to-group classification."""

    symbol: str
    as_of_date: str
    group_id: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class IndustryBuildResult:
    """Deterministic result metadata for a generated industry CSV."""

    output: Path
    bundle_sha256: str
    classification_rows: int
    industry_rows: int
    snapshot_dates: int


def _regular_file(path: str | Path, field: str) -> Path:
    candidate = Path(path).resolve()
    if not candidate.is_file() or candidate.is_symlink():
        raise ValueError(f"{field} must be a regular, non-symlink file")
    return candidate


def _new_output(path: str | Path) -> Path:
    candidate = Path(path).resolve()
    if candidate.exists() or candidate.is_symlink():
        raise ValueError("output already exists")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate


def _iso_date(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be YYYY-MM-DD")
    return value


def _symbol(value: object, field: str = "symbol") -> str:
    if not isinstance(value, str) or value != value.strip() or _INTEGER_SYMBOL.fullmatch(value) is None:
        raise ValueError(f"{field} must be a canonical uppercase PIT symbol")
    return value


def _json_strings(value: object, field: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(
            value,
            parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(constant)),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field} must be a JSON array of strings") from exc
    if not isinstance(parsed, list) or not parsed:
        raise ValueError(f"{field} must be a non-empty JSON array")
    if any(not isinstance(item, str) or not item or item.strip() != item for item in parsed):
        raise ValueError(f"{field} must contain non-empty trimmed strings")
    if len(set(parsed)) != len(parsed):
        raise ValueError(f"{field} must not contain duplicates")
    return tuple(sorted(parsed))


def _read_classifications(
    path: Path,
    *,
    data_cutoff: str,
    max_rows: int = _MAX_ROWS,
) -> tuple[ClassificationRow, ...]:
    """Read and validate the strict, dated classification contract."""

    if type(max_rows) is not int or not 0 < max_rows <= _MAX_ROWS:
        raise ValueError(f"max_rows must be between 1 and {_MAX_ROWS}")
    rows: list[ClassificationRow] = []
    seen: set[tuple[str, str]] = set()
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            if tuple(reader.fieldnames or ()) != _CLASSIFICATION_FIELDS:
                raise ValueError(
                    "classification CSV header must be exactly "
                    + ",".join(_CLASSIFICATION_FIELDS)
                )
            for raw in reader:
                if raw.get(None) is not None or any(value is None for value in raw.values()):
                    raise ValueError(f"classification CSV row {reader.line_num} has the wrong number of fields")
                if len(rows) >= max_rows:
                    raise ValueError(f"classification CSV exceeds max_rows={max_rows}")
                symbol = _symbol(raw["symbol"])
                as_of = _iso_date(raw["as_of_date"], "classification as_of_date")
                if as_of > data_cutoff:
                    raise ValueError("classification row is after PIT bundle data_cutoff")
                key = (symbol, as_of)
                if key in seen:
                    raise ValueError(f"ambiguous or duplicate classification: {symbol} {as_of}")
                seen.add(key)
                group_id = raw["group_id"]
                if (
                    not isinstance(group_id, str)
                    or not group_id
                    or group_id.strip() != group_id
                    or any(ord(character) < 32 for character in group_id)
                ):
                    raise ValueError("classification group_id must be non-empty and trimmed")
                evidence_ids = _json_strings(raw["evidence_ids"], "classification evidence_ids")
                rows.append(ClassificationRow(symbol, as_of, group_id, evidence_ids))
    except UnicodeDecodeError as exc:
        raise ValueError("classification CSV must be UTF-8") from exc
    except csv.Error as exc:
        raise ValueError(f"classification CSV is malformed: {exc}") from exc
    if not rows:
        raise ValueError("classification CSV is empty")
    return tuple(sorted(rows, key=lambda row: (row.as_of_date, row.group_id, row.symbol)))


def _finite(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("PIT RS result is not numeric") from exc
    if not math.isfinite(number):
        raise ValueError("PIT RS result is not finite")
    return number


def _snapshot_rows(
    bundle: object,
    classifications: Iterable[ClassificationRow],
) -> tuple[dict[str, str], ...]:
    """Build output rows from a validated bundle using causal RS snapshots.

    ``bundle`` is intentionally duck-typed so the rank computation remains
    fixtureable without weakening the production ``PITDataBundle`` boundary.
    Production callers always pass a validated, hash-pinned bundle.
    """

    rows = tuple(classifications)
    if not rows:
        raise ValueError("classification rows are empty")
    cutoff = _iso_date(str(bundle.metadata["data_cutoff"]), "bundle data_cutoff")
    symbols = frozenset(str(symbol) for symbol in bundle.symbols())
    if not symbols:
        raise ValueError("PIT bundle has no symbols")
    dates = tuple(sorted({row.as_of_date for row in rows}))
    if any(row.as_of_date > cutoff for row in rows):
        raise ValueError("classification row is after PIT bundle data_cutoff")

    # Load only through the final requested snapshot.  Each individual RS
    # call receives a second, exact-session slice below, so future bars cannot
    # influence an earlier classification even if this frame is reused.
    warmup = pd.Timestamp(str(bundle.metadata["warmup_start"]))
    last_session = pd.Timestamp(dates[-1])
    all_symbols = tuple(sorted(symbols))
    closes = bundle.fetch_closes(all_symbols, warmup, last_session)
    if closes.empty:
        raise ValueError("PIT bundle has no prices through classification snapshots")
    if not isinstance(closes.index, pd.DatetimeIndex):
        closes.index = pd.to_datetime(closes.index, errors="raise").normalize()
    closes = closes.sort_index()
    if "SPY" not in closes.columns:
        raise ValueError("PIT bundle closes do not contain SPY sessions")

    by_date: dict[str, list[ClassificationRow]] = {}
    for row in rows:
        if row.symbol == "SPY":
            raise ValueError("classification must not map SPY")
        if row.symbol not in symbols:
            raise ValueError(f"unmapped classification symbol: {row.symbol}")
        by_date.setdefault(row.as_of_date, []).append(row)

    output: list[dict[str, str]] = []
    for as_of in dates:
        session = pd.Timestamp(as_of)
        # The date itself must be a completed SPY session.  No previous/next
        # session fallback is allowed because that would alter the PIT state.
        if session not in closes.index or pd.isna(closes.at[session, "SPY"]):
            raise ValueError(f"classification date is not an exact PIT price session: {as_of}")
        active = frozenset(str(symbol) for symbol in bundle.members_at(as_of))
        classified = by_date[as_of]
        classified_symbols = {row.symbol for row in classified}
        missing_classifications = sorted(active.difference(classified_symbols))
        if missing_classifications:
            raise ValueError(
                f"classification snapshot is incomplete on {as_of}; "
                f"missing active PIT symbols: {missing_classifications[:5]}"
            )
        for row in classified:
            if row.symbol not in active:
                raise ValueError(f"classification symbol is not a PIT member on {as_of}: {row.symbol}")

        grouped: dict[str, list[str]] = {}
        for row in classified:
            grouped.setdefault(row.group_id, []).append(row.symbol)
        group_members = {
            group_id: tuple(sorted(set(group_symbols)))
            for group_id, group_symbols in grouped.items()
        }
        if any(len(symbols_for_group) == 0 for symbols_for_group in group_members.values()):
            raise ValueError(f"industry group has no members on {as_of}")

        # ``calculate_pit_rs_snapshot`` is the repository's canonical PIT RS
        # implementation.  Passing only history through this exact session
        # makes the no-lookahead invariant explicit at this boundary.
        causal_closes = closes.loc[closes.index <= session]
        if causal_closes.empty or causal_closes.index[-1] != session:
            raise ValueError(f"classification date has no exact PIT close: {as_of}")
        rs_snapshot = calculate_pit_rs_snapshot(causal_closes, session, eligible_tickers=active)
        group_scores: dict[str, float] = {}
        for group_id, members in group_members.items():
            missing = [symbol for symbol in members if symbol not in rs_snapshot]
            if missing:
                raise ValueError(
                    f"industry group cannot be ranked without causal RS for {as_of} "
                    f"{group_id}: {missing[:5]}"
                )
            values = tuple(_finite(rs_snapshot[symbol]) for symbol in members)
            group_scores[group_id] = sum(values) / len(values)
        ranked_groups = sorted(group_scores, key=lambda group: (-group_scores[group], group))
        ranks = {group_id: rank for rank, group_id in enumerate(ranked_groups, start=1)}

        for row in classified:
            output.append(
                {
                    "symbol": row.symbol,
                    "as_of_date": row.as_of_date,
                    "group_id": row.group_id,
                    "group_rank": str(ranks[row.group_id]),
                    "group_members": json.dumps(group_members[row.group_id], separators=(",", ":")),
                    "evidence_ids": json.dumps(row.evidence_ids, separators=(",", ":")),
                }
            )
    return tuple(sorted(output, key=lambda row: (row["symbol"], row["as_of_date"])))


def _write_output(path: Path, rows: Iterable[Mapping[str, str]]) -> int:
    output_rows = tuple(rows)
    if not output_rows:
        raise ValueError("industry output is empty")
    partial = Path(f"{path}.partial")
    if partial.exists() or partial.is_symlink():
        raise ValueError("partial industry output already exists")
    try:
        with partial.open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=_INDUSTRY_FIELDS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(output_rows)
        partial.replace(path)
        return len(output_rows)
    except Exception:
        partial.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        raise


def build_industry_csv(
    *,
    pit_bundle: str | Path,
    bundle_sha256: str,
    classification_csv: str | Path,
    output: str | Path,
    max_rows: int = _MAX_ROWS,
) -> IndustryBuildResult:
    """Build a strict supplemental ``industry.csv`` from PIT-only inputs."""

    if not isinstance(bundle_sha256, str) or _DIGEST.fullmatch(bundle_sha256) is None:
        raise ValueError("bundle_sha256 must be a lowercase SHA-256")
    bundle_path = _regular_file(pit_bundle, "pit_bundle")
    classification_path = _regular_file(classification_csv, "classification_csv")
    output_path = _new_output(output)
    # A tiny metadata read is not sufficient: PITDataBundle validates the
    # whole hash-bound membership/price/fundamental contract before use.
    try:
        with PITDataBundle(bundle_path, expected_sha256=bundle_sha256) as bundle:
            classifications = _read_classifications(
                classification_path,
                data_cutoff=str(bundle.metadata["data_cutoff"]),
                max_rows=max_rows,
            )
            rows = _snapshot_rows(bundle, classifications)
            count = _write_output(output_path, rows)
            return IndustryBuildResult(
                output=output_path,
                bundle_sha256=bundle.sha256,
                classification_rows=len(classifications),
                industry_rows=count,
                snapshot_dates=len({row.as_of_date for row in classifications}),
            )
    except Exception:
        output_path.unlink(missing_ok=True)
        raise


def _digest(value: str) -> str:
    if _DIGEST.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("expected a lowercase SHA-256")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pit-bundle", type=Path, required=True)
    parser.add_argument("--bundle-sha256", type=_digest, required=True)
    parser.add_argument("--classification-csv", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-rows", type=int, default=_MAX_ROWS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = build_industry_csv(
        pit_bundle=args.pit_bundle,
        bundle_sha256=args.bundle_sha256,
        classification_csv=args.classification_csv,
        output=args.output,
        max_rows=args.max_rows,
    )
    print(
        json.dumps(
            {
                "bundle_sha256": result.bundle_sha256,
                "classification_rows": result.classification_rows,
                "industry_rows": result.industry_rows,
                "output": str(result.output),
                "snapshot_dates": result.snapshot_dates,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
