"""Controller for the credential-free, two-arm V5 evidence study.

The controller is intentionally a boundary around the already reviewed V5
ports.  It creates the synthetic campaign with the ordinary runtime, records
the exact F and L request authorities, and leaves the one external attempt per
arm to :mod:`ledger`.  No response or terminal is manufactured by this module.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from decimal import Decimal
import gc
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import time
from typing import Mapping, Literal

from core.pit_optimizer_v5.artifacts import (
    ArtifactRepositoryFailureV5,
    ArtifactRefV5,
    LocalArtifactRepositoryV5,
    RepositoryCheckpointV5,
)
from core.pit_optimizer_v5.candidate_ir import SourceBundleV5
from core.pit_optimizer_v5.contracts import canonical_json_bytes_v5, canonical_primitive_v5
from core.pit_optimizer_v5.mechanism_artifacts import (
    MechanismExperimentIndexV1,
    MechanismArtifactRepositoryV5,
    MechanismPrecommitmentIndexV1,
    manifest_source_identity_sha256_v1,
)
from core.pit_optimizer_v5.memory import (
    RoleCompletionPayloadV5,
    StoredExperimentRecordV5,
    project_investigator_memory_v5,
)
from core.pit_optimizer_v5.production_runtime import (
    LocalArchiveReducerFactoryV5,
    LocalRoleRequestFactoryV5,
    MechanismRoleRequestAdapterV1,
)
from core.pit_optimizer_v5.provider import (
    MechanismRoleInputV1,
    RoleCallKeyV5,
    RoleInvocationPackageV5,
    RoleRequestV5,
)
from core.pit_optimizer_v5.runtime import (
    FeedbackRoundInputV5,
    FeedbackRoundResultV5,
    SearchProjectionV5,
    run_feedback_round_v5,
)
from core.pit_optimizer_v5.selection import select_parent_v5
from core.pit_optimizer_v5.probes import classify_semantic_fingerprints_v5

from .comparison import RequestComparisonV1, compare_study_requests_v1
from .contracts import (
    StudyAdmissionError,
    StudyArmV1,
    StudyAuthorityError,
    StudyContractError,
    StudyManifestV1,
    StudyModeV1,
    StudyOfflineSettingsV1,
    StudyPendingAccounting,
    StudyProviderSettingsV1,
)
from .compiler import StudyCommitmentIndexV1
from .fixtures import (
    StudyFixtureV1,
    create_study_fixture_v1,
    reopen_study_fixture_v1,
    study_resource_budget_v1,
)
from .imports import (
    StudyImportV1,
    authenticate_study_import_v1,
    create_study_import_v1,
    verify_imported_package_v1,
)
from .ledger import (
    AuthenticatedStudyTerminalV1,
    StudyAdmissionRejectionV1,
    StudyLedgerV1,
    recover_study_call_v1,
    run_study_call_v1,
)
from .live_calls import (
    FixturePreflightV1,
    StudyCallRequestV1,
    authenticate_fixture_preflight_v1,
    build_study_call_v1,
    study_parser_authority_bytes_v1,
    study_prompt_bytes_v1,
)
from .registry import FrozenBehaviorRegistryV1, build_study_registry_v1, verify_registry_v1
from .runtime_ports import (
    StudyRequestRouterV1,
    compose_study_round_v1,
    prepare_scripted_role_authority_v1,
    verify_scripted_role_authority_v1,
)
from .schema import study_response_schema_v1
from .store import StudyStoreV1


_SHA256_HEX = frozenset("0123456789abcdef")
_ROUND_TWO = 2
_STUDY_ID = "study-v5-two-round-example"
_OFFLINE_FIXTURE_ID = "study-fixture-v1"


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _digest(value: str, label: str) -> str:
    if type(value) is not str or len(value) != 64 or any(item not in _SHA256_HEX for item in value):
        raise StudyAuthorityError(f"{label} is not a SHA-256 digest")
    return value


def _long_path(path: Path | str) -> str:
    """Return a Windows extended path for repository-owned I/O.

    The fixture roots intentionally sit below a long plan workspace path.
    ``pathlib.Path.rglob`` and ordinary ``Path.read_bytes`` can silently omit
    entries once the full path crosses the legacy Windows limit, so the closed
    graph code uses the extended path spelling for traversal and copying.
    """

    value = os.path.abspath(os.fspath(path))
    if os.name != "nt" or value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def _long_exists(path: Path | str) -> bool:
    try:
        os.lstat(_long_path(path))
    except FileNotFoundError:
        return False
    return True


def _long_read_bytes(path: Path | str) -> bytes:
    with open(_long_path(path), "rb") as handle:
        return handle.read()


def _long_write_bytes(path: Path | str, raw: bytes) -> None:
    target = Path(path)
    os.makedirs(_long_path(target.parent), exist_ok=True)
    with open(_long_path(target), "wb") as handle:
        handle.write(raw)


def _long_files(root: Path) -> tuple[tuple[str, str], ...]:
    """Enumerate every regular file below ``root`` with long-path I/O."""

    result: list[tuple[str, str]] = []

    def walk(directory: str, prefix: tuple[str, ...]) -> None:
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            raise StudyAuthorityError("closed graph directory could not be enumerated") from exc
        for entry in entries:
            name = entry.name
            if not name or name in {".", ".."} or "/" in name or "\\" in name:
                raise StudyAuthorityError("closed graph contains an invalid path component")
            try:
                metadata = entry.stat(follow_symlinks=False)
                attributes = getattr(metadata, "st_file_attributes", 0)
                reparse = bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
                if entry.is_symlink() or reparse:
                    raise StudyAuthorityError("closed graph contains a symlink or reparse point")
                child_prefix = (*prefix, name)
                if entry.is_dir(follow_symlinks=False):
                    walk(entry.path, child_prefix)
                elif entry.is_file(follow_symlinks=False):
                    result.append((PurePosixPath(*child_prefix).as_posix(), entry.path))
                else:
                    raise StudyAuthorityError("closed graph contains a non-regular file")
            except StudyAuthorityError:
                raise
            except OSError as exc:
                raise StudyAuthorityError("closed graph entry could not be inspected") from exc

    walk(_long_path(root), ())
    return tuple(result)


def _long_has_entries(root: Path) -> bool:
    """Inspect direct children without following links, including empty dirs."""

    try:
        with os.scandir(_long_path(root)) as iterator:
            entries = sorted(iterator, key=lambda item: item.name)
            found = False
            for entry in entries:
                name = entry.name
                if not name or name in {".", ".."} or "/" in name or "\\" in name:
                    raise StudyAuthorityError("fresh root contains an invalid path component")
                metadata = entry.stat(follow_symlinks=False)
                attributes = getattr(metadata, "st_file_attributes", 0)
                reparse = bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
                if entry.is_symlink() or reparse:
                    raise StudyAuthorityError("fresh root contains a symlink or reparse point")
                if not entry.is_dir(follow_symlinks=False) and not entry.is_file(follow_symlinks=False):
                    raise StudyAuthorityError("fresh root contains a non-regular child")
                found = True
            return found
    except StudyAuthorityError:
        raise
    except OSError as exc:
        raise StudyAuthorityError("fresh root entries could not be inspected") from exc


def _absolute_fresh_container(root: Path) -> Path:
    candidate = Path(os.path.abspath(os.fspath(root)))
    if not candidate.is_absolute():
        raise StudyAuthorityError("study root must be absolute")
    if _long_exists(candidate):
        try:
            metadata = os.lstat(_long_path(candidate))
            attributes = getattr(metadata, "st_file_attributes", 0)
            reparse = bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        except OSError as exc:
            raise StudyAuthorityError("study root could not be inspected") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or reparse
            or not stat.S_ISDIR(metadata.st_mode)
            or _long_has_entries(candidate)
        ):
            raise StudyAuthorityError("study root must be a fresh empty directory")
    else:
        os.makedirs(_long_path(candidate), exist_ok=True)
    # ``Path.resolve(strict=True)`` still uses legacy Win32 path handling and
    # fails for the deliberately long roots used by the closed-graph test.
    # The absolute spelling above is sufficient after the explicit fresh-root
    # checks and keeps subsequent repository I/O on the long-path helpers.
    return candidate


def _fresh_child(parent: Path, name: str) -> Path:
    child = parent / name
    if _long_exists(child):
        raise StudyAuthorityError("study preparation child root already exists")
    os.mkdir(_long_path(child))
    return child


def _provider_settings(
    mode: StudyModeV1,
    supplied: Mapping[str, object] | None,
) -> StudyProviderSettingsV1 | None:
    if mode == "offline_fixture":
        if supplied is not None:
            raise StudyAuthorityError("offline fixture preparation does not accept provider settings")
        return None
    if type(supplied) is not dict and not isinstance(supplied, Mapping):
        raise StudyAuthorityError("live study preparation requires provider settings")
    assert supplied is not None
    expected = {"provider", "model", "max_output_tokens", "temperature", "seed"}
    if set(supplied) != expected:
        raise StudyAuthorityError("live provider settings are incomplete or contain an extra field")
    temperature = supplied["temperature"]
    if temperature is not None and type(temperature) is not Decimal:
        try:
            temperature = Decimal(str(temperature))
        except Exception as exc:  # pragma: no cover - contract translates it
            raise StudyAuthorityError("live provider temperature is invalid") from exc
    return StudyProviderSettingsV1(
        provider=supplied["provider"],  # type: ignore[arg-type]
        model=supplied["model"],  # type: ignore[arg-type]
        max_output_tokens=supplied["max_output_tokens"],  # type: ignore[arg-type]
        temperature=temperature,  # type: ignore[arg-type]
        seed=supplied["seed"],  # type: ignore[arg-type]
    )


def _round_two_input(fixture: StudyFixtureV1) -> FeedbackRoundInputV5:
    manifest = fixture.manifest
    return FeedbackRoundInputV5(
        manifest.manifest,
        manifest.panel_plan,
        manifest.evaluator_contract,
        manifest.baseline_authority,
        _ROUND_TWO,
        hashlib.sha256(canonical_json_bytes_v5({"fixture": manifest.manifest.sha256, "round": _ROUND_TWO})).hexdigest(),
    )


def _checkpoint_bytes(repository: LocalArtifactRepositoryV5, checkpoint: RepositoryCheckpointV5) -> bytes:
    reference = ArtifactRefV5("checkpoint.json", _sha256(canonical_json_bytes_v5(checkpoint.to_primitive())))
    return repository.authenticate_exact(reference).content


def _snapshot_bytes(repository: LocalArtifactRepositoryV5, checkpoint: RepositoryCheckpointV5) -> bytes:
    return repository.authenticate_exact(ArtifactRefV5("archive.json", checkpoint.archive_sha256)).content


def _projection(repository: LocalArtifactRepositoryV5, fixture: StudyFixtureV1) -> SearchProjectionV5:
    factory = LocalArchiveReducerFactoryV5(repository)
    state = factory.verify_projection(
        manifest=fixture.manifest.manifest,
        panel_plan=fixture.manifest.panel_plan,
        evaluator_contract=fixture.manifest.evaluator_contract,
        repair=False,
    )
    checkpoint = repository.load_checkpoint()
    if checkpoint is None:
        raise StudyAuthorityError("round-one runtime did not publish a checkpoint")
    stored = tuple(
        StoredExperimentRecordV5(reference, repository.load_experiment(reference))
        for reference in checkpoint.record_refs
    )
    return SearchProjectionV5(checkpoint, state, stored)


def _configuration_for_record(registry: FrozenBehaviorRegistryV1, record) -> str | None:
    revision = record.policy_revision
    if revision is None:
        return None
    for item in registry.configurations:
        if item.policy_revision == revision:
            return item.configuration_id
    return None


def _round_one_critic_packages(fixture: StudyFixtureV1) -> tuple[RoleInvocationPackageV5, ...]:
    events = fixture.repository.load_round_events(
        campaign_id=fixture.manifest.manifest.campaign_id,
        round_index=1,
    )
    packages: list[RoleInvocationPackageV5] = []
    for event in events:
        payload = fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
        if type(payload) is RoleCompletionPayloadV5 and payload.role == "critic":
            packages.append(fixture.repository.load_role_invocation(payload))
    return tuple(packages)


def _require_round_one_cleanup(result: FeedbackRoundResultV5) -> None:
    """Admit an ancestor only after every runtime-owned resource is closed."""

    if type(result) is not FeedbackRoundResultV5 or result.status != "completed":
        raise StudyAuthorityError("seeded round one did not produce a completed result")
    cleanup = result.cleanup
    if cleanup is None or not cleanup.cleanup_complete:
        raise StudyAuthorityError("seeded round one cleanup is absent or incomplete")
    if result.cleanup_failure is not None:
        raise StudyAuthorityError("seeded round one cleanup failed")
    if result.pending_controller_role is not None:
        raise StudyAuthorityError("seeded round one retains a pending controller role")


def _typed_state_references(
    repository: LocalArtifactRepositoryV5,
    *,
    namespace: str,
    key: str,
    value_type: type[object],
) -> tuple[ArtifactRefV5, ArtifactRefV5]:
    """Load a typed value and its create-only index without repairing either."""

    try:
        value = repository.load_typed_state(
            namespace=namespace,
            key=key,
            value_type=value_type,
            repair=False,
        )
        authority_item = repository._load_adapter_state_authority(namespace=namespace, key=key)
    except (OSError, TypeError, ValueError) as exc:
        raise StudyAuthorityError(f"typed control authority is unreadable: {namespace}/{key}") from exc
    if value is None or authority_item is None:
        raise StudyAuthorityError(f"typed control authority is incomplete: {namespace}/{key}")
    authority, authority_ref = authority_item
    value_ref = ArtifactRefV5(
        f"adapter-state/{namespace}/{key}.json",
        _sha256(canonical_json_bytes_v5(value)),
    )
    if authority.value_ref != value_ref:
        raise StudyAuthorityError(f"typed control authority differs: {namespace}/{key}")
    return value_ref, authority_ref


def _require_actual_p1_distinct_child(
    registry: FrozenBehaviorRegistryV1,
    selected: object,
) -> str:
    """Prove a registry child differs from the checkpoint-selected P1 on probes."""

    selected_revision = getattr(selected, "policy_identity_sha256", None)
    selected_config = tuple(
        item for item in registry.configurations if item.policy_revision.sha256 == selected_revision
    )
    if len(selected_config) != 1:
        raise StudyAuthorityError("checkpoint-selected P1 is not uniquely registered")
    parent = selected_config[0]
    children = tuple(
        item for item in registry.configurations if item.parent_configuration_id == parent.configuration_id
    )
    for child in children:
        comparison = classify_semantic_fingerprints_v5(parent.fixed_suite, child.fixed_suite)
        if (
            comparison.classification == "behaviorally_distinct_on_suite_v1"
            and comparison.differing_probe_ids
            and child.policy_revision.sha256 != parent.policy_revision.sha256
        ):
            return child.configuration_id
    raise StudyAuthorityError(
        f"checkpoint-selected P1 {parent.configuration_id} has no registered distinct discriminating child"
    )


def _round_one_gate(
    fixture: StudyFixtureV1,
    result: FeedbackRoundResultV5,
) -> tuple[SearchProjectionV5, object, tuple[str, ...]]:
    _require_round_one_cleanup(result)
    if result.round_index != 1 or not result.record_refs:
        raise StudyAuthorityError("seeded round one did not complete and publish records")
    projection = _projection(fixture.repository, fixture)
    by_id = {
        _configuration_for_record(fixture.registry, stored.record): stored
        for stored in projection.stored_records
    }
    if by_id.get("A") is None or by_id.get("S") is None:
        raise StudyAuthorityError("seeded round one did not publish the registered A and S records")
    baseline = fixture.registry.configuration("P0").portfolio_evaluator_inputs[0].base_annualized_return_pct
    a_record = by_id["A"].record
    s_record = by_id["S"].record
    if a_record.campaign_evidence is None or s_record.campaign_evidence is None:
        raise StudyAuthorityError("seeded round one records lack campaign evidence")
    a_cagr = a_record.campaign_evidence.campaign_cagr_pct
    s_cagr = s_record.campaign_evidence.campaign_cagr_pct
    if not baseline < a_cagr < s_cagr:
        raise StudyAuthorityError("seeded round one does not satisfy P0 < A < S")

    mechanism = MechanismArtifactRepositoryV5(fixture.repository)
    source_identity = manifest_source_identity_sha256_v1(fixture.manifest)
    persisted = mechanism.load_existing_evidence_for_record(
        campaign_id=fixture.manifest.manifest.campaign_id,
        round_index=1,
        manifest_ref=fixture.manifest.manifest_ref,
        manifest_source_identity_sha256=source_identity,
        stored_record=by_id["A"],
    )
    if persisted is None:
        raise StudyAuthorityError("A has no persisted mechanism evidence")
    evidence, _intent = persisted
    predictions = {item.metric_id: item for item in evidence.report.predictions}
    decision = predictions.get("exit.decision_changed_count")
    controls = predictions.get("exit.protected_control_unchanged_count")
    if (
        decision is None
        or decision.denominator != 3
        or decision.numerator != Decimal("0")
        or decision.assessment != "contradicted_on_cases"
        or controls is None
        or controls.denominator != 4
        or controls.numerator != Decimal("4")
        or controls.assessment != "supported_on_cases"
    ):
        raise StudyAuthorityError("A mechanism evidence is not the required 0/3 and 4/4 result")

    mechanism_ids = set(evidence.report.evidence_ids) if hasattr(evidence.report, "evidence_ids") else set()
    for package in _round_one_critic_packages(fixture):
        request_ids = {item.evidence_id for item in package.request.role_evidence.items}
        artifact_ids = set(getattr(package.artifact, "evidence_ids", ()))
        if mechanism_ids.intersection(request_ids | artifact_ids):
            raise StudyAuthorityError("round-one critic cited mechanism evidence")
        encoded = canonical_json_bytes_v5(canonical_primitive_v5(package.artifact)).decode("utf-8").lower()
        if "paired local observations" in encoded or "mechanism contradiction" in encoded:
            raise StudyAuthorityError("round-one critic contains mechanism prose")

    inputs2 = _round_two_input(fixture)
    selected = select_parent_v5(
        state=projection.state,
        baseline=inputs2.baseline,
        discovery_plan=inputs2.panel_plan,
        evaluator_contract=inputs2.evaluator_contract,
        stored_records=projection.stored_records,
    )
    if selected.policy_identity_sha256 != s_record.policy_revision.sha256:
        raise StudyAuthorityError("round-two parent was not selected from the recovered archive")
    _require_actual_p1_distinct_child(fixture.registry, selected)
    base_factory = LocalRoleRequestFactoryV5(repository=fixture.repository, manifest=inputs2.manifest)
    memory_request = base_factory.investigator_request(inputs2, projection, selected)
    base_input = memory_request.role_input.base_input if isinstance(memory_request.role_input, MechanismRoleInputV1) else memory_request.role_input
    memory = project_investigator_memory_v5(
        stored_records=projection.stored_records,
        selected_parent_revision_sha256=selected.policy_identity_sha256,
        selected_parent_record_ref=selected.selected_parent_record_ref,
        relevant_mechanism=selected.primary_mechanism or "cross_policy",
        maximum_bytes=inputs2.manifest.search.investigator_memory_max_bytes,
    )
    memory_ids = tuple(item.experiment_id for item in memory.summaries)
    if by_id["A"].record.experiment_id not in memory_ids:
        raise StudyAuthorityError(
            "unchanged round-two memory projection omitted A "
            f"(A={by_id['A'].record.experiment_id}, ids={memory_ids}, "
            f"selected_mechanism={selected.primary_mechanism}, "
            f"records={[(k, v.record.hypothesis.primary_mechanism, v.record.status) for k, v in by_id.items()]})"
        )
    if by_id["A"].record.experiment_id in tuple(item.experiment_id for item in memory.complete_feedback):
        raise StudyAuthorityError("A unexpectedly retained full feedback instead of a bounded summary")
    role_ids = tuple(item.experiment_id for item in base_input.experiment_summaries)
    if by_id["A"].record.experiment_id not in role_ids:
        raise StudyAuthorityError("production investigator input omitted the authenticated A summary")
    return projection, selected, memory_ids


def _authenticated_closed_snapshot_unchecked(
    fixture: StudyFixtureV1,
) -> tuple[tuple[str, bytes], ...]:
    """Return the closed ancestor graph after authenticating every edge.

    The fixture directory is deliberately *not* treated as a manifest of its
    own contents.  Runtime journals and adapter state are partly discovered
    through their authenticated control records, so a closed snapshot first
    builds that graph and then rejects every present path outside it.  This is
    what makes a sentinel file (including a canonical-looking JSON file) an
    admission failure instead of an implicitly trusted new input.
    """

    checkpoint = fixture.repository.load_checkpoint()
    if checkpoint is None:
        raise StudyAuthorityError("cannot snapshot an uncheckpointed ancestor")
    # A final read-only projection verification proves the archive and all
    # checkpoint records before any copy is made.
    projection = _projection(fixture.repository, fixture)

    def collect(value: object, references: dict[str, ArtifactRefV5]) -> None:
        if isinstance(value, ArtifactRefV5):
            references.setdefault(value.relative_path, value)
            return
        if dataclasses.is_dataclass(value):
            for item in dataclasses.fields(value):
                collect(getattr(value, item.name), references)
            return
        if isinstance(value, (tuple, list)):
            for item in value:
                collect(item, references)
            return
        if isinstance(value, dict):
            for item in value.values():
                collect(item, references)

    # The manifest graph, checkpoint, records, round journal payloads, role
    # packages, and persisted mechanism indices are the authenticated roots of
    # the ancestor.  These are intentionally explicit rather than a directory
    # glob so that a new file cannot become trusted merely by existing.
    references: dict[str, ArtifactRefV5] = {}
    collect(fixture.manifest, references)
    collect(fixture.manifest.manifest, references)
    collect(projection.checkpoint, references)
    collect(projection.stored_records, references)
    mechanism = MechanismArtifactRepositoryV5(fixture.repository)
    source_identity = manifest_source_identity_sha256_v1(fixture.manifest)
    mechanism_experiment_ids: set[str] = set()
    for stored in projection.stored_records:
        persisted = mechanism.load_existing_evidence_for_record(
            campaign_id=fixture.manifest.manifest.campaign_id,
            round_index=1,
            manifest_ref=fixture.manifest.manifest_ref,
            manifest_source_identity_sha256=source_identity,
            stored_record=stored,
        )
        if persisted is not None:
            collect(persisted, references)
            mechanism_experiment_ids.add(persisted[0].index.experiment_id)

    campaign_id = fixture.manifest.manifest.campaign_id
    events_root = fixture.repository.root / "events" / campaign_id
    if _long_exists(events_root):
        try:
            round_entries = sorted(
                os.scandir(_long_path(events_root)),
                key=lambda item: item.name,
            )
        except OSError as exc:
            raise StudyAuthorityError("closed ancestor event journal could not be enumerated") from exc
        for round_entry in round_entries:
            try:
                is_reparse = bool(
                    getattr(
                        round_entry.stat(follow_symlinks=False),
                        "st_file_attributes",
                        0,
                    )
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                )
                if round_entry.is_symlink() or is_reparse or not round_entry.is_dir(follow_symlinks=False):
                    raise StudyAuthorityError("closed ancestor contains an unknown event journal directory")
            except StudyAuthorityError:
                raise
            except OSError as exc:
                raise StudyAuthorityError("closed ancestor event journal entry could not be inspected") from exc
            if not round_entry.name.isdigit():
                raise StudyAuthorityError("closed ancestor contains an unknown event journal directory")
            round_index = int(round_entry.name)
            events = fixture.repository.load_round_events(
                campaign_id=campaign_id,
                round_index=round_index,
            )
            collect(events, references)
            for event in events:
                event_reference = ArtifactRefV5(
                    f"events/{campaign_id}/{round_index:04d}/{event.sequence:06d}.json",
                    event.sha256,
                )
                references.setdefault(event_reference.relative_path, event_reference)
                payload = fixture.repository.load_round_payload(
                    event.payload_ref,
                    expected_kind=event.event_kind,
                )
                collect(payload, references)
                if type(payload) is RoleCompletionPayloadV5:
                    collect(fixture.repository.load_role_invocation(payload), references)

    checkpoint_reference = ArtifactRefV5(
        "checkpoint.json",
        _sha256(_checkpoint_bytes(fixture.repository, checkpoint)),
    )
    archive_reference = ArtifactRefV5("archive.json", checkpoint.archive_sha256)
    snapshot_raw = _snapshot_bytes(fixture.repository, checkpoint)
    if _sha256(snapshot_raw) != checkpoint.archive_sha256:
        raise StudyAuthorityError("archive bytes differ from the checkpoint archive edge")
    references.setdefault(checkpoint_reference.relative_path, checkpoint_reference)
    references.setdefault(archive_reference.relative_path, archive_reference)

    # Bind control records to their independent typed authorities before
    # collecting the closed inventory.  A digest computed from the bytes being
    # copied is never an authority for a control path.
    evaluator_contract = fixture.manifest.evaluator_contract
    sandbox_profile = fixture.manifest.sandbox_profile
    if sandbox_profile.runtime_source_sha256 != evaluator_contract.evaluator_source_sha256:
        raise StudyAuthorityError("sandbox/runtime evaluator source authorities differ")
    references["evaluator/study-runtime-source.json"] = ArtifactRefV5(
        "evaluator/study-runtime-source.json",
        evaluator_contract.evaluator_source_sha256,
    )
    references["adapter-blobs/study-fixture/transition.bin"] = ArtifactRefV5(
        "adapter-blobs/study-fixture/transition.bin",
        evaluator_contract.identity_transition_contract_sha256,
    )

    round_key = f"{campaign_id}-0001"
    scripted_ref = verify_scripted_role_authority_v1(
        repository=fixture.repository,
        manifest=fixture.manifest.manifest,
        registry=fixture.registry,
        round_index=1,
    )
    references[scripted_ref.relative_path] = scripted_ref
    _scripted_authority = fixture.repository._load_adapter_state_authority(
        namespace="study-scripted-role",
        key=f"{campaign_id}-1",
    )
    if _scripted_authority is None:
        raise StudyAuthorityError("scripted-role authority index is missing")
    references[_scripted_authority[1].relative_path] = _scripted_authority[1]
    mechanism_index_ref, mechanism_index_authority_ref = _typed_state_references(
        fixture.repository,
        namespace="mechanism-v5",
        key=round_key,
        value_type=MechanismPrecommitmentIndexV1,
    )
    references[mechanism_index_ref.relative_path] = mechanism_index_ref
    references[mechanism_index_authority_ref.relative_path] = mechanism_index_authority_ref
    mechanism_index = fixture.repository.load_typed_state(
        namespace="mechanism-v5",
        key=round_key,
        value_type=MechanismPrecommitmentIndexV1,
        repair=False,
    )
    if mechanism_index is None:
        raise StudyAuthorityError("mechanism precommitment index disappeared during snapshot authentication")
    collect(mechanism_index, references)
    for item in projection.stored_records:
        if item.record.policy_revision is not None:
            revision = item.record.policy_revision.sha256
            source_ref, source_authority_ref = _typed_state_references(
                fixture.repository,
                namespace="policy-source",
                key=revision,
                value_type=SourceBundleV5,
            )
            references[source_ref.relative_path] = source_ref
            references[source_authority_ref.relative_path] = source_authority_ref
    for experiment_id in mechanism_experiment_ids:
        for phase in ("binding", "run", "report", "complete"):
            value_ref, authority_ref = _typed_state_references(
                fixture.repository,
                namespace="mechanism-v5",
                key=f"{experiment_id}-{phase}",
                value_type=MechanismExperimentIndexV1,
            )
            references[value_ref.relative_path] = value_ref
            references[authority_ref.relative_path] = authority_ref
            phase_index = fixture.repository.load_typed_state(
                namespace="mechanism-v5",
                key=f"{experiment_id}-{phase}",
                value_type=MechanismExperimentIndexV1,
                repair=False,
            )
            if phase_index is None:
                raise StudyAuthorityError("mechanism experiment index disappeared during snapshot authentication")
            collect(phase_index, references)

    # The final allowlist is exactly the authenticated graph/control refs.
    # Any present path not reached through one of those authorities is rejected.
    expected_paths = set(references)

    files: list[tuple[str, bytes]] = []
    present_paths: set[str] = set()
    for relative, path in _long_files(fixture.repository.root):
        present_paths.add(relative)
        if relative not in expected_paths:
            raise StudyAuthorityError(f"closed ancestor contains an unauthenticated path: {relative}")
        raw = _long_read_bytes(path)
        reference = references.get(relative)
        if reference is None:
            raise StudyAuthorityError(f"closed ancestor control path lacks an authenticated edge: {relative}")
        if relative == fixture.manifest.manifest.panel_plan_ref.relative_path:
            # The panel-plan aggregate is a canonical CampaignPanelPlanV5
            # document; only its referenced episode panels use the historical
            # newline-bearing EvaluationPanelSpec contract.
            fixture.repository.authenticate_exact(reference)
        elif relative.startswith("panels/"):
            fixture.repository.load_evaluation_panel_spec_exact(reference)
        elif relative.endswith(".json"):
            fixture.repository.authenticate_exact(reference)
        else:
            fixture.repository.authenticate_raw_artifact(reference)
        files.append((relative, raw))
    missing_paths = sorted(expected_paths - present_paths)
    if missing_paths:
        raise StudyAuthorityError(
            "closed ancestor is missing authenticated graph/control paths: "
            + ", ".join(missing_paths)
        )
    if not files:
        raise StudyAuthorityError("closed ancestor snapshot is empty")
    return tuple(files)


def _authenticated_closed_snapshot(
    fixture: StudyFixtureV1,
) -> tuple[tuple[str, bytes], ...]:
    """Return the closed authenticated graph or one stable study error.

    Repository authentication deliberately exposes digest and relocation
    failures as low-level artifact exceptions.  They are useful internally,
    but this study boundary must fail closed with the typed authority error so
    callers cannot mistake a corrupt control edge for a valid snapshot.
    """

    try:
        return _authenticated_closed_snapshot_unchecked(fixture)
    except StudyAuthorityError:
        raise
    except (ArtifactRepositoryFailureV5, OSError, TypeError, ValueError, KeyError) as exc:
        raise StudyAuthorityError("closed ancestor graph authority/digest could not be authenticated") from exc


def _clone_snapshot(source: tuple[tuple[str, bytes], ...], destination: Path) -> None:
    if _long_exists(destination):
        try:
            metadata = os.lstat(_long_path(destination))
            attributes = getattr(metadata, "st_file_attributes", 0)
            reparse = bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        except OSError as exc:
            raise StudyAuthorityError("descendant fixture root could not be inspected") from exc
        if stat.S_ISLNK(metadata.st_mode) or reparse or not stat.S_ISDIR(metadata.st_mode) or _long_has_entries(destination):
            raise StudyAuthorityError("descendant fixture root must be a fresh empty directory")
    else:
        os.makedirs(_long_path(destination), exist_ok=True)
    for relative, raw in source:
        target = destination.joinpath(*PurePosixPath(relative).parts)
        _long_write_bytes(target, raw)


def _snapshot_inventory(root: Path) -> tuple[tuple[str, str], ...]:
    """Hash every descendant file with the same long-path-safe walk."""

    return tuple(
        (relative, _sha256(_long_read_bytes(path)))
        for relative, path in _long_files(root)
    )


def _prepare_descendant_preflight(
    *,
    fixture: StudyFixtureV1,
    arm: StudyArmV1,
    store: StudyStoreV1,
    mode: StudyModeV1,
) -> tuple[FixturePreflightV1, RoleRequestV5, ArtifactRefV5, tuple[str, ...]]:
    inputs = _round_two_input(fixture)
    projection = _projection(fixture.repository, fixture)
    recovered = LocalArchiveReducerFactoryV5(fixture.repository).recover_scheduling(inputs, projection)
    selected = select_parent_v5(
        state=recovered.state,
        baseline=inputs.baseline,
        discovery_plan=inputs.panel_plan,
        evaluator_contract=inputs.evaluator_contract,
        stored_records=recovered.stored_records,
    )
    enabled = LocalRoleRequestFactoryV5(
        repository=fixture.repository,
        manifest=inputs.manifest,
        mechanism_adapter=MechanismRoleRequestAdapterV1(
            MechanismArtifactRepositoryV5(fixture.repository), fixture.manifest
        ),
    )
    base = LocalRoleRequestFactoryV5(repository=fixture.repository, manifest=inputs.manifest)
    router = StudyRequestRouterV1(arm, base, enabled)
    request = router.investigator_request(inputs, recovered, selected)
    if type(request) is not RoleRequestV5 or request.role != "investigator":
        raise StudyAuthorityError("round-two router did not return the investigator F request")
    # This create-only source pin must precede the pending request edge.  The
    # historical branch is strict on every later compose/reopen.
    scripted_ref = prepare_scripted_role_authority_v1(
        repository=fixture.repository,
        manifest=fixture.manifest.manifest,
        registry=fixture.registry,
        round_index=_ROUND_TWO,
    )
    scripted_authority = fixture.repository._load_adapter_state_authority(
        namespace="study-scripted-role",
        key=f"{inputs.campaign_id}-2",
    )
    if scripted_authority is None:
        raise StudyAuthorityError("round-two scripted-role authority index is missing")
    scripted_authority_ref = scripted_authority[1]
    call = RoleCallKeyV5(
        campaign_id=inputs.campaign_id,
        round_index=_ROUND_TWO,
        role="investigator",
        role_position=1,
        attempt_kind="primary",
        attempt_index=1,
        request_sha256=request.sha256,
    )
    persisted = fixture.repository.append_role_request(call=call, request=request)
    checkpoint = fixture.repository.load_checkpoint()
    if checkpoint is None:
        raise StudyAuthorityError("descendant preflight lost the common checkpoint")
    checkpoint_raw = _checkpoint_bytes(fixture.repository, checkpoint)
    snapshot_raw = _snapshot_bytes(fixture.repository, checkpoint)
    checkpoint_ref = ArtifactRefV5("checkpoint.json", _sha256(checkpoint_raw))
    snapshot_ref = ArtifactRefV5("archive.json", _sha256(snapshot_raw))
    if _sha256(snapshot_raw) != checkpoint.archive_sha256:
        raise StudyAuthorityError("descendant archive bytes differ from their checkpoint edge")
    evidence_primitive = canonical_primitive_v5(request.role_evidence)
    parent_primitive = {
        "arm": arm,
        "configuration_id": next(
            item.configuration_id
            for item in fixture.registry.configurations
            if item.policy_revision.sha256 == selected.policy_identity_sha256
        ),
        "parent_revision_sha256": selected.policy_identity_sha256,
        "record_ref": None
        if selected.selected_parent_record_ref is None
        else selected.selected_parent_record_ref.to_primitive(),
    }
    evidence_ref = fixture.repository._create_only(
        f"adapter-state/study-sidecars/{arm}-investigator-evidence.json",
        evidence_primitive,
    )
    parent_ref = fixture.repository._create_only(
        f"adapter-state/study-sidecars/{arm}-selected-parent.json",
        parent_primitive,
    )
    evidence_refs = (evidence_ref, parent_ref, scripted_ref, scripted_authority_ref)
    evidence_bytes = tuple(
        fixture.repository.authenticate_exact(reference).content for reference in evidence_refs
    )
    schema = request.schema_authority.canonical_schema_json
    preflight = FixturePreflightV1(
        arm=arm,
        mode=mode,
        fixture_root_identity_sha256=fixture.repository.root_identity_sha256,
        checkpoint_ref=checkpoint_ref,
        snapshot_ref=snapshot_ref,
        call=call,
        request_ref=persisted.reference,
        request_bytes=fixture.repository.authenticate_exact(persisted.reference).content,
        schema_json=schema,
        evidence_refs=evidence_refs,
        request_sha256=request.sha256,
        fixture_root_locator=str(fixture.repository.root),
        checkpoint_bytes=checkpoint_raw,
        snapshot_bytes=snapshot_raw,
        evidence_bytes=evidence_bytes,
    )
    # Authenticate immediately, including current checkpoint/archive bytes, so
    # an accidentally stale or hand-assembled request never reaches the L step.
    if authenticate_fixture_preflight_v1(preflight, require_current=True) != request:
        raise StudyAuthorityError("descendant preflight F does not round-trip")
    return preflight, request, persisted.reference, (selected.policy_identity_sha256,)


def _fresh_process_reopen(
    root: Path,
    manifest_ref: ArtifactRefV5,
    registry: FrozenBehaviorRegistryV1,
    *,
    arm: StudyArmV1 | None = None,
    expected_request_sha256: str | None = None,
) -> dict[str, str | tuple[str, ...]]:
    """Reconstruct selection and (when requested) exact F in a clean process."""

    code = "\n".join(
        (
            "import hashlib",
            "import json",
            "import sys",
            "from pathlib import Path",
            "from core.pit_optimizer_v5.artifacts import ArtifactRefV5",
            "from core.pit_optimizer_v5.contracts import canonical_json_bytes_v5",
            "from core.pit_optimizer_v5.mechanism_artifacts import MechanismArtifactRepositoryV5",
            "from core.pit_optimizer_v5.production_runtime import LocalArchiveReducerFactoryV5, LocalRoleRequestFactoryV5, MechanismRoleRequestAdapterV1",
            "from core.pit_optimizer_v5.provider import MechanismRoleInputV1",
            "from core.pit_optimizer_v5.selection import select_parent_v5",
            "from core.pit_optimizer_v5.two_round_study.driver import _projection, _round_two_input",
            "from core.pit_optimizer_v5.two_round_study.fixtures import reopen_study_fixture_v1",
            "from core.pit_optimizer_v5.two_round_study.registry import build_study_registry_v1",
            "from core.pit_optimizer_v5.two_round_study.runtime_ports import StudyRequestRouterV1",
            "root = Path(sys.argv[1])",
            "ref = ArtifactRefV5(sys.argv[2], sys.argv[3])",
            "fixture = reopen_study_fixture_v1(root=root, manifest_ref=ref, registry=build_study_registry_v1())",
            "projection = _projection(fixture.repository, fixture)",
            "inputs = _round_two_input(fixture)",
            "recovered = LocalArchiveReducerFactoryV5(fixture.repository).recover_scheduling(inputs, projection)",
            "selected = select_parent_v5(state=recovered.state, baseline=inputs.baseline, discovery_plan=inputs.panel_plan, evaluator_contract=inputs.evaluator_contract, stored_records=recovered.stored_records)",
            "checkpoint = fixture.repository.load_checkpoint()",
            "assert checkpoint is not None",
            "checkpoint_ref = ArtifactRefV5('checkpoint.json', hashlib.sha256(canonical_json_bytes_v5(checkpoint.to_primitive())).hexdigest())",
            "checkpoint_raw = fixture.repository.authenticate_exact(checkpoint_ref).content",
            "result = {'manifest_sha256': fixture.manifest_ref.sha256, 'checkpoint_sha256': hashlib.sha256(checkpoint_raw).hexdigest(), 'archive_sha256': checkpoint.archive_sha256, 'selected_parent': selected.policy_identity_sha256}",
            "arm = sys.argv[4] if len(sys.argv) > 4 else ''",
            "if arm:",
            "    base = LocalRoleRequestFactoryV5(repository=fixture.repository, manifest=inputs.manifest)",
            "    enabled = LocalRoleRequestFactoryV5(repository=fixture.repository, manifest=inputs.manifest, mechanism_adapter=MechanismRoleRequestAdapterV1(MechanismArtifactRepositoryV5(fixture.repository), fixture.manifest))",
            "    request = StudyRequestRouterV1(arm, base, enabled).investigator_request(inputs, recovered, selected)",
            "    base_input = request.role_input.base_input if isinstance(request.role_input, MechanismRoleInputV1) else request.role_input",
            "    result.update({'request_sha256': request.sha256, 'request_bytes_sha256': hashlib.sha256(canonical_json_bytes_v5(request.to_primitive())).hexdigest(), 'memory_summary_ids': tuple(item.experiment_id for item in base_input.experiment_summaries)})",
            "print(json.dumps(result, sort_keys=True, separators=(',', ':')))"
        )
    )
    try:
        import ast

        ast.parse(code, filename="<fresh-reopen>")
        compile(code, "<fresh-reopen>", "exec")
    except SyntaxError as exc:
        raise StudyAuthorityError("fresh-process helper source failed stdlib parsing") from exc
    # Keep only interpreter/runtime location variables needed for the clean
    # child to find its standard and already-installed offline dependencies;
    # all provider and dotenv credentials are explicitly blanked.
    env = {
        key: os.environ[key]
        for key in (
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "SYSTEMDRIVE",
            "WINDIR",
            "LOCALAPPDATA",
            "APPDATA",
            "USERPROFILE",
            "PROGRAMFILES",
            "PROGRAMFILES(X86)",
            "TEMP",
            "TMP",
        )
        if key in os.environ
    }
    env.update(
        {
            "PYTHONPATH": str(Path(__file__).resolve().parents[3]),
            "PYTHON_DOTENV_DISABLED": "1",
            "OPENROUTER_API_KEY": "",
            "OPENROUTER": "",
            "ALPACA_API_KEY": "",
            "ALPACA_SECRET_KEY": "",
            "FMP_API_KEY": "",
            "NOTIFY_EMAIL_PASSWORD": "",
        }
    )
    completed = subprocess.run(
        ["py", "-3.13", "-c", code, _long_path(root), manifest_ref.relative_path, manifest_ref.sha256]
        + ([] if arm is None else [arm]),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().replace("\n", " ")[-800:]
        raise StudyAuthorityError(f"fresh-process fixture reopen failed: {detail}")
    try:
        result = json.loads(completed.stdout.strip())
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StudyAuthorityError("fresh-process fixture reopen returned non-JSON output") from exc
    if result.get("manifest_sha256") != manifest_ref.sha256:
        raise StudyAuthorityError("fresh-process fixture reopen returned a different manifest")
    if arm is not None and result.get("request_sha256") != expected_request_sha256:
        raise StudyAuthorityError("fresh-process reconstruction returned a different F request")
    return result


def _rubric_bytes() -> bytes:
    # This rubric describes independent axes and statuses.  It deliberately
    # does not contain an expected response, metric count, or answer example.
    return canonical_json_bytes_v5(
        {
            "schema_version": 1,
            "axes": [
                "trace_integrity",
                "evidence_delivery",
                "evidence_use",
                "production_assessment",
                "case_contrast",
                "experiment_completion",
                "feedback_attribution",
                "optimization_improvement",
                "authored_code_execution",
            ],
            "offline_defaults": {
                "evidence_use": "not_assessed",
                "optimization_improvement": "not_established",
                "authored_code_execution": "not_established",
            },
            "answer_validation": "preserve_raw_invalid_unavailable_pending_and_rejected_states",
        }
    )


def _stored_call(store: StudyStoreV1, preflight: FixturePreflightV1) -> StudyCallRequestV1:
    matches = []
    for reference in store.list_refs(kind="calls"):
        call = StudyCallRequestV1.from_canonical_json(store.read(reference))
        if call.fixture_request_sha256 == preflight.request_sha256:
            matches.append(call)
    if len(matches) != 1:
        raise StudyAuthorityError("study call authority is not unique")
    return matches[0]


def _manifest_for(
    *,
    mode: StudyModeV1,
    fixture: StudyFixtureV1,
    registry: FrozenBehaviorRegistryV1,
    rubric_sha256: str,
    schema_sha256: str,
    parser_sha256: str,
    prompt_sha256: str,
    checkpoint_ref: ArtifactRefV5,
    snapshot_ref: ArtifactRefV5,
    primary_preflight_ref: ArtifactRefV5,
    withheld_preflight_ref: ArtifactRefV5,
    provider_settings: StudyProviderSettingsV1 | None,
) -> StudyManifestV1:
    source_revision = _implementation_source_revision()
    return StudyManifestV1(
        schema_version=1,
        study_id=_STUDY_ID,
        mode=mode,
        source_revision=source_revision,
        fixture_sha256=fixture.manifest_ref.sha256,
        registry_sha256=registry.sha256,
        rubric_sha256=rubric_sha256,
        schema_sha256=schema_sha256,
        parser_sha256=parser_sha256,
        prompt_sha256=prompt_sha256,
        round_one_checkpoint_ref=checkpoint_ref,
        round_one_snapshot_ref=snapshot_ref,
        primary_preflight_ref=primary_preflight_ref,
        withheld_preflight_ref=withheld_preflight_ref,
        resource_limits=study_resource_budget_v1(),
        provider_settings=provider_settings,
        offline_settings=(StudyOfflineSettingsV1(_OFFLINE_FIXTURE_ID) if mode == "offline_fixture" else None),
    )


def _implementation_source_revision() -> str:
    """Resolve the checked-out implementation revision for the final manifest."""

    repository_root = Path(__file__).resolve().parents[3]
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repository_root),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise StudyAuthorityError("study implementation source revision is unavailable") from exc
    revision = completed.stdout.strip()
    if completed.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise StudyAuthorityError("study implementation source revision is unavailable")
    return revision


@dataclass(frozen=True, slots=True)
class PreparedStudyV1:
    root: Path
    mode: StudyModeV1
    store_root: Path
    ancestor_root: Path
    primary_root: Path
    withheld_root: Path
    registry: FrozenBehaviorRegistryV1
    registry_ref: ArtifactRefV5
    rubric_ref: ArtifactRefV5
    round_one_checkpoint_ref: ArtifactRefV5
    round_one_snapshot_ref: ArtifactRefV5
    ancestor_manifest_ref: ArtifactRefV5
    primary_manifest_ref: ArtifactRefV5
    withheld_manifest_ref: ArtifactRefV5
    primary_preflight: FixturePreflightV1
    withheld_preflight: FixturePreflightV1
    primary_preflight_ref: ArtifactRefV5
    withheld_preflight_ref: ArtifactRefV5
    primary_live_call: StudyCallRequestV1
    withheld_live_call: StudyCallRequestV1
    primary_live_call_ref: ArtifactRefV5
    withheld_live_call_ref: ArtifactRefV5
    manifest: StudyManifestV1
    manifest_ref: ArtifactRefV5
    request_comparison: RequestComparisonV1
    request_comparison_ref: ArtifactRefV5
    selected_parent_configuration_id: str
    primary_memory_summary_ids: tuple[str, ...]
    round_one_record_refs: tuple[ArtifactRefV5, ...]
    common_ancestor_files: tuple[tuple[str, str], ...] = ()

    @property
    def primary_live_call_sha256(self) -> str:
        return self.primary_live_call.sha256

    @property
    def withheld_live_call_sha256(self) -> str:
        return self.withheld_live_call.sha256

    @property
    def has_execution_grant(self) -> bool:
        return bool(StudyStoreV1(self.store_repository()).list_refs(kind="grants"))

    def store_repository(self) -> LocalArtifactRepositoryV5:
        return LocalArtifactRepositoryV5(self.store_root)

    def fixture_path(self, arm: StudyArmV1) -> Path:
        if arm == "primary":
            return self.primary_root
        if arm == "withheld":
            return self.withheld_root
        raise StudyContractError("unknown study arm")

    def preflight_for(self, arm: StudyArmV1) -> FixturePreflightV1:
        return self.primary_preflight if arm == "primary" else self.withheld_preflight

    def live_call_for(self, arm: StudyArmV1) -> StudyCallRequestV1:
        return self.primary_live_call if arm == "primary" else self.withheld_live_call


@dataclass(frozen=True, slots=True)
class StudyArmResultV1:
    arm: StudyArmV1
    mode: StudyModeV1
    state: Literal["completed", "incomplete", "rejected"] = "incomplete"
    selected_parent_configuration_id: str | None = None
    terminal_ref: ArtifactRefV5 | None = None
    admission_rejection_ref: ArtifactRefV5 | None = None
    import_ref: ArtifactRefV5 | None = None
    raw_response_ref: ArtifactRefV5 | None = None
    parsed_response_ref: ArtifactRefV5 | None = None
    translated_response_ref: ArtifactRefV5 | None = None
    ordinary_artifact_ref: ArtifactRefV5 | None = None
    draft_refs: tuple[ArtifactRefV5, ...] = ()
    feedback_result: FeedbackRoundResultV5 | None = None
    reopened_feedback_result: FeedbackRoundResultV5 | None = None
    recovered_checkpoint_ref: ArtifactRefV5 | None = None
    production_record_refs: tuple[ArtifactRefV5, ...] = ()
    contrast_refs: tuple[ArtifactRefV5, ...] = ()
    failure_code: str | None = None
    failure_reason: str | None = None
    not_started_reason: str | None = None
    evidence_use: str = "not_assessed"
    experiment_outcome: str = "not_assessed"
    feedback_attribution: str = "not_assessed"
    production_assessment: str = "insufficient_evidence"
    optimization_improvement: str = "not_established"
    authored_code_execution: str = "not_established"

    def __post_init__(self) -> None:
        if self.arm not in {"primary", "withheld"} or self.mode not in {"offline_fixture", "live_study"}:
            raise StudyContractError("study arm result identity is invalid")
        if self.state not in {"completed", "incomplete", "rejected"}:
            raise StudyContractError("study arm result state is invalid")
        if self.state == "completed" and self.feedback_result is None:
            raise StudyAuthorityError("completed arm result lacks its feedback result")
        if type(self.draft_refs) is not tuple or any(type(item) is not ArtifactRefV5 for item in self.draft_refs):
            raise StudyContractError("study arm draft references are invalid")
        if self.state == "rejected" and self.admission_rejection_ref is None:
            raise StudyAuthorityError("rejected arm result lacks its admission rejection")


def _make_live_calls(
    *,
    store: StudyStoreV1,
    manifest: StudyManifestV1,
    registry: FrozenBehaviorRegistryV1,
    primary: FixturePreflightV1,
    withheld: FixturePreflightV1,
) -> tuple[StudyCallRequestV1, StudyCallRequestV1, ArtifactRefV5, ArtifactRefV5]:
    primary_request = authenticate_fixture_preflight_v1(primary, require_current=True)
    withheld_request = authenticate_fixture_preflight_v1(withheld, require_current=True)
    primary_call = build_study_call_v1(
        preflight=primary,
        manifest=manifest,
        fixture_request=primary_request,
        registry=registry,
    )
    withheld_call = build_study_call_v1(
        preflight=withheld,
        manifest=manifest,
        fixture_request=withheld_request,
        registry=registry,
    )
    primary_ref = store.put(kind="calls", key=primary_call.sha256, content=primary_call.canonical_bytes())
    withheld_ref = store.put(kind="calls", key=withheld_call.sha256, content=withheld_call.canonical_bytes())
    return primary_call, withheld_call, primary_ref, withheld_ref


def prepare_two_round_study_v1(
    *,
    root: Path,
    mode: StudyModeV1,
    provider_settings: Mapping[str, object] | None,
) -> PreparedStudyV1:
    """Prepare both exact arms without issuing a study grant or attempt."""

    if mode not in {"offline_fixture", "live_study"}:
        raise StudyContractError("study mode is invalid")
    provider = _provider_settings(mode, provider_settings)
    container = _absolute_fresh_container(root)
    ancestor_root = _fresh_child(container, "round-one-ancestor")
    store_root = _fresh_child(container, "study-store")
    registry = build_study_registry_v1()
    verify_registry_v1(registry)
    # The fixture and artifact repository implementations predate the closed
    # graph's long-path walker and perform a few pathlib checks.  Feed them
    # extended Windows spellings while retaining ordinary absolute roots in
    # PreparedStudyV1 for callers; every file operation remains exact and
    # long-path safe at this boundary.
    ancestor = create_study_fixture_v1(root=Path(_long_path(ancestor_root)), registry=registry)
    store = StudyStoreV1(LocalArtifactRepositoryV5(Path(_long_path(store_root))))

    round_one_inputs, round_one_dependencies = compose_study_round_v1(
        fixture=ancestor,
        round_index=1,
        arm="primary",
        store=store,
        ledger=None,
        imported=None,
    )
    round_one = run_feedback_round_v5(inputs=round_one_inputs, dependencies=round_one_dependencies)
    projection, selected, memory_ids = _round_one_gate(ancestor, round_one)
    snapshot = _authenticated_closed_snapshot(ancestor)
    checkpoint = ancestor.repository.load_checkpoint()
    if checkpoint is None:
        raise StudyAuthorityError("round-one checkpoint disappeared before cloning")
    checkpoint_raw = _checkpoint_bytes(ancestor.repository, checkpoint)
    snapshot_raw = _snapshot_bytes(ancestor.repository, checkpoint)
    checkpoint_ref = ArtifactRefV5("checkpoint.json", _sha256(checkpoint_raw))
    snapshot_ref = ArtifactRefV5("archive.json", _sha256(snapshot_raw))
    ancestor_manifest_ref = ancestor.manifest_ref
    # The child restart must reconstruct from persisted identities after the
    # live round's runtime objects have been released.  Keeping the immutable
    # fixture/registry objects is harmless; retaining dependencies/results or
    # the projection would make this only a half-restart assertion.
    del round_one_dependencies, round_one_inputs, round_one, projection
    gc.collect()
    _fresh_process_reopen(ancestor_root, ancestor_manifest_ref, registry)

    primary_root = _fresh_child(container, "primary-descendant")
    withheld_root = _fresh_child(container, "withheld-descendant")
    _clone_snapshot(snapshot, primary_root)
    _clone_snapshot(snapshot, withheld_root)
    expected_inventory = tuple((relative, _sha256(raw)) for relative, raw in snapshot)
    for descendant_root in (primary_root, withheld_root):
        if _snapshot_inventory(descendant_root) != expected_inventory:
            raise StudyAuthorityError("descendant closed graph differs from the authenticated ancestor snapshot")
    primary_fixture = reopen_study_fixture_v1(
        root=Path(_long_path(primary_root)),
        manifest_ref=ancestor_manifest_ref,
        registry=registry,
    )
    withheld_fixture = reopen_study_fixture_v1(
        root=Path(_long_path(withheld_root)),
        manifest_ref=ancestor_manifest_ref,
        registry=registry,
    )
    if (
        primary_fixture.repository.root_identity_sha256 == withheld_fixture.repository.root_identity_sha256
        or primary_fixture.manifest_ref.sha256 != withheld_fixture.manifest_ref.sha256
        or primary_fixture.manifest_ref.sha256 != ancestor_manifest_ref.sha256
    ):
        raise StudyAuthorityError("descendant fixture identity or manifest hash is not authenticated")
    primary_preflight, _primary_request, _primary_request_ref, _ = _prepare_descendant_preflight(
        fixture=primary_fixture, arm="primary", store=store, mode=mode
    )
    withheld_preflight, _withheld_request, _withheld_request_ref, _ = _prepare_descendant_preflight(
        fixture=withheld_fixture, arm="withheld", store=store, mode=mode
    )
    # Reconstruct the selected parent and exact persisted F independently in
    # each descendant.  This is a fresh interpreter read of the actual graph,
    # rather than a manifest-only reopen or a copy of the parent process's
    # selected objects.
    _fresh_process_reopen(
        primary_root,
        ancestor_manifest_ref,
        registry,
        arm="primary",
        expected_request_sha256=primary_preflight.request_sha256,
    )
    _fresh_process_reopen(
        withheld_root,
        ancestor_manifest_ref,
        registry,
        arm="withheld",
        expected_request_sha256=withheld_preflight.request_sha256,
    )
    if primary_preflight.checkpoint_bytes != checkpoint_raw or withheld_preflight.checkpoint_bytes != checkpoint_raw:
        raise StudyAuthorityError("descendant checkpoint bytes differ from the common ancestor")
    if primary_preflight.snapshot_bytes != snapshot_raw or withheld_preflight.snapshot_bytes != snapshot_raw:
        raise StudyAuthorityError("descendant archive bytes differ from the common ancestor")
    primary_preflight_ref = store.put(
        kind="preflights", key="primary", content=primary_preflight.canonical_bytes()
    )
    withheld_preflight_ref = store.put(
        kind="preflights", key="withheld", content=withheld_preflight.canonical_bytes()
    )
    registry_ref = store.put(kind="registry", key=registry.sha256, content=registry.canonical_bytes())
    rubric_ref = store.put(kind="rubrics", key="v1", content=_rubric_bytes())
    schema_primary = study_response_schema_v1(
        fixture_request=authenticate_fixture_preflight_v1(primary_preflight, require_current=True),
        configuration_ids=tuple(item.configuration_id for item in registry.configurations),
    )
    schema_withheld = study_response_schema_v1(
        fixture_request=authenticate_fixture_preflight_v1(withheld_preflight, require_current=True),
        configuration_ids=tuple(item.configuration_id for item in registry.configurations),
    )
    if schema_primary != schema_withheld:
        raise StudyAuthorityError("paired study schemas differ before comparison")
    parser_raw = study_parser_authority_bytes_v1()
    prompt_raw = study_prompt_bytes_v1(registry=registry)
    schema_sha256 = _sha256(schema_primary)
    parser_sha256 = _sha256(parser_raw)
    prompt_sha256 = _sha256(prompt_raw)
    for kind, key, raw in (
        ("schemas", "v1", schema_primary),
        ("parsers", "v1", parser_raw),
        ("prompts", "v1", prompt_raw),
    ):
        store.put(kind=kind, key=key, content=raw)
    provisional = _manifest_for(
        mode=mode,
        fixture=primary_fixture,
        registry=registry,
        rubric_sha256=rubric_ref.sha256,
        schema_sha256=schema_sha256,
        parser_sha256=parser_sha256,
        prompt_sha256=prompt_sha256,
        checkpoint_ref=checkpoint_ref,
        snapshot_ref=snapshot_ref,
        primary_preflight_ref=primary_preflight_ref,
        withheld_preflight_ref=withheld_preflight_ref,
        provider_settings=provider,
    )
    primary_call, withheld_call, primary_call_ref, withheld_call_ref = _make_live_calls(
        store=store,
        manifest=provisional,
        registry=registry,
        primary=primary_preflight,
        withheld=withheld_preflight,
    )
    comparison = compare_study_requests_v1(primary=primary_preflight, withheld=withheld_preflight, store=store)
    comparison_ref = store.put(kind="comparisons", key="v1", content=comparison.canonical_bytes())
    manifest = _manifest_for(
        mode=mode,
        fixture=primary_fixture,
        registry=registry,
        rubric_sha256=rubric_ref.sha256,
        schema_sha256=schema_sha256,
        parser_sha256=parser_sha256,
        prompt_sha256=prompt_sha256,
        checkpoint_ref=checkpoint_ref,
        snapshot_ref=snapshot_ref,
        primary_preflight_ref=primary_preflight_ref,
        withheld_preflight_ref=withheld_preflight_ref,
        provider_settings=provider,
    )
    if manifest != provisional:
        raise StudyAuthorityError("study manifest changed between request and comparison preflights")
    manifest_ref = store.put_contract(kind="manifests", key=manifest.sha256, value=manifest)
    # The calls were created against this exact manifest; reconstruct and
    # authenticate their bytes after the immutable manifest is persisted.
    rebuilt_primary, rebuilt_withheld, rebuilt_primary_ref, rebuilt_withheld_ref = _make_live_calls(
        store=store,
        manifest=manifest,
        registry=registry,
        primary=primary_preflight,
        withheld=withheld_preflight,
    )
    if rebuilt_primary != primary_call or rebuilt_withheld != withheld_call:
        raise StudyAuthorityError("persisted L request changed after final manifest freeze")
    if rebuilt_primary_ref != primary_call_ref or rebuilt_withheld_ref != withheld_call_ref:
        raise StudyAuthorityError("persisted L references changed after final manifest freeze")
    common_hashes = tuple((relative, _sha256(raw)) for relative, raw in snapshot)
    return PreparedStudyV1(
        root=container,
        mode=mode,
        store_root=store_root,
        ancestor_root=ancestor_root,
        primary_root=primary_root,
        withheld_root=withheld_root,
        registry=registry,
        registry_ref=registry_ref,
        rubric_ref=rubric_ref,
        round_one_checkpoint_ref=checkpoint_ref,
        round_one_snapshot_ref=snapshot_ref,
        ancestor_manifest_ref=ancestor_manifest_ref,
        primary_manifest_ref=primary_fixture.manifest_ref,
        withheld_manifest_ref=withheld_fixture.manifest_ref,
        primary_preflight=primary_preflight,
        withheld_preflight=withheld_preflight,
        primary_preflight_ref=primary_preflight_ref,
        withheld_preflight_ref=withheld_preflight_ref,
        primary_live_call=primary_call,
        withheld_live_call=withheld_call,
        primary_live_call_ref=primary_call_ref,
        withheld_live_call_ref=withheld_call_ref,
        manifest=manifest,
        manifest_ref=manifest_ref,
        request_comparison=comparison,
        request_comparison_ref=comparison_ref,
        selected_parent_configuration_id="S",
        primary_memory_summary_ids=memory_ids,
        round_one_record_refs=checkpoint.record_refs,
        common_ancestor_files=common_hashes,
    )


def _preflight_call(prepared: PreparedStudyV1, arm: StudyArmV1) -> tuple[FixturePreflightV1, StudyCallRequestV1]:
    preflight = prepared.preflight_for(arm)
    call = prepared.live_call_for(arm)
    if preflight.mode != prepared.mode or call.arm != arm or call.fixture_request_sha256 != preflight.request_sha256:
        raise StudyAuthorityError("prepared arm request authority differs")
    if call.preflight_ref != (prepared.primary_preflight_ref if arm == "primary" else prepared.withheld_preflight_ref):
        raise StudyAuthorityError("prepared L request preflight reference differs")
    return preflight, call


def _rejection_for(store: StudyStoreV1, request_sha256: str) -> tuple[ArtifactRefV5, StudyAdmissionRejectionV1] | None:
    matches: list[tuple[ArtifactRefV5, StudyAdmissionRejectionV1]] = []
    for reference in store.list_refs(kind="admission-rejections"):
        value = StudyAdmissionRejectionV1.from_canonical_json(store.read(reference))
        if value.request_sha256 == request_sha256:
            matches.append((reference, value))
    if len(matches) > 1:
        raise StudyAuthorityError("study request has duplicate admission rejections")
    return matches[0] if matches else None


def _import_for(store: StudyStoreV1, arm: StudyArmV1) -> tuple[ArtifactRefV5, StudyImportV1] | None:
    matches: list[tuple[ArtifactRefV5, StudyImportV1]] = []
    for reference in store.list_refs(kind="imports"):
        value = StudyImportV1.from_canonical_json(store.read(reference))
        if value.arm == arm:
            matches.append((reference, value))
    if len(matches) > 1:
        raise StudyAuthorityError("study arm has duplicate imports")
    return matches[0] if matches else None


def _terminal_result(
    *,
    prepared: PreparedStudyV1,
    arm: StudyArmV1,
    terminal: AuthenticatedStudyTerminalV1,
    selected_parent_configuration_id: str,
) -> StudyArmResultV1:
    raw_ref = terminal.terminal.response_ref
    parsed_ref = terminal.terminal.parsed_ref
    if terminal.failure_code is not None:
        return StudyArmResultV1(
            arm=arm,
            mode=prepared.mode,
            state="incomplete",
            selected_parent_configuration_id=selected_parent_configuration_id,
            terminal_ref=terminal.reference,
            raw_response_ref=raw_ref,
            parsed_response_ref=parsed_ref,
            failure_code=terminal.failure_code.value,
            failure_reason=terminal.terminal.failure_reason,
            not_started_reason="terminal_failure",
        )
    return StudyArmResultV1(
        arm=arm,
        mode=prepared.mode,
        state="incomplete",
        selected_parent_configuration_id=selected_parent_configuration_id,
        terminal_ref=terminal.reference,
        raw_response_ref=raw_ref,
        parsed_response_ref=parsed_ref,
    )


def _authenticated_arm_graph_refs(
    *,
    arm: StudyArmV1,
    fixture: StudyFixtureV1,
    ledger: StudyLedgerV1,
    imported: StudyImportV1,
    required: bool,
) -> tuple[ArtifactRefV5, ...]:
    """Return only the persisted commitment graph for ``arm``.

    The ledger store is intentionally shared by the two descendants.  A
    directory-wide namespace listing therefore includes the other arm's
    commitment and mechanism graph as soon as its preflight has run.  Results
    must expose only the graph selected by this arm's stable commitment slot;
    a precommitment failure may legitimately have no graph at all.  Runtime
    recovery authenticates its own fixture graph separately.
    """

    commitment_slot_hash = _sha256(
        canonical_json_bytes_v5((arm, fixture.manifest.manifest.campaign_id, _ROUND_TWO))
    )
    commitment_path = f"adapter-blobs/study-v1-commitments/{arm}-{commitment_slot_hash}.bin"
    commitment_refs = tuple(
        reference
        for reference in ledger.store.list_refs(kind="commitments")
        if reference.relative_path == commitment_path
    )
    if not commitment_refs and not required:
        return ()
    if len(commitment_refs) != 1:
        raise StudyAuthorityError("arm commitment index is not unique")
    commitment_ref = commitment_refs[0]
    try:
        commitment_raw = ledger.store.read(commitment_ref)
        if _sha256(commitment_raw) != commitment_ref.sha256:
            raise StudyAuthorityError("arm commitment index differs from its bytes")
        index = StudyCommitmentIndexV1.from_canonical_json(commitment_raw)
    except StudyAuthorityError:
        raise
    except (UnicodeDecodeError, StudyContractError, TypeError, ValueError, ArithmeticError) as exc:
        raise StudyAuthorityError("arm commitment index is not authenticated") from exc
    if (
        index.storage_ref != commitment_ref
        or index.arm != arm
        or index.campaign_id != fixture.manifest.manifest.campaign_id
        or index.round_index != _ROUND_TWO
        or index.import_ref != imported.storage_ref
        or index.draft_ref not in imported.draft_refs
    ):
        raise StudyAuthorityError("arm commitment index differs from its imported authority")

    # These are the ledger-owned bytes of the commitment graph.  The fixture
    # package, parent source, checkpoint and journal payloads remain rooted in
    # this arm's repository and are authenticated by compose_study_round_v1;
    # they are deliberately not replaced with another arm's shared-store
    # entries here.
    expected = (
        ("commitments", commitment_ref),
        ("import-drafts", index.draft_ref),
        ("draft-bindings", index.binding_ref),
        ("mechanism-specs", index.spec_ref),
        ("mechanism-corpora", index.corpus_ref),
        ("contrasts", index.contrast_ref),
    )
    result: list[ArtifactRefV5] = []
    for kind, expected_ref in expected:
        matches = tuple(item for item in ledger.store.list_refs(kind=kind) if item == expected_ref)
        if len(matches) != 1:
            raise StudyAuthorityError(f"arm {kind} graph reference is missing or ambiguous")
        raw = ledger.store.read(expected_ref)
        if _sha256(raw) != expected_ref.sha256:
            raise StudyAuthorityError(f"arm {kind} graph bytes differ from their references")
        result.append(expected_ref)
    return tuple(result)


def _round_two_runtime(
    *,
    prepared: PreparedStudyV1,
    arm: StudyArmV1,
    ledger: StudyLedgerV1,
    imported: StudyImportV1,
    selected_parent_configuration_id: str,
) -> tuple[FeedbackRoundResultV5, FeedbackRoundResultV5, tuple[ArtifactRefV5, ...], tuple[ArtifactRefV5, ...]]:
    fixture = reopen_study_fixture_v1(
        root=prepared.fixture_path(arm),
        manifest_ref=(prepared.primary_manifest_ref if arm == "primary" else prepared.withheld_manifest_ref),
        registry=prepared.registry,
    )
    preflight, call = _preflight_call(prepared, arm)
    request = authenticate_fixture_preflight_v1(preflight, require_current=False)
    events = fixture.repository.load_round_events(
        campaign_id=fixture.manifest.manifest.campaign_id,
        round_index=_ROUND_TWO,
    )
    package = None
    for event in events:
        payload = fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
        if type(payload) is RoleCompletionPayloadV5 and payload.role == "investigator":
            package = fixture.repository.load_role_invocation(payload)
            break
    if package is not None:
        verify_imported_package_v1(
            package=package,
            record=imported,
            store=ledger.store,
            ledger=ledger,
            fixture_repository=fixture.repository,
        )
    inputs, dependencies = compose_study_round_v1(
        fixture=fixture,
        round_index=_ROUND_TWO,
        arm=arm,
        store=ledger.store,
        ledger=ledger,
        imported=imported,
    )
    first = run_feedback_round_v5(inputs=inputs, dependencies=dependencies)
    checkpoint = fixture.repository.load_checkpoint()
    if first.status == "completed" and checkpoint is None:
        raise StudyAuthorityError("completed round two lacks a durable checkpoint")
    # Drop all runtime instances and rebuild from the persisted graph before a
    # read-only second pass.  The imported investigator must be replayed with
    # zero external usage, and the ordinary runtime may take its fast return.
    del dependencies, inputs, package, fixture
    gc.collect()
    reopened_fixture = reopen_study_fixture_v1(
        root=prepared.fixture_path(arm),
        manifest_ref=(prepared.primary_manifest_ref if arm == "primary" else prepared.withheld_manifest_ref),
        registry=prepared.registry,
    )
    imported_ref_value = _import_for(ledger.store, arm)
    if imported_ref_value is None:
        raise StudyAuthorityError("completed round two has no persisted import")
    imported_ref, reopened_import = imported_ref_value
    request = authenticate_fixture_preflight_v1(preflight, require_current=False)
    authenticate_study_import_v1(
        store=ledger.store,
        ledger=ledger,
        record=reopened_import,
        fixture_repository=reopened_fixture.repository,
        request=request,
        call=preflight.call,
    )
    inputs_reopen, dependencies_reopen = compose_study_round_v1(
        fixture=reopened_fixture,
        round_index=_ROUND_TWO,
        arm=arm,
        store=ledger.store,
        ledger=StudyLedgerV1(ledger.store, prepared.manifest, ledger.grant, approval=None),
        imported=reopened_import,
    )
    second = run_feedback_round_v5(inputs=inputs_reopen, dependencies=dependencies_reopen)
    # Report the durable graph authenticated for this arm only.  The store is
    # shared by both descendants, so namespace-wide enumeration would leak the
    # other arm's commitment/draft graph into this result and into recovery.
    production_refs = tuple(first.record_refs)
    contrast_refs = _authenticated_arm_graph_refs(
        arm=arm,
        fixture=reopened_fixture,
        ledger=ledger,
        imported=reopened_import,
        required=first.status in {
            "completed",
            "no_novel_hypothesis",
            "novelty_exhausted",
            "critic_unavailable",
        },
    )
    return first, second, production_refs, contrast_refs


def _validate_ledger(prepared: PreparedStudyV1, ledger: StudyLedgerV1) -> None:
    if type(ledger) is not StudyLedgerV1 or ledger.manifest != prepared.manifest:
        raise StudyAuthorityError("study ledger does not bind the prepared manifest")
    if ledger.grant.mode != prepared.mode or ledger.grant.study_id != prepared.manifest.study_id:
        raise StudyAuthorityError("study grant mode or study identity differs")
    if ledger.grant.manifest_sha256 != prepared.manifest.sha256:
        raise StudyAuthorityError("study grant does not bind the prepared manifest")


def execute_study_arm_v1(
    *,
    prepared: PreparedStudyV1,
    arm: StudyArmV1,
    ledger: StudyLedgerV1,
    gateway,
) -> StudyArmResultV1:
    """Use one currently approved ledger slot, then run and reopen round two."""

    if type(prepared) is not PreparedStudyV1 or arm not in {"primary", "withheld"}:
        raise StudyContractError("study execution inputs are invalid")
    _validate_ledger(prepared, ledger)
    from .transport import StudyOpenRouterGatewayV1

    if prepared.mode == "live_study":
        if not isinstance(gateway, StudyOpenRouterGatewayV1):
            raise StudyAuthorityError("live study execution requires its bound gateway")
    elif isinstance(gateway, StudyOpenRouterGatewayV1):
        raise StudyAuthorityError("offline fixture execution cannot use the provider gateway")
    if ledger.approval is None:
        raise StudyAuthorityError("study execution requires current approval")
    preflight, call = _preflight_call(prepared, arm)
    request = authenticate_fixture_preflight_v1(preflight, require_current=True)
    try:
        terminal = run_study_call_v1(
            request=call,
            fixture_request=request,
            ledger=ledger,
            gateway=gateway,
            deadline_monotonic=time.monotonic() + float(ledger.grant.per_call_deadline_seconds),
        )
    except StudyPendingAccounting as exc:
        rejection = _rejection_for(ledger.store, call.sha256)
        if rejection is not None:
            return StudyArmResultV1(
                arm=arm,
                mode=prepared.mode,
                state="rejected",
                selected_parent_configuration_id=prepared.selected_parent_configuration_id,
                admission_rejection_ref=rejection[0],
                failure_reason=str(exc),
                not_started_reason="admission_rejected",
            )
        return StudyArmResultV1(
            arm=arm,
            mode=prepared.mode,
            state="incomplete",
            selected_parent_configuration_id=prepared.selected_parent_configuration_id,
            failure_reason=str(exc),
            not_started_reason="pending_accounting",
        )
    except (StudyAdmissionError, StudyAuthorityError, StudyContractError) as exc:
        rejection = _rejection_for(ledger.store, call.sha256)
        if rejection is None:
            raise
        return StudyArmResultV1(
            arm=arm,
            mode=prepared.mode,
            state="rejected",
            selected_parent_configuration_id=prepared.selected_parent_configuration_id,
            admission_rejection_ref=rejection[0],
            failure_reason=str(exc),
            not_started_reason="admission_rejected",
        )
    if terminal.failure_code is not None:
        return _terminal_result(
            prepared=prepared,
            arm=arm,
            terminal=terminal,
            selected_parent_configuration_id=prepared.selected_parent_configuration_id,
        )
    imported = create_study_import_v1(
        store=ledger.store,
        ledger=ledger,
        preflight=preflight,
        terminal=terminal,
    )
    first, second, production_refs, contrast_refs = _round_two_runtime(
        prepared=prepared,
        arm=arm,
        ledger=ledger,
        imported=imported,
        selected_parent_configuration_id=prepared.selected_parent_configuration_id,
    )
    final_state: Literal["completed", "incomplete"] = "completed" if first.status in {
        "completed",
        "no_novel_hypothesis",
        "novelty_exhausted",
        "critic_unavailable",
    } else "incomplete"
    return StudyArmResultV1(
        arm=arm,
        mode=prepared.mode,
        state=final_state,
        selected_parent_configuration_id=prepared.selected_parent_configuration_id,
        terminal_ref=terminal.reference,
        import_ref=imported.storage_ref,
        raw_response_ref=imported.original_response_ref,
        parsed_response_ref=imported.parsed_envelope_ref,
        translated_response_ref=imported.translated_ref,
        ordinary_artifact_ref=imported.ordinary_artifact_ref,
        draft_refs=imported.draft_refs,
        feedback_result=first,
        reopened_feedback_result=second,
        recovered_checkpoint_ref=(
            ArtifactRefV5("checkpoint.json", _sha256(canonical_json_bytes_v5(reopened_fixture_checkpoint_bytes(prepared, arm))))
            if first.checkpoint is not None
            else None
        ),
        production_record_refs=production_refs,
        contrast_refs=contrast_refs,
        experiment_outcome=("completed" if first.status == "completed" else first.status),
        feedback_attribution="inconclusive" if prepared.mode == "offline_fixture" else "not_assessed",
    )


def reopened_fixture_checkpoint_bytes(prepared: PreparedStudyV1, arm: StudyArmV1) -> object:
    fixture = reopen_study_fixture_v1(
        root=prepared.fixture_path(arm),
        manifest_ref=(prepared.primary_manifest_ref if arm == "primary" else prepared.withheld_manifest_ref),
        registry=prepared.registry,
    )
    return fixture.repository.load_checkpoint()


def resume_study_arm_v1(
    *,
    prepared: PreparedStudyV1,
    arm: StudyArmV1,
    ledger: StudyLedgerV1,
) -> StudyArmResultV1:
    """Recover only persisted terminal/import/runtime state; never dispatch."""

    if type(prepared) is not PreparedStudyV1 or arm not in {"primary", "withheld"}:
        raise StudyContractError("study recovery inputs are invalid")
    _validate_ledger(prepared, ledger)
    preflight, call = _preflight_call(prepared, arm)
    try:
        terminal = recover_study_call_v1(request=call, ledger=ledger)
    except StudyPendingAccounting:
        # A reservation with a dispatch claim but no authenticated response
        # remains a reservation-only pending state.  Recovery must expose it
        # as incomplete and must never reinterpret it as a safe retry.
        return StudyArmResultV1(
            arm=arm,
            mode=prepared.mode,
            state="incomplete",
            selected_parent_configuration_id=prepared.selected_parent_configuration_id,
            not_started_reason="pending_accounting",
        )
    except StudyAdmissionError as exc:
        # The ledger intentionally raises on an already persisted local
        # admission rejection.  Reopen that durable rejection as a rejected
        # result without attempting dispatch or manufacturing a terminal.
        rejection = _rejection_for(ledger.store, call.sha256)
        if rejection is None:
            raise
        return StudyArmResultV1(
            arm=arm,
            mode=prepared.mode,
            state="rejected",
            selected_parent_configuration_id=prepared.selected_parent_configuration_id,
            admission_rejection_ref=rejection[0],
            failure_reason=str(exc),
            not_started_reason="admission_rejected",
        )
    if terminal is None:
        rejection = _rejection_for(ledger.store, call.sha256)
        if rejection is not None:
            return StudyArmResultV1(
                arm=arm,
                mode=prepared.mode,
                state="rejected",
                selected_parent_configuration_id=prepared.selected_parent_configuration_id,
                admission_rejection_ref=rejection[0],
                not_started_reason="admission_rejected",
            )
        return StudyArmResultV1(
            arm=arm,
            mode=prepared.mode,
            state="incomplete",
            selected_parent_configuration_id=prepared.selected_parent_configuration_id,
            not_started_reason="not_reserved",
        )
    if terminal.failure_code is not None:
        return _terminal_result(
            prepared=prepared,
            arm=arm,
            terminal=terminal,
            selected_parent_configuration_id=prepared.selected_parent_configuration_id,
        )
    imported_pair = _import_for(ledger.store, arm)
    if imported_pair is None:
        return StudyArmResultV1(
            arm=arm,
            mode=prepared.mode,
            state="incomplete",
            selected_parent_configuration_id=prepared.selected_parent_configuration_id,
            terminal_ref=terminal.reference,
            not_started_reason="import_missing",
        )
    imported_ref, imported = imported_pair
    fixture = reopen_study_fixture_v1(
        root=prepared.fixture_path(arm),
        manifest_ref=(prepared.primary_manifest_ref if arm == "primary" else prepared.withheld_manifest_ref),
        registry=prepared.registry,
    )
    request = authenticate_fixture_preflight_v1(preflight, require_current=False)
    authenticate_study_import_v1(
        store=ledger.store,
        ledger=ledger,
        record=imported,
        fixture_repository=fixture.repository,
        request=request,
        call=preflight.call,
    )
    first, second, production_refs, contrast_refs = _round_two_runtime(
        prepared=prepared,
        arm=arm,
        ledger=StudyLedgerV1(ledger.store, prepared.manifest, ledger.grant, approval=None),
        imported=imported,
        selected_parent_configuration_id=prepared.selected_parent_configuration_id,
    )
    return StudyArmResultV1(
        arm=arm,
        mode=prepared.mode,
        state="completed" if first.status in {"completed", "no_novel_hypothesis", "novelty_exhausted", "critic_unavailable"} else "incomplete",
        selected_parent_configuration_id=prepared.selected_parent_configuration_id,
        terminal_ref=terminal.reference,
        import_ref=imported_ref,
        raw_response_ref=imported.original_response_ref,
        parsed_response_ref=imported.parsed_envelope_ref,
        translated_response_ref=imported.translated_ref,
        ordinary_artifact_ref=imported.ordinary_artifact_ref,
        draft_refs=imported.draft_refs,
        feedback_result=first,
        reopened_feedback_result=second,
        production_record_refs=production_refs,
        contrast_refs=contrast_refs,
        experiment_outcome=("completed" if first.status == "completed" else first.status),
        feedback_attribution="inconclusive" if prepared.mode == "offline_fixture" else "not_assessed",
    )


__all__ = [
    "PreparedStudyV1",
    "StudyArmResultV1",
    "execute_study_arm_v1",
    "prepare_two_round_study_v1",
    "resume_study_arm_v1",
]
