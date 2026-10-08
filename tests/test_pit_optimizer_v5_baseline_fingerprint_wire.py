"""Regression for the sandbox probe wire format and stored baseline format."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from core.pit_optimizer_v5.artifacts import ArtifactRefV5, ArtifactSchemaFailureV5
from core.pit_optimizer_v5.baseline import BaselineSandboxWorkerV5, _load
from core.pit_optimizer_v5.contracts import canonical_json_bytes_v5
from tests.test_pit_optimizer_v5_mechanism_artifacts import _static_fingerprint


def _worker(fingerprint_body, *, request_override=None):
    fingerprint = _static_fingerprint()
    inputs = SimpleNamespace(
        reference=ArtifactRefV5("evaluator/engineering-baseline-inputs.json", "a" * 64),
        policy=SimpleNamespace(sha256="b" * 64),
        sandbox=SimpleNamespace(output_limit_bytes=67_108_864),
    )

    class ProbeTransport:
        def probe(self, *, request_sha256):
            envelope = {
                "fingerprint": fingerprint_body,
                "policy_revision_sha256": inputs.policy.sha256,
                "request_sha256": request_sha256 if request_override is None else request_override,
                "suite_id": fingerprint.suite_id,
            }
            return canonical_json_bytes_v5(envelope)

    return BaselineSandboxWorkerV5(
        repository=None, inputs=inputs, transport=ProbeTransport(), ordinal=1,
        pit_data_scope="engineering_v3",
    )


def _wire_body():
    return json.loads(json.dumps(_static_fingerprint().to_primitive()))


def test_sandbox_probe_wire_roundtrips_then_persisted_baseline_still_loads():
    fingerprint = _static_fingerprint()
    wire = _wire_body()
    assert "decision_json_utf8" in wire["observations"][0]
    assert "decision_json" not in wire["observations"][0]

    decoded = _worker(wire).fingerprint()
    assert decoded == fingerprint

    stored = canonical_json_bytes_v5(decoded)
    assert b'"decision_json"' in stored
    assert b'"decision_json_utf8"' not in stored
    ref = ArtifactRefV5("evaluator/semantic-fingerprint.json", hashlib.sha256(stored).hexdigest())
    assert _load({ref: stored}, ref, type(fingerprint)) == fingerprint


@pytest.mark.parametrize("change", ("extra", "missing", "bad_digest"))
def test_sandbox_probe_rejects_malformed_nested_wire(change):
    wire = _wire_body()
    if change == "extra":
        wire["observations"][0]["unexpected"] = True
    elif change == "missing":
        del wire["observations"][0]["decision_json_utf8"]
    else:
        wire["fingerprint_sha256"] = "0" * 64
    with pytest.raises((ArtifactSchemaFailureV5, ValueError)):
        _worker(wire).fingerprint()


def test_sandbox_probe_rejects_wrong_request_identity():
    with pytest.raises(ValueError, match="identity differs"):
        _worker(_wire_body(), request_override="0" * 64).fingerprint()
