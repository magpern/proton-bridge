#!/usr/bin/env bash
# entrypoint.sh — Proton Mail Bridge container entrypoint
#
# Why socat?
#   Bridge binds IMAP and SMTP to 127.0.0.1 inside the container.
#   Docker's port mapping only reaches services bound to 0.0.0.0.
#   socat proxies 0.0.0.0:2143 → 127.0.0.1:1143 (IMAP)
#               and 0.0.0.0:2025 → 127.0.0.1:1025 (SMTP)
#   using port numbers that don't conflict with Bridge's own listeners.
#   Docker maps host:1143 → container:2143, host:1025 → container:2025.
#
# Usage:
#   (no args)  — headless daemon + socat proxies  [docker compose up -d]
#   --cli      — interactive CLI (stop daemon first, no proxy needed)
#                [docker compose stop && docker compose run --rm -it proton-bridge --cli]
#
# Volumes that must persist:
#   bridge_config  → /root/.config/protonmail   (Bridge session/accounts)
#   bridge_gnupg   → /root/.gnupg               (GPG key ring)
#   bridge_pass    → /root/.password-store       (pass credential store)
set -euo pipefail

GPG_HOME="${HOME}/.gnupg"
PASS_STORE="${HOME}/.password-store"
GPG_EMAIL="bridge@local"
GPG_NAME="ProtonBridge"

# ---------------------------------------------------------------------------
# GPG requires 700 permissions on its home directory.
# ---------------------------------------------------------------------------
mkdir -p "${GPG_HOME}"
chmod 700 "${GPG_HOME}"

# ---------------------------------------------------------------------------
# Remove a stale lock file left behind by an unclean shutdown.
# ---------------------------------------------------------------------------
find "${HOME}/.cache/protonmail" -name "*.lock" -delete 2>/dev/null || true

# ---------------------------------------------------------------------------
# First-run: generate GPG key and initialise pass store.
# ---------------------------------------------------------------------------
if [[ ! -f "${PASS_STORE}/.gpg-id" ]]; then
  echo "[bridge] First run — initialising credential store..."

  gpg --batch --gen-key <<EOF
%no-protection
Key-Type: RSA
Key-Length: 4096
Subkey-Type: RSA
Subkey-Length: 4096
Name-Real: ${GPG_NAME}
Name-Email: ${GPG_EMAIL}
Expire-Date: 0
%commit
EOF

  KEY_FP="$(gpg --list-keys --with-colons "${GPG_EMAIL}" \
    | awk -F: '/^fpr:/{print $10; exit}')"

  if [[ -z "${KEY_FP}" ]]; then
    echo "[bridge] ERROR: could not retrieve GPG key fingerprint." >&2
    exit 1
  fi

  echo "[bridge] GPG key: ${KEY_FP}"
  pass init "${KEY_FP}"
  echo "[bridge] Pass store ready."
else
  echo "[bridge] Credential store already present."
fi

# ---------------------------------------------------------------------------
# D-Bus session bus (Bridge may use libsecret over D-Bus).
# ---------------------------------------------------------------------------
if command -v dbus-launch &>/dev/null; then
  eval "$(dbus-launch --sh-syntax 2>/dev/null)" || true
  export DBUS_SESSION_BUS_ADDRESS
fi

# ---------------------------------------------------------------------------
# If arguments were supplied (e.g. --cli), run Bridge directly.
# CLI mode must not run alongside the daemon (lock file conflict).
# ---------------------------------------------------------------------------
if [[ $# -gt 0 ]]; then
  exec protonmail-bridge "$@"
fi

# ---------------------------------------------------------------------------
# Daemon mode: start Bridge in background, wait for its IMAP listener,
# then start socat proxies that expose the loopback ports to Docker.
# ---------------------------------------------------------------------------
cat <<'BANNER'

======================================================================
  Proton Mail Bridge — headless daemon
----------------------------------------------------------------------
  Host IMAP  → 127.0.0.1:1143  (via socat → container 127.0.0.1:1143)
  Host SMTP  → 127.0.0.1:1025  (via socat → container 127.0.0.1:1025)

  First-time login:
    docker compose stop proton-bridge
    docker compose run --rm -it proton-bridge --cli
      > login          (follow the prompts)
      > info           (copy credentials into .env)
      > quit
    docker compose up -d

  View logs:  docker logs -f proton-bridge
======================================================================

BANNER

protonmail-bridge --noninteractive &
BRIDGE_PID=$!

# Forward SIGTERM/SIGINT to Bridge so `docker stop` shuts it down cleanly.
cleanup() {
  echo "[bridge] Shutting down..."
  kill -TERM "${BRIDGE_PID}" 2>/dev/null || true
  wait "${BRIDGE_PID}" 2>/dev/null || true
  exit 0
}
trap cleanup SIGTERM SIGINT SIGHUP

# Wait up to 60 s for Bridge's IMAP port (127.0.0.1:1143) to open.
echo "[bridge] Waiting for IMAP listener..."
for i in $(seq 1 60); do
  if bash -c "echo > /dev/tcp/127.0.0.1/1143" 2>/dev/null; then
    echo "[bridge] IMAP listener ready (${i}s)."
    break
  fi
  if ! kill -0 "${BRIDGE_PID}" 2>/dev/null; then
    echo "[bridge] ERROR: Bridge exited unexpectedly." >&2
    exit 1
  fi
  sleep 1
done

# socat proxy: 0.0.0.0:2143 → 127.0.0.1:1143  (IMAP, Docker maps host:1143→container:2143)
# socat proxy: 0.0.0.0:2025 → 127.0.0.1:1025  (SMTP, Docker maps host:1025→container:2025)
# Port numbers 2143/2025 differ from Bridge's 1143/1025 to avoid bind conflicts.
socat TCP-LISTEN:2143,fork,reuseaddr TCP4:127.0.0.1:1143 &
socat TCP-LISTEN:2025,fork,reuseaddr TCP4:127.0.0.1:1025 &
echo "[bridge] socat proxies active (IMAP :2143→:1143  SMTP :2025→:1025)."

wait "${BRIDGE_PID}"
