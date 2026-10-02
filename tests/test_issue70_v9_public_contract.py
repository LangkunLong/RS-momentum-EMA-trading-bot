"""Public Issue 70 v9 boundary controls using only versioned synthetic fixtures."""

from __future__ import annotations

import copy
import builtins
import hashlib
import importlib.util
import inspect
import io
import json
import os
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import generate_issue70_q4_source_sample as generator
from tools import issue70_v9_coverage as coverage


REPOSITORY = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "issue70_v9_public"
SYNTHETIC_CALENDAR = FIXTURES / "synthetic_calendar.csv"
SYNTHETIC_MEMBERS = FIXTURES / "synthetic_selected_member_manifest.json"

# These pins bind only the small test fixtures below, never retained SEC or
# adopted-calendar inputs. Refresh them only with an intentional fixture change.
SYNTHETIC_CALENDAR_SHA256 = "45e19b621f09da731abea705a2ba1f15b3cc06d186afd771f76f437d42e33728"
SYNTHETIC_CALENDAR_BYTES = 44
SYNTHETIC_MEMBERS_SHA256 = "bbf91de41e372c70b65c9940f3ed887c5642d69ce1fb72254020a6082fd53750"
SYNTHETIC_MEMBERS_BYTES = 2339
RETAINED_PRIVATE_INPUTS = (
    "docs/issue-70-calendar-candidate-v1/exchange_sessions.csv",
    "docs/issue-70-calendar-candidate-v1/calendar_provenance.json",
    "docs/issue-70-calendar-candidate-v1/candidate-publication.json",
    "docs/issue-70-calendar-candidate-v1/principal-adoption-decision.json",
    "docs/issue-70-q4-source-sample-v8/spy_trading_days.csv",
    "docs/issue-70-q4-source-sample-v8/fundamentals_provenance.json",
)
_BLOCKED_PATHS = {
    os.path.normcase(os.path.abspath(os.fspath(REPOSITORY / relative))).casefold()
    for relative in RETAINED_PRIVATE_INPUTS
}

RETAINED_TEST_SOURCE = REPOSITORY / "tests" / "retained_issue70_v9_source_contract.py"
_RETAINED_SPEC = importlib.util.spec_from_file_location(
    "issue70_v9_retained_source_contract", RETAINED_TEST_SOURCE
)
if _RETAINED_SPEC is None or _RETAINED_SPEC.loader is None:
    raise RuntimeError("accepted retained source-contract tests are unavailable")
_RETAINED_TESTS = importlib.util.module_from_spec(_RETAINED_SPEC)
_RETAINED_SPEC.loader.exec_module(_RETAINED_TESTS)

SAFE_ORIGINAL_TESTS = (
    "test_v9_generation_rejects_partial_contract_before_archive_access",
    "test_generation_refuses_existing_output_before_any_input_access",
    "test_v9_coverage_gzip_is_reproducible_and_enforces_record_and_byte_caps",
    "test_v9_caps_measure_complete_nested_summary_and_csv",
    "test_v9_cli_routes_complete_contract_to_owned_source_path",
    "test_inherited_filed_fallback_uses_origin_date_not_trigger_snapshot_date",
    "test_annual_growth_keeps_accepted_status_and_reports_skipped_year_gap",
    "test_annual_growth_no_gap_has_no_gap_diagnostic",
    "test_annual_growth_partial_window_keeps_only_real_missing_slot",
    "test_annual_diluted_eps_growth_keeps_a_positive_gap_pair",
    "test_annual_growth_gap_preserves_invalid_prior_reason_and_period_endpoints",
    "test_v8_source_window_projection_is_exact_slot_anchored_and_classifies_missingness",
    "test_v8_window_excludes_raw_2019_origin_even_when_available_in_2020",
    "test_v8_projection_reselects_an_admitted_alternative_vintage",
    "test_roe_income_endpoint_change_with_same_equity_endpoint_is_natural_anchor",
    "test_v8_source_window_independently_gates_each_derived_q4_basis_origin",
    "test_v8_projection_preserves_once_mapped_availability_session",
    "test_same_session_vintage_order_uses_acceptance_timestamp_then_direct_q4_tie",
)


