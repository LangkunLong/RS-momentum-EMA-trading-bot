"""Authoritative compilation of one V5 two-round mechanism study.

The compiler is deliberately a graph verifier.  The caller is responsible for
authenticating the public study import, package, and saved intent before it
calls this module; this module nevertheless reloads every durable edge it
needs from the original fixture repository and the controller store.  A
replacement dataclass supplied by a caller can therefore never become study
authority merely because it has matching fields.
"""

from __future__ import annotations

from dataclasses import dataclass
import base64
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Literal, TYPE_CHECKING

from core.pit_optimizer_v5.artifacts import LocalArtifactRepositoryV5
from core.pit_optimizer_v5.contracts import (
    ArtifactRefV5,
    InvestigatorArtifactV5,
    canonical_json_bytes_v5,
)
from core.pit_optimizer_v5.candidate_ir import SourceBundleV5
from core.pit_optimizer_v5.manifest import authenticate_campaign_manifest_v5
from core.pit_optimizer_v5.mechanism_contracts import (
    MECHANISM_ALLOWED_SYMBOLS_V1,
    MechanismControlV1,
    MechanismExperimentSpecV1,
    MechanismMetricSpecV1,
    validate_mechanism_spec_hypothesis_v1,
)
from core.pit_optimizer_v5.mechanism_probes import (
    MechanismObservationCorpusV1,
    MechanismPairedCaseV1,
    build_mechanism_observation_corpus_v1,
)
from core.pit_optimizer_v5.probes import canonical_probe_json_v5
from core.pit_optimizer_v5.memory import (
    RoleCompletionPayloadV5,
    RoundIntentPayloadV5,
    round_event_payload_primitive_v5,
)
from core.pit_optimizer_v5.provider import (
    RoleRequestV5,
    decode_parsed_role_artifact_v5,
)
from core.pit_optimizer_v5.search import ParentCandidateV5
from core.strategy_policy.contracts_v3 import ExitSnapshotV3

from .contracts import (
    ExperimentDraftV1,
    StudyAdmissionError,
    StudyAuthorityError,
    StudyContractError,
    StudyPrecommitmentError,
)
from .fixtures import study_resource_budget_v1
from .imports import StudyImportV1
from .live_calls import FixturePreflightV1, authenticate_fixture_preflight_v1
from .registry import FrozenBehaviorRegistryV1, verify_registry_v1
from .store import StudyStoreV1

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from .contrast import StudyContrastV1


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_STUDY_SCHEMA_VERSION_V1 = 1


def _digest(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise StudyContractError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _text(value: object, label: str, *, maximum: int = 512) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > maximum
    ):
        raise StudyContractError(f"{label} is invalid")
    return value


def _identifier(value: object, label: str) -> str:
    text = _text(value, label, maximum=128)
    if _IDENTIFIER_RE.fullmatch(text) is None:
        raise StudyContractError(f"{label} is invalid")
    return text


def _strict_mapping(value: object, expected: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != expected:
        raise StudyContractError(f"{label} fields are invalid")
    return value


def _strict_json(raw: bytes, label: str) -> object:
    if type(raw) is not bytes:
        raise StudyAuthorityError(f"{label} bytes are invalid")

    def reject_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        parsed: dict[str, object] = {}
        for key, item in pairs:
            if key in parsed:
                raise StudyAuthorityError(f"{label} contains duplicate fields")
            parsed[key] = item
        return parsed

    def reject_constant(value: str) -> object:
        raise StudyAuthorityError(f"{label} contains a nonfinite value {value}")

    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_pairs, parse_constant=reject_constant)
    except StudyAuthorityError:
        raise
    except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StudyAuthorityError(f"{label} is not canonical JSON") from exc
    if canonical_json_bytes_v5(parsed) != raw:
        raise StudyAuthorityError(f"{label} is not canonical JSON")
    return parsed


def _ref(value: object, label: str) -> ArtifactRefV5:
    if type(value) is not dict or set(value) != {"relative_path", "sha256"}:
        raise StudyContractError(f"{label} is invalid")
    try:
        return ArtifactRefV5(value["relative_path"], value["sha256"])  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise StudyContractError(f"{label} is invalid") from exc


def _ref_primitive(value: ArtifactRefV5) -> dict[str, object]:
    return value.to_primitive()


def _bool_tuple(value: object, label: str, *, nonempty: bool = True) -> tuple[bool, ...]:
    if type(value) is not tuple or (nonempty and not value) or len(value) > 64:
        raise StudyContractError(f"{label} is invalid")
    if any(type(item) is not bool for item in value):
        raise StudyContractError(f"{label} must contain strict booleans")
    return value


def _digest_tuple(value: object, label: str, *, nonempty: bool = True) -> tuple[str, ...]:
    if type(value) is not tuple or (nonempty and not value) or len(value) > 100_000:
        raise StudyContractError(f"{label} is invalid")
    parsed = tuple(_digest(item, label) for item in value)
    if len(set(parsed)) != len(parsed):
        raise StudyContractError(f"{label} must be unique")
    return parsed


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _intent_sha256(intent: RoundIntentPayloadV5) -> str:
    return _sha256(canonical_json_bytes_v5(round_event_payload_primitive_v5(intent)))


def _decode_probe_value(value: object) -> object:
    """Decode the lossless tagged values used by corpus canonical bytes."""

    if type(value) is not list or not value or type(value[0]) is not str:
        raise StudyContractError("mechanism corpus canonical value is invalid")
    tag = value[0]
    if tag == "none":
        if len(value) != 1:
            raise StudyContractError("mechanism corpus null value is invalid")
        return None
    if tag == "bool":
        if len(value) != 2 or type(value[1]) is not bool:
            raise StudyContractError("mechanism corpus boolean value is invalid")
        return value[1]
    if tag == "int":
        if len(value) != 2 or type(value[1]) is not str:
            raise StudyContractError("mechanism corpus integer value is invalid")
        try:
            parsed = int(value[1])
        except (TypeError, ValueError) as exc:
            raise StudyContractError("mechanism corpus integer value is invalid") from exc
        if str(parsed) != value[1]:
            raise StudyContractError("mechanism corpus integer value is not canonical")
        return parsed
    if tag == "float":
        if len(value) != 2 or type(value[1]) is not str:
            raise StudyContractError("mechanism corpus float value is invalid")
        try:
            parsed = float.fromhex(value[1])
        except (TypeError, ValueError) as exc:
            raise StudyContractError("mechanism corpus float value is invalid") from exc
        if not math.isfinite(parsed) or parsed.hex() != value[1]:
            raise StudyContractError("mechanism corpus float value is not canonical")
        return parsed
    if tag == "decimal":
        if (
            len(value) != 4
            or type(value[1]) is not int
            or type(value[2]) is not str
            or type(value[3]) is not int
            or value[1] not in {0, 1}
            or not value[2]
            or any(character not in "0123456789" for character in value[2])
        ):
            raise StudyContractError("mechanism corpus decimal value is invalid")
        try:
            parsed = Decimal((value[1], tuple(int(character) for character in value[2]), value[3]))
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise StudyContractError("mechanism corpus decimal value is invalid") from exc
        if not parsed.is_finite() or parsed.as_tuple().sign != value[1] or "".join(
            str(item) for item in parsed.as_tuple().digits
        ) != value[2] or parsed.as_tuple().exponent != value[3]:
            raise StudyContractError("mechanism corpus decimal value is not canonical")
        return parsed
    if tag == "str":
        if len(value) != 2 or type(value[1]) is not str:
            raise StudyContractError("mechanism corpus string value is invalid")
        return value[1]
    if tag == "bytes":
        if len(value) != 2 or type(value[1]) is not str:
            raise StudyContractError("mechanism corpus bytes value is invalid")
        try:
            parsed = bytes.fromhex(value[1])
        except (TypeError, ValueError) as exc:
            raise StudyContractError("mechanism corpus bytes value is invalid") from exc
        if parsed.hex() != value[1]:
            raise StudyContractError("mechanism corpus bytes value is not canonical")
        return parsed
    if tag == "tuple":
        if len(value) != 2 or type(value[1]) is not list:
            raise StudyContractError("mechanism corpus tuple value is invalid")
        return tuple(_decode_probe_value(item) for item in value[1])
    raise StudyContractError("mechanism corpus canonical value uses an unsupported tag")


