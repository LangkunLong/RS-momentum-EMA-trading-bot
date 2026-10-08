from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from core.pit_corporate_actions import (
    project_corporate_actions_as_of,
    validate_corporate_action_rows,
)


FIXTURES = Path(__file__).parent / "fixtures" / "pit" / "corporate_actions"
LINEAGES = frozenset({"alpha_inc", "beta_inc", "legacy_co", "successor_co"})
SESSIONS = tuple(
    date.fromisoformat(value)
    for value in (
        "2024-01-02",
        "2024-01-03",
        "2024-01-04",
        "2024-01-05",
        "2024-01-08",
        "2024-01-09",
        "2024-01-10",
        "2024-01-11",
        "2024-01-12",
        "2024-01-20",
        "2024-01-22",
        "2024-02-01",
        "2024-02-02",
        "2024-02-05",
        "2024-02-06",
        "2024-02-15",
        "2024-02-20",
        "2024-02-21",
    )
)


def load_rows(name: str) -> list[dict[str, Any]]:
    path = FIXTURES / name
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def validate(name: str):
    return validate_corporate_action_rows(
        load_rows(name),
        authenticated_security_lineages=LINEAGES,
    )


def project(name: str, *, session: str, lineage: str):
    batch = validate(name)
    assert batch.findings == ()
    return project_corporate_actions_as_of(
        batch.records,
        decision_session=date.fromisoformat(session),
        exchange_sessions=SESSIONS,
        security_lineage_id=lineage,
    )


def test_split_facts_round_trip_without_adjustment_policy():
    result = validate("split-known.jsonl")
    assert result.findings == ()
    [record] = result.records
    row = record.to_row()
    assert row["facts"]["split_numerator"] == 2
    assert row["facts"]["split_denominator"] == 1
    assert row["effective_date"] == "2024-01-10"
    assert row["revision_public_at"] == "2024-01-02T23:30:00-10:00"
    assert not ({"adjusted_close", "total_return", "price_adjustment"} & set(row))


def test_revision_public_date_uses_first_strictly_later_exchange_session():
    rows = validate("split-known.jsonl").records
    before = project_corporate_actions_as_of(
        rows,
        decision_session=date(2024, 1, 2),
        exchange_sessions=SESSIONS,
        security_lineage_id="alpha_inc",
    )
    first_eligible = project_corporate_actions_as_of(
        rows,
        decision_session=date(2024, 1, 3),
        exchange_sessions=SESSIONS,
        security_lineage_id="alpha_inc",
    )
    assert before.records == ()
    assert [record.source_event_id for record in first_eligible.records] == [
        "split-known-001"
    ]


def test_cash_distribution_retains_amount_currency_basis_and_dates():
    [record] = project(
        "cash-distribution.jsonl",
        session="2024-01-04",
        lineage="alpha_inc",
    ).records
    assert record.facts == {
        "cash_amount": "0.2500",
        "cash_currency": "USD",
        "cash_basis": "per_share",
        "ex_date": "2024-01-10",
        "pay_date": "2024-01-20",
    }


def test_merger_rows_preserve_predecessor_successor_and_unknown_cash():
    [predecessor] = project(
        "merger-lineages.jsonl",
        session="2024-02-02",
        lineage="alpha_inc",
    ).records
    assert predecessor.affected_role == "predecessor"
    assert predecessor.facts["successor_lineage_id"] == "beta_inc"
    assert predecessor.facts["exchange_ratio"] == "0.5"
    assert predecessor.facts["cash_component_amount"] is None

    [successor] = project(
        "merger-lineages.jsonl",
        session="2024-02-02",
        lineage="beta_inc",
    ).records
    assert successor.affected_role == "successor"
    assert successor.security_lineage_id == "beta_inc"


def test_membership_removal_without_action_does_not_synthesize_terminal_facts():
    result = validate("removal-without-action.jsonl")
    assert result.records == ()
    projection = project_corporate_actions_as_of(
        result.records,
        decision_session=date(2024, 2, 2),
        exchange_sessions=SESSIONS,
        security_lineage_id="legacy_co",
    )
    assert projection.records == ()
    assert projection.findings == ()


