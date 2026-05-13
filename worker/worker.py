#!/usr/bin/env python3
"""
worker.py — Proton Bridge IMAP → generic HTTP API mail worker

Polls Proton Mail Bridge IMAP for messages matching IMAP_SEARCH, parses each
one, and POSTs it to a generic REST API. Sends a heartbeat after every cycle.

Configuration (environment variables or .env file):
    IMAP_HOST           Proton Bridge host          (default: proton-bridge)
    IMAP_PORT           socat proxy port            (default: 2143)
    IMAP_USER           Bridge login — your Proton address
    IMAP_PASS           Bridge-generated password
    IMAP_MAILBOX        Mailbox to poll             (default: INBOX)
    IMAP_SEARCH         IMAP search criteria        (default: UNSEEN)
    MARK_SEEN           Mark imported messages Seen (default: true)
    API_BASE_URL        REST API base, no trailing slash
    API_TOKEN           Bearer token
    POLL_INTERVAL       Seconds between cycles      (default: 300)
    MESSAGE_CAP         Max messages per cycle; 0=all (default: 50)
    WORKER_VERSION      Reported in heartbeat       (default: 1.0.0)
    WORKER_HTTP_PORT    Port for the trigger HTTP server (default: 9090)
"""

import email
import email.header
import email.message
import email.utils
import hashlib
import imaplib
import json
import logging
import os
import re
import signal
import ssl
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import List, Optional, Tuple

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Logging — set up before config so validation errors are formatted
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("worker")


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        log.error("Missing required environment variable: %s", name)
        sys.exit(1)
    return val


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        log.error("Environment variable %s must be an integer, got: %r", name, raw)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
IMAP_HOST      = os.environ.get("IMAP_HOST", "proton-bridge")
IMAP_PORT      = _int_env("IMAP_PORT", 2143)
IMAP_USER      = _require("IMAP_USER")
IMAP_PASS      = _require("IMAP_PASS")
IMAP_MAILBOX   = os.environ.get("IMAP_MAILBOX", "INBOX")
IMAP_SEARCH    = os.environ.get("IMAP_SEARCH", "UNSEEN")
MARK_SEEN      = os.environ.get("MARK_SEEN", "true").lower() in ("1", "true", "yes")
API_BASE_URL   = os.environ.get("API_BASE_URL", "").rstrip("/")
API_TOKEN      = _require("API_TOKEN")
POLL_INTERVAL     = _int_env("POLL_INTERVAL", 300)
MESSAGE_CAP       = _int_env("MESSAGE_CAP", 50)
WORKER_VERSION    = os.environ.get("WORKER_VERSION", "1.0.0")
WORKER_HTTP_PORT  = _int_env("WORKER_HTTP_PORT", 8080)

if not API_BASE_URL:
    log.error("Missing required environment variable: API_BASE_URL")
    sys.exit(1)

log.info("worker started")
log.info("config loaded")
log.info("poll interval: %s", POLL_INTERVAL)
log.info("message cap: %s", MESSAGE_CAP)
log.info("trigger endpoint: POST http://0.0.0.0:%s/poll", WORKER_HTTP_PORT)


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_running = True


def _stop(signum, frame):
    global _running
    log.info("Signal %s received — shutting down after current cycle.", signum)
    _running = False


signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)

# Set by the HTTP trigger endpoint to wake the sleep loop immediately.
_force_poll = threading.Event()

# Held during an active poll cycle. Prevents concurrent IMAP runs when
# /poll is called while a cycle is already in progress.
_poll_lock = threading.Lock()


