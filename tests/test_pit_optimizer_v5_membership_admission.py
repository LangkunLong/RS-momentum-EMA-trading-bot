"""V5 production admission rejects labeled nonproduction V3 PIT bundles."""

from __future__ import annotations

from pathlib import Path

import pytest

import build_pit_bundle as bundle_builder
from core.pit_data import (
    PITDataBundle,
    require_v5_production_membership_admission,
    sha256_file,
)
from core.pit_optimizer_v5.artifacts import LocalArtifactRepositoryV5
from core.pit_optimizer_v5.contracts import (
    ArtifactRefV5,
    EVALUATOR_SOURCE_PATHS_V5,
    evaluator_source_map_v5,
    evaluator_source_sha256,
)
from core.pit_optimizer_v5.evaluator import PitPanelEvaluatorV5
from core.pit_optimizer_v5.image_manifest import verify_installed_evaluator_source_v5
from core.pit_optimizer_v5.panels import load_bundle_panel_authority_v5
from core.pit_provenance import (
    PIT_NON_TRADABLE_REFERENCE_SYMBOLS,
    pit_canonical_json,
    pit_canonical_json_sha256,
)


def _labeled_v3_bundle(tmp_path: Path) -> tuple[Path, Path, str]:
    contracts: dict[str, dict[str, object]] = {}
    for ticker, lineage in (
        ("FIXT", "fixture_lineage"),
        ("IWM", "ref_iwm"),
        ("QQQ", "ref_qqq"),
        ("SPY", "ref_spy"),
    ):
        contracts[ticker] = {
            "provider_symbol": ticker,
            "identity_asof": "2020-01-03",
            "admitted_start": "2020-01-02",
            "admitted_end": "2020-01-03",
            "chain_id": lineage,
            "continuity_kind": "same_issuer_rename",
            "warmup_predecessor": None,
            "factor_anchor": True,
        }
    contract_sha = pit_canonical_json_sha256(contracts)
    identity_map_sha = "b" * 64
    transitions: list[dict[str, object]] = []
    provenance = {
        "price_identity_request_contracts": contracts,
        "price_identity_request_contracts_sha256": contract_sha,
        "price_identity_map_sha256": identity_map_sha,
        "price_identity_transitions": transitions,
    }
    provenance_path = tmp_path / "prices-provenance.json"
    provenance_path.write_text(pit_canonical_json(provenance) + "\n", encoding="utf-8")
    provenance_sha = sha256_file(provenance_path)
    digest = "a" * 64
    references = list(PIT_NON_TRADABLE_REFERENCE_SYMBOLS)
    metadata = {
        "bundle_kind": "canslim_pit_v3",
        "schema_version": "3",
        "data_cutoff": "2020-01-03",
        "evaluation_start": "2020-01-03",
        "warmup_start": "2020-01-02",
        "membership_admission_status": "nonproduction_fixture",
        "membership_source_evidence_mode": "nonproduction_fixture",
        "membership_source_sha256": digest,
        "prices_source_sha256": digest,
        "fundamentals_source_sha256": digest,
        "membership_provenance_sha256": digest,
        "prices_provenance_sha256": provenance_sha,
        "fundamentals_provenance_sha256": digest,
        "membership_source_kind": "normalized_three_universe_membership",
        "membership_revision_id": digest,
        "membership_raw_sha256": digest,
        "membership_symbol_map_sha256": digest,
        "membership_security_names_sha256": digest,
        "prices_source_kind": "synthetic fixture",
        "prices_upstream_source_sha256": digest,
        "spy_trading_days_sha256": digest,
        "price_identity_map_sha256": identity_map_sha,
        "price_identity_request_contracts_sha256": contract_sha,
        "price_identity_transitions_sha256": pit_canonical_json_sha256(transitions),
        "price_exclusion_count": "0",
        "price_exclusions_sha256": digest,
        "fundamentals_source_kind": "synthetic fixture",
        "fundamentals_submissions_archive_sha256": digest,
        "fundamentals_companyfacts_archive_sha256": digest,
        "fundamentals_identity_manifest_csv_sha256": digest,
        "non_tradable_reference_symbols_json": pit_canonical_json(references),
        "non_tradable_reference_symbols_sha256": pit_canonical_json_sha256(references),
        "source_universes_json": pit_canonical_json(
            ["nasdaq100", "russell2000", "sp500"]
        ),
    }
    bundle_path = tmp_path / "pit-v3-fixture.sqlite3"
    dates = ("2020-01-02", "2020-01-03")
    price_rows = [
        (day, ticker, 100.0, 101.0, 99.0, 100.0, 1000.0)
        for day in dates
        for ticker in (*references, "FIXT")
    ]
    bundle_builder._create_bundle_v3(
        bundle_path,
        metadata=metadata,
        membership=[("2020-01-02", "fixture_lineage", "sp500", 1)],
        prices=price_rows,
        fundamentals=[
            ("FIXT", "quarterly", "2019-12-31", "2020-01-02", *([None] * 10))
        ],
        industry=[("FIXT", "2020-01-03", "fixture", 1, "FIXT", "[]")],
    )
    return bundle_path, provenance_path, sha256_file(bundle_path)


