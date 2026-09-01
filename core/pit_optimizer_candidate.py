"""Controller-owned validation and identity for model-authored policy candidates."""

from __future__ import annotations

import ast
from dataclasses import InitVar, dataclass
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Mapping
from weakref import WeakValueDictionary

import core.pit_optimization_contract as optimization_contract
from core.pit_optimization_contract import (
    AuthorArtifact,
    AuthorInput,
    CriticInput,
    InvestigatorInput,
    PatchBounds,
    PitOptimizerCallBudget,
    PolicySourceBundle,
    PolicySourceRecord,
)


EDITABLE_POLICY_PATHS = (
    "core/strategy_policy/entry.py",
    "core/strategy_policy/risk.py",
    "core/strategy_policy/exit.py",
)
LEGACY_PATCH_BOUNDS = PatchBounds(4, 25, 400, 256 * 1024)
PIT_OPTIMIZER_PATCH_BOUNDS = PatchBounds(3, 12, 200, 64 * 1024)

_ALLOWED_PUBLIC = {
    "core/strategy_policy/entry.py": frozenset({"evaluate_entry"}),
    "core/strategy_policy/risk.py": frozenset(
        {"recommend_capacity", "recommend_allocation", "select_eviction"}
    ),
    "core/strategy_policy/exit.py": frozenset({"evaluate_exit"}),
}
_DECLARED_SYMBOLS = {
    "core/strategy_policy/entry.py": (
        "core.strategy_policy.entry.evaluate_entry",
    ),
    "core/strategy_policy/risk.py": (
        "core.strategy_policy.risk.recommend_capacity",
        "core.strategy_policy.risk.recommend_allocation",
        "core.strategy_policy.risk.select_eviction",
    ),
    "core/strategy_policy/exit.py": (
        "core.strategy_policy.exit.evaluate_exit",
    ),
}
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_AUTHOR_HUNK_HEADER_RE = re.compile(
    r"^@@ -(0|[1-9][0-9]*)(?:,([0-9]+))? "
    r"\+(0|[1-9][0-9]*)(?:,([0-9]+))? @@((?: .*)?)\n$"
)
_FULL_SOURCE_TRANSPORT_PREFIX = "PIT_FULL_SOURCE_V1\n"
_CANDIDATE_IDENTITY_CONSTRUCTION_SEAL = object()
_CONTRACT_IMPORTS = frozenset(
    {
        "AllocationDecision",
        "AllocationSnapshot",
        "BenchmarkContextV1",
        "CapacityDecision",
        "CapacitySnapshot",
        "EntryDecision",
        "EntrySnapshot",
        "EvictionDecision",
        "EvictionPosition",
        "EvictionSnapshot",
        "ExitAction",
        "ExitDecision",
        "ExitSnapshot",
        "MarketContextV1",
    }
)
_NON_CALLABLE_CONTRACT_IMPORTS = frozenset(
    {"BenchmarkContextV1", "MarketContextV1"}
)
_PURE_BUILTINS = frozenset(
    {
        "abs",
        "all",
        "any",
        "bool",
        "enumerate",
        "float",
        "int",
        "isinstance",
        "len",
        "list",
        "max",
        "min",
        "range",
        "reversed",
        "round",
        "sorted",
        "sum",
        "tuple",
        "zip",
    }
)
_REFLECTION_CALLS = frozenset(
    {"delattr", "dir", "getattr", "globals", "hasattr", "locals", "setattr", "vars"}
)
_DANGEROUS_CALLS = frozenset(
    {
        "__import__",
        "breakpoint",
        "compile",
        "eval",
        "exec",
        "help",
        "input",
        "memoryview",
        "open",
        "print",
    }
)
_ALLOWED_AST_NODES = (
    ast.Module,
    ast.Expr,
    ast.FunctionDef,
    ast.arguments,
    ast.arg,
    ast.Return,
    ast.Assign,
    ast.AnnAssign,
    ast.AugAssign,
    ast.If,
    ast.For,
    ast.While,
    ast.Break,
    ast.Continue,
    ast.BoolOp,
    ast.BinOp,
    ast.UnaryOp,
    ast.Compare,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Store,
    ast.Constant,
    ast.Attribute,
    ast.Subscript,
    ast.Tuple,
    ast.List,
    ast.Dict,
    ast.Set,
    ast.GeneratorExp,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.comprehension,
    ast.IfExp,
    ast.Lambda,
    ast.keyword,
    ast.alias,
    ast.Import,
    ast.ImportFrom,
    ast.operator,
    ast.boolop,
    ast.unaryop,
    ast.cmpop,
)


@dataclass(frozen=True, slots=True, weakref_slot=True)
class CandidateIdentity:
    source_commit: str
    policy_interface_version: int
    cumulative_diff_sha256: str
    editable_file_sha256s: tuple[tuple[str, str], ...]
    changed_paths: tuple[str, ...]
    changed_symbols: tuple[str, ...]
    immutable_constraints_sha256: str
    discovery_manifest_sha256: str
    identity_sha256: str
    _controller_seal: InitVar[object] = None

    def __post_init__(self, _controller_seal: object) -> None:
        if _controller_seal is not _CANDIDATE_IDENTITY_CONSTRUCTION_SEAL:
            raise ValueError("candidate identity must be controller derived")
        _validate_candidate_identity_fields(self)

    def to_primitive(self) -> dict[str, object]:
        return {
            **_candidate_identity_values(self),
            "identity_sha256": self.identity_sha256,
        }

    def to_canonical_json(self) -> str:
        return json.dumps(
            self.to_primitive(),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ) + "\n"


@dataclass(frozen=True, slots=True, weakref_slot=True)
class CandidateIdentityV4:
    source_commit: str
    policy_interface_version: int
    cumulative_diff_sha256: str
    editable_file_sha256s: tuple[tuple[str, str], ...]
    changed_paths: tuple[str, ...]
    changed_symbols: tuple[str, ...]
    immutable_constraints_sha256: str
    discovery_panel_plan_sha256: str
    parent_identity_sha256: str
    identity_sha256: str
    _controller_seal: InitVar[object] = None

    def __post_init__(self, _controller_seal: object) -> None:
        if _controller_seal is not _CANDIDATE_IDENTITY_CONSTRUCTION_SEAL:
            raise ValueError("candidate identity must be controller derived")
        _validate_candidate_identity_v4_fields(self)

    def to_primitive(self) -> dict[str, object]:
        return {
            **_candidate_identity_v4_values(self),
            "identity_sha256": self.identity_sha256,
        }

    def to_canonical_json(self) -> str:
        return json.dumps(
            self.to_primitive(),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ) + "\n"


