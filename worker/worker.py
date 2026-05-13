#!/usr/bin/env python3
"""
worker.py — Proton Bridge → WordPress REST API mail worker

Polls Proton Mail Bridge IMAP for UNSEEN messages, parses them, and POSTs
each one to a WordPress custom REST API endpoint. Sends periodic heartbeats
so the WordPress side can detect stalls.

Configuration (environment variables or .env file):
    IMAP_HOST           Proton Bridge host (default: proton-bridge)
    IMAP_PORT           socat proxy port  (default: 2143)
    IMAP_USER           Bridge login — your Proton address
    IMAP_PASS           Bridge-generated password (NOT your Proton password)
    IMAP_MAILBOX        Mailbox to poll (default: INBOX)
    IMAP_FILTER_TO      Comma-separated addresses — only messages where any
                        of them appears in To or Cc.  Leave empty for all.
    IMAP_MARK_SEEN      Mark imported messages as \\Seen (default: true)
    WP_URL              WordPress base URL, e.g. https://example.com
    WP_TOKEN            Bearer token for biopentra-support REST API
    POLL_INTERVAL       Seconds between poll cycles (default: 60)
    MESSAGE_CAP         Max messages to import per cycle; 0 = all (default: 20)
"""

import email
import email.header
import email.message
import email.utils
import imaplib
import logging
import os
import signal
import ssl
import sys
import time
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
IMAP_HOST     = os.environ.get("IMAP_HOST", "proton-bridge")
IMAP_PORT     = int(os.environ.get("IMAP_PORT", "2143"))
IMAP_USER     = os.environ.get("IMAP_USER", "")
IMAP_PASS     = os.environ.get("IMAP_PASS", "")
IMAP_MAILBOX  = os.environ.get("IMAP_MAILBOX", "INBOX")
IMAP_FILTER   = [a.strip() for a in os.environ.get("IMAP_FILTER_TO", "").split(",") if a.strip()]
MARK_SEEN     = os.environ.get("IMAP_MARK_SEEN", "true").lower() in ("1", "true", "yes")
WP_URL        = os.environ.get("WP_URL", "").rstrip("/")
WP_TOKEN      = os.environ.get("WP_TOKEN", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
MESSAGE_CAP   = int(os.environ.get("MESSAGE_CAP", "20"))

BASE_API      = f"{WP_URL}/wp-json/biopentra-support/v1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("worker")


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


# ---------------------------------------------------------------------------
# IMAP helpers
# ---------------------------------------------------------------------------

def _tls_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _connect() -> imaplib.IMAP4:
    log.debug("Connecting to %s:%s", IMAP_HOST, IMAP_PORT)
    conn = imaplib.IMAP4(IMAP_HOST, IMAP_PORT)
    try:
        conn.starttls(ssl_context=_tls_context())
    except imaplib.IMAP4.error as exc:
        log.warning("STARTTLS unavailable (%s) — using plain connection.", exc)
    conn.login(IMAP_USER, IMAP_PASS)
    return conn


def _recipient_criteria(addresses: List[str]) -> str:
    """Build a nested IMAP OR search for multiple To/Cc addresses."""
    clauses = [f'(OR TO "{a}" CC "{a}")' for a in addresses]
    while len(clauses) > 1:
        paired = []
        for i in range(0, len(clauses), 2):
            if i + 1 < len(clauses):
                paired.append(f"(OR {clauses[i]} {clauses[i + 1]})")
            else:
                paired.append(clauses[i])
        clauses = paired
    return clauses[0]


def _search_unseen(conn: imaplib.IMAP4) -> List[bytes]:
    if IMAP_FILTER:
        recipient_clause = _recipient_criteria(IMAP_FILTER)
        criteria = f"UNSEEN {recipient_clause}"
    else:
        criteria = "UNSEEN"
    _, data = conn.search(None, criteria)
    ids = data[0].split()
    if MESSAGE_CAP > 0:
        ids = ids[:MESSAGE_CAP]
    return ids


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


def _parse_date(msg: email.message.Message) -> str:
    raw = msg.get("Date", "")
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def _address_list(header_value: Optional[str]) -> List[str]:
    if not header_value:
        return []
    return [f"{name} <{addr}>".strip() if name else addr
            for name, addr in email.utils.getaddresses([header_value])]


def _extract_bodies(msg: email.message.Message) -> Tuple[str, str]:
    """Return (plain_text, html) extracted from a (potentially MIME) message."""
    plain, html = [], []
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = part.get("Content-Disposition", "")
            if "attachment" in cd:
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


def _build_payload(raw_bytes: bytes) -> dict:
    msg = email.message_from_bytes(raw_bytes)
    plain, html = _extract_bodies(msg)
    from_pairs = email.utils.parseaddr(_decode_header(msg.get("From")))
    return {
        "message_id":  msg.get("Message-ID", ""),
        "in_reply_to": msg.get("In-Reply-To", ""),
        "references":  msg.get("References", ""),
        "from_email":  from_pairs[1],
        "from_name":   from_pairs[0],
        "to":          _address_list(msg.get("To")),
        "cc":          _address_list(msg.get("Cc")),
        "subject":     _decode_header(msg.get("Subject")),
        "body_text":   plain,
        "body_html":   html,
        "date":        _parse_date(msg),
    }


# ---------------------------------------------------------------------------
# WordPress REST API helpers
# ---------------------------------------------------------------------------

def _headers() -> dict:
    return {"Authorization": f"Bearer {WP_TOKEN}", "Content-Type": "application/json"}


def _post_message(payload: dict) -> bool:
    url = f"{BASE_API}/messages/import"
    try:
        resp = requests.post(url, json=payload, headers=_headers(), timeout=15)
        if resp.status_code in (200, 201):
            log.info("Imported message_id=%s subject=%r", payload["message_id"], payload["subject"])
            return True
        log.warning("POST %s → HTTP %s: %s", url, resp.status_code, resp.text[:200])
        return False
    except requests.RequestException as exc:
        log.error("POST %s failed: %s", url, exc)
        return False


def _send_heartbeat(ok: bool, imported: int, errors: int) -> None:
    url = f"{BASE_API}/worker/status"
    body = {
        "status":   "ok" if ok else "error",
        "imported": imported,
        "errors":   errors,
        "ts":       datetime.now(timezone.utc).isoformat(),
    }
    try:
        resp = requests.post(url, json=body, headers=_headers(), timeout=10)
        if resp.status_code not in (200, 201):
            log.warning("Heartbeat %s → HTTP %s", url, resp.status_code)
    except requests.RequestException as exc:
        log.warning("Heartbeat failed: %s", exc)


# ---------------------------------------------------------------------------
# Poll cycle
# ---------------------------------------------------------------------------

def poll_once() -> Tuple[int, int]:
    """Connect, import UNSEEN messages, return (imported, errors)."""
    imported, errors = 0, 0
    conn = _connect()
    try:
        conn.select(IMAP_MAILBOX)
        ids = _search_unseen(conn)
        if not ids:
            log.info("No new messages.")
            return 0, 0

        log.info("Found %d unseen message(s).", len(ids))
        id_str = b",".join(ids)
        _, data = conn.fetch(id_str, "(RFC822)")

        for item in data or []:
            if not isinstance(item, tuple):
                continue
            try:
                payload = _build_payload(item[1])
            except Exception as exc:
                log.error("Failed to parse message: %s", exc)
                errors += 1
                continue

            success = _post_message(payload)
            if success:
                imported += 1
                if MARK_SEEN:
                    # Extract the sequence number from the fetch response header
                    # item[0] is e.g. b"1 (RFC822 {12345})"
                    seq = item[0].split()[0]
                    conn.store(seq, "+FLAGS", "\\Seen")
            else:
                errors += 1
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    return imported, errors


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    missing = [v for v in ("IMAP_USER", "IMAP_PASS", "WP_URL", "WP_TOKEN") if not os.environ.get(v)]
    if missing:
        log.error("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)

    log.info(
        "Worker started. IMAP=%s:%s mailbox=%s filter=%s poll=%ss cap=%s mark_seen=%s",
        IMAP_HOST, IMAP_PORT, IMAP_MAILBOX,
        IMAP_FILTER or "ALL", POLL_INTERVAL, MESSAGE_CAP or "unlimited", MARK_SEEN,
    )

    while _running:
        cycle_ok = True
        imported = errors = 0
        try:
            imported, errors = poll_once()
        except Exception as exc:
            log.error("Poll cycle failed: %s", exc)
            cycle_ok = False
            errors = 1

        _send_heartbeat(cycle_ok and errors == 0, imported, errors)

        # Sleep in short chunks so SIGTERM is handled promptly.
        for _ in range(POLL_INTERVAL):
            if not _running:
                break
            time.sleep(1)

    log.info("Worker stopped.")


if __name__ == "__main__":
    main()
