"""MCP server for Greek School Network (sch.gr) email via IMAP + SMTP.

Provides read AND write access to a sch.gr mailbox: list, read, search,
download attachments, send, forward, move, create folders.

Run: python -m server
Credentials: ~/.sch-mail/credentials.json  {"email": "...", "password": "..."}
Optional local MCP instructions: ~/.sch-mail/instructions.md
"""

from __future__ import annotations

import email
import email.header
import email.utils
import html as html_module
import imaplib
import json
import os
import re
import smtplib
import ssl
import tempfile
import time
from datetime import UTC, datetime, timedelta
from email import encoders
from email.message import Message
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from mcp.server import MCPServer

IMAP_HOST = "mail.sch.gr"
IMAP_PORT = 993
SMTP_HOST = "mail.sch.gr"
SMTP_PORT = 465
CRED_PATH = Path.home() / ".sch-mail" / "credentials.json"
INSTRUCTIONS_PATH = Path.home() / ".sch-mail" / "instructions.md"
DEFAULT_DOWNLOAD_DIR = Path.home() / "Downloads"
ALLOWED_DIRS_ENV = "SCH_MAIL_ALLOWED_DIRS"
DRAFTS_FOLDER_CANDIDATES = ["Drafts", "INBOX.Drafts", "INBOX/Drafts"]
SENT_FOLDER_CANDIDATES = ["Sent", "INBOX.Sent", "INBOX/Sent", "Sent Items", "Sent Messages"]

# What every client is told, whoever runs the server. Guidance specific to one
# deployment (house rules for how a particular mailbox may be used) belongs to
# whoever runs it, not to the code, so it lives in INSTRUCTIONS_PATH, outside
# the repo, and _load_instructions appends it at startup.
BASE_INSTRUCTIONS = (
    "Greek School Network (sch.gr) email server. "
    "List, read, search, download attachments, move to folders, create folders. "
    "Message ids are IMAP UIDs: stable across calls, and what every msg_id expects."
)


def _load_instructions(path: Path | None = None) -> str:
    """The MCP instructions: BASE_INSTRUCTIONS plus the local file, when there is one.

    The file is optional and read once, at startup. Its text is appended after a
    single space, so a one-paragraph file reads as a continuation of the base
    text. A missing or blank file leaves the base text alone. A file that exists
    but cannot be read or decoded raises, so the server fails to start rather
    than quietly running without the guidance it was given.
    """
    path = INSTRUCTIONS_PATH if path is None else path
    if not path.is_file():
        return BASE_INSTRUCTIONS
    extra = path.read_text(encoding="utf-8").strip()
    return f"{BASE_INSTRUCTIONS} {extra}" if extra else BASE_INSTRUCTIONS


mcp = MCPServer("sch-mail", instructions=_load_instructions())


# ── Credentials ──────────────────────────────────────────────────────────


def _load_credentials(account: str | None = None) -> tuple[str, str]:
    if not CRED_PATH.exists():
        raise FileNotFoundError(
            f"Credentials not found at {CRED_PATH}. "
            f"Create it with: "
            f'{{"accounts": {{"name": {{"email": "user@sch.gr", "password": "..."}}}}, "default": "name"}}'
        )
    data = json.loads(CRED_PATH.read_text())
    if "accounts" in data:
        name = account or data.get("default", next(iter(data["accounts"])))
        if name not in data["accounts"]:
            available = ", ".join(data["accounts"].keys())
            raise ValueError(f"Account '{name}' not found. Available: {available}")
        acct = data["accounts"][name]
        return acct["email"], acct["password"]
    return data["email"], data["password"]


# ── File access ──────────────────────────────────────────────────────────
#
# Every path a tool receives is chosen by a model that is also reading the
# inbox, and anyone can send mail to the address. Without a limit, one hostile
# message could get the model to attach ~/.ssh keys or credentials.json to a
# message, or to save its own attachment into a folder that runs what lands
# there (~/Library/LaunchAgents, a venv's site-packages). So attachments are
# read, and downloads written, only inside an allowlist of folders, which by
# default is just ~/Downloads and the temp dirs: ~/Documents and cloud-synced
# folders hold private documents and projects that load files on their own.


class PathNotAllowedError(PermissionError):
    """A path this server may not read an attachment from or save a download into."""


def _allowed_dirs() -> list[Path]:
    """The allowed folders, with symlinks resolved.

    By default ~/Downloads, the system temp dir and /tmp. SCH_MAIL_ALLOWED_DIRS,
    absolute paths separated by os.pathsep, replaces the defaults outright; "~"
    is expanded and relative entries are ignored, because they would silently
    mean whatever directory the server was started in.
    """
    raw = os.environ.get(ALLOWED_DIRS_ENV, "")
    if raw.strip():
        dirs = [Path(entry.strip()).expanduser() for entry in raw.split(os.pathsep)]
    else:
        dirs = [Path.home() / "Downloads", Path(tempfile.gettempdir()), Path("/tmp")]
    return [d.resolve() for d in dirs if d.is_absolute()]


def _allowed_path(path: str | Path) -> Path:
    """The real path of `path`, symlinks followed, if it lies inside an allowed folder.

    Raises PathNotAllowedError otherwise. Resolving first is the point: "..",
    and a symlink inside an allowed folder that points out of it, both resolve
    to where they really lead, and that is what gets checked.

    Hidden names below an allowed folder are refused as well. Tools load files
    from hidden folders without asking: a project's .venv runs any .pth file in
    site-packages at interpreter start (this server's own .venv, when the repo
    is cloned under an allowed folder), and .git/hooks, .claude and .vscode hold
    files other programs act on. A hidden folder named itself in
    SCH_MAIL_ALLOWED_DIRS is allowed, because the check covers only the part of
    the path below the allowed folder.
    """
    real = Path(path).expanduser().resolve()
    roots = _allowed_dirs()
    below = [real.relative_to(root).parts for root in roots if real.is_relative_to(root)]
    if any(not any(part.startswith(".") for part in parts) for parts in below):
        return real
    if below:
        raise PathNotAllowedError(
            f"{path} is a hidden file or sits in a hidden folder, and sch-mail does "
            f"not read or save hidden paths inside its allowed folders. To use a "
            f"hidden folder anyway, name that folder itself in {ALLOWED_DIRS_ENV}."
        )
    allowed = ", ".join(str(root) for root in roots) or "none"
    raise PathNotAllowedError(
        f"{path} is outside the folders sch-mail may read attachments from or save "
        f"into (allowed: {allowed}). To change them, set {ALLOWED_DIRS_ENV} to a "
        f"list of absolute paths separated by {os.pathsep!r}."
    )


