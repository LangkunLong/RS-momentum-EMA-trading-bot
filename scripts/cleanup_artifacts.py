"""Organize and clean generated workspace artifacts.

Moves useful generated outputs into ``.artifacts/`` and removes stale cache /
temporary files that do not belong in the repo root.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings


ARTIFACTS = ROOT / settings.ARTIFACTS_DIR
SCAN_RESULTS = ROOT / settings.SCAN_RESULTS_DIR
BACKTEST_RESULTS = ROOT / settings.BACKTEST_RESULTS_DIR
CACHE_ROOT = ROOT / settings.CACHE_DIR


def _ensure_dirs() -> None:
    for path in (
        ARTIFACTS,
        SCAN_RESULTS,
        BACKTEST_RESULTS,
        CACHE_ROOT / "rs_score_cache",
        CACHE_ROOT / "ticker_cache",
        CACHE_ROOT / "fundamentals",
        ARTIFACTS / "pytest" / "cache",
        ARTIFACTS / "pytest" / "tmp",
        ARTIFACTS / "legacy",
    ):
        path.mkdir(parents=True, exist_ok=True)


def _move_contents(src: Path, dest: Path) -> int:
    if not src.exists() or src.resolve() == dest.resolve():
        return 0
    dest.mkdir(parents=True, exist_ok=True)
    moved = 0
    for child in src.iterdir():
        target = dest / child.name
        if target.exists():
            continue
        try:
            shutil.move(str(child), str(target))
        except (PermissionError, OSError):
            if child.is_dir():
                shutil.copytree(child, target, dirs_exist_ok=True)
                shutil.rmtree(child, ignore_errors=True)
            else:
                shutil.copy2(child, target)
                try:
                    child.unlink()
                except OSError:
                    pass
        moved += 1
    try:
        src.rmdir()
    except OSError:
        pass
    return moved


def _move_file(src: Path, dest_dir: Path) -> bool:
    if not src.exists():
        return False
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / src.name
    if target.exists():
        return False
    try:
        shutil.move(str(src), str(target))
    except (PermissionError, OSError):
        shutil.copy2(src, target)
        try:
            src.unlink()
        except OSError:
            pass
    return True


def cleanup() -> dict[str, int]:
    _ensure_dirs()

    moved = 0
    removed = 0

    moved += _move_contents(ROOT / "scan_results", SCAN_RESULTS)
    moved += _move_contents(ROOT / "rs_score_cache", CACHE_ROOT / "rs_score_cache")
    moved += _move_contents(ROOT / "ticker_cache", CACHE_ROOT / "ticker_cache")
    moved += _move_contents(ROOT / "fundamentals_cache", CACHE_ROOT / "fundamentals")

    for path in ROOT.glob("backtest_results_*.csv"):
        moved += int(_move_file(path, BACKTEST_RESULTS))
    moved += int(_move_file(ROOT / "backtest_output.txt", BACKTEST_RESULTS))

    removable_dirs = []
    removable_dirs.extend(ROOT.glob("pytest-cache-files-*"))
    removable_dirs.extend(ROOT.glob(".tmp_*"))
    removable_dirs.extend(ROOT.glob("tmp_*"))
    removable_dirs.extend(
        path
        for path in (
            ROOT / ".pytest_cache",
            ROOT / ".pytest_cache_local",
            ROOT / "__pycache__",
            ROOT / "execution_audit",
        )
        if path.exists()
    )

    for path in removable_dirs:
        if path.exists() and path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            removed += 1

    removable_files = []
    removable_files.extend(ROOT.glob("tmp_probe.sqlite3*"))
    removable_files.extend(
        path
        for path in (
            ROOT / ".coverage",
            ROOT / "=0.4",
            ROOT / "=3.7",
            ROOT / "=5.0",
            ROOT / "=8.0",
        )
        if path.exists()
    )

    for path in removable_files:
        try:
            path.unlink(missing_ok=True)
            removed += 1
        except OSError:
            pass

    return {"moved": moved, "removed": removed}


if __name__ == "__main__":
    summary = cleanup()
    print(f"Cleanup complete: moved={summary['moved']} removed={summary['removed']}")
