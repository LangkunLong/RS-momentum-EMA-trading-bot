"""Lightweight shared names and canonical point-in-time provenance helpers."""

from __future__ import annotations

import hashlib
import json

PIT_PUBLIC_DATES_ATTR = "pit_public_date_by_period"
PIT_NON_TRADABLE_REFERENCE_SYMBOLS = ("IWM", "QQQ", "SPY")


def pit_canonical_json(value: object) -> str:
    """Return the stable compact JSON representation used by PIT provenance."""

    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))


def pit_canonical_json_bytes(value: object) -> bytes:
    """Return canonical PIT JSON as one newline-terminated UTF-8 record."""

    return (pit_canonical_json(value) + "\n").encode("utf-8")


def pit_canonical_json_sha256(value: object) -> str:
    """Hash the canonical newline-terminated PIT JSON representation."""

    return hashlib.sha256(pit_canonical_json_bytes(value)).hexdigest()

__all__ = [
    "PIT_NON_TRADABLE_REFERENCE_SYMBOLS",
    "PIT_PUBLIC_DATES_ATTR",
    "pit_canonical_json",
    "pit_canonical_json_bytes",
    "pit_canonical_json_sha256",
]
