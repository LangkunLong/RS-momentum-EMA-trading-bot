"""Controlled V3 inputs shared by the combined policy integration tests."""

from datetime import date, datetime, timezone

from core.current_policy_inputs import build_current_feature_context_snapshot
from core.pit_data import PITDataBundle
from core.policy_execution_state import DecisionClock, PolicyDeploymentIdentity
from tests.test_current_feature_context_adapter import (
    _decision_clock, _fixture, _market_inputs,
    _unavailable_active_members, _write_schema_v3_bundle,
)


def build_feature_fixture(tmp_path, *, rs_delta=0):
    fixture = _fixture()
    next_session = date.fromisoformat(fixture["next_eligible_session"])
    fixture["valuation_time"] = datetime.combine(
        next_session, datetime.min.time(), tzinfo=timezone.utc
    ).replace(hour=13, minute=31).isoformat()
    path, digest, provenance, availability = _write_schema_v3_bundle(tmp_path)
    with PITDataBundle(path, expected_sha256=digest, prices_provenance=provenance) as bundle:
        histories, closes, active, rs = _market_inputs(bundle, fixture)
        rs = dict(rs)
        rs["AAA"] += rs_delta
        missingness = {
            symbol: {
                name: (
                    {"state": record["state"], "reason": "controlled V3 fixture has no dated sector taxonomy"}
                    if name == "sector_rs" else record
                )
                for name, record in records.items()
                if name != "industry_group_rs"
            }
            for symbol, records in fixture["missingness"].items()
        }
        return build_current_feature_context_snapshot(
            bundle=bundle,
            decision_clock=_decision_clock(fixture),
            candidate_symbols=tuple(fixture["candidate_symbols"]),
            price_history_by_symbol=histories,
            rs_snapshot=rs,
            market_closes=closes,
            oneil_regime=fixture["market"]["oneil_regime"],
            distribution_days=fixture["market"]["distribution_days"],
            follow_through=fixture["market"]["follow_through"],
            missingness=missingness,
            fundamental_availability={"AAA": availability},
            unavailable_members=_unavailable_active_members(active, fixture),
            source_revision="ab385d792e19ff6db39d87f1123f47f660fc1e1d",
        )


def build_chain_identity(features):
    source_clock = features.decision_clock
    clock = DecisionClock(
        exchange_id="XNYS",
        decision_session=source_clock.completed_session,
        as_of_cutoff_at=source_clock.as_of_cutoff,
        next_execution_session=source_clock.next_eligible_session,
        account_valuation_session=source_clock.next_eligible_session,
        account_valuation_at=source_clock.valuation_time,
    )
    deployment = PolicyDeploymentIdentity(
        policy_artifact_id="fixed-policy:combined-offline-v1",
        capability_manifest_id="manifest:combined-offline-v1",
        policy_interface_version="3",
        feature_contract_id=features.feature_contract_id,
        feature_calculator_id=features.feature_calculator_identity,
        source_revision=features.source_revision,
        runtime_identity="offline-test-only",
        execution_profile_id="synthetic-paper-profile",
        paper_account_environment_id="synthetic-chain-account",
        store_identity="synthetic-chain-store",
    )
    return deployment, clock
