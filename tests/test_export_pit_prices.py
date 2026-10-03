"""Focused trust-boundary tests for the confined PIT price exporter."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from contextlib import contextmanager
from copy import deepcopy
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
import pandas as pd

import export_pit_prices as exporter
from core.alpaca_pit_backfill import (
    PRICE_COLUMNS,
    ProviderIdentity,
    REQUEST_ALIASES,
    _apply_cutoff_split_factors,
    _derive_cutoff_split_factors,
    _request_symbols,
    _response_rows,
)


def _provenance() -> dict[str, object]:
    return {"source_kind": "existing_hash_pinned_cache"}


def _write_minimal_price_identity_manifest(
    path: Path,
    *,
    bf_provider: str = "BF.B",
    psky_provider: str = "PSKY",
    bf_end: date = date(2025, 12, 31),
) -> tuple[exporter.Membership, exporter.IdentityBounds, str]:
    rows = (
        ("BF-B", bf_provider, bf_end, date(2020, 1, 1), bf_end, "bf", "historical_identity", "", "1", "https://example.test/bf"),
        ("BRK-B", "BRK.B", date(2025, 12, 31), date(2020, 1, 1), date(2025, 12, 31), "brk", "historical_identity", "", "1", "https://example.test/brk"),
        ("PSKY", psky_provider, date(2025, 12, 31), date(2025, 8, 7), date(2025, 12, 31), "paramount_successor", "successor_reset", "", "1", "https://example.test/psky"),
    )
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = exporter.csv.writer(stream, lineterminator="\n")
        writer.writerow(exporter._PRICE_IDENTITY_COLUMNS)
        for row in rows:
            writer.writerow(
                (
                    row[0],
                    row[1],
                    row[2].isoformat(),
                    row[3].isoformat(),
                    row[4].isoformat(),
                    *row[5:],
                )
            )
    membership = exporter.Membership((), ("BF-B", "BRK-B", "PSKY"))
    history = exporter.IdentityBounds(
        path,
        "0" * 64,
        {ticker: date(2025, 12, 31) for ticker in membership.tickers},
        3,
        membership.tickers,
    )
    return membership, history, exporter._sha256_file(path)


def _synthetic_fiserv_segment_sidecar(
    output_dir: Path,
    parent_request_contracts: dict[str, dict[str, object]],
) -> tuple[Path, str]:
    evidence_dir = output_dir / "identity-evidence"
    evidence_dir.mkdir(parents=True)
    assertions: list[dict[str, object]] = []
    for index, (assertion_id, effective_date) in enumerate(
        (
            ("synthetic-fiserv-2023", "2023-06-07"),
            ("synthetic-fiserv-2025", "2025-11-11"),
        )
    ):
        source_bytes = f"Synthetic test fixture; not source evidence {index}\n".encode()
        source_path = evidence_dir / f"source-{index}.bin"
        source_path.write_bytes(source_bytes)
        assertions.append(
            {
                "assertion_id": assertion_id,
                "authority": "Synthetic test fixture; not source evidence",
                "url": f"https://example.test/synthetic-identity-{index}",
                "accession": None,
                "document_date": "2023-05-25" if index == 0 else "2025-10-29",
                "locator": "Synthetic shape-only assertion",
                "effective_date": effective_date,
                "supports": "Synthetic test data; no real filing or source claim.",
                "source_byte_sha256": hashlib.sha256(source_bytes).hexdigest(),
                "source_document_path": f"identity-evidence/{source_path.name}",
            }
        )
    parent_digest = exporter.pit_canonical_json_sha256(parent_request_contracts)
    contract: dict[str, object] = {
        "schema_version": 1,
        "parent_price_identity_request_contracts_sha256": parent_digest,
        "segments": [
            {
                "segment_id": "fiserv-fisv-pre-2023",
                "provider_symbol": "FISV",
                "chain_id": "fiserv",
                "continuity_kind": "same_issuer_ticker_reuse",
                "admitted_start": "2020-01-01",
                "admitted_end": "2023-06-06",
                "factor_anchor": False,
            },
            {
                "segment_id": "fiserv-fi-2023-2025",
                "provider_symbol": "FI",
                "chain_id": "fiserv",
                "continuity_kind": "same_issuer_ticker_reuse",
                "admitted_start": "2023-06-07",
                "admitted_end": "2025-11-10",
                "factor_anchor": False,
            },
            {
                "segment_id": "fiserv-fisv-post-2025",
                "provider_symbol": "FISV",
                "chain_id": "fiserv",
                "continuity_kind": "same_issuer_ticker_reuse",
                "admitted_start": "2025-11-11",
                "admitted_end": "2025-12-31",
                "factor_anchor": True,
            },
        ],
        "transitions": [
            {
                "effective_date": "2023-06-07",
                "predecessor_segment_id": "fiserv-fisv-pre-2023",
                "successor_segment_id": "fiserv-fi-2023-2025",
                "chain_id": "fiserv",
                "continuity_kind": "same_issuer_ticker_reuse",
                "source_assertion_ids": ["synthetic-fiserv-2023"],
            },
            {
                "effective_date": "2025-11-11",
                "predecessor_segment_id": "fiserv-fi-2023-2025",
                "successor_segment_id": "fiserv-fisv-post-2025",
                "chain_id": "fiserv",
                "continuity_kind": "same_issuer_ticker_reuse",
                "source_assertion_ids": ["synthetic-fiserv-2025"],
            },
        ],
        "source_assertions": assertions,
    }
    contract_path = output_dir.parent / "synthetic-segment-contract.json"
    contract_path.write_text(
        exporter.json.dumps(contract, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return contract_path, exporter.pit_canonical_json_sha256(contract)


def _composed_export_args(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    output_name: str,
    include_segments: bool,
    cache_close: float = 100,
) -> SimpleNamespace:
    output_dir = tmp_path / output_name
    output_dir.mkdir()
    membership_path = tmp_path / f"{output_name}-membership.csv"
    membership_path.write_text(
        "effective_date,ticker,member\n"
        "2020-01-02,FISV,1\n"
        "2023-06-07,FI,1\n"
        "2023-06-07,FISV,0\n"
        "2025-11-11,FI,0\n"
        "2025-11-11,FISV,1\n",
        encoding="utf-8",
    )
    membership = exporter._load_membership(membership_path)
    history_path = tmp_path / "synthetic-symbol-history.csv"
    history_path.write_text("synthetic nonproduction symbol history\n", encoding="utf-8")
    identity_map_path = tmp_path / "synthetic-price-identity.csv"
    identity_map_path.write_text("synthetic nonproduction price identity\n", encoding="utf-8")
    fi = exporter.PriceIdentity(
        "FI",
        "FI",
        date(2025, 11, 10),
        date(2023, 6, 7),
        date(2025, 11, 10),
        "fiserv",
        "same_issuer_ticker_reuse",
        "FISV",
        False,
        "https://example.test/synthetic-fi",
    )
    fisv = exporter.PriceIdentity(
        "FISV",
        "FISV",
        date(2025, 12, 31),
        date(2020, 1, 1),
        date(2025, 12, 31),
        "fiserv",
        "same_issuer_ticker_reuse",
        None,
        True,
        "https://example.test/synthetic-fisv",
    )
    manifest = exporter.PriceIdentityManifest(
        identity_map_path,
        exporter._sha256_file(identity_map_path),
        {"FI": fi, "FISV": fisv},
        1,
    )
    history = exporter.IdentityBounds(
        history_path,
        exporter._sha256_file(history_path),
        {"FI": date(2025, 11, 10), "FISV": date(2025, 12, 31)},
        2,
        ("FI", "FISV"),
    )
    monkeypatch.setattr(exporter, "_load_identity_bounds", lambda *_args: history)
    monkeypatch.setattr(exporter, "_load_price_identity_manifest", lambda *_args: manifest)

    cache_path = tmp_path / f"{output_name}-cache.sqlite3"
    cache_path.write_bytes(b"synthetic cache input\n")
    cache_sha256 = exporter._sha256_file(cache_path)
    worker_script = tmp_path / "synthetic-worker.py"
    worker_script.write_text("# synthetic confined worker fixture\n", encoding="utf-8")

    def copy_cache(_source: Path, expected_sha256: str, root: Path) -> exporter.CacheSnapshot:
        return exporter.CacheSnapshot(
            root / "synthetic-cache-snapshot.sqlite3",
            expected_sha256,
            0,
            hashlib.sha256(b"").hexdigest(),
        )

    def run_worker(*_args: object) -> Path:
        worker_output = _args[-1]
        assert isinstance(worker_output, Path)
        prices_path = worker_output / "prices.csv"
        with prices_path.open("x", encoding="utf-8", newline="") as stream:
            writer = exporter.csv.writer(stream, lineterminator="\n")
            writer.writerow(PRICE_COLUMNS)
            writer.writerow(
                ("2023-06-06", "FISV", cache_close, cache_close + 1, cache_close - 1, cache_close, 1_000)
            )
        return prices_path

    def write_snapshot(output_path: Path) -> SimpleNamespace:
        fixture_rows = (
            ("2023-06-06", "FISV"),
            ("2023-06-07", "FI"),
            ("2023-06-07", "FISV"),
            ("2025-11-10", "FI"),
            ("2025-11-10", "FISV"),
            ("2025-11-11", "FISV"),
            ("2023-06-06", "IWM"),
            ("2023-06-06", "QQQ"),
            ("2023-06-06", "SPY"),
        )
        with output_path.open("x", encoding="utf-8", newline="") as stream:
            writer = exporter.csv.writer(stream, lineterminator="\n")
            writer.writerow(PRICE_COLUMNS)
            for trade_date, ticker in sorted(fixture_rows):
                writer.writerow((trade_date, ticker, 100, 101, 99, 100, 1_000))
        return SimpleNamespace(
            path=output_path,
            retrieved_at_utc="2026-10-02T00:00:00Z",
            requested_symbol_count=5,
            requested_membership_symbol_count=2,
            returned_symbol_count=5,
            returned_membership_symbol_count=2,
            chunk_count=1,
            row_count=len(fixture_rows),
            adjustment="SPLIT",
            identity_group_count=2,
        )

    def write_raw_snapshot(output_path: Path) -> SimpleNamespace:
        output_path.write_text(",".join(PRICE_COLUMNS) + "\n", encoding="utf-8")
        return SimpleNamespace(
            path=output_path,
            retrieved_at_utc="2026-10-02T00:00:00Z",
            chunk_count=1,
            row_count=0,
            identity_group_count=2,
        )

    def fake_validate(
        path: Path,
        _membership: exporter.Membership,
        _start: date,
        _end: date,
        *,
        enforce_gates: bool = True,
    ) -> tuple[dict[str, object], tuple[date, ...]]:
        with path.open("r", encoding="utf-8", newline="") as stream:
            rows = list(exporter.csv.DictReader(stream))
        return (
            {
                "member_trading_day_pairs": 10,
                "covered_member_trading_day_pairs": min(len(rows), 10),
                "coverage_pct": 100.0,
                "symbols_with_no_prices": [],
                "symbols_with_partial_prices": [],
                "reference_symbol_coverage": {},
                "spy_first_date": "2025-11-11",
                "spy_last_date": "2025-11-11",
                "price_row_count": len(rows),
                "synthetic_fixture_enforce_gates": enforce_gates,
            },
            (date(2025, 11, 11),),
        )

    monkeypatch.setattr(exporter, "_copy_and_validate_cache", copy_cache)
    monkeypatch.setattr(exporter, "_prepare_container_access", lambda *_args: object())
    monkeypatch.setattr(exporter, "_run_worker", run_worker)
    monkeypatch.setattr(exporter, "load_alpaca_credentials", lambda *_args: ("fixture", "fixture"))
    monkeypatch.setattr(
        exporter,
        "fetch_alpaca_sip_snapshot",
        lambda *args, **kwargs: write_snapshot(kwargs["output_path"]),
    )
    monkeypatch.setattr(
        exporter,
        "fetch_alpaca_sip_raw_calibration",
        lambda *args, **kwargs: write_raw_snapshot(kwargs["output_path"]),
    )
    monkeypatch.setattr(
        exporter,
        "_derive_cutoff_split_factors",
        lambda _split, _raw, symbols: {symbol: 1.0 for symbol in symbols},
    )
    monkeypatch.setattr(exporter, "_validate_prices", fake_validate)

    args = SimpleNamespace(
        start_date=date(2020, 1, 1),
        end_date=date(2025, 12, 31),
        membership_csv=membership_path,
        symbol_history_map=history_path,
        symbol_history_map_sha256=history.sha256,
        price_identity_map=identity_map_path,
        price_identity_map_sha256=manifest.sha256,
        cache=cache_path,
        cache_sha256=cache_sha256,
        worker_script=worker_script,
        output_dir=output_dir,
        docker_executable="docker",
        sandbox_image="synthetic-fixture-image",
        alpaca_sip_backfill=True,
        alpaca_env_file=None,
        price_identity_segments_json=None,
        price_identity_segments_sha256=None,
        allow_nonproduction_price_identity_segment_fixture=False,
    )
    if include_segments:
        price_identities = exporter._complete_price_identities(
            membership, manifest, args.end_date
        )
        parent = {
            ticker: {
                "provider_symbol": identity.provider_symbol,
                "identity_asof": identity.identity_asof.isoformat(),
                "admitted_start": identity.admitted_start.isoformat(),
                "admitted_end": identity.admitted_end.isoformat(),
                "chain_id": identity.chain_id,
                "continuity_kind": identity.continuity_kind,
                "warmup_predecessor": identity.warmup_predecessor,
                "factor_anchor": identity.factor_anchor,
            }
            for ticker, identity in sorted(price_identities.items())
        }
        contract_path, contract_digest = _synthetic_fiserv_segment_sidecar(
            output_dir, parent
        )
        args.price_identity_segments_json = contract_path
        args.price_identity_segments_sha256 = contract_digest
        args.allow_nonproduction_price_identity_segment_fixture = True
    return args


def test_price_identity_manifest_accepts_only_reviewed_class_share_provider_formatting(
    tmp_path: Path,
) -> None:
    path = tmp_path / "price-identities.csv"
    membership, history, digest = _write_minimal_price_identity_manifest(path)

    manifest = exporter._load_price_identity_manifest(
        path, digest, membership, history, date(2025, 12, 31)
    )

    assert manifest.identities["BF-B"].provider_symbol == "BF.B"
    assert manifest.identities["BRK-B"].provider_symbol == "BRK.B"
    completed = exporter._complete_price_identities(
        membership, manifest, date(2025, 12, 31)
    )
    assert tuple(completed) == ("BF-B", "BRK-B", "IWM", "PSKY", "QQQ", "SPY")
    assert {
        reference: completed[reference].chain_id for reference in ("IWM", "QQQ", "SPY")
    } == {"IWM": "unmapped_iwm", "QQQ": "unmapped_qqq", "SPY": "unmapped_spy"}
    request = tmp_path / "request.json"
    exporter._canonical_request(
        request, membership, date(2020, 1, 1), date(2025, 12, 31)
    )
    assert exporter.json.loads(request.read_text(encoding="utf-8"))["tickers"] == [
        "BF-B",
        "BRK-B",
        "IWM",
        "PSKY",
        "QQQ",
        "SPY",
    ]


def test_price_identity_manifest_rejects_unreviewed_provider_symbol_mismatch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "price-identities.csv"
    membership, history, digest = _write_minimal_price_identity_manifest(
        path, psky_provider="PARA"
    )

    with pytest.raises(ValueError, match="price-identity row 4 is invalid"):
        exporter._load_price_identity_manifest(
            path, digest, membership, history, date(2025, 12, 31)
        )


def test_price_identity_manifest_rejects_end_before_membership_history_end(
    tmp_path: Path,
) -> None:
    path = tmp_path / "price-identities.csv"
    membership, history, digest = _write_minimal_price_identity_manifest(
        path, bf_end=date(2025, 12, 30)
    )

    with pytest.raises(ValueError, match="price-identity row 2 is invalid"):
        exporter._load_price_identity_manifest(
            path, digest, membership, history, date(2025, 12, 31)
        )


def test_cache_only_path_does_not_load_backfill_identity_manifests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    membership = tmp_path / "membership.csv"
    membership.write_text("effective_date,ticker,member\n2021-01-01,AAPL,1\n", encoding="utf-8")
    monkeypatch.setattr(
        exporter,
        "_load_identity_bounds",
        lambda *_args: (_ for _ in ()).throw(AssertionError("backfill manifest loaded")),
    )
    args = SimpleNamespace(
        start_date=date(2020, 1, 1),
        end_date=date(2025, 12, 31),
        membership_csv=membership,
        symbol_history_map=tmp_path / "missing-history.csv",
        symbol_history_map_sha256="0" * 64,
        price_identity_map=tmp_path / "missing-price-map.csv",
        price_identity_map_sha256="0" * 64,
        cache=tmp_path / "missing-cache.sqlite3",
        cache_sha256="0" * 64,
        worker_script=tmp_path / "missing-worker.py",
        output_dir=tmp_path / "output",
        alpaca_sip_backfill=False,
        alpaca_env_file=None,
    )

    with pytest.raises(FileNotFoundError):
        exporter.export(args)


def test_segment_export_composes_projection_warmup_provenance_and_legacy_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary_index = 0

    @contextmanager
    def workspace_temporary_directory(*, prefix: str):
        nonlocal temporary_index
        path = tmp_path / f"{prefix}{temporary_index}"
        temporary_index += 1
        path.mkdir()
        yield str(path)

    monkeypatch.setattr(
        exporter.tempfile, "TemporaryDirectory", workspace_temporary_directory
    )
    segment_args = _composed_export_args(
        tmp_path, monkeypatch, output_name="segment-output", include_segments=True
    )
    segment_provenance = exporter.export(segment_args)
    legacy_args = _composed_export_args(
        tmp_path, monkeypatch, output_name="legacy-output", include_segments=False
    )
    legacy_provenance = exporter.export(legacy_args)

    assert segment_provenance["price_identity_request_contracts"] == (
        legacy_provenance["price_identity_request_contracts"]
    )
    assert segment_provenance["price_identity_request_contracts_sha256"] == (
        legacy_provenance["price_identity_request_contracts_sha256"]
    )
    assert len(segment_provenance["price_identity_request_contracts"]) == 5
    assert "price_identity_segments_v1" not in legacy_provenance
    assert segment_provenance["price_identity_segment_sip_projection"] == {
        "scope": "active_price_identity_segments",
        "source_row_count": 9,
        "active_segment_row_count": 4,
        "active_rows_by_segment": {
            "fiserv-fisv-pre-2023": 1,
            "fiserv-fi-2023-2025": 2,
            "fiserv-fisv-post-2025": 1,
        },
        "unsegmented_row_count": 3,
        "discarded_inactive_symbol_row_count": 2,
        "discarded_outside_segment_window_row_count": 0,
    }
    warmup = segment_provenance["price_identity_segment_warmup_validation"]
    assert warmup["copied_warmup_rows_by_segment"] == {
        "fiserv-fi-2023-2025": 1,
        "fiserv-fisv-post-2025": 3,
    }
    assert warmup["transition_audits"]["fiserv-fisv-post-2025"][
        "effective_date"
    ] == "2025-11-11"
    assert segment_provenance["price_identity_segment_parent_request_contracts_sha256"] == (
        segment_provenance["price_identity_request_contracts_sha256"]
    )
    assert segment_provenance["price_identity_segments_admission_status"] == (
        "nonproduction_fixture"
    )
    assert segment_provenance["price_identity_segments_source_use_status"] == (
        "source_bytes_hash_verified_rights_not_adjudicated"
    )
    assert exporter.pit_canonical_json_sha256(
        segment_provenance["price_identity_segments_v1"]
    ) == segment_provenance["price_identity_segments_v1_sha256"]
    staged_segment_input = exporter.validate_price_identity_segments_v1(
        segment_provenance["price_identity_segments_v1"],
        declared_sha256=segment_provenance["price_identity_segments_v1_sha256"],
        parent_request_contracts=segment_provenance["price_identity_request_contracts"],
        identities=segment_provenance["price_identity_request_contracts"],
        source_evidence_root=segment_args.output_dir,
        data_cutoff=date(2025, 12, 31),
    )
    assert staged_segment_input.segment_contract_sha256 == (
        segment_provenance["price_identity_segments_v1_sha256"]
    )

    with (segment_args.output_dir / "prices.csv").open(
        "r", encoding="utf-8", newline=""
    ) as stream:
        prices = list(exporter.csv.DictReader(stream))
    assert [(row["trade_date"], row["ticker"]) for row in prices] == sorted(
        (row["trade_date"], row["ticker"]) for row in prices
    )
    assert ("2023-06-07", "FISV") in {
        (row["trade_date"], row["ticker"]) for row in prices
    }  # explicitly retained as FI-segment warm-up
    assert ("2025-11-10", "FISV") in {
        (row["trade_date"], row["ticker"]) for row in prices
    }  # explicitly retained as post-2025 FISV-segment warm-up

    conflicting_args = _composed_export_args(
        tmp_path,
        monkeypatch,
        output_name="segment-conflicting-cache",
        include_segments=True,
        cache_close=101,
    )
    with pytest.raises(ValueError, match="published price identity continuity mismatch"):
        exporter.export(conflicting_args)
    assert not (conflicting_args.output_dir / "prices.csv").exists()

    unopted_args = _composed_export_args(
        tmp_path,
        monkeypatch,
        output_name="segment-without-fixture-opt-in",
        include_segments=True,
    )
    unopted_args.allow_nonproduction_price_identity_segment_fixture = False
    with pytest.raises(ValueError, match="fixture-only until price-identity source-use admission"):
        exporter.export(unopted_args)


def test_segment_export_revalidates_source_evidence_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextmanager
    def workspace_temporary_directory(*, prefix: str):
        path = tmp_path / f"{prefix}revalidation"
        path.mkdir()
        yield str(path)

    monkeypatch.setattr(
        exporter.tempfile, "TemporaryDirectory", workspace_temporary_directory
    )
    args = _composed_export_args(
        tmp_path, monkeypatch, output_name="segment-evidence-mutated", include_segments=True
    )
    source_path = args.output_dir / "identity-evidence" / "source-0.bin"
    original_csv_text = exporter._csv_text
    mutated = False

    def mutate_before_final_validation(
        rows: object,
        header: object,
    ) -> str:
        nonlocal mutated
        result = original_csv_text(rows, header)
        if header == ("trade_date",) and not mutated:
            source_path.write_bytes(b"changed after segment validation\n")
            mutated = True
        return result

    monkeypatch.setattr(exporter, "_csv_text", mutate_before_final_validation)

    with pytest.raises(ValueError, match="price identity source document hash does not match assertion"):
        exporter.export(args)

    assert mutated
    assert not (args.output_dir / "prices.csv").exists()


def test_alpaca_class_share_aliases_use_provider_dots_and_restore_canonical_hyphens() -> None:
    """Break caught: canonical hyphen class shares were sent to Alpaca and rejected with HTTP 400."""
    requested, provider_to_canonical = _request_symbols(("BF-B", "BRK-B"))

    assert REQUEST_ALIASES == {"BF.B": "BF-B", "BRK.B": "BRK-B"}
    assert requested == ("BF.B", "BRK.B")
    assert provider_to_canonical == {"BF.B": "BF-B", "BRK.B": "BRK-B"}


def test_cutoff_factor_undoes_only_post_cutoff_split_adjustment(tmp_path: Path) -> None:
    """Break caught: a 2026 split created a 4x discontinuity in the 2020-25 normalized snapshot."""
    split_path = tmp_path / "split.csv"
    raw_path = tmp_path / "raw.csv"
    normalized_path = tmp_path / "normalized.csv"
    dates = [f"2025-12-{day:02d}" for day in range(1, 21)]

    def write_snapshot(path: Path, *, crwd_price: float, crwd_volume: float) -> None:
        with path.open("x", encoding="utf-8", newline="") as stream:
            writer = exporter.csv.writer(stream, lineterminator="\n")
            writer.writerow(PRICE_COLUMNS)
            for trade_date in dates:
                writer.writerow((trade_date, "AAPL", 100, 101, 99, 100.5, 1_000))
                writer.writerow(
                    (
                        trade_date,
                        "CRWD",
                        crwd_price,
                        crwd_price * 1.04,
                        crwd_price * 0.96,
                        crwd_price,
                        crwd_volume,
                    )
                )

    write_snapshot(split_path, crwd_price=25, crwd_volume=400)
    write_snapshot(raw_path, crwd_price=100, crwd_volume=100)

    factors = _derive_cutoff_split_factors(split_path, raw_path, ("AAPL", "CRWD"))
    _apply_cutoff_split_factors(split_path, factors, normalized_path)

    assert factors == {"AAPL": 1.0, "CRWD": 4.0}
    with normalized_path.open("r", encoding="utf-8", newline="") as stream:
        rows = list(exporter.csv.DictReader(stream))
    crwd = next(row for row in rows if row["ticker"] == "CRWD")
    assert float(crwd["open"]) == 100.0
    assert float(crwd["close"]) == 100.0
    assert float(crwd["volume"]) == 100.0


def test_cutoff_factor_uses_stable_suffix_after_now_in_window_split(tmp_path: Path) -> None:
    """Break caught: NOW's December 2025 split made a fixed 20-session calibration tail unstable."""
    split_path = tmp_path / "split.csv"
    raw_path = tmp_path / "raw.csv"
    dates = [f"2025-12-{day:02d}" for day in range(1, 21)]
    with (
        split_path.open("x", encoding="utf-8", newline="") as split_stream,
        raw_path.open("x", encoding="utf-8", newline="") as raw_stream,
    ):
        split_writer = exporter.csv.writer(split_stream, lineterminator="\n")
        raw_writer = exporter.csv.writer(raw_stream, lineterminator="\n")
        split_writer.writerow(PRICE_COLUMNS)
        raw_writer.writerow(PRICE_COLUMNS)
        for index, trade_date in enumerate(dates):
            split_price, split_volume = (20, 500) if index < 10 else (100, 100)
            split_writer.writerow(
                (trade_date, "NOW", split_price, split_price, split_price, split_price, split_volume)
            )
            raw_writer.writerow((trade_date, "NOW", 100, 100, 100, 100, 100))

    factors = _derive_cutoff_split_factors(split_path, raw_path, ("NOW",))

    assert factors == {"NOW": 1.0}


