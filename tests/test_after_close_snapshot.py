"""Completed-bar technical snapshot behavior tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from core.after_close_snapshot import build_after_close_snapshot, write_after_close_snapshot
from core.canslim import MarketTrend


def _market(*, is_bullish: bool = True) -> MarketTrend:
    return MarketTrend(
        symbol="SPY",
        score=0.8 if is_bullish else 0.2,
        is_bullish=is_bullish,
        latest_close=102.0,
        indicators={},
    )


def _history(
    *,
    length: int = 260,
    anchors: tuple[float, float, float, float] = (10.0, 30.0, 50.0, 80.0),
    pivot: float = 100.0,
    previous_close: float = 100.0,
    latest_close: float = 102.0,
    latest_volume: float = 1_400.0,
    end: str = "2025-12-31",
) -> pd.DataFrame:
    """Create a literal, deterministic daily OHLCV fixture with 260 sessions."""
    dates = pd.date_range(end=end, periods=length, freq="B")
    closes = [anchors[0]] * length
    if length >= 260:
        closes[65] = anchors[1]
        closes[130] = anchors[2]
        closes[195] = anchors[3]
        closes[-3] = pivot
    closes[-2] = previous_close
    closes[-1] = latest_close
    highs = [value * 1.002 for value in closes]
    lows = [value * 0.998 for value in closes]
    volumes = [1_000.0] * length
    volumes[-1] = latest_volume
    return pd.DataFrame(
        {"Open": closes, "High": highs, "Low": lows, "Close": closes, "Volume": volumes}, index=dates
    )


def _rows(snapshot: object) -> dict[str, dict[str, object]]:
    return {row["symbol"]: row for row in snapshot.rows}  # type: ignore[attr-defined]


def test_snapshot_ranks_rs_without_turning_it_into_a_full_entry_gate() -> None:
    """After-close keeps RS ranking metadata but remains technical-only."""
    bullish_spy = _history(anchors=(50.0, 55.0, 60.0, 70.0))
    qualifying_leader = _history()
    low_rs = _history(anchors=(100.0, 100.0, 100.0, 100.0))

    snapshot = build_after_close_snapshot(
        {"SPY": bullish_spy, "LEAD": qualifying_leader, "LOW": low_rs},
        market=_market(),
        expected_symbols=["LEAD", "LOW", "MISSING"],
    )
    rows = _rows(snapshot)

    assert rows["LEAD"]["technical_eligible"] is True
    assert rows["LOW"]["technical_eligible"] is True
    assert rows["LOW"]["blocking_reasons"] == ""
    assert rows["LOW"]["rs_score"] < rows["LEAD"]["rs_score"]
    assert rows["MISSING"]["blocking_reasons"] == "missing_price_history"


def test_snapshot_catches_stale_session_break() -> None:
    """A regression that scores data from a non-SPY session must fail here."""
    spy = _history()
    stale = _history(end="2025-12-30")

    snapshot = build_after_close_snapshot({"SPY": spy, "STALE": stale}, market=_market(), expected_symbols=["STALE"])

    assert _rows(snapshot)["STALE"]["blocking_reasons"] == "stale_price_history"


def test_snapshot_uses_one_canonical_view_for_unsorted_history() -> None:
    """Break caught: the session label was canonical but snapshot facts used raw order."""
    spy = _history(anchors=(50.0, 55.0, 60.0, 70.0))
    leader = _history()
    expected = _rows(
        build_after_close_snapshot(
            {"SPY": spy, "LEAD": leader},
            market=_market(),
            expected_symbols=["LEAD"],
        )
    )["LEAD"]

    actual = _rows(
        build_after_close_snapshot(
            {"SPY": spy.iloc[::-1], "LEAD": leader.iloc[::-1]},
            market=_market(),
            expected_symbols=["LEAD"],
        )
    )["LEAD"]

    for field in (
        "technical_eligible",
        "blocking_reasons",
        "close",
        "prior_close",
        "pivot",
        "volume_ratio_50d",
        "average_dollar_volume_50d",
        "atr_pct_20d",
        "realized_volatility_20d",
        "weighted_performance",
        "rs_score",
    ):
        if isinstance(expected[field], float):
            assert actual[field] == pytest.approx(expected[field])
        else:
            assert actual[field] == expected[field]


def test_snapshot_catches_no_up_day_volume_surge_break() -> None:
    """A regression that accepts below-threshold volume must fail here."""
    spy = _history(anchors=(50.0, 55.0, 60.0, 70.0))
    leader = _history()
    quiet = _history(anchors=(5.0, 35.0, 60.0, 85.0), latest_volume=1_299.0)

    snapshot = build_after_close_snapshot(
        {"SPY": spy, "LEAD": leader, "QUIET": quiet}, market=_market(), expected_symbols=["QUIET"]
    )

    assert _rows(snapshot)["QUIET"]["blocking_reasons"] == "volume_ratio_below_threshold"


def test_snapshot_volume_gate_uses_the_prior_fifty_session_baseline() -> None:
    """The event bar must not dilute its own fixed prior-50 volume baseline."""
    spy = _history(anchors=(50.0, 55.0, 60.0, 70.0))
    boundary = _history(latest_volume=1_300.0)

    snapshot = build_after_close_snapshot(
        {"SPY": spy, "BOUNDARY": boundary}, market=_market(), expected_symbols=["BOUNDARY"]
    )
    row = _rows(snapshot)["BOUNDARY"]

    assert row["volume_ratio_50d"] == pytest.approx(1.3)
    assert row["average_dollar_volume_50d"] == pytest.approx(13_600.0)
    assert row["technical_eligible"] is True
    assert row["blocking_reasons"] == ""


def test_snapshot_keeps_rs_as_ranking_metadata_when_volume_blocks() -> None:
    """A weak RS rank cannot masquerade as a full-CANSLIM after-close gate."""
    spy = _history(anchors=(50.0, 55.0, 60.0, 70.0))
    leader = _history()
    low_and_quiet = _history(anchors=(100.0, 100.0, 100.0, 100.0), latest_volume=1_200.0)

    snapshot = build_after_close_snapshot(
        {"SPY": spy, "LEAD": leader, "LOW": low_and_quiet},
        market=_market(),
        expected_symbols=["LEAD", "LOW"],
    )
    row = _rows(snapshot)["LOW"]

    assert row["blocking_reasons"] == "volume_ratio_below_threshold"
    assert row["rs_score"] < _rows(snapshot)["LEAD"]["rs_score"]
    assert row["normalized_trigger_gap"] == pytest.approx((1.3 - 1.2) / 1.3)


def test_snapshot_catches_below_pivot_break() -> None:
    """A regression that permits entries below the prior-window pivot must fail here."""
    spy = _history(anchors=(50.0, 55.0, 60.0, 70.0))
    leader = _history()
    below = _history(anchors=(5.0, 35.0, 60.0, 85.0), previous_close=98.0, latest_close=99.0)

    snapshot = build_after_close_snapshot(
        {"SPY": spy, "LEAD": leader, "BELOW": below}, market=_market(), expected_symbols=["BELOW"]
    )

    assert _rows(snapshot)["BELOW"]["blocking_reasons"] == "close_below_pivot"


def test_snapshot_catches_beyond_buy_zone_break() -> None:
    """A regression that chases prices more than 5% above pivot must fail here."""
    spy = _history(anchors=(50.0, 55.0, 60.0, 70.0))
    leader = _history()
    extended = _history(anchors=(5.0, 35.0, 60.0, 85.0), latest_close=106.0)

    snapshot = build_after_close_snapshot(
        {"SPY": spy, "LEAD": leader, "EXTENDED": extended}, market=_market(), expected_symbols=["EXTENDED"]
    )

    assert _rows(snapshot)["EXTENDED"]["blocking_reasons"] == "close_above_buy_zone"


def test_snapshot_uses_prior_close_max_when_latest_high_is_intraday_spike() -> None:
    """A regression that lets the latest intraday high lower breakout proximity must fail here."""
    spy = _history(anchors=(50.0, 55.0, 60.0, 70.0))
    leader = _history()
    leader.loc[leader.index[-1], "High"] = 120.0

    snapshot = build_after_close_snapshot({"SPY": spy, "LEAD": leader}, market=_market(), expected_symbols=["LEAD"])
    row = _rows(snapshot)["LEAD"]

    assert row["pivot"] == 100.0
    assert row["proximity_to_52week_high"] == 1.02
    assert row["technical_eligible"] is True


def test_snapshot_normalizes_extended_gap_by_buy_zone_extension_threshold() -> None:
    """A regression that divides extension excess by price instead of the 5% threshold must fail here."""
    spy = _history(anchors=(50.0, 55.0, 60.0, 70.0))
    leader = _history()
    extended = _history(anchors=(5.0, 35.0, 60.0, 85.0), latest_close=106.0)

    snapshot = build_after_close_snapshot(
        {"SPY": spy, "LEAD": leader, "EXTENDED": extended}, market=_market(), expected_symbols=["EXTENDED"]
    )

    assert _rows(snapshot)["EXTENDED"]["normalized_trigger_gap"] == pytest.approx(0.2)


def test_snapshot_ignores_legacy_buy_zone_undercut_tolerance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy settings cannot loosen the fixed canonical pivot lower bound."""
    monkeypatch.setattr("core.after_close_snapshot.settings.BUY_ZONE_UNDERCUT_TOLERANCE_PCT", 0.02)
    spy = _history(anchors=(50.0, 55.0, 60.0, 70.0))
    edge = _history(previous_close=97.0, latest_close=98.0)
    below = _history(previous_close=96.0, latest_close=97.0)

    edge_snapshot = build_after_close_snapshot(
        {"SPY": spy, "EDGE": edge}, market=_market(), expected_symbols=["EDGE"]
    )
    below_snapshot = build_after_close_snapshot(
        {"SPY": spy, "BELOW": below}, market=_market(), expected_symbols=["BELOW"]
    )

    assert _rows(edge_snapshot)["EDGE"]["technical_eligible"] is False
    assert _rows(edge_snapshot)["EDGE"]["blocking_reasons"] == "close_below_pivot"
    assert _rows(below_snapshot)["BELOW"]["blocking_reasons"] == "close_below_pivot"
    assert _rows(below_snapshot)["BELOW"]["normalized_trigger_gap"] == pytest.approx(0.03)


