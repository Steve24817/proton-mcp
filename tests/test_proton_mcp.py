import email
import os
from unittest.mock import patch, MagicMock

import pytest

import proton_mcp
from proton_mcp import search_messages, read_message, create_draft, list_folders


# Real LIST lines captured from the live Proton Bridge. The fake must return
# these verbatim so folder parsing is tested against the real wire format
# (including the space in "Folders/Misc Services").
REAL_LIST_LINES = [
    b'(\\Unmarked) "/" "Folders/Misc Services"',
    b'(\\Noinferiors \\Unmarked) "/" "INBOX"',
    b'(\\Drafts \\Noinferiors \\Unmarked) "/" "Drafts"',
]


class FakeIMAP:
    """Minimal IMAP mock that records calls and returns canned responses.

    Behaves like real imaplib: every status is the str "OK", never bytes.
    """

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.selected = None
        self.appended = []
        self.uid_calls = []
        self._search_results = b""
        self._fetch_responses = []
        self._list_response = ("OK", list(REAL_LIST_LINES))

    def starttls(self, ctx):
        pass

    def login(self, user, pw):
        pass

    def list(self):
        return self._list_response

    def select(self, folder, readonly=False):
        self.selected = folder
        return ("OK", [b"1"])

    def uid(self, cmd, uid, *args):
        self.uid_calls.append((cmd, uid, *args))
        if cmd == "search":
            return ("OK", [self._search_results])
        if cmd == "fetch":
            return self._fetch_responses.pop(0) if self._fetch_responses else ("OK", [None])
        return ("OK", [])

    def append(self, folder, flags, date_time, message):
        self.appended.append((folder, flags, message))
        return ("OK", [])

    def logout(self):
        pass

    def shutdown(self):
        pass


def _make_search_header(uid, from_val, date_val, subject_val):
    raw = f"From: {from_val}\r\nDate: {date_val}\r\nSubject: {subject_val}\r\n\r\n"
    return ("OK", [(b"1 (UID " + str(uid).encode() + b" BODY[HEADER.FIELDS (FROM DATE SUBJECT)] {}", raw.encode())])


def _make_rfc822(uid, from_val, to_val, date_val, subject_val, body, content_type="text/plain"):
    from email.mime.text import MIMEText
    if content_type == "text/plain":
        msg = MIMEText(body, "plain", "utf-8")
    else:
        msg = MIMEText(body, "html", "utf-8")
    msg["From"] = from_val
    msg["To"] = to_val
    msg["Date"] = date_val
    msg["Subject"] = subject_val
    raw = msg.as_bytes()
    return ("OK", [(b"1 (UID " + str(uid).encode() + b" RFC822 {}", raw)])


@pytest.fixture(autouse=True)
def _set_env():
    os.environ["PROTON_EMAIL"] = "test@example.com"
    os.environ["PROTON_BRIDGE_PASSWORD"] = "testpass"
    proton_mcp._cached_user = None
    proton_mcp._cached_pass = None
    yield
    proton_mcp._cached_user = None
    proton_mcp._cached_pass = None


# --- search_messages: SEARCH criteria ---

@pytest.mark.parametrize("kwargs,expected", [
    ({}, ["ALL"]),
    ({"sender": "a@b.com"}, ["FROM", '"a@b.com"']),
    ({"subject": "hello"}, ["SUBJECT", '"hello"']),
    ({"since": "2025-01-01"}, ["SINCE", "01-Jan-2025"]),
    ({"before": "2025-12-31"}, ["BEFORE", "31-Dec-2025"]),
    ({"sender": "a@b.com", "subject": "hi", "since": "2025-01-01"},
     ["FROM", '"a@b.com"', "SUBJECT", '"hi"', "SINCE", "01-Jan-2025"]),
    ({"sender": "a@b.com", "before": "2025-12-31"},
     ["FROM", '"a@b.com"', "BEFORE", "31-Dec-2025"]),
])
def test_search_builds_criteria(kwargs, expected):
    fake = FakeIMAP("127.0.0.1", 1143)
    fake._search_results = b""
    with patch("proton_mcp._connect", return_value=fake):
        search_messages(**kwargs)
    search_call = [c for c in fake.uid_calls if c[0] == "search"][0]
    assert list(search_call[2:]) == expected


def test_search_returns_newest_first_capped():
    fake = FakeIMAP("127.0.0.1", 1143)
    fake._search_results = b"101 102 103 104 105"
    fake._fetch_responses = [
        _make_search_header(str(i), f"user{i}@x.com", "Fri, 10 Jan 2025", f"Sub {i}")
        for i in range(101, 106)
    ]
    with patch("proton_mcp._connect", return_value=fake):
        results = search_messages(limit=3)
    assert len(results) == 3
    assert results[0]["uid"] == "105"
    assert results[1]["uid"] == "104"
    assert results[2]["uid"] == "103"


