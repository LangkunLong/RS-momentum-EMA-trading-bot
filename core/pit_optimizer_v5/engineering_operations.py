"""Finite, resumable operator entry for isolated V5 engineering rounds.

The inspect mode composes authenticated authorities without evaluation. The
run mode uses the ordinary feedback runtime and its existing Docker worker;
it never imports or executes candidate policy source in the host process.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
import time
from typing import Literal

from core.pit_optimizer_v5.artifacts import LocalArtifactRepositoryV5
from core.pit_optimizer_v5.cli import CampaignAuthoritiesV5
from core.pit_optimizer_v5.contracts import ArtifactRefV5, canonical_sha256_v5
from core.pit_optimizer_v5.engineering_composition import compose_engineering_round_v5
from core.pit_optimizer_v5.manifest import authenticate_campaign_manifest_v5
from core.pit_optimizer_v5.memory import CleanupResultPayloadV5
from core.pit_optimizer_v5.runtime import run_feedback_round_v5


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class EngineeringCampaignWindowV5:
    manifest_ref: ArtifactRefV5
    baseline_capture_authority_ref: ArtifactRefV5
    owner_token_sha256: str
    host_config_sha256: str
    started_epoch_ms: int

    def __post_init__(self) -> None:
        if (
            type(self.manifest_ref) is not ArtifactRefV5
            or type(self.baseline_capture_authority_ref) is not ArtifactRefV5
            or type(self.owner_token_sha256) is not str
            or _DIGEST.fullmatch(self.owner_token_sha256) is None
            or type(self.host_config_sha256) is not str
            or _DIGEST.fullmatch(self.host_config_sha256) is None
            or type(self.started_epoch_ms) is not int
            or self.started_epoch_ms < 1
        ):
            raise ValueError("engineering campaign window is invalid")


@dataclass(frozen=True, slots=True)
class EngineeringRoundWindowV5:
    manifest_sha256: str
    round_index: Literal[1, 2]
    owner_token_sha256: str
    started_epoch_ms: int

    def __post_init__(self) -> None:
        if (
            type(self.manifest_sha256) is not str
            or _DIGEST.fullmatch(self.manifest_sha256) is None
            or self.round_index not in (1, 2)
            or type(self.owner_token_sha256) is not str
            or _DIGEST.fullmatch(self.owner_token_sha256) is None
            or type(self.started_epoch_ms) is not int
            or self.started_epoch_ms < 1
        ):
            raise ValueError("engineering round window is invalid")


def _round_one_measurement(
    repository: LocalArtifactRepositoryV5, *, campaign_id: str, experiment_id: str
) -> None:
    events = repository.load_round_events(campaign_id=campaign_id, round_index=1)
    cleanup = tuple(
        payload
        for event in events
        for payload in (repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind),)
        if type(payload) is CleanupResultPayloadV5
    )
    if len(cleanup) != 1 or not cleanup[0].cleanup_complete:
        raise ValueError("round two requires completed round-one cleanup")
    checkpoint = repository.load_checkpoint()
    if checkpoint is None:
        raise ValueError("round two requires a persisted round-one checkpoint")
    records = tuple(repository.load_experiment(ref) for ref in checkpoint.record_refs)
    matches = tuple(record for record in records if record.experiment_id == experiment_id)
    if len(matches) != 1 or matches[0].round_index != 1 or matches[0].status not in {"evaluated", "zero_trade"}:
        raise ValueError("round two requires one persisted measured round-one experiment")


def _host_config_sha256(paths: dict[str, str]) -> str:
    if any(type(value) is not str or not Path(value).is_absolute() for value in paths.values()):
        raise ValueError("engineering host paths must be absolute")
    return canonical_sha256_v5(paths)


def operate_engineering_round_v5(
    *,
    mode: Literal["inspect", "run"],
    artifact_root: Path,
    manifest_ref: ArtifactRefV5,
    baseline_capture_authority_ref: ArtifactRefV5,
    round_index: int,
    owner_token_sha256: str,
    feedback_experiment_id: str | None,
    response_directory: str,
    source_root: str,
    workspace_root: str,
    data_root: str,
    output_root: str,
    control_root: str,
    git_executable: str,
    docker_executable: str,
) -> dict[str, object]:
    """Inspect or run one admitted engineering round with persisted wall limits."""

    if mode not in {"inspect", "run"}:
        raise ValueError("engineering operation mode is invalid")
    repository = LocalArtifactRepositoryV5(artifact_root)
    authenticated = authenticate_campaign_manifest_v5(repository=repository, manifest_ref=manifest_ref)
    authorities = CampaignAuthoritiesV5(
        authenticated.manifest,
        authenticated.panel_plan,
        authenticated.evaluator_contract,
        authenticated.baseline_authority,
        authenticated.sandbox_profile,
    )
    paths = {
        "response_directory": response_directory,
        "source_root": source_root,
        "workspace_root": workspace_root,
        "data_root": data_root,
        "output_root": output_root,
        "control_root": control_root,
        "git_executable": git_executable,
        "docker_executable": docker_executable,
    }
    host_config_sha256 = _host_config_sha256(paths)
    composition = compose_engineering_round_v5(
        repository=repository,
        manifest_ref=manifest_ref,
        baseline_capture_authority_ref=baseline_capture_authority_ref,
        authorities=authorities,
        round_index=round_index,
        owner_token_sha256=owner_token_sha256,
        feedback_experiment_id=feedback_experiment_id,
        **paths,
    )
    manifest = authenticated.manifest
    if round_index == 2:
        assert feedback_experiment_id is not None
        _round_one_measurement(
            repository, campaign_id=manifest.campaign_id, experiment_id=feedback_experiment_id
        )
    common = {
        "schema_version": 5,
        "mode": mode,
        "manifest_ref": manifest_ref.to_primitive(),
        "baseline_capture_authority_ref": baseline_capture_authority_ref.to_primitive(),
        "round_index": round_index,
        "host_config_sha256": host_config_sha256,
        "pit_data_scope": manifest.pit_data_scope,
        "provider_calls": 0,
        "image_reference": authenticated.sandbox_profile.image_reference,
        "evaluator_source_sha256": authenticated.evaluator_contract.evaluator_source_sha256,
        "pit_bundle_sha256": authenticated.evaluator_contract.pit_bundle_sha256,
        "prices_provenance_sha256": authenticated.evaluator_contract.prices_provenance_sha256,
        "maximum_feedback_rounds": manifest.search.max_feedback_rounds,
        "round_wall_timeout_seconds": manifest.resources.round_wall_timeout_seconds,
        "campaign_wall_timeout_seconds": manifest.resources.campaign_wall_timeout_seconds,
    }
    if mode == "inspect":
        return {**common, "status": "composition_ready", "evaluation_executed": False}

    with repository.adapter_state_transition(namespace="engineering-launch", key=manifest.sha256):
        now_ms = time.time_ns() // 1_000_000
        launch = repository.load_typed_state(
            namespace="engineering-launch", key=manifest.sha256,
            value_type=EngineeringCampaignWindowV5, repair=False,
        )
        if launch is None:
            if (
                round_index != 1
                or repository.load_checkpoint() is not None
                or repository.load_round_events(campaign_id=manifest.campaign_id, round_index=1)
            ):
                raise ValueError("engineering campaign must begin with a fresh round one")
            launch = EngineeringCampaignWindowV5(
                manifest_ref, baseline_capture_authority_ref,
                owner_token_sha256, host_config_sha256, now_ms,
            )
            repository.append_typed_state(
                namespace="engineering-launch", key=manifest.sha256, value=launch,
            )
        elif (
            launch.manifest_ref != manifest_ref
            or launch.baseline_capture_authority_ref != baseline_capture_authority_ref
            or launch.owner_token_sha256 != owner_token_sha256
            or launch.host_config_sha256 != host_config_sha256
        ):
            raise ValueError("engineering campaign resume differs from original authority")
        round_key = canonical_sha256_v5({
            "manifest_sha256": manifest.sha256,
            "round_index": round_index,
        })
        started = repository.load_typed_state(
            namespace="engineering-round-window", key=round_key,
            value_type=EngineeringRoundWindowV5, repair=False,
        )
        if started is None:
            started = EngineeringRoundWindowV5(
                manifest.sha256, round_index, owner_token_sha256, now_ms,
            )
            repository.append_typed_state(
                namespace="engineering-round-window", key=round_key, value=started,
            )
        elif (
            started.manifest_sha256 != manifest.sha256
            or started.round_index != round_index
            or started.owner_token_sha256 != owner_token_sha256
        ):
            raise ValueError("engineering round resume differs from original authority")
        if now_ms < launch.started_epoch_ms or now_ms < started.started_epoch_ms:
            raise ValueError("host clock precedes persisted engineering start")
        campaign_remaining = (
            manifest.resources.campaign_wall_timeout_seconds
            - (now_ms - launch.started_epoch_ms) / 1000
        )
        round_remaining = (
            manifest.resources.round_wall_timeout_seconds
            - (now_ms - started.started_epoch_ms) / 1000
        )
        remaining = min(campaign_remaining, round_remaining)
        if remaining <= 0:
            raise ValueError("engineering campaign or round wall limit expired")
        result = run_feedback_round_v5(
            composition.inputs,
            composition.dependencies,
            campaign_deadline_monotonic=time.monotonic() + remaining,
        )
        pending = result.pending_controller_role
        measured_records = tuple(repository.load_experiment(ref) for ref in result.record_refs)
        return {
            **common,
            "status": result.status,
            "evaluation_executed": any(
                record.round_index == round_index
                and record.status in {"evaluated", "zero_trade"}
                and record.campaign_evidence is not None
                for record in measured_records
            ),
            "campaign_started_epoch_ms": launch.started_epoch_ms,
            "round_started_epoch_ms": started.started_epoch_ms,
            "campaign_deadline_epoch_ms": launch.started_epoch_ms + manifest.resources.campaign_wall_timeout_seconds * 1000,
            "round_deadline_epoch_ms": started.started_epoch_ms + manifest.resources.round_wall_timeout_seconds * 1000,
            "pending_controller_role": None if pending is None else {
                "role": pending.role,
                "call_key_sha256": pending.call_key_sha256,
                "request_sha256": pending.request_sha256,
                "request_ref": pending.request_ref.to_primitive(),
                "response_filename": pending.call_key_sha256 + ".json",
            },
            "record_refs": [ref.to_primitive() for ref in result.record_refs],
            "checkpoint_generation": None if result.checkpoint is None else result.checkpoint.generation,
            "failure": None if result.failure is None else {
                "stage": result.failure.stage, "code": result.failure.code,
            },
            "cleanup_complete": None if result.cleanup is None else result.cleanup.cleanup_complete,
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("mode", choices=("inspect", "run"))
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--manifest-path", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--baseline-capture-path", required=True)
    parser.add_argument("--baseline-capture-sha256", required=True)
    parser.add_argument("--round-index", type=int, choices=(1, 2), required=True)
    parser.add_argument("--owner-token-sha256", required=True)
    parser.add_argument("--feedback-experiment-id")
    for name in (
        "response-directory", "source-root", "workspace-root", "data-root",
        "output-root", "control-root", "git-executable", "docker-executable",
    ):
        parser.add_argument("--" + name, required=True)
    return parser


def main(argv: tuple[str, ...] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = operate_engineering_round_v5(
            mode=args.mode,
            artifact_root=args.artifact_root,
            manifest_ref=ArtifactRefV5(args.manifest_path, args.manifest_sha256),
            baseline_capture_authority_ref=ArtifactRefV5(
                args.baseline_capture_path, args.baseline_capture_sha256,
            ),
            round_index=args.round_index,
            owner_token_sha256=args.owner_token_sha256,
            feedback_experiment_id=args.feedback_experiment_id,
            **{
                name.replace("-", "_"): getattr(args, name.replace("-", "_"))
                for name in (
                    "response-directory", "source-root", "workspace-root", "data-root",
                    "output-root", "control-root", "git-executable", "docker-executable",
                )
            },
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"engineering operation rejected: {type(exc).__name__}: {str(exc)[:512]}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] in {"composition_ready", "completed", "awaiting_controller_response"} else 3


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys.argv[1:])))


__all__ = ["EngineeringCampaignWindowV5", "EngineeringRoundWindowV5", "operate_engineering_round_v5", "main"]
