"""Behavioral contracts for fold manifests and persisted policy parity."""

from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from core.backtest_engine import EntryAttemptOutcome, SimulationResult, Trade
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


def _aggregate(fold_id: str) -> FoldAggregateSummary:
    funnel = (AggregateMetric("entries_executed", 1),)
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


def _evidence(fold_id: str, *, equity: float = 1_010.0) -> ParityFoldEvidence:
    transaction = ParityTransaction(
        date="2021-01-05",
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
        signal_date="2021-01-04",
        entry_date="2021-01-05",
        pivot=None,
        buy_zone_lower=None,
        buy_zone_upper=None,
        entry_open=100.0,
        outcome="entries_executed",
    )
    fields = {
        "fold_id": fold_id,
        "transactions": (transaction,),
        "entry_outcomes": (outcome,),
        "equity": (ParityEquityPoint("2021-01-05", equity),),
        "funnel": (AggregateMetric("entries_executed", 1),),
        "aggregate": _aggregate(fold_id),
        "effective_policy_sha256": "d" * 64,
    }
    primitive = {
        "fold_id": fold_id,
        "transactions": [asdict(transaction)],
        "entry_outcomes": [asdict(outcome)],
        "equity": [asdict(fields["equity"][0])],
        "funnel": [asdict(fields["funnel"][0])],
        "aggregate": asdict(fields["aggregate"]),
        "effective_policy_sha256": "d" * 64,
    }
    digest = hashlib.sha256(
        json.dumps(primitive, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
    ).hexdigest()
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
        data_identity_sha256="a" * 64,
        universe_sha256="b" * 64,
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
        data_identity_sha256="a" * 64,
        universe_sha256="b" * 64,
        benchmark="SPY",
        warmup_start_date="2020-01-02",
        discovery_folds=(first, second),
        hidden_fold=_fold("hidden_1", "hidden", "2022-01-03"),
    )
    output = tmp_path / "reference.json"
    evidence = (_evidence("discovery_1"), _evidence("discovery_2"))

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
        data_identity_sha256="a" * 64,
        universe_sha256="b" * 64,
        benchmark="SPY",
        warmup_start_date="2020-01-02",
        discovery_folds=(first, second),
        hidden_fold=_fold("hidden_1", "hidden", "2022-01-03"),
    )
    reference_path = tmp_path / "reference.json"
    reference_evidence = (_evidence("discovery_1"), _evidence("discovery_2"))
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
                _evidence("discovery_1", equity=999.0),
                reference_evidence[1],
            ),
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
        return _evidence(fold.fold_id)

    reference = capture_from_authenticated_inputs(
        readiness=readiness,
        readiness_sha256="3" * 64,
        pit_bundle_sha256="4" * 64,
        reference_source_head="1" * 40,
        reference_source_fingerprint_sha256="2" * 64,
        benchmark_sessions=sessions,
        output=tmp_path / "reference.json",
        evaluate_discovery_fold=evaluate,
    )

    assert evaluated == ["discovery_1", "discovery_2"]
    assert reference.fold_manifest.hidden_fold.fold_id == "hidden_1"
    assert len(reference.discovery_evidence) == 2


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
