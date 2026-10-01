"""_decode_header, _get_text_body and _strip_html, against real-world messy input.

These three run on data the owner does not control: whatever arrives in the
inbox. Every read tool calls _decode_header inside a per-message loop with no
try/except, so a single hostile or merely old message decides whether the whole
mailbox is readable.
"""

from __future__ import annotations

import email
import html as html_module
import random
import re
import time
from email.header import Header

import pytest

import server


def _msg(raw: str):
    """Parse from BYTES, as imaplib FETCH delivers them.

    message_from_string on a payload with no Content-Transfer-Encoding round
    trips it through raw-unicode-escape, which would mangle Greek and make these
    tests assert against something no real mailbox ever produces.
    """
    return email.message_from_bytes(raw.encode("utf-8"))


# ── _decode_header: the happy paths that must keep working ──────────────────


def test_decode_header_handles_none_and_empty():
    # BREAKS IN PRODUCTION: a message with no Subject header is common (many
    # automated school notifications omit it). msg.get("Subject") returns None,
    # and an AttributeError here would kill the whole listing.
    assert server._decode_header(None) == ""
    assert server._decode_header("") == ""


def test_decode_header_passes_plain_ascii_through():
    assert server._decode_header("Weekly timetable") == "Weekly timetable"


@pytest.mark.parametrize("charset", ["utf-8", "iso-8859-7", "windows-1253"])
def test_decode_header_decodes_greek_in_every_charset_the_school_network_uses(charset):
    # BREAKS IN PRODUCTION: sch.gr correspondents run a wide range of clients and
    # legacy Greek mailers still emit iso-8859-7 and windows-1253. Failing to
    # decode these leaves the subject as raw =?...?= gibberish in every listing.
    raw = str(Header("Καλημέρα", charset))
    assert server._decode_header(raw) == "Καλημέρα"


def test_decode_header_joins_adjacent_encoded_words_without_inserting_a_space():
    # BREAKS IN PRODUCTION: long Greek subjects are split across several
    # encoded-words by the sending client. A spurious space inside a word both
    # looks wrong and breaks the client-side Greek search in sch_search_mail,
    # which does a plain substring match on this decoded string.
    folded = "=?utf-8?B?zprOsc67zrc=?=\r\n =?utf-8?B?zrzOrc+BzrE=?="
    assert server._decode_header(folded) == "Καλημέρα"


def test_decode_header_survives_an_unterminated_encoded_word():
    # BREAKS IN PRODUCTION: truncated headers arrive from broken senders. This
    # must degrade to the raw text, not raise.
    assert server._decode_header("=?utf-8?Q?broken") == "=?utf-8?Q?broken"


# ── _get_text_body ──────────────────────────────────────────────────────────


def test_get_text_body_reads_a_simple_non_multipart_message():
    msg = _msg("Content-Type: text/plain; charset=utf-8\r\n\r\nΚαλημέρα")
    assert server._get_text_body(msg) == "Καλημέρα"


def test_get_text_body_decodes_a_legacy_greek_charset():
    # BREAKS IN PRODUCTION: assuming utf-8 for an iso-8859-7 body renders every
    # Greek character as a replacement glyph. The charset must come from the part.
    raw = b"Content-Type: text/plain; charset=iso-8859-7\r\n\r\n\xca\xe1\xeb\xe7\xec\xdd\xf1\xe1"
    assert server._get_text_body(email.message_from_bytes(raw)) == "Καλημέρα"


def test_get_text_body_prefers_the_plain_part_of_a_multipart_alternative():
    # BREAKS IN PRODUCTION: returning the HTML alternative when a clean plain
    # part exists gives the reader tag-stripped output instead of what the sender
    # actually wrote.
    raw = (
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/alternative; boundary="B"\r\n\r\n'
        "--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nΚαθαρό κείμενο\r\n"
        "--B\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>HTML</p>\r\n"
        "--B--\r\n"
    )
    assert server._get_text_body(_msg(raw)) == "Καθαρό κείμενο"


def test_get_text_body_falls_back_to_stripped_html_when_there_is_no_plain_part():
    # BREAKS IN PRODUCTION: HTML-only messages are most of a modern inbox. An
    # empty body here means sch_get_mail returns nothing readable, and
    # sch_forward_mail forwards an empty quote block.
    raw = (
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        "--B\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
        "<html><body><p>Καλημέρα</p><p>Γεια</p></body></html>\r\n"
        "--B--\r\n"
    )
    assert server._get_text_body(_msg(raw)) == "Καλημέρα\n\nΓεια"


