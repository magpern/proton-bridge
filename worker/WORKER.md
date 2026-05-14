# Mail Worker — Technical Reference

The `mail-worker` is a Python daemon that bridges Proton Mail Bridge IMAP to a generic HTTP REST API. It runs in a Docker container on the same private network as `proton-bridge`, polls for new messages on a configurable interval, and POSTs each parsed message to an external API. A built-in HTTP server allows external callers to trigger an immediate poll outside the normal interval.

---

## Environment variables

| Variable | Type | Required | Default | Description |
|---|---|---|---|---|
| `IMAP_HOST` | string | | `proton-bridge` | Hostname of the Bridge container |
| `IMAP_PORT` | integer | | `2143` | socat proxy port (Bridge 1143, re-exposed by socat on 2143) |
| `IMAP_USER` | string | ✓ | | Proton address used as IMAP login |
| `IMAP_PASS` | string | ✓ | | Bridge-generated password (not the Proton account password) |
| `IMAP_MAILBOX` | string | | `INBOX` | Mailbox to poll |
| `IMAP_FETCH_MODE` | string | | `keyword` | How to track imported messages — see below |
| `IMAP_FETCH_KEYWORD` | string | | `FETCHED` | Custom keyword name (mode `keyword` only) |
| `IMAP_SEARCH` | string | | *(derived)* | Override auto-derived IMAP search expression |
| `API_BASE_URL` | string | ✓ | | Base URL of the REST API, no trailing slash |
| `API_TOKEN` | string | ✓ | | Bearer token sent in every outbound API request and required for `/poll` |
| `POLL_INTERVAL` | integer | | `300` | Seconds between automatic poll cycles |
| `MESSAGE_CAP` | integer | | `50` | Maximum messages processed per cycle; `0` = unlimited |
| `WORKER_VERSION` | string | | `1.0.0` | Version string reported in heartbeat |
| `WORKER_HTTP_PORT` | integer | | `8080` | Port for the built-in trigger HTTP server |

Credentials (`IMAP_PASS`, `API_TOKEN`) are never written to logs.

### Fetch mode

`IMAP_FETCH_MODE` controls both what the worker searches for and what it marks after a successful import. `IMAP_SEARCH` is auto-derived unless explicitly overridden.

| Mode | Search (auto) | Mark after import | Notes |
|---|---|---|---|
| `keyword` | `UNKEYWORD FETCHED` | `+FLAGS (FETCHED)` | Custom keyword; test Proton Bridge support before using |
| `flagged` | `UNFLAGGED` | `+FLAGS (\Flagged)` | Uses the starred/pin flag; always works |
| `seen` | `UNSEEN` | `+FLAGS (\Seen)` | Risk: Proton mobile app sets `\Seen` when opening mail |
| `none` | `ALL` | *(nothing)* | Stateless; relies on `imap_dedupe_key` in the API |

`IMAP_FETCH_KEYWORD` sets the keyword name when mode is `keyword` (default: `FETCHED`).

If Proton Bridge does not support custom keywords, the `STORE` command will fail silently (logged as WARNING) and messages will be re-fetched next cycle. Switch to `flagged` in that case.

Startup exits with code 1 if any required variable is missing or if `IMAP_PORT`, `POLL_INTERVAL`, `MESSAGE_CAP`, or `WORKER_HTTP_PORT` is not a valid integer.

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
     trigger endpoint: POST http://0.0.0.0:8080/poll
4. Install SIGTERM / SIGINT handlers for graceful shutdown
5. Start trigger HTTP server in a background daemon thread (port 8080)
6. Enter main loop
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
10. Sleep until:
      - POLL_INTERVAL seconds elapse, OR
      - SIGTERM / SIGINT received, OR
      - POST /poll trigger fires
    (1-second ticks via threading.Event.wait so all three wake conditions
     are handled promptly)