def test_cache_basis_normalization_removes_amcr_lookahead_but_preserves_crwd_cutoff_rows(
    tmp_path: Path,
) -> None:
    """Break caught: cache rows retained mixed post-cutoff split lookahead by symbol."""
    cache_path = tmp_path / "cache.csv"
    split_path = tmp_path / "split.csv"
    cutoff_path = tmp_path / "cutoff.csv"
    normalized_cache_path = tmp_path / "normalized-cache.csv"
    dates = [f"2025-12-{day:02d}" for day in range(1, 21)]

    def write_rows(path: Path, rows_by_symbol: dict[str, tuple[float, float]]) -> None:
        with path.open("x", encoding="utf-8", newline="") as stream:
            writer = exporter.csv.writer(stream, lineterminator="\n")
            writer.writerow(PRICE_COLUMNS)
            for trade_date in dates:
                for ticker, (price, volume) in sorted(rows_by_symbol.items()):
                    writer.writerow((trade_date, ticker, price, price, price, price, volume))

    write_rows(cache_path, {"AMCR": (100, 20), "CRWD": (100, 100)})
    write_rows(split_path, {"AMCR": (100, 20), "CRWD": (25, 400)})
    write_rows(cutoff_path, {"AMCR": (20, 100), "CRWD": (100, 100)})

    result = exporter._normalize_cache_to_cutoff_basis(
        cache_path,
        split_path,
        cutoff_path,
        {"AMCR": 0.2, "CRWD": 4.0},
        normalized_cache_path,
    )

    assert result["cache_basis_by_symbol"] == {
        "AMCR": "current_split_transformed_to_cutoff",
        "CRWD": "already_cutoff_aligned",
    }
    with normalized_cache_path.open("r", encoding="utf-8", newline="") as stream:
        rows = list(exporter.csv.DictReader(stream))
    amcr = next(row for row in rows if row["ticker"] == "AMCR")
    crwd = next(row for row in rows if row["ticker"] == "CRWD")
    assert (float(amcr["close"]), float(amcr["volume"])) == (20.0, 100.0)
    assert (float(crwd["close"]), float(crwd["volume"])) == (100.0, 100.0)


