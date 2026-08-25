from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys

import pandas as pd
import pytest

from core.pit_diagnosis.fact_cache import FactCacheBuilder, _number, build_fact_cache, open_fact_cache
from core.pit_diagnosis.models import DatePartition, DatePartitions
from core.pit_diagnosis.rulebook import load_rulebook
from core.pit_diagnosis.supplemental import IndustryGroupSnapshot, InstitutionalSnapshot


class _MiniPITBundle:
    """Small offline bundle-shaped fixture for fact-cache causality tests."""

    def __init__(self, digest: str = "a" * 64) -> None:
        self.sha256 = digest
        self.metadata = {"warmup_start": "2023-12-01", "schema_version": "1"}
        self._sessions = pd.bdate_range("2023-10-02", "2025-12-31")
        self._symbols = tuple(f"S{number:02d}" for number in range(10))
        self._prices: dict[str, pd.DataFrame] = {}
        for offset, symbol in enumerate(self._symbols):
            pattern = [100.0] * 5 + [98.0, 97.0, 99.0, 100.0] * 200
            close = pd.Series([value + offset for value in pattern[: len(self._sessions)]], index=self._sessions)
            self._prices[symbol] = pd.DataFrame(
                {"Open": close - 0.1, "High": close + 0.5, "Low": close - 0.5, "Close": close, "Volume": 1_000 + offset},
                index=self._sessions,
            )
        spy = pd.Series([400.0 + day * 0.1 for day in range(len(self._sessions))], index=self._sessions)
        self._prices["SPY"] = pd.DataFrame(
            {"Open": spy - 0.1, "High": spy + 0.5, "Low": spy - 0.5, "Close": spy, "Volume": 10_000}, index=self._sessions,
        )

    def members_at(self, when: object) -> frozenset[str]:
        del when
        return frozenset(self._symbols)

    def symbols(self) -> tuple[str, ...]:
        return (*self._symbols, "SPY")

    def fetch_price_data(self, tickers: object, start_date: object, end_date: object) -> dict[str, pd.DataFrame]:
        selected = tuple(tickers)
        start, end = pd.Timestamp(start_date), pd.Timestamp(end_date)
        return {symbol: self._prices[symbol].loc[start:end].copy() for symbol in selected if symbol in self._prices}

    def fetch_closes(self, tickers: object, start_date: object, end_date: object) -> pd.DataFrame:
        prices = self.fetch_price_data(tickers, start_date, end_date)
        return pd.DataFrame({symbol: frame["Close"] for symbol, frame in prices.items()})

    def iter_fundamental_state_boundaries(self, bounds: object):
        for symbol, (start, _end) in sorted(bounds.items()):
            periods = pd.to_datetime(["2023-09-30", "2022-09-30", "2021-09-30", "2020-09-30"])
            quarterly = pd.DataFrame(
                [[1.25, 1.0, 0.9, 0.8], [150.0, 120.0, 100.0, 90.0]],
                index=["Diluted EPS", "Total Revenue"], columns=periods,
            )
            annual = pd.DataFrame([[4.0, 3.5, 3.0, 2.5]], index=["Diluted EPS"], columns=periods)
            balance = pd.DataFrame(
                [[100.0], [500.0]], index=["Net Income", "Total Stockholders Equity"], columns=[periods[0]]
            )
            yield symbol, pd.Timestamp(start).date(), {
                "quarterly_income": quarterly,
                "annual_income": annual,
                "balance_sheet": balance,
                "company_info": {"shares_outstanding": 1_000_000},
            }


@pytest.fixture
def mini_pit_bundle() -> _MiniPITBundle:
    return _MiniPITBundle()


def rulebook_v1():
    return load_rulebook(Path("config/pit_canslim_rulebook_v1.json"))


def mini_partitions() -> DatePartitions:
    return DatePartitions(
        discovery=DatePartition("discovery", "2024-01-02", "2024-01-05"),
        validation=DatePartition("validation", "2024-01-08", "2024-01-09"),
        locked_evaluation=DatePartition("locked_evaluation", "2024-01-10", "2024-01-12"),
    )


def cache_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    return tmp_path / "facts.sqlite3", tmp_path / "facts.checkpoint.json", tmp_path / "facts.progress.jsonl"