def _decode_observation_corpus(raw: bytes) -> MechanismObservationCorpusV1:
    parsed = _strict_json(raw, "mechanism observation corpus")
    decoded = _decode_probe_value(parsed)
    if type(decoded) is not tuple or len(decoded) != 2:
        raise StudyContractError("mechanism observation corpus shape is invalid")
    recipe_sha256, decoded_cases = decoded
    if type(recipe_sha256) is not str or type(decoded_cases) is not tuple:
        raise StudyContractError("mechanism observation corpus fields are invalid")
    cases: list[MechanismPairedCaseV1] = []
    for item in decoded_cases:
        if type(item) is not tuple or len(item) != 7:
            raise StudyContractError("mechanism observation corpus case shape is invalid")
        order, input_bytes, input_identity, converted, snapshot_bytes, snapshot_identity, applicable = item
        if type(order) is not int or type(input_bytes) is not bytes or type(snapshot_bytes) is not bytes:
            raise StudyContractError("mechanism observation corpus case encoding is invalid")
        try:
            input_value = _decode_probe_value(_strict_json(input_bytes, "mechanism case input"))
            if canonical_probe_json_v5(input_value) != input_bytes:
                raise StudyContractError("mechanism observation corpus input encoding is not canonical")
        except StudyContractError:
            raise
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise StudyContractError("mechanism observation corpus input is invalid") from exc
        snapshot_json = snapshot_bytes
        try:
            snapshot = ExitSnapshotV3.from_canonical_json(snapshot_json.decode("utf-8"))
        except (UnicodeDecodeError, TypeError, ValueError, ArithmeticError) as exc:
            raise StudyContractError("mechanism observation corpus snapshot is invalid") from exc
        try:
            cases.append(
                MechanismPairedCaseV1(
                    order=order,
                    input_value=input_value,  # type: ignore[arg-type]
                    input_canonical_bytes=input_bytes,
                    input_identity_sha256=input_identity,  # type: ignore[arg-type]
                    converted_value=converted,  # type: ignore[arg-type]
                    snapshot=snapshot,
                    snapshot_canonical_json=snapshot_json,
                    snapshot_sha256=snapshot_identity,  # type: ignore[arg-type]
                    applicable=applicable,  # type: ignore[arg-type]
                )
            )
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise StudyContractError("mechanism observation corpus case is invalid") from exc
    try:
        corpus = MechanismObservationCorpusV1(recipe_sha256=recipe_sha256, cases=tuple(cases))  # type: ignore[arg-type]
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise StudyContractError("mechanism observation corpus is invalid") from exc
    if corpus.canonical_bytes != raw:
        raise StudyContractError("mechanism observation corpus encoding is not canonical")
    return corpus


def _load_imported_draft(store: StudyStoreV1, reference: ArtifactRefV5) -> ExperimentDraftV1:
    try:
        raw = store.read(reference)
        if _sha256(raw) != reference.sha256:
            raise StudyAuthorityError("persisted study draft reference differs from its bytes")
        return ExperimentDraftV1.from_canonical_json(raw)
    except StudyAuthorityError:
        raise
    except (StudyContractError, TypeError, ValueError, ArithmeticError) as exc:
        raise StudyAuthorityError("persisted study draft is not an authenticated canonical draft") from exc


def _load_investigator_artifact(store: StudyStoreV1, imported: StudyImportV1) -> InvestigatorArtifactV5:
    try:
        raw = store.read(imported.ordinary_artifact_ref)
    except (StudyContractError, TypeError, ValueError, ArithmeticError) as exc:
        raise StudyAuthorityError("imported ordinary artifact is unavailable") from exc
    if _sha256(raw) != imported.ordinary_artifact_sha256:
        raise StudyAuthorityError("imported ordinary artifact bytes differ from its persisted identity")
    primitive = _strict_json(raw, "imported ordinary artifact")
    try:
        artifact = decode_parsed_role_artifact_v5(
            role="investigator",
            primitive={"role": "investigator", "artifact": primitive},
        )
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise StudyAuthorityError("imported ordinary artifact is not an investigator artifact") from exc
    if type(artifact) is not InvestigatorArtifactV5:
        raise StudyAuthorityError("imported ordinary artifact has the wrong role")
    return artifact


def _load_preflight(store: StudyStoreV1, imported: StudyImportV1) -> FixturePreflightV1:
    try:
        raw = store.read(imported.preflight_ref)
        if _sha256(raw) != imported.preflight_ref.sha256:
            raise StudyAuthorityError("imported preflight reference differs from its bytes")
        preflight = FixturePreflightV1.from_canonical_json(raw)
    except StudyAuthorityError:
        raise
    except (StudyContractError, TypeError, ValueError, ArithmeticError) as exc:
        raise StudyAuthorityError("imported preflight is not an authenticated canonical preflight") from exc
    if (
        preflight.arm != imported.arm
        or preflight.mode != imported.mode
        or preflight.call != imported.fixture_call
        or preflight.request_ref != imported.fixture_request_ref
        or preflight.request_sha256 != imported.fixture_request_sha256
        or preflight.fixture_root_identity_sha256 != imported.fixture_root_identity_sha256
    ):
        raise StudyAuthorityError("imported preflight does not bind the exact study import")
    return preflight


def _fixture_repository(preflight: FixturePreflightV1, imported: StudyImportV1) -> LocalArtifactRepositoryV5:
    try:
        repository = LocalArtifactRepositoryV5(Path(preflight.fixture_root_locator))
    except (OSError, ValueError) as exc:
        raise StudyAuthorityError("original fixture repository is unavailable") from exc
    if repository.root_identity_sha256 != imported.fixture_root_identity_sha256:
        raise StudyAuthorityError("original fixture repository identity differs from the preflight")
    return repository


