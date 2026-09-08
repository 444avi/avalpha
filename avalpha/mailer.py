"""Email delivery of the daily digest via Amazon SES.

Sent through SESv2 SendEmail (raw content) authenticated by the EC2 instance
role, so there is no SMTP host or password. The From address (config.toml
[email] sender) must be an address on a verified SES identity
(arboretuminvestments.net). boto3 is imported lazily inside
:func:`send_digest_email` so importing this module never requires boto3.
"""

from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path

from avalpha.config import Config


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
    if not parseaddr(config.email_sender)[1]:
        raise RuntimeError(
            "email.sender in config.toml must contain an address on the "
            "verified SES domain (arboretuminvestments.net)"
        )
    if not config.email_recipient:
        raise RuntimeError("email.recipient in config.toml is not set")

    msg = build_message(config, pdf_path, label_date)

    import boto3

    client = boto3.client("sesv2", region_name=config.aws_region)
    kwargs = dict(
        FromEmailAddress=config.email_sender,
        Destination={"ToAddresses": [config.email_recipient]},
        Content={"Raw": {"Data": msg.as_bytes()}},
    )
    if config.ses_configuration_set:
        kwargs["ConfigurationSetName"] = config.ses_configuration_set
    client.send_email(**kwargs)
