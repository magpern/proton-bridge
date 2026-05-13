# Proton Mail Bridge — Docker

> **For AI agents:** This README is the reference for consuming Proton Mail in containerised applications. Jump to [Mail worker](#mail-worker-proton--rest-api) for the recommended integration pattern, or [IMAP connection parameters](#imap-connection-parameters) if you prefer to speak IMAP directly.

Pre-built images:
- **`ghcr.io/magpern/proton-bridge:latest`** — headless Bridge daemon
- **`ghcr.io/magpern/proton-mail-worker:latest`** — IMAP → REST API worker

Source: <https://github.com/magpern/proton-bridge>
Releases: <https://github.com/magpern/proton-bridge/releases>

---

## What this is

[Proton Mail](https://proton.me/mail) stores all messages end-to-end encrypted. Standard IMAP clients cannot decrypt them. **Proton Mail Bridge** is a local proxy that:

1. Holds your Proton decryption keys
2. Speaks standard IMAP/SMTP to any client
3. Transparently decrypts inbound and encrypts outbound messages

This repo ships two containers:

| Container | Purpose |
|---|---|
| `proton-bridge` | Headless Bridge daemon — exposes IMAP and SMTP on a private Docker network |
| `mail-worker` | Python daemon — polls Bridge IMAP and POSTs each message to a REST API |

---

## Architecture

```
                    Proton Mail servers
                           │  HTTPS (E2E encrypted)
                           ▼
┌──────────────────────────────────────┐
│  proton-bridge container             │
│  socat  0.0.0.0:2143 → 127.0.0.1:1143│  ← IMAP proxy (bridge-net)
│  Bridge         127.0.0.1:1143       │
│  socat  0.0.0.0:2025 → 127.0.0.1:1025│  ← SMTP proxy (bridge-net)
│  Bridge         127.0.0.1:1025       │
└──────────────────────────────────────┘
          │  IMAP STARTTLS (bridge-net)
          ▼
┌─────────────────────────┐
│  mail-worker container  │
│  • polls IMAP           │
│  • parses MIME          │
│  • POSTs to REST API    │
│  • sends heartbeat      │
└─────────────────────────┘
          │  HTTPS  Bearer <API_TOKEN>
          ▼
   Your REST API
   POST /messages/import
   POST /worker/status
   GET  /health
```

Bridge binds only to `127.0.0.1` inside its container. `socat` re-exposes IMAP and SMTP on `0.0.0.0` using different port numbers so other containers on `bridge-net` can reach them. No ports are published to the host.

---

## IMAP connection parameters

Values needed to connect directly to Bridge (e.g. using `imaplib` or a custom client).

| Parameter | Value |
|---|---|
| Host | `proton-bridge` (Docker service name on `bridge-net`) |
| IMAP port | `2143` (socat proxy → Bridge 1143) |
| SMTP port | `2025` (socat proxy → Bridge 1025) |
| Security | STARTTLS |
| Certificate | Self-signed for `127.0.0.1` — skip hostname verification |
| Username | Your Proton address, e.g. `user@proton.me` |
| Password | **Bridge-generated** (≠ your Proton account password) — see [First-time login](#first-time-login) |

---

## Connecting an application

### Add Bridge to an existing docker-compose stack

```yaml
services:
  proton-bridge:
    image: ghcr.io/magpern/proton-bridge:latest
    container_name: proton-bridge
    restart: unless-stopped
    volumes:
      - bridge_config:/root/.config/protonmail
      - bridge_gnupg:/root/.gnupg
      - bridge_pass:/root/.password-store
    networks:
      - bridge-net
    stdin_open: true
    tty: true

networks:
  bridge-net:
    driver: bridge

volumes:
  bridge_config:
  bridge_gnupg:
  bridge_pass:
```

### Python (imaplib)

```python
import imaplib, ssl

ctx = ssl.create_default_context()
ctx.check_hostname = False        # Bridge cert is for 127.0.0.1, not the service name
ctx.verify_mode = ssl.CERT_NONE

conn = imaplib.IMAP4("proton-bridge", 2143)
conn.starttls(ssl_context=ctx)
conn.login("user@proton.me", "<bridge-password>")

conn.select("INBOX")
_, data = conn.search(None, "ALL")
print(data[0].split())   # list of message IDs

conn.logout()
```

---

## Mail worker (Proton → REST API)

The `mail-worker` container is the recommended way to integrate Proton Mail with a web application. It runs as a daemon, polls Bridge IMAP, and POSTs each message to your REST API — no PHP IMAP extension required.

### Quick start

```bash
# 1. Fill in the worker variables in .env (see table below)
# 2. Start Bridge + worker
docker compose --profile worker up -d

# Logs
docker logs -f mail-worker
```

### docker-compose snippet

```yaml
services:
  mail-worker:
    image: ghcr.io/magpern/proton-mail-worker:latest
    restart: unless-stopped
    env_file:
      - .env
    networks:
      - bridge-net   # reaches proton-bridge
      # add your API's network here if it runs in Docker
    depends_on:
      - proton-bridge
```

### Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `IMAP_HOST` | | `proton-bridge` | Bridge service name on `bridge-net` |
| `IMAP_PORT` | | `2143` | socat proxy port |
| `IMAP_USER` | ✓ | | Your Proton address |
| `IMAP_PASS` | ✓ | | Bridge-generated password |
| `IMAP_MAILBOX` | | `INBOX` | Mailbox to poll |
| `IMAP_SEARCH` | | `UNSEEN` | Any valid IMAP SEARCH expression |
| `MARK_SEEN` | | `true` | Mark imported messages `\Seen` |
| `API_BASE_URL` | ✓ | | REST API base URL, no trailing slash |
| `API_TOKEN` | ✓ | | Bearer token |
| `POLL_INTERVAL` | | `300` | Seconds between cycles |
| `MESSAGE_CAP` | | `50` | Max messages per cycle; `0` = unlimited |
| `WORKER_VERSION` | | `1.0.0` | Reported in heartbeat |

### API endpoints the worker calls

#### `GET {API_BASE_URL}/health`

Called before every poll cycle. Worker skips the cycle and retries next interval on failure.

Expected response:
```json
{ "ok": true }
```

#### `POST {API_BASE_URL}/messages/import`

```http
Authorization: Bearer {API_TOKEN}
Content-Type: application/json
```

Request body:
```json
{
  "message_id":       "abc123@proton.me",
  "in_reply_to":      "parent@example.com",
  "references":       ["root@example.com", "parent@example.com"],
  "imap_folder":      "INBOX",
  "imap_uidvalidity": "123456",
  "imap_uid":         42,
  "imap_dedupe_key":  "sha1hex...",
  "from_email":       "customer@example.com",
  "from_name":        "Customer Name",
  "to_email":         "support@example.com",
  "subject":          "Question about order",
  "date":             "2026-05-13T12:00:00+00:00",
  "body_text":        "Plain text body",
  "body_html":        "<p>HTML body</p>",
  "raw_headers":      "From: ...\r\nTo: ...\r\n"
}
```

`imap_dedupe_key` = `sha1(lower(folder) + "|" + uidvalidity + "|" + uid)`. Null if UIDVALIDITY is unavailable.

Expected responses:
```json
{ "status": "imported",          "ticket_id": 123 }
{ "status": "skipped_duplicate", "reason": "message_id" }
```

The worker marks a message `\Seen` only after receiving `imported` or `skipped_duplicate`. On `4xx`/`5xx`/timeout the message is left unseen and retried next cycle.

#### `POST {API_BASE_URL}/worker/status`

Heartbeat sent after every poll cycle.

```json
{
  "worker_version":   "1.0.0",
  "heartbeat_at":     "2026-05-13T12:00:00+00:00",
  "last_poll_status": "ok",
  "imported":         5,
  "skipped":          2,
  "errors":           0,
  "last_error":       null
}
```

---

## First-time login

> **This step requires a human.** Bridge must be authenticated with a Proton account before any IMAP connection will succeed. It cannot be automated — Proton requires interactive login with a password and optional 2FA.

```bash
# 1. Pull and start the stack (Bridge will be running but not logged in)
docker compose up -d

# 2. Stop just the bridge daemon
docker compose stop proton-bridge

# 3. Open an interactive CLI session (uses the same persistent volumes)
docker compose run --rm -it proton-bridge --cli
```

Inside the Bridge CLI:

```
> login
Username: user@proton.me
Password: <your Proton account password>
2FA code: <TOTP if enabled>

> info
```

`info` prints the Bridge-generated IMAP credentials:

```
Configuration for user@proton.me
IMAP Settings
  Address:   127.0.0.1
  IMAP port: 1143
  Username:  user@proton.me
  Password:  AbCdEfGh1234_XyZ   ← copy this into IMAP_PASS / BRIDGE_IMAP_PASS
  Security:  STARTTLS

> quit
```

```bash
# 4. Restart the daemon (and worker if using it)
docker compose --profile worker up -d
```

The Bridge password is permanent until explicitly revoked. It survives container restarts as long as the three Docker volumes (`bridge_config`, `bridge_gnupg`, `bridge_pass`) are intact. **Deleting any of these volumes forces a fresh login.**

---

## Operations reference

| Task | Command |
|---|---|
| Start Bridge only | `docker compose up -d` |
| Start Bridge + worker | `docker compose --profile worker up -d` |
| Stop everything | `docker compose --profile worker down` |
| Bridge logs | `docker logs -f proton-bridge` |
| Worker logs | `docker logs -f mail-worker` |
| Re-open Bridge CLI (stop daemon first) | `docker compose stop proton-bridge && docker compose run --rm -it proton-bridge --cli` |
| Show Bridge credentials | CLI → `info` |
| Show sync status | CLI → `status` |
| Log out of Bridge | CLI → `logout user@proton.me` |
| Wipe all Bridge data | `docker compose down -v` |

---

## Releases and versioning

Tags follow the format `v<bridge-version>-<wrapper-patch>`:

```
v3.24.2-1   first wrapper release for Proton Bridge 3.24.2
v3.24.2-2   bug fix in worker or entrypoint, same Bridge version
v3.25.0-1   first wrapper release for Proton Bridge 3.25.0
```

A single tag publishes both images:

| Image | Tags produced |
|---|---|
| `proton-bridge` | `3.24.2`, `3.24`, `latest` |
| `proton-mail-worker` | `3.24.2-1`, `latest` |

```bash
git tag v3.25.0-1
git push origin v3.25.0-1
```

Branch pushes build both images for validation but do not publish.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Failed to create lock file` | `--cli` launched while daemon is running | Stop daemon first: `docker compose stop proton-bridge` |
| `no such user` on IMAP login | Wrong Bridge password | Re-run `info` in CLI and copy the password exactly into `IMAP_PASS` |
| `socket error: EOF` | Bridge not yet listening (still starting) | Wait ~10 s and retry; check `docker logs proton-bridge` |
| `Broken pipe` in socat logs | Normal — IMAP session closed cleanly | Not an error; suppressed in recent versions |
| Worker: `api health` warning every cycle | `API_BASE_URL` unreachable | Check URL, network, and that the REST plugin is active |
| Worker: messages found but not imported | API returning non-2xx | Check `docker logs mail-worker` for HTTP status; fix token or payload |
| Worker: messages re-imported every cycle | `MARK_SEEN=false` or API not returning `imported`/`skipped_duplicate` | Set `MARK_SEEN=true`; verify API response body |
| Volumes deleted accidentally | All Bridge state lost | Must log in again via CLI |
