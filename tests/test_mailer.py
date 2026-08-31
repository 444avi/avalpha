from avalpha.config import Config
from avalpha.mailer import build_message


def _config(sender="avalpha <you@example.com>") -> Config:
    return Config(
        db_path="/tmp/x.db",
        digest_dir="/tmp",
        email_recipient="you@example.com",
        email_sender=sender,
    )


def test_build_message_has_pdf_attachment(tmp_path):
    pdf = tmp_path / "avalpha-2026-08-28.pdf"
    pdf.write_bytes(b"%PDF-1.7 fake")
    msg = build_message(_config(), pdf, "2026-08-28")

    assert msg["Subject"] == "avalpha digest — 2026-08-28"
    assert msg["From"] == "avalpha <you@example.com>"
    assert msg["To"] == "you@example.com"

    attachments = list(msg.iter_attachments())
    assert len(attachments) == 1
    att = attachments[0]
    assert att.get_filename() == "avalpha-2026-08-28.pdf"
    assert att.get_content_type() == "application/pdf"
    assert att.get_payload(decode=True) == b"%PDF-1.7 fake"
