"""Focused trust-boundary tests for the confined PIT price exporter."""

from __future__ import annotations

import os
import stat
import subprocess
from copy import deepcopy
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
import pandas as pd

import export_pit_prices as exporter
from core.alpaca_pit_backfill import (
    PRICE_COLUMNS,
    ProviderIdentity,
    REQUEST_ALIASES,
    _apply_cutoff_split_factors,
    _derive_cutoff_split_factors,
    _request_symbols,
    _response_rows,
)


def _provenance() -> dict[str, object]:
    return {"source_kind": "existing_hash_pinned_cache"}


def test_cache_only_path_does_not_load_backfill_identity_manifests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    membership = tmp_path / "membership.csv"
    membership.write_text("effective_date,ticker,member\n2021-01-01,AAPL,1\n", encoding="utf-8")
    monkeypatch.setattr(
        exporter,
        "_load_identity_bounds",
        lambda *_args: (_ for _ in ()).throw(AssertionError("backfill manifest loaded")),
    )
    args = SimpleNamespace(
        start_date=date(2020, 1, 1),
        end_date=date(2025, 12, 31),
        membership_csv=membership,
        symbol_history_map=tmp_path / "missing-history.csv",
        symbol_history_map_sha256="0" * 64,
        price_identity_map=tmp_path / "missing-price-map.csv",
        price_identity_map_sha256="0" * 64,
        cache=tmp_path / "missing-cache.sqlite3",
        cache_sha256="0" * 64,
        worker_script=tmp_path / "missing-worker.py",
        output_dir=tmp_path / "output",
        alpaca_sip_backfill=False,
        alpaca_env_file=None,
    )

    with pytest.raises(FileNotFoundError):
        exporter.export(args)


def test_alpaca_class_share_aliases_use_provider_dots_and_restore_canonical_hyphens() -> None:
    """Break caught: canonical hyphen class shares were sent to Alpaca and rejected with HTTP 400."""
    requested, provider_to_canonical = _request_symbols(("BF-B", "BRK-B"))

    assert REQUEST_ALIASES == {"BF.B": "BF-B", "BRK.B": "BRK-B"}
    assert requested == ("BF.B", "BRK.B")
    assert provider_to_canonical == {"BF.B": "BF-B", "BRK.B": "BRK-B"}


def test_cutoff_factor_undoes_only_post_cutoff_split_adjustment(tmp_path: Path) -> None:
    """Break caught: a 2026 split created a 4x discontinuity in the 2020-25 normalized snapshot."""
    split_path = tmp_path / "split.csv"
    raw_path = tmp_path / "raw.csv"
    normalized_path = tmp_path / "normalized.csv"
    dates = [f"2025-12-{day:02d}" for day in range(1, 21)]

    def write_snapshot(path: Path, *, crwd_price: float, crwd_volume: float) -> None:
        with path.open("x", encoding="utf-8", newline="") as stream:
            writer = exporter.csv.writer(stream, lineterminator="\n")
            writer.writerow(PRICE_COLUMNS)
            for trade_date in dates:
                writer.writerow((trade_date, "AAPL", 100, 101, 99, 100.5, 1_000))
                writer.writerow(
                    (
                        trade_date,
                        "CRWD",
                        crwd_price,
                        crwd_price * 1.04,
                        crwd_price * 0.96,
                        crwd_price,
                        crwd_volume,
                    )
                )

    write_snapshot(split_path, crwd_price=25, crwd_volume=400)
    write_snapshot(raw_path, crwd_price=100, crwd_volume=100)

    factors = _derive_cutoff_split_factors(split_path, raw_path, ("AAPL", "CRWD"))
    _apply_cutoff_split_factors(split_path, factors, normalized_path)

    assert factors == {"AAPL": 1.0, "CRWD": 4.0}
    with normalized_path.open("r", encoding="utf-8", newline="") as stream:
        rows = list(exporter.csv.DictReader(stream))
    crwd = next(row for row in rows if row["ticker"] == "CRWD")
    assert float(crwd["open"]) == 100.0
    assert float(crwd["close"]) == 100.0
    assert float(crwd["volume"]) == 100.0


