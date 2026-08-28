from __future__ import annotations

from dataclasses import asdict, replace
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Mapping

import pytest

from core import pit_optimization_contract as contract
from core.pit_optimizer_evaluation import (
    AggregateMetric,
    FoldAggregateSummary,
    FoldManifest,
    FoldSpec,
)
from core.pit_policy_parity import (
    ParityAttestation,
    ParityEquityPoint,
    ParityFoldEvidence,
)


def _canonical_bytes(value: object) -> bytes:
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


def _sessions(start: date) -> tuple[str, ...]:
    values: list[str] = []
    cursor = start
    while len(values) < 60:
        if cursor.weekday() < 5:
            values.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return tuple(values)


def _fold(fold_id: str, purpose: str, start: date) -> FoldSpec:
    sessions = _sessions(start)
    return FoldSpec(
        fold_id=fold_id,
        purpose=purpose,
        start_date=sessions[0],
        end_date=sessions[-1],
        sessions=sessions,
    )


def _aggregate(fold_id: str, total_return: float) -> FoldAggregateSummary:
    return FoldAggregateSummary(
        fold_id=fold_id,
        total_return_pct=total_return,
        excess_total_return_pp=None,
        max_drawdown_pct=-2.0,
        sharpe_ratio=1.0,
        closed_trades=4,
        turnover_pct=12.0,
        average_exposure_pct=35.0,
        entry_funnel=(AggregateMetric("entries", 4),),
        exit_attribution=(AggregateMetric("profit_target", 2),),
    )


def _evidence(
    fold: FoldSpec,
    *,
    effective_policy_sha256: str,
    total_return: float,
) -> ParityFoldEvidence:
    aggregate = _aggregate(fold.fold_id, total_return)
    primitive = {
        "fold_id": fold.fold_id,
        "transactions": [],
        "entry_outcomes": [],
        "equity": [
            asdict(ParityEquityPoint(session=session, equity=100.0 + index))
            for index, session in enumerate(fold.sessions)
        ],
        "funnel": [asdict(item) for item in aggregate.entry_funnel],
        "aggregate": asdict(aggregate),
        "effective_policy_sha256": effective_policy_sha256,
    }
    return ParityFoldEvidence(
        fold_id=fold.fold_id,
        transactions=(),
        entry_outcomes=(),
        equity=tuple(
            ParityEquityPoint(session=session, equity=100.0 + index)
            for index, session in enumerate(fold.sessions)
        ),
        funnel=aggregate.entry_funnel,
        aggregate=aggregate,
        effective_policy_sha256=effective_policy_sha256,
        evidence_sha256=hashlib.sha256(_canonical_bytes(primitive)).hexdigest(),
    )


def _call_budgets() -> tuple[contract.PitOptimizerCallBudget, ...]:
    caps = {
        "investigator": (8_000, 80_000, 88_000, 4_000, 8 * 1024, 0.05),
        "author": (12_000, 76_000, 88_000, 8_000, 16 * 1024, 0.10),
        "critic": (8_000, 24_000, 32_000, 4_000, 8 * 1024, 0.05),
    }
    return tuple(
        contract.PitOptimizerCallBudget(
            call_index=(iteration - 1) * 3 + ordinal,
            iteration=iteration,
            role=role,
            model=contract.PIT_OPTIMIZER_R1_MODEL,
            max_static_input_bytes=caps[role][0],
            max_dynamic_input_bytes=caps[role][1],
            max_input_tokens=caps[role][2],
            max_output_tokens=caps[role][3],
            max_response_bytes=caps[role][4],
            max_usd=caps[role][5],
        )
        for iteration in (1, 2)
        for ordinal, role in enumerate(contract.OPTIMIZER_V2_ROLES, start=1)
    )


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def _prepare_fixture(tmp_path: Path) -> dict[str, object]:
    source_root = tmp_path / "source"
    source_texts = {
        "core/strategy_policy/entry.py": "def evaluate_entry(snapshot):\n    return None\n",
        "core/strategy_policy/risk.py": (
            "def recommend_capacity(snapshot):\n    return 1\n"
            "def recommend_allocation(snapshot):\n    return 0.1\n"
            "def select_eviction(snapshot):\n    return None\n"
        ),
        "core/strategy_policy/exit.py": "def evaluate_exit(snapshot):\n    return None\n",
    }
    for relative, text in source_texts.items():
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
    for args in (
        ("init",),
        ("config", "user.name", "Task 7 Test"),
        ("config", "user.email", "task7@example.invalid"),
        ("add", "."),
        ("commit", "-m", "synthetic source"),
    ):
        _git(source_root, *args)
    source_head = _git(source_root, "rev-parse", "HEAD").decode().strip()
    source_fingerprint = hashlib.sha256(
        _git(source_root, "ls-tree", "-r", "--full-tree", "HEAD")
    ).hexdigest()

    pit_bundle = tmp_path / "pit.sqlite3"
    pit_bundle.write_bytes(b"provider-free-pit-fixture")
    pit_sha256 = hashlib.sha256(pit_bundle.read_bytes()).hexdigest()
    baseline_run = tmp_path / "baseline"
    baseline_run.mkdir()
    baseline_manifest = baseline_run / "run_manifest.json"
    baseline_manifest.write_bytes(_canonical_bytes({"schema_version": 1}))
    baseline_sha256 = hashlib.sha256(baseline_manifest.read_bytes()).hexdigest()
    effective_policy_sha256 = "3" * 64

    fold_manifest = FoldManifest(
        data_identity_sha256=pit_sha256,
        universe_sha256="4" * 64,
        benchmark="SPY",
        warmup_start_date="2020-01-02",
        discovery_folds=(
            _fold("discovery_1", "discovery", date(2021, 1, 4)),
            _fold("discovery_2", "discovery", date(2021, 4, 1)),
        ),
        hidden_fold=_fold("hidden_sentinel", "hidden", date(2021, 7, 1)),
    )
    evidence = (
        _evidence(
            fold_manifest.discovery_folds[0],
            effective_policy_sha256=effective_policy_sha256,
            total_return=1.0,
        ),
        _evidence(
            fold_manifest.discovery_folds[1],
            effective_policy_sha256=effective_policy_sha256,
            total_return=2.0,
        ),
    )
    parity_path = tmp_path / "final-parity.json"
    provisional = ParityAttestation(
        schema_version=1,
        reference_artifact_sha256="5" * 64,
        reference_source_head="1" * 40,
        final_source_head=source_head,
        final_source_fingerprint_sha256=source_fingerprint,
        pit_bundle_sha256=pit_sha256,
        baseline_manifest_sha256=baseline_sha256,
        effective_policy_sha256=effective_policy_sha256,
        discovery_fold_manifest_sha256=fold_manifest.sha256,
        policy_interface_version=1,
        reference_output_sha256s=tuple(
            (item.fold_id, item.evidence_sha256) for item in evidence
        ),
        final_output_sha256s=tuple(
            (item.fold_id, item.evidence_sha256) for item in evidence
        ),
        final_discovery_evidence=evidence,
        transactions_equal=True,
        entry_outcomes_equal=True,
        equity_equal=True,
        funnels_equal=True,
        effective_policy_equal=True,
        artifact_path=parity_path.resolve(),
        artifact_sha256="0" * 64,
    )
    parity_primitive = asdict(provisional)
    parity_primitive.pop("artifact_path")
    parity_primitive.pop("artifact_sha256")
    parity_path.write_bytes(_canonical_bytes(parity_primitive))
    parity = replace(
        provisional,
        artifact_sha256=hashlib.sha256(parity_path.read_bytes()).hexdigest(),
    )

    source_hashes = tuple(
        (relative, hashlib.sha256(text.encode("utf-8")).hexdigest())
        for relative, text in source_texts.items()
    )
    bounds = contract.PatchBounds(3, 12, 80, 8 * 1024)
    scope = contract.PolicySourceScope(
        schema_version=2,
        policy_interface_version=1,
        initial_policy_source_sha256s=source_hashes,
        editable_paths=tuple(source_texts),
        max_policy_source_bundle_bytes=contract.MAX_POLICY_SOURCE_BUNDLE_BYTES,
        max_iteration_feedback_bytes=contract.MAX_ITERATION_FEEDBACK_BYTES,
        max_iteration_history_bytes=contract.MAX_ITERATION_HISTORY_BYTES,
        hard_patch_bounds=contract.PatchBounds(3, 12, 200, contract.MAX_AUTHOR_DIFF_BYTES),
        candidate_bounds=bounds,
        max_iterations=2,
        allowed_descendant_rule="authenticated_initial_sources_plus_validated_cumulative_diff",
    )
    authorization = contract.AuthorizationRequirement(
        window_id="window_task7",
        max_calls=6,
        max_tokens=448_000,
        max_usd=0.40,
        policy_source_scope_sha256=scope.sha256,
        provider_retries=0,
        apply=False,
    )
    constraints = ("causal_only", "no_external_io")
    constraints_sha256 = hashlib.sha256(_canonical_bytes(constraints)).hexdigest()
    manifest = contract.PitOptimizerRunManifest(
        schema_version=2,
        run_id="run_task7",
        run_kind="subset_canary",
        model=contract.PIT_OPTIMIZER_R1_MODEL,
        source_head=source_head,
        source_fingerprint_sha256=source_fingerprint,
        legacy_readiness_sha256="6" * 64,
        pit_bundle_sha256=pit_sha256,
        baseline_manifest_sha256=baseline_sha256,
        effective_policy_sha256=effective_policy_sha256,
        policy_interface_version=1,
        policy_source_sha256s=source_hashes,
        editable_paths=tuple(source_texts),
        policy_source_scope=scope,
        immutable_constraints_sha256=constraints_sha256,
        fold_manifest=fold_manifest,
        parity_attestation_sha256=parity.artifact_sha256,
        sandbox_image="example.invalid/task7@sha256:" + "7" * 64,
        validation_ledger_name="pit_optimizer_validation_ledger.jsonl",
        immutable_constraint_ids=constraints,
        candidate_bounds=bounds,
        call_budgets=_call_budgets(),
        max_iterations=2,
        non_improving_limit=3,
        authorization_requirement=authorization,
    )
    manifest_path = tmp_path / "optimizer-manifest.json"
    contract.write_optimizer_manifest(manifest, manifest_path)
    artifact_root = tmp_path / "artifacts"
    permanent_runtime_root = tmp_path / "runtime"
    artifact_root.mkdir()
    permanent_runtime_root.mkdir()
    config = contract.PitOptimizerGateConfig(
        phase="prepare",
        baseline_run=baseline_run,
        baseline_manifest_sha256=baseline_sha256,
        pit_bundle=pit_bundle,
        pit_bundle_sha256=pit_sha256,
        effective_policy_sha256=effective_policy_sha256,
        optimizer_manifest=manifest_path,
        optimizer_manifest_sha256=manifest.sha256,
        verified_parity_artifact=parity_path,
        verified_parity_sha256=parity.artifact_sha256,
        readiness_artifact=None,
        readiness_sha256=None,
        authorization_window_id=None,
        authorization_requirement_sha256=authorization.sha256,
        source_transmission_authorized=False,
        max_usd=0.40,
        max_api_calls=6,
        max_tokens=448_000,
        max_iterations=2,
        apply=False,
    )
    return {
        "config": config,
        "manifest": manifest,
        "parity": parity,
        "source_root": source_root,
        "artifact_root": artifact_root,
        "permanent_runtime_root": permanent_runtime_root,
        "source_head": source_head,
        "source_fingerprint_sha256": source_fingerprint,
    }


def _prepare(inputs: dict[str, object]):
    from core.pit_optimizer_controller import prepare_pit_optimizer_v2

    return prepare_pit_optimizer_v2(
        inputs["config"],
        source_root=inputs["source_root"],
        artifact_root=inputs["artifact_root"],
        permanent_runtime_root=inputs["permanent_runtime_root"],
        source_head=inputs["source_head"],
        source_fingerprint_sha256=inputs["source_fingerprint_sha256"],
    )


def test_prepare_v2_writes_one_self_digested_provider_free_readiness(
    tmp_path: Path,
) -> None:
    """Break caught: prepare could project unauthenticated or hidden evidence."""
    inputs = _prepare_fixture(tmp_path)

    readiness = _prepare(inputs)

    raw = readiness.artifact_path.read_bytes()
    primitive = json.loads(raw)
    assert readiness.readiness_sha256 == hashlib.sha256(raw).hexdigest()
    assert "readiness_sha256" not in primitive
    assert "artifact_path" not in primitive
    assert readiness.baseline_discovery.folds == tuple(
        item.aggregate for item in inputs["parity"].final_discovery_evidence
    )
    assert readiness.provider_seed.baseline_discovery == readiness.baseline_discovery
    projected = readiness.provider_seed.canonical_json_bytes().decode("utf-8")
    hidden = inputs["manifest"].fold_manifest.hidden_fold
    assert hidden.fold_id not in projected
    assert hidden.start_date not in projected
    assert hidden.end_date not in projected
    ledger = Path(inputs["permanent_runtime_root"]) / "pit_optimizer_validation_ledger.jsonl"
    records = [json.loads(line) for line in ledger.read_bytes().splitlines()]
    assert [item["metadata"]["exposure_kind"] for item in records] == [
        "provider_context",
        "provider_context",
    ]
    assert not any(Path(inputs["artifact_root"]).glob("run.json"))


