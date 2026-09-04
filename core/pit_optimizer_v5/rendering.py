"""Deterministic, source-span-preserving local variant rendering for optimizer V5."""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from itertools import product
import io
import math
import token
import tokenize

from core.pit_optimizer_v5.candidate_ir import (
    LiteralAxisV5,
    LiteralValueV5,
    PolicyRevisionIdentityV5,
    RenderedVariantV5,
    SourceBundleV5,
    SourceFileV5,
    SourceOperationV5,
    StructuralTemplateV5,
    VariantAssignmentV5,
    derive_policy_revision_identity_v5,
)
from core.pit_optimizer_v5.policy_scope import (
    EDITABLE_POLICY_PATHS_V5,
    REQUIRED_POLICY_EXPORTS_V5,
    validate_policy_symbol_edit_v5,
)


_AXIS_MARKER = "PIT_AXIS"


class VariantRenderingCollisionV5(ValueError):
    """Two distinct strict assignments rendered the same complete source bytes."""


@dataclass(frozen=True, slots=True)
class _SourceReplacement:
    start: int
    end: int
    source: str

    def __post_init__(self) -> None:
        if (
            type(self.start) is not int
            or type(self.end) is not int
            or self.start < 0
            or self.end <= self.start
            or type(self.source) is not str
            or not self.source
        ):
            raise ValueError("source replacement span is invalid")


def _parse_source(*, path: str, source: str) -> ast.Module:
    try:
        return ast.parse(source, filename=path)
    except (SyntaxError, ValueError, TypeError) as exc:
        raise ValueError("rendering source syntax is invalid") from exc


def _line_character_offset(source: str, *, line_number: int, byte_column: int) -> int:
    lines = source.splitlines(keepends=True)
    if (
        type(line_number) is not int
        or type(byte_column) is not int
        or line_number <= 0
        or line_number > len(lines)
        or byte_column < 0
    ):
        raise ValueError("source location is invalid")
    line = lines[line_number - 1]
    encoded = line.encode("utf-8")
    if byte_column > len(encoded):
        raise ValueError("source byte column is invalid")
    try:
        prefix = encoded[:byte_column].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("source byte column splits a UTF-8 character") from exc
    return sum(len(item) for item in lines[: line_number - 1]) + len(prefix)


def _node_span(source: str, node: ast.AST) -> tuple[int, int]:
    if (
        not hasattr(node, "lineno")
        or not hasattr(node, "col_offset")
        or getattr(node, "end_lineno", None) is None
        or getattr(node, "end_col_offset", None) is None
    ):
        raise ValueError("source node has no complete location")
    start = _line_character_offset(
        source,
        line_number=node.lineno,
        byte_column=node.col_offset,
    )
    end = _line_character_offset(
        source,
        line_number=node.end_lineno,
        byte_column=node.end_col_offset,
    )
    if end <= start:
        raise ValueError("source node span is empty")
    return start, end


def _statement_line_span(
    source: str,
    *,
    tree: ast.Module,
    node: ast.stmt,
) -> tuple[int, int]:
    if node not in tree.body or node.col_offset != 0 or node.end_lineno is None:
        raise ValueError("source operation target must be one top-level statement")
    if any(
        other is not node
        and other.end_lineno is not None
        and other.lineno <= node.end_lineno
        and node.lineno <= other.end_lineno
        for other in tree.body
    ):
        raise ValueError("source operation target shares a physical line")
    lines = source.splitlines(keepends=True)
    if node.end_lineno > len(lines) or not lines[node.end_lineno - 1].endswith("\n"):
        raise ValueError("source operation target must end on an LF-terminated line")
    return (
        sum(len(item) for item in lines[: node.lineno - 1]),
        sum(len(item) for item in lines[: node.end_lineno]),
    )


def _apply_replacements(
    source: str,
    replacements: tuple[_SourceReplacement, ...],
) -> str:
    ordered = tuple(sorted(replacements, key=lambda item: (item.start, item.end)))
    previous_end = -1
    for replacement in ordered:
        if replacement.end > len(source):
            raise ValueError("source replacement exceeds its source")
        if replacement.start < previous_end:
            raise ValueError("source replacements overlap")
        previous_end = replacement.end
    rendered = source
    for replacement in reversed(ordered):
        rendered = (
            rendered[: replacement.start]
            + replacement.source
            + rendered[replacement.end :]
        )
    return rendered