def test_reused_ticker_does_not_transfer_actions_between_lineages():
    rows = validate("ticker-reuse.jsonl").records
    old_lineage = project_corporate_actions_as_of(
        rows,
        decision_session=date(2024, 1, 3),
        exchange_sessions=SESSIONS,
        security_lineage_id="legacy_co",
    )
    new_lineage = project_corporate_actions_as_of(
        rows,
        decision_session=date(2024, 1, 4),
        exchange_sessions=SESSIONS,
        security_lineage_id="successor_co",
    )
    assert [record.source_event_id for record in old_lineage.records] == [
        "ticker-old-action"
    ]
    assert [record.source_event_id for record in new_lineage.records] == [
        "ticker-new-dividend"
    ]


def test_missing_terminal_proceeds_remain_unknown_and_never_become_zero():
    [record] = project(
        "terminal-proceeds-unknown.jsonl",
        session="2024-02-02",
        lineage="legacy_co",
    ).records
    assert record.facts["terminal_date"] == "2024-02-15"
    assert record.facts["proceeds_amount"] is None
    assert record.field_status["proceeds_amount"].status == "not_reported"
    assert record.facts["proceeds_currency"] is None


def test_later_correction_does_not_change_an_earlier_projection():
    rows = validate("revision-causality.jsonl").records
    before_correction = project_corporate_actions_as_of(
        rows[:1],
        decision_session=date(2024, 1, 5),
        exchange_sessions=SESSIONS,
        security_lineage_id="alpha_inc",
    )
    with_later_revision = project_corporate_actions_as_of(
        rows,
        decision_session=date(2024, 1, 5),
        exchange_sessions=SESSIONS,
        security_lineage_id="alpha_inc",
    )

    def canonical_projection(result) -> bytes:
        encoded = json.dumps(
            [record.to_row() for record in result.records],
            sort_keys=True,
            separators=(",", ":"),
        )
        return encoded.encode("utf-8")

    assert canonical_projection(before_correction) == canonical_projection(
        with_later_revision
    )
    [earlier] = before_correction.records
    assert earlier.source_revision_id == "r1"

    [latest] = project_corporate_actions_as_of(
        rows,
        decision_session=date(2024, 1, 11),
        exchange_sessions=SESSIONS,
        security_lineage_id="alpha_inc",
    ).records
    assert latest.source_revision_id == "r2"
    assert latest.facts["split_numerator"] == 3


def test_future_effective_action_is_known_after_publication_without_application():
    rows = validate("future-effective.jsonl").records
    before_publication = project_corporate_actions_as_of(
        rows,
        decision_session=date(2024, 1, 4),
        exchange_sessions=SESSIONS,
        security_lineage_id="alpha_inc",
    )
    before_effective_date = project_corporate_actions_as_of(
        rows,
        decision_session=date(2024, 1, 9),
        exchange_sessions=SESSIONS,
        security_lineage_id="alpha_inc",
    )
    assert before_publication.records == ()
    [known_fact] = before_effective_date.records
    assert known_fact.effective_date == "2024-01-20"
    assert not hasattr(known_fact, "adjusted_price")
    assert not hasattr(known_fact, "return_value")


def test_unknown_revision_date_is_retained_but_not_projected():
    result = validate("revision-causality.jsonl")
    projection = project_corporate_actions_as_of(
        result.records,
        decision_session=date(2024, 1, 5),
        exchange_sessions=SESSIONS,
        security_lineage_id="alpha_inc",
    )
    assert [record.source_revision_id for record in projection.records] == ["r1"]
    assert {finding.code for finding in projection.findings} == {
        "revision_public_date_unknown"
    }


