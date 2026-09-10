"""Immutable campaign admission authority and pure budget decisions."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, fields, replace
from decimal import Context, Decimal, ROUND_CEILING, localcontext
from typing import TYPE_CHECKING, Literal

from core.pit_optimizer_v5.artifacts import (
    ArtifactMissingV5,
    LocalArtifactRepositoryV5,
)
from core.pit_optimizer_v5.contracts import (
    ArtifactRefV5,
    CampaignManifestV5,
    CriticArtifactV5,
    canonical_sha256_v5,
)
from core.pit_optimizer_v5.development_preparation import (
    ProviderDevelopmentAdapterConfigV5,
    load_development_config_v5,
)
from core.pit_optimizer_v5.manifest import (
    AuthenticatedCampaignManifestV5,
    authenticate_campaign_manifest_v5,
)
from core.pit_optimizer_v5.memory import (
    CleanupResultPayloadV5,
    ExperimentRecordV5,
    RoleCompletionPayloadV5,
    RoundOutcomeKindV5,
    RoundOutcomePayloadV5,
)
from core.pit_optimizer_v5.provider import (
    LedgerRoleTerminalAuthorityV5,
    RoleCallKeyV5,
    RoleInvocationPackageV5,
    RoleRequestV5,
    prospective_role_usage_v5,
    sum_cost_usd_v5,
)

if TYPE_CHECKING:
    from core.pit_optimizer_v5.development_preparation import (
        _CompletedDevelopmentHistoryV5,
    )
    from core.pit_optimizer_v5.historical_verification import (
        CompletedHistoricalRoundResourcesV5,
    )
    from core.pit_optimizer_v5.operations import CampaignLaunchV5, CampaignRoundLaunchV5


_ADMISSION_NAMESPACE_V5 = "campaign-admission"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_THREE_ROLE_CALLS_PER_ROUND_V5 = 3
_USD_TOKEN_DENOMINATOR_V5 = Decimal(1_000_000)
_ROUND_DERIVED_CONFIG_FIELDS_V5 = frozenset(
    {
        "candidate_base_identity_sha256",
        "workspace_driver_identity_sha256",
        "mount_factory_identity_sha256",
        "container_executor_identity_sha256",
    }
)

def _digest(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _positive_integer(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _nonnegative_integer(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


@dataclass(frozen=True, slots=True)
class CampaignAdmissionPolicyV5:
    schema_version: int
    policy_revision: int
    manifest_ref: ArtifactRefV5
    adapter_config_ref: ArtifactRefV5
    owner_token_sha256: str
    repository_root_identity_sha256: str
    target_evaluated_feedback_rounds: int
    prospective_total_tokens_per_role: int

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 5:
            raise ValueError("campaign admission policy schema must be V5")
        if type(self.policy_revision) is not int or self.policy_revision != 1:
            raise ValueError("campaign admission policy revision must be 1")
        if type(self.manifest_ref) is not ArtifactRefV5:
            raise ValueError("campaign admission manifest reference is invalid")
        if type(self.adapter_config_ref) is not ArtifactRefV5:
            raise ValueError("campaign admission adapter config reference is invalid")
        _digest(self.owner_token_sha256, "campaign admission owner token")
        _digest(
            self.repository_root_identity_sha256,
            "campaign admission repository root identity",
        )
        _positive_integer(
            self.target_evaluated_feedback_rounds,
            "campaign admission evaluated-feedback target",
        )
        _positive_integer(
            self.prospective_total_tokens_per_role,
            "campaign admission prospective role-token envelope",
        )


@dataclass(frozen=True, slots=True)
class CampaignAdmissionBindingV5:
    schema_version: int
    policy_ref: ArtifactRefV5
    manifest_ref: ArtifactRefV5
    adapter_config_ref: ArtifactRefV5
    owner_token_sha256: str
    repository_root_identity_sha256: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 5:
            raise ValueError("campaign admission binding schema must be V5")
        if type(self.policy_ref) is not ArtifactRefV5:
            raise ValueError("campaign admission policy reference is invalid")
        if type(self.manifest_ref) is not ArtifactRefV5:
            raise ValueError("campaign admission manifest reference is invalid")
        if type(self.adapter_config_ref) is not ArtifactRefV5:
            raise ValueError("campaign admission adapter config reference is invalid")
        _digest(self.owner_token_sha256, "campaign admission owner token")
        _digest(
            self.repository_root_identity_sha256,
            "campaign admission repository root identity",
        )


@dataclass(frozen=True, slots=True)
class AuthenticatedCampaignAdmissionV5:
    binding: CampaignAdmissionBindingV5
    policy: CampaignAdmissionPolicyV5

    def __post_init__(self) -> None:
        if (
            type(self.binding) is not CampaignAdmissionBindingV5
            or type(self.policy) is not CampaignAdmissionPolicyV5
        ):
            raise ValueError("authenticated campaign admission values are invalid")
        if self.binding.policy_ref.sha256 != canonical_sha256_v5(self.policy):
            raise ValueError("campaign admission policy digest differs from its binding")
        if (
            self.binding.manifest_ref != self.policy.manifest_ref
            or self.binding.adapter_config_ref != self.policy.adapter_config_ref
            or self.binding.owner_token_sha256 != self.policy.owner_token_sha256
            or self.binding.repository_root_identity_sha256
            != self.policy.repository_root_identity_sha256
        ):
            raise ValueError("campaign admission binding differs from its policy")


@dataclass(frozen=True, slots=True)
class _LiveCampaignRoundAuthorityV5:
    admission: AuthenticatedCampaignAdmissionV5
    authenticated_manifest: AuthenticatedCampaignManifestV5
    campaign_launch: CampaignLaunchV5
    round_launch: CampaignRoundLaunchV5
    round_started: CampaignRoundLaunchV5
    original_config: ProviderDevelopmentAdapterConfigV5
    round_config: ProviderDevelopmentAdapterConfigV5


@dataclass(frozen=True, slots=True)
class _LiveRolePrefixV5:
    evaluated_feedback_rounds: int
    attempted_rounds: int
    external_attempts: int
    total_tokens: int
    cost_usd: Decimal
    replay: bool


@dataclass(frozen=True, slots=True)
class _CampaignRoundStateCensusV5:
    authenticated_manifest: AuthenticatedCampaignManifestV5
    campaign_launch: CampaignLaunchV5
    original_config: ProviderDevelopmentAdapterConfigV5
    launches: tuple[tuple[int, CampaignRoundLaunchV5], ...]
    starts: tuple[tuple[int, CampaignRoundLaunchV5], ...]


@dataclass(frozen=True, slots=True)
class _CampaignBoundaryFactsV5:
    evaluated_feedback_rounds: int
    attempted_rounds: int
    external_attempts: int
    total_tokens: int
    cost_usd: Decimal
    observed_started_rounds: tuple[int, ...]
    prestart_round_index: int | None


@dataclass(frozen=True, slots=True)
class _CampaignPrefixFactsV5:
    boundary: _CampaignBoundaryFactsV5
    evaluated_round_indices: tuple[int, ...]
    final_round_outcome: RoundOutcomeKindV5 | None


@dataclass(frozen=True, slots=True)
class CampaignPolicyComplianceV5:
    """Data-only enrolled-policy report over authenticated completed history."""

    policy_ref: ArtifactRefV5
    manifest_ref: ArtifactRefV5
    configured_round_indices: tuple[int, ...]
    started_epoch_ms_by_round: tuple[tuple[int, int], ...]
    evaluated_feedback_round_indices: tuple[int, ...]
    attempted_rounds: int
    external_attempts: int
    total_tokens: int
    cost_usd: Decimal
    final_decision: Literal[
        "admit",
        "evaluated_feedback_target_reached",
        "insufficient_milestone_reserve",
    ]
    final_round_outcome: RoundOutcomeKindV5 | None
    logical_prefix_eligibility: Literal["verified"]
    temporal_evidence_scope: Literal[
        "stored_campaign_launch_and_round_start_epochs_only"
    ]
    global_publication_timestamps: Literal["unavailable"]

    def __post_init__(self) -> None:
        if (
            type(self.policy_ref) is not ArtifactRefV5
            or type(self.manifest_ref) is not ArtifactRefV5
            or type(self.configured_round_indices) is not tuple
            or not self.configured_round_indices
            or self.configured_round_indices
            != tuple(range(1, len(self.configured_round_indices) + 1))
            or type(self.started_epoch_ms_by_round) is not tuple
            or tuple(index for index, _epoch in self.started_epoch_ms_by_round)
            != self.configured_round_indices
            or any(
                type(index) is not int
                or type(epoch) is not int
                or epoch < 1
                for index, epoch in self.started_epoch_ms_by_round
            )
            or type(self.evaluated_feedback_round_indices) is not tuple
            or self.evaluated_feedback_round_indices
            != tuple(sorted(set(self.evaluated_feedback_round_indices)))
            or any(
                index not in self.configured_round_indices
                for index in self.evaluated_feedback_round_indices
            )
            or type(self.attempted_rounds) is not int
            or self.attempted_rounds != len(self.configured_round_indices)
            or type(self.external_attempts) is not int
            or self.external_attempts < 0
            or type(self.total_tokens) is not int
            or self.total_tokens < 0
            or type(self.cost_usd) is not Decimal
            or not self.cost_usd.is_finite()
            or self.cost_usd < 0
            or self.final_decision
            not in {
                "admit",
                "evaluated_feedback_target_reached",
                "insufficient_milestone_reserve",
            }
            or self.final_round_outcome
            not in {
                None,
                "no_novel_hypothesis",
                "novelty_exhausted",
                "critic_unavailable",
                "runtime_failed",
            }
            or self.logical_prefix_eligibility != "verified"
            or self.temporal_evidence_scope
            != "stored_campaign_launch_and_round_start_epochs_only"
            or self.global_publication_timestamps != "unavailable"
        ):
            raise ValueError("campaign policy compliance report is invalid")


def _worst_role_envelope_cost_v5(
    *,
    policy: CampaignAdmissionPolicyV5,
    manifest: CampaignManifestV5,
) -> Decimal | None:
    provider = manifest.provider
    if provider is None or provider.maximum_usd is None:
        return None
    price = provider.price_upper_bound
    if price is None:
        raise ValueError("finite provider USD authority lacks model prices")
    maximum_unit_price = max(
        price.input_usd_per_million_tokens,
        price.output_usd_per_million_tokens,
    )
    maximum_output_tokens = provider.maximum_output_tokens_per_role
    maximum_input_tokens = (
        policy.prospective_total_tokens_per_role - maximum_output_tokens
    )
    with localcontext(Context(prec=50, rounding=ROUND_CEILING)):
        return (
            (
                Decimal(maximum_input_tokens) * maximum_unit_price
                + Decimal(maximum_output_tokens) * maximum_unit_price
            )
            / _USD_TOKEN_DENOMINATOR_V5
        )


def _required_role_envelope_cost_v5(
    *,
    policy: CampaignAdmissionPolicyV5,
    manifest: CampaignManifestV5,
    role_calls: int,
) -> Decimal | None:
    worst_role_cost = _worst_role_envelope_cost_v5(
        policy=policy,
        manifest=manifest,
    )
    if worst_role_cost is None:
        return None
    with localcontext(Context(prec=50, rounding=ROUND_CEILING)):
        return Decimal(role_calls) * worst_role_cost


def _validate_policy_manifest_v5(
    *,
    policy: CampaignAdmissionPolicyV5,
    manifest: CampaignManifestV5,
) -> None:
    if type(policy) is not CampaignAdmissionPolicyV5:
        raise ValueError("campaign admission policy is invalid")
    if type(manifest) is not CampaignManifestV5:
        raise ValueError("campaign manifest is invalid")
    if (
        manifest.pit_data_scope != "development_sp500_v2"
        or manifest.semantic_mode != "disabled_development"
        or manifest.provider is None
        or manifest.apply
        or manifest.qualification_allowed
        or manifest.full_replay_allowed
    ):
        raise ValueError("campaign admission requires a paid development manifest")
    if policy.manifest_ref.sha256 != manifest.sha256:
        raise ValueError("campaign admission policy manifest digest differs")

    search = manifest.search
    provider = manifest.provider
    if (
        search.hypotheses_per_investigator != 1
        or search.max_tunable_axes != 1
        or search.max_variants_per_template != 1
        or search.max_discovery_survivors_per_template != 1
        or search.allow_full_source_escape
    ):
        raise ValueError("campaign admission requires the single-candidate lifecycle")
    if provider.automatic_retries != 0 or provider.schema_repair_calls != 0:
        raise ValueError("campaign admission requires zero retries and repair calls")
    if policy.target_evaluated_feedback_rounds > search.max_feedback_rounds:
        raise ValueError("campaign admission target exceeds lifecycle capacity")
    if (
        provider.maximum_role_calls
        < _THREE_ROLE_CALLS_PER_ROUND_V5 * policy.target_evaluated_feedback_rounds
    ):
        raise ValueError("campaign admission target exceeds role-call capacity")
    if provider.maximum_output_tokens_per_role > policy.prospective_total_tokens_per_role:
        raise ValueError("provider output bound exceeds the prospective role envelope")
    if (
        provider.maximum_total_tokens
        < provider.maximum_role_calls * policy.prospective_total_tokens_per_role
    ):
        raise ValueError("provider token capacity cannot fund every role envelope")
    required_role_cost = _required_role_envelope_cost_v5(
        policy=policy,
        manifest=manifest,
        role_calls=provider.maximum_role_calls,
    )
    if required_role_cost is not None and (
        provider.maximum_usd is None
        or provider.maximum_usd < required_role_cost
    ):
        raise ValueError("provider USD capacity cannot fund every role envelope")


def authenticate_prepared_campaign_policy_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    manifest: CampaignManifestV5,
    policy_ref: ArtifactRefV5,
) -> CampaignAdmissionPolicyV5:
    """Authenticate one prepared policy without requiring or writing enrollment."""

    if type(repository) is not LocalArtifactRepositoryV5:
        raise ValueError("campaign admission repository is invalid")
    if type(manifest) is not CampaignManifestV5:
        raise ValueError("campaign manifest is invalid")
    if type(policy_ref) is not ArtifactRefV5:
        raise ValueError("campaign admission policy reference is invalid")
    policy = repository.load_typed_artifact(
        policy_ref,
        value_type=CampaignAdmissionPolicyV5,
    )
    if policy_ref.sha256 != canonical_sha256_v5(policy):
        raise ValueError("campaign admission policy reference differs from its bytes")
    authenticated_manifest = authenticate_campaign_manifest_v5(
        repository=repository,
        manifest_ref=policy.manifest_ref,
    )
    if authenticated_manifest.manifest != manifest:
        raise ValueError("campaign admission policy selects a different manifest")
    _validate_policy_manifest_v5(policy=policy, manifest=manifest)
    config = load_development_config_v5(
        repository,
        authenticated_manifest,
        policy.adapter_config_ref,
    )
    if (
        type(config) is not ProviderDevelopmentAdapterConfigV5
        or policy.adapter_config_ref.sha256 != canonical_sha256_v5(config)
        or config.campaign_manifest_sha256 != manifest.sha256
        or config.repository_root_identity_sha256 != repository.root_identity_sha256
        or policy.repository_root_identity_sha256 != repository.root_identity_sha256
    ):
        raise ValueError("campaign admission adapter configuration authority differs")
    return policy


def _campaign_admission_bindings_v5(
    repository: LocalArtifactRepositoryV5,
) -> tuple[tuple[str, CampaignAdmissionBindingV5], ...]:
    keys: set[str] = set()
    for root in ("adapter-state", "adapter-state-authority"):
        try:
            names = repository._names((root, _ADMISSION_NAMESPACE_V5))
        except ArtifactMissingV5:
            names = ()
        for name in names:
            if re.fullmatch(r"[0-9a-f]{64}\.json", name) is None:
                raise ValueError("campaign admission namespace member is invalid")
            keys.add(name[:-5])

    bindings: list[tuple[str, CampaignAdmissionBindingV5]] = []
    for key in sorted(keys):
        binding = repository.load_typed_state(
            namespace=_ADMISSION_NAMESPACE_V5,
            key=key,
            value_type=CampaignAdmissionBindingV5,
            repair=False,
        )
        if binding is None:
            raise ValueError("campaign admission namespace member is incomplete")
        if binding.manifest_ref.sha256 != key:
            raise ValueError("campaign admission binding is stored under a foreign key")
        bindings.append((key, binding))
    return tuple(bindings)


def load_campaign_admission_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    manifest: CampaignManifestV5,
    expected_policy_ref: ArtifactRefV5 | None = None,
) -> AuthenticatedCampaignAdmissionV5 | None:
    """Discover and authenticate selected enrollment without repairing state."""

    if type(repository) is not LocalArtifactRepositoryV5:
        raise ValueError("campaign admission repository is invalid")
    if type(manifest) is not CampaignManifestV5:
        raise ValueError("campaign manifest is invalid")
    if expected_policy_ref is not None and type(expected_policy_ref) is not ArtifactRefV5:
        raise ValueError("expected campaign admission policy reference is invalid")

    manifest_key = manifest.sha256
    selected = tuple(
        binding
        for key, binding in _campaign_admission_bindings_v5(repository)
        if key == manifest_key
    )
    if not selected:
        if expected_policy_ref is not None:
            raise ValueError("expected campaign admission enrollment is missing")
        return None
    if len(selected) != 1:
        raise ValueError("campaign admission enrollment is ambiguous")
    binding = selected[0]
    if expected_policy_ref is not None and binding.policy_ref != expected_policy_ref:
        raise ValueError("campaign admission enrollment selects a different policy")
    policy = authenticate_prepared_campaign_policy_v5(
        repository=repository,
        manifest=manifest,
        policy_ref=binding.policy_ref,
    )
    admission = AuthenticatedCampaignAdmissionV5(binding=binding, policy=policy)
    if binding.manifest_ref.sha256 != manifest_key:
        raise ValueError("campaign admission binding selects a different manifest")
    return admission


def require_role_envelope_v5(
    *,
    policy: CampaignAdmissionPolicyV5,
    manifest: CampaignManifestV5,
    request: RoleRequestV5,
) -> None:
    """Require an exact role request to fit the policy's prospective envelope."""

    _validate_policy_manifest_v5(policy=policy, manifest=manifest)
    if type(request) is not RoleRequestV5:
        raise ValueError("campaign admission role request is invalid")
    provider = manifest.provider
    if provider is None:
        raise ValueError("campaign admission requires provider capabilities")
    if request.max_output_tokens != provider.maximum_output_tokens_per_role:
        raise ValueError("role request output bound differs from provider authority")
    input_bound, _cost_bound = prospective_role_usage_v5(request, provider)
    if input_bound + request.max_output_tokens > policy.prospective_total_tokens_per_role:
        raise ValueError("role request exceeds the campaign admission envelope")


