# Proton Mail Bridge — Docker

> **For AI agents:** This README is written as a reference for implementing Proton Mail IMAP access in applications such as WordPress. Jump to [Connecting an application](#connecting-an-application) for connection parameters and code.

Pre-built image: **`ghcr.io/magpern/proton-bridge:latest`**
Source: <https://github.com/magpern/proton-bridge>
Releases: <https://github.com/magpern/proton-bridge/releases>

---

## What this is

[Proton Mail](https://proton.me/mail) stores all messages end-to-end encrypted. Standard IMAP clients cannot decrypt them. **Proton Mail Bridge** is a local proxy that:

1. Holds your Proton decryption keys
2. Speaks standard IMAP/SMTP to any client
3. Transparently decrypts inbound and encrypts outbound messages

This repo packages Bridge as a headless Docker container so any containerised application can read and send Proton Mail using ordinary IMAP/SMTP libraries — no Proton-specific SDK required.

---

## Architecture

```
Your application (IMAP client)
        │
        │  Standard IMAP — STARTTLS  (proton-bridge:2143 on bridge-net)
        ▼
┌──────────────────────────────────────┐
│  proton-bridge container             │
│                                      │
│  socat  0.0.0.0:2143 → 127.0.0.1:1143│  ← Docker-reachable proxy
│  Bridge         127.0.0.1:1143       │  ← actual IMAP server
│  socat  0.0.0.0:2025 → 127.0.0.1:1025│  ← SMTP proxy
│  Bridge         127.0.0.1:1025       │  ← actual SMTP server
└──────────────────────────────────────┘
        │
        │  Proton API (HTTPS, E2E encrypted)
        ▼
   Proton Mail servers
```

Bridge binds only to `127.0.0.1` inside the container. `socat` re-exposes IMAP and SMTP on `0.0.0.0` (on different port numbers to avoid conflicts) so other containers on the same Docker network can reach them.

---

## IMAP connection parameters

These are the values an application needs to connect once Bridge is running and logged in.

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

  your-app:
    image: your-app-image
    networks:
      - bridge-net          # same network — can reach proton-bridge:2143
    environment:
      IMAP_HOST: proton-bridge
      IMAP_PORT: "2143"

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

### PHP (imap extension)

```php
$imap = imap_open(
    "{proton-bridge:2143/imap/tls/novalidate-cert}INBOX",
    "user@proton.me",
    "<bridge-password>"
);

$unseen = imap_search($imap, "UNSEEN");
foreach ($unseen ?? [] as $uid) {
    $header  = imap_headerinfo($imap, $uid);
    $from    = $header->from[0]->mailbox . "@" . $header->from[0]->host;
    $subject = imap_utf8($header->subject ?? "");
    // process …
    imap_setflag_full($imap, (string)$uid, "\\Seen");
}
imap_close($imap);
```

### WordPress — WP-Cron plugin

Add to `wp-config.php`:

```php
define("PROTON_IMAP_HOST", "proton-bridge");
define("PROTON_IMAP_PORT", 2143);
define("PROTON_IMAP_USER", "user@proton.me");
define("PROTON_IMAP_PASS", "<bridge-password>");
```

Plugin skeleton (`wp-content/plugins/proton-reader/proton-reader.php`):

```php
<?php
/** Plugin Name: Proton Mail Reader */
defined("ABSPATH") || exit;

register_activation_hook(__FILE__, function () {
    if (!wp_next_scheduled("proton_fetch")) {
        wp_schedule_event(time(), "hourly", "proton_fetch");
    }
});
register_deactivation_hook(__FILE__, function () {
    wp_clear_scheduled_hook("proton_fetch");
});

add_action("proton_fetch", function () {
    $host = PROTON_IMAP_HOST;
    $port = PROTON_IMAP_PORT;
    $mbox = imap_open(
        "{{$host}:{$port}/imap/tls/novalidate-cert}INBOX",
        PROTON_IMAP_USER,
        PROTON_IMAP_PASS
    );
    if (!$mbox) {
        error_log("[proton] " . imap_last_error());
        return;
    }
    $ids = imap_search($mbox, "UNSEEN") ?: [];
    foreach ($ids as $id) {
        $h = imap_headerinfo($mbox, $id);
        // do_action("proton_new_email", $h->from[0], imap_utf8($h->subject));
        imap_setflag_full($mbox, (string)$id, "\\Seen");
    }
    imap_close($mbox);
});
```

WordPress must be on `bridge-net` and have the PHP `imap` extension enabled. Test manually:

```bash
docker exec wordpress wp --allow-root cron event run proton_fetch
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
  Password:  AbCdEfGh1234_XyZ   ← this goes into your app config
  Security:  STARTTLS

> quit
```

```bash
# 4. Restart the daemon
docker compose up -d
```

The Bridge password is permanent until explicitly revoked. It survives container restarts as long as the three Docker volumes (`bridge_config`, `bridge_gnupg`, `bridge_pass`) are intact. **Deleting any of these volumes forces a fresh login.**

---

## Operations reference

| Task | Command |
|---|---|
| Start daemon | `docker compose up -d` |
| Stop daemon | `docker compose down` |
| View logs | `docker logs -f proton-bridge` |
| Re-open CLI (stop daemon first) | `docker compose stop proton-bridge && docker compose run --rm -it proton-bridge --cli` |
| Show credentials | CLI → `info` |
| Show sync status | CLI → `status` |
| Log out | CLI → `logout user@proton.me` |
| Wipe all data | `docker compose down -v` |

---

## Releases and versioning

Tags follow the format `v<bridge-version>-<wrapper-patch>`:

```
v3.24.2-1   first wrapper release for Proton Bridge 3.24.2
v3.24.2-2   bug fix in entrypoint, same Bridge version
v3.25.0-1   first wrapper release for Proton Bridge 3.25.0
```

The GitHub Actions workflow builds and pushes to GHCR **only on tagged commits**. Branch pushes compile the image for validation but do not publish.

To release a new version:

```bash
git tag v3.25.0-1
git push origin v3.25.0-1
```

This publishes:

```
ghcr.io/magpern/proton-bridge:3.25.0
ghcr.io/magpern/proton-bridge:3.25
ghcr.io/magpern/proton-bridge:latest
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Failed to create lock file` | `--cli` launched while daemon is running | Stop daemon first: `docker compose stop proton-bridge` |
| `no such user` on IMAP login | Wrong Bridge password in app config | Re-run `info` in CLI and copy the password exactly |
| `socket error: EOF` | Bridge not yet listening (still starting) | Wait ~10 s and retry; check `docker logs proton-bridge` |
| `Broken pipe` in socat logs | Normal — IMAP session closed cleanly | Not an error; suppressed in recent versions |
| PHP `imap_open` returns false | `imap` extension not installed in container | Rebuild PHP image with `docker-php-ext-install imap` |
| Volumes deleted accidentally | All Bridge state lost | Must log in again via CLI |
