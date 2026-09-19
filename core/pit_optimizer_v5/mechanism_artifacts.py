"""Authenticated persistence and the narrow runtime adapter for mechanism evidence.

The mechanism evidence contracts deliberately keep their local wire format
separate from the legacy typed artifact decoder.  Observation cases retain
finite floats and canonical bytes, while the generic V5 artifact codec is
intentionally stricter and does not reconstruct those values.  This module
therefore stores bounded versioned binary sidecars and only uses typed state
for small, create-only indexes.

No object in this module is an authorization inferred from a manifest flag,
filesystem presence, or an environment variable.  A capability is issued at
an explicit local fixture boundary and is revalidated against the exact
authenticated manifest, current source, round intent, spec, corpus, and
resource budget every time it is used.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, replace
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from typing import Literal, Protocol, runtime_checkable

from core.pit_optimizer_v5.artifacts import ArtifactRefV5, ArtifactRepositoryFailureV5, LocalArtifactRepositoryV5
from core.pit_optimizer_v5.candidate_ir import PolicyRevisionIdentityV5, SourceBundleV5
from core.pit_optimizer_v5.contracts import canonical_json_bytes_v5, canonical_sha256_v5
from core.pit_optimizer_v5.contracts import (
    CampaignEvidenceV5,
    PanelEvaluationV5,
    validate_campaign_evidence_v5,
)
from core.pit_optimizer_v5.memory import (
    RoundIntentPayloadV5,
    StoredExperimentRecordV5,
    round_event_payload_primitive_v5,
)
from core.pit_optimizer_v5.manifest import (
    AuthenticatedCampaignManifestV5,
    ManifestAuthenticationFailureV5,
    authenticate_campaign_manifest_v5,
)
from core.pit_optimizer_v5.search import (
    ArchiveAuthorityMismatchV5,
    ArchiveEntryV5,
    ParentCandidateV5,
    archive_parent_from_record_v5,
)
from core.pit_optimizer_v5.mechanism_contracts import (
    MechanismEvidenceReportV1,
    MechanismCoverageV1,
    MechanismExecutionV1,
    MechanismExperimentSpecV1,
    MechanismObservationBindingV1,
    MechanismResourceBudgetV1,
    bind_mechanism_observation_v1,
    validate_mechanism_report_against_spec_v1,
    validate_mechanism_observation_binding_v1,
    validate_mechanism_spec_hypothesis_v1,
)
from core.pit_optimizer_v5.mechanism_probes import (
    MechanismObservationCorpusV1,
    MechanismObservationRunV1,
    MechanismPairedCaseV1,
    MechanismPairedObservationV1,
    MechanismWorkerPortV1,
    collect_mechanism_observations_v1,
    validate_mechanism_observation_corpus_v1,
)
from core.pit_optimizer_v5.mechanism_reports import (
    MechanismEvaluatorMatchV1,
    build_mechanism_evidence_report_v1,
)
from core.pit_optimizer_v5.probes import canonical_probe_json_v5


MECHANISM_ARTIFACT_SCHEMA_VERSION_V1 = 1
MECHANISM_ARTIFACT_NAMESPACE_V1 = "mechanism-v5"
MECHANISM_ARTIFACT_MAX_BYTES_V1 = 64 * 1024 * 1024
MECHANISM_ARTIFACT_MAX_INDEX_TEXT_V1 = 256 * 1024
MECHANISM_EXTENSION_SOURCE_PATH_V1 = Path(__file__)

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_CAMPAIGN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_WIRE_PREFIX = b"pit-optimizer-v5-mechanism\x00"
_STAGES = ("binding", "run", "report", "complete")


def _digest(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _text(value: object, label: str, *, maximum: int = 512) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{label} is invalid")
    return value


def _campaign(value: object) -> str:
    text = _text(value, "campaign ID", maximum=128)
    if _CAMPAIGN_RE.fullmatch(text) is None:
        raise ValueError("campaign ID is invalid")
    return text


def _positive(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be positive")
    return value


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _extension_source_sha256() -> str:
    try:
        content = MECHANISM_EXTENSION_SOURCE_PATH_V1.read_bytes()
    except OSError as exc:
        raise MechanismCapabilityError("mechanism extension source is unavailable") from exc
    return _sha256_bytes(content)


def mechanism_extension_source_sha256_v1() -> str:
    """Return the current source identity used by capability revalidation."""

    return _extension_source_sha256()


def round_intent_sha256_v1(intent: RoundIntentPayloadV5) -> str:
    """Hash the exact authenticated round-intent payload representation."""

    if type(intent) is not RoundIntentPayloadV5:
        raise ValueError("round intent must use the V5 payload")
    return canonical_sha256_v5(round_event_payload_primitive_v5(intent))


def manifest_source_identity_sha256_v1(authenticated: AuthenticatedCampaignManifestV5) -> str:
    """Bind the exact manifest and its authenticated editable source scope."""

    if type(authenticated) is not AuthenticatedCampaignManifestV5:
        raise ValueError("mechanism capability requires an authenticated campaign manifest")
    _validate_authenticated_manifest_v1(authenticated)
    return canonical_sha256_v5(
        {
            "manifest_ref": authenticated.manifest_ref.to_primitive(),
            "manifest_sha256": authenticated.manifest_ref.sha256,
            "source_commit": authenticated.policy_scope.source_commit,
            "editable_source_sha256": authenticated.policy_scope.editable_source_sha256,
            "baseline_revision_sha256": authenticated.manifest.baseline_policy_revision_ref.sha256,
        }
    )


def _validate_authenticated_manifest_v1(authenticated: AuthenticatedCampaignManifestV5) -> None:
    """Recheck the persisted aggregate before allowing mechanism use.

    The aggregate is normally returned by ``authenticate_campaign_manifest_v5``;
    these checks keep an externally reconstructed object from serving as a
    permission token merely because it has the expected Python type.
    """

    if type(authenticated) is not AuthenticatedCampaignManifestV5:
        raise MechanismCapabilityError("mechanism capability requires an authenticated campaign manifest")
    try:
        graph = authenticated.graph
        manifest = authenticated.manifest
        if not graph.verified or graph.manifest_ref != authenticated.manifest_ref:
            raise ValueError("manifest graph is not verified")
        if authenticated.manifest_ref.sha256 != manifest.sha256:
            raise ValueError("manifest reference digest differs")
        if manifest.source_commit != authenticated.policy_scope.source_commit:
            raise ValueError("manifest source commit differs")
        if manifest.policy_scope_ref.sha256 != canonical_sha256_v5(authenticated.policy_scope):
            raise ValueError("manifest policy scope digest differs")
        if manifest.panel_plan_ref.sha256 != authenticated.panel_plan.sha256:
            raise ValueError("manifest panel plan digest differs")
        if manifest.execution_profile_ref.sha256 != authenticated.execution_profile.sha256:
            raise ValueError("manifest execution profile digest differs")
        if manifest.evaluator_contract_ref.sha256 != authenticated.evaluator_contract.sha256:
            raise ValueError("manifest evaluator contract digest differs")
        if manifest.sandbox_profile_ref.sha256 != authenticated.sandbox_profile.sha256:
            raise ValueError("manifest sandbox profile digest differs")
        if manifest.baseline_authority_ref.sha256 != canonical_sha256_v5(authenticated.baseline_authority):
            raise ValueError("manifest baseline authority digest differs")
        if manifest.baseline_policy_revision_ref != authenticated.baseline_authority.policy_revision_ref:
            raise ValueError("manifest baseline revision reference differs")
        if authenticated.baseline_policy_revision != authenticated.baseline_authority.policy_revision:
            raise ValueError("authenticated baseline revision differs")
        if (
            authenticated.baseline_authority.source_bundle_ref.sha256
            != authenticated.baseline_authority.source_bundle.sha256
        ):
            raise ValueError("authenticated baseline source reference differs")
        if authenticated.baseline_authority.policy_revision.editable_source_sha256 != tuple(
            (item.path, item.sha256) for item in authenticated.baseline_authority.source_bundle.files
        ):
            raise ValueError("authenticated baseline source differs from its revision")
        graph_keys = {(item.reference.relative_path, item.reference.sha256) for item in graph.authenticated}
        required_refs = (
            authenticated.manifest_ref,
            manifest.panel_plan_ref,
            manifest.execution_profile_ref,
            manifest.evaluator_contract_ref,
            manifest.baseline_authority_ref,
            manifest.policy_scope_ref,
            manifest.baseline_policy_revision_ref,
            manifest.sandbox_profile_ref,
            authenticated.baseline_authority.source_bundle_ref,
        )
        if any((reference.relative_path, reference.sha256) not in graph_keys for reference in required_refs):
            raise ValueError("authenticated manifest graph is incomplete")
    except (AttributeError, TypeError, ValueError, ArithmeticError) as exc:
        raise MechanismCapabilityError("authenticated manifest/source context is incomplete") from exc


class MechanismCapabilityError(ValueError):
    """A capability or binding failed closed before mechanism work."""


class MechanismChronologyError(MechanismCapabilityError):
    """The durable author-request boundary makes a new precommitment invalid."""


class MechanismArtifactCorrupt(MechanismCapabilityError):
    """An authenticated sidecar or index cannot be reconstructed exactly."""


@dataclass(frozen=True, slots=True)
class MechanismFixtureOptInV1:
    """An explicit ephemeral opt-in for the authorized local fixture slice.

    ``nonce_sha256`` is an identity for this local opt-in, not a cryptographic
    signature or a replacement for human authorization.  The capability also
    binds this value to the exact authenticated worktree and experiment tuple.
    """

    kind: Literal["focused_offline_fixture"]
    nonce_sha256: str
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        if self.kind != "focused_offline_fixture":
            raise ValueError("mechanism opt-in kind is unsupported")
        _digest(self.nonce_sha256, "mechanism opt-in nonce")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("mechanism opt-in schema version is unsupported")

    @property
    def sha256(self) -> str:
        return canonical_sha256_v5(self)


def explicit_mechanism_fixture_opt_in_v1(seed: str) -> MechanismFixtureOptInV1:
    """Issue one typed, local-only opt-in from an explicit caller seed."""

    if type(seed) is not str or not seed.strip():
        raise ValueError("mechanism fixture opt-in seed is required")
    return MechanismFixtureOptInV1(
        kind="focused_offline_fixture",
        nonce_sha256=hashlib.sha256(seed.encode("utf-8")).hexdigest(),
    )


# Friendly aliases for callers that prefer the noun-first spelling.
MechanismExplicitOptInV1 = MechanismFixtureOptInV1
issue_mechanism_fixture_opt_in_v1 = explicit_mechanism_fixture_opt_in_v1


def _budget_sha256(budget: MechanismResourceBudgetV1) -> str:
    if type(budget) is not MechanismResourceBudgetV1:
        raise ValueError("mechanism resource budget is invalid")
    return budget.sha256


def _budget_primitive_json(budget: MechanismResourceBudgetV1) -> str:
    return canonical_json_bytes_v5(budget).decode("utf-8")


def _decode_budget(raw: str) -> MechanismResourceBudgetV1:
    if type(raw) is not str or len(raw.encode("utf-8")) > MECHANISM_ARTIFACT_MAX_INDEX_TEXT_V1:
        raise MechanismArtifactCorrupt("mechanism budget index bytes are invalid")
    try:
        primitive = json.loads(raw)
        budget = MechanismResourceBudgetV1.from_primitive(primitive)
    except (UnicodeError, TypeError, ValueError, ArithmeticError, json.JSONDecodeError) as exc:
        raise MechanismArtifactCorrupt("mechanism budget index is invalid") from exc
    if canonical_json_bytes_v5(budget).decode("utf-8") != raw:
        raise MechanismArtifactCorrupt("mechanism budget index is noncanonical")
    return budget


def _decode_probe_input(raw: bytes) -> Decimal | None:
    """Decode the lossless Decimal/None identity used by the probe corpus."""

    try:
        primitive = json.loads(raw.decode("utf-8"))
    except (UnicodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MechanismArtifactCorrupt("mechanism recipe input identity is invalid") from exc
    if primitive == ["none"]:
        return None
    if (
        type(primitive) is not list
        or len(primitive) != 4
        or primitive[0] != "decimal"
        or type(primitive[1]) is not int
        or type(primitive[2]) is not str
        or type(primitive[3]) is not int
    ):
        raise MechanismArtifactCorrupt("mechanism recipe input identity is invalid")
    if primitive[1] not in {0, 1} or not primitive[2].isdigit():
        raise MechanismArtifactCorrupt("mechanism recipe input identity is invalid")
    try:
        value = Decimal((primitive[1], tuple(int(item) for item in primitive[2]), primitive[3]))
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise MechanismArtifactCorrupt("mechanism recipe input identity is invalid") from exc
    if not value.is_finite():
        raise MechanismArtifactCorrupt("mechanism recipe input identity is nonfinite")
    return value


def _encode_spec(spec: MechanismExperimentSpecV1) -> bytes:
    # The legacy JSON identity normalizes Decimal lexical zeros.  Retain the
    # exact lossless probe input bytes beside it so Task 2's spec-bound
    # validator can recheck recipe identity after restart.
    payload = {
        "spec_canonical_json": spec.to_canonical_json(),
        "recipe_input_canonical_bytes": [_b64(canonical_probe_json_v5(item)) for item in spec.recipe.input_values],
    }
    return canonical_json_bytes_v5(payload)


def _decode_spec(raw: bytes) -> MechanismExperimentSpecV1:
    payload = _json_object(raw, "mechanism spec")
    if (
        set(payload) != {"spec_canonical_json", "recipe_input_canonical_bytes"}
        or type(payload["recipe_input_canonical_bytes"]) is not list
    ):
        raise MechanismArtifactCorrupt("mechanism spec fields are invalid")
    try:
        spec = MechanismExperimentSpecV1.from_canonical_json(payload["spec_canonical_json"])  # type: ignore[arg-type]
        recipe_inputs = tuple(
            _decode_probe_input(_unb64(item, "mechanism recipe input"))
            for item in payload["recipe_input_canonical_bytes"]
        )
    except (MechanismCapabilityError, TypeError, ValueError, ArithmeticError) as exc:
        raise MechanismArtifactCorrupt("mechanism spec does not reconstruct") from exc
    if len(recipe_inputs) != len(spec.recipe.input_values):
        raise MechanismArtifactCorrupt("mechanism spec recipe input count differs")
    try:
        spec = replace(spec, recipe=replace(spec.recipe, input_values=recipe_inputs))
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise MechanismArtifactCorrupt("mechanism spec recipe does not reconstruct") from exc
    if _encode_spec(spec) != raw:
        raise MechanismArtifactCorrupt("mechanism spec wire payload is not canonical")
    return spec


def _validate_resource_budget(
    authenticated: AuthenticatedCampaignManifestV5, budget: MechanismResourceBudgetV1
) -> None:
    resources = authenticated.manifest.resources
    sandbox = authenticated.sandbox_profile
    # cpu_seconds is a duration.  The manifest's evaluation_cpu_limit is a
    # quota in cores; multiplying it by the admitted mechanics wall duration
    # yields the only deterministic conversion at this boundary.
    cpu_seconds_ceiling = resources.evaluation_cpu_limit * Decimal(resources.mechanics_timeout_seconds)
    if (
        budget.timeout_ms > resources.mechanics_timeout_seconds * 1000
        or budget.cpu_seconds > cpu_seconds_ceiling
        or budget.memory_mib > min(resources.evaluation_memory_mib, sandbox.memory_limit_mib)
        or budget.output_bytes > min(resources.evaluation_output_limit_bytes, sandbox.output_limit_bytes)
    ):
        raise MechanismCapabilityError("mechanism resource budget exceeds authenticated manifest limits")


def _archive_entry_from_stored_record_v1(
    *,
    stored_record: StoredExperimentRecordV5,
    source_bundle_ref: ArtifactRefV5,
) -> ArchiveEntryV5:
    """Construct archive authority only from one complete persisted record."""

    record = stored_record.record
    if record.policy_revision is None or record.campaign_evidence is None:
        raise MechanismCapabilityError("archive parent record lacks evaluated campaign authority")
    try:
        return ArchiveEntryV5(
            policy_revision=record.policy_revision,
            primary_mechanism=record.hypothesis.primary_mechanism,
            admitted_round=record.round_index,
            campaign=record.campaign_evidence,
            source_bundle_ref=source_bundle_ref,
            experiment_record_ref=stored_record.reference,
        )
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise MechanismCapabilityError("archive parent record is not admissible authority") from exc


def _validate_archive_parent_record_v1(
    *,
    parent_candidate: ParentCandidateV5,
    parent_record: StoredExperimentRecordV5 | None,
    parent_source_bundle_ref: ArtifactRefV5,
) -> None:
    if parent_candidate.origin != "archive":
        if parent_record is not None:
            raise MechanismCapabilityError("baseline parent cannot carry an archive record")
        return
    if parent_record is None:
        raise MechanismCapabilityError("archive parent lacks persisted record authority")
    if parent_candidate.experiment_record_ref != parent_record.reference:
        raise MechanismCapabilityError("archive parent record reference differs")
    entry = _archive_entry_from_stored_record_v1(
        stored_record=parent_record,
        source_bundle_ref=parent_source_bundle_ref,
    )
    try:
        resolved = archive_parent_from_record_v5(entry=entry, stored_record=parent_record)
    except (ArchiveAuthorityMismatchV5, TypeError, ValueError, ArithmeticError) as exc:
        raise MechanismCapabilityError("archive parent record authority differs") from exc
    if resolved != parent_candidate:
        raise MechanismCapabilityError("archive parent candidate differs from its record authority")


@dataclass(frozen=True, slots=True)
class MechanismExtensionCapabilityV1:
    """Ephemeral content-bound permission for one mechanism evidence run."""

    authenticated_manifest: AuthenticatedCampaignManifestV5
    round_intent: RoundIntentPayloadV5
    parent_candidate: ParentCandidateV5
    parent_revision: PolicyRevisionIdentityV5
    parent_revision_ref: ArtifactRefV5
    parent_source_bundle: SourceBundleV5
    parent_source_bundle_ref: ArtifactRefV5
    spec: MechanismExperimentSpecV1
    corpus: MechanismObservationCorpusV1
    resource_budget: MechanismResourceBudgetV1
    authorization: MechanismFixtureOptInV1
    round_index: int
    execution_kind: Literal["synthetic_fixture", "registered_sandbox"] = "synthetic_fixture"
    extension_source_sha256: str = ""
    schema_version: Literal[1] = 1
    parent_campaign_evidence: CampaignEvidenceV5 | None = None
    parent_quick_evidence: PanelEvaluationV5 | None = None
    parent_record: StoredExperimentRecordV5 | None = None

    def __post_init__(self) -> None:
        if type(self.authenticated_manifest) is not AuthenticatedCampaignManifestV5:
            raise MechanismCapabilityError("mechanism capability requires authenticated manifest authority")
        _validate_authenticated_manifest_v1(self.authenticated_manifest)
        if type(self.round_intent) is not RoundIntentPayloadV5:
            raise MechanismCapabilityError("mechanism capability round intent is invalid")
        if type(self.parent_candidate) is not ParentCandidateV5:
            raise MechanismCapabilityError("mechanism selected parent authority is invalid")
        if (
            type(self.parent_revision) is not PolicyRevisionIdentityV5
            or type(self.parent_source_bundle) is not SourceBundleV5
        ):
            raise MechanismCapabilityError("mechanism authored parent source authority is invalid")
        if (
            type(self.parent_revision_ref) is not ArtifactRefV5
            or type(self.parent_source_bundle_ref) is not ArtifactRefV5
        ):
            raise MechanismCapabilityError("mechanism authored parent source reference is invalid")
        if type(self.spec) is not MechanismExperimentSpecV1 or type(self.corpus) is not MechanismObservationCorpusV1:
            raise MechanismCapabilityError("mechanism capability spec or corpus is invalid")
        if type(self.resource_budget) is not MechanismResourceBudgetV1:
            raise MechanismCapabilityError("mechanism capability resource budget is invalid")
        if type(self.authorization) is not MechanismFixtureOptInV1:
            raise MechanismCapabilityError("mechanism capability requires explicit typed opt-in")
        _positive(self.round_index, "mechanism capability round")
        if self.execution_kind not in {"synthetic_fixture", "registered_sandbox"}:
            raise MechanismCapabilityError("mechanism execution kind is unsupported")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise MechanismCapabilityError("mechanism capability schema version is unsupported")
        if self.parent_campaign_evidence is not None and type(self.parent_campaign_evidence) is not CampaignEvidenceV5:
            raise MechanismCapabilityError("mechanism selected parent campaign evidence is invalid")
        if self.parent_quick_evidence is not None and type(self.parent_quick_evidence) is not PanelEvaluationV5:
            raise MechanismCapabilityError("mechanism selected parent quick evidence is invalid")
        if self.parent_record is not None and type(self.parent_record) is not StoredExperimentRecordV5:
            raise MechanismCapabilityError("mechanism selected parent record authority is invalid")
        source_sha256 = self.extension_source_sha256 or _extension_source_sha256()
        _digest(source_sha256, "mechanism extension source SHA-256")
        object.__setattr__(self, "extension_source_sha256", source_sha256)
        manifest = self.authenticated_manifest.manifest
        if manifest.campaign_id != _campaign(manifest.campaign_id):
            raise MechanismCapabilityError("mechanism capability campaign identity is invalid")
        expected_intent_sha256 = round_intent_sha256_v1(self.round_intent)
        parent_revision_sha256 = self.parent_revision.sha256
        if self.parent_candidate.policy_revision != self.parent_revision:
            raise MechanismCapabilityError("mechanism selected parent revision differs")
        if self.parent_candidate.source_bundle_ref != self.parent_source_bundle_ref:
            raise MechanismCapabilityError("mechanism selected parent source reference differs")
        if (
            self.parent_candidate.pit_data_scope != manifest.pit_data_scope
            or self.parent_candidate.semantic_mode != manifest.semantic_mode
            or self.round_intent.pit_data_scope != manifest.pit_data_scope
            or self.round_intent.semantic_mode != manifest.semantic_mode
            or self.round_intent.discovery_plan_sha256 != self.authenticated_manifest.panel_plan.discovery_plan_sha256
        ):
            raise MechanismCapabilityError("mechanism round semantic authority differs")
        expected_parent_semantic = self.parent_candidate.semantic_fingerprint_sha256
        if expected_parent_semantic != self.round_intent.parent_semantic_fingerprint_sha256:
            raise MechanismCapabilityError("mechanism selected parent fingerprint differs")
        if (
            self.round_intent.parent_revision_sha256 != parent_revision_sha256
            or self.spec.parent_revision_sha256 != parent_revision_sha256
        ):
            raise MechanismCapabilityError("mechanism authored parent identity differs")
        if self.parent_source_bundle_ref.sha256 != self.parent_source_bundle.sha256:
            raise MechanismCapabilityError("mechanism authored parent source reference differs")
        if self.parent_revision_ref.sha256 != self.parent_revision.sha256:
            raise MechanismCapabilityError("mechanism authored parent revision reference differs")
        if self.parent_revision.editable_source_sha256 != tuple(
            (item.path, item.sha256) for item in self.parent_source_bundle.files
        ):
            raise MechanismCapabilityError("mechanism authored parent source differs from its revision")
        if self.parent_candidate.origin == "baseline":
            _validate_archive_parent_record_v1(
                parent_candidate=self.parent_candidate,
                parent_record=self.parent_record,
                parent_source_bundle_ref=self.parent_source_bundle_ref,
            )
            if self.parent_revision != self.authenticated_manifest.baseline_authority.policy_revision:
                raise MechanismCapabilityError("baseline parent differs from authenticated campaign baseline")
            parent_campaign_evidence = self.authenticated_manifest.baseline_authority.campaign
        else:
            _validate_archive_parent_record_v1(
                parent_candidate=self.parent_candidate,
                parent_record=self.parent_record,
                parent_source_bundle_ref=self.parent_source_bundle_ref,
            )
            assert self.parent_record is not None
            parent_campaign_evidence = self.parent_record.record.campaign_evidence
            if parent_campaign_evidence is None:
                raise MechanismCapabilityError("archive parent lacks checkpoint-authorized campaign evidence")
            if self.parent_campaign_evidence is not None and self.parent_campaign_evidence != parent_campaign_evidence:
                raise MechanismCapabilityError("archive parent campaign differs from its record authority")
        try:
            validate_campaign_evidence_v5(
                parent_campaign_evidence,
                panel_plan=self.authenticated_manifest.panel_plan,
                evaluator_contract=self.authenticated_manifest.evaluator_contract,
                policy_identity_sha256=parent_revision_sha256,
            )
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise MechanismCapabilityError("selected parent campaign evidence is not authenticated") from exc
        if self.parent_quick_evidence is not None:
            quick = self.parent_quick_evidence
            if (
                quick.policy_identity_sha256 != parent_revision_sha256
                or quick.evaluator_contract_sha256 != self.authenticated_manifest.evaluator_contract.sha256
                or quick.sandbox_profile_sha256 != self.authenticated_manifest.sandbox_profile.sha256
                or quick.panel_sha256 != self.authenticated_manifest.panel_plan.quick.panel_ref.sha256
                or quick.start_date != self.authenticated_manifest.panel_plan.quick.start_date
                or quick.end_date != self.authenticated_manifest.panel_plan.quick.end_date
            ):
                raise MechanismCapabilityError("selected parent quick evidence is not authenticated")
        if self.spec.round_intent_sha256 != expected_intent_sha256:
            raise MechanismCapabilityError("mechanism spec round intent differs from authenticated payload")
        if self.spec.hypothesis_sha256 != self.round_intent.hypothesis.sha256:
            raise MechanismCapabilityError("mechanism spec hypothesis differs from round intent")
        validate_mechanism_spec_hypothesis_v1(
            self.spec,
            self.round_intent.hypothesis,
            parent_revision_sha256=parent_revision_sha256,
            round_intent_sha256=expected_intent_sha256,
        )
        if self.corpus.recipe_sha256 != self.spec.recipe.sha256:
            raise MechanismCapabilityError("mechanism corpus differs from frozen recipe")
        expected_inputs = tuple(canonical_probe_json_v5(item) for item in self.spec.recipe.input_values)
        if tuple(item.input_canonical_bytes for item in self.corpus.cases) != expected_inputs:
            raise MechanismCapabilityError("mechanism corpus input identity differs from frozen recipe")
        if tuple(item.input_value for item in self.corpus.cases) != self.spec.recipe.input_values:
            raise MechanismCapabilityError("mechanism corpus order differs from frozen recipe")
        _validate_resource_budget(self.authenticated_manifest, self.resource_budget)

    @property
    def campaign_id(self) -> str:
        return self.authenticated_manifest.manifest.campaign_id

    @property
    def manifest_ref(self) -> ArtifactRefV5:
        return self.authenticated_manifest.manifest_ref

    @property
    def manifest_source_identity_sha256(self) -> str:
        return manifest_source_identity_sha256_v1(self.authenticated_manifest)

    @property
    def parent_revision_sha256(self) -> str:
        return self.parent_revision.sha256

    @property
    def authorization_sha256(self) -> str:
        return self.authorization.sha256

    @property
    def precommitment_id(self) -> str:
        return self.spec.precommitment_id

    def revalidate(
        self,
        *,
        authenticated_manifest: AuthenticatedCampaignManifestV5 | None = None,
        round_intent: RoundIntentPayloadV5 | None = None,
        spec: MechanismExperimentSpecV1 | None = None,
        corpus: MechanismObservationCorpusV1 | None = None,
        resource_budget: MechanismResourceBudgetV1 | None = None,
    ) -> None:
        """Revalidate all content bindings and the current extension source."""

        if _extension_source_sha256() != self.extension_source_sha256:
            raise MechanismCapabilityError("mechanism extension source identity changed")
        manifest = self.authenticated_manifest if authenticated_manifest is None else authenticated_manifest
        intent = self.round_intent if round_intent is None else round_intent
        selected_spec = self.spec if spec is None else spec
        selected_corpus = self.corpus if corpus is None else corpus
        selected_budget = self.resource_budget if resource_budget is None else resource_budget
        if type(manifest) is not AuthenticatedCampaignManifestV5:
            raise MechanismCapabilityError("mechanism manifest authority is not authenticated")
        if manifest_source_identity_sha256_v1(manifest) != self.manifest_source_identity_sha256:
            raise MechanismCapabilityError("mechanism manifest or source identity changed")
        if manifest.manifest_ref != self.manifest_ref or manifest.manifest.campaign_id != self.campaign_id:
            raise MechanismCapabilityError("mechanism manifest identity changed")
        if type(intent) is not RoundIntentPayloadV5 or round_intent_sha256_v1(intent) != round_intent_sha256_v1(
            self.round_intent
        ):
            raise MechanismCapabilityError("mechanism round intent changed")
        if (
            type(self.parent_revision) is not PolicyRevisionIdentityV5
            or type(self.parent_candidate) is not ParentCandidateV5
            or type(self.parent_revision_ref) is not ArtifactRefV5
            or type(self.parent_source_bundle) is not SourceBundleV5
            or type(self.parent_source_bundle_ref) is not ArtifactRefV5
            or intent.parent_revision_sha256 != self.parent_revision.sha256
            or self.parent_candidate.policy_revision != self.parent_revision
            or self.parent_candidate.source_bundle_ref != self.parent_source_bundle_ref
            or self.parent_candidate.semantic_fingerprint_sha256 != intent.parent_semantic_fingerprint_sha256
            or self.parent_revision_ref.sha256 != self.parent_revision.sha256
            or self.parent_source_bundle_ref.sha256 != self.parent_source_bundle.sha256
            or self.parent_revision.editable_source_sha256
            != tuple((item.path, item.sha256) for item in self.parent_source_bundle.files)
        ):
            raise MechanismCapabilityError("mechanism authored parent source authority changed")
        if type(selected_spec) is not MechanismExperimentSpecV1 or selected_spec.sha256 != self.spec.sha256:
            raise MechanismCapabilityError("mechanism spec identity changed")
        if type(selected_corpus) is not MechanismObservationCorpusV1 or selected_corpus.sha256 != self.corpus.sha256:
            raise MechanismCapabilityError("mechanism corpus identity changed")
        if (
            type(selected_budget) is not MechanismResourceBudgetV1
            or selected_budget.sha256 != self.resource_budget.sha256
        ):
            raise MechanismCapabilityError("mechanism resource budget changed")
        if type(self.authorization) is not MechanismFixtureOptInV1:
            raise MechanismCapabilityError("mechanism explicit opt-in is unavailable")
        # Keep the full constructor checks active for fresh current values.
        _validate_resource_budget(manifest, selected_budget)

    def bind_candidate(
        self,
        *,
        experiment_id: str,
        candidate_revision: PolicyRevisionIdentityV5,
        candidate_source_bundle: SourceBundleV5,
        scenario_id: str = "base",
    ) -> "MechanismBoundCandidateV1":
        self.revalidate()
        _digest(experiment_id, "mechanism experiment ID")
        if (
            type(candidate_revision) is not PolicyRevisionIdentityV5
            or type(candidate_source_bundle) is not SourceBundleV5
        ):
            raise MechanismCapabilityError("candidate source authority is invalid")
        if candidate_revision.editable_source_sha256 != tuple(
            (item.path, item.sha256) for item in candidate_source_bundle.files
        ):
            raise MechanismCapabilityError("candidate revision differs from its source bundle")
        if candidate_revision.sha256 == self.parent_revision_sha256:
            raise MechanismCapabilityError("candidate policy revision is not distinct from parent")
        try:
            binding = bind_mechanism_observation_v1(
                self.spec,
                experiment_id=experiment_id,
                candidate_bytes_sha256=candidate_source_bundle.sha256,
                corpus_sha256=self.corpus.sha256,
                evaluator_contract_sha256=self.authenticated_manifest.evaluator_contract.sha256,
                scenario_id=scenario_id,
                resource_budget=self.resource_budget,
            )
            validate_mechanism_observation_binding_v1(
                self.spec,
                binding,
                expected_candidate_bytes_sha256=candidate_source_bundle.sha256,
            )
        except (TypeError, ValueError) as exc:
            raise MechanismCapabilityError("candidate observation binding is invalid") from exc
        return MechanismBoundCandidateV1(
            capability=self,
            binding=binding,
            candidate_revision=candidate_revision,
            candidate_source_bundle=candidate_source_bundle,
        )

    @property
    def selected_parent_campaign_evidence(self) -> CampaignEvidenceV5 | None:
        if self.parent_candidate.origin == "baseline":
            return self.authenticated_manifest.baseline_authority.campaign
        if self.parent_record is not None:
            return self.parent_record.record.campaign_evidence
        return None


@dataclass(frozen=True, slots=True)
class MechanismBoundCandidateV1:
    capability: MechanismExtensionCapabilityV1
    binding: MechanismObservationBindingV1
    candidate_revision: PolicyRevisionIdentityV5
    candidate_source_bundle: SourceBundleV5

    def __post_init__(self) -> None:
        if type(self.capability) is not MechanismExtensionCapabilityV1:
            raise MechanismCapabilityError("bound mechanism capability is invalid")
        if type(self.binding) is not MechanismObservationBindingV1:
            raise MechanismCapabilityError("bound mechanism observation binding is invalid")
        self.capability.revalidate()
        validate_mechanism_observation_binding_v1(
            self.capability.spec,
            self.binding,
            expected_candidate_bytes_sha256=self.candidate_source_bundle.sha256,
        )
        _digest(self.binding.experiment_id, "bound mechanism experiment ID")

    @property
    def experiment_id(self) -> str:
        return self.binding.experiment_id


@dataclass(frozen=True, slots=True)
class MechanismPrecommitmentIndexV1:
    schema_version: Literal[1]
    campaign_id: str
    round_index: int
    precommitment_id: str
    manifest_ref: ArtifactRefV5
    manifest_sha256: str
    manifest_source_identity_sha256: str
    extension_source_sha256: str
    round_intent_sha256: str
    parent_revision_sha256: str
    parent_revision_ref: ArtifactRefV5
    parent_source_bundle_ref: ArtifactRefV5
    parent_source_bundle_sha256: str
    hypothesis_sha256: str
    spec_sha256: str
    corpus_sha256: str
    resource_budget_sha256: str
    resource_budget_canonical_json: str
    execution_kind: Literal["synthetic_fixture", "registered_sandbox"]
    authorization_sha256: str
    spec_ref: ArtifactRefV5
    corpus_ref: ArtifactRefV5

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("mechanism precommitment index schema is unsupported")
        _campaign(self.campaign_id)
        _positive(self.round_index, "mechanism precommitment round")
        _text(self.precommitment_id, "mechanism precommitment ID", maximum=128)
        if (
            type(self.manifest_ref) is not ArtifactRefV5
            or type(self.parent_revision_ref) is not ArtifactRefV5
            or type(self.parent_source_bundle_ref) is not ArtifactRefV5
            or type(self.spec_ref) is not ArtifactRefV5
            or type(self.corpus_ref) is not ArtifactRefV5
        ):
            raise ValueError("mechanism precommitment index artifact reference is invalid")
        for value, label in (
            (self.manifest_sha256, "manifest SHA-256"),
            (self.manifest_source_identity_sha256, "manifest source identity SHA-256"),
            (self.extension_source_sha256, "extension source SHA-256"),
            (self.round_intent_sha256, "round intent SHA-256"),
            (self.parent_revision_sha256, "parent revision SHA-256"),
            (self.parent_source_bundle_sha256, "parent source bundle SHA-256"),
            (self.hypothesis_sha256, "hypothesis SHA-256"),
            (self.spec_sha256, "spec SHA-256"),
            (self.corpus_sha256, "corpus SHA-256"),
            (self.resource_budget_sha256, "resource budget SHA-256"),
            (self.authorization_sha256, "authorization SHA-256"),
        ):
            _digest(value, label)
        if self.execution_kind not in {"synthetic_fixture", "registered_sandbox"}:
            raise ValueError("mechanism precommitment execution kind is invalid")
        if self.parent_source_bundle_ref.sha256 != self.parent_source_bundle_sha256:
            raise ValueError("mechanism parent source bundle reference differs")
        if self.parent_revision_ref.sha256 != self.parent_revision_sha256:
            raise ValueError("mechanism parent revision reference differs")
        _decode_budget(self.resource_budget_canonical_json)


@dataclass(frozen=True, slots=True)
class MechanismExperimentIndexV1:
    schema_version: Literal[1]
    phase: Literal["binding", "run", "report", "complete"]
    campaign_id: str
    round_index: int
    experiment_id: str
    precommitment_id: str
    spec_sha256: str
    corpus_sha256: str
    binding_sha256: str
    candidate_revision_sha256: str
    candidate_source_bundle_sha256: str
    binding_ref: ArtifactRefV5
    run_ref: ArtifactRefV5 | None
    report_ref: ArtifactRefV5 | None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1 or self.phase not in _STAGES:
            raise ValueError("mechanism experiment index schema or phase is invalid")
        _campaign(self.campaign_id)
        _positive(self.round_index, "mechanism experiment round")
        for value, label in (
            (self.experiment_id, "experiment ID"),
            (self.spec_sha256, "spec SHA-256"),
            (self.corpus_sha256, "corpus SHA-256"),
            (self.binding_sha256, "binding SHA-256"),
            (self.candidate_revision_sha256, "candidate revision SHA-256"),
            (self.candidate_source_bundle_sha256, "candidate source bundle SHA-256"),
        ):
            _digest(value, label)
        _text(self.precommitment_id, "mechanism precommitment ID", maximum=128)
        if type(self.binding_ref) is not ArtifactRefV5:
            raise ValueError("mechanism binding reference is invalid")
        if self.run_ref is not None and type(self.run_ref) is not ArtifactRefV5:
            raise ValueError("mechanism run reference is invalid")
        if self.report_ref is not None and type(self.report_ref) is not ArtifactRefV5:
            raise ValueError("mechanism report reference is invalid")
        if self.phase == "binding" and (self.run_ref is not None or self.report_ref is not None):
            raise ValueError("binding index cannot contain later sidecars")
        if self.phase == "run" and self.run_ref is None:
            raise ValueError("run index requires a run sidecar")
        if self.phase == "report" and self.report_ref is None:
            raise ValueError("report index requires a report sidecar")
        if self.phase == "complete" and (self.run_ref is None or self.report_ref is None):
            raise ValueError("complete index requires run and report sidecars")


@dataclass(frozen=True, slots=True)
class MechanismPersistedPrecommitmentV1:
    index: MechanismPrecommitmentIndexV1
    spec: MechanismExperimentSpecV1
    corpus: MechanismObservationCorpusV1
    spec_ref: ArtifactRefV5
    corpus_ref: ArtifactRefV5


@dataclass(frozen=True, slots=True)
class MechanismPersistedEvidenceV1:
    """A read-only authenticated evidence bundle recovered after restart.

    This DTO contains no fixture opt-in and cannot be passed to the runtime
    extension.  Callers must independently authenticate the legacy experiment
    record before asking the repository to load the matching mechanism bundle.
    """

    index: MechanismExperimentIndexV1
    spec: MechanismExperimentSpecV1
    corpus: MechanismObservationCorpusV1
    binding: MechanismObservationBindingV1
    run: MechanismObservationRunV1
    report: MechanismEvidenceReportV1
    parent_source_bundle_sha256: str | None = None


def _wire(kind: str, content: bytes) -> bytes:
    _text(kind, "mechanism wire kind", maximum=32)
    if type(content) is not bytes or len(content) > MECHANISM_ARTIFACT_MAX_BYTES_V1:
        raise ValueError("mechanism wire content exceeds its bound")
    return _WIRE_PREFIX + kind.encode("ascii") + b"\x00" + len(content).to_bytes(8, "big") + content


def _unwire(raw: bytes, expected_kind: str) -> bytes:
    if type(raw) is not bytes or len(raw) < len(_WIRE_PREFIX) + 10:
        raise MechanismArtifactCorrupt("mechanism sidecar wire envelope is truncated")
    prefix = _WIRE_PREFIX + expected_kind.encode("ascii") + b"\x00"
    if not raw.startswith(prefix):
        raise MechanismArtifactCorrupt("mechanism sidecar wire kind differs")
    cursor = len(prefix)
    declared = int.from_bytes(raw[cursor : cursor + 8], "big")
    content = raw[cursor + 8 :]
    if declared != len(content) or len(content) > MECHANISM_ARTIFACT_MAX_BYTES_V1:
        raise MechanismArtifactCorrupt("mechanism sidecar wire length differs")
    return content


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: object, label: str) -> bytes:
    if type(value) is not str:
        raise MechanismArtifactCorrupt(f"{label} is not base64 text")
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeError, base64.binascii.Error) as exc:
        raise MechanismArtifactCorrupt(f"{label} is not valid base64") from exc
    return decoded


def _json_object(raw: bytes, label: str) -> dict[str, object]:
    def reject_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_pairs)
    except (UnicodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MechanismArtifactCorrupt(f"{label} is not canonical JSON") from exc
    if type(value) is not dict:
        raise MechanismArtifactCorrupt(f"{label} is not an object")
    if canonical_json_bytes_v5(value) != raw:
        raise MechanismArtifactCorrupt(f"{label} is not canonical JSON")
    return value


def _encode_corpus(corpus: MechanismObservationCorpusV1) -> bytes:
    payload = {
        "recipe_sha256": corpus.recipe_sha256,
        "cases": [
            {
                "order": case.order,
                "input_canonical_bytes": _b64(case.input_canonical_bytes),
                "input_identity_sha256": case.input_identity_sha256,
                "converted_value": None if case.converted_value is None else case.converted_value.hex(),
                "snapshot_canonical_json": _b64(case.snapshot_canonical_json),
                "snapshot_sha256": case.snapshot_sha256,
                "applicable": case.applicable,
            }
            for case in corpus.cases
        ],
    }
    return canonical_json_bytes_v5(payload)


def _decode_corpus(raw: bytes, spec: MechanismExperimentSpecV1) -> MechanismObservationCorpusV1:
    payload = _json_object(raw, "mechanism corpus")
    expected_keys = {"recipe_sha256", "cases"}
    if set(payload) != expected_keys or type(payload["cases"]) is not list:
        raise MechanismArtifactCorrupt("mechanism corpus fields are invalid")
    raw_cases = payload["cases"]
    assert type(raw_cases) is list
    if len(raw_cases) != len(spec.recipe.input_values):
        raise MechanismArtifactCorrupt("mechanism corpus case count differs from spec")
    cases: list[MechanismPairedCaseV1] = []
    for expected_order, (item, input_value) in enumerate(zip(raw_cases, spec.recipe.input_values, strict=True)):
        if type(item) is not dict:
            raise MechanismArtifactCorrupt("mechanism corpus case is invalid")
        expected = {
            "order",
            "input_canonical_bytes",
            "input_identity_sha256",
            "converted_value",
            "snapshot_canonical_json",
            "snapshot_sha256",
            "applicable",
        }
        if set(item) != expected or item["order"] != expected_order:
            raise MechanismArtifactCorrupt("mechanism corpus case fields are invalid")
        converted_raw = item["converted_value"]
        if converted_raw is None:
            converted = None
        elif type(converted_raw) is str:
            try:
                converted = float.fromhex(converted_raw)
            except (TypeError, ValueError, OverflowError) as exc:
                raise MechanismArtifactCorrupt("mechanism corpus converted input is invalid") from exc
        else:
            raise MechanismArtifactCorrupt("mechanism corpus converted input is invalid")
        snapshot_json = _unb64(item["snapshot_canonical_json"], "mechanism corpus snapshot")
        try:
            from core.strategy_policy.contracts_v3 import ExitSnapshotV3

            snapshot = ExitSnapshotV3.from_canonical_json(snapshot_json.decode("utf-8"))
        except (UnicodeError, TypeError, ValueError, OverflowError) as exc:
            raise MechanismArtifactCorrupt("mechanism corpus snapshot is invalid") from exc
        cases.append(
            MechanismPairedCaseV1(
                order=expected_order,
                input_value=input_value,
                input_canonical_bytes=_unb64(item["input_canonical_bytes"], "mechanism corpus input"),
                input_identity_sha256=item["input_identity_sha256"],  # type: ignore[arg-type]
                converted_value=converted,
                snapshot=snapshot,
                snapshot_canonical_json=snapshot_json,
                snapshot_sha256=item["snapshot_sha256"],  # type: ignore[arg-type]
                applicable=item["applicable"],  # type: ignore[arg-type]
            )
        )
    try:
        corpus = MechanismObservationCorpusV1(recipe_sha256=payload["recipe_sha256"], cases=tuple(cases))  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise MechanismArtifactCorrupt("mechanism corpus does not reconstruct") from exc
    if (
        canonical_json_bytes_v5(
            {
                "recipe_sha256": corpus.recipe_sha256,
                "cases": [
                    {
                        "order": case.order,
                        "input_canonical_bytes": _b64(case.input_canonical_bytes),
                        "input_identity_sha256": case.input_identity_sha256,
                        "converted_value": None if case.converted_value is None else case.converted_value.hex(),
                        "snapshot_canonical_json": _b64(case.snapshot_canonical_json),
                        "snapshot_sha256": case.snapshot_sha256,
                        "applicable": case.applicable,
                    }
                    for case in corpus.cases
                ],
            }
        )
        != raw
    ):
        raise MechanismArtifactCorrupt("mechanism corpus wire payload is not canonical")
    return corpus


def _encode_observation(item: MechanismPairedObservationV1) -> dict[str, object]:
    return {
        "corpus_sha256": item.corpus_sha256,
        "case_order": item.case_order,
        "input_canonical_bytes": _b64(item.input_canonical_bytes),
        "input_identity_sha256": item.input_identity_sha256,
        "converted_value": None if item.converted_value is None else item.converted_value.hex(),
        "case_snapshot_json": _b64(item.case_snapshot_json),
        "snapshot_sha256": item.snapshot_sha256,
        "applicable": item.applicable,
        "repetition": item.repetition,
        "parent_decision_json": _b64(item.parent_decision_json),
        "parent_decision_sha256": item.parent_decision_sha256,
        "candidate_decision_json": _b64(item.candidate_decision_json),
        "candidate_decision_sha256": item.candidate_decision_sha256,
        "decision_changed": item.decision_changed,
        "protected_control_unchanged": item.protected_control_unchanged,
    }


def _decode_observation(item: object, corpus: MechanismObservationCorpusV1) -> MechanismPairedObservationV1:
    if type(item) is not dict:
        raise MechanismArtifactCorrupt("mechanism observation is invalid")
    expected = {
        "corpus_sha256",
        "case_order",
        "input_canonical_bytes",
        "input_identity_sha256",
        "converted_value",
        "case_snapshot_json",
        "snapshot_sha256",
        "applicable",
        "repetition",
        "parent_decision_json",
        "parent_decision_sha256",
        "candidate_decision_json",
        "candidate_decision_sha256",
        "decision_changed",
        "protected_control_unchanged",
    }
    if set(item) != expected:
        raise MechanismArtifactCorrupt("mechanism observation fields are invalid")
    converted_raw = item["converted_value"]
    if converted_raw is None:
        converted = None
    elif type(converted_raw) is str:
        try:
            converted = float.fromhex(converted_raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise MechanismArtifactCorrupt("mechanism observation converted input is invalid") from exc
    else:
        raise MechanismArtifactCorrupt("mechanism observation converted input is invalid")
    try:
        observation = MechanismPairedObservationV1(
            corpus_sha256=item["corpus_sha256"],  # type: ignore[arg-type]
            case_order=item["case_order"],  # type: ignore[arg-type]
            input_value=(
                corpus.cases[item["case_order"]].input_value  # type: ignore[index]
                if type(item["case_order"]) is int and 0 <= item["case_order"] < len(corpus.cases)
                else None
            ),
            input_canonical_bytes=_unb64(item["input_canonical_bytes"], "mechanism observation input"),
            input_identity_sha256=item["input_identity_sha256"],  # type: ignore[arg-type]
            converted_value=converted,
            case_snapshot_json=_unb64(item["case_snapshot_json"], "mechanism observation snapshot"),
            snapshot_sha256=item["snapshot_sha256"],  # type: ignore[arg-type]
            applicable=item["applicable"],  # type: ignore[arg-type]
            repetition=item["repetition"],  # type: ignore[arg-type]
            parent_decision_json=_unb64(item["parent_decision_json"], "mechanism parent decision"),
            parent_decision_sha256=item["parent_decision_sha256"],  # type: ignore[arg-type]
            candidate_decision_json=_unb64(item["candidate_decision_json"], "mechanism candidate decision"),
            candidate_decision_sha256=item["candidate_decision_sha256"],  # type: ignore[arg-type]
            decision_changed=item["decision_changed"],  # type: ignore[arg-type]
            protected_control_unchanged=item["protected_control_unchanged"],  # type: ignore[arg-type]
        )
    except (IndexError, TypeError, ValueError) as exc:
        raise MechanismArtifactCorrupt("mechanism observation does not reconstruct") from exc
    return observation


def _encode_run(run: MechanismObservationRunV1) -> bytes:
    payload = {
        "binding": run.binding.to_canonical_json(),
        "corpus_sha256": run.corpus.sha256,
        "execution": run.execution.to_canonical_json(),
        "coverage": run.coverage.to_canonical_json(),
        "observations": [_encode_observation(item) for item in run.observations],
        "repetitions": run.repetitions,
        "reset_semantics": run.reset_semantics,
        "limitations": list(run.limitations),
    }
    return canonical_json_bytes_v5(payload)


def _decode_run(
    raw: bytes,
    *,
    spec: MechanismExperimentSpecV1,
    binding: MechanismObservationBindingV1,
    corpus: MechanismObservationCorpusV1,
) -> MechanismObservationRunV1:
    payload = _json_object(raw, "mechanism observation run")
    expected = {
        "binding",
        "corpus_sha256",
        "execution",
        "coverage",
        "observations",
        "repetitions",
        "reset_semantics",
        "limitations",
    }
    if set(payload) != expected or payload["corpus_sha256"] != corpus.sha256:
        raise MechanismArtifactCorrupt("mechanism observation run fields are invalid")
    if (
        payload["binding"] != binding.to_canonical_json()
        or type(payload["observations"]) is not list
        or type(payload["limitations"]) is not list
    ):
        raise MechanismArtifactCorrupt("mechanism observation run binding or collections differ")
    try:
        execution = MechanismExecutionV1.from_canonical_json(payload["execution"])  # type: ignore[arg-type]
        from core.pit_optimizer_v5.mechanism_contracts import MechanismCoverageV1

        coverage = MechanismCoverageV1.from_canonical_json(payload["coverage"])  # type: ignore[arg-type]
        observations = tuple(_decode_observation(item, corpus) for item in payload["observations"])
        run = MechanismObservationRunV1(
            binding=binding,
            corpus=corpus,
            execution=execution,
            coverage=coverage,
            observations=observations,
            repetitions=payload["repetitions"],  # type: ignore[arg-type]
            reset_semantics=payload["reset_semantics"],  # type: ignore[arg-type]
            limitations=tuple(payload["limitations"]),  # type: ignore[arg-type]
        )
    except (TypeError, ValueError, IndexError, ArithmeticError) as exc:
        raise MechanismArtifactCorrupt("mechanism observation run does not reconstruct") from exc
    if _encode_run(run) != raw:
        raise MechanismArtifactCorrupt("mechanism observation run wire payload is not canonical")
    validate_mechanism_observation_corpus_v1(spec, binding, corpus)
    return run


def _encode_contract(kind: str, value: object) -> bytes:
    if not hasattr(value, "to_canonical_json"):
        raise ValueError(f"{kind} is not a canonical mechanism contract")
    return value.to_canonical_json().encode("utf-8")


def _decode_contract(raw: bytes, kind: str, value_type: type[object]) -> object:
    content = _unwire(raw, kind)
    try:
        return value_type.from_canonical_json(content.decode("utf-8"))  # type: ignore[attr-defined]
    except (UnicodeError, TypeError, ValueError, ArithmeticError, OverflowError) as exc:
        raise MechanismArtifactCorrupt(f"mechanism {kind} sidecar does not reconstruct") from exc


@runtime_checkable
class MechanismWorkerFactoryV1(Protocol):
    def __call__(self, bound: MechanismBoundCandidateV1) -> tuple[MechanismWorkerPortV1, MechanismWorkerPortV1]: ...


class MechanismArtifactRepositoryV5:
    """Repository adapter using only LocalArtifactRepositoryV5 safe primitives."""

    def __init__(self, repository: LocalArtifactRepositoryV5) -> None:
        if type(repository) is not LocalArtifactRepositoryV5:
            raise ValueError("mechanism artifact repository requires LocalArtifactRepositoryV5")
        self.repository = repository

    def _authenticate_capability(self, capability: MechanismExtensionCapabilityV1) -> None:
        """Reauthenticate every persisted authority through this repository."""

        capability.revalidate()
        try:
            authenticated = authenticate_campaign_manifest_v5(
                repository=self.repository,
                manifest_ref=capability.manifest_ref,
            )
            parent_revision = self.repository.load_typed_artifact(
                capability.parent_revision_ref,
                value_type=PolicyRevisionIdentityV5,
            )
            parent_source_bundle = self.repository.load_typed_artifact(
                capability.parent_source_bundle_ref,
                value_type=SourceBundleV5,
            )
        except (ArtifactRepositoryFailureV5, ManifestAuthenticationFailureV5, TypeError, ValueError) as exc:
            raise MechanismCapabilityError("mechanism capability is not authenticated by this repository") from exc
        if (
            authenticated != capability.authenticated_manifest
            or parent_revision != capability.parent_revision
            or parent_source_bundle != capability.parent_source_bundle
        ):
            raise MechanismCapabilityError("mechanism capability authority differs from this repository")
        manifest = authenticated.manifest
        intent = capability.round_intent
        if (
            intent.pit_data_scope != manifest.pit_data_scope
            or intent.semantic_mode != manifest.semantic_mode
            or intent.discovery_plan_sha256 != authenticated.panel_plan.discovery_plan_sha256
        ):
            raise MechanismCapabilityError("mechanism round intent differs from authenticated manifest")
        if capability.parent_candidate.origin == "archive":
            record_ref = capability.parent_candidate.experiment_record_ref
            if record_ref is None:
                raise MechanismCapabilityError("archive parent lacks experiment record reference")
            if capability.parent_record is None:
                raise MechanismCapabilityError("archive parent lacks persisted record authority")
            try:
                record = self.repository.load_experiment(record_ref)
                stored_record = StoredExperimentRecordV5(reference=record_ref, record=record)
            except (ArtifactRepositoryFailureV5, TypeError, ValueError) as exc:
                raise MechanismCapabilityError("archive parent record is not authenticated by this repository") from exc
            self._require_checkpoint_record(record_ref)
            if stored_record != capability.parent_record:
                raise MechanismCapabilityError("archive parent record differs from this repository")
            _validate_archive_parent_record_v1(
                parent_candidate=capability.parent_candidate,
                parent_record=stored_record,
                parent_source_bundle_ref=capability.parent_source_bundle_ref,
            )

    def _require_checkpoint_record(self, reference: ArtifactRefV5) -> None:
        try:
            checkpoint = self.repository.load_checkpoint()
        except ArtifactRepositoryFailureV5 as exc:
            raise MechanismCapabilityError("mechanism checkpoint authority is unreadable") from exc
        if checkpoint is None or reference not in checkpoint.record_refs:
            raise MechanismCapabilityError("mechanism record is not checkpoint-authorized")

    @staticmethod
    def _round_key(campaign_id: str, round_index: int) -> str:
        return f"{_campaign(campaign_id)}-{_positive(round_index, 'mechanism round'):04d}"

    @staticmethod
    def _experiment_key(experiment_id: str, phase: str) -> str:
        _digest(experiment_id, "mechanism experiment ID")
        if phase not in _STAGES:
            raise ValueError("mechanism experiment index phase is invalid")
        return f"{experiment_id}-{phase}"

    def _author_requests_exist(self, campaign_id: str, round_index: int) -> bool:
        try:
            entries = self.repository.load_authenticated_role_requests(
                campaign_id=campaign_id,
                round_index=round_index,
                role="author",
            )
        except AttributeError:
            # The narrow public method is supplied by this task's artifact seam;
            # absence in an alternate repository is fail-closed.
            raise MechanismCapabilityError("authenticated role-request query is unavailable") from None
        return bool(entries)

    def _load_precommitment_index(
        self, capability: MechanismExtensionCapabilityV1
    ) -> MechanismPrecommitmentIndexV1 | None:
        try:
            return self.repository.load_typed_state(
                namespace=MECHANISM_ARTIFACT_NAMESPACE_V1,
                key=self._round_key(capability.campaign_id, capability.round_index),
                value_type=MechanismPrecommitmentIndexV1,
                repair=True,
            )
        except (ArtifactRepositoryFailureV5, TypeError, ValueError) as exc:
            raise MechanismArtifactCorrupt("mechanism precommitment index is unreadable") from exc

    def _load_precommitment_index_by_identity(
        self,
        *,
        campaign_id: str,
        round_index: int,
    ) -> MechanismPrecommitmentIndexV1 | None:
        try:
            return self.repository.load_typed_state(
                namespace=MECHANISM_ARTIFACT_NAMESPACE_V1,
                key=self._round_key(campaign_id, round_index),
                value_type=MechanismPrecommitmentIndexV1,
                repair=False,
            )
        except (ArtifactRepositoryFailureV5, TypeError, ValueError) as exc:
            raise MechanismArtifactCorrupt("mechanism precommitment index is unreadable") from exc

    def _validate_precommitment_index_identity(
        self,
        *,
        campaign_id: str,
        round_index: int,
        index: MechanismPrecommitmentIndexV1,
    ) -> None:
        if index.campaign_id != _campaign(campaign_id) or index.round_index != _positive(
            round_index, "mechanism round"
        ):
            raise MechanismArtifactCorrupt("mechanism precommitment identity differs")
        round_key = self._round_key(campaign_id, round_index)
        if index.spec_ref.relative_path != f"adapter-blobs/{MECHANISM_ARTIFACT_NAMESPACE_V1}/{round_key}-spec.bin":
            raise MechanismArtifactCorrupt("mechanism spec sidecar path is not canonical")
        if index.corpus_ref.relative_path != f"adapter-blobs/{MECHANISM_ARTIFACT_NAMESPACE_V1}/{round_key}-corpus.bin":
            raise MechanismArtifactCorrupt("mechanism corpus sidecar path is not canonical")

    def _validate_precommitment_index(
        self,
        capability: MechanismExtensionCapabilityV1,
        index: MechanismPrecommitmentIndexV1,
    ) -> None:
        expected = self._build_precommitment_index(capability, index.spec_ref, index.corpus_ref)
        if index != expected:
            raise MechanismCapabilityError("mechanism precommitment index binding differs")
        if (
            index.spec_ref.relative_path
            != f"adapter-blobs/{MECHANISM_ARTIFACT_NAMESPACE_V1}/{self._round_key(capability.campaign_id, capability.round_index)}-spec.bin"
        ):
            raise MechanismArtifactCorrupt("mechanism spec sidecar path is not canonical")

    @staticmethod
    def _build_precommitment_index(
        capability: MechanismExtensionCapabilityV1,
        spec_ref: ArtifactRefV5,
        corpus_ref: ArtifactRefV5,
    ) -> MechanismPrecommitmentIndexV1:
        return MechanismPrecommitmentIndexV1(
            schema_version=1,
            campaign_id=capability.campaign_id,
            round_index=capability.round_index,
            precommitment_id=capability.precommitment_id,
            manifest_ref=capability.manifest_ref,
            manifest_sha256=capability.manifest_ref.sha256,
            manifest_source_identity_sha256=capability.manifest_source_identity_sha256,
            extension_source_sha256=capability.extension_source_sha256,
            round_intent_sha256=round_intent_sha256_v1(capability.round_intent),
            parent_revision_sha256=capability.parent_revision_sha256,
            parent_revision_ref=capability.parent_revision_ref,
            parent_source_bundle_ref=capability.parent_source_bundle_ref,
            parent_source_bundle_sha256=capability.parent_source_bundle.sha256,
            hypothesis_sha256=capability.round_intent.hypothesis.sha256,
            spec_sha256=capability.spec.sha256,
            corpus_sha256=capability.corpus.sha256,
            resource_budget_sha256=capability.resource_budget.sha256,
            resource_budget_canonical_json=_budget_primitive_json(capability.resource_budget),
            execution_kind=capability.execution_kind,
            authorization_sha256=capability.authorization_sha256,
            spec_ref=spec_ref,
            corpus_ref=corpus_ref,
        )

    def append_precommitment(self, capability: MechanismExtensionCapabilityV1) -> MechanismPersistedPrecommitmentV1:
        self._authenticate_capability(capability)
        existing = self._load_precommitment_index(capability)
        if existing is not None:
            self._validate_precommitment_index(capability, existing)
            spec, corpus = self._load_precommitment_payloads(capability, existing)
            return MechanismPersistedPrecommitmentV1(existing, spec, corpus, existing.spec_ref, existing.corpus_ref)
        if self._author_requests_exist(capability.campaign_id, capability.round_index):
            raise MechanismChronologyError("author request already exists before a mechanism precommitment")
        key = self._round_key(capability.campaign_id, capability.round_index)
        spec_ref = self.repository.append_binary_state(
            namespace=MECHANISM_ARTIFACT_NAMESPACE_V1,
            key=f"{key}-spec",
            content=_wire("spec", _encode_spec(capability.spec)),
        )
        corpus_ref = self.repository.append_binary_state(
            namespace=MECHANISM_ARTIFACT_NAMESPACE_V1,
            key=f"{key}-corpus",
            content=_wire("corpus", _encode_corpus(capability.corpus)),
        )
        index = self._build_precommitment_index(capability, spec_ref, corpus_ref)
        try:
            self.repository.append_typed_state(
                namespace=MECHANISM_ARTIFACT_NAMESPACE_V1,
                key=key,
                value=index,
            )
        except ArtifactRepositoryFailureV5 as exc:
            raise MechanismChronologyError("mechanism precommitment index could not be published") from exc
        return MechanismPersistedPrecommitmentV1(index, capability.spec, capability.corpus, spec_ref, corpus_ref)

    def _load_precommitment_payloads(
        self,
        capability: MechanismExtensionCapabilityV1,
        index: MechanismPrecommitmentIndexV1,
    ) -> tuple[MechanismExperimentSpecV1, MechanismObservationCorpusV1]:
        self._validate_precommitment_index_identity(
            campaign_id=capability.campaign_id,
            round_index=capability.round_index,
            index=index,
        )
        try:
            spec, corpus = self._load_precommitment_payloads_by_identity(
                campaign_id=capability.campaign_id,
                round_index=capability.round_index,
                index=index,
            )
            if spec.sha256 != index.spec_sha256:
                raise MechanismArtifactCorrupt("mechanism spec sidecar identity differs")
            if corpus.sha256 != index.corpus_sha256:
                raise MechanismArtifactCorrupt("mechanism corpus sidecar identity differs")
        except MechanismCapabilityError:
            raise
        except (ArtifactRepositoryFailureV5, TypeError, ValueError, ArithmeticError) as exc:
            raise MechanismArtifactCorrupt("mechanism precommitment sidecars are unreadable") from exc
        return spec, corpus

    def _load_precommitment_payloads_by_identity(
        self,
        *,
        campaign_id: str,
        round_index: int,
        index: MechanismPrecommitmentIndexV1,
    ) -> tuple[MechanismExperimentSpecV1, MechanismObservationCorpusV1]:
        key = self._round_key(campaign_id, round_index)
        try:
            spec_raw = self.repository.load_binary_state(
                namespace=MECHANISM_ARTIFACT_NAMESPACE_V1,
                key=f"{key}-spec",
                reference=index.spec_ref,
                maximum_bytes=MECHANISM_ARTIFACT_MAX_BYTES_V1,
            )
            spec = _decode_spec(_unwire(spec_raw, "spec"))
            if type(spec) is not MechanismExperimentSpecV1:
                raise MechanismArtifactCorrupt("mechanism spec sidecar identity differs")
            corpus_raw = self.repository.load_binary_state(
                namespace=MECHANISM_ARTIFACT_NAMESPACE_V1,
                key=f"{key}-corpus",
                reference=index.corpus_ref,
                maximum_bytes=MECHANISM_ARTIFACT_MAX_BYTES_V1,
            )
            corpus = _decode_corpus(_unwire(corpus_raw, "corpus"), spec)
            if type(corpus) is not MechanismObservationCorpusV1:
                raise MechanismArtifactCorrupt("mechanism corpus sidecar identity differs")
        except MechanismCapabilityError:
            raise
        except (ArtifactRepositoryFailureV5, TypeError, ValueError, ArithmeticError) as exc:
            raise MechanismArtifactCorrupt("mechanism precommitment sidecars are unreadable") from exc
        return spec, corpus

    def load_precommitment(
        self, capability: MechanismExtensionCapabilityV1
    ) -> MechanismPersistedPrecommitmentV1 | None:
        self._authenticate_capability(capability)
        index = self._load_precommitment_index(capability)
        if index is None:
            return None
        self._validate_precommitment_index(capability, index)
        spec, corpus = self._load_precommitment_payloads(capability, index)
        return MechanismPersistedPrecommitmentV1(index, spec, corpus, index.spec_ref, index.corpus_ref)

    def _load_experiment_index(
        self,
        experiment_id: str,
        phase: str,
        *,
        repair: bool = True,
    ) -> MechanismExperimentIndexV1 | None:
        try:
            return self.repository.load_typed_state(
                namespace=MECHANISM_ARTIFACT_NAMESPACE_V1,
                key=self._experiment_key(experiment_id, phase),
                value_type=MechanismExperimentIndexV1,
                repair=repair,
            )
        except (ArtifactRepositoryFailureV5, TypeError, ValueError) as exc:
            raise MechanismArtifactCorrupt("mechanism experiment index is unreadable") from exc

    @staticmethod
    def _candidate_index(
        bound: MechanismBoundCandidateV1,
        phase: Literal["binding", "run", "report", "complete"],
        binding_ref: ArtifactRefV5,
        run_ref: ArtifactRefV5 | None = None,
        report_ref: ArtifactRefV5 | None = None,
    ) -> MechanismExperimentIndexV1:
        capability = bound.capability
        return MechanismExperimentIndexV1(
            schema_version=1,
            phase=phase,
            campaign_id=capability.campaign_id,
            round_index=capability.round_index,
            experiment_id=bound.experiment_id,
            precommitment_id=capability.precommitment_id,
            spec_sha256=capability.spec.sha256,
            corpus_sha256=capability.corpus.sha256,
            binding_sha256=bound.binding.sha256,
            candidate_revision_sha256=bound.candidate_revision.sha256,
            candidate_source_bundle_sha256=bound.candidate_source_bundle.sha256,
            binding_ref=binding_ref,
            run_ref=run_ref,
            report_ref=report_ref,
        )

    def append_binding(self, bound: MechanismBoundCandidateV1) -> ArtifactRefV5:
        self._authenticate_capability(bound.capability)
        existing = self._load_experiment_index(bound.experiment_id, "binding")
        if existing is not None:
            expected = self._candidate_index(bound, "binding", existing.binding_ref)
            if existing != expected:
                raise MechanismCapabilityError("existing mechanism binding index differs")
            raw = self.repository.load_binary_state(
                namespace=MECHANISM_ARTIFACT_NAMESPACE_V1,
                key=f"{bound.experiment_id}-binding",
                reference=existing.binding_ref,
                maximum_bytes=MECHANISM_ARTIFACT_MAX_BYTES_V1,
            )
            decoded = _decode_contract(raw, "binding", MechanismObservationBindingV1)
            if decoded != bound.binding:
                raise MechanismArtifactCorrupt("existing mechanism binding sidecar differs")
            return existing.binding_ref
        reference = self.repository.append_binary_state(
            namespace=MECHANISM_ARTIFACT_NAMESPACE_V1,
            key=f"{bound.experiment_id}-binding",
            content=_wire("binding", _encode_contract("binding", bound.binding)),
        )
        index = self._candidate_index(bound, "binding", reference)
        self.repository.append_typed_state(
            namespace=MECHANISM_ARTIFACT_NAMESPACE_V1,
            key=self._experiment_key(bound.experiment_id, "binding"),
            value=index,
        )
        return reference

    def load_binding(self, bound: MechanismBoundCandidateV1) -> MechanismBoundCandidateV1:
        self._authenticate_capability(bound.capability)
        index = self._load_experiment_index(bound.experiment_id, "binding")
        if index is None:
            raise MechanismArtifactCorrupt("mechanism binding index is missing")
        expected = self._candidate_index(bound, "binding", index.binding_ref)
        if index != expected:
            raise MechanismCapabilityError("mechanism binding index differs from candidate")
        raw = self.repository.load_binary_state(
            namespace=MECHANISM_ARTIFACT_NAMESPACE_V1,
            key=f"{bound.experiment_id}-binding",
            reference=index.binding_ref,
            maximum_bytes=MECHANISM_ARTIFACT_MAX_BYTES_V1,
        )
        decoded = _decode_contract(raw, "binding", MechanismObservationBindingV1)
        if decoded != bound.binding:
            raise MechanismArtifactCorrupt("mechanism binding sidecar differs from candidate")
        return bound

    def append_run(self, bound: MechanismBoundCandidateV1, run: MechanismObservationRunV1) -> ArtifactRefV5:
        self._authenticate_capability(bound.capability)
        self.append_binding(bound)
        if run.binding != bound.binding:
            raise MechanismCapabilityError("mechanism run binding differs from candidate")
        validate_mechanism_observation_corpus_v1(bound.capability.spec, bound.binding, run.corpus)
        existing = self._load_experiment_index(bound.experiment_id, "run")
        content = _wire("run", _encode_run(run))
        if existing is not None:
            if existing.run_ref is None:
                raise MechanismArtifactCorrupt("mechanism run index has no run sidecar")
            expected = self._candidate_index(bound, "run", existing.binding_ref, existing.run_ref)
            if existing != expected:
                raise MechanismCapabilityError("existing mechanism run index differs")
            raw = self.repository.load_binary_state(
                namespace=MECHANISM_ARTIFACT_NAMESPACE_V1,
                key=f"{bound.experiment_id}-run",
                reference=existing.run_ref,
                maximum_bytes=MECHANISM_ARTIFACT_MAX_BYTES_V1,
            )
            if _unwire(raw, "run") != _encode_run(run):
                raise MechanismArtifactCorrupt("existing mechanism run differs")
            return existing.run_ref
        reference = self.repository.append_binary_state(
            namespace=MECHANISM_ARTIFACT_NAMESPACE_V1,
            key=f"{bound.experiment_id}-run",
            content=content,
        )
        binding_index = self._load_experiment_index(bound.experiment_id, "binding")
        if binding_index is None:
            raise MechanismArtifactCorrupt("mechanism binding index disappeared")
        index = self._candidate_index(bound, "run", binding_index.binding_ref, reference)
        self.repository.append_typed_state(
            namespace=MECHANISM_ARTIFACT_NAMESPACE_V1, key=self._experiment_key(bound.experiment_id, "run"), value=index
        )
        return reference

    def load_run(self, bound: MechanismBoundCandidateV1) -> MechanismObservationRunV1 | None:
        self._authenticate_capability(bound.capability)
        index = self._load_experiment_index(bound.experiment_id, "run")
        if index is None:
            try:
                orphan = self.repository.has_binary_state(
                    namespace=MECHANISM_ARTIFACT_NAMESPACE_V1,
                    key=f"{bound.experiment_id}-run",
                )
            except ArtifactRepositoryFailureV5 as exc:
                raise MechanismArtifactCorrupt("mechanism run sidecar presence is unreadable") from exc
            if orphan:
                raise MechanismArtifactCorrupt("mechanism run sidecar exists without an authenticated index")
            return None
        if index.run_ref is None:
            raise MechanismArtifactCorrupt("mechanism run index is incomplete")
        expected = self._candidate_index(bound, "run", index.binding_ref, index.run_ref)
        if index != expected:
            raise MechanismCapabilityError("mechanism run index binding differs")
        binding_raw = self.repository.load_binary_state(
            namespace=MECHANISM_ARTIFACT_NAMESPACE_V1,
            key=f"{bound.experiment_id}-binding",
            reference=index.binding_ref,
            maximum_bytes=MECHANISM_ARTIFACT_MAX_BYTES_V1,
        )
        binding = _decode_contract(binding_raw, "binding", MechanismObservationBindingV1)
        if binding != bound.binding:
            raise MechanismArtifactCorrupt("mechanism run binding sidecar differs")
        raw = self.repository.load_binary_state(
            namespace=MECHANISM_ARTIFACT_NAMESPACE_V1,
            key=f"{bound.experiment_id}-run",
            reference=index.run_ref,
            maximum_bytes=MECHANISM_ARTIFACT_MAX_BYTES_V1,
        )
        try:
            return _decode_run(
                _unwire(raw, "run"), spec=bound.capability.spec, binding=bound.binding, corpus=bound.capability.corpus
            )
        except MechanismCapabilityError:
            raise
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise MechanismArtifactCorrupt("mechanism run sidecar is corrupt") from exc

    @staticmethod
    def _validate_matched_evaluations(
        bound: MechanismBoundCandidateV1,
        matched_evaluations: tuple[MechanismEvaluatorMatchV1, ...],
    ) -> None:
        if type(matched_evaluations) is not tuple or any(
            type(item) is not MechanismEvaluatorMatchV1 for item in matched_evaluations
        ):
            raise MechanismCapabilityError("mechanism evaluator matches are not typed")
        for item in matched_evaluations:
            if (
                item.parent_revision != bound.capability.parent_revision
                or item.parent_source_bundle != bound.capability.parent_source_bundle
                or item.candidate_revision != bound.candidate_revision
                or item.candidate_source_bundle != bound.candidate_source_bundle
            ):
                raise MechanismCapabilityError("mechanism evaluator match differs from bound candidate authority")

    def _ensure_complete_index(
        self,
        bound: MechanismBoundCandidateV1,
        report_index: MechanismExperimentIndexV1,
    ) -> MechanismExperimentIndexV1:
        """Authenticate predecessors, then repair only a missing final index."""

        binding_index = self._load_experiment_index(bound.experiment_id, "binding", repair=False)
        run_index = self._load_experiment_index(bound.experiment_id, "run", repair=False)
        if (
            binding_index is None
            or run_index is None
            or run_index.run_ref is None
            or report_index.binding_ref != binding_index.binding_ref
            or report_index.run_ref != run_index.run_ref
            or report_index.report_ref is None
        ):
            raise MechanismArtifactCorrupt("mechanism report predecessors are incomplete")
        self.load_binding(bound)
        if self.load_run(bound) is None:
            raise MechanismArtifactCorrupt("mechanism report predecessor run is missing")
        expected = self._candidate_index(
            bound,
            "complete",
            binding_index.binding_ref,
            run_index.run_ref,
            report_index.report_ref,
        )
        complete = self._load_experiment_index(bound.experiment_id, "complete", repair=False)
        if complete is None:
            try:
                self.repository.append_typed_state(
                    namespace=MECHANISM_ARTIFACT_NAMESPACE_V1,
                    key=self._experiment_key(bound.experiment_id, "complete"),
                    value=expected,
                )
            except ArtifactRepositoryFailureV5 as exc:
                raise MechanismArtifactCorrupt("mechanism complete index could not be repaired") from exc
            complete = self._load_experiment_index(bound.experiment_id, "complete", repair=False)
        if complete != expected:
            raise MechanismCapabilityError("mechanism complete index differs from authenticated predecessors")
        return complete

    def append_report(
        self,
        bound: MechanismBoundCandidateV1,
        run: MechanismObservationRunV1,
        *,
        matched_evaluations: tuple[MechanismEvaluatorMatchV1, ...] = (),
    ) -> ArtifactRefV5:
        self._authenticate_capability(bound.capability)
        self.append_run(bound, run)
        self._validate_matched_evaluations(bound, matched_evaluations)
        report = build_mechanism_evidence_report_v1(
            bound.capability.spec,
            bound.binding,
            run,
            matched_evaluations=matched_evaluations,
        )
        content = _wire("report", _encode_contract("report", report))
        existing = self._load_experiment_index(bound.experiment_id, "report")
        if existing is not None:
            if existing.report_ref is None:
                raise MechanismArtifactCorrupt("mechanism report index has no report sidecar")
            expected = self._candidate_index(
                bound, "report", existing.binding_ref, existing.run_ref, existing.report_ref
            )
            if existing != expected:
                raise MechanismCapabilityError("existing mechanism report index differs")
            raw = self.repository.load_binary_state(
                namespace=MECHANISM_ARTIFACT_NAMESPACE_V1,
                key=f"{bound.experiment_id}-report",
                reference=existing.report_ref,
                maximum_bytes=MECHANISM_ARTIFACT_MAX_BYTES_V1,
            )
            if _unwire(raw, "report") != _unwire(content, "report"):
                raise MechanismArtifactCorrupt("existing mechanism report differs")
            report_ref = existing.report_ref
            self._ensure_complete_index(bound, existing)
        else:
            report_ref = self.repository.append_binary_state(
                namespace=MECHANISM_ARTIFACT_NAMESPACE_V1,
                key=f"{bound.experiment_id}-report",
                content=content,
            )
            run_index = self._load_experiment_index(bound.experiment_id, "run")
            binding_index = self._load_experiment_index(bound.experiment_id, "binding")
            if run_index is None or binding_index is None or run_index.run_ref is None:
                raise MechanismArtifactCorrupt("mechanism report predecessors are missing")
            report_index = self._candidate_index(
                bound, "report", binding_index.binding_ref, run_index.run_ref, report_ref
            )
            self.repository.append_typed_state(
                namespace=MECHANISM_ARTIFACT_NAMESPACE_V1,
                key=self._experiment_key(bound.experiment_id, "report"),
                value=report_index,
            )
            complete_index = self._candidate_index(
                bound, "complete", binding_index.binding_ref, run_index.run_ref, report_ref
            )
            self.repository.append_typed_state(
                namespace=MECHANISM_ARTIFACT_NAMESPACE_V1,
                key=self._experiment_key(bound.experiment_id, "complete"),
                value=complete_index,
            )
            self._ensure_complete_index(bound, report_index)
        return report_ref

    def load_report(self, bound: MechanismBoundCandidateV1):
        self._authenticate_capability(bound.capability)
        index = self._load_experiment_index(bound.experiment_id, "report")
        if index is None or index.report_ref is None:
            return None
        expected = self._candidate_index(bound, "report", index.binding_ref, index.run_ref, index.report_ref)
        if index != expected:
            raise MechanismCapabilityError("mechanism report index binding differs")
        raw = self.repository.load_binary_state(
            namespace=MECHANISM_ARTIFACT_NAMESPACE_V1,
            key=f"{bound.experiment_id}-report",
            reference=index.report_ref,
            maximum_bytes=MECHANISM_ARTIFACT_MAX_BYTES_V1,
        )
        try:
            report = _decode_contract(raw, "report", MechanismEvidenceReportV1)
        except (MechanismCapabilityError, ArtifactRepositoryFailureV5):
            raise
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise MechanismArtifactCorrupt("mechanism report sidecar is corrupt") from exc
        self._ensure_complete_index(bound, index)
        return report

    def load_existing_evidence(
        self,
        *,
        campaign_id: str,
        round_index: int,
        experiment_id: str,
        manifest_ref: ArtifactRefV5,
        manifest_source_identity_sha256: str,
        precommitment_id: str,
        expected_parent_revision_sha256: str,
        expected_parent_revision_ref: ArtifactRefV5,
        expected_parent_source_bundle_sha256: str,
        expected_spec_sha256: str,
        expected_corpus_sha256: str,
        expected_candidate_record_ref: ArtifactRefV5,
        expected_candidate_revision_sha256: str,
        expected_candidate_source_bundle_sha256: str,
        expected_candidate_source_bundle_ref: ArtifactRefV5,
        expected_binding_sha256: str | None = None,
    ) -> MechanismPersistedEvidenceV1 | None:
        """Load complete evidence using existing record authority only.

        The caller supplies identities from an independently authenticated
        campaign manifest and legacy experiment record.  This method performs
        no capability revalidation, worker allocation, opt-in issuance, or
        create/repair operation; persisted sidecars therefore cannot become a
        new executable permission after restart.
        """

        campaign_id = _campaign(campaign_id)
        round_index = _positive(round_index, "mechanism round")
        experiment_id = _digest(experiment_id, "mechanism experiment ID")
        if type(manifest_ref) is not ArtifactRefV5:
            raise ValueError("mechanism manifest reference is invalid")
        manifest_source_identity_sha256 = _digest(
            manifest_source_identity_sha256,
            "mechanism manifest source identity SHA-256",
        )
        precommitment_id = _text(precommitment_id, "mechanism precommitment ID", maximum=128)
        expected_parent_revision_sha256 = _digest(
            expected_parent_revision_sha256,
            "expected mechanism parent revision SHA-256",
        )
        if type(expected_parent_revision_ref) is not ArtifactRefV5:
            raise ValueError("expected mechanism parent revision reference is invalid")
        expected_parent_source_bundle_sha256 = _digest(
            expected_parent_source_bundle_sha256,
            "expected mechanism parent source bundle SHA-256",
        )
        expected_spec_sha256 = _digest(expected_spec_sha256, "expected mechanism spec SHA-256")
        expected_corpus_sha256 = _digest(expected_corpus_sha256, "expected mechanism corpus SHA-256")
        if type(expected_candidate_record_ref) is not ArtifactRefV5:
            raise ValueError("expected mechanism candidate record reference is invalid")
        expected_candidate_revision_sha256 = _digest(
            expected_candidate_revision_sha256,
            "expected mechanism candidate revision SHA-256",
        )
        expected_candidate_source_bundle_sha256 = _digest(
            expected_candidate_source_bundle_sha256,
            "expected mechanism candidate source bundle SHA-256",
        )
        if type(expected_candidate_source_bundle_ref) is not ArtifactRefV5:
            raise ValueError("expected mechanism candidate source bundle reference is invalid")
        if expected_candidate_source_bundle_ref.sha256 != expected_candidate_source_bundle_sha256:
            raise ValueError("expected mechanism candidate authority references differ")
        if expected_binding_sha256 is not None:
            expected_binding_sha256 = _digest(expected_binding_sha256, "expected mechanism binding SHA-256")

        try:
            authenticated = authenticate_campaign_manifest_v5(
                repository=self.repository,
                manifest_ref=manifest_ref,
            )
        except (ArtifactRepositoryFailureV5, ManifestAuthenticationFailureV5, TypeError, ValueError) as exc:
            raise MechanismCapabilityError("mechanism manifest authority is unavailable") from exc
        if (
            authenticated.manifest.campaign_id != campaign_id
            or manifest_source_identity_sha256_v1(authenticated) != manifest_source_identity_sha256
        ):
            raise MechanismCapabilityError("mechanism manifest authority differs from this repository")

        precommitment = self._load_precommitment_index_by_identity(
            campaign_id=campaign_id,
            round_index=round_index,
        )
        if precommitment is None:
            return None
        self._validate_precommitment_index_identity(
            campaign_id=campaign_id,
            round_index=round_index,
            index=precommitment,
        )
        if (
            precommitment.manifest_ref != manifest_ref
            or precommitment.manifest_source_identity_sha256 != manifest_source_identity_sha256
            or precommitment.precommitment_id != precommitment_id
            or precommitment.parent_revision_sha256 != expected_parent_revision_sha256
            or precommitment.parent_revision_ref != expected_parent_revision_ref
            or precommitment.parent_source_bundle_ref.sha256 != expected_parent_source_bundle_sha256
            or precommitment.parent_source_bundle_sha256 != expected_parent_source_bundle_sha256
            or precommitment.spec_sha256 != expected_spec_sha256
            or precommitment.corpus_sha256 != expected_corpus_sha256
        ):
            raise MechanismCapabilityError("mechanism precommitment differs from existing authority")
        spec, corpus = self._load_precommitment_payloads_by_identity(
            campaign_id=campaign_id,
            round_index=round_index,
            index=precommitment,
        )
        if spec.sha256 != expected_spec_sha256 or corpus.sha256 != expected_corpus_sha256:
            raise MechanismArtifactCorrupt("mechanism precommitment payload identity differs")
        if (
            precommitment.round_intent_sha256 != spec.round_intent_sha256
            or spec.parent_revision_sha256 != expected_parent_revision_sha256
        ):
            raise MechanismCapabilityError("mechanism saved round intent differs from its frozen spec")
        try:
            parent_revision = self.repository.load_typed_artifact(
                precommitment.parent_revision_ref,
                value_type=PolicyRevisionIdentityV5,
            )
            parent_source_bundle = self.repository.load_typed_artifact(
                precommitment.parent_source_bundle_ref,
                value_type=SourceBundleV5,
            )
        except (ArtifactRepositoryFailureV5, TypeError, ValueError) as exc:
            raise MechanismCapabilityError("mechanism parent authority is unavailable") from exc
        if (
            parent_revision.sha256 != expected_parent_revision_sha256
            or parent_source_bundle.sha256 != expected_parent_source_bundle_sha256
            or parent_revision.editable_source_sha256
            != tuple((item.path, item.sha256) for item in parent_source_bundle.files)
        ):
            raise MechanismCapabilityError("mechanism parent authority differs from this repository")

        try:
            candidate_record = self.repository.load_experiment(expected_candidate_record_ref)
            candidate_stored = StoredExperimentRecordV5(
                reference=expected_candidate_record_ref,
                record=candidate_record,
            )
            candidate_source_bundle = self.repository.load_typed_artifact(
                expected_candidate_source_bundle_ref,
                value_type=SourceBundleV5,
            )
        except (ArtifactRepositoryFailureV5, TypeError, ValueError) as exc:
            raise MechanismCapabilityError("mechanism candidate checkpoint authority is unavailable") from exc
        self._require_checkpoint_record(expected_candidate_record_ref)
        candidate_revision = candidate_record.policy_revision
        if (
            candidate_stored.record.experiment_id != experiment_id
            or candidate_stored.record.status not in {"evaluated", "zero_trade"}
            or candidate_revision is None
            or candidate_revision.sha256 != expected_candidate_revision_sha256
            or candidate_source_bundle.sha256 != expected_candidate_source_bundle_sha256
            or expected_candidate_source_bundle_ref not in candidate_stored.record.artifact_refs
            or expected_candidate_source_bundle_ref.relative_path
            != f"adapter-state/policy-source/{candidate_revision.sha256}.json"
            or candidate_revision.editable_source_sha256
            != tuple((item.path, item.sha256) for item in candidate_source_bundle.files)
            or candidate_stored.record.round_index != round_index
            or candidate_stored.record.parent_revision_sha256 != expected_parent_revision_sha256
            or candidate_stored.record.template.parent_revision_sha256 != expected_parent_revision_sha256
            or candidate_stored.record.hypothesis.sha256 != spec.hypothesis_sha256
            or candidate_stored.record.experiment_identity.discovery_plan_sha256
            != authenticated.panel_plan.discovery_plan_sha256
            or candidate_stored.record.pit_data_scope != authenticated.manifest.pit_data_scope
            or candidate_stored.record.semantic_mode != authenticated.manifest.semantic_mode
        ):
            raise MechanismCapabilityError("mechanism candidate checkpoint authority differs")
        if candidate_stored.record.campaign_evidence is not None:
            try:
                validate_campaign_evidence_v5(
                    candidate_stored.record.campaign_evidence,
                    panel_plan=authenticated.panel_plan,
                    evaluator_contract=authenticated.evaluator_contract,
                    policy_identity_sha256=candidate_revision.sha256,
                )
            except (TypeError, ValueError, ArithmeticError) as exc:
                raise MechanismCapabilityError("mechanism candidate campaign authority differs") from exc

        complete = self._load_experiment_index(experiment_id, "complete", repair=False)
        if complete is None:
            return None
        if (
            complete.phase != "complete"
            or complete.campaign_id != campaign_id
            or complete.round_index != round_index
            or complete.experiment_id != experiment_id
            or complete.precommitment_id != precommitment_id
            or complete.spec_sha256 != expected_spec_sha256
            or complete.corpus_sha256 != expected_corpus_sha256
            or complete.candidate_revision_sha256 != expected_candidate_revision_sha256
            or complete.candidate_source_bundle_sha256 != expected_candidate_source_bundle_sha256
            or (expected_binding_sha256 is not None and complete.binding_sha256 != expected_binding_sha256)
            or complete.run_ref is None
            or complete.report_ref is None
        ):
            raise MechanismCapabilityError("mechanism complete index differs from existing authority")

        try:
            binding_raw = self.repository.load_binary_state(
                namespace=MECHANISM_ARTIFACT_NAMESPACE_V1,
                key=f"{experiment_id}-binding",
                reference=complete.binding_ref,
                maximum_bytes=MECHANISM_ARTIFACT_MAX_BYTES_V1,
            )
            binding = _decode_contract(binding_raw, "binding", MechanismObservationBindingV1)
            if type(binding) is not MechanismObservationBindingV1 or binding.sha256 != complete.binding_sha256:
                raise MechanismArtifactCorrupt("mechanism binding sidecar identity differs")
            if binding.candidate_bytes_sha256 != expected_candidate_source_bundle_sha256:
                raise MechanismCapabilityError("mechanism binding candidate source differs from checkpoint authority")
            run_raw = self.repository.load_binary_state(
                namespace=MECHANISM_ARTIFACT_NAMESPACE_V1,
                key=f"{experiment_id}-run",
                reference=complete.run_ref,
                maximum_bytes=MECHANISM_ARTIFACT_MAX_BYTES_V1,
            )
            run = _decode_run(_unwire(run_raw, "run"), spec=spec, binding=binding, corpus=corpus)
            report_raw = self.repository.load_binary_state(
                namespace=MECHANISM_ARTIFACT_NAMESPACE_V1,
                key=f"{experiment_id}-report",
                reference=complete.report_ref,
                maximum_bytes=MECHANISM_ARTIFACT_MAX_BYTES_V1,
            )
            report = _decode_contract(report_raw, "report", MechanismEvidenceReportV1)
            if type(report) is not MechanismEvidenceReportV1 or report.binding != binding:
                raise MechanismArtifactCorrupt("mechanism report binding differs")
            if any(
                context.context.candidate_policy_identity_sha256 != expected_candidate_revision_sha256
                or context.context.candidate_source_bundle_sha256 != expected_candidate_source_bundle_sha256
                for context in report.consequence_contexts
            ):
                raise MechanismCapabilityError("mechanism report candidate context differs from checkpoint authority")
            validate_mechanism_report_against_spec_v1(spec, report)
        except MechanismCapabilityError:
            raise
        except (ArtifactRepositoryFailureV5, TypeError, ValueError, ArithmeticError) as exc:
            raise MechanismArtifactCorrupt("mechanism complete evidence is unreadable") from exc
        return MechanismPersistedEvidenceV1(
            index=complete,
            spec=spec,
            corpus=corpus,
            binding=binding,
            run=run,
            report=report,
            parent_source_bundle_sha256=expected_parent_source_bundle_sha256,
        )

    def load_existing_evidence_for_record(
        self,
        *,
        campaign_id: str,
        round_index: int,
        manifest_ref: ArtifactRefV5,
        manifest_source_identity_sha256: str,
        stored_record: StoredExperimentRecordV5,
    ) -> tuple[MechanismPersistedEvidenceV1, RoundIntentPayloadV5] | None:
        """Discover one persisted finding through authenticated legacy authority.

        This restart path is deliberately narrower than capability loading.  It
        authenticates the manifest, round-intent event, checkpoint record, and
        the record's inline policy-source edge, then delegates to the existing
        complete-index reader.  Every state read uses ``repair=False`` (or an
        immutable artifact reader), so an orphan cannot mint an authority file
        while historical memory is being projected into a new request.
        """

        campaign_id = _campaign(campaign_id)
        round_index = _positive(round_index, "mechanism round")
        if type(manifest_ref) is not ArtifactRefV5:
            raise ValueError("mechanism manifest reference is invalid")
        manifest_source_identity_sha256 = _digest(
            manifest_source_identity_sha256,
            "mechanism manifest source identity SHA-256",
        )
        if type(stored_record) is not StoredExperimentRecordV5:
            raise ValueError("mechanism stored record is invalid")

        try:
            authenticated = authenticate_campaign_manifest_v5(
                repository=self.repository,
                manifest_ref=manifest_ref,
            )
        except (ArtifactRepositoryFailureV5, ManifestAuthenticationFailureV5, TypeError, ValueError) as exc:
            raise MechanismCapabilityError("mechanism manifest authority is unavailable") from exc
        if (
            authenticated.manifest.campaign_id != campaign_id
            or manifest_source_identity_sha256_v1(authenticated) != manifest_source_identity_sha256
        ):
            raise MechanismCapabilityError("mechanism manifest authority differs from this repository")

        try:
            record = self.repository.load_experiment(stored_record.reference)
            self._require_checkpoint_record(stored_record.reference)
        except (ArtifactRepositoryFailureV5, TypeError, ValueError) as exc:
            raise MechanismCapabilityError("mechanism candidate checkpoint authority is unavailable") from exc
        if record != stored_record.record:
            raise MechanismCapabilityError("mechanism candidate record differs from checkpoint authority")
        if (
            record.round_index != round_index
            or record.status not in {"evaluated", "zero_trade"}
            or record.pit_data_scope != authenticated.manifest.pit_data_scope
            or record.semantic_mode != authenticated.manifest.semantic_mode
            or record.experiment_identity.discovery_plan_sha256 != authenticated.panel_plan.discovery_plan_sha256
        ):
            raise MechanismCapabilityError("mechanism candidate record semantic authority differs")

        index = self._load_precommitment_index_by_identity(
            campaign_id=campaign_id,
            round_index=round_index,
        )
        if index is None:
            return None
        self._validate_precommitment_index_identity(
            campaign_id=campaign_id,
            round_index=round_index,
            index=index,
        )

        try:
            events = self.repository.load_round_events(campaign_id=campaign_id, round_index=round_index)
            payloads = tuple(
                self.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
                for event in events
            )
        except (ArtifactRepositoryFailureV5, TypeError, ValueError) as exc:
            raise MechanismCapabilityError("mechanism round history is unavailable") from exc
        intents = tuple(item for item in payloads if type(item) is RoundIntentPayloadV5)
        if len(intents) != 1:
            raise MechanismCapabilityError("mechanism round history lacks one authenticated intent")
        intent = intents[0]

        if (
            index.manifest_ref != manifest_ref
            or index.manifest_source_identity_sha256 != manifest_source_identity_sha256
            or index.round_intent_sha256 != round_intent_sha256_v1(intent)
            or intent.parent_revision_sha256 != index.parent_revision_sha256
            or intent.hypothesis.sha256 != index.hypothesis_sha256
            or intent.discovery_plan_sha256 != authenticated.panel_plan.discovery_plan_sha256
            or intent.pit_data_scope != authenticated.manifest.pit_data_scope
            or intent.semantic_mode != authenticated.manifest.semantic_mode
            or record.hypothesis.sha256 != index.hypothesis_sha256
            or record.parent_revision_sha256 != index.parent_revision_sha256
        ):
            raise MechanismCapabilityError("mechanism saved round intent differs from record authority")

        candidate_revision = record.policy_revision
        if candidate_revision is None:
            raise MechanismCapabilityError("mechanism candidate lacks policy revision authority")
        candidate_source_refs = tuple(
            reference
            for reference in record.artifact_refs
            if reference.relative_path == f"adapter-state/policy-source/{candidate_revision.sha256}.json"
        )
        if len(candidate_source_refs) != 1:
            raise MechanismCapabilityError("mechanism candidate lacks one inline policy-source authority")
        candidate_source_ref = candidate_source_refs[0]
        if candidate_source_ref.sha256 == "":
            raise MechanismCapabilityError("mechanism candidate source authority is empty")

        persisted = self.load_existing_evidence(
            campaign_id=campaign_id,
            round_index=round_index,
            experiment_id=record.experiment_id,
            manifest_ref=manifest_ref,
            manifest_source_identity_sha256=manifest_source_identity_sha256,
            precommitment_id=index.precommitment_id,
            expected_parent_revision_sha256=index.parent_revision_sha256,
            expected_parent_revision_ref=index.parent_revision_ref,
            expected_parent_source_bundle_sha256=index.parent_source_bundle_sha256,
            expected_spec_sha256=index.spec_sha256,
            expected_corpus_sha256=index.corpus_sha256,
            expected_candidate_record_ref=stored_record.reference,
            expected_candidate_revision_sha256=candidate_revision.sha256,
            expected_candidate_source_bundle_sha256=candidate_source_ref.sha256,
            expected_candidate_source_bundle_ref=candidate_source_ref,
        )
        if persisted is None:
            return None
        return persisted, intent


class MechanismRuntimeExtensionV1:
    """Narrow runtime adapter used only when an explicit capability is supplied."""

    def __init__(
        self,
        repository: MechanismArtifactRepositoryV5,
        capability: MechanismExtensionCapabilityV1,
        *,
        worker_factory: MechanismWorkerFactoryV1 | None = None,
    ) -> None:
        if (
            type(repository) is not MechanismArtifactRepositoryV5
            or type(capability) is not MechanismExtensionCapabilityV1
        ):
            raise ValueError("mechanism runtime extension requires its V5 repository and capability")
        if worker_factory is not None and not callable(worker_factory):
            raise ValueError("mechanism worker factory is invalid")
        self.repository = repository
        self.capability = capability
        self.worker_factory = worker_factory
        self._precommitted = False
        self._bound: dict[str, MechanismBoundCandidateV1] = {}
        self._runs: dict[str, MechanismObservationRunV1] = {}
        self._reports: dict[str, MechanismEvidenceReportV1] = {}

    def before_authoring(
        self,
        *,
        round_intent: RoundIntentPayloadV5,
        campaign_id: str | None = None,
        round_index: int | None = None,
        parent_candidate: object | None = None,
    ) -> None:
        if campaign_id is not None and campaign_id != self.capability.campaign_id:
            raise MechanismCapabilityError("mechanism campaign differs from runtime round")
        if round_index is not None and round_index != self.capability.round_index:
            raise MechanismCapabilityError("mechanism round differs from runtime round")
        if parent_candidate is not None and parent_candidate != self.capability.parent_candidate:
            raise MechanismCapabilityError("mechanism parent differs from runtime round")
        self.capability.revalidate(round_intent=round_intent)
        self.repository.append_precommitment(self.capability)
        self._precommitted = True

    def observe_candidate(
        self,
        *,
        experiment_id: str,
        candidate_revision: PolicyRevisionIdentityV5,
        candidate_source_bundle: SourceBundleV5,
        semantic_outcome: str,
        semantic_recovered: bool,
        deadline_monotonic: float | None,
    ) -> MechanismObservationRunV1 | None:
        if not self._precommitted:
            raise MechanismCapabilityError("mechanism precommitment is missing before candidate observation")
        if semantic_outcome != "behaviorally_distinct":
            return None
        bound = self.capability.bind_candidate(
            experiment_id=experiment_id,
            candidate_revision=candidate_revision,
            candidate_source_bundle=candidate_source_bundle,
        )
        self.repository.append_binding(bound)
        manifest_mode = self.capability.authenticated_manifest.manifest.semantic_mode
        if manifest_mode == "disabled_development":
            run = MechanismObservationRunV1(
                binding=bound.binding,
                corpus=self.capability.corpus,
                execution=MechanismExecutionV1(status="not_run", reason="disabled_development"),
                coverage=MechanismCoverageV1(
                    total_cases=0,
                    relevant_cases=0,
                    decision_changed_cases=0,
                    protected_control_cases=0,
                    protected_control_unchanged_cases=0,
                    unsupported_cases=0,
                ),
                observations=(),
                repetitions=1,
                reset_semantics="reset_per_case",
                limitations=("Mechanism observations were skipped because disabled development semantics are active.",),
            )
            self.repository.append_run(bound, run)
            self._bound[experiment_id] = bound
            self._runs[experiment_id] = run
            return run
        existing = self.repository.load_run(bound)
        if existing is not None:
            if self.capability.execution_kind == "registered_sandbox" and existing.execution.status == "completed":
                raise MechanismArtifactCorrupt(
                    "registered sandbox cannot reuse a completed synthetic mechanism observation"
                )
            self._bound[experiment_id] = bound
            self._runs[experiment_id] = existing
            return existing
        if self.capability.execution_kind == "registered_sandbox":
            run = MechanismObservationRunV1(
                binding=bound.binding,
                corpus=self.capability.corpus,
                execution=MechanismExecutionV1(status="not_run", reason="worker_unavailable"),
                coverage=MechanismCoverageV1(
                    total_cases=0,
                    relevant_cases=0,
                    decision_changed_cases=0,
                    protected_control_cases=0,
                    protected_control_unchanged_cases=0,
                    unsupported_cases=0,
                ),
                observations=(),
                repetitions=1,
                reset_semantics="reset_per_case",
                limitations=(
                    "No registered sandbox executor exists in this adapter slice; observation failed closed.",
                ),
            )
        elif semantic_recovered or self.worker_factory is None:
            reason = "worker_unavailable"
            limitation = (
                "A recovered fixed-semantic candidate had no matching completed mechanism sidecar; no observation was rerun."
                if semantic_recovered
                else "No concrete registered mechanism executor was supplied; observation failed closed."
            )
            run = MechanismObservationRunV1(
                binding=bound.binding,
                corpus=self.capability.corpus,
                execution=MechanismExecutionV1(status="not_run", reason=reason),
                coverage=MechanismCoverageV1(
                    total_cases=0,
                    relevant_cases=0,
                    decision_changed_cases=0,
                    protected_control_cases=0,
                    protected_control_unchanged_cases=0,
                    unsupported_cases=0,
                ),
                observations=(),
                repetitions=1,
                reset_semantics="reset_per_case",
                limitations=(limitation,),
            )
        else:
            workers = self.worker_factory(bound)
            if type(workers) is not tuple or len(workers) != 2:
                raise MechanismCapabilityError("mechanism worker factory returned invalid ports")
            if any(
                getattr(getattr(worker, "registration", None), "execution_kind", None) != self.capability.execution_kind
                for worker in workers
            ):
                raise MechanismCapabilityError("mechanism worker registration kind differs from capability")
            run = collect_mechanism_observations_v1(
                self.capability.spec,
                bound.binding,
                self.capability.corpus,
                parent_worker=workers[0],
                candidate_worker=workers[1],
                repetitions=1,
                deadline_monotonic=deadline_monotonic,
            )
        self.repository.append_run(bound, run)
        self._bound[experiment_id] = bound
        self._runs[experiment_id] = run
        return run

    def _matched_evaluations_for_candidate(
        self,
        bound: MechanismBoundCandidateV1,
        candidate_evidence: object,
    ) -> tuple[MechanismEvaluatorMatchV1, ...]:
        """Derive only exact supplied parent/candidate evaluator contexts."""

        from core.pit_optimizer_v5.runtime import CandidateEvidenceV5

        if type(candidate_evidence) is not CandidateEvidenceV5:
            raise MechanismCapabilityError("candidate final evidence is not typed")
        if candidate_evidence.experiment_id != bound.experiment_id:
            raise MechanismCapabilityError("candidate final evidence identity differs")
        if (
            candidate_evidence.materialized.variant.policy_revision != bound.candidate_revision
            or candidate_evidence.materialized.variant.source_bundle != bound.candidate_source_bundle
        ):
            raise MechanismCapabilityError("candidate final source authority differs")

        matches: list[MechanismEvaluatorMatchV1] = []
        parent_campaign = self.capability.selected_parent_campaign_evidence
        if parent_campaign is not None:
            parent_by_key = {
                (episode.episode_id, episode.episode_ordinal): episode for episode in parent_campaign.episodes
            }
            for candidate_episode in candidate_evidence.discovery_episodes:
                key = (candidate_episode.episode_id, candidate_episode.episode_ordinal)
                parent_episode = parent_by_key.get(key)
                if parent_episode is None:
                    continue
                parent_panel = parent_episode.evaluation
                candidate_panel = candidate_episode.evaluation
                if (
                    parent_episode.start_date != candidate_episode.start_date
                    or parent_episode.end_date != candidate_episode.end_date
                    or parent_panel.evaluator_contract_sha256 != candidate_panel.evaluator_contract_sha256
                    or parent_panel.sandbox_profile_sha256 != candidate_panel.sandbox_profile_sha256
                    or parent_panel.panel_sha256 != candidate_panel.panel_sha256
                    or parent_panel.selection_scenario_id != candidate_panel.selection_scenario_id
                ):
                    continue
                matches.append(
                    MechanismEvaluatorMatchV1(
                        stage="discovery",
                        episode_id=candidate_episode.episode_id,
                        episode_ordinal=candidate_episode.episode_ordinal,
                        scenario_id=parent_panel.selection_scenario_id,
                        parent=parent_episode,
                        candidate=candidate_episode,
                        parent_revision=self.capability.parent_revision,
                        parent_source_bundle=self.capability.parent_source_bundle,
                        candidate_revision=bound.candidate_revision,
                        candidate_source_bundle=bound.candidate_source_bundle,
                    )
                )

        parent_quick = self.capability.parent_quick_evidence
        candidate_quick = candidate_evidence.quick_evidence
        if parent_quick is not None and candidate_quick is not None:
            if (
                parent_quick.evaluator_contract_sha256 == candidate_quick.evaluator_contract_sha256
                and parent_quick.sandbox_profile_sha256 == candidate_quick.sandbox_profile_sha256
                and parent_quick.panel_sha256 == candidate_quick.panel_sha256
                and parent_quick.start_date == candidate_quick.start_date
                and parent_quick.end_date == candidate_quick.end_date
                and parent_quick.selection_scenario_id == candidate_quick.selection_scenario_id
            ):
                quick_plan = self.capability.authenticated_manifest.panel_plan.quick
                matches.append(
                    MechanismEvaluatorMatchV1(
                        stage="quick",
                        episode_id=quick_plan.episode_id,
                        episode_ordinal=None,
                        scenario_id=parent_quick.selection_scenario_id,
                        parent=parent_quick,
                        candidate=candidate_quick,
                        parent_revision=self.capability.parent_revision,
                        parent_source_bundle=self.capability.parent_source_bundle,
                        candidate_revision=bound.candidate_revision,
                        candidate_source_bundle=bound.candidate_source_bundle,
                        episode_plan=quick_plan,
                    )
                )
        return tuple(matches)

    def finalize_report(
        self,
        *,
        experiment_id: str,
        candidate_evidence: object,
    ) -> object | None:
        bound = self._bound.get(experiment_id)
        run = self._runs.get(experiment_id)
        if bound is None:
            return None
        if run is None:
            run = self.repository.load_run(bound)
        if run is None:
            return None
        from core.pit_optimizer_v5.runtime import CandidateEvidenceV5

        if (
            type(candidate_evidence) is not CandidateEvidenceV5
            or candidate_evidence.status not in {"evaluated", "zero_trade"}
            or len(candidate_evidence.discovery_episodes) != 4
        ):
            raise MechanismCapabilityError("finalization requires complete post-evaluation candidate evidence")
        supplied_matches = self._matched_evaluations_for_candidate(bound, candidate_evidence)
        self.repository.append_report(bound, run, matched_evaluations=supplied_matches)
        report = self.repository.load_report(bound)
        if type(report) is not MechanismEvidenceReportV1:
            raise MechanismArtifactCorrupt("mechanism finalized report is unavailable")
        self._reports[experiment_id] = report
        return report

    def role_request_evidence(
        self,
        experiment_id: str,
    ) -> tuple[
        MechanismExtensionCapabilityV1,
        MechanismBoundCandidateV1,
        MechanismObservationRunV1,
        MechanismEvidenceReportV1,
        RoundIntentPayloadV5,
    ]:
        """Return authenticated current-round evidence for the critic adapter.

        This exposes only typed in-memory objects created by the precommitted
        extension.  It performs no repository repair or discovery and is valid
        only after ``finalize_report`` has stored the complete report.
        """

        experiment_id = _digest(experiment_id, "mechanism role experiment ID")
        bound = self._bound.get(experiment_id)
        run = self._runs.get(experiment_id)
        report = self._reports.get(experiment_id)
        if bound is None or run is None or report is None or not self._precommitted:
            raise MechanismCapabilityError("current mechanism role evidence is incomplete")
        if report.binding != bound.binding or run.binding != bound.binding:
            raise MechanismArtifactCorrupt("current mechanism role evidence binding differs")
        return self.capability, bound, run, report, self.capability.round_intent


__all__ = [
    "MECHANISM_ARTIFACT_MAX_BYTES_V1",
    "MECHANISM_ARTIFACT_NAMESPACE_V1",
    "MechanismArtifactCorrupt",
    "MechanismArtifactRepositoryV5",
    "MechanismBoundCandidateV1",
    "MechanismCapabilityError",
    "MechanismChronologyError",
    "MechanismExplicitOptInV1",
    "MechanismExperimentIndexV1",
    "MechanismExtensionCapabilityV1",
    "MechanismFixtureOptInV1",
    "MechanismPersistedPrecommitmentV1",
    "MechanismPersistedEvidenceV1",
    "MechanismPrecommitmentIndexV1",
    "MechanismRuntimeExtensionV1",
    "explicit_mechanism_fixture_opt_in_v1",
    "issue_mechanism_fixture_opt_in_v1",
    "manifest_source_identity_sha256_v1",
    "mechanism_extension_source_sha256_v1",
    "round_intent_sha256_v1",
]
