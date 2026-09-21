"""Explicit offline and read-only study commands.

There is intentionally no default live command.  A live caller must supply a
current grant, an admitted gateway and an explicit human review through the
Python API described in the example documentation.
"""

from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path
import sys
from typing import Any

from core.pit_optimizer_v5.contracts import HypothesisV5, InvestigatorArtifactV5, MetricPredictionV5
from core.pit_optimizer_v5.mechanism_contracts import (
    MechanismDisconfirmingObservationV1,
    MechanismMetricSpecV1,
    MechanismPredicateV1,
    MechanismRecipeV1,
)
from .contracts import ExperimentDraftV1, RivalPatternV1, StudyResponseV1


def _offline_ledger(prepared) -> Any:
    from .ledger import StudyLedgerV1
    from .live_calls import StudyGrantV1, authorize_offline_fixture_v1
    from .store import StudyStoreV1

    store = StudyStoreV1(prepared.store_repository())
    grant = StudyGrantV1(
        study_id=prepared.manifest.study_id,
        manifest_sha256=prepared.manifest.sha256,
        repository_root_identity_sha256=store.repository.root_identity_sha256,
        audit_domain="task8-offline-cli",
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
        operator_approval_reference="offline-fixture:task8-cli",
    )
    approval = authorize_offline_fixture_v1(
        store=store,
        manifest=prepared.manifest,
        grant=grant,
        approval_reference=grant.operator_approval_reference,
    )
    return StudyLedgerV1(store, prepared.manifest, grant, approval)


def _offline_response(request) -> str:
    evidence_ids = tuple(item.evidence_id for item in request.role_evidence.items)
    if not evidence_ids:
        raise RuntimeError("offline round-two request contains no evidence IDs")
    hypothesis_id = "hyp.task8.offline"
    hypothesis = HypothesisV5(
        hypothesis_id=hypothesis_id,
        rank=1,
        primary_mechanism="exit",
        causal_claim="A registered exit threshold changes applicable decisions while preserving the protected control.",
        predicted_changes=(
            MetricPredictionV5("exit.decision_changed_count", "increase", "Applicable cases change."),
            MetricPredictionV5("exit.protected_control_unchanged_count", "unchanged", "The protected control holds."),
        ),
        evidence_ids=evidence_ids[:1],
        author_instructions="Use the registered exit mechanism recipe.",
        authoring_mode="symbol_edits",
    )
    draft = ExperimentDraftV1(
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
                "exit.decision_changed_count", "count", "increase", Decimal("0"), "relevant_cases"
            ),
            MechanismMetricSpecV1(
                "exit.protected_control_unchanged_count", "count", "unchanged", Decimal("0"), "control_cases"
            ),
        ),
        disconfirming_observations=(
            MechanismDisconfirmingObservationV1("protected_control_changed", "exit.protected_control_unchanged_count"),
        ),
        expected_changed=(False, True, True, False),
        rivals=(
            RivalPatternV1("inert", (False, False, False, False)),
            RivalPatternV1("always_on", (True, True, True, True)),
        ),
        configuration_id="S-gte-0.05",
        claim_kind="threshold",
    )
    return StudyResponseV1(
        1,
        request.expected_binding,
        InvestigatorArtifactV5((hypothesis,)),
        (draft,),
    ).canonical_bytes().decode("utf-8")


class _OfflineGateway:
    def __init__(self, responses: dict[str, str]) -> None:
        self.responses = dict(responses)
        self.calls: list[str] = []

    def invoke_json_once(self, *, request_sha256: str, model: str, **kwargs: object) -> Any:
        from core.pit_optimizer_v5.provider import CompletionResultV5

        self.calls.append(request_sha256)
        try:
            response = self.responses[request_sha256]
        except KeyError as exc:
            raise RuntimeError("offline gateway received an unregistered request") from exc
        return CompletionResultV5(
            response_text=response,
            accepted=True,
            input_tokens=1,
            output_tokens=1,
            provider_request_id=f"offline-cli-{len(self.calls)}",
            returned_model=model,
            cost_usd=Decimal("0"),
            external_attempt_count=1,
            response_received=True,
        )