def test_snapshot_catches_under_thirty_bar_history_break() -> None:
    """A regression that treats fewer than 30 completed bars as usable must fail here."""
    snapshot = build_after_close_snapshot(
        {"SPY": _history(), "SHORT": _history(length=29)}, market=_market(), expected_symbols=["SHORT"]
    )

    assert _rows(snapshot)["SHORT"]["blocking_reasons"] == "insufficient_price_history"


def test_snapshot_warns_without_blocking_for_thirty_to_251_bars() -> None:
    """A regression that makes 30-to-251 sessions a history blocker must fail here."""
    snapshot = build_after_close_snapshot(
        {"SPY": _history(), "YOUNG": _history(length=30)}, market=_market(), expected_symbols=["YOUNG"]
    )
    row = _rows(snapshot)["YOUNG"]

    assert row["blocking_reasons"] != "insufficient_price_history"
    assert row["warnings"] == "limited_price_history"


def test_snapshot_excludes_spy_from_candidate_percentile() -> None:
    """A regression that ranks the market benchmark as a candidate must fail here."""
    spy = _history()
    only_candidate = _history(anchors=(100.0, 100.0, 100.0, 100.0))

    snapshot = build_after_close_snapshot(
        {"SPY": spy, "ONLY": only_candidate}, market=_market(), expected_symbols=["ONLY"]
    )

    assert _rows(snapshot)["ONLY"]["rs_score"] == 99.0


