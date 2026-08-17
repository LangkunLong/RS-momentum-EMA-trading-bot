"""CLI safety tests for the provider-backed scanner."""

from unittest.mock import patch

import pytest

import enhanced_scanner


def test_help_exits_without_starting_provider_scan(capsys) -> None:
    with patch("enhanced_scanner.scan_for_canslim_stocks") as scan:
        with pytest.raises(SystemExit) as exc_info:
            enhanced_scanner.main(["--help"])

    assert exc_info.value.code == 0
    assert "CANSLIM" in capsys.readouterr().out
    scan.assert_not_called()