def _canonical_python_literal(value: LiteralValueV5) -> str:
    if type(value) is bool:
        rendered = "True" if value else "False"
    elif type(value) is int:
        rendered = str(value)
    elif type(value) is float:
        if not math.isfinite(value):
            raise ValueError("axis value must be a finite strict primitive")
        rendered = repr(value)
    elif type(value) is str:
        rendered = repr(value)
    else:
        raise ValueError("axis value must be a supported strict primitive")
    try:
        parsed = ast.literal_eval(rendered)
    except (SyntaxError, ValueError) as exc:
        raise ValueError("axis value has no canonical Python literal") from exc
    if not _strict_literal_equal(parsed, value):
        raise ValueError("axis literal does not preserve its strict primitive type")
    # Parentheses preserve call-expression precedence for negative numeric values.
    return f"({rendered})"


def _assert_no_marker_token(*, path: str, source: str) -> None:
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        if any(item.type == token.NAME and item.string == _AXIS_MARKER for item in tokens):
            raise ValueError(f"rendered source {path} retains a PIT_AXIS marker")
    except (IndentationError, tokenize.TokenError) as exc:
        raise ValueError("rendered source tokenization is invalid") from exc


def _render_markers(
    *,
    path: str,
    source: str,
    authorized_spans: tuple[tuple[int, int], ...],
    assignment: Mapping[str, LiteralValueV5],
) -> tuple[str, tuple[str, ...]]:
    tree = _parse_source(path=path, source=source)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    replacements: list[_SourceReplacement] = []
    marker_names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Name) or node.id != _AXIS_MARKER:
            continue
        call = parents.get(node)
        if (
            not isinstance(call, ast.Call)
            or call.func is not node
            or len(call.args) != 1
            or call.keywords
            or not isinstance(call.args[0], ast.Constant)
            or type(call.args[0].value) is not str
        ):
            raise ValueError('axis marker must be exactly PIT_AXIS("name")')
        start, end = _node_span(source, call)
        if not any(
            authorized_start <= start and end <= authorized_end
            for authorized_start, authorized_end in authorized_spans
        ):
            raise ValueError("axis marker is outside an authorized replacement")
        axis_name = call.args[0].value
        if axis_name not in assignment:
            raise ValueError("axis marker is not declared by the structural template")
        marker_names.append(axis_name)
        replacements.append(
            _SourceReplacement(
                start=start,
                end=end,
                source=_canonical_python_literal(assignment[axis_name]),
            )
        )
    rendered = _apply_replacements(source, tuple(replacements))
    _assert_no_marker_token(path=path, source=rendered)
    return rendered, tuple(marker_names)


def _constant_name(statement: ast.stmt) -> str | None:
    if (
        isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
    ):
        return statement.targets[0].id
    if (
        isinstance(statement, ast.AnnAssign)
        and isinstance(statement.target, ast.Name)
        and statement.value is not None
    ):
        return statement.target.id
    return None


def _find_symbol_node(
    *,
    tree: ast.Module,
    symbol: str,
    kind: str,
) -> ast.stmt:
    if kind == "replace_function":
        matches = tuple(
            statement
            for statement in tree.body
            if isinstance(statement, ast.FunctionDef) and statement.name == symbol
        )
    elif kind == "replace_constant":
        matches = tuple(
            statement
            for statement in tree.body
            if _constant_name(statement) == symbol
        )
    else:
        raise ValueError("source operation kind is invalid")
    if len(matches) != 1:
        raise ValueError("source operation target must exist exactly once in its parent")
    match = matches[0]
    if isinstance(match, ast.FunctionDef) and match.decorator_list:
        raise ValueError("decorated source operation targets are not supported")
    return match


def _authorized_marker_span(
    source: str,
    *,
    node: ast.stmt,
    kind: str,
) -> tuple[int, int]:
    if kind == "replace_function" and isinstance(node, ast.FunctionDef):
        return _node_span(source, node)
    if kind == "replace_constant" and isinstance(node, (ast.Assign, ast.AnnAssign)):
        if node.value is None:
            raise ValueError("constant replacement requires a value")
        return _node_span(source, node.value)
    raise ValueError("source operation target kind is inconsistent")


def _qualified_symbol_parts(symbol: str) -> tuple[str, str, str]:
    for path in EDITABLE_POLICY_PATHS_V5:
        prefix = f"{path.removesuffix('.py').replace('/', '.')}."
        if not symbol.startswith(prefix):
            continue
        name = symbol.removeprefix(prefix)
        if "." in name:
            break
        kind = (
            "replace_function"
            if name in REQUIRED_POLICY_EXPORTS_V5[path]
            else "replace_constant"
        )
        validate_policy_symbol_edit_v5(
            path=path,
            symbol=name,
            kind="function" if kind == "replace_function" else "constant",
        )
        return path, name, kind
    raise ValueError("changed symbol is outside the V5 editable policy scope")