def test_prepare_parity_rejects_nested_evidence_digest_mismatch(
    tmp_path: Path,
) -> None:
    """Break caught: an outer rehash could bless tampered per-fold aggregates."""
    inputs = _prepare_fixture(tmp_path)
    config = inputs["config"]
    manifest = inputs["manifest"]
    assert isinstance(config, contract.PitOptimizerGateConfig)
    assert isinstance(manifest, contract.PitOptimizerRunManifest)
    parity_value = json.loads(Path(config.verified_parity_artifact).read_bytes())
    parity_value["final_discovery_evidence"][0]["aggregate"]["total_return_pct"] = 99.0
    tampered_parity = tmp_path / "tampered-parity.json"
    tampered_parity.write_bytes(_canonical_bytes(parity_value))
    tampered_parity_sha256 = hashlib.sha256(tampered_parity.read_bytes()).hexdigest()
    rebound_manifest = replace(
        manifest,
        parity_attestation_sha256=tampered_parity_sha256,
    )
    rebound_manifest_path = tmp_path / "tampered-manifest.json"
    contract.write_optimizer_manifest(rebound_manifest, rebound_manifest_path)
    inputs["config"] = replace(
        config,
        optimizer_manifest=rebound_manifest_path,
        optimizer_manifest_sha256=rebound_manifest.sha256,
        verified_parity_artifact=tampered_parity,
        verified_parity_sha256=tampered_parity_sha256,
    )

    with pytest.raises(ValueError, match="evidence digest"):
        _prepare(inputs)


def test_prepare_v2_rejects_tracked_source_drift(tmp_path: Path) -> None:
    """Break caught: prepare could trust caller-supplied Git identity after drift."""
    inputs = _prepare_fixture(tmp_path)
    changed = Path(inputs["source_root"]) / "core/strategy_policy/entry.py"
    changed.write_text("def evaluate_entry(snapshot):\n    return True\n", encoding="utf-8")

    with pytest.raises(ValueError, match="clean committed source"):
        _prepare(inputs)


def test_prepare_discovery_exposure_precedes_provider_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: discovery aggregates could enter context before permanent marking."""
    import core.pit_optimizer_controller as controller

    inputs = _prepare_fixture(tmp_path)
    events: list[str] = []

    class Ledger:
        def __init__(self, path: Path) -> None:
            assert path.name == "pit_optimizer_validation_ledger.jsonl"

        def mark_discovery(self, identity: object, metadata: object) -> object:
            events.append("mark")
            return object()

        def seal_discovery_folds(self, manifest: object, reservations: object) -> object:
            events.append("seal")
            return object()

    original = controller._baseline_from_parity

    def project(parity: ParityAttestation):
        events.append("project")
        return original(parity)

    monkeypatch.setattr(controller, "ValidationLedger", Ledger)
    monkeypatch.setattr(controller, "_baseline_from_parity", project)

    _prepare(inputs)

    assert events == ["mark", "mark", "seal", "project"]


def test_run_lease_initialization_freezes_pricing_before_opening_lease() -> None:
    """Break caught: provider authority could be acquired against mutable pricing."""
    from unittest.mock import Mock

    from core.pit_optimizer_authorization import AuthorizationRunLease, FrozenModelPricing
    from core.pit_optimizer_controller import (
        PitOptimizerReadiness,
        PitOptimizerServices,
        _RunState,
        _initialize_provider,
    )

    events: list[str] = []
    pricing = FrozenModelPricing.from_rates(
        model="deepseek/deepseek-r1",
        prompt_per_million=Decimal("1"),
        completion_per_million=Decimal("2"),
    )
    lease = Mock(spec=AuthorizationRunLease)
    readiness = Mock(spec=PitOptimizerReadiness)
    readiness.manifest.model = "deepseek/deepseek-r1"
    state = Mock(spec=_RunState)
    services = Mock(spec=PitOptimizerServices)
    services.freeze_pricing.side_effect = lambda model: events.append("pricing") or pricing
    services.open_run_lease.side_effect = (
        lambda ready, frozen: events.append("lease") or lease
    )

    _initialize_provider(readiness, state, services)

    assert events == ["pricing", "lease"]
    assert state.frozen_pricing is pricing
    assert state.authorization_lease is lease


def _run_state(readiness: object):
    from core.pit_optimizer_controller import _RunState

    return _RunState(
        run_id=readiness.manifest.run_id,
        next_iteration=1,
        incumbent_workspace=None,
        incumbent_identity=None,
        incumbent_cumulative_diff="",
        incumbent_discovery=readiness.baseline_discovery,
        prior_iterations=(),
        valid_evaluations=0,
        incumbent_updates=0,
        non_improving_streak=0,
        provider_enabled=True,
        frozen_pricing=None,
        authorization_lease=None,
    )


def _pricing_and_lease(manifest: contract.PitOptimizerRunManifest):
    from core.pit_optimizer_authorization import AuthorizationRunLease, FrozenModelPricing

    pricing = FrozenModelPricing.from_rates(
        model=manifest.model,
        prompt_per_million=Decimal("1"),
        completion_per_million=Decimal("2"),
    )
    lease = AuthorizationRunLease(
        lease_id="lease_task7",
        one_shot_key_sha256="8" * 64,
        window_id=manifest.authorization_requirement.window_id,
        run_manifest_sha256=manifest.sha256,
        frozen_pricing_sha256=pricing.pricing_sha256,
        max_calls=manifest.authorization_requirement.max_calls,
        max_tokens=manifest.authorization_requirement.max_tokens,
        max_usd=manifest.authorization_requirement.max_usd,
    )
    return pricing, lease


def test_artifact_initialization_seals_run_baseline_and_accounting_after_lease(
    tmp_path: Path,
) -> None:
    """Break caught: a run could call or advance without its initial durable identities."""
    from unittest.mock import Mock

    from core.pit_optimizer_controller import (
        PitOptimizerServices,
        _initialize_run_artifacts,
    )

    readiness = _prepare(_prepare_fixture(tmp_path))
    state = _run_state(readiness)
    pricing, lease = _pricing_and_lease(readiness.manifest)
    services = Mock(spec=PitOptimizerServices)
    events: list[str] = []
    payloads: dict[str, Mapping[str, object]] = {}
    services.freeze_pricing.side_effect = lambda model: events.append("pricing") or pricing
    services.open_run_lease.side_effect = (
        lambda ready, frozen: events.append("lease") or lease
    )

    def write(name: str, value: Mapping[str, object]) -> tuple[Path, str]:
        events.append(name)
        payloads[name] = value
        path = (tmp_path / "run" / name).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path, hashlib.sha256(_canonical_bytes(value)).hexdigest()

    services.write_json_artifact.side_effect = write

    _initialize_run_artifacts(readiness, state, services)

    assert events == ["pricing", "lease", "run.json", "baseline.json", "accounting.json"]
    assert tuple(payloads) == ("run.json", "baseline.json", "accounting.json")
    assert payloads["run.json"]["run_id"] == readiness.manifest.run_id
    assert payloads["run.json"]["authorization"]["lease_id"] == lease.lease_id
    assert payloads["run.json"]["frozen_pricing_sha256"] == pricing.pricing_sha256
    assert payloads["baseline.json"]["fold_ids"] == ["discovery_1", "discovery_2"]
    serialized = json.dumps(payloads, sort_keys=True)
    assert "hidden_results" not in serialized
    assert "raw_response" not in serialized
    assert "system_prompt" not in serialized
    assert state.next_iteration == 1


def test_artifact_failure_closes_retained_lease_without_state_advance(
    tmp_path: Path,
) -> None:
    """Break caught: an fsync/write failure could leak authority or advance iteration state."""
    from unittest.mock import Mock

    from core.pit_optimizer_controller import PitOptimizerServices, _initialize_run_artifacts

    readiness = _prepare(_prepare_fixture(tmp_path))
    state = _run_state(readiness)
    pricing, lease = _pricing_and_lease(readiness.manifest)
    services = Mock(spec=PitOptimizerServices)
    services.freeze_pricing.return_value = pricing
    services.open_run_lease.return_value = lease
    writes: list[str] = []

    def fail_second(name: str, value: Mapping[str, object]) -> tuple[Path, str]:
        writes.append(name)
        if name == "baseline.json":
            raise OSError("injected artifact fsync failure")
        return (tmp_path / name).resolve(), "9" * 64

    services.write_json_artifact.side_effect = fail_second

    with pytest.raises(OSError, match="injected artifact fsync failure"):
        _initialize_run_artifacts(readiness, state, services)

    assert writes == ["run.json", "baseline.json"]
    services.close_run_lease.assert_called_once_with(lease, "audit_failure")
    assert state.authorization_lease is None
    assert state.next_iteration == 1


def test_artifact_initialization_store_is_create_only_and_accounting_replaceable(
    tmp_path: Path,
) -> None:
    """Break caught: immutable evidence could be overwritten through the artifact store."""
    from core.pit_optimizer_artifacts import IncrementalArtifactStore

    root = (tmp_path / "run-artifacts").resolve()
    root.mkdir()
    store = IncrementalArtifactStore(root)
    first_path, first_digest = store.write_json_artifact(
        "run.json",
        {"schema_version": 2, "run_id": "run_task7"},
    )
    assert first_path == root / "run.json"
    assert first_digest == hashlib.sha256(first_path.read_bytes()).hexdigest()
    with pytest.raises(FileExistsError):
        store.write_json_artifact(
            "run.json",
            {"schema_version": 2, "run_id": "run_other"},
        )
    store.write_json_artifact(
        "accounting.json",
        {"schema_version": 2, "api_calls": 0},
    )
    replaced_path, replaced_digest = store.write_json_artifact(
        "accounting.json",
        {"schema_version": 2, "api_calls": 1},
    )
    assert json.loads(replaced_path.read_bytes())["api_calls"] == 1
    assert replaced_digest == hashlib.sha256(replaced_path.read_bytes()).hexdigest()


def test_artifact_failure_removes_an_uncommitted_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a failed file fsync could leave a seemingly valid partial artifact."""
    import core.pit_optimizer_artifacts as artifacts

    target = (tmp_path / "run.json").resolve()
    monkeypatch.setattr(
        artifacts.os,
        "fsync",
        lambda descriptor: (_ for _ in ()).throw(OSError("injected fsync")),
    )

    with pytest.raises(OSError, match="injected fsync"):
        artifacts.write_create_only_json(
            target,
            {"schema_version": 2, "run_id": "run_task7"},
        )
    assert not target.exists()