_AUTHENTICATED_CANDIDATE_IDENTITIES: WeakValueDictionary[
    int, CandidateIdentity | CandidateIdentityV4
] = WeakValueDictionary()


def _immutable_literal(value: object) -> bool:
    if value is None or type(value) in {bool, int, float, str}:
        return True
    return type(value) is tuple and all(_immutable_literal(item) for item in value)


def _root_name(node: ast.AST) -> str | None:
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def validate_policy_ast(*, path: str, source: str) -> None:
    """Enforce a closed pure-Python policy language without ambient capabilities."""
    if path not in _ALLOWED_PUBLIC or not isinstance(source, str):
        raise ValueError("policy AST path is invalid")
    try:
        tree = ast.parse(source, filename=path)
        compile(tree, path, "exec", dont_inherit=True)
    except SyntaxError as exc:
        raise ValueError("policy AST syntax is invalid") from exc
    if any(isinstance(node, ast.AsyncFunctionDef) for node in tree.body):
        raise ValueError("policy async definitions are forbidden")

    local_functions = {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    public_functions = {name for name in local_functions if not name.startswith("_")}
    if public_functions != set(_ALLOWED_PUBLIC[path]):
        raise ValueError("policy public symbols differ from the closed interface")
    imported_contracts: set[str] = set()
    imported_math = False
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom):
            if statement.module == "__future__" and statement.level == 0:
                if tuple(alias.name for alias in statement.names) != ("annotations",):
                    raise ValueError("policy import is outside the allowlist")
                continue
            if not (
                (statement.level == 1 and statement.module == "contracts")
                or (
                    statement.level == 0
                    and statement.module == "core.strategy_policy.contracts"
                )
            ):
                raise ValueError("policy import is outside the allowlist")
            names = {alias.name for alias in statement.names}
            if (
                any(alias.asname is not None for alias in statement.names)
                or not names
                or not names <= _CONTRACT_IMPORTS
            ):
                raise ValueError("policy contract import is outside the allowlist")
            imported_contracts.update(names)
            continue
        if isinstance(statement, ast.Import):
            if len(statement.names) != 1 or statement.names[0].name != "math" or (
                statement.names[0].asname is not None
            ):
                raise ValueError("policy import is outside the allowlist")
            imported_math = True
            continue
        if isinstance(statement, (ast.FunctionDef, ast.Assign, ast.AnnAssign)):
            continue
        if isinstance(statement, ast.Expr) and isinstance(
            statement.value, ast.Constant
        ) and isinstance(statement.value.value, str):
            continue
        if isinstance(statement, (ast.ClassDef, ast.AsyncFunctionDef)):
            label = "class" if isinstance(statement, ast.ClassDef) else "async"
            raise ValueError(f"policy {label} definitions are forbidden")
        raise ValueError("policy module statement is outside the allowlist")

    for statement in tree.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets = (
                statement.targets
                if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            if (
                len(targets) != 1
                or not isinstance(targets[0], ast.Name)
                or not targets[0].id.isupper()
            ):
                raise ValueError("policy module assignments must be constants")
            value_node = statement.value
            if value_node is None:
                raise ValueError("policy constant requires an immutable literal")
            try:
                value = ast.literal_eval(value_node)
            except (TypeError, ValueError) as exc:
                raise ValueError("policy constant requires an immutable literal") from exc
            if not _immutable_literal(value):
                raise ValueError("policy constant requires an immutable literal")

    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    appendable_names: dict[ast.FunctionDef, set[str]] = {}
    for function in (
        item for item in tree.body if isinstance(item, ast.FunctionDef)
    ):
        binding_counts: dict[str, int] = {}
        for descendant in ast.walk(function):
            if isinstance(descendant, ast.Name) and isinstance(
                descendant.ctx, ast.Store
            ):
                binding_counts[descendant.id] = binding_counts.get(descendant.id, 0) + 1
        names: set[str] = set()
        for descendant in ast.walk(function):
            if (
                isinstance(descendant, ast.Assign)
                and len(descendant.targets) == 1
                and isinstance(descendant.targets[0], ast.Name)
                and (
                    isinstance(descendant.value, (ast.List, ast.ListComp))
                    or (
                        isinstance(descendant.value, ast.Call)
                        and isinstance(descendant.value.func, ast.Name)
                        and descendant.value.func.id == "list"
                    )
                )
                and binding_counts.get(descendant.targets[0].id) == 1
            ):
                names.add(descendant.targets[0].id)
            elif (
                isinstance(descendant, ast.AnnAssign)
                and isinstance(descendant.target, ast.Name)
                and (
                    isinstance(descendant.value, (ast.List, ast.ListComp))
                    or (
                        isinstance(descendant.value, ast.Call)
                        and isinstance(descendant.value.func, ast.Name)
                        and descendant.value.func.id == "list"
                    )
                )
                and binding_counts.get(descendant.target.id) == 1
            ):
                names.add(descendant.target.id)
        appendable_names[function] = names

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_AST_NODES):
            if isinstance(node, ast.ClassDef):
                raise ValueError("policy class definitions are forbidden")
            if isinstance(node, ast.AsyncFunctionDef):
                raise ValueError("policy async definitions are forbidden")
            if isinstance(node, (ast.Yield, ast.YieldFrom)):
                raise ValueError("policy generator behavior is forbidden")
            if isinstance(node, (ast.Global, ast.Nonlocal)):
                raise ValueError("policy global or nonlocal state is forbidden")
            raise ValueError(
                f"policy AST node {type(node).__name__} is outside the allowlist"
            )
        if isinstance(node, ast.FunctionDef):
            if node not in tree.body:
                raise ValueError("nested policy functions are forbidden")
            if node.decorator_list:
                raise ValueError("policy function decorators are forbidden")
            for default in (*node.args.defaults, *node.args.kw_defaults):
                if default is None:
                    continue
                try:
                    value = ast.literal_eval(default)
                except (TypeError, ValueError) as exc:
                    raise ValueError("policy function default must be immutable") from exc
                if not _immutable_literal(value):
                    raise ValueError("policy function default must be immutable")
            argument_names = {
                argument.arg
                for argument in (
                    *node.args.posonlyargs,
                    *node.args.args,
                    *node.args.kwonlyargs,
                )
            }
            for descendant in ast.walk(node):
                if isinstance(descendant, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    targets = (
                        descendant.targets
                        if isinstance(descendant, ast.Assign)
                        else [descendant.target]
                    )
                    for target in targets:
                        root = _root_name(target)
                        if root in argument_names:
                            raise ValueError("policy input writes are forbidden")
                        if root in local_functions and isinstance(
                            target, (ast.Attribute, ast.Subscript)
                        ):
                            raise ValueError("policy function attribute writes are forbidden")
                        if isinstance(target, (ast.Attribute, ast.Subscript)):
                            raise ValueError("policy mutation root is outside local state")
        if isinstance(node, (ast.Set, ast.SetComp)):
            raise ValueError("policy unordered iteration is forbidden")
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__"):
                raise ValueError("policy reflection attributes are forbidden")
            if _root_name(node) in local_functions:
                raise ValueError("policy function attributes are forbidden")
        if isinstance(node, ast.Call):
            owner: ast.AST | None = node
            while owner is not None and not isinstance(owner, ast.FunctionDef):
                owner = parents.get(owner)
            call_appendables = (
                appendable_names.get(owner, set())
                if isinstance(owner, ast.FunctionDef)
                else set()
            )
            if isinstance(node.func, ast.Name):
                name = node.func.id
                if name in _REFLECTION_CALLS:
                    raise ValueError("policy reflection calls are forbidden")
                if name in _DANGEROUS_CALLS:
                    raise ValueError("policy call is outside the allowlist")
                if name not in (
                    _PURE_BUILTINS
                    | (imported_contracts - _NON_CALLABLE_CONTRACT_IMPORTS)
                    | local_functions
                ):
                    raise ValueError("policy call is outside the allowlist")
            elif isinstance(node.func, ast.Attribute):
                root = _root_name(node.func)
                if root == "math":
                    if not imported_math or node.func.attr.startswith("_"):
                        raise ValueError("policy math call is outside the allowlist")
                elif (
                    node.func.attr == "append"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in call_appendables
                ):
                    continue
                else:
                    raise ValueError("policy attribute call is outside the allowlist")
            else:
                raise ValueError("policy dynamic call is outside the allowlist")


def _symbol_nodes(path: str, source: str) -> dict[str, str]:
    if path not in _ALLOWED_PUBLIC:
        raise ValueError("policy source path is outside the editable scope")
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        raise ValueError("policy source syntax is invalid") from exc
    nodes: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Private top-level helpers are part of the authenticated policy
            # implementation even though they are not part of its public API.
            # Track them so a model can safely refine helper-based strategy
            # logic without producing an empty candidate identity.
            nodes[node.name] = ast.dump(node, include_attributes=False)
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id.isupper()
        ):
            try:
                ast.literal_eval(node.value)
            except (TypeError, ValueError) as exc:
                raise ValueError("policy constant is not literal") from exc
            nodes[node.targets[0].id] = ast.dump(node, include_attributes=False)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id.isupper()
            and node.value is not None
        ):
            try:
                ast.literal_eval(node.value)
            except (TypeError, ValueError) as exc:
                raise ValueError("policy constant is not literal") from exc
            nodes[node.target.id] = ast.dump(node, include_attributes=False)
    return nodes


