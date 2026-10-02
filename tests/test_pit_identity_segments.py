"""Synthetic tests for the opt-in PIT price identity segment contract."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import replace
from datetime import date
import json
from pathlib import Path

import pytest

from core.pit_data import (
    PITDataBundle,
    PriceIdentityTransitionContract,
    _segment_contract_object,
    validate_price_identity_segments_v1,
)
from core.pit_provenance import pit_canonical_json_bytes, pit_canonical_json_sha256
from core.pit_universe_v3 import PointInTimeUniverseV3


_MAP_SHA256 = "a" * 64


def _identity(
    ticker: str,
    *,
    chain_id: str,
    start: str = "2020-01-01",
    end: str = "2025-12-31",
    factor_anchor: bool = True,
    warmup_predecessor: str | None = None,
) -> dict[str, object]:
    return {
        "provider_symbol": ticker,
        "identity_asof": end,
        "admitted_start": start,
        "admitted_end": end,
        "chain_id": chain_id,
        "continuity_kind": "same_issuer_ticker_reuse",
        "warmup_predecessor": warmup_predecessor,
        "factor_anchor": factor_anchor,
    }


def _request_contracts() -> dict[str, dict[str, object]]:
    return {
        "FI": _identity(
            "FI",
            chain_id="fiserv",
            start="2023-06-07",
            end="2025-11-10",
            factor_anchor=False,
            warmup_predecessor="FISV",
        ),
        "FISV": _identity("FISV", chain_id="fiserv"),
        "IWM": _identity("IWM", chain_id="ref_iwm"),
        "QQQ": _identity("QQQ", chain_id="ref_qqq"),
        "SPY": _identity("SPY", chain_id="ref_spy"),
    }


def _segment_contract(parent_digest: str) -> dict[str, object]:
    return {
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
                "source_assertion_ids": ["fiserv-2023-sec"],
            },
            {
                "effective_date": "2025-11-11",
                "predecessor_segment_id": "fiserv-fi-2023-2025",
                "successor_segment_id": "fiserv-fisv-post-2025",
                "chain_id": "fiserv",
                "continuity_kind": "same_issuer_ticker_reuse",
                "source_assertion_ids": [
                    "fiserv-2025-8a",
                    "fiserv-2025-issuer",
                ],
            },
        ],
        "source_assertions": [
            {
                "assertion_id": "fiserv-2023-sec",
                "authority": "U.S. Securities and Exchange Commission, Fiserv Form 8-K Exhibit 99.1",
                "url": "https://www.sec.gov/Archives/edgar/data/798354/000119312523154199/d470900dex991.htm",
                "accession": "0001193125-23-154199",
                "document_date": "2023-05-25",
                "locator": "Ticker and listing-transfer announcement",
                "effective_date": "2023-06-07",
                "supports": "Fiserv common stock changes from Nasdaq ticker FISV to NYSE ticker FI effective 2023-06-07.",
                "source_byte_sha256": "1" * 64,
            },
            {
                "assertion_id": "fiserv-2025-8a",
                "authority": "U.S. Securities and Exchange Commission, Fiserv Form 8-A",
                "url": "https://www.sec.gov/Archives/edgar/data/798354/000119312525274207/d53898d8a12b.htm",
                "accession": "0001193125-25-274207",
                "document_date": "2025-11-10",
                "locator": "Explanatory Note and securities registered table",
                "effective_date": "2025-11-11",
                "supports": "The same Fiserv common stock listing moves from NYSE ticker FI to Nasdaq ticker FISV effective 2025-11-11.",
                "source_byte_sha256": "2" * 64,
            },
            {
                "assertion_id": "fiserv-2025-issuer",
                "authority": "Fiserv investor relations",
                "url": "https://investors.fiserv.com/news-releases/news-release-details/fiserv-announces-transfer-stock-exchange-listing-nasdaq",
                "accession": None,
                "document_date": "2025-10-29",
                "locator": "Ticker-change announcement paragraph",
                "effective_date": "2025-11-11",
                "supports": "Fiserv Class A common stock begins Nasdaq trading under its original symbol FISV on 2025-11-11.",
                "source_byte_sha256": "3" * 64,
            },
        ],
    }


def _bundle_with_segments(
    tmp_path: Path,
    *,
    edit: str | None = None,
    refresh_segment_digest: bool = True,
    include_segment_object: bool = True,
    include_segment_digest: bool = True,
    weekend_handoff: bool = False,
) -> tuple[PITDataBundle, Path]:
    contracts = _request_contracts()
    if weekend_handoff:
        contracts["FI"]["admitted_start"] = "2021-10-04"
    contract_digest = pit_canonical_json_sha256(contracts)
    segment_contract = _segment_contract(contract_digest)
    if weekend_handoff:
        segment_contract["segments"][0]["admitted_end"] = "2021-10-01"
        segment_contract["segments"][1]["admitted_start"] = "2021-10-04"
        segment_contract["transitions"][0]["effective_date"] = "2021-10-04"
        segment_contract["source_assertions"][0]["effective_date"] = "2021-10-04"
        segment_contract["source_assertions"][0]["document_date"] = "2021-09-20"
    for index, assertion in enumerate(segment_contract["source_assertions"]):
        source_bytes = f"synthetic test evidence {index}\n".encode("utf-8")
        source_path = tmp_path / f"synthetic-source-{index}.bin"
        source_path.write_bytes(source_bytes)
        assertion["authority"] = "Synthetic test fixture; not source evidence"
        assertion["url"] = f"https://example.test/identity-assertion-{index}"
        assertion["accession"] = None
        assertion["locator"] = "Synthetic shape-only assertion"
        assertion["supports"] = "Synthetic test data; no real filing or source claim."
        assertion["source_document_path"] = source_path.name
        assertion["source_byte_sha256"] = hashlib.sha256(source_bytes).hexdigest()
    if edit is not None:
        if edit == "wrong_parent_digest":
            segment_contract["parent_price_identity_request_contracts_sha256"] = "b" * 64
        elif edit == "duplicate_segment_id":
            segment_contract["segments"].append(deepcopy(segment_contract["segments"][0]))
        elif edit == "overlap_predecessor":
            segment_contract["segments"][0]["admitted_end"] = "2023-06-07"
        elif edit == "gap_before_successor":
            segment_contract["segments"][1]["admitted_start"] = "2023-06-08"
        elif edit == "missing_source_assertion":
            segment_contract["transitions"][0]["source_assertion_ids"] = ["unknown-source"]
        elif edit == "wrong_source_effective_date":
            segment_contract["source_assertions"][0]["effective_date"] = "2023-06-08"
        elif edit == "unknown_provider_symbol":
            segment_contract["segments"][0]["provider_symbol"] = "NOPE"
        elif edit == "missing_source_hash":
            segment_contract["source_assertions"][0]["source_byte_sha256"] = None
        elif edit == "missing_source_bytes":
            segment_contract["source_assertions"][0].pop("source_document_path")
        elif edit == "source_hash_mismatch":
            segment_contract["source_assertions"][0]["source_byte_sha256"] = "1" * 64
        elif edit == "source_path_traversal":
            segment_contract["source_assertions"][0]["source_document_path"] = "../outside.bin"
        elif edit == "cycle":
            segment_contract["transitions"][1]["successor_segment_id"] = "fiserv-fisv-pre-2023"
        else:
            raise AssertionError(f"unknown fixture edit: {edit}")

    provenance: dict[str, object] = {
        "price_identity_request_contracts": contracts,
        "price_identity_request_contracts_sha256": contract_digest,
        "price_identity_map_sha256": _MAP_SHA256,
        "price_identity_transitions": [],
    }
    if include_segment_object:
        provenance["price_identity_segments_v1"] = segment_contract
        if include_segment_digest:
            if refresh_segment_digest:
                provenance["price_identity_segments_v1_sha256"] = pit_canonical_json_sha256(
                    segment_contract
                )
            else:
                provenance["price_identity_segments_v1_sha256"] = "0" * 64

    provenance_path = tmp_path / "prices_provenance.json"
    provenance_path.write_bytes(pit_canonical_json_bytes(provenance))
    bundle = PITDataBundle.__new__(PITDataBundle)
    bundle.metadata = {
        "schema_version": "3",
        "prices_provenance_sha256": hashlib.sha256(provenance_path.read_bytes()).hexdigest(),
        "price_identity_request_contracts_sha256": contract_digest,
        "price_identity_map_sha256": _MAP_SHA256,
        "data_cutoff": "2025-12-31",
    }
    bundle._tradable_symbols = frozenset({"FISV", "FI"})
    bundle._reference_symbols = frozenset({"SPY", "QQQ", "IWM"})
    bundle._security_lineage_ids = None
    return bundle, provenance_path


def test_fiserv_segments_resolve_both_effective_dated_handoffs_and_v3_ticker(
    tmp_path: Path,
) -> None:
    bundle, provenance_path = _bundle_with_segments(tmp_path)
    contract = bundle.load_price_identity_transition_contract(provenance_path)

    assert contract.transitions == ()
    assert contract.resolve_segment(
        "fiserv-fisv-pre-2023", "2023-06-06"
    ).provider_symbol == "FISV"
    assert contract.resolve_segment(
        "fiserv-fisv-pre-2023", "2023-06-07"
    ).provider_symbol == "FI"
    assert contract.resolve_segment(
        "fiserv-fi-2023-2025", "2025-11-11"
    ).provider_symbol == "FISV"
    assert contract.resolve_ticker_for_lineage(
        "fiserv", "2023-06-07"
    ) == "FI"
    assert contract.resolve_ticker_for_lineage(
        "fiserv", "2025-11-11"
    ) == "FISV"
    assert contract.resolve_open_holding_v3("FISV", "2023-06-07") == "FI"
    assert contract.resolve_open_holding_v3("FI", "2025-11-11") == "FISV"

    membership = PointInTimeUniverseV3.from_rows(
        [
            {
                "effective_date": "2020-01-01",
                "security_lineage_id": "fiserv",
                "universe_id": "sp500",
                "member": True,
            }
        ],
        contract,
    )
    assert membership.ticker_for_lineage_at("fiserv", "2023-06-06") == "FISV"
    assert membership.ticker_for_lineage_at("fiserv", "2023-06-07") == "FI"
    assert membership.ticker_for_lineage_at("fiserv", "2025-11-10") == "FI"
    assert membership.ticker_for_lineage_at("fiserv", "2025-11-11") == "FISV"


def test_prebundle_segment_input_uses_exact_parent_and_has_no_provenance_placeholder(
    tmp_path: Path,
) -> None:
    _, provenance_path = _bundle_with_segments(tmp_path)
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    parent = provenance["price_identity_request_contracts"]

    validated = validate_price_identity_segments_v1(
        provenance["price_identity_segments_v1"],
        declared_sha256=provenance["price_identity_segments_v1_sha256"],
        parent_request_contracts=parent,
        identities=parent,
        source_evidence_root=tmp_path,
        data_cutoff=date(2025, 12, 31),
    )

    assert validated.parent_request_contracts_sha256 == pit_canonical_json_sha256(parent)
    assert validated.segment_contract_sha256 == provenance["price_identity_segments_v1_sha256"]
    assert pit_canonical_json_sha256(validated.to_provenance_object()) == validated.segment_contract_sha256
    assert len(validated.source_assertions) == 3
    assert not hasattr(validated, "prices_provenance_sha256")


def test_prebundle_segment_input_rejects_parent_identity_mismatch(tmp_path: Path) -> None:
    _, provenance_path = _bundle_with_segments(tmp_path)
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    parent = provenance["price_identity_request_contracts"]
    wrong_identities = deepcopy(parent)
    wrong_identities["FI"]["provider_symbol"] = "FISV"

    with pytest.raises(ValueError, match="do not match validated identities"):
        validate_price_identity_segments_v1(
            provenance["price_identity_segments_v1"],
            declared_sha256=provenance["price_identity_segments_v1_sha256"],
            parent_request_contracts=parent,
            identities=wrong_identities,
            source_evidence_root=tmp_path,
            data_cutoff=date(2025, 12, 31),
        )


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        ("bad_date", "parent request contract row is invalid"),
        ("noncanonical_ticker", "parent request contract row is invalid"),
        ("bad_factor_anchor_type", "parent request contract row is invalid"),
        ("extra_field", "parent request contract row is invalid"),
    ],
)
def test_prebundle_segment_input_validates_parent_row_shape_and_identity(
    tmp_path: Path,
    edit: str,
    message: str,
) -> None:
    _, provenance_path = _bundle_with_segments(tmp_path)
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    parent = deepcopy(provenance["price_identity_request_contracts"])
    if edit == "bad_date":
        parent["FI"]["admitted_start"] = "2023-99-07"
    elif edit == "noncanonical_ticker":
        parent["fi"] = parent.pop("FI")
    elif edit == "bad_factor_anchor_type":
        parent["FISV"]["factor_anchor"] = "true"
    elif edit == "extra_field":
        parent["FISV"]["unexpected"] = "extra"

    with pytest.raises(ValueError, match=message):
        validate_price_identity_segments_v1(
            provenance["price_identity_segments_v1"],
            declared_sha256=provenance["price_identity_segments_v1_sha256"],
            parent_request_contracts=parent,
            identities=parent,
            source_evidence_root=tmp_path,
            data_cutoff=date(2025, 12, 31),
        )


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        ("wrong_parent_digest", "parent digest does not match request contracts"),
        ("overlap_predecessor", "invalid segment transition"),
        ("gap_before_successor", "invalid segment transition"),
        ("cycle", "invalid segment transition"),
        ("wrong_source_effective_date", "source assertion is missing or misdated"),
        ("source_hash_mismatch", "source document hash does not match assertion"),
        ("source_path_traversal", "source document path is invalid"),
    ],
)
def test_prebundle_segment_input_reuses_existing_source_graph_and_date_gates(
    tmp_path: Path,
    edit: str,
    message: str,
) -> None:
    _, provenance_path = _bundle_with_segments(tmp_path, edit=edit)
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    parent = provenance["price_identity_request_contracts"]

    with pytest.raises(ValueError, match=message):
        validate_price_identity_segments_v1(
            provenance["price_identity_segments_v1"],
            declared_sha256=provenance["price_identity_segments_v1_sha256"],
            parent_request_contracts=parent,
            identities=parent,
            source_evidence_root=tmp_path,
            data_cutoff=date(2025, 12, 31),
        )


def test_prebundle_segment_input_confines_source_bytes_to_evidence_root(
    tmp_path: Path,
) -> None:
    _, provenance_path = _bundle_with_segments(tmp_path)
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    other_root = tmp_path / "other-evidence-root"
    other_root.mkdir()
    parent = provenance["price_identity_request_contracts"]

    with pytest.raises(ValueError, match="source document is unavailable"):
        validate_price_identity_segments_v1(
            provenance["price_identity_segments_v1"],
            declared_sha256=provenance["price_identity_segments_v1_sha256"],
            parent_request_contracts=parent,
            identities=parent,
            source_evidence_root=other_root,
            data_cutoff=date(2025, 12, 31),
        )


def test_existing_transition_contract_constructor_keeps_legacy_behavior() -> None:
    contracts = {
        "AAA": {
            "provider_symbol": "AAA",
            "admitted_start": "2020-01-01",
            "admitted_end": "2025-12-31",
            "chain_id": "alpha",
        }
    }
    contract = PriceIdentityTransitionContract("a" * 64, "b" * 64, contracts, ())

    assert contract.resolve_open_holding_v3("AAA", "2024-03-04") == "AAA"
    assert contract.segments == {}
    assert contract.segment_transitions == ()


def test_weekend_only_closed_session_gap_is_allowed(tmp_path: Path) -> None:
    bundle, provenance_path = _bundle_with_segments(
        tmp_path, weekend_handoff=True
    )
    contract = bundle.load_price_identity_transition_contract(provenance_path)

    assert contract.resolve_ticker_for_lineage("fiserv", "2021-10-04") == "FI"


def test_segment_contract_direct_constructor_cannot_bypass_validation(
    tmp_path: Path,
) -> None:
    bundle, provenance_path = _bundle_with_segments(tmp_path)
    contract = bundle.load_price_identity_transition_contract(provenance_path)

    with pytest.raises(ValueError, match="verified source evidence"):
        PriceIdentityTransitionContract(
            contract.prices_provenance_sha256,
            contract.request_contracts_sha256,
            contract.identities,
            contract.transitions,
            contract.segments,
            contract.segment_transitions,
            contract.source_assertions,
            contract.segment_parent_request_contracts_sha256,
            contract.segment_contract_sha256,
        )

    bad_last_edge = replace(
        contract.segment_transitions[-1],
        successor_segment_id="fiserv-fisv-pre-2023",
    )
    bad_edges = (*contract.segment_transitions[:-1], bad_last_edge)
    bad_payload = _segment_contract_object(
        contract.segment_parent_request_contracts_sha256,
        contract.segments,
        bad_edges,
        contract.source_assertions,
    )
    with pytest.raises(ValueError):
        PriceIdentityTransitionContract(
            contract.prices_provenance_sha256,
            contract.request_contracts_sha256,
            contract.identities,
            contract.transitions,
            contract.segments,
            bad_edges,
            contract.source_assertions,
            contract.segment_parent_request_contracts_sha256,
            pit_canonical_json_sha256(bad_payload),
            provenance_path.parent,
        )


def test_segment_contract_constructor_recomputes_parent_identity_digest(
    tmp_path: Path,
) -> None:
    bundle, provenance_path = _bundle_with_segments(tmp_path)
    contract = bundle.load_price_identity_transition_contract(provenance_path)
    altered_identities = {
        ticker: dict(identity) for ticker, identity in contract.identities.items()
    }
    altered_identities["SPY"]["admitted_end"] = "2025-12-30"
    altered_identities["SPY"]["identity_asof"] = "2025-12-30"

    with pytest.raises(ValueError, match="parent request-contract digest"):
        PriceIdentityTransitionContract(
            contract.prices_provenance_sha256,
            contract.request_contracts_sha256,
            altered_identities,
            contract.transitions,
            contract.segments,
            contract.segment_transitions,
            contract.source_assertions,
            contract.segment_parent_request_contracts_sha256,
            contract.segment_contract_sha256,
            provenance_path.parent,
        )


@pytest.mark.parametrize(
    "edit",
    [
        "wrong_parent_digest",
        "duplicate_segment_id",
        "overlap_predecessor",
        "gap_before_successor",
        "missing_source_assertion",
        "wrong_source_effective_date",
        "unknown_provider_symbol",
        "missing_source_hash",
        "missing_source_bytes",
        "source_hash_mismatch",
        "source_path_traversal",
        "cycle",
    ],
)
def test_segment_loader_rejects_invalid_fiserv_contracts(
    tmp_path: Path,
    edit: str,
) -> None:
    bundle, provenance_path = _bundle_with_segments(tmp_path, edit=edit)

    with pytest.raises(ValueError):
        bundle.load_price_identity_transition_contract(provenance_path)


def test_segment_loader_rejects_unbound_segment_digest(tmp_path: Path) -> None:
    bundle, provenance_path = _bundle_with_segments(
        tmp_path, refresh_segment_digest=False
    )

    with pytest.raises(ValueError, match="segment contract digest"):
        bundle.load_price_identity_transition_contract(provenance_path)


def test_segment_loader_rejects_hash_without_segment_object(tmp_path: Path) -> None:
    bundle, provenance_path = _bundle_with_segments(
        tmp_path,
        include_segment_object=False,
    )
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["price_identity_segments_v1_sha256"] = "c" * 64
    provenance_path.write_bytes(pit_canonical_json_bytes(provenance))
    bundle.metadata["prices_provenance_sha256"] = hashlib.sha256(
        provenance_path.read_bytes()
    ).hexdigest()

    with pytest.raises(ValueError, match="segment object and digest must appear together"):
        bundle.load_price_identity_transition_contract(provenance_path)
