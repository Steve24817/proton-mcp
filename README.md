# proton-mcp

An MCP server for reading ProtonMail through a locally running
[Proton Bridge](https://proton.me/mail/bridge), plus the ability to create a draft.
It speaks IMAP to `127.0.0.1` only. It does not talk to any cloud API.

## What it does

| Tool | What it does |
|------|--------------|
| `list_folders` | Lists the folders on the account. |
| `search_messages` | Searches a folder by sender, subject, and date range. Returns headers only. |
| `read_message` | Returns one message: headers and body text. |
| `create_draft` | Writes a draft into the Drafts folder. **Never sends it.** |

Four tools. The whole server is one file you can read in a sitting.

## What it deliberately does not do, and why

This repository previously held a 2,505-line fork of unknown provenance exposing 27
tools. It could send mail as the account owner and bulk-delete mailboxes. Its history
includes a commit titled *"Apply security fixes from upstream PR #3"* — security
defects were found in that code once, and the fix arrived by merge rather than by
anyone here reading it. It was rebuilt from scratch in August 2026.

Before the rebuild we counted what had ever actually called those 27 tools across
every Claude session on this machine: **22 calls, using 4 tools** — `search_emails`,
`get_recent_emails`, `get_mailboxes`, `get_email_content`. All four are reads. The
other 23 tools, including every destructive one, were never called once.

**Sending is absent, not disabled.** There is no `send_email` tool, and `smtplib` is
not imported anywhere in this repository — a test enforces that. The server is
structurally incapable of sending mail, rather than merely choosing not to. This
mirrors a boundary that has held up well elsewhere in the estate: the CRM's
`ship_to_outlook_draft` writes a draft and a human presses send.

**Destructive operations are absent, not gated.** No delete, no expunge, no move, no
folder creation or deletion, no flag changes beyond marking a new draft as a draft.
The old server's `bulk_delete_emails`, `delete_folder` and `delete_filter_rule` are
gone. An assistant should not perform these unattended, and for an operation like
that the safest implementation is absence — a confirmation prompt is only as good as
the attention of whoever is reading it, and prompts get approved by habit.

**The convenience surface is gone**: filter rules, unsubscribe automation, junk
scoring, and the bulk variants of everything. None of it was ever used. Starting from
27 tools is how the original reached 2,505 lines. If one of these turns out to be
genuinely needed, add it back deliberately, with a reason, and a test.

## Credentials

Credentials live in 1Password, not in a file. The server reads them at startup via the
`op` CLI, authenticated by the `OP_SERVICE_ACCOUNT_TOKEN` already in the environment:

```bash
op read "op://Claude/Proton Bridge MCP/username"
op read "op://Claude/Proton Bridge MCP/password"
```

`op` itself needs `OP_SERVICE_ACCOUNT_TOKEN`. A GUI-launched Claude never sources
`~/.zshrc`, so when that variable is absent the server falls back to reading it from
`~/.config/op/service-account-token` (mode 0600). Create that file once:

```bash
mkdir -p ~/.config/op && chmod 700 ~/.config/op
printf '%s' "$OP_SERVICE_ACCOUNT_TOKEN" > ~/.config/op/service-account-token
chmod 600 ~/.config/op/service-account-token
```

The stored secret is a **Bridge-specific password**, not the Proton account password;
it grants access only to the local Bridge listener. There is no `.env` file, and
`python-dotenv` is not a dependency.

The server never logs message subjects, bodies, addresses, or credentials. It logs
connection events and errors only.

## Setup

```bash
python3 -m venv venv && ./venv/bin/python3 -m pip install -r requirements.txt
```

Register it with Claude Code (note: no secrets in the registration):

```bash
claude mcp add proton-mail --scope user -- /Users/stephengray/Developer/tools/proton-mcp/venv/bin/python3 /Users/stephengray/Developer/tools/proton-mcp/proton_mcp.py
```

Proton Bridge must be running. It listens on `127.0.0.1:1143` and requires STARTTLS
with a self-signed local certificate. Override with `BRIDGE_IMAP_HOST` /
`BRIDGE_IMAP_PORT` if your Bridge is configured differently.

## Tests

```bash
./venv/bin/python3 -m pytest tests/ -q
```

The tests use a fake IMAP object and touch no network and no real mailbox.
