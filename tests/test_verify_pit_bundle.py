"""Read-only bundle verifier integration boundary tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import verify_pit_bundle as bundle_verifier
from core.pit_data import sha256_file


def test_v3_verifier_forwards_validated_segment_contract_to_integrity_gate(
    tmp_path: Path, monkeypatch
) -> None:
    """Check the returned contract reaches the gate without invoking admission."""
    builder = bundle_verifier.bundle_builder
    source_names = (
        "membership_csv",
        "prices_csv",
        "fundamentals_csv",
        "industry_csv",
        "membership_provenance",
        "prices_provenance",
        "fundamentals_provenance",
        "industry_provenance",
    )
    source_files = {name: tmp_path / f"{name}.input" for name in source_names}
    for name, path in source_files.items():
        path.write_text(name, encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}\n", encoding="utf-8")
    args = SimpleNamespace(
        **{name: str(path) for name, path in source_files.items()}
    )
    bundle_metadata = {
        "data_cutoff": "2025-12-31",
        "evaluation_start": "2024-01-02",
        "warmup_start": "2020-01-02",
    }
    metadata_keys = {
        "membership_csv": "membership_source_sha256",
        "prices_csv": "prices_source_sha256",
        "fundamentals_csv": "fundamentals_source_sha256",
        "industry_csv": "industry_source_sha256",
        "membership_provenance": "membership_provenance_sha256",
        "prices_provenance": "prices_provenance_sha256",
        "fundamentals_provenance": "fundamentals_provenance_sha256",
        "industry_provenance": "industry_provenance_sha256",
    }
    bundle_metadata.update(
        {
            metadata_key: sha256_file(source_files[name])
            for name, metadata_key in metadata_keys.items()
        }
    )
    universes = builder._V3_SOURCE_UNIVERSES
    affiliations = {"fiserv": frozenset(universes)}
    bundle = SimpleNamespace(
        metadata=bundle_metadata,
        security_lineages_at=lambda _as_of: frozenset(affiliations),
        members_at=lambda _as_of: frozenset({"FISV"}),
        membership_v3=SimpleNamespace(
            lineage_affiliations_at=lambda _as_of: affiliations
        ),
    )
    segment_contract = object()
    monkeypatch.setattr(
        builder,
        "_json_input_v3",
        lambda path, *, label: (Path(path), {}),
    )
    monkeypatch.setattr(builder, "_load_membership_v3", lambda *_args: [])
    monkeypatch.setattr(builder, "_load_prices", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(builder, "_load_fundamentals", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(builder, "_load_industry", lambda *_args: [])
    monkeypatch.setattr(
        builder,
        "_v3_provenance_metadata",
        lambda **_kwargs: ({}, set(), {}, (), segment_contract),
    )
    monkeypatch.setattr(
        builder,
        "_v3_active_lineages",
        lambda _membership, _as_of: {
            universe: {"fiserv"} for universe in universes
        },
    )
    monkeypatch.setattr(builder, "_manifest_with_v3_industry", lambda value, _rows: value)
    monkeypatch.setattr(bundle_verifier, "_database_rows", lambda *_args: [])
    gate_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        builder, "_integrity_gate_v3", lambda **kwargs: gate_calls.append(kwargs)
    )

    bundle_verifier._verify_v3(
        bundle=bundle,
        bundle_path=tmp_path / "bundle.sqlite3",
        args=args,
        manifest={},
        manifest_path=manifest_path,
        manifest_sha256=sha256_file(manifest_path),
    )

    assert len(gate_calls) == 1
    assert gate_calls[0]["segment_contract"] is segment_contract
