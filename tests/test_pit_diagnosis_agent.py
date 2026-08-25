from __future__ import annotations

import json
import hashlib
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_loop import (
    AgentCompletion,
    BudgetLedger,
    CompletionEnvelope,
    CODER_MODEL,
    ConfigurationError,
    ORCHESTRATOR_MODEL,
    OpenRouterGateway,
    ProcessResult,
    ProtocolValidationError,
    REASONER_MODEL,
    Usage,
    WorkerObservation,
)
from pit_diagnosis_agent import (
    PitAgentEvidence,
    PitDiagnosisGateConfig,
    PitDiagnosisLoopServices,
    PitDomain,
    PitDiagnosisLoopResult,
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


@pytest.mark.parametrize(
    "forbidden_key",
    ("raw", "rows", "transactions", "prices", "fundamentals", "payload", "secret", "path", "source_text"),
)
def test_direct_pit_evidence_construction_rejects_forbidden_keys(forbidden_key: str) -> None:
    with pytest.raises(ProtocolValidationError, match="forbids key"):
        replace(pit_agent_evidence(), metrics={forbidden_key: 1})


def test_direct_pit_evidence_construction_rejects_oversized_provider_payload() -> None:
    def long_ids(prefix: str) -> tuple[str, ...]:
        return tuple(f"{prefix}{index:03d}{'A' * 124}" for index in range(16))

    evidence_ids = long_ids("E")
    rule_ids = long_ids("R")
    invariant_ids = long_ids("I")
    experiment_ids = long_ids("D")
    with pytest.raises(ProtocolValidationError, match="provider byte limit"):
        replace(
            pit_agent_evidence(),
            experiment_result_sha256s={experiment_id: _sha("f") for experiment_id in experiment_ids},
            experiment_partition_result_sha256s={
                f"{experiment_id}@discovery": _sha("f") for experiment_id in experiment_ids
            },
            evidence_ids=evidence_ids,
            rule_ids=rule_ids,
            invariant_ids=invariant_ids,
            experiment_ids=experiment_ids,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("evidence_ids", []),
        ("evidence_ids", ["EXIT.A", "EXIT.MA_001"]),
        ("rule_ids", []),
        ("rule_ids", ["X.A", "X.STRUCTURAL_SELL"]),
        ("invariant_ids", []),
        ("invariant_ids", ["INV.A", "INV.LOSS_LIMIT"]),
    ),
)
def test_non_skip_reasoning_plan_requires_exactly_one_grounding_id(
    field: str, value: list[str]
) -> None:
    payload = json.loads(valid_pit_plan_json("D4.STRUCTURAL_SELL"))
    payload[field] = value
    with pytest.raises(ProtocolValidationError, match="exactly one"):
        PitReasoningPlan.from_json(json.dumps(payload))


def test_skip_reasoning_plan_remains_explicit() -> None:
    plan = PitReasoningPlan.from_json(
        json.dumps(
            {
                "causal_hypothesis": "The supplied evidence is insufficient for a falsifiable experiment.",
                "evidence_ids": [],
                "rule_ids": [],
                "invariant_ids": [],
                "experiment_id": "",
                "skip": True,
                "skip_reason": "Insufficient closed evidence.",
            }
        )
    )
    assert plan.skip is True


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