def test_cutoff_factor_uses_stable_suffix_after_now_in_window_split(tmp_path: Path) -> None:
    """Break caught: NOW's December 2025 split made a fixed 20-session calibration tail unstable."""
    split_path = tmp_path / "split.csv"
    raw_path = tmp_path / "raw.csv"
    dates = [f"2025-12-{day:02d}" for day in range(1, 21)]
    with (
        split_path.open("x", encoding="utf-8", newline="") as split_stream,
        raw_path.open("x", encoding="utf-8", newline="") as raw_stream,
    ):
        split_writer = exporter.csv.writer(split_stream, lineterminator="\n")
        raw_writer = exporter.csv.writer(raw_stream, lineterminator="\n")
        split_writer.writerow(PRICE_COLUMNS)
        raw_writer.writerow(PRICE_COLUMNS)
        for index, trade_date in enumerate(dates):
            split_price, split_volume = (20, 500) if index < 10 else (100, 100)
            split_writer.writerow(
                (trade_date, "NOW", split_price, split_price, split_price, split_price, split_volume)
            )
            raw_writer.writerow((trade_date, "NOW", 100, 100, 100, 100, 100))

    factors = _derive_cutoff_split_factors(split_path, raw_path, ("NOW",))

    assert factors == {"NOW": 1.0}


def test_cache_basis_normalization_removes_amcr_lookahead_but_preserves_crwd_cutoff_rows(
    tmp_path: Path,
) -> None:
    """Break caught: cache rows retained mixed post-cutoff split lookahead by symbol."""
    cache_path = tmp_path / "cache.csv"
    split_path = tmp_path / "split.csv"
    cutoff_path = tmp_path / "cutoff.csv"
    normalized_cache_path = tmp_path / "normalized-cache.csv"
    dates = [f"2025-12-{day:02d}" for day in range(1, 21)]

    def write_rows(path: Path, rows_by_symbol: dict[str, tuple[float, float]]) -> None:
        with path.open("x", encoding="utf-8", newline="") as stream:
            writer = exporter.csv.writer(stream, lineterminator="\n")
            writer.writerow(PRICE_COLUMNS)
            for trade_date in dates:
                for ticker, (price, volume) in sorted(rows_by_symbol.items()):
                    writer.writerow((trade_date, ticker, price, price, price, price, volume))

    write_rows(cache_path, {"AMCR": (100, 20), "CRWD": (100, 100)})
    write_rows(split_path, {"AMCR": (100, 20), "CRWD": (25, 400)})
    write_rows(cutoff_path, {"AMCR": (20, 100), "CRWD": (100, 100)})

    result = exporter._normalize_cache_to_cutoff_basis(
        cache_path,
        split_path,
        cutoff_path,
        {"AMCR": 0.2, "CRWD": 4.0},
        normalized_cache_path,
    )

    assert result["cache_basis_by_symbol"] == {
        "AMCR": "current_split_transformed_to_cutoff",
        "CRWD": "already_cutoff_aligned",
    }
    with normalized_cache_path.open("r", encoding="utf-8", newline="") as stream:
        rows = list(exporter.csv.DictReader(stream))
    amcr = next(row for row in rows if row["ticker"] == "AMCR")
    crwd = next(row for row in rows if row["ticker"] == "CRWD")
    assert (float(amcr["close"]), float(amcr["volume"])) == (20.0, 100.0)
    assert (float(crwd["close"]), float(crwd["volume"])) == (100.0, 100.0)


