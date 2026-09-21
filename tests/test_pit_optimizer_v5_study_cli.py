"""Fresh-process import and offline CLI checks for Task 8."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from core.pit_optimizer_v5.two_round_study.driver import _long_exists, _long_files, _long_read_bytes
from core.pit_optimizer_v5.artifacts import ArtifactRefV5, LocalArtifactRepositoryV5


_CREDENTIAL_NAMES = (
    "OPENROUTER_API_KEY",
    "OPENROUTER",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "FMP_API_KEY",
    "NOTIFY_EMAIL_PASSWORD",
)
_REPO_ROOT = Path(__file__).resolve().parents[1]
_TEST_ROOT = _REPO_ROOT / ".superpowers" / "sdd" / "2026-09-19-v5-two-round-example" / "task-8-test-runs"


def _new_test_root(label: str) -> Path:
    root = _TEST_ROOT / f"{label}-{uuid.uuid4().hex}"
    root.mkdir(parents=True)
    return root


def _clean_process_env(*, pythonpath: str | None = None) -> dict[str, str]:
    env: dict[str, str] = {}
    for name in ("PATH", "SystemRoot", "WINDIR", "PATHEXT", "TEMP", "TMP"):
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    env.update({name: "" for name in _CREDENTIAL_NAMES})
    env["PYTHON_DOTENV_DISABLED"] = "1"
    if pythonpath is not None:
        env["PYTHONPATH"] = pythonpath
    return env


def _inventory(root: Path) -> tuple[tuple[str, str], ...]:
    return tuple((relative, hashlib.sha256(_long_read_bytes(path)).hexdigest()) for relative, path in _long_files(root))


def _foreign_readonly_ledger(*, source_store, manifest, grant, root: Path):
    from core.pit_optimizer_v5.two_round_study.ledger import StudyLedgerV1
    from core.pit_optimizer_v5.two_round_study.store import StudyStoreV1

    root.mkdir(parents=True)
    foreign_store = StudyStoreV1(LocalArtifactRepositoryV5(root))
    foreign_grant = replace(
        grant,
        repository_root_identity_sha256=foreign_store.repository.root_identity_sha256,
    )
    foreign_store.put(
        kind="grants",
        key=foreign_grant.study_id,
        content=foreign_grant.canonical_bytes(),
    )
    foreign_store.put(
        kind="manifests",
        key=manifest.sha256,
        content=manifest.canonical_bytes(),
    )
    rubric_reference = next(
        reference for reference in source_store.list_refs(kind="rubrics") if reference.sha256 == manifest.rubric_sha256
    )
    for kind, reference in (
        ("preflights", manifest.primary_preflight_ref),
        ("preflights", manifest.withheld_preflight_ref),
        ("rubrics", rubric_reference),
    ):
        foreign_store.put(
            kind=kind,
            key=reference.relative_path.rsplit("/", 1)[-1][:-4],
            content=source_store.read(reference),
        )
    return foreign_store, StudyLedgerV1(foreign_store, manifest, foreign_grant, approval=None)


def test_foreign_readonly_ledger_setup_is_complete_before_public_store_guard(tmp_path: Path) -> None:
    from core.pit_optimizer_v5.two_round_study.contracts import StudyAuthorityError
    from core.pit_optimizer_v5.two_round_study.ledger import StudyLedgerV1
    from tests.test_pit_optimizer_v5_study_live_calls import _study_context

    _fixture, _fixture_request, _preflight, manifest, _request, store, grant, _approval, _ledger, *_ = _study_context(
        tmp_path,
    )
    foreign_root = tmp_path / "foreign-store"
    foreign_store, foreign_ledger = _foreign_readonly_ledger(
        source_store=store,
        manifest=manifest,
        grant=grant,
        root=foreign_root,
    )
    assert foreign_ledger.store is foreign_store
    assert foreign_ledger.store.repository.root_identity_sha256 != store.repository.root_identity_sha256
    assert foreign_ledger.manifest == manifest
    with pytest.raises(StudyAuthorityError, match="grant does not bind"):
        StudyLedgerV1(foreign_store, manifest, grant, approval=None)


def _tripwire_dir(root: Path) -> Path:
    tripwire = root / "tripwire"
    tripwire.mkdir()
    (tripwire / "sitecustomize.py").write_text(
        """from __future__ import annotations
import builtins
import io
import os
from pathlib import Path
import socket
import urllib.request
import dotenv

_install_marker = os.environ.get("TASK8_TRIPWIRE_INSTALL_MARKER")
if _install_marker:
    Path(_install_marker).write_text("installed", encoding="utf-8")
_marker = os.environ.get("TASK8_TRIPWIRE_MARKER")

def _fail(label, *args, **kwargs):
    if _marker:
        Path(_marker).write_text(label + "\\n", encoding="utf-8")
    raise RuntimeError("Task8 forbidden side effect")

