"""Normalize three authenticated membership streams into schema-V3 lineage events."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

from core.pit_data import sha256_file
from core.pit_provenance import (
    PIT_NON_TRADABLE_REFERENCE_SYMBOLS,
    pit_canonical_json_bytes,
    pit_canonical_json_sha256,
)

_SOURCE_COLUMNS = ("effective_date", "ticker", "member")
_OUTPUT_COLUMNS = (
    "effective_date",
    "security_lineage_id",
    "universe_id",
    "member",
)
_UNIVERSES = ("nasdaq100", "russell2000", "sp500")
_TICKER_RE = re.compile(r"[A-Z0-9][A-Z0-9.-]{0,14}\Z")
_LINEAGE_RE = re.compile(r"[a-z][a-z0-9_]{0,47}\Z")
_SAME_ISSUER_CONTINUITIES = frozenset(
    {
        "same_issuer_rename",
        "same_issuer_ticker_reuse",
        "legacy_survivor_rename",
        "accounting_acquirer_rename",
    }
)
_IDENTITY_FIELDS = frozenset(
    {
        "provider_symbol",
        "identity_asof",
        "admitted_start",
        "admitted_end",
        "chain_id",
        "continuity_kind",
        "warmup_predecessor",
        "factor_anchor",
    }
)
_TRANSITION_FIELDS = frozenset(
    {"effective_date", "predecessor", "successor", "chain_id", "continuity_kind"}
)


def _regular_file(path: str | Path, *, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_file() or candidate.is_symlink():
        raise ValueError(f"{label} must be a regular non-link file")
    return candidate.resolve()


def _new_output(path: str | Path, *, label: str) -> Path:
    candidate = Path(path).resolve()
    if candidate.exists() or candidate.is_symlink():
        raise ValueError(f"refusing to overwrite existing {label}: {candidate}")
    return candidate


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate provenance key: {key}")
        result[key] = value
    return result


def _json_mapping(path: str | Path, *, label: str) -> tuple[Path, Mapping[str, object]]:
    resolved = _regular_file(path, label=label)
    try:
        raw = resolved.read_bytes()
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_pairs,
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return resolved, value


def _iso_date(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be an ISO date")
    return value


def _ticker(value: object, *, field: str = "ticker") -> str:
    if not isinstance(value, str) or _TICKER_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a canonical V3 ticker")
    return value


def _lineage(value: object, *, field: str = "chain_id") -> str:
    if not isinstance(value, str) or _LINEAGE_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a canonical security lineage ID")
    return value


def _positive_int(value: object, *, field: str, allow_zero: bool = False) -> int:
    if type(value) is not int or value < (0 if allow_zero else 1):
        qualifier = "nonnegative" if allow_zero else "positive"
        raise ValueError(f"{field} must be a {qualifier} integer")
    return value


def _timestamp(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{field} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{field} must be a UTC timestamp") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError(f"{field} must be a UTC timestamp")
    return value


def _required_text(source: Mapping[str, object], field: str) -> str:
    value = source.get(field)
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"provenance {field} must be non-empty trimmed text")
    return value


def _load_price_identity(
    provenance: Mapping[str, object],
) -> tuple[dict[str, Mapping[str, object]], tuple[Mapping[str, object], ...], str, str]:
    raw_identities = provenance.get("price_identity_request_contracts")
    if not isinstance(raw_identities, dict) or not raw_identities:
        raise ValueError("prices provenance has no price identity request contracts")
    identity_digest = pit_canonical_json_sha256(raw_identities)
    if provenance.get("price_identity_request_contracts_sha256") != identity_digest:
        raise ValueError("prices provenance identity contract digest is invalid")
    identities: dict[str, Mapping[str, object]] = {}
    for raw_ticker, raw_identity in raw_identities.items():
        ticker = _ticker(raw_ticker, field="price identity ticker")
        if not isinstance(raw_identity, dict) or set(raw_identity) != _IDENTITY_FIELDS:
            raise ValueError(f"price identity row is invalid: {ticker}")
        start = _iso_date(raw_identity["admitted_start"], field="admitted_start")
        end = _iso_date(raw_identity["admitted_end"], field="admitted_end")
        identity_asof = _iso_date(raw_identity["identity_asof"], field="identity_asof")
        if end < start or identity_asof != end:
            raise ValueError(f"price identity chronology is invalid: {ticker}")
        _ticker(raw_identity["provider_symbol"], field="provider_symbol")
        lineage = _lineage(raw_identity["chain_id"])
        continuity_kind = _required_text(raw_identity, "continuity_kind")
        predecessor = raw_identity["warmup_predecessor"]
        if predecessor is not None:
            _ticker(predecessor, field="warmup_predecessor")
        if type(raw_identity["factor_anchor"]) is not bool:
            raise ValueError(f"price identity factor_anchor is invalid: {ticker}")
        identities[ticker] = {
            **raw_identity,
            "chain_id": lineage,
            "continuity_kind": continuity_kind,
        }

    raw_transitions = provenance.get("price_identity_transitions")
    if not isinstance(raw_transitions, list):
        raise ValueError("prices provenance has no explicit price identity transitions")
    transitions: list[Mapping[str, object]] = []
    previous_key: tuple[str, str, str, str, str] | None = None
    predecessor_boundaries: set[str] = set()
    successor_boundaries: set[str] = set()
    for raw in raw_transitions:
        if not isinstance(raw, dict) or set(raw) != _TRANSITION_FIELDS:
            raise ValueError("price identity transition row is invalid")
        effective = _iso_date(raw["effective_date"], field="transition effective_date")
        predecessor = _ticker(raw["predecessor"], field="transition predecessor")
        successor = _ticker(raw["successor"], field="transition successor")
        lineage = _lineage(raw["chain_id"], field="transition chain_id")
        continuity = _required_text(raw, "continuity_kind")
        key = (effective, predecessor, successor, lineage, continuity)
        if previous_key is not None and key <= previous_key:
            raise ValueError("price identity transitions are not canonical-sorted")
        if predecessor == successor or continuity not in _SAME_ISSUER_CONTINUITIES:
            raise ValueError("price identity transition is not an authenticated rename")
        predecessor_identity = identities.get(predecessor)
        successor_identity = identities.get(successor)
        if (
            predecessor_identity is None
            or successor_identity is None
            or predecessor_identity["chain_id"] != lineage
            or successor_identity["chain_id"] != lineage
            or successor_identity["continuity_kind"] != continuity
            or not str(successor_identity["admitted_start"]) <= effective
            <= str(successor_identity["admitted_end"])
        ):
            raise ValueError("price identity transition disagrees with identity rows")
        if predecessor in predecessor_boundaries or successor in successor_boundaries:
            raise ValueError("price identity transition boundary is ambiguous")
        predecessor_boundaries.add(predecessor)
        successor_boundaries.add(successor)
        transitions.append(dict(raw))
        previous_key = key
    transition_digest = pit_canonical_json_sha256(raw_transitions)
    declared_transition_digest = provenance.get("price_identity_transitions_sha256")
    if declared_transition_digest is not None and declared_transition_digest != transition_digest:
        raise ValueError("prices provenance transition digest is invalid")
    chains: dict[str, set[str]] = {}
    for ticker, identity in identities.items():
        chains.setdefault(str(identity["chain_id"]), set()).add(ticker)
    for chain_id, tickers in chains.items():
        anchors = {
            ticker for ticker in tickers if identities[ticker]["factor_anchor"] is True
        }
        if len(anchors) != 1:
            raise ValueError(f"price identity chain lacks one factor anchor: {chain_id}")
        edges = [row for row in transitions if row["chain_id"] == chain_id]
        if len(edges) != len(tickers) - 1:
            raise ValueError(f"price identity chain is not explicit and contiguous: {chain_id}")
        incoming = {str(row["successor"]): str(row["predecessor"]) for row in edges}
        outgoing = {str(row["predecessor"]): str(row["successor"]) for row in edges}
        roots = tickers.difference(incoming)
        leaves = tickers.difference(outgoing)
        if len(roots) != 1 or len(leaves) != 1 or anchors != leaves:
            raise ValueError(f"price identity chain is ambiguous: {chain_id}")
        current = next(iter(roots))
        visited: set[str] = set()
        previous_effective: str | None = None
        while current in tickers and current not in visited:
            visited.add(current)
            predecessor = incoming.get(current)
            if identities[current]["warmup_predecessor"] != predecessor:
                raise ValueError(f"price identity predecessor is inconsistent: {current}")
            successor = outgoing.get(current)
            if successor is None:
                break
            edge = next(row for row in edges if row["predecessor"] == current)
            effective = str(edge["effective_date"])
            if previous_effective is not None and effective <= previous_effective:
                raise ValueError(f"price identity chain chronology is invalid: {chain_id}")
            previous_effective = effective
            current = successor
        if visited != tickers:
            raise ValueError(f"price identity chain is disconnected: {chain_id}")
    return identities, tuple(transitions), identity_digest, transition_digest


def _source_rows(path: Path, *, universe_id: str) -> list[tuple[str, str, int]]:
    rows: list[tuple[str, str, int]] = []
    active: set[str] = set()
    previous: tuple[str, str] | None = None
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            if tuple(reader.fieldnames or ()) != _SOURCE_COLUMNS:
                raise ValueError(
                    f"{universe_id} membership header must be exactly {_SOURCE_COLUMNS!r}"
                )
            for raw in reader:
                if raw.get(None) is not None or any(value is None for value in raw.values()):
                    raise ValueError(f"{universe_id} membership row {reader.line_num} is malformed")
                effective = _iso_date(raw["effective_date"], field="effective_date")
                ticker = _ticker(raw["ticker"])
                if raw["member"] not in {"0", "1"}:
                    raise ValueError("membership member must be canonical integer 0 or 1")
                member = int(raw["member"])
                key = (effective, ticker)
                if previous is not None and key <= previous:
                    raise ValueError(f"{universe_id} membership is not canonical-sorted")
                if bool(member) == (ticker in active):
                    raise ValueError(
                        f"{universe_id} membership transition is not a state change: {ticker}"
                    )
                active.add(ticker) if member else active.remove(ticker)
                rows.append((effective, ticker, member))
                previous = key
    except UnicodeDecodeError as exc:
        raise ValueError(f"{universe_id} membership must be UTF-8") from exc
    except csv.Error as exc:
        raise ValueError(f"{universe_id} membership CSV is malformed") from exc
    if not rows:
        raise ValueError(f"{universe_id} membership is empty")
    return rows


def _validate_source_provenance(
    *,
    universe_id: str,
    membership_path: Path,
    rows: list[tuple[str, str, int]],
    provenance: Mapping[str, object],
) -> tuple[str, str]:
    if provenance.get("universe_id") != universe_id:
        raise ValueError(f"{universe_id} provenance universe/source mismatch")
    if provenance.get("membership_sha256") != sha256_file(membership_path):
        raise ValueError(f"{universe_id} provenance does not bind membership bytes")
    if _positive_int(provenance.get("event_count"), field="event_count") != len(rows):
        raise ValueError(f"{universe_id} provenance event count is inconsistent")
    if provenance.get("first_effective_date") != rows[0][0]:
        raise ValueError(f"{universe_id} provenance first date is inconsistent")
    if provenance.get("last_effective_date") != rows[-1][0]:
        raise ValueError(f"{universe_id} provenance last date is inconsistent")
    symbols = {row[1] for row in rows}
    if _positive_int(provenance.get("symbol_count"), field="symbol_count") != len(symbols):
        raise ValueError(f"{universe_id} provenance symbol count is inconsistent")
    return (
        _required_text(provenance, "source_kind"),
        _timestamp(provenance.get("retrieved_at_utc"), field="retrieved_at_utc"),
    )


def _normalize(
    sources: Mapping[str, tuple[Path, Path, Mapping[str, object]]],
    *,
    identities: Mapping[str, Mapping[str, object]],
    transitions: tuple[Mapping[str, object], ...],
) -> tuple[list[tuple[str, str, str, int]], list[dict[str, object]], int]:
    transition_index = {
        (
            str(row["effective_date"]),
            str(row["predecessor"]),
            str(row["successor"]),
            str(row["chain_id"]),
        )
        for row in transitions
    }
    output: list[tuple[str, str, str, int]] = []
    source_bindings: list[dict[str, object]] = []
    coalesced = 0
    reference_symbols = set(PIT_NON_TRADABLE_REFERENCE_SYMBOLS)
    for universe_id in _UNIVERSES:
        membership_path, provenance_path, provenance = sources[universe_id]
        rows = _source_rows(membership_path, universe_id=universe_id)
        source_kind, retrieved_at = _validate_source_provenance(
            universe_id=universe_id,
            membership_path=membership_path,
            rows=rows,
            provenance=provenance,
        )
        grouped: dict[tuple[str, str], list[tuple[str, int]]] = {}
        for effective, ticker, member in rows:
            if ticker in reference_symbols:
                raise ValueError("market references cannot appear in membership")
            identity = identities.get(ticker)
            if identity is None:
                raise ValueError(f"membership ticker has no authenticated price identity: {ticker}")
            if not str(identity["admitted_start"]) <= effective <= str(
                identity["admitted_end"]
            ):
                raise ValueError(
                    f"membership event is outside authenticated identity bounds: {ticker}"
                )
            grouped.setdefault((effective, str(identity["chain_id"])), []).append(
                (ticker, member)
            )
        lineage_state: set[str] = set()
        for (effective, lineage), events in sorted(grouped.items()):
            if len(events) == 1:
                ticker, member = events[0]
                if bool(member) == (lineage in lineage_state):
                    raise ValueError(
                        f"noncanonical lineage membership state: {universe_id} {lineage}"
                    )
                lineage_state.add(lineage) if member else lineage_state.remove(lineage)
                output.append((effective, lineage, universe_id, member))
                continue
            removals = [ticker for ticker, member in events if member == 0]
            additions = [ticker for ticker, member in events if member == 1]
            if (
                len(events) != 2
                or len(removals) != 1
                or len(additions) != 1
                or lineage not in lineage_state
                or (effective, removals[0], additions[0], lineage)
                not in transition_index
            ):
                raise ValueError(
                    f"ambiguous same-lineage membership transitions: {universe_id} {lineage}"
                )
            coalesced += 1
        source_bindings.append(
            {
                "event_count": len(rows),
                "membership_sha256": sha256_file(membership_path),
                "provenance_sha256": sha256_file(provenance_path),
                "retrieved_at_utc": retrieved_at,
                "source_kind": source_kind,
                "universe_id": universe_id,
            }
        )
    output.sort()
    if not output:
        raise ValueError("normalized membership is empty")
    return output, source_bindings, coalesced


def _csv_bytes(rows: list[tuple[str, str, str, int]]) -> bytes:
    lines = [",".join(_OUTPUT_COLUMNS)]
    lines.extend(",".join((effective, lineage, universe, str(member))) for effective, lineage, universe, member in rows)
    return ("\n".join(lines) + "\n").encode("utf-8")


def _publish(staged: list[tuple[Path, Path]]) -> None:
    published: list[tuple[Path, Path]] = []
    try:
        for temporary, target in staged:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.link(temporary, target)
            published.append((temporary, target))
    except FileExistsError as exc:
        for temporary, target in reversed(published):
            try:
                if target.exists() and os.path.samefile(temporary, target):
                    target.unlink()
            except OSError:
                pass
        raise ValueError(f"refusing to overwrite existing output: {exc.filename}") from exc
    except Exception:
        for temporary, target in reversed(published):
            try:
                if target.exists() and os.path.samefile(temporary, target):
                    target.unlink()
            except OSError:
                pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Normalize authenticated S&P 500, Nasdaq-100, and Russell 2000 membership"
    )
    for universe in ("sp500", "nasdaq100", "russell2000"):
        parser.add_argument(f"--{universe}-membership", required=True)
        parser.add_argument(f"--{universe}-provenance", required=True)
    parser.add_argument("--prices-provenance", required=True)
    parser.add_argument("--output-csv", required=True)
    parser.add_argument("--output-provenance", required=True)
    args = parser.parse_args()

    sources: dict[str, tuple[Path, Path, Mapping[str, object]]] = {}
    inputs: set[Path] = set()
    for universe in _UNIVERSES:
        membership_path = _regular_file(
            getattr(args, f"{universe}_membership"), label=f"{universe} membership"
        )
        provenance_path, provenance = _json_mapping(
            getattr(args, f"{universe}_provenance"),
            label=f"{universe} provenance",
        )
        sources[universe] = (membership_path, provenance_path, provenance)
        inputs.update((membership_path, provenance_path))
    prices_path, prices_provenance = _json_mapping(
        args.prices_provenance, label="prices provenance"
    )
    inputs.add(prices_path)
    if len(inputs) != 7:
        raise ValueError("all membership and provenance inputs must be distinct")
    output_csv = _new_output(args.output_csv, label="membership CSV")
    output_provenance = _new_output(args.output_provenance, label="membership provenance")
    if output_csv == output_provenance or {output_csv, output_provenance}.intersection(inputs):
        raise ValueError("outputs must differ from each other and all inputs")

    before = {path: sha256_file(path) for path in inputs}
    identities, transitions, identity_digest, transition_digest = _load_price_identity(
        prices_provenance
    )
    rows, source_bindings, coalesced = _normalize(
        sources, identities=identities, transitions=transitions
    )
    csv_payload = _csv_bytes(rows)
    csv_digest = hashlib.sha256(csv_payload).hexdigest()
    lineages = {row[1] for row in rows}
    universe_counts = {
        universe: sum(row[2] == universe for row in rows) for universe in _UNIVERSES
    }
    provenance = {
        "coalesced_transition_count": coalesced,
        "event_count": len(rows),
        "first_effective_date": rows[0][0],
        "inputs": source_bindings,
        "kind": "pit_universe_membership_v3",
        "last_effective_date": rows[-1][0],
        "lineage_count": len(lineages),
        "membership_sha256": csv_digest,
        "price_identity_request_contracts_sha256": identity_digest,
        "price_identity_transitions_sha256": transition_digest,
        "prices_provenance_sha256": sha256_file(prices_path),
        "schema_version": 3,
        "source_universes": list(_UNIVERSES),
        "universe_count": len(_UNIVERSES),
        "universe_event_counts": universe_counts,
    }
    provenance_payload = pit_canonical_json_bytes(provenance)
    if before != {path: sha256_file(path) for path in inputs}:
        raise ValueError("an input changed while membership was being normalized")

    temp_paths: list[Path] = []
    staged: list[tuple[Path, Path]] = []
    try:
        for target, payload in (
            (output_csv, csv_payload),
            (output_provenance, provenance_payload),
        ):
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            temp_paths.append(temporary)
            temporary.write_bytes(payload)
            staged.append((temporary, target))
        if before != {path: sha256_file(path) for path in inputs}:
            raise ValueError("an input changed before membership publication")
        _publish(staged)
    finally:
        for temporary in temp_paths:
            if temporary.exists() or temporary.is_symlink():
                temporary.unlink()
    print(pit_canonical_json_bytes(provenance).decode("utf-8").rstrip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
