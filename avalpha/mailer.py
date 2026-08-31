"""Email delivery via Gmail's SMTP relay with an app password.

Mail originates from Google's servers (not the app host), so the "residential
IP goes to spam" concern about self-hosting SMTP doesn't apply — and for a
daily PDF to your own inbox this needs no verified domain or third-party
service. Requires 2FA on the account plus an app password (GMAIL_APP_PASSWORD).
The SMTP login is the bare address parsed from the configured sender.
"""

import smtplib
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path

from avalpha.config import Config

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def build_message(config: Config, pdf_path: Path, label_date: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = config.email_sender
    msg["To"] = config.email_recipient
    msg["Subject"] = f"avalpha digest — {label_date}"
    msg.set_content(
        f"Morning digest covering {label_date} is attached.\n\n— avalpha\n"
    )
    msg.add_attachment(
        pdf_path.read_bytes(),
        maintype="application",
        subtype="pdf",
        filename=pdf_path.name,
    )
    return msg


def send_digest_email(config: Config, pdf_path: Path, label_date: str) -> None:
    msg = build_message(config, pdf_path, label_date)
    login = parseaddr(config.email_sender)[1]
    if not login:
        raise RuntimeError(
            "email.sender in config.toml must contain a Gmail address"
        )
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=60) as smtp:
        smtp.starttls()
        smtp.login(login, config.gmail_app_password)
        smtp.send_message(msg)
