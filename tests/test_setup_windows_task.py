"""Safety and reporting tests for Windows scheduled-task configuration."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import paper_trading_console as console
import setup_windows_task as task_setup


def test_default_task_action_is_dry_run() -> None:
    action = task_setup._build_action_command(dry_run=True)

    assert "scheduler.py" in action
    assert "--dry-run" in action


def test_enable_orders_task_action_omits_dry_run() -> None:
    action = task_setup._build_action_command(dry_run=False)

    assert "scheduler.py" in action
    assert "--dry-run" not in action


def test_task_action_sets_working_directory_and_creates_ignored_log_path(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project with spaces"
    project_dir.mkdir()
    probe = project_dir / "probe.py"
    probe.write_text("from pathlib import Path\nprint(Path.cwd())\n", encoding="utf-8")
    log_file = project_dir / ".artifacts" / "logs" / "scheduler.log"

    with (
        patch("setup_windows_task.PROJECT_DIR", project_dir),
        patch("setup_windows_task.PYTHON_EXE", sys.executable),
        patch("setup_windows_task.SCHEDULER_SCRIPT", probe),
        patch("setup_windows_task.LOG_FILE", log_file),
    ):
        action = task_setup._build_action_command(dry_run=False)

    result = subprocess.run(
        action,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert log_file.read_text(encoding="utf-8").strip() == str(project_dir)


def test_register_task_defaults_to_dry_run_and_returns_success() -> None:
    with patch("setup_windows_task._schtasks", return_value=(0, "created")) as schtasks:
        rc = task_setup.register_task()

    assert rc == 0
    args = schtasks.call_args.args
    action = args[args.index("/TR") + 1]
    assert "--dry-run" in action


def test_console_requires_explicit_enable_orders_flag() -> None:
    with patch("paper_trading_console.register_task", return_value=0) as register:
        assert console.main(["install-task"]) == 0
        register.assert_called_once_with(dry_run=True)

    with patch("paper_trading_console.register_task", return_value=0) as register:
        assert console.main(["install-task", "--enable-orders"]) == 0
        register.assert_called_once_with(dry_run=False)
