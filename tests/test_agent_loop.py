from __future__ import annotations

import importlib
import json
import sys
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest


def _route_json(**overrides: object) -> str:
    payload: dict[str, object] = {
        "action": "reason",
        "failure_summary": "The focused test fails.",
        "relevant_files": ["core/backtest_engine.py"],
        "reasoning_focus": "Find the smallest safe repair.",
    }
    payload.update(overrides)
    return json.dumps(payload)


def _plan_json(**overrides: object) -> str:
    payload: dict[str, object] = {
        "diagnosis": "A boundary is wrong.",
        "root_cause": "The inclusive condition was omitted.",
        "invariants": ["Existing valid signals remain valid."],
        "files_to_change": ["core/backtest_engine.py"],
        "steps": ["Correct the boundary comparison."],
        "skip": False,
        "skip_reason": "",
    }
    payload.update(overrides)
    return json.dumps(payload)


@dataclass
class FakeResponse:
    """Complete enough OpenAI-shaped response for gateway boundary tests."""

    content: str
    finish_reason: str = "stop"
    cost: float | None = None
    error: object | None = None

    @property
    def choices(self) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                finish_reason=self.finish_reason,
                message=SimpleNamespace(content=self.content, refusal=None),
            )
        ]

    @property
    def usage(self) -> SimpleNamespace:
        return SimpleNamespace(
            prompt_tokens=11,
            completion_tokens=7,
            total_tokens=18,
            prompt_tokens_details=SimpleNamespace(cached_tokens=3),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=5),
            cost=self.cost,
        )


class FakeCompletions:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome  # type: ignore[return-value]


class FakeClient:
    def __init__(self, outcomes: list[object]) -> None:
        self.completions = FakeCompletions(outcomes)
        self.chat = SimpleNamespace(completions=self.completions)


def test_import_is_lazy_and_never_reads_key_or_execution_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    """Break caught: adding import-time credential, dotenv, or live-runtime side effects."""
    import agent_loop

    def forbidden_getenv(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("import must not inspect environment")

    monkeypatch.setattr(agent_loop.os, "getenv", forbidden_getenv)
    monkeypatch.delitem(sys.modules, "agent_loop")
    reloaded = importlib.import_module("agent_loop")

    assert reloaded.MAX_ITERATIONS == 10
    assert "auto_trader" not in sys.modules
    assert "paper_trading_console" not in sys.modules
    assert "scheduler" not in sys.modules


def test_protocol_rejects_duplicate_and_unknown_json_keys() -> None:
    """Break caught: permissive JSON parsing could hide conflicting model instructions."""
    from agent_loop import ProtocolValidationError, Route

    duplicate = (
        '{"action":"reason","action":"abort","failure_summary":"x",'
        '"relevant_files":[],"reasoning_focus":"y"}'
    )
    with pytest.raises(ProtocolValidationError, match="duplicate"):
        Route.from_json(duplicate)
    with pytest.raises(ProtocolValidationError, match="unknown"):
        Route.from_json(_route_json(untrusted="instruction"))


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (_route_json(action="write"), "action"),
        (_route_json(failure_summary="   "), "blank"),
        (_route_json(relevant_files="core/backtest_engine.py"), "list"),
        (_route_json(relevant_files=["../config/settings.py"]), "relative"),
        (_route_json(relevant_files=["C:/outside.py"]), "relative"),
        (_route_json(relevant_files=["core\\backtest_engine.py"]), "relative"),
        (_route_json(relevant_files=["x.py"] * 9), "too many"),
    ],
)
def test_protocol_rejects_untrusted_route_shapes(payload: str, message: str) -> None:
    """Break caught: invalid route data could expand controller scope."""
    from agent_loop import ProtocolValidationError, Route

    with pytest.raises(ProtocolValidationError, match=message):
        Route.from_json(payload)


def test_reasoning_plan_and_coding_proposal_are_frozen_and_validate_limits() -> None:
    """Break caught: mutable or oversized role payloads could change after validation."""
    from agent_loop import CodingProposal, ProtocolValidationError, ReasoningPlan

    plan = ReasoningPlan.from_json(_plan_json())
    with pytest.raises(AttributeError):
        plan.diagnosis = "mutated"  # type: ignore[misc]
    with pytest.raises(ProtocolValidationError, match="too long"):
        CodingProposal.from_json(
            json.dumps(
                {
                    "summary": "safe patch",
                    "files": ["core/backtest_engine.py"],
                    "unified_diff": "x" * 262145,
                }
            )
        )


