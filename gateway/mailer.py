"""Minimal SMTP delivery boundary for Semifly security emails.

Delivery is deliberately explicit: production must configure SMTP over TLS.
There is no development fallback that returns verification or reset secrets to a
browser response.
"""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage


class Mailer:
    def __init__(self) -> None:
        self.host = os.environ.get("SEMIFLY_SMTP_HOST", "")
        self.port = int(os.environ.get("SEMIFLY_SMTP_PORT", "587"))
        self.username = os.environ.get("SEMIFLY_SMTP_USERNAME", "")
        self.password = os.environ.get("SEMIFLY_SMTP_PASSWORD", "")
        self.sender = os.environ.get("SEMIFLY_EMAIL_FROM", "")

    @property
    def configured(self) -> bool:
        return bool(self.host and self.sender)

    def send(self, *, recipient: str, subject: str, text: str) -> bool:
        if not self.configured:
            return False
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(text)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=15) as smtp:
                smtp.starttls()
                if self.username:
                    smtp.login(self.username, self.password)
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException):
            return False
        return True
