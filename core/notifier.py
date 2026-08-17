"""Email notifications for CANSLIM order fills and exits.

Sends via Gmail SMTP (TLS on port 587).  Requires three environment variables:

    NOTIFY_EMAIL_FROM     — your Gmail address (the sending account)
    NOTIFY_EMAIL_TO       — recipient address (can be the same Gmail)
    NOTIFY_EMAIL_PASSWORD — Gmail App Password (not your login password).
                            Generate at https://myaccount.google.com/apppasswords

If any of the three variables is empty, notification calls are silently skipped
so the rest of the trading workflow is never blocked by a misconfigured mailer.
"""

from __future__ import annotations

import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

from config import settings

_ET = ZoneInfo("America/New_York")
_SMTP_HOST = "smtp.gmail.com"
_SMTP_PORT = 587


def _is_configured() -> bool:
    """Return True when all three email env vars are present."""
    return bool(
        settings.NOTIFY_EMAIL_FROM
        and settings.NOTIFY_EMAIL_TO
        and settings.NOTIFY_EMAIL_PASSWORD
    )


def send_email(subject: str, body: str) -> bool:
    """Send a plain-text email via Gmail SMTP.

    Args:
        subject: Email subject line.
        body: Plain-text email body.

    Returns:
        True when the message was accepted by Gmail; False otherwise (including
        when email is not configured).
    """
    if not _is_configured():
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
    mode = "Paper Trading" if paper else "LIVE Trading"
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

    mode = "Paper Trading" if paper else "LIVE Trading"
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
    entry_price: float,
    exit_reason: str,
    workflow_id: str | None = None,
    paper: bool = True,
) -> bool:
    """Send a notification when a sell/exit order is filled.

    Args:
        symbol: Ticker that was sold.
        qty: Number of shares sold.
        fill_price: Average fill price per share.
        entry_price: Original average entry price (for P&L display).
        exit_reason: Human-readable reason (e.g. 'stop-loss', 'MA violation').
        paper: Whether this is a paper-trading fill.

    Returns:
        True if the email was sent successfully.
    """
    pnl = (fill_price - entry_price) * qty
    pnl_pct = (fill_price - entry_price) / entry_price if entry_price else 0.0
    sign = "+" if pnl >= 0 else ""
    mode = "Paper Trading" if paper else "LIVE Trading"
    now_str = datetime.now(tz=_ET).strftime("%Y-%m-%d %H:%M ET")
    workflow_line = f"Workflow ID: {workflow_id}\n" if workflow_id else ""

    subject = f"[CANSLIM] SELL FILLED: {symbol}"
    body = (
        f"Order Filled — SELL\n"
        f"{'─' * 40}\n"
        f"Symbol:      {symbol}\n"
        f"Qty:         {qty:,.4g} shares\n"
        f"Fill price:  ${fill_price:,.2f}\n"
        f"Entry price: ${entry_price:,.2f}\n"
        f"P&L:         {sign}${pnl:,.2f} ({sign}{pnl_pct * 100:.2f}%)\n"
        f"Exit reason: {exit_reason}\n"
        f"{workflow_line}"
        f"Mode:        {mode}\n"
        f"Time:        {now_str}\n"
    )
    return send_email(subject, body)


def notify_cycle_summary(
    entered: list[str],
    exited: list[str],
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

    mode = "Paper Trading" if paper else "LIVE Trading"
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
