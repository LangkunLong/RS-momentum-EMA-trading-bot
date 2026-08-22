"""Focused trust-boundary tests for the confined PIT price exporter."""

from __future__ import annotations

import os
import stat
import subprocess
from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest

import export_pit_prices as exporter


def _provenance() -> dict[str, object]:
    return {"source_kind": "existing_hash_pinned_cache"}


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
