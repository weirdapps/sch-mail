"""Message ids are IMAP UIDs, and a move expunges nothing but the message it moved.

Regression for a defect fixed on 2026-09-23. The ids this server handed out used to
be SEQUENCE numbers, which renumber every time any message leaves the folder, and
every move ended in a bare EXPUNGE. So: list INBOX, let webmail or a phone delete
one message, then move the message the listing called "3", and the server moved
the one after it. Two moves from one listing had the same defect with no other
client involved, because the first move's own expunge shifted the second id.

FakeMailServer models just enough of the server side to show it: a sequence
number is a position and shifts on expunge, a UID never changes, a bare EXPUNGE
purges every message flagged \\Deleted, UID commands silently ignore a UID that
does not exist, and UID MOVE / UID EXPUNGE are only accepted when the capability
list says so. The sequence-number commands are modelled too, so the old code runs
against the same fake and fails these tests rather than erroring out of them.
"""

from __future__ import annotations

import re

import pytest

import server

CAPABILITY_SETS = [
    pytest.param(("MOVE", "UIDPLUS"), id="move"),
    pytest.param(("UIDPLUS",), id="uidplus-only"),
    pytest.param((), id="neither"),
]


def _as_str(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _unquote(mailbox: str) -> str:
    if len(mailbox) >= 2 and mailbox[0] == mailbox[-1] == '"':
        return mailbox[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return mailbox


def _parse_set(message_set: str, highest: int) -> set[int]:
    """An IMAP sequence set ("3", "3,5", "2:4", "1:*") as a set of numbers."""
    out: set[int] = set()
    for part in message_set.split(","):
        lo, _, hi = part.partition(":")
        first = highest if lo == "*" else int(lo)
        last = first if not hi else (highest if hi == "*" else int(hi))
        out.update(range(min(first, last), max(first, last) + 1))
    return out


class FakeMailServer:
    """Server-side mailbox state, shared by every connection a test opens."""

    def __init__(self, folders: dict[str, list[int]], caps=()):
        self.folders = {
            name: [{"uid": uid, "flags": set(), "subject": f"msg {uid}"} for uid in uids]
            for name, uids in folders.items()
        }
        self.uidnext = {name: max(uids, default=0) + 1 for name, uids in folders.items()}
        self.caps = set(caps)
        self.commands: list[tuple] = []

    def subjects(self, folder: str) -> list[str]:
        return [m["subject"] for m in self.folders[folder]]

    # What another client (webmail, a phone) does between two tool calls.
    def expunged_elsewhere(self, folder: str, uid: int) -> None:
        self.folders[folder] = [m for m in self.folders[folder] if m["uid"] != uid]

    def flagged_deleted_elsewhere(self, folder: str, uid: int) -> None:
        for m in self.folders[folder]:
            if m["uid"] == uid:
                m["flags"].add("\\Deleted")


class FakeSession:
    """One IMAP connection to a FakeMailServer."""

    def __init__(self, mail: FakeMailServer):
        self.mail = mail
        self.folder: str | None = None
        self.readonly = True
        # imaplib fills .capabilities once, from the PRE-login greeting, while
        # servers commonly add MOVE and UIDPLUS only after login. Model that, so
        # code trusting the cached tuple takes the wrong path and a test says so.
        self.capabilities = ("IMAP4REV1",)

    def login(self, user, password):
        return ("OK", [b"LOGIN completed"])

    def logout(self):
        return ("BYE", [b"Logging out"])

    def capability(self):
        self.mail.commands.append(("CAPABILITY",))
        return ("OK", [" ".join(["IMAP4rev1", *sorted(self.mail.caps)]).encode()])

    def list(self, directory='""', pattern="*"):
        return ("OK", [f'(\\HasNoChildren) "." "{name}"'.encode() for name in self.mail.folders])

    def select(self, mailbox="INBOX", readonly=False):
        self.folder = _unquote(_as_str(mailbox))
        self.readonly = readonly
        return ("OK", [str(len(self.msgs)).encode()])

    @property
    def msgs(self) -> list[dict]:
        return self.mail.folders[self.folder]

    def _by_seq(self, message_set) -> list[dict]:
        wanted = _parse_set(_as_str(message_set), len(self.msgs))
        return [m for seq, m in enumerate(self.msgs, 1) if seq in wanted]

    def _by_uid(self, uid_set) -> list[dict]:
        # RFC 3501: a UID that does not exist is ignored, not an error.
        highest = max((m["uid"] for m in self.msgs), default=0)
        wanted = _parse_set(_as_str(uid_set), highest)
        return [m for m in self.msgs if m["uid"] in wanted]

    def _fetch_response(self, found: list[dict]):
        if not found:
            return ("OK", [None])
        data: list = []
        for m in found:
            seq = self.msgs.index(m) + 1
            raw = (
                "From: sender@example.gr\r\nTo: user@sch.gr\r\n"
                f"Subject: {m['subject']}\r\nDate: Mon, 15 Sep 2026 09:00:00 +0300\r\n"
                f"\r\nBody of {m['subject']}\r\n"
            ).encode()
            flags = " ".join(sorted(m["flags"]))
            data.append(
                (f"{seq} (UID {m['uid']} FLAGS ({flags}) RFC822 {{{len(raw)}}}".encode(), raw)
            )
            data.append(b")")
        return ("OK", data)

    def _copy(self, found: list[dict], dest) -> None:
        name = _unquote(_as_str(dest))
        for m in found:
            uid = self.mail.uidnext[name]
            self.mail.uidnext[name] += 1
            self.mail.folders[name].append(
                {"uid": uid, "flags": set(m["flags"]), "subject": m["subject"]}
            )

    def _purge(self, doomed: set[int]) -> None:
        self.mail.folders[self.folder] = [m for m in self.msgs if m["uid"] not in doomed]

    # Sequence-number commands: positions, which shift on every expunge.
    def search(self, charset, *criteria):
        self.mail.commands.append(("SEARCH", *criteria))
        return ("OK", [" ".join(str(n) for n in range(1, len(self.msgs) + 1)).encode()])

    def fetch(self, message_set, parts):
        self.mail.commands.append(("FETCH", _as_str(message_set), parts))
        return self._fetch_response(self._by_seq(message_set))

    def copy(self, message_set, dest):
        self.mail.commands.append(("COPY", _as_str(message_set), _as_str(dest)))
        self._copy(self._by_seq(message_set), dest)
        return ("OK", [b"COPY completed"])

    def store(self, message_set, op, flags):
        self.mail.commands.append(("STORE", _as_str(message_set), op, flags))
        assert not self.readonly, "STORE on a folder selected read-only"
        for m in self._by_seq(message_set):
            m["flags"].add("\\Deleted")
        return ("OK", [None])

    def expunge(self):
        self.mail.commands.append(("EXPUNGE",))
        assert not self.readonly, "EXPUNGE on a folder selected read-only"
        self._purge({m["uid"] for m in self.msgs if "\\Deleted" in m["flags"]})
        return ("OK", [None])

    # UID commands: identities, which never change.
    def uid(self, command, *args):
        command = command.upper()
        args = [_as_str(a) for a in args if a is not None]
        self.mail.commands.append(("UID", command, *args))
        if command == "SEARCH":
            found = self.msgs
            only = re.search(r"\bUID (\d+)\b", " ".join(args))
            if only:
                found = [m for m in found if m["uid"] == int(only.group(1))]
            return ("OK", [" ".join(str(m["uid"]) for m in found).encode()])
        found = self._by_uid(args[0])
        if command == "FETCH":
            return self._fetch_response(found)
        if command == "COPY":
            self._copy(found, args[1])
            return ("OK", [None])
        assert not self.readonly, f"UID {command} on a folder selected read-only"
        if command == "STORE":
            for m in found:
                m["flags"].add("\\Deleted")
            return ("OK", [None])
        if command == "EXPUNGE":
            assert "UIDPLUS" in self.mail.caps, "UID EXPUNGE sent to a server without UIDPLUS"
            self._purge({m["uid"] for m in found if "\\Deleted" in m["flags"]})
            return ("OK", [None])
        if command == "MOVE":
            assert "MOVE" in self.mail.caps, "UID MOVE sent to a server without MOVE"
            self._copy(found, args[1])
            self._purge({m["uid"] for m in found})
            return ("OK", [None])
        raise AssertionError(f"FakeSession does not model UID {command}")


@pytest.fixture
def mailserver(monkeypatch):
    """Build a FakeMailServer and route every IMAP connection to it."""

    def make(caps=("MOVE", "UIDPLUS"), extra_folders=()):
        folders = {"INBOX": [101, 102, 103, 104], "INBOX.Archive": []}
        folders.update({name: [] for name in extra_folders})
        mail = FakeMailServer(folders, caps)
        monkeypatch.setattr(server.imaplib, "IMAP4_SSL", lambda *a, **k: FakeSession(mail))
        return mail

    return make


def _ids(rows: list[dict]) -> dict[str, str]:
    return {row["subject"]: row["id"] for row in rows}


def test_listed_ids_are_uids_not_positions(mailserver):
    mailserver()
    assert _ids(server.sch_list_mail()) == {
        "msg 101": "101",
        "msg 102": "102",
        "msg 103": "103",
        "msg 104": "104",
    }


def test_search_hands_out_the_same_uids_as_list(mailserver):
    mailserver()
    assert _ids(server.sch_search_mail("msg")) == _ids(server.sch_list_mail())


@pytest.mark.parametrize("caps", CAPABILITY_SETS)
def test_move_after_another_client_expunged_moves_the_listed_message(mailserver, caps):
    mail = mailserver(caps)
    target = _ids(server.sch_list_mail())["msg 103"]
    mail.expunged_elsewhere("INBOX", 101)  # webmail deletes the oldest message

    result = server.sch_move_mail(target, "INBOX.Archive")

    assert result["status"] == "moved"
    assert mail.subjects("INBOX.Archive") == ["msg 103"]
    assert mail.subjects("INBOX") == ["msg 102", "msg 104"]


@pytest.mark.parametrize("caps", CAPABILITY_SETS)
def test_two_moves_from_one_listing_each_move_their_own_message(mailserver, caps):
    mail = mailserver(caps)
    ids = _ids(server.sch_list_mail())

    server.sch_move_mail(ids["msg 102"], "INBOX.Archive")
    server.sch_move_mail(ids["msg 103"], "INBOX.Archive")

    assert mail.subjects("INBOX.Archive") == ["msg 102", "msg 103"]
    assert mail.subjects("INBOX") == ["msg 101", "msg 104"]


def test_get_mail_after_another_client_expunged_reads_the_listed_message(mailserver):
    mail = mailserver()
    target = _ids(server.sch_list_mail())["msg 103"]
    mail.expunged_elsewhere("INBOX", 101)

    assert server.sch_get_mail(target)["subject"] == "msg 103"


@pytest.mark.parametrize(
    "caps",
    [pytest.param(("MOVE", "UIDPLUS"), id="move"), pytest.param(("UIDPLUS",), id="uidplus-only")],
)
def test_move_does_not_purge_what_another_client_flagged_deleted(mailserver, caps):
    mail = mailserver(caps)
    mail.flagged_deleted_elsewhere("INBOX", 101)  # flagged on a phone, not yet purged

    server.sch_move_mail("103", "INBOX.Archive")

    assert mail.subjects("INBOX") == ["msg 101", "msg 102", "msg 104"]
    assert ("EXPUNGE",) not in mail.commands


def test_without_move_or_uidplus_the_fallback_is_a_bare_expunge(mailserver):
    # The documented cost of the last-resort path in server._expunge_uid: the
    # right message still moves, and anything already flagged \Deleted by
    # another client is purged with it.
    mail = mailserver(())
    mail.flagged_deleted_elsewhere("INBOX", 101)

    server.sch_move_mail("103", "INBOX.Archive")

    assert mail.subjects("INBOX.Archive") == ["msg 103"]
    assert mail.subjects("INBOX") == ["msg 102", "msg 104"]
    assert ("EXPUNGE",) in mail.commands


def test_capabilities_are_read_after_login_not_from_the_greeting(mailserver):
    mail = mailserver(("MOVE", "UIDPLUS"))

    server.sch_move_mail("103", "INBOX.Archive")

    assert ("UID", "MOVE", "103", "INBOX.Archive") in mail.commands
    assert not any(c[:2] == ("UID", "COPY") for c in mail.commands)


def test_move_quotes_a_destination_with_a_space(mailserver):
    # imaplib quotes the mailbox for COPY but passes UID MOVE arguments through
    # raw, so an unquoted "Archive 2026" would be rejected by the server.
    mail = mailserver(extra_folders=["INBOX.Archive 2026"])

    server.sch_move_mail("103", "INBOX.Archive 2026")

    assert ("UID", "MOVE", "103", '"INBOX.Archive 2026"') in mail.commands
    assert mail.subjects("INBOX.Archive 2026") == ["msg 103"]


def test_move_leaves_a_prequoted_destination_alone(mailserver):
    # Callers pre-quoted names with spaces before _q existed; quoting them again
    # double-quoted them and the move failed (round-3 review).
    mail = mailserver(extra_folders=["INBOX.Archive 2026"])

    server.sch_move_mail("103", '"INBOX.Archive 2026"')

    assert ("UID", "MOVE", "103", '"INBOX.Archive 2026"') in mail.commands


@pytest.mark.parametrize("bad", ["1:*", "103,104", "102:103", "abc", "", "１０３"])
def test_move_refuses_anything_but_a_single_uid(mailserver, bad):
    mail = mailserver()

    result = server.sch_move_mail(bad, "INBOX.Archive")

    assert "error" in result
    assert mail.subjects("INBOX") == ["msg 101", "msg 102", "msg 103", "msg 104"]
    assert mail.subjects("INBOX.Archive") == []


def test_move_of_a_uid_that_is_gone_reports_not_found(mailserver):
    # UID COPY / UID MOVE silently ignore a UID that does not exist, so without
    # an explicit check the tool would report "moved" having moved nothing.
    mail = mailserver()
    mail.expunged_elsewhere("INBOX", 103)

    result = server.sch_move_mail("103", "INBOX.Archive")

    assert "not found" in result["error"]
    assert mail.subjects("INBOX.Archive") == []