def test_provider_identity_rows_outside_admitted_interval_are_not_output() -> None:
    """Break caught: FI's reused pre-entry symbol history leaked into Fiserv output."""
    index = pd.MultiIndex.from_tuples(
        [
            ("BLL", pd.Timestamp("2022-05-09", tz="UTC")),
            ("BLL", pd.Timestamp("2022-05-10", tz="UTC")),
            ("BALL", pd.Timestamp("2022-04-25", tz="UTC")),
            ("BALL", pd.Timestamp("2022-05-10", tz="UTC")),
            ("PARA", pd.Timestamp("2025-08-06", tz="UTC")),
            ("PARA", pd.Timestamp("2025-08-07", tz="UTC")),
            ("PSKY", pd.Timestamp("2025-08-07", tz="UTC")),
        ],
        names=("symbol", "timestamp"),
    )
    frame = pd.DataFrame(
        {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 1_000},
        index=index,
    )
    symbols = ("BALL", "BLL", "PARA", "PSKY")

    rows, returned = _response_rows(
        frame,
        frozenset(symbols),
        {symbol: symbol for symbol in symbols},
        date(2020, 1, 1),
        date(2025, 12, 31),
        frozenset(
            {
                date(2022, 4, 25),
                date(2022, 5, 9),
                date(2022, 5, 10),
                date(2025, 8, 6),
                date(2025, 8, 7),
            }
        ),
        {
            "BLL": ProviderIdentity("BLL", "BLL", date(2022, 5, 9), date(2020, 1, 1), date(2022, 5, 9), True),
            "BALL": ProviderIdentity("BALL", "BALL", date(2025, 12, 31), date(2022, 5, 10), date(2025, 12, 31), True),
            "PARA": ProviderIdentity("PARA", "PARA", date(2025, 8, 6), date(2022, 2, 17), date(2025, 8, 6), True),
            "PSKY": ProviderIdentity("PSKY", "PSKY", date(2025, 12, 31), date(2025, 8, 7), date(2025, 12, 31), True),
        },
    )

    assert returned == set(symbols)
    assert [(row[0], row[1]) for row in rows] == [
        (date(2022, 5, 9), "BLL"),
        (date(2022, 5, 10), "BALL"),
        (date(2025, 8, 6), "PARA"),
        (date(2025, 8, 7), "PSKY"),
    ]


def test_ticker_reuse_warmup_copies_reviewed_predecessor_not_successor_symbol_history(
    tmp_path: Path,
) -> None:
    admitted_path = tmp_path / "admitted.csv"
    output_path = tmp_path / "warmup.csv"
    with admitted_path.open("x", encoding="utf-8", newline="") as stream:
        writer = exporter.csv.writer(stream, lineterminator="\n")
        writer.writerow(PRICE_COLUMNS)
        writer.writerow(("2023-06-05", "FISV", 100, 101, 99, 100, 1_000))
        writer.writerow(("2023-06-07", "FISV", 102, 103, 101, 102, 1_100))
        writer.writerow(("2023-06-07", "FI", 102, 103, 101, 102, 1_100))
    identities = {
        "FISV": exporter.PriceIdentity(
            "FISV", "FISV", date(2025, 12, 31), date(2020, 1, 1),
            date(2025, 12, 31), "fiserv", "same_issuer_ticker_reuse", None, True,
            "https://example.test/fisv",
        ),
        "FI": exporter.PriceIdentity(
            "FI", "FI", date(2025, 11, 10), date(2023, 6, 7),
            date(2025, 11, 10), "fiserv", "same_issuer_ticker_reuse", "FISV", False,
            "https://example.test/fi",
        ),
    }

    metrics = exporter._build_price_identity_warmup(admitted_path, identities, output_path)

    with output_path.open("r", encoding="utf-8", newline="") as stream:
        rows = list(exporter.csv.DictReader(stream))
    fi_rows = [row for row in rows if row["ticker"] == "FI"]
    assert [(row["trade_date"], float(row["close"])) for row in fi_rows] == [
        ("2023-06-05", 100.0),
        ("2023-06-07", 102.0),
    ]
    assert metrics["copied_warmup_row_count"] == 1
    assert metrics["successor_audits"]["FI"]["exact_overlap_row_count"] == 1


