"""Bridge V3 lineage membership to the ticker membership consumed by SEC export."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path, PurePath
from typing import Callable, Iterable, Mapping, Sequence

from core.pit_provenance import (
    pit_canonical_json,
    pit_canonical_json_bytes,
    pit_canonical_json_sha256,
)


_LINEAGE_RE = re.compile(r"[a-z][a-z0-9_]{0,47}\Z")
_UNIVERSES = frozenset({"sp500", "nasdaq100", "russell2000"})
_V3_MEMBERSHIP_COLUMNS = (
    "effective_date",
    "security_lineage_id",
    "universe_id",
    "member",
)
_TICKER_MEMBERSHIP_COLUMNS = ("effective_date", "ticker", "member")
_PROJECTION_LEDGER_COLUMNS = (
    "effective_date",
    "ticker",
    "member",
    "security_lineage_id",
    "projection_reason",
    "source_membership_event_keys",
    "identity_boundary_id",
)
_EXTRACTION_HISTORY_COLUMNS = (
    "ticker",
    "first_extraction_date",
    "last_extraction_date",
    "security_lineage_id",
    "identity_segment_id",
)
_BRIDGE_KIND = "financial_lineage_membership_projection_v1"
_EXPORT_BRIDGE_KIND = "sec_fundamentals_lineage_projection_v1"
_DEFAULT_MEMBERSHIP_START = "2021-01-01"


@dataclass(frozen=True, slots=True)
class IdentityBoundary:
    """An authenticated date at which one lineage changes its ticker."""

    effective_date: date
    lineage_id: str
    predecessor_ticker: str
    successor_ticker: str
    boundary_id: str

    def __post_init__(self) -> None:
        if type(self.effective_date) is not date:
            raise ValueError("identity boundary date is invalid")
        if _LINEAGE_RE.fullmatch(self.lineage_id) is None:
            raise ValueError("identity boundary lineage is invalid")
        if not self.predecessor_ticker or not self.successor_ticker:
            raise ValueError("identity boundary tickers are required")
        if self.predecessor_ticker == self.successor_ticker:
            raise ValueError("identity boundary tickers must differ")
        if not self.boundary_id:
            raise ValueError("identity boundary id is required")


@dataclass(frozen=True, slots=True)
class MembershipProjection:
    """Ticker membership events and their lineage-level derivation ledger."""

    ticker_events: tuple[tuple[str, str, int], ...]
    ledger_rows: tuple[Mapping[str, str], ...]


def _clip_projection_to_membership_window(
    projection: MembershipProjection,
    source_membership: Sequence[tuple[str, str, str, int]],
    *,
    start_date: str,
    end_date: str,
) -> MembershipProjection:
    """Retain true membership during the SEC baseline, seeding active state once."""
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError as exc:
        raise ValueError("membership extraction date bounds are invalid") from exc
    if start.isoformat() != start_date or end.isoformat() != end_date or end < start:
        raise ValueError("membership extraction date bounds are invalid")
    ledger_by_key = {
        (row["effective_date"], row["ticker"], int(row["member"])): row
        for row in projection.ledger_rows
    }
    active_before: dict[str, str] = {}
    start_rows: list[tuple[str, str, int]] = []
    for event in projection.ticker_events:
        when, ticker, member = event
        if when < start_date:
            if member:
                if ticker in active_before:
                    raise ValueError("projected ticker membership has duplicate pre-window additions")
                active_before[ticker] = ledger_by_key[(when, ticker, member)]["security_lineage_id"]
            else:
                if ticker not in active_before:
                    raise ValueError("projected ticker membership has unmatched pre-window removal")
                del active_before[ticker]
        elif when == start_date:
            start_rows.append(event)

    active_at_start = dict(active_before)
    changed_at_start = {ticker for _when, ticker, _member in start_rows}
    for when, ticker, member in start_rows:
        if member:
            if ticker in active_at_start:
                raise ValueError("projected ticker membership has duplicate baseline additions")
            active_at_start[ticker] = ledger_by_key[(when, ticker, member)]["security_lineage_id"]
        else:
            if ticker not in active_at_start:
                raise ValueError("projected ticker membership has unmatched baseline removal")
            del active_at_start[ticker]

    output_events: list[tuple[str, str, int]] = []
    output_ledger: list[Mapping[str, str]] = []
    active_output: dict[str, str] = {}
    source_keys_by_lineage: dict[str, list[str]] = {}
    for effective, lineage, universe, member in source_membership:
        if effective < start_date:
            source_keys_by_lineage.setdefault(lineage, []).append(
                f"{effective}|{lineage}|{universe}|{member}"
            )
    for ticker, lineage in sorted(active_at_start.items()):
        if ticker in changed_at_start:
            continue
        source_keys = "|".join(sorted(source_keys_by_lineage.get(lineage, ())))
        if not source_keys:
            raise ValueError("membership window seed has no earlier V3 source events")
        output_events.append((start_date, ticker, 1))
        output_ledger.append(
            {
                "effective_date": start_date,
                "ticker": ticker,
                "member": "1",
                "security_lineage_id": lineage,
                "projection_reason": "membership_window_seed",
                "source_membership_event_keys": source_keys,
                "identity_boundary_id": f"membership-window:{start_date}:{lineage}",
            }
        )
        active_output[ticker] = lineage

    for effective, ticker, member in projection.ticker_events:
        if not start_date <= effective <= end_date:
            continue
        ledger = ledger_by_key[(effective, ticker, member)]
        lineage = ledger["security_lineage_id"]
        if member:
            if ticker in active_output:
                raise ValueError("projected ticker membership adds an already-active baseline ticker")
            active_output[ticker] = lineage
        else:
            if active_output.get(ticker) != lineage:
                # A predecessor removed on the first baseline date had no
                # membership inside the SEC window, so omit that close event.
                if effective == start_date and ticker not in active_output:
                    continue
                raise ValueError("projected ticker membership removal has no baseline episode")
            del active_output[ticker]
        output_events.append((effective, ticker, member))
        output_ledger.append(ledger)

    ordered = sorted(
        zip(output_events, output_ledger, strict=True),
        key=lambda pair: pair[0],
    )
    if not ordered:
        raise ValueError("SEC membership window has no ticker episodes")
    return MembershipProjection(
        tuple(event for event, _ledger in ordered),
        tuple(ledger for _event, ledger in ordered),
    )


def project_v3_membership_to_ticker(
    membership: Iterable[tuple[str, str, str, int]],
    *,
    resolve_ticker_for_lineage: Callable[[str, str], str],
    identity_boundaries: Sequence[IdentityBoundary] = (),
) -> MembershipProjection:
    """Project lineage union state into deterministic legacy ticker events.

    Index overlaps are collapsed to union membership. Authenticated identity
    boundaries add a ticker removal/addition only when the lineage is a union
    member immediately before and after the boundary. Issuer history for SEC
    extraction is carried separately; this function never rewrites facts.
    """

    events_by_date: dict[str, list[tuple[str, str, int]]] = {}
    seen: set[tuple[str, str, str]] = set()
    previous: tuple[str, str, str] | None = None
    for raw in membership:
        if not isinstance(raw, tuple) or len(raw) != 4:
            raise ValueError("V3 membership event shape is invalid")
        effective, lineage, universe, member = raw
        try:
            parsed_date = date.fromisoformat(effective)
        except (TypeError, ValueError) as exc:
            raise ValueError("V3 membership effective date is invalid") from exc
        if parsed_date.isoformat() != effective:
            raise ValueError("V3 membership effective date is not canonical")
        if not isinstance(lineage, str) or _LINEAGE_RE.fullmatch(lineage) is None:
            raise ValueError("V3 membership lineage is invalid")
        if universe not in _UNIVERSES:
            raise ValueError("V3 membership universe is invalid")
        if type(member) is not int or member not in (0, 1):
            raise ValueError("V3 membership state is invalid")
        key = (effective, lineage, universe)
        if key in seen:
            raise ValueError("duplicate V3 membership transition")
        if previous is not None and key <= previous:
            raise ValueError("V3 membership events must be canonical-sorted")
        seen.add(key)
        previous = key
        events_by_date.setdefault(effective, []).append(raw)

    boundaries_by_date: dict[str, dict[str, IdentityBoundary]] = {}
    for boundary in identity_boundaries:
        if type(boundary) is not IdentityBoundary:
            raise ValueError("identity boundary has the wrong type")
        effective = boundary.effective_date.isoformat()
        by_lineage = boundaries_by_date.setdefault(effective, {})
        if boundary.lineage_id in by_lineage:
            raise ValueError("identity boundary is ambiguous for a lineage and date")
        by_lineage[boundary.lineage_id] = boundary

    state: dict[str, set[str]] = {}
    projected: list[tuple[str, str, int, str, str, str, str]] = []
    for effective in sorted(set(events_by_date).union(boundaries_by_date)):
        rows = events_by_date.get(effective, [])
        changed_lineages = {row[1] for row in rows}
        boundary_lineages = set(boundaries_by_date.get(effective, {}))
        affected = sorted(changed_lineages | boundary_lineages)
        before_union = {
            lineage: bool(state.get(lineage)) for lineage in affected
        }
        event_keys: dict[str, list[str]] = {lineage: [] for lineage in affected}
        for _, lineage, universe, member in rows:
            active = state.setdefault(lineage, set())
            if member and universe in active:
                raise ValueError("V3 membership addition is already active")
            if not member and universe not in active:
                raise ValueError("V3 membership removal has no active affiliation")
            active.add(universe) if member else active.remove(universe)
            event_keys[lineage].append(
                f"{effective}|{lineage}|{universe}|{member}"
            )

        for lineage in affected:
            before = before_union[lineage]
            after = bool(state.get(lineage))
            boundary = boundaries_by_date.get(effective, {}).get(lineage)
            if boundary is not None:
                if before:
                    projected.append(
                        (
                            effective,
                            boundary.predecessor_ticker,
                            0,
                            lineage,
                            "identity_boundary_exit",
                            "|".join(event_keys[lineage]),
                            boundary.boundary_id,
                        )
                    )
                if after:
                    projected.append(
                        (
                            effective,
                            boundary.successor_ticker,
                            1,
                            lineage,
                            "identity_boundary_entry",
                            "|".join(event_keys[lineage]),
                            boundary.boundary_id,
                        )
                    )
            elif before != after:
                ticker = resolve_ticker_for_lineage(lineage, effective)
                if not isinstance(ticker, str) or not ticker:
                    raise ValueError("lineage has no unique active ticker")
                projected.append(
                    (
                        effective,
                        ticker,
                        int(after),
                        lineage,
                        "union_membership_add" if after else "union_membership_remove",
                        "|".join(event_keys[lineage]),
                        "",
                    )
                )

    projected.sort(key=lambda row: (row[0], row[1], row[2]))
    ticker_events: list[tuple[str, str, int]] = []
    ledger_rows: list[Mapping[str, str]] = []
    active_ticker_lineages: dict[str, str] = {}
    event_keys_seen: set[tuple[str, str]] = set()
    for effective, ticker, member, lineage, reason, source_keys, boundary_id in projected:
        event_key = (effective, ticker)
        if event_key in event_keys_seen:
            raise ValueError("ticker identity is ambiguous for a membership date")
        event_keys_seen.add(event_key)
        if member:
            if ticker in active_ticker_lineages:
                raise ValueError("ticker identity is ambiguous across lineages")
            active_ticker_lineages[ticker] = lineage
        else:
            if active_ticker_lineages.get(ticker) != lineage:
                raise ValueError("ticker membership removal does not match its lineage")
            del active_ticker_lineages[ticker]
        ticker_events.append((effective, ticker, member))
        ledger_rows.append(
            {
                "effective_date": effective,
                "ticker": ticker,
                "member": str(member),
                "security_lineage_id": lineage,
                "projection_reason": reason,
                "source_membership_event_keys": source_keys,
                "identity_boundary_id": boundary_id,
            }
        )

    return MembershipProjection(tuple(ticker_events), tuple(ledger_rows))


def identity_boundaries_from_contract(
    transitions: Sequence[Mapping[str, object]], segment_contract: object | None
) -> tuple[IdentityBoundary, ...]:
    """Extract rename boundaries from the validated destination identity contract."""

    result: list[IdentityBoundary] = []
    segmented_lineages: set[str] = set()
    if segment_contract is not None:
        segments = getattr(segment_contract, "segments", None)
        segment_transitions = getattr(segment_contract, "segment_transitions", None)
        if not isinstance(segments, Mapping) or not isinstance(segment_transitions, tuple):
            raise ValueError("price identity segment contract is invalid")
        for item in segment_transitions:
            predecessor = segments.get(item.predecessor_segment_id)
            successor = segments.get(item.successor_segment_id)
            if predecessor is None or successor is None:
                raise ValueError("price identity segment transition is incomplete")
            lineage = str(item.chain_id)
            segmented_lineages.add(lineage)
            result.append(
                IdentityBoundary(
                    effective_date=item.effective_date,
                    lineage_id=lineage,
                    predecessor_ticker=str(predecessor.provider_symbol),
                    successor_ticker=str(successor.provider_symbol),
                    boundary_id=(
                        f"segment:{item.predecessor_segment_id}->"
                        f"{item.successor_segment_id}"
                    ),
                )
            )
    for item in transitions:
        lineage = str(item.get("chain_id", ""))
        if lineage in segmented_lineages:
            raise ValueError("identity lineage has both legacy and segmented transitions")
        try:
            effective = date.fromisoformat(str(item["effective_date"]))
        except (KeyError, ValueError) as exc:
            raise ValueError("price identity transition date is invalid") from exc
        result.append(
            IdentityBoundary(
                effective_date=effective,
                lineage_id=lineage,
                predecessor_ticker=str(item.get("predecessor", "")),
                successor_ticker=str(item.get("successor", "")),
                boundary_id=(
                    f"legacy:{effective.isoformat()}:{item.get('predecessor')}->"
                    f"{item.get('successor')}"
                ),
            )
        )
    return tuple(
        sorted(
            result,
            key=lambda item: (
                item.effective_date,
                item.lineage_id,
                item.predecessor_ticker,
                item.successor_ticker,
            ),
        )
    )


def _ticker_for_lineage(
    lineage: str,
    as_of: str,
    identities: Mapping[str, Mapping[str, object]],
    transitions: Sequence[Mapping[str, object]],
    segment_contract: object | None,
) -> str:
    if segment_contract is not None and segment_contract.has_segmented_chain(lineage):
        return segment_contract.resolve_ticker_for_lineage(lineage, as_of)
    active: list[str] = []
    for ticker, identity in identities.items():
        if identity.get("chain_id") != lineage:
            continue
        if not (
            str(identity["admitted_start"]) <= as_of <= str(identity["admitted_end"])
        ):
            continue
        is_successor = any(
            str(item["chain_id"]) == lineage
            and str(item["successor"]) == ticker
            and as_of >= str(item["effective_date"])
            for item in transitions
        )
        is_predecessor = any(
            str(item["chain_id"]) == lineage
            and str(item["predecessor"]) == ticker
            and as_of >= str(item["effective_date"])
            for item in transitions
        )
        if is_successor or not is_predecessor:
            active.append(ticker)
    if len(active) != 1:
        raise ValueError(f"lineage has ambiguous price identity on {as_of}: {lineage}")
    return active[0]


def _identity_extraction_history_rows(
    membership: Sequence[tuple[str, str, str, int]],
    *,
    identities: Mapping[str, Mapping[str, object]],
    transitions: Sequence[Mapping[str, object]],
    segment_contract: object | None,
    start_date: str,
    end_date: str,
) -> tuple[tuple[str, str, str, str, str], ...]:
    """Return issuer-history extraction windows, kept separate from membership."""
    try:
        first = date.fromisoformat(start_date)
        last = date.fromisoformat(end_date)
    except ValueError as exc:
        raise ValueError("financial extraction date bounds are invalid") from exc
    if first.isoformat() != start_date or last.isoformat() != end_date or last < first:
        raise ValueError("financial extraction date bounds are invalid")
    relevant_lineages = {row[1] for row in membership}
    candidates: list[tuple[str, date, date, str, str]] = []
    if segment_contract is not None:
        segments = getattr(segment_contract, "segments", None)
        if not isinstance(segments, Mapping):
            raise ValueError("price identity segment contract is invalid")
        for segment_id, segment in segments.items():
            lineage = str(getattr(segment, "chain_id", ""))
            if lineage not in relevant_lineages:
                continue
            ticker = str(getattr(segment, "provider_symbol", ""))
            segment_start = getattr(segment, "admitted_start", None)
            segment_end = getattr(segment, "admitted_end", None)
            if type(segment_start) is not date or type(segment_end) is not date:
                raise ValueError("price identity segment dates are invalid")
            clipped_start = max(first, segment_start)
            clipped_end = min(last, segment_end)
            if clipped_start <= clipped_end:
                candidates.append((ticker, clipped_start, clipped_end, lineage, str(segment_id)))
    else:
        chain_by_ticker = {
            ticker: str(identity.get("chain_id", ""))
            for ticker, identity in identities.items()
        }
        bounds: dict[str, list[date]] = {}
        for ticker, identity in identities.items():
            lineage = chain_by_ticker[ticker]
            if lineage not in relevant_lineages:
                continue
            try:
                segment_start = date.fromisoformat(str(identity["admitted_start"]))
                segment_end = date.fromisoformat(str(identity["admitted_end"]))
            except (KeyError, ValueError) as exc:
                raise ValueError("price identity extraction bounds are invalid") from exc
            clipped_start = max(first, segment_start)
            clipped_end = min(last, segment_end)
            if clipped_start <= clipped_end:
                bounds[ticker] = [clipped_start, clipped_end]
        for transition in transitions:
            lineage = str(transition.get("chain_id", ""))
            predecessor = str(transition.get("predecessor", ""))
            successor = str(transition.get("successor", ""))
            try:
                effective = date.fromisoformat(str(transition["effective_date"]))
            except (KeyError, ValueError) as exc:
                raise ValueError("price identity extraction transition is invalid") from exc
            if lineage not in relevant_lineages:
                continue
            if predecessor in bounds:
                bounds[predecessor][1] = min(bounds[predecessor][1], effective - timedelta(days=1))
            if successor in bounds:
                bounds[successor][0] = max(bounds[successor][0], effective)
        for ticker, (segment_start, segment_end) in bounds.items():
            if segment_start <= segment_end:
                candidates.append(
                    (ticker, segment_start, segment_end, chain_by_ticker[ticker], "")
                )
    candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3], item[4]))
    previous_by_ticker: dict[str, date] = {}
    rows: list[tuple[str, str, str, str, str]] = []
    for ticker, first_date, last_date, lineage, segment_id in candidates:
        if not ticker or not lineage or first_date > last_date:
            continue
        previous_last = previous_by_ticker.get(ticker)
        if previous_last is not None and first_date <= previous_last:
            raise ValueError("financial identity extraction windows overlap for a ticker")
        previous_by_ticker[ticker] = last_date
        rows.append(
            (
                ticker,
                first_date.isoformat(),
                last_date.isoformat(),
                lineage,
                segment_id,
            )
        )
    if not rows:
        raise ValueError("V3 membership has no identity history for SEC extraction")
    return tuple(rows)


def _csv_bytes(fieldnames: Sequence[str], rows: Iterable[Sequence[str]]) -> bytes:
    from io import StringIO

    buffer = StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(fieldnames)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(path: str | Path, label: str) -> Path:
    value = Path(os.path.abspath(path))
    info = value.lstat()
    resolved = value.resolve(strict=True)
    if not value.is_file() or value.is_symlink() or resolved != value:
        raise ValueError(f"{label} must be a regular non-link file")
    if info.st_size <= 0:
        raise ValueError(f"{label} is empty")
    return resolved


def _json_mapping(path: Path, label: str) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain an object")
    return value


def _read_csv(
    path: Path,
    columns: Sequence[str],
    label: str,
    *,
    allow_empty: bool = False,
) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != tuple(columns):
                raise ValueError(f"{label} columns are invalid")
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValueError(f"{label} is invalid CSV") from exc
    if (not rows and not allow_empty) or any(None in row for row in rows):
        raise ValueError(f"{label} is empty or malformed")
    return rows


def _relative_reference(base: Path, path: Path) -> str:
    try:
        return os.path.relpath(path.resolve(strict=False), base.resolve(strict=False))
    except (OSError, ValueError) as exc:
        raise ValueError("lineage bridge input path cannot be retained") from exc


def _resolve_reference(base: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{label} reference is invalid")
    candidate = Path(value)
    if candidate.is_absolute() or PurePath(value).is_absolute():
        raise ValueError(f"{label} reference must be relative")
    if re.match(r"^[A-Za-z]:", value):
        raise ValueError(f"{label} reference must be relative")
    current = base.resolve(strict=True)
    for part in candidate.parts:
        if part in ("", "."):
            continue
        current = current / part
        if current.is_symlink():
            raise ValueError(f"{label} reference cannot contain symlinks")
    try:
        resolved = (base / candidate).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{label} reference is unavailable") from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise ValueError(f"{label} reference must be a regular file")
    return resolved


def _read_v3_membership(path: Path) -> list[tuple[str, str, str, int]]:
    rows = _read_csv(path, _V3_MEMBERSHIP_COLUMNS, "schema-V3 membership CSV")
    result: list[tuple[str, str, str, int]] = []
    for row in rows:
        if row["member"] not in {"0", "1"}:
            raise ValueError("schema-V3 membership member must be canonical 0 or 1")
        result.append(
            (
                row["effective_date"],
                row["security_lineage_id"],
                row["universe_id"],
                int(row["member"]),
            )
        )
    return result


def _projected_ticker_intervals(
    path: Path, *, start_date: str, end_date: str
) -> Counter[tuple[str, str, str]]:
    try:
        first = date.fromisoformat(start_date)
        last = date.fromisoformat(end_date)
    except ValueError as exc:
        raise ValueError("export membership date bounds are invalid") from exc
    if first.isoformat() != start_date or last.isoformat() != end_date or last < first:
        raise ValueError("export membership date bounds are invalid")
    rows = _read_csv(path, _TICKER_MEMBERSHIP_COLUMNS, "ticker membership CSV")
    active: dict[str, date] = {}
    result: Counter[tuple[str, str, str]] = Counter()
    previous: tuple[str, str] | None = None
    for row in rows:
        effective_text = row["effective_date"]
        try:
            effective = date.fromisoformat(effective_text)
        except ValueError as exc:
            raise ValueError("ticker membership effective date is invalid") from exc
        if effective.isoformat() != effective_text or not first <= effective <= last:
            raise ValueError("ticker membership event is outside the SEC export window")
        ticker = row["ticker"]
        if not ticker or ticker != ticker.upper() or row["member"] not in {"0", "1"}:
            raise ValueError("ticker membership event is not canonical")
        key = (effective_text, ticker)
        if previous is not None and key <= previous:
            raise ValueError("ticker membership events are not canonical-sorted")
        previous = key
        member = int(row["member"])
        if member:
            if ticker in active:
                raise ValueError("ticker membership addition is already active")
            active[ticker] = effective
        else:
            first_member = active.pop(ticker, None)
            if first_member is None:
                raise ValueError("ticker membership removal has no active episode")
            interval_end = effective - timedelta(days=1)
            if interval_end < first_member:
                raise ValueError("ticker membership episode is empty")
            result[(ticker, first_member.isoformat(), interval_end.isoformat())] += 1
    for ticker, first_member in active.items():
        result[(ticker, first_member.isoformat(), last.isoformat())] += 1
    if not result:
        raise ValueError("ticker membership has no SEC export episodes")
    return result


def _publish_no_clobber(outputs: Sequence[tuple[Path, bytes]]) -> None:
    staged: list[tuple[Path, Path]] = []
    created: list[Path] = []
    try:
        for target, payload in outputs:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                raise ValueError(f"refusing to overwrite lineage bridge output: {target}")
            temporary = target.with_name(f".{target.name}.{os.urandom(8).hex()}.tmp")
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            staged.append((temporary, target))
        for temporary, target in staged:
            try:
                os.link(temporary, target)
            except FileExistsError as exc:
                raise ValueError(f"refusing to overwrite lineage bridge output: {target}") from exc
            created.append(target)
    except Exception:
        for target in reversed(created):
            try:
                target.unlink()
            except OSError:
                pass
        raise
    finally:
        for temporary, _ in staged:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def build_membership_projection(
    *,
    membership_csv: str | Path,
    membership_provenance: str | Path,
    prices_provenance: str | Path,
    output_membership_csv: str | Path,
    output_projection_ledger_csv: str | Path,
    output_extraction_history_csv: str | Path,
    output_projection_provenance: str | Path,
    membership_start_date: str = _DEFAULT_MEMBERSHIP_START,
) -> Mapping[str, object]:
    """Create retained ticker-membership inputs from authenticated V3 inputs."""

    source_membership = _regular_file(membership_csv, "schema-V3 membership CSV")
    source_membership_provenance = _regular_file(
        membership_provenance, "schema-V3 membership provenance"
    )
    source_prices_provenance = _regular_file(prices_provenance, "prices provenance")
    output_csv = Path(output_membership_csv).absolute()
    output_ledger = Path(output_projection_ledger_csv).absolute()
    output_history = Path(output_extraction_history_csv).absolute()
    output_provenance = Path(output_projection_provenance).absolute()
    inputs = {
        source_membership,
        source_membership_provenance,
        source_prices_provenance,
    }
    initial_hashes = {path: sha256_file(path) for path in inputs}
    outputs = {output_csv, output_ledger, output_history, output_provenance}
    if len(outputs) != 4 or inputs.intersection(
        outputs
    ):
        raise ValueError("lineage bridge outputs must be distinct from all inputs")

    membership_manifest = _json_mapping(
        source_membership_provenance, "schema-V3 membership provenance"
    )
    prices_manifest = _json_mapping(source_prices_provenance, "prices provenance")
    if source_membership_provenance.read_bytes() != pit_canonical_json_bytes(
        membership_manifest
    ):
        raise ValueError("schema-V3 membership provenance is not canonical JSON")
    membership = _read_v3_membership(source_membership)
    membership_sha = sha256_file(source_membership)
    prices_sha = sha256_file(source_prices_provenance)
    if (
        membership_manifest.get("schema_version") != 3
        or membership_manifest.get("kind") != "pit_universe_membership_v3"
        or membership_manifest.get("membership_sha256") != membership_sha
        or membership_manifest.get("event_count") != len(membership)
        or membership_manifest.get("prices_provenance_sha256") != prices_sha
    ):
        raise ValueError("schema-V3 membership provenance does not bind its inputs")

    from normalize_pit_universe_membership import _load_price_identity

    identities, transitions, identity_digest, transition_digest, segment_contract = (
        _load_price_identity(
            prices_manifest,
            source_root=source_prices_provenance.parent,
            prices_provenance_sha256=prices_sha,
        )
    )
    segment_digest = (
        segment_contract.segment_contract_sha256 if segment_contract is not None else None
    )
    if (
        membership_manifest.get("price_identity_request_contracts_sha256")
        != identity_digest
        or membership_manifest.get("price_identity_transitions_sha256")
        != transition_digest
        or membership_manifest.get("price_identity_segments_v1_sha256")
        != segment_digest
    ):
        raise ValueError("schema-V3 membership provenance has foreign identity metadata")
    boundaries = identity_boundaries_from_contract(transitions, segment_contract)
    projection = project_v3_membership_to_ticker(
        membership,
        resolve_ticker_for_lineage=lambda lineage, as_of: _ticker_for_lineage(
            lineage, as_of, identities, transitions, segment_contract
        ),
        identity_boundaries=boundaries,
    )
    membership_end_date = str(prices_manifest.get("end_date", ""))
    projection = _clip_projection_to_membership_window(
        projection,
        membership,
        start_date=membership_start_date,
        end_date=membership_end_date,
    )
    ticker_payload = _csv_bytes(
        _TICKER_MEMBERSHIP_COLUMNS, projection.ticker_events
    )
    ledger_payload = _csv_bytes(
        _PROJECTION_LEDGER_COLUMNS,
        (
            tuple(row[column] for column in _PROJECTION_LEDGER_COLUMNS)
            for row in projection.ledger_rows
        ),
    )
    history_rows = _identity_extraction_history_rows(
        membership,
        identities=identities,
        transitions=transitions,
        segment_contract=segment_contract,
        start_date=str(prices_manifest.get("start_date", "")),
        end_date=str(prices_manifest.get("end_date", "")),
    )
    history_payload = _csv_bytes(_EXTRACTION_HISTORY_COLUMNS, history_rows)
    ticker_sha = _sha256_bytes(ticker_payload)
    ledger_sha = _sha256_bytes(ledger_payload)
    history_sha = _sha256_bytes(history_payload)
    refs = {
        "lineage_projection_csv": _relative_reference(
            output_provenance.parent, output_ledger
        ),
        "lineage_extraction_history_csv": _relative_reference(
            output_provenance.parent, output_history
        ),
        "source_prices_provenance": _relative_reference(
            output_provenance.parent, source_prices_provenance
        ),
        "source_v3_membership_csv": _relative_reference(
            output_provenance.parent, source_membership
        ),
        "source_v3_membership_provenance": _relative_reference(
            output_provenance.parent, source_membership_provenance
        ),
        "ticker_membership_csv": _relative_reference(output_provenance.parent, output_csv),
    }
    provenance: dict[str, object] = {
        "extraction_end_date": str(prices_manifest["end_date"]),
        "extraction_history_row_count": len(history_rows),
        "extraction_history_sha256": history_sha,
        "extraction_start_date": str(prices_manifest["start_date"]),
        "identity_request_contracts_sha256": identity_digest,
        "identity_segments_v1_sha256": segment_digest,
        "identity_transitions_sha256": transition_digest,
        "kind": _BRIDGE_KIND,
        "membership_window_end_date": membership_end_date,
        "membership_window_start_date": membership_start_date,
        "projection_ledger_row_count": len(projection.ledger_rows),
        "projection_ledger_sha256": ledger_sha,
        "references": refs,
        "schema_version": 1,
        "source_prices_provenance_sha256": prices_sha,
        "source_v3_membership_provenance_sha256": sha256_file(
            source_membership_provenance
        ),
        "source_v3_membership_sha256": membership_sha,
        "ticker_membership_event_count": len(projection.ticker_events),
        "ticker_membership_sha256": ticker_sha,
    }
    provenance_payload = pit_canonical_json_bytes(provenance)
    if initial_hashes != {path: sha256_file(path) for path in inputs}:
        raise ValueError("lineage bridge input changed during projection")
    _publish_no_clobber(
        (
            (output_csv, ticker_payload),
            (output_ledger, ledger_payload),
            (output_history, history_payload),
            (output_provenance, provenance_payload),
        )
    )
    return provenance


def _validate_projection_provenance(
    *,
    provenance_path: Path,
    expected_sha256: str,
    source_membership_csv: Path,
    source_membership_provenance: Path,
    source_prices_provenance: Path,
    identities: Mapping[str, Mapping[str, object]],
    transitions: Sequence[Mapping[str, object]],
    segment_contract: object | None,
    membership: Sequence[tuple[str, str, str, int]],
) -> tuple[Path, Path, Path]:
    if sha256_file(provenance_path) != expected_sha256:
        raise ValueError("financial lineage bridge projection provenance hash is invalid")
    document = _json_mapping(provenance_path, "financial lineage bridge projection")
    if provenance_path.read_bytes() != pit_canonical_json_bytes(document):
        raise ValueError("financial lineage bridge projection is not canonical JSON")
    if document.get("schema_version") != 1 or document.get("kind") != _BRIDGE_KIND:
        raise ValueError("financial lineage bridge projection schema is unsupported")
    references = document.get("references")
    if not isinstance(references, dict) or set(references) != {
        "lineage_extraction_history_csv",
        "lineage_projection_csv",
        "source_prices_provenance",
        "source_v3_membership_csv",
        "source_v3_membership_provenance",
        "ticker_membership_csv",
    }:
        raise ValueError("financial lineage bridge projection references are invalid")
    source_refs = {
        "source_v3_membership_csv": (source_membership_csv, "source_v3_membership_sha256"),
        "source_v3_membership_provenance": (
            source_membership_provenance,
            "source_v3_membership_provenance_sha256",
        ),
        "source_prices_provenance": (
            source_prices_provenance,
            "source_prices_provenance_sha256",
        ),
    }
    for name, (expected_path, digest_name) in source_refs.items():
        referenced = _resolve_reference(
            provenance_path.parent, references[name], name
        )
        if (
            sha256_file(referenced) != document.get(digest_name)
            or sha256_file(referenced) != sha256_file(expected_path)
        ):
            raise ValueError("financial lineage bridge binds foreign V3 inputs")
    projection_csv = _resolve_reference(
        provenance_path.parent, references["ticker_membership_csv"], "ticker membership CSV"
    )
    ledger_csv = _resolve_reference(
        provenance_path.parent,
        references["lineage_projection_csv"],
        "lineage projection ledger",
    )
    history_csv = _resolve_reference(
        provenance_path.parent,
        references["lineage_extraction_history_csv"],
        "identity extraction history CSV",
    )
    identity_digest = document.get("identity_request_contracts_sha256")
    transition_digest = document.get("identity_transitions_sha256")
    segment_digest = document.get("identity_segments_v1_sha256")
    if (
        identity_digest != pit_canonical_json_sha256(identities)
        or transition_digest != pit_canonical_json_sha256(list(transitions))
        or segment_digest
        != (segment_contract.segment_contract_sha256 if segment_contract is not None else None)
    ):
        raise ValueError("financial lineage bridge binds foreign identity metadata")
    boundaries = identity_boundaries_from_contract(transitions, segment_contract)
    projection = project_v3_membership_to_ticker(
        membership,
        resolve_ticker_for_lineage=lambda lineage, as_of: _ticker_for_lineage(
            lineage, as_of, identities, transitions, segment_contract
        ),
        identity_boundaries=boundaries,
    )
    projection = _clip_projection_to_membership_window(
        projection,
        membership,
        start_date=str(document.get("membership_window_start_date", "")),
        end_date=str(document.get("membership_window_end_date", "")),
    )
    expected_membership_bytes = _csv_bytes(
        _TICKER_MEMBERSHIP_COLUMNS, projection.ticker_events
    )
    expected_ledger_bytes = _csv_bytes(
        _PROJECTION_LEDGER_COLUMNS,
        (
            tuple(row[column] for column in _PROJECTION_LEDGER_COLUMNS)
            for row in projection.ledger_rows
        ),
    )
    history_rows = _identity_extraction_history_rows(
        membership,
        identities=identities,
        transitions=transitions,
        segment_contract=segment_contract,
        start_date=str(document.get("extraction_start_date", "")),
        end_date=str(document.get("extraction_end_date", "")),
    )
    expected_history_bytes = _csv_bytes(_EXTRACTION_HISTORY_COLUMNS, history_rows)
    if (
        projection_csv.read_bytes() != expected_membership_bytes
        or sha256_file(projection_csv) != document.get("ticker_membership_sha256")
    ):
        raise ValueError("financial lineage bridge ticker membership projection is inconsistent")
    if (
        ledger_csv.read_bytes() != expected_ledger_bytes
        or sha256_file(ledger_csv) != document.get("projection_ledger_sha256")
        or document.get("projection_ledger_row_count") != len(projection.ledger_rows)
        or document.get("ticker_membership_event_count") != len(projection.ticker_events)
    ):
        raise ValueError("financial lineage bridge transformation ledger is inconsistent")
    if (
        history_csv.read_bytes() != expected_history_bytes
        or sha256_file(history_csv) != document.get("extraction_history_sha256")
        or document.get("extraction_history_row_count") != len(history_rows)
    ):
        raise ValueError("financial lineage bridge extraction history is inconsistent")
    return projection_csv, ledger_csv, history_csv


def make_export_bridge_record(
    *,
    output_dir: Path,
    membership_csv: Path,
    projection_provenance_path: Path,
) -> dict[str, object]:
    """Bind the SEC export to the membership projection used by its extractor."""

    projection_provenance_path = _regular_file(
        projection_provenance_path, "financial lineage bridge projection provenance"
    )
    document = _json_mapping(projection_provenance_path, "financial lineage bridge projection")
    if (
        document.get("schema_version") != 1
        or document.get("kind") != _BRIDGE_KIND
        or not isinstance(document.get("references"), dict)
    ):
        raise ValueError("financial lineage bridge projection schema is unsupported")
    references = document["references"]
    projected_membership = _resolve_reference(
        projection_provenance_path.parent,
        references.get("ticker_membership_csv"),
        "ticker membership CSV",
    )
    if sha256_file(projected_membership) != sha256_file(membership_csv):
        raise ValueError("SEC exporter membership input differs from its lineage projection")
    if document.get("ticker_membership_sha256") != sha256_file(membership_csv):
        raise ValueError("SEC exporter lineage projection does not bind its membership input")
    history_csv = _resolve_reference(
        projection_provenance_path.parent,
        references.get("lineage_extraction_history_csv"),
        "identity extraction history CSV",
    )
    if sha256_file(history_csv) != document.get("extraction_history_sha256"):
        raise ValueError("SEC exporter lineage projection does not bind its extraction history")
    return {
        "extraction_end_date": document.get("extraction_end_date"),
        "extraction_start_date": document.get("extraction_start_date"),
        "kind": _EXPORT_BRIDGE_KIND,
        "membership_start_date": document.get("membership_window_start_date"),
        "identity_extraction_history_csv_path": _relative_reference(output_dir, history_csv),
        "identity_extraction_history_csv_sha256": sha256_file(history_csv),
        "projection_provenance_path": _relative_reference(
            output_dir, projection_provenance_path
        ),
        "projection_provenance_sha256": sha256_file(projection_provenance_path),
        "schema_version": 1,
        "ticker_membership_csv_path": _relative_reference(output_dir, membership_csv),
        "ticker_membership_csv_sha256": sha256_file(membership_csv),
        "values_and_public_dates_transformed": False,
    }


def validate_export_bridge(
    *,
    output_provenance_path: Path,
    output_provenance: Mapping[str, object],
    fundamentals_csv: Path,
    destination_membership_csv: Path,
    destination_membership_provenance: Path,
    prices_provenance: Path,
    membership: Sequence[tuple[str, str, str, int]],
    identities: Mapping[str, Mapping[str, object]],
    transitions: Sequence[Mapping[str, object]],
    segment_contract: object | None,
    fundamentals_row_count: int,
) -> Mapping[str, str]:
    """Recompute and verify the bridge on the production bundle-build path."""

    bridge = output_provenance.get("financial_lineage_bridge_v1")
    if not isinstance(bridge, dict) or set(bridge) != {
        "extraction_end_date",
        "extraction_start_date",
        "identity_extraction_history_csv_path",
        "identity_extraction_history_csv_sha256",
        "kind",
        "membership_start_date",
        "projection_provenance_path",
        "projection_provenance_sha256",
        "schema_version",
        "ticker_membership_csv_path",
        "ticker_membership_csv_sha256",
        "values_and_public_dates_transformed",
    }:
        raise ValueError("fundamentals provenance has no complete financial lineage bridge")
    if (
        bridge["schema_version"] != 1
        or bridge["kind"] != _EXPORT_BRIDGE_KIND
        or bridge["values_and_public_dates_transformed"] is not False
    ):
        raise ValueError("fundamentals financial lineage bridge declaration is invalid")
    if (
        bridge["extraction_start_date"] != output_provenance.get("start_date")
        or bridge["extraction_end_date"] != output_provenance.get("end_date")
        or bridge["membership_start_date"]
        != output_provenance.get("membership_start_date")
    ):
        raise ValueError("SEC export window differs from the identity extraction history")
    projection_path = _resolve_reference(
        output_provenance_path.parent,
        bridge["projection_provenance_path"],
        "financial lineage bridge provenance",
    )
    ticker_csv = _resolve_reference(
        output_provenance_path.parent,
        bridge["ticker_membership_csv_path"],
        "ticker membership CSV",
    )
    history_csv = _resolve_reference(
        output_provenance_path.parent,
        bridge["identity_extraction_history_csv_path"],
        "identity extraction history CSV",
    )
    if sha256_file(history_csv) != bridge["identity_extraction_history_csv_sha256"]:
        raise ValueError("SEC exporter lineage bridge extraction history digest is invalid")
    if sha256_file(ticker_csv) != bridge["ticker_membership_csv_sha256"]:
        raise ValueError("SEC exporter lineage bridge membership digest is invalid")
    projection_csv, ledger_csv, projected_history_csv = _validate_projection_provenance(
        provenance_path=projection_path,
        expected_sha256=str(bridge["projection_provenance_sha256"]),
        source_membership_csv=destination_membership_csv,
        source_membership_provenance=destination_membership_provenance,
        source_prices_provenance=prices_provenance,
        identities=identities,
        transitions=transitions,
        segment_contract=segment_contract,
        membership=membership,
    )
    if projection_csv != ticker_csv:
        if sha256_file(projection_csv) != sha256_file(ticker_csv):
            raise ValueError("SEC exporter did not consume the projected ticker membership")
    if projected_history_csv != history_csv:
        if sha256_file(projected_history_csv) != sha256_file(history_csv):
            raise ValueError("SEC exporter did not consume the projected identity history")
    if output_provenance.get("membership_csv_sha256") != sha256_file(ticker_csv):
        raise ValueError("fundamentals provenance does not bind projected ticker membership")
    if fundamentals_row_count < 1 or output_provenance.get("fundamental_row_count") != fundamentals_row_count:
        raise ValueError("fundamentals row count is inconsistent with lineage bridge")

    audit_path = output_provenance_path.parent / "fundamentals_audit.csv"
    _regular_file(audit_path, "fundamentals audit CSV")
    expected_audit_sha = output_provenance.get("fundamentals_audit_sha256")
    if not isinstance(expected_audit_sha, str) or sha256_file(audit_path) != expected_audit_sha:
        raise ValueError("fundamentals provenance does not bind its audit CSV")
    if sha256_file(fundamentals_csv) != output_provenance.get("fundamentals_sha256"):
        raise ValueError("fundamentals provenance does not bind its CSV")
    from core.sec_pit_fundamentals import FUNDAMENTAL_AUDIT_COLUMNS, FUNDAMENTAL_COLUMNS

    financial_rows = _read_csv(fundamentals_csv, FUNDAMENTAL_COLUMNS, "fundamentals CSV")
    audit_rows = _read_csv(audit_path, FUNDAMENTAL_AUDIT_COLUMNS, "fundamentals audit CSV")
    if len(financial_rows) != len(audit_rows) or len(financial_rows) != fundamentals_row_count:
        raise ValueError("fundamentals CSV and audit rows are not paired")
    financial_keys = [
        tuple(row[key] for key in ("ticker", "statement_type", "period_end", "public_date"))
        for row in financial_rows
    ]
    audit_keys = [
        tuple(row[key] for key in ("ticker", "statement_type", "period_end", "public_date"))
        for row in audit_rows
    ]
    if financial_keys != audit_keys or len(financial_keys) != len(set(financial_keys)):
        raise ValueError("fundamentals audit keys do not exactly pair with financial rows")
    from core.sec_pit_fundamentals import (
        IDENTITY_EXTRACTION_RESULT_COLUMNS,
        SECURITY_MASTER_COLUMNS,
        SECURITY_MASTER_EXCLUSION_COLUMNS,
    )

    membership_start = output_provenance.get("membership_start_date")
    export_end = output_provenance.get("end_date")
    expected_intervals = _projected_ticker_intervals(
        ticker_csv,
        start_date=str(membership_start),
        end_date=str(export_end),
    )
    master_path = output_provenance_path.parent / "security_master.csv"
    exclusion_path = output_provenance_path.parent / "security_master_exclusions.csv"
    _regular_file(master_path, "security master CSV")
    _regular_file(exclusion_path, "security master exclusions CSV")
    if (
        sha256_file(master_path) != output_provenance.get("security_master_sha256")
        or sha256_file(exclusion_path)
        != output_provenance.get("security_master_exclusions_sha256")
    ):
        raise ValueError("fundamentals provenance does not bind its security master inputs")
    master_rows = _read_csv(master_path, SECURITY_MASTER_COLUMNS, "security master CSV")
    exclusion_rows = _read_csv(
        exclusion_path,
        SECURITY_MASTER_EXCLUSION_COLUMNS,
        "security master exclusions CSV",
        allow_empty=True,
    )
    if (
        output_provenance.get("security_master_row_count") != len(master_rows)
        or output_provenance.get("security_master_exclusion_row_count")
        != len(exclusion_rows)
    ):
        raise ValueError("security master row counts are inconsistent with provenance")
    if any(
        not re.fullmatch(r"\d{10}", row["cik"])
        or not row["company_name"]
        or not row["mapping_basis"]
        for row in master_rows
    ):
        raise ValueError("security master contains incomplete issuer identity evidence")
    actual_intervals: Counter[tuple[str, str, str]] = Counter()
    for row in (*master_rows, *exclusion_rows):
        actual_intervals[(row["ticker"], row["first_membership_date"], row["last_membership_date"])] += 1
    if actual_intervals != expected_intervals:
        raise ValueError("security master does not account for projected membership episodes")

    history_path = output_provenance_path.parent / "financial_lineage_extraction_history.csv"
    _regular_file(history_path, "financial identity extraction history CSV")
    if sha256_file(history_path) != output_provenance.get(
        "financial_lineage_extraction_history_sha256"
    ):
        raise ValueError("fundamentals provenance does not bind its identity extraction history")
    history_rows = _read_csv(
        history_path,
        IDENTITY_EXTRACTION_RESULT_COLUMNS,
        "financial identity extraction history CSV",
    )
    projected_history_rows = _read_csv(
        history_csv, _EXTRACTION_HISTORY_COLUMNS, "projected identity extraction history CSV"
    )
    history_projection_keys = [
        (row["ticker"], row["first_extraction_date"], row["last_extraction_date"], row["security_lineage_id"], row["identity_segment_id"])
        for row in history_rows
    ]
    if (
        output_provenance.get("financial_lineage_extraction_history_row_count")
        != len(history_rows)
        or any(
            not re.fullmatch(r"\d{10}", row["cik"])
            or not row["company_name"]
            or not row["mapping_basis"]
            for row in history_rows
        )
    ):
        raise ValueError("SEC identity extraction history has incomplete CIK evidence")
    projected_history_keys = [
        (row["ticker"], row["first_extraction_date"], row["last_extraction_date"], row["security_lineage_id"], row["identity_segment_id"])
        for row in projected_history_rows
    ]
    if history_projection_keys != projected_history_keys:
        raise ValueError("SEC security master did not consume the authenticated identity history")
    return {
        "financial_lineage_projection_sha256": sha256_file(projection_path),
        "financial_lineage_projection_ledger_sha256": sha256_file(ledger_csv),
        "fundamentals_audit_sha256": sha256_file(audit_path),
        "fundamentals_lineage_membership_sha256": sha256_file(ticker_csv),
        "financial_lineage_extraction_history_sha256": sha256_file(history_path),
        "security_master_sha256": sha256_file(master_path),
        "security_master_exclusions_sha256": sha256_file(exclusion_path),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Project authenticated schema-V3 lineage membership into SEC ticker membership"
    )
    parser.add_argument("--membership-v3-csv", required=True)
    parser.add_argument("--membership-provenance", required=True)
    parser.add_argument("--prices-provenance", required=True)
    parser.add_argument("--output-ticker-membership-csv", required=True)
    parser.add_argument("--output-projection-ledger-csv", required=True)
    parser.add_argument("--output-extraction-history-csv", required=True)
    parser.add_argument("--output-projection-provenance", required=True)
    parser.add_argument(
        "--membership-start-date", default=_DEFAULT_MEMBERSHIP_START
    )
    args = parser.parse_args(argv)
    result = build_membership_projection(
        membership_csv=args.membership_v3_csv,
        membership_provenance=args.membership_provenance,
        prices_provenance=args.prices_provenance,
        output_membership_csv=args.output_ticker_membership_csv,
        output_projection_ledger_csv=args.output_projection_ledger_csv,
        output_extraction_history_csv=args.output_extraction_history_csv,
        output_projection_provenance=args.output_projection_provenance,
        membership_start_date=args.membership_start_date,
    )
    print(pit_canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
