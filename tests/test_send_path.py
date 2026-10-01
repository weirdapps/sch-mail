"""send_now defaults to False for both sch_send_mail and sch_forward_mail.

This is the highest-value file in the repo. Both tools' docstrings and the
README promise draft-first. If that promise ever silently inverts, every
message the assistant composes on the owner's behalf is delivered the instant
it is written, with no review step, from a real Greek School Network account.
There is no undo for a sent email.
"""

from __future__ import annotations

import inspect
import socket

import pytest
from conftest import NetworkAccessAttempted, build_raw_message

import server

# ── The guards themselves must work ─────────────────────────────────────────


def test_real_sockets_are_blocked():
    # BREAKS IN PRODUCTION: if the autouse guard in conftest stopped working, a
    # careless future test could open a real connection to mail.sch.gr from CI.
    # Every other test in this suite rests on this one.
    with pytest.raises(NetworkAccessAttempted):
        socket.create_connection(("mail.sch.gr", 993), timeout=1)
    with pytest.raises(NetworkAccessAttempted):
        socket.getaddrinfo("mail.sch.gr", 993)


def test_transports_are_denied_until_a_test_opts_in():
    # BREAKS IN PRODUCTION: the draft-path tests prove "SMTP was never touched"
    # only because constructing SMTP_SSL raises. This asserts that mechanism.
    with pytest.raises(AssertionError, match="SMTP_SSL"):
        server.smtplib.SMTP_SSL("mail.sch.gr", 465)
    with pytest.raises(AssertionError, match="IMAP4_SSL"):
        server.imaplib.IMAP4_SSL("mail.sch.gr", 993)


def test_real_credentials_are_never_read():
    # BREAKS IN PRODUCTION: a test reading ~/.sch-mail/credentials.json could
    # print a live password into CI logs.
    assert server._load_credentials() == ("user@sch.gr", "placeholder-not-a-real-password")
    assert not server.CRED_PATH.exists()


# ── The guarantee, asserted at the signature ────────────────────────────────


@pytest.mark.parametrize("tool", [server.sch_send_mail, server.sch_forward_mail])
def test_send_now_parameter_defaults_to_false(tool):
    # BREAKS IN PRODUCTION: flipping this default to True turns every draft into
    # an immediate dispatch. Asserting at the signature catches the regression at
    # its source, even if some future refactor stops exercising the runtime path.
    assert inspect.signature(tool).parameters["send_now"].default is False


# ── sch_send_mail ───────────────────────────────────────────────────────────


def test_send_mail_without_send_now_saves_a_draft_and_never_touches_smtp(imap):
    # BREAKS IN PRODUCTION: mail leaves the mailbox without the owner ever seeing
    # it. The autouse `forbid_transports` fixture makes any SMTP_SSL construction
    # raise, so this test fails loudly rather than silently sending.
    result = server.sch_send_mail(
        to="parent@example.gr",
        subject="Ενημέρωση για τη συνάντηση",
        body="Καλησπέρα, στέλνω την ενημέρωση.",
    )

    assert result["status"] == "draft_saved"
    assert len(imap.appended) == 1
    mailbox, flags, _raw = imap.appended[0]
    assert mailbox == "INBOX.Drafts"
    assert flags == "(\\Draft \\Seen)"
    assert result["folder"] == "INBOX.Drafts"
    assert imap.logged_out is True


def test_send_mail_draft_is_flagged_draft_not_sent(imap):
    # BREAKS IN PRODUCTION: without the \Draft flag the message lands in the
    # Drafts folder but webmail renders it as a read message rather than an
    # editable draft, so the owner cannot open, review and send it.
    server.sch_send_mail(to="a@example.gr", subject="s", body="b")
    _mailbox, flags, _raw = imap.appended[0]
    assert "\\Draft" in flags


def test_send_mail_with_send_now_dispatches_via_smtp(imap, smtp):
    # BREAKS IN PRODUCTION: if send_now=True stopped actually sending, the owner
    # would believe a message went out when it never did. Silent non-delivery is
    # as damaging as an accidental send.
    result = server.sch_send_mail(
        to="a@example.gr",
        subject="Θέμα",
        body="Κείμενο",
        cc="b@example.gr",
        send_now=True,
    )

    assert result["status"] == "sent"
    assert len(smtp.sent) == 1
    from_addr, rcpts, _payload = smtp.sent[0]
    assert from_addr == "user@sch.gr"
    assert rcpts == ["a@example.gr", "b@example.gr"]
    assert smtp.quit_called is True


