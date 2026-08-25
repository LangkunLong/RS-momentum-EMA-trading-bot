from pathlib import Path

import pytest

from core.pit_diagnosis.models import RuleOutcome
from core.pit_diagnosis.rulebook import canonical_sha256, evaluate_fidelity, load_canonical_json, load_rulebook


RULEBOOK = Path("config/pit_canslim_rulebook_v1.json")


def test_rulebook_v1_has_exact_required_domains_and_citations() -> None:
    book = load_rulebook(RULEBOOK)
    assert book.version == "pit-canslim-v1"
    assert set(book.rules) == {
        "C.EPS_YOY", "C.SALES_YOY", "C.ACCELERATION", "A.EPS_MULTIYEAR", "A.ROE",
        "N.NEWNESS", "N.CATALYST", "N.NEW_HIGH", "S.VOLUME_CONFIRMATION", "S.SUPPLY",
        "L.RS", "L.INDUSTRY_GROUP", "I.SPONSORSHIP", "M.CONFIRMED_UPTREND",
        "M.DISTRIBUTION_EXPOSURE", "E.PROPER_BASE", "E.PIVOT", "E.BUY_ZONE", "E.NEXT_OPEN",
        "X.LOSS_LIMIT", "X.PROFIT_ZONE", "X.EIGHT_WEEK_HOLD", "X.STRUCTURAL_SELL",
    }
    assert all(rule.source_id in book.sources for rule in book.rules.values())
    assert book.rules["N.NEWNESS"].satisfaction_logic == "one_of:N.CATALYST,N.NEW_HIGH"


def test_new_high_from_proper_base_can_satisfy_n_without_qualitative_catalyst() -> None:
    book = load_rulebook(RULEBOOK)
    outcomes = {rule_id: RuleOutcome.passed(rule_id) for rule_id in book.rules}
    outcomes["N.CATALYST"] = RuleOutcome.unavailable("N.CATALYST")
    outcomes["N.NEW_HIGH"] = RuleOutcome.passed("N.NEW_HIGH")
    assessment = evaluate_fidelity(book, outcomes)
    assert "N.NEWNESS" not in assessment.failed_required_rule_ids


def test_missing_evidence_is_not_a_pass_and_unknown_ids_are_rejected() -> None:
    book = load_rulebook(RULEBOOK)
    with pytest.raises(ValueError, match="missing"):
        evaluate_fidelity(book, {})
    with pytest.raises(ValueError, match="unknown"):
        evaluate_fidelity(book, {"UNKNOWN": RuleOutcome.passed("UNKNOWN")})


def test_canonical_json_rejects_duplicate_keys_and_hash_is_stable(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"a": 1, "a": 2}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_canonical_json(duplicate)
    assert canonical_sha256({"b": 2, "a": 1}) == canonical_sha256({"a": 1, "b": 2})


def test_proxy_must_be_declared() -> None:
    book = load_rulebook(RULEBOOK)
    with pytest.raises(ValueError, match="proxy"):
        evaluate_fidelity(book, {}, approved_proxy_rule_ids=frozenset({"C.EPS_YOY"}))
