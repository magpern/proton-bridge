#!/usr/bin/env python3
"""
imap_test.py — Proton Mail Bridge IMAP test client

Connects to the Bridge container, lists mailboxes, searches INBOX, and prints
messages sorted by date (newest first).

Configuration (environment variables or .env file):
    BRIDGE_IMAP_HOST        default: proton-bridge
    BRIDGE_IMAP_PORT        default: 2143
    BRIDGE_IMAP_USER        Bridge login (usually your Proton address)
    BRIDGE_IMAP_PASS        Bridge-generated password
    BRIDGE_IMAP_SSL         "true" to use IMAP over SSL (port 993)
    BRIDGE_SKIP_TLS_VERIFY  "true" to accept Bridge's self-signed certificate
    BRIDGE_FILTER_TO        show only messages where this address appears in
                            To or Cc (substring match, case-insensitive)
    BRIDGE_FETCH_LIMIT      max messages to display; 0 = all (default: 0)
"""

import email
import email.header
import email.message
import email.utils
import imaplib
import os
import ssl
import sys
from datetime import datetime, timezone
from typing import List, Optional, Tuple

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BRIDGE_HOST  = os.environ.get("BRIDGE_IMAP_HOST", "proton-bridge")
BRIDGE_PORT  = int(os.environ.get("BRIDGE_IMAP_PORT", "2143"))
BRIDGE_USER  = os.environ.get("BRIDGE_IMAP_USER", "")
BRIDGE_PASS  = os.environ.get("BRIDGE_IMAP_PASS", "")
USE_SSL      = os.environ.get("BRIDGE_IMAP_SSL", "").lower() in ("1", "true", "yes")
SKIP_TLS     = os.environ.get("BRIDGE_SKIP_TLS_VERIFY", "true").lower() in ("1", "true", "yes")
FILTER_TO    = os.environ.get("BRIDGE_FILTER_TO", "").strip()
FETCH_LIMIT  = int(os.environ.get("BRIDGE_FETCH_LIMIT", "0"))  # 0 = all


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def die(msg: str) -> None:
    print(f"\nERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def decode_header(value: Optional[str]) -> str:
    if not value:
        return ""
    parts = email.header.decode_header(value)
    decoded = []
    for raw, charset in parts:
        if isinstance(raw, bytes):
            decoded.append(raw.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(str(raw))
    return "".join(decoded)


def parse_date(msg: email.message.Message) -> datetime:
    raw = msg.get("Date", "")
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        # Normalise to UTC so mixed-timezone messages sort correctly.
        return dt.astimezone(timezone.utc)
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def tls_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if SKIP_TLS:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def connect() -> imaplib.IMAP4:
    if USE_SSL:
        print(f"Connecting (SSL) to {BRIDGE_HOST}:{BRIDGE_PORT} ...")
        return imaplib.IMAP4_SSL(BRIDGE_HOST, BRIDGE_PORT, ssl_context=tls_context())

    print(f"Connecting to {BRIDGE_HOST}:{BRIDGE_PORT} ...")
    conn = imaplib.IMAP4(BRIDGE_HOST, BRIDGE_PORT)
    try:
        conn.starttls(ssl_context=tls_context())
        print("STARTTLS negotiated.")
    except imaplib.IMAP4.error as exc:
        print(f"STARTTLS not available ({exc}), using plain.")
    return conn


def fetch_headers(mail: imaplib.IMAP4, msg_ids: List[bytes]) -> List[email.message.Message]:
    """Fetch RFC822 headers for a list of message IDs in one round trip."""
    if not msg_ids:
        return []
    id_str = b",".join(msg_ids)
    _, data = mail.fetch(id_str, "(RFC822.HEADER)")
    messages = []
    for item in data or []:
        if isinstance(item, tuple):
            messages.append(email.message_from_bytes(item[1]))
    return messages


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if not BRIDGE_USER or not BRIDGE_PASS:
        die(
            "BRIDGE_IMAP_USER and BRIDGE_IMAP_PASS are not set.\n\n"
            "  1. docker compose stop proton-bridge\n"
            "  2. docker compose run --rm -it proton-bridge --cli\n"
            "     > info\n"
            "  3. Copy credentials into .env\n"
            "  4. docker compose up -d\n"
        )

    conn = connect()
    print(f"Logging in as {BRIDGE_USER} ...")
    conn.login(BRIDGE_USER, BRIDGE_PASS)
    print("Login successful.\n")

    # -- Mailboxes ------------------------------------------------------------
    _, mailboxes = conn.list()
    print("Mailboxes:")
    for mb in mailboxes or []:
        print(f"  {mb.decode(errors='replace')}")

    # -- Select INBOX ---------------------------------------------------------
    _, count_data = conn.select("INBOX")
    total = int(count_data[0]) if count_data and count_data[0] else 0
    print(f"\nINBOX: {total} message(s) total.\n")

    if total == 0:
        print("No messages.")
        conn.logout()
        return

    # -- Search ---------------------------------------------------------------
    if FILTER_TO:
        criteria = f'(OR TO "{FILTER_TO}" CC "{FILTER_TO}")'
        print(f"Filter: recipient contains '{FILTER_TO}'")
    else:
        criteria = "ALL"

    _, search_data = conn.search(None, criteria)
    all_ids: List[bytes] = search_data[0].split()

    if not all_ids:
        print("No messages match the filter.")
        conn.logout()
        return

    print(f"Matched: {len(all_ids)} message(s). Fetching headers...\n")

    # -- Fetch all headers in one round trip ----------------------------------
    messages = fetch_headers(conn, all_ids)
    conn.logout()

    # -- Sort by date, newest first -------------------------------------------
    messages.sort(key=parse_date, reverse=True)

    # -- Apply display limit --------------------------------------------------
    display = messages if FETCH_LIMIT == 0 else messages[:FETCH_LIMIT]

    divider = "─" * 64
    label = f"to/cc '{FILTER_TO}'" if FILTER_TO else "in INBOX"
    limit_note = f" (showing {len(display)})" if FETCH_LIMIT and len(messages) > FETCH_LIMIT else ""
    print(divider)
    print(f"  {len(messages)} message(s) {label}{limit_note} — newest first")
    print(divider)

    for msg in display:
        print(f"From   : {decode_header(msg.get('From'))}")
        print(f"To     : {decode_header(msg.get('To'))}")
        print(f"Subject: {decode_header(msg.get('Subject'))}")
        print(f"Date   : {msg.get('Date', '—')}")
        print(divider)

    print("\nDone.")


if __name__ == "__main__":
    main()
