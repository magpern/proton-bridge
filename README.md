# Proton Mail Bridge — Docker Setup

A self-contained Docker Compose stack that runs [Proton Mail Bridge](https://proton.me/mail/bridge) as a headless daemon and exposes IMAP and SMTP on a private Docker network. Includes a Python test client and notes on integrating with WordPress.

---

## Table of contents

1. [What is Proton Mail Bridge?](#1-what-is-proton-mail-bridge)
2. [How Bridge works](#2-how-bridge-works)
3. [This Docker setup](#3-this-docker-setup)
4. [First-time setup](#4-first-time-setup)
5. [Day-to-day commands](#5-day-to-day-commands)
6. [IMAP test client](#6-imap-test-client)
7. [WordPress integration](#7-wordpress-integration)
8. [Security notes](#8-security-notes)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. What is Proton Mail Bridge?

Proton Mail is an end-to-end encrypted email service. All messages are encrypted on Proton's servers — standard IMAP/SMTP clients cannot decrypt them directly.

**Proton Mail Bridge** is a local application that sits between your email client and Proton's servers:

- It holds your decryption keys locally.
- It speaks standard IMAP and SMTP to your client.
- It transparently encrypts outgoing messages and decrypts incoming ones before handing them to the client.

The result: any standard email client (Thunderbird, Apple Mail, Outlook, Python's `imaplib`, WordPress…) can read and send Proton Mail as if it were a normal IMAP/SMTP server — without ever exposing your plaintext messages to Proton's infrastructure.

---

## 2. How Bridge works

```
Your application (IMAP client)
        │
        │  Standard IMAP / SMTP  (localhost, plain or STARTTLS)
        ▼
┌─────────────────────┐
│  Proton Mail Bridge │  ← runs on your machine / in Docker
│  ─────────────────  │
│  Decrypts inbound   │
│  Encrypts outbound  │
│  Manages sessions   │
└─────────────────────┘
        │
        │  Proton API (HTTPS, end-to-end encrypted)
        ▼
   Proton Mail servers
```

### Ports (Bridge defaults)

| Protocol | Port | Security |
|---|---|---|
| IMAP | 1143 | STARTTLS |
| SMTP | 1025 | STARTTLS |

Bridge binds these ports to `127.0.0.1` only. In this Docker setup a `socat` proxy re-exposes them inside the container network (see §3).

### Credentials

Bridge generates a **separate, random password** for each account. This password is used for IMAP/SMTP login — your real Proton account password is never sent to any IMAP/SMTP client. You can revoke the Bridge password at any time without changing your Proton account.

### Session persistence

Bridge stores its session token and account keys using the `pass` password manager (GPG-encrypted on Linux). This is why the Docker volumes must persist: losing the volumes means Bridge loses the session and you must log in again.

---

## 3. This Docker setup

### Architecture

```
┌────────────────────── Docker: bridge-net ──────────────────────┐
│                                                                  │
│   ┌──────────────────────────────────────────────────────────┐  │
│   │  proton-bridge container                                 │  │
│   │                                                          │  │
│   │   protonmail-bridge --noninteractive                     │  │
│   │      └─ IMAP  127.0.0.1:1143  (loopback only)           │  │
│   │      └─ SMTP  127.0.0.1:1025  (loopback only)           │  │
│   │                                                          │  │
│   │   socat proxy (exposes to Docker network)                │  │
│   │      └─ 0.0.0.0:2143  →  127.0.0.1:1143  (IMAP)        │  │
│   │      └─ 0.0.0.0:2025  →  127.0.0.1:1025  (SMTP)        │  │
│   └──────────────────────────────────────────────────────────┘  │
│                                                                  │
│   ┌──────────────────────┐   ┌──────────────────────────────┐   │
│   │  imap-client         │   │  your-wordpress (optional)   │   │
│   │  python imap_test.py │   │  connects to proton-bridge   │   │
│   │  (profile: test)     │   │  on bridge-net               │   │
│   └──────────────────────┘   └──────────────────────────────┘   │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘

Windows host — NO host port bindings; IMAP/SMTP not reachable from host
```

### File layout

```
proton/
├── docker-compose.yml        Main stack
├── .env.example              Copy → .env
├── .env                      Your credentials (gitignored)
├── imap_test.py              Python IMAP client
├── requirements.txt
├── bridge/
│   ├── Dockerfile            Debian + Bridge .deb + socat
│   └── entrypoint.sh         GPG/pass init + socat proxy + daemon
└── client/
    └── Dockerfile            Python 3.13-slim image
```

### Volumes

| Volume | Mounted at | Contents |
|---|---|---|
| `bridge_config` | `/root/.config/protonmail` | Bridge session, account keys |
| `bridge_gnupg` | `/root/.gnupg` | GPG key ring used by `pass` |
| `bridge_pass` | `/root/.password-store` | `pass` credential store |

**Deleting any of these volumes forces a fresh login.**

---

## 4. First-time setup

### Prerequisites

- Docker Desktop for Windows (WSL 2 backend recommended)
- Python 3.8+ (only needed if running `imap_test.py` locally; not needed for the Docker-only path)

### Step 1 — Clone and configure

```powershell
cd d:\DeveloperArea\Repos\proton
Copy-Item .env.example .env
notepad .env   # set BRIDGE_VERSION if needed
```

Check the latest Bridge version at:
<https://github.com/ProtonMail/proton-bridge/releases>

### Step 2 — Build

```powershell
docker compose build
```

### Step 3 — First-time login

Bridge's `--cli` mode cannot attach to a running daemon (they share a lock file). Stop the daemon, run CLI in a temporary container that shares the same volumes, log in, then restart.

```powershell
# Nothing running yet on first setup, so just:
docker compose run --rm -it proton-bridge --cli
```

Inside the Bridge CLI:

```
> login
Username: your.address@proton.me
Password: <your Proton account password>
2FA code: <TOTP if enabled>

Login successful.

> info
```

The `info` command shows the Bridge-generated IMAP credentials:

```
Configuration for your.address@proton.me
IMAP Settings
  Address:   127.0.0.1
  IMAP port: 1143
  Username:  your.address@proton.me
  Password:  AbCdEfGh1234_XyZ   ← copy this
  Security:  STARTTLS
```

```
> quit
```

### Step 4 — Fill in credentials

Edit `.env`:

```env
BRIDGE_IMAP_USER=your.address@proton.me
BRIDGE_IMAP_PASS=AbCdEfGh1234_XyZ
BRIDGE_FILTER_TO=                    # optional: filter by recipient address
BRIDGE_FETCH_LIMIT=0                 # 0 = all messages
```

### Step 5 — Start the daemon

```powershell
docker compose up -d
docker logs -f proton-bridge
```

You should see:

```
[bridge] socat proxies active (IMAP :2143→:1143  SMTP :2025→:1025).
```

### Step 6 — Test

```powershell
docker compose run --rm imap-client
```

---

## 5. Day-to-day commands

| Task | Command |
|---|---|
| Start daemon | `docker compose up -d` |
| Stop daemon | `docker compose down` |
| View logs | `docker logs -f proton-bridge` |
| Open Bridge CLI | `docker compose stop proton-bridge && docker compose run --rm -it proton-bridge --cli` |
| Show IMAP credentials | CLI → `info` |
| Show status | CLI → `status` |
| Log out an account | CLI → `logout your.address@proton.me` |
| Restart daemon after CLI | `docker compose up -d` |
| Rebuild after version bump | `docker compose up -d --build` |
| Wipe everything and start fresh | `docker compose down -v` |

---

## 6. IMAP test client

`imap_test.py` connects to the Bridge container, searches INBOX, and prints messages sorted newest-first.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `BRIDGE_IMAP_HOST` | `proton-bridge` | Container name on `bridge-net` |
| `BRIDGE_IMAP_PORT` | `2143` | socat proxy port |
| `BRIDGE_IMAP_USER` | — | Bridge username (from `info`) |
| `BRIDGE_IMAP_PASS` | — | Bridge password (from `info`) |
| `BRIDGE_IMAP_SSL` | `false` | `true` for SSL, `false` for STARTTLS |
| `BRIDGE_SKIP_TLS_VERIFY` | `true` | Accept Bridge's self-signed certificate |
| `BRIDGE_FILTER_TO` | *(empty)* | Filter by To/Cc recipient (substring) |
| `BRIDGE_FETCH_LIMIT` | `0` | Max messages to show; `0` = all |

### Examples

```powershell
# All messages in INBOX
docker compose run --rm imap-client

# Only messages to a specific address
docker compose run --rm -e BRIDGE_FILTER_TO=info@example.com imap-client

# Latest 20 messages to a specific address
docker compose run --rm \
  -e BRIDGE_FILTER_TO=info@example.com \
  -e BRIDGE_FETCH_LIMIT=20 \
  imap-client
```

---

## 7. WordPress integration

WordPress can read email from Proton Mail Bridge the same way any IMAP client does. The key requirement: **WordPress must run on the same Docker network (`bridge-net`) as `proton-bridge`.**

### Add WordPress to the stack

```yaml
# append to docker-compose.yml

  wordpress:
    image: wordpress:latest
    container_name: wordpress
    restart: unless-stopped
    networks:
      - bridge-net          # gives access to proton-bridge:2143
    environment:
      WORDPRESS_DB_HOST: db
      WORDPRESS_DB_USER: wp
      WORDPRESS_DB_PASSWORD: wp
      WORDPRESS_DB_NAME: wordpress
    ports:
      - "127.0.0.1:8080:80"
    depends_on:
      - db
      - proton-bridge

  db:
    image: mysql:8
    container_name: wordpress-db
    restart: unless-stopped
    networks:
      - bridge-net
    environment:
      MYSQL_DATABASE: wordpress
      MYSQL_USER: wp
      MYSQL_PASSWORD: wp
      MYSQL_ROOT_PASSWORD: rootpassword
    volumes:
      - db_data:/var/lib/mysql

volumes:
  db_data:
```

From inside the WordPress container, the Bridge is reachable at:

| Setting | Value |
|---|---|
| IMAP host | `proton-bridge` |
| IMAP port | `2143` |
| Username | Bridge username (from `info`) |
| Password | Bridge password (from `info`) |
| Security | STARTTLS |

### Option A — Plugin (no code)

Several WordPress plugins can connect to an IMAP mailbox and process incoming messages. Install via the WordPress admin panel.

**For support tickets / help desk:**
- [Awesome Support](https://wordpress.org/plugins/awesome-support/) — creates tickets from emails, configure IMAP under *Awesome Support → Settings → Emails → Fetch Emails*
- [WP Desk Ticketing System](https://wordpress.org/plugins/wp-desk-ticketing-system/)

**For general email-to-post / email processing:**
- [Postie](https://wordpress.org/plugins/postie/) — publishes posts from emails received in an IMAP mailbox
- [Email to Post](https://wordpress.org/plugins/email-to-post/) — similar

Configure any of these with:
- **IMAP server:** `proton-bridge`
- **Port:** `2143`
- **Username/Password:** Bridge credentials
- **TLS:** STARTTLS (disable certificate verification if the plugin supports it, as Bridge uses a self-signed cert)

### Option B — Custom PHP (WP-Cron + IMAP)

For custom processing (e.g. parsing contact form replies, importing orders from email) use PHP's built-in `imap_*` functions inside a scheduled WP-Cron job.

PHP's IMAP extension must be enabled. In the WordPress container add a custom image or use a Dockerfile:

```dockerfile
FROM wordpress:latest
RUN docker-php-ext-install imap || true
RUN apt-get update && apt-get install -y --no-install-recommends \
    libc-client-dev libkrb5-dev \
    && docker-php-ext-configure imap --with-kerberos --with-imap-ssl \
    && docker-php-ext-install imap \
    && rm -rf /var/lib/apt/lists/*
```

#### Example plugin: fetch and log unread emails

Create `wp-content/plugins/proton-imap-reader/proton-imap-reader.php`:

```php
<?php
/**
 * Plugin Name: Proton IMAP Reader
 * Description: Reads unread emails from Proton Mail Bridge via IMAP.
 */

defined('ABSPATH') || exit;

// Register a daily WP-Cron event.
register_activation_hook(__FILE__, function () {
    if (!wp_next_scheduled('proton_imap_fetch')) {
        wp_schedule_event(time(), 'hourly', 'proton_imap_fetch');
    }
});

register_deactivation_hook(__FILE__, function () {
    wp_clear_scheduled_hook('proton_imap_fetch');
});

add_action('proton_imap_fetch', 'proton_imap_fetch_emails');

function proton_imap_fetch_emails(): void {
    // Credentials — store in wp-config.php or use a secrets manager.
    $host = defined('PROTON_IMAP_HOST') ? PROTON_IMAP_HOST : 'proton-bridge';
    $port = defined('PROTON_IMAP_PORT') ? PROTON_IMAP_PORT : 2143;
    $user = defined('PROTON_IMAP_USER') ? PROTON_IMAP_USER : '';
    $pass = defined('PROTON_IMAP_PASS') ? PROTON_IMAP_PASS : '';

    if (!$user || !$pass) {
        error_log('[proton-imap] PROTON_IMAP_USER / PROTON_IMAP_PASS not set.');
        return;
    }

    // /novalidate-cert: accept Bridge's self-signed certificate.
    // /tls:            use STARTTLS.
    $mailbox_str = "{{$host}:{$port}/imap/tls/novalidate-cert}INBOX";

    $imap = @imap_open($mailbox_str, $user, $pass, 0, 1);
    if (!$imap) {
        error_log('[proton-imap] Connection failed: ' . imap_last_error());
        return;
    }

    // Fetch unseen messages.
    $uids = imap_search($imap, 'UNSEEN');
    if (!$uids) {
        imap_close($imap);
        return;
    }

    foreach ($uids as $uid) {
        $header  = imap_headerinfo($imap, $uid);
        $from    = $header->from[0]->mailbox . '@' . $header->from[0]->host;
        $subject = isset($header->subject)
            ? imap_utf8($header->subject)
            : '(no subject)';
        $date    = $header->date;

        // ── Do something with the email here ──────────────────────────────
        // Examples:
        //   - wp_insert_post([...]) to create a post
        //   - do_action('proton_imap_new_email', $from, $subject, $body)
        //   - update_option() to store the latest message
        // ─────────────────────────────────────────────────────────────────

        error_log("[proton-imap] New email — From: {$from} | Subject: {$subject} | Date: {$date}");

        // Mark as seen so we don't process it again.
        imap_setflag_full($imap, (string)$uid, '\\Seen');
    }

    imap_close($imap);
}
```

Add credentials to `wp-config.php` (never hardcode them in the plugin):

```php
define('PROTON_IMAP_HOST', 'proton-bridge');
define('PROTON_IMAP_PORT', 2143);
define('PROTON_IMAP_USER', 'your.address@proton.me');
define('PROTON_IMAP_PASS', 'AbCdEfGh1234_XyZ');
```

#### Trigger the cron manually for testing

```bash
docker exec wordpress wp --allow-root cron event run proton_imap_fetch
```

### Option C — Sidecar PHP script (outside WordPress)

For heavy processing (import pipelines, CRM sync) it is cleaner to run a standalone PHP or Python script as its own container in `bridge-net` and write results to the WordPress database or REST API directly, rather than burdening WP-Cron.

```yaml
  email-processor:
    build: ./processor        # your custom image
    networks:
      - bridge-net
    environment:
      PROTON_IMAP_HOST: proton-bridge
      PROTON_IMAP_PORT: 2143
    env_file:
      - .env
    restart: unless-stopped
```

---

## 8. Security notes

| Concern | How it is handled here |
|---|---|
| IMAP/SMTP exposed to the internet | Not exposed. No host port bindings. |
| IMAP/SMTP exposed to the Windows host | Not exposed. Ports only exist on `bridge-net`. |
| Bridge credentials in plain text | Stored in `.env` (gitignored). Use Docker secrets for production. |
| Bridge GPG key has no passphrase | Acceptable for local dev. The key is inside a Docker volume, not on the host filesystem directly. |
| Bridge self-signed TLS cert | `BRIDGE_SKIP_TLS_VERIFY=true` is safe on a loopback/private Docker network. Do not use on a public or shared network. |
| Volume loss = forced re-login | Back up `bridge_config`, `bridge_gnupg`, `bridge_pass` volumes if continuity matters. |

---

## 9. Troubleshooting

### `Failed to create lock file; another instance is running`

You tried to run `--cli` while the daemon is running. Stop the daemon first:

```powershell
docker compose stop proton-bridge
docker compose run --rm -it proton-bridge --cli
# ... do what you need ...
docker compose up -d
```

### `socket error: EOF` or `SSLEOFError`

Bridge binds to `127.0.0.1` inside the container; Docker's port mapping cannot reach it. The `socat` proxy in `entrypoint.sh` bridges the gap. If socat hasn't started yet (Bridge still initialising), wait a few seconds and retry.

### `no such user` on IMAP login

The credentials in `.env` don't match what Bridge generated. Re-check with:

```powershell
docker compose stop proton-bridge
docker compose run --rm -it proton-bridge --cli
> info
docker compose up -d
```

Copy the `Password` field exactly into `BRIDGE_IMAP_PASS`.

### `module 'email' has no attribute 'message'`

Python did not auto-import the `email.message` submodule. Ensure `import email.message` is in `imap_test.py`.

### GPG key generation is slow

GPG needs entropy. On modern kernels this resolves in under 10 seconds. If it hangs, ensure the host is not heavily loaded.

### Bridge exits immediately after start

Check logs: `docker logs proton-bridge`. A stale lock file from an unclean shutdown is the most common cause. The entrypoint cleans `~/.cache/protonmail/*.lock` automatically; if it persists, wipe the `bridge_config` volume and log in again.

### WordPress PHP IMAP extension missing

```bash
docker exec wordpress php -m | grep imap
```

If not listed, you need a custom WordPress image with `imap` compiled in (see §7 Option B).
