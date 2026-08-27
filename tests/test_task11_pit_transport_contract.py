"""Offline trust-boundary tests for Task 11 PIT bundle transport."""

from __future__ import annotations

from contextlib import closing
import hashlib
from datetime import date
from itertools import product
from pathlib import Path
import sqlite3
import string
import subprocess
import sys

import pandas as pd
import pytest

from core.pit_data import PITDataBundle
from core.pit_provenance import PIT_PUBLIC_DATES_ATTR


_DIGEST = "0" * 64
_METADATA = {
    "bundle_kind": "canslim_pit_v1",
    "schema_version": "1",
    "data_cutoff": "2021-01-04",
    "evaluation_start": "2021-01-01",
    "warmup_start": "2020-01-01",
    "membership_source_sha256": _DIGEST,
    "prices_source_sha256": _DIGEST,
    "fundamentals_source_sha256": _DIGEST,
    "membership_provenance_sha256": _DIGEST,
    "prices_provenance_sha256": _DIGEST,
    "fundamentals_provenance_sha256": _DIGEST,
    "membership_source_kind": "offline_test_fixture",
    "membership_revision_id": "fixture-v1",
    "membership_raw_sha256": _DIGEST,
    "membership_symbol_map_sha256": _DIGEST,
    "membership_security_names_sha256": _DIGEST,
    "prices_source_kind": "offline_test_fixture",
    "prices_upstream_source_sha256": _DIGEST,
    "spy_trading_days_sha256": _DIGEST,
    "price_identity_map_sha256": _DIGEST,
    "price_identity_request_contracts_sha256": _DIGEST,
    "price_exclusion_count": "0",
    "price_exclusions_sha256": _DIGEST,
    "fundamentals_source_kind": "offline_test_fixture",
    "fundamentals_submissions_archive_sha256": _DIGEST,
    "fundamentals_companyfacts_archive_sha256": _DIGEST,
    "fundamentals_identity_manifest_csv_sha256": _DIGEST,
}


def _member_symbols() -> tuple[str, ...]:
    symbols = []
    for letters in product(string.ascii_uppercase, repeat=3):
        ticker = "".join(letters)
        if ticker != "SPY":
            symbols.append(ticker)
        if len(symbols) == 495:
            return tuple(symbols)
    raise AssertionError("test ticker generator did not produce 495 symbols")


def _valid_bundle_bytes(tmp_path: Path) -> bytes:
    path = tmp_path / "minimal-valid-pit.sqlite3"
    members = _member_symbols()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "CREATE TABLE dataset_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE membership (effective_date TEXT NOT NULL, ticker TEXT NOT NULL, member INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE price (trade_date TEXT NOT NULL, ticker TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE fundamentals (ticker TEXT NOT NULL, statement_type TEXT NOT NULL, period_end TEXT NOT NULL, public_date TEXT NOT NULL, basic_eps REAL, diluted_eps REAL, total_revenue REAL, net_income REAL, common_stock REAL, total_stockholders_equity REAL, shares_outstanding REAL, held_percent_institutions REAL, institution_count INTEGER, prev_institution_count INTEGER)"
        )
        connection.executemany(
            "INSERT INTO dataset_metadata VALUES (?, ?)", _METADATA.items()
        )
        connection.executemany(
            "INSERT INTO membership VALUES (?, ?, ?)",
            (("2021-01-01", symbol, 1) for symbol in members),
        )
        connection.executemany(
            "INSERT INTO price VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ("2020-01-02", "SPY", 100.0, 101.0, 99.0, 100.0, 1_000.0),
                ("2021-01-04", "SPY", 101.0, 102.0, 100.0, 101.0, 1_100.0),
            ),
        )
        connection.commit()
        connection.executemany(
            "INSERT INTO fundamentals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    members[0],
                    "quarterly",
                    "2020-06-30",
                    "2020-10-30",
                    0.75,
                    0.75,
                    75.0,
                    7.5,
                    None,
                    None,
                    900_000.0,
                    None,
                    None,
                    None,
                ),
                (
                    members[0],
                    "quarterly",
                    "2020-09-30",
                    "2020-11-01",
                    1.0,
                    1.0,
                    100.0,
                    10.0,
                    None,
                    None,
                    1_000_000.0,
                    None,
                    None,
                    None,
                ),
                (
                    members[0],
                    "quarterly",
                    "2020-09-30",
                    "2020-11-03",
                    1.25,
                    1.25,
                    125.0,
                    12.5,
                    None,
                    None,
                    1_100_000.0,
                    None,
                    None,
                    None,
                ),
                (
                    members[0],
                    "quarterly",
                    "2020-09-30",
                    "2020-11-05",
                    9.0,
                    9.0,
                    900.0,
                    90.0,
                    None,
                    None,
                    9_000_000.0,
                    None,
                    None,
                    None,
                ),
            ),
        )
        connection.commit()
    return path.read_bytes()


