"""Bounded local source-contract checks for the Issue 70 v9 implementation."""

from __future__ import annotations

import gzip
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import generate_issue70_q4_source_sample as generator
from tools import issue70_v9_coverage as coverage


REPOSITORY = Path(__file__).resolve().parents[1]
CALENDAR_DIR = REPOSITORY / "docs" / "issue-70-calendar-candidate-v1"
RETAINED_CALENDAR = REPOSITORY / "docs" / "issue-70-q4-source-sample-v8" / "spy_trading_days.csv"


def test_v9_calendar_input_is_adopted_and_matches_entire_retained_window() -> None:
    calendar = generator._validate_v9_calendar_inputs(
        session_calendar_path=CALENDAR_DIR / "exchange_sessions.csv",
        adoption_decision_path=CALENDAR_DIR / "principal-adoption-decision.json",
        calendar_provenance_path=CALENDAR_DIR / "calendar_provenance.json",
        candidate_marker_path=CALENDAR_DIR / "candidate-publication.json",
        retained_calendar_path=RETAINED_CALENDAR,
    )

    assert calendar["source_label"] == "derived_exchange_schedule"
    assert calendar["calendar_sha256"] == "f658bbfff04b623a3e20afa0ed2909e15e33470da4ef8305bd811f811a3c8cb5"
    assert len(calendar["sessions"]) == 4024
    assert (calendar["sessions"][0].isoformat(), calendar["sessions"][-1].isoformat()) == (
        "2010-01-04",
        "2025-12-31",
    )
    assert calendar["retained_window_overlap"] == {
        "dates": 1508,
        "derived_only": 0,
        "retained_only": 0,
    }


def test_v9_calendar_rejects_changed_csv_before_reading_reference(tmp_path: Path) -> None:
    source = CALENDAR_DIR / "exchange_sessions.csv"
    changed = tmp_path / "exchange_sessions.csv"
    changed.write_bytes(source.read_bytes()[:-1])

    with pytest.raises(ValueError, match="calendar CSV digest"):
        generator._validate_v9_calendar_inputs(
            session_calendar_path=changed,
            adoption_decision_path=CALENDAR_DIR / "principal-adoption-decision.json",
            calendar_provenance_path=CALENDAR_DIR / "calendar_provenance.json",
            candidate_marker_path=CALENDAR_DIR / "candidate-publication.json",
            retained_calendar_path=tmp_path / "must-not-be-read.csv",
        )


def test_v9_generation_rejects_partial_contract_before_archive_access(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="v9 calendar and date inputs must be supplied together"):
        generator.generate(
            archive_dir=tmp_path / "missing-archives",
            legacy_root=tmp_path / "missing-legacy",
            alternate_dir=tmp_path / "missing-alternate",
            output_dir=REPOSITORY / "docs" / "issue-70-q4-source-sample-v9",
            session_calendar_path=CALENDAR_DIR / "exchange_sessions.csv",
        )


def test_generation_refuses_existing_output_before_any_input_access(tmp_path: Path) -> None:
    existing = tmp_path / "existing-output"
    existing.mkdir()

    with pytest.raises(ValueError, match="refusing to overwrite existing sample directory"):
        generator.generate(
            archive_dir=tmp_path / "must-not-be-read-archives",
            legacy_root=tmp_path / "must-not-be-read-legacy",
            alternate_dir=tmp_path / "must-not-be-read-alternate",
            output_dir=existing,
            session_calendar_path=CALENDAR_DIR / "exchange_sessions.csv",
        )


def test_v9_coverage_gzip_is_reproducible_and_enforces_record_and_byte_caps() -> None:
    row = {column: "" for column in coverage.CSV_COLUMNS}
    row.update({
        "record_kind": "expected_slot",
        "slot_id": "slot",
        "ticker": "AAA",
        "session_date": "2025-03-11",
        "status": "missing",
        "missing_reason": "insufficient_history",
    })
    compressed_one, metadata = generator.serialize_v9_coverage_records([row])
    compressed_two, repeated_metadata = generator.serialize_v9_coverage_records([row])

    assert compressed_one == compressed_two
    assert metadata == repeated_metadata
    assert gzip.decompress(compressed_one).decode("utf-8").splitlines()[0] == ",".join(
        coverage.CSV_COLUMNS
    )
    assert metadata["gzip_filename"] == ""
    assert metadata["gzip_mtime"] == 0
    assert metadata["compressed_sha256"]
    assert metadata["uncompressed_sha256"]
    with pytest.raises(ValueError, match="250,000-row cap"):
        generator.serialize_v9_coverage_records([row], max_records=0)
    with pytest.raises(ValueError, match="128 MiB cap"):
        generator.serialize_v9_coverage_records([row], max_uncompressed_bytes=1)