def test_invalid_records_have_stable_findings_and_keep_valid_neighbors():
    result = validate("invalid-records.jsonl")
    assert [record.source_event_id for record in result.records] == [
        "invalid-neighbor-valid"
    ]
    codes = [finding.code for finding in result.findings]
    assert codes.count("event_type_not_supported") == 1
    assert codes.count("invalid_date") == 1
    assert codes.count("invalid_split_ratio") == 1
    assert codes.count("unknown_value_not_null") == 1
    assert codes.count("duplicate_primary_key") == 2


def test_unknown_values_require_a_reason_and_raw_timestamp_precision_is_kept():
    row = load_rows("terminal-proceeds-unknown.jsonl")[0]
    row["field_status"]["proceeds_amount"] = {
        "status": "not_reported",
        "reason": None,
    }
    result = validate_corporate_action_rows(
        [row],
        authenticated_security_lineages=LINEAGES,
    )
    assert result.records == ()
    assert result.findings[0].code == "unknown_value_missing_reason"

    [cash_record] = validate("cash-distribution.jsonl").records
    assert cash_record.revision_public_at == "2024-01-03"


def test_sourced_zero_is_valid_but_bad_currency_and_nonfinite_amount_fail():
    row = load_rows("cash-distribution.jsonl")[0]
    row["facts"]["cash_amount"] = "0.000"
    zero = validate_corporate_action_rows(
        [row],
        authenticated_security_lineages=LINEAGES,
    )
    assert len(zero.records) == 1
    assert zero.records[0].facts["cash_amount"] == "0.000"

    malformed_currency = json.loads(json.dumps(row))
    malformed_currency["facts"]["cash_currency"] = "US"
    bad_currency = validate_corporate_action_rows(
        [malformed_currency],
        authenticated_security_lineages=LINEAGES,
    )
    assert bad_currency.records == ()
    assert bad_currency.findings[0].code == "invalid_currency"

    nonfinite_amount = json.loads(json.dumps(row))
    nonfinite_amount["facts"]["cash_amount"] = "NaN"
    bad_amount = validate_corporate_action_rows(
        [nonfinite_amount],
        authenticated_security_lineages=LINEAGES,
    )
    assert bad_amount.records == ()
    assert bad_amount.findings[0].code == "invalid_decimal"


def test_timestamp_requires_preserved_timezone_when_time_is_supplied():
    row = load_rows("split-known.jsonl")[0]
    row["revision_public_at"] = "2024-01-02T16:10:00"
    result = validate_corporate_action_rows(
        [row],
        authenticated_security_lineages=LINEAGES,
    )
    assert result.records == ()
    assert result.findings[0].code == "invalid_timestamp"


def test_revision_cannot_precede_known_publication_without_losing_date_precision():
    original = load_rows("split-known.jsonl")[0]
    before_announcement = json.loads(json.dumps(original))
    before_announcement["public_at"] = "2024-01-05"
    before_announcement["revision_public_at"] = "2024-01-02"
    result = validate_corporate_action_rows(
        [before_announcement], authenticated_security_lineages=LINEAGES,
    )
    assert result.records == ()
    assert [finding.code for finding in result.findings] == ["revision_before_publication"]

    earlier_utc_instant = json.loads(json.dumps(original))
    earlier_utc_instant["revision_public_at"] = "2024-01-03T08:00:00+00:00"
    result = validate_corporate_action_rows(
        [earlier_utc_instant], authenticated_security_lineages=LINEAGES,
    )
    assert result.records == ()
    assert [finding.code for finding in result.findings] == ["revision_before_publication"]

    same_day_date_only = json.loads(json.dumps(original))
    same_day_date_only["public_at"] = "2024-01-03"
    same_day_date_only["revision_public_at"] = "2024-01-03T08:00:00+00:00"
    result = validate_corporate_action_rows(
        [same_day_date_only], authenticated_security_lineages=LINEAGES,
    )
    assert result.findings == ()
    [record] = result.records
    assert record.public_at == "2024-01-03"
    assert record.revision_public_at == "2024-01-03T08:00:00+00:00"