dotenv.load_dotenv = lambda *args, **kwargs: _fail("dotenv.load_dotenv", *args, **kwargs)
socket.create_connection = lambda *args, **kwargs: _fail("socket.create_connection", *args, **kwargs)
socket.socket.connect = lambda *args, **kwargs: _fail("socket.socket.connect", *args, **kwargs)
socket.socket.connect_ex = lambda *args, **kwargs: _fail("socket.socket.connect_ex", *args, **kwargs)
urllib.request.urlopen = lambda *args, **kwargs: _fail("urllib.request.urlopen", *args, **kwargs)
_getenv = os.getenv
_names = {"OPENROUTER_API_KEY", "OPENROUTER", "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "FMP_API_KEY", "NOTIFY_EMAIL_PASSWORD"}

def _safe_getenv(name, default=None):
    if name in _names:
        _fail("credential getenv:" + name)
    return _getenv(name, default)

os.getenv = _safe_getenv
_environ_type = type(os.environ)
_environ_get = _environ_type.get
_environ_getitem = _environ_type.__getitem__

def _safe_environ_get(self, name, default=None):
    if name in _names:
        _fail("credential environ.get:" + name)
    return _environ_get(self, name, default)

def _safe_environ_getitem(self, name):
    if name in _names:
        _fail("credential environ[]:" + name)
    return _environ_getitem(self, name)

_environ_type.get = _safe_environ_get
_environ_type.__getitem__ = _safe_environ_getitem
_open = builtins.open
_io_open = io.open

def _is_env_path(file):
    try:
        return any(part == ".env" or part.startswith(".env.") for part in Path(str(file)).parts)
    except (TypeError, ValueError):
        return False

def _safe_open(file, *args, **kwargs):
    if _is_env_path(file):
        _fail(".env open")
    return _open(file, *args, **kwargs)

def _safe_io_open(file, *args, **kwargs):
    if _is_env_path(file):
        _fail(".env io.open")
    return _io_open(file, *args, **kwargs)

builtins.open = _safe_open
io.open = _safe_io_open
""",
        encoding="utf-8",
    )
    return tripwire


def _run_cli(*args: str, cwd: Path, env: dict[str, str], timeout: float = 1800) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "core.pit_optimizer_v5.two_round_study", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_fresh_import_tripwire_does_not_load_legacy_provider_settings() -> None:
    code = """
import importlib
import sys
import dotenv

calls = []
def fail(*args, **kwargs):
    calls.append((args, kwargs))
    raise RuntimeError('dotenv was called')
dotenv.load_dotenv = fail
importlib.import_module('core.pit_optimizer_v5.two_round_study.__main__')
assert calls == []
assert 'config.settings' not in sys.modules
assert 'core.pit_optimizer_v5.evaluator' not in sys.modules
assert 'core.pit_optimizer_v5.backtest_engine' not in sys.modules
print('task8-import-tripwire-passed')
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_REPO_ROOT,
        env=_clean_process_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "task8-import-tripwire-passed"


def test_deferred_legacy_aliases_keep_identity_and_call_contracts() -> None:
    """Exercise the import seam without loading provider settings on startup."""

    code = r'''
import importlib
import sys
from datetime import date
from types import ModuleType, SimpleNamespace

import pandas as pd

# Keep the compatibility probe provider-free: every deferred module is a
# sentinel module, so the feature facade never enters settings or data code.
fake_canslim = ModuleType("core.canslim")
fake_canslim.__path__ = []
sys.modules["core.canslim"] = fake_canslim
fake_fiscal = ModuleType("core.canslim.fiscal_periods")
fake_leader = ModuleType("core.canslim.l_leader_laggard")
fake_industry = ModuleType("core.industry_group")
sys.modules[fake_fiscal.__name__] = fake_fiscal
sys.modules[fake_leader.__name__] = fake_leader
sys.modules[fake_industry.__name__] = fake_industry

calls = {"fiscal": [], "finite": [], "group": [], "industry": []}

def match_periods(series):
    calls["fiscal"].append(series)
    return (
        SimpleNamespace(matched=True, current_value=2.0, prior_value=1.0),
        SimpleNamespace(matched=True, current_value=1.5, prior_value=1.0),
    )

def finite_snapshot(snapshot, *, required_symbols):
    calls["finite"].append((snapshot, frozenset(required_symbols)))
    return snapshot

def group_rs(group_id, *, active_symbols, symbol_groups, rs_snapshot):
    calls["group"].append((group_id, frozenset(active_symbols), dict(symbol_groups), dict(rs_snapshot)))
    return 42.0

class Assignment:
    group_id = "G"

def industry_assignments(bundle, *, session, symbols, allow_schema_v2_development=False):
    calls["industry"].append((bundle, session, frozenset(symbols), allow_schema_v2_development))
    return {symbol: Assignment() for symbol in symbols}

fake_fiscal.match_fiscal_year_over_year_periods = match_periods
fake_leader.finite_rs_snapshot = finite_snapshot
fake_leader.calculate_group_rs = group_rs
fake_industry.load_pit_industry_assignments_as_of = industry_assignments

import core.pit_feature_snapshot as features

assert "config.settings" not in sys.modules
assert "core.momentum_analysis" not in sys.modules
aliases = {
    "match_fiscal_year_over_year_periods": (fake_fiscal, match_periods),
    "calculate_group_rs": (fake_leader, group_rs),
    "finite_rs_snapshot": (fake_leader, finite_snapshot),
    "load_pit_industry_assignments_as_of": (fake_industry, industry_assignments),
}

# A pre-installed global is authoritative, including an intentional None.
for name in aliases:
    sentinel = None if name == "finite_rs_snapshot" else object()
    features.__dict__[name] = sentinel
    assert features._resolve_deferred_feature(name) is sentinel
    del features.__dict__[name]

for name, (module, expected) in aliases.items():
    assert getattr(features, name) is expected
    assert getattr(importlib.import_module(module.__name__), name) is expected
assert "config.settings" not in sys.modules

class Bundle:
    metadata = {"schema_version": "3"}
    data_cutoff = pd.Timestamp("2025-12-31")
    membership_v3 = SimpleNamespace(ticker_for_lineage_at=lambda lineage, session: "AAA")

    @staticmethod
    def members_at(session):
        assert session == date(2025, 1, 2)
        return ("AAA", "BBB")

    @staticmethod
    def security_lineage_id(symbol):
        return "lineage-" + symbol

    @staticmethod
    def affiliations_at(session):
        return {"AAA": ()}

    @staticmethod
    def fundamentals_provider(symbol, timestamp, *, include_provenance):
        assert symbol == "AAA" and include_provenance is True
        return {"quarterly_income": pd.DataFrame()}

bundle = Bundle()
rs = {"AAA": 50.0, "BBB": 51.0}
features._validated_context(bundle, "AAA", date(2025, 1, 2), rs, require_active=True)
assert calls["finite"][-1] == (rs, frozenset({"AAA", "BBB"}))

empty_history = pd.DataFrame()
features.build_entry_features_v3(
    bundle=bundle, symbol="AAA", session=date(2025, 1, 2),
    price_history=empty_history, rs_snapshot=rs,
)
features.build_holding_features_v3(
    bundle=bundle, symbol="AAA", session=date(2025, 1, 2),
    price_history=empty_history, rs_snapshot=rs,
)
assert len(calls["industry"]) == 2
assert all(item[1] == date(2025, 1, 2) for item in calls["industry"])
assert all(item[2] == frozenset({"AAA", "BBB"}) for item in calls["industry"])
assert len(calls["group"]) == 2
assert all(item[0] == "G" and item[1] == frozenset({"AAA", "BBB"}) for item in calls["group"])

series = pd.Series([1.0, 2.0], index=["2024-03-31", "2025-03-31"])
assert features._growth_acceleration(series) is not None
quarterly = pd.DataFrame({"2024-03-31": [1.0], "2025-03-31": [2.0]}, index=["Diluted EPS"])
assert features._earnings_acceleration(quarterly) is not None
assert calls["fiscal"][-3] is series
assert isinstance(calls["fiscal"][-1], pd.Series)

# The public dataclasses keep their strict V3 validation boundary.
features.EntryFeaturesV3((), None, None, None, None, None, None, None, None, None)
try:
    features.EntryFeaturesV3(["AAA"], None, None, None, None, None, None, None, None, None)
except ValueError:
    pass
else:
    raise AssertionError("invalid affiliation container was accepted")
try:
    features.HoldingFeaturesV3(True, None, None, None)
except ValueError:
    pass
else:
    raise AssertionError("boolean feature value was accepted")

# The contract's historical helper is an identity-preserving lazy alias.
contract = importlib.import_module("core.pit_optimization_contract")
assert "core.engine_policy" not in sys.modules
fake_engine = ModuleType("core.engine_policy")
sentinel = object()
fake_engine.effective_engine_policy_sha256 = sentinel
sys.modules["core.engine_policy"] = fake_engine
assert contract.effective_engine_policy_sha256 is sentinel
assert contract.effective_engine_policy_sha256 is sentinel
print("task8-deferred-aliases-passed")
'''
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_REPO_ROOT,
        env=_clean_process_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "task8-deferred-aliases-passed"


def test_offline_rejects_existing_root_with_empty_child(owned_root: Path) -> None:
    root = owned_root / "occupied"
    root.mkdir()
    child = root / "empty-child"
    child.mkdir()
    before = tuple(sorted(path.relative_to(root).as_posix() for path in root.rglob("*")))
    completed = _run_cli("offline", "--root", str(root), cwd=_REPO_ROOT, env=_clean_process_env())
    assert completed.returncode == 2
    assert "error" in json.loads(completed.stderr)
    assert child.is_dir()
    assert tuple(sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))) == before