def campaign_boundary_decision_v5(
    *,
    policy: CampaignAdmissionPolicyV5,
    manifest: CampaignManifestV5,
    evaluated_feedback_rounds: int,
    attempted_rounds: int,
    external_attempts: int,
    total_tokens: int,
    cost_usd: Decimal,
) -> Literal[
    "admit",
    "evaluated_feedback_target_reached",
    "insufficient_milestone_reserve",
]:
    """Return the exact settled-boundary milestone admission decision."""

    _validate_policy_manifest_v5(policy=policy, manifest=manifest)
    _nonnegative_integer(evaluated_feedback_rounds, "evaluated feedback rounds")
    _nonnegative_integer(attempted_rounds, "attempted rounds")
    _nonnegative_integer(external_attempts, "external attempts")
    _nonnegative_integer(total_tokens, "total tokens")
    if type(cost_usd) is not Decimal or not cost_usd.is_finite() or cost_usd < 0:
        raise ValueError("campaign cost must be a nonnegative finite Decimal")

    missing = max(
        0,
        policy.target_evaluated_feedback_rounds - evaluated_feedback_rounds,
    )
    if missing == 0:
        return "evaluated_feedback_target_reached"

    provider = manifest.provider
    if provider is None:
        raise ValueError("campaign admission requires provider capabilities")
    needed_calls = _THREE_ROLE_CALLS_PER_ROUND_V5 * missing
    if (
        manifest.search.max_feedback_rounds - attempted_rounds < missing
        or provider.maximum_role_calls - external_attempts < needed_calls
        or provider.maximum_total_tokens - total_tokens
        < needed_calls * policy.prospective_total_tokens_per_role
    ):
        return "insufficient_milestone_reserve"

    required_reserve = _required_role_envelope_cost_v5(
        policy=policy,
        manifest=manifest,
        role_calls=needed_calls,
    )
    if required_reserve is not None and (
        provider.maximum_usd is None
        or provider.maximum_usd < sum_cost_usd_v5(cost_usd, required_reserve)
    ):
        return "insufficient_milestone_reserve"
    return "admit"