@pytest.fixture(autouse=True)
def forbid_retained_input_access(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make public tests independent of retained calendar and v8 bytes."""
    def guard(value: object) -> None:
        if not isinstance(value, (str, bytes, os.PathLike)):
            return
        candidate = os.path.normcase(os.path.abspath(os.fsdecode(os.fspath(value)))).casefold()
        if candidate in _BLOCKED_PATHS:
            raise AssertionError(f"public test selection attempted retained-input access: {value}")

    path_open = Path.open
    path_stat = Path.stat
    builtin_open = builtins.open
    io_open = io.open

    def guarded_path_open(path: Path, *args: object, **kwargs: object) -> object:
        guard(path)
        return path_open(path, *args, **kwargs)

    def guarded_path_stat(path: Path, *args: object, **kwargs: object) -> object:
        guard(path)
        return path_stat(path, *args, **kwargs)

    def guarded_builtin_open(path: object, *args: object, **kwargs: object) -> object:
        guard(path)
        return builtin_open(path, *args, **kwargs)

    def guarded_io_open(path: object, *args: object, **kwargs: object) -> object:
        guard(path)
        return io_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_path_open)
    monkeypatch.setattr(Path, "stat", guarded_path_stat)
    monkeypatch.setattr(builtins, "open", guarded_builtin_open)
    monkeypatch.setattr(io, "open", guarded_io_open)


def _canonical_fixture_copy(source: Path, destination: Path) -> tuple[bytes, Path]:
    text = source.read_text(encoding="utf-8")
    raw = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    destination.write_bytes(raw)
    return raw, destination


def _synthetic_manifest(tmp_path: Path) -> dict[str, object]:
    raw, bound_path = _canonical_fixture_copy(
        SYNTHETIC_MEMBERS, tmp_path / SYNTHETIC_MEMBERS.name
    )
    assert len(raw) == SYNTHETIC_MEMBERS_BYTES
    assert hashlib.sha256(raw).hexdigest() == SYNTHETIC_MEMBERS_SHA256
    assert generator._validate_bound_file(
        bound_path,
        SYNTHETIC_MEMBERS_SHA256,
        expected_bytes=SYNTHETIC_MEMBERS_BYTES,
    ) == SYNTHETIC_MEMBERS_SHA256
    manifest = json.loads(raw.decode("utf-8"))
    assert manifest["fixture_id"] == "issue70-v9-public-synthetic-members-v1"
    assert manifest["fixture_scope"] == "TEST_ONLY_NOT_REAL_SEC_MEMBER_IDENTITIES"
    return manifest


def _synthetic_selected(manifest: dict[str, object]) -> dict[str, list[dict[str, object]]]:
    budgets = manifest["source_budgets"]
    return copy.deepcopy(budgets["selected_member_hashes"])


def _annual_eps_fixture(
    period_years: tuple[int, ...],
) -> tuple[SimpleNamespace, SimpleNamespace, tuple[date, ...]]:
    filing_dates = (
        (date(2019, 3, 1), date(2019, 3, 4)),
        (date(2020, 3, 1), date(2020, 3, 2)),
        (date(2021, 3, 1), date(2021, 3, 2)),
        (date(2022, 3, 1), date(2022, 3, 2)),
    )
    rows: list[SimpleNamespace] = []
    audits: list[SimpleNamespace] = []
    for index, (year, (source_date, available_session)) in enumerate(
        zip(period_years, filing_dates, strict=False),
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
            "source_value": float(index),
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
        rows.append(SimpleNamespace(
            ticker="SYNTH",
            public_date=available_session,
            period_end=period_end,
            statement_type="annual",
            basic_eps=float(index),
            total_revenue=float(index * 100),
            diluted_eps=None,
            net_income=None,
            common_stock=None,
            total_stockholders_equity=None,
            shares_outstanding=None,
        ))
        audits.append(SimpleNamespace(
            ticker="SYNTH",
            metric_sources=json.dumps({"basic_eps": detail, "total_revenue": revenue_detail}),
            acceptance_datetime=detail["acceptance_datetime"],
            filed_date=source_date,
            form="10-K",
            accession_number=accession,
            public_date_basis="acceptance_datetime",
            fiscal_period="FY",
        ))
    fundamentals = SimpleNamespace(rows=tuple(rows), audit_rows=tuple(audits), coverage={})
    security = SimpleNamespace(
        ticker="SYNTH",
        cik="9999999999",
        first_membership_date=date(2021, 1, 1),
        last_membership_date=date(2025, 12, 31),
    )
    calendar = (
        date(2019, 3, 4), date(2020, 3, 2), date(2021, 3, 2),
        date(2022, 3, 2), date(2023, 3, 1),
    )
    return fundamentals, security, calendar


def _projection_fixture(
    period_years: tuple[int, ...] = (2018, 2019, 2020, 2021),
) -> tuple[list[dict[str, str]], list[dict[str, object]], tuple[date, ...], date]:
    fundamentals, security, calendar = _annual_eps_fixture(period_years)
    evaluation_session = date(2023, 3, 1)
    origins: list[dict[str, object]] = []
    records, _summary = coverage.build_v9_coverage_records(
        fundamentals=fundamentals,
        security_rows=(security,),
        evaluation_sessions=(evaluation_session,),
        calendar_sessions=calendar,
        normalized_origins_out=origins,
    )
    return records, origins, calendar, evaluation_session


def _project(
    records: list[dict[str, str]],
    origins: list[dict[str, object]],
    calendar: tuple[date, ...],
    evaluation_session: date,
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
        measured_member_binding={"status": "matched", "selected_member_count": 14, "selected_members": []},
    )


def _annual_roe_fixture() -> tuple[
    list[dict[str, str]], list[dict[str, object]], tuple[date, ...], date
]:
    def detail(
        *, accession: str, source_date: date, available: date,
        period_end: date, metric: str, value: float,
    ) -> dict[str, object]:
        return {
            "accession_number": accession,
            "form": "10-K",
            "filed_date": source_date.isoformat(),
            "acceptance_datetime": f"{source_date.isoformat()}T13:00:00Z",
            "public_date_basis": "acceptance_datetime",
            "source_public_date": source_date.isoformat(),
            "public_date": available.isoformat(),
            "source_concept": "us-gaap:ProfitLoss" if metric == "net_income" else "us-gaap:StockholdersEquity",
            "source_value": value,
            "unit": "USD",
            "currency": "USD",
            "value_scale_multiplier": 1,
            "period_start": f"{period_end.year - 1}-10-01" if metric == "net_income" else None,
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
            ticker="SYNTH", public_date=restatement_available,
            period_end=fy2018_end, statement_type="annual", net_income=100.0,
            total_stockholders_equity=500.0, basic_eps=None, diluted_eps=None,
            total_revenue=None, common_stock=None, shares_outstanding=None,
        ),
        SimpleNamespace(
            ticker="SYNTH", public_date=fy2019_available,
            period_end=fy2019_end, statement_type="annual", net_income=120.0,
            total_stockholders_equity=None, basic_eps=None, diluted_eps=None,
            total_revenue=None, common_stock=None, shares_outstanding=None,
        ),
    )
    audits = (
        SimpleNamespace(
            ticker="SYNTH",
            metric_sources=json.dumps({"net_income": fy2018_income, "total_stockholders_equity": fy2018_equity}),
            acceptance_datetime=f"{restatement_date.isoformat()}T13:00:00Z",
            filed_date=restatement_date, form="10-K", accession_number=accession_2018,
            public_date_basis="acceptance_datetime", fiscal_period="FY",
        ),
        SimpleNamespace(
            ticker="SYNTH",
            metric_sources=json.dumps({"net_income": fy2019_income}),
            acceptance_datetime=f"{fy2019_date.isoformat()}T13:00:00Z",
            filed_date=fy2019_date, form="10-K", accession_number=accession_2019,
            public_date_basis="acceptance_datetime", fiscal_period="FY",
        ),
    )
    fundamentals = SimpleNamespace(rows=rows, audit_rows=audits, coverage={})
    security = SimpleNamespace(
        ticker="SYNTH", cik="9999999999", first_membership_date=date(2010, 1, 1),
        last_membership_date=date(2025, 12, 31),
    )
    calendar = (fy2019_available, restatement_available, date(2020, 1, 20))
    evaluation_session = date(2020, 1, 20)
    origins: list[dict[str, object]] = []
    records, _summary = coverage.build_v9_coverage_records(
        fundamentals=fundamentals,
        security_rows=(security,),
        evaluation_sessions=(evaluation_session,),
        calendar_sessions=calendar,
        normalized_origins_out=origins,
    )
    return records, origins, calendar, evaluation_session


def test_public_selected_modules_resolve_inside_this_checkout() -> None:
    root = REPOSITORY.resolve()
    for module in (generator, coverage, generator.sec, _RETAINED_TESTS):
        assert Path(module.__file__).resolve().is_relative_to(root)


@pytest.mark.parametrize("test_name", SAFE_ORIGINAL_TESTS)
def test_public_original_self_contained_contract_case(
    test_name: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Run each unchanged accepted assertion body without its five retained inputs."""
    test_function = getattr(_RETAINED_TESTS, test_name)
    available_fixtures: dict[str, object] = {
        "monkeypatch": monkeypatch,
        "capsys": capsys,
        "tmp_path": tmp_path,
    }
    kwargs = {
        parameter: available_fixtures[parameter]
        for parameter in inspect.signature(test_function).parameters
    }
    test_function(**kwargs)


def _bind_synthetic_v8_manifest(
    monkeypatch: pytest.MonkeyPatch,
    manifest: dict[str, object],
) -> None:
    """Bypass only retained manifest-file admission for a public CLI control."""
    monkeypatch.setattr(
        generator,
        "_read_bound_json",
        lambda _path, _expected_sha: (SYNTHETIC_MEMBERS_SHA256, manifest),
    )


def test_public_synthetic_cli_mismatch_fails_before_coverage_and_writes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Exercise the real CLI ordering with a controlled manifest and stubbed input admission."""
    _bind_synthetic_v8_manifest(monkeypatch, _synthetic_manifest(tmp_path))
    _RETAINED_TESTS.test_measured_member_mismatch_fails_through_cli_before_publication(
        monkeypatch, capsys, tmp_path
    )


def test_public_synthetic_cli_binds_manifest_before_legacy_and_archive_access(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Exercise manifest-before-legacy ordering without retained bytes or archive access."""
    root = REPOSITORY.resolve()
    for module in (generator, coverage, generator.sec, _RETAINED_TESTS):
        assert Path(module.__file__).resolve().is_relative_to(root)

    manifest = _synthetic_manifest(tmp_path)
    expected_manifest_path = (
        REPOSITORY / "docs" / "issue-70-q4-source-sample-v8" / "fundamentals_provenance.json"
    )
    expected_manifest_sha256 = generator.V9_V8_SOURCE_MANIFEST_SHA256
    legacy_root = tmp_path / "missing-legacy"
    legacy_import_path = legacy_root / "import-provenance.json"
    events: list[tuple[str, Path, str | None]] = []

    def read_synthetic_manifest(path: Path, expected_sha256: str) -> tuple[str, dict[str, object]]:
        normalized_path = Path(path)
        events.append(("manifest", normalized_path, expected_sha256))
        assert normalized_path == expected_manifest_path
        assert expected_sha256 == expected_manifest_sha256
        return SYNTHETIC_MEMBERS_SHA256, manifest

    original_json_file = generator._json_file

    def record_legacy_json_access(path: Path) -> object:
        normalized_path = Path(path)
        if normalized_path.is_relative_to(legacy_root):
            assert events == [("manifest", expected_manifest_path, expected_manifest_sha256)]
            events.append(("legacy_json", normalized_path, None))
        return original_json_file(normalized_path)

    monkeypatch.setattr(generator, "_read_bound_json", read_synthetic_manifest)
    monkeypatch.setattr(generator, "_json_file", record_legacy_json_access)
    _RETAINED_TESTS.test_v9_cli_source_path_binds_manifest_before_retained_source_access(
        monkeypatch, capsys, tmp_path
    )
    assert events == [
        ("manifest", expected_manifest_path, expected_manifest_sha256),
        ("legacy_json", legacy_import_path, None),
    ]


def test_public_synthetic_calendar_hash_and_date_boundaries(tmp_path: Path) -> None:
    calendar_bytes, calendar_path = _canonical_fixture_copy(
        SYNTHETIC_CALENDAR, tmp_path / SYNTHETIC_CALENDAR.name
    )
    assert len(calendar_bytes) == SYNTHETIC_CALENDAR_BYTES
    assert hashlib.sha256(calendar_bytes).hexdigest() == SYNTHETIC_CALENDAR_SHA256
    assert generator._validate_bound_file(
        calendar_path,
        SYNTHETIC_CALENDAR_SHA256,
        expected_bytes=SYNTHETIC_CALENDAR_BYTES,
    ) == SYNTHETIC_CALENDAR_SHA256
    assert generator._calendar_dates(
        calendar_path, expected_header=("trade_date",)
    ) == (date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6))

    changed = tmp_path / "changed.csv"
    changed.write_bytes(calendar_bytes + b"\n")
    with pytest.raises(ValueError, match="digest differs from its receipt"):
        generator._validate_bound_file(changed, SYNTHETIC_CALENDAR_SHA256)

    for contents in (
        "trade_date\n2025-01-03\n2025-01-02\n",
        "trade_date\n2025-01-02\n2025-01-02\n",
        "trade_date\nnot-a-date\n",
    ):
        invalid = tmp_path / f"invalid-{hashlib.sha256(contents.encode()).hexdigest()[:8]}.csv"
        invalid.write_text(contents, encoding="utf-8")
        with pytest.raises(ValueError, match="invalid date|nonempty, unique, and sorted"):
            generator._calendar_dates(invalid, expected_header=("trade_date",))