# ── IMAP helpers ─────────────────────────────────────────────────────────


def _connect(account: str | None = None) -> imaplib.IMAP4_SSL:
    user, pwd = _load_credentials(account)
    ctx = ssl.create_default_context()
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ctx)
    conn.login(user, pwd)
    return conn


def _smtp_connect(account: str | None = None) -> smtplib.SMTP_SSL:
    user, pwd = _load_credentials(account)
    ctx = ssl.create_default_context()
    smtp = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx)
    smtp.login(user, pwd)
    return smtp


_IMAP_CONTROL_CHARS = re.compile(r"[\x00\r\n]")


def _check_imap_args(**values: str | None) -> None:
    """Refuse NUL, CR and LF in values that end up on an IMAP command line.

    A CR LF inside an argument ends the command and starts another, so a folder
    name or message id carrying one could issue IMAP commands no tool exposes
    (re-select read-write, flag, expunge, delete a folder). Recent imaplib
    releases refuse these themselves, but not every Python that requires-python
    admits: 3.12.14's imaplib sends them through.
    """
    for name, value in values.items():
        if value and _IMAP_CONTROL_CHARS.search(value):
            raise ValueError(f"{name} must not contain NUL, CR or LF characters")


def _imap_quote(value: str) -> str:
    """`value` as an IMAP quoted string (RFC 3501), for SEARCH criteria.

    Backslash and double quote are escaped, so a quote inside a search term is
    searched for instead of closing the string and turning the rest of the term
    into criteria. NUL, CR and LF cannot appear in a quoted string at all.
    """
    _check_imap_args(query=value)
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _decode_header(raw: str | None) -> str:
    """Decode an RFC 2047 header, degrading rather than raising.

    Two defects fixed here, found 2026-09-17 by the first test suite this repo
    ever had.

    IT USED TO RAISE, and that took out far more than one header. `unknown-8bit`
    is a LEGAL RFC 2047 charset token with no Python codec, so it raises
    LookupError, and malformed base64 raises email.errors.HeaderParseError. This
    function is called unguarded inside the per-message loop of sch_list_mail
    (see the Subject/From decode below), so a SINGLE spam message carrying a
    broken encoded-word made the entire folder listing fail rather than that one
    row. It is on the forward path too. A mail client that cannot list a folder
    because someone sent junk to it is not usable, so every failure mode here
    degrades to the raw text.

    IT USED TO JOIN WITH A SPACE. decode_header already returns the surrounding
    whitespace as part of the adjacent plain-text run, so " ".join inserted a
    second one: "Re: =?utf-8?B?...?=" came back as "Re:  Καλη". Cosmetic in a
    listing, not cosmetic for search, because the client-side substring filter
    in sch_search_mail matches against this string and any Greek query spanning
    that boundary silently missed.
    """
    if not raw:
        return ""
    try:
        parts = email.header.decode_header(raw)
    except Exception:
        # Malformed encoded-word. The raw header is still the best answer
        # available, and is what the user would see in any other client.
        return raw
    decoded = []
    for data, charset in parts:
        if isinstance(data, bytes):
            try:
                decoded.append(data.decode(charset or "utf-8", errors="replace"))
            except LookupError:
                # A charset Python has no codec for, `unknown-8bit` being the
                # common legal one. Latin-1 maps every byte to a character, so
                # this cannot raise again and keeps the ASCII subset readable.
                decoded.append(data.decode("latin-1", errors="replace"))
        else:
            decoded.append(data)
    return "".join(decoded)


def _parse_date(msg: Message) -> str:
    raw = msg.get("Date", "")
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
        return parsed.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return raw


def _decode_payload(payload: bytes, charset: str | None) -> str:
    """Decode a body part, degrading the way _decode_header does for headers.

    A part can declare a charset Python has no codec for (`unknown-8bit` is the
    common legal one), or one that is not a text codec at all, and bytes.decode
    then raises instead of replacing: sch_get_mail and sch_forward_mail failed
    outright on such a message. Latin-1 maps every byte, so it cannot raise.
    """
    try:
        return payload.decode(charset or "utf-8", errors="replace")
    except (LookupError, ValueError):
        return payload.decode("latin-1", errors="replace")


def _get_text_body(msg: Message) -> str:
    """The message body, which is NOT simply the first text/plain part.

    Fixed 2026-09-17. This returned the first text/plain in walk order, and an
    attached .txt is a text/plain part. When such an attachment sorts ahead of
    the real body, the attachment's CONTENTS were returned as the message body,
    and sch_forward_mail quotes this to build the forwarded block. So forwarding
    a message with a .txt attachment sent the attachment's text to the recipient
    in place of what the sender actually wrote, with no error.

    Content-Disposition is the discriminator: an attachment declares itself.
    """
    if msg.is_multipart():
        for part in msg.walk():
            if "attachment" in (part.get("Content-Disposition") or "").lower():
                continue
            ct = part.get_content_type()
            if ct == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return _decode_payload(payload, part.get_content_charset())
            elif ct == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    html = _decode_payload(payload, part.get_content_charset())
                    return _strip_html(html)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            text = _decode_payload(payload, msg.get_content_charset())
            if msg.get_content_type() == "text/html":
                return _strip_html(text)
            return text
    return ""


# Fixed 2026-10-02. _strip_html used to remove tags with
# re.sub(r"<style[^>]*>.*?</style>", ...) and re.sub(r"<[^>]+>", ...), which
# rescan to the end of the body from every "<style" or "<" that is never
# closed. The time grew with the square of the body: 8,000 unclosed <style>
# tags took over a second, so one hostile message stalled every read of it.
# The two helpers below give exactly the old result in a single pass;
# tests/test_parsing.py checks them against the old regexes.
_BLOCK_TAGS = {
    tag: (re.compile(f"<{tag}", re.IGNORECASE), re.compile(f"</{tag}>", re.IGNORECASE))
    for tag in ("style", "script")
}


