"""Regression tests for deterministic SQLite connection cleanup."""

import sqlite3

import pytest

from core.backtest_engine import DataFetcher
from core.execution_store import ExecutionStore


def _assert_connection_is_closed(connection: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_execution_store_context_closes_connection(tmp_path) -> None:
    store = ExecutionStore(str(tmp_path / "execution.db"))

    with store._connect() as connection:
        connection.execute("SELECT 1")

    _assert_connection_is_closed(connection)


def test_backtest_cache_context_closes_connection(tmp_path) -> None:
    fetcher = DataFetcher(str(tmp_path / "backtest.db"))

    with fetcher._connect() as connection:
        connection.execute("SELECT 1")

    _assert_connection_is_closed(connection)