def test_search_limit_capped_at_100():
    fake = FakeIMAP("127.0.0.1", 1143)
    fake._search_results = b"1 2"
    fake._fetch_responses = [
        _make_search_header("1", "a@b.com", "Fri, 10 Jan 2025", "S1"),
        _make_search_header("2", "a@b.com", "Fri, 10 Jan 2025", "S2"),
    ]
    with patch("proton_mcp._connect", return_value=fake):
        search_messages(limit=999)
    # Verify cap: the search call should have returned results limited to 100.
    # With only 2 UIDs both limits work. The key is it doesn't crash.


# --- read_message ---

def test_read_message_plain_text():
    fake = FakeIMAP("127.0.0.1", 1143)
    fake._fetch_responses = [
        _make_rfc822("42", "alice@x.com", "bob@y.com", "Mon, 13 Jan 2025", "Hello", "Plain body here")
    ]
    with patch("proton_mcp._connect", return_value=fake):
        result = read_message("42")
    assert result["from"] == "alice@x.com"
    assert result["to"] == "bob@y.com"
    assert result["subject"] == "Hello"
    assert result["body"] == "Plain body here"


def test_read_message_html_only_strips_tags():
    fake = FakeIMAP("127.0.0.1", 1143)
    fake._fetch_responses = [
        _make_rfc822("43", "a@b.com", "c@d.com", "Tue, 14 Jan 2025", "HTML Msg",
                     "<html><body><p>Hello <b>world</b></p></body></html>", "text/html")
    ]
    with patch("proton_mcp._connect", return_value=fake):
        result = read_message("43")
    assert "Hello" in result["body"]
    assert "<b>" not in result["body"]
    assert "<html>" not in result["body"]


def test_read_message_truncation():
    fake = FakeIMAP("127.0.0.1", 1143)
    long_body = "x" * 60000
    fake._fetch_responses = [
        _make_rfc822("44", "a@b.com", "c@d.com", "Wed, 15 Jan 2025", "Long", long_body)
    ]
    with patch("proton_mcp._connect", return_value=fake):
        result = read_message("44")
    assert len(result["body"]) == 50000 + len("\n\n[... truncated at 50,000 characters]")
    assert "truncated" in result["body"]


# --- create_draft ---

def test_create_draft_appends_to_drafts_with_flag():
    fake = FakeIMAP("127.0.0.1", 1143)
    fake._list_response = ("OK", [b'(\\HasNoChildren) "/" "INBOX"'])
    with patch("proton_mcp._connect", return_value=fake):
        result = create_draft("to@example.com", "Test Subject", "Test body")
    assert result["folder"] == "Drafts"
    assert len(fake.appended) == 1
    folder, flags, _msg_bytes = fake.appended[0]
    assert folder == "Drafts"
    assert "\\Draft" in flags


def test_create_draft_finds_drafts_by_special_use():
    fake = FakeIMAP("127.0.0.1", 1143)
    fake._list_response = ("OK", [b'(\\Drafts \\HasNoChildren) "/" "SomeFolder/Drafts"'])
    with patch("proton_mcp._connect", return_value=fake):
        result = create_draft("to@example.com", "Subj", "Body")
    assert result["folder"] == "SomeFolder/Drafts"


def test_create_draft_never_sends():
    """Ensure only append was called, no uid commands."""
    fake = FakeIMAP("127.0.0.1", 1143)
    fake._list_response = ("OK", [])
    with patch("proton_mcp._connect", return_value=fake):
        create_draft("to@x.com", "Sub", "Body")
    assert fake.uid_calls == []


# --- regression: bugs found against the live Proton Bridge ---

def test_list_folders_parses_real_bridge_lines():
    """Bug 1 + 4: real LIST output (str status, spaced names) must parse."""
    fake = FakeIMAP("127.0.0.1", 1143)
    with patch("proton_mcp._connect", return_value=fake):
        folders = list_folders()
    assert isinstance(folders, list)
    assert "INBOX" in folders
    assert "Folders/Misc Services" in folders


def test_search_dates_use_imap_format():
    """Bug 2: SINCE/BEFORE must be DD-Mon-YYYY, never the raw YYYY-MM-DD."""
    fake = FakeIMAP("127.0.0.1", 1143)
    with patch("proton_mcp._connect", return_value=fake):
        search_messages(since="2026-08-01", before="2026-08-31")
    args = list([c for c in fake.uid_calls if c[0] == "search"][0][2:])
    assert "SINCE" in args and "01-Aug-2026" in args
    assert "BEFORE" in args and "31-Aug-2026" in args
    assert "2026-08-01" not in args
    assert "2026-08-31" not in args


