"""Values that end up on an IMAP command line.

Query, folder, message id and folder name are all chosen by a model that is
also reading mail anyone can send. Two ways one of them could do more than its
tool intends:

1. A double quote in a search term closed the quoted SEARCH string, so the rest
   of the term became search criteria of the sender's choosing.
2. A CR LF inside any argument ends the IMAP command and starts another, one no
   tool exposes: re-select read-write, flag, expunge, delete a folder. Recent
   imaplib releases refuse these themselves; 3.12.14, which requires-python
   admits, sends them through.
"""

from __future__ import annotations

import pytest

import server

INJECTED = "INBOX\r\nA1 DELETE INBOX.Archive"


class _SearchRecorder:
    """Just enough IMAP for the ASCII path of sch_search_mail, recording criteria."""

    def __init__(self):
        self.criteria: list[str] = []

    def login(self, user, password):
        return ("OK", [b"LOGIN completed"])

    def select(self, mailbox="INBOX", readonly=False):
        return ("OK", [b"0"])

    def uid(self, command, *args):
        assert command == "SEARCH", f"_SearchRecorder does not model UID {command}"
        self.criteria.append(args[-1])
        return ("OK", [b""])

    def logout(self):
        return ("BYE", [b"Logging out"])


@pytest.fixture
def recorder(monkeypatch):
    conn = _SearchRecorder()
    monkeypatch.setattr(server.imaplib, "IMAP4_SSL", lambda *a, **k: conn)
    return conn


def test_quote_escapes_backslash_and_double_quote():
    assert server._imap_quote('say "hi" \\o/') == '"say \\"hi\\" \\\\o/"'


@pytest.mark.parametrize("bad", ["x\r\nA1 EXPUNGE", "x\nA1 EXPUNGE", "x\rA1", "x\x00y"])
def test_quote_refuses_line_breaks_and_nul(bad):
    with pytest.raises(ValueError, match="NUL, CR or LF"):
        server._imap_quote(bad)


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("subject", '(SUBJECT "say \\"hi\\"")'),
        ("from", '(FROM "say \\"hi\\"")'),
        ("body", '(BODY "say \\"hi\\"")'),
        ("all", '(OR OR SUBJECT "say \\"hi\\"" FROM "say \\"hi\\"" BODY "say \\"hi\\"")'),
    ],
)
def test_a_quote_in_a_search_term_stays_inside_the_search_string(recorder, field, expected):
    # BREAKS IN PRODUCTION: unescaped, 'say "hi"' closed the string after "say ",
    # and the server either rejected the search or ran criteria the term carried.
    assert server.sch_search_mail('say "hi"', field=field) == []
    assert recorder.criteria == [expected]


@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        pytest.param(server.sch_list_mail, {"folder": INJECTED}, id="list-folder"),
        pytest.param(server.sch_get_mail, {"msg_id": "1\r\nA1 EXPUNGE"}, id="get-msg_id"),
        pytest.param(server.sch_get_mail, {"msg_id": "1", "folder": INJECTED}, id="get-folder"),
        pytest.param(server.sch_search_mail, {"query": "x\r\nA1 EXPUNGE"}, id="search-query"),
        pytest.param(
            server.sch_search_mail, {"query": "x", "folder": INJECTED}, id="search-folder"
        ),
        pytest.param(
            server.sch_download_attachments, {"msg_id": "1\r\nA1 EXPUNGE"}, id="download-msg_id"
        ),
        pytest.param(server.sch_mail_stats, {"folder": INJECTED}, id="stats-folder"),
        pytest.param(
            server.sch_forward_mail,
            {"msg_id": "1\r\nA1 EXPUNGE", "to": "a@example.gr"},
            id="forward-msg_id",
        ),
        pytest.param(
            server.sch_move_mail, {"msg_id": "103", "dest_folder": INJECTED}, id="move-dest"
        ),
        pytest.param(
            server.sch_move_mail,
            {"msg_id": "103", "dest_folder": "INBOX.Archive", "source_folder": INJECTED},
            id="move-source",
        ),
        pytest.param(server.sch_create_folder, {"name": INJECTED}, id="create-name"),
    ],
)
def test_line_breaks_are_refused_before_any_connection(tool, kwargs):
    # BREAKS IN PRODUCTION: on an imaplib that does not refuse them itself, the
    # second line runs as an IMAP command of its own. No `imap` fixture here: a
    # tool that connected before checking would hit the default-deny stub and
    # raise AssertionError rather than ValueError.
    with pytest.raises(ValueError, match="NUL, CR or LF"):
        tool(**kwargs)
