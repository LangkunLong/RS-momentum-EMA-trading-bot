"""Operational paths must not depend on the process working directory."""

from pathlib import Path

from config import settings
from core import index_ticker_fetcher


def test_project_artifact_paths_are_absolute_and_repo_anchored() -> None:
    project_root = Path(settings.__file__).resolve().parents[1]

    for configured_path in (
        settings.ARTIFACTS_DIR,
        settings.SCAN_RESULTS_DIR,
        settings.BACKTEST_RESULTS_DIR,
        settings.BACKTEST_DATA_CACHE_DB_PATH,
        settings.FUNDAMENTALS_CACHE_DIR,
        settings.INDUSTRY_GROUP_CACHE_PATH,
        settings.RS_CACHE_DIR,
        settings.TICKER_CACHE_DIR,
    ):
        path = Path(configured_path)
        assert path.is_absolute()
        assert path.is_relative_to(project_root)


def test_index_ticker_cache_uses_centralized_project_path() -> None:
    assert index_ticker_fetcher.CACHE_DIR == Path(settings.TICKER_CACHE_DIR)
