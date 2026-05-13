# Mail Worker — Technical Reference

The `mail-worker` is a Python daemon that bridges Proton Mail Bridge IMAP to a generic HTTP REST API. It runs in a Docker container on the same private network as `proton-bridge`, polls for new messages on a configurable interval, and POSTs each parsed message to an external API.

---

## Environment variables

| Variable | Type | Required | Default | Description |
|---|---|---|---|---|
| `IMAP_HOST` | string | | `proton-bridge` | Hostname of the Bridge container |
| `IMAP_PORT` | integer | | `2143` | socat proxy port (Bridge listens on 1143; socat re-exposes on 2143) |
| `IMAP_USER` | string | ✓ | | Proton address used as IMAP login |
| `IMAP_PASS` | string | ✓ | | Bridge-generated password (not the Proton account password) |
| `IMAP_MAILBOX` | string | | `INBOX` | Mailbox to poll |
| `IMAP_SEARCH` | string | | `UNSEEN` | Any valid IMAP SEARCH expression, e.g. `UNSEEN TO "support@example.com"` |
| `MARK_SEEN` | bool | | `true` | Mark message `\Seen` after a successful import or duplicate response |
| `API_BASE_URL` | string | ✓ | | Base URL of the REST API, no trailing slash |
| `API_TOKEN` | string | ✓ | | Bearer token sent in every API request |
| `POLL_INTERVAL` | integer | | `300` | Seconds between poll cycles |
| `MESSAGE_CAP` | integer | | `50` | Maximum messages processed per cycle; `0` = unlimited |
| `WORKER_VERSION` | string | | `1.0.0` | Version string reported in heartbeat |

Credentials (`IMAP_PASS`, `API_TOKEN`) are never written to logs.

Startup exits with code 1 if any required variable is missing or if `IMAP_PORT`, `POLL_INTERVAL`, or `MESSAGE_CAP` is not a valid integer.

---

## Startup sequence

```
1. Configure logging
2. Read and validate environment variables → exit 1 on failure
3. Log:
     worker started
     config loaded
     poll interval: <n>
     message cap: <n>
4. Install SIGTERM / SIGINT handlers for graceful shutdown
5. Enter main loop
```

---

## Main loop

Each iteration:

```
1. GET  {API_BASE_URL}/health
       → failure: log warning, send status (api_unavailable), sleep, repeat
       → success: continue

2. Connect to IMAP (STARTTLS, self-signed cert accepted)
3. SELECT {IMAP_MAILBOX}
4. UID SEARCH {IMAP_SEARCH}  →  collect UIDs
5. Slice to MESSAGE_CAP
6. UID FETCH <uid-list> (RFC822)
7. For each message:
     a. Parse MIME
     b. POST {API_BASE_URL}/messages/import
     c. On success (imported / skipped_duplicate): optionally UID STORE \Seen
     d. On failure: leave unseen, increment error counter
8. POST {API_BASE_URL}/worker/status  (heartbeat)
9. Logout IMAP
10. Sleep POLL_INTERVAL seconds
    (sleep is broken into 1-second ticks so SIGTERM is handled promptly)
```

IMAP errors, network errors, and unhandled exceptions are caught per cycle. The worker logs the error and continues to the next cycle rather than exiting.

---

## IMAP behaviour

### Transport

- Protocol: IMAP4 with STARTTLS (not IMAP-over-SSL)
- Port: `IMAP_PORT` (default 2143, socat proxy inside `proton-bridge`)
- TLS: self-signed certificate accepted (`check_hostname=False`, `verify_mode=CERT_NONE`)

### UID-based operations

All IMAP operations use the `UID` command prefix so message references survive `EXPUNGE` and reconnection:

| Operation | Command |
|---|---|
| Search | `UID SEARCH {IMAP_SEARCH}` |
| Fetch | `UID FETCH {uid-list} (RFC822)` |
| Mark seen | `UID STORE {uid} +FLAGS \Seen` |

Proton Bridge does not echo `UID` back in the `FETCH` response body. UIDs are tracked from the `SEARCH` result and matched to fetch responses by position.

### UIDVALIDITY

Retrieved once per cycle via `STATUS "{IMAP_MAILBOX}" (UIDVALIDITY)` immediately after `SELECT`. Used to compute `imap_dedupe_key`. If unavailable, `imap_dedupe_key` is `null`.

---

## Message parsing

### Dedupe key

```
imap_dedupe_key = sha1( lower(imap_folder) + "|" + imap_uidvalidity + "|" + imap_uid )
```

Encoded as a lowercase hex string. Null when `imap_uidvalidity` is missing.

### Header normalisation

All three message-ID fields are normalised the same way:
- Strip surrounding whitespace
- Strip surrounding `< >`
- Convert to lowercase

```python
# e.g.  "<ABC@proton.me>"  →  "abc@proton.me"
```

`references` is split on whitespace and each entry is normalised individually, producing a list.

### Body extraction

MIME parts are walked recursively. Parts with a `Content-Disposition: attachment` header are skipped (phase 1 — attachments not supported). `text/plain` parts are joined into `body_text`; `text/html` parts into `body_html`. For non-multipart messages the single payload is placed in whichever field matches its content type.

### Parsed fields

