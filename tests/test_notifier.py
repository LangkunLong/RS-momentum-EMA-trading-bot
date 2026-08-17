"""Unit tests for core.notifier — mocked SMTP, no real email sent."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from core.notifier import (
    notify_buy_filled,
    notify_cycle_summary,
    notify_entry_submitted,
    notify_sell_filled,
    send_email,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(**overrides):
    """Return a settings-like dict used to patch notifier settings."""
    defaults = {
        "NOTIFY_EMAIL_FROM": "bot@gmail.com",
        "NOTIFY_EMAIL_TO": "trader@gmail.com",
        "NOTIFY_EMAIL_PASSWORD": "secret",
        "STOP_LOSS_PCT": 0.07,
    }
    defaults.update(overrides)
    return defaults


def _patch_settings(**overrides):
    """Patch notifier.settings with the supplied values."""
    cfg = _cfg(**overrides)
    patches = {k: cfg[k] for k in cfg}
    return patch.multiple("core.notifier.settings", **patches)


# ---------------------------------------------------------------------------
# send_email
# ---------------------------------------------------------------------------


class TestSendEmail:
    def test_sends_via_smtp_when_configured(self):
        with _patch_settings():
            with patch("smtplib.SMTP") as mock_smtp_cls:
                mock_server = MagicMock()
                mock_smtp_cls.return_value.__enter__.return_value = mock_server

                result = send_email("Test subject", "Test body")

        assert result is True
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with("bot@gmail.com", "secret")
        mock_server.send_message.assert_called_once()

    def test_skips_silently_when_no_from_address(self):
        with _patch_settings(NOTIFY_EMAIL_FROM=""):
            with patch("smtplib.SMTP") as mock_smtp_cls:
                result = send_email("subject", "body")

        assert result is False
        mock_smtp_cls.assert_not_called()

    def test_skips_silently_when_no_to_address(self):
        with _patch_settings(NOTIFY_EMAIL_TO=""):
            with patch("smtplib.SMTP") as mock_smtp_cls:
                result = send_email("subject", "body")

        assert result is False
        mock_smtp_cls.assert_not_called()

    def test_skips_silently_when_no_password(self):
        with _patch_settings(NOTIFY_EMAIL_PASSWORD=""):
            with patch("smtplib.SMTP") as mock_smtp_cls:
                result = send_email("subject", "body")

        assert result is False
        mock_smtp_cls.assert_not_called()

    def test_returns_false_on_smtp_exception(self):
        with _patch_settings():
            with patch("smtplib.SMTP") as mock_smtp_cls:
                mock_smtp_cls.side_effect = OSError("connection refused")
                result = send_email("subject", "body")

        assert result is False

    def test_email_addressed_to_correct_recipient(self):
        captured = {}

        def _capture_message(msg):
            captured["to"] = msg["To"]
            captured["from"] = msg["From"]
            captured["subject"] = msg["Subject"]

        with _patch_settings():
            with patch("smtplib.SMTP") as mock_smtp_cls:
                mock_server = MagicMock()
                mock_smtp_cls.return_value.__enter__.return_value = mock_server
                mock_server.send_message.side_effect = _capture_message

                send_email("Hello", "World")

        assert captured["to"] == "trader@gmail.com"
        assert captured["from"] == "bot@gmail.com"
        assert captured["subject"] == "Hello"


# ---------------------------------------------------------------------------
# notify_buy_filled
# ---------------------------------------------------------------------------


class TestNotifyBuyFilled:
    def test_subject_contains_symbol(self):
        subjects = []

        with _patch_settings():
            with patch("core.notifier.send_email") as mock_send:
                mock_send.side_effect = lambda s, b: subjects.append(s) or True
                notify_buy_filled("NVDA", qty=10, fill_price=900.0, stop_price=837.0)

        assert "NVDA" in subjects[0]
        assert "BUY" in subjects[0]

    def test_body_contains_fill_price_and_stop(self):
        bodies = []

        with _patch_settings():
            with patch("core.notifier.send_email") as mock_send:
                mock_send.side_effect = lambda s, b: bodies.append(b) or True
                notify_buy_filled("AAPL", qty=5, fill_price=200.0, stop_price=186.0)

        body = bodies[0]
        assert "200.00" in body
        assert "186.00" in body
        assert "AAPL" in body

    def test_paper_label_in_body(self):
        bodies = []

        with _patch_settings():
            with patch("core.notifier.send_email") as mock_send:
                mock_send.side_effect = lambda s, b: bodies.append(b) or True
                notify_buy_filled("X", qty=1, fill_price=50.0, stop_price=46.5, paper=True)

        assert "Paper Trading" in bodies[0]

    def test_live_label_in_body(self):
        bodies = []

        with _patch_settings():
            with patch("core.notifier.send_email") as mock_send:
                mock_send.side_effect = lambda s, b: bodies.append(b) or True
                notify_buy_filled("X", qty=1, fill_price=50.0, stop_price=46.5, paper=False)

        assert "LIVE Trading" in bodies[0]


# ---------------------------------------------------------------------------
# notify_entry_submitted
# ---------------------------------------------------------------------------


class TestNotifyEntrySubmitted:
    def test_subject_contains_symbol_and_buy_submitted(self):
        subjects = []

        with _patch_settings():
            with patch("core.notifier.send_email") as mock_send:
                mock_send.side_effect = lambda s, b: subjects.append(s) or True
                notify_entry_submitted(
                    "NVDA",
                    qty=10,
                    entry_price=875.0,
                    stop_price=813.75,
                    position_value=8750.0,
                    risk_amount=612.5,
                    price_source="intraday_minute_close",
                    order_id="entry-1",
                )

        assert "NVDA" in subjects[0]
        assert "BUY SUBMITTED" in subjects[0]

    def test_body_contains_entry_stop_risk_and_source(self):
        bodies = []

        with _patch_settings():
            with patch("core.notifier.send_email") as mock_send:
                mock_send.side_effect = lambda s, b: bodies.append(b) or True
                notify_entry_submitted(
                    "AAPL",
                    qty=5,
                    entry_price=200.0,
                    stop_price=186.0,
                    position_value=1000.0,
                    risk_amount=70.0,
                    price_source="daily_close",
                    order_id="entry-2",
                    workflow_id="wf-aapl-2",
                )

        body = bodies[0]
        assert "200.00" in body
        assert "186.00" in body
        assert "70.00" in body
        assert "daily_close" in body
        assert "entry-2" in body
        assert "wf-aapl-2" in body
        assert "actual fill price" in body


# ---------------------------------------------------------------------------
# notify_sell_filled
# ---------------------------------------------------------------------------


class TestNotifySellFilled:
    def test_subject_contains_symbol_and_sell(self):
        subjects = []

        with _patch_settings():
            with patch("core.notifier.send_email") as mock_send:
                mock_send.side_effect = lambda s, b: subjects.append(s) or True
                notify_sell_filled(
                    "CRWD", qty=8, fill_price=350.0, entry_price=300.0,
                    exit_reason="stop-loss triggered",
                )

        assert "CRWD" in subjects[0]
        assert "SELL" in subjects[0]

    def test_positive_pnl_shown_with_plus_sign(self):
        bodies = []

        with _patch_settings():
            with patch("core.notifier.send_email") as mock_send:
                mock_send.side_effect = lambda s, b: bodies.append(b) or True
                notify_sell_filled(
                    "NVDA", qty=10, fill_price=1000.0, entry_price=875.0,
                    exit_reason="MA violation exit",
                )

        body = bodies[0]
        assert "+$1,250.00" in body or "+1,250" in body  # qty * profit
        assert "+" in body  # positive sign present

    def test_negative_pnl_shown_without_plus_sign(self):
        bodies = []

        with _patch_settings():
            with patch("core.notifier.send_email") as mock_send:
                mock_send.side_effect = lambda s, b: bodies.append(b) or True
                notify_sell_filled(
                    "AAPL", qty=5, fill_price=186.0, entry_price=200.0,
                    exit_reason="stop-loss triggered",
                )

        body = bodies[0]
        # P&L should be negative: 5 * (186 - 200) = -70
        assert "-$70.00" in body or "-70" in body

    def test_exit_reason_appears_in_body(self):
        bodies = []

        with _patch_settings():
            with patch("core.notifier.send_email") as mock_send:
                mock_send.side_effect = lambda s, b: bodies.append(b) or True
                notify_sell_filled(
                    "X", qty=1, fill_price=50.0, entry_price=50.0,
                    exit_reason="MA violation exit",
                )

        assert "MA violation exit" in bodies[0]


# ---------------------------------------------------------------------------
# notify_cycle_summary
# ---------------------------------------------------------------------------


class TestNotifyCycleSummary:
    def test_returns_false_when_nothing_to_report(self):
        with _patch_settings():
            with patch("core.notifier.send_email") as mock_send:
                result = notify_cycle_summary(entered=[], exited=[])

        assert result is False
        mock_send.assert_not_called()

    def test_sends_when_entries_submitted(self):
        with _patch_settings():
            with patch("core.notifier.send_email", return_value=True) as mock_send:
                result = notify_cycle_summary(entered=["NVDA", "CRWD"], exited=[])

        assert result is True
        mock_send.assert_called_once()

    def test_body_lists_all_entered_symbols(self):
        bodies = []

        with _patch_settings():
            with patch("core.notifier.send_email") as mock_send:
                mock_send.side_effect = lambda s, b: bodies.append(b) or True
                notify_cycle_summary(entered=["NVDA", "CRWD"], exited=["AAPL"])

        body = bodies[0]
        assert "NVDA" in body
        assert "CRWD" in body
        assert "AAPL" in body
