import hashlib
from datetime import date
from pathlib import Path

import fetch_sp500_membership as command
import core.public_membership as membership
from core.public_membership import _normalize, load_symbol_map


_PINNED_URL = "https://example.com/index?oldid=123"


def test_reviewed_map_projects_snapshot_ticker_to_evaluation_cutoff(tmp_path: Path) -> None:
    """A post-cutoff snapshot label must not leak backward into PIT membership."""
    raw = b"""
    <table>
      <tr><th>Symbol</th><th>Security</th></tr>
      <tr><td>MRSH</td><td>Marsh</td></tr>
      <tr><td>CCC</td><td>Current Co</td></tr>
    </table>
    <table>
      <tr><th rowspan="2">Effective Date</th><th colspan="2">Added</th><th colspan="2">Removed</th></tr>
      <tr><th>Ticker</th><th>Security</th><th>Ticker</th><th>Security</th></tr>
      <tr><td>January 3, 2022</td><td>CCC</td><td>Current Co</td><td>BBB</td><td>Before Co</td></tr>
    </table>
    """
    mapping_path = tmp_path / "symbols.csv"
    mapping_path.write_text(
        "source_ticker,canonical_ticker,effective_start,effective_end,reason\n"
        "MRSH,MMC,2021-01-01,2025-12-31,Official post-cutoff ticker change\n",
        encoding="utf-8",
    )

    export = _normalize(
        raw,
        _PINNED_URL,
        date(2021, 1, 1),
        date(2025, 12, 31),
        mappings=load_symbol_map(mapping_path),
    )

    seed = {event.ticker for event in export.events if event.effective_date == date(2021, 1, 1)}
    assert "MMC" in seed
    assert "MRSH" not in {event.ticker for event in export.events}
    assert export.company_names["MMC"] == "Marsh"


def test_reviewed_map_emits_in_window_ticker_identity_transition(tmp_path: Path) -> None:
    """Removing identity transitions must make the CDAY-to-DAY assertion fail."""
    raw = b"""
    <table>
      <tr><th>Symbol</th><th>Security</th></tr>
      <tr><td>NEW</td><td>New Co</td></tr>
    </table>
    <table>
      <tr><th rowspan="2">Effective Date</th><th colspan="2">Added</th><th colspan="2">Removed</th></tr>
      <tr><th>Ticker</th><th>Security</th><th>Ticker</th><th>Security</th></tr>
      <tr><td>February 9, 2026</td><td>NEW</td><td>New Co</td><td>DAY</td><td>Dayforce</td></tr>
      <tr><td>September 20, 2021</td><td>CDAY</td><td>Ceridian</td><td>OTHER</td><td>Other Co</td></tr>
    </table>
    """
    mapping_path = tmp_path / "symbols.csv"
    mapping_path.write_text(
        "source_ticker,canonical_ticker,effective_start,effective_end,reason\n"
        "DAY,CDAY,2021-01-01,2024-01-31,Official ticker timeline\n"
        "DAY,DAY,2024-02-01,2025-12-31,Official ticker timeline\n",
        encoding="utf-8",
    )

    export = _normalize(
        raw,
        _PINNED_URL,
        date(2021, 1, 1),
        date(2025, 12, 31),
        mappings=load_symbol_map(mapping_path),
    )

    timeline = [
        (event.effective_date.isoformat(), event.ticker, event.member)
        for event in export.events
        if event.ticker in {"CDAY", "DAY"}
    ]
    assert timeline == [
        ("2021-09-20", "CDAY", True),
        ("2024-02-01", "CDAY", False),
        ("2024-02-01", "DAY", True),
    ]