def _import_fingerprint(*, path: str, source: str) -> tuple[str, ...]:
    tree = _parse_source(path=path, source=source)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    imports = tuple(
        node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
    )
    if any(parents.get(node) is not tree for node in imports):
        raise ValueError("rendered policy imports must remain top-level")
    return tuple(
        ast.dump(node, annotate_fields=True, include_attributes=False)
        for node in imports
    )


def _assert_imports_unchanged(
    *,
    parent_sources: Mapping[str, str],
    rendered_sources: Mapping[str, str],
) -> None:
    for path in EDITABLE_POLICY_PATHS_V5:
        if _import_fingerprint(
            path=path,
            source=parent_sources[path],
        ) != _import_fingerprint(path=path, source=rendered_sources[path]):
            raise ValueError("rendered policy imports differ from the authenticated parent")


def _strict_literal_equal(left: LiteralValueV5, right: LiteralValueV5) -> bool:
    return type(left) is type(right) and left == right


def _assignment_key(
    assignment: VariantAssignmentV5,
) -> tuple[tuple[str, type[object], object], ...]:
    return tuple((name, type(value), value) for name, value in assignment.values)


def _assignment(
    axes: tuple[LiteralAxisV5, ...],
    values: tuple[LiteralValueV5, ...],
) -> VariantAssignmentV5:
    return VariantAssignmentV5(
        values=tuple((axis.name, value) for axis, value in zip(axes, values, strict=True))
    )


def _ordered_assignments(
    axes: tuple[LiteralAxisV5, ...],
) -> Iterator[VariantAssignmentV5]:
    defaults = tuple(axis.default for axis in axes)
    seen: set[tuple[tuple[str, type[object], object], ...]] = set()

    default_assignment = _assignment(axes, defaults)
    seen.add(_assignment_key(default_assignment))
    yield default_assignment

    for axis_index, axis in enumerate(axes):
        for value in axis.values:
            if _strict_literal_equal(value, axis.default):
                continue
            selected = list(defaults)
            selected[axis_index] = value
            assignment = _assignment(axes, tuple(selected))
            key = _assignment_key(assignment)
            if key in seen:
                continue
            seen.add(key)
            yield assignment

    remaining: list[VariantAssignmentV5] = []
    dimensions = tuple(axis.values for axis in axes)
    for values in product(*dimensions):
        assignment = _assignment(axes, values)
        key = _assignment_key(assignment)
        if key in seen:
            continue
        seen.add(key)
        remaining.append(assignment)
    yield from sorted(
        remaining,
        key=lambda item: (
            item.sha256,
            tuple(
                (name, type(value).__name__, repr(value))
                for name, value in item.values
            ),
        ),
    )


def _source_map(bundle: SourceBundleV5) -> dict[str, str]:
    return {item.path: item.source for item in bundle.files}


def _bundle_from_sources(sources: Mapping[str, str]) -> SourceBundleV5:
    if tuple(sources) != EDITABLE_POLICY_PATHS_V5:
        raise ValueError("rendered sources must preserve the exact four-file path order")
    files = tuple(SourceFileV5(path=path, source=sources[path]) for path in sources)
    return SourceBundleV5(files=files)


def _operation_root_segment(operation: SourceOperationV5) -> str:
    tree = _parse_source(path=operation.path, source=operation.replacement_source)
    node = _find_symbol_node(
        tree=tree,
        symbol=operation.symbol,
        kind=operation.kind,
    )
    start, end = _statement_line_span(
        operation.replacement_source,
        tree=tree,
        node=node,
    )
    if (
        operation.replacement_source[:start].strip()
        or operation.replacement_source[end:].strip()
    ):
        raise ValueError("source operation contains content outside its replacement")
    return operation.replacement_source[start:end]


