"""Copy-on-write refresh of PIT fact-cache fundamentals.

The price/volume/RS/base fields in a finalized fact cache are independent of
the SEC fundamentals source.  This utility reuses those immutable fields and
recomputes only the fundamental columns against a corrected PIT bundle.  It is
deliberately resumable and writes only a ``.partial`` output until the final
integrity/content checks pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from typing import Any, Mapping

# Running a file under ``tools`` places that directory first on sys.path;
# explicitly bind imports to this checkout rather than an ambient package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.pit_data import PITDataBundle, sha256_file
from core.pit_diagnosis.catalog import fixed_partitions
from core.pit_diagnosis.fact_cache import (
    _HASHED_ROW_COLUMNS,
    _SCHEMA_SHA256,
    FactCacheBuilder,
    _fundamental_values,
    _state_at,
)
from core.pit_diagnosis.rulebook import canonical_sha256, load_rulebook
from core.pit_diagnosis.supplemental import UnavailableSupplementalPITProvider


_DIGEST = "0123456789abcdef"
_FUNDAMENTAL_COLUMNS = (
    "current_eps", "prior_year_eps", "current_sales", "prior_year_sales",
    "annual_eps_1", "annual_eps_2", "annual_eps_3", "annual_eps_4",
    "net_income", "total_stockholders_equity", "current_eps_yoy", "sales_yoy", "roe", "shares_outstanding",
)
_REFRESH_COLUMNS = frozenset({*_FUNDAMENTAL_COLUMNS, "bundle_sha256", "latest_fundamental_public_date", "availability_bitset", "row_sha256"})


def _digest(value: str) -> str:
    if len(value) != 64 or any(char not in _DIGEST for char in value.lower()) or value != value.lower():
        raise argparse.ArgumentTypeError("SHA-256 must be lowercase hexadecimal")
    return value


def _absolute(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("path must be absolute")
    return path


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def _metadata(connection: sqlite3.Connection) -> dict[str, str]:
    return {str(row[0]): str(row[1]) for row in connection.execute("SELECT key,value FROM metadata")}


def _write_checkpoint(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(dict(payload), sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _load_checkpoint(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("refresh checkpoint is invalid")
    return value


def _write_progress(path: Path, payload: Mapping[str, object]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(dict(payload), sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _content_sha256(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for row in connection.execute("SELECT row_sha256 FROM session_facts ORDER BY session,symbol"):
        digest.update(str(row[0]).encode("ascii"))
    return digest.hexdigest()


def _nonfundamental_signature(row: Mapping[str, object]) -> tuple[object, ...]:
    return tuple(row[column] for column in _HASHED_ROW_COLUMNS if column not in _REFRESH_COLUMNS)


def refresh(
    *,
    source_cache: Path,
    source_cache_sha256: str,
    bundle: PITDataBundle,
    rulebook: object,
    output: Path,
    checkpoint: Path,
    progress: Path,
    resume: bool,
) -> str:
    if sha256_file(source_cache) != source_cache_sha256:
        raise ValueError("source fact cache SHA-256 does not match")
    if output.exists() and not resume:
        raise ValueError("output already exists; use --resume")
    partial = Path(f"{output}.partial")
    partial_state = partial.exists() or checkpoint.exists() or progress.exists()
    if partial_state and not resume:
        raise ValueError("refresh partial state already exists; use --resume")

    source = _connect(source_cache)
    try:
        source_meta = _metadata(source)
        if source_meta.get("status") != "complete" or source_meta.get("schema_sha256") != _SCHEMA_SHA256:
            raise ValueError("source fact cache is not a complete recognized schema")
        source_identity = json.loads(source_meta["identity"])
        if not isinstance(source_identity, dict):
            raise ValueError("source fact-cache identity is invalid")
        source_bundle_sha = str(source_identity["bundle_sha256"])
        sessions = [str(row[0]) for row in source.execute("SELECT DISTINCT session FROM session_facts ORDER BY session")]
        if not sessions:
            raise ValueError("source fact cache has no sessions")
    finally:
        source.close()

    builder = FactCacheBuilder(
        bundle=bundle,
        rulebook=rulebook,
        partitions=fixed_partitions(),
        output_path=output,
        checkpoint_path=checkpoint,
        progress_path=progress,
        supplemental_provider=UnavailableSupplementalPITProvider(),
    )
    target_identity = dict(builder.identity.fields)
    target_identity_sha = builder.identity.sha256
    target_bundle_sha = str(target_identity["bundle_sha256"])
    if target_bundle_sha == source_bundle_sha:
        raise ValueError("source and target bundles are identical")

    next_index = 0
    if resume and partial.exists():
        connection = _connect(partial)
        try:
            row_count, distinct_sessions = connection.execute("SELECT count(*),count(distinct session) FROM session_facts").fetchone()
            if int(row_count) != int(source_meta.get("row_count", row_count)) or int(distinct_sessions) != len(sessions):
                raise ValueError("refresh partial row coverage is invalid")
            if connection.execute("SELECT count(*) FROM session_facts WHERE bundle_sha256=?", (target_bundle_sha,)).fetchone()[0] != row_count:
                raise ValueError("refresh partial bundle identity is invalid")
        finally:
            connection.close()
        if checkpoint.exists():
            state = _load_checkpoint(checkpoint)
            if state.get("source_cache_sha256") != source_cache_sha256 or state.get("target_identity_sha256") != target_identity_sha:
                raise ValueError("refresh checkpoint identity mismatch")
            next_index = int(state.get("next_session_index", 0))
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_cache, partial)
        os.chmod(partial, 0o644)
        connection = _connect(partial)
        try:
            old_count = connection.execute("SELECT count(*) FROM session_facts WHERE bundle_sha256=?", (source_bundle_sha,)).fetchone()[0]
            if int(old_count) != int(connection.execute("SELECT count(*) FROM session_facts").fetchone()[0]):
                raise ValueError("source cache contains mixed bundle identities")
            connection.execute("UPDATE session_facts SET bundle_sha256=? WHERE bundle_sha256=?", (target_bundle_sha, source_bundle_sha))
            if connection.total_changes != old_count:
                raise ValueError("refresh did not rebind every source row")
            connection.commit()
            _write_checkpoint(checkpoint, {"source_cache_sha256": source_cache_sha256, "source_bundle_sha256": source_bundle_sha, "target_bundle_sha256": target_bundle_sha, "target_identity_sha256": target_identity_sha, "next_session_index": 0})
        finally:
            connection.close()

    states = builder._fundamental_states(sessions)
    placeholders = ",".join("?" for _ in _FUNDAMENTAL_COLUMNS)
    update_sql = (
        "UPDATE session_facts SET "
        + ",".join(f"{column}=?" for column in (*_FUNDAMENTAL_COLUMNS, "latest_fundamental_public_date", "availability_bitset", "row_sha256"))
        + " WHERE bundle_sha256=? AND rulebook_schema_version=? AND symbol=? AND session=?"
    )
    del placeholders
    connection = _connect(partial)
    try:
        for index in range(next_index, len(sessions)):
            session = sessions[index]
            rows = connection.execute("SELECT * FROM session_facts WHERE bundle_sha256=? AND session=? ORDER BY symbol", (target_bundle_sha, session)).fetchall()
            updates: list[tuple[object, ...]] = []
            for raw in rows:
                row = dict(raw)
                state, state_date = _state_at(states.get(str(row["symbol"]).upper(), []), session)
                fundamentals = _fundamental_values(state)
                original_nonfundamentals = _nonfundamental_signature(row)
                row.update(fundamentals)
                row["latest_fundamental_public_date"] = state_date
                row["availability_bitset"] = (int(row["availability_bitset"]) & ~2) | (2 if state is not None else 0)
                if _nonfundamental_signature(row) != original_nonfundamentals:
                    raise ValueError(f"nonfundamental field changed for {row['symbol']} {session}")
                row["row_sha256"] = canonical_sha256({column: row[column] for column in _HASHED_ROW_COLUMNS})
                updates.append(tuple(row[column] for column in (*_FUNDAMENTAL_COLUMNS, "latest_fundamental_public_date", "availability_bitset", "row_sha256", "bundle_sha256", "rulebook_schema_version", "symbol", "session")))
            connection.executemany(update_sql, updates)
            connection.commit()
            next_index = index + 1
            payload = {"phase": "session_complete", "session": session, "rows": len(rows), "next_session_index": next_index, "target_identity_sha256": target_identity_sha}
            _write_progress(progress, payload)
            _write_checkpoint(checkpoint, {"source_cache_sha256": source_cache_sha256, "source_bundle_sha256": source_bundle_sha, "target_bundle_sha256": target_bundle_sha, "target_identity_sha256": target_identity_sha, "next_session_index": next_index})
        if next_index != len(sessions):
            raise ValueError("refresh did not cover every session")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("refreshed fact cache integrity check failed")
        connection.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('status','complete')")
        connection.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('identity_sha256',?)", (target_identity_sha,))
        connection.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('identity',?)", (json.dumps(target_identity, sort_keys=True, separators=(",", ":")),))
        connection.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('schema_version','2')")
        connection.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('schema_sha256',?)", (_SCHEMA_SHA256,))
        connection.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('content_sha256',?)", (_content_sha256(connection),))
        connection.commit()
        connection.execute("VACUUM")
        connection.commit()
    finally:
        connection.close()
    os.replace(partial, output)
    os.chmod(output, 0o444)
    checkpoint.unlink(missing_ok=True)
    progress.unlink(missing_ok=True)
    return sha256_file(output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-cache", type=_absolute, required=True)
    parser.add_argument("--source-cache-sha256", type=_digest, required=True)
    parser.add_argument("--pit-bundle", type=_absolute, required=True)
    parser.add_argument("--pit-bundle-sha256", type=_digest, required=True)
    parser.add_argument("--rulebook", type=_absolute, required=True)
    parser.add_argument("--output", type=_absolute, required=True)
    parser.add_argument("--checkpoint", type=_absolute, required=True)
    parser.add_argument("--progress", type=_absolute, required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    rulebook = load_rulebook(args.rulebook)
    with PITDataBundle(args.pit_bundle, expected_sha256=args.pit_bundle_sha256) as bundle:
        digest = refresh(source_cache=args.source_cache, source_cache_sha256=args.source_cache_sha256, bundle=bundle, rulebook=rulebook, output=args.output, checkpoint=args.checkpoint, progress=args.progress, resume=args.resume)
    print(json.dumps({"output": str(args.output), "sha256": digest}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