def _round_state_key_v5(manifest: CampaignManifestV5, round_index: int) -> str:
    return canonical_sha256_v5(
        {
            "campaign_manifest_sha256": manifest.sha256,
            "round_index": round_index,
        }
    )


def _state_name_sets_v5(
    repository: LocalArtifactRepositoryV5,
    namespace: str,
) -> tuple[frozenset[str], frozenset[str]]:
    groups: list[frozenset[str]] = []
    for root in ("adapter-state", "adapter-state-authority"):
        try:
            observed = repository._names((root, namespace))
        except ArtifactMissingV5:
            observed = ()
        names: set[str] = set()
        for name in observed:
            if re.fullmatch(r"[0-9a-f]{64}\.json", name) is None:
                raise ValueError("campaign admission state member is noncanonical")
            names.add(name[:-5])
        groups.append(frozenset(names))
    return groups[0], groups[1]


def _state_names_v5(
    repository: LocalArtifactRepositoryV5,
    namespace: str,
) -> tuple[str, ...]:
    value_names, authority_names = _state_name_sets_v5(repository, namespace)
    return tuple(sorted(value_names | authority_names))


def _selected_campaign_launches_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    manifest: CampaignManifestV5,
    admission: AuthenticatedCampaignAdmissionV5,
) -> tuple[CampaignLaunchV5, ...]:
    from core.pit_optimizer_v5.operations import CampaignLaunchV5

    selected: list[CampaignLaunchV5] = []
    for key in _state_names_v5(repository, "campaign-launch"):
        launch = repository.load_typed_state(
            namespace="campaign-launch",
            key=key,
            value_type=CampaignLaunchV5,
            repair=False,
        )
        if launch is None:
            raise ValueError("observed campaign launch state disappeared")
        belongs_to_campaign = (
            launch.manifest_ref.sha256 == manifest.sha256
            or launch.adapter_config_ref == admission.policy.adapter_config_ref
            or launch.owner_token_sha256 == admission.policy.owner_token_sha256
        )
        if not belongs_to_campaign:
            continue
        if key != manifest.sha256:
            raise ValueError("campaign launch is stored under a foreign key")
        selected.append(launch)
    return tuple(selected)


def _selected_campaign_launch_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    manifest: CampaignManifestV5,
    admission: AuthenticatedCampaignAdmissionV5,
) -> CampaignLaunchV5:
    selected = _selected_campaign_launches_v5(
        repository=repository,
        manifest=manifest,
        admission=admission,
    )
    if len(selected) != 1:
        raise ValueError("campaign admission requires one original campaign launch")
    return selected[0]


def _round_state_belongs_to_campaign_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    value: CampaignRoundLaunchV5,
    key: str,
    manifest: CampaignManifestV5,
    admission: AuthenticatedCampaignAdmissionV5,
) -> bool:
    expected_key = _round_state_key_v5(manifest, value.round_index)
    round_config_prefix = f"campaigns/{manifest.sha256}/adapter-config-round-"
    directly_selected = (
        key == expected_key
        or value.owner_token_sha256 == admission.policy.owner_token_sha256
        or value.adapter_config_ref == admission.policy.adapter_config_ref
        or value.adapter_config_ref.relative_path.startswith(round_config_prefix)
    )
    if directly_selected:
        return True
    config = repository.load_typed_artifact(
        value.adapter_config_ref,
        value_type=ProviderDevelopmentAdapterConfigV5,
    )
    return config.campaign_manifest_sha256 == manifest.sha256


def _selected_round_states_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    manifest: CampaignManifestV5,
    admission: AuthenticatedCampaignAdmissionV5,
    namespace: str,
) -> dict[int, CampaignRoundLaunchV5]:
    from core.pit_optimizer_v5.operations import CampaignRoundLaunchV5

    selected: dict[int, CampaignRoundLaunchV5] = {}
    for key in _state_names_v5(repository, namespace):
        value = repository.load_typed_state(
            namespace=namespace,
            key=key,
            value_type=CampaignRoundLaunchV5,
            repair=False,
        )
        if value is None:
            raise ValueError("observed campaign round state disappeared")
        if not _round_state_belongs_to_campaign_v5(
            repository=repository,
            value=value,
            key=key,
            manifest=manifest,
            admission=admission,
        ):
            continue
        if (
            value.round_index > manifest.search.max_feedback_rounds
            or key != _round_state_key_v5(manifest, value.round_index)
            or value.round_index in selected
        ):
            raise ValueError("campaign round state is noncanonical or duplicated")
        selected[value.round_index] = value
    return selected


def _campaign_admission_enrollment_state_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    manifest: CampaignManifestV5,
    expected: AuthenticatedCampaignAdmissionV5,
) -> Literal["absent", "enrolled", "missing_index"]:
    """Observe one exact prepared enrollment without repairing it."""

    if (
        type(repository) is not LocalArtifactRepositoryV5
        or type(manifest) is not CampaignManifestV5
        or type(expected) is not AuthenticatedCampaignAdmissionV5
        or expected.policy.manifest_ref.sha256 != manifest.sha256
    ):
        raise ValueError("campaign admission enrollment authority is invalid")
    value_names, authority_names = _state_name_sets_v5(
        repository,
        _ADMISSION_NAMESPACE_V5,
    )
    key = manifest.sha256
    if key in authority_names and key not in value_names:
        raise ValueError("campaign admission authority lacks its immutable value")
    if key in value_names and key not in authority_names:
        for other_key in sorted((value_names | authority_names) - {key}):
            binding = repository.load_typed_state(
                namespace=_ADMISSION_NAMESPACE_V5,
                key=other_key,
                value_type=CampaignAdmissionBindingV5,
                repair=False,
            )
            if binding is None or binding.manifest_ref.sha256 != other_key:
                raise ValueError("campaign admission namespace member is incomplete")
        expected_ref = ArtifactRefV5(
            f"adapter-state/{_ADMISSION_NAMESPACE_V5}/{key}.json",
            canonical_sha256_v5(expected.binding),
        )
        observed = repository.load_typed_artifact(
            expected_ref,
            value_type=CampaignAdmissionBindingV5,
        )
        if observed != expected.binding:
            raise ValueError("campaign admission orphan differs from the expected binding")
        return "missing_index"

    admission = load_campaign_admission_v5(
        repository=repository,
        manifest=manifest,
        expected_policy_ref=None if key not in value_names else expected.binding.policy_ref,
    )
    if admission is None:
        return "absent"
    if admission != expected:
        raise ValueError("campaign admission enrollment differs from the expected binding")
    return "enrolled"