def _open_readonly(root: Path):
    from .ledger import StudyLedgerV1
    from .live_calls import StudyGrantV1
    from .store import StudyStoreV1
    from .verification import load_prepared_study_v1

    prepared = load_prepared_study_v1(root=root)
    store = StudyStoreV1(prepared.store_repository())
    grant_refs = store.list_refs(kind="grants")
    if len(grant_refs) > 1:
        raise RuntimeError("study has duplicate grants")
    ledger = None
    if grant_refs:
        grant = StudyGrantV1.from_canonical_json(store.read(grant_refs[0]))
        ledger = StudyLedgerV1(store, prepared.manifest, grant, approval=None)
    return prepared, store, ledger


def _run_offline(root: Path) -> dict[str, object]:
    from .driver import execute_study_arm_v1, prepare_two_round_study_v1
    from .live_calls import authenticate_fixture_preflight_v1
    from .verification import verify_study_v1

    if not root.is_absolute():
        raise RuntimeError("offline root must be an absolute path")
    prepared = prepare_two_round_study_v1(root=root, mode="offline_fixture", provider_settings=None)
    ledger = _offline_ledger(prepared)
    responses = {}
    for arm in ("primary", "withheld"):
        request = authenticate_fixture_preflight_v1(prepared.preflight_for(arm), require_current=True)
        responses[prepared.live_call_for(arm).sha256] = _offline_response(request)
    gateway = _OfflineGateway(responses)
    results = tuple(
        execute_study_arm_v1(prepared=prepared, arm=arm, ledger=ledger, gateway=gateway)
        for arm in ("primary", "withheld")
    )
    reopened, store, readonly_ledger = _open_readonly(root)
    verification = verify_study_v1(prepared=reopened, store=store, ledger=readonly_ledger)
    return {
        "command": "offline",
        "root": str(root),
        "arms": [
            {"arm": item.arm, "state": item.state, "failure_reason": item.failure_reason}
            for item in results
        ],
        "verdicts": verification.verdicts.to_primitive(),
        "gateway_calls": len(gateway.calls),
        "synthetic_provider_calls": 0,
    }


def _run_verify(root: Path) -> dict[str, object]:
    from .verification import verify_study_v1

    prepared, store, ledger = _open_readonly(root)
    verification = verify_study_v1(prepared=prepared, store=store, ledger=ledger)
    return {
        "command": "verify",
        "root": str(root),
        "verdicts": verification.verdicts.to_primitive(),
        "arms": [item.to_primitive() for item in verification.arms],
        "artifact_refs": [item.to_primitive() for item in verification.artifact_refs],
    }


def _run_export(root: Path, output: Path) -> dict[str, object]:
    from .trace import export_study_trace_v1
    from .verification import verify_study_v1

    prepared, store, ledger = _open_readonly(root)
    verification = verify_study_v1(prepared=prepared, store=store, ledger=ledger)
    index = export_study_trace_v1(prepared=prepared, verification=verification, store=store, output=output)
    return {
        "command": "export",
        "root": str(root),
        "output": str(output),
        "index": index.to_primitive(),
        "verdicts": verification.verdicts.to_primitive(),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="V5 two-round study verification")
    subparsers = parser.add_subparsers(dest="command", required=True)
    offline = subparsers.add_parser("offline", help="create and execute a local synthetic study")
    offline.add_argument("--root", required=True, type=Path)
    verify = subparsers.add_parser("verify", help="read-only verify a persisted study")
    verify.add_argument("--root", required=True, type=Path)
    export = subparsers.add_parser("export", help="read-only verify and export exact bytes")
    export.add_argument("--root", required=True, type=Path)
    export.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "offline":
            result = _run_offline(args.root.absolute())
        elif args.command == "verify":
            result = _run_verify(args.root.absolute())
        else:
            result = _run_export(args.root.absolute(), args.output.absolute())
    except Exception as exc:  # noqa: BLE001 - CLI reports a single closed failure
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
