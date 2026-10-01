"""Recipient construction, the place a misdirected email comes from.

_split_addresses and _collect_recipients decide who actually receives a
message. _build_message decides who the recipients can SEE. A defect in the
first sends mail to the wrong person; a defect in the second exposes a blind
copy. Both are unrecoverable once sent.
"""

from __future__ import annotations

import pytest

import server

# ── _split_addresses ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, []),
        ("", []),
        ("   ", []),
        (",", []),
        ("a@example.gr", ["a@example.gr"]),
        ("a@example.gr,b@example.gr", ["a@example.gr", "b@example.gr"]),
        ("  a@example.gr ,  b@example.gr  ", ["a@example.gr", "b@example.gr"]),
        ("a@example.gr,", ["a@example.gr"]),
        ("a@example.gr,,b@example.gr", ["a@example.gr", "b@example.gr"]),
        ("Ονομα <a@example.gr>", ["Ονομα <a@example.gr>"]),
    ],
)
def test_split_addresses(raw, expected):
    # BREAKS IN PRODUCTION: a trailing comma or a stray space becoming an empty
    # recipient makes SMTP reject the whole message, so nothing is delivered to
    # anyone, including the recipients that were valid.
    assert server._split_addresses(raw) == expected


# ── _collect_recipients ─────────────────────────────────────────────────────


def test_collect_recipients_with_only_to():
    # BREAKS IN PRODUCTION: None cc/bcc are the common case. If they were not
    # tolerated, every simple one-recipient email would fail.
    assert server._collect_recipients("a@example.gr") == ["a@example.gr"]


def test_collect_recipients_merges_to_cc_and_bcc_in_order():
    # BREAKS IN PRODUCTION: a bcc missing from the envelope means the blind
    # recipient never receives the message, and nothing reports the omission.
    assert server._collect_recipients("a@example.gr", "b@example.gr", "c@example.gr") == [
        "a@example.gr",
        "b@example.gr",
        "c@example.gr",
    ]


def test_collect_recipients_tolerates_empty_cc_and_bcc():
    # BREAKS IN PRODUCTION: the MCP layer passes "" rather than None for an
    # omitted optional string. An empty entry in the envelope is an SMTP error.
    assert server._collect_recipients("a@example.gr", "", "") == ["a@example.gr"]


def test_collect_recipients_dedupes_case_insensitively():
    # BREAKS IN PRODUCTION: a parent listed in both To and Cc with different
    # capitalisation receives the same email twice.
    got = server._collect_recipients("A@Example.gr", "a@example.gr", "  a@EXAMPLE.GR  ")
    assert got == ["A@Example.gr"]


def test_collect_recipients_returns_empty_for_whitespace_only():
    # BREAKS IN PRODUCTION: this empty list is what triggers the "No recipients
    # provided" guard in the send path. If it ever returned [""] the guard would
    # pass and smtplib would be handed an empty envelope address.
    assert server._collect_recipients("  ", None, None) == []


# ── bcc must be in the envelope but never in the headers ────────────────────


def test_bcc_is_not_present_in_any_visible_header():
    # BREAKS IN PRODUCTION: this is the classic bcc leak. If _build_message ever
    # started setting a Bcc header, every visible recipient would learn who was
    # blind-copied. In a school context that exposes, for example, a complaint
    # copied to the headmaster.
    msg = server._build_message(
        from_addr="user@sch.gr",
        to="parent@example.gr",
        subject="Θέμα",
        body="Κείμενο",
        cc="colleague@sch.gr",
        bcc="headmaster@sch.gr",
    )
    assert msg.get("Bcc") is None
    assert "headmaster@sch.gr" not in msg.as_string()


def test_bcc_is_still_in_the_smtp_envelope(imap, smtp):
    # BREAKS IN PRODUCTION: the counterpart to the test above. Hiding the bcc
    # from the headers is only correct if it still reaches the envelope; if both
    # were dropped, the blind recipient silently gets nothing.
    server.sch_send_mail(
        to="parent@example.gr",
        subject="Θέμα",
        body="Κείμενο",
        bcc="headmaster@sch.gr",
        send_now=True,
    )
    _from, rcpts, payload = smtp.sent[0]
    assert "headmaster@sch.gr" in rcpts
    assert "headmaster@sch.gr" not in payload


# ── Known defects, kept visible ─────────────────────────────────────────────


# REGRESSION GUARD. DEFECT (_build_message, FIXED 2026-09-17): _build_message accepts
# bcc and never uses it, so a DRAFT carries no record of the blind recipients. Because
# draft-first is the DEFAULT path, the normal flow is: ask for a blind copy, review the
# draft in webmail, hit send, blind recipient never receives it. The draft_saved dict
# does not even echo bcc back, while the sent dict does, so the caller gets no signal.
# Fix: set a Bcc header on the draft only, or return bcc with a warning that it will
# not survive webmail send.
def test_draft_preserves_bcc_recipients(imap):
    server.sch_send_mail(
        to="parent@example.gr",
        subject="Θέμα",
        body="Κείμενο",
        bcc="headmaster@sch.gr",
    )
    _mailbox, _flags, raw = imap.appended[0]
    assert b"headmaster@sch.gr" in raw


# REGRESSION GUARD. DEFECT (_split_addresses, FIXED 2026-09-17): _split_addresses splits on
# bare commas, so a quoted display name containing a comma becomes two bogus envelope
# recipients. Greek address books produce «Επώνυμο, Όνομα» routinely. The visible To:
# header stays correct because _build_message assigns the raw string, so the
# misdirection is invisible in the sent copy. Fix: email.utils.getaddresses.
def test_split_addresses_respects_quoted_display_names():
    assert server._split_addresses('"Πλέσσας, Δημήτρης" <d@example.gr>, b@example.gr') == [
        '"Πλέσσας, Δημήτρης" <d@example.gr>',
        "b@example.gr",
    ]