def _require_campaign_enrollment_freshness_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    manifest: CampaignManifestV5,
    expected: AuthenticatedCampaignAdmissionV5,
) -> None:
    """Require no selected launch, round, request, checkpoint, or paid ledger state."""

    from core.pit_optimizer_v5.historical_verification import (
        _existing_campaign_role_requests_v5,
    )
    from core.pit_optimizer_v5.production_provider import (
        _load_verified_role_ledger_v5,
        _role_ledger_read_identity_v5,
    )

    if _selected_campaign_launches_v5(
        repository=repository,
        manifest=manifest,
        admission=expected,
    ):
        raise ValueError("campaign admission enrollment requires an absent launch")
    if _selected_round_states_v5(
        repository=repository,
        manifest=manifest,
        admission=expected,
        namespace="campaign-round",
    ) or _selected_round_states_v5(
        repository=repository,
        manifest=manifest,
        admission=expected,
        namespace="campaign-round-started",
    ):
        raise ValueError("campaign admission enrollment observed round authority")
    if _campaign_event_rounds_v5(repository, manifest):
        raise ValueError("campaign admission enrollment observed a round journal")
    if repository.load_checkpoint() is not None:
        raise ValueError("campaign admission enrollment observed a checkpoint")
    if _existing_campaign_role_requests_v5(repository, manifest):
        raise ValueError("campaign admission enrollment observed a role request")

    authenticated_manifest = authenticate_campaign_manifest_v5(
        repository=repository,
        manifest_ref=expected.policy.manifest_ref,
    )
    if authenticated_manifest.manifest != manifest:
        raise ValueError("campaign admission enrollment manifest differs")
    original_config = load_development_config_v5(
        repository,
        authenticated_manifest,
        expected.policy.adapter_config_ref,
    )
    if type(original_config) is not ProviderDevelopmentAdapterConfigV5:
        raise ValueError("campaign admission enrollment config differs")
    identity = _role_ledger_read_identity_v5(
        repository,
        manifest,
        api_key_environment_variable=original_config.api_key_environment_variable,
    )
    reservations, terminals = _load_verified_role_ledger_v5(
        repository=repository,
        manifest=manifest,
        identity=identity,
    )
    if reservations or terminals:
        raise ValueError("campaign admission enrollment observed paid role state")


def _require_round_config_matches_original_v5(
    original: ProviderDevelopmentAdapterConfigV5,
    current: ProviderDevelopmentAdapterConfigV5,
) -> None:
    if type(current) is not type(original):
        raise ValueError("campaign round config type differs from original config")
    if any(
        item.name not in _ROUND_DERIVED_CONFIG_FIELDS_V5
        and getattr(current, item.name) != getattr(original, item.name)
        for item in fields(original)
    ):
        raise ValueError("campaign round config differs outside derived identities")


def _load_campaign_round_state_census_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    manifest: CampaignManifestV5,
    admission: AuthenticatedCampaignAdmissionV5,
    owner_token_sha256: str,
) -> _CampaignRoundStateCensusV5:
    """Authenticate the global launched/started frontier without repairing state."""

    _digest(owner_token_sha256, "campaign boundary owner token")
    authenticated_manifest = authenticate_campaign_manifest_v5(
        repository=repository,
        manifest_ref=admission.policy.manifest_ref,
    )
    if authenticated_manifest.manifest != manifest:
        raise ValueError("campaign boundary manifest differs")
    campaign_launch = _selected_campaign_launch_v5(
        repository=repository,
        manifest=manifest,
        admission=admission,
    )
    if (
        campaign_launch.manifest_ref != admission.policy.manifest_ref
        or campaign_launch.adapter_config_ref != admission.policy.adapter_config_ref
        or campaign_launch.owner_token_sha256 != admission.policy.owner_token_sha256
        or campaign_launch.owner_token_sha256 != owner_token_sha256
    ):
        raise ValueError("campaign boundary launch authority differs")
    original_config = load_development_config_v5(
        repository,
        authenticated_manifest,
        admission.policy.adapter_config_ref,
    )
    if type(original_config) is not ProviderDevelopmentAdapterConfigV5:
        raise ValueError("campaign boundary requires the original provider config")

    launches = _selected_round_states_v5(
        repository=repository,
        manifest=manifest,
        admission=admission,
        namespace="campaign-round",
    )
    starts = _selected_round_states_v5(
        repository=repository,
        manifest=manifest,
        admission=admission,
        namespace="campaign-round-started",
    )
    expected_starts = set(range(1, len(starts) + 1))
    if set(starts) != expected_starts or frozenset(launches) not in {
        frozenset(expected_starts),
        frozenset(expected_starts | {len(starts) + 1}),
    }:
        raise ValueError("campaign boundary round authority is gapped or later")
    if len(launches) > manifest.search.max_feedback_rounds:
        raise ValueError("campaign boundary exceeds lifecycle authority")

    campaign_deadline_epoch_ms = (
        campaign_launch.started_epoch_ms
        + manifest.resources.campaign_wall_timeout_seconds * 1000
    )
    for index, launch in launches.items():
        started = starts.get(index)
        if (
            launch.round_index != index
            or launch.owner_token_sha256 != campaign_launch.owner_token_sha256
            or launch.started_epoch_ms is not None
            or (
                started is not None
                and (
                    started.started_epoch_ms is None
                    or replace(started, started_epoch_ms=None) != launch
                    or started.started_epoch_ms < campaign_launch.started_epoch_ms
                    or started.started_epoch_ms >= campaign_deadline_epoch_ms
                )
            )
        ):
            raise ValueError("campaign boundary round launch/start differs")
        if index == 1 and launch.adapter_config_ref != admission.policy.adapter_config_ref:
            raise ValueError("campaign boundary first round differs from the original config")
        round_config = load_development_config_v5(
            repository,
            authenticated_manifest,
            launch.adapter_config_ref,
        )
        if type(round_config) is not ProviderDevelopmentAdapterConfigV5:
            raise ValueError("campaign boundary round config type differs")
        _require_round_config_matches_original_v5(original_config, round_config)

    return _CampaignRoundStateCensusV5(
        authenticated_manifest=authenticated_manifest,
        campaign_launch=campaign_launch,
        original_config=original_config,
        launches=tuple(sorted(launches.items())),
        starts=tuple(sorted(starts.items())),
    )


def _campaign_observed_started_rounds_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    manifest: CampaignManifestV5,
    admission: AuthenticatedCampaignAdmissionV5,
    owner_token_sha256: str,
) -> tuple[int, ...]:
    census = _load_campaign_round_state_census_v5(
        repository=repository,
        manifest=manifest,
        admission=admission,
        owner_token_sha256=owner_token_sha256,
    )
    return tuple(index for index, _started in census.starts)


