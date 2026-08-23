"""Offline publication guards for the immutable PIT baseline runner."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import pit_baseline


def test_baseline_artifacts_are_hashed_and_never_overwritten(tmp_path: Path) -> None:
    """Break caught: a rerun silently replaced an existing baseline artifact."""
    artifact = tmp_path / "run" / "summary.json"
    artifact.parent.mkdir()
    payload = pit_baseline._json_bytes({"status": "complete", "symbols": ["AAA", "SPY"]})
    pit_baseline._write_bytes(artifact, payload)
    assert pit_baseline.sha256_file(artifact) == hashlib.sha256(payload).hexdigest()
    with pytest.raises(FileExistsError):
        pit_baseline._write_bytes(artifact, payload)