def _render_operation_sources(
    *,
    parent_sources: Mapping[str, str],
    template: StructuralTemplateV5,
    assignment: VariantAssignmentV5,
) -> dict[str, str]:
    assignment_map = dict(assignment.values)
    by_path: dict[str, list[_SourceReplacement]] = {
        path: [] for path in EDITABLE_POLICY_PATHS_V5
    }
    marker_names: list[str] = []
    for operation in template.source_operations:
        validate_policy_symbol_edit_v5(
            path=operation.path,
            symbol=operation.symbol,
            kind=(
                "function" if operation.kind == "replace_function" else "constant"
            ),
        )
        parent_tree = _parse_source(
            path=operation.path,
            source=parent_sources[operation.path],
        )
        parent_node = _find_symbol_node(
            tree=parent_tree,
            symbol=operation.symbol,
            kind=operation.kind,
        )
        target_start, target_end = _statement_line_span(
            parent_sources[operation.path],
            tree=parent_tree,
            node=parent_node,
        )
        operation_source = _operation_root_segment(operation)
        operation_tree = _parse_source(
            path=operation.path,
            source=operation_source,
        )
        operation_node = _find_symbol_node(
            tree=operation_tree,
            symbol=operation.symbol,
            kind=operation.kind,
        )
        rendered_operation, found = _render_markers(
            path=operation.path,
            source=operation_source,
            authorized_spans=(
                _authorized_marker_span(
                    operation_source,
                    node=operation_node,
                    kind=operation.kind,
                ),
            ),
            assignment=assignment_map,
        )
        marker_names.extend(found)
        by_path[operation.path].append(
            _SourceReplacement(
                start=target_start,
                end=target_end,
                source=rendered_operation,
            )
        )

    _validate_marker_counts(template.axes, tuple(marker_names))
    rendered = {
        path: _apply_replacements(parent_sources[path], tuple(by_path[path]))
        for path in EDITABLE_POLICY_PATHS_V5
    }
    for path, source in rendered.items():
        _assert_no_marker_token(path=path, source=source)
    return rendered


def _prepare_full_source_escape(
    *,
    parent_sources: Mapping[str, str],
    template: StructuralTemplateV5,
) -> tuple[dict[str, str], dict[str, tuple[tuple[int, int], ...]]]:
    if template.full_source_escape is None:
        raise ValueError("full-source rendering requires the full-source escape")
    escape_sources = {item.path: item.source for item in template.full_source_escape}
    if tuple(escape_sources) != EDITABLE_POLICY_PATHS_V5:
        raise ValueError("full-source escape must preserve the exact four-file path order")

    parent_replacements: dict[str, list[_SourceReplacement]] = {
        path: [] for path in EDITABLE_POLICY_PATHS_V5
    }
    authorized_spans: dict[str, list[tuple[int, int]]] = {
        path: [] for path in EDITABLE_POLICY_PATHS_V5
    }
    for qualified_symbol in template.changed_symbols:
        path, symbol, kind = _qualified_symbol_parts(qualified_symbol)
        parent_tree = _parse_source(path=path, source=parent_sources[path])
        escape_tree = _parse_source(path=path, source=escape_sources[path])
        parent_node = _find_symbol_node(
            tree=parent_tree,
            symbol=symbol,
            kind=kind,
        )
        escape_node = _find_symbol_node(
            tree=escape_tree,
            symbol=symbol,
            kind=kind,
        )
        parent_start, parent_end = _statement_line_span(
            parent_sources[path],
            tree=parent_tree,
            node=parent_node,
        )
        escape_start, escape_end = _statement_line_span(
            escape_sources[path],
            tree=escape_tree,
            node=escape_node,
        )
        replacement_source = escape_sources[path][escape_start:escape_end]
        parent_replacements[path].append(
            _SourceReplacement(
                start=parent_start,
                end=parent_end,
                source=replacement_source,
            )
        )
        authorized_spans[path].append(
            _authorized_marker_span(
                escape_sources[path],
                node=escape_node,
                kind=kind,
            )
        )

    reconstructed = {
        path: _apply_replacements(
            parent_sources[path],
            tuple(parent_replacements[path]),
        )
        for path in EDITABLE_POLICY_PATHS_V5
    }
    if reconstructed != escape_sources:
        raise ValueError("full-source escape changes bytes outside declared policy symbols")
    _assert_imports_unchanged(
        parent_sources=parent_sources,
        rendered_sources=escape_sources,
    )
    return escape_sources, {
        path: tuple(spans) for path, spans in authorized_spans.items()
    }


