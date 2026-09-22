"""Focused RED regressions for the bounded V5 live-readiness fix wave."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import uuid

import pytest

from core.pit_optimizer_v5.two_round_study.contracts import StudyPendingAccounting
from core.pit_optimizer_v5.two_round_study.driver import (
    _long_files,
    _long_read_bytes,
    prepare_two_round_study_v1,
)
from core.pit_optimizer_v5.two_round_study.fixtures import create_study_fixture_v1
from core.pit_optimizer_v5.two_round_study.__main__ import _offline_ledger
from core.pit_optimizer_v5.two_round_study.ledger import StudyLedgerV1
from core.pit_optimizer_v5.two_round_study.runtime_ports import StudyCandidateRuntimeV1
from core.pit_optimizer_v5.two_round_study.store import StudyStoreV1
from core.pit_optimizer_v5.two_round_study.trace import export_study_trace_v1
from core.pit_optimizer_v5.two_round_study.verification import (
    StudyArmVerificationV1,
)
import core.pit_optimizer_v5.two_round_study.verification as verification_module


@pytest.fixture(scope="module")
def live_attribution_fixture():
    """Use one real prepared graph while keeping all response data synthetic."""

    prepared = prepare_two_round_study_v1(
        root=Path.cwd() / ".artifacts" / f"live-attribution-{uuid.uuid4().hex}" / "study",
        mode="offline_fixture",
        provider_settings=None,
    )
    # Keep the prepared graph's authenticated execution grant available so
    # the verifier exercises the live attribution branch without a provider.
    ledger = _offline_ledger(prepared)
    return replace(prepared, mode="live_study"), ledger


def _fake_arm(
    arm: str,
    *,
    production_assessment: str,
    case_contrast: str,
) -> StudyArmVerificationV1:
    return StudyArmVerificationV1(
        arm=arm,
        state="completed",
        terminal_ref=None,
        import_ref=None,
        round_two_status="completed",
        evidence_ids=(f"{arm}-synthetic-evidence",),
        production_assessment=production_assessment,
        case_contrast=case_contrast,
        usage={},
        artifact_refs=(),
        repository_root_identity_sha256="",
        measurements=(),
    )


@pytest.mark.parametrize(
    ("primary", "withheld", "reason_fragment"),
    (
        (
            ("supported_on_cases", "matched_on_cases"),
            ("insufficient_evidence", "contradicted_on_cases"),
            "insufficient_production",
        ),
        (
            ("supported_on_cases", "matched_on_cases"),
            ("insufficient_evidence", "unavailable"),
            "insufficient_production",
        ),
        (
            ("supported_on_cases", "matched_on_cases"),
            ("contradicted_on_cases", "unavailable"),
            "unavailable_or_unmeasured",
        ),
        (
            ("insufficient_evidence", "matched_on_cases"),
            ("contradicted_on_cases", "contradicted_on_cases"),
            "insufficient_production",
        ),
        (
            ("contradicted_on_cases", "unavailable"),
            ("supported_on_cases", "matched_on_cases"),
            "unavailable_or_unmeasured",
        ),
    ),
)
def test_live_attribution_requires_sufficient_measured_evidence_in_both_arms(
    live_attribution_fixture,
    monkeypatch,
    primary,
    withheld,
    reason_fragment,
) -> None:
    prepared, ledger = live_attribution_fixture
    store = StudyStoreV1(prepared.store_repository())
    monkeypatch.setattr(verification_module, "_load_prepared_study_v1", lambda root: prepared)

    def fake_verify_fixture_arm(*, prepared, arm, ledger, reservation_projection):
        values = primary if arm == "primary" else withheld
        return _fake_arm(
            arm,
            production_assessment=values[0],
            case_contrast=values[1],
        ), (), object()

    monkeypatch.setattr(verification_module, "_verify_fixture_arm", fake_verify_fixture_arm)
    verification = verification_module.verify_study_v1(prepared=prepared, store=store, ledger=ledger)

    assert verification.verdicts.feedback_attribution == "inconclusive"
    assert any(reason_fragment in item for item in verification.confounds)


def test_live_attribution_accepts_a_measured_supported_primary_against_measured_contradiction(
    live_attribution_fixture,
    monkeypatch,
) -> None:
    prepared, ledger = live_attribution_fixture
    store = StudyStoreV1(prepared.store_repository())
    monkeypatch.setattr(verification_module, "_load_prepared_study_v1", lambda root: prepared)

    def fake_verify_fixture_arm(*, prepared, arm, ledger, reservation_projection):
        values = (
            ("supported_on_cases", "matched_on_cases")
            if arm == "primary"
            else ("contradicted_on_cases", "contradicted_on_cases")
        )
        return _fake_arm(
            arm,
            production_assessment=values[0],
            case_contrast=values[1],
        ), (), object()

    monkeypatch.setattr(verification_module, "_verify_fixture_arm", fake_verify_fixture_arm)
    verification = verification_module.verify_study_v1(prepared=prepared, store=store, ledger=ledger)

    assert verification.verdicts.feedback_attribution == "supported"


def test_runtime_reports_emit_total_return_friction_drag_for_every_synthetic_scenario(tmp_path) -> None:
    fixture = create_study_fixture_v1(root=tmp_path / "fixture")
    runtime = StudyCandidateRuntimeV1(fixture.repository, fixture.registry)
    base_report = fixture.manifest.baseline_authority.campaign.episodes[0].evaluation.scenarios[0].report
    expected_drag = {"gross": Decimal("0"), "base": Decimal("0.5"), "stress": Decimal("1.0")}

    for configuration in fixture.registry.configurations:
        for outcome in configuration.portfolio_evaluator_inputs:
            gross_total = ((outcome.gross_ending_equity / outcome.starting_equity) - Decimal("1")) * Decimal("100")
            for scenario_id, ending_equity in (
                ("gross", outcome.gross_ending_equity),
                ("base", outcome.ending_equity),
                ("stress", outcome.stress_ending_equity),
            ):
                report = runtime._report(
                    configuration,
                    outcome,
                    scenario_id=scenario_id,
                    base_report=base_report,
                )
                scenario_total = ((ending_equity / outcome.starting_equity) - Decimal("1")) * Decimal("100")
                assert report.portfolio_total_return_pct == scenario_total
                assert report.friction_drag_pct == gross_total - scenario_total
                assert report.friction_drag_pct == expected_drag[scenario_id]


def test_fixture_baseline_reports_emit_corrected_friction_drag(tmp_path) -> None:
    fixture = create_study_fixture_v1(root=tmp_path / "fixture")
    p0 = fixture.registry.configuration("P0").portfolio_evaluator_inputs[0]
    gross_total = ((p0.gross_ending_equity / p0.starting_equity) - Decimal("1")) * Decimal("100")
    reports = {
        item.scenario_id: item.report
        for item in fixture.manifest.baseline_authority.campaign.episodes[0].evaluation.scenarios
    }

    assert reports["gross"].friction_drag_pct == Decimal("0")
    assert reports["base"].friction_drag_pct == gross_total - (
        (p0.ending_equity / p0.starting_equity) - Decimal("1")
    ) * Decimal("100")
    assert reports["stress"].friction_drag_pct == gross_total - (
        (p0.stress_ending_equity / p0.starting_equity) - Decimal("1")
    ) * Decimal("100")


def test_interrupted_publication_stays_pending_in_read_only_verification_and_export(monkeypatch) -> None:
    """A missing ordinary reservation must not erase its published pending spend."""

    prepared = prepare_two_round_study_v1(
        root=Path.cwd() / ".artifacts" / f"pending-publication-{uuid.uuid4().hex}" / "study",
        mode="offline_fixture",
        provider_settings=None,
    )
    writer = _offline_ledger(prepared)
    store = writer.store
    request = prepared.live_call_for("primary")
    original_put = store.put
    publication_ref = None
    publication_bytes = None
    published_reservation = None

    def inventory():
        return tuple(
            (relative, hashlib.sha256(_long_read_bytes(path)).hexdigest())
            for relative, path in _long_files(prepared.root)
        )

    def reopen():
        return StudyLedgerV1(store, prepared.manifest, writer.grant, approval=None)

    def fail_after_first_put(*, kind, key, content):
        original_put(kind=kind, key=key, content=content)
        raise RuntimeError("interrupted after publication write")

    def verify_prefix(label, *, reservation_ref=None):
        before = inventory()
        reloaded = verification_module.load_prepared_study_v1(root=prepared.root)
        verification = verification_module.verify_study_v1(prepared=reloaded, store=store, ledger=reopen())
        assert inventory() == before
        primary, withheld = verification.arms
        assert primary.arm == "primary" and withheld.arm == "withheld"
        assert withheld.state == "not_started"
        assert withheld.usage["pending_reservations"] == []
        assert withheld.usage["pending_prospective_totals"] == {
            "input_tokens": 0, "output_tokens": 0, "cost_usd": 0,
        }
        assert primary.usage["role_attempts"] == {}
        assert "terminal_usage" not in primary.usage
        assert "terminal_receipt" not in primary.usage
        for kind in ("dispatches", "responses", "raw-responses", "raw-response-failures", "parsed", "terminals", "reconciliations"):
            assert store.list_refs(kind=kind) == ()
        if published_reservation is None:
            assert primary.state == "not_started"
            assert primary.usage["pending_reservations"] == []
            assert primary.usage["pending_prospective_totals"] == {
                "input_tokens": 0, "output_tokens": 0, "cost_usd": 0,
            }
            assert store.list_refs(kind="requests") == ()
            assert store.list_refs(kind="reservations") == ()
            return

        assert primary.state == "incomplete"
        assert verification.verdicts.trace_integrity == "incomplete"
        assert verification.verdicts.experiment_completion == "incomplete"
        assert primary.usage["pending_reservations"] == [published_reservation]
        assert primary.usage["pending_prospective_totals"] == {
            "input_tokens": published_reservation["prospective_input_tokens"],
            "output_tokens": published_reservation["prospective_output_tokens"],
            "cost_usd": published_reservation["prospective_cost_usd"],
        }
        expected_provenance = [{
            "reservation_sha256": published_reservation["reservation_sha256"],
            "authority": f"study-store:{store.repository.root_identity_sha256}",
            "reservation_ref": None if reservation_ref is None else reservation_ref.to_primitive(),
            "publication_ref": publication_ref.to_primitive(),
        }]
        assert primary.usage["pending_reservation_provenance"] == expected_provenance
        assert publication_ref in verification.artifact_refs
        assert publication_ref not in primary.artifact_refs  # This is store-owned, not fixture-owned.
        output = prepared.root.parent / f"trace-{label}"
        export_study_trace_v1(prepared=reloaded, verification=verification, store=store, output=output)
        assert inventory() == before
        index = json.loads(_long_read_bytes(output / "artifact-index.json"))
        publication_entries = [
            entry for entry in index["artifacts"]
            if entry["source_relative_path"] == publication_ref.relative_path
            and entry["source_sha256"] == publication_ref.sha256
        ]
        assert len(publication_entries) == 1
        entry = publication_entries[0]
        assert entry["authority"] == f"study-store:{store.repository.root_identity_sha256}"
        assert entry["sha256"] == publication_ref.sha256
        assert _long_read_bytes(output / Path(*entry["relative_path"].split("/"))) == publication_bytes
        rubric = json.loads(_long_read_bytes(output / "rubric-results.json"))
        exported_primary = next(arm for arm in rubric["arms"] if arm["arm"] == "primary")
        assert exported_primary["state"] == "incomplete"
        assert exported_primary["usage"] == primary.usage
        trace = _long_read_bytes(output / "trace.md").decode("utf-8")
        assert publication_ref.relative_path in trace
        assert published_reservation["reservation_sha256"] in trace
        usage_line = next(line for line in trace.splitlines() if line.startswith("- recorded usage/accounting: `"))
        rendered_usage = usage_line.removeprefix("- recorded usage/accounting: `").removesuffix("`")
        assert json.loads(rendered_usage) == primary.usage
        with pytest.raises(StudyPendingAccounting):
            writer.reserve(prepared.live_call_for("withheld"))
        assert inventory() == before

    # Reuse one real graph through increasingly durable prefixes.  All missing
    # records result from interrupted production writes, never hidden/deleted refs.
    with monkeypatch.context() as fault:
        def fail_before_first_put(**_kwargs):
            raise RuntimeError("interrupted before first publication")

        fault.setattr(store, "put", fail_before_first_put)
        with pytest.raises(RuntimeError, match="before first publication"):
            writer.reserve(request)
    assert reopen().recover(request) is None
    verify_prefix("zero-write")

    with monkeypatch.context() as fault:
        fault.setattr(store, "put", fail_after_first_put)
        with pytest.raises(RuntimeError, match="after publication write"):
            writer.reserve(request)
    publication_ref, = store.list_refs(kind="requests")
    publication_bytes = store.read(publication_ref)
    publication = json.loads(publication_bytes)
    published_reservation = publication["reservation"]
    assert published_reservation["request_sha256"] == request.sha256
    assert published_reservation["prospective_input_tokens"] == request.input_bound_bytes
    assert published_reservation["prospective_output_tokens"] == request.max_output_tokens
    assert store.list_refs(kind="reservations") == ()
    verify_prefix("envelope-only")

    with monkeypatch.context() as fault:
        fault.setattr(store, "put", fail_after_first_put)
        with pytest.raises(RuntimeError, match="after publication write"):
            reopen().recover(request)
    assert len(store.list_refs(kind="requests")) == 2
    assert store.list_refs(kind="reservations") == ()
    verify_prefix("envelope-and-request")

    with pytest.raises(StudyPendingAccounting):
        reopen().recover(request)
    reservation_ref, = store.list_refs(kind="reservations")
    assert json.loads(store.read(reservation_ref)) == published_reservation
    verify_prefix("recovered", reservation_ref=reservation_ref)
    before_repeated_recovery = inventory()
    with pytest.raises(StudyPendingAccounting):
        reopen().recover(request)
    assert inventory() == before_repeated_recovery