def test_iteration_directory_is_durable_before_investigator_authority_and_retained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: an investigator failure could leave no partial iteration layout."""
    from unittest.mock import Mock

    import core.pit_optimizer_controller as controller
    from core.pit_optimizer_artifacts import IncrementalArtifactStore
    from core.pit_optimizer_controller import (
        PitOptimizerReadiness,
        PitOptimizerServices,
        ProviderProtocolFailure,
        run_pit_optimizer_v2,
    )

    root = (tmp_path / "run").resolve()
    root.mkdir()
    store = IncrementalArtifactStore(root)
    readiness = Mock(spec=PitOptimizerReadiness)
    readiness.manifest.run_id = "run_task7"
    readiness.baseline_discovery = Mock()
    services = Mock(spec=PitOptimizerServices)
    events: list[str] = []
    services.prepare_iteration_artifacts.side_effect = lambda iteration: (
        events.append(f"prepare:{iteration}") or store.prepare_iteration(iteration)
    )
    monkeypatch.setattr(controller, "_initialize_run_artifacts", lambda *args: None)
    monkeypatch.setattr(controller, "_pre_iteration_stop", lambda *args: None)
    monkeypatch.setattr(controller, "_prepare_iteration_source", lambda *args: None)

    def fail_investigator(*args: object) -> object:
        events.append("investigator")
        assert (root / "iterations" / "001").is_dir()
        raise ProviderProtocolFailure("injected investigator failure")

    monkeypatch.setattr(controller, "_run_iteration", fail_investigator)
    sentinel = Mock()
    monkeypatch.setattr(
        controller,
        "_finalize_result",
        lambda *args: sentinel,
    )

    assert run_pit_optimizer_v2(readiness=readiness, services=services) is sentinel
    assert events == ["prepare:1", "investigator"]
    assert (root / "iterations" / "001").is_dir()


def test_artifact_store_rejects_nested_link_before_any_outside_mutation(
    tmp_path: Path,
) -> None:
    """Break caught: an intermediate iterations link could redirect artifact creation."""
    from core.pit_optimizer_artifacts import IncrementalArtifactStore

    root = (tmp_path / "run").resolve()
    outside = (tmp_path / "outside").resolve()
    root.mkdir()
    outside.mkdir()
    try:
        (root / "iterations").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this platform")

    store = IncrementalArtifactStore(root)
    with pytest.raises(ValueError, match="link|reparse|parent"):
        store.prepare_iteration(1)

    assert list(outside.iterdir()) == []


def test_artifact_initialization_candidate_capability_is_keyed_only_by_workspace_id(
    tmp_path: Path,
) -> None:
    """Break caught: a removed/forged root reference could recreate candidate authority."""
    from types import SimpleNamespace
    from unittest.mock import Mock

    from core.pit_optimizer_controller import (
        CandidateValidationOutcome,
        CandidateWorkspace,
        _CandidateCapabilityRegistry,
    )
    from core.pit_optimizer_evaluation import PitOptimizerCleanup

    root = (tmp_path / "candidate").resolve()
    root.mkdir()
    capability = SimpleNamespace(root=root)
    author = Mock(spec=contract.AuthorArtifact)
    outcome = CandidateValidationOutcome(False, "syntax_failed", "diff", "diff", None, (), ())
    disposed: list[object] = []
    registry = _CandidateCapabilityRegistry(
        create_capability=lambda cumulative: capability,
        validate_capability=lambda live, artifact, cumulative: outcome,
        dispose_capability=lambda live: disposed.append(live)
        or PitOptimizerCleanup(True, True, False),
    )

    workspace = registry.create_candidate(None)
    assert registry.validate_and_apply(workspace, author, None) is outcome
    forged = CandidateWorkspace("workspace_forged", workspace.root)
    with pytest.raises(RuntimeError, match="unknown candidate workspace"):
        registry.validate_and_apply(forged, author, None)
    assert registry.dispose_candidate(workspace) == PitOptimizerCleanup(True, True, False)
    assert disposed == [capability]
    with pytest.raises(RuntimeError, match="unknown candidate workspace"):
        registry.validate_and_apply(workspace, author, None)
    with pytest.raises(RuntimeError, match="unknown candidate workspace"):
        registry.dispose_candidate(workspace)


def test_single_iteration_orders_critic_before_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: an improving candidate could be accepted before durable criticism."""
    from unittest.mock import Mock

    import core.pit_optimizer_controller as controller
    from core.pit_optimizer_authorization import PitOptimizerRoleCall
    from core.pit_optimizer_controller import (
        CandidateValidationOutcome,
        _IterationOutcome,
        _run_iteration,
    )
    from core.pit_optimizer_evaluation import DiscoveryEvaluation

    events: list[str] = []
    investigator_call = Mock(spec=PitOptimizerRoleCall)
    author_call = Mock(spec=PitOptimizerRoleCall)
    validation = Mock(spec=CandidateValidationOutcome)
    discovery = Mock(spec=DiscoveryEvaluation)
    critic_call = Mock(spec=PitOptimizerRoleCall)
    outcome = Mock(spec=_IterationOutcome)
    monkeypatch.setattr(
        controller,
        "_run_investigator",
        lambda *args: events.append("investigator") or investigator_call,
    )
    monkeypatch.setattr(
        controller,
        "_run_author",
        lambda *args: events.append("author") or author_call,
    )
    monkeypatch.setattr(
        controller,
        "_validate_iteration_candidate",
        lambda *args: events.append("validate") or validation,
    )
    monkeypatch.setattr(
        controller,
        "_evaluate_iteration_candidate",
        lambda *args: events.append("discovery") or discovery,
    )
    monkeypatch.setattr(
        controller,
        "_run_critic",
        lambda *args: events.append("critic") or critic_call,
    )
    monkeypatch.setattr(
        controller,
        "_persist_iteration_decision",
        lambda *args: events.append("decision") or outcome,
    )

    assert _run_iteration(Mock(), Mock(), Mock()) is outcome
    assert events == ["investigator", "author", "validate", "discovery", "critic", "decision"]


@pytest.mark.parametrize(
    "failure_code",
    sorted(contract.CANDIDATE_VALIDATION_FAILURE_CODES - {"no_discovery_trades"}),
)
def test_invalid_author_still_receives_critic_before_a_retain_decision(
    monkeypatch: pytest.MonkeyPatch,
    failure_code: str,
) -> None:
    """Break caught: static/syntax rejection could silently skip critic feedback."""
    from unittest.mock import Mock

    import core.pit_optimizer_controller as controller
    from core.pit_optimizer_controller import CandidateValidationOutcome, _run_iteration

    events: list[str] = []
    validation = CandidateValidationOutcome(
        valid=False,
        failure_code=failure_code,
        incremental_diff="candidate diff",
        cumulative_diff="",
        identity=None,
        changed_paths=(),
        changed_symbols=(),
    )
    monkeypatch.setattr(
        controller,
        "_run_investigator",
        lambda *args: events.append("investigator") or Mock(),
    )
    monkeypatch.setattr(
        controller,
        "_run_author",
        lambda *args: events.append("author") or Mock(),
    )
    monkeypatch.setattr(
        controller,
        "_validate_iteration_candidate",
        lambda *args: events.append("validate") or validation,
    )

    def no_discovery(*args: object) -> None:
        events.append("no_discovery")
        return None

    monkeypatch.setattr(controller, "_evaluate_iteration_candidate", no_discovery)
    monkeypatch.setattr(
        controller,
        "_run_critic",
        lambda *args: events.append("critic") or Mock(),
    )
    monkeypatch.setattr(
        controller,
        "_persist_iteration_decision",
        lambda *args: events.append("retain") or Mock(),
    )

    _run_iteration(Mock(), Mock(), Mock())

    assert events == [
        "investigator",
        "author",
        "validate",
        "no_discovery",
        "critic",
        "retain",
    ]


def test_malformed_author_protocol_failure_never_becomes_critic_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: malformed provider output could be exposed as candidate feedback."""
    from unittest.mock import Mock

    import core.pit_optimizer_controller as controller
    from core.pit_optimizer_controller import ProviderProtocolFailure, _run_iteration

    events: list[str] = []
    monkeypatch.setattr(
        controller,
        "_run_investigator",
        lambda *args: events.append("investigator") or Mock(),
    )

    def malformed(*args: object) -> object:
        events.append("author")
        raise ProviderProtocolFailure("malformed author response")

    monkeypatch.setattr(controller, "_run_author", malformed)
    monkeypatch.setattr(
        controller,
        "_run_critic",
        lambda *args: events.append("critic") or Mock(),
        raising=False,
    )

    with pytest.raises(ProviderProtocolFailure, match="malformed author"):
        _run_iteration(Mock(), Mock(), Mock())
    assert events == ["investigator", "author"]


def test_focused_candidate_checks_persist_only_controller_derived_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: advisory author scope could replace controller-derived check scope."""
    from unittest.mock import Mock

    import core.pit_optimizer_controller as controller
    from core.pit_optimizer_authorization import PitOptimizerRoleCall
    from core.pit_optimizer_controller import (
        CandidateValidationOutcome,
        CandidateWorkspace,
        PitOptimizerServices,
        _validate_iteration_candidate,
    )

    readiness = Mock()
    readiness.manifest.run_id = "run_task7"
    state = Mock()
    state.next_iteration = 1
    state.incumbent_cumulative_diff = "prior diff"
    state.iteration_workspace = CandidateWorkspace("workspace_task7", tmp_path.resolve())
    author = Mock(spec=PitOptimizerRoleCall)
    author.payload = Mock(spec=contract.AuthorArtifact)
    outcome = CandidateValidationOutcome(
        valid=False,
        failure_code="purity_failed",
        incremental_diff="candidate diff",
        cumulative_diff="prior diff",
        identity=None,
        changed_paths=("core/strategy_policy/risk.py",),
        changed_symbols=("core.strategy_policy.risk.recommend_allocation",),
    )
    services = Mock(spec=PitOptimizerServices)
    services.validate_and_apply.return_value = outcome
    written: dict[str, Mapping[str, object]] = {}
    services.write_diff_artifact.side_effect = (
        lambda name, value: ((tmp_path / name).resolve(), hashlib.sha256(value.encode()).hexdigest())
    )
    services.write_json_artifact.side_effect = (
        lambda name, value: written.setdefault(name, value)
        and ((tmp_path / name).resolve(), hashlib.sha256(_canonical_bytes(value)).hexdigest())
    )
    monkeypatch.setattr(controller, "_record_artifact", lambda *args: None)

    assert _validate_iteration_candidate(readiness, state, author, services) is outcome
    services.validate_and_apply.assert_called_once_with(
        state.iteration_workspace,
        author.payload,
        "prior diff",
    )
    assert written["iterations/001/validation.json"]["changed_symbols"] == [
        "core.strategy_policy.risk.recommend_allocation"
    ]


