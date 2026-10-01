"""_build_message and _guess_mime.

On a Greek School Network mailbox, non-ASCII subjects and bodies are the normal
case, not an edge case. Every assertion here is about a message that must
survive the trip through smtplib.sendmail(), which encodes the flattened string
as strict ASCII and raises UnicodeEncodeError on anything else.
"""

from __future__ import annotations

import email
from email.header import decode_header

import pytest

import server

# ── Greek subjects and bodies ───────────────────────────────────────────────


def test_greek_subject_is_rfc2047_encoded():
    # BREAKS IN PRODUCTION: a raw 8-bit Subject header is rejected or mangled by
    # intermediate MTAs, and recipients see mojibake where the subject should be.
    msg = server._build_message(
        from_addr="user@sch.gr",
        to="parent@example.gr",
        subject="Ενημέρωση για τη συνάντηση",
        body="Κείμενο",
    )
    # The encoding happens when the message is flattened for SMTP or IMAP APPEND,
    # so assert on the wire form rather than on the in-memory header object.
    on_the_wire = email.message_from_string(msg.as_string())["Subject"]
    assert on_the_wire.startswith("=?utf-8?")
    decoded, charset = decode_header(on_the_wire)[0]
    assert charset == "utf-8"
    assert decoded.decode("utf-8") == "Ενημέρωση για τη συνάντηση"


def test_flattened_greek_message_is_ascii_safe():
    # BREAKS IN PRODUCTION: this is the one that crashes the send outright.
    # sch_send_mail passes msg.as_string() to smtplib.sendmail, which does
    # .encode('ascii') on a str payload. If any Greek survived unencoded into the
    # flattened form, every Greek email would raise UnicodeEncodeError at dispatch.
    msg = server._build_message(
        from_addr="user@sch.gr",
        to="parent@example.gr",
        subject="Καλημέρα σε όλους",
        body="Καλημέρα σας, η συνάντηση μεταφέρεται για την Παρασκευή.",
    )
    msg.as_string().encode("ascii")  # must not raise


def test_greek_body_round_trips_intact():
    # BREAKS IN PRODUCTION: the recipient opens the email and the body is
    # unreadable. Base64 with an explicit utf-8 charset is what makes this work.
    body = "Καλημέρα σας,\n\nΗ συνάντηση μεταφέρεται για την επόμενη εβδομάδα."
    msg = server._build_message("user@sch.gr", "parent@example.gr", "Θέμα", body)
    parsed = email.message_from_string(msg.as_string())
    assert server._get_text_body(parsed) == body


def test_plain_text_body_is_sent_as_text_plain_not_html():
    # BREAKS IN PRODUCTION: a plain body declared as text/html means any "<" the
    # sender types is swallowed as a tag, and line breaks collapse into one
    # paragraph. html defaults to False and must stay that way.
    msg = server._build_message("user@sch.gr", "a@example.gr", "s", "line 1\nline 2")
    types = [p.get_content_type() for p in msg.walk()]
    assert "text/plain" in types
    assert "text/html" not in types


def test_html_true_produces_a_text_html_part():
    # BREAKS IN PRODUCTION: an HTML body sent as text/plain shows the recipient
    # raw markup instead of a formatted message.
    msg = server._build_message("user@sch.gr", "a@example.gr", "s", "<b>Καλημέρα</b>", html=True)
    assert "text/html" in [p.get_content_type() for p in msg.walk()]


# ── Headers ─────────────────────────────────────────────────────────────────


def test_cc_header_is_set_and_to_is_preserved_verbatim():
    # BREAKS IN PRODUCTION: a missing Cc header means the cc'd colleague receives
    # the message but nobody on the thread can see they were included, so
    # reply-all drops them.
    msg = server._build_message(
        "user@sch.gr", "a@example.gr,b@example.gr", "s", "b", cc="c@example.gr"
    )
    assert msg["To"] == "a@example.gr,b@example.gr"
    assert msg["Cc"] == "c@example.gr"


def test_no_cc_header_when_cc_is_absent():
    # BREAKS IN PRODUCTION: an empty Cc: header is malformed and some clients
    # display a blank recipient row.
    msg = server._build_message("user@sch.gr", "a@example.gr", "s", "b")
    assert msg.get("Cc") is None


