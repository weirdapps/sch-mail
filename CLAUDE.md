# sch-mail

MCP server for the Greek School Network (`sch.gr`) mailbox. Provides IMAP read + SMTP send access via 10 MCP tools (6 read, 4 write) exposed through the MCP SDK's `MCPServer`. Used from Claude Code (or any MCP client) as the `sch-mail` MCP server.

## Send policy (read this before using any write tool)

`sch_send_mail` and `sch_forward_mail` are draft-first: without `send_now=True` they only save to Drafts. `send_now=True` dispatches at once and cannot be undone, so never pass it unless the user asked for that exact send, and do not build automation that does. Policy for a particular deployment is not kept in this repo: the server appends the operator's optional `~/.sch-mail/instructions.md` to its MCP instructions at startup, and when that text is present, follow it. The SMTP write path stays in the code, and is inherited by the derived `weirdapps/yahoo-access` server.

## Tech Stack

- Python 3.12+, single runtime dependency: `mcp[cli]>=2.0.0`; `dev` extra adds `ruff>=0.16.3` and `pytest>=8.3.0`
- Server object is `from mcp.server import MCPServer` (SDK v2), **not** the older `mcp.server.fastmcp.FastMCP`. The v2 `@mcp.tool()` decorator returns the plain function, so tests call `server.sch_list_mail(...)` directly (no `.fn` attribute).
- Standard library only otherwise: `imaplib`, `smtplib`, `email.*`, `ssl`
- Servers: IMAP `mail.sch.gr:993` (SSL), SMTP `mail.sch.gr:465` (SSL)
- CI: `.github/workflows/ci.yml` runs `ruff check` + `ruff format --check` (lint job) and an `import server` smoke check + `uv run pytest -q` (test job) on push and PR to `master`. Installs use `uv sync --frozen`, so `uv.lock` must stay in step with `pyproject.toml`.
- Tests: `tests/`, run with `uv run pytest -q`, all offline. `tests/conftest.py` is default-deny: real sockets, `IMAP4_SSL` / `SMTP_SSL` and the real credentials file all fail loudly unless a test opts into a recorded fake. `tests/test_uid_addressing.py` models a stateful IMAP server for the UID and expunge rules below.

## Install

```bash
uv sync --extra dev --frozen     # matches CI; plain `uv sync` omits ruff and pytest
# pip fallback:
python -m venv .venv && .venv/bin/pip install -e .
```

`run_mcp.sh` runs `.venv/bin/python`, so create `.venv` with one of the commands above before the first run.

## Run

```bash
bash run_mcp.sh
# Equivalent: .venv/bin/python -m server
```

The MCP server is started by Claude Code automatically via the registered config. Run manually only for debugging.

## Credentials

Stored at `~/.sch-mail/credentials.json` — never committed. Two formats supported:

```json
{ "email": "...", "password": "..." }
```
or multi-account:
```json
{ "accounts": { "personal": { "email": "...", "password": "..." } }, "default": "personal" }
```

## Code Organization

Single-file project (`server.py`, ~1,400 lines):

1. Constants (hosts, ports, credential and instructions paths, folder candidates), `BASE_INSTRUCTIONS` and `_load_instructions()`, which appends the optional `~/.sch-mail/instructions.md`
2. `_load_credentials()` — reads `~/.sch-mail/credentials.json`. File access: `PathNotAllowedError`, `_allowed_dirs`, `_allowed_path`
3. IMAP helpers — `_connect`, `_check_imap_args`, `_imap_quote`, `_decode_header`, `_parse_date`, `_decode_payload`, `_get_text_body`, `_drop_blocks`, `_drop_tags`, `_strip_html`, `_list_attachment_info`, `_imap_date`
4. Message addressing: `_q`, `_is_uid`, `_uid_exists`, `_capabilities`, `_expunge_uid`, `_move_uid`
5. Send helpers — `_build_message`, `_smtp_connect`, `_split_addresses`, `_collect_recipients`, `_find_special_folder`, `_save_to_folder`
6. MCP read tools — `sch_list_folders`, `sch_list_mail`, `sch_get_mail`, `sch_search_mail`, `sch_download_attachments`, `sch_mail_stats`
7. MCP write tools — `sch_send_mail`, `sch_forward_mail`, `sch_move_mail`, `sch_create_folder`

## Key Conventions

- All IMAP connections opened fresh per tool call, closed in `finally` blocks — no connection pooling.
- All tools accept optional `account: str | None`, matching a key in the `accounts` dict. When omitted, `_load_credentials` uses the `default` key from `credentials.json`, falling back to the first account defined. Docstrings and docs use placeholder keys only ("personal", "work").
- `send_now=False` default: appends to Drafts with flags `(\Draft \Seen)`, never dispatches without explicit opt-in. Leave it at the default, see the send policy above.
- Message ids are IMAP UIDs everywhere (`conn.uid(...)`), never sequence numbers, which renumber on every expunge: list and search hand out UIDs, and get, download, forward and move take them. Move uses UID MOVE when the post-login CAPABILITY has MOVE, else UID COPY + `\Deleted` + UID EXPUNGE (UIDPLUS), and a bare EXPUNGE only when the server has neither (`_expunge_uid` says why). Capabilities are read after login on purpose: sch.gr's pre-login greeting lists neither extension. A move refuses anything but one numeric UID and reports a UID that no longer exists as not found.
- `send_now=True` dispatches via SMTP AND archives to Sent via IMAP APPEND. Only on an explicit request for that send.
- Unicode queries (Greek text) fall back to client-side filtering over 180-day window — IMAP SEARCH doesn't handle non-ASCII reliably on sch.gr.
- Attachments download to `~/Downloads` by default; `filename_filter` does case-insensitive substring match.
- File access is confined to an allowlist (`_allowed_dirs` / `_allowed_path`, symlinks resolved), by default `~/Downloads`, the system temp dir and `/tmp`: `sch_send_mail` reads attachments and `sch_download_attachments` writes files only inside it, and never through a hidden name below an allowed folder. `SCH_MAIL_ALLOWED_DIRS` (os.pathsep-separated absolute paths) replaces the defaults. An attachment that is refused, missing or not a regular file fails the call; it is never dropped.
- Values that reach an IMAP command line (query, folder, msg_id, folder name) are refused if they hold NUL, CR or LF (`_check_imap_args`), and search terms are quoted with `_imap_quote`. `tests/conftest.py` pins `SCH_MAIL_ALLOWED_DIRS` to each test's tmp_path.
- Folder discovery for Drafts/Sent uses a candidate list to handle sch.gr IMAP namespace variations.