def test_v8_manifest_binds_exact_measured_member_names_hashes_and_lengths() -> None:
    manifest_path = REPOSITORY / "docs" / "issue-70-q4-source-sample-v8" / "fundamentals_provenance.json"
    digest, manifest = generator._read_bound_json(
        manifest_path, generator.V9_V8_SOURCE_MANIFEST_SHA256
    )
    assert digest == generator.V9_V8_SOURCE_MANIFEST_SHA256
    expected = manifest["source_budgets"]["selected_member_hashes"]
    measured = {
        group: [
            {
                "member_name": row["member_name"],
                "sha256": row["sha256"],
                "expanded_bytes": row["expanded_bytes"],
            }
            for row in rows
        ]
        for group, rows in expected.items()
    }

    binding = generator._validate_v8_selected_member_hashes(manifest, measured)

    assert binding["status"] == "matched"
    assert binding["selected_member_count"] == 14
    assert binding["selected_members_by_archive"] == {"submissions": 10, "companyfacts": 4}
    assert binding["compared_fields"] == ["archive", "member_name", "sha256", "expanded_bytes"]
    assert len(binding["selected_members"]) == 14
    assert any(
        row["member_name"] == "CIK0000789019.json" for row in measured["submissions"]
    )
    assert any(
        row["member_name"] == "CIK0000789019.json" for row in measured["companyfacts"]
    )
    changed_read_counts = json.loads(json.dumps(measured))
    for rows in changed_read_counts.values():
        for row in rows:
            row["read_passes"] = 987
            row["preflight_sha256"] = "f" * 64
    assert generator._validate_v8_selected_member_hashes(
        manifest, changed_read_counts
    ) == binding

    expected_duplicate = json.loads(json.dumps(manifest))
    expected_rows = expected_duplicate["source_budgets"]["selected_member_hashes"]["submissions"]
    expected_rows.append(dict(expected_rows[0]))
    with pytest.raises(ValueError, match="identity is duplicated"):
        generator._validate_v8_selected_member_hashes(expected_duplicate, measured)

    malformed_expected = json.loads(json.dumps(manifest))
    malformed_expected["source_budgets"]["selected_member_hashes"]["companyfacts"][0][
        "sha256"
    ] = "not-a-digest"
    with pytest.raises(ValueError, match="identity is malformed"):
        generator._validate_v8_selected_member_hashes(malformed_expected, measured)
    malformed_expected_name = json.loads(json.dumps(manifest))
    malformed_expected_name["source_budgets"]["selected_member_hashes"]["submissions"][0][
        "member_name"
    ] = "../CIK0000000001.json"
    with pytest.raises(ValueError, match="identity is malformed"):
        generator._validate_v8_selected_member_hashes(malformed_expected_name, measured)
    malformed_expected_length = json.loads(json.dumps(manifest))
    malformed_expected_length["source_budgets"]["selected_member_hashes"]["companyfacts"][0][
        "expanded_bytes"
    ] = True
    with pytest.raises(ValueError, match="identity is malformed"):
        generator._validate_v8_selected_member_hashes(malformed_expected_length, measured)

    changed_hash = json.loads(json.dumps(measured))
    changed_hash["companyfacts"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="digest or expanded length differs"):
        generator._validate_v8_selected_member_hashes(manifest, changed_hash)
    changed_length = json.loads(json.dumps(measured))
    changed_length["submissions"][0]["expanded_bytes"] += 1
    with pytest.raises(ValueError, match="digest or expanded length differs"):
        generator._validate_v8_selected_member_hashes(manifest, changed_length)
    missing_member = json.loads(json.dumps(measured))
    missing_member["submissions"].pop()
    with pytest.raises(ValueError, match="namespaces differ"):
        generator._validate_v8_selected_member_hashes(manifest, missing_member)
    extra_member = json.loads(json.dumps(measured))
    extra_member["companyfacts"].append({
        "member_name": "CIK0000000000.json",
        "sha256": "1" * 64,
        "expanded_bytes": 1,
    })
    with pytest.raises(ValueError, match="namespaces differ"):
        generator._validate_v8_selected_member_hashes(manifest, extra_member)
    duplicate = json.loads(json.dumps(measured))
    duplicate["submissions"].append(dict(duplicate["submissions"][0]))
    with pytest.raises(ValueError, match="identity is duplicated"):
        generator._validate_v8_selected_member_hashes(manifest, duplicate)
    malformed_name = json.loads(json.dumps(measured))
    malformed_name["companyfacts"][0]["member_name"] = "../CIK0000789019.json"
    with pytest.raises(ValueError, match="identity is malformed"):
        generator._validate_v8_selected_member_hashes(manifest, malformed_name)
    malformed_digest = json.loads(json.dumps(measured))
    malformed_digest["companyfacts"][0]["sha256"] = "A" * 64
    with pytest.raises(ValueError, match="identity is malformed"):
        generator._validate_v8_selected_member_hashes(manifest, malformed_digest)
    malformed_length = json.loads(json.dumps(measured))
    malformed_length["companyfacts"][0]["expanded_bytes"] = True
    with pytest.raises(ValueError, match="identity is malformed"):
        generator._validate_v8_selected_member_hashes(manifest, malformed_length)

    renamed = json.loads(json.dumps(measured))
    renamed["companyfacts"][0]["member_name"] = "CIK0000000000.json"
    with pytest.raises(ValueError, match="namespaces differ"):
        generator._validate_v8_selected_member_hashes(manifest, renamed)

    moved_collision_content = json.loads(json.dumps(measured))
    submissions_collision = next(
        row for row in moved_collision_content["submissions"]
        if row["member_name"] == "CIK0000789019.json"
    )
    companyfacts_collision = next(
        row for row in moved_collision_content["companyfacts"]
        if row["member_name"] == "CIK0000789019.json"
    )
    submissions_identity = (submissions_collision["sha256"], submissions_collision["expanded_bytes"])
    companyfacts_identity = (companyfacts_collision["sha256"], companyfacts_collision["expanded_bytes"])
    submissions_collision["sha256"], submissions_collision["expanded_bytes"] = companyfacts_identity
    companyfacts_collision["sha256"], companyfacts_collision["expanded_bytes"] = submissions_identity
    with pytest.raises(ValueError, match="digest or expanded length differs"):
        generator._validate_v8_selected_member_hashes(manifest, moved_collision_content)


