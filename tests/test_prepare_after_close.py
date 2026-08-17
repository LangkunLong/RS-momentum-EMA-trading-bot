"""Read-only after-close preparation command tests."""

from __future__ import annotations

import importlib
import json
import sys
from datetime import date
from types import SimpleNamespace

import pandas as pd


def _history(*, end: str = "2026-08-17", length: int = 260) -> pd.DataFrame:
    dates = pd.date_range(end=end, periods=length, freq="B")
    closes = [100.0] * length
    closes[-3:] = [100.0, 100.0, 102.0]
    volumes = [1_000.0] * length
    volumes[-1] = 1_300.0
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [value * 1.002 for value in closes],
            "Low": [value * 0.998 for value in closes],
            "Close": closes,
            "Volume": volumes,
        },
        index=dates,
    )


def _price_data() -> dict[str, pd.DataFrame]:
    return {"SPY": _history(), "LEAD": _history(), "MISS": _history()}


def test_prepare_writes_durable_snapshot_from_one_bulk_download(tmp_path, monkeypatch) -> None:
    """A regression that splits the bulk boundary or omits artifacts must fail here."""
    import prepare_after_close as command

    calls: list[tuple[list[str], str, int]] = []

    def fake_bulk(symbols: list[str], *, period: str, chunk_size: int) -> dict[str, pd.DataFrame]:
        calls.append((symbols, period, chunk_size))
        return _price_data()

    monkeypatch.setattr(command, "fetch_bulk_ohlcv", fake_bulk)
    csv_path, json_path = command.prepare_after_close(
        custom_symbols=["LEAD", "MISS", "LEAD"], as_of=date(2026, 8, 17), output_dir=tmp_path
    )

    assert csv_path.exists()
    assert csv_path.name.startswith("after_close_snapshot_2026-08-17_")
    assert json_path.name.startswith("after_close_snapshot_2026-08-17_")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["as_of_session"] == "2026-08-17"
    assert payload["summary"]["configured_symbols"] == 2
    assert calls == [(["LEAD", "MISS", "SPY"], command.settings.RS_CALCULATION_PERIOD, 100)]


def test_cli_rejects_mismatched_as_of_without_artifacts(tmp_path, monkeypatch) -> None:
    """A requested session mismatch must not leave misleading snapshot files."""
    import prepare_after_close as command

    monkeypatch.setattr(command, "fetch_bulk_ohlcv", lambda *_args, **_kwargs: _price_data())

    result = command.main(
        ["--symbols", "LEAD", "--as-of", "2026-08-14", "--output-dir", str(tmp_path)]
    )

    assert result != 0
    assert list(tmp_path.iterdir()) == []


def test_prepare_writes_empty_shortlist_as_strict_json(tmp_path, monkeypatch) -> None:
    """An advisory run with no executable names must still be auditable."""
    import prepare_after_close as command

    bearish = _price_data()
    monkeypatch.setattr(command, "fetch_bulk_ohlcv", lambda *_args, **_kwargs: bearish)
    monkeypatch.setattr(
        command,
        "evaluate_m",
        lambda *, price_data: SimpleNamespace(
            symbol="SPY", score=0.0, is_bullish=False, latest_close=102.0, indicators={}, distribution_days=0,
            follow_through=False,
        ),
    )
    _csv_path, json_path = command.prepare_after_close(custom_symbols=["LEAD"], output_dir=tmp_path)

    text = json_path.read_text(encoding="utf-8")
    assert "NaN" not in text
    assert "Infinity" not in text
    assert json.loads(text)["shortlist"] == []


def test_parser_accepts_symbols() -> None:
    """The command must expose explicit custom-symbol preparation."""
    import prepare_after_close as command

    args = command.build_parser().parse_args(["--symbols", "AAPL", "MSFT"])

    assert args.symbols == ["AAPL", "MSFT"]


def test_importing_command_does_not_import_fmp_or_broker_modules() -> None:
    """The advisory CLI module must not pull provider or execution boundaries at import time."""
    sys.modules.pop("prepare_after_close", None)
    before = {name for name in sys.modules if "fmp" in name.lower() or name.startswith("alpaca.trading")}
    module = importlib.import_module("prepare_after_close")
    after = {name for name in sys.modules if "fmp" in name.lower() or name.startswith("alpaca.trading")}

    assert module is not None
    assert after == before