def test_no_discovery_trades_becomes_safe_critic_feedback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: zero trades could be misclassified as evidence tampering."""
    from unittest.mock import Mock

    import core.pit_optimizer_controller as controller
    from core.pit_optimizer_controller import (
        CandidateValidationOutcome,
        CandidateWorkspace,
        PitOptimizerServices,
        _evaluate_iteration_candidate,
    )
    from core.pit_optimizer_evaluation import (
        DiscoveryComparison,
        DiscoveryEvaluation,
        DiscoveryScore,
        FoldEvaluationResult,
    )

    readiness = _prepare(_prepare_fixture(tmp_path))
    state = _run_state(readiness)
    state.iteration_workspace = CandidateWorkspace(
        "workspace_zero_trades",
        tmp_path.resolve(),
    )
    identity = Mock()
    identity.identity_sha256 = "a" * 64
    validation = CandidateValidationOutcome(
        True,
        None,
        "candidate diff",
        "cumulative diff",
        identity,
        ("core/strategy_policy/entry.py",),
        ("core.strategy_policy.entry.evaluate_entry",),
    )
    zero = DiscoveryScore(Decimal("0.00"), Decimal("0.00"), Decimal("0.00"))
    folds = tuple(
        FoldEvaluationResult(
            fold_id=baseline.fold_id,
            engine_policy_sha256=readiness.manifest.effective_policy_sha256,
            candidate_identity_sha256="a" * 64,
            aggregate_metrics=replace(
                baseline,
                closed_trades=0,
                excess_total_return_pp=0.0,
            ),
        )
        for baseline in readiness.baseline_discovery.folds
    )
    services = Mock(spec=PitOptimizerServices)
    services.evaluate_discovery.return_value = DiscoveryEvaluation(
        folds,
        DiscoveryComparison(zero, zero, False, False),
    )
    payloads: list[Mapping[str, object]] = []
    services.write_json_artifact.side_effect = lambda name, value: (
        payloads.append(value) or (tmp_path / name).resolve(),
        hashlib.sha256(_canonical_bytes(value)).hexdigest(),
    )
    monkeypatch.setattr(controller, "_record_artifact", lambda *args: None)

    assert _evaluate_iteration_candidate(readiness, state, validation, services) is None
    assert state.evaluation_failure_code == "no_discovery_trades"
    assert payloads == [
        {
            "schema_version": 2,
            "failure_code": "no_discovery_trades",
            "fixed_baseline_comparison": None,
            "incumbent_diagnostics": None,
            "rankable": False,
            "strictly_improves_incumbent": False,
            "folds": [
                {
                    "fold_id": item.fold_id,
                    "aggregate": asdict(item.aggregate_metrics),
                }
                for item in folds
            ],
            "engine_policy_sha256": readiness.manifest.effective_policy_sha256,
            "candidate_identity_sha256": "a" * 64,
        }
    ]


def _role_facts(
    plan: contract.PitOptimizerCallBudget,
    *,
    frozen_pricing_sha256: str = "8" * 64,
):
    from core.pit_optimizer_authorization import PitOptimizerProviderFacts

    return PitOptimizerProviderFacts(
        call_index=plan.call_index,
        iteration=plan.iteration,
        role=plan.role,
        requested_model=plan.model,
        returned_model=plan.model,
        frozen_pricing_sha256=frozen_pricing_sha256,
        outcome="accepted",
        request_started=True,
        response_received=True,
        finish_reason="stop",
        response_schema_valid=True,
        accounting_complete=True,
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        cost_usd=0.001,
        retained_reservation_tokens=0,
        retained_reservation_usd=0.0,
        audit_sha256=hashlib.sha256(f"audit-{plan.call_index}".encode()).hexdigest(),
    )


def _failed_role_facts(
    plan: contract.PitOptimizerCallBudget,
    *,
    outcome: str,
    frozen_pricing_sha256: str,
):
    from core.pit_optimizer_authorization import PitOptimizerProviderFacts

    uncertain = outcome == "uncertain_accounting"
    return PitOptimizerProviderFacts(
        call_index=plan.call_index,
        iteration=plan.iteration,
        role=plan.role,
        requested_model=plan.model,
        returned_model=plan.model,
        frozen_pricing_sha256=frozen_pricing_sha256,
        outcome=outcome,
        request_started=True,
        response_received=True,
        finish_reason="unknown" if uncertain else "stop",
        response_schema_valid=False,
        accounting_complete=not uncertain,
        prompt_tokens=None if uncertain else 17,
        completion_tokens=None if uncertain else 3,
        total_tokens=None if uncertain else 20,
        cost_usd=None if uncertain else 0.0125,
        retained_reservation_tokens=(
            plan.max_input_tokens + plan.max_output_tokens if uncertain else 0
        ),
        retained_reservation_usd=plan.max_usd if uncertain else 0.0,
        audit_sha256=hashlib.sha256(
            f"audit-{plan.call_index}-{outcome}".encode()
        ).hexdigest(),
    )


@pytest.mark.parametrize(
    ("outcome", "expected_exception"),
    [
        ("schema_invalid", "ProviderProtocolFailure"),
        ("uncertain_accounting", "ProviderAccountingFailure"),
    ],
)
def test_failed_provider_attempt_is_durable_and_retained_in_exact_accounting(
    tmp_path: Path,
    outcome: str,
    expected_exception: str,
) -> None:
    """Break caught: finalized failed calls vanished when the gateway raised."""
    from unittest.mock import Mock

    import core.pit_optimizer_controller as controller
    from core.pit_optimizer_controller import (
        CandidateWorkspace,
        PitOptimizerServices,
        _run_investigator,
    )

    inputs = _prepare_fixture(tmp_path)
    readiness = _prepare(inputs)
    state = _run_state(readiness)
    pricing, lease = _pricing_and_lease(readiness.manifest)
    state.frozen_pricing = pricing
    state.authorization_lease = lease
    plan = readiness.manifest.call_budgets[0]
    facts = _failed_role_facts(
        plan,
        outcome=outcome,
        frozen_pricing_sha256=pricing.pricing_sha256,
    )
    services = Mock(spec=PitOptimizerServices)
    services.create_candidate.return_value = CandidateWorkspace(
        "workspace_failed_attempt",
        Path(inputs["source_root"]).resolve(),
    )
    services.call_role.side_effect = RuntimeError("injected gateway failure")
    services.recover_role_attempt.return_value = facts
    accounting: list[Mapping[str, object]] = []

    def write(name: str, value: Mapping[str, object]) -> tuple[Path, str]:
        if name == "accounting.json":
            accounting.append(value)
        path = (tmp_path / "run" / name).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path, hashlib.sha256(_canonical_bytes(value)).hexdigest()

    services.write_json_artifact.side_effect = write

    with pytest.raises(getattr(controller, expected_exception)):
        _run_investigator(readiness, state, services)

    services.recover_role_attempt.assert_called_once_with(plan, lease)
    assert len(accounting) == 1
    record = accounting[0]["call_records"][0]
    assert record["plan"] == plan.to_primitive()
    assert record["payload_sha256"] is None
    assert record["facts"] == asdict(facts)
    assert accounting[0]["actual_totals"] == {
        "calls": 1,
        "prompt_tokens": 0 if outcome == "uncertain_accounting" else 17,
        "completion_tokens": 0 if outcome == "uncertain_accounting" else 3,
        "total_tokens": 0 if outcome == "uncertain_accounting" else 20,
        "usd": 0.0 if outcome == "uncertain_accounting" else 0.0125,
    }
    assert accounting[0]["incomplete_exposure"] == {
        "calls": 1 if outcome == "uncertain_accounting" else 0,
        "retained_tokens": (
            plan.max_input_tokens + plan.max_output_tokens
            if outcome == "uncertain_accounting"
            else 0
        ),
        "retained_usd": plan.max_usd if outcome == "uncertain_accounting" else 0.0,
    }
    from core.pit_optimizer_evaluation import PitOptimizerCleanup

    result = controller._build_final_result(
        readiness=readiness,
        state=state,
        terminal_code=(
            "provider_accounting_failure"
            if outcome == "uncertain_accounting"
            else "provider_protocol_failure"
        ),
        cleanup=PitOptimizerCleanup(True, True, False),
    )
    assert asdict(result.budget) == {
        "api_calls": 1,
        "prompt_tokens": 0 if outcome == "uncertain_accounting" else 17,
        "completion_tokens": 0 if outcome == "uncertain_accounting" else 3,
        "total_tokens": 0 if outcome == "uncertain_accounting" else 20,
        "authoritative_usd": 0.0 if outcome == "uncertain_accounting" else 0.0125,
        "retained_reservation_tokens": (
            plan.max_input_tokens + plan.max_output_tokens
            if outcome == "uncertain_accounting"
            else 0
        ),
        "retained_reservation_usd": (
            plan.max_usd if outcome == "uncertain_accounting" else 0.0
        ),
        "incomplete_accounting_calls": (
            1 if outcome == "uncertain_accounting" else 0
        ),
    }


def _entry_diff(old: str, new: str) -> str:
    return (
        "--- a/core/strategy_policy/entry.py\n"
        "+++ b/core/strategy_policy/entry.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def evaluate_entry(snapshot):\n"
        f"-    return {old}\n"
        f"+    return {new}\n"
    )


def _entry_git_diff(old: str, new: str) -> str:
    return (
        "diff --git a/core/strategy_policy/entry.py b/core/strategy_policy/entry.py\n"
        "index 1111111..2222222 100644\n"
        + _entry_diff(old, new)
    )


def test_two_iteration_role_lineage_is_exact_and_provider_seed_stays_hidden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: iteration two could lose critic history or use stale source context."""
    from unittest.mock import Mock

    import core.pit_optimizer_controller as controller
    from core.pit_optimization_contract import (
        AuthorArtifact,
        CriticArtifact,
        InvestigatorArtifact,
        IterationFeedbackSummary,
    )
    from core.pit_optimizer_authorization import PitOptimizerRoleCall
    from core.pit_optimizer_controller import (
        CandidateValidationOutcome,
        CandidateWorkspace,
        PitOptimizerServices,
        _IterationOutcome,
        _run_iteration,
    )

    inputs = _prepare_fixture(tmp_path)
    readiness = _prepare(inputs)
    state = _run_state(readiness)
    pricing, lease = _pricing_and_lease(readiness.manifest)
    state.frozen_pricing = pricing
    state.authorization_lease = lease
    source_before = _git(Path(inputs["source_root"]), "status", "--porcelain")
    candidate_count = 0
    role_inputs: list[object] = []
    role_order: list[str] = []

    def create_candidate(cumulative_diff: str | None) -> CandidateWorkspace:
        nonlocal candidate_count
        candidate_count += 1
        root = (tmp_path / f"candidate-{candidate_count}").resolve()
        texts = {
            "core/strategy_policy/entry.py": (
                "def evaluate_entry(snapshot):\n    return True\n"
                if cumulative_diff
                else "def evaluate_entry(snapshot):\n    return None\n"
            ),
            "core/strategy_policy/risk.py": (
                "def recommend_capacity(snapshot):\n    return 1\n"
                "def recommend_allocation(snapshot):\n    return 0.1\n"
                "def select_eviction(snapshot):\n    return None\n"
            ),
            "core/strategy_policy/exit.py": "def evaluate_exit(snapshot):\n    return None\n",
        }
        for relative, text in texts.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="")
        return CandidateWorkspace(f"workspace_{candidate_count}", root)

    def call_role(plan: contract.PitOptimizerCallBudget, role_input: object, *args: object):
        role_order.append(plan.role)
        role_inputs.append(role_input)
        hypothesis = f"hypothesis_{plan.iteration}"
        if plan.role == "investigator":
            payload = InvestigatorArtifact(
                hypothesis_id=hypothesis,
                family="entry",
                evidence_ids=(readiness.baseline_discovery.evidence_ids[0],),
                causal_rationale="Test one bounded entry change.",
                target_paths=("core/strategy_policy/entry.py",),
                target_symbols=("core.strategy_policy.entry.evaluate_entry",),
                expected_diagnostic_changes=("entries",),
                known_risks=(),
                author_instructions=("Change only the entry return.",),
            )
        elif plan.role == "author":
            payload = AuthorArtifact(
                hypothesis_id=hypothesis,
                behavioral_summary=f"iteration {plan.iteration} entry change",
                changed_paths=("core/strategy_policy/entry.py",),
                changed_symbols=("core.strategy_policy.entry.evaluate_entry",),
                unified_diff=(
                        _entry_diff("None", "True")
                        if plan.iteration == 1
                        else _entry_diff("True", "False")
                ),
                assumptions=(),
                validation_suggestions=(),
            )
        else:
            payload = CriticArtifact(
                hypothesis_id=hypothesis,
                prediction_vs_observation="Candidate was locally rejected.",
                causal_explanation="Keep the bounded direction for the next iteration.",
                evidence_ids=(readiness.baseline_discovery.evidence_ids[0],),
                disposition="refine",
                next_direction=f"direction {plan.iteration}",
            )
        return PitOptimizerRoleCall(
            plan,
            payload,
            _role_facts(plan, frozen_pricing_sha256=pricing.pricing_sha256),
        )

    services = Mock(spec=PitOptimizerServices)
    services.create_candidate.side_effect = create_candidate
    services.call_role.side_effect = call_role
    services.write_json_artifact.side_effect = lambda name, value: (
        (tmp_path / "run" / name).resolve(),
        hashlib.sha256(_canonical_bytes(value)).hexdigest(),
    )
    services.write_diff_artifact.side_effect = lambda name, value: (
        (tmp_path / "run" / name).resolve(),
        hashlib.sha256(value.encode()).hexdigest(),
    )
    monkeypatch.setattr(controller, "_record_artifact", lambda *args: None)

    def validate(*args: object) -> CandidateValidationOutcome:
        author_call = args[2]
        return CandidateValidationOutcome(
            valid=False,
            failure_code="syntax_failed",
            incremental_diff=author_call.payload.unified_diff,
            cumulative_diff=state.incumbent_cumulative_diff,
            identity=None,
            changed_paths=author_call.payload.changed_paths,
            changed_symbols=author_call.payload.changed_symbols,
        )

    monkeypatch.setattr(controller, "_validate_iteration_candidate", validate)
    monkeypatch.setattr(controller, "_evaluate_iteration_candidate", lambda *args: None)

    def persist(
        ready: object,
        current: object,
        validation: object,
        discovery: object,
        critic: object,
        service_values: object,
    ) -> _IterationOutcome:
        investigator = state.last_investigator.payload
        author = state.last_author.payload
        changed = state.next_iteration == 1
        feedback = IterationFeedbackSummary(
            iteration=state.next_iteration,
            hypothesis_id=investigator.hypothesis_id,
            family=investigator.family,
            author_summary=author.behavioral_summary,
            validation_code="syntax_failed",
            discovery_score=None,
            critic_disposition=critic.payload.disposition,
            critic_next_direction=critic.payload.next_direction,
            incumbent_changed=changed,
        )
        if changed:
            state.incumbent_cumulative_diff = author.unified_diff
        state.prior_iterations = (*state.prior_iterations, feedback)
        state.iteration_workspace = None
        state.iteration_source_bundle = None
        state.next_iteration += 1
        return _IterationOutcome(True, None, feedback, None, None, None, changed)

    monkeypatch.setattr(controller, "_persist_iteration_decision", persist)

    _run_iteration(readiness, state, services)
    _run_iteration(readiness, state, services)

    assert role_order == ["investigator", "author", "critic"] * 2
    assert len(role_inputs) == 6
    second_investigator = role_inputs[3]
    second_author = role_inputs[4]
    assert second_investigator.prior_iterations == (state.prior_iterations[0],)
    assert second_investigator.source_bundle.cumulative_diff == _entry_diff("None", "True")
    assert second_author.source_bundle == second_investigator.source_bundle
    hidden = readiness.manifest.fold_manifest.hidden_fold
    assert all(
        hidden.fold_id.encode() not in item.canonical_json_bytes()
        and hidden.start_date.encode() not in item.canonical_json_bytes()
        and hidden.end_date.encode() not in item.canonical_json_bytes()
        for item in role_inputs
    )
    assert _git(Path(inputs["source_root"]), "status", "--porcelain") == source_before == b""


