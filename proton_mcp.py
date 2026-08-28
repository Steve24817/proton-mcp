import imaplib
import logging
import os
import re
import ssl
import subprocess
import email
from datetime import datetime
from email.mime.text import MIMEText
from email.utils import formatdate

from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("proton-mail")

mcp = FastMCP("proton-mail")

_IMAP_HOST = os.environ.get("BRIDGE_IMAP_HOST", "127.0.0.1")
_IMAP_PORT = int(os.environ.get("BRIDGE_IMAP_PORT", "1143"))

_cached_user = None
_cached_pass = None


_OP_TOKEN_FILE = os.path.expanduser("~/.config/op/service-account-token")


def _op_env():
    """`op` needs OP_SERVICE_ACCOUNT_TOKEN. A GUI-launched Claude never sources
    ~/.zshrc, so fall back to a 0600 token file when the variable is absent."""
    env = dict(os.environ)
    if not env.get("OP_SERVICE_ACCOUNT_TOKEN"):
        try:
            with open(_OP_TOKEN_FILE) as fh:
                token = fh.read().strip()
        except OSError:
            return env
        if token:
            env["OP_SERVICE_ACCOUNT_TOKEN"] = token
    return env


def _op_read(ref):
    return subprocess.run(
        ["op", "read", ref],
        capture_output=True, text=True, check=True, env=_op_env(),
    ).stdout.strip()


def _get_credentials():
    global _cached_user, _cached_pass
    if _cached_user and _cached_pass:
        return _cached_user, _cached_pass
    env_user = os.environ.get("PROTON_EMAIL")
    env_pass = os.environ.get("PROTON_BRIDGE_PASSWORD")
    if env_user and env_pass:
        _cached_user, _cached_pass = env_user, env_pass
        return _cached_user, _cached_pass
    _cached_user = _op_read("op://Claude/Proton Bridge MCP/username")
    _cached_pass = _op_read("op://Claude/Proton Bridge MCP/password")
    return _cached_user, _cached_pass


def _connect():
    user, pw = _get_credentials()
    m = imaplib.IMAP4(_IMAP_HOST, _IMAP_PORT)
    try:
        m.starttls(ssl._create_unverified_context())
        m.login(user, pw)
        return m
    except Exception:
        try:
            m.shutdown()
        except Exception:
            pass
        raise


def _find_drafts_folder(conn):
    typ, data = conn.list()
    if typ != "OK":
        return "Drafts"
    for line in data:
        if b"\\Drafts" in line:
            match = re.search(rb'"([^"]*)"\s*$', line)
            if match:
                return match.group(1).decode("utf-8", errors="replace")
    return "Drafts"


