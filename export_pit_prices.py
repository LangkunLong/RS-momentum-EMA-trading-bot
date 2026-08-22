"""Export a hash-pinned DataFetcher cache through a confined offline worker."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import BinaryIO, Callable, Sequence
from urllib.parse import quote

from core.alpaca_pit_backfill import (
    ProviderIdentity,
    REQUEST_ALIASES,
    _apply_cutoff_split_factors,
    _derive_cutoff_split_factors,
    fetch_alpaca_sip_raw_calibration,
    fetch_alpaca_sip_snapshot,
    load_alpaca_credentials,
)

_BASELINE_START = date(2020, 1, 1)
_BASELINE_END = date(2025, 12, 31)
_EXPECTED_SCHEMA = [
    (0, "cache_key", "TEXT", 0, None, 1),
    (1, "cache_kind", "TEXT", 1, None, 0),
    (2, "created_at", "TEXT", 1, None, 0),
    (3, "payload", "BLOB", 1, None, 0),
]
_MEMBERSHIP_COLUMNS = ("effective_date", "ticker", "member")
_SYMBOL_HISTORY_COLUMNS = (
    "source_ticker",
    "canonical_ticker",
    "effective_start",
    "effective_end",
    "reason",
)
_PRICE_IDENTITY_COLUMNS = (
    "canonical_ticker",
    "provider_symbol",
    "identity_asof",
    "admitted_start",
    "admitted_end",
    "chain_id",
    "continuity_kind",
    "warmup_predecessor",
    "factor_anchor",
    "evidence_url",
)
_PRICE_COLUMNS = ("trade_date", "ticker", "open", "high", "low", "close", "volume")
_OUTPUT_NAMES = ("prices.csv", "spy_trading_days.csv", "prices_provenance.json")
_BACKFILL_OUTPUT_NAMES = (*_OUTPUT_NAMES, "alpaca_sip_snapshot.csv")
_TICKER_RE = re.compile(r"[A-Z0-9][A-Z0-9.-]{0,14}")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_IMAGE_RE = re.compile(r"[^\s]+@sha256:[0-9a-f]{64}")
_COMPLETION = '{"status":"complete","version":1}\n'
_REVIEWED_SYMBOL_HISTORY_SHA256 = "6284214a6a4cefd766b3c52e84be57ac7e087cbf76d642d22abad131d61d8fa4"
_REVIEWED_PRICE_IDENTITY_SHA256 = "6a9ec69bc0fe05decea1b832cac8e26a611d706cce831d5687fa5424f9544955"
_WORKER_UID_GID = "65532:65532"
_PIDS_LIMIT = 64
_MEMORY_BYTES = 2 * 1024 * 1024 * 1024
_NANO_CPUS = 2_000_000_000
_TMPFS_BYTES = 64 * 1024 * 1024
_TMPFS_DESTINATION = "/tmp"
_TMPFS_FLAGS = frozenset({"rw", "noexec", "nosuid", "nodev"})
_TMPFS_CREATE_SPEC = f"{_TMPFS_DESTINATION}:rw,noexec,nosuid,nodev,size={_TMPFS_BYTES}"
_NYSE_HOLIDAYS = frozenset(
    date.fromisoformat(value)
    for value in (
        "2020-01-01", "2020-01-20", "2020-02-17", "2020-04-10", "2020-05-25",
        "2020-07-03", "2020-09-07", "2020-11-26", "2020-12-25",
        "2021-01-01", "2021-01-18", "2021-02-15", "2021-04-02", "2021-05-31",
        "2021-07-05", "2021-09-06", "2021-11-25", "2021-12-24",
        "2022-01-17", "2022-02-21", "2022-04-15", "2022-05-30", "2022-06-20",
        "2022-07-04", "2022-09-05", "2022-11-24", "2022-12-26",
        "2023-01-02", "2023-01-16", "2023-02-20", "2023-04-07", "2023-05-29",
        "2023-06-19", "2023-07-04", "2023-09-04", "2023-11-23", "2023-12-25",
        "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27",
        "2024-06-19", "2024-07-04", "2024-09-02", "2024-11-28", "2024-12-25",
        "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18",
        "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27",
        "2025-12-25",
    )
)


@dataclass(frozen=True)
class Membership:
    events: tuple[tuple[date, str, bool], ...]
    tickers: tuple[str, ...]


@dataclass(frozen=True)
class CacheSnapshot:
    path: Path
    sha256: str
    key_count: int
    keys_sha256: str


@dataclass(frozen=True)
class IdentityBounds:
    path: Path
    sha256: str
    end_dates: dict[str, date]
    mapped_symbol_count: int
    canonical_tickers: tuple[str, ...]


@dataclass(frozen=True)
class PriceIdentity:
    canonical_ticker: str
    provider_symbol: str
    identity_asof: date
    admitted_start: date
    admitted_end: date
    chain_id: str
    continuity_kind: str
    warmup_predecessor: str | None
    factor_anchor: bool
    evidence_url: str


@dataclass(frozen=True)
class PriceIdentityManifest:
    path: Path
    sha256: str
    identities: dict[str, PriceIdentity]
    chain_count: int


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def _regular_file(path: Path, label: str) -> Path:
    absolute = Path(os.path.abspath(path))
    info = absolute.lstat()
    resolved = absolute.resolve(strict=True)
    if not stat.S_ISREG(info.st_mode) or absolute.is_symlink() or resolved != absolute:
        raise ValueError(f"{label} must be a regular non-link file")
    return resolved


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_membership(path: Path) -> Membership:
    path = _regular_file(path, "membership CSV")
    events: list[tuple[date, str, bool]] = []
    seen: set[tuple[date, str]] = set()
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != _MEMBERSHIP_COLUMNS:
            raise ValueError("membership CSV has an unexpected header")
        for row_number, row in enumerate(reader, start=2):
            try:
                effective = date.fromisoformat(row["effective_date"])
            except (KeyError, ValueError) as exc:
                raise ValueError(f"membership row {row_number} has an invalid date") from exc
            ticker = row.get("ticker", "")
            member = row.get("member")
            if _TICKER_RE.fullmatch(ticker) is None or member not in {"0", "1"}:
                raise ValueError(f"membership row {row_number} is invalid")
            identity = (effective, ticker)
            if identity in seen:
                raise ValueError(f"membership row {row_number} is duplicated")
            seen.add(identity)
            events.append((effective, ticker, member == "1"))
    if not events or events != sorted(events, key=lambda item: (item[0], item[1])):
        raise ValueError("membership rows must be non-empty and sorted by date/ticker")
    return Membership(tuple(events), tuple(sorted({ticker for _, ticker, _ in events})))


def _load_identity_bounds(
    path: Path,
    expected_sha256: str,
    membership: Membership,
    cutoff: date,
) -> IdentityBounds:
    path = _regular_file(path, "reviewed symbol-history map")
    if _DIGEST_RE.fullmatch(expected_sha256) is None or _sha256_file(path) != expected_sha256:
        raise ValueError("reviewed symbol-history map does not match its required SHA-256")
    mapped: dict[str, date] = {}
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != _SYMBOL_HISTORY_COLUMNS:
            raise ValueError("reviewed symbol-history map has an unexpected header")
        for row_number, row in enumerate(reader, start=2):
            source = row.get("source_ticker", "")
            canonical = row.get("canonical_ticker", "")
            try:
                effective_start = date.fromisoformat(row["effective_start"])
                effective_end = date.fromisoformat(row["effective_end"])
            except (KeyError, ValueError):
                raise ValueError(f"reviewed symbol-history row {row_number} has invalid dates") from None
            if (
                _TICKER_RE.fullmatch(source) is None
                or _TICKER_RE.fullmatch(canonical) is None
                or canonical not in membership.tickers
                or not _BASELINE_START <= effective_start <= effective_end <= cutoff
                or not row.get("reason", "").strip()
            ):
                raise ValueError(f"reviewed symbol-history row {row_number} is invalid")
            mapped[canonical] = max(mapped.get(canonical, effective_end), effective_end)
    if not mapped:
        raise ValueError("reviewed symbol-history map contains no canonical identities")
    end_dates = {ticker: mapped.get(ticker, cutoff) for ticker in membership.tickers}
    end_dates["SPY"] = cutoff
    return IdentityBounds(
        path,
        expected_sha256,
        end_dates,
        len(mapped),
        tuple(sorted(mapped)),
    )


def _load_price_identity_manifest(
    path: Path,
    expected_sha256: str,
    membership: Membership,
    history: IdentityBounds,
    cutoff: date,
) -> PriceIdentityManifest:
    path = _regular_file(path, "reviewed price-identity map")
    if _DIGEST_RE.fullmatch(expected_sha256) is None or _sha256_file(path) != expected_sha256:
        raise ValueError("reviewed price-identity map does not match its required SHA-256")
    identities: dict[str, PriceIdentity] = {}
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != _PRICE_IDENTITY_COLUMNS:
            raise ValueError("reviewed price-identity map has an unexpected header")
        for row_number, row in enumerate(reader, start=2):
            canonical = row.get("canonical_ticker", "")
            provider = row.get("provider_symbol", "")
            predecessor = row.get("warmup_predecessor", "") or None
            try:
                identity_asof = date.fromisoformat(row["identity_asof"])
                admitted_start = date.fromisoformat(row["admitted_start"])
                admitted_end = date.fromisoformat(row["admitted_end"])
            except (KeyError, ValueError):
                raise ValueError(
                    f"reviewed price-identity row {row_number} has invalid dates"
                ) from None
            identity = PriceIdentity(
                canonical,
                provider,
                identity_asof,
                admitted_start,
                admitted_end,
                row.get("chain_id", ""),
                row.get("continuity_kind", ""),
                predecessor,
                row.get("factor_anchor") == "1",
                row.get("evidence_url", ""),
            )
            if (
                _TICKER_RE.fullmatch(canonical) is None
                or _TICKER_RE.fullmatch(provider) is None
                or canonical in identities
                or canonical not in membership.tickers
                or row.get("factor_anchor") not in {"0", "1"}
                or re.fullmatch(r"[a-z][a-z0-9_]{0,47}", identity.chain_id) is None
                or re.fullmatch(r"[a-z][a-z0-9_]{0,47}", identity.continuity_kind) is None
                or predecessor is not None and _TICKER_RE.fullmatch(predecessor) is None
                or not _BASELINE_START <= admitted_start <= admitted_end <= cutoff
                or identity_asof != admitted_end
                or not identity.evidence_url.startswith("https://")
            ):
                raise ValueError(f"reviewed price-identity row {row_number} is invalid")
            identities[canonical] = identity
    if set(identities) != set(history.canonical_tickers):
        raise ValueError("price-identity map does not exactly cover the membership symbol map")
    chains: dict[str, list[PriceIdentity]] = {}
    for identity in identities.values():
        chains.setdefault(identity.chain_id, []).append(identity)
    for chain_id, members in chains.items():
        members.sort(key=lambda item: (item.admitted_start, item.canonical_ticker))
        anchors = [member for member in members if member.factor_anchor]
        terminal = max(members, key=lambda item: (item.admitted_end, item.canonical_ticker))
        if len(anchors) != 1 or anchors[0] != terminal:
            raise ValueError(f"price-identity chain {chain_id} lacks one terminal factor anchor")
        for index, identity in enumerate(members):
            if index == 0:
                if identity.warmup_predecessor is not None:
                    raise ValueError(f"price-identity chain {chain_id} has an invalid first predecessor")
            elif identity.warmup_predecessor != members[index - 1].canonical_ticker:
                raise ValueError(f"price-identity chain {chain_id} is not explicitly contiguous")
            if index:
                predecessor = members[index - 1]
                contiguous = predecessor.admitted_end + timedelta(days=1) == identity.admitted_start
                containing = (
                    predecessor.admitted_start <= identity.admitted_start
                    and identity.admitted_end <= predecessor.admitted_end
                )
                if not contiguous and not containing:
                    raise ValueError(
                        f"price-identity chain {chain_id} has an invalid admission boundary"
                    )
    psky = identities.get("PSKY")
    if (
        psky is None
        or psky.chain_id != "paramount_successor"
        or psky.continuity_kind != "successor_reset"
        or psky.warmup_predecessor is not None
        or psky.admitted_start != date(2025, 8, 7)
    ):
        raise ValueError("PSKY must remain an explicit no-predecessor successor reset")
    return PriceIdentityManifest(path, expected_sha256, identities, len(chains))


def _provider_symbol(canonical_ticker: str) -> str:
    return next(
        (
            provider
            for provider, canonical in REQUEST_ALIASES.items()
            if canonical == canonical_ticker
        ),
        canonical_ticker,
    )


def _complete_price_identities(
    membership: Membership,
    manifest: PriceIdentityManifest,
    cutoff: date,
) -> dict[str, PriceIdentity]:
    identities = dict(manifest.identities)
    for ticker in (*membership.tickers, "SPY"):
        if ticker not in identities:
            identities[ticker] = PriceIdentity(
                ticker,
                _provider_symbol(ticker),
                cutoff,
                _BASELINE_START,
                cutoff,
                f"unmapped_{ticker.casefold().replace('-', '_').replace('.', '_')}",
                "unmapped_cutoff_identity",
                None,
                True,
                "https://docs.alpaca.markets/reference/stockbars",
            )
    return {ticker: identities[ticker] for ticker in sorted(identities)}


def _provider_identity_contracts(
    identities: dict[str, PriceIdentity],
) -> dict[str, ProviderIdentity]:
    return {
        ticker: ProviderIdentity(
            ticker,
            identity.provider_symbol,
            identity.identity_asof,
            identity.admitted_start,
            identity.admitted_end,
            True,
        )
        for ticker, identity in sorted(identities.items())
    }


def _expand_chain_cutoff_factors(
    anchor_factors: dict[str, float],
    identities: dict[str, PriceIdentity],
    cutoff: date,
) -> dict[str, float]:
    chains: dict[str, list[PriceIdentity]] = {}
    for identity in identities.values():
        chains.setdefault(identity.chain_id, []).append(identity)
    expected_anchors = {
        identity.canonical_ticker
        for identity in identities.values()
        if identity.factor_anchor
    }
    if set(anchor_factors) != expected_anchors:
        raise ValueError("cutoff factor anchors differ from reviewed price-identity chains")
    factors: dict[str, float] = {}
    for chain_id, members in sorted(chains.items()):
        anchors = [member for member in members if member.factor_anchor]
        if len(anchors) != 1:
            raise ValueError(f"price-identity chain {chain_id} lacks one factor anchor")
        anchor = anchors[0]
        factor = anchor_factors[anchor.canonical_ticker]
        if max(member.admitted_end for member in members) < cutoff and factor != 1.0:
            raise ValueError(
                f"pre-cutoff chain {chain_id} has an unproved non-unity terminal factor"
            )
        for member in members:
            factors[member.canonical_ticker] = factor
    return {ticker: factors[ticker] for ticker in sorted(factors)}


def _copy_and_validate_cache(source: Path, expected_sha256: str, root: Path) -> CacheSnapshot:
    if _DIGEST_RE.fullmatch(expected_sha256) is None:
        raise ValueError("cache SHA-256 must be exactly 64 lowercase hexadecimal characters")
    source = _regular_file(source, "cache")
    if any(source.name.endswith(suffix) for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("cache sidecars cannot be supplied as the cache")
    if any(Path(str(source) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("cache has an unapproved SQLite sidecar")
    before = source.stat()
    destination = root / "cache.sqlite3"
    digest = hashlib.sha256()
    with source.open("rb") as reader, destination.open("xb") as writer:
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            digest.update(chunk)
            writer.write(chunk)
    after = source.stat()
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after or digest.hexdigest() != expected_sha256:
        raise ValueError("cache changed during capture or does not match the required SHA-256")
    os.chmod(destination, stat.S_IREAD)
    with destination.open("rb") as stream:
        if stream.read(16) != b"SQLite format 3\x00":
            raise ValueError("cache lacks the SQLite header")
    uri = f"file:{quote(destination.as_posix(), safe='/:')}?mode=ro&immutable=1"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA query_only=ON")
            objects = connection.execute(
                "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
            if objects != [("table", "dataset_cache")]:
                raise ValueError("cache contains unexpected SQLite schema objects")
            if connection.execute("PRAGMA table_info(dataset_cache)").fetchall() != _EXPECTED_SCHEMA:
                raise ValueError("cache dataset_cache columns differ from DataFetcher")
            rows = connection.execute(
                "SELECT cache_key, cache_kind, created_at, length(payload) "
                "FROM dataset_cache ORDER BY cache_kind, cache_key"
            ).fetchall()
    except sqlite3.Error as exc:
        raise ValueError("cache SQLite validation failed") from exc
    if not rows:
        raise ValueError("cache contains no dataset rows")
    canonical_keys: list[str] = []
    kinds: set[str] = set()
    for cache_key, cache_kind, created_at, payload_length in rows:
        kinds.add(cache_kind)
        parts = cache_key.split("::", 4) if isinstance(cache_key, str) else []
        try:
            key_start = date.fromisoformat(parts[2])
            key_end = date.fromisoformat(parts[3])
        except (IndexError, TypeError, ValueError) as exc:
            raise ValueError("cache contains a malformed cache key") from exc
        symbols = parts[4].split(",") if len(parts) == 5 else []
        if (
            cache_kind not in {"price", "closes"}
            or len(parts) != 5
            or parts[0] != cache_kind
            or not parts[1]
            or key_start > key_end
            or not symbols
            or symbols != sorted(set(symbols))
            or any(_TICKER_RE.fullmatch(symbol) is None for symbol in symbols)
            or not isinstance(created_at, str)
            or not created_at
            or not isinstance(payload_length, int)
            or payload_length <= 0
        ):
            raise ValueError("cache contains a non-price/closes or malformed row")
        canonical_keys.append(f"{cache_kind}\0{cache_key}\0{created_at}\0{payload_length}\n")
    if "price" not in kinds:
        raise ValueError("cache contains no price payloads")
    keys_sha256 = hashlib.sha256("".join(canonical_keys).encode()).hexdigest()
    return CacheSnapshot(destination, expected_sha256, len(rows), keys_sha256)


def _canonical_request(path: Path, membership: Membership, start: date, end: date) -> None:
    value = {
        "end_date": end.isoformat(),
        "start_date": start.isoformat(),
        "tickers": sorted({*membership.tickers, "SPY"}),
        "version": 1,
    }
    path.write_bytes((json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode())
    os.chmod(path, stat.S_IREAD)


def _docker_call(executable: Path, args: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "PATHEXT", "SYSTEMDRIVE", "SYSTEMROOT", "WINDIR", "COMSPEC"}
    }
    return subprocess.run(
        (str(executable), *args),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=timeout,
        env=environment,
    )


def _mount(source: Path, destination: str, *, readonly: bool) -> str:
    value = f"type=bind,src={source.resolve()},dst={destination}"
    return value + (",readonly" if readonly else "")


def _local_image_id(executable: Path, image: str) -> str:
    inspected = _docker_call(executable, ("image", "inspect", image), timeout=30)
    if inspected.returncode != 0:
        raise RuntimeError("digest-pinned sandbox image is not available locally")
    try:
        values = json.loads(inspected.stdout)
        item = values[0]
        image_id = item["Id"]
        repo_digests = item["RepoDigests"]
    except (json.JSONDecodeError, IndexError, KeyError, TypeError) as exc:
        raise RuntimeError("local sandbox image inspection is malformed") from exc
    if (
        len(values) != 1
        or not isinstance(image_id, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None
        or not isinstance(repo_digests, list)
        or image not in repo_digests
    ):
        raise RuntimeError("local sandbox image does not match the required digest")
    return image_id


def _docker_create_args(
    name: str,
    ownership: str,
    image: str,
    mount_specs: Sequence[tuple[Path, str, bool]],
) -> list[str]:
    args = [
        "create", "--pull", "never", "--name", name,
        "--label", f"pit-price-export.owner={ownership}",
        "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--pids-limit", str(_PIDS_LIMIT),
        "--memory", f"{_MEMORY_BYTES}b", "--cpus", "2", "--user", _WORKER_UID_GID,
        "--entrypoint", "python", "--workdir", "/worker",
        "--tmpfs", _TMPFS_CREATE_SPEC,
    ]
    for source, destination, readonly in mount_specs:
        args.extend(("--mount", _mount(source, destination, readonly=readonly)))
    for setting in (
        "HOME=/tmp", "PYTHONDONTWRITEBYTECODE=1", "PYTHONNOUSERSITE=1", "PYTHONHASHSEED=0",
        "OPENBLAS_NUM_THREADS=1", "OMP_NUM_THREADS=1", "MKL_NUM_THREADS=1",
        "NUMEXPR_NUM_THREADS=1", "HTTP_PROXY=http://127.0.0.1:9",
        "HTTPS_PROXY=http://127.0.0.1:9", "ALL_PROXY=http://127.0.0.1:9", "NO_PROXY=",
    ):
        args.extend(("--env", setting))
    args.extend(
        (
            image, "/worker/export_price_cache_worker.py", "--request", "/input/request.json",
            "--cache", "/input/cache.sqlite3", "--output", "/output/prices.csv",
        )
    )
    return args


def _size_bytes(value: str) -> int:
    match = re.fullmatch(r"([0-9]+)([kmgt]?)(?:b)?", value.casefold())
    if match is None:
        raise ValueError
    number = int(match.group(1))
    factor = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}[
        match.group(2)
    ]
    return number * factor


def _exact_tmpfs(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {_TMPFS_DESTINATION}:
        return False
    options = value[_TMPFS_DESTINATION]
    if not isinstance(options, str):
        return False
    parts = options.split(",")
    if len(parts) != len(set(parts)):
        return False
    flags = {part for part in parts if "=" not in part}
    sizes = [part.split("=", 1)[1] for part in parts if part.startswith("size=")]
    if flags != _TMPFS_FLAGS or len(sizes) != 1 or len(parts) != len(flags) + 1:
        return False
    try:
        return _size_bytes(sizes[0]) == _TMPFS_BYTES
    except ValueError:
        return False


def _validate_container_item(
    item: object,
    container_id: str,
    name: str,
    ownership: str,
    image: str,
    image_id: str,
    mounts: dict[str, tuple[Path, bool]],
) -> None:
    try:
        if not isinstance(item, dict):
            raise TypeError
        config = item["Config"]
        host = item["HostConfig"]
        network = item["NetworkSettings"]["Networks"]
        actual_mounts = {
            entry["Destination"]: (Path(entry["Source"]).resolve(), not entry["RW"])
            for entry in item["Mounts"]
        }
    except (KeyError, TypeError) as exc:
        raise RuntimeError("offline worker inspection is malformed") from exc
    expected_cmd = [
        "/worker/export_price_cache_worker.py", "--request", "/input/request.json",
        "--cache", "/input/cache.sqlite3", "--output", "/output/prices.csv",
    ]
    exact_limits = (
        isinstance(host, dict)
        and type(host.get("PidsLimit")) is int
        and host.get("PidsLimit") == _PIDS_LIMIT
        and type(host.get("Memory")) is int
        and host.get("Memory") == _MEMORY_BYTES
        and type(host.get("NanoCpus")) is int
        and host.get("NanoCpus") == _NANO_CPUS
        and _exact_tmpfs(host.get("Tmpfs"))
    )
    labels = config.get("Labels") if isinstance(config, dict) else None
    if (
        not isinstance(config, dict)
        or not isinstance(host, dict)
        or not isinstance(network, dict)
        or item.get("Id") != container_id
        or item.get("Name") != f"/{name}"
        or item.get("Image") != image_id
        or not isinstance(labels, dict)
        or labels.get("pit-price-export.owner") != ownership
        or config.get("Image") != image
        or config.get("Entrypoint") != ["python"]
        or config.get("Cmd") != expected_cmd
        or config.get("User") != _WORKER_UID_GID
        or config.get("WorkingDir") != "/worker"
        or host.get("NetworkMode") != "none"
        or host.get("ReadonlyRootfs") is not True
        or host.get("CapDrop") != ["ALL"]
        or "no-new-privileges" not in (host.get("SecurityOpt") or [])
        or not exact_limits
        or set(network) != {"none"}
        or actual_mounts != mounts
    ):
        raise RuntimeError("offline worker confinement differs from the exact contract")


def _attest_container(
    executable: Path,
    container_id: str,
    name: str,
    ownership: str,
    image: str,
    image_id: str,
    mounts: dict[str, tuple[Path, bool]],
) -> None:
    inspected = _docker_call(executable, ("inspect", container_id), timeout=30)
    if inspected.returncode != 0:
        raise RuntimeError(f"cannot inspect offline worker: {inspected.stderr.strip()}")
    try:
        values = json.loads(inspected.stdout)
        item = values[0]
    except (json.JSONDecodeError, IndexError, TypeError) as exc:
        raise RuntimeError("offline worker inspection is malformed") from exc
    if len(values) != 1:
        raise RuntimeError("offline worker inspection is malformed")
    _validate_container_item(item, container_id, name, ownership, image, image_id, mounts)


def _prepare_container_access(
    root: Path,
    worker_script: Path,
    request_path: Path,
    cache_path: Path,
    output_dir: Path,
    *,
    native_posix: bool | None = None,
) -> Path:
    prepared_worker = root / "export_price_cache_worker.py"
    with worker_script.open("rb") as reader, prepared_worker.open("xb") as writer:
        shutil.copyfileobj(reader, writer, length=1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())
    use_posix_modes = os.name == "posix" if native_posix is None else native_posix
    if use_posix_modes:
        os.chmod(root, 0o700)
        for path in (prepared_worker, request_path, cache_path):
            os.chmod(path, 0o444)
        # The unpredictable 0700 parent keeps the bind private on the host;
        # execute/write for "other" lets only the mounted UID create its known output.
        os.chmod(output_dir, 0o733)
    else:
        for path in (prepared_worker, request_path, cache_path):
            os.chmod(path, stat.S_IREAD)
    return prepared_worker


def _run_worker(
    executable: Path,
    image: str,
    worker_script: Path,
    snapshot: CacheSnapshot,
    request_path: Path,
    output_dir: Path,
) -> Path:
    executable = _regular_file(executable, "Docker executable")
    if executable.name.casefold() not in {"docker", "docker.exe"}:
        raise ValueError("Docker executable must be docker or docker.exe")
    if _IMAGE_RE.fullmatch(image) is None:
        raise ValueError("sandbox image must be repository-and-SHA256 digest pinned")
    worker_script = _regular_file(worker_script, "worker script")
    image_id = _local_image_id(executable, image)
    name = f"pit-price-export-{uuid.uuid4().hex}"
    ownership = uuid.uuid4().hex
    mount_specs = (
        (worker_script, "/worker/export_price_cache_worker.py", True),
        (request_path, "/input/request.json", True),
        (snapshot.path, "/input/cache.sqlite3", True),
        (output_dir, "/output", False),
    )
    mounts = {
        destination: (source.resolve(), readonly)
        for source, destination, readonly in mount_specs
    }
    args = _docker_create_args(name, ownership, image, mount_specs)
    container_id = ""
    try:
        created = _docker_call(executable, tuple(args), timeout=60)
        container_id = created.stdout.strip()
        if created.returncode != 0 or re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
            raise RuntimeError(f"offline worker creation failed: {created.stderr.strip()}")
        _attest_container(
            executable, container_id, name, ownership, image, image_id, mounts
        )
        started = _docker_call(executable, ("start", container_id), timeout=30)
        if started.returncode != 0 or started.stdout.strip() != container_id:
            raise RuntimeError(f"offline worker start failed: {started.stderr.strip()}")
        waited = _docker_call(executable, ("wait", container_id), timeout=600)
        if waited.returncode != 0 or waited.stdout.strip() != "0":
            logs = _docker_call(executable, ("logs", container_id), timeout=30)
            raise RuntimeError(f"offline worker failed: {logs.stderr.strip() or logs.stdout.strip()}")
        logs = _docker_call(executable, ("logs", container_id), timeout=30)
        if logs.returncode != 0 or logs.stdout != _COMPLETION or logs.stderr:
            raise RuntimeError("offline worker returned an invalid completion envelope")
    finally:
        listing = _docker_call(
            executable,
            (
                "container", "ls", "--all", "--quiet", "--no-trunc",
                "--filter", f"label=pit-price-export.owner={ownership}",
            ),
            timeout=30,
        )
        identifiers = tuple(line.strip() for line in listing.stdout.splitlines() if line.strip())
        if listing.returncode != 0 or len(identifiers) > 1:
            raise RuntimeError("offline worker ownership discovery failed during cleanup")
        owned_id = identifiers[0] if identifiers else container_id
        if owned_id:
            if re.fullmatch(r"[0-9a-f]{64}", owned_id) is None or (
                container_id and owned_id != container_id
            ):
                raise RuntimeError("offline worker cleanup identity is ambiguous")
            removed = _docker_call(executable, ("rm", "--force", owned_id), timeout=30)
            remaining = _docker_call(
                executable,
                (
                    "container", "ls", "--all", "--quiet", "--no-trunc",
                    "--filter", f"label=pit-price-export.owner={ownership}",
                ),
                timeout=30,
            )
            if removed.returncode != 0 or remaining.returncode != 0 or remaining.stdout.strip():
                raise RuntimeError("offline worker cleanup could not be verified")
    entries = tuple(output_dir.iterdir())
    if (
        len(entries) != 1
        or entries[0].name != "prices.csv"
        or entries[0].is_symlink()
        or not entries[0].is_file()
    ):
        raise RuntimeError("offline worker emitted files outside the exact output contract")
    return entries[0]


def _expected_trading_days(start: date, end: date) -> tuple[date, ...]:
    days: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5 and current not in _NYSE_HOLIDAYS:
            days.append(current)
        current += timedelta(days=1)
    return tuple(days)


def _validate_prices(
    path: Path,
    membership: Membership,
    start: date,
    end: date,
    *,
    enforce_gates: bool = True,
) -> tuple[dict[str, object], tuple[date, ...]]:
    requested = {*membership.tickers, "SPY"}
    closes: dict[date, set[str]] = {}
    spy_days: list[date] = []
    previous: tuple[date, str] | None = None
    first_price_date: date | None = None
    last_price_date: date | None = None
    price_row_count = 0
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != _PRICE_COLUMNS:
            raise ValueError("worker prices CSV has an unexpected header")
        for row_number, row in enumerate(reader, start=2):
            try:
                trade_date = date.fromisoformat(row["trade_date"])
                numbers = tuple(float(row[name]) for name in _PRICE_COLUMNS[2:])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"worker prices row {row_number} is malformed") from exc
            ticker = row["ticker"]
            identity = (trade_date, ticker)
            if (
                ticker not in requested
                or not start <= trade_date <= end
                or previous is not None and identity <= previous
                or any(not math.isfinite(value) for value in numbers)
                or any(value <= 0 for value in numbers[:4])
                or numbers[4] < 0
            ):
                raise ValueError(f"worker prices row {row_number} violates the output contract")
            previous = identity
            price_row_count += 1
            first_price_date = first_price_date or trade_date
            last_price_date = trade_date
            closes.setdefault(trade_date, set()).add(ticker)
            if ticker == "SPY":
                spy_days.append(trade_date)
    expected_spy = _expected_trading_days(start, end)
    if not spy_days or first_price_date is None or last_price_date is None:
        raise ValueError("worker prices CSV contains no SPY or price rows")
    events_by_date: dict[date, list[tuple[str, bool]]] = {}
    for effective, ticker, member in membership.events:
        events_by_date.setdefault(effective, []).append((ticker, member))
    active: set[str] = set()
    expected_by_symbol = {ticker: 0 for ticker in membership.tickers}
    covered_by_symbol = {ticker: 0 for ticker in membership.tickers}
    pairs = 0
    covered = 0
    event_dates = sorted(events_by_date)
    event_index = 0
    for trading_day in expected_spy:
        while event_index < len(event_dates) and event_dates[event_index] <= trading_day:
            for ticker, member in events_by_date[event_dates[event_index]]:
                if member:
                    active.add(ticker)
                else:
                    active.discard(ticker)
            event_index += 1
        day_closes = closes.get(trading_day, set())
        pairs += len(active)
        for ticker in active:
            expected_by_symbol[ticker] += 1
            if ticker in day_closes:
                covered += 1
                covered_by_symbol[ticker] += 1
    if pairs <= 0:
        raise ValueError("membership does not define any trading-day pairs")
    coverage_ratio = covered / pairs
    no_prices = sorted(ticker for ticker in membership.tickers if covered_by_symbol[ticker] == 0)
    partial = sorted(
        ticker
        for ticker in membership.tickers
        if 0 < covered_by_symbol[ticker] < expected_by_symbol[ticker]
    )
    metrics: dict[str, object] = {
        "member_trading_day_pairs": pairs,
        "covered_member_trading_day_pairs": covered,
        "coverage_pct": round(coverage_ratio * 100, 8),
        "symbols_with_no_prices": no_prices,
        "symbols_with_partial_prices": partial,
        "spy_first_date": spy_days[0].isoformat(),
        "spy_last_date": spy_days[-1].isoformat(),
        "price_row_count": price_row_count,
    }
    if enforce_gates and tuple(spy_days) != expected_spy:
        missing = sorted(set(expected_spy) - set(spy_days))
        unexpected = sorted(set(spy_days) - set(expected_spy))
        detail = f"; first missing trading day: {missing[0]}" if missing else ""
        if unexpected:
            detail += f"; first unexpected day: {unexpected[0]}"
        raise ValueError(
            "SPY coverage is incomplete for 2020-01-01 through 2025-12-31"
            f"; observed SPY: {spy_days[0]} through {spy_days[-1]}"
            f"; observed prices: {first_price_date} through {last_price_date}"
            f"; member coverage: {covered}/{pairs} ({coverage_ratio:.8%})"
            + detail
        )
    if enforce_gates and coverage_ratio < 0.98:
        raise ValueError(f"member/trading-day close coverage is below 98%: {coverage_ratio:.6%}")
    return metrics, tuple(spy_days)


def _relative_difference(left: float, right: float) -> float:
    return abs(left - right) / max(abs(left), abs(right), 1e-12)


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(math.ceil(percentile * len(ordered)) - 1, len(ordered) - 1)
    return ordered[max(index, 0)]


class _OverlapAudit:
    _SAMPLE_LIMIT = 100_000
    _FIELDS = ("open", "high", "low", "close", "volume")

    def __init__(self) -> None:
        self._random = random.Random(0)
        self.count = 0
        self.samples: list[tuple[float, ...]] = []
        self.sums = [0.0] * len(self._FIELDS)
        self.maxima = [0.0] * len(self._FIELDS)
        self.symbol_totals: dict[str, int] = {}
        self.symbol_incompatible: dict[str, int] = {}
        self.symbol_max_ohlc_difference: dict[str, float] = {}

    def add(self, ticker: str, cache_values: Sequence[float], provider_values: Sequence[float]) -> None:
        differences = tuple(
            _relative_difference(cache_value, provider_value)
            for cache_value, provider_value in zip(cache_values, provider_values, strict=True)
        )
        self.count += 1
        for index, difference in enumerate(differences):
            self.sums[index] += difference
            self.maxima[index] = max(self.maxima[index], difference)
        if len(self.samples) < self._SAMPLE_LIMIT:
            self.samples.append(differences)
        else:
            replacement = self._random.randrange(self.count)
            if replacement < self._SAMPLE_LIMIT:
                self.samples[replacement] = differences
        self.symbol_totals[ticker] = self.symbol_totals.get(ticker, 0) + 1
        max_ohlc_difference = max(differences[:4])
        self.symbol_max_ohlc_difference[ticker] = max(
            self.symbol_max_ohlc_difference.get(ticker, 0.0), max_ohlc_difference
        )
        if max_ohlc_difference > 0.20:
            self.symbol_incompatible[ticker] = self.symbol_incompatible.get(ticker, 0) + 1

    def incompatible_symbols(self) -> tuple[str, ...]:
        return tuple(sorted(
            ticker
            for ticker, bad_count in self.symbol_incompatible.items()
            if bad_count >= 5 and bad_count / self.symbol_totals[ticker] >= 0.10
        ))

    def finish(self, *, fail_closed: bool = True) -> dict[str, object]:
        incompatible = self.incompatible_symbols()
        if fail_closed and incompatible:
            ticker = incompatible[0]
            bad_count = self.symbol_incompatible[ticker]
            total = self.symbol_totals[ticker]
            raise ValueError(
                "cache/SIP overlap shows a clear price-scale or corporate-action incompatibility "
                f"for {ticker}: {bad_count}/{total} rows exceed 0.20 "
                f"({bad_count / total:.8%}); max OHLC relative difference "
                f"{self.symbol_max_ohlc_difference[ticker]:.12f}"
            )
        fields: dict[str, object] = {}
        for index, field in enumerate(self._FIELDS):
            sampled = [row[index] for row in self.samples]
            fields[field] = {
                "mean": round(self.sums[index] / self.count, 12) if self.count else 0.0,
                "p50_sample": round(_percentile(sampled, 0.50), 12),
                "p95_sample": round(_percentile(sampled, 0.95), 12),
                "max": round(self.maxima[index], 12),
            }
        return {
            "relative_difference_definition": "abs(cache-sip)/max(abs(cache),abs(sip),1e-12)",
            "overlap_row_count": self.count,
            "sample_limit": self._SAMPLE_LIMIT,
            "sampled_row_count": len(self.samples),
            "incompatibility_rule": (
                "fail when at least 5 and at least 10% of one symbol's overlap rows have "
                "an OHLC relative difference above 0.20"
            ),
            "incompatible_symbol_count": len(incompatible),
            "incompatible_symbols": list(incompatible),
            "fields": fields,
        }


def _audit_price_source_overlaps(left_path: Path, right_path: Path) -> _OverlapAudit:
    audit = _OverlapAudit()
    with (
        left_path.open("r", encoding="utf-8", newline="") as left_stream,
        right_path.open("r", encoding="utf-8", newline="") as right_stream,
    ):
        left_reader = csv.DictReader(left_stream)
        right_reader = csv.DictReader(right_stream)
        if tuple(left_reader.fieldnames or ()) != _PRICE_COLUMNS:
            raise ValueError("left overlap source has an unexpected header")
        if tuple(right_reader.fieldnames or ()) != _PRICE_COLUMNS:
            raise ValueError("right overlap source has an unexpected header")
        left_row = next(left_reader, None)
        right_row = next(right_reader, None)
        while left_row is not None and right_row is not None:
            left_key = (left_row["trade_date"], left_row["ticker"])
            right_key = (right_row["trade_date"], right_row["ticker"])
            if left_key < right_key:
                left_row = next(left_reader, None)
            elif right_key < left_key:
                right_row = next(right_reader, None)
            else:
                left_values = tuple(float(left_row[name]) for name in _PRICE_COLUMNS[2:])
                right_values = tuple(float(right_row[name]) for name in _PRICE_COLUMNS[2:])
                audit.add(left_row["ticker"], left_values, right_values)
                left_row = next(left_reader, None)
                right_row = next(right_reader, None)
    return audit


def _build_price_identity_warmup(
    admitted_path: Path,
    identities: dict[str, PriceIdentity],
    output_path: Path,
) -> dict[str, object]:
    """Copy only reviewed predecessor rows into successor warm-up history."""
    rows: dict[str, dict[date, tuple[float, ...]]] = {
        ticker: {} for ticker in identities
    }
    with admitted_path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != _PRICE_COLUMNS:
            raise ValueError("cutoff snapshot has an unexpected continuity header")
        for row in reader:
            ticker = row["ticker"]
            identity = identities.get(ticker)
            if identity is None:
                raise ValueError("cutoff snapshot contains an unreviewed price identity")
            trade_date = date.fromisoformat(row["trade_date"])
            if not identity.admitted_start <= trade_date <= identity.admitted_end:
                raise ValueError("cutoff snapshot contains a row outside its admitted interval")
            if trade_date in rows[ticker]:
                raise ValueError("cutoff snapshot contains a duplicate identity/date row")
            rows[ticker][trade_date] = tuple(
                float(row[name]) for name in _PRICE_COLUMNS[2:]
            )
    audits: dict[str, object] = {}
    for ticker, identity in sorted(identities.items()):
        predecessor = identity.warmup_predecessor
        if predecessor is None:
            continue
        predecessor_identity = identities.get(predecessor)
        if predecessor_identity is None or predecessor_identity.chain_id != identity.chain_id:
            raise ValueError(f"warmup predecessor {predecessor} is outside {ticker}'s price chain")
        shared = sorted(set(rows[ticker]).intersection(rows[predecessor]))
        mismatches = [
            trade_date
            for trade_date in shared
            if rows[ticker][trade_date] != rows[predecessor][trade_date]
        ]
        if mismatches:
            raise ValueError(
                f"provider identities {predecessor}/{ticker} disagree on an admitted overlap"
            )
        audits[ticker] = {
            "predecessor": predecessor,
            "exact_overlap_row_count": len(shared),
            "overlap_first_date": shared[0].isoformat() if shared else None,
            "overlap_last_date": shared[-1].isoformat() if shared else None,
        }
    augmented = {ticker: dict(ticker_rows) for ticker, ticker_rows in rows.items()}
    unresolved = {
        ticker for ticker, identity in identities.items() if identity.warmup_predecessor is not None
    }
    copied_by_symbol: dict[str, int] = {}
    while unresolved:
        progress = False
        for ticker in sorted(unresolved):
            identity = identities[ticker]
            predecessor = identity.warmup_predecessor
            if predecessor in unresolved:
                continue
            if predecessor is None:
                raise ValueError("reviewed warmup dependency is missing")
            copied = 0
            for trade_date, values in augmented[predecessor].items():
                if trade_date < identity.admitted_start:
                    existing = augmented[ticker].setdefault(trade_date, values)
                    if existing != values:
                        raise ValueError("reviewed predecessor warmup conflicts with admitted rows")
                    copied += 1
            if copied == 0:
                raise ValueError(f"reviewed predecessor {predecessor} supplies no warmup for {ticker}")
            copied_by_symbol[ticker] = copied
            unresolved.remove(ticker)
            progress = True
        if not progress:
            raise ValueError("reviewed price-identity warmup dependencies contain a cycle")
    created = False
    try:
        with output_path.open("x", encoding="utf-8", newline="") as output:
            created = True
            writer = csv.writer(output, lineterminator="\n")
            writer.writerow(_PRICE_COLUMNS)
            for trade_date, ticker, values in sorted(
                (
                    (trade_date, ticker, values)
                    for ticker, ticker_rows in augmented.items()
                    for trade_date, values in ticker_rows.items()
                ),
                key=lambda item: (item[0], item[1]),
            ):
                writer.writerow(
                    (trade_date.isoformat(), ticker, *(repr(value) for value in values))
                )
    except Exception:
        if created:
            output_path.unlink(missing_ok=True)
        raise
    return {
        "validated_successor_count": len(audits),
        "copied_warmup_row_count": sum(copied_by_symbol.values()),
        "copied_warmup_rows_by_symbol": copied_by_symbol,
        "successor_audits": audits,
    }


def _clip_cache_to_admitted_identities(
    source_path: Path,
    identities: dict[str, PriceIdentity],
    output_path: Path,
) -> dict[str, int]:
    kept = 0
    discarded = 0
    created = False
    try:
        with (
            source_path.open("r", encoding="utf-8", newline="") as source,
            output_path.open("x", encoding="utf-8", newline="") as output,
        ):
            created = True
            reader = csv.DictReader(source)
            if tuple(reader.fieldnames or ()) != _PRICE_COLUMNS:
                raise ValueError("cache identity clipping source has an unexpected header")
            writer = csv.DictWriter(output, fieldnames=_PRICE_COLUMNS, lineterminator="\n")
            writer.writeheader()
            for row in reader:
                identity = identities.get(row["ticker"])
                if identity is None:
                    raise ValueError("cache row lacks a reviewed price identity")
                trade_date = date.fromisoformat(row["trade_date"])
                if identity.admitted_start <= trade_date <= identity.admitted_end:
                    writer.writerow(row)
                    kept += 1
                else:
                    discarded += 1
    except Exception:
        if created:
            output_path.unlink(missing_ok=True)
        raise
    return {"kept_row_count": kept, "discarded_row_count": discarded}


def _normalize_cache_to_cutoff_basis(
    cache_path: Path,
    split_path: Path,
    cutoff_path: Path,
    cutoff_factors: dict[str, float],
    output_path: Path,
    identities: dict[str, PriceIdentity] | None = None,
) -> dict[str, object]:
    """Classify each frozen-cache symbol basis and normalize lookahead splits away."""
    split_audit = _audit_price_source_overlaps(cache_path, split_path)
    cutoff_audit = _audit_price_source_overlaps(cache_path, cutoff_path)
    split_incompatible = set(split_audit.incompatible_symbols())
    cutoff_incompatible = set(cutoff_audit.incompatible_symbols())
    cache_symbols = set(split_audit.symbol_totals).union(cutoff_audit.symbol_totals)
    classification: dict[str, str] = {}
    cache_factors: dict[str, float] = {}
    for ticker in sorted(cutoff_factors):
        if ticker not in cache_symbols:
            classification[ticker] = "no_cache_overlap"
            continue
        if ticker not in cutoff_incompatible:
            classification[ticker] = "already_cutoff_aligned"
            cache_factors[ticker] = 1.0
        elif ticker not in split_incompatible:
            factor = cutoff_factors[ticker]
            if factor == 1.0:
                raise ValueError(f"cache basis classification is contradictory for {ticker}")
            classification[ticker] = "current_split_transformed_to_cutoff"
            cache_factors[ticker] = factor
        else:
            raise ValueError(f"cache matches neither cutoff nor current SPLIT basis for {ticker}")
    created = False
    try:
        with (
            cache_path.open("r", encoding="utf-8", newline="") as source,
            output_path.open("x", encoding="utf-8", newline="") as output,
        ):
            created = True
            reader = csv.DictReader(source)
            if tuple(reader.fieldnames or ()) != _PRICE_COLUMNS:
                raise ValueError("cache normalization source has an unexpected header")
            writer = csv.writer(output, lineterminator="\n")
            writer.writerow(_PRICE_COLUMNS)
            for row in reader:
                ticker = row["ticker"]
                trade_date = date.fromisoformat(row["trade_date"])
                identity = identities.get(ticker) if identities is not None else None
                if identities is not None and identity is None:
                    raise ValueError(f"cache row lacks a canonical price-identity contract for {ticker}")
                if identity is not None and (
                    trade_date > identity.admitted_end
                    or identity.continuity_kind == "successor_reset"
                    and trade_date < identity.admitted_start
                ):
                    continue
                factor = cache_factors.get(ticker)
                if factor is None:
                    raise ValueError(f"cache row lacks a proved cutoff-basis classification for {ticker}")
                values = tuple(float(row[name]) for name in _PRICE_COLUMNS[2:])
                adjusted = (
                    *(value * factor for value in values[:4]),
                    values[4] / factor,
                )
                writer.writerow((row["trade_date"], ticker, *(repr(value) for value in adjusted)))
    except Exception:
        if created:
            output_path.unlink(missing_ok=True)
        raise
    normalized_audit = _audit_price_source_overlaps(output_path, cutoff_path)
    normalized_summary = normalized_audit.finish()
    counts = {
        basis: sum(value == basis for value in classification.values())
        for basis in (
            "already_cutoff_aligned",
            "current_split_transformed_to_cutoff",
            "no_cache_overlap",
        )
    }
    return {
        "cache_basis_by_symbol": classification,
        "cache_basis_counts": counts,
        "cache_vs_current_split_overlap_audit": split_audit.finish(fail_closed=False),
        "cache_vs_cutoff_overlap_audit_before_normalization": cutoff_audit.finish(fail_closed=False),
        "normalized_cache_vs_cutoff_overlap_audit": normalized_summary,
    }


def _merge_price_sources(cache_path: Path, sip_path: Path, output_path: Path) -> dict[str, object]:
    """Stream two sorted price CSVs, retaining the cache row on every overlap."""
    cache_count = 0
    sip_count = 0
    sip_fill_count = 0
    merged_count = 0
    output_created = False
    audit = _OverlapAudit()
    try:
        with (
            cache_path.open("r", encoding="utf-8", newline="") as cache_stream,
            sip_path.open("r", encoding="utf-8", newline="") as sip_stream,
            output_path.open("x", encoding="utf-8", newline="") as output_stream,
        ):
            output_created = True
            cache_reader = csv.DictReader(cache_stream)
            sip_reader = csv.DictReader(sip_stream)
            if tuple(cache_reader.fieldnames or ()) != _PRICE_COLUMNS:
                raise ValueError("cache price CSV has an unexpected merge header")
            if tuple(sip_reader.fieldnames or ()) != _PRICE_COLUMNS:
                raise ValueError("Alpaca SIP snapshot has an unexpected merge header")
            writer = csv.DictWriter(output_stream, fieldnames=_PRICE_COLUMNS, lineterminator="\n")
            writer.writeheader()
            cache_row = next(cache_reader, None)
            sip_row = next(sip_reader, None)
            while cache_row is not None or sip_row is not None:
                cache_key = (
                    (cache_row["trade_date"], cache_row["ticker"])
                    if cache_row is not None
                    else None
                )
                sip_key = (
                    (sip_row["trade_date"], sip_row["ticker"])
                    if sip_row is not None
                    else None
                )
                if sip_key is None or cache_key is not None and cache_key < sip_key:
                    writer.writerow(cache_row)
                    cache_count += 1
                    merged_count += 1
                    cache_row = next(cache_reader, None)
                elif cache_key is None or sip_key < cache_key:
                    writer.writerow(sip_row)
                    sip_count += 1
                    sip_fill_count += 1
                    merged_count += 1
                    sip_row = next(sip_reader, None)
                else:
                    cache_values = tuple(float(cache_row[name]) for name in _PRICE_COLUMNS[2:])
                    sip_values = tuple(float(sip_row[name]) for name in _PRICE_COLUMNS[2:])
                    audit.add(cache_row["ticker"], cache_values, sip_values)
                    writer.writerow(cache_row)
                    cache_count += 1
                    sip_count += 1
                    merged_count += 1
                    cache_row = next(cache_reader, None)
                    sip_row = next(sip_reader, None)
    except Exception:
        if output_created:
            output_path.unlink(missing_ok=True)
        raise
    return {
        "cache_source_row_count": cache_count,
        "alpaca_sip_source_row_count": sip_count,
        "alpaca_sip_fill_row_count": sip_fill_count,
        "merged_row_count": merged_count,
        "overlap_audit": audit.finish(),
    }


def _csv_text(rows: Sequence[Sequence[object]], header: Sequence[str]) -> str:
    from io import StringIO

    stream = StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return stream.getvalue()


def _file_identity(path: Path) -> tuple[int, int, int]:
    info = path.lstat()
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _unlink_owned(path: Path, identity: tuple[int, int, int] | None) -> None:
    if identity is None:
        return
    try:
        if _file_identity(path) == identity:
            path.unlink()
    except FileNotFoundError:
        pass


def _write_staged(path: Path, write: Callable[[BinaryIO], None]) -> tuple[int, int, int]:
    identity: tuple[int, int, int] | None = None
    try:
        with path.open("xb") as stream:
            info = os.fstat(stream.fileno())
            identity = (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))
            write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        return identity
    except Exception:
        _unlink_owned(path, identity)
        raise


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish(
    output_dir: Path,
    source_prices: Path,
    spy_days: Sequence[date],
    provenance: dict[str, object],
    *,
    alpaca_snapshot: Path | None = None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_names = _BACKFILL_OUTPUT_NAMES if alpaca_snapshot is not None else _OUTPUT_NAMES
    finals = tuple(output_dir / name for name in output_names)
    staging = tuple(
        final.with_name(f".{final.name}.{uuid.uuid4().hex}.tmp") for final in finals
    )
    if any(path.exists() or path.is_symlink() for path in finals):
        raise ValueError("refusing to overwrite an existing price export artifact")
    staged_identities: dict[Path, tuple[int, int, int]] = {}
    installed_identities: dict[Path, tuple[int, int, int]] = {}
    success = False
    try:
        def copy_prices(stream: BinaryIO) -> None:
            with source_prices.open("rb") as reader:
                shutil.copyfileobj(reader, stream, length=1024 * 1024)

        staged_identities[staging[0]] = _write_staged(staging[0], copy_prices)
        spy_bytes = _csv_text(
            [(value.isoformat(),) for value in spy_days], ("trade_date",)
        ).encode()
        staged_identities[staging[1]] = _write_staged(
            staging[1], lambda stream: stream.write(spy_bytes)
        )
        provenance_bytes = (json.dumps(provenance, indent=2, sort_keys=True) + "\n").encode()
        staged_identities[staging[2]] = _write_staged(
            staging[2], lambda stream: stream.write(provenance_bytes)
        )
        if alpaca_snapshot is not None:
            def copy_snapshot(stream: BinaryIO) -> None:
                with alpaca_snapshot.open("rb") as reader:
                    shutil.copyfileobj(reader, stream, length=1024 * 1024)

            staged_identities[staging[3]] = _write_staged(staging[3], copy_snapshot)
        for staged, final in zip(staging, finals, strict=True):
            os.link(staged, final)
            installed_identities[final] = staged_identities[staged]
            installed = _file_identity(final)
            if installed != staged_identities[staged]:
                raise RuntimeError("published artifact identity changed during installation")
        _fsync_directory(output_dir)
        success = True
    finally:
        if not success:
            for final in reversed(finals):
                _unlink_owned(final, installed_identities.get(final))
        for staged in staging:
            _unlink_owned(staged, staged_identities.get(staged))
        _fsync_directory(output_dir)


def export(args: argparse.Namespace) -> dict[str, object]:
    """Run the validated, confined cache export and return its provenance."""
    if args.start_date != _BASELINE_START or args.end_date != _BASELINE_END:
        raise ValueError("price window must be exactly 2020-01-01 through 2025-12-31")
    membership_path = _regular_file(Path(args.membership_csv), "membership CSV")
    membership = _load_membership(membership_path)
    alpaca_sip_backfill = bool(getattr(args, "alpaca_sip_backfill", False))
    alpaca_env_file_value = getattr(args, "alpaca_env_file", None)
    if not alpaca_sip_backfill and alpaca_env_file_value is not None:
        raise ValueError("--alpaca-env-file requires explicit --alpaca-sip-backfill")
    if alpaca_sip_backfill:
        identity_bounds = _load_identity_bounds(
            Path(args.symbol_history_map),
            args.symbol_history_map_sha256,
            membership,
            args.end_date,
        )
        price_identity_manifest = _load_price_identity_manifest(
            Path(args.price_identity_map),
            args.price_identity_map_sha256,
            membership,
            identity_bounds,
            args.end_date,
        )
        price_identities = _complete_price_identities(
            membership,
            price_identity_manifest,
            args.end_date,
        )
        provider_identities = _provider_identity_contracts(price_identities)
        identity_end_values = {
            ticker: identity_end.isoformat()
            for ticker, identity_end in sorted(identity_bounds.end_dates.items())
        }
        identity_end_bytes = (
            json.dumps(identity_end_values, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        identity_request_values = {
            ticker: {
                "provider_symbol": identity.provider_symbol,
                "identity_asof": identity.identity_asof.isoformat(),
                "admitted_start": identity.admitted_start.isoformat(),
                "admitted_end": identity.admitted_end.isoformat(),
                "chain_id": identity.chain_id,
                "continuity_kind": identity.continuity_kind,
                "warmup_predecessor": identity.warmup_predecessor,
                "factor_anchor": identity.factor_anchor,
            }
            for ticker, identity in sorted(price_identities.items())
        }
        identity_request_bytes = (
            json.dumps(identity_request_values, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
    source = _regular_file(Path(args.cache), "cache")
    source_before = source.stat()
    worker_script = _regular_file(Path(args.worker_script), "worker script")
    output_dir = Path(args.output_dir).resolve()
    with tempfile.TemporaryDirectory(prefix="pit-price-export-") as temporary:
        root = Path(temporary)
        worker_output = root / "output"
        worker_output.mkdir()
        snapshot = _copy_and_validate_cache(source, args.cache_sha256, root)
        request_path = root / "request.json"
        _canonical_request(request_path, membership, args.start_date, args.end_date)
        prepared_worker = _prepare_container_access(
            root, worker_script, request_path, snapshot.path, worker_output
        )
        prices_path = _run_worker(
            Path(args.docker_executable), args.sandbox_image, prepared_worker,
            snapshot, request_path, worker_output,
        )
        cache_metrics, _cache_spy_days = _validate_prices(
            prices_path,
            membership,
            args.start_date,
            args.end_date,
            enforce_gates=not alpaca_sip_backfill,
        )
        publication_prices = prices_path
        alpaca_snapshot_path: Path | None = None
        merge_metrics: dict[str, object] = {}
        provider_provenance: dict[str, object] = {}
        if alpaca_sip_backfill:
            env_path = Path(alpaca_env_file_value) if alpaca_env_file_value is not None else None
            api_key, secret_key = load_alpaca_credentials(env_path)
            provider_symbols = tuple(sorted({*membership.tickers, "SPY"}))
            expected_trading_days = _expected_trading_days(args.start_date, args.end_date)
            split_snapshot = fetch_alpaca_sip_snapshot(
                provider_symbols,
                membership_symbol_count=len(membership.tickers),
                start=args.start_date,
                end=args.end_date,
                expected_trading_days=expected_trading_days,
                output_path=root / "alpaca_sip_split.csv",
                api_key=api_key,
                secret_key=secret_key,
                identities=provider_identities,
            )
            factor_anchor_symbols = tuple(
                sorted(
                    ticker
                    for ticker, identity in price_identities.items()
                    if identity.factor_anchor
                )
            )
            raw_snapshot = fetch_alpaca_sip_raw_calibration(
                factor_anchor_symbols,
                membership_symbol_count=len(factor_anchor_symbols) - 1,
                start=args.start_date,
                end=args.end_date,
                expected_trading_days=expected_trading_days,
                output_path=root / "alpaca_sip_raw.csv",
                api_key=api_key,
                secret_key=secret_key,
                identities={
                    ticker: provider_identities[ticker] for ticker in factor_anchor_symbols
                },
            )
            anchor_factors = _derive_cutoff_split_factors(
                split_snapshot.path,
                raw_snapshot.path,
                factor_anchor_symbols,
            )
            cutoff_factors = _expand_chain_cutoff_factors(
                anchor_factors,
                price_identities,
                args.end_date,
            )
            factor_bytes = (
                json.dumps(cutoff_factors, allow_nan=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode()
            admitted_snapshot_path = root / "alpaca_sip_cutoff_admitted.csv"
            _apply_cutoff_split_factors(
                split_snapshot.path,
                cutoff_factors,
                admitted_snapshot_path,
            )
            alpaca_snapshot_path = root / "alpaca_sip_snapshot.csv"
            warmup_continuity = _build_price_identity_warmup(
                admitted_snapshot_path,
                price_identities,
                alpaca_snapshot_path,
            )
            admitted_cache_path = root / "admitted_cache.csv"
            cache_identity_clipping = _clip_cache_to_admitted_identities(
                prices_path,
                price_identities,
                admitted_cache_path,
            )
            normalized_cache_path = root / "cutoff_normalized_cache.csv"
            cache_basis = _normalize_cache_to_cutoff_basis(
                admitted_cache_path,
                split_snapshot.path,
                alpaca_snapshot_path,
                cutoff_factors,
                normalized_cache_path,
            )
            cache_basis_bytes = (
                json.dumps(
                    cache_basis["cache_basis_by_symbol"],
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
            publication_prices = root / "merged_prices.csv"
            merge_metrics = _merge_price_sources(
                normalized_cache_path,
                alpaca_snapshot_path,
                publication_prices,
            )
            metrics, spy_days = _validate_prices(
                publication_prices,
                membership,
                args.start_date,
                args.end_date,
            )
            member_pair_fill_count = int(metrics["covered_member_trading_day_pairs"]) - int(
                cache_metrics["covered_member_trading_day_pairs"]
            )
            provider_provenance = {
                "alpaca_retrieved_at_utc": split_snapshot.retrieved_at_utc,
                "alpaca_request_start_date": args.start_date.isoformat(),
                "alpaca_request_end_date": args.end_date.isoformat(),
                "alpaca_feed": "SIP",
                "alpaca_adjustment": "SPLIT",
                "alpaca_timeframe": "1Day",
                "alpaca_requested_symbol_count": split_snapshot.requested_symbol_count,
                "alpaca_requested_membership_symbol_count": (
                    split_snapshot.requested_membership_symbol_count
                ),
                "alpaca_returned_symbol_count": split_snapshot.returned_symbol_count,
                "alpaca_returned_membership_symbol_count": (
                    split_snapshot.returned_membership_symbol_count
                ),
                "alpaca_chunk_count": split_snapshot.chunk_count,
                "alpaca_alias_map": {
                    canonical: provider
                    for provider, canonical in sorted(REQUEST_ALIASES.items())
                },
                "symbol_history_map_sha256": identity_bounds.sha256,
                "symbol_history_mapped_symbol_count": identity_bounds.mapped_symbol_count,
                "price_identity_map_sha256": price_identity_manifest.sha256,
                "price_identity_mapped_symbol_count": len(price_identity_manifest.identities),
                "price_identity_chain_count": price_identity_manifest.chain_count,
                "price_identity_request_contracts": identity_request_values,
                "price_identity_request_contracts_sha256": hashlib.sha256(
                    identity_request_bytes
                ).hexdigest(),
                "alpaca_identity_end_by_symbol": identity_end_values,
                "alpaca_identity_end_sha256": hashlib.sha256(identity_end_bytes).hexdigest(),
                "alpaca_identity_group_count": split_snapshot.identity_group_count,
                "alpaca_raw_identity_group_count": raw_snapshot.identity_group_count,
                "successor_identity_caveat": (
                    "PSKY returned no pre-2025-08-07 predecessor rows in the reviewed identity probe; "
                    "no predecessor synthesis or price alias is applied"
                ),
                "alpaca_split_source_row_count": split_snapshot.row_count,
                "alpaca_raw_calibration_retrieved_at_utc": raw_snapshot.retrieved_at_utc,
                "alpaca_raw_calibration_adjustment": "RAW",
                "alpaca_raw_calibration_chunk_count": raw_snapshot.chunk_count,
                "alpaca_raw_calibration_request_count": raw_snapshot.chunk_count,
                "alpaca_raw_calibration_row_count": raw_snapshot.row_count,
                "alpaca_raw_factor_anchor_symbols": list(factor_anchor_symbols),
                "alpaca_raw_factor_anchor_count": len(factor_anchor_symbols),
                "cutoff_adjustment_date": args.end_date.isoformat(),
                "cutoff_factor_tail_size": 20,
                "cutoff_factor_minimum_matching_days": 5,
                "cutoff_factor_count": len(cutoff_factors),
                "cutoff_factor_anchor_factors": anchor_factors,
                "cutoff_factor_non_unity_symbols": sorted(
                    ticker for ticker, factor in cutoff_factors.items() if factor != 1.0
                ),
                "cutoff_factors": cutoff_factors,
                "cutoff_factors_sha256": hashlib.sha256(factor_bytes).hexdigest(),
                "price_identity_warmup_validation": warmup_continuity,
                "cache_identity_clipping": cache_identity_clipping,
                "cache_basis_by_symbol": cache_basis["cache_basis_by_symbol"],
                "cache_basis_counts": cache_basis["cache_basis_counts"],
                "cache_basis_sha256": hashlib.sha256(cache_basis_bytes).hexdigest(),
                "cutoff_normalized_cache_sha256": _sha256_file(normalized_cache_path),
                "cache_basis_audits": {
                    "current_split": cache_basis["cache_vs_current_split_overlap_audit"],
                    "cutoff_before_normalization": cache_basis[
                        "cache_vs_cutoff_overlap_audit_before_normalization"
                    ],
                    "cutoff_after_normalization": cache_basis[
                        "normalized_cache_vs_cutoff_overlap_audit"
                    ],
                },
                "alpaca_sip_snapshot_sha256": _sha256_file(alpaca_snapshot_path),
                "member_pair_fill_count": member_pair_fill_count,
                "remaining_member_pair_gap_count": int(metrics["member_trading_day_pairs"])
                - int(metrics["covered_member_trading_day_pairs"]),
                **merge_metrics,
            }
        else:
            metrics, spy_days = cache_metrics, _cache_spy_days
        source_after = source.stat()
        if (
            (source_before.st_dev, source_before.st_ino, source_before.st_size, source_before.st_mtime_ns)
            != (source_after.st_dev, source_after.st_ino, source_after.st_size, source_after.st_mtime_ns)
            or _sha256_file(source) != snapshot.sha256
        ):
            raise ValueError("source cache changed during the confined export")
        provenance: dict[str, object] = {
            "source_kind": (
                "existing_hash_pinned_cache_plus_alpaca_sip_snapshot"
                if alpaca_sip_backfill
                else "existing_hash_pinned_cache"
            ),
            "source_sha256": snapshot.sha256,
            "cache_key_count": snapshot.key_count,
            "cache_keys_sha256": snapshot.keys_sha256,
            "membership_sha256": _sha256_file(membership_path),
            "start_date": args.start_date.isoformat(),
            "end_date": args.end_date.isoformat(),
            "sandbox_image": args.sandbox_image,
            "prices_sha256": _sha256_file(publication_prices),
            **metrics,
            **provider_provenance,
        }
        spy_text = _csv_text([(value.isoformat(),) for value in spy_days], ("trade_date",))
        provenance["spy_trading_days_sha256"] = hashlib.sha256(spy_text.encode()).hexdigest()
        _publish(
            output_dir,
            publication_prices,
            spy_days,
            provenance,
            alpaca_snapshot=alpaca_snapshot_path,
        )
        return provenance


def _default_docker() -> str:
    value = shutil.which("docker")
    return str(Path(value).resolve()) if value else "docker"


def main() -> int:
    parser = argparse.ArgumentParser(description="Export hash-pinned PIT OHLCV through an offline worker")
    parser.add_argument("--cache", required=True)
    parser.add_argument("--cache-sha256", required=True)
    parser.add_argument("--membership-csv", required=True)
    parser.add_argument(
        "--symbol-history-map",
        default=str(Path(__file__).resolve().parent / "config" / "pit_membership_symbol_map.csv"),
    )
    parser.add_argument(
        "--symbol-history-map-sha256",
        default=_REVIEWED_SYMBOL_HISTORY_SHA256,
    )
    parser.add_argument(
        "--price-identity-map",
        default=str(Path(__file__).resolve().parent / "config" / "pit_price_identity_map.csv"),
    )
    parser.add_argument(
        "--price-identity-map-sha256",
        default=_REVIEWED_PRICE_IDENTITY_SHA256,
    )
    parser.add_argument("--start-date", type=_iso_date, default=_BASELINE_START)
    parser.add_argument("--end-date", type=_iso_date, default=_BASELINE_END)
    parser.add_argument("--output-dir", default="exports/pit")
    parser.add_argument("--docker-executable", default=_default_docker())
    parser.add_argument("--sandbox-image", required=True)
    parser.add_argument(
        "--alpaca-sip-backfill",
        action="store_true",
        help="explicitly fill cache-missing rows from an Alpaca SIP/SPLIT/1Day snapshot",
    )
    parser.add_argument(
        "--alpaca-env-file",
        help="optional dotenv file containing ALPACA_API_KEY and ALPACA_SECRET_KEY",
    )
    parser.add_argument(
        "--worker-script",
        default=str(Path(__file__).resolve().parent / "tools" / "export_price_cache_worker.py"),
    )
    args = parser.parse_args()
    try:
        provenance = export(args)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
    print(json.dumps(provenance, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
