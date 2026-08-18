from __future__ import annotations

import importlib
import hashlib
import json
import stat
import subprocess
import sys
import tempfile
import time
import os
from dataclasses import dataclass
from pathlib import Path
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
    with pytest.raises(ProtocolValidationError, match="Usage"):
        AgentCompletion(payload="route", usage=object(), finish_reason="stop", model=None)  # type: ignore[arg-type]


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
    assert call["extra_body"] == {"provider": {"require_parameters": True}}
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


def test_gateway_classifies_lazy_openai_connection_and_timeout_shapes() -> None:
    """Break caught: SDK transport failures would be treated as permanent without eager imports."""
    from agent_loop import _is_retryable

    connection_type = type("APIConnectionError", (Exception,), {"__module__": "openai._exceptions"})
    timeout_type = type("APITimeoutError", (connection_type,), {"__module__": "openai._exceptions"})

    connection = connection_type()
    connection.request = object()  # type: ignore[attr-defined]
    connection.body = None  # type: ignore[attr-defined]
    timeout = timeout_type()
    timeout.request = object()  # type: ignore[attr-defined]
    timeout.body = None  # type: ignore[attr-defined]

    assert _is_retryable(connection)
    assert _is_retryable(timeout)


def test_gateway_embedded_permanent_error_never_retries_or_repairs() -> None:
    """Break caught: an embedded 400 error was incorrectly sent through the repair loop."""
    from agent_loop import BudgetLedger, GatewayError, OpenRouterGateway, Route

    client = FakeClient([FakeResponse(_route_json(), error={"status": 400}), FakeResponse(_route_json())])
    gateway = OpenRouterGateway(
        client=client,
        pricing_loader=lambda _model: {"prompt": 1.0, "completion": 1.0},
        ledger=BudgetLedger(max_usd=1.0),
    )

    with pytest.raises(GatewayError) as raised:
        gateway.request("orchestrator", "evidence", Route.from_json)
    assert raised.value.status_code == 400
    assert len(client.completions.calls) == 1


def test_gateway_retries_embedded_transient_error_without_repair_prompt() -> None:
    """Break caught: an embedded 429 error was not classified by the bounded retry policy."""
    from agent_loop import BudgetLedger, OpenRouterGateway, Route

    client = FakeClient([FakeResponse(_route_json(), error={"status_code": 429}), FakeResponse(_route_json())])
    gateway = OpenRouterGateway(
        client=client,
        pricing_loader=lambda _model: {"prompt": 1.0, "completion": 1.0},
        ledger=BudgetLedger(max_usd=1.0),
    )

    assert gateway.request("orchestrator", "evidence", Route.from_json).payload.action == "reason"
    assert len(client.completions.calls) == 2
    assert "repair" not in client.completions.calls[1]["messages"][2]["content"].lower()


def test_gateway_classifies_nested_embedded_error_status_metadata() -> None:
    """Break caught: provider metadata status was discarded and skipped retry classification."""
    from agent_loop import BudgetLedger, OpenRouterGateway, Route

    client = FakeClient(
        [FakeResponse(_route_json(), error={"metadata": {"status": 503}}), FakeResponse(_route_json())]
    )
    gateway = OpenRouterGateway(
        client=client,
        pricing_loader=lambda _model: {"prompt": 1.0, "completion": 1.0},
        ledger=BudgetLedger(max_usd=1.0),
    )

    assert gateway.request("orchestrator", "evidence", Route.from_json).payload.action == "reason"
    assert len(client.completions.calls) == 2


@pytest.mark.parametrize(
    ("code", "should_retry"),
    [(429, True), (402, False)],
)
def test_gateway_classifies_standard_embedded_openrouter_error_code(
    code: int,
    should_retry: bool,
) -> None:
    """Break caught: documented OpenRouter ``error.code`` was ignored for retry safety."""
    from agent_loop import BudgetLedger, GatewayError, OpenRouterGateway, Route

    client = FakeClient(
        [
            FakeResponse(_route_json(), error={"code": code, "message": "provider error"}),
            FakeResponse(_route_json()),
        ]
    )
    gateway = OpenRouterGateway(
        client=client,
        pricing_loader=lambda _model: {"prompt": 1.0, "completion": 1.0},
        ledger=BudgetLedger(max_usd=1.0),
    )

    if should_retry:
        assert gateway.request("orchestrator", "evidence", Route.from_json).payload.action == "reason"
        assert len(client.completions.calls) == 2
        assert "repair" not in client.completions.calls[1]["messages"][2]["content"].lower()
    else:
        with pytest.raises(GatewayError) as raised:
            gateway.request("orchestrator", "evidence", Route.from_json)
        assert raised.value.status_code == code
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


def test_budget_enforces_exact_call_and_token_reservation_boundaries() -> None:
    """Break caught: call/token limits allowed one extra provider call or over-budget token use."""
    from agent_loop import BudgetExceededError, BudgetLedger, Pricing, Usage

    pricing = Pricing(prompt_per_million=0.0, completion_per_million=0.0)
    ledger = BudgetLedger(max_usd=1.0, max_calls=1, max_tokens=10)
    reservation = ledger.reserve("abc", 7, pricing)
    ledger.reconcile(reservation, Usage())
    assert ledger.total_tokens == 10
    with pytest.raises(BudgetExceededError, match="call"):
        ledger.reserve("", 1, pricing)

    token_limited = BudgetLedger(max_usd=1.0, max_calls=2, max_tokens=10)
    with pytest.raises(BudgetExceededError, match="token"):
        token_limited.reserve("abc", 8, pricing)


def test_usage_rejects_total_lower_than_reported_prompt_and_completion() -> None:
    """Break caught: a provider total lower than component totals could undercharge token budget."""
    from agent_loop import ProtocolValidationError, Usage

    with pytest.raises(ProtocolValidationError, match="total_tokens"):
        Usage(prompt_tokens=6, completion_tokens=5, total_tokens=1)


@pytest.mark.parametrize(
    ("prompt_tokens", "completion_tokens"),
    [(3, None), (None, 3)],
)
def test_usage_rejects_total_lower_than_a_reported_partial_component(
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> None:
    """Break caught: a partial provider usage shape could still release token headroom."""
    from agent_loop import ProtocolValidationError, Usage

    with pytest.raises(ProtocolValidationError, match="total_tokens"):
        Usage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, total_tokens=2)


@pytest.mark.parametrize(
    ("prompt_tokens", "completion_tokens", "total_tokens"),
    [(3, None, 3), (None, 3, 3), (3, None, 4), (None, 3, 4)],
)
def test_usage_accepts_consistent_partial_component_totals(
    prompt_tokens: int | None,
    completion_tokens: int | None,
    total_tokens: int,
) -> None:
    """Break caught: hardening could reject valid provider usage shapes with one component omitted."""
    from agent_loop import Usage

    usage = Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )
    assert usage.total_tokens == total_tokens


def test_consistent_usage_reconciles_and_leaves_only_real_token_headroom() -> None:
    """Break caught: strict validation could reject a consistent provider total or release too much headroom."""
    from agent_loop import BudgetLedger, Pricing, Usage

    ledger = BudgetLedger(max_usd=1.0, max_calls=2, max_tokens=10)
    reservation = ledger.reserve("abc", 7, Pricing(0.0, 0.0))
    ledger.reconcile(reservation, Usage(prompt_tokens=3, completion_tokens=4, total_tokens=7))

    assert ledger.total_tokens == 7
    assert ledger.reserved_tokens == 7
    ledger.reserve("", 3, Pricing(0.0, 0.0))


def test_gateway_reserves_full_static_and_dynamic_message_bytes_before_call() -> None:
    """Break caught: reservations omitted immutable system/static messages from the prompt bound."""
    from agent_loop import BudgetExceededError, BudgetLedger, OpenRouterGateway, Route

    client = FakeClient([FakeResponse(_route_json())])
    dynamic = "evidence"
    expected_messages = [
        {"role": "system", "content": OpenRouterGateway.SYSTEM_PROMPTS["orchestrator"]},
        {"role": "system", "content": OpenRouterGateway.STATIC_CONTEXT},
        {"role": "user", "content": "<dynamic-input>\nevidence\n</dynamic-input>"},
    ]
    full_prompt_upper_bound = len(
        json.dumps(expected_messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ) + 2048
    gateway = OpenRouterGateway(
        client=client,
        pricing_loader=lambda _model: {"prompt": 0.0, "completion": 0.0},
        ledger=BudgetLedger(max_usd=1.0, max_calls=1, max_tokens=full_prompt_upper_bound - 1),
    )

    with pytest.raises(BudgetExceededError, match="token"):
        gateway.request("orchestrator", dynamic, Route.from_json)
    assert client.completions.calls == []


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

    monkeypatch.setattr(agent_loop, "_controller_dotenv_values", lambda _root: {})
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
    monkeypatch.setattr(agent_loop, "_controller_dotenv_values", lambda _root: {"OPENROUTER": "dotenv-key"})

    with pytest.raises(ConfigurationError, match="differ"):
        OpenRouterGateway()


@pytest.mark.parametrize(
    ("timeout_seconds", "max_attempts"),
    [
        (float("nan"), 2),
        (float("inf"), 2),
        (0.0, 2),
        (30.0, 0),
        (30.0, 3),
        (30.0, 1000),
    ],
)
def test_gateway_rejects_unbounded_timeout_or_attempt_configuration(
    timeout_seconds: float,
    max_attempts: int,
) -> None:
    """Break caught: unbounded retries/timeouts could exceed the controller's hard wall budget."""
    from agent_loop import BudgetLedger, ConfigurationError, OpenRouterGateway

    with pytest.raises(ConfigurationError):
        OpenRouterGateway(
            client=FakeClient([]),
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            ledger=BudgetLedger(max_usd=1.0),
        )


def _run_git(path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True)


def test_common_dotenv_lookup_is_bound_to_explicit_linked_worktree_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: ambient cwd could select an unrelated repository's dotenv credential."""
    import agent_loop

    controller = tmp_path / "controller"
    controller.mkdir()
    _run_git(controller, "init")
    _run_git(controller, "config", "user.email", "tests@example.invalid")
    _run_git(controller, "config", "user.name", "Tests")
    (controller / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    (controller / ".env").write_text(
        "OPENROUTER=controller-only\nIGNORED_SECRET=must-not-be-read\n",
        encoding="utf-8",
    )
    _run_git(controller, "add", "tracked.txt")
    _run_git(controller, "commit", "-m", "initial")
    linked = tmp_path / "linked"
    _run_git(controller, "worktree", "add", "-b", "codex/test-dotenv", str(linked))

    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / ".env").write_text("OPENROUTER=ambient-wrong\n", encoding="utf-8")
    monkeypatch.chdir(unrelated)

    assert agent_loop._controller_dotenv_values(linked) == {"OPENROUTER": "controller-only"}


def _task2_repo(tmp_path: Path, *, branch: str = "codex/task2") -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    _run_git(repo, "init")
    _run_git(repo, "config", "user.email", "tests@example.invalid")
    _run_git(repo, "config", "user.name", "Tests")
    _run_git(repo, "config", "core.autocrlf", "false")
    _run_git(repo, "switch", "-c", branch)
    (repo / "core").mkdir()
    (repo / "tests").mkdir()
    (repo / "core" / "backtest_engine.py").write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
    (repo / "core" / "momentum_analysis.py").write_text("MOMENTUM = 1\n", encoding="utf-8", newline="\n")
    (repo / "tests" / "test_safe.py").write_text(
        "def test_safe():\n    assert True\n", encoding="utf-8", newline="\n"
    )
    (repo / "agent_loop.py").write_text("# captured protected gate\n", encoding="utf-8", newline="\n")
    (repo / ".gitignore").write_text(".env\nignored.txt\n", encoding="utf-8", newline="\n")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "initial")
    return repo


