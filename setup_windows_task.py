"""Register (or update) the CANSLIM scheduler as a Windows Scheduled Task.

Creates a task named "CANSLIM-Scheduler" that:
  - Runs at 09:00 ET every weekday (Mon–Fri)
  - Uses the project virtual environment in unbuffered mode
  - Starts in the project directory so relative paths resolve correctly
  - Logs stdout/stderr under the ignored .artifacts/logs directory

Usage:
    python setup_windows_task.py            # register/update a dry-run task
    python setup_windows_task.py --enable-orders  # enable paper order submission
    python setup_windows_task.py --remove   # delete the task
    python setup_windows_task.py --status   # show current task status

Requirements:
    - Run this script with the stable project virtual-environment interpreter
    - The current user must be allowed to create a least-privilege scheduled task

After setup, the scheduler runs automatically at 09:00 ET on weekdays.
You can also start/stop it manually from Task Scheduler (taskschd.msc).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

TASK_NAME = "CANSLIM-Scheduler"
PROJECT_DIR = Path(__file__).resolve().parent
PYTHON_EXE = PROJECT_DIR / ".venv" / "Scripts" / "python.exe"
LOG_FILE = PROJECT_DIR / ".artifacts" / "logs" / "scheduler.log"
_TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
# Keep unattended live scans to a small slice of FMP's free daily allowance.
_LIVE_FMP_DAILY_BUDGET = 20


def _schtasks(*args: str) -> tuple[int, str]:
    """Run schtasks.exe and return (returncode, output)."""
    cmd = ["schtasks"] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stdout + result.stderr
    return result.returncode, output.strip()


def _task_user() -> str:
    """Return the current interactive Windows identity."""
    username = os.environ.get("USERNAME", "")
    domain = os.environ.get("USERDOMAIN", "")
    return f"{domain}\\{username}" if domain and username else username


def _task_element(parent: ElementTree.Element, name: str, text: str) -> None:
    element = ElementTree.SubElement(parent, f"{{{_TASK_NAMESPACE}}}{name}")
    element.text = text


def _build_task_xml(*, dry_run: bool) -> str:
    """Build a direct-Python task definition with bounded safe defaults."""
    ElementTree.register_namespace("", _TASK_NAMESPACE)
    root = ElementTree.Element(
        f"{{{_TASK_NAMESPACE}}}Task",
        {"version": "1.2"},
    )

    registration = ElementTree.SubElement(
        root,
        f"{{{_TASK_NAMESPACE}}}RegistrationInfo",
    )
    _task_element(registration, "Date", datetime.now().isoformat(timespec="seconds"))
    _task_element(registration, "Author", _task_user())
    _task_element(registration, "URI", f"\\{TASK_NAME}")

    principals = ElementTree.SubElement(root, f"{{{_TASK_NAMESPACE}}}Principals")
    principal = ElementTree.SubElement(
        principals,
        f"{{{_TASK_NAMESPACE}}}Principal",
        {"id": "Author"},
    )
    _task_element(principal, "UserId", _task_user())
    _task_element(principal, "LogonType", "InteractiveToken")
    _task_element(principal, "RunLevel", "LeastPrivilege")

    task_settings = ElementTree.SubElement(root, f"{{{_TASK_NAMESPACE}}}Settings")
    _task_element(task_settings, "MultipleInstancesPolicy", "IgnoreNew")
    _task_element(task_settings, "DisallowStartIfOnBatteries", "false")
    _task_element(task_settings, "StopIfGoingOnBatteries", "false")
    _task_element(task_settings, "StartWhenAvailable", "true")
    _task_element(task_settings, "AllowHardTerminate", "true")
    _task_element(task_settings, "ExecutionTimeLimit", "PT8H")
    _task_element(task_settings, "Enabled", "true")

    triggers = ElementTree.SubElement(root, f"{{{_TASK_NAMESPACE}}}Triggers")
    calendar = ElementTree.SubElement(
        triggers,
        f"{{{_TASK_NAMESPACE}}}CalendarTrigger",
    )
    _task_element(
        calendar,
        "StartBoundary",
        f"{datetime.now().date().isoformat()}T09:00:00",
    )
    schedule = ElementTree.SubElement(
        calendar,
        f"{{{_TASK_NAMESPACE}}}ScheduleByWeek",
    )
    _task_element(schedule, "WeeksInterval", "1")
    days = ElementTree.SubElement(schedule, f"{{{_TASK_NAMESPACE}}}DaysOfWeek")
    for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"):
        ElementTree.SubElement(days, f"{{{_TASK_NAMESPACE}}}{day}")

    actions = ElementTree.SubElement(
        root,
        f"{{{_TASK_NAMESPACE}}}Actions",
        {"Context": "Author"},
    )
    execution = ElementTree.SubElement(actions, f"{{{_TASK_NAMESPACE}}}Exec")
    mode = "--dry-run" if dry_run else "--enable-orders"
    arguments = (
        f"-u scheduler.py {mode} --session "
        "--task-log .artifacts/logs/scheduler.log"
    )
    fmp_budget = 0 if dry_run else _LIVE_FMP_DAILY_BUDGET
    arguments += f" --fmp-daily-budget {fmp_budget}"
    _task_element(execution, "Command", str(PYTHON_EXE))
    _task_element(execution, "Arguments", arguments)
    _task_element(execution, "WorkingDirectory", str(PROJECT_DIR))
    return ElementTree.tostring(root, encoding="unicode")


def register_task(*, dry_run: bool = True) -> int:
    """Create or replace the CANSLIM-Scheduler Windows task."""
    xml_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".xml",
            encoding="utf-16",
            delete=False,
        ) as task_file:
            task_file.write('<?xml version="1.0" encoding="UTF-16"?>\n')
            task_file.write(_build_task_xml(dry_run=dry_run))
            xml_path = Path(task_file.name)
        rc, out = _schtasks(
            "/Create",
            "/F",
            "/TN",
            TASK_NAME,
            "/XML",
            str(xml_path),
        )
    finally:
        if xml_path is not None:
            xml_path.unlink(missing_ok=True)

    if rc == 0:
        print(f"[OK] Task '{TASK_NAME}' registered successfully.")
        print(f"     Mode: {'dry run (no broker mutations)' if dry_run else 'paper orders enabled'}")
        print("     Runs: weekdays at 09:00 (set PC timezone to US/Eastern)")
        print(f"     Python: {PYTHON_EXE}")
        print(f"     CWD:    {PROJECT_DIR}")
        print(f"     Log:  {LOG_FILE}")
        print(f"\nTo verify: schtasks /Query /TN {TASK_NAME} /FO LIST")
        print(f"To run now: schtasks /Run /TN {TASK_NAME}")
        return 0
    else:
        print(f"[ERROR] Failed to register task (exit {rc}):")
        print(out)
        print("\nTip: Confirm the current user can create Task Scheduler entries.")
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
