from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from agent_loop import BudgetLedger, ORCHESTRATOR_MODEL, OpenRouterGateway, ProtocolValidationError
from pit_diagnosis_agent import (
    PitAgentEvidence,
    PitDomain,
    PitReasoningPlan,
    PitRoute,
    validate_pit_reasoning_plan,
)


def _sha(character: str) -> str:
    return character * 64


def pit_agent_evidence(*, experiment_ids: tuple[str, ...] = ("D4.STRUCTURAL_SELL",)) -> PitAgentEvidence:
    return PitAgentEvidence(
        diagnosis_run_sha256=_sha("a"),
        pit_bundle_sha256=_sha("b"),
        fact_cache_sha256=_sha("c"),
        rulebook_sha256=_sha("d"),
        experiment_catalog_sha256=_sha("e"),
        experiment_result_sha256s={experiment_id: _sha("f") for experiment_id in experiment_ids},
        metrics={"average_cash_pct": 42.0, "completed_positions": 7},
        evidence_ids=("EXIT.MA_001",),
        rule_ids=("X.STRUCTURAL_SELL",),
        invariant_ids=("INV.LOSS_LIMIT",),
        experiment_ids=experiment_ids,
        fidelity_label="fidelity_incomplete",
        promotion_eligible=False,
    )


def valid_pit_plan_json(experiment_id: str) -> str:
    return json.dumps(
        {
            "causal_hypothesis": "The cited exit condition can be measured with the supplied experiment.",
            "evidence_ids": ["EXIT.MA_001"],
            "rule_ids": ["X.STRUCTURAL_SELL"],
            "invariant_ids": ["INV.LOSS_LIMIT"],
            "experiment_id": experiment_id,
            "skip": False,
            "skip_reason": "",
        }
    )


def test_pit_orchestrator_can_only_route_closed_ids() -> None:
    route = PitRoute.from_json(
        '{"action":"reason","domain":"exit","evidence_ids":["EXIT.MA_001"]}'
    )
    assert route.domain is PitDomain.EXIT
    with pytest.raises(ProtocolValidationError):
        PitRoute.from_json(
            '{"action":"reason","domain":"exit","evidence_ids":["EXIT.MA_001"],'
            '"reasoning":"change the moving average"}'
        )


def test_reasoner_must_choose_one_supplied_experiment() -> None:
    evidence = pit_agent_evidence(experiment_ids=("D4.PROFIT_ZONE",))
    plan = PitReasoningPlan.from_json(valid_pit_plan_json("D4.PROFIT_ZONE"))
    validate_pit_reasoning_plan(plan, evidence)
    with pytest.raises(ProtocolValidationError, match="experiment"):
        validate_pit_reasoning_plan(
            PitReasoningPlan.from_json(valid_pit_plan_json("D4.INVENTED")),
            evidence,
        )


def test_pit_evidence_rejects_raw_data_bearing_keys() -> None:
    payload = pit_agent_evidence().to_provider_payload()
    payload["raw"] = {"prices": []}
    with pytest.raises(ProtocolValidationError):
        PitAgentEvidence.from_json(json.dumps(payload))


@dataclass
class _FakeResponse:
    content: str
    model: str
    cost: float = 0.01

    @property
    def choices(self) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=self.content, refusal=None),
            )
        ]

    @property
    def usage(self) -> SimpleNamespace:
        return SimpleNamespace(
            prompt_tokens=11,
            completion_tokens=7,
            total_tokens=18,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
            cost=self.cost,
        )


class _FakeCompletions:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        return self.responses.pop(0)


class _FakeClient:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.completions = _FakeCompletions(responses)
        self.chat = SimpleNamespace(completions=self.completions)


def test_pit_gateway_uses_same_models_with_distinct_closed_prompts() -> None:
    response = _FakeResponse(
        '{"action":"reason","domain":"exit","evidence_ids":["EXIT.MA_001"]}',
        model=ORCHESTRATOR_MODEL,
    )
    fake_client = _FakeClient([response])
    gateway = OpenRouterGateway(
        client=fake_client,
        pricing_loader=lambda _model: {"prompt": 1.0, "completion": 1.0},
        ledger=BudgetLedger(max_usd=1.0),
    )

    gateway.request_pit_diagnosis_once("orchestrator", "{}", PitRoute.from_json)

    request = fake_client.chat.completions.calls[0]
    assert request["model"] == ORCHESTRATOR_MODEL
    assert "failure_summary" not in request["messages"][0]["content"]
    assert '"domain"' in request["messages"][0]["content"]
    assert request["messages"][1]["content"] == OpenRouterGateway.STATIC_CONTEXT
