#!/usr/bin/env python3
"""One-time, synthetic V5 semantic-probe verification on a Linux Docker runner.

This stdlib-only host harness invokes the trusted image-owned probe entrypoint.
The candidate containers receive only four read-only policy files and a bounded
write-only output directory. No PIT data, providers, models, paper state, or
production campaign authority are used.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any


SOURCE_COMMIT = "b33d4fe1d5c88a6face2815fb3fdceaeb199b275"
SOURCE_TREE = "36676250cfc4f3703e42750eb2c0e56bdabe9102"
RUNTIME_SOURCE_SHA256 = "117fabb267ffc4294c3075e6965621d14c7bb6c901ae3e593950f334fd234ee0"
SOURCE_PIN_STATUS = "final-public"
RUNNER_PACKAGE_BRANCH = "codex/issue-82-combined-verification"
CALCULATOR_GIT_BLOB = "71e562ffc00dc4e617f1cd625e3f13ea5fe3f81a"
DOCKERFILE_GIT_BLOB = "10ae5c560bcca9e2294fc7caae05a5b72acfb97d"
DOCKERIGNORE_GIT_BLOB = "c10d0adaeb062eefd3a5e3424442b2190c60f4e4"
LOCK_GIT_BLOB = "69df1ab4c04282aec47076c5437d77215f413738"
POLICY_SCOPE_GIT_BLOB = "9e7591ad4d506ce57c2f52031c12c125624a76f0"
BASE_IMAGE = "python:3.13.14-slim@sha256:9662417aace5ae7b8e2609cce472b72a8958e134ba372808abe9cc1a0c0125e6"
RUNTIME_KIND = "evaluator-probe-v5"
RUNTIME_KIND_LABEL = "io.trading-bot.pit-v5.runtime-kind"
RUNTIME_SOURCE_LABEL = "io.trading-bot.pit-v5.runtime-source-sha256"
SUITE_ID = "pit-policy-v3-probes-v1"
POLICY_PATHS = (
    "core/strategy_policy/v3/entry.py",
    "core/strategy_policy/v3/risk.py",
    "core/strategy_policy/v3/position.py",
    "core/strategy_policy/v3/exit.py",
)
POLICY_COMMENT = b"# Issue 82 synthetic comment-only candidate fixture\n"
TRUSTED_RUNTIME_SHA256 = "1" * 64
IMMUTABLE_CONSTRAINTS_SHA256 = "2" * 64
MAX_OUTPUT_BYTES = 67_108_864
MAX_BUILD_METADATA_BYTES = 1_048_576
MAX_BUILD_SECONDS = 10 * 60
MECHANICS_TIMEOUT_SECONDS = 60
CONTAINER_UID_GID = "65532:65532"
IMPORT_SMOKE_PROGRAM = (
    "import core.data_client as data_client; "
    "import core.pit_data as pit_data; "
    "from core.alpaca_client_policy import configure_alpaca_rest_client; "
    "from core.backtest_engine import PortfolioSimulator; "
    "import core.pit_optimizer_v5.container_entry as container_entry; "
    "assert callable(configure_alpaca_rest_client); "
    "assert callable(pit_data.validate_price_identity_segments_v1); "
    "assert callable(data_client.fetch_bulk_close_prices); "
    "assert callable(PortfolioSimulator); "
    "assert callable(container_entry.main); "
    "print('issue82-import-smoke-ok')"
)
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class VerificationError(RuntimeError):
    pass


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _output_sha256(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode("utf-8")
    else:
        raw = b""
    return _sha256(raw)


def _digest_object(value: object) -> str:
    return _sha256(_canonical_bytes(value))


def _command(
    argv: list[str], *, timeout: float, check: bool = True,
    trace: list[dict[str, Any]] | None = None, trace_action: str | None = None,
) -> subprocess.CompletedProcess[str]:
    started_at = time.monotonic()
    try:
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        if trace is not None:
            trace.append({
                "action": trace_action or "command",
                "command_prefix": argv[:3],
                "command_sha256": _sha256(_canonical_bytes(argv)),
                "timeout_seconds": timeout,
                "elapsed_seconds": round(time.monotonic() - started_at, 3),
                "returncode": None,
                "timed_out": True,
                "stdout_sha256": _output_sha256(exc.stdout),
                "stderr_sha256": _output_sha256(exc.stderr),
            })
        raise VerificationError(
            f"command timed out after {timeout:g}s: {argv[0]} {argv[1] if len(argv) > 1 else ''}"
        ) from exc
    if trace is not None:
        trace.append({
            "action": trace_action or "command",
            "command_prefix": argv[:3],
            "command_sha256": _sha256(_canonical_bytes(argv)),
            "timeout_seconds": timeout,
            "elapsed_seconds": round(time.monotonic() - started_at, 3),
            "returncode": result.returncode,
            "timed_out": False,
            "stdout_sha256": _output_sha256(result.stdout),
            "stderr_sha256": _output_sha256(result.stderr),
        })
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout)[-6000:]
        raise VerificationError(
            f"command failed ({result.returncode}): {argv[0]} {argv[1] if len(argv) > 1 else ''}\n{detail}"
        )
    return result


def _image_absence_verified(result: subprocess.CompletedProcess[str]) -> bool:
    detail = f"{result.stdout}\n{result.stderr}"
    return result.returncode != 0 and "No such image" in detail


def _output_size_bytes(value: str | bytes | None) -> int:
    if isinstance(value, bytes):
        return len(value)
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    return 0


def _prebuild_image_inspection_receipt(
    image_tag: str,
    result: subprocess.CompletedProcess[str],
    trace: list[dict[str, Any]],
) -> dict[str, Any]:
    argv = ["docker", "image", "inspect", image_tag]
    absence_verified = _image_absence_verified(result)
    status = "present" if result.returncode == 0 else "absent" if absence_verified else "unverified"
    return {
        "status": status,
        "command_argv": argv,
        "command_sha256": _sha256(_canonical_bytes(argv)),
        "exit_code": result.returncode,
        "stdout_size_bytes": _output_size_bytes(result.stdout),
        "stdout_sha256": _output_sha256(result.stdout),
        "stderr_size_bytes": _output_size_bytes(result.stderr),
        "stderr_sha256": _output_sha256(result.stderr),
        "absence_verified": absence_verified,
        "trace": trace,
    }


def _prebuild_image_inspection_attempt(image_tag: str) -> dict[str, Any]:
    argv = ["docker", "image", "inspect", image_tag]
    return {
        "status": "running",
        "command_argv": argv,
        "command_sha256": _sha256(_canonical_bytes(argv)),
        "exit_code": None,
        "stdout_size_bytes": None,
        "stdout_sha256": None,
        "stderr_size_bytes": None,
        "stderr_sha256": None,
        "absence_verified": False,
        "trace": [],
    }


def _stream_command(argv: list[str], *, timeout: float) -> int:
    """Stream the networked build log to Actions without buffering it in memory."""
    try:
        result = subprocess.run(argv, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise VerificationError(f"Docker image build exceeded its {timeout:g}s bound") from exc
    if result.returncode != 0:
        raise VerificationError(f"Docker image build failed with exit code {result.returncode}; see the retained Actions log")
    return result.returncode


def _git(source_root: Path, *args: str, timeout: float = 20) -> str:
    return _command(
        ["git", "-C", str(source_root), *args], timeout=timeout
    ).stdout.strip()


def _git_blob(source_root: Path, revision: str, relative_path: str) -> bytes:
    return subprocess.check_output(
        ["git", "-C", str(source_root), "cat-file", "blob", f"{revision}:{relative_path}"],
        timeout=20,
    )


def _assignment(tree: ast.Module, name: str) -> object:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise VerificationError(f"required closed source declaration is absent: {name}")


def verify_source(source_root: Path, *, require_clean_checkout: bool = True) -> dict[str, Any]:
    root = source_root.resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise VerificationError("pinned evaluator source root is not a regular directory")
    observed_commit = _git(root, "rev-parse", "HEAD")
    if require_clean_checkout and observed_commit != SOURCE_COMMIT:
        raise VerificationError(f"evaluator commit mismatch: {observed_commit}")
    if require_clean_checkout and _git(root, "status", "--porcelain=v1"):
        raise VerificationError("pinned evaluator checkout is not clean")
    observed_tree = _git(root, "rev-parse", f"{SOURCE_COMMIT}^{{tree}}")
    if observed_tree != SOURCE_TREE:
        raise VerificationError(f"pinned evaluator tree mismatch: {observed_tree}")

    contracts_raw = _git_blob(root, SOURCE_COMMIT, "core/pit_optimizer_v5/contracts.py")
    contracts_tree = ast.parse(contracts_raw.decode("utf-8"))
    source_paths = _assignment(contracts_tree, "EVALUATOR_SOURCE_PATHS_V5")
    if not isinstance(source_paths, tuple) or len(source_paths) != 58:
        raise VerificationError("pinned evaluator closure is not the expected 58-path tuple")
    if tuple(sorted(source_paths)) != tuple(sorted(set(source_paths))):
        raise VerificationError("pinned evaluator source closure contains duplicate paths")
    dockerignore_lines = _git_blob(
        root, SOURCE_COMMIT, "Dockerfile.pit-optimizer-v5.dockerignore"
    ).decode("utf-8").splitlines()
    allowed_files = {
        line[1:]
        for line in dockerignore_lines
        if line.startswith("!") and not line.endswith("/")
    }
    if allowed_files != set(source_paths) | {
        "Dockerfile.pit-optimizer-v5",
        "Dockerfile.pit-optimizer-v5.dockerignore",
        "requirements-lock.txt",
    }:
        raise VerificationError("Dockerfile-specific ignore allowlist differs from the closed build context")
    policy_scope_raw = _git_blob(root, SOURCE_COMMIT, "core/pit_optimizer_v5/policy_scope.py")
    policy_scope_tree = ast.parse(policy_scope_raw.decode("utf-8"))
    policy_paths = _assignment(policy_scope_tree, "EDITABLE_POLICY_PATHS_V5")
    if policy_paths != POLICY_PATHS:
        raise VerificationError("pinned editable policy scope differs from the four mounted files")

    raw_map: dict[str, str] = {}
    for relative in source_paths:
        if not isinstance(relative, str) or PurePosixPath(relative).as_posix() != relative:
            raise VerificationError("pinned evaluator source closure contains a noncanonical path")
        raw = _git_blob(root, SOURCE_COMMIT, relative)
        if require_clean_checkout:
            checked_out = root.joinpath(*PurePosixPath(relative).parts)
            if checked_out.is_symlink() or not checked_out.is_file() or checked_out.read_bytes() != raw:
                raise VerificationError(f"checkout bytes differ from raw Git blob: {relative}")
        raw_map[relative] = _sha256(raw)
    observed_map = _digest_object(raw_map)
    if observed_map != RUNTIME_SOURCE_SHA256:
        raise VerificationError(f"raw Git evaluator-map mismatch: {observed_map}")

    fixed_blobs = {
        "core/pit_feature_snapshot.py": CALCULATOR_GIT_BLOB,
        "Dockerfile.pit-optimizer-v5": DOCKERFILE_GIT_BLOB,
        "Dockerfile.pit-optimizer-v5.dockerignore": DOCKERIGNORE_GIT_BLOB,
        "requirements-lock.txt": LOCK_GIT_BLOB,
        "core/pit_optimizer_v5/policy_scope.py": POLICY_SCOPE_GIT_BLOB,
    }
    for relative, expected in fixed_blobs.items():
        observed = _git(root, "rev-parse", f"{SOURCE_COMMIT}:{relative}")
        if observed != expected:
            raise VerificationError(f"pinned Git blob mismatch for {relative}: {observed}")
        if require_clean_checkout and (root / relative).read_bytes() != _git_blob(root, SOURCE_COMMIT, relative):
            raise VerificationError(f"checkout bytes differ from raw Git blob: {relative}")
    dockerfile = _git_blob(root, SOURCE_COMMIT, "Dockerfile.pit-optimizer-v5").decode("utf-8")
    if f"FROM {BASE_IMAGE}" not in dockerfile or "PIT_V5_RUNTIME_SOURCE_SHA256" not in dockerfile:
        raise VerificationError("pinned Dockerfile does not use the expected base or runtime identity input")

    return {
        "commit": SOURCE_COMMIT,
        "observed_checkout_head": observed_commit,
        "tree": observed_tree,
        "evaluator_path_count": len(source_paths),
        "evaluator_source_map_sha256": observed_map,
        "calculator_path": "core/pit_feature_snapshot.py",
        "calculator_git_blob": CALCULATOR_GIT_BLOB,
        "dockerfile_git_blob": DOCKERFILE_GIT_BLOB,
        "dockerignore_git_blob": DOCKERIGNORE_GIT_BLOB,
        "requirements_lock_git_blob": LOCK_GIT_BLOB,
        "policy_scope_git_blob": POLICY_SCOPE_GIT_BLOB,
        "requirements_lock_bytes_sha256": _sha256((root / "requirements-lock.txt").read_bytes()),
    }


def _policy_sources(source_root: Path, label: str) -> tuple[dict[str, bytes], str, str]:
    parent: dict[str, bytes] = {}
    for relative in POLICY_PATHS:
        raw = _git_blob(source_root, SOURCE_COMMIT, relative)
        if not raw.endswith(b"\n") or b"\r" in raw:
            raise VerificationError(f"policy source is not canonical LF text: {relative}")
        parent[relative] = raw
    candidate = dict(parent)
    candidate[POLICY_PATHS[-1]] += POLICY_COMMENT
    if any(path != POLICY_PATHS[-1] and candidate[path] != parent[path] for path in POLICY_PATHS):
        raise VerificationError("candidate changed more than the intended exit-policy comment")
    if candidate[POLICY_PATHS[-1]] != parent[POLICY_PATHS[-1]] + POLICY_COMMENT:
        raise VerificationError("candidate is not the exact comment-only source variant")

    selected = parent if label == "parent" else candidate
    for relative, raw in selected.items():
        compile(raw.decode("utf-8", errors="strict"), relative, "exec", dont_inherit=True)
    bundle = {
        "files": [
            {"path": relative, "source": selected[relative].decode("utf-8", errors="strict")}
            for relative in POLICY_PATHS
        ]
    }
    source_bundle_sha256 = _digest_object(bundle)
    revision = {
        "policy_interface_version": 3,
        "trusted_policy_runtime_sha256": TRUSTED_RUNTIME_SHA256,
        "immutable_constraints_sha256": IMMUTABLE_CONSTRAINTS_SHA256,
        "editable_source_sha256": [
            [relative, _sha256(selected[relative])] for relative in POLICY_PATHS
        ],
    }
    return selected, source_bundle_sha256, _digest_object(revision)


def _image_identity(image_tag: str) -> dict[str, Any]:
    argv = ["docker", "image", "inspect", "--format", "{{json .}}", image_tag]
    raw = _command(
        argv, timeout=15
    ).stdout
    image = json.loads(raw)
    image_id = image.get("Id")
    if not isinstance(image_id, str) or not image_id.startswith("sha256:"):
        raise VerificationError("built image has no Docker backend image ID")
    if image.get("Os") != "linux" or image.get("Architecture") != "amd64":
        raise VerificationError("built evaluator image is not linux/amd64")
    config = image.get("Config") or {}
    labels = config.get("Labels") or {}
    if labels.get(RUNTIME_KIND_LABEL) != RUNTIME_KIND:
        raise VerificationError("built image runtime-kind label differs")
    if labels.get(RUNTIME_SOURCE_LABEL) != RUNTIME_SOURCE_SHA256:
        raise VerificationError("built image runtime source label differs")
    return {
        "repository_tag": image_tag,
        "docker_backend_image_id": image_id,
        "native_inspect_receipt": {
            "command_argv": argv,
            "command_sha256": _sha256(_canonical_bytes(argv)),
            "stdout_size_bytes": _output_size_bytes(raw),
            "stdout_sha256": _output_sha256(raw),
        },
        "repo_digests": image.get("RepoDigests") or [],
        "image_descriptor_digest": (image.get("Descriptor") or {}).get("digest"),
        "image_descriptor_media_type": (image.get("Descriptor") or {}).get("mediaType"),
        "platform": "linux/amd64",
        "rootfs_layer_digests": (image.get("RootFS") or {}).get("Layers", []),
        "runtime_kind": labels.get(RUNTIME_KIND_LABEL),
        "runtime_source_sha256": labels.get(RUNTIME_SOURCE_LABEL),
    }


def _parse_build_metadata(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > MAX_BUILD_METADATA_BYTES:
        raise VerificationError("Buildx metadata file is empty or exceeds the size limit")
    try:
        metadata = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError("Buildx metadata file is not valid UTF-8 JSON") from exc
    if not isinstance(metadata, dict):
        raise VerificationError("Buildx metadata root must be an object")

    config_digest = metadata.get("containerimage.config.digest")
    if not isinstance(config_digest, str) or not _SHA256_DIGEST.fullmatch(config_digest):
        raise VerificationError("Buildx metadata config digest is missing or noncanonical")
    image_digest = metadata.get("containerimage.digest")
    if not isinstance(image_digest, str) or not _SHA256_DIGEST.fullmatch(image_digest):
        raise VerificationError("Buildx metadata image digest is missing or noncanonical")
    descriptor = metadata.get("containerimage.descriptor")
    if not isinstance(descriptor, dict):
        raise VerificationError("Buildx metadata image descriptor is missing")
    descriptor_digest = descriptor.get("digest")
    if not isinstance(descriptor_digest, str) or not _SHA256_DIGEST.fullmatch(descriptor_digest):
        raise VerificationError("Buildx metadata descriptor digest is missing or noncanonical")
    if descriptor_digest != image_digest:
        raise VerificationError("Buildx metadata image and descriptor digests differ")
    media_type = descriptor.get("mediaType")
    if not isinstance(media_type, str) or not media_type:
        raise VerificationError("Buildx metadata descriptor media type is missing")

    return {
        "metadata_file_sha256": _sha256(raw),
        "metadata_file_size_bytes": len(raw),
        "buildx_image_digest": image_digest,
        "buildx_config_digest": config_digest,
        "buildx_descriptor_digest": descriptor_digest,
        "buildx_descriptor_media_type": media_type,
    }


def _inspect_container(
    container_id: str, *, trace: list[dict[str, Any]] | None = None,
    trace_action: str = "inspect_container",
) -> dict[str, Any]:
    raw = _command(
        ["docker", "container", "inspect", "--format", "{{json .}}", container_id],
        timeout=10,
        trace=trace,
        trace_action=trace_action,
    ).stdout
    return json.loads(raw)


def _run_import_smoke(*, image: dict[str, Any], evidence_id: str) -> dict[str, Any]:
    """Prove key runtime imports resolve in the isolated built image."""
    name = f"pit-v5-issue82-{evidence_id}-import-smoke-{secrets.token_hex(4)}"
    owned_label = f"io.trading-bot.issue-82-run={evidence_id}"
    command = [
        "docker", "container", "create",
        "--name", name,
        "--label", owned_label,
        "--pull", "never",
        "--network", "none",
        "--read-only",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--pids-limit", "32",
        "--cpus", "1",
        "--memory", "1024m",
        "--memory-swap", "1024m",
        "--log-driver", "none",
        "--user", CONTAINER_UID_GID,
        "--entrypoint", "python",
        "--workdir", "/",
        image["repository_tag"],
        "-P", "-B", "-c", IMPORT_SMOKE_PROGRAM,
    ]
    create_attempted = False
    container_id: str | None = None
    container_image_id: str | None = None
    create_exit_code: int | None = None
    create_error: str | None = None
    start_exit_code: int | None = None
    stdout = ""
    stderr = ""
    state: dict[str, Any] = {}
    failure: str | None = None
    docker_control_trace: list[dict[str, Any]] = []
    cleanup: dict[str, Any] = {
        "attempted": False,
        "container_removed": False,
        "absence_check_exit_code": None,
        "absence_check_names": None,
        "cleanup_complete": False,
    }
    try:
        create_attempted = True
        created = _command(
            command, timeout=20, check=False,
            trace=docker_control_trace, trace_action="create_import_smoke",
        )
        create_exit_code = created.returncode
        if created.returncode != 0:
            create_error = (created.stderr or created.stdout)[-2000:]
        container_id = created.stdout.strip() if created.returncode == 0 else None
        if created.returncode != 0 or not _HEX64.fullmatch(container_id or ""):
            failure = "Docker did not create the import-smoke container"
        else:
            inspected = _inspect_container(
                container_id, trace=docker_control_trace,
                trace_action="inspect_import_smoke_created",
            )
            config = inspected.get("Config") or {}
            host = inspected.get("HostConfig") or {}
            container_image_id = inspected.get("Image")
            if (
                config.get("Image") != image["repository_tag"]
                or container_image_id != image["docker_backend_image_id"]
                or config.get("User") != CONTAINER_UID_GID
                or host.get("NetworkMode") != "none"
                or host.get("ReadonlyRootfs") is not True
                or "ALL" not in (host.get("CapDrop") or [])
                or "no-new-privileges:true" not in (host.get("SecurityOpt") or [])
                or host.get("PidsLimit") != 32
                or host.get("NanoCpus") != 1_000_000_000
                or host.get("Memory") != 1_073_741_824
                or host.get("MemorySwap") != 1_073_741_824
                or inspected.get("Mounts")
            ):
                failure = "import-smoke image binding or no-mount isolation profile failed"
            else:
                started = _command(
                    ["docker", "container", "start", "--attach", container_id],
                    timeout=MECHANICS_TIMEOUT_SECONDS,
                    check=False,
                    trace=docker_control_trace,
                    trace_action="start_import_smoke",
                )
                start_exit_code = started.returncode
                stdout = started.stdout
                stderr = started.stderr
                state = _inspect_container(
                    container_id, trace=docker_control_trace,
                    trace_action="inspect_import_smoke_terminal",
                ).get("State") or {}
                if (
                    start_exit_code != 0
                    or state.get("Status") != "exited"
                    or state.get("ExitCode") != 0
                    or stdout.strip() != "issue82-import-smoke-ok"
                ):
                    failure = "in-image runtime imports did not complete successfully"
    except BaseException as exc:
        failure = str(exc) or type(exc).__name__
    finally:
        cleanup["attempted"] = create_attempted
        if create_attempted:
            target = container_id or name
            try:
                removed = _command(
                    ["docker", "container", "rm", "--force", target],
                    timeout=15,
                    check=False,
                    trace=docker_control_trace,
                    trace_action="remove_import_smoke",
                )
                cleanup["container_removed"] = removed.returncode == 0
                absence = _command(
                    ["docker", "container", "ls", "--all", "--quiet", "--no-trunc", "--filter", f"name=^{name}$"],
                    timeout=10,
                    check=False,
                    trace=docker_control_trace,
                    trace_action="check_import_smoke_absent",
                )
                absence_names = [line.strip() for line in absence.stdout.splitlines() if line.strip()]
                cleanup["absence_check_exit_code"] = absence.returncode
                cleanup["absence_check_names"] = absence_names
                cleanup["cleanup_complete"] = absence.returncode == 0 and not absence_names
            except BaseException as exc:
                cleanup["cleanup_error"] = str(exc) or type(exc).__name__
        else:
            cleanup["cleanup_complete"] = True

    if not cleanup["cleanup_complete"]:
        failure = failure or "import-smoke container cleanup or targeted absence check failed"
    return {
        "container_name": name,
        "container_id": container_id,
        "image_reference": image["repository_tag"],
        "expected_docker_backend_image_id": image["docker_backend_image_id"],
        "container_image_id": container_image_id,
        "command_sha256": _sha256(_canonical_bytes(command)),
        "import_targets": [
            "core.data_client",
            "core.pit_data.validate_price_identity_segments_v1",
            "core.alpaca_client_policy.configure_alpaca_rest_client",
            "core.pit_optimizer_v5.container_entry",
            "core.backtest_engine.PortfolioSimulator",
        ],
        "network": "none",
        "root_filesystem": "read_only",
        "mounts": [],
        "uid_gid": CONTAINER_UID_GID,
        "cpu_limit": 1,
        "memory_limit_mib": 1024,
        "memory_swap_limit_mib": 1024,
        "pid_limit": 32,
        "create_exit_code": create_exit_code,
        "create_error": create_error,
        "start_exit_code": start_exit_code,
        "container_status": state.get("Status"),
        "container_exit_code": state.get("ExitCode"),
        "stdout_sha256": _sha256(stdout.encode("utf-8")),
        "stderr_sha256": _sha256(stderr.encode("utf-8")),
        "cleanup": cleanup,
        "docker_control_trace": docker_control_trace,
        "control_trace_sha256": _sha256(_canonical_bytes(docker_control_trace)),
        "cleanup_trace": [
            item for item in docker_control_trace
            if item["action"] in {"remove_import_smoke", "check_import_smoke_absent"}
        ],
        "failure": failure,
    }


def _container_request(
    *, label: str, policy_revision_sha256: str, evaluator_contract_sha256: str,
    sandbox_profile_sha256: str,
) -> tuple[str, dict[str, str]]:
    request = {
        "domain": "issue-82-synthetic-semantic-probe-request-v1",
        "case": label,
        "source_commit": SOURCE_COMMIT,
        "runtime_source_sha256": RUNTIME_SOURCE_SHA256,
        "policy_revision_sha256": policy_revision_sha256,
        "evaluator_contract_sha256": evaluator_contract_sha256,
        "sandbox_profile_sha256": sandbox_profile_sha256,
        "suite_id": SUITE_ID,
        "scenario_ids": [],
        "production_pit_data_mounted": False,
    }
    request_sha256 = _digest_object(request)
    authority_sha256 = _digest_object(
        {
            "domain": "pit-optimizer-v5-trusted-probe-runtime-v1",
            "evaluator_contract_sha256": evaluator_contract_sha256,
            "sandbox_profile_sha256": sandbox_profile_sha256,
            "runtime_source_sha256": RUNTIME_SOURCE_SHA256,
            "probe_suite_id": SUITE_ID,
        }
    )
    return request_sha256, {
        "--request-sha256": request_sha256,
        "--policy-sha256": policy_revision_sha256,
        "--trusted-runtime-sha256": TRUSTED_RUNTIME_SHA256,
        "--immutable-constraints-sha256": IMMUTABLE_CONSTRAINTS_SHA256,
        "--suite-id": SUITE_ID,
        "--evaluator-contract-sha256": evaluator_contract_sha256,
        "--sandbox-profile-sha256": sandbox_profile_sha256,
        "--probe-runtime-sha256": RUNTIME_SOURCE_SHA256,
        "--probe-runtime-authority-sha256": authority_sha256,
        "--call-timeout-seconds": "1",
        "--output-limit-bytes": str(MAX_OUTPUT_BYTES),
    }


def _run_case(
    *, source_root: Path, temp_root: Path, label: str, image: dict[str, Any],
    source_map: dict[str, Any], source_bundle_sha256: str, policy_revision_sha256: str,
    evidence_id: str,
) -> dict[str, Any]:
    overlay_root = temp_root / f"{label}-policy"
    output_root = temp_root / f"{label}-output"
    overlay_root.mkdir(mode=0o755)
    output_root.mkdir(mode=0o777)
    os.chmod(overlay_root, 0o755)
    os.chmod(output_root, 0o777)
    selected, _bundle_sha, _revision_sha = _policy_sources(source_root, label)
    for relative, raw in selected.items():
        destination = overlay_root / relative.rsplit("/", 1)[-1]
        destination.write_bytes(raw)
        os.chmod(destination, 0o444)

    # The envelope is deliberately synthetic and grants no evaluator or campaign authority.
    sandbox_payload = {
        "schema_version": 5,
        "image_name": "pit-optimizer-v5-evaluator",
        "image_digest": image["docker_backend_image_id"],
        "runtime_source_sha256": RUNTIME_SOURCE_SHA256,
        "network_mode": "none",
        "root_filesystem": "read_only",
        "source_mount_mode": "read_only",
        "data_mount_mode": "read_only",
        "output_mode": "bounded_write_only",
        "cpu_limit": "1",
        "memory_limit_mib": 1024,
        "output_limit_bytes": MAX_OUTPUT_BYTES,
        "pid_limit": 32,
    }
    sandbox_profile_sha256 = _digest_object(sandbox_payload)
    evaluator_payload = {
        "domain": "issue-82-synthetic-evaluator-contract-v1",
        "evaluator_source_sha256": RUNTIME_SOURCE_SHA256,
        "sandbox_profile_sha256": sandbox_profile_sha256,
        "base_image": BASE_IMAGE,
        "probe_suite_id": SUITE_ID,
    }
    evaluator_contract_sha256 = _digest_object(evaluator_payload)
    request_sha256, request_args = _container_request(
        label=label,
        policy_revision_sha256=policy_revision_sha256,
        evaluator_contract_sha256=evaluator_contract_sha256,
        sandbox_profile_sha256=sandbox_profile_sha256,
    )
    name = f"pit-v5-issue82-{evidence_id}-{label}-{secrets.token_hex(4)}"
    owned_label = f"io.trading-bot.issue-82-run={evidence_id}"
    command = [
        "docker", "container", "create",
        "--name", name,
        "--label", owned_label,
        "--pull", "never",
        "--network", "none",
        "--read-only",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--pids-limit", "32",
        "--cpus", "1",
        "--memory", "1024m",
        "--log-driver", "none",
        "--user", CONTAINER_UID_GID,
        "--entrypoint", "python",
        "--workdir", "/",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m",
    ]
    for relative in POLICY_PATHS:
        source_path = overlay_root / relative.rsplit("/", 1)[-1]
        target_path = "/pit/candidate/" + relative.rsplit("/", 1)[-1]
        command.extend((
            "--mount", f"type=bind,src={source_path},dst={target_path},readonly"
        ))
    command.extend(("--mount", f"type=bind,src={output_root},dst=/pit/output"))
    command.extend((image["docker_backend_image_id"], "-P", "-B", "-m", "core.pit_optimizer_v5.probe_entry"))
    for option, value in request_args.items():
        command.extend((option, value))

    create_started = False
    container_id: str | None = None
    status: dict[str, Any] = {}
    cleanup: dict[str, Any] = {
        "attempted": False,
        "cleanup_complete": False,
        "remove_exit_code": None,
        "absence_check_exit_code": None,
        "absence_check_names": None,
        "container_removed": False,
    }
    failure: str | None = None
    output_bytes = b""
    create_exit_code: int | None = None
    create_error: str | None = None
    start_result_code: int | None = None
    start_timed_out = False
    docker_control_trace: list[dict[str, Any]] = []
    observed_container_image_id: str | None = None
    observed_config_image: str | None = None
    observed_isolation: dict[str, Any] | None = None
    try:
        create_started = True
        created = _command(
            command, timeout=20, check=False,
            trace=docker_control_trace, trace_action="create_probe_container",
        )
        create_exit_code = created.returncode
        if created.returncode != 0:
            create_error = (created.stderr or created.stdout)[-2000:]
        possible_container_id = created.stdout.strip()
        container_id = possible_container_id if _HEX64.fullmatch(possible_container_id) else None
        if created.returncode != 0 or container_id is None:
            raise VerificationError("docker create did not return one full container ID")
        inspected = _inspect_container(
            container_id, trace=docker_control_trace,
            trace_action="inspect_probe_container_created",
        )
        config = inspected.get("Config") or {}
        host = inspected.get("HostConfig") or {}
        mounts = inspected.get("Mounts") or []
        observed_container_image_id = inspected.get("Image")
        observed_config_image = config.get("Image")
        observed_isolation = {
            "user": config.get("User"),
            "network_mode": host.get("NetworkMode"),
            "read_only_root": host.get("ReadonlyRootfs"),
            "cap_drop": sorted(host.get("CapDrop") or []),
            "security_options": sorted(host.get("SecurityOpt") or []),
            "pid_limit": host.get("PidsLimit"),
            "nano_cpus": host.get("NanoCpus"),
            "memory_bytes": host.get("Memory"),
            "memory_swap_bytes": host.get("MemorySwap"),
            "tmpfs": host.get("Tmpfs") or {},
            "log_driver": (host.get("LogConfig") or {}).get("Type"),
            "mounts": [
                {
                    "type": mount.get("Type"),
                    "destination": mount.get("Destination"),
                    "mode": mount.get("Mode"),
                    "read_write": mount.get("RW"),
                    "propagation": mount.get("Propagation"),
                }
                for mount in sorted(mounts, key=lambda item: str(item.get("Destination", "")))
            ],
        }
        expected_mount_targets = {
            *("/pit/candidate/" + path.rsplit("/", 1)[-1] for path in POLICY_PATHS),
            "/pit/output",
        }
        actual_mount_targets = {mount.get("Destination") for mount in mounts}
        if (
            observed_container_image_id != image["docker_backend_image_id"]
            or observed_config_image != image["docker_backend_image_id"]
            or config.get("User") != CONTAINER_UID_GID
            or host.get("NetworkMode") != "none"
            or host.get("ReadonlyRootfs") is not True
            or "ALL" not in (host.get("CapDrop") or [])
            or "no-new-privileges:true" not in (host.get("SecurityOpt") or [])
            or host.get("PidsLimit") != 32
            or host.get("NanoCpus") != 1_000_000_000
            or host.get("Memory") != 1_073_741_824
            or actual_mount_targets != expected_mount_targets
            or any(
                mount.get("RW") is not False
                for mount in mounts
                if mount.get("Destination") != "/pit/output"
            )
            or sum(mount.get("Destination") == "/pit/output" and mount.get("RW") is True for mount in mounts) != 1
        ):
            raise VerificationError("created container does not satisfy the closed isolation profile")
        start_started_at = time.monotonic()
        try:
            started = subprocess.run(
                ["docker", "container", "start", "--attach", container_id],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=MECHANICS_TIMEOUT_SECONDS,
            )
            start_result_code = started.returncode
            docker_control_trace.append({
                "action": "start_probe_container",
                "command_prefix": ["docker", "container", "start"],
                "command_sha256": _sha256(_canonical_bytes(["docker", "container", "start", "--attach", container_id])),
                "timeout_seconds": MECHANICS_TIMEOUT_SECONDS,
                "elapsed_seconds": round(time.monotonic() - start_started_at, 3),
                "returncode": start_result_code,
                "timed_out": False,
                "stdout_stderr": "discarded",
            })
        except subprocess.TimeoutExpired:
            start_timed_out = True
            failure = f"candidate container exceeded {MECHANICS_TIMEOUT_SECONDS}s mechanics timeout"
            docker_control_trace.append({
                "action": "start_probe_container",
                "command_prefix": ["docker", "container", "start"],
                "command_sha256": _sha256(_canonical_bytes(["docker", "container", "start", "--attach", container_id])),
                "timeout_seconds": MECHANICS_TIMEOUT_SECONDS,
                "elapsed_seconds": round(time.monotonic() - start_started_at, 3),
                "returncode": None,
                "timed_out": True,
                "stdout_stderr": "discarded",
            })
        state = _inspect_container(
            container_id, trace=docker_control_trace,
            trace_action="inspect_probe_container_terminal",
        ).get("State") or {}
        status = {
            "container_status": state.get("Status"),
            "container_exit_code": state.get("ExitCode"),
            "container_oom_killed": state.get("OOMKilled"),
            "container_error": state.get("Error"),
            "start_command_exit_code": start_result_code,
            "start_timed_out": start_timed_out,
            "container_stream_output_capture": "discarded; bounded result is read from the output mount",
        }
        output_path = output_root / "semantic-fingerprint.json"
        if not failure:
            output_names = sorted(path.name for path in output_root.iterdir())
            if (
                state.get("Status") != "exited"
                or state.get("ExitCode") != 0
                or start_result_code != 0
                or state.get("OOMKilled") is not False
                or not output_path.is_file()
                or output_path.is_symlink()
                or output_path.stat().st_size <= 0
                or output_path.stat().st_size > MAX_OUTPUT_BYTES
                or output_names != ["semantic-fingerprint.json"]
            ):
                failure = "candidate container did not exit cleanly with its expected output"
            else:
                _command(["sudo", "-n", "chmod", "0644", "--", str(output_path)], timeout=10)
                output_bytes = output_path.read_bytes()
                if not output_bytes or len(output_bytes) > MAX_OUTPUT_BYTES:
                    failure = "candidate output is empty or exceeds the 64 MiB limit"
    except BaseException as exc:
        failure = str(exc) or type(exc).__name__
    finally:
        cleanup["attempted"] = create_started
        if create_started:
            target = container_id or name
            try:
                removed = _command(
                    ["docker", "container", "rm", "--force", target],
                    timeout=15,
                    check=False,
                    trace=docker_control_trace,
                    trace_action="remove_probe_container",
                )
                cleanup["container_removed"] = removed.returncode == 0
                cleanup["remove_exit_code"] = removed.returncode
                absence = _command(
                    ["docker", "container", "ls", "--all", "--quiet", "--no-trunc", "--filter", f"name=^{name}$"],
                    timeout=10,
                    check=False,
                    trace=docker_control_trace,
                    trace_action="check_probe_container_absent",
                )
                absence_names = [line.strip() for line in absence.stdout.splitlines() if line.strip()]
                cleanup["absence_check_exit_code"] = absence.returncode
                cleanup["absence_check_names"] = absence_names
                cleanup["cleanup_complete"] = absence.returncode == 0 and not absence_names
            except BaseException as exc:
                cleanup["cleanup_error"] = str(exc) or type(exc).__name__
            if not cleanup["cleanup_complete"]:
                failure = failure or "owned candidate container cleanup or targeted absence check failed"
        else:
            cleanup["cleanup_complete"] = True

    output_payload: dict[str, Any] | None = None
    fingerprint: dict[str, Any] | None = None
    if output_bytes and not failure:
        try:
            output_payload = json.loads(output_bytes.decode("utf-8"))
            if _canonical_bytes(output_payload) != output_bytes:
                raise VerificationError("candidate output is not canonical JSON")
            if set(output_payload) != {"fingerprint", "policy_revision_sha256", "request_sha256", "suite_id"}:
                raise VerificationError("candidate output fields differ from the V5 probe protocol")
            if (
                output_payload["request_sha256"] != request_sha256
                or output_payload["policy_revision_sha256"] != policy_revision_sha256
                or output_payload["suite_id"] != SUITE_ID
            ):
                raise VerificationError("candidate output identity differs from its request")
            fingerprint = output_payload["fingerprint"]
            if (
                not isinstance(fingerprint, dict)
                or fingerprint.get("suite_id") != SUITE_ID
                or not isinstance(fingerprint.get("observations"), list)
                or len(fingerprint["observations"]) != 11
                or not _HEX64.fullmatch(str(fingerprint.get("fingerprint_sha256", "")))
            ):
                raise VerificationError("candidate semantic fingerprint differs from the fixed 11-probe suite")
        except (UnicodeError, json.JSONDecodeError, VerificationError, TypeError) as exc:
            failure = str(exc) or type(exc).__name__

    return {
        "label": label,
        "source_bundle_sha256": source_bundle_sha256,
        "policy_revision_sha256": policy_revision_sha256,
        "editable_source_sha256": {path: _sha256(raw) for path, raw in selected.items()},
        "editable_source_git_blobs": {
            path: _git(source_root, "rev-parse", f"{SOURCE_COMMIT}:{path}")
            for path in POLICY_PATHS
        },
        "sandbox_profile_sha256": sandbox_profile_sha256,
        "evaluator_contract_sha256": evaluator_contract_sha256,
        "request_sha256": request_sha256,
        "command_argv": command,
        "command_sha256": _sha256(_canonical_bytes(command)),
        "create_exit_code": create_exit_code,
        "create_error": create_error,
        "container_id": container_id,
        "expected_docker_backend_image_id": image["docker_backend_image_id"],
        "observed_container_image_id": observed_container_image_id,
        "observed_config_image": observed_config_image,
        "observed_isolation": observed_isolation,
        "observed_isolation_sha256": (
            None if observed_isolation is None else _digest_object(observed_isolation)
        ),
        "observed_container_inspection_sha256": (
            None
            if observed_isolation is None
            else _digest_object(
                {
                    "container_image_id": observed_container_image_id,
                    "config_image": observed_config_image,
                    "isolation": observed_isolation,
                }
            )
        ),
        **status,
        "output_observed_bytes": len(output_bytes),
        "output_sha256": _sha256(output_bytes) if output_bytes else None,
        "fingerprint_sha256": None if fingerprint is None else fingerprint.get("fingerprint_sha256"),
        "observation_count": None if fingerprint is None else len(fingerprint.get("observations", [])),
        "runtime_network_none": True,
        "scenario_data_mount": False,
        "cleanup": cleanup,
        "docker_control_trace": docker_control_trace,
        "control_trace_sha256": _sha256(_canonical_bytes(docker_control_trace)),
        "cleanup_trace": [
            item for item in docker_control_trace
            if item["action"] in {"remove_probe_container", "check_probe_container_absent"}
        ],
        "failure": failure,
        "_fingerprint": fingerprint,
        "_source_map": source_map,
    }


def _redact_case(case: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in case.items() if not key.startswith("_")}


def _write_evidence_checkpoint(evidence_path: Path, report: dict[str, Any]) -> None:
    """Atomically retain valid JSON after each completed stage or probe case."""
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{evidence_path.name}.", suffix=".tmp", dir=evidence_path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, evidence_path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def execute(source_root: Path, evidence_path: Path) -> dict[str, Any]:
    if SOURCE_PIN_STATUS != "final-public":
        raise VerificationError(
            "execution is disabled until the reachable public source commit/tree pins are finalized"
        )
    started_at = time.time()
    report: dict[str, Any] = {
        "schema": "issue-82-github-actions-container-evidence-v4",
        "issue": 82,
        "status": "running",
        "phase": "preflight",
        "source": None,
        "build": None,
        "runtime": None,
        "run_contract": {
            "maximum_build_attempts": 1,
            "build_timeout_seconds": MAX_BUILD_SECONDS,
            "import_smoke_runs": 1,
            "fixed_probe_cases": ["parent", "comment_only_candidate"],
            "probe_mechanics_timeout_seconds": MECHANICS_TIMEOUT_SECONDS,
        },
        "synthetic_authority_note": (
            "Synthetic authority is limited to the fixed semantic-probe suite. Import and probe stages "
            "are conditional on preceding source/build gates; a failure before those stages means they "
            "did not run. No production evaluator authority, PIT data, provider, model, broker, or paper "
            "state is used."
        ),
        "probes": [],
    }
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", attempt):
        raise VerificationError("workflow run identity is not safe for an image tag")
    image_tag = f"pit-optimizer-v5-evaluator:issue82-{run_id}-{attempt}"
    temp_root: Path | None = None
    build_metadata_root: Path | None = None
    build_metadata_path: Path | None = None
    image: dict[str, Any] | None = None
    build_attempted = False
    build_metadata_cleanup: dict[str, Any] = {
        "attempted": False,
        "directory_removed": False,
        "cleanup_complete": None,
    }
    image_cleanup: dict[str, Any] = {
        "attempted": False,
        "tag_removed": False,
        "absence_check": None,
        "cleanup_complete": None,
    }
    try:
        source_info = verify_source(source_root)
        report["source"] = source_info
        _write_evidence_checkpoint(evidence_path, report)
        event = os.environ.get("GITHUB_EVENT_NAME")
        trigger_ref = os.environ.get("GITHUB_REF")
        if event == "workflow_dispatch":
            if trigger_ref != f"refs/heads/{RUNNER_PACKAGE_BRANCH}":
                raise VerificationError("workflow package was dispatched from outside the pinned combined verification branch")
            if attempt != "1":
                raise VerificationError("workflow reruns are disabled for this one-time evidence gate")
        engine = _command(
            ["docker", "version", "--format", "{{.Server.Version}} {{.Server.Os}}/{{.Server.Arch}}"],
            timeout=15,
        ).stdout.strip()
        if not engine.endswith("linux/amd64"):
            raise VerificationError(f"Docker Engine is not a Linux/amd64 daemon: {engine!r}")
        report["runner"] = {
            "docker_server_identity": engine,
            "runner_os": os.environ.get("RUNNER_OS", "local"),
            "runner_arch": os.environ.get("RUNNER_ARCH", "local"),
            "workflow_run_id": os.environ.get("GITHUB_RUN_ID"),
            "workflow_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "expected_package_branch": RUNNER_PACKAGE_BRANCH,
            "package_source_commit": os.environ.get("GITHUB_SHA"),
            "actions_run_started_by_workflow_dispatch": event == "workflow_dispatch",
            "workflow_run_url": (
                f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
                if all(os.environ.get(key) for key in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
                else None
            ),
        }

        inspect_trace: list[dict[str, Any]] = []
        prior_image_receipt = _prebuild_image_inspection_attempt(image_tag)
        report["prebuild_image_inspection"] = prior_image_receipt
        _write_evidence_checkpoint(evidence_path, report)
        inspect_started_at = time.monotonic()
        try:
            prior_image = _command(
                ["docker", "image", "inspect", image_tag],
                timeout=10,
                check=False,
                trace=inspect_trace,
                trace_action="prebuild_image_inspect",
            )
        except Exception as exc:
            if not inspect_trace:
                argv = prior_image_receipt["command_argv"]
                inspect_trace.append({
                    "action": "prebuild_image_inspect",
                    "command_prefix": argv[:3],
                    "command_sha256": prior_image_receipt["command_sha256"],
                    "timeout_seconds": 10,
                    "elapsed_seconds": round(time.monotonic() - inspect_started_at, 3),
                    "returncode": None,
                    "timed_out": isinstance(exc, subprocess.TimeoutExpired),
                    "raised_exception": type(exc).__name__,
                    "stdout_sha256": _output_sha256(getattr(exc, "stdout", None)),
                    "stderr_sha256": _output_sha256(getattr(exc, "stderr", None)),
                })
            cause = exc.__cause__
            prior_image_receipt.update({
                "status": "unverified",
                "absence_verified": False,
                "exception": {
                    "type": type(exc).__name__,
                    "message": str(exc) or type(exc).__name__,
                    "message_sha256": _output_sha256(str(exc) or type(exc).__name__),
                    "cause_type": type(cause).__name__ if cause is not None else None,
                    "cause_message_sha256": (
                        _output_sha256(str(cause) or type(cause).__name__)
                        if cause is not None else None
                    ),
                },
                "trace": inspect_trace,
            })
            report["prebuild_image_inspection"] = prior_image_receipt
            _write_evidence_checkpoint(evidence_path, report)
            raise VerificationError(
                "pre-build image inspection did not return a verifiable result; refusing to build or remove it"
            ) from exc
        prior_image_receipt = _prebuild_image_inspection_receipt(image_tag, prior_image, inspect_trace)
        report["prebuild_image_inspection"] = prior_image_receipt
        _write_evidence_checkpoint(evidence_path, report)
        if prior_image_receipt["status"] == "present":
            raise VerificationError("the unique image tag already exists; refusing to reuse or remove it")
        if prior_image_receipt["status"] != "absent":
            raise VerificationError(
                "could not verify the unique image tag is absent "
                f"(image inspect exited {prior_image.returncode}); refusing to build or remove it"
            )
        runner_temp = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())).resolve()
        build_metadata_root = Path(tempfile.mkdtemp(prefix="issue82-build-metadata-", dir=runner_temp))
        build_metadata_path = build_metadata_root / "build-metadata.json"
        build_argv = [
            "docker", "buildx", "build", "--pull", "--progress=plain", "--platform", "linux/amd64",
            "--network=default", "--build-arg", f"PIT_V5_RUNTIME_SOURCE_SHA256={RUNTIME_SOURCE_SHA256}",
            "--tag", image_tag, "--file", str(source_root.resolve() / "Dockerfile.pit-optimizer-v5"),
            "--metadata-file", str(build_metadata_path), "--load", str(source_root.resolve()),
        ]
        build_attempted = True
        report["phase"] = "build"
        report["build"] = {
            "attempt_count": 1,
            "network_scope": "networked image/dependency build only; candidate runs use network none",
            "command_argv": build_argv,
            "command_sha256": _sha256(_canonical_bytes(build_argv)),
            "timeout_seconds": MAX_BUILD_SECONDS,
            "status": "running",
        }
        _write_evidence_checkpoint(evidence_path, report)
        build_exit_code = _stream_command(build_argv, timeout=MAX_BUILD_SECONDS)
        report["build"].update({
            "status": "completed",
            "exit_code": build_exit_code,
            "live_build_log_retained_in_actions_run": True,
        })
        if build_exit_code != 0:
            raise VerificationError(f"Buildx image build exited with code {build_exit_code}")
        metadata_raw = build_metadata_path.read_bytes()
        report["build"].update({
            "metadata_file_sha256": _sha256(metadata_raw),
            "metadata_file_size_bytes": len(metadata_raw),
        })
        build_metadata = _parse_build_metadata(metadata_raw)
        report["build"].update(build_metadata)
        image = _image_identity(image_tag)
        config_identity_matches = (
            build_metadata["buildx_config_digest"] == image["docker_backend_image_id"]
        )
        report["runtime"] = {
            "source_binding": {
                "commit": SOURCE_COMMIT,
                "tree": SOURCE_TREE,
                "runtime_source_sha256": RUNTIME_SOURCE_SHA256,
            },
            "pinned_base_image": BASE_IMAGE,
            "dockerfile_git_blob": DOCKERFILE_GIT_BLOB,
            "dockerignore_git_blob": DOCKERIGNORE_GIT_BLOB,
            **image,
            "buildx_image_digest": build_metadata["buildx_image_digest"],
            "buildx_config_digest": build_metadata["buildx_config_digest"],
            "buildx_descriptor_digest": build_metadata["buildx_descriptor_digest"],
            "buildx_descriptor_media_type": build_metadata["buildx_descriptor_media_type"],
            "buildx_metadata_file_sha256": build_metadata["metadata_file_sha256"],
            "buildx_config_matches_docker_backend_image_id": config_identity_matches,
            "user": CONTAINER_UID_GID,
            "network": "none",
            "root_filesystem": "read_only",
            "capabilities_dropped": "ALL",
            "no_new_privileges": True,
            "cpu_limit": 1,
            "memory_limit_mib": 1024,
            "pid_limit": 32,
            "policy_method_timeout_seconds": 1,
            "mechanics_timeout_seconds": MECHANICS_TIMEOUT_SECONDS,
            "output_limit_bytes": MAX_OUTPUT_BYTES,
            "source_mount": "four individual read-only policy files",
            "scenario_data_mount": None,
            "image_pull_during_candidate_runs": "never",
        }
        _write_evidence_checkpoint(evidence_path, report)
        if not config_identity_matches:
            raise VerificationError(
                "Buildx config digest differs from Docker backend image ID; "
                "refusing import smoke and probes"
            )

        evidence_id = secrets.token_hex(8)
        temp_root = Path(tempfile.mkdtemp(prefix=f"issue82-{evidence_id}-", dir=runner_temp))
        os.chmod(temp_root, 0o755)
        import_smoke = _run_import_smoke(image=image, evidence_id=evidence_id)
        report["import_smoke"] = import_smoke
        report["phase"] = "import_smoke_complete"
        _write_evidence_checkpoint(evidence_path, report)
        if import_smoke["failure"]:
            raise VerificationError(f"in-image import smoke failed: {import_smoke['failure']}")
        _parent, parent_bundle_sha, parent_policy_sha = _policy_sources(source_root.resolve(), "parent")
        _candidate, candidate_bundle_sha, candidate_policy_sha = _policy_sources(source_root.resolve(), "candidate")
        if parent_policy_sha == candidate_policy_sha or parent_bundle_sha == candidate_bundle_sha:
            raise VerificationError("comment-only candidate failed to produce distinct source identities")

        parent_fingerprint: dict[str, Any] | None = None
        candidate_fingerprint: dict[str, Any] | None = None
        for label, bundle_sha, policy_sha in (
            ("parent", parent_bundle_sha, parent_policy_sha),
            ("comment_only_candidate", candidate_bundle_sha, candidate_policy_sha),
        ):
            normalized_label = "candidate" if label == "comment_only_candidate" else "parent"
            report["phase"] = f"probe_{label}"
            _write_evidence_checkpoint(evidence_path, report)
            try:
                case = _run_case(
                    source_root=source_root.resolve(),
                    temp_root=temp_root,
                    label=normalized_label,
                    image=image,
                    source_map=source_info,
                    source_bundle_sha256=bundle_sha,
                    policy_revision_sha256=policy_sha,
                    evidence_id=evidence_id,
                )
            except BaseException as exc:
                case = {
                    "label": label,
                    "status": "failed_before_container_setup",
                    "docker_control_trace": [],
                    "cleanup_trace": [],
                    "failure": str(exc) or type(exc).__name__,
                }
                report["probes"].append(case)
                _write_evidence_checkpoint(evidence_path, report)
                raise
            case["label"] = label
            report["probes"].append(_redact_case(case))
            _write_evidence_checkpoint(evidence_path, report)
            if case["failure"]:
                raise VerificationError(f"{label} probe failed: {case['failure']}")
            if label == "parent":
                parent_fingerprint = case["_fingerprint"]
            else:
                candidate_fingerprint = case["_fingerprint"]

        if parent_fingerprint is None or candidate_fingerprint is None or parent_fingerprint != candidate_fingerprint:
            raise VerificationError("parent and comment-only candidate fingerprints differ")
        report["comparison"] = {
            "classification": "same_fixed_suite_fingerprint",
            "fingerprints_equal": True,
            "shared_semantic_fingerprint_sha256": parent_fingerprint["fingerprint_sha256"],
            "scope": "the fixed 11-probe suite only; not a global behavior-equivalence claim",
        }
        report["status"] = "verified"
        report["phase"] = "completed"
    except BaseException as exc:
        report["status"] = "failed"
        report["phase"] = "failed"
        report["failure"] = str(exc) or type(exc).__name__
        if isinstance(report.get("build"), dict) and report["build"].get("status") == "running":
            report["build"]["status"] = "failed"
            report["build"]["failure"] = report["failure"]
    finally:
        if build_attempted:
            image_cleanup["attempted"] = True
            try:
                removed = _command(["docker", "image", "rm", "--force", image_tag], timeout=30, check=False)
                absence = _command(["docker", "image", "inspect", image_tag], timeout=10, check=False)
                image_cleanup["remove_exit_code"] = removed.returncode
                image_cleanup["tag_removed"] = removed.returncode == 0
                image_cleanup["absence_check"] = _image_absence_verified(absence)
                image_cleanup["cleanup_complete"] = bool(image_cleanup["absence_check"])
            except BaseException as exc:
                image_cleanup["cleanup_error"] = str(exc) or type(exc).__name__
                image_cleanup["cleanup_complete"] = False
            if not image_cleanup["cleanup_complete"]:
                report["status"] = "failed"
                report["cleanup_failure"] = "built image tag remained inspectable after targeted removal"
        if temp_root is not None and temp_root.exists():
            try:
                shutil.rmtree(temp_root)
                report["temporary_mount_workspace_removed"] = not temp_root.exists()
            except OSError as exc:
                report["temporary_mount_workspace_removed"] = False
                report["cleanup_failure"] = report.get("cleanup_failure") or str(exc)
                report["status"] = "failed"
        else:
            report["temporary_mount_workspace_removed"] = True
        if build_metadata_root is not None:
            build_metadata_cleanup["attempted"] = True
            try:
                shutil.rmtree(build_metadata_root)
                build_metadata_cleanup["directory_removed"] = not build_metadata_root.exists()
                build_metadata_cleanup["cleanup_complete"] = not build_metadata_root.exists()
            except OSError as exc:
                build_metadata_cleanup["cleanup_error"] = str(exc) or type(exc).__name__
                build_metadata_cleanup["cleanup_complete"] = False
                report["status"] = "failed"
                report["cleanup_failure"] = report.get("cleanup_failure") or "Buildx metadata directory cleanup failed"
        else:
            build_metadata_cleanup["cleanup_complete"] = True
        report["build_metadata_cleanup"] = build_metadata_cleanup
        report["image_cleanup"] = image_cleanup
        report["finished_at_unix"] = int(time.time())
        report["elapsed_seconds"] = round(time.time() - started_at, 3)
        try:
            _write_evidence_checkpoint(evidence_path, report)
        except OSError as exc:
            report["status"] = "failed"
            report["evidence_write_failure"] = str(exc)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--evidence-path", type=Path)
    parser.add_argument("--verify-git-objects-only", action="store_true")
    arguments = parser.parse_args()
    if arguments.verify_git_objects_only:
        try:
            print(json.dumps(verify_source(arguments.source_root, require_clean_checkout=False), indent=2, sort_keys=True))
            return 0
        except BaseException as exc:
            print(str(exc) or type(exc).__name__, file=sys.stderr)
            return 1
    if arguments.evidence_path is None:
        parser.error("--evidence-path is required unless --verify-git-objects-only is set")
    try:
        report = execute(arguments.source_root, arguments.evidence_path)
    except BaseException as exc:
        report = {
            "schema": "issue-82-github-actions-container-evidence-v4",
            "issue": 82,
            "status": "failed",
            "phase": "failed_before_execution",
            "failure": str(exc) or type(exc).__name__,
            "finished_at_unix": int(time.time()),
        }
        if arguments.evidence_path is not None:
            _write_evidence_checkpoint(arguments.evidence_path, report)
        print(json.dumps(report, indent=2, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
