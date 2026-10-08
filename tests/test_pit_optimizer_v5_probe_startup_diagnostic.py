"""Offline checks for the probe's separate startup budget and bounded error signal."""

from __future__ import annotations

import json
from types import SimpleNamespace

from core.pit_optimizer_v5 import baseline, image_manifest, probe_entry, sandbox
from core.pit_optimizer_v5.contracts import canonical_sha256_v5


def _probe_args(*, call: str = "1", startup: str = "30") -> tuple[str, ...]:
    digest = "a" * 64
    runtime = "b" * 64
    authority = canonical_sha256_v5(
        {
            "domain": "pit-optimizer-v5-trusted-probe-runtime-v1",
            "evaluator_contract_sha256": digest,
            "sandbox_profile_sha256": digest,
            "runtime_source_sha256": runtime,
            "probe_suite_id": probe_entry.PROBE_SUITE_ID_V5,
        }
    )
    return (
        "--request-sha256", digest,
        "--policy-sha256", digest,
        "--trusted-runtime-sha256", digest,
        "--immutable-constraints-sha256", digest,
        "--suite-id", probe_entry.PROBE_SUITE_ID_V5,
        "--evaluator-contract-sha256", digest,
        "--sandbox-profile-sha256", digest,
        "--probe-runtime-sha256", runtime,
        "--probe-runtime-authority-sha256", authority,
        "--call-timeout-seconds", call,
        "--startup-timeout-seconds", startup,
        "--output-limit-bytes", "4096",
    )


def _past_revision(monkeypatch) -> None:
    monkeypatch.setattr(image_manifest, "verify_installed_evaluator_source_v5", lambda **_kwargs: None)
    monkeypatch.setattr(probe_entry, "read_policy_source_v5", lambda: object())
    monkeypatch.setattr(
        probe_entry,
        "derive_policy_revision_identity_v5",
        lambda **_kwargs: SimpleNamespace(sha256="a" * 64),
    )


def test_probe_keeps_startup_and_method_budgets_separate(monkeypatch, capfd) -> None:
    _past_revision(monkeypatch)
    observed = {}

    def fail_startup(**kwargs):
        observed.update(kwargs)
        raise TimeoutError("private worker detail must not escape")

    monkeypatch.setattr(probe_entry, "PolicyWorkerSessionV5", fail_startup)
    assert probe_entry.main(_probe_args(call="1", startup="30")) == 3
    assert observed["call_timeout_seconds"] == 1.0
    assert observed["startup_timeout_seconds"] == 30.0
    captured = capfd.readouterr()
    assert captured.out == ""
    diagnostic = json.loads(captured.err)
    assert diagnostic == {
        "error_type": "TimeoutError",
        "schema_version": 5,
        "stage": "worker_startup",
    }
    assert "private worker detail" not in captured.err
    assert len(captured.err.encode("utf-8")) < 256


def test_probe_failure_before_worker_names_policy_stage(monkeypatch, capfd) -> None:
    monkeypatch.setattr(image_manifest, "verify_installed_evaluator_source_v5", lambda **_kwargs: None)
    monkeypatch.setattr(
        probe_entry,
        "read_policy_source_v5",
        lambda: (_ for _ in ()).throw(ValueError("private source detail")),
    )
    assert probe_entry.main(_probe_args()) == 3
    diagnostic = json.loads(capfd.readouterr().err)
    assert diagnostic == {
        "error_type": "ValueError",
        "schema_version": 5,
        "stage": "policy_overlay",
    }


def test_typed_probe_failures_keep_distinct_bounded_categories(monkeypatch, capfd) -> None:
    _past_revision(monkeypatch)

    class PolicyProbeTimeoutV5(TimeoutError):
        pass

    class PolicyProbeProtocolFailureV5(RuntimeError):
        pass

    class Client:
        def __init__(self, **_kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(probe_entry, "PolicyWorkerSessionV5", lambda **_kwargs: object())
    monkeypatch.setattr(probe_entry, "JsonLinePolicyClient", Client)
    for failure in (PolicyProbeTimeoutV5, PolicyProbeProtocolFailureV5):
        def fail_fingerprint(_client, _failure=failure):
            raise _failure("private sentinel must not escape")

        monkeypatch.setattr(probe_entry, "fingerprint_policy_client_v5", fail_fingerprint)
        assert probe_entry.main(_probe_args()) == 3
        captured = capfd.readouterr()
        assert captured.out == ""
        diagnostic = json.loads(captured.err)
        assert diagnostic == {
            "error_type": failure.__name__,
            "schema_version": 5,
            "stage": "fingerprint",
        }
        assert "private sentinel" not in captured.err
        assert len(captured.err.encode("utf-8")) < 256


def test_probe_success_keeps_wire_output_and_emits_no_diagnostic(monkeypatch, capfd) -> None:
    _past_revision(monkeypatch)
    observed = {}

    class Session:
        def __init__(self, **kwargs):
            observed.update(kwargs)

    class Client:
        def __init__(self, **_kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(probe_entry, "PolicyWorkerSessionV5", Session)
    monkeypatch.setattr(probe_entry, "JsonLinePolicyClient", Client)
    monkeypatch.setattr(
        probe_entry,
        "fingerprint_policy_client_v5",
        lambda _client: SimpleNamespace(to_primitive=lambda: {"probe": "ok"}),
    )
    monkeypatch.setattr(probe_entry, "_write_output", lambda **kwargs: observed.update(kwargs))
    assert probe_entry.main(_probe_args()) == 0
    assert observed["call_timeout_seconds"] == 1.0
    assert observed["startup_timeout_seconds"] == 30.0
    assert json.loads(observed["content"])["fingerprint"] == {"probe": "ok"}
    assert capfd.readouterr().err == ""


def test_baseline_probe_argv_propagates_both_budgets(monkeypatch) -> None:
    digest = "a" * 64
    inputs = SimpleNamespace(
        policy=SimpleNamespace(
            sha256=digest,
            trusted_policy_runtime_sha256=digest,
            immutable_constraints_sha256=digest,
        ),
        evaluator=SimpleNamespace(sha256=digest),
        sandbox=SimpleNamespace(sha256=digest, runtime_source_sha256=digest, output_limit_bytes=4096),
        resources=SimpleNamespace(
            policy_method_timeout_seconds=1,
            worker_startup_timeout_seconds=30,
            mechanics_timeout_seconds=60,
        ),
    )
    monkeypatch.setattr(sandbox, "derive_trusted_probe_runtime_authority_v5", lambda **_kwargs: digest)
    transport = baseline._LocalBaselineTransportV5(object(), inputs)
    transport._run = lambda **kwargs: kwargs
    call = transport.probe(request_sha256=digest)
    args = call["module_args"]
    assert args[args.index("--call-timeout-seconds") + 1] == "1"
    assert args[args.index("--startup-timeout-seconds") + 1] == "30"
    assert call["timeout"] == 60
