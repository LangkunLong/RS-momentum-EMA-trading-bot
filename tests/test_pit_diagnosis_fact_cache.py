from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any, Mapping

import pandas as pd
import pytest

from core.pit_data import PITDataBundle
from core.pit_diagnosis import fact_cache as fact_cache_module
from core.pit_diagnosis.fact_cache import (
    FactCacheBuilder,
    _FACT_COLUMNS,
    _V1_SCHEMA_SHA256,
    _V1_SESSION_FACTS_CREATE_SQL,
    _detect_prepared_base,
    _frame_values,
    _number,
    build_fact_cache,
    open_fact_cache,
)
from core.pit_diagnosis.catalog import load_experiment_catalog
from core.pit_diagnosis.models import DatePartition, DatePartitions, FidelityLabel, Observability, RuleOutcome
from core.pit_diagnosis.patterns import BasePolicy, _history_sha256, detect_proper_base
from core.pit_diagnosis.rulebook import canonical_sha256, evaluate_fidelity, load_rulebook
from core.pit_diagnosis.supplemental import IndustryGroupSnapshot, InstitutionalSnapshot, UnavailableSupplementalPITProvider
from core.pit_diagnosis.strategy import evaluate_session_rules


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


class _SuccessorWithoutAdmissionPriceBundle(_MiniPITBundle):
    """A PIT member whose first observed bar follows its effective date."""

    def __init__(self) -> None:
        super().__init__()
        self._prices["NEW"] = self._prices["S00"].loc["2024-01-04":].copy()

    def members_at(self, when: object) -> frozenset[str]:
        return super().members_at(when) | {"NEW"}

    def symbols(self) -> tuple[str, ...]:
        return (*super().symbols(), "NEW")


class _PriceFetchRecordingBundle(_MiniPITBundle):
    """Record bundle price access while retaining the mini-bundle data contract."""

    def __init__(self) -> None:
        super().__init__()
        self.price_fetches: list[tuple[tuple[str, ...], pd.Timestamp, pd.Timestamp]] = []

    def fetch_price_data(self, tickers: object, start_date: object, end_date: object) -> dict[str, pd.DataFrame]:
        self.price_fetches.append((tuple(tickers), pd.Timestamp(start_date), pd.Timestamp(end_date)))
        return super().fetch_price_data(tickers, start_date, end_date)


class _AsOfSupplementalProvider:
    """Small deterministic supplemental provider with explicit PIT snapshots."""

    content_identity_sha256 = "f" * 64

    def __init__(self, snapshots: tuple[tuple[str, int], ...], *, force_future: bool = False) -> None:
        self.snapshots = snapshots
        self.force_future = force_future

    def _selected(self, session: str) -> tuple[str, int] | None:
        if self.force_future:
            return self.snapshots[0]
        eligible = [snapshot for snapshot in self.snapshots if snapshot[0] <= session]
        return eligible[-1] if eligible else None

    def institutional_snapshot(self, symbol: str, session: str) -> InstitutionalSnapshot:
        del symbol
        selected = self._selected(session)
        if selected is None:
            return InstitutionalSnapshot(None, None, None, None)
        as_of, marker = selected
        return InstitutionalSnapshot(as_of, 0.25, 100 + marker, 90 + marker, (f"institutional:{marker}",))

    def industry_group_snapshot(self, symbol: str, session: str) -> IndustryGroupSnapshot:
        del symbol
        selected = self._selected(session)
        if selected is None:
            return IndustryGroupSnapshot(None, None, None)
        as_of, marker = selected
        return IndustryGroupSnapshot(as_of, f"industry-{marker}", 10 + marker, ("S00",), (f"industry:{marker}",))


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