def _drop_blocks(text: str, tag: str) -> str:
    """Remove every <tag ...>...</tag> block, case-insensitively, closing at the first </tag>.

    Returns exactly what the old
    re.sub(r"<tag[^>]*>.*?</tag>", "", text, flags=DOTALL | IGNORECASE) did.
    Once an opening tag has no ">" after it, or no closing tag after that ">",
    no later opening tag can have one either, so the scan stops there instead of
    retrying from each of them.
    """
    opening, closing = _BLOCK_TAGS[tag]
    out, pos = [], 0
    while (start := opening.search(text, pos)) is not None:
        gt = text.find(">", start.end())
        if gt < 0:
            break
        end = closing.search(text, gt + 1)
        if end is None:
            break
        out.append(text[pos : start.start()])
        pos = end.end()
    out.append(text[pos:])
    return "".join(out)


def _drop_tags(text: str) -> str:
    """Remove every <...> tag, as re.sub(r"<[^>]+>", "", text) did.

    Every tag ends in ">", so nothing after the last ">" can be one. Cutting the
    regex off there is what makes it linear: before it, every "<" has a ">"
    after it, so each match attempt either matches or fails at once.
    """
    cut = text.rfind(">") + 1
    return re.sub(r"<[^>]+>", "", text[:cut]) + text[cut:]