```

**Concurrent poll protection:** a `threading.Lock` is held for the duration of step 2–9. If `POST /poll` arrives while a cycle is running, the event is still set — the current cycle finishes normally, then the sleep is skipped and a second cycle starts immediately. No two IMAP sessions ever run simultaneously.

**Trigger during sleep:** `_force_poll` is cleared at the *top* of each iteration (not at the start of sleep), so a trigger that fires during steps 2–9 is never silently dropped — it will wake the next sleep immediately.

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

`Message-ID`, `In-Reply-To`, and `References` are normalised consistently:
- Strip surrounding whitespace
- Strip surrounding `< >`
- Convert to lowercase

`references` is split on whitespace and each entry normalised individually, producing a list.

### Body extraction

MIME parts are walked recursively. Parts with `Content-Disposition: attachment` are skipped (attachments not supported in phase 1). `text/plain` parts are joined into `body_text`; `text/html` parts into `body_html`. Non-multipart messages go into whichever field matches their content type.

Malformed messages do not crash the worker — a parse exception increments the error counter and skips to the next message.

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

---

## Trigger endpoint (worker exposes)

The worker runs a lightweight HTTP server on port `8080` (configurable via `WORKER_HTTP_PORT`) in a background daemon thread. It accepts connections from any container on the same Docker network. No `ports:` mapping is added in Compose — this is internal-only.

Expected base URLs from a sibling container:
```
http://mail-worker:8080/health
http://mail-worker:8080/poll
```

### `GET /health`

No authentication required. Suitable as a Docker healthcheck.

**Request:**
```http
GET http://mail-worker:8080/health
```

**Response `200`:**
```json
{ "ok": true, "worker_version": "1.0.0" }
```

### `POST /poll`

Wakes the sleep loop immediately and triggers a poll cycle outside the normal interval. If a poll cycle is already running, the trigger is queued — the current cycle finishes first, then a second cycle runs immediately. Two IMAP sessions never overlap.

No authentication required. The endpoint is reachable only from containers on `bridge-net` — it is never published to the host or the internet.

**Request:**
```http
POST http://mail-worker:8080/poll
```

**Response `202` — trigger accepted:**
```json
{ "ok": true, "message": "poll triggered" }
```

**Response `202` — already polling, queued:**
```json
{ "ok": true, "message": "poll queued — will run after current cycle" }
```

**Example:**
```bash
curl -s -X POST http://mail-worker:8080/poll
```

---

## API endpoints (worker calls outbound)

### `GET {API_BASE_URL}/health`

Called at the start of every poll cycle to verify the API is reachable.

**Request:** No body. `Authorization: Bearer {API_TOKEN}` header sent.

**Expected response — HTTP 200:**
```json
{ "ok": true }
```

**Worker behaviour:**

| Response | Action |
|---|---|
| HTTP 200 | Log `api health ok`, continue cycle |
| Any other status | Log warning, send status `api_unavailable`, skip IMAP, sleep |
| Network error / timeout | Same as non-200 |

---

### `POST {API_BASE_URL}/messages/import`

Delivers one parsed message per request.

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
  "date":             "2026-05-14T08:00:00+00:00",
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
| HTTP 400 / 401 | No | `errors++` (non-retryable, logged ERROR) |
| HTTP 500 | No | `errors++` (retryable, logged WARNING) |
| Timeout | No | `errors++` |
| Network error | No | `errors++` |

---

### `POST {API_BASE_URL}/worker/status`

Heartbeat sent once after every poll cycle, including failed ones.

**Request headers:**
```http
Authorization: Bearer {API_TOKEN}
Content-Type: application/json
```

**Request body:**
```json
{
  "worker_version":   "1.0.0",
  "heartbeat_at":     "2026-05-14T08:01:00+00:00",
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

## Mark rules

The configured flag (`IMAP_FETCH_MODE`) is applied via `UID STORE +FLAGS` only when **all** of the following are true:

1. `IMAP_FETCH_MODE` is not `none`
2. The API returned HTTP 2xx
3. The response body contains `"status": "imported"` or `"status": "skipped_duplicate"`

The flag is **not** set after a parse failure, API 4xx/5xx, timeout, network error, or unexpected status string — leaving the message eligible for retry on the next cycle.

If the `STORE` command itself fails (e.g. Proton Bridge rejects a custom keyword), the error is logged at WARNING level and the cycle continues. Switch `IMAP_FETCH_MODE` to `flagged` if custom keywords are unsupported.

---

## Docker

```dockerfile
FROM python:3.13-slim
RUN useradd -r -s /bin/false worker
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY worker.py .
EXPOSE 8080
USER worker
CMD ["python", "worker.py"]
```

**Networks required:**

| Network | Purpose |
|---|---|
| `bridge-net` | Reach `proton-bridge:2143` (IMAP) |
| API network | Reach `API_BASE_URL` (if API runs in Docker) |

If `API_BASE_URL` is a public HTTPS URL, `bridge-net` alone is sufficient.

**The container does not:**
- Store mail on disk
- Write to any volume
- Require root

---

## Dependencies

| Package | Purpose |
|---|---|
| `requests` | HTTP client for all outbound API calls |
| `python-dotenv` | Optional `.env` file loading |

Standard library: `email`, `hashlib`, `http.server`, `imaplib`, `json`, `logging`, `os`, `re`, `signal`, `ssl`, `sys`, `threading`, `time`
