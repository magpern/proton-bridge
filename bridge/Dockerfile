FROM debian:bookworm-slim

ARG BRIDGE_VERSION=3.24.2

# Single apt layer: install base deps, download Bridge .deb, resolve its deps
# via `apt-get install -f`, then clean up — all while the package index is live.
#
# Bridge is a Qt app and pulls in X11/EGL/Mesa/PulseAudio libs even in headless
# mode; `apt-get install -f` installs them automatically from the .deb metadata.
RUN set -eux; \
    apt-get update; \
    # Base tools needed by entrypoint.sh: GPG + pass (credential store) and dbus
    apt-get install -y --no-install-recommends \
        ca-certificates \
        gnupg \
        pass \
        dbus \
        libsecret-1-0 \
        socat \
        wget; \
    # Download the official Bridge .deb
    wget -q -O /tmp/protonmail-bridge.deb \
        "https://github.com/ProtonMail/proton-bridge/releases/download/v${BRIDGE_VERSION}/protonmail-bridge_${BRIDGE_VERSION}-1_amd64.deb"; \
    # Stage the package (will be "unconfigured" due to missing deps — that's expected)
    dpkg -i /tmp/protonmail-bridge.deb || true; \
    # Resolve and install all missing dependencies declared by the .deb
    apt-get install -f -y --no-install-recommends; \
    rm /tmp/protonmail-bridge.deb; \
    rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# IMAP and SMTP ports (bound to 127.0.0.1 by docker-compose)
EXPOSE 1143 1025

ENTRYPOINT ["/entrypoint.sh"]