def derive_changed_symbols(
    *,
    before_sources: Mapping[str, str],
    after_sources: Mapping[str, str],
) -> tuple[str, ...]:
    """Compare controller-read ASTs and return sorted qualified changed symbols."""
    if tuple(sorted(before_sources)) != tuple(sorted(after_sources)):
        raise ValueError("policy source maps differ")
    changed: list[str] = []
    for path in sorted(before_sources):
        before = _symbol_nodes(path, before_sources[path])
        after = _symbol_nodes(path, after_sources[path])
        module = path.removesuffix(".py").replace("/", ".")
        changed.extend(
            f"{module}.{name}"
            for name in sorted(before.keys() | after.keys())
            if before.get(name) != after.get(name)
        )
    return tuple(changed)


def _require_identity_inputs(
    *,
    bounds: PatchBounds,
    source_commit: str,
    policy_interface_version: int,
    immutable_constraints_sha256: str,
    discovery_manifest_sha256: str,
) -> None:
    if not isinstance(bounds, PatchBounds):
        raise ValueError("candidate patch bounds are invalid")
    for name in ("max_files", "max_hunks", "max_changed_lines", "max_diff_bytes"):
        if getattr(bounds, name) > getattr(PIT_OPTIMIZER_PATCH_BOUNDS, name):
            raise ValueError("candidate patch bounds exceed the PIT optimizer ceiling")
    if re.fullmatch(r"[0-9a-f]{40}", source_commit or "") is None:
        raise ValueError("candidate source commit is invalid")
    if type(policy_interface_version) is not int or policy_interface_version <= 0:
        raise ValueError("candidate policy interface version is invalid")
    if _SHA256_RE.fullmatch(immutable_constraints_sha256 or "") is None:
        raise ValueError("candidate immutable constraint digest is invalid")
    if _SHA256_RE.fullmatch(discovery_manifest_sha256 or "") is None:
        raise ValueError("candidate discovery manifest digest is invalid")


def _read_policy_sources(root: Path) -> dict[str, str]:
    if not isinstance(root, Path) or not root.is_absolute():
        raise ValueError("policy source provenance root is invalid")
    try:
        absolute_root = _existing_path_without_links(root)
        if not stat.S_ISDIR(absolute_root.lstat().st_mode):
            raise ValueError("policy source provenance root is not a directory")
    except OSError as exc:
        raise ValueError("policy source provenance contains a link or reparse point") from exc
    sources: dict[str, str] = {}
    for relative in EDITABLE_POLICY_PATHS:
        target = absolute_root / relative
        try:
            target = _existing_path_without_links(target)
            target.relative_to(absolute_root)
        except (OSError, ValueError) as exc:
            raise ValueError(
                "policy source provenance contains a link or reparse point"
            ) from exc
        try:
            if not stat.S_ISREG(target.lstat().st_mode):
                raise ValueError("policy source is not a regular file")
            sources[relative] = target.read_bytes().decode("utf-8", errors="strict")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError("policy source cannot be read as UTF-8") from exc
    return sources


