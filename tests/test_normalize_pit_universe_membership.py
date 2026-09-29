"""Focused PIT membership lineage normalization contract tests."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from core.pit_provenance import pit_canonical_json_sha256
from normalize_pit_universe_membership import (
    _csv_bytes,
    _load_price_identity,
    _normalize,
)


_UNIVERSES = ("nasdaq100", "russell2000", "sp500")
_RETRIEVED_AT = "2026-09-28T00:00:00Z"


def _source(
    tmp_path: Path,
    universe_id: str,
    events: list[tuple[str, str, int]],
) -> tuple[Path, Path, dict[str, object]]:
    membership_path = tmp_path / f"{universe_id}.csv"
    with membership_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(("effective_date", "ticker", "member"))
        writer.writerows(events)

    symbols = {ticker for _, ticker, _ in events}
    raw = membership_path.read_bytes()
    provenance: dict[str, object] = {
        "universe_id": universe_id,
        "membership_sha256": hashlib.sha256(raw).hexdigest(),
        "event_count": len(events),
        "first_effective_date": events[0][0],
        "last_effective_date": events[-1][0],
        "symbol_count": len(symbols),
        "source_kind": "deterministic fixture",
        "retrieved_at_utc": _RETRIEVED_AT,
    }
    provenance_path = tmp_path / f"{universe_id}.json"
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")
    return membership_path, provenance_path, provenance


def _identity(chain_id: str, start: str, end: str) -> dict[str, object]:
    return {"chain_id": chain_id, "admitted_start": start, "admitted_end": end}


def _price_identity_provenance(
    predecessor_start: str,
    predecessor_end: str,
) -> dict[str, object]:
    contracts = {
        "OLD": {
            "provider_symbol": "OLD",
            "identity_asof": predecessor_end,
            "admitted_start": predecessor_start,
            "admitted_end": predecessor_end,
            "chain_id": "renamed_co",
            "continuity_kind": "same_issuer_rename",
            "warmup_predecessor": None,
            "factor_anchor": False,
        },
        "NEW": {
            "provider_symbol": "NEW",
            "identity_asof": "2021-12-31",
            "admitted_start": "2021-06-10",
            "admitted_end": "2021-12-31",
            "chain_id": "renamed_co",
            "continuity_kind": "same_issuer_rename",
            "warmup_predecessor": "OLD",
            "factor_anchor": True,
        },
    }
    transitions = [
        {
            "effective_date": "2021-06-10",
            "predecessor": "OLD",
            "successor": "NEW",
            "chain_id": "renamed_co",
            "continuity_kind": "same_issuer_rename",
        }
    ]
    return {
        "price_identity_request_contracts": contracts,
        "price_identity_request_contracts_sha256": pit_canonical_json_sha256(
            contracts
        ),
        "price_identity_transitions": transitions,
    }


def _fiserv_segmented_price_identity(
    tmp_path: Path,
) -> tuple[dict[str, object], tuple[object, ...]]:
    contracts: dict[str, dict[str, object]] = {
        "FI": {
            "provider_symbol": "FI",
            "identity_asof": "2025-11-10",
            "admitted_start": "2023-06-07",
            "admitted_end": "2025-11-10",
            "chain_id": "fiserv",
            "continuity_kind": "same_issuer_ticker_reuse",
            "warmup_predecessor": "FISV",
            "factor_anchor": False,
        },
        "FISV": {
            "provider_symbol": "FISV",
            "identity_asof": "2025-12-31",
            "admitted_start": "2020-01-01",
            "admitted_end": "2025-12-31",
            "chain_id": "fiserv",
            "continuity_kind": "same_issuer_ticker_reuse",
            "warmup_predecessor": None,
            "factor_anchor": True,
        },
        "IWM": {
            "provider_symbol": "IWM",
            "identity_asof": "2025-12-31",
            "admitted_start": "2020-01-01",
            "admitted_end": "2025-12-31",
            "chain_id": "ref_iwm",
            "continuity_kind": "same_issuer_rename",
            "warmup_predecessor": None,
            "factor_anchor": True,
        },
        "QQQ": {
            "provider_symbol": "QQQ",
            "identity_asof": "2025-12-31",
            "admitted_start": "2020-01-01",
            "admitted_end": "2025-12-31",
            "chain_id": "ref_qqq",
            "continuity_kind": "same_issuer_rename",
            "warmup_predecessor": None,
            "factor_anchor": True,
        },
        "SPY": {
            "provider_symbol": "SPY",
            "identity_asof": "2025-12-31",
            "admitted_start": "2020-01-01",
            "admitted_end": "2025-12-31",
            "chain_id": "ref_spy",
            "continuity_kind": "same_issuer_rename",
            "warmup_predecessor": None,
            "factor_anchor": True,
        },
    }
    parent_digest = pit_canonical_json_sha256(contracts)
    segment_contract: dict[str, object] = {
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
        "source_assertions": [],
    }
    assertions: list[dict[str, object]] = []
    for assertion_id, effective, index in (
        ("synthetic-fiserv-2023", "2023-06-07", 0),
        ("synthetic-fiserv-2025", "2025-11-11", 1),
    ):
        source_bytes = f"synthetic non-evidentiary bytes {index}\n".encode()
        source_path = tmp_path / f"fiserv-synthetic-{index}.bin"
        source_path.write_bytes(source_bytes)
        assertions.append(
            {
                "assertion_id": assertion_id,
                "authority": "Synthetic test fixture; not source evidence",
                "url": f"https://example.test/fiserv-shape-{index}",
                "accession": None,
                "document_date": "2023-05-01" if index == 0 else "2025-10-01",
                "locator": "Synthetic shape-only assertion",
                "effective_date": effective,
                "supports": "Synthetic test data; no real filing or source claim.",
                "source_byte_sha256": hashlib.sha256(source_bytes).hexdigest(),
                "source_document_path": source_path.name,
            }
        )
    segment_contract["source_assertions"] = assertions
    provenance: dict[str, object] = {
        "price_identity_request_contracts": contracts,
        "price_identity_request_contracts_sha256": parent_digest,
        "price_identity_transitions": [],
        "price_identity_segments_v1": segment_contract,
        "price_identity_segments_v1_sha256": pit_canonical_json_sha256(
            segment_contract
        ),
    }
    return provenance, _load_price_identity(provenance, source_root=tmp_path)


def _fiserv_membership_sources(
    tmp_path: Path,
    *,
    include_handoff_events: bool,
) -> dict[str, tuple[Path, Path, dict[str, object]]]:
    events = [
        ("2020-01-02", "FISV", 1),
    ]
    if include_handoff_events:
        events.extend(
            [
                ("2023-06-07", "FI", 1),
                ("2023-06-07", "FISV", 0),
                ("2025-11-11", "FI", 0),
                ("2025-11-11", "FISV", 1),
            ]
        )
    return {
        universe: _source(tmp_path, universe, list(events))
        for universe in _UNIVERSES
    }


def test_normalization_preserves_overlaps_renames_and_short_lived_memberships(
    tmp_path: Path,
) -> None:
    """Break caught: dropping an index tag, rename continuity, or brief affiliation."""
    sources = {
        "sp500": _source(
            tmp_path,
            "sp500",
            [
                ("2021-01-01", "AAA", 1),
                ("2021-01-01", "OLD", 1),
                ("2021-06-10", "NEW", 1),
                ("2021-06-10", "OLD", 0),
                ("2021-07-01", "AAA", 0),
            ],
        ),
        "nasdaq100": _source(
            tmp_path,
            "nasdaq100",
            [("2021-01-01", "AAA", 1)],
        ),
        "russell2000": _source(
            tmp_path,
            "russell2000",
            [
                ("2021-05-03", "BRIEF", 1),
                ("2021-05-07", "BRIEF", 0),
            ],
        ),
    }
    identities = {
        "AAA": _identity("alpha", "2021-01-01", "2021-12-31"),
        "OLD": _identity("renamed_co", "2021-01-01", "2021-06-09"),
        "NEW": _identity("renamed_co", "2021-06-10", "2021-12-31"),
        "BRIEF": _identity("brief_co", "2021-01-01", "2021-12-31"),
    }
    transitions = (
        {
            "effective_date": "2021-06-10",
            "predecessor": "OLD",
            "successor": "NEW",
            "chain_id": "renamed_co",
            "continuity_kind": "same_issuer_rename",
        },
    )

    rows, source_bindings, coalesced = _normalize(
        sources,
        identities=identities,
        transitions=transitions,
    )

    assert rows == [
        ("2021-01-01", "alpha", "nasdaq100", 1),
        ("2021-01-01", "alpha", "sp500", 1),
        ("2021-01-01", "renamed_co", "sp500", 1),
        ("2021-05-03", "brief_co", "russell2000", 1),
        ("2021-05-07", "brief_co", "russell2000", 0),
        ("2021-07-01", "alpha", "sp500", 0),
    ]
    assert coalesced == 1
    assert [item["universe_id"] for item in source_bindings] == list(_UNIVERSES)


def test_segmented_normalizer_coalesces_both_fiserv_handoffs_and_keeps_v3_rows(
    tmp_path: Path,
) -> None:
    _, loaded = _fiserv_segmented_price_identity(tmp_path)
    identities, transitions, _, _, segment_contract = loaded
    sources = _fiserv_membership_sources(
        tmp_path, include_handoff_events=True
    )

    rows, _, coalesced = _normalize(
        sources,
        identities=identities,
        transitions=transitions,
        segment_contract=segment_contract,
    )

    assert rows == [
        ("2020-01-02", "fiserv", "nasdaq100", 1),
        ("2020-01-02", "fiserv", "russell2000", 1),
        ("2020-01-02", "fiserv", "sp500", 1),
    ]
    assert coalesced == 6
    assert all(len(row) == 4 for row in rows)
    assert _csv_bytes(rows).splitlines()[0] == (
        b"effective_date,security_lineage_id,universe_id,member"
    )


def test_segmented_normalizer_preserves_lineage_without_handoff_membership_events(
    tmp_path: Path,
) -> None:
    _, loaded = _fiserv_segmented_price_identity(tmp_path)
    identities, transitions, _, _, segment_contract = loaded
    sources = _fiserv_membership_sources(
        tmp_path, include_handoff_events=False
    )

    rows, _, coalesced = _normalize(
        sources,
        identities=identities,
        transitions=transitions,
        segment_contract=segment_contract,
    )

    assert rows == [
        ("2020-01-02", "fiserv", "nasdaq100", 1),
        ("2020-01-02", "fiserv", "russell2000", 1),
        ("2020-01-02", "fiserv", "sp500", 1),
    ]
    assert coalesced == 0


def test_segmented_normalizer_rejects_membership_event_outside_segment(
    tmp_path: Path,
) -> None:
    _, loaded = _fiserv_segmented_price_identity(tmp_path)
    identities, transitions, _, _, segment_contract = loaded
    sources = {
        universe: _source(
            tmp_path,
            universe,
            [
                ("2020-01-02", "FISV", 1),
                ("2023-06-08", "FISV", 0),
            ],
        )
        for universe in _UNIVERSES
    }

    with pytest.raises(ValueError, match="outside its authenticated price identity segment"):
        _normalize(
            sources,
            identities=identities,
            transitions=transitions,
            segment_contract=segment_contract,
        )


@pytest.mark.parametrize(
    ("predecessor_start", "predecessor_end"),
    [
        ("2021-01-01", "2021-06-10"),
        ("2021-06-10", "2021-06-10"),
        ("2021-06-11", "2021-06-11"),
    ],
)
def test_price_identity_loader_rejects_predecessor_not_admitted_before_boundary(
    predecessor_start: str,
    predecessor_end: str,
) -> None:
    """Break caught: a valid successor cannot mask overlapping predecessor dates."""
    with pytest.raises(
        ValueError,
        match="predecessor is not admitted before boundary",
    ):
        _load_price_identity(
            _price_identity_provenance(predecessor_start, predecessor_end)
        )


@pytest.mark.parametrize(
    ("predecessor_start", "predecessor_end"),
    [
        ("2021-01-01", "2021-06-10"),
        ("2021-06-10", "2021-06-10"),
        ("2021-06-11", "2021-06-11"),
    ],
)
def test_direct_normalize_rejects_transition_with_invalid_predecessor_dates(
    tmp_path: Path,
    predecessor_start: str,
    predecessor_end: str,
) -> None:
    """Break caught: direct normalization cannot bypass loader chronology checks."""
    sources = {
        "sp500": _source(
            tmp_path,
            "sp500",
            [
                ("2021-01-01", "OLD", 1),
                ("2021-06-10", "NEW", 1),
                ("2021-06-10", "OLD", 0),
            ],
        ),
        "nasdaq100": _source(
            tmp_path,
            "nasdaq100",
            [("2021-01-01", "AAA", 1)],
        ),
        "russell2000": _source(
            tmp_path,
            "russell2000",
            [("2021-01-01", "BRIEF", 1)],
        ),
    }
    identities = {
        "AAA": _identity("alpha", "2021-01-01", "2021-12-31"),
        "OLD": _identity("renamed_co", predecessor_start, predecessor_end),
        "NEW": _identity("renamed_co", "2021-06-10", "2021-12-31"),
        "BRIEF": _identity("brief_co", "2021-01-01", "2021-12-31"),
    }
    transitions = (
        {
            "effective_date": "2021-06-10",
            "predecessor": "OLD",
            "successor": "NEW",
            "chain_id": "renamed_co",
            "continuity_kind": "same_issuer_rename",
        },
    )

    with pytest.raises(
        ValueError,
        match="predecessor is not admitted before boundary",
    ):
        _normalize(sources, identities=identities, transitions=transitions)


def test_unreviewed_same_day_identity_change_is_rejected(tmp_path: Path) -> None:
    """Break caught: same-lineage ticker swaps cannot be silently guessed."""
    sources = {
        "sp500": _source(
            tmp_path,
            "sp500",
            [
                ("2021-01-01", "OLD", 1),
                ("2021-06-10", "NEW", 1),
                ("2021-06-10", "OLD", 0),
            ],
        ),
        "nasdaq100": _source(
            tmp_path,
            "nasdaq100",
            [("2021-01-01", "AAA", 1)],
        ),
        "russell2000": _source(
            tmp_path,
            "russell2000",
            [("2021-01-01", "BRIEF", 1)],
        ),
    }
    identities = {
        "AAA": _identity("alpha", "2021-01-01", "2021-12-31"),
        "OLD": _identity("renamed_co", "2021-01-01", "2021-12-31"),
        "NEW": _identity("renamed_co", "2021-06-10", "2021-12-31"),
        "BRIEF": _identity("brief_co", "2021-01-01", "2021-12-31"),
    }

    with pytest.raises(ValueError, match="ambiguous same-lineage membership transitions"):
        _normalize(sources, identities=identities, transitions=())


def test_out_of_range_predecessor_removal_requires_same_day_successor_addition(
    tmp_path: Path,
) -> None:
    """Break caught: a transition declaration alone cannot authorize a removal."""
    sources = {
        "sp500": _source(
            tmp_path,
            "sp500",
            [
                ("2021-01-01", "OLD", 1),
                ("2021-06-10", "OLD", 0),
            ],
        ),
        "nasdaq100": _source(
            tmp_path,
            "nasdaq100",
            [("2021-01-01", "AAA", 1)],
        ),
        "russell2000": _source(
            tmp_path,
            "russell2000",
            [("2021-01-01", "BRIEF", 1)],
        ),
    }
    identities = {
        "AAA": _identity("alpha", "2021-01-01", "2021-12-31"),
        "OLD": _identity("renamed_co", "2021-01-01", "2021-06-09"),
        "NEW": _identity("renamed_co", "2021-06-10", "2021-12-31"),
        "BRIEF": _identity("brief_co", "2021-01-01", "2021-12-31"),
    }
    transitions = (
        {
            "effective_date": "2021-06-10",
            "predecessor": "OLD",
            "successor": "NEW",
            "chain_id": "renamed_co",
            "continuity_kind": "same_issuer_rename",
        },
    )

    with pytest.raises(ValueError, match="outside authenticated identity bounds"):
        _normalize(sources, identities=identities, transitions=transitions)


def test_membership_event_outside_authenticated_identity_dates_is_rejected(
    tmp_path: Path,
) -> None:
    """Break caught: a membership row cannot extend a price identity's dates."""
    sources = {
        "sp500": _source(tmp_path, "sp500", [("2021-01-01", "AAA", 1)]),
        "nasdaq100": _source(
            tmp_path,
            "nasdaq100",
            [("2021-01-01", "BRIEF", 1)],
        ),
        "russell2000": _source(
            tmp_path,
            "russell2000",
            [("2021-01-01", "LATE", 1)],
        ),
    }
    identities = {
        "AAA": _identity("alpha", "2021-01-01", "2021-12-31"),
        "BRIEF": _identity("brief_co", "2021-01-01", "2021-12-31"),
        "LATE": _identity("late_co", "2021-01-02", "2021-12-31"),
    }

    with pytest.raises(ValueError, match="outside authenticated identity bounds"):
        _normalize(sources, identities=identities, transitions=())
