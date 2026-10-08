"""Prepare one provider-free R1 controller proposal from an issued V5 request.

This controller-side tool never evaluates policy code. Round-two risk sizing is
selected by a declared branch on the authenticated first candidate's measured
campaign CAGR, including when the archive kept the baseline as parent.
"""

from __future__ import annotations

import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sys

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from core.pit_optimizer_v5.artifacts import LocalArtifactRepositoryV5
from core.pit_optimizer_v5.candidate_ir import LiteralAxisV5, SourceBundleV5, SourceOperationV5, StructuralTemplateV5
from core.pit_optimizer_v5.contracts import (
    ArtifactRefV5,
    CriticArtifactV5,
    CriticReviewV5,
    HypothesisV5,
    InvestigatorArtifactV5,
    MetricPredictionV5,
)
from core.pit_optimizer_v5.manifest import authenticate_campaign_manifest_v5
from core.pit_optimizer_v5.memory import (
    CleanupResultPayloadV5,
    EpisodeEvidencePayloadV5,
    QuickEvidencePayloadV5,
)
from core.pit_optimizer_v5.production_fs import (
    acquire_absolute_directory_v5,
    read_regular_in_directory_v5,
    write_new_regular_in_directory_v5,
)
from core.pit_optimizer_v5.provider import parse_and_bind_role_artifact, parsed_role_artifact_primitive_v5
from core.pit_optimizer_v5.search import verified_campaign_cagr_pct


_RISK_PATH = "core/strategy_policy/v3/risk.py"
_RISK_SYMBOL = "core.strategy_policy.v3.risk.recommend_allocation"
_MEASURED_METRIC = "prior_experiment.campaign_cagr_pct"
_REPLACEMENT = '''def recommend_allocation(snapshot: AllocationSnapshotV3) -> AllocationDecision:
    """Scale the baseline allocation risk while retaining its stop and cap."""
    baseline = _recommend_allocation_v2(snapshot.base)
    return AllocationDecision(
        risk_fraction=baseline.risk_fraction * PIT_AXIS("risk_multiplier"),
        stop_distance_fraction=baseline.stop_distance_fraction,
        notional_fraction_cap=baseline.notional_fraction_cap,
    )
'''


def _round_two_measurement(repository, authenticated, experiment_id: str):
    checkpoint = repository.load_checkpoint()
    if checkpoint is None:
        raise ValueError("round two has no persisted checkpoint")
    matches = tuple(
        (reference, record)
        for reference in checkpoint.record_refs
        for record in (repository.load_experiment(reference),)
        if record.experiment_id == experiment_id
    )
    if len(matches) != 1:
        raise ValueError("round-two feedback experiment is absent or ambiguous")
    reference, record = matches[0]
    if (
        record.round_index != 1
        or record.status not in {"evaluated", "zero_trade"}
        or record.campaign_evidence is None
        or record.policy_revision is None
        or record.pit_data_scope != "engineering_v3"
        or record.semantic_mode != "required"
        or record.experiment_identity.discovery_plan_sha256
        != authenticated.panel_plan.discovery_plan_sha256
    ):
        raise ValueError("round-two feedback is not the measured first candidate")
    events = repository.load_round_events(
        campaign_id=authenticated.manifest.campaign_id, round_index=1
    )
    payloads = tuple(
        repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
        for event in events
    )
    episode_payloads = tuple(
        payload for event, payload in zip(events, payloads, strict=True)
        if event.experiment_id == experiment_id and type(payload) is EpisodeEvidencePayloadV5
    )
    quick_payloads = tuple(
        payload for event, payload in zip(events, payloads, strict=True)
        if event.experiment_id == experiment_id and type(payload) is QuickEvidencePayloadV5
    )
    cleanup = tuple(payload for payload in payloads if type(payload) is CleanupResultPayloadV5)
    if (
        len(episode_payloads) != 4
        or tuple(sorted(
            (payload.episode for payload in episode_payloads),
            key=lambda episode: episode.episode_ordinal,
        ))
        != record.campaign_evidence.episodes
        or len(quick_payloads) != 1
        or quick_payloads[0].evaluation != record.quick_evidence
        or len(cleanup) != 1
        or not cleanup[0].cleanup_complete
    ):
        raise ValueError("measured feedback is outside the completed current campaign")
    source = repository.load_typed_state(
        namespace="policy-source",
        key=record.policy_revision.sha256,
        value_type=SourceBundleV5,
        repair=False,
    )
    if source is None:
        raise ValueError("round-two candidate source is unavailable")
    source_ref = ArtifactRefV5(
        f"adapter-state/policy-source/{record.policy_revision.sha256}.json", source.sha256
    )
    if (
        source_ref not in record.artifact_refs
        or tuple((item.path, item.sha256) for item in source.files)
        != record.policy_revision.editable_source_sha256
    ):
        raise ValueError("round-two candidate source differs from its measured revision")
    value = verified_campaign_cagr_pct(
        campaign=record.campaign_evidence,
        discovery_plan=authenticated.panel_plan,
        evaluator_contract=authenticated.evaluator_contract,
        policy_identity_sha256=record.policy_revision.sha256,
    )
    return reference, record, value


