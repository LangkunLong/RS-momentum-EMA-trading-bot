from __future__ import annotations

from dataclasses import asdict, replace
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path

import pytest

from core.pit_optimizer_evaluation import (
    AggregateMetric,
    FoldAggregateSummary,
    FoldManifest,
    FoldSpec,
)
from core.pit_policy_parity import (
    ParityAttestation,
    ParityEquityPoint,
    ParityFoldEvidence,
    ParityReference,
)


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
        + b"\n"
    ).hexdigest()


def _sessions(start: date) -> tuple[str, ...]:
    return tuple((start + timedelta(days=index)).isoformat() for index in range(60))


def _fold(fold_id: str, purpose: str, start: date) -> FoldSpec:
    sessions = _sessions(start)
    return FoldSpec(
        fold_id=fold_id,
        purpose=purpose,
        start_date=sessions[0],
        end_date=sessions[-1],
        sessions=sessions,
    )


def _evidence(fold: FoldSpec) -> ParityFoldEvidence:
    aggregate = FoldAggregateSummary(
        fold_id=fold.fold_id,
        total_return_pct=0.0,
        excess_total_return_pp=0.0,
        max_drawdown_pct=0.0,
        sharpe_ratio=0.0,
        closed_trades=0,
        turnover_pct=0.0,
        average_exposure_pct=0.0,
        entry_funnel=(AggregateMetric("entries", 0),),
        exit_attribution=(AggregateMetric("end_of_test", 0),),
    )
    equity = tuple(ParityEquityPoint(session, 1_000.0) for session in fold.sessions)
    primitive = {
        "fold_id": fold.fold_id,
        "transactions": [],
        "entry_outcomes": [],
        "equity": [asdict(item) for item in equity],
        "funnel": [asdict(item) for item in aggregate.entry_funnel],
        "aggregate": asdict(aggregate),
        "effective_policy_sha256": "d" * 64,
    }
    return ParityFoldEvidence(
        fold_id=fold.fold_id,
        transactions=(),
        entry_outcomes=(),
        equity=equity,
        funnel=aggregate.entry_funnel,
        aggregate=aggregate,
        effective_policy_sha256="d" * 64,
        evidence_sha256=_canonical_digest(primitive),
    )


def _attested_reference(tmp_path: Path) -> tuple[ParityAttestation, ParityReference]:
    first = _fold("discovery_1", "discovery", date(2022, 3, 14))
    second = _fold("discovery_2", "discovery", date(2022, 5, 13))
    hidden = _fold("hidden_1", "hidden", date(2022, 7, 12))
    manifest = FoldManifest(
        data_identity_sha256="a" * 64,
        universe_sha256=_canonical_digest(["AAA"]),
        benchmark="SPY",
        warmup_start_date="2021-01-01",
        discovery_folds=(first, second),
        hidden_fold=hidden,
    )
    evidence = (_evidence(first), _evidence(second))
    reference = ParityReference(
        schema_version=1,
        reference_source_head="b" * 40,
        reference_source_fingerprint_sha256="c" * 64,
        readiness_sha256="e" * 64,
        pit_bundle_sha256="a" * 64,
        baseline_manifest_sha256="f" * 64,
        effective_policy_sha256="d" * 64,
        fold_manifest=manifest,
        universe=("AAA",),
        discovery_evidence=evidence,
        discovery_output_sha256s=tuple(
            (item.fold_id, item.evidence_sha256) for item in evidence
        ),
        artifact_path=(tmp_path / "reference.json").resolve(),
        artifact_sha256="1" * 64,
    )
    attestation = ParityAttestation(
        schema_version=1,
        reference_artifact_sha256=reference.artifact_sha256,
        reference_source_head=reference.reference_source_head,
        final_source_head="2" * 40,
        final_source_fingerprint_sha256="3" * 64,
        pit_bundle_sha256=reference.pit_bundle_sha256,
        baseline_manifest_sha256=reference.baseline_manifest_sha256,
        effective_policy_sha256=reference.effective_policy_sha256,
        discovery_fold_manifest_sha256=reference.fold_manifest.sha256,
        policy_interface_version=1,
        reference_output_sha256s=reference.discovery_output_sha256s,
        final_output_sha256s=reference.discovery_output_sha256s,
        final_discovery_evidence=evidence,
        transactions_equal=True,
        entry_outcomes_equal=True,
        equity_equal=True,
        funnels_equal=True,
        effective_policy_equal=True,
        artifact_path=(tmp_path / "attestation.json").resolve(),
        artifact_sha256="4" * 64,
    )
    return attestation, reference


def test_manifest_uses_only_the_fold_manifest_bound_by_the_parity_reference(
    tmp_path: Path,
) -> None:
    """Break caught: a new reference could be silently replaced with the first fixed subset."""
    from core.pit_optimization_contract import _attested_parity_reference_folds

    attestation, reference = _attested_reference(tmp_path)

    folds, universe = _attested_parity_reference_folds(
        parity_attestation=attestation,
        parity_reference=reference,
    )

    assert folds == reference.fold_manifest
    assert universe == reference.universe
    with pytest.raises(ValueError, match="fold manifest"):
        _attested_parity_reference_folds(
            parity_attestation=replace(
                attestation,
                discovery_fold_manifest_sha256="5" * 64,
            ),
            parity_reference=reference,
        )


def test_subset_manifest_cli_accepts_an_attested_parity_reference() -> None:
    """Break caught: the manifest builder could not receive the fresh reference it must authenticate."""
    from core.pit_optimizer_evaluation import _manifest_cli_parser

    parser = _manifest_cli_parser()
    build = next(
        action.choices["build-subset-manifest"]
        for action in parser._actions
        if "build-subset-manifest" in (getattr(action, "choices", None) or {})
    )

    assert "--parity-reference" in {
        option
        for action in build._actions
        for option in action.option_strings
    }
