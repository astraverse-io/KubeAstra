"""Outbound email for password reset.

Sends via SMTP when configured (SMTP_HOST/PORT/USERNAME/PASSWORD/FROM/STARTTLS);
otherwise logs the reset link so the flow is fully testable in dev without a mail
server. Never raises to the caller — a delivery failure must not change the
forgot-password response (which is deliberately non-revealing).
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Optional

logger = logging.getLogger(__name__)


def _smtp_config() -> Optional[dict]:
    host = os.environ.get("SMTP_HOST")
    if not host:
        return None
    username = os.environ.get("SMTP_USERNAME")
    return {
        "host": host,
        "port": int(os.environ.get("SMTP_PORT", "587") or "587"),
        "username": username,
        "password": os.environ.get("SMTP_PASSWORD"),
        "from_addr": os.environ.get("SMTP_FROM") or username or "no-reply@localhost",
        "starttls": (os.environ.get("SMTP_STARTTLS", "true").strip().lower() in {"1", "true", "yes", "on"}),
    }


def send_password_reset_email(to_email: str, reset_url: str, ttl_minutes: int) -> bool:
    """Send the reset email.

    Returns True if dispatched via SMTP. Returns False when SMTP is unconfigured
    or delivery fails. The reset link is logged only for the unconfigured-dev
    path, never after a configured SMTP delivery failure. Never raises.
    """
    subject = "Reset your KubeAstra Assistant password"
    body = (
        "We received a request to reset your password.\n\n"
        f"Reset it using this link (valid for {ttl_minutes} minutes):\n{reset_url}\n\n"
        "If you did not request this, you can safely ignore this email."
    )

    cfg = _smtp_config()
    if not cfg:
        logger.warning("SMTP not configured; password reset link for %s: %s", to_email, reset_url)
        return False

    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = cfg["from_addr"]
        msg["To"] = to_email
        msg.set_content(body)
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as server:
            if cfg["starttls"]:
                server.starttls(context=ssl.create_default_context())
            if cfg["username"]:
                server.login(cfg["username"], cfg["password"] or "")
            server.send_message(msg)
        logger.info("Password reset email sent to %s", to_email)
        return True
    except Exception as exc:  # delivery failure must not leak or break the flow
        logger.error("Failed to send password reset email to %s: %s", to_email, exc)
        return False
