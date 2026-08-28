"""Schema-v2 contracts for the model-authored PIT optimizer."""

from __future__ import annotations

import base64
import json
import hashlib
import hmac
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, fields, replace
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_EVEN
import difflib
from itertools import product
import os
from pathlib import Path
import shutil
import sqlite3
import string
import subprocess
import threading

import pytest

import core.pit_optimization_contract as contract
import core.pit_optimizer_evaluation as evaluation
from core.pit_optimizer_evaluation import (
    AggregateMetric,
    FoldAggregateSummary,
    FoldManifest,
    FoldSpec,
)
from core.pit_policy_parity import ParityAttestation


_POLICY_PATHS = (
    "core/strategy_policy/entry.py",
    "core/strategy_policy/risk.py",
    "core/strategy_policy/exit.py",
)


def _canonical_text(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def test_v2_canonical_text_preserves_utf8_and_rejects_unsafe_controls() -> None:
    """Break caught: ASCII escaping made valid Unicode larger than the sealed bound."""
    raw_bytes = contract.MAX_ROLE_TEXT_BYTES
    backslashes = "\\" * raw_bytes
    quotes = '"' * raw_bytes
    newlines = "\n" * raw_bytes
    unicode_text = "é" * (raw_bytes // len("é".encode("utf-8")))
    astral_text = "😀" * (raw_bytes // len("😀".encode("utf-8")))
    unsafe_controls = "x\x01y"
    for value in (backslashes, quotes, newlines, unicode_text, astral_text):
        assert len(value.encode("utf-8")) == raw_bytes

    def canonical_string_bytes(value: str) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    backslash_json = canonical_string_bytes(backslashes)
    quote_json = canonical_string_bytes(quotes)
    newline_json = canonical_string_bytes(newlines)
    unicode_json = canonical_string_bytes(unicode_text)
    astral_json = canonical_string_bytes(astral_text)
    control_json = json.dumps(
        "\x01" * raw_bytes,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(backslash_json) == 2 + (2 * raw_bytes)
    assert len(quote_json) == len(backslash_json)
    assert len(newline_json) == len(backslash_json)
    assert len(unicode_json) == 2 + raw_bytes
    assert len(astral_json) == len(unicode_json)
    assert len(control_json) == 2 + (6 * raw_bytes)

    artifact = contract.InvestigatorArtifact.from_json(
        json.dumps(
            {**_investigator_payload(), "causal_rationale": unicode_text},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        max_total_bytes=contract.MAX_INVESTIGATOR_ARTIFACT_BYTES,
    )
    assert unicode_text.encode("utf-8") in artifact.canonical_json_bytes()
    assert b"\\u00e9" not in artifact.canonical_json_bytes()

    with pytest.raises(ValueError, match="control"):
        contract.InvestigatorArtifact.from_json(
            json.dumps(
                {**_investigator_payload(), "causal_rationale": unsafe_controls},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            max_total_bytes=contract.MAX_INVESTIGATOR_ARTIFACT_BYTES,
        )


def test_v2_source_and_diff_blobs_reject_unsafe_controls() -> None:
    """Break caught: control escapes could exceed every raw-byte source/diff bound."""
    author_payload = {
        **_author_payload(),
        "unified_diff": "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-x\n+y\x01\n",
    }
    with pytest.raises(ValueError, match="control"):
        contract.AuthorArtifact.from_json(
            json.dumps(
                author_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            max_total_bytes=contract.MAX_AUTHOR_NON_DIFF_ARTIFACT_BYTES
            + contract.MAX_AUTHOR_DIFF_BYTES,
            max_diff_bytes=contract.MAX_AUTHOR_DIFF_BYTES,
        )

    base = _source_bundle()
    source_texts = {
        record.path: (
            record.text + "# unsafe \x01 control\n"
            if record.path == "core/strategy_policy/entry.py"
            else record.text
        )
        for record in base.files
    }
    scope = replace(
        _source_scope(base),
        initial_policy_source_sha256s=tuple(
            (path, hashlib.sha256(text.encode("utf-8")).hexdigest())
            for path, text in source_texts.items()
        ),
    )
    with pytest.raises(ValueError, match="control"):
        contract.initial_policy_source_bundle(
            scope=scope,
            source_texts=source_texts,
        )


def _investigator_payload() -> dict[str, object]:
    return {
        "hypothesis_id": "hypothesis_1",
        "family": "entry",
        "evidence_ids": ["discovery_1.excess_return"],
        "causal_rationale": "Raise selectivity where the entry funnel loses quality.",
        "target_paths": ["core/strategy_policy/entry.py"],
        "target_symbols": ["core.strategy_policy.entry.evaluate_entry"],
        "expected_diagnostic_changes": ["fewer low-quality entries"],
        "known_risks": ["lower trade count"],
        "author_instructions": ["Change only evaluate_entry."],
    }


def test_role_schema_investigator_output_is_closed_and_bounded() -> None:
    """Break caught: investigator output could widen or overflow the author envelope."""
    payload = _investigator_payload()

    artifact = contract.InvestigatorArtifact.from_json(
        _canonical_text(payload),
        max_total_bytes=contract.MAX_INVESTIGATOR_ARTIFACT_BYTES,
    )

    assert artifact.hypothesis_id == "hypothesis_1"
    assert artifact.family == "entry"
    schema = contract.pit_optimizer_response_format("investigator")
    assert schema["json_schema"]["name"] == "pit_optimizer_investigator_v2"
    body = schema["json_schema"]["schema"]
    assert body["additionalProperties"] is False
    assert set(body["required"]) == set(payload)
    assert body["properties"]["family"]["enum"] == ["entry", "exit", "risk_sizing"]

    with pytest.raises(ValueError, match="invalid keys"):
        contract.InvestigatorArtifact.from_json(
            _canonical_text({**payload, "hidden_fold": "hidden_1"}),
            max_total_bytes=contract.MAX_INVESTIGATOR_ARTIFACT_BYTES,
        )
    duplicate = _canonical_text(payload)[:-1] + ',"hypothesis_id":"hypothesis_2"}'
    with pytest.raises(ValueError, match="duplicate"):
        contract.InvestigatorArtifact.from_json(
            duplicate,
            max_total_bytes=contract.MAX_INVESTIGATOR_ARTIFACT_BYTES,
        )
    with pytest.raises(ValueError, match="family"):
        contract.InvestigatorArtifact.from_json(
            _canonical_text({**payload, "family": "hidden_validation"}),
            max_total_bytes=contract.MAX_INVESTIGATOR_ARTIFACT_BYTES,
        )
    with pytest.raises(ValueError, match="at most 16"):
        contract.InvestigatorArtifact.from_json(
            _canonical_text(
                {
                    **payload,
                    "evidence_ids": [f"evidence_{index}" for index in range(17)],
                }
            ),
            max_total_bytes=contract.MAX_INVESTIGATOR_ARTIFACT_BYTES,
        )
    with pytest.raises(ValueError, match="unique"):
        contract.InvestigatorArtifact.from_json(
            _canonical_text({**payload, "known_risks": ["same", "same"]}),
            max_total_bytes=contract.MAX_INVESTIGATOR_ARTIFACT_BYTES,
        )
    with pytest.raises(ValueError, match="bounded"):
        contract.InvestigatorArtifact.from_json(
            _canonical_text(payload),
            max_total_bytes=32,
        )


def _author_payload() -> dict[str, object]:
    return {
        "hypothesis_id": "hypothesis_1",
        "behavioral_summary": "Require a stronger entry confirmation.",
        "changed_paths": ["core/strategy_policy/entry.py"],
        "changed_symbols": ["core.strategy_policy.entry.evaluate_entry"],
        "unified_diff": (
            "--- a/core/strategy_policy/entry.py\n"
            "+++ b/core/strategy_policy/entry.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def evaluate_entry(snapshot):\n"
            "-    return None\n"
            "+    return True\n"
        ),
        "assumptions": ["The aggregate funnel is causal."],
        "validation_suggestions": ["Run the focused entry checks."],
    }


def test_role_schema_author_output_has_independent_diff_and_metadata_caps() -> None:
    """Break caught: an author response could hide oversized metadata beside a bounded diff."""
    payload = _author_payload()

    artifact = contract.AuthorArtifact.from_json(
        _canonical_text(payload),
        max_diff_bytes=8 * 1024,
        max_total_bytes=16 * 1024,
    )

    assert artifact.changed_paths == ("core/strategy_policy/entry.py",)
    assert artifact.changed_symbols == (
        "core.strategy_policy.entry.evaluate_entry",
    )
    schema = contract.pit_optimizer_response_format("author")["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(payload)

    with pytest.raises(ValueError, match="diff"):
        contract.AuthorArtifact.from_json(
            _canonical_text({**payload, "unified_diff": "x" * 33}),
            max_diff_bytes=32,
            max_total_bytes=16 * 1024,
        )
    with pytest.raises(ValueError, match="non-diff"):
        contract.AuthorArtifact.from_json(
            _canonical_text(
                {
                    **payload,
                    "behavioral_summary": "s" * 4096,
                    "assumptions": ["a" * 4096],
                }
            ),
            max_diff_bytes=8 * 1024,
            max_total_bytes=16 * 1024,
        )
    with pytest.raises(ValueError, match="invalid keys"):
        contract.AuthorArtifact.from_json(
            _canonical_text({**payload, "provider_audit_body": "forbidden"}),
            max_diff_bytes=8 * 1024,
            max_total_bytes=16 * 1024,
        )


def _critic_payload() -> dict[str, object]:
    return {
        "hypothesis_id": "hypothesis_1",
        "prediction_vs_observation": "Trade quality rose, but participation fell too far.",
        "causal_explanation": "The confirmation excluded both weak and valid entries.",
        "evidence_ids": ["candidate.discovery_1.entry_funnel"],
        "disposition": "refine",
        "next_direction": "Retain confirmation only for extended entries.",
    }


def test_role_schema_critic_output_is_advisory_closed_and_bounded() -> None:
    """Break caught: the critic could claim acceptance authority or expose hidden results."""
    payload = _critic_payload()

    artifact = contract.CriticArtifact.from_json(
        _canonical_text(payload),
        max_total_bytes=contract.MAX_CRITIC_ARTIFACT_BYTES,
    )

    assert artifact.disposition == "refine"
    schema = contract.pit_optimizer_response_format("critic")["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(payload)
    assert schema["properties"]["disposition"]["enum"] == [
        "refine",
        "abandon",
        "change_family",
    ]

    with pytest.raises(ValueError, match="disposition"):
        contract.CriticArtifact.from_json(
            _canonical_text({**payload, "disposition": "accept"}),
            max_total_bytes=contract.MAX_CRITIC_ARTIFACT_BYTES,
        )
    with pytest.raises(ValueError, match="invalid keys"):
        contract.CriticArtifact.from_json(
            _canonical_text({**payload, "hidden_metrics": {"return": 99}}),
            max_total_bytes=contract.MAX_CRITIC_ARTIFACT_BYTES,
        )
    with pytest.raises(ValueError, match="bounded"):
        contract.CriticArtifact.from_json(
            _canonical_text(payload),
            max_total_bytes=64,
        )


def _source_bundle() -> contract.PolicySourceBundle:
    sources = (
        (
            "core/strategy_policy/entry.py",
            "core.strategy_policy.entry.evaluate_entry",
            "def evaluate_entry(snapshot):\n    return None\n",
        ),
        (
            "core/strategy_policy/risk.py",
            (
                "core.strategy_policy.risk.recommend_capacity",
                "core.strategy_policy.risk.recommend_allocation",
                "core.strategy_policy.risk.select_eviction",
            ),
            "def recommend_capacity(snapshot):\n    return 1\n",
        ),
        (
            "core/strategy_policy/exit.py",
            "core.strategy_policy.exit.evaluate_exit",
            "def evaluate_exit(snapshot):\n    return None\n",
        ),
    )
    source_texts = {path: text for path, _symbol, text in sources}
    source_sha256s = tuple(
        (path, hashlib.sha256(text.encode("utf-8")).hexdigest())
        for path, text in source_texts.items()
    )
    scope = contract.PolicySourceScope(
        schema_version=2,
        policy_interface_version=1,
        initial_policy_source_sha256s=source_sha256s,
        editable_paths=tuple(source_texts),
        max_policy_source_bundle_bytes=64 * 1024,
        max_iteration_feedback_bytes=4 * 1024,
        max_iteration_history_bytes=32 * 1024,
        hard_patch_bounds=contract.PatchBounds(3, 12, 200, 64 * 1024),
        candidate_bounds=contract.PatchBounds(3, 12, 80, 8 * 1024),
        max_iterations=2,
        allowed_descendant_rule=(
            "authenticated_initial_sources_plus_validated_cumulative_diff"
        ),
    )
    return contract.initial_policy_source_bundle(
        scope=scope,
        source_texts=source_texts,
    )


def _source_scope(
    bundle: contract.PolicySourceBundle,
    *,
    bounds: contract.PatchBounds | None = None,
    bundle_cap: int = 64 * 1024,
) -> contract.PolicySourceScope:
    candidate_bounds = bounds or contract.PatchBounds(3, 12, 80, 8 * 1024)
    return contract.PolicySourceScope(
        schema_version=2,
        policy_interface_version=bundle.policy_interface_version,
        initial_policy_source_sha256s=tuple(
            (record.path, record.sha256) for record in bundle.files
        ),
        editable_paths=tuple(record.path for record in bundle.files),
        max_policy_source_bundle_bytes=bundle_cap,
        max_iteration_feedback_bytes=4 * 1024,
        max_iteration_history_bytes=32 * 1024,
        hard_patch_bounds=contract.PatchBounds(3, 12, 200, 64 * 1024),
        candidate_bounds=candidate_bounds,
        max_iterations=2,
        allowed_descendant_rule=(
            "authenticated_initial_sources_plus_validated_cumulative_diff"
        ),
    )


def _diff_for_changes(
    bundle: contract.PolicySourceBundle,
    changes: dict[str, str],
    *,
    context: int = 3,
) -> str:
    by_path = {record.path: record.text for record in bundle.files}
    return "".join(
        line
        for path in tuple(record.path for record in bundle.files)
        if path in changes
        for line in difflib.unified_diff(
            by_path[path].splitlines(keepends=True),
            changes[path].splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=context,
            lineterm="\n",
        )
    )


def _artifact_for_diff(diff: str, paths: tuple[str, ...]) -> contract.AuthorArtifact:
    symbols = tuple(
        {
            "core/strategy_policy/entry.py": "core.strategy_policy.entry.evaluate_entry",
            "core/strategy_policy/risk.py": "core.strategy_policy.risk.recommend_capacity",
            "core/strategy_policy/exit.py": "core.strategy_policy.exit.evaluate_exit",
        }[path]
        for path in paths
    )
    return contract.AuthorArtifact.from_json(
        _canonical_text(
            {
                **_author_payload(),
                "changed_paths": list(paths),
                "changed_symbols": list(symbols),
                "unified_diff": diff,
            }
        ),
        max_diff_bytes=64 * 1024,
        max_total_bytes=72 * 1024,
    )


def test_controller_materializes_only_bounds_valid_source_descendants() -> None:
    """Break caught: arbitrary source text or an over-bound patch could reach a role."""
    initial = _source_bundle()
    scope = _source_scope(initial)
    initial = contract.initial_policy_source_bundle(
        scope=scope,
        source_texts={record.path: record.text for record in initial.files},
    )
    entry_path = "core/strategy_policy/entry.py"
    risk_path = "core/strategy_policy/risk.py"
    entry_text = initial.files[0].text.replace("return None", "return True")
    valid_diff = _diff_for_changes(initial, {entry_path: entry_text})
    valid = contract.materialize_policy_source_descendant(
        scope=scope,
        initial_bundle=initial,
        current_bundle=initial,
        artifact=_artifact_for_diff(valid_diff, (entry_path,)),
        immutable_constraint_ids=("causal_only", "no_external_io"),
        call_budgets=_call_budgets(),
    )
    assert valid.validation.failure_code is None
    assert valid.bundle is not None
    contract.validate_policy_source_bundle_descendant(
        scope=scope,
        initial_bundle=initial,
        bundle=valid.bundle,
    )

    arbitrary_record = replace(
        initial.files[0],
        text="arbitrary provider source\n",
        sha256=hashlib.sha256(b"arbitrary provider source\n").hexdigest(),
    )
    with pytest.raises(ValueError, match="controller derived"):
        replace(
            valid.bundle,
            files=(arbitrary_record, *initial.files[1:]),
        )

    risk_text = initial.files[1].text.replace("return 1", "return 2")
    two_file_diff = _diff_for_changes(
        initial,
        {entry_path: entry_text, risk_path: risk_text},
    )
    with pytest.raises(ValueError, match="max_files"):
        contract.materialize_policy_source_descendant(
            scope=replace(
                scope,
                candidate_bounds=contract.PatchBounds(1, 12, 80, 8 * 1024),
            ),
            initial_bundle=initial,
            current_bundle=initial,
            artifact=_artifact_for_diff(two_file_diff, (entry_path, risk_path)),
            immutable_constraint_ids=("causal_only", "no_external_io"),
            call_budgets=_call_budgets(),
        )

    five_lines = "a\nb\nc\nd\ne\n"
    expanded_initial_texts = {
        record.path: (five_lines if record.path == entry_path else record.text)
        for record in initial.files
    }
    expanded_hashes = tuple(
        (path, hashlib.sha256(text.encode("utf-8")).hexdigest())
        for path, text in expanded_initial_texts.items()
    )
    expanded_scope = replace(scope, initial_policy_source_sha256s=expanded_hashes)
    expanded_initial = contract.initial_policy_source_bundle(
        scope=expanded_scope,
        source_texts=expanded_initial_texts,
    )
    two_hunk_text = "A\nb\nc\nd\nE\n"
    two_hunk_diff = _diff_for_changes(
        expanded_initial,
        {entry_path: two_hunk_text},
        context=0,
    )
    with pytest.raises(ValueError, match="max_hunks"):
        contract.materialize_policy_source_descendant(
            scope=replace(
                expanded_scope,
                candidate_bounds=contract.PatchBounds(3, 1, 80, 8 * 1024),
            ),
            initial_bundle=expanded_initial,
            current_bundle=expanded_initial,
            artifact=_artifact_for_diff(two_hunk_diff, (entry_path,)),
            immutable_constraint_ids=("causal_only", "no_external_io"),
            call_budgets=_call_budgets(),
        )
    with pytest.raises(ValueError, match="max_changed_lines"):
        contract.materialize_policy_source_descendant(
            scope=replace(
                expanded_scope,
                candidate_bounds=contract.PatchBounds(3, 12, 3, 8 * 1024),
            ),
            initial_bundle=expanded_initial,
            current_bundle=expanded_initial,
            artifact=_artifact_for_diff(two_hunk_diff, (entry_path,)),
            immutable_constraint_ids=("causal_only", "no_external_io"),
            call_budgets=_call_budgets(),
        )
    with pytest.raises(ValueError, match="max_diff_bytes"):
        contract.materialize_policy_source_descendant(
            scope=replace(
                expanded_scope,
                candidate_bounds=contract.PatchBounds(
                    3,
                    12,
                    80,
                    len(two_hunk_diff.encode("utf-8")) - 1,
                ),
            ),
            initial_bundle=expanded_initial,
            current_bundle=expanded_initial,
            artifact=_artifact_for_diff(two_hunk_diff, (entry_path,)),
            immutable_constraint_ids=("causal_only", "no_external_io"),
            call_budgets=_call_budgets(),
        )

    overflow_scope = replace(
        scope,
        max_policy_source_bundle_bytes=len(initial.canonical_json_bytes()) + 32,
    )
    overflow_text = initial.files[0].text.replace("return None", "return " + "1" * 256)
    overflow_diff = _diff_for_changes(initial, {entry_path: overflow_text})
    overflow = contract.materialize_policy_source_descendant(
        scope=overflow_scope,
        initial_bundle=initial,
        current_bundle=initial,
        artifact=_artifact_for_diff(overflow_diff, (entry_path,)),
        immutable_constraint_ids=("causal_only", "no_external_io"),
        call_budgets=_call_budgets(),
    )
    assert overflow.bundle is None
    assert overflow.validation.failure_code == "next_context_oversize"


def test_controller_applies_middle_insertion_only_hunk_at_declared_position() -> None:
    """Break caught: a zero-old-count hunk was inserted one source line too early."""
    initial = _source_bundle()
    scope = _source_scope(initial)
    entry_path = "core/strategy_policy/entry.py"
    insertion = (
        f"--- a/{entry_path}\n"
        f"+++ b/{entry_path}\n"
        "@@ -1,0 +2,1 @@\n"
        "+    marker = True\n"
    )

    materialized = contract.materialize_policy_source_descendant(
        scope=scope,
        initial_bundle=initial,
        current_bundle=initial,
        artifact=_artifact_for_diff(insertion, (entry_path,)),
        immutable_constraint_ids=("causal_only", "no_external_io"),
        call_budgets=_call_budgets(),
    )

    assert materialized.bundle is not None
    assert materialized.bundle.files[0].text == (
        "def evaluate_entry(snapshot):\n"
        "    marker = True\n"
        "    return None\n"
    )


@pytest.mark.parametrize(
    ("hunk", "expected"),
    (
        (
            "@@ -0,0 +1,1 @@\n+# first\n",
            "# first\ndef evaluate_entry(snapshot):\n    return None\n",
        ),
        (
            "@@ -2,0 +3,1 @@\n+# last\n",
            "def evaluate_entry(snapshot):\n    return None\n# last\n",
        ),
        (
            "@@ -2,1 +2,1 @@\n-    return None\n+    return True\n",
            "def evaluate_entry(snapshot):\n    return True\n",
        ),
        (
            "@@ -2,1 +1,0 @@\n-    return None\n",
            "def evaluate_entry(snapshot):\n",
        ),
    ),
)
def test_controller_preserves_boundary_insertion_replacement_and_deletion_semantics(
    hunk: str,
    expected: str,
) -> None:
    initial = _source_bundle()
    scope = _source_scope(initial)
    entry_path = "core/strategy_policy/entry.py"
    patch = f"--- a/{entry_path}\n+++ b/{entry_path}\n{hunk}"

    materialized = contract.materialize_policy_source_descendant(
        scope=scope,
        initial_bundle=initial,
        current_bundle=initial,
        artifact=_artifact_for_diff(patch, (entry_path,)),
        immutable_constraint_ids=("causal_only", "no_external_io"),
        call_budgets=_call_budgets(),
    )

    assert materialized.bundle is not None
    assert materialized.bundle.files[0].text == expected


def test_controller_rejects_inconsistent_new_range_location() -> None:
    initial = _source_bundle()
    scope = _source_scope(initial)
    entry_path = "core/strategy_policy/entry.py"
    inconsistent = (
        f"--- a/{entry_path}\n"
        f"+++ b/{entry_path}\n"
        "@@ -1,0 +1,1 @@\n"
        "+    marker = True\n"
    )

    with pytest.raises(ValueError, match="new hunk location"):
        contract.materialize_policy_source_descendant(
            scope=scope,
            initial_bundle=initial,
            current_bundle=initial,
            artifact=_artifact_for_diff(inconsistent, (entry_path,)),
            immutable_constraint_ids=("causal_only", "no_external_io"),
            call_budgets=_call_budgets(),
        )


@pytest.mark.parametrize(
    ("padding_kind", "raw_padding_bytes", "expected_failure_code"),
    (
        ("ascii", 10_000, None),
        ("ascii", 45_000, "next_context_oversize"),
        ("backslash", 30_000, "next_context_oversize"),
        ("unicode", 30_000, None),
    ),
)
def test_candidate_materialization_checks_exact_next_role_envelope(
    padding_kind: str,
    raw_padding_bytes: int,
    expected_failure_code: str | None,
) -> None:
    """Break caught: a source-cap-valid incumbent could overflow the next role input."""
    base = _source_bundle()
    entry_path = "core/strategy_policy/entry.py"
    padding_unit = {"ascii": "p", "backslash": "\\", "unicode": "é"}[
        padding_kind
    ]
    padding = padding_unit * (
        raw_padding_bytes // len(padding_unit.encode("utf-8"))
    )
    assert len(padding.encode("utf-8")) == raw_padding_bytes
    padded_texts = {
        record.path: (
            "# "
            + padding
            + "\n# spacer_1\n# spacer_2\n# spacer_3\n# spacer_4\n"
            + record.text
            if record.path == entry_path
            else record.text
        )
        for record in base.files
    }
    padded_hashes = tuple(
        (path, hashlib.sha256(text.encode("utf-8")).hexdigest())
        for path, text in padded_texts.items()
    )
    scope = replace(
        _source_scope(base),
        initial_policy_source_sha256s=padded_hashes,
    )
    initial = contract.initial_policy_source_bundle(
        scope=scope,
        source_texts=padded_texts,
    )
    changed = padded_texts[entry_path].replace("return None", "return True")
    patch = _diff_for_changes(initial, {entry_path: changed}, context=0)
    independently_derived_cumulative = _diff_for_changes(
        initial,
        {entry_path: changed},
    )
    candidate_records = [
        {
            "path": record.path,
            "sha256": hashlib.sha256(
                (changed if record.path == entry_path else record.text).encode("utf-8")
            ).hexdigest(),
            "declared_symbols": list(record.declared_symbols),
            "text": changed if record.path == entry_path else record.text,
        }
        for record in initial.files
    ]
    candidate_bundle = {
        "policy_interface_version": initial.policy_interface_version,
        "cumulative_diff_sha256": hashlib.sha256(
            independently_derived_cumulative.encode("utf-8")
        ).hexdigest(),
        "cumulative_diff": independently_derived_cumulative,
        "files": candidate_records,
    }
    assert (
        len(
            json.dumps(
                candidate_bundle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
        <= scope.max_policy_source_bundle_bytes
    )

    result = contract.materialize_policy_source_descendant(
        scope=scope,
        initial_bundle=initial,
        current_bundle=initial,
        artifact=_artifact_for_diff(patch, (entry_path,)),
        immutable_constraint_ids=("causal_only", "no_external_io"),
        call_budgets=_call_budgets(),
    )

    assert result.validation.failure_code == expected_failure_code
    assert (result.bundle is None) == (expected_failure_code is not None)


def _fold_summary(fold_id: str, excess: float) -> FoldAggregateSummary:
    return FoldAggregateSummary(
        fold_id=fold_id,
        total_return_pct=excess + 1.0,
        excess_total_return_pp=excess,
        max_drawdown_pct=-2.0,
        sharpe_ratio=1.0,
        closed_trades=2,
        turnover_pct=10.0,
        average_exposure_pct=20.0,
        entry_funnel=(AggregateMetric("entries_executed", 2),),
        exit_attribution=(AggregateMetric("end_of_test", 2),),
    )


def _aggregate_sha256(folds: tuple[FoldAggregateSummary, ...]) -> str:
    return hashlib.sha256(
        (_canonical_text([asdict(item) for item in folds]) + "\n").encode("utf-8")
    ).hexdigest()


def _discovery_summary() -> contract.DiscoveryEvidenceSummary:
    return contract.DiscoveryEvidenceSummary(
        folds=(
            _fold_summary("discovery_1", 0.5),
            _fold_summary("discovery_2", 0.25),
        ),
        score=None,
        evidence_ids=("evidence.discovery_1", "evidence.discovery_2"),
    )


def _discovery_exposure_proof(tmp_path: Path) -> evaluation.DiscoveryExposureProof:
    ledger = evaluation.ValidationLedger(
        tmp_path / "pit_optimizer_validation_ledger.jsonl"
    )
    manifest = _fold_manifest()
    metadata = evaluation.ValidationExposureMetadata(
        run_id="run_1",
        source_head="1" * 40,
        baseline_policy_sha256="d" * 64,
        candidate_identity_sha256=None,
        exposure_kind="provider_context",
    )
    reservations = tuple(
        ledger.mark_discovery(_validation_identity(fold, fold.fold_id), metadata)
        for fold in manifest.discovery_folds
    )
    return ledger.seal_discovery_folds(manifest, reservations)


def test_discovery_exposure_proof_retains_complete_identity_and_lineage(
    tmp_path: Path,
) -> None:
    """Break caught: proof output dropped warmup identity and release lineage."""
    ledger = evaluation.ValidationLedger(
        tmp_path / "pit_optimizer_validation_ledger.jsonl"
    )
    manifest = _fold_manifest()
    identities = tuple(
        _validation_identity(fold, fold.fold_id)
        for fold in manifest.discovery_folds
    )
    metadata = evaluation.ValidationExposureMetadata(
        run_id="run_1",
        source_head="1" * 40,
        baseline_policy_sha256="d" * 64,
        candidate_identity_sha256="f" * 64,
        exposure_kind="provider_context",
    )
    reservations = tuple(
        ledger.mark_discovery(identity, metadata) for identity in identities
    )

    proof = ledger.seal_discovery_folds(manifest, reservations)

    assert getattr(proof, "window_identities", None) == identities
    assert getattr(proof, "metadata", None) == metadata


def test_discovery_exposure_proof_rejects_warmup_identity_discontinuity(
    tmp_path: Path,
) -> None:
    ledger = evaluation.ValidationLedger(
        tmp_path / "pit_optimizer_validation_ledger.jsonl"
    )
    manifest = _fold_manifest()
    identities = tuple(
        _validation_identity(fold, fold.fold_id)
        for fold in manifest.discovery_folds
    )
    metadata = evaluation.ValidationExposureMetadata(
        run_id="run_1",
        source_head="1" * 40,
        baseline_policy_sha256="d" * 64,
        candidate_identity_sha256="f" * 64,
        exposure_kind="provider_context",
    )
    reservations = (
        ledger.mark_discovery(identities[0], metadata),
        ledger.mark_discovery(
            replace(identities[1], warmup_contract_sha256="9" * 64),
            metadata,
        ),
    )

    with pytest.raises(ValueError, match="window lineage"):
        ledger.seal_discovery_folds(manifest, reservations)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("run_id", "run_2"),
        ("source_head", "2" * 40),
        ("baseline_policy_sha256", "e" * 64),
        ("candidate_identity_sha256", "e" * 64),
        ("exposure_kind", "candidate_validation"),
    ),
)
def test_discovery_exposure_proof_rejects_metadata_lineage_discontinuity(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    ledger = evaluation.ValidationLedger(
        tmp_path / "pit_optimizer_validation_ledger.jsonl"
    )
    manifest = _fold_manifest()
    identities = tuple(
        _validation_identity(fold, fold.fold_id)
        for fold in manifest.discovery_folds
    )
    metadata = evaluation.ValidationExposureMetadata(
        run_id="run_1",
        source_head="1" * 40,
        baseline_policy_sha256="d" * 64,
        candidate_identity_sha256="f" * 64,
        exposure_kind="provider_context",
    )
    reservations = (
        ledger.mark_discovery(identities[0], metadata),
        ledger.mark_discovery(identities[1], replace(metadata, **{field: value})),
    )

    with pytest.raises(ValueError, match="metadata lineage"):
        ledger.seal_discovery_folds(manifest, reservations)


def test_candidate_comparison_requires_expected_window_and_metadata_lineage(
    tmp_path: Path,
) -> None:
    proof = _discovery_exposure_proof(tmp_path)
    candidate = (
        _fold_summary("discovery_1", 0.5),
        _fold_summary("discovery_2", 0.25),
    )
    baseline = (
        _fold_summary("discovery_1", 0.0),
        _fold_summary("discovery_2", 0.0),
    )
    baseline_sha256 = _aggregate_sha256(baseline)

    with pytest.raises(ValueError, match="expected metadata lineage"):
        contract.candidate_comparison_from_fixed_baseline(
            candidate_folds=candidate,
            original_baseline_folds=baseline,
            original_baseline_sha256=baseline_sha256,
            expected_original_baseline_sha256=baseline_sha256,
            discovery_exposure=proof,
            expected_window_identities=proof.window_identities,
            expected_metadata=replace(proof.metadata, run_id="run_other"),
            diagnostics=(),
        )
    with pytest.raises(ValueError, match="expected window identities"):
        contract.candidate_comparison_from_fixed_baseline(
            candidate_folds=candidate,
            original_baseline_folds=baseline,
            original_baseline_sha256=baseline_sha256,
            expected_original_baseline_sha256=baseline_sha256,
            discovery_exposure=proof,
            expected_window_identities=tuple(
                replace(identity, warmup_contract_sha256="9" * 64)
                for identity in proof.window_identities
            ),
            expected_metadata=proof.metadata,
            diagnostics=(),
        )


def test_candidate_comparison_structurally_rejects_hidden_fold_identity(
    tmp_path: Path,
) -> None:
    """Break caught: a caller could serialize hidden validation evidence for a role."""
    baseline = (
        _fold_summary("discovery_1", 0.0),
        _fold_summary("discovery_2", 0.0),
    )
    baseline_sha256 = _aggregate_sha256(baseline)
    proof = _discovery_exposure_proof(tmp_path)
    with pytest.raises(ValueError, match="ledger exposure"):
        contract.candidate_comparison_from_fixed_baseline(
            candidate_folds=(
                _fold_summary("discovery_1", 0.5),
                _fold_summary("hidden_1", 99.0),
            ),
            original_baseline_folds=baseline,
            original_baseline_sha256=baseline_sha256,
            expected_original_baseline_sha256=baseline_sha256,
            discovery_exposure=proof,
            expected_window_identities=proof.window_identities,
            expected_metadata=proof.metadata,
            diagnostics=(),
        )


def test_candidate_validation_codes_are_closed_and_match_stage_flags() -> None:
    """Break caught: arbitrary or contradictory failure labels could reach a role."""
    with pytest.raises(ValueError, match="failure code is not closed"):
        contract.CandidateValidationSummary(
            failure_code="invented_failure",
            syntax_ok=False,
            imports_ok=False,
            purity_ok=False,
            deterministic_ok=False,
            worker_ok=False,
            replay_attempted=False,
        )
    with pytest.raises(ValueError, match="syntax_failed flags"):
        contract.CandidateValidationSummary(
            failure_code="syntax_failed",
            syntax_ok=True,
            imports_ok=False,
            purity_ok=False,
            deterministic_ok=False,
            worker_ok=False,
            replay_attempted=False,
        )
    with pytest.raises(ValueError, match="successful validation flags"):
        contract.CandidateValidationSummary(
            failure_code=None,
            syntax_ok=True,
            imports_ok=True,
            purity_ok=True,
            deterministic_ok=True,
            worker_ok=True,
            replay_attempted=False,
        )


def _investigator_artifact() -> contract.InvestigatorArtifact:
    return contract.InvestigatorArtifact.from_json(
        _canonical_text(_investigator_payload()),
        max_total_bytes=contract.MAX_INVESTIGATOR_ARTIFACT_BYTES,
    )


def test_role_schema_inputs_are_exact_bounded_provider_projections(
    tmp_path: Path,
) -> None:
    """Break caught: a role input could admit hidden identity or unbounded prior context."""
    bounds = contract.PatchBounds(3, 12, 80, 8 * 1024)
    discovery = _discovery_summary()
    investigator = _investigator_artifact()
    source_bundle = _source_bundle()
    rule_summary = contract.StrategyRuleSummary(
        records=(contract.RuleSummaryRecord("rule.entry", "Use causal entry inputs."),)
    )
    incumbent = contract.IncumbentSummary(
        candidate_identity_sha256=None,
        accepted_iteration=None,
        behavioral_summary="Authenticated fixed baseline.",
        discovery=discovery,
    )
    feedback = contract.IterationFeedbackSummary(
        iteration=1,
        hypothesis_id="hypothesis_0",
        family="risk_sizing",
        author_summary="Reduced concentration.",
        validation_code="valid",
        discovery_score=None,
        critic_disposition="refine",
        critic_next_direction="Use a smaller adjustment.",
        incumbent_changed=False,
    )
    investigator_input = contract.InvestigatorInput(
        schema_version=2,
        iteration=2,
        policy_interface_version=1,
        immutable_constraint_ids=("causal_only", "no_external_io"),
        candidate_bounds=bounds,
        rule_summary=rule_summary,
        source_bundle=source_bundle,
        baseline_discovery=discovery,
        incumbent_summary=incumbent,
        prior_iterations=(feedback,),
    )
    author_input = contract.AuthorInput(
        schema_version=2,
        iteration=2,
        policy_interface_version=1,
        immutable_constraint_ids=("causal_only", "no_external_io"),
        candidate_bounds=bounds,
        investigator=investigator,
        source_bundle=source_bundle,
    )
    author_artifact = contract.AuthorArtifact.from_json(
        _canonical_text(_author_payload()),
        max_diff_bytes=8 * 1024,
        max_total_bytes=16 * 1024,
    )
    author_manifest = contract.AuthorManifestSummary(
        hypothesis_id="hypothesis_1",
        behavioral_summary=author_artifact.behavioral_summary,
        changed_paths=author_artifact.changed_paths,
        changed_symbols=author_artifact.changed_symbols,
    )
    original_baseline = (
        _fold_summary("discovery_1", 0.0),
        _fold_summary("discovery_2", 0.0),
    )
    baseline_sha256 = _aggregate_sha256(original_baseline)
    proof = _discovery_exposure_proof(tmp_path)
    comparison = contract.candidate_comparison_from_fixed_baseline(
        candidate_folds=discovery.folds,
        original_baseline_folds=original_baseline,
        original_baseline_sha256=baseline_sha256,
        expected_original_baseline_sha256=baseline_sha256,
        discovery_exposure=proof,
        expected_window_identities=proof.window_identities,
        expected_metadata=proof.metadata,
        diagnostics=(AggregateMetric("entry_quality_delta", 0.2),),
    )
    critic_input = contract.CriticInput(
        schema_version=2,
        iteration=2,
        immutable_constraint_ids=("causal_only", "no_external_io"),
        hypothesis_id="hypothesis_1",
        investigator_summary=investigator,
        author_manifest=author_manifest,
        validation=contract.CandidateValidationSummary(
            failure_code=None,
            syntax_ok=True,
            imports_ok=True,
            purity_ok=True,
            deterministic_ok=True,
            worker_ok=True,
            replay_attempted=True,
        ),
        candidate_vs_baseline=comparison,
        candidate_vs_incumbent=comparison,
    )

    assert tuple(field.name for field in fields(contract.InvestigatorInput)) == (
        "schema_version",
        "iteration",
        "policy_interface_version",
        "immutable_constraint_ids",
        "candidate_bounds",
        "rule_summary",
        "source_bundle",
        "baseline_discovery",
        "incumbent_summary",
        "prior_iterations",
    )
    assert tuple(field.name for field in fields(contract.AuthorInput)) == (
        "schema_version",
        "iteration",
        "policy_interface_version",
        "immutable_constraint_ids",
        "candidate_bounds",
        "investigator",
        "source_bundle",
    )
    assert tuple(field.name for field in fields(contract.CriticInput)) == (
        "schema_version",
        "iteration",
        "immutable_constraint_ids",
        "hypothesis_id",
        "investigator_summary",
        "author_manifest",
        "validation",
        "candidate_vs_baseline",
        "candidate_vs_incumbent",
    )
    for role_input in (investigator_input, author_input, critic_input):
        rendered = role_input.canonical_json_bytes().decode("utf-8")
        for forbidden in (
            "hidden_fold",
            "2021-12-15",
            "validation_ledger",
            "provider_audit_body",
            "credential",
            "C:\\\\",
        ):
            assert forbidden not in rendered

    author_input.validate_artifact(author_artifact)
    with pytest.raises(ValueError, match="hypothesis"):
        author_input.validate_artifact(replace(author_artifact, hypothesis_id="other"))
    with pytest.raises(ValueError, match="hypothesis"):
        replace(critic_input, hypothesis_id="other")
    with pytest.raises(ValueError, match="source SHA-256"):
        replace(source_bundle.files[0], sha256="0" * 64)
    with pytest.raises(ValueError, match="declared symbols"):
        replace(source_bundle.files[0], declared_symbols=("unrelated.symbol",))
    with pytest.raises(ValueError, match="target paths"):
        replace(investigator, target_paths=("C:\\private\\policy.py",))
    with pytest.raises(ValueError, match="changed paths"):
        replace(author_artifact, changed_paths=("core/backtest_engine.py",))
    with pytest.raises(ValueError, match="at most 8"):
        replace(investigator_input, prior_iterations=(feedback,) * 9)
    with pytest.raises(ValueError, match="too large"):
        replace(investigator, causal_rationale="x" * (4 * 1024 + 1))
    with pytest.raises(ValueError, match="too large"):
        replace(author_artifact, behavioral_summary="x" * (4 * 1024 + 1))
    with pytest.raises(ValueError, match="too large"):
        replace(
            contract.CriticArtifact.from_json(
                _canonical_text(_critic_payload()),
                max_total_bytes=contract.MAX_CRITIC_ARTIFACT_BYTES,
            ),
            next_direction="x" * (4 * 1024 + 1),
        )


def _sessions(start: str, end: str) -> tuple[str, ...]:
    first = date.fromisoformat(start)
    return tuple(
        [
            (first + timedelta(days=offset)).isoformat()
            for offset in range(59)
        ]
        + [end]
    )


def _fold_manifest() -> FoldManifest:
    discovery_1 = FoldSpec(
        fold_id="discovery_1",
        purpose="discovery",
        start_date="2021-06-25",
        end_date="2021-09-20",
        sessions=_sessions("2021-06-25", "2021-09-20"),
    )
    discovery_2 = FoldSpec(
        fold_id="discovery_2",
        purpose="discovery",
        start_date="2021-09-21",
        end_date="2021-12-14",
        sessions=_sessions("2021-09-21", "2021-12-14"),
    )
    hidden = FoldSpec(
        fold_id="hidden_1",
        purpose="hidden",
        start_date="2021-12-15",
        end_date="2022-03-11",
        sessions=_sessions("2021-12-15", "2022-03-11"),
    )
    return FoldManifest(
        data_identity_sha256="a" * 64,
        universe_sha256="b" * 64,
        benchmark="SPY",
        warmup_start_date="2021-01-01",
        discovery_folds=(discovery_1, discovery_2),
        hidden_fold=hidden,
    )


def _call_budgets() -> tuple[contract.PitOptimizerCallBudget, ...]:
    role_caps = {
        "investigator": (8_000, 80_000, 88_000, 4_000, 8 * 1024, 0.05),
        "author": (12_000, 76_000, 88_000, 8_000, 16 * 1024, 0.10),
        "critic": (8_000, 24_000, 32_000, 4_000, 8 * 1024, 0.05),
    }
    return tuple(
        contract.PitOptimizerCallBudget(
            call_index=(iteration - 1) * 3 + ordinal,
            iteration=iteration,
            role=role,
            model="deepseek/deepseek-r1",
            max_static_input_bytes=role_caps[role][0],
            max_dynamic_input_bytes=role_caps[role][1],
            max_input_tokens=role_caps[role][2],
            max_output_tokens=role_caps[role][3],
            max_response_bytes=role_caps[role][4],
            max_usd=role_caps[role][5],
        )
        for iteration in (1, 2)
        for ordinal, role in enumerate(contract.OPTIMIZER_V2_ROLES, start=1)
    )


def _v2_manifest() -> contract.PitOptimizerRunManifest:
    candidate_bounds = contract.PatchBounds(3, 12, 80, 8 * 1024)
    hard_bounds = contract.PatchBounds(3, 12, 200, 64 * 1024)
    paths = (
        "core/strategy_policy/entry.py",
        "core/strategy_policy/risk.py",
        "core/strategy_policy/exit.py",
    )
    source_sha256s = tuple((path, str(index) * 64) for index, path in enumerate(paths, 1))
    scope = contract.PolicySourceScope(
        schema_version=2,
        policy_interface_version=1,
        initial_policy_source_sha256s=source_sha256s,
        editable_paths=paths,
        max_policy_source_bundle_bytes=64 * 1024,
        max_iteration_feedback_bytes=4 * 1024,
        max_iteration_history_bytes=32 * 1024,
        hard_patch_bounds=hard_bounds,
        candidate_bounds=candidate_bounds,
        max_iterations=2,
        allowed_descendant_rule="authenticated_initial_sources_plus_validated_cumulative_diff",
    )
    budgets = _call_budgets()
    constraint_ids = ("causal_only", "no_external_io")
    constraints_sha256 = hashlib.sha256(
        json.dumps(
            list(constraint_ids),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    ).hexdigest()
    authorization = contract.AuthorizationRequirement(
        window_id="window_1",
        max_calls=6,
        max_tokens=448_000,
        max_usd=0.40,
        policy_source_scope_sha256=scope.sha256,
        provider_retries=0,
        apply=False,
    )
    return contract.PitOptimizerRunManifest(
        schema_version=2,
        run_id="run_1",
        run_kind="subset_canary",
        model="deepseek/deepseek-r1",
        source_head="c" * 40,
        source_fingerprint_sha256="d" * 64,
        legacy_readiness_sha256="e" * 64,
        pit_bundle_sha256="a" * 64,
        baseline_manifest_sha256="f" * 64,
        effective_policy_sha256="9" * 64,
        policy_interface_version=1,
        policy_source_sha256s=source_sha256s,
        editable_paths=paths,
        policy_source_scope=scope,
        immutable_constraints_sha256=constraints_sha256,
        fold_manifest=_fold_manifest(),
        parity_attestation_sha256="7" * 64,
        sandbox_image="example.invalid/pit-optimizer@sha256:" + "6" * 64,
        validation_ledger_name="pit_optimizer_validation_ledger.jsonl",
        immutable_constraint_ids=constraint_ids,
        candidate_bounds=candidate_bounds,
        call_budgets=budgets,
        max_iterations=2,
        non_improving_limit=3,
        authorization_requirement=authorization,
    )


def _independent_digest(value: object) -> str:
    payload = (
        json.dumps(
            asdict(value),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    return hashlib.sha256(payload).hexdigest()


def test_manifest_identity_binds_scope_budget_order_and_authorization() -> None:
    """Break caught: expanded source or reordered calls could reuse an old authorization."""
    manifest = _v2_manifest()

    assert manifest.policy_source_scope.sha256 == _independent_digest(
        manifest.policy_source_scope
    )
    assert manifest.authorization_requirement.sha256 == _independent_digest(
        manifest.authorization_requirement
    )
    assert manifest.sha256 == _independent_digest(manifest)
    assert [
        (item.call_index, item.iteration, item.role)
        for item in manifest.call_budgets
    ] == [
        (1, 1, "investigator"),
        (2, 1, "author"),
        (3, 1, "critic"),
        (4, 2, "investigator"),
        (5, 2, "author"),
        (6, 2, "critic"),
    ]
    assert sum(item.max_input_tokens for item in manifest.call_budgets) == 416_000
    assert sum(item.max_output_tokens for item in manifest.call_budgets) == 32_000
    assert sum(
        item.max_input_tokens + item.max_output_tokens
        for item in manifest.call_budgets
    ) == 448_000
    assert manifest.authorization_requirement.max_tokens == 448_000
    assert sum(item.max_usd for item in manifest.call_budgets) == pytest.approx(0.40)
    assert manifest.authorization_requirement.apply is False
    assert manifest.authorization_requirement.provider_retries == 0

    expanded = replace(
        manifest.policy_source_scope,
        max_iterations=manifest.policy_source_scope.max_iterations + 1,
    )
    assert expanded.sha256 != manifest.policy_source_scope.sha256
    assert (
        manifest.authorization_requirement.policy_source_scope_sha256
        == manifest.policy_source_scope.sha256
    )
    with pytest.raises(ValueError, match="source scope authorization"):
        replace(manifest, policy_source_scope=expanded, max_iterations=3)
    with pytest.raises(ValueError, match="call order"):
        replace(
            manifest,
            call_budgets=(
                replace(manifest.call_budgets[0], call_index=2),
                *manifest.call_budgets[1:],
            ),
        )
    with pytest.raises(ValueError, match="hard patch bounds"):
        replace(
            manifest,
            candidate_bounds=contract.PatchBounds(3, 12, 201, 8 * 1024),
        )
    with pytest.raises(ValueError, match="apply"):
        replace(
            manifest.authorization_requirement,
            apply=True,
        )
    inflated_output = replace(
        manifest.call_budgets[0],
        max_output_tokens=manifest.call_budgets[0].max_output_tokens + 1,
    )
    with pytest.raises(ValueError, match="tokens exceed authorization"):
        replace(manifest, call_budgets=(inflated_output, *manifest.call_budgets[1:]))
    with pytest.raises(ValueError, match="exactly consume authorization"):
        replace(
            manifest,
            authorization_requirement=replace(
                manifest.authorization_requirement,
                max_tokens=448_001,
            ),
        )
    with pytest.raises(ValueError, match="exactly 448000"):
        replace(
            manifest,
            call_budgets=(inflated_output, *manifest.call_budgets[1:]),
            authorization_requirement=replace(
                manifest.authorization_requirement,
                max_tokens=448_001,
            ),
        )


_PIT_METADATA = {
    "bundle_kind": "canslim_pit_v1",
    "schema_version": "1",
    "data_cutoff": "2022-03-11",
    "evaluation_start": "2021-06-25",
    "warmup_start": "2020-01-01",
    "membership_source_sha256": "0" * 64,
    "prices_source_sha256": "0" * 64,
    "fundamentals_source_sha256": "0" * 64,
    "membership_provenance_sha256": "0" * 64,
    "prices_provenance_sha256": "0" * 64,
    "fundamentals_provenance_sha256": "0" * 64,
    "membership_source_kind": "offline_test_fixture",
    "membership_revision_id": "fixture-v1",
    "membership_raw_sha256": "0" * 64,
    "membership_symbol_map_sha256": "0" * 64,
    "membership_security_names_sha256": "0" * 64,
    "prices_source_kind": "offline_test_fixture",
    "prices_upstream_source_sha256": "0" * 64,
    "spy_trading_days_sha256": "0" * 64,
    "price_identity_map_sha256": "0" * 64,
    "price_identity_request_contracts_sha256": "0" * 64,
    "price_exclusion_count": "0",
    "price_exclusions_sha256": "0" * 64,
    "fundamentals_source_kind": "offline_test_fixture",
    "fundamentals_submissions_archive_sha256": "0" * 64,
    "fundamentals_companyfacts_archive_sha256": "0" * 64,
    "fundamentals_identity_manifest_csv_sha256": "0" * 64,
}


def _member_symbols(count: int) -> tuple[str, ...]:
    symbols: list[str] = []
    for letters in product(string.ascii_uppercase, repeat=3):
        ticker = "".join(letters)
        if ticker != "SPY":
            symbols.append(ticker)
        if len(symbols) == count:
            return tuple(symbols)
    raise AssertionError("could not create synthetic tickers")


def _write_pit_bundle(path: Path) -> tuple[str, tuple[str, ...]]:
    members = _member_symbols(495)
    fold_sessions = (
        *_sessions("2021-06-25", "2021-09-20"),
        *_sessions("2021-09-21", "2021-12-14"),
        *_sessions("2021-12-15", "2022-03-11"),
    )
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "CREATE TABLE dataset_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE membership (effective_date TEXT NOT NULL, ticker TEXT NOT NULL, member INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE price (trade_date TEXT NOT NULL, ticker TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE fundamentals (ticker TEXT NOT NULL, statement_type TEXT NOT NULL, period_end TEXT NOT NULL, public_date TEXT NOT NULL, basic_eps REAL, diluted_eps REAL, total_revenue REAL, net_income REAL, common_stock REAL, total_stockholders_equity REAL, shares_outstanding REAL, held_percent_institutions REAL, institution_count INTEGER, prev_institution_count INTEGER)"
        )
        connection.executemany(
            "INSERT INTO dataset_metadata VALUES (?, ?)", _PIT_METADATA.items()
        )
        connection.executemany(
            "INSERT INTO membership VALUES (?, ?, ?)",
            (("2021-01-01", symbol, 1) for symbol in members),
        )
        connection.executemany(
            "INSERT INTO price VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                [("2020-01-02", "SPY", 100.0, 101.0, 99.0, 100.0, 1_000.0)]
                + [
                    (session, "SPY", 100.0, 101.0, 99.0, 100.0, 1_000.0)
                    for session in fold_sessions
                ]
            ),
        )
        connection.execute(
            "INSERT INTO fundamentals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                members[0],
                "quarterly",
                "2020-09-30",
                "2020-11-01",
                1.0,
                1.0,
                100.0,
                10.0,
                None,
                None,
                1_000_000.0,
                None,
                None,
                None,
            ),
        )
        connection.commit()
    return hashlib.sha256(path.read_bytes()).hexdigest(), members[:25]


def _canonical_file_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _builder_fixture(
    tmp_path: Path,
    *,
    source_padding: int = 0,
) -> tuple[dict[str, object], dict[str, object]]:
    source_root = tmp_path / "source"
    source_texts = {
        "core/strategy_policy/entry.py": (
            "raise RuntimeError('must not import')\n"
            "def evaluate_entry(snapshot):\n"
            "    return None\n"
            + ("# bounded source padding\n" * source_padding)
        ),
        "core/strategy_policy/risk.py": "raise RuntimeError('must not import')\ndef recommend_capacity(snapshot):\n    return 1\n",
        "core/strategy_policy/exit.py": "raise RuntimeError('must not import')\ndef evaluate_exit(snapshot):\n    return None\n",
    }
    for relative, text in source_texts.items():
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    for arguments in (
        ("init",),
        ("config", "user.name", "Optimizer Contract Test"),
        ("config", "user.email", "optimizer@example.invalid"),
        ("add", "."),
        ("commit", "-m", "synthetic policy source"),
    ):
        subprocess.run(
            ["git", *arguments],
            cwd=source_root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    source_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source_root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    source_tree = subprocess.run(
        ["git", "ls-tree", "-r", "--full-tree", "HEAD"],
        cwd=source_root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout
    source_fingerprint = hashlib.sha256(source_tree).hexdigest()

    pit_bundle = tmp_path / "pit.sqlite3"
    pit_sha256, universe = _write_pit_bundle(pit_bundle)
    universe_sha256 = hashlib.sha256(_canonical_file_bytes(list(universe))).hexdigest()
    fold_manifest = FoldManifest(
        data_identity_sha256=pit_sha256,
        universe_sha256=universe_sha256,
        benchmark="SPY",
        warmup_start_date="2021-01-01",
        discovery_folds=(
            FoldSpec(
                "discovery_1",
                "discovery",
                "2021-06-25",
                "2021-09-20",
                _sessions("2021-06-25", "2021-09-20"),
            ),
            FoldSpec(
                "discovery_2",
                "discovery",
                "2021-09-21",
                "2021-12-14",
                _sessions("2021-09-21", "2021-12-14"),
            ),
        ),
        hidden_fold=FoldSpec(
            "hidden_1",
            "hidden",
            "2021-12-15",
            "2022-03-11",
            _sessions("2021-12-15", "2022-03-11"),
        ),
    )

    baseline_run = tmp_path / "baseline"
    baseline_run.mkdir()
    baseline_bytes = _canonical_file_bytes({"schema_version": 1, "run": "baseline"})
    (baseline_run / "run_manifest.json").write_bytes(baseline_bytes)
    baseline_sha256 = hashlib.sha256(baseline_bytes).hexdigest()
    policy = {"schema_version": 1, "policy": "synthetic"}
    effective_policy_sha256 = hashlib.sha256(_canonical_file_bytes(policy)).hexdigest()
    constraint_ids = ["causal_only", "no_external_io"]
    readiness = {
        "schema_version": 1,
        "gate": "pit_optimization",
        "phase": "ready",
        "identities": {
            "source_head": source_head,
            "source_fingerprint_sha256": source_fingerprint,
            "pit_bundle_sha256": pit_sha256,
            "baseline_manifest_sha256": baseline_sha256,
            "effective_policy_sha256": effective_policy_sha256,
        },
        "sealed_inputs": {
            "pit_bundle_sha256": pit_sha256,
            "baseline_artifact_sha256": {"run_manifest.json": baseline_sha256},
        },
        "evaluation_contract": {
            "verification_only": True,
            "scope": {
                "benchmark": "SPY",
                "discovery_start": "2021-06-25",
                "discovery_end": "2021-09-20",
                "holdout_start": "2021-09-21",
                "holdout_end": "2021-12-14",
                "warmup_start": "2021-01-01",
                "session_count": 60,
                "symbol_count": 25,
                "symbols": list(universe),
            },
        },
        "effective_policy": policy,
        "invariant_ids": constraint_ids,
    }
    readiness_path = tmp_path / "legacy-readiness.json"
    readiness_bytes = _canonical_file_bytes(readiness)
    readiness_path.write_bytes(readiness_bytes)

    parity_path = tmp_path / "verified-parity.json"
    provisional = ParityAttestation(
        schema_version=1,
        reference_artifact_sha256="4" * 64,
        reference_source_head="1" * 40,
        final_source_head=source_head,
        final_source_fingerprint_sha256=source_fingerprint,
        pit_bundle_sha256=pit_sha256,
        baseline_manifest_sha256=baseline_sha256,
        effective_policy_sha256=effective_policy_sha256,
        discovery_fold_manifest_sha256=fold_manifest.sha256,
        policy_interface_version=1,
        reference_output_sha256s=(),
        final_output_sha256s=(),
        final_discovery_evidence=(),
        transactions_equal=True,
        entry_outcomes_equal=True,
        equity_equal=True,
        funnels_equal=True,
        effective_policy_equal=True,
        artifact_path=parity_path.resolve(),
        artifact_sha256="0" * 64,
    )
    parity_primitive = asdict(provisional)
    parity_primitive.pop("artifact_path")
    parity_primitive.pop("artifact_sha256")
    parity_bytes = _canonical_file_bytes(parity_primitive)
    parity_path.write_bytes(parity_bytes)
    parity = replace(
        provisional,
        artifact_sha256=hashlib.sha256(parity_bytes).hexdigest(),
    )

    permanent_runtime_root = tmp_path / "runtime"
    controller_temp_parent = tmp_path / "controller-temp"
    artifact_root = tmp_path / "artifacts"
    for directory in (permanent_runtime_root, controller_temp_parent, artifact_root):
        directory.mkdir()
    inputs = {
        "legacy_readiness": readiness,
        "legacy_readiness_path": readiness_path,
        "parity_attestation": parity,
        "verified_parity_path": parity_path,
        "pit_bundle": pit_bundle,
        "baseline_run": baseline_run,
        "source_root": source_root,
        "permanent_runtime_root": permanent_runtime_root,
        "controller_temp_parent": controller_temp_parent,
        "artifact_root": artifact_root,
        "sandbox_image": "example.invalid/pit-optimizer@sha256:" + "6" * 64,
        "call_budgets": _call_budgets(),
        "candidate_bounds": contract.PatchBounds(3, 12, 80, 8 * 1024),
        "max_iterations": 2,
    }
    expected = {
        "fold_manifest": fold_manifest,
        "source_texts": source_texts,
        "readiness_sha256": hashlib.sha256(readiness_bytes).hexdigest(),
    }
    return inputs, expected


def _patch_authenticated_readiness(
    monkeypatch: pytest.MonkeyPatch,
    inputs: dict[str, object],
) -> None:
    import core.pit_policy_parity as parity

    readiness = inputs["legacy_readiness"]
    readiness_path = Path(inputs["legacy_readiness_path"])
    readiness_sha256 = hashlib.sha256(readiness_path.read_bytes()).hexdigest()

    def authenticate(path: Path, *, source_root: Path) -> tuple[dict[str, object], str]:
        assert Path(path).resolve() == readiness_path.resolve()
        assert Path(source_root).resolve() == Path(inputs["source_root"]).resolve()
        assert isinstance(readiness, dict)
        return readiness, readiness_sha256

    monkeypatch.setattr(parity, "_authenticated_readiness", authenticate)


def test_manifest_builder_is_provider_free_canonical_and_source_budgeted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: preparation could execute source or seal a context that cannot fit."""
    inputs, expected = _builder_fixture(tmp_path)
    _patch_authenticated_readiness(monkeypatch, inputs)

    manifest = contract.build_subset_manifest(**inputs)
    second = contract.build_subset_manifest(**inputs)

    assert manifest.fold_manifest == expected["fold_manifest"]
    assert manifest.legacy_readiness_sha256 == expected["readiness_sha256"]
    assert manifest.run_id != second.run_id
    assert (
        manifest.authorization_requirement.window_id
        != second.authorization_requirement.window_id
    )
    assert manifest.authorization_requirement.max_calls == 6
    assert manifest.authorization_requirement.max_tokens == 448_000
    assert manifest.authorization_requirement.max_usd == pytest.approx(0.40)
    assert manifest.authorization_requirement.apply is False
    assert manifest.authorization_requirement.provider_retries == 0

    source_records = []
    declared = {
        "core/strategy_policy/entry.py": ["core.strategy_policy.entry.evaluate_entry"],
        "core/strategy_policy/risk.py": ["core.strategy_policy.risk.recommend_capacity"],
        "core/strategy_policy/exit.py": ["core.strategy_policy.exit.evaluate_exit"],
    }
    for path, text in expected["source_texts"].items():
        source_records.append(
            {
                "path": path,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "declared_symbols": declared[path],
                "text": text,
            }
        )
    initial_bundle = {
        "policy_interface_version": 1,
        "cumulative_diff_sha256": hashlib.sha256(b"").hexdigest(),
        "cumulative_diff": "",
        "files": source_records,
    }
    initial_policy_bytes = sum(
        len(text.encode("utf-8")) for text in expected["source_texts"].values()
    )
    envelope_bytes = len(_canonical_text(initial_bundle).encode("utf-8")) - initial_policy_bytes
    assert initial_policy_bytes + (2 * 8 * 1024) + envelope_bytes <= 64 * 1024
    worst_records = [dict(record) for record in source_records]
    worst_records[0]["text"] = str(worst_records[0]["text"]) + ("s" * (8 * 1024))
    worst_records[0]["sha256"] = hashlib.sha256(
        str(worst_records[0]["text"]).encode("utf-8")
    ).hexdigest()
    worst_iteration_2_bundle = {
        "policy_interface_version": 1,
        "cumulative_diff_sha256": hashlib.sha256(
            ("d" * (8 * 1024)).encode("utf-8")
        ).hexdigest(),
        "cumulative_diff": "d" * (8 * 1024),
        "files": worst_records,
    }
    assert len(_canonical_text(worst_iteration_2_bundle).encode("utf-8")) == (
        initial_policy_bytes + (2 * 8 * 1024) + envelope_bytes
    )
    assert len(_canonical_text(worst_iteration_2_bundle).encode("utf-8")) <= 64 * 1024
    assert manifest.policy_source_sha256s == tuple(
        (record["path"], record["sha256"]) for record in source_records
    )
    rendered = contract.render_worst_iteration_two_role_inputs(
        scope=manifest.policy_source_scope,
        source_texts=expected["source_texts"],
        immutable_constraint_ids=manifest.immutable_constraint_ids,
        call_budgets=manifest.call_budgets,
    )
    rendered_values = {
        role: json.loads(payload.decode("utf-8"))
        for role, payload in rendered.items()
    }
    investigator_value = rendered_values["investigator"]
    source_value = investigator_value["source_bundle"]
    worst_raw_diff_bytes = manifest.policy_source_scope.candidate_bounds.max_diff_bytes
    expected_diff_maximizer = "\\" * worst_raw_diff_bytes
    assert len(source_value["cumulative_diff"].encode("utf-8")) == worst_raw_diff_bytes
    assert source_value["cumulative_diff"] == expected_diff_maximizer
    entry_record = next(
        record
        for record in source_value["files"]
        if record["path"] == "core/strategy_policy/entry.py"
    )
    initial_entry = expected["source_texts"]["core/strategy_policy/entry.py"]
    assert entry_record["text"][len(initial_entry) :] == expected_diff_maximizer

    incumbent_value = investigator_value["incumbent_summary"]
    expected_text_maximizer = "\\" * contract.MAX_ROLE_TEXT_BYTES
    assert incumbent_value["behavioral_summary"] == expected_text_maximizer
    unicode_incumbent = {
        **incumbent_value,
        "behavioral_summary": "é"
        * (contract.MAX_ROLE_TEXT_BYTES // len("é".encode("utf-8"))),
    }
    incumbent_bytes = len(
        json.dumps(
            incumbent_value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )
    unicode_incumbent_bytes = len(
        json.dumps(
            unicode_incumbent,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )
    assert incumbent_bytes - unicode_incumbent_bytes == contract.MAX_ROLE_TEXT_BYTES
    cap_derived_sections = (
        (
            rendered_values["investigator"]["rule_summary"],
            contract.MAX_DISCOVERY_EVIDENCE_BYTES,
        ),
        (
            rendered_values["investigator"]["baseline_discovery"],
            contract.MAX_DISCOVERY_EVIDENCE_BYTES,
        ),
        (
            rendered_values["investigator"]["prior_iterations"][0],
            manifest.policy_source_scope.max_iteration_feedback_bytes,
        ),
        (
            rendered_values["author"]["investigator"],
            contract.MAX_INVESTIGATOR_ARTIFACT_BYTES,
        ),
        (
            rendered_values["critic"]["author_manifest"],
            contract.MAX_AUTHOR_NON_DIFF_ARTIFACT_BYTES,
        ),
        (
            rendered_values["critic"]["candidate_vs_baseline"],
            contract.MAX_CANDIDATE_COMPARISON_BYTES,
        ),
    )
    for section, cap in cap_derived_sections:
        section_bytes = len(_canonical_text(section).encode("utf-8"))
        assert cap - 1 <= section_bytes <= cap
    for budget in manifest.call_budgets:
        static_bytes = len(
            contract.PIT_OPTIMIZER_V2_SYSTEM_PROMPTS[budget.role].encode("utf-8")
        ) + len(
            _canonical_text(contract.pit_optimizer_response_format(budget.role)).encode(
                "utf-8"
            )
        )
        assert static_bytes <= budget.max_static_input_bytes
        assert static_bytes + budget.max_dynamic_input_bytes <= budget.max_input_tokens
        if budget.iteration == 2:
            assert len(rendered[budget.role]) <= budget.max_dynamic_input_bytes
            assert static_bytes + len(rendered[budget.role]) <= budget.max_input_tokens

    output = tmp_path / "optimizer-manifest.json"
    written, digest = contract.write_optimizer_manifest(manifest, output)
    assert written == output.resolve()
    assert output.read_bytes() == _canonical_file_bytes(asdict(manifest))
    assert digest == manifest.sha256 == hashlib.sha256(output.read_bytes()).hexdigest()
    with pytest.raises(FileExistsError):
        contract.write_optimizer_manifest(manifest, output)

    with pytest.raises(ValueError, match="source bundle"):
        contract.build_subset_manifest(
            **{
                **inputs,
                "candidate_bounds": contract.PatchBounds(3, 12, 80, 32 * 1024),
            }
        )


def test_manifest_builder_rejects_fabricated_non_git_source_identity(
    tmp_path: Path,
) -> None:
    """Break caught: caller-provided source hashes could authorize unrelated file bytes."""
    inputs, _expected = _builder_fixture(tmp_path)
    inputs["source_root"] = Path(inputs["source_root"]) / "core"

    with pytest.raises(ValueError, match="Git repository root"):
        contract.build_subset_manifest(**inputs)


def test_manifest_builder_requires_complete_authenticated_v1_readiness(
    tmp_path: Path,
) -> None:
    """Break caught: a caller-selected readiness subset could bypass schema-v1 identity."""
    inputs, _expected = _builder_fixture(tmp_path)

    with pytest.raises(ValueError, match="closed readiness contract"):
        contract.build_subset_manifest(**inputs)


def test_manifest_builder_renders_and_rejects_oversized_iteration_two_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: declared arithmetic could hide an oversized actual role message."""
    inputs, _expected = _builder_fixture(tmp_path, source_padding=1_000)
    _patch_authenticated_readiness(monkeypatch, inputs)

    with pytest.raises(ValueError, match="worst iteration-2 investigator"):
        contract.build_subset_manifest(**inputs)


def test_gate_and_prepare_command_authenticate_without_granting_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a prepare command could imply spending or use an unauthenticated path."""
    inputs, _expected = _builder_fixture(tmp_path)
    _patch_authenticated_readiness(monkeypatch, inputs)
    manifest = contract.build_subset_manifest(**inputs)
    manifest_path = tmp_path / "optimizer-manifest.json"
    contract.write_optimizer_manifest(manifest, manifest_path)
    gate = contract.PitOptimizerGateConfig(
        phase="prepare",
        baseline_run=inputs["baseline_run"],
        baseline_manifest_sha256=manifest.baseline_manifest_sha256,
        pit_bundle=inputs["pit_bundle"],
        pit_bundle_sha256=manifest.pit_bundle_sha256,
        effective_policy_sha256=manifest.effective_policy_sha256,
        optimizer_manifest=manifest_path,
        optimizer_manifest_sha256=manifest.sha256,
        verified_parity_artifact=inputs["verified_parity_path"],
        verified_parity_sha256=manifest.parity_attestation_sha256,
        readiness_artifact=None,
        readiness_sha256=None,
        authorization_window_id=None,
        authorization_requirement_sha256=manifest.authorization_requirement.sha256,
        source_transmission_authorized=False,
        max_usd=0.40,
        max_api_calls=6,
        max_tokens=448_000,
        max_iterations=2,
        apply=False,
    )

    gate.validate()
    with pytest.raises(ValueError, match="source transmission"):
        replace(gate, source_transmission_authorized=True).validate()
    with pytest.raises(ValueError, match="apply"):
        replace(gate, apply=True).validate()
    with pytest.raises(ValueError, match="optimizer manifest"):
        replace(gate, optimizer_manifest_sha256="0" * 64).validate()
    parity_mismatch = replace(manifest, parity_attestation_sha256="8" * 64)
    parity_mismatch_path = tmp_path / "optimizer-manifest-parity-mismatch.json"
    contract.write_optimizer_manifest(parity_mismatch, parity_mismatch_path)
    with pytest.raises(ValueError, match="parity identity differs from manifest"):
        replace(
            gate,
            optimizer_manifest=parity_mismatch_path,
            optimizer_manifest_sha256=parity_mismatch.sha256,
        ).validate()
    run_gate = replace(
        gate,
        phase="run",
        readiness_artifact=inputs["legacy_readiness_path"],
        readiness_sha256=manifest.legacy_readiness_sha256,
        authorization_window_id=manifest.authorization_requirement.window_id,
        source_transmission_authorized=True,
    )
    run_gate.validate()
    readiness_mismatch = replace(manifest, legacy_readiness_sha256="8" * 64)
    readiness_mismatch_path = tmp_path / "optimizer-manifest-readiness-mismatch.json"
    contract.write_optimizer_manifest(readiness_mismatch, readiness_mismatch_path)
    with pytest.raises(ValueError, match="readiness identity differs from manifest"):
        replace(
            run_gate,
            optimizer_manifest=readiness_mismatch_path,
            optimizer_manifest_sha256=readiness_mismatch.sha256,
        ).validate()
    with pytest.raises(ValueError, match="authorization window"):
        replace(run_gate, authorization_window_id="window_live").validate()
    with pytest.raises(ValueError, match="apply"):
        replace(run_gate, apply=True).validate()

    git_executable = tmp_path / "git.exe"
    docker_executable = tmp_path / "docker.exe"
    git_executable.write_bytes(b"synthetic executable")
    docker_executable.write_bytes(b"synthetic executable")
    command = contract.build_prepare_command(
        manifest,
        manifest_path=manifest_path,
        legacy_readiness_path=inputs["legacy_readiness_path"],
        verified_parity_path=inputs["verified_parity_path"],
        pit_bundle_path=inputs["pit_bundle"],
        baseline_run_path=inputs["baseline_run"],
        repo_root=inputs["source_root"],
        permanent_runtime_root=inputs["permanent_runtime_root"],
        controller_temp_parent=inputs["controller_temp_parent"],
        artifact_root=inputs["artifact_root"],
        git_executable=git_executable,
        docker_executable=docker_executable,
        sandbox_image=inputs["sandbox_image"],
    )

    assert "core.pit_optimization prepare-v2" in command
    for value in (
        manifest.sha256,
        manifest.legacy_readiness_sha256,
        manifest.parity_attestation_sha256,
        manifest.pit_bundle_sha256,
        manifest.baseline_manifest_sha256,
        manifest.authorization_requirement.sha256,
        str(inputs["permanent_runtime_root"].resolve()),
        str(git_executable.resolve()),
        str(docker_executable.resolve()),
    ):
        assert value in command
    assert "authorization-window" not in command
    assert "source-transmission-authorized" not in command
    assert "credential" not in command.lower()


def test_objective_is_quantized_lexicographic_strict_and_trade_eligible() -> None:
    """Break caught: floating or non-strict ranking could promote an ineligible candidate."""
    folds = (
        replace(
            _fold_summary("discovery_1", 1.235),
            max_drawdown_pct=-4.005,
            closed_trades=1,
        ),
        replace(
            _fold_summary("discovery_2", 0.505),
            max_drawdown_pct=-2.0,
            closed_trades=3,
        ),
    )

    original_baseline = (
        _fold_summary("discovery_1", 0.0),
        _fold_summary("discovery_2", 0.0),
    )
    baseline_sha256 = _aggregate_sha256(original_baseline)
    score = evaluation.discovery_score_from_folds(
        folds,
        original_baseline,
        original_baseline_sha256=baseline_sha256,
        expected_original_baseline_sha256=baseline_sha256,
    )

    expected_first = Decimal(str(1.235)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_EVEN
    )
    expected_second = Decimal(str(0.505)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_EVEN
    )
    assert score == evaluation.DiscoveryScore(
        median_excess_return_pp=((expected_first + expected_second) / 2).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_EVEN
        ),
        worst_excess_return_pp=expected_second,
        max_drawdown_magnitude_pp=Decimal(str(4.005)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_EVEN
        ),
    )
    assert score.ordering_key == (
        Decimal("0.87"),
        Decimal("0.50"),
        Decimal("-4.00"),
    )
    assert evaluation.strictly_improves_discovery(score, score) is False
    assert evaluation.strictly_improves_discovery(
        replace(score, worst_excess_return_pp=Decimal("0.51")),
        score,
    ) is True
    with pytest.raises(ValueError, match="closed discovery trade"):
        evaluation.discovery_score_from_folds(
            (replace(folds[0], closed_trades=0), folds[1]),
            original_baseline,
            original_baseline_sha256=baseline_sha256,
            expected_original_baseline_sha256=baseline_sha256,
        )


def test_discovery_objective_derives_excess_from_authenticated_fixed_baseline(
    tmp_path: Path,
) -> None:
    """Break caught: fabricated excess or a substituted incumbent could drive ranking."""
    candidate = (
        replace(
            _fold_summary("discovery_1", 0.0),
            total_return_pct=2.235,
            excess_total_return_pp=99.0,
        ),
        replace(
            _fold_summary("discovery_2", 0.0),
            total_return_pct=1.505,
            excess_total_return_pp=-99.0,
        ),
    )
    original_baseline = (
        replace(_fold_summary("discovery_1", 0.0), total_return_pct=1.0),
        replace(_fold_summary("discovery_2", 0.0), total_return_pct=1.0),
    )
    baseline_sha256 = _aggregate_sha256(original_baseline)

    score = evaluation.discovery_score_from_folds(
        candidate,
        original_baseline,
        original_baseline_sha256=baseline_sha256,
        expected_original_baseline_sha256=baseline_sha256,
    )

    assert score.median_excess_return_pp == Decimal("0.87")
    assert score.worst_excess_return_pp == Decimal("0.50")
    with pytest.raises(ValueError, match="fixed baseline identity"):
        evaluation.discovery_score_from_folds(
            candidate,
            original_baseline,
            original_baseline_sha256=baseline_sha256,
            expected_original_baseline_sha256="f" * 64,
        )
    with pytest.raises(ValueError, match="supplied score differs"):
        proof = _discovery_exposure_proof(tmp_path)
        contract.candidate_comparison_from_fixed_baseline(
            candidate_folds=candidate,
            original_baseline_folds=original_baseline,
            original_baseline_sha256=baseline_sha256,
            expected_original_baseline_sha256=baseline_sha256,
            discovery_exposure=proof,
            expected_window_identities=proof.window_identities,
            expected_metadata=proof.metadata,
            diagnostics=(),
            supplied_score=replace(
                score,
                worst_excess_return_pp=score.worst_excess_return_pp
                + Decimal("0.01"),
            ),
        )


def test_holdout_gate_uses_return_trades_and_completeness_without_sharpe() -> None:
    """Break caught: a Sharpe gate or permissive equality boundary could alter eligibility."""
    decision = evaluation.HoldoutDecision.from_result(
        excess_total_return_pp=0.105,
        closed_trades=3,
        safety_complete=True,
        integrity_complete=True,
        accounting_complete=True,
    )

    assert decision.excess_total_return_pp == Decimal("0.10")
    assert decision.long_replay_eligible is True
    assert "sharpe" not in {field.name for field in fields(evaluation.HoldoutDecision)}
    assert evaluation.HoldoutDecision.from_result(
        excess_total_return_pp=0.094,
        closed_trades=3,
        safety_complete=True,
        integrity_complete=True,
        accounting_complete=True,
    ).long_replay_eligible is False
    assert evaluation.HoldoutDecision.from_result(
        excess_total_return_pp=0.10,
        closed_trades=2,
        safety_complete=True,
        integrity_complete=True,
        accounting_complete=True,
    ).long_replay_eligible is False
    with pytest.raises(ValueError, match="eligibility"):
        evaluation.HoldoutDecision(
            excess_total_return_pp=Decimal("0.10"),
            closed_trades=3,
            safety_complete=True,
            integrity_complete=True,
            accounting_complete=True,
            long_replay_eligible=False,
        )


def _validation_identity(fold: FoldSpec, suffix: str) -> evaluation.ValidationWindowIdentity:
    sessions_sha256 = hashlib.sha256(
        _canonical_file_bytes(list(fold.sessions))
    ).hexdigest()
    return evaluation.ValidationWindowIdentity(
        pit_bundle_sha256="a" * 64,
        universe_sha256="b" * 64,
        benchmark="SPY",
        warmup_contract_sha256="c" * 64,
        sessions_sha256=sessions_sha256,
        session_count=60,
        first_session=fold.start_date,
        last_session=fold.end_date,
    )


def test_validation_ledger_permanently_consumes_identity_before_outcome(
    tmp_path: Path,
) -> None:
    """Break caught: metadata changes or a failed evaluation could make a window hidden again."""
    ledger_path = tmp_path / "pit_optimizer_validation_ledger.jsonl"
    ledger = evaluation.ValidationLedger(ledger_path)
    folds = _fold_manifest()
    provider_metadata = evaluation.ValidationExposureMetadata(
        run_id="run_1",
        source_head="1" * 40,
        baseline_policy_sha256="d" * 64,
        candidate_identity_sha256=None,
        exposure_kind="provider_context",
    )
    discovery_reservations = tuple(
        ledger.mark_discovery(_validation_identity(fold, fold.fold_id), provider_metadata)
        for fold in folds.discovery_folds
    )

    first_identity = _validation_identity(folds.discovery_folds[0], "discovery_1")
    expected_key = hashlib.sha256(
        _canonical_file_bytes(asdict(first_identity))
    ).hexdigest()
    assert discovery_reservations[0].consumption_key_sha256 == expected_key
    with pytest.raises(ValueError, match="consumed"):
        ledger.reserve_hidden(
            first_identity,
            evaluation.ValidationExposureMetadata(
                run_id="run_later",
                source_head="2" * 40,
                baseline_policy_sha256="e" * 64,
                candidate_identity_sha256="f" * 64,
                exposure_kind="hidden_validation",
            ),
        )

    hidden_identity = _validation_identity(folds.hidden_fold, "hidden_1")
    hidden_metadata = evaluation.ValidationExposureMetadata(
        run_id="run_1",
        source_head="1" * 40,
        baseline_policy_sha256="d" * 64,
        candidate_identity_sha256="f" * 64,
        exposure_kind="hidden_validation",
    )
    hidden_reservation = ledger.reserve_hidden(hidden_identity, hidden_metadata)
    with pytest.raises(ValueError, match="outcome failure code is not closed"):
        ledger.record_outcome(
            hidden_reservation,
            attempted=True,
            completed=False,
            failure_code="invented_failure",
        )
    ledger.record_outcome(
        hidden_reservation,
        attempted=True,
        completed=False,
        failure_code="worker_failed",
    )

    reopened = evaluation.ValidationLedger(ledger_path)
    with pytest.raises(ValueError, match="consumed"):
        reopened.reserve_hidden(
            hidden_identity,
            replace(hidden_metadata, run_id="run_later", source_head="2" * 40),
        )
    records = [json.loads(line) for line in ledger_path.read_text("utf-8").splitlines()]
    assert [record["record_type"] for record in records] == [
        "consumption",
        "consumption",
        "consumption",
        "outcome",
    ]
    assert records[1]["previous_record_sha256"] == records[0]["record_sha256"]
    outcome = records[-1]
    assert set(outcome) == {
        "schema_version",
        "record_type",
        "record_index",
        "previous_record_sha256",
        "reservation_record_sha256",
        "attempted",
        "completed",
        "failure_code",
        "record_sha256",
    }
    assert not {"return", "sharpe", "trades", "holdings", "metrics"}.intersection(
        outcome
    )


def test_validation_ledger_atomically_rejects_concurrent_duplicate_reservation(
    tmp_path: Path,
) -> None:
    """Break caught: two controllers could race the same fold past uniqueness."""
    ledger_path = tmp_path / "pit_optimizer_validation_ledger.jsonl"
    identity = _validation_identity(_fold_manifest().hidden_fold, "hidden_1")
    metadata = evaluation.ValidationExposureMetadata(
        run_id="run_1",
        source_head="1" * 40,
        baseline_policy_sha256="d" * 64,
        candidate_identity_sha256="f" * 64,
        exposure_kind="hidden_validation",
    )
    barrier = threading.Barrier(2)

    def reserve() -> str:
        ledger = evaluation.ValidationLedger(ledger_path)
        barrier.wait(timeout=5)
        try:
            ledger.reserve_hidden(identity, metadata)
        except ValueError as exc:
            return str(exc)
        return "reserved"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _index: reserve(), range(2)))

    assert results.count("reserved") == 1
    assert sum("permanently consumed" in result for result in results) == 1
    records = ledger_path.read_text("utf-8").splitlines()
    assert len(records) == 1


def _task5_git(root: Path, *args: str, text: bool = False) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=text,
    )
    if text:
        assert isinstance(completed.stdout, str)
        return completed.stdout
    return completed.stdout.decode("utf-8")


def _task5_policy_roots(tmp_path: Path) -> tuple[Path, Path, object]:
    import agent_loop

    git_path = shutil.which("git")
    assert git_path is not None
    capability = agent_loop.configure_git_executable(Path(git_path).resolve())
    authenticated = tmp_path / "authenticated"
    authenticated.mkdir()
    sources = {
        "core/strategy_policy/entry.py": (
            "from .contracts import EntryDecision, EntrySnapshot\n\n"
            "def evaluate_entry(snapshot: EntrySnapshot) -> EntryDecision:\n"
            "    return EntryDecision(True, True, (None, None), ())\n"
        ),
        "core/strategy_policy/risk.py": (
            "from .contracts import AllocationDecision, AllocationSnapshot, CapacityDecision, CapacitySnapshot, EvictionDecision, EvictionSnapshot\n\n"
            "def recommend_capacity(snapshot: CapacitySnapshot) -> CapacityDecision:\n"
            "    return CapacityDecision(snapshot.configured_max_positions, False)\n\n"
            "def recommend_allocation(snapshot: AllocationSnapshot) -> AllocationDecision:\n"
            "    return AllocationDecision(0.01, 0.08, None)\n\n"
            "def select_eviction(snapshot: EvictionSnapshot) -> EvictionDecision:\n"
            "    return EvictionDecision(None)\n"
        ),
        "core/strategy_policy/exit.py": (
            "from .contracts import ExitDecision, ExitSnapshot\n\n"
            "def evaluate_exit(snapshot: ExitSnapshot) -> ExitDecision:\n"
            "    return ExitDecision((), None, False, 0, False, False)\n"
        ),
    }
    for relative, source in sources.items():
        target = authenticated / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8", newline="\n")
    _task5_git(authenticated, "init")
    _task5_git(authenticated, "config", "user.name", "Task Five Tests")
    _task5_git(authenticated, "config", "user.email", "task5@example.invalid")
    _task5_git(authenticated, "config", "core.autocrlf", "false")
    _task5_git(authenticated, "add", ".")
    _task5_git(authenticated, "commit", "-m", "authenticated policy")
    candidate = tmp_path / "candidate"
    shutil.copytree(authenticated, candidate)
    return authenticated, candidate, capability


def _task5_incremental_diff(candidate: Path, path: str, old: str, new: str) -> str:
    target = candidate / path
    source = target.read_text("utf-8")
    target.write_text(source.replace(old, new), encoding="utf-8", newline="\n")
    raw = _task5_git(
        candidate, "diff", "--no-ext-diff", "--no-color", "HEAD", "--", path
    )
    target.write_text(source, encoding="utf-8", newline="\n")
    assert raw
    return raw


def _task5_raw_author_diff(
    old: str,
    new: str,
    *,
    path: str = "core/strategy_policy/entry.py",
) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "index 1111111..2222222 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -4,1 +4,1 @@\n"
        f"-{old}\n"
        f"+{new}\n"
    )


def _task5_create_directory_link(target: Path, link: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except OSError as symlink_error:
        if os.name != "nt":
            pytest.skip(f"directory-link creation unavailable: {symlink_error}")
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        pytest.skip(
            "directory-link creation unavailable: "
            f"{completed.stderr or completed.stdout}"
        )


def test_candidate_identity_is_git_derived_and_author_manifest_must_match(
    tmp_path: Path,
) -> None:
    """Break caught: author-declared scope could replace controller-derived identity."""
    from core.pit_optimizer_candidate import (
        PIT_OPTIMIZER_PATCH_BOUNDS,
        validate_author_manifest,
        validate_candidate_diff,
    )

    authenticated, candidate_root, git = _task5_policy_roots(tmp_path)
    incremental = _task5_incremental_diff(
        candidate_root,
        "core/strategy_policy/entry.py",
        "return EntryDecision(True, True, (None, None), ())",
        "return EntryDecision(snapshot.market_is_bullish, True, (None, None), ())",
    )
    source_commit = _task5_git(authenticated, "rev-parse", "HEAD", text=True).strip()

    identity, cumulative_diff = validate_candidate_diff(
        authenticated_base_root=authenticated,
        candidate_root=candidate_root,
        incremental_diff=incremental,
        git=git,
        bounds=PIT_OPTIMIZER_PATCH_BOUNDS,
        source_commit=source_commit,
        policy_interface_version=1,
        immutable_constraints_sha256="a" * 64,
        discovery_manifest_sha256="b" * 64,
    )

    assert cumulative_diff == _task5_git(
        candidate_root,
        "diff",
        "--no-ext-diff",
        "--no-color",
        "HEAD",
        "--",
        *_POLICY_PATHS,
    )
    assert identity.source_commit == source_commit
    assert identity.cumulative_diff_sha256 == hashlib.sha256(
        cumulative_diff.encode("utf-8")
    ).hexdigest()
    assert identity.changed_paths == ("core/strategy_policy/entry.py",)
    assert identity.changed_symbols == (
        "core.strategy_policy.entry.evaluate_entry",
    )
    assert tuple(path for path, _digest in identity.editable_file_sha256s) == tuple(
        _POLICY_PATHS
    )

    matching = contract.AuthorArtifact.from_json(
        _canonical_text(
            {
                **_author_payload(),
                "changed_paths": list(identity.changed_paths),
                "changed_symbols": list(identity.changed_symbols),
                "unified_diff": incremental,
            }
        ),
        max_diff_bytes=64 * 1024,
        max_total_bytes=72 * 1024,
    )
    validate_author_manifest(matching, identity)
    mismatch = replace(
        matching,
        changed_paths=("core/strategy_policy/risk.py",),
        changed_symbols=("core.strategy_policy.risk.recommend_capacity",),
    )
    with pytest.raises(ValueError, match="author_manifest_mismatch"):
        validate_author_manifest(mismatch, identity)


def _task5_authenticated_identity(
    tmp_path: Path,
) -> tuple[object, contract.AuthorArtifact]:
    from core.pit_optimizer_candidate import validate_candidate_diff

    authenticated, candidate_root, git = _task5_policy_roots(tmp_path)
    incremental = _task5_incremental_diff(
        candidate_root,
        "core/strategy_policy/entry.py",
        "return EntryDecision(True, True, (None, None), ())",
        "return EntryDecision(False, True, (None, None), ())",
    )
    identity, _cumulative = validate_candidate_diff(
        authenticated_base_root=authenticated,
        candidate_root=candidate_root,
        incremental_diff=incremental,
        git=git,
        bounds=contract.PatchBounds(3, 12, 200, 64 * 1024),
        source_commit=_task5_git(authenticated, "rev-parse", "HEAD", text=True).strip(),
        policy_interface_version=1,
        immutable_constraints_sha256="a" * 64,
        discovery_manifest_sha256="b" * 64,
    )
    author = contract.AuthorArtifact.from_json(
        _canonical_text(
            {
                **_author_payload(),
                "changed_paths": list(identity.changed_paths),
                "changed_symbols": list(identity.changed_symbols),
                "unified_diff": incremental,
            }
        ),
        max_diff_bytes=64 * 1024,
        max_total_bytes=72 * 1024,
    )
    return identity, author


def test_candidate_identity_rejects_direct_unsealed_construction() -> None:
    """Break caught: a caller could construct an identity without Git/source derivation."""
    from core.pit_optimizer_candidate import CandidateIdentity

    with pytest.raises(ValueError, match="controller derived"):
        CandidateIdentity(
            source_commit="a" * 40,
            policy_interface_version=1,
            cumulative_diff_sha256="b" * 64,
            editable_file_sha256s=tuple(
                (path, "c" * 64) for path in _POLICY_PATHS
            ),
            changed_paths=("core/strategy_policy/entry.py",),
            changed_symbols=("core.strategy_policy.entry.evaluate_entry",),
            immutable_constraints_sha256="d" * 64,
            discovery_manifest_sha256="e" * 64,
            identity_sha256="f" * 64,
        )


def test_candidate_identity_has_exact_public_nine_field_json_shape(
    tmp_path: Path,
) -> None:
    """Break caught: the construction seal leaked into persisted identity data."""
    from core.pit_optimizer_candidate import CandidateIdentity

    identity, _author = _task5_authenticated_identity(tmp_path)
    expected_fields = (
        "source_commit",
        "policy_interface_version",
        "cumulative_diff_sha256",
        "editable_file_sha256s",
        "changed_paths",
        "changed_symbols",
        "immutable_constraints_sha256",
        "discovery_manifest_sha256",
        "identity_sha256",
    )
    assert tuple(item.name for item in fields(CandidateIdentity)) == expected_fields
    primitive = identity.to_primitive()
    assert tuple(primitive) == expected_fields
    assert primitive == asdict(identity)
    assert not any(key.startswith("_") for key in primitive)
    rendered = identity.to_canonical_json()
    assert rendered.endswith("\n")
    assert json.loads(rendered) == json.loads(json.dumps(primitive))
    assert "controller_seal" not in rendered


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("source_commit", "f" * 40),
        ("policy_interface_version", True),
        ("cumulative_diff_sha256", "f" * 64),
        (
            "editable_file_sha256s",
            tuple((path, "f" * 64) for path in _POLICY_PATHS),
        ),
        ("changed_paths", ("core/strategy_policy/risk.py",)),
        ("changed_symbols", ("core.strategy_policy.risk.recommend_capacity",)),
        ("immutable_constraints_sha256", "f" * 64),
        ("discovery_manifest_sha256", "f" * 64),
        ("identity_sha256", "f" * 64),
    ),
)
def test_candidate_identity_tampering_fails_before_manifest_comparison(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    """Break caught: a mutated identity was trusted when advisory scope still matched."""
    from core.pit_optimizer_candidate import validate_author_manifest

    identity, author = _task5_authenticated_identity(tmp_path)
    try:
        tampered = replace(identity, **{field: value})
    except ValueError as exc:
        assert "candidate identity" in str(exc)
        return
    with pytest.raises(ValueError, match="candidate identity"):
        validate_author_manifest(author, tampered)


def test_candidate_identity_rejects_attacker_recomputed_digest(
    tmp_path: Path,
) -> None:
    """Break caught: a copied seal plus attacker-selected fields recreated a trusted identity."""
    from core.pit_optimizer_candidate import validate_author_manifest

    identity, author = _task5_authenticated_identity(tmp_path)
    forged_fields = {
        "source_commit": "f" * 40,
        "policy_interface_version": identity.policy_interface_version,
        "cumulative_diff_sha256": identity.cumulative_diff_sha256,
        "editable_file_sha256s": identity.editable_file_sha256s,
        "changed_paths": identity.changed_paths,
        "changed_symbols": identity.changed_symbols,
        "immutable_constraints_sha256": identity.immutable_constraints_sha256,
        "discovery_manifest_sha256": identity.discovery_manifest_sha256,
    }
    recomputed = hashlib.sha256(
        json.dumps(
            forged_fields,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    ).hexdigest()
    forged = object.__new__(type(identity))
    for field_name, field_value in {
        **forged_fields,
        "identity_sha256": recomputed,
    }.items():
        object.__setattr__(forged, field_name, field_value)
    with pytest.raises(ValueError, match="candidate identity"):
        validate_author_manifest(author, forged)


def test_candidate_identity_constant_symbol_is_author_representable(
    tmp_path: Path,
) -> None:
    """Break caught: an AST-valid immutable constant could not appear in AuthorArtifact."""
    from core.pit_optimizer_candidate import (
        validate_author_manifest,
        validate_candidate_diff,
    )

    authenticated, candidate_root, git = _task5_policy_roots(tmp_path)
    incremental = _task5_incremental_diff(
        candidate_root,
        "core/strategy_policy/entry.py",
        "from .contracts import EntryDecision, EntrySnapshot",
        "ENTRY_FLOOR = 0.25\nfrom .contracts import EntryDecision, EntrySnapshot",
    )
    identity, _cumulative = validate_candidate_diff(
        authenticated_base_root=authenticated,
        candidate_root=candidate_root,
        incremental_diff=incremental,
        git=git,
        bounds=contract.PatchBounds(3, 12, 200, 64 * 1024),
        source_commit=_task5_git(authenticated, "rev-parse", "HEAD", text=True).strip(),
        policy_interface_version=1,
        immutable_constraints_sha256="a" * 64,
        discovery_manifest_sha256="b" * 64,
    )
    assert identity.changed_symbols == ("core.strategy_policy.entry.ENTRY_FLOOR",)
    author = contract.AuthorArtifact.from_json(
        _canonical_text(
            {
                **_author_payload(),
                "changed_paths": list(identity.changed_paths),
                "changed_symbols": list(identity.changed_symbols),
                "unified_diff": incremental,
            }
        ),
        max_diff_bytes=64 * 1024,
        max_total_bytes=72 * 1024,
    )
    validate_author_manifest(author, identity)


def test_candidate_identity_reverse_diff_sections_share_canonical_path_order(
    tmp_path: Path,
) -> None:
    """Break caught: reverse section order made AuthorInput and Git identity contradictory."""
    from core.pit_optimizer_candidate import (
        build_policy_source_bundle,
        validate_author_manifest,
        validate_candidate_diff,
    )

    authenticated, candidate_root, git = _task5_policy_roots(tmp_path)
    initial_bundle = build_policy_source_bundle(
        candidate_root=candidate_root,
        cumulative_diff="",
        policy_interface_version=1,
    )
    entry_diff = _task5_incremental_diff(
        candidate_root,
        "core/strategy_policy/entry.py",
        "return EntryDecision(True, True, (None, None), ())",
        "return EntryDecision(False, True, (None, None), ())",
    )
    risk_diff = _task5_incremental_diff(
        candidate_root,
        "core/strategy_policy/risk.py",
        "return CapacityDecision(snapshot.configured_max_positions, False)",
        "return CapacityDecision(None, False)",
    )
    reverse_diff = risk_diff + entry_diff
    identity, _cumulative = validate_candidate_diff(
        authenticated_base_root=authenticated,
        candidate_root=candidate_root,
        incremental_diff=reverse_diff,
        git=git,
        bounds=contract.PatchBounds(3, 12, 200, 64 * 1024),
        source_commit=_task5_git(authenticated, "rev-parse", "HEAD", text=True).strip(),
        policy_interface_version=1,
        immutable_constraints_sha256="a" * 64,
        discovery_manifest_sha256="b" * 64,
    )
    assert identity.changed_paths == (
        "core/strategy_policy/entry.py",
        "core/strategy_policy/risk.py",
    )
    author = contract.AuthorArtifact.from_json(
        _canonical_text(
            {
                **_author_payload(),
                "changed_paths": list(identity.changed_paths),
                "changed_symbols": list(identity.changed_symbols),
                "unified_diff": reverse_diff,
            }
        ),
        max_diff_bytes=64 * 1024,
        max_total_bytes=72 * 1024,
    )
    author_input = contract.AuthorInput(
        schema_version=2,
        iteration=1,
        policy_interface_version=1,
        immutable_constraint_ids=("causal_only", "no_external_io"),
        candidate_bounds=contract.PatchBounds(3, 12, 200, 64 * 1024),
        investigator=_investigator_artifact(),
        source_bundle=initial_bundle,
    )
    author_input.validate_artifact(author)
    validate_author_manifest(author, identity)


def test_candidate_identity_rejects_git_derived_cumulative_scope_growth(
    tmp_path: Path,
) -> None:
    """Break caught: individually small generations could evade cumulative bounds."""
    from core.pit_optimizer_candidate import validate_candidate_diff

    authenticated, candidate_root, git = _task5_policy_roots(tmp_path)
    risk_path = candidate_root / "core/strategy_policy/risk.py"
    risk_path.write_text(
        risk_path.read_text("utf-8").replace(
            "return CapacityDecision(snapshot.configured_max_positions, False)",
            "return CapacityDecision(snapshot.maximum_policy_positions, False)",
        ),
        encoding="utf-8",
        newline="\n",
    )
    incremental = _task5_incremental_diff(
        candidate_root,
        "core/strategy_policy/entry.py",
        "return EntryDecision(True, True, (None, None), ())",
        "return EntryDecision(False, True, (None, None), ())",
    )

    with pytest.raises(ValueError, match="max_files"):
        validate_candidate_diff(
            authenticated_base_root=authenticated,
            candidate_root=candidate_root,
            incremental_diff=incremental,
            git=git,
            bounds=contract.PatchBounds(1, 12, 200, 64 * 1024),
            source_commit=_task5_git(
                authenticated, "rev-parse", "HEAD", text=True
            ).strip(),
            policy_interface_version=1,
            immutable_constraints_sha256="a" * 64,
            discovery_manifest_sha256="b" * 64,
        )
    assert "return EntryDecision(True, True, (None, None), ())" in (
        candidate_root / "core/strategy_policy/entry.py"
    ).read_text("utf-8")


@pytest.mark.parametrize(
    ("old", "new", "message"),
    (
        (
            "return EntryDecision(True, True, (None, None), ())",
            "return EntryDecision(True, True, (None, None), ())",
            "no-op",
        ),
        (
            "return EntryDecision(False, False, (None, None), ())",
            "return EntryDecision(True, False, (None, None), ())",
            "apply",
        ),
    ),
)
def test_candidate_identity_rejects_noop_or_non_applicable_incremental_diff(
    tmp_path: Path,
    old: str,
    new: str,
    message: str,
) -> None:
    """Break caught: an unapplied author artifact could receive an executable identity."""
    from core.pit_optimizer_candidate import validate_candidate_diff

    authenticated, candidate_root, git = _task5_policy_roots(tmp_path)
    with pytest.raises(ValueError, match=message):
        validate_candidate_diff(
            authenticated_base_root=authenticated,
            candidate_root=candidate_root,
            incremental_diff=_task5_raw_author_diff(old, new),
            git=git,
            bounds=contract.PatchBounds(3, 12, 200, 64 * 1024),
            source_commit=_task5_git(
                authenticated, "rev-parse", "HEAD", text=True
            ).strip(),
            policy_interface_version=1,
            immutable_constraints_sha256="a" * 64,
            discovery_manifest_sha256="b" * 64,
        )


@pytest.mark.parametrize(
    "path",
    (
        "core/strategy_policy/contracts.py",
        "core/strategy_policy/runtime.py",
        "core/strategy_policy/worker.py",
        "core/strategy_policy/generated.py",
    ),
)
def test_candidate_identity_rejects_protected_or_generated_paths(
    tmp_path: Path,
    path: str,
) -> None:
    """Break caught: a policy diff could target protected infrastructure or generated code."""
    from core.pit_optimizer_candidate import validate_candidate_diff

    authenticated, candidate_root, git = _task5_policy_roots(tmp_path)
    with pytest.raises(ValueError, match="editable|permanently denied|outside"):
        validate_candidate_diff(
            authenticated_base_root=authenticated,
            candidate_root=candidate_root,
            incremental_diff=_task5_raw_author_diff("old", "new", path=path),
            git=git,
            bounds=contract.PatchBounds(3, 12, 200, 64 * 1024),
            source_commit=_task5_git(
                authenticated, "rev-parse", "HEAD", text=True
            ).strip(),
            policy_interface_version=1,
            immutable_constraints_sha256="a" * 64,
            discovery_manifest_sha256="b" * 64,
        )


@pytest.mark.parametrize("mode", ("120000", "160000"))
def test_candidate_identity_rejects_non_regular_index_mode(
    tmp_path: Path,
    mode: str,
) -> None:
    """Break caught: a symlink/gitlink could evade regular-source provenance."""
    from core.pit_optimizer_candidate import validate_candidate_diff

    authenticated, candidate_root, git = _task5_policy_roots(tmp_path)
    incremental = _task5_incremental_diff(
        candidate_root,
        "core/strategy_policy/entry.py",
        "return EntryDecision(True, True, (None, None), ())",
        "return EntryDecision(False, True, (None, None), ())",
    )
    commit = _task5_git(authenticated, "rev-parse", "HEAD", text=True).strip()
    object_id = (
        commit
        if mode == "160000"
        else _task5_git(
            authenticated,
            "rev-parse",
            "HEAD:core/strategy_policy/entry.py",
            text=True,
        ).strip()
    )
    _task5_git(
        candidate_root,
        "update-index",
        "--add",
        "--cacheinfo",
        f"{mode},{object_id},core/strategy_policy/entry.py",
    )

    with pytest.raises(ValueError, match="100644|regular"):
        validate_candidate_diff(
            authenticated_base_root=authenticated,
            candidate_root=candidate_root,
            incremental_diff=incremental,
            git=git,
            bounds=contract.PatchBounds(3, 12, 200, 64 * 1024),
            source_commit=commit,
            policy_interface_version=1,
            immutable_constraints_sha256="a" * 64,
            discovery_manifest_sha256="b" * 64,
        )


@pytest.mark.parametrize(
    "metadata",
    (
        "GIT binary patch\n",
        (
            "similarity index 100%\n"
            "rename from core/strategy_policy/entry.py\n"
            "rename to core/strategy_policy/risk.py\n"
        ),
        "old mode 100644\nnew mode 100755\n",
    ),
)
def test_candidate_identity_rejects_v2_structural_diff_metadata(
    tmp_path: Path,
    metadata: str,
) -> None:
    """Break caught: binary, rename, or mode metadata crossed the V2 identity boundary."""
    from core.pit_optimizer_candidate import validate_candidate_diff

    authenticated, candidate_root, git = _task5_policy_roots(tmp_path)
    incremental = _task5_raw_author_diff(
        "return EntryDecision(True, True, (None, None), ())",
        "return EntryDecision(False, True, (None, None), ())",
    ).replace("index 1111111..2222222 100644\n", metadata)
    with pytest.raises(ValueError, match="structural|binary|rename|mode"):
        validate_candidate_diff(
            authenticated_base_root=authenticated,
            candidate_root=candidate_root,
            incremental_diff=incremental,
            git=git,
            bounds=contract.PatchBounds(3, 12, 200, 64 * 1024),
            source_commit=_task5_git(
                authenticated, "rev-parse", "HEAD", text=True
            ).strip(),
            policy_interface_version=1,
            immutable_constraints_sha256="a" * 64,
            discovery_manifest_sha256="b" * 64,
        )


@pytest.mark.parametrize(
    ("field", "expected"),
    (
        ("hunks", "max_hunks"),
        ("lines", "max_changed_lines"),
        ("bytes", "max_diff_bytes"),
    ),
)
def test_candidate_identity_rejects_cumulative_hunk_line_and_byte_growth(
    tmp_path: Path,
    field: str,
    expected: str,
) -> None:
    """Break caught: an in-bounds generation could push cumulative state beyond its cap."""
    from core.pit_optimizer_candidate import validate_candidate_diff

    authenticated, candidate_root, git = _task5_policy_roots(tmp_path)
    risk = candidate_root / "core/strategy_policy/risk.py"
    risk.write_text(
        risk.read_text("utf-8").replace(
            "return CapacityDecision(snapshot.configured_max_positions, False)",
            "return CapacityDecision(None, False)",
        ),
        encoding="utf-8",
        newline="\n",
    )
    entry = candidate_root / "core/strategy_policy/entry.py"
    before_entry = entry.read_bytes()
    incremental = _task5_incremental_diff(
        candidate_root,
        "core/strategy_policy/entry.py",
        "return EntryDecision(True, True, (None, None), ())",
        "return EntryDecision(False, True, (None, None), ())",
    )
    bounds = {
        "hunks": contract.PatchBounds(3, 1, 200, 64 * 1024),
        "lines": contract.PatchBounds(3, 12, 2, 64 * 1024),
        "bytes": contract.PatchBounds(3, 12, 200, len(incremental.encode("utf-8"))),
    }[field]

    with pytest.raises(ValueError, match=expected):
        validate_candidate_diff(
            authenticated_base_root=authenticated,
            candidate_root=candidate_root,
            incremental_diff=incremental,
            git=git,
            bounds=bounds,
            source_commit=_task5_git(
                authenticated, "rev-parse", "HEAD", text=True
            ).strip(),
            policy_interface_version=1,
            immutable_constraints_sha256="a" * 64,
            discovery_manifest_sha256="b" * 64,
        )
    assert entry.read_bytes() == before_entry


@pytest.mark.parametrize("failure", (RuntimeError("fault"), KeyboardInterrupt()))
def test_candidate_identity_rolls_back_after_fault_or_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    """Break caught: post-apply faults or cancellation left candidate policy bytes changed."""
    import agent_loop
    from core.pit_optimizer_candidate import validate_candidate_diff

    authenticated, candidate_root, git = _task5_policy_roots(tmp_path)
    entry = candidate_root / "core/strategy_policy/entry.py"
    before_entry = entry.read_bytes()
    incremental = _task5_incremental_diff(
        candidate_root,
        "core/strategy_policy/entry.py",
        "return EntryDecision(True, True, (None, None), ())",
        "return EntryDecision(False, True, (None, None), ())",
    )

    def fail_after_apply(**_kwargs: object) -> str:
        raise failure

    monkeypatch.setattr(agent_loop, "derive_authenticated_cumulative_diff", fail_after_apply)
    with pytest.raises(type(failure), match="fault" if isinstance(failure, RuntimeError) else None):
        validate_candidate_diff(
            authenticated_base_root=authenticated,
            candidate_root=candidate_root,
            incremental_diff=incremental,
            git=git,
            bounds=contract.PatchBounds(3, 12, 200, 64 * 1024),
            source_commit=_task5_git(
                authenticated, "rev-parse", "HEAD", text=True
            ).strip(),
            policy_interface_version=1,
            immutable_constraints_sha256="a" * 64,
            discovery_manifest_sha256="b" * 64,
        )
    assert entry.read_bytes() == before_entry


@pytest.mark.parametrize(
    "failure",
    (RuntimeError("mutating apply fault"), KeyboardInterrupt()),
)
def test_candidate_identity_rolls_back_when_real_mutating_apply_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    """Break caught: Git could mutate bytes and raise before rollback was armed."""
    import agent_loop
    from core.pit_optimizer_candidate import (
        EDITABLE_POLICY_PATHS,
        validate_candidate_diff,
    )

    authenticated, candidate_root, git = _task5_policy_roots(tmp_path)
    before = {
        path: (candidate_root / path).read_bytes() for path in EDITABLE_POLICY_PATHS
    }
    incremental = _task5_incremental_diff(
        candidate_root,
        "core/strategy_policy/entry.py",
        "return EntryDecision(True, True, (None, None), ())",
        "return EntryDecision(False, True, (None, None), ())",
    )
    real_git = agent_loop._git
    mutating_apply_seen = False

    def mutate_then_raise(root: Path, *args: str, **kwargs: object) -> object:
        nonlocal mutating_apply_seen
        result = real_git(root, *args, **kwargs)
        if args and args[0] == "apply" and "--check" not in args:
            mutating_apply_seen = True
            assert (candidate_root / "core/strategy_policy/entry.py").read_bytes() != (
                before["core/strategy_policy/entry.py"]
            )
            raise failure
        return result

    monkeypatch.setattr(agent_loop, "_git", mutate_then_raise)
    with pytest.raises(
        type(failure),
        match="mutating apply fault" if isinstance(failure, RuntimeError) else None,
    ):
        validate_candidate_diff(
            authenticated_base_root=authenticated,
            candidate_root=candidate_root,
            incremental_diff=incremental,
            git=git,
            bounds=contract.PatchBounds(3, 12, 200, 64 * 1024),
            source_commit=_task5_git(
                authenticated, "rev-parse", "HEAD", text=True
            ).strip(),
            policy_interface_version=1,
            immutable_constraints_sha256="a" * 64,
            discovery_manifest_sha256="b" * 64,
        )
    assert mutating_apply_seen
    assert {
        path: (candidate_root / path).read_bytes() for path in EDITABLE_POLICY_PATHS
    } == before


def test_changed_symbols_use_before_after_ast() -> None:
    """Break caught: author symbol claims were trusted instead of AST comparison."""
    from core.pit_optimizer_candidate import derive_changed_symbols

    path = "core/strategy_policy/risk.py"
    before = {path: "def recommend_capacity(snapshot):\n    return None\n"}
    after = {
        path: (
            "def recommend_capacity(snapshot):\n"
            "    return snapshot.configured_max_positions\n"
        )
    }
    assert derive_changed_symbols(
        before_sources=before,
        after_sources=after,
    ) == ("core.strategy_policy.risk.recommend_capacity",)


def _task5_entry_source(body: str, *, prelude: str = "") -> str:
    return (
        "from .contracts import EntryDecision, EntrySnapshot\n"
        + prelude
        + "\ndef evaluate_entry(snapshot: EntrySnapshot) -> EntryDecision:\n"
        + "".join(f"    {line}\n" for line in body.splitlines())
    )


@pytest.mark.parametrize(
    ("source", "message"),
    (
        (_task5_entry_source("return EntryDecision(True, True, (None, None), ())", prelude="\nclass MutablePolicy:\n    pass\n"), "class"),
        (_task5_entry_source("return EntryDecision(True, True, (None, None), ())", prelude="\nVALUES = []\n"), "constant"),
        ("from .contracts import EntryDecision\n\ndef evaluate_entry(snapshot, cache=[]):\n    return EntryDecision(True, True, (None, None), ())\n", "default"),
        (_task5_entry_source("global STATE\nreturn EntryDecision(True, True, (None, None), ())"), "global"),
        (_task5_entry_source("snapshot.market_is_bullish = True\nreturn EntryDecision(True, True, (None, None), ())"), "input"),
        (_task5_entry_source("snapshot[0] = True\nreturn EntryDecision(True, True, (None, None), ())"), "input"),
        (_task5_entry_source("evaluate_entry.cache = True\nreturn EntryDecision(True, True, (None, None), ())"), "function attribute"),
        (_task5_entry_source("math.pi += 1.0\nreturn EntryDecision(True, True, (None, None), ())", prelude="\nimport math\n"), "mutation root"),
        (_task5_entry_source("EntryDecision.to_primitive = _helper\nreturn EntryDecision(True, True, (None, None), ())", prelude="\ndef _helper():\n    return None\n"), "mutation root"),
        (_task5_entry_source("cache = []\ncache = math\ncache.pi += 1.0\nreturn EntryDecision(True, True, (None, None), ())", prelude="\nimport math\n"), "mutation root"),
        (_task5_entry_source("cache = []\ncache = EntryDecision\ncache.to_primitive = _helper\nreturn EntryDecision(True, True, (None, None), ())", prelude="\ndef _helper():\n    return None\n"), "mutation root"),
        (_task5_entry_source("cache = [math]\ncache[0].pi += 1.0\nreturn EntryDecision(True, True, (None, None), ())", prelude="\nimport math\n"), "mutation root"),
        (_task5_entry_source("cache = [[]]\ncache[0].append(True)\nreturn EntryDecision(True, True, (None, None), ())"), "attribute call"),
        (_task5_entry_source("cache = []\ncache.member.append(True)\nreturn EntryDecision(True, True, (None, None), ())"), "attribute call"),
        (_task5_entry_source("value = getattr(snapshot, 'market_is_bullish')\nreturn EntryDecision(value, True, (None, None), ())"), "reflection"),
        (_task5_entry_source("return EntryDecision(True, True, (None, None), ())", prelude="\nimport time\n"), "import"),
        (_task5_entry_source("return EntryDecision(True, True, (None, None), ())", prelude="\nimport random\n"), "import"),
        (_task5_entry_source("return EntryDecision(True, True, (None, None), ())", prelude="\nimport os\n"), "import"),
        (_task5_entry_source("handle = open('x')\nreturn EntryDecision(True, True, (None, None), ())"), "call"),
        (_task5_entry_source("module = __import__('os')\nreturn EntryDecision(True, True, (None, None), ())"), "call"),
        (_task5_entry_source("value = eval('1')\nreturn EntryDecision(True, True, (None, None), ())"), "call"),
        ("from .contracts import EntryDecision\n\nasync def evaluate_entry(snapshot):\n    return EntryDecision(True, True, (None, None), ())\n", "async"),
        (_task5_entry_source("yield snapshot\nreturn EntryDecision(True, True, (None, None), ())"), "generator"),
        (_task5_entry_source("return EntryDecision(True, True, (None, None), ())", prelude="\ndef public_helper():\n    return True\n"), "public"),
        (_task5_entry_source("return EntryDecision(True, True, (None, None), ())", prelude="\nfrom .contracts import _CanonicalContract\n"), "contract import"),
        ("FLOOR = 0.25\nfrom __future__ import annotations\nfrom .contracts import EntryDecision\n\ndef evaluate_entry(snapshot):\n    return EntryDecision(True, True, (None, None), ())\n", "syntax"),
        (_task5_entry_source("selected = False\nfor value in {'first', 'second'}:\n    selected = bool(value)\n    break\nreturn EntryDecision(selected, True, (None, None), ())"), "unordered"),
    ),
)
def test_ast_purity_rejects_non_closed_policy_behavior(
    source: str,
    message: str,
) -> None:
    """Break caught: candidate code could retain state or reach ambient capabilities."""
    from core.pit_optimizer_candidate import validate_policy_ast

    with pytest.raises(ValueError, match=message):
        validate_policy_ast(path="core/strategy_policy/entry.py", source=source)


def test_ast_purity_accepts_current_policy_and_immutable_literal_constants() -> None:
    """Break caught: the closed validator rejected the authenticated baseline policy."""
    from core.pit_optimizer_candidate import validate_policy_ast

    root = Path(__file__).parents[1]
    for path in _POLICY_PATHS:
        source = (root / path).read_text("utf-8")
        if path == "core/strategy_policy/entry.py":
            source = source.replace(
                "from __future__ import annotations\n",
                "from __future__ import annotations\nFLOORS = (0.25, 70.0, None, 'entry')\n",
                1,
            )
        validate_policy_ast(path=path, source=source)


def _task5_feedback(iteration: int) -> contract.IterationFeedbackSummary:
    return contract.IterationFeedbackSummary(
        iteration=iteration,
        hypothesis_id=f"hypothesis_{iteration}",
        family="entry",
        author_summary="bounded author summary",
        validation_code="valid",
        discovery_score=None,
        critic_disposition="refine",
        critic_next_direction="bounded next direction",
        incumbent_changed=False,
    )


def _task5_investigator_input(
    *,
    iteration: int,
    prior_iterations: tuple[contract.IterationFeedbackSummary, ...],
) -> contract.InvestigatorInput:
    return contract.InvestigatorInput(
        schema_version=2,
        iteration=iteration,
        policy_interface_version=1,
        immutable_constraint_ids=("causal_only", "no_external_io"),
        candidate_bounds=contract.PatchBounds(3, 12, 80, 8 * 1024),
        rule_summary=contract.StrategyRuleSummary(
            records=(
                contract.RuleSummaryRecord(
                    "rule.entry",
                    "Use causal entry inputs.",
                ),
            )
        ),
        source_bundle=_source_bundle(),
        baseline_discovery=_discovery_summary(),
        incumbent_summary=contract.IncumbentSummary(
            candidate_identity_sha256=None,
            accepted_iteration=None,
            behavioral_summary="Authenticated fixed baseline.",
            discovery=_discovery_summary(),
        ),
        prior_iterations=prior_iterations,
    )


@pytest.mark.parametrize(
    ("iteration", "history"),
    (
        (2, ()),
        (2, (_task5_feedback(2),)),
        (3, (_task5_feedback(1),)),
        (3, (_task5_feedback(2), _task5_feedback(1))),
        (1, (_task5_feedback(1),)),
    ),
)
def test_source_context_requires_exact_contiguous_feedback_lineage(
    iteration: int,
    history: tuple[contract.IterationFeedbackSummary, ...],
) -> None:
    """Break caught: a caller could silently omit, reorder, or relabel prior feedback."""
    with pytest.raises(ValueError, match="contiguous"):
        _task5_investigator_input(
            iteration=iteration,
            prior_iterations=history,
        )


def test_source_context_accepts_exact_general_feedback_prefix() -> None:
    """Break caught: lineage hardening accidentally limited the general eight-summary contract."""
    role_input = _task5_investigator_input(
        iteration=5,
        prior_iterations=tuple(_task5_feedback(index) for index in range(1, 5)),
    )
    assert tuple(item.iteration for item in role_input.prior_iterations) == (1, 2, 3, 4)


def test_source_context_fit_measures_complete_canonical_unicode_role_input() -> None:
    """Break caught: preflight measured two fields and ASCII escaping instead of the role contract."""
    from core.pit_optimizer_candidate import require_source_context_fit

    unicode_feedback = replace(
        _task5_feedback(1),
        author_summary="Réglage causal mesuré.",
    )
    role_input = _task5_investigator_input(
        iteration=2,
        prior_iterations=(unicode_feedback,),
    )
    rendered = role_input.canonical_json_bytes()
    assert "Réglage".encode("utf-8") in rendered
    static_bytes = len(
        contract.PIT_OPTIMIZER_V2_SYSTEM_PROMPTS["investigator"].encode("utf-8")
    ) + len(
        json.dumps(
            contract.pit_optimizer_response_format("investigator"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    )
    exact = contract.PitOptimizerCallBudget(
        call_index=4,
        iteration=2,
        role="investigator",
        model="deepseek/deepseek-r1",
        max_static_input_bytes=static_bytes,
        max_dynamic_input_bytes=len(rendered),
        max_input_tokens=static_bytes + len(rendered),
        max_output_tokens=1,
        max_response_bytes=1,
        max_usd=0.01,
    )
    assert require_source_context_fit(role_input=role_input, role_budget=exact) == rendered
    too_small = replace(
        exact,
        max_dynamic_input_bytes=len(rendered) - 1,
        max_input_tokens=static_bytes + len(rendered) - 1,
    )
    with pytest.raises(ValueError, match="context_budget_exhausted"):
        require_source_context_fit(role_input=role_input, role_budget=too_small)


def test_source_bundle_contains_only_complete_current_policy_context(
    tmp_path: Path,
) -> None:
    """Break caught: source packaging leaked unrelated repository or local material."""
    from core.pit_optimizer_candidate import build_policy_source_bundle

    _authenticated, candidate_root, _git = _task5_policy_roots(tmp_path)
    (candidate_root / "credentials.env").write_text("TOKEN=forbidden\n", encoding="utf-8")
    (candidate_root / "trades.json").write_text('[{"symbol":"SECRET"}]', encoding="utf-8")
    unrelated = candidate_root / "core" / "unrelated.py"
    unrelated.write_text("LOCAL_PATH = 'C:/private/data'\n", encoding="utf-8")

    bundle = build_policy_source_bundle(
        candidate_root=candidate_root,
        cumulative_diff="",
        policy_interface_version=1,
    )

    assert tuple(record.path for record in bundle.files) == _POLICY_PATHS
    assert all(record.text == (candidate_root / record.path).read_text("utf-8") for record in bundle.files)
    assert all(record.sha256 == hashlib.sha256(record.text.encode("utf-8")).hexdigest() for record in bundle.files)
    assert bundle.files[0].declared_symbols == (
        "core.strategy_policy.entry.evaluate_entry",
    )
    assert bundle.files[1].declared_symbols == (
        "core.strategy_policy.risk.recommend_capacity",
        "core.strategy_policy.risk.recommend_allocation",
        "core.strategy_policy.risk.select_eviction",
    )
    rendered = bundle.canonical_json_bytes()
    assert len(rendered) <= 64 * 1024
    for forbidden in (b"TOKEN", b"SECRET", b"C:/private", b"credentials.env", b"trades.json"):
        assert forbidden not in rendered


def test_source_bundle_rejects_intermediate_policy_directory_link(
    tmp_path: Path,
) -> None:
    """Break caught: a linked ``core`` directory escaped the candidate provenance root."""
    from core.pit_optimizer_candidate import build_policy_source_bundle

    source = Path(__file__).parents[1] / "core" / "strategy_policy"
    outside_core = tmp_path / "outside-core"
    outside_policy = outside_core / "strategy_policy"
    outside_policy.mkdir(parents=True)
    for name in ("entry.py", "risk.py", "exit.py"):
        shutil.copyfile(source / name, outside_policy / name)
    candidate = tmp_path / "linked-candidate"
    candidate.mkdir()
    _task5_create_directory_link(outside_core, candidate / "core")

    with pytest.raises(ValueError, match="link|provenance"):
        build_policy_source_bundle(
            candidate_root=candidate,
            cumulative_diff="",
            policy_interface_version=1,
        )


def test_next_context_oversize_is_candidate_attributable_and_never_truncated(
    tmp_path: Path,
) -> None:
    """Break caught: an oversized next incumbent could be truncated into a different policy."""
    from core.pit_optimizer_candidate import build_policy_source_bundle

    _authenticated, candidate_root, _git = _task5_policy_roots(tmp_path)
    entry = candidate_root / "core/strategy_policy/entry.py"
    original = entry.read_text("utf-8")
    entry.write_text(original + ("# candidate padding\n" * 4_000), encoding="utf-8")

    with pytest.raises(ValueError, match="next_context_oversize"):
        build_policy_source_bundle(
            candidate_root=candidate_root,
            cumulative_diff="candidate cumulative diff",
            policy_interface_version=1,
        )
    assert entry.read_text("utf-8").endswith("# candidate padding\n")


def test_source_context_fit_preserves_all_feedback_and_enforces_precall_budget(
) -> None:
    """Break caught: later calls silently dropped history or exceeded their sealed input cap."""
    from core.pit_optimizer_candidate import require_source_context_fit

    role_input = _task5_investigator_input(
        iteration=2,
        prior_iterations=(_task5_feedback(1),),
    )
    iteration_two = next(
        budget
        for budget in _call_budgets()
        if budget.role == "investigator" and budget.iteration == 2
    )
    require_source_context_fit(
        role_input=role_input,
        role_budget=iteration_two,
    )

    tiny = contract.PitOptimizerCallBudget(
        call_index=4,
        iteration=2,
        role="investigator",
        model="deepseek/deepseek-r1",
        max_static_input_bytes=1,
        max_dynamic_input_bytes=1,
        max_input_tokens=2,
        max_output_tokens=1,
        max_response_bytes=1,
        max_usd=0.01,
    )
    with pytest.raises(ValueError, match="context_budget_exhausted"):
        require_source_context_fit(
            role_input=role_input,
            role_budget=tiny,
        )


def test_worker_protocol_bootstrap_has_closed_shape_and_fresh_key_material() -> None:
    """Break caught: short, reusable, or extensible bootstrap secrets weakened framing."""
    from core.strategy_policy.worker import WorkerBootstrap

    first = WorkerBootstrap.create(interface_version=1)
    second = WorkerBootstrap.create(interface_version=1)
    assert first.schema_version == 1
    assert len(base64.b64decode(first.nonce_b64, validate=True)) == 16
    assert len(base64.b64decode(first.hmac_key_b64, validate=True)) == 32
    assert (first.nonce_b64, first.hmac_key_b64) != (
        second.nonce_b64,
        second.hmac_key_b64,
    )
    assert WorkerBootstrap.from_json(first.to_json()) == first
    duplicate = first.to_json()[:-1] + f',"schema_version":{first.schema_version}}}'
    with pytest.raises(ValueError, match="duplicate"):
        WorkerBootstrap.from_json(duplicate)
    with pytest.raises(ValueError, match="fields"):
        WorkerBootstrap.from_json(first.to_json()[:-1] + ',"extra":true}')
    with pytest.raises(ValueError, match="nonce"):
        WorkerBootstrap(
            schema_version=1,
            interface_version=1,
            nonce_b64=base64.b64encode(b"short").decode("ascii"),
            hmac_key_b64=first.hmac_key_b64,
        )
    for scalar in (True, 1.0):
        malformed = json.loads(first.to_json())
        malformed["schema_version"] = scalar
        with pytest.raises(ValueError, match="schema"):
            WorkerBootstrap.from_json(_canonical_text(malformed))


@pytest.mark.parametrize("sequence", (True, 1.0))
def test_worker_protocol_rejects_reauthenticated_non_integer_sequences(
    sequence: object,
) -> None:
    """Break caught: JSON booleans/floats compared equal to the expected integer sequence."""
    from core.strategy_policy.contracts import CapacityDecision, CapacitySnapshot
    from core.strategy_policy.worker import (
        PolicyRequestEnvelope,
        WorkerBootstrap,
        decode_policy_request,
        decode_policy_response,
        encode_policy_request,
        encode_policy_response,
        initial_chain_sha256,
    )

    bootstrap = WorkerBootstrap.create(interface_version=1)
    request_line, request = encode_policy_request(
        bootstrap=bootstrap,
        sequence=1,
        previous_hmac_sha256=initial_chain_sha256(bootstrap),
        method="recommend_capacity",
        snapshot=CapacitySnapshot(None, 25, 0, 3, 1.0, False),
    )

    def reauthenticate(raw: str, replacement: object) -> str:
        value = json.loads(raw)
        value["sequence"] = replacement
        unsigned = {key: item for key, item in value.items() if key != "hmac_sha256"}
        canonical = json.dumps(
            unsigned,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        value["hmac_sha256"] = hmac.new(
            base64.b64decode(bootstrap.hmac_key_b64, validate=True),
            base64.b64decode(bootstrap.nonce_b64, validate=True) + b"\n" + canonical,
            hashlib.sha256,
        ).hexdigest()
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    with pytest.raises(ValueError, match="sequence"):
        decode_policy_request(
            reauthenticate(request_line, sequence),
            bootstrap=bootstrap,
            expected_sequence=1,
            expected_previous_hmac_sha256=initial_chain_sha256(bootstrap),
        )
    response_line, _response = encode_policy_response(
        bootstrap=bootstrap,
        sequence=1,
        request_hmac_sha256=request.hmac_sha256,
        method="recommend_capacity",
        decision=CapacityDecision(None, False),
    )
    with pytest.raises(ValueError, match="sequence"):
        decode_policy_response(
            reauthenticate(response_line, sequence),
            bootstrap=bootstrap,
            expected_sequence=1,
            expected_request_hmac_sha256=request.hmac_sha256,
            expected_method="recommend_capacity",
        )
    with pytest.raises(ValueError, match="sequence"):
        PolicyRequestEnvelope(
            sequence=sequence,  # type: ignore[arg-type]
            previous_hmac_sha256=request.previous_hmac_sha256,
            method=request.method,
            payload_sha256=request.payload_sha256,
            payload=request.payload,
            hmac_sha256=request.hmac_sha256,
        )


def test_worker_protocol_authenticates_hashes_sequence_chain_and_response_binding() -> None:
    """Break caught: a replayed/tampered causal snapshot or decision could be accepted."""
    from core.strategy_policy.contracts import CapacityDecision, CapacitySnapshot
    from core.strategy_policy.worker import (
        WorkerBootstrap,
        decode_policy_request,
        decode_policy_response,
        encode_policy_request,
        encode_policy_response,
        initial_chain_sha256,
    )

    bootstrap = WorkerBootstrap.create(interface_version=1)
    snapshot = CapacitySnapshot(None, 25, 0, 3, 1.0, False)
    request_line, request = encode_policy_request(
        bootstrap=bootstrap,
        sequence=1,
        previous_hmac_sha256=initial_chain_sha256(bootstrap),
        method="recommend_capacity",
        snapshot=snapshot,
    )
    decoded_request, decoded_snapshot = decode_policy_request(
        request_line,
        bootstrap=bootstrap,
        expected_sequence=1,
        expected_previous_hmac_sha256=initial_chain_sha256(bootstrap),
    )
    assert decoded_request == request
    assert decoded_snapshot == snapshot
    assert request.payload_sha256 == hashlib.sha256(
        snapshot.to_canonical_json().encode("utf-8")
    ).hexdigest()

    tampered = json.loads(request_line)
    tampered["payload"]["eligible_signal_count"] = 4
    with pytest.raises(ValueError, match="payload hash"):
        decode_policy_request(
            _canonical_text(tampered),
            bootstrap=bootstrap,
            expected_sequence=1,
            expected_previous_hmac_sha256=initial_chain_sha256(bootstrap),
        )
    with pytest.raises(ValueError, match="sequence"):
        decode_policy_request(
            request_line,
            bootstrap=bootstrap,
            expected_sequence=2,
            expected_previous_hmac_sha256=initial_chain_sha256(bootstrap),
        )
    with pytest.raises(ValueError, match="chain"):
        decode_policy_request(
            request_line,
            bootstrap=bootstrap,
            expected_sequence=1,
            expected_previous_hmac_sha256="0" * 64,
        )

    response_line, response = encode_policy_response(
        bootstrap=bootstrap,
        sequence=1,
        request_hmac_sha256=request.hmac_sha256,
        method="recommend_capacity",
        decision=CapacityDecision(None, False),
    )
    decoded_response, decoded_decision = decode_policy_response(
        response_line,
        bootstrap=bootstrap,
        expected_sequence=1,
        expected_request_hmac_sha256=request.hmac_sha256,
        expected_method="recommend_capacity",
    )
    assert decoded_response == response
    assert decoded_decision == CapacityDecision(None, False)
    with pytest.raises(ValueError, match="request binding"):
        decode_policy_response(
            response_line,
            bootstrap=bootstrap,
            expected_sequence=1,
            expected_request_hmac_sha256="0" * 64,
            expected_method="recommend_capacity",
        )


def test_worker_protocol_rejects_malformed_oversized_or_mispaired_lines() -> None:
    """Break caught: permissive JSON or method pairing widened the worker API."""
    from core.strategy_policy.contracts import CapacitySnapshot
    from core.strategy_policy.worker import (
        POLICY_METHODS,
        WorkerBootstrap,
        decode_policy_request,
        encode_policy_request,
        initial_chain_sha256,
    )

    bootstrap = WorkerBootstrap.create(interface_version=1)
    snapshot = CapacitySnapshot(None, 25, 0, 3, 1.0, False)
    assert POLICY_METHODS == (
        "evaluate_entry",
        "recommend_capacity",
        "recommend_allocation",
        "select_eviction",
        "evaluate_exit",
    )
    with pytest.raises(ValueError, match="pairing"):
        encode_policy_request(
            bootstrap=bootstrap,
            sequence=1,
            previous_hmac_sha256=initial_chain_sha256(bootstrap),
            method="evaluate_entry",
            snapshot=snapshot,
        )
    with pytest.raises(ValueError, match="method"):
        encode_policy_request(
            bootstrap=bootstrap,
            sequence=1,
            previous_hmac_sha256=initial_chain_sha256(bootstrap),
            method="unknown",
            snapshot=snapshot,
        )
    for raw, message in (
        ("x" * (16 * 1024 + 1), "line limit"),
        ("not-json", "malformed"),
        ('{"sequence":1,"sequence":1}', "duplicate"),
    ):
        with pytest.raises(ValueError, match=message):
            decode_policy_request(
                raw,
                bootstrap=bootstrap,
                expected_sequence=1,
                expected_previous_hmac_sha256=initial_chain_sha256(bootstrap),
            )


def test_worker_determinism_observation_state_is_explicitly_bounded() -> None:
    """Break caught: adversarial distinct snapshots grew controller state without limit."""
    from core.strategy_policy.contracts import CapacityDecision, CapacitySnapshot
    from core.strategy_policy.worker import DecisionDeterminismGuard

    guard = DecisionDeterminismGuard(max_observations=2)
    for count in range(3):
        guard.observe(
            "recommend_capacity",
            CapacitySnapshot(None, 25, count, 3, 1.0, False),
            CapacityDecision(None, False),
        )
    assert guard.observed_count == 2


def test_worker_protocol_json_line_policy_client_dispatches_and_closes_once() -> None:
    """Break caught: the evaluator adapter omitted a method or leaked a worker session."""
    from core.strategy_policy.contracts import (
        AllocationDecision,
        AllocationSnapshot,
        CapacityDecision,
        CapacitySnapshot,
        EntryDecision,
        EntrySnapshot,
        EvictionDecision,
        EvictionSnapshot,
        ExitDecision,
        ExitSnapshot,
    )
    from core.strategy_policy.runtime import JsonLinePolicyClient

    decisions = {
        "evaluate_entry": EntryDecision(True, True, (None, None), ()),
        "recommend_capacity": CapacityDecision(None, False),
        "recommend_allocation": AllocationDecision(0.01, 0.08, None),
        "select_eviction": EvictionDecision(None),
        "evaluate_exit": ExitDecision((), None, False, 0, False, False),
    }

    class RecordingSession:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []
            self.closed = 0

        def call(self, method: str, snapshot: object) -> object:
            self.calls.append((method, snapshot))
            return decisions[method]

        def close(self) -> None:
            self.closed += 1

    snapshots = (
        object.__new__(EntrySnapshot),
        object.__new__(CapacitySnapshot),
        object.__new__(AllocationSnapshot),
        object.__new__(EvictionSnapshot),
        object.__new__(ExitSnapshot),
    )
    session = RecordingSession()
    client = JsonLinePolicyClient(session=session, interface_version=1)
    assert client.evaluate_entry(snapshots[0]) == decisions["evaluate_entry"]
    assert client.recommend_capacity(snapshots[1]) == decisions["recommend_capacity"]
    assert client.recommend_allocation(snapshots[2]) == decisions["recommend_allocation"]
    assert client.select_eviction(snapshots[3]) == decisions["select_eviction"]
    assert client.evaluate_exit(snapshots[4]) == decisions["evaluate_exit"]
    assert [method for method, _snapshot in session.calls] == list(decisions)
    client.close()
    client.close()
    assert session.closed == 1
