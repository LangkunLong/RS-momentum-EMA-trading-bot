"""Task 7 integration contracts for the linked two-round study driver."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.pit_optimizer_v5.artifacts import LocalArtifactRepositoryV5
from core.pit_optimizer_v5.contracts import (
    HypothesisV5,
    InvestigatorArtifactV5,
    MetricPredictionV5,
)
from core.pit_optimizer_v5.mechanism_artifacts import (
    MechanismArtifactRepositoryV5,
    manifest_source_identity_sha256_v1,
    MechanismRuntimeExtensionV1,
)
from core.pit_optimizer_v5.memory import RoleCompletionPayloadV5, StoredExperimentRecordV5
from core.pit_optimizer_v5.provider import MechanismRoleInputV1
from core.pit_optimizer_v5.mechanism_contracts import (
    MechanismDisconfirmingObservationV1,
    MechanismMetricSpecV1,
    MechanismPredicateV1,
    MechanismRecipeV1,
)
from core.pit_optimizer_v5.provider import CompletionResultV5
from core.pit_optimizer_v5.two_round_study.contracts import StudyAdmissionError, StudyAuthorityError
from core.pit_optimizer_v5.two_round_study.contrast import StudyContrastV1, evaluate_case_contrast_v1
from core.pit_optimizer_v5.two_round_study.driver import (
    PreparedStudyV1,
    StudyArmResultV1,
    _authenticated_closed_snapshot,
    _clone_snapshot,
    _require_actual_p1_distinct_child,
    _require_round_one_cleanup,
    _snapshot_inventory,
    _implementation_source_revision,
    execute_study_arm_v1,
    prepare_two_round_study_v1,
    resume_study_arm_v1,
)
from core.pit_optimizer_v5.two_round_study.fixtures import reopen_study_fixture_v1
from core.pit_optimizer_v5.two_round_study.contracts import StudyResponseV1
from core.pit_optimizer_v5.two_round_study.ledger import StudyLedgerV1
from core.pit_optimizer_v5.two_round_study.live_calls import (
    StudyGrantV1,
    authorize_offline_fixture_v1,
    authenticate_fixture_preflight_v1,
)
from core.pit_optimizer_v5.two_round_study.store import StudyStoreV1


def _offline_ledger(prepared: PreparedStudyV1) -> StudyLedgerV1:
    store = StudyStoreV1(prepared.store_repository())
    grant = StudyGrantV1(
        study_id=prepared.manifest.study_id,
        manifest_sha256=prepared.manifest.sha256,
        repository_root_identity_sha256=store.repository.root_identity_sha256,
        audit_domain="task7-offline-audit",
        mode="offline_fixture",
        provider="offline_fixture",
        model="offline_fixture/study-v1",
        arm_slots=("primary", "withheld"),
        input_token_ceiling=1_000_000,
        output_token_ceiling=8_192,
        cumulative_token_ceiling=2_000_000,
        per_call_usd_ceiling=Decimal("1"),
        cumulative_usd_ceiling=Decimal("2"),
        per_call_deadline_seconds=Decimal("120"),
        input_price_upper_bound=Decimal("1"),
        output_price_upper_bound=Decimal("1"),
        response_persistence_consent=True,
        operator_approval_reference="offline-fixture:task7-tests",
    )
    approval = authorize_offline_fixture_v1(
        store=store,
        manifest=prepared.manifest,
        grant=grant,
        approval_reference=grant.operator_approval_reference,
    )
    return StudyLedgerV1(store, prepared.manifest, grant, approval)


def _response_for_request(
    request,
    *,
    hypothesis_id: str,
    claim: str,
    configuration_id: str = "S-gte-0.05",
    expected_changed: tuple[bool, bool, bool, bool] = (False, True, True, False),
) -> str:
    evidence_ids = tuple(item.evidence_id for item in request.role_evidence.items)
    if not evidence_ids:
        raise AssertionError("round-two F request has no evidence to cite")
    hypothesis = HypothesisV5(
        hypothesis_id=hypothesis_id,
        rank=1,
        primary_mechanism="exit",
        causal_claim=claim,
        predicted_changes=(
            MetricPredictionV5("exit.decision_changed_count", "increase", "Applicable cases change."),
            MetricPredictionV5("exit.protected_control_unchanged_count", "unchanged", "Control holds."),
        ),
        evidence_ids=evidence_ids[:1],
        author_instructions="Use the registered exit mechanism recipe.",
        authoring_mode="symbol_edits",
    )
    draft = __import__(
        "core.pit_optimizer_v5.two_round_study.contracts",
        fromlist=["ExperimentDraftV1", "RivalPatternV1"],
    ).ExperimentDraftV1(
        hypothesis_id=hypothesis_id,
        cited_evidence_ids=evidence_ids[:1],
        applicability=MechanismPredicateV1("features.atr_20_fraction", "is_present", None),
        recipe=MechanismRecipeV1(
            recipe_id="evaluate_exit_atr20_fraction_v1",
            input_field="features.atr_20_fraction",
            input_values=(Decimal("0.049"), Decimal("0.050"), Decimal("0.051"), None),
        ),
        metrics=(
            MechanismMetricSpecV1(
                "exit.decision_changed_count",
                "count",
                "increase",
                Decimal("0"),
                "relevant_cases",
            ),
            MechanismMetricSpecV1(
                "exit.protected_control_unchanged_count",
                "count",
                "unchanged",
                Decimal("0"),
                "control_cases",
            ),
        ),
        disconfirming_observations=(
            MechanismDisconfirmingObservationV1(
                "protected_control_changed", "exit.protected_control_unchanged_count"
            ),
        ),
        expected_changed=expected_changed,
        rivals=(
            __import__(
                "core.pit_optimizer_v5.two_round_study.contracts",
                fromlist=["RivalPatternV1"],
            ).RivalPatternV1("inert", (False, False, False, False)),
            __import__(
                "core.pit_optimizer_v5.two_round_study.contracts",
                fromlist=["RivalPatternV1"],
            ).RivalPatternV1("always_on", (True, True, True, True)),
        ),
        configuration_id=configuration_id,
        claim_kind="threshold",
    )
    return StudyResponseV1(
        1,
        request.expected_binding,
        InvestigatorArtifactV5((hypothesis,)),
        (draft,),
    ).canonical_bytes().decode("utf-8")


class _OneShotStudyGateway:
    def __init__(self, responses: dict[str, str]) -> None:
        self.responses = dict(responses)
        self.calls: list[str] = []

    def invoke_json_once(self, *, request_sha256: str, model: str, **kwargs: object) -> CompletionResultV5:
        self.calls.append(request_sha256)
        return CompletionResultV5(
            response_text=self.responses[request_sha256],
            accepted=True,
            input_tokens=1,
            output_tokens=1,
            provider_request_id=f"task7-{len(self.calls)}",
            returned_model=model,
            cost_usd=Decimal("0"),
            external_attempt_count=1,
            response_received=True,
        )


class _UnavailableStudyGateway:
    """Offline transport that records a single unavailable provider attempt."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def invoke_json_once(self, *, request_sha256: str, model: str, **kwargs: object) -> CompletionResultV5:
        self.calls.append(request_sha256)
        return CompletionResultV5(
            response_text="",
            accepted=False,
            input_tokens=1,
            output_tokens=0,
            provider_request_id=f"task7-unavailable-{len(self.calls)}",
            returned_model=model,
            cost_usd=Decimal("0"),
            external_attempt_count=1,
            response_received=False,
        )


