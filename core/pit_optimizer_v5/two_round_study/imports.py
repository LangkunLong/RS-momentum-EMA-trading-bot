"""Authenticated translation of a live investigator response into fixture F.

The study response is deliberately kept outside the ordinary V5 role protocol.
This module authenticates the complete study ledger and frozen fixture preflight,
then stores a lossless ordinary response projection (``T``).  Fixture replay
uses that exact projection and never enters a provider transport.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import stat
from typing import Iterator, Literal

from core.pit_optimizer_artifacts import _windows_extended_path

from core.pit_optimizer_v5.artifacts import (
    ArchiveSnapshotV5,
    ArtifactRefV5,
    ArtifactRepositoryFailureV5,
    LocalArtifactRepositoryV5,
    RepositoryCheckpointV5,
)
from core.pit_optimizer_v5.candidate_ir import SourceBundleV5
from core.pit_optimizer_v5.contracts import (
    CampaignManifestV5,
    canonical_json_bytes_v5,
    canonical_primitive_v5,
)
from core.pit_optimizer_v5.manifest import (
    AuthenticatedCampaignManifestV5,
    authenticate_campaign_manifest_v5,
)
from core.pit_optimizer_v5.memory import StoredExperimentRecordV5, reduce_experiment_journal_v5
from core.pit_optimizer_v5.provider import (
    ExistingPersistedRoleRequestV5,
    FixtureRoleRunnerV5,
    FixtureRoleTerminalAuthorityV5,
    FreshPersistedRoleRequestV5,
    RecoverableRoleInvokerV5,
    RoleCallKeyV5,
    RoleInvocationPackageV5,
    RoleReconciliationFailureV5,
    RoleRequestV5,
    canonical_role_response_sha256_v5,
    parse_and_bind_role_artifact,
    role_schema_authority_from_manifest_v5,
)
from core.pit_optimizer_v5.search import ArchiveRecordAuthorityV5, CandidateArchiveReducerV5

from .contracts import (
    ExperimentDraftV1,
    StudyArmV1,
    StudyAuthorityError,
    StudyContractError,
    StudyModeV1,
    StudyResponseV1,
)
from .ledger import AuthenticatedStudyTerminalV1, StudyLedgerV1
from .live_calls import (
    FixturePreflightV1,
    StudyCallRequestV1,
    authenticate_fixture_preflight_v1,
)
from .schema import parse_study_response_v1
from .store import StudyStoreV1


IMPORT_SCHEMA_VERSION_V1 = 1
IMPORT_TRANSLATOR_VERSION_V1 = "lossless-ordinary-artifact-v1"
_MAX_IMPORT_DRAFTS = 64
_MAX_TEXT_BYTES = 512


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _digest(value: object, label: str) -> str:
    if type(value) is not str or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise StudyContractError(f"{label} is not a SHA-256 digest")
    return value


def _text(value: object, label: str, *, maximum: int = _MAX_TEXT_BYTES) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > maximum
    ):
        raise StudyContractError(f"{label} is invalid")
    return value


def _is_fixture_manifest_path(value: str) -> bool:
    """Recognize the bounded manifest edge used by provider-free fixture roots."""

    path = Path(value)
    return (
        path.is_absolute() is False
        and path.parent.as_posix() == "evaluator"
        and path.name.endswith("manifest.json")
    )


def _long_path(path: Path) -> str:
    return _windows_extended_path(path) if os.name == "nt" else str(path)


def _long_directory(path: Path, label: str) -> None:
    try:
        metadata = os.stat(_long_path(path), follow_symlinks=False)
    except OSError as exc:
        raise StudyAuthorityError(f"{label} is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    ):
        raise StudyAuthorityError(f"{label} is not a fixed directory")


def _fixture_manifest_entries(root: Path) -> Iterator[str]:
    """Enumerate only the fixed evaluator manifest directory with long I/O."""

    _long_directory(root, "fixture campaign manifest root")
    evaluator = root / "evaluator"
    _long_directory(evaluator, "fixture campaign evaluator directory")
    try:
        with os.scandir(_long_path(evaluator)) as iterator:
            entries = sorted(iterator, key=lambda item: item.name)
    except OSError as exc:
        raise StudyAuthorityError("fixture campaign evaluator directory is unavailable") from exc
    for entry in entries:
        name = entry.name
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise StudyAuthorityError("fixture campaign manifest path is invalid")
        try:
            metadata = entry.stat(follow_symlinks=False)
            reparse = stat.S_ISLNK(metadata.st_mode) or bool(
                getattr(metadata, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            )
            if reparse:
                raise StudyAuthorityError("fixture campaign evaluator contains a reparse point")
            if not stat.S_ISREG(metadata.st_mode) or not name.endswith(".json"):
                continue
            relative_path = f"evaluator/{name}"
            if _is_fixture_manifest_path(relative_path):
                yield relative_path
        except StudyAuthorityError:
            raise
        except OSError as exc:
            raise StudyAuthorityError("fixture campaign manifest entry is unavailable") from exc


def _strict_json(raw: bytes, label: str) -> object:
    if type(raw) is not bytes:
        raise StudyAuthorityError(f"{label} bytes are invalid")

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise StudyAuthorityError(f"{label} contains duplicate fields")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise StudyAuthorityError(f"{label} contains nonfinite JSON constant {value}")

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=reject_constant)
    except StudyAuthorityError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise StudyAuthorityError(f"{label} JSON is invalid") from exc


def _canonical_decode(cls, raw: bytes, label: str):
    value = _strict_json(raw, label)
    try:
        decoded = cls.from_primitive(value)
    except StudyContractError as exc:
        raise StudyAuthorityError(f"{label} does not satisfy its closed contract") from exc
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise StudyAuthorityError(f"{label} does not satisfy its closed contract") from exc
    if decoded.canonical_bytes() != raw:
        raise StudyAuthorityError(f"{label} bytes are not canonical")
    return decoded


def _strict_mapping(value: object, fields: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise StudyContractError(f"{label} fields are invalid")
    return value


def _ref_primitive(reference: ArtifactRefV5) -> dict[str, object]:
    return {"relative_path": reference.relative_path, "sha256": reference.sha256}


def _ref_from_primitive(value: object, label: str) -> ArtifactRefV5:
    raw = _strict_mapping(value, {"relative_path", "sha256"}, label)
    try:
        return ArtifactRefV5(relative_path=raw["relative_path"], sha256=raw["sha256"])  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise StudyContractError(f"{label} is invalid") from exc


def _call_primitive(call: RoleCallKeyV5) -> dict[str, object]:
    return {
        "campaign_id": call.campaign_id,
        "round_index": call.round_index,
        "role": call.role,
        "role_position": call.role_position,
        "attempt_kind": call.attempt_kind,
        "attempt_index": call.attempt_index,
        "request_sha256": call.request_sha256,
    }


def _call_from_primitive(value: object, label: str) -> RoleCallKeyV5:
    raw = _strict_mapping(
        value,
        {"campaign_id", "round_index", "role", "role_position", "attempt_kind", "attempt_index", "request_sha256"},
        label,
    )
    try:
        return RoleCallKeyV5(
            campaign_id=raw["campaign_id"],  # type: ignore[arg-type]
            round_index=raw["round_index"],  # type: ignore[arg-type]
            role=raw["role"],  # type: ignore[arg-type]
            role_position=raw["role_position"],  # type: ignore[arg-type]
            attempt_kind=raw["attempt_kind"],  # type: ignore[arg-type]
            attempt_index=raw["attempt_index"],  # type: ignore[arg-type]
            request_sha256=raw["request_sha256"],  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as exc:
        raise StudyContractError(f"{label} is invalid") from exc


def _ref_path(reference: ArtifactRefV5, prefix: str) -> bool:
    return reference.relative_path.startswith(prefix) and reference.relative_path.endswith(".bin")


@dataclass(frozen=True, slots=True)
class StudyImportV1:
    """Immutable provenance for one authenticated live investigator import."""

    study_id: str
    arm: StudyArmV1
    mode: StudyModeV1
    preflight_ref: ArtifactRefV5
    terminal_ref: ArtifactRefV5
    fixture_call: RoleCallKeyV5
    live_call: RoleCallKeyV5
    fixture_request_ref: ArtifactRefV5
    fixture_request_sha256: str
    live_request_ref: ArtifactRefV5
    live_request_sha256: str
    original_response_ref: ArtifactRefV5
    original_response_sha256: str
    parsed_envelope_ref: ArtifactRefV5
    parsed_envelope_sha256: str
    translated_ref: ArtifactRefV5
    translated_sha256: str
    ordinary_artifact_ref: ArtifactRefV5
    ordinary_artifact_sha256: str
    draft_refs: tuple[ArtifactRefV5, ...]
    draft_hashes: tuple[str, ...]
    fixture_root_identity_sha256: str
    fixture_manifest_ref: ArtifactRefV5
    ledger_root_identity_sha256: str
    manifest_sha256: str
    grant_sha256: str
    audit_domain: str
    response_schema_sha256: str
    parser_sha256: str
    translator_version: str = IMPORT_TRANSLATOR_VERSION_V1
    schema_version: Literal[1] = IMPORT_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        _text(self.study_id, "study import study ID")
        if self.arm not in {"primary", "withheld"}:
            raise StudyContractError("study import arm is invalid")
        if self.mode not in {"offline_fixture", "live_study"}:
            raise StudyContractError("study import mode is invalid")
        references = (
            (self.preflight_ref, "study import preflight reference"),
            (self.terminal_ref, "study import terminal reference"),
            (self.fixture_request_ref, "study import fixture request reference"),
            (self.live_request_ref, "study import live request reference"),
            (self.original_response_ref, "study import response reference"),
            (self.parsed_envelope_ref, "study import parsed reference"),
            (self.translated_ref, "study import translated reference"),
            (self.ordinary_artifact_ref, "study import ordinary artifact reference"),
            (self.fixture_manifest_ref, "study import fixture manifest reference"),
        )
        for reference, label in references:
            if type(reference) is not ArtifactRefV5:
                raise StudyContractError(f"{label} is invalid")
        if type(self.fixture_call) is not RoleCallKeyV5 or type(self.live_call) is not RoleCallKeyV5:
            raise StudyContractError("study import role call identities are invalid")
        for call, label in ((self.fixture_call, "fixture"), (self.live_call, "live")):
            if (
                call.role != "investigator"
                or call.role_position != 1
                or call.round_index != 2
                or call.attempt_kind != "primary"
                or call.attempt_index != 1
            ):
                raise StudyContractError(f"study import {label} call identity is invalid")
        for value, label in (
            (self.fixture_request_sha256, "fixture request identity"),
            (self.live_request_sha256, "live request identity"),
            (self.original_response_sha256, "original response identity"),
            (self.parsed_envelope_sha256, "parsed envelope identity"),
            (self.translated_sha256, "translated response identity"),
            (self.ordinary_artifact_sha256, "ordinary artifact identity"),
            (self.fixture_root_identity_sha256, "fixture root identity"),
            (self.ledger_root_identity_sha256, "ledger root identity"),
            (self.manifest_sha256, "manifest identity"),
            (self.grant_sha256, "grant identity"),
            (self.response_schema_sha256, "response schema identity"),
            (self.parser_sha256, "parser identity"),
        ):
            _digest(value, label)
        if not _is_fixture_manifest_path(self.fixture_manifest_ref.relative_path):
            raise StudyAuthorityError("study import fixture manifest reference is not canonical")
        if self.fixture_call.request_sha256 != self.fixture_request_sha256:
            raise StudyAuthorityError("study import fixture call does not bind F")
        if self.live_call.request_sha256 != self.live_request_sha256:
            raise StudyAuthorityError("study import live call does not bind L")
        if not _ref_path(self.preflight_ref, "adapter-blobs/study-v1-preflights/"):
            raise StudyAuthorityError("study import preflight reference is outside the study namespace")
        if not _ref_path(self.terminal_ref, "adapter-blobs/study-v1-terminals/"):
            raise StudyAuthorityError("study import terminal reference is outside the study namespace")
        if self.fixture_request_ref.relative_path != f"roles/requests/{self.fixture_call.sha256}.json":
            raise StudyAuthorityError("study import fixture request reference differs from its call")
        if self.live_request_ref.relative_path != f"adapter-blobs/study-v1-requests/{self.live_request_sha256}.bin":
            raise StudyAuthorityError("study import live request reference differs from L")
        if self.original_response_ref.relative_path != f"adapter-blobs/study-v1-raw-responses/{self.live_request_sha256}.bin":
            raise StudyAuthorityError("study import response reference differs from L")
        if self.parsed_envelope_ref.relative_path != f"adapter-blobs/study-v1-parsed/{self.live_request_sha256}.bin":
            raise StudyAuthorityError("study import parsed reference differs from L")
        storage_key = self.terminal_ref.sha256
        if self.translated_ref.relative_path != f"adapter-blobs/study-v1-import-translations/{storage_key}.bin":
            raise StudyAuthorityError("study import translated reference is not deterministic")
        if self.ordinary_artifact_ref.relative_path != f"adapter-blobs/study-v1-import-artifacts/{storage_key}.bin":
            raise StudyAuthorityError("study import ordinary artifact reference is not deterministic")
        if type(self.draft_refs) is not tuple or type(self.draft_hashes) is not tuple:
            raise StudyContractError("study import draft references are invalid")
        if not self.draft_refs or len(self.draft_refs) > _MAX_IMPORT_DRAFTS or len(self.draft_refs) != len(self.draft_hashes):
            raise StudyContractError("study import drafts are invalid")
        for index, (reference, digest) in enumerate(zip(self.draft_refs, self.draft_hashes, strict=True)):
            if type(reference) is not ArtifactRefV5:
                raise StudyContractError("study import draft reference is invalid")
            if reference.relative_path != f"adapter-blobs/study-v1-import-drafts/{storage_key}-{index}.bin":
                raise StudyAuthorityError("study import draft reference is not deterministic")
            if reference.sha256 != digest:
                raise StudyAuthorityError("study import draft hash differs from its reference")
            _digest(digest, "study import draft identity")
        if type(self.translator_version) is not str or self.translator_version != IMPORT_TRANSLATOR_VERSION_V1:
            raise StudyContractError("study import translator version is invalid")
        if type(self.audit_domain) is not str or not self.audit_domain.strip():
            raise StudyContractError("study import audit domain is invalid")
        if type(self.schema_version) is not int or self.schema_version != IMPORT_SCHEMA_VERSION_V1:
            raise StudyContractError("study import schema version is invalid")

    @property
    def preflight_sha256(self) -> str:
        return self.preflight_ref.sha256

    @property
    def terminal_sha256(self) -> str:
        return self.terminal_ref.sha256

    @property
    def fixture_request(self) -> str:
        return self.fixture_request_sha256

    @property
    def live_request(self) -> str:
        return self.live_request_sha256

    @property
    def response_ref(self) -> ArtifactRefV5:
        return self.original_response_ref

    @property
    def response_sha256(self) -> str:
        return self.original_response_sha256

    @property
    def parsed_ref(self) -> ArtifactRefV5:
        return self.parsed_envelope_ref

    @property
    def parsed_sha256(self) -> str:
        return self.parsed_envelope_sha256

    @property
    def artifact_ref(self) -> ArtifactRefV5:
        return self.ordinary_artifact_ref

    @property
    def artifact_sha256(self) -> str:
        return self.ordinary_artifact_sha256

    @property
    def draft_sha256s(self) -> tuple[str, ...]:
        return self.draft_hashes

    @property
    def storage_key(self) -> str:
        return self.terminal_ref.sha256

    @property
    def storage_ref(self) -> ArtifactRefV5:
        return ArtifactRefV5(
            f"adapter-blobs/study-v1-imports/{self.storage_key}.bin",
            self.sha256,
        )

    def to_primitive(self) -> dict[str, object]:
        return {
            "study_id": self.study_id,
            "arm": self.arm,
            "mode": self.mode,
            "preflight_ref": _ref_primitive(self.preflight_ref),
            "terminal_ref": _ref_primitive(self.terminal_ref),
            "fixture_call": _call_primitive(self.fixture_call),
            "live_call": _call_primitive(self.live_call),
            "fixture_request_ref": _ref_primitive(self.fixture_request_ref),
            "fixture_request_sha256": self.fixture_request_sha256,
            "live_request_ref": _ref_primitive(self.live_request_ref),
            "live_request_sha256": self.live_request_sha256,
            "original_response_ref": _ref_primitive(self.original_response_ref),
            "original_response_sha256": self.original_response_sha256,
            "parsed_envelope_ref": _ref_primitive(self.parsed_envelope_ref),
            "parsed_envelope_sha256": self.parsed_envelope_sha256,
            "translated_ref": _ref_primitive(self.translated_ref),
            "translated_sha256": self.translated_sha256,
            "ordinary_artifact_ref": _ref_primitive(self.ordinary_artifact_ref),
            "ordinary_artifact_sha256": self.ordinary_artifact_sha256,
            "draft_refs": [_ref_primitive(item) for item in self.draft_refs],
            "draft_hashes": list(self.draft_hashes),
            "fixture_root_identity_sha256": self.fixture_root_identity_sha256,
            "fixture_manifest_ref": _ref_primitive(self.fixture_manifest_ref),
            "ledger_root_identity_sha256": self.ledger_root_identity_sha256,
            "manifest_sha256": self.manifest_sha256,
            "grant_sha256": self.grant_sha256,
            "audit_domain": self.audit_domain,
            "response_schema_sha256": self.response_schema_sha256,
            "parser_sha256": self.parser_sha256,
            "translator_version": self.translator_version,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes_v5(self.to_primitive())

    @property
    def sha256(self) -> str:
        return _sha256(self.canonical_bytes())

    @classmethod
    def from_primitive(cls, value: object) -> "StudyImportV1":
        fields = {
            "study_id",
            "arm",
            "mode",
            "preflight_ref",
            "terminal_ref",
            "fixture_call",
            "live_call",
            "fixture_request_ref",
            "fixture_request_sha256",
            "live_request_ref",
            "live_request_sha256",
            "original_response_ref",
            "original_response_sha256",
            "parsed_envelope_ref",
            "parsed_envelope_sha256",
            "translated_ref",
            "translated_sha256",
            "ordinary_artifact_ref",
            "ordinary_artifact_sha256",
            "draft_refs",
            "draft_hashes",
            "fixture_root_identity_sha256",
            "fixture_manifest_ref",
            "ledger_root_identity_sha256",
            "manifest_sha256",
            "grant_sha256",
            "audit_domain",
            "response_schema_sha256",
            "parser_sha256",
            "translator_version",
            "schema_version",
        }
        raw = _strict_mapping(value, fields, "study import")
        draft_refs = raw["draft_refs"]
        draft_hashes = raw["draft_hashes"]
        if type(draft_refs) is not list or type(draft_hashes) is not list:
            raise StudyContractError("study import drafts are invalid")
        return cls(
            study_id=raw["study_id"],  # type: ignore[arg-type]
            arm=raw["arm"],  # type: ignore[arg-type]
            mode=raw["mode"],  # type: ignore[arg-type]
            preflight_ref=_ref_from_primitive(raw["preflight_ref"], "study import preflight ref"),
            terminal_ref=_ref_from_primitive(raw["terminal_ref"], "study import terminal ref"),
            fixture_call=_call_from_primitive(raw["fixture_call"], "study import fixture call"),
            live_call=_call_from_primitive(raw["live_call"], "study import live call"),
            fixture_request_ref=_ref_from_primitive(raw["fixture_request_ref"], "study import fixture request ref"),
            fixture_request_sha256=raw["fixture_request_sha256"],  # type: ignore[arg-type]
            live_request_ref=_ref_from_primitive(raw["live_request_ref"], "study import live request ref"),
            live_request_sha256=raw["live_request_sha256"],  # type: ignore[arg-type]
            original_response_ref=_ref_from_primitive(raw["original_response_ref"], "study import response ref"),
            original_response_sha256=raw["original_response_sha256"],  # type: ignore[arg-type]
            parsed_envelope_ref=_ref_from_primitive(raw["parsed_envelope_ref"], "study import parsed ref"),
            parsed_envelope_sha256=raw["parsed_envelope_sha256"],  # type: ignore[arg-type]
            translated_ref=_ref_from_primitive(raw["translated_ref"], "study import translated ref"),
            translated_sha256=raw["translated_sha256"],  # type: ignore[arg-type]
            ordinary_artifact_ref=_ref_from_primitive(raw["ordinary_artifact_ref"], "study import artifact ref"),
            ordinary_artifact_sha256=raw["ordinary_artifact_sha256"],  # type: ignore[arg-type]
            draft_refs=tuple(_ref_from_primitive(item, "study import draft ref") for item in draft_refs),
            draft_hashes=tuple(draft_hashes),  # type: ignore[arg-type]
            fixture_root_identity_sha256=raw["fixture_root_identity_sha256"],  # type: ignore[arg-type]
            fixture_manifest_ref=_ref_from_primitive(raw["fixture_manifest_ref"], "study import fixture manifest ref"),
            ledger_root_identity_sha256=raw["ledger_root_identity_sha256"],  # type: ignore[arg-type]
            manifest_sha256=raw["manifest_sha256"],  # type: ignore[arg-type]
            grant_sha256=raw["grant_sha256"],  # type: ignore[arg-type]
            audit_domain=raw["audit_domain"],  # type: ignore[arg-type]
            response_schema_sha256=raw["response_schema_sha256"],  # type: ignore[arg-type]
            parser_sha256=raw["parser_sha256"],  # type: ignore[arg-type]
            translator_version=raw["translator_version"],  # type: ignore[arg-type]
            schema_version=raw["schema_version"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_canonical_json(cls, raw: bytes | str) -> "StudyImportV1":
        encoded = raw.encode("utf-8") if type(raw) is str else raw
        if type(encoded) is not bytes:
            raise StudyContractError("study import bytes are invalid")
        return _canonical_decode(cls, encoded, "study import")


@dataclass(frozen=True, slots=True)
class _ImportMaterials:
    fixture_request: RoleRequestV5
    live_request: StudyCallRequestV1
    live_request_ref: ArtifactRefV5
    fixture_preflight: FixturePreflightV1
    fixture_preflight_ref: ArtifactRefV5
    fixture_manifest_ref: ArtifactRefV5
    terminal: AuthenticatedStudyTerminalV1
    raw_response: bytes
    parsed_response: StudyResponseV1
    translated: bytes
    ordinary_artifact: bytes
    drafts: tuple[ExperimentDraftV1, ...]


def _exact_store_ref(store: StudyStoreV1, *, kind: str, path: str) -> tuple[ArtifactRefV5, bytes]:
    refs = tuple(ref for ref in store.list_refs(kind=kind, maximum_entries=4096) if ref.relative_path == path)
    if len(refs) != 1:
        raise StudyAuthorityError(f"study {kind} authority is missing or ambiguous")
    reference = refs[0]
    raw = store.read(reference)
    if reference.sha256 != _sha256(raw):
        raise StudyAuthorityError(f"study {kind} reference differs from its bytes")
    return reference, raw


def _preflight_for_manifest(store: StudyStoreV1, ledger: StudyLedgerV1, arm: StudyArmV1) -> tuple[ArtifactRefV5, FixturePreflightV1]:
    reference = ledger.manifest.primary_preflight_ref if arm == "primary" else ledger.manifest.withheld_preflight_ref
    stored_ref, raw = _exact_store_ref(store, kind="preflights", path=reference.relative_path)
    if stored_ref != reference:
        raise StudyAuthorityError("study preflight reference differs from the manifest")
    preflight = _canonical_decode(FixturePreflightV1, raw, "fixture preflight")
    if preflight.arm != arm or preflight.mode != ledger.manifest.mode:
        raise StudyAuthorityError("study preflight mode or arm differs from the import")
    if preflight.sha256 != reference.sha256:
        raise StudyAuthorityError("study preflight identity differs from its reference")
    return reference, preflight


def _live_request_from_store(store: StudyStoreV1, request_sha256: str) -> tuple[ArtifactRefV5, StudyCallRequestV1]:
    _digest(request_sha256, "live request identity")
    reference, raw = _exact_store_ref(
        store,
        kind="requests",
        path=f"adapter-blobs/study-v1-requests/{request_sha256}.bin",
    )
    request = _canonical_decode(StudyCallRequestV1, raw, "study request")
    if request.sha256 != request_sha256 or reference.sha256 != _sha256(raw):
        raise StudyAuthorityError("persisted L identity differs from its request bytes")
    return reference, request


def _configuration_ids(schema_json: bytes) -> tuple[str, ...]:
    value = _strict_json(schema_json, "study response schema")
    try:
        ids = value["properties"]["drafts"]["items"]["properties"]["configuration_id"]["enum"]  # type: ignore[index]
    except (KeyError, TypeError) as exc:
        raise StudyAuthorityError("study response schema has no configuration vocabulary") from exc
    if type(ids) is not list or not ids or any(type(item) is not str for item in ids) or len(set(ids)) != len(ids):
        raise StudyAuthorityError("study response schema configuration vocabulary is invalid")
    return tuple(ids)


def _live_call(request: StudyCallRequestV1) -> RoleCallKeyV5:
    return RoleCallKeyV5(
        campaign_id=request.study_id,
        round_index=2,
        role="investigator",
        role_position=1,
        attempt_kind="primary",
        attempt_index=1,
        request_sha256=request.sha256,
    )


def _fixture_manifest_reference(repository: LocalArtifactRepositoryV5) -> ArtifactRefV5:
    """Discover exactly one authenticated provider-free fixture manifest edge."""

    candidates: list[ArtifactRefV5] = []
    try:
        root = repository.root.resolve(strict=True)
        for relative_path in _fixture_manifest_entries(root):
            raw = repository._read_relative(relative_path)
            reference = ArtifactRefV5(relative_path, _sha256(raw))
            authenticated_bytes = repository.authenticate_exact(reference)
            if authenticated_bytes.content != raw:
                raise StudyAuthorityError("fixture campaign manifest changed during authentication")
            try:
                manifest = authenticate_campaign_manifest_v5(
                    repository=repository,
                    manifest_ref=reference,
                )
            except (ArtifactRepositoryFailureV5, OSError, ValueError):
                continue
            if manifest.manifest.provider is None and manifest.manifest.pit_data_scope == "production":
                candidates.append(reference)
    except (ArtifactRepositoryFailureV5, OSError, ValueError) as exc:
        raise StudyAuthorityError("fixture campaign manifest is unavailable") from exc
    if len(candidates) != 1:
        raise StudyAuthorityError("fixture campaign manifest authority is ambiguous or unavailable")
    return candidates[0]


def _authenticate_fixture_manifest(
    *,
    repository: LocalArtifactRepositoryV5,
    expected_ref: ArtifactRefV5 | None = None,
    expected_manifest: AuthenticatedCampaignManifestV5 | None = None,
) -> AuthenticatedCampaignManifestV5:
    """Authenticate the actual fixture campaign graph at its fixed edge."""

    discovered = _fixture_manifest_reference(repository)
    if expected_ref is not None and discovered != expected_ref:
        raise StudyAuthorityError("fixture campaign manifest differs from its persisted authority")
    try:
        authenticated = authenticate_campaign_manifest_v5(
            repository=repository,
            manifest_ref=discovered,
        )
    except (ArtifactRepositoryFailureV5, OSError, ValueError) as exc:
        raise StudyAuthorityError("fixture campaign manifest could not be authenticated") from exc
    if (
        authenticated.manifest_ref != discovered
        or authenticated.manifest.provider is not None
        or authenticated.manifest.pit_data_scope != "production"
    ):
        raise StudyAuthorityError("fixture campaign manifest is not the provider-free production authority")
    if expected_manifest is not None and authenticated != expected_manifest:
        raise StudyAuthorityError("supplied fixture manifest differs from its persisted authority")
    return authenticated


def _authenticate_fixture_authority(
    *,
    repository: LocalArtifactRepositoryV5,
    preflight: FixturePreflightV1,
    request: RoleRequestV5,
    expected_manifest_ref: ArtifactRefV5 | None = None,
    expected_manifest: AuthenticatedCampaignManifestV5 | None = None,
    expected_fixture_sha256: str | None = None,
) -> AuthenticatedCampaignManifestV5:
    """Authenticate the actual fixture manifest and the complete F authority."""

    if repository.root_identity_sha256 != preflight.fixture_root_identity_sha256:
        raise StudyAuthorityError("fixture replay repository root differs from F")
    authenticated_manifest = _authenticate_fixture_manifest(
        repository=repository,
        expected_ref=expected_manifest_ref,
        expected_manifest=expected_manifest,
    )
    if expected_fixture_sha256 is not None and authenticated_manifest.manifest_ref.sha256 != expected_fixture_sha256:
        raise StudyAuthorityError("study manifest fixture digest differs from its actual fixture authority")
    if request.expected_binding.discovery_plan_sha256 != authenticated_manifest.panel_plan.discovery_plan_sha256:
        raise StudyAuthorityError("fixture request discovery plan differs from its campaign manifest")
    author_policy_paths = (
        tuple(item.path for item in request.role_input.editable_sources)
        if request.role == "author"
        else ()
    )
    try:
        manifest_schema = role_schema_authority_from_manifest_v5(
            role=request.role,
            manifest=authenticated_manifest.manifest,
            author_policy_paths=author_policy_paths,
        )
    except (TypeError, ValueError) as exc:
        raise StudyAuthorityError("fixture request schema authority is invalid") from exc
    if request.schema_authority != manifest_schema:
        raise StudyAuthorityError("fixture request schema differs from its campaign manifest")
    if preflight.call.campaign_id != authenticated_manifest.manifest.campaign_id:
        raise StudyAuthorityError("fixture call campaign differs from its campaign manifest")
    # The reduction is deliberately shared with both the public import verifier
    # and the replay invoker.  It authenticates baseline or frozen historical
    # archive ancestry and each selected record's persisted source bundle.
    StudyImportInvokerV1._verify_fixture_parent(
        repository=repository,
        preflight=preflight,
        request=request,
        authenticated_manifest=authenticated_manifest,
    )
    return authenticated_manifest


def _ensure_live_terminal(terminal: AuthenticatedStudyTerminalV1, ledger: StudyLedgerV1) -> None:
    item = terminal.terminal
    if ledger.manifest.mode not in {"live_study", "offline_fixture"} or item.arm not in {"primary", "withheld"}:
        raise StudyAuthorityError("only an admitted study terminal can produce an import")
    if item.failure_code is not None or item.parsed_ref is None:
        raise StudyAuthorityError("failed or non-importable study terminal cannot produce an import")
    if item.response_ref.relative_path != f"adapter-blobs/study-v1-raw-responses/{item.request_sha256}.bin":
        raise StudyAuthorityError("study terminal response is not an importable raw response")
    if item.usage.external_attempt_count != 1 or not item.usage.request_started or not item.usage.response_received:
        raise StudyAuthorityError("study import requires one accounted live provider attempt")


def _prepare_materials(
    *,
    store: StudyStoreV1,
    ledger: StudyLedgerV1,
    preflight: FixturePreflightV1,
    terminal: AuthenticatedStudyTerminalV1,
) -> _ImportMaterials:
    if type(store) is not StudyStoreV1 or type(ledger) is not StudyLedgerV1:
        raise StudyContractError("study import store or ledger is invalid")
    if type(preflight) is not FixturePreflightV1 or type(terminal) is not AuthenticatedStudyTerminalV1:
        raise StudyContractError("study import preflight or terminal is invalid")
    if ledger.store.repository.root_identity_sha256 != store.repository.root_identity_sha256:
        raise StudyAuthorityError("study import store differs from the ledger authority root")
    # The wrapper is not authority by itself.  Re-read and rederive the full
    # terminal chain before trusting any response, usage, or identity fields.
    try:
        verified_terminal = ledger.verify_terminal(terminal.reference)
    except StudyAuthorityError:
        raise
    except (StudyContractError, TypeError, ValueError, ArithmeticError) as exc:
        raise StudyAuthorityError("study terminal authority could not be reverified") from exc
    if verified_terminal != terminal:
        raise StudyAuthorityError("supplied study terminal differs from the authenticated journal terminal")
    _ensure_live_terminal(verified_terminal, ledger)
    arm = verified_terminal.arm
    preflight_ref, persisted_preflight = _preflight_for_manifest(store, ledger, arm)
    if persisted_preflight != preflight:
        raise StudyAuthorityError("supplied preflight differs from the persisted frozen preflight")
    fixture_request = authenticate_fixture_preflight_v1(persisted_preflight, require_current=False)
    if fixture_request.sha256 != persisted_preflight.request_sha256:
        raise StudyAuthorityError("authenticated F identity differs from the preflight")
    try:
        fixture_repository = LocalArtifactRepositoryV5(Path(persisted_preflight.fixture_root_locator))
    except (OSError, ValueError) as exc:
        raise StudyAuthorityError("fixture repository is unavailable") from exc
    fixture_manifest = _authenticate_fixture_authority(
        repository=fixture_repository,
        preflight=persisted_preflight,
        request=fixture_request,
        expected_fixture_sha256=ledger.manifest.fixture_sha256,
    )
    live_request_ref, live_request = _live_request_from_store(store, verified_terminal.request_sha256)
    if live_request.study_id != ledger.study_id or live_request.arm != arm:
        raise StudyAuthorityError("persisted L identity differs from the terminal")
    if live_request.preflight_ref != preflight_ref or live_request.fixture_request_sha256 != fixture_request.sha256:
        raise StudyAuthorityError("L does not bind the exact persisted F")
    if persisted_preflight.call != RoleCallKeyV5(
        campaign_id=persisted_preflight.call.campaign_id,
        round_index=2,
        role="investigator",
        role_position=1,
        attempt_kind="primary",
        attempt_index=1,
        request_sha256=fixture_request.sha256,
    ):
        raise StudyAuthorityError("fixture call identity is not the investigator primary slot")
    raw_response = store.read(verified_terminal.terminal.response_ref)
    if verified_terminal.terminal.response_ref.sha256 != _sha256(raw_response):
        raise StudyAuthorityError("study raw response reference differs from its bytes")
    parsed_ref = verified_terminal.terminal.parsed_ref
    if parsed_ref is None:
        raise StudyAuthorityError("study terminal has no parsed envelope")
    parsed_raw = store.read(parsed_ref)
    if parsed_ref.sha256 != _sha256(parsed_raw):
        raise StudyAuthorityError("study parsed envelope reference differs from its bytes")
    parsed_response = parse_study_response_v1(
        raw=raw_response,
        fixture_request=fixture_request,
        configuration_ids=_configuration_ids(live_request.schema_json),
    )
    stored_parsed = _canonical_decode(StudyResponseV1, parsed_raw, "study parsed response")
    if stored_parsed != parsed_response:
        raise StudyAuthorityError("persisted parsed envelope differs from the exact raw response")
    translated = canonical_json_bytes_v5(
        {
            "binding": fixture_request.expected_binding.to_primitive(),
            "artifact": canonical_primitive_v5(parsed_response.artifact),
        }
    )
    ordinary_artifact = canonical_json_bytes_v5(canonical_primitive_v5(parsed_response.artifact))
    legacy_artifact = parse_and_bind_role_artifact(
        request=fixture_request,
        response_text=translated.decode("utf-8"),
    )
    if legacy_artifact != parsed_response.artifact:
        raise StudyAuthorityError("ordinary artifact translation is not lossless")
    return _ImportMaterials(
        fixture_request=fixture_request,
        live_request=live_request,
        live_request_ref=live_request_ref,
        fixture_preflight=persisted_preflight,
        fixture_preflight_ref=preflight_ref,
        fixture_manifest_ref=fixture_manifest.manifest_ref,
        terminal=verified_terminal,
        raw_response=raw_response,
        parsed_response=parsed_response,
        translated=translated,
        ordinary_artifact=ordinary_artifact,
        drafts=parsed_response.drafts,
    )


def _record_from_materials(*, store: StudyStoreV1, ledger: StudyLedgerV1, materials: _ImportMaterials) -> StudyImportV1:
    terminal_ref = materials.terminal.reference
    storage_key = terminal_ref.sha256
    translated_ref = store.put(
        kind="import-translations",
        key=storage_key,
        content=materials.translated,
    )
    ordinary_ref = store.put(
        kind="import-artifacts",
        key=storage_key,
        content=materials.ordinary_artifact,
    )
    draft_refs = tuple(
        store.put(
            kind="import-drafts",
            key=f"{storage_key}-{index}",
            content=draft.canonical_bytes(),
        )
        for index, draft in enumerate(materials.drafts)
    )
    record = StudyImportV1(
        study_id=ledger.study_id,
        arm=materials.fixture_preflight.arm,
        mode=ledger.manifest.mode,
        preflight_ref=materials.fixture_preflight_ref,
        terminal_ref=terminal_ref,
        fixture_call=materials.fixture_preflight.call,
        live_call=_live_call(materials.live_request),
        fixture_request_ref=materials.fixture_preflight.request_ref,
        fixture_request_sha256=materials.fixture_request.sha256,
        live_request_ref=materials.live_request_ref,
        live_request_sha256=materials.live_request.sha256,
        original_response_ref=materials.terminal.terminal.response_ref,
        original_response_sha256=_sha256(materials.raw_response),
        parsed_envelope_ref=materials.terminal.terminal.parsed_ref,  # type: ignore[arg-type]
        parsed_envelope_sha256=materials.terminal.terminal.parsed_ref.sha256,  # type: ignore[union-attr]
        translated_ref=translated_ref,
        translated_sha256=_sha256(materials.translated),
        ordinary_artifact_ref=ordinary_ref,
        ordinary_artifact_sha256=_sha256(materials.ordinary_artifact),
        draft_refs=draft_refs,
        draft_hashes=tuple(ref.sha256 for ref in draft_refs),
        fixture_root_identity_sha256=materials.fixture_preflight.fixture_root_identity_sha256,
        fixture_manifest_ref=materials.fixture_manifest_ref,
        ledger_root_identity_sha256=store.repository.root_identity_sha256,
        manifest_sha256=ledger.manifest.sha256,
        grant_sha256=ledger.grant.sha256,
        audit_domain=ledger.grant.audit_domain,
        response_schema_sha256=materials.live_request.schema_sha256,
        parser_sha256=ledger.manifest.parser_sha256,
    )
    store.put(kind="imports", key=record.storage_key, content=record.canonical_bytes())
    return record


def create_study_import_v1(
    *,
    store: StudyStoreV1,
    ledger: StudyLedgerV1,
    preflight: FixturePreflightV1,
    terminal: AuthenticatedStudyTerminalV1,
) -> StudyImportV1:
    """Create and persist one immutable import after terminal authentication."""

    materials = _prepare_materials(store=store, ledger=ledger, preflight=preflight, terminal=terminal)
    record = _record_from_materials(store=store, ledger=ledger, materials=materials)
    # Creation ends with the same read-only path used by recovery.  This makes
    # a partially interrupted translation idempotent and catches any conflict
    # between the bytes just persisted and the complete authority graph.
    authenticate_study_import_v1(
        store=store,
        ledger=ledger,
        record=record,
        fixture_repository=LocalArtifactRepositoryV5(Path(preflight.fixture_root_locator)),
        request=materials.fixture_request,
        call=materials.fixture_preflight.call,
    )
    return record


def _load_persisted_record(store: StudyStoreV1, record: StudyImportV1) -> StudyImportV1:
    reference, raw = _exact_store_ref(
        store,
        kind="imports",
        path=f"adapter-blobs/study-v1-imports/{record.storage_key}.bin",
    )
    try:
        parsed = StudyImportV1.from_canonical_json(raw)
    except StudyContractError as exc:
        raise StudyAuthorityError("study import record is not a valid canonical authority") from exc
    if reference.sha256 != _sha256(raw) or parsed != record:
        raise StudyAuthorityError("study import record differs from its persisted authority")
    return parsed


def _verify_import_material(
    *,
    store: StudyStoreV1,
    ledger: StudyLedgerV1,
    record: StudyImportV1,
    fixture_repository: LocalArtifactRepositoryV5,
    request: RoleRequestV5,
    call: RoleCallKeyV5,
) -> bytes:
    if type(fixture_repository) is not LocalArtifactRepositoryV5:
        raise StudyContractError("fixture repository is invalid")
    if ledger.store.repository.root_identity_sha256 != store.repository.root_identity_sha256:
        raise StudyAuthorityError("study import store differs from the ledger authority root")
    _load_persisted_record(store, record)
    if record.mode != ledger.manifest.mode or record.mode not in {"live_study", "offline_fixture"}:
        raise StudyAuthorityError("study import mode is not the admitted study mode")
    if record.study_id != ledger.study_id or record.manifest_sha256 != ledger.manifest.sha256:
        raise StudyAuthorityError("study import manifest identity differs from the ledger")
    if record.grant_sha256 != ledger.grant.sha256 or record.audit_domain != ledger.grant.audit_domain:
        raise StudyAuthorityError("study import grant or audit identity differs from the ledger")
    if record.ledger_root_identity_sha256 != store.repository.root_identity_sha256:
        raise StudyAuthorityError("study import ledger root identity differs")
    try:
        terminal = ledger.verify_terminal(record.terminal_ref)
    except StudyAuthorityError:
        raise
    except (StudyContractError, TypeError, ValueError, ArithmeticError) as exc:
        raise StudyAuthorityError("study terminal authority could not be reverified") from exc
    if terminal.study_id != record.study_id or terminal.arm != record.arm:
        raise StudyAuthorityError("study import terminal study or arm differs")
    _ensure_live_terminal(terminal, ledger)
    if terminal.terminal.request_sha256 != record.live_request_sha256:
        raise StudyAuthorityError("study import terminal does not bind L")
    preflight_ref, preflight = _preflight_for_manifest(store, ledger, record.arm)
    if preflight_ref != record.preflight_ref or preflight.mode != record.mode:
        raise StudyAuthorityError("study import preflight authority differs")
    if preflight.fixture_root_identity_sha256 != record.fixture_root_identity_sha256:
        raise StudyAuthorityError("study import fixture root identity differs")
    if fixture_repository.root_identity_sha256 != preflight.fixture_root_identity_sha256:
        raise StudyAuthorityError("fixture replay repository root differs from F")
    authenticated_fixture_request = authenticate_fixture_preflight_v1(preflight, require_current=False)
    _authenticate_fixture_authority(
        repository=fixture_repository,
        preflight=preflight,
        request=authenticated_fixture_request,
        expected_manifest_ref=record.fixture_manifest_ref,
        expected_fixture_sha256=ledger.manifest.fixture_sha256,
    )
    if authenticated_fixture_request != request or request.sha256 != record.fixture_request_sha256:
        raise StudyAuthorityError("supplied fixture request differs from the authenticated F")
    if call != record.fixture_call or call != preflight.call:
        raise StudyAuthorityError("supplied fixture call differs from the authenticated F call")
    if record.fixture_request_ref != preflight.request_ref:
        raise StudyAuthorityError("study import fixture request reference differs from F")
    live_request_ref, live_request = _live_request_from_store(store, record.live_request_sha256)
    if live_request_ref != record.live_request_ref:
        raise StudyAuthorityError("study import L reference differs from persisted L")
    expected_live_call = _live_call(live_request)
    if record.live_call != expected_live_call:
        raise StudyAuthorityError("study import live call identity differs from L")
    if live_request.fixture_request_sha256 != request.sha256 or live_request.preflight_ref != preflight_ref:
        raise StudyAuthorityError("L does not bind the exact F and preflight")
    if record.response_schema_sha256 != live_request.schema_sha256:
        raise StudyAuthorityError("study import schema identity differs from L")
    if record.parser_sha256 != ledger.manifest.parser_sha256:
        raise StudyAuthorityError("study import parser identity differs from the manifest")
    raw_response = store.read(terminal.terminal.response_ref)
    if terminal.terminal.response_ref != record.original_response_ref or _sha256(raw_response) != record.original_response_sha256:
        raise StudyAuthorityError("study import raw response identity differs")
    if terminal.terminal.parsed_ref != record.parsed_envelope_ref:
        raise StudyAuthorityError("study import parsed envelope reference differs from terminal")
    parsed_raw = store.read(record.parsed_envelope_ref)
    if _sha256(parsed_raw) != record.parsed_envelope_sha256:
        raise StudyAuthorityError("study import parsed envelope hash differs")
    parsed = parse_study_response_v1(
        raw=raw_response,
        fixture_request=request,
        configuration_ids=_configuration_ids(live_request.schema_json),
    )
    persisted_parsed = _canonical_decode(StudyResponseV1, parsed_raw, "study parsed response")
    if persisted_parsed != parsed:
        raise StudyAuthorityError("study import parsed envelope differs from raw L response")
    translated = store.read(record.translated_ref)
    if _sha256(translated) != record.translated_sha256:
        raise StudyAuthorityError("study import translated response hash differs")
    try:
        translated_text = translated.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StudyAuthorityError("study import translated response is not UTF-8") from exc
    translated_artifact = parse_and_bind_role_artifact(request=request, response_text=translated_text)
    if translated_artifact != parsed.artifact:
        raise StudyAuthorityError("study import T rewrites the ordinary investigator artifact")
    expected_translated = canonical_json_bytes_v5(
        {
            "binding": request.expected_binding.to_primitive(),
            "artifact": canonical_primitive_v5(parsed.artifact),
        }
    )
    if translated != expected_translated:
        raise StudyAuthorityError("study import T bytes differ from the lossless projection")
    ordinary = store.read(record.ordinary_artifact_ref)
    expected_ordinary = canonical_json_bytes_v5(canonical_primitive_v5(parsed.artifact))
    if _sha256(ordinary) != record.ordinary_artifact_sha256 or ordinary != expected_ordinary:
        raise StudyAuthorityError("study import ordinary artifact bytes differ")
    if record.draft_refs and len(record.draft_refs) != len(parsed.drafts):
        raise StudyAuthorityError("study import draft count differs from the parsed response")
    persisted_drafts: list[ExperimentDraftV1] = []
    for reference, expected in zip(record.draft_refs, parsed.drafts, strict=True):
        raw = store.read(reference)
        if reference.sha256 != _sha256(raw):
            raise StudyAuthorityError("study import draft reference differs from its bytes")
        draft = _canonical_decode(ExperimentDraftV1, raw, "study import draft")
        if draft != expected:
            raise StudyAuthorityError("study import draft differs from the parsed response")
        persisted_drafts.append(draft)
    return translated


def authenticate_study_import_v1(
    *,
    store: StudyStoreV1,
    ledger: StudyLedgerV1,
    record: StudyImportV1,
    fixture_repository: LocalArtifactRepositoryV5,
    request: RoleRequestV5,
    call: RoleCallKeyV5,
) -> bytes:
    """Authenticate every import edge and return only the exact T bytes."""

    if type(record) is not StudyImportV1 or type(request) is not RoleRequestV5 or type(call) is not RoleCallKeyV5:
        raise StudyContractError("study import authentication inputs are invalid")
    return _verify_import_material(
        store=store,
        ledger=ledger,
        record=record,
        fixture_repository=fixture_repository,
        request=request,
        call=call,
    )


def verify_imported_package_v1(
    *,
    package: RoleInvocationPackageV5,
    record: StudyImportV1,
    store: StudyStoreV1,
    ledger: StudyLedgerV1,
    fixture_repository: LocalArtifactRepositoryV5,
) -> None:
    """Independently verify an already-completed fixture package's import chain."""

    if (
        type(package) is not RoleInvocationPackageV5
        or type(record) is not StudyImportV1
        or type(store) is not StudyStoreV1
        or type(ledger) is not StudyLedgerV1
        or type(fixture_repository) is not LocalArtifactRepositoryV5
    ):
        raise StudyContractError("imported role package is invalid")
    if (
        not package.accepted
        or type(package.terminal_authority) is not FixtureRoleTerminalAuthorityV5
        or package.terminal_authority.fixture_id != "study-import-v1"
    ):
        raise StudyAuthorityError("imported package is not a fixture investigator completion")
    if package.call != record.fixture_call or package.request.sha256 != record.fixture_request_sha256:
        raise StudyAuthorityError("imported package call or request differs from the import")
    if package.attempt.usage.external_attempt_count != 0:
        raise StudyAuthorityError("fixture replay package carries external usage")
    translated = authenticate_study_import_v1(
        store=store,
        ledger=ledger,
        record=record,
        fixture_repository=fixture_repository,
        request=package.request,
        call=package.call,
    )
    artifact = parse_and_bind_role_artifact(request=package.request, response_text=translated.decode("utf-8"))
    if package.artifact != artifact:
        raise StudyAuthorityError("fixture package artifact differs from imported T")
    if package.attempt.response_sha256 != canonical_role_response_sha256_v5(translated.decode("utf-8")):
        raise StudyAuthorityError("fixture package response hash differs from imported T")
    if package.attempt.artifact_sha256 != hashlib.sha256(canonical_json_bytes_v5(canonical_primitive_v5(artifact))).hexdigest():
        raise StudyAuthorityError("fixture package artifact hash differs from imported artifact")
    authority = package.terminal_authority
    if (
        authority.call_key_sha256 != package.call.sha256
        or authority.request_sha256 != package.request.sha256
        or authority.attempt_facts_sha256 != package.attempt.sha256
        or authority.artifact_sha256 != package.attempt.artifact_sha256
    ):
        raise StudyAuthorityError("fixture package terminal authority differs")