def _load_campaign_round_authority_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    manifest: CampaignManifestV5,
    admission: AuthenticatedCampaignAdmissionV5,
    round_index: int,
    owner_token_sha256: str | None,
) -> _LiveCampaignRoundAuthorityV5:
    if (
        type(round_index) is not int
        or round_index < 1
        or round_index > manifest.search.max_feedback_rounds
    ):
        raise ValueError("campaign admission round is invalid")
    if owner_token_sha256 is not None:
        _digest(owner_token_sha256, "live campaign admission owner token")

    authenticated_manifest = authenticate_campaign_manifest_v5(
        repository=repository,
        manifest_ref=admission.policy.manifest_ref,
    )
    if authenticated_manifest.manifest != manifest:
        raise ValueError("live campaign admission manifest differs")
    campaign_launch = _selected_campaign_launch_v5(
        repository=repository,
        manifest=manifest,
        admission=admission,
    )
    if (
        campaign_launch.manifest_ref != admission.policy.manifest_ref
        or campaign_launch.adapter_config_ref != admission.policy.adapter_config_ref
        or campaign_launch.owner_token_sha256 != admission.policy.owner_token_sha256
        or (
            owner_token_sha256 is not None
            and owner_token_sha256 != campaign_launch.owner_token_sha256
        )
    ):
        raise ValueError("live campaign admission launch authority differs")

    launches = _selected_round_states_v5(
        repository=repository,
        manifest=manifest,
        admission=admission,
        namespace="campaign-round",
    )
    starts = _selected_round_states_v5(
        repository=repository,
        manifest=manifest,
        admission=admission,
        namespace="campaign-round-started",
    )
    expected_rounds = set(range(1, round_index + 1))
    if set(launches) != expected_rounds or set(starts) != expected_rounds:
        raise ValueError("live campaign admission round authority is gapped or later")
    for index in range(1, round_index + 1):
        launch = launches[index]
        started = starts[index]
        if (
            launch.round_index != index
            or launch.owner_token_sha256 != campaign_launch.owner_token_sha256
            or launch.started_epoch_ms is not None
            or started.started_epoch_ms is None
            or replace(started, started_epoch_ms=None) != launch
        ):
            raise ValueError("live campaign admission round launch/start differs")

    round_launch = launches[round_index]
    round_started = starts[round_index]
    if round_index == 1 and round_launch.adapter_config_ref != admission.policy.adapter_config_ref:
        raise ValueError("first admitted round differs from the original config")
    original_config = load_development_config_v5(
        repository,
        authenticated_manifest,
        admission.policy.adapter_config_ref,
    )
    round_config = load_development_config_v5(
        repository,
        authenticated_manifest,
        round_launch.adapter_config_ref,
    )
    if (
        type(original_config) is not ProviderDevelopmentAdapterConfigV5
        or type(round_config) is not ProviderDevelopmentAdapterConfigV5
    ):
        raise ValueError("live campaign admission requires provider development configs")
    _require_round_config_matches_original_v5(original_config, round_config)

    campaign_deadline_epoch_ms = (
        campaign_launch.started_epoch_ms
        + manifest.resources.campaign_wall_timeout_seconds * 1000
    )
    if any(
        started.started_epoch_ms is None
        or started.started_epoch_ms < campaign_launch.started_epoch_ms
        or started.started_epoch_ms >= campaign_deadline_epoch_ms
        for started in starts.values()
    ):
        raise ValueError("campaign round start is outside the original campaign deadline")
    return _LiveCampaignRoundAuthorityV5(
        admission=admission,
        authenticated_manifest=authenticated_manifest,
        campaign_launch=campaign_launch,
        round_launch=round_launch,
        round_started=round_started,
        original_config=original_config,
        round_config=round_config,
    )


def _require_live_round_deadline_v5(
    *,
    manifest: CampaignManifestV5,
    authority: _LiveCampaignRoundAuthorityV5,
    now_epoch_ms: int,
) -> None:
    if type(now_epoch_ms) is not int or now_epoch_ms < 0:
        raise ValueError("live campaign admission time is invalid")
    round_started_epoch_ms = authority.round_started.started_epoch_ms
    assert round_started_epoch_ms is not None
    campaign_deadline_epoch_ms = (
        authority.campaign_launch.started_epoch_ms
        + manifest.resources.campaign_wall_timeout_seconds * 1000
    )
    round_deadline_epoch_ms = (
        round_started_epoch_ms
        + manifest.resources.round_wall_timeout_seconds * 1000
    )
    if (
        now_epoch_ms < round_started_epoch_ms
        or now_epoch_ms >= campaign_deadline_epoch_ms
        or now_epoch_ms >= round_deadline_epoch_ms
    ):
        raise ValueError("live campaign admission deadline is exhausted or inconsistent")


def _campaign_event_rounds_v5(
    repository: LocalArtifactRepositoryV5,
    manifest: CampaignManifestV5,
) -> tuple[int, ...]:
    try:
        names = repository._names(("events", manifest.campaign_id))
    except ArtifactMissingV5:
        return ()
    rounds: list[int] = []
    for name in names:
        if re.fullmatch(r"[0-9]{4,}", name) is None:
            raise ValueError("campaign event round directory is noncanonical")
        round_index = int(name)
        if (
            round_index < 1
            or round_index > manifest.search.max_feedback_rounds
            or name != f"{round_index:04d}"
        ):
            raise ValueError("campaign event round directory is outside authority")
        rounds.append(round_index)
    return tuple(sorted(rounds))


def _validated_role_packages_v5(
    *,
    manifest: CampaignManifestV5,
    round_index: int,
    payloads: tuple[object, ...],
    policy: CampaignAdmissionPolicyV5,
    packages: tuple[RoleInvocationPackageV5, ...],
) -> tuple[tuple[RoleCompletionPayloadV5, RoleInvocationPackageV5], ...]:
    result: list[tuple[RoleCompletionPayloadV5, RoleInvocationPackageV5]] = []
    role_payloads = tuple(
        payload for payload in payloads if type(payload) is RoleCompletionPayloadV5
    )
    if len(role_payloads) != len(packages):
        raise ValueError("campaign admission role package count differs")
    for payload, package in zip(role_payloads, packages, strict=True):
        call = package.call
        if (
            payload.campaign_id != manifest.campaign_id
            or payload.round_index != round_index
            or call.campaign_id != manifest.campaign_id
            or call.round_index != round_index
            or call.role != payload.role
            or call.role_position != payload.role_position
            or call.attempt_kind != "primary"
            or call.attempt_index != 1
            or package.request.role != call.role
        ):
            raise ValueError("campaign admission role completion is foreign")
        require_role_envelope_v5(
            policy=policy,
            manifest=manifest,
            request=package.request,
        )
        result.append((payload, package))
    positions = tuple(item[1].call.role_position for item in result)
    if positions != tuple(range(1, len(result) + 1)):
        raise ValueError("campaign admission role completions are not a prefix")
    return tuple(result)


def _role_packages_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    manifest: CampaignManifestV5,
    round_index: int,
    payloads: tuple[object, ...],
    policy: CampaignAdmissionPolicyV5,
) -> tuple[tuple[RoleCompletionPayloadV5, RoleInvocationPackageV5], ...]:
    role_payloads = tuple(
        payload for payload in payloads if type(payload) is RoleCompletionPayloadV5
    )
    packages = tuple(repository.load_role_invocation(payload) for payload in role_payloads)
    return _validated_role_packages_v5(
        manifest=manifest,
        round_index=round_index,
        payloads=payloads,
        policy=policy,
        packages=packages,
    )


def _evaluated_round_indices_v5(
    *,
    records: tuple[ExperimentRecordV5, ...],
    completed_by_round: dict[
        int,
        tuple[tuple[RoleCompletionPayloadV5, RoleInvocationPackageV5], ...],
    ],
    round_indices: range,
) -> tuple[int, ...]:
    evaluated_rounds: list[int] = []
    for index in round_indices:
        evaluated_records = tuple(
            record
            for record in records
            if record.round_index == index
            and record.status == "evaluated"
            and record.campaign_evidence is not None
        )
        if not evaluated_records:
            continue
        critics = tuple(
            package
            for _payload, package in completed_by_round[index]
            if package.call.role == "critic"
        )
        if (
            len(critics) != 1
            or not critics[0].accepted
            or type(critics[0].artifact) is not CriticArtifactV5
            or any(
                record.critic_artifact_ref is None
                or canonical_sha256_v5(critics[0].artifact)
                != record.critic_artifact_ref.sha256
                for record in evaluated_records
            )
        ):
            raise ValueError("evaluated campaign round lacks its authenticated critic")
        evaluated_rounds.append(index)
    return tuple(evaluated_rounds)


