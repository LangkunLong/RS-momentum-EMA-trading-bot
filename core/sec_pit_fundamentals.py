"""Normalize SEC bulk archives into point-in-time CANSLIM fundamentals.

The module is deliberately acquisition-provider agnostic: callers supply the
two official SEC ZIP archives after download.  Archive publication and CLI
orchestration live outside this module.  Every mapping is exact or backed by
the repository's reviewed issuer-identity manifest; fuzzy issuer matching is
not permitted.
"""

from __future__ import annotations

import bisect
import csv
import hashlib
import json
import math
import os
import re
import stat
import unicodedata
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence


FUNDAMENTAL_COLUMNS = (
    "ticker",
    "statement_type",
    "period_end",
    "public_date",
    "basic_eps",
    "diluted_eps",
    "total_revenue",
    "net_income",
    "common_stock",
    "total_stockholders_equity",
    "shares_outstanding",
    "held_percent_institutions",
    "institution_count",
    "prev_institution_count",
)
SECURITY_MASTER_COLUMNS = (
    "ticker",
    "cik",
    "company_name",
    "first_membership_date",
    "last_membership_date",
    "mapping_basis",
)
SECURITY_MASTER_EXCLUSION_COLUMNS = (
    "ticker",
    "company_name",
    "first_membership_date",
    "last_membership_date",
    "reason",
    "details",
)
FUNDAMENTAL_AUDIT_COLUMNS = (
    "ticker",
    "statement_type",
    "period_end",
    "public_date",
    "accession_number",
    "form",
    "filed_date",
    "fiscal_year",
    "fiscal_period",
    "acceptance_datetime",
    "public_date_basis",
    "source_concepts",
    "inherited_metrics",
    "metric_sources",
)

_MEMBERSHIP_COLUMNS = ("effective_date", "ticker", "member")
_SECURITY_NAME_COLUMNS = ("ticker", "company_name")
_IDENTITY_COLUMNS = (
    "canonical_ticker",
    "provider_symbol",
    "identity_asof",
    "admitted_start",
    "admitted_end",
    "chain_id",
    "continuity_kind",
    "warmup_predecessor",
    "factor_anchor",
    "evidence_url",
)
_CIK_MEMBER = re.compile(r"(?:^|/)CIK(?P<cik>\d{10})\.json$")
_ACCESSION = re.compile(r"^\d{18}$")
_TICKER = re.compile(r"^[A-Z][A-Z0-9.-]{0,7}$")
_KNOWN_SEC_ALIASES = {"BF.B": "BF-B", "BRK.B": "BRK-B"}
_INCOME_CONCEPTS = {
    "basic_eps": ("us-gaap", "EarningsPerShareBasic", "USD/shares"),
    "diluted_eps": ("us-gaap", "EarningsPerShareDiluted", "USD/shares"),
    "net_income": ("us-gaap", "NetIncomeLoss", "USD"),
}
_REVENUE_PRIORITY = (
    ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax", "USD"),
    ("us-gaap", "Revenues", "USD"),
)
_BALANCE_CONCEPTS = {
    "common_stock": ("us-gaap", "CommonStockValue", "USD"),
    "total_stockholders_equity": ("us-gaap", "StockholdersEquity", "USD"),
    "shares_outstanding": ("dei", "EntityCommonStockSharesOutstanding", "shares"),
}
_NUMERIC_COLUMNS = FUNDAMENTAL_COLUMNS[4:11]
_REVIEWED_BASELINE_CIKS = {
    "VIAC": "0000813828",
    "PARA": "0000813828",
    "PSKY": "0002041610",
    "FISV": "0000798354",
    "FI": "0000798354",
    "DOC": "0000765880",
    "CTRA": "0000858470",
    "XOM": "0000034088",
}
_SAME_ISSUER_KINDS = {
    "same_issuer_rename",
    "same_issuer_ticker_reuse",
    "accounting_acquirer_rename",
    "legacy_survivor_rename",
}
_IDENTITY_KINDS = {"standalone", "historical_identity", "successor_reset", *_SAME_ISSUER_KINDS}


@dataclass(frozen=True)
class SecurityMasterRow:
    ticker: str
    cik: str
    company_name: str
    first_membership_date: date
    last_membership_date: date
    mapping_basis: str


@dataclass(frozen=True)
class SecurityMasterExclusion:
    ticker: str
    company_name: str
    first_membership_date: date
    last_membership_date: date
    reason: str
    details: str


@dataclass(frozen=True)
class FilingAcceptance:
    accession_number: str
    form: str
    filed_date: date
    acceptance_datetime: datetime


@dataclass(frozen=True)
class SecurityMasterResult:
    rows: tuple[SecurityMasterRow, ...]
    exclusions: tuple[SecurityMasterExclusion, ...]
    acceptance_by_cik: Mapping[str, Mapping[str, FilingAcceptance]]
    membership_union: tuple[str, ...]
    identity_manifest_sha256: str
    submissions_archive_sha256: str
    missing_submission_fragments: int

    def __post_init__(self) -> None:
        frozen = {
            cik: MappingProxyType(dict(values))
            for cik, values in self.acceptance_by_cik.items()
        }
        object.__setattr__(self, "acceptance_by_cik", MappingProxyType(frozen))


@dataclass(frozen=True)
class FundamentalRow:
    ticker: str
    statement_type: str
    period_end: date
    public_date: date
    basic_eps: float | None = None
    diluted_eps: float | None = None
    total_revenue: float | None = None
    net_income: float | None = None
    common_stock: float | None = None
    total_stockholders_equity: float | None = None
    shares_outstanding: float | None = None


@dataclass(frozen=True)
class FundamentalAuditRow:
    ticker: str
    statement_type: str
    period_end: date
    public_date: date
    accession_number: str
    form: str
    filed_date: date
    fiscal_year: str
    fiscal_period: str
    acceptance_datetime: str
    public_date_basis: str
    source_concepts: str
    inherited_metrics: str
    metric_sources: str


@dataclass(frozen=True)
class FundamentalExportResult:
    rows: tuple[FundamentalRow, ...]
    audit_rows: tuple[FundamentalAuditRow, ...]
    coverage: Mapping[str, Any]
    companyfacts_archive_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "coverage", MappingProxyType(dict(self.coverage)))


@dataclass(frozen=True)
class _MembershipInterval:
    ticker: str
    first: date
    last: date


@dataclass(frozen=True)
class _Identity:
    ticker: str
    chain_id: str
    continuity_kind: str
    admitted_start: date
    admitted_end: date


