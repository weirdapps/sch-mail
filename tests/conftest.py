"""Shared fixtures for the sch-mail suite.

This module exists to make three whole classes of accident impossible, because
this package can send email from a real Greek School Network account.

1. NO TEST MAY OPEN A SOCKET. A test that reached mail.sch.gr would, at best,
   hammer the sch.gr mail server from CI and, at worst, deliver live email from
   a real mailbox. The guards below are autouse and default-deny: sockets,
   IMAP4_SSL and SMTP_SSL all raise a named error unless a test explicitly opts
   into a recorded fake. A future careless test cannot quietly start dialling
   out; it fails loudly on the first attempt.

2. NO TEST MAY READ THE REAL CREDENTIALS. ``server._load_credentials`` reads
   ~/.sch-mail/credentials.json, which holds a live password. It is replaced
   everywhere, and CRED_PATH is redirected at a nonexistent temp path so that
   any code path bypassing the stub still cannot find the real file. The
   placeholder password below is a literal string, not a credential.

3. NO TEST MAY READ FROM OR WRITE INTO A REAL USER FOLDER. Attachments and
   downloads are confined to the folders SCH_MAIL_ALLOWED_DIRS names, and it is
   pinned to each test's own tmp_path, which replaces the built-in defaults, so
   ~/Downloads is refused and a developer's own setting of the variable cannot
   change what the suite sees. Tests of the built-in defaults delete it
   explicitly.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402

FAKE_USER = "user@sch.gr"
FAKE_PASSWORD = "placeholder-not-a-real-password"

# A realistic IMAP LIST response from a server using "." as the hierarchy
# separator, which is what sch.gr does.
LIST_ENGLISH = [
    b'(\\HasNoChildren) "." "INBOX"',
    b'(\\HasNoChildren) "." "INBOX.Drafts"',
    b'(\\HasNoChildren) "." "INBOX.Sent"',
    b'(\\HasNoChildren) "." "INBOX.Trash"',
]


class NetworkAccessAttempted(RuntimeError):
    """Raised when a test tries to touch the network. Always a bug in the test."""


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    """Make every real socket operation fail loudly, in every test."""

    def _boom(*args, **kwargs):
        raise NetworkAccessAttempted(
            "A test attempted a real network connection. Tests must never reach "
            "mail.sch.gr. Use the `imap` / `smtp` fixtures instead."
        )

    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(socket.socket, "connect_ex", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr(socket, "getaddrinfo", _boom)


@pytest.fixture(autouse=True)
def fake_credentials(monkeypatch, tmp_path):
    """Never read the real credentials file, and never emit a real secret."""
    monkeypatch.setattr(server, "CRED_PATH", tmp_path / "absent" / "credentials.json")
    monkeypatch.setattr(
        server, "_load_credentials", lambda account=None: (FAKE_USER, FAKE_PASSWORD)
    )


@pytest.fixture(autouse=True)
def allowed_dirs(monkeypatch, tmp_path):
    """Confine attachment reads and downloads to this test's tmp_path."""
    monkeypatch.setenv(server.ALLOWED_DIRS_ENV, str(tmp_path))


