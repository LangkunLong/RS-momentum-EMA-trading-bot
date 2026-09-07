"""Deterministic controller-owned panel planning for PIT optimizer V5."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction
from pathlib import Path, PurePosixPath
from typing import Literal

from core.backtest_fills import ExecutionProfileV5
from core.pit_optimizer_evaluation import (
    EvaluationPanelSpec,
    PanelSecurityLineage,
    QualificationRetirementLedger,
)
from core.pit_optimizer_v5.artifacts import LocalArtifactRepositoryV5
from core.pit_optimizer_v5.memory import ExperimentRecordV5
from core.pit_optimizer_v5.search import (
    ArchiveEntryV5,
    BaselineParentAuthorityV5,
    baseline_parent_candidate_v5,
    verified_campaign_cagr_pct,
)
from core.pit_optimizer_v5.contracts import (
    AnnualizedReturnTargetV5,
    ArtifactRefV5,
    CampaignManifestV5,
    CampaignPanelPlanV5,
    ConfirmationAttemptCommitmentV5,
    ConfirmationOutcomeV5,
    ConfirmationPanelPlanV5,
    EpisodePlanV5,
    EvaluatorContractV5,
    PanelEvaluationV5,
    QualificationAttemptCommitmentV5,
    QualificationOutcomeV5,
    QualificationPanelPlanV5,
    SandboxProfileV5,
    ScenarioGridV5,
    canonical_sha256_v5,
    selected_scenario,
    validate_campaign_manifest_bindings_v5,
    validate_episode_plan_panel_v5,
)


StageNameV5 = Literal["confirmation", "qualification"]

CANONICAL_STAGE_LEDGER_PATHS_V5 = {
    "confirmation": "panels/confirmation-retirement.json",
    "qualification": "panels/qualification-retirement.json",
}
AVERAGE_DOLLAR_VOLUME_HISTORY_ROWS_V5 = 51
ANNUAL_HIGH_HISTORY_ROWS_V5 = 252

AFFILIATION_BITSET_ORDER_V5 = (
    ("sp500",),
    ("nasdaq100",),
    ("sp500", "nasdaq100"),
    ("russell2000",),
    ("sp500", "russell2000"),
    ("nasdaq100", "russell2000"),
    ("sp500", "nasdaq100", "russell2000"),
)
COHORT_COUNTS_V5 = {
    "mechanics": 24,
    "quick": 96,
    "discovery-1": 150,
    "discovery-2": 150,
    "discovery-3": 150,
    "discovery-4": 150,
    "confirmation": 400,
    "qualification": 500,
}
_ALLOCATION_ORDER_V5 = (
    "qualification",
    "confirmation",
    "discovery-4",
    "discovery-3",
    "discovery-2",
    "discovery-1",
    "quick",
    "mechanics",
)
_DISCOVERY_KEYS_V5 = ("discovery-1", "discovery-2", "discovery-3", "discovery-4")
_RAW_PURPOSE_BY_COHORT_V5 = {
    "mechanics": "quick",
    "quick": "quick",
    "discovery-1": "discovery",
    "discovery-2": "discovery",
    "discovery-3": "discovery",
    "discovery-4": "discovery",
    # The legacy raw-panel vocabulary has no confirmation purpose.  Only the
    # typed V5 owner makes this a confirmation panel; raw purpose is not stage
    # authority and may never be used to select or open it.
    "confirmation": "qualification",
    "qualification": "qualification",
}


@dataclass(frozen=True, slots=True)
class StageRetirementSnapshotV5:
    schema_version: Literal[5]
    stage: StageNameV5
    ledger_relative_path: str
    retirement_domain_id: str
    ledger_head_sha256: str
    record_count: int
    retired_security_lineage_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 5 or self.stage not in {"confirmation", "qualification"}:
            raise ValueError("stage retirement snapshot is invalid")
        ArtifactRefV5(self.ledger_relative_path, "0" * 64)
        if self.ledger_relative_path != CANONICAL_STAGE_LEDGER_PATHS_V5[self.stage]:
            raise ValueError("stage retirement snapshot ledger location is not canonical")
        for value in (self.retirement_domain_id, self.ledger_head_sha256):
            ArtifactRefV5("identity", value)
        if type(self.record_count) is not int or self.record_count < 1:
            raise ValueError("stage retirement snapshot record count is invalid")
        if (
            type(self.retired_security_lineage_ids) is not tuple
            or self.retired_security_lineage_ids != tuple(sorted(set(self.retired_security_lineage_ids)))
        ):
            raise ValueError("stage retirement snapshot lineages are invalid")


@dataclass(frozen=True, slots=True)
class BuiltPanelPlansV5:
    discovery: CampaignPanelPlanV5
    confirmation: ConfirmationPanelPlanV5
    qualification: QualificationPanelPlanV5

    def __post_init__(self) -> None:
        if (
            type(self.discovery) is not CampaignPanelPlanV5
            or type(self.confirmation) is not ConfirmationPanelPlanV5
            or type(self.qualification) is not QualificationPanelPlanV5
        ):
            raise ValueError("built panel plans require all three typed V5 owners")


def _digest_seed_v5(seed_label: str) -> str:
    if (
        type(seed_label) is not str
        or not seed_label
        or seed_label != seed_label.strip()
        or len(seed_label.encode("utf-8")) > 256
    ):
        raise ValueError("partition seed label is invalid")
    return hashlib.sha256(seed_label.encode("utf-8")).hexdigest()


def _stage_domain_id_v5(
    *,
    stage: StageNameV5,
    pit_bundle_ref: ArtifactRefV5,
    prices_provenance_ref: ArtifactRefV5,
    ledger_relative_path: str,
) -> str:
    if stage not in {"confirmation", "qualification"}:
        raise ValueError("retirement stage is invalid")
    ArtifactRefV5(ledger_relative_path, "0" * 64)
    if ledger_relative_path != CANONICAL_STAGE_LEDGER_PATHS_V5[stage]:
        raise ValueError("retirement ledger location is not canonical for its stage")
    return canonical_sha256_v5(
        {
            "schema_version": 5,
            "domain": "pit-optimizer-v5-stage-retirement-v1",
            "stage": stage,
            "ledger_relative_path": ledger_relative_path,
            "pit_bundle_sha256": pit_bundle_ref.sha256,
            "prices_provenance_sha256": prices_provenance_ref.sha256,
        }
    )


def _absolute_artifact_path_v5(root: Path, relative_path: str) -> Path:
    ArtifactRefV5(relative_path, "0" * 64)
    parts = PurePosixPath(relative_path).parts
    candidate = root.joinpath(*parts)
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError("artifact path escapes the V5 root") from None
    current = root
    for part in parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise ValueError("artifact path crosses a symbolic link")
    return candidate


def _existing_stage_ledger_path_v5(root: Path, relative_path: str) -> Path:
    """Resolve a stage ledger without permitting the legacy constructor to create it."""

    candidate = _absolute_artifact_path_v5(root, relative_path)
    if not candidate.exists() or not candidate.is_file() or candidate.is_symlink():
        raise ValueError("canonical stage ledger is absent or not a regular non-link file")
    return candidate


def _snapshot_from_ledger_v5(
    *,
    stage: StageNameV5,
    ledger_relative_path: str,
    ledger: QualificationRetirementLedger,
) -> StageRetirementSnapshotV5:
    snapshot = ledger.snapshot()
    return StageRetirementSnapshotV5(
        schema_version=5,
        stage=stage,
        ledger_relative_path=ledger_relative_path,
        retirement_domain_id=snapshot.qualification_retirement_domain_id,
        ledger_head_sha256=snapshot.ledger_head_sha256,
        record_count=snapshot.record_count,
        retired_security_lineage_ids=snapshot.retired_security_lineage_ids,
    )


def _snapshot_relative_path_v5(snapshot: StageRetirementSnapshotV5) -> str:
    return f"panels/retirement-snapshots/{snapshot.stage}-{snapshot.retirement_domain_id}.json"


def initialize_stage_ledgers_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    pit_bundle_ref: ArtifactRefV5,
    prices_provenance_ref: ArtifactRefV5,
    confirmation_ledger_path: str,
    qualification_ledger_path: str,
) -> Mapping[str, object]:
    """Create/authenticate two distinct append-only domains and immutable genesis snapshots."""

    if type(repository) is not LocalArtifactRepositoryV5:
        raise ValueError("stage ledgers require a local V5 artifact repository")
    if confirmation_ledger_path == qualification_ledger_path:
        raise ValueError("confirmation and qualification ledgers must be distinct")
    if (
        confirmation_ledger_path != CANONICAL_STAGE_LEDGER_PATHS_V5["confirmation"]
        or qualification_ledger_path != CANONICAL_STAGE_LEDGER_PATHS_V5["qualification"]
    ):
        raise ValueError("stage retirement ledgers must use their one canonical location")
    repository.authenticate_raw_artifact(pit_bundle_ref)
    repository.authenticate_raw_artifact(prices_provenance_ref)
    result: dict[str, object] = {"schema_version": 5}
    domains: set[str] = set()
    for stage, relative_path in (
        ("confirmation", confirmation_ledger_path),
        ("qualification", qualification_ledger_path),
    ):
        absolute = _absolute_artifact_path_v5(repository.root, relative_path)
        domain_id = _stage_domain_id_v5(
            stage=stage,
            pit_bundle_ref=pit_bundle_ref,
            prices_provenance_ref=prices_provenance_ref,
            ledger_relative_path=relative_path,
        )
        domains.add(domain_id)
        ledger = QualificationRetirementLedger(absolute, domain_id)
        snapshot = _snapshot_from_ledger_v5(
            stage=stage,
            ledger_relative_path=relative_path,
            ledger=ledger,
        )
        if snapshot.record_count != 1 or snapshot.retired_security_lineage_ids:
            raise ValueError("stage ledger is no longer at its unopened create-only genesis")
        snapshot_ref = repository.create_typed_artifact(_snapshot_relative_path_v5(snapshot), snapshot)
        result[stage] = {
            "retirement_domain_id": domain_id,
            "ledger_head_sha256": snapshot.ledger_head_sha256,
            "snapshot_sha256": snapshot_ref.sha256,
        }
    if len(domains) != 2:
        raise ValueError("stage retirement domains must be distinct")
    return result


def _sessions_by_cohort_v5(sessions: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    if type(sessions) is not tuple or len(sessions) < 10:
        raise ValueError("five-year panel calendar is incomplete")
    parsed = tuple(date.fromisoformat(item) for item in sessions)
    if tuple(item.isoformat() for item in parsed) != sessions or any(
        left >= right for left, right in zip(parsed, parsed[1:], strict=False)
    ):
        raise ValueError("panel sessions must be canonical, unique, and chronological")
    windows = {
        "discovery-1": tuple(item for item in sessions if "2021-01-01" <= item <= "2021-06-30"),
        "discovery-2": tuple(item for item in sessions if "2021-07-01" <= item <= "2021-12-31"),
        "discovery-3": tuple(item for item in sessions if "2022-01-01" <= item <= "2022-12-31"),
        "discovery-4": tuple(item for item in sessions if "2023-01-01" <= item <= "2023-12-31"),
        "confirmation": tuple(item for item in sessions if "2024-01-01" <= item <= "2024-12-31"),
        "qualification": tuple(item for item in sessions if "2025-01-01" <= item <= "2025-12-31"),
    }
    if any(len(items) < 2 for items in windows.values()):
        raise ValueError("panel calendar must cover both 2021 half-years and calendar years 2022 through 2025")
    windows["mechanics"] = windows["discovery-1"]
    windows["quick"] = windows["discovery-1"]
    return windows


def lineage_has_causal_feature_coverage_v5(
    *,
    lineage: PanelSecurityLineage,
    active_occurrences: tuple[tuple[str, str], ...],
    price_sessions_by_ticker: Mapping[str, frozenset[str]],
    reference_sessions: tuple[str, ...],
) -> bool:
    """Prove continuous price history for every causal V3 price feature.

    ADV50 consumes the event row plus 50 prior rows (51 total), while the
    annual-high distance consumes 252 rows.  The stronger 252-session window
    is required at every active session and may continue across authenticated
    aliases within the same security lineage.
    """

    if type(lineage) is not PanelSecurityLineage or not active_occurrences:
        return False
    if type(reference_sessions) is not tuple or len(set(reference_sessions)) != len(reference_sessions):
        raise ValueError("feature-history reference sessions are invalid")
    reference_index = {session: index for index, session in enumerate(reference_sessions)}
    aliases = frozenset(lineage.executable_tickers)
    if any(ticker not in aliases or session not in reference_index for ticker, session in active_occurrences):
        raise ValueError("feature-history activity differs from its lineage/calendar authority")
    lineage_price_sessions = frozenset(
        session
        for ticker in aliases
        for session in price_sessions_by_ticker.get(ticker, frozenset())
    )
    for ticker, session in active_occurrences:
        index = reference_index[session]
        if session not in price_sessions_by_ticker.get(ticker, frozenset()):
            return False
        if index + 1 < ANNUAL_HIGH_HISTORY_ROWS_V5:
            return False
        annual_window = reference_sessions[
            index + 1 - ANNUAL_HIGH_HISTORY_ROWS_V5 : index + 1
        ]
        adv_window = reference_sessions[
            index + 1 - AVERAGE_DOLLAR_VOLUME_HISTORY_ROWS_V5 : index + 1
        ]
        if not set(annual_window).issubset(lineage_price_sessions) or not set(adv_window).issubset(
            lineage_price_sessions
        ):
            return False
    return True


def _stratified_take_v5(
    *,
    cohort: str,
    count: int,
    candidates: tuple[PanelSecurityLineage, ...],
    partition_seed_sha256: str,
) -> tuple[PanelSecurityLineage, ...]:
    strata = {
        key: tuple(item for item in candidates if item.source_affiliations == key)
        for key in AFFILIATION_BITSET_ORDER_V5
    }
    capacity = {key: len(items) for key, items in strata.items()}
    total = sum(capacity.values())
    if total < count:
        raise ValueError(f"{cohort} cohort has insufficient causally covered lineages")
    quotas = {key: Fraction(count * value, total) for key, value in capacity.items()}
    assigned = {key: quotas[key].numerator // quotas[key].denominator for key in capacity}
    remaining = count - sum(assigned.values())
    order = {key: index for index, key in enumerate(AFFILIATION_BITSET_ORDER_V5)}
    for key in sorted(
        capacity,
        key=lambda item: (-(quotas[item] - assigned[item]), order[item]),
    ):
        if remaining == 0:
            break
        if assigned[key] < capacity[key]:
            assigned[key] += 1
            remaining -= 1
    if remaining:
        raise ValueError(f"{cohort} cohort stratum apportionment failed")
    selected: list[PanelSecurityLineage] = []
    for key in AFFILIATION_BITSET_ORDER_V5:
        ranked = sorted(
            strata[key],
            key=lambda item: (
                hashlib.sha256(
                    f"{partition_seed_sha256}\0{cohort}\0{item.security_lineage_id}".encode()
                ).hexdigest(),
                item.security_lineage_id,
            ),
        )
        selected.extend(ranked[: assigned[key]])
    return tuple(sorted(selected, key=lambda item: item.security_lineage_id))


def allocate_panel_cohorts_v5(
    *,
    lineages: tuple[PanelSecurityLineage, ...],
    eligible_lineage_ids: Mapping[str, frozenset[str]],
    partition_seed_sha256: str,
) -> Mapping[str, tuple[PanelSecurityLineage, ...]]:
    """Allocate every lineage once, protecting latest held-out capacity first."""

    ArtifactRefV5("seed", partition_seed_sha256)
    if (
        type(lineages) is not tuple
        or len({item.security_lineage_id for item in lineages}) != len(lineages)
        or any(type(item) is not PanelSecurityLineage for item in lineages)
    ):
        raise ValueError("panel lineage authority is invalid")
    if set(eligible_lineage_ids) != set(COHORT_COUNTS_V5):
        raise ValueError("panel causal-coverage authority is incomplete")
    by_id = {item.security_lineage_id: item for item in lineages}
    assigned_ids: set[str] = set()
    allocations: dict[str, tuple[PanelSecurityLineage, ...]] = {}
    for cohort in _ALLOCATION_ORDER_V5:
        eligible = eligible_lineage_ids[cohort]
        if type(eligible) is not frozenset or not eligible.issubset(by_id):
            raise ValueError(f"{cohort} causal-coverage lineage set is invalid")
        candidates = tuple(by_id[item] for item in sorted(eligible - assigned_ids))
        selected = _stratified_take_v5(
            cohort=cohort,
            count=COHORT_COUNTS_V5[cohort],
            candidates=candidates,
            partition_seed_sha256=partition_seed_sha256,
        )
        allocations[cohort] = selected
        assigned_ids.update(item.security_lineage_id for item in selected)
    if len(assigned_ids) != sum(COHORT_COUNTS_V5.values()):
        raise ValueError("panel allocation contains a cross-cohort security lineage")
    return allocations


def _episode_v5(
    *,
    cohort: str,
    ordinal: int | None,
    sessions: tuple[str, ...],
    lineages: tuple[PanelSecurityLineage, ...],
    persist_panel: Callable[[str, EvaluationPanelSpec], ArtifactRefV5],
) -> EpisodePlanV5:
    panel = EvaluationPanelSpec.from_lineages(
        purpose=_RAW_PURPOSE_BY_COHORT_V5[cohort],
        sessions=sessions,
        lineages=lineages,
    )
    reference = persist_panel(cohort, panel)
    if reference.sha256 != panel.sha256:
        raise ValueError("persisted panel bytes differ from the complete panel specification")
    episode = EpisodePlanV5(
        episode_id=cohort,
        episode_ordinal=ordinal,
        purpose=panel.purpose,
        start_date=panel.start_date,
        end_date=panel.end_date,
        lineage_ids=tuple(item.security_lineage_id for item in panel.lineages),
        panel_ref=reference,
    )
    validate_episode_plan_panel_v5(episode, panel)
    return episode


def compose_panel_plans_v5(
    *,
    lineages: tuple[PanelSecurityLineage, ...],
    sessions: tuple[str, ...],
    eligible_lineage_ids: Mapping[str, frozenset[str]],
    pit_bundle_ref: ArtifactRefV5,
    prices_provenance_ref: ArtifactRefV5,
    partition_seed: str,
    target: AnnualizedReturnTargetV5,
    confirmation_snapshot: StageRetirementSnapshotV5,
    qualification_snapshot: StageRetirementSnapshotV5,
    persist_panel: Callable[[str, EvaluationPanelSpec], ArtifactRefV5],
) -> BuiltPanelPlansV5:
    """Compose all typed owners; held-out paths never enter the discovery plan."""

    if type(target) is not AnnualizedReturnTargetV5 or not callable(persist_panel):
        raise ValueError("panel composition authority is invalid")
    if confirmation_snapshot.stage != "confirmation" or qualification_snapshot.stage != "qualification":
        raise ValueError("stage snapshots differ from their typed owner")
    partition_seed_sha256 = _digest_seed_v5(partition_seed)
    sessions_by_cohort = _sessions_by_cohort_v5(sessions)
    allocations = allocate_panel_cohorts_v5(
        lineages=lineages,
        eligible_lineage_ids=eligible_lineage_ids,
        partition_seed_sha256=partition_seed_sha256,
    )
    episodes = {
        cohort: _episode_v5(
            cohort=cohort,
            ordinal=(int(cohort[-1]) if cohort in _DISCOVERY_KEYS_V5 else None),
            sessions=sessions_by_cohort[cohort],
            lineages=allocations[cohort],
            persist_panel=persist_panel,
        )
        for cohort in COHORT_COUNTS_V5
    }
    confirmation = ConfirmationPanelPlanV5(
        schema_version=5,
        pit_bundle_sha256=pit_bundle_ref.sha256,
        partition_seed_sha256=partition_seed_sha256,
        target_sha256=target.sha256,
        confirmation_retirement_domain_id=confirmation_snapshot.retirement_domain_id,
        confirmation_ledger_snapshot_sha256=confirmation_snapshot.ledger_head_sha256,
        episode=episodes["confirmation"],
    )
    qualification = QualificationPanelPlanV5(
        schema_version=5,
        pit_bundle_sha256=pit_bundle_ref.sha256,
        partition_seed_sha256=partition_seed_sha256,
        target=target,
        qualification_retirement_domain_id=qualification_snapshot.retirement_domain_id,
        qualification_ledger_snapshot_sha256=qualification_snapshot.ledger_head_sha256,
        episode=episodes["qualification"],
    )
    discovery = CampaignPanelPlanV5(
        schema_version=5,
        pit_bundle_ref=pit_bundle_ref,
        prices_provenance_ref=prices_provenance_ref,
        partition_seed_sha256=partition_seed_sha256,
        target_sha256=target.sha256,
        mechanics=episodes["mechanics"],
        quick=episodes["quick"],
        discovery=tuple(episodes[item] for item in _DISCOVERY_KEYS_V5),
        confirmation_plan_sha256=confirmation.sha256,
        qualification_plan_sha256=qualification.sha256,
    )
    return BuiltPanelPlansV5(discovery, confirmation, qualification)


def load_bundle_panel_authority_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    pit_bundle_ref: ArtifactRefV5,
    prices_provenance_ref: ArtifactRefV5,
    start_date: str,
    end_date: str,
) -> tuple[
    tuple[PanelSecurityLineage, ...],
    tuple[str, ...],
    Mapping[str, frozenset[str]],
]:
    """Authenticate V3 bundle structure and derive causal per-episode eligibility."""

    repository.authenticate_raw_artifact(pit_bundle_ref)
    repository.authenticate_raw_artifact(prices_provenance_ref)
    bundle_path = _absolute_artifact_path_v5(repository.root, pit_bundle_ref.relative_path)
    provenance_path = _absolute_artifact_path_v5(repository.root, prices_provenance_ref.relative_path)
    if bundle_path.is_symlink() or provenance_path.is_symlink():
        raise ValueError("panel data authorities must be regular non-link files")
    if hashlib.sha256(provenance_path.read_bytes()).hexdigest() != prices_provenance_ref.sha256:
        raise ValueError("prices provenance differs from its authenticated reference")
    from core.pit_data import PITDataBundle
    import pandas as pd

    with PITDataBundle(bundle_path, expected_sha256=pit_bundle_ref.sha256) as bundle:
        if bundle.metadata.get("schema_version") != "3":
            raise ValueError("V5 panels require the authenticated schema-V3 three-universe bundle")
        transition = bundle.load_price_identity_transition_contract(provenance_path)
        if transition.prices_provenance_sha256 != prices_provenance_ref.sha256:
            raise ValueError("bundle transition authority differs from prices provenance")
        reference = bundle.fetch_price_data(("SPY",), pd.Timestamp(start_date), pd.Timestamp(end_date)).get("SPY")
        if reference is None or reference.empty:
            raise ValueError("panel session calendar is absent")
        sessions = tuple(item.date().isoformat() for item in reference.index)
        windows = _sessions_by_cohort_v5(sessions)
        ticker_lineages = bundle.security_lineage_ids()
        affiliations_by_lineage: dict[str, set[str]] = {}
        active_by_cohort: dict[str, dict[str, list[tuple[str, str]]]] = {
            cohort: {} for cohort in COHORT_COUNTS_V5
        }
        for cohort, cohort_sessions in windows.items():
            for session in cohort_sessions:
                affiliations = bundle.affiliations_at(session)
                for ticker, values in affiliations.items():
                    lineage_id = ticker_lineages.get(ticker)
                    if lineage_id is None:
                        raise ValueError("active ticker lacks a security-lineage identity")
                    affiliations_by_lineage.setdefault(lineage_id, set()).update(values)
                    active_by_cohort[cohort].setdefault(lineage_id, []).append((ticker, session))
        grouped_tickers: dict[str, list[str]] = {}
        for ticker, lineage_id in ticker_lineages.items():
            grouped_tickers.setdefault(lineage_id, []).append(ticker)
        lineages = tuple(
            sorted(
                (
                    PanelSecurityLineage(
                        security_lineage_id=lineage_id,
                        executable_tickers=tuple(
                            sorted(
                                grouped_tickers[lineage_id],
                                key=lambda ticker: (
                                    str(transition.identities[ticker]["admitted_start"]),
                                    str(transition.identities[ticker]["admitted_end"]),
                                    ticker,
                                ),
                            )
                        ),
                        source_affiliations=tuple(
                            item
                            for item in ("sp500", "nasdaq100", "russell2000")
                            if item in affiliations
                        ),
                    )
                    for lineage_id, affiliations in affiliations_by_lineage.items()
                ),
                key=lambda item: item.security_lineage_id,
            )
        )
        all_tickers = tuple(ticker for item in lineages for ticker in item.executable_tickers)
        warmup_start = bundle.metadata.get("warmup_start")
        if type(warmup_start) is not str or warmup_start > sessions[0]:
            raise ValueError("bundle warmup cannot supply causal panel features")
        price_frames = bundle.fetch_price_data(all_tickers, pd.Timestamp(warmup_start), pd.Timestamp(end_date))
        history_reference = bundle.fetch_price_data(
            ("SPY",),
            pd.Timestamp(warmup_start),
            pd.Timestamp(end_date),
        ).get("SPY")
        if history_reference is None or history_reference.empty:
            raise ValueError("causal feature-history session authority is absent")
        reference_sessions = tuple(item.date().isoformat() for item in history_reference.index)
        price_sessions = {
            ticker: frozenset(item.date().isoformat() for item in frame.index)
            for ticker, frame in price_frames.items()
        }
        lineage_by_id = {item.security_lineage_id: item for item in lineages}
        eligibility: dict[str, frozenset[str]] = {}
        for cohort, activity in active_by_cohort.items():
            eligible: set[str] = set()
            for lineage_id, occurrences in activity.items():
                lineage = lineage_by_id[lineage_id]
                if len({session for _ticker, session in occurrences}) >= 2 and lineage_has_causal_feature_coverage_v5(
                    lineage=lineage,
                    active_occurrences=tuple(occurrences),
                    price_sessions_by_ticker=price_sessions,
                    reference_sessions=reference_sessions,
                ):
                    eligible.add(lineage_id)
            eligibility[cohort] = frozenset(eligible)
    return lineages, sessions, eligibility


def _authenticate_initial_snapshot_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    stage: StageNameV5,
    domain_id: str,
    ledger_path: str,
) -> StageRetirementSnapshotV5:
    ledger_path_on_disk = _existing_stage_ledger_path_v5(repository.root, ledger_path)
    ledger = QualificationRetirementLedger(
        ledger_path_on_disk,
        domain_id,
        create_if_missing=False,
    )
    live = _snapshot_from_ledger_v5(
        stage=stage,
        ledger_relative_path=ledger_path,
        ledger=ledger,
    )
    snapshot_path = _snapshot_relative_path_v5(live)
    snapshot_ref = ArtifactRefV5(snapshot_path, canonical_sha256_v5(live))
    stored = repository.load_typed_artifact(snapshot_ref, value_type=StageRetirementSnapshotV5)
    if stored != live or live.record_count != 1 or live.retired_security_lineage_ids:
        raise ValueError("stage ledger differs from its immutable unopened snapshot")
    return live


def build_panels_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    pit_bundle_ref: ArtifactRefV5,
    prices_provenance_ref: ArtifactRefV5,
    start_date: str,
    end_date: str,
    partition_seed: str,
    target: AnnualizedReturnTargetV5,
    confirmation_ledger_path: str,
    qualification_ledger_path: str,
    discovery_output_path: str,
    confirmation_output_path: str,
    qualification_output_path: str,
) -> tuple[BuiltPanelPlansV5, tuple[ArtifactRefV5, ArtifactRefV5, ArtifactRefV5]]:
    """Build and create-only publish all three owner plans."""

    if (
        confirmation_ledger_path != CANONICAL_STAGE_LEDGER_PATHS_V5["confirmation"]
        or qualification_ledger_path != CANONICAL_STAGE_LEDGER_PATHS_V5["qualification"]
    ):
        raise ValueError("panel build requires both canonical stage-ledger locations")
    output_paths = (discovery_output_path, confirmation_output_path, qualification_output_path)
    if len(set(output_paths)) != 3 or any(not item.startswith("panels/") for item in output_paths):
        raise ValueError("panel output paths must be three distinct canonical paths beneath panels/")
    lineages, sessions, eligibility = load_bundle_panel_authority_v5(
        repository=repository,
        pit_bundle_ref=pit_bundle_ref,
        prices_provenance_ref=prices_provenance_ref,
        start_date=start_date,
        end_date=end_date,
    )
    confirmation_domain = _stage_domain_id_v5(
        stage="confirmation",
        pit_bundle_ref=pit_bundle_ref,
        prices_provenance_ref=prices_provenance_ref,
        ledger_relative_path=confirmation_ledger_path,
    )
    qualification_domain = _stage_domain_id_v5(
        stage="qualification",
        pit_bundle_ref=pit_bundle_ref,
        prices_provenance_ref=prices_provenance_ref,
        ledger_relative_path=qualification_ledger_path,
    )
    confirmation_snapshot = _authenticate_initial_snapshot_v5(
        repository=repository,
        stage="confirmation",
        domain_id=confirmation_domain,
        ledger_path=confirmation_ledger_path,
    )
    qualification_snapshot = _authenticate_initial_snapshot_v5(
        repository=repository,
        stage="qualification",
        domain_id=qualification_domain,
        ledger_path=qualification_ledger_path,
    )

    def persist_panel(cohort: str, panel: EvaluationPanelSpec) -> ArtifactRefV5:
        return repository.create_evaluation_panel_spec(
            f"panels/specs/{cohort}-{panel.sha256}.json",
            panel,
        )

    plans = compose_panel_plans_v5(
        lineages=lineages,
        sessions=sessions,
        eligible_lineage_ids=eligibility,
        pit_bundle_ref=pit_bundle_ref,
        prices_provenance_ref=prices_provenance_ref,
        partition_seed=partition_seed,
        target=target,
        confirmation_snapshot=confirmation_snapshot,
        qualification_snapshot=qualification_snapshot,
        persist_panel=persist_panel,
    )
    confirmation_ref = repository.create_typed_artifact(confirmation_output_path, plans.confirmation)
    qualification_ref = repository.create_typed_artifact(qualification_output_path, plans.qualification)
    discovery_ref = repository.create_typed_artifact(discovery_output_path, plans.discovery)
    if (
        confirmation_ref.sha256 != plans.discovery.confirmation_plan_sha256
        or qualification_ref.sha256 != plans.discovery.qualification_plan_sha256
        or discovery_ref.sha256 != plans.discovery.sha256
    ):
        raise ValueError("published panel owner commitment differs")
    return plans, (discovery_ref, confirmation_ref, qualification_ref)


def _owned_episodes_v5(
    plans: BuiltPanelPlansV5,
) -> tuple[tuple[str, EpisodePlanV5, int], ...]:
    return (
        ("mechanics", plans.discovery.mechanics, 24),
        ("quick", plans.discovery.quick, 96),
        *((f"discovery-{index}", item, 150) for index, item in enumerate(plans.discovery.discovery, 1)),
        ("confirmation", plans.confirmation.episode, 400),
        ("qualification", plans.qualification.episode, 500),
    )


def validate_owned_panel_plans_v5(
    *,
    repository: object,
    plans: BuiltPanelPlansV5,
) -> None:
    """Require typed owners before interpreting legacy raw-panel purpose values."""

    discovery = plans.discovery
    confirmation = plans.confirmation
    qualification = plans.qualification
    expected_confirmation_domain = _stage_domain_id_v5(
        stage="confirmation",
        pit_bundle_ref=discovery.pit_bundle_ref,
        prices_provenance_ref=discovery.prices_provenance_ref,
        ledger_relative_path=CANONICAL_STAGE_LEDGER_PATHS_V5["confirmation"],
    )
    expected_qualification_domain = _stage_domain_id_v5(
        stage="qualification",
        pit_bundle_ref=discovery.pit_bundle_ref,
        prices_provenance_ref=discovery.prices_provenance_ref,
        ledger_relative_path=CANONICAL_STAGE_LEDGER_PATHS_V5["qualification"],
    )
    load_panel = getattr(repository, "load_evaluation_panel_spec", None)
    if not callable(load_panel):
        raise ValueError("typed panel owner verification requires an authenticated panel loader")
    if (
        discovery.confirmation_plan_sha256 != confirmation.sha256
        or discovery.qualification_plan_sha256 != qualification.sha256
        or discovery.pit_bundle_ref.sha256 != confirmation.pit_bundle_sha256
        or discovery.pit_bundle_ref.sha256 != qualification.pit_bundle_sha256
        or discovery.partition_seed_sha256 != confirmation.partition_seed_sha256
        or discovery.partition_seed_sha256 != qualification.partition_seed_sha256
        or discovery.target_sha256 != confirmation.target_sha256
        or discovery.target_sha256 != qualification.target.sha256
        or confirmation.confirmation_retirement_domain_id != expected_confirmation_domain
        or qualification.qualification_retirement_domain_id != expected_qualification_domain
    ):
        raise ValueError("panel owner commitments are inconsistent")
    seen: set[str] = set()
    for cohort, episode, count in _owned_episodes_v5(plans):
        if episode.episode_id != cohort or episode.purpose != _RAW_PURPOSE_BY_COHORT_V5[cohort]:
            raise ValueError("raw panel purpose cannot substitute for its typed V5 stage owner")
        if len(episode.lineage_ids) != count or seen.intersection(episode.lineage_ids):
            raise ValueError("panel security cohorts are not exactly sized and disjoint")
        panel = load_panel(episode.panel_ref)
        validate_episode_plan_panel_v5(episode, panel)
        if panel.purpose != _RAW_PURPOSE_BY_COHORT_V5[cohort]:
            raise ValueError("raw panel differs from its typed V5 owner")
        seen.update(episode.lineage_ids)
    discovery_end = max(date.fromisoformat(item.end_date) for item in discovery.discovery)
    confirmation_start = date.fromisoformat(confirmation.episode.start_date)
    confirmation_end = date.fromisoformat(confirmation.episode.end_date)
    qualification_start = date.fromisoformat(qualification.episode.start_date)
    if not discovery_end < confirmation_start <= confirmation_end < qualification_start:
        raise ValueError("discovery, confirmation, and qualification are not time-separated")


def verify_panels_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    discovery_ref: ArtifactRefV5,
    confirmation_ref: ArtifactRefV5,
    qualification_ref: ArtifactRefV5,
) -> Mapping[str, object]:
    """Authenticate all owners/specs and return a content-free projection."""

    plans = BuiltPanelPlansV5(
        discovery=repository.load_typed_artifact(discovery_ref, value_type=CampaignPanelPlanV5),
        confirmation=repository.load_typed_artifact(confirmation_ref, value_type=ConfirmationPanelPlanV5),
        qualification=repository.load_typed_artifact(qualification_ref, value_type=QualificationPanelPlanV5),
    )
    if (
        discovery_ref.sha256 != plans.discovery.sha256
        or confirmation_ref.sha256 != plans.confirmation.sha256
        or qualification_ref.sha256 != plans.qualification.sha256
    ):
        raise ValueError("panel owner file differs from its canonical identity")
    validate_owned_panel_plans_v5(repository=repository, plans=plans)
    return discovery_evidence_projection_v5(plans)


def discovery_evidence_projection_v5(plans: BuiltPanelPlansV5) -> Mapping[str, object]:
    """Project identities/counts only: never paths, members, signals, trades, or returns."""

    return {
        "schema_version": 5,
        "pit_bundle_sha256": plans.discovery.pit_bundle_ref.sha256,
        "partition_seed_sha256": plans.discovery.partition_seed_sha256,
        "target_sha256": plans.discovery.target_sha256,
        "discovery_plan_sha256": plans.discovery.sha256,
        "confirmation_plan_sha256": plans.confirmation.sha256,
        "qualification_plan_sha256": plans.qualification.sha256,
        "cohorts": tuple(
            {
                "stage": cohort,
                "lineage_count": count,
                "start_date": episode.start_date,
                "end_date": episode.end_date,
                "panel_sha256": episode.panel_ref.sha256,
            }
            for cohort, episode, count in _owned_episodes_v5(plans)
        ),
        "security_lineages_pairwise_disjoint": True,
        "held_out_time_separation_verified": True,
    }


def confirmation_outcome_v5(
    *,
    attempt_ref: ArtifactRefV5,
    status: Literal["completed", "failed", "timed_out", "cancelled"],
    confirmed_policy_ref: ArtifactRefV5,
    baseline_evidence_ref: ArtifactRefV5 | None,
    candidate_evidence_ref: ArtifactRefV5 | None,
    baseline_cagr_pct: Decimal | None,
    candidate_cagr_pct: Decimal | None,
    behaviorally_active: bool | None,
    retirement_terminal_ref: ArtifactRefV5,
    cleanup_evidence_ref: ArtifactRefV5,
) -> ConfirmationOutcomeV5:
    """Recompute the confirmation excess and eligibility gate."""

    completed = status == "completed"
    excess = (
        candidate_cagr_pct - baseline_cagr_pct
        if completed and candidate_cagr_pct is not None and baseline_cagr_pct is not None
        else None
    )
    eligible = bool(completed and behaviorally_active and excess is not None and excess > 0)
    return ConfirmationOutcomeV5(
        5,
        attempt_ref,
        status,
        confirmed_policy_ref,
        baseline_evidence_ref if completed else None,
        candidate_evidence_ref if completed else None,
        baseline_cagr_pct if completed else None,
        candidate_cagr_pct if completed else None,
        excess,
        behaviorally_active if completed else None,
        eligible,
        retirement_terminal_ref,
        cleanup_evidence_ref,
        0,
    )


def confirmation_attempt_commitment_v5(
    *,
    repository: object,
    attempt: ConfirmationAttemptCommitmentV5,
    discovery_manifest_ref: ArtifactRefV5,
    campaign_id: str,
    confirmation_plan_ref: ArtifactRefV5,
    confirmation_plan: ConfirmationPanelPlanV5,
    discovery_plan: CampaignPanelPlanV5,
    preopen_snapshot: StageRetirementSnapshotV5,
) -> ConfirmationAttemptCommitmentV5:
    """Require the typed owner and authenticate the one canonical unopened ledger."""

    if type(attempt) is not ConfirmationAttemptCommitmentV5:
        raise ValueError("confirmation attempt must use the V5 commitment schema")
    if type(preopen_snapshot) is not StageRetirementSnapshotV5:
        raise ValueError("confirmation attempt requires a V5 preopen snapshot")
    (
        stored_plan,
        _manifest,
        stored_discovery,
        _evaluator,
        _execution,
        _sandbox,
        _scenario_grid,
        stored_snapshot,
    ) = _authenticate_confirmation_attempt_dependencies_v5(
        repository=repository,
        attempt=attempt,
        discovery_manifest_ref=discovery_manifest_ref,
        campaign_id=campaign_id,
    )
    if (
        type(confirmation_plan) is not ConfirmationPanelPlanV5
        or type(discovery_plan) is not CampaignPanelPlanV5
        or confirmation_plan_ref != attempt.confirmation_plan_ref
        or stored_plan != confirmation_plan
        or stored_discovery != discovery_plan
        or stored_snapshot != preopen_snapshot
    ):
        raise ValueError("confirmation attempt differs from its typed owner commitments")
    _authenticate_live_unopened_stage_v5(repository=repository, expected=preopen_snapshot)
    return attempt


def qualification_outcome_v5(
    *,
    attempt_ref: ArtifactRefV5,
    status: Literal["completed", "failed", "timed_out", "cancelled"],
    qualified_policy_ref: ArtifactRefV5,
    baseline_evidence_ref: ArtifactRefV5 | None,
    candidate_evidence_ref: ArtifactRefV5 | None,
    target: AnnualizedReturnTargetV5,
    baseline_cagr_pct: Decimal | None,
    candidate_cagr_pct: Decimal | None,
    retirement_terminal_ref: ArtifactRefV5,
    cleanup_evidence_ref: ArtifactRefV5,
) -> QualificationOutcomeV5:
    """Recompute exact-target and strict-baseline qualification gates."""

    completed = status == "completed"
    excess = (
        candidate_cagr_pct - baseline_cagr_pct
        if completed and candidate_cagr_pct is not None and baseline_cagr_pct is not None
        else None
    )
    target_reached = bool(completed and candidate_cagr_pct is not None and candidate_cagr_pct >= target.target_pct)
    baseline_beaten = bool(completed and excess is not None and excess > 0)
    return QualificationOutcomeV5(
        5,
        attempt_ref,
        status,
        qualified_policy_ref,
        baseline_evidence_ref if completed else None,
        candidate_evidence_ref if completed else None,
        target.target_pct,
        baseline_cagr_pct if completed else None,
        candidate_cagr_pct if completed else None,
        excess,
        target_reached,
        baseline_beaten,
        target_reached and baseline_beaten,
        retirement_terminal_ref,
        cleanup_evidence_ref,
        0,
    )


def _load_typed_artifact_v5(repository: object, reference: ArtifactRefV5, value_type: type[object]):
    if type(reference) is not ArtifactRefV5:
        raise ValueError("stage graph contains an absent or invalid typed-artifact reference")
    loader = getattr(repository, "load_typed_artifact", None)
    if not callable(loader):
        raise ValueError("stage graph requires an authenticated typed-artifact loader")
    value = loader(reference, value_type=value_type)
    if type(value) is not value_type or canonical_sha256_v5(value) != reference.sha256:
        raise ValueError("stage graph typed artifact differs from its authenticated reference")
    return value


def _authenticate_reference_v5(repository: object, reference: ArtifactRefV5) -> None:
    authenticate = getattr(repository, "authenticate", None)
    if not callable(authenticate):
        raise ValueError("stage graph requires authenticated artifact resolution")
    authenticate(reference)


def _authenticate_raw_reference_v5(repository: object, reference: ArtifactRefV5) -> None:
    authenticate = getattr(repository, "authenticate_raw_artifact", None)
    if not callable(authenticate):
        raise ValueError("stage graph requires authenticated raw-artifact resolution")
    authenticate(reference)


def _authenticate_live_unopened_stage_v5(
    *,
    repository: object,
    expected: StageRetirementSnapshotV5,
) -> None:
    if type(repository) is LocalArtifactRepositoryV5:
        ledger_path = _existing_stage_ledger_path_v5(
            repository.root,
            expected.ledger_relative_path,
        )
        ledger = QualificationRetirementLedger(
            ledger_path,
            expected.retirement_domain_id,
            create_if_missing=False,
        )
        live = _snapshot_from_ledger_v5(
            stage=expected.stage,
            ledger_relative_path=expected.ledger_relative_path,
            ledger=ledger,
        )
    else:
        loader = getattr(repository, "load_stage_retirement_snapshot", None)
        if not callable(loader):
            raise ValueError("stage graph requires authenticated live-ledger resolution")
        live = loader(
            stage=expected.stage,
            relative_path=expected.ledger_relative_path,
            retirement_domain_id=expected.retirement_domain_id,
        )
    if live != expected or live.record_count != 1 or live.retired_security_lineage_ids:
        raise ValueError("stage ledger differs from its canonical immutable unopened snapshot")


def _authenticate_confirmation_attempt_dependencies_v5(
    *,
    repository: object,
    attempt: ConfirmationAttemptCommitmentV5,
    discovery_manifest_ref: ArtifactRefV5,
    campaign_id: str,
) -> tuple[
    ConfirmationPanelPlanV5,
    CampaignManifestV5,
    CampaignPanelPlanV5,
    EvaluatorContractV5,
    ExecutionProfileV5,
    SandboxProfileV5,
    ScenarioGridV5,
    StageRetirementSnapshotV5,
]:
    """Resolve and cross-bind every dependency before confirmation can open."""

    confirmation_plan = _load_typed_artifact_v5(
        repository,
        attempt.confirmation_plan_ref,
        ConfirmationPanelPlanV5,
    )
    manifest = _load_typed_artifact_v5(
        repository,
        attempt.discovery_manifest_ref,
        CampaignManifestV5,
    )
    discovery_plan = _load_typed_artifact_v5(
        repository,
        manifest.panel_plan_ref,
        CampaignPanelPlanV5,
    )
    evaluator = _load_typed_artifact_v5(
        repository,
        attempt.evaluator_contract_ref,
        EvaluatorContractV5,
    )
    execution = _load_typed_artifact_v5(
        repository,
        attempt.execution_profile_ref,
        ExecutionProfileV5,
    )
    sandbox = _load_typed_artifact_v5(
        repository,
        attempt.sandbox_profile_ref,
        SandboxProfileV5,
    )
    scenario_grid = _load_typed_artifact_v5(
        repository,
        attempt.scenario_grid_ref,
        ScenarioGridV5,
    )
    snapshot = _load_typed_artifact_v5(
        repository,
        attempt.retirement_ledger.preopen_snapshot_ref,
        StageRetirementSnapshotV5,
    )
    baseline = _load_typed_artifact_v5(
        repository,
        attempt.baseline_authority_ref,
        BaselineParentAuthorityV5,
    )
    load_experiment = getattr(repository, "load_experiment", None)
    if not callable(load_experiment):
        raise ValueError("confirmation attempt requires authenticated experiment resolution")
    experiment = load_experiment(attempt.discovery_champion_experiment_ref)
    if type(experiment) is not ExperimentRecordV5:
        raise ValueError("confirmation champion must be an authenticated experiment record")
    load_champion = getattr(repository, "load_frozen_discovery_champion", None)
    if not callable(load_champion):
        raise ValueError("confirmation attempt requires frozen archive resolution")
    champion = load_champion(attempt.discovery_champion_experiment_ref)
    if type(champion) is not ArchiveEntryV5:
        raise ValueError("confirmation champion archive entry is invalid")
    load_panel = getattr(repository, "load_evaluation_panel_spec", None)
    if not callable(load_panel):
        raise ValueError("confirmation attempt requires authenticated panel resolution")
    confirmation_panel = load_panel(confirmation_plan.episode.panel_ref)
    validate_episode_plan_panel_v5(confirmation_plan.episode, confirmation_panel)
    for reference in (
        attempt.discovery_champion_policy_ref,
        manifest.baseline_policy_revision_ref,
        manifest.policy_scope_ref,
    ):
        _authenticate_reference_v5(repository, reference)
    _authenticate_raw_reference_v5(repository, attempt.pit_bundle_ref)
    _authenticate_raw_reference_v5(repository, attempt.prices_provenance_ref)
    validate_campaign_manifest_bindings_v5(
        manifest,
        panel_plan=discovery_plan,
        evaluator_contract=evaluator,
    )
    baseline_parent = baseline_parent_candidate_v5(
        authority=baseline,
        discovery_plan=discovery_plan,
        evaluator_contract=evaluator,
        pit_data_scope=manifest.pit_data_scope,
    )
    if experiment.campaign_evidence is not None and experiment.policy_revision is not None:
        verified_campaign_cagr_pct(
            campaign=experiment.campaign_evidence,
            discovery_plan=discovery_plan,
            evaluator_contract=evaluator,
            policy_identity_sha256=experiment.policy_revision.sha256,
        )
    expected_domain = _stage_domain_id_v5(
        stage="confirmation",
        pit_bundle_ref=discovery_plan.pit_bundle_ref,
        prices_provenance_ref=discovery_plan.prices_provenance_ref,
        ledger_relative_path=CANONICAL_STAGE_LEDGER_PATHS_V5["confirmation"],
    )
    experiment_identity = getattr(experiment, "experiment_identity", None)
    experiment_policy = getattr(experiment, "policy_revision", None)
    if (
        attempt.discovery_manifest_ref != discovery_manifest_ref
        or manifest.campaign_id != campaign_id
        or attempt.discovery_manifest_ref.sha256 != manifest.sha256
        or discovery_plan.confirmation_plan_sha256 != confirmation_plan.sha256
        or attempt.pit_bundle_ref != discovery_plan.pit_bundle_ref
        or attempt.prices_provenance_ref != discovery_plan.prices_provenance_ref
        or attempt.execution_profile_ref != manifest.execution_profile_ref
        or attempt.evaluator_contract_ref != manifest.evaluator_contract_ref
        or attempt.baseline_authority_ref != manifest.baseline_authority_ref
        or manifest.baseline_policy_revision_ref != baseline.policy_revision_ref
        or baseline.policy_revision.sha256 != evaluator.baseline_policy_revision_sha256
        or baseline.source_bundle.sha256 != evaluator.baseline_source_bundle_sha256
        or attempt.sandbox_profile_ref != manifest.sandbox_profile_ref
        or execution.sha256 != evaluator.execution_profile_sha256
        or sandbox.sha256 != evaluator.sandbox_profile_sha256
        or scenario_grid.scenarios != evaluator.friction_grid
        or attempt.scenario_grid_ref.sha256 != scenario_grid.sha256
        or manifest.baseline_policy_revision_ref.sha256
        != evaluator.baseline_policy_revision_sha256
        or confirmation_plan.pit_bundle_sha256 != evaluator.pit_bundle_sha256
        or confirmation_plan.partition_seed_sha256 != discovery_plan.partition_seed_sha256
        or confirmation_plan.target_sha256 != discovery_plan.target_sha256
        or confirmation_plan.confirmation_retirement_domain_id != expected_domain
        or attempt.retirement_domain_id != expected_domain
        or attempt.retirement_ledger.relative_path
        != CANONICAL_STAGE_LEDGER_PATHS_V5["confirmation"]
        or snapshot.stage != "confirmation"
        or snapshot.ledger_relative_path != attempt.retirement_ledger.relative_path
        or snapshot.retirement_domain_id != expected_domain
        or snapshot.ledger_head_sha256
        != confirmation_plan.confirmation_ledger_snapshot_sha256
        or attempt.retirement_ledger.preopen_snapshot_ref.sha256
        != canonical_sha256_v5(snapshot)
        or getattr(experiment_identity, "discovery_plan_sha256", None)
        != discovery_plan.discovery_plan_sha256
        or getattr(experiment_identity, "policy_revision_sha256", None)
        != attempt.discovery_champion_policy_ref.sha256
        or getattr(experiment_policy, "sha256", None)
        != attempt.discovery_champion_policy_ref.sha256
        or experiment.status != "evaluated"
        or experiment.campaign_evidence is None
        or experiment.policy_revision is None
        or champion.experiment_record_ref != attempt.discovery_champion_experiment_ref
        or champion.policy_revision != experiment.policy_revision
        or champion.campaign != experiment.campaign_evidence
        or champion.policy_identity_sha256 != attempt.discovery_champion_policy_ref.sha256
        or champion.campaign_cagr_pct <= baseline_parent.campaign_cagr_pct
    ):
        raise ValueError("confirmation attempt dependency graph is inconsistent")
    return (
        confirmation_plan,
        manifest,
        discovery_plan,
        evaluator,
        execution,
        sandbox,
        scenario_grid,
        snapshot,
    )


def _confirmation_cagr_from_evidence_v5(evaluation: PanelEvaluationV5) -> Decimal:
    from core.pit_optimizer_v5.search import annualized_return_pct

    scenario = selected_scenario(evaluation)
    recomputed = annualized_return_pct(
        starting_equity=scenario.starting_equity,
        ending_equity=scenario.ending_equity,
        days=evaluation.elapsed_calendar_days,
    )
    if recomputed != scenario.report.portfolio_annualized_return_pct:
        raise ValueError("confirmation CAGR report differs from authenticated equity evidence")
    return recomputed


def _confirmation_graph_v5(
    *,
    repository: object,
    confirmation_outcome_ref: ArtifactRefV5,
) -> tuple[
    ConfirmationOutcomeV5,
    ConfirmationAttemptCommitmentV5,
    CampaignPanelPlanV5,
    ConfirmationPanelPlanV5,
]:
    outcome = _load_typed_artifact_v5(repository, confirmation_outcome_ref, ConfirmationOutcomeV5)
    if confirmation_outcome_ref.sha256 != outcome.sha256:
        raise ValueError("confirmation outcome authentication failed")
    if (
        outcome.status != "completed"
        or type(outcome.baseline_evidence_ref) is not ArtifactRefV5
        or type(outcome.candidate_evidence_ref) is not ArtifactRefV5
    ):
        raise ValueError("qualification requires completed confirmation evaluation evidence")
    attempt = _load_typed_artifact_v5(repository, outcome.attempt_ref, ConfirmationAttemptCommitmentV5)
    confirmation_plan = _load_typed_artifact_v5(
        repository,
        attempt.confirmation_plan_ref,
        ConfirmationPanelPlanV5,
    )
    manifest = _load_typed_artifact_v5(repository, attempt.discovery_manifest_ref, CampaignManifestV5)
    discovery_plan = _load_typed_artifact_v5(repository, manifest.panel_plan_ref, CampaignPanelPlanV5)
    evaluator = _load_typed_artifact_v5(repository, attempt.evaluator_contract_ref, EvaluatorContractV5)
    execution = _load_typed_artifact_v5(repository, attempt.execution_profile_ref, ExecutionProfileV5)
    sandbox = _load_typed_artifact_v5(repository, attempt.sandbox_profile_ref, SandboxProfileV5)
    scenario_grid = _load_typed_artifact_v5(repository, attempt.scenario_grid_ref, ScenarioGridV5)
    snapshot = _load_typed_artifact_v5(
        repository,
        attempt.retirement_ledger.preopen_snapshot_ref,
        StageRetirementSnapshotV5,
    )
    baseline = _load_typed_artifact_v5(repository, outcome.baseline_evidence_ref, PanelEvaluationV5)
    candidate = _load_typed_artifact_v5(repository, outcome.candidate_evidence_ref, PanelEvaluationV5)
    load_experiment = getattr(repository, "load_experiment", None)
    if not callable(load_experiment):
        raise ValueError("confirmation graph requires authenticated experiment resolution")
    experiment = load_experiment(attempt.discovery_champion_experiment_ref)
    load_panel = getattr(repository, "load_evaluation_panel_spec", None)
    if not callable(load_panel):
        raise ValueError("confirmation graph requires authenticated panel resolution")
    confirmation_panel = load_panel(confirmation_plan.episode.panel_ref)
    validate_episode_plan_panel_v5(confirmation_plan.episode, confirmation_panel)
    for reference in (
        attempt.discovery_champion_policy_ref,
        attempt.baseline_authority_ref,
        manifest.baseline_policy_revision_ref,
        manifest.policy_scope_ref,
        outcome.retirement_terminal_ref,
        outcome.cleanup_evidence_ref,
    ):
        _authenticate_reference_v5(repository, reference)
    _authenticate_raw_reference_v5(repository, attempt.pit_bundle_ref)
    _authenticate_raw_reference_v5(repository, attempt.prices_provenance_ref)
    validate_campaign_manifest_bindings_v5(
        manifest,
        panel_plan=discovery_plan,
        evaluator_contract=evaluator,
    )
    expected_domain = _stage_domain_id_v5(
        stage="confirmation",
        pit_bundle_ref=discovery_plan.pit_bundle_ref,
        prices_provenance_ref=discovery_plan.prices_provenance_ref,
        ledger_relative_path=CANONICAL_STAGE_LEDGER_PATHS_V5["confirmation"],
    )
    expected_scenarios = tuple(item.scenario_id for item in evaluator.friction_grid)
    experiment_identity = getattr(experiment, "experiment_identity", None)
    experiment_policy = getattr(experiment, "policy_revision", None)
    if (
        attempt.discovery_manifest_ref.sha256 != manifest.sha256
        or attempt.confirmation_plan_ref.sha256 != confirmation_plan.sha256
        or discovery_plan.confirmation_plan_sha256 != confirmation_plan.sha256
        or attempt.pit_bundle_ref != discovery_plan.pit_bundle_ref
        or attempt.prices_provenance_ref != discovery_plan.prices_provenance_ref
        or attempt.execution_profile_ref != manifest.execution_profile_ref
        or attempt.evaluator_contract_ref != manifest.evaluator_contract_ref
        or attempt.baseline_authority_ref != manifest.baseline_authority_ref
        or attempt.sandbox_profile_ref != manifest.sandbox_profile_ref
        or execution.sha256 != evaluator.execution_profile_sha256
        or sandbox.sha256 != evaluator.sandbox_profile_sha256
        or scenario_grid.scenarios != evaluator.friction_grid
        or attempt.scenario_grid_ref.sha256 != scenario_grid.sha256
        or manifest.baseline_policy_revision_ref.sha256 != evaluator.baseline_policy_revision_sha256
        or confirmation_plan.pit_bundle_sha256 != evaluator.pit_bundle_sha256
        or confirmation_plan.partition_seed_sha256 != discovery_plan.partition_seed_sha256
        or confirmation_plan.target_sha256 != discovery_plan.target_sha256
        or confirmation_plan.confirmation_retirement_domain_id != expected_domain
        or attempt.retirement_domain_id != expected_domain
        or attempt.retirement_ledger.relative_path != CANONICAL_STAGE_LEDGER_PATHS_V5["confirmation"]
        or snapshot.stage != "confirmation"
        or snapshot.ledger_relative_path != attempt.retirement_ledger.relative_path
        or snapshot.retirement_domain_id != expected_domain
        or snapshot.ledger_head_sha256 != confirmation_plan.confirmation_ledger_snapshot_sha256
        or attempt.retirement_ledger.preopen_snapshot_ref.sha256 != canonical_sha256_v5(snapshot)
        or getattr(experiment_identity, "discovery_plan_sha256", None)
        != discovery_plan.discovery_plan_sha256
        or getattr(experiment_identity, "policy_revision_sha256", None)
        != attempt.discovery_champion_policy_ref.sha256
        or getattr(experiment_policy, "sha256", None) != attempt.discovery_champion_policy_ref.sha256
    ):
        raise ValueError("confirmation outcome ancestry differs from the discovery campaign")
    for evaluation, policy_sha256 in (
        (baseline, evaluator.baseline_policy_revision_sha256),
        (candidate, attempt.discovery_champion_policy_ref.sha256),
    ):
        if (
            evaluation.panel_sha256 != confirmation_plan.episode.panel_ref.sha256
            or evaluation.start_date != confirmation_plan.episode.start_date
            or evaluation.end_date != confirmation_plan.episode.end_date
            or evaluation.evaluator_contract_sha256 != evaluator.sha256
            or evaluation.sandbox_profile_sha256 != sandbox.sha256
            or evaluation.policy_identity_sha256 != policy_sha256
            or evaluation.selection_scenario_id != evaluator.selection_scenario_id
            or tuple(item.scenario_id for item in evaluation.scenarios) != expected_scenarios
        ):
            raise ValueError("confirmation evaluation differs from its attempt dependencies")
    baseline_cagr = _confirmation_cagr_from_evidence_v5(baseline)
    candidate_cagr = _confirmation_cagr_from_evidence_v5(candidate)
    behaviorally_active = selected_scenario(candidate).report.closed_trades != 0
    recomputed = confirmation_outcome_v5(
        attempt_ref=outcome.attempt_ref,
        status=outcome.status,
        confirmed_policy_ref=attempt.discovery_champion_policy_ref,
        baseline_evidence_ref=outcome.baseline_evidence_ref,
        candidate_evidence_ref=outcome.candidate_evidence_ref,
        baseline_cagr_pct=baseline_cagr,
        candidate_cagr_pct=candidate_cagr,
        behaviorally_active=behaviorally_active,
        retirement_terminal_ref=outcome.retirement_terminal_ref,
        cleanup_evidence_ref=outcome.cleanup_evidence_ref,
    )
    if recomputed != outcome:
        raise ValueError("confirmation outcome gate differs from authenticated evaluation evidence")
    return outcome, attempt, discovery_plan, confirmation_plan


def qualification_attempt_commitment_v5(
    *,
    repository: object,
    confirmation_outcome_ref: ArtifactRefV5,
    operator_approved: bool,
    attempt: QualificationAttemptCommitmentV5,
) -> QualificationAttemptCommitmentV5:
    """Seal qualification only from a fully authenticated eligible confirmation graph."""

    if type(operator_approved) is not bool or not operator_approved:
        raise ValueError("qualification requires a separate affirmative operator decision")
    if type(attempt) is not QualificationAttemptCommitmentV5:
        raise ValueError("qualification attempt must use the V5 commitment schema")
    outcome, confirmation_attempt, discovery_plan, _confirmation_plan = _confirmation_graph_v5(
        repository=repository,
        confirmation_outcome_ref=confirmation_outcome_ref,
    )
    qualification_plan = _load_typed_artifact_v5(
        repository,
        attempt.qualification_plan_ref,
        QualificationPanelPlanV5,
    )
    snapshot = _load_typed_artifact_v5(
        repository,
        attempt.retirement_ledger.preopen_snapshot_ref,
        StageRetirementSnapshotV5,
    )
    expected_domain = _stage_domain_id_v5(
        stage="qualification",
        pit_bundle_ref=discovery_plan.pit_bundle_ref,
        prices_provenance_ref=discovery_plan.prices_provenance_ref,
        ledger_relative_path=CANONICAL_STAGE_LEDGER_PATHS_V5["qualification"],
    )
    if (
        not outcome.eligible_to_request_qualification
        or attempt.confirmation_outcome_ref != confirmation_outcome_ref
        or attempt.confirmed_policy_ref != outcome.confirmed_policy_ref
        or attempt.confirmed_policy_ref != confirmation_attempt.discovery_champion_policy_ref
        or attempt.qualification_plan_ref.sha256 != qualification_plan.sha256
        or discovery_plan.qualification_plan_sha256 != qualification_plan.sha256
        or attempt.pit_bundle_ref != confirmation_attempt.pit_bundle_ref
        or attempt.prices_provenance_ref != confirmation_attempt.prices_provenance_ref
        or attempt.execution_profile_ref != confirmation_attempt.execution_profile_ref
        or attempt.evaluator_contract_ref != confirmation_attempt.evaluator_contract_ref
        or attempt.scenario_grid_ref != confirmation_attempt.scenario_grid_ref
        or attempt.baseline_authority_ref != confirmation_attempt.baseline_authority_ref
        or attempt.sandbox_profile_ref != confirmation_attempt.sandbox_profile_ref
        or qualification_plan.pit_bundle_sha256 != discovery_plan.pit_bundle_ref.sha256
        or qualification_plan.partition_seed_sha256 != discovery_plan.partition_seed_sha256
        or qualification_plan.target.sha256 != discovery_plan.target_sha256
        or attempt.retirement_domain_id != expected_domain
        or qualification_plan.qualification_retirement_domain_id != expected_domain
        or attempt.retirement_ledger.relative_path != CANONICAL_STAGE_LEDGER_PATHS_V5["qualification"]
        or snapshot.stage != "qualification"
        or snapshot.ledger_relative_path != attempt.retirement_ledger.relative_path
        or snapshot.retirement_domain_id != expected_domain
        or snapshot.ledger_head_sha256 != qualification_plan.qualification_ledger_snapshot_sha256
        or attempt.retirement_ledger.preopen_snapshot_ref.sha256 != canonical_sha256_v5(snapshot)
    ):
        raise ValueError("authenticated confirmation evidence does not permit qualification")
    _authenticate_live_unopened_stage_v5(repository=repository, expected=snapshot)
    return attempt


__all__ = [
    "AFFILIATION_BITSET_ORDER_V5",
    "ANNUAL_HIGH_HISTORY_ROWS_V5",
    "AVERAGE_DOLLAR_VOLUME_HISTORY_ROWS_V5",
    "BuiltPanelPlansV5",
    "CANONICAL_STAGE_LEDGER_PATHS_V5",
    "COHORT_COUNTS_V5",
    "StageRetirementSnapshotV5",
    "allocate_panel_cohorts_v5",
    "build_panels_v5",
    "compose_panel_plans_v5",
    "confirmation_attempt_commitment_v5",
    "confirmation_outcome_v5",
    "discovery_evidence_projection_v5",
    "initialize_stage_ledgers_v5",
    "lineage_has_causal_feature_coverage_v5",
    "load_bundle_panel_authority_v5",
    "qualification_attempt_commitment_v5",
    "qualification_outcome_v5",
    "validate_owned_panel_plans_v5",
    "verify_panels_v5",
]