def _assert_snapshot_cell_parity(
    ordinary: dict[str, object], traced: dict[str, object]
) -> None:
    assert set(ordinary) == {
        "quarterly_income",
        "annual_income",
        "balance_sheet",
        "company_info",
    }
    assert set(traced) == set(ordinary)
    assert traced["company_info"] == ordinary["company_info"]
    for key in ("quarterly_income", "annual_income", "balance_sheet"):
        ordinary_frame = ordinary[key]
        traced_frame = traced[key]
        assert isinstance(ordinary_frame, pd.DataFrame)
        assert isinstance(traced_frame, pd.DataFrame)
        ordinary_cells = ordinary_frame.copy()
        traced_cells = traced_frame.copy()
        ordinary_cells.attrs = {}
        traced_cells.attrs = {}
        pd.testing.assert_frame_equal(traced_cells, ordinary_cells)
        assert PIT_PUBLIC_DATES_ATTR not in ordinary_frame.attrs
        assert set(traced_frame.attrs) == {PIT_PUBLIC_DATES_ATTR}


def test_authenticated_bytes_open_the_exact_bundle_query_only(tmp_path: Path) -> None:
    """Break caught: authenticated bytes are reopened by path or left writable."""
    data = _valid_bundle_bytes(tmp_path)
    expected_sha256 = hashlib.sha256(data).hexdigest()

    bundle = PITDataBundle.from_authenticated_bytes(
        data, expected_sha256=expected_sha256
    )
    with bundle:
        assert bundle.path is None
        assert bundle.sha256 == expected_sha256
        assert len(bundle.members_at("2021-01-04")) == 495
        assert "SPY" in bundle.symbols()
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            bundle._connection.execute("DELETE FROM price")
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        bundle._connection.execute("SELECT 1")


def test_public_fundamental_snapshot_provenance_changes_only_frame_attrs(
    tmp_path: Path,
) -> None:
    """Break caught: the public point query changes cells or loses amendment dates."""
    data = _valid_bundle_bytes(tmp_path)
    digest = hashlib.sha256(data).hexdigest()
    symbol = _member_symbols()[0]

    with PITDataBundle.from_authenticated_bytes(
        data, expected_sha256=digest
    ) as bundle:
        ordinary = bundle.fundamentals_as_of(symbol, pd.Timestamp("2020-11-03"))
        traced = bundle.fundamentals_as_of(
            symbol,
            pd.Timestamp("2020-11-03"),
            include_provenance=True,
        )

    _assert_snapshot_cell_parity(ordinary, traced)
    quarterly = traced["quarterly_income"]
    assert isinstance(quarterly, pd.DataFrame)
    assert quarterly.shape == (4, 2)
    assert quarterly.loc["Diluted EPS", pd.Timestamp("2020-06-30")] == 0.75
    assert quarterly.loc["Diluted EPS", pd.Timestamp("2020-09-30")] == 1.25
    assert quarterly.attrs == {
        "pit_public_date_by_period": {
            "2020-06-30": "2020-10-30",
            "2020-09-30": "2020-11-03",
        }
    }
    for key in ("annual_income", "balance_sheet"):
        frame = traced[key]
        assert isinstance(frame, pd.DataFrame)
        assert frame.attrs == {"pit_public_date_by_period": {}}