def test_symbol_map_provenance_preserves_hash_ranges_and_reasons(tmp_path: Path) -> None:
    """Dropping reviewed identity evidence from provenance must fail this test."""
    mapping_path = tmp_path / "symbols.csv"
    raw = (
        "source_ticker,canonical_ticker,effective_start,effective_end,reason\n"
        "MRSH,MMC,2021-01-01,2025-12-31,Official issuer evidence\n"
    ).encode()
    mapping_path.write_bytes(raw)
    mappings = load_symbol_map(mapping_path)

    mapping_path.write_text("changed after parsing\n", encoding="utf-8")

    assert command._symbol_map_provenance(mappings) == {
        "symbol_map_sha256": hashlib.sha256(raw).hexdigest(),
        "reviewed_symbol_mappings": [
            {
                "source_ticker": "MRSH",
                "canonical_ticker": "MMC",
                "effective_start": "2021-01-01",
                "effective_end": "2025-12-31",
                "reason": "Official issuer evidence",
            }
        ],
    }


def test_symbol_map_rejects_full_window_coverage_gap(tmp_path: Path) -> None:
    mapping_path = tmp_path / "symbols.csv"
    mapping_path.write_text(
        "source_ticker,canonical_ticker,effective_start,effective_end,reason\n"
        "DAY,CDAY,2021-01-01,2024-01-30,Official timeline\n"
        "DAY,DAY,2024-02-01,2025-12-31,Official timeline\n",
        encoding="utf-8",
    )

    try:
        load_symbol_map(mapping_path)
    except ValueError as exc:
        assert str(exc) == "symbol map contains a coverage gap for DAY"
    else:
        raise AssertionError("coverage gap was accepted")


def test_symbol_map_rejects_full_window_overlap(tmp_path: Path) -> None:
    mapping_path = tmp_path / "symbols.csv"
    mapping_path.write_text(
        "source_ticker,canonical_ticker,effective_start,effective_end,reason\n"
        "DAY,CDAY,2021-01-01,2024-02-01,Official timeline\n"
        "DAY,DAY,2024-02-01,2025-12-31,Official timeline\n",
        encoding="utf-8",
    )

    try:
        load_symbol_map(mapping_path)
    except ValueError as exc:
        assert str(exc) == "symbol map contains overlapping ranges for DAY"
    else:
        raise AssertionError("overlap was accepted")


def test_snapshot_name_is_propagated_to_every_reviewed_identity(tmp_path: Path) -> None:
    raw = b"""
    <table>
      <tr><th>Symbol</th><th>Security</th></tr>
      <tr><td>DAY</td><td>Dayforce</td></tr>
      <tr><td>CCC</td><td>Current Co</td></tr>
    </table>
    <table>
      <tr><th rowspan="2">Effective Date</th><th colspan="2">Added</th><th colspan="2">Removed</th></tr>
      <tr><th>Ticker</th><th>Security</th><th>Ticker</th><th>Security</th></tr>
      <tr><td>January 3, 2022</td><td>CCC</td><td>Current Co</td><td>BBB</td><td>Before Co</td></tr>
    </table>
    """
    mapping_path = tmp_path / "symbols.csv"
    mapping_path.write_text(
        "source_ticker,canonical_ticker,effective_start,effective_end,reason\n"
        "DAY,CDAY,2021-01-01,2024-01-31,Official timeline\n"
        "DAY,DAY,2024-02-01,2025-12-31,Official timeline\n",
        encoding="utf-8",
    )

    export = _normalize(
        raw,
        _PINNED_URL,
        date(2021, 1, 1),
        date(2025, 12, 31),
        mappings=load_symbol_map(mapping_path),
    )

    assert export.company_names["CDAY"] == "Dayforce"
    assert export.company_names["DAY"] == "Dayforce"


def test_fetch_membership_defaults_to_tracked_reviewed_map(monkeypatch) -> None:
    sentinel = object()
    observed: list[Path | None] = []
    monkeypatch.setattr(membership, "fetch_revision", lambda _url: b"raw")
    monkeypatch.setattr(
        membership,
        "load_symbol_map",
        lambda path=None: observed.append(path) or {},
    )
    monkeypatch.setattr(membership, "_normalize", lambda *_args, **_kwargs: sentinel)

    result = membership.fetch_membership(
        _PINNED_URL,
        date(2021, 1, 1),
        date(2025, 12, 31),
    )

    assert result is sentinel
    assert observed == [None]
