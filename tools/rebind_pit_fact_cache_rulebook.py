"""Copy-on-write rebind of a PIT fact cache to an equivalent rulebook.

This is only valid when the rulebook change affects interpretation, not the
cached scalar facts or schema.  The utility preserves every row byte-level
value, verifies the logical row-content digest, and changes only the sealed
fact-cache identity's rulebook digest.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.pit_diagnosis.fact_cache import _SCHEMA_SHA256, _SCHEMA_VERSION  # noqa: E402
from core.pit_diagnosis.rulebook import canonical_sha256, load_rulebook  # noqa: E402


def _digest(value: str) -> str:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise argparse.ArgumentTypeError("expected a lowercase SHA-256")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _logical_content(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for row in connection.execute("SELECT row_sha256 FROM session_facts ORDER BY session,symbol"):
        digest.update(str(row[0]).encode("ascii"))
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-sha256", type=_digest, required=True)
    parser.add_argument("--rulebook", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    output = args.output.resolve()
    if not source.is_file() or source.is_symlink():
        raise ValueError("source fact cache must be a regular file")
    if output.exists():
        raise ValueError("output already exists")
    if _sha256(source) != args.source_sha256:
        raise ValueError("source fact-cache SHA-256 does not match")
    rulebook = load_rulebook(args.rulebook.resolve())
    partial = Path(f"{output}.partial")
    if partial.exists():
        raise ValueError("partial output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, partial)
    try:
        connection = sqlite3.connect(partial)
        connection.row_factory = sqlite3.Row
        metadata = {str(row[0]): str(row[1]) for row in connection.execute("SELECT key,value FROM metadata")}
        if metadata.get("status") != "complete" or metadata.get("schema_version") != _SCHEMA_VERSION or metadata.get("schema_sha256") != _SCHEMA_SHA256:
            raise ValueError("source fact cache is not a complete recognized schema")
        identity = json.loads(metadata["identity"])
        if not isinstance(identity, dict):
            raise ValueError("source fact-cache identity is malformed")
        if identity.get("rulebook_version") != rulebook.version:
            raise ValueError("rulebook version differs from fact-cache identity")
        old_content = _logical_content(connection)
        if old_content != metadata.get("content_sha256"):
            raise ValueError("source fact-cache logical content digest is stale")
        updated_identity = dict(identity)
        updated_identity["rulebook_sha256"] = rulebook.sha256
        identity_sha256 = canonical_sha256(updated_identity)
        connection.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('identity_sha256',?)", (identity_sha256,))
        connection.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('identity',?)", (json.dumps(updated_identity, sort_keys=True, separators=(",", ":")),))
        connection.commit()
        if _logical_content(connection) != old_content:
            raise ValueError("rulebook rebind changed fact rows")
        connection.execute("PRAGMA query_only=ON")
        connection.close()
        os.replace(partial, output)
        os.chmod(output, 0o444)
        print(json.dumps({"path": str(output), "sha256": _sha256(output), "identity_sha256": identity_sha256, "content_sha256": old_content, "rulebook_sha256": rulebook.sha256}, sort_keys=True))
        return 0
    except Exception:
        try:
            connection.close()
        except (NameError, sqlite3.Error):
            pass
        partial.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    main()
