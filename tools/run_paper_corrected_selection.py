"""Run the committed offline #101/#102 corrected-selector selection."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import socket
import sys


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tests/paper_verification/corrected_selectors.txt"


def _forbid_network(*args, **kwargs):
    raise AssertionError("offline paper regression attempted network access")


def main() -> int:
    raw = MANIFEST.read_bytes()
    selectors = [line.strip() for line in raw.decode("utf-8").splitlines() if line.strip()]
    if len(selectors) != len(set(selectors)) or len(selectors) != 39:
        raise RuntimeError("expected 39 distinct paper corrected selectors")
    for selector in selectors:
        relative = Path(selector.split("::", 1)[0])
        if (
            relative.is_absolute()
            or not relative.parts
            or relative.parts[0] != "tests"
            or not (ROOT / relative).is_file()
        ):
            raise RuntimeError(f"invalid paper selector: {selector}")

    socket.socket.connect = _forbid_network
    socket.socket.connect_ex = _forbid_network
    socket.create_connection = _forbid_network
    import pytest

    print(
        "PAPER_CORRECTED_SELECTION count=39 sha256="
        + hashlib.sha256(raw).hexdigest(),
        flush=True,
    )
    os.chdir(ROOT)
    return int(pytest.main(["-q", "-x", "-o", "addopts=", *sys.argv[1:], *selectors]))


if __name__ == "__main__":
    raise SystemExit(main())
