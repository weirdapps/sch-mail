"""The MCP instructions every client receives.

The repo ships only a generic description. Guidance specific to one deployment,
such as house rules for how a particular mailbox may be used, belongs to
whoever runs it, so it lives in ~/.sch-mail/instructions.md, outside the repo,
and server._load_instructions appends it at startup.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import server

REPO = Path(__file__).resolve().parent.parent
LOCAL_TEXT = "Use the read tools only."


def test_without_a_local_file_the_generic_text_is_used(tmp_path):
    # BREAKS IN PRODUCTION: a fresh install has no local file and must still
    # start and describe itself.
    assert server._load_instructions(tmp_path / "absent.md") == server.BASE_INSTRUCTIONS


def test_a_local_file_is_appended_after_the_generic_text(tmp_path):
    local = tmp_path / "instructions.md"
    local.write_text(f"{LOCAL_TEXT}\n", encoding="utf-8")
    assert server._load_instructions(local) == f"{server.BASE_INSTRUCTIONS} {LOCAL_TEXT}"


def test_a_blank_local_file_changes_nothing(tmp_path):
    local = tmp_path / "instructions.md"
    local.write_text(" \n\n", encoding="utf-8")
    assert server._load_instructions(local) == server.BASE_INSTRUCTIONS


def test_greek_in_the_local_file_survives(tmp_path):
    # BREAKS IN PRODUCTION: read with the platform's default encoding, Greek
    # guidance would reach every client as mojibake.
    local = tmp_path / "instructions.md"
    local.write_text("Μόνο ανάγνωση.", encoding="utf-8")
    assert server._load_instructions(local) == f"{server.BASE_INSTRUCTIONS} Μόνο ανάγνωση."


def test_an_undecodable_local_file_stops_the_server_starting(tmp_path):
    # BREAKS IN PRODUCTION: a server that starts anyway, quietly without the
    # guidance it was given, is worse than one that refuses to start.
    local = tmp_path / "instructions.md"
    local.write_bytes(b"\xff\xfe not utf-8 \xff")
    with pytest.raises(UnicodeDecodeError):
        server._load_instructions(local)


def test_the_local_file_lives_beside_the_credentials():
    assert server.INSTRUCTIONS_PATH == Path.home() / ".sch-mail" / "instructions.md"


def test_the_server_announces_the_local_file_read_at_startup(tmp_path):
    # BREAKS IN PRODUCTION: the loader passing in isolation proves nothing if the
    # server object is built from something else. Import the module fresh, in a
    # child process with its own HOME, and read what the server would announce.
    config = tmp_path / ".sch-mail"
    config.mkdir()
    (config / "instructions.md").write_text(f"{LOCAL_TEXT}\n", encoding="utf-8")
    child = subprocess.run(
        [sys.executable, "-c", "import server; print(server.mcp.instructions)"],
        cwd=REPO,
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert child.stdout.strip() == f"{server.BASE_INSTRUCTIONS} {LOCAL_TEXT}"


def test_the_generic_text_claims_no_restriction_the_code_does_not_enforce():
    # BREAKS IN PRODUCTION: a client told that a tool is blocked will rely on it,
    # and nothing in this code blocks any tool. A restriction belongs in the
    # local file of a deployment that enforces it.
    assert "blocked" not in server.BASE_INSTRUCTIONS.lower()
