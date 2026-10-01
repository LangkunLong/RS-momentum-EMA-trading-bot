"""Offline cross-adapter identity evidence using actual #98 and #100 APIs."""

import pytest

from core.policy_execution_state import (
    DecisionCategory,
    DecisionConflictError,
    DecisionIdentity,
    DecisionSubjectType,
    assert_same_decision_slot_consistent,
)
from tests.paper_policy_fixtures import build_chain_identity, build_feature_fixture


def decision_for(features):
    deployment, clock = build_chain_identity(features)
    return DecisionIdentity.build(
        deployment=deployment,
        clock=clock,
        snapshot_sha256=features.recorded_input_manifest_sha256,
        category=DecisionCategory.ENTRY,
        subject_type=DecisionSubjectType.SECURITY,
        subject_id="fixture:AAA",
    )


def test_current_feature_identity_survives_decision_boundary(tmp_path):
    features = build_feature_fixture(tmp_path)
    decision = decision_for(features)
    assert decision == decision_for(features)
    assert decision.snapshot_sha256 == features.recorded_input_manifest_sha256
    assert decision.deployment_identity.feature_contract_id == features.feature_contract_id
    assert decision.deployment_identity.feature_calculator_id == features.feature_calculator_identity
    assert decision.deployment_identity.source_revision == features.source_revision
    assert decision.clock.decision_session == features.decision_clock.completed_session
    assert decision.clock.as_of_cutoff_at == features.decision_clock.as_of_cutoff
    assert decision.clock.next_execution_session == features.decision_clock.next_eligible_session
    assert decision.clock.account_valuation_at == features.decision_clock.valuation_time
    assert decision.clock.account_valuation_at > decision.clock.as_of_cutoff_at
    assert set(features.universe_ids) == {"sp500", "nasdaq100", "russell2000"}
    assert_same_decision_slot_consistent(decision, decision_for(features))


def test_changed_supplied_fact_conflicts_with_same_policy_decision_slot(tmp_path):
    original_path = tmp_path / "original"
    changed_path = tmp_path / "changed"
    original_path.mkdir()
    changed_path.mkdir()
    original = build_feature_fixture(original_path)
    changed = build_feature_fixture(changed_path, rs_delta=1)
    assert original.data_bundle_sha256 == changed.data_bundle_sha256
    assert original.recorded_input_manifest_sha256 != changed.recorded_input_manifest_sha256
    before, after = decision_for(original), decision_for(changed)
    assert before.decision_slot_id == after.decision_slot_id
    assert before.decision_id != after.decision_id
    with pytest.raises(DecisionConflictError, match="different snapshot identity"):
        assert_same_decision_slot_consistent(before, after)