def test_labeled_nonproduction_v3_fixture_is_readable_but_rejected_by_v5_evaluator(
    tmp_path: Path,
) -> None:
    bundle_path, provenance_path, bundle_sha = _labeled_v3_bundle(tmp_path)
    with PITDataBundle(
        bundle_path,
        expected_sha256=bundle_sha,
        prices_provenance=provenance_path,
    ) as bundle:
        assert bundle.metadata["membership_admission_status"] == "nonproduction_fixture"
        assert bundle.tradable_symbols() == ("FIXT",)
        with pytest.raises(ValueError, match="production membership admission"):
            PitPanelEvaluatorV5(
                contract=None,  # type: ignore[arg-type]
                sandbox_profile=None,  # type: ignore[arg-type]
                resource_manifest=None,  # type: ignore[arg-type]
                execution_profile=None,  # type: ignore[arg-type]
                pit_bundle=bundle,
                prices_provenance=provenance_path,
                report_builder=lambda **_kwargs: None,
                pit_data_scope="production",
            )


def test_labeled_nonproduction_v3_fixture_is_rejected_by_panel_authority_path(
    tmp_path: Path,
) -> None:
    bundle_path, provenance_path, bundle_sha = _labeled_v3_bundle(tmp_path)
    provenance_sha = sha256_file(provenance_path)
    repository = LocalArtifactRepositoryV5(tmp_path)

    with pytest.raises(ValueError, match="production membership admission"):
        load_bundle_panel_authority_v5(
            repository=repository,
            pit_bundle_ref=ArtifactRefV5(bundle_path.name, bundle_sha),
            prices_provenance_ref=ArtifactRefV5(
                provenance_path.name, provenance_sha
            ),
            start_date="2020-01-03",
            end_date="2020-01-03",
        )


@pytest.mark.parametrize(
    "metadata",
    (
        {},
        {"membership_admission_status": "production"},
        {
            "membership_admission_status": "production",
            "membership_source_evidence_mode": "unreviewed_mode",
        },
        {
            "membership_admission_status": "nonproduction_fixture",
            "membership_source_evidence_mode": "nonproduction_fixture",
        },
    ),
)
def test_v5_production_membership_admission_rejects_missing_or_unapproved_pairs(
    metadata: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match="production membership admission"):
        require_v5_production_membership_admission(
            metadata, consumer="test consumer"
        )


def test_admission_helper_is_in_closed_container_source_map() -> None:
    import core.pit_optimizer_v5.container_entry as container_entry

    source_root = Path(__file__).resolve().parents[1]
    source_map = evaluator_source_map_v5(source_root)
    source_identity = evaluator_source_sha256(source_map)

    assert len(EVALUATOR_SOURCE_PATHS_V5) == 58
    assert "core/pit_data.py" in source_map
    assert "core/alpaca_client_policy.py" in source_map
    assert "core/scheduler_observation.py" in source_map
    dockerignore = (
        source_root / "Dockerfile.pit-optimizer-v5.dockerignore"
    ).read_text(encoding="utf-8").splitlines()
    allowed_files = {
        line[1:]
        for line in dockerignore
        if line.startswith("!") and not line.endswith("/")
    }
    assert allowed_files == set(EVALUATOR_SOURCE_PATHS_V5) | {
        "Dockerfile.pit-optimizer-v5",
        "Dockerfile.pit-optimizer-v5.dockerignore",
        "requirements-lock.txt",
    }
    assert "core/pit_optimizer_v5/evaluator.py" in source_map
    assert "core/pit_optimizer_v5/pit_admission.py" not in source_map
    assert container_entry.PitPanelEvaluatorV5 is PitPanelEvaluatorV5
    assert verify_installed_evaluator_source_v5(
        source_root=source_root, expected_sha256=source_identity
    ) == source_identity


def test_installed_image_authenticates_observation_dependency(tmp_path: Path) -> None:
    source_root = Path(__file__).resolve().parents[1]
    source_map = evaluator_source_map_v5(source_root)
    expected = evaluator_source_sha256(source_map)
    for relative in EVALUATOR_SOURCE_PATHS_V5:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((source_root / relative).read_bytes())
    observation = tmp_path / "core/scheduler_observation.py"
    assert observation.is_file(), "installed provider imports require observation code"
    observation.write_bytes(observation.read_bytes() + b"\n# altered dependency\n")
    with pytest.raises(ValueError, match="differs from the expected source map"):
        verify_installed_evaluator_source_v5(source_root=tmp_path, expected_sha256=expected)
    observation.unlink()
    with pytest.raises(ValueError, match="required evaluator source is unavailable"):
        verify_installed_evaluator_source_v5(source_root=tmp_path, expected_sha256=expected)