def _compose_campaign_prefix_facts_v5(
    *,
    manifest: CampaignManifestV5,
    policy: CampaignAdmissionPolicyV5,
    completed_rounds: tuple[CompletedHistoricalRoundResourcesV5, ...],
    records: tuple[ExperimentRecordV5, ...],
    completed_by_round: dict[
        int,
        tuple[tuple[RoleCompletionPayloadV5, RoleInvocationPackageV5], ...],
    ],
    observed_started_rounds: tuple[int, ...],
    prestart_round_index: int | None,
    allow_final_stop: bool,
) -> _CampaignPrefixFactsV5:
    """Compose one authenticated logical prefix without reading mutable state."""

    from core.pit_optimizer_v5.historical_verification import (
        CompletedHistoricalRoundResourcesV5,
    )

    round_indices = tuple(
        completed.round_authority.owner.round_index
        for completed in completed_rounds
    )
    if (
        type(completed_rounds) is not tuple
        or any(
            type(completed) is not CompletedHistoricalRoundResourcesV5
            for completed in completed_rounds
        )
        or round_indices != tuple(range(1, len(round_indices) + 1))
        or type(records) is not tuple
        or any(type(record) is not ExperimentRecordV5 for record in records)
        or any(record.round_index not in round_indices for record in records)
        or type(completed_by_round) is not dict
        or tuple(completed_by_round) != round_indices
        or type(observed_started_rounds) is not tuple
        or observed_started_rounds != round_indices
        or (
            prestart_round_index is not None
            and (
                type(prestart_round_index) is not int
                or prestart_round_index != len(round_indices) + 1
            )
        )
        or type(allow_final_stop) is not bool
    ):
        raise ValueError("campaign admission prefix facts are invalid")

    packages: list[RoleInvocationPackageV5] = []
    final_round_outcome: RoundOutcomeKindV5 | None = None
    for position, completed in enumerate(completed_rounds, start=1):
        entries = completed_by_round[position]
        if (
            type(entries) is not tuple
            or any(
                type(payload) is not RoleCompletionPayloadV5
                or type(package) is not RoleInvocationPackageV5
                or payload.round_index != position
                or package.call.round_index != position
                for payload, package in entries
            )
        ):
            raise ValueError("campaign admission prefix role facts are invalid")
        round_packages = tuple(package for _payload, package in entries)
        for package in round_packages:
            if type(package.terminal_authority) is not LedgerRoleTerminalAuthorityV5:
                raise ValueError("campaign admission prefix requires paid ledger authority")
            require_role_envelope_v5(
                policy=policy,
                manifest=manifest,
                request=package.request,
            )
        packages.extend(round_packages)

        round_records = tuple(
            record for record in records if record.round_index == position
        )
        round_outcomes = tuple(
            payload
            for payload in completed.payloads
            if type(payload) is RoundOutcomePayloadV5
        )
        if (
            len(round_outcomes) > 1
            or (round_outcomes and round_records)
            or (not round_records and not round_outcomes)
        ):
            raise ValueError("campaign admission prefix settlement is invalid")
        outcome = None if not round_outcomes else round_outcomes[0].outcome
        continuable = (
            outcome in {None, "no_novel_hypothesis"}
            and all(package.accepted for package in round_packages)
        )
        if not continuable and (
            not allow_final_stop or position != len(completed_rounds)
        ):
            raise ValueError("campaign admission follows a noncontinuable round")
        if position == len(completed_rounds):
            final_round_outcome = outcome

    evaluated_rounds = _evaluated_round_indices_v5(
        records=records,
        completed_by_round=completed_by_round,
        round_indices=range(1, len(completed_rounds) + 1),
    )
    receipt = None
    if packages:
        terminal = packages[-1].terminal_authority
        assert type(terminal) is LedgerRoleTerminalAuthorityV5
        receipt = terminal.receipt
    boundary = _CampaignBoundaryFactsV5(
        evaluated_feedback_rounds=len(evaluated_rounds),
        attempted_rounds=len(completed_rounds),
        external_attempts=(
            0 if receipt is None else receipt.cumulative_external_attempts
        ),
        total_tokens=(0 if receipt is None else receipt.cumulative_total_tokens),
        cost_usd=(Decimal("0") if receipt is None else receipt.cumulative_cost_usd),
        observed_started_rounds=observed_started_rounds,
        prestart_round_index=prestart_round_index,
    )
    return _CampaignPrefixFactsV5(
        boundary=boundary,
        evaluated_round_indices=evaluated_rounds,
        final_round_outcome=final_round_outcome,
    )


def _load_live_role_prefix_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    manifest: CampaignManifestV5,
    authority: _LiveCampaignRoundAuthorityV5,
    request: RoleRequestV5,
    round_index: int,
) -> _LiveRolePrefixV5:
    from core.pit_optimizer_v5.historical_verification import (
        _existing_campaign_role_requests_v5,
        authenticate_completed_historical_round_resources_v5,
    )
    from core.pit_optimizer_v5.production_provider import (
        _authenticate_role_invocations_v5,
        _load_verified_role_ledger_v5,
        _role_ledger_read_identity_v5,
    )
    from core.pit_optimizer_v5.production_runtime import LocalArchiveReducerFactoryV5

    if any(index > round_index for index in _campaign_event_rounds_v5(repository, manifest)):
        raise ValueError("campaign admission observed a later round journal")
    prior_rounds = tuple(
        authenticate_completed_historical_round_resources_v5(
            repository=repository,
            manifest=manifest,
            round_index=index,
        )
        for index in range(1, round_index)
    )
    if any(
        completed.round_authority.authenticated_manifest
        != authority.authenticated_manifest
        or completed.round_authority.campaign_launch != authority.campaign_launch
        or completed.round_authority.original_config_ref
        != authority.admission.policy.adapter_config_ref
        or completed.round_authority.original_config != authority.original_config
        or completed.round_authority.owner.owner_token_sha256
        != authority.campaign_launch.owner_token_sha256
        for completed in prior_rounds
    ):
        raise ValueError("campaign admission prior round authority differs")

    LocalArchiveReducerFactoryV5(repository).verify_projection(
        manifest=manifest,
        panel_plan=authority.authenticated_manifest.panel_plan,
        evaluator_contract=authority.authenticated_manifest.evaluator_contract,
        repair=False,
    )
    checkpoint = repository.load_checkpoint()
    records = (
        ()
        if checkpoint is None
        else tuple(repository.load_experiment(reference) for reference in checkpoint.record_refs)
    )
    if any(record.round_index > round_index for record in records):
        raise ValueError("campaign admission checkpoint advances beyond the current round")

    journal_entries: list[tuple[RoleCompletionPayloadV5, RoleInvocationPackageV5]] = []
    completed_by_round: dict[int, tuple[tuple[RoleCompletionPayloadV5, RoleInvocationPackageV5], ...]] = {}
    for index, completed in enumerate(prior_rounds, start=1):
        entries = _role_packages_v5(
            repository=repository,
            manifest=manifest,
            round_index=index,
            payloads=completed.payloads,
            policy=authority.admission.policy,
        )
        completed_by_round[index] = entries
        journal_entries.extend(entries)
        round_records = tuple(record for record in records if record.round_index == index)
        round_outcomes = tuple(
            payload
            for payload in completed.payloads
            if type(payload) is RoundOutcomePayloadV5
        )
        if round_outcomes and (
            len(round_outcomes) != 1
            or round_outcomes[0].outcome != "no_novel_hypothesis"
            or bool(round_records)
        ):
            raise ValueError("campaign admission prior round has a stopping outcome")
        if not round_records and not round_outcomes:
            raise ValueError("campaign admission prior round lacks committed settlement")

    current_events = repository.load_round_events(
        campaign_id=manifest.campaign_id,
        round_index=round_index,
    )
    current_payloads = tuple(
        repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
        for event in current_events
    )
    current_entries = _role_packages_v5(
        repository=repository,
        manifest=manifest,
        round_index=round_index,
        payloads=current_payloads,
        policy=authority.admission.policy,
    )
    journal_entries.extend(current_entries)

    role_position = {"investigator": 1, "author": 2, "critic": 3}[request.role]
    current_by_position = {entry[1].call.role_position: entry for entry in current_entries}
    replay = role_position in current_by_position
    if replay:
        replay_package = current_by_position[role_position][1]
        if replay_package.request != request:
            raise ValueError("campaign admission replay request differs")
    elif role_position != len(current_entries) + 1:
        raise ValueError("campaign admission request skips the current role prefix")
    elif any(not package.accepted for _payload, package in current_entries):
        raise ValueError("campaign admission current role predecessor was not accepted")

    current_records = tuple(record for record in records if record.round_index == round_index)
    current_settled = bool(current_records) or any(
        type(payload) in {RoundOutcomePayloadV5, CleanupResultPayloadV5}
        for payload in current_payloads
    )
    if current_settled and not replay:
        raise ValueError("settled campaign round cannot admit a new role")

    observed_requests = _existing_campaign_role_requests_v5(repository, manifest)
    journal_requests = {
        package.call.sha256: (payload.request_ref, package.call, package.request)
        for payload, package in journal_entries
    }
    if len(journal_requests) != len(journal_entries):
        raise ValueError("campaign admission journal reuses a role call")
    observed_by_call = {call.sha256: (reference, call, stored_request) for reference, call, stored_request in observed_requests}
    if len(observed_by_call) != len(observed_requests):
        raise ValueError("campaign admission request calls are duplicated")
    if any(observed_by_call.get(call_sha256) != expected for call_sha256, expected in journal_requests.items()):
        raise ValueError("campaign admission journal request differs from persisted state")
    tail_requests = tuple(
        item
        for call_sha256, item in observed_by_call.items()
        if call_sha256 not in journal_requests
    )
    if len(tail_requests) > 1:
        raise ValueError("campaign admission has multiple unjournaled requests")
    tail_call: RoleCallKeyV5 | None = None
    tail_request: RoleRequestV5 | None = None
    if tail_requests:
        _tail_ref, tail_call, tail_request = tail_requests[0]
        expected_tail_position = len(current_entries) + 1
        if (
            current_settled
            or any(not package.accepted for _payload, package in current_entries)
            or expected_tail_position > 3
            or tail_call.campaign_id != manifest.campaign_id
            or tail_call.round_index != round_index
            or tail_call.role_position != expected_tail_position
            or tail_call.role != ("investigator", "author", "critic")[expected_tail_position - 1]
            or tail_call.attempt_kind != "primary"
            or tail_call.attempt_index != 1
            or tail_call.request_sha256 != tail_request.sha256
        ):
            raise ValueError("campaign admission unjournaled request is not the exact current tail")
        require_role_envelope_v5(
            policy=authority.admission.policy,
            manifest=manifest,
            request=tail_request,
        )
        if not replay and (tail_call.request_sha256 != request.sha256 or tail_request != request):
            raise ValueError("campaign admission current request conflicts with its persisted tail")
    elif not replay and any(
        stored_request.sha256 == request.sha256
        for _reference, _call, stored_request in observed_requests
    ):
        raise ValueError("campaign admission new request reuses a persisted request identity")

    expected_call = RoleCallKeyV5(
        campaign_id=manifest.campaign_id,
        round_index=round_index,
        role=request.role,
        role_position=role_position,
        attempt_kind="primary",
        attempt_index=1,
        request_sha256=request.sha256,
    )
    if replay and current_by_position[role_position][1].call != expected_call:
        raise ValueError("campaign admission replay call differs")

    identity = _role_ledger_read_identity_v5(
        repository,
        manifest,
        api_key_environment_variable=authority.original_config.api_key_environment_variable,
    )
    reservations, terminals = _load_verified_role_ledger_v5(
        repository=repository,
        manifest=manifest,
        identity=identity,
    )
    reservation_calls: list[RoleCallKeyV5] = []
    for reservation in reservations:
        call, stored_request = repository.load_unique_role_request_entry_by_sha256(
            reservation.slot.request.request_sha256
        )
        if (
            call.campaign_id != manifest.campaign_id
            or call.round_index > round_index
            or call.role != stored_request.role
            or call.role_position != {"investigator": 1, "author": 2, "critic": 3}[call.role]
            or call.attempt_kind != "primary"
            or call.attempt_index != 1
            or reservation.slot.request.role != call.role
            or reservation.slot.request.attempt_kind != call.attempt_kind
            or reservation.slot.request.attempt_index != call.attempt_index
        ):
            raise ValueError("campaign admission ledger call differs from persisted request")
        reservation_calls.append(call)
    if tuple((call.round_index, call.role_position) for call in reservation_calls) != tuple(
        sorted((call.round_index, call.role_position) for call in reservation_calls)
    ):
        raise ValueError("campaign admission ledger call order is noncanonical")
    for index in range(1, round_index + 1):
        positions = tuple(call.role_position for call in reservation_calls if call.round_index == index)
        if positions != tuple(range(1, len(positions) + 1)):
            raise ValueError("campaign admission ledger roles are not a round prefix")

    package_count = len(journal_entries)
    if (
        len(reservations) not in {package_count, package_count + 1}
        or len(terminals) not in {package_count, package_count + 1}
        or len(terminals) > len(reservations)
    ):
        raise ValueError("campaign admission ledger differs from the journal prefix")
    _authenticate_role_invocations_v5(
        repository=repository,
        manifest=manifest,
        identity=identity,
        packages=tuple(package for _payload, package in journal_entries),
        reservations=reservations[:package_count],
        terminals=terminals[:package_count],
    )
    ledger_tail_count = len(reservations) - package_count
    if ledger_tail_count:
        if tail_call is None or reservation_calls[-1] != tail_call:
            raise ValueError("campaign admission ledger tail differs from the current request")
    elif len(terminals) != package_count:
        raise ValueError("campaign admission terminal lacks its reservation")
    if len(terminals) == package_count + 1 and ledger_tail_count != 1:
        raise ValueError("campaign admission terminal tail is unsupported")

    prior_call_count = sum(call.round_index < round_index for call in reservation_calls)
    if any(call.round_index >= round_index for call in reservation_calls[:prior_call_count]):
        raise ValueError("campaign admission prior ledger boundary is discontinuous")
    if prior_call_count and len(terminals) < prior_call_count:
        raise ValueError("campaign admission prior round has an incomplete reservation")
    prior_receipt = None if prior_call_count == 0 else terminals[prior_call_count - 1].receipt

    evaluated_rounds = _evaluated_round_indices_v5(
        records=records,
        completed_by_round=completed_by_round,
        round_indices=range(1, round_index),
    )

    return _LiveRolePrefixV5(
        evaluated_feedback_rounds=len(evaluated_rounds),
        attempted_rounds=round_index - 1,
        external_attempts=(
            0 if prior_receipt is None else prior_receipt.cumulative_external_attempts
        ),
        total_tokens=(0 if prior_receipt is None else prior_receipt.cumulative_total_tokens),
        cost_usd=(Decimal("0") if prior_receipt is None else prior_receipt.cumulative_cost_usd),
        replay=replay,
    )


