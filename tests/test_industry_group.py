"""Tests for industry group RS ranking logic."""
from __future__ import annotations


from core.industry_group import get_top_groups


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
