from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.pit_data import PriceIdentityTransitionContract
from core.pit_optimizer_evaluation import EvaluationPanelSpec, PanelSecurityLineage
from core.pit_optimizer_v5.contracts import (
    ProviderCapabilitiesV5,
    RoleEvidenceV5,
    canonical_sha256_v5,
    selected_scenario,
    validate_semantic_mode_v5,
)
from core.pit_optimizer_v5.evaluator import PitPanelEvaluatorV5
from core.pit_optimizer_v5.provider import (
    RoleBindingV5,
    build_role_request_v5,
    role_schema_authority_from_manifest_v5,
)
from core.pit_optimizer_v5.production_runtime import LocalRoleRequestFactoryV5
from core.pit_optimizer_v5.runtime import FeedbackRoundInputV5, SearchProjectionV5
from core.pit_optimizer_v5.search import (
    CandidateArchiveV5,
    SearchStateV5,
    baseline_parent_candidate_v5,
)
from core.pit_provenance import pit_canonical_json_sha256
from tests.test_pit_optimizer_v5_mechanism_artifacts import _capability


def test_investigator_feedback_request_binds_measured_report_and_source_identities(
    tmp_path: Path,
) -> None:
    repository_adapter, capability, _candidate_source, _candidate_revision = _capability(
        tmp_path,
        semantic_mode="required",
    )
    repository = repository_adapter.repository
    authenticated = capability.authenticated_manifest
    manifest = authenticated.manifest
    inputs = FeedbackRoundInputV5(
        manifest=manifest,
        panel_plan=authenticated.panel_plan,
        evaluator_contract=authenticated.evaluator_contract,
        baseline=authenticated.baseline_authority,
        round_index=1,
        owner_token_sha256=canonical_sha256_v5({"test": "r1-controlled-delivery"}),
    )
    parent = baseline_parent_candidate_v5(
        authority=authenticated.baseline_authority,
        discovery_plan=authenticated.panel_plan,
        evaluator_contract=authenticated.evaluator_contract,
        pit_data_scope=manifest.pit_data_scope,
    )
    projection = SearchProjectionV5(
        checkpoint=None,
        state=SearchStateV5(
            next_round_index=1,
            archive=CandidateArchiveV5(capacity=manifest.search.archive_capacity),
            attempted_novelty_keys=(),
        ),
        stored_records=(),
    )
    factory = LocalRoleRequestFactoryV5(repository=repository, manifest=manifest)
    role_input, evidence = factory._investigator_parts(inputs, projection, parent)
    measured_episode = authenticated.baseline_authority.campaign.episodes[0]
    measured_evaluation = measured_episode.evaluation
    measured_report = selected_scenario(measured_evaluation).report
    measured = next(
        item
        for item in evidence.items
        if item.metric_id == "episode.1.portfolio_annualized_return_pct"
    )

    assert measured.value == measured_report.portfolio_annualized_return_pct
    assert measured.description is not None
    for identity in (
        canonical_sha256_v5(measured_report),
        canonical_sha256_v5(measured_evaluation),
        parent.policy_identity_sha256,
        authenticated.baseline_authority.source_bundle_ref.sha256,
        authenticated.evaluator_contract.sha256,
        authenticated.evaluator_contract.sandbox_profile_sha256,
        authenticated.panel_plan.pit_bundle_ref.sha256,
        authenticated.panel_plan.prices_provenance_ref.sha256,
        measured_evaluation.panel_sha256,
    ):
        assert identity in measured.description

    schema = role_schema_authority_from_manifest_v5(
        role="investigator",
        manifest=manifest,
    )
    request = build_role_request_v5(
        role="investigator",
        role_input=role_input,
        issued_evidence=evidence,
        expected_binding=RoleBindingV5(
            parent.policy_identity_sha256,
            None,
            (),
            authenticated.panel_plan.discovery_plan_sha256,
        ),
        schema_authority=schema,
        max_output_tokens=4096,
    )
    report_identity = canonical_sha256_v5(measured_report)
    altered_items = tuple(
        replace(item, description=item.description.replace(identity, "0" * 64))
        if item.evidence_id == measured.evidence_id
        else item
        for item in evidence.items
        for identity in (report_identity,)
    )
    altered = build_role_request_v5(
        role="investigator",
        role_input=role_input,
        issued_evidence=RoleEvidenceV5(5, altered_items),
        expected_binding=request.expected_binding,
        schema_authority=schema,
        max_output_tokens=4096,
    )
    assert altered.sha256 != request.sha256


def test_engineering_scope_requires_semantics_and_no_provider(tmp_path: Path) -> None:
    repository_adapter, capability, _candidate_source, _candidate_revision = _capability(tmp_path)
    manifest = replace(capability.authenticated_manifest.manifest, pit_data_scope="engineering_v3")
    assert manifest.semantic_mode == "required"
    with pytest.raises(ValueError, match="disabled semantics require development"):
        validate_semantic_mode_v5("engineering_v3", "disabled_development")
    with pytest.raises(ValueError, match="cannot authorize a provider"):
        replace(
            manifest,
            provider=ProviderCapabilitiesV5(
                model="offline-test",
                maximum_role_calls=1,
                maximum_total_tokens=100,
                maximum_output_tokens_per_role=10,
            ),
        )


