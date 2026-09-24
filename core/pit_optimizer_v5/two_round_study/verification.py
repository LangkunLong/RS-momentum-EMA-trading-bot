"""Read-only verification and reconstruction for the V5 two-round study.

The driver deliberately keeps :class:`PreparedStudyV1` transient.  This
module is the opposite boundary: it opens the persisted graph, authenticates
every authority edge it can name, and derives the scientific verdicts without
calling preparation, recovery, a provider, or a candidate worker.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal, Mapping

from core.pit_optimizer_v5.artifacts import ArtifactRefV5, LocalArtifactRepositoryV5
from core.pit_optimizer_v5.contracts import canonical_json_bytes_v5, canonical_primitive_v5
from core.pit_optimizer_v5.mechanism_artifacts import (
    MechanismArtifactRepositoryV5,
    manifest_source_identity_sha256_v1,
)
from core.pit_optimizer_v5.candidate_ir import PolicyRevisionIdentityV5, SourceBundleV5
from core.pit_optimizer_v5.memory import RoleCompletionPayloadV5, StoredExperimentRecordV5, project_investigator_memory_v5
from core.pit_optimizer_v5.production_runtime import LocalArchiveReducerFactoryV5
from core.pit_optimizer_v5.provider import RoleInvocationPackageV5, role_request_artifact_primitive_v5
from core.pit_optimizer_v5.selection import select_parent_v5
from core.strategy_policy.contracts_v3 import ExitSnapshotV3

from .comparison import RequestComparisonV1, compare_study_requests_v1
from .compiler import StudyCommitmentIndexV1, _commitment_slot_key
from .contracts import (
    ArtifactBackedReasonV1,
    StudyAuthorityError,
    StudyCaseContrastV1,
    StudyContractError,
    StudyGateV1,
    StudyManifestV1,
    StudyVerdictsV1,
    STUDY_SCHEMA_VERSION_V1,
)
from .contrast import CaseContrastResultV1, StudyContrastV1, evaluate_case_contrast_v1
from .driver import (
    PreparedStudyV1,
    _authenticated_closed_snapshot,
    _configuration_for_record,
    _long_path,
    _projection,
    _round_two_input,
)
from .fixtures import reopen_study_fixture_v1
from .imports import (
    StudyImportV1,
    authenticate_study_import_v1,
    verify_imported_package_v1,
)
from .ledger import (
    StudyAdmissionRejectionV1,
    StudyCallTerminalV1,
    StudyLedgerV1,
    StudyReservationV1,
)
from .live_calls import (
    FixturePreflightV1,
    StudyCallRequestV1,
    authenticate_fixture_preflight_v1,
)
from core.pit_optimizer_v5.mechanism_reports import build_mechanism_evidence_report_v1
from .registry import FrozenBehaviorRegistryV1, registry_decision_v1, verify_registry_v1
from .runtime_ports import (
    StudyMechanismComposerV1,
    StudyScriptedRoleInvokerV1,
    verify_scripted_role_authority_v1,
)
from .store import StudyStoreV1


_ARM_NAMES = ("primary", "withheld")
_REVIEW_AXES = ("evidence_interpretation", "revision_quality", "claim_pattern")
_FIXED_CHILDREN = ("round-one-ancestor", "study-store", "primary-descendant", "withheld-descendant")
_STORE_KINDS = (
    "manifests",
    "registry",
    "rubrics",
    "preflights",
    "schemas",
    "parsers",
    "prompts",
    "calls",
    "comparisons",
    "grants",
    "requests",
    "reservations",
    "dispatches",
    "responses",
    "raw-responses",
    "raw-response-failures",
    "observed-raw-responses",
    "response-observations",
    "provider-diagnostics",
    "transport-observations",
    "reconciliations",
    "terminals",
    "parsed",
    "admission-rejections",
    "imports",
    "import-translations",
    "import-artifacts",
    "import-drafts",
    "commitments",
    "draft-bindings",
    "mechanism-specs",
    "mechanism-corpora",
    "contrasts",
    "mechanism-links",
)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _ref_primitive(ref: ArtifactRefV5) -> dict[str, str]:
    return ref.to_primitive()


def _json_bytes(value: object) -> bytes:
    return canonical_json_bytes_v5(value)


def _strict_json(raw: bytes, label: str) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise StudyAuthorityError(f"{label} contains duplicate fields")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise StudyAuthorityError(f"{label} is not canonical JSON") from exc
    if canonical_json_bytes_v5(value) != raw:
        raise StudyAuthorityError(f"{label} is not canonical JSON")
    return value


def _read_store(store: StudyStoreV1, ref: ArtifactRefV5, label: str) -> bytes:
    try:
        raw = store.read(ref)
    except Exception as exc:  # noqa: BLE001 - convert every storage failure to study authority
        raise StudyAuthorityError(f"{label} bytes could not be authenticated") from exc
    if _sha256(raw) != ref.sha256:
        raise StudyAuthorityError(f"{label} bytes differ from their reference")
    return raw


def _one_store_ref(
    store: StudyStoreV1,
    *,
    kind: str,
    label: str,
    expected: ArtifactRefV5 | None = None,
    key: str | None = None,
    allow_empty: bool = False,
) -> ArtifactRefV5 | None:
    refs = store.list_refs(kind=kind)
    if key is not None:
        suffix = f"/{key}.bin"
        refs = tuple(item for item in refs if item.relative_path.endswith(suffix))
    if expected is not None:
        refs = tuple(item for item in refs if item == expected)
    if len(refs) == 0 and allow_empty:
        return None
    if len(refs) != 1:
        raise StudyAuthorityError(f"{label} namespace is not uniquely authenticated")
    return refs[0]


def _decode_contract(store: StudyStoreV1, ref: ArtifactRefV5, value_type: type[Any], label: str) -> Any:
    raw = _read_store(store, ref, label)
    try:
        value = value_type.from_canonical_json(raw)
    except Exception as exc:  # noqa: BLE001 - closed contract errors are authority failures here
        raise StudyAuthorityError(f"{label} is not a valid canonical contract") from exc
    if getattr(value, "canonical_bytes", lambda: b"")() != raw:
        raise StudyAuthorityError(f"{label} was not decoded from its original canonical bytes")
    return value


def _path_for(root: Path, relative: str) -> Path:
    # Only fixed, source-backed paths are accepted.  This helper intentionally
    # does not search for a matching digest or follow a relocation.
    candidate = root.joinpath(*relative.split("/"))
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise StudyAuthorityError("study authority path escapes its fixed root")
    return candidate


def _assert_ref_path(ref: ArtifactRefV5, prefix: str, label: str) -> None:
    if not ref.relative_path.startswith(prefix):
        raise StudyAuthorityError(f"{label} is outside its controller namespace")


def _load_prepared_study_v1(root: Path) -> PreparedStudyV1:
    """Reconstruct a prepared study from persisted authorities, read-only."""

    supplied = Path(root)
    if not supplied.is_absolute() or not supplied.is_dir() or supplied.is_symlink():
        raise StudyAuthorityError("study root must be an existing absolute directory")
    resolved = Path(os.path.abspath(supplied))
    children: dict[str, Path] = {}
    for name in _FIXED_CHILDREN:
        child = resolved / name
        if not child.is_dir() or child.is_symlink():
            raise StudyAuthorityError(f"study root is missing fixed child {name}")
        children[name] = child
    store_root = children["study-store"]
    store = StudyStoreV1(LocalArtifactRepositoryV5(store_root))

    manifest_ref = _one_store_ref(store, kind="manifests", label="study manifest")
    assert manifest_ref is not None
    manifest = _decode_contract(store, manifest_ref, StudyManifestV1, "study manifest")
    if manifest_ref.sha256 != manifest.sha256 or manifest_ref.relative_path != f"adapter-blobs/study-v1-manifests/{manifest.sha256}.bin":
        raise StudyAuthorityError("study manifest reference is not deterministic")

    registry_ref = _one_store_ref(store, kind="registry", label="behavior registry")
    assert registry_ref is not None
    if registry_ref.sha256 != manifest.registry_sha256:
        raise StudyAuthorityError("behavior registry does not match the manifest")
    registry = _decode_contract(store, registry_ref, FrozenBehaviorRegistryV1, "behavior registry")
    try:
        verify_registry_v1(registry)
    except Exception as exc:  # noqa: BLE001
        raise StudyAuthorityError("behavior registry failed its independent verification") from exc

    rubric_ref = _one_store_ref(store, kind="rubrics", label="frozen rubric", key="v1")
    assert rubric_ref is not None
    rubric_bytes = _read_store(store, rubric_ref, "frozen rubric")
    if _sha256(rubric_bytes) != manifest.rubric_sha256:
        raise StudyAuthorityError("frozen rubric differs from the manifest")
    for kind, digest, label in (
        ("schemas", manifest.schema_sha256, "response schema"),
        ("parsers", manifest.parser_sha256, "response parser"),
        ("prompts", manifest.prompt_sha256, "study prompt"),
    ):
        ref = _one_store_ref(store, kind=kind, label=label, key="v1")
        assert ref is not None
        if _sha256(_read_store(store, ref, label)) != digest:
            raise StudyAuthorityError(f"{label} differs from the manifest")

    primary_preflight_ref = _one_store_ref(
        store, kind="preflights", label="primary preflight", expected=manifest.primary_preflight_ref
    )
    withheld_preflight_ref = _one_store_ref(
        store, kind="preflights", label="withheld preflight", expected=manifest.withheld_preflight_ref
    )
    assert primary_preflight_ref is not None and withheld_preflight_ref is not None
    primary_preflight = _decode_contract(store, primary_preflight_ref, FixturePreflightV1, "primary preflight")
    withheld_preflight = _decode_contract(store, withheld_preflight_ref, FixturePreflightV1, "withheld preflight")
    if primary_preflight.arm != "primary" or withheld_preflight.arm != "withheld":
        raise StudyAuthorityError("stored preflights are not one primary and one withheld arm")
    if primary_preflight.mode != manifest.mode or withheld_preflight.mode != manifest.mode:
        raise StudyAuthorityError("preflight mode differs from the study manifest")
    if primary_preflight_ref.sha256 != primary_preflight.sha256 or withheld_preflight_ref.sha256 != withheld_preflight.sha256:
        raise StudyAuthorityError("preflight reference differs from its canonical bytes")
    if (
        primary_preflight_ref != manifest.primary_preflight_ref
        or withheld_preflight_ref != manifest.withheld_preflight_ref
        or primary_preflight.checkpoint_ref != manifest.round_one_checkpoint_ref
        or withheld_preflight.checkpoint_ref != manifest.round_one_checkpoint_ref
        or primary_preflight.snapshot_ref != manifest.round_one_snapshot_ref
        or withheld_preflight.snapshot_ref != manifest.round_one_snapshot_ref
    ):
        raise StudyAuthorityError("preflight historical checkpoint/archive authority differs")

    fixture_manifest_ref = ArtifactRefV5("evaluator/study-manifest.json", manifest.fixture_sha256)
    ancestor_root = children["round-one-ancestor"]
    primary_root = children["primary-descendant"]
    withheld_root = children["withheld-descendant"]
    try:
        ancestor = reopen_study_fixture_v1(root=ancestor_root, manifest_ref=fixture_manifest_ref, registry=registry)
        primary_fixture = reopen_study_fixture_v1(root=primary_root, manifest_ref=fixture_manifest_ref, registry=registry)
        withheld_fixture = reopen_study_fixture_v1(root=withheld_root, manifest_ref=fixture_manifest_ref, registry=registry)
    except Exception as exc:  # noqa: BLE001
        raise StudyAuthorityError("one of the persisted fixture roots failed manifest authentication") from exc
    fixture_roots = ((primary_preflight, primary_fixture, primary_root), (withheld_preflight, withheld_fixture, withheld_root))
    if len({ancestor.repository.root_identity_sha256, primary_fixture.repository.root_identity_sha256, withheld_fixture.repository.root_identity_sha256, store.repository.root_identity_sha256}) != 4:
        raise StudyAuthorityError("study fixture and store roots do not have distinct authenticated identities")
    if not (
        ancestor.manifest_ref == fixture_manifest_ref
        and primary_fixture.manifest_ref == fixture_manifest_ref
        and withheld_fixture.manifest_ref == fixture_manifest_ref
    ):
        raise StudyAuthorityError("fixture manifest identities differ across roots")
    for preflight, fixture, expected_root in fixture_roots:
        if preflight.fixture_root_identity_sha256 != fixture.repository.root_identity_sha256:
            raise StudyAuthorityError(f"{preflight.arm} preflight root identity differs from its fixture")
        locator = preflight.fixture_root_locator
        fixed = _long_path(expected_root)
        if os.path.normcase(locator) != os.path.normcase(fixed):
            raise StudyAuthorityError(f"{preflight.arm} preflight locator differs from its fixed descendant root")
        try:
            authenticate_fixture_preflight_v1(preflight, require_current=False)
        except Exception as exc:  # noqa: BLE001
            raise StudyAuthorityError(f"{preflight.arm} historical F/checkpoint/archive authority failed") from exc

    if manifest.fixture_sha256 != ancestor.manifest_ref.sha256 or primary_fixture.manifest_ref.sha256 != manifest.fixture_sha256 or withheld_fixture.manifest_ref.sha256 != manifest.fixture_sha256:
        raise StudyAuthorityError("fixture manifest digest is not bound to the study manifest")

    calls: dict[str, tuple[ArtifactRefV5, StudyCallRequestV1]] = {}
    for reference in store.list_refs(kind="calls"):
        call = _decode_contract(store, reference, StudyCallRequestV1, "study call")
        if reference.sha256 != call.sha256 or reference.relative_path != f"adapter-blobs/study-v1-calls/{call.sha256}.bin":
            raise StudyAuthorityError("study call reference is not deterministic")
        if call.study_id != manifest.study_id or call.arm in calls:
            raise StudyAuthorityError("study call identity is duplicated or bound to another study")
        calls[call.arm] = (reference, call)
    if set(calls) != set(_ARM_NAMES):
        raise StudyAuthorityError("study call namespace does not contain exactly both arms")
    for arm, preflight in (("primary", primary_preflight), ("withheld", withheld_preflight)):
        call = calls[arm][1]
        if (
            call.preflight_ref != (primary_preflight_ref if arm == "primary" else withheld_preflight_ref)
            or call.fixture_request_sha256 != preflight.request_sha256
            or call.study_id != manifest.study_id
        ):
            raise StudyAuthorityError("study L request does not bind its exact F preflight")
        expected_provider = "offline_fixture" if manifest.mode == "offline_fixture" else manifest.provider_settings.provider  # type: ignore[union-attr]
        expected_model = "offline_fixture/study-v1" if manifest.mode == "offline_fixture" else manifest.provider_settings.model  # type: ignore[union-attr]
        if call.provider != expected_provider or call.model != expected_model:
            raise StudyAuthorityError("study L provider/model differs from the manifest")
        if _sha256(call.schema_json) != manifest.schema_sha256:
            raise StudyAuthorityError("study L schema differs from the manifest")
        try:
            # The persisted projection is deliberately compared to the exact
            # deterministic wire projection, without replacing stored bytes.
            from core.pit_optimizer_v5.provider import wire_role_messages_v5, wire_role_schema_v5

            expected_wire_messages = canonical_json_bytes_v5(wire_role_messages_v5(call.messages))
            schema_value = json.loads(call.schema_json.decode("utf-8"))
            expected_wire_schema = canonical_json_bytes_v5(wire_role_schema_v5(schema_value))
        except Exception as exc:  # noqa: BLE001
            raise StudyAuthorityError("study L wire projection is invalid") from exc
        if call.projected_wire_messages != expected_wire_messages or call.projected_wire_schema != expected_wire_schema:
            raise StudyAuthorityError("study L wire projection differs from its stored messages/schema")

    comparison_ref = _one_store_ref(store, kind="comparisons", label="request comparison", key="v1")
    assert comparison_ref is not None
    comparison = _decode_contract(store, comparison_ref, RequestComparisonV1, "request comparison")
    if comparison_ref.sha256 != comparison.sha256:
        raise StudyAuthorityError("request comparison reference differs from its bytes")
    if (
        comparison.primary_preflight_sha256 != primary_preflight.sha256
        or comparison.withheld_preflight_sha256 != withheld_preflight.sha256
        or comparison.primary_live_call_sha256 != calls["primary"][1].sha256
        or comparison.withheld_live_call_sha256 != calls["withheld"][1].sha256
    ):
        raise StudyAuthorityError("request comparison is bound to different persisted F/L identities")
    try:
        recomputed_comparison = compare_study_requests_v1(
            primary=primary_preflight,
            withheld=withheld_preflight,
            store=store,
        )
    except Exception as exc:  # noqa: BLE001
        raise StudyAuthorityError("request comparison could not be independently recomputed") from exc
    if recomputed_comparison != comparison:
        raise StudyAuthorityError("stored request comparison differs from its recomputed gates")

    try:
        projection = _projection(ancestor.repository, ancestor)
        round_two_input = _round_two_input(ancestor)
        selected = select_parent_v5(
            state=projection.state,
            baseline=round_two_input.baseline,
            discovery_plan=round_two_input.panel_plan,
            evaluator_contract=round_two_input.evaluator_contract,
            stored_records=projection.stored_records,
        )
        selected_configuration = _configuration_for_record(registry, selected)
        if selected_configuration is None:
            matches = tuple(
                item.configuration_id
                for item in registry.configurations
                if item.policy_revision.sha256 == selected.policy_identity_sha256
            )
            selected_configuration = matches[0] if len(matches) == 1 else None
        if selected_configuration is None:
            raise StudyAuthorityError("checkpoint-selected parent is not uniquely registered")
        memory = project_investigator_memory_v5(
            stored_records=projection.stored_records,
            selected_parent_revision_sha256=selected.policy_identity_sha256,
            selected_parent_record_ref=selected.selected_parent_record_ref,
            relevant_mechanism=selected.primary_mechanism or "cross_policy",
            maximum_bytes=round_two_input.manifest.search.investigator_memory_max_bytes,
        )
        memory_ids = tuple(item.experiment_id for item in memory.summaries)
    except StudyAuthorityError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise StudyAuthorityError("checkpoint-selected parent or memory projection failed") from exc
    if selected_configuration != "S":
        raise StudyAuthorityError("actual P1 selected parent differs from the accepted study authority")

    # Reauthenticate the untouched ancestor closed graph, including long-path
    # policy/source/index/control files.  Read it twice through the
    # authenticated helper: comparing a snapshot to a digest projection made
    # from that same snapshot is tautological and cannot detect a moving
    # authority root.
    try:
        ancestor_snapshot = _authenticated_closed_snapshot(ancestor)
        ancestor_snapshot_reread = _authenticated_closed_snapshot(ancestor)
    except Exception as exc:  # noqa: BLE001
        raise StudyAuthorityError(
            f"round-one closed graph failed independent authentication: {exc}"
        ) from exc
    ancestor_inventory = tuple((path, _sha256(raw)) for path, raw in ancestor_snapshot)
    reread_inventory = tuple((path, _sha256(raw)) for path, raw in ancestor_snapshot_reread)
    if ancestor_inventory != reread_inventory:
        raise StudyAuthorityError("round-one closed graph changed between authenticated reads")
    checkpoint = ancestor.repository.load_checkpoint()
    if checkpoint is None:
        raise StudyAuthorityError("round-one ancestor has no checkpoint")
    checkpoint_ref = ArtifactRefV5("checkpoint.json", _sha256(canonical_json_bytes_v5(checkpoint.to_primitive())))
    snapshot_ref = ArtifactRefV5("archive.json", checkpoint.archive_sha256)
    if checkpoint_ref != manifest.round_one_checkpoint_ref or snapshot_ref != manifest.round_one_snapshot_ref:
        raise StudyAuthorityError("historical checkpoint/archive differs from the manifest")
    if ancestor_inventory != tuple((path, digest) for path, digest in _ancestor_inventory(ancestor_snapshot)):
        raise StudyAuthorityError("round-one closed graph inventory could not be stabilized")

    return PreparedStudyV1(
        root=resolved,
        mode=manifest.mode,
        store_root=store_root,
        ancestor_root=ancestor_root,
        primary_root=primary_root,
        withheld_root=withheld_root,
        registry=registry,
        registry_ref=registry_ref,
        rubric_ref=rubric_ref,
        round_one_checkpoint_ref=manifest.round_one_checkpoint_ref,
        round_one_snapshot_ref=manifest.round_one_snapshot_ref,
        ancestor_manifest_ref=fixture_manifest_ref,
        primary_manifest_ref=fixture_manifest_ref,
        withheld_manifest_ref=fixture_manifest_ref,
        primary_preflight=primary_preflight,
        withheld_preflight=withheld_preflight,
        primary_preflight_ref=primary_preflight_ref,
        withheld_preflight_ref=withheld_preflight_ref,
        primary_live_call=calls["primary"][1],
        withheld_live_call=calls["withheld"][1],
        primary_live_call_ref=calls["primary"][0],
        withheld_live_call_ref=calls["withheld"][0],
        manifest=manifest,
        manifest_ref=manifest_ref,
        request_comparison=comparison,
        request_comparison_ref=comparison_ref,
        selected_parent_configuration_id=selected_configuration,
        primary_memory_summary_ids=memory_ids,
        round_one_record_refs=checkpoint.record_refs,
        common_ancestor_files=tuple((path, _sha256(raw)) for path, raw in ancestor_snapshot),
    )


def _ancestor_inventory(snapshot: tuple[tuple[str, bytes], ...]) -> tuple[tuple[str, str], ...]:
    return tuple((path, _sha256(raw)) for path, raw in snapshot)


def load_prepared_study_v1(*, root: Path) -> PreparedStudyV1:
    """Public read-only loader used by the CLI and independent callers."""

    return _load_prepared_study_v1(root)


@dataclass(frozen=True, slots=True, init=False)
class HumanEvidenceReviewV1:
    """A user-supplied semantic review bound to exact generated artifacts."""

    reviewer_id: str
    rubric_sha256: str
    response_refs: tuple[ArtifactRefV5, ...]
    draft_refs: tuple[ArtifactRefV5, ...]
    evidence_interpretation: str
    revision_quality: str
    claim_pattern: str
    reasons: tuple[str, ...]
    artifact_refs: tuple[ArtifactRefV5, ...]
    draft_refs_by_arm: tuple[tuple[ArtifactRefV5, ...], ...]
    axis_reasons_by_name: tuple[tuple[str, tuple[str, ...]], ...]
    axis_citations_by_name: tuple[tuple[str, tuple[ArtifactRefV5, ...]], ...]

    def __init__(
        self,
        reviewer_id: str | None = None,
        rubric_sha256: str | None = None,
        response_refs: tuple[ArtifactRefV5, ...] = (),
        draft_refs: tuple[ArtifactRefV5, ...] = (),
        evidence_interpretation: str = "not_assessed",
        revision_quality: str = "not_assessed",
        claim_pattern: str = "not_assessed",
        reasons: tuple[str, ...] = (),
        artifact_refs: tuple[ArtifactRefV5, ...] = (),
        *,
        reviewer: str | None = None,
        arm_response_refs: Mapping[str, ArtifactRefV5] | None = None,
        arm_draft_refs: Mapping[str, tuple[ArtifactRefV5, ...]] | None = None,
        semantic_reasons: tuple[str, ...] | None = None,
        citations: tuple[ArtifactRefV5, ...] | None = None,
        axis_reasons: Mapping[str, tuple[str, ...]] | None = None,
        axis_citations: Mapping[str, tuple[ArtifactRefV5, ...]] | None = None,
    ) -> None:
        if reviewer_id is None:
            reviewer_id = reviewer
        if arm_response_refs is not None:
            if set(arm_response_refs) != set(_ARM_NAMES):
                raise StudyContractError("human arm response refs must name both study arms exactly")
            response_refs = tuple(arm_response_refs[name] for name in _ARM_NAMES)
        grouped_drafts: tuple[tuple[ArtifactRefV5, ...], ...] = ()
        if arm_draft_refs is not None:
            if set(arm_draft_refs) != set(_ARM_NAMES):
                raise StudyContractError("human arm draft refs must name both study arms exactly")
            grouped_drafts = tuple(tuple(arm_draft_refs[name]) for name in _ARM_NAMES)
            # Keep an explicitly supplied flat sequence intact.  The grouped
            # mapping is a separate ownership authority, so callers cannot
            # make a wrong per-arm partition look valid merely by having the
            # same flattened references.  The normal convenience form still
            # derives the flat sequence when it was omitted.
            if not draft_refs:
                draft_refs = tuple(ref for values in grouped_drafts for ref in values)
        if semantic_reasons is not None:
            reasons = semantic_reasons
        if citations is not None:
            artifact_refs = citations
        if type(reviewer_id) is not str or not reviewer_id.strip() or type(rubric_sha256) is not str:
            raise StudyContractError("human evidence reviewer identity is invalid")
        if len(rubric_sha256) != 64 or any(char not in "0123456789abcdef" for char in rubric_sha256):
            raise StudyContractError("human evidence rubric identity is invalid")
        for values, label in ((response_refs, "human response refs"), (draft_refs, "human draft refs"), (artifact_refs, "human citation refs")):
            if type(values) is not tuple or any(type(item) is not ArtifactRefV5 for item in values):
                raise StudyContractError(f"{label} are invalid")
        if type(grouped_drafts) is not tuple or any(
            type(values) is not tuple or any(type(item) is not ArtifactRefV5 for item in values)
            for values in grouped_drafts
        ):
            raise StudyContractError("human per-arm draft refs are invalid")
        for value, label in ((evidence_interpretation, "evidence interpretation"), (revision_quality, "revision quality"), (claim_pattern, "claim pattern")):
            if value not in {"supported", "not_supported", "inconclusive", "not_assessed"}:
                raise StudyContractError(f"human {label} is invalid")
        if type(reasons) is not tuple or any(type(item) is not str or not item.strip() for item in reasons):
            raise StudyContractError("human semantic reasons are invalid")
        if axis_reasons is None:
            axis_reasons = {}
        if axis_citations is None:
            axis_citations = {}
        if set(axis_reasons) - set(_REVIEW_AXES) or set(axis_citations) - set(_REVIEW_AXES):
            raise StudyContractError("human review axes are invalid")
        axis_reason_items: list[tuple[str, tuple[str, ...]]] = []
        axis_citation_items: list[tuple[str, tuple[ArtifactRefV5, ...]]] = []
        for axis in _REVIEW_AXES:
            axis_values = axis_reasons.get(axis, ())
            citation_values = axis_citations.get(axis, ())
            if type(axis_values) is not tuple or any(type(item) is not str or not item.strip() for item in axis_values):
                raise StudyContractError(f"human {axis} reasons are invalid")
            if type(citation_values) is not tuple or any(type(item) is not ArtifactRefV5 for item in citation_values):
                raise StudyContractError(f"human {axis} citations are invalid")
            axis_reason_items.append((axis, axis_values))
            axis_citation_items.append((axis, citation_values))
        object.__setattr__(self, "reviewer_id", reviewer_id)
        object.__setattr__(self, "rubric_sha256", rubric_sha256)
        object.__setattr__(self, "response_refs", response_refs)
        object.__setattr__(self, "draft_refs", draft_refs)
        object.__setattr__(self, "evidence_interpretation", evidence_interpretation)
        object.__setattr__(self, "revision_quality", revision_quality)
        object.__setattr__(self, "claim_pattern", claim_pattern)
        object.__setattr__(self, "reasons", reasons)
        object.__setattr__(self, "artifact_refs", artifact_refs)
        object.__setattr__(self, "draft_refs_by_arm", grouped_drafts)
        object.__setattr__(self, "axis_reasons_by_name", tuple(axis_reason_items))
        object.__setattr__(self, "axis_citations_by_name", tuple(axis_citation_items))

    @property
    def reviewer(self) -> str:
        return self.reviewer_id

    @property
    def arm_response_refs(self) -> dict[str, ArtifactRefV5]:
        return {name: ref for name, ref in zip(_ARM_NAMES, self.response_refs, strict=False)}

    @property
    def arm_draft_refs(self) -> dict[str, tuple[ArtifactRefV5, ...]]:
        return {name: values for name, values in zip(_ARM_NAMES, self.draft_refs_by_arm, strict=False)}

    @property
    def axis_reasons(self) -> dict[str, tuple[str, ...]]:
        return dict(self.axis_reasons_by_name)

    @property
    def axis_citations(self) -> dict[str, tuple[ArtifactRefV5, ...]]:
        return dict(self.axis_citations_by_name)

    @property
    def semantic_reasons(self) -> tuple[str, ...]:
        return self.reasons

    @property
    def citations(self) -> tuple[ArtifactRefV5, ...]:
        return self.artifact_refs

    def to_primitive(self) -> dict[str, object]:
        return {
            "reviewer_id": self.reviewer_id,
            "rubric_sha256": self.rubric_sha256,
            "response_refs": [_ref_primitive(item) for item in self.response_refs],
            "draft_refs": [_ref_primitive(item) for item in self.draft_refs],
            "evidence_interpretation": self.evidence_interpretation,
            "revision_quality": self.revision_quality,
            "claim_pattern": self.claim_pattern,
            "reasons": list(self.reasons),
            "artifact_refs": [_ref_primitive(item) for item in self.artifact_refs],
            "arm_response_refs": {
                name: _ref_primitive(self.arm_response_refs[name]) for name in _ARM_NAMES
            },
            "arm_draft_refs": {
                name: [_ref_primitive(item) for item in self.arm_draft_refs[name]]
                for name in _ARM_NAMES
            },
            "axis_reasons": {name: list(values) for name, values in self.axis_reasons_by_name},
            "axis_citations": {
                name: [_ref_primitive(item) for item in values]
                for name, values in self.axis_citations_by_name
            },
        }

    def canonical_bytes(self) -> bytes:
        return _json_bytes(self.to_primitive())

    @property
    def sha256(self) -> str:
        return _sha256(self.canonical_bytes())


@dataclass(frozen=True, slots=True)
class StudyArmVerificationV1:
    arm: Literal["primary", "withheld"]
    state: Literal["completed", "incomplete", "rejected", "not_started"]
    terminal_ref: ArtifactRefV5 | None
    import_ref: ArtifactRefV5 | None
    round_two_status: str
    evidence_ids: tuple[str, ...]
    production_assessment: str
    case_contrast: str
    usage: Mapping[str, object]
    artifact_refs: tuple[ArtifactRefV5, ...]
    errors: tuple[str, ...] = ()
    repository_root_identity_sha256: str = ""
    measurements: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        if self.arm not in _ARM_NAMES or self.state not in {"completed", "incomplete", "rejected", "not_started"}:
            raise StudyContractError("study arm verification identity is invalid")
        if self.terminal_ref is not None and type(self.terminal_ref) is not ArtifactRefV5:
            raise StudyContractError("study arm terminal reference is invalid")
        if self.import_ref is not None and type(self.import_ref) is not ArtifactRefV5:
            raise StudyContractError("study arm import reference is invalid")
        if type(self.evidence_ids) is not tuple or any(type(item) is not str for item in self.evidence_ids):
            raise StudyContractError("study arm evidence IDs are invalid")
        if type(self.artifact_refs) is not tuple or any(type(item) is not ArtifactRefV5 for item in self.artifact_refs):
            raise StudyContractError("study arm artifact references are invalid")
        if type(self.repository_root_identity_sha256) is not str:
            raise StudyContractError("study arm repository identity is invalid")
        if type(self.measurements) is not tuple or any(not isinstance(item, Mapping) for item in self.measurements):
            raise StudyContractError("study arm measurements are invalid")

    def to_primitive(self) -> dict[str, object]:
        return {
            "arm": self.arm,
            "state": self.state,
            "terminal_ref": None if self.terminal_ref is None else _ref_primitive(self.terminal_ref),
            "import_ref": None if self.import_ref is None else _ref_primitive(self.import_ref),
            "round_two_status": self.round_two_status,
            "evidence_ids": list(self.evidence_ids),
            "production_assessment": self.production_assessment,
            "case_contrast": self.case_contrast,
            "usage": canonical_primitive_v5(dict(self.usage)),
            "artifact_refs": [_ref_primitive(item) for item in self.artifact_refs],
            "errors": list(self.errors),
            "repository_root_identity_sha256": self.repository_root_identity_sha256,
            "measurements": [canonical_primitive_v5(dict(item)) for item in self.measurements],
        }


@dataclass(frozen=True, slots=True)
class StudyVerificationV1:
    """Independent gate measurements and separate scientific claim axes."""

    schema_version: Literal[1]
    manifest_ref: ArtifactRefV5
    verdicts: StudyVerdictsV1
    arms: tuple[StudyArmVerificationV1, ...]
    measurements: tuple[Mapping[str, object], ...]
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    confounds: tuple[str, ...]
    artifact_refs: tuple[ArtifactRefV5, ...]
    human_review: HumanEvidenceReviewV1 | None = None

    @property
    def gate_measurements(self) -> tuple[Mapping[str, object], ...]:
        return self.measurements

    @property
    def exact_artifact_refs(self) -> tuple[ArtifactRefV5, ...]:
        return self.artifact_refs

    def to_primitive(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "manifest_ref": _ref_primitive(self.manifest_ref),
            "verdicts": self.verdicts.to_primitive(),
            "arms": [item.to_primitive() for item in self.arms],
            "measurements": [canonical_primitive_v5(dict(item)) for item in self.measurements],
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "confounds": list(self.confounds),
            "artifact_refs": [_ref_primitive(item) for item in self.artifact_refs],
            "human_review": None if self.human_review is None else self.human_review.to_primitive(),
        }

    def canonical_bytes(self) -> bytes:
        return _json_bytes(self.to_primitive())

    @property
    def sha256(self) -> str:
        return _sha256(self.canonical_bytes())


def _add_ref(result: dict[tuple[str, str], ArtifactRefV5], reference: ArtifactRefV5) -> None:
    result.setdefault((reference.relative_path, reference.sha256), reference)


def _collect_refs(value: object, result: dict[tuple[str, str], ArtifactRefV5]) -> None:
    if isinstance(value, ArtifactRefV5):
        _add_ref(result, value)
        return
    if is_dataclass(value):
        for item in fields(value):
            _collect_refs(getattr(value, item.name), result)
        return
    if isinstance(value, (tuple, list)):
        for item in value:
            _collect_refs(item, result)
        return
    if isinstance(value, Mapping):
        for item in value.values():
            _collect_refs(item, result)


def _authenticate_round_two_precommitment(
    *,
    fixture: object,
    mechanism: MechanismArtifactRepositoryV5,
    refs: dict[tuple[str, str], ArtifactRefV5],
    arm: str,
) -> bool:
    """Authenticate a round-two precommitment even before evidence exists."""

    repository = fixture.repository
    campaign_id = fixture.manifest.manifest.campaign_id
    round_key = mechanism._round_key(campaign_id, 2)
    try:
        index = mechanism._load_precommitment_index_by_identity(
            campaign_id=campaign_id,
            round_index=2,
        )
    except Exception as exc:  # noqa: BLE001 - a value without its authority is an orphan
        raise StudyAuthorityError(f"{arm} round-two precommitment value/index authority is unavailable") from exc
    authority = repository._load_adapter_state_authority(
        namespace="mechanism-v5",
        key=round_key,
    )
    if authority is None:
        if index is not None:
            raise StudyAuthorityError(f"{arm} round-two precommitment authority index is missing")
        return False
    if index is None:
        raise StudyAuthorityError(f"{arm} mechanism precommitment value is missing")
    try:
        mechanism._validate_precommitment_index_identity(
            campaign_id=campaign_id,
            round_index=2,
            index=index,
        )
        spec, corpus = mechanism._load_precommitment_payloads_by_identity(
            campaign_id=campaign_id,
            round_index=2,
            index=index,
        )
        parent_revision = repository.load_typed_artifact(
            index.parent_revision_ref,
            value_type=PolicyRevisionIdentityV5,
        )
        parent_source = repository.load_typed_artifact(
            index.parent_source_bundle_ref,
            value_type=SourceBundleV5,
        )
    except StudyAuthorityError:
        raise
    except Exception as exc:  # noqa: BLE001 - map every closure failure to study authority
        raise StudyAuthorityError(f"{arm} round-two precommitment authority is unavailable") from exc
    if (
        authority[0].value_ref.relative_path != f"adapter-state/mechanism-v5/{round_key}.json"
        or authority[0].value_ref.sha256 != _sha256(canonical_json_bytes_v5(index))
        or index.campaign_id != campaign_id
        or index.round_index != 2
        or index.manifest_ref != fixture.manifest_ref
        or index.manifest_sha256 != fixture.manifest_ref.sha256
        or index.manifest_source_identity_sha256 != manifest_source_identity_sha256_v1(fixture.manifest)
        or parent_revision.sha256 != index.parent_revision_sha256
        or parent_source.sha256 != index.parent_source_bundle_sha256
        or spec.sha256 != index.spec_sha256
        or corpus.sha256 != index.corpus_sha256
    ):
        raise StudyAuthorityError(f"{arm} round-two precommitment is not bound to its fixture graph")
    _add_ref(refs, authority[0].value_ref)
    _add_ref(refs, authority[1])
    _collect_refs(index, refs)
    return True


def _registered_configuration_for_source(
    *,
    registry: FrozenBehaviorRegistryV1,
    source_bundle: SourceBundleV5,
    policy_revision: PolicyRevisionIdentityV5,
    label: str,
) -> str:
    """Resolve one frozen registry configuration from exact source identities."""

    matches = tuple(
        configuration
        for configuration in registry.configurations
        if configuration.source_bundle == source_bundle
        and configuration.policy_revision == policy_revision
    )
    if len(matches) != 1:
        raise StudyAuthorityError(f"{label} does not resolve to one frozen registry configuration")
    return matches[0].configuration_id


def _recompute_authenticated_mechanism_report(
    *,
    fixture,
    registry: FrozenBehaviorRegistryV1,
    mechanism: MechanismArtifactRepositoryV5,
    record: StoredExperimentRecordV5,
    evidence: object,
    candidate_source_bundle: SourceBundleV5,
    arm: str,
) -> object:
    """Re-evaluate every persisted observation and reduce it independently.

    The mechanism reader authenticates sidecar bytes and typed identities, but
    it intentionally does not know the study's frozen behavior registry.  The
    restart verifier supplies that missing semantic edge: it resolves the
    exact parent/candidate source+revision pair, evaluates every case in every
    repetition through the pure registry decision function, and only then
    trusts the pure report reducer.  No worker or candidate source execution
    is entered here.
    """

    index = mechanism._load_precommitment_index_by_identity(
        campaign_id=fixture.manifest.manifest.campaign_id,
        round_index=record.record.round_index,
    )
    if index is None:
        raise StudyAuthorityError(f"{arm} mechanism precommitment index is unavailable")
    try:
        parent_revision = fixture.repository.load_typed_artifact(
            index.parent_revision_ref,
            value_type=PolicyRevisionIdentityV5,
        )
        parent_source_bundle = fixture.repository.load_typed_artifact(
            index.parent_source_bundle_ref,
            value_type=SourceBundleV5,
        )
    except Exception as exc:  # noqa: BLE001 - convert exact authority failures
        raise StudyAuthorityError(f"{arm} mechanism parent source authority is unavailable") from exc
    candidate_revision = record.record.policy_revision
    if candidate_revision is None:
        raise StudyAuthorityError(f"{arm} mechanism candidate revision authority is unavailable")
    parent_configuration_id = _registered_configuration_for_source(
        registry=registry,
        source_bundle=parent_source_bundle,
        policy_revision=parent_revision,
        label=f"{arm} mechanism parent",
    )
    candidate_configuration_id = _registered_configuration_for_source(
        registry=registry,
        source_bundle=candidate_source_bundle,
        policy_revision=candidate_revision,
        label=f"{arm} mechanism candidate",
    )
    run = evidence.run
    spec = evidence.spec
    for observation in run.observations:
        try:
            snapshot = ExitSnapshotV3.from_canonical_json(observation.case_snapshot_json.decode("utf-8"))
            parent_decision = registry_decision_v1(
                registry=registry,
                configuration_id=parent_configuration_id,
                method=spec.target_method,
                snapshot=snapshot,
            )
            candidate_decision = registry_decision_v1(
                registry=registry,
                configuration_id=candidate_configuration_id,
                method=spec.target_method,
                snapshot=snapshot,
            )
            parent_json = parent_decision.to_canonical_json().encode("utf-8")
            candidate_json = candidate_decision.to_canonical_json().encode("utf-8")
        except Exception as exc:  # noqa: BLE001 - registry mismatch is study authority failure
            raise StudyAuthorityError(
                f"{arm} persisted mechanism observation could not be recomputed from the frozen registry"
            ) from exc
        if parent_json != observation.parent_decision_json:
            raise StudyAuthorityError(
                f"{arm} persisted parent decision differs from the frozen registry observation"
            )
        if candidate_json != observation.candidate_decision_json:
            raise StudyAuthorityError(
                f"{arm} persisted candidate decision differs from the frozen registry observation"
            )
    try:
        recomputed = build_mechanism_evidence_report_v1(
            spec,
            evidence.binding,
            run,
        )
    except Exception as exc:  # noqa: BLE001 - reducer mismatch is study authority failure
        raise StudyAuthorityError(f"{arm} mechanism report could not be recomputed") from exc
    if recomputed.to_canonical_json().encode("utf-8") != evidence.report.to_canonical_json().encode("utf-8"):
        raise StudyAuthorityError(
            f"{arm} persisted mechanism report differs from the recomputed registry observation reduction"
        )
    return recomputed


def _verify_fixture_arm(
    *,
    prepared: PreparedStudyV1,
    arm: Literal["primary", "withheld"],
    ledger: StudyLedgerV1 | None,
    reservation_projection: tuple[tuple[StudyReservationV1, ArtifactRefV5 | None, ArtifactRefV5 | None], ...],
) -> tuple[StudyArmVerificationV1, tuple[ArtifactRefV5, ...], RoleInvocationPackageV5 | None]:
    fixture = reopen_study_fixture_v1(
        root=prepared.fixture_path(arm),
        manifest_ref=prepared.primary_manifest_ref if arm == "primary" else prepared.withheld_manifest_ref,
        registry=prepared.registry,
    )
    graph = fixture.repository.verify_graph(fixture.manifest_ref)
    if graph.failure is not None:
        raise StudyAuthorityError(f"{arm} fixture graph has an authenticated failure")
    # The descendant checkpoint is current round-two state.  It is verified
    # independently from the historical preflight checkpoint above.
    try:
        LocalArchiveReducerFactoryV5(fixture.repository).verify_projection(
            manifest=fixture.manifest.manifest,
            panel_plan=fixture.manifest.panel_plan,
            evaluator_contract=fixture.manifest.evaluator_contract,
            repair=False,
        )
    except Exception as exc:  # noqa: BLE001
        raise StudyAuthorityError(f"{arm} current descendant projection is not authenticated") from exc

    refs: dict[tuple[str, str], ArtifactRefV5] = {}
    _collect_refs(fixture.manifest, refs)
    try:
        scripted_ref = verify_scripted_role_authority_v1(
            repository=fixture.repository,
            manifest=fixture.manifest.manifest,
            registry=fixture.registry,
            round_index=1,
        )
    except Exception as exc:  # noqa: BLE001 - convert source-pin failures to study authority
        raise StudyAuthorityError(f"{arm} scripted role authority is unavailable") from exc
    _add_ref(refs, scripted_ref)
    scripted_index = fixture.repository._load_adapter_state_authority(
        namespace="study-scripted-role",
        key=f"{fixture.manifest.manifest.campaign_id}-1",
    )
    if scripted_index is None:
        raise StudyAuthorityError(f"{arm} scripted role authority index is missing")
    _add_ref(refs, scripted_index[1])
    mechanism = MechanismArtifactRepositoryV5(fixture.repository)
    round_two_precommitment_present = _authenticate_round_two_precommitment(
        fixture=fixture,
        mechanism=mechanism,
        refs=refs,
        arm=arm,
    )
    measurements: list[Mapping[str, object]] = []
    errors: list[str] = []
    persisted_evidence: list[tuple[StoredExperimentRecordV5, object, object, object]] = []
    checkpoint = fixture.repository.load_checkpoint()
    if checkpoint is not None:
        _collect_refs(checkpoint, refs)
        # ``RepositoryCheckpointV5`` stores the archive digest and record
        # edges, not its own envelope reference.  Retain both exact current
        # controller envelopes so export cannot silently omit the closure
        # authority that authenticated this projection.
        checkpoint_ref = ArtifactRefV5(
            "checkpoint.json",
            _sha256(canonical_json_bytes_v5(checkpoint.to_primitive())),
        )
        archive_ref = ArtifactRefV5("archive.json", checkpoint.archive_sha256)
        try:
            if fixture.repository.authenticate_exact(checkpoint_ref).content != canonical_json_bytes_v5(checkpoint.to_primitive()):
                raise StudyAuthorityError(f"{arm} checkpoint bytes differ from its authenticated value")
            fixture.repository.authenticate_exact(archive_ref)
        except Exception as exc:  # noqa: BLE001 - preserve the exact control edge
            raise StudyAuthorityError(f"{arm} current checkpoint/archive authority is unavailable") from exc
        _add_ref(refs, checkpoint_ref)
        _add_ref(refs, archive_ref)
    events = fixture.repository.load_round_events(campaign_id=fixture.manifest.manifest.campaign_id, round_index=2)
    if events:
        try:
            round_two_scripted_ref = verify_scripted_role_authority_v1(
                repository=fixture.repository,
                manifest=fixture.manifest.manifest,
                registry=fixture.registry,
                round_index=2,
            )
        except Exception as exc:  # noqa: BLE001 - source pin is phase-gated by round-two history
            raise StudyAuthorityError(f"{arm} round-two scripted role authority is unavailable") from exc
        _add_ref(refs, round_two_scripted_ref)
        round_two_index = fixture.repository._load_adapter_state_authority(
            namespace="study-scripted-role",
            key=f"{fixture.manifest.manifest.campaign_id}-2",
        )
        if round_two_index is None:
            raise StudyAuthorityError(f"{arm} round-two scripted role authority index is missing")
        _add_ref(refs, round_two_index[1])
    packages: dict[str, RoleInvocationPackageV5] = {}
    completion_payloads: dict[str, RoleCompletionPayloadV5] = {}
    role_failures: list[str] = []
    for event in events:
        event_ref = ArtifactRefV5(
            f"events/{event.campaign_id}/{event.round_index:04d}/{event.sequence:06d}.json",
            event.sha256,
        )
        try:
            if fixture.repository.authenticate_exact(event_ref).content != canonical_json_bytes_v5(event.to_primitive()):
                raise StudyAuthorityError(f"{arm} journal event bytes differ from their authenticated value")
        except Exception as exc:  # noqa: BLE001 - event envelope is part of the closure
            raise StudyAuthorityError(f"{arm} journal event authority is unavailable") from exc
        _add_ref(refs, event_ref)
        _collect_refs(event, refs)
        payload = fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
        _collect_refs(payload, refs)
        if type(payload) is RoleCompletionPayloadV5:
            package = fixture.repository.load_role_invocation(payload)
            packages[payload.role] = package
            completion_payloads[payload.role] = payload
            if payload.outcome != "accepted" or package.attempt.outcome != "accepted":
                role_failures.append(f"{payload.role}:{payload.outcome}")
            _collect_refs(package, refs)
            request_call, request_value = fixture.repository.load_unique_role_request_entry_by_sha256(
                package.request.sha256
            )
            request_raw = canonical_json_bytes_v5(
                role_request_artifact_primitive_v5(call=request_call, request=request_value)
            )
            request_ref = ArtifactRefV5(f"roles/requests/{request_call.sha256}.json", _sha256(request_raw))
            # Authenticate the exact public request path; package.request is
            # a decoded value and never substitutes for these bytes.
            authenticated_request = fixture.repository.authenticate_exact(request_ref)
            _add_ref(refs, authenticated_request.reference)

    # Include source/index sidecars explicitly through their typed APIs.  A
    # mechanism sidecar that was changed or removed therefore fails before a
    # verdict can be emitted.
    if checkpoint is not None:
        source_identity = manifest_source_identity_sha256_v1(fixture.manifest)
        for record_ref in checkpoint.record_refs:
            record = fixture.repository.load_experiment(record_ref)
            stored = StoredExperimentRecordV5(record_ref, record)
            if record.policy_revision is not None:
                source = fixture.repository.load_typed_state(
                    namespace="policy-source",
                    key=record.policy_revision.sha256,
                    value_type=SourceBundleV5,
                    repair=False,
                )
                if source is None:
                    raise StudyAuthorityError("policy source authority is unavailable")
                source_ref = ArtifactRefV5(
                    f"adapter-state/policy-source/{record.policy_revision.sha256}.json",
                    _sha256(canonical_json_bytes_v5(source)),
                )
                source_authority = fixture.repository._load_adapter_state_authority(
                    namespace="policy-source", key=record.policy_revision.sha256
                )
                if source_authority is None or source_authority[0].value_ref != source_ref:
                    raise StudyAuthorityError("policy source authority index differs from its value")
                _add_ref(refs, source_ref)
                _add_ref(refs, source_authority[1])
            try:
                persisted = mechanism.load_existing_evidence_for_record(
                    campaign_id=fixture.manifest.manifest.campaign_id,
                    round_index=record.round_index,
                    manifest_ref=fixture.manifest_ref,
                    manifest_source_identity_sha256=source_identity,
                    stored_record=stored,
                )
            except Exception as exc:  # noqa: BLE001 - convert mechanism authority failures at the study boundary
                raise StudyAuthorityError(f"{arm} persisted mechanism evidence could not be authenticated") from exc
            if persisted is not None:
                evidence, intent = persisted
                if record.policy_revision is None:
                    raise StudyAuthorityError(f"{arm} mechanism candidate revision authority is unavailable")
                precommitment_key = mechanism._round_key(
                    fixture.manifest.manifest.campaign_id,
                    record.round_index,
                )
                precommitment_authority = fixture.repository._load_adapter_state_authority(
                    namespace="mechanism-v5",
                    key=precommitment_key,
                )
                if precommitment_authority is None:
                    raise StudyAuthorityError(f"{arm} mechanism precommitment authority index is missing")
                precommitment_index = mechanism._load_precommitment_index_by_identity(
                    campaign_id=fixture.manifest.manifest.campaign_id,
                    round_index=record.round_index,
                )
                if precommitment_index is None:
                    raise StudyAuthorityError(f"{arm} mechanism precommitment value is missing")
                _add_ref(refs, precommitment_authority[0].value_ref)
                _add_ref(refs, precommitment_authority[1])
                for reference in (
                    precommitment_index.parent_revision_ref,
                    precommitment_index.parent_source_bundle_ref,
                    precommitment_index.spec_ref,
                    precommitment_index.corpus_ref,
                ):
                    _add_ref(refs, reference)
                for reference in (
                    evidence.index.binding_ref,
                    evidence.index.run_ref,
                    evidence.index.report_ref,
                ):
                    if reference is not None:
                        _add_ref(refs, reference)
                recomputed_report = _recompute_authenticated_mechanism_report(
                    fixture=fixture,
                    registry=fixture.registry,
                    mechanism=mechanism,
                    record=stored,
                    evidence=evidence,
                    candidate_source_bundle=source,
                    arm=arm,
                )
                persisted_evidence.append((stored, evidence, intent, recomputed_report))
                _collect_refs(evidence, refs)
                _collect_refs(intent, refs)
                for phase in ("binding", "run", "report", "complete"):
                    key = f"{record.experiment_id}-{phase}"
                    index_item = fixture.repository._load_adapter_state_authority(namespace="mechanism-v5", key=key)
                    if index_item is None:
                        raise StudyAuthorityError(
                            f"{arm} mechanism {phase} authority index is missing for an authenticated evidence bundle"
                        )
                    _add_ref(refs, index_item[0].value_ref)
                    _add_ref(refs, index_item[1])
                    # The typed index is itself a persisted authority edge;
                    # load it without repair and retain its exact value ref.
                    from core.pit_optimizer_v5.mechanism_artifacts import MechanismExperimentIndexV1

                    typed_index = fixture.repository.load_typed_state(
                        namespace="mechanism-v5",
                        key=key,
                        value_type=MechanismExperimentIndexV1,
                        repair=False,
                    )
                    if typed_index is None or typed_index.experiment_id != record.experiment_id:
                        raise StudyAuthorityError(
                            f"{arm} mechanism {phase} typed authority differs from its index"
                        )
                    _add_ref(refs, typed_index.binding_ref)
                    if typed_index.run_ref is not None:
                        _add_ref(refs, typed_index.run_ref)
                    if typed_index.report_ref is not None:
                        _add_ref(refs, typed_index.report_ref)

                for prediction in recomputed_report.predictions:
                    measurements.append(
                        {
                            "arm": arm,
                            "experiment_id": record.experiment_id,
                            "round_index": record.round_index,
                            "metric_id": prediction.metric_id,
                            "direction": prediction.direction,
                            "denominator_kind": prediction.denominator_kind,
                            "denominator": prediction.denominator,
                            "parent_value": None if prediction.parent_value is None else str(prediction.parent_value),
                            "candidate_value": None if prediction.candidate_value is None else str(prediction.candidate_value),
                            "paired_delta": None if prediction.paired_delta is None else str(prediction.paired_delta),
                            "assessment": prediction.assessment,
                            "availability": prediction.availability,
                            "unavailable_reason": prediction.unavailable_reason,
                            "evidence_origin": prediction.evidence_origin,
                        }
                    )
    request_package = packages.get("investigator")
    if request_package is not None:
        # The actual import chain is authenticated below when a persisted
        # ledger/import exists.  This package is still retained for evidence
        # delivery measurements even when a round terminated later.
        evidence_ids = tuple(item.evidence_id for item in request_package.request.role_evidence.items)
    else:
        evidence_ids = ()

    store = StudyStoreV1(prepared.store_repository())
    import_pairs: list[tuple[ArtifactRefV5, StudyImportV1]] = []
    for import_ref in store.list_refs(kind="imports"):
        value = _decode_contract(store, import_ref, StudyImportV1, "study import")
        if value.arm == arm:
            import_pairs.append((import_ref, value))
    if len(import_pairs) > 1:
        raise StudyAuthorityError(f"{arm} has duplicate imported responses")
    if ledger is None:
        arm_reservations: list[tuple[StudyReservationV1, ArtifactRefV5 | None, ArtifactRefV5 | None]] = []
        for reservation_ref in store.list_refs(kind="reservations"):
            reservation = _decode_contract(store, reservation_ref, StudyReservationV1, "study reservation")
            if reservation.arm == arm:
                arm_reservations.append((reservation, reservation_ref, None))
    else:
        arm_reservations = [item for item in reservation_projection if item[0].arm == arm]
    if len(arm_reservations) > 1:
        raise StudyAuthorityError(f"{arm} has duplicate reservations")
    terminal_pairs: list[tuple[ArtifactRefV5, StudyCallTerminalV1]] = []
    for terminal_ref in store.list_refs(kind="terminals"):
        terminal = _decode_contract(store, terminal_ref, StudyCallTerminalV1, "study terminal")
        if terminal.arm == arm:
            terminal_pairs.append((terminal_ref, terminal))
    if len(terminal_pairs) > 1:
        raise StudyAuthorityError(f"{arm} has duplicate terminal records")
    rejection_pairs: list[tuple[ArtifactRefV5, StudyAdmissionRejectionV1]] = []
    for rejection_ref in store.list_refs(kind="admission-rejections"):
        rejection = _decode_contract(store, rejection_ref, StudyAdmissionRejectionV1, "admission rejection")
        if rejection.arm == arm:
            rejection_pairs.append((rejection_ref, rejection))
    if len(rejection_pairs) > 1:
        raise StudyAuthorityError(f"{arm} has duplicate admission rejections")
    commitment_path = (
        f"adapter-blobs/study-v1-commitments/"
        f"{_commitment_slot_key(arm=arm, campaign_id=fixture.manifest.manifest.campaign_id, round_index=2)}.bin"
    )
    commitment_refs = tuple(
        ref for ref in store.list_refs(kind="commitments") if ref.relative_path == commitment_path
    )
    if len(commitment_refs) > 1:
        raise StudyAuthorityError(f"{arm} commitment index is duplicated")
    round_two_checkpoint_present = checkpoint is not None and any(
        record_ref in checkpoint.record_refs
        and fixture.repository.load_experiment(record_ref).round_index == 2
        for record_ref in checkpoint.record_refs
    )
    if request_package is None:
        later_roles = set(completion_payloads).intersection({"author", "critic"})
        later_events = any(
            event.event_kind
            in {
                "rendered_variant",
                "resource_lease",
                "candidate_stage_result",
                "quick_evidence",
                "episode_evidence",
            }
            for event in events
        )
        round_two_persisted_evidence = any(
            item[0].record.round_index == 2 for item in persisted_evidence
        )
        if (
            later_roles
            or later_events
            or round_two_persisted_evidence
            or round_two_checkpoint_present
            or commitment_refs
        ):
            raise StudyAuthorityError(
                f"{arm} round-two graph has later phases without an authenticated investigator package"
            )
    contrast_result: CaseContrastResultV1 | None = None
    if ledger is not None:
        if terminal_pairs:
            authenticated_terminal = ledger.verify_terminal(terminal_pairs[0][0])
            if (
                authenticated_terminal.reference != terminal_pairs[0][0]
                or authenticated_terminal.terminal != terminal_pairs[0][1]
            ):
                raise StudyAuthorityError(f"{arm} terminal value differs from its locally decoded authority")
            terminal_ref = authenticated_terminal.reference
        else:
            terminal_ref = None
        if import_pairs:
            import_ref, imported = import_pairs[0]
            preflight = prepared.preflight_for(arm)
            request = authenticate_fixture_preflight_v1(preflight, require_current=False)
            authenticate_study_import_v1(
                store=store,
                ledger=ledger,
                record=imported,
                fixture_repository=fixture.repository,
                request=request,
                call=preflight.call,
            )
            if request_package is None:
                errors.append(f"{arm} imported response has no journaled investigator package")
            else:
                verify_imported_package_v1(
                    package=request_package,
                    record=imported,
                    store=store,
                    ledger=ledger,
                    fixture_repository=fixture.repository,
                )
            if events and request_package is not None:
                # The runtime's existing-round reader authenticates the exact
                # round-two F/intent/P1/spec/corpus/link graph and reruns the
                # deterministic author/critic response authority.  It only
                # reads typed state with repair disabled; no composition,
                # persistence, recovery, or resume path is entered here.
                try:
                    scripted = StudyScriptedRoleInvokerV1(
                        fixture.manifest.manifest,
                        fixture.registry,
                        2,
                        fixture.repository,
                    )
                    composer = StudyMechanismComposerV1(
                        fixture=fixture,
                        inputs=_round_two_input(fixture),
                        arm=arm,
                        store=store,
                        ledger=ledger,
                        imported=imported,
                        scripted=scripted,
                    )
                    composer.authenticate_existing_round()
                except Exception as exc:  # noqa: BLE001 - convert any graph mismatch to authority failure
                    raise StudyAuthorityError(f"{arm} round-two persisted graph or scripted authority failed") from exc
        else:
            import_ref = None

        # A completed round-two graph owns one stable commitment index in the
        # shared study store.  Reopen its immutable contrast and recompute the
        # ordered case result from the authenticated mechanism run; the result
        # is never taken from a serialized verdict or arm summary.
        if commitment_refs:
            commitment = _decode_contract(store, commitment_refs[0], StudyCommitmentIndexV1, f"{arm} commitment index")
            if commitment.storage_ref != commitment_refs[0]:
                raise StudyAuthorityError(f"{arm} commitment index storage identity differs")
            if commitment.arm != arm or not import_pairs or commitment.import_ref != import_pairs[0][0]:
                raise StudyAuthorityError(f"{arm} commitment index is not bound to its import")
            contrast_ref = commitment.contrast_ref
            contrast = _decode_contract(store, contrast_ref, StudyContrastV1, f"{arm} frozen contrast")
            if contrast.storage_ref != contrast_ref or contrast.sha256 != contrast_ref.sha256:
                raise StudyAuthorityError(f"{arm} frozen contrast identity differs")
            round_two_evidence = [item for item in persisted_evidence if item[0].record.round_index == 2]
            if len(round_two_evidence) > 1:
                raise StudyAuthorityError(f"{arm} has duplicate round-two mechanism evidence")
            if round_two_evidence:
                authenticated_record, authenticated_evidence, _intent, _report = round_two_evidence[0]
                if authenticated_record.record.policy_revision is None:
                    raise StudyAuthorityError(f"{arm} commitment has no authenticated candidate revision")
                if (
                    commitment.parent_revision_sha256 != authenticated_record.record.parent_revision_sha256
                    or commitment.spec_ref.sha256 != authenticated_evidence.index.spec_sha256
                    or commitment.corpus_ref.sha256 != authenticated_evidence.index.corpus_sha256
                ):
                    raise StudyAuthorityError(
                        f"{arm} stable commitment is not bound to its authenticated P1/mechanism graph"
                    )
                contrast_result = evaluate_case_contrast_v1(
                    contrast=contrast,
                    run=round_two_evidence[0][1].run,
                )
                measurements.append(
                    {
                        "arm": arm,
                        "experiment_id": round_two_evidence[0][0].record.experiment_id,
                        "round_index": 2,
                        "contrast_status": contrast_result.status,
                        "observed_changed": None
                        if contrast_result.observed_changed is None
                        else list(contrast_result.observed_changed),
                        "mismatched_case_ids": list(contrast_result.mismatched_case_ids),
                        "missing_case_ids": list(contrast_result.missing_case_ids),
                        "limitations": list(contrast_result.limitations),
                        "contract_ref": contrast_result.contract_ref,
                        "run_ref": contrast_result.run_ref,
                    }
                )
            elif import_pairs and terminal_pairs and terminal_pairs[0][1].failure_code is None:
                # A durable commitment can be published before the first
                # candidate observation.  It is an authenticated interruption
                # phase, not a completed experiment and not a reason to
                # manufacture a zero-evidence success.
                errors.append(f"{arm} round-two commitment is present before mechanism evidence publication")
    else:
        if terminal_pairs or import_pairs or rejection_pairs or store.list_refs(kind="reservations"):
            raise StudyAuthorityError("study accounting records exist without an authenticated grant")
        terminal_ref = None
        import_ref = None

    if rejection_pairs and (terminal_pairs or import_pairs):
        raise StudyAuthorityError(f"{arm} admission rejection conflicts with a terminal/import")
    state: Literal["completed", "incomplete", "rejected", "not_started"]
    required_roles = {"investigator", "author", "critic"}
    round_two_roles_complete = (
        set(completion_payloads) == required_roles
        and all(payload.outcome == "accepted" for payload in completion_payloads.values())
        and all(package.attempt.outcome == "accepted" for package in packages.values())
    )
    round_two_records = tuple(item for item in persisted_evidence if item[0].record.round_index == 2)
    round_two_checkpoint = round_two_checkpoint_present
    if rejection_pairs:
        state = "rejected"
    elif (
        terminal_pairs
        and terminal_pairs[0][1].failure_code is None
        and import_pairs
        and request_package is not None
        and round_two_roles_complete
        and round_two_records
        and round_two_checkpoint
        and contrast_result is not None
    ):
        state = "completed"
    elif (
        terminal_pairs
        or import_pairs
        or arm_reservations
        or events
        or commitment_refs
        or round_two_precommitment_present
    ):
        state = "incomplete"
    else:
        state = "not_started"

    round_two_status = "started" if events else "not_started"
    for payload in (fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind) for event in events):
        # RoundOutcomePayloadV5 and RoleCompletionPayloadV5 expose the typed
        # terminal state as ``outcome``.  ``status`` belongs to unrelated
        # legacy payloads and silently converted an accepted/failed round to
        # ``not_started`` in the old verifier.
        outcome = getattr(payload, "outcome", None)
        if outcome is not None:
            round_two_status = str(outcome)
        elif hasattr(payload, "status"):
            round_two_status = str(payload.status)
    if checkpoint is not None and state == "completed":
        round_two_status = "completed"
    elif round_two_checkpoint_present and round_two_status == "not_started":
        round_two_status = "checkpointed"
    elif round_two_precommitment_present and round_two_status == "not_started":
        round_two_status = "precommitment"
    prediction_assessments = {
        row["assessment"]
        for row in measurements
        if row.get("round_index") == 2 and "metric_id" in row
    }
    prediction_rows = [
        row
        for row in measurements
        if row.get("round_index") == 2 and "metric_id" in row
    ]
    has_contradiction = "contradicted_on_cases" in prediction_assessments
    has_unavailable = any(
        row.get("availability") != "measured"
        or row.get("assessment") in {"insufficient_evidence", "unavailable"}
        for row in prediction_rows
    )
    if not round_two_records or not prediction_rows:
        production_assessment = "insufficient_evidence"
    elif has_contradiction:
        production_assessment = "contradicted_on_cases"
    elif not has_unavailable and all(item == "supported_on_cases" for item in prediction_assessments):
        production_assessment = "supported_on_cases"
    else:
        production_assessment = "insufficient_evidence"
    usage: dict[str, object] = {
        "accounting_mode": "offline_synthetic" if prepared.mode == "offline_fixture" else "live_recorded",
        "provider_spend_claim": prepared.mode == "live_study",
        "role_attempts": {
            role: canonical_primitive_v5(package.attempt.usage)
            for role, package in sorted(packages.items())
        },
    }
    if terminal_pairs:
        terminal = terminal_pairs[0][1]
        usage["terminal_usage"] = canonical_primitive_v5(terminal.usage)
        usage["terminal_receipt"] = canonical_primitive_v5(terminal.receipt)
        usage["terminal_failure_code"] = None if terminal.failure_code is None else terminal.failure_code.value
        usage["terminal_failure_reason"] = terminal.failure_reason
    active_reservation_entries = tuple(
        (reservation, reservation_ref, publication_ref)
        for reservation, reservation_ref, publication_ref in arm_reservations
        if not any(terminal.reservation_sha256 == reservation.reservation_sha256 for _ref, terminal in terminal_pairs)
        and not any(rejection.reservation_sha256 == reservation.reservation_sha256 for _ref, rejection in rejection_pairs)
    )
    active_reservations = tuple(reservation for reservation, _reservation_ref, _publication_ref in active_reservation_entries)
    usage["pending_reservations"] = [canonical_primitive_v5(item) for item in active_reservations]
    usage["pending_prospective_totals"] = {
        "input_tokens": sum(item.prospective_input_tokens for item in active_reservations),
        "output_tokens": sum(item.prospective_output_tokens for item in active_reservations),
        "cost_usd": canonical_primitive_v5(sum((item.prospective_cost_usd for item in active_reservations), start=0)),
    }
    pending_provenance = [
        {
            "reservation_sha256": reservation.reservation_sha256,
            "authority": f"study-store:{store.repository.root_identity_sha256}",
            "reservation_ref": None if reservation_ref is None else reservation_ref.to_primitive(),
            "publication_ref": publication_ref.to_primitive(),
        }
        for reservation, reservation_ref, publication_ref in active_reservation_entries
        if publication_ref is not None
    ]
    if pending_provenance:
        usage["pending_reservation_provenance"] = pending_provenance
    usage["recovery_record_count"] = sum(1 for event in events if "recovery" in event.event_kind)
    return (
        StudyArmVerificationV1(
            arm=arm,
            state=state,
            terminal_ref=terminal_ref,
            import_ref=import_ref,
            round_two_status=round_two_status,
            evidence_ids=evidence_ids,
            production_assessment=production_assessment,
            case_contrast="unavailable" if contrast_result is None else contrast_result.status,
            usage=usage,
            artifact_refs=tuple(refs.values()),
            errors=tuple(errors) + tuple(f"round-two role failure: {item}" for item in role_failures),
            repository_root_identity_sha256=fixture.repository.root_identity_sha256,
            measurements=tuple(measurements),
        ),
        tuple(refs.values()),
        request_package,
    )


def _reason(gate: str, text: str, refs: tuple[ArtifactRefV5, ...]) -> ArtifactBackedReasonV1:
    return ArtifactBackedReasonV1(gate=gate, reason=text, artifact_refs=refs[:32])


def _read_human_reference(
    *,
    store: StudyStoreV1,
    prepared: PreparedStudyV1,
    reference: ArtifactRefV5,
    label: str,
) -> bytes:
    """Authenticate a review citation from the store or a fixed fixture root.

    Imported responses and drafts live in the shared study store.  Mechanism
    reports, role packages, and other evidence citations remain in their
    arm-owned fixture repository, even though the verifier has already bound
    their exact references.  Sending every citation through ``StudyStore``
    would therefore reject legitimate fixture evidence (or tempt a caller to
    relocate it into the store).  Try the store first, then authenticate the
    exact path in each fixed root with no digest search or repair.
    """

    store_error: StudyAuthorityError | None = None
    try:
        return _read_store(store, reference, label)
    except StudyAuthorityError as exc:
        store_error = exc
        pass

    roots = (
        (prepared.ancestor_root, prepared.ancestor_manifest_ref),
        (prepared.primary_root, prepared.primary_manifest_ref),
        (prepared.withheld_root, prepared.withheld_manifest_ref),
    )
    for root, manifest_ref in roots:
        try:
            fixture = reopen_study_fixture_v1(
                root=root,
                manifest_ref=manifest_ref,
                registry=prepared.registry,
            )
            relative = reference.relative_path
            if relative.startswith("adapter-blobs/") and relative.endswith(".bin"):
                parts = relative.split("/")
                if len(parts) != 3:
                    raise StudyAuthorityError(f"{label} binary path is not canonical")
                raw = fixture.repository.load_binary_state(
                    namespace=parts[1],
                    key=parts[2][:-4],
                    reference=reference,
                    maximum_bytes=64 * 1024 * 1024,
                )
            else:
                raw = fixture.repository.authenticate_exact(reference).content
            if _sha256(raw) != reference.sha256:
                raise StudyAuthorityError(f"{label} bytes differ from their reference")
            return raw
        except Exception:  # noqa: BLE001 - try the next fixed authority root
            continue
    raise StudyAuthorityError(f"{label} is not readable from an authenticated study root") from store_error


def _validate_human_review(
    *,
    review: HumanEvidenceReviewV1,
    prepared: PreparedStudyV1,
    store: StudyStoreV1,
    verification_refs: tuple[ArtifactRefV5, ...],
) -> None:
    """Bind semantic review citations to the exact imported arm artifacts."""

    if review.rubric_sha256 != prepared.manifest.rubric_sha256:
        raise StudyAuthorityError("human review is bound to a different frozen rubric")
    if not review.reasons:
        raise StudyAuthorityError("human review lacks semantic reasons")
    allowed = {(ref.relative_path, ref.sha256) for ref in verification_refs}
    if any((ref.relative_path, ref.sha256) not in allowed for ref in review.artifact_refs):
        raise StudyAuthorityError("human review cites an artifact outside the authenticated study graph")
    if any(
        not review.axis_reasons.get(axis) or not review.axis_citations.get(axis)
        for axis in _REVIEW_AXES
    ):
        raise StudyAuthorityError("human review lacks separate semantic reasons and citations for every review axis")
    axis_citations = tuple(
        ref
        for axis in _REVIEW_AXES
        for ref in review.axis_citations[axis]
    )
    if any((ref.relative_path, ref.sha256) not in allowed for ref in axis_citations):
        raise StudyAuthorityError("human review axis cites an artifact outside the authenticated study graph")
    import_by_arm: dict[str, StudyImportV1] = {}
    for import_ref in store.list_refs(kind="imports"):
        imported = _decode_contract(store, import_ref, StudyImportV1, "study import")
        if imported.arm in import_by_arm:
            raise StudyAuthorityError("human review import binding is ambiguous")
        import_by_arm[imported.arm] = imported
    if set(import_by_arm) != set(_ARM_NAMES):
        raise StudyAuthorityError("human review requires one authenticated import for each arm")
    expected_response_sequence = tuple(
        (import_by_arm[name].original_response_ref.relative_path, import_by_arm[name].original_response_ref.sha256)
        for name in _ARM_NAMES
    )
    expected_drafts_by_arm = {
        name: tuple((reference.relative_path, reference.sha256) for reference in import_by_arm[name].draft_refs)
        for name in _ARM_NAMES
    }
    if set(review.arm_draft_refs) != set(_ARM_NAMES):
        raise StudyAuthorityError("human review draft ownership does not name both arms exactly")
    for name in _ARM_NAMES:
        supplied_arm_drafts = tuple(
            (reference.relative_path, reference.sha256)
            for reference in review.arm_draft_refs[name]
        )
        if supplied_arm_drafts != expected_drafts_by_arm[name]:
            raise StudyAuthorityError(f"human review draft references do not bind the {name} arm")
    expected_draft_sequence = tuple(
        (reference.relative_path, reference.sha256)
        for name in _ARM_NAMES
        for reference in import_by_arm[name].draft_refs
    )
    supplied_response_sequence = tuple((ref.relative_path, ref.sha256) for ref in review.response_refs)
    supplied_draft_sequence = tuple((ref.relative_path, ref.sha256) for ref in review.draft_refs)
    if supplied_response_sequence != expected_response_sequence:
        raise StudyAuthorityError("human review response references do not bind both imported arm responses in arm order")
    if supplied_draft_sequence != expected_draft_sequence:
        raise StudyAuthorityError("human review draft references do not bind both imported arm drafts in arm order")
    for reference in review.artifact_refs:
        _read_human_reference(
            store=store,
            prepared=prepared,
            reference=reference,
            label="human citation",
        )
    for reference in review.response_refs + review.draft_refs:
        _read_store(store, reference, "human-reviewed imported artifact")
    for reference in axis_citations:
        _read_human_reference(
            store=store,
            prepared=prepared,
            reference=reference,
            label="human axis citation",
        )


def verify_study_v1(
    *,
    prepared: PreparedStudyV1,
    store: StudyStoreV1,
    ledger: StudyLedgerV1 | None,
    human_review: HumanEvidenceReviewV1 | None = None,
) -> StudyVerificationV1:
    """Authenticate a prepared study without preparation, repair or execution."""

    if type(prepared) is not PreparedStudyV1 or type(store) is not StudyStoreV1:
        raise StudyContractError("study verification inputs are invalid")
    # A caller-supplied PreparedStudy is only a locator.  Reconstruct the
    # complete object from the fixed persisted graph before using comparison,
    # mode, parent, memory, F/L, or root identities.  This is read-only and
    # deliberately does not prepare, compose, recover, or repair anything.
    try:
        authenticated_prepared = _load_prepared_study_v1(root=prepared.root)
    except Exception as exc:  # noqa: BLE001 - convert all stale locator failures to authority
        raise StudyAuthorityError(
            f"prepared study could not be reauthenticated from its persisted graph: {exc}"
        ) from exc
    if authenticated_prepared != prepared:
        raise StudyAuthorityError("prepared study differs from its reauthenticated persisted authorities")
    prepared = authenticated_prepared
    prepared_store_identity = LocalArtifactRepositoryV5(prepared.store_root).root_identity_sha256
    if store.repository.root_identity_sha256 != prepared_store_identity:
        raise StudyAuthorityError("study verifier store root differs from prepared authority")
    if ledger is not None:
        if type(ledger) is not StudyLedgerV1 or ledger.manifest != prepared.manifest:
            raise StudyAuthorityError("study verifier ledger differs from prepared manifest")
        if ledger.store.repository.root_identity_sha256 != store.repository.root_identity_sha256:
            raise StudyAuthorityError("study verifier ledger store differs from the supplied study store")
        if ledger.grant.repository_root_identity_sha256 != store.repository.root_identity_sha256:
            raise StudyAuthorityError("study verifier grant differs from the supplied study store")
        if ledger.grant.manifest_sha256 != prepared.manifest.sha256:
            raise StudyAuthorityError("study verifier grant differs from the prepared manifest")

    arms: list[StudyArmVerificationV1] = []
    # Keep arm/root provenance in the key.  The two descendant repositories
    # intentionally reuse canonical relative paths (checkpoint.json,
    # archive.json, policy-source values), but those bytes are distinct
    # authorities and must not be silently merged.
    all_refs: dict[tuple[str, str, str, str], ArtifactRefV5] = {}
    packages: dict[str, RoleInvocationPackageV5 | None] = {}
    reservation_projection = () if ledger is None else ledger.read_only_reservation_projection()
    for arm in _ARM_NAMES:
        verified, refs, package = _verify_fixture_arm(
            prepared=prepared,
            arm=arm,
            ledger=ledger,
            reservation_projection=reservation_projection,
        )
        arms.append(verified)
        packages[arm] = package
        for ref in refs:
            all_refs.setdefault(
                (arm, verified.repository_root_identity_sha256, ref.relative_path, ref.sha256),
                ref,
            )

    if ledger is None:
        # A preparation-only/no-grant verifier may retain the historical
        # round-one graph.  It must reject any round-two fixture authority
        # even when a caller has hidden every shared-store execution record;
        # otherwise a detached descendant could masquerade as an untouched
        # prepared study.
        fixture_only_round_two = [
            item.arm
            for item in arms
            if item.state != "not_started"
            or item.round_two_status != "not_started"
            or any(row.get("round_index") == 2 for row in item.measurements)
        ]
        if fixture_only_round_two:
            raise StudyAuthorityError(
                "study fixture roots contain round-two history without an authenticated execution grant: "
                + ", ".join(fixture_only_round_two)
            )

    # The persisted store is itself an allowlist.  Every record in each
    # controller-owned namespace is read and hash-checked before it can enter
    # the trace; this catches orphan imports and duplicate cost/receipt blobs.
    store_refs: list[ArtifactRefV5] = []
    for kind in _STORE_KINDS:
        for ref in store.list_refs(kind=kind):
            _read_store(store, ref, f"study {kind} record")
            store_refs.append(ref)
            all_refs.setdefault(
                ("study-store", store.repository.root_identity_sha256, ref.relative_path, ref.sha256),
                ref,
            )

    if ledger is None and store_refs:
        allowed = {prepared.manifest_ref.relative_path, prepared.registry_ref.relative_path, prepared.rubric_ref.relative_path, prepared.primary_preflight_ref.relative_path, prepared.withheld_preflight_ref.relative_path, prepared.primary_live_call_ref.relative_path, prepared.withheld_live_call_ref.relative_path, prepared.request_comparison_ref.relative_path}
        allowed.update(ref.relative_path for kind in ("schemas", "parsers", "prompts") for ref in store.list_refs(kind=kind))
        extra = [ref.relative_path for ref in store_refs if ref.relative_path not in allowed]
        if extra:
            raise StudyAuthorityError("study store contains execution records without a grant: " + ", ".join(extra))

    errors: list[str] = []
    warnings: list[str] = []
    confounds = list(prepared.request_comparison.confounds)
    if prepared.request_comparison.status != "eligible":
        confounds.extend(prepared.request_comparison.actual_differences)
    if prepared.request_comparison.unresolved_audit_channels:
        confounds.extend(prepared.request_comparison.unresolved_audit_channels)
    confounds = list(dict.fromkeys(confounds))
    if prepared.mode == "offline_fixture":
        warnings.append("Offline fixture evidence use is not assessed; responses and outcomes are synthetic.")
    if any(item.state != "completed" for item in arms):
        warnings.append("At least one arm is incomplete or rejected; no paired attribution is claimed.")

    both_completed = all(item.state == "completed" for item in arms)
    # A readable trace is a completed linked study result.  Rejected or
    # interrupted arms remain authentically reportable, but they cannot make
    # the trace-integrity gate appear complete merely because some persisted
    # authority exists.
    trace_gate: StudyGateV1 = "verified" if both_completed else "incomplete"
    completion_gate: StudyGateV1 = "verified" if both_completed else ("incomplete" if any(item.state == "incomplete" for item in arms) else "not_assessed")
    evidence_delivery: StudyGateV1 = "verified" if both_completed and all(packages[arm] is not None for arm in _ARM_NAMES) else "incomplete"
    arm_contrasts = tuple(item.case_contrast for item in arms)
    if not both_completed or any(item == "unavailable" for item in arm_contrasts):
        case_contrast: StudyCaseContrastV1 = "unavailable"
    elif any(item == "contradicted_on_cases" for item in arm_contrasts):
        case_contrast = "contradicted_on_cases"
    else:
        case_contrast = "matched_on_cases"
    arm_production = tuple(item.production_assessment for item in arms)
    if any(item == "contradicted_on_cases" for item in arm_production):
        production = "contradicted_on_cases"
    elif both_completed and all(item == "supported_on_cases" for item in arm_production):
        production = "supported_on_cases"
    else:
        production = "insufficient_evidence"
    if prepared.mode == "offline_fixture":
        attribution = "not_assessed"
    else:
        primary_arm, withheld_arm = arms
        pair_label = (
            f"primary production={primary_arm.production_assessment}, contrast={primary_arm.case_contrast}; "
            f"withheld production={withheld_arm.production_assessment}, contrast={withheld_arm.case_contrast}"
        )
        if prepared.request_comparison.leak_observations:
            confounds.append("paired_attribution_invalid_leak: " + "; ".join(prepared.request_comparison.leak_observations))
            attribution = "inconclusive"
        elif prepared.request_comparison.status != "eligible":
            confounds.append("paired_attribution_invalid_request_comparison: " + pair_label)
            attribution = "inconclusive"
        elif not both_completed:
            confounds.append("paired_attribution_incomplete_arm: " + pair_label)
            attribution = "inconclusive"
        elif (
            primary_arm.production_assessment == withheld_arm.production_assessment
            and primary_arm.case_contrast == withheld_arm.case_contrast
        ):
            confounds.append("paired_attribution_equal_or_nondiscriminating_pair: " + pair_label)
            attribution = "inconclusive"
        elif not all(
            item.production_assessment in {"supported_on_cases", "contradicted_on_cases"}
            for item in arms
        ):
            confounds.append("paired_attribution_insufficient_production_evidence: " + pair_label)
            attribution = "inconclusive"
        elif not all(
            item.case_contrast in {"matched_on_cases", "contradicted_on_cases"}
            for item in arms
        ):
            confounds.append("paired_attribution_unavailable_or_unmeasured_contrast: " + pair_label)
            attribution = "inconclusive"
        elif (
            primary_arm.production_assessment == "supported_on_cases"
            and withheld_arm.production_assessment != "supported_on_cases"
            and primary_arm.case_contrast == "matched_on_cases"
            and withheld_arm.case_contrast != "matched_on_cases"
        ):
            attribution = "supported"
        else:
            confounds.append("paired_attribution_observed_but_not_positive_or_discriminating: " + pair_label)
            attribution = "inconclusive"
    evidence_use = "not_assessed"
    reasons = (
        _reason("trace_integrity", "Persisted fixture graphs, F/L identities, and accounting records were independently authenticated.", tuple(all_refs.values())),
        _reason("claims", "Evidence use, experiment outcome, attribution, improvement, and authored-code execution remain separate axes.", tuple(all_refs.values())),
    )
    if human_review is not None:
        _validate_human_review(
            review=human_review,
            prepared=prepared,
            store=store,
            verification_refs=tuple(all_refs.values()),
        )
        if prepared.mode == "live_study":
            if human_review.evidence_interpretation == "supported" and human_review.revision_quality == "supported" and human_review.claim_pattern == "supported":
                evidence_use = "supported"
            elif human_review.evidence_interpretation in {"not_supported", "inconclusive"}:
                evidence_use = human_review.evidence_interpretation  # type: ignore[assignment]
            else:
                evidence_use = "inconclusive"

    verdicts = StudyVerdictsV1(
        schema_version=STUDY_SCHEMA_VERSION_V1,
        trace_integrity=trace_gate,
        evidence_delivery=evidence_delivery,
        evidence_use=evidence_use,  # type: ignore[arg-type]
        production_assessment=production,  # type: ignore[arg-type]
        case_contrast=case_contrast,
        experiment_completion=completion_gate,
        feedback_attribution=attribution,  # type: ignore[arg-type]
        optimization_improvement="not_established",
        authored_code_execution="not_established",
        reasons=reasons,
    )
    measurements: tuple[Mapping[str, object], ...] = tuple(
        row
        for item in arms
        for row in (
            {
                "arm": item.arm,
                "repository_root_identity_sha256": item.repository_root_identity_sha256,
                "state": item.state,
                "round_two_status": item.round_two_status,
                "evidence_ids": item.evidence_ids,
                "production_assessment": item.production_assessment,
                "case_contrast": item.case_contrast,
                "terminal_ref": None if item.terminal_ref is None else item.terminal_ref.to_primitive(),
                "import_ref": None if item.import_ref is None else item.import_ref.to_primitive(),
            },
            *item.measurements,
        )
    )
    return StudyVerificationV1(
        schema_version=STUDY_SCHEMA_VERSION_V1,
        manifest_ref=prepared.manifest_ref,
        verdicts=verdicts,
        arms=tuple(arms),
        measurements=measurements,
        errors=tuple(errors),
        warnings=tuple(warnings),
        confounds=tuple(confounds),
        artifact_refs=tuple(all_refs.values()),
        human_review=human_review,
    )


__all__ = [
    "HumanEvidenceReviewV1",
    "StudyArmVerificationV1",
    "StudyVerificationV1",
    "load_prepared_study_v1",
    "verify_study_v1",
]
