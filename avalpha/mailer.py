"""Email delivery of the daily digest.

Two backends behind one send path, chosen by config: SMTP (Mailtrap) when
``SMTP_HOST`` is set, otherwise Amazon SES via the EC2 instance role (kept as a
fallback). The From address (config.toml [email] sender) must be an address on
the verified arboretuminvestments.net domain; replies go to ``config.reply_to``.
boto3 and smtplib are imported lazily so importing this module needs neither.
"""

from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path

from avalpha.config import Config


def build_message(
    config: Config,
    pdf_path: Path,
    label_date: str,
    recipient: str | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = config.email_sender
    msg["To"] = recipient or config.email_recipient
    if config.reply_to:
        msg["Reply-To"] = config.reply_to
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


def send_digest_email(
    config: Config,
    pdf_path: Path,
    label_date: str,
    recipient: str | None = None,
) -> None:
    if not parseaddr(config.email_sender)[1]:
        raise RuntimeError(
            "email.sender in config.toml must contain an address on the "
            "verified domain (arboretuminvestments.net)"
        )
    recipient = recipient or config.email_recipient
    if not recipient:
        raise RuntimeError("digest recipient is not set")

    msg = build_message(config, pdf_path, label_date, recipient=recipient)

    if config.smtp_host:
        import smtplib

        with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=60) as smtp:
            smtp.starttls()
            if config.smtp_user:
                smtp.login(config.smtp_user, config.smtp_password)
            smtp.send_message(msg)
    else:
        import boto3

        client = boto3.client("sesv2", region_name=config.aws_region)
        kwargs = dict(
            FromEmailAddress=config.email_sender,
            Destination={"ToAddresses": [recipient]},
            Content={"Raw": {"Data": msg.as_bytes()}},
        )
        if config.ses_configuration_set:
            kwargs["ConfigurationSetName"] = config.ses_configuration_set
        client.send_email(**kwargs)
