"""Safety and reporting tests for Windows scheduled-task configuration."""

from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

import paper_trading_console as console
import setup_windows_task as task_setup


def _xml_values(xml_text: str) -> dict[str, str]:
    root = ElementTree.fromstring(xml_text)
    namespace = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    paths = {
        "command": ".//t:Exec/t:Command",
        "arguments": ".//t:Exec/t:Arguments",
        "working_directory": ".//t:Exec/t:WorkingDirectory",
        "multiple_instances": ".//t:MultipleInstancesPolicy",
        "start_when_available": ".//t:StartWhenAvailable",
        "execution_limit": ".//t:ExecutionTimeLimit",
        "run_level": ".//t:RunLevel",
    }
    return {
        key: str(root.find(path, namespace).text)
        for key, path in paths.items()
    }


def test_default_task_xml_is_direct_bounded_zero_budget_dry_run() -> None:
    values = _xml_values(task_setup._build_task_xml(dry_run=True))

    assert values["command"] == str(task_setup.PYTHON_EXE)
    assert values["working_directory"] == str(task_setup.PROJECT_DIR)
    assert "--dry-run" in values["arguments"]
    assert "--enable-orders" not in values["arguments"]
    assert "--session" in values["arguments"]
    assert "--fmp-daily-budget 0" in values["arguments"]
    assert "--task-log .artifacts/logs/scheduler.log" in values["arguments"]
    assert values["multiple_instances"] == "IgnoreNew"
    assert values["start_when_available"] == "true"
    assert values["execution_limit"] == "PT8H"
    assert values["run_level"] == "LeastPrivilege"


def test_enable_orders_task_xml_is_explicit_and_uses_conservative_free_plan_budget() -> None:
    values = _xml_values(task_setup._build_task_xml(dry_run=False))

    assert "--enable-orders" in values["arguments"]
    assert "--dry-run" not in values["arguments"]
    assert "--fmp-daily-budget 20" in values["arguments"]


def test_register_task_defaults_to_dry_run_and_returns_success() -> None:
    captured: dict[str, str] = {}

    def fake_schtasks(*args: str) -> tuple[int, str]:
        xml_path = Path(args[args.index("/XML") + 1])
        captured["xml"] = xml_path.read_text(encoding="utf-16")
        captured["args"] = " ".join(args)
        return 0, "created"

    with patch("setup_windows_task._schtasks", side_effect=fake_schtasks):
        rc = task_setup.register_task()

    assert rc == 0
    assert "/Create /F /TN CANSLIM-Scheduler /XML" in captured["args"]
    assert "--dry-run" in _xml_values(captured["xml"])["arguments"]


def test_console_requires_explicit_enable_orders_flag() -> None:
    with patch("paper_trading_console.register_task", return_value=0) as register:
        assert console.main(["install-task"]) == 0
        register.assert_called_once_with(dry_run=True)

    with patch("paper_trading_console.register_task", return_value=0) as register:
        assert console.main(["install-task", "--enable-orders"]) == 0
        register.assert_called_once_with(dry_run=False)
