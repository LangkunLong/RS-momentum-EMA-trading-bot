"""Authenticated JSON-lines protocol for one untrusted stateless policy worker."""

from __future__ import annotations

import base64
from collections import OrderedDict
from dataclasses import dataclass, fields
import hashlib
import hmac
import json
import secrets
import sys
from typing import Mapping

from .contracts import (
    AllocationDecision,
    AllocationSnapshot,
    CapacityDecision,
    CapacitySnapshot,
    EntryDecision,
    EntrySnapshot,
    EvictionDecision,
    EvictionSnapshot,
    ExitDecision,
    ExitSnapshot,
)


MAX_POLICY_LINE_BYTES = 16 * 1024
POLICY_METHODS = (
    "evaluate_entry",
    "recommend_capacity",
    "recommend_allocation",
    "select_eviction",
    "evaluate_exit",
)
_METHOD_TYPES = {
    "evaluate_entry": (EntrySnapshot, EntryDecision),
    "recommend_capacity": (CapacitySnapshot, CapacityDecision),
    "recommend_allocation": (AllocationSnapshot, AllocationDecision),
    "select_eviction": (EvictionSnapshot, EvictionDecision),
    "evaluate_exit": (ExitSnapshot, ExitDecision),
}


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("policy JSON contains a duplicate field")
        result[key] = value
    return result


def _closed_json(raw: str, expected: frozenset[str]) -> dict[str, object]:
    if not isinstance(raw, str) or "\r" in raw or len(raw.encode("utf-8")) > MAX_POLICY_LINE_BYTES:
        raise ValueError("policy JSON line limit exceeded")
    raw = raw.removesuffix("\n")
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise ValueError("policy JSON is malformed") from exc
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("policy JSON fields are invalid")
    return value


def _b64(value: str, size: int, label: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"worker {label} is invalid") from exc
    if len(decoded) != size or base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError(f"worker {label} is invalid")
    return decoded


