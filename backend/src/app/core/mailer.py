"""Minimal SMTP mailer for encouraging magic-login links.

No external dependency — uses the stdlib ``smtplib``/``email``. When an SMTP
server is configured in settings it sends the link; otherwise it returns
``False`` so the caller can expose the link directly (dev fallback).
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from ..config import settings

logger = logging.getLogger(__name__)


def send_html_email(to_email: str, subject: str, body_html: str) -> bool:
    """Send an HTML email. Returns True on success, False when SMTP isn't
    configured (so the caller can decide how to surface the link)."""
    if not settings.smtp_host or not settings.smtp_from:
        logger.warning("SMTP not configured — skipping email to %s", to_email)
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from
    msg["To"] = to_email
    msg.set_content(_strip_html(body_html))
    msg.add_alternative(body_html, subtype="html")

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
            if settings.smtp_use_tls:
                server.starttls()
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)
        logger.info("Email sent to %s subject=%s", to_email, subject)
        return True
    except Exception as exc:  # noqa: BLE001 - never let email failure break the flow
        logger.error("Email delivery failed to %s: %s", to_email, exc)
        return False


def send_magic_link(to_email: str, link: str) -> bool:
    body = (
        "<p>Hi,</p>"
        "<p>Click below to sign in to your Speako merchant dashboard:</p>"
        f'<p><a href="{link}">Sign in</a></p>'
        "<p>This link expires in 15 minutes and can only be used once.</p>"
        "<p>If you didn't request this, you can safely ignore this email.</p>"
    )
    return send_html_email(to_email, "Your Speako sign-in link", body)


def _strip_html(html: str) -> str:
    import re
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html)).strip()