def test_usage_and_completion_reject_invalid_direct_values() -> None:
    """Break caught: malformed provider metadata could poison the shared budget ledger."""
    from agent_loop import AgentCompletion, ProtocolValidationError, Usage

    with pytest.raises(ProtocolValidationError, match="usage"):
        Usage(prompt_tokens=-1)
    with pytest.raises(ProtocolValidationError, match="finish_reason"):
        AgentCompletion(payload="route", usage=Usage(), finish_reason="length", model=None)


@pytest.mark.parametrize("max_usd", [0.0, -1.0, float("nan"), float("inf")])
def test_budget_rejects_non_finite_or_non_positive_hard_caps(max_usd: float) -> None:
    """Break caught: NaN/Infinity budgets could bypass the controller's USD limit."""
    from agent_loop import BudgetLedger, ConfigurationError

    with pytest.raises(ConfigurationError, match="max_usd"):
        BudgetLedger(max_usd=max_usd)


def test_direct_protocol_construction_validates_action_and_paths() -> None:
    """Break caught: callers could bypass JSON parsing and inject an unsafe protocol object."""
    from agent_loop import ProtocolValidationError, Route

    with pytest.raises(ProtocolValidationError, match="action"):
        Route("write", "failure", (), "focus")
    with pytest.raises(ProtocolValidationError, match="relative"):
        Route("reason", "failure", ("../config/settings.py",), "focus")


def test_gateway_uses_immutable_three_message_prefix_and_reasoner_cap() -> None:
    """Break caught: changing message order or cap loses cache/safety guarantees."""
    from agent_loop import BudgetLedger, OpenRouterGateway, ReasoningPlan

    client = FakeClient([FakeResponse(_plan_json(), cost=0.00001)])
    gateway = OpenRouterGateway(
        client=client,
        run_id="run-123",
        pricing_loader=lambda _model: {"prompt": 1.0, "completion": 2.0},
        ledger=BudgetLedger(max_usd=1.0),
    )

    completion = gateway.request("reasoner", "failure evidence", ReasoningPlan.from_json)
    call = client.completions.calls[0]

    assert completion.payload.diagnosis == "A boundary is wrong."
    assert call["model"] == "deepseek/deepseek-r1"
    assert call["max_tokens"] == 4096
    assert call["response_format"] == {"type": "json_object"}
    assert call["stream"] is False
    assert call["extra_headers"]["X-Session-Id"] == "run-123:reasoner"
    assert [message["content"] for message in call["messages"][:2]] == [
        gateway.SYSTEM_PROMPTS["reasoner"],
        gateway.STATIC_CONTEXT,
    ]
    assert call["messages"][2] == {
        "role": "user",
        "content": "<dynamic-input>\nfailure evidence\n</dynamic-input>",
    }
    with pytest.raises(TypeError):
        gateway.SYSTEM_PROMPTS["reasoner"] = "unsafe mutation"  # type: ignore[index]


def test_gateway_repairs_malformed_and_rejects_truncated_output_after_one_attempt() -> None:
    """Break caught: malformed provider output could cause an unbounded repair loop."""
    from agent_loop import BudgetLedger, OpenRouterGateway, ResponseValidationError, Route

    client = FakeClient([FakeResponse("not json", finish_reason="length"), FakeResponse("not json")])
    gateway = OpenRouterGateway(
        client=client,
        pricing_loader=lambda _model: {"prompt": 1.0, "completion": 1.0},
        ledger=BudgetLedger(max_usd=1.0),
    )

    with pytest.raises(ResponseValidationError):
        gateway.request("orchestrator", "failure evidence", Route.from_json)
    assert len(client.completions.calls) == 2
    assert "repair" in client.completions.calls[1]["messages"][2]["content"].lower()


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503, 504, 524, 529])
def test_gateway_retries_only_transient_statuses(status: int) -> None:
    """Break caught: a transient OpenRouter failure would abort a recoverable call."""
    from agent_loop import BudgetLedger, GatewayError, OpenRouterGateway, Route

    client = FakeClient([GatewayError("temporary", status_code=status), FakeResponse(_route_json())])
    gateway = OpenRouterGateway(
        client=client,
        pricing_loader=lambda _model: {"prompt": 1.0, "completion": 1.0},
        ledger=BudgetLedger(max_usd=1.0),
    )

    assert gateway.request("orchestrator", "evidence", Route.from_json).payload.action == "reason"
    assert len(client.completions.calls) == 2


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 422])
def test_gateway_never_retries_non_transient_statuses(status: int) -> None:
    """Break caught: retrying auth/input failures spends budget without possible recovery."""
    from agent_loop import BudgetLedger, GatewayError, OpenRouterGateway, Route

    client = FakeClient([GatewayError("permanent", status_code=status), FakeResponse(_route_json())])
    gateway = OpenRouterGateway(
        client=client,
        pricing_loader=lambda _model: {"prompt": 1.0, "completion": 1.0},
        ledger=BudgetLedger(max_usd=1.0),
    )

    with pytest.raises(GatewayError):
        gateway.request("orchestrator", "evidence", Route.from_json)
    assert len(client.completions.calls) == 1


