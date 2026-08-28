"""Crash-safe, incremental local artifacts for the schema-v2 PIT optimizer."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Mapping


def canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    """Return the one canonical UTF-8 representation used by optimizer artifacts."""

    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _write_all(handle: object, payload: bytes) -> None:
    written = handle.write(payload)  # type: ignore[attr-defined]
    if written != len(payload):
        raise OSError("optimizer artifact write was incomplete")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_create_only_bytes(path: Path, payload: bytes) -> tuple[Path, str]:
    """Create and fsync one artifact without permitting an overwrite."""

    target = Path(path)
    if not target.is_absolute() or target.parent.is_symlink() or not target.parent.is_dir():
        raise ValueError("optimizer artifact target is invalid")
    target = target.resolve(strict=False)
    created = False
    try:
        with target.open("xb") as handle:
            created = True
            _write_all(handle, payload)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(target.parent)
    except BaseException:
        if created:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return target, hashlib.sha256(payload).hexdigest()


def write_create_only_json(
    path: Path,
    value: Mapping[str, object],
) -> tuple[Path, str]:
    return write_create_only_bytes(Path(path), canonical_json_bytes(value))


def atomic_replace_bytes(path: Path, payload: bytes) -> tuple[Path, str]:
    """Fsync a sibling temporary file before atomically replacing an artifact."""

    target = Path(path)
    if not target.is_absolute() or target.parent.is_symlink() or not target.parent.is_dir():
        raise ValueError("optimizer artifact replacement target is invalid")
    target = target.resolve(strict=False)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            _write_all(handle, payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return target, hashlib.sha256(payload).hexdigest()


def atomic_replace_json(
    path: Path,
    value: Mapping[str, object],
) -> tuple[Path, str]:
    return atomic_replace_bytes(Path(path), canonical_json_bytes(value))


class IncrementalArtifactStore:
    """Bounded create-only evidence with two explicit replaceable snapshots."""

    _REPLACEABLE_JSON = frozenset({"accounting.json"})
    _REPLACEABLE_DIFF = frozenset({"incumbent.diff"})
    _ROOT_JSON = frozenset(
        {"run.json", "baseline.json", "accounting.json", "holdout.json", "summary.json"}
    )
    _ITERATION_JSON = frozenset(
        {"investigator.json", "author.json", "validation.json", "discovery.json", "critic.json", "decision.json"}
    )

    def __init__(self, root: Path) -> None:
        candidate = Path(root)
        if not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_dir():
            raise ValueError("optimizer artifact root is invalid")
        self._root = candidate.resolve()

    @property
    def root(self) -> Path:
        return self._root

    def _target(self, name: str, *, json_artifact: bool) -> Path:
        if not isinstance(name, str) or "\\" in name:
            raise ValueError("optimizer artifact name is invalid")
        parts = Path(name).parts
        valid = False
        if json_artifact and name in self._ROOT_JSON:
            valid = True
        elif (
            json_artifact
            and len(parts) == 3
            and parts[0] == "iterations"
            and len(parts[1]) == 3
            and parts[1].isdigit()
            and parts[2] in self._ITERATION_JSON
        ):
            valid = True
        elif not json_artifact and (
            name in self._REPLACEABLE_DIFF
            or (
                len(parts) == 3
                and parts[0] == "iterations"
                and len(parts[1]) == 3
                and parts[1].isdigit()
                and parts[2] == "candidate.diff"
            )
        ):
            valid = True
        if not valid:
            raise ValueError("optimizer artifact name is outside the closed layout")
        parent = self._root.joinpath(*parts[:-1])
        parent.mkdir(parents=True, exist_ok=True)
        if parent.is_symlink() or self._root not in (parent, *parent.parents):
            raise ValueError("optimizer artifact parent is invalid")
        return parent / parts[-1]

    def write_json_artifact(
        self,
        name: str,
        value: Mapping[str, object],
    ) -> tuple[Path, str]:
        if not isinstance(value, Mapping) or value.get("schema_version") != 2:
            raise ValueError("optimizer JSON artifact schema is invalid")
        target = self._target(name, json_artifact=True)
        if name in self._REPLACEABLE_JSON and target.exists():
            return atomic_replace_json(target, value)
        return write_create_only_json(target, value)

    def write_diff_artifact(self, name: str, value: str) -> tuple[Path, str]:
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError("optimizer diff artifact is invalid")
        target = self._target(name, json_artifact=False)
        payload = value.encode("utf-8")
        if name in self._REPLACEABLE_DIFF and target.exists():
            return atomic_replace_bytes(target, payload)
        return write_create_only_bytes(target, payload)


PitOptimizerArtifactStore = IncrementalArtifactStore
