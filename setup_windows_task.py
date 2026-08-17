"""Register (or update) the CANSLIM scheduler as a Windows Scheduled Task.

Creates a task named "CANSLIM-Scheduler" that:
  - Runs at 09:00 ET every weekday (Mon–Fri)
  - Uses the same Python interpreter that runs this script
  - Starts in the project directory so relative paths resolve correctly
  - Logs stdout/stderr to scheduler_log.txt in the project root

Usage:
    python setup_windows_task.py            # register/update the task
    python setup_windows_task.py --remove   # delete the task
    python setup_windows_task.py --status   # show current task status

Requirements:
    - Run from an Administrator command prompt (schtasks needs elevated rights)
    - Python must be on the system PATH (verify with: where python)

After setup, the scheduler runs automatically at 09:00 ET on weekdays.
You can also start/stop it manually from Task Scheduler (taskschd.msc).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

TASK_NAME = "CANSLIM-Scheduler"
PROJECT_DIR = Path(__file__).resolve().parent
PYTHON_EXE = sys.executable
SCHEDULER_SCRIPT = PROJECT_DIR / "scheduler.py"
LOG_FILE = PROJECT_DIR / "scheduler_log.txt"


def _schtasks(*args: str) -> tuple[int, str]:
    """Run schtasks.exe and return (returncode, output)."""
    cmd = ["schtasks"] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stdout + result.stderr
    return result.returncode, output.strip()


def register_task() -> None:
    """Create or replace the CANSLIM-Scheduler Windows task."""
    # Build the action command.  We wrap in cmd /c so stdout redirects work.
    # The redirect ensures we can read logs even when the task runs in background.
    action_cmd = (
        f'cmd /c "{PYTHON_EXE}" "{SCHEDULER_SCRIPT}" >> "{LOG_FILE}" 2>&1'
    )

    rc, out = _schtasks(
        "/Create",
        "/F",                          # Force overwrite if already exists
        "/TN", TASK_NAME,
        "/TR", action_cmd,
        "/SC", "WEEKLY",
        "/D", "MON,TUE,WED,THU,FRI",
        "/ST", "09:00",               # 09:00 local time — set your PC clock to ET
        "/RL", "HIGHEST",             # Run with highest privileges
        "/RU", os.environ.get("USERNAME", ""),  # Current user
    )

    if rc == 0:
        print(f"[OK] Task '{TASK_NAME}' registered successfully.")
        print("     Runs: weekdays at 09:00 (set PC timezone to US/Eastern)")
        print(f"     Log:  {LOG_FILE}")
        print(f"\nTo verify: schtasks /Query /TN {TASK_NAME} /FO LIST")
        print(f"To run now: schtasks /Run /TN {TASK_NAME}")
    else:
        print(f"[ERROR] Failed to register task (exit {rc}):")
        print(out)
        print("\nTip: Run this script from an Administrator command prompt.")


def remove_task() -> None:
    """Delete the CANSLIM-Scheduler task if it exists."""
    rc, out = _schtasks("/Delete", "/TN", TASK_NAME, "/F")
    if rc == 0:
        print(f"[OK] Task '{TASK_NAME}' removed.")
    else:
        print("[WARN] Could not remove task (it may not exist):")
        print(out)


def show_status() -> None:
    """Print current task status."""
    rc, out = _schtasks("/Query", "/TN", TASK_NAME, "/FO", "LIST")
    if rc == 0:
        print(out)
    else:
        print(f"Task '{TASK_NAME}' not found. Run without --remove to register it.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CANSLIM Windows Task Scheduler setup")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--remove", action="store_true", help="Delete the scheduled task")
    group.add_argument("--status", action="store_true", help="Show current task status")
    args = parser.parse_args()

    if args.remove:
        remove_task()
    elif args.status:
        show_status()
    else:
        register_task()