class _PendingStudyGateway:
    """Offline transport that stops after dispatch, leaving accounting pending."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def invoke_json_once(self, *, request_sha256: str, **kwargs: object) -> CompletionResultV5:
        self.calls.append(request_sha256)
        raise RuntimeError("task7 synthetic interruption after dispatch")


def _round_two_evidence(prepared: PreparedStudyV1, arm: str, result: StudyArmResultV1):
    fixture = reopen_study_fixture_v1(
        root=prepared.fixture_path(arm),
        manifest_ref=prepared.primary_manifest_ref if arm == "primary" else prepared.withheld_manifest_ref,
        registry=prepared.registry,
    )
    checkpoint = fixture.repository.load_checkpoint()
    assert checkpoint is not None
    records = tuple(
        StoredExperimentRecordV5(reference, fixture.repository.load_experiment(reference))
        for reference in checkpoint.record_refs
        if fixture.repository.load_experiment(reference).round_index == 2
    )
    assert len(records) == 1
    persisted = MechanismArtifactRepositoryV5(fixture.repository).load_existing_evidence_for_record(
        campaign_id=fixture.manifest.manifest.campaign_id,
        round_index=2,
        manifest_ref=fixture.manifest.manifest_ref,
        manifest_source_identity_sha256=manifest_source_identity_sha256_v1(fixture.manifest),
        stored_record=records[0],
    )
    assert persisted is not None
    contrast_ref = next(
        reference
        for reference in result.contrast_refs
        if reference.relative_path.startswith("adapter-blobs/study-v1-contrasts/")
    )
    contrast = StudyContrastV1.from_canonical_json(
        StudyStoreV1(prepared.store_repository()).read(contrast_ref)
    )
    return fixture, records[0], persisted[0], contrast


def test_preparation_builds_real_round_one_and_two_isolated_preflights(tmp_path: Path) -> None:
    prepared = prepare_two_round_study_v1(
        root=tmp_path / "study",
        mode="offline_fixture",
        provider_settings=None,
    )

    assert isinstance(prepared, PreparedStudyV1)
    assert prepared.primary_preflight.arm == "primary"
    assert prepared.withheld_preflight.arm == "withheld"
    assert prepared.primary_preflight.fixture_root_identity_sha256 != (
        prepared.withheld_preflight.fixture_root_identity_sha256
    )
    assert prepared.primary_preflight.checkpoint_bytes == prepared.withheld_preflight.checkpoint_bytes
    assert prepared.primary_preflight.snapshot_bytes == prepared.withheld_preflight.snapshot_bytes
    assert prepared.primary_preflight.request_bytes != prepared.withheld_preflight.request_bytes
    assert prepared.primary_live_call_sha256 != prepared.withheld_live_call_sha256
    assert prepared.manifest.mode == "offline_fixture"
    assert prepared.manifest.source_revision == _implementation_source_revision()
    assert prepared.manifest.source_revision != "a" * 40
    assert prepared.manifest.offline_settings is not None
    assert prepared.manifest.provider_settings is None
    assert prepared.primary_preflight_ref != prepared.withheld_preflight_ref
    assert prepared.request_comparison_ref is not None
    assert not prepared.has_execution_grant
    assert prepared.selected_parent_configuration_id == "S"
    ancestor = LocalArtifactRepositoryV5(prepared.ancestor_root)
    a_revision = prepared.registry.configuration("A").policy_revision
    a_records = tuple(
        ancestor.load_experiment(reference)
        for reference in prepared.round_one_record_refs
        if ancestor.load_experiment(reference).policy_revision == a_revision
    )
    assert len(a_records) == 1
    assert a_records[0].experiment_id in prepared.primary_memory_summary_ids

    # The closed-graph allowlist rejects a canonical-looking file that is not
    # reachable from an authenticated control record.  This regression keeps
    # snapshot admission fail-closed instead of treating the directory as its
    # own manifest.
    ancestor_fixture = reopen_study_fixture_v1(
        root=prepared.ancestor_root,
        manifest_ref=prepared.ancestor_manifest_ref,
        registry=prepared.registry,
    )
    (prepared.ancestor_root / "unrelated-task7-sentinel.json").write_text(
        '{"schema_version": 1, "artifact_type": "unrelated"}', encoding="utf-8"
    )
    with pytest.raises(StudyAuthorityError, match="unauthenticated path"):
        _authenticated_closed_snapshot(ancestor_fixture)


def test_preparation_refuses_an_occupied_root(tmp_path: Path) -> None:
    root = tmp_path / "occupied"
    root.mkdir()
    (root / "keep.txt").write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(StudyAuthorityError, match="fresh|occupied|empty"):
        prepare_two_round_study_v1(root=root, mode="offline_fixture", provider_settings=None)


def test_preparation_rejects_a_preexisting_empty_child_directory(tmp_path: Path) -> None:
    import core.pit_optimizer_v5.two_round_study.driver as driver_module

    root = tmp_path / ("task7-empty-root-" + ("x" * 210))
    empty_child = root / "unrelated-empty"
    import os

    os.makedirs(driver_module._long_path(empty_child), exist_ok=True)
    with pytest.raises(StudyAuthorityError, match="fresh|occupied|empty"):
        prepare_two_round_study_v1(root=root, mode="offline_fixture", provider_settings=None)
    assert driver_module._long_exists(empty_child)
    assert tuple(driver_module._long_files(empty_child)) == ()


def test_clone_rejects_a_preexisting_empty_child_directory(tmp_path: Path) -> None:
    import core.pit_optimizer_v5.two_round_study.driver as driver_module
    import os

    destination = tmp_path / ("task7-empty-destination-" + ("x" * 210))
    empty_child = destination / "unrelated-empty"
    os.makedirs(driver_module._long_path(empty_child), exist_ok=True)
    with pytest.raises(StudyAuthorityError, match="fresh|occupied|empty"):
        _clone_snapshot((("known.json", b"{}"),), destination)
    assert driver_module._long_exists(empty_child)
    assert not driver_module._long_exists(destination / "known.json")


def test_round_one_cleanup_gate_rejects_incomplete_result_before_snapshot() -> None:
    from core.pit_optimizer_v5.artifacts import ArtifactRefV5, RepositoryCheckpointV5
    from core.pit_optimizer_v5.memory import CleanupResultPayloadV5
    from core.pit_optimizer_v5.runtime import FeedbackRoundResultV5, RuntimeFailureV5

    checkpoint = RepositoryCheckpointV5(
        generation=1,
        archive_sha256="a" * 64,
        record_refs=(ArtifactRefV5("experiments/one.json", "b" * 64),),
    )
    result = FeedbackRoundResultV5(
        status="completed",
        campaign_id="task7-cleanup-gate",
        round_index=1,
        parent=None,
        terminal_outcome=None,
        record_refs=checkpoint.record_refs,
        checkpoint=checkpoint,
        cleanup=CleanupResultPayloadV5(1, 0, 0, 0, False, "cleanup_execution_failed"),
        failure=None,
        cleanup_failure=RuntimeFailureV5("cleanup", "cleanup_failed"),
    )
    with pytest.raises(StudyAuthorityError, match="cleanup"):
        _require_round_one_cleanup(result)


def test_preparation_rejects_runtime_cleanup_failure_before_snapshot_or_clone(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import core.pit_optimizer_v5.two_round_study.driver as driver_module

    from core.pit_optimizer_v5.memory import CleanupResultPayloadV5
    from core.pit_optimizer_v5.runtime import RuntimeFailureV5

    original_run = driver_module.run_feedback_round_v5

    def return_cleanup_failure(*, inputs, dependencies):
        result = original_run(inputs=inputs, dependencies=dependencies)
        assert result.cleanup is not None
        return replace(
            result,
            cleanup=CleanupResultPayloadV5(
                result.cleanup.owned_workspaces,
                result.cleanup.owned_policy_workers,
                result.cleanup.owned_evaluators,
                result.cleanup.owned_containers,
                False,
                "cleanup_execution_failed",
            ),
            cleanup_failure=RuntimeFailureV5("cleanup", "cleanup_failed"),
        )

    monkeypatch.setattr(driver_module, "run_feedback_round_v5", return_cleanup_failure)
    root = tmp_path / "study"
    with pytest.raises(StudyAuthorityError, match="cleanup"):
        prepare_two_round_study_v1(root=root, mode="offline_fixture", provider_settings=None)
    assert not (root / "primary-descendant").exists()
    assert not (root / "withheld-descendant").exists()


def test_snapshot_binds_runtime_controls_to_contract_authorities(tmp_path: Path) -> None:
    prepared = prepare_two_round_study_v1(
        root=tmp_path / "study",
        mode="offline_fixture",
        provider_settings=None,
    )
    fixture = reopen_study_fixture_v1(
        root=prepared.ancestor_root,
        manifest_ref=prepared.ancestor_manifest_ref,
        registry=prepared.registry,
    )
    runtime_source = prepared.ancestor_root / "evaluator" / "study-runtime-source.json"
    runtime_source.write_bytes(b'{"fixture":"replaced-control"}')
    with pytest.raises(StudyAuthorityError, match="runtime source|authority|digest"):
        _authenticated_closed_snapshot(fixture)


def test_snapshot_rejects_transition_replacement_and_missing_scripted_authority_index(tmp_path: Path) -> None:
    prepared = prepare_two_round_study_v1(
        root=tmp_path / "study",
        mode="offline_fixture",
        provider_settings=None,
    )
    fixture = reopen_study_fixture_v1(
        root=prepared.ancestor_root,
        manifest_ref=prepared.ancestor_manifest_ref,
        registry=prepared.registry,
    )
    transition = prepared.ancestor_root / "adapter-blobs" / "study-fixture" / "transition.bin"
    transition_raw = transition.read_bytes()
    transition.write_bytes(b"replaced-transition-control")
    with pytest.raises(StudyAuthorityError, match="transition|authority|digest"):
        _authenticated_closed_snapshot(fixture)
    transition.write_bytes(transition_raw)

    # The descendant preflight pins the round-two authority.  This mutation is
    # against the round-one ancestor snapshot, so derive its own typed index
    # edge explicitly rather than borrowing a descendant evidence ref.
    scripted_index_path = (
        prepared.ancestor_root
        / "adapter-state-authority"
        / "study-scripted-role"
        / f"{prepared.primary_preflight.call.campaign_id}-1.json"
    )
    scripted_index_raw = scripted_index_path.read_bytes()
    scripted_index_path.unlink()
    with pytest.raises(StudyAuthorityError, match="scripted|authority|index"):
        _authenticated_closed_snapshot(fixture)
    scripted_index_path.write_bytes(scripted_index_raw)


def test_preflight_retains_authenticated_sidecars_and_detects_substitution(tmp_path: Path) -> None:
    prepared = prepare_two_round_study_v1(
        root=tmp_path / "study",
        mode="offline_fixture",
        provider_settings=None,
    )
    assert prepared.primary_preflight.evidence_refs
    assert prepared.withheld_preflight.evidence_refs
    assert len(prepared.primary_preflight.evidence_refs) >= 2
    for sidecar, raw in zip(
        prepared.primary_preflight.evidence_refs,
        prepared.primary_preflight.evidence_bytes,
        strict=True,
    ):
        sidecar_path = prepared.primary_root.joinpath(*sidecar.relative_path.split("/"))
        sidecar_path.write_bytes(b"substituted-sidecar")
        with pytest.raises(StudyAuthorityError, match="evidence|preflight|authority"):
            authenticate_fixture_preflight_v1(prepared.primary_preflight, require_current=False)
        sidecar_path.unlink()
        with pytest.raises(StudyAuthorityError, match="evidence|preflight|authority"):
            authenticate_fixture_preflight_v1(prepared.primary_preflight, require_current=False)
        sidecar_path.write_bytes(raw)


def test_actual_p1_requires_a_registry_distinct_child(tmp_path: Path, monkeypatch) -> None:
    prepared = prepare_two_round_study_v1(
        root=tmp_path / "study",
        mode="offline_fixture",
        provider_settings=None,
    )
    selected = SimpleNamespace(
        policy_identity_sha256=prepared.registry.configuration("S").policy_revision.sha256
    )
    child_id = _require_actual_p1_distinct_child(prepared.registry, selected)
    assert child_id.startswith("S-")
    assert child_id in {item.configuration_id for item in prepared.registry.configurations}

    import core.pit_optimizer_v5.two_round_study.driver as driver_module
    from core.pit_optimizer_v5.probes import (
        PROBE_SUITE_ID_V5,
        SemanticFingerprintComparisonV5,
        classify_semantic_fingerprints_v5,
    )
    parent_config = prepared.registry.configuration("S")
    child_config = prepared.registry.configuration(child_id)
    assert child_config.parent_configuration_id == parent_config.configuration_id
    semantic = classify_semantic_fingerprints_v5(parent_config.fixed_suite, child_config.fixed_suite)
    assert semantic.classification == "behaviorally_distinct_on_suite_v1"
    assert semantic.differing_probe_ids
    assert child_config.policy_revision.sha256 != parent_config.policy_revision.sha256

    monkeypatch.setattr(
        driver_module,
        "classify_semantic_fingerprints_v5",
        lambda _parent, _candidate: SemanticFingerprintComparisonV5(
            PROBE_SUITE_ID_V5,
            "behavioral_equivalent_on_suite_v1",
            (),
        ),
    )
    with pytest.raises(StudyAuthorityError, match="distinct|discriminating"):
        _require_actual_p1_distinct_child(prepared.registry, selected)


def test_explicit_long_root_preserves_authority_inventory_and_fresh_reopen(tmp_path: Path) -> None:
    root = tmp_path / ("task7-long-root-" + ("x" * 210))
    assert len(str(root)) >= 240
    prepared = prepare_two_round_study_v1(
        root=root,
        mode="offline_fixture",
        provider_settings=None,
    )
    inventory = dict(prepared.common_ancestor_files)
    required = {
        "evaluator/study-runtime-source.json",
        "adapter-blobs/study-fixture/transition.bin",
        "adapter-state-authority/study-scripted-role/"
        + prepared.primary_preflight.call.campaign_id
        + "-1.json",
        "adapter-state/mechanism-v5/"
        + prepared.primary_preflight.call.campaign_id
        + "-0001.json",
        "adapter-state-authority/mechanism-v5/"
        + prepared.primary_preflight.call.campaign_id
        + "-0001.json",
    }
    assert required.difference(inventory) == set()
    mechanism_blobs = {
        path for path in inventory if path.startswith("adapter-blobs/mechanism-v5/")
    }
    spec_stems = {path.removesuffix("-spec.bin") for path in mechanism_blobs if path.endswith("-spec.bin")}
    corpus_stems = {path.removesuffix("-corpus.bin") for path in mechanism_blobs if path.endswith("-corpus.bin")}
    assert spec_stems.intersection(corpus_stems)
    round_two_paths = {reference.relative_path for reference in prepared.primary_preflight.evidence_refs}
    campaign_id = prepared.primary_preflight.call.campaign_id
    assert f"adapter-state/study-scripted-role/{campaign_id}-2.json" in round_two_paths
    assert f"adapter-state-authority/study-scripted-role/{campaign_id}-2.json" in round_two_paths
    assert all(
        hashlib.sha256(raw).hexdigest() == reference.sha256
        for reference, raw in zip(
            prepared.primary_preflight.evidence_refs,
            prepared.primary_preflight.evidence_bytes,
            strict=True,
        )
    )
    policy_values = {
        path for path in inventory if path.startswith("adapter-state/policy-source/")
    }
    policy_authorities = {
        path for path in inventory if path.startswith("adapter-state-authority/policy-source/")
    }
    assert len(policy_values) >= 2
    assert {
        path.replace("adapter-state-authority/", "adapter-state/", 1)
        for path in policy_authorities
    } == policy_values
    assert any(path.startswith("events/") for path in inventory)
    primary_inventory = dict(_snapshot_inventory(prepared.primary_root))
    withheld_inventory = dict(_snapshot_inventory(prepared.withheld_root))
    assert all(primary_inventory.get(path) == digest for path, digest in inventory.items())
    assert all(withheld_inventory.get(path) == digest for path, digest in inventory.items())
    assert prepared.primary_preflight.fixture_root_identity_sha256 != prepared.withheld_preflight.fixture_root_identity_sha256


def test_both_arms_execute_real_round_two_and_reader_replays_without_gateway(tmp_path: Path) -> None:
    prepared = prepare_two_round_study_v1(
        root=tmp_path / "study",
        mode="offline_fixture",
        provider_settings=None,
    )
    ledger = _offline_ledger(prepared)
    primary_request = authenticate_fixture_preflight_v1(prepared.primary_preflight, require_current=True)
    withheld_request = authenticate_fixture_preflight_v1(prepared.withheld_preflight, require_current=True)
    gateway = _OneShotStudyGateway(
        {
            prepared.primary_live_call.sha256: _response_for_request(
                primary_request,
                hypothesis_id="study-exit-primary-v1",
                claim="The local contradiction is acknowledged; portfolio effect is a separate result.",
            ),
            prepared.withheld_live_call.sha256: _response_for_request(
                withheld_request,
                hypothesis_id="study-exit-withheld-v1",
                claim="Base evidence is independently authored for this arm.",
            ),
        }
    )

    primary = execute_study_arm_v1(prepared=prepared, arm="primary", ledger=ledger, gateway=gateway)
    withheld = execute_study_arm_v1(prepared=prepared, arm="withheld", ledger=ledger, gateway=gateway)

    assert primary.state == "completed", f"code={primary.failure_code!r} reason={primary.failure_reason!r} result={primary!r}"
    assert withheld.state == "completed", f"code={withheld.failure_code!r} reason={withheld.failure_reason!r} result={withheld!r}"
    assert primary.feedback_result is not None
    assert withheld.feedback_result is not None
    assert primary.reopened_feedback_result is not None
    assert withheld.reopened_feedback_result is not None
    assert primary.experiment_outcome != primary.optimization_improvement
    assert primary.evidence_use == "not_assessed"
    assert primary.optimization_improvement == "not_established"
    assert primary.authored_code_execution == "not_established"
    assert len(gateway.calls) == 2
    assert len(set(gateway.calls)) == 2
    assert primary.contrast_refs
    assert withheld.contrast_refs
    shared_refs = set(primary.contrast_refs).intersection(withheld.contrast_refs)
    assert all("study-v1-mechanism-corpora/" in ref.relative_path for ref in shared_refs)
    assert any("study-v1-commitments/primary-" in ref.relative_path for ref in primary.contrast_refs)
    assert any("study-v1-commitments/withheld-" in ref.relative_path for ref in withheld.contrast_refs)

    # Both arms deliberately use the same registered P1 policy revision.  The
    # equal revision is retained as an outcome fact, while offline attribution
    # remains inconclusive and the arm graph identities stay separate.
    primary_fixture = reopen_study_fixture_v1(
        root=prepared.primary_root,
        manifest_ref=prepared.primary_manifest_ref,
        registry=prepared.registry,
    )
    withheld_fixture = reopen_study_fixture_v1(
        root=prepared.withheld_root,
        manifest_ref=prepared.withheld_manifest_ref,
        registry=prepared.registry,
    )
    primary_round_two = tuple(
        primary_fixture.repository.load_experiment(reference)
        for reference in primary_fixture.repository.load_checkpoint().record_refs
        if primary_fixture.repository.load_experiment(reference).round_index == 2
    )
    withheld_round_two = tuple(
        withheld_fixture.repository.load_experiment(reference)
        for reference in withheld_fixture.repository.load_checkpoint().record_refs
        if withheld_fixture.repository.load_experiment(reference).round_index == 2
    )
    assert len(primary_round_two) == len(withheld_round_two) == 1
    assert primary_round_two[0].policy_revision == withheld_round_two[0].policy_revision
    assert primary.feedback_attribution == withheld.feedback_attribution == "inconclusive"

    for arm in ("primary", "withheld"):
        fixture = __import__(
            "core.pit_optimizer_v5.two_round_study.fixtures",
            fromlist=["reopen_study_fixture_v1"],
        ).reopen_study_fixture_v1(
            root=prepared.fixture_path(arm),
            manifest_ref=prepared.primary_manifest_ref if arm == "primary" else prepared.withheld_manifest_ref,
            registry=prepared.registry,
        )
        events = fixture.repository.load_round_events(
            campaign_id=fixture.manifest.manifest.campaign_id,
            round_index=2,
        )
        kinds = {event.event_kind for event in events}
        assert {"role_completion", "round_intent", "cleanup_result"}.issubset(kinds)
        role_requests = fixture.repository.load_authenticated_role_requests(
            campaign_id=fixture.manifest.manifest.campaign_id,
            round_index=2,
        )
        assert {call.role for _reference, call, _request in role_requests} == {
            "investigator",
            "author",
            "critic",
        }
        assert "candidate_stage_result" in kinds
        assert "episode_evidence" in kinds
        completions = tuple(
            fixture.repository.load_role_invocation(
                fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
            )
            for event in events
            if event.event_kind == "role_completion"
        )
        assert {package.call.role for package in completions} == {"investigator", "author", "critic"}
        assert all(package.accepted for package in completions)
        checkpoint = fixture.repository.load_checkpoint()
        assert checkpoint is not None
        published = tuple(fixture.repository.load_experiment(reference) for reference in checkpoint.record_refs)
        assert published
        # The checkpoint is append-only and therefore retains the seeded
        # round-one records alongside the newly published round-two row.
        # Require the actual round-two publication while preserving that
        # ordinary history remains in the authenticated checkpoint.
        round_two_published = tuple(record for record in published if record.round_index == 2)
        assert round_two_published
        assert all(record.status in {"evaluated", "zero_trade"} for record in round_two_published)

    reader = StudyLedgerV1(
        StudyStoreV1(prepared.store_repository()),
        prepared.manifest,
        ledger.grant,
        approval=None,
    )
    replay_primary = resume_study_arm_v1(prepared=prepared, arm="primary", ledger=reader)
    replay_withheld = resume_study_arm_v1(prepared=prepared, arm="withheld", ledger=reader)
    assert replay_primary.state == "completed"
    assert replay_withheld.state == "completed"
    assert replay_primary.import_ref == primary.import_ref
    assert replay_withheld.import_ref == withheld.import_ref


def test_resume_without_a_gateway_preserves_invalid_and_unavailable_attempts(tmp_path: Path) -> None:
    prepared = prepare_two_round_study_v1(
        root=tmp_path / "study",
        mode="offline_fixture",
        provider_settings=None,
    )
    ledger = _offline_ledger(prepared)
    invalid_raw = "  { invalid task7 response }\n"
    invalid_gateway = _OneShotStudyGateway({prepared.primary_live_call.sha256: invalid_raw})
    invalid = execute_study_arm_v1(
        prepared=prepared,
        arm="primary",
        ledger=ledger,
        gateway=invalid_gateway,
    )
    assert invalid.state == "incomplete"
    assert invalid.failure_code == "response_schema"
    assert invalid.raw_response_ref is not None
    assert invalid.import_ref is None
    assert invalid.feedback_result is None
    assert invalid_gateway.calls == [prepared.primary_live_call.sha256]
    assert ledger.store.read(invalid.raw_response_ref).decode("utf-8") == invalid_raw

    unavailable_gateway = _UnavailableStudyGateway()
    unavailable = execute_study_arm_v1(
        prepared=prepared,
        arm="withheld",
        ledger=ledger,
        gateway=unavailable_gateway,
    )
    assert unavailable.state == "incomplete"
    assert unavailable.failure_code == "transport"
    assert unavailable.terminal_ref is not None
    # The one-shot ledger persists the exact empty transport response body
    # even when the provider did not report an accepted response.  Preserve
    # that durable zero-byte artifact separately from the typed transport
    # failure rather than treating it as an absent response.
    assert unavailable.raw_response_ref is not None
    assert ledger.store.read(unavailable.raw_response_ref) == b""
    assert unavailable.import_ref is None
    assert unavailable.feedback_result is None
    assert unavailable_gateway.calls == [prepared.withheld_live_call.sha256]

    # Recovery authenticates the two durable failures and never opens a
    # gateway; it must preserve their distinct raw-response/transport states.
    reader = StudyLedgerV1(StudyStoreV1(prepared.store_repository()), prepared.manifest, ledger.grant, approval=None)
    replay_invalid = resume_study_arm_v1(prepared=prepared, arm="primary", ledger=reader)
    replay_unavailable = resume_study_arm_v1(prepared=prepared, arm="withheld", ledger=reader)
    assert replay_invalid.failure_code == invalid.failure_code
    assert replay_invalid.raw_response_ref == invalid.raw_response_ref
    assert replay_unavailable.failure_code == unavailable.failure_code
    assert replay_unavailable.raw_response_ref == unavailable.raw_response_ref
    assert replay_unavailable.raw_response_ref is not None
    assert reader.store.read(replay_unavailable.raw_response_ref) == b""


def test_pending_and_admission_rejection_recover_without_retry_or_provider_call(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pending_prepared = prepare_two_round_study_v1(
        root=tmp_path / "pending-study",
        mode="offline_fixture",
        provider_settings=None,
    )
    pending_ledger = _offline_ledger(pending_prepared)
    pending_gateway = _PendingStudyGateway()
    pending = execute_study_arm_v1(
        prepared=pending_prepared,
        arm="primary",
        ledger=pending_ledger,
        gateway=pending_gateway,
    )
    assert pending.state == "incomplete"
    assert pending.not_started_reason == "pending_accounting"
    assert pending_gateway.calls == [pending_prepared.primary_live_call.sha256]
    blocked_gateway = _OneShotStudyGateway({})
    blocked_other_arm = execute_study_arm_v1(
        prepared=pending_prepared,
        arm="withheld",
        ledger=pending_ledger,
        gateway=blocked_gateway,
    )
    assert blocked_other_arm.state == "incomplete"
    assert blocked_other_arm.not_started_reason == "pending_accounting"
    assert blocked_gateway.calls == []
    pending_reader = StudyLedgerV1(
        StudyStoreV1(pending_prepared.store_repository()),
        pending_prepared.manifest,
        pending_ledger.grant,
        approval=None,
    )
    replay_pending = resume_study_arm_v1(
        prepared=pending_prepared,
        arm="primary",
        ledger=pending_reader,
    )
    assert replay_pending.state == "incomplete"
    assert replay_pending.not_started_reason == "pending_accounting"
    untouched_other_arm = resume_study_arm_v1(
        prepared=pending_prepared,
        arm="withheld",
        ledger=pending_reader,
    )
    assert untouched_other_arm.state == "incomplete"
    assert untouched_other_arm.not_started_reason == "not_reserved"

    rejected_prepared = prepare_two_round_study_v1(
        root=tmp_path / "rejected-study",
        mode="offline_fixture",
        provider_settings=None,
    )
    rejected_ledger = _offline_ledger(rejected_prepared)
    original_claim_dispatch = rejected_ledger.claim_dispatch

    def reject_before_dispatch(**kwargs: object):
        raise StudyAdmissionError("task7 synthetic local admission rejection")

    monkeypatch.setattr(rejected_ledger, "claim_dispatch", reject_before_dispatch)
    rejected_gateway = _OneShotStudyGateway({})
    rejected = execute_study_arm_v1(
        prepared=rejected_prepared,
        arm="primary",
        ledger=rejected_ledger,
        gateway=rejected_gateway,
    )
    assert rejected.state == "rejected"
    assert rejected.admission_rejection_ref is not None
    assert rejected.not_started_reason == "admission_rejected"
    assert rejected_gateway.calls == []
    monkeypatch.setattr(rejected_ledger, "claim_dispatch", original_claim_dispatch)
    rejected_reader = StudyLedgerV1(
        StudyStoreV1(rejected_prepared.store_repository()),
        rejected_prepared.manifest,
        rejected_ledger.grant,
        approval=None,
    )
    replay_rejected = resume_study_arm_v1(
        prepared=rejected_prepared,
        arm="primary",
        ledger=rejected_reader,
    )
    assert replay_rejected.state == "rejected"
    assert replay_rejected.admission_rejection_ref == rejected.admission_rejection_ref


def test_equivalent_selection_and_second_contradiction_keep_runtime_outcomes_typed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import core.pit_optimizer_v5.two_round_study.runtime_ports as runtime_ports_module

    original_workers = runtime_ports_module.study_workers_v1
    opened_workers: list[str] = []

    def count_workers(*, bound, registry):
        opened_workers.append(bound.experiment_id)
        return original_workers(bound=bound, registry=registry)

    prepared = prepare_two_round_study_v1(
        root=tmp_path / "study",
        mode="offline_fixture",
        provider_settings=None,
    )
    monkeypatch.setattr(runtime_ports_module, "study_workers_v1", count_workers)
    ledger = _offline_ledger(prepared)
    primary_request = authenticate_fixture_preflight_v1(prepared.primary_preflight, require_current=True)
    withheld_request = authenticate_fixture_preflight_v1(prepared.withheld_preflight, require_current=True)
    gateway = _OneShotStudyGateway(
        {
            prepared.primary_live_call.sha256: _response_for_request(
                primary_request,
                hypothesis_id="study-exit-second-contradiction-v1",
                claim="The second arm's declared case pattern is intentionally contradicted.",
                # Distinct from both registered inert/always rivals; the
                # intentionally wrong frozen pattern remains a valid draft
                # and yields a typed case contradiction after observation.
                expected_changed=(False, False, True, False),
            ),
            prepared.withheld_live_call.sha256: _response_for_request(
                withheld_request,
                hypothesis_id="study-exit-equivalent-v1",
                claim="The equivalent catalog option is retained as a negative case.",
                configuration_id="S-gte-0.50",
            ),
        }
    )
    primary = execute_study_arm_v1(prepared=prepared, arm="primary", ledger=ledger, gateway=gateway)
    withheld = execute_study_arm_v1(prepared=prepared, arm="withheld", ledger=ledger, gateway=gateway)
    assert primary.state == "completed"
    assert withheld.state == "completed"
    assert primary.feedback_result is not None and primary.feedback_result.status == "completed"
    assert withheld.feedback_result is not None and withheld.feedback_result.status == "completed"
    assert len(gateway.calls) == 2

    for arm in ("primary", "withheld"):
        fixture = reopen_study_fixture_v1(
            root=prepared.fixture_path(arm),
            manifest_ref=prepared.primary_manifest_ref if arm == "primary" else prepared.withheld_manifest_ref,
            registry=prepared.registry,
        )
        records = tuple(
            fixture.repository.load_experiment(reference)
            for reference in fixture.repository.load_checkpoint().record_refs
            if fixture.repository.load_experiment(reference).round_index == 2
        )
        assert len(records) == 1
        if arm == "primary":
            assert records[0].status in {"evaluated", "zero_trade"}
            _fixture, _record, evidence, contrast = _round_two_evidence(prepared, arm, primary)
            case_contrast = evaluate_case_contrast_v1(contrast=contrast, run=evidence.run)
            assert case_contrast.status == "contradicted_on_cases"
            assert case_contrast.mismatched_case_ids
        else:
            assert records[0].status == "behavioral_equivalent"
            assert withheld.contrast_refs

    # The primary second-contradiction candidate is behaviorally distinct and
    # therefore opens exactly one mechanism worker pair.  The equivalent
    # withheld candidate is rejected before supplemental worker allocation.
    assert len(opened_workers) == 1


def test_unavailable_mechanism_worker_reaches_current_critic_and_case_contrast(
    tmp_path: Path,
    monkeypatch,
) -> None:
    original_observe = MechanismRuntimeExtensionV1.observe_candidate

    def disable_worker_factory(self, **kwargs):
        # The precommitment remains real; only the concrete supplemental
        # executor is unavailable when the distinct candidate is observed.
        self.worker_factory = None
        return original_observe(self, **kwargs)

    prepared = prepare_two_round_study_v1(
        root=tmp_path / "study",
        mode="offline_fixture",
        provider_settings=None,
    )
    monkeypatch.setattr(MechanismRuntimeExtensionV1, "observe_candidate", disable_worker_factory)
    ledger = _offline_ledger(prepared)
    request = authenticate_fixture_preflight_v1(prepared.primary_preflight, require_current=True)
    gateway = _OneShotStudyGateway(
        {
            prepared.primary_live_call.sha256: _response_for_request(
                request,
                hypothesis_id="study-exit-worker-unavailable-v1",
                claim="The registered worker is unavailable; the case contrast remains typed.",
            )
        }
    )
    result = execute_study_arm_v1(prepared=prepared, arm="primary", ledger=ledger, gateway=gateway)
    assert result.state == "completed"
    assert result.feedback_result is not None and result.feedback_result.status == "completed"
    fixture, _record, evidence, contrast = _round_two_evidence(prepared, "primary", result)
    assert evidence.run.execution.status == "not_run"
    assert evidence.run.execution.reason == "worker_unavailable"
    assert all(item.availability == "unavailable" for item in evidence.report.predictions)
    assert all(item.unavailable_reason == "not_run" for item in evidence.report.predictions)
    case_contrast = evaluate_case_contrast_v1(contrast=contrast, run=evidence.run)
    assert case_contrast.status == "unavailable"
    assert case_contrast.observed_changed is None
    events = fixture.repository.load_round_events(
        campaign_id=fixture.manifest.manifest.campaign_id,
        round_index=2,
    )
    critic_payload = next(
        fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
        for event in events
        if event.event_kind == "role_completion"
        and isinstance(
            fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind),
            RoleCompletionPayloadV5,
        )
        and fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind).role == "critic"
    )
    critic_package = fixture.repository.load_role_invocation(critic_payload)
    assert critic_package.accepted
    assert isinstance(critic_package.request.role_input, MechanismRoleInputV1)
    assert critic_package.request.role_input.projections
    for projection in critic_package.request.role_input.projections:
        assert projection.execution.status == "not_run"
        assert projection.execution.reason == "worker_unavailable"
        assert all(
            row.prediction.availability == "unavailable"
            and row.prediction.unavailable_reason == "not_run"
            for row in projection.rows
        )
    assert result.reopened_feedback_result is not None
    assert result.reopened_feedback_result.status == "completed"