def _task2_diff(*, path: str = "core/backtest_engine.py", old: str = "VALUE = 1", new: str = "VALUE = 2") -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "index 1111111..2222222 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,1 +1,1 @@\n"
        f"-{old}\n"
        f"+{new}\n"
    )


@pytest.mark.parametrize("break_kind", ["dirty", "detached", "protected"])
def test_preflight_rejects_unsafe_source_state(tmp_path: Path, break_kind: str) -> None:
    """Break caught: an unsafe source state could become the captured proposal baseline."""
    from agent_loop import PreflightError, preflight_source

    repo = _task2_repo(tmp_path)
    if break_kind == "dirty":
        (repo / "untracked.txt").write_text("dirty", encoding="utf-8")
    elif break_kind == "detached":
        _run_git(repo, "checkout", "--detach")
    else:
        _run_git(repo, "branch", "-m", "main")

    with pytest.raises(PreflightError):
        preflight_source(repo, acquire_lock=False)


def test_preflight_captures_head_and_uses_worktree_git_lock_path(tmp_path: Path) -> None:
    """Break caught: linked worktrees could lock the wrong repository path or lose the exact HEAD."""
    from agent_loop import preflight_source

    repo = _task2_repo(tmp_path)
    state = preflight_source(repo, acquire_lock=False)

    assert state.head == subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    expected = subprocess.run(
        ["git", "rev-parse", "--git-path", "agent-loop.lock"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert state.lock_path == (repo / expected).resolve() if not Path(expected).is_absolute() else Path(expected).resolve()


def test_preflight_exclusive_lock_and_permanent_runtime_fail_closed(tmp_path: Path) -> None:
    """Break caught: concurrent loops or the permanent paper checkout could become an execution controller."""
    from agent_loop import PreflightError, preflight_source

    repo = _task2_repo(tmp_path)
    with pytest.raises(PreflightError, match="permanent"):
        preflight_source(repo, permanent_runtime_root=repo)

    first = preflight_source(repo)
    try:
        with pytest.raises(PreflightError, match="lock"):
            preflight_source(repo)
    finally:
        first.close()


def test_quarantine_exports_only_exact_tracked_commit_and_private_git(tmp_path: Path) -> None:
    """Break caught: source credentials or ignored files could leak into the candidate export."""
    from agent_loop import export_candidate, preflight_source

    repo = _task2_repo(tmp_path)
    (repo / ".env").write_text("OPENROUTER_API_KEY=secret\n", encoding="utf-8")
    (repo / "ignored.txt").write_text("secret\n", encoding="utf-8")
    state = preflight_source(repo, acquire_lock=False)
    candidate = export_candidate(state)

    assert not (candidate.root / ".env").exists()
    assert not (candidate.root / "ignored.txt").exists()
    assert (candidate.root / ".git").is_dir()
    assert candidate.root.resolve() not in repo.resolve().parents
    assert not candidate.root.resolve().is_relative_to(repo.resolve())
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=candidate.root, check=True, capture_output=True, text=True
    ).stdout == ""


def test_child_environment_is_allowlisted_scrubbed_and_parent_is_unchanged(tmp_path: Path) -> None:
    """Break caught: a worker could inherit broker/provider/Git credentials or mutate its parent env."""
    from agent_loop import build_child_environment

    parent = {
        "PATH": "safe-path",
        "SYSTEMROOT": "safe-root",
        "OPENROUTER_API_KEY": "router-secret",
        "ALPACA_API_KEY": "broker-secret",
        "FMP_API_KEY": "data-secret",
        "GIT_ASKPASS": "credential-helper",
        "HTTPS_PROXY": "http://credential-proxy.invalid",
        "AWS_SECRET_ACCESS_KEY": "cloud-secret",
    }
    original = dict(parent)
    child = build_child_environment(parent, tmp_path / "child-home")

    assert parent == original
    assert "OPENROUTER_API_KEY" not in child
    assert "ALPACA_API_KEY" not in child
    assert "FMP_API_KEY" not in child
    assert "GIT_ASKPASS" not in child
    assert "AWS_SECRET_ACCESS_KEY" not in child
    assert child["ALPACA_PAPER"] == "false"
    assert child["FMP_DAILY_REQUEST_BUDGET"] == "0"
    assert child["PYTHONNOUSERSITE"] == "1"
    assert child["HTTP_PROXY"] == child["HTTPS_PROXY"] == "http://127.0.0.1:9"
    assert child["HOME"] == str((tmp_path / "child-home").resolve())


def test_unsafe_local_mode_can_never_apply_and_has_no_promotion_surface() -> None:
    """Break caught: the development escape hatch could execute model-authored code."""
    from agent_loop import ConfigurationError, ExecutionMode

    assert ExecutionMode(unsafe_local=True, apply=False).status == "unsafe-local-baseline-only"
    with pytest.raises(ConfigurationError):
        ExecutionMode(unsafe_local=True, apply=True)
    with pytest.raises(TypeError):
        ExecutionMode(promote=True)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "path",
    [
        "../core/backtest_engine.py",
        "C:/core/backtest_engine.py",
        "//server/share/file.py",
        "core\\backtest_engine.py",
        "core/file.py:stream",
        "core/CON.py",
        "core/trailing.py.",
        '"core/backtest_engine.py"',
    ],
)
def test_patch_policy_rejects_windows_and_traversal_paths(tmp_path: Path, path: str) -> None:
    """Break caught: path canonicalization ambiguity could write outside approved regular files."""
    from agent_loop import PatchPolicyError, validate_unified_diff

    repo = _task2_repo(tmp_path)
    with pytest.raises(PatchPolicyError):
        validate_unified_diff(repo, _task2_diff(path=path), [path])


@pytest.mark.parametrize(
    "mutation",
    [
        "new file mode 100644\n",
        "deleted file mode 100644\n",
        "rename from core/backtest_engine.py\nrename to core/momentum_analysis.py\n",
        "old mode 100644\nnew mode 100755\n",
        "GIT binary patch\n",
        "diff --cc core/backtest_engine.py\n",
    ],
)
def test_patch_policy_rejects_structural_diff_features(tmp_path: Path, mutation: str) -> None:
    """Break caught: a model could create/delete/rename/re-mode or smuggle a non-text patch."""
    from agent_loop import PatchPolicyError, validate_unified_diff

    repo = _task2_repo(tmp_path)
    patch = _task2_diff().replace("index 1111111..2222222 100644\n", mutation)
    with pytest.raises(PatchPolicyError):
        validate_unified_diff(repo, patch, ["core/backtest_engine.py"])


def test_patch_policy_checks_hunk_counts_declared_files_deny_precedence_and_live_imports(tmp_path: Path) -> None:
    """Break caught: malformed or scope-expanding content could reach Git apply."""
    from agent_loop import PatchPolicyError, validate_unified_diff

    repo = _task2_repo(tmp_path)
    malformed = _task2_diff().replace("@@ -1,1 +1,1 @@", "@@ -1,2 +1,1 @@")
    with pytest.raises(PatchPolicyError, match="hunk"):
        validate_unified_diff(repo, malformed, ["core/backtest_engine.py"])
    with pytest.raises(PatchPolicyError, match="declared"):
        validate_unified_diff(repo, _task2_diff(), ["core/momentum_analysis.py"])
    denied = _task2_diff(path="agent_loop.py")
    with pytest.raises(PatchPolicyError, match="denied"):
        validate_unified_diff(repo, denied, ["agent_loop.py"], editable_paths=["agent_loop.py"])
    live_import = _task2_diff(new="from core.order_execution import Broker")
    with pytest.raises(PatchPolicyError, match="live"):
        validate_unified_diff(repo, live_import, ["core/backtest_engine.py"])


def test_backtest_gate_makes_engine_files_read_only_and_requires_mode_100644(tmp_path: Path) -> None:
    """Break caught: a metrics patch could rewrite its oracle or target a non-regular tracked mode."""
    from agent_loop import PatchPolicyError, validate_unified_diff

    repo = _task2_repo(tmp_path)
    with pytest.raises(PatchPolicyError, match="read-only"):
        validate_unified_diff(repo, _task2_diff(), ["core/backtest_engine.py"], gate="backtest")
    _run_git(repo, "update-index", "--chmod=+x", "core/backtest_engine.py")
    with pytest.raises(PatchPolicyError, match="100644"):
        validate_unified_diff(repo, _task2_diff(), ["core/backtest_engine.py"])