def _existing_path_without_links(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or bool(
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise OSError("path contains a link or reparse point")
    return absolute


def _identity_digest(fields: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            fields,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    ).hexdigest()


def _candidate_identity_values(candidate: CandidateIdentity) -> dict[str, object]:
    return {
        "source_commit": candidate.source_commit,
        "policy_interface_version": candidate.policy_interface_version,
        "cumulative_diff_sha256": candidate.cumulative_diff_sha256,
        "editable_file_sha256s": candidate.editable_file_sha256s,
        "changed_paths": candidate.changed_paths,
        "changed_symbols": candidate.changed_symbols,
        "immutable_constraints_sha256": candidate.immutable_constraints_sha256,
        "discovery_manifest_sha256": candidate.discovery_manifest_sha256,
    }


def _candidate_identity_v4_values(
    candidate: CandidateIdentityV4,
) -> dict[str, object]:
    return {
        "source_commit": candidate.source_commit,
        "policy_interface_version": candidate.policy_interface_version,
        "cumulative_diff_sha256": candidate.cumulative_diff_sha256,
        "editable_file_sha256s": candidate.editable_file_sha256s,
        "changed_paths": candidate.changed_paths,
        "changed_symbols": candidate.changed_symbols,
        "immutable_constraints_sha256": candidate.immutable_constraints_sha256,
        "discovery_panel_plan_sha256": candidate.discovery_panel_plan_sha256,
        "parent_identity_sha256": candidate.parent_identity_sha256,
    }


def _validate_candidate_identity_fields(candidate: CandidateIdentity) -> None:
    if re.fullmatch(r"[0-9a-f]{40}", candidate.source_commit or "") is None:
        raise ValueError("candidate identity source commit is invalid")
    if (
        type(candidate.policy_interface_version) is not int
        or candidate.policy_interface_version <= 0
    ):
        raise ValueError("candidate identity interface version is invalid")
    for value in (
        candidate.cumulative_diff_sha256,
        candidate.immutable_constraints_sha256,
        candidate.discovery_manifest_sha256,
        candidate.identity_sha256,
    ):
        if _SHA256_RE.fullmatch(value or "") is None:
            raise ValueError("candidate identity digest is invalid")
    if (
        type(candidate.editable_file_sha256s) is not tuple
        or tuple(path for path, _digest in candidate.editable_file_sha256s)
        != EDITABLE_POLICY_PATHS
        or any(
            _SHA256_RE.fullmatch(digest or "") is None
            for _path, digest in candidate.editable_file_sha256s
        )
    ):
        raise ValueError("candidate identity editable hashes are invalid")
    canonical_paths = tuple(
        path for path in EDITABLE_POLICY_PATHS if path in candidate.changed_paths
    )
    if (
        type(candidate.changed_paths) is not tuple
        or not candidate.changed_paths
        or candidate.changed_paths != canonical_paths
    ):
        raise ValueError("candidate identity changed paths are invalid")
    if (
        type(candidate.changed_symbols) is not tuple
        or not candidate.changed_symbols
        or len(candidate.changed_symbols) != len(set(candidate.changed_symbols))
        or any(not isinstance(symbol, str) for symbol in candidate.changed_symbols)
    ):
        raise ValueError("candidate identity changed symbols are invalid")
    allowed = {
        symbol
        for path in candidate.changed_paths
        for symbol in _DECLARED_SYMBOLS[path]
    }
    constant_prefixes = tuple(
        f"{path.removesuffix('.py').replace('/', '.')}."
        for path in candidate.changed_paths
    )
    if any(
        symbol not in allowed
        and not any(
            symbol.startswith(prefix)
            and (
                re.fullmatch(
                    r"[A-Z][A-Z0-9_]*", symbol.removeprefix(prefix)
                )
                is not None
                or re.fullmatch(
                    r"_[A-Za-z][A-Za-z0-9_]*", symbol.removeprefix(prefix)
                )
                is not None
            )
            for prefix in constant_prefixes
        )
        for symbol in candidate.changed_symbols
    ):
        raise ValueError("candidate identity changed symbols are invalid")
    if candidate.identity_sha256 != _identity_digest(
        _candidate_identity_values(candidate)
    ):
        raise ValueError("candidate identity self-digest is invalid")


def _validate_candidate_identity_v4_fields(candidate: CandidateIdentityV4) -> None:
    if re.fullmatch(r"[0-9a-f]{40}", candidate.source_commit or "") is None:
        raise ValueError("candidate identity source commit is invalid")
    if (
        type(candidate.policy_interface_version) is not int
        or candidate.policy_interface_version <= 0
    ):
        raise ValueError("candidate identity interface version is invalid")
    for value in (
        candidate.cumulative_diff_sha256,
        candidate.immutable_constraints_sha256,
        candidate.discovery_panel_plan_sha256,
        candidate.parent_identity_sha256,
        candidate.identity_sha256,
    ):
        if _SHA256_RE.fullmatch(value or "") is None:
            raise ValueError("candidate identity digest is invalid")
    if (
        type(candidate.editable_file_sha256s) is not tuple
        or tuple(path for path, _digest in candidate.editable_file_sha256s)
        != EDITABLE_POLICY_PATHS
        or any(
            _SHA256_RE.fullmatch(digest or "") is None
            for _path, digest in candidate.editable_file_sha256s
        )
    ):
        raise ValueError("candidate identity editable hashes are invalid")
    canonical_paths = tuple(
        path for path in EDITABLE_POLICY_PATHS if path in candidate.changed_paths
    )
    if (
        type(candidate.changed_paths) is not tuple
        or candidate.changed_paths != canonical_paths
    ):
        raise ValueError("candidate identity changed paths are invalid")
    if (
        type(candidate.changed_symbols) is not tuple
        or len(candidate.changed_symbols) != len(set(candidate.changed_symbols))
        or any(not isinstance(symbol, str) for symbol in candidate.changed_symbols)
    ):
        raise ValueError("candidate identity changed symbols are invalid")
    allowed = {
        symbol
        for path in candidate.changed_paths
        for symbol in _DECLARED_SYMBOLS[path]
    }
    constant_prefixes = tuple(
        f"{path.removesuffix('.py').replace('/', '.')}."
        for path in candidate.changed_paths
    )
    if any(
        symbol not in allowed
        and not any(
            symbol.startswith(prefix)
            and (
                re.fullmatch(
                    r"[A-Z][A-Z0-9_]*", symbol.removeprefix(prefix)
                )
                is not None
                or re.fullmatch(
                    r"_[A-Za-z][A-Za-z0-9_]*", symbol.removeprefix(prefix)
                )
                is not None
            )
            for prefix in constant_prefixes
        )
        for symbol in candidate.changed_symbols
    ):
        raise ValueError("candidate identity changed symbols are invalid")
    if candidate.identity_sha256 != _identity_digest(
        _candidate_identity_v4_values(candidate)
    ):
        raise ValueError("candidate identity self-digest is invalid")


def validate_candidate_identity(
    candidate: CandidateIdentity | CandidateIdentityV4,
) -> None:
    """Authenticate one exact controller-created identity object at consumption."""
    if isinstance(candidate, CandidateIdentity):
        _validate_candidate_identity_fields(candidate)
    elif isinstance(candidate, CandidateIdentityV4):
        _validate_candidate_identity_v4_fields(candidate)
    else:
        raise ValueError("candidate identity is invalid")
    if _AUTHENTICATED_CANDIDATE_IDENTITIES.get(id(candidate)) is not candidate:
        raise ValueError("candidate identity is not authenticated")


def materialize_author_candidate_diff(
    *,
    candidate_root: Path,
    author: AuthorArtifact,
    bounds: PatchBounds,
) -> str:
    """Convert the author transport into a controller-generated unified diff.

    A full-source envelope avoids asking a reasoning model to reproduce hunk
    coordinates and duplicate context exactly.  It still targets one
    controller-authenticated policy path, and the resulting diff passes through
    the same bounds, Git, AST, purity, determinism, and sandbox validation.
    Conventional unified diffs remain accepted for historical artifacts.
    """

    if not isinstance(author, AuthorArtifact) or not isinstance(bounds, PatchBounds):
        raise ValueError("author candidate transport is invalid")
    transport = author.unified_diff
    if not transport.startswith(_FULL_SOURCE_TRANSPORT_PREFIX):
        return transport
    if len(author.changed_paths) != 1:
        raise ValueError("full-source author transport requires one policy path")
    path = author.changed_paths[0]
    if path not in EDITABLE_POLICY_PATHS:
        raise ValueError("full-source author transport path is outside scope")
    replacement = transport.removeprefix(_FULL_SOURCE_TRANSPORT_PREFIX)
    if (
        not replacement
        or "\r" in replacement
        or "\x00" in replacement
        or not replacement.endswith("\n")
    ):
        raise ValueError("full-source author transport is invalid")
    before = _read_policy_sources(candidate_root)[path]
    if replacement == before:
        raise ValueError("candidate patch is a no-op")
    rendered = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            replacement.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
            lineterm="\n",
        )
    )
    if not rendered or len(rendered.encode("utf-8")) > bounds.max_diff_bytes:
        raise ValueError("candidate patch exceeds max_diff_bytes")
    return rendered


