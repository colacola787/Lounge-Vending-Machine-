"""
mailer.py

University-tailored SMTP helper for the Lounge Vending Machine.
This module is configurable via environment variables and includes helpers useful
for the University of Wollongong in Dubai (UOWD) deployment.

Environment variables (defaults shown):
- SMTP_HOST: SMTP server hostname (default: smtp.gmail.com)
- SMTP_PORT: SMTP port (default: 587)
- SMTP_USE_SSL: if true, use smtplib.SMTP_SSL (default: false)
- SMTP_STARTTLS: if true, call starttls() on SMTP connection (default: true for port 587)
- SMTP_USERNAME: username to authenticate with (optional)
- SMTP_PASSWORD: password for authentication (optional)
- SENDER_EMAIL: From address to use (defaults to SMTP_USERNAME or no-reply)
- UOWD_EMAIL_DOMAIN: Student email domain for UOWD (default: uowmail.edu.au)
- UOWD_STRICT_DOMAIN: if true, prevent sending to addresses outside the UOWD domain (default: false)
"""

import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Optional


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# Basic SMTP configuration
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USE_SSL = _env_bool("SMTP_USE_SSL", False)
# Default STARTTLS behavior: enabled for port 587 unless explicitly disabled
SMTP_STARTTLS = _env_bool("SMTP_STARTTLS", SMTP_PORT == 587)
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD") or os.getenv("EMAIL_APP_PASSWORD")
SENDER_EMAIL = os.getenv("SENDER_EMAIL") or SMTP_USERNAME or os.getenv("MAIL_FROM") or "no-reply@localhost"

# UOWD-specific defaults
UOWD_EMAIL_DOMAIN = os.getenv("UOWD_EMAIL_DOMAIN", "uowmail.edu.au")
UOWD_STRICT_DOMAIN = _env_bool("UOWD_STRICT_DOMAIN", False)


def _compose_message(to: str, subject: str, body: str, from_addr: Optional[str]) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr or SENDER_EMAIL
    msg["To"] = to
    msg.set_content(body)
    return msg


def send_email(to: str, subject: str, body: str, from_addr: Optional[str] = None, allow_external: Optional[bool] = None) -> None:
    """Send a plain-text email using configured SMTP settings.

    When UOWD_STRICT_DOMAIN is set, the helper will block sending to addresses
    outside of UOWD_EMAIL_DOMAIN unless allow_external=True is passed explicitly.
    """
    if not to:
        raise ValueError("Recipient email address is required.")

    # Enforce UOWD domain if requested
    if allow_external is None:
        allow_external = not UOWD_STRICT_DOMAIN

    if not allow_external:
        if not to.lower().endswith("@" + UOWD_EMAIL_DOMAIN.lower()):
            raise ValueError(f"Sending restricted: recipient '{to}' is outside the UOWD domain {UOWD_EMAIL_DOMAIN}.")

    from_addr = from_addr or SENDER_EMAIL
    msg = _compose_message(to, subject, body, from_addr)

    context = ssl.create_default_context()

    # If using SMTP over SSL (SMTPS), connect with SMTP_SSL
    if SMTP_USE_SSL:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context) as server:
            if SMTP_USERNAME:
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
        return

    # Otherwise use regular SMTP and optionally upgrade to TLS
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        if SMTP_STARTTLS:
            server.starttls(context=context)
            server.ehlo()
        if SMTP_USERNAME:
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(msg)


def send(to: str, subject: str, body: str) -> None:
    """Backward-compatible convenience wrapper."""
    send_email(to, subject, body)


# UOWD convenience helpers
def student_email_from_id(student_id: str) -> str:
    """Construct a likely student email using the UOWD email domain.

    Note: this assumes the university's student mailbox naming scheme is the
    student identifier followed by @UOWD_EMAIL_DOMAIN (e.g. s1234567@uowmail.edu.au).
    If UOWD uses a different canonical mapping, adjust accordingly.
    """
    if not student_id or not student_id.strip():
        raise ValueError("Student ID is required to construct a student email address.")
    cleaned = student_id.strip().lower()
    return f"{cleaned}@{UOWD_EMAIL_DOMAIN}"


def send_to_student_id(student_id: str, subject: str, body: str, allow_external: Optional[bool] = None) -> None:
    """Convenience to send an email to a student by ID using the configured UOWD domain."""
    to = student_email_from_id(student_id)
    send_email(to, subject, body, allow_external=allow_external)