def test_get_text_body_strips_html_on_a_non_multipart_html_message():
    raw = "Content-Type: text/html; charset=utf-8\r\n\r\n<div>Καλημέρα</div>"
    assert server._get_text_body(_msg(raw)) == "Καλημέρα"


def test_get_text_body_returns_empty_string_for_a_body_less_message():
    # BREAKS IN PRODUCTION: None here propagates into the string slicing in
    # sch_get_mail and raises there.
    raw = 'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="B"\r\n\r\n--B--\r\n'
    assert server._get_text_body(_msg(raw)) == ""


# ── _strip_html ─────────────────────────────────────────────────────────────


def test_strip_html_removes_script_and_style_content():
    # BREAKS IN PRODUCTION: CSS and JavaScript dumped into the body make a
    # newsletter unreadable and waste the max_body_chars budget, truncating the
    # actual message away.
    html = "<style>.x{color:red}</style><script>alert(1)</script><p>Καλημέρα</p>"
    assert server._strip_html(html) == "Καλημέρα"


def test_strip_html_converts_breaks_and_paragraphs_to_newlines():
    assert server._strip_html("<p>a</p><p>b</p>") == "a\n\nb"
    assert server._strip_html("a<br>b<br />c") == "a\nb\nc"


def test_strip_html_collapses_excessive_blank_lines():
    assert server._strip_html("<p>a</p><p></p><p></p><p>b</p>") == "a\n\nb"


def test_strip_html_leaves_greek_text_untouched():
    assert server._strip_html("<div>Αγαπητοί γονείς</div>") == "Αγαπητοί γονείς"


# ── Known defects, kept visible ─────────────────────────────────────────────


# REGRESSION GUARD. DEFECT (_decode_header, FIXED 2026-09-17), SEVERITY HIGH:
# _decode_header raises instead of degrading. 'unknown-8bit' is a legal RFC 2047 charset
# token with no Python codec, so it raises LookupError; malformed base64 raises
# email.errors.HeaderParseError. _decode_header is called unguarded inside the
# per-message loop of sch_list_mail, so ONE spam message with a broken encoded-word
# makes the entire folder listing fail, not just that row. It is also in the forward
# path, sch_forward_mail. Fix: wrap the decode in try/except and fall back to the raw
# string.
@pytest.mark.parametrize(
    "hostile",
    [
        "=?unknown-8bit?Q?Kalhmera?=",
        "=?utf-8?B?!!!notvalidbase64!!!?=",
        "=?not-a-real-charset?B?zrE=?=",
    ],
)
def test_decode_header_degrades_instead_of_raising_on_hostile_input(hostile):
    assert isinstance(server._decode_header(hostile), str)


# REGRESSION GUARD. DEFECT (_decode_header, FIXED 2026-09-17): the ' '.join of
# decode_header parts inserts a space that is already there, so an encoded-word next to
# plain text yields a doubled space. Visible in every listing, and it makes the Greek
# client-side substring search in sch_search_mail miss any query that spans the boundary.
def test_decode_header_does_not_double_the_space_next_to_plain_text():
    assert server._decode_header("Re: =?utf-8?B?zprOsc67zrc=?=") == "Re: Καλη"


# REGRESSION GUARD. DEFECT (_get_text_body, FIXED 2026-09-17): _get_text_body returns the
# first text/plain part in walk order, which is the ATTACHMENT when a .txt attachment
# precedes the body part. sch_forward_mail calls this to build the quoted block, so
# such a message is forwarded with the attachment's contents in place of the real
# body. Fix: skip parts whose Content-Disposition is attachment.
def test_get_text_body_ignores_a_text_plain_attachment():
    raw = (
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        "--B\r\nContent-Type: text/plain; charset=utf-8\r\n"
        'Content-Disposition: attachment; filename="notes.txt"\r\n\r\nATTACHMENT\r\n'
        "--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nΠΡΑΓΜΑΤΙΚΟ ΚΕΙΜΕΝΟ\r\n"
        "--B--\r\n"
    )
    assert server._get_text_body(_msg(raw)) == "ΠΡΑΓΜΑΤΙΚΟ ΚΕΙΜΕΝΟ"


