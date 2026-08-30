"""Behavioral contracts for fold manifests and persisted policy parity."""

from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import subprocess

import pandas as pd
import pytest

import core.pit_policy_parity as parity
from core.backtest_engine import (
    EntryAttemptOutcome,
    PortfolioSimulator,
    SimulationResult,
    Trade,
)
from core.strategy_policy.runtime import InProcessPolicyClient
from core.pit_optimizer_evaluation import (
    AggregateMetric,
    FoldAggregateSummary,
    FoldManifest,
    FoldSpec,
)
from core.pit_policy_parity import (
    ParityEntryOutcome,
    ParityEquityPoint,
    ParityFoldEvidence,
    ParityTransaction,
    build_fixed_fold_manifest,
    build_fold_evidence,
    capture_from_authenticated_inputs,
    load_parity_reference,
    persist_parity_reference,
    simulator_kwargs_from_readiness,
    verify_parity_evidence,
)


def canonical_json_bytes_without_self_digest(
    value: object,
    self_digest: str,
) -> bytes:
    """Independent test oracle for canonical self-digest preimages."""
    primitive = asdict(value) if hasattr(value, "__dataclass_fields__") else dict(value)  # type: ignore[arg-type]
    primitive.pop(self_digest, None)
    primitive.pop("artifact_path", None)
    return (
        json.dumps(
            primitive,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _fold(fold_id: str, purpose: str, start: str) -> FoldSpec:
    sessions = tuple(value.date().isoformat() for value in pd.bdate_range(start=start, periods=60))
    return FoldSpec(
        fold_id=fold_id,
        purpose=purpose,
        start_date=sessions[0],
        end_date=sessions[-1],
        sessions=sessions,
    )


def test_policy_client_factory_is_fresh_for_consecutive_parity_fold_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: fold reuse could leak one stateful policy client across runs."""
    made: list[InProcessPolicyClient] = []
    closed: list[int] = []

    class Client(InProcessPolicyClient):
        def close(self) -> None:
            closed.append(1)

    def factory() -> Client:
        client = Client()
        made.append(client)
        return client

    simulator = PortfolioSimulator(policy_client_factory=factory)
    monkeypatch.setattr(
        simulator,
        "_run_with_policy_client_active",
        lambda *_args, **_kwargs: SimulationResult(),
    )

    for fold in (
        _fold("discovery_1", "discovery", "2021-06-25"),
        _fold("discovery_2", "discovery", "2021-09-21"),
    ):
        simulator.run(
            ["AAA"],
            start_date=fold.start_date,
            end_date=fold.end_date,
        )

    assert len({id(client) for client in made}) == 2
    assert closed == [1, 1]
    assert simulator._policy_client is None


def _aggregate(
    fold_id: str,
    *,
    funnel: tuple[AggregateMetric, ...] | None = None,
) -> FoldAggregateSummary:
    funnel = funnel or (AggregateMetric("entries_executed", 1),)
    exits = (AggregateMetric("end_of_test", 1),)
    return FoldAggregateSummary(
        fold_id=fold_id,
        total_return_pct=1.0,
        excess_total_return_pp=0.5,
        max_drawdown_pct=-0.25,
        sharpe_ratio=1.5,
        closed_trades=1,
        turnover_pct=20.0,
        average_exposure_pct=10.0,
        entry_funnel=funnel,
        exit_attribution=exits,
    )


def _json_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
        + b"\n"
    ).hexdigest()


def _evidence(
    fold: FoldSpec,
    *,
    equity: float = 1_010.0,
    equity_sessions: tuple[str, ...] | None = None,
    transaction_date: str | None = None,
    signal_date: str | None = None,
    entry_date: str | None = None,
    aggregate_funnel: tuple[AggregateMetric, ...] | None = None,
) -> ParityFoldEvidence:
    transaction_date = transaction_date or fold.sessions[1]
    signal_date = signal_date or fold.sessions[0]
    entry_date = entry_date or fold.sessions[1]
    transaction = ParityTransaction(
        date=transaction_date,
        symbol="AAA",
        from_symbol=None,
        action="BUY",
        price=100.0,
        quantity=1.0,
        value=100.0,
        reason="fixture",
    )
    outcome = ParityEntryOutcome(
        symbol="AAA",
        signal_date=signal_date,
        entry_date=entry_date,
        pivot=None,
        buy_zone_lower=None,
        buy_zone_upper=None,
        entry_open=100.0,
        outcome="entries_executed",
    )
    funnel = (AggregateMetric("entries_executed", 1),)
    equity_points = tuple(
        ParityEquityPoint(session, equity)
        for session in (equity_sessions or fold.sessions)
    )
    fields = {
        "fold_id": fold.fold_id,
        "transactions": (transaction,),
        "entry_outcomes": (outcome,),
        "equity": equity_points,
        "funnel": funnel,
        "aggregate": _aggregate(fold.fold_id, funnel=aggregate_funnel),
        "effective_policy_sha256": "d" * 64,
    }
    primitive = {
        "fold_id": fold.fold_id,
        "transactions": [asdict(transaction)],
        "entry_outcomes": [asdict(outcome)],
        "equity": [asdict(point) for point in equity_points],
        "funnel": [asdict(metric) for metric in funnel],
        "aggregate": asdict(fields["aggregate"]),
        "effective_policy_sha256": "d" * 64,
    }
    digest = _json_digest(primitive)
    return ParityFoldEvidence(**fields, evidence_sha256=digest)


def test_fold_manifest_rejects_reused_discovery_sessions() -> None:
    """Break caught: the same market sessions could influence both discovery folds."""
    first = _fold("discovery_1", "discovery", "2021-01-04")
    second = replace(first, fold_id="discovery_2")
    hidden = _fold("hidden_1", "hidden", "2022-01-03")
    with pytest.raises(ValueError, match="overlap"):
        FoldManifest(
            data_identity_sha256="a" * 64,
            universe_sha256="b" * 64,
            benchmark="SPY",
            warmup_start_date="2020-01-02",
            discovery_folds=(first, second),
            hidden_fold=hidden,
        )


def test_fold_manifest_requires_two_chronological_discovery_folds_before_hidden() -> None:
    """Break caught: a short, reordered, or mislabeled fold could be evaluated."""
    first = _fold("discovery_1", "discovery", "2021-01-04")
    second = _fold("discovery_2", "discovery", "2021-04-01")
    hidden = _fold("hidden_1", "hidden", "2022-01-03")
    manifest = FoldManifest(
        data_identity_sha256="4" * 64,
        universe_sha256=_json_digest(["AAA", "BBB"]),
        benchmark="SPY",
        warmup_start_date="2020-01-02",
        discovery_folds=(first, second),
        hidden_fold=hidden,
    )

    assert len(first.sessions) == len(second.sessions) == len(hidden.sessions) == 60
    assert manifest.sha256 == hashlib.sha256(canonical_json_bytes_without_self_digest(manifest, "sha256")).hexdigest()
    with pytest.raises(ValueError, match="chronological"):
        replace(manifest, discovery_folds=(second, first))


def test_reference_persists_canonical_nested_evidence_and_is_create_only(tmp_path: Path) -> None:
    """Break caught: parity hashes replaced retrievable evidence or an artifact was overwritten."""
    first = _fold("discovery_1", "discovery", "2021-01-04")
    second = _fold("discovery_2", "discovery", "2021-04-01")
    manifest = FoldManifest(
        data_identity_sha256="4" * 64,
        universe_sha256=_json_digest(["AAA", "BBB"]),
        benchmark="SPY",
        warmup_start_date="2020-01-02",
        discovery_folds=(first, second),
        hidden_fold=_fold("hidden_1", "hidden", "2022-01-03"),
    )
    output = tmp_path / "reference.json"
    evidence = (_evidence(first), _evidence(second))

    reference = persist_parity_reference(
        output=output,
        reference_source_head="1" * 40,
        reference_source_fingerprint_sha256="2" * 64,
        readiness_sha256="3" * 64,
        pit_bundle_sha256="4" * 64,
        baseline_manifest_sha256="5" * 64,
        effective_policy_sha256="d" * 64,
        fold_manifest=manifest,
        universe=("AAA", "BBB"),
        discovery_evidence=evidence,
    )
    loaded = load_parity_reference(output)

    assert loaded == reference
    assert loaded.discovery_evidence == evidence
    assert output.read_bytes() == canonical_json_bytes_without_self_digest(loaded, "artifact_sha256")
    assert hashlib.sha256(output.read_bytes()).hexdigest() == loaded.artifact_sha256
    with pytest.raises(FileExistsError):
        persist_parity_reference(
            output=output,
            reference_source_head="1" * 40,
            reference_source_fingerprint_sha256="2" * 64,
            readiness_sha256="3" * 64,
            pit_bundle_sha256="4" * 64,
            baseline_manifest_sha256="5" * 64,
            effective_policy_sha256="d" * 64,
            fold_manifest=manifest,
            universe=("AAA", "BBB"),
            discovery_evidence=evidence,
        )


def test_attestation_is_not_written_when_any_retrievable_evidence_differs(tmp_path: Path) -> None:
    """Break caught: matching summary hashes could conceal a changed equity path."""
    first = _fold("discovery_1", "discovery", "2021-01-04")
    second = _fold("discovery_2", "discovery", "2021-04-01")
    manifest = FoldManifest(
        data_identity_sha256="4" * 64,
        universe_sha256=_json_digest(["AAA", "BBB"]),
        benchmark="SPY",
        warmup_start_date="2020-01-02",
        discovery_folds=(first, second),
        hidden_fold=_fold("hidden_1", "hidden", "2022-01-03"),
    )
    reference_path = tmp_path / "reference.json"
    reference_evidence = (_evidence(first), _evidence(second))
    persist_parity_reference(
        output=reference_path,
        reference_source_head="1" * 40,
        reference_source_fingerprint_sha256="2" * 64,
        readiness_sha256="3" * 64,
        pit_bundle_sha256="4" * 64,
        baseline_manifest_sha256="5" * 64,
        effective_policy_sha256="d" * 64,
        fold_manifest=manifest,
        universe=("AAA", "BBB"),
        discovery_evidence=reference_evidence,
    )
    output = tmp_path / "attestation.json"

    with pytest.raises(ValueError, match="equity"):
        verify_parity_evidence(
            reference=load_parity_reference(reference_path),
            output=output,
            final_source_head="6" * 40,
            final_source_fingerprint_sha256="7" * 64,
            policy_interface_version=1,
            final_discovery_evidence=(
                _evidence(first, equity=999.0),
                reference_evidence[1],
            ),
            pre_persist_check=lambda: None,
        )
    assert not output.exists()


def test_fixed_fold_manifest_uses_only_the_supplied_benchmark_calendar() -> None:
    """Break caught: fold construction queried hidden values instead of sealing session IDs."""
    closures = {"2021-07-05", "2021-09-06", "2021-11-25", "2021-12-24", "2022-01-17", "2022-02-21"}
    sessions = tuple(
        value.date().isoformat()
        for value in pd.bdate_range("2021-06-25", "2022-03-11")
        if value.date().isoformat() not in closures
    )
    readiness = {
        "evaluation_contract": {
            "verification_only": True,
            "scope": {
                "benchmark": "SPY",
                "discovery_start": "2021-06-25",
                "discovery_end": "2021-09-20",
                "holdout_start": "2021-09-21",
                "holdout_end": "2021-12-14",
                "warmup_start": "2021-01-01",
                "session_count": 60,
                "symbol_count": 2,
                "symbols": ["AAA", "BBB"],
            },
        }
    }

    manifest, universe = build_fixed_fold_manifest(
        readiness=readiness,
        benchmark_sessions=sessions,
        data_identity_sha256="a" * 64,
    )

    assert universe == ("AAA", "BBB")
    assert [(fold.start_date, fold.end_date) for fold in manifest.discovery_folds] == [
        ("2021-06-25", "2021-09-20"),
        ("2021-09-21", "2021-12-14"),
    ]
    assert (manifest.hidden_fold.start_date, manifest.hidden_fold.end_date) == (
        "2021-12-15",
        "2022-03-11",
    )
    assert all(len(fold.sessions) == 60 for fold in (*manifest.discovery_folds, manifest.hidden_fold))


def test_fixed_fold_manifest_can_seal_a_later_independent_subset() -> None:
    """Break caught: every later canary was forced to reuse the first consumed hidden window."""
    sessions = tuple(
        value.date().isoformat()
        for value in pd.bdate_range("2021-06-25", periods=360)
    )
    first_discovery_session = sessions[120]
    readiness = {
        "evaluation_contract": {
            "verification_only": True,
            "scope": {
                "benchmark": "SPY",
                "discovery_start": "2021-06-25",
                "discovery_end": "2021-09-20",
                "holdout_start": "2021-09-21",
                "holdout_end": "2021-12-14",
                "warmup_start": "2021-01-01",
                "session_count": 60,
                "symbol_count": 2,
                "symbols": ["AAA", "BBB"],
            },
        }
    }

    manifest, _ = build_fixed_fold_manifest(
        readiness=readiness,
        benchmark_sessions=sessions,
        data_identity_sha256="a" * 64,
        first_discovery_session=first_discovery_session,
    )

    folds = (*manifest.discovery_folds, manifest.hidden_fold)
    assert [fold.sessions[0] for fold in folds] == [
        sessions[120],
        sessions[180],
        sessions[240],
    ]
    assert [fold.sessions[-1] for fold in folds] == [
        sessions[179],
        sessions[239],
        sessions[299],
    ]


def test_fold_evidence_embeds_exact_result_rows_and_hand_checked_aggregates() -> None:
    """Break caught: capture emitted only hashes or computed aggregates from a different result."""
    fold = _fold("discovery_1", "discovery", "2021-01-04")
    equity = pd.Series([1_000.0] * 60, index=fold.sessions)
    transactions = pd.DataFrame(
        [
            {
                "Date": fold.sessions[0],
                "Ticker": "AAA",
                "Action": "BUY",
                "Price": 100.0,
                "Quantity": 1.0,
                "Value": 100.0,
                "Reason": "fixture",
            },
            {
                "Date": fold.sessions[-1],
                "Ticker": "AAA",
                "Action": "SELL",
                "Price": 100.0,
                "Quantity": 1.0,
                "Value": 100.0,
                "Reason": "end_of_test",
            },
        ]
    )
    trade = Trade("AAA", fold.sessions[0], 100.0, 1.0, 90.0)
    trade.exit_date = fold.sessions[-1]
    trade.exit_price = 100.0
    trade.exit_reason = "end_of_test"
    outcome = EntryAttemptOutcome(
        "AAA", fold.sessions[0], fold.sessions[1], None, None, None, 100.0, "entries_executed"
    )
    result = SimulationResult(
        trades=[trade],
        equity_curve=equity,
        benchmark_curve=equity,
        initial_capital=1_000.0,
        config={
            "candidate_universe_count": 1,
            "rs_universe_count": 1,
            "effective_engine_policy_sha256": "d" * 64,
        },
        transaction_log=transactions,
        signal_log=pd.DataFrame(),
        entry_outcomes=(outcome,),
    )

    evidence = build_fold_evidence(fold=fold, result=result)

    assert len(evidence.transactions) == 2
    assert evidence.entry_outcomes == (
        ParityEntryOutcome("AAA", fold.sessions[0], fold.sessions[1], None, None, None, 100.0, "entries_executed"),
    )
    assert tuple(point.session for point in evidence.equity) == fold.sessions
    assert evidence.aggregate.total_return_pct == 0.0
    assert evidence.aggregate.excess_total_return_pp == 0.0
    assert evidence.aggregate.closed_trades == 1
    assert evidence.aggregate.turnover_pct == 20.0
    assert evidence.aggregate.average_exposure_pct == pytest.approx(59 / 6)
    assert dict((item.metric_id, item.value) for item in evidence.aggregate.exit_attribution) == {"end_of_test": 1}


def test_capture_evaluates_only_discovery_and_seals_hidden_calendar(tmp_path: Path) -> None:
    """Break caught: reference capture evaluated hidden data or omitted its sealed boundary."""
    closures = {"2021-07-05", "2021-09-06", "2021-11-25", "2021-12-24", "2022-01-17", "2022-02-21"}
    sessions = tuple(
        value.date().isoformat()
        for value in pd.bdate_range("2021-06-25", "2022-03-11")
        if value.date().isoformat() not in closures
    )
    readiness = {
        "evaluation_contract": {
            "verification_only": True,
            "scope": {
                "benchmark": "SPY",
                "discovery_start": "2021-06-25",
                "discovery_end": "2021-09-20",
                "holdout_start": "2021-09-21",
                "holdout_end": "2021-12-14",
                "warmup_start": "2021-01-01",
                "session_count": 60,
                "symbol_count": 2,
                "symbols": ["AAA", "BBB"],
            },
        },
        "identities": {
            "pit_bundle_sha256": "4" * 64,
            "baseline_manifest_sha256": "5" * 64,
            "effective_policy_sha256": "d" * 64,
        },
    }
    evaluated: list[str] = []

    def evaluate(fold: FoldSpec, _universe: tuple[str, ...], _warmup: str) -> ParityFoldEvidence:
        evaluated.append(fold.fold_id)
        return _evidence(fold)

    reference = capture_from_authenticated_inputs(
        readiness=readiness,
        readiness_sha256="3" * 64,
        pit_bundle_sha256="4" * 64,
        reference_source_head="1" * 40,
        reference_source_fingerprint_sha256="2" * 64,
        benchmark_sessions=sessions,
        output=tmp_path / "reference.json",
        evaluate_discovery_fold=evaluate,
        pre_persist_check=lambda: None,
    )

    assert evaluated == ["discovery_1", "discovery_2"]
    assert reference.fold_manifest.hidden_fold.fold_id == "hidden_1"
    assert len(reference.discovery_evidence) == 2


def test_capture_carries_a_later_subset_selection_into_the_reference(
    tmp_path: Path,
) -> None:
    """Break caught: a manifest could claim a fresh window while parity still sealed the original one."""
    sessions = tuple(
        value.date().isoformat()
        for value in pd.bdate_range("2021-06-25", periods=360)
    )
    readiness = {
        "evaluation_contract": {
            "verification_only": True,
            "scope": {
                "benchmark": "SPY",
                "discovery_start": "2021-06-25",
                "discovery_end": "2021-09-20",
                "holdout_start": "2021-09-21",
                "holdout_end": "2021-12-14",
                "warmup_start": "2021-01-01",
                "session_count": 60,
                "symbol_count": 2,
                "symbols": ["AAA", "BBB"],
            },
        },
        "identities": {
            "pit_bundle_sha256": "4" * 64,
            "baseline_manifest_sha256": "5" * 64,
            "effective_policy_sha256": "d" * 64,
        },
    }
    evaluated: list[tuple[str, str]] = []

    def evaluate(
        fold: FoldSpec,
        _universe: tuple[str, ...],
        _warmup: str,
    ) -> ParityFoldEvidence:
        evaluated.append((fold.fold_id, fold.start_date))
        return _evidence(fold)

    reference = capture_from_authenticated_inputs(
        readiness=readiness,
        readiness_sha256="3" * 64,
        pit_bundle_sha256="4" * 64,
        reference_source_head="1" * 40,
        reference_source_fingerprint_sha256="2" * 64,
        benchmark_sessions=sessions,
        first_discovery_session=sessions[120],
        output=tmp_path / "later-reference.json",
        evaluate_discovery_fold=evaluate,
        pre_persist_check=lambda: None,
    )

    assert evaluated == [
        ("discovery_1", sessions[120]),
        ("discovery_2", sessions[180]),
    ]
    assert reference.fold_manifest.hidden_fold.sessions == sessions[240:300]


def test_parity_capture_cli_accepts_a_later_subset_start() -> None:
    """Break caught: the selectable fresh subset was available only to in-process callers."""
    parser = parity._parser()

    args = parser.parse_args(
        [
            "capture",
            "--readiness",
            "C:/artifacts/readiness.json",
            "--pit-bundle",
            "C:/artifacts/pit.sqlite3",
            "--output",
            "C:/artifacts/reference.json",
            "--first-discovery-session",
            "2022-03-14",
        ]
    )

    assert args.first_discovery_session == "2022-03-14"


def test_capture_simulator_binds_authenticated_signal_cadence() -> None:
    """Break caught: reference capture silently used the engine's five-day default cadence."""
    readiness = {
        "effective_policy": {
            "entry_policy": {
                "signal_every_n_days": {
                    "classification": "active_tunable_policy",
                    "optimizer_candidate": True,
                    "source": "core.backtest_engine.PortfolioSimulator.signal_every_n_days",
                    "value": 1,
                }
            }
        }
    }

    assert simulator_kwargs_from_readiness(readiness) == {"signal_every_n_days": 1}


def test_attestation_rejects_the_reference_source_head(tmp_path: Path) -> None:
    """Break caught: capture HEAD could immediately attest itself as post-extraction."""
    first = _fold("discovery_1", "discovery", "2021-01-04")
    second = _fold("discovery_2", "discovery", "2021-04-01")
    reference_path = tmp_path / "reference.json"
    reference = persist_parity_reference(
        output=reference_path,
        reference_source_head="1" * 40,
        reference_source_fingerprint_sha256="2" * 64,
        readiness_sha256="3" * 64,
        pit_bundle_sha256="4" * 64,
        baseline_manifest_sha256="5" * 64,
        effective_policy_sha256="d" * 64,
        fold_manifest=FoldManifest(
            data_identity_sha256="4" * 64,
            universe_sha256=_json_digest(["AAA", "BBB"]),
            benchmark="SPY",
            warmup_start_date="2020-01-02",
            discovery_folds=(first, second),
            hidden_fold=_fold("hidden_1", "hidden", "2022-01-03"),
        ),
        universe=("AAA", "BBB"),
        discovery_evidence=(_evidence(first), _evidence(second)),
    )

    with pytest.raises(ValueError, match="later source HEAD"):
        verify_parity_evidence(
            reference=reference,
            output=tmp_path / "attestation.json",
            final_source_head=reference.reference_source_head,
            final_source_fingerprint_sha256="7" * 64,
            policy_interface_version=1,
            final_discovery_evidence=reference.discovery_evidence,
            pre_persist_check=lambda: None,
        )
    assert not (tmp_path / "attestation.json").exists()


def test_verification_rejects_a_non_descendant_source_head(tmp_path: Path) -> None:
    """Break caught: an unrelated commit could be called the final extracted source."""
    repository = tmp_path / "repository"
    repository.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=repository,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()

    git("init")
    git("config", "user.name", "Parity Test")
    git("config", "user.email", "parity@example.invalid")
    (repository / "tracked.txt").write_text("reference\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("commit", "-m", "reference")
    reference_head = git("rev-parse", "HEAD")
    git("switch", "--orphan", "unrelated")
    (repository / "other.txt").write_text("unrelated\n", encoding="utf-8")
    git("add", "other.txt")
    git("commit", "-m", "unrelated")
    final_head = git("rev-parse", "HEAD")

    with pytest.raises(ValueError, match="descendant"):
        parity._require_later_descendant_source(
            source_root=repository,
            reference_head=reference_head,
            final_head=final_head,
        )


def test_readiness_authentication_rejects_a_canonical_incomplete_contract(
    tmp_path: Path,
) -> None:
    """Break caught: canonical bytes alone could masquerade as closed readiness."""
    policy = {"schema_version": 1}
    policy_sha256 = hashlib.sha256(
        json.dumps(policy, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    ).hexdigest()
    readiness = {
        "effective_policy": policy,
        "identities": {"effective_policy_sha256": policy_sha256},
    }
    path = tmp_path / "readiness.json"
    path.write_bytes(
        json.dumps(readiness, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )

    with pytest.raises(ValueError, match="closed readiness contract"):
        parity._authenticated_readiness(
            path,
            source_root=Path(__file__).resolve().parents[1],
        )


def test_readiness_authentication_rejects_tampered_candidate_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: known candidate IDs could carry unauthenticated edit records."""
    import core.pit_optimization as optimization
    from core.engine_policy import effective_engine_policy_sha256

    policy = {"schema_version": 1}
    policy_sha256 = effective_engine_policy_sha256(policy)
    readiness = {
        "schema_version": 1,
        "gate": "pit_optimization",
        "phase": "ready",
        "identities": {
            "source_head": "1" * 40,
            "source_fingerprint_sha256": "2" * 64,
            "entry_contract_source_sha256": "3" * 64,
            "pit_bundle_sha256": optimization.PIT_BUNDLE_SHA256,
            "baseline_manifest_sha256": optimization.BASELINE_MANIFEST_SHA256,
            "baseline_source_commit": optimization.BASELINE_SOURCE_COMMIT,
            "effective_policy_sha256": policy_sha256,
            "prior_discovery_feedback_sha256": None,
        },
        "sealed_inputs": {
            "pit_bundle_sha256": optimization.PIT_BUNDLE_SHA256,
            "baseline_artifact_sha256": {
                "run_manifest.json": optimization.BASELINE_MANIFEST_SHA256
            },
            "prior_discovery_feedback_sha256": None,
        },
        "date_contract": {
            "full_start": optimization.FULL_START_DATE,
            "full_end": optimization.FULL_END_DATE,
            "holdout_start": optimization.HOLDOUT_START_DATE,
            "holdout_end": optimization.HOLDOUT_END_DATE,
        },
        "budget_contract": {},
        "evaluation_contract": {},
        "candidate_catalog": [
            {"candidate_id": "min_rs_score_075", "new_line": "TAMPERED = True"}
        ],
        "effective_policy": policy,
        "baseline": {},
        "prior_discovery_feedback": [],
        "evidence_ids": list(optimization._VERIFICATION_EVIDENCE_IDS),
        "invariant_ids": list(optimization._INVARIANT_IDS),
    }
    path = tmp_path / "readiness.json"
    path.write_bytes(
        json.dumps(readiness, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    monkeypatch.setattr(optimization, "_verify_policy_catalog", lambda _policy: None)
    monkeypatch.setattr(optimization, "_provider_payload", lambda _readiness: {})
    monkeypatch.setattr(optimization, "_readiness_identity", lambda *_args, **_kwargs: None)

    with pytest.raises(ValueError, match="candidate catalog changed"):
        parity._authenticated_readiness(path, source_root=tmp_path)


def test_source_identity_rejects_nonignored_untracked_files(tmp_path: Path) -> None:
    """Break caught: untracked executable input was compatible with a clean source claim."""
    repository = tmp_path / "repository"
    repository.mkdir()

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=repository,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    git("init")
    git("config", "user.name", "Parity Test")
    git("config", "user.email", "parity@example.invalid")
    (repository / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("commit", "-m", "tracked")
    (repository / "runtime_policy.py").write_text("ACTIVE = True\n", encoding="utf-8")

    with pytest.raises(ValueError, match="clean committed source"):
        parity._source_identity(repository)


def test_capture_rechecks_source_after_evaluation_before_write(tmp_path: Path) -> None:
    """Break caught: capture could publish after source changed during evaluation."""
    closures = {
        "2021-07-05",
        "2021-09-06",
        "2021-11-25",
        "2021-12-24",
        "2022-01-17",
        "2022-02-21",
    }
    sessions = tuple(
        value.date().isoformat()
        for value in pd.bdate_range("2021-06-25", "2022-03-11")
        if value.date().isoformat() not in closures
    )
    readiness = {
        "evaluation_contract": {
            "verification_only": True,
            "scope": {
                "benchmark": "SPY",
                "discovery_start": "2021-06-25",
                "discovery_end": "2021-09-20",
                "holdout_start": "2021-09-21",
                "holdout_end": "2021-12-14",
                "warmup_start": "2021-01-01",
                "session_count": 60,
                "symbol_count": 2,
                "symbols": ["AAA", "BBB"],
            },
        },
        "identities": {
            "pit_bundle_sha256": "4" * 64,
            "baseline_manifest_sha256": "5" * 64,
            "effective_policy_sha256": "d" * 64,
        },
    }

    def reject_drift() -> None:
        raise ValueError("source changed during parity evaluation")

    output = tmp_path / "reference.json"
    with pytest.raises(ValueError, match="source changed"):
        capture_from_authenticated_inputs(
            readiness=readiness,
            readiness_sha256="3" * 64,
            pit_bundle_sha256="4" * 64,
            reference_source_head="1" * 40,
            reference_source_fingerprint_sha256="2" * 64,
            benchmark_sessions=sessions,
            output=output,
            evaluate_discovery_fold=lambda fold, _universe, _warmup: _evidence(
                fold
            ),
            pre_persist_check=reject_drift,
        )
    assert not output.exists()


def test_attestation_rechecks_source_after_evaluation_before_write(tmp_path: Path) -> None:
    """Break caught: verification could publish after source changed during evaluation."""
    first = _fold("discovery_1", "discovery", "2021-01-04")
    second = _fold("discovery_2", "discovery", "2021-04-01")
    reference = persist_parity_reference(
        output=tmp_path / "reference.json",
        reference_source_head="1" * 40,
        reference_source_fingerprint_sha256="2" * 64,
        readiness_sha256="3" * 64,
        pit_bundle_sha256="4" * 64,
        baseline_manifest_sha256="5" * 64,
        effective_policy_sha256="d" * 64,
        fold_manifest=FoldManifest(
            data_identity_sha256="4" * 64,
            universe_sha256=_json_digest(["AAA", "BBB"]),
            benchmark="SPY",
            warmup_start_date="2020-01-02",
            discovery_folds=(first, second),
            hidden_fold=_fold("hidden_1", "hidden", "2022-01-03"),
        ),
        universe=("AAA", "BBB"),
        discovery_evidence=(_evidence(first), _evidence(second)),
    )

    def reject_drift() -> None:
        raise ValueError("source changed during parity evaluation")

    output = tmp_path / "attestation.json"
    with pytest.raises(ValueError, match="source changed"):
        verify_parity_evidence(
            reference=reference,
            output=output,
            final_source_head="6" * 40,
            final_source_fingerprint_sha256="7" * 64,
            policy_interface_version=1,
            final_discovery_evidence=reference.discovery_evidence,
            pre_persist_check=reject_drift,
        )
    assert not output.exists()


@pytest.mark.parametrize(
    ("data_identity_sha256", "universe_sha256", "message"),
    [
        ("a" * 64, _json_digest(["AAA", "BBB"]), "data identity"),
        ("4" * 64, "b" * 64, "universe identity"),
    ],
)
def test_reference_rejects_manifest_identity_crosslink_mismatch(
    tmp_path: Path,
    data_identity_sha256: str,
    universe_sha256: str,
    message: str,
) -> None:
    """Break caught: embedded evidence could be attributed to different sealed inputs."""
    first = _fold("discovery_1", "discovery", "2021-01-04")
    second = _fold("discovery_2", "discovery", "2021-04-01")

    with pytest.raises(ValueError, match=message):
        persist_parity_reference(
            output=tmp_path / "reference.json",
            reference_source_head="1" * 40,
            reference_source_fingerprint_sha256="2" * 64,
            readiness_sha256="3" * 64,
            pit_bundle_sha256="4" * 64,
            baseline_manifest_sha256="5" * 64,
            effective_policy_sha256="d" * 64,
            fold_manifest=FoldManifest(
                data_identity_sha256=data_identity_sha256,
                universe_sha256=universe_sha256,
                benchmark="SPY",
                warmup_start_date="2020-01-02",
                discovery_folds=(first, second),
                hidden_fold=_fold("hidden_1", "hidden", "2022-01-03"),
            ),
            universe=("AAA", "BBB"),
            discovery_evidence=(_evidence(first), _evidence(second)),
        )
    assert not (tmp_path / "reference.json").exists()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("equity", "equity sessions"),
        ("funnel", "entry funnel"),
        ("transaction", "transaction date"),
        ("signal", "signal date"),
        ("entry", "entry date"),
    ],
)
def test_reference_rejects_fold_evidence_crosslink_mismatch(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    """Break caught: self-consistent evidence could be attached to the wrong fold."""
    first = _fold("discovery_1", "discovery", "2021-01-04")
    second = _fold("discovery_2", "discovery", "2021-04-01")
    kwargs: dict[str, object] = {}
    if mutation == "equity":
        kwargs["equity_sessions"] = first.sessions[:-1]
    elif mutation == "funnel":
        kwargs["aggregate_funnel"] = (AggregateMetric("other", 1),)
    elif mutation == "transaction":
        kwargs["transaction_date"] = "2020-12-31"
    elif mutation == "signal":
        kwargs["signal_date"] = "2020-12-31"
    else:
        kwargs["entry_date"] = "2020-12-31"

    with pytest.raises(ValueError, match=message):
        persist_parity_reference(
            output=tmp_path / "reference.json",
            reference_source_head="1" * 40,
            reference_source_fingerprint_sha256="2" * 64,
            readiness_sha256="3" * 64,
            pit_bundle_sha256="4" * 64,
            baseline_manifest_sha256="5" * 64,
            effective_policy_sha256="d" * 64,
            fold_manifest=FoldManifest(
                data_identity_sha256="4" * 64,
                universe_sha256=_json_digest(["AAA", "BBB"]),
                benchmark="SPY",
                warmup_start_date="2020-01-02",
                discovery_folds=(first, second),
                hidden_fold=_fold("hidden_1", "hidden", "2022-01-03"),
            ),
            universe=("AAA", "BBB"),
            discovery_evidence=(_evidence(first, **kwargs), _evidence(second)),
        )
    assert not (tmp_path / "reference.json").exists()