def test_send_mail_send_now_archives_a_copy_to_sent(imap, smtp):
    # BREAKS IN PRODUCTION: no record of what was sent. The owner cannot prove
    # what was communicated, or to whom.
    result = server.sch_send_mail(to="a@example.gr", subject="s", body="b", send_now=True)
    assert result["sent_folder_archive"] == "INBOX.Sent"
    mailbox, flags, _raw = imap.appended[0]
    assert mailbox == "INBOX.Sent"
    assert "\\Draft" not in flags


def test_send_mail_with_no_recipients_refuses_before_opening_smtp(imap):
    # BREAKS IN PRODUCTION: an empty recipient list reaching smtp.sendmail is an
    # SMTP protocol error and, on some servers, a message accepted with no
    # envelope. The check must happen before the connection. `smtp` is not
    # requested here, so SMTP_SSL is still the exploding stub.
    result = server.sch_send_mail(to="   ", subject="s", body="b", send_now=True)
    assert "error" in result
    assert result["error"] == "No recipients provided"


def test_send_mail_reports_the_append_failure_instead_of_claiming_success(imap):
    # BREAKS IN PRODUCTION: a failed APPEND reported as "draft_saved" means the
    # owner goes looking in webmail for a draft that does not exist, and the
    # content is gone.
    imap.append_status = "NO"
    result = server.sch_send_mail(to="a@example.gr", subject="s", body="b")
    assert "error" in result
    assert "APPEND" in result["error"]
    assert result.get("status") != "draft_saved"


# ── sch_forward_mail ────────────────────────────────────────────────────────


def test_forward_mail_without_send_now_saves_a_draft_and_never_touches_smtp(imap):
    # BREAKS IN PRODUCTION: forwarding is the higher-risk operation, because the
    # forwarded content was written by someone else and may contain material the
    # owner would not choose to pass on. Auto-dispatch removes the only review step.
    imap.fetch_payload = build_raw_message(subject="Αρχικό θέμα", body="Αρχικό κείμενο")

    result = server.sch_forward_mail(msg_id="7", to="colleague@sch.gr")

    assert result["status"] == "draft_saved"
    assert result["subject"] == "Fwd: Αρχικό θέμα"
    assert len(imap.appended) == 1
    mailbox, flags, _raw = imap.appended[0]
    assert mailbox == "INBOX.Drafts"
    assert flags == "(\\Draft \\Seen)"


def test_forward_mail_with_send_now_dispatches_via_smtp(imap, smtp):
    # BREAKS IN PRODUCTION: same silent non-delivery risk as the send path.
    imap.fetch_payload = build_raw_message(subject="Θέμα", body="Κείμενο")

    result = server.sch_forward_mail(msg_id="7", to="colleague@sch.gr", send_now=True)

    assert result["status"] == "sent"
    assert len(smtp.sent) == 1
    _from, rcpts, _payload = smtp.sent[0]
    assert rcpts == ["colleague@sch.gr"]


def test_forward_mail_preserves_attachments(imap):
    # BREAKS IN PRODUCTION: a forwarded circular arrives at the recipient with the
    # PDF missing and nothing in the response says so.
    imap.fetch_payload = build_raw_message(
        subject="Εγκύκλιος",
        body="Δείτε συνημμένο",
        attachment=("egkyklios.pdf", b"%PDF-1.4 fake", "application/pdf"),
    )

    result = server.sch_forward_mail(msg_id="7", to="colleague@sch.gr")

    assert result["forwarded_attachments"] == 1
    _mailbox, _flags, raw = imap.appended[0]
    assert b"egkyklios.pdf" in raw


def test_forward_mail_does_not_double_prefix_an_existing_fwd(imap):
    # BREAKS IN PRODUCTION: "Fwd: Fwd: Fwd: ..." subjects on a thread that gets
    # forwarded down a chain of colleagues.
    imap.fetch_payload = build_raw_message(subject="Fwd: Εγκύκλιος")
    result = server.sch_forward_mail(msg_id="7", to="colleague@sch.gr")
    assert result["subject"] == "Fwd: Εγκύκλιος"


def test_forward_mail_reports_a_missing_source_message(imap):
    # BREAKS IN PRODUCTION: forwarding a message id that does not exist must not
    # produce an empty forward sent to a real recipient.
    imap.fetch_payload = None
    result = server.sch_forward_mail(msg_id="999", to="colleague@sch.gr")
    assert "error" in result
    assert imap.appended == []
