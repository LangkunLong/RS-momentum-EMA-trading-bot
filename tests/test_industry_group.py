"""Tests for industry group RS ranking logic."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from config import settings
from core.industry_group import get_top_groups, load_industry_map


def test_top_groups_returns_top_n() -> None:
    """Returns exactly top N groups by average RS score."""
    rs = {
        "NVDA": 95.0, "AMD": 88.0, "AVGO": 91.0,  # Semiconductors avg=91.3
        "MSFT": 82.0, "CRM": 79.0, "NOW": 85.0,   # Software avg=82.0
        "JPM": 71.0, "BAC": 65.0, "GS": 68.0,      # Banks avg=68.0
        "XOM": 60.0, "CVX": 58.0, "COP": 62.0,     # Energy avg=60.0
        "UNH": 55.0, "CVS": 52.0, "CI": 57.0,      # Healthcare avg=54.7
    }
    industry = {
        "NVDA": "Semiconductors", "AMD": "Semiconductors", "AVGO": "Semiconductors",
        "MSFT": "Software", "CRM": "Software", "NOW": "Software",
        "JPM": "Banks", "BAC": "Banks", "GS": "Banks",
        "XOM": "Energy", "CVX": "Energy", "COP": "Energy",
        "UNH": "Healthcare", "CVS": "Healthcare", "CI": "Healthcare",
    }
    result = get_top_groups(rs, industry, top_n=2, min_size=3)
    assert result == {"Semiconductors", "Software"}


def test_min_size_excludes_small_groups() -> None:
    """Groups with fewer than min_size members are excluded from ranking."""
    rs = {
        "NVDA": 99.0, "AMD": 98.0,                  # Semiconductors: 2 members
        "MSFT": 82.0, "CRM": 79.0, "NOW": 85.0,    # Software: 3 members
        "JPM": 71.0, "BAC": 65.0, "GS": 68.0,       # Banks: 3 members
    }
    industry = {
        "NVDA": "Semiconductors", "AMD": "Semiconductors",
        "MSFT": "Software", "CRM": "Software", "NOW": "Software",
        "JPM": "Banks", "BAC": "Banks", "GS": "Banks",
    }
    # Semiconductors has highest RS (98.5 avg) but only 2 members < min_size=3
    result = get_top_groups(rs, industry, top_n=1, min_size=3)
    assert "Semiconductors" not in result
    assert "Software" in result


def test_group_rs_is_average_of_member_scores() -> None:
    """Group RS equals the arithmetic mean of its members' RS scores."""
    rs = {"A": 80.0, "B": 90.0, "C": 100.0}
    industry = {"A": "Tech", "B": "Tech", "C": "Tech"}
    result = get_top_groups(rs, industry, top_n=1, min_size=3)
    assert "Tech" in result  # avg = 90.0, only group → top 1


def test_missing_industry_ticker_is_ignored() -> None:
    """Tickers absent from ticker_industry map are simply ignored in group computation."""
    rs = {
        "NVDA": 95.0, "AMD": 88.0, "AVGO": 91.0,  # Semiconductors avg=91.3
        "UNKNOWN": 99.0,                             # no industry label — ignored
        "JPM": 71.0, "BAC": 65.0, "GS": 68.0,      # Banks avg=68.0
    }
    industry = {
        "NVDA": "Semiconductors", "AMD": "Semiconductors", "AVGO": "Semiconductors",
        "JPM": "Banks", "BAC": "Banks", "GS": "Banks",
        # UNKNOWN intentionally omitted
    }
    result = get_top_groups(rs, industry, top_n=1, min_size=3)
    assert "Semiconductors" in result
    assert "Banks" not in result


def test_ticker_in_top_group_not_blocked() -> None:
    """A ticker whose group is in top_groups passes the gate (not in the filtered-out set)."""
    rs = {
        "NVDA": 95.0, "AMD": 88.0, "AVGO": 91.0,
        "JPM": 71.0, "BAC": 65.0, "GS": 68.0,
    }
    industry = {
        "NVDA": "Semiconductors", "AMD": "Semiconductors", "AVGO": "Semiconductors",
        "JPM": "Banks", "BAC": "Banks", "GS": "Banks",
    }
    top_groups = get_top_groups(rs, industry, top_n=1, min_size=3)
    # NVDA's group should be in top_groups → gate passes
    ticker_group = industry.get("NVDA")
    assert ticker_group in top_groups