def _render_full_source_escape(
    *,
    escape_sources: Mapping[str, str],
    authorized_spans: Mapping[str, tuple[tuple[int, int], ...]],
    template: StructuralTemplateV5,
    assignment: VariantAssignmentV5,
) -> dict[str, str]:
    assignment_map = dict(assignment.values)
    rendered: dict[str, str] = {}
    marker_names: list[str] = []
    for path in EDITABLE_POLICY_PATHS_V5:
        rendered_source, found = _render_markers(
            path=path,
            source=escape_sources[path],
            authorized_spans=authorized_spans[path],
            assignment=assignment_map,
        )
        rendered[path] = rendered_source
        marker_names.extend(found)
    _validate_marker_counts(template.axes, tuple(marker_names))
    return rendered


def _validate_marker_counts(
    axes: tuple[LiteralAxisV5, ...],
    marker_names: tuple[str, ...],
) -> None:
    expected = tuple(axis.name for axis in axes)
    counts = Counter(marker_names)
    if set(counts) != set(expected) or any(counts[name] != 1 for name in expected):
        raise ValueError("each structural template axis must have exactly one PIT_AXIS marker")


def _validate_render_inputs(
    *,
    parent: SourceBundleV5,
    parent_revision: PolicyRevisionIdentityV5,
    template: StructuralTemplateV5,
    maximum: int,
) -> None:
    if type(parent) is not SourceBundleV5:
        raise ValueError("variant parent must use the V5 source bundle schema")
    if type(parent_revision) is not PolicyRevisionIdentityV5:
        raise ValueError("variant parent revision must use the V5 identity schema")
    if type(template) is not StructuralTemplateV5:
        raise ValueError("variant template must use the V5 structural schema")
    if type(maximum) is not int or maximum <= 0:
        raise ValueError("variant maximum must be a positive integer")
    parent_hashes = tuple((item.path, item.sha256) for item in parent.files)
    if parent_revision.editable_source_sha256 != parent_hashes:
        raise ValueError("parent revision differs from the exact parent source bytes")
    if parent_revision.sha256 != template.parent_revision_sha256:
        raise ValueError("template parent differs from the authenticated parent revision")
    for item in parent.files:
        _assert_no_marker_token(path=item.path, source=item.source)


def render_variants(
    *,
    parent: SourceBundleV5,
    parent_revision: PolicyRevisionIdentityV5,
    template: StructuralTemplateV5,
    maximum: int,
) -> tuple[RenderedVariantV5, ...]:
    """Render capped, assignment-distinct variants without rewriting unrelated bytes."""

    _validate_render_inputs(
        parent=parent,
        parent_revision=parent_revision,
        template=template,
        maximum=maximum,
    )
    parent_sources = _source_map(parent)
    if template.source_operations:
        escape_sources = None
        authorized_spans = None
    else:
        escape_sources, authorized_spans = _prepare_full_source_escape(
            parent_sources=parent_sources,
            template=template,
        )

    rendered_variants: list[RenderedVariantV5] = []
    seen_bundles: dict[
        tuple[tuple[str, str], ...],
        tuple[tuple[str, type[object], object], ...],
    ] = {}
    for assignment in _ordered_assignments(template.axes):
        if template.source_operations:
            rendered_sources = _render_operation_sources(
                parent_sources=parent_sources,
                template=template,
                assignment=assignment,
            )
        else:
            if escape_sources is None or authorized_spans is None:
                raise ValueError("full-source rendering state is incomplete")
            rendered_sources = _render_full_source_escape(
                escape_sources=escape_sources,
                authorized_spans=authorized_spans,
                template=template,
                assignment=assignment,
            )
        _assert_imports_unchanged(
            parent_sources=parent_sources,
            rendered_sources=rendered_sources,
        )
        source_bundle = _bundle_from_sources(rendered_sources)
        bundle_key = tuple((item.path, item.source) for item in source_bundle.files)
        prior_assignment = seen_bundles.get(bundle_key)
        if prior_assignment is not None:
            raise VariantRenderingCollisionV5(
                "distinct strict assignments rendered identical complete source bytes"
            )
        seen_bundles[bundle_key] = _assignment_key(assignment)
        policy_revision = derive_policy_revision_identity_v5(
            source_bundle=source_bundle,
            trusted_policy_runtime_sha256=(
                parent_revision.trusted_policy_runtime_sha256
            ),
            immutable_constraints_sha256=(
                parent_revision.immutable_constraints_sha256
            ),
        )
        rendered_variants.append(
            RenderedVariantV5(
                assignment=assignment,
                source_bundle=source_bundle,
                policy_revision=policy_revision,
            )
        )
        if len(rendered_variants) == maximum:
            break
    return tuple(rendered_variants)


__all__ = ["VariantRenderingCollisionV5", "render_variants"]
