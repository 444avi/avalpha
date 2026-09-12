from avalpha.config import Config
from avalpha.mailer import build_message, build_swing_message


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


def test_build_swing_message_single_breach():
    msg = build_swing_message(
        _config(), "holder@example.com", [("NVDA", -11.4, 102.30)]
    )
    assert msg["Subject"] == "avalpha alert — NVDA -11.4%"
    assert msg["From"] == "avalpha <you@example.com>"
    assert msg["To"] == "holder@example.com"
    assert msg["Reply-To"] == "avi@arboretuminvestments.net"
    assert not list(msg.iter_attachments())  # plain text, no PDF
    body = msg.get_content()
    assert "NVDA" in body and "-11.4%" in body and "$102.30" in body


def test_build_swing_message_multiple_breaches():
    msg = build_swing_message(
        _config(),
        "holder@example.com",
        [("NVDA", -11.4, 102.30), ("AAPL", 10.2, 242.10)],
    )
    assert msg["Subject"] == "avalpha alert — 2 holdings moved 10%+"
    body = msg.get_content()
    assert "NVDA" in body and "-11.4%" in body
    assert "AAPL" in body and "+10.2%" in body  # gains carry an explicit +