def test_cache_normalization_discards_rows_before_price_identity_admission(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "cache.csv"
    split_path = tmp_path / "split.csv"
    cutoff_path = tmp_path / "cutoff.csv"
    normalized_path = tmp_path / "normalized.csv"
    with cache_path.open("x", encoding="utf-8", newline="") as stream:
        writer = exporter.csv.writer(stream, lineterminator="\n")
        writer.writerow(PRICE_COLUMNS)
        writer.writerow(("2023-06-06", "FI", 50, 51, 49, 50, 800))
        writer.writerow(("2023-06-07", "FI", 100, 101, 99, 100, 1_000))
    for path in (split_path, cutoff_path):
        with path.open("x", encoding="utf-8", newline="") as stream:
            writer = exporter.csv.writer(stream, lineterminator="\n")
            writer.writerow(PRICE_COLUMNS)
            writer.writerow(("2023-06-07", "FI", 100, 101, 99, 100, 1_000))
    identities = {
        "FI": exporter.PriceIdentity(
            "FI", "FI", date(2025, 11, 10), date(2023, 6, 7),
            date(2025, 11, 10), "fiserv", "same_issuer_ticker_reuse", "FISV", False,
            "https://example.test/fi",
        ),
    }

    exporter._normalize_cache_to_cutoff_basis(
        cache_path, split_path, cutoff_path, {"FI": 1.0}, normalized_path, identities
    )

    with normalized_path.open("r", encoding="utf-8", newline="") as stream:
        rows = list(exporter.csv.DictReader(stream))
    assert [(row["trade_date"], row["ticker"]) for row in rows] == [
        ("2023-06-07", "FI"),
    ]


def test_published_identity_audit_catches_cache_rows_overriding_matching_provider_rows(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "cache.csv"
    sip_path = tmp_path / "sip.csv"
    merged_path = tmp_path / "merged.csv"
    provider_values = (115.89, 116.0, 113.57, 115.89, 102_643)
    cache_values = {
        "FI": (115.78, 116.0, 113.57, 115.78, 3_075_854),
        "FISV": provider_values,
    }
    with cache_path.open("x", encoding="utf-8", newline="") as stream:
        writer = exporter.csv.writer(stream, lineterminator="\n")
        writer.writerow(PRICE_COLUMNS)
        for ticker, values in sorted(cache_values.items()):
            writer.writerow(("2023-06-07", ticker, *values))
    with sip_path.open("x", encoding="utf-8", newline="") as stream:
        writer = exporter.csv.writer(stream, lineterminator="\n")
        writer.writerow(PRICE_COLUMNS)
        for ticker in ("FI", "FISV"):
            writer.writerow(("2023-06-07", ticker, *provider_values))
    identities = {
        "FISV": exporter.PriceIdentity(
            "FISV", "FISV", date(2025, 12, 31), date(2020, 1, 1),
            date(2025, 12, 31), "fiserv", "same_issuer_ticker_reuse", None, True,
            "https://example.test/fisv",
        ),
        "FI": exporter.PriceIdentity(
            "FI", "FI", date(2025, 11, 10), date(2023, 6, 7),
            date(2025, 11, 10), "fiserv", "same_issuer_ticker_reuse", "FISV", False,
            "https://example.test/fi",
        ),
    }

    exporter._merge_price_sources(cache_path, sip_path, merged_path)

    with pytest.raises(
        ValueError,
        match=r"published price identity continuity mismatch for FISV/FI: 1/1 shared rows differ",
    ):
        exporter._validate_published_price_identity_continuity(merged_path, identities)


def test_published_identity_audit_records_exact_merged_overlap(tmp_path: Path) -> None:
    prices_path = tmp_path / "prices.csv"
    values = (115.89, 116.0, 113.57, 115.89, 102_643)
    with prices_path.open("x", encoding="utf-8", newline="") as stream:
        writer = exporter.csv.writer(stream, lineterminator="\n")
        writer.writerow(PRICE_COLUMNS)
        for ticker in ("FI", "FISV"):
            writer.writerow(("2023-06-07", ticker, *values))
    identities = {
        "FISV": exporter.PriceIdentity(
            "FISV", "FISV", date(2025, 12, 31), date(2020, 1, 1),
            date(2025, 12, 31), "fiserv", "same_issuer_ticker_reuse", None, True,
            "https://example.test/fisv",
        ),
        "FI": exporter.PriceIdentity(
            "FI", "FI", date(2025, 11, 10), date(2023, 6, 7),
            date(2025, 11, 10), "fiserv", "same_issuer_ticker_reuse", "FISV", False,
            "https://example.test/fi",
        ),
    }

    result = exporter._validate_published_price_identity_continuity(prices_path, identities)

    assert result == {
        "scope": "published_merged_price_csv",
        "validated_successor_count": 1,
        "successor_audits": {
            "FI": {
                "predecessor": "FISV",
                "exact_overlap_row_count": 1,
                "overlap_first_date": "2023-06-07",
                "overlap_last_date": "2023-06-07",
            },
        },
    }


def test_published_identity_audit_allows_no_overlap_without_claiming_continuity(
    tmp_path: Path,
) -> None:
    prices_path = tmp_path / "prices.csv"
    values = (100.0, 101.0, 99.0, 100.0, 1_000)
    with prices_path.open("x", encoding="utf-8", newline="") as stream:
        writer = exporter.csv.writer(stream, lineterminator="\n")
        writer.writerow(PRICE_COLUMNS)
        writer.writerow(("2023-06-06", "FISV", *values))
        writer.writerow(("2023-06-07", "FI", *values))
    identities = {
        "FISV": exporter.PriceIdentity(
            "FISV", "FISV", date(2025, 12, 31), date(2020, 1, 1),
            date(2025, 12, 31), "fiserv", "same_issuer_ticker_reuse", None, True,
            "https://example.test/fisv",
        ),
        "FI": exporter.PriceIdentity(
            "FI", "FI", date(2025, 11, 10), date(2023, 6, 7),
            date(2025, 11, 10), "fiserv", "same_issuer_ticker_reuse", "FISV", False,
            "https://example.test/fi",
        ),
    }

    result = exporter._validate_published_price_identity_continuity(prices_path, identities)

    assert result["successor_audits"]["FI"] == {
        "predecessor": "FISV",
        "exact_overlap_row_count": 0,
        "overlap_first_date": None,
        "overlap_last_date": None,
    }


def test_provider_identity_rows_outside_admitted_interval_are_not_output() -> None:
    """Break caught: FI's reused pre-entry symbol history leaked into Fiserv output."""
    index = pd.MultiIndex.from_tuples(
        [
            ("BLL", pd.Timestamp("2022-05-09", tz="UTC")),
            ("BLL", pd.Timestamp("2022-05-10", tz="UTC")),
            ("BALL", pd.Timestamp("2022-04-25", tz="UTC")),
            ("BALL", pd.Timestamp("2022-05-10", tz="UTC")),
            ("PARA", pd.Timestamp("2025-08-06", tz="UTC")),
            ("PARA", pd.Timestamp("2025-08-07", tz="UTC")),
            ("PSKY", pd.Timestamp("2025-08-07", tz="UTC")),
        ],
        names=("symbol", "timestamp"),
    )
    frame = pd.DataFrame(
        {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 1_000},
        index=index,
    )
    symbols = ("BALL", "BLL", "PARA", "PSKY")

    rows, returned = _response_rows(
        frame,
        frozenset(symbols),
        {symbol: symbol for symbol in symbols},
        date(2020, 1, 1),
        date(2025, 12, 31),
        frozenset(
            {
                date(2022, 4, 25),
                date(2022, 5, 9),
                date(2022, 5, 10),
                date(2025, 8, 6),
                date(2025, 8, 7),
            }
        ),
        {
            "BLL": ProviderIdentity("BLL", "BLL", date(2022, 5, 9), date(2020, 1, 1), date(2022, 5, 9), True),
            "BALL": ProviderIdentity("BALL", "BALL", date(2025, 12, 31), date(2022, 5, 10), date(2025, 12, 31), True),
            "PARA": ProviderIdentity("PARA", "PARA", date(2025, 8, 6), date(2022, 2, 17), date(2025, 8, 6), True),
            "PSKY": ProviderIdentity("PSKY", "PSKY", date(2025, 12, 31), date(2025, 8, 7), date(2025, 12, 31), True),
        },
    )

    assert returned == set(symbols)
    assert [(row[0], row[1]) for row in rows] == [
        (date(2022, 5, 9), "BLL"),
        (date(2022, 5, 10), "BALL"),
        (date(2025, 8, 6), "PARA"),
        (date(2025, 8, 7), "PSKY"),
    ]


def test_ticker_reuse_warmup_copies_reviewed_predecessor_not_successor_symbol_history(
    tmp_path: Path,
) -> None:
    admitted_path = tmp_path / "admitted.csv"
    output_path = tmp_path / "warmup.csv"
    with admitted_path.open("x", encoding="utf-8", newline="") as stream:
        writer = exporter.csv.writer(stream, lineterminator="\n")
        writer.writerow(PRICE_COLUMNS)
        writer.writerow(("2023-06-05", "FISV", 100, 101, 99, 100, 1_000))
        writer.writerow(("2023-06-07", "FISV", 102, 103, 101, 102, 1_100))
        writer.writerow(("2023-06-07", "FI", 102, 103, 101, 102, 1_100))
    identities = {
        "FISV": exporter.PriceIdentity(
            "FISV", "FISV", date(2025, 12, 31), date(2020, 1, 1),
            date(2025, 12, 31), "fiserv", "same_issuer_ticker_reuse", None, True,
            "https://example.test/fisv",
        ),
        "FI": exporter.PriceIdentity(
            "FI", "FI", date(2025, 11, 10), date(2023, 6, 7),
            date(2025, 11, 10), "fiserv", "same_issuer_ticker_reuse", "FISV", False,
            "https://example.test/fi",
        ),
    }

    metrics = exporter._build_price_identity_warmup(admitted_path, identities, output_path)

    with output_path.open("r", encoding="utf-8", newline="") as stream:
        rows = list(exporter.csv.DictReader(stream))
    fi_rows = [row for row in rows if row["ticker"] == "FI"]
    assert [(row["trade_date"], float(row["close"])) for row in fi_rows] == [
        ("2023-06-05", 100.0),
        ("2023-06-07", 102.0),
    ]
    assert metrics["copied_warmup_row_count"] == 1
    assert metrics["successor_audits"]["FI"]["exact_overlap_row_count"] == 1


def test_chain_factor_uses_terminal_anchor_and_rejects_unproved_pre_cutoff_chain() -> None:
    identities = {
        "BLL": exporter.PriceIdentity("BLL", "BLL", date(2022, 5, 9), date(2020, 1, 1), date(2022, 5, 9), "ball", "same_issuer_rename", None, False, "https://example.test/bll"),
        "BALL": exporter.PriceIdentity("BALL", "BALL", date(2025, 12, 31), date(2022, 5, 10), date(2025, 12, 31), "ball", "same_issuer_rename", "BLL", True, "https://example.test/ball"),
        "VIAC": exporter.PriceIdentity("VIAC", "VIAC", date(2022, 2, 16), date(2020, 1, 1), date(2022, 2, 16), "paramount_legacy", "same_issuer_rename", None, False, "https://example.test/viac"),
        "PARA": exporter.PriceIdentity("PARA", "PARA", date(2025, 8, 6), date(2022, 2, 17), date(2025, 8, 6), "paramount_legacy", "same_issuer_rename", "VIAC", True, "https://example.test/para"),
    }

    factors = exporter._expand_chain_cutoff_factors(
        {"BALL": 4.0, "PARA": 1.0}, identities, date(2025, 12, 31)
    )

    assert factors == {"BALL": 4.0, "BLL": 4.0, "PARA": 1.0, "VIAC": 1.0}
    with pytest.raises(ValueError, match="pre-cutoff chain paramount_legacy"):
        exporter._expand_chain_cutoff_factors(
            {"BALL": 4.0, "PARA": 2.0}, identities, date(2025, 12, 31)
        )


def test_publish_uses_unique_staging_and_cleans_owned_files_on_install_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a destination race left a directly-written partial price bundle."""
    output = tmp_path / "exports"
    source = tmp_path / "worker-prices.csv"
    source.write_bytes(b"trade_date,ticker,open,high,low,close,volume\n")
    real_link = os.link

    def race_on_second_install(source_path: str | bytes, target_path: str | bytes) -> None:
        target = Path(target_path)
        if target.name == "spy_trading_days.csv":
            target.write_bytes(b"racer-owned\n")
        real_link(source_path, target_path)

    monkeypatch.setattr(exporter.os, "link", race_on_second_install)

    with pytest.raises(FileExistsError):
        exporter._publish(output, source, (date(2025, 12, 31),), _provenance())

    assert not (output / "prices.csv").exists()
    assert (output / "spy_trading_days.csv").read_bytes() == b"racer-owned\n"
    assert not (output / "prices_provenance.json").exists()
    assert not tuple(output.glob(".*.tmp"))


def test_publish_installs_fsynced_staging_files_without_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: final files were opened directly and never flushed durably before install."""
    output = tmp_path / "exports"
    source = tmp_path / "worker-prices.csv"
    prices = b"trade_date,ticker,open,high,low,close,volume\n"
    source.write_bytes(prices)
    fsynced: list[int] = []
    real_fsync = os.fsync

    def record_fsync(file_descriptor: int) -> None:
        fsynced.append(file_descriptor)
        real_fsync(file_descriptor)

    monkeypatch.setattr(exporter.os, "fsync", record_fsync)

    exporter._publish(output, source, (date(2025, 12, 31),), _provenance())

    assert (output / "prices.csv").read_bytes() == prices
    assert (output / "spy_trading_days.csv").read_text(encoding="utf-8") == "trade_date\n2025-12-31\n"
    assert '"source_kind": "existing_hash_pinned_cache"' in (
        output / "prices_provenance.json"
    ).read_text(encoding="utf-8")
    assert len(fsynced) >= 3
    assert not tuple(output.glob(".*.tmp"))


def test_publish_registers_final_before_post_link_attestation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a linked final escaped cleanup when its first identity read failed."""
    output = tmp_path / "exports"
    source = tmp_path / "worker-prices.csv"
    source.write_bytes(b"trade_date,ticker,open,high,low,close,volume\n")
    real_identity = exporter._file_identity
    failed_once = False

    def fail_first_final_identity(path: Path) -> tuple[int, int, int]:
        nonlocal failed_once
        if path.name == "prices.csv" and not failed_once:
            failed_once = True
            raise OSError("simulated post-link stat failure")
        return real_identity(path)

    monkeypatch.setattr(exporter, "_file_identity", fail_first_final_identity)

    with pytest.raises(OSError, match="post-link stat failure"):
        exporter._publish(output, source, (date(2025, 12, 31),), _provenance())

    assert not any((output / name).exists() for name in exporter._OUTPUT_NAMES)
    assert not tuple(output.glob(".*.tmp"))


def test_local_image_check_rejects_digest_that_is_not_already_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: Docker create could pull an absent image despite a pinned digest."""
    executable = tmp_path / "docker"
    executable.write_bytes(b"docker")
    image = "registry.invalid/worker@sha256:" + "a" * 64

    def missing_image(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1, "", "No such image")

    monkeypatch.setattr(exporter, "_docker_call", missing_image)

    with pytest.raises(RuntimeError, match="not available locally"):
        exporter._local_image_id(executable, image)


def test_docker_create_policy_forbids_pulls_and_requests_exact_limits(tmp_path: Path) -> None:
    """Break caught: create omitted local-only policy or requested different resource limits."""
    source = tmp_path / "worker.py"
    request = tmp_path / "request.json"
    cache = tmp_path / "cache.sqlite3"
    output = tmp_path / "output"
    for path in (source, request, cache):
        path.write_bytes(b"x")
    output.mkdir()
    mounts = (
        (source, "/worker/export_price_cache_worker.py", True),
        (request, "/input/request.json", True),
        (cache, "/input/cache.sqlite3", True),
        (output, "/output", False),
    )

    args = exporter._docker_create_args(
        "worker-name",
        "owner-token",
        "registry.invalid/worker@sha256:" + "a" * 64,
        mounts,
    )

    assert args[args.index("--pull") + 1] == "never"
    assert args[args.index("--pids-limit") + 1] == "64"
    assert args[args.index("--memory") + 1] == "2147483648b"
    assert args[args.index("--cpus") + 1] == "2"
    assert args[args.index("--tmpfs") + 1] == "/tmp:rw,noexec,nosuid,nodev,size=67108864"


def test_native_posix_access_keeps_root_private_and_grants_only_required_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: UID 65532 could not read owner-only inputs or write the output bind."""
    root = tmp_path / "private"
    root.mkdir()
    worker = tmp_path / "worker.py"
    request = root / "request.json"
    cache = root / "cache.sqlite3"
    output = root / "output"
    worker.write_bytes(b"worker")
    request.write_bytes(b"request")
    cache.write_bytes(b"cache")
    output.mkdir()
    modes: dict[Path, int] = {}

    def record_chmod(path: str | bytes | os.PathLike[str], mode: int) -> None:
        modes[Path(path)] = mode

    monkeypatch.setattr(exporter.os, "chmod", record_chmod)

    prepared = exporter._prepare_container_access(
        root,
        worker,
        request,
        cache,
        output,
        native_posix=True,
    )

    assert prepared.parent == root
    assert prepared.read_bytes() == b"worker"
    assert modes == {
        root: 0o700,
        prepared: 0o444,
        request: 0o444,
        cache: 0o444,
        output: 0o733,
    }


@pytest.mark.skipif(os.name != "posix", reason="requires native POSIX mode bits")
def test_native_posix_access_modes_are_effective_on_disk(tmp_path: Path) -> None:
    """Break caught: requested POSIX modes were not the effective bind-source modes."""
    root = tmp_path / "private"
    root.mkdir()
    worker = tmp_path / "worker.py"
    request = root / "request.json"
    cache = root / "cache.sqlite3"
    output = root / "output"
    worker.write_bytes(b"worker")
    request.write_bytes(b"request")
    cache.write_bytes(b"cache")
    output.mkdir()

    prepared = exporter._prepare_container_access(root, worker, request, cache, output)

    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(prepared.stat().st_mode) == 0o444
    assert stat.S_IMODE(request.stat().st_mode) == 0o444
    assert stat.S_IMODE(cache.stat().st_mode) == 0o444
    assert stat.S_IMODE(output.stat().st_mode) == 0o733


def _inspection(tmp_path: Path) -> tuple[dict[str, object], dict[str, tuple[Path, bool]]]:
    mounts = {
        "/worker/export_price_cache_worker.py": (tmp_path / "worker.py", True),
        "/input/request.json": (tmp_path / "request.json", True),
        "/input/cache.sqlite3": (tmp_path / "cache.sqlite3", True),
        "/output": (tmp_path / "output", False),
    }
    for path, _readonly in mounts.values():
        if path.suffix:
            path.write_bytes(b"x")
        else:
            path.mkdir()
    item: dict[str, object] = {
        "Id": "b" * 64,
        "Name": "/worker-name",
        "Image": "sha256:" + "c" * 64,
        "Config": {
            "Image": "registry.invalid/worker@sha256:" + "a" * 64,
            "Entrypoint": ["python"],
            "Cmd": [
                "/worker/export_price_cache_worker.py", "--request", "/input/request.json",
                "--cache", "/input/cache.sqlite3", "--output", "/output/prices.csv",
            ],
            "User": "65532:65532",
            "WorkingDir": "/worker",
            "Labels": {"pit-price-export.owner": "owner-token"},
        },
        "HostConfig": {
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "PidsLimit": 64,
            "Memory": 2_147_483_648,
            "NanoCpus": 2_000_000_000,
            "Tmpfs": {"/tmp": "nodev,size=64m,rw,nosuid,noexec"},
        },
        "NetworkSettings": {"Networks": {"none": {}}},
        "Mounts": [
            {
                "Destination": destination,
                "Source": str(source.resolve()),
                "RW": not readonly,
            }
            for destination, (source, readonly) in mounts.items()
        ],
    }
    return item, mounts


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("PidsLimit", 65),
        ("PidsLimit", 64.0),
        ("Memory", 2_147_483_647),
        ("NanoCpus", 1_000_000_000),
        ("Tmpfs", {"/tmp": "rw,noexec,nosuid,nodev,size=64m,exec"}),
    ],
)
def test_container_attestation_rejects_resource_limit_drift(
    tmp_path: Path,
    field: str,
    bad_value: object,
) -> None:
    """Break caught: inspected limits could differ from the requested confinement contract."""
    item, mounts = _inspection(tmp_path)
    mutated = deepcopy(item)
    mutated["HostConfig"][field] = bad_value  # type: ignore[index]

    with pytest.raises(RuntimeError, match="confinement differs"):
        exporter._validate_container_item(
            mutated,
            "b" * 64,
            "worker-name",
            "owner-token",
            "registry.invalid/worker@sha256:" + "a" * 64,
            "sha256:" + "c" * 64,
            mounts,
        )


def test_container_attestation_normalizes_exact_tmpfs_size_and_option_order(
    tmp_path: Path,
) -> None:
    """Break caught: harmless Docker tmpfs option reordering was confused with policy drift."""
    item, mounts = _inspection(tmp_path)

    exporter._validate_container_item(
        item,
        "b" * 64,
        "worker-name",
        "owner-token",
        "registry.invalid/worker@sha256:" + "a" * 64,
        "sha256:" + "c" * 64,
        mounts,
    )