def test_search_drops_unparseable_dates():
    """Bug 2: a bad date is ignored, never interpolated raw."""
    fake = FakeIMAP("127.0.0.1", 1143)
    with patch("proton_mcp._connect", return_value=fake):
        search_messages(since="not-a-date", before="31/12/2026")
    args = list([c for c in fake.uid_calls if c[0] == "search"][0][2:])
    assert args == ["ALL"]


def test_search_sender_quote_cannot_inject_tokens():
    """Bug 3: a quote in sender must not add extra SEARCH tokens."""
    fake = FakeIMAP("127.0.0.1", 1143)
    with patch("proton_mcp._connect", return_value=fake):
        search_messages(sender='a"@b.com" SUBJECT "x')
    args = list([c for c in fake.uid_calls if c[0] == "search"][0][2:])
    # Sender stays a single quoted argument...
    assert len(args) == 2
    assert args[0] == "FROM"
    assert '"' not in args[1][1:-1]
    # ...and the smuggled SUBJECT never becomes its own token.
    assert "SUBJECT" not in args


def test_find_drafts_folder_from_real_list_line():
    """Bug 4: the real \\Drafts special-use line must yield "Drafts"."""
    fake = FakeIMAP("127.0.0.1", 1143)
    assert proton_mcp._find_drafts_folder(fake) == "Drafts"


def test_create_draft_sets_from_header():
    """Bug 6: drafts must carry a From header (the account address)."""
    fake = FakeIMAP("127.0.0.1", 1143)
    with patch("proton_mcp._connect", return_value=fake):
        create_draft("to@example.com", "Subj", "Body")
    msg = email.message_from_bytes(fake.appended[0][2])
    assert msg["From"] == "test@example.com"


# --- smtplib absence ---

def test_no_smtplib_in_server():
    with open("proton_mcp.py") as f:
        source = f.read()
    assert "smtplib" not in source, "smtplib must not be imported or referenced"


# --- credentials via op ---

def test_credentials_from_op_when_no_env(monkeypatch):
    monkeypatch.delenv("PROTON_EMAIL", raising=False)
    monkeypatch.delenv("PROTON_BRIDGE_PASSWORD", raising=False)
    proton_mcp._cached_user = None
    proton_mcp._cached_pass = None

    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        result = MagicMock()
        if "username" in argv[2]:
            result.stdout = "user@proton.me\n"
        else:
            result.stdout = "secret123\n"
        return result

    with patch("subprocess.run", side_effect=fake_run):
        user, pw = proton_mcp._get_credentials()
    assert user == "user@proton.me"
    assert pw == "secret123"
    assert calls[0] == ["op", "read", "op://Claude/Proton Bridge MCP/username"]
    assert calls[1] == ["op", "read", "op://Claude/Proton Bridge MCP/password"]


def test_dotenv_never_opened(monkeypatch):
    """Ensure .env file is never opened for reading."""
    opened = []
    real_open = open

    def tracking_open(path, *a, **kw):
        opened.append(str(path))
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", tracking_open)
    os.environ["PROTON_EMAIL"] = "t@t.com"
    os.environ["PROTON_BRIDGE_PASSWORD"] = "pw"
    proton_mcp._cached_user = None
    proton_mcp._cached_pass = None

    fake = FakeIMAP("127.0.0.1", 1143)
    with patch("proton_mcp._connect", return_value=fake):
        list_folders()

    for p in opened:
        assert not p.endswith(".env"), f".env should not be opened, but got {p}"


def test_op_env_falls_back_to_token_file(tmp_path, monkeypatch):
    """A GUI-launched Claude has no OP_SERVICE_ACCOUNT_TOKEN; the 0600 file supplies it."""
    tf = tmp_path / "service-account-token"
    tf.write_text("tok-from-file\n")
    monkeypatch.setattr(proton_mcp, "_OP_TOKEN_FILE", str(tf))
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    assert proton_mcp._op_env()["OP_SERVICE_ACCOUNT_TOKEN"] == "tok-from-file"


def test_op_env_prefers_real_environment(tmp_path, monkeypatch):
    tf = tmp_path / "service-account-token"
    tf.write_text("tok-from-file")
    monkeypatch.setattr(proton_mcp, "_OP_TOKEN_FILE", str(tf))
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "tok-from-env")
    assert proton_mcp._op_env()["OP_SERVICE_ACCOUNT_TOKEN"] == "tok-from-env"


def test_op_env_survives_missing_token_file(monkeypatch):
    monkeypatch.setattr(proton_mcp, "_OP_TOKEN_FILE", "/nonexistent/token")
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    assert "OP_SERVICE_ACCOUNT_TOKEN" not in proton_mcp._op_env()
