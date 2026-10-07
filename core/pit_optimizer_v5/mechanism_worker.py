"""Worker process boundary for one mechanism-only V3 exit decision."""

from __future__ import annotations

import math
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
from typing import TYPE_CHECKING

from core.strategy_policy import POLICY_INTERFACE_VERSION_V3
from core.strategy_policy.contracts_v3 import ExitSnapshotV3
from core.strategy_policy.worker import (
    MAX_POLICY_LINE_BYTES,
    WorkerBootstrap,
    decode_policy_response,
    decode_worker_ready,
    encode_policy_request,
    initial_chain_sha256,
)

if TYPE_CHECKING:
    from core.pit_optimizer_v5.candidate_ir import SourceBundleV5


class MechanismPolicyWorkerSessionV1:
    """One isolated evaluate_exit worker; separate from the fixed probe session."""

    def __init__(
        self,
        *,
        call_timeout_seconds: float,
        startup_timeout_seconds: float,
        cpu_seconds: float,
        source: SourceBundleV5,
        policy_overlay_root: Path,
    ) -> None:
        from core.pit_optimizer_v5.candidate_ir import SourceBundleV5

        if (
            type(call_timeout_seconds) is not float
            or not math.isfinite(call_timeout_seconds)
            or call_timeout_seconds <= 0
            or type(startup_timeout_seconds) is not float
            or not math.isfinite(startup_timeout_seconds)
            or startup_timeout_seconds <= 0
            or type(cpu_seconds) is not float
            or not math.isfinite(cpu_seconds)
            or cpu_seconds <= 0
            or type(source) is not SourceBundleV5
            or type(policy_overlay_root) is not Path
            or os.name == "nt"
        ):
            raise ValueError("mechanism worker resource or source authority is invalid")
        self._timeout = call_timeout_seconds
        self._startup_timeout = startup_timeout_seconds
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
        if set(source_by_name) != {"entry", "risk", "position", "exit"}:
            raise ValueError("mechanism worker policy source path set is invalid")
        worker_argv = [
            sys.executable,
            "-B",
            "-m",
            "core.strategy_policy.worker",
            "--policy-overlay-root",
            str(policy_overlay_root),
        ]
        for name in ("entry", "risk", "position", "exit"):
            worker_argv.extend((f"--policy-{name}-sha256", source_by_name[name]))
        cpu_limit_seconds = max(1, math.ceil(cpu_seconds))

        def set_child_cpu_limit() -> None:
            import resource

            resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit_seconds, cpu_limit_seconds))

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
            preexec_fn=set_child_cpu_limit,
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
            raise RuntimeError("mechanism worker input is closed")
        encoded = raw.encode("utf-8")
        if not encoded or len(encoded) > MAX_POLICY_LINE_BYTES:
            raise ValueError("mechanism worker request is invalid")
        self._process.stdin.write(encoded + b"\n")
        self._process.stdin.flush()

    def _read_line(self, *, timeout_seconds: float | None = None) -> str:
        stream = self._process.stdout
        if stream is None:
            raise RuntimeError("mechanism worker output is closed")
        ready, _writable, _exceptional = select.select(
            (stream,),
            (),
            (),
            self._timeout if timeout_seconds is None else timeout_seconds,
        )
        if not ready:
            raise TimeoutError("mechanism worker timed out")
        raw = stream.readline(MAX_POLICY_LINE_BYTES + 2)
        if not raw or len(raw) > MAX_POLICY_LINE_BYTES + 1 or not raw.endswith(b"\n"):
            raise RuntimeError("mechanism worker output is invalid")
        return raw.decode("utf-8", errors="strict")

    def call(self, method: str, snapshot: object) -> object:
        if method != "evaluate_exit" or type(snapshot) is not ExitSnapshotV3:
            raise ValueError("mechanism worker method or snapshot is unsupported")
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

    def terminate_child_after_ready(self) -> None:
        """Kill the ready worker process and process group for one crash case."""

        process = self._process
        if process.poll() is not None:
            raise RuntimeError("mechanism worker exited before the crash point")
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=self._timeout)

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
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=self._timeout)
        finally:
            if process.stdout is not None:
                process.stdout.close()


__all__ = ["MechanismPolicyWorkerSessionV1"]