def test_chain_factor_uses_terminal_anchor_and_rejects_unproved_pre_cutoff_chain() -> None:
    identities = {
        "BLL": exporter.PriceIdentity("BLL", "BLL", date(2022, 5, 9), date(2020, 1, 1), date(2022, 5, 9), "ball", "same_issuer_rename", None, False, "https://example.test/bll"),
        "BALL": exporter.PriceIdentity("BALL", "BALL", date(2025, 12, 31), date(2022, 5, 10), date(2025, 12, 31), "ball", "same_issuer_rename", "BLL", True, "https://example.test/ball"),
        "VIAC": exporter.PriceIdentity("VIAC", "VIAC", date(2022, 2, 16), date(2020, 1, 1), date(2022, 2, 16), "paramount_legacy", "same_issuer_rename", None, False, "https://example.test/viac"),
        "PARA": exporter.PriceIdentity("PARA", "PARA", date(2025, 8, 6), date(2022, 2, 17), date(2025, 8, 6), "paramount_legacy", "same_issuer_rename", "VIAC", True, "https://example.test/para"),
    }

    factors = exporter._expand_chain_cutoff_factors(
        {"BALL": 4.0, "PARA": 1.0}, identities, date(2025, 12, 31)
    )

    assert factors == {"BALL": 4.0, "BLL": 4.0, "PARA": 1.0, "VIAC": 1.0}
    with pytest.raises(ValueError, match="pre-cutoff chain paramount_legacy"):
        exporter._expand_chain_cutoff_factors(
            {"BALL": 4.0, "PARA": 2.0}, identities, date(2025, 12, 31)
        )


def test_publish_uses_unique_staging_and_cleans_owned_files_on_install_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a destination race left a directly-written partial price bundle."""
    output = tmp_path / "exports"
    source = tmp_path / "worker-prices.csv"
    source.write_bytes(b"trade_date,ticker,open,high,low,close,volume\n")
    real_link = os.link

    def race_on_second_install(source_path: str | bytes, target_path: str | bytes) -> None:
        target = Path(target_path)
        if target.name == "spy_trading_days.csv":
            target.write_bytes(b"racer-owned\n")
        real_link(source_path, target_path)

    monkeypatch.setattr(exporter.os, "link", race_on_second_install)

    with pytest.raises(FileExistsError):
        exporter._publish(output, source, (date(2025, 12, 31),), _provenance())

    assert not (output / "prices.csv").exists()
    assert (output / "spy_trading_days.csv").read_bytes() == b"racer-owned\n"
    assert not (output / "prices_provenance.json").exists()
    assert not tuple(output.glob(".*.tmp"))


def test_publish_installs_fsynced_staging_files_without_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: final files were opened directly and never flushed durably before install."""
    output = tmp_path / "exports"
    source = tmp_path / "worker-prices.csv"
    prices = b"trade_date,ticker,open,high,low,close,volume\n"
    source.write_bytes(prices)
    fsynced: list[int] = []
    real_fsync = os.fsync

    def record_fsync(file_descriptor: int) -> None:
        fsynced.append(file_descriptor)
        real_fsync(file_descriptor)

    monkeypatch.setattr(exporter.os, "fsync", record_fsync)

    exporter._publish(output, source, (date(2025, 12, 31),), _provenance())

    assert (output / "prices.csv").read_bytes() == prices
    assert (output / "spy_trading_days.csv").read_text(encoding="utf-8") == "trade_date\n2025-12-31\n"
    assert '"source_kind": "existing_hash_pinned_cache"' in (
        output / "prices_provenance.json"
    ).read_text(encoding="utf-8")
    assert len(fsynced) >= 3
    assert not tuple(output.glob(".*.tmp"))


