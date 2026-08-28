# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this is

An MCP server exposing a ProtonMail account through a local Proton Bridge, over IMAP
to `127.0.0.1` only. The entire server is `proton_mcp.py` — read it before changing it;
it is short on purpose.

## The one rule

**This server reads mail and creates drafts. It does nothing else.**

It has four tools: `list_folders`, `search_messages`, `read_message`, `create_draft`.

Before adding anything, read the "What it deliberately does not do" section of the
README. This repository previously held a 2,505-line fork with 27 tools that could
send mail and bulk-delete mailboxes; a usage audit found only 4 of those 27 tools had
ever been called. It was rebuilt in August 2026 to be small enough to review.

Do not add:
- **sending.** `smtplib` must never be imported. A test enforces this. Sending is
  absent by design so the server is incapable of it, not merely declining.
- **destructive operations** — delete, expunge, move, folder create/delete, flag
  changes beyond `\Draft` on a new draft. A confirmation prompt is not an acceptable
  substitute for absence.
- convenience surface (filter rules, unsubscribe, junk scoring, bulk variants). This
  is what took the previous version to 2,505 lines.

If a new tool is genuinely needed, add it with a stated reason and a test, and keep the
file under 400 lines.

## Working on it

Credentials come from 1Password (`op://Claude/Proton Bridge MCP`), never a file. Do not
add a `.env` or a `dotenv` dependency. Never log message subjects, bodies, addresses,
or credentials.

IMAP quirks that have bitten this code before, all covered by tests:
- `imaplib` returns status as **`str`** (`"OK"`), not bytes. Comparing to `b"OK"`
  silently breaks every tool while the test suite stays green.
- IMAP SEARCH dates are `DD-Mon-YYYY`. The tools take `YYYY-MM-DD` and convert.
- Never interpolate user input into a SEARCH string. Pass criteria as separate argv
  and sanitise quotes and backslashes.
- Folder names contain spaces; do not match them with `\S+`.
- `close()` is illegal before `SELECT`; use `shutdown()` in connection error paths.

Tests use a fake IMAP whose return types match the real library. If you change the
fake, make sure it still returns `str` statuses and realistic `LIST` bytes — a fake
that is wrong produces a green suite over a broken server.

```bash
./venv/bin/python3 -m pytest tests/ -q
```