def _strip_html(html: str) -> str:
    text = _drop_blocks(html, "style")
    text = _drop_blocks(text, "script")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = _drop_tags(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Unescape AFTER stripping tags, never before: doing it first would turn a
    # &lt;script&gt; in the source text into a real tag that the regex above
    # then removes. Added 2026-09-17, because older Greek webmail sends body
    # text as numeric entities and without this the result is unreadable.
    text = html_module.unescape(text)
    # &nbsp; unescapes to U+00A0, which is correct HTML and wrong for extracted
    # body text: it is invisible in the output yet does not match a space typed
    # into the client-side search in sch_search_mail. Normalise it.
    return text.replace("\xa0", " ").strip()


def _list_attachment_info(msg: Message) -> list[dict]:
    attachments = []
    for part in msg.walk():
        cd = part.get("Content-Disposition", "")
        if "attachment" in cd or (part.get_filename() and part.get_content_maintype() != "text"):
            fname = _decode_header(part.get_filename()) or "unnamed"
            size = len(part.get_payload(decode=True) or b"")
            attachments.append(
                {
                    "filename": fname,
                    "content_type": part.get_content_type(),
                    "size_bytes": size,
                }
            )
    return attachments


def _imap_date(dt: datetime) -> str:
    return dt.strftime("%d-%b-%Y")


# ── Message addressing ───────────────────────────────────────────────────
#
# Every message id this server hands out or accepts is an IMAP UID. Fixed
# 2026-09-23: the ids used to be SEQUENCE numbers, which renumber whenever any
# message leaves the folder, and every move ended in a bare EXPUNGE. So listing
# INBOX, letting webmail or a phone delete one message, then moving the message
# the listing called "3" moved the one after it. Two moves from one listing did
# the same with no other client involved, because the first move's expunge
# shifted the second id. Each tool call opens its own connection, so nothing
# pinned the numbering between calls. A UID never changes while the folder's
# UIDVALIDITY holds. tests/test_uid_addressing.py pins all of this.


def _q(mailbox: str) -> str:
    """Quote a mailbox name when it needs it (spaces or specials).

    For UID MOVE, whose arguments imaplib passes through raw, so a folder such as
    "INBOX.Archive 2026" would reach the server unquoted and be rejected. Also
    applied to UID COPY, which older imaplib does not quote either; newer imaplib
    leaves an already-quoted name unchanged. Same rule as the derived yahoo-access
    server.
    """
    # Idempotent: callers were told to pre-quote names with spaces before _q
    # existed, so an already-quoted name passes through unchanged instead of
    # being quoted a second time, which the server rejects.
    if len(mailbox) >= 2 and mailbox[0] == '"' and mailbox[-1] == '"' and mailbox[-2] != "\\":
        return mailbox
    if re.search(r'[\s"\\]', mailbox):
        escaped = mailbox.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return mailbox


def _is_uid(msg_id: str) -> bool:
    """One UID, never a set: "1:*" or "3,4" would move or delete several messages."""
    return msg_id.isascii() and msg_id.isdigit()


def _uid_exists(conn: imaplib.IMAP4_SSL, uid: str) -> bool:
    """Whether the selected folder holds this UID.

    Needed because UID COPY, UID MOVE and UID STORE silently ignore a UID that
    does not exist (RFC 3501), so a stale id would otherwise report "moved"
    having moved nothing.
    """
    status, data = conn.uid("SEARCH", None, f"UID {uid}")
    return status == "OK" and bool(data and data[0]) and uid.encode() in data[0].split()


def _capabilities(conn: imaplib.IMAP4_SSL) -> set[str]:
    """The server's CAPABILITY list as it stands after login, upper-cased.

    Not conn.capabilities: imaplib fills that once from the pre-login greeting and
    never refreshes it, and servers commonly advertise MOVE and UIDPLUS only after
    authentication, so it under-reports exactly the two extensions asked about.
    """
    status, data = conn.capability()
    if status != "OK" or not data or not data[0]:
        return set()
    return set(data[0].decode("ascii", errors="replace").upper().split())


def _expunge_uid(conn: imaplib.IMAP4_SSL, uid: str, caps: set[str]) -> None:
    """Expunge one message already flagged \\Deleted, by UID where possible.

    UID EXPUNGE (RFC 4315, UIDPLUS) removes this UID and nothing else. Without
    UIDPLUS, IMAP4rev1 has no command that removes a single message, so the last
    resort is a bare EXPUNGE, which also purges anything another client (webmail,
    a phone) flagged \\Deleted and has not purged yet. That is kept on purpose:
    the alternative, leaving our flag in place, turns a move into a copy that some
    later client silently finishes, and addressing by UID already guarantees the
    only message this call flagged is the one asked for.
    """
    if "UIDPLUS" in caps:
        conn.uid("EXPUNGE", uid)
    else:
        conn.expunge()


def _move_uid(conn: imaplib.IMAP4_SSL, uid: str, dest_folder: str) -> str | None:
    """Move one message, by UID, out of the selected folder. None, or an error.

    UID MOVE (RFC 6851) when the server advertises MOVE, else UID COPY + UID
    STORE \\Deleted + _expunge_uid.
    """
    caps = _capabilities(conn)
    if "MOVE" in caps:
        move_status, move_data = conn.uid("MOVE", uid, _q(dest_folder))
        if move_status != "OK":
            detail = move_data[0].decode() if move_data and move_data[0] else "unknown"
            return f"MOVE to {dest_folder} failed: {detail}"
        return None

    copy_status, copy_data = conn.uid("COPY", uid, _q(dest_folder))
    if copy_status != "OK":
        detail = copy_data[0].decode() if copy_data and copy_data[0] else "unknown"
        return f"COPY to {dest_folder} failed: {detail}"

    store_status, _ = conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
    if store_status != "OK":
        return "Copied to destination but failed to flag source for deletion"

    _expunge_uid(conn, uid, caps)
    return None


# ── Send helpers ─────────────────────────────────────────────────────────


def _split_addresses(s: str | None) -> list[str]:
    """Split an address list without breaking on commas INSIDE a display name.

    Fixed 2026-09-17. This used to be `s.split(",")`, so
    `"Πλέσσας, Δημήτρης" <a@sch.gr>` became TWO envelope recipients,
    `'"Πλέσσας'` and `'Δημήτρης" <a@sch.gr>'`. Greek address books produce
    «Επώνυμο, Όνομα» as a matter of course. The visible To: header stayed
    correct, because _build_message assigns the raw string rather than the
    split, so the misdirection was invisible in the sent copy: exactly the
    wrong-thing-to-the-wrong-person shape this module must not have.

    TWO TRAPS, which is why this is not a one-line change. Both were hit for
    real while making this fix.

    1. `getaddresses` defaults to STRICT since Python 3.13, and in strict mode a
       list it considers malformed yields nothing usable. A trailing comma,
       `"a@example.gr,"`, is enough: strict returns no address and the function
       returned [], which would SILENTLY DROP EVERY RECIPIENT. That is a far
       worse bug than the one being fixed, so strict=False is deliberate. The
       empty slots it then produces are filtered on the ADDRESS being non-empty,
       not on truthiness of the pair, because a malformed entry can parse to an
       empty address while the display name survives and handing that to SMTP is
       worse than omitting it.
    2. `email.utils.formataddr` RFC-2047-ENCODES a non-ASCII display name, so
       `Ονομα <a@example.gr>` came back as `=?utf-8?b?...?= <a@example.gr>`.
       Correct for a header being serialised, wrong here: this value is read
       back by callers and shown to the user, and on a Greek account almost
       every display name is non-ASCII. Reassemble plainly instead.
    """
    if not s:
        return []
    try:
        pairs = email.utils.getaddresses([s], strict=False)
    except TypeError:
        # Python older than the release that added the keyword. Its getaddresses
        # is non-strict anyway, which is the behaviour being asked for.
        pairs = email.utils.getaddresses([s])
    out = []
    for name, addr in pairs:
        if not addr:
            continue
        if not name:
            out.append(addr)
            continue
        # Re-quote a name that needs it. Without this the comma in
        # «Επώνυμο, Όνομα» comes back unquoted and the value re-parses into two
        # recipients on the next pass, reintroducing the exact bug. Done by hand
        # rather than with formataddr, which would also RFC-2047-encode it.
        if any(c in name for c in ',;:<>@"\\') or name != name.strip():
            name = '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
        out.append(f"{name} <{addr}>")
    return out


def _collect_recipients(to: str, cc: str | None = None, bcc: str | None = None) -> list[str]:
    rcpts: list[str] = []
    for field in (to, cc, bcc):
        rcpts.extend(_split_addresses(field))
    # Dedupe preserving order
    seen = set()
    unique = []
    for addr in rcpts:
        if addr.lower() not in seen:
            seen.add(addr.lower())
            unique.append(addr)
    return unique


def _build_message(
    from_addr: str,
    to: str,
    subject: str,
    body: str,
    html: bool = False,
    cc: str | None = None,
    bcc: str | None = None,
    attachments: list[str] | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    extra_attachments: list[tuple[str, bytes, str]] | None = None,
    bcc_header: bool = False,
) -> MIMEMultipart:
    """Build a MIME message ready for SMTP send or IMAP append.

    extra_attachments: list of (filename, payload_bytes, content_type) for
    re-attaching files from existing messages (forward use case).

    bcc_header: write a Bcc header into the message. FALSE for anything going
    to SMTP, TRUE only for a draft. This asymmetry is the whole point, so it is
    a parameter rather than a constant:

      - On a SENT message a Bcc header is a disclosure bug. Every recipient
        would see who was blind-copied, which is the opposite of what bcc means.
        The blind recipients belong in the SMTP envelope only, which is what
        _collect_recipients builds.
      - On a DRAFT the header is the only place the information can live. A
        draft has no envelope: it is a message sat in a folder, and sch.gr
        webmail builds the envelope from the headers when the user hits send.

    Fixed 2026-09-17. `bcc` was accepted here and then never used at all, and
    draft-first is the DEFAULT path, so the normal flow was: ask for a blind
    copy, review the draft in webmail, send it, blind recipient never receives
    it. Nothing errored. The returned dicts made it harder to notice rather
    than easier, since `sent` echoed bcc back and `draft_saved` did not.
    """
    msg = MIMEMultipart("mixed")
    msg["From"] = from_addr
    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc
    if bcc and bcc_header:
        msg["Bcc"] = bcc
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain="sch.gr")
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references

    body_part = MIMEMultipart("alternative")
    if html:
        body_part.attach(MIMEText(body, "html", "utf-8"))
    else:
        body_part.attach(MIMEText(body, "plain", "utf-8"))
    msg.attach(body_part)

    if attachments:
        for path_str in attachments:
            # Raises for a path outside the allowed folders, and then for one
            # that is missing or not a regular file. Never skip either: the
            # message would then go out without an attachment the caller asked
            # for, and nothing would say so. The allowlist is checked first, so
            # the error never reveals whether a file outside it exists.
            path = _allowed_path(path_str)
            if not path.exists() or not path.is_file():
                raise FileNotFoundError(
                    f"Attachment {path_str} does not exist or is not a regular file."
                )
            # The name the caller gave, not the symlink target's.
            name = Path(path_str).expanduser().name
            with path.open("rb") as f:
                payload = f.read()
            maintype, _, subtype = _guess_mime(name).partition("/")
            att = MIMEBase(maintype or "application", subtype or "octet-stream")
            att.set_payload(payload)
            encoders.encode_base64(att)
            att.add_header("Content-Disposition", "attachment", filename=name)
            msg.attach(att)

    if extra_attachments:
        for fname, payload, ctype in extra_attachments:
            maintype, _, subtype = ctype.partition("/")
            att = MIMEBase(maintype or "application", subtype or "octet-stream")
            att.set_payload(payload)
            encoders.encode_base64(att)
            att.add_header("Content-Disposition", "attachment", filename=fname)
            msg.attach(att)

    return msg