def test_publish_registers_final_before_post_link_attestation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a linked final escaped cleanup when its first identity read failed."""
    output = tmp_path / "exports"
    source = tmp_path / "worker-prices.csv"
    source.write_bytes(b"trade_date,ticker,open,high,low,close,volume\n")
    real_identity = exporter._file_identity
    failed_once = False

    def fail_first_final_identity(path: Path) -> tuple[int, int, int]:
        nonlocal failed_once
        if path.name == "prices.csv" and not failed_once:
            failed_once = True
            raise OSError("simulated post-link stat failure")
        return real_identity(path)

    monkeypatch.setattr(exporter, "_file_identity", fail_first_final_identity)

    with pytest.raises(OSError, match="post-link stat failure"):
        exporter._publish(output, source, (date(2025, 12, 31),), _provenance())

    assert not any((output / name).exists() for name in exporter._OUTPUT_NAMES)
    assert not tuple(output.glob(".*.tmp"))


def test_local_image_check_rejects_digest_that_is_not_already_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: Docker create could pull an absent image despite a pinned digest."""
    executable = tmp_path / "docker"
    executable.write_bytes(b"docker")
    image = "registry.invalid/worker@sha256:" + "a" * 64

    def missing_image(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1, "", "No such image")

    monkeypatch.setattr(exporter, "_docker_call", missing_image)

    with pytest.raises(RuntimeError, match="not available locally"):
        exporter._local_image_id(executable, image)


def test_docker_create_policy_forbids_pulls_and_requests_exact_limits(tmp_path: Path) -> None:
    """Break caught: create omitted local-only policy or requested different resource limits."""
    source = tmp_path / "worker.py"
    request = tmp_path / "request.json"
    cache = tmp_path / "cache.sqlite3"
    output = tmp_path / "output"
    for path in (source, request, cache):
        path.write_bytes(b"x")
    output.mkdir()
    mounts = (
        (source, "/worker/export_price_cache_worker.py", True),
        (request, "/input/request.json", True),
        (cache, "/input/cache.sqlite3", True),
        (output, "/output", False),
    )

    args = exporter._docker_create_args(
        "worker-name",
        "owner-token",
        "registry.invalid/worker@sha256:" + "a" * 64,
        mounts,
    )

    assert args[args.index("--pull") + 1] == "never"
    assert args[args.index("--pids-limit") + 1] == "64"
    assert args[args.index("--memory") + 1] == "2147483648b"
    assert args[args.index("--cpus") + 1] == "2"
    assert args[args.index("--tmpfs") + 1] == "/tmp:rw,noexec,nosuid,nodev,size=67108864"