def _normalize_author_diff_transport(raw: str) -> str:
    """Repair only transport delimiters and hunk count metadata before Git checks.

    The model-authored file bodies, paths, hunk start coordinates, and context are
    left untouched.  A later strict parser and ``git apply --check`` remain the
    authority for every semantic and applicability property of the candidate.
    """

    if not isinstance(raw, str) or not raw:
        return raw
    candidate = raw if raw.endswith("\n") else raw + "\n"
    if "\r" in candidate or "\x00" in candidate:
        return candidate
    lines = candidate.splitlines(keepends=True)
    # Some model responses use the standard repo-relative ``--- path`` / ``+++ path``
    # form instead of Git's ``a/`` / ``b/`` prefixes.  Canonicalize only an exact,
    # matched pair for one of the sealed editable files.  The strict parser, Git,
    # AST, scope, and bounds checks below remain authoritative.
    for header_index in range(len(lines) - 1):
        old_header = lines[header_index]
        new_header = lines[header_index + 1]
        if not old_header.startswith("--- ") or not new_header.startswith("+++ "):
            continue
        old_path = old_header[4:].removesuffix("\n")
        new_path = new_header[4:].removesuffix("\n")
        if (
            old_path == new_path
            and old_path in EDITABLE_POLICY_PATHS
            and not old_path.startswith("a/")
            and not new_path.startswith("b/")
        ):
            lines[header_index] = f"--- a/{old_path}\n"
            lines[header_index + 1] = f"+++ b/{new_path}\n"
    normalized: list[str] = []
    index = 0
    while index < len(lines):
        header = _AUTHOR_HUNK_HEADER_RE.fullmatch(lines[index])
        if header is None:
            normalized.append(lines[index])
            index += 1
            continue
        old_count = 0
        new_count = 0
        body_index = index + 1
        while body_index < len(lines):
            line = lines[body_index]
            if line.startswith(("@@ ", "diff --git ", "--- a/")):
                break
            if line.startswith(" "):
                old_count += 1
                new_count += 1
            elif line.startswith("-"):
                old_count += 1
            elif line.startswith("+"):
                new_count += 1
            elif line != "\\ No newline at end of file\n":
                # Do not make a malformed body look valid.  The strict parser
                # below will reject this unchanged candidate.
                return candidate
            body_index += 1
        declared_old = int(header.group(2) or "1")
        declared_new = int(header.group(4) or "1")
        if (declared_old, declared_new) == (old_count, new_count):
            normalized.append(lines[index])
        else:
            normalized.append(
                f"@@ -{header.group(1)},{old_count} "
                f"+{header.group(3)},{new_count} @@{header.group(5)}\n"
            )
        index += 1
    return "".join(normalized)


def _reanchor_unique_author_hunks(
    raw: str,
    source_texts: Mapping[str, str],
) -> str:
    """Correct hunk coordinates only when the exact old body has one source match."""

    lines = raw.splitlines(keepends=True)
    current_path: str | None = None
    line_delta = 0
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("--- a/"):
            current_path = line[len("--- a/") :].removesuffix("\n")
            line_delta = 0
            index += 1
            continue
        header = _AUTHOR_HUNK_HEADER_RE.fullmatch(line)
        if header is None or current_path not in source_texts:
            index += 1
            continue
        body_index = index + 1
        old_body: list[str] = []
        old_count = 0
        new_count = 0
        while body_index < len(lines):
            body = lines[body_index]
            if body.startswith(("@@ ", "diff --git ", "--- a/")):
                break
            if body.startswith(" "):
                old_body.append(body[1:])
                old_count += 1
                new_count += 1
            elif body.startswith("-") and not body.startswith("--- "):
                old_body.append(body[1:])
                old_count += 1
            elif body.startswith("+") and not body.startswith("+++ "):
                new_count += 1
            elif body != "\\ No newline at end of file\n":
                return raw
            body_index += 1
        source_lines = source_texts[current_path].splitlines(keepends=True)
        matches = [
            offset
            for offset in range(len(source_lines) - len(old_body) + 1)
            if source_lines[offset : offset + len(old_body)] == old_body
        ]
        if old_body and len(matches) == 1:
            old_start = matches[0] + 1
            new_start = old_start + line_delta
            lines[index] = (
                f"@@ -{old_start},{old_count} +{new_start},{new_count} "
                f"@@{header.group(5)}\n"
            )
        line_delta += new_count - old_count
        index = body_index
    return "".join(lines)