@dataclass(frozen=True)
class _Issuer:
    cik: str
    company_name: str
    tickers: tuple[str, ...]
    current_name_key: str
    former_name_keys: tuple[str, ...]


@dataclass(frozen=True)
class _Candidate:
    accession: str
    form: str
    statement_type: str
    period_end: date
    filed: date
    fiscal_year: str
    fiscal_period: str
    acceptance: datetime | None
    public_date: date
    public_date_basis: str
    values: Mapping[str, float]
    concepts: Mapping[str, str]


def _regular_file(path: Path, label: str) -> Path:
    absolute = Path(os.path.abspath(path))
    info = absolute.lstat()
    resolved = absolute.resolve(strict=True)
    if not stat.S_ISREG(info.st_mode) or absolute.is_symlink() or resolved != absolute:
        raise ValueError(f"{label} must be a regular non-link file")
    return resolved


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with _regular_file(path, "input").open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _csv_rows(path: Path, header: tuple[str, ...]) -> list[dict[str, str]]:
    path = _regular_file(path, path.name)
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != header:
            raise ValueError(f"{path.name} header is invalid")
        rows = []
        for number, row in enumerate(reader, start=2):
            if any(value is None for value in row.values()):
                raise ValueError(f"{path.name}:{number} is malformed")
            rows.append({key: str(row[key]).strip() for key in header})
    return rows


def _iso_date(value: object, label: str) -> date:
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO date") from exc


def _canonical_ticker(value: object) -> str:
    ticker = str(value).strip().upper()
    ticker = _KNOWN_SEC_ALIASES.get(ticker, ticker)
    if not _TICKER.fullmatch(ticker):
        raise ValueError(f"invalid ticker: {value!r}")
    return ticker


def _sec_ticker_or_none(value: object) -> str | None:
    ticker = str(value).strip().upper()
    ticker = _KNOWN_SEC_ALIASES.get(ticker, ticker)
    return ticker if _TICKER.fullmatch(ticker) else None


def _name_key(value: object) -> str:
    """Normalize only Unicode, case, and whitespace for an otherwise exact name match."""
    return " ".join(unicodedata.normalize("NFKC", str(value)).casefold().split())


def _accession(value: object) -> str:
    normalized = str(value).strip().replace("-", "")
    if not _ACCESSION.fullmatch(normalized):
        raise ValueError(f"invalid accession number: {value!r}")
    return normalized


def _display_accession(value: str) -> str:
    return f"{value[:10]}-{value[10:12]}-{value[12:]}"


def _zip_members(
    archive: Path,
    *,
    max_entries: int = 2_000_000,
    max_uncompressed_bytes: int = 250 * 1024 * 1024 * 1024,
    max_compression_ratio: float = 2_000.0,
    max_member_bytes: int = 512 * 1024 * 1024,
) -> tuple[zipfile.ZipFile, Mapping[str, zipfile.ZipInfo]]:
    archive = _regular_file(archive, "SEC archive")
    handle = zipfile.ZipFile(archive, "r")
    try:
        infos = handle.infolist()
        if not infos or len(infos) > max_entries:
            raise ValueError("SEC archive entry count is outside its safety bound")
        names: dict[str, zipfile.ZipInfo] = {}
        total = 0
        for info in infos:
            path = PurePosixPath(info.filename)
            if info.flag_bits & 0x1:
                raise ValueError("SEC archive contains an encrypted member")
            if info.is_dir() or path.is_absolute() or ".." in path.parts or "\\" in info.filename:
                raise ValueError("SEC archive contains an unsafe member path")
            if info.filename in names:
                raise ValueError("SEC archive contains duplicate member names")
            total += info.file_size
            if total > max_uncompressed_bytes:
                raise ValueError("SEC archive exceeds its uncompressed byte bound")
            if info.file_size > max_member_bytes:
                raise ValueError("SEC archive member exceeds the caller-bound expanded-byte limit")
            if info.file_size and info.compress_size == 0:
                raise ValueError("SEC archive contains an invalid compressed member")
            if info.compress_size and info.file_size / info.compress_size > max_compression_ratio:
                raise ValueError("SEC archive member exceeds its compression-ratio bound")
            names[info.filename] = info
        return handle, MappingProxyType(names)
    except Exception:
        handle.close()
        raise


def validate_sec_archive(path: Path, *, max_member_bytes: int = 512 * 1024 * 1024) -> tuple[int, int]:
    """Validate ZIP structure without extracting it; return entries and bytes."""
    handle, members = _zip_members(path, max_member_bytes=max_member_bytes)
    try:
        return len(members), sum(info.file_size for info in members.values())
    finally:
        handle.close()


