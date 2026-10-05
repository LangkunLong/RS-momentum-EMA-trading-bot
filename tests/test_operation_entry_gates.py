"""Offline checks for manifest gates that run before client construction."""

from unittest.mock import patch
import subprocess
import sys

import scheduler
import verify_paper_trading
from config import settings


def test_lifecycle_execute_without_manifest_is_refused_before_client_creation() -> None:
    with (
        patch.object(verify_paper_trading, "_get_trading_client") as client_factory,
        patch.object(settings, "load_runtime_credentials") as load_credentials,
    ):
        assert verify_paper_trading.main(execute=True) == 2

    client_factory.assert_not_called()
    load_credentials.assert_not_called()


def test_observer_without_manifest_is_refused_before_scheduler_start() -> None:
    with (
        patch.object(scheduler, "run_scheduler") as run,
        patch.object(settings, "load_runtime_credentials") as load_credentials,
    ):
        result = scheduler.main(
            [
                "--dry-run",
                "--now",
                "--session",
                "--observe-health",
                "--observe-stop-at=2026-10-05T16:05:00-04:00",
                "--observe-hard-deadline-at=2026-10-05T16:06:00-04:00",
            ]
        )

    assert result == 2
    run.assert_not_called()
    load_credentials.assert_not_called()


def test_importing_bounded_entry_modules_does_not_load_dotenv_or_credentials() -> None:
    code = 'import os; os.environ.update({"ALPACA_API_KEY":"sentinel-key","ALPACA_SECRET_KEY":"sentinel-secret","FMP_API_KEY":"sentinel-fmp"}); import dotenv; dotenv.load_dotenv = lambda *a, **k: (_ for _ in ()).throw(AssertionError("dotenv load ran during import")); import scheduler; import verify_paper_trading; from config import settings; assert not settings._RUNTIME_CREDENTIALS_LOADED; assert settings.ALPACA_API_KEY == ""; assert settings.ALPACA_SECRET_KEY == ""; assert settings.FMP_API_KEY == ""; print("safe imports")'
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "safe imports"