def test_pit_reasoner_gateway_uses_fixed_json_schema() -> None:
    response = _FakeResponse(valid_pit_plan_json("D4.STRUCTURAL_SELL"), model="deepseek/deepseek-r1")
    fake_client = _FakeClient([response])
    gateway = OpenRouterGateway(
        client=fake_client,
        pricing_loader=lambda _model: {"prompt": 1.0, "completion": 1.0},
        ledger=BudgetLedger(max_usd=1.0),
    )

    gateway.request_pit_diagnosis_once("reasoner", "{}", PitReasoningPlan.from_json)

    response_format = fake_client.chat.completions.calls[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "pit_diagnosis_reasoner_v1"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"]["additionalProperties"] is False


def test_pit_gateway_audits_closed_incomplete_accounting_before_terminal_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_loop

    class IncompleteGateway:
        def __init__(self) -> None:
            self.ledger = BudgetLedger(0.50, max_calls=3, max_tokens=32_768)

        def request_pit_diagnosis_once(self, _role: str, _payload: str, _parser: object, **_kwargs: object) -> object:
            reservation = self.ledger.reserve(
                "sealed-prompt",
                10,
                agent_loop.Pricing(prompt_per_million=1000.0, completion_per_million=1000.0),
            )
            facts = agent_loop.IncompleteAccountingFacts(
                schema_version=2,
                call_index=self.ledger.calls,
                role="orchestrator",
                inline_failure_code=agent_loop.AccountingFailureCode.INLINE_USAGE_MISSING,
                recovery_failure_code=agent_loop.AccountingFailureCode.RECOVERY_UNAVAILABLE,
                recovery_usage_diagnostic=None,
                generation_attempts=0,
                response_id_safe=True,
                accounting_complete=False,
                budget_charge_basis="full_reservation",
                retained_reservation_tokens=reservation.token_upper_bound,
                retained_reservation_usd=reservation.amount_usd,
            )
            self.ledger.reconcile(reservation, Usage())
            raise agent_loop.IncompleteAccountingError(facts)

    events: list[dict[str, object]] = []
    monkeypatch.setattr(
        agent_loop.AuditTrail,
        "append_event",
        lambda _self, _state, _event_type, details: events.append(dict(details)),
    )
    audit = agent_loop.AuditTrail(tmp_path / "audit", "run-20260825T020304Z")
    gateway = IncompleteGateway()
    services = SimpleNamespace(gateway=gateway, known_secrets=())
    with pytest.raises(agent_loop.IncompleteAccountingError):
        agent_loop._pit_call_gateway(
            audit,
            services,
            "orchestrator",
            {"sealed": True},
            PitRoute.from_json,
            deadline=10.0,
            monotonic=lambda: 1.0,
        )
    assert audit._pit_call_count == 1
    assert events
    assert events[0]["accounting_complete"] is False
    assert "accounting_failure" in events[0]


def _controller_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _controller_git(repo: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=repo, check=True, capture_output=True, text=True)


def _pit_controller_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    experiment_id: str = "D3.M_CONFIRMED_UPTREND",
    synthetic_code: bool = False,
    baseline_match: bool = True,
) -> tuple[Any, Any, Any, Any, Any, Any, str, str, str]:
    """Build a small sealed publication and a real controller candidate for Task 9 tests."""
    import agent_loop
    from agent_loop import TypedCodingProposal

    git = Path(shutil.which("git") or "git").resolve()
    agent_loop.configure_git_executable(git)
    source = (tmp_path / "source").resolve()
    source.mkdir()
    _controller_git(source, "init", "--quiet")
    _controller_git(source, "config", "user.email", "pit-tests@example.invalid")
    _controller_git(source, "config", "user.name", "PIT Tests")
    _controller_git(source, "switch", "-c", "codex/pit-controller")
    (source / "core").mkdir()
    (source / "agent_loop.py").write_text("# protected gate\n", encoding="utf-8", newline="\n")
    (source / "core" / "backtest_engine.py").write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
    _controller_git(source, "add", ".")
    _controller_git(source, "commit", "--quiet", "-m", "fixture")

    # The repository test harness lives below a dotenv-bearing checkout.  The
    # dedicated preflight/quarantine tests cover that ancestor policy; this
    # fixture patches only the temp-parent resolver so the state-machine tests
    # can exercise the PIT protocol without depending on the host temp ACL.
    controller_root = (tmp_path / "controller").resolve()
    controller_root.mkdir()
    monkeypatch.setattr(
        agent_loop,
        "_validate_controller_temp_parent",
        lambda parent, _forbidden: parent.resolve(),
    )
    state = agent_loop.preflight_source(source, acquire_lock=False, controller_temp_parent=controller_root)
    candidate = agent_loop.export_candidate(state, destination_parent=controller_root)
    audit = agent_loop.AuditTrail((tmp_path / "audit").resolve(), "run-20260825T010203Z")
    audit_shadow = (tmp_path / "audit-shadow").resolve()
    audit_shadow.mkdir()
    monkeypatch.setattr(agent_loop.AuditTrail, "append_event", lambda *_args, **_kwargs: {})

    def write_handoff(_self: object, value: object, *, name: str = "handoff") -> Path:
        path = audit_shadow / f"{name}.json"
        path.write_bytes(agent_loop._canonical_json_bytes(value) + b"\n")
        return path

    monkeypatch.setattr(agent_loop.AuditTrail, "write_handoff_metadata", write_handoff)

    def write_provider_call(
        _self: object,
        record: object,
        *,
        payload_sha256: str | None = None,
    ) -> tuple[Path, str]:
        del payload_sha256
        path = audit_shadow / f"provider-call-{len(list(audit_shadow.glob('provider-call-*.json'))) + 1:04d}.json"
        path.write_bytes(agent_loop._canonical_json_bytes(agent_loop.asdict(record)) + b"\n")
        return path, _controller_sha256(path)

    monkeypatch.setattr(agent_loop.AuditTrail, "write_provider_call", write_provider_call)
    monkeypatch.setattr(
        agent_loop,
        "export_inert_handoff",
        lambda *_args, **_kwargs: SimpleNamespace(diff_sha256="a" * 64),
    )

    diagnosis_run = (tmp_path / "diagnosis-run").resolve()
    diagnosis_run.mkdir()
    baseline_identity = "1" * 64
    root = Path(__file__).parents[1]
    rulebook = (tmp_path / "rulebook.json").resolve()
    catalog = (tmp_path / "catalog.json").resolve()
    shutil.copyfile(root / "config" / "pit_canslim_rulebook_v1.json", rulebook)
    catalog_payload = json.loads((root / "config" / "pit_diagnosis_experiments_v1.json").read_text(encoding="utf-8"))
    if synthetic_code:
        catalog_payload["experiments"].append(
            {
                "experiment_id": experiment_id,
                "phase": "D4",
                "domain": "exit",
                "kind": "exit",
                "changed_dimensions": ["synthetic_structural_exit"],
                "rule_ids": ["X.STRUCTURAL_SELL"],
                "promotion_eligible": False,
                "controller_composed": False,
                "requires_code": True,
                "allowed_variant_ids": [],
            }
        )
    catalog.write_text(json.dumps(catalog_payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")
    pit_bundle = (tmp_path / "pit-bundle.bin").resolve()
    fact_cache = (tmp_path / "fact-cache.bin").resolve()
    pit_bundle.write_bytes(b"sealed pit bundle")
    fact_cache.write_bytes(b"sealed fact cache")
    manifest = {
        "schema_version": 1,
        "status": "complete",
        "source_commit": state.head,
        "source_fingerprint_sha256": state.fingerprint.sha256,
        "baseline_manifest_sha256": baseline_identity,
        "bundle_sha256": _controller_sha256(pit_bundle),
        "fact_cache_sha256": _controller_sha256(fact_cache),
        "rulebook_sha256": _controller_sha256(rulebook),
        "catalog_sha256": _controller_sha256(catalog),
        "fidelity_label": "fidelity_incomplete",
    }
    manifest_path = diagnosis_run / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")
    reproduced_identity = baseline_identity if baseline_match else "2" * 64
    (diagnosis_run / "baseline_reproduction.json").write_text(
        json.dumps(
            {
                "passed": True,
                "authority_manifest_sha256": baseline_identity,
                "reproduced_manifest_sha256": reproduced_identity,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )

    gate = PitDiagnosisGateConfig(
        diagnosis_run=diagnosis_run,
        diagnosis_manifest_sha256=_controller_sha256(manifest_path),
        pit_bundle=pit_bundle,
        pit_bundle_sha256=_controller_sha256(pit_bundle),
        fact_cache=fact_cache,
        fact_cache_sha256=_controller_sha256(fact_cache),
        rulebook=rulebook,
        rulebook_sha256=_controller_sha256(rulebook),
        experiment_catalog=catalog,
        experiment_catalog_sha256=_controller_sha256(catalog),
        output_root=(tmp_path / "derivatives").resolve(),
    )
    if synthetic_code:
        domain = PitDomain.EXIT
        evidence_id = "EXIT.STRUCTURAL_SELL"
        rule_id = "X.STRUCTURAL_SELL"
        invariant_id = "INV.EXIT"
    else:
        domain = PitDomain.MARKET
        evidence_id = "MARKET.CONFIRMED_UPTREND"
        rule_id = "M.CONFIRMED_UPTREND"
        invariant_id = "INV.MARKET"
    result_sha = "f" * 64
    evidence = PitAgentEvidence(
        diagnosis_run_sha256=gate.diagnosis_manifest_sha256,
        pit_bundle_sha256=gate.pit_bundle_sha256,
        fact_cache_sha256=gate.fact_cache_sha256,
        rulebook_sha256=gate.rulebook_sha256,
        experiment_catalog_sha256=gate.experiment_catalog_sha256,
        experiment_result_sha256s={experiment_id: result_sha},
        metrics={"average_cash_pct": 42.0},
        evidence_ids=(evidence_id,),
        rule_ids=(rule_id,),
        invariant_ids=(invariant_id,),
        experiment_ids=(experiment_id,),
        fidelity_label="fidelity_incomplete",
        promotion_eligible=False,
    )

    class Gateway:
        def __init__(self) -> None:
            self.roles: list[str] = []
            self.payloads: dict[str, dict[str, object]] = {}
            self.ledger = BudgetLedger(0.50, max_calls=3, max_tokens=32_768)
            self._MODELS = {
                "orchestrator": ORCHESTRATOR_MODEL,
                "reasoner": REASONER_MODEL,
                "coder": CODER_MODEL,
            }

        def _completion(self, role: str, value: object) -> AgentCompletion[object]:
            return AgentCompletion(
                payload=value,
                usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2, cost_usd=0.0),
                finish_reason="stop",
                model=self._MODELS[role],
            )

        def request_pit_diagnosis_once(self, role: str, payload: str, parser: object, **_kwargs: object) -> object:
            del parser
            self.roles.append(role)
            self.payloads[role] = json.loads(payload)
            # Mirror the strict gateway's post-call ledger reconciliation for the
            # controller's exact PIT call-index and token-delta checks.
            self.ledger.calls += 1
            self.ledger.prompt_tokens += 1
            self.ledger.completion_tokens += 1
            self.ledger.total_tokens += 2
            if role == "orchestrator":
                return self._completion(role, PitRoute("reason", domain, (evidence_id,)))
            if role == "reasoner":
                return self._completion(role, PitReasoningPlan(
                    causal_hypothesis="The selected causal dimension is measurable in the sealed run.",
                    evidence_ids=(evidence_id,),
                    rule_ids=(rule_id,),
                    invariant_ids=(invariant_id,),
                    experiment_id=experiment_id,
                    skip=False,
                    skip_reason="",
                ))
            replacement = agent_loop.ExactLineReplacement(
                path="core/backtest_engine.py", old_lines=("VALUE = 1",), new_lines=("VALUE = 2",)
            )
            return self._completion(role, TypedCodingProposal(summary="Apply the controller-approved exact replacement.", replacements=(replacement,)))

    gateway = Gateway()
    replacement = agent_loop.ExactLineReplacement(
        path="core/backtest_engine.py", old_lines=("VALUE = 1",), new_lines=("VALUE = 2",)
    )
    def run_experiment(*, sealed_input_root: Path, **_kwargs: object) -> dict[str, object]:
        sealed = json.loads((sealed_input_root / "sealed-inputs.json").read_text(encoding="utf-8"))
        assert sealed["read_only"] is True
        assert sealed["network"] == "none"
        return {
            "experiment_id": experiment_id,
            "partition": "discovery",
            "identity_sha256": _sha("1"),
            "result_sha256": result_sha,
            "network_disabled": True,
            "read_only": True,
            "worker_confined": True,
        }

    def run_quality(**_kwargs: object) -> dict[str, object]:
        return {
            "experiment_id": experiment_id,
            "partition": "discovery",
            "identity_sha256": _sha("2"),
            "result_sha256": result_sha,
            "quality_passed": True,
            "network_disabled": True,
            "read_only": True,
            "worker_confined": True,
        }

    worker_attestation = {
        "network_disabled": True,
        "read_only": True,
        "worker_confined": True,
    }
    run_experiment.__pit_worker_attestation__ = worker_attestation
    run_quality.__pit_worker_attestation__ = worker_attestation

    def compile_runner(_layout: object, _paths: tuple[str, ...]) -> bool:
        return True

    compile_runner.__pit_worker_attestation__ = worker_attestation

    services = PitDiagnosisLoopServices(
        gateway=gateway,
        verify_diagnosis_run=lambda _path: {"verified": True},
        build_evidence=evidence,
        run_experiment=run_experiment,
        run_quality=run_quality,
        compile_runner=compile_runner,
        allowed_replacements={experiment_id: (replacement,)} if synthetic_code else (),
        editable_paths=("core/backtest_engine.py",),
    )
    return gate, state, candidate, audit, services, gateway, result_sha, str(source / "core" / "backtest_engine.py"), evidence_id


def test_config_experiment_uses_two_calls_and_never_calls_coder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import agent_loop

    gate, state, candidate, audit, services, gateway, _result_sha, source_path, _evidence_id = _pit_controller_fixture(tmp_path, monkeypatch)
    source_before = Path(source_path).read_bytes()
    result = agent_loop.run_pit_diagnosis_loop(gate, state, candidate, audit, services)
    assert isinstance(result, PitDiagnosisLoopResult)
    assert gateway.roles == ["orchestrator", "reasoner"], result
    assert result.selected_experiment_id == "D3.M_CONFIRMED_UPTREND"
    assert result.coder_called is False
    assert result.source_modified is False
    assert result.cleanup_complete is True
    assert Path(source_path).read_bytes() == source_before
    reason_payload = gateway.payloads["reasoner"]
    assert reason_payload["evidence"]["evidence_ids"] == ["MARKET.CONFIRMED_UPTREND"]
    assert reason_payload["rule_ids"] == ["M.CONFIRMED_UPTREND"]


def test_code_experiment_requires_exact_controller_replacement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import agent_loop

    gate, state, candidate, audit, services, gateway, _result_sha, source_path, _evidence_id = _pit_controller_fixture(
        tmp_path, monkeypatch, experiment_id="D4.TEST_STRUCTURAL_VARIANT", synthetic_code=True
    )
    source_before = Path(source_path).read_bytes()
    result = agent_loop.run_pit_diagnosis_loop(gate, state, candidate, audit, services)
    assert gateway.roles == ["orchestrator", "reasoner", "coder"], result
    assert result.coder_called is True
    assert result.exported_diff_sha256 is not None
    assert result.source_modified is False
    assert result.cleanup_complete is True
    assert Path(source_path).read_bytes() == source_before


def test_orchestrator_route_is_rejected_before_reasoner_when_it_contains_unknown_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import agent_loop

    gate, state, candidate, audit, services, gateway, _result_sha, _source_path, _evidence_id = _pit_controller_fixture(tmp_path, monkeypatch)
    original = gateway.request_pit_diagnosis_once

    def unknown_route(role: str, payload: str, parser: object, **kwargs: object) -> object:
        if role == "orchestrator":
            gateway.roles.append(role)
            gateway.payloads[role] = json.loads(payload)
            return PitRoute("reason", PitDomain.MARKET, ("UNKNOWN",))
        return original(role, payload, parser, **kwargs)

    gateway.request_pit_diagnosis_once = unknown_route  # type: ignore[method-assign]
    result = agent_loop.run_pit_diagnosis_loop(gate, state, candidate, audit, services)
    assert result.terminal_status == "protocol_rejected", result
    assert gateway.roles == ["orchestrator"]


def test_d0_uses_published_baseline_identity_and_closes_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import agent_loop

    gate, state, candidate, audit, services, gateway, _result_sha, _source_path, _evidence_id = _pit_controller_fixture(
        tmp_path, monkeypatch, baseline_match=False
    )
    result = agent_loop.run_pit_diagnosis_loop(gate, state, candidate, audit, services)
    assert result.terminal_status == "d0_failed", result
    assert result.d0_passed is False
    assert gateway.roles == []
    assert result.cleanup_complete is True


def test_pit_cli_adds_gate_and_rejects_proposal_samples(tmp_path: Path) -> None:
    import agent_loop
    from pit_diagnosis import parse_pit_diagnosis_result

    common = [
        "--repo-root", str(tmp_path / "source"),
        "--permanent-runtime-root", str(tmp_path / "runtime"),
        "--git-executable", str(tmp_path / "git.exe"),
        "--controller-temp-parent", str(tmp_path / "controller"),
        "--artifact-root", str(tmp_path / "artifacts"),
        "--docker-executable", str(tmp_path / "docker.exe"),
        "--sandbox-image", "example.invalid/pit@sha256:" + "a" * 64,
        "--gate", "pit_diagnosis", "--max-usd", "0.50",
    ]
    namespace = agent_loop.build_parser().parse_args(common)
    assert namespace.gate == "pit_diagnosis"
    with pytest.raises(agent_loop.ConfigurationError, match="proposal samples"):
        agent_loop._build_cli_config(
            agent_loop.build_parser().parse_args(common + ["--proposal-samples", "1"])
        )
    payload = {
        "experiment_id": "D3.M_CONFIRMED_UPTREND",
        "partition": "discovery",
        "identity_sha256": "b" * 64,
        "result_sha256": "c" * 64,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    assert parse_pit_diagnosis_result("PIT_DIAGNOSIS_RESULT=" + encoded) == payload
    with pytest.raises(ValueError, match="canonical"):
        parse_pit_diagnosis_result("PIT_DIAGNOSIS_RESULT=" + json.dumps(payload))
    invalid_id = dict(payload, experiment_id="D3/NOT-CLOSED")
    invalid_encoded = json.dumps(invalid_id, sort_keys=True, separators=(",", ":"))
    with pytest.raises(ValueError, match="identity"):
        parse_pit_diagnosis_result("PIT_DIAGNOSIS_RESULT=" + invalid_encoded)


def test_pit_result_hash_is_bound_to_selected_experiment_and_partition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_loop

    gate, state, candidate, audit, services, _gateway, result_sha, _source_path, _evidence_id = _pit_controller_fixture(
        tmp_path, monkeypatch
    )

    def wrong_result(**_kwargs: object) -> dict[str, object]:
        return {
            "experiment_id": "D3.M_CONFIRMED_UPTREND",
            "partition": "validation",
            "identity_sha256": "1" * 64,
            "result_sha256": result_sha,
            "network_disabled": True,
            "read_only": True,
            "worker_confined": True,
        }

    wrong_result.__pit_worker_attestation__ = {
        "network_disabled": True,
        "read_only": True,
        "worker_confined": True,
    }
    services = replace(services, run_experiment=wrong_result)
    result = agent_loop.run_pit_diagnosis_loop(gate, state, candidate, audit, services)
    assert result.terminal_status == "controller_error"
    assert result.worker_confined is False
    assert result.deterministic_result_sha256 is None


def test_pit_direct_worker_without_attestation_cannot_report_confined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_loop

    gate, state, candidate, audit, services, _gateway, result_sha, _source_path, _evidence_id = _pit_controller_fixture(
        tmp_path, monkeypatch
    )

    def unattested(**_kwargs: object) -> dict[str, object]:
        return {
            "experiment_id": "D3.M_CONFIRMED_UPTREND",
            "partition": "discovery",
            "identity_sha256": "1" * 64,
            "result_sha256": result_sha,
            "network_disabled": True,
            "read_only": True,
            "worker_confined": True,
        }

    services = replace(services, run_experiment=unattested)
    result = agent_loop.run_pit_diagnosis_loop(gate, state, candidate, audit, services)
    assert result.terminal_status == "controller_error"
    assert result.worker_confined is False


def test_pit_domain_routes_canslim_fundamental_rule_prefixes() -> None:
    import agent_loop

    assert agent_loop._pit_domain_matches(PitDomain.DATA, "A.EPS_MULTIYEAR")
    assert agent_loop._pit_domain_matches(PitDomain.DATA, "C.EPS_YOY")
    assert not agent_loop._pit_domain_matches(PitDomain.ENTRY, "A.EPS_MULTIYEAR")


def test_pit_services_reject_legacy_gateway_fallback() -> None:
    with pytest.raises(ConfigurationError, match="request_pit_diagnosis_once"):
        PitDiagnosisLoopServices(gateway=SimpleNamespace(request=lambda *_args, **_kwargs: None))


def test_pit_evidence_rejects_selected_hash_disagreement() -> None:
    with pytest.raises(ProtocolValidationError, match="disagrees"):
        replace(
            pit_agent_evidence(),
            experiment_partition_result_sha256s={
                "D4.STRUCTURAL_SELL@discovery": _sha("0"),
            },
        )


def test_pit_catalog_rejects_noncanonical_experiment_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import agent_loop

    gate, _state, _candidate, _audit, _services, _gateway, _result_sha, _source_path, _evidence_id = _pit_controller_fixture(
        tmp_path, monkeypatch
    )
    payload = json.loads(gate.experiment_catalog.read_text(encoding="utf-8"))
    payload["experiments"][0]["experiment_id"] = "d0.not-canonical"
    catalog = tmp_path / "catalog-invalid.json"
    catalog.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="catalog"):
        agent_loop._pit_catalog_records(catalog, gate.rulebook)


def test_pit_publication_source_identity_is_bound_to_preflight_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_loop

    gate, state, _candidate, _audit, _services, _gateway, _result_sha, _source_path, _evidence_id = _pit_controller_fixture(
        tmp_path, monkeypatch
    )
    manifest_path = gate.diagnosis_run / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_commit"] = "0" * 40
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    gate = replace(gate, diagnosis_manifest_sha256=_controller_sha256(manifest_path))
    with pytest.raises(ConfigurationError, match="source_commit"):
        agent_loop._pit_snapshot_input_identities(gate, state)


def test_pit_production_experiment_adapter_parses_signed_worker_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_loop
    from pit_diagnosis import PIT_DIAGNOSIS_SENTINEL

    gate, state, candidate, _audit, _services, _gateway, result_sha, source_path, _evidence_id = _pit_controller_fixture(
        tmp_path, monkeypatch
    )
    baseline = tmp_path / "baseline-run"
    baseline.mkdir()
    (baseline / "run_manifest.json").write_text("{}\n", encoding="utf-8")
    gate = replace(gate, baseline_run=baseline)

    selected = "D3.M_CONFIRMED_UPTREND"
    encoded = json.dumps(
        {
            "experiment_id": selected,
            "partition": "discovery",
            "identity_sha256": "1" * 64,
            "result_sha256": result_sha,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    stdout = PIT_DIAGNOSIS_SENTINEL + encoded

    class FakeSandbox:
        def __init__(self) -> None:
            self.argv: tuple[str, ...] | None = None

        def run_worker(self, _layout: object, argv: tuple[str, ...], _environment: object) -> WorkerObservation:
            self.argv = argv
            process = ProcessResult.ok(stdout)
            payload = {
                "returncode": process.returncode,
                "timed_out": process.timed_out,
                "oom_killed": False,
                "stdout_sha256": process.stdout_sha256,
                "stderr_sha256": process.stderr_sha256,
                "cleanup_verified": True,
                "gate_observation": True,
                "worker_confined": True,
                "network_disabled": True,
                "read_only": True,
            }
            return WorkerObservation(process, CompletionEnvelope(payload, "test-signature"))

        def verify_completion_envelope(self, _envelope: CompletionEnvelope) -> bool:
            return True

    fake = FakeSandbox()
    runner = agent_loop._pit_sandbox_experiment_runner(fake, gate, candidate)
    observed = runner(
        config=gate,
        candidate=candidate,
        experiment_id=selected,
        partition="discovery",
        sealed_input_root=tmp_path / "sealed",
    )
    assert observed["experiment_id"] == selected
    assert observed["partition"] == "discovery"
    assert observed["result_sha256"] == result_sha
    assert observed["network_disabled"] is True
    assert fake.argv is not None
    assert fake.argv[:2] == ("pit_diagnosis.py", "run-experiment")
    assert "/workspace/data/baseline-run" in fake.argv
    assert fake.argv[16:20] == (
        "--source-commit",
        state.head,
        "--source-fingerprint-sha256",
        state.fingerprint.sha256,
    )
    agent_loop.SandboxRunner._validate_python_args(Path(__file__).parents[1], fake.argv)
    assert Path(source_path).read_bytes() == b"VALUE = 1\n"
    agent_loop.dispose_candidate(candidate)
    state.close()


def test_pit_source_fingerprint_matches_controller_checkout_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_loop
    import pit_diagnosis

    _gate, state, candidate, _audit, _services, _gateway, _result_sha, _source_path, _evidence_id = _pit_controller_fixture(
        tmp_path, monkeypatch
    )
    assert state.fingerprint is not None
    assert pit_diagnosis._source_commit(state.root) == state.head
    assert pit_diagnosis._source_fingerprint(state.root) == state.fingerprint.sha256
    agent_loop.dispose_candidate(candidate)
    state.close()


def test_pit_compile_adapter_requires_signed_worker_boundary_facts(
    tmp_path: Path,
) -> None:
    import agent_loop

    class FakeSandbox:
        def __init__(self) -> None:
            self.network_disabled = True

        def run_worker(self, _layout: object, _argv: tuple[str, ...], _environment: object) -> WorkerObservation:
            process = ProcessResult.ok()
            payload = {
                "returncode": process.returncode,
                "timed_out": process.timed_out,
                "oom_killed": False,
                "stdout_sha256": process.stdout_sha256,
                "stderr_sha256": process.stderr_sha256,
                "cleanup_verified": True,
                "gate_observation": True,
                "worker_confined": True,
                "network_disabled": self.network_disabled,
                "read_only": True,
            }
            return WorkerObservation(process, CompletionEnvelope(payload, "test-signature"))

        def verify_completion_envelope(self, _envelope: CompletionEnvelope) -> bool:
            return True

    fake = FakeSandbox()
    runner = agent_loop.sandbox_compile_runner(fake)
    layout = SimpleNamespace(home=tmp_path)
    assert runner(layout, ("pit_diagnosis.py",)) is True
    fake.network_disabled = False
    assert runner(layout, ("pit_diagnosis.py",)) is False