def _sha256(value: str, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"policy {label} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class WorkerBootstrap:
    schema_version: int
    interface_version: int
    nonce_b64: str
    hmac_key_b64: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("worker bootstrap schema is unsupported")
        if type(self.interface_version) is not int or self.interface_version <= 0:
            raise ValueError("worker bootstrap interface is invalid")
        _b64(self.nonce_b64, 16, "nonce")
        _b64(self.hmac_key_b64, 32, "HMAC key")

    @classmethod
    def create(cls, *, interface_version: int) -> "WorkerBootstrap":
        return cls(
            schema_version=1,
            interface_version=interface_version,
            nonce_b64=base64.b64encode(secrets.token_bytes(16)).decode("ascii"),
            hmac_key_b64=base64.b64encode(secrets.token_bytes(32)).decode("ascii"),
        )

    def to_json(self) -> str:
        return _canonical_bytes(
            {
                "schema_version": self.schema_version,
                "interface_version": self.interface_version,
                "nonce_b64": self.nonce_b64,
                "hmac_key_b64": self.hmac_key_b64,
            }
        ).decode("utf-8")

    @classmethod
    def from_json(cls, raw: str) -> "WorkerBootstrap":
        value = _closed_json(
            raw,
            frozenset(
                {"schema_version", "interface_version", "nonce_b64", "hmac_key_b64"}
            ),
        )
        return cls(**value)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class WorkerReady:
    schema_version: int
    interface_version: int
    status: str
    hmac_sha256: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("worker ready schema is unsupported")
        if type(self.interface_version) is not int or self.interface_version <= 0:
            raise ValueError("worker ready interface is invalid")
        if self.status != "ready":
            raise ValueError("worker ready status is invalid")
        _sha256(self.hmac_sha256, "ready HMAC")


@dataclass(frozen=True, slots=True)
class PolicyRequestEnvelope:
    sequence: int
    previous_hmac_sha256: str
    method: str
    payload_sha256: str
    payload: Mapping[str, object]
    hmac_sha256: str

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("policy sequence is invalid")
        _sha256(self.previous_hmac_sha256, "request chain")
        if self.method not in _METHOD_TYPES:
            raise ValueError("policy method is invalid")
        _sha256(self.payload_sha256, "request payload hash")
        if not isinstance(self.payload, Mapping):
            raise ValueError("policy payload is invalid")
        _sha256(self.hmac_sha256, "request HMAC")


@dataclass(frozen=True, slots=True)
class PolicyResponseEnvelope:
    sequence: int
    request_hmac_sha256: str
    method: str
    payload_sha256: str
    payload: Mapping[str, object]
    hmac_sha256: str

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("policy sequence is invalid")
        _sha256(self.request_hmac_sha256, "response request binding")
        if self.method not in _METHOD_TYPES:
            raise ValueError("policy method is invalid")
        _sha256(self.payload_sha256, "response payload hash")
        if not isinstance(self.payload, Mapping):
            raise ValueError("policy payload is invalid")
        _sha256(self.hmac_sha256, "response HMAC")


def initial_chain_sha256(bootstrap: WorkerBootstrap) -> str:
    if not isinstance(bootstrap, WorkerBootstrap):
        raise ValueError("worker bootstrap is invalid")
    return hashlib.sha256(_b64(bootstrap.nonce_b64, 16, "nonce")).hexdigest()


def _envelope_hmac(bootstrap: WorkerBootstrap, primitive: Mapping[str, object]) -> str:
    nonce = _b64(bootstrap.nonce_b64, 16, "nonce")
    key = _b64(bootstrap.hmac_key_b64, 32, "HMAC key")
    return hmac.new(key, nonce + b"\n" + _canonical_bytes(primitive), hashlib.sha256).hexdigest()


def encode_worker_ready(*, bootstrap: WorkerBootstrap) -> str:
    if not isinstance(bootstrap, WorkerBootstrap):
        raise ValueError("worker bootstrap is invalid")
    unsigned = {
        "schema_version": 1,
        "interface_version": bootstrap.interface_version,
        "status": "ready",
    }
    ready = WorkerReady(
        **unsigned,
        hmac_sha256=_envelope_hmac(bootstrap, unsigned),
    )
    return _canonical_bytes(
        {field.name: getattr(ready, field.name) for field in fields(ready)}
    ).decode("utf-8")


def decode_worker_ready(raw: str, *, bootstrap: WorkerBootstrap) -> WorkerReady:
    if not isinstance(bootstrap, WorkerBootstrap):
        raise ValueError("worker bootstrap is invalid")
    value = _closed_json(
        raw,
        frozenset({"schema_version", "interface_version", "status", "hmac_sha256"}),
    )
    ready = WorkerReady(**value)  # type: ignore[arg-type]
    if ready.interface_version != bootstrap.interface_version:
        raise ValueError("worker ready interface differs")
    unsigned = {key: item for key, item in value.items() if key != "hmac_sha256"}
    if not hmac.compare_digest(
        ready.hmac_sha256,
        _envelope_hmac(bootstrap, unsigned),
    ):
        raise ValueError("worker ready HMAC is invalid")
    return ready


def _require_method_pair(method: str, value: object, *, response: bool) -> type[object]:
    if method not in _METHOD_TYPES:
        raise ValueError("policy method is invalid")
    expected = _METHOD_TYPES[method][1 if response else 0]
    if type(value) is not expected:
        raise ValueError("policy method/payload pairing is invalid")
    return expected


def _contract_from_payload(contract_type: type[object], payload: object) -> object:
    """Rebuild interface-v2 contracts, including their nested market context."""
    if not isinstance(payload, dict):
        raise ValueError("policy payload is invalid")
    raw = _canonical_bytes(payload).decode("utf-8")
    try:
        return contract_type.from_canonical_json(raw)  # type: ignore[attr-defined]
    except (TypeError, ValueError) as exc:
        raise ValueError("policy method/payload pairing is invalid") from exc


def encode_policy_request(
    *,
    bootstrap: WorkerBootstrap,
    sequence: int,
    previous_hmac_sha256: str,
    method: str,
    snapshot: object,
) -> tuple[str, PolicyRequestEnvelope]:
    _require_method_pair(method, snapshot, response=False)
    if type(sequence) is not int or sequence <= 0:
        raise ValueError("policy sequence is invalid")
    _sha256(previous_hmac_sha256, "request chain")
    payload = snapshot.to_primitive()  # type: ignore[attr-defined]
    payload_sha256 = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    unsigned = {
        "sequence": sequence,
        "previous_hmac_sha256": previous_hmac_sha256,
        "method": method,
        "payload_sha256": payload_sha256,
        "payload": payload,
    }
    envelope = PolicyRequestEnvelope(
        **unsigned,
        hmac_sha256=_envelope_hmac(bootstrap, unsigned),
    )
    raw = _canonical_bytes(
        {field.name: getattr(envelope, field.name) for field in fields(envelope)}
    ).decode("utf-8")
    if len(raw.encode("utf-8")) > MAX_POLICY_LINE_BYTES:
        raise ValueError("policy JSON line limit exceeded")
    return raw, envelope


def decode_policy_request(
    raw: str,
    *,
    bootstrap: WorkerBootstrap,
    expected_sequence: int,
    expected_previous_hmac_sha256: str,
) -> tuple[PolicyRequestEnvelope, object]:
    value = _closed_json(
        raw,
        frozenset(
            {
                "sequence",
                "previous_hmac_sha256",
                "method",
                "payload_sha256",
                "payload",
                "hmac_sha256",
            }
        ),
    )
    if (
        type(expected_sequence) is not int
        or expected_sequence <= 0
        or type(value["sequence"]) is not int
        or value["sequence"] != expected_sequence
    ):
        raise ValueError("policy sequence is invalid")
    if value["previous_hmac_sha256"] != expected_previous_hmac_sha256:
        raise ValueError("policy request chain is invalid")
    method = value["method"]
    if not isinstance(method, str) or method not in _METHOD_TYPES:
        raise ValueError("policy method is invalid")
    expected_type = _METHOD_TYPES[method][0]
    parsed = _contract_from_payload(expected_type, value["payload"])
    payload_sha256 = hashlib.sha256(_canonical_bytes(value["payload"])).hexdigest()
    if value["payload_sha256"] != payload_sha256:
        raise ValueError("policy payload hash is invalid")
    unsigned = {key: item for key, item in value.items() if key != "hmac_sha256"}
    supplied_hmac = _sha256(value["hmac_sha256"], "request HMAC")  # type: ignore[arg-type]
    if not hmac.compare_digest(supplied_hmac, _envelope_hmac(bootstrap, unsigned)):
        raise ValueError("policy request HMAC is invalid")
    envelope = PolicyRequestEnvelope(**value)  # type: ignore[arg-type]
    return envelope, parsed


def encode_policy_response(
    *,
    bootstrap: WorkerBootstrap,
    sequence: int,
    request_hmac_sha256: str,
    method: str,
    decision: object,
) -> tuple[str, PolicyResponseEnvelope]:
    _require_method_pair(method, decision, response=True)
    if type(sequence) is not int or sequence <= 0:
        raise ValueError("policy sequence is invalid")
    _sha256(request_hmac_sha256, "response request binding")
    payload = decision.to_primitive()  # type: ignore[attr-defined]
    payload_sha256 = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    unsigned = {
        "sequence": sequence,
        "request_hmac_sha256": request_hmac_sha256,
        "method": method,
        "payload_sha256": payload_sha256,
        "payload": payload,
    }
    envelope = PolicyResponseEnvelope(
        **unsigned,
        hmac_sha256=_envelope_hmac(bootstrap, unsigned),
    )
    raw = _canonical_bytes(
        {field.name: getattr(envelope, field.name) for field in fields(envelope)}
    ).decode("utf-8")
    if len(raw.encode("utf-8")) > MAX_POLICY_LINE_BYTES:
        raise ValueError("policy JSON line limit exceeded")
    return raw, envelope


def decode_policy_response(
    raw: str,
    *,
    bootstrap: WorkerBootstrap,
    expected_sequence: int,
    expected_request_hmac_sha256: str,
    expected_method: str,
) -> tuple[PolicyResponseEnvelope, object]:
    value = _closed_json(
        raw,
        frozenset(
            {
                "sequence",
                "request_hmac_sha256",
                "method",
                "payload_sha256",
                "payload",
                "hmac_sha256",
            }
        ),
    )
    if (
        type(expected_sequence) is not int
        or expected_sequence <= 0
        or type(value["sequence"]) is not int
        or value["sequence"] != expected_sequence
    ):
        raise ValueError("policy sequence is invalid")
    if value["request_hmac_sha256"] != expected_request_hmac_sha256:
        raise ValueError("policy response request binding is invalid")
    if value["method"] != expected_method:
        raise ValueError("policy response method binding is invalid")
    if expected_method not in _METHOD_TYPES:
        raise ValueError("policy method is invalid")
    expected_type = _METHOD_TYPES[expected_method][1]
    parsed = _contract_from_payload(expected_type, value["payload"])
    payload_sha256 = hashlib.sha256(_canonical_bytes(value["payload"])).hexdigest()
    if value["payload_sha256"] != payload_sha256:
        raise ValueError("policy payload hash is invalid")
    unsigned = {key: item for key, item in value.items() if key != "hmac_sha256"}
    supplied_hmac = _sha256(value["hmac_sha256"], "response HMAC")  # type: ignore[arg-type]
    if not hmac.compare_digest(supplied_hmac, _envelope_hmac(bootstrap, unsigned)):
        raise ValueError("policy response HMAC is invalid")
    envelope = PolicyResponseEnvelope(**value)  # type: ignore[arg-type]
    return envelope, parsed


@dataclass(frozen=True, slots=True)
class PolicyDeterminismProbe:
    method: str
    repeated_snapshot: object
    unrelated_snapshot: object

    def __post_init__(self) -> None:
        _require_method_pair(self.method, self.repeated_snapshot, response=False)
        _require_method_pair(self.method, self.unrelated_snapshot, response=False)
        if _canonical_bytes(self.repeated_snapshot.to_primitive()) == _canonical_bytes(  # type: ignore[attr-defined]
            self.unrelated_snapshot.to_primitive()  # type: ignore[attr-defined]
        ):
            raise ValueError("policy determinism probe snapshots must differ")


def validate_policy_determinism_probes(
    probes: tuple[object, ...],
) -> tuple[PolicyDeterminismProbe, ...]:
    """Validate the bounded, method-unique probe set before worker allocation."""
    if (
        type(probes) is not tuple
        or not 1 <= len(probes) <= 5
        or any(type(item) is not PolicyDeterminismProbe for item in probes)
        or len({item.method for item in probes}) != len(probes)
    ):
        raise ValueError("policy determinism probes are invalid")
    return probes  # type: ignore[return-value]


class DecisionDeterminismGuard:
    """Remember bounded snapshot results and reject state-dependent changes."""

    def __init__(self, *, max_observations: int = 32) -> None:
        if type(max_observations) is not int or not 1 <= max_observations <= 32:
            raise ValueError("policy determinism observation cap is invalid")
        self._max_observations = max_observations
        self._observed: OrderedDict[tuple[str, bytes], bytes] = OrderedDict()

    @property
    def observed_count(self) -> int:
        return len(self._observed)

    def observe(self, method: str, snapshot: object, decision: object) -> None:
        _require_method_pair(method, snapshot, response=False)
        _require_method_pair(method, decision, response=True)
        key = (method, _canonical_bytes(snapshot.to_primitive()))  # type: ignore[attr-defined]
        rendered = _canonical_bytes(decision.to_primitive())  # type: ignore[attr-defined]
        previous = self._observed.get(key)
        if previous is None:
            if len(self._observed) == self._max_observations:
                self._observed.popitem(last=False)
            self._observed[key] = rendered
            return
        self._observed.move_to_end(key)
        if not hmac.compare_digest(previous, rendered):
            raise ValueError("candidate_nondeterminism")


def worker_main() -> int:
    """Run one trusted wrapper around candidate policy modules."""
    from . import entry, exit, risk

    bootstrap_raw = sys.stdin.buffer.readline(MAX_POLICY_LINE_BYTES + 2)
    if not bootstrap_raw or len(bootstrap_raw) > MAX_POLICY_LINE_BYTES + 1:
        return 2
    try:
        bootstrap = WorkerBootstrap.from_json(bootstrap_raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, ValueError):
        return 2
    dispatch = {
        "evaluate_entry": entry.evaluate_entry,
        "recommend_capacity": risk.recommend_capacity,
        "recommend_allocation": risk.recommend_allocation,
        "select_eviction": risk.select_eviction,
        "evaluate_exit": exit.evaluate_exit,
    }
    try:
        ready = encode_worker_ready(bootstrap=bootstrap)
        sys.stdout.buffer.write(ready.encode("utf-8") + b"\n")
        sys.stdout.buffer.flush()
    except (OSError, ValueError):
        return 2
    sequence = 1
    previous_hmac = initial_chain_sha256(bootstrap)
    while True:
        raw = sys.stdin.buffer.readline(MAX_POLICY_LINE_BYTES + 2)
        if not raw:
            return 0
        if len(raw) > MAX_POLICY_LINE_BYTES + 1:
            return 3
        try:
            request, snapshot = decode_policy_request(
                raw.decode("utf-8", errors="strict"),
                bootstrap=bootstrap,
                expected_sequence=sequence,
                expected_previous_hmac_sha256=previous_hmac,
            )
            decision = dispatch[request.method](snapshot)
            line, _response = encode_policy_response(
                bootstrap=bootstrap,
                sequence=sequence,
                request_hmac_sha256=request.hmac_sha256,
                method=request.method,
                decision=decision,
            )
            sys.stdout.buffer.write(line.encode("utf-8") + b"\n")
            sys.stdout.buffer.flush()
        except (UnicodeDecodeError, ValueError, TypeError):
            return 3
        previous_hmac = request.hmac_sha256
        sequence += 1


if __name__ == "__main__":
    raise SystemExit(worker_main())


__all__ = [
    "DecisionDeterminismGuard",
    "MAX_POLICY_LINE_BYTES",
    "POLICY_METHODS",
    "PolicyDeterminismProbe",
    "PolicyRequestEnvelope",
    "PolicyResponseEnvelope",
    "WorkerBootstrap",
    "WorkerReady",
    "decode_worker_ready",
    "decode_policy_request",
    "decode_policy_response",
    "encode_policy_request",
    "encode_policy_response",
    "encode_worker_ready",
    "initial_chain_sha256",
    "validate_policy_determinism_probes",
    "worker_main",
]
