"""Create-only, exact-byte export for an authenticated two-round study.

The live study roots remain the authorities for path and device identity.  A
trace is a portable byte bundle: every original artifact keeps its original
bytes and hash, while the index, rubric result and readable Markdown are
separate derivative artifacts with their own hashes.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat

from core.pit_optimizer_v5.artifacts import ArtifactRefV5, LocalArtifactRepositoryV5
from core.pit_optimizer_v5.contracts import canonical_json_bytes_v5, canonical_primitive_v5

from .contracts import StudyAuthorityError, StudyContractError
from .driver import (
    PreparedStudyV1,
    _long_files,
    _long_exists,
    _long_path,
    _long_read_bytes,
)
from .fixtures import reopen_study_fixture_v1
from .store import StudyStoreV1
from .verification import StudyVerificationV1


_STORE_KINDS = (
    "manifests",
    "registry",
    "rubrics",
    "preflights",
    "schemas",
    "parsers",
    "prompts",
    "calls",
    "comparisons",
    "grants",
    "requests",
    "reservations",
    "dispatches",
    "responses",
    "raw-responses",
    "raw-response-failures",
    "reconciliations",
    "terminals",
    "parsed",
    "admission-rejections",
    "imports",
    "import-translations",
    "import-artifacts",
    "import-drafts",
    "commitments",
    "draft-bindings",
    "mechanism-specs",
    "mechanism-corpora",
    "contrasts",
    "mechanism-links",
)
_JSON_SUFFIXES = (".json", ".bin")
_MAX_FIXTURE_BYTES = 128 * 1024 * 1024
_REQUIRED_DIRECTORIES = (
    "live-study-calls",
    "imports",
    "behavior-registry",
    "round-1",
    "round-2-primary",
    "round-2-withheld",
    "case-contrasts",
)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _safe_component(value: str) -> str:
    if not value or value in {".", ".."} or any(char in value for char in "/\\:\x00"):
        raise StudyAuthorityError("trace output contains an unsafe path component")
    return value


def _safe_relative(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise StudyAuthorityError("trace artifact path escapes its output root")
    for part in path.parts:
        _safe_component(part)
    return path.as_posix()


def _write_new(output: Path, relative: str, raw: bytes) -> str:
    relative = _safe_relative(relative)
    target = output.joinpath(*PurePosixPath(relative).parts)
    if _long_exists(target):
        raise StudyAuthorityError(f"trace output path is already occupied: {relative}")
    _reject_reparse_components(target.parent)
    try:
        os.makedirs(_long_path(target.parent), exist_ok=True)
        # CREATE_NEW/``xb`` prevents a race from replacing an existing or
        # reparse-point target after the allowlist check.
        with open(_long_path(target), "xb") as handle:
            handle.write(raw)
    except FileExistsError as exc:
        raise StudyAuthorityError(f"trace output path is already occupied: {relative}") from exc
    except OSError as exc:
        raise StudyAuthorityError(f"trace output path could not be created: {relative}") from exc
    return _sha256(raw)


def _fresh_output(output: Path) -> Path:
    if not output.is_absolute():
        raise StudyAuthorityError("trace output must be an absolute path")
    if _long_exists(output):
        if not os.path.isdir(_long_path(output)) or output.is_symlink():
            raise StudyAuthorityError("trace output must be a fresh directory")
        with os.scandir(_long_path(output)) as entries:
            if any(True for _ in entries):
                raise StudyAuthorityError("trace output directory must be empty")
    else:
        os.makedirs(_long_path(output), exist_ok=False)
    return output


def _is_reparse_component(path: Path) -> bool:
    try:
        metadata = os.lstat(_long_path(path))
    except (FileNotFoundError, OSError):
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    return path.is_symlink() or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _reject_reparse_components(path: Path) -> None:
    """Reject symlink/junction ancestors before any output directory write."""

    absolute = Path(os.path.abspath(os.fspath(path)))
    for component in (absolute, *absolute.parents):
        if _long_exists(component) and _is_reparse_component(component):
            raise StudyAuthorityError("trace output path contains a symlink or reparse point")


def _reject_output_inside_input(prepared: PreparedStudyV1, output: Path) -> None:
    """Reject an export destination inside any original authority root."""

    try:
        _reject_reparse_components(output)
        roots = (
            prepared.root,
            prepared.store_root,
            prepared.ancestor_root,
            prepared.primary_root,
            prepared.withheld_root,
        )
        candidate = os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(output))))
        for root in roots:
            _reject_reparse_components(root)
            original = os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(root))))
            if os.path.commonpath((original, candidate)) == original:
                raise StudyAuthorityError("trace output must be outside the original study authority root")
    except StudyAuthorityError:
        raise
    except ValueError as exc:
        raise StudyAuthorityError("trace output path could not be compared with the study root") from exc


def _inventory(root: Path) -> tuple[tuple[str, str], ...]:
    return tuple((relative, _sha256(_long_read_bytes(path))) for relative, path in _long_files(root))


def _input_inventory(prepared: PreparedStudyV1) -> tuple[tuple[str, str, str], ...]:
    result: list[tuple[str, str, str]] = []
    for name, root in (
        ("round-one-ancestor", prepared.ancestor_root),
        ("study-store", prepared.store_root),
        ("primary-descendant", prepared.primary_root),
        ("withheld-descendant", prepared.withheld_root),
    ):
        result.extend((name, relative, digest) for relative, digest in _inventory(root))
    return tuple(result)


def _fixture_bytes(repository: LocalArtifactRepositoryV5, reference: ArtifactRefV5) -> bytes:
    """Read one named fixture edge without relocation search."""

    relative = reference.relative_path
    try:
        if relative.startswith("adapter-blobs/") and relative.endswith(".bin"):
            parts = relative.split("/")
            if len(parts) != 3:
                raise StudyAuthorityError("fixture binary edge has an invalid path")
            raw = repository.load_binary_state(
                namespace=parts[1],
                key=parts[2][:-4],
                reference=reference,
                maximum_bytes=_MAX_FIXTURE_BYTES,
            )
        elif relative.startswith("panels/"):
            # Panel and panel-plan files have two established encodings.  The
            # repository's named relative reader performs the same exact-path
            # and reparse checks without relocating or reserializing either.
            raw = repository._read_relative(relative)
        else:
            raw = repository.authenticate_exact(reference).content
    except Exception as exc:  # noqa: BLE001 - source authority is fail-closed
        raise StudyAuthorityError(f"fixture edge could not be authenticated: {relative}") from exc
    if _sha256(raw) != reference.sha256:
        raise StudyAuthorityError(f"fixture edge differs from its reference: {relative}")
    return raw


def _json_arm(raw: bytes) -> str | None:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if isinstance(value, dict) and value.get("arm") in {"primary", "withheld"}:
        return str(value["arm"])
    for key in ("request", "call", "record"):
        child = value.get(key) if isinstance(value, dict) else None
        if isinstance(child, dict) and child.get("arm") in {"primary", "withheld"}:
            return str(child["arm"])
    return None


def _store_destination(kind: str, reference: ArtifactRefV5, raw: bytes) -> str:
    parts = reference.relative_path.split("/")
    key = parts[-1][:-4] if parts and parts[-1].endswith(".bin") else parts[-1]
    arm = _json_arm(raw)
    if arm is None and key.startswith(("primary-", "withheld-")):
        arm = key.split("-", 1)[0]
    owner = arm or "shared"
    if kind == "manifests":
        return "study-manifest.json"
    if kind == "comparisons":
        return "request-comparison.json"
    if kind in {"imports", "import-translations", "import-artifacts", "import-drafts"}:
        return f"imports/{owner}/{kind}/{_safe_component(key)}.bin"
    if kind in {"commitments", "contrasts"}:
        return f"case-contrasts/{owner}/{kind}/{_safe_component(key)}.bin"
    if kind in {"calls", "requests", "reservations", "dispatches", "responses", "raw-responses", "raw-response-failures", "reconciliations", "terminals", "parsed", "admission-rejections"}:
        return f"live-study-calls/{owner}/{kind}/{_safe_component(key)}.bin"
    if kind in {"registry", "rubrics", "preflights", "schemas", "parsers", "prompts", "draft-bindings", "mechanism-specs", "mechanism-corpora", "mechanism-links"}:
        return f"behavior-registry/{kind}/{_safe_component(key)}.bin"
    return f"behavior-registry/store/{_safe_component(kind)}/{_safe_component(key)}.bin"


def _artifact_type(relative: str) -> str:
    if relative.endswith(".bin"):
        return "binary"
    if relative.endswith(".json"):
        return "canonical_json"
    return "bytes"


def _json_text(value: object) -> str:
    return canonical_json_bytes_v5(canonical_primitive_v5(value)).decode("utf-8")


def _raw_json_text(raw: bytes) -> str:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return f"<non-JSON bytes: {len(raw)} bytes, sha256={_sha256(raw)}>"
    return _json_text(value)


def _embedded_reference_pairs(raw: bytes) -> tuple[tuple[str, str], ...]:
    """Return exact ArtifactRef-shaped edges embedded in an original byte blob.

    The export index records a reference only when the pair can be resolved to
    an independently authenticated source authority.  A relative path alone
    is insufficient here because both descendant roots intentionally reuse
    names such as ``checkpoint.json`` and ``archive.json``.  The pair itself
    carries only path and digest; resolved values are labelled as authenticated
    content-match aliases; resolving them does not establish causal
    dependencies or owning authority edges.
    """

    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return ()
    found: set[tuple[str, str]] = set()

    def visit(item: object) -> None:
        if isinstance(item, dict):
            if set(item) == {"relative_path", "sha256"} and type(item.get("relative_path")) is str and type(item.get("sha256")) is str:
                found.add((item["relative_path"], item["sha256"]))
                return
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return tuple(sorted(found))


def _append_json_block(lines: list[str], title: str, value: object) -> None:
    lines.extend((f"#### {title}", "", "```json", _json_text(value), "```", ""))


def _record_trace_primitive(record: object) -> dict[str, object]:
    policy = getattr(record, "policy_revision", None)
    source = getattr(record, "template", None)
    hypothesis = getattr(record, "hypothesis", None)
    return {
        "experiment_id": getattr(record, "experiment_id", None),
        "round_index": getattr(record, "round_index", None),
        "status": getattr(record, "status", None),
        "parent_revision_sha256": getattr(record, "parent_revision_sha256", None),
        "policy_revision_sha256": None if policy is None else policy.sha256,
        "hypothesis": canonical_primitive_v5(hypothesis),
        "predicted_changes": canonical_primitive_v5(getattr(hypothesis, "predicted_changes", ())),
        "template": canonical_primitive_v5(source),
        "validation": canonical_primitive_v5(getattr(record, "validation", None)),
        "quick_evidence": canonical_primitive_v5(getattr(record, "quick_evidence", None)),
        "target_gap_pct": canonical_primitive_v5(getattr(record, "target_gap_pct", None)),
    }


def _fixture_trace_material(prepared: PreparedStudyV1, arm: str) -> dict[str, object]:
    """Read exact persisted round and mechanism values for the readable derivative."""

    from core.pit_optimizer_v5.mechanism_artifacts import MechanismArtifactRepositoryV5, manifest_source_identity_sha256_v1
    from core.pit_optimizer_v5.memory import RoleCompletionPayloadV5, StoredExperimentRecordV5

    fixture = reopen_study_fixture_v1(
        root=prepared.fixture_path(arm),
        manifest_ref=prepared.primary_manifest_ref if arm == "primary" else prepared.withheld_manifest_ref,
        registry=prepared.registry,
    )
    checkpoint = fixture.repository.load_checkpoint()
    records: list[dict[str, object]] = []
    mechanisms: list[dict[str, object]] = []
    if checkpoint is not None:
        mechanism = MechanismArtifactRepositoryV5(fixture.repository)
        source_identity = manifest_source_identity_sha256_v1(fixture.manifest)
        for record_ref in checkpoint.record_refs:
            record = fixture.repository.load_experiment(record_ref)
            records.append(_record_trace_primitive(record))
            if record.round_index != 2:
                continue
            persisted = mechanism.load_existing_evidence_for_record(
                campaign_id=fixture.manifest.manifest.campaign_id,
                round_index=record.round_index,
                manifest_ref=fixture.manifest_ref,
                manifest_source_identity_sha256=source_identity,
                stored_record=StoredExperimentRecordV5(record_ref, record),
            )
            if persisted is None:
                continue
            evidence, _intent = persisted
            run = evidence.run
            report = evidence.report
            mechanisms.append(
                {
                    "experiment_id": record.experiment_id,
                    "spec": canonical_primitive_v5(evidence.spec),
                    "binding": canonical_primitive_v5(evidence.binding),
                    "corpus": canonical_primitive_v5(evidence.corpus),
                    "execution": canonical_primitive_v5(run.execution),
                    "coverage": canonical_primitive_v5(run.coverage),
                    "repetitions": run.repetitions,
                    "reset_semantics": run.reset_semantics,
                    "resource_budget": canonical_primitive_v5(evidence.binding.resource_budget),
                    "predictions": canonical_primitive_v5(report.predictions),
                    "limitations": list(run.limitations) + list(report.limitations),
                    "observations": [
                        {
                            "case_order": observation.case_order,
                            "repetition": observation.repetition,
                            "input_value": canonical_primitive_v5(observation.input_value),
                            "applicable": observation.applicable,
                            "decision_changed": observation.decision_changed,
                            "protected_control_unchanged": observation.protected_control_unchanged,
                            "parent_decision_sha256": observation.parent_decision_sha256,
                            "candidate_decision_sha256": observation.candidate_decision_sha256,
                            "parent_decision": _raw_json_text(observation.parent_decision_json),
                            "candidate_decision": _raw_json_text(observation.candidate_decision_json),
                        }
                        for observation in run.observations
                    ],
                }
            )
    role_artifacts: dict[str, object] = {}
    events = fixture.repository.load_round_events(
        campaign_id=fixture.manifest.manifest.campaign_id,
        round_index=2,
    )
    for event in events:
        payload = fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
        if type(payload) is not RoleCompletionPayloadV5:
            continue
        package = fixture.repository.load_role_invocation(payload)
        role_artifacts[payload.role] = {
            "call": canonical_primitive_v5(package.call),
            "request_sha256": package.request.sha256,
            "attempt": canonical_primitive_v5(package.attempt),
            "terminal_authority": canonical_primitive_v5(package.terminal_authority),
            "artifact_sha256": package.attempt.artifact_sha256,
            "artifact": canonical_primitive_v5(package.artifact),
        }
    return {"records": records, "mechanisms": mechanisms, "role_artifacts": role_artifacts}


def _fresh_authenticated_verification(
    *,
    prepared: PreparedStudyV1,
    store: StudyStoreV1,
    supplied: StudyVerificationV1,
):
    """Recompute the verification from current authorities before writing."""

    from .ledger import StudyLedgerV1
    from .live_calls import StudyGrantV1
    from .verification import verify_study_v1

    grant_refs = tuple(store.list_refs(kind="grants"))
    if len(grant_refs) > 1:
        raise StudyAuthorityError("trace export found multiple persisted grants")
    ledger = None
    if grant_refs:
        grant = StudyGrantV1.from_canonical_json(store.read(grant_refs[0]))
        ledger = StudyLedgerV1(store, prepared.manifest, grant, None)
    fresh = verify_study_v1(
        prepared=prepared,
        store=store,
        ledger=ledger,
        human_review=supplied.human_review,
    )
    if fresh.canonical_bytes() != supplied.canonical_bytes():
        raise StudyAuthorityError("trace verification is stale or forged for the current authenticated graph")
    return fresh


def _trace_markdown(
    prepared: PreparedStudyV1,
    verification: StudyVerificationV1,
    entries: list[dict[str, object]],
    *,
    store: StudyStoreV1 | None = None,
) -> bytes:
    verdict = verification.verdicts
    is_offline = prepared.mode == "offline_fixture"
    mode_label = (
        "offline synthetic responses and fake-transport accounting"
        if is_offline
        else "live-generated responses and admitted provider accounting"
    )
    if is_offline:
        accounting_note = (
            "This export is an offline synthetic fixture replay: responses and fake-transport accounting are "
            "recorded test facts with zero provider cost or execution. Evidence use is **not_assessed**."
        )
    else:
        accounting_note = (
            "This export describes a live-generated study attempt and its admitted provider accounting. "
            "Verify/export performs no new dispatch and preserves recorded usage only."
        )
    lines = [
        "# V5 two-round study trace",
        "",
        f"This is an exact-byte portable trace of an authenticated study ({mode_label}).",
        "The original fixture and study-store roots remain the authorities for path, device and ledger identity; this export verifies portable bytes and provenance separately.",
        "",
        "## Verdict axes",
        "",
        f"- trace integrity: **{verdict.trace_integrity}**",
        f"- evidence delivery: **{verdict.evidence_delivery}**",
        f"- evidence use: **{verdict.evidence_use}**",
        f"- production assessment: **{verdict.production_assessment}**",
        f"- case contrast: **{verdict.case_contrast}**",
        f"- experiment completion: **{verdict.experiment_completion}**",
        f"- feedback attribution: **{verdict.feedback_attribution}**",
        f"- optimization improvement: **{verdict.optimization_improvement}**",
        f"- authored code execution: **{verdict.authored_code_execution}**",
        "",
        accounting_note,
        "Optimization improvement and authored-code execution remain **not_established** unless independently established by their separate admitted axes.",
        "",
        "## Arm measurements",
        "",
    ]
    materials = {arm: _fixture_trace_material(prepared, arm) for arm in ("primary", "withheld")}
    for arm in verification.arms:
        lines.extend(
            (
                f"### {arm.arm}",
                "",
                f"- state: **{arm.state}**",
                f"- round-two status: **{arm.round_two_status}**",
                f"- production assessment: **{arm.production_assessment}**",
                f"- case contrast: **{arm.case_contrast}**",
                f"- fixture root identity: `{arm.repository_root_identity_sha256}`",
                f"- evidence IDs: {', '.join(arm.evidence_ids) if arm.evidence_ids else 'unavailable'}",
                "",
            )
        )
        for measurement in arm.measurements:
            if "metric_id" in measurement:
                lines.append(
                    f"- `{measurement['metric_id']}`: {measurement['assessment']} (parent={measurement['parent_value']}, candidate={measurement['candidate_value']}, delta={measurement['paired_delta']}, denominator={measurement['denominator']})"
                )
            elif "contrast_status" in measurement:
                lines.append(
                    f"- typed contrast: {measurement['contrast_status']}; mismatches={measurement['mismatched_case_ids']}; missing={measurement['missing_case_ids']}"
                )
        lines.append("")
    lines.extend((
        "## Side-by-side generated role proposals",
        "",
        "The following canonical artifact blocks are the exact persisted investigator, author, and critic outputs for each arm. The two arms are shown separately so an equal artifact does not erase its authority identity.",
        "",
    ))
    roles = sorted(
        set(materials["primary"]["role_artifacts"]) | set(materials["withheld"]["role_artifacts"])
    )
    for role in roles:
        lines.extend((f"### {role}", ""))
        for arm in ("primary", "withheld"):
            artifact = materials[arm]["role_artifacts"].get(role)
            if artifact is None:
                lines.extend((f"#### {arm}", "", "`unavailable`", ""))
            else:
                _append_json_block(lines, f"{arm} exact persisted package", artifact)

    lines.extend((
        "## P0→A and P1→B proposals",
        "",
        "P0→A is the historical round-one proposal retained by the closed ancestor. P1→B is the round-two proposal retained by each descendant checkpoint; the selected parent, policy revision, and predictions remain arm-qualified.",
        "",
    ))
    for arm in ("primary", "withheld"):
        records = materials[arm]["records"]
        lines.extend((f"### {arm}", ""))
        for label, round_index in (("P0→A", 1), ("P1→B", 2)):
            matching = [item for item in records if item.get("round_index") == round_index]
            lines.extend((f"#### {label}", ""))
            if not matching:
                lines.extend(("`unavailable: phase was not persisted`", ""))
            else:
                _append_json_block(lines, f"{arm} {label} exact record summaries", matching)

    lines.extend((
        "## Raw mechanism case/control observations",
        "",
        "Each row retains the raw parent/candidate decision JSON and both decision hashes. Applicable-case changes and protected-control preservation are shown per case and repetition; report reductions remain separate from these raw observations.",
        "",
    ))
    for arm in ("primary", "withheld"):
        lines.extend((f"### {arm}", ""))
        for mechanism in materials[arm]["mechanisms"]:
            lines.extend((f"#### experiment `{mechanism['experiment_id']}`", ""))
            _append_json_block(lines, f"{arm} binding, corpus, budget, execution and report", {
                key: mechanism[key]
                for key in (
                    "binding",
                    "corpus",
                    "resource_budget",
                    "execution",
                    "coverage",
                    "repetitions",
                    "reset_semantics",
                    "predictions",
                    "limitations",
                )
            })
            _append_json_block(lines, f"{arm} raw case/control rows", mechanism["observations"])
        if not materials[arm]["mechanisms"]:
            lines.extend(("`unavailable: no authenticated round-two mechanism run`", ""))

    lines.extend((
        "## F/L request authorities and caps",
        "",
        "F is the fixture request authority and L is the separately authenticated live-study request. Byte sizes and transport caps are reported without copying or reserializing their authority bytes.",
        "",
    ))
    for arm in ("primary", "withheld"):
        preflight = prepared.preflight_for(arm)
        call = prepared.live_call_for(arm)
        call_ref = prepared.primary_live_call_ref if arm == "primary" else prepared.withheld_live_call_ref
        lines.extend((f"### {arm}", "", "| authority | reference/hash | messages bytes | schema bytes | output cap |", "| --- | --- | ---: | ---: | ---: |"))
        lines.append(
            f"| F fixture preflight | `{preflight.request_ref.relative_path}:{preflight.request_ref.sha256}` | {len(preflight.request_bytes)} | {len(preflight.schema_json)} | n/a |"
        )
        lines.append(
            f"| L live call | `{call_ref.relative_path}:{call_ref.sha256}` | {len(call.projected_wire_messages)} | {len(call.schema_json)} | {call.max_output_tokens} |"
        )
        lines.extend(
            (
                "",
                f"- F request identity: `{preflight.request_ref.sha256}`; F checkpoint/archive: `{preflight.checkpoint_ref.sha256}` / `{preflight.snapshot_ref.sha256}`",
                f"- L request identity: `{call.sha256}`; input bound bytes: **{call.input_bound_bytes}**; projected message/schema hashes: `{call.projected_wire_messages_sha256}` / `{call.projected_wire_schema_sha256}`",
                "",
            )
        )

    if store is not None:
        from .live_calls import StudyGrantV1

        grant_refs = tuple(store.list_refs(kind="grants"))
        if len(grant_refs) == 1:
            grant = StudyGrantV1.from_canonical_json(store.read(grant_refs[0]))
            lines.extend(
                (
                    "## Grant caps and attempt mode",
                    "",
                    f"- execution mode: **{'offline synthetic zero-cost fixture replay' if is_offline else 'live-generated provider attempt'}**",
                    f"- grant identity/root: `{grant_refs[0].sha256}` / `{grant.repository_root_identity_sha256}`",
                    f"- input token cap: **{grant.input_token_ceiling}**; output token cap: **{grant.output_token_ceiling}**; cumulative token cap: **{grant.cumulative_token_ceiling}**",
                    f"- per-call USD cap: **{grant.per_call_usd_ceiling}**; cumulative USD cap: **{grant.cumulative_usd_ceiling}**; per-call deadline seconds: **{grant.per_call_deadline_seconds}**",
                    "- verify/export preserves these caps and performs no new dispatch, retry, schema repair, or provider settlement.",
                    "",
                )
            )
        elif not grant_refs:
            lines.extend(
                (
                    "## Grant caps and attempt mode",
                    "",
                    "- execution mode: **preparation-only; no execution grant persisted**",
                    "- no provider attempt or cost is inferred from this trace.",
                    "",
                )
            )

    lines.extend((
        "## Admission, equivalence, failures, usage and recovery",
        "",
        f"- request comparison: **{prepared.request_comparison.status}**",
        f"- actual request differences: {list(prepared.request_comparison.actual_differences) or 'none'}",
        f"- unresolved audit channels: {list(prepared.request_comparison.unresolved_audit_channels) or 'none'}",
        f"- verification errors: {list(verification.errors) or 'none'}",
        f"- verification warnings: {list(verification.warnings) or 'none'}",
        f"- confounds: {list(verification.confounds) or 'none'}",
        "",
    ))
    if store is not None:
        counts = {kind: len(store.list_refs(kind=kind)) for kind in _STORE_KINDS}
        _append_json_block(lines, "authenticated study-store namespace counts", counts)
    for arm in verification.arms:
        lines.extend((f"### {arm.arm}", "", f"- state: **{arm.state}**", f"- errors: {list(arm.errors) or 'none'}", f"- recorded usage/accounting: `{_json_text(arm.usage)}`", ""))
    lines.extend(
        (
            "## Provenance and limits",
            "",
            "### Export provenance semantics",
            "",
            "- original artifact `upstream_refs` are authenticated content-match aliases: each value names a same-path, same-SHA-256 match under an authenticated root, but does not establish a causal dependency or owning authority edge.",
            "- derivative artifact `upstream_refs` are authenticated input references used to construct the derivative export files; they do not replace original root authority.",
            "- each original entry's `authority`, `source_relative_path`, and `source_sha256` retain the authenticated owning root, path, and bytes.",
            "",
            f"- study ID: `{prepared.manifest.study_id}`",
            f"- source revision: `{prepared.manifest.source_revision}`",
            f"- selected parent: `{prepared.selected_parent_configuration_id}`",
            "- original P0→A is retained in round-one evidence; actual P1→B is retained in each descendant graph.",
            f"- exported artifact entries: **{len(entries)}**",
            "- raw provider text, empty raw bytes, typed failures and incomplete states are preserved when present; no receipt is converted into a successful import.",
            "- this trace does not claim a fully working self-recursive optimizer or statistical reliability from a finite synthetic case lattice.",
            "",
        )
    )
    return "\n".join(lines).encode("utf-8")


def export_study_trace_v1(
    *,
    prepared: PreparedStudyV1,
    verification: StudyVerificationV1,
    store: StudyStoreV1,
    output: Path,
) -> ArtifactRefV5:
    """Export authenticated allowlisted bytes into a new empty directory."""

    if type(prepared) is not PreparedStudyV1 or type(verification) is not StudyVerificationV1 or type(store) is not StudyStoreV1:
        raise StudyContractError("trace export inputs are invalid")
    if verification.manifest_ref != prepared.manifest_ref:
        raise StudyAuthorityError("trace verification belongs to another study manifest")
    if store.repository.root_identity_sha256 != LocalArtifactRepositoryV5(prepared.store_root).root_identity_sha256:
        raise StudyAuthorityError("trace export store differs from the prepared study store")
    verification = _fresh_authenticated_verification(
        prepared=prepared,
        store=store,
        supplied=verification,
    )
    _reject_output_inside_input(prepared, Path(output))
    bound_refs = {(ref.relative_path, ref.sha256) for ref in verification.artifact_refs}
    before = _input_inventory(prepared)
    destination = _fresh_output(Path(output))
    for relative in _REQUIRED_DIRECTORIES:
        directory = destination / _safe_relative(relative)
        if _long_exists(directory):
            raise StudyAuthorityError(f"trace output path is already occupied: {relative}")
        os.makedirs(_long_path(directory), exist_ok=False)
    entries: list[dict[str, object]] = []
    occupied: set[str] = set()

    # Build root-qualified content-match aliases once from the same
    # authenticated graph that supplied ``bound_refs``.  Embedded ArtifactRef
    # values are the only identities admitted to this lookup; an unresolvable
    # pair is omitted rather than replaced with a guessed relative path.  Each
    # value later placed in an original artifact's ``upstream_refs`` is an
    # authenticated same-path, same-digest match; resolving it does not
    # establish a causal or owning-authority edge.  The entry's
    # authority/source fields remain authoritative.
    # This keeps repeated paths in the two descendants distinct without making
    # the portable index a new authority.
    #
    # Derivative entries use the resulting authenticated identity set as input
    # references.  Their separate meaning is declared in the index metadata
    # and human-readable trace below.
    #
    # The lookup remains keyed by the exact pair so no runtime authorization
    # rule is changed by the export labels.
    provenance: dict[tuple[str, str], set[str]] = {}

    def register(reference: ArtifactRefV5, authority: str) -> None:
        provenance.setdefault((reference.relative_path, reference.sha256), set()).add(
            f"{authority}|{reference.relative_path}|{reference.sha256}"
        )

    store_entries: list[tuple[str, ArtifactRefV5]] = []
    for kind in _STORE_KINDS:
        for reference in store.list_refs(kind=kind):
            store_entries.append((kind, reference))
            register(reference, f"study-store:{store.repository.root_identity_sha256}")

    ancestor = reopen_study_fixture_v1(
        root=prepared.ancestor_root,
        manifest_ref=prepared.ancestor_manifest_ref,
        registry=prepared.registry,
    )
    ancestor_authority = f"round-one:{ancestor.repository.root_identity_sha256}"
    for relative, digest in prepared.common_ancestor_files:
        register(ArtifactRefV5(relative, digest), ancestor_authority)

    arm_authorities: dict[str, str] = {}
    for arm in verification.arms:
        fixture = reopen_study_fixture_v1(
            root=prepared.fixture_path(arm.arm),
            manifest_ref=prepared.primary_manifest_ref if arm.arm == "primary" else prepared.withheld_manifest_ref,
            registry=prepared.registry,
        )
        authority = f"round-two-{arm.arm}:{fixture.repository.root_identity_sha256}"
        arm_authorities[arm.arm] = authority
        for reference in arm.artifact_refs:
            register(reference, authority)

    def upstream_for(raw: bytes) -> tuple[str, ...]:
        result: set[str] = set()
        for pair in _embedded_reference_pairs(raw):
            result.update(provenance.get(pair, ()))
        return tuple(sorted(result))

    derivative_upstream = tuple(sorted(item for values in provenance.values() for item in values))

    def add(relative: str, raw: bytes, *, source: ArtifactRefV5, authority: str, upstream: tuple[str, ...] = ()) -> None:
        relative = _safe_relative(relative)
        if relative in occupied:
            raise StudyAuthorityError(f"trace allowlist maps two authorities to one output path: {relative}")
        digest = _write_new(destination, relative, raw)
        occupied.add(relative)
        entries.append(
            {
                "relative_path": relative,
                "sha256": digest,
                "artifact_type": _artifact_type(relative),
                "source_relative_path": source.relative_path,
                "source_sha256": source.sha256,
                "authority": authority,
                "upstream_refs": list(upstream),
            }
        )

    # Every controller-owned byte is read through StudyStoreV1.  Namespace
    # listing is bounded and does not inspect credentials or unrelated files.
    for kind, reference in store_entries:
        if (reference.relative_path, reference.sha256) not in bound_refs:
            raise StudyAuthorityError(f"study store record was not bound by the verification: {reference.relative_path}")
        raw = store.read(reference)
        if _sha256(raw) != reference.sha256:
            raise StudyAuthorityError(f"study store bytes changed during export: {reference.relative_path}")
        add(
            _store_destination(kind, reference, raw),
            raw,
            source=reference,
            authority=f"study-store:{store.repository.root_identity_sha256}",
            upstream=upstream_for(raw),
        )

    # The round-one snapshot is the accepted closed graph, while descendant
    # roots are enumerated only through each arm's independently collected
    # references.  No directory-wide descendant copy is used here.
    for relative, digest in prepared.common_ancestor_files:
        source = ArtifactRefV5(relative, digest)
        raw = _fixture_bytes(ancestor.repository, source)
        add(
            f"round-1/{_safe_relative(relative)}",
            raw,
            source=source,
            authority=ancestor_authority,
            upstream=upstream_for(raw),
        )

    for arm in verification.arms:
        fixture = reopen_study_fixture_v1(
            root=prepared.fixture_path(arm.arm),
            manifest_ref=prepared.primary_manifest_ref if arm.arm == "primary" else prepared.withheld_manifest_ref,
            registry=prepared.registry,
        )
        for source in arm.artifact_refs:
            raw = _fixture_bytes(fixture.repository, source)
            add(
                f"round-2-{arm.arm}/{_safe_relative(source.relative_path)}",
                raw,
                source=source,
                authority=arm_authorities[arm.arm],
                upstream=upstream_for(raw),
            )

    rubric_raw = verification.canonical_bytes()
    rubric_source = ArtifactRefV5("verification.json", _sha256(rubric_raw))
    add(
        "rubric-results.json",
        rubric_raw,
        source=rubric_source,
        authority="export-derivative",
        upstream=derivative_upstream,
    )
    trace_raw = _trace_markdown(prepared, verification, entries, store=store)
    trace_source = ArtifactRefV5("trace.md", _sha256(trace_raw))
    add(
        "trace.md",
        trace_raw,
        source=trace_source,
        authority="export-derivative",
        upstream=derivative_upstream,
    )

    entries.sort(key=lambda item: str(item["relative_path"]))
    index_raw = canonical_json_bytes_v5(
        {
            "schema_version": 1,
            "kind": "v5-two-round-study-trace",
            "original_root_authority": {
                "study_root": str(prepared.root),
                "study_store_root_identity_sha256": store.repository.root_identity_sha256,
                "portable_bundle_note": "The export root has a distinct filesystem identity and cannot replace the original authority root.",
            },
            "provenance_semantics": {
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
            },
            "artifacts": entries,
        }
    )
    index_ref = ArtifactRefV5("artifact-index.json", _sha256(index_raw))
    _write_new(destination, index_ref.relative_path, index_raw)

    after = _input_inventory(prepared)
    if before != after:
        raise StudyAuthorityError("study input bytes changed during trace export")
    return index_ref


__all__ = ["export_study_trace_v1"]