def make_interrupted_partial_v1(paths: tuple[Path, Path, Path], *, exact_v1_table: bool = True) -> None:
    """Convert a priced interrupted fixture to the exact historical v1 shape."""
    partial = Path(f"{paths[0]}.partial")
    connection = sqlite3.connect(partial)
    try:
        identity = json.loads(connection.execute("SELECT value FROM metadata WHERE key='identity'").fetchone()[0])
        identity["fact_cache_schema_version"] = "1"
        identity["fact_cache_schema_sha256"] = _V1_SCHEMA_SHA256
        if exact_v1_table:
            connection.execute("ALTER TABLE session_facts RENAME TO session_facts_v2")
            connection.execute(_V1_SESSION_FACTS_CREATE_SQL)
            columns = ",".join(_FACT_COLUMNS)
            connection.execute(f"INSERT INTO session_facts({columns}) SELECT {columns} FROM session_facts_v2")
            connection.execute("DROP TABLE session_facts_v2")
        connection.execute("UPDATE metadata SET value='1' WHERE key='schema_version'")
        connection.execute("UPDATE metadata SET value=? WHERE key='schema_sha256'", (_V1_SCHEMA_SHA256,))
        connection.execute("UPDATE metadata SET value=? WHERE key='identity'", (json.dumps(identity, sort_keys=True, separators=(",", ":")),))
        connection.execute("UPDATE metadata SET value=? WHERE key='identity_sha256'", (canonical_sha256(identity),))
        connection.commit()
    finally:
        connection.close()
    checkpoint = json.loads(paths[1].read_text(encoding="utf-8"))
    checkpoint["identity"] = identity
    checkpoint["identity_sha256"] = canonical_sha256(identity)
    paths[1].write_text(json.dumps(checkpoint, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    records = [json.loads(line) for line in paths[2].read_text(encoding="utf-8").splitlines()]
    for record in records:
        record["identity_sha256"] = canonical_sha256(identity)
    records[-1]["state_sha256"] = hashlib.sha256(partial.read_bytes()).hexdigest()
    paths[2].write_text("\n".join(json.dumps(record, sort_keys=True, separators=(",", ":")) for record in records) + "\n", encoding="utf-8")


def build_cache(
    bundle: _MiniPITBundle,
    paths: tuple[Path, Path, Path],
    *,
    resume: bool,
    supplemental_provider: object | None = None,
):
    output_path, checkpoint_path, progress_path = paths
    return build_fact_cache(
        bundle=bundle,
        rulebook=rulebook_v1(),
        partitions=mini_partitions(),
        output_path=output_path,
        checkpoint_path=checkpoint_path,
        progress_path=progress_path,
        resume=resume,
        supplemental_provider=supplemental_provider,
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


def test_supplemental_snapshots_are_selected_as_of_each_cached_session(
    mini_pit_bundle: _MiniPITBundle, tmp_path: Path,
) -> None:
    provider = _AsOfSupplementalProvider(
        (("2023-12-20", 1), ("2024-01-08", 2), ("2024-01-10", 3)),
    )
    result = build_cache(
        mini_pit_bundle,
        cache_paths(tmp_path),
        resume=False,
        supplemental_provider=provider,
    )

    with open_fact_cache(result.path, result.content_sha256) as cache:
        before_update = cache.session_fact("S00", "2024-01-05")
        after_update = cache.session_fact("S00", "2024-01-08")
        same_day = cache.session_fact("S00", "2024-01-10")

    assert before_update.institutional_as_of_date == "2023-12-20"
    assert json.loads(before_update.institutional_evidence_ids) == ["institutional:1"]
    assert before_update.industry_as_of_date == "2023-12-20"
    assert after_update.institutional_as_of_date == "2024-01-08"
    assert json.loads(after_update.industry_evidence_ids) == ["industry:2"]
    assert same_day.institutional_as_of_date == "2024-01-10"


def test_future_supplemental_snapshot_is_rejected_before_cache_publication(
    mini_pit_bundle: _MiniPITBundle, tmp_path: Path,
) -> None:
    provider = _AsOfSupplementalProvider((("2024-01-03", 1),), force_future=True)

    with pytest.raises(ValueError, match="supplemental snapshot is future-dated"):
        build_cache(
            mini_pit_bundle,
            cache_paths(tmp_path),
            resume=False,
            supplemental_provider=provider,
        )


def test_unavailable_i_and_l_evidence_remains_a_strict_fidelity_blocker(
    mini_pit_bundle: _MiniPITBundle, tmp_path: Path,
) -> None:
    result = build_cache(mini_pit_bundle, cache_paths(tmp_path), resume=False)
    rulebook = rulebook_v1()
    catalog = load_experiment_catalog(Path("config/pit_diagnosis_experiments_v1.json"), rulebook)
    with open_fact_cache(result.path, result.content_sha256) as cache:
        fact = cache.session_fact("S00", "2024-01-05")

    outcomes = dict(
        zip(
            rulebook.rules,
            evaluate_session_rules(
                fact,
                rulebook,
                catalog["D2.RULE_STAGE_FUNNEL"],
                strict_canslim=True,
            ),
            strict=True,
        )
    )
    assessment = evaluate_fidelity(rulebook, outcomes)

    assert outcomes["I.SPONSORSHIP"].status == "unavailable"
    assert outcomes["L.INDUSTRY_GROUP"].status == "unavailable"
    assert assessment.label is FidelityLabel.FIDELITY_INCOMPLETE
    assert {"I.SPONSORSHIP", "L.INDUSTRY_GROUP"} <= set(assessment.unavailable_required_rule_ids)


def test_approved_proxy_rulebook_remains_compatible_with_strict_fidelity_evaluation() -> None:
    book = rulebook_v1()
    proxy_rules = dict(book.rules)
    for rule_id in ("I.SPONSORSHIP", "L.INDUSTRY_GROUP"):
        proxy_rules[rule_id] = replace(proxy_rules[rule_id], observability=Observability.PIT_PROXY)
    proxy_book = replace(book, rules=proxy_rules)
    outcomes = {rule_id: RuleOutcome.passed(rule_id) for rule_id in proxy_book.rules}

    assessment = evaluate_fidelity(
        proxy_book,
        outcomes,
        approved_proxy_rule_ids=frozenset({"I.SPONSORSHIP", "L.INDUSTRY_GROUP"}),
    )

    assert assessment.label is FidelityLabel.QUANTITATIVE_CANSLIM_PROXY
    assert assessment.proxy_rule_ids == ("I.SPONSORSHIP", "L.INDUSTRY_GROUP")
    assert assessment.promotion_eligible is True


def test_fact_cache_prefetches_full_range_once_without_changing_session_facts(tmp_path: Path) -> None:
    bundle = _PriceFetchRecordingBundle()
    paths = cache_paths(tmp_path)

    result = build_cache(bundle, paths, resume=False)

    assert bundle.price_fetches == [
        (bundle.symbols(), pd.Timestamp("2023-12-01"), pd.Timestamp("2024-01-12")),
    ]
    with open_fact_cache(result.path, result.content_sha256) as cache:
        row = cache.session_fact("S00", "2024-01-05")
        assert row.close == bundle._prices["S00"].loc[pd.Timestamp("2024-01-05"), "Close"]
        assert row.prior_close == bundle._prices["S00"].loc[pd.Timestamp("2024-01-04"), "Close"]
        assert row.market_regime == "unavailable"


def test_session_materialization_passes_full_prefetched_price_frames_to_rows(
    mini_pit_bundle: _MiniPITBundle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = cache_paths(tmp_path)
    builder = FactCacheBuilder(
        bundle=mini_pit_bundle,
        rulebook=rulebook_v1(),
        partitions=mini_partitions(),
        output_path=paths[0],
        checkpoint_path=paths[1],
        progress_path=paths[2],
        supplemental_provider=UnavailableSupplementalPITProvider(),
    )
    prefetched = builder._prefetch_prices()
    session = "2024-01-05"
    seen: dict[str, pd.DataFrame | None] = {}
    original = builder._row

    def capture(
        symbol: str,
        row_session: str,
        prices: pd.DataFrame | None,
        state: Mapping[str, Any] | None,
        state_date: str | None,
        rs_rating: float | None,
        market: Mapping[str, object],
    ) -> dict[str, object]:
        seen[symbol] = prices
        return original(symbol, row_session, prices, state, state_date, rs_rating, market)

    monkeypatch.setattr(builder, "_row", capture)
    builder._materialize_session(session, builder._fundamental_states([session]), prefetched)

    assert seen
    assert all(seen[symbol] is prefetched.prices[symbol] for symbol in seen)
    assert all(pd.Timestamp("2024-01-08") in prices.index for prices in seen.values() if prices is not None)


def test_successor_member_without_admission_price_is_cached_as_price_unavailable(tmp_path: Path) -> None:
    paths = cache_paths(tmp_path)
    result = build_cache(_SuccessorWithoutAdmissionPriceBundle(), paths, resume=False)

    with open_fact_cache(result.path, result.content_sha256) as cache:
        unavailable = cache.session_fact("NEW", "2024-01-02")
        assert unavailable.member == 1
        assert unavailable.availability_bitset & 1 == 0
        assert all(unavailable[column] is None for column in ("open", "high", "low", "close", "volume"))
        admitted = cache.session_fact("NEW", "2024-01-04")
        assert admitted.availability_bitset & 1 == 1
        assert all(admitted[column] is not None for column in ("open", "high", "low", "close", "volume"))
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


def test_resume_rebuilds_only_incomplete_v1_price_schema(
    mini_pit_bundle: _MiniPITBundle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = cache_paths(tmp_path)

    def interrupt_after_first_session(_builder: FactCacheBuilder, _session: str) -> None:
        raise InterruptedError("leave v1 partial")

    monkeypatch.setattr(FactCacheBuilder, "_after_session", interrupt_after_first_session)
    with pytest.raises(InterruptedError, match="leave v1"):
        build_cache(mini_pit_bundle, paths, resume=False)
    make_interrupted_partial_v1(paths)
    monkeypatch.setattr(FactCacheBuilder, "_after_session", lambda *_args: None)

    rebuilt = build_cache(mini_pit_bundle, paths, resume=True)

    assert rebuilt.resumed is False


def test_resume_refuses_foreign_v1_partial_before_destructive_migration(
    mini_pit_bundle: _MiniPITBundle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = cache_paths(tmp_path)

    def interrupt_after_first_session(_builder: FactCacheBuilder, _session: str) -> None:
        raise InterruptedError("leave foreign v1 partial")

    monkeypatch.setattr(FactCacheBuilder, "_after_session", interrupt_after_first_session)
    with pytest.raises(InterruptedError, match="leave foreign v1"):
        build_cache(mini_pit_bundle, paths, resume=False)
    make_interrupted_partial_v1(paths)
    partial = Path(f"{paths[0]}.partial")
    connection = sqlite3.connect(partial)
    try:
        identity = json.loads(connection.execute("SELECT value FROM metadata WHERE key='identity'").fetchone()[0])
        identity["bundle_sha256"] = "b" * 64
        connection.execute("UPDATE metadata SET value=? WHERE key='identity'", (json.dumps(identity, sort_keys=True, separators=(",", ":")),))
        connection.execute("UPDATE metadata SET value=? WHERE key='identity_sha256'", (canonical_sha256(identity),))
        connection.commit()
    finally:
        connection.close()
    monkeypatch.setattr(FactCacheBuilder, "_after_session", lambda *_args: None)

    with pytest.raises(ValueError, match="fact cache"):
        build_cache(mini_pit_bundle, paths, resume=True)

    assert partial.exists()
    assert paths[1].exists()
    assert paths[2].exists()


@pytest.mark.parametrize("sidecar", ("checkpoint_foreign", "checkpoint_malformed", "checkpoint_only", "checkpoint_arbitrary_index", "progress_foreign", "progress_malformed", "progress_state_mismatch"))
def test_resume_refuses_untrusted_v1_migration_sidecars(
    mini_pit_bundle: _MiniPITBundle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sidecar: str,
) -> None:
    paths = cache_paths(tmp_path)

    def interrupt_after_first_session(_builder: FactCacheBuilder, _session: str) -> None:
        raise InterruptedError("leave v1 sidecars")

    monkeypatch.setattr(FactCacheBuilder, "_after_session", interrupt_after_first_session)
    with pytest.raises(InterruptedError, match="leave v1 sidecars"):
        build_cache(mini_pit_bundle, paths, resume=False)
    make_interrupted_partial_v1(paths)
    if sidecar == "checkpoint_foreign":
        checkpoint = json.loads(paths[1].read_text(encoding="utf-8"))
        checkpoint["identity_sha256"] = "b" * 64
        paths[1].write_text(json.dumps(checkpoint), encoding="utf-8")
    elif sidecar == "checkpoint_malformed":
        paths[1].write_text("{}", encoding="utf-8")
    elif sidecar == "checkpoint_only":
        paths[2].unlink()
    elif sidecar == "checkpoint_arbitrary_index":
        checkpoint = json.loads(paths[1].read_text(encoding="utf-8"))
        checkpoint["next_session_index"] = 0
        paths[1].write_text(json.dumps(checkpoint), encoding="utf-8")
    elif sidecar == "progress_foreign":
        record = json.loads(paths[2].read_text(encoding="utf-8").splitlines()[0])
        record["identity_sha256"] = "b" * 64
        paths[2].write_text(json.dumps(record) + "\n", encoding="utf-8")
    elif sidecar == "progress_state_mismatch":
        record = json.loads(paths[2].read_text(encoding="utf-8").splitlines()[0])
        record["state_sha256"] = "b" * 64
        paths[2].write_text(json.dumps(record) + "\n", encoding="utf-8")
    else:
        paths[2].write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(FactCacheBuilder, "_after_session", lambda *_args: None)

    with pytest.raises(ValueError, match="fact cache"):
        build_cache(mini_pit_bundle, paths, resume=True)

    assert Path(f"{paths[0]}.partial").exists()
    assert paths[1].exists()
    assert paths[2].exists() is (sidecar != "checkpoint_only")


def test_resume_refuses_v1_metadata_copied_onto_v2_table(
    mini_pit_bundle: _MiniPITBundle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = cache_paths(tmp_path)

    def interrupt_after_first_session(_builder: FactCacheBuilder, _session: str) -> None:
        raise InterruptedError("leave copied metadata table")

    monkeypatch.setattr(FactCacheBuilder, "_after_session", interrupt_after_first_session)
    with pytest.raises(InterruptedError, match="leave copied metadata table"):
        build_cache(mini_pit_bundle, paths, resume=False)
    make_interrupted_partial_v1(paths, exact_v1_table=False)
    monkeypatch.setattr(FactCacheBuilder, "_after_session", lambda *_args: None)

    with pytest.raises(ValueError, match="identity|schema"):
        build_cache(mini_pit_bundle, paths, resume=True)

    assert Path(f"{paths[0]}.partial").exists()


def test_resume_refuses_populated_v1_partial_without_sidecars(
    mini_pit_bundle: _MiniPITBundle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = cache_paths(tmp_path)

    def interrupt_after_first_session(_builder: FactCacheBuilder, _session: str) -> None:
        raise InterruptedError("leave populated v1 without sidecars")

    monkeypatch.setattr(FactCacheBuilder, "_after_session", interrupt_after_first_session)
    with pytest.raises(InterruptedError, match="leave populated v1"):
        build_cache(mini_pit_bundle, paths, resume=False)
    make_interrupted_partial_v1(paths)
    partial = Path(f"{paths[0]}.partial")
    connection = sqlite3.connect(partial)
    try:
        assert connection.execute("SELECT COUNT(*) FROM session_facts").fetchone()[0] == 10
    finally:
        connection.close()
    paths[1].unlink()
    paths[2].unlink()
    before = hashlib.sha256(partial.read_bytes()).hexdigest()
    monkeypatch.setattr(FactCacheBuilder, "_after_session", lambda *_args: None)

    with pytest.raises(ValueError, match="fact cache"):
        build_cache(mini_pit_bundle, paths, resume=True)

    assert partial.exists()
    assert hashlib.sha256(partial.read_bytes()).hexdigest() == before
    assert not paths[1].exists()
    assert not paths[2].exists()


def test_resume_migrates_empty_v1_partial_without_sidecars(
    mini_pit_bundle: _MiniPITBundle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = cache_paths(tmp_path)

    def interrupt_after_first_session(_builder: FactCacheBuilder, _session: str) -> None:
        raise InterruptedError("leave empty v1 without sidecars")

    monkeypatch.setattr(FactCacheBuilder, "_after_session", interrupt_after_first_session)
    with pytest.raises(InterruptedError, match="leave empty v1"):
        build_cache(mini_pit_bundle, paths, resume=False)
    make_interrupted_partial_v1(paths)
    partial = Path(f"{paths[0]}.partial")
    connection = sqlite3.connect(partial)
    try:
        connection.execute("DELETE FROM session_facts")
        connection.commit()
    finally:
        connection.close()
    paths[1].unlink()
    paths[2].unlink()
    monkeypatch.setattr(FactCacheBuilder, "_after_session", lambda *_args: None)

    rebuilt = build_cache(mini_pit_bundle, paths, resume=True)

    assert rebuilt.resumed is False


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

    def wrong_member(builder: FactCacheBuilder, session: str, states: object, prefetched_prices: object):
        nonlocal changed
        rows = original(builder, session, states, prefetched_prices)
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


def test_sqlite_null_fundamental_cells_are_explicitly_unavailable() -> None:
    frame = PITDataBundle._statement_frame([{
        "statement_type": "quarterly", "period_end": "2023-12-31", "public_date": "2024-02-01",
        "basic_eps": None, "diluted_eps": None, "total_revenue": 1.0, "net_income": None,
        "common_stock": None, "total_stockholders_equity": None,
    }], "quarterly")
    snapshot = {"quarterly_income": frame}

    assert _frame_values(snapshot, "quarterly_income", "Diluted EPS", 3) == [None, None, None]


def test_non_sqlite_nan_fundamental_cell_fails_closed() -> None:
    frame = PITDataBundle._statement_frame([{
        "statement_type": "quarterly", "period_end": "2023-12-31", "public_date": "2024-02-01",
        "basic_eps": None, "diluted_eps": float("nan"), "total_revenue": 1.0, "net_income": None,
        "common_stock": None, "total_stockholders_equity": None,
    }], "quarterly")
    snapshot = {"quarterly_income": frame}

    with pytest.raises(ValueError, match="non-finite"):
        _frame_values(snapshot, "quarterly_income", "Diluted EPS", 1)


def test_cached_pattern_prefixes_and_detector_match_public_history(
    mini_pit_bundle: _MiniPITBundle, tmp_path: Path,
) -> None:
    paths = cache_paths(tmp_path)
    builder = FactCacheBuilder(
        bundle=mini_pit_bundle,
        rulebook=rulebook_v1(),
        partitions=mini_partitions(),
        output_path=paths[0],
        checkpoint_path=paths[1],
        progress_path=paths[2],
        supplemental_provider=UnavailableSupplementalPITProvider(),
    )
    prefetched = builder._prefetch_prices()
    prepared = builder._prepared_pattern_histories["S00"]
    policy = BasePolicy.canonical_v1()
    prices = prefetched.prices["S00"]

    sample_positions = sorted({1, policy.flat_min_sessions - 1, policy.flat_min_sessions, len(prepared.index)})
    for position in sample_positions:
        history = pd.DataFrame(
            {
                "High": prepared.highs[:position],
                "Low": prepared.lows[:position],
                "Close": prepared.closes[:position],
            },
            index=prepared.index[:position],
        )
        assert prepared.input_sha256_prefixes[position] == _history_sha256(history)

    sample_sessions = ("2023-12-29", "2024-01-10", "2024-01-12")
    for session in (pd.Timestamp(item) for item in sample_sessions):
        session_text = session.date().isoformat()
        position = prepared.positions[session_text]
        if position < policy.flat_min_sessions:
            continue
        prior = prices.loc[:session].iloc[:-1]
        expected = detect_proper_base(prior[["High", "Low", "Close"]], event_session=session_text, policy=policy)
        actual = _detect_prepared_base(
            prepared,
            end_pos=position,
            policy=policy,
            input_sha256=prepared.input_sha256_prefixes[position],
        )
        assert actual == expected


def test_materialization_uses_prepared_pattern_inputs_without_public_revalidation(
    mini_pit_bundle: _MiniPITBundle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = cache_paths(tmp_path)
    builder = FactCacheBuilder(
        bundle=mini_pit_bundle,
        rulebook=rulebook_v1(),
        partitions=mini_partitions(),
        output_path=paths[0],
        checkpoint_path=paths[1],
        progress_path=paths[2],
        supplemental_provider=UnavailableSupplementalPITProvider(),
    )
    prefetched = builder._prefetch_prices()

    def public_detector_must_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("public detector was called after pattern preparation")

    monkeypatch.setattr(fact_cache_module, "detect_proper_base", public_detector_must_not_run)

    rows = builder._materialize_session(
        "2024-01-12",
        builder._fundamental_states(["2024-01-12"]),
        prefetched,
    )

    assert len(rows) == len(mini_pit_bundle._symbols)