def test_engineering_evaluator_accepts_schema_v3_baseline_and_rejects_held_out(
    tmp_path: Path,
) -> None:
    _repository_adapter, capability, _candidate_source, _candidate_revision = _capability(tmp_path)
    authenticated = capability.authenticated_manifest
    provenance = tmp_path / "engineering-provenance.json"
    provenance.write_text("{}\n", encoding="utf-8")
    provenance_sha256 = hashlib.sha256(provenance.read_bytes()).hexdigest()
    transition_sha256 = pit_canonical_json_sha256({})
    transition = PriceIdentityTransitionContract(
        prices_provenance_sha256=provenance_sha256,
        request_contracts_sha256=transition_sha256,
        identities={},
        transitions=(),
    )

    class EngineeringBundle:
        sha256 = "b" * 64
        metadata = {"schema_version": "3", "warmup_start": "2026-01-01"}

        def load_price_identity_transition_contract(self, _path: Path) -> PriceIdentityTransitionContract:
            return transition

    contract = replace(
        authenticated.evaluator_contract,
        pit_bundle_sha256=EngineeringBundle.sha256,
        prices_provenance_sha256=provenance_sha256,
        identity_transition_contract_sha256=transition_sha256,
    )
    common = dict(
        contract=contract,
        sandbox_profile=authenticated.sandbox_profile,
        resource_manifest=SimpleNamespace(
            evaluation_cpu_limit=authenticated.sandbox_profile.cpu_limit,
            evaluation_memory_mib=authenticated.sandbox_profile.memory_limit_mib,
            evaluation_output_limit_bytes=authenticated.sandbox_profile.output_limit_bytes,
            evaluation_pid_limit=authenticated.sandbox_profile.pid_limit,
        ),
        execution_profile=authenticated.execution_profile,
        prices_provenance=provenance,
        report_builder=lambda **_kwargs: None,
        baseline_policy_revision=authenticated.baseline_policy_revision,
        pit_data_scope="engineering_v3",
    )
    evaluator = PitPanelEvaluatorV5(pit_bundle=EngineeringBundle(), **common)
    held_out = EvaluationPanelSpec.from_lineages(
        purpose="qualification",
        sessions=("2026-03-24", "2026-03-25"),
        lineages=(PanelSecurityLineage("fixture-aaa", ("AAA",), ("sp500",)),),
    )
    with pytest.raises(ValueError, match="cannot authorize held-out"):
        evaluator.evaluate_baseline(held_out, scenario_ids=("base",))

    class WrongSchemaBundle(EngineeringBundle):
        metadata = {"schema_version": "2", "warmup_start": "2026-01-01"}

    with pytest.raises(ValueError, match="schema-V3"):
        PitPanelEvaluatorV5(pit_bundle=WrongSchemaBundle(), **common)


def test_engineering_round_two_fails_without_identified_persisted_measurement(tmp_path: Path) -> None:
    repository_adapter, capability, _candidate_source, _candidate_revision = _capability(tmp_path)
    authenticated = capability.authenticated_manifest
    baseline = replace(authenticated.baseline_authority, pit_data_scope="engineering_v3")
    manifest = replace(authenticated.manifest, pit_data_scope="engineering_v3")
    inputs = FeedbackRoundInputV5(
        manifest=manifest,
        panel_plan=authenticated.panel_plan,
        evaluator_contract=authenticated.evaluator_contract,
        baseline=baseline,
        round_index=2,
        owner_token_sha256=canonical_sha256_v5({"test": "engineering-missing-feedback"}),
    )
    parent = baseline_parent_candidate_v5(
        authority=baseline,
        discovery_plan=authenticated.panel_plan,
        evaluator_contract=authenticated.evaluator_contract,
        pit_data_scope="engineering_v3",
    )
    projection = SearchProjectionV5(
        checkpoint=None,
        state=SearchStateV5(
            next_round_index=1,
            archive=CandidateArchiveV5(capacity=manifest.search.archive_capacity),
            attempted_novelty_keys=(),
        ),
        stored_records=(),
    )
    factory = LocalRoleRequestFactoryV5(repository=repository_adapter.repository, manifest=manifest)
    with pytest.raises(ValueError, match="lacks its measured experiment identity"):
        factory._investigator_parts(inputs, projection, parent)
    identified = LocalRoleRequestFactoryV5(
        repository=repository_adapter.repository,
        manifest=manifest,
        feedback_experiment_id="a" * 64,
    )
    with pytest.raises(ValueError, match="absent or ambiguous"):
        identified._investigator_parts(inputs, projection, parent)
