"""Trusted entrypoint for one source-bound mechanism policy decision."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

from core.pit_optimizer_v5.candidate_ir import (
    PolicyRevisionIdentityV5,
    SourceBundleV5,
    SourceFileV5,
)
from core.pit_optimizer_v5.contracts import canonical_json_bytes_v5
from core.pit_optimizer_v5.contracts import canonical_primitive_v5
from core.pit_optimizer_v5.mechanism_contracts import (
    MechanismObservationBindingV1,
    mechanism_request_allocation_v1,
)
from core.pit_optimizer_v5.mechanism_probes import MechanismPairedCaseV1
from core.pit_optimizer_v5.probes import canonical_probe_json_v5
from core.strategy_policy.contracts import ExitDecision, validate_exit_decision
from core.strategy_policy.contracts_v3 import ExitSnapshotV3


_INPUT_ROOT = Path("/pit/request")
_INPUT_NAME = "mechanism-case.json"
_OUTPUT_ROOT = Path("/pit/output")
_OUTPUT_NAME = "mechanism-case.json"
_MAX_POLICY_SOURCE_BYTES = 131_072


@dataclass(frozen=True, slots=True)
class DecodedMechanismDockerCaseV1:
    """Validated fields passed from the bounded input mount to the worker."""

    request_sha256: str
    owner_sha256: str
    manifest_sha256: str
    sandbox_profile_sha256: str
    role: str
    policy_revision: PolicyRevisionIdentityV5
    source_bundle: SourceBundleV5
    binding: MechanismObservationBindingV1
    corpus_sha256: str
    case: MechanismPairedCaseV1
    repetition: int
    failure_mode: str
    allocation_request_count: int
    cpu_seconds_per_request: Decimal
    timeout_ms_per_request: int
    effective_timeout_ms: int
    output_limit_bytes: int


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("mechanism request contains a duplicate field")
        result[key] = value
    return result


def _digest(value: object, name: str) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"mechanism request {name} is invalid")
    return value


def _decode_probe_input(value: object, encoded: str) -> Decimal | None:
    """Decode the lossless input identity and reconcile its readable value."""

    try:
        raw = encoded.encode("utf-8")
        primitive = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, AttributeError):
        raise ValueError("mechanism case input identity is invalid") from None
    if type(primitive) is not list:
        raise ValueError("mechanism case input identity is invalid")
    if primitive == ["none"]:
        if value is not None:
            raise ValueError("mechanism case input value does not reconcile")
        return None
    if (
        len(primitive) != 4
        or primitive[0] != "decimal"
        or type(primitive[1]) is not int
        or primitive[1] not in {0, 1}
        or type(primitive[2]) is not str
        or re.fullmatch(r"[0-9]{1,64}", primitive[2]) is None
        or type(primitive[3]) is not int
        or abs(primitive[3]) > 64
    ):
        raise ValueError("mechanism case input identity is invalid")
    parsed = Decimal((primitive[1], tuple(int(char) for char in primitive[2]), primitive[3]))
    if canonical_probe_json_v5(parsed) != raw:
        raise ValueError("mechanism case input identity is not canonical")
    if type(value) is not str:
        raise ValueError("mechanism case readable input is invalid")
    try:
        readable = Decimal(value)
    except Exception:
        raise ValueError("mechanism case readable input is invalid") from None
    if not readable.is_finite() or readable != parsed:
        raise ValueError("mechanism case input value does not reconcile")
    return parsed


def decode_mechanism_case_request_v1(
    raw: bytes,
    *,
    expected_request_sha256: str,
    expected_role: str,
    expected_policy_sha256: str,
    expected_source_bundle_sha256: str,
    expected_snapshot_sha256: str,
) -> DecodedMechanismDockerCaseV1:
    """Reconstruct and authenticate one canonical request without evaluating policy code."""

    if type(raw) is not bytes or not raw or len(raw) > 8 * 1024 * 1024:
        raise ValueError("mechanism request bytes are invalid")
    expected = (
        (expected_request_sha256, hashlib.sha256(raw).hexdigest()),
    )
    for digest, observed in expected:
        if _digest(digest, "command request digest") != observed:
            raise ValueError("mechanism request bytes differ from command authority")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
        canonical = canonical_json_bytes_v5(payload)
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError, OverflowError, RecursionError):
        raise ValueError("mechanism request is not canonical JSON") from None
    if type(payload) is not dict or canonical != raw:
        raise ValueError("mechanism request is not canonical JSON")
    expected_fields = {
        "schema_version",
        "owner_sha256",
        "manifest_sha256",
        "sandbox_profile_sha256",
        "role",
        "policy_revision",
        "policy_revision_sha256",
        "source_bundle",
        "source_bundle_sha256",
        "binding",
        "corpus_sha256",
        "case",
        "repetition",
        "failure_mode",
        "resource_budget",
        "output_mount",
        "allocation_request_count",
        "cpu_seconds_per_request",
        "timeout_ms_per_request",
        "output_bytes_per_request",
        "effective_timeout_ms",
        "output_limit_bytes",
    }
    if set(payload) != expected_fields or type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("mechanism request schema is invalid")

    revision_raw = payload["policy_revision"]
    if type(revision_raw) is not dict or set(revision_raw) != {
        "policy_interface_version",
        "trusted_policy_runtime_sha256",
        "immutable_constraints_sha256",
        "editable_source_sha256",
    }:
        raise ValueError("mechanism policy revision payload is invalid")
    source_raw = payload["source_bundle"]
    if type(source_raw) is not dict or set(source_raw) != {"files"} or type(source_raw["files"]) is not list:
        raise ValueError("mechanism source bundle payload is invalid")
    source_bundle = SourceBundleV5(
        tuple(SourceFileV5(item["path"], item["source"]) for item in source_raw["files"])
    )
    editable = revision_raw["editable_source_sha256"]
    if type(editable) is not list or any(type(item) is not list or len(item) != 2 for item in editable):
        raise ValueError("mechanism source revision paths are invalid")
    policy_revision = PolicyRevisionIdentityV5(
        policy_interface_version=revision_raw["policy_interface_version"],  # type: ignore[arg-type]
        trusted_policy_runtime_sha256=revision_raw["trusted_policy_runtime_sha256"],  # type: ignore[arg-type]
        immutable_constraints_sha256=revision_raw["immutable_constraints_sha256"],  # type: ignore[arg-type]
        editable_source_sha256=tuple((item[0], item[1]) for item in editable),  # type: ignore[arg-type]
    )
    if (
        policy_revision.sha256 != _digest(payload["policy_revision_sha256"], "policy revision")
        or source_bundle.sha256 != _digest(payload["source_bundle_sha256"], "source bundle")
        or policy_revision.editable_source_sha256
        != tuple((item.path, item.sha256) for item in source_bundle.files)
    ):
        raise ValueError("mechanism policy source identity does not reconcile")

    binding_raw = payload["binding"]
    if type(binding_raw) is not dict:
        raise ValueError("mechanism observation binding is invalid")
    binding = MechanismObservationBindingV1.from_primitive(binding_raw)
    allocation_request_count, cpu_seconds_per_request, timeout_ms_per_request, output_bytes_per_request = (
        mechanism_request_allocation_v1(binding)
    )
    effective_timeout_ms = payload["effective_timeout_ms"]
    if (
        type(payload["allocation_request_count"]) is not int
        or payload["allocation_request_count"] != allocation_request_count
        or type(payload["cpu_seconds_per_request"]) is not str
        or payload["cpu_seconds_per_request"] != canonical_primitive_v5(cpu_seconds_per_request)
        or type(payload["timeout_ms_per_request"]) is not int
        or payload["timeout_ms_per_request"] != timeout_ms_per_request
        or type(payload["output_bytes_per_request"]) is not int
        or payload["output_bytes_per_request"] != output_bytes_per_request
        or type(effective_timeout_ms) is not int
        or not 0 < effective_timeout_ms <= timeout_ms_per_request
    ):
        raise ValueError("mechanism request run-total allocation differs from its binding")
    corpus_sha256 = _digest(payload["corpus_sha256"], "corpus")
    if (
        binding.corpus_sha256 != corpus_sha256
        or payload["resource_budget"] != binding.resource_budget.to_primitive()
    ):
        raise ValueError("mechanism corpus identity differs from binding")
    case_raw = payload["case"]
    case_fields = {
        "order",
        "input_value",
        "input_canonical_bytes",
        "input_identity_sha256",
        "converted_value",
        "snapshot_canonical_json",
        "snapshot_sha256",
        "applicable",
    }
    if type(case_raw) is not dict or set(case_raw) != case_fields:
        raise ValueError("mechanism case payload is invalid")
    input_bytes = case_raw["input_canonical_bytes"]
    snapshot_raw = case_raw["snapshot_canonical_json"]
    if type(input_bytes) is not str or type(snapshot_raw) is not str:
        raise ValueError("mechanism case canonical bytes are invalid")
    input_value = _decode_probe_input(case_raw["input_value"], input_bytes)
    snapshot_bytes = snapshot_raw.encode("utf-8")
    snapshot = ExitSnapshotV3.from_canonical_json(snapshot_raw)
    case = MechanismPairedCaseV1(
        order=case_raw["order"],  # type: ignore[arg-type]
        input_value=input_value,  # type: ignore[arg-type]
        input_canonical_bytes=input_bytes.encode("utf-8"),
        input_identity_sha256=case_raw["input_identity_sha256"],  # type: ignore[arg-type]
        converted_value=case_raw["converted_value"],  # type: ignore[arg-type]
        snapshot=snapshot,
        snapshot_canonical_json=snapshot_bytes,
        snapshot_sha256=case_raw["snapshot_sha256"],  # type: ignore[arg-type]
        applicable=case_raw["applicable"],  # type: ignore[arg-type]
    )

    role = payload["role"]
    repetition = payload["repetition"]
    failure_mode = payload["failure_mode"]
    output_mount = payload["output_mount"]
    output_limit_bytes = payload["output_limit_bytes"]
    if (
        role not in {"parent", "candidate"}
        or type(repetition) is not int
        or repetition < 0
        or failure_mode not in {"none", "terminate_child_after_ready"}
        or type(output_mount) is not dict
        or set(output_mount) != {
            "root_identity_sha256",
            "content_authority_sha256",
            "container_path",
            "maximum_bytes",
        }
        or type(output_limit_bytes) is not int
        or output_limit_bytes != output_bytes_per_request
        or output_mount["container_path"] != "/pit/output"
        or output_mount["maximum_bytes"] != output_limit_bytes
    ):
        raise ValueError("mechanism request execution scope is invalid")
    _digest(payload["owner_sha256"], "owner")
    _digest(payload["manifest_sha256"], "manifest")
    profile_sha256 = _digest(payload["sandbox_profile_sha256"], "sandbox profile")
    _digest(output_mount["root_identity_sha256"], "output root")
    _digest(output_mount["content_authority_sha256"], "output authority")
    if role == "parent":
        if policy_revision.sha256 != binding.parent_revision_sha256:
            raise ValueError("mechanism parent revision differs from binding")
    elif source_bundle.sha256 != binding.candidate_bytes_sha256:
        raise ValueError("mechanism candidate source differs from binding")
    if failure_mode == "terminate_child_after_ready" and (role != "candidate" or case.order != 0 or repetition != 0):
        raise ValueError("mechanism worker termination is outside its one-case scope")
    if (
        role != expected_role
        or policy_revision.sha256 != _digest(expected_policy_sha256, "command policy")
        or source_bundle.sha256 != _digest(expected_source_bundle_sha256, "command source bundle")
        or case.snapshot_sha256 != _digest(expected_snapshot_sha256, "command snapshot")
    ):
        raise ValueError("mechanism request differs from command identity")
    return DecodedMechanismDockerCaseV1(
        hashlib.sha256(raw).hexdigest(),
        payload["owner_sha256"],  # type: ignore[arg-type]
        payload["manifest_sha256"],  # type: ignore[arg-type]
        profile_sha256,
        role,  # type: ignore[arg-type]
        policy_revision,
        source_bundle,
        binding,
        corpus_sha256,
        case,
        repetition,
        failure_mode,  # type: ignore[arg-type]
        allocation_request_count,
        cpu_seconds_per_request,
        timeout_ms_per_request,
        effective_timeout_ms,
        output_limit_bytes,
    )


def _parser():
    import argparse

    parser = argparse.ArgumentParser(allow_abbrev=False, add_help=False)
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--policy-sha256", required=True)
    parser.add_argument("--source-bundle-sha256", required=True)
    parser.add_argument("--binding-sha256", required=True)
    parser.add_argument("--corpus-sha256", required=True)
    parser.add_argument("--case-order", required=True, type=int)
    parser.add_argument("--snapshot-sha256", required=True)
    parser.add_argument("--repetition", required=True, type=int)
    parser.add_argument("--failure-mode", required=True)
    parser.add_argument("--sandbox-profile-sha256", required=True)
    parser.add_argument("--runtime-source-sha256", required=True)
    parser.add_argument("--timeout-ms", required=True, type=int)
    parser.add_argument("--cpu-seconds", required=True)
    parser.add_argument("--output-limit-bytes", required=True, type=int)
    return parser


def _read_request_file_v1(maximum_bytes: int = 8 * 1024 * 1024) -> bytes:
    directory_fd = os.open(_INPUT_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open(
            _INPUT_NAME,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= maximum_bytes:
                raise ValueError("mechanism request file is invalid")
            chunks: list[bytes] = []
            observed = 0
            while True:
                chunk = os.read(descriptor, min(65_536, maximum_bytes + 1 - observed))
                if not chunk:
                    break
                observed += len(chunk)
                if observed > maximum_bytes:
                    raise ValueError("mechanism request file is invalid")
                chunks.append(chunk)
            after = os.fstat(descriptor)
            if (
                (before.st_dev, before.st_ino, before.st_size)
                != (after.st_dev, after.st_ino, after.st_size)
                or observed != before.st_size
            ):
                raise ValueError("mechanism request file changed")
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_fd)


def _stage_policy_source_v1(source_bundle: SourceBundleV5) -> Path:
    """Stage only the four bound policy files into the container's bounded tmpfs."""

    from core.pit_optimizer_v5.policy_scope import EDITABLE_POLICY_PATHS_V5

    if tuple(item.path for item in source_bundle.files) != EDITABLE_POLICY_PATHS_V5:
        raise ValueError("mechanism policy source path set is invalid")
    overlay = Path("/tmp/pit-mechanism-policy")
    os.mkdir(overlay, 0o700)
    directory_fd = os.open(overlay, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for item in source_bundle.files:
            filename = item.path.rsplit("/", 1)[-1]
            encoded = item.source.encode("utf-8")
            if not encoded or len(encoded) > _MAX_POLICY_SOURCE_BYTES:
                raise ValueError("mechanism policy source exceeds its bound")
            descriptor = os.open(
                filename,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o400,
                dir_fd=directory_fd,
            )
            try:
                view = memoryview(encoded)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("mechanism policy source write was incomplete")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        os.close(directory_fd)
    return overlay


def _write_output_v1(*, content: bytes, maximum_bytes: int) -> None:
    if type(maximum_bytes) is not int or maximum_bytes <= 0 or not content or len(content) > maximum_bytes:
        raise ValueError("mechanism output exceeds its bound")
    directory_fd = os.open(_OUTPUT_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        file_fd = os.open(
            _OUTPUT_NAME,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        try:
            with os.fdopen(file_fd, "wb", closefd=False) as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(file_fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def decode_mechanism_case_output_v1(
    raw: bytes,
    *,
    expected_request_sha256: str,
    expected_binding_sha256: str,
    expected_role: str,
    expected_policy_sha256: str,
    expected_source_bundle_sha256: str,
    expected_corpus_sha256: str,
    expected_case: MechanismPairedCaseV1,
    expected_repetition: int,
    maximum_bytes: int,
) -> ExitDecision:
    """Decode and reconcile the output of exactly one mechanism request."""

    if (
        type(raw) is not bytes
        or not raw
        or len(raw) > maximum_bytes
        or type(expected_case) is not MechanismPairedCaseV1
        or type(expected_repetition) is not int
        or expected_repetition < 0
    ):
        raise ValueError("mechanism output bytes or expected identity are invalid")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
        canonical = canonical_json_bytes_v5(payload)
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError, OverflowError, RecursionError):
        raise ValueError("mechanism output is not canonical JSON") from None
    fields = {
        "schema_version",
        "request_sha256",
        "binding_sha256",
        "corpus_sha256",
        "role",
        "policy_revision_sha256",
        "source_bundle_sha256",
        "case_order",
        "input_identity_sha256",
        "snapshot_sha256",
        "repetition",
        "decision_json",
        "decision_sha256",
    }
    if (
        type(payload) is not dict
        or set(payload) != fields
        or canonical != raw
        or type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or payload["request_sha256"] != _digest(expected_request_sha256, "expected request")
        or payload["binding_sha256"] != _digest(expected_binding_sha256, "expected binding")
        or payload["role"] != expected_role
        or payload["policy_revision_sha256"] != _digest(expected_policy_sha256, "expected policy")
        or payload["source_bundle_sha256"] != _digest(expected_source_bundle_sha256, "expected source bundle")
        or payload["corpus_sha256"] != _digest(expected_corpus_sha256, "expected corpus")
        or payload["case_order"] != expected_case.order
        or payload["input_identity_sha256"] != expected_case.input_identity_sha256
        or payload["snapshot_sha256"] != expected_case.snapshot_sha256
        or payload["repetition"] != expected_repetition
        or type(payload["decision_json"]) is not str
    ):
        raise ValueError("mechanism output identity differs from its request")
    decision_raw = payload["decision_json"].encode("utf-8")
    if _digest(payload["decision_sha256"], "decision") != hashlib.sha256(decision_raw).hexdigest():
        raise ValueError("mechanism decision digest differs")
    decision = ExitDecision.from_canonical_json(payload["decision_json"])
    validate_exit_decision(expected_case.snapshot.base, decision)
    if canonical_json_bytes_v5(decision) != decision_raw:
        raise ValueError("mechanism decision is not canonical")
    return decision


def main(argv: tuple[str, ...] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        for name in (
            "request_sha256",
            "policy_sha256",
            "source_bundle_sha256",
            "binding_sha256",
            "corpus_sha256",
            "snapshot_sha256",
            "sandbox_profile_sha256",
            "runtime_source_sha256",
        ):
            _digest(getattr(arguments, name), name)
        if (
            arguments.role not in {"parent", "candidate"}
            or arguments.case_order < 0
            or arguments.repetition < 0
            or arguments.timeout_ms <= 0
            or arguments.output_limit_bytes <= 0
            or arguments.failure_mode not in {"none", "terminate_child_after_ready"}
        ):
            raise ValueError("mechanism command resource scope is invalid")
        from .image_manifest import verify_installed_evaluator_source_v5

        verify_installed_evaluator_source_v5(
            source_root=Path(__file__).resolve().parents[2],
            expected_sha256=arguments.runtime_source_sha256,
        )
        raw = _read_request_file_v1()
        request = decode_mechanism_case_request_v1(
            raw,
            expected_request_sha256=arguments.request_sha256,
            expected_role=arguments.role,
            expected_policy_sha256=arguments.policy_sha256,
            expected_source_bundle_sha256=arguments.source_bundle_sha256,
            expected_snapshot_sha256=arguments.snapshot_sha256,
        )
        cpu_seconds = Decimal(arguments.cpu_seconds)
        if (
            request.binding.sha256 != arguments.binding_sha256
            or request.corpus_sha256 != arguments.corpus_sha256
            or request.case.order != arguments.case_order
            or request.repetition != arguments.repetition
            or request.failure_mode != arguments.failure_mode
            or request.sandbox_profile_sha256 != arguments.sandbox_profile_sha256
            or arguments.timeout_ms != request.effective_timeout_ms
            or canonical_primitive_v5(request.cpu_seconds_per_request) != arguments.cpu_seconds
            or not cpu_seconds.is_finite()
            or cpu_seconds != request.cpu_seconds_per_request
            or request.output_limit_bytes != arguments.output_limit_bytes
        ):
            raise ValueError("mechanism command differs from its canonical request")

        from .mechanism_worker import MechanismPolicyWorkerSessionV1

        overlay = _stage_policy_source_v1(request.source_bundle)
        session = MechanismPolicyWorkerSessionV1(
            call_timeout_seconds=min(
                arguments.timeout_ms / 1000.0,
                float(request.timeout_ms_per_request) / 1000.0,
            ),
            startup_timeout_seconds=arguments.timeout_ms / 1000.0,
            cpu_seconds=float(request.cpu_seconds_per_request),
            source=request.source_bundle,
            policy_overlay_root=overlay,
        )
        try:
            if request.failure_mode == "terminate_child_after_ready":
                session.terminate_child_after_ready()
            decision = session.call("evaluate_exit", request.case.snapshot)
        finally:
            session.close()
        if type(decision) is not ExitDecision:
            raise ValueError("mechanism worker returned a non-exit decision")
        decision_json = canonical_json_bytes_v5(decision)
        parsed_decision = ExitDecision.from_canonical_json(decision_json.decode("utf-8"))
        validate_exit_decision(request.case.snapshot.base, parsed_decision)
        if parsed_decision != decision:
            raise ValueError("mechanism worker decision is not canonical")
        output = canonical_json_bytes_v5(
            {
                "schema_version": 1,
                "request_sha256": request.request_sha256,
                "binding_sha256": request.binding.sha256,
                "corpus_sha256": request.corpus_sha256,
                "role": request.role,
                "policy_revision_sha256": request.policy_revision.sha256,
                "source_bundle_sha256": request.source_bundle.sha256,
                "case_order": request.case.order,
                "input_identity_sha256": request.case.input_identity_sha256,
                "snapshot_sha256": request.case.snapshot_sha256,
                "repetition": request.repetition,
                "decision_json": decision_json.decode("utf-8"),
                "decision_sha256": hashlib.sha256(decision_json).hexdigest(),
            }
        )
        _write_output_v1(content=output, maximum_bytes=request.output_limit_bytes)
        return 0
    except BaseException:
        return 3


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys.argv[1:])))


__all__ = [
    "DecodedMechanismDockerCaseV1",
    "decode_mechanism_case_output_v1",
    "decode_mechanism_case_request_v1",
    "main",
]
