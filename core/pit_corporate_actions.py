"""Causal validation and projection for source-bound corporate-action facts.

This module keeps action records descriptive. It does not adjust prices, returns,
cash flows, membership, or feature snapshots. Callers must supply lineages already
bound by an accepted identity contract and the exchange-session calendar used for
the requested historical decision.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType

_EVENT_FACTS: dict[str, tuple[str, ...]] = {
    "split": ("split_numerator", "split_denominator", "ex_date"),
    "cash_distribution": ("cash_amount", "cash_currency", "cash_basis", "ex_date", "pay_date"),
    "merger": ("successor_lineage_id", "exchange_ratio", "cash_component_amount", "cash_component_currency"),
    "delisting": ("terminal_date", "proceeds_amount", "proceeds_currency"),
    "liquidation": ("terminal_date", "proceeds_amount", "proceeds_currency"),
}
_ROLES = frozenset({"subject", "predecessor", "successor"})
_STATUSES = frozenset({"known", "not_reported", "source_conflict", "lineage_unresolved", "unsupported"})
_LINEAGE_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_SOURCE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TICKER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.-]{0,14}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_BASIS_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_RECORD_FIELDS = frozenset({
    "source_name", "source_event_id", "source_revision_id", "source_reference",
    "security_lineage_id", "observed_ticker", "affected_role", "event_type",
    "public_at", "revision_public_at", "effective_date", "facts", "field_status",
})
_COMMON_FIELDS = ("public_at", "revision_public_at", "effective_date")


@dataclass(frozen=True, slots=True)
class ActionFieldStatus:
    status: str
    reason: str | None


@dataclass(frozen=True, slots=True)
class CorporateActionRevision:
    source_name: str
    source_event_id: str
    source_revision_id: str
    source_reference: str
    security_lineage_id: str
    observed_ticker: str | None
    affected_role: str
    event_type: str
    public_at: str | None
    revision_public_at: str | None
    effective_date: str | None
    facts: Mapping[str, object]
    field_status: Mapping[str, ActionFieldStatus]

    @property
    def primary_key(self) -> tuple[str, str, str, str, str]:
        return (
            self.source_name, self.source_event_id, self.source_revision_id,
            self.security_lineage_id, self.affected_role,
        )

    @property
    def event_key(self) -> tuple[str, str, str, str]:
        return self.source_name, self.source_event_id, self.security_lineage_id, self.affected_role

    def to_row(self) -> dict[str, object]:
        """Return the canonical JSON-compatible row while retaining raw date precision."""
        return {
            "source_name": self.source_name,
            "source_event_id": self.source_event_id,
            "source_revision_id": self.source_revision_id,
            "source_reference": self.source_reference,
            "security_lineage_id": self.security_lineage_id,
            "observed_ticker": self.observed_ticker,
            "affected_role": self.affected_role,
            "event_type": self.event_type,
            "public_at": self.public_at,
            "revision_public_at": self.revision_public_at,
            "effective_date": self.effective_date,
            "facts": dict(self.facts),
            "field_status": {
                name: {"status": status.status, "reason": status.reason}
                for name, status in self.field_status.items()
            },
        }


@dataclass(frozen=True, slots=True)
class ValidationFinding:
    row_index: int
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class CorporateActionValidation:
    records: tuple[CorporateActionRevision, ...]
    findings: tuple[ValidationFinding, ...]


@dataclass(frozen=True, slots=True)
class ProjectionFinding:
    source_name: str
    source_event_id: str
    security_lineage_id: str
    code: str


@dataclass(frozen=True, slots=True)
class CorporateActionProjection:
    records: tuple[CorporateActionRevision, ...]
    findings: tuple[ProjectionFinding, ...]


class _InvalidRecord(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _invalid(code: str, message: str) -> None:
    raise _InvalidRecord(code, message)


def _lineage(value: object, *, code: str = "invalid_lineage") -> str:
    if not isinstance(value, str) or _LINEAGE_RE.fullmatch(value) is None:
        _invalid(code, "security lineage ID is not canonical")
    return value


def _date_only(value: object, name: str) -> str:
    if not isinstance(value, str):
        _invalid("invalid_date", f"{name} must be a canonical date string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        _invalid("invalid_date", f"{name} is not a valid date")
    if parsed.isoformat() != value:
        _invalid("invalid_date", f"{name} is not a canonical date")
    return value


def _public_time(value: object, name: str) -> date | datetime:
    if not isinstance(value, str):
        _invalid("invalid_timestamp", f"{name} must preserve its supplied date or timestamp")
    if len(value) == 10:
        try:
            parsed_date = date.fromisoformat(value)
        except ValueError:
            _invalid("invalid_timestamp", f"{name} is invalid")
        if parsed_date.isoformat() != value:
            _invalid("invalid_timestamp", f"{name} is not canonical")
        return parsed_date
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        _invalid("invalid_timestamp", f"{name} is invalid")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _invalid("invalid_timestamp", f"{name} timestamp must include its supplied timezone")
    return parsed


def _decimal(value: object, name: str, *, positive: bool = False) -> None:
    if not isinstance(value, str):
        _invalid("invalid_decimal", f"{name} must be decimal text")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        _invalid("invalid_decimal", f"{name} is invalid")
    if not parsed.is_finite() or (parsed <= 0 if positive else parsed < 0):
        _invalid("invalid_decimal", f"{name} must be finite and nonnegative")


def _parse_status(raw: object, name: str) -> ActionFieldStatus:
    if not isinstance(raw, Mapping) or set(raw) != {"status", "reason"}:
        _invalid("invalid_field_status", f"{name} status shape is invalid")
    status, reason = raw["status"], raw["reason"]
    if not isinstance(status, str) or status not in _STATUSES:
        _invalid("invalid_field_status", f"{name} status is unsupported")
    if status == "known":
        if reason is not None:
            _invalid("known_value_has_reason", f"{name} known status cannot carry an unknown reason")
    elif not isinstance(reason, str) or not reason.strip():
        _invalid("unknown_value_missing_reason", f"{name} unknown status requires a reason")
    return ActionFieldStatus(status=status, reason=reason)


def _parse_record(
    row: object,
    *,
    authenticated_security_lineages: frozenset[str],
) -> CorporateActionRevision:
    if not isinstance(row, Mapping) or set(row) != _RECORD_FIELDS:
        _invalid("invalid_record_shape", "corporate-action record fields do not match the sidecar contract")
    event_type = row["event_type"]
    if not isinstance(event_type, str) or event_type not in _EVENT_FACTS:
        _invalid("event_type_not_supported", "event type is outside the closed action domain")
    if not isinstance(row["source_name"], str) or _SOURCE_RE.fullmatch(row["source_name"]) is None:
        _invalid("invalid_source_name", "source_name is not canonical")
    for field in ("source_event_id", "source_revision_id"):
        if not isinstance(row[field], str) or _ID_RE.fullmatch(row[field]) is None:
            _invalid("invalid_source_identity", f"{field} must be a stable nonempty identifier")
    if (
        not isinstance(row["source_reference"], str)
        or not row["source_reference"].strip()
        or any(ord(char) < 32 for char in row["source_reference"])
    ):
        _invalid("invalid_source_reference", "source_reference must identify the supporting source document")
    lineage = _lineage(row["security_lineage_id"])
    if lineage not in authenticated_security_lineages:
        _invalid("lineage_unverified", "security_lineage_id is not in the caller's authenticated lineage set")
    ticker = row["observed_ticker"]
    if ticker is not None and (not isinstance(ticker, str) or _TICKER_RE.fullmatch(ticker) is None):
        _invalid("invalid_observed_ticker", "observed_ticker is not a valid source alias")
    role = row["affected_role"]
    if not isinstance(role, str) or role not in _ROLES:
        _invalid("invalid_affected_role", "affected_role is outside the closed role domain")

    facts_raw = row["facts"]
    expected_facts = set(_EVENT_FACTS[event_type])
    if not isinstance(facts_raw, Mapping) or set(facts_raw) != expected_facts:
        _invalid("invalid_fact_shape", "event facts do not match the event-type contract")
    facts = dict(facts_raw)
    for field in _COMMON_FIELDS:
        value = row[field]
        if value is not None:
            if field == "effective_date":
                _date_only(value, field)
            else:
                _public_time(value, field)
    for field in ("ex_date", "pay_date", "terminal_date"):
        if field in facts and facts[field] is not None:
            _date_only(facts[field], field)
    statuses_raw = row["field_status"]
    expected_statuses = set(_COMMON_FIELDS) | expected_facts
    if not isinstance(statuses_raw, Mapping) or set(statuses_raw) != expected_statuses:
        _invalid("invalid_field_status", "every represented fact must carry exactly one status")
    statuses = {name: _parse_status(statuses_raw[name], name) for name in expected_statuses}
    values = {name: row[name] for name in _COMMON_FIELDS}
    values.update(facts)
    for name, status in statuses.items():
        value = values[name]
        if status.status == "known" and value is None:
            _invalid("known_value_missing", f"{name} is marked known without a value")
        if status.status != "known" and value is not None:
            _invalid("unknown_value_not_null", f"{name} has a value while its status is unknown")

    for name, value in facts.items():
        status = statuses[name]
        if status.status != "known":
            continue
        if name in {"split_numerator", "split_denominator"}:
            if type(value) is not int or value <= 0:
                _invalid("invalid_split_ratio", "split numerator and denominator must be positive integers")
        elif name in {"cash_amount", "cash_component_amount", "proceeds_amount"}:
            _decimal(value, name)
        elif name == "exchange_ratio":
            _decimal(value, name, positive=True)
        elif name in {"cash_currency", "cash_component_currency", "proceeds_currency"}:
            if not isinstance(value, str) or _CURRENCY_RE.fullmatch(value) is None:
                _invalid("invalid_currency", f"{name} must be a three-letter uppercase currency code")
        elif name == "cash_basis":
            if not isinstance(value, str) or _BASIS_RE.fullmatch(value) is None:
                _invalid("invalid_cash_basis", "cash_basis must be a stated canonical basis")
        elif name == "successor_lineage_id":
            successor = _lineage(value, code="invalid_successor_lineage")
            if successor not in authenticated_security_lineages:
                _invalid("successor_lineage_unverified", "successor lineage is not authenticated by the caller")
            if role == "successor" and successor != lineage:
                _invalid("successor_lineage_role_mismatch", "successor-role row must bind to its own lineage")

    if event_type == "cash_distribution" and statuses["cash_amount"].status == "known":
        if statuses["cash_currency"].status != "known" or statuses["cash_basis"].status != "known":
            _invalid("cash_fact_incomplete", "a known cash amount requires its currency and stated basis")
    if event_type == "merger" and statuses["cash_component_amount"].status == "known":
        if statuses["cash_component_currency"].status != "known":
            _invalid("merger_cash_fact_incomplete", "known merger cash requires its currency")
    if event_type in {"delisting", "liquidation"} and statuses["proceeds_amount"].status == "known":
        if statuses["proceeds_currency"].status != "known":
            _invalid("terminal_proceeds_incomplete", "known terminal proceeds require their currency")

    return CorporateActionRevision(
        source_name=row["source_name"],
        source_event_id=row["source_event_id"],
        source_revision_id=row["source_revision_id"],
        source_reference=row["source_reference"],
        security_lineage_id=lineage,
        observed_ticker=ticker,
        affected_role=role,
        event_type=event_type,
        public_at=row["public_at"],
        revision_public_at=row["revision_public_at"],
        effective_date=row["effective_date"],
        facts=MappingProxyType(facts),
        field_status=MappingProxyType(statuses),
    )


def validate_corporate_action_rows(
    rows: Iterable[Mapping[str, object]],
    *,
    authenticated_security_lineages: Iterable[str],
) -> CorporateActionValidation:
    """Validate rows independently and reject every member of a duplicate-key group.

    The lineage collection must come from an already-authenticated identity binding;
    this sidecar validator checks membership in that set but does not authenticate it.
    """
    if isinstance(authenticated_security_lineages, (str, bytes)):
        raise ValueError("authenticated_security_lineages must be a collection of lineage IDs")
    lineages = frozenset(authenticated_security_lineages)
    for lineage in lineages:
        _lineage(lineage)
    candidates: list[tuple[int, CorporateActionRevision]] = []
    findings: list[ValidationFinding] = []
    for index, row in enumerate(rows):
        try:
            record = _parse_record(row, authenticated_security_lineages=lineages)
        except _InvalidRecord as exc:
            findings.append(ValidationFinding(index, exc.code, str(exc)))
        else:
            candidates.append((index, record))
    counts = Counter(record.primary_key for _, record in candidates)
    duplicate_keys = {key for key, count in counts.items() if count > 1}
    records = []
    for index, record in candidates:
        if record.primary_key in duplicate_keys:
            findings.append(ValidationFinding(index, "duplicate_primary_key", "primary key appears more than once"))
        else:
            records.append(record)
    return CorporateActionValidation(
        records=tuple(sorted(records, key=lambda item: item.primary_key)),
        findings=tuple(sorted(findings, key=lambda item: (item.row_index, item.code))),
    )


def _decision_date(value: object, name: str) -> date:
    if type(value) is date:
        return value
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise ValueError(f"{name} must be a canonical exchange-session date") from None
        if parsed.isoformat() == value:
            return parsed
    raise ValueError(f"{name} must be a canonical exchange-session date")


def _public_date(value: str) -> date:
    parsed = _public_time(value, "revision_public_at")
    return parsed.date() if isinstance(parsed, datetime) else parsed


def _latest_revision(
    records: list[CorporateActionRevision],
) -> tuple[CorporateActionRevision | None, bool]:
    latest_date = max(
        _public_date(item.revision_public_at)
        for item in records
        if item.revision_public_at is not None
    )
    same_day = [
        item
        for item in records
        if item.revision_public_at is not None
        and _public_date(item.revision_public_at) == latest_date
    ]
    if len(same_day) == 1:
        return same_day[0], False
    parsed: list[tuple[datetime, CorporateActionRevision]] = []
    for item in same_day:
        when = _public_time(item.revision_public_at, "revision_public_at")
        if not isinstance(when, datetime):
            return None, True
        parsed.append((when.astimezone(timezone.utc), item))
    parsed.sort(key=lambda pair: pair[0])
    if len(parsed) > 1 and parsed[-1][0] == parsed[-2][0]:
        return None, True
    return parsed[-1][1], False


def project_corporate_actions_as_of(
    records: Iterable[CorporateActionRevision],
    *,
    decision_session: date | str,
    exchange_sessions: Iterable[date | str],
    security_lineage_id: str,
) -> CorporateActionProjection:
    """Project the latest revision public by the decision session for one lineage.

    A revision is first eligible on the first supplied exchange session strictly
    after its source-public local date. Effective dates are retained as facts and
    never cause an adjustment or other economic effect here.
    """
    decision = _decision_date(decision_session, "decision_session")
    sessions = tuple(_decision_date(value, "exchange session") for value in exchange_sessions)
    if not sessions or any(later <= earlier for earlier, later in zip(sessions, sessions[1:], strict=False)):
        raise ValueError("exchange_sessions must be nonempty, unique, and increasing")
    if decision not in sessions:
        raise ValueError("decision_session must be an exchange session")
    lineage = _lineage(security_lineage_id)
    grouped: dict[tuple[str, str, str, str], list[CorporateActionRevision]] = defaultdict(list)
    findings: list[ProjectionFinding] = []
    for record in records:
        if not isinstance(record, CorporateActionRevision):
            raise ValueError("projection records must be validated corporate-action revisions")
        if record.security_lineage_id != lineage:
            continue
        status = record.field_status["revision_public_at"]
        if status.status != "known" or record.revision_public_at is None:
            findings.append(
                ProjectionFinding(
                    record.source_name,
                    record.source_event_id,
                    lineage,
                    "revision_public_date_unknown",
                )
            )
            continue
        public_date = _public_date(record.revision_public_at)
        available = next((session for session in sessions if session > public_date), None)
        if available is None:
            if decision > public_date:
                findings.append(
                    ProjectionFinding(
                        record.source_name,
                        record.source_event_id,
                        lineage,
                        "session_calendar_incomplete",
                    )
                )
            continue
        if available <= decision:
            grouped[record.event_key].append(record)

    projected: list[CorporateActionRevision] = []
    for key, visible in grouped.items():
        latest, ambiguous = _latest_revision(visible)
        if ambiguous:
            findings.append(ProjectionFinding(key[0], key[1], key[2], "ambiguous_revision_order"))
        elif latest is not None:
            projected.append(latest)
    unique_findings = {
        (item.source_name, item.source_event_id, item.security_lineage_id, item.code): item
        for item in findings
    }
    return CorporateActionProjection(
        records=tuple(sorted(projected, key=lambda item: item.primary_key)),
        findings=tuple(unique_findings[key] for key in sorted(unique_findings)),
    )


__all__ = [
    "ActionFieldStatus",
    "CorporateActionProjection",
    "CorporateActionRevision",
    "CorporateActionValidation",
    "ProjectionFinding",
    "ValidationFinding",
    "project_corporate_actions_as_of",
    "validate_corporate_action_rows",
]
