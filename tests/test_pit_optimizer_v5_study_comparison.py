"""Task 7 request comparison contracts."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import core.pit_optimizer_v5.two_round_study.comparison as comparison_module
from core.pit_optimizer_v5.two_round_study.comparison import (
    RequestComparisonV1,
    compare_study_requests_v1,
)
from core.pit_optimizer_v5.contracts import (
    RoleEvidenceV5,
    canonical_json_bytes_v5,
    canonical_primitive_v5,
)
from core.pit_optimizer_v5.provider import MechanismRoleInputV1, wire_role_messages_v5
from core.pit_optimizer_v5.two_round_study.contracts import StudyAuthorityError, StudyContractError
from core.pit_optimizer_v5.two_round_study.driver import prepare_two_round_study_v1
from core.pit_optimizer_v5.two_round_study.store import StudyStoreV1


def test_comparison_normalizes_only_the_known_mechanism_wrapper(tmp_path: Path) -> None:
    prepared = prepare_two_round_study_v1(
        root=tmp_path / "study",
        mode="offline_fixture",
        provider_settings=None,
    )
    store = StudyStoreV1(prepared.store_repository())
    comparison = compare_study_requests_v1(
        primary=prepared.primary_preflight,
        withheld=prepared.withheld_preflight,
        store=store,
    )

    assert isinstance(comparison, RequestComparisonV1)
    assert comparison.base_input_equal
    assert comparison.same_task_wording
    assert comparison.same_caps
    assert comparison.status == "eligible"
    assert comparison.primary_f_sha256 != comparison.withheld_f_sha256
    assert comparison.primary_l_sha256 != comparison.withheld_l_sha256
    assert comparison.f_byte_sizes[0] >= comparison.f_byte_sizes[1]
    assert comparison.l_byte_sizes[0] >= comparison.l_byte_sizes[1]
    assert "mechanism_wrapper" in comparison.allowed_differences
    assert {"preflight_identity", "fixture_request_identity", "live_request_identity"}.issubset(
        comparison.actual_differences
    )
    assert comparison.primary_fixture_request_sha256 == prepared.primary_preflight.request_sha256
    assert comparison.withheld_fixture_request_sha256 == prepared.withheld_preflight.request_sha256
    assert comparison.primary_live_call_sha256 == prepared.primary_live_call_sha256
    assert comparison.withheld_live_call_sha256 == prepared.withheld_live_call_sha256
    assert comparison.evidence_identity_sets[0]
    assert comparison.row_identity_sets[0]
    assert comparison.row_payload_digests[0]
    assert comparison.retained_id_sets[0]
    assert comparison.omitted_id_sets[0] == ()
    assert comparison.paired_changes


def test_comparison_round_trips_canonical_bytes(tmp_path: Path) -> None:
    prepared = prepare_two_round_study_v1(
        root=tmp_path / "study",
        mode="offline_fixture",
        provider_settings=None,
    )
    store = StudyStoreV1(prepared.store_repository())
    comparison = compare_study_requests_v1(
        primary=prepared.primary_preflight,
        withheld=prepared.withheld_preflight,
        store=store,
    )
    assert RequestComparisonV1.from_canonical_json(comparison.canonical_bytes()) == comparison


def test_comparison_rejects_changed_instruction_and_actual_limit(tmp_path: Path, monkeypatch) -> None:
    prepared = prepare_two_round_study_v1(
        root=tmp_path / "study",
        mode="offline_fixture",
        provider_settings=None,
    )
    store = StudyStoreV1(prepared.store_repository())
    original_load = comparison_module._load_live_call

    def changed_instruction(*, store, preflight):
        call = original_load(store=store, preflight=preflight)
        if call.arm != "withheld":
            return call
        message = dict(call.messages[0])
        content = dict(message["content"])
        content["ordinary_instruction"] = "unexpected changed task wording"
        message["content"] = content
        messages = (message, *call.messages[1:])
        return replace(
            call,
            messages=messages,
            projected_wire_messages=canonical_json_bytes_v5(messages),
        )

    monkeypatch.setattr(comparison_module, "_load_live_call", changed_instruction)
    changed = compare_study_requests_v1(
        primary=prepared.primary_preflight,
        withheld=prepared.withheld_preflight,
        store=store,
    )
    assert changed.same_task_wording is False
    assert changed.status == "inconclusive"
    assert "paired equality gate failed: same_task_wording" in changed.confounds

    monkeypatch.setattr(comparison_module, "_load_live_call", original_load)
    def changed_limit(*, store, preflight):
        call = original_load(store=store, preflight=preflight)
        if call.arm == "withheld":
            return replace(call, max_output_tokens=call.max_output_tokens + 1)
        return call

    monkeypatch.setattr(comparison_module, "_load_live_call", changed_limit)
    limited = compare_study_requests_v1(
        primary=prepared.primary_preflight,
        withheld=prepared.withheld_preflight,
        store=store,
    )
    assert limited.same_caps is False
    assert limited.status == "inconclusive"
    assert "paired equality gate failed: same_caps" in limited.confounds
    assert "output_token_cap" in limited.actual_differences

    monkeypatch.setattr(comparison_module, "_load_live_call", original_load)

    def changed_evidence_row(*, store, preflight):
        call = original_load(store=store, preflight=preflight)
        if call.arm != "withheld":
            return call
        message = dict(call.messages[0])
        content = dict(message["content"])
        evidence = list(content["evidence"])
        row = dict(evidence[0])
        payload = dict(row["payload"])
        payload["value"] = "ordinary replacement with no registered leak phrase"
        row["payload"] = payload
        evidence[0] = row
        content["evidence"] = evidence
        message["content"] = content
        messages = (message, *call.messages[1:])
        return replace(
            call,
            messages=messages,
            projected_wire_messages=canonical_json_bytes_v5(
                wire_role_messages_v5(messages)
            ),
        )

    monkeypatch.setattr(comparison_module, "_load_live_call", changed_evidence_row)
    changed_row = compare_study_requests_v1(
        primary=prepared.primary_preflight,
        withheld=prepared.withheld_preflight,
        store=store,
    )
    assert changed_row.same_task_wording is False
    assert changed_row.status == "inconclusive"
    assert "paired equality gate failed: same_task_wording" in changed_row.confounds


def test_comparison_surfaces_same_count_row_identity_and_payload_replacements(
    tmp_path: Path,
    monkeypatch,
) -> None:
    prepared = prepare_two_round_study_v1(
        root=tmp_path / "study",
        mode="offline_fixture",
        provider_settings=None,
    )
    original_authenticate = comparison_module.authenticate_fixture_preflight_v1
    original_load = comparison_module._load_live_call
    primary_request = original_authenticate(prepared.primary_preflight, require_current=False)
    withheld_request = original_authenticate(prepared.withheld_preflight, require_current=False)
    assert type(primary_request.role_input) is MechanismRoleInputV1
    assert type(withheld_request.role_input) is not MechanismRoleInputV1
    projections = primary_request.role_input.projections
    assert len(projections) >= 2

    all_extension_ids = {
        evidence_id
        for projection in projections
        for row in projection.rows
        for evidence_id in row.evidence_ids
    }
    base_items = tuple(
        item for item in primary_request.role_evidence.items if item.evidence_id not in all_extension_ids
    )

    def single_projection(request, projection):
        selected_items = tuple(
            item for item in primary_request.role_evidence.items if item.evidence_id in projection.evidence_ids
        )
        evidence = RoleEvidenceV5(schema_version=5, items=(*base_items, *selected_items))
        base_input = (
            request.role_input.base_input
            if type(request.role_input) is MechanismRoleInputV1
            else request.role_input
        )
        role_input = MechanismRoleInputV1(
            role="investigator",
            base_input=base_input,
            projection=projection,
        )
        return replace(request, role_input=role_input, role_evidence=evidence)

    # Both arms use real closed typed projections and rows from the authenticated
    # fixture.  Selecting different projections gives equal row counts while
    # changing the actual row identities and payload digests.
    fake_requests = {
        "primary": single_projection(primary_request, projections[0]),
        "withheld": single_projection(withheld_request, projections[1]),
    }
    store = StudyStoreV1(prepared.store_repository())
    fake_calls = {
        arm: replace(
            original_load(store=store, preflight=preflight),
            messages=request.messages,
            projected_wire_messages=canonical_json_bytes_v5(request.messages),
        )
        for arm, preflight, request in (
            ("primary", prepared.primary_preflight, fake_requests["primary"]),
            ("withheld", prepared.withheld_preflight, fake_requests["withheld"]),
        )
    }

    def fake_authenticate(preflight, *, require_current):
        return fake_requests[preflight.arm]

    def fake_load(*, store, preflight):
        return fake_calls[preflight.arm]

    monkeypatch.setattr(comparison_module, "authenticate_fixture_preflight_v1", fake_authenticate)
    monkeypatch.setattr(comparison_module, "_load_live_call", fake_load)
    comparison = compare_study_requests_v1(
        primary=prepared.primary_preflight,
        withheld=prepared.withheld_preflight,
        store=StudyStoreV1(prepared.store_repository()),
    )
    assert comparison.row_counts[0] == comparison.row_counts[1] == 2
    primary_ids, withheld_ids = comparison.row_identity_sets
    assert len(primary_ids) == len(withheld_ids) == 2
    assert set(primary_ids).isdisjoint(withheld_ids)
    assert comparison.row_payload_digests[0] != comparison.row_payload_digests[1]
    assert all("=" in item for item in comparison.row_payload_digests[0] + comparison.row_payload_digests[1])
    assert any(item.startswith("row_identity:primary_only:") for item in comparison.paired_changes)
    assert any(item.startswith("row_identity:withheld_only:") for item in comparison.paired_changes)
    assert any(item.startswith("row_payload:primary_only:") for item in comparison.paired_changes)
    assert any(item.startswith("row_payload:withheld_only:") for item in comparison.paired_changes)


def test_comparison_strictly_decodes_nested_delta_fields(tmp_path: Path) -> None:
    prepared = prepare_two_round_study_v1(
        root=tmp_path / "study",
        mode="offline_fixture",
        provider_settings=None,
    )
    raw = prepared.request_comparison.to_primitive()
    raw["row_identity_sets"] = [["row-primary"], "row-withheld"]
    with pytest.raises(StudyContractError, match="row identity sets"):
        RequestComparisonV1.from_primitive(raw)


@pytest.mark.parametrize("schema_version", [True, 1.0, "1"])
def test_comparison_schema_version_requires_exact_integer(tmp_path: Path, schema_version: object) -> None:
    prepared = prepare_two_round_study_v1(
        root=tmp_path / "study",
        mode="offline_fixture",
        provider_settings=None,
    )
    raw = prepared.request_comparison.to_primitive()
    raw["schema_version"] = schema_version
    with pytest.raises(StudyContractError, match="schema version"):
        RequestComparisonV1.from_primitive(raw)


def test_comparison_cannot_mark_a_failed_equality_gate_eligible(tmp_path: Path) -> None:
    prepared = prepare_two_round_study_v1(
        root=tmp_path / "study",
        mode="offline_fixture",
        provider_settings=None,
    )
    comparison = prepared.request_comparison
    with pytest.raises(StudyAuthorityError, match="equality gate|confound"):
        replace(comparison, same_caps=False, status="eligible", confounds=())

    honest = replace(
        comparison,
        same_caps=False,
        status="inconclusive",
        confounds=("paired equality gate failed: same_caps",),
    )
    assert honest.status == "inconclusive"
    assert "same_caps" in honest.confounds[0]
    assert RequestComparisonV1.from_canonical_json(honest.canonical_bytes()) == honest


def test_comparison_records_a_withheld_prompt_leak_as_inconclusive(tmp_path: Path, monkeypatch) -> None:
    prepared = prepare_two_round_study_v1(
        root=tmp_path / "study",
        mode="offline_fixture",
        provider_settings=None,
    )
    store = StudyStoreV1(prepared.store_repository())
    original_wire_content = comparison_module._wire_content

    def leaked_wire_content(call):
        wire = original_wire_content(call)
        if call.arm == "withheld":
            return {"messages": wire, "leaked_example": "contradicted_on_cases 0/3"}
        return wire

    monkeypatch.setattr(comparison_module, "_wire_content", leaked_wire_content)
    comparison = compare_study_requests_v1(
        primary=prepared.primary_preflight,
        withheld=prepared.withheld_preflight,
        store=store,
    )
    assert comparison.status == "inconclusive"
    assert comparison.leak_observations
    assert any("contradiction" in item for item in comparison.leak_observations)
    assert RequestComparisonV1.from_canonical_json(comparison.canonical_bytes()) == comparison


def test_comparison_distinguishes_intended_mechanism_from_primary_ordinary_disclosure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    prepared = prepare_two_round_study_v1(
        root=tmp_path / "study",
        mode="offline_fixture",
        provider_settings=None,
    )
    store = StudyStoreV1(prepared.store_repository())
    original_load = comparison_module._load_live_call
    primary_call = original_load(store=store, preflight=prepared.primary_preflight)
    withheld_call = original_load(store=store, preflight=prepared.withheld_preflight)
    comparison = prepared.request_comparison
    assert comparison.status == "eligible"
    assert any("primary treatment" in item for item in comparison.actual_differences)

    ordinary_content = dict(primary_call.messages[0]["content"])
    ordinary_content["history"] = {
        "summary": "A was contradicted; no decisions changed",
        "critic": "contradiction on cases",
        "source": "the mechanism contradicts the parent",
        "contradicted_on_cases": "0",
        "contradiction_count": "0 / 3 decisions changed",
        "copied_role_input": canonical_primitive_v5(
            comparison_module.authenticate_fixture_preflight_v1(
                prepared.primary_preflight,
                require_current=False,
            ).role_input
        ),
    }
    ordinary_messages = (
        {"role": "user", "content": ordinary_content},
        *primary_call.messages[1:],
    )
    withheld_content = dict(withheld_call.messages[0]["content"])
    withheld_content["history"] = {
        "summary": "A was contradicted; no decisions changed",
        "critic": "contradiction on cases",
        "source": "the mechanism contradicts the parent",
        "contradicted_on_cases": "0",
        "contradiction_count": "0 / 3 decisions changed",
    }
    withheld_messages = (
        {"role": "user", "content": withheld_content},
        *withheld_call.messages[1:],
    )

    def ordinary_load(*, store, preflight):
        if preflight.arm == "primary":
            return replace(
                primary_call,
                messages=ordinary_messages,
                projected_wire_messages=canonical_json_bytes_v5(
                    wire_role_messages_v5(ordinary_messages)
                ),
            )
        return replace(
            withheld_call,
            messages=withheld_messages,
            projected_wire_messages=canonical_json_bytes_v5(
                wire_role_messages_v5(withheld_messages)
            ),
        )

    monkeypatch.setattr(comparison_module, "_load_live_call", ordinary_load)
    ordinary = compare_study_requests_v1(
        primary=prepared.primary_preflight,
        withheld=prepared.withheld_preflight,
        store=store,
    )
    assert ordinary.status == "inconclusive"
    assert any("primary" in item and "history" in item for item in ordinary.leak_observations)
    assert any("A was contradicted" in item for item in ordinary.leak_observations)
    assert any("copied_role_input" in item for item in ordinary.leak_observations)
    assert any("withheld" in item and "history" in item for item in ordinary.leak_observations)
    assert any("contradiction count" in item for item in ordinary.leak_observations)
    assert any("[key]" in item and "contradicted_on_cases" in item for item in ordinary.leak_observations)
    assert not any(
        "primary treatment" in item and "copied_role_input" in item
        for item in ordinary.actual_differences
    )


def test_comparison_rejects_primary_wire_bytes_that_do_not_match_l_messages(
    tmp_path: Path,
    monkeypatch,
) -> None:
    prepared = prepare_two_round_study_v1(
        root=tmp_path / "study",
        mode="offline_fixture",
        provider_settings=None,
    )
    store = StudyStoreV1(prepared.store_repository())
    original_load = comparison_module._load_live_call
    primary_call = original_load(store=store, preflight=prepared.primary_preflight)
    withheld_call = original_load(store=store, preflight=prepared.withheld_preflight)
    wire = comparison_module._wire_content(primary_call)
    assert isinstance(wire, list)
    altered_wire = list(wire)
    altered_wire[0] = dict(altered_wire[0])
    altered_wire[0]["content"] = altered_wire[0]["content"] + " ordinary contradiction on cases"

    def inconsistent_load(*, store, preflight):
        if preflight.arm == "primary":
            return replace(
                primary_call,
                projected_wire_messages=canonical_json_bytes_v5(altered_wire),
            )
        return withheld_call

    monkeypatch.setattr(comparison_module, "_load_live_call", inconsistent_load)
    comparison = compare_study_requests_v1(
        primary=prepared.primary_preflight,
        withheld=prepared.withheld_preflight,
        store=store,
    )
    assert comparison.status == "inconclusive"
    assert "audit parser failure" in comparison.confounds
    assert any("unresolved audit channel" in item for item in comparison.confounds)
    assert comparison.audit_limitations


def test_primary_treatment_requires_authenticated_position_and_multiplicity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    prepared = prepare_two_round_study_v1(
        root=tmp_path / "study",
        mode="offline_fixture",
        provider_settings=None,
    )
    store = StudyStoreV1(prepared.store_repository())
    original_load = comparison_module._load_live_call
    primary_call = original_load(store=store, preflight=prepared.primary_preflight)
    withheld_call = original_load(store=store, preflight=prepared.withheld_preflight)
    primary_request = comparison_module.authenticate_fixture_preflight_v1(
        prepared.primary_preflight,
        require_current=False,
    )
    assert prepared.request_comparison.status == "eligible"
    wrapper = canonical_primitive_v5(primary_request.role_input)
    mechanism_ids = {
        evidence_id
        for projection in primary_request.role_input.projections
        for row in projection.rows
        for evidence_id in row.evidence_ids
    }
    original_content = dict(primary_call.messages[0]["content"])
    original_rows = list(original_content["evidence"])
    mechanism_positions = [
        index for index, row in enumerate(original_rows) if row["evidence_id"] in mechanism_ids
    ]
    assert len(mechanism_positions) >= 2
    first_index, second_index = mechanism_positions[:2]
    first_row = original_rows[first_index]
    marker_text = canonical_json_bytes_v5(first_row).decode("utf-8").casefold()
    assert not any(
        marker in marker_text
        for marker in (
            "contradicted_on_cases",
            "paired_local_observations",
            "0 / 3 decisions changed",
            "a was contradicted",
            "mechanism contradicts the parent",
        )
    )
    second_marker_text = canonical_json_bytes_v5(original_rows[second_index]).decode("utf-8").casefold()
    assert not any(
        marker in second_marker_text
        for marker in (
            "contradicted_on_cases",
            "paired_local_observations",
            "0 / 3 decisions changed",
            "a was contradicted",
            "mechanism contradicts the parent",
        )
    )

    def wire_messages(messages):
        return canonical_json_bytes_v5(wire_role_messages_v5(messages))

    def compare_pair(primary_messages, withheld_messages=None):
        if withheld_messages is None:
            withheld_messages = withheld_call.messages
        primary_variant = replace(
            primary_call,
            messages=primary_messages,
            projected_wire_messages=wire_messages(primary_messages),
        )
        withheld_variant = replace(
            withheld_call,
            messages=withheld_messages,
            projected_wire_messages=wire_messages(withheld_messages),
        )

        def fake_load(*, store, preflight):
            return primary_variant if preflight.arm == "primary" else withheld_variant

        monkeypatch.setattr(comparison_module, "_load_live_call", fake_load)
        return compare_study_requests_v1(
            primary=prepared.primary_preflight,
            withheld=prepared.withheld_preflight,
            store=store,
        )

    # A marker-free same-ID payload change must remain visible even though the
    # row still cites an authenticated mechanism evidence ID.
    changed_row = dict(first_row)
    changed_payload = dict(changed_row["payload"])
    changed_payload["value"] = "marker-free primary payload replacement"
    changed_row["payload"] = changed_payload
    changed_rows = list(original_rows)
    changed_rows[first_index] = changed_row
    changed_content = dict(original_content)
    changed_content["evidence"] = changed_rows
    changed_messages = (
        {"role": "user", "content": changed_content},
        *primary_call.messages[1:],
    )
    changed = compare_pair(changed_messages)
    assert changed.same_task_wording is False
    assert changed.status == "inconclusive"

    # An exact treatment row duplicated at another first-message position is
    # not an additional authenticated occurrence.
    duplicate_content = dict(original_content)
    duplicate_content["evidence"] = [*original_rows, first_row]
    duplicate_messages = (
        {"role": "user", "content": duplicate_content},
        *primary_call.messages[1:],
    )
    duplicate = compare_pair(duplicate_messages)
    assert duplicate.same_task_wording is False
    assert duplicate.status == "inconclusive"

    # Swapping two exact mechanism rows relocates both issued values without
    # changing the count or introducing a lexical leak marker.
    moved_rows = list(original_rows)
    moved_rows[first_index], moved_rows[second_index] = moved_rows[second_index], moved_rows[first_index]
    moved_content = dict(original_content)
    moved_content["evidence"] = moved_rows
    moved_messages = (
        {"role": "user", "content": moved_content},
        *primary_call.messages[1:],
    )
    moved = compare_pair(moved_messages)
    assert moved.same_task_wording is False
    assert moved.status == "inconclusive"

    # Keep the L message count and prompt shape aligned across arms while
    # copying one exact row into the appended study message only in primary.
    primary_tail = dict(primary_call.messages[1]["content"])
    withheld_tail = dict(withheld_call.messages[1]["content"])
    primary_tail["evidence"] = [first_row]
    withheld_tail["evidence"] = []
    primary_l_copy = (
        primary_call.messages[0],
        {"role": "user", "content": primary_tail},
    )
    withheld_l_copy = (
        withheld_call.messages[0],
        {"role": "user", "content": withheld_tail},
    )
    later_row = compare_pair(primary_l_copy, withheld_l_copy)
    assert later_row.same_task_wording is False
    assert later_row.status == "inconclusive"

    # The same placement rule applies to a copied authenticated wrapper in
    # the appended message.  Its unchanged inner assessment marker must be
    # ordinary disclosure at the unissued location, rather than treatment.
    primary_tail["evidence"] = []
    primary_tail["role_input"] = wrapper
    withheld_tail["role_input"] = canonical_primitive_v5(primary_request.role_input.base_input)
    primary_wrapper_copy = (
        primary_call.messages[0],
        {"role": "user", "content": primary_tail},
    )
    withheld_wrapper_copy = (
        withheld_call.messages[0],
        {"role": "user", "content": withheld_tail},
    )
    later_wrapper = compare_pair(primary_wrapper_copy, withheld_wrapper_copy)
    assert later_wrapper.same_task_wording is False
    assert later_wrapper.status == "inconclusive"
    assert any(
        "primary:messages:[1].content.role_input.projection" in item
        and "contradicted_on_cases" in item
        for item in later_wrapper.leak_observations
    )
    assert not any(
        "primary treatment observation" in item
        and "[1].content.role_input.projection" in item
        for item in later_wrapper.actual_differences
    )