def _guess_mime(filename: str) -> str:
    import mimetypes

    ctype, _ = mimetypes.guess_type(filename)
    return ctype or "application/octet-stream"


def _find_special_folder(conn: imaplib.IMAP4_SSL, candidates: list[str]) -> str:
    """Pick the first existing folder from candidates, else fallback to first."""
    status, data = conn.list()
    if status != "OK":
        return candidates[0]
    existing: set[str] = set()
    for item in data:
        if isinstance(item, bytes):
            match = re.search(rb'"([^"]*)"$|(\S+)$', item)
            if match:
                name = (match.group(1) or match.group(2)).decode("utf-8", errors="replace")
                existing.add(name)
    for cand in candidates:
        if cand in existing:
            return cand
    return candidates[0]


def _save_to_folder(
    folder: str, msg: MIMEMultipart, flags: str = "\\Seen", account: str | None = None
) -> dict[str, Any]:
    conn = _connect(account)
    try:
        target = folder
        if folder in ("__DRAFTS__",):
            target = _find_special_folder(conn, DRAFTS_FOLDER_CANDIDATES)
        elif folder in ("__SENT__",):
            target = _find_special_folder(conn, SENT_FOLDER_CANDIDATES)
        status, data = conn.append(
            target,
            flags,
            imaplib.Time2Internaldate(time.time()),
            msg.as_bytes(),
        )
        if status != "OK":
            return {"error": f"APPEND to {target} failed: {data}"}
        return {"status": "ok", "folder": target}
    finally:
        conn.logout()


# ── MCP Tools — read ─────────────────────────────────────────────────────


@mcp.tool()
def sch_list_folders(account: str | None = None) -> list[str]:
    """List all mailbox folders.

    Returns folder names available in the sch.gr mailbox.
    Pass account (a key in credentials.json, e.g. "work") to pick a mailbox, or
    omit it for the account named by "default" in that file.
    """
    conn = _connect(account)
    try:
        status, data = conn.list()
        folders = []
        for item in data:
            if isinstance(item, bytes):
                match = re.search(rb'"([^"]*)"$|(\S+)$', item)
                if match:
                    name = (match.group(1) or match.group(2)).decode("utf-8", errors="replace")
                    folders.append(name)
        return sorted(folders)
    finally:
        conn.logout()


@mcp.tool()
def sch_list_mail(
    folder: str = "INBOX",
    top: int = 20,
    since: str | None = None,
    account: str | None = None,
) -> list[dict[str, Any]]:
    """List recent messages with subject, sender, date, and attachment indicators.

    Each result's "id" is the message's IMAP UID: stable across calls, unlike a
    sequence number, and the value every msg_id parameter expects.

    Args:
        folder: Mailbox folder (default: INBOX)
        top: Max messages to return (default: 20, max: 100)
        since: Only messages after this date (YYYY-MM-DD). Default: last 30 days.
        account: Account key in credentials.json (e.g. "personal" or "work").
            Default: the account named by "default" in that file.
    """
    _check_imap_args(folder=folder)
    top = min(top, 100)
    conn = _connect(account)
    try:
        conn.select(folder, readonly=True)
        if since:
            since_dt = datetime.strptime(since, "%Y-%m-%d")
        else:
            since_dt = datetime.now(UTC) - timedelta(days=30)
        criteria = f"(SINCE {_imap_date(since_dt)})"
        status, msg_ids = conn.uid("SEARCH", None, criteria)
        if status != "OK" or not msg_ids[0]:
            return []
        ids = msg_ids[0].split()
        ids = ids[-top:]
        ids.reverse()

        results = []
        for uid in ids:
            status, data = conn.uid("FETCH", uid, "(RFC822.HEADER FLAGS)")
            if status != "OK" or not data or not data[0]:
                continue
            raw = data[0][1] if isinstance(data[0], tuple) else data[0]
            msg = email.message_from_bytes(raw)

            flags_raw = b""
            for part in data:
                if isinstance(part, bytes) and b"FLAGS" in part:
                    flags_raw = part
                    break
                elif isinstance(part, tuple) and len(part) > 0:
                    hdr = part[0] if isinstance(part[0], bytes) else b""
                    if b"FLAGS" in hdr:
                        flags_raw = hdr

            seen = b"\\Seen" in flags_raw

            results.append(
                {
                    "id": uid.decode(),
                    "date": _parse_date(msg),
                    "from": _decode_header(msg.get("From")),
                    "to": _decode_header(msg.get("To")),
                    "subject": _decode_header(msg.get("Subject")),
                    "read": seen,
                    "has_attachments": bool(
                        any(
                            "attachment" in (p.get("Content-Disposition") or "")
                            for p in msg.walk()
                            if msg.is_multipart()
                        )
                        or msg.get("Content-Disposition", "")
                        and "attachment" in msg.get("Content-Disposition", "")
                    ),
                }
            )
        return results
    finally:
        conn.logout()


