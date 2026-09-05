"""Build-time verification for the installed PIT optimizer V5 evaluator source map."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .contracts import evaluator_source_map_v5, evaluator_source_sha256


def verify_installed_evaluator_source_v5(
    *,
    source_root: Path,
    expected_sha256: str,
) -> str:
    """Recompute the canonical source-map identity from installed image bytes."""

    if (
        type(expected_sha256) is not str
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        raise ValueError("expected evaluator source identity is invalid")
    observed = evaluator_source_sha256(evaluator_source_map_v5(source_root))
    if observed != expected_sha256:
        raise ValueError("installed evaluator source differs from the expected source map")
    return observed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False, add_help=False)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    return parser


def main(argv: tuple[str, ...] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        verify_installed_evaluator_source_v5(
            source_root=arguments.source_root,
            expected_sha256=arguments.expected_sha256,
        )
        return 0
    except BaseException:
        return 3


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys.argv[1:])))


__all__ = ["main", "verify_installed_evaluator_source_v5"]