def test_public_fundamental_boundary_stream_preserves_cells_and_revision_order(
    tmp_path: Path,
) -> None:
    """Break caught: the boundary iterator omits or mislabels an atomic revision."""
    data = _valid_bundle_bytes(tmp_path)
    digest = hashlib.sha256(data).hexdigest()
    symbol = _member_symbols()[0]
    bounds = {symbol: (pd.Timestamp("2020-11-01"), pd.Timestamp("2020-11-03"))}

    with PITDataBundle.from_authenticated_bytes(
        data, expected_sha256=digest
    ) as bundle:
        ordinary = list(bundle.iter_fundamental_state_boundaries(bounds))
        traced = list(
            bundle.iter_fundamental_state_boundaries(
                bounds,
                include_provenance=True,
            )
        )

    assert [(ticker, boundary) for ticker, boundary, _snapshot in ordinary] == [
        (symbol, date(2020, 11, 1)),
        (symbol, date(2020, 11, 3)),
    ]
    assert [(ticker, boundary) for ticker, boundary, _snapshot in traced] == [
        (symbol, date(2020, 11, 1)),
        (symbol, date(2020, 11, 3)),
    ]
    expected = ((1.0, "2020-11-01"), (1.25, "2020-11-03"))
    for ordinary_item, traced_item, (value, public_date) in zip(
        ordinary, traced, expected, strict=True
    ):
        _assert_snapshot_cell_parity(ordinary_item[2], traced_item[2])
        quarterly = traced_item[2]["quarterly_income"]
        assert isinstance(quarterly, pd.DataFrame)
        assert quarterly.loc["Diluted EPS", pd.Timestamp("2020-09-30")] == value
        assert quarterly.attrs == {
            "pit_public_date_by_period": {
                "2020-06-30": "2020-10-30",
                "2020-09-30": public_date,
            }
        }

    assert all(
        snapshot["quarterly_income"].loc[
            "Diluted EPS", pd.Timestamp("2020-09-30")
        ]
        != 9.0
        for _ticker, _boundary, snapshot in traced
    )


def test_authenticated_bytes_reject_mutability_and_tampering_before_sqlite_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break caught: mutable or hash-mismatched bundle bytes reach SQLite parsing."""
    data = _valid_bundle_bytes(tmp_path)
    expected_sha256 = hashlib.sha256(data).hexdigest()
    tampered = bytearray(data)
    tampered[-1] ^= 1
    connect_calls: list[tuple[object, ...]] = []

    def forbidden_connect(*args: object, **_kwargs: object) -> None:
        connect_calls.append(args)
        raise AssertionError("SQLite connect must not run before byte authentication")

    monkeypatch.setattr(sqlite3, "connect", forbidden_connect)

    with pytest.raises(ValueError, match="immutable bytes"):
        PITDataBundle.from_authenticated_bytes(
            bytearray(data), expected_sha256=expected_sha256  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="does not match"):
        PITDataBundle.from_authenticated_bytes(
            bytes(tampered), expected_sha256=expected_sha256
        )
    assert connect_calls == []


@pytest.mark.parametrize("failure", ["deserialize", "schema"])
def test_authenticated_bytes_close_connection_when_construction_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Break caught: a failed in-memory bundle construction leaks SQLite state."""
    if failure == "deserialize":
        data = b"not-a-sqlite-database"
        message = "database"
    else:
        path = tmp_path / "invalid-schema.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
            connection.commit()
        data = path.read_bytes()
        message = "missing tables"

    original_connect = sqlite3.connect
    connections: list[sqlite3.Connection] = []

    def recording_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = original_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", recording_connect)

    with pytest.raises((sqlite3.DatabaseError, ValueError), match=message):
        PITDataBundle.from_authenticated_bytes(
            data,
            expected_sha256=hashlib.sha256(data).hexdigest(),
        )

    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connections[0].execute("SELECT 1")


def test_pit_data_import_keeps_live_provider_modules_out_of_process() -> None:
    """Break caught: provenance constants restore an eager live-provider import edge."""
    command = (
        "import sys; import core.pit_data; "
        "assert 'core.data_client' not in sys.modules; "
        "assert 'core.index_ticker_fetcher' not in sys.modules"
    )

    try:
        result = subprocess.run(
            [sys.executable, "-B", "-c", command],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            "isolated core.pit_data import did not finish within 10 seconds",
            pytrace=False,
        )

    assert result.returncode == 0, result.stderr
