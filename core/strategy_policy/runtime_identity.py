"""Content identity for the trusted paper-policy host and execution path."""

from __future__ import annotations

import hashlib
import json
import platform
from pathlib import Path


_RUNTIME_SOURCES = (
    "auto_trader.py",
    "core/current_policy_inputs.py",
    "core/execution_workflow.py",
    "core/order_manager.py",
    "core/order_execution.py",
    "core/policy_execution_state.py",
    "core/policy_execution_store.py",
    "core/pit_feature_snapshot.py",
    "core/strategy_policy/account_reconciliation.py",
    "core/strategy_policy/contracts.py",
    "core/strategy_policy/contracts_v3.py",
    "core/strategy_policy/frozen_bundle.py",
    "core/strategy_policy/paper_orchestration.py",
    "core/strategy_policy/runtime.py",
    "core/strategy_policy/runtime_identity.py",
    "config/settings.py",
)
_OPTIONAL_RUNTIME_SOURCES = ("core/policy_protection_bridge.py",)


def current_paper_runtime_identity() -> str:
    """Return a digest bound to the actual installed paper execution sources."""

    repository_root = Path(__file__).resolve().parents[2]
    sources: list[list[str]] = []
    for relative_path in _RUNTIME_SOURCES:
        path = repository_root / relative_path
        try:
            if path.is_symlink() or not path.is_file():
                raise OSError("required runtime source is not a regular file")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise RuntimeError(f"trusted paper runtime source is unavailable: {relative_path}") from exc
        sources.append([relative_path, digest])
    for relative_path in _OPTIONAL_RUNTIME_SOURCES:
        path = repository_root / relative_path
        if path.is_symlink():
            raise RuntimeError(f"trusted paper runtime source is unavailable: {relative_path}")
        if path.is_file():
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as exc:
                raise RuntimeError(f"trusted paper runtime source is unavailable: {relative_path}") from exc
            sources.append([relative_path, digest])
        else:
            sources.append([relative_path, "absent"])
    material = {
        "schema_version": 1,
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "sources": sources,
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return f"paper-runtime:sha256:{hashlib.sha256(canonical).hexdigest()}"


__all__ = ["current_paper_runtime_identity"]