def test_actual_two_iteration_loop_persists_exact_closed_artifacts_without_leaks(
    tmp_path: Path,
) -> None:
    """Break caught: mocked stage tests could hide defects in the real loop path."""
    import shutil

    import agent_loop
    from core import pit_optimizer_controller as controller
    from core.pit_optimization_contract import (
        AuthorArtifact,
        CriticArtifact,
        InvestigatorArtifact,
    )
    from core.pit_optimizer_artifacts import IncrementalArtifactStore
    from core.pit_optimizer_authorization import PitOptimizerRoleCall
    from core.pit_optimizer_candidate import validate_candidate_diff
    from core.pit_optimizer_controller import (
        CandidateValidationOutcome,
        CandidateWorkspace,
        PitOptimizerServices,
        run_pit_optimizer_v2,
    )
    from core.pit_optimizer_evaluation import (
        DeterminismAttestation,
        DiscoveryComparison,
        DiscoveryEvaluation,
        FoldEvaluationResult,
        PitOptimizerCleanup,
        ValidationReservation,
    )

    inputs = _prepare_fixture(tmp_path)
    readiness = _prepare(inputs)
    git_path = shutil.which("git")
    assert git_path is not None
    git_capability = agent_loop.configure_git_executable(Path(git_path).resolve())
    run_root = (tmp_path / "run").resolve()
    run_root.mkdir()
    store = IncrementalArtifactStore(run_root)
    pricing, lease = _pricing_and_lease(readiness.manifest)
    initial_bundle = controller.build_policy_source_bundle(
        candidate_root=Path(inputs["source_root"]).resolve(),
        cumulative_diff="",
        policy_interface_version=readiness.manifest.policy_interface_version,
    )
    controller._require_complete_iteration_context(readiness, initial_bundle)
    live: dict[str, Path] = {}
    candidate_number = 0
    evaluation_number = 0
    role_inputs: list[object] = []
    disposed: list[str] = []

    def create_candidate(cumulative_diff: str | None) -> CandidateWorkspace:
        nonlocal candidate_number
        candidate_number += 1
        workspace_id = f"workspace_actual_{candidate_number}"
        root = (tmp_path / workspace_id).resolve()
        shutil.copytree(Path(inputs["source_root"]), root)
        if cumulative_diff:
            agent_loop._git(
                root,
                "apply",
                "--whitespace=error-all",
                "-",
                input_bytes=cumulative_diff.encode("utf-8"),
                git=git_capability,
            )
        live[workspace_id] = root
        return CandidateWorkspace(workspace_id, root)

    def call_role(
        plan: contract.PitOptimizerCallBudget,
        role_input: object,
        parser: object,
        authorization_lease: object,
        frozen_pricing: object,
    ) -> PitOptimizerRoleCall:
        assert authorization_lease == lease
        assert frozen_pricing == pricing
        role_inputs.append(role_input)
        hypothesis = f"actual_hypothesis_{plan.iteration}"
        if plan.role == "investigator":
            payload = InvestigatorArtifact(
                hypothesis_id=hypothesis,
                family="entry",
                evidence_ids=(readiness.baseline_discovery.evidence_ids[0],),
                causal_rationale="Test one bounded entry-policy change.",
                target_paths=("core/strategy_policy/entry.py",),
                target_symbols=("core.strategy_policy.entry.evaluate_entry",),
                expected_diagnostic_changes=("entries",),
                known_risks=(),
                author_instructions=("Change only the literal return value.",),
            )
        elif plan.role == "author":
            payload = AuthorArtifact(
                hypothesis_id=hypothesis,
                behavioral_summary=f"bounded entry change {plan.iteration}",
                changed_paths=("core/strategy_policy/entry.py",),
                changed_symbols=("core.strategy_policy.entry.evaluate_entry",),
                unified_diff=(
                    _entry_git_diff("None", "True")
                    if plan.iteration == 1
                    else _entry_git_diff("True", "False")
                ),
                assumptions=(),
                validation_suggestions=(),
            )
        else:
            payload = CriticArtifact(
                hypothesis_id=hypothesis,
                prediction_vs_observation="The closed discovery evidence was evaluated.",
                causal_explanation="Only fixed-baseline strict improvement can replace the incumbent.",
                evidence_ids=(readiness.baseline_discovery.evidence_ids[0],),
                disposition="refine",
                next_direction=f"bounded direction {plan.iteration}",
            )
        return PitOptimizerRoleCall(
            plan,
            payload,
            _role_facts(plan, frozen_pricing_sha256=pricing.pricing_sha256),
        )

    def validate(
        workspace: CandidateWorkspace,
        author: AuthorArtifact,
        cumulative_diff: str | None,
    ) -> CandidateValidationOutcome:
        assert live[workspace.workspace_id] == workspace.root
        identity, authenticated_cumulative = validate_candidate_diff(
            authenticated_base_root=Path(inputs["source_root"]).resolve(),
            candidate_root=workspace.root,
            incremental_diff=author.unified_diff,
            git=git_capability,
            bounds=readiness.manifest.candidate_bounds,
            source_commit=readiness.manifest.source_head,
            policy_interface_version=readiness.manifest.policy_interface_version,
            immutable_constraints_sha256=(
                readiness.manifest.immutable_constraints_sha256
            ),
            discovery_manifest_sha256=readiness.manifest.fold_manifest.sha256,
        )
        return CandidateValidationOutcome(
            True,
            None,
            author.unified_diff,
            authenticated_cumulative,
            identity,
            identity.changed_paths,
            identity.changed_symbols,
        )

    incumbent_candidate_folds: tuple[FoldAggregateSummary, ...] | None = None
    incumbent_evidence_id: str | None = None

    def evaluate(
        workspace: CandidateWorkspace,
        identity: object,
    ) -> DiscoveryEvaluation:
        nonlocal evaluation_number, incumbent_candidate_folds, incumbent_evidence_id
        assert live[workspace.workspace_id] == workspace.root
        evaluation_number += 1
        increment = 1.0 if evaluation_number == 1 else 0.5
        folds = tuple(
            FoldEvaluationResult(
                fold_id=baseline.fold_id,
                engine_policy_sha256=readiness.manifest.effective_policy_sha256,
                candidate_identity_sha256=identity.identity_sha256,
                aggregate_metrics=replace(
                    baseline,
                    total_return_pct=baseline.total_return_pct + increment,
                    excess_total_return_pp=increment,
                ),
            )
            for baseline in readiness.baseline_discovery.folds
        )
        baseline_sha256 = controller._folds_digest(
            readiness.baseline_discovery.folds
        )
        fixed = controller.discovery_score_from_folds(
            tuple(item.aggregate_metrics for item in folds),
            readiness.baseline_discovery.folds,
            original_baseline_sha256=baseline_sha256,
            expected_original_baseline_sha256=baseline_sha256,
        )
        incumbent_folds = (
            readiness.baseline_discovery.folds
            if incumbent_candidate_folds is None
            else incumbent_candidate_folds
        )
        incumbent_sha256 = controller._folds_digest(incumbent_folds)
        diagnostics = controller.discovery_score_from_folds(
            tuple(item.aggregate_metrics for item in folds),
            incumbent_folds,
            original_baseline_sha256=incumbent_sha256,
            expected_original_baseline_sha256=incumbent_sha256,
        )
        improves = evaluation_number == 1
        if improves:
            incumbent_candidate_folds = tuple(
                item.aggregate_metrics for item in folds
            )
            incumbent_evidence_id = hashlib.sha256(
                controller.canonical_json_bytes(
                    {"aggregate": asdict(incumbent_candidate_folds[0])}
                )
            ).hexdigest()
        return DiscoveryEvaluation(
            folds,
            DiscoveryComparison(fixed, diagnostics, True, improves),
        )

    def dispose(workspace: CandidateWorkspace) -> PitOptimizerCleanup:
        assert live.pop(workspace.workspace_id) == workspace.root
        disposed.append(workspace.workspace_id)
        return PitOptimizerCleanup(True, True, False)

    reservation = ValidationReservation("b" * 64, "c" * 64)

    def reserve(identity: object) -> ValidationReservation:
        assert identity.identity_sha256
        return reservation

    def hidden_evaluate(
        workspace: CandidateWorkspace,
        identity: object,
        reserved: ValidationReservation,
    ) -> object:
        assert live[workspace.workspace_id] == workspace.root
        hidden = _hidden_result(
            readiness.manifest.fold_manifest.hidden_fold.fold_id
        )
        return _hidden_attestation(
            readiness,
            identity.identity_sha256,
            reserved,
            hidden,
        )

    services = PitOptimizerServices(
        freeze_pricing=lambda model: pricing,
        open_run_lease=lambda ready, frozen: lease,
        close_run_lease=lambda active, terminal: None,
        call_role=call_role,
        recover_role_attempt=lambda plan, active: (_ for _ in ()).throw(
            AssertionError("accepted calls must not recover")
        ),
        create_candidate=create_candidate,
        validate_and_apply=validate,
        evaluate_discovery=evaluate,
        confirm_discovery=lambda workspace, identity, fold_id: DeterminismAttestation(
            fold_id=fold_id,
            expected_evidence_sha256=incumbent_evidence_id,
            repeated_evidence_sha256=incumbent_evidence_id,
            matched=True,
        ),
        reserve_hidden_validation=reserve,
        evaluate_hidden=hidden_evaluate,
        record_hidden_outcome=lambda reserved, attempted, completed, failure: (
            _hidden_outcome_proof(
                reserved,
                attempted=attempted,
                completed=completed,
                failure_code=failure,
            )
        ),
        dispose_candidate=dispose,
        verify_inputs=lambda ready: None,
        cancellation_requested=lambda: False,
        prepare_iteration_artifacts=store.prepare_iteration,
        write_json_artifact=store.write_json_artifact,
        write_diff_artifact=store.write_diff_artifact,
    )
    result = run_pit_optimizer_v2(readiness=readiness, services=services)

    assert result.terminal_code == "iteration_limit"
    assert result.status == "long_replay_eligible"
    assert result.iterations_started == result.iterations_completed == 2
    assert result.incumbent_updates == 1
    assert result.valid_evaluations == 2
    assert result.non_improving_streak == 1
    assert len(role_inputs) == 6
    assert [item.__class__.__name__ for item in role_inputs] == [
        "InvestigatorInput",
        "AuthorInput",
        "CriticInput",
    ] * 2
    assert role_inputs[3].prior_iterations == (role_inputs[3].prior_iterations[0],)
    assert role_inputs[3].prior_iterations[0].incumbent_changed is True
    assert sorted(disposed) == ["workspace_actual_1", "workspace_actual_2"]
    expected_files = {
        "run.json",
        "baseline.json",
        "accounting.json",
        "incumbent.diff",
        "holdout.json",
        "summary.json",
        *{
            f"iterations/{iteration:03d}/{name}"
            for iteration in (1, 2)
            for name in (
                "investigator.json",
                "author.json",
                "candidate.diff",
                "validation.json",
                "discovery.json",
                "critic.json",
                "decision.json",
            )
        },
    }
    assert {
        path.relative_to(run_root).as_posix()
        for path in run_root.rglob("*")
        if path.is_file()
    } == expected_files
    expected_json_keys = {
        "run.json": {
            "schema_version", "run_id", "manifest_sha256", "readiness_sha256",
            "source", "pit_bundle_sha256", "baseline_manifest_sha256",
            "effective_policy_sha256", "parity_attestation_sha256",
            "fold_manifest_sha256", "fold_identities", "policy_interface_version",
            "candidate_bounds", "authorization", "frozen_pricing_sha256", "status",
        },
        "baseline.json": {
            "schema_version", "fold_ids", "fold_aggregates", "evidence_ids",
            "universe_sha256", "warmup_start_date", "engine_policy_sha256",
            "parity_attestation_sha256",
        },
        "accounting.json": {
            "schema_version", "call_records", "authorized_totals", "reserved_totals",
            "actual_totals", "incomplete_exposure", "audit_chain_head",
        },
        "holdout.json": {
            "schema_version", "consumed_validation_key_sha256",
            "validation_reservation_sha256", "baseline_aggregate",
            "candidate_aggregate", "baseline_identity_sha256",
            "candidate_identity_sha256", "eligibility_checks",
        },
        "summary.json": {
            "schema_version", "phase", "status", "terminal", "run_id",
            "readiness_sha256", "manifest_sha256", "iterations", "incumbent",
            "discovery_outcome", "hidden_outcome", "accounting", "cleanup",
            "artifact_digests",
        },
    }
    for name, keys in expected_json_keys.items():
        assert set(json.loads((run_root / name).read_bytes())) == keys
    role_keys = {
        "schema_version", "call_index", "iteration", "role",
        "payload_sha256", "payload",
    }
    for role_name in ("investigator", "author", "critic"):
        assert set(
            json.loads(
                (run_root / f"iterations/001/{role_name}.json").read_bytes()
            )
        ) == role_keys
    assert set(json.loads((run_root / "iterations/001/validation.json").read_bytes())) == {
        "schema_version", "failure_code", "candidate_identity",
        "author_manifest_matches", "focused_checks", "worker_attestation",
        "changed_paths", "changed_symbols",
    }
    assert set(json.loads((run_root / "iterations/001/discovery.json").read_bytes())) == {
        "schema_version", "fixed_baseline_comparison", "incumbent_diagnostics",
        "rankable", "strictly_improves_incumbent", "folds",
        "engine_policy_sha256", "candidate_identity_sha256",
    }
    assert set(json.loads((run_root / "iterations/001/decision.json").read_bytes())) == {
        "schema_version", "rankable", "quantized_score",
        "prior_incumbent_identity_sha256", "new_incumbent_identity_sha256", "decision",
    }
    durable = b"\n".join(
        path.read_bytes() for path in run_root.rglob("*") if path.is_file()
    )
    for sentinel in (
        b"RAW_PROVIDER_SECRET_SENTINEL",
        b"SYSTEM_PROMPT_SECRET_SENTINEL",
        b"OPERATOR_APPROVAL_SECRET_SENTINEL",
        b"AUTHORIZATION_HEADER_SECRET_SENTINEL",
    ):
        assert sentinel not in durable


def _decision_calls(state: object, *, iteration: int):
    from unittest.mock import Mock

    from core.pit_optimization_contract import AuthorArtifact, CriticArtifact, InvestigatorArtifact

    investigator = Mock()
    investigator.payload = InvestigatorArtifact(
        hypothesis_id=f"hypothesis_{iteration}",
        family="entry",
        evidence_ids=("evidence",),
        causal_rationale="Bounded causal rationale.",
        target_paths=("core/strategy_policy/entry.py",),
        target_symbols=("core.strategy_policy.entry.evaluate_entry",),
        expected_diagnostic_changes=("entries",),
        known_risks=(),
        author_instructions=("Keep the patch bounded.",),
    )
    author = Mock()
    author.payload = AuthorArtifact(
        hypothesis_id=f"hypothesis_{iteration}",
        behavioral_summary="Bounded entry behavior.",
        changed_paths=("core/strategy_policy/entry.py",),
        changed_symbols=("core.strategy_policy.entry.evaluate_entry",),
        unified_diff=_entry_diff("None", "True"),
        assumptions=(),
        validation_suggestions=(),
    )
    critic = Mock()
    critic.payload = CriticArtifact(
        hypothesis_id=f"hypothesis_{iteration}",
        prediction_vs_observation="The measured result matched the bounded evidence.",
        causal_explanation="The fixed-baseline score is authoritative.",
        evidence_ids=("evidence",),
        disposition="refine",
        next_direction="Retain only strict fixed-baseline improvements.",
    )
    state.last_investigator = investigator
    state.last_author = author
    return critic


