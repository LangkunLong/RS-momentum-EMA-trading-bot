"""Standalone, fixture-only checks for the Issue 70 v9 resource contract.

This file is not imported by production code and does not read repository or
SEC inputs. It checks counting/serialization arithmetic on tiny synthetic
objects; its values are not production measurements or resource estimates.
Run with: python -B docs/issue-70-v9-resource-fixture-check.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import zlib
from collections.abc import Mapping, Sequence
from typing import Any


def comparison_canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def financial_summary_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _minimal_slot() -> dict[str, Any]:
    return {
        "ticker": "",
        "session_date": "",
        "feature_id": "",
        "slot_metric": "",
        "slot_number": "",
        "expected_period_end": "",
        "current": {
            "status": "",
            "missing_reason": "",
            "source_metric": "",
            "matched_period_end": "",
            "comparison_period_end": "",
            "lookback_stage": "",
            "calculation_stage": "",
        },
        "v8_source_window_projection": {
            "status": "",
            "missing_reason": "",
            "source_metric": "",
            "family": "",
            "matched_period_end": "",
            "comparison_period_end": "",
        },
        "classification": "",
        "classification_reason": "",
        "origin_evidence": {
            "current_slot_origin_ids": [],
            "baseline_selected_origin_ids": [],
            "window_candidates_by_origin_id": {},
        },
    }


def _minimal_summary_value(slot_count: int) -> dict[str, Any]:
    by_slot_id = {
        f"{index:064x}": _minimal_slot()
        for index in range(slot_count)
    }
    return {
        "v8_source_window_comparison": {"by_slot_id": by_slot_id},
    }


def _minimal_pretty_summary(slot_count: int) -> bytes:
    return financial_summary_bytes(_minimal_summary_value(slot_count))


def _logical_record_count(
    coverage_rows: Sequence[Mapping[str, Any]],
    comparison: Mapping[str, Any],
    annual_gap_rows: Sequence[Mapping[str, Any]],
) -> int:
    slots = comparison["by_slot_id"]
    decisions = sum(
        len(slot["origin_evidence"]["window_candidates_by_origin_id"])
        for slot in slots.values()
    )
    return len(coverage_rows) + len(slots) + decisions + len(annual_gap_rows)


def _envelope_for_fixture(comparison: Mapping[str, Any]) -> dict[str, Any]:
    decoded = comparison_canonical_bytes(comparison)
    payload = zlib.compress(decoded, level=9)
    return {
        "representation": "issue70-v9-comparison-envelope",
        "representation_schema_version": 1,
        "logical_schema_version": 1,
        "encoding": "base64",
        "codec": "zlib-rfc1950-deflate",
        "decoded_canonical_byte_length": len(decoded),
        "decoded_canonical_sha256": hashlib.sha256(decoded).hexdigest(),
        "payload_byte_length": len(payload),
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "payload_base64": base64.b64encode(payload).decode("ascii"),
    }


def _decode_fixture_envelope(envelope: Mapping[str, Any]) -> dict[str, Any]:
    payload = base64.b64decode(envelope["payload_base64"], validate=True)
    assert len(payload) == envelope["payload_byte_length"]
    assert hashlib.sha256(payload).hexdigest() == envelope["payload_sha256"]
    decoded = zlib.decompress(payload)
    assert len(decoded) == envelope["decoded_canonical_byte_length"]
    assert hashlib.sha256(decoded).hexdigest() == envelope["decoded_canonical_sha256"]
    value = json.loads(decoded.decode("utf-8"))
    assert comparison_canonical_bytes(value) == decoded
    return value


def _preflight_fixture(
    *, planned_records: int, planned_bytes: int, record_limit: int, byte_limit: int,
) -> None:
    if planned_records > record_limit:
        raise ValueError("fixture record budget exceeded")
    if planned_bytes > byte_limit:
        raise ValueError("fixture byte budget exceeded")


def _run_fixture_plan(
    *, planned_records: int, planned_bytes: int, record_limit: int, byte_limit: int,
    materialize: Any, stage: Any,
) -> None:
    _preflight_fixture(
        planned_records=planned_records,
        planned_bytes=planned_bytes,
        record_limit=record_limit,
        byte_limit=byte_limit,
    )
    materialize()
    stage()


def main() -> None:
    # Tiny logical evidence fixture: five coverage rows, two slots, three
    # per-origin decisions, and one gap diagnostic.
    comparison = {
        "schema_version": 1,
        "label": "v8_source_window_projection_not_regenerated_v8",
        "by_slot_id": {
            "a" * 64: {
                **_minimal_slot(),
                "origin_evidence": {
                    "current_slot_origin_ids": ["origin-current"],
                    "baseline_selected_origin_ids": ["origin-baseline"],
                    "window_candidates_by_origin_id": {
                        "origin-current": "admitted_visible",
                        "origin-baseline": "excluded_outside_window",
                    },
                },
            },
            "b" * 64: {
                **_minimal_slot(),
                "origin_evidence": {
                    "current_slot_origin_ids": [],
                    "baseline_selected_origin_ids": [],
                    "window_candidates_by_origin_id": {
                        "origin-gap": "admitted_not_yet_public",
                    },
                },
            },
        },
    }
    coverage_rows = [{"record_kind": "field_session"} for _ in range(5)]
    annual_gap_rows = [{"slot_id": "gap-slot"}]
    logical_records = _logical_record_count(coverage_rows, comparison, annual_gap_rows)
    assert logical_records == 11

    plain_summary = {
        "annual_fiscal_year_gap_slots": annual_gap_rows,
        "v8_source_window_comparison": comparison,
    }
    plain_logical_summary_bytes = financial_summary_bytes(plain_summary)
    fixture_csv = b"record_kind,slot_id\nfield_session,slot-a\n"
    plain_logical_evidence_bytes = len(fixture_csv) + len(plain_logical_summary_bytes)

    # Exercise the a47 envelope as a synthetic round trip. Counting occurs on
    # the decoded logical comparison; the envelope itself is never a record.
    envelope = _envelope_for_fixture(comparison)
    decoded = _decode_fixture_envelope(envelope)
    assert decoded == comparison
    envelope_summary = {
        "annual_fiscal_year_gap_slots": annual_gap_rows,
        "v8_source_window_comparison": envelope,
    }
    decoded_envelope_summary = {
        "annual_fiscal_year_gap_slots": annual_gap_rows,
        "v8_source_window_comparison": decoded,
    }
    assert _logical_record_count(coverage_rows, decoded, annual_gap_rows) == logical_records
    assert len(fixture_csv) + len(financial_summary_bytes(decoded_envelope_summary)) == plain_logical_evidence_bytes
    stored_plain_bytes = len(financial_summary_bytes(plain_summary))
    stored_envelope_bytes = len(financial_summary_bytes(envelope_summary))
    assert stored_plain_bytes > 0 and stored_envelope_bytes > 0

    # Prove the fixture preflight rejects before either expensive callback is
    # invoked. This models an acceptance requirement; it is not production
    # enforcement because no production code is imported or modified here.
    calls = {"materialize": 0, "stage": 0}

    def materialize() -> None:
        calls["materialize"] += 1

    def stage() -> None:
        calls["stage"] += 1

    try:
        _run_fixture_plan(
            planned_records=logical_records,
            planned_bytes=plain_logical_evidence_bytes,
            record_limit=logical_records - 1,
            byte_limit=plain_logical_evidence_bytes,
            materialize=materialize,
            stage=stage,
        )
    except ValueError as exc:
        assert "record" in str(exc)
    else:
        raise AssertionError("over-budget record plan was not rejected")
    assert calls == {"materialize": 0, "stage": 0}

    try:
        _run_fixture_plan(
            planned_records=logical_records,
            planned_bytes=plain_logical_evidence_bytes,
            record_limit=logical_records,
            byte_limit=plain_logical_evidence_bytes - 1,
            materialize=materialize,
            stage=stage,
        )
    except ValueError as exc:
        assert "byte" in str(exc)
    else:
        raise AssertionError("over-budget byte plan was not rejected")
    assert calls == {"materialize": 0, "stage": 0}

    # Static lower-bound arithmetic from retained calendar/security metadata
    # and the fixed source contract. 214 bytes deliberately omits other
    # non-empty CSV fields, so it remains a conservative per-row floor.
    fixed_slots_per_eligible_session = 4 + 2 + (2 * (3 + 4)) + 3 + 1
    assert fixed_slots_per_eligible_session == 24
    minimum_csv_row_bytes = 39 + 13 + 64 + 1 + 10 + 21 + 10 + 26 + 1 + 29
    assert minimum_csv_row_bytes == 214
    eligible_security_sessions = 4_652
    grid_cells = 6_032
    field_rows_per_cell = 7
    minimum_slots = eligible_security_sessions * fixed_slots_per_eligible_session
    minimum_field_rows = grid_cells * field_rows_per_cell
    minimum_csv_rows = minimum_slots + minimum_field_rows
    minimum_csv_bytes = minimum_csv_rows * minimum_csv_row_bytes
    assert (minimum_slots, minimum_field_rows, minimum_csv_rows, minimum_csv_bytes) == (
        111_648, 42_224, 153_872, 32_928_608,
    )

    # One/two-slot fixtures verify both serializers' per-slot increments. The
    # affine intercept for N >= 1 is one-slot length minus one increment.
    pretty_one = len(_minimal_pretty_summary(1))
    pretty_per_slot_increment = len(_minimal_pretty_summary(2)) - pretty_one
    compact_one = len(comparison_canonical_bytes(_minimal_summary_value(1)))
    compact_per_slot_increment = (
        len(comparison_canonical_bytes(_minimal_summary_value(2))) - compact_one
    )
    assert (pretty_one, pretty_per_slot_increment) == (1_055, 987)
    assert (compact_one, compact_per_slot_increment) == (691, 643)
    pretty_nonempty_map_intercept = pretty_one - pretty_per_slot_increment
    assert pretty_nonempty_map_intercept == 68
    minimum_pretty_comparison_bytes = pretty_one + (minimum_slots - 1) * pretty_per_slot_increment
    minimum_compact_comparison_bytes = compact_one + (minimum_slots - 1) * compact_per_slot_increment
    minimum_canonical_evidence_bytes = minimum_pretty_comparison_bytes + minimum_csv_bytes
    assert (
        minimum_compact_comparison_bytes,
        minimum_pretty_comparison_bytes,
        minimum_canonical_evidence_bytes,
    ) == (
        71_789_712, 110_196_644, 143_125_252,
    )
    assert minimum_canonical_evidence_bytes - (128 * 1024 * 1024) == 8_907_524
    print(f"fixture_logical_records={logical_records}")
    print(f"fixture_round_trip=exact; logical_records_preserved={logical_records}")
    print(f"fixture_stored_summary_bytes_plain={stored_plain_bytes}")
    print(f"fixture_stored_summary_bytes_envelope={stored_envelope_bytes}")
    print(f"fixed_slots_per_eligible_session={fixed_slots_per_eligible_session}")
    print(f"minimum_csv_row_bytes={minimum_csv_row_bytes}")
    print(f"minimum_csv_rows={minimum_csv_rows}")
    print(f"minimum_csv_bytes={minimum_csv_bytes}")
    print(f"pretty_comparison_one_slot_bytes={pretty_one}")
    print(f"pretty_comparison_per_additional_slot_bytes={pretty_per_slot_increment}")
    print(f"pretty_nonempty_map_intercept_bytes={pretty_nonempty_map_intercept}")
    print(f"compact_comparison_one_slot_bytes={compact_one}")
    print(f"compact_comparison_per_additional_slot_bytes={compact_per_slot_increment}")
    print(f"minimum_compact_comparison_bytes={minimum_compact_comparison_bytes}")
    print(f"minimum_pretty_comparison_bytes={minimum_pretty_comparison_bytes}")
    print(f"minimum_canonical_evidence_bytes={minimum_canonical_evidence_bytes}")
    print(f"canonical_cap_shortfall_bytes={minimum_canonical_evidence_bytes - (128 * 1024 * 1024)}")


if __name__ == "__main__":
    main()
