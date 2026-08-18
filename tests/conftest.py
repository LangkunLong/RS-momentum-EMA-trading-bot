"""Shared pytest fixtures for the CANSLIM trading bot test suite."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from uuid import uuid4

import pytest


_TEST_TMP_ROOT = Path(__file__).resolve().parents[1] / ".artifacts" / "pytest" / "tmp_test_roots"


def _configured_test_tmp_root() -> Path:
    override = os.environ.get("AGENT_LOOP_TEST_TMP_ROOT")
    if override is None:
        return _TEST_TMP_ROOT
    configured = Path(override)
    if not configured.is_absolute():
        raise pytest.UsageError("AGENT_LOOP_TEST_TMP_ROOT must be absolute")
    resolved = configured.resolve()
    source_root = Path(__file__).resolve().parents[1]
    if resolved == source_root or resolved.is_relative_to(source_root):
        raise pytest.UsageError("AGENT_LOOP_TEST_TMP_ROOT must be outside source")
    return resolved


@pytest.fixture()
def tmp_path() -> Path:
    """Provide a writable per-test directory outside confined candidate source.

    Pytest's built-in ``tmp_path`` has been flaky on this Windows environment
    during session cleanup, causing false-negative test failures unrelated to
    application logic. Local runs retain the repo-local default; the agent-loop
    controller supplies its isolated writable root to confined workers.
    """
    test_tmp_root = _configured_test_tmp_root()
    test_tmp_root.mkdir(parents=True, exist_ok=True)
    path = test_tmp_root / f"test_{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture()
def mock_opportunity() -> dict:
    """Return a standard CANSLIM opportunity result dict with all required fields.

    This mirrors the structure produced by core/canslim/core.py evaluate_canslim()
    and consumed by enhanced_scanner.export_results_to_csv().
    """
    return {
        "symbol": "AAPL",
        "rs_score": 95.0,
        "total_score": 85.0,
        "scores": {
            "C": 0.8,
            "A": 0.9,
            "N": 1.0,
            "S": 0.7,
            "L": 0.9,
            "I": 0.8,
            "M": 1.0,
        },
        "metrics": {
            "current_growth": 0.30,
            "annual_growth": 0.25,
            "revenue_growth": 0.20,
            "proximity_to_high": 0.99,
            "avg_volume_50": 75_000_000,
            "shares_outstanding": 15_000_000_000,
            "roe": 0.145,
            "s_metrics": {"up_down_volume_ratio": 1.4},
        },
    }