def test_public_synthetic_member_binding_matches_and_ignores_read_counts(tmp_path: Path) -> None:
    manifest = _synthetic_manifest(tmp_path)
    selected = _synthetic_selected(manifest)
    baseline = generator._validate_v8_selected_member_hashes(manifest, selected)
    assert baseline["status"] == "matched"
    assert baseline["selected_member_count"] == 14  # Test-only contract fixture size.
    assert baseline["selected_members_by_archive"] == {"submissions": 10, "companyfacts": 4}
    assert len(baseline["selected_members"]) == 14
    assert any(row["member_name"] == "CIK9999900000.json" for row in selected["submissions"])
    assert any(row["member_name"] == "CIK9999900000.json" for row in selected["companyfacts"])

    with_read_counts = copy.deepcopy(selected)
    for rows in with_read_counts.values():
        for row in rows:
            row["read_passes"] = 987
            row["preflight_sha256"] = "f" * 64
    assert generator._validate_v8_selected_member_hashes(manifest, with_read_counts) == baseline


def test_public_synthetic_member_binding_rejects_identity_negatives(tmp_path: Path) -> None:
    manifest = _synthetic_manifest(tmp_path)
    selected = _synthetic_selected(manifest)

    expected_duplicate = copy.deepcopy(manifest)
    expected_rows = expected_duplicate["source_budgets"]["selected_member_hashes"]["submissions"]
    expected_rows.append(dict(expected_rows[0]))
    with pytest.raises(ValueError, match="identity is duplicated"):
        generator._validate_v8_selected_member_hashes(expected_duplicate, selected)

    measured_duplicate = copy.deepcopy(selected)
    measured_duplicate["submissions"].append(dict(measured_duplicate["submissions"][0]))
    with pytest.raises(ValueError, match="identity is duplicated"):
        generator._validate_v8_selected_member_hashes(manifest, measured_duplicate)

    malformed_expected = copy.deepcopy(manifest)
    malformed_expected["source_budgets"]["selected_member_hashes"]["companyfacts"][0]["sha256"] = "A" * 64
    with pytest.raises(ValueError, match="identity is malformed"):
        generator._validate_v8_selected_member_hashes(malformed_expected, selected)

    malformed_name = copy.deepcopy(selected)
    malformed_name["submissions"][0]["member_name"] = "../CIK9999900000.json"
    with pytest.raises(ValueError, match="identity is malformed"):
        generator._validate_v8_selected_member_hashes(manifest, malformed_name)

    malformed_length = copy.deepcopy(selected)
    malformed_length["companyfacts"][0]["expanded_bytes"] = True
    with pytest.raises(ValueError, match="identity is malformed"):
        generator._validate_v8_selected_member_hashes(manifest, malformed_length)

    for mutate in (
        lambda value: value["companyfacts"][0].update(sha256="0" * 64),
        lambda value: value["submissions"][0].update(expanded_bytes=999),
    ):
        changed = copy.deepcopy(selected)
        mutate(changed)
        with pytest.raises(ValueError, match="digest or expanded length differs"):
            generator._validate_v8_selected_member_hashes(manifest, changed)

    missing = copy.deepcopy(selected)
    missing["submissions"].pop()
    with pytest.raises(ValueError, match="namespaces differ"):
        generator._validate_v8_selected_member_hashes(manifest, missing)

    extra = copy.deepcopy(selected)
    extra["companyfacts"].append({
        "member_name": "CIK9999999999.json", "sha256": "1" * 64, "expanded_bytes": 1,
    })
    with pytest.raises(ValueError, match="namespaces differ"):
        generator._validate_v8_selected_member_hashes(manifest, extra)

    renamed = copy.deepcopy(selected)
    renamed["companyfacts"][0]["member_name"] = "CIK9999999999.json"
    with pytest.raises(ValueError, match="namespaces differ"):
        generator._validate_v8_selected_member_hashes(manifest, renamed)

    moved_between_namespaces = copy.deepcopy(selected)
    submissions_collision = moved_between_namespaces["submissions"][0]
    companyfacts_collision = moved_between_namespaces["companyfacts"][0]
    submissions_collision["sha256"], companyfacts_collision["sha256"] = (
        companyfacts_collision["sha256"], submissions_collision["sha256"]
    )
    submissions_collision["expanded_bytes"], companyfacts_collision["expanded_bytes"] = (
        companyfacts_collision["expanded_bytes"], submissions_collision["expanded_bytes"]
    )
    with pytest.raises(ValueError, match="digest or expanded length differs"):
        generator._validate_v8_selected_member_hashes(manifest, moved_between_namespaces)


