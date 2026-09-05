"""Pure source, template, and separated identity contracts for optimizer V5."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
import hashlib
import json
import math
import re
from typing import Literal

from core.pit_optimizer_candidate import validate_policy_source_ast
from core.pit_optimizer_v5.contracts import HypothesisV5
from core.pit_optimizer_v5.policy_scope import (
    EDITABLE_POLICY_PATHS_V5,
    POLICY_INTERFACE_VERSION_V5,
    REQUIRED_POLICY_EXPORTS_V5,
    validate_policy_authoring_scope_v5,
    validate_policy_symbol_edit_v5,
)


LiteralValueV5 = bool | int | float | str
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PATH_ORDER_V5 = {path: index for index, path in enumerate(EDITABLE_POLICY_PATHS_V5)}

_MAX_POLICY_SOURCE_BYTES_V5 = 131_072
_MAX_POLICY_AST_NODES_V5 = 8_192
_MAX_POLICY_EXPRESSION_DEPTH_V5 = 64
_MAX_POLICY_TUPLE_ITEMS_V5 = 256
_MAX_POLICY_CALL_ARGUMENTS_V5 = 16
_MAX_POLICY_INTEGER_MAGNITUDE_V5 = 10**12
_MAX_POLICY_FLOAT_MAGNITUDE_V5 = 10**12
_PURE_POLICY_BUILTINS_V5 = frozenset(
    {
        "abs",
        "all",
        "any",
        "bool",
        "float",
        "int",
        "len",
        "max",
        "min",
        "round",
        "sum",
        "tuple",
    }
)
_SAFE_MATH_CALLS_V5 = frozenset(
    {
        "ceil",
        "copysign",
        "exp",
        "fabs",
        "floor",
        "isfinite",
        "isnan",
        "log",
        "log1p",
        "sqrt",
        "tanh",
    }
)
_SAFE_POLICY_IMPORTS_V5: Mapping[str, frozenset[str]] = {
    "core.strategy_policy.contracts": frozenset(
        {
            "AllocationDecision",
            "CapacityDecision",
            "EntryDecision",
            "EvictionDecision",
            "ExitAction",
            "ExitDecision",
        }
    ),
    "core.strategy_policy.contracts_v3": frozenset(
        {
            "AddOnDecisionV3",
            "AddOnSnapshotV3",
            "AllocationSnapshotV3",
            "CapacitySnapshotV3",
            "EntrySnapshotV3",
            "EvictionPositionV3",
            "EvictionSnapshotV3",
            "ExitSnapshotV3",
            "PortfolioFeaturesV3",
        }
    ),
    "core.strategy_policy.entry": frozenset({"evaluate_entry"}),
    "core.strategy_policy.exit": frozenset({"evaluate_exit"}),
    "core.strategy_policy.risk": frozenset(
        {"recommend_allocation", "recommend_capacity", "select_eviction"}
    ),
}
_FORBIDDEN_POLICY_NODES_V5 = (
    ast.AsyncFor,
    ast.AsyncFunctionDef,
    ast.AsyncWith,
    ast.AugAssign,
    ast.Await,
    ast.ClassDef,
    ast.Delete,
    ast.Dict,
    ast.DictComp,
    ast.For,
    ast.GeneratorExp,
    ast.Global,
    ast.Lambda,
    ast.List,
    ast.ListComp,
    ast.Match,
    ast.NamedExpr,
    ast.Nonlocal,
    ast.Raise,
    ast.SetComp,
    ast.Set,
    ast.Starred,
    ast.Try,
    ast.While,
    ast.With,
    ast.Yield,
    ast.YieldFrom,
)


def _digest(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _text(value: object, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise ValueError(f"{label} must be non-empty canonical text")
    return value


def _source_text(value: object, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or "\x00" in value
        or "\r" in value
        or value.startswith("\ufeff")
        or not value.endswith("\n")
    ):
        raise ValueError(f"{label} must be non-empty UTF-8/LF source ending in LF")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be non-empty UTF-8/LF source ending in LF") from exc
    return value


def _literal(value: object, label: str) -> LiteralValueV5:
    if type(value) not in {bool, int, float, str}:
        raise ValueError(f"{label} must be a strict JSON-compatible literal")
    if type(value) is float and not math.isfinite(value):
        raise ValueError(f"{label} must be a finite literal")
    return value  # type: ignore[return-value]


def _literal_identity(value: LiteralValueV5) -> tuple[type[object], object]:
    """Return an equality key that deliberately keeps bool distinct from int."""

    return (type(value), value)


def _primitive(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _primitive(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, tuple):
        return [_primitive(item) for item in value]
    if isinstance(value, Mapping):
        return {
            str(key): _primitive(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    return value


def _sha256(value: object) -> str:
    payload = json.dumps(
        _primitive(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _qualified_symbol(path: str, symbol: str) -> str:
    return f"{path.removesuffix('.py').replace('/', '.')}.{symbol}"


def _split_qualified_symbol(symbol: str) -> tuple[str, str]:
    for path in EDITABLE_POLICY_PATHS_V5:
        prefix = f"{path.removesuffix('.py').replace('/', '.')}."
        if symbol.startswith(prefix):
            name = symbol.removeprefix(prefix)
            if "." not in name:
                return path, name
    raise ValueError("template changed symbol is outside the V5 editable scope")


def _source_hashes(bundle: SourceBundleV5) -> tuple[tuple[str, str], ...]:
    return tuple(
        (item.path, hashlib.sha256(item.source.encode("utf-8")).hexdigest())
        for item in bundle.files
    )


def _policy_assignment_name_and_value_v5(
    statement: ast.Assign | ast.AnnAssign,
) -> tuple[str, ast.expr]:
    if isinstance(statement, ast.Assign):
        if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
            raise ValueError("policy module assignment must target one name")
        return statement.targets[0].id, statement.value
    if not isinstance(statement.target, ast.Name) or statement.value is None:
        raise ValueError("policy module assignment must target one initialized name")
    return statement.target.id, statement.value


def _policy_literal_is_deeply_immutable_v5(value: object) -> bool:
    if type(value) in {type(None), bool}:
        return True
    if type(value) is int:
        return abs(value) <= _MAX_POLICY_INTEGER_MAGNITUDE_V5
    if type(value) is float:
        return math.isfinite(value) and abs(value) <= _MAX_POLICY_FLOAT_MAGNITUDE_V5
    if type(value) is str:
        return len(value.encode("utf-8")) <= 4_096
    if type(value) is bytes:
        return len(value) <= 4_096
    return (
        type(value) is tuple
        and len(value) <= _MAX_POLICY_TUPLE_ITEMS_V5
        and all(_policy_literal_is_deeply_immutable_v5(item) for item in value)
    )


def _policy_local_target_names_v5(target: ast.expr) -> tuple[str, ...]:
    if isinstance(target, ast.Name):
        if target.id.startswith("__"):
            raise ValueError("V5 policy assignment target is invalid")
        return (target.id,)
    if isinstance(target, ast.Tuple) and 1 <= len(target.elts) <= 8:
        names = tuple(
            name
            for item in target.elts
            for name in _policy_local_target_names_v5(item)
        )
        if len(names) != len(set(names)):
            raise ValueError("V5 policy assignment targets are duplicated")
        return names
    raise ValueError("V5 policy writes require bounded local name targets")


def _policy_call_graph_is_acyclic_v5(
    functions: Mapping[str, ast.FunctionDef],
) -> bool:
    calls = {
        name: frozenset(
            node.func.id
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in functions
        )
        for name, function in functions.items()
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> bool:
        if name in visiting:
            return False
        if name in visited:
            return True
        visiting.add(name)
        if any(not visit(child) for child in calls[name]):
            return False
        visiting.remove(name)
        visited.add(name)
        return True

    return all(visit(name) for name in functions)


def _policy_ast_depth_is_bounded_v5(tree: ast.Module) -> bool:
    stack: list[tuple[ast.AST, int]] = [(tree, 0)]
    while stack:
        node, depth = stack.pop()
        expression_depth = depth + 1 if isinstance(node, ast.expr) else depth
        if expression_depth > _MAX_POLICY_EXPRESSION_DEPTH_V5:
            return False
        stack.extend((child, expression_depth) for child in ast.iter_child_nodes(node))
    return True


def _policy_expression_is_non_index_numeric_v5(expression: ast.expr) -> bool:
    """Return whether a successful expression cannot be a sequence index.

    Multiplication is the one permitted binary operator that can allocate an
    input-sized sequence.  Requiring one operand to be a guaranteed float
    keeps numeric scaling useful while making Python sequence repetition
    unreachable through the closed language.
    """

    if isinstance(expression, ast.Constant):
        return type(expression.value) is float
    if isinstance(expression, ast.UnaryOp) and isinstance(
        expression.op,
        (ast.UAdd, ast.USub),
    ):
        return _policy_expression_is_non_index_numeric_v5(expression.operand)
    if isinstance(expression, ast.BinOp):
        if isinstance(expression.op, ast.Div):
            return True
        if isinstance(expression.op, ast.Mult):
            return _policy_expression_is_non_index_numeric_v5(
                expression.left
            ) or _policy_expression_is_non_index_numeric_v5(expression.right)
        return _policy_expression_is_non_index_numeric_v5(
            expression.left
        ) and _policy_expression_is_non_index_numeric_v5(expression.right)
    if isinstance(expression, ast.Call):
        if isinstance(expression.func, ast.Name) and expression.func.id == "float":
            return True
        return (
            isinstance(expression.func, ast.Attribute)
            and isinstance(expression.func.value, ast.Name)
            and expression.func.value.id == "math"
            and expression.func.attr
            in {
                "copysign",
                "exp",
                "fabs",
                "log",
                "log1p",
                "sqrt",
                "tanh",
            }
        )
    return False


def validate_policy_source_ast_v5(*, path: str, source: str) -> ast.Module:
    """Validate one bounded, capability-closed V3 policy module.

    The evaluator still runs candidates inside its Docker sandbox.  This
    stricter controller-side language is used for deterministic semantic
    probes, so it deliberately excludes loops, recursion, dynamic calls,
    reflection, mutation helpers, and ambient imports.
    """

    if path not in EDITABLE_POLICY_PATHS_V5 or type(source) is not str:
        raise ValueError("V5 policy source authority is invalid")
    if len(source.encode("utf-8")) > _MAX_POLICY_SOURCE_BYTES_V5:
        raise ValueError("V5 policy source exceeds the bounded AST envelope")
    tree = validate_policy_source_ast(
        path=path,
        source=source,
        required_public_symbols=REQUIRED_POLICY_EXPORTS_V5[path],
    )
    nodes = tuple(ast.walk(tree))
    if len(nodes) > _MAX_POLICY_AST_NODES_V5:
        raise ValueError("V5 policy AST exceeds the bounded node envelope")
    if not _policy_ast_depth_is_bounded_v5(tree):
        raise ValueError("V5 policy AST exceeds the bounded expression depth")
    if any(isinstance(node, _FORBIDDEN_POLICY_NODES_V5) for node in nodes):
        raise ValueError("V5 policy AST contains an unbounded or stateful construct")
    if any(isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Pow, ast.MatMult)) for node in nodes):
        raise ValueError("V5 policy AST contains an unbounded arithmetic construct")

    functions: dict[str, ast.FunctionDef] = {}
    imported_call_names: set[str] = set()
    bound_module_names: set[str] = set()
    direct_import_nodes: set[int] = set()
    all_exports_seen = False
    for statement in tree.body:
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and type(
            statement.value.value
        ) is str:
            continue
        if isinstance(statement, ast.Import):
            if (
                len(statement.names) != 1
                or statement.names[0].name != "math"
                or statement.names[0].asname is not None
                or "math" in bound_module_names
            ):
                raise ValueError("V5 policy import is outside the closed allowlist")
            bound_module_names.add("math")
            direct_import_nodes.add(id(statement))
            continue
        if isinstance(statement, ast.ImportFrom):
            if statement.level != 0 or statement.module is None:
                raise ValueError("V5 policy import is outside the closed allowlist")
            if statement.module == "__future__":
                if tuple((item.name, item.asname) for item in statement.names) != (
                    ("annotations", None),
                ):
                    raise ValueError("V5 policy future import is invalid")
                direct_import_nodes.add(id(statement))
                continue
            permitted = _SAFE_POLICY_IMPORTS_V5.get(statement.module)
            if permitted is None or not statement.names:
                raise ValueError("V5 policy import is outside the closed allowlist")
            trusted_helper = statement.module in {
                "core.strategy_policy.entry",
                "core.strategy_policy.exit",
                "core.strategy_policy.risk",
            }
            for item in statement.names:
                if item.name not in permitted:
                    raise ValueError("V5 policy import name is outside the closed allowlist")
                if trusted_helper:
                    if item.asname is None or not item.asname.startswith("_") or item.asname.startswith("__"):
                        raise ValueError("trusted baseline helpers require a private local alias")
                elif item.asname is not None:
                    raise ValueError("V5 policy contract imports cannot be aliased")
                local_name = item.asname or item.name
                if local_name in bound_module_names or local_name in _PURE_POLICY_BUILTINS_V5:
                    raise ValueError("V5 policy import binding is ambiguous")
                bound_module_names.add(local_name)
                imported_call_names.add(local_name)
            direct_import_nodes.add(id(statement))
            continue
        if isinstance(statement, ast.FunctionDef):
            if (
                statement.name in bound_module_names
                or statement.name in _PURE_POLICY_BUILTINS_V5
                or statement.name.startswith("__")
            ):
                raise ValueError("V5 policy function names must be unique and non-dunder")
            if (
                statement.decorator_list
                or statement.args.defaults
                or statement.args.kw_defaults
                or statement.args.vararg is not None
                or statement.args.kwarg is not None
                or statement.args.kwonlyargs
            ):
                raise ValueError("V5 policy function signature is outside the closed contract")
            arguments = (*statement.args.posonlyargs, *statement.args.args)
            if statement.name in REQUIRED_POLICY_EXPORTS_V5[path]:
                if len(arguments) != 1:
                    raise ValueError("V5 public policy functions require one snapshot argument")
            elif not statement.name.startswith("_") or not 1 <= len(arguments) <= 8:
                raise ValueError("V5 private helper signature is outside the bounded contract")
            if any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not statement
                for node in ast.walk(statement)
            ):
                raise ValueError("nested V5 policy functions are forbidden")
            functions[statement.name] = statement
            bound_module_names.add(statement.name)
            continue
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            name, value = _policy_assignment_name_and_value_v5(statement)
            try:
                literal = ast.literal_eval(value)
            except (TypeError, ValueError):
                raise ValueError("V5 policy module constants must be literal") from None
            if name == "__all__":
                if all_exports_seen or type(literal) is not tuple:
                    raise ValueError("V5 policy export declaration is invalid")
                exports = tuple(literal)
                if (
                    any(type(item) is not str for item in exports)
                    or len(set(exports)) != len(exports)
                    or set(exports) != set(REQUIRED_POLICY_EXPORTS_V5[path])
                ):
                    raise ValueError("V5 policy export declaration differs from the interface")
                all_exports_seen = True
            elif (
                not name.isupper()
                or name.startswith("_")
                or not _policy_literal_is_deeply_immutable_v5(literal)
            ):
                raise ValueError("V5 policy module assignment is outside the constant scope")
            if name in bound_module_names:
                raise ValueError("V5 policy module binding is duplicated")
            bound_module_names.add(name)
            continue
        raise ValueError("V5 policy module statement is outside the closed language")
    if not all_exports_seen:
        raise ValueError("V5 policy module must declare its exact public exports")

    allowed_calls = _PURE_POLICY_BUILTINS_V5 | frozenset(imported_call_names) | frozenset(functions)
    for node in nodes:
        if isinstance(node, (ast.Import, ast.ImportFrom)) and id(node) not in direct_import_nodes:
            raise ValueError("nested V5 policy imports are forbidden")
        if isinstance(node, ast.Name) and node.id.startswith("__") and node.id != "__all__":
            raise ValueError("V5 policy dunder access is forbidden")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError("V5 policy private attribute access is forbidden")
        if isinstance(node, ast.Constant):
            if type(node.value) not in {type(None), bool, int, float, str, bytes}:
                raise ValueError("V5 policy constants must be bounded JSON literals")
            if type(node.value) is int and abs(node.value) > _MAX_POLICY_INTEGER_MAGNITUDE_V5:
                raise ValueError("V5 policy integer literal is too large")
            if type(node.value) is float and (
                not math.isfinite(node.value)
                or abs(node.value) > _MAX_POLICY_FLOAT_MAGNITUDE_V5
            ):
                raise ValueError("V5 policy float literal is invalid")
            if type(node.value) is str and len(node.value.encode("utf-8")) > 4_096:
                raise ValueError("V5 policy string or bytes literal is too large")
            if type(node.value) is bytes and len(node.value) > 4_096:
                raise ValueError("V5 policy string or bytes literal is too large")
        if isinstance(node, ast.Tuple) and len(node.elts) > _MAX_POLICY_TUPLE_ITEMS_V5:
            raise ValueError("V5 policy tuple literal is too large")
        if isinstance(node, ast.BinOp):
            if not isinstance(
                node.op,
                (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod),
            ):
                raise ValueError("V5 policy binary operator is outside the bounded set")
            if isinstance(node.op, ast.Mult):
                try:
                    literal_left = ast.literal_eval(node.left)
                    literal_right = ast.literal_eval(node.right)
                except (TypeError, ValueError):
                    literal_left = literal_right = None
                constant_numeric_product = (
                    type(literal_left) in {int, float}
                    and type(literal_right) in {int, float}
                    and abs(literal_left * literal_right)
                    <= _MAX_POLICY_FLOAT_MAGNITUDE_V5
                )
                if not constant_numeric_product and not (
                    _policy_expression_is_non_index_numeric_v5(node.left)
                    or _policy_expression_is_non_index_numeric_v5(node.right)
                ):
                    raise ValueError("V5 policy sequence repetition is forbidden")
        if not isinstance(node, ast.Call):
            continue
        if len(node.args) + len(node.keywords) > _MAX_POLICY_CALL_ARGUMENTS_V5:
            raise ValueError("V5 policy call exceeds the bounded argument count")
        if any(keyword.arg is None for keyword in node.keywords) or any(
            isinstance(argument, ast.Starred) for argument in node.args
        ):
            raise ValueError("V5 policy variadic calls are forbidden")
        if isinstance(node.func, ast.Name):
            if node.func.id not in allowed_calls:
                raise ValueError("V5 policy call is outside the pure allowlist")
            continue
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "math"
            and node.func.attr in _SAFE_MATH_CALLS_V5
        ):
            continue
        raise ValueError("V5 policy attribute and dynamic calls are forbidden")
    for function in functions.values():
        protected_names = allowed_calls | {"math"}
        arguments = (*function.args.posonlyargs, *function.args.args)
        if any(argument.arg in protected_names or argument.arg.startswith("__") for argument in arguments):
            raise ValueError("V5 policy argument shadows a callable authority")
        local_names: set[str] = set()
        for node in ast.walk(function):
            if isinstance(node, ast.Assign):
                names = tuple(
                    name
                    for target in node.targets
                    for name in _policy_local_target_names_v5(target)
                )
                if len(names) > 8 or len(names) != len(set(names)):
                    raise ValueError("V5 policy assignment targets are invalid")
                local_names.update(names)
            elif isinstance(node, ast.AnnAssign):
                local_names.update(_policy_local_target_names_v5(node.target))
            if isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(
                node.ctx,
                (ast.Store, ast.Del),
            ):
                raise ValueError("V5 policy mutation target is forbidden")
        if local_names & protected_names:
            raise ValueError("V5 policy local shadows a callable authority")
        available_names = bound_module_names | _PURE_POLICY_BUILTINS_V5 | {
            argument.arg for argument in arguments
        } | local_names
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id not in available_names
            ):
                raise ValueError("V5 policy name resolves outside the closed lexical scope")
    if not _policy_call_graph_is_acyclic_v5(functions):
        raise ValueError("V5 policy helper recursion is forbidden")
    return tree


def _policy_symbol_nodes_v5(source_file: SourceFileV5) -> dict[str, str]:
    tree = validate_policy_source_ast_v5(path=source_file.path, source=source_file.source)
    result: dict[str, str] = {}
    for statement in tree.body:
        if isinstance(statement, ast.FunctionDef):
            result[statement.name] = ast.dump(statement, include_attributes=False)
        elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
            name, _value = _policy_assignment_name_and_value_v5(statement)
            if name.isupper() and not name.startswith("_"):
                result[name] = ast.dump(statement, include_attributes=False)
    return result


def derive_changed_symbols_v5(
    *,
    before: SourceBundleV5,
    after: SourceBundleV5,
) -> tuple[str, ...]:
    """Return exact qualified top-level symbols changed across two V5 bundles."""

    if type(before) is not SourceBundleV5 or type(after) is not SourceBundleV5:
        raise ValueError("V5 changed-symbol inputs must be source bundles")
    before_files = {item.path: item for item in before.files}
    after_files = {item.path: item for item in after.files}
    if tuple(before_files) != EDITABLE_POLICY_PATHS_V5 or tuple(after_files) != EDITABLE_POLICY_PATHS_V5:
        raise ValueError("V5 changed-symbol source paths differ")
    changed: list[str] = []
    for path in EDITABLE_POLICY_PATHS_V5:
        left = _policy_symbol_nodes_v5(before_files[path])
        right = _policy_symbol_nodes_v5(after_files[path])
        module = path.removesuffix(".py").replace("/", ".")
        changed.extend(
            f"{module}.{name}"
            for name in left.keys() | right.keys()
            if left.get(name) != right.get(name)
        )
    return tuple(sorted(changed))


@dataclass(frozen=True, slots=True)
class PolicyRevisionIdentityV5:
    """Panel-independent identity of one exact V3 policy source revision."""

    policy_interface_version: Literal[3]
    trusted_policy_runtime_sha256: str
    immutable_constraints_sha256: str
    editable_source_sha256: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if (
            type(self.policy_interface_version) is not int
            or self.policy_interface_version != POLICY_INTERFACE_VERSION_V5
        ):
            raise ValueError("policy revision interface must be the exact V3 interface")
        _digest(self.trusted_policy_runtime_sha256, "trusted policy runtime identity")
        _digest(self.immutable_constraints_sha256, "immutable constraint identity")
        if (
            type(self.editable_source_sha256) is not tuple
            or len(self.editable_source_sha256) != len(EDITABLE_POLICY_PATHS_V5)
            or any(type(item) is not tuple or len(item) != 2 for item in self.editable_source_sha256)
            or tuple(path for path, _ in self.editable_source_sha256)
            != EDITABLE_POLICY_PATHS_V5
        ):
            raise ValueError("policy revision must identify the exact four editable sources")
        for path, digest in self.editable_source_sha256:
            if type(path) is not str:
                raise ValueError("policy revision source path is invalid")
            _digest(digest, f"policy revision source {path}")

    def to_primitive(self) -> dict[str, object]:
        """Return the complete canonical identity payload."""

        return {
            "policy_interface_version": self.policy_interface_version,
            "trusted_policy_runtime_sha256": self.trusted_policy_runtime_sha256,
            "immutable_constraints_sha256": self.immutable_constraints_sha256,
            "editable_source_sha256": self.editable_source_sha256,
        }

    @property
    def sha256(self) -> str:
        """Hash only interface, trusted runtime, constraints, and exact source bytes."""

        return _sha256(self.to_primitive())


@dataclass(frozen=True, slots=True)
class LiteralAxisV5:
    """One named, strictly typed literal dimension for local rendering."""

    name: str
    default: LiteralValueV5
    values: tuple[LiteralValueV5, ...]

    def __post_init__(self) -> None:
        name = _text(self.name, "literal axis name")
        if not name.isidentifier() or name.startswith("_"):
            raise ValueError("literal axis name must be a public Python identifier")
        default = _literal(self.default, f"literal axis {name} default")
        if type(self.values) is not tuple or not self.values:
            raise ValueError("literal axis values must be a non-empty tuple")
        identities: list[tuple[type[object], object]] = []
        for value in self.values:
            identities.append(
                _literal_identity(_literal(value, f"literal axis {name} value"))
            )
        if len(identities) != len(set(identities)):
            raise ValueError("literal axis values must be unique by strict primitive type")
        if _literal_identity(default) not in identities:
            raise ValueError("literal axis default must occur in its declared values")

    def to_primitive(self) -> dict[str, object]:
        """Return this axis as canonical primitive data."""

        return {"name": self.name, "default": self.default, "values": self.values}

    @property
    def sha256(self) -> str:
        return _sha256(self.to_primitive())


@dataclass(frozen=True, slots=True)
class SourceFileV5:
    """One complete canonical V5 policy module at its fixed repository path."""

    path: str
    source: str

    def __post_init__(self) -> None:
        if type(self.path) is not str or self.path not in EDITABLE_POLICY_PATHS_V5:
            raise ValueError("policy source path is outside the V5 editable scope")
        source = _source_text(self.source, f"policy source {self.path}")
        tree = validate_policy_source_ast_v5(path=self.path, source=source)
        public_definitions = tuple(
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
        )
        if len(public_definitions) != len(set(public_definitions)):
            raise ValueError("policy public symbols must have one definition each")

    def to_primitive(self) -> dict[str, str]:
        """Return the exact source and its path without normalization."""

        return {"path": self.path, "source": self.source}

    @property
    def sha256(self) -> str:
        """Return the digest of the exact UTF-8 source bytes."""

        return hashlib.sha256(self.source.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SourceBundleV5:
    """The atomic, ordered four-file V5 editable policy bundle."""

    files: tuple[SourceFileV5, ...]

    def __post_init__(self) -> None:
        if (
            type(self.files) is not tuple
            or any(type(item) is not SourceFileV5 for item in self.files)
            or tuple(item.path for item in self.files) != EDITABLE_POLICY_PATHS_V5
        ):
            raise ValueError("policy source bundle must contain the exact ordered four-file scope")

    def to_primitive(self) -> dict[str, object]:
        """Return the exact complete source bundle as primitive data."""

        return {"files": tuple(item.to_primitive() for item in self.files)}

    @property
    def sha256(self) -> str:
        return _sha256(self.to_primitive())


@dataclass(frozen=True, slots=True)
class SourceOperationV5:
    """One authorized whole-function or module-constant source replacement."""

    path: str
    symbol: str
    kind: Literal["replace_function", "replace_constant"]
    replacement_source: str

    def __post_init__(self) -> None:
        if self.kind == "replace_function":
            scope_kind = "function"
        elif self.kind == "replace_constant":
            scope_kind = "constant"
        else:
            raise ValueError("source operation kind is invalid")
        validate_policy_symbol_edit_v5(
            path=self.path,
            symbol=self.symbol,
            kind=scope_kind,
        )
        source = _source_text(self.replacement_source, "source operation replacement")
        try:
            tree = ast.parse(source, filename=self.path)
            compile(tree, self.path, "exec", dont_inherit=True)
        except (SyntaxError, ValueError, TypeError) as exc:
            raise ValueError("source operation replacement syntax is invalid") from exc
        if len(tree.body) != 1:
            raise ValueError("source operation must contain exactly one complete replacement")
        statement = tree.body[0]
        if self.kind == "replace_function":
            if (
                not isinstance(statement, ast.FunctionDef)
                or statement.name != self.symbol
                or statement.decorator_list
            ):
                raise ValueError("function operation must replace one complete authorized function")
        elif not _is_constant_replacement(statement, self.symbol):
            raise ValueError("constant operation must replace one complete authorized constant")

    def to_primitive(self) -> dict[str, str]:
        """Return this complete replacement as canonical primitive data."""

        return {
            "path": self.path,
            "symbol": self.symbol,
            "kind": self.kind,
            "replacement_source": self.replacement_source,
        }

    @property
    def sha256(self) -> str:
        return _sha256(self.to_primitive())


def _is_constant_replacement(statement: ast.stmt, symbol: str) -> bool:
    if (
        isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
    ):
        return statement.targets[0].id == symbol
    return isinstance(statement, ast.AnnAssign) and (
        isinstance(statement.target, ast.Name)
        and statement.target.id == symbol
        and statement.value is not None
    )


def _declared_source_symbols(source_file: SourceFileV5) -> frozenset[str]:
    tree = ast.parse(source_file.source, filename=source_file.path)
    symbols: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, ast.FunctionDef):
            symbols.add(statement.name)
        elif (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
        ):
            symbols.add(statement.targets[0].id)
        elif isinstance(statement, ast.AnnAssign) and isinstance(
            statement.target, ast.Name
        ):
            symbols.add(statement.target.id)
    return frozenset(symbols)


@dataclass(frozen=True, slots=True)
class VariantAssignmentV5:
    """One canonical sorted assignment of strict primitive axis values."""

    values: tuple[tuple[str, LiteralValueV5], ...]

    def __post_init__(self) -> None:
        if (
            type(self.values) is not tuple
            or any(type(item) is not tuple or len(item) != 2 for item in self.values)
        ):
            raise ValueError("variant assignment values are invalid")
        names: list[str] = []
        for name, value in self.values:
            if type(name) is not str or not name.isidentifier() or name.startswith("_"):
                raise ValueError("variant assignment axis name is invalid")
            _literal(value, f"variant assignment {name}")
            names.append(name)
        if names != sorted(names) or len(names) != len(set(names)):
            raise ValueError("variant assignment axes must be unique and sorted")

    def to_primitive(self) -> dict[str, object]:
        """Return the canonical assignment payload."""

        return {"values": self.values}

    @property
    def sha256(self) -> str:
        return _sha256(self.to_primitive())


@dataclass(frozen=True, slots=True)
class StructuralTemplateV5:
    """One bounded author template in normal-operation or full-source mode."""

    hypothesis_id: str
    parent_revision_sha256: str
    changed_symbols: tuple[str, ...]
    source_operations: tuple[SourceOperationV5, ...]
    axes: tuple[LiteralAxisV5, ...]
    full_source_escape: tuple[SourceFileV5, ...] | None

    def __post_init__(self) -> None:
        _text(self.hypothesis_id, "structural template hypothesis ID")
        _digest(self.parent_revision_sha256, "structural template parent revision")
        if (
            type(self.changed_symbols) is not tuple
            or not self.changed_symbols
            or any(type(symbol) is not str for symbol in self.changed_symbols)
            or tuple(sorted(self.changed_symbols)) != self.changed_symbols
            or len(set(self.changed_symbols)) != len(self.changed_symbols)
        ):
            raise ValueError("template changed symbols must be non-empty, unique, and sorted")
        if (
            type(self.source_operations) is not tuple
            or any(type(item) is not SourceOperationV5 for item in self.source_operations)
            or type(self.axes) is not tuple
            or any(type(item) is not LiteralAxisV5 for item in self.axes)
        ):
            raise ValueError("structural template operations or axes are invalid")
        axis_names = tuple(axis.name for axis in self.axes)
        if axis_names != tuple(sorted(axis_names)) or len(set(axis_names)) != len(axis_names):
            raise ValueError("structural template axes must be unique and sorted")

        if self.full_source_escape is not None and (
            type(self.full_source_escape) is not tuple
            or any(type(item) is not SourceFileV5 for item in self.full_source_escape)
        ):
            raise ValueError("structural template full-source escape is invalid")
        full_source_paths = (
            tuple(item.path for item in self.full_source_escape)
            if self.full_source_escape is not None
            else ()
        )
        symbol_edits = tuple(
            (
                operation.path,
                operation.symbol,
                "function" if operation.kind == "replace_function" else "constant",
            )
            for operation in self.source_operations
        )
        validate_policy_authoring_scope_v5(
            symbol_edits=symbol_edits,
            full_source_paths=full_source_paths,
        )

        for symbol in self.changed_symbols:
            path, name = _split_qualified_symbol(symbol)
            validate_policy_symbol_edit_v5(
                path=path,
                symbol=name,
                kind="function" if name in REQUIRED_POLICY_EXPORTS_V5[path] else "constant",
            )
        if self.source_operations:
            canonical_operations = tuple(
                sorted(
                    self.source_operations,
                    key=lambda item: (_PATH_ORDER_V5[item.path], item.symbol),
                )
            )
            if self.source_operations != canonical_operations:
                raise ValueError("structural template operations must be canonically sorted")
            expected_symbols = tuple(
                sorted(
                    _qualified_symbol(operation.path, operation.symbol)
                    for operation in self.source_operations
                )
            )
            if self.changed_symbols != expected_symbols:
                raise ValueError("template changed symbols must exactly match source operations")
        elif self.full_source_escape is not None:
            declared_by_path = {
                item.path: _declared_source_symbols(item) for item in self.full_source_escape
            }
            if any(
                name not in declared_by_path[path]
                for symbol in self.changed_symbols
                for path, name in (_split_qualified_symbol(symbol),)
            ):
                raise ValueError("full-source changed symbols must exist in replacement sources")

    def to_primitive(self) -> dict[str, object]:
        """Return the complete template, including exact replacement source bytes."""

        return {
            "hypothesis_id": self.hypothesis_id,
            "parent_revision_sha256": self.parent_revision_sha256,
            "changed_symbols": self.changed_symbols,
            "source_operations": tuple(item.to_primitive() for item in self.source_operations),
            "axes": tuple(item.to_primitive() for item in self.axes),
            "full_source_escape": (
                tuple(item.to_primitive() for item in self.full_source_escape)
                if self.full_source_escape is not None
                else None
            ),
        }

    @property
    def sha256(self) -> str:
        return _sha256(self.to_primitive())


def _validated_template_assignment_digests_v5(
    *,
    template: StructuralTemplateV5,
    assignment: VariantAssignmentV5,
) -> tuple[str, str]:
    """Validate one exact template assignment and return its bound digests."""

    axis_names = tuple(axis.name for axis in template.axes)
    assignment_names = tuple(name for name, _ in assignment.values)
    if assignment_names != axis_names:
        raise ValueError(
            "experiment assignment axes must exactly match the structural template"
        )
    for axis, (_, value) in zip(template.axes, assignment.values, strict=True):
        permitted_values = tuple(_literal_identity(item) for item in axis.values)
        if _literal_identity(value) not in permitted_values:
            raise ValueError(
                "experiment assignment value is outside its strictly typed template axis"
            )
    return template.sha256, assignment.sha256


@dataclass(frozen=True, slots=True)
class RenderedVariantV5:
    """A rendered assignment whose source bytes exactly match its policy identity."""

    assignment: VariantAssignmentV5
    source_bundle: SourceBundleV5
    policy_revision: PolicyRevisionIdentityV5

    def __post_init__(self) -> None:
        if (
            type(self.assignment) is not VariantAssignmentV5
            or type(self.source_bundle) is not SourceBundleV5
            or type(self.policy_revision) is not PolicyRevisionIdentityV5
        ):
            raise ValueError("rendered variant components are invalid")
        if self.policy_revision.editable_source_sha256 != _source_hashes(self.source_bundle):
            raise ValueError("rendered variant policy identity differs from exact source bytes")

    def to_primitive(self) -> dict[str, object]:
        """Return the complete rendered variant as canonical primitive data."""

        return {
            "assignment": self.assignment.to_primitive(),
            "source_bundle": self.source_bundle.to_primitive(),
            "policy_revision": self.policy_revision.to_primitive(),
        }

    @property
    def sha256(self) -> str:
        return _sha256(self.to_primitive())


@dataclass(frozen=True, slots=True)
class ExperimentIdentityV5:
    """Identity of one validated policy revision in one evaluation context."""

    policy_revision_sha256: str
    parent_revision_sha256: str
    hypothesis_sha256: str
    template_sha256: str
    assignment_sha256: str
    round_index: int
    discovery_plan_sha256: str

    def __post_init__(self) -> None:
        _digest(self.policy_revision_sha256, "experiment policy revision")
        for value, label in (
            (self.parent_revision_sha256, "experiment parent revision"),
            (self.hypothesis_sha256, "experiment hypothesis"),
            (self.template_sha256, "experiment template"),
            (self.assignment_sha256, "experiment assignment"),
            (self.discovery_plan_sha256, "experiment discovery plan"),
        ):
            _digest(value, label)
        if type(self.round_index) is not int or self.round_index <= 0:
            raise ValueError("experiment round index must be a positive integer")

    def to_primitive(self) -> dict[str, object]:
        """Return all policy lineage and evaluation-context bindings."""

        return {
            "policy_revision_sha256": self.policy_revision_sha256,
            "parent_revision_sha256": self.parent_revision_sha256,
            "hypothesis_sha256": self.hypothesis_sha256,
            "template_sha256": self.template_sha256,
            "assignment_sha256": self.assignment_sha256,
            "round_index": self.round_index,
            "discovery_plan_sha256": self.discovery_plan_sha256,
        }

    @property
    def sha256(self) -> str:
        return _sha256(self.to_primitive())


@dataclass(frozen=True, slots=True)
class PreValidationInvalidExperimentIdentityV5:
    """Typed identity for a variant rejected before policy revision validation."""

    parent_revision_sha256: str
    hypothesis_sha256: str
    template_sha256: str
    assignment_sha256: str
    round_index: int
    discovery_plan_sha256: str
    policy_revision_sha256: None = field(init=False, default=None)

    def __post_init__(self) -> None:
        for value, label in (
            (self.parent_revision_sha256, "experiment parent revision"),
            (self.hypothesis_sha256, "experiment hypothesis"),
            (self.template_sha256, "experiment template"),
            (self.assignment_sha256, "experiment assignment"),
            (self.discovery_plan_sha256, "experiment discovery plan"),
        ):
            _digest(value, label)
        if type(self.round_index) is not int or self.round_index <= 0:
            raise ValueError("experiment round index must be a positive integer")

    def to_primitive(self) -> dict[str, object]:
        """Return explicit null policy identity and all pre-validation bindings."""

        return {
            "policy_revision_sha256": self.policy_revision_sha256,
            "parent_revision_sha256": self.parent_revision_sha256,
            "hypothesis_sha256": self.hypothesis_sha256,
            "template_sha256": self.template_sha256,
            "assignment_sha256": self.assignment_sha256,
            "round_index": self.round_index,
            "discovery_plan_sha256": self.discovery_plan_sha256,
        }

    @property
    def sha256(self) -> str:
        return _sha256(self.to_primitive())


def derive_policy_revision_identity_v5(
    *,
    source_bundle: SourceBundleV5,
    trusted_policy_runtime_sha256: str,
    immutable_constraints_sha256: str,
) -> PolicyRevisionIdentityV5:
    """Derive the stable V5 revision from the complete exact four-source bundle."""

    if type(source_bundle) is not SourceBundleV5:
        raise ValueError("policy revision source bundle must use the V5 schema")
    return PolicyRevisionIdentityV5(
        policy_interface_version=POLICY_INTERFACE_VERSION_V5,
        trusted_policy_runtime_sha256=trusted_policy_runtime_sha256,
        immutable_constraints_sha256=immutable_constraints_sha256,
        editable_source_sha256=_source_hashes(source_bundle),
    )


def derive_experiment_identity_v5(
    *,
    policy_revision: PolicyRevisionIdentityV5,
    parent_revision_sha256: str,
    hypothesis: HypothesisV5,
    template: StructuralTemplateV5,
    assignment: VariantAssignmentV5,
    round_index: int,
    discovery_plan_sha256: str,
) -> ExperimentIdentityV5:
    """Construct one post-validation experiment with a mandatory exact policy revision."""

    if type(policy_revision) is not PolicyRevisionIdentityV5:
        raise ValueError("post-validation experiment requires a V5 policy revision")
    if type(hypothesis) is not HypothesisV5:
        raise ValueError("experiment hypothesis must use the V5 schema")
    if type(template) is not StructuralTemplateV5:
        raise ValueError("experiment template must use the V5 schema")
    if type(assignment) is not VariantAssignmentV5:
        raise ValueError("experiment assignment must use the V5 schema")
    if template.parent_revision_sha256 != parent_revision_sha256:
        raise ValueError("experiment parent revision differs from its template")
    if template.hypothesis_id != hypothesis.hypothesis_id:
        raise ValueError("experiment hypothesis differs from its template")
    template_sha256, assignment_sha256 = _validated_template_assignment_digests_v5(
        template=template,
        assignment=assignment,
    )
    return ExperimentIdentityV5(
        policy_revision_sha256=policy_revision.sha256,
        parent_revision_sha256=parent_revision_sha256,
        hypothesis_sha256=_sha256(hypothesis),
        template_sha256=template_sha256,
        assignment_sha256=assignment_sha256,
        round_index=round_index,
        discovery_plan_sha256=discovery_plan_sha256,
    )


def derive_pre_validation_invalid_experiment_identity_v5(
    *,
    parent_revision_sha256: str,
    hypothesis: HypothesisV5,
    template: StructuralTemplateV5,
    assignment: VariantAssignmentV5,
    round_index: int,
    discovery_plan_sha256: str,
) -> PreValidationInvalidExperimentIdentityV5:
    """Construct the sole identity form permitted before policy validation succeeds."""

    if type(hypothesis) is not HypothesisV5:
        raise ValueError("pre-validation experiment hypothesis must use the V5 schema")
    if type(template) is not StructuralTemplateV5:
        raise ValueError("pre-validation experiment template must use the V5 schema")
    if type(assignment) is not VariantAssignmentV5:
        raise ValueError("pre-validation experiment assignment must use the V5 schema")
    if template.parent_revision_sha256 != parent_revision_sha256:
        raise ValueError("pre-validation experiment parent differs from its template")
    if template.hypothesis_id != hypothesis.hypothesis_id:
        raise ValueError("pre-validation experiment hypothesis differs from its template")
    template_sha256, assignment_sha256 = _validated_template_assignment_digests_v5(
        template=template,
        assignment=assignment,
    )
    return PreValidationInvalidExperimentIdentityV5(
        parent_revision_sha256=parent_revision_sha256,
        hypothesis_sha256=_sha256(hypothesis),
        template_sha256=template_sha256,
        assignment_sha256=assignment_sha256,
        round_index=round_index,
        discovery_plan_sha256=discovery_plan_sha256,
    )


def validate_post_validation_experiment_identity_v5(
    identity: ExperimentIdentityV5,
    *,
    policy_revision: PolicyRevisionIdentityV5,
) -> None:
    """Reject null or mismatched policy identity at every post-validation boundary."""

    if type(identity) is not ExperimentIdentityV5:
        raise ValueError("post-validation experiment identity must use the V5 schema")
    if type(policy_revision) is not PolicyRevisionIdentityV5:
        raise ValueError("post-validation policy revision must use the V5 schema")
    if identity.policy_revision_sha256 is None:
        raise ValueError("post-validation experiment identity cannot carry a null policy revision")
    if identity.policy_revision_sha256 != policy_revision.sha256:
        raise ValueError("post-validation experiment policy revision is not exact")


__all__ = [
    "ExperimentIdentityV5",
    "LiteralAxisV5",
    "LiteralValueV5",
    "PolicyRevisionIdentityV5",
    "PreValidationInvalidExperimentIdentityV5",
    "RenderedVariantV5",
    "SourceBundleV5",
    "SourceFileV5",
    "SourceOperationV5",
    "StructuralTemplateV5",
    "VariantAssignmentV5",
    "derive_experiment_identity_v5",
    "derive_changed_symbols_v5",
    "derive_policy_revision_identity_v5",
    "derive_pre_validation_invalid_experiment_identity_v5",
    "validate_policy_source_ast_v5",
    "validate_post_validation_experiment_identity_v5",
]
