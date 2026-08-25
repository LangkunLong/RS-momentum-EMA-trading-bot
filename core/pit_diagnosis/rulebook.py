"""Canonical JSON loading and fail-closed fidelity evaluation."""

import hashlib
import json
from pathlib import Path
from typing import Mapping

from .models import (
    FidelityAssessment,
    FidelityLabel,
    ImplementationStatus,
    Observability,
    Rulebook,
    RuleClassification,
    RuleOutcome,
    RuleRecord,
    RuleSource,
)


def _reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_canonical_json(path: Path) -> Mapping[str, object]:
    with Path(path).open("r", encoding="utf-8") as handle:
        value = json.load(handle, object_pairs_hook=_reject_duplicates, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(f"non-finite JSON value: {x}")))
    if not isinstance(value, dict):
        raise ValueError("canonical JSON root must be an object")
    return value


def canonical_sha256(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_rulebook(path: Path) -> Rulebook:
    raw = load_canonical_json(path)
    sources_raw = raw.get("sources")
    rules_raw = raw.get("rules")
    if not isinstance(sources_raw, dict) or not isinstance(rules_raw, dict):
        raise ValueError("rulebook requires object sources and rules")
    sources = {
        key: RuleSource(source_id=key, title=value["title"], url=value["url"], source_location=value["source_location"])
        for key, value in sources_raw.items()
    }
    rules = {
        key: RuleRecord(
            rule_id=key,
            letter_or_domain=value["letter_or_domain"],
            requirement=value["requirement"],
            classification=RuleClassification(value["classification"]),
            observability=Observability(value["observability"]),
            parameter_policy=value.get("parameter_policy", {}),
            source_id=value["source_id"],
            source_location=value["source_location"],
            implementation_status=ImplementationStatus(value["implementation_status"]),
            satisfaction_logic=value.get("satisfaction_logic", "all"),
        )
        for key, value in rules_raw.items()
    }
    return Rulebook(version=raw["version"], sources=sources, rules=rules, sha256=canonical_sha256(raw))


def evaluate_fidelity(
    rulebook: Rulebook,
    outcomes: Mapping[str, RuleOutcome],
    *,
    approved_proxy_rule_ids: frozenset[str] = frozenset(),
) -> FidelityAssessment:
    declared = set(rulebook.rules)
    supplied = set(outcomes)
    unknown = supplied - declared
    if unknown:
        raise ValueError(f"unknown outcome rule IDs: {sorted(unknown)}")
    mismatched = sorted(key for key, outcome in outcomes.items() if not isinstance(outcome, RuleOutcome) or outcome.rule_id != key)
    if mismatched:
        raise ValueError(f"outcome rule_id does not match mapping key: {mismatched}")
    proxy_declared = {rid for rid, rule in rulebook.rules.items() if rule.observability is Observability.PIT_PROXY}
    approved = set(approved_proxy_rule_ids)
    if not approved <= proxy_declared:
        raise ValueError("approved proxy IDs must be an exact subset of declared pit_proxy rules")
    missing = declared - supplied
    if missing:
        raise ValueError(f"missing outcome rule IDs: {sorted(missing)}")

    resolved = dict(outcomes)
    # N.NEWNESS is a true one-of: a passing child satisfies the parent, while
    # proximity or an unavailable child never silently passes it.
    newness = rulebook.rules.get("N.NEWNESS")
    if newness and newness.satisfaction_logic == "one_of:N.CATALYST,N.NEW_HIGH":
        child_ids = ("N.CATALYST", "N.NEW_HIGH")
        children = [resolved.get(rid) for rid in child_ids]
        if any(item is not None and item.status == "passed" for item in children):
            resolved["N.NEWNESS"] = RuleOutcome.passed("N.NEWNESS")
        elif all(item is not None and item.status == "failed" for item in children):
            resolved["N.NEWNESS"] = RuleOutcome.failed("N.NEWNESS")
        elif any(item is not None and item.status == "unavailable" for item in children):
            resolved["N.NEWNESS"] = RuleOutcome.unavailable("N.NEWNESS")
        else:
            resolved["N.NEWNESS"] = RuleOutcome.unimplemented("N.NEWNESS")

    required = [rid for rid, rule in rulebook.rules.items() if rule.classification is RuleClassification.REQUIRED]
    passed, failed, unavailable = [], [], []
    for rid in required:
        outcome = resolved.get(rid)
        if outcome is None or outcome.status in {"unavailable", "unimplemented"}:
            unavailable.append(rid)
        elif outcome.status == "failed":
            failed.append(rid)
        elif outcome.status == "passed":
            passed.append(rid)
        else:  # defensive: RuleOutcome validates this, but mappings can be hostile.
            raise ValueError(f"invalid outcome for {rid}")

    proxy_ids = tuple(sorted(approved))
    blockers = failed + unavailable
    required_proxy_ids = {
        rid for rid, rule in rulebook.rules.items()
        if rule.classification is RuleClassification.REQUIRED and rule.observability is Observability.PIT_PROXY
    }
    if blockers or (required_proxy_ids - approved):
        label = FidelityLabel.FIDELITY_INCOMPLETE
    elif proxy_ids:
        label = FidelityLabel.QUANTITATIVE_CANSLIM_PROXY
    else:
        label = FidelityLabel.STRICT_CANSLIM
    return FidelityAssessment(
        label, tuple(passed), tuple(failed), tuple(unavailable), proxy_ids,
        not blockers and not (required_proxy_ids - approved),
    )