@pytest.fixture
def owned_root() -> Path:
    return _new_test_root("cli-test")


def test_offline_verify_export_round_trip_uses_no_side_effect_tripwires(owned_root: Path, monkeypatch) -> None:
    root = owned_root / "cli-study"
    output = owned_root / "cli-export"
    marker = owned_root / "tripwire-fired.txt"
    install_marker = owned_root / "tripwire-installed.txt"
    probe_marker = owned_root / "tripwire-probe-fired.txt"
    tripwire = _tripwire_dir(owned_root)
    env = _clean_process_env(pythonpath=os.pathsep.join((str(tripwire), str(_REPO_ROOT))))
    env["TASK8_TRIPWIRE_MARKER"] = str(marker)
    env["TASK8_TRIPWIRE_INSTALL_MARKER"] = str(install_marker)

    probe_env = dict(env)
    probe_env["TASK8_TRIPWIRE_MARKER"] = str(probe_marker)
    probe = subprocess.run(
        [sys.executable, "-c", "import socket; socket.create_connection(('127.0.0.1', 1))"],
        cwd=_REPO_ROOT,
        env=probe_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode != 0
    assert install_marker.read_text(encoding="utf-8") == "installed"
    assert probe_marker.read_text(encoding="utf-8").strip() == "socket.create_connection"

    offline = _run_cli("offline", "--root", str(root), cwd=_REPO_ROOT, env=env)
    assert offline.returncode == 0, offline.stderr
    offline_result = json.loads(offline.stdout)
    assert offline_result["command"] == "offline"
    assert offline_result["gateway_calls"] == 2
    assert offline_result["verdicts"]["evidence_use"] == "not_assessed"
    assert offline_result["verdicts"]["optimization_improvement"] == "not_established"
    assert all(item["state"] == "completed" for item in offline_result["arms"])
    assert not marker.exists(), marker.read_text(encoding="utf-8") if marker.exists() else ""

    # The completed graph is available from this single offline campaign.  Bind
    # a semantic review to its exact imports, then exercise the arm-swap
    # regression through the same read-only validator used by verification.
    from core.pit_optimizer_v5.two_round_study.imports import StudyImportV1
    from core.pit_optimizer_v5.two_round_study.store import StudyStoreV1
    from core.pit_optimizer_v5.two_round_study.ledger import StudyLedgerV1
    from core.pit_optimizer_v5.two_round_study.live_calls import StudyGrantV1
    from core.pit_optimizer_v5.two_round_study.compiler import StudyCommitmentIndexV1
    from core.pit_optimizer_v5.two_round_study.contrast import StudyContrastV1
    from core.pit_optimizer_v5.two_round_study.fixtures import reopen_study_fixture_v1
    from core.pit_optimizer_v5.mechanism_artifacts import MechanismArtifactRepositoryV5
    from core.pit_optimizer_v5.two_round_study.registry import registry_decision_v1
    from core.strategy_policy.contracts import ExitDecision
    from core.strategy_policy.contracts_v3 import ExitSnapshotV3
    from decimal import Decimal
    from core.pit_optimizer_v5.two_round_study.verification import (
        HumanEvidenceReviewV1,
        _validate_human_review,
        load_prepared_study_v1,
        verify_study_v1,
    )
    from core.pit_optimizer_v5.two_round_study.contracts import StudyAuthorityError

    completed_prepared = load_prepared_study_v1(root=root)
    completed_store = StudyStoreV1(completed_prepared.store_repository())
    imported_by_arm = {}
    for reference in completed_store.list_refs(kind="imports"):
        imported = StudyImportV1.from_canonical_json(completed_store.read(reference))
        imported_by_arm[imported.arm] = imported
    assert set(imported_by_arm) == {"primary", "withheld"}
    review_kwargs = {
        "reviewer": "task8-arm-binding-reviewer",
        "rubric_sha256": completed_prepared.rubric_ref.sha256,
        "arm_response_refs": {
            arm: imported_by_arm[arm].original_response_ref for arm in ("primary", "withheld")
        },
        "arm_draft_refs": {
            arm: imported_by_arm[arm].draft_refs for arm in ("primary", "withheld")
        },
        "reasons": ("The review cites the exact arm artifacts and gives a semantic reason.",),
        "artifact_refs": tuple(completed_store.list_refs(kind="imports")),
        "axis_reasons": {
            axis: (f"Semantic reason for {axis}.",)
            for axis in ("evidence_interpretation", "revision_quality", "claim_pattern")
        },
        "axis_citations": {
            axis: tuple(completed_store.list_refs(kind="imports"))
            for axis in ("evidence_interpretation", "revision_quality", "claim_pattern")
        },
    }
    # The public verifier must enforce the same exact arm ownership.  A
    # private helper-only check would leave the CLI/export boundary vulnerable
    # to a caller that swaps the two response identities after construction.
    readonly_grant = StudyGrantV1.from_canonical_json(
        completed_store.read(completed_store.list_refs(kind="grants")[0])
    )
    readonly_ledger = StudyLedgerV1(
        completed_store,
        completed_prepared.manifest,
        readonly_grant,
        approval=None,
    )
    base_verification = verify_study_v1(
        prepared=completed_prepared,
        store=completed_store,
        ledger=readonly_ledger,
    )

    # The public boundary must bind both the ledger's store and its grant to
    # the exact store supplied by the caller.  A valid grant copied onto a
    # different repository identity is an authority error, not a pending run.
    foreign_root = _new_test_root("foreign-ledger") / "store"
    foreign_root.mkdir()
    foreign_store = StudyStoreV1(LocalArtifactRepositoryV5(foreign_root))
    with pytest.raises(StudyAuthorityError, match="grant does not bind"):
        StudyLedgerV1(
            foreign_store,
            completed_prepared.manifest,
            readonly_grant,
            approval=None,
        )
    foreign_grant = replace(
        readonly_grant,
        repository_root_identity_sha256=foreign_store.repository.root_identity_sha256,
    )
    foreign_store.put(
        kind="grants",
        key=foreign_grant.study_id,
        content=foreign_grant.canonical_bytes(),
    )
    foreign_store.put(
        kind="manifests",
        key=completed_prepared.manifest.sha256,
        content=completed_prepared.manifest.canonical_bytes(),
    )
    for kind, reference in (
        ("preflights", completed_prepared.manifest.primary_preflight_ref),
        ("preflights", completed_prepared.manifest.withheld_preflight_ref),
        ("rubrics", completed_prepared.rubric_ref),
    ):
        foreign_store.put(
            kind=kind,
            key=reference.relative_path.rsplit("/", 1)[-1][:-4],
            content=completed_store.read(reference),
        )
    foreign_ledger = StudyLedgerV1(
        foreign_store,
        completed_prepared.manifest,
        foreign_grant,
        approval=None,
    )
    with pytest.raises(StudyAuthorityError, match="ledger store differs"):
        verify_study_v1(
            prepared=completed_prepared,
            store=completed_store,
            ledger=foreign_ledger,
        )

    # Hiding the shared execution namespaces must not turn completed
    # descendant fixture history into a legitimate preparation-only study.
    # The verifier must reject the fixture-only round-two authorities before
    # any synthetic zero-usage interpretation.
    execution_kinds = {
        "grants",
        "requests",
        "reservations",
        "dispatches",
        "responses",
        "raw-responses",
        "raw-response-failures",
        "reconciliations",
        "terminals",
        "parsed",
        "admission-rejections",
        "imports",
        "import-translations",
        "import-artifacts",
        "import-drafts",
        "commitments",
        "draft-bindings",
        "mechanism-specs",
        "mechanism-corpora",
        "contrasts",
        "mechanism-links",
    }
    original_list_refs = StudyStoreV1.list_refs

    def hide_execution_namespaces(store, *, kind, maximum_entries=4096):
        if kind in execution_kinds:
            return ()
        return original_list_refs(store, kind=kind, maximum_entries=maximum_entries)

    with monkeypatch.context() as patch:
        patch.setattr(StudyStoreV1, "list_refs", hide_execution_namespaces)
        with pytest.raises(StudyAuthorityError, match="fixture roots contain round-two history"):
            verify_study_v1(prepared=completed_prepared, store=completed_store, ledger=None)

    fixture_report_refs = tuple(
        reference
        for reference in base_verification.artifact_refs
        if reference.relative_path.startswith("adapter-blobs/mechanism-v5/")
        and reference.relative_path.endswith("-report.bin")
    )
    assert fixture_report_refs
    # This is an arm-owned mechanism artifact, not a store import.  It must
    # be authenticated from its fixed fixture root when used as a semantic
    # axis citation.
    fixture_axis_citation = fixture_report_refs[-1]
    review_kwargs["axis_citations"] = {
        axis: tuple(completed_store.list_refs(kind="imports")) + (fixture_axis_citation,)
        for axis in ("evidence_interpretation", "revision_quality", "claim_pattern")
    }
    valid_review = HumanEvidenceReviewV1(**review_kwargs)
    _validate_human_review(
        review=valid_review,
        prepared=completed_prepared,
        store=completed_store,
        verification_refs=base_verification.artifact_refs,
    )
    swapped_kwargs = dict(review_kwargs)
    swapped_kwargs["arm_response_refs"] = {
        "primary": imported_by_arm["withheld"].original_response_ref,
        "withheld": imported_by_arm["primary"].original_response_ref,
    }
    with pytest.raises(StudyAuthorityError, match="response references"):
        _validate_human_review(
            review=HumanEvidenceReviewV1(**swapped_kwargs),
            prepared=completed_prepared,
            store=completed_store,
            verification_refs=base_verification.artifact_refs,
        )
    # Keep the flat sequence correct while swapping only the grouped draft
    # ownership.  The canonical review identity must preserve that partition
    # and reject the same flattened references under the wrong arm.
    partition_swapped_kwargs = dict(review_kwargs)
    partition_swapped_kwargs["draft_refs"] = valid_review.draft_refs
    partition_swapped_kwargs["arm_draft_refs"] = {
        "primary": imported_by_arm["withheld"].draft_refs,
        "withheld": imported_by_arm["primary"].draft_refs,
    }
    with pytest.raises(StudyAuthorityError, match="draft (references|ownership)"):
        _validate_human_review(
            review=HumanEvidenceReviewV1(**partition_swapped_kwargs),
            prepared=completed_prepared,
            store=completed_store,
            verification_refs=base_verification.artifact_refs,
        )
    reviewed = verify_study_v1(
        prepared=completed_prepared,
        store=completed_store,
        ledger=readonly_ledger,
        human_review=valid_review,
    )
    assert reviewed.verdicts.evidence_use == "not_assessed"
    with pytest.raises(StudyAuthorityError, match="response references"):
        verify_study_v1(
            prepared=completed_prepared,
            store=completed_store,
            ledger=readonly_ledger,
            human_review=HumanEvidenceReviewV1(**swapped_kwargs),
        )
    with pytest.raises(StudyAuthorityError, match="draft (references|ownership)"):
        verify_study_v1(
            prepared=completed_prepared,
            store=completed_store,
            ledger=readonly_ledger,
            human_review=HumanEvidenceReviewV1(**partition_swapped_kwargs),
        )

    before = _inventory(root)
    verify = _run_cli("verify", "--root", str(root), cwd=_REPO_ROOT, env=env)
    assert verify.returncode == 0, verify.stderr
    verify_result = json.loads(verify.stdout)
    assert verify_result["verdicts"]["trace_integrity"] == "verified"
    assert verify_result["verdicts"]["evidence_use"] == "not_assessed"
    assert _inventory(root) == before
    assert not marker.exists()

    export = _run_cli("export", "--root", str(root), "--output", str(output), cwd=_REPO_ROOT, env=env)
    assert export.returncode == 0, export.stderr
    assert json.loads(export.stdout)["command"] == "export"
    assert _inventory(root) == before
    assert not marker.exists()
    for relative in (
        "study-manifest.json",
        "artifact-index.json",
        "live-study-calls",
        "imports",
        "behavior-registry",
        "round-1",
        "round-2-primary",
        "round-2-withheld",
        "request-comparison.json",
        "case-contrasts",
        "rubric-results.json",
        "trace.md",
    ):
        assert (output / relative).exists(), relative
    completed_trace = (output / "trace.md").read_text(encoding="utf-8")
    for marker in (
        "offline synthetic zero-cost fixture replay",
        "input token cap",
        "per-call USD cap",
        "verify/export preserves these caps",
    ):
        assert marker in completed_trace, marker
    index = json.loads(_long_read_bytes(output / "artifact-index.json").decode("utf-8"))
    source_roots = {
        "study-store": completed_prepared.store_root,
        "round-one": completed_prepared.ancestor_root,
        "round-two-primary": completed_prepared.primary_root,
        "round-two-withheld": completed_prepared.withheld_root,
    }
    original_entries = [item for item in index["artifacts"] if item["authority"] != "export-derivative"]
    assert original_entries
    assert any(item["upstream_refs"] for item in original_entries)
    assert any(
        item["relative_path"].startswith("imports/") and item["upstream_refs"]
        for item in original_entries
    )
    for authority_prefix in ("round-two-primary:", "round-two-withheld:"):
        arm_entries = [item for item in original_entries if str(item["authority"]).startswith(authority_prefix)]
        assert arm_entries
        assert any(Path(str(item["source_relative_path"])).name == "checkpoint.json" for item in arm_entries)
        assert any(Path(str(item["source_relative_path"])).name == "archive.json" for item in arm_entries)
        assert any("events" in Path(str(item["source_relative_path"])).parts for item in arm_entries)
        mechanism_precommitments = [
            item
            for item in arm_entries
            if str(item["source_relative_path"]).startswith("adapter-state/mechanism-v5/")
            and str(item["source_relative_path"]).endswith("-0002.json")
        ]
        mechanism_specs = [
            item
            for item in arm_entries
            if str(item["source_relative_path"]).startswith("adapter-blobs/mechanism-v5/")
            and str(item["source_relative_path"]).endswith("-0002-spec.bin")
        ]
        mechanism_corpora = [
            item
            for item in arm_entries
            if str(item["source_relative_path"]).startswith("adapter-blobs/mechanism-v5/")
            and str(item["source_relative_path"]).endswith("-0002-corpus.bin")
        ]
        policy_sources = [
            item
            for item in arm_entries
            if str(item["source_relative_path"]).startswith("adapter-state/policy-source/")
        ]
        assert len(mechanism_precommitments) == 1
        assert len(mechanism_specs) == 1
        assert len(mechanism_corpora) == 1
        assert policy_sources
        selected_precommitment = mechanism_precommitments[0]
        selected_authority = str(selected_precommitment["authority"])
        assert selected_authority.startswith(authority_prefix)
        precommitment_upstreams = set(selected_precommitment["upstream_refs"])
        assert precommitment_upstreams
        # The canonical precommitment is the authenticated consuming node.  It
        # embeds the exact root-qualified spec/corpus references; the binary
        # spec/corpus blobs are opaque leaves, so they cannot expose inverse
        # edges through _embedded_reference_pairs and must retain empty
        # upstream_refs rather than receiving invented links.
        for leaf in (mechanism_specs[0], mechanism_corpora[0]):
            leaf_authority = str(leaf["authority"])
            assert leaf_authority.startswith(authority_prefix)
            assert leaf_authority == selected_authority
            expected_edge = (
                f"{selected_authority}|{leaf['source_relative_path']}|{leaf['source_sha256']}"
            )
            assert expected_edge in precommitment_upstreams
            assert leaf["upstream_refs"] == []
    assert all(
        "|" in upstream and upstream.rsplit("|", 1)[-1] == upstream.rsplit("|", 1)[-1].lower()
        for item in index["artifacts"]
        for upstream in item["upstream_refs"]
    )
    for item in original_entries:
        authority = str(item["authority"])
        prefix = authority.split(":", 1)[0]
        source_root = source_roots[prefix]
        source_path = source_root.joinpath(*str(item["source_relative_path"]).split("/"))
        exported_path = output.joinpath(*str(item["relative_path"]).split("/"))
        assert _long_exists(source_path), item["source_relative_path"]
        assert _long_exists(exported_path), item["relative_path"]
        source_bytes = _long_read_bytes(source_path)
        exported_bytes = _long_read_bytes(exported_path)
        assert source_bytes == exported_bytes, item["relative_path"]
        assert hashlib.sha256(source_bytes).hexdigest() == item["source_sha256"]
        assert hashlib.sha256(exported_bytes).hexdigest() == item["sha256"]

    # Each named tamper case is applied at an authenticated read boundary and
    # then verified through the public verifier.  The completed graph is kept
    # intact on disk; these are read-only simulations of a changed persisted
    # byte/index/namespace, so the test never turns an invalid completed graph
    # into a purported pending phase.
    def fixture_relative(root_path: Path, predicate) -> str:
        matches = [relative for relative, _path in _long_files(root_path) if predicate(relative)]
        assert matches, str(root_path)
        return matches[0]

    primary_fixture = reopen_study_fixture_v1(
        root=completed_prepared.primary_root,
        manifest_ref=completed_prepared.primary_manifest_ref,
        registry=completed_prepared.registry,
    )
    primary_checkpoint = primary_fixture.repository.load_checkpoint()
    assert primary_checkpoint is not None
    primary_round_two_records = tuple(
        primary_fixture.repository.load_experiment(reference)
        for reference in primary_checkpoint.record_refs
        if primary_fixture.repository.load_experiment(reference).round_index == 2
    )
    assert len(primary_round_two_records) == 1
    primary_round_two_record = primary_round_two_records[0]
    round_two_experiment_id = primary_round_two_record.experiment_id
    round_two_campaign_id = primary_fixture.manifest.manifest.campaign_id
    round_two_policy_key = primary_round_two_record.policy_revision.sha256

    primary_mechanism_run = fixture_relative(
        completed_prepared.primary_root,
        lambda relative: relative == f"adapter-blobs/mechanism-v5/{round_two_experiment_id}-run.bin",
    )
    primary_mechanism_spec = fixture_relative(
        completed_prepared.primary_root,
        lambda relative: relative == f"adapter-blobs/mechanism-v5/{round_two_campaign_id}-0002-spec.bin",
    )
    primary_mechanism_corpus = fixture_relative(
        completed_prepared.primary_root,
        lambda relative: relative == f"adapter-blobs/mechanism-v5/{round_two_campaign_id}-0002-corpus.bin",
    )
    primary_precommitment = fixture_relative(
        completed_prepared.primary_root,
        lambda relative: relative == f"adapter-state/mechanism-v5/{round_two_campaign_id}-0002.json",
    )
    primary_policy_source = fixture_relative(
        completed_prepared.primary_root,
        lambda relative: relative == f"adapter-state/policy-source/{round_two_policy_key}.json",
    )
    primary_runtime_package = fixture_relative(
        completed_prepared.primary_root,
        lambda relative: relative.startswith("roles/artifacts/author/") and relative.endswith(".json"),
    )
    primary_report = fixture_relative(
        completed_prepared.primary_root,
        lambda relative: relative == f"adapter-blobs/mechanism-v5/{round_two_experiment_id}-report.bin",
    )

    expected_tamper_labels = frozenset(
        {
            "raw observations",
            "round-two report sidecar",
            "spec",
            "corpus",
            "precommitment",
            "modified existing runtime package",
            "wrong checkpoint",
            "wrong P1 source",
            "wrong P1 commitment link",
            "spec commitment link",
            "corpus commitment link",
            "registry/fingerprint vector",
            "registry decision observation",
            "stale spec-valid local report",
            "orphan import",
            "duplicate cost/terminal",
            "swapped arm report/commitment",
            "post-author contrast",
            "source/index/sidecar authority",
        }
    )
    selected_tamper_cases = {
        item.strip()
        for item in os.environ.get("TASK8_TAMPER_CASES", "").split(",")
        if item.strip()
    }
    unknown_tamper_cases = selected_tamper_cases - expected_tamper_labels
    assert not unknown_tamper_cases, f"unknown TASK8_TAMPER_CASES labels: {sorted(unknown_tamper_cases)}"
    executed_tamper_cases: set[str] = set()

    def expect_tamper(label: str, install, match: str) -> None:
        if selected_tamper_cases and label not in selected_tamper_cases:
            return
        assert label in expected_tamper_labels
        executed_tamper_cases.add(label)
        del match  # The target hook and typed authority failure are the assertion; diagnostics remain in the log.
        before_roots = tuple(
            _inventory(root_path)
            for root_path in (
                completed_prepared.ancestor_root,
                completed_prepared.store_root,
                completed_prepared.primary_root,
                completed_prepared.withheld_root,
            )
        )
        failure: str | None = None
        with monkeypatch.context() as patch:
            hit = install(patch)
            try:
                with pytest.raises(StudyAuthorityError) as raised:
                    verify_study_v1(
                        prepared=completed_prepared,
                        store=completed_store,
                        ledger=readonly_ledger,
                    )
            except (Exception, pytest.fail.Exception) as exc:  # collect ordinary pytest failures without swallowing process interrupts
                failure = f"{type(exc).__name__}: {exc}"
            else:
                if not str(raised.value).strip():
                    failure = "StudyAuthorityError had no diagnostic"
        after_roots = tuple(
            _inventory(root_path)
            for root_path in (
                completed_prepared.ancestor_root,
                completed_prepared.store_root,
                completed_prepared.primary_root,
                completed_prepared.withheld_root,
            )
        )
        if hit is not None and not hit["value"]:
            failure = (failure + "; " if failure else "") + "tamper hook did not reach its bound target"
        if after_roots != before_roots:
            failure = (failure + "; " if failure else "") + "input roots changed"
        if failure:
            tamper_failures.append(f"{label}: {failure}")

    tamper_failures: list[str] = []

    original_relative_reader = LocalArtifactRepositoryV5._read_relative

    def install_fixture_byte_tamper(patch, root_path: Path, relative_path: str) -> dict[str, bool]:
        hit = {"value": False}

        def changed(repository, relative):
            raw = original_relative_reader(repository, relative)
            if Path(repository.root).resolve() == root_path.resolve() and relative == relative_path:
                hit["value"] = True
                return raw + b"task8-tamper"
            return raw

        patch.setattr(LocalArtifactRepositoryV5, "_read_relative", changed)
        return hit

    commitment_refs_by_arm = {}
    commitments_by_arm = {}
    for reference in completed_store.list_refs(kind="commitments"):
        value = StudyCommitmentIndexV1.from_canonical_json(completed_store.read(reference))
        commitment_refs_by_arm[value.arm] = reference
        commitments_by_arm[value.arm] = value

    def install_commitment_variant(patch, arm: str, mutate) -> dict[str, bool]:
        original_list = StudyStoreV1.list_refs
        original_read = StudyStoreV1.read
        old_ref = commitment_refs_by_arm[arm]
        changed_value = mutate(commitments_by_arm[arm])
        changed_raw = changed_value.canonical_bytes()
        changed_ref = ArtifactRefV5(old_ref.relative_path, hashlib.sha256(changed_raw).hexdigest())
        hit = {"value": False}

        def listed(store, *, kind, maximum_entries=4096):
            refs = original_list(store, kind=kind, maximum_entries=maximum_entries)
            if kind == "commitments":
                return tuple(changed_ref if item == old_ref else item for item in refs)
            return refs

        def read(store, reference):
            if reference == changed_ref:
                hit["value"] = True
                return changed_raw
            return original_read(store, reference)

        patch.setattr(StudyStoreV1, "list_refs", listed)
        patch.setattr(StudyStoreV1, "read", read)
        return hit

    def install_registry_relational_tamper(patch) -> dict[str, bool]:
        from core.pit_optimizer_v5.probes import PROBE_SUITE_ID_V5, _fingerprint_sha256

        original_list = StudyStoreV1.list_refs
        original_read = StudyStoreV1.read
        registry = completed_prepared.registry
        first = registry.configurations[0]
        second = registry.configurations[1]
        changed_output = replace(
            first.fixed_suite_outputs[9],
            decision_json=second.fixed_suite_outputs[9].decision_json,
        )
        changed_outputs = (*first.fixed_suite_outputs[:9], changed_output, *first.fixed_suite_outputs[10:])
        changed_first = replace(
            first,
            fixed_suite_outputs=changed_outputs,
            fixed_suite_fingerprint=_fingerprint_sha256(PROBE_SUITE_ID_V5, changed_outputs),
        )
        changed_registry = replace(registry, configurations=(changed_first, *registry.configurations[1:]))
        changed_raw = changed_registry.canonical_bytes()
        old_ref = completed_prepared.registry_ref
        changed_ref = ArtifactRefV5(old_ref.relative_path, hashlib.sha256(changed_raw).hexdigest())
        assert changed_ref != old_ref
        hit = {"value": False}

        def listed(store, *, kind, maximum_entries=4096):
            refs = original_list(store, kind=kind, maximum_entries=maximum_entries)
            if kind == "registry":
                old_positions = [index for index, item in enumerate(refs) if item == old_ref]
                replaced = tuple(changed_ref if item == old_ref else item for item in refs)
                if (
                    len(old_positions) == 1
                    and replaced[old_positions[0]] == changed_ref
                    and changed_ref != old_ref
                ):
                    hit["value"] = True
                return replaced
            return refs

        def read(store, reference):
            if reference == changed_ref:
                return changed_raw
            return original_read(store, reference)

        patch.setattr(StudyStoreV1, "list_refs", listed)
        patch.setattr(StudyStoreV1, "read", read)
        return hit

    expect_tamper(
        "raw observations",
        lambda patch: install_fixture_byte_tamper(patch, completed_prepared.primary_root, primary_mechanism_run),
        "mechanism|bytes|authority|digest|differ",
    )
    expect_tamper(
        "round-two report sidecar",
        lambda patch: install_fixture_byte_tamper(patch, completed_prepared.primary_root, primary_report),
        "mechanism|bytes|authority|digest|differ|report",
    )
    expect_tamper(
        "spec",
        lambda patch: install_fixture_byte_tamper(patch, completed_prepared.primary_root, primary_mechanism_spec),
        "mechanism|bytes|authority|digest|differ|precommitment",
    )
    expect_tamper(
        "corpus",
        lambda patch: install_fixture_byte_tamper(patch, completed_prepared.primary_root, primary_mechanism_corpus),
        "mechanism|bytes|authority|digest|differ|precommitment",
    )
    expect_tamper(
        "precommitment",
        lambda patch: install_fixture_byte_tamper(patch, completed_prepared.primary_root, primary_precommitment),
        "mechanism|bytes|authority|digest|differ|precommitment",
    )
    expect_tamper(
        "modified existing runtime package",
        lambda patch: install_fixture_byte_tamper(patch, completed_prepared.primary_root, primary_runtime_package),
        "role|package|authority|digest|differ|authenticated",
    )
    expect_tamper(
        "wrong checkpoint",
        lambda patch: install_fixture_byte_tamper(patch, completed_prepared.primary_root, "checkpoint.json"),
        "checkpoint|projection|authority|digest|differ|authenticated",
    )
    expect_tamper(
        "wrong P1 source",
        lambda patch: install_fixture_byte_tamper(patch, completed_prepared.primary_root, primary_policy_source),
        "policy|source|authority|digest|differ|authenticated",
    )
    expect_tamper(
        "wrong P1 commitment link",
        lambda patch: install_commitment_variant(
            patch,
            "primary",
            lambda value: replace(value, parent_revision_sha256="0" * 64),
        ),
        "P1|parent|commitment|graph|authority|differ",
    )
    expect_tamper(
        "spec commitment link",
        lambda patch: install_commitment_variant(
            patch,
            "primary",
            lambda value: replace(value, spec_ref=commitments_by_arm["withheld"].spec_ref),
        ),
        "spec|commitment|graph|authority|differ|precommitment",
    )
    primary_mechanism_corpus_ref = next(
        reference
        for reference in base_verification.artifact_refs
        if reference.relative_path == primary_mechanism_corpus
    )
    expect_tamper(
        "corpus commitment link",
        lambda patch: install_commitment_variant(
            patch,
            "primary",
            # Both arms intentionally share the same decoded corpus.  Use the
            # exact primary mechanism sidecar identity instead of a no-op
            # cross-arm substitution; the study-store commitment must bind to
            # the authenticated mechanism corpus digest, not merely a valid
            # ArtifactRef-shaped value.
            lambda value: replace(
                value,
                corpus_ref=ArtifactRefV5(value.corpus_ref.relative_path, primary_mechanism_corpus_ref.sha256),
            ),
        ),
        "corpus|commitment|graph|authority|differ|precommitment",
    )

    expect_tamper(
        "registry/fingerprint vector",
        install_registry_relational_tamper,
        "registry|hash|reference|differ|canonical|manifest",
    )

    original_evidence_loader = MechanismArtifactRepositoryV5.load_existing_evidence_for_record

    def _patch_primary_round_two_evidence(patch, transform) -> dict[str, bool]:
        hit = {"value": False}

        def changed(repository, **kwargs):
            result = original_evidence_loader(repository, **kwargs)
            if (
                result is not None
                and kwargs.get("round_index") == 2
                and Path(repository.repository.root).resolve() == completed_prepared.primary_root.resolve()
                and result[0].index.experiment_id == round_two_experiment_id
            ):
                hit["value"] = True
                return transform(result[0]), result[1]
            return result

        patch.setattr(MechanismArtifactRepositoryV5, "load_existing_evidence_for_record", changed)
        return hit

    def install_registry_observation_tamper(patch) -> dict[str, bool]:
        def transform(evidence):
            observations = evidence.run.observations
            target = observations[0]
            snapshot = ExitSnapshotV3.from_canonical_json(target.case_snapshot_json.decode("utf-8"))
            alternate_json = None
            for configuration in completed_prepared.registry.configurations:
                candidate = registry_decision_v1(
                    registry=completed_prepared.registry,
                    configuration_id=configuration.configuration_id,
                    method=evidence.spec.target_method,
                    snapshot=snapshot,
                )
                candidate_json = candidate.to_canonical_json().encode("utf-8")
                if candidate_json != target.candidate_decision_json:
                    alternate_json = candidate_json
                    break
            assert alternate_json is not None, "fixture lacks a distinct registered decision for the tamper"
            alternate_decision = ExitDecision.from_canonical_json(alternate_json.decode("utf-8"))
            parent_decision = ExitDecision.from_canonical_json(target.parent_decision_json.decode("utf-8"))
            changed = []
            for observation in observations:
                if observation.case_order != target.case_order:
                    changed.append(observation)
                    continue
                changed.append(
                    replace(
                        observation,
                        candidate_decision_json=alternate_json,
                        candidate_decision_sha256=hashlib.sha256(alternate_json).hexdigest(),
                        decision_changed=observation.parent_decision_json != alternate_json,
                        protected_control_unchanged=(
                            parent_decision.next_stop_price == alternate_decision.next_stop_price
                        ),
                    )
                )
            return replace(evidence, run=replace(evidence.run, observations=tuple(changed)))

        return _patch_primary_round_two_evidence(patch, transform)

    expect_tamper(
        "registry decision observation",
        install_registry_observation_tamper,
        "registry|decision|observation|frozen",
    )

    def install_stale_report_reduction(patch) -> dict[str, bool]:
        def transform(evidence):
            prediction = next(
                item for item in evidence.report.predictions if item.availability == "measured"
            )
            stale_assessment = (
                "supported_on_cases"
                if prediction.direction == "unchanged"
                else "contradicted_on_cases"
            )
            stale_prediction = replace(
                prediction,
                parent_value=Decimal(0),
                candidate_value=Decimal(0),
                numerator=Decimal(0),
                paired_delta=Decimal(0),
                assessment=stale_assessment,
            )
            stale_report = replace(
                evidence.report,
                predictions=tuple(
                    stale_prediction if item is prediction else item
                    for item in evidence.report.predictions
                ),
            )
            return replace(evidence, report=stale_report)

        return _patch_primary_round_two_evidence(patch, transform)

    expect_tamper(
        "stale spec-valid local report",
        install_stale_report_reduction,
        "report|recomputed|reduction|registry",
    )

    def install_orphan_import(patch) -> dict[str, bool]:
        original_list = StudyStoreV1.list_refs
        primary_import = next(
            reference
            for reference in original_list(completed_store, kind="imports")
            if StudyImportV1.from_canonical_json(completed_store.read(reference)).arm == "primary"
        )
        hit = {"value": False}

        def changed(store, *, kind, maximum_entries=4096):
            refs = original_list(store, kind=kind, maximum_entries=maximum_entries)
            if kind == "imports":
                hit["value"] = True
                return refs + (primary_import,)
            return refs

        patch.setattr(StudyStoreV1, "list_refs", changed)
        return hit

    expect_tamper("orphan import", install_orphan_import, "study import|canonical|schema|arm")

    def install_duplicate_terminal_cost(patch) -> dict[str, bool]:
        original_list = StudyStoreV1.list_refs
        hit = {"value": False}

        def changed(store, *, kind, maximum_entries=4096):
            refs = original_list(store, kind=kind, maximum_entries=maximum_entries)
            if kind == "terminals" and refs:
                hit["value"] = True
                return refs + (refs[0],)
            return refs

        patch.setattr(StudyStoreV1, "list_refs", changed)
        return hit

    expect_tamper("duplicate cost/terminal", install_duplicate_terminal_cost, "duplicate terminal|duplicate cost|ambiguous")

    expect_tamper(
        "swapped arm report/commitment",
        lambda patch: install_commitment_variant(
            patch,
            "primary",
            lambda value: replace(value, contrast_ref=commitments_by_arm["withheld"].contrast_ref),
        ),
        "arm|contrast|commitment|graph|authority|differ|mechanism",
    )

    def install_post_author_contrast(patch) -> dict[str, bool]:
        original_list = StudyStoreV1.list_refs
        original_read = StudyStoreV1.read
        primary_commitment_ref = commitment_refs_by_arm["primary"]
        primary_commitment = commitments_by_arm["primary"]
        original_contrast_ref = primary_commitment.contrast_ref
        original_contrast = StudyContrastV1.from_canonical_json(
            original_read(completed_store, original_contrast_ref)
        )
        changed_contrast = replace(original_contrast, parent_revision_sha256="0" * 64)
        changed_contrast_raw = changed_contrast.canonical_bytes()
        changed_contrast_ref = ArtifactRefV5(
            f"adapter-blobs/study-v1-contrasts/{hashlib.sha256(changed_contrast_raw).hexdigest()}.bin",
            hashlib.sha256(changed_contrast_raw).hexdigest(),
        )
        changed_commitment = replace(primary_commitment, contrast_ref=changed_contrast_ref)
        changed_commitment_raw = changed_commitment.canonical_bytes()
        changed_commitment_ref = ArtifactRefV5(
            primary_commitment_ref.relative_path,
            hashlib.sha256(changed_commitment_raw).hexdigest(),
        )
        hit = {"value": False}

        def listed(store, *, kind, maximum_entries=4096):
            refs = original_list(store, kind=kind, maximum_entries=maximum_entries)
            if kind == "commitments":
                return tuple(changed_commitment_ref if item == primary_commitment_ref else item for item in refs)
            if kind == "contrasts":
                return tuple(changed_contrast_ref if item == original_contrast_ref else item for item in refs)
            return refs

        def changed(store, reference):
            if reference == changed_commitment_ref:
                hit["value"] = True
                return changed_commitment_raw
            if reference == changed_contrast_ref:
                hit["value"] = True
                return changed_contrast_raw
            return original_read(store, reference)

        patch.setattr(StudyStoreV1, "list_refs", listed)
        patch.setattr(StudyStoreV1, "read", changed)
        return hit

    expect_tamper("post-author contrast", install_post_author_contrast, "contrast|parent|commitment|graph|authority|differ")

    def install_sidecar_index_tamper(patch) -> dict[str, bool]:
        original_loader = LocalArtifactRepositoryV5._load_adapter_state_authority
        hit = {"value": False}

        def missing(repository, *, namespace, key):
            if namespace == "policy-source" and key == primary_policy_source.rsplit("/", 1)[-1][:-5]:
                hit["value"] = True
                return None
            return original_loader(repository, namespace=namespace, key=key)

        patch.setattr(LocalArtifactRepositoryV5, "_load_adapter_state_authority", missing)
        return hit

    expect_tamper("source/index/sidecar authority", install_sidecar_index_tamper, "policy source|authority|index|missing|unavailable")
    assert executed_tamper_cases == (selected_tamper_cases or expected_tamper_labels)
    assert not tamper_failures, "\n".join(tamper_failures)
