"""Offline contract tests for bounded Norgate sample retention and V3 projection."""

from __future__ import annotations

import builtins
import csv
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from build_pit_bundle import _load_membership_v3
from export_norgate_development_sample import (
    CaptureFailure,
    EXPECTED_READS,
    capture_native_generation,
    main,
    project_development_sample,
)


PRICE_DATES = ("2026-09-01", "2026-09-03")
CALENDAR_DATES = ("2026-09-01", "2026-09-02", "2026-09-03")


def _prices() -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "open": [100.0, 102.0],
            "high": [103.0, 105.0],
            "low": [99.0, 101.0],
            "close": [102.0, 104.0],
            "volume": [1000, 1200],
        },
        index=pd.to_datetime(PRICE_DATES),
    )
    frame.index.name = "date"
    return frame


class FakeNorgateProvider:
    """Test-only provider double; every value it returns is labeled synthetic."""

    source_class = "synthetic_test_fixture"

    class StockPriceAdjustmentType:
        CAPITAL = "CAPITAL"

    class PaddingType:
        NONE = "NONE"

    def __init__(
        self,
        *,
        membership_dates: tuple[str, ...] = PRICE_DATES,
        missing_membership_date: str | None = None,
        fail_on: str | None = None,
        status_result: bool = True,
        requested_asset_id: object = 123456,
        resolved_symbol: object = "MSFT",
        roundtrip_asset_id: object = 123456,
    ) -> None:
        self.calls: list[tuple[str, object]] = []
        self.membership_dates = tuple(
            value for value in membership_dates if value != missing_membership_date
        )
        self.fail_on = fail_on
        self.status_result = status_result
        self.requested_asset_id = requested_asset_id
        self.resolved_symbol = resolved_symbol
        self.roundtrip_asset_id = roundtrip_asset_id
        self.assetid_calls = 0

    def _record(self, name: str, detail: object = None) -> None:
        self.calls.append((name, detail))
        if self.fail_on == name:
            raise RuntimeError(f"synthetic failure at {name}")

    def status(self) -> bool:
        self._record("status")
        return self.status_result

    def assetid(self, symbol: str) -> object:
        self._record("assetid", symbol)
        self.assetid_calls += 1
        return self.requested_asset_id if self.assetid_calls == 1 else self.roundtrip_asset_id

    def symbol(self, asset_id: int) -> object:
        self._record("symbol", asset_id)
        return self.resolved_symbol

    def price_timeseries(
        self,
        asset_id: int,
        *,
        stock_price_adjustment_setting: str,
        padding_setting: str,
        start_date: str,
        end_date: str,
        timeseriesformat: str,
    ) -> pd.DataFrame:
        self._record(
            "price_timeseries",
            {
                "asset_id": asset_id,
                "stock_price_adjustment_setting": stock_price_adjustment_setting,
                "padding_setting": padding_setting,
                "start_date": start_date,
                "end_date": end_date,
                "timeseriesformat": timeseriesformat,
            },
        )
        return _prices().copy()

    def index_constituent_timeseries(
        self,
        asset_id: int,
        index_name: str,
        *,
        padding_setting: str,
        pandas_dataframe: pd.DataFrame,
        timeseriesformat: str,
    ) -> pd.DataFrame:
        self._record(
            "index_constituent_timeseries",
            {
                "asset_id": asset_id,
                "index_name": index_name,
                "padding_setting": padding_setting,
                "seed_dates": tuple(pd.Timestamp(value).date().isoformat() for value in pandas_dataframe.index),
                "timeseriesformat": timeseriesformat,
            },
        )
        states = {
            "$SPX": {"2026-09-01": True, "2026-09-02": True, "2026-09-03": True},
            "$NDX": {"2026-09-01": False, "2026-09-02": True, "2026-09-03": True},
            "$RUT": {"2026-09-01": False, "2026-09-02": False, "2026-09-03": False},
        }[index_name]
        seed_dates = tuple(pd.Timestamp(value).date().isoformat() for value in pandas_dataframe.index)
        dates = tuple(value for value in seed_dates if value in self.membership_dates)
        result = pd.DataFrame({"member": [states[value] for value in dates]}, index=pd.to_datetime(dates))
        result.index.name = "date"
        return result


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _synthetic_acquisition_metadata(evidence_root: Path) -> dict[str, object]:
    evidence_root.mkdir(parents=True, exist_ok=True)
    setup_evidence = evidence_root / "acquisition-source.bin"
    setup_evidence.write_bytes(b"SYNTHETIC_TEST_FIXTURE_ONLY: acquisition context")
    updater_executable = evidence_root / "norgate-updater-test.exe"
    updater_executable.write_bytes(b"SYNTHETIC_TEST_FIXTURE_ONLY: updater executable bytes")
    return {
        "schema": "norgate_acquisition_context/v1",
        "source_class": "synthetic_test_fixture",
        "product_name": "Synthetic Norgate test context",
        "norgate_data_updater_version": "test-version",
        "database_identity": "synthetic-database-id",
        "entitlement": {
            "status": "confirmed_active",
            "date_start": "2026-09-01",
            "date_end": "2026-09-30",
        },
        "update": {
            "status": "completed",
            "generation_id": "synthetic-generation-id",
            "completed_at_utc": "2026-09-01T00:00:00Z",
            "coherence_status": "single_generation_confirmed",
        },
        "retention_basis": "synthetic test temporary directory only",
        "updater_executable_path": updater_executable.name,
        "updater_executable_sha256": _sha(updater_executable),
        "source_evidence": [
            {
                "path": setup_evidence.name,
                "sha256": _sha(setup_evidence),
                "source_locator": "Synthetic test fixture only",
            }
        ],
    }


