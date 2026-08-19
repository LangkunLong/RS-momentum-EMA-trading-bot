"""Unit tests for core.notifier — mocked SMTP, no real email sent."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from core.notifier import (
    _is_configured,
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
        "NOTIFY_EMAIL_PROVIDER": "smtp",
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

    def test_gmail_oauth_is_configured_without_an_smtp_password(self):
        """Requiring the legacy password would defeat browser-based authorization."""
        with _patch_settings(
            NOTIFY_EMAIL_PROVIDER="gmail_oauth",
            NOTIFY_EMAIL_FROM="langkunlong@gmail.com",
            NOTIFY_EMAIL_TO="langkunlong@gmail.com",
            NOTIFY_EMAIL_PASSWORD="",
        ):
            with patch("core.notifier.is_gmail_authorized", return_value=True):
                assert _is_configured() is True

    def test_gmail_oauth_routes_through_gmail_api_without_smtp(self):
        """The OAuth backend must never expose a path back to SMTP password login."""
        with _patch_settings(
            NOTIFY_EMAIL_PROVIDER="gmail_oauth",
            NOTIFY_EMAIL_FROM="langkunlong@gmail.com",
            NOTIFY_EMAIL_TO="langkunlong@gmail.com",
            NOTIFY_EMAIL_PASSWORD="",
        ):
            with (
                patch("core.notifier.is_gmail_authorized", return_value=True),
                patch("core.notifier.send_gmail_email", return_value="gmail-message-1") as gmail_send,
                patch("smtplib.SMTP") as smtp,
            ):
                result = send_email("OAuth subject", "OAuth body")

        assert result is True
        assert gmail_send.call_args.kwargs == {
            "from_email": "langkunlong@gmail.com",
            "to_email": "langkunlong@gmail.com",
            "subject": "OAuth subject",
            "body": "OAuth body",
        }
        smtp.assert_not_called()

    def test_gmail_oauth_failure_does_not_fall_back_to_smtp(self):
        """A revoked OAuth grant must fail visibly rather than use an unrelated legacy secret."""
        from core.gmail_oauth import GmailOAuthError

        with _patch_settings(
            NOTIFY_EMAIL_PROVIDER="gmail_oauth",
            NOTIFY_EMAIL_FROM="langkunlong@gmail.com",
            NOTIFY_EMAIL_TO="langkunlong@gmail.com",
            NOTIFY_EMAIL_PASSWORD="legacy-secret",
        ):
            with (
                patch("core.notifier.is_gmail_authorized", return_value=True),
                patch(
                    "core.notifier.send_gmail_email",
                    side_effect=GmailOAuthError("Gmail API send failed"),
                ),
                patch("smtplib.SMTP") as smtp,
            ):
                result = send_email("OAuth subject", "OAuth body")

        assert result is False
        smtp.assert_not_called()

    def test_explicit_unavailable_gmail_oauth_logs_a_safe_static_error(self, capsys):
        """A selected OAuth backend must diagnose a missing authorization without leaking settings."""
        with _patch_settings(
            NOTIFY_EMAIL_PROVIDER="gmail_oauth",
            NOTIFY_EMAIL_FROM="langkunlong@gmail.com",
            NOTIFY_EMAIL_TO="langkunlong@gmail.com",
            NOTIFY_EMAIL_PASSWORD="legacy-secret",
        ):
            with (
                patch("core.notifier.is_gmail_authorized", return_value=False),
                patch("smtplib.SMTP") as smtp,
            ):
                result = send_email("OAuth subject", "OAuth body")

        assert result is False
        assert capsys.readouterr().out == (
            "[NOTIFY ERROR] Gmail OAuth notifications are unavailable; run `email-auth` again\n"
        )
        smtp.assert_not_called()

    def test_invalid_recipient_is_not_configured_and_never_reaches_smtp(self):
        """A malformed recipient must fail before an SMTP connection is attempted."""
        with _patch_settings(NOTIFY_EMAIL_TO="not-an-email"):
            with patch("smtplib.SMTP") as smtp:
                result = send_email("subject", "body")
                configured = _is_configured()

        assert configured is False
        assert result is False
        smtp.assert_not_called()

    def test_invalid_sender_is_not_configured(self):
        """A malformed sender must be rejected before either backend is selected."""
        with _patch_settings(NOTIFY_EMAIL_FROM="not-an-email"):
            assert _is_configured() is False

    def test_notification_configuration_error_accepts_valid_addresses(self):
        """Valid addresses must not be misclassified as an operator configuration error."""
        from core import notifier

        with _patch_settings():
            assert notifier.notification_configuration_error() is None


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

    def test_unknown_entry_price_reports_pnl_unavailable(self):
        """Missing recovery state must not be presented as a zero-gain trade."""
        bodies = []

        with _patch_settings():
            with patch("core.notifier.send_email") as mock_send:
                mock_send.side_effect = lambda _subject, body: bodies.append(body) or True
                notify_sell_filled(
                    "ORPHAN",
                    qty=10,
                    fill_price=110.0,
                    entry_price=None,
                    exit_reason="exit order filled",
                )

        assert "Entry price: unavailable" in bodies[0]
        assert "P&L:         unavailable" in bodies[0]
        assert "+$0.00" not in bodies[0]


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