def test_budget_reserves_before_call_and_keeps_reservation_when_usage_cost_is_missing() -> None:
    """Break caught: a missing provider cost could make later calls exceed the USD cap."""
    from agent_loop import BudgetLedger, OpenRouterGateway, Route

    client = FakeClient([FakeResponse(_route_json(), cost=None)])
    ledger = BudgetLedger(max_usd=1.0)
    gateway = OpenRouterGateway(
        client=client,
        pricing_loader=lambda _model: {"prompt": 1.0, "completion": 2.0},
        ledger=ledger,
    )

    gateway.request("orchestrator", "evidence", Route.from_json)

    assert ledger.calls == 1
    assert ledger.prompt_tokens == 11
    assert ledger.completion_tokens == 7
    assert ledger.reserved_usd > 0
    assert ledger.spent_usd == ledger.reserved_usd


def test_budget_reconciles_an_explicit_zero_usage_cost() -> None:
    """Break caught: treating zero cost as missing leaves an unnecessary budget reservation."""
    from agent_loop import BudgetLedger, OpenRouterGateway, Route

    client = FakeClient([FakeResponse(_route_json(), cost=0.0)])
    ledger = BudgetLedger(max_usd=1.0)
    gateway = OpenRouterGateway(
        client=client,
        pricing_loader=lambda _model: {"prompt": 1.0, "completion": 2.0},
        ledger=ledger,
    )

    completion = gateway.request("orchestrator", "evidence", Route.from_json)

    assert completion.usage.cost_usd == 0.0
    assert ledger.spent_usd == 0.0
    assert ledger.reserved_usd == 0.0


def test_usage_preserves_zero_cost_when_only_usage_contains_it() -> None:
    """Break caught: SDK shape variation could turn a reported free call into a reservation."""
    from agent_loop import BudgetLedger, OpenRouterGateway, Route

    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=_route_json(), refusal=None),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            prompt_tokens_details=None,
            completion_tokens_details=None,
            cost=0.0,
        ),
    )
    ledger = BudgetLedger(max_usd=1.0)
    gateway = OpenRouterGateway(
        client=FakeClient([response]),
        pricing_loader=lambda _model: {"prompt": 1.0, "completion": 2.0},
        ledger=ledger,
    )

    assert gateway.request("orchestrator", "evidence", Route.from_json).usage.cost_usd == 0.0
    assert ledger.reserved_usd == 0.0


def test_default_gateway_accepts_aliases_fails_closed_and_lazily_configures_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: key aliases diverge or an SDK retry/client is configured unsafely."""
    import agent_loop
    from agent_loop import ConfigurationError, OpenRouterGateway

    monkeypatch.setattr(agent_loop, "_controller_dotenv_values", lambda: {})
    monkeypatch.setenv("OPENROUTER", "alias-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    gateway = OpenRouterGateway()
    assert gateway.api_key == "alias-key"
    monkeypatch.setenv("OPENROUTER_API_KEY", "different-key")
    with pytest.raises(ConfigurationError, match="differ"):
        OpenRouterGateway()

    configured: dict[str, object] = {}

    class FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            configured.update(kwargs)

    monkeypatch.setenv("OPENROUTER_API_KEY", "same-key")
    monkeypatch.setenv("OPENROUTER", "same-key")
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    lazy_gateway = OpenRouterGateway(app_url="https://example.invalid", app_name="safe-loop")
    assert configured == {}
    lazy_gateway._get_client()
    assert configured == {
        "api_key": "same-key",
        "base_url": "https://openrouter.ai/api/v1",
        "timeout": 30.0,
        "max_retries": 0,
        "default_headers": {"HTTP-Referer": "https://example.invalid", "X-Title": "safe-loop"},
    }


def test_default_gateway_fails_closed_when_environment_and_common_dotenv_disagree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: linked-worktree lookup could silently select a different credential."""
    import agent_loop
    from agent_loop import ConfigurationError, OpenRouterGateway

    monkeypatch.setenv("OPENROUTER_API_KEY", "environment-key")
    monkeypatch.delenv("OPENROUTER", raising=False)
    monkeypatch.setattr(agent_loop, "_controller_dotenv_values", lambda: {"OPENROUTER": "dotenv-key"})

    with pytest.raises(ConfigurationError, match="differ"):
        OpenRouterGateway()
