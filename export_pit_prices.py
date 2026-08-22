"""Export a hash-pinned DataFetcher cache through a confined offline worker."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
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
from typing import Sequence
from urllib.parse import quote

_BASELINE_START = date(2020, 1, 1)
_BASELINE_END = date(2025, 12, 31)
_EXPECTED_SCHEMA = [
    (0, "cache_key", "TEXT", 0, None, 1),
    (1, "cache_kind", "TEXT", 1, None, 0),
    (2, "created_at", "TEXT", 1, None, 0),
    (3, "payload", "BLOB", 1, None, 0),
]
_MEMBERSHIP_COLUMNS = ("effective_date", "ticker", "member")
_PRICE_COLUMNS = ("trade_date", "ticker", "open", "high", "low", "close", "volume")
_OUTPUT_NAMES = ("prices.csv", "spy_trading_days.csv", "prices_provenance.json")
_TICKER_RE = re.compile(r"[A-Z0-9][A-Z0-9.-]{0,14}")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_IMAGE_RE = re.compile(r"[^\s]+@sha256:[0-9a-f]{64}")
_COMPLETION = '{"status":"complete","version":1}\n'
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


def _attest_container(
    executable: Path,
    container_id: str,
    name: str,
    ownership: str,
    image: str,
    mounts: dict[str, tuple[Path, bool]],
) -> None:
    inspected = _docker_call(executable, ("inspect", container_id), timeout=30)
    if inspected.returncode != 0:
        raise RuntimeError(f"cannot inspect offline worker: {inspected.stderr.strip()}")
    try:
        item = json.loads(inspected.stdout)[0]
        config = item["Config"]
        host = item["HostConfig"]
        network = item["NetworkSettings"]["Networks"]
    except (json.JSONDecodeError, IndexError, KeyError, TypeError) as exc:
        raise RuntimeError("offline worker inspection is malformed") from exc
    actual_mounts = {
        entry["Destination"]: (Path(entry["Source"]).resolve(), not entry["RW"])
        for entry in item.get("Mounts", [])
    }
    expected_cmd = [
        "/worker/export_price_cache_worker.py", "--request", "/input/request.json",
        "--cache", "/input/cache.sqlite3", "--output", "/output/prices.csv",
    ]
    if (
        item.get("Id") != container_id
        or item.get("Name") != f"/{name}"
        or (config.get("Labels") or {}).get("pit-price-export.owner") != ownership
        or config.get("Image") != image
        or config.get("Entrypoint") != ["python"]
        or config.get("Cmd") != expected_cmd
        or config.get("User") != "65532:65532"
        or config.get("WorkingDir") != "/worker"
        or host.get("NetworkMode") != "none"
        or host.get("ReadonlyRootfs") is not True
        or host.get("CapDrop") != ["ALL"]
        or "no-new-privileges" not in (host.get("SecurityOpt") or [])
        or set(network) != {"none"}
        or actual_mounts != mounts
    ):
        raise RuntimeError("offline worker confinement differs from the exact contract")


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
    name = f"pit-price-export-{uuid.uuid4().hex}"
    ownership = uuid.uuid4().hex
    mounts = {
        "/worker/export_price_cache_worker.py": (worker_script, True),
        "/input/request.json": (request_path.resolve(), True),
        "/input/cache.sqlite3": (snapshot.path.resolve(), True),
        "/output": (output_dir.resolve(), False),
    }
    args = [
        "create", "--name", name, "--label", f"pit-price-export.owner={ownership}",
        "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--pids-limit", "64",
        "--memory", "2g", "--cpus", "2", "--user", "65532:65532",
        "--entrypoint", "python", "--workdir", "/worker",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
    ]
    for source, destination, readonly in (
        (worker_script, "/worker/export_price_cache_worker.py", True),
        (request_path, "/input/request.json", True),
        (snapshot.path, "/input/cache.sqlite3", True),
        (output_dir, "/output", False),
    ):
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
    container_id = ""
    try:
        created = _docker_call(executable, tuple(args), timeout=60)
        container_id = created.stdout.strip()
        if created.returncode != 0 or re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
            raise RuntimeError(f"offline worker creation failed: {created.stderr.strip()}")
        _attest_container(executable, container_id, name, ownership, image, mounts)
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
    if len(entries) != 1 or entries[0].name != "prices.csv" or not entries[0].is_file():
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
) -> tuple[dict[str, object], tuple[date, ...]]:
    requested = {*membership.tickers, "SPY"}
    closes: dict[date, set[str]] = {}
    spy_days: list[date] = []
    previous: tuple[date, str] | None = None
    first_price_date: date | None = None
    last_price_date: date | None = None
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
    }
    if tuple(spy_days) != expected_spy:
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
    if coverage_ratio < 0.98:
        raise ValueError(f"member/trading-day close coverage is below 98%: {coverage_ratio:.6%}")
    return metrics, tuple(spy_days)


def _csv_text(rows: Sequence[Sequence[object]], header: Sequence[str]) -> str:
    from io import StringIO

    stream = StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return stream.getvalue()


def _publish(output_dir: Path, source_prices: Path, spy_days: Sequence[date], provenance: dict[str, object]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    targets = [output_dir / name for name in _OUTPUT_NAMES]
    if any(path.exists() or path.is_symlink() for path in targets):
        raise ValueError("refusing to overwrite an existing price export artifact")
    created: list[Path] = []
    try:
        prices = targets[0]
        with source_prices.open("rb") as reader, prices.open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
        created.append(prices)
        targets[1].write_text(
            _csv_text([(value.isoformat(),) for value in spy_days], ("trade_date",)),
            encoding="utf-8",
            newline="",
        )
        created.append(targets[1])
        targets[2].write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="")
        created.append(targets[2])
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise


def export(args: argparse.Namespace) -> dict[str, object]:
    """Run the validated, confined cache export and return its provenance."""
    if args.start_date != _BASELINE_START or args.end_date != _BASELINE_END:
        raise ValueError("price window must be exactly 2020-01-01 through 2025-12-31")
    membership_path = _regular_file(Path(args.membership_csv), "membership CSV")
    membership = _load_membership(membership_path)
    source = _regular_file(Path(args.cache), "cache")
    source_before = source.stat()
    worker_script = Path(args.worker_script).resolve(strict=True)
    output_dir = Path(args.output_dir).resolve()
    with tempfile.TemporaryDirectory(prefix="pit-price-export-") as temporary:
        root = Path(temporary)
        worker_output = root / "output"
        worker_output.mkdir()
        snapshot = _copy_and_validate_cache(source, args.cache_sha256, root)
        request_path = root / "request.json"
        _canonical_request(request_path, membership, args.start_date, args.end_date)
        prices_path = _run_worker(
            Path(args.docker_executable), args.sandbox_image, worker_script,
            snapshot, request_path, worker_output,
        )
        metrics, spy_days = _validate_prices(prices_path, membership, args.start_date, args.end_date)
        source_after = source.stat()
        if (
            (source_before.st_dev, source_before.st_ino, source_before.st_size, source_before.st_mtime_ns)
            != (source_after.st_dev, source_after.st_ino, source_after.st_size, source_after.st_mtime_ns)
            or _sha256_file(source) != snapshot.sha256
        ):
            raise ValueError("source cache changed during the confined export")
        provenance: dict[str, object] = {
            "source_kind": "existing_hash_pinned_cache",
            "source_sha256": snapshot.sha256,
            "cache_key_count": snapshot.key_count,
            "cache_keys_sha256": snapshot.keys_sha256,
            "membership_sha256": _sha256_file(membership_path),
            "start_date": args.start_date.isoformat(),
            "end_date": args.end_date.isoformat(),
            "sandbox_image": args.sandbox_image,
            "prices_sha256": _sha256_file(prices_path),
            **metrics,
        }
        spy_text = _csv_text([(value.isoformat(),) for value in spy_days], ("trade_date",))
        provenance["spy_trading_days_sha256"] = hashlib.sha256(spy_text.encode()).hexdigest()
        _publish(output_dir, prices_path, spy_days, provenance)
        return provenance


def _default_docker() -> str:
    value = shutil.which("docker")
    return str(Path(value).resolve()) if value else "docker"


def main() -> int:
    parser = argparse.ArgumentParser(description="Export hash-pinned PIT OHLCV through an offline worker")
    parser.add_argument("--cache", required=True)
    parser.add_argument("--cache-sha256", required=True)
    parser.add_argument("--membership-csv", required=True)
    parser.add_argument("--start-date", type=_iso_date, default=_BASELINE_START)
    parser.add_argument("--end-date", type=_iso_date, default=_BASELINE_END)
    parser.add_argument("--output-dir", default="exports/pit")
    parser.add_argument("--docker-executable", default=_default_docker())
    parser.add_argument("--sandbox-image", required=True)
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
