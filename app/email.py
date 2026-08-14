"""Email notification helper.

Reads SMTP config from AppSettings (id=1) and sends plain-text notifications.
Silently no-ops when email is not configured. Never raises — a broken SMTP
config must not crash task saves or reflow.
"""

import logging
import smtplib
from email.message import EmailMessage

from sqlmodel import Session

from app.models import AppSettings

logger = logging.getLogger(__name__)

# Beyond this many projects the subject line stops being scannable in an inbox,
# so the tail is collapsed into a "+N more" count.
MAX_SUBJECT_PROJECTS = 3


def subject_tag(projects: list[str] | None = None) -> str:
    """Build the subject prefix: "[trundlr]", or "[trundlr|Alpha, Beta +2 more]".

    Duplicates and blanks are dropped, first-seen order is kept.
    """
    names = [p for p in dict.fromkeys(projects or []) if p]
    if not names:
        return "[trundlr]"
    shown = names[:MAX_SUBJECT_PROJECTS]
    joined = ", ".join(shown)
    if len(names) > len(shown):
        joined += f" +{len(names) - len(shown)} more"
    return f"[trundlr|{joined}]"


def send_notification(session: Session, subject: str, body: str,
                      projects: list[str] | None = None) -> None:
    try:
        s = session.get(AppSettings, 1)
        if not s or not s.notify_email or not s.smtp_host:
            return
        msg = EmailMessage()
        msg["Subject"] = f"{subject_tag(projects)} {subject}"
        msg["From"] = s.smtp_from or s.smtp_user or "trundlr@localhost"
        msg["To"] = s.notify_email
        msg.set_content(body)
        with smtplib.SMTP(s.smtp_host, s.smtp_port) as conn:
            if s.smtp_tls:
                conn.starttls()
            if s.smtp_user and s.smtp_password:
                conn.login(s.smtp_user, s.smtp_password)
            conn.send_message(msg)
    except Exception as exc:
        logger.warning("Email notification failed: %s", exc)


def send_test(session: Session) -> str:
    """Try to send a test email. Returns None on success, error string on failure."""
    try:
        s = session.get(AppSettings, 1)
        if not s or not s.notify_email or not s.smtp_host:
            return "Email not configured (set SMTP host and recipient first)."
        msg = EmailMessage()
        msg["Subject"] = f"{subject_tag()} Test notification"
        msg["From"] = s.smtp_from or s.smtp_user or "trundlr@localhost"
        msg["To"] = s.notify_email
        msg.set_content("This is a test notification from trundlr. If you received it, email is working.")
        with smtplib.SMTP(s.smtp_host, s.smtp_port) as conn:
            if s.smtp_tls:
                conn.starttls()
            if s.smtp_user and s.smtp_password:
                conn.login(s.smtp_user, s.smtp_password)
            conn.send_message(msg)
        return None
    except Exception as exc:
        return str(exc)
