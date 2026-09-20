"""Immutable bounded storage for two-round study bytes."""

from __future__ import annotations

import re

from core.pit_optimizer_v5.artifacts import (
    ArtifactDigestMismatchV5,
    ArtifactMissingV5,
    ArtifactNonCanonicalV5,
    ArtifactRelocatedV5,
    ArtifactRepositoryFailureV5,
    ArtifactSchemaFailureV5,
    LocalArtifactRepositoryV5,
)
from core.pit_optimizer_v5.contracts import ArtifactRefV5

from .contracts import (
    StudyAdmissionError,
    StudyAuthorityError,
    StudyContractError,
    study_contract_bytes_v1,
)


STUDY_BLOB_MAX_BYTES_V1 = 4 * 1024 * 1024
STUDY_MAX_BLOBS_PER_NAMESPACE_V1 = 4096
STUDY_MAX_NAMESPACE_BYTES_V1 = 64 * 1024 * 1024
STUDY_NAMESPACE_QUOTA_LOCK_KEY_V1 = "namespace-quota"
_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def _component(value: object, label: str) -> str:
    if type(value) is not str or _COMPONENT_RE.fullmatch(value) is None:
        raise StudyContractError(f"{label} is invalid")
    if value.endswith((".", " ")) or ":" in value or value.split(".", 1)[0].upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }:
        raise StudyContractError(f"{label} is not portable")
    return value


def _namespace(kind: object) -> tuple[str, str]:
    safe_kind = _component(kind, "study kind")
    return f"study-v1-{safe_kind}", safe_kind


def _translate_storage_failure(exc: BaseException, message: str) -> StudyAuthorityError:
    return StudyAuthorityError(message)


