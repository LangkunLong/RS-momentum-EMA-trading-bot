"""Trusted in-sandbox entrypoint for one PIT optimizer V5 panel evaluation."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import stat
import sys

from core.pit_data import PITDataBundle
from core.strategy_policy import POLICY_INTERFACE_VERSION_V3
from core.strategy_policy.runtime import JsonLinePolicyClient

from .candidate_ir import (
    PolicyRevisionIdentityV5,
    SourceBundleV5,
    derive_policy_revision_identity_v5,
)
from .container_protocol import (
    PanelExecutionRequestV5,
    decode_panel_execution_request_v5,
    panel_execution_output_bytes_v5,
)
from .diagnostics import summarize_panel_result
from .evaluator import CandidateWorkerBindingV5, PitPanelEvaluatorV5
from .probe_entry import PolicyWorkerSessionV5, read_policy_source_v5


_POLICY_ROOT = Path("/pit/candidate")
_DATA_ROOT = Path("/pit/data")
_INPUT_ROOT = Path("/pit/request")
_OUTPUT_ROOT = Path("/pit/output")
_BUNDLE_PATH = _DATA_ROOT / "pit_bundle.sqlite3"
_PROVENANCE_PATH = _DATA_ROOT / "prices_provenance.json"
_INPUT_NAME = "panel-request.json"
_OUTPUT_NAME = "panel-evaluation.json"
_MAXIMUM_INPUT_BYTES = 8 * 1024 * 1024


def _digest(value: str, label: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} is invalid")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False, add_help=False)
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--input-sha256", required=True)
    parser.add_argument("--evaluator-sha256", required=True)
    parser.add_argument("--sandbox-sha256", required=True)
    parser.add_argument("--policy-sha256", required=True)
    parser.add_argument("--panel-sha256", required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--output-limit-bytes", required=True, type=int)
    parser.add_argument("--scenario", action="append", required=True)
    return parser


def _read_input() -> bytes:
    directory_fd = os.open(
        _INPUT_ROOT,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
            raise ValueError("panel request root is invalid")
        descriptor = os.open(
            _INPUT_NAME,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or not 0 < before.st_size <= _MAXIMUM_INPUT_BYTES
            ):
                raise ValueError("panel request file is invalid")
            chunks: list[bytes] = []
            observed = 0
            while True:
                chunk = os.read(
                    descriptor,
                    min(65_536, _MAXIMUM_INPUT_BYTES + 1 - observed),
                )
                if not chunk:
                    break
                observed += len(chunk)
                if observed > _MAXIMUM_INPUT_BYTES:
                    raise ValueError("panel request exceeds its bound")
                chunks.append(chunk)
            after = os.fstat(descriptor)
            if (
                (before.st_dev, before.st_ino, before.st_size)
                != (after.st_dev, after.st_ino, after.st_size)
                or observed != before.st_size
            ):
                raise ValueError("panel request changed during read")
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_fd)


def _write_output(*, content: bytes, maximum_bytes: int) -> None:
    if (
        type(maximum_bytes) is not int
        or maximum_bytes <= 0
        or not content
        or len(content) > maximum_bytes
    ):
        raise ValueError("panel output exceeds its bound")
    directory_fd = os.open(
        _OUTPUT_ROOT,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        descriptor = os.open(
            _OUTPUT_NAME,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


class _CandidateWorkerFactoryV5:
    def __init__(
        self,
        *,
        request: PanelExecutionRequestV5,
        source: SourceBundleV5,
    ) -> None:
        self._request = request
        self._source = source

    def __call__(
        self,
        *,
        candidate_root: Path,
        policy_revision: PolicyRevisionIdentityV5,
    ) -> CandidateWorkerBindingV5:
        expected_root = _POLICY_ROOT.resolve(strict=True)
        if candidate_root != expected_root or policy_revision != self._request.policy_revision:
            raise ValueError("panel candidate worker authority differs")
        session = PolicyWorkerSessionV5(
            call_timeout_seconds=float(
                self._request.policy_method_timeout_seconds
            ),
            startup_timeout_seconds=float(
                self._request.worker_startup_timeout_seconds
            ),
            source=self._source,
        )
        return CandidateWorkerBindingV5(
            candidate_root=expected_root,
            policy_revision=policy_revision,
            client=JsonLinePolicyClient(
                session=session,
                interface_version=POLICY_INTERFACE_VERSION_V3,
            ),
        )


def _validate_cli_authority(
    arguments: argparse.Namespace,
    *,
    raw_input: bytes,
    request: PanelExecutionRequestV5,
) -> None:
    input_sha256 = hashlib.sha256(raw_input).hexdigest()
    expected = (
        (_digest(arguments.request_sha256, "panel request"), request.request_sha256),
        (_digest(arguments.input_sha256, "panel input"), request.sha256),
        (_digest(arguments.evaluator_sha256, "panel evaluator"), request.evaluator_contract.sha256),
        (_digest(arguments.sandbox_sha256, "panel sandbox"), request.sandbox_profile.sha256),
        (_digest(arguments.policy_sha256, "panel policy"), request.policy_revision.sha256),
        (_digest(arguments.panel_sha256, "panel identity"), request.panel.sha256),
        (arguments.start_date, request.panel.start_date),
        (arguments.end_date, request.panel.end_date),
        (arguments.output_limit_bytes, request.output_limit_bytes),
        (tuple(arguments.scenario), request.scenario_ids),
        (input_sha256, request.sha256),
    )
    if any(actual != authority for actual, authority in expected):
        raise ValueError("panel command differs from its canonical request")


def main(argv: tuple[str, ...] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        raw_input = _read_input()
        request = decode_panel_execution_request_v5(raw_input)
        _validate_cli_authority(arguments, raw_input=raw_input, request=request)
        source = read_policy_source_v5()
        policy_revision = derive_policy_revision_identity_v5(
            source_bundle=source,
            trusted_policy_runtime_sha256=(
                request.policy_revision.trusted_policy_runtime_sha256
            ),
            immutable_constraints_sha256=(
                request.policy_revision.immutable_constraints_sha256
            ),
        )
        if policy_revision != request.policy_revision:
            raise ValueError("panel policy source differs from request authority")
        with PITDataBundle(
            _BUNDLE_PATH,
            expected_sha256=request.evaluator_contract.pit_bundle_sha256,
            prices_provenance=_PROVENANCE_PATH,
        ) as bundle:
            evaluator = PitPanelEvaluatorV5(
                contract=request.evaluator_contract,
                sandbox_profile=request.sandbox_profile,
                resource_manifest=request.sandbox_profile,
                execution_profile=request.execution_profile,
                pit_bundle=bundle,
                prices_provenance=_PROVENANCE_PATH,
                report_builder=summarize_panel_result,
                candidate_policy_authority=policy_revision,
            )
            evaluation = evaluator.evaluate_candidate(
                candidate_root=_POLICY_ROOT.resolve(strict=True),
                panel=request.panel,
                policy_revision=policy_revision,
                worker_factory=_CandidateWorkerFactoryV5(
                    request=request,
                    source=source,
                ),
                scenario_ids=request.scenario_ids,
            )
        output = panel_execution_output_bytes_v5(
            request=request,
            evaluation=evaluation,
        )
        _write_output(content=output, maximum_bytes=request.output_limit_bytes)
        return 0
    except BaseException:
        return 3


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys.argv[1:])))


__all__ = ["main"]