def test_snapshot_excludes_unexpected_symbols_from_candidate_percentile() -> None:
    """A regression that lets downloaded non-universe symbols dilute expected-symbol RS must fail here."""
    spy = _history(anchors=(100.0, 100.0, 100.0, 100.0))
    candidate = _history(anchors=(50.0, 55.0, 60.0, 70.0))
    unexpected_leader = _history()

    baseline = build_after_close_snapshot(
        {"SPY": spy, "ONLY": candidate}, market=_market(), expected_symbols=["ONLY"]
    )
    with_extra = build_after_close_snapshot(
        {"SPY": spy, "ONLY": candidate, "EXTRA": unexpected_leader},
        market=_market(),
        expected_symbols=["ONLY"],
    )

    assert _rows(baseline)["ONLY"]["rs_score"] == 99.0
    assert _rows(with_extra)["ONLY"]["rs_score"] == 99.0


def test_snapshot_annualizes_clean_short_history_like_live_rs() -> None:
    """A regression that uses raw short-history return instead of the live annualized fallback must fail here."""
    young = _history(length=100, anchors=(100.0, 100.0, 100.0, 100.0), previous_close=100.0, latest_close=110.0)

    snapshot = build_after_close_snapshot(
        {"SPY": _history(), "YOUNG": young}, market=_market(), expected_symbols=["YOUNG"]
    )
    row = _rows(snapshot)["YOUNG"]

    assert row["weighted_performance"] == pytest.approx(1.1**2.52 - 1.0)
    assert row["rs_score"] == 99.0


def test_snapshot_preserves_technical_eligibility_in_bearish_market() -> None:
    """A regression that erases technical eligibility in a bearish regime must fail here."""
    snapshot = build_after_close_snapshot(
        {"SPY": _history(anchors=(50.0, 55.0, 60.0, 70.0)), "LEAD": _history()},
        market=_market(is_bullish=False),
        expected_symbols=["LEAD"],
    )
    row = _rows(snapshot)["LEAD"]

    assert row["technical_eligible"] is True
    assert row["tomorrow_executable"] is False
    assert row["execution_blocking_reasons"] == "market_not_bullish"


def test_snapshot_ranking_is_deterministic() -> None:
    """A regression that leaves equal candidates in input order must fail here."""
    spy = _history(anchors=(50.0, 55.0, 60.0, 70.0))
    candidate = _history()

    snapshot = build_after_close_snapshot(
        {"SPY": spy, "ZED": candidate, "ALP": candidate}, market=_market(), expected_symbols=["ZED", "ALP"]
    )

    assert [row["symbol"] for row in snapshot.rows] == ["ALP", "ZED"]


def test_writer_creates_strict_json_and_csv_for_empty_snapshot(tmp_path: Path) -> None:
    """A regression that writes NaN JSON or skips empty artifacts must fail here."""
    snapshot = build_after_close_snapshot({"SPY": _history()}, market=_market(), expected_symbols=["MISSING"])

    csv_path, json_path = write_after_close_snapshot(
        snapshot, tmp_path, generated_at=datetime(2026, 8, 17, tzinfo=timezone.utc)
    )

    assert csv_path.exists()
    assert json_path.exists()
    assert "execution_blocking_reasons" in pd.read_csv(csv_path).columns
    text = json_path.read_text(encoding="utf-8")
    assert "NaN" not in text
    assert "Infinity" not in text
    assert json.loads(text)["rows"][0]["close"] is None
    assert json.loads(text)["rows"][0]["execution_blocking_reasons"] == ""