def _branch(round_index: int, measured_value: Decimal | None) -> tuple[str, float]:
    if round_index == 1:
        return "first", 0.5
    if type(measured_value) is not Decimal:
        raise ValueError("round two requires a measured Decimal CAGR")
    return ("positive", 0.75) if measured_value > 0 else ("nonpositive", 0.25)


def _create_or_match(directory, name: str, content: bytes) -> None:
    """Finish an interrupted two-file proposal only when retained bytes match."""
    try:
        existing, _ = read_regular_in_directory_v5(
            directory, name, maximum_bytes=max(1, len(content))
        )
    except FileNotFoundError:
        try:
            write_new_regular_in_directory_v5(directory, name, content)
            return
        except FileExistsError:
            existing, _ = read_regular_in_directory_v5(
                directory, name, maximum_bytes=max(1, len(content))
            )
    if existing != content:
        raise ValueError("retained controlled proposal differs from the exact request")


def prepare_controlled_proposal_v1(
    *,
    repository: LocalArtifactRepositoryV5,
    manifest_ref: ArtifactRefV5,
    request_ref: ArtifactRefV5,
    output_directory: Path,
    feedback_experiment_id: str | None,
    expected_source_commit: str,
) -> dict[str, object]:
    """Create a bound proposal and branch receipt; leave actual sealing separate."""
    if type(expected_source_commit) is not str or re.fullmatch(r"[0-9a-f]{40}", expected_source_commit) is None:
        raise ValueError("expected source commit must be a full lowercase Git SHA")
    authenticated = authenticate_campaign_manifest_v5(repository=repository, manifest_ref=manifest_ref)
    manifest = authenticated.manifest
    if (
        manifest.source_commit != expected_source_commit
        or manifest.pit_data_scope != "engineering_v3"
        or manifest.semantic_mode != "required"
        or manifest.provider is not None
        or manifest.search.max_feedback_rounds != 2
        or manifest.search.hypotheses_per_investigator != 1
        or manifest.search.max_variants_per_template != 1
        or manifest.search.allow_full_source_escape
    ):
        raise ValueError("controlled response requires the exact finite R1 scope")
    call, request = repository._load_role_request_entry(request_ref)
    if (
        call.campaign_id != manifest.campaign_id
        or call.round_index not in (1, 2)
        or call.attempt_kind != "primary"
        or call.attempt_index != 1
        or request.role != call.role
    ):
        raise ValueError("issued role request differs from the R1 campaign")
    if (call.round_index == 1) != (feedback_experiment_id is None):
        raise ValueError("feedback experiment ID must occur exactly in round two")
    measured_ref = None
    measured_value = None
    if feedback_experiment_id is not None:
        measured_ref, _record, measured_value = _round_two_measurement(
            repository, authenticated, feedback_experiment_id
        )
    branch, multiplier = _branch(call.round_index, measured_value)
    hypothesis_id = f"r1-controlled-risk-{branch}"
    evidence_items = request.role_evidence.items
    if not evidence_items:
        raise ValueError("controlled response has no issued role evidence")
    issued_id = evidence_items[0].evidence_id
    if call.round_index == 2 and request.role in {"investigator", "author"}:
        measured = tuple(item for item in evidence_items if item.metric_id == _MEASURED_METRIC)
        if len(measured) != 1 or measured[0].value != measured_value:
            raise ValueError("issued round-two evidence differs from the measured candidate")
        description = measured[0].description or ""
        if (
            f"experiment_id {feedback_experiment_id};" not in description
            or f"record_sha256 {measured_ref.sha256};" not in description
        ):
            raise ValueError("issued round-two evidence lacks the exact measured identity")
        issued_id = measured[0].evidence_id

    if request.role == "investigator":
        artifact = InvestigatorArtifactV5((
            HypothesisV5(
                hypothesis_id=hypothesis_id,
                rank=1,
                primary_mechanism="risk_sizing",
                causal_claim=(
                    "The first candidate precommits to half of baseline allocation risk."
                    if call.round_index == 1
                    else f"Controlled risk multiplier {multiplier} follows branch {branch} "
                    "of the measured first-candidate CAGR."
                ),
                predicted_changes=(MetricPredictionV5(
                    "average_exposure_pct",
                    "increase" if branch == "positive" else "decrease",
                    "Risk scaling may change realized exposure; the evaluator determines the outcome.",
                ),),
                evidence_ids=(issued_id,),
                author_instructions=(
                    f"Replace only V3 recommend_allocation with risk multiplier {multiplier}; "
                    "retain the baseline stop distance and notional cap."
                ),
            ),
        ))
    elif request.role == "author":
        hypothesis = request.role_input.hypothesis
        if hypothesis.hypothesis_id != hypothesis_id or issued_id not in hypothesis.evidence_ids:
            raise ValueError("author request differs from the measured branch hypothesis")
        if _RISK_PATH not in {item.path for item in request.role_input.editable_sources}:
            raise ValueError("author request lacks the authenticated risk source")
        artifact = StructuralTemplateV5(
            hypothesis_id,
            request.expected_binding.parent_revision_sha256,
            (_RISK_SYMBOL,),
            (SourceOperationV5(_RISK_PATH, "recommend_allocation", "replace_function", _REPLACEMENT),),
            (LiteralAxisV5("risk_multiplier", multiplier, (multiplier,)),),
            None,
        )
    elif request.role == "critic":
        artifact = CriticArtifactV5(
            tuple(CriticReviewV5(
                experiment_id,
                "Compare the declared risk change with authenticated evaluator evidence.",
                "A controlled engineering outcome does not establish production improvement.",
                (issued_id,),
                "refine",
                "Retain the measured result and do not promote from this engineering fixture.",
            ) for experiment_id in request.expected_binding.experiment_ids),
            "The controlled engineering measurements remain scoped to the synthetic D1 fixture.",
            "Continue only with eligible historical inputs and separate stage authority.",
            (issued_id,),
        )
    else:
        raise ValueError("R1 controller role is unsupported")
    proposal = parsed_role_artifact_primitive_v5(artifact)
    response = {"binding": request.expected_binding.to_primitive(), "artifact": proposal["artifact"]}
    parse_and_bind_role_artifact(
        request=request,
        response_text=json.dumps(response, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
    )
    directory = Path(output_directory)
    if not directory.is_absolute() or not directory.is_dir() or directory.is_symlink():
        raise ValueError("proposal output directory must be an absolute regular directory")
    if not directory.resolve(strict=True).is_relative_to(repository.root.resolve(strict=True)):
        raise ValueError("proposal output must remain inside the engineering artifact root")
    payload = json.dumps(proposal, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    receipt = {
        "schema_version": 1,
        "scope": "engineering_v3_controlled_response",
        "manifest_sha256": manifest_ref.sha256,
        "expected_source_commit": expected_source_commit,
        "request_sha256": request.sha256,
        "request_ref": request_ref.to_primitive(),
        "call_key_sha256": call.sha256,
        "round_index": call.round_index,
        "role": request.role,
        "branch": branch,
        "rule": "first=0.5; first-candidate campaign CAGR > 0 => 0.75; otherwise 0.25",
        "risk_multiplier": multiplier,
        "feedback_experiment_id": feedback_experiment_id,
        "measured_record_sha256": None if measured_ref is None else measured_ref.sha256,
        "measured_cagr_pct": None if measured_value is None else str(measured_value),
        "proposal_filename": call.sha256 + ".proposal.json",
        "proposal_sha256": hashlib.sha256(payload).hexdigest(),
    }
    receipt_bytes = json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    with acquire_absolute_directory_v5(directory) as handle:
        _create_or_match(handle, call.sha256 + ".decision.json", receipt_bytes)
        _create_or_match(handle, call.sha256 + ".proposal.json", payload)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--manifest-path", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--request-path", required=True)
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--feedback-experiment-id")
    parser.add_argument("--expected-source-commit", required=True)
    args = parser.parse_args()
    try:
        result = prepare_controlled_proposal_v1(
            repository=LocalArtifactRepositoryV5(args.artifact_root),
            manifest_ref=ArtifactRefV5(args.manifest_path, args.manifest_sha256),
            request_ref=ArtifactRefV5(args.request_path, args.request_sha256),
            output_directory=args.output_directory,
            feedback_experiment_id=args.feedback_experiment_id,
            expected_source_commit=args.expected_source_commit,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"controlled proposal rejected: {type(exc).__name__}: {str(exc)[:512]}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