def _strip_html(html):
    text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _imap_date(value):
    """Convert a YYYY-MM-DD tool input to the DD-Mon-YYYY IMAP SEARCH wants."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d-%b-%Y")
    except (ValueError, TypeError):
        return None


def _sanitize(value):
    """Strip characters that could break out of a quoted IMAP string."""
    return re.sub(r'["\\\r\n]', "", value)


@mcp.tool()
def list_folders():
    "List all mailbox folders on the account."
    try:
        conn = _connect()
        try:
            typ, data = conn.list()
            if typ != "OK":
                return "Error listing folders"
            folders = []
            for line in data:
                match = re.search(rb'"([^"]*)"\s*$', line)
                if match:
                    folders.append(match.group(1).decode("utf-8", errors="replace"))
            return folders
        finally:
            try:
                conn.logout()
            except Exception:
                pass
    except Exception as e:
        log.error("list_folders failed: %s", type(e).__name__)
        return f"Error: {type(e).__name__}"


@mcp.tool()
def search_messages(folder="INBOX", sender=None, subject=None,
                    since=None, before=None, limit=25):
    "Search messages in a folder. Returns uid, date, from, subject (no bodies)."
    try:
        limit = min(int(limit), 100)
    except (ValueError, TypeError):
        limit = 25
    criteria = []
    if sender:
        cleaned = _sanitize(sender)
        if cleaned:
            criteria += ["FROM", f'"{cleaned}"']
    if subject:
        cleaned = _sanitize(subject)
        if cleaned:
            criteria += ["SUBJECT", f'"{cleaned}"']
    if since:
        imap_since = _imap_date(since)
        if imap_since:
            criteria += ["SINCE", imap_since]
    if before:
        imap_before = _imap_date(before)
        if imap_before:
            criteria += ["BEFORE", imap_before]
    if not criteria:
        criteria = ["ALL"]
    try:
        conn = _connect()
        try:
            conn.select(folder, readonly=True)
            typ, data = conn.uid("search", None, *criteria)
            if typ != "OK" or not data[0]:
                return []
            uids = data[0].split()
            uids = uids[-limit:]
            uids.reverse()
            results = []
            for uid in uids:
                rt, rd = conn.uid("fetch", uid, "(BODY[HEADER.FIELDS (FROM DATE SUBJECT)])")
                if rt != "OK" or not rd or not rd[0]:
                    continue
                raw_header = rd[0][1]
                if isinstance(raw_header, bytes):
                    raw_header = raw_header.decode("utf-8", errors="replace")
                msg = email.message_from_string(raw_header)
                results.append({
                    "uid": uid.decode(),
                    "date": msg.get("Date", ""),
                    "from": msg.get("From", ""),
                    "subject": msg.get("Subject", ""),
                })
            return results
        finally:
            try:
                conn.logout()
            except Exception:
                pass
    except Exception as e:
        log.error("search_messages failed: %s", type(e).__name__)
        return f"Error: {type(e).__name__}"


@mcp.tool()
def read_message(uid, folder="INBOX"):
    "Read a single message by UID. Returns headers and plain-text body."
    try:
        conn = _connect()
        try:
            conn.select(folder, readonly=True)
            typ, data = conn.uid("fetch", uid, "(RFC822)")
            if typ != "OK" or not data or not data[0]:
                return "Error: message not found"
            raw = data[0][1]
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            msg = email.message_from_string(raw)
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    ct = part.get_content_type()
                    if ct == "text/plain":
                        payload = part.get_payload(decode=True)
                        if payload:
                            body = payload.decode("utf-8", errors="replace")
                            break
                if not body:
                    for part in msg.walk():
                        if part.get_content_type() == "text/html":
                            payload = part.get_payload(decode=True)
                            if payload:
                                html = payload.decode("utf-8", errors="replace")
                                body = _strip_html(html)
                                break
            else:
                ct = msg.get_content_type()
                payload = msg.get_payload(decode=True)
                if payload:
                    if ct == "text/html":
                        body = _strip_html(payload.decode("utf-8", errors="replace"))
                    else:
                        body = payload.decode("utf-8", errors="replace")
            if len(body) > 50000:
                body = body[:50000] + "\n\n[... truncated at 50,000 characters]"
            return {
                "from": msg.get("From", ""),
                "to": msg.get("To", ""),
                "date": msg.get("Date", ""),
                "subject": msg.get("Subject", ""),
                "body": body,
            }
        finally:
            try:
                conn.logout()
            except Exception:
                pass
    except Exception as e:
        log.error("read_message failed: %s", type(e).__name__)
        return f"Error: {type(e).__name__}"


@mcp.tool()
def create_draft(to, subject, body, cc=None):
    "Create a draft message in the Drafts folder. Does not send."
    try:
        msg = MIMEText(body, "plain", "utf-8")
        user, _pw = _get_credentials()
        msg["From"] = user
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        if cc:
            msg["Cc"] = cc
        conn = _connect()
        try:
            drafts = _find_drafts_folder(conn)
            conn.append(drafts, r"\Draft", None, msg.as_bytes())
            return {"folder": drafts}
        finally:
            try:
                conn.logout()
            except Exception:
                pass
    except Exception as e:
        log.error("create_draft failed: %s", type(e).__name__)
        return f"Error: {type(e).__name__}"


if __name__ == "__main__":
    mcp.run()