def _load_authoritative_fixture_graph(
    *,
    preflight: FixturePreflightV1,
    imported: StudyImportV1,
) -> tuple[
    LocalArtifactRepositoryV5,
    RoundIntentPayloadV5,
    ArtifactRefV5,
    ArtifactRefV5,
    object,
    int,
    int,
    tuple[tuple[ArtifactRefV5, object, RoleRequestV5], ...],
    bool,
]:
    """Return the actual saved intent and investigator package authority.

    The returned package is intentionally typed as ``object`` here to keep the
    public compiler surface small; the exact RoleInvocationPackageV5 checks are
    performed immediately below in ``_authoritative_graph``.
    """

    repository = _fixture_repository(preflight, imported)
    try:
        manifest = authenticate_campaign_manifest_v5(
            repository=repository,
            manifest_ref=imported.fixture_manifest_ref,
        )
    except (OSError, TypeError, ValueError, ArithmeticError) as exc:
        raise StudyAuthorityError("fixture campaign manifest authority is unavailable") from exc
    if (
        manifest.manifest_ref != imported.fixture_manifest_ref
        or manifest.manifest.provider is not None
        or manifest.manifest.pit_data_scope != "production"
    ):
        raise StudyAuthorityError("fixture campaign manifest is not the provider-free production authority")

    try:
        fixture_request = authenticate_fixture_preflight_v1(preflight, require_current=False)
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise StudyAuthorityError("fixture preflight request is not authenticated") from exc
    if fixture_request.sha256 != imported.fixture_request_sha256:
        raise StudyAuthorityError("fixture request differs from the imported authority")
    if preflight.call.campaign_id != manifest.manifest.campaign_id:
        raise StudyAuthorityError("fixture call campaign differs from its manifest")

    # The fixture checkpoint is immutable for this compile.  It is checked
    # against the preflight bytes and then against the repository's current
    # authenticated checkpoint; a replacement parent cannot hide behind a
    # matching configuration label.
    checkpoint = repository.load_checkpoint()
    if checkpoint is None:
        raise StudyAuthorityError("fixture checkpoint authority is unavailable")
    checkpoint_raw = repository.authenticate_exact(preflight.checkpoint_ref).content
    if (
        _sha256(checkpoint_raw) != preflight.checkpoint_ref.sha256
        or checkpoint_raw != preflight.checkpoint_bytes
        or checkpoint.archive_sha256 != preflight.snapshot_ref.sha256
    ):
        raise StudyAuthorityError("fixture checkpoint authority differs from its preflight")
    snapshot_raw = repository.authenticate_exact(preflight.snapshot_ref).content
    if snapshot_raw != preflight.snapshot_bytes or _sha256(snapshot_raw) != preflight.snapshot_ref.sha256:
        raise StudyAuthorityError("fixture snapshot authority differs from its preflight")

    try:
        events = repository.load_round_events(
            campaign_id=preflight.call.campaign_id,
            round_index=preflight.call.round_index,
        )
    except (OSError, TypeError, ValueError, ArithmeticError) as exc:
        raise StudyAuthorityError("fixture round journal is unavailable") from exc
    intents: list[tuple[object, RoundIntentPayloadV5]] = []
    investigators: list[tuple[object, RoleCompletionPayloadV5]] = []
    try:
        event_payloads = tuple(
            (
                event,
                repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind),
            )
            for event in events
        )
    except (OSError, TypeError, ValueError, ArithmeticError) as exc:
        raise StudyAuthorityError("fixture round journal payload is unavailable") from exc
    for event, payload in event_payloads:
        if type(payload) is RoundIntentPayloadV5:
            intents.append((event, payload))
        elif type(payload) is RoleCompletionPayloadV5 and payload.role == "investigator":
            investigators.append((event, payload))
    if len(intents) != 1 or len(investigators) != 1:
        raise StudyAuthorityError("fixture journal does not contain one saved intent and investigator package")
    intent_event, intent = intents[0]
    investigator_event, completion = investigators[0]
    if (
        investigator_event.event_kind != "role_completion"
        or investigator_event.sequence >= intent_event.sequence
        or completion.campaign_id != preflight.call.campaign_id
        or completion.round_index != preflight.call.round_index
        or completion.role_position != 1
        or completion.request_sha256 != preflight.request_sha256
        or completion.request_ref != preflight.request_ref
        or completion.outcome != "accepted"
        or intent_event.payload_ref.sha256 != _intent_sha256(intent)
    ):
        raise StudyAuthorityError("fixture journal order or package identity is not immutable")
    author_completion_exists = any(
        event.event_kind == "role_completion"
        and type(payload) is RoleCompletionPayloadV5
        and payload.role == "author"
        for event, payload in event_payloads
    )
    try:
        author_requests = repository.load_authenticated_role_requests(
            campaign_id=preflight.call.campaign_id,
            round_index=preflight.call.round_index,
            role="author",
        )
    except (OSError, TypeError, ValueError, ArithmeticError) as exc:
        raise StudyAuthorityError("fixture author request authority is unavailable") from exc
    try:
        package = repository.load_role_invocation(completion)
    except (OSError, TypeError, ValueError, ArithmeticError) as exc:
        raise StudyAuthorityError("fixture investigator package is unavailable") from exc
    if (
        not package.accepted
        or package.call != preflight.call
        or package.request.sha256 != preflight.request_sha256
        or package.attempt.usage.external_attempt_count != 0
        or package.sha256 != completion.package_sha256
        or package.terminal_authority.__class__.__name__ != "FixtureRoleTerminalAuthorityV5"
        or getattr(package.terminal_authority, "fixture_id", None) != "study-import-v1"
        or type(package.artifact) is not InvestigatorArtifactV5
    ):
        raise StudyAuthorityError("fixture investigator package is not the imported authenticated package")
    return (
        repository,
        intent,
        investigator_event.payload_ref,
        intent_event.payload_ref,
        package,
        investigator_event.sequence,
        intent_event.sequence,
        author_requests,
        author_completion_exists,
    )


