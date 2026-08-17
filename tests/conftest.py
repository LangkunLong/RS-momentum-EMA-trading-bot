"""Shared pytest fixtures for the CANSLIM trading bot test suite."""

from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

import pytest


_TEST_TMP_ROOT = Path(__file__).resolve().parents[1] / ".artifacts" / "pytest" / "tmp_test_roots"


@pytest.fixture()
def tmp_path() -> Path:
    """Provide a repo-local temporary directory.

    Pytest's built-in ``tmp_path`` has been flaky on this Windows environment
    during session cleanup, causing false-negative test failures unrelated to
    application logic. This fixture keeps temp I/O inside the repo workspace
    and performs best-effort cleanup per test.
    """
    _TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = _TEST_TMP_ROOT / f"test_{uuid4().hex}"
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
