"""Trusted in-sandbox entrypoint for one PIT optimizer V5 semantic probe."""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import select
import stat
import subprocess
import sys

from core.pit_optimizer_v5.candidate_ir import (
    SourceBundleV5,
    SourceFileV5,
    derive_policy_revision_identity_v5,
)
from core.pit_optimizer_v5.contracts import canonical_json_bytes_v5, canonical_sha256_v5
from core.pit_optimizer_v5.policy_scope import EDITABLE_POLICY_PATHS_V5
from core.pit_optimizer_v5.probes import (
    PROBE_SUITE_ID_V5,
    SemanticFingerprintV5,
    fingerprint_policy_client_v5,
)
from core.strategy_policy import POLICY_INTERFACE_VERSION_V3
from core.strategy_policy.runtime import JsonLinePolicyClient
from core.strategy_policy.worker import (
    MAX_POLICY_LINE_BYTES,
    WorkerBootstrap,
    decode_policy_response,
    decode_worker_ready,
    encode_policy_request,
    initial_chain_sha256,
)


_POLICY_OVERLAY_ROOT = Path("/pit/candidate")
_OUTPUT_ROOT = Path("/pit/output")
_OUTPUT_NAME = "semantic-fingerprint.json"
_MAX_POLICY_SOURCE_BYTES = 131_072
_OVERLAY_FILE_BY_POLICY_PATH = {
    relative: relative.rsplit("/", 1)[-1] for relative in EDITABLE_POLICY_PATHS_V5
}


def _digest(value: str, label: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} is invalid")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False, add_help=False)
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--policy-sha256", required=True)
    parser.add_argument("--trusted-runtime-sha256", required=True)
    parser.add_argument("--immutable-constraints-sha256", required=True)
    parser.add_argument("--suite-id", required=True)
    parser.add_argument("--evaluator-contract-sha256", required=True)
    parser.add_argument("--sandbox-profile-sha256", required=True)
    parser.add_argument("--probe-runtime-sha256", required=True)
    parser.add_argument("--probe-runtime-authority-sha256", required=True)
    parser.add_argument("--call-timeout-seconds", required=True, type=float)
    parser.add_argument("--output-limit-bytes", required=True, type=int)
    return parser