# ---------------------------------------------------------------------------
# HTTP trigger server
# ---------------------------------------------------------------------------
class _TriggerHandler(BaseHTTPRequestHandler):
    """Minimal HTTP server exposing a single trigger endpoint."""

    def do_POST(self):
        if self.path != "/poll":
            self._respond(404, {"error": "not found"})
            return
        auth = self.headers.get("Authorization", "")
        if auth != f"Bearer {API_TOKEN}":
            self._respond(401, {"error": "unauthorized"})
            return
        _force_poll.set()
        if _poll_lock.locked():
            log.info("Force poll requested via HTTP (queued — poll already running)")
            self._respond(202, {"ok": True, "message": "poll queued — will run after current cycle"})
        else:
            log.info("Force poll requested via HTTP")
            self._respond(202, {"ok": True, "message": "poll triggered"})

    def do_GET(self):
        if self.path == "/health":
            self._respond(200, {"ok": True, "worker_version": WORKER_VERSION})
        else:
            self._respond(404, {"error": "not found"})

    def _respond(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        log.debug("HTTP %s", fmt % args)


def _start_trigger_server() -> None:
    server = HTTPServer(("0.0.0.0", WORKER_HTTP_PORT), _TriggerHandler)
    log.info("Trigger server listening on :%s", WORKER_HTTP_PORT)
    server.serve_forever()


# ---------------------------------------------------------------------------
# IMAP helpers
# ---------------------------------------------------------------------------
def _tls_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _connect() -> imaplib.IMAP4:
    conn = imaplib.IMAP4(IMAP_HOST, IMAP_PORT)
    try:
        conn.starttls(ssl_context=_tls_context())
    except imaplib.IMAP4.error as exc:
        log.warning("STARTTLS unavailable (%s) — using plain connection.", exc)
    conn.login(IMAP_USER, IMAP_PASS)
    log.info("imap connected")
    return conn


def _get_uidvalidity(conn: imaplib.IMAP4) -> Optional[str]:
    try:
        _, data = conn.status(f'"{IMAP_MAILBOX}"', "(UIDVALIDITY)")
        if data and data[0]:
            m = re.search(r"UIDVALIDITY (\d+)", data[0].decode(errors="replace"))
            if m:
                return m.group(1)
    except Exception as exc:
        log.warning("Could not retrieve UIDVALIDITY: %s", exc)
    return None


def _dedupe_key(folder: str, uidvalidity: Optional[str], uid: int) -> Optional[str]:
    if not uidvalidity:
        return None
    raw = f"{folder.lower()}|{uidvalidity}|{uid}"
    return hashlib.sha1(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Email parsing helpers
# ---------------------------------------------------------------------------
def _decode_header(value: Optional[str]) -> str:
    if not value:
        return ""
    parts = email.header.decode_header(value)
    out = []
    for raw, charset in parts:
        if isinstance(raw, bytes):
            out.append(raw.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(str(raw))
    return "".join(out)


def _norm_msgid(value: Optional[str]) -> str:
    """Lowercase, strip whitespace and angle brackets from a message ID."""
    if not value:
        return ""
    return value.strip().strip("<>").lower()


def _norm_references(value: Optional[str]) -> List[str]:
    """Split References header into a list of normalized IDs."""
    if not value:
        return []
    return [_norm_msgid(ref) for ref in re.split(r"\s+", value.strip()) if ref]


def _first_email(header_value: Optional[str]) -> str:
    if not header_value:
        return ""
    pairs = email.utils.getaddresses([header_value])
    return pairs[0][1] if pairs else ""


def _parse_date(msg: email.message.Message) -> str:
    raw = msg.get("Date", "")
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def _extract_bodies(msg: email.message.Message) -> Tuple[str, str]:
    plain: List[str] = []
    html:  List[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if "attachment" in part.get("Content-Disposition", ""):
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if ct == "text/plain":
                plain.append(text)
            elif ct == "text/html":
                html.append(text)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                html.append(text)
            else:
                plain.append(text)
    return "\n".join(plain), "\n".join(html)


def _raw_headers(msg: email.message.Message) -> str:
    return "".join(f"{k}: {v}\r\n" for k, v in msg.items())


def _build_payload(raw_bytes: bytes, uid: int, uidvalidity: Optional[str]) -> dict:
    msg = email.message_from_bytes(raw_bytes)
    plain, html = _extract_bodies(msg)
    from_name, from_email = email.utils.parseaddr(_decode_header(msg.get("From")))
    return {
        "message_id":       _norm_msgid(msg.get("Message-ID")),
        "in_reply_to":      _norm_msgid(msg.get("In-Reply-To")) or None,
        "references":       _norm_references(msg.get("References")),
        "imap_folder":      IMAP_MAILBOX,
        "imap_uidvalidity": uidvalidity,
        "imap_uid":         uid,
        "imap_dedupe_key":  _dedupe_key(IMAP_MAILBOX, uidvalidity, uid),
        "from_email":       from_email,
        "from_name":        from_name,
        "to_email":         _first_email(msg.get("To")),
        "subject":          _decode_header(msg.get("Subject")),
        "date":             _parse_date(msg),
        "body_text":        plain,
        "body_html":        html,
        "raw_headers":      _raw_headers(msg),
    }


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------
def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {API_TOKEN}", "Content-Type": "application/json"}


def _health_check() -> bool:
    url = f"{API_BASE_URL}/health"
    try:
        resp = requests.get(url, headers=_auth_headers(), timeout=10)
        if resp.status_code == 200:
            log.info("api health ok")
            return True
        log.warning("Health check %s → HTTP %s", url, resp.status_code)
        return False
    except requests.RequestException as exc:
        log.warning("Health check failed: %s", exc)
        return False


def _import_message(payload: dict) -> str:
    """
    POST one message to the import endpoint.
    Returns 'imported' | 'skipped_duplicate' | 'error'.
    """
    url = f"{API_BASE_URL}/messages/import"
    try:
        resp = requests.post(url, json=payload, headers=_auth_headers(), timeout=15)
        if resp.status_code in (200, 201):
            status = resp.json().get("status", "")
            if status in ("imported", "skipped_duplicate"):
                log.info("%s  message_id=%s  subject=%r", status, payload["message_id"], payload["subject"])
                return status
            log.warning("Unexpected API status %r for message_id=%s", status, payload["message_id"])
            return "error"
        if resp.status_code in (400, 401):
            log.error("POST %s → HTTP %s (not retrying): %s", url, resp.status_code, resp.text[:200])
            return "error"
        log.warning("POST %s → HTTP %s (will retry): %s", url, resp.status_code, resp.text[:200])
        return "error"
    except requests.Timeout:
        log.warning("POST %s timed out (will retry)", url)
        return "error"
    except requests.RequestException as exc:
        log.error("POST %s failed: %s", url, exc)
        return "error"


def _send_status(
    last_poll_status: str,
    imported: int,
    skipped: int,
    errors: int,
    last_error: Optional[str],
) -> None:
    url = f"{API_BASE_URL}/worker/status"
    body = {
        "worker_version":   WORKER_VERSION,
        "heartbeat_at":     datetime.now(timezone.utc).isoformat(),
        "last_poll_status": last_poll_status,
        "imported":         imported,
        "skipped":          skipped,
        "errors":           errors,
        "last_error":       last_error,
    }
    try:
        resp = requests.post(url, json=body, headers=_auth_headers(), timeout=10)
        if resp.status_code not in (200, 201):
            log.warning("Status POST %s → HTTP %s", url, resp.status_code)
    except requests.RequestException as exc:
        log.warning("Status POST failed: %s", exc)


# ---------------------------------------------------------------------------
# Poll cycle
# ---------------------------------------------------------------------------
def poll_once() -> Tuple[int, int, int, Optional[str]]:
    """
    Connect, fetch up to MESSAGE_CAP matching messages, import each one.
    Returns (imported, skipped, errors, last_error).
    """
    imported = skipped = errors = 0
    last_error: Optional[str] = None

    conn = _connect()
    try:
        conn.select(IMAP_MAILBOX)
        log.info("mailbox selected: %s", IMAP_MAILBOX)

        uidvalidity = _get_uidvalidity(conn)

        # UID SEARCH returns UIDs, not sequence numbers
        _, data = conn.uid("SEARCH", None, IMAP_SEARCH)
        all_uids: List[bytes] = data[0].split()

        if not all_uids:
            log.info("no new messages")
            return 0, 0, 0, None

        total = len(all_uids)
        batch = all_uids[:MESSAGE_CAP] if MESSAGE_CAP > 0 else all_uids
        log.info("found %d messages", total)
        log.info("processing %d messages", len(batch))

        uid_str = b",".join(batch)
        _, fetch_data = conn.uid("FETCH", uid_str, "(RFC822)")

        # Match fetch responses to UIDs by position — Proton Bridge does not
        # echo UID back in the FETCH response body even when requested.
        message_tuples = [item for item in (fetch_data or []) if isinstance(item, tuple)]

        for uid_bytes, item in zip(batch, message_tuples):
            uid_int = int(uid_bytes)

            try:
                payload = _build_payload(item[1], uid_int, uidvalidity)
            except Exception as exc:
                log.error("Failed to parse message UID %s: %s", uid_int, exc)
                errors += 1
                last_error = str(exc)
                continue

            result = _import_message(payload)

            if result in ("imported", "skipped_duplicate"):
                if result == "imported":
                    imported += 1
                else:
                    skipped += 1
                if MARK_SEEN:
                    conn.uid("STORE", str(uid_int).encode(), "+FLAGS", "\\Seen")
            else:
                errors += 1
                last_error = f"import_error uid={uid_int}"

    finally:
        try:
            conn.logout()
        except Exception:
            pass

    return imported, skipped, errors, last_error


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main() -> None:
    threading.Thread(target=_start_trigger_server, daemon=True).start()

    while _running:
        # Consume any trigger that arrived during the previous cycle before
        # starting a new one — prevents a stale event from skipping the sleep.
        _force_poll.clear()

        poll_status = "ok"
        imported = skipped = errors = 0
        last_error: Optional[str] = None

        # 1. Health check
        if not _health_check():
            log.warning("API unavailable — skipping poll cycle.")
            _send_status("api_unavailable", 0, 0, 0, "health_check_failed")
        else:
            # 2–4. IMAP → search → process
            # _poll_lock prevents a concurrent cycle if /poll fires mid-run.
            with _poll_lock:
                try:
                    imported, skipped, errors, last_error = poll_once()
                    if errors:
                        poll_status = "partial_error"
                except imaplib.IMAP4.error as exc:
                    log.error("IMAP error: %s", exc)
                    poll_status = "imap_error"
                    last_error = str(exc)
                except Exception as exc:
                    log.error("Poll cycle failed: %s", exc)
                    poll_status = "error"
                    last_error = str(exc)

            # 5. Heartbeat
            _send_status(poll_status, imported, skipped, errors, last_error)

        # 6. Sleep until interval elapses, SIGTERM arrives, or a force-poll fires.
        # _force_poll.wait() returns immediately if the event was set during the
        # poll cycle above, so a queued /poll is never silently dropped.
        for _ in range(POLL_INTERVAL):
            if not _running:
                break
            if _force_poll.wait(timeout=1.0):
                break

    log.info("worker stopped")


if __name__ == "__main__":
    main()