def _json_member(handle: zipfile.ZipFile, info: zipfile.ZipInfo) -> Mapping[str, Any]:
    try:
        with handle.open(info, "r") as stream:
            value = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError) as exc:
        raise ValueError(f"invalid SEC JSON member: {info.filename}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"SEC JSON member is not an object: {info.filename}")
    return value


def _membership_intervals(
    membership_csv: Path,
    *,
    start_date: date,
    end_date: date,
) -> tuple[tuple[_MembershipInterval, ...], tuple[str, ...]]:
    if end_date < start_date:
        raise ValueError("membership end date precedes start date")
    events: list[tuple[date, str, bool]] = []
    seen: set[tuple[date, str]] = set()
    for row in _csv_rows(membership_csv, _MEMBERSHIP_COLUMNS):
        when = _iso_date(row["effective_date"], "membership effective_date")
        if not start_date <= when <= end_date:
            raise ValueError("membership event is outside the requested interval")
        ticker = _canonical_ticker(row["ticker"])
        if row["member"] not in {"0", "1"}:
            raise ValueError("membership member must be 0 or 1")
        key = (when, ticker)
        if key in seen:
            raise ValueError("duplicate membership transition")
        seen.add(key)
        events.append((when, ticker, row["member"] == "1"))
    events.sort()
    active: dict[str, date] = {}
    intervals: list[_MembershipInterval] = []
    union: set[str] = set()
    for when, ticker, member in events:
        union.add(ticker)
        if member:
            if ticker in active:
                raise ValueError(f"membership adds an already-active ticker: {ticker}")
            active[ticker] = when
        else:
            first = active.pop(ticker, None)
            if first is None:
                raise ValueError(f"membership removes an inactive ticker: {ticker}")
            last = when - timedelta(days=1)
            if last < first:
                raise ValueError(f"membership interval is empty: {ticker}")
            intervals.append(_MembershipInterval(ticker, first, last))
    intervals.extend(_MembershipInterval(ticker, first, end_date) for ticker, first in active.items())
    ordered = tuple(sorted(intervals, key=lambda item: (item.ticker, item.first, item.last)))
    if not ordered:
        raise ValueError("membership export is empty")
    return ordered, tuple(sorted(union))


def _security_names(path: Path, union: Sequence[str]) -> Mapping[str, str]:
    allowed = set(union)
    result: dict[str, str] = {}
    for row in _csv_rows(path, _SECURITY_NAME_COLUMNS):
        ticker = _canonical_ticker(row["ticker"])
        if ticker not in allowed:
            continue
        name = row["company_name"].strip()
        if not name or not _name_key(name):
            raise ValueError(f"security name is empty for {ticker}")
        if ticker in result and result[ticker] != name:
            raise ValueError(f"security names contain conflicting rows for {ticker}")
        result[ticker] = name
    missing = allowed.difference(result)
    if missing:
        raise ValueError(f"security names omit membership tickers: {sorted(missing)!r}")
    return MappingProxyType(result)


def _identity_manifest(
    path: Path,
    union: Sequence[str],
    intervals: Sequence[_MembershipInterval],
) -> tuple[Mapping[str, _Identity], str]:
    raw_hash = sha256_file(path)
    result: dict[str, _Identity] = {}
    for row in _csv_rows(path, _IDENTITY_COLUMNS):
        ticker = _canonical_ticker(row["canonical_ticker"])
        if ticker not in set(union):
            continue
        if ticker in result:
            raise ValueError(f"reviewed identity manifest repeats {ticker}")
        chain = row["chain_id"].strip()
        kind = row["continuity_kind"].strip() or "standalone"
        if not chain or kind not in _IDENTITY_KINDS:
            raise ValueError(f"reviewed identity manifest has invalid continuity for {ticker}")
        result[ticker] = _Identity(
            ticker=ticker,
            chain_id=chain,
            continuity_kind=kind,
            admitted_start=_iso_date(row["admitted_start"], "identity admitted_start"),
            admitted_end=_iso_date(row["admitted_end"], "identity admitted_end"),
        )
    bounds: dict[str, tuple[date, date]] = {}
    for ticker in union:
        ticker_intervals = [item for item in intervals if item.ticker == ticker]
        bounds[ticker] = (
            min(item.first for item in ticker_intervals),
            max(item.last for item in ticker_intervals),
        )
    for ticker in set(union).difference(result):
        first, last = bounds[ticker]
        result[ticker] = _Identity(ticker, f"standalone:{ticker}", "standalone", first, last)
    return MappingProxyType(result), raw_hash


def _issuer(payload: Mapping[str, Any], expected_cik: str) -> _Issuer:
    raw_cik = str(payload.get("cik", "")).strip()
    try:
        cik = f"{int(raw_cik):010d}"
    except ValueError as exc:
        raise ValueError(f"submission payload has invalid CIK: {expected_cik}") from exc
    if cik != expected_cik:
        raise ValueError(f"submission filename/payload CIK mismatch: {expected_cik}")
    company_name = str(payload.get("name", "")).strip()
    current_key = _name_key(company_name)
    if not current_key:
        raise ValueError(f"submission payload has no company name: {cik}")
    raw_tickers = payload.get("tickers", [])
    if not isinstance(raw_tickers, list):
        raise ValueError(f"submission payload has invalid tickers: {cik}")
    tickers = tuple(sorted({ticker for value in raw_tickers if (ticker := _sec_ticker_or_none(value)) is not None}))
    raw_former = payload.get("formerNames", [])
    if not isinstance(raw_former, list):
        raise ValueError(f"submission payload has invalid formerNames: {cik}")
    former: set[str] = set()
    for entry in raw_former:
        if not isinstance(entry, dict) or "name" not in entry:
            raise ValueError(f"submission payload has malformed formerNames: {cik}")
        key = _name_key(entry["name"])
        if key:
            former.add(key)
    return _Issuer(cik, company_name, tickers, current_key, tuple(sorted(former)))


def _scan_candidate_issuers(
    submissions_archive: Path,
    *,
    union: Sequence[str],
    names: Mapping[str, str],
    max_json_member_bytes: int,
) -> Mapping[str, _Issuer]:
    target_tickers = set(union)
    target_names = {_name_key(names[ticker]) for ticker in union}
    reviewed_ciks = {
        expected_cik
        for ticker, expected_cik in _REVIEWED_BASELINE_CIKS.items()
        if ticker in target_tickers
    }
    handle, members = _zip_members(submissions_archive, max_member_bytes=max_json_member_bytes)
    issuers: dict[str, _Issuer] = {}
    try:
        for member_name, info in members.items():
            match = _CIK_MEMBER.search(member_name)
            if not match:
                continue
            cik = match.group("cik")
            payload = _json_member(handle, info)
            # The SEC submissions bulk archive includes legacy/non-issuer
            # records that can have empty names, malformed optional fields, or
            # no ticker at all.  They cannot match the requested universe, so
            # do not let their unrelated metadata abort the scan.  Once a
            # record can match a requested ticker/name, `_issuer` remains the
            # strict validator and rejects malformed candidate data.
            raw_tickers = payload.get("tickers")
            candidate_tickers = (
                {
                    ticker
                    for value in raw_tickers
                    if (ticker := _sec_ticker_or_none(value)) is not None
                }
                if isinstance(raw_tickers, list)
                else set()
            )
            candidate_name = _name_key(payload.get("name", ""))
            raw_former_names = payload.get("formerNames")
            candidate_former_names = (
                {
                    _name_key(entry["name"])
                    for entry in raw_former_names
                    if isinstance(entry, dict) and "name" in entry
                }
                if isinstance(raw_former_names, list)
                else set()
            )
            if not (
                cik in reviewed_ciks
                or
                target_tickers.intersection(candidate_tickers)
                or candidate_name in target_names
                or target_names.intersection(candidate_former_names)
            ):
                continue
            issuer = _issuer(payload, cik)
            if (
                cik in reviewed_ciks
                or
                target_tickers.intersection(issuer.tickers)
                or issuer.current_name_key in target_names
                or target_names.intersection(issuer.former_name_keys)
            ):
                issuers[cik] = issuer
    finally:
        handle.close()
    return MappingProxyType(issuers)


def _resolve_ciks(
    *,
    intervals: Sequence[_MembershipInterval],
    union: Sequence[str],
    names: Mapping[str, str],
    identities: Mapping[str, _Identity],
    issuers: Mapping[str, _Issuer],
) -> tuple[
    Mapping[_MembershipInterval, tuple[str, str]],
    Mapping[_MembershipInterval, tuple[str, str]],
]:
    ticker_index: dict[str, set[str]] = defaultdict(set)
    current_name_index: dict[str, set[str]] = defaultdict(set)
    former_name_index: dict[str, set[str]] = defaultdict(set)
    for cik, issuer in issuers.items():
        for ticker in issuer.tickers:
            ticker_index[ticker].add(cik)
        current_name_index[issuer.current_name_key].add(cik)
        for key in issuer.former_name_keys:
            former_name_index[key].add(cik)

    ticker_resolved: dict[str, tuple[str, str]] = {}
    ticker_unresolved: dict[str, tuple[str, str]] = {}
    for ticker in union:
        direct = ticker_index.get(ticker, set())
        if len(direct) == 1:
            ticker_resolved[ticker] = (next(iter(direct)), "exact_current_ticker")
        elif len(direct) > 1:
            # A single display name has no date-bounded issuer evidence and may
            # describe only the latest user of a reused ticker.  Every interval
            # therefore remains closed unless an explicit reviewed same-issuer
            # chain below supplies one consistent CIK.
            ticker_unresolved[ticker] = ("ambiguous_ticker_reuse", ",".join(sorted(direct)))

    for ticker in union:
        if ticker in ticker_resolved or ticker in ticker_unresolved:
            continue
        key = _name_key(names[ticker])
        current = current_name_index.get(key, set())
        former = former_name_index.get(key, set())
        candidates = current | former
        if len(candidates) == 1:
            cik = next(iter(candidates))
            basis = "exact_current_name" if cik in current else "exact_former_name"
            ticker_resolved[ticker] = (cik, basis)
        elif candidates:
            ticker_unresolved[ticker] = ("ambiguous_exact_name", ",".join(sorted(candidates)))

    chain_members: dict[str, list[str]] = defaultdict(list)
    for ticker, identity in identities.items():
        chain_members[identity.chain_id].append(ticker)
    for chain_id, tickers in chain_members.items():
        same_issuer_tickers = [
            ticker for ticker in tickers if identities[ticker].continuity_kind in _SAME_ISSUER_KINDS
        ]
        if not same_issuer_tickers:
            continue
        known = {ticker_resolved[ticker][0] for ticker in same_issuer_tickers if ticker in ticker_resolved}
        if len(known) == 1:
            cik = next(iter(known))
            for ticker in same_issuer_tickers:
                ticker_resolved[ticker] = (cik, "reviewed_same_issuer_chain")
                ticker_unresolved.pop(ticker, None)
        elif len(known) > 1:
            for ticker in same_issuer_tickers:
                ticker_resolved.pop(ticker, None)
                ticker_unresolved[ticker] = (
                    "conflicting_reviewed_same_issuer_chain",
                    f"{chain_id}:{','.join(sorted(known))}",
                )

    for ticker in union:
        if ticker not in ticker_resolved and ticker not in ticker_unresolved:
            ticker_unresolved[ticker] = ("no_exact_sec_identity", "no exact ticker or exact normalized-name match")

    # A small set of reviewed PIT identity boundaries intentionally overrides
    # present-day SEC ticker reuse (for example, PARA is now used by an
    # unrelated issuer).  Require the reviewed CIK to exist in the archive and
    # bind every interval for that ticker to it before emitting the result.
    for ticker, expected_cik in _REVIEWED_BASELINE_CIKS.items():
        if ticker not in union:
            continue
        if expected_cik in issuers:
            ticker_resolved[ticker] = (expected_cik, "reviewed_baseline_cik")
            ticker_unresolved.pop(ticker, None)
        else:
            ticker_resolved.pop(ticker, None)
            ticker_unresolved[ticker] = (
                "reviewed_baseline_cik_missing",
                expected_cik,
            )

    resolved: dict[_MembershipInterval, tuple[str, str]] = {}
    unresolved: dict[_MembershipInterval, tuple[str, str]] = {}
    for interval in intervals:
        if interval.ticker in ticker_resolved:
            resolved[interval] = ticker_resolved[interval.ticker]
        else:
            unresolved[interval] = ticker_unresolved[interval.ticker]
    return MappingProxyType(resolved), MappingProxyType(unresolved)


def _acceptance_datetime(value: object, *, cik: str, accession: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if re.fullmatch(r"\d{14}", text):
            parsed = datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            else:
                parsed = parsed.astimezone(timezone.utc)
    except ValueError as exc:
        raise ValueError(f"malformed acceptanceDateTime for CIK {cik} accession {accession}") from exc
    return parsed


def _filing_table(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    filings = payload.get("filings")
    if isinstance(filings, dict) and isinstance(filings.get("recent"), dict):
        return filings["recent"]
    if isinstance(payload.get("accessionNumber"), list):
        return payload
    return None


def _parse_acceptances(
    payload: Mapping[str, Any],
    *,
    cik: str,
    target: dict[str, FilingAcceptance],
) -> None:
    table = _filing_table(payload)
    if table is None:
        return
    accessions = table.get("accessionNumber", [])
    forms = table.get("form", [])
    filed_dates = table.get("filingDate", [])
    accepted = table.get("acceptanceDateTime", [])
    if not all(isinstance(values, list) for values in (accessions, forms, filed_dates)):
        raise ValueError(f"submission filing arrays are invalid for CIK {cik}")
    if not (len(accessions) == len(forms) == len(filed_dates)):
        raise ValueError(f"submission filing arrays differ in length for CIK {cik}")
    if accepted and (not isinstance(accepted, list) or len(accepted) != len(accessions)):
        raise ValueError(f"submission acceptance array differs in length for CIK {cik}")
    accepted_values = accepted if isinstance(accepted, list) and accepted else [""] * len(accessions)
    for raw_accession, raw_form, raw_filed, raw_accepted in zip(
        accessions, forms, filed_dates, accepted_values, strict=True
    ):
        accession = _accession(raw_accession)
        acceptance = _acceptance_datetime(raw_accepted, cik=cik, accession=accession)
        if acceptance is None:
            continue
        item = FilingAcceptance(
            accession_number=accession,
            form=str(raw_form).strip().upper(),
            filed_date=_iso_date(raw_filed, "submission filingDate"),
            acceptance_datetime=acceptance,
        )
        previous = target.get(accession)
        if previous is not None and previous != item:
            raise ValueError(f"conflicting submission metadata for CIK {cik} accession {accession}")
        target[accession] = item


def _acceptances_for_ciks(
    submissions_archive: Path,
    ciks: Iterable[str],
    *,
    max_json_member_bytes: int,
) -> tuple[Mapping[str, Mapping[str, FilingAcceptance]], int]:
    handle, members = _zip_members(submissions_archive, max_member_bytes=max_json_member_bytes)
    result: dict[str, Mapping[str, FilingAcceptance]] = {}
    missing_fragments = 0
    try:
        main_by_cik: dict[str, zipfile.ZipInfo] = {}
        by_basename: dict[str, list[zipfile.ZipInfo]] = defaultdict(list)
        for member_name, info in members.items():
            by_basename[PurePosixPath(member_name).name].append(info)
            match = _CIK_MEMBER.search(member_name)
            if match:
                cik = match.group("cik")
                if cik in main_by_cik:
                    raise ValueError(f"SEC submissions archive repeats main CIK {cik}")
                main_by_cik[cik] = info
        for cik in sorted(set(ciks)):
            main = main_by_cik.get(cik)
            if main is None:
                raise ValueError(f"SEC submissions archive lacks a main record for CIK {cik}")
            payload = _json_member(handle, main)
            acceptances: dict[str, FilingAcceptance] = {}
            _parse_acceptances(payload, cik=cik, target=acceptances)
            filings = payload.get("filings", {})
            files = filings.get("files", []) if isinstance(filings, dict) else []
            if not isinstance(files, list):
                raise ValueError(f"submission fragment list is invalid for CIK {cik}")
            for reference in files:
                if not isinstance(reference, dict) or not isinstance(reference.get("name"), str):
                    raise ValueError(f"submission fragment reference is malformed for CIK {cik}")
                filename = reference["name"].strip()
                info = members.get(filename)
                if info is None:
                    matches = by_basename.get(filename, [])
                    if len(matches) > 1:
                        raise ValueError(f"submission fragment basename is ambiguous: {filename}")
                    info = matches[0] if matches else None
                if info is None:
                    missing_fragments += 1
                    continue
                _parse_acceptances(_json_member(handle, info), cik=cik, target=acceptances)
            result[cik] = MappingProxyType(acceptances)
    finally:
        handle.close()
    return MappingProxyType(result), missing_fragments


def build_security_master(
    membership_csv: Path,
    security_names_csv: Path,
    submissions_archive: Path,
    identity_manifest_csv: Path,
    *,
    start_date: date = date(2021, 1, 1),
    end_date: date = date(2025, 12, 31),
    max_json_member_bytes: int = 512 * 1024 * 1024,
) -> SecurityMasterResult:
    """Resolve membership intervals to exact CIKs without fuzzy name matching."""
    intervals, union = _membership_intervals(membership_csv, start_date=start_date, end_date=end_date)
    names = _security_names(security_names_csv, union)
    identities, identity_hash = _identity_manifest(identity_manifest_csv, union, intervals)
    for interval in intervals:
        identity = identities[interval.ticker]
        if interval.first < identity.admitted_start or interval.last > identity.admitted_end:
            raise ValueError(f"reviewed identity does not cover membership interval for {interval.ticker}")
    issuers = _scan_candidate_issuers(
        submissions_archive,
        union=union,
        names=names,
        max_json_member_bytes=max_json_member_bytes,
    )
    resolved, unresolved = _resolve_ciks(
        intervals=intervals,
        union=union,
        names=names,
        identities=identities,
        issuers=issuers,
    )
    for ticker, expected_cik in _REVIEWED_BASELINE_CIKS.items():
        ticker_mappings = {resolved[item][0] for item in intervals if item.ticker == ticker and item in resolved}
        if ticker in union and ticker_mappings != {expected_cik}:
            actual = ",".join(sorted(ticker_mappings)) or "unresolved"
            raise ValueError(f"reviewed baseline CIK boundary failed for {ticker}: {actual} != {expected_cik}")

    rows: list[SecurityMasterRow] = []
    exclusions: list[SecurityMasterExclusion] = []
    for interval in intervals:
        mapping = resolved.get(interval)
        if mapping is None:
            reason, details = unresolved[interval]
            exclusions.append(
                SecurityMasterExclusion(
                    interval.ticker,
                    names[interval.ticker],
                    interval.first,
                    interval.last,
                    reason,
                    details,
                )
            )
            continue
        cik, basis = mapping
        rows.append(
            SecurityMasterRow(
                interval.ticker,
                cik,
                names[interval.ticker],
                interval.first,
                interval.last,
                basis,
            )
        )
    row_keys = [(row.ticker, row.cik, row.first_membership_date, row.last_membership_date) for row in rows]
    if len(row_keys) != len(set(row_keys)):
        raise ValueError("security master contains duplicate membership intervals")
    by_ticker: dict[str, list[SecurityMasterRow]] = defaultdict(list)
    for row in rows:
        by_ticker[row.ticker].append(row)
    for ticker, ticker_rows in by_ticker.items():
        ordered = sorted(ticker_rows, key=lambda row: row.first_membership_date)
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if previous.cik != current.cik and previous.last_membership_date >= current.first_membership_date:
                raise ValueError(f"security master has overlapping reused issuers for {ticker}")

    acceptance_by_cik, missing_fragments = _acceptances_for_ciks(
        submissions_archive,
        (row.cik for row in rows),
        max_json_member_bytes=max_json_member_bytes,
    )
    return SecurityMasterResult(
        rows=tuple(sorted(rows, key=lambda row: (row.ticker, row.first_membership_date, row.cik))),
        exclusions=tuple(sorted(exclusions, key=lambda row: (row.ticker, row.first_membership_date))),
        acceptance_by_cik=acceptance_by_cik,
        membership_union=union,
        identity_manifest_sha256=identity_hash,
        submissions_archive_sha256=sha256_file(submissions_archive),
        missing_submission_fragments=missing_fragments,
    )


def _spy_dates(path: Path) -> tuple[date, ...]:
    values = [_iso_date(row["trade_date"], "SPY trade_date") for row in _csv_rows(path, ("trade_date",))]
    if not values or values != sorted(set(values)):
        raise ValueError("SPY trading days must be nonempty, unique, and sorted")
    return tuple(values)


def _next_trading_day(days: Sequence[date], value: date) -> date | None:
    index = bisect.bisect_right(days, value)
    return days[index] if index < len(days) else None


def _companyfacts_members(members: Mapping[str, zipfile.ZipInfo]) -> Mapping[str, zipfile.ZipInfo]:
    result: dict[str, zipfile.ZipInfo] = {}
    for name, info in members.items():
        match = _CIK_MEMBER.search(name)
        if match:
            cik = match.group("cik")
            if cik in result:
                raise ValueError(f"companyfacts archive repeats CIK {cik}")
            result[cik] = info
    return MappingProxyType(result)


def _fact_list(
    facts: Mapping[str, Any],
    namespace: str,
    concept: str,
    unit: str,
) -> list[Mapping[str, Any]]:
    namespace_value = facts.get(namespace, {})
    if not isinstance(namespace_value, dict):
        raise ValueError(f"companyfacts namespace is invalid: {namespace}")
    concept_value = namespace_value.get(concept)
    if concept_value is None:
        return []
    if not isinstance(concept_value, dict) or not isinstance(concept_value.get("units"), dict):
        raise ValueError(f"companyfacts concept is invalid: {namespace}:{concept}")
    units = concept_value["units"]
    raw = units.get(unit, [])
    if not isinstance(raw, list):
        raise ValueError(f"companyfacts unit is invalid: {namespace}:{concept}:{unit}")
    if any(not isinstance(item, dict) for item in raw):
        raise ValueError(f"companyfacts facts are malformed: {namespace}:{concept}:{unit}")
    return raw


def _fact_number(value: object, *, concept: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"companyfacts concept has a boolean value: {concept}")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"companyfacts concept is not numeric: {concept}") from exc
    if not math.isfinite(number):
        raise ValueError(f"companyfacts concept is non-finite: {concept}")
    return number


def _fact_form(value: object) -> tuple[str, str] | None:
    form = str(value).strip().upper()
    base = form.removesuffix("/A")
    if base == "10-Q":
        return form, "quarterly"
    if base == "10-K":
        return form, "annual"
    return None


def _form_family(value: str) -> str:
    """Compare SEC filing variants by their statement family."""
    base = value.strip().upper().removesuffix("/A")
    if base in {"10-Q", "10-QT"}:
        return "10-Q"
    if base in {"10-K", "10-KT"}:
        return "10-K"
    return base


def _candidate_metadata(
    raw: Mapping[str, Any],
    *,
    cik: str,
    acceptances: Mapping[str, FilingAcceptance],
    spy_days: Sequence[date],
    start_date: date,
    end_date: date,
    counters: Counter[str],
) -> tuple[str, str, str, date, date, str, str, datetime | None, date, str] | None:
    required = ("accn", "form", "filed", "end", "val")
    if any(key not in raw for key in required):
        counters["malformed_fact_omissions"] += 1
        return None
    normalized_form = _fact_form(raw["form"])
    if normalized_form is None:
        return None
    form, statement_type = normalized_form
    accession = _accession(raw["accn"])
    filed = _iso_date(raw["filed"], "companyfacts filed")
    period_end = _iso_date(raw["end"], "companyfacts end")
    acceptance_record = acceptances.get(accession)
    if acceptance_record is not None:
        if _form_family(acceptance_record.form) != _form_family(form):
            raise ValueError(f"submission/companyfacts metadata mismatch for CIK {cik} accession {accession}")
        if acceptance_record.form != form:
            counters["submission_companyfacts_form_variant_count"] += 1
        if acceptance_record.filed_date != filed:
            counters["submission_companyfacts_filed_date_mismatch_count"] += 1
        acceptance = acceptance_record.acceptance_datetime
        basis = "acceptance_datetime"
        source_date = acceptance.date()
        counters["accession_join_fact_count"] += 1
    else:
        acceptance = None
        basis = "filed_date_fallback"
        source_date = filed
        counters["filed_date_fallback_fact_count"] += 1
    public_date = _next_trading_day(spy_days, source_date)
    if public_date is None or public_date > end_date:
        counters["post_cutoff_fact_omissions"] += 1
        return None
    if public_date < start_date:
        counters["pre_window_fact_omissions"] += 1
        return None
    if public_date <= period_end:
        counters["public_before_period_end_fact_omissions"] += 1
        return None
    return (
        accession,
        form,
        statement_type,
        period_end,
        filed,
        str(raw.get("fy", "") or ""),
        str(raw.get("fp", "") or ""),
        acceptance,
        public_date,
        basis,
    )


def _duration_score(
    raw: Mapping[str, Any],
    *,
    statement_type: str,
    counters: Counter[str],
) -> tuple[int, int] | None:
    if "start" not in raw or not str(raw.get("start", "")).strip():
        counters[f"{statement_type}_duration_omissions"] += 1
        return None
    start = _iso_date(raw["start"], "companyfacts start")
    end = _iso_date(raw["end"], "companyfacts end")
    duration = (end - start).days + 1
    frame = str(raw.get("frame", "") or "").upper()
    if statement_type == "quarterly":
        if not 70 <= duration <= 115:
            counters["quarterly_ytd_or_duration_omissions"] += 1
            return None
        explicit = int(bool(re.fullmatch(r"CY\d{4}Q[1-4](?:I)?", frame)))
        return explicit, -abs(duration - 91)
    if not 300 <= duration <= 430:
        counters["annual_duration_omissions"] += 1
        return None
    explicit = int(bool(re.fullmatch(r"CY\d{4}(?:I)?", frame)))
    return explicit, -abs(duration - 365)


def _candidates_for_cik(
    payload: Mapping[str, Any],
    *,
    cik: str,
    acceptances: Mapping[str, FilingAcceptance],
    spy_days: Sequence[date],
    start_date: date,
    end_date: date,
    counters: Counter[str],
) -> tuple[_Candidate, ...]:
    raw_cik = str(payload.get("cik", "")).strip()
    try:
        payload_cik = f"{int(raw_cik):010d}"
    except ValueError as exc:
        raise ValueError(f"companyfacts payload has invalid CIK: {cik}") from exc
    if payload_cik != cik or not isinstance(payload.get("facts"), dict):
        raise ValueError(f"companyfacts filename/payload mismatch for CIK {cik}")
    facts: Mapping[str, Any] = payload["facts"]
    # One fact can appear twice in companyfacts (for example with and without a frame).
    # Exact repeats are removed before context selection.
    seen_facts: set[tuple[Any, ...]] = set()
    groups: dict[tuple[Any, ...], dict[str, list[tuple[tuple[int, ...], float, str]]]] = defaultdict(
        lambda: defaultdict(list)
    )

    definitions: list[tuple[str, str, str, str, int, bool]] = []
    definitions.extend((metric, ns, concept, unit, 0, False) for metric, (ns, concept, unit) in _INCOME_CONCEPTS.items())
    definitions.extend(
        ("total_revenue", ns, concept, unit, rank, False)
        for rank, (ns, concept, unit) in enumerate(_REVENUE_PRIORITY)
    )
    definitions.extend((metric, ns, concept, unit, 0, True) for metric, (ns, concept, unit) in _BALANCE_CONCEPTS.items())

    for metric, namespace, concept, unit, concept_rank, balance_metric in definitions:
        for raw in _fact_list(facts, namespace, concept, unit):
            metadata = _candidate_metadata(
                raw,
                cik=cik,
                acceptances=acceptances,
                spy_days=spy_days,
                start_date=start_date,
                end_date=end_date,
                counters=counters,
            )
            if metadata is None:
                continue
            accession, form, income_type, period_end, filed, fy, fp, acceptance, public_date, basis = metadata
            statement_type = "balance" if balance_metric else income_type
            if balance_metric:
                context_score = (0, 0)
            else:
                duration_score = _duration_score(raw, statement_type=statement_type, counters=counters)
                if duration_score is None:
                    continue
                context_score = duration_score
            number = _fact_number(raw["val"], concept=concept)
            duplicate_key = (
                metric,
                namespace,
                concept,
                unit,
                accession,
                form,
                str(raw.get("start", "")),
                period_end,
                str(raw.get("frame", "")),
                filed,
                fy,
                fp,
                number,
            )
            if duplicate_key in seen_facts:
                counters["exact_duplicate_fact_count"] += 1
                continue
            seen_facts.add(duplicate_key)
            key = (
                accession,
                form,
                statement_type,
                period_end,
                filed,
                fy,
                fp,
                acceptance,
                public_date,
                basis,
            )
            score = (-concept_rank, *context_score)
            groups[key][metric].append((score, number, f"{namespace}:{concept}"))

    candidates: list[_Candidate] = []
    for key, metric_entries in groups.items():
        values: dict[str, float] = {}
        concepts: dict[str, str] = {}
        for metric, entries in metric_entries.items():
            best_score = max(item[0] for item in entries)
            best = [item for item in entries if item[0] == best_score]
            if len({item[1] for item in best}) > 1:
                raise ValueError(f"companyfacts has ambiguous best contexts for CIK {cik} metric {metric}")
            chosen = sorted(best, key=lambda item: (item[2], item[1]))[0]
            distinct_values = {item[1] for item in entries}
            if len(distinct_values) > 1:
                counters["context_value_conflict_count"] += 1
            if metric == "total_revenue":
                distinct_concepts = {item[2] for item in entries}
                if len(distinct_concepts) > 1:
                    counters["revenue_concept_conflict_count"] += 1
            values[metric] = chosen[1]
            concepts[metric] = chosen[2]
            counters[f"selected_{chosen[2].replace(':', '_')}"] += 1
        accession, form, statement_type, period_end, filed, fy, fp, acceptance, public_date, basis = key
        candidates.append(
            _Candidate(
                accession=accession,
                form=form,
                statement_type=statement_type,
                period_end=period_end,
                filed=filed,
                fiscal_year=fy,
                fiscal_period=fp,
                acceptance=acceptance,
                public_date=public_date,
                public_date_basis=basis,
                values=MappingProxyType(values),
                concepts=MappingProxyType(concepts),
            )
        )
    return tuple(candidates)


def _candidate_timestamp(candidate: _Candidate) -> datetime:
    if candidate.acceptance is not None:
        return candidate.acceptance
    return datetime.combine(candidate.filed, datetime.min.time(), tzinfo=timezone.utc)


def _materialize_ticker(
    ticker: str,
    candidates: Iterable[_Candidate],
    counters: Counter[str],
) -> tuple[list[FundamentalRow], list[FundamentalAuditRow]]:
    ordered = sorted(
        candidates,
        key=lambda item: (
            item.statement_type,
            item.period_end,
            item.public_date,
            _candidate_timestamp(item),
            item.accession,
            item.form,
            item.filed,
            item.fiscal_year,
            item.fiscal_period,
        ),
    )
    by_period: dict[tuple[str, date], list[_Candidate]] = defaultdict(list)
    for candidate in ordered:
        by_period[(candidate.statement_type, candidate.period_end)].append(candidate)

    visible: dict[tuple[str, str, date, date], tuple[FundamentalRow, FundamentalAuditRow]] = {}
    for (statement_type, period_end), period_candidates in by_period.items():
        state: dict[str, float] = {}
        state_concepts: dict[str, str] = {}
        state_origins: dict[str, Mapping[str, str]] = {}
        previous_public: date | None = None
        for candidate in period_candidates:
            inherited = sorted(metric for metric in state if metric not in candidate.values)
            state.update(candidate.values)
            state_concepts.update(candidate.concepts)
            for metric in candidate.values:
                state_origins[metric] = {
                    "accession_number": _display_accession(candidate.accession),
                    "form": candidate.form,
                    "filed_date": candidate.filed.isoformat(),
                    "fiscal_year": candidate.fiscal_year,
                    "fiscal_period": candidate.fiscal_period,
                    "acceptance_datetime": (
                        candidate.acceptance.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                        if candidate.acceptance is not None
                        else ""
                    ),
                    "public_date_basis": candidate.public_date_basis,
                    "source_concept": candidate.concepts[metric],
                }
            if not state:
                continue
            if previous_public is not None and candidate.public_date > previous_public:
                counters["later_snapshot_count"] += 1
            previous_public = candidate.public_date
            row = FundamentalRow(
                ticker=ticker,
                statement_type=statement_type,
                period_end=period_end,
                public_date=candidate.public_date,
                **{metric: state.get(metric) for metric in _NUMERIC_COLUMNS},
            )
            audit = FundamentalAuditRow(
                ticker=ticker,
                statement_type=statement_type,
                period_end=period_end,
                public_date=candidate.public_date,
                accession_number=_display_accession(candidate.accession),
                form=candidate.form,
                filed_date=candidate.filed,
                fiscal_year=candidate.fiscal_year,
                fiscal_period=candidate.fiscal_period,
                acceptance_datetime=(
                    candidate.acceptance.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                    if candidate.acceptance is not None
                    else ""
                ),
                public_date_basis=candidate.public_date_basis,
                source_concepts=json.dumps(state_concepts, sort_keys=True, separators=(",", ":")),
                inherited_metrics=",".join(inherited),
                metric_sources=json.dumps(state_origins, sort_keys=True, separators=(",", ":")),
            )
            key = (ticker, statement_type, period_end, candidate.public_date)
            if key in visible:
                counters["same_public_date_collision_count"] += 1
            visible[key] = (row, audit)
    pairs = sorted(
        visible.values(),
        key=lambda pair: (pair[0].ticker, pair[0].statement_type, pair[0].public_date, pair[0].period_end),
    )
    return [pair[0] for pair in pairs], [pair[1] for pair in pairs]


def _cik_windows(rows: Sequence[SecurityMasterRow]) -> Mapping[str, Mapping[str, tuple[date | None, date | None]]]:
    by_ticker: dict[str, dict[str, list[SecurityMasterRow]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        by_ticker[row.ticker][row.cik].append(row)
    result: dict[str, Mapping[str, tuple[date | None, date | None]]] = {}
    for ticker, ciks in by_ticker.items():
        if len(ciks) == 1:
            cik = next(iter(ciks))
            result[ticker] = MappingProxyType({cik: (None, None)})
            continue
        ordered = sorted(
            (
                min(item.first_membership_date for item in cik_rows),
                max(item.last_membership_date for item in cik_rows),
                cik,
            )
            for cik, cik_rows in ciks.items()
        )
        windows: dict[str, tuple[date | None, date | None]] = {}
        for index, (first, last, cik) in enumerate(ordered):
            lower = None if index == 0 else first
            windows[cik] = (lower, last)
        result[ticker] = MappingProxyType(windows)
    return MappingProxyType(result)


def extract_fundamentals(
    companyfacts_archive: Path,
    security_master: SecurityMasterResult,
    spy_trading_days_csv: Path,
    *,
    start_date: date = date(2020, 1, 1),
    end_date: date = date(2025, 12, 31),
    max_json_member_bytes: int = 512 * 1024 * 1024,
) -> FundamentalExportResult:
    """Extract accession-aware, coherent PIT snapshots from SEC companyfacts."""
    if end_date < start_date:
        raise ValueError("fundamental end date precedes start date")
    spy_days = _spy_dates(spy_trading_days_csv)
    if spy_days[-1] < end_date:
        raise ValueError("SPY trading-day input does not cover the requested cutoff")
    counters: Counter[str] = Counter()
    handle, members = _zip_members(companyfacts_archive, max_member_bytes=max_json_member_bytes)
    cik_members = _companyfacts_members(members)
    resolved_ciks = sorted({row.cik for row in security_master.rows})
    by_cik: dict[str, tuple[_Candidate, ...]] = {}
    missing_ciks: set[str] = set()
    try:
        for cik in resolved_ciks:
            info = cik_members.get(cik)
            if info is None:
                missing_ciks.add(cik)
                continue
            by_cik[cik] = _candidates_for_cik(
                _json_member(handle, info),
                cik=cik,
                acceptances=security_master.acceptance_by_cik.get(cik, {}),
                spy_days=spy_days,
                start_date=start_date,
                end_date=end_date,
                counters=counters,
            )
    finally:
        handle.close()

    windows = _cik_windows(security_master.rows)
    all_rows: list[FundamentalRow] = []
    all_audit: list[FundamentalAuditRow] = []
    statement_symbols: dict[str, set[str]] = defaultdict(set)
    no_facts: set[str] = set()
    for ticker, cik_values in sorted(windows.items()):
        selected: list[_Candidate] = []
        for cik, (lower, upper) in cik_values.items():
            for candidate in by_cik.get(cik, ()):
                if lower is not None and candidate.public_date < lower:
                    continue
                if upper is not None and candidate.public_date > upper:
                    continue
                selected.append(candidate)
        ticker_rows, ticker_audit = _materialize_ticker(ticker, selected, counters)
        if not ticker_rows:
            no_facts.add(ticker)
            continue
        all_rows.extend(ticker_rows)
        all_audit.extend(ticker_audit)
        for row in ticker_rows:
            statement_symbols[row.statement_type].add(ticker)

    paired = sorted(
        zip(all_rows, all_audit, strict=True),
        key=lambda pair: (pair[0].ticker, pair[0].statement_type, pair[0].public_date, pair[0].period_end),
    )
    rows = tuple(pair[0] for pair in paired)
    audit_rows = tuple(pair[1] for pair in paired)
    visible_keys = [(row.ticker, row.statement_type, row.period_end, row.public_date) for row in rows]
    if len(visible_keys) != len(set(visible_keys)):
        raise ValueError("normalized fundamentals contain duplicate visible keys")
    if any(row.public_date <= row.period_end for row in rows):
        raise ValueError("normalized fundamentals contain a non-PIT public date")
    if len(rows) != len(audit_rows):
        raise ValueError("fundamental output and audit sidecar differ in length")

    resolved_symbols = {row.ticker for row in security_master.rows}
    excluded_symbols = {row.ticker for row in security_master.exclusions}
    union = set(security_master.membership_union)
    if resolved_symbols | excluded_symbols != union or resolved_symbols & excluded_symbols:
        raise ValueError("security-master resolved/excluded accounting is incomplete")
    accounted_percent = 100.0 * len(resolved_symbols | excluded_symbols) / len(union)
    if accounted_percent < 95.0:
        raise ValueError("fewer than 95% of membership tickers resolve or have a closed exclusion")

    acceptance_accessions = {
        audit.accession_number for audit in audit_rows if audit.public_date_basis == "acceptance_datetime"
    }
    fallback_accessions = {
        audit.accession_number for audit in audit_rows if audit.public_date_basis == "filed_date_fallback"
    }
    coverage: dict[str, Any] = {
        "membership_union_symbol_count": len(union),
        "resolved_symbol_count": len(resolved_symbols),
        "resolved_cik_percentage": round(100.0 * len(resolved_symbols) / len(union), 8),
        "explicitly_excluded_symbol_count": len(excluded_symbols),
        "ambiguous_exclusion_symbol_count": len(
            {row.ticker for row in security_master.exclusions if row.reason.startswith("ambiguous_")}
        ),
        "resolved_or_closed_exclusion_percentage": round(accounted_percent, 8),
        "companyfacts_missing_cik_count": len(missing_ciks),
        "no_fundamental_rows_symbol_count": len(no_facts),
        "quarterly_symbol_count": len(statement_symbols["quarterly"]),
        "annual_symbol_count": len(statement_symbols["annual"]),
        "balance_symbol_count": len(statement_symbols["balance"]),
        "fundamental_row_count": len(rows),
        "accession_join_row_count": sum(
            audit.public_date_basis == "acceptance_datetime" for audit in audit_rows
        ),
        "accession_join_unique_count": len(acceptance_accessions),
        "filed_date_fallback_count": sum(
            audit.public_date_basis == "filed_date_fallback" for audit in audit_rows
        ),
        "filed_date_fallback_unique_count": len(fallback_accessions),
        "missing_submission_fragment_count": security_master.missing_submission_fragments,
        **dict(sorted(counters.items())),
    }
    return FundamentalExportResult(
        rows=rows,
        audit_rows=audit_rows,
        coverage=coverage,
        companyfacts_archive_sha256=sha256_file(companyfacts_archive),
    )