def validate_candidate_diff(
    *,
    authenticated_base_root: Path,
    candidate_root: Path,
    incremental_diff: str,
    git: object,
    bounds: PatchBounds,
    source_commit: str,
    policy_interface_version: int,
    immutable_constraints_sha256: str,
    discovery_manifest_sha256: str,
) -> tuple[CandidateIdentity, str]:
    """Validate/apply one author diff and attest the fresh Git-derived cumulative state."""
    from agent_loop import (
        PatchPolicyError,
        PreflightError,
        _git,
        _parse_unified_diff,
        derive_authenticated_cumulative_diff,
        validate_unified_diff,
    )

    _require_identity_inputs(
        bounds=bounds,
        source_commit=source_commit,
        policy_interface_version=policy_interface_version,
        immutable_constraints_sha256=immutable_constraints_sha256,
        discovery_manifest_sha256=discovery_manifest_sha256,
    )
    if not all(
        isinstance(root, Path) and root.is_absolute() and root.is_dir()
        for root in (authenticated_base_root, candidate_root)
    ):
        raise ValueError("candidate roots must be absolute directories")
    base_sources = _read_policy_sources(authenticated_base_root)
    before_sources = _read_policy_sources(candidate_root)
    before_bytes = {
        path: (candidate_root / path).read_bytes() for path in EDITABLE_POLICY_PATHS
    }
    candidate_diff = _reanchor_unique_author_hunks(
        _normalize_author_diff_transport(incremental_diff),
        before_sources,
    )
    applied = False
    try:
        # The author contract permits both conventional Git diffs and standard
        # ``---``/``+++`` unified diffs.  Preserve that contract only for this
        # PIT ingestion boundary; all scope, bounds, Git-apply, AST, and
        # cumulative-diff checks remain mandatory.
        parsed = _parse_unified_diff(
            candidate_diff,
            bounds=bounds,
            allow_plain_unified_diff=True,
        )
        validate_unified_diff(
            candidate_root,
            candidate_diff,
            parsed.files,
            editable_paths=EDITABLE_POLICY_PATHS,
            gate="test",
            bounds=bounds,
            git=git,
            allow_plain_unified_diff=True,
        )
        encoded = candidate_diff.encode("utf-8")
        try:
            _git(
                candidate_root,
                "apply",
                "--check",
                "--whitespace=error-all",
                "-",
                input_bytes=encoded,
                timeout=30.0,
                git=git,
            )
            applied = True
            _git(
                candidate_root,
                "apply",
                "--whitespace=error-all",
                "-",
                input_bytes=encoded,
                timeout=30.0,
                git=git,
            )
        except PreflightError as exc:
            removed_lines = tuple(
                line[1:]
                for line in candidate_diff.splitlines(keepends=True)
                if line.startswith("-") and not line.startswith("--- ")
            )
            added_lines = tuple(
                line[1:]
                for line in candidate_diff.splitlines(keepends=True)
                if line.startswith("+") and not line.startswith("+++ ")
            )
            if removed_lines == added_lines:
                raise ValueError("candidate patch is a no-op") from exc
            raise ValueError("candidate patch does not apply") from exc
        after_sources = _read_policy_sources(candidate_root)
        if after_sources == before_sources:
            raise ValueError("candidate patch is a no-op")
        for path in EDITABLE_POLICY_PATHS:
            validate_policy_ast(path=path, source=base_sources[path])
            validate_policy_ast(path=path, source=after_sources[path])
        diff_check = _git(candidate_root, "diff", "--check", git=git)
        if diff_check.stdout or diff_check.stderr:
            raise ValueError("candidate patch fails git diff --check")
        cumulative_diff = derive_authenticated_cumulative_diff(
            git=git,
            authenticated_base_root=authenticated_base_root,
            candidate_root=candidate_root,
            editable_paths=EDITABLE_POLICY_PATHS,
        )
        if not cumulative_diff:
            raise ValueError("candidate cumulative diff is a no-op")
        cumulative = _parse_unified_diff(cumulative_diff, bounds=bounds)
        validate_unified_diff(
            candidate_root,
            cumulative_diff,
            cumulative.files,
            editable_paths=EDITABLE_POLICY_PATHS,
            gate="test",
            bounds=bounds,
            git=git,
        )
        actual_head = _git(
            authenticated_base_root,
            "rev-parse",
            "HEAD",
            git=git,
        ).stdout.decode("ascii", errors="strict").strip()
        if actual_head != source_commit:
            raise ValueError("candidate source commit differs from authenticated base")
        # The authenticated Git cumulative diff is the canonical authority for
        # file scope.  The decoded source maps above still prove that the patch
        # is not a no-op and feed symbol derivation, but they must not override
        # Git's exact line-ending and index semantics when identifying files.
        changed_paths = tuple(cumulative.files)
        changed_symbols = derive_changed_symbols(
            before_sources=base_sources,
            after_sources=after_sources,
        )
        editable_hashes = tuple(
            (path, hashlib.sha256(after_sources[path].encode("utf-8")).hexdigest())
            for path in EDITABLE_POLICY_PATHS
        )
        values: dict[str, object] = {
            "source_commit": source_commit,
            "policy_interface_version": policy_interface_version,
            "cumulative_diff_sha256": hashlib.sha256(
                cumulative_diff.encode("utf-8")
            ).hexdigest(),
            "editable_file_sha256s": editable_hashes,
            "changed_paths": changed_paths,
            "changed_symbols": changed_symbols,
            "immutable_constraints_sha256": immutable_constraints_sha256,
            "discovery_manifest_sha256": discovery_manifest_sha256,
        }
        identity = CandidateIdentity(
            **values,
            identity_sha256=_identity_digest(values),
            _controller_seal=_CANDIDATE_IDENTITY_CONSTRUCTION_SEAL,
        )
        _AUTHENTICATED_CANDIDATE_IDENTITIES[id(identity)] = identity
        return identity, cumulative_diff
    except BaseException as exc:
        if applied:
            for path, content in before_bytes.items():
                (candidate_root / path).write_bytes(content)
        if isinstance(exc, (PatchPolicyError, UnicodeDecodeError)):
            raise ValueError(str(exc)) from exc
        raise