def test_ticker_not_in_top_group_is_blocked() -> None:
    """A ticker whose group is outside top_groups is blocked by the gate."""
    rs = {
        "NVDA": 95.0, "AMD": 88.0, "AVGO": 91.0,
        "JPM": 71.0, "BAC": 65.0, "GS": 68.0,
    }
    industry = {
        "NVDA": "Semiconductors", "AMD": "Semiconductors", "AVGO": "Semiconductors",
        "JPM": "Banks", "BAC": "Banks", "GS": "Banks",
    }
    top_groups = get_top_groups(rs, industry, top_n=1, min_size=3)
    # JPM's group (Banks) should NOT be in top_groups → gate blocks
    ticker_group = industry.get("JPM")
    assert ticker_group not in top_groups


def test_fmp_company_profile_normalizes_industry_and_sector() -> None:
    """The FMP adapter must return only non-empty normalized labels."""
    from core.fmp_provider import fetch_company_profile

    def fake_fmp_get(endpoint: str, params: dict) -> list[dict]:
        assert endpoint == "profile"
        assert params == {"symbol": "NVDA"}
        return [{"industry": " Semiconductors ", "sector": "Technology", "companyName": "NVIDIA"}]

    assert fetch_company_profile("NVDA", fake_fmp_get) == {
        "industry": "Semiconductors",
        "sector": "Technology",
    }


@pytest.mark.parametrize("payload", [[], {}, [None], [{"industry": " ", "sector": ""}]])
def test_fmp_company_profile_rejects_empty_or_malformed_payloads(payload: object) -> None:
    """Malformed provider payloads must degrade to an empty profile."""
    from core.fmp_provider import fetch_company_profile

    assert fetch_company_profile("EMPTY", lambda *_args, **_kwargs: payload) == {}


def test_load_industry_map_uses_fmp_and_reuses_disk_cache(tmp_path) -> None:
    """Industry labels use FMP industry-first fallback and a seven-day cache."""
    cache_path = tmp_path / "industry_map.json"
    profiles = {
        "NVDA": {"industry": "Semiconductors", "sector": "Technology"},
        "BRK.B": {"sector": "Financial Services"},
        "EMPTY": {},
    }

    with (
        patch("core.industry_group.settings.INDUSTRY_GROUP_CACHE_PATH", str(cache_path)),
        patch("core.industry_group.settings.FMP_PLAN", "paid", create=True),
        patch("core.industry_group.fetch_company_profile", side_effect=lambda symbol: profiles[symbol]) as fetch,
    ):
        first = load_industry_map(["NVDA", "BRK.B", "EMPTY"])
        second = load_industry_map(["NVDA", "BRK.B"])

    assert first == {"NVDA": "Semiconductors", "BRK.B": "Financial Services"}
    assert second == first
    assert fetch.call_count == 3


def test_free_plan_industry_map_uses_cache_without_profile_calls(tmp_path) -> None:
    """Free mode must not spend quota on company profiles for industry labels."""
    cache_path = tmp_path / "industry_map.json"

    with (
        patch("core.industry_group.settings.INDUSTRY_GROUP_CACHE_PATH", str(cache_path)),
        patch("core.industry_group.settings.FMP_PLAN", "free", create=True),
        patch("core.industry_group.fetch_company_profile") as fetch,
    ):
        result = load_industry_map(["NVDA", "AAPL"])

    assert result == {}
    fetch.assert_not_called()


@pytest.mark.integration
def test_paid_fmp_profile_smoke_returns_nonblank_label(tmp_path, monkeypatch) -> None:
    """An explicitly authorized paid-plan smoke resolves one uncached industry label."""
    if os.environ.get("RUN_FMP_PROFILE_INTEGRATION") != "1":
        pytest.skip("set RUN_FMP_PROFILE_INTEGRATION=1 to authorize one paid FMP profile call")
    if settings.FMP_PLAN != "paid":
        pytest.skip("the live FMP profile smoke requires FMP_PLAN=paid")
    if not os.environ.get("FMP_API_KEY", "").strip():
        pytest.skip("the live FMP profile smoke requires FMP_API_KEY")

    monkeypatch.setattr(settings, "INDUSTRY_GROUP_CACHE_PATH", str(tmp_path / "industry-map.json"))

    result = load_industry_map(["NVDA"])

    assert isinstance(result.get("NVDA"), str)
    assert result["NVDA"].strip()
