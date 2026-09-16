"""Sends the rendered digest via SMTP (Gmail app password or any SMTP relay,
including AWS SES's SMTP interface)."""

from __future__ import annotations

import logging
import os
import smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


def build_message(
    html: str, subject: str, from_name: str, from_address: str, to_addresses: list[str]
) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_address}>"
    msg["To"] = ", ".join(to_addresses)
    msg.attach(MIMEText(html, "html"))
    return msg


def send_digest(html: str, config: dict, week_start: date) -> None:
    """Sends the digest email. Reads SMTP credentials/host from environment
    variables so no secrets live in config.yaml:
      SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASS
    """
    recipients_cfg = config.get("recipients", {})
    to_addresses = recipients_cfg.get("to", [])
    if not to_addresses:
        raise ValueError("No recipients configured in config.yaml recipients.to")

    from_address = recipients_cfg.get("from_address")
    from_name = recipients_cfg.get("from_name", "RA Deal Radar")
    subject_prefix = recipients_cfg.get("subject_prefix", "RA Deal Radar")
    subject = f"{subject_prefix} — Week of {week_start.strftime('%d %b %Y')}"

    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")

    if not smtp_user or not smtp_pass:
        raise ValueError(
            "SMTP_USER and SMTP_PASS environment variables must be set to send mail"
        )

    msg = build_message(html, subject, from_name, from_address or smtp_user, to_addresses)

    logger.info("Sending digest to %d recipient(s) via %s:%s", len(to_addresses), smtp_host, smtp_port)
    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, to_addresses, msg.as_string())
    logger.info("Digest sent.")