@mcp.tool()
def sch_get_mail(
    msg_id: str,
    folder: str = "INBOX",
    body: str = "text",
    max_body_chars: int = 5000,
    account: str | None = None,
) -> dict[str, Any]:
    """Read a specific message by UID (the "id" from sch_list_mail or sch_search_mail).

    Args:
        msg_id: Message UID from sch_list_mail / sch_search_mail
        folder: Mailbox folder (default: INBOX)
        body: Body format: "text" (plain text, default), "html", or "none"
        max_body_chars: Truncate body to this many chars (default: 5000)
        account: Account key in credentials.json (e.g. "personal" or "work").
            Default: the account named by "default" in that file.
    """
    _check_imap_args(msg_id=msg_id, folder=folder)
    conn = _connect(account)
    try:
        conn.select(folder, readonly=True)
        status, data = conn.uid("FETCH", msg_id, "(RFC822)")
        if status != "OK" or not data or not data[0]:
            return {"error": f"Message {msg_id} not found in {folder}"}
        raw = data[0][1]
        msg = email.message_from_bytes(raw)

        result: dict[str, Any] = {
            "id": msg_id,
            "date": _parse_date(msg),
            "from": _decode_header(msg.get("From")),
            "to": _decode_header(msg.get("To")),
            "cc": _decode_header(msg.get("Cc")),
            "subject": _decode_header(msg.get("Subject")),
            "attachments": _list_attachment_info(msg),
        }

        if body == "text":
            text = _get_text_body(msg)
            result["body"] = text[:max_body_chars]
            if len(text) > max_body_chars:
                result["body_truncated"] = True
        elif body == "html":
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        html = _decode_payload(payload, part.get_content_charset())
                        result["body"] = html[:max_body_chars]
                        if len(html) > max_body_chars:
                            result["body_truncated"] = True
                        break

        return result
    finally:
        conn.logout()


@mcp.tool()
def sch_search_mail(
    query: str,
    folder: str = "INBOX",
    field: str = "subject",
    since: str | None = None,
    top: int = 20,
    account: str | None = None,
) -> list[dict[str, Any]]:
    """Search messages by keyword in subject, sender, or body.

    Result ids are IMAP UIDs, the same ids sch_list_mail returns.

    Args:
        query: Search text (case-insensitive)
        folder: Mailbox folder (default: INBOX)
        field: Where to search: "subject", "from", "body", or "all" (default: subject)
        since: Only search after this date (YYYY-MM-DD)
        top: Max results (default: 20)
        account: Account key in credentials.json (e.g. "personal" or "work").
            Default: the account named by "default" in that file.
    """
    _check_imap_args(query=query, folder=folder)
    top = min(top, 100)
    has_unicode = any(ord(c) > 127 for c in query)
    conn = _connect(account)
    try:
        conn.select(folder, readonly=True)

        # IMAP SEARCH can't handle non-ASCII in criteria reliably,
        # so for Greek text we fetch headers and filter client-side
        if has_unicode:
            since_criteria = ""
            if since:
                since_dt = datetime.strptime(since, "%Y-%m-%d")
                since_criteria = f"(SINCE {_imap_date(since_dt)})"
            else:
                since_dt = datetime.now(UTC) - timedelta(days=180)
                since_criteria = f"(SINCE {_imap_date(since_dt)})"
            status, msg_ids = conn.uid("SEARCH", None, since_criteria)
            if status != "OK" or not msg_ids[0]:
                return []
            all_ids = msg_ids[0].split()
            q_lower = query.lower()
            results = []
            for uid in reversed(all_ids):
                if len(results) >= top:
                    break
                status, data = conn.uid("FETCH", uid, "(RFC822.HEADER)")
                if status != "OK" or not data or not data[0]:
                    continue
                raw = data[0][1] if isinstance(data[0], tuple) else data[0]
                msg = email.message_from_bytes(raw)
                subj = _decode_header(msg.get("Subject")).lower()
                frm = _decode_header(msg.get("From")).lower()
                match = False
                if field in ("subject", "all") and q_lower in subj:
                    match = True
                if field in ("from", "all") and q_lower in frm:
                    match = True
                if match:
                    results.append(
                        {
                            "id": uid.decode(),
                            "date": _parse_date(msg),
                            "from": _decode_header(msg.get("From")),
                            "subject": _decode_header(msg.get("Subject")),
                        }
                    )
            return results

        criteria_parts = []
        if since:
            since_dt = datetime.strptime(since, "%Y-%m-%d")
            criteria_parts.append(f"SINCE {_imap_date(since_dt)}")
        quoted = _imap_quote(query)
        if field == "subject":
            criteria_parts.append(f"SUBJECT {quoted}")
        elif field == "from":
            criteria_parts.append(f"FROM {quoted}")
        elif field == "body":
            criteria_parts.append(f"BODY {quoted}")
        elif field == "all":
            criteria_parts.append(f"OR OR SUBJECT {quoted} FROM {quoted} BODY {quoted}")

        criteria = "(" + " ".join(criteria_parts) + ")" if criteria_parts else "ALL"
        status, msg_ids = conn.uid("SEARCH", None, criteria)
        if status != "OK" or not msg_ids[0]:
            return []

        ids = msg_ids[0].split()[-top:]
        ids.reverse()

        results = []
        for uid in ids:
            status, data = conn.uid("FETCH", uid, "(RFC822.HEADER)")
            if status != "OK" or not data or not data[0]:
                continue
            raw = data[0][1] if isinstance(data[0], tuple) else data[0]
            msg = email.message_from_bytes(raw)
            results.append(
                {
                    "id": uid.decode(),
                    "date": _parse_date(msg),
                    "from": _decode_header(msg.get("From")),
                    "subject": _decode_header(msg.get("Subject")),
                }
            )
        return results
    finally:
        conn.logout()


