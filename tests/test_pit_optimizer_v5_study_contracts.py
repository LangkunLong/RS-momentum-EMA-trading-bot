"""Focused closed-contract and immutable-storage checks for the two-round study."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
import json
from pathlib import Path
import threading

import pytest

from core.pit_optimizer_v5.artifacts import (
    ArtifactDigestMismatchV5,
    ArtifactMissingV5,
    ArtifactRelocatedV5,
    ArtifactSchemaFailureV5,
    LocalArtifactRepositoryV5,
)
from core.pit_optimizer_v5.contracts import (
    HypothesisV5,
    MetricPredictionV5,
    InvestigatorArtifactV5,
    RoleEvidenceItemV5,
    RoleEvidenceV5,
)
from core.pit_optimizer_v5.mechanism_contracts import (
    MechanismDisconfirmingObservationV1,
    MechanismMetricSpecV1,
    MechanismPredicateV1,
    MechanismRecipeV1,
)
from core.pit_optimizer_v5.provider import (
    InvestigatorRoleInputV5,
    RoleBindingV5,
    RoleResponseSchemaFailureV5,
    build_role_request_v5,
    parse_and_bind_role_artifact,
    role_schema_authority_from_manifest_v5,
    wire_role_schema_v5,
)
from core.pit_optimizer_v5.two_round_study.contracts import (
    ExperimentDraftV1,
    RivalPatternV1,
    StudyAdmissionError,
    StudyAuthorityError,
    StudyContractError,
    StudyProviderSettingsV1,
    StudyResponseV1,
    StudyVerdictsV1,
)
from core.pit_optimizer_v5.two_round_study.schema import (
    parse_study_response_v1,
    study_response_schema_v1,
)
from core.pit_optimizer_v5.two_round_study.store import StudyStoreV1


def _hypothesis() -> HypothesisV5:
    return HypothesisV5(
        hypothesis_id="hyp.exit.atr",
        rank=1,
        primary_mechanism="exit",
        causal_claim="ATR-aware exit behavior changes exit decisions.",
        predicted_changes=(
            MetricPredictionV5(
                metric_id="exit.decision_changed_count",
                direction="increase",
                rationale="Applicable cases should change.",
            ),
        ),
        evidence_ids=("v5.fixture.evidence",),
        author_instructions="Use the registered exit mechanism recipe.",
    )


def _draft(*, claim_kind: str = "threshold", hypothesis_id: str = "hyp.exit.atr") -> ExperimentDraftV1:
    metrics = (
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
    return ExperimentDraftV1(
        hypothesis_id=hypothesis_id,
        cited_evidence_ids=("v5.fixture.evidence",),
        applicability=MechanismPredicateV1("features.atr_20_fraction", "is_present", None),
        recipe=MechanismRecipeV1(
            recipe_id="evaluate_exit_atr20_fraction_v1",
            input_field="features.atr_20_fraction",
            input_values=(Decimal("0.20"), Decimal("0.50"), Decimal("0.80"), None),
        ),
        metrics=metrics,
        disconfirming_observations=(
            MechanismDisconfirmingObservationV1(
                observation_id="protected_control_changed",
                metric_id="exit.protected_control_unchanged_count",
            ),
        ),
        expected_changed=(False, True, True, False),
        rivals=(
            RivalPatternV1("inert", (False, False, False, False)),
            RivalPatternV1("always_on", (True, True, True, True)),
        ),
        configuration_id="threshold-050",
        claim_kind=claim_kind,
    )


def test_study_store_is_create_only(tmp_path: Path) -> None:
    repository = LocalArtifactRepositoryV5(tmp_path)
    store = StudyStoreV1(repository)
    reference = store.put(kind="test", key="one", content=b"first")
    assert store.read(reference) == b"first"
    assert store.put(kind="test", key="one", content=b"first") == reference
    with pytest.raises(StudyAuthorityError):
        store.put(kind="test", key="one", content=b"changed")


def test_binary_blob_enumeration_is_bounded_sorted_and_does_not_repair(tmp_path: Path) -> None:
    repository = LocalArtifactRepositoryV5(tmp_path)
    assert repository.list_binary_state_refs(
        namespace="missing-study", maximum_entries=4, maximum_bytes=64
    ) == ()
    assert not (tmp_path / "adapter-blobs" / "missing-study").exists()
    repository.append_binary_state(namespace="study", key="z", content=b"zz")
    repository.append_binary_state(namespace="study", key="a", content=b"a")
    refs = repository.list_binary_state_refs(namespace="study", maximum_entries=4, maximum_bytes=64)
    assert tuple(item.relative_path for item in refs) == (
        "adapter-blobs/study/a.bin",
        "adapter-blobs/study/z.bin",
    )
    with pytest.raises(ArtifactSchemaFailureV5):
        repository.list_binary_state_refs(namespace="study", maximum_entries=1, maximum_bytes=64)
    with pytest.raises(ArtifactSchemaFailureV5):
        repository.list_binary_state_refs(namespace="study", maximum_entries=4, maximum_bytes=2)


def test_binary_blob_enumeration_rejects_unexpected_entries(tmp_path: Path) -> None:
    repository = LocalArtifactRepositoryV5(tmp_path)
    repository.append_binary_state(namespace="study", key="one", content=b"one")
    unknown = tmp_path / "adapter-blobs" / "study" / "unexpected.txt"
    unknown.write_bytes(b"unexpected")
    with pytest.raises(ArtifactSchemaFailureV5):
        repository.list_binary_state_refs(namespace="study", maximum_entries=4, maximum_bytes=64)


def test_binary_blob_enumeration_rejects_links(tmp_path: Path) -> None:
    repository = LocalArtifactRepositoryV5(tmp_path)
    repository.append_binary_state(namespace="study", key="one", content=b"one")
    link = tmp_path / "adapter-blobs" / "study" / "link.bin"
    try:
        link.symlink_to(tmp_path / "adapter-blobs" / "study" / "one.bin")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable in this test environment: {exc}")
    with pytest.raises(ArtifactRelocatedV5):
        repository.list_binary_state_refs(namespace="study", maximum_entries=4, maximum_bytes=64)


def test_binary_blob_enumeration_rejects_missing_listed_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = LocalArtifactRepositoryV5(tmp_path)
    repository.append_binary_state(namespace="study", key="one", content=b"one")
    original_read = LocalArtifactRepositoryV5._read_relative

    def delete_before_read(self, relative_path: str) -> bytes:
        (tmp_path / "adapter-blobs" / "study" / "one.bin").unlink()
        return original_read(self, relative_path)

    monkeypatch.setattr(LocalArtifactRepositoryV5, "_read_relative", delete_before_read)
    with pytest.raises(ArtifactMissingV5):
        repository.list_binary_state_refs(namespace="study", maximum_entries=4, maximum_bytes=64)


def test_study_contracts_round_trip_and_claim_axes_are_distinct() -> None:
    threshold = _draft(claim_kind="threshold")
    general = replace(threshold, claim_kind="general")
    assert threshold.canonical_bytes() != general.canonical_bytes()
    assert ExperimentDraftV1.from_canonical_json(threshold.canonical_bytes()) == threshold


def test_study_contracts_reject_arbitrary_dataclass() -> None:
    @dataclass(frozen=True)
    class Foreign:
        value: str

    with pytest.raises(StudyContractError):
        StudyStoreV1(LocalArtifactRepositoryV5(Path.cwd())).put_contract(
            kind="foreign", key="one", value=Foreign("code")
        )


def test_strict_study_decoder_rejects_unknown_code_path_and_metric_fields() -> None:
    primitive = _draft().to_primitive()
    primitive["unknown_code"] = "raise SystemExit"
    with pytest.raises((StudyContractError, ValueError)):
        ExperimentDraftV1.from_primitive(primitive)

    primitive = _draft().to_primitive()
    primitive["recipe"]["input_values"][0] = "0.20"
    with pytest.raises(StudyContractError):
        ExperimentDraftV1.from_primitive(primitive)

    primitive = _draft().to_primitive()
    primitive["metrics"][0]["path"] = "core/secret.py"
    with pytest.raises((StudyContractError, ValueError)):
        ExperimentDraftV1.from_primitive(primitive)

    primitive = _draft().to_primitive()
    primitive["metrics"][0]["metric_id"] = "arbitrary.metric"
    with pytest.raises((StudyContractError, ValueError)):
        ExperimentDraftV1.from_primitive(primitive)


def test_legacy_role_schema_rejects_study_envelope(tmp_path: Path) -> None:
    request = _fixture_request(tmp_path)
    response = StudyResponseV1(1, request.expected_binding, InvestigatorArtifactV5((_hypothesis(),)), (_draft(),))
    with pytest.raises(RoleResponseSchemaFailureV5):
        parse_and_bind_role_artifact(request=request, response_text=response.canonical_bytes().decode("utf-8"))


def _fixture_request(tmp_path: Path):
    from tests.test_pit_optimizer_v5_mechanism_artifacts import _authenticated_fixture, _source_bundle
    from core.pit_optimizer_v5.candidate_ir import derive_policy_revision_identity_v5

    repository = LocalArtifactRepositoryV5(tmp_path)
    source = _source_bundle()
    revision = derive_policy_revision_identity_v5(
        source_bundle=source,
        trusted_policy_runtime_sha256="1" * 64,
        immutable_constraints_sha256="2" * 64,
    )
    manifest = _authenticated_fixture(repository=repository, parent_bundle=source, parent_revision=revision)
    return build_role_request_v5(
        role="investigator",
        role_input=InvestigatorRoleInputV5(("v5.fixture.evidence",), (), ()),
        issued_evidence=RoleEvidenceV5(
            5,
            (RoleEvidenceItemV5("v5.fixture.evidence", "evaluator.exit_attribution_count", 1),),
        ),
        expected_binding=RoleBindingV5(
            manifest.baseline_policy_revision.sha256,
            None,
            (),
            manifest.panel_plan.discovery_plan_sha256,
        ),
        schema_authority=role_schema_authority_from_manifest_v5(
            role="investigator", manifest=manifest.manifest
        ),
        max_output_tokens=4096,
    )


def test_study_parser_projects_ordinary_artifact_without_mutating_legacy_schema(tmp_path: Path) -> None:
    request = _fixture_request(tmp_path)
    legacy_schema = request.schema_authority.canonical_schema_json
    response = StudyResponseV1(
        1,
        request.expected_binding,
        InvestigatorArtifactV5((_hypothesis(),)),
        (_draft(),),
    )
    parsed = parse_study_response_v1(
        raw=response.canonical_bytes(),
        fixture_request=request,
        configuration_ids=("threshold-050",),
    )
    assert parsed == response
    parsed_with_provider_whitespace = parse_study_response_v1(
        raw=json.dumps(response.to_primitive(), indent=2).encode("utf-8"),
        fixture_request=request,
        configuration_ids=("threshold-050",),
    )
    assert parsed_with_provider_whitespace == response
    assert request.schema_authority.canonical_schema_json == legacy_schema
    with pytest.raises(RoleResponseSchemaFailureV5):
        parse_and_bind_role_artifact(request=request, response_text=response.canonical_bytes().decode("utf-8"))
    unsupported = replace(_hypothesis(), authoring_mode="full_source_escape")
    unsupported_response = StudyResponseV1(
        1,
        request.expected_binding,
        InvestigatorArtifactV5((unsupported,)),
        (_draft(),),
    )
    with pytest.raises(StudyContractError):
        parse_study_response_v1(
            raw=unsupported_response.canonical_bytes(),
            fixture_request=request,
            configuration_ids=("threshold-050",),
        )


def test_study_schema_is_projectable_and_static_constraints_are_closed(tmp_path: Path) -> None:
    request = _fixture_request(tmp_path)
    schema = json.loads(study_response_schema_v1(fixture_request=request, configuration_ids=("threshold-050",)))
    wire_role_schema_v5(schema, allow_full_source_escape=False)
    ordinary_schema = json.loads(request.schema_authority.canonical_schema_json.decode("utf-8"))
    assert schema["properties"]["binding"] == ordinary_schema["properties"]["binding"]
    assert "x-pit-optimizer-v5-study-authority" not in schema
    assert schema["properties"]["schema_version"] == {"enum": [1], "type": "integer"}
    draft = schema["properties"]["drafts"]["items"]
    assert draft["properties"]["cited_evidence_ids"]["uniqueItems"] is True
    assert draft["properties"]["metrics"]["uniqueItems"] is True
    assert draft["properties"]["disconfirming_observations"]["uniqueItems"] is True
    assert draft["properties"]["rivals"]["uniqueItems"] is True
    assert draft["properties"]["recipe"]["properties"]["input_values"]["uniqueItems"] is True
    decimal_pattern = draft["properties"]["metrics"]["items"]["properties"]["tolerance"]["pattern"]
    assert decimal_pattern == (
        r"^(?!-0(?:\.0*)?$)-?(?=(?:[^0-9]*[0-9]){1,64}[^0-9]*$)"
        r"(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?$"
    )


def test_study_parser_rejects_wrong_binding_citation_and_draft_cardinality(tmp_path: Path) -> None:
    request = _fixture_request(tmp_path)
    response = StudyResponseV1(1, request.expected_binding, InvestigatorArtifactV5((_hypothesis(),)), (_draft(),))
    wrong_binding = replace(
        response,
        binding=replace(response.binding, parent_revision_sha256="0" * 64),
    )
    with pytest.raises(StudyAuthorityError):
        parse_study_response_v1(
            raw=wrong_binding.canonical_bytes(), fixture_request=request, configuration_ids=("threshold-050",)
        )

    wrong_citation = replace(response, drafts=(replace(_draft(), cited_evidence_ids=("v5.not-issued",)),))
    with pytest.raises(StudyAuthorityError):
        parse_study_response_v1(
            raw=wrong_citation.canonical_bytes(), fixture_request=request, configuration_ids=("threshold-050",)
        )

    duplicate = response.to_primitive()
    duplicate["drafts"] = [duplicate["drafts"][0], duplicate["drafts"][0]]
    with pytest.raises(StudyContractError):
        parse_study_response_v1(
            raw=json.dumps(duplicate).encode("utf-8"), fixture_request=request, configuration_ids=("threshold-050",)
        )
    missing = response.to_primitive()
    missing["drafts"] = []
    with pytest.raises(StudyContractError):
        parse_study_response_v1(
            raw=json.dumps(missing).encode("utf-8"), fixture_request=request, configuration_ids=("threshold-050",)
        )


def test_study_store_translates_corruption_with_exception_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = LocalArtifactRepositoryV5(tmp_path)
    store = StudyStoreV1(repository)
    reference = store.put(kind="test", key="one", content=b"first")
    (tmp_path / "adapter-blobs" / "study-v1-test" / "one.bin").write_bytes(b"corrupt")
    with pytest.raises(StudyAuthorityError) as captured:
        store.read(reference)
    assert isinstance(captured.value.__cause__, ArtifactDigestMismatchV5)

    def raise_missing(self, *, namespace: str, maximum_entries: int, maximum_bytes: int):
        raise ArtifactMissingV5()

    monkeypatch.setattr(LocalArtifactRepositoryV5, "list_binary_state_refs", raise_missing)
    with pytest.raises(StudyAuthorityError) as captured:
        store.put(kind="test", key="two", content=b"second")
    assert isinstance(captured.value.__cause__, ArtifactMissingV5)


def test_study_store_namespace_quota_is_atomic_under_concurrent_puts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import core.pit_optimizer_v5.two_round_study.store as store_module

    monkeypatch.setattr(store_module, "STUDY_MAX_NAMESPACE_BYTES_V1", 5)
    repository = LocalArtifactRepositoryV5(tmp_path)
    store = StudyStoreV1(repository)
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()
    original_append = LocalArtifactRepositoryV5.append_binary_state

    def blocking_append(self, *, namespace: str, key: str, content: bytes):
        nonlocal calls
        with calls_lock:
            calls += 1
            first = calls == 1
        if first:
            entered.set()
            assert release.wait(timeout=5)
        return original_append(self, namespace=namespace, key=key, content=content)

    monkeypatch.setattr(LocalArtifactRepositoryV5, "append_binary_state", blocking_append)
    results: list[object] = []

    def put(key: str) -> None:
        try:
            results.append(store.put(kind="race", key=key, content=b"123"))
        except BaseException as exc:  # noqa: BLE001 - capture thread result for assertion
            results.append(exc)

    first = threading.Thread(target=put, args=("one",))
    second = threading.Thread(target=put, args=("two",))
    first.start()
    assert entered.wait(timeout=5)
    second.start()
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)
    assert not first.is_alive() and not second.is_alive()
    assert sum(isinstance(item, ArtifactSchemaFailureV5) for item in results) == 0
    assert sum(isinstance(item, StudyAdmissionError) for item in results) == 1
    assert sum(isinstance(item, Exception) for item in results) == 1


def test_study_verdict_axes_and_v1_version_are_independent() -> None:
    supported = StudyVerdictsV1(
        schema_version=1,
        trace_integrity="verified",
        evidence_delivery="verified",
        evidence_use="not_assessed",
        production_assessment="supported_on_cases",
        case_contrast="matched_on_cases",
        experiment_completion="verified",
        feedback_attribution="not_assessed",
        optimization_improvement="not_established",
        authored_code_execution="not_established",
        reasons=(),
    )
    assert supported.production_assessment == "supported_on_cases"
    not_used = replace(supported, evidence_use="not_supported")
    assert not_used.production_assessment == "supported_on_cases"
    with pytest.raises(StudyContractError):
        replace(supported, optimization_improvement="supported")
    with pytest.raises(StudyContractError):
        replace(supported, schema_version=2)
    with pytest.raises(StudyContractError):
        StudyVerdictsV1.from_canonical_json(
            replace(supported, schema_version=1).canonical_bytes().replace(b'"schema_version":1', b'"schema_version":2')
        )


def test_provider_settings_accept_provider_qualified_model_identity() -> None:
    settings = StudyProviderSettingsV1(
        provider="openrouter",
        model="deepseek/deepseek-r1",
        max_output_tokens=128,
    )
    assert StudyProviderSettingsV1.from_canonical_json(settings.canonical_bytes()) == settings