def test_measured_member_mismatch_fails_through_cli_before_publication(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    output_dir = REPOSITORY / "docs" / "issue-70-q4-source-sample-v9"
    assert not output_dir.exists()
    manifest_path = REPOSITORY / "docs" / "issue-70-q4-source-sample-v8" / "fundamentals_provenance.json"
    _, manifest = generator._read_bound_json(
        manifest_path, generator.V9_V8_SOURCE_MANIFEST_SHA256
    )
    expected = manifest["source_budgets"]["selected_member_hashes"]

    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    archive_hashes = {"submissions.zip": "1" * 64, "companyfacts.zip": "2" * 64}
    archive_receipt = {
        "archives": {
            name: {"sha256": digest} for name, digest in archive_hashes.items()
        }
    }
    archive_metadata: dict[str, dict[str, object]] = {}
    for group, name in (("submissions", "submissions.zip"), ("companyfacts", "companyfacts.zip")):
        archive_path = archive_dir / name
        archive_path.write_bytes(b"fixture")
        expanded = sum(row["expanded_bytes"] for row in expected[group])
        archive_metadata[name] = {
            "byte_length": archive_path.stat().st_size,
            "zip_entry_count": len(expected[group]),
            "zip_uncompressed_bytes": expanded,
            "sha256": archive_hashes[name],
        }

    legacy_root = tmp_path / "legacy"
    alternate_dir = tmp_path / "alternate"
    input_paths = {
        "fundamentals/fundamentals.csv": legacy_root / "fundamentals" / "fundamentals.csv",
        "fundamentals/security_master.csv": legacy_root / "fundamentals" / "security_master.csv",
        "fundamentals/security_names.csv": legacy_root / "fundamentals" / "security_names.csv",
        "prices/membership.csv": legacy_root / "prices" / "membership.csv",
        "prices/spy_trading_days.csv": legacy_root / "prices" / "spy_trading_days.csv",
        "prices/pit_price_identity_map.csv": legacy_root / "prices" / "pit_price_identity_map.csv",
    }
    receipt_files = []
    file_hashes: dict[str, str] = {}
    for relative_name, path in input_paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        digest = "a" * 64
        file_hashes[relative_name] = digest
        receipt_files.append({
            "file": relative_name,
            "bound_by_original_provenance": True,
            "bytes": path.stat().st_size,
            "sha256": digest,
        })
    input_receipt = {
        "admission_status": "authenticated_previous_SP500_material_only_not_complete_V5_inputs",
        "files": receipt_files,
    }
    old_provenance = {
        "archive_manifest": archive_receipt,
        "submissions_archive_sha256": archive_hashes["submissions.zip"],
        "companyfacts_archive_sha256": archive_hashes["companyfacts.zip"],
        "fundamentals_sha256": file_hashes["fundamentals/fundamentals.csv"],
        "security_master_sha256": file_hashes["fundamentals/security_master.csv"],
        "membership_csv_sha256": file_hashes["prices/membership.csv"],
        "security_names_csv_sha256": file_hashes["fundamentals/security_names.csv"],
        "spy_trading_days_csv_sha256": file_hashes["prices/spy_trading_days.csv"],
        "identity_manifest_csv_sha256": file_hashes["prices/pit_price_identity_map.csv"],
    }
    alternate_provenance = {
        "archive_manifest": archive_receipt,
        "submissions_archive_sha256": archive_hashes["submissions.zip"],
        "companyfacts_archive_sha256": archive_hashes["companyfacts.zip"],
    }

    def fake_json_file(path: Path) -> dict[str, object]:
        if path.name == "import-provenance.json":
            return input_receipt
        if path.parent == legacy_root / "fundamentals":
            return old_provenance
        if path.name == "fundamentals_publication.json":
            return {"status": "complete", "files": {}}
        if path.parent == alternate_dir:
            return alternate_provenance
        raise AssertionError(f"unexpected provenance read: {path}")

    monkeypatch.setattr(generator, "_json_file", fake_json_file)
    monkeypatch.setattr(generator, "_validate_bound_file", lambda *args, **kwargs: None)
    monkeypatch.setattr(generator, "_verify_archive_receipt", lambda _path: (archive_receipt, archive_metadata))
    monkeypatch.setattr(
        generator, "_archive_file_metadata",
        lambda path: {"byte_length": path.stat().st_size, "sha256": "e" * 64},
    )
    monkeypatch.setattr(
        generator, "_validate_v9_calendar_inputs",
        lambda **_kwargs: {
            "sessions": (date(2020, 1, 2),),
            "calendar_sha256": "f" * 64,
        },
    )
    monkeypatch.setattr(
        generator, "_source_revision",
        lambda _repo: {"commit": "c" * 40, "worktree_status": "clean"},
    )
    security_rows = tuple(
        SimpleNamespace(
            ticker=ticker, cik=cik, company_name=ticker,
            first_membership_date=date(2010, 1, 1),
            last_membership_date=date(2025, 12, 31),
            mapping_basis="fixture",
        )
        for ticker, cik in generator.V9_EXPECTED_CIKS.items()
    )
    monkeypatch.setattr(generator, "_load_security_rows", lambda _path: security_rows)

    def make_budget(group: str) -> dict[str, object]:
        rows = expected[group]
        signatures = {
            row["member_name"]: {"expanded_bytes": row["expanded_bytes"]}
            for row in rows
        }
        main_preflight = {
            row["member_name"]: {
                "expanded_bytes": row["expanded_bytes"],
                "sha256": row["sha256"],
            }
            for row in rows
            if group == "submissions" and "-submissions-" not in row["member_name"]
        }
        archive_name = f"{group}.zip"
        return {
            "zip_entry_count": archive_metadata[archive_name]["zip_entry_count"],
            "zip_uncompressed_bytes": archive_metadata[archive_name]["zip_uncompressed_bytes"],
            "selected_member_signatures": signatures,
            "main_preflight_reads": main_preflight,
        }

    submissions_budget = make_budget("submissions")
    companyfacts_budget = make_budget("companyfacts")
    monkeypatch.setattr(generator, "_submission_workset", lambda *_args: submissions_budget)
    monkeypatch.setattr(generator, "_companyfacts_workset", lambda *_args: companyfacts_budget)

    def fake_acceptances(_archive: Path, _ciks: object, **kwargs: object) -> tuple[dict[str, object], tuple[()]]:
        callback = kwargs["member_use_callback"]
        for row in expected["submissions"]:
            callback(row["member_name"], row["expanded_bytes"], row["sha256"])
        return {}, ()

    def fake_extract(_archive: Path, _security: object, _calendar: Path, **kwargs: object) -> SimpleNamespace:
        callback = kwargs["companyfacts_member_use_callback"]
        for index, row in enumerate(expected["companyfacts"]):
            measured_digest = "0" * 64 if index == 0 else row["sha256"]
            callback(row["member_name"], row["expanded_bytes"], measured_digest)
        return SimpleNamespace(rows=(), audit_rows=(), coverage={})

    monkeypatch.setattr(generator.sec, "_acceptances_for_ciks", fake_acceptances)
    monkeypatch.setattr(generator.sec, "extract_fundamentals", fake_extract)
    writes: list[str] = []
    monkeypatch.setattr(generator, "_write_json", lambda path, value: writes.append(path.name))
    monkeypatch.setattr(
        generator, "_build_v9_coverage_records",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("mismatch reached coverage build")),
    )

    with pytest.raises(SystemExit) as failure:
        generator.main([
            "--archive-dir", str(archive_dir),
            "--legacy-root", str(legacy_root),
            "--alternate-dir", str(alternate_dir),
            "--session-calendar", str(CALENDAR_DIR / "exchange_sessions.csv"),
            "--calendar-adoption-decision", str(CALENDAR_DIR / "principal-adoption-decision.json"),
            "--history-start-date", "2010-01-01",
            "--evaluation-start-date", "2020-01-01",
            "--evaluation-end-date", "2025-12-31",
        ])
    assert failure.value.code == 2
    assert "measured archive/member digest or expanded length differs" in capsys.readouterr().err
    assert writes == []
    assert not output_dir.exists()


def test_v9_caps_measure_complete_nested_summary_and_csv() -> None:
    records, origins, calendar, evaluation_session, coverage_summary = _v8_projection_fixture(
        period_years=(2018, 2020, 2021, 2022)
    )
    _, coverage_artifact = generator.serialize_v9_coverage_records(records)
    comparison = _build_v8_projection(records, origins, calendar, evaluation_session)
    complete_summary = generator._complete_v9_coverage_summary(
        coverage_summary, coverage_artifact, comparison, {}
    )
    measured = generator._validate_combined_v9_evidence_caps(
        coverage_artifact,
        complete_summary,
        max_records=1_000_000,
        max_uncompressed_bytes=1_000_000_000,
    )

    assert measured["origin_window_decision_count"] > 0
    assert measured["annual_gap_diagnostic_count"] > 0
    assert measured["combined_record_count"] == (
        len(records)
        + len(comparison["by_slot_id"])
        + sum(
            len(detail["origin_evidence"]["window_candidates_by_origin_id"])
            for detail in comparison["by_slot_id"].values()
        )
        + len(coverage_summary["annual_fiscal_year_gap_slots"])
    )
    assert measured["financial_summary_bytes"] == len(generator._json_bytes(complete_summary))
    assert measured["combined_canonical_uncompressed_bytes"] == (
        coverage_artifact["uncompressed_byte_length"]
        + len(generator._json_bytes(complete_summary))
    )

    with pytest.raises(ValueError, match="configured .*record cap"):
        generator._validate_combined_v9_evidence_caps(
            coverage_artifact,
            complete_summary,
            max_records=measured["combined_record_count"] - 1,
            max_uncompressed_bytes=measured["combined_canonical_uncompressed_bytes"],
        )
    with pytest.raises(ValueError, match="configured .*byte.*cap"):
        generator._validate_combined_v9_evidence_caps(
            coverage_artifact,
            complete_summary,
            max_records=measured["combined_record_count"],
            max_uncompressed_bytes=measured["combined_canonical_uncompressed_bytes"] - 1,
        )
    passed = generator._validate_combined_v9_evidence_caps(
        coverage_artifact,
        complete_summary,
        max_records=measured["combined_record_count"],
        max_uncompressed_bytes=measured["combined_canonical_uncompressed_bytes"],
    )
    assert passed == measured
    generator_source = Path(generator.__file__).read_text(encoding="utf-8")
    assert generator_source.index("v9_coverage_totals = _validate_combined_v9_evidence_caps") < (
        generator_source.index("staging = output_dir.parent")
    )