def build_cache(bundle: _MiniPITBundle, paths: tuple[Path, Path, Path], *, resume: bool):
    output_path, checkpoint_path, progress_path = paths
    return build_fact_cache(
        bundle=bundle,
        rulebook=rulebook_v1(),
        partitions=mini_partitions(),
        output_path=output_path,
        checkpoint_path=checkpoint_path,
        progress_path=progress_path,
        resume=resume,
        checkpoint_every_sessions=1,
    )


def mutated_bundle(bundle: _MiniPITBundle) -> _MiniPITBundle:
    return _MiniPITBundle(hashlib.sha256((bundle.sha256 + "changed").encode()).hexdigest())


def test_fact_cache_contains_only_pit_session_inputs(mini_pit_bundle: _MiniPITBundle, tmp_path: Path) -> None:
    paths = cache_paths(tmp_path)
    result = build_cache(mini_pit_bundle, paths, resume=False)
    with open_fact_cache(result.path, result.content_sha256) as cache:
        row = cache.session_fact("S00", "2024-01-05")
        assert row.session == "2024-01-05"
        assert row.latest_fundamental_public_date <= row.session
        assert "leader" not in set(cache.column_names)
        assert "agent" not in set(cache.column_names)


def test_resume_skips_completed_sessions_and_rejects_identity_change(
    mini_pit_bundle: _MiniPITBundle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = cache_paths(tmp_path)
    original = FactCacheBuilder._after_session
    calls = 0

    def interrupt_once(builder: FactCacheBuilder, session: str) -> None:
        nonlocal calls
        original(builder, session)
        calls += 1
        if calls == 1:
            raise InterruptedError("test interruption")

    monkeypatch.setattr(FactCacheBuilder, "_after_session", interrupt_once)
    with pytest.raises(InterruptedError):
        build_cache(mini_pit_bundle, paths, resume=False)
    monkeypatch.setattr(FactCacheBuilder, "_after_session", original)
    resumed = build_cache(mini_pit_bundle, paths, resume=True)
    assert resumed.resumed is True
    assert resumed.reprocessed_sessions == 0
    with pytest.raises(ValueError, match="identity"):
        build_cache(mutated_bundle(mini_pit_bundle), paths, resume=True)


def test_fact_cache_import_does_not_load_live_provider_dependencies() -> None:
    command = (
        "import sys; import core.pit_diagnosis.fact_cache; "
        "assert 'core.data_client' not in sys.modules; "
        "assert 'core.index_ticker_fetcher' not in sys.modules"
    )

    result = subprocess.run([sys.executable, "-c", command], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


def test_resume_reconciles_progress_durable_after_checkpoint_window(
    mini_pit_bundle: _MiniPITBundle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = cache_paths(tmp_path)
    original = FactCacheBuilder._write_checkpoint
    calls = 0

    def interrupt_after_progress(builder: FactCacheBuilder, next_index: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise InterruptedError("between progress and checkpoint")
        original(builder, next_index)

    monkeypatch.setattr(FactCacheBuilder, "_write_checkpoint", interrupt_after_progress)
    with pytest.raises(InterruptedError, match="between progress"):
        build_cache(mini_pit_bundle, paths, resume=False)
    monkeypatch.setattr(FactCacheBuilder, "_write_checkpoint", original)

    resumed = build_cache(mini_pit_bundle, paths, resume=True)

    assert resumed.resumed is True
    assert resumed.reprocessed_sessions == 0


def test_supplemental_snapshot_with_date_but_no_evidence_is_rejected() -> None:
    with pytest.raises(ValueError, match="available"):
        InstitutionalSnapshot("2024-01-05", None, None, None)
    with pytest.raises(ValueError, match="available"):
        IndustryGroupSnapshot("2024-01-05", None, None)


def test_finalization_rejects_wrong_member_even_when_total_rows_match(
    mini_pit_bundle: _MiniPITBundle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = cache_paths(tmp_path)
    original = FactCacheBuilder._materialize_session
    changed = False

    def wrong_member(builder: FactCacheBuilder, session: str, states: object):
        nonlocal changed
        rows = original(builder, session, states)
        if not changed:
            rows[0] = {**rows[0], "symbol": "NOT_A_MEMBER"}
            changed = True
        return rows

    monkeypatch.setattr(FactCacheBuilder, "_materialize_session", wrong_member)

    with pytest.raises(ValueError, match="membership"):
        build_cache(mini_pit_bundle, paths, resume=False)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_fail_closed(value: float) -> None:
    with pytest.raises(ValueError, match="non-finite"):
        _number(value)