@mcp.tool()
def sch_download_attachments(
    msg_id: str,
    folder: str = "INBOX",
    out_dir: str | None = None,
    filename_filter: str | None = None,
    account: str | None = None,
) -> list[dict[str, str]]:
    """Download attachments from a message to disk.

    Args:
        msg_id: Message UID from sch_list_mail / sch_search_mail
        folder: Mailbox folder (default: INBOX)
        out_dir: Directory to save files (default: ~/Downloads). Must lie inside
            the allowed folders (by default ~/Downloads and the temp dirs;
            SCH_MAIL_ALLOWED_DIRS replaces them).
        filename_filter: Only download files matching this substring (case-insensitive)
        account: Account key in credentials.json (e.g. "personal" or "work").
            Default: the account named by "default" in that file.
    """
    _check_imap_args(msg_id=msg_id, folder=folder)
    # Checked before mkdir, which would otherwise create the refused folder.
    try:
        dest = _allowed_path(out_dir or DEFAULT_DOWNLOAD_DIR)
    except PathNotAllowedError as exc:
        return [{"error": str(exc)}]
    dest.mkdir(parents=True, exist_ok=True)

    conn = _connect(account)
    try:
        conn.select(folder, readonly=True)
        status, data = conn.uid("FETCH", msg_id, "(RFC822)")
        if status != "OK" or not data or not data[0]:
            return [{"error": f"Message {msg_id} not found"}]
        msg = email.message_from_bytes(data[0][1])

        saved = []
        for part in msg.walk():
            fname = _decode_header(part.get_filename())
            if not fname:
                continue
            cd = part.get("Content-Disposition", "")
            if "attachment" not in cd and part.get_content_maintype() == "text":
                continue
            if filename_filter and filename_filter.lower() not in fname.lower():
                continue

            payload = part.get_payload(decode=True)
            if not payload:
                continue

            # The name is the sender's. Path separators would leave dest, control
            # and bidi-override characters disguise the real name and extension,
            # and a leading dot hides the file, so all of them are neutralised.
            safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]', "_", fname)
            if safe_name.startswith("."):
                safe_name = "_" + safe_name
            target = dest / safe_name
            counter = 1
            # is_symlink too: a dangling link reports exists() False, and
            # writing to it would follow it out of the allowed folder.
            while target.exists() or target.is_symlink():
                stem = target.stem
                target = dest / f"{stem}_{counter}{target.suffix}"
                counter += 1
            target.write_bytes(payload)
            saved.append(
                {
                    "filename": fname,
                    "saved_to": str(target),
                    "size_bytes": str(len(payload)),
                }
            )
        if not saved:
            return [{"message": "No attachments found (or none matched filter)"}]
        return saved
    finally:
        conn.logout()


@mcp.tool()
def sch_mail_stats(folder: str = "INBOX", account: str | None = None) -> dict[str, Any]:
    """Quick mailbox statistics: total messages, recent/unseen counts, date range.

    Args:
        folder: Mailbox folder (default: INBOX)
        account: Account key in credentials.json (e.g. "personal" or "work").
            Default: the account named by "default" in that file.
    """
    _check_imap_args(folder=folder)
    conn = _connect(account)
    try:
        status, data = conn.select(folder, readonly=True)
        total = int(data[0]) if status == "OK" else 0

        _, unseen_data = conn.search(None, "UNSEEN")
        unseen = len(unseen_data[0].split()) if unseen_data[0] else 0

        week_ago = datetime.now(UTC) - timedelta(days=7)
        _, recent_data = conn.search(None, f"(SINCE {_imap_date(week_ago)})")
        recent_7d = len(recent_data[0].split()) if recent_data[0] else 0

        today = datetime.now(UTC)
        _, today_data = conn.search(None, f"(SINCE {_imap_date(today)})")
        today_count = len(today_data[0].split()) if today_data[0] else 0

        return {
            "folder": folder,
            "total_messages": total,
            "unread": unseen,
            "received_today": today_count,
            "received_last_7_days": recent_7d,
        }
    finally:
        conn.logout()


# ── MCP Tools — write ────────────────────────────────────────────────────


@mcp.tool()
def sch_send_mail(
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
    html: bool = False,
    attachments: list[str] | None = None,
    send_now: bool = False,
    account: str | None = None,
) -> dict[str, Any]:
    """Send a new email via SMTP, or save as draft (default).

    Args:
        to: Recipient(s), comma-separated (e.g. "a@x.gr,b@y.gr")
        subject: Subject line
        body: Email body (plain text by default; set html=True for HTML)
        cc: CC recipient(s), comma-separated
        bcc: BCC recipient(s), comma-separated
        html: True if body is HTML; False (default) for plain text
        attachments: List of absolute file paths to attach. Each must be an existing
            file inside the allowed folders (by default ~/Downloads and the temp
            dirs; SCH_MAIL_ALLOWED_DIRS replaces them). One that is missing or
            outside them fails the whole call, so nothing is saved or sent without it.
        send_now: True to dispatch via SMTP immediately. Default False = save to Drafts folder.
        account: Account key in credentials.json (e.g. "personal" or "work").
            Default: the account named by "default" in that file.

    Returns dict with status ("draft_saved" or "sent"), folder/recipients, and message id.
    """
    user, _ = _load_credentials(account)
    try:
        msg = _build_message(
            from_addr=user,
            to=to,
            subject=subject,
            body=body,
            html=html,
            cc=cc,
            bcc=bcc,
            attachments=attachments,
            # Header on the draft, envelope-only on the send. See _build_message.
            bcc_header=not send_now,
        )
    except (PathNotAllowedError, FileNotFoundError) as exc:
        return {"error": str(exc)}

    if not send_now:
        result = _save_to_folder("__DRAFTS__", msg, flags="(\\Draft \\Seen)", account=account)
        if "error" in result:
            return result
        return {
            "status": "draft_saved",
            "folder": result["folder"],
            "to": to,
            "cc": cc,
            "bcc": bcc,
            "subject": subject,
            "attachment_count": len(attachments or []),
            "message_id": msg["Message-ID"],
            "note": "Draft saved. Open sch.gr webmail to review and send, or rerun with send_now=True.",
        }

    recipients = _collect_recipients(to, cc, bcc)
    if not recipients:
        return {"error": "No recipients provided"}

    smtp = _smtp_connect(account)
    try:
        smtp.sendmail(user, recipients, msg.as_string())
    finally:
        smtp.quit()

    sent_result = _save_to_folder("__SENT__", msg, flags="(\\Seen)", account=account)
    return {
        "status": "sent",
        "to": to,
        "cc": cc,
        "bcc": bcc,
        "subject": subject,
        "attachment_count": len(attachments or []),
        "message_id": msg["Message-ID"],
        "sent_folder_archive": sent_result.get("folder") if "error" not in sent_result else None,
    }