def test_v9_cli_routes_complete_contract_to_owned_source_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def fake_generate(**kwargs: object) -> dict[str, str]:
        captured.update(kwargs)
        return {"status": "contract_routed"}

    monkeypatch.setattr(generator, "generate", fake_generate)
    assert generator.main([
        "--archive-dir", str(tmp_path / "archive"),
        "--legacy-root", str(tmp_path / "legacy"),
        "--alternate-dir", str(tmp_path / "alternate"),
        "--session-calendar", str(CALENDAR_DIR / "exchange_sessions.csv"),
        "--calendar-adoption-decision", str(CALENDAR_DIR / "principal-adoption-decision.json"),
        "--history-start-date", "2010-01-01",
        "--evaluation-start-date", "2020-01-01",
        "--evaluation-end-date", "2025-12-31",
    ]) == 0

    assert capsys.readouterr().out.strip() == '{"status": "contract_routed"}'
    assert captured["history_start_date"] == date(2010, 1, 1)
    assert captured["evaluation_start_date"] == date(2020, 1, 1)
    assert captured["evaluation_end_date"] == date(2025, 12, 31)
    assert captured["session_calendar_path"] == CALENDAR_DIR / "exchange_sessions.csv"
    assert captured["calendar_adoption_decision_path"] == CALENDAR_DIR / "principal-adoption-decision.json"
    assert captured["output_dir"] == REPOSITORY / "docs" / "issue-70-q4-source-sample-v9"


