"""Seal one human-authored controller proposal to an actual V5 role request.

The proposal contains an artifact body only. This tool binds it to the exact
persisted request, then writes the existing file-backed controller envelope.
It has no provider, evaluator, policy execution, or trading capability.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from core.pit_optimizer_v5.artifacts import LocalArtifactRepositoryV5
from core.pit_optimizer_v5.contracts import ArtifactRefV5
from core.pit_optimizer_v5.production_fs import (
    acquire_absolute_directory_v5,
    write_new_regular_in_directory_v5,
)
from core.pit_optimizer_v5.provider import parse_and_bind_role_artifact


_MAX_PROPOSAL_BYTES = 4 * 1024 * 1024


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("controller proposal has duplicate keys")
        result[key] = value
    return result


def seal_engineering_controller_response_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    request_ref: ArtifactRefV5,
    proposal_path: Path,
    response_directory: Path,
) -> dict[str, str]:
    """Validate and create exactly one call-key-named controller response."""

    if type(repository) is not LocalArtifactRepositoryV5 or type(request_ref) is not ArtifactRefV5:
        raise ValueError("controller proposal authority is invalid")
    proposal = Path(proposal_path)
    response_root = Path(response_directory)
    if (
        not proposal.is_absolute()
        or not proposal.is_file()
        or proposal.is_symlink()
        or not response_root.is_absolute()
        or not response_root.is_dir()
        or response_root.is_symlink()
    ):
        raise ValueError("controller proposal and response paths must be absolute regular paths")
    raw = proposal.read_bytes()
    if not raw or len(raw) > _MAX_PROPOSAL_BYTES:
        raise ValueError("controller proposal exceeds its byte bound")
    parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    if type(parsed) is not dict or set(parsed) != {"role", "artifact"} or type(parsed["artifact"]) is not dict:
        raise ValueError("controller proposal must contain exact role and artifact fields")
    call, request = repository._load_role_request_entry(request_ref)
    if parsed["role"] != call.role or request.sha256 != call.request_sha256:
        raise ValueError("controller proposal differs from its persisted role request")
    response = {
        "binding": request.expected_binding.to_primitive(),
        "artifact": parsed["artifact"],
    }
    response_text = json.dumps(response, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    parse_and_bind_role_artifact(request=request, response_text=response_text)
    envelope = {
        "schema_version": 5,
        "artifact_type": "controller_role_response",
        "call_key_sha256": call.sha256,
        "request_sha256": request.sha256,
        "response": response,
    }
    content = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(content) > _MAX_PROPOSAL_BYTES:
        raise ValueError("controller response exceeds its byte bound")
    name = call.sha256 + ".json"
    with acquire_absolute_directory_v5(response_root) as directory:
        write_new_regular_in_directory_v5(directory, name, content)
    return {
        "role": call.role,
        "call_key_sha256": call.sha256,
        "request_sha256": request.sha256,
        "response_path": str(response_root / name),
    }


def main(argv: tuple[str, ...] | None = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--request-path", required=True)
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--proposal-path", type=Path, required=True)
    parser.add_argument("--response-directory", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = seal_engineering_controller_response_v5(
            repository=LocalArtifactRepositoryV5(args.artifact_root),
            request_ref=ArtifactRefV5(args.request_path, args.request_sha256),
            proposal_path=args.proposal_path,
            response_directory=args.response_directory,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"controller proposal rejected: {type(exc).__name__}: {str(exc)[:512]}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys.argv[1:])))


__all__ = ["seal_engineering_controller_response_v5", "main"]
