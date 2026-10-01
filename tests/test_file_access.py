"""The folders attachments may be read from and downloads saved into.

Every path a tool receives is chosen by a model that is also reading the inbox,
and anyone can send mail to an sch.gr address. Without a limit, one hostile
message could get the model to attach ~/.ssh keys or ~/.sch-mail/credentials.json
to a draft, or to save its own attachment into a folder that runs what lands
there: ~/Library/LaunchAgents at the next login, or a venv's site-packages,
where a .pth file runs at the next interpreter start.

server._allowed_path confines both directions to an allowlist, resolved with
symlinks followed, and refuses hidden names below it. The defaults are ~/Downloads
plus the temp dirs; SCH_MAIL_ALLOWED_DIRS replaces them.
conftest pins it to each test's tmp_path, so nothing here touches a real user
folder.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from conftest import build_raw_message

import server


@pytest.fixture
def folders(tmp_path, monkeypatch):
    """An allowed folder, and a private one beside it holding a secret."""
    allowed = tmp_path / "allowed"
    private = tmp_path / "private"
    allowed.mkdir()
    private.mkdir()
    secret = private / "id_ed25519"
    secret.write_bytes(b"not a real key")
    monkeypatch.setenv(server.ALLOWED_DIRS_ENV, str(allowed))
    return allowed, private, secret


def _message_with(name: str, payload: bytes = b"%PDF-1.4 fake") -> bytes:
    return build_raw_message(subject="Εγκύκλιος", attachment=(name, payload, "application/pdf"))


# ── Attachments: sch_send_mail reads them from disk ─────────────────────────


def test_attachment_outside_the_allowed_folders_fails_the_send(folders):
    # BREAKS IN PRODUCTION: a model steered by a hostile email attaches a private
    # key or credentials.json, and the draft (or, with send_now, the message)
    # carries it out. No `imap` fixture: had anything been saved, the
    # default-deny IMAP stub would have raised instead.
    _allowed, _private, secret = folders
    result = server.sch_send_mail(
        to="a@example.gr", subject="s", body="b", attachments=[str(secret)]
    )
    assert "error" in result
    assert server.ALLOWED_DIRS_ENV in result["error"]


def test_a_refused_attachment_is_an_error_never_a_silent_omission(folders, imap):
    # BREAKS IN PRODUCTION: paths the code cannot use used to be skipped, so a
    # refused attachment would have produced a draft without it, reported as
    # "draft_saved". The refusal must be loud, and nothing may be saved, not
    # even with the other, allowed attachment.
    allowed, _private, secret = folders
    fine = allowed / "egkyklios.pdf"
    fine.write_bytes(b"%PDF-1.4")
    result = server.sch_send_mail(
        to="a@example.gr", subject="s", body="b", attachments=[str(fine), str(secret)]
    )
    assert result.get("status") != "draft_saved"
    assert server.ALLOWED_DIRS_ENV in result["error"]
    assert imap.appended == []


def test_a_missing_attachment_fails_the_send_and_saves_nothing(folders, imap):
    # BREAKS IN PRODUCTION: a path inside the allowed folders that does not
    # exist, a typo say, used to be skipped, and the draft was saved without it
    # and reported as "draft_saved".
    allowed, _private, _secret = folders
    fine = allowed / "egkyklios.pdf"
    fine.write_bytes(b"%PDF-1.4")
    typo = allowed / "egkyklio.pdf"
    result = server.sch_send_mail(
        to="a@example.gr", subject="s", body="b", attachments=[str(fine), str(typo)]
    )
    assert result.get("status") != "draft_saved"
    assert str(typo) in result["error"]
    assert imap.appended == []


def test_build_message_refuses_the_path_itself(folders):
    # BREAKS IN PRODUCTION: _build_message is the one place files are read, so
    # the check lives there, and a future caller gets it without remembering it.
    _allowed, _private, secret = folders
    with pytest.raises(server.PathNotAllowedError, match=server.ALLOWED_DIRS_ENV):
        server._build_message("user@sch.gr", "a@example.gr", "s", "b", attachments=[str(secret)])


def test_a_symlink_in_an_allowed_folder_cannot_point_out_of_it(folders):
    # BREAKS IN PRODUCTION: a harmless name in ~/Downloads that links to ~/.ssh
    # passes any check made on the path as written. The check is on where it
    # resolves to.
    allowed, _private, secret = folders
    link = allowed / "report.pdf"
    link.symlink_to(secret)
    with pytest.raises(server.PathNotAllowedError):
        server._build_message("user@sch.gr", "a@example.gr", "s", "b", attachments=[str(link)])


def test_dot_dot_cannot_climb_out_of_an_allowed_folder(folders):
    allowed, _private, _secret = folders
    climbing = f"{allowed}/../private/id_ed25519"
    with pytest.raises(server.PathNotAllowedError):
        server._build_message("user@sch.gr", "a@example.gr", "s", "b", attachments=[climbing])


def test_an_attachment_in_an_allowed_folder_is_attached(folders, imap):
    # BREAKS IN PRODUCTION: the allowlist must not stop the normal case, a file
    # the user saved into one of their own folders.
    allowed, _private, _secret = folders
    pdf = allowed / "egkyklios.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake content")
    result = server.sch_send_mail(to="a@example.gr", subject="s", body="b", attachments=[str(pdf)])
    assert result["status"] == "draft_saved"
    _mailbox, _flags, raw = imap.appended[0]
    assert b"egkyklios.pdf" in raw


def test_an_allowed_symlink_keeps_the_name_the_caller_gave(folders):
    # BREAKS IN PRODUCTION: resolving the path for the check must not rename the
    # attachment to whatever the link happens to point at.
    allowed, _private, _secret = folders
    real = allowed / "scan-0001.pdf"
    real.write_bytes(b"%PDF-1.4")
    link = allowed / "egkyklios.pdf"
    link.symlink_to(real)
    msg = server._build_message("user@sch.gr", "a@example.gr", "s", "b", attachments=[str(link)])
    assert [p.get_filename() for p in msg.walk() if p.get_filename()] == ["egkyklios.pdf"]


def test_a_hidden_file_inside_an_allowed_folder_is_refused(folders):
    # BREAKS IN PRODUCTION: a project kept under an allowed folder carries its .env
    # and .git/config with it; the allowlist alone would let either be attached.
    allowed, _private, _secret = folders
    dotenv = allowed / "project" / ".env"
    dotenv.parent.mkdir()
    dotenv.write_bytes(b"API_KEY=not-a-real-key")
    with pytest.raises(server.PathNotAllowedError, match=server.ALLOWED_DIRS_ENV):
        server._build_message("user@sch.gr", "a@example.gr", "s", "b", attachments=[str(dotenv)])


def test_a_hidden_folder_named_itself_in_the_env_var_is_allowed(tmp_path, monkeypatch):
    # BREAKS IN PRODUCTION: an operator who keeps outgoing files in a hidden folder
    # needs a way to allow it, and naming it in the variable is that way.
    outbox = tmp_path / ".outbox"
    outbox.mkdir()
    pdf = outbox / "egkyklios.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    monkeypatch.setenv(server.ALLOWED_DIRS_ENV, str(outbox))
    msg = server._build_message("user@sch.gr", "a@example.gr", "s", "b", attachments=[str(pdf)])
    assert [p.get_filename() for p in msg.walk() if p.get_filename()] == ["egkyklios.pdf"]


# ── Downloads: sch_download_attachments writes to disk ──────────────────────


def test_download_refuses_an_out_dir_outside_the_allowed_folders(folders, tmp_path):
    # BREAKS IN PRODUCTION: this is the persistence path. A sender-named file
    # saved into ~/Library/LaunchAgents runs at the next login. The folder must
    # not even be created: mkdir used to run before anything was checked. No
    # `imap` fixture, so the refusal must also come before any connection.
    launch_agents = tmp_path / "home" / "Library" / "LaunchAgents"
    result = server.sch_download_attachments(msg_id="7", out_dir=str(launch_agents))
    assert len(result) == 1
    assert server.ALLOWED_DIRS_ENV in result[0]["error"]
    assert not launch_agents.exists()


def test_download_refuses_a_venv_inside_an_allowed_folder(folders):
    # BREAKS IN PRODUCTION: a repo cloned under an allowed folder puts its own
    # .venv inside the allowlist, and a .pth file saved into its site-packages
    # runs the next time anything starts that interpreter, this server included.
    allowed, _private, _secret = folders
    site_packages = allowed / "sch-mail" / ".venv" / "lib" / "python3.12" / "site-packages"
    result = server.sch_download_attachments(msg_id="7", out_dir=str(site_packages))
    assert server.ALLOWED_DIRS_ENV in result[0]["error"]
    assert "hidden" in result[0]["error"]
    assert not (allowed / "sch-mail").exists()


def test_download_into_an_allowed_folder_saves_the_file(folders, imap):
    allowed, _private, _secret = folders
    imap.fetch_payload = _message_with("egkyklios.pdf", b"%PDF-1.4 fake")
    result = server.sch_download_attachments(msg_id="7", out_dir=str(allowed / "circulars"))
    saved = allowed / "circulars" / "egkyklios.pdf"
    assert result[0]["saved_to"] == str(saved.resolve())
    assert saved.read_bytes() == b"%PDF-1.4 fake"


def test_download_without_out_dir_still_lands_in_the_default_folder(imap, tmp_path, monkeypatch):
    # BREAKS IN PRODUCTION: the commonest call, a download with no out_dir into
    # ~/Downloads, must keep working under the built-in defaults.
    home = tmp_path / "home"
    downloads = home / "Downloads"
    monkeypatch.delenv(server.ALLOWED_DIRS_ENV)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(server, "DEFAULT_DOWNLOAD_DIR", downloads)
    imap.fetch_payload = _message_with("egkyklios.pdf")
    result = server.sch_download_attachments(msg_id="7")
    assert result[0]["saved_to"] == str((downloads / "egkyklios.pdf").resolve())


def test_download_never_writes_through_a_dangling_symlink(folders, imap):
    # BREAKS IN PRODUCTION: a link in an allowed folder that points at a file
    # which does not exist yet reports exists() False, and writing to its name
    # would create that file wherever it is. The download lands beside it.
    allowed, private, _secret = folders
    outside = private / "zz_evil.pth"
    (allowed / "egkyklios.pdf").symlink_to(outside)
    imap.fetch_payload = _message_with("egkyklios.pdf")
    result = server.sch_download_attachments(msg_id="7", out_dir=str(allowed))
    assert not outside.exists()
    assert Path(result[0]["saved_to"]).name == "egkyklios_1.pdf"


@pytest.mark.parametrize(
    ("sent_as", "saved_as"),
    [
        (".zshenv", "_.zshenv"),
        ("invoice\u202efdp.exe", "invoice_fdp.exe"),
        ("bell\x07name.pdf", "bell_name.pdf"),
    ],
)
def test_download_neutralises_hidden_and_disguised_names(folders, imap, sent_as, saved_as):
    # BREAKS IN PRODUCTION: the file name is the sender's. A leading dot hides
    # the file from Finder and ls, a right-to-left override makes an .exe read
    # as a .pdf, and control characters do the same job less subtly.
    allowed, _private, _secret = folders
    imap.fetch_payload = _message_with(sent_as)
    result = server.sch_download_attachments(msg_id="7", out_dir=str(allowed))
    assert Path(result[0]["saved_to"]).name == saved_as


# ── Which folders are allowed ───────────────────────────────────────────────


@pytest.mark.parametrize("unset", ["delete", "empty"])
def test_built_in_defaults(tmp_path, monkeypatch, unset):
    # BREAKS IN PRODUCTION: ~/Documents, ~/Desktop, ~/Pictures and cloud-synced
    # folders used to be defaults too. They hold private documents a steered
    # model could attach, and projects whose startup hooks a download could
    # plant (a .pth in a venv that is not hidden). The defaults are exactly these.
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    if unset == "delete":
        monkeypatch.delenv(server.ALLOWED_DIRS_ENV)
    else:
        monkeypatch.setenv(server.ALLOWED_DIRS_ENV, "")
    expected = [home / "Downloads", Path(tempfile.gettempdir()), Path("/tmp")]
    assert server._allowed_dirs() == [p.resolve() for p in expected]


def test_the_env_var_replaces_the_defaults_rather_than_adding_to_them(tmp_path, monkeypatch):
    # BREAKS IN PRODUCTION: an operator who narrows the list must get exactly the
    # folders named, not those plus ~/Downloads and the temp dirs.
    home = tmp_path / "home"
    first = tmp_path / "first"
    second = tmp_path / "second"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(server.ALLOWED_DIRS_ENV, os.pathsep.join([str(first), str(second)]))
    assert server._allowed_dirs() == [first.resolve(), second.resolve()]
    assert server._allowed_path(second / "x.pdf") == (second / "x.pdf").resolve()
    with pytest.raises(server.PathNotAllowedError, match=server.ALLOWED_DIRS_ENV):
        server._allowed_path(home / "Downloads" / "x.pdf")


def test_tilde_in_the_env_var_is_expanded(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(server.ALLOWED_DIRS_ENV, "~/Mail")
    assert server._allowed_dirs() == [(home / "Mail").resolve()]


def test_relative_entries_in_the_env_var_are_ignored(tmp_path, monkeypatch):
    # BREAKS IN PRODUCTION: a relative entry would silently mean whatever
    # directory the server was started in; run_mcp.sh starts it in the repo.
    monkeypatch.setenv(server.ALLOWED_DIRS_ENV, os.pathsep.join(["relative/dir", str(tmp_path)]))
    assert server._allowed_dirs() == [tmp_path.resolve()]