def read_policy_source_v5() -> SourceBundleV5:
    files: list[SourceFileV5] = []
    directory_fd = os.open(
        _POLICY_OVERLAY_ROOT,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
            raise ValueError("probe policy overlay is invalid")
        for relative in EDITABLE_POLICY_PATHS_V5:
            descriptor = os.open(
                _OVERLAY_FILE_BY_POLICY_PATH[relative],
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            try:
                before = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or not 0 < before.st_size <= _MAX_POLICY_SOURCE_BYTES
                ):
                    raise ValueError("probe policy source is invalid")
                chunks: list[bytes] = []
                observed = 0
                while True:
                    chunk = os.read(
                        descriptor,
                        min(65_536, _MAX_POLICY_SOURCE_BYTES + 1 - observed),
                    )
                    if not chunk:
                        break
                    observed += len(chunk)
                    if observed > _MAX_POLICY_SOURCE_BYTES:
                        raise ValueError("probe policy source is invalid")
                    chunks.append(chunk)
                after = os.fstat(descriptor)
                if (
                    (before.st_dev, before.st_ino, before.st_size)
                    != (after.st_dev, after.st_ino, after.st_size)
                    or observed != before.st_size
                ):
                    raise ValueError("probe policy source changed")
                raw = b"".join(chunks)
            finally:
                os.close(descriptor)
            files.append(SourceFileV5(relative, raw.decode("utf-8", errors="strict")))
    finally:
        os.close(directory_fd)
    return SourceBundleV5(tuple(files))


class PolicyWorkerSessionV5:
    """One local V3 worker process contained by the already-owned outer sandbox."""

    def __init__(
        self,
        *,
        call_timeout_seconds: float,
        source: SourceBundleV5,
        startup_timeout_seconds: float | None = None,
    ) -> None:
        startup_timeout = (
            call_timeout_seconds
            if startup_timeout_seconds is None
            else startup_timeout_seconds
        )
        if (
            type(call_timeout_seconds) is not float
            or not math.isfinite(call_timeout_seconds)
            or call_timeout_seconds <= 0
            or type(startup_timeout) is not float
            or not math.isfinite(startup_timeout)
            or startup_timeout <= 0
            or type(source) is not SourceBundleV5
        ):
            raise ValueError("probe worker timeout is invalid")
        self._timeout = call_timeout_seconds
        self._startup_timeout = startup_timeout
        self._bootstrap = WorkerBootstrap.create(interface_version=POLICY_INTERFACE_VERSION_V3)
        self._sequence = 1
        self._previous_hmac_sha256 = initial_chain_sha256(self._bootstrap)
        environment = {
            "LANG": "C",
            "LC_ALL": "C",
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        source_by_name = {
            item.path.rsplit("/", 1)[-1].removesuffix(".py"): item.sha256
            for item in source.files
        }
        worker_argv = [
            sys.executable,
            "-B",
            "-m",
            "core.strategy_policy.worker",
            "--policy-overlay-root",
            str(_POLICY_OVERLAY_ROOT),
        ]
        for name in ("entry", "risk", "position", "exit"):
            worker_argv.extend((f"--policy-{name}-sha256", source_by_name[name]))
        self._process = subprocess.Popen(
            tuple(worker_argv),
            cwd=Path("/"),
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            close_fds=True,
            start_new_session=True,
        )
        try:
            self._write_line(self._bootstrap.to_json())
            decode_worker_ready(
                self._read_line(timeout_seconds=self._startup_timeout),
                bootstrap=self._bootstrap,
            )
        except BaseException:
            self.close()
            raise

    def _write_line(self, raw: str) -> None:
        if self._process.stdin is None:
            raise RuntimeError("probe worker input is closed")
        encoded = raw.encode("utf-8")
        if not encoded or len(encoded) > MAX_POLICY_LINE_BYTES:
            raise ValueError("probe worker request is invalid")
        self._process.stdin.write(encoded + b"\n")
        self._process.stdin.flush()

    def _read_line(self, *, timeout_seconds: float | None = None) -> str:
        stream = self._process.stdout
        if stream is None:
            raise RuntimeError("probe worker output is closed")
        ready, _writable, _exceptional = select.select(
            (stream,),
            (),
            (),
            self._timeout if timeout_seconds is None else timeout_seconds,
        )
        if not ready:
            raise TimeoutError("probe worker timed out")
        raw = stream.readline(MAX_POLICY_LINE_BYTES + 2)
        if not raw or len(raw) > MAX_POLICY_LINE_BYTES + 1 or not raw.endswith(b"\n"):
            raise RuntimeError("probe worker output is invalid")
        return raw.decode("utf-8", errors="strict")

    def call(self, method: str, snapshot: object) -> object:
        request_raw, request = encode_policy_request(
            bootstrap=self._bootstrap,
            sequence=self._sequence,
            previous_hmac_sha256=self._previous_hmac_sha256,
            method=method,
            snapshot=snapshot,
        )
        self._write_line(request_raw)
        _response, decision = decode_policy_response(
            self._read_line(),
            bootstrap=self._bootstrap,
            expected_sequence=self._sequence,
            expected_request_hmac_sha256=request.hmac_sha256,
            expected_method=method,
        )
        self._previous_hmac_sha256 = request.hmac_sha256
        self._sequence += 1
        return decision

    def close(self) -> None:
        process = getattr(self, "_process", None)
        if type(process) is not subprocess.Popen:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=self._timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=self._timeout)
        finally:
            if process.stdout is not None:
                process.stdout.close()


def _write_output(*, content: bytes, maximum_bytes: int) -> None:
    if (
        type(maximum_bytes) is not int
        or maximum_bytes <= 0
        or not content
        or len(content) > maximum_bytes
    ):
        raise ValueError("probe output exceeds its bound")
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


def main(argv: tuple[str, ...] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        request_sha256 = _digest(arguments.request_sha256, "probe request")
        expected_policy_sha256 = _digest(arguments.policy_sha256, "probe policy")
        trusted_runtime_sha256 = _digest(
            arguments.trusted_runtime_sha256,
            "trusted policy runtime",
        )
        immutable_constraints_sha256 = _digest(
            arguments.immutable_constraints_sha256,
            "immutable constraints",
        )
        evaluator_contract_sha256 = _digest(
            arguments.evaluator_contract_sha256,
            "evaluator contract",
        )
        sandbox_profile_sha256 = _digest(
            arguments.sandbox_profile_sha256,
            "sandbox profile",
        )
        probe_runtime_sha256 = _digest(
            arguments.probe_runtime_sha256,
            "trusted probe runtime",
        )
        probe_runtime_authority_sha256 = _digest(
            arguments.probe_runtime_authority_sha256,
            "trusted probe authority",
        )
        expected_probe_authority = canonical_sha256_v5(
            {
                "domain": "pit-optimizer-v5-trusted-probe-runtime-v1",
                "evaluator_contract_sha256": evaluator_contract_sha256,
                "sandbox_profile_sha256": sandbox_profile_sha256,
                "runtime_source_sha256": probe_runtime_sha256,
                "probe_suite_id": PROBE_SUITE_ID_V5,
            }
        )
        if probe_runtime_authority_sha256 != expected_probe_authority:
            raise ValueError("trusted probe runtime authority differs")
        if arguments.suite_id != PROBE_SUITE_ID_V5:
            raise ValueError("probe suite is invalid")
        from .image_manifest import verify_installed_evaluator_source_v5

        verify_installed_evaluator_source_v5(
            source_root=Path(__file__).resolve().parents[2],
            expected_sha256=probe_runtime_sha256,
        )
        source = read_policy_source_v5()
        revision = derive_policy_revision_identity_v5(
            source_bundle=source,
            trusted_policy_runtime_sha256=trusted_runtime_sha256,
            immutable_constraints_sha256=immutable_constraints_sha256,
        )
        if revision.sha256 != expected_policy_sha256:
            raise ValueError("probe policy identity differs")
        session = PolicyWorkerSessionV5(
            call_timeout_seconds=float(arguments.call_timeout_seconds),
            source=source,
        )
        client = JsonLinePolicyClient(
            session=session,
            interface_version=POLICY_INTERFACE_VERSION_V3,
        )
        try:
            fingerprint: SemanticFingerprintV5 = fingerprint_policy_client_v5(client)
        finally:
            client.close()
        output = canonical_json_bytes_v5(
            {
                "fingerprint": fingerprint.to_primitive(),
                "policy_revision_sha256": revision.sha256,
                "request_sha256": request_sha256,
                "suite_id": PROBE_SUITE_ID_V5,
            }
        )
        _write_output(content=output, maximum_bytes=arguments.output_limit_bytes)
        return 0
    except BaseException:
        return 3


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys.argv[1:])))


__all__ = ["PolicyWorkerSessionV5", "main", "read_policy_source_v5"]