def validate_candidate_sources(
    *,
    authenticated_base_root: Path,
    candidate_root: Path,
    replacement_sources: Mapping[str, str],
    git: object,
    source_commit: str,
    policy_interface_version: int,
    immutable_constraints_sha256: str,
    discovery_panel_plan_sha256: str,
    parent_identity_sha256: str,
) -> tuple[CandidateIdentityV4, str]:
    """Atomically validate and publish one complete three-source candidate."""
    from agent_loop import PreflightError, _git, derive_authenticated_cumulative_diff

    if re.fullmatch(r"[0-9a-f]{40}", source_commit or "") is None:
        raise ValueError("candidate source commit is invalid")
    if type(policy_interface_version) is not int or policy_interface_version <= 0:
        raise ValueError("candidate policy interface version is invalid")
    for value, label in (
        (immutable_constraints_sha256, "immutable constraint"),
        (discovery_panel_plan_sha256, "discovery panel plan"),
        (parent_identity_sha256, "parent identity"),
    ):
        if _SHA256_RE.fullmatch(value or "") is None:
            raise ValueError(f"candidate {label} digest is invalid")
    if not all(
        isinstance(root, Path) and root.is_absolute() and root.is_dir()
        for root in (authenticated_base_root, candidate_root)
    ):
        raise ValueError("candidate roots must be absolute directories")
    try:
        authenticated_base_root = _existing_path_without_links(
            authenticated_base_root
        )
        candidate_root = _existing_path_without_links(candidate_root)
    except OSError as exc:
        raise ValueError(
            "candidate roots contain a link or reparse point"
        ) from exc
    if authenticated_base_root == candidate_root:
        raise ValueError("candidate root must be disposable and distinct from its base")
    if not isinstance(replacement_sources, Mapping):
        raise ValueError("candidate replacement sources must be a mapping")
    try:
        supplied_sources = dict(replacement_sources)
    except (TypeError, ValueError) as exc:
        raise ValueError("candidate replacement sources are invalid") from exc
    if tuple(path for path in EDITABLE_POLICY_PATHS if path in supplied_sources) != (
        EDITABLE_POLICY_PATHS
    ) or set(supplied_sources) != set(EDITABLE_POLICY_PATHS):
        raise ValueError(
            "candidate replacement sources must contain exactly the three editable paths"
        )

    # Parse, compile, and enforce the closed policy language for every supplied
    # source before the first candidate byte is changed.
    canonical_sources: dict[str, str] = {}
    for path in EDITABLE_POLICY_PATHS:
        source_file = optimization_contract.AuthorSourceFile.from_source(
            path=path,
            source=supplied_sources[path],
        )
        validate_policy_ast(path=path, source=source_file.source)
        canonical_sources[path] = source_file.source

    base_sources = _read_policy_sources(authenticated_base_root)
    before_sources = _read_policy_sources(candidate_root)
    before_bytes = {
        path: (candidate_root / path).read_bytes() for path in EDITABLE_POLICY_PATHS
    }
    comparable_before_sources = {
        path: source.replace("\r\n", "\n")
        for path, source in before_sources.items()
    }
    if any("\r" in source for source in comparable_before_sources.values()):
        raise ValueError("candidate parent sources contain invalid line endings")
    if canonical_sources == comparable_before_sources:
        raise ValueError("candidate source bundle is a no-op")
    for path in EDITABLE_POLICY_PATHS:
        validate_policy_ast(path=path, source=base_sources[path])

    try:
        actual_head = _git(
            authenticated_base_root,
            "rev-parse",
            "HEAD",
            git=git,
        ).stdout.decode("ascii", errors="strict").strip()
        if actual_head != source_commit:
            raise ValueError("candidate source commit differs from authenticated base")
        if _git(
            authenticated_base_root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            git=git,
        ).stdout:
            raise ValueError("authenticated candidate base is not clean")
    except (PreflightError, UnicodeDecodeError) as exc:
        raise ValueError("candidate source provenance cannot be authenticated") from exc

    write_started = False
    try:
        for path in EDITABLE_POLICY_PATHS:
            write_started = True
            (candidate_root / path).write_bytes(canonical_sources[path].encode("utf-8"))

        after_sources = _read_policy_sources(candidate_root)
        if after_sources != canonical_sources:
            raise ValueError("candidate replacement sources were not published exactly")
        for path in EDITABLE_POLICY_PATHS:
            validate_policy_ast(path=path, source=after_sources[path])

        status = _git(
            candidate_root,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            git=git,
        ).stdout.decode("utf-8", errors="strict")
        records = tuple(item for item in status.split("\x00") if item)
        if any(
            len(item) < 4
            or item[:2] != " M"
            or item[3:] not in EDITABLE_POLICY_PATHS
            for item in records
        ):
            raise ValueError("candidate checkout contains out-of-scope changes")
        changed_names = _git(
            candidate_root,
            "diff",
            "--name-only",
            "-z",
            "HEAD",
            git=git,
        ).stdout.decode("utf-8", errors="strict")
        changed_name_set = {
            item for item in changed_names.split("\x00") if item
        }
        if not changed_name_set <= set(EDITABLE_POLICY_PATHS):
            raise ValueError("candidate checkout contains out-of-scope changes")
        if _git(
            candidate_root,
            "diff",
            "--summary",
            "HEAD",
            git=git,
        ).stdout:
            raise ValueError("candidate checkout contains structural or file-mode changes")
        for path in EDITABLE_POLICY_PATHS:
            index_entry = _git(
                candidate_root,
                "ls-files",
                "-s",
                "--",
                path,
                git=git,
            ).stdout.decode("utf-8", errors="strict").strip().split()
            if (
                len(index_entry) != 4
                or index_entry[0] != "100644"
                or index_entry[2] != "0"
                or index_entry[3] != path
            ):
                raise ValueError("candidate policy source must be a tracked 100644 file")

        _git(candidate_root, "diff", "--check", git=git)
        cumulative_diff = derive_authenticated_cumulative_diff(
            git=git,
            authenticated_base_root=authenticated_base_root,
            candidate_root=candidate_root,
            editable_paths=EDITABLE_POLICY_PATHS,
        )
        changed_paths = tuple(
            path for path in EDITABLE_POLICY_PATHS if path in changed_name_set
        )
        changed_symbols = derive_changed_symbols(
            before_sources=base_sources,
            after_sources=after_sources,
        )
        editable_hashes = tuple(
            (path, hashlib.sha256(after_sources[path].encode("utf-8")).hexdigest())
            for path in EDITABLE_POLICY_PATHS
        )
        values: dict[str, object] = {
            "source_commit": source_commit,
            "policy_interface_version": policy_interface_version,
            "cumulative_diff_sha256": hashlib.sha256(
                cumulative_diff.encode("utf-8")
            ).hexdigest(),
            "editable_file_sha256s": editable_hashes,
            "changed_paths": changed_paths,
            "changed_symbols": changed_symbols,
            "immutable_constraints_sha256": immutable_constraints_sha256,
            "discovery_panel_plan_sha256": discovery_panel_plan_sha256,
            "parent_identity_sha256": parent_identity_sha256,
        }
        identity = CandidateIdentityV4(
            **values,
            identity_sha256=_identity_digest(values),
            _controller_seal=_CANDIDATE_IDENTITY_CONSTRUCTION_SEAL,
        )
        _AUTHENTICATED_CANDIDATE_IDENTITIES[id(identity)] = identity
        return identity, cumulative_diff
    except BaseException as exc:
        rollback_errors: list[OSError] = []
        if write_started:
            for path, content in before_bytes.items():
                try:
                    (candidate_root / path).write_bytes(content)
                except OSError as rollback_error:
                    rollback_errors.append(rollback_error)
        if rollback_errors:
            raise ValueError("candidate source rollback failed") from exc
        if isinstance(exc, (PreflightError, UnicodeDecodeError)):
            raise ValueError(str(exc)) from exc
        raise


