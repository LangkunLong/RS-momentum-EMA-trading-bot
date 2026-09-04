"""Closed authoring scope for optimizer V5 policy sources."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Literal

from core.strategy_policy import POLICY_INTERFACE_VERSION_V3


POLICY_INTERFACE_VERSION_V5 = POLICY_INTERFACE_VERSION_V3
EDITABLE_POLICY_PATHS_V5 = (
    "core/strategy_policy/v3/entry.py",
    "core/strategy_policy/v3/risk.py",
    "core/strategy_policy/v3/position.py",
    "core/strategy_policy/v3/exit.py",
)
REQUIRED_POLICY_EXPORTS_V5: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        EDITABLE_POLICY_PATHS_V5[0]: ("evaluate_entry",),
        EDITABLE_POLICY_PATHS_V5[1]: (
            "recommend_capacity",
            "recommend_allocation",
            "select_eviction",
        ),
        EDITABLE_POLICY_PATHS_V5[2]: ("evaluate_add_on",),
        EDITABLE_POLICY_PATHS_V5[3]: ("evaluate_exit",),
    }
)
NORMAL_POLICY_SYMBOL_KINDS_V5 = ("function", "constant")
FULL_SOURCE_ESCAPE_PATHS_V5 = EDITABLE_POLICY_PATHS_V5

PolicySymbolKindV5 = Literal["function", "constant"]


def required_policy_exports_v5(path: str) -> tuple[str, ...]:
    """Return the fixed public functions required from one editable policy module."""

    try:
        return REQUIRED_POLICY_EXPORTS_V5[path]
    except (KeyError, TypeError) as exc:
        raise ValueError("policy source path is outside the V5 editable scope") from exc


def validate_policy_symbol_edit_v5(
    *,
    path: str,
    symbol: str,
    kind: PolicySymbolKindV5,
) -> None:
    """Authorize one normal-mode complete function or module-constant replacement."""

    exports = required_policy_exports_v5(path)
    if type(symbol) is not str or not symbol.isidentifier():
        raise ValueError("policy edit symbol must be a named Python identifier")
    if kind == "function":
        if symbol not in exports:
            raise ValueError("policy function is outside the V5 editable scope")
        return
    if kind == "constant":
        if not symbol.isupper() or symbol.startswith("_"):
            raise ValueError("policy constant is outside the V5 editable scope")
        return
    raise ValueError("policy edit kind is outside the V5 editable scope")


def validate_full_source_escape_v5(paths: Iterable[str]) -> tuple[str, ...]:
    """Require full-source escape to replace the atomic ordered four-file bundle."""

    if isinstance(paths, (str, bytes)):
        raise ValueError("full-source escape must bind all four V5 policy paths")
    try:
        supplied = tuple(paths)
    except TypeError as exc:
        raise ValueError("full-source escape must bind all four V5 policy paths") from exc
    if supplied != FULL_SOURCE_ESCAPE_PATHS_V5:
        raise ValueError("full-source escape must bind all four V5 policy paths")
    return supplied


def validate_policy_authoring_scope_v5(
    *,
    symbol_edits: Iterable[tuple[str, str, PolicySymbolKindV5]] = (),
    full_source_paths: Iterable[str] = (),
) -> None:
    """Require exactly one valid normal-edit or atomic full-source authoring mode."""

    try:
        edits = tuple(symbol_edits)
        sources = tuple(full_source_paths)
    except TypeError as exc:
        raise ValueError("V5 policy authoring scope is invalid") from exc
    if bool(edits) == bool(sources):
        raise ValueError("V5 policy authoring requires exactly one source-edit mode")
    if sources:
        validate_full_source_escape_v5(sources)
        return
    seen: set[tuple[str, str]] = set()
    for edit in edits:
        if type(edit) is not tuple or len(edit) != 3:
            raise ValueError("V5 normal policy edit is invalid")
        path, symbol, kind = edit
        validate_policy_symbol_edit_v5(path=path, symbol=symbol, kind=kind)
        identity = (path, symbol)
        if identity in seen:
            raise ValueError("V5 normal policy edits must be unique")
        seen.add(identity)


__all__ = [
    "EDITABLE_POLICY_PATHS_V5",
    "FULL_SOURCE_ESCAPE_PATHS_V5",
    "NORMAL_POLICY_SYMBOL_KINDS_V5",
    "POLICY_INTERFACE_VERSION_V5",
    "REQUIRED_POLICY_EXPORTS_V5",
    "PolicySymbolKindV5",
    "required_policy_exports_v5",
    "validate_full_source_escape_v5",
    "validate_policy_authoring_scope_v5",
    "validate_policy_symbol_edit_v5",
]