def _load_campaign_boundary_facts_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    manifest: CampaignManifestV5,
    admission: AuthenticatedCampaignAdmissionV5,
    owner_token_sha256: str,
    next_round_index: int,
) -> _CampaignBoundaryFactsV5:
    """Authenticate one settled global frontier without repair or a fake request."""

    from core.pit_optimizer_v5.historical_verification import (
        _existing_campaign_role_requests_v5,
        authenticate_completed_historical_round_resources_v5,
    )
    from core.pit_optimizer_v5.production_provider import (
        _authenticate_role_invocations_v5,
        _load_verified_role_ledger_v5,
        _role_ledger_read_identity_v5,
    )
    from core.pit_optimizer_v5.production_runtime import LocalArchiveReducerFactoryV5

    if (
        type(next_round_index) is not int
        or next_round_index < 1
        or next_round_index > manifest.search.max_feedback_rounds + 1
    ):
        raise ValueError("campaign boundary next round is invalid")
    census = _load_campaign_round_state_census_v5(
        repository=repository,
        manifest=manifest,
        admission=admission,
        owner_token_sha256=owner_token_sha256,
    )
    launches = dict(census.launches)
    starts = dict(census.starts)
    expected_started = tuple(range(1, next_round_index))
    if tuple(starts) != expected_started:
        raise ValueError("campaign boundary does not follow every observed start")
    permitted_launches = set(expected_started)
    if next_round_index <= manifest.search.max_feedback_rounds:
        permitted_launches.add(next_round_index)
    if frozenset(launches) not in {
        frozenset(expected_started),
        frozenset(permitted_launches),
    }:
        raise ValueError("campaign boundary has unsupported pre-start state")
    event_rounds = _campaign_event_rounds_v5(repository, manifest)
    if event_rounds != expected_started:
        raise ValueError("campaign boundary journal differs from observed starts")

    completed_rounds = tuple(
        authenticate_completed_historical_round_resources_v5(
            repository=repository,
            manifest=manifest,
            round_index=index,
        )
        for index in expected_started
    )
    if any(
        completed.round_authority.authenticated_manifest
        != census.authenticated_manifest
        or completed.round_authority.campaign_launch != census.campaign_launch
        or completed.round_authority.original_config_ref
        != admission.policy.adapter_config_ref
        or completed.round_authority.original_config != census.original_config
        or completed.round_authority.owner.owner_token_sha256
        != census.campaign_launch.owner_token_sha256
        for completed in completed_rounds
    ):
        raise ValueError("campaign boundary completed-round authority differs")

    LocalArchiveReducerFactoryV5(repository).verify_projection(
        manifest=manifest,
        panel_plan=census.authenticated_manifest.panel_plan,
        evaluator_contract=census.authenticated_manifest.evaluator_contract,
        repair=False,
    )
    checkpoint = repository.load_checkpoint()
    records = (
        ()
        if checkpoint is None
        else tuple(
            repository.load_experiment(reference)
            for reference in checkpoint.record_refs
        )
    )
    if any(record.round_index not in starts for record in records):
        raise ValueError("campaign boundary checkpoint is outside the settled prefix")

    journal_entries: list[
        tuple[RoleCompletionPayloadV5, RoleInvocationPackageV5]
    ] = []
    completed_by_round: dict[
        int,
        tuple[tuple[RoleCompletionPayloadV5, RoleInvocationPackageV5], ...],
    ] = {}
    for index, completed in zip(expected_started, completed_rounds, strict=True):
        entries = _role_packages_v5(
            repository=repository,
            manifest=manifest,
            round_index=index,
            payloads=completed.payloads,
            policy=admission.policy,
        )
        completed_by_round[index] = entries
        journal_entries.extend(entries)

    observed_requests = _existing_campaign_role_requests_v5(repository, manifest)
    journal_requests = {
        package.call.sha256: (payload.request_ref, package.call, package.request)
        for payload, package in journal_entries
    }
    observed_by_call = {
        call.sha256: (reference, call, request)
        for reference, call, request in observed_requests
    }
    if (
        len(journal_requests) != len(journal_entries)
        or len(observed_by_call) != len(observed_requests)
        or observed_by_call != journal_requests
    ):
        raise ValueError("campaign boundary requests differ from the settled journal")

    identity = _role_ledger_read_identity_v5(
        repository,
        manifest,
        api_key_environment_variable=census.original_config.api_key_environment_variable,
    )
    reservations, terminals = _load_verified_role_ledger_v5(
        repository=repository,
        manifest=manifest,
        identity=identity,
    )
    _authenticate_role_invocations_v5(
        repository=repository,
        manifest=manifest,
        identity=identity,
        packages=tuple(package for _payload, package in journal_entries),
        reservations=reservations,
        terminals=terminals,
    )
    prefix = _compose_campaign_prefix_facts_v5(
        manifest=manifest,
        policy=admission.policy,
        completed_rounds=completed_rounds,
        records=records,
        completed_by_round=completed_by_round,
        observed_started_rounds=tuple(starts),
        prestart_round_index=(
            next_round_index if next_round_index in launches else None
        ),
        allow_final_stop=False,
    )
    return prefix.boundary


