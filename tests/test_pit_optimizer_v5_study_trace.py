"""Read-only verification and exact-byte export checks for Task 8."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import time
import uuid

import pytest

from core.pit_optimizer_v5.artifacts import ArtifactRefV5
from core.pit_optimizer_v5.artifacts import LocalArtifactRepositoryV5
from core.pit_optimizer_v5.two_round_study.contracts import StudyAdmissionError, StudyAuthorityError
from core.pit_optimizer_v5.two_round_study.driver import (
    _long_exists,
    _long_files,
    _long_read_bytes,
    execute_study_arm_v1,
    prepare_two_round_study_v1,
)
from core.pit_optimizer_v5.two_round_study.__main__ import _offline_ledger
from core.pit_optimizer_v5.provider import ProviderFailureDiagnosticV5, ProviderResponseAccountingErrorV5
from core.pit_optimizer_v5.two_round_study.store import StudyStoreV1
from core.pit_optimizer_v5.two_round_study.trace import _trace_markdown, export_study_trace_v1
from core.pit_optimizer_v5.two_round_study.verification import (
    HumanEvidenceReviewV1,
    load_prepared_study_v1,
    verify_study_v1,
)


_TEST_ROOT = Path(__file__).resolve().parents[1] / ".superpowers" / "sdd" / "2026-09-19-v5-two-round-example" / "task-8-test-runs"


def _new_test_root(label: str) -> Path:
    root = _TEST_ROOT / f"{label}-{uuid.uuid4().hex}"
    root.mkdir(parents=True)
    return root


def _inventory(root: Path) -> tuple[tuple[str, str], ...]:
    return tuple((relative, hashlib.sha256(_long_read_bytes(path)).hexdigest()) for relative, path in _long_files(root))


@pytest.fixture(scope="module")
def prepared_study():
    root = _new_test_root("trace") / "study"
    return prepare_two_round_study_v1(root=root, mode="offline_fixture", provider_settings=None)


@pytest.fixture(scope="module")
def persisted_phase_studies():
    """Build genuine persisted reservation and import-before-journal phases."""

    reservation = prepare_two_round_study_v1(
        root=_new_test_root("phase-reservation") / "study",
        mode="offline_fixture",
        provider_settings=None,
    )
    import_before_journal = prepare_two_round_study_v1(
        root=_new_test_root("phase-import") / "study",
        mode="offline_fixture",
        provider_settings=None,
    )
    return reservation, import_before_journal


@pytest.fixture
def owned_root() -> Path:
    return _new_test_root("trace-test")


def test_loader_and_verifier_are_read_only_and_keep_arm_authority_separate(prepared_study) -> None:
    before = tuple(
        (name, _inventory(root))
        for name, root in (
            ("ancestor", prepared_study.ancestor_root),
            ("store", prepared_study.store_root),
            ("primary", prepared_study.primary_root),
            ("withheld", prepared_study.withheld_root),
        )
    )
    reloaded = load_prepared_study_v1(root=prepared_study.root)
    store = StudyStoreV1(reloaded.store_repository())
    verification = verify_study_v1(prepared=reloaded, store=store, ledger=None)
    after = tuple(
        (name, _inventory(root))
        for name, root in (
            ("ancestor", prepared_study.ancestor_root),
            ("store", prepared_study.store_root),
            ("primary", prepared_study.primary_root),
            ("withheld", prepared_study.withheld_root),
        )
    )

    assert before == after
    assert reloaded.manifest_ref == prepared_study.manifest_ref
    assert verification.verdicts.evidence_use == "not_assessed"
    assert verification.verdicts.optimization_improvement == "not_established"
    assert verification.verdicts.authored_code_execution == "not_established"
    assert tuple(item.arm for item in verification.arms) == ("primary", "withheld")
    assert verification.arms[0].repository_root_identity_sha256
    assert verification.arms[0].repository_root_identity_sha256 != verification.arms[1].repository_root_identity_sha256


def test_export_preserves_original_manifest_bytes_and_required_bundle_paths(prepared_study, owned_root: Path) -> None:
    reloaded = load_prepared_study_v1(root=prepared_study.root)
    store = StudyStoreV1(reloaded.store_repository())
    verification = verify_study_v1(prepared=reloaded, store=store, ledger=None)
    before = _inventory(prepared_study.root)
    output = owned_root / "trace"
    index_ref = export_study_trace_v1(prepared=reloaded, verification=verification, store=store, output=output)

    assert index_ref.relative_path == "artifact-index.json"
    assert (output / index_ref.relative_path).is_file()
    for relative in (
        "study-manifest.json",
        "artifact-index.json",
        "request-comparison.json",
        "rubric-results.json",
        "trace.md",
    ):
        assert (output / relative).is_file(), relative
    for relative in ("live-study-calls", "imports", "behavior-registry", "round-1", "round-2-primary", "round-2-withheld", "case-contrasts"):
        assert (output / relative).is_dir(), relative

    original_manifest = store.read(prepared_study.manifest_ref)
    exported_manifest = (output / "study-manifest.json").read_bytes()
    assert exported_manifest == original_manifest
    index = json.loads((output / "artifact-index.json").read_text(encoding="utf-8"))
    manifest_entries = [item for item in index["artifacts"] if item["relative_path"] == "study-manifest.json"]
    assert len(manifest_entries) == 1
    assert manifest_entries[0]["source_relative_path"] == prepared_study.manifest_ref.relative_path
    assert manifest_entries[0]["source_sha256"] == prepared_study.manifest_ref.sha256
    assert manifest_entries[0]["sha256"] == hashlib.sha256(original_manifest).hexdigest()
    assert index["provenance_semantics"] == {
        "reference_encoding": "authority|relative_path|sha256",
        "original_artifact_upstream_refs": {
            "relationship": "authenticated_content_match_alias",
            "match_key": ["relative_path", "sha256"],
            "establishes_causal_dependency": False,
            "establishes_owning_authority": False,
            "owning_authority_fields": ["authority", "source_relative_path", "source_sha256"],
        },
        "derivative_artifact_upstream_refs": {
            "relationship": "authenticated_input_reference",
        },
    }


    original_entries = [item for item in index["artifacts"] if item["authority"] != "export-derivative"]
    primary_authority = next(
        item["authority"]
        for item in original_entries
        if str(item["authority"]).startswith("round-two-primary:")
    )
    precommitment_candidates = []
    for item in original_entries:
        source_path = str(item["source_relative_path"])
        if (
            item["authority"] != primary_authority
            or not source_path.startswith("adapter-state/mechanism-v5/")
            or not source_path.endswith(".json")
        ):
            continue
        raw = _long_read_bytes(output / Path(*str(item["relative_path"]).split("/")))
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (
            isinstance(value, dict)
            and value.get("round_index") == 1
            and "spec_ref" in value
            and "corpus_ref" in value
        ):
            precommitment_candidates.append((item, value))
    assert len(precommitment_candidates) == 1
    primary_precommitment, precommitment_value = precommitment_candidates[0]
    inherited_leaf_refs = tuple(precommitment_value[key] for key in ("spec_ref", "corpus_ref"))
    assert precommitment_value["round_index"] == 1
    for leaf_ref in inherited_leaf_refs:
        assert set(leaf_ref) == {"relative_path", "sha256"}
        primary_leaf = next(
            item
            for item in original_entries
            if item["authority"] == primary_authority
            and item["source_relative_path"] == leaf_ref["relative_path"]
            and item["source_sha256"] == leaf_ref["sha256"]
        )
        expected_own_arm_edge = (
            f"{primary_precommitment['authority']}|{leaf_ref['relative_path']}|{leaf_ref['sha256']}"
        )
        assert expected_own_arm_edge in primary_precommitment["upstream_refs"]
        assert primary_leaf["upstream_refs"] == []
        withheld_leaf = next(
            item
            for item in original_entries
            if str(item["authority"]).startswith("round-two-withheld:")
            and item["source_relative_path"] == leaf_ref["relative_path"]
            and item["source_sha256"] == leaf_ref["sha256"]
        )
        expected_cross_root_alias = (
            f"{withheld_leaf['authority']}|{leaf_ref['relative_path']}|{leaf_ref['sha256']}"
        )
        assert expected_cross_root_alias in primary_precommitment["upstream_refs"]
    derivative_entries = [item for item in index["artifacts"] if item["authority"] == "export-derivative"]
    assert {item["relative_path"] for item in derivative_entries} == {"rubric-results.json", "trace.md"}
    assert all(item["upstream_refs"] for item in derivative_entries)
    assert any(
        ref.startswith(f"{primary_authority}|")
        for item in derivative_entries
        for ref in item["upstream_refs"]
    )
    assert original_entries
    source_paths = {str(item["source_relative_path"]) for item in original_entries}
    assert any(Path(path).name == "checkpoint.json" for path in source_paths)
    assert any(Path(path).name == "archive.json" for path in source_paths)
    assert any("events" in Path(path).parts for path in source_paths)
    assert any(item["upstream_refs"] for item in original_entries)
    for item in original_entries:
        exported = output / Path(*item["relative_path"].split("/"))
        assert _long_exists(exported), item["relative_path"]
        assert item["sha256"] == item["source_sha256"]
        assert hashlib.sha256(_long_read_bytes(exported)).hexdigest() == item["source_sha256"]
    assert _inventory(prepared_study.root) == before
    trace = (output / "trace.md").read_text(encoding="utf-8")
    for marker in (
        "### Export provenance semantics",
        "## Side-by-side generated role proposals",
        "## P0→A and P1→B proposals",
        "## Raw mechanism case/control observations",
        "## F/L request authorities and caps",
        "## Admission, equivalence, failures, usage and recovery",
        "pending_prospective_totals",
        "optimization improvement",
        "authored-code execution",
        "offline synthetic fixture replay",
    ):
        assert marker in trace, marker
    assert (
        f"artifact-index entries (including `trace.md`; excluding self-referential `artifact-index.json`): **{len(index['artifacts'])}**"
        in trace
    )
    def completed_live_arm(arm):
        return replace(
            arm,
            state="completed",
            terminal_ref=ArtifactRefV5(f"terminals/{arm.arm}", "a" * 64),
            import_ref=ArtifactRefV5(f"imports/{arm.arm}", "b" * 64),
            usage={
                **arm.usage,
                "terminal_usage": {"input_tokens": 1, "output_tokens": 1},
                "terminal_failure_code": None,
            },
        )

    completed_primary, completed_withheld = tuple(
        completed_live_arm(arm) for arm in verification.arms
    )
    completed_verification = replace(
        verification,
        arms=(completed_primary, completed_withheld),
    )
    completed_live_trace = _trace_markdown(
        replace(reloaded, mode="live_study"),
        completed_verification,
        [],
        store=None,
    ).decode("utf-8")
    assert "live-generated responses and admitted provider accounting" in completed_live_trace
    assert "offline synthetic responses and fake-transport accounting" not in completed_live_trace

    pending_withheld = replace(
        completed_withheld,
        state="incomplete",
        terminal_ref=None,
        import_ref=None,
    )
    mixed_verification = replace(
        verification,
        arms=(completed_primary, pending_withheld),
    )
    mixed_live_trace = _trace_markdown(
        replace(reloaded, mode="live_study"),
        mixed_verification,
        [],
        store=None,
    ).decode("utf-8")
    assert "live-study evidence/accounting with at least one arm not fully completed" in mixed_live_trace
    assert "Completion, retained responses, and settled accounting are reported per arm" in mixed_live_trace
    assert "Retained response bytes remain evidence even when accounting is pending" in mixed_live_trace
    assert "arm state alone does not establish provider handoff or billing" in mixed_live_trace
    assert "live-generated responses and admitted provider accounting" not in mixed_live_trace

    settled_failure_primary = replace(
        completed_primary,
        state="incomplete",
        import_ref=None,
        usage={
            **completed_primary.usage,
            "terminal_failure_code": "response_accounting_failed",
            "terminal_usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )
    settled_failure_trace = _trace_markdown(
        replace(reloaded, mode="live_study"),
        replace(verification, arms=(settled_failure_primary, completed_withheld)),
        [],
        store=None,
    ).decode("utf-8")
    assert "A settled failure can retain authoritative usage without a successful or imported output" in settled_failure_trace
    assert "Pending prospective holds are not actual cost" in settled_failure_trace


def test_pending_provider_observation_survives_readonly_verification_and_exact_export(owned_root: Path) -> None:
    prepared = prepare_two_round_study_v1(
        root=owned_root / "pending-observation" / "study",
        mode="offline_fixture",
        provider_settings=None,
    )
    ledger = _offline_ledger(prepared)
    observed_text = '{"observed":"SDK model content"}'

    class IncompleteUsageGateway:
        def invoke_json_once(self, **_kwargs):
            raise ProviderResponseAccountingErrorV5(
                response_text=observed_text,
                provider_request_id="safe-observed-id",
                phase="response_accounting",
                code="inline_usage_missing",
                cleanup_diagnostic=("client_cleanup", "client_cleanup_failed"),
            )

    primary_result = execute_study_arm_v1(
        prepared=prepared,
        arm="primary",
        ledger=ledger,
        gateway=IncompleteUsageGateway(),
    )
    assert primary_result.state == "incomplete"

    reloaded = load_prepared_study_v1(root=prepared.root)
    store = StudyStoreV1(reloaded.store_repository())
    readonly_ledger = type(ledger)(store, reloaded.manifest, ledger.grant, approval=None)
    before_verify = _inventory(reloaded.root)
    verification = verify_study_v1(prepared=reloaded, store=store, ledger=readonly_ledger)
    assert _inventory(reloaded.root) == before_verify

    primary_key = prepared.live_call_for("primary").sha256
    observation_ref = store.list_refs(kind="response-observations")[0]
    observation_payload = json.loads(store.read(observation_ref))
    assert observation_payload["cleanup_diagnostic"] == {
        "phase": "client_cleanup",
        "code": "client_cleanup_failed",
    }
    observed_raw_ref = store.list_refs(kind="observed-raw-responses")[0]
    assert store.list_refs(kind="provider-diagnostics") == ()
    expected = {
        f"live-study-calls/shared/response-observations/{primary_key}.bin": store.read(
            observation_ref
        ),
        f"live-study-calls/shared/observed-raw-responses/{primary_key}.bin": observed_text.encode("utf-8"),
    }
    assert observed_raw_ref.relative_path.endswith(f"/{primary_key}.bin")
    output = owned_root / "pending-observation-export"
    export_study_trace_v1(prepared=reloaded, verification=verification, store=store, output=output)
    assert _inventory(reloaded.root) == before_verify
    for relative, source_bytes in expected.items():
        exported = _long_read_bytes(output / Path(*relative.split("/")))
        assert exported == source_bytes


def test_pending_provider_diagnostic_survives_readonly_verification_and_exact_export(owned_root: Path) -> None:
    prepared = prepare_two_round_study_v1(
        root=owned_root / "pending-diagnostic" / "study",
        mode="offline_fixture",
        provider_settings=None,
    )
    ledger = _offline_ledger(prepared)

    class TimeoutGateway:
        def invoke_json_once(self, **_kwargs):
            raise ProviderFailureDiagnosticV5(
                phase="response_extraction",
                code="response_content_unavailable",
                provider_request_id="safe-response-id",
                cleanup_diagnostic=("client_cleanup", "client_cleanup_failed"),
                content_failure="content_non_string",
                accounting_failure="inline_usage_missing",
            )

    result = execute_study_arm_v1(prepared=prepared, arm="primary", ledger=ledger, gateway=TimeoutGateway())
    assert result.state == "incomplete"

    reloaded = load_prepared_study_v1(root=prepared.root)
    store = StudyStoreV1(reloaded.store_repository())
    readonly_ledger = type(ledger)(store, reloaded.manifest, ledger.grant, approval=None)
    before_verify = _inventory(reloaded.root)
    verification = verify_study_v1(prepared=reloaded, store=store, ledger=readonly_ledger)
    assert _inventory(reloaded.root) == before_verify

    request_key = prepared.live_call_for("primary").sha256
    diagnostic_ref = store.list_refs(kind="provider-diagnostics")[0]
    diagnostic = store.read(diagnostic_ref)
    diagnostic_payload = json.loads(diagnostic)
    assert diagnostic_payload["cleanup_diagnostic"] == {
        "phase": "client_cleanup",
        "code": "client_cleanup_failed",
    }
    assert diagnostic_payload["content_failure"] == "content_non_string"
    assert diagnostic_payload["accounting_failure"] == "inline_usage_missing"
    output = owned_root / "pending-diagnostic-export"
    export_study_trace_v1(prepared=reloaded, verification=verification, store=store, output=output)
    exported_diagnostic = output / "live-study-calls" / "shared" / "provider-diagnostics" / f"{request_key}.bin"
    assert _long_read_bytes(exported_diagnostic) == diagnostic
    assert _inventory(reloaded.root) == before_verify


def test_interrupted_observation_envelope_reopens_verifies_and_exports_as_pending(
    owned_root: Path, monkeypatch
) -> None:
    prepared = prepare_two_round_study_v1(
        root=owned_root / "pending-prefix" / "study",
        mode="offline_fixture",
        provider_settings=None,
    )
    ledger = _offline_ledger(prepared)
    observed_text = '{"observed":"exact returned text"}'

    class IncompleteUsageGateway:
        def invoke_json_once(self, **_kwargs):
            raise ProviderResponseAccountingErrorV5(
                response_text=observed_text,
                provider_request_id="safe-prefix-id",
                phase="response_accounting",
                code="inline_usage_missing",
            )

    original_put = StudyStoreV1.put

    def interrupt_raw_write(self, *, kind, key, content):
        if kind == "observed-raw-responses":
            raise StudyAdmissionError("simulated interrupted raw write")
        return original_put(self, kind=kind, key=key, content=content)

    monkeypatch.setattr(StudyStoreV1, "put", interrupt_raw_write)
    result = execute_study_arm_v1(
        prepared=prepared,
        arm="primary",
        ledger=ledger,
        gateway=IncompleteUsageGateway(),
    )
    assert result.state == "incomplete"
    monkeypatch.setattr(StudyStoreV1, "put", original_put)

    reloaded = load_prepared_study_v1(root=prepared.root)
    store = StudyStoreV1(reloaded.store_repository())
    readonly_ledger = type(ledger)(store, reloaded.manifest, ledger.grant, approval=None)
    before_verify = _inventory(reloaded.root)
    verification = verify_study_v1(prepared=reloaded, store=store, ledger=readonly_ledger)
    assert _inventory(reloaded.root) == before_verify

    request_key = prepared.live_call_for("primary").sha256
    observation_ref = store.list_refs(kind="response-observations")[0]
    assert store.list_refs(kind="observed-raw-responses") == ()
    output = owned_root / "pending-prefix-export"
    export_study_trace_v1(prepared=reloaded, verification=verification, store=store, output=output)
    exported_envelope = output / "live-study-calls" / "shared" / "response-observations" / f"{request_key}.bin"
    assert _long_read_bytes(exported_envelope) == store.read(observation_ref)
    assert not (output / "live-study-calls" / "shared" / "observed-raw-responses" / f"{request_key}.bin").exists()
    assert _inventory(reloaded.root) == before_verify


def test_export_rejects_nested_destination_and_unbound_verification_before_writing(prepared_study, owned_root: Path, monkeypatch) -> None:
    reloaded = load_prepared_study_v1(root=prepared_study.root)
    store = StudyStoreV1(reloaded.store_repository())
    verification = verify_study_v1(prepared=reloaded, store=store, ledger=None)

    nested = prepared_study.root / "attempted-export"
    with pytest.raises(StudyAuthorityError, match="outside the original"):
        export_study_trace_v1(prepared=reloaded, verification=verification, store=store, output=nested)
    assert not nested.exists()

    outside = owned_root / "outside"
    outside.mkdir()
    reparse = owned_root / "reparse-parent"
    try:
        reparse.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        # CI hosts may deny symlink creation.  Exercise the same path guard
        # with an explicitly marked synthetic reparse component so the test
        # still proves rejection occurs before any output write.
        del exc
        reparse.mkdir()
        from core.pit_optimizer_v5.two_round_study import trace as trace_module

        original_reparse_check = trace_module._is_reparse_component
        monkeypatch.setattr(
            trace_module,
            "_is_reparse_component",
            lambda path: path == reparse or original_reparse_check(path),
        )
    reparse_output = reparse / "export"
    with pytest.raises(StudyAuthorityError, match="symlink or reparse"):
        export_study_trace_v1(prepared=reloaded, verification=verification, store=store, output=reparse_output)
    assert not reparse_output.exists()

    unbound = replace(verification, manifest_ref=ArtifactRefV5("study-manifest.json", "0" * 64))
    output = owned_root / "unbound"
    with pytest.raises(StudyAuthorityError, match="another study manifest"):
        export_study_trace_v1(prepared=reloaded, verification=unbound, store=store, output=output)
    assert not output.exists()


def test_export_rejects_same_manifest_forged_verification_before_writing(prepared_study, owned_root: Path) -> None:
    """A same-manifest derivative cannot authorize a stale or forged export."""

    reloaded = load_prepared_study_v1(root=prepared_study.root)
    store = StudyStoreV1(reloaded.store_repository())
    verification = verify_study_v1(prepared=reloaded, store=store, ledger=None)
    forged = replace(verification, warnings=verification.warnings + ("forged warning",))
    output = owned_root / "forged"
    with pytest.raises(StudyAuthorityError, match="stale or forged"):
        export_study_trace_v1(prepared=reloaded, verification=forged, store=store, output=output)
    assert not output.exists()


def test_verifier_rejects_stale_prepared_summary_before_using_it(prepared_study) -> None:
    reloaded = load_prepared_study_v1(root=prepared_study.root)
    store = StudyStoreV1(reloaded.store_repository())
    stale = replace(reloaded, selected_parent_configuration_id="A")
    with pytest.raises(StudyAuthorityError, match="reauthenticated persisted authorities"):
        verify_study_v1(prepared=stale, store=store, ledger=None)


def test_verifier_rejects_changed_store_bytes_before_emitting_verdict(prepared_study, monkeypatch) -> None:
    reloaded = load_prepared_study_v1(root=prepared_study.root)
    store = StudyStoreV1(reloaded.store_repository())
    target = store.list_refs(kind="schemas")[0]
    original_read = store.read

    def tampered_read(reference):
        raw = original_read(reference)
        return raw + b"tamper" if reference == target else raw

    monkeypatch.setattr(store, "read", tampered_read)
    with pytest.raises(StudyAuthorityError, match="hash mismatch|changed|differ"):
        verify_study_v1(prepared=reloaded, store=store, ledger=None)


def test_verifier_rejects_missing_required_authority_index(prepared_study, monkeypatch) -> None:
    reloaded = load_prepared_study_v1(root=prepared_study.root)
    store = StudyStoreV1(reloaded.store_repository())
    original_loader = LocalArtifactRepositoryV5._load_adapter_state_authority

    def missing_scripted_index(repository, *, namespace, key):
        if namespace == "study-scripted-role" and key.endswith("-1"):
            return None
        return original_loader(repository, namespace=namespace, key=key)

    monkeypatch.setattr(LocalArtifactRepositoryV5, "_load_adapter_state_authority", missing_scripted_index)
    with pytest.raises(StudyAuthorityError, match="reauthenticated from its persisted graph|scripted role authority"):
        verify_study_v1(prepared=reloaded, store=store, ledger=None)


def test_human_review_must_bind_current_arm_artifacts(prepared_study) -> None:
    reloaded = load_prepared_study_v1(root=prepared_study.root)
    store = StudyStoreV1(reloaded.store_repository())
    review = HumanEvidenceReviewV1(
        reviewer="task8-test-reviewer",
        rubric_sha256=reloaded.rubric_ref.sha256,
        response_refs=(ArtifactRefV5("responses/wrong-arm.bin", "0" * 64),),
        reasons=("citation presence is not semantic proof",),
        artifact_refs=(reloaded.rubric_ref,),
        axis_reasons={axis: (f"reason for {axis}",) for axis in ("evidence_interpretation", "revision_quality", "claim_pattern")},
        axis_citations={axis: (reloaded.rubric_ref,) for axis in ("evidence_interpretation", "revision_quality", "claim_pattern")},
    )
    with pytest.raises(StudyAuthorityError, match="authenticated import"):
        verify_study_v1(prepared=reloaded, store=store, ledger=None, human_review=review)


def test_verifier_preserves_genuine_pending_and_import_before_journal_phases(persisted_phase_studies, monkeypatch) -> None:
    """Pending states come from real ledger transitions, never hidden completed refs."""

    from core.pit_optimizer_v5.two_round_study import driver
    from core.pit_optimizer_v5.two_round_study.__main__ import _OfflineGateway, _offline_ledger, _offline_response
    from core.pit_optimizer_v5.two_round_study.driver import execute_study_arm_v1
    from core.pit_optimizer_v5.two_round_study.imports import create_study_import_v1
    from core.pit_optimizer_v5.two_round_study.ledger import StudyLedgerV1
    from core.pit_optimizer_v5.two_round_study.live_calls import StudyGrantV1, authenticate_fixture_preflight_v1

    reservation_prepared, partial_prepared = persisted_phase_studies

    reservation_ledger = _offline_ledger(reservation_prepared)
    primary_reservation = reservation_ledger.reserve(reservation_prepared.live_call_for("primary"))
    reservation_store = StudyStoreV1(reservation_prepared.store_repository())
    reservation_grant = StudyGrantV1.from_canonical_json(
        reservation_store.read(reservation_store.list_refs(kind="grants")[0])
    )
    reservation_readonly = StudyLedgerV1(
        reservation_store,
        reservation_prepared.manifest,
        reservation_grant,
        approval=None,
    )
    reservation_before = _inventory(reservation_prepared.root)
    reservation_verification = verify_study_v1(
        prepared=load_prepared_study_v1(root=reservation_prepared.root),
        store=reservation_store,
        ledger=reservation_readonly,
    )
    reservation_after = _inventory(reservation_prepared.root)
    assert reservation_before == reservation_after
    reservation_arms = {item.arm: item for item in reservation_verification.arms}
    assert reservation_arms["primary"].state == "incomplete"
    assert reservation_arms["primary"].usage["pending_reservations"]
    assert reservation_arms["primary"].usage["pending_prospective_totals"]["input_tokens"] > 0
    assert reservation_arms["withheld"].state == "not_started"
    assert reservation_verification.verdicts.trace_integrity == "incomplete"

    # A real pre-dispatch rejection is a distinct persisted state.  Continue
    # the same durable graph through a mixed rejected/pending phase, then
    # reject the second arm.  Both rejected arms must keep the trace gate
    # incomplete; a non-completed history cannot be promoted by aggregation.
    reservation_ledger.reject_pre_dispatch(primary_reservation, reason="task8 deterministic test rejection")
    withheld_reservation = reservation_ledger.reserve(reservation_prepared.live_call_for("withheld"))
    mixed_verification = verify_study_v1(
        prepared=load_prepared_study_v1(root=reservation_prepared.root),
        store=reservation_store,
        ledger=reservation_readonly,
    )
    mixed_arms = {item.arm: item for item in mixed_verification.arms}
    assert mixed_arms["primary"].state == "rejected"
    assert mixed_arms["withheld"].state == "incomplete"
    assert mixed_verification.verdicts.trace_integrity == "incomplete"
    reservation_ledger.reject_pre_dispatch(withheld_reservation, reason="task8 deterministic test rejection")
    rejection_store = StudyStoreV1(reservation_prepared.store_repository())
    rejection_grant = StudyGrantV1.from_canonical_json(
        rejection_store.read(rejection_store.list_refs(kind="grants")[0])
    )
    rejection_readonly = StudyLedgerV1(
        rejection_store,
        reservation_prepared.manifest,
        rejection_grant,
        approval=None,
    )
    rejection_verification = verify_study_v1(
        prepared=load_prepared_study_v1(root=reservation_prepared.root),
        store=rejection_store,
        ledger=rejection_readonly,
    )
    rejection_arms = {item.arm: item for item in rejection_verification.arms}
    assert {item.state for item in rejection_arms.values()} == {"rejected"}
    assert rejection_verification.verdicts.trace_integrity == "incomplete"
    assert rejection_verification.verdicts.experiment_completion == "not_assessed"

    partial_ledger = _offline_ledger(partial_prepared)
    preflight_request = authenticate_fixture_preflight_v1(
        partial_prepared.preflight_for("primary"),
        require_current=True,
    )
    gateway = _OfflineGateway(
        {partial_prepared.live_call_for("primary").sha256: _offline_response(preflight_request)}
    )

    original_create_import = driver.create_study_import_v1

    def interrupt_before_import(**_kwargs):
        raise RuntimeError("task8 simulated interruption after durable terminal")

    monkeypatch.setattr(driver, "create_study_import_v1", interrupt_before_import)
    with pytest.raises(RuntimeError, match="after durable terminal"):
        execute_study_arm_v1(
            prepared=partial_prepared,
            arm="primary",
            ledger=partial_ledger,
            gateway=gateway,
        )

    partial_store = StudyStoreV1(partial_prepared.store_repository())
    partial_grant = StudyGrantV1.from_canonical_json(
        partial_store.read(partial_store.list_refs(kind="grants")[0])
    )
    partial_readonly = StudyLedgerV1(
        partial_store,
        partial_prepared.manifest,
        partial_grant,
        approval=None,
    )
    assert partial_store.list_refs(kind="terminals")
    assert not partial_store.list_refs(kind="imports")
    partial_before = _inventory(partial_prepared.root)
    terminal_only_verification = verify_study_v1(
        prepared=load_prepared_study_v1(root=partial_prepared.root),
        store=partial_store,
        ledger=partial_readonly,
    )
    partial_after = _inventory(partial_prepared.root)
    assert partial_before == partial_after
    terminal_only_arm = next(item for item in terminal_only_verification.arms if item.arm == "primary")
    assert terminal_only_arm.state == "incomplete"
    assert terminal_only_verification.verdicts.experiment_completion == "incomplete"

    # Continue the same real journal progression by importing the already
    # authenticated terminal, then stop before any investigator package or
    # round-two journal event exists.
    monkeypatch.setattr(driver, "create_study_import_v1", original_create_import)
    terminal = partial_readonly.verify_terminal(partial_store.list_refs(kind="terminals")[0])
    imported = create_study_import_v1(
        store=partial_store,
        ledger=partial_ledger,
        preflight=partial_prepared.preflight_for("primary"),
        terminal=terminal,
    )
    assert imported.arm == "primary"
    assert partial_store.list_refs(kind="imports")
    assert not partial_store.list_refs(kind="commitments")
    partial_before = _inventory(partial_prepared.root)
    partial_verification = verify_study_v1(
        prepared=load_prepared_study_v1(root=partial_prepared.root),
        store=partial_store,
        ledger=partial_readonly,
    )
    partial_after = _inventory(partial_prepared.root)
    assert partial_before == partial_after
    partial_arm = next(item for item in partial_verification.arms if item.arm == "primary")
    assert partial_arm.state == "incomplete"
    assert any("no journaled investigator package" in item for item in partial_arm.errors)
    assert partial_verification.verdicts.experiment_completion == "incomplete"


def test_no_grant_rejects_authenticated_event_only_history(tmp_path: Path) -> None:
    """A typed round intent cannot hide as an untouched prepared study."""

    from core.pit_optimizer_v5.contracts import HypothesisV5, MetricPredictionV5
    from core.pit_optimizer_v5.memory import RoundIntentPayloadV5
    from core.pit_optimizer_v5.runtime import _Journal
    from core.pit_optimizer_v5.selection import select_parent_v5
    from core.pit_optimizer_v5.two_round_study.driver import _projection, _round_two_input
    from core.pit_optimizer_v5.two_round_study.fixtures import reopen_study_fixture_v1
    from core.pit_optimizer_v5.two_round_study.runtime_ports import verify_scripted_role_authority_v1

    prepared = prepare_two_round_study_v1(
        root=tmp_path / "event-only" / "study",
        mode="offline_fixture",
        provider_settings=None,
    )
    fixture = reopen_study_fixture_v1(
        root=prepared.primary_root,
        manifest_ref=prepared.primary_manifest_ref,
        registry=prepared.registry,
    )
    verify_scripted_role_authority_v1(
        repository=fixture.repository,
        manifest=fixture.manifest.manifest,
        registry=fixture.registry,
        round_index=2,
    )
    inputs = _round_two_input(fixture)
    projection = _projection(fixture.repository, fixture)
    parent = select_parent_v5(
        state=projection.state,
        baseline=inputs.baseline,
        discovery_plan=inputs.panel_plan,
        evaluator_contract=inputs.evaluator_contract,
        stored_records=projection.stored_records,
    )
    intent = RoundIntentPayloadV5(
        parent_revision_sha256=parent.policy_identity_sha256,
        parent_semantic_fingerprint_sha256=parent.semantic_fingerprint_sha256,
        hypothesis=HypothesisV5(
            hypothesis_id="task8-event-only",
            rank=1,
            primary_mechanism="exit",
            causal_claim="typed event-only history",
            predicted_changes=(MetricPredictionV5("task8.probe.metric", "unchanged", "probe"),),
            evidence_ids=("v5.task8.event-only",),
            author_instructions="probe only",
        ),
        discovery_plan_sha256=inputs.panel_plan.discovery_plan_sha256,
        pit_data_scope=inputs.manifest.pit_data_scope,
        semantic_mode=inputs.manifest.semantic_mode,
    )
    event = _Journal(inputs, fixture.repository).append(intent)
    events = fixture.repository.load_round_events(
        campaign_id=inputs.campaign_id,
        round_index=2,
    )
    payloads = tuple(
        fixture.repository.load_round_payload(item.payload_ref, expected_kind=item.event_kind)
        for item in events
    )
    assert events == (event,)
    assert payloads == (intent,)
    assert all(not hasattr(item, "outcome") and not hasattr(item, "status") for item in payloads)

    loaded = load_prepared_study_v1(root=prepared.root)
    before = _inventory(loaded.root)
    store = StudyStoreV1(loaded.store_repository())
    with pytest.raises(StudyAuthorityError, match="fixture roots contain round-two history"):
        verify_study_v1(prepared=loaded, store=store, ledger=None)
    assert _inventory(loaded.root) == before


def _build_precommit_before_observation(tmp_path: Path):
    """Persist the real composer precommitment and stop before observation."""

    from core.pit_optimizer_v5.runtime import _Runtime
    from core.pit_optimizer_v5.selection import select_parent_v5
    from core.pit_optimizer_v5.two_round_study.__main__ import _OfflineGateway, _offline_ledger, _offline_response
    from core.pit_optimizer_v5.two_round_study.fixtures import reopen_study_fixture_v1
    from core.pit_optimizer_v5.two_round_study.imports import create_study_import_v1
    from core.pit_optimizer_v5.two_round_study.ledger import StudyLedgerV1, run_study_call_v1
    from core.pit_optimizer_v5.two_round_study.live_calls import authenticate_fixture_preflight_v1
    from core.pit_optimizer_v5.two_round_study.runtime_ports import compose_study_round_v1
    from core.pit_optimizer_v5.two_round_study.verification import load_prepared_study_v1
    from core.pit_optimizer_v5.memory import RoundIntentPayloadV5

    prepared = prepare_two_round_study_v1(
        root=tmp_path / "precommit" / "study",
        mode="offline_fixture",
        provider_settings=None,
    )
    ledger = _offline_ledger(prepared)
    preflight = authenticate_fixture_preflight_v1(prepared.preflight_for("primary"), require_current=True)
    gateway = _OfflineGateway({prepared.live_call_for("primary").sha256: _offline_response(preflight)})
    terminal = run_study_call_v1(
        request=prepared.live_call_for("primary"),
        fixture_request=preflight,
        ledger=ledger,
        gateway=gateway,
        deadline_monotonic=time.monotonic() + float(ledger.grant.per_call_deadline_seconds),
    )
    store = StudyStoreV1(prepared.store_repository())
    imported = create_study_import_v1(
        store=store,
        ledger=ledger,
        preflight=prepared.preflight_for("primary"),
        terminal=terminal,
    )
    fixture = reopen_study_fixture_v1(
        root=prepared.primary_root,
        manifest_ref=prepared.primary_manifest_ref,
        registry=prepared.registry,
    )
    inputs, dependencies = compose_study_round_v1(
        fixture=fixture,
        round_index=2,
        arm="primary",
        store=store,
        ledger=ledger,
        imported=imported,
    )
    runtime = _Runtime(inputs, dependencies)
    projection = runtime._recover_projection()
    parent = select_parent_v5(
        state=projection.state,
        baseline=inputs.baseline,
        discovery_plan=inputs.panel_plan,
        evaluator_contract=inputs.evaluator_contract,
        stored_records=projection.stored_records,
    )
    investigator_request = dependencies.requests.investigator_request(inputs, projection, parent)
    investigator_package = runtime._complete_role(
        request=investigator_request,
        role="investigator",
        parent=parent,
        hypothesis_id=None,
        experiment_ids=(),
    )
    assert investigator_package.accepted and investigator_package.artifact is not None
    novelty = dependencies.novelty.resolve(
        inputs=inputs,
        projection=projection,
        parent=parent,
        request=investigator_request,
        package=investigator_package,
        artifact=investigator_package.artifact,
    )
    intent = RoundIntentPayloadV5(
        parent_revision_sha256=parent.policy_identity_sha256,
        parent_semantic_fingerprint_sha256=parent.semantic_fingerprint_sha256,
        hypothesis=novelty.hypothesis,
        discovery_plan_sha256=inputs.panel_plan.discovery_plan_sha256,
        pit_data_scope=inputs.manifest.pit_data_scope,
        semantic_mode=inputs.manifest.semantic_mode,
    )
    runtime._round_intent(intent)
    assert dependencies.mechanism is not None
    dependencies.mechanism.before_authoring(
        round_intent=intent,
        campaign_id=inputs.campaign_id,
        round_index=2,
        parent_candidate=parent,
    )
    events = fixture.repository.load_round_events(campaign_id=inputs.campaign_id, round_index=2)
    assert not any(event.event_kind == "round_outcome" for event in events)
    loaded = load_prepared_study_v1(root=prepared.root)
    grant = ledger.grant
    readonly = StudyLedgerV1(store, prepared.manifest, grant, approval=None)
    return prepared, fixture, loaded, store, readonly


def test_precommitment_before_observation_is_exported_as_authenticated_closure(tmp_path: Path) -> None:
    from core.pit_optimizer_v5.mechanism_artifacts import MechanismArtifactRepositoryV5

    prepared, fixture, loaded, store, readonly = _build_precommit_before_observation(tmp_path)
    before = _inventory(loaded.root)
    verification = verify_study_v1(prepared=loaded, store=store, ledger=readonly)
    after_verify = _inventory(loaded.root)
    assert after_verify == before
    primary = next(item for item in verification.arms if item.arm == "primary")
    assert primary.state == "incomplete"
    assert not any(row.get("round_index") == 2 for row in primary.measurements)

    mechanism = MechanismArtifactRepositoryV5(fixture.repository)
    campaign_id = fixture.manifest.manifest.campaign_id
    authority = fixture.repository._load_adapter_state_authority(
        namespace="mechanism-v5",
        key=f"{campaign_id}-0002",
    )
    index = mechanism._load_precommitment_index_by_identity(campaign_id=campaign_id, round_index=2)
    assert authority is not None and index is not None
    expected = (
        authority[0].value_ref,
        authority[1],
        index.parent_revision_ref,
        index.parent_source_bundle_ref,
        index.spec_ref,
        index.corpus_ref,
    )
    expected_pairs = {(ref.relative_path, ref.sha256) for ref in expected}
    assert expected_pairs.issubset(
        {(ref.relative_path, ref.sha256) for ref in primary.artifact_refs}
    )
    for reference in expected:
        original_path = fixture.repository.root / Path(*reference.relative_path.split("/"))
        assert _long_exists(original_path)
        original_bytes = _long_read_bytes(original_path)
        assert hashlib.sha256(original_bytes).hexdigest() == reference.sha256

    output = tmp_path / "export"
    export_study_trace_v1(prepared=loaded, verification=verification, store=store, output=output)
    index_value = json.loads(_long_read_bytes(output / "artifact-index.json").decode("utf-8"))
    original_entries = [item for item in index_value["artifacts"] if item["authority"] != "export-derivative"]
    primary_authority = f"round-two-primary:{fixture.repository.root_identity_sha256}"
    exported = {
        (item["authority"], item["source_relative_path"], item["source_sha256"]): item
        for item in original_entries
        if item["authority"] == primary_authority
    }
    expected_primary = {(primary_authority, relative, digest) for relative, digest in expected_pairs}
    assert expected_primary.issubset(exported)
    for reference in expected:
        item = exported[(primary_authority, reference.relative_path, reference.sha256)]
        exported_path = output / Path(*item["relative_path"].split("/"))
        assert _long_exists(exported_path)
        assert _long_read_bytes(exported_path) == _long_read_bytes(
            fixture.repository.root / Path(*reference.relative_path.split("/"))
        )
    assert _inventory(loaded.root) == before


@pytest.mark.parametrize("missing", ("authority", "value"))
def test_precommitment_missing_authority_or_value_fails_closed(tmp_path: Path, monkeypatch, missing: str) -> None:
    _prepared, _fixture, loaded, store, readonly = _build_precommit_before_observation(tmp_path)
    original_authority = LocalArtifactRepositoryV5._load_adapter_state_authority
    original_state = LocalArtifactRepositoryV5.load_typed_state

    def missing_authority(repository, *, namespace, key):
        if missing == "authority" and namespace == "mechanism-v5" and key.endswith("-0002"):
            return None
        return original_authority(repository, namespace=namespace, key=key)

    def missing_value(repository, *, namespace, key, value_type, repair=True):
        if missing == "value" and namespace == "mechanism-v5" and key.endswith("-0002"):
            return None
        return original_state(
            repository,
            namespace=namespace,
            key=key,
            value_type=value_type,
            repair=repair,
        )

    monkeypatch.setattr(LocalArtifactRepositoryV5, "_load_adapter_state_authority", missing_authority)
    monkeypatch.setattr(LocalArtifactRepositoryV5, "load_typed_state", missing_value)
    before = _inventory(loaded.root)
    with pytest.raises(StudyAuthorityError, match="precommitment|authority"):
        verify_study_v1(prepared=loaded, store=store, ledger=readonly)
    assert _inventory(loaded.root) == before