# REGRESSION GUARD. DEFECT (_strip_html, FIXED 2026-09-17), SEVERITY LOW: _strip_html
# never unescapes HTML entities, so &nbsp;, &amp; and numeric entities survive into the
# body text. Greek text sent as numeric entities by older webmail is unreadable after
# stripping. Fix: html.unescape() at the end.
def test_strip_html_unescapes_entities():
    assert server._strip_html("<p>Καλημέρα&nbsp;&amp; &#956;&#941;&#961;&#945;</p>") == (
        "Καλημέρα & μέρα"
    )


# REGRESSION GUARD. DEFECT (server.py _decode_payload, FIXED 2026-10-01), SEVERITY LOW:
# a body part declaring a charset Python has no codec for ('unknown-8bit' is legal), or
# a non-text codec such as 'base64', made bytes.decode raise LookupError, so sch_get_mail
# and sch_forward_mail failed on that one message instead of returning its text.
# _decode_header already degraded for headers; bodies now do the same.
@pytest.mark.parametrize("charset", ["unknown-8bit", "x-no-such-charset", "base64"])
def test_get_text_body_degrades_on_a_charset_python_cannot_decode(charset):
    raw = f"Content-Type: text/plain; charset={charset}\r\n\r\nKalhmera".encode()
    assert server._get_text_body(email.message_from_bytes(raw)) == "Kalhmera"


def test_get_mail_html_body_degrades_on_a_charset_python_cannot_decode(imap):
    imap.fetch_payload = b"Content-Type: text/html; charset=unknown-8bit\r\n\r\n<p>Kalhmera</p>"
    assert server.sch_get_mail("7", body="html")["body"] == "<p>Kalhmera</p>"


# REGRESSION GUARD. DEFECT (_strip_html, FIXED 2026-10-02), SEVERITY LOW: the regexes
# that removed <style> and <script> blocks and every other tag rescanned to the end of
# the body from each "<style" or "<" that was never closed, so the time grew with the
# square of the body and one hostile message stalled every read of it. The single-pass
# replacements must keep the old output exactly, so they are checked against the old
# regexes on tricky markup and on thousands of generated strings.
def _strip_html_the_old_way(html: str) -> str:
    text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return html_module.unescape(text).replace("\xa0", " ").strip()


@pytest.mark.parametrize(
    "html",
    [
        "<style</style>",
        "<style>a</style></style>",
        "<style>x<style>y</style>z</style>",
        "<stylesheet>t</style>",
        "<STYLE>u</Style>v",
        "<style>never closed",
        "<script>alert(1)</script><p>x</p>",
        "<<>>",
        "<a<>",
        "<>>",
        "a < b and c > d",
        "a<",
        "",
    ],
)
def test_strip_html_gives_the_old_result_on_tricky_markup(html):
    assert server._strip_html(html) == _strip_html_the_old_way(html)


def test_strip_html_gives_the_old_result_on_generated_markup():
    pieces = ["<", ">", "/", "style", "STYLE", "script", "br", "p", " ", "x", "Κ", "\n"]
    pieces += ["&amp;", "<style>", "</style>", "<script>", "</Script>", "<>", "</p>", "<br/>"]
    rng = random.Random(20261002)
    for _ in range(5000):
        html = "".join(rng.choice(pieces) for _ in range(rng.randrange(40)))
        assert server._strip_html(html) == _strip_html_the_old_way(html), html


@pytest.mark.parametrize(
    "hostile",
    [
        pytest.param("<style>" * 50_000, id="unclosed-style"),
        pytest.param("<script>" * 50_000, id="unclosed-script"),
        pytest.param("<style" * 50_000, id="style-without-gt"),
        pytest.param("<" * 200_000, id="bare-lt"),
        pytest.param("<a" * 100_000, id="tag-without-gt"),
    ],
)
def test_strip_html_takes_linear_time_on_unclosed_tags(hostile):
    # The old regexes took 6 to 56 seconds on each of these when measured; one
    # pass takes a few milliseconds, so a one-second bound leaves a wide margin
    # for a slow CI runner.
    start = time.perf_counter()
    server._strip_html(hostile)
    assert time.perf_counter() - start < 1.0