def test_public_synthetic_cap_accounting_counts_complete_nested_summary() -> None:
    coverage_artifact = {"record_count": 2, "uncompressed_byte_length": 37}
    summary: dict[str, object] = {
        "annual_fiscal_year_gap_slot_count": 1,
        "annual_fiscal_year_gap_slots": [{"slot_id": "synthetic-gap"}],
    }
    comparison = {
        "expected_slot_count": 1,
        "by_slot_id": {
            "synthetic-slot": {
                "origin_evidence": {
                    "window_candidates_by_origin_id": {
                        "synthetic-origin": {"decision": "admitted_visible"}
                    }
                }
            }
        },
    }
    complete = generator._complete_v9_coverage_summary(summary, coverage_artifact, comparison, {})
    measured = generator._validate_combined_v9_evidence_caps(
        coverage_artifact,
        complete,
        max_records=5,
        max_uncompressed_bytes=1_000_000,
    )
    assert measured["combined_record_count"] == 5
    assert measured["origin_window_decision_count"] == 1
    assert measured["annual_gap_diagnostic_count"] == 1
    assert measured["financial_summary_bytes"] == len(generator._json_bytes(complete))
    assert measured["combined_canonical_uncompressed_bytes"] == 37 + len(generator._json_bytes(complete))

    with pytest.raises(ValueError, match="configured .*record cap"):
        generator._validate_combined_v9_evidence_caps(
            coverage_artifact, complete, max_records=4, max_uncompressed_bytes=1_000_000
        )
    exact_bytes = measured["combined_canonical_uncompressed_bytes"]
    with pytest.raises(ValueError, match="configured .*byte.*cap"):
        generator._validate_combined_v9_evidence_caps(
            coverage_artifact, complete, max_records=5, max_uncompressed_bytes=exact_bytes - 1
        )