def test_v9_cli_source_path_binds_manifest_before_retained_source_access(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    output_dir = REPOSITORY / "docs" / "issue-70-q4-source-sample-v9"
    assert not output_dir.exists()
    calendar_inputs: list[dict[str, object]] = []
    archive_attempts: list[bool] = []

    def fake_calendar_validation(**kwargs: object) -> dict[str, object]:
        calendar_inputs.append(kwargs)
        return {"sessions": (), "calendar_sha256": "fixture-calendar"}

    def forbidden_archive_access(*args: object, **kwargs: object) -> None:
        archive_attempts.append(True)
        raise AssertionError("archive access must not occur in this source-path contract test")

    monkeypatch.setattr(generator, "_validate_v9_calendar_inputs", fake_calendar_validation)
    monkeypatch.setattr(
        generator, "_source_revision", lambda repo_root: {"commit": "0" * 40, "worktree_status": "clean"}
    )
    monkeypatch.setattr(generator, "_verify_archive_receipt", forbidden_archive_access)
    with pytest.raises(SystemExit) as error:
        generator.main([
            "--archive-dir", str(tmp_path / "archive"),
            "--legacy-root", str(tmp_path / "missing-legacy"),
            "--alternate-dir", str(tmp_path / "alternate"),
            "--session-calendar", str(CALENDAR_DIR / "exchange_sessions.csv"),
            "--calendar-adoption-decision", str(CALENDAR_DIR / "principal-adoption-decision.json"),
            "--history-start-date", "2010-01-01",
            "--evaluation-start-date", "2020-01-01",
            "--evaluation-end-date", "2025-12-31",
        ])

    assert error.value.code == 2
    assert "invalid JSON input" in capsys.readouterr().err
    assert calendar_inputs and archive_attempts == []
    assert output_dir.exists() is False


def test_inherited_filed_fallback_uses_origin_date_not_trigger_snapshot_date() -> None:
    detail = {
        "accession_number": "0000000001-24-000001",
        "form": "10-K",
        "filed_date": "2024-03-01",
        "acceptance_datetime": "",
        "public_date_basis": "filed_date_fallback",
        "public_date": "2024-03-04",
        "source_concept": "us-gaap:NetIncomeLoss",
        "source_value": 10,
        "unit": "USD",
        "currency": "USD",
        "value_scale_multiplier": 1,
        "period_start": "2023-01-01",
        "period_end": "2023-12-31",
    }
    later_trigger_audit = SimpleNamespace(
        acceptance_datetime="2025-03-10T22:00:00Z",
        filed_date=date(2025, 3, 10),
        form="10-K/A",
        accession_number="0000000001-25-000002",
        public_date_basis="acceptance_datetime",
        fiscal_period="FY",
    )
    inherited_row = SimpleNamespace(
        public_date=date(2025, 3, 12),
        period_end=date(2023, 12, 31),
        statement_type="annual",
        net_income=10,
    )

    origin = coverage._normalized_detail(
        detail,
        later_trigger_audit,
        inherited_row,
        (date(2024, 3, 1), date(2024, 3, 4), date(2025, 3, 12)),
        source_metric="net_income",
    )

    assert origin["source_public_date"] == date(2024, 3, 1)
    assert origin["available_from_session"] == date(2024, 3, 4)
    assert origin["accession_number"] == "0000000001-24-000001"
    assert origin["source_form"] == "10-K"
    assert origin["filed_date"] == date(2024, 3, 1)
    assert origin["public_date_basis"] == "filed_date_fallback"
    assert origin["acceptance_datetime"] == ""
    vintage = coverage._vintage_key(origin)
    assert vintage[1].date() == date(2024, 3, 1)
    assert vintage[1].year != later_trigger_audit.filed_date.year
    slot_row = {column: "" for column in coverage.CSV_COLUMNS}
    slot_row.update({"slot_id": "fallback-origin-slot", "ticker": "AAA"})
    exported_origin = coverage._origin_row(
        slot_row=slot_row, item=origin, role="field_origin"
    )
    assert exported_origin["acceptance_datetime"] == ""
    assert exported_origin["filed_date"] == "2024-03-01"
    assert exported_origin["public_date_basis"] == "filed_date_fallback"

    same_session_later_acceptance = {
        **origin,
        "accession_number": "0000000001-24-000002",
        "filed_date": date(2024, 3, 2),
        "acceptance_datetime": "2024-03-02T22:00:00Z",
        "source_public_date": date(2024, 3, 2),
        "source_value": 11,
    }
    selected = coverage._period_selection(
        {("AAA", "annual", "net_income"): [origin, same_session_later_acceptance]},
        ticker="AAA",
        metric="net_income",
        statement="annual",
        session=date(2024, 3, 4),
    )
    assert selected[date(2023, 12, 31)]["accession_number"] == "0000000001-24-000002"


def _annual_eps_fixture(
    period_years: tuple[int, ...], *, eps_values: tuple[float, ...] | None = None,
    diluted_eps_values: tuple[float, ...] | None = None,
) -> tuple[SimpleNamespace, SimpleNamespace, tuple[date, ...]]:
    filing_dates = (
        (date(2019, 3, 1), date(2019, 3, 4)),
        (date(2020, 3, 1), date(2020, 3, 2)),
        (date(2021, 3, 1), date(2021, 3, 2)),
        (date(2022, 3, 1), date(2022, 3, 2)),
    )
    rows = []
    audits = []
    values = eps_values or tuple(float(index) for index in range(1, len(period_years) + 1))
    if len(values) != len(period_years) or len(period_years) > len(filing_dates):
        raise ValueError("annual EPS fixture requires matching values and at most four periods")
    if diluted_eps_values is not None and len(diluted_eps_values) != len(period_years):
        raise ValueError("diluted EPS fixture requires one value per annual period")
    for index, (year, (source_date, available_session), eps_value) in enumerate(
        zip(period_years, filing_dates[:len(period_years)], values, strict=True),
        start=1,
    ):
        period_end = date(year, 12, 31)
        accession = f"0000000001-{source_date.year}-00000{index}"
        detail = {
            "accession_number": accession,
            "form": "10-K",
            "filed_date": source_date.isoformat(),
            "acceptance_datetime": f"{source_date.isoformat()}T20:00:00Z",
            "public_date_basis": "acceptance_datetime",
            "source_public_date": source_date.isoformat(),
            "public_date": available_session.isoformat(),
            "source_concept": "us-gaap:EarningsPerShareBasic",
            "source_value": eps_value,
            "unit": "USD/shares",
            "currency": "USD",
            "value_scale_multiplier": 1,
            "period_start": f"{year}-01-01",
            "period_end": period_end.isoformat(),
            "fiscal_period": "FY",
        }
        revenue_detail = {
            **detail,
            "source_concept": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
            "source_value": index * 100,
            "unit": "USD",
        }
        metric_sources = {"basic_eps": detail, "total_revenue": revenue_detail}
        diluted_value = None
        if diluted_eps_values is not None:
            diluted_value = diluted_eps_values[index - 1]
            metric_sources["diluted_eps"] = {
                **detail,
                "source_concept": "us-gaap:EarningsPerShareDiluted",
                "source_value": diluted_value,
            }
        rows.append(SimpleNamespace(
            ticker="AAA",
            public_date=available_session,
            period_end=period_end,
            statement_type="annual",
            basic_eps=float(eps_value),
            total_revenue=float(index * 100),
            diluted_eps=diluted_value,
            net_income=None,
            common_stock=None,
            total_stockholders_equity=None,
            shares_outstanding=None,
        ))
        audits.append(SimpleNamespace(
            ticker="AAA",
            metric_sources=json.dumps(metric_sources),
            acceptance_datetime=detail["acceptance_datetime"],
            filed_date=source_date,
            form="10-K",
            accession_number=accession,
            public_date_basis="acceptance_datetime",
            fiscal_period="FY",
        ))
    fundamentals = SimpleNamespace(rows=tuple(rows), audit_rows=tuple(audits), coverage={})
    security = SimpleNamespace(
        ticker="AAA",
        cik="0000000001",
        first_membership_date=date(2021, 1, 1),
        last_membership_date=date(2023, 12, 31),
    )
    calendar = (
        date(2019, 3, 4), date(2020, 3, 2), date(2021, 3, 2),
        date(2022, 3, 2), date(2023, 3, 1),
    )
    return fundamentals, security, calendar


def test_annual_growth_keeps_accepted_status_and_reports_skipped_year_gap() -> None:
    fundamentals, security, calendar = _annual_eps_fixture((2018, 2020, 2021, 2022))

    records, summary = coverage.build_v9_coverage_records(
        fundamentals=fundamentals,
        security_rows=(security,),
        evaluation_sessions=(date(2023, 3, 1),),
        calendar_sessions=calendar,
    )

    gap_slot = next(
        row for row in records
        if row["record_kind"] == "expected_slot"
        and row["feature_id"] == "annual_eps_growth"
        and row["slot_metric"] == "annual_basic_eps_growth"
        and row["slot_number"] == "3"
    )
    assert gap_slot["status"] == "matched"
    assert gap_slot["missing_reason"] == ""
    assert gap_slot["matched_period_end"] == "2020-12-31"
    assert gap_slot["comparison_period_end"] == "2018-12-31"
    diagnostic = next(
        row for row in summary["annual_fiscal_year_gap_slots"]
        if row["slot_id"] == gap_slot["slot_id"]
    )
    assert diagnostic["skipped_fiscal_years"] == 1
    revenue_gap_slot = next(
        row for row in records
        if row["record_kind"] == "expected_slot"
        and row["feature_id"] == "annual_revenue_growth"
        and row["slot_number"] == "3"
    )
    assert revenue_gap_slot["status"] == "matched"
    assert revenue_gap_slot["matched_period_end"] == "2020-12-31"
    assert revenue_gap_slot["comparison_period_end"] == "2018-12-31"
    assert any(
        row["slot_id"] == revenue_gap_slot["slot_id"]
        for row in summary["annual_fiscal_year_gap_slots"]
    )


def test_annual_growth_no_gap_has_no_gap_diagnostic() -> None:
    fundamentals, security, calendar = _annual_eps_fixture((2018, 2019, 2020, 2021))

    records, summary = coverage.build_v9_coverage_records(
        fundamentals=fundamentals,
        security_rows=(security,),
        evaluation_sessions=(date(2023, 3, 1),),
        calendar_sessions=calendar,
    )

    annual_rows = [
        row for row in records
        if row["record_kind"] == "expected_slot"
        and row["feature_id"] == "annual_eps_growth"
        and row["slot_metric"] == "annual_basic_eps_growth"
    ]
    assert all(row["status"] == "matched" for row in annual_rows)
    revenue_rows = [
        row for row in records
        if row["record_kind"] == "expected_slot" and row["feature_id"] == "annual_revenue_growth"
    ]
    assert all(row["status"] == "matched" for row in revenue_rows)
    assert summary["annual_fiscal_year_gap_slot_count"] == 0
    assert summary["annual_fiscal_year_gap_slots"] == []


def test_annual_growth_partial_window_keeps_only_real_missing_slot() -> None:
    fundamentals, security, calendar = _annual_eps_fixture((2020, 2021, 2023))

    records, summary = coverage.build_v9_coverage_records(
        fundamentals=fundamentals,
        security_rows=(security,),
        evaluation_sessions=(date(2023, 3, 1),),
        calendar_sessions=calendar,
    )

    annual_rows = [
        row for row in records
        if row["record_kind"] == "expected_slot"
        and row["feature_id"] == "annual_eps_growth"
        and row["slot_metric"] == "annual_basic_eps_growth"
    ]
    assert [row["status"] for row in annual_rows] == ["matched", "matched", "missing"]
    assert [row["matched_period_end"] for row in annual_rows] == [
        "2023-12-31", "2021-12-31", "",
    ]
    assert annual_rows[2]["missing_reason"] == "insufficient_reported_periods"
    revenue_rows = [
        row for row in records
        if row["record_kind"] == "expected_slot"
        and row["feature_id"] == "annual_revenue_growth"
    ]
    assert len(annual_rows) == len(revenue_rows) == 3
    assert {row["slot_number"] for row in annual_rows} == {"1", "2", "3"}
    assert [row["status"] for row in revenue_rows] == ["matched", "matched", "missing"]
    assert revenue_rows[2]["missing_reason"] == "insufficient_reported_periods"
    assert summary["annual_fiscal_year_gap_slot_count"] == 2


def test_annual_diluted_eps_growth_keeps_a_positive_gap_pair() -> None:
    fundamentals, security, calendar = _annual_eps_fixture(
        (2018, 2020, 2021, 2022), diluted_eps_values=(1, 2, 3, 4)
    )
    records, summary = coverage.build_v9_coverage_records(
        fundamentals=fundamentals,
        security_rows=(security,),
        evaluation_sessions=(date(2023, 3, 1),),
        calendar_sessions=calendar,
    )
    slot = next(
        row for row in records
        if row["record_kind"] == "expected_slot"
        and row["feature_id"] == "annual_eps_growth"
        and row["slot_metric"] == "annual_diluted_eps_growth"
        and row["slot_number"] == "3"
    )
    assert slot["status"] == "matched"
    assert slot["missing_reason"] == ""
    assert slot["matched_period_end"] == "2020-12-31"
    assert slot["comparison_period_end"] == "2018-12-31"
    assert slot["lookback_stage"] == slot["calculation_stage"] == "observed"
    assert any(
        item["slot_id"] == slot["slot_id"] and item["skipped_fiscal_years"] == 1
        for item in summary["annual_fiscal_year_gap_slots"]
    )


def test_annual_growth_gap_preserves_invalid_prior_reason_and_period_endpoints() -> None:
    fundamentals, security, calendar = _annual_eps_fixture(
        (2018, 2020, 2021, 2022), eps_values=(0, 2, 3, 4)
    )

    records, summary = coverage.build_v9_coverage_records(
        fundamentals=fundamentals,
        security_rows=(security,),
        evaluation_sessions=(date(2023, 3, 1),),
        calendar_sessions=calendar,
    )

    invalid_slot = next(
        row for row in records
        if row["record_kind"] == "expected_slot"
        and row["feature_id"] == "annual_eps_growth"
        and row["slot_metric"] == "annual_basic_eps_growth"
        and row["slot_number"] == "3"
    )
    assert invalid_slot["status"] == "missing"
    assert invalid_slot["missing_reason"] == "invalid_nonpositive_or_near_zero_comparison_value"
    assert invalid_slot["expected_period_end"] == "2020-12-31"
    assert invalid_slot["matched_period_end"] == "2020-12-31"
    assert invalid_slot["comparison_period_end"] == "2018-12-31"
    assert invalid_slot["lookback_stage"] == "invalid"
    assert invalid_slot["calculation_stage"] == "invalid"
    assert any(
        row["slot_id"] == invalid_slot["slot_id"]
        and row["skipped_fiscal_years"] == 1
        for row in summary["annual_fiscal_year_gap_slots"]
    )


def _v8_projection_fixture(
    *, filing_dates: tuple[tuple[date, date], ...] | None = None,
    period_years: tuple[int, ...] = (2018, 2019, 2020, 2021),
) -> tuple[
    list[dict[str, str]], list[dict[str, object]], tuple[date, ...], date, dict[str, object]
]:
    fundamentals, security, calendar = _annual_eps_fixture(period_years)
    if filing_dates is not None:
        rows = []
        audits = []
        for index, (row, audit, dates) in enumerate(
            zip(fundamentals.rows, fundamentals.audit_rows, filing_dates, strict=True),
            start=1,
        ):
            source_date, available_session = dates
            period_end = row.period_end
            accession = f"0000000001-{source_date.year}-00000{index}"
            sources = json.loads(audit.metric_sources)
            for detail in sources.values():
                detail.update({
                    "filed_date": source_date.isoformat(),
                    "acceptance_datetime": f"{source_date.isoformat()}T20:00:00Z",
                    "source_public_date": source_date.isoformat(),
                    "public_date": available_session.isoformat(),
                    "period_end": period_end.isoformat(),
                    "accession_number": accession,
                })
            rows.append(SimpleNamespace(**{**vars(row), "public_date": available_session}))
            audits.append(SimpleNamespace(**{
                **vars(audit), "metric_sources": json.dumps(sources),
                "acceptance_datetime": f"{source_date.isoformat()}T20:00:00Z",
                "filed_date": source_date, "accession_number": accession,
            }))
        fundamentals = SimpleNamespace(rows=tuple(rows), audit_rows=tuple(audits), coverage={})
        calendar = tuple(sorted({available for _, available in filing_dates} | {date(2023, 3, 1)}))

    evaluation_session = date(2023, 3, 1)
    normalized_origins: list[dict[str, object]] = []
    v9_records, coverage_summary = coverage.build_v9_coverage_records(
        fundamentals=fundamentals,
        security_rows=(security,),
        evaluation_sessions=(evaluation_session,),
        calendar_sessions=calendar,
        normalized_origins_out=normalized_origins,
    )
    return v9_records, normalized_origins, calendar, evaluation_session, coverage_summary


def _build_v8_projection(
    records: list[dict[str, str]], origins: list[dict[str, object]],
    calendar: tuple[date, ...], evaluation_session: date,
) -> dict[str, object]:
    return coverage.build_v8_source_window_comparison(
        v9_records=records,
        normalized_origins=origins,
        evaluation_sessions=(evaluation_session,),
        calendar_sessions=calendar,
        source_window_start=date(2020, 1, 1),
        source_window_end=date(2025, 12, 31),
        v8_manifest_sha256="a" * 64,
        adopted_calendar_sha256="b" * 64,
        source_revision={"commit": "c" * 40, "worktree_status": "clean"},
        measured_member_binding={
            "status": "matched", "selected_member_count": 14,
            "selected_members": [],
        },
    )


def _annual_roe_projection_fixture() -> tuple[
    list[dict[str, str]], list[dict[str, object]], tuple[date, ...], date
]:
    def detail(
        *, accession: str, source_date: date, available: date, period_end: date,
        metric: str, value: float,
    ) -> dict[str, object]:
        return {
            "accession_number": accession,
            "form": "10-K",
            "filed_date": source_date.isoformat(),
            "acceptance_datetime": f"{source_date.isoformat()}T13:00:00Z",
            "public_date_basis": "acceptance_datetime",
            "source_public_date": source_date.isoformat(),
            "public_date": available.isoformat(),
            "source_concept": (
                "us-gaap:ProfitLoss" if metric == "net_income"
                else "us-gaap:StockholdersEquity"
            ),
            "source_value": value,
            "unit": "USD",
            "currency": "USD",
            "value_scale_multiplier": 1,
            "period_start": (
                f"{period_end.year - 1}-10-01" if metric == "net_income" else None
            ),
            "period_end": period_end.isoformat(),
            "fiscal_period": "FY",
        }

    fy2018_end = date(2018, 9, 30)
    fy2019_end = date(2019, 9, 30)
    restatement_date = date(2020, 1, 15)
    restatement_available = date(2020, 1, 16)
    fy2019_date = date(2019, 11, 15)
    fy2019_available = date(2019, 11, 18)
    accession_2018 = "0000000001-20-000001"
    accession_2019 = "0000000001-19-000002"
    fy2018_income = detail(
        accession=accession_2018, source_date=restatement_date,
        available=restatement_available, period_end=fy2018_end,
        metric="net_income", value=100,
    )
    fy2018_equity = detail(
        accession=accession_2018, source_date=restatement_date,
        available=restatement_available, period_end=fy2018_end,
        metric="total_stockholders_equity", value=500,
    )
    fy2019_income = detail(
        accession=accession_2019, source_date=fy2019_date,
        available=fy2019_available, period_end=fy2019_end,
        metric="net_income", value=120,
    )
    rows = (
        SimpleNamespace(
            ticker="AAA", public_date=restatement_available, period_end=fy2018_end,
            statement_type="annual", net_income=100.0, total_stockholders_equity=500.0,
            basic_eps=None, diluted_eps=None, total_revenue=None, common_stock=None,
            shares_outstanding=None,
        ),
        SimpleNamespace(
            ticker="AAA", public_date=fy2019_available, period_end=fy2019_end,
            statement_type="annual", net_income=120.0, total_stockholders_equity=None,
            basic_eps=None, diluted_eps=None, total_revenue=None, common_stock=None,
            shares_outstanding=None,
        ),
    )
    audits = (
        SimpleNamespace(
            ticker="AAA", metric_sources=json.dumps({
                "net_income": fy2018_income,
                "total_stockholders_equity": fy2018_equity,
            }),
            acceptance_datetime=f"{restatement_date.isoformat()}T13:00:00Z",
            filed_date=restatement_date, form="10-K", accession_number=accession_2018,
            public_date_basis="acceptance_datetime", fiscal_period="FY",
        ),
        SimpleNamespace(
            ticker="AAA", metric_sources=json.dumps({"net_income": fy2019_income}),
            acceptance_datetime=f"{fy2019_date.isoformat()}T13:00:00Z",
            filed_date=fy2019_date, form="10-K", accession_number=accession_2019,
            public_date_basis="acceptance_datetime", fiscal_period="FY",
        ),
    )
    fundamentals = SimpleNamespace(rows=rows, audit_rows=audits, coverage={})
    security = SimpleNamespace(
        ticker="AAA", cik="0000000001", first_membership_date=date(2010, 1, 1),
        last_membership_date=date(2025, 12, 31),
    )
    calendar = (fy2019_available, restatement_available, date(2020, 1, 20))
    evaluation_session = date(2020, 1, 20)
    normalized_origins: list[dict[str, object]] = []
    records, _summary = coverage.build_v9_coverage_records(
        fundamentals=fundamentals,
        security_rows=(security,),
        evaluation_sessions=(evaluation_session,),
        calendar_sessions=calendar,
        normalized_origins_out=normalized_origins,
    )
    return records, normalized_origins, calendar, evaluation_session


def test_v8_source_window_projection_is_exact_slot_anchored_and_classifies_missingness() -> None:
    records, origins, calendar, evaluation_session, _summary = _v8_projection_fixture()
    comparison = _build_v8_projection(records, origins, calendar, evaluation_session)
    by_slot_id = comparison["by_slot_id"]
    current_slots = [
        row for row in records
        if row["record_kind"] == "expected_slot"
        and row["membership_status"] == "eligible_sample_membership"
    ]
    assert set(by_slot_id) == {row["slot_id"] for row in current_slots}
    assert comparison["evaluation_sessions"] == [evaluation_session.isoformat()]
    assert comparison["source_identity"]["eligible_membership_denominator"][
        "eligible_security_session_count"
    ] == 1

    recovered = next(
        row for row in current_slots
        if row["feature_id"] == "annual_eps_growth"
        and row["slot_metric"] == "annual_basic_eps_growth"
        and row["slot_number"] == "3"
    )
    recovered_projection = by_slot_id[recovered["slot_id"]]
    assert recovered_projection["classification"] == "recovered_by_prehistory"
    assert recovered_projection["v8_source_window_projection"]["status"] == "missing"
    assert recovered_projection["v8_source_window_projection"]["missing_reason"] == "insufficient_reported_periods"
    assert recovered_projection["origin_evidence"]["current_slot_origin_ids"]
    assert any(
        decision == "source_public_date_before_v8_window"
        for decision in recovered_projection["origin_evidence"]["window_candidates_by_origin_id"].values()
    )
    assert any(
        item["classification"] == "still_missing"
        and item["classification_reason"]
        for item in by_slot_id.values()
    )
    assert any(
        item["classification"] == "available_in_both_unchanged"
        for item in by_slot_id.values()
    )
    first_serialization = coverage._canonical_json_bytes(comparison)
    second_serialization = coverage._canonical_json_bytes(
        _build_v8_projection(records, origins, calendar, evaluation_session)
    )
    assert first_serialization == second_serialization
    assert comparison["canonical_detail_byte_length"] == len(first_serialization)
    assert comparison["complete_comparison_sha256"]


def test_v8_window_excludes_raw_2019_origin_even_when_available_in_2020() -> None:
    filing_dates = (
        (date(2019, 12, 31), date(2020, 1, 2)),
        (date(2020, 3, 1), date(2020, 3, 2)),
        (date(2021, 3, 1), date(2021, 3, 2)),
        (date(2022, 3, 1), date(2022, 3, 2)),
    )
    records, origins, calendar, evaluation_session, _summary = _v8_projection_fixture(
        filing_dates=filing_dates
    )
    comparison = _build_v8_projection(records, origins, calendar, evaluation_session)
    matched_origin = next(
        row for row in records
        if row["record_kind"] == "source_origin"
        and row["source_period_end"] == "2018-12-31"
        and row["source_public_date"] == "2019-12-31"
        and row["available_from_session"] == "2020-01-02"
    )
    slot = next(
        row for row in records
        if row["record_kind"] == "expected_slot"
        and row["feature_id"] == "annual_eps_growth"
        and row["slot_number"] == "3"
    )
    evidence = comparison["by_slot_id"][slot["slot_id"]]["origin_evidence"]
    normalized = next(
        item for item in origins
        if item["period_end"] == date(2018, 12, 31)
        and item["source_public_date"] == date(2019, 12, 31)
    )
    retained_reference = coverage._origin_reference(
        normalized, coverage._origin_reference_index(records)
    )
    assert matched_origin["available_from_session"] == "2020-01-02"
    assert evidence["window_candidates_by_origin_id"][retained_reference] == (
        "source_public_date_before_v8_window"
    )


def test_v8_projection_reselects_an_admitted_alternative_vintage() -> None:
    records, origins, calendar, evaluation_session, _summary = _v8_projection_fixture()
    old_origin = next(
        item for item in origins
        if item["source_metric"] == "basic_eps" and item["period_end"] == date(2018, 12, 31)
    )
    alternative = dict(old_origin)
    alternative.update({
        "accession_number": "0000000001-20-000099",
        "source_value": 9.0,
        "source_public_date": date(2020, 3, 1),
        "filed_date": date(2020, 3, 1),
        "acceptance_datetime": "2020-03-01T20:00:00Z",
        "available_from_session": date(2020, 3, 2),
    })
    field_slot = next(
        row for row in records
        if row["record_kind"] == "field_session"
        and row["slot_metric"] == "basic_eps"
    )
    alternative_row = coverage._origin_row(
        slot_row=field_slot, item=alternative, role="field_origin"
    )
    records.append(alternative_row)
    origins.append(alternative)

    comparison = _build_v8_projection(records, origins, calendar, evaluation_session)
    target = next(
        row for row in records
        if row["record_kind"] == "expected_slot"
        and row["feature_id"] == "annual_eps_growth"
        and row["slot_metric"] == "annual_basic_eps_growth"
        and row["slot_number"] == "3"
    )
    projected = comparison["by_slot_id"][target["slot_id"]]
    assert projected["classification"] == "admitted_vintage_changed"
    assert projected["v8_source_window_projection"]["status"] == "matched"
    assert projected["current"]["status"] == "matched"
    assert projected["current"]["matched_period_end"] == "2019-12-31"
    assert projected["current"]["comparison_period_end"] == "2018-12-31"
    assert projected["v8_source_window_projection"]["matched_period_end"] == "2019-12-31"
    assert projected["v8_source_window_projection"]["comparison_period_end"] == "2018-12-31"
    assert alternative_row["origin_id"] in projected["origin_evidence"]["baseline_selected_origin_ids"]
    assert projected["origin_evidence"]["window_candidates_by_origin_id"][alternative_row["origin_id"]] == (
        "admitted_visible"
    )


def test_roe_income_endpoint_change_with_same_equity_endpoint_is_natural_anchor() -> None:
    records, origins, calendar, evaluation_session = _annual_roe_projection_fixture()
    comparison = _build_v8_projection(records, origins, calendar, evaluation_session)
    current_slot = next(
        row for row in records
        if row["record_kind"] == "expected_slot" and row["feature_id"] == "annual_roe"
    )
    projected = comparison["by_slot_id"][current_slot["slot_id"]]

    assert projected["ticker"] == current_slot["ticker"]
    assert projected["session_date"] == current_slot["session_date"]
    assert projected["expected_period_end"] == "2019-09-30"
    assert projected["current"]["status"] == "observed"
    assert projected["current"]["matched_period_end"] == "2019-09-30"
    assert projected["current"]["comparison_period_end"] == "2018-09-30"
    assert projected["v8_source_window_projection"]["status"] == "observed"
    assert projected["v8_source_window_projection"]["matched_period_end"] == "2018-09-30"
    assert projected["v8_source_window_projection"]["comparison_period_end"] == "2018-09-30"
    assert projected["classification"] == "natural_anchor_changed"
    assert "latest annual income endpoint" in projected["classification_reason"]
    assert projected["origin_evidence"]["current_slot_origin_ids"]


def test_v8_source_window_independently_gates_each_derived_q4_basis_origin() -> None:
    in_window = {
        "source_public_date": date(2020, 1, 2),
        "filed_date": date(2020, 1, 2),
        "acceptance_datetime": "2020-01-02T12:00:00Z",
    }
    for excluded_index in range(4):
        inputs = [dict(in_window) for _ in range(4)]
        inputs[excluded_index]["source_public_date"] = date(2019, 12, 31)
        item = {
            "source_public_date": date(2020, 1, 2),
            "q4_attribution": "derived",
            "inputs": inputs,
        }
        assert coverage._source_window_decision(
            item, start=date(2020, 1, 1), end=date(2025, 12, 31)
        ) == "derived_q4_basis_outside_v8_window"
    fully_admitted = {
        "source_public_date": date(2020, 1, 2),
        "q4_attribution": "derived",
        "inputs": [dict(in_window) for _ in range(4)],
    }
    assert coverage._source_window_decision(
        fully_admitted, start=date(2020, 1, 1), end=date(2025, 12, 31)
    ) == "admitted_to_v8_source_window"


def test_v8_projection_preserves_once_mapped_availability_session() -> None:
    origin = {
        "ticker": "AAA",
        "source_metric": "basic_eps",
        "statement_type": "annual",
        "period_end": date(2020, 12, 31),
        "available_from_session": date(2020, 1, 2),
        "source_public_date": date(2020, 1, 1),
        "acceptance_datetime": "2020-01-01T12:00:00Z",
        "accession_number": "0000000001-20-000001",
        "source_form": "10-K",
    }
    index = {("AAA", "annual", "basic_eps"): [origin]}
    before = coverage._period_selection(
        index, ticker="AAA", metric="basic_eps", statement="annual",
        session=date(2020, 1, 1),
    )
    on_session = coverage._period_selection(
        index, ticker="AAA", metric="basic_eps", statement="annual",
        session=date(2020, 1, 2),
    )
    assert before == {}
    assert on_session == {date(2020, 12, 31): origin}


def test_same_session_vintage_order_uses_acceptance_timestamp_then_direct_q4_tie() -> None:
    period_end = date(2024, 12, 31)
    session = date(2025, 3, 11)
    shared = {
        "source_metric": "total_revenue",
        "statement_type": "quarterly",
        "period_end": period_end,
        "available_from_session": session,
        "source_public_date": date(2025, 3, 10),
        "source_form": "10-K",
    }
    older_acceptance = {
        **shared,
        "accession_number": "0000000001-25-000002",
        "acceptance_datetime": "2025-03-10T20:00:00Z",
        "q4_attribution": "derived",
    }
    later_acceptance = {
        **shared,
        "accession_number": "0000000001-25-000001",
        "acceptance_datetime": "2025-03-10T22:00:00Z",
        "q4_attribution": "derived",
    }
    later_direct = {
        **later_acceptance,
        "q4_attribution": "direct_10k_quarter_duration",
    }
    index = {
        ("AAA", "quarterly", "total_revenue"): [
            older_acceptance,
            later_acceptance,
            later_direct,
        ]
    }

    selected = coverage._period_selection(
        index,
        ticker="AAA",
        metric="total_revenue",
        statement="quarterly",
        session=date(2025, 3, 12),
    )

    assert selected[period_end]["accession_number"] == "0000000001-25-000001"
    assert selected[period_end]["q4_attribution"] == "direct_10k_quarter_duration"
