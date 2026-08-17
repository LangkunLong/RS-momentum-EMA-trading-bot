"""Register (or update) the CANSLIM scheduler as a Windows Scheduled Task.

Creates a task named "CANSLIM-Scheduler" that:
  - Runs at 09:00 ET every weekday (Mon–Fri)
  - Uses the same Python interpreter that runs this script
  - Starts in the project directory so relative paths resolve correctly
  - Logs stdout/stderr under the ignored .artifacts/logs directory

Usage:
    python setup_windows_task.py            # register/update a dry-run task
    python setup_windows_task.py --enable-orders  # enable paper order submission
    python setup_windows_task.py --remove   # delete the task
    python setup_windows_task.py --status   # show current task status

Requirements:
    - Run from an Administrator command prompt (schtasks needs elevated rights)
    - Run this script with the stable project virtual-environment interpreter

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
LOG_FILE = PROJECT_DIR / ".artifacts" / "logs" / "scheduler.log"


def _schtasks(*args: str) -> tuple[int, str]:
    """Run schtasks.exe and return (returncode, output)."""
    cmd = ["schtasks"] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stdout + result.stderr
    return result.returncode, output.strip()


def _build_action_command(*, dry_run: bool) -> str:
    """Build the scheduled command, defaulting installation to observation only."""
    scheduler_argv = [PYTHON_EXE, str(SCHEDULER_SCRIPT)]
    if dry_run:
        scheduler_argv.append("--dry-run")

    project_dir = subprocess.list2cmdline([str(PROJECT_DIR)])
    log_dir = subprocess.list2cmdline([str(LOG_FILE.parent)])
    log_file = subprocess.list2cmdline([str(LOG_FILE)])
    scheduler_command = subprocess.list2cmdline(scheduler_argv)
    inner_command = (
        f"if not exist {log_dir} mkdir {log_dir} "
        f"&& cd /d {project_dir} "
        f"&& {scheduler_command} >> {log_file} 2>&1"
    )
    return f'cmd.exe /d /s /c "{inner_command}"'


def register_task(*, dry_run: bool = True) -> int:
    """Create or replace the CANSLIM-Scheduler Windows task."""
    action_cmd = _build_action_command(dry_run=dry_run)

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
        print(f"     Mode: {'dry run (no broker mutations)' if dry_run else 'paper orders enabled'}")
        print("     Runs: weekdays at 09:00 (set PC timezone to US/Eastern)")
        print(f"     Log:  {LOG_FILE}")
        print(f"\nTo verify: schtasks /Query /TN {TASK_NAME} /FO LIST")
        print(f"To run now: schtasks /Run /TN {TASK_NAME}")
        return 0
    else:
        print(f"[ERROR] Failed to register task (exit {rc}):")
        print(out)
        print("\nTip: Run this script from an Administrator command prompt.")
        return 1


def remove_task() -> int:
    """Delete the CANSLIM-Scheduler task if it exists."""
    rc, out = _schtasks("/Delete", "/TN", TASK_NAME, "/F")
    if rc == 0:
        print(f"[OK] Task '{TASK_NAME}' removed.")
        return 0
    else:
        print("[WARN] Could not remove task (it may not exist):")
        print(out)
        return 1


def show_status() -> int:
    """Print current task status."""
    rc, out = _schtasks("/Query", "/TN", TASK_NAME, "/FO", "LIST")
    if rc == 0:
        print(out)
        return 0
    else:
        print(f"Task '{TASK_NAME}' not found. Run without --remove to register it.")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CANSLIM Windows Task Scheduler setup")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--remove", action="store_true", help="Delete the scheduled task")
    group.add_argument("--status", action="store_true", help="Show current task status")
    group.add_argument(
        "--enable-orders",
        action="store_true",
        help="Register the order-enabled paper scheduler instead of the default dry run",
    )
    args = parser.parse_args()

    if args.remove:
        rc = remove_task()
    elif args.status:
        rc = show_status()
    else:
        rc = register_task(dry_run=not args.enable_orders)
    raise SystemExit(rc)