@mcp.tool()
def sch_forward_mail(
    msg_id: str,
    to: str,
    cc: str | None = None,
    bcc: str | None = None,
    additional_text: str = "",
    folder: str = "INBOX",
    send_now: bool = False,
    account: str | None = None,
) -> dict[str, Any]:
    """Forward an existing message to new recipient(s), preserving attachments.

    Args:
        msg_id: Source message UID (from sch_list_mail / sch_search_mail)
        to: Recipient(s), comma-separated
        cc: CC recipient(s)
        bcc: BCC recipient(s)
        additional_text: Optional text prepended above the forwarded content (plain text)
        folder: Source folder (default: INBOX)
        send_now: True to dispatch immediately. Default False = save to Drafts.
        account: Account key in credentials.json (e.g. "personal" or "work").
            Default: the account named by "default" in that file.

    Returns status, subject, attachment count, and message id.
    """
    _check_imap_args(msg_id=msg_id, folder=folder)
    user, _ = _load_credentials(account)

    conn = _connect(account)
    try:
        conn.select(folder, readonly=True)
        status, data = conn.uid("FETCH", msg_id, "(RFC822)")
        if status != "OK" or not data or not data[0]:
            return {"error": f"Message {msg_id} not found in {folder}"}
        original = email.message_from_bytes(data[0][1])
    finally:
        conn.logout()

    orig_subject = _decode_header(original.get("Subject", ""))
    fwd_subject = (
        orig_subject if orig_subject.lower().startswith("fwd:") else f"Fwd: {orig_subject}"
    )
    orig_from = _decode_header(original.get("From", ""))
    orig_date = original.get("Date", "")
    orig_to = _decode_header(original.get("To", ""))
    orig_cc = _decode_header(original.get("Cc", ""))
    orig_body = _get_text_body(original)

    forward_block = (
        "\n\n---------- Forwarded message ---------\n"
        f"From: {orig_from}\n"
        f"Date: {orig_date}\n"
        f"Subject: {orig_subject}\n"
        f"To: {orig_to}\n"
    )
    if orig_cc:
        forward_block += f"Cc: {orig_cc}\n"
    forward_block += "\n" + orig_body

    full_body = (additional_text + forward_block) if additional_text else forward_block

    extra_attachments: list[tuple[str, bytes, str]] = []
    for part in original.walk():
        fname = _decode_header(part.get_filename())
        if not fname:
            continue
        cd = part.get("Content-Disposition", "")
        if "attachment" not in cd and part.get_content_maintype() == "text":
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        extra_attachments.append((fname, payload, part.get_content_type()))

    msg = _build_message(
        from_addr=user,
        to=to,
        subject=fwd_subject,
        body=full_body,
        html=False,
        cc=cc,
        bcc=bcc,
        extra_attachments=extra_attachments,
        # Header on the draft, envelope-only on the send. See _build_message.
        bcc_header=not send_now,
    )

    if not send_now:
        result = _save_to_folder("__DRAFTS__", msg, flags="(\\Draft \\Seen)", account=account)
        if "error" in result:
            return result
        return {
            "status": "draft_saved",
            "folder": result["folder"],
            "to": to,
            "cc": cc,
            "bcc": bcc,
            "subject": fwd_subject,
            "forwarded_attachments": len(extra_attachments),
            "message_id": msg["Message-ID"],
            "note": "Draft saved. Open sch.gr webmail to review and send, or rerun with send_now=True.",
        }

    recipients = _collect_recipients(to, cc, bcc)
    if not recipients:
        return {"error": "No recipients provided"}

    smtp = _smtp_connect(account)
    try:
        smtp.sendmail(user, recipients, msg.as_string())
    finally:
        smtp.quit()

    sent_result = _save_to_folder("__SENT__", msg, flags="(\\Seen)", account=account)
    return {
        "status": "sent",
        "to": to,
        "cc": cc,
        "bcc": bcc,
        "subject": fwd_subject,
        "forwarded_attachments": len(extra_attachments),
        "message_id": msg["Message-ID"],
        "sent_folder_archive": sent_result.get("folder") if "error" not in sent_result else None,
    }


@mcp.tool()
def sch_move_mail(
    msg_id: str,
    dest_folder: str,
    source_folder: str = "INBOX",
    account: str | None = None,
) -> dict[str, Any]:
    """Move one message, addressed by UID, from source_folder to dest_folder.

    Uses UID MOVE when the server supports it, else UID COPY + \\Deleted + UID
    EXPUNGE, and a bare EXPUNGE only on a server with neither MOVE nor UIDPLUS.
    Destination folder must already exist (use sch_create_folder first if needed).

    Args:
        msg_id: Source message UID from sch_list_mail / sch_search_mail (one UID;
            ranges and lists are refused)
        dest_folder: Target folder name
        source_folder: Source folder (default: INBOX)
        account: Account key in credentials.json (e.g. "personal" or "work").
            Default: the account named by "default" in that file.
    """
    if not _is_uid(msg_id):
        return {"error": f"msg_id must be one message UID from sch_list_mail, got {msg_id!r}"}
    _check_imap_args(dest_folder=dest_folder, source_folder=source_folder)

    conn = _connect(account)
    try:
        status, _ = conn.select(source_folder, readonly=False)
        if status != "OK":
            return {"error": f"Cannot select source folder {source_folder}"}

        if not _uid_exists(conn, msg_id):
            return {"error": f"Message {msg_id} not found in {source_folder}"}

        error = _move_uid(conn, msg_id, dest_folder)
        if error:
            return {"error": error}

        return {
            "status": "moved",
            "msg_id": msg_id,
            "from": source_folder,
            "to": dest_folder,
        }
    finally:
        conn.logout()


@mcp.tool()
def sch_create_folder(
    name: str, subscribe: bool = True, account: str | None = None
) -> dict[str, Any]:
    """Create a new mailbox folder.

    Args:
        name: Folder name. For nested folders use the server's separator
              (commonly "/" or ".", e.g. "INBOX/Archive2026" or "Archive.2026").
        subscribe: Subscribe to the folder so it appears in mail clients (default: True).
        account: Account key in credentials.json (e.g. "personal" or "work").
            Default: the account named by "default" in that file.
    """
    _check_imap_args(name=name)
    conn = _connect(account)
    try:
        status, data = conn.create(name)
        if status != "OK":
            detail = data[0].decode() if data and data[0] else "unknown"
            if "exists" in detail.lower() or "already" in detail.lower():
                return {"status": "already_exists", "folder": name}
            return {"error": f"CREATE failed: {detail}"}
        if subscribe:
            conn.subscribe(name)
        return {"status": "created", "folder": name, "subscribed": subscribe}
    finally:
        conn.logout()


if __name__ == "__main__":
    mcp.run()
