"""Email notifications for CANSLIM order fills and exits.

Gmail API OAuth is preferred and stores refresh credentials in the OS credential vault.
Legacy Gmail SMTP (TLS on port 587) remains available with three environment variables:

    NOTIFY_EMAIL_PROVIDER — gmail_oauth (preferred), smtp, or auto
    NOTIFY_EMAIL_FROM     — your Gmail address (the sending account)
    NOTIFY_EMAIL_TO       — recipient address (can be the same Gmail)
    NOTIFY_EMAIL_PASSWORD — legacy SMTP App Password; unused by Gmail OAuth

If the selected backend is incomplete, notification calls do not block the trading workflow.
An explicitly selected but unavailable Gmail OAuth backend emits a safe diagnostic instead.
"""

from __future__ import annotations

import smtplib
from collections.abc import Sequence
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parseaddr
from zoneinfo import ZoneInfo

from config import settings
from core.gmail_oauth import is_gmail_authorized, send_gmail_email
from core.order_execution import require_paper_mode

_ET = ZoneInfo("America/New_York")
_SMTP_HOST = "smtp.gmail.com"
_SMTP_PORT = 587


def _paper_mode_label(paper: bool) -> str:
    """Return the only supported execution label after enforcing paper mode."""
    require_paper_mode(paper)
    return "Paper Trading"


def _is_configured() -> bool:
    """Return whether the selected notification backend has complete credentials."""
    return _configured_backend() is not None


def _is_valid_email_address(value: str) -> bool:
    display_name, address = parseaddr(value)
    if display_name or address != value or value.count("@") != 1:
        return False
    local_part, domain = value.split("@", maxsplit=1)
    return bool(local_part and domain) and not any(character.isspace() for character in value)


def notification_configuration_error() -> str | None:
    """Return a safe static validation error for an invalid notification configuration."""
    provider = str(settings.NOTIFY_EMAIL_PROVIDER or "auto").strip().lower() or "auto"
    if provider not in {"auto", "gmail_oauth", "smtp"}:
        return f"Unsupported NOTIFY_EMAIL_PROVIDER: {provider}"

    sender = str(settings.NOTIFY_EMAIL_FROM or "").strip()
    recipient = str(settings.NOTIFY_EMAIL_TO or "").strip()
    if sender and not _is_valid_email_address(sender):
        return "NOTIFY_EMAIL_FROM must be a valid email address"
    if recipient and not _is_valid_email_address(recipient):
        return "NOTIFY_EMAIL_TO must be a valid email address"
    return None


def _configured_backend() -> str | None:
    if notification_configuration_error() is not None:
        return None
    sender = str(settings.NOTIFY_EMAIL_FROM or "").strip()
    recipient = str(settings.NOTIFY_EMAIL_TO or "").strip()
    if not sender or not recipient:
        return None
    provider = str(settings.NOTIFY_EMAIL_PROVIDER or "auto").strip().lower() or "auto"
    if provider in {"auto", "gmail_oauth"} and is_gmail_authorized(sender):
        return "gmail_oauth"
    if provider == "gmail_oauth":
        return None
    if settings.NOTIFY_EMAIL_PASSWORD:
        return "smtp"
    return None


def _gmail_oauth_selected() -> bool:
    return str(settings.NOTIFY_EMAIL_PROVIDER or "auto").strip().lower() == "gmail_oauth"


def send_email(subject: str, body: str) -> bool:
    """Send a plain-text email through the configured Gmail backend.

    Args:
        subject: Email subject line.
        body: Plain-text email body.

    Returns:
        True when the message was accepted by Gmail; False otherwise (including
        when email is not configured).
    """
    backend = _configured_backend()
    if backend is None:
        if _gmail_oauth_selected():
            print("[NOTIFY ERROR] Gmail OAuth notifications are unavailable; run `email-auth` again")
        return False

    if backend == "gmail_oauth":
        try:
            send_gmail_email(
                from_email=settings.NOTIFY_EMAIL_FROM,
                to_email=settings.NOTIFY_EMAIL_TO,
                subject=subject,
                body=body,
            )
            print(f"[NOTIFY] Email sent: {subject}")
            return True
        except Exception:
            print("[NOTIFY ERROR] Gmail OAuth delivery failed; run `email-auth` again")
            return False

    msg = MIMEMultipart()
    msg["From"] = settings.NOTIFY_EMAIL_FROM
    msg["To"] = settings.NOTIFY_EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT) as server:
            server.starttls()
            server.login(settings.NOTIFY_EMAIL_FROM, settings.NOTIFY_EMAIL_PASSWORD)
            server.send_message(msg)
        print(f"[NOTIFY] Email sent: {subject}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[NOTIFY ERROR] Failed to send email: {exc}")
        return False


def notify_buy_filled(
    symbol: str,
    qty: float,
    fill_price: float,
    stop_price: float,
    workflow_id: str | None = None,
    stop_loss_pct: float | None = None,
    paper: bool = True,
) -> bool:
    """Send a notification when a buy order is filled.

    Args:
        symbol: Ticker that was purchased.
        qty: Number of shares filled.
        fill_price: Average fill price per share.
        stop_price: Stop-loss price attached to the bracket.
        stop_loss_pct: Stop-loss percentage for display (e.g. 0.07 = 7%).
        paper: Whether this is a paper-trading fill.

    Returns:
        True if the email was sent successfully.
    """
    if stop_loss_pct is None:
        stop_loss_pct = settings.STOP_LOSS_PCT

    position_value = qty * fill_price
    mode = _paper_mode_label(paper)
    now_str = datetime.now(tz=_ET).strftime("%Y-%m-%d %H:%M ET")
    workflow_line = f"Workflow ID: {workflow_id}\n" if workflow_id else ""

    subject = f"[CANSLIM] BUY FILLED: {symbol}"
    body = (
        f"Order Filled — BUY\n"
        f"{'─' * 40}\n"
        f"Symbol:      {symbol}\n"
        f"Qty:         {qty:,.4g} shares\n"
        f"Fill price:  ${fill_price:,.2f}\n"
        f"Stop-loss:   ${stop_price:,.2f} ({stop_loss_pct * 100:.1f}% below fill)\n"
        f"Position:    ~${position_value:,.0f}\n"
        f"{workflow_line}"
        f"Mode:        {mode}\n"
        f"Time:        {now_str}\n"
    )
    return send_email(subject, body)