| Field | Source | Type | Notes |
|---|---|---|---|
| `message_id` | `Message-ID` header | string | Normalised |
| `in_reply_to` | `In-Reply-To` header | string \| null | Normalised; null if absent |
| `references` | `References` header | string[] | Normalised list |
| `imap_folder` | `IMAP_MAILBOX` env var | string | |
| `imap_uidvalidity` | IMAP STATUS | string \| null | |
| `imap_uid` | IMAP UID SEARCH result | integer | |
| `imap_dedupe_key` | computed | string \| null | sha1 hex |
| `from_email` | `From` header | string | RFC 5322 address |
| `from_name` | `From` header | string | Display name |
| `to_email` | `To` header | string | First recipient address only |
| `subject` | `Subject` header | string | RFC 2047 decoded |
| `date` | `Date` header | string | ISO 8601, UTC |
| `body_text` | MIME body | string | |
| `body_html` | MIME body | string | |
| `raw_headers` | All headers | string | `Key: Value\r\n` per line |

Malformed messages do not crash the worker. A parse exception increments the error counter and skips to the next message.

---

## API endpoints

### `GET {API_BASE_URL}/health`

**Purpose:** Verify the API is reachable before starting a poll cycle.

**Request:** No body. `Authorization: Bearer {API_TOKEN}` header is sent.

**Expected response — HTTP 200:**
```json
{
  "ok": true,
  "plugin": "Biopentra Support Desk",
  "version": "2.0.0",
  "time": "2026-05-13T12:00:00Z"
}
```

**Worker behaviour:**

| Response | Action |
|---|---|
| HTTP 200 | Log `api health ok`, continue cycle |
| Any other status | Log warning, send status `api_unavailable`, skip IMAP, sleep |
| Network error / timeout | Same as non-200 |

---

### `POST {API_BASE_URL}/messages/import`

**Purpose:** Deliver one parsed message to the API.

**Request headers:**
```http
Authorization: Bearer {API_TOKEN}
Content-Type: application/json
```

**Request body:**
```json
{
  "message_id":       "abc123@proton.me",
  "in_reply_to":      "parent@example.com",
  "references":       ["root@example.com", "parent@example.com"],
  "imap_folder":      "INBOX",
  "imap_uidvalidity": "123456789",
  "imap_uid":         42,
  "imap_dedupe_key":  "da39a3ee5e6b4b0d3255bfef95601890afd80709",
  "from_email":       "customer@example.com",
  "from_name":        "Customer Name",
  "to_email":         "support@example.com",
  "subject":          "Question about order",
  "date":             "2026-05-13T12:00:00+00:00",
  "body_text":        "Plain text body",
  "body_html":        "<p>HTML body</p>",
  "raw_headers":      "From: Customer Name <customer@example.com>\r\nTo: ...\r\n"
}
```

**Expected responses:**

```json
{ "status": "imported",          "ticket_id": 123, "message_id": "abc123@proton.me" }
{ "status": "skipped_duplicate", "reason": "message_id" }
```

**Worker behaviour per response:**

| Condition | Mark seen | Counter |
|---|---|---|
| HTTP 2xx, `status=imported` | Yes (if `MARK_SEEN=true`) | `imported++` |
| HTTP 2xx, `status=skipped_duplicate` | Yes (if `MARK_SEEN=true`) | `skipped++` |
| HTTP 2xx, unexpected `status` | No | `errors++` |
| HTTP 400 / 401 | No | `errors++` (logged as non-retryable) |
| HTTP 500 | No | `errors++` (logged as retryable) |
| Timeout | No | `errors++` |
| Network error | No | `errors++` |

`400` and `401` are logged at ERROR level and not retried in the same cycle. `500` and timeouts are logged at WARNING level and will be retried on the next cycle (message remains unseen).

---

### `POST {API_BASE_URL}/worker/status`

**Purpose:** Heartbeat. Sent once after every poll cycle, including failed ones.

**Request headers:**
```http
Authorization: Bearer {API_TOKEN}
Content-Type: application/json
```

**Request body:**
```json
{
  "worker_version":   "1.0.0",
  "heartbeat_at":     "2026-05-13T12:01:00+00:00",
  "last_poll_status": "ok",
  "imported":         5,
  "skipped":          2,
  "errors":           0,
  "last_error":       null
}
```

**`last_poll_status` values:**

| Value | Meaning |
|---|---|
| `ok` | Cycle completed with no errors |
| `partial_error` | Cycle completed but one or more messages failed |
| `imap_error` | IMAP connection or auth failure |
| `error` | Unhandled exception in poll cycle |
| `api_unavailable` | Health check failed — IMAP was not attempted |

**Expected response — HTTP 200:**
```json
{ "ok": true }
```

A failed heartbeat is logged at WARNING level. The worker continues regardless.

---

## Mark-seen rules

`\Seen` is set via `UID STORE` only when **all** of the following are true:

1. `MARK_SEEN=true`
2. The API returned HTTP 2xx
3. The response body contains `"status": "imported"` or `"status": "skipped_duplicate"`

`\Seen` is **not** set after:
- Parse failure
- API 4xx / 5xx
- Timeout or network error
- Unexpected API status string

---

## Docker

```dockerfile
FROM python:3.13-slim
RUN useradd -r -s /bin/false worker   # non-root
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY worker.py .
USER worker
CMD ["python", "worker.py"]
```

**Networks required:**

| Network | Purpose |
|---|---|
| `bridge-net` | Reach `proton-bridge:2143` (IMAP) |
| API network | Reach `API_BASE_URL` (if API runs in Docker) |

If `API_BASE_URL` is a public HTTPS URL the worker only needs `bridge-net` (outbound internet is available by default).

**The container does not:**
- Expose any ports
- Store mail on disk
- Write to any volume

---

## Dependencies

| Package | Purpose |
|---|---|
| `requests` | HTTP client for all API calls |
| `python-dotenv` | Optional `.env` file loading |

Standard library: `email`, `hashlib`, `imaplib`, `logging`, `os`, `re`, `signal`, `ssl`, `sys`, `time`
