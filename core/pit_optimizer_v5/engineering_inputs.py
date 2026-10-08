"""Import the accepted D1 fixture into a separate engineering V5 repository.

Only exact source bytes and discovery panels are imported. Mechanics and quick
panels are explicitly derived from the first two discovery date windows. No
evaluation or production held-out panel is opened by this preparation step.
"""

from __future__ import annotations

import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys

from core.pit_optimizer_evaluation import EvaluationPanelSpec
from core.pit_optimizer_v5.artifacts import ArtifactMissingV5, LocalArtifactRepositoryV5
from core.pit_optimizer_v5.contracts import (
    AnnualizedReturnTargetV5,
    ArtifactRefV5,
    CampaignPanelPlanV5,
    EpisodePlanV5,
    canonical_sha256_v5,
    validate_episode_plan_panel_v5,
)
from core.pit_optimizer_v5.production_fs import (
    acquire_absolute_directory_v5,
    acquire_directory_v5,
    write_new_regular_in_directory_v5,
)


_EXPECTED_MANIFEST_SHA256 = "a10d3098147e53cdbbe8882827f425be19357763589980ce5fd9079d4de16722"
_EXPECTED_BUNDLE_SHA256 = "6b2156d7260353ab3be8df6a91cbc7b234a5983d7acb704aab9d25102cf24332"
_EXPECTED_PROVENANCE_SHA256 = "d68da731115ca45c43849f01a1013a10d0ebb93e3b8543605ebf325a304511a2"
_EXPECTED_DISCOVERY_SHA256 = (
    "0697f03e823b6e41ec473b638455353f54246582cc466924b9478d38a71fa0f8",
    "4083ab14be19391b525e53a9d371eb3690393379f52071c2589bd495658952d2",
    "931de064cbd87653cb58baa3e29e9838217a19ca565cf54c494f935f1c5ab456",
    "019350e21ac1c5e505b94669ec9cb9bc0f2a2a415478a76366e3301e147c8499",
)


def _import_raw(
    *, source_root: Path, repository: LocalArtifactRepositoryV5, reference: ArtifactRefV5
) -> None:
    source_path = source_root.joinpath(*reference.relative_path.split("/"))
    if not source_path.is_file() or source_path.is_symlink():
        raise ValueError("D1 raw input is absent or linked")
    raw = source_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != reference.sha256:
        raise ValueError("D1 raw input differs from its accepted digest")
    try:
        repository.authenticate_raw_artifact(reference)
        return
    except ArtifactMissingV5:
        pass
    with acquire_absolute_directory_v5(repository.root) as root:
        with acquire_directory_v5(
            repository.root, ("data",), create=True,
            expected_root_identity=root.identity,
        ) as directory:
            write_new_regular_in_directory_v5(
                directory, reference.relative_path.rsplit("/", 1)[-1], raw
            )
    repository.authenticate_raw_artifact(reference)