def test_incumbent_transition_replaces_diff_only_after_critic_and_fixed_score(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: incumbent state could follow diagnostic deltas or precede criticism."""
    from types import SimpleNamespace
    from unittest.mock import Mock

    import core.pit_optimizer_controller as controller
    from core.pit_optimizer_controller import (
        CandidateValidationOutcome,
        CandidateWorkspace,
        PitOptimizerServices,
        _persist_iteration_decision,
    )
    from core.pit_optimizer_evaluation import (
        DiscoveryComparison,
        DiscoveryEvaluation,
        DiscoveryScore,
        FoldEvaluationResult,
    )

    readiness = _prepare(_prepare_fixture(tmp_path))
    state = _run_state(readiness)
    state.iteration_workspace = CandidateWorkspace("workspace_improver", tmp_path.resolve())
    state.prospective_source_bundle = SimpleNamespace(files=())
    identity = Mock()
    identity.identity_sha256 = "a" * 64
    identity.to_primitive.return_value = {"identity_sha256": "a" * 64}
    validation = CandidateValidationOutcome(
        True,
        None,
        "incremental diff",
        "cumulative winning diff",
        identity,
        ("core/strategy_policy/entry.py",),
        ("core.strategy_policy.entry.evaluate_entry",),
    )
    fixed_score = controller.discovery_score_from_folds(
        tuple(
            replace(fold, total_return_pct=fold.total_return_pct + 1.0)
            for fold in readiness.baseline_discovery.folds
        ),
        readiness.baseline_discovery.folds,
        original_baseline_sha256=controller._folds_digest(readiness.baseline_discovery.folds),
        expected_original_baseline_sha256=controller._folds_digest(
            readiness.baseline_discovery.folds
        ),
    )
    diagnostics = DiscoveryScore(Decimal("0.50"), Decimal("0.25"), Decimal("2.00"))
    folds = tuple(
        FoldEvaluationResult(
            fold_id=baseline.fold_id,
            engine_policy_sha256=readiness.manifest.effective_policy_sha256,
            candidate_identity_sha256="a" * 64,
            aggregate_metrics=replace(
                baseline,
                total_return_pct=baseline.total_return_pct + 1.0,
                excess_total_return_pp=1.0,
            ),
        )
        for baseline in readiness.baseline_discovery.folds
    )
    discovery = DiscoveryEvaluation(
        folds,
        DiscoveryComparison(fixed_score, diagnostics, True, True),
    )
    critic = _decision_calls(state, iteration=1)
    critic.plan = next(
        item
        for item in readiness.manifest.call_budgets
        if item.iteration == 1 and item.role == "critic"
    )
    events = ["critic"]
    services = Mock(spec=PitOptimizerServices)
    services.write_diff_artifact.side_effect = lambda name, value: (
        events.append(name) or (tmp_path / name).resolve(),
        hashlib.sha256(value.encode()).hexdigest(),
    )
    services.write_json_artifact.side_effect = lambda name, value: (
        events.append(name) or (tmp_path / name).resolve(),
        hashlib.sha256(_canonical_bytes(value)).hexdigest(),
    )
    monkeypatch.setattr(controller, "validate_candidate_identity", lambda item: None)
    monkeypatch.setattr(controller, "_record_artifact", lambda *args: None)

    outcome = _persist_iteration_decision(
        readiness,
        state,
        validation,
        discovery,
        critic,
        services,
    )

    assert events == ["critic", "iterations/001/decision.json", "incumbent.diff"]
    assert outcome.incumbent_changed is True
    assert state.incumbent_cumulative_diff == "cumulative winning diff"
    assert state.incumbent_discovery.score == fixed_score
    assert state.non_improving_streak == 0


@pytest.mark.parametrize("failure_kind", ["write", "directory_sync"])
def test_decision_durability_failure_preserves_prior_incumbent_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    """Break caught: state/capability could advance before decision durability."""
    from types import SimpleNamespace
    from unittest.mock import Mock

    import core.pit_optimizer_artifacts as artifacts
    import core.pit_optimizer_controller as controller
    from core.pit_optimizer_artifacts import IncrementalArtifactStore
    from core.pit_optimizer_controller import (
        CandidateValidationOutcome,
        CandidateWorkspace,
        PitOptimizerServices,
        _persist_iteration_decision,
    )
    from core.pit_optimizer_evaluation import (
        DiscoveryComparison,
        DiscoveryEvaluation,
        FoldEvaluationResult,
    )

    readiness = _prepare(_prepare_fixture(tmp_path))
    state = _run_state(readiness)
    root = (tmp_path / "run").resolve()
    root.mkdir()
    store = IncrementalArtifactStore(root)
    store.prepare_iteration(1)
    critic_path, critic_digest = store.write_json_artifact(
        "iterations/001/critic.json",
        {"schema_version": 2},
    )
    state.artifact_root = root
    state.artifact_paths.append((critic_path, critic_digest))
    prior_root = (tmp_path / "prior").resolve()
    candidate_root = (tmp_path / "candidate").resolve()
    prior_root.mkdir()
    candidate_root.mkdir()
    prior_workspace = CandidateWorkspace("workspace_prior", prior_root)
    candidate_workspace = CandidateWorkspace("workspace_candidate", candidate_root)
    prior_identity = Mock()
    prior_identity.identity_sha256 = "b" * 64
    candidate_identity = Mock()
    candidate_identity.identity_sha256 = "a" * 64
    candidate_identity.cumulative_diff_sha256 = hashlib.sha256(
        b"cumulative winning diff"
    ).hexdigest()
    candidate_identity.editable_file_sha256s = ()
    state.incumbent_workspace = prior_workspace
    state.incumbent_identity = prior_identity
    state.incumbent_cumulative_diff = "prior incumbent diff"
    state.iteration_workspace = candidate_workspace
    state.prospective_source_bundle = SimpleNamespace(files=())
    validation = CandidateValidationOutcome(
        True,
        None,
        "incremental diff",
        "cumulative winning diff",
        candidate_identity,
        ("core/strategy_policy/entry.py",),
        ("core.strategy_policy.entry.evaluate_entry",),
    )
    fixed_score = controller.discovery_score_from_folds(
        tuple(
            replace(fold, total_return_pct=fold.total_return_pct + 1.0)
            for fold in readiness.baseline_discovery.folds
        ),
        readiness.baseline_discovery.folds,
        original_baseline_sha256=controller._folds_digest(
            readiness.baseline_discovery.folds
        ),
        expected_original_baseline_sha256=controller._folds_digest(
            readiness.baseline_discovery.folds
        ),
    )
    folds = tuple(
        FoldEvaluationResult(
            fold_id=baseline.fold_id,
            engine_policy_sha256=readiness.manifest.effective_policy_sha256,
            candidate_identity_sha256="a" * 64,
            aggregate_metrics=replace(
                baseline,
                total_return_pct=baseline.total_return_pct + 1.0,
                excess_total_return_pp=1.0,
            ),
        )
        for baseline in readiness.baseline_discovery.folds
    )
    discovery = DiscoveryEvaluation(
        folds,
        DiscoveryComparison(fixed_score, fixed_score, True, True),
    )
    critic = _decision_calls(state, iteration=1)
    services = Mock(spec=PitOptimizerServices)
    services.write_diff_artifact.side_effect = store.write_diff_artifact
    if failure_kind == "write":
        services.write_json_artifact.side_effect = OSError(
            "injected decision write failure"
        )
    else:
        services.write_json_artifact.side_effect = store.write_json_artifact
        original_sync = artifacts._fsync_directory

        def fail_iteration_sync(path: Path) -> None:
            if path.name == "001":
                raise OSError("injected decision directory sync failure")
            original_sync(path)

        monkeypatch.setattr(artifacts, "_fsync_directory", fail_iteration_sync)
    monkeypatch.setattr(controller, "validate_candidate_identity", lambda item: None)

    with pytest.raises(OSError, match="injected decision"):
        _persist_iteration_decision(
            readiness,
            state,
            validation,
            discovery,
            critic,
            services,
        )

    assert state.incumbent_workspace is prior_workspace
    assert state.incumbent_identity is prior_identity
    assert state.incumbent_cumulative_diff == "prior incumbent diff"
    assert state.incumbent_updates == 0
    assert state.valid_evaluations == 0
    assert state.iterations_completed == 0
    services.dispose_candidate.assert_not_called()
    services.write_diff_artifact.assert_not_called()


def test_next_context_oversize_keeps_prior_incumbent_without_stagnation_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a candidate-attributable context overflow could discard the incumbent."""
    from unittest.mock import Mock

    import core.pit_optimizer_controller as controller
    from core.pit_optimizer_controller import (
        CandidateValidationOutcome,
        PitOptimizerServices,
        _persist_iteration_decision,
    )

    readiness = _prepare(_prepare_fixture(tmp_path))
    state = _run_state(readiness)
    state.incumbent_cumulative_diff = "prior incumbent diff"
    critic = _decision_calls(state, iteration=1)
    validation = CandidateValidationOutcome(
        False,
        "next_context_oversize",
        "oversized candidate diff",
        "prior incumbent diff",
        None,
        ("core/strategy_policy/entry.py",),
        ("core.strategy_policy.entry.evaluate_entry",),
    )
    services = Mock(spec=PitOptimizerServices)
    writes: list[str] = []
    services.write_json_artifact.side_effect = lambda name, value: (
        writes.append(name) or (tmp_path / name).resolve(),
        hashlib.sha256(_canonical_bytes(value)).hexdigest(),
    )
    monkeypatch.setattr(controller, "_record_artifact", lambda *args: None)

    outcome = _persist_iteration_decision(
        readiness,
        state,
        validation,
        None,
        critic,
        services,
    )

    assert outcome.incumbent_changed is False
    assert state.incumbent_cumulative_diff == "prior incumbent diff"
    assert state.non_improving_streak == 0
    services.write_diff_artifact.assert_not_called()
    assert writes == ["iterations/001/decision.json"]


def test_real_next_context_oversize_becomes_safe_invalid_and_reaches_critic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: real cumulative overflow became terminal identity drift."""
    from unittest.mock import Mock

    import core.pit_optimizer_controller as controller
    from core.pit_optimizer_controller import (
        CandidateValidationOutcome,
        CandidateWorkspace,
        PitOptimizerServices,
        _run_critic,
        _validate_iteration_candidate,
    )

    inputs = _prepare_fixture(tmp_path)
    readiness = _prepare(inputs)
    state = _run_state(readiness)
    state.iteration_workspace = CandidateWorkspace(
        "workspace_context_overflow",
        Path(inputs["source_root"]).resolve(),
    )
    critic = _decision_calls(state, iteration=1)
    critic.plan = next(
        item
        for item in readiness.manifest.call_budgets
        if item.iteration == 1 and item.role == "critic"
    )
    candidate_identity = Mock()
    oversized = "x" * (64 * 1024 + 1)
    supplied = CandidateValidationOutcome(
        True,
        None,
        "incremental candidate diff",
        oversized,
        candidate_identity,
        ("core/strategy_policy/entry.py",),
        ("core.strategy_policy.entry.evaluate_entry",),
    )
    services = Mock(spec=PitOptimizerServices)
    services.validate_and_apply.return_value = supplied
    payloads: dict[str, Mapping[str, object]] = {}
    services.write_diff_artifact.side_effect = lambda name, value: (
        (tmp_path / name).resolve(),
        hashlib.sha256(value.encode()).hexdigest(),
    )
    services.write_json_artifact.side_effect = lambda name, value: (
        payloads.setdefault(name, value) and (tmp_path / name).resolve(),
        hashlib.sha256(_canonical_bytes(value)).hexdigest(),
    )
    monkeypatch.setattr(controller, "_require_identity_graph", lambda *args: None)
    monkeypatch.setattr(controller, "validate_author_manifest", lambda *args: None)
    monkeypatch.setattr(controller, "_record_artifact", lambda *args: None)

    validation = _validate_iteration_candidate(
        readiness,
        state,
        state.last_author,
        services,
    )

    assert validation.valid is False
    assert validation.failure_code == "next_context_oversize"
    assert validation.identity is None
    assert state.prospective_source_bundle is None
    assert payloads["iterations/001/validation.json"]["author_manifest_matches"] is True
    captured: list[object] = []
    monkeypatch.setattr(
        controller,
        "_call_role",
        lambda ready, current, service_values, plan, role_input, parser: (
            captured.append(role_input) or critic
        ),
    )

    assert (
        _run_critic(
            readiness,
            state,
            state.last_investigator,
            state.last_author,
            validation,
            None,
            services,
        )
        is critic
    )
    assert captured[0].validation.failure_code == "next_context_oversize"


def test_complete_three_role_context_preflight_stops_before_iteration_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: investigator authority could be spent before later context overflow."""
    from unittest.mock import Mock

    import core.pit_optimizer_controller as controller
    from core.pit_optimizer_controller import (
        CandidateWorkspace,
        PitOptimizerServices,
        run_pit_optimizer_v2,
    )

    inputs = _prepare_fixture(tmp_path)
    readiness = _prepare(inputs)
    narrowed = tuple(
        replace(item, max_dynamic_input_bytes=1)
        if item.iteration == 2 and item.role == "critic"
        else item
        for item in readiness.manifest.call_budgets
    )
    manifest = replace(readiness.manifest, call_budgets=narrowed)
    readiness = replace(
        readiness,
        manifest=manifest,
        manifest_sha256=manifest.sha256,
    )
    services = Mock(spec=PitOptimizerServices)
    services.create_candidate.return_value = CandidateWorkspace(
        "workspace_context_preflight",
        Path(inputs["source_root"]).resolve(),
    )
    monkeypatch.setattr(controller, "_initialize_run_artifacts", lambda *args: None)
    monkeypatch.setattr(controller, "_pre_iteration_stop", lambda *args: None)
    observed: dict[str, object] = {}
    sentinel = Mock()

    def finalize(
        ready: object,
        state: object,
        service_values: object,
        terminal_code: str,
    ) -> object:
        observed["started"] = state.iterations_started
        observed["terminal_code"] = terminal_code
        observed["detail"] = state.terminal_detail
        return sentinel

    monkeypatch.setattr(controller, "_finalize_result", finalize)

    assert run_pit_optimizer_v2(readiness=readiness, services=services) is sentinel
    assert observed == {
        "started": 0,
        "terminal_code": "budget_exhausted",
        "detail": "context_budget_exhausted",
    }
    services.prepare_iteration_artifacts.assert_not_called()
    services.call_role.assert_not_called()


def test_stop_condition_precedence_and_complete_iteration_budget(
    tmp_path: Path,
) -> None:
    """Break caught: the loop could start a role when all three calls cannot complete."""
    from unittest.mock import Mock

    from core.pit_optimizer_controller import PitOptimizerServices, _pre_iteration_stop

    readiness = _prepare(_prepare_fixture(tmp_path))
    state = _run_state(readiness)
    pricing, lease = _pricing_and_lease(readiness.manifest)
    state.frozen_pricing = pricing
    state.authorization_lease = lease
    services = Mock(spec=PitOptimizerServices)
    services.cancellation_requested.return_value = False

    assert _pre_iteration_stop(readiness, state, services) is None
    state.non_improving_streak = 3
    assert _pre_iteration_stop(readiness, state, services) == (
        "stagnation_limit",
        None,
    )
    state.non_improving_streak = 0
    services.cancellation_requested.return_value = True
    assert _pre_iteration_stop(readiness, state, services) == ("cancelled", None)
    services.cancellation_requested.return_value = False
    state.authorization_lease = replace(lease, max_calls=2)
    assert _pre_iteration_stop(readiness, state, services) == (
        "budget_exhausted",
        "call_budget_exhausted",
    )
    state.authorization_lease = replace(lease, max_tokens=223_999)
    assert _pre_iteration_stop(readiness, state, services) == (
        "budget_exhausted",
        "token_budget_exhausted",
    )
    state.authorization_lease = replace(lease, max_usd=0.19)
    assert _pre_iteration_stop(readiness, state, services) == (
        "budget_exhausted",
        "cost_budget_exhausted",
    )
    state.authorization_lease = lease
    state.next_iteration = 3
    assert _pre_iteration_stop(readiness, state, services) == (
        "iteration_limit",
        None,
    )


@pytest.mark.parametrize(
    ("exception_name", "terminal_code", "terminal_detail"),
    [
        ("ProviderProtocolFailure", "provider_protocol_failure", None),
        ("ProviderAccountingFailure", "provider_accounting_failure", None),
        ("AuditFailure", "audit_failure", None),
        ("AuthorizationExhausted", "authorization_exhausted", None),
        ("IdentityDrift", "identity_drift", None),
        ("TrustedEvaluatorNondeterminism", "trusted_evaluator_nondeterminism", None),
        ("SandboxIntegrityFailure", "sandbox_integrity_failure", None),
        ("EvidenceTampering", "evidence_tampering", None),
        ("ContextBudgetExhausted", "budget_exhausted", "context_budget_exhausted"),
    ],
)
def test_terminal_boundary_classification_is_closed(
    exception_name: str,
    terminal_code: str,
    terminal_detail: str | None,
) -> None:
    """Break caught: integrity/accounting failures could become critic feedback or normal stops."""
    import core.pit_optimizer_controller as controller

    exception_type = getattr(controller, exception_name)
    assert controller._terminal_from_exception(exception_type("injected")) == (
        terminal_code,
        terminal_detail,
    )


def test_terminal_boundary_run_routes_partial_iteration_to_common_finalizer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a partial protocol failure could bypass lease cleanup/final evidence."""
    from unittest.mock import Mock

    import core.pit_optimizer_controller as controller
    from core.pit_optimizer_controller import (
        PitOptimizerReadiness,
        PitOptimizerServices,
        ProviderProtocolFailure,
        run_pit_optimizer_v2,
    )

    readiness = Mock(spec=PitOptimizerReadiness)
    readiness.manifest.run_id = "run_task7"
    readiness.manifest.max_iterations = 2
    readiness.manifest.call_budgets = ()
    readiness.baseline_discovery = Mock()
    services = Mock(spec=PitOptimizerServices)
    events: list[str] = []
    monkeypatch.setattr(
        controller,
        "_initialize_run_artifacts",
        lambda *args: events.append("initialize"),
    )
    monkeypatch.setattr(controller, "_pre_iteration_stop", lambda *args: None)
    monkeypatch.setattr(controller, "_prepare_iteration_source", lambda *args: None)
    iteration_directory = (tmp_path / "iterations" / "001").resolve()
    iteration_directory.mkdir(parents=True)
    services.prepare_iteration_artifacts.return_value = iteration_directory
    monkeypatch.setattr(
        controller,
        "_run_iteration",
        lambda *args: (_ for _ in ()).throw(ProviderProtocolFailure("malformed")),
    )
    sentinel = Mock()
    monkeypatch.setattr(
        controller,
        "_finalize_result",
        lambda ready, state, service_values, code: events.append(code) or sentinel,
        raising=False,
    )

    assert run_pit_optimizer_v2(readiness=readiness, services=services) is sentinel
    assert events == ["initialize", "provider_protocol_failure"]


def test_discovery_repeat_requires_byte_identical_incumbent_evidence(
    tmp_path: Path,
) -> None:
    """Break caught: a nondeterministic discovery winner could cross the hidden boundary."""
    from unittest.mock import Mock

    from core.pit_optimizer_controller import (
        CandidateWorkspace,
        PitOptimizerServices,
        TrustedEvaluatorNondeterminism,
        _finish_discovery,
    )
    from core.pit_optimizer_evaluation import DeterminismAttestation

    readiness = _prepare(_prepare_fixture(tmp_path))
    state = _run_state(readiness)
    services = Mock(spec=PitOptimizerServices)
    assert _finish_discovery(readiness, state, services) is None
    services.confirm_discovery.assert_not_called()

    identity = Mock()
    identity.identity_sha256 = "a" * 64
    state.incumbent_identity = identity
    state.incumbent_workspace = CandidateWorkspace("workspace_winner", tmp_path.resolve())
    expected = state.incumbent_discovery.evidence_ids[0]
    services.confirm_discovery.return_value = DeterminismAttestation(
        fold_id="discovery_1",
        expected_evidence_sha256=expected,
        repeated_evidence_sha256=expected,
        matched=True,
    )
    assert _finish_discovery(readiness, state, services).matched is True

    services.confirm_discovery.return_value = DeterminismAttestation(
        fold_id="discovery_1",
        expected_evidence_sha256=expected,
        repeated_evidence_sha256="f" * 64,
        matched=False,
    )
    with pytest.raises(TrustedEvaluatorNondeterminism):
        _finish_discovery(readiness, state, services)


def _hidden_result(
    fold_id: str,
    *,
    baseline_return: float = 1.0,
    candidate_return: float = 1.1,
    closed_trades: int = 3,
    safety_complete: bool = True,
    integrity_complete: bool = True,
    accounting_complete: bool = True,
):
    from core.pit_optimizer_evaluation import HiddenEvaluation, HoldoutDecision

    baseline = replace(
        _aggregate(fold_id, baseline_return),
        closed_trades=3,
        excess_total_return_pp=None,
    )
    candidate = replace(
        _aggregate(fold_id, candidate_return),
        closed_trades=closed_trades,
        excess_total_return_pp=candidate_return - baseline_return,
    )
    return HiddenEvaluation(
        baseline,
        candidate,
        HoldoutDecision.from_result(
            excess_total_return_pp=Decimal(str(candidate_return))
            - Decimal(str(baseline_return)),
            closed_trades=closed_trades,
            safety_complete=safety_complete,
            integrity_complete=integrity_complete,
            accounting_complete=accounting_complete,
        ),
    )


def _hidden_attestation(
    readiness: object,
    identity_sha256: str,
    reservation: object,
    hidden: object,
    *,
    attested_candidate_identity_sha256: str | None = None,
    reuse_reset: bool = False,
):
    from core.pit_optimizer_evaluation import (
        HiddenEvaluationAttestation,
        HiddenResetReceipt,
    )

    fold_id = readiness.manifest.fold_manifest.hidden_fold.fold_id
    baseline_reset = HiddenResetReceipt(
        fold_id=fold_id,
        subject="baseline",
        subject_identity_sha256=readiness.manifest.effective_policy_sha256,
        reset_receipt_sha256="d" * 64,
    )
    candidate_reset = HiddenResetReceipt(
        fold_id=fold_id,
        subject="candidate",
        subject_identity_sha256=(
            identity_sha256
            if attested_candidate_identity_sha256 is None
            else attested_candidate_identity_sha256
        ),
        reset_receipt_sha256="d" * 64 if reuse_reset else "e" * 64,
    )
    return HiddenEvaluationAttestation.issue(
        reservation_record_sha256=reservation.reservation_record_sha256,
        source_head=readiness.manifest.source_head,
        source_fingerprint_sha256=readiness.manifest.source_fingerprint_sha256,
        baseline_policy_sha256=readiness.manifest.effective_policy_sha256,
        candidate_identity_sha256=(
            identity_sha256
            if attested_candidate_identity_sha256 is None
            else attested_candidate_identity_sha256
        ),
        fold_id=fold_id,
        baseline_reset=baseline_reset,
        candidate_reset=candidate_reset,
        evaluation=hidden,
    )


def _hidden_outcome_proof(
    reservation: object,
    *,
    attempted: bool,
    completed: bool,
    failure_code: str | None,
):
    from core.pit_optimizer_evaluation import ValidationOutcomeProof

    return ValidationOutcomeProof(
        reservation_record_sha256=reservation.reservation_record_sha256,
        attempted=attempted,
        completed=completed,
        failure_code=failure_code,
        outcome_record_sha256="f" * 64,
        ledger_head_sha256="f" * 64,
    )


def test_provider_call_is_impossible_after_hidden_boundary() -> None:
    """Break caught: a role call could observe or react to hidden validation."""
    from unittest.mock import Mock

    from core.pit_optimizer_controller import _RunState, _call_role

    state = Mock(spec=_RunState)
    state.provider_enabled = False
    with pytest.raises(RuntimeError, match="provider capability is closed"):
        _call_role(Mock(), state, Mock(), Mock(), Mock(), Mock())


def test_hidden_boundary_reserves_once_closes_provider_then_writes_holdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: hidden results could be evaluated before provider revocation/reservation."""
    from unittest.mock import Mock

    import core.pit_optimizer_controller as controller
    from core.pit_optimizer_controller import (
        CandidateWorkspace,
        PitOptimizerServices,
        _call_role,
        _run_hidden_once,
    )
    from core.pit_optimizer_evaluation import ValidationReservation

    readiness = _prepare(_prepare_fixture(tmp_path))
    state = _run_state(readiness)
    identity = Mock()
    identity.identity_sha256 = "a" * 64
    state.incumbent_identity = identity
    state.incumbent_workspace = CandidateWorkspace("workspace_hidden", tmp_path.resolve())
    reservation = ValidationReservation("b" * 64, "c" * 64)
    hidden = _hidden_result(readiness.manifest.fold_manifest.hidden_fold.fold_id)
    attestation = _hidden_attestation(
        readiness,
        identity.identity_sha256,
        reservation,
        hidden,
    )
    services = Mock(spec=PitOptimizerServices)
    events: list[str] = []
    services.reserve_hidden_validation.side_effect = (
        lambda candidate: events.append("reserve") or reservation
    )

    def evaluate(*args: object):
        events.append("evaluate")
        assert state.provider_enabled is False
        with pytest.raises(RuntimeError, match="provider capability is closed"):
            _call_role(readiness, state, services, Mock(), Mock(), Mock())
        return attestation

    services.evaluate_hidden.side_effect = evaluate
    services.record_hidden_outcome.side_effect = (
        lambda reserved, attempted, completed, failure_code: events.append("outcome")
        or _hidden_outcome_proof(
            reserved,
            attempted=attempted,
            completed=completed,
            failure_code=failure_code,
        )
    )
    holdout_payloads: list[Mapping[str, object]] = []

    def write(name: str, value: Mapping[str, object]) -> tuple[Path, str]:
        events.append(name)
        holdout_payloads.append(value)
        return (tmp_path / name).resolve(), hashlib.sha256(_canonical_bytes(value)).hexdigest()

    services.write_json_artifact.side_effect = write
    monkeypatch.setattr(controller, "_record_artifact", lambda *args: None)

    assert _run_hidden_once(readiness, state, services) is hidden

    assert events == ["reserve", "evaluate", "holdout.json", "outcome"]
    services.reserve_hidden_validation.assert_called_once_with(identity)
    services.evaluate_hidden.assert_called_once_with(
        state.incumbent_workspace,
        identity,
        reservation,
    )
    assert state.hidden_validation_opened is True
    assert state.validation_reservation is reservation
    assert state.hidden_evaluation is hidden
    services.record_hidden_outcome.assert_called_once_with(
        reservation,
        True,
        True,
        None,
    )
    assert holdout_payloads[0]["consumed_validation_key_sha256"] == "b" * 64
    assert holdout_payloads[0]["candidate_identity_sha256"] == "a" * 64


@pytest.mark.parametrize(
    ("candidate_return", "closed_trades", "complete", "expected"),
    [
        (1.10, 3, True, True),
        (1.09, 3, True, False),
        (1.10, 2, True, False),
        (1.10, 3, False, False),
    ],
)
def test_hidden_qualification_is_quantized_and_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_return: float,
    closed_trades: int,
    complete: bool,
    expected: bool,
) -> None:
    """Break caught: holdout eligibility could ignore the exact return/trade/completeness gate."""
    from unittest.mock import Mock

    import core.pit_optimizer_controller as controller
    from core.pit_optimizer_controller import CandidateWorkspace, PitOptimizerServices, _run_hidden_once
    from core.pit_optimizer_evaluation import ValidationReservation

    readiness = _prepare(_prepare_fixture(tmp_path))
    state = _run_state(readiness)
    identity = Mock()
    identity.identity_sha256 = "a" * 64
    state.incumbent_identity = identity
    state.incumbent_workspace = CandidateWorkspace("workspace_hidden", tmp_path.resolve())
    services = Mock(spec=PitOptimizerServices)
    reservation = ValidationReservation("b" * 64, "c" * 64)
    services.reserve_hidden_validation.return_value = reservation
    hidden = _hidden_result(
        readiness.manifest.fold_manifest.hidden_fold.fold_id,
        candidate_return=candidate_return,
        closed_trades=closed_trades,
        safety_complete=complete,
        integrity_complete=complete,
        accounting_complete=complete,
    )
    services.evaluate_hidden.return_value = _hidden_attestation(
        readiness,
        identity.identity_sha256,
        reservation,
        hidden,
    )
    services.record_hidden_outcome.return_value = _hidden_outcome_proof(
        reservation,
        attempted=True,
        completed=True,
        failure_code=None,
    )
    services.write_json_artifact.side_effect = lambda name, value: (
        (tmp_path / name).resolve(),
        hashlib.sha256(_canonical_bytes(value)).hexdigest(),
    )
    monkeypatch.setattr(controller, "_record_artifact", lambda *args: None)

    result = _run_hidden_once(readiness, state, services)

    assert result.decision.long_replay_eligible is expected