@dataclass(frozen=True, slots=True)
class StudyDraftBindingV1:
    """Immutable edge set joining the imported draft to the pre-author journal."""

    import_ref: ArtifactRefV5
    draft_ref: ArtifactRefV5
    hypothesis_id: str
    hypothesis_sha256: str
    fixture_role_package_ref: ArtifactRefV5
    fixture_role_package_sha256: str
    round_intent_ref: ArtifactRefV5
    round_intent_sha256: str
    parent_revision_sha256: str
    parent_source_bundle_ref: ArtifactRefV5
    parent_experiment_record_ref: ArtifactRefV5 | None
    checkpoint_ref: ArtifactRefV5
    snapshot_ref: ArtifactRefV5
    fixture_manifest_ref: ArtifactRefV5
    fixture_root_identity_sha256: str
    journal_campaign_id: str
    journal_round_index: int
    investigator_sequence: int
    intent_sequence: int
    schema_version: Literal[1] = _STUDY_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        for value, label in (
            (self.import_ref, "draft binding import reference"),
            (self.draft_ref, "draft binding draft reference"),
            (self.fixture_role_package_ref, "draft binding role-package reference"),
            (self.round_intent_ref, "draft binding round-intent reference"),
            (self.parent_source_bundle_ref, "draft binding source reference"),
            (self.checkpoint_ref, "draft binding checkpoint reference"),
            (self.snapshot_ref, "draft binding snapshot reference"),
            (self.fixture_manifest_ref, "draft binding fixture manifest reference"),
        ):
            if type(value) is not ArtifactRefV5:
                raise StudyContractError(f"{label} is invalid")
        if self.parent_experiment_record_ref is not None and type(self.parent_experiment_record_ref) is not ArtifactRefV5:
            raise StudyContractError("draft binding parent record reference is invalid")
        _identifier(self.hypothesis_id, "draft binding hypothesis ID")
        for value, label in (
            (self.hypothesis_sha256, "draft binding hypothesis identity"),
            (self.fixture_role_package_sha256, "draft binding role-package identity"),
            (self.round_intent_sha256, "draft binding round-intent identity"),
            (self.parent_revision_sha256, "draft binding parent revision"),
            (self.fixture_root_identity_sha256, "draft binding fixture root"),
        ):
            _digest(value, label)
        # The public fixture repository stores a RoleCompletionPayloadV5 at
        # this reference.  Its package_sha256 is the identity of the complete
        # invocation package reconstructed from the payload's component refs,
        # so the two digests intentionally differ.
        if self.round_intent_ref.sha256 != self.round_intent_sha256:
            raise StudyAuthorityError("draft binding round-intent reference differs from its identity")
        if type(self.journal_campaign_id) is not str or not self.journal_campaign_id.strip():
            raise StudyContractError("draft binding journal campaign is invalid")
        if type(self.journal_round_index) is not int or self.journal_round_index <= 0:
            raise StudyContractError("draft binding journal round is invalid")
        if type(self.investigator_sequence) is not int or self.investigator_sequence < 0:
            raise StudyContractError("draft binding investigator sequence is invalid")
        if type(self.intent_sequence) is not int or self.intent_sequence <= self.investigator_sequence:
            raise StudyAuthorityError("draft binding intent does not follow investigator completion")
        if self.schema_version != _STUDY_SCHEMA_VERSION_V1 or type(self.schema_version) is not int:
            raise StudyContractError("draft binding schema version is invalid")

    @property
    def import_sha256(self) -> str:
        return self.import_ref.sha256

    @property
    def draft_sha256(self) -> str:
        return self.draft_ref.sha256

    @property
    def round_intent_ref_sha256(self) -> str:
        return self.round_intent_sha256

    @property
    def storage_ref(self) -> ArtifactRefV5:
        return ArtifactRefV5(
            f"adapter-blobs/study-v1-draft-bindings/{self.sha256}.bin",
            self.sha256,
        )

    def to_primitive(self) -> dict[str, object]:
        return {
            "import_ref": _ref_primitive(self.import_ref),
            "draft_ref": _ref_primitive(self.draft_ref),
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_sha256": self.hypothesis_sha256,
            "fixture_role_package_ref": _ref_primitive(self.fixture_role_package_ref),
            "fixture_role_package_sha256": self.fixture_role_package_sha256,
            "round_intent_ref": _ref_primitive(self.round_intent_ref),
            "round_intent_sha256": self.round_intent_sha256,
            "parent_revision_sha256": self.parent_revision_sha256,
            "parent_source_bundle_ref": _ref_primitive(self.parent_source_bundle_ref),
            "parent_experiment_record_ref": (
                None if self.parent_experiment_record_ref is None else _ref_primitive(self.parent_experiment_record_ref)
            ),
            "checkpoint_ref": _ref_primitive(self.checkpoint_ref),
            "snapshot_ref": _ref_primitive(self.snapshot_ref),
            "fixture_manifest_ref": _ref_primitive(self.fixture_manifest_ref),
            "fixture_root_identity_sha256": self.fixture_root_identity_sha256,
            "journal_campaign_id": self.journal_campaign_id,
            "journal_round_index": self.journal_round_index,
            "investigator_sequence": self.investigator_sequence,
            "intent_sequence": self.intent_sequence,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes_v5(self.to_primitive())

    @property
    def sha256(self) -> str:
        return _sha256(self.canonical_bytes())

    @classmethod
    def from_primitive(cls, value: object) -> "StudyDraftBindingV1":
        fields = {
            "import_ref",
            "draft_ref",
            "hypothesis_id",
            "hypothesis_sha256",
            "fixture_role_package_ref",
            "fixture_role_package_sha256",
            "round_intent_ref",
            "round_intent_sha256",
            "parent_revision_sha256",
            "parent_source_bundle_ref",
            "parent_experiment_record_ref",
            "checkpoint_ref",
            "snapshot_ref",
            "fixture_manifest_ref",
            "fixture_root_identity_sha256",
            "journal_campaign_id",
            "journal_round_index",
            "investigator_sequence",
            "intent_sequence",
            "schema_version",
        }
        raw = _strict_mapping(value, fields, "draft binding")
        return cls(
            import_ref=_ref(raw["import_ref"], "draft binding import reference"),
            draft_ref=_ref(raw["draft_ref"], "draft binding draft reference"),
            hypothesis_id=raw["hypothesis_id"],  # type: ignore[arg-type]
            hypothesis_sha256=raw["hypothesis_sha256"],  # type: ignore[arg-type]
            fixture_role_package_ref=_ref(raw["fixture_role_package_ref"], "draft binding role-package reference"),
            fixture_role_package_sha256=raw["fixture_role_package_sha256"],  # type: ignore[arg-type]
            round_intent_ref=_ref(raw["round_intent_ref"], "draft binding round-intent reference"),
            round_intent_sha256=raw["round_intent_sha256"],  # type: ignore[arg-type]
            parent_revision_sha256=raw["parent_revision_sha256"],  # type: ignore[arg-type]
            parent_source_bundle_ref=_ref(raw["parent_source_bundle_ref"], "draft binding source reference"),
            parent_experiment_record_ref=(
                None
                if raw["parent_experiment_record_ref"] is None
                else _ref(raw["parent_experiment_record_ref"], "draft binding parent record reference")
            ),
            checkpoint_ref=_ref(raw["checkpoint_ref"], "draft binding checkpoint reference"),
            snapshot_ref=_ref(raw["snapshot_ref"], "draft binding snapshot reference"),
            fixture_manifest_ref=_ref(raw["fixture_manifest_ref"], "draft binding fixture manifest reference"),
            fixture_root_identity_sha256=raw["fixture_root_identity_sha256"],  # type: ignore[arg-type]
            journal_campaign_id=raw["journal_campaign_id"],  # type: ignore[arg-type]
            journal_round_index=raw["journal_round_index"],  # type: ignore[arg-type]
            investigator_sequence=raw["investigator_sequence"],  # type: ignore[arg-type]
            intent_sequence=raw["intent_sequence"],  # type: ignore[arg-type]
            schema_version=raw["schema_version"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_canonical_json(cls, raw: bytes | str) -> "StudyDraftBindingV1":
        encoded = raw.encode("utf-8") if type(raw) is str else raw
        value = _strict_json(encoded, "draft binding")
        parsed = cls.from_primitive(value)
        if parsed.canonical_bytes() != encoded:
            raise StudyContractError("draft binding encoding is not canonical")
        return parsed


@dataclass(frozen=True, slots=True)
class CompiledStudyExperimentV1:
    """The frozen mechanism graph returned before authoring begins."""

    spec: MechanismExperimentSpecV1
    corpus: MechanismObservationCorpusV1
    contrast: "StudyContrastV1"
    draft_binding: StudyDraftBindingV1
    selected_configuration_id: str
    schema_version: Literal[1] = _STUDY_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        if type(self.spec) is not MechanismExperimentSpecV1 or type(self.corpus) is not MechanismObservationCorpusV1:
            raise StudyContractError("compiled mechanism spec or corpus is invalid")
        # Import lazily to avoid compiler <-> contrast module import cycles.
        from .contrast import StudyContrastV1

        if type(self.contrast) is not StudyContrastV1 or type(self.draft_binding) is not StudyDraftBindingV1:
            raise StudyContractError("compiled study contrast or binding is invalid")
        _identifier(self.selected_configuration_id, "compiled configuration ID")
        if self.contrast.spec_sha256 != self.spec.sha256 or self.contrast.corpus_sha256 != self.corpus.sha256:
            raise StudyAuthorityError("compiled contrast differs from the frozen mechanism graph")
        if self.contrast.draft_binding_sha256 != self.draft_binding.sha256:
            raise StudyAuthorityError("compiled contrast differs from the frozen draft binding")
        if self.spec.hypothesis_sha256 != self.draft_binding.hypothesis_sha256:
            raise StudyAuthorityError("compiled spec differs from the frozen hypothesis")
        if self.schema_version != _STUDY_SCHEMA_VERSION_V1 or type(self.schema_version) is not int:
            raise StudyContractError("compiled experiment schema version is invalid")

    def to_primitive(self) -> dict[str, object]:
        # The corpus has a binary-like canonical probe identity.  Retain its
        # exact wire bytes in a tagged base64 field so this aggregate can be
        # persisted without teaching the generic study codec about probes.
        return {
            "spec": self.spec.to_primitive(),
            "corpus_canonical_b64": base64.b64encode(self.corpus.canonical_bytes).decode("ascii"),
            "contrast": self.contrast.to_primitive(),
            "draft_binding": self.draft_binding.to_primitive(),
            "selected_configuration_id": self.selected_configuration_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes_v5(self.to_primitive())

    @property
    def sha256(self) -> str:
        return _sha256(self.canonical_bytes())

    @classmethod
    def from_primitive(cls, value: object) -> "CompiledStudyExperimentV1":
        raw = _strict_mapping(
            value,
            {
                "spec",
                "corpus_canonical_b64",
                "contrast",
                "draft_binding",
                "selected_configuration_id",
                "schema_version",
            },
            "compiled study experiment",
        )
        encoded_corpus = raw["corpus_canonical_b64"]
        if type(encoded_corpus) is not str:
            raise StudyContractError("compiled corpus encoding is invalid")
        try:
            corpus_raw = base64.b64decode(encoded_corpus, validate=True)
        except (TypeError, ValueError) as exc:
            raise StudyContractError("compiled corpus encoding is invalid") from exc
        if base64.b64encode(corpus_raw).decode("ascii") != encoded_corpus:
            raise StudyContractError("compiled corpus encoding is not canonical")
        try:
            spec = MechanismExperimentSpecV1.from_primitive(raw["spec"])
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise StudyContractError("compiled mechanism spec is invalid") from exc
        if spec.to_primitive() != raw["spec"]:
            raise StudyContractError("compiled mechanism spec is not canonical")
        from .contrast import StudyContrastV1

        try:
            contrast = StudyContrastV1.from_primitive(raw["contrast"])
            binding = StudyDraftBindingV1.from_primitive(raw["draft_binding"])
            corpus = _decode_observation_corpus(corpus_raw)
        except StudyContractError:
            raise
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise StudyContractError("compiled study graph is invalid") from exc
        return cls(
            spec=spec,
            corpus=corpus,
            contrast=contrast,
            draft_binding=binding,
            selected_configuration_id=raw["selected_configuration_id"],  # type: ignore[arg-type]
            schema_version=raw["schema_version"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_canonical_json(cls, raw: bytes | str) -> "CompiledStudyExperimentV1":
        encoded = raw.encode("utf-8") if type(raw) is str else raw
        value = _strict_json(encoded, "compiled study experiment")
        parsed = cls.from_primitive(value)
        if parsed.canonical_bytes() != encoded:
            raise StudyContractError("compiled study experiment encoding is not canonical")
        return parsed


def _commitment_slot_key(*, arm: str, campaign_id: str, round_index: int) -> str:
    return f"{arm}-{_sha256(canonical_json_bytes_v5((arm, campaign_id, round_index)))}"


@dataclass(frozen=True, slots=True)
class StudyCommitmentIndexV1:
    """Stable create-only slot for one arm's round-two study graph."""

    arm: Literal["primary", "withheld"]
    campaign_id: str
    round_index: int
    import_ref: ArtifactRefV5
    draft_ref: ArtifactRefV5
    round_intent_ref: ArtifactRefV5
    parent_revision_sha256: str
    parent_source_bundle_ref: ArtifactRefV5
    binding_ref: ArtifactRefV5
    spec_ref: ArtifactRefV5
    corpus_ref: ArtifactRefV5
    contrast_ref: ArtifactRefV5
    selected_configuration_id: str
    schema_version: Literal[1] = _STUDY_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        if self.arm not in {"primary", "withheld"}:
            raise StudyContractError("study commitment arm is invalid")
        _text(self.campaign_id, "study commitment campaign")
        if type(self.round_index) is not int or self.round_index <= 0:
            raise StudyContractError("study commitment round is invalid")
        for value, label in (
            (self.import_ref, "study commitment import reference"),
            (self.draft_ref, "study commitment draft reference"),
            (self.round_intent_ref, "study commitment intent reference"),
            (self.parent_source_bundle_ref, "study commitment parent source reference"),
            (self.binding_ref, "study commitment binding reference"),
            (self.spec_ref, "study commitment spec reference"),
            (self.corpus_ref, "study commitment corpus reference"),
            (self.contrast_ref, "study commitment contrast reference"),
        ):
            if type(value) is not ArtifactRefV5:
                raise StudyContractError(f"{label} is invalid")
        _digest(self.parent_revision_sha256, "study commitment parent revision")
        _identifier(self.selected_configuration_id, "study commitment configuration")
        if type(self.schema_version) is not int or self.schema_version != _STUDY_SCHEMA_VERSION_V1:
            raise StudyContractError("study commitment schema version is invalid")

    @property
    def slot_key(self) -> str:
        return _commitment_slot_key(
            arm=self.arm,
            campaign_id=self.campaign_id,
            round_index=self.round_index,
        )

    @property
    def storage_ref(self) -> ArtifactRefV5:
        return ArtifactRefV5(
            f"adapter-blobs/study-v1-commitments/{self.slot_key}.bin",
            self.sha256,
        )

    def to_primitive(self) -> dict[str, object]:
        return {
            "arm": self.arm,
            "campaign_id": self.campaign_id,
            "round_index": self.round_index,
            "import_ref": _ref_primitive(self.import_ref),
            "draft_ref": _ref_primitive(self.draft_ref),
            "round_intent_ref": _ref_primitive(self.round_intent_ref),
            "parent_revision_sha256": self.parent_revision_sha256,
            "parent_source_bundle_ref": _ref_primitive(self.parent_source_bundle_ref),
            "binding_ref": _ref_primitive(self.binding_ref),
            "spec_ref": _ref_primitive(self.spec_ref),
            "corpus_ref": _ref_primitive(self.corpus_ref),
            "contrast_ref": _ref_primitive(self.contrast_ref),
            "selected_configuration_id": self.selected_configuration_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes_v5(self.to_primitive())

    @property
    def sha256(self) -> str:
        return _sha256(self.canonical_bytes())

    @classmethod
    def from_primitive(cls, value: object) -> "StudyCommitmentIndexV1":
        raw = _strict_mapping(
            value,
            {
                "arm",
                "campaign_id",
                "round_index",
                "import_ref",
                "draft_ref",
                "round_intent_ref",
                "parent_revision_sha256",
                "parent_source_bundle_ref",
                "binding_ref",
                "spec_ref",
                "corpus_ref",
                "contrast_ref",
                "selected_configuration_id",
                "schema_version",
            },
            "study commitment index",
        )
        return cls(
            arm=raw["arm"],  # type: ignore[arg-type]
            campaign_id=raw["campaign_id"],  # type: ignore[arg-type]
            round_index=raw["round_index"],  # type: ignore[arg-type]
            import_ref=_ref(raw["import_ref"], "study commitment import reference"),
            draft_ref=_ref(raw["draft_ref"], "study commitment draft reference"),
            round_intent_ref=_ref(raw["round_intent_ref"], "study commitment intent reference"),
            parent_revision_sha256=raw["parent_revision_sha256"],  # type: ignore[arg-type]
            parent_source_bundle_ref=_ref(raw["parent_source_bundle_ref"], "study commitment parent source reference"),
            binding_ref=_ref(raw["binding_ref"], "study commitment binding reference"),
            spec_ref=_ref(raw["spec_ref"], "study commitment spec reference"),
            corpus_ref=_ref(raw["corpus_ref"], "study commitment corpus reference"),
            contrast_ref=_ref(raw["contrast_ref"], "study commitment contrast reference"),
            selected_configuration_id=raw["selected_configuration_id"],  # type: ignore[arg-type]
            schema_version=raw["schema_version"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_canonical_json(cls, raw: bytes | str) -> "StudyCommitmentIndexV1":
        encoded = raw.encode("utf-8") if type(raw) is str else raw
        value = _strict_json(encoded, "study commitment index")
        parsed = cls.from_primitive(value)
        if parsed.canonical_bytes() != encoded:
            raise StudyContractError("study commitment index encoding is not canonical")
        return parsed


def _validate_parent_authority(
    *,
    parent: ParentCandidateV5,
    registry: FrozenBehaviorRegistryV1,
    configuration_id: str,
    repository: LocalArtifactRepositoryV5,
    preflight: FixturePreflightV1,
    fixture_manifest_ref: ArtifactRefV5,
) -> None:
    if type(parent) is not ParentCandidateV5:
        raise StudyAuthorityError("selected parent is not a typed candidate")
    try:
        configuration = registry.configuration(configuration_id)
    except (TypeError, ValueError) as exc:
        raise StudyAdmissionError("draft configuration is outside the frozen registry catalog") from exc
    if configuration.parent_configuration_id is None:
        expected = configuration
    else:
        expected = registry.configuration(configuration.parent_configuration_id)
    if (
        expected.policy_revision.sha256 != parent.policy_revision.sha256
        or expected.source_bundle.sha256 != parent.source_bundle_ref.sha256
    ):
        raise StudyAuthorityError("selected parent differs from the registered configuration parent")
    if parent.pit_data_scope != "production" or parent.semantic_mode != "required":
        raise StudyAuthorityError("selected parent does not use the production fixture authority")
    checkpoint = repository.load_checkpoint()
    if checkpoint is None:
        raise StudyAuthorityError("selected parent has no authenticated fixture checkpoint")
    try:
        source_bundle = repository.load_typed_artifact(parent.source_bundle_ref, value_type=SourceBundleV5)
    except (OSError, TypeError, ValueError, ArithmeticError) as exc:
        raise StudyAuthorityError("selected parent source bundle is unavailable") from exc
    if source_bundle.sha256 != parent.source_bundle_ref.sha256:
        raise StudyAuthorityError("selected parent source bundle identity differs from its reference")
    if parent.origin == "baseline":
        if parent.experiment_record_ref is not None:
            raise StudyAuthorityError("baseline parent carries archive ancestry")
        try:
            manifest = authenticate_campaign_manifest_v5(
                repository=repository,
                manifest_ref=fixture_manifest_ref,
            )
        except (OSError, TypeError, ValueError, ArithmeticError) as exc:
            raise StudyAuthorityError("baseline fixture authority is unavailable") from exc
        baseline = manifest.baseline_authority
        if (
            parent.policy_revision != baseline.policy_revision
            or parent.source_bundle_ref != baseline.source_bundle_ref
            or parent.semantic_fingerprint_sha256
            != (None if baseline.semantic_fingerprint is None else baseline.semantic_fingerprint.fingerprint_sha256)
        ):
            raise StudyAuthorityError("selected baseline parent differs from the fixture manifest authority")
    else:
        if parent.experiment_record_ref is None or parent.experiment_record_ref not in checkpoint.record_refs:
            raise StudyAuthorityError("archived parent is not present in the saved checkpoint ancestry")
        try:
            record = repository.load_experiment(parent.experiment_record_ref)
        except (OSError, TypeError, ValueError, ArithmeticError) as exc:
            raise StudyAuthorityError("archived parent record is unavailable") from exc
        record_fingerprint = getattr(record, "semantic_fingerprint", None)
        record_fingerprint_sha256 = (
            None if record_fingerprint is None else getattr(record_fingerprint, "fingerprint_sha256", None)
        )
        if (
            record.policy_revision != parent.policy_revision
            or parent.source_bundle_ref not in record.artifact_refs
            or record.status != "evaluated"
            or record.hypothesis.primary_mechanism != parent.primary_mechanism
            or record_fingerprint_sha256 != parent.semantic_fingerprint_sha256
            or getattr(record, "pit_data_scope", None) != parent.pit_data_scope
            or getattr(record, "semantic_mode", None) != parent.semantic_mode
        ):
            raise StudyAuthorityError("archived parent differs from checkpoint-authenticated ancestry")


def _exact_study_metrics(draft: ExperimentDraftV1) -> tuple[MechanismMetricSpecV1, MechanismMetricSpecV1]:
    expected = (
        MechanismMetricSpecV1(
            metric_id="exit.decision_changed_count",
            unit="count",
            direction="increase",
            tolerance=Decimal("0"),
            denominator="relevant_cases",
        ),
        MechanismMetricSpecV1(
            metric_id="exit.protected_control_unchanged_count",
            unit="count",
            direction="unchanged",
            tolerance=Decimal("0"),
            denominator="control_cases",
        ),
    )
    if draft.metrics != expected:
        raise StudyPrecommitmentError("study draft must declare exactly the local decision metric and protected control")
    return expected


def _persist_compiled_graph(
    *,
    store: StudyStoreV1,
    binding: StudyDraftBindingV1,
    spec: MechanismExperimentSpecV1,
    corpus: MechanismObservationCorpusV1,
    contrast: "StudyContrastV1",
) -> tuple[ArtifactRefV5, ArtifactRefV5, ArtifactRefV5, ArtifactRefV5]:
    # Each put is create-only.  Replaying the exact call is idempotent, while
    # changing any parameter collides with the same deterministic identity key.
    return (
        store.put(kind="draft-bindings", key=binding.sha256, content=binding.canonical_bytes()),
        store.put(kind="mechanism-specs", key=spec.sha256, content=spec.to_canonical_json().encode("utf-8")),
        store.put(kind="mechanism-corpora", key=corpus.sha256, content=corpus.canonical_bytes),
        store.put(kind="contrasts", key=contrast.sha256, content=contrast.canonical_bytes()),
    )


def _authenticate_import_record(store: StudyStoreV1, imported: StudyImportV1) -> None:
    try:
        raw = store.read(imported.storage_ref)
        if _sha256(raw) != imported.storage_ref.sha256:
            raise StudyAuthorityError("study import reference differs from its bytes")
        persisted = StudyImportV1.from_canonical_json(raw)
    except StudyAuthorityError:
        raise
    except (StudyContractError, TypeError, ValueError, ArithmeticError) as exc:
        raise StudyAuthorityError("study import record is not authenticated") from exc
    if persisted != imported:
        raise StudyAuthorityError("supplied study import differs from the persisted import")


def _load_commitment_index(
    *,
    store: StudyStoreV1,
    imported: StudyImportV1,
    preflight: FixturePreflightV1,
) -> StudyCommitmentIndexV1 | None:
    expected_path = f"adapter-blobs/study-v1-commitments/{_commitment_slot_key(arm=imported.arm, campaign_id=preflight.call.campaign_id, round_index=preflight.call.round_index)}.bin"
    refs = tuple(ref for ref in store.list_refs(kind="commitments") if ref.relative_path == expected_path)
    if len(refs) > 1:
        raise StudyAuthorityError("study commitment slot contains duplicate index bytes")
    if not refs:
        return None
    reference = refs[0]
    try:
        raw = store.read(reference)
        if _sha256(raw) != reference.sha256:
            raise StudyAuthorityError("study commitment index reference differs from its bytes")
        index = StudyCommitmentIndexV1.from_canonical_json(raw)
    except StudyAuthorityError:
        raise
    except (StudyContractError, TypeError, ValueError, ArithmeticError) as exc:
        raise StudyAuthorityError("study commitment index is not authenticated") from exc
    if (
        index.storage_ref != reference
        or index.arm != imported.arm
        or index.campaign_id != preflight.call.campaign_id
        or index.round_index != preflight.call.round_index
    ):
        raise StudyAuthorityError("study commitment index identity differs from its stable slot")
    return index


def _read_study_bytes(store: StudyStoreV1, reference: ArtifactRefV5, label: str) -> bytes:
    try:
        raw = store.read(reference)
    except (StudyContractError, TypeError, ValueError, ArithmeticError) as exc:
        raise StudyAuthorityError(f"{label} is unavailable") from exc
    if _sha256(raw) != reference.sha256:
        raise StudyAuthorityError(f"{label} differs from its persisted identity")
    return raw


def _load_committed_graph(
    *,
    store: StudyStoreV1,
    imported: StudyImportV1,
    index: StudyCommitmentIndexV1,
) -> CompiledStudyExperimentV1:
    if index.import_ref != imported.storage_ref or index.draft_ref not in imported.draft_refs:
        raise StudyAuthorityError("study commitment import or draft reference differs from the persisted import")
    _load_imported_draft(store, index.draft_ref)
    binding_raw = _read_study_bytes(store, index.binding_ref, "study commitment binding")
    spec_raw = _read_study_bytes(store, index.spec_ref, "study commitment spec")
    corpus_raw = _read_study_bytes(store, index.corpus_ref, "study commitment corpus")
    contrast_raw = _read_study_bytes(store, index.contrast_ref, "study commitment contrast")
    try:
        binding = StudyDraftBindingV1.from_canonical_json(binding_raw)
        spec = MechanismExperimentSpecV1.from_canonical_json(spec_raw.decode("utf-8"))
        corpus = _decode_observation_corpus(corpus_raw)
        from .contrast import StudyContrastV1

        contrast = StudyContrastV1.from_canonical_json(contrast_raw)
    except (UnicodeDecodeError, StudyContractError, TypeError, ValueError, ArithmeticError) as exc:
        raise StudyAuthorityError("study commitment graph contains invalid canonical bytes") from exc
    if (
        binding.storage_ref != index.binding_ref
        or binding.import_ref != index.import_ref
        or binding.draft_ref != index.draft_ref
        or binding.round_intent_ref != index.round_intent_ref
        or binding.journal_campaign_id != index.campaign_id
        or binding.journal_round_index != index.round_index
        or binding.parent_revision_sha256 != index.parent_revision_sha256
        or binding.parent_source_bundle_ref != index.parent_source_bundle_ref
        or spec.sha256 != index.spec_ref.sha256
        or corpus.sha256 != index.corpus_ref.sha256
        or contrast.sha256 != index.contrast_ref.sha256
        or contrast.draft_binding_sha256 != binding.sha256
        or contrast.spec_sha256 != spec.sha256
        or contrast.corpus_sha256 != corpus.sha256
    ):
        raise StudyAuthorityError("study commitment graph references differ from its authenticated index")
    try:
        return CompiledStudyExperimentV1(
            spec=spec,
            corpus=corpus,
            contrast=contrast,
            draft_binding=binding,
            selected_configuration_id=index.selected_configuration_id,
        )
    except (StudyContractError, TypeError, ValueError, ArithmeticError) as exc:
        raise StudyAuthorityError("study commitment graph is not internally consistent") from exc


def _candidate_commitment_index(
    *,
    imported: StudyImportV1,
    preflight: FixturePreflightV1,
    compiled: CompiledStudyExperimentV1,
    graph_refs: tuple[ArtifactRefV5, ArtifactRefV5, ArtifactRefV5, ArtifactRefV5],
) -> StudyCommitmentIndexV1:
    binding_ref, spec_ref, corpus_ref, contrast_ref = graph_refs
    return StudyCommitmentIndexV1(
        arm=imported.arm,
        campaign_id=preflight.call.campaign_id,
        round_index=preflight.call.round_index,
        import_ref=imported.storage_ref,
        draft_ref=compiled.draft_binding.draft_ref,
        round_intent_ref=compiled.draft_binding.round_intent_ref,
        parent_revision_sha256=compiled.draft_binding.parent_revision_sha256,
        parent_source_bundle_ref=compiled.draft_binding.parent_source_bundle_ref,
        binding_ref=binding_ref,
        spec_ref=spec_ref,
        corpus_ref=corpus_ref,
        contrast_ref=contrast_ref,
        selected_configuration_id=compiled.selected_configuration_id,
    )


def _expected_graph_refs(
    *,
    binding: StudyDraftBindingV1,
    spec: MechanismExperimentSpecV1,
    corpus: MechanismObservationCorpusV1,
    contrast: "StudyContrastV1",
) -> tuple[ArtifactRefV5, ArtifactRefV5, ArtifactRefV5, ArtifactRefV5]:
    return (
        binding.storage_ref,
        ArtifactRefV5(f"adapter-blobs/study-v1-mechanism-specs/{spec.sha256}.bin", spec.sha256),
        ArtifactRefV5(f"adapter-blobs/study-v1-mechanism-corpora/{corpus.sha256}.bin", corpus.sha256),
        ArtifactRefV5(f"adapter-blobs/study-v1-contrasts/{contrast.sha256}.bin", contrast.sha256),
    )


def _reject_orphaned_graph(
    *,
    store: StudyStoreV1,
    imported: StudyImportV1,
    preflight: FixturePreflightV1,
    graph_refs: tuple[ArtifactRefV5, ArtifactRefV5, ArtifactRefV5, ArtifactRefV5],
) -> None:
    # Graph leaves are content addressed and can be shared by two immutable
    # arm slots (the frozen observation corpus is the normal example).  A
    # namespace-wide path match is therefore not enough to call a candidate
    # orphan: first authenticate every existing stable commitment index and
    # retain only graph references that an index actually owns.  Unindexed
    # bytes remain a hard failure, including a same-hash corpus left by an
    # interrupted compile.
    owned_graph_refs: set[ArtifactRefV5] = set()
    for commitment_ref in store.list_refs(kind="commitments"):
        raw = _read_study_bytes(store, commitment_ref, "existing study commitment index")
        try:
            index = StudyCommitmentIndexV1.from_canonical_json(raw)
        except (StudyContractError, TypeError, ValueError, ArithmeticError) as exc:
            raise StudyAuthorityError("existing study commitment index is not authenticated") from exc
        if index.storage_ref != commitment_ref:
            raise StudyAuthorityError("existing study commitment index reference differs from its bytes")
        try:
            imported_raw = _read_study_bytes(store, index.import_ref, "existing study commitment import")
            existing_import = StudyImportV1.from_canonical_json(imported_raw)
            expected_slot_path = (
                f"adapter-blobs/study-v1-commitments/"
                f"{_commitment_slot_key(arm=index.arm, campaign_id=index.campaign_id, round_index=index.round_index)}.bin"
            )
            if (
                commitment_ref.relative_path != expected_slot_path
                or existing_import.storage_ref != index.import_ref
                or existing_import.arm != index.arm
                or existing_import.fixture_call.campaign_id != index.campaign_id
                or existing_import.fixture_call.round_index != index.round_index
            ):
                raise StudyAuthorityError("existing study commitment index ownership is invalid")
            _load_committed_graph(store=store, imported=existing_import, index=index)
        except (UnicodeDecodeError, StudyContractError, TypeError, ValueError, ArithmeticError) as exc:
            raise StudyAuthorityError("existing study commitment graph is not authenticated") from exc
        graph_pairs = (
            ("draft-bindings", index.binding_ref),
            ("mechanism-specs", index.spec_ref),
            ("mechanism-corpora", index.corpus_ref),
            ("contrasts", index.contrast_ref),
        )
        for kind, graph_ref in graph_pairs:
            matching = tuple(item for item in store.list_refs(kind=kind) if item == graph_ref)
            if len(matching) != 1:
                raise StudyAuthorityError("existing study commitment graph is incomplete")
            _read_study_bytes(store, graph_ref, f"existing {kind} graph")
            owned_graph_refs.add(graph_ref)

    for kind, reference in zip(
        ("draft-bindings", "mechanism-specs", "mechanism-corpora", "contrasts"),
        graph_refs,
        strict=True,
    ):
        if (
            any(item.relative_path == reference.relative_path for item in store.list_refs(kind=kind))
            and reference not in owned_graph_refs
        ):
            raise StudyPrecommitmentError("study commitment has orphaned graph bytes without its stable index")
    # A prior process may have persisted a different binding before it was
    # interrupted, so checking only the current candidate hashes would allow a
    # changed proposal to acquire the same stable slot.  Authenticate every
    # existing binding and reject any binding for this immutable import/round;
    # a stable index is required before a graph can be replayed.
    for reference in store.list_refs(kind="draft-bindings"):
        if reference == graph_refs[0]:
            continue
        raw = _read_study_bytes(store, reference, "orphaned study binding")
        try:
            binding = StudyDraftBindingV1.from_canonical_json(raw)
        except (StudyContractError, TypeError, ValueError, ArithmeticError) as exc:
            raise StudyAuthorityError("orphaned study binding is not authenticated") from exc
        if (
            binding.import_ref == imported.storage_ref
            and binding.journal_campaign_id == preflight.call.campaign_id
            and binding.journal_round_index == preflight.call.round_index
        ):
            raise StudyPrecommitmentError("study commitment has an orphaned binding without its stable index")


def compile_study_experiment_v1(
    *,
    store: StudyStoreV1,
    imported: StudyImportV1,
    request: RoleRequestV5,
    round_intent: RoundIntentPayloadV5,
    parent: ParentCandidateV5,
    seed_snapshot: ExitSnapshotV3,
    registry: FrozenBehaviorRegistryV1,
) -> CompiledStudyExperimentV1:
    """Resolve one stable, authoritative mechanism study commitment."""

    if type(store) is not StudyStoreV1 or type(imported) is not StudyImportV1:
        raise StudyContractError("study compiler store or import is invalid")
    if type(request) is not RoleRequestV5 or request.role != "investigator":
        raise StudyAuthorityError("study compiler requires the authenticated investigator request")
    if type(round_intent) is not RoundIntentPayloadV5 or type(parent) is not ParentCandidateV5:
        raise StudyContractError("study compiler intent or parent is invalid")
    if type(seed_snapshot) is not ExitSnapshotV3 or type(registry) is not FrozenBehaviorRegistryV1:
        raise StudyContractError("study compiler seed snapshot or registry is invalid")
    try:
        verify_registry_v1(registry)
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise StudyAuthorityError("supplied registry is not the frozen catalog") from exc

    preflight = _load_preflight(store, imported)
    _authenticate_import_record(store, imported)
    commitment_index = _load_commitment_index(store=store, imported=imported, preflight=preflight)
    (
        repository,
        persisted_intent,
        role_package_ref,
        intent_ref,
        package,
        investigator_sequence,
        intent_sequence,
        author_requests,
        author_completion_exists,
    ) = (
        _load_authoritative_fixture_graph(preflight=preflight, imported=imported)
    )
    if request != package.request or request.sha256 != preflight.request_sha256:
        raise StudyAuthorityError("supplied investigator request differs from the authenticated F package")
    if persisted_intent != round_intent:
        raise StudyAuthorityError("supplied round intent differs from the immutable saved intent")
    persisted_intent_sha256 = _intent_sha256(persisted_intent)
    investigator_artifact = _load_investigator_artifact(store, imported)
    if package.artifact != investigator_artifact:
        raise StudyAuthorityError("fixture role package differs from the imported ordinary artifact")
    selected_hypotheses = tuple(
        item for item in investigator_artifact.hypotheses if item.hypothesis_id == persisted_intent.hypothesis.hypothesis_id
    )
    if len(selected_hypotheses) != 1 or selected_hypotheses[0] != persisted_intent.hypothesis:
        raise StudyAuthorityError("selected hypothesis is not the exact persisted F hypothesis")
    fixture_evidence_ids = set(persisted_intent.hypothesis.evidence_ids)

    matching_drafts = tuple(
        (reference, _load_imported_draft(store, reference))
        for reference in imported.draft_refs
        if reference.sha256 in imported.draft_hashes
    )
    selected_drafts = tuple(item for item in matching_drafts if item[1].hypothesis_id == persisted_intent.hypothesis.hypothesis_id)
    if len(selected_drafts) != 1:
        raise StudyAuthorityError("persisted imports do not contain exactly one selected draft")
    draft_ref, draft = selected_drafts[0]
    if draft_ref.sha256 != imported.draft_hashes[imported.draft_refs.index(draft_ref)]:
        raise StudyAuthorityError("selected draft reference differs from the import authority")
    if any(citation not in fixture_evidence_ids for citation in draft.cited_evidence_ids):
        raise StudyAuthorityError("selected draft cites evidence outside the imported F artifact")

    if persisted_intent.parent_revision_sha256 != parent.policy_revision.sha256:
        raise StudyAuthorityError("selected parent differs from the persisted intent")
    if persisted_intent.parent_semantic_fingerprint_sha256 != parent.semantic_fingerprint_sha256:
        raise StudyAuthorityError("selected parent fingerprint differs from the persisted intent")
    if persisted_intent.pit_data_scope != parent.pit_data_scope or persisted_intent.semantic_mode != parent.semantic_mode:
        raise StudyAuthorityError("selected parent semantic authority differs from the persisted intent")
    _validate_parent_authority(
        parent=parent,
        registry=registry,
        configuration_id=draft.configuration_id,
        repository=repository,
        preflight=preflight,
        fixture_manifest_ref=imported.fixture_manifest_ref,
    )

    metrics = _exact_study_metrics(draft)
    if (
        draft.applicability.field != "features.atr_20_fraction"
        or draft.recipe.recipe_id != "evaluate_exit_atr20_fraction_v1"
        or draft.recipe.input_field != "features.atr_20_fraction"
        or any(item.metric_id == "evaluator.exit_attribution_count" for item in draft.metrics)
    ):
        raise StudyPrecommitmentError("study draft uses an unsupported field, recipe, or evaluator metric")
    resource_budget = study_resource_budget_v1()
    if len(draft.recipe.input_values) > resource_budget.max_cases:
        raise StudyAdmissionError("study recipe exceeds the frozen case resource bound")

    binding_seed = canonical_json_bytes_v5(
        {
            "import": imported.sha256,
            "draft": draft_ref.sha256,
            "hypothesis": persisted_intent.hypothesis.sha256,
            "intent": persisted_intent_sha256,
            "parent": parent.policy_revision.sha256,
            "configuration": draft.configuration_id,
        }
    )
    precommitment_id = f"v5.study.{_sha256(binding_seed)}"
    spec = MechanismExperimentSpecV1(
        precommitment_id=precommitment_id,
        hypothesis_id=persisted_intent.hypothesis.hypothesis_id,
        hypothesis_sha256=persisted_intent.hypothesis.sha256,
        parent_revision_sha256=parent.policy_revision.sha256,
        round_intent_sha256=persisted_intent_sha256,
        target_method="evaluate_exit",
        allowed_symbols=MECHANISM_ALLOWED_SYMBOLS_V1,
        applicability=draft.applicability,
        controls=(MechanismControlV1(control_id="protected_next_stop_price"),),
        metrics=metrics,
        aggregation="paired_case_delta",
        minimum_relevant_cases=2,
        recipe=draft.recipe,
        disconfirming_observations=draft.disconfirming_observations,
        provenance="synthetic_offline",
        frozen_before_authoring=True,
    )
    try:
        validate_mechanism_spec_hypothesis_v1(
            spec,
            persisted_intent.hypothesis,
            parent_revision_sha256=parent.policy_revision.sha256,
            round_intent_sha256=persisted_intent_sha256,
        )
        corpus = build_mechanism_observation_corpus_v1(spec, seed_snapshot=seed_snapshot)
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise StudyPrecommitmentError("mechanism spec or corpus is not admissible") from exc
    if sum(case.applicable for case in corpus.cases) < spec.minimum_relevant_cases:
        raise StudyAdmissionError("mechanism corpus has fewer than the required applicable cases")
    if not any(not case.applicable for case in corpus.cases):
        raise StudyAdmissionError("mechanism corpus must retain a negative case")

    binding = StudyDraftBindingV1(
        import_ref=imported.storage_ref,
        draft_ref=draft_ref,
        hypothesis_id=persisted_intent.hypothesis.hypothesis_id,
        hypothesis_sha256=persisted_intent.hypothesis.sha256,
        fixture_role_package_ref=role_package_ref,
        fixture_role_package_sha256=package.sha256,
        round_intent_ref=intent_ref,
        round_intent_sha256=persisted_intent_sha256,
        parent_revision_sha256=parent.policy_revision.sha256,
        parent_source_bundle_ref=parent.source_bundle_ref,
        parent_experiment_record_ref=parent.experiment_record_ref,
        checkpoint_ref=preflight.checkpoint_ref,
        snapshot_ref=preflight.snapshot_ref,
        fixture_manifest_ref=imported.fixture_manifest_ref,
        fixture_root_identity_sha256=imported.fixture_root_identity_sha256,
        journal_campaign_id=preflight.call.campaign_id,
        journal_round_index=preflight.call.round_index,
        investigator_sequence=investigator_sequence,
        intent_sequence=intent_sequence,
    )

    # Import lazily so compiler.py remains importable for callers that only
    # need its binding type.
    from .contrast import StudyContrastV1

    case_ids = tuple(f"case-{case.order}" for case in corpus.cases)
    input_identities = tuple(case.input_identity_sha256 for case in corpus.cases)
    contrast = StudyContrastV1(
        draft_binding_ref=binding.storage_ref,
        draft_binding_sha256=binding.sha256,
        parent_revision_sha256=parent.policy_revision.sha256,
        parent_source_bundle_ref=parent.source_bundle_ref,
        spec_sha256=spec.sha256,
        corpus_sha256=corpus.sha256,
        precommitment_id=precommitment_id,
        case_ids=case_ids,
        input_identities=input_identities,
        expected_changed=draft.expected_changed,
        rivals=draft.rivals,
        claim_kind=draft.claim_kind,
    )
    compiled = CompiledStudyExperimentV1(
        spec=spec,
        corpus=corpus,
        contrast=contrast,
        draft_binding=binding,
        selected_configuration_id=draft.configuration_id,
    )
    graph_refs = _expected_graph_refs(binding=binding, spec=spec, corpus=corpus, contrast=contrast)
    candidate_index = _candidate_commitment_index(
        imported=imported,
        preflight=preflight,
        compiled=compiled,
        graph_refs=graph_refs,
    )
    if commitment_index is not None:
        committed = _load_committed_graph(store=store, imported=imported, index=commitment_index)
        if commitment_index != candidate_index or committed != compiled:
            raise StudyPrecommitmentError("study commitment differs from the stable authenticated graph")
        return committed
    if author_requests or author_completion_exists:
        raise StudyAuthorityError("author request already exists before a new study commitment")
    _reject_orphaned_graph(
        store=store,
        imported=imported,
        preflight=preflight,
        graph_refs=graph_refs,
    )
    persisted_refs = _persist_compiled_graph(
        store=store,
        binding=binding,
        spec=spec,
        corpus=corpus,
        contrast=contrast,
    )
    if persisted_refs != graph_refs:
        raise StudyAuthorityError("persisted study graph references differ from the canonical graph")
    index_ref = store.put(
        kind="commitments",
        key=candidate_index.slot_key,
        content=candidate_index.canonical_bytes(),
    )
    if index_ref != candidate_index.storage_ref:
        raise StudyAuthorityError("persisted study commitment index differs from its stable slot")
    return compiled


__all__ = [
    "CompiledStudyExperimentV1",
    "StudyCommitmentIndexV1",
    "StudyDraftBindingV1",
    "compile_study_experiment_v1",
]