def test_patch_apply_rolls_back_exact_bytes_on_compile_failure(tmp_path: Path) -> None:
    """Break caught: a failed compile could leave a partially mutated candidate behind."""
    from agent_loop import CodingProposal, PatchApplicationError, apply_candidate_patch, export_candidate, preflight_source

    repo = _task2_repo(tmp_path)
    candidate = export_candidate(preflight_source(repo, acquire_lock=False))
    before = (candidate.root / "core" / "backtest_engine.py").read_bytes()
    proposal = CodingProposal("change", ("core/backtest_engine.py",), _task2_diff())

    with pytest.raises(PatchApplicationError, match="compile"):
        apply_candidate_patch(candidate, proposal, compile_runner=lambda _root, _paths: False)

    assert (candidate.root / "core" / "backtest_engine.py").read_bytes() == before
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=candidate.root, check=True, capture_output=True, text=True
    ).stdout == ""


def test_patch_application_accumulates_valid_iterations_without_rejecting_prior_changes(tmp_path: Path) -> None:
    """Break caught: the second safe iteration could be rejected merely because the first patch remains."""
    from agent_loop import CodingProposal, apply_candidate_patch, export_candidate, preflight_source

    repo = _task2_repo(tmp_path)
    candidate = export_candidate(preflight_source(repo, acquire_lock=False))
    first = CodingProposal("first", ("core/backtest_engine.py",), _task2_diff())
    second_diff = _task2_diff(
        path="core/momentum_analysis.py",
        old="MOMENTUM = 1",
        new="MOMENTUM = 2",
    )
    second = CodingProposal("second", ("core/momentum_analysis.py",), second_diff)

    apply_candidate_patch(candidate, first, compile_runner=lambda _root, _paths: True)
    apply_candidate_patch(candidate, second, compile_runner=lambda _root, _paths: True)

    assert (candidate.root / "core" / "backtest_engine.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert (candidate.root / "core" / "momentum_analysis.py").read_text(encoding="utf-8") == "MOMENTUM = 2\n"


def test_worker_export_has_no_git_and_hostile_runner_cannot_change_candidate(tmp_path: Path) -> None:
    """Break caught: candidate execution could mutate its controller-owned Git metadata or manifest."""
    from agent_loop import CandidateMutationError, export_candidate, preflight_source, run_in_disposable_worker, snapshot_tree

    repo = _task2_repo(tmp_path)
    candidate = export_candidate(preflight_source(repo, acquire_lock=False))
    before = snapshot_tree(candidate.root)

    def hostile(layout: Any) -> bool:
        assert not (layout.source / ".git").exists()
        (layout.output / "hostile.txt").write_text("discard me", encoding="utf-8")
        (candidate.root / "candidate-hostile.txt").write_text("must be detected", encoding="utf-8")
        return True

    with pytest.raises(CandidateMutationError):
        run_in_disposable_worker(candidate, hostile)
    assert snapshot_tree(candidate.root) == before


def test_sandbox_command_and_inspection_contract_is_fail_closed(tmp_path: Path) -> None:
    """Break caught: an unattested or weakly configured container could be trusted as a sandbox."""
    from agent_loop import SandboxError, SandboxRunner, export_candidate, preflight_source, run_test_gate

    source = _task2_repo(tmp_path)
    candidate = export_candidate(preflight_source(source, acquire_lock=False))
    image = "registry.invalid/agent-loop@sha256:" + "a" * 64
    with pytest.raises(SandboxError):
        run_test_gate(candidate, SandboxRunner(engine_path=Path("C:/missing/docker.exe"), image=image))


def _create_bundle(path: Path, keys: list[tuple[str, str]]) -> str:
    import hashlib
    import sqlite3

    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE dataset_cache (cache_key TEXT PRIMARY KEY, cache_kind TEXT NOT NULL, "
            "created_at TEXT NOT NULL, payload BLOB NOT NULL)"
        )
        for cache_key, kind in keys:
            conn.execute(
                "INSERT INTO dataset_cache VALUES (?, ?, ?, ?)",
                (cache_key, kind, "2026-08-17T00:00:00", b"opaque-pickle-never-loaded"),
            )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_data_bundle_validates_hash_schema_exact_keys_and_copies_privately(tmp_path: Path) -> None:
    """Break caught: unapproved, incomplete, or schema-confused pickle caches could enter a worker."""
    from agent_loop import DataBundleError, copy_validated_data_bundle, validate_historical_data_bundle

    bundle = tmp_path / "historical.sqlite3"
    symbols = "AAPL,MSFT,SPY"
    keys = [
        (f"price::6mo::2026-01-01::2026-02-01::{symbols}", "price"),
        (f"closes::6mo::2026-01-01::2026-02-01::{symbols}", "closes"),
    ]
    digest = _create_bundle(bundle, keys)

    validated = validate_historical_data_bundle(
        bundle, digest, ["MSFT", "AAPL"], "SPY", "2026-01-01", "2026-02-01"
    )
    copied = copy_validated_data_bundle(validated, tmp_path / "private")
    assert copied.read_bytes() == bundle.read_bytes()
    assert copied.resolve() != bundle.resolve()
    with pytest.raises(DataBundleError, match="SHA-256"):
        validate_historical_data_bundle(
            bundle, "0" * 64, ["MSFT", "AAPL"], "SPY", "2026-01-01", "2026-02-01"
        )
    with pytest.raises(DataBundleError, match="coverage"):
        validate_historical_data_bundle(
            bundle, digest, ["NVDA"], "SPY", "2026-01-01", "2026-02-01"
        )


def test_backtest_gate_copies_approved_bundle_and_fails_closed_on_missing_sentinel(tmp_path: Path) -> None:
    """Break caught: process success without trusted SimulationResult metrics could pass the gate."""
    from agent_loop import (
        BACKTEST_SENTINEL,
        BacktestThresholds,
        CompletionEnvelope,
        ProcessResult,
        WorkerObservation,
        export_candidate,
        preflight_source,
        run_backtest_gate,
        validate_historical_data_bundle,
    )

    repo = _task2_repo(tmp_path)
    (repo / "agent_loop.py").write_text("# trusted hidden worker\n", encoding="utf-8")
    _run_git(repo, "add", "agent_loop.py")
    _run_git(repo, "commit", "-m", "worker")
    candidate = export_candidate(preflight_source(repo, acquire_lock=False))
    bundle = tmp_path / "historical.sqlite3"
    symbols = "AAPL,SPY"
    digest = _create_bundle(
        bundle,
        [
            (f"price::6mo::2026-01-01::2026-02-01::{symbols}", "price"),
            (f"closes::6mo::2026-01-01::2026-02-01::{symbols}", "closes"),
        ],
    )
    approved = validate_historical_data_bundle(
        bundle, digest, ["AAPL"], "SPY", "2026-01-01", "2026-02-01"
    )
    metrics = {
        "total_return_pct": 10.0,
        "annualized_return_pct": 8.0,
        "sharpe_ratio": 1.0,
        "max_drawdown_pct": -5.0,
        "closed_trades": 3,
    }

    class FakeSandbox:
        def __init__(self, output: str) -> None:
            self.output = output
            self.argv: tuple[str, ...] | None = None

        def run_worker(
            self,
            worker: Any,
            argv: tuple[str, ...],
            environment: dict[str, str],
            data_bundle: Any = None,
        ) -> WorkerObservation:
            self.argv = argv
            assert data_bundle.path.read_bytes() == bundle.read_bytes()
            assert environment["BACKTEST_DATA_CACHE_DB_PATH"] == "/workspace/tmp/backtest-cache/historical_data.sqlite3"
            digest_index = argv.index("--historical-data-sha256") + 1
            assert argv[digest_index] == data_bundle.sha256
            return WorkerObservation(
                ProcessResult.ok(self.output),
                CompletionEnvelope({"worker_confined": False}, "0" * 64),
            )

    passing = FakeSandbox(BACKTEST_SENTINEL + json.dumps(metrics) + "\n")
    result = run_backtest_gate(
        candidate,
        passing,  # type: ignore[arg-type]
        approved,
        ["AAPL"],
        "SPY",
        "2026-01-01",
        "2026-02-01",
        BacktestThresholds(10.0, 8.0, 1.0, 5.0, 3),
    )
    assert result.gate_observation
    assert passing.argv is not None and "--technical-only" in passing.argv

    missing = FakeSandbox("ordinary output only\n")
    failed = run_backtest_gate(
        candidate,
        missing,  # type: ignore[arg-type]
        approved,
        ["AAPL"],
        "SPY",
        "2026-01-01",
        "2026-02-01",
        BacktestThresholds(10.0, 8.0, 1.0, 5.0, 3),
    )
    assert not failed.gate_observation
    assert failed.evaluation.failures == ("sentinel",)


@pytest.mark.parametrize(
    ("metrics", "expected"),
    [
        ({"total_return_pct": 10.0, "annualized_return_pct": 8.0, "sharpe_ratio": 1.0, "max_drawdown_pct": -5.0, "closed_trades": 3}, True),
        ({"total_return_pct": 9.999, "annualized_return_pct": 8.0, "sharpe_ratio": 1.0, "max_drawdown_pct": -5.0, "closed_trades": 3}, False),
        ({"total_return_pct": 10.0, "annualized_return_pct": 8.0, "sharpe_ratio": 1.0, "max_drawdown_pct": -5.001, "closed_trades": 3}, False),
    ],
)
def test_backtest_threshold_boundaries_are_deterministic(metrics: dict[str, float], expected: bool) -> None:
    """Break caught: an LLM or an off-by-one comparison could decide a metrics gate."""
    from agent_loop import BacktestThresholds, evaluate_backtest_metrics

    thresholds = BacktestThresholds(10.0, 8.0, 1.0, 5.0, 3)
    assert evaluate_backtest_metrics(metrics, thresholds).thresholds_met_observation is expected


def test_backtest_gate_hidden_worker_uses_exact_tickers_and_neutralizes_extra_symbols(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Break caught: hidden settings/S&P expansion could widen the operator-approved offline universe."""
    from agent_loop import BACKTEST_SENTINEL, run_hidden_backtest_worker

    settings = SimpleNamespace(EXTRA_SYMBOLS=["SECRET"], BACKTEST_DATA_CACHE_DB_PATH="wrong")
    observed: dict[str, object] = {}
    result = SimpleNamespace(
        total_return_pct=1.0,
        annualized_return_pct=2.0,
        sharpe_ratio=3.0,
        max_drawdown_pct=-4.0,
        closed_trades=[object(), object()],
    )

    def run_cli(argv: list[str]) -> object:
        observed["argv"] = argv
        return result

    engine = SimpleNamespace(run_cli=run_cli, get_sp500_tickers=lambda: ["WIDENED"])
    monkeypatch.setitem(sys.modules, "config", SimpleNamespace(settings=settings))
    monkeypatch.setitem(sys.modules, "core", SimpleNamespace(backtest_engine=engine))
    bundle = tmp_path / "private.sqlite3"
    bundle.write_bytes(b"private")
    digest = hashlib.sha256(b"private").hexdigest()
    scratch = tmp_path / "scratch" / "historical_data.sqlite3"
    candidate_source = tmp_path / "candidate-source"
    candidate_source.mkdir()
    monkeypatch.setattr(sys, "path", list(sys.path))

    assert run_hidden_backtest_worker(
        tickers=["MSFT", "AAPL", "MSFT"],
        benchmark="SPY",
        start_date="2026-01-01",
        end_date="2026-02-01",
        bundle_path=bundle,
        expected_sha256=digest,
        scratch_path=scratch,
        candidate_source_root=candidate_source,
    ) == 0

    assert settings.EXTRA_SYMBOLS == []
    assert settings.BACKTEST_DATA_CACHE_DB_PATH == str(scratch.resolve())
    assert sys.path[0] == str(candidate_source.resolve())
    assert engine.get_sp500_tickers() == ["MSFT", "AAPL", "SPY"]
    from agent_loop import GateConfigurationError

    for attribute in ("_download_price_data", "_download_bulk_closes", "fetch_bulk_ohlcv"):
        with pytest.raises(GateConfigurationError, match="cache miss"):
            getattr(engine, attribute)(["AAPL"])
    assert observed["argv"] == [
        "--tickers", "MSFT", "AAPL", "--start-date", "2026-01-01", "--end-date", "2026-02-01",
        "--benchmark", "SPY", "--technical-only", "--no-csv",
    ]
    sentinel_lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith(BACKTEST_SENTINEL)]
    assert len(sentinel_lines) == 1


def test_fixed_test_gate_accepts_only_tracked_tests_selectors(tmp_path: Path) -> None:
    """Break caught: a selector beginning with an option could inject an arbitrary pytest command."""
    from agent_loop import GateConfigurationError, build_test_gate_argv

    repo = _task2_repo(tmp_path)
    assert build_test_gate_argv(repo, ["tests/test_safe.py"]) == (
        "-m", "pytest", "-p", "no:cacheprovider", "--no-cov", "-q", "-m", "not integration", "tests/test_safe.py"
    )
    with pytest.raises(GateConfigurationError):
        build_test_gate_argv(repo, ["--collect-only"])
    with pytest.raises(GateConfigurationError):
        build_test_gate_argv(repo, ["tests/not-tracked.py"])


def test_patch_policy_ast_rejects_multiline_core_live_import_alias(tmp_path: Path) -> None:
    """Break caught: line-wise regex misses ``from core import (...)`` live aliases."""
    from agent_loop import PatchPolicyError, validate_unified_diff

    repo = _task2_repo(tmp_path)
    patch = (
        "diff --git a/core/backtest_engine.py b/core/backtest_engine.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/core/backtest_engine.py\n"
        "+++ b/core/backtest_engine.py\n"
        "@@ -1,1 +1,3 @@\n"
        "-VALUE = 1\n"
        "+from core import (\n"
        "+    order_execution as execution,\n"
        "+)\n"
    )

    with pytest.raises(PatchPolicyError, match="live"):
        validate_unified_diff(repo, patch, ["core/backtest_engine.py"])


def test_patch_application_allows_same_file_full_revert(tmp_path: Path) -> None:
    """Break caught: a later proposal cannot fully undo an earlier candidate-only iteration."""
    from agent_loop import CodingProposal, apply_candidate_patch, export_candidate, preflight_source

    repo = _task2_repo(tmp_path)
    candidate = export_candidate(preflight_source(repo, acquire_lock=False))
    apply_candidate_patch(
        candidate,
        CodingProposal("forward", ("core/backtest_engine.py",), _task2_diff()),
        compile_runner=lambda _root, _paths: True,
    )
    apply_candidate_patch(
        candidate,
        CodingProposal(
            "revert",
            ("core/backtest_engine.py",),
            _task2_diff(old="VALUE = 2", new="VALUE = 1"),
        ),
        compile_runner=lambda _root, _paths: True,
    )

    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=candidate.root, check=True, capture_output=True, text=True
    ).stdout == ""


def test_git_subprocess_environment_is_minimal_and_disables_extension_points(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: Git inherits provider keys, credentials, proxies, hooks, fsmonitor, or external diff."""
    import agent_loop

    captured: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-reach-git")
    monkeypatch.setenv("GIT_DIR", "attacker")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "99")
    monkeypatch.setenv("HTTPS_PROXY", "http://credential-proxy.invalid")
    monkeypatch.setattr(agent_loop.subprocess, "run", fake_run)

    agent_loop._git(tmp_path, "status", "--porcelain")

    environment = captured["env"]
    assert isinstance(environment, dict)
    for forbidden in (
        "OPENROUTER_API_KEY", "GIT_DIR", "GIT_CONFIG_COUNT", "HTTPS_PROXY", "GIT_ASKPASS",
        "SSH_ASKPASS", "GIT_EXTERNAL_DIFF",
    ):
        assert forbidden not in environment or environment[forbidden] in {"", "0"}
    command = captured["argv"]
    assert isinstance(command, list)
    assert "core.hooksPath=NUL" in command or "core.hooksPath=/dev/null" in command
    assert "core.fsmonitor=false" in command
    assert "diff.external=" in command


def test_quarantine_force_tracks_commit_files_even_when_ignore_rule_matches(tmp_path: Path) -> None:
    """Break caught: a tracked-but-ignored protected input silently disappears from candidate provenance."""
    from agent_loop import export_candidate, preflight_source

    repo = _task2_repo(tmp_path)
    (repo / "ignored.txt").write_text("tracked oracle\n", encoding="utf-8", newline="\n")
    _run_git(repo, "add", "-f", "ignored.txt")
    _run_git(repo, "commit", "-m", "track ignored oracle")

    candidate = export_candidate(preflight_source(repo, acquire_lock=False))

    assert (candidate.root / "ignored.txt").read_text(encoding="utf-8") == "tracked oracle\n"
    assert "ignored.txt" in subprocess.run(
        ["git", "ls-files"], cwd=candidate.root, check=True, capture_output=True, text=True
    ).stdout.splitlines()


def test_unsafe_local_commit_export_ignores_mutable_worktree_bytes(tmp_path: Path) -> None:
    """Break caught: unsafe-local baseline races preflight and executes mutable checkout bytes."""
    from agent_loop import preflight_source, run_source_commit_in_disposable_worker

    repo = _task2_repo(tmp_path)
    state = preflight_source(repo, acquire_lock=False)
    (repo / "core" / "backtest_engine.py").write_text("VALUE = 999\n", encoding="utf-8", newline="\n")

    observed = run_source_commit_in_disposable_worker(
        state,
        lambda layout: (layout.source / "core" / "backtest_engine.py").read_text(encoding="utf-8"),
    )

    assert observed == "VALUE = 1\n"


def test_data_bundle_is_streamed_once_to_immutable_controller_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: trusted validation hashes one DB but later mounts mutable source bytes."""
    from agent_loop import validate_historical_data_bundle

    bundle = tmp_path / "operator.sqlite3"
    symbols = "AAPL,SPY"
    digest = _create_bundle(
        bundle,
        [
            (f"price::6mo::2026-01-01::2026-02-01::{symbols}", "price"),
            (f"closes::6mo::2026-01-01::2026-02-01::{symbols}", "closes"),
        ],
    )
    original_read_bytes = Path.read_bytes

    def forbid_whole_file_read(path: Path) -> bytes:
        if path == bundle:
            raise AssertionError("bundle hashing must stream")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", forbid_whole_file_read)
    validated = validate_historical_data_bundle(
        bundle, digest, ["AAPL"], "SPY", "2026-01-01", "2026-02-01"
    )
    monkeypatch.setattr(Path, "read_bytes", original_read_bytes)
    bundle.write_bytes(b"mutated after approval")

    assert validated.path != bundle.resolve()
    assert validated.path.read_bytes().startswith(b"SQLite format 3\x00")
    assert validated.path.stat().st_mode & stat.S_IWUSR == 0


def test_hidden_backtest_argv_grammar_rejects_reordering_duplicates_and_unknown_values(tmp_path: Path) -> None:
    """Break caught: option allowlisting still accepts ambiguous hidden-worker command grammar."""
    from agent_loop import SandboxError, SandboxRunner

    worker = tmp_path / "worker"
    worker.mkdir()
    (worker / "agent_loop.py").write_text("# gate\n", encoding="utf-8")
    malformed = (
        "agent_loop.py", "--_hidden-backtest", "--benchmark", "SPY", "--tickers", "AAPL", "AAPL",
        "--start-date", "2026-01-01", "--end-date", "2026-02-01", "--historical-data-bundle",
        "/workspace/data/historical_data.sqlite3", "--technical-only", "--no-csv",
    )

    with pytest.raises(SandboxError, match="grammar"):
        SandboxRunner._validate_python_args(worker, malformed)


def test_candidate_apply_api_rejects_source_checkout_paths(tmp_path: Path) -> None:
    """Break caught: candidate-only ``--apply`` can be pointed at the source checkout."""
    from agent_loop import CodingProposal, ConfigurationError, apply_candidate_patch

    source = _task2_repo(tmp_path)
    before = (source / "core" / "backtest_engine.py").read_bytes()

    with pytest.raises(ConfigurationError, match="candidate"):
        apply_candidate_patch(
            source,  # type: ignore[arg-type]
            CodingProposal("must stay inert", ("core/backtest_engine.py",), _task2_diff()),
            compile_runner=lambda _root, _paths: True,
        )
    assert (source / "core" / "backtest_engine.py").read_bytes() == before


def test_execution_mode_has_no_promotion_surface_and_gate_results_are_observational() -> None:
    """Break caught: a worker outcome is exposed as verification or source-mutation authorization."""
    from agent_loop import ExecutionMode, GateResult

    with pytest.raises(TypeError):
        ExecutionMode(promote=True)  # type: ignore[call-arg]
    result = GateResult(
        gate_observation=True,
        observed_exit_zero=True,
        worker_confined=False,
        source_modified=False,
        security_attestation=False,
        returncode=0,
        stdout="spoofable",
        stderr="",
        stdout_sha256="a" * 64,
        stderr_sha256="b" * 64,
        completion_envelope=None,
    )
    assert not hasattr(result, "passed")
    assert result.gate_observation
    assert result.source_modified is False
    assert result.security_attestation is False


def test_worker_dockerfile_uses_fixed_numeric_identity_without_build_args() -> None:
    """Break caught: image build arguments change the runtime UID/GID outside the inspected contract."""
    dockerfile = (Path(__file__).parents[1] / "Dockerfile.agent-loop").read_text(encoding="utf-8")

    assert "ARG AGENT_UID" not in dockerfile
    assert "ARG AGENT_GID" not in dockerfile
    assert "groupadd --gid 65532" in dockerfile
    assert "useradd --uid 65532 --gid 65532" in dockerfile


def _process_result(returncode: int, stdout: str = "", stderr: str = "", *, timed_out: bool = False):
    import hashlib

    from agent_loop import ProcessResult

    return ProcessResult(
        returncode,
        stdout,
        stderr,
        hashlib.sha256(stdout.encode()).hexdigest(),
        hashlib.sha256(stderr.encode()).hexdigest(),
        timed_out,
    )


class FaithfulSandboxEngine:
    """Faithful Docker-shaped process boundary; never production provenance."""

    def __init__(self, image: str) -> None:
        self.image = image
        self.image_id = "sha256:" + "b" * 64
        self.container_id = "c" * 64
        self.calls: list[tuple[str, ...]] = []
        self.call_kwargs: list[dict[str, object]] = []
        self.created = False
        self.started = False
        self.removed = False
        self.absence_verified = False
        self.malformed_create_output = False
        self.raise_after_create = False
        self.cleanup_fails = False
        self.oom_killed = False
        self.mutate_inspection: Any = None
        self.mutate_terminal_state: Any = None
        self.cleanup_inspect_error = False
        self.mutate_data_on_start = False
        self.name = ""
        self.inspect_payload: dict[str, object] = {}

    @staticmethod
    def _option(argv: tuple[str, ...], name: str) -> str:
        return argv[argv.index(name) + 1]

    def __call__(self, argv: tuple[str, ...], **_kwargs: object):
        self.calls.append(argv)
        self.call_kwargs.append(dict(_kwargs))
        command = argv[1] if len(argv) > 1 else ""
        if command == "version":
            return _process_result(0, '{"Server":{"Version":"fake"}}')
        if argv[1:3] == ("image", "inspect"):
            return _process_result(
                0,
                json.dumps(
                    [{
                        "Id": self.image_id,
                        "RepoDigests": [self.image],
                        "Config": {"Env": [
                            "PATH=/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                            "GPG_KEY=7169605F62C751356D054A26A821E680E5FA6305",
                            "PYTHON_VERSION=3.13.14",
                            "PYTHON_SHA256=639e43243c620a308f968213df9e00f2f8f62332f7adbaa7a7eeb9783057c690",
                        ]},
                    }]
                ),
            )
        if command == "create":
            self.created = True
            self.removed = False
            self.absence_verified = False
            self.name = self._option(argv, "--name")
            mounts: list[dict[str, object]] = []
            for index, value in enumerate(argv):
                if value != "--mount":
                    continue
                fields = argv[index + 1].split(",")
                parsed: dict[str, str] = {}
                readonly = False
                for field in fields:
                    if field == "readonly":
                        readonly = True
                    elif "=" in field:
                        key, item = field.split("=", 1)
                        parsed[key] = item
                mounts.append({
                    "Type": parsed.get("type"),
                    "Source": str(Path(parsed["src"]).resolve()),
                    "Destination": parsed["dst"],
                    "RW": not readonly,
                    "Mode": "",
                    "Propagation": "rprivate",
                })
            explicit_env: dict[str, str] = {}
            for index, value in enumerate(argv):
                if value == "--env":
                    key, item = argv[index + 1].split("=", 1)
                    explicit_env[key] = item
            environment = {
                "PATH": "/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "GPG_KEY": "7169605F62C751356D054A26A821E680E5FA6305",
                "PYTHON_VERSION": "3.13.14",
                "PYTHON_SHA256": "639e43243c620a308f968213df9e00f2f8f62332f7adbaa7a7eeb9783057c690",
            }
            environment.update(explicit_env)
            image_index = argv.index(self.image_id)
            self.inspect_payload = {
                "Id": self.container_id,
                "Name": "/" + self.name,
                "Image": self.image_id,
                "Config": {
                    "User": "65532:65532",
                    "Entrypoint": ["python"],
                    "WorkingDir": "/workspace/src",
                    "Cmd": list(argv[image_index + 1 :]),
                    "Env": [f"{key}={value}" for key, value in sorted(environment.items())],
                },
                "HostConfig": {
                    "NetworkMode": "none",
                    "ReadonlyRootfs": True,
                    "CapDrop": ["ALL"],
                    "CapAdd": [],
                    "Privileged": False,
                    "SecurityOpt": ["no-new-privileges"],
                    "PidsLimit": 64,
                    "Memory": 1073741824,
                    "NanoCpus": 1000000000,
                    "Devices": [],
                    "DeviceRequests": [],
                    "IpcMode": "private",
                    "PidMode": "",
                    "UTSMode": "",
                    "CgroupnsMode": "private",
                    "CgroupParent": "",
                    "PortBindings": {},
                    "PublishAllPorts": False,
                },
                "Mounts": mounts,
                "NetworkSettings": {"Ports": {}},
                "State": {
                    "OOMKilled": False,
                    "Status": "created",
                    "Running": False,
                    "Paused": False,
                    "Restarting": False,
                    "Dead": False,
                    "ExitCode": 0,
                },
            }
            output = "malformed\n" if self.malformed_create_output else self.container_id + "\n"
            if self.raise_after_create:
                raise OSError("injected create transport failure")
            return _process_result(0, output)
        if command == "inspect":
            if self.removed:
                if self.cleanup_inspect_error:
                    return _process_result(1, stderr="permission denied")
                self.absence_verified = True
                return _process_result(1, stderr="No such container")
            payload = json.loads(json.dumps(self.inspect_payload))
            payload["State"] = {
                "OOMKilled": self.oom_killed,
                "Status": "exited" if self.started else "created",
                "Running": False,
                "Paused": False,
                "Restarting": False,
                "Dead": False,
                "ExitCode": 137 if self.oom_killed else 0,
            }
            if self.mutate_inspection is not None:
                self.mutate_inspection(payload)
            if self.started and self.mutate_terminal_state is not None:
                self.mutate_terminal_state(payload["State"])
            return _process_result(0, json.dumps([payload]))
        if command == "start":
            self.started = True
            if self.mutate_data_on_start:
                data_mount = next(
                    item for item in self.inspect_payload["Mounts"]
                    if item["Destination"] == "/workspace/data/historical_data.sqlite3"
                )
                data_path = Path(data_mount["Source"])
                data_path.chmod(stat.S_IWRITE)
                with data_path.open("ab") as stream:
                    stream.write(b"tampered")
            return _process_result(137 if self.oom_killed else 0, "candidate says success\n")
        if command == "rm":
            if self.cleanup_fails:
                return _process_result(1, stderr="cleanup failed")
            self.removed = True
            return _process_result(0)
        if argv[1:3] == ("container", "ls"):
            if self.cleanup_inspect_error:
                return _process_result(1, stderr="permission denied")
            self.absence_verified = self.removed
            return _process_result(0, "" if self.removed else self.container_id + "\n")
        raise AssertionError(f"unexpected fake engine command: {argv}")


def _faithful_runner(image: str, engine: FaithfulSandboxEngine):
    from agent_loop import SandboxRunner

    return SandboxRunner(
        engine_path=Path("relative/fake/docker.exe"),
        image=image,
        process_runner=engine,
        run_id="run-1234567890abcdef",
    )


def test_worker_completion_envelope_is_host_sealed_chained_and_observational(tmp_path: Path) -> None:
    """Break caught: candidate stdout can forge an authoritative same-interpreter gate outcome."""
    from agent_loop import export_candidate, preflight_source, run_test_gate

    source = _task2_repo(tmp_path)
    candidate = export_candidate(preflight_source(source, acquire_lock=False))
    image = "registry.invalid/agent-loop@sha256:" + "a" * 64
    engine = FaithfulSandboxEngine(image)
    runner = _faithful_runner(image, engine)

    first = run_test_gate(candidate, runner)
    second = run_test_gate(candidate, runner)

    assert first.gate_observation and first.observed_exit_zero
    assert first.worker_confined is False  # injected backend is never production provenance
    assert first.source_modified is False
    assert first.security_attestation is False
    assert runner.verify_completion_envelope(first.completion_envelope)
    payload = first.completion_envelope.payload
    for key in (
        "nonce", "run_id", "image_repository_digest", "image_id", "container_id",
        "container_config_sha256", "candidate_manifest_sha256", "data_sha256",
        "environment_policy_sha256", "argv", "started_at_ns", "deadline_ns", "ended_at_ns",
        "returncode", "timed_out", "oom_killed", "stdout_sha256", "stderr_sha256",
        "cleanup_verified", "gate_observation", "worker_confined", "source_modified",
        "security_attestation",
    ):
        assert key in payload
    assert payload["argv"] == [
        "-m", "pytest", "-p", "no:cacheprovider", "--no-cov", "-q", "-m", "not integration"
    ]
    assert second.completion_envelope.payload["previous_hmac_sha256"] == first.completion_envelope.hmac_sha256
    assert first.completion_envelope.hmac_sha256 not in first.stdout


def test_worker_mounts_candidate_gate_and_data_read_only_with_narrow_writable_dirs(tmp_path: Path) -> None:
    """Break caught: candidate/protected inputs share a writable bind and can rewrite their own evidence."""
    from agent_loop import export_candidate, preflight_source, run_test_gate

    source = _task2_repo(tmp_path)
    candidate = export_candidate(preflight_source(source, acquire_lock=False))
    image = "registry.invalid/agent-loop@sha256:" + "a" * 64
    engine = FaithfulSandboxEngine(image)
    run_test_gate(candidate, _faithful_runner(image, engine))
    create = next(call for call in engine.calls if call[1] == "create")
    mount_specs = [create[index + 1] for index, value in enumerate(create) if value == "--mount"]

    assert len(mount_specs) == 6
    for destination in ("/workspace/src", "/workspace/gate", "/workspace/data"):
        spec = next(value for value in mount_specs if f"dst={destination}" in value)
        assert "readonly" in spec
    for destination in ("/workspace/tmp", "/workspace/home", "/workspace/output"):
        spec = next(value for value in mount_specs if f"dst={destination}" in value)
        assert "readonly" not in spec


@pytest.mark.parametrize(
    "mutator",
    [
        lambda item: item["HostConfig"].update(Privileged=True),
        lambda item: item["HostConfig"].update(CapAdd=["SYS_ADMIN"]),
        lambda item: item["HostConfig"].update(Devices=[{"PathOnHost": "C:/"}]),
        lambda item: item["HostConfig"].update(DeviceRequests=[{"Count": -1}]),
        lambda item: item["HostConfig"].update(SecurityOpt=["no-new-privileges", "seccomp=unconfined"]),
        lambda item: item["Config"].update(Env=[*item["Config"]["Env"], "SECRET=inherited"]),
        lambda item: item["Config"].update(WorkingDir="/workspace"),
        lambda item: item["Config"].update(Cmd=["-c", "print('forged')"]),
        lambda item: item["Mounts"][0].update(RW=True, Mode="rw"),
        lambda item: item["Mounts"][0].update(Type="volume"),
    ],
)
def test_container_attestation_rejects_every_privilege_env_command_and_mount_weakening(
    tmp_path: Path,
    mutator: Any,
) -> None:
    """Break caught: a missing/extra inspect field silently weakens the controller-built contract."""
    from agent_loop import SandboxError, export_candidate, preflight_source, run_test_gate

    source = _task2_repo(tmp_path)
    candidate = export_candidate(preflight_source(source, acquire_lock=False))
    image = "registry.invalid/agent-loop@sha256:" + "a" * 64
    engine = FaithfulSandboxEngine(image)
    engine.mutate_inspection = mutator

    with pytest.raises(SandboxError):
        run_test_gate(candidate, _faithful_runner(image, engine))
    assert engine.removed and engine.absence_verified


def test_engine_path_is_resolved_once_and_every_call_uses_canonical_absolute_path(tmp_path: Path) -> None:
    """Break caught: PATH/cwd replacement swaps the inspected engine executable between calls."""
    from agent_loop import export_candidate, preflight_source, run_test_gate

    source = _task2_repo(tmp_path)
    candidate = export_candidate(preflight_source(source, acquire_lock=False))
    image = "registry.invalid/agent-loop@sha256:" + "a" * 64
    engine = FaithfulSandboxEngine(image)
    run_test_gate(candidate, _faithful_runner(image, engine))

    paths = {Path(call[0]) for call in engine.calls}
    assert len(paths) == 1
    assert next(iter(paths)).is_absolute()


def test_malformed_create_output_still_cleans_deterministic_name_and_verifies_absence(tmp_path: Path) -> None:
    """Break caught: malformed create output strands a running/created container outside cleanup."""
    from agent_loop import SandboxError, export_candidate, preflight_source, run_test_gate

    source = _task2_repo(tmp_path)
    candidate = export_candidate(preflight_source(source, acquire_lock=False))
    image = "registry.invalid/agent-loop@sha256:" + "a" * 64
    engine = FaithfulSandboxEngine(image)
    engine.malformed_create_output = True

    with pytest.raises(SandboxError):
        run_test_gate(candidate, _faithful_runner(image, engine))
    assert engine.name == "agent-loop-run-1234567890a-000001"
    assert engine.removed and engine.absence_verified


def test_create_transport_failure_still_cleans_deterministic_name(tmp_path: Path) -> None:
    """Break caught: engine failure after create bypasses the named-container cleanup finally path."""
    from agent_loop import export_candidate, preflight_source, run_test_gate

    source = _task2_repo(tmp_path)
    candidate = export_candidate(preflight_source(source, acquire_lock=False))
    image = "registry.invalid/agent-loop@sha256:" + "a" * 64
    engine = FaithfulSandboxEngine(image)
    engine.raise_after_create = True

    with pytest.raises(OSError, match="transport"):
        run_test_gate(candidate, _faithful_runner(image, engine))
    assert engine.name == "agent-loop-run-1234567890a-000001"
    assert engine.removed and engine.absence_verified


def test_cleanup_failure_is_fatal_and_never_returns_observation(tmp_path: Path) -> None:
    """Break caught: successful stdout is returned while the worker container remains live."""
    from agent_loop import SandboxError, export_candidate, preflight_source, run_test_gate

    source = _task2_repo(tmp_path)
    candidate = export_candidate(preflight_source(source, acquire_lock=False))
    image = "registry.invalid/agent-loop@sha256:" + "a" * 64
    engine = FaithfulSandboxEngine(image)
    engine.cleanup_fails = True

    with pytest.raises(SandboxError, match="cleanup"):
        run_test_gate(candidate, _faithful_runner(image, engine))


def test_source_completion_recheck_reports_external_change_without_restoring_it(tmp_path: Path) -> None:
    """Break caught: source completion check cleans or overwrites a concurrent user edit."""
    from agent_loop import preflight_source, recheck_source_unchanged

    source = _task2_repo(tmp_path)
    state = preflight_source(source, acquire_lock=False)
    clean = recheck_source_unchanged(state)
    assert clean.source_modified is False
    concurrent = source / "core" / "backtest_engine.py"
    concurrent.write_text("CONCURRENT = True\n", encoding="utf-8", newline="\n")

    changed = recheck_source_unchanged(state)

    assert changed.source_modified is True
    assert concurrent.read_text(encoding="utf-8") == "CONCURRENT = True\n"


def test_data_bundle_rejects_sidecars_size_overflow_and_post_run_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: WAL/SHM, oversized, or daemon-mutated DB bytes escape approved snapshot hashing."""
    import agent_loop
    from agent_loop import (
        BacktestThresholds,
        DataBundleError,
        SandboxError,
        export_candidate,
        preflight_source,
        run_backtest_gate,
        validate_historical_data_bundle,
    )

    source = _task2_repo(tmp_path)
    (source / "agent_loop.py").write_text("# hidden gate\n", encoding="utf-8", newline="\n")
    _run_git(source, "add", "agent_loop.py")
    _run_git(source, "commit", "-m", "gate")
    candidate = export_candidate(preflight_source(source, acquire_lock=False))
    bundle = tmp_path / "operator.sqlite3"
    symbols = "AAPL,SPY"
    keys = [
        (f"price::6mo::2026-01-01::2026-02-01::{symbols}", "price"),
        (f"closes::6mo::2026-01-01::2026-02-01::{symbols}", "closes"),
    ]
    digest = _create_bundle(bundle, keys)
    sidecar = Path(str(bundle) + "-wal")
    sidecar.write_bytes(b"unapproved")
    with pytest.raises(DataBundleError, match="sidecar"):
        validate_historical_data_bundle(bundle, digest, ["AAPL"], "SPY", "2026-01-01", "2026-02-01")
    sidecar.unlink()

    monkeypatch.setattr(agent_loop, "_MAX_DATA_BUNDLE_BYTES", 16)
    with pytest.raises(DataBundleError, match="size"):
        validate_historical_data_bundle(bundle, digest, ["AAPL"], "SPY", "2026-01-01", "2026-02-01")
    monkeypatch.setattr(agent_loop, "_MAX_DATA_BUNDLE_BYTES", 8 * 1024 * 1024 * 1024)
    approved = validate_historical_data_bundle(
        bundle, digest, ["AAPL"], "SPY", "2026-01-01", "2026-02-01"
    )
    image = "registry.invalid/agent-loop@sha256:" + "a" * 64
    engine = FaithfulSandboxEngine(image)
    engine.mutate_data_on_start = True

    with pytest.raises(SandboxError, match="data"):
        run_backtest_gate(
            candidate,
            _faithful_runner(image, engine),
            approved,
            ["AAPL"],
            "SPY",
            "2026-01-01",
            "2026-02-01",
            BacktestThresholds(0.0, 0.0, 0.0, 100.0, 0),
        )


def test_engine_cli_receives_only_minimal_controller_owned_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: Docker/Podman inherits provider, broker, cloud, proxy, Git, or credential state."""
    from agent_loop import export_candidate, preflight_source, run_test_gate

    source = _task2_repo(tmp_path)
    for name in (
        "OPENROUTER_API_KEY", "ALPACA_API_KEY", "FMP_API_KEY", "AWS_SECRET_ACCESS_KEY",
        "HTTPS_PROXY", "GIT_DIR", "GIT_ASKPASS", "SSH_AUTH_SOCK",
    ):
        monkeypatch.setenv(name, "must-not-reach-engine")
    candidate = export_candidate(preflight_source(source, acquire_lock=False))
    image = "registry.invalid/agent-loop@sha256:" + "a" * 64
    engine = FaithfulSandboxEngine(image)

    run_test_gate(candidate, _faithful_runner(image, engine))

    assert engine.call_kwargs
    for kwargs in engine.call_kwargs:
        environment = kwargs.get("env")
        assert isinstance(environment, dict)
        assert not {
            "OPENROUTER_API_KEY", "ALPACA_API_KEY", "FMP_API_KEY", "AWS_SECRET_ACCESS_KEY",
            "HTTPS_PROXY", "GIT_DIR", "GIT_ASKPASS", "SSH_AUTH_SOCK",
        } & set(environment)
        assert Path(environment["DOCKER_CONFIG"]).parts[-2:] == (".engine-control", "config")
        assert Path(environment["HOME"]).parts[-2:] == (".engine-control", "home")


def test_private_tree_cleanup_does_not_follow_hostile_symlink(tmp_path: Path) -> None:
    """Break caught: cleanup chmod follows a candidate-created link and changes an outside target."""
    import os

    from agent_loop import _remove_private_tree

    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside must remain exact")
    outside.chmod(0o444)
    before = outside.stat().st_mode
    worker = tmp_path / "worker"
    output = worker / "output"
    output.mkdir(parents=True)
    try:
        os.symlink(outside, output / "hostile-link")
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    _remove_private_tree(worker)

    assert not worker.exists()
    assert outside.read_bytes() == b"outside must remain exact"
    assert outside.stat().st_mode == before


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction behavior")
def test_private_tree_cleanup_does_not_follow_hostile_junction(tmp_path: Path) -> None:
    """Break caught: cleanup chmod/traversal follows a candidate-created junction outside its exact root."""
    from agent_loop import _remove_private_tree

    outside = tmp_path / "outside-dir"
    outside.mkdir()
    protected = outside / "protected.txt"
    protected.write_bytes(b"outside junction target")
    protected.chmod(stat.S_IREAD)
    before = protected.stat().st_mode
    worker = tmp_path / "worker"
    output = worker / "output"
    output.mkdir(parents=True)
    junction = output / "hostile-junction"
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        check=False, capture_output=True, text=True,
    )
    if created.returncode != 0:
        pytest.skip(f"junction creation unavailable: {created.stderr}")

    _remove_private_tree(worker)

    assert not worker.exists()
    assert protected.read_bytes() == b"outside junction target"
    assert protected.stat().st_mode == before


def test_controller_temp_failure_never_falls_back_inside_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: failed system temp creation falls back to cwd inside the source checkout."""
    import agent_loop
    from agent_loop import QuarantineError, preflight_source, run_source_commit_in_disposable_worker

    source = _task2_repo(tmp_path)
    monkeypatch.chdir(source)
    monkeypatch.setattr(agent_loop.tempfile, "gettempdir", lambda: str(source))
    with pytest.raises(QuarantineError, match="outside"):
        state = preflight_source(source, acquire_lock=False)
        run_source_commit_in_disposable_worker(state, lambda _layout: True)
    assert not (source / ".controller-tmp").exists()
    assert not any(source.glob("agent-loop-*-*"))


@pytest.mark.parametrize(
    "statement",
    [
        "from . import order_execution",
        "from .order_execution import Broker as X",
        "from ..core import order_execution as execution",
        "from alpaca import trading as broker_trading",
    ],
)
def test_ast_policy_resolves_relative_and_parent_live_imports(tmp_path: Path, statement: str) -> None:
    """Break caught: relative or parent-package imports bypass live-execution AST policy."""
    from agent_loop import PatchPolicyError, validate_unified_diff

    repo = _task2_repo(tmp_path)
    patch = _task2_diff(new=statement)

    with pytest.raises(PatchPolicyError, match="live"):
        validate_unified_diff(repo, patch, ["core/backtest_engine.py"])


def test_hidden_backtest_argv_requires_gate_path_approved_input_and_exact_digest(tmp_path: Path) -> None:
    """Break caught: hidden argv can reorder fields, duplicate tickers, or omit approved-input digest."""
    from agent_loop import SandboxError, SandboxRunner

    worker = tmp_path / "worker"
    worker.mkdir()
    valid = (
        "/workspace/gate/agent_loop.py", "--_hidden-backtest", "--tickers", "AAPL",
        "--benchmark", "SPY", "--start-date", "2026-01-01", "--end-date", "2026-02-01",
        "--historical-data-bundle", "/workspace/data/historical_data.sqlite3",
        "--historical-data-sha256", "a" * 64, "--technical-only", "--no-csv",
    )
    SandboxRunner._validate_python_args(worker, valid)
    reordered = (*valid[:3], "AAPL", "AAPL", *valid[4:])
    with pytest.raises(SandboxError, match="grammar"):
        SandboxRunner._validate_python_args(worker, reordered)
    with pytest.raises(SandboxError, match="grammar"):
        SandboxRunner._validate_python_args(worker, valid[:-4] + valid[-2:])


def test_hidden_gate_streams_verified_approved_db_to_writable_scratch_before_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Break caught: candidate DataFetcher opens the immutable approved mount directly for writes."""
    import hashlib

    from agent_loop import run_hidden_backtest_worker

    approved = tmp_path / "approved.sqlite3"
    approved.write_bytes(b"approved bytes")
    approved.chmod(0o444)
    digest = hashlib.sha256(b"approved bytes").hexdigest()
    scratch = tmp_path / "tmp" / "historical_data.sqlite3"
    settings = SimpleNamespace(EXTRA_SYMBOLS=["SECRET"], BACKTEST_DATA_CACHE_DB_PATH="wrong")
    result = SimpleNamespace(
        total_return_pct=1.0, annualized_return_pct=2.0, sharpe_ratio=3.0,
        max_drawdown_pct=-4.0, closed_trades=[],
    )

    def run_cli(_argv: list[str]) -> object:
        assert settings.BACKTEST_DATA_CACHE_DB_PATH == str(scratch.resolve())
        assert scratch.read_bytes() == b"approved bytes"
        with scratch.open("ab") as stream:
            stream.write(b" writable")
        return result

    engine = SimpleNamespace(run_cli=run_cli, get_sp500_tickers=lambda: [])
    monkeypatch.setitem(sys.modules, "config", SimpleNamespace(settings=settings))
    monkeypatch.setitem(sys.modules, "core", SimpleNamespace(backtest_engine=engine))

    assert run_hidden_backtest_worker(
        tickers=["AAPL"], benchmark="SPY", start_date="2026-01-01", end_date="2026-02-01",
        bundle_path=approved, expected_sha256=digest, scratch_path=scratch,
        candidate_source_root=tmp_path,
    ) == 0
    assert approved.read_bytes() == b"approved bytes"
    assert scratch.read_bytes().endswith(b" writable")
    assert "AGENT_LOOP_BACKTEST_RESULT=" in capsys.readouterr().out


def test_real_data_fetcher_opens_verified_writable_scratch_without_network(tmp_path: Path) -> None:
    """Break caught: fixed-UID backtest points real DataFetcher at the read-only approved fixture."""
    import sqlite3

    from agent_loop import prepare_backtest_scratch_copy
    from core.backtest_engine import DataFetcher

    approved = tmp_path / "approved.sqlite3"
    digest = _create_bundle(approved, [])
    approved.chmod(0o444)
    scratch = tmp_path / "scratch" / "historical_data.sqlite3"
    prepare_backtest_scratch_copy(approved, digest, scratch)

    fetcher = DataFetcher(str(scratch))
    assert fetcher._db_available is True
    fetcher._store_cached("probe", "price", {"offline": True})
    with sqlite3.connect(scratch) as connection:
        assert connection.execute(
            "SELECT cache_kind FROM dataset_cache WHERE cache_key = 'probe'"
        ).fetchone() == ("price",)
    assert approved.stat().st_mode & stat.S_IWUSR == 0


@pytest.mark.parametrize(
    "mutator",
    [
        lambda item: item["HostConfig"].update(IpcMode="host"),
        lambda item: item["HostConfig"].update(PidMode="host"),
        lambda item: item["HostConfig"].update(UTSMode="host"),
        lambda item: item["HostConfig"].update(CgroupnsMode="host"),
        lambda item: item["HostConfig"].update(PortBindings={"80/tcp": [{"HostPort": "8080"}]}),
        lambda item: item.update(NetworkSettings={"Ports": {"80/tcp": [{"HostPort": "8080"}]}}),
    ],
)
def test_container_attestation_rejects_host_namespaces_cgroups_and_published_ports(
    tmp_path: Path,
    mutator: Any,
) -> None:
    """Break caught: host namespaces, host cgroup, or published ports weaken the inspected boundary."""
    from agent_loop import SandboxError, export_candidate, preflight_source, run_test_gate

    source = _task2_repo(tmp_path)
    candidate = export_candidate(preflight_source(source, acquire_lock=False))
    image = "registry.invalid/agent-loop@sha256:" + "a" * 64
    engine = FaithfulSandboxEngine(image)
    engine.mutate_inspection = mutator
    with pytest.raises(SandboxError):
        run_test_gate(candidate, _faithful_runner(image, engine))


@pytest.mark.parametrize(
    "mutator",
    [
        lambda state: state.update(Running=True),
        lambda state: state.update(Paused=True),
        lambda state: state.update(Restarting=True),
        lambda state: state.update(Dead=True),
        lambda state: state.update(ExitCode=9),
        lambda state: state.update(OOMKilled="false"),
    ],
)
def test_terminal_state_attestation_rejects_nonterminal_or_exit_mismatch(
    tmp_path: Path,
    mutator: Any,
) -> None:
    """Break caught: status=exited alone hides live flags, malformed OOM, or exit-code mismatch."""
    from agent_loop import SandboxError, export_candidate, preflight_source, run_test_gate

    source = _task2_repo(tmp_path)
    candidate = export_candidate(preflight_source(source, acquire_lock=False))
    image = "registry.invalid/agent-loop@sha256:" + "a" * 64
    engine = FaithfulSandboxEngine(image)
    engine.mutate_terminal_state = mutator
    with pytest.raises(SandboxError):
        run_test_gate(candidate, _faithful_runner(image, engine))


def test_cleanup_permission_error_is_not_authoritative_absence(tmp_path: Path) -> None:
    """Break caught: any nonzero inspect is treated as proof that cleanup removed the container."""
    from agent_loop import SandboxError, export_candidate, preflight_source, run_test_gate

    source = _task2_repo(tmp_path)
    candidate = export_candidate(preflight_source(source, acquire_lock=False))
    image = "registry.invalid/agent-loop@sha256:" + "a" * 64
    engine = FaithfulSandboxEngine(image)
    engine.cleanup_inspect_error = True
    with pytest.raises(SandboxError, match="cleanup"):
        run_test_gate(candidate, _faithful_runner(image, engine))


def test_preflight_rejects_unstable_double_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Break caught: clean status and fingerprint are captured once around a concurrent source edit."""
    import agent_loop
    from agent_loop import PreflightError, preflight_source

    source = _task2_repo(tmp_path)
    real_fingerprint = agent_loop.source_fingerprint
    calls = 0

    def racing_fingerprint(root: Path):
        nonlocal calls
        value = real_fingerprint(root)
        calls += 1
        if calls == 1:
            (root / "core" / "backtest_engine.py").write_text("RACE = True\n", encoding="utf-8")
        return value

    monkeypatch.setattr(agent_loop, "source_fingerprint", racing_fingerprint)
    with pytest.raises(PreflightError, match="stable"):
        preflight_source(source, acquire_lock=False)


def test_protected_gate_bytes_come_from_captured_commit_not_live_controller_file(tmp_path: Path) -> None:
    """Break caught: disposable worker copies live __file__ instead of captured protected gate bytes."""
    from agent_loop import preflight_source, run_source_commit_in_disposable_worker

    source = _task2_repo(tmp_path)
    state = preflight_source(source, acquire_lock=False)
    (source / "agent_loop.py").write_text("# concurrent hostile gate\n", encoding="utf-8")

    observed = run_source_commit_in_disposable_worker(
        state, lambda layout: (layout.gate / "agent_loop.py").read_text(encoding="utf-8")
    )
    assert observed == "# captured protected gate\n"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable-mode behavior")
def test_worker_export_preserves_executable_tracked_mode(tmp_path: Path) -> None:
    """Break caught: worker export copies bytes but silently drops executable tracked mode."""
    from agent_loop import export_candidate, preflight_source, run_in_disposable_worker

    source = _task2_repo(tmp_path)
    script = source / "core" / "tool.py"
    script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    script.chmod(0o755)
    _run_git(source, "add", "core/tool.py")
    _run_git(source, "commit", "-m", "executable")
    candidate = export_candidate(preflight_source(source, acquire_lock=False))
    mode = run_in_disposable_worker(candidate, lambda layout: stat.S_IMODE((layout.source / "core/tool.py").stat().st_mode))
    assert mode == 0o755


def test_linked_worktree_uses_real_worktree_specific_exclusive_lock(tmp_path: Path) -> None:
    """Break caught: a named-path imitation misses linked-worktree Git-dir lock semantics."""
    from agent_loop import PreflightError, preflight_source

    primary = _task2_repo(tmp_path)
    linked = tmp_path / "linked"
    _run_git(primary, "worktree", "add", "-b", "codex/linked-task2", str(linked))
    first = preflight_source(linked)
    try:
        assert first.lock_path.parent != linked / ".git"
        with pytest.raises(PreflightError, match="lock"):
            preflight_source(linked)
    finally:
        first.close()


def test_candidate_bootstrap_commit_is_reproducible_with_fixed_identity_time(tmp_path: Path) -> None:
    """Break caught: candidate bootstrap commit depends on wall-clock author/committer timestamps."""
    from agent_loop import export_candidate, preflight_source

    source = _task2_repo(tmp_path)
    state = preflight_source(source, acquire_lock=False)
    first = export_candidate(state)
    second = export_candidate(state)
    first_commit = subprocess.run(
        ["git", "show", "-s", "--format=%H%n%at%n%ct", "HEAD"], cwd=first.root,
        check=True, capture_output=True, text=True,
    ).stdout
    second_commit = subprocess.run(
        ["git", "show", "-s", "--format=%H%n%at%n%ct", "HEAD"], cwd=second.root,
        check=True, capture_output=True, text=True,
    ).stdout
    assert first_commit == second_commit
    assert first_commit.splitlines()[1:] == ["946684800", "946684800"]


def test_failed_bundle_validation_deletes_controller_snapshot(tmp_path: Path) -> None:
    """Break caught: header/schema/coverage rejection leaves a private DB snapshot behind."""
    import hashlib

    from agent_loop import DataBundleError, validate_historical_data_bundle

    bundle = tmp_path / "not-sqlite.bin"
    bundle.write_bytes(b"not sqlite")
    controller = Path(tempfile.gettempdir()) / f"agent-loop-test-controller-{time.time_ns()}"
    controller.mkdir()
    digest = hashlib.sha256(b"not sqlite").hexdigest()
    before = tuple(controller.iterdir())
    with pytest.raises(DataBundleError, match="header"):
        validate_historical_data_bundle(
            bundle, digest, ["AAPL"], "SPY", "2026-01-01", "2026-02-01",
            controller_temp_parent=controller,
        )
    assert tuple(controller.iterdir()) == before
    controller.rmdir()


def test_git_execution_keeps_resolved_absolute_binary_after_path_poisoning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: sanitized Git children still resolve a hostile executable from mutable PATH."""
    import agent_loop
    from agent_loop import preflight_source

    source = _task2_repo(tmp_path)
    preflight_source(source, acquire_lock=False)
    approved = agent_loop._GIT_EXECUTABLE
    assert approved is not None and approved.is_absolute()
    monkeypatch.setenv("PATH", str(tmp_path / "hostile-bin"))

    state = preflight_source(source, acquire_lock=False)
    assert state.head
    assert agent_loop._GIT_EXECUTABLE == approved


def test_engine_control_directories_are_never_mounted_to_candidate(tmp_path: Path) -> None:
    """Break caught: candidate can rewrite Docker config/context used by final inspect and cleanup."""
    from agent_loop import export_candidate, preflight_source, run_test_gate

    source = _task2_repo(tmp_path)
    candidate = export_candidate(preflight_source(source, acquire_lock=False))
    image = "registry.invalid/agent-loop@sha256:" + "a" * 64
    engine = FaithfulSandboxEngine(image)
    run_test_gate(candidate, _faithful_runner(image, engine))
    create = next(call for call in engine.calls if call[1] == "create")
    mounted_sources = {
        field.split(",", 2)[1].split("=", 1)[1]
        for index, value in enumerate(create)
        if value == "--mount"
        for field in (create[index + 1],)
    }
    engine_env = engine.call_kwargs[0]["env"]
    assert isinstance(engine_env, dict)
    for key in ("HOME", "DOCKER_CONFIG", "TEMP", "TMP"):
        assert ".engine-control" in str(engine_env[key])
        assert all(not str(engine_env[key]).startswith(source) for source in mounted_sources)


def test_bundle_snapshot_rejects_temp_parent_inside_git_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: poisoned TEMP causes approved DB snapshots to land inside the source checkout."""
    import agent_loop
    from agent_loop import QuarantineError, validate_historical_data_bundle

    source = _task2_repo(tmp_path)
    bundle = tmp_path / "outside.sqlite3"
    digest = _create_bundle(bundle, [])
    monkeypatch.setattr(agent_loop.tempfile, "gettempdir", lambda: str(source))

    with pytest.raises(QuarantineError, match="Git"):
        validate_historical_data_bundle(
            bundle, digest, ["AAPL"], "SPY", "2026-01-01", "2026-02-01"
        )
    assert not any(source.glob("agent-loop-data-*"))


def test_private_tree_cleanup_propagates_exact_root_removal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: cleanup ignores a failed final rmdir and reports a removed worker."""
    import agent_loop
    from agent_loop import _remove_private_tree

    worker = tmp_path / "worker"
    worker.mkdir()
    real_rmdir = agent_loop.os.rmdir

    def fail_root(path: str | os.PathLike[str]) -> None:
        if Path(path) == worker:
            raise OSError("injected exact-root removal failure")
        real_rmdir(path)

    monkeypatch.setattr(agent_loop.os, "rmdir", fail_root)
    with pytest.raises(OSError, match="injected"):
        _remove_private_tree(worker)
    assert worker.exists()


def _pipe_holding_tree_program(marker: Path, *, parent_sleep: bool) -> str:
    child = (
        "import pathlib,time; p=pathlib.Path(" + repr(str(marker)) + "); "
        "p.write_text('start',encoding='utf-8'); "
        "[(p.write_text(p.read_text(encoding='utf-8')+'x',encoding='utf-8'),time.sleep(.05)) "
        "for _ in range(400)]"
    )
    tail = "time.sleep(30)" if parent_sleep else (
        "[time.sleep(.01) for _ in range(100) if not pathlib.Path(" + repr(str(marker)) + ").exists()]"
    )
    return (
        "import pathlib,subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{child!r}],stdout=sys.stdout,stderr=sys.stderr); "
        + tail
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object containment")
@pytest.mark.parametrize("parent_sleep", [False, True])
def test_bounded_process_kills_pipe_holding_grandchild_on_success_and_timeout(
    tmp_path: Path,
    parent_sleep: bool,
) -> None:
    """Break caught: a grandchild survives parent exit/timeout and keeps drain threads mutable."""
    from agent_loop import _bounded_process

    marker = tmp_path / "heartbeat.txt"
    result = _bounded_process(
        (sys.executable, "-c", _pipe_holding_tree_program(marker, parent_sleep=parent_sleep)),
        timeout=0.75 if parent_sleep else 5,
    )
    assert result.timed_out is parent_sleep
    assert marker.exists()
    before = marker.read_bytes()
    time.sleep(0.25)
    assert marker.read_bytes() == before


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object containment")
def test_windows_job_assignment_failure_never_releases_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: target can spawn before the controller proves Job Object assignment."""
    import agent_loop
    from agent_loop import SandboxError, _bounded_process

    marker = tmp_path / "target-started.txt"

    def reject_assignment(_process: object) -> int:
        raise OSError("injected assignment failure")

    monkeypatch.setattr(agent_loop, "_assign_windows_kill_job", reject_assignment)
    with pytest.raises(SandboxError, match="contained"):
        _bounded_process(
            (sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('bad')"),
            timeout=2,
        )
    assert not marker.exists()