@pytest.mark.parametrize("failure_kind", ["wrong_identity", "reset_reuse"])
def test_hidden_attestation_rejects_wrong_identity_or_reused_reset_and_closes_outcome(
    tmp_path: Path,
    failure_kind: str,
) -> None:
    """Break caught: unbound or non-independent hidden evidence could qualify replay."""
    from unittest.mock import Mock

    from core.pit_optimizer_controller import (
        CandidateWorkspace,
        EvidenceTampering,
        IdentityDrift,
        PitOptimizerServices,
        _run_hidden_once,
    )
    from core.pit_optimizer_evaluation import ValidationReservation

    readiness = _prepare(_prepare_fixture(tmp_path))
    state = _run_state(readiness)
    identity = Mock()
    identity.identity_sha256 = "a" * 64
    state.incumbent_identity = identity
    state.incumbent_workspace = CandidateWorkspace(
        "workspace_hidden_binding",
        tmp_path.resolve(),
    )
    reservation = ValidationReservation("b" * 64, "c" * 64)
    hidden = _hidden_result(readiness.manifest.fold_manifest.hidden_fold.fold_id)
    attestation = _hidden_attestation(
        readiness,
        identity.identity_sha256,
        reservation,
        hidden,
        attested_candidate_identity_sha256=(
            "9" * 64 if failure_kind == "wrong_identity" else None
        ),
        reuse_reset=failure_kind == "reset_reuse",
    )
    services = Mock(spec=PitOptimizerServices)
    services.reserve_hidden_validation.return_value = reservation
    services.evaluate_hidden.return_value = attestation
    services.record_hidden_outcome.return_value = _hidden_outcome_proof(
        reservation,
        attempted=True,
        completed=False,
        failure_code="integrity_failed",
    )

    expected = IdentityDrift if failure_kind == "wrong_identity" else EvidenceTampering
    with pytest.raises(expected):
        _run_hidden_once(readiness, state, services)

    services.record_hidden_outcome.assert_called_once_with(
        reservation,
        True,
        False,
        "integrity_failed",
    )
    services.write_json_artifact.assert_not_called()
    assert state.hidden_evaluation is None


