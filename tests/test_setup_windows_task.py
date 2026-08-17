"""Safety and reporting tests for Windows scheduled-task configuration."""

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