def test_public_annual_gap_diagnostic_keeps_the_accepted_matched_slot() -> None:
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
    assert (gap_slot["matched_period_end"], gap_slot["comparison_period_end"]) == (
        "2020-12-31", "2018-12-31"
    )
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
    assert (revenue_gap_slot["matched_period_end"], revenue_gap_slot["comparison_period_end"]) == (
        "2020-12-31", "2018-12-31"
    )
    assert summary["annual_fiscal_year_gap_slot_count"] == 2
    assert any(
        row["slot_id"] == revenue_gap_slot["slot_id"]
        for row in summary["annual_fiscal_year_gap_slots"]
    )


def test_public_projection_reselects_a_visible_same_period_vintage() -> None:
    records, origins, calendar, evaluation_session = _projection_fixture()
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
        if row["record_kind"] == "field_session" and row["slot_metric"] == "basic_eps"
    )
    alternative_row = coverage._origin_row(slot_row=field_slot, item=alternative, role="field_origin")
    records.append(alternative_row)
    origins.append(alternative)

    comparison = _project(records, origins, calendar, evaluation_session)
    target = next(
        row for row in records
        if row["record_kind"] == "expected_slot"
        and row["feature_id"] == "annual_eps_growth"
        and row["slot_metric"] == "annual_basic_eps_growth"
        and row["slot_number"] == "3"
    )
    projected = comparison["by_slot_id"][target["slot_id"]]
    assert projected["classification"] == "admitted_vintage_changed"
    assert projected["current"]["matched_period_end"] == "2019-12-31"
    assert projected["current"]["comparison_period_end"] == "2018-12-31"
    assert projected["v8_source_window_projection"]["matched_period_end"] == "2019-12-31"
    assert projected["v8_source_window_projection"]["comparison_period_end"] == "2018-12-31"
    assert alternative_row["origin_id"] in projected["origin_evidence"]["baseline_selected_origin_ids"]
    assert projected["origin_evidence"]["window_candidates_by_origin_id"][alternative_row["origin_id"]] == (
        "admitted_visible"
    )


def test_public_roe_income_endpoint_change_is_natural_anchor_with_same_equity_endpoint() -> None:
    records, origins, calendar, evaluation_session = _annual_roe_fixture()
    comparison = _project(records, origins, calendar, evaluation_session)
    current_slot = next(
        row for row in records if row["record_kind"] == "expected_slot" and row["feature_id"] == "annual_roe"
    )
    projected = comparison["by_slot_id"][current_slot["slot_id"]]
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
