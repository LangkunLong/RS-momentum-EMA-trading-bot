"""Focused PIT membership lineage normalization contract tests."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from normalize_pit_universe_membership import _normalize


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