def test_native_posix_access_keeps_root_private_and_grants_only_required_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: UID 65532 could not read owner-only inputs or write the output bind."""
    root = tmp_path / "private"
    root.mkdir()
    worker = tmp_path / "worker.py"
    request = root / "request.json"
    cache = root / "cache.sqlite3"
    output = root / "output"
    worker.write_bytes(b"worker")
    request.write_bytes(b"request")
    cache.write_bytes(b"cache")
    output.mkdir()
    modes: dict[Path, int] = {}

    def record_chmod(path: str | bytes | os.PathLike[str], mode: int) -> None:
        modes[Path(path)] = mode

    monkeypatch.setattr(exporter.os, "chmod", record_chmod)

    prepared = exporter._prepare_container_access(
        root,
        worker,
        request,
        cache,
        output,
        native_posix=True,
    )

    assert prepared.parent == root
    assert prepared.read_bytes() == b"worker"
    assert modes == {
        root: 0o700,
        prepared: 0o444,
        request: 0o444,
        cache: 0o444,
        output: 0o733,
    }


@pytest.mark.skipif(os.name != "posix", reason="requires native POSIX mode bits")
def test_native_posix_access_modes_are_effective_on_disk(tmp_path: Path) -> None:
    """Break caught: requested POSIX modes were not the effective bind-source modes."""
    root = tmp_path / "private"
    root.mkdir()
    worker = tmp_path / "worker.py"
    request = root / "request.json"
    cache = root / "cache.sqlite3"
    output = root / "output"
    worker.write_bytes(b"worker")
    request.write_bytes(b"request")
    cache.write_bytes(b"cache")
    output.mkdir()

    prepared = exporter._prepare_container_access(root, worker, request, cache, output)

    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(prepared.stat().st_mode) == 0o444
    assert stat.S_IMODE(request.stat().st_mode) == 0o444
    assert stat.S_IMODE(cache.stat().st_mode) == 0o444
    assert stat.S_IMODE(output.stat().st_mode) == 0o733


def _inspection(tmp_path: Path) -> tuple[dict[str, object], dict[str, tuple[Path, bool]]]:
    mounts = {
        "/worker/export_price_cache_worker.py": (tmp_path / "worker.py", True),
        "/input/request.json": (tmp_path / "request.json", True),
        "/input/cache.sqlite3": (tmp_path / "cache.sqlite3", True),
        "/output": (tmp_path / "output", False),
    }
    for path, _readonly in mounts.values():
        if path.suffix:
            path.write_bytes(b"x")
        else:
            path.mkdir()
    item: dict[str, object] = {
        "Id": "b" * 64,
        "Name": "/worker-name",
        "Image": "sha256:" + "c" * 64,
        "Config": {
            "Image": "registry.invalid/worker@sha256:" + "a" * 64,
            "Entrypoint": ["python"],
            "Cmd": [
                "/worker/export_price_cache_worker.py", "--request", "/input/request.json",
                "--cache", "/input/cache.sqlite3", "--output", "/output/prices.csv",
            ],
            "User": "65532:65532",
            "WorkingDir": "/worker",
            "Labels": {"pit-price-export.owner": "owner-token"},
        },
        "HostConfig": {
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "PidsLimit": 64,
            "Memory": 2_147_483_648,
            "NanoCpus": 2_000_000_000,
            "Tmpfs": {"/tmp": "nodev,size=64m,rw,nosuid,noexec"},
        },
        "NetworkSettings": {"Networks": {"none": {}}},
        "Mounts": [
            {
                "Destination": destination,
                "Source": str(source.resolve()),
                "RW": not readonly,
            }
            for destination, (source, readonly) in mounts.items()
        ],
    }
    return item, mounts


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("PidsLimit", 65),
        ("PidsLimit", 64.0),
        ("Memory", 2_147_483_647),
        ("NanoCpus", 1_000_000_000),
        ("Tmpfs", {"/tmp": "rw,noexec,nosuid,nodev,size=64m,exec"}),
    ],
)
def test_container_attestation_rejects_resource_limit_drift(
    tmp_path: Path,
    field: str,
    bad_value: object,
) -> None:
    """Break caught: inspected limits could differ from the requested confinement contract."""
    item, mounts = _inspection(tmp_path)
    mutated = deepcopy(item)
    mutated["HostConfig"][field] = bad_value  # type: ignore[index]

    with pytest.raises(RuntimeError, match="confinement differs"):
        exporter._validate_container_item(
            mutated,
            "b" * 64,
            "worker-name",
            "owner-token",
            "registry.invalid/worker@sha256:" + "a" * 64,
            "sha256:" + "c" * 64,
            mounts,
        )


def test_container_attestation_normalizes_exact_tmpfs_size_and_option_order(
    tmp_path: Path,
) -> None:
    """Break caught: harmless Docker tmpfs option reordering was confused with policy drift."""
    item, mounts = _inspection(tmp_path)

    exporter._validate_container_item(
        item,
        "b" * 64,
        "worker-name",
        "owner-token",
        "registry.invalid/worker@sha256:" + "a" * 64,
        "sha256:" + "c" * 64,
        mounts,
    )
