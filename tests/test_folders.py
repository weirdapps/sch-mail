"""_find_special_folder and _save_to_folder.

sch.gr folder naming is not guaranteed to be English, and the candidate lists
(DRAFTS_FOLDER_CANDIDATES, SENT_FOLDER_CANDIDATES) are English-only. The
question these tests answer is what happens when nothing matches. The dangerous
outcome would be silently appending to INBOX: a "draft" containing an unsent,
unreviewed message would appear in the inbox as an ordinary read email and be
lost, while the owner waits in webmail for a draft that never shows up.
"""

from __future__ import annotations

import pytest

import server


class _Lister:
    """Minimal IMAP stand-in exposing only LIST, which is all this helper uses."""

    def __init__(self, status="OK", lines=None):
        self._status = status
        self._lines = lines or []

    def list(self, directory='""', pattern="*"):
        return (self._status, self._lines)


def _line(name: str) -> bytes:
    return b'(\\HasNoChildren) "." "' + name.encode("utf-8") + b'"'


# ── Finding the folder ──────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["Drafts", "INBOX.Drafts"])
def test_finds_a_drafts_folder_under_each_supported_name(name):
    # BREAKS IN PRODUCTION: sch.gr uses "." as its hierarchy separator, so the
    # real folder is INBOX.Drafts, not Drafts. Matching only the bare name would
    # send every draft to a nonexistent mailbox.
    conn = _Lister(lines=[_line("INBOX"), _line(name)])
    assert server._find_special_folder(conn, server.DRAFTS_FOLDER_CANDIDATES) == name


def test_prefers_the_earliest_candidate_when_several_exist():
    # BREAKS IN PRODUCTION: a mailbox holding both Drafts and INBOX.Drafts must
    # resolve deterministically, or drafts scatter across two folders.
    conn = _Lister(lines=[_line("INBOX.Drafts"), _line("Drafts")])
    assert server._find_special_folder(conn, server.DRAFTS_FOLDER_CANDIDATES) == "Drafts"


def test_finds_the_sent_folder_separately_from_drafts():
    # BREAKS IN PRODUCTION: archiving a sent message into Drafts would leave a
    # copy that looks unsent, inviting the owner to send it a second time.
    conn = _Lister(lines=[_line("INBOX.Drafts"), _line("INBOX.Sent")])
    assert server._find_special_folder(conn, server.SENT_FOLDER_CANDIDATES) == "INBOX.Sent"


def test_parses_an_unquoted_folder_name():
    # BREAKS IN PRODUCTION: not every IMAP server quotes mailbox names in LIST
    # responses. An unquoted name that failed to parse would make every folder
    # invisible and force the fallback path.
    conn = _Lister(lines=[b'(\\HasNoChildren) "." Drafts'])
    assert server._find_special_folder(conn, server.DRAFTS_FOLDER_CANDIDATES) == "Drafts"


# ── Failing cleanly ─────────────────────────────────────────────────────────


def test_non_english_only_mailbox_falls_back_without_targeting_inbox():
    # BREAKS IN PRODUCTION: this is the whole point of the file. A mailbox whose
    # Drafts folder is named in Greek matches no candidate. The fallback must not
    # be INBOX, because appending an unsent message to INBOX loses it among real
    # mail with no error anywhere. Today it returns the literal "Drafts", which
    # does not exist, and the APPEND then fails loudly. That is the correct
    # behaviour and this test pins it.
    conn = _Lister(lines=[_line("INBOX"), _line("Πρόχειρα"), _line("Απεσταλμένα")])
    target = server._find_special_folder(conn, server.DRAFTS_FOLDER_CANDIDATES)
    assert target != "INBOX"
    assert target == "Drafts"


def test_falls_back_without_raising_when_list_fails():
    # BREAKS IN PRODUCTION: a LIST failure during a transient server problem must
    # not crash the send tool with an unhandled exception.
    conn = _Lister(status="NO", lines=[])
    assert server._find_special_folder(conn, server.DRAFTS_FOLDER_CANDIDATES) == "Drafts"


def test_falls_back_when_the_mailbox_is_empty():
    conn = _Lister(lines=[])
    assert server._find_special_folder(conn, server.SENT_FOLDER_CANDIDATES) == "Sent"


def test_ignores_non_bytes_entries_in_the_list_response():
    # BREAKS IN PRODUCTION: imaplib yields tuples for literal-continuation
    # responses. A TypeError here would break folder resolution on any server
    # that uses them.
    conn = _Lister(lines=[(b'(\\HasNoChildren) "." {6}', b"Drafts"), _line("INBOX.Drafts")])
    assert server._find_special_folder(conn, server.DRAFTS_FOLDER_CANDIDATES) == "INBOX.Drafts"


# ── _save_to_folder must surface a failure, never swallow it ────────────────


def test_save_to_folder_resolves_the_drafts_sentinel(imap):
    # BREAKS IN PRODUCTION: the "__DRAFTS__" sentinel reaching APPEND verbatim
    # would create a junk top-level folder named __DRAFTS__, or fail outright.
    msg = server._build_message("user@sch.gr", "a@example.gr", "s", "b")
    result = server._save_to_folder("__DRAFTS__", msg)
    assert result == {"status": "ok", "folder": "INBOX.Drafts"}
    assert imap.appended[0][0] == "INBOX.Drafts"


def test_save_to_folder_passes_an_explicit_folder_through_unchanged(imap):
    msg = server._build_message("user@sch.gr", "a@example.gr", "s", "b")
    server._save_to_folder("INBOX.Archive2026", msg)
    assert imap.appended[0][0] == "INBOX.Archive2026"


def test_save_to_folder_reports_a_rejected_append_rather_than_claiming_success(imap):
    # BREAKS IN PRODUCTION: this is the clean-failure guarantee for the
    # non-English mailbox case above. When the fallback folder does not exist the
    # server answers NO with [TRYCREATE], and that must reach the caller as an
    # error, not as a silent success that loses the message.
    imap.append_status = "NO"
    msg = server._build_message("user@sch.gr", "a@example.gr", "s", "b")
    result = server._save_to_folder("__DRAFTS__", msg)
    assert "error" in result
    assert "APPEND to INBOX.Drafts failed" in result["error"]


def test_save_to_folder_always_logs_out(imap):
    # BREAKS IN PRODUCTION: sch.gr limits concurrent IMAP connections. Leaking a
    # connection on every send eventually locks the account out of its own mail.
    imap.append_status = "NO"
    msg = server._build_message("user@sch.gr", "a@example.gr", "s", "b")
    server._save_to_folder("__DRAFTS__", msg)
    assert imap.logged_out is True