class StudyImportInvokerV1:
    """Replay one exact investigator import and delegate scripted roles."""

    def __init__(
        self,
        manifest: CampaignManifestV5 | AuthenticatedCampaignManifestV5 | None = None,
        imported: StudyImportV1 | None = None,
        store: StudyStoreV1 | None = None,
        ledger: StudyLedgerV1 | None = None,
        scripted_role_delegate: RecoverableRoleInvokerV5 | None = None,
        *,
        fixture_manifest: CampaignManifestV5 | AuthenticatedCampaignManifestV5 | None = None,
        record: StudyImportV1 | None = None,
        delegate: RecoverableRoleInvokerV5 | None = None,
    ) -> None:
        if fixture_manifest is not None:
            if manifest is not None and manifest != fixture_manifest:
                raise StudyAuthorityError("fixture manifests conflict")
            manifest = fixture_manifest
        if record is not None:
            if imported is not None and imported != record:
                raise StudyAuthorityError("study import records conflict")
            imported = record
        if delegate is not None:
            if scripted_role_delegate is not None and scripted_role_delegate is not delegate:
                raise StudyAuthorityError("scripted role delegates conflict")
            scripted_role_delegate = delegate
        if type(manifest) not in {CampaignManifestV5, AuthenticatedCampaignManifestV5} or type(imported) is not StudyImportV1:
            raise StudyContractError("study import invoker authority is invalid")
        campaign_manifest = manifest.manifest if type(manifest) is AuthenticatedCampaignManifestV5 else manifest
        if campaign_manifest.provider is not None or campaign_manifest.pit_data_scope != "production":
            raise StudyAuthorityError("study import invoker requires the provider-free fixture manifest")
        if type(store) is not StudyStoreV1 or type(ledger) is not StudyLedgerV1:
            raise StudyContractError("study import invoker store or ledger is invalid")
        if imported.study_id != ledger.study_id or imported.mode != ledger.manifest.mode:
            raise StudyAuthorityError("study import invoker authority differs from the ledger")
        if not isinstance(scripted_role_delegate, RecoverableRoleInvokerV5):
            raise StudyContractError("study import invoker requires a recoverable scripted-role delegate")
        self._manifest = campaign_manifest
        self._authenticated_manifest = manifest
        self._record = imported
        self._store = store
        self._ledger = ledger
        self._delegate = scripted_role_delegate

    def _fixture_authority(
        self,
    ) -> tuple[
        LocalArtifactRepositoryV5,
        FixturePreflightV1,
        RoleRequestV5,
        RoleCallKeyV5,
        AuthenticatedCampaignManifestV5,
    ]:
        reference, preflight = _preflight_for_manifest(self._store, self._ledger, self._record.arm)
        if reference != self._record.preflight_ref or preflight.request_ref != self._record.fixture_request_ref:
            raise StudyAuthorityError("study import invoker preflight differs from its import")
        try:
            repository = LocalArtifactRepositoryV5(Path(preflight.fixture_root_locator))
        except (OSError, ValueError) as exc:
            raise StudyAuthorityError("study import invoker fixture root is unavailable") from exc
        request = authenticate_fixture_preflight_v1(preflight, require_current=False)
        if request.sha256 != self._record.fixture_request_sha256 or preflight.call != self._record.fixture_call:
            raise StudyAuthorityError("study import invoker F differs from its import")
        expected_manifest = (
            self._authenticated_manifest
            if type(self._authenticated_manifest) is AuthenticatedCampaignManifestV5
            else None
        )
        expected_manifest_ref = self._authenticated_manifest.manifest_ref if expected_manifest is not None else None
        authenticated_manifest = _authenticate_fixture_authority(
            repository=repository,
            preflight=preflight,
            request=request,
            expected_manifest_ref=expected_manifest_ref,
            expected_manifest=expected_manifest,
            expected_fixture_sha256=self._ledger.manifest.fixture_sha256,
        )
        if authenticated_manifest.manifest != self._manifest:
            raise StudyAuthorityError("supplied fixture manifest differs from its persisted authority")
        return repository, preflight, request, preflight.call, authenticated_manifest

    def _fixture_request(self) -> tuple[LocalArtifactRepositoryV5, RoleRequestV5, RoleCallKeyV5]:
        repository, _preflight, request, call, _manifest = self._fixture_authority()
        return repository, request, call

    @staticmethod
    def _verify_fixture_parent(
        *,
        repository: LocalArtifactRepositoryV5,
        preflight: FixturePreflightV1,
        request: RoleRequestV5,
        authenticated_manifest: AuthenticatedCampaignManifestV5,
    ) -> None:
        """Bind F's selected parent to baseline or the frozen archive ancestry.

        Round two may select an archived parent produced after the campaign
        baseline.  The fixture manifest authenticates the baseline, while the
        frozen preflight checkpoint authenticates later archive records.  A
        matching digest in F is accepted only through one of those authorities.
        """

        parent_revision_sha256 = request.expected_binding.parent_revision_sha256
        if parent_revision_sha256 == authenticated_manifest.baseline_policy_revision.sha256:
            return
        try:
            checkpoint_value = _strict_json(preflight.checkpoint_bytes, "fixture checkpoint")
            if type(checkpoint_value) is not dict or set(checkpoint_value) != {
                "schema_version",
                "artifact_type",
                "generation",
                "archive_sha256",
                "record_refs",
            }:
                raise StudyAuthorityError("fixture checkpoint authority is invalid")
            record_refs = checkpoint_value["record_refs"]
            if type(record_refs) is not list:
                raise StudyAuthorityError("fixture checkpoint record references are invalid")
            checkpoint = RepositoryCheckpointV5(
                generation=checkpoint_value["generation"],  # type: ignore[arg-type]
                archive_sha256=checkpoint_value["archive_sha256"],  # type: ignore[arg-type]
                record_refs=tuple(_ref_from_primitive(item, "fixture checkpoint record ref") for item in record_refs),
            )
            if (
                checkpoint.to_primitive() != checkpoint_value
                or checkpoint.archive_sha256 != preflight.snapshot_ref.sha256
            ):
                raise StudyAuthorityError("fixture checkpoint does not bind its frozen archive")

            snapshot_value = _strict_json(preflight.snapshot_bytes, "fixture archive snapshot")
            if type(snapshot_value) is not dict or set(snapshot_value) != {
                "schema_version",
                "artifact_type",
                "generation",
                "record_refs",
                "projection",
            }:
                raise StudyAuthorityError("fixture archive snapshot authority is invalid")
            snapshot_records = snapshot_value["record_refs"]
            if type(snapshot_records) is not list:
                raise StudyAuthorityError("fixture archive record references are invalid")
            snapshot = ArchiveSnapshotV5(
                generation=snapshot_value["generation"],  # type: ignore[arg-type]
                record_refs=tuple(_ref_from_primitive(item, "fixture archive record ref") for item in snapshot_records),
                projection=snapshot_value["projection"],
            )
            if (
                snapshot.to_primitive() != snapshot_value
                or snapshot.generation != checkpoint.generation
                or snapshot.record_refs != checkpoint.record_refs
            ):
                raise StudyAuthorityError("fixture checkpoint/archive graph is inconsistent")

            stored_records: list[StoredExperimentRecordV5] = []
            authorities: list[ArchiveRecordAuthorityV5] = []
            for reference in checkpoint.record_refs:
                candidate = repository.load_experiment(reference)
                stored = StoredExperimentRecordV5(reference=reference, record=candidate)
                stored_records.append(stored)
                if candidate.policy_revision is None:
                    continue
                source_refs = tuple(
                    item
                    for item in candidate.artifact_refs
                    if item.relative_path == f"adapter-state/policy-source/{candidate.policy_revision.sha256}.json"
                )
                if len(source_refs) != 1:
                    raise StudyAuthorityError("fixture archive record lacks its exact source authority")
                source_ref = source_refs[0]
                source_bundle = repository.load_typed_artifact(source_ref, value_type=SourceBundleV5)
                authorities.append(
                    ArchiveRecordAuthorityV5(
                        experiment_id=candidate.experiment_id,
                        experiment_record_ref=reference,
                        source_bundle=source_bundle,
                        source_bundle_ref=source_ref,
                    )
                )
            reducer = CandidateArchiveReducerV5(
                discovery_plan=authenticated_manifest.panel_plan,
                evaluator_contract=authenticated_manifest.evaluator_contract,
                target=authenticated_manifest.manifest.target,
                authorities=tuple(sorted(authorities, key=lambda item: item.experiment_id)),
                capacity=authenticated_manifest.manifest.search.archive_capacity,
                pit_data_scope=authenticated_manifest.manifest.pit_data_scope,
                semantic_mode=authenticated_manifest.manifest.semantic_mode,
            )
            state = reduce_experiment_journal_v5(
                tuple(item.record for item in stored_records),
                reducer,
            )
            expected_snapshot = ArchiveSnapshotV5(
                generation=checkpoint.generation,
                record_refs=checkpoint.record_refs,
                projection=reducer.to_primitive(state),
            )
            if canonical_json_bytes_v5(expected_snapshot.to_primitive()) != canonical_json_bytes_v5(snapshot.to_primitive()):
                raise StudyAuthorityError("fixture archive projection is not independently verified")
            if not any(item.policy_identity_sha256 == parent_revision_sha256 for item in state.archive.entries):
                raise StudyAuthorityError("study import invoker F parent is not baseline or checkpoint-authorized")
        except StudyAuthorityError:
            raise
        except (ArtifactRepositoryFailureV5, OSError, TypeError, ValueError, ArithmeticError) as exc:
            raise StudyAuthorityError("fixture checkpoint/archive parent authority is unavailable") from exc

    def _authenticate_delegated_request(
        self,
        persisted: FreshPersistedRoleRequestV5 | ExistingPersistedRoleRequestV5,
    ) -> None:
        repository, preflight, investigator_request, investigator_call, authenticated_manifest = self._fixture_authority()
        # Delegated roles may only run after the original investigator import
        # has been reauthenticated through its complete persisted F/L graph.
        # This binds the imported record's manifest, roots, ledger identities,
        # terminal, raw/parsed response, translated T, ordinary artifact, and
        # drafts before any role-specific delegate is called.
        authenticate_study_import_v1(
            store=self._store,
            ledger=self._ledger,
            record=self._record,
            fixture_repository=repository,
            request=investigator_request,
            call=investigator_call,
        )
        self._validate_scripted_slot(persisted, manifest=authenticated_manifest.manifest)
        try:
            repository.authenticate_exact(persisted.reference)
            stored_call, stored_request = repository.load_unique_role_request_entry_by_sha256(persisted.request.sha256)
            exact_request = repository.load_role_request(persisted.reference)
        except (ArtifactRepositoryFailureV5, OSError, TypeError, ValueError) as exc:
            raise StudyAuthorityError("scripted-role request is not persisted in the authenticated fixture root") from exc
        if (
            stored_call != persisted.call
            or stored_request != persisted.request
            or exact_request != persisted.request
        ):
            raise StudyAuthorityError("scripted-role request differs from its authenticated persisted authority")
        _authenticate_fixture_authority(
            repository=repository,
            preflight=preflight,
            request=persisted.request,
            expected_manifest_ref=authenticated_manifest.manifest_ref,
            expected_manifest=authenticated_manifest,
            expected_fixture_sha256=self._ledger.manifest.fixture_sha256,
        )

    def _validate_scripted_slot(
        self,
        persisted: FreshPersistedRoleRequestV5 | ExistingPersistedRoleRequestV5,
        *,
        manifest: CampaignManifestV5 | None = None,
    ) -> None:
        call = persisted.call
        expected_position = {"author": 2, "critic": 3}.get(call.role)
        campaign_manifest = self._manifest if manifest is None else manifest
        if (
            expected_position is None
            or call.campaign_id != campaign_manifest.campaign_id
            or call.round_index != 2
            or call.role_position != expected_position
            or call.attempt_kind != "primary"
            or call.attempt_index != 1
        ):
            raise StudyAuthorityError("scripted-role request differs from the exact fixture manifest slot")

    def _replay_investigator(
        self,
        persisted: FreshPersistedRoleRequestV5 | ExistingPersistedRoleRequestV5,
    ) -> RoleInvocationPackageV5:
        repository, _preflight, request, call, _manifest = self._fixture_authority()
        if persisted.call != call or persisted.request != request or persisted.reference != self._record.fixture_request_ref:
            raise StudyAuthorityError("runtime fixture request differs from the authenticated import")
        translated = authenticate_study_import_v1(
            store=self._store,
            ledger=self._ledger,
            record=self._record,
            fixture_repository=repository,
            request=request,
            call=call,
        )
        runner = FixtureRoleRunnerV5(
            responses={"investigator": (translated.decode("utf-8"),)},
            campaign_fixture=False,
        )
        artifact = runner.invoke_once(request)
        if not runner.attempts or runner.attempts[-1].attempt_index != 1:
            raise StudyAuthorityError("fixture import replay did not preserve investigator attempt identity")
        attempt = runner.attempts[-1]
        authority = FixtureRoleTerminalAuthorityV5(
            "study-import-v1",
            call.sha256,
            request.sha256,
            attempt.sha256,
            attempt.artifact_sha256,
        )
        package = RoleInvocationPackageV5(call, request, attempt, authority, artifact)
        verify_imported_package_v1(
            package=package,
            record=self._record,
            store=self._store,
            ledger=self._ledger,
            fixture_repository=repository,
        )
        return package

    @staticmethod
    def _delegate_result(
        result: RoleInvocationPackageV5 | RoleReconciliationFailureV5,
        persisted: FreshPersistedRoleRequestV5 | ExistingPersistedRoleRequestV5,
    ) -> RoleInvocationPackageV5 | RoleReconciliationFailureV5:
        if type(result) is RoleReconciliationFailureV5:
            if result.call != persisted.call:
                raise StudyAuthorityError("scripted-role recovery failure differs from its request")
            return result
        if type(result) is not RoleInvocationPackageV5 or result.call != persisted.call or result.request != persisted.request:
            raise StudyAuthorityError("scripted-role package differs from its persisted request")
        if type(result.terminal_authority) is not FixtureRoleTerminalAuthorityV5:
            raise StudyAuthorityError("scripted-role package is not provider-free fixture authority")
        if result.attempt.usage.external_attempt_count != 0:
            raise StudyAuthorityError("scripted-role package carries external usage")
        return result

    def invoke_once(
        self,
        persisted_request: FreshPersistedRoleRequestV5,
        *,
        deadline_monotonic: float,
    ) -> RoleInvocationPackageV5:
        if type(persisted_request) is not FreshPersistedRoleRequestV5:
            raise StudyContractError("study import invocation requires a fresh persisted request")
        if type(deadline_monotonic) is not float or not math.isfinite(deadline_monotonic):
            raise StudyContractError("study import invocation deadline is invalid")
        if persisted_request.call.role == "investigator":
            return self._replay_investigator(persisted_request)
        if persisted_request.call.role not in {"author", "critic"}:
            raise StudyAuthorityError("study import invoker role is not delegated")
        self._authenticate_delegated_request(persisted_request)
        return self._delegate_result(
            self._delegate.invoke_once(persisted_request, deadline_monotonic=deadline_monotonic),
            persisted_request,
        )  # type: ignore[return-value]

    def reconcile_once(
        self,
        persisted_request: ExistingPersistedRoleRequestV5,
    ) -> RoleInvocationPackageV5 | RoleReconciliationFailureV5:
        if type(persisted_request) is not ExistingPersistedRoleRequestV5:
            raise StudyContractError("study import recovery requires an existing persisted request")
        if persisted_request.call.role == "investigator":
            return self._replay_investigator(persisted_request)
        if persisted_request.call.role not in {"author", "critic"}:
            raise StudyAuthorityError("study import invoker role is not delegated")
        self._authenticate_delegated_request(persisted_request)
        return self._delegate_result(self._delegate.reconcile_once(persisted_request), persisted_request)


__all__ = [
    "IMPORT_SCHEMA_VERSION_V1",
    "IMPORT_TRANSLATOR_VERSION_V1",
    "StudyImportV1",
    "StudyImportInvokerV1",
    "authenticate_study_import_v1",
    "create_study_import_v1",
    "verify_imported_package_v1",
]