def test_hidden_evaluation_failure_records_content_free_outcome(
    tmp_path: Path,
) -> None:
    """Break caught: an evaluation exception could leave a permanent reservation open."""
    from unittest.mock import Mock

    from core.pit_optimizer_controller import (
        CandidateWorkspace,
        PitOptimizerServices,
        _run_hidden_once,
    )
    from core.pit_optimizer_evaluation import ValidationReservation

    readiness = _prepare(_prepare_fixture(tmp_path))
    state = _run_state(readiness)
    identity = Mock()
    identity.identity_sha256 = "a" * 64
    state.incumbent_identity = identity
    state.incumbent_workspace = CandidateWorkspace(
        "workspace_hidden_failure",
        tmp_path.resolve(),
    )
    reservation = ValidationReservation("b" * 64, "c" * 64)
    services = Mock(spec=PitOptimizerServices)
    services.reserve_hidden_validation.return_value = reservation
    services.evaluate_hidden.side_effect = RuntimeError("injected hidden replay failure")
    services.record_hidden_outcome.return_value = _hidden_outcome_proof(
        reservation,
        attempted=True,
        completed=False,
        failure_code="replay_failed",
    )

    with pytest.raises(RuntimeError, match="injected hidden replay failure"):
        _run_hidden_once(readiness, state, services)

    services.record_hidden_outcome.assert_called_once_with(
        reservation,
        True,
        False,
        "replay_failed",
    )
    services.write_json_artifact.assert_not_called()
    assert state.hidden_evaluation is None


@pytest.mark.parametrize("failure_kind", ["write", "directory_sync"])
def test_holdout_durability_failure_preserves_uncommitted_hidden_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    """Break caught: hidden eligibility could advance without durable holdout evidence."""
    from unittest.mock import Mock

    import core.pit_optimizer_artifacts as artifacts
    from core.pit_optimizer_artifacts import IncrementalArtifactStore
    from core.pit_optimizer_controller import (
        CandidateWorkspace,
        PitOptimizerServices,
        _run_hidden_once,
    )
    from core.pit_optimizer_evaluation import ValidationReservation

    readiness = _prepare(_prepare_fixture(tmp_path))
    state = _run_state(readiness)
    identity = Mock()
    identity.identity_sha256 = "a" * 64
    state.incumbent_identity = identity
    state.incumbent_workspace = CandidateWorkspace(
        "workspace_hidden_durability",
        tmp_path.resolve(),
    )
    reservation = ValidationReservation("b" * 64, "c" * 64)
    hidden = _hidden_result(readiness.manifest.fold_manifest.hidden_fold.fold_id)
    services = Mock(spec=PitOptimizerServices)
    services.reserve_hidden_validation.return_value = reservation
    services.evaluate_hidden.return_value = _hidden_attestation(
        readiness,
        identity.identity_sha256,
        reservation,
        hidden,
    )
    services.record_hidden_outcome.return_value = _hidden_outcome_proof(
        reservation,
        attempted=True,
        completed=False,
        failure_code="integrity_failed",
    )
    if failure_kind == "write":
        services.write_json_artifact.side_effect = OSError(
            "injected holdout write failure"
        )
    else:
        root = (tmp_path / "run").resolve()
        root.mkdir()
        store = IncrementalArtifactStore(root)
        services.write_json_artifact.side_effect = store.write_json_artifact
        original_sync = artifacts._fsync_directory

        def fail_root_sync(path: Path) -> None:
            if path == root:
                raise OSError("injected holdout directory sync failure")
            original_sync(path)

        monkeypatch.setattr(artifacts, "_fsync_directory", fail_root_sync)

    with pytest.raises(OSError, match="injected holdout"):
        _run_hidden_once(readiness, state, services)

    assert state.hidden_evaluation is None
    services.record_hidden_outcome.assert_called_once_with(
        reservation,
        True,
        False,
        "integrity_failed",
    )


def test_hidden_boundary_provider_projectable_inputs_omit_hidden_identity_and_results(
    tmp_path: Path,
) -> None:
    """Break caught: a hidden sentinel could leak through the readiness provider seed."""
    readiness = _prepare(_prepare_fixture(tmp_path))
    hidden = readiness.manifest.fold_manifest.hidden_fold
    payload = readiness.provider_seed.canonical_json_bytes()
    assert hidden.fold_id.encode() not in payload
    assert hidden.start_date.encode() not in payload
    assert hidden.end_date.encode() not in payload
    assert b"holdout" not in payload.lower()


def test_hidden_boundary_finalization_attempts_every_cleanup_and_writes_summary_last(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: one cleanup failure could suppress revocation, verification, or summary."""
    from unittest.mock import Mock

    import core.pit_optimizer_controller as controller
    from core.pit_optimizer_controller import (
        CandidateWorkspace,
        PitOptimizerServices,
        _finalize_result,
    )
    from core.pit_optimizer_evaluation import PitOptimizerCleanup

    readiness = _prepare(_prepare_fixture(tmp_path))
    state = _run_state(readiness)
    pricing, lease = _pricing_and_lease(readiness.manifest)
    state.frozen_pricing = pricing
    state.authorization_lease = lease
    state.lease_snapshot = lease
    state.artifact_root = (tmp_path / "run").resolve()
    state.incumbent_workspace = CandidateWorkspace("workspace_cleanup", tmp_path.resolve())
    services = Mock(spec=PitOptimizerServices)
    events: list[str] = []

    def fail_close(*args: object) -> None:
        events.append("close")
        raise RuntimeError("injected close failure")

    services.close_run_lease.side_effect = fail_close
    services.dispose_candidate.side_effect = lambda workspace: events.append("dispose") or PitOptimizerCleanup(
        True,
        True,
        False,
    )
    services.verify_inputs.side_effect = lambda ready: events.append("verify")

    def write(name: str, value: Mapping[str, object]) -> tuple[Path, str]:
        events.append(name)
        return (state.artifact_root / name).resolve(), hashlib.sha256(
            _canonical_bytes(value)
        ).hexdigest()

    services.write_json_artifact.side_effect = write
    monkeypatch.setattr(controller, "_record_artifact", lambda *args: None)

    result = _finalize_result(readiness, state, services, "iteration_limit")

    assert events == ["close", "dispose", "verify", "accounting.json", "summary.json"]
    assert result.status == "aborted"
    assert result.terminal_code == "audit_failure"
    assert result.cleanup_complete is False
    assert state.provider_enabled is False
    assert state.authorization_lease is None


def test_hidden_boundary_result_has_exact_fields_and_no_secret_bearing_paths(
    tmp_path: Path,
) -> None:
    """Break caught: public summary could serialize runtime paths or provider/source bodies."""
    from dataclasses import fields

    from core.pit_optimizer_controller import PitOptimizerResult

    expected_fields = (
        "schema_version",
        "phase",
        "status",
        "terminal_code",
        "terminal_detail",
        "exit_code",
        "run_id",
        "readiness_sha256",
        "manifest_sha256",
        "iterations_started",
        "iterations_completed",
        "valid_evaluations",
        "incumbent_updates",
        "non_improving_streak",
        "discovery_winner",
        "hidden_validation_opened",
        "validation_reservation_sha256",
        "long_replay_eligible",
        "budget",
        "artifact_root",
        "artifact_paths",
        "source_modified",
        "cleanup_complete",
    )
    assert tuple(item.name for item in fields(PitOptimizerResult)) == expected_fields