def _completed_history_role_facts_v5(
    *,
    manifest: CampaignManifestV5,
    policy: CampaignAdmissionPolicyV5,
    history: _CompletedDevelopmentHistoryV5,
) -> dict[
    int,
    tuple[tuple[RoleCompletionPayloadV5, RoleInvocationPackageV5], ...],
]:
    completed_by_round: dict[
        int,
        tuple[tuple[RoleCompletionPayloadV5, RoleInvocationPackageV5], ...],
    ] = {}
    ordered_packages: list[RoleInvocationPackageV5] = []
    for completed in history.rounds:
        round_index = completed.round_authority.owner.round_index
        packages = tuple(
            package
            for package in history.packages
            if package.call.round_index == round_index
        )
        entries = _validated_role_packages_v5(
            manifest=manifest,
            round_index=round_index,
            payloads=completed.payloads,
            policy=policy,
            packages=packages,
        )
        completed_by_round[round_index] = entries
        ordered_packages.extend(package for _payload, package in entries)
    if tuple(ordered_packages) != history.packages:
        raise ValueError("campaign compliance packages differ from completed history")
    return completed_by_round


def _authenticate_completed_campaign_policy_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    manifest: CampaignManifestV5,
    history: _CompletedDevelopmentHistoryV5,
    expected_policy_ref: ArtifactRefV5 | None,
) -> CampaignPolicyComplianceV5 | None:
    """Compose optional policy compliance after original history authentication."""

    from core.pit_optimizer_v5.development_preparation import (
        _CompletedDevelopmentHistoryV5,
    )

    if type(history) is not _CompletedDevelopmentHistoryV5:
        raise ValueError("campaign compliance history authority is invalid")
    admission = load_campaign_admission_v5(
        repository=repository,
        manifest=manifest,
        expected_policy_ref=expected_policy_ref,
    )
    if admission is None:
        return None
    if (
        type(history.original_config) is not ProviderDevelopmentAdapterConfigV5
        or history.authenticated_manifest.manifest != manifest
        or history.campaign_launch.manifest_ref != admission.policy.manifest_ref
        or history.campaign_launch.adapter_config_ref
        != admission.policy.adapter_config_ref
        or history.campaign_launch.owner_token_sha256
        != admission.policy.owner_token_sha256
        or history.original_config.repository_root_identity_sha256
        != admission.policy.repository_root_identity_sha256
    ):
        raise ValueError("campaign compliance original authority differs")

    census = _load_campaign_round_state_census_v5(
        repository=repository,
        manifest=manifest,
        admission=admission,
        owner_token_sha256=history.campaign_launch.owner_token_sha256,
    )
    configured_rounds = tuple(index for index, _launch in census.launches)
    started_rounds = tuple(index for index, _started in census.starts)
    if (
        census.authenticated_manifest != history.authenticated_manifest
        or census.campaign_launch != history.campaign_launch
        or census.original_config != history.original_config
        or configured_rounds != history.round_indices
        or started_rounds != history.round_indices
    ):
        raise ValueError("campaign compliance round census differs from completed history")

    completed_by_round = _completed_history_role_facts_v5(
        manifest=manifest,
        policy=admission.policy,
        history=history,
    )
    for round_index in history.round_indices:
        prior_count = round_index - 1
        prior = _compose_campaign_prefix_facts_v5(
            manifest=manifest,
            policy=admission.policy,
            completed_rounds=history.rounds[:prior_count],
            records=tuple(
                record
                for record in history.records
                if record.round_index < round_index
            ),
            completed_by_round={
                index: completed_by_round[index]
                for index in range(1, round_index)
            },
            observed_started_rounds=tuple(range(1, round_index)),
            prestart_round_index=None,
            allow_final_stop=False,
        )
        decision = campaign_boundary_decision_v5(
            policy=admission.policy,
            manifest=manifest,
            evaluated_feedback_rounds=prior.boundary.evaluated_feedback_rounds,
            attempted_rounds=prior.boundary.attempted_rounds,
            external_attempts=prior.boundary.external_attempts,
            total_tokens=prior.boundary.total_tokens,
            cost_usd=prior.boundary.cost_usd,
        )
        if decision != "admit":
            raise ValueError(
                f"historical campaign round was not policy-admissible: {decision}"
            )

    final = _compose_campaign_prefix_facts_v5(
        manifest=manifest,
        policy=admission.policy,
        completed_rounds=history.rounds,
        records=history.records,
        completed_by_round=completed_by_round,
        observed_started_rounds=history.round_indices,
        prestart_round_index=None,
        allow_final_stop=True,
    )
    final_decision = campaign_boundary_decision_v5(
        policy=admission.policy,
        manifest=manifest,
        evaluated_feedback_rounds=final.boundary.evaluated_feedback_rounds,
        attempted_rounds=final.boundary.attempted_rounds,
        external_attempts=final.boundary.external_attempts,
        total_tokens=final.boundary.total_tokens,
        cost_usd=final.boundary.cost_usd,
    )
    return CampaignPolicyComplianceV5(
        policy_ref=admission.binding.policy_ref,
        manifest_ref=admission.binding.manifest_ref,
        configured_round_indices=configured_rounds,
        started_epoch_ms_by_round=tuple(
            (index, started.started_epoch_ms)
            for index, started in census.starts
            if started.started_epoch_ms is not None
        ),
        evaluated_feedback_round_indices=final.evaluated_round_indices,
        attempted_rounds=final.boundary.attempted_rounds,
        external_attempts=final.boundary.external_attempts,
        total_tokens=final.boundary.total_tokens,
        cost_usd=final.boundary.cost_usd,
        final_decision=final_decision,
        final_round_outcome=final.final_round_outcome,
        logical_prefix_eligibility="verified",
        temporal_evidence_scope=(
            "stored_campaign_launch_and_round_start_epochs_only"
        ),
        global_publication_timestamps="unavailable",
    )


def authenticate_campaign_policy_compliance_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    manifest: CampaignManifestV5,
    expected_policy_ref: ArtifactRefV5,
) -> CampaignPolicyComplianceV5:
    """Authenticate completed history under one explicitly pinned enrolled policy."""

    if type(expected_policy_ref) is not ArtifactRefV5:
        raise ValueError("expected campaign compliance policy reference is invalid")
    from core.pit_optimizer_v5.development_preparation import (
        _authenticate_completed_development_history_facts_v5,
    )

    history = _authenticate_completed_development_history_facts_v5(
        repository,
        manifest,
    )
    report = _authenticate_completed_campaign_policy_v5(
        repository=repository,
        manifest=manifest,
        history=history,
        expected_policy_ref=expected_policy_ref,
    )
    if report is None:
        raise ValueError("expected campaign compliance enrollment is missing")
    return report


def require_live_role_admission_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    manifest: CampaignManifestV5,
    request: RoleRequestV5,
    round_index: int,
    now_epoch_ms: int,
    owner_token_sha256: str | None = None,
) -> None:
    """Require one exact live role request to retain its original round admission."""

    admission = load_campaign_admission_v5(
        repository=repository,
        manifest=manifest,
    )
    if admission is None:
        return
    require_role_envelope_v5(
        policy=admission.policy,
        manifest=manifest,
        request=request,
    )
    authority = _load_campaign_round_authority_v5(
        repository=repository,
        manifest=manifest,
        admission=admission,
        round_index=round_index,
        owner_token_sha256=owner_token_sha256,
    )
    prefix = _load_live_role_prefix_v5(
        repository=repository,
        manifest=manifest,
        authority=authority,
        request=request,
        round_index=round_index,
    )
    decision = campaign_boundary_decision_v5(
        policy=admission.policy,
        manifest=manifest,
        evaluated_feedback_rounds=prefix.evaluated_feedback_rounds,
        attempted_rounds=prefix.attempted_rounds,
        external_attempts=prefix.external_attempts,
        total_tokens=prefix.total_tokens,
        cost_usd=prefix.cost_usd,
    )
    if decision != "admit":
        raise ValueError(f"campaign role is not admitted: {decision}")
    _require_live_round_deadline_v5(
        manifest=manifest,
        authority=authority,
        now_epoch_ms=now_epoch_ms,
    )
    _require_live_round_deadline_v5(
        manifest=manifest,
        authority=authority,
        now_epoch_ms=time.time_ns() // 1_000_000,
    )


__all__ = [
    "AuthenticatedCampaignAdmissionV5",
    "CampaignAdmissionBindingV5",
    "CampaignAdmissionPolicyV5",
    "CampaignPolicyComplianceV5",
    "authenticate_campaign_policy_compliance_v5",
    "authenticate_prepared_campaign_policy_v5",
    "campaign_boundary_decision_v5",
    "load_campaign_admission_v5",
    "require_live_role_admission_v5",
    "require_role_envelope_v5",
]