class StudyStoreV1:
    """Create-only study bytes backed by the repository's safe blob API."""

    def __init__(self, repository: LocalArtifactRepositoryV5) -> None:
        if type(repository) is not LocalArtifactRepositoryV5:
            raise StudyContractError("study store requires a V5 local repository")
        self.repository = repository

    def put(self, *, kind: str, key: str, content: bytes) -> ArtifactRefV5:
        namespace, _ = _namespace(kind)
        safe_key = _component(key, "study key")
        if type(content) is not bytes:
            raise StudyContractError("study content must be immutable bytes")
        if len(content) > STUDY_BLOB_MAX_BYTES_V1:
            raise StudyAdmissionError("study blob exceeds its frozen size limit")
        try:
            with self.repository.adapter_state_transition(
                namespace=namespace,
                key=STUDY_NAMESPACE_QUOTA_LOCK_KEY_V1,
            ):
                return self._put_locked(namespace=namespace, key=safe_key, content=content)
        except (StudyAdmissionError, StudyAuthorityError):
            raise
        except (ArtifactDigestMismatchV5, ArtifactMissingV5, ArtifactNonCanonicalV5, ArtifactRelocatedV5, ArtifactSchemaFailureV5, ArtifactRepositoryFailureV5) as exc:
            raise _translate_storage_failure(exc, "study storage could not authenticate its bytes") from exc
        except (OSError, ValueError, TypeError) as exc:
            raise _translate_storage_failure(exc, "study storage operation failed") from exc

    def _put_locked(self, *, namespace: str, key: str, content: bytes) -> ArtifactRefV5:
        expected_path = f"adapter-blobs/{namespace}/{key}.bin"
        refs = self.repository.list_binary_state_refs(
            namespace=namespace,
            maximum_entries=STUDY_MAX_BLOBS_PER_NAMESPACE_V1,
            maximum_bytes=STUDY_MAX_NAMESPACE_BYTES_V1,
        )
        current = next((item for item in refs if item.relative_path == expected_path), None)
        if current is not None:
            existing = self.repository.load_binary_state(
                namespace=namespace,
                key=key,
                reference=current,
                maximum_bytes=STUDY_BLOB_MAX_BYTES_V1,
            )
            if existing != content:
                raise StudyAuthorityError("study storage slot contains conflicting bytes")
            return current
        total_bytes = 0
        for ref in refs:
            ref_key = ref.relative_path.rsplit("/", 1)[-1][:-4]
            existing = self.repository.load_binary_state(
                namespace=namespace,
                key=ref_key,
                reference=ref,
                maximum_bytes=STUDY_BLOB_MAX_BYTES_V1,
            )
            total_bytes += len(existing)
        if len(refs) >= STUDY_MAX_BLOBS_PER_NAMESPACE_V1:
            raise StudyAdmissionError("study namespace exceeds its blob count limit")
        if total_bytes + len(content) > STUDY_MAX_NAMESPACE_BYTES_V1:
            raise StudyAdmissionError("study namespace exceeds its byte limit")
        return self.repository.append_binary_state(namespace=namespace, key=key, content=content)

    def read(self, reference: ArtifactRefV5) -> bytes:
        if type(reference) is not ArtifactRefV5:
            raise StudyContractError("study reference is invalid")
        parts = reference.relative_path.split("/")
        if len(parts) != 3 or parts[0] != "adapter-blobs" or not parts[1].startswith("study-v1-"):
            raise StudyContractError("study reference is outside controller-owned namespaces")
        namespace = parts[1]
        kind = namespace.removeprefix("study-v1-")
        if not kind:
            raise StudyContractError("study reference namespace is invalid")
        filename = parts[2]
        if not filename.endswith(".bin"):
            raise StudyContractError("study reference filename is invalid")
        key = filename[:-4]
        _component(kind, "study kind")
        _component(key, "study key")
        try:
            return self.repository.load_binary_state(
                namespace=namespace,
                key=key,
                reference=reference,
                maximum_bytes=STUDY_BLOB_MAX_BYTES_V1,
            )
        except (ArtifactDigestMismatchV5, ArtifactMissingV5, ArtifactNonCanonicalV5, ArtifactRelocatedV5, ArtifactSchemaFailureV5, ArtifactRepositoryFailureV5) as exc:
            raise _translate_storage_failure(exc, "study bytes failed authentication") from exc
        except (OSError, ValueError, TypeError) as exc:
            raise _translate_storage_failure(exc, "study read failed") from exc

    def list_refs(self, *, kind: str, maximum_entries: int = STUDY_MAX_BLOBS_PER_NAMESPACE_V1) -> tuple[ArtifactRefV5, ...]:
        """Enumerate one bounded controller-owned study namespace read-only."""

        namespace, _ = _namespace(kind)
        if type(maximum_entries) is not int or maximum_entries <= 0 or maximum_entries > STUDY_MAX_BLOBS_PER_NAMESPACE_V1:
            raise StudyAdmissionError("study namespace enumeration bound is invalid")
        try:
            return self.repository.list_binary_state_refs(
                namespace=namespace,
                maximum_entries=maximum_entries,
                maximum_bytes=STUDY_MAX_NAMESPACE_BYTES_V1,
            )
        except (ArtifactDigestMismatchV5, ArtifactMissingV5, ArtifactNonCanonicalV5, ArtifactRelocatedV5, ArtifactSchemaFailureV5, ArtifactRepositoryFailureV5) as exc:
            raise _translate_storage_failure(exc, "study namespace enumeration failed") from exc
        except (OSError, ValueError, TypeError) as exc:
            raise _translate_storage_failure(exc, "study namespace enumeration failed") from exc

    def put_contract(self, *, kind: str, key: str, value: object) -> ArtifactRefV5:
        # StudyImportV1 is defined in the adapter layer rather than the closed
        # response-contract registry.  Keep its registration narrow and lazy
        # so importing the store does not create a module cycle.
        from .imports import StudyImportV1
        from .compiler import CompiledStudyExperimentV1, StudyCommitmentIndexV1, StudyDraftBindingV1
        from .contrast import CaseContrastResultV1, StudyContrastV1

        if type(value) in {
            StudyImportV1,
            StudyCommitmentIndexV1,
            StudyDraftBindingV1,
            StudyContrastV1,
            CaseContrastResultV1,
            CompiledStudyExperimentV1,
        }:
            return self.put(kind=kind, key=key, content=value.canonical_bytes())
        try:
            content = study_contract_bytes_v1(value)
        except StudyContractError:
            raise
        except (TypeError, ValueError) as exc:
            raise StudyContractError("study value is not a registered contract") from exc
        return self.put(kind=kind, key=key, content=content)


__all__ = [
    "STUDY_BLOB_MAX_BYTES_V1",
    "STUDY_MAX_BLOBS_PER_NAMESPACE_V1",
    "STUDY_MAX_NAMESPACE_BYTES_V1",
    "STUDY_NAMESPACE_QUOTA_LOCK_KEY_V1",
    "StudyStoreV1",
]
