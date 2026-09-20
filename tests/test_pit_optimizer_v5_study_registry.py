"""Focused offline checks for the frozen two-round study registry."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import builtins
from datetime import date
import hashlib
import importlib
import json
from itertools import pairwise
from pathlib import Path

import pytest

from core.pit_optimizer_v5.probes import policy_probe_suite_v1
from core.pit_optimizer_v5.search import annualized_return_pct
from core.pit_optimizer_v5.candidate_ir import (
    derive_changed_symbols_v5,
    derive_policy_revision_identity_v5,
)
from core.pit_optimizer_v5.artifacts import ArtifactRefV5, LocalArtifactRepositoryV5
from core.pit_optimizer_v5.rendering import render_variants
from core.strategy_policy.contracts_v3 import ExitSnapshotV3
from core.pit_optimizer_v5.two_round_study.fixtures import (
    _build_manifest,
    _study_resource_capabilities,
    _study_search_capabilities,
    create_study_fixture_v1,
    reopen_study_fixture_v1,
    study_resource_budget_v1,
)
from core.pit_optimizer_v5.two_round_study.registry import (
    FrozenBehaviorRegistryV1,
    build_study_registry_v1,
    registry_client_v1,
    registry_decision_v1,
    reviewed_source_parent_v1,
    reviewed_source_template_v1,
    verify_registry_v1,
)


def _exit_cases():
    return tuple(case for case in policy_probe_suite_v1() if case.method == "evaluate_exit")


def test_registry_is_deterministic_and_fingerprints_are_derived_from_full_outputs() -> None:
    first = build_study_registry_v1()
    second = build_study_registry_v1()

    assert first == second
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.sha256 == second.sha256
    verify_registry_v1(first)
    assert tuple(item.configuration_id for item in first.configurations) == (
        "P0",
        "A",
        "S",
        "S-inert",
        "S-always-on",
        "S-gte-0.05",
        "S-gte-0.50",
    )
    assert first.configurations[0].fixed_suite_fingerprint
    assert first.configurations[0].fixed_suite_outputs


def test_fixed_and_supplemental_observations_use_full_snapshot_identity() -> None:
    registry = build_study_registry_v1()

    fixed_exit = _exit_cases()
    expected_truth_table = {
        "P0": (False, False),
        "A": (True, False),
        "S": (False, True),
        "S-inert": (False, True),
        "S-always-on": (True, False),
        "S-gte-0.05": (False, False),
        "S-gte-0.50": (False, True),
    }
    clients = {
        configuration_id: registry_client_v1(registry, configuration_id)
        for configuration_id in expected_truth_table
    }
    for configuration_id, expected in expected_truth_table.items():
        actual = tuple(
            clients[configuration_id].evaluate_exit(case.snapshot).early_winner_hold
            for case in fixed_exit
        )
        assert actual == expected

    fixed_a = tuple(clients["A"].evaluate_exit(case.snapshot) for case in fixed_exit)
    fixed_p0 = tuple(clients["P0"].evaluate_exit(case.snapshot) for case in fixed_exit)
    fixed_s = tuple(clients["S"].evaluate_exit(case.snapshot) for case in fixed_exit)
    assert fixed_a[0] != fixed_p0[0]
    assert fixed_a[1] == fixed_p0[1]
    assert fixed_a[0] != fixed_s[0]
    assert fixed_a[1] != fixed_s[1]

    config_a = registry.configuration("A")
    assert tuple(item.input_value for item in config_a.synthetic_evaluator_inputs) == (
        Decimal("0.20"),
        Decimal("0.50"),
        Decimal("0.80"),
        None,
    )
    assert tuple(item.changed for item in config_a.synthetic_evaluator_outputs) == (
        False,
        False,
        False,
        False,
    )
    assert len({item.snapshot_sha256 for item in config_a.synthetic_evaluator_inputs}) == 4
    assert config_a.synthetic_evaluator_inputs[0].method == "evaluate_exit"

    synthetic_snapshots = tuple(
        ExitSnapshotV3.from_canonical_json(item.snapshot_canonical_json.decode("utf-8"))
        for item in registry.configuration("S").synthetic_evaluator_inputs
    )
    for snapshot, value in zip(synthetic_snapshots, (Decimal("0.20"), Decimal("0.50"), Decimal("0.80"), None), strict=True):
        s_decision = clients["S"].evaluate_exit(snapshot)
        for configuration_id, local in (
            ("S-inert", False),
            ("S-always-on", True),
            ("S-gte-0.05", value is not None and value >= Decimal("0.05")),
            ("S-gte-0.50", value is not None and value >= Decimal("0.50")),
        ):
            candidate = clients[configuration_id].evaluate_exit(snapshot)
            assert candidate.early_winner_hold == (s_decision.early_winner_hold ^ local)


def test_equivalent_parent_and_sibling_configurations_remain_equal() -> None:
    registry = build_study_registry_v1()
    p0 = registry.configuration("P0")
    s = registry.configuration("S")
    assert registry.configuration("S-inert").fixed_suite_fingerprint == s.fixed_suite_fingerprint
    assert registry.configuration("S-always-on").fixed_suite_fingerprint != s.fixed_suite_fingerprint
    assert registry.configuration("S-gte-0.05").fixed_suite_fingerprint != s.fixed_suite_fingerprint
    assert registry.configuration("S-gte-0.50").fixed_suite_fingerprint == s.fixed_suite_fingerprint
    assert registry.configuration("S-gte-0.05").synthetic_evaluator_outputs == p0.synthetic_evaluator_outputs


def test_decisions_are_contract_valid_and_preserve_exit_fields() -> None:
    registry = build_study_registry_v1()
    for case in policy_probe_suite_v1():
        for configuration_id in ("P0", "A", "S", "S-inert", "S-always-on", "S-gte-0.05", "S-gte-0.50"):
            decision = registry_decision_v1(
                registry=registry,
                configuration_id=configuration_id,
                method=case.method,
                snapshot=case.snapshot,
            )
            assert type(decision) is not type(None)
            if case.method == "evaluate_exit":
                parent = registry_decision_v1(
                    registry=registry,
                    configuration_id="P0",
                    method=case.method,
                    snapshot=case.snapshot,
                )
                assert decision.actions == parent.actions
                assert decision.next_stop_price == parent.next_stop_price
                assert decision.scale_out_tier == parent.scale_out_tier
                assert decision.breakeven_armed == parent.breakeven_armed
                assert decision.ema_trailing_active == parent.ema_trailing_active


def test_non_exit_decisions_are_canonically_equal_across_the_catalog() -> None:
    registry = build_study_registry_v1()
    configuration_ids = tuple(item.configuration_id for item in registry.configurations)
    for case in policy_probe_suite_v1():
        if case.method == "evaluate_exit":
            continue
        baseline = registry_decision_v1(
            registry=registry,
            configuration_id="P0",
            method=case.method,
            snapshot=case.snapshot,
        ).to_canonical_json()
        for configuration_id in configuration_ids:
            decision = registry_decision_v1(
                registry=registry,
                configuration_id=configuration_id,
                method=case.method,
                snapshot=case.snapshot,
            )
            assert decision.to_canonical_json() == baseline


def test_registry_rejects_tampered_outputs_unknown_ids_and_source_execution() -> None:
    registry = build_study_registry_v1()
    tampered = replace(
        registry,
        configurations=tuple(
            replace(configuration, fixed_suite_fingerprint="0" * 64)
            if configuration.configuration_id == "A"
            else configuration
            for configuration in registry.configurations
        ),
    )
    with pytest.raises(ValueError, match="fingerprint|output"):
        verify_registry_v1(tampered)
    with pytest.raises(ValueError, match="unknown"):
        registry_client_v1(registry, "unknown")

    with pytest.raises(ValueError, match="template identity"):
        verify_registry_v1(replace(registry, source_template_sha256="0" * 64))

    source = registry.configuration("A").source_bundle.files[-1].source
    assert "exec(" not in source
    assert "importlib" not in source
    assert "__import__" not in source


def test_registry_rejects_altered_registered_source_and_ambiguous_parameter_wire_types() -> None:
    registry = build_study_registry_v1()
    configuration = registry.configuration("A")
    exit_file = configuration.source_bundle.files[-1]
    altered_exit = replace(
        exit_file,
        source=exit_file.source.replace("configuration_axis = (1)", "configuration_axis = (0)"),
    )
    altered_bundle = replace(
        configuration.source_bundle,
        files=configuration.source_bundle.files[:-1] + (altered_exit,),
    )
    altered_revision = derive_policy_revision_identity_v5(
        source_bundle=altered_bundle,
        trusted_policy_runtime_sha256=configuration.policy_revision.trusted_policy_runtime_sha256,
        immutable_constraints_sha256=configuration.policy_revision.immutable_constraints_sha256,
    )
    altered_configuration = replace(
        configuration,
        source_bundle=altered_bundle,
        policy_revision=altered_revision,
    )
    altered_registry = replace(
        registry,
        configurations=tuple(
            altered_configuration if item.configuration_id == "A" else item
            for item in registry.configurations
        ),
    )
    with pytest.raises(ValueError, match="catalog|metadata|source"):
        verify_registry_v1(altered_registry)

    wire = json.loads(configuration.canonical_bytes())
    wire["parameters"][1][1] = {"type": "str", "value": "0.05"}
    with pytest.raises(ValueError, match="metadata|configuration|parameter"):
        type(configuration).from_primitive(wire)
    wire["parameters"][1][1] = "0.05"
    with pytest.raises(ValueError, match="parameter"):
        type(configuration).from_primitive(wire)


def test_source_templates_encode_the_frozen_registered_rule() -> None:
    registry = build_study_registry_v1()
    expected_axes = {
        "P0": 0,
        "A": 1,
        "S": 2,
        "S-inert": 3,
        "S-always-on": 4,
        "S-gte-0.05": 5,
        "S-gte-0.50": 6,
    }
    for configuration in registry.configurations:
        source = configuration.source_bundle.files[-1].source
        assert f"configuration_axis = ({expected_axes[configuration.configuration_id]})" in source
        assert "early_winner_hold=snapshot.base.early_winner_hold != active" in source
        assert "PIT_AXIS" not in source
    assert "0.50" in registry.configuration("S-gte-0.50").source_bundle.files[-1].source


def test_normal_renderer_reproduces_every_registered_source_variant() -> None:
    registry = build_study_registry_v1()
    parent, parent_revision = reviewed_source_parent_v1()
    template = reviewed_source_template_v1()
    assert template.axes[0].name == "configuration_axis"
    assert 'PIT_AXIS("configuration_axis")' in template.source_operations[0].replacement_source
    rendered = render_variants(
        parent=parent,
        parent_revision=parent_revision,
        template=template,
        maximum=7,
    )
    assert tuple(dict(item.assignment.values)["configuration_axis"] for item in rendered) == tuple(range(7))
    for variant in rendered:
        axis = dict(variant.assignment.values)["configuration_axis"]
        configuration = registry.configurations[axis]
        assert variant.source_bundle == configuration.source_bundle
        assert variant.policy_revision == configuration.policy_revision


def test_catalog_axis_is_a_single_exit_function_symbol_edit() -> None:
    registry = build_study_registry_v1()
    expected = ("core.strategy_policy.v3.exit.evaluate_exit",)
    assert derive_changed_symbols_v5(
        before=registry.configuration("P0").source_bundle,
        after=registry.configuration("A").source_bundle,
    ) == expected
    assert derive_changed_symbols_v5(
        before=registry.configuration("P0").source_bundle,
        after=registry.configuration("S").source_bundle,
    ) == expected


def test_registry_verification_binds_catalog_metadata() -> None:
    registry = build_study_registry_v1()
    tampered = replace(
        registry,
        configurations=tuple(
            replace(configuration, parameters=(("operator", "lt"), ("threshold", Decimal("0.10"))))
            if configuration.configuration_id == "A"
            else configuration
            for configuration in registry.configurations
        ),
    )
    with pytest.raises(ValueError, match="catalog|metadata|configuration"):
        verify_registry_v1(tampered)


def test_registry_round_trip_preserves_bound_portfolio_inputs() -> None:
    registry = build_study_registry_v1()
    decoded = FrozenBehaviorRegistryV1.from_canonical_json(registry.canonical_bytes())
    assert decoded == registry
    assert tuple(
        item.ending_equity
        for item in decoded.configuration("A").portfolio_evaluator_inputs
    ) == (Decimal("101"), Decimal("101"))
    tampered = replace(
        registry,
        configurations=tuple(
            replace(
                configuration,
                portfolio_evaluator_inputs=tuple(
                    replace(item, configuration_id="A")
                    for item in registry.configuration("S").portfolio_evaluator_inputs
                ),
            )
            if configuration.configuration_id == "A"
            else configuration
            for configuration in registry.configurations
        ),
    )
    with pytest.raises(ValueError, match="portfolio|metadata|configuration"):
        verify_registry_v1(tampered)


def test_decision_function_digest_pins_the_reviewed_rule() -> None:
    registry = build_study_registry_v1()
    assert registry.decision_function_sha256 != hashlib.sha256(
        b"registered_v5_decision_function_v1"
    ).hexdigest()


def test_registry_does_not_import_or_execute_materialized_source(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = build_study_registry_v1()
    case = next(case for case in policy_probe_suite_v1() if case.method == "evaluate_exit")

    def denied(*args: object, **kwargs: object) -> object:
        raise AssertionError("candidate source execution/import is forbidden")

    monkeypatch.setattr(builtins, "exec", denied)
    monkeypatch.setattr(importlib, "import_module", denied)
    decision = registry_decision_v1(
        registry=registry,
        configuration_id="A",
        method=case.method,
        snapshot=case.snapshot,
    )
    assert decision is not None


def test_synthetic_budget_and_fresh_fixture_reopen(tmp_path: Path) -> None:
    registry = build_study_registry_v1()
    budget = study_resource_budget_v1()
    assert budget.max_cases == 8
    assert budget.max_repetitions == 2
    assert budget.timeout_ms == 1000
    assert budget.cpu_seconds == Decimal("1")
    assert budget.memory_mib == 128
    assert budget.output_bytes == 65536

    fixture = create_study_fixture_v1(root=tmp_path / "fresh", registry=registry)
    assert fixture.resource_budget == budget
    assert fixture.manifest.manifest.provider is None
    assert fixture.manifest.manifest.pit_data_scope == "production"
    assert fixture.manifest.manifest.search.hypotheses_per_investigator == 1
    assert fixture.manifest.manifest.search.max_variants_per_template == 2
    assert fixture.manifest.manifest.search.max_discovery_survivors_per_template == 2
    assert fixture.manifest.manifest.search.archive_capacity == 1
    assert fixture.manifest.manifest.search.max_feedback_rounds == 2
    assert fixture.manifest.manifest.search.investigator_memory_max_bytes == 3072
    assert fixture.manifest.manifest.search == _study_search_capabilities()
    assert fixture.manifest.manifest.resources == _study_resource_capabilities()
    assert fixture.manifest.manifest.semantic_mode == "required"
    assert fixture.manifest.baseline_authority.semantic_mode == "required"
    assert fixture.manifest.baseline_authority.semantic_fingerprint.fingerprint_sha256 == registry.configuration("P0").fixed_suite_fingerprint
    assert tuple(item.configuration_id for item in fixture.synthetic_return_inputs) == tuple(
        item.configuration_id for item in registry.configurations
    )
    assert tuple(item.configuration_id for item in fixture.round_two_outcomes) == tuple(
        item.configuration_id for item in registry.configurations
    )
    assert not hasattr(fixture.round_two_outcomes[0], "supplied_decision_changed_count")
    assert tuple(item.ending_equity for item in fixture.round_two_outcomes[:3]) == (
        Decimal("100"),
        Decimal("101"),
        Decimal("102"),
    )
    assert (
        fixture.round_two_outcomes[0].ending_equity
        < fixture.round_two_outcomes[1].ending_equity
        < fixture.round_two_outcomes[2].ending_equity
    )
    for configuration in registry.configurations:
        round_one, round_two = configuration.portfolio_evaluator_inputs
        assert round_one.round_index == 1
        assert round_two.round_index == 2
        assert round_one != round_two
        assert round_one.ending_equity == round_two.ending_equity
    for item in fixture.round_two_outcomes:
        assert item.base_annualized_return_pct == annualized_return_pct(
            starting_equity=item.starting_equity,
            ending_equity=item.ending_equity,
            days=item.days,
        )

    reopened = reopen_study_fixture_v1(
        root=tmp_path / "fresh",
        manifest_ref=fixture.manifest.manifest_ref,
        registry=registry,
    )
    assert reopened.manifest == fixture.manifest
    assert reopened.repository.root == fixture.repository.root
    assert reopened.resource_budget == fixture.resource_budget == budget
    assert reopened.manifest.manifest.search == _study_search_capabilities()
    assert reopened.manifest.manifest.resources == _study_resource_capabilities()


def test_fixture_rejects_changed_search_and_resource_authorities_on_reopen(tmp_path: Path) -> None:
    registry = build_study_registry_v1()
    altered_search_root = tmp_path / "altered-search"
    altered_search_root.mkdir()
    altered_search = replace(_study_search_capabilities(), investigator_memory_max_bytes=4096)
    altered_search_manifest = _build_manifest(
        repository=LocalArtifactRepositoryV5(altered_search_root),
        registry=registry,
        search=altered_search,
    )
    before_search = tuple(
        (path.relative_to(altered_search_root).as_posix(), path.read_bytes())
        for path in altered_search_root.rglob("*")
        if path.is_file()
    )
    with pytest.raises(ValueError, match="authority"):
        reopen_study_fixture_v1(
            root=altered_search_root,
            manifest_ref=altered_search_manifest.manifest_ref,
            registry=registry,
        )
    after_search = tuple(
        (path.relative_to(altered_search_root).as_posix(), path.read_bytes())
        for path in altered_search_root.rglob("*")
        if path.is_file()
    )
    assert after_search == before_search

    altered_resource_root = tmp_path / "altered-resource"
    altered_resource_root.mkdir()
    altered_resources = replace(_study_resource_capabilities(), campaign_wall_timeout_seconds=241)
    altered_resource_manifest = _build_manifest(
        repository=LocalArtifactRepositoryV5(altered_resource_root),
        registry=registry,
        resources=altered_resources,
    )
    with pytest.raises(ValueError, match="authority"):
        reopen_study_fixture_v1(
            root=altered_resource_root,
            manifest_ref=altered_resource_manifest.manifest_ref,
            registry=registry,
        )


def test_fixture_rejects_nonempty_missing_and_corrupt_roots_without_repair(tmp_path: Path) -> None:
    registry = build_study_registry_v1()
    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    sentinel = nonempty / "sentinel.txt"
    sentinel.write_bytes(b"keep")
    with pytest.raises(ValueError, match="fresh empty"):
        create_study_fixture_v1(root=nonempty, registry=registry)
    assert sentinel.read_bytes() == b"keep"

    missing_root = tmp_path / "missing"
    with pytest.raises(ValueError, match="root"):
        reopen_study_fixture_v1(
            root=missing_root,
            manifest_ref=ArtifactRefV5("evaluator/study-manifest.json", "0" * 64),
            registry=registry,
        )
    assert not missing_root.exists()

    missing_fixture = create_study_fixture_v1(root=tmp_path / "missing-artifact", registry=registry)
    missing_manifest_path = missing_fixture.repository.root / missing_fixture.manifest_ref.relative_path
    missing_manifest_path.unlink()
    with pytest.raises(ValueError):
        reopen_study_fixture_v1(
            root=missing_fixture.repository.root,
            manifest_ref=missing_fixture.manifest_ref,
            registry=registry,
        )
    assert not missing_manifest_path.exists()

    fixture = create_study_fixture_v1(root=tmp_path / "corrupt", registry=registry)
    manifest_path = fixture.repository.root / fixture.manifest_ref.relative_path
    original = manifest_path.read_bytes()
    manifest_path.write_bytes(b"corrupt")
    with pytest.raises(ValueError):
        reopen_study_fixture_v1(
            root=fixture.repository.root,
            manifest_ref=fixture.manifest_ref,
            registry=registry,
        )
    assert manifest_path.read_bytes() == b"corrupt"
    assert original != b"corrupt"


def test_fixture_calendar_and_portfolio_inputs_match_the_frozen_runtime(tmp_path: Path) -> None:
    fixture = create_study_fixture_v1(root=tmp_path / "calendar-fixture")
    plan = fixture.manifest.panel_plan
    episodes = (plan.mechanics, plan.quick, *plan.discovery)
    for episode in episodes:
        assert (date.fromisoformat(episode.end_date) - date.fromisoformat(episode.start_date)).days == 365
    discovery_intervals = tuple(
        (date.fromisoformat(episode.start_date), date.fromisoformat(episode.end_date))
        for episode in plan.discovery
    )
    assert all(left[1] <= right[0] for left, right in pairwise(discovery_intervals))

    registry = build_study_registry_v1()
    for p0 in registry.configuration("P0").portfolio_evaluator_inputs:
        a = next(item for item in registry.configuration("A").portfolio_evaluator_inputs if item.round_index == p0.round_index)
        s = next(item for item in registry.configuration("S").portfolio_evaluator_inputs if item.round_index == p0.round_index)
        assert p0.days == a.days == s.days == 365
        assert p0.ending_equity < a.ending_equity < s.ending_equity
        assert p0.base_annualized_return_pct < a.base_annualized_return_pct < s.base_annualized_return_pct
