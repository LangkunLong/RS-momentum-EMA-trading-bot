import hashlib
from datetime import date
from pathlib import Path

import fetch_sp500_membership as command
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

    assert command._symbol_map_provenance(mapping_path, mappings) == {
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