def validate_author_manifest(
    author: AuthorArtifact,
    candidate: CandidateIdentity,
) -> None:
    """Require advisory author scope to equal controller-derived scope exactly."""
    if not isinstance(author, AuthorArtifact) or not isinstance(
        candidate, CandidateIdentity
    ):
        raise ValueError("author_manifest_mismatch")
    validate_candidate_identity(candidate)
    if (
        author.changed_paths != candidate.changed_paths
        or author.changed_symbols != candidate.changed_symbols
    ):
        raise ValueError("author_manifest_mismatch")


def build_policy_source_bundle(
    *,
    candidate_root: Path,
    cumulative_diff: str,
    policy_interface_version: int,
) -> PolicySourceBundle:
    """Package exactly the current three policy modules without excerpts or truncation."""
    if (
        not isinstance(candidate_root, Path)
        or not candidate_root.is_absolute()
        or not candidate_root.is_dir()
    ):
        raise ValueError("policy source root is invalid")
    if type(policy_interface_version) is not int or policy_interface_version <= 0:
        raise ValueError("policy interface version is invalid")
    if not isinstance(cumulative_diff, str) or "\x00" in cumulative_diff:
        raise ValueError("policy cumulative diff is invalid")
    if len(cumulative_diff.encode("utf-8")) > 64 * 1024:
        raise ValueError("next_context_oversize")
    sources = _read_policy_sources(candidate_root)
    records: list[PolicySourceRecord] = []
    try:
        for path in EDITABLE_POLICY_PATHS:
            validate_policy_ast(path=path, source=sources[path])
            records.append(
                PolicySourceRecord(
                    path=path,
                    sha256=hashlib.sha256(sources[path].encode("utf-8")).hexdigest(),
                    declared_symbols=_DECLARED_SYMBOLS[path],
                    text=sources[path],
                )
            )
        return PolicySourceBundle(
            policy_interface_version=policy_interface_version,
            cumulative_diff_sha256=hashlib.sha256(
                cumulative_diff.encode("utf-8")
            ).hexdigest(),
            cumulative_diff=cumulative_diff,
            files=tuple(records),
            _controller_seal=optimization_contract._POLICY_SOURCE_BUNDLE_SEAL,
        )
    except ValueError as exc:
        if "byte cap" in str(exc) or "exceeds" in str(exc):
            raise ValueError("next_context_oversize") from exc
        raise


def require_source_context_fit(
    *,
    role_input: InvestigatorInput | AuthorInput | CriticInput,
    role_budget: PitOptimizerCallBudget,
) -> bytes:
    """Reject complete role context that cannot fit; never drop or truncate feedback."""
    role_types = {
        "investigator": InvestigatorInput,
        "author": AuthorInput,
        "critic": CriticInput,
    }
    if not isinstance(role_budget, PitOptimizerCallBudget) or type(role_input) not in (
        InvestigatorInput,
        AuthorInput,
        CriticInput,
    ):
        raise ValueError("source context contracts are invalid")
    if (
        type(role_input) is not role_types[role_budget.role]
        or role_input.iteration != role_budget.iteration
    ):
        raise ValueError("source context role or iteration differs from its budget")
    rendered = role_input.canonical_json_bytes()
    static_bytes = len(
        optimization_contract.PIT_OPTIMIZER_V2_SYSTEM_PROMPTS[
            role_budget.role
        ].encode("utf-8")
    ) + len(
        json.dumps(
            optimization_contract.pit_optimizer_response_format(role_budget.role),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    )
    if (
        static_bytes > role_budget.max_static_input_bytes
        or len(rendered) > role_budget.max_dynamic_input_bytes
        or static_bytes + len(rendered) > role_budget.max_input_tokens
    ):
        raise ValueError("context_budget_exhausted")
    return rendered
__all__ = [
    "CandidateIdentity",
    "CandidateIdentityV4",
    "EDITABLE_POLICY_PATHS",
    "LEGACY_PATCH_BOUNDS",
    "PIT_OPTIMIZER_PATCH_BOUNDS",
    "build_policy_source_bundle",
    "derive_changed_symbols",
    "require_source_context_fit",
    "validate_author_manifest",
    "validate_candidate_diff",
    "validate_candidate_sources",
    "validate_candidate_identity",
    "validate_policy_ast",
]