class FakeIMAP:
    """Records IMAP traffic instead of performing it.

    Configure per test via the attributes on the instance handed back by the
    `imap` fixture: ``list_lines``, ``append_status``, ``fetch_payload``.
    """

    def __init__(self, host=None, port=None, ssl_context=None):
        self.host = host
        self.port = port
        self.login_calls: list[tuple[str, str]] = []
        self.appended: list[tuple[str, str, bytes]] = []
        self.selected: list[tuple[str, bool]] = []
        self.logged_out = False
        self.list_lines = list(LIST_ENGLISH)
        self.append_status = "OK"
        self.fetch_payload: bytes | None = None

    def login(self, user, password):
        self.login_calls.append((user, password))
        return ("OK", [b"LOGIN completed"])

    def list(self, directory='""', pattern="*"):
        return ("OK", self.list_lines)

    def select(self, folder="INBOX", readonly=False):
        self.selected.append((folder, readonly))
        return ("OK", [b"1"])

    def fetch(self, message_set, message_parts):
        if self.fetch_payload is None:
            return ("NO", [None])
        return ("OK", [(b"1 (RFC822 {0})", self.fetch_payload)])

    def uid(self, command, *args):
        # server.py addresses messages by UID only, so FETCH arrives as UID
        # FETCH. Moves are modelled by the stateful fake in test_uid_addressing.
        if command.upper() == "FETCH":
            return self.fetch(*args)
        raise AssertionError(f"FakeIMAP does not model UID {command}")

    def append(self, mailbox, flags, date_time, message):
        self.appended.append((mailbox, flags, message))
        if self.append_status != "OK":
            return (self.append_status, [b"[TRYCREATE] Mailbox does not exist"])
        return ("OK", [b"APPEND completed"])

    def logout(self):
        self.logged_out = True
        return ("BYE", [b"Logging out"])


class FakeSMTP:
    """Records SMTP traffic instead of performing it."""

    def __init__(self, host=None, port=None, context=None):
        self.host = host
        self.port = port
        self.login_calls: list[tuple[str, str]] = []
        self.sent: list[tuple[str, list[str], str]] = []
        self.quit_called = False

    def login(self, user, password):
        self.login_calls.append((user, password))

    def sendmail(self, from_addr, to_addrs, msg):
        self.sent.append((from_addr, list(to_addrs), msg))
        return {}

    def quit(self):
        self.quit_called = True


class _Exploding:
    """Default-deny stand-in. Constructing one means the code tried to connect."""

    def __init__(self, what):
        self._what = what

    def __call__(self, *args, **kwargs):
        raise AssertionError(
            f"{self._what} was constructed during a test that did not opt in. "
            f"For the send path this usually means a draft-only operation "
            f"attempted to dispatch mail."
        )


@pytest.fixture(autouse=True)
def forbid_transports(monkeypatch):
    """IMAP and SMTP are both denied until a test asks for a recorded fake."""
    monkeypatch.setattr(server.imaplib, "IMAP4_SSL", _Exploding("imaplib.IMAP4_SSL"))
    monkeypatch.setattr(server.smtplib, "SMTP_SSL", _Exploding("smtplib.SMTP_SSL"))


@pytest.fixture
def imap(monkeypatch):
    """Opt in to a recorded IMAP connection. Returns the single FakeIMAP used."""
    conn = FakeIMAP()
    monkeypatch.setattr(server.imaplib, "IMAP4_SSL", lambda *a, **k: conn)
    return conn


@pytest.fixture
def smtp(monkeypatch):
    """Opt in to a recorded SMTP connection. Returns the single FakeSMTP used."""
    client = FakeSMTP()
    monkeypatch.setattr(server.smtplib, "SMTP_SSL", lambda *a, **k: client)
    return client


def build_raw_message(
    subject: str = "Test",
    body: str = "Body text",
    charset: str = "utf-8",
    from_addr: str = "sender@sch.gr",
    to_addr: str = "user@sch.gr",
    attachment: tuple[str, bytes, str] | None = None,
) -> bytes:
    """Produce RFC822 bytes as an IMAP FETCH would return them."""
    from email.mime.base import MIMEBase
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    outer = MIMEMultipart("mixed")
    outer["From"] = from_addr
    outer["To"] = to_addr
    outer["Subject"] = subject
    outer["Date"] = "Mon, 15 Sep 2026 09:00:00 +0300"
    outer.attach(MIMEText(body, "plain", charset))
    if attachment:
        name, payload, ctype = attachment
        maintype, _, subtype = ctype.partition("/")
        part = MIMEBase(maintype, subtype)
        part.set_payload(payload)
        from email import encoders

        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=name)
        outer.attach(part)
    return outer.as_bytes()