def prepare_d1_engineering_inputs_v5(
    *, fixture_root: Path, artifact_root: Path, target_pct: Decimal
) -> dict[str, object]:
    source_root = Path(fixture_root)
    destination = Path(artifact_root)
    if (
        not source_root.is_absolute() or not source_root.is_dir() or source_root.is_symlink()
        or not destination.is_absolute() or not destination.is_dir() or destination.is_symlink()
    ):
        raise ValueError("engineering fixture and artifact roots must be distinct absolute directories")
    canonical_source = source_root.resolve(strict=True)
    canonical_destination = destination.resolve(strict=True)
    if canonical_source.is_relative_to(canonical_destination) or canonical_destination.is_relative_to(canonical_source):
        raise ValueError("engineering fixture and artifact roots must not overlap")
    manifest_path = source_root / "fixture_manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("accepted D1 fixture manifest is unavailable")
    manifest_raw = manifest_path.read_bytes()
    if hashlib.sha256(manifest_raw).hexdigest() != _EXPECTED_MANIFEST_SHA256:
        raise ValueError("D1 fixture manifest identity differs")
    fixture = json.loads(manifest_raw.decode("utf-8"))
    if (
        type(fixture) is not dict
        or fixture.get("schema") != "r1-d1-engineering-fixture/v1"
        or fixture.get("scope") != "synthetic_fixture_only"
        or fixture.get("production_source_coverage") is not False
        or fixture.get("source_commit") != "bce3ac825a3de91081d11db7ad811a827e9f24bc"
        or fixture.get("pit_bundle") != {
            "relative_path": "data/pit_bundle.sqlite3", "sha256": _EXPECTED_BUNDLE_SHA256
        }
        or fixture.get("prices_provenance") != {
            "relative_path": "data/prices_provenance.json", "sha256": _EXPECTED_PROVENANCE_SHA256
        }
    ):
        raise ValueError("D1 fixture scope or data identities differ")
    source = LocalArtifactRepositoryV5(source_root)
    repository = LocalArtifactRepositoryV5(destination)
    bundle_ref = ArtifactRefV5("data/pit_bundle.sqlite3", _EXPECTED_BUNDLE_SHA256)
    provenance_ref = ArtifactRefV5("data/prices_provenance.json", _EXPECTED_PROVENANCE_SHA256)
    _import_raw(source_root=source_root, repository=repository, reference=bundle_ref)
    _import_raw(source_root=source_root, repository=repository, reference=provenance_ref)

    discovery: list[EpisodePlanV5] = []
    panels: list[EvaluationPanelSpec] = []
    rows = fixture.get("discovery")
    if type(rows) is not list or len(rows) != 4:
        raise ValueError("D1 fixture does not have four discovery episodes")
    for index, row in enumerate(rows, start=1):
        if type(row) is not dict or row.get("panel_sha256") != _EXPECTED_DISCOVERY_SHA256[index - 1]:
            raise ValueError("D1 discovery panel identity differs")
        source_ref = ArtifactRefV5(
            f"panels/discovery-{index}.json", _EXPECTED_DISCOVERY_SHA256[index - 1]
        )
        panel = source.load_evaluation_panel_spec_exact(source_ref)
        imported_ref = repository.create_evaluation_panel_spec(source_ref.relative_path, panel)
        if imported_ref != source_ref:
            raise ValueError("imported D1 discovery panel identity differs")
        original = row.get("episode")
        if type(original) is not dict:
            raise ValueError("D1 discovery episode is invalid")
        episode = EpisodePlanV5(
            episode_id=f"engineering-discovery-{index}",
            episode_ordinal=index,
            purpose="discovery",
            start_date=panel.start_date,
            end_date=panel.end_date,
            lineage_ids=tuple(item.security_lineage_id for item in panel.lineages),
            panel_ref=source_ref,
        )
        if (
            original.get("episode_ordinal") != index
            or original.get("start_date") != episode.start_date
            or original.get("end_date") != episode.end_date
            or original.get("lineage_ids") != list(episode.lineage_ids)
        ):
            raise ValueError("D1 discovery episode differs from its authenticated panel")
        validate_episode_plan_panel_v5(episode, panel)
        discovery.append(episode)
        panels.append(panel)

    def quick_episode(name: str, source_panel: EvaluationPanelSpec) -> EpisodePlanV5:
        quick_panel = EvaluationPanelSpec.from_lineages(
            purpose="quick", sessions=source_panel.sessions, lineages=source_panel.lineages
        )
        reference = repository.create_evaluation_panel_spec(f"panels/{name}.json", quick_panel)
        episode = EpisodePlanV5(
            episode_id=f"engineering-{name}", episode_ordinal=None,
            purpose="quick", start_date=quick_panel.start_date,
            end_date=quick_panel.end_date,
            lineage_ids=tuple(item.security_lineage_id for item in quick_panel.lineages),
            panel_ref=reference,
        )
        validate_episode_plan_panel_v5(episode, quick_panel)
        return episode

    target = AnnualizedReturnTargetV5(target_pct)
    plan = CampaignPanelPlanV5(
        schema_version=5,
        pit_bundle_ref=bundle_ref,
        prices_provenance_ref=provenance_ref,
        partition_seed_sha256=canonical_sha256_v5({
            "scope": "engineering_v3", "fixture_manifest_sha256": _EXPECTED_MANIFEST_SHA256,
        }),
        target_sha256=target.sha256,
        mechanics=quick_episode("mechanics", panels[0]),
        quick=quick_episode("quick", panels[1]),
        discovery=tuple(discovery),
        confirmation_plan_sha256=canonical_sha256_v5({
            "scope": "engineering_v3", "held_out": "confirmation_unavailable",
        }),
        qualification_plan_sha256=canonical_sha256_v5({
            "scope": "engineering_v3", "held_out": "qualification_unavailable",
        }),
    )
    reference = repository.create_typed_artifact("panels/engineering-discovery-plan.json", plan)
    return {
        "schema_version": 5,
        "scope": "engineering_v3",
        "source_fixture_manifest_sha256": _EXPECTED_MANIFEST_SHA256,
        "pit_bundle_ref": bundle_ref.to_primitive(),
        "prices_provenance_ref": provenance_ref.to_primitive(),
        "target_pct": str(target_pct),
        "target_sha256": target.sha256,
        "panel_plan_ref": reference.to_primitive(),
        "mechanics_panel_ref": plan.mechanics.panel_ref.to_primitive(),
        "quick_panel_ref": plan.quick.panel_ref.to_primitive(),
        "discovery_panel_refs": [item.panel_ref.to_primitive() for item in plan.discovery],
        "evaluation_executed": False,
    }


def main(argv: tuple[str, ...] | None = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--fixture-root", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--target-pct", type=Decimal, required=True)
    args = parser.parse_args(argv)
    try:
        result = prepare_d1_engineering_inputs_v5(
            fixture_root=args.fixture_root,
            artifact_root=args.artifact_root,
            target_pct=args.target_pct,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"engineering input preparation rejected: {type(exc).__name__}: {str(exc)[:512]}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys.argv[1:])))


__all__ = ["prepare_d1_engineering_inputs_v5", "main"]
