"""Declared fixed-action and execution-outcome comparisons for #106."""

from copy import deepcopy
from decimal import Decimal

import pytest

from core.pit_policy_parity import ActionParityDisposition, compare_action_parity_cases
from tests.test_issue106_action_parity import _complete_addition_case, _complete_entry_case
from tests.test_issue106_replacement_parity import _comparison_case


_FAMILIES = ("entry", "replacement", "addition", "scale_out", "close")


def _complete_case(family: str) -> dict[str, object]:
    if family == "entry":
        return _complete_entry_case()
    if family == "replacement":
        case = _comparison_case()
        case["execution"].update(
            order_status="filled", filled_quantity=Decimal("6"),
            partial_fill=False, missed_fill=False, rejected=False,
            cancelled=False, liquidity_assumption="recorded-full-fill-control",
        )
        return case
    case = _complete_addition_case()
    if family == "addition":
        return case
    case["facts"] = {
        "holding_episode_id": "holding:1",
        "security_id": "security:ABC",
        "broker_symbol": "ABC",
        "original_quantity": Decimal("6"),
        "remaining_quantity": Decimal("6"),
        "holding_state_version": 3,
        "confirmed_stop_price": Decimal("90"),
    }
    case["decision"].update(
        action_family=family, category="exit", subject_type="holding", subject_id="holding:1",
    )
    case["intent"] = {
        "action_family": family,
        "role": family,
        "side": "sell",
        "security_id": "security:ABC",
        "broker_symbol": "ABC",
        "requested_quantity": Decimal("3") if family == "scale_out" else Decimal("6"),
        "order_type": "market",
        "holding_episode_id": "holding:1",
    }
    if family == "scale_out":
        case["intent"].update(
            snapshot_original_quantity=Decimal("6"),
            fraction_of_original_quantity=Decimal("0.5"),
            exit_tier=1,
            rounding_rule_id="whole_share_floor_v1",
        )
    case["execution"].update(
        filled_quantity=case["intent"]["requested_quantity"],
        order_status="filled",
    )
    return case


@pytest.mark.parametrize("family", _FAMILIES)
def test_all_supported_action_families_have_a_complete_control(family):
    case = _complete_case(family)

    result = compare_action_parity_cases(case, deepcopy(case))

    assert result.disposition is ActionParityDisposition.MATCHED
    assert result.unknowns == ()


@pytest.mark.parametrize("family", _FAMILIES)
@pytest.mark.parametrize(
    ("changes", "expected_fields"),
    [
        (
            {"order_status": "rejected", "rejected": True, "rejection_reason": "broker-rejected"},
            {"execution.order_status", "execution.rejected"},
        ),
        (
            {"order_status": "cancelled", "cancelled": True, "cancellation_reason": "remainder-cancelled"},
            {"execution.order_status", "execution.cancelled"},
        ),
        (
            {"order_status": "partially_filled", "partial_fill": True, "filled_quantity": Decimal("1")},
            {"execution.order_status", "execution.partial_fill", "execution.filled_quantity"},
        ),
        (
            {"order_status": "expired", "missed_fill": True, "filled_quantity": Decimal("0")},
            {"execution.order_status", "execution.missed_fill", "execution.filled_quantity"},
        ),
        (
            {"liquidity_assumption": "paper-unknown-depth"},
            {"execution.liquidity_assumption"},
        ),
    ],
    ids=("rejected", "cancelled", "partial", "missed", "liquidity"),
)
def test_broker_outcome_difference_does_not_change_fixed_action(family, changes, expected_fields):
    historical = _complete_case(family)
    paper = deepcopy(historical)
    paper["execution"].update(changes)

    result = compare_action_parity_cases(historical, paper)

    assert result.disposition is ActionParityDisposition.MATCHED_WITH_EXECUTION_VARIANCE
    assert expected_fields.issubset(set(result.execution_variances))
    assert result.mismatches == ()


@pytest.mark.parametrize("family", _FAMILIES)
def test_changed_order_type_is_a_fixed_intent_mismatch(family):
    historical = _complete_case(family)
    paper = deepcopy(historical)
    field = "sell_order_type" if family == "replacement" else "order_type"
    paper["intent"][field] = "limit" if historical["intent"][field] == "market" else "market"

    result = compare_action_parity_cases(historical, paper)

    assert result.disposition is ActionParityDisposition.MISMATCH
    assert result.mismatches == (f"intent.{field}",)
    assert result.execution_variances == ()


@pytest.mark.parametrize("family", _FAMILIES)
def test_quantity_precision_changes_only_when_numeric_value_changes(family):
    historical = _complete_case(family)
    paper = deepcopy(historical)
    field = "sell_quantity" if family == "replacement" else "requested_quantity"
    paper["intent"][field] = Decimal(str(historical["intent"][field])) + Decimal("0.001")

    changed = compare_action_parity_cases(historical, paper)

    assert changed.disposition is ActionParityDisposition.MISMATCH
    assert changed.mismatches == (f"intent.{field}",)
    paper["intent"][field] = Decimal(str(historical["intent"][field])).quantize(Decimal("0.00"))
    assert compare_action_parity_cases(historical, paper).disposition is ActionParityDisposition.MATCHED


def test_decision_instant_has_zero_tolerance_but_execution_time_can_vary():
    historical = _complete_entry_case()
    paper = deepcopy(historical)
    paper["decision"]["as_of_cutoff_at"] = "2025-02-04T15:00:00-05:00"
    assert compare_action_parity_cases(historical, paper).disposition is ActionParityDisposition.MATCHED

    paper["decision"]["as_of_cutoff_at"] = "2025-02-04T20:00:01+00:00"
    assert compare_action_parity_cases(historical, paper).mismatches == ("decision.as_of_cutoff_at",)

    paper["decision"]["as_of_cutoff_at"] = historical["decision"]["as_of_cutoff_at"]
    historical["execution"]["submitted_at"] = "2025-02-05T14:31:00+00:00"
    paper["execution"]["submitted_at"] = "2025-02-05T14:31:01+00:00"
    assert compare_action_parity_cases(historical, paper).execution_variances == ("execution.submitted_at",)


def test_missing_liquidity_or_order_type_is_unknown_not_a_match():
    historical = _complete_case("close")
    paper = deepcopy(historical)
    historical["execution"]["liquidity_assumption"] = None
    paper["execution"]["liquidity_assumption"] = None
    historical["intent"]["order_type"] = None
    paper["intent"]["order_type"] = None

    result = compare_action_parity_cases(historical, paper)

    assert result.disposition is ActionParityDisposition.INCOMPLETE
    assert result.unknowns == ("execution.liquidity_assumption", "intent.order_type")


def test_one_sided_execution_reference_or_time_is_unknown():
    historical = _complete_entry_case()
    paper = deepcopy(historical)
    paper["execution"]["provider_order_id"] = "paper-order:1"
    paper["execution"]["submitted_at"] = "2025-02-05T14:31:00+00:00"

    result = compare_action_parity_cases(historical, paper)

    assert result.disposition is ActionParityDisposition.INCOMPLETE
    assert result.unknowns == ("execution.provider_order_id", "execution.submitted_at")
    assert result.execution_variances == ()


def test_undeclared_execution_difference_cannot_be_hidden_as_broker_variance():
    historical = _complete_entry_case()
    paper = deepcopy(historical)
    historical["execution"]["unregistered_profit"] = Decimal("5")
    paper["execution"]["unregistered_profit"] = Decimal("6")

    result = compare_action_parity_cases(historical, paper)

    assert result.disposition is ActionParityDisposition.MISMATCH
    assert result.mismatches == ("execution.unregistered_profit",)
