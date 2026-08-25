"""Read-only verification of the corrected PIT CANSLIM baseline authority.

This module deliberately verifies publication artifacts; it neither runs a strategy nor
imports the runner, provider, broker, scheduler, or agent-controller surfaces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from numbers import Real
import os
from pathlib import Path
import stat
from types import MappingProxyType
from typing import Mapping

import pandas as pd


_DATE_CONTRACT = MappingProxyType(
    {
        "warmup_start": "2020-01-01",
        "evaluation_start": "2021-01-01",
        "data_cutoff": "2025-12-31",
    }
)
_REQUIRED_ARTIFACTS = frozenset(
    {
        "canslim_signals.csv",
        "entry_attempt_outcomes.csv",
        "daily_entry_funnel.csv",
        "transactions.csv",
        "equity_curve.csv",
        "leader_recall.csv",
        "summary.json",
    }
)
_CSV_ROUNDTRIP_ARTIFACTS = (
    "entry_attempt_outcomes.csv",
    "transactions.csv",
    "equity_curve.csv",
    "leader_recall.csv",
)
_JSON_EXACT_ARTIFACTS = ("summary.json",)
_REJECTION_COUNTS = (
    "entry_rejected_capacity",
    "entry_rejected_invalid_price",
    "entry_rejected_invalid_risk",
    "entry_rejected_missing_data",
    "entry_rejected_no_cash",
)
_OUTCOME_COLUMNS = (
    "symbol",
    "signal_date",
    "entry_date",
    "pivot",
    "buy_zone_lower",
    "buy_zone_upper",
    "entry_open",
    "outcome",
)
_FUNNEL_COLUMNS = (
    "signal_date",
    "evaluated_count",
    "qualified_count",
    "attempted_count",
    "executed_count",
    "rejected_count",
)


def _freeze_mapping(value: Mapping[str, str]) -> Mapping[str, str]:
    return MappingProxyType(dict(value))


@dataclass(frozen=True)
class BaselineAuthority:
    """The fixed corrected replay facts, never strategy observations."""

    source_commit: str
    replay_git_head: str
    bundle_sha256: str
    manifest_sha256: str
    artifact_sha256: Mapping[str, str]
    entry_outcome_row_sha256: str
    transaction_row_sha256: str
    total_return_pct: float
    annualized_return_pct: float
    sharpe_ratio: float
    max_drawdown_pct: float
    closed_trades: int
    win_rate_pct: float
    average_cash_pct: float
    qualified_entries: int
    executed_entries: int
    next_open_buy_zone_rejections: int
    cash_rejections: int
    date_contract: Mapping[str, str] = field(default_factory=lambda: _DATE_CONTRACT)

    def __post_init__(self) -> None:
        for name in ("source_commit", "replay_git_head"):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) != 40 or any(
                character not in "0123456789abcdef" for character in value
            ):
                raise ValueError(f"{name} must be a lowercase Git SHA-1")
        for name in (
            "bundle_sha256",
            "manifest_sha256",
            "entry_outcome_row_sha256",
            "transaction_row_sha256",
        ):
            _require_digest(getattr(self, name), name)
        if not isinstance(self.artifact_sha256, Mapping) or not self.artifact_sha256:
            raise ValueError("artifact_sha256 must be a non-empty mapping")
        artifacts = dict(self.artifact_sha256)
        if not _REQUIRED_ARTIFACTS.issubset(artifacts):
            raise ValueError("artifact_sha256 omits a required baseline artifact")
        for name, digest in artifacts.items():
            if not isinstance(name, str) or Path(name).name != name:
                raise ValueError("artifact_sha256 keys must be plain filenames")
            _require_digest(digest, f"artifact SHA-256 {name}")
        object.__setattr__(self, "artifact_sha256", _freeze_mapping(artifacts))
        if dict(self.date_contract) != dict(_DATE_CONTRACT):
            raise ValueError("date_contract must be the fixed 2020/2021-2025 baseline")
        object.__setattr__(self, "date_contract", _DATE_CONTRACT)
        for name in (
            "total_return_pct",
            "annualized_return_pct",
            "sharpe_ratio",
            "max_drawdown_pct",
            "win_rate_pct",
            "average_cash_pct",
        ):
            _finite(getattr(self, name), name)
        for name in (
            "closed_trades",
            "qualified_entries",
            "executed_entries",
            "next_open_buy_zone_rejections",
            "cash_rejections",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True)
class BaselineSnapshot:
    """A verified, row-free representation of one baseline publication."""

    run_dir: Path
    manifest_sha256: str
    source_commit: str
    replay_git_head: str
    bundle_sha256: str
    artifact_sha256: Mapping[str, str]
    signal_row_sha256: str
    entry_outcome_row_sha256: str
    transaction_row_sha256: str
    total_return_pct: float
    annualized_return_pct: float
    sharpe_ratio: float
    max_drawdown_pct: float
    closed_trades: int
    win_rate_pct: float
    average_cash_pct: float
    qualified_entries: int
    executed_entries: int
    next_open_buy_zone_rejections: int
    cash_rejections: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_dir", Path(self.run_dir))
        if not isinstance(self.source_commit, str) or len(self.source_commit) != 40 or any(
            character not in "0123456789abcdef" for character in self.source_commit
        ):
            raise ValueError("source_commit must be a lowercase Git SHA-1")
        if not isinstance(self.replay_git_head, str) or len(self.replay_git_head) != 40 or any(
            character not in "0123456789abcdef" for character in self.replay_git_head
        ):
            raise ValueError("replay_git_head must be a lowercase Git SHA-1")
        for name in (
            "manifest_sha256",
            "bundle_sha256",
            "signal_row_sha256",
            "entry_outcome_row_sha256",
            "transaction_row_sha256",
        ):
            _require_digest(getattr(self, name), name)
        if not isinstance(self.artifact_sha256, Mapping):
            raise ValueError("artifact_sha256 must be a mapping")
        for name, digest in self.artifact_sha256.items():
            if not isinstance(name, str) or Path(name).name != name:
                raise ValueError("artifact_sha256 keys must be plain filenames")
            _require_digest(digest, f"artifact SHA-256 {name}")
        object.__setattr__(self, "artifact_sha256", _freeze_mapping(self.artifact_sha256))
        for name in (
            "total_return_pct",
            "annualized_return_pct",
            "sharpe_ratio",
            "max_drawdown_pct",
            "win_rate_pct",
            "average_cash_pct",
        ):
            _finite(getattr(self, name), name)
        for name in (
            "closed_trades",
            "qualified_entries",
            "executed_entries",
            "next_open_buy_zone_rejections",
            "cash_rejections",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True)
class BaselineReproduction:
    """A compact comparison result; raw replay rows never leave the verifier."""

    passed: bool
    mismatch_codes: tuple[str, ...]
    authority_manifest_sha256: str
    reproduced_manifest_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.passed, bool):
            raise ValueError("passed must be bool")
        if not isinstance(self.mismatch_codes, tuple) or any(
            not isinstance(code, str) or not code for code in self.mismatch_codes
        ):
            raise ValueError("mismatch_codes must be a tuple of non-empty strings")
        for name in ("authority_manifest_sha256", "reproduced_manifest_sha256"):
            _require_digest(getattr(self, name), name)


def canonical_authority() -> BaselineAuthority:
    """Return the immutable corrected replay authority described by the design."""

    return BaselineAuthority(
        source_commit="eb93437c185152bb98974303a8c485d08b3ba779",
        replay_git_head="d555f7f4c7727d9c6a440bba50cced0fbe9f3095",
        bundle_sha256="1af306ef1e46797473cd186fc48938ed6694ae25f5943c9f1905b528307cc2eb",
        manifest_sha256="efeb8c3e9b0189131f04361122395f8ea89adfd0f8bcbb781e333a9359d43bd3",
        artifact_sha256={
            "canslim_signals.csv": "168e438cf7999e04ae30a6c1ff3f5f1bb0886572ed04ed6f6bfe70501acf24ff",
            "coverage.json": "d535225da6177352bb9f7b80aff285e4b35393c19519fbb4d131af8fa76e50fa",
            "daily_entry_funnel.csv": "fb6fdd18514a5655cc496fc577380abc636434f5d009b46f99a53cdfc8b36605",
            "entry_attempt_outcomes.csv": "8ceb2a5edb4555bb16f437404a0e9cbe5a8023426a1864cf7c291ba56586831e",
            "equity_curve.csv": "e02f9fd987958939da10c1420a05e211c18d29c20d73692a607b8976e4262598",
            "five_year_leaders.csv": "534dc047dff8379bda774d1e718b713c326be3553d48442ef80ad835b7e38f96",
            "leader_basket_equity.csv": "60125c7359af558b5299993e64c732c49c84fc96c4ba0ed58f3a89fde31fa326",
            "leader_basket_holdings.csv": "8a17eb1f2bef964bf16980beb3539c613c78daf4e8e0263a4e106917761ffce2",
            "leader_basket_transactions.csv": "e9599f457bbdc05dec16646db4b053d08a2d64373d5f5b664efd28a20ff64464",
            "leader_recall.csv": "39c8ebdc0b2776c423feeaf8c9bb61294017afcd5e9df855d65b2bce06dd3603",
            "portfolio_checkpoint.json": "71080a7c965a88fee48511e030b075979ea679d776bd3fd462ecc9a4a25878a8",
            "portfolio_progress.jsonl": "e9398cac0b34ac9089d59e484d5a7f5261610baac3a8bd866a8ec27fea711053",
            "portfolio_state.jsonl": "a4adf20fa91bc9e525c3b742bf83b543774790bedc0bf3e426f22096c217eae7",
            "report.md": "cfa5c77b4937c9c96059477e10cd4c1f30501f005efc2d1a959f90749d9d42df",
            "rolling_leader_labels.csv": "c4463e9d3a5c37e4a7d7db6ebb514b36f0c1fbff0fef3c9f394e1a99bec674df",
            "summary.json": "299ea91f0d54918efcfe03422c2bef8b50af997e7bcd3b7dd355393307d488cf",
            "transactions.csv": "b80d420864149c128d16cf2f6d19e77944618d900928c8135e980852091eda80",
            "weekly_holdings.csv": "e76e450a959029c5e300cc9848960ffa3e13bac2e8f6d382fea10168a8508cf7",
        },
        entry_outcome_row_sha256="8b479ef13e693a2fc101dc3c8b1bdb0204e71122c6701fc5a7e23cd67cf3f3aa",
        transaction_row_sha256="603ccc01141cf55447412d1caa40e9942c5f59745c73183644be8d9b65ab72c5",
        total_return_pct=-9.994717769465932,
        annualized_return_pct=-2.0874097904821753,
        sharpe_ratio=-0.2082076838233648,
        max_drawdown_pct=-13.664400600134604,
        closed_trades=225,
        win_rate_pct=39.111111111111114,
        average_cash_pct=67.31359377429541,
        qualified_entries=286,
        executed_entries=225,
        next_open_buy_zone_rejections=51,
        cash_rejections=10,
    )


def verify_baseline_run(run_dir: Path, authority: BaselineAuthority) -> BaselineSnapshot:
    """Fail closed unless *run_dir* is exactly the hash-bound corrected baseline."""

    if not isinstance(authority, BaselineAuthority):
        raise ValueError("authority must be a BaselineAuthority")
    directory = _regular_directory(run_dir, "run directory")
    manifest_path = _regular_file(directory / "run_manifest.json", "run manifest")
    manifest = _load_json(manifest_path, "run manifest")
    if manifest.get("schema_version") != 1 or manifest.get("status") != "complete":
        raise ValueError("run manifest is not a completed schema-v1 publication")
    if manifest.get("git_head") != authority.replay_git_head:
        raise ValueError("source commit differs from corrected replay authority")
    if manifest.get("bundle_sha256") != authority.bundle_sha256:
        raise ValueError("bundle SHA-256 differs from corrected replay authority")
    if manifest.get("date_contract") != dict(authority.date_contract):
        raise ValueError("run date contract differs from corrected baseline")
    if manifest.get("entry_attempt_outcome_schema_version") != 1 or (
        manifest.get("entry_attempt_outcome_count") != authority.qualified_entries
    ):
        raise ValueError("manifest entry-outcome schema/count differs from corrected baseline")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping) or set(artifacts) != set(authority.artifact_sha256):
        raise ValueError("manifest artifact identity set differs from corrected baseline")
    for name, expected in authority.artifact_sha256.items():
        declared = artifacts.get(name)
        if declared != expected:
            raise ValueError(f"artifact hash identity differs for {name}")
        path = _regular_file(directory / name, f"artifact {name}")
        if _sha256_file(path) != expected:
            raise ValueError(f"artifact hash mismatch for {name}")

    # The signal ledger and resume journals are intentionally hash-only: the corrected
    # replay contains 343 MB and 710 MB of them respectively.
    summary = _load_json(directory / "summary.json", "summary")
    _reject_nonfinite(summary, "summary")
    outcomes = _read_csv(directory / "entry_attempt_outcomes.csv")
    transactions = _read_csv(directory / "transactions.csv")
    funnel = _read_csv(directory / "daily_entry_funnel.csv")
    equity = _read_csv(directory / "equity_curve.csv")
    recall = _read_csv(directory / "leader_recall.csv")
    _reject_nonfinite_frame(outcomes, "entry outcomes")
    _reject_nonfinite_frame(transactions, "transactions")
    _reject_nonfinite_frame(funnel, "daily funnel")
    _reject_nonfinite_frame(equity, "equity curve")
    _reject_nonfinite_frame(recall, "leader recall")
    entry_outcome_row_sha256 = _normalized_ordered_row_sha256(outcomes)
    transaction_row_sha256 = _normalized_ordered_row_sha256(transactions)
    if entry_outcome_row_sha256 != authority.entry_outcome_row_sha256:
        raise ValueError("entry-outcome row hash differs from corrected authority")
    if transaction_row_sha256 != authority.transaction_row_sha256:
        raise ValueError("transaction row hash differs from corrected authority")
    _verify_summary(summary, authority)
    _verify_ledgers(outcomes, transactions, funnel, authority)
    _verify_equity(equity)
    return BaselineSnapshot(
        run_dir=directory,
        manifest_sha256=_sha256_file(manifest_path),
        source_commit=authority.source_commit,
        replay_git_head=authority.replay_git_head,
        bundle_sha256=authority.bundle_sha256,
        artifact_sha256={str(key): str(value) for key, value in artifacts.items()},
        signal_row_sha256=authority.artifact_sha256["canslim_signals.csv"],
        entry_outcome_row_sha256=entry_outcome_row_sha256,
        transaction_row_sha256=transaction_row_sha256,
        total_return_pct=authority.total_return_pct,
        annualized_return_pct=authority.annualized_return_pct,
        sharpe_ratio=authority.sharpe_ratio,
        max_drawdown_pct=authority.max_drawdown_pct,
        closed_trades=authority.closed_trades,
        win_rate_pct=authority.win_rate_pct,
        average_cash_pct=authority.average_cash_pct,
        qualified_entries=authority.qualified_entries,
        executed_entries=authority.executed_entries,
        next_open_buy_zone_rejections=authority.next_open_buy_zone_rejections,
        cash_rejections=authority.cash_rejections,
    )


def compare_reproduction(
    authority: BaselineSnapshot, reproduced: BaselineSnapshot
) -> BaselineReproduction:
    """Compare a fresh verified replay without returning any CSV or journal rows."""

    if not isinstance(authority, BaselineSnapshot) or not isinstance(reproduced, BaselineSnapshot):
        raise ValueError("authority and reproduced must be BaselineSnapshot values")
    mismatches: list[str] = []
    for name in ("source_commit", "replay_git_head", "bundle_sha256", "manifest_sha256"):
        if getattr(authority, name) != getattr(reproduced, name):
            mismatches.append(f"identity.{name}")
    for name in ("entry_outcome_row_sha256", "transaction_row_sha256"):
        if getattr(authority, name) != getattr(reproduced, name):
            mismatches.append(f"rows.{name}")
    for name in (
        "total_return_pct", "annualized_return_pct", "sharpe_ratio", "max_drawdown_pct",
        "closed_trades", "win_rate_pct", "average_cash_pct", "qualified_entries",
        "executed_entries", "next_open_buy_zone_rejections", "cash_rejections",
    ):
        if getattr(authority, name) != getattr(reproduced, name):
            mismatches.append(f"summary.{name}")
    strict_artifacts = set(authority.artifact_sha256).difference(
        _CSV_ROUNDTRIP_ARTIFACTS + _JSON_EXACT_ARTIFACTS
    )
    for name in sorted(strict_artifacts):
        if authority.artifact_sha256.get(name) != reproduced.artifact_sha256.get(name):
            mismatches.append(f"artifact.{name}")
    for name in _CSV_ROUNDTRIP_ARTIFACTS:
        if not _csv_frames_equal(authority.run_dir / name, reproduced.run_dir / name):
            mismatches.append(f"csv.{name}")
    for name in _JSON_EXACT_ARTIFACTS:
        if not _json_values_equal(authority.run_dir / name, reproduced.run_dir / name):
            mismatches.append(f"json.{name}")
    return BaselineReproduction(
        passed=not mismatches,
        mismatch_codes=tuple(mismatches),
        authority_manifest_sha256=authority.manifest_sha256,
        reproduced_manifest_sha256=reproduced.manifest_sha256,
    )


def _require_digest(value: object, field: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{field} must be a finite number")
    return float(value)


def _regular_directory(path: Path, field: str) -> Path:
    value = Path(path)
    if not value.is_dir() or value.is_symlink() or _is_reparse_point(value):
        raise ValueError(f"{field} must be a regular non-link directory")
    return value.resolve()


def _regular_file(path: Path, field: str) -> Path:
    value = Path(path)
    if not value.is_file() or value.is_symlink() or _is_reparse_point(value):
        raise ValueError(f"{field} must be a regular non-link file")
    return value


def _is_reparse_point(path: Path) -> bool:
    metadata = os.lstat(path)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(flag and int(getattr(metadata, "st_file_attributes", 0) or 0) & flag)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _load_json(path: Path, label: str) -> object:
    with _regular_file(path, label).open("r", encoding="utf-8") as handle:
        try:
            return json.load(handle, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite JSON")))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"{label} is not valid finite JSON") from exc


def _read_csv(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(
            _regular_file(path, f"artifact {path.name}"), keep_default_na=False
        )
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        raise ValueError(f"artifact {path.name} is not a valid CSV") from exc


def _reject_nonfinite(value: object, label: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_nonfinite(item, f"{label}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_nonfinite(item, f"{label}[{index}]")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{label} contains a non-finite number")


def _reject_nonfinite_frame(frame: pd.DataFrame, label: str) -> None:
    for column in frame.columns:
        text = frame[column].astype(str).str.strip().str.lower()
        if text.isin({"nan", "+nan", "-nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}).any():
            raise ValueError(f"{label} contains a non-finite number")
        values = pd.to_numeric(frame[column], errors="coerce")
        numeric = values.notna()
        if numeric.any() and not values.loc[numeric].map(math.isfinite).all():
            raise ValueError(f"{label} contains a non-finite number")


def _verify_summary(summary: object, authority: BaselineAuthority) -> None:
    if not isinstance(summary, Mapping):
        raise ValueError("summary is not an object")
    canslim = summary.get("canslim")
    entry_contract = summary.get("entry_contract")
    if not isinstance(canslim, Mapping) or not isinstance(entry_contract, Mapping):
        raise ValueError("summary omits CANSLIM or entry-contract metrics")
    expected_metrics = {
        "total_return_pct": authority.total_return_pct,
        "annualized_return_pct": authority.annualized_return_pct,
        "sharpe_ratio": authority.sharpe_ratio,
        "max_drawdown_pct": authority.max_drawdown_pct,
        "closed_trades": authority.closed_trades,
        "win_rate_pct": authority.win_rate_pct,
        "average_cash_pct": authority.average_cash_pct,
    }
    for name, expected in expected_metrics.items():
        if canslim.get(name) != expected:
            raise ValueError(f"summary metric differs from corrected authority: {name}")
    for name, expected in {
        "qualified_signals": authority.qualified_entries,
        "executed_attempts": authority.executed_entries,
        "next_open_buy_zone_rejections": authority.next_open_buy_zone_rejections,
    }.items():
        if entry_contract.get(name) != expected:
            raise ValueError(f"summary entry metric differs from corrected authority: {name}")
    counts = entry_contract.get("rejection_counts")
    if not isinstance(counts, Mapping) or counts.get("entry_rejected_no_cash") != authority.cash_rejections:
        raise ValueError("summary cash rejection count differs from corrected authority")
    if any(counts.get(name) != 0 for name in _REJECTION_COUNTS[:-1]):
        raise ValueError("summary contains an impossible capacity/invalid/missing rejection")


def _verify_ledgers(
    outcomes: pd.DataFrame,
    transactions: pd.DataFrame,
    funnel: pd.DataFrame,
    authority: BaselineAuthority,
) -> None:
    if tuple(outcomes.columns) != _OUTCOME_COLUMNS:
        raise ValueError("entry outcomes schema differs from corrected baseline")
    if tuple(funnel.columns) != _FUNNEL_COLUMNS:
        raise ValueError("daily funnel schema differs from corrected baseline")
    required_transactions = {"Ticker", "Date", "Action", "Price", "Quantity", "Reason"}
    if not required_transactions.issubset(transactions):
        raise ValueError("transactions lack reconciliation columns")
    if outcomes.duplicated(["symbol", "signal_date"]).any():
        raise ValueError("entry outcomes are not unique by symbol/session")
    if funnel["signal_date"].duplicated().any():
        raise ValueError("daily funnel dates are not unique")
    for name in _FUNNEL_COLUMNS[1:]:
        values = pd.to_numeric(funnel[name], errors="coerce")
        if values.isna().any() or (values < 0).any() or not (values == values.astype(int)).all():
            raise ValueError(f"daily funnel {name} is not a non-negative integer")
    outcome_counts = outcomes["outcome"].value_counts()
    allowed_outcomes = {"entries_executed", "entry_rejected_next_open_buy_zone", *_REJECTION_COUNTS[:-1], "entry_rejected_no_cash", "entry_rejected_already_open"}
    if not outcomes["outcome"].isin(allowed_outcomes).all():
        raise ValueError("entry outcomes contain an unsupported outcome")
    funnel_totals = funnel[list(_FUNNEL_COLUMNS[1:])].sum().astype(int)
    if int(funnel_totals["qualified_count"]) != authority.qualified_entries:
        raise ValueError("qualified entry count does not reconcile")
    if len(outcomes) != int(funnel_totals["attempted_count"]):
        raise ValueError("entry attempt count does not reconcile")
    if int(outcome_counts.get("entries_executed", 0)) != authority.executed_entries or int(funnel_totals["executed_count"]) != authority.executed_entries:
        raise ValueError("executed entry count does not reconcile")
    if int(outcome_counts.get("entry_rejected_next_open_buy_zone", 0)) != authority.next_open_buy_zone_rejections:
        raise ValueError("next-open buy-zone rejection count does not reconcile")
    if int(outcome_counts.get("entry_rejected_no_cash", 0)) != authority.cash_rejections:
        raise ValueError("cash rejection count does not reconcile")
    if any(int(outcome_counts.get(name, 0)) != 0 for name in _REJECTION_COUNTS[:-1]):
        raise ValueError("entry outcomes contain an impossible capacity/invalid/missing rejection")
    if not (funnel_totals["rejected_count"] == funnel_totals["attempted_count"] - funnel_totals["executed_count"]):
        raise ValueError("daily funnel rejected count does not reconcile")
    outcome_daily = outcomes.assign(
        _executed=outcomes["outcome"].eq("entries_executed")
    ).groupby("signal_date", sort=False).agg(
        attempted_count=("outcome", "size"), executed_count=("_executed", "sum")
    )
    outcome_daily["rejected_count"] = (
        outcome_daily["attempted_count"] - outcome_daily["executed_count"]
    )
    funnel_daily = funnel.set_index("signal_date")
    for name in ("attempted_count", "executed_count", "rejected_count"):
        expected = outcome_daily[name].reindex(funnel_daily.index, fill_value=0).astype(int)
        if not (funnel_daily[name].astype(int) == expected).all():
            raise ValueError(f"daily funnel {name} does not reconcile with outcomes")
    if (funnel_daily["attempted_count"].astype(int) > funnel_daily["qualified_count"].astype(int)).any():
        raise ValueError("daily funnel qualified_count does not cover entry attempts")
    buy_actions = transactions["Action"].astype(str).str.upper().eq("BUY")
    if int(buy_actions.sum()) != authority.executed_entries:
        raise ValueError("BUY transactions do not reconcile with executed entries")
    executed_keys = set(
        zip(
            outcomes.loc[outcomes["outcome"].eq("entries_executed"), "symbol"],
            outcomes.loc[outcomes["outcome"].eq("entries_executed"), "entry_date"],
            strict=True,
        )
    )
    buy_keys = set(zip(transactions.loc[buy_actions, "Ticker"], transactions.loc[buy_actions, "Date"], strict=True))
    if executed_keys != buy_keys:
        raise ValueError("BUY transactions do not exactly reconcile with executed outcomes")


def _verify_equity(equity: pd.DataFrame) -> None:
    if not {"date", "portfolio", "benchmark"}.issubset(equity):
        raise ValueError("equity curve lacks baseline calendar fields")
    if equity.empty or equity["date"].duplicated().any():
        raise ValueError("equity curve is empty or has duplicate dates")
    values = pd.to_numeric(equity[["portfolio", "benchmark"]].stack(), errors="coerce")
    if values.isna().any() or (values <= 0).any() or not values.map(math.isfinite).all():
        raise ValueError("equity curve contains invalid values")


def _normalized_ordered_row_sha256(frame: pd.DataFrame) -> str:
    payload = {
        "columns": list(frame.columns),
        "rows": [
            [_normalized_row_value(value) for value in row]
            for row in frame.itertuples(index=False, name=None)
        ],
    }
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalized_row_value(value: object) -> object:
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, str):
        return None if value == "" else value
    if isinstance(value, bool) or value.__class__.__name__ == "bool_":
        return ("bool", bool(value))
    if isinstance(value, Real):
        number = float(value)
        if not math.isfinite(number):
            return None
        return ("number", format(number, ".17g"))
    if pd.isna(value):
        return None
    return str(value)


def _csv_frames_equal(left_path: Path, right_path: Path) -> bool:
    try:
        left = _read_csv(left_path)
        right = _read_csv(right_path)
    except ValueError:
        return False
    if tuple(left.columns) != tuple(right.columns) or len(left) != len(right):
        return False
    for left_row, right_row in zip(left.itertuples(index=False, name=None), right.itertuples(index=False, name=None), strict=True):
        if not all(_normalized_values_equal(one, two) for one, two in zip(left_row, right_row, strict=True)):
            return False
    return True


def _json_values_equal(left_path: Path, right_path: Path) -> bool:
    try:
        return _load_json(left_path, left_path.name) == _load_json(right_path, right_path.name)
    except ValueError:
        return False


def _normalized_values_equal(left: object, right: object) -> bool:
    if pd.isna(left) and pd.isna(right):
        return True
    if left == right:
        return True
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(float(left), float(right), rel_tol=1e-12, abs_tol=1e-12)
    return False