def test_message_id_and_date_are_always_set():
    # BREAKS IN PRODUCTION: mail without a Message-ID scores heavily as spam and
    # cannot be threaded; the send path also returns msg["Message-ID"] to the
    # caller as the handle for the message it just created.
    msg = server._build_message("user@sch.gr", "a@example.gr", "s", "b")
    assert msg["Message-ID"].endswith("@sch.gr>")
    assert msg["Date"]


def test_threading_headers_are_set_only_when_supplied():
    # BREAKS IN PRODUCTION: a stray empty In-Reply-To breaks threading in the
    # recipient's client; a missing one when replying starts a new thread.
    threaded = server._build_message(
        "user@sch.gr", "a@example.gr", "s", "b", in_reply_to="<x@y>", references="<x@y>"
    )
    assert threaded["In-Reply-To"] == "<x@y>"
    plain = server._build_message("user@sch.gr", "a@example.gr", "s", "b")
    assert plain.get("In-Reply-To") is None


# ── Attachments ─────────────────────────────────────────────────────────────


def test_attachment_from_disk_keeps_its_filename_and_mime_type(tmp_path):
    # BREAKS IN PRODUCTION: an attachment typed as application/octet-stream
    # arrives as an unnamed blob the recipient cannot open, and a PDF circular
    # becomes undeliverable in practice.
    pdf = tmp_path / "egkyklios.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake content")

    msg = server._build_message("user@sch.gr", "a@example.gr", "s", "b", attachments=[str(pdf)])

    parts = [p for p in msg.walk() if p.get_filename()]
    assert len(parts) == 1
    assert parts[0].get_filename() == "egkyklios.pdf"
    assert parts[0].get_content_type() == "application/pdf"
    assert parts[0].get_payload(decode=True) == b"%PDF-1.4 fake content"


def test_missing_attachment_path_raises_rather_than_being_skipped(tmp_path):
    # BREAKS IN PRODUCTION: a missing path used to be skipped, so a typo in it
    # sent the email, or saved the draft, without the attachment and nothing
    # said so. It must fail the whole message instead, even when the other
    # attachment is fine.
    real = tmp_path / "real.txt"
    real.write_bytes(b"x")
    with pytest.raises(FileNotFoundError, match="nope.pdf"):
        server._build_message(
            "user@sch.gr",
            "a@example.gr",
            "s",
            "b",
            attachments=[str(real), str(tmp_path / "nope.pdf")],
        )


def test_a_folder_is_refused_as_an_attachment(tmp_path):
    # BREAKS IN PRODUCTION: a folder path used to be skipped like a missing file.
    folder = tmp_path / "circulars"
    folder.mkdir()
    with pytest.raises(FileNotFoundError, match="not a regular file"):
        server._build_message("user@sch.gr", "a@example.gr", "s", "b", attachments=[str(folder)])


def test_extra_attachments_are_reattached_for_forwarding():
    # BREAKS IN PRODUCTION: sch_forward_mail feeds this parameter, so a defect
    # here strips attachments off every forwarded message.
    msg = server._build_message(
        "user@sch.gr",
        "a@example.gr",
        "Fwd: s",
        "b",
        extra_attachments=[("photo.jpg", b"\xff\xd8\xff-fake", "image/jpeg")],
    )
    parts = [p for p in msg.walk() if p.get_filename() == "photo.jpg"]
    assert len(parts) == 1
    assert parts[0].get_content_type() == "image/jpeg"
    assert parts[0].get_payload(decode=True) == b"\xff\xd8\xff-fake"


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("a.pdf", "application/pdf"),
        ("a.txt", "text/plain"),
        ("a.jpg", "image/jpeg"),
        ("a.png", "image/png"),
        ("a.unknownext", "application/octet-stream"),
        ("noextension", "application/octet-stream"),
    ],
)
def test_guess_mime(filename, expected):
    # BREAKS IN PRODUCTION: the wrong maintype means MIMEBase is constructed with
    # a bogus type and the recipient's client refuses to preview the file.
    assert server._guess_mime(filename) == expected


def test_guess_mime_handles_a_greek_filename():
    # BREAKS IN PRODUCTION: Greek filenames are the norm on sch.gr accounts. If the
    # extension lookup tripped on the non-ASCII stem, every Greek-named
    # attachment would be typed as an opaque blob.
    assert server._guess_mime("εγκύκλιος.pdf") == "application/pdf"
