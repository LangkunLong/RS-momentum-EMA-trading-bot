"""Local regressions for the combined runner's image admission gates.

Every subprocess, Docker command, build, import boundary, and probe is mocked.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest
import uuid
from unittest.mock import patch

from scripts import issue82_github_runner_probe_combined as runner_probe


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "issue-82-v5-container-combined.yml"


def completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["docker", "image", "inspect", "synthetic-tag"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def forbidden_subprocess(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("regression tests must not launch subprocesses")


def trace_result(trace: list[dict[str, object]] | None, argv: list[str], result: subprocess.CompletedProcess[str], action: str | None) -> None:
    if trace is not None:
        trace.append({
            "action": action or "mocked_command",
            "command_prefix": argv[:3],
            "command_sha256": runner_probe._sha256(runner_probe._canonical_bytes(argv)),
            "timeout_seconds": 10,
            "elapsed_seconds": 0,
            "returncode": result.returncode,
            "timed_out": False,
            "stdout_sha256": runner_probe._output_sha256(result.stdout),
            "stderr_sha256": runner_probe._output_sha256(result.stderr),
        })


def action_exporter(evidence_path: Path, summary_path: Path) -> tuple[int, str, str]:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    marker = 'python3 - "$EVIDENCE_PATH" "$GITHUB_STEP_SUMMARY" <<\'PY\'\n'
    if marker not in workflow:
        raise AssertionError("current workflow checkpoint exporter was not found")
    source = workflow.split(marker, 1)[1].split("\n          PY", 1)[0]
    source = textwrap.dedent(source)
    stdout = io.StringIO()
    code = 0
    with patch.object(sys, "argv", ["issue82-checkpoint-exporter", str(evidence_path), str(summary_path)]):
        with contextlib.redirect_stdout(stdout):
            try:
                exec(compile(source, str(WORKFLOW_PATH), "exec"), {"__name__": "__main__"})
            except SystemExit as exc:
                code = int(exc.code or 0)
    return code, stdout.getvalue(), summary_path.read_text(encoding="utf-8")


class CombinedRunnerPreflightTests(unittest.TestCase):
    def run_preflight(self, inspect_result: subprocess.CompletedProcess[str] | None = None,
                      inspect_exception: BaseException | None = None,
                      use_real_command_for_exception: bool = False) -> tuple[dict[str, object], list[list[str]], list[list[str]], list[dict[str, object]], Path]:
        runner_temp = ROOT
        evidence_path = ROOT / f".issue82-preflight-{uuid.uuid4().hex}.json"
        self.addCleanup(lambda: evidence_path.unlink(missing_ok=True))
        env = {
            "GITHUB_RUN_ID": "local-preflight-regression",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": f"refs/heads/{runner_probe.RUNNER_PACKAGE_BRANCH}",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_REPOSITORY": "LangkunLong/RS-momentum-EMA-trading-bot",
            "RUNNER_OS": "Linux",
            "RUNNER_ARCH": "X64",
            "RUNNER_TEMP": str(runner_temp),
        }
        docker_calls: list[list[str]] = []
        build_calls: list[list[str]] = []
        checkpoints: list[dict[str, object]] = []
        inspect_count = 0
        real_command = runner_probe._command

        def fake_command(argv: list[str], *, timeout: float, check: bool = True,
                         trace: list[dict[str, object]] | None = None,
                         trace_action: str | None = None) -> subprocess.CompletedProcess[str]:
            nonlocal inspect_count
            docker_calls.append(list(argv))
            if argv[:2] == ["docker", "version"]:
                result = completed(0, "test-engine linux/amd64")
            elif argv[:3] == ["docker", "image", "inspect"]:
                inspect_count += 1
                if inspect_count == 1:
                    if inspect_exception is not None:
                        if use_real_command_for_exception:
                            return real_command(argv, timeout=timeout, check=check,
                                                trace=trace, trace_action=trace_action)
                        raise inspect_exception
                    result = inspect_result or completed(1, stderr="Error: No such image: synthetic-tag")
                else:
                    result = completed(1, stderr="Error: No such image: synthetic-tag")
            elif argv[:4] == ["docker", "image", "rm", "--force"]:
                result = completed(1, stderr="Error: No such image: synthetic-tag")
            else:
                raise AssertionError(f"unexpected command reached test boundary: {argv!r}")
            trace_result(trace, argv, result, trace_action)
            return result

        def stop_before_build(argv: list[str], *, timeout: float) -> int:
            build_calls.append(list(argv))
            raise runner_probe.VerificationError("mocked build admission boundary; no subprocess launched")

        original_checkpoint = runner_probe._write_evidence_checkpoint
        def workspace_mkdtemp(*, prefix: str, dir: str | None = None) -> str:
            path = Path(dir or ROOT) / f"{prefix}{uuid.uuid4().hex}"
            path.mkdir()
            return str(path)

        def capture_checkpoint(path: Path, report: dict[str, object]) -> None:
            original_checkpoint(path, report)
            checkpoints.append(json.loads(path.read_text(encoding="utf-8")))

        with patch.dict(os.environ, env), \
             patch.object(runner_probe.subprocess, "run", side_effect=(
                 subprocess.TimeoutExpired("docker", 10) if use_real_command_for_exception
                 else forbidden_subprocess
             )), \
             patch.object(runner_probe, "verify_source", return_value={
                 "commit": runner_probe.SOURCE_COMMIT, "tree": runner_probe.SOURCE_TREE,
             }), \
             patch.object(runner_probe, "_command", side_effect=fake_command), \
             patch.object(runner_probe, "_stream_command", side_effect=stop_before_build), \
             patch.object(runner_probe.tempfile, "mkdtemp", side_effect=workspace_mkdtemp), \
             patch.object(runner_probe, "_write_evidence_checkpoint", side_effect=capture_checkpoint):
            report = runner_probe.execute(ROOT, evidence_path)
        return report, docker_calls, build_calls, checkpoints, evidence_path

    def test_existing_image_and_unverified_return_codes_fail_closed_and_export(self) -> None:
        cases = [
            ("present", completed(0, stdout='[{"Id":"sha256:existing"}]'), "present"),
            ("daemon", completed(1, stderr="Cannot connect to Docker daemon"), "unverified"),
            ("transport", completed(1, stderr="connection refused"), "unverified"),
            ("permission", completed(1, stderr="permission denied"), "unverified"),
            ("unknown", completed(1, stderr="unclassified inspect failure"), "unverified"),
        ]
        for name, result, expected_status in cases:
            with self.subTest(case=name):
                report, docker_calls, builds, checkpoints, evidence = self.run_preflight(result)
                receipt = report["prebuild_image_inspection"]
                self.assertEqual(report["status"], "failed")
                self.assertIn("a failure before those stages means they did not run",
                              report["synthetic_authority_note"])
                self.assertEqual(receipt["status"], expected_status)
                self.assertEqual(receipt["exit_code"], result.returncode)
                self.assertFalse(receipt["absence_verified"])
                self.assertEqual(receipt["stdout_sha256"], hashlib.sha256(result.stdout.encode()).hexdigest())
                self.assertEqual(receipt["stderr_sha256"], hashlib.sha256(result.stderr.encode()).hexdigest())
                self.assertTrue(receipt["trace"])
                self.assertIsNone(report["build"])
                self.assertFalse(report["image_cleanup"]["attempted"])
                self.assertEqual(builds, [])
                self.assertFalse(any(call[:3] == ["docker", "image", "rm"] for call in docker_calls))
                persisted = json.loads(evidence.read_text(encoding="utf-8"))
                self.assertEqual(persisted, report)
                self.assertEqual(checkpoints[-1]["status"], "failed")
                summary_path = ROOT / f".issue82-summary-{uuid.uuid4().hex}.md"
                self.addCleanup(lambda path=summary_path: path.unlink(missing_ok=True))
                code, output, summary = action_exporter(evidence, summary_path)
                self.assertEqual(code, 0)
                self.assertIn("**FAILED", output)
                self.assertIn('"prebuild_image_inspection"', output)
                self.assertEqual(output, summary)

    def test_raised_timeout_and_transport_errors_keep_unverified_checkpoint(self) -> None:
        cases: list[tuple[str, BaseException, bool]] = [
            ("timeout", subprocess.TimeoutExpired("docker", 10, output="partial"), True),
            ("missing-executable", FileNotFoundError("docker executable unavailable"), False),
        ]
        for name, error, through_real_command in cases:
            with self.subTest(case=name):
                report, docker_calls, builds, checkpoints, evidence = self.run_preflight(
                    inspect_exception=error,
                    use_real_command_for_exception=through_real_command,
                )
                receipt = report["prebuild_image_inspection"]
                self.assertEqual(report["status"], "failed")
                self.assertEqual(receipt["status"], "unverified")
                self.assertFalse(receipt["absence_verified"])
                self.assertEqual(receipt["exception"]["type"],
                                 "VerificationError" if through_real_command else "FileNotFoundError")
                self.assertEqual(receipt["exception"]["cause_type"],
                                 "TimeoutExpired" if through_real_command else None)
                self.assertTrue(receipt["trace"])
                self.assertIsNone(receipt["trace"][0]["returncode"])
                self.assertIsNone(report["build"])
                self.assertFalse(report["image_cleanup"]["attempted"])
                self.assertEqual(builds, [])
                self.assertFalse(any(call[:3] == ["docker", "image", "rm"] for call in docker_calls))
                self.assertEqual(checkpoints[1]["prebuild_image_inspection"]["status"], "running")
                self.assertEqual(json.loads(evidence.read_text(encoding="utf-8")), report)
                summary_path = ROOT / f".issue82-summary-{uuid.uuid4().hex}.md"
                self.addCleanup(lambda path=summary_path: path.unlink(missing_ok=True))
                code, output, summary = action_exporter(evidence, summary_path)
                self.assertEqual(code, 0)
                self.assertIn("**FAILED", output)
                self.assertIn('"exception"', output)
                self.assertEqual(output, summary)

    def test_explicit_absence_is_only_result_to_admit_one_mocked_build(self) -> None:
        result = completed(1, stderr="Error: No such image: synthetic-tag")
        report, calls, builds, checkpoints, evidence = self.run_preflight(result)
        self.assertEqual(report["prebuild_image_inspection"]["status"], "absent")
        self.assertTrue(report["prebuild_image_inspection"]["absence_verified"])
        self.assertEqual(len(builds), 1)
        self.assertEqual(builds[0][:3], ["docker", "buildx", "build"])
        self.assertEqual(report["build"]["attempt_count"], 1)
        self.assertEqual(report["build"]["status"], "failed")
        self.assertTrue(report["image_cleanup"]["attempted"])
        self.assertEqual(sum(call[:3] == ["docker", "image", "rm"] for call in calls), 1)
        self.assertEqual(checkpoints[-1]["status"], "failed")
        self.assertEqual(json.loads(evidence.read_text(encoding="utf-8")), report)


class CombinedRunnerBuildIdentityTests(unittest.TestCase):
    def run_identity(self, config_digest: str, backend_image_id: str) -> tuple[dict[str, object], list[list[str]], list[dict[str, object]], Path]:
        runner_temp = ROOT
        evidence_path = ROOT / f".issue82-identity-{uuid.uuid4().hex}.json"
        self.addCleanup(lambda: evidence_path.unlink(missing_ok=True))
        env = {
            "GITHUB_RUN_ID": "local-image-identity-regression", "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": f"refs/heads/{runner_probe.RUNNER_PACKAGE_BRANCH}",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_REPOSITORY": "LangkunLong/RS-momentum-EMA-trading-bot",
            "RUNNER_OS": "Linux", "RUNNER_ARCH": "X64", "RUNNER_TEMP": str(runner_temp),
        }
        manifest_digest = f"sha256:{'c' * 64}"
        metadata_raw = json.dumps({
            "containerimage.config.digest": config_digest,
            "containerimage.digest": manifest_digest,
            "containerimage.descriptor": {
                "digest": manifest_digest,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
            },
        }, sort_keys=True).encode()
        inspect_raw = json.dumps({
            "Id": backend_image_id, "Os": "linux", "Architecture": "amd64",
            "Config": {"Labels": {
                runner_probe.RUNTIME_KIND_LABEL: runner_probe.RUNTIME_KIND,
                runner_probe.RUNTIME_SOURCE_LABEL: runner_probe.RUNTIME_SOURCE_SHA256,
            }},
            "RepoDigests": [], "Descriptor": {"digest": manifest_digest,
                "mediaType": "application/vnd.oci.image.manifest.v1+json"},
            "RootFS": {"Layers": []},
        }, sort_keys=True)
        calls: list[list[str]] = []
        imports: list[dict[str, object]] = []
        checkpoints: list[dict[str, object]] = []
        inspect_count = 0
        real_checkpoint = runner_probe._write_evidence_checkpoint

        def fake_command(argv: list[str], *, timeout: float, check: bool = True,
                         trace: list[dict[str, object]] | None = None,
                         trace_action: str | None = None) -> subprocess.CompletedProcess[str]:
            nonlocal inspect_count
            calls.append(list(argv))
            if argv[:2] == ["docker", "version"]:
                result = completed(0, "test-engine linux/amd64")
            elif argv[:4] == ["docker", "image", "inspect", "--format"]:
                result = completed(0, inspect_raw)
            elif argv[:3] == ["docker", "image", "inspect"]:
                inspect_count += 1
                result = completed(1, stderr="Error: No such image: synthetic-tag")
            elif argv[:4] == ["docker", "image", "rm", "--force"]:
                result = completed(0, "sha256:removed")
            else:
                raise AssertionError(f"unexpected command: {argv!r}")
            trace_result(trace, argv, result, trace_action)
            return result

        def mocked_build(argv: list[str], *, timeout: float) -> int:
            metadata_path = Path(argv[argv.index("--metadata-file") + 1])
            metadata_path.write_bytes(metadata_raw)
            return 0

        def mocked_import(*, image: dict[str, object], evidence_id: str) -> dict[str, str]:
            imports.append({"image": image, "evidence_id": evidence_id})
            return {"failure": "mocked import boundary; no container created"}

        def capture_checkpoint(path: Path, report: dict[str, object]) -> None:
            real_checkpoint(path, report)
            checkpoints.append(json.loads(path.read_text(encoding="utf-8")))

        def workspace_mkdtemp(*, prefix: str, dir: str | None = None) -> str:
            path = Path(dir or ROOT) / f"{prefix}{uuid.uuid4().hex}"
            path.mkdir()
            return str(path)

        source_info = {
            "commit": runner_probe.SOURCE_COMMIT, "tree": runner_probe.SOURCE_TREE,
            "evaluator_path_count": 58,
            "evaluator_source_map_sha256": runner_probe.RUNTIME_SOURCE_SHA256,
        }
        with patch.dict(os.environ, env), \
             patch.object(runner_probe.subprocess, "run", side_effect=forbidden_subprocess), \
             patch.object(runner_probe, "verify_source", return_value=source_info), \
             patch.object(runner_probe, "_command", side_effect=fake_command), \
             patch.object(runner_probe, "_stream_command", side_effect=mocked_build), \
             patch.object(runner_probe, "_run_import_smoke", side_effect=mocked_import), \
             patch.object(runner_probe, "_run_case", side_effect=AssertionError("probe must not run")), \
             patch.object(runner_probe.os, "chmod", return_value=None), \
             patch.object(runner_probe.tempfile, "mkdtemp", side_effect=workspace_mkdtemp), \
             patch.object(runner_probe, "_write_evidence_checkpoint", side_effect=capture_checkpoint):
            report = runner_probe.execute(ROOT, evidence_path)
        return report, calls, imports, evidence_path

    def test_config_match_gate_compares_config_identity_and_blocks_mismatch_before_import(self) -> None:
        for matches in (False, True):
            with self.subTest(matches=matches):
                config = f"sha256:{'a' * 64}"
                backend = config if matches else f"sha256:{'b' * 64}"
                report, _calls, imports, evidence = self.run_identity(config, backend)
                runtime = report["runtime"]
                self.assertEqual(runtime["source_binding"], {
                    "commit": runner_probe.SOURCE_COMMIT,
                    "tree": runner_probe.SOURCE_TREE,
                    "runtime_source_sha256": runner_probe.RUNTIME_SOURCE_SHA256,
                })
                self.assertEqual(runtime["docker_backend_image_id"], backend)
                self.assertEqual(runtime["buildx_config_digest"], config)
                self.assertIs(runtime["buildx_config_matches_docker_backend_image_id"], matches)
                self.assertEqual(runtime["buildx_image_digest"], f"sha256:{'c' * 64}")
                self.assertNotEqual(runtime["buildx_image_digest"], backend)
                self.assertTrue(runtime["native_inspect_receipt"]["stdout_sha256"])
                self.assertTrue(runtime["native_inspect_receipt"]["command_argv"][:4] ==
                                ["docker", "image", "inspect", "--format"])
                self.assertEqual(report["status"], "failed")
                if matches:
                    self.assertEqual(len(imports), 1)
                    self.assertIn("import_smoke", report)
                else:
                    self.assertEqual(imports, [])
                    self.assertIn("Buildx config digest differs", report["failure"])
                    self.assertNotIn("import_smoke", report)
                self.assertEqual(json.loads(evidence.read_text(encoding="utf-8")), report)
                summary_path = ROOT / f".issue82-summary-{uuid.uuid4().hex}.md"
                self.addCleanup(lambda path=summary_path: path.unlink(missing_ok=True))
                code, output, summary = action_exporter(evidence, summary_path)
                self.assertEqual(code, 0)
                self.assertIn("**FAILED", output)
                self.assertIn("source_binding", output)
                self.assertEqual(output, summary)


if __name__ == "__main__":
    unittest.main()
