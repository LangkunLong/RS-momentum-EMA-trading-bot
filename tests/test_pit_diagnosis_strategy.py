from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

import pandas as pd

from core.pit_diagnosis.catalog import load_experiment_catalog
from core.pit_diagnosis.fact_cache import SessionFact
from core.pit_diagnosis.rulebook import load_rulebook
from core.pit_diagnosis.strategy import CachedDiagnosisStrategy, evaluate_session_rules


RULEBOOK = load_rulebook(Path("config/pit_canslim_rulebook_v1.json"))
CATALOG = load_experiment_catalog(Path("config/pit_diagnosis_experiments_v1.json"), RULEBOOK)
EXPERIMENT = CATALOG["D2.RULE_STAGE_FUNNEL"]


class _Facts:
    def __init__(self, fact: SessionFact) -> None:
        self.fact = fact

    def session_fact(self, _symbol: str, _session: str) -> SessionFact:
        return self.fact


def _fact(**updates: object) -> SessionFact:
    values: dict[str, object] = {
        "symbol": "AAA",
        "session": "2024-01-02",
        "close": 103.0,
        "open": 103.0,
        "current_eps_yoy": 0.30,
        "sales_yoy": 0.30,
        "annual_eps_1": 4.0,
        "annual_eps_2": 3.0,
        "annual_eps_3": 2.0,
        "annual_eps_4": 1.0,
        "roe": 0.20,
        "base_kind": "flat",
        "pivot": 100.0,
        "extension_pct": 0.03,
        "event_volume_ratio": 1.5,
        "rs_rating": 90.0,
        "market_regime": "uptrend",
        "distribution_count": 0,
        "institutional_evidence_ids": "[]",
        "industry_evidence_ids": "[]",
    }
    values.update(updates)
    return SessionFact(MappingProxyType(values))


def _outcomes(fact: SessionFact, *, strict: bool = False) -> dict[str, object]:
    return {
        outcome.rule_id: outcome
        for outcome in evaluate_session_rules(fact, RULEBOOK, EXPERIMENT, strict_canslim=strict)
    }


def test_strict_industry_group_requires_evidence_and_top_rank() -> None:
    evidence = {"industry_rank": 20, "industry_evidence_ids": '["pit:industry:AAA:2024-01-02"]'}
    assert _outcomes(_fact(**evidence), strict=True)["L.INDUSTRY_GROUP"].status == "passed"
    assert _outcomes(_fact(industry_rank=21, industry_evidence_ids=evidence["industry_evidence_ids"]), strict=True)["L.INDUSTRY_GROUP"].status == "failed"
    assert _outcomes(_fact(industry_rank=1), strict=True)["L.INDUSTRY_GROUP"].status == "unavailable"


def test_strict_sponsorship_requires_increasing_holders_and_ownership_floor() -> None:
    evidence = {"institutional_ownership_percent": 0.10, "institutional_holder_count": 11, "institutional_previous_holder_count": 10, "institutional_evidence_ids": '["pit:institutional:AAA:2024-01-02"]'}
    assert _outcomes(_fact(**evidence), strict=True)["I.SPONSORSHIP"].status == "passed"
    assert _outcomes(_fact(institutional_ownership_percent=0.099, **{key: value for key, value in evidence.items() if key != "institutional_ownership_percent"}), strict=True)["I.SPONSORSHIP"].status == "failed"
    assert _outcomes(_fact(institutional_holder_count=10, **{key: value for key, value in evidence.items() if key != "institutional_holder_count"}), strict=True)["I.SPONSORSHIP"].status == "failed"
    assert _outcomes(_fact(institutional_ownership_percent=0.30, institutional_holder_count=11, institutional_previous_holder_count=10), strict=True)["I.SPONSORSHIP"].status == "unavailable"


def test_proxy_mode_ignores_unavailable_i_and_l_but_strict_mode_blocks() -> None:
    fact = _fact()
    proxy_signal = CachedDiagnosisStrategy(_Facts(fact), RULEBOOK, EXPERIMENT).evaluate_symbol(
        ticker="AAA", ticker_ohlcv={}, all_closes=None, eval_date=pd.Timestamp("2024-01-02"), market_state={}
    )
    strict_signal = CachedDiagnosisStrategy(_Facts(fact), RULEBOOK, EXPERIMENT, strict_canslim=True).evaluate_symbol(
        ticker="AAA", ticker_ohlcv={}, all_closes=None, eval_date=pd.Timestamp("2024-01-02"), market_state={}
    )
    assert proxy_signal is not None and proxy_signal["entry_contract_eligible"] is True
    assert strict_signal is not None and strict_signal["entry_contract_eligible"] is False


def test_newness_parent_is_derived_from_child_outcomes() -> None:
    unavailable = _outcomes(_fact(base_kind=None), strict=True)
    failed_child = _outcomes(_fact(extension_pct=-0.01), strict=True)
    passed_child = _outcomes(_fact(extension_pct=0.0), strict=True)

    assert unavailable["N.NEW_HIGH"].status == "unavailable"
    assert unavailable["N.NEWNESS"].status == "unavailable"
    assert failed_child["N.NEW_HIGH"].status == "failed"
    assert failed_child["N.NEWNESS"].status == "unavailable"
    assert passed_child["N.NEW_HIGH"].status == "passed"
    assert passed_child["N.NEWNESS"].status == "passed"