def notify_entry_submitted(
    symbol: str,
    qty: float,
    entry_price: float,
    stop_price: float,
    position_value: float,
    risk_amount: float,
    price_source: str,
    order_id: str,
    workflow_id: str | None = None,
    stop_loss_pct: float | None = None,
    paper: bool = True,
) -> bool:
    """Send a notification when a buy entry order is accepted by the broker."""
    if stop_loss_pct is None:
        stop_loss_pct = settings.STOP_LOSS_PCT

    mode = _paper_mode_label(paper)
    now_str = datetime.now(tz=_ET).strftime("%Y-%m-%d %H:%M ET")
    workflow_line = f"Workflow ID:   {workflow_id}\n" if workflow_id else ""

    subject = f"[CANSLIM] BUY SUBMITTED: {symbol}"
    body = (
        f"Order Submitted - BUY\n"
        f"{'-' * 40}\n"
        f"Symbol:        {symbol}\n"
        f"Qty:           {qty:,.4g} shares\n"
        f"Entry ref:     ${entry_price:,.2f}\n"
        f"Planned stop:  ${stop_price:,.2f} ({stop_loss_pct * 100:.1f}% below reference)\n"
        f"Position:      ~${position_value:,.2f}\n"
        f"Planned risk:  ~${risk_amount:,.2f}\n"
        f"Price source:  {price_source}\n"
        f"Order ID:      {order_id}\n"
        f"{workflow_line}"
        f"Mode:          {mode}\n"
        f"Time:          {now_str}\n"
        f"Note: final protective stop is reconciled from the actual fill price.\n"
    )
    return send_email(subject, body)


def notify_sell_filled(
    symbol: str,
    qty: float,
    fill_price: float,
    entry_price: float | None,
    exit_reason: str,
    workflow_id: str | None = None,
    paper: bool = True,
) -> bool:
    """Send a notification when a sell/exit order is filled.

    Args:
        symbol: Ticker that was sold.
        qty: Number of shares sold.
        fill_price: Average fill price per share.
        entry_price: Original average entry price, or None when recovery failed.
        exit_reason: Human-readable reason (e.g. 'stop-loss', 'MA violation').
        paper: Whether this is a paper-trading fill.

    Returns:
        True if the email was sent successfully.
    """
    if entry_price is not None and entry_price > 0:
        pnl = (fill_price - entry_price) * qty
        pnl_pct = (fill_price - entry_price) / entry_price
        sign = "+" if pnl >= 0 else ""
        entry_price_line = f"Entry price: ${entry_price:,.2f}"
        pnl_line = f"P&L:         {sign}${pnl:,.2f} ({sign}{pnl_pct * 100:.2f}%)"
    else:
        entry_price_line = "Entry price: unavailable"
        pnl_line = "P&L:         unavailable"
    mode = _paper_mode_label(paper)
    now_str = datetime.now(tz=_ET).strftime("%Y-%m-%d %H:%M ET")
    workflow_line = f"Workflow ID: {workflow_id}\n" if workflow_id else ""

    subject = f"[CANSLIM] SELL FILLED: {symbol}"
    body = (
        f"Order Filled — SELL\n"
        f"{'─' * 40}\n"
        f"Symbol:      {symbol}\n"
        f"Qty:         {qty:,.4g} shares\n"
        f"Fill price:  ${fill_price:,.2f}\n"
        f"{entry_price_line}\n"
        f"{pnl_line}\n"
        f"Exit reason: {exit_reason}\n"
        f"{workflow_line}"
        f"Mode:        {mode}\n"
        f"Time:        {now_str}\n"
    )
    return send_email(subject, body)


def notify_cycle_summary(
    entered: Sequence[str],
    exited: Sequence[str],
    paper: bool = True,
) -> bool:
    """Send a daily trading cycle summary.

    Args:
        entered: Symbols for which buy orders were submitted.
        exited: Symbols for which exit orders were submitted.
        paper: Whether this is a paper-trading session.

    Returns:
        True if the email was sent successfully.
    """
    if not entered and not exited:
        return False  # Nothing to report

    mode = _paper_mode_label(paper)
    now_str = datetime.now(tz=_ET).strftime("%Y-%m-%d %H:%M ET")

    entered_str = ", ".join(entered) if entered else "none"
    exited_str = ", ".join(exited) if exited else "none"

    subject = f"[CANSLIM] Daily Cycle Summary — {datetime.now(tz=_ET).strftime('%Y-%m-%d')}"
    body = (
        f"CANSLIM Auto-Trader Cycle Complete\n"
        f"{'─' * 40}\n"
        f"Entries submitted: {entered_str}\n"
        f"Exits submitted:   {exited_str}\n"
        f"Mode:              {mode}\n"
        f"Time:              {now_str}\n"
    )
    return send_email(subject, body)
