"""Canonical pre/post extraction evidence for the PIT strategy-policy boundary."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from collections import Counter
import argparse
import hashlib
import json
import math
import re
import subprocess
from typing import Callable, Iterable, Mapping, Sequence

from core.pit_optimizer_evaluation import (
    AggregateMetric,
    FoldAggregateSummary,
    FoldManifest,
    FoldSpec,
)


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_HEAD_RE = re.compile(r"[0-9a-f]{40}")
_REFERENCE_SCHEMA_VERSION = 1
_ATTESTATION_SCHEMA_VERSION = 1
_DISCOVERY_WINDOWS = (
    ("discovery_1", "2021-06-25", "2021-09-20"),
    ("discovery_2", "2021-09-21", "2021-12-14"),
)
_HIDDEN_WINDOW = ("hidden_1", "2021-12-15", "2022-03-11")


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _require_digest(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


def _require_head(value: str, label: str) -> None:
    if not isinstance(value, str) or _HEAD_RE.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


def _finite(value: float, label: str) -> None:
    if isinstance(value, bool) or type(value) not in {int, float} or not math.isfinite(float(value)):
        raise ValueError(f"{label} must be finite")


@dataclass(frozen=True, slots=True)
class ParityTransaction:
    date: str
    symbol: str
    from_symbol: str | None
    action: str
    price: float
    quantity: float
    value: float
    reason: str

    def __post_init__(self) -> None:
        if not all(isinstance(item, str) and item for item in (self.date, self.symbol, self.action, self.reason)):
            raise ValueError("parity transaction text is invalid")
        if self.from_symbol is not None and (not isinstance(self.from_symbol, str) or not self.from_symbol):
            raise ValueError("parity transaction from_symbol is invalid")
        for name in ("price", "quantity", "value"):
            _finite(getattr(self, name), f"parity transaction {name}")


@dataclass(frozen=True, slots=True)
class ParityEntryOutcome:
    symbol: str
    signal_date: str
    entry_date: str
    pivot: float | None
    buy_zone_lower: float | None
    buy_zone_upper: float | None
    entry_open: float | None
    outcome: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(item, str) and item for item in (self.symbol, self.signal_date, self.entry_date, self.outcome)
        ):
            raise ValueError("parity entry outcome text is invalid")
        for name in ("pivot", "buy_zone_lower", "buy_zone_upper", "entry_open"):
            value = getattr(self, name)
            if value is not None:
                _finite(value, f"parity entry outcome {name}")


@dataclass(frozen=True, slots=True)
class ParityEquityPoint:
    session: str
    equity: float

    def __post_init__(self) -> None:
        if not isinstance(self.session, str) or not self.session:
            raise ValueError("parity equity session is invalid")
        _finite(self.equity, "parity equity")


@dataclass(frozen=True, slots=True)
class ParityFoldEvidence:
    fold_id: str
    transactions: tuple[ParityTransaction, ...]
    entry_outcomes: tuple[ParityEntryOutcome, ...]
    equity: tuple[ParityEquityPoint, ...]
    funnel: tuple[AggregateMetric, ...]
    aggregate: FoldAggregateSummary
    effective_policy_sha256: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.fold_id, str) or not self.fold_id:
            raise ValueError("parity evidence fold_id is invalid")
        expected_types = (
            (self.transactions, ParityTransaction, "transactions"),
            (self.entry_outcomes, ParityEntryOutcome, "entry outcomes"),
            (self.equity, ParityEquityPoint, "equity"),
            (self.funnel, AggregateMetric, "funnel"),
        )
        for values, expected, label in expected_types:
            if type(values) is not tuple or any(not isinstance(value, expected) for value in values):
                raise ValueError(f"parity evidence {label} are invalid")
        if self.aggregate.fold_id != self.fold_id:
            raise ValueError("parity aggregate fold differs from evidence")
        _require_digest(self.effective_policy_sha256, "effective policy digest")
        _require_digest(self.evidence_sha256, "parity evidence digest")
        primitive = asdict(self)
        primitive.pop("evidence_sha256")
        if _digest(primitive) != self.evidence_sha256:
            raise ValueError("parity evidence digest mismatch")


@dataclass(frozen=True, slots=True)
class ParityReference:
    schema_version: int
    reference_source_head: str
    reference_source_fingerprint_sha256: str
    readiness_sha256: str
    pit_bundle_sha256: str
    baseline_manifest_sha256: str
    effective_policy_sha256: str
    fold_manifest: FoldManifest
    universe: tuple[str, ...]
    discovery_evidence: tuple[ParityFoldEvidence, ...]
    discovery_output_sha256s: tuple[tuple[str, str], ...]
    artifact_path: Path
    artifact_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != _REFERENCE_SCHEMA_VERSION:
            raise ValueError("parity reference schema is unsupported")
        _require_head(self.reference_source_head, "reference source head")
        for name in (
            "reference_source_fingerprint_sha256",
            "readiness_sha256",
            "pit_bundle_sha256",
            "baseline_manifest_sha256",
            "effective_policy_sha256",
            "artifact_sha256",
        ):
            _require_digest(getattr(self, name), name)
        if not isinstance(self.fold_manifest, FoldManifest):
            raise ValueError("parity reference fold manifest is invalid")
        if (
            type(self.universe) is not tuple
            or not self.universe
            or any(not isinstance(symbol, str) or not symbol for symbol in self.universe)
        ):
            raise ValueError("parity reference universe is invalid")
        if len(set(self.universe)) != len(self.universe):
            raise ValueError("parity reference universe contains duplicates")
        if type(self.discovery_evidence) is not tuple or len(self.discovery_evidence) != 2:
            raise ValueError("parity reference requires two discovery evidence records")
        expected_ids = tuple(fold.fold_id for fold in self.fold_manifest.discovery_folds)
        if tuple(evidence.fold_id for evidence in self.discovery_evidence) != expected_ids:
            raise ValueError("parity reference evidence fold order is invalid")
        expected_outputs = tuple((evidence.fold_id, evidence.evidence_sha256) for evidence in self.discovery_evidence)
        if self.discovery_output_sha256s != expected_outputs:
            raise ValueError("parity reference output digests are invalid")
        if any(
            evidence.effective_policy_sha256 != self.effective_policy_sha256 for evidence in self.discovery_evidence
        ):
            raise ValueError("parity reference effective policy differs by fold")
        if not isinstance(self.artifact_path, Path) or not self.artifact_path.is_absolute():
            raise ValueError("parity reference artifact path is invalid")


@dataclass(frozen=True, slots=True)
class ParityAttestation:
    schema_version: int
    reference_artifact_sha256: str
    reference_source_head: str
    final_source_head: str
    final_source_fingerprint_sha256: str
    pit_bundle_sha256: str
    baseline_manifest_sha256: str
    effective_policy_sha256: str
    discovery_fold_manifest_sha256: str
    policy_interface_version: int
    reference_output_sha256s: tuple[tuple[str, str], ...]
    final_output_sha256s: tuple[tuple[str, str], ...]
    final_discovery_evidence: tuple[ParityFoldEvidence, ...]
    transactions_equal: bool
    entry_outcomes_equal: bool
    equity_equal: bool
    funnels_equal: bool
    effective_policy_equal: bool
    artifact_path: Path
    artifact_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != _ATTESTATION_SCHEMA_VERSION:
            raise ValueError("parity attestation schema is unsupported")
        _require_head(self.reference_source_head, "reference source head")
        _require_head(self.final_source_head, "final source head")
        for name in (
            "reference_artifact_sha256",
            "final_source_fingerprint_sha256",
            "pit_bundle_sha256",
            "baseline_manifest_sha256",
            "effective_policy_sha256",
            "discovery_fold_manifest_sha256",
            "artifact_sha256",
        ):
            _require_digest(getattr(self, name), name)
        if type(self.policy_interface_version) is not int or self.policy_interface_version < 1:
            raise ValueError("policy interface version is invalid")
        flags = (
            self.transactions_equal,
            self.entry_outcomes_equal,
            self.equity_equal,
            self.funnels_equal,
            self.effective_policy_equal,
        )
        if any(type(flag) is not bool for flag in flags) or not all(flags):
            raise ValueError("parity attestation may contain only equal evidence")
        if not isinstance(self.artifact_path, Path) or not self.artifact_path.is_absolute():
            raise ValueError("parity attestation artifact path is invalid")


def _reference_primitive(reference: ParityReference) -> dict[str, object]:
    primitive = asdict(reference)
    primitive.pop("artifact_path")
    primitive.pop("artifact_sha256")
    return primitive


def _attestation_primitive(attestation: ParityAttestation) -> dict[str, object]:
    primitive = asdict(attestation)
    primitive.pop("artifact_path")
    primitive.pop("artifact_sha256")
    return primitive


def _write_create_only(output: Path, primitive: Mapping[str, object]) -> tuple[Path, str]:
    path = Path(output).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical_json_bytes(primitive)
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
    return path, hashlib.sha256(payload).hexdigest()


def persist_parity_reference(
    *,
    output: Path,
    reference_source_head: str,
    reference_source_fingerprint_sha256: str,
    readiness_sha256: str,
    pit_bundle_sha256: str,
    baseline_manifest_sha256: str,
    effective_policy_sha256: str,
    fold_manifest: FoldManifest,
    universe: tuple[str, ...],
    discovery_evidence: tuple[ParityFoldEvidence, ...],
) -> ParityReference:
    output_sha256s = tuple((evidence.fold_id, evidence.evidence_sha256) for evidence in discovery_evidence)
    path = Path(output).resolve()
    provisional = ParityReference(
        schema_version=_REFERENCE_SCHEMA_VERSION,
        reference_source_head=reference_source_head,
        reference_source_fingerprint_sha256=reference_source_fingerprint_sha256,
        readiness_sha256=readiness_sha256,
        pit_bundle_sha256=pit_bundle_sha256,
        baseline_manifest_sha256=baseline_manifest_sha256,
        effective_policy_sha256=effective_policy_sha256,
        fold_manifest=fold_manifest,
        universe=universe,
        discovery_evidence=discovery_evidence,
        discovery_output_sha256s=output_sha256s,
        artifact_path=path,
        artifact_sha256="0" * 64,
    )
    primitive = _reference_primitive(provisional)
    artifact_sha256 = _digest(primitive)
    path, written_sha256 = _write_create_only(path, primitive)
    if written_sha256 != artifact_sha256:
        raise ValueError("parity reference artifact digest mismatch")
    return ParityReference(
        **{
            **asdict(provisional),
            "fold_manifest": fold_manifest,
            "discovery_evidence": discovery_evidence,
            "universe": universe,
            "discovery_output_sha256s": output_sha256s,
            "artifact_path": path,
            "artifact_sha256": artifact_sha256,
        }
    )


def _metric(value: object) -> AggregateMetric:
    return AggregateMetric(**value)  # type: ignore[arg-type]


def _aggregate_from_primitive(value: object) -> FoldAggregateSummary:
    if not isinstance(value, dict):
        raise ValueError("parity aggregate is malformed")
    primitive = dict(value)
    primitive["entry_funnel"] = tuple(_metric(item) for item in primitive.get("entry_funnel", ()))
    primitive["exit_attribution"] = tuple(_metric(item) for item in primitive.get("exit_attribution", ()))
    return FoldAggregateSummary(**primitive)


def _fold_from_primitive(value: object) -> FoldSpec:
    if not isinstance(value, dict):
        raise ValueError("parity fold is malformed")
    primitive = dict(value)
    primitive["sessions"] = tuple(primitive.get("sessions", ()))
    return FoldSpec(**primitive)


def _manifest_from_primitive(value: object) -> FoldManifest:
    if not isinstance(value, dict):
        raise ValueError("parity fold manifest is malformed")
    primitive = dict(value)
    primitive["discovery_folds"] = tuple(_fold_from_primitive(item) for item in primitive.get("discovery_folds", ()))
    primitive["hidden_fold"] = _fold_from_primitive(primitive.get("hidden_fold"))
    return FoldManifest(**primitive)


def _evidence_from_primitive(value: object) -> ParityFoldEvidence:
    if not isinstance(value, dict):
        raise ValueError("parity fold evidence is malformed")
    primitive = dict(value)
    primitive["transactions"] = tuple(ParityTransaction(**item) for item in primitive.get("transactions", ()))
    primitive["entry_outcomes"] = tuple(ParityEntryOutcome(**item) for item in primitive.get("entry_outcomes", ()))
    primitive["equity"] = tuple(ParityEquityPoint(**item) for item in primitive.get("equity", ()))
    primitive["funnel"] = tuple(_metric(item) for item in primitive.get("funnel", ()))
    primitive["aggregate"] = _aggregate_from_primitive(primitive.get("aggregate"))
    return ParityFoldEvidence(**primitive)


def load_parity_reference(path: Path) -> ParityReference:
    resolved = Path(path).resolve()
    raw = resolved.read_bytes()
    try:
        primitive = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("parity reference JSON is invalid") from exc
    if not isinstance(primitive, dict) or raw != _canonical_json_bytes(primitive):
        raise ValueError("parity reference is not canonical JSON")
    artifact_sha256 = hashlib.sha256(raw).hexdigest()
    value = dict(primitive)
    value["fold_manifest"] = _manifest_from_primitive(value.get("fold_manifest"))
    value["universe"] = tuple(value.get("universe", ()))
    value["discovery_evidence"] = tuple(_evidence_from_primitive(item) for item in value.get("discovery_evidence", ()))
    value["discovery_output_sha256s"] = tuple(tuple(item) for item in value.get("discovery_output_sha256s", ()))
    return ParityReference(
        **value,
        artifact_path=resolved,
        artifact_sha256=artifact_sha256,
    )


def verify_parity_evidence(
    *,
    reference: ParityReference,
    output: Path,
    final_source_head: str,
    final_source_fingerprint_sha256: str,
    policy_interface_version: int,
    final_discovery_evidence: tuple[ParityFoldEvidence, ...],
) -> ParityAttestation:
    if tuple(item.fold_id for item in final_discovery_evidence) != tuple(
        item.fold_id for item in reference.discovery_evidence
    ):
        raise ValueError("final parity folds differ from reference")
    transactions_equal = all(
        left.transactions == right.transactions
        for left, right in zip(reference.discovery_evidence, final_discovery_evidence, strict=True)
    )
    entry_outcomes_equal = all(
        left.entry_outcomes == right.entry_outcomes
        for left, right in zip(reference.discovery_evidence, final_discovery_evidence, strict=True)
    )
    equity_equal = all(
        left.equity == right.equity
        for left, right in zip(reference.discovery_evidence, final_discovery_evidence, strict=True)
    )
    funnels_equal = all(
        left.funnel == right.funnel and left.aggregate == right.aggregate
        for left, right in zip(reference.discovery_evidence, final_discovery_evidence, strict=True)
    )
    effective_policy_equal = all(
        item.effective_policy_sha256 == reference.effective_policy_sha256 for item in final_discovery_evidence
    )
    failures = [
        label
        for label, equal in (
            ("transactions", transactions_equal),
            ("entry outcomes", entry_outcomes_equal),
            ("equity", equity_equal),
            ("funnels", funnels_equal),
            ("effective policy", effective_policy_equal),
        )
        if not equal
    ]
    if failures:
        raise ValueError("parity differs in " + ", ".join(failures))
    final_output_sha256s = tuple((item.fold_id, item.evidence_sha256) for item in final_discovery_evidence)
    path = Path(output).resolve()
    provisional = ParityAttestation(
        schema_version=_ATTESTATION_SCHEMA_VERSION,
        reference_artifact_sha256=reference.artifact_sha256,
        reference_source_head=reference.reference_source_head,
        final_source_head=final_source_head,
        final_source_fingerprint_sha256=final_source_fingerprint_sha256,
        pit_bundle_sha256=reference.pit_bundle_sha256,
        baseline_manifest_sha256=reference.baseline_manifest_sha256,
        effective_policy_sha256=reference.effective_policy_sha256,
        discovery_fold_manifest_sha256=reference.fold_manifest.sha256,
        policy_interface_version=policy_interface_version,
        reference_output_sha256s=reference.discovery_output_sha256s,
        final_output_sha256s=final_output_sha256s,
        final_discovery_evidence=final_discovery_evidence,
        transactions_equal=True,
        entry_outcomes_equal=True,
        equity_equal=True,
        funnels_equal=True,
        effective_policy_equal=True,
        artifact_path=path,
        artifact_sha256="0" * 64,
    )
    primitive = _attestation_primitive(provisional)
    artifact_sha256 = _digest(primitive)
    path, written_sha256 = _write_create_only(path, primitive)
    if written_sha256 != artifact_sha256:
        raise ValueError("parity attestation artifact digest mismatch")
    return ParityAttestation(
        **{
            **asdict(provisional),
            "final_discovery_evidence": final_discovery_evidence,
            "reference_output_sha256s": reference.discovery_output_sha256s,
            "final_output_sha256s": final_output_sha256s,
            "artifact_path": path,
            "artifact_sha256": artifact_sha256,
        }
    )


def build_fixed_fold_manifest(
    *,
    readiness: Mapping[str, object],
    benchmark_sessions: Iterable[str],
    data_identity_sha256: str,
) -> tuple[FoldManifest, tuple[str, ...]]:
    """Seal the fixed two-discovery/one-hidden split from calendar labels only."""

    _require_digest(data_identity_sha256, "fold data identity")
    evaluation = readiness.get("evaluation_contract")
    if not isinstance(evaluation, Mapping) or evaluation.get("verification_only") is not True:
        raise ValueError("readiness is not the authenticated verification subset")
    scope = evaluation.get("scope")
    if not isinstance(scope, Mapping):
        raise ValueError("readiness verification scope is absent")
    expected_scope = {
        "benchmark": "SPY",
        "discovery_start": _DISCOVERY_WINDOWS[0][1],
        "discovery_end": _DISCOVERY_WINDOWS[0][2],
        "holdout_start": _DISCOVERY_WINDOWS[1][1],
        "holdout_end": _DISCOVERY_WINDOWS[1][2],
        "warmup_start": "2021-01-01",
        "session_count": 60,
    }
    for name, expected in expected_scope.items():
        if scope.get(name) != expected:
            raise ValueError(f"readiness verification scope {name} is invalid")
    raw_symbols = scope.get("symbols")
    if not isinstance(raw_symbols, list) or any(not isinstance(item, str) or not item for item in raw_symbols):
        raise ValueError("readiness verification universe is invalid")
    universe = tuple(raw_symbols)
    if len(universe) != scope.get("symbol_count") or len(set(universe)) != len(universe):
        raise ValueError("readiness verification universe count is invalid")
    calendar = tuple(str(item) for item in benchmark_sessions)
    if len(calendar) != len(set(calendar)) or tuple(sorted(calendar)) != calendar:
        raise ValueError("benchmark sessions are not unique and chronological")

    def fold(fold_id: str, purpose: str, start: str, end: str) -> FoldSpec:
        sessions = tuple(session for session in calendar if start <= session <= end)
        return FoldSpec(fold_id, purpose, start, end, sessions)

    discoveries = tuple(fold(fold_id, "discovery", start, end) for fold_id, start, end in _DISCOVERY_WINDOWS)
    hidden = fold(_HIDDEN_WINDOW[0], "hidden", _HIDDEN_WINDOW[1], _HIDDEN_WINDOW[2])
    universe_sha256 = _digest(list(universe))
    return (
        FoldManifest(
            data_identity_sha256=data_identity_sha256,
            universe_sha256=universe_sha256,
            benchmark=str(scope["benchmark"]),
            warmup_start_date=str(scope["warmup_start"]),
            discovery_folds=discoveries,
            hidden_fold=hidden,
        ),
        universe,
    )


def _sealed_evidence(
    *,
    fold_id: str,
    transactions: tuple[ParityTransaction, ...],
    entry_outcomes: tuple[ParityEntryOutcome, ...],
    equity: tuple[ParityEquityPoint, ...],
    funnel: tuple[AggregateMetric, ...],
    aggregate: FoldAggregateSummary,
    effective_policy_sha256: str,
) -> ParityFoldEvidence:
    primitive = {
        "fold_id": fold_id,
        "transactions": [asdict(item) for item in transactions],
        "entry_outcomes": [asdict(item) for item in entry_outcomes],
        "equity": [asdict(item) for item in equity],
        "funnel": [asdict(item) for item in funnel],
        "aggregate": asdict(aggregate),
        "effective_policy_sha256": effective_policy_sha256,
    }
    return ParityFoldEvidence(
        fold_id=fold_id,
        transactions=transactions,
        entry_outcomes=entry_outcomes,
        equity=equity,
        funnel=funnel,
        aggregate=aggregate,
        effective_policy_sha256=effective_policy_sha256,
        evidence_sha256=_digest(primitive),
    )


def build_fold_evidence(*, fold: FoldSpec, result: object) -> ParityFoldEvidence:
    """Convert one simulator result into complete canonical retrievable evidence."""

    equity_curve = getattr(result, "equity_curve", None)
    benchmark_curve = getattr(result, "benchmark_curve", None)
    if equity_curve is None or benchmark_curve is None:
        raise ValueError("simulation result curves are absent")
    equity = tuple(ParityEquityPoint(str(session), float(value)) for session, value in equity_curve.items())
    if tuple(point.session for point in equity) != fold.sessions:
        raise ValueError("simulation equity sessions differ from the fold")
    if tuple(str(session) for session in benchmark_curve.index) != fold.sessions:
        raise ValueError("simulation benchmark sessions differ from the fold")

    transaction_log = getattr(result, "transaction_log", None)
    records = [] if transaction_log is None or transaction_log.empty else transaction_log.to_dict("records")
    transaction_values: list[ParityTransaction] = []
    for row in records:
        raw_from_symbol = row.get("FromTicker")
        from_symbol = (
            None
            if raw_from_symbol is None or (isinstance(raw_from_symbol, float) and math.isnan(raw_from_symbol))
            else str(raw_from_symbol)
        )
        transaction_values.append(
            ParityTransaction(
                date=str(row["Date"]),
                symbol=str(row["Ticker"]),
                from_symbol=from_symbol,
                action=str(row["Action"]),
                price=float(row["Price"]),
                quantity=float(row["Quantity"]),
                value=float(row["Value"]),
                reason=str(row["Reason"]),
            )
        )
    transactions = tuple(transaction_values)
    raw_outcomes = getattr(result, "entry_outcomes", ())
    entry_outcomes = tuple(ParityEntryOutcome(**outcome.to_primitive()) for outcome in raw_outcomes)
    raw_funnel = result.signal_funnel
    funnel = tuple(AggregateMetric(metric_id, raw_funnel[metric_id]) for metric_id in sorted(raw_funnel))
    exit_counts = Counter(str(getattr(trade, "exit_reason", None) or "unknown") for trade in result.closed_trades)
    exit_attribution = tuple(AggregateMetric(reason, count) for reason, count in sorted(exit_counts.items()))
    initial_capital = float(result.initial_capital)
    if not math.isfinite(initial_capital) or initial_capital <= 0:
        raise ValueError("simulation initial capital is invalid")
    turnover_pct = sum(abs(item.value) for item in transactions) / initial_capital * 100.0
    by_session: dict[str, list[ParityTransaction]] = {}
    for transaction in transactions:
        by_session.setdefault(transaction.date, []).append(transaction)
    cash = initial_capital
    exposure_values: list[float] = []
    for point in equity:
        for transaction in by_session.get(point.session, ()):
            if transaction.action == "BUY":
                cash -= transaction.value
            elif transaction.action == "SELL":
                cash += transaction.value
        exposure_values.append(0.0 if point.equity == 0 else (point.equity - cash) / point.equity * 100.0)
    average_exposure_pct = sum(exposure_values) / len(exposure_values) if exposure_values else 0.0
    total_return_pct = float(result.total_return_pct)
    benchmark_return_pct = float(result.benchmark_return_pct)
    aggregate = FoldAggregateSummary(
        fold_id=fold.fold_id,
        total_return_pct=total_return_pct,
        excess_total_return_pp=total_return_pct - benchmark_return_pct,
        max_drawdown_pct=float(result.max_drawdown_pct),
        sharpe_ratio=float(result.sharpe_ratio),
        closed_trades=len(result.closed_trades),
        turnover_pct=turnover_pct,
        average_exposure_pct=average_exposure_pct,
        entry_funnel=funnel,
        exit_attribution=exit_attribution,
    )
    config = getattr(result, "config", None)
    effective_policy_sha256 = config.get("effective_engine_policy_sha256") if isinstance(config, Mapping) else None
    _require_digest(effective_policy_sha256, "simulation effective policy digest")
    return _sealed_evidence(
        fold_id=fold.fold_id,
        transactions=transactions,
        entry_outcomes=entry_outcomes,
        equity=equity,
        funnel=funnel,
        aggregate=aggregate,
        effective_policy_sha256=effective_policy_sha256,
    )


def capture_from_authenticated_inputs(
    *,
    readiness: Mapping[str, object],
    readiness_sha256: str,
    pit_bundle_sha256: str,
    reference_source_head: str,
    reference_source_fingerprint_sha256: str,
    benchmark_sessions: Iterable[str],
    output: Path,
    evaluate_discovery_fold: Callable[[FoldSpec, tuple[str, ...], str], ParityFoldEvidence],
) -> ParityReference:
    """Compose a reference after callers authenticate source, readiness, and bundle."""

    if Path(output).exists():
        raise FileExistsError(Path(output))
    _require_digest(readiness_sha256, "readiness digest")
    identities = readiness.get("identities")
    if not isinstance(identities, Mapping):
        raise ValueError("readiness identities are absent")
    if identities.get("pit_bundle_sha256") != pit_bundle_sha256:
        raise ValueError("readiness PIT bundle identity differs")
    baseline_manifest_sha256 = identities.get("baseline_manifest_sha256")
    effective_policy_sha256 = identities.get("effective_policy_sha256")
    _require_digest(baseline_manifest_sha256, "baseline manifest digest")
    _require_digest(effective_policy_sha256, "effective policy digest")
    manifest, universe = build_fixed_fold_manifest(
        readiness=readiness,
        benchmark_sessions=benchmark_sessions,
        data_identity_sha256=pit_bundle_sha256,
    )
    evidence = tuple(
        evaluate_discovery_fold(fold, universe, manifest.warmup_start_date) for fold in manifest.discovery_folds
    )
    if any(item.effective_policy_sha256 != effective_policy_sha256 for item in evidence):
        raise ValueError("captured fold effective policy differs from readiness")
    return persist_parity_reference(
        output=Path(output),
        reference_source_head=reference_source_head,
        reference_source_fingerprint_sha256=reference_source_fingerprint_sha256,
        readiness_sha256=readiness_sha256,
        pit_bundle_sha256=pit_bundle_sha256,
        baseline_manifest_sha256=baseline_manifest_sha256,
        effective_policy_sha256=effective_policy_sha256,
        fold_manifest=manifest,
        universe=universe,
        discovery_evidence=evidence,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_object(path: Path, label: str) -> tuple[Path, dict[str, object], str]:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError(f"{label} must be a regular non-link file")
    resolved = candidate.resolve()
    raw = resolved.read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} JSON is invalid") from exc
    if not isinstance(value, dict) or raw != _canonical_json_bytes(value):
        raise ValueError(f"{label} is not canonical JSON")
    return resolved, value, hashlib.sha256(raw).hexdigest()


def _source_identity(source_root: Path) -> tuple[str, str]:
    root = Path(source_root).resolve()

    def git(*args: str) -> bytes:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return completed.stdout

    if git("status", "--porcelain", "--untracked-files=no").strip():
        raise ValueError("parity capture requires a clean committed source")
    head = git("rev-parse", "HEAD").decode("ascii").strip()
    _require_head(head, "source head")
    tree = git("ls-tree", "-r", "--full-tree", "HEAD")
    return head, hashlib.sha256(tree).hexdigest()


def _benchmark_calendar(bundle: object, benchmark: str) -> tuple[str, ...]:
    connection = getattr(bundle, "_connection", None)
    if connection is None:
        raise ValueError("PIT bundle benchmark calendar is unavailable")
    rows = connection.execute(
        "SELECT trade_date FROM price WHERE ticker=? AND trade_date>=? AND trade_date<=? ORDER BY trade_date",
        (benchmark, _DISCOVERY_WINDOWS[0][1], _HIDDEN_WINDOW[2]),
    ).fetchall()
    sessions = tuple(str(row[0]) for row in rows)
    if not sessions:
        raise ValueError("PIT bundle benchmark calendar is empty")
    return sessions


def _authenticated_readiness(path: Path) -> tuple[dict[str, object], str]:
    _, readiness, readiness_sha256 = _canonical_object(path, "readiness artifact")
    identities = readiness.get("identities")
    effective_policy = readiness.get("effective_policy")
    if not isinstance(identities, Mapping) or not isinstance(effective_policy, Mapping):
        raise ValueError("readiness identity graph is incomplete")
    from core.engine_policy import effective_engine_policy_sha256

    expected_policy_sha256 = identities.get("effective_policy_sha256")
    if effective_engine_policy_sha256(effective_policy) != expected_policy_sha256:
        raise ValueError("readiness effective policy digest mismatch")
    return readiness, readiness_sha256


def capture_parity_reference(
    *,
    readiness_path: Path,
    pit_bundle_path: Path,
    output: Path,
    source_root: Path | None = None,
) -> ParityReference:
    """Authenticate local inputs and capture the inline-policy discovery reference."""

    root = Path(source_root or Path(__file__).resolve().parents[1]).resolve()
    source_head, source_fingerprint = _source_identity(root)
    readiness, readiness_sha256 = _authenticated_readiness(Path(readiness_path))
    identities = readiness["identities"]
    if not isinstance(identities, Mapping):
        raise ValueError("readiness identities are absent")
    expected_bundle_sha256 = identities.get("pit_bundle_sha256")
    _require_digest(expected_bundle_sha256, "readiness PIT bundle digest")
    bundle_path = Path(pit_bundle_path)
    if bundle_path.is_symlink() or not bundle_path.is_file():
        raise ValueError("PIT bundle must be a regular non-link file")
    if _sha256_file(bundle_path.resolve()) != expected_bundle_sha256:
        raise ValueError("PIT bundle digest differs from readiness")

    from core.backtest_engine import PortfolioSimulator
    from core.pit_data import PITDataBundle

    with PITDataBundle(bundle_path, expected_sha256=expected_bundle_sha256) as bundle:
        evaluation = readiness.get("evaluation_contract")
        scope = evaluation.get("scope") if isinstance(evaluation, Mapping) else None
        benchmark = scope.get("benchmark") if isinstance(scope, Mapping) else None
        if benchmark != "SPY":
            raise ValueError("readiness benchmark is invalid")
        calendar = _benchmark_calendar(bundle, benchmark)
        simulator = PortfolioSimulator(pit_bundle=bundle, benchmark_symbol=benchmark)

        def evaluate(fold: FoldSpec, universe: tuple[str, ...], warmup: str) -> ParityFoldEvidence:
            result = simulator.run(
                list(universe),
                start_date=fold.start_date,
                end_date=fold.end_date,
                history_start_date=warmup,
                benchmark_symbol=benchmark,
            )
            return build_fold_evidence(fold=fold, result=result)

        return capture_from_authenticated_inputs(
            readiness=readiness,
            readiness_sha256=readiness_sha256,
            pit_bundle_sha256=expected_bundle_sha256,
            reference_source_head=source_head,
            reference_source_fingerprint_sha256=source_fingerprint,
            benchmark_sessions=calendar,
            output=Path(output),
            evaluate_discovery_fold=evaluate,
        )


def verify_parity_reference(
    *,
    reference_path: Path,
    pit_bundle_path: Path,
    output: Path,
    source_root: Path | None = None,
) -> ParityAttestation:
    """Re-evaluate discovery at a later clean HEAD and persist equality attestation."""

    root = Path(source_root or Path(__file__).resolve().parents[1]).resolve()
    final_head, final_fingerprint = _source_identity(root)
    reference = load_parity_reference(Path(reference_path))
    bundle_path = Path(pit_bundle_path)
    if bundle_path.is_symlink() or not bundle_path.is_file():
        raise ValueError("PIT bundle must be a regular non-link file")
    if _sha256_file(bundle_path.resolve()) != reference.pit_bundle_sha256:
        raise ValueError("PIT bundle digest differs from parity reference")

    from core.backtest_engine import PortfolioSimulator
    from core.pit_data import PITDataBundle
    from core.strategy_policy import POLICY_INTERFACE_VERSION

    with PITDataBundle(bundle_path, expected_sha256=reference.pit_bundle_sha256) as bundle:
        simulator = PortfolioSimulator(
            pit_bundle=bundle,
            benchmark_symbol=reference.fold_manifest.benchmark,
        )
        evidence = tuple(
            build_fold_evidence(
                fold=fold,
                result=simulator.run(
                    list(reference.universe),
                    start_date=fold.start_date,
                    end_date=fold.end_date,
                    history_start_date=reference.fold_manifest.warmup_start_date,
                    benchmark_symbol=reference.fold_manifest.benchmark,
                ),
            )
            for fold in reference.fold_manifest.discovery_folds
        )
    return verify_parity_evidence(
        reference=reference,
        output=Path(output),
        final_source_head=final_head,
        final_source_fingerprint_sha256=final_fingerprint,
        policy_interface_version=POLICY_INTERFACE_VERSION,
        final_discovery_evidence=evidence,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture")
    capture.add_argument("--readiness", required=True, type=Path)
    capture.add_argument("--pit-bundle", required=True, type=Path)
    capture.add_argument("--output", required=True, type=Path)
    verify = commands.add_parser("verify")
    verify.add_argument("--reference", required=True, type=Path)
    verify.add_argument("--pit-bundle", required=True, type=Path)
    verify.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "capture":
        reference = capture_parity_reference(
            readiness_path=args.readiness,
            pit_bundle_path=args.pit_bundle,
            output=args.output,
        )
        folds = ",".join(
            f"{fold.fold_id}:{fold.start_date}..{fold.end_date}" for fold in reference.fold_manifest.discovery_folds
        )
        hidden = reference.fold_manifest.hidden_fold
        print(
            "PIT_POLICY_PARITY_REFERENCE "
            f"sha256={reference.artifact_sha256} discovery={folds} "
            f"hidden_unevaluated={hidden.fold_id}:{hidden.start_date}..{hidden.end_date}"
        )
        return 0
    attestation = verify_parity_reference(
        reference_path=args.reference,
        pit_bundle_path=args.pit_bundle,
        output=args.output,
    )
    print(f"PIT_POLICY_PARITY_VERIFIED sha256={attestation.artifact_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