class CaptureTests(unittest.TestCase):
    def test_capture_retains_returned_rows_hashes_and_fixed_eight_read_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "private"
            evidence_root = Path(temp) / "acquisition-evidence"
            acquisition_metadata = _synthetic_acquisition_metadata(evidence_root)
            provider = FakeNorgateProvider()
            generation = capture_native_generation(
                provider,
                start_date="2026-09-01",
                end_date="2026-09-30",
                private_root=root,
                sdk_version="1.0.77-test",
                acquisition_metadata=acquisition_metadata,
                acquisition_evidence_root=evidence_root,
            )

            manifest = json.loads((generation / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(provider.calls), 8)
            self.assertEqual(manifest["sdk_reads_planned"], 8)
            self.assertEqual(manifest["sdk_reads_attempted"], 8)
            self.assertEqual(manifest["sdk_reads_completed"], 8)
            self.assertEqual(manifest["source_class"], "synthetic_test_fixture")
            self.assertFalse(manifest["production_admission"])
            self.assertEqual(manifest["sdk_version"], "1.0.77-test")
            self.assertEqual(manifest["query"]["start_date"], "2026-09-01")
            self.assertEqual(manifest["query"]["end_date"], "2026-09-30")
            self.assertEqual(manifest["query"]["adjustment_setting"], "CAPITAL")
            self.assertEqual(manifest["query"]["padding_setting"], "NONE")
            self.assertEqual(manifest["acquisition_context"]["status"], "hash_bound_context_complete")
            self.assertEqual(manifest["acquisition_context"]["capture_coherence"]["independent_verification"], False)
            self.assertEqual(len(manifest["acquisition_context"]["capture_executable_sha256"]), 64)
            self.assertTrue(manifest["acquisition_context"]["updater_executable_hash_verified"])

            price_record = manifest["native_frames"]["prices"]
            price_path = generation / price_record["path"]
            self.assertEqual(price_record["rows"], 2)
            self.assertEqual(price_record["sha256"], _sha(price_path))
            retained_prices = pd.read_csv(price_path)
            self.assertEqual(retained_prices["session_date"].tolist(), list(PRICE_DATES))
            self.assertEqual(retained_prices["close"].tolist(), [102.0, 104.0])

            for index_name in ("$SPX", "$NDX", "$RUT"):
                record = manifest["native_frames"]["membership"][index_name]
                path = generation / record["path"]
                self.assertEqual(record["sha256"], _sha(path))
                self.assertEqual(record["rows"], 2)
                self.assertEqual(pd.read_csv(path)["session_date"].tolist(), list(PRICE_DATES))

            membership_calls = [detail for name, detail in provider.calls if name == "index_constituent_timeseries"]
            self.assertEqual([item["index_name"] for item in membership_calls], ["$SPX", "$NDX", "$RUT"])
            self.assertTrue(all(item["seed_dates"] == PRICE_DATES for item in membership_calls))
            price_query = next(detail for name, detail in provider.calls if name == "price_timeseries")
            self.assertEqual(price_query["stock_price_adjustment_setting"], "CAPITAL")

    def test_capture_rejects_more_than_31_calendar_days_before_any_read(self) -> None:
        provider = FakeNorgateProvider()
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "31 calendar days"):
                capture_native_generation(
                    provider,
                    start_date="2026-09-01",
                    end_date="2026-10-02",
                    private_root=Path(temp) / "private",
                    sdk_version="1.0.77-test",
                )
        self.assertEqual(provider.calls, [])

    def test_capture_failure_persists_stage_and_exact_read_counts_without_retry(self) -> None:
        provider = FakeNorgateProvider(fail_on="price_timeseries")
        with tempfile.TemporaryDirectory() as temp:
            evidence_root = Path(temp) / "acquisition-evidence"
            acquisition_metadata = _synthetic_acquisition_metadata(evidence_root)
            with self.assertRaises(CaptureFailure) as caught:
                capture_native_generation(
                    provider,
                    start_date="2026-09-01",
                    end_date="2026-09-30",
                    private_root=Path(temp) / "private",
                    sdk_version="1.0.77-test",
                    acquisition_metadata=acquisition_metadata,
                    acquisition_evidence_root=evidence_root,
                )
            manifest = json.loads((caught.exception.generation_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([name for name, _ in provider.calls], ["status", "assetid", "symbol", "assetid", "price_timeseries"])
        self.assertEqual(manifest["capture_status"], "failed")
        self.assertEqual(manifest["failure"]["stage"], "price_timeseries")
        self.assertEqual(manifest["sdk_reads_attempted"], 5)
        self.assertEqual(manifest["sdk_reads_completed"], 4)
        self.assertEqual(manifest["retry_count"], 0)

    def test_bad_status_or_identity_stops_before_any_following_sdk_reads(self) -> None:
        cases = (
            (FakeNorgateProvider(status_result=False), ["status"], "status"),
            (FakeNorgateProvider(requested_asset_id=0), ["status", "assetid"], "assetid_requested_symbol"),
            (FakeNorgateProvider(resolved_symbol=""), ["status", "assetid", "symbol"], "symbol_for_asset"),
            (
                FakeNorgateProvider(roundtrip_asset_id=654321),
                ["status", "assetid", "symbol", "assetid"],
                "assetid_resolved_symbol",
            ),
        )
        for provider, expected_calls, expected_stage in cases:
            with self.subTest(expected_stage=expected_stage), tempfile.TemporaryDirectory() as temp:
                with self.assertRaises(CaptureFailure) as caught:
                    capture_native_generation(
                        provider,
                        start_date="2026-09-01",
                        end_date="2026-09-30",
                        private_root=Path(temp) / "private",
                        sdk_version="1.0.77-test",
                    )
                manifest = json.loads((caught.exception.generation_dir / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual([name for name, _ in provider.calls], expected_calls)
                self.assertEqual(manifest["failure"]["stage"], expected_stage)
                self.assertLess(manifest["sdk_reads_attempted"], EXPECTED_READS)

    def test_default_capture_is_no_call_and_does_not_import_sdk(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = io.StringIO()
            original_import = builtins.__import__

            def guarded_import(name: str, *args: object, **kwargs: object) -> object:
                if name == "norgatedata" or name.startswith("norgatedata."):
                    raise AssertionError("default capture imported the Norgate SDK")
                return original_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=guarded_import), redirect_stdout(output):
                result = main(
                    [
                        "capture",
                        "--start-date",
                        "2026-09-01",
                        "--end-date",
                        "2026-09-30",
                        "--private-root",
                        str(Path(temp) / "private"),
                    ]
                )
        self.assertEqual(result, 0)
        self.assertIn("DRY RUN", output.getvalue())
        self.assertIn("8", output.getvalue())


class ProjectionTests(unittest.TestCase):
    def _inputs(
        self,
        temp: Path,
        *,
        membership_dates: tuple[str, ...] = PRICE_DATES,
        include_no_price_membership: bool = False,
    ) -> tuple[Path, Path, dict[str, object], dict[str, object], Path]:
        evidence_root = temp / "source-evidence"
        evidence_root.mkdir()
        acquisition_metadata = _synthetic_acquisition_metadata(evidence_root)
        generation = capture_native_generation(
            FakeNorgateProvider(membership_dates=membership_dates),
            start_date="2026-09-01",
            end_date="2026-09-30",
            private_root=temp / "private-input",
            sdk_version="1.0.77-test",
            acquisition_metadata=acquisition_metadata,
            acquisition_evidence_root=evidence_root,
        )
        if include_no_price_membership:
            self._extend_with_offline_no_price_membership(generation)
        calendar_evidence = evidence_root / "calendar-source.bin"
        calendar_evidence.write_bytes(b"SYNTHETIC_TEST_FIXTURE_ONLY: independent-session-calendar")
        identity_evidence = evidence_root / "identity-source.bin"
        identity_evidence.write_bytes(b"SYNTHETIC_TEST_FIXTURE_ONLY: asset-to-lineage evidence")

        calendar_csv = temp / "calendar.csv"
        with calendar_csv.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(("session_date",))
            writer.writerows((value,) for value in CALENDAR_DATES)
        calendar_provenance: dict[str, object] = {
            "schema": "independent_calendar_provenance/v1",
            "source_class": "synthetic_test_fixture",
            "calendar_id": "synthetic-three-session-calendar",
            "calendar_csv_sha256": _sha(calendar_csv),
            "market_id": "synthetic-us-equities-calendar",
            "window_start_date": "2026-09-01",
            "window_end_date": "2026-09-30",
            "full_session_coverage": True,
            "coverage_status": "full_market_sessions",
            "declared_session_dates": list(CALENDAR_DATES),
            "declared_session_count": len(CALENDAR_DATES),
            "coverage_review_status": "synthetic_fixture_only",
            "source_evidence_path": calendar_evidence.name,
            "source_evidence_sha256": _sha(calendar_evidence),
            "source_locator": "Synthetic test fixture only",
        }
        identity_binding: dict[str, object] = {
            "schema": "norgate_asset_identity_binding/v1",
            "source_class": "synthetic_test_fixture",
            "assertion_review_status": "synthetic_fixture_only",
            "provider": "Norgate",
            "asset_bindings": [
                {
                    "provider_asset_id": 123456,
                    "provider_symbol": "MSFT",
                    "ticker": "MSFT",
                    "security_lineage_id": "msft_common",
                    "share_class": "common",
                    "same_security_assertion": True,
                    "effective_start": CALENDAR_DATES[0],
                    "effective_end": CALENDAR_DATES[-1],
                    "source_url": "https://example.invalid/synthetic-identity-source",
                    "source_document_date": "2026-09-01",
                    "source_evidence_path": identity_evidence.name,
                    "source_evidence_sha256": _sha(identity_evidence),
                    "source_raw_sha256": _sha(identity_evidence),
                    "source_locator": "Synthetic test fixture only",
                }
            ],
        }
        return generation, calendar_csv, calendar_provenance, identity_binding, evidence_root

    def _extend_with_offline_no_price_membership(self, generation: Path) -> None:
        """Add clearly synthetic full-calendar states to retained fixture frames offline."""
        states = {"$SPX": "True", "$NDX": "True", "$RUT": "False"}
        manifest_path = generation / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for index_name, value in states.items():
            record = manifest["native_frames"]["membership"][index_name]
            path = generation / record["path"]
            with path.open("r", encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            rows.append({"session_date": "2026-09-02", "member": value})
            rows.sort(key=lambda row: row["session_date"])
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=("session_date", "member"), lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
            record["rows"] = len(rows)
            record["sha256"] = _sha(path)
        manifest["offline_fixture_annotation"] = "SYNTHETIC_TEST_FIXTURE_ONLY: added no-price membership states"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def _append_membership_fixture_row(
        self, generation: Path, index_name: str, session_date: str, member: str
    ) -> None:
        manifest_path = generation / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        record = manifest["native_frames"]["membership"][index_name]
        path = generation / record["path"]
        with path.open("r", encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        rows.append({"session_date": session_date, "member": member})
        rows.sort(key=lambda row: row["session_date"])
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=("session_date", "member"), lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        record["rows"] = len(rows)
        record["sha256"] = _sha(path)
        manifest["offline_fixture_annotation"] = "SYNTHETIC_TEST_FIXTURE_ONLY: malformed membership row"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def _rewrite_native_session_date(
        self,
        generation: Path,
        *,
        old_date: str,
        new_date: str,
        index_name: str | None = None,
    ) -> None:
        manifest_path = generation / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if index_name is None:
            record = manifest["native_frames"]["prices"]
        else:
            record = manifest["native_frames"]["membership"][index_name]
        path = generation / record["path"]
        with path.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            fieldnames = reader.fieldnames
            rows = list(reader)
        target = next(row for row in rows if row["session_date"] == old_date)
        target["session_date"] = new_date
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        record["sha256"] = _sha(path)
        manifest["offline_fixture_annotation"] = "SYNTHETIC_TEST_FIXTURE_ONLY: session-date parser control"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def test_project_preserves_membership_events_and_reports_no_price_membership(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            generation, calendar_csv, calendar_provenance, identity_binding, evidence_root = self._inputs(
                temp, include_no_price_membership=True
            )
            result = project_development_sample(
                generation,
                calendar_csv=calendar_csv,
                calendar_provenance=calendar_provenance,
                identity_binding=identity_binding,
                evidence_root=evidence_root,
                member_column="member",
                output_dir=temp / "private-output",
            )

            report = json.loads(result["quality_report_path"].read_text(encoding="utf-8"))
            self.assertEqual(report["projection_status"], "partial_development_projection")
            self.assertFalse(report["production_admission"])
            self.assertEqual(report["source_classes"], ["synthetic_test_fixture"])
            self.assertEqual(report["basis"]["setting"], "CAPITAL")
            self.assertEqual(report["basis"]["status"], "known_but_unsupported_by_v3_schema")
            self.assertFalse(report["basis"]["v3_price_output_allowed"])
            self.assertIn("not established as split-only", report["basis"]["limitation"].lower())
            self.assertIsNone(result["prices_v3_path"])
            self.assertFalse((temp / "private-output" / "prices_v3.csv").exists())
            self.assertIsNotNone(result["membership_v3_path"])
            self.assertEqual(report["identity"]["status"], "synthetic_fixture_identity_assertion")
            self.assertEqual(report["identity"]["assertion"]["share_class"], "common")
            self.assertEqual(report["calendar"]["market_id"], "synthetic-us-equities-calendar")
            self.assertTrue(report["calendar"]["full_session_coverage"])
            self.assertTrue(report["membership_capture_semantics"]["seeded_from_price_frame"])
            self.assertFalse(report["membership_capture_semantics"]["independent_calendar_retrieval"])
            self.assertEqual(report["membership_capture_semantics"]["seed_session_dates"], list(PRICE_DATES))
            self.assertEqual(report["missingness"]["price_session_dates"], ["2026-09-02"])
            self.assertEqual(
                report["missingness"]["member_without_price_sessions"],
                {"nasdaq100": ["2026-09-02"], "sp500": ["2026-09-02"]},
            )

            membership_rows = _load_membership_v3(result["membership_v3_path"], "2026-09-03")
            self.assertEqual(
                membership_rows,
                [
                    ("2026-09-01", "msft_common", "sp500", 1),
                    ("2026-09-02", "msft_common", "nasdaq100", 1),
                ],
            )
            self.assertEqual(report["evaluator_readiness"]["status"], "NOT_READY")
            self.assertTrue(
                {
                    "financials_and_provenance",
                    "industry_and_provenance",
                    "feature_snapshot",
                    "continuous_warmup",
                    "benchmark_market_context",
                    "matched_strategy_baseline",
                }.issubset(set(report["evaluator_readiness"]["missing_inputs"]))
            )

    def test_missing_membership_calendar_state_stays_unknown_and_blocks_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            generation, calendar_csv, calendar_provenance, identity_binding, evidence_root = self._inputs(
                temp, membership_dates=PRICE_DATES
            )
            result = project_development_sample(
                generation,
                calendar_csv=calendar_csv,
                calendar_provenance=calendar_provenance,
                identity_binding=identity_binding,
                evidence_root=evidence_root,
                member_column="member",
                output_dir=temp / "private-output",
            )
            report = json.loads(result["quality_report_path"].read_text(encoding="utf-8"))
            self.assertIsNone(result["prices_v3_path"])
            self.assertIsNone(result["membership_v3_path"])
            self.assertFalse((temp / "private-output" / "membership_v3.csv").exists())
            self.assertEqual(report["projection_status"], "blocked")
            self.assertEqual(
                report["membership_by_universe"]["sp500"]["unknown_session_dates"],
                ["2026-09-02"],
            )
            self.assertEqual(
                report["membership_by_universe"]["sp500"]["status"],
                "blocked_unknown_calendar_sessions",
            )

    def test_failed_capture_blocks_membership_and_price_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            generation, calendar_csv, calendar_provenance, identity_binding, evidence_root = self._inputs(
                temp, include_no_price_membership=True
            )
            manifest_path = generation / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["capture_status"] = "failed"
            manifest["failure"] = {"stage": "membership:$NDX", "message": "synthetic test failure"}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = project_development_sample(
                generation,
                calendar_csv=calendar_csv,
                calendar_provenance=calendar_provenance,
                identity_binding=identity_binding,
                evidence_root=evidence_root,
                member_column="member",
                output_dir=temp / "private-output",
            )
            report = json.loads(result["quality_report_path"].read_text(encoding="utf-8"))
            self.assertIsNone(result["prices_v3_path"])
            self.assertIsNone(result["membership_v3_path"])
            self.assertFalse((temp / "private-output" / "membership_v3.csv").exists())
            self.assertEqual(report["projection_status"], "blocked")
            self.assertIn("native_capture_incomplete", report["blockers"])
            self.assertTrue(
                all(
                    item["status"] == "blocked_invalid_capture_generation"
                    for item in report["membership_by_universe"].values()
                )
            )

    def test_unverified_acquisition_context_blocks_projection_but_keeps_quality_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            generation, calendar_csv, calendar_provenance, identity_binding, evidence_root = self._inputs(
                temp, include_no_price_membership=True
            )
            (generation / "acquisition_metadata.json").unlink()
            result = project_development_sample(
                generation,
                calendar_csv=calendar_csv,
                calendar_provenance=calendar_provenance,
                identity_binding=identity_binding,
                evidence_root=evidence_root,
                member_column="member",
                output_dir=temp / "private-output",
            )
            report = json.loads(result["quality_report_path"].read_text(encoding="utf-8"))
            self.assertIsNone(result["prices_v3_path"])
            self.assertIsNone(result["membership_v3_path"])
            self.assertEqual(report["acquisition_context"]["capture_coherence"]["status"], "unknown_unverified")
            self.assertIn("acquisition_context_unverified", report["blockers"])
            self.assertEqual(report["native_row_counts"]["prices"], 2)
            self.assertEqual(report["evaluator_readiness"]["status"], "NOT_READY")

    def test_project_refuses_reused_output_directory_instead_of_leaving_stale_v3_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            generation, calendar_csv, calendar_provenance, identity_binding, evidence_root = self._inputs(
                temp, include_no_price_membership=True
            )
            output_dir = temp / "private-output"
            first = project_development_sample(
                generation,
                calendar_csv=calendar_csv,
                calendar_provenance=calendar_provenance,
                identity_binding=identity_binding,
                evidence_root=evidence_root,
                member_column="member",
                output_dir=output_dir,
            )
            manifest_path = generation / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["query"]["adjustment_setting"] = "UNSPECIFIED"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "refusing stale projection files"):
                project_development_sample(
                    generation,
                    calendar_csv=calendar_csv,
                    calendar_provenance=calendar_provenance,
                    identity_binding=identity_binding,
                    evidence_root=evidence_root,
                    member_column="member",
                    output_dir=output_dir,
                )
            report = json.loads(first["quality_report_path"].read_text(encoding="utf-8"))
            self.assertEqual(report["projection_status"], "partial_development_projection")
            self.assertIsNone(first["prices_v3_path"])
            self.assertTrue(Path(first["membership_v3_path"]).is_file())

    def test_offline_project_cli_uses_explicit_inputs_and_writes_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            generation, calendar_csv, calendar_provenance, identity_binding, evidence_root = self._inputs(
                temp, include_no_price_membership=True
            )
            calendar_json = temp / "calendar-provenance.json"
            identity_json = temp / "identity-binding.json"
            calendar_json.write_text(json.dumps(calendar_provenance), encoding="utf-8")
            identity_json.write_text(json.dumps(identity_binding), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                result = main(
                    [
                        "project",
                        "--generation",
                        str(generation),
                        "--calendar-csv",
                        str(calendar_csv),
                        "--calendar-provenance",
                        str(calendar_json),
                        "--identity-binding",
                        str(identity_json),
                        "--evidence-root",
                        str(evidence_root),
                        "--member-column",
                        "member",
                        "--output-dir",
                        str(temp / "cli-output"),
                    ]
                )
            self.assertEqual(result, 0)
            self.assertIn("partial_development_projection", output.getvalue())
            self.assertTrue((temp / "cli-output" / "native_quality_report.json").is_file())

    def test_uncovered_identity_date_blocks_price_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            generation, calendar_csv, calendar_provenance, identity_binding, evidence_root = self._inputs(
                temp, include_no_price_membership=True
            )
            identity_binding["asset_bindings"][0]["effective_end"] = "2026-09-02"  # type: ignore[index]
            result = project_development_sample(
                generation,
                calendar_csv=calendar_csv,
                calendar_provenance=calendar_provenance,
                identity_binding=identity_binding,
                evidence_root=evidence_root,
                member_column="member",
                output_dir=temp / "private-output",
            )
            report = json.loads(result["quality_report_path"].read_text(encoding="utf-8"))
            self.assertEqual(report["projection_status"], "blocked")
            self.assertIsNone(result["prices_v3_path"])
            self.assertEqual(report["identity"]["unresolved_dates"], ["2026-09-03"])
            self.assertEqual(report["identity"]["status"], "unresolved_source_identity_assertion")
            self.assertEqual(
                report["membership_by_universe"]["sp500"]["status"],
                "blocked_unresolved_identity",
            )
            self.assertEqual(
                report["membership_by_universe"]["sp500"]["unresolved_identity_session_dates"],
                ["2026-09-03"],
            )

    def test_identity_requires_reviewed_dated_source_assertion_and_rejects_transitions(self) -> None:
        for case in ("missing_assertion", "multiple_segments"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_value:
                temp = Path(temp_value)
                generation, calendar_csv, calendar_provenance, identity_binding, evidence_root = self._inputs(
                    temp, include_no_price_membership=True
                )
                if case == "missing_assertion":
                    identity_binding["asset_bindings"][0]["same_security_assertion"] = False  # type: ignore[index]
                else:
                    identity_binding["asset_bindings"].append(  # type: ignore[union-attr]
                        dict(identity_binding["asset_bindings"][0])  # type: ignore[index]
                    )
                result = project_development_sample(
                    generation,
                    calendar_csv=calendar_csv,
                    calendar_provenance=calendar_provenance,
                    identity_binding=identity_binding,
                    evidence_root=evidence_root,
                    member_column="member",
                    output_dir=temp / "private-output",
                )
                report = json.loads(result["quality_report_path"].read_text(encoding="utf-8"))
                self.assertIsNone(result["membership_v3_path"])
                self.assertEqual(report["identity"]["status"], "unresolved_source_identity_assertion")
                self.assertIn("identity_source_mapping_unavailable", report["blockers"])

    def test_calendar_requires_full_declared_market_window_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            generation, calendar_csv, calendar_provenance, identity_binding, evidence_root = self._inputs(
                temp, include_no_price_membership=True
            )
            with calendar_csv.open("r", encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            with calendar_csv.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=("session_date",), lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows[:-1])
            calendar_provenance["calendar_csv_sha256"] = _sha(calendar_csv)
            result = project_development_sample(
                generation,
                calendar_csv=calendar_csv,
                calendar_provenance=calendar_provenance,
                identity_binding=identity_binding,
                evidence_root=evidence_root,
                member_column="member",
                output_dir=temp / "private-output",
            )
            report = json.loads(result["quality_report_path"].read_text(encoding="utf-8"))
            self.assertIsNone(result["membership_v3_path"])
            self.assertEqual(report["calendar"]["status"], "unresolved_independent_calendar")
            self.assertIn("independent_calendar_unverified", report["blockers"])

    def test_off_calendar_and_duplicate_membership_rows_fail_closed(self) -> None:
        cases = (
            ("off_calendar", "2026-09-04", "False"),
            ("duplicate", "2026-09-01", "False"),
            ("invalid_value", "2026-09-02", "not-a-state"),
        )
        for case, session, member in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_value:
                temp = Path(temp_value)
                generation, calendar_csv, calendar_provenance, identity_binding, evidence_root = self._inputs(
                    temp, include_no_price_membership=True
                )
                if case == "invalid_value":
                    manifest_path = generation / "manifest.json"
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    record = manifest["native_frames"]["membership"]["$SPX"]
                    path = generation / record["path"]
                    with path.open("r", encoding="utf-8", newline="") as stream:
                        rows = list(csv.DictReader(stream))
                    next(row for row in rows if row["session_date"] == session)["member"] = member
                    with path.open("w", encoding="utf-8", newline="") as stream:
                        writer = csv.DictWriter(stream, fieldnames=("session_date", "member"), lineterminator="\n")
                        writer.writeheader()
                        writer.writerows(rows)
                    record["sha256"] = _sha(path)
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                else:
                    self._append_membership_fixture_row(generation, "$SPX", session, member)
                result = project_development_sample(
                    generation,
                    calendar_csv=calendar_csv,
                    calendar_provenance=calendar_provenance,
                    identity_binding=identity_binding,
                    evidence_root=evidence_root,
                    member_column="member",
                    output_dir=temp / "private-output",
                )
                report = json.loads(result["quality_report_path"].read_text(encoding="utf-8"))
                self.assertIsNone(result["membership_v3_path"])
                self.assertEqual(
                    report["membership_by_universe"]["sp500"]["status"],
                    "blocked_invalid_native_membership_observations",
                )
                self.assertIn("membership_observations_invalid:$SPX", report["blockers"])

    def test_membership_dates_reject_suffixes_and_accept_pandas_timestamps(self) -> None:
        cases = (
            ("malformed", "2026-09-02garbage"),
            ("invalid_offset_minutes", "2026-09-02 00:00:00+00:60"),
            ("invalid_offset_hours", "2026-09-02 00:00:00+24:00"),
            ("pandas_timestamp", "2026-09-02 00:00:00.000000000-04:00"),
            ("valid_offset_boundary", "2026-09-02 00:00:00+23:59"),
        )
        for case, label in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_value:
                temp = Path(temp_value)
                generation, calendar_csv, calendar_provenance, identity_binding, evidence_root = self._inputs(
                    temp, include_no_price_membership=True
                )
                self._rewrite_native_session_date(
                    generation, old_date="2026-09-02", new_date=label, index_name="$SPX"
                )
                result = project_development_sample(
                    generation,
                    calendar_csv=calendar_csv,
                    calendar_provenance=calendar_provenance,
                    identity_binding=identity_binding,
                    evidence_root=evidence_root,
                    member_column="member",
                    output_dir=temp / "private-output",
                )
                report = json.loads(result["quality_report_path"].read_text(encoding="utf-8"))
                status = report["membership_by_universe"]["sp500"]
                if case in {"malformed", "invalid_offset_minutes", "invalid_offset_hours"}:
                    self.assertIsNone(result["membership_v3_path"])
                    self.assertEqual(status["unknown_session_dates"], ["2026-09-02"])
                    self.assertEqual(status["invalid_observation_dates"], [label])
                    self.assertEqual(status["status"], "blocked_invalid_native_membership_observations")
                else:
                    self.assertIsNotNone(result["membership_v3_path"])
                    self.assertEqual(status["unknown_session_dates"], [])
                    self.assertEqual(status["invalid_observation_dates"], [])
                    self.assertEqual(status["status"], "complete_observed_calendar")

    def test_price_dates_reject_suffixes_and_accept_pandas_timestamps(self) -> None:
        cases = (
            ("malformed", "2026-09-03garbage"),
            ("invalid_offset_minutes", "2026-09-03T00:00:00+00:60"),
            ("invalid_offset_hours", "2026-09-03T00:00:00+24:00"),
            ("pandas_timestamp", "2026-09-03T00:00:00.000000000Z"),
            ("valid_offset_boundary", "2026-09-03T00:00:00+23:59"),
        )
        for case, label in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_value:
                temp = Path(temp_value)
                generation, calendar_csv, calendar_provenance, identity_binding, evidence_root = self._inputs(
                    temp, include_no_price_membership=True
                )
                self._rewrite_native_session_date(generation, old_date="2026-09-03", new_date=label)
                result = project_development_sample(
                    generation,
                    calendar_csv=calendar_csv,
                    calendar_provenance=calendar_provenance,
                    identity_binding=identity_binding,
                    evidence_root=evidence_root,
                    member_column="member",
                    output_dir=temp / "private-output",
                )
                report = json.loads(result["quality_report_path"].read_text(encoding="utf-8"))
                self.assertIsNone(result["prices_v3_path"])
                if case in {"malformed", "invalid_offset_minutes", "invalid_offset_hours"}:
                    self.assertEqual(report["price_date_validation"]["invalid_values"], [label])
                    self.assertEqual(report["price_date_validation"]["recognized_session_dates"], ["2026-09-01"])
                    self.assertEqual(report["missingness"]["price_session_dates"], ["2026-09-02", "2026-09-03"])
                    self.assertIn("native_price_session_date_invalid", report["blockers"])
                else:
                    self.assertEqual(report["price_date_validation"]["invalid_values"], [])
                    self.assertEqual(
                        report["price_date_validation"]["recognized_session_dates"],
                        list(PRICE_DATES),
                    )
                    self.assertEqual(report["missingness"]["price_session_dates"], ["2026-09-02"])
                    self.assertNotIn("native_price_session_date_invalid", report["blockers"])

    def test_unknown_price_basis_blocks_projection_without_relabeling(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            generation, calendar_csv, calendar_provenance, identity_binding, evidence_root = self._inputs(temp)
            manifest_path = generation / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["query"]["adjustment_setting"] = "UNSPECIFIED"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = project_development_sample(
                generation,
                calendar_csv=calendar_csv,
                calendar_provenance=calendar_provenance,
                identity_binding=identity_binding,
                evidence_root=evidence_root,
                member_column="member",
                output_dir=temp / "private-output",
            )
            report = json.loads(result["quality_report_path"].read_text(encoding="utf-8"))
            self.assertEqual(report["projection_status"], "blocked")
            self.assertIsNone(result["prices_v3_path"])
            self.assertEqual(report["basis"]["status"], "unknown")
            self.assertIn("unknown_price_basis", report["blockers"])

    def test_native_file_hash_mismatch_blocks_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            generation, calendar_csv, calendar_provenance, identity_binding, evidence_root = self._inputs(temp)
            price_path = generation / "prices.csv"
            price_path.write_bytes(price_path.read_bytes() + b"\n")
            result = project_development_sample(
                generation,
                calendar_csv=calendar_csv,
                calendar_provenance=calendar_provenance,
                identity_binding=identity_binding,
                evidence_root=evidence_root,
                member_column="member",
                output_dir=temp / "private-output",
            )
            report = json.loads(result["quality_report_path"].read_text(encoding="utf-8"))
            self.assertEqual(report["projection_status"], "blocked")
            self.assertIsNone(result["prices_v3_path"])
            self.assertIn("native_frame_hash_mismatch", report["blockers"])


if __name__ == "__main__":
    unittest.main()
