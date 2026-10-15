#!/usr/bin/env bash
# Install the quadlet unit for the Bitcoin Node MCP server.
#
# Symlinks this repo's deploy/ unit into the user quadlet directory
# (~/.config/containers/systemd) and reloads systemd, so the checkout is the
# single source of truth. Also seeds the bearer-token env file and, on a
# fresh host, the upstream RPC credentials -- reading them from the running
# bitcoind container when it can, so nothing has to be copied by hand.
#
# Idempotent: re-running refreshes the symlink and leaves existing env files
# untouched. No secret is committed or printed -- the files land in
# ~/.config/bitcoin-node-mcp/ (mode 600).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="$REPO_DIR/deploy"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
QUADLET_DIR="$CONFIG_HOME/containers/systemd"
ENV_DIR="$CONFIG_HOME/bitcoin-node-mcp"
TOKEN_FILE="$ENV_DIR/token.env"
UPSTREAM_FILE="$ENV_DIR/upstream.env"

# npm-network.network is shared with the rest of the stack; only link our
# own unit, and add the network only if no other repo already provides it.
UNITS=(bitcoin-node-mcp.container)

log() { printf '==> %s\n' "$*"; }

# A hex token, from openssl if present, else /dev/urandom.
rand_hex() {
    openssl rand -hex 32 2>/dev/null && return 0
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
}

# Write a file with a 600 mode from a here-doc.
write_secret() {
    local path="$1"
    local old_umask
    old_umask=$(umask)
    umask 077
    cat > "$path"
    umask "$old_umask"
    chmod 600 "$path"
}

mkdir -p "$ENV_DIR"

# 1. bearer token ---------------------------------------------------------
if [ -s "$TOKEN_FILE" ]; then
    log "token env present: $TOKEN_FILE"
else
    token=""
    # Reuse the token already in service so existing clients keep working.
    if command -v podman >/dev/null 2>&1; then
        token=$(podman exec bitcoin-node-mcp printenv BITCOIN_MCP_TOKEN 2>/dev/null || true)
    fi
    if [ -n "$token" ]; then
        log "reused the existing bearer token"
    else
        token=$(rand_hex)
        log "generated a new bearer token -- update clients to match"
    fi
    write_secret "$TOKEN_FILE" <<EOF
BITCOIN_MCP_TOKEN=$token
EOF
    log "wrote $TOKEN_FILE (mode 600)"
fi

# 2. upstream RPC credentials --------------------------------------------
if [ -s "$UPSTREAM_FILE" ]; then
    log "upstream env present: $UPSTREAM_FILE"
else
    user=""
    password=""
    # Derive from the running daemon rather than asking the operator to copy
    # secrets. bitcoind owns its bitcoin.conf in a bind mount, so read it
    # through the container.
    if command -v podman >/dev/null 2>&1; then
        conf=$(podman exec bitcoind cat /data/.bitcoin/bitcoin.conf 2>/dev/null || true)
        user=$(printf '%s\n' "$conf" | sed -n 's/^rpcuser=//p' | head -n1)
        password=$(printf '%s\n' "$conf" | sed -n 's/^rpcpassword=//p' | head -n1)
    fi
    if [ -n "$user" ] && [ -n "$password" ]; then
        log "read bitcoind RPC credentials from the running container"
    else
        log "could not read bitcoind credentials; fill in $UPSTREAM_FILE by hand"
    fi
    write_secret "$UPSTREAM_FILE" <<EOF
# Upstream credentials for the Bitcoin Core node this server reads.
# deploy/install.sh wrote this file (mode 600); the unit reads it via
# EnvironmentFile. Re-run install.sh after editing, or just restart the
# service.
BITCOIN_RPC_USER=$user
BITCOIN_RPC_PASSWORD=$password
EOF
    log "wrote $UPSTREAM_FILE (mode 600)"
fi

# 3. symlink units into the quadlet search path ---------------------------
mkdir -p "$QUADLET_DIR"

# The shared network unit: created by whichever repo deploys first.
NETWORK_TARGET="$QUADLET_DIR/npm-network.network"
if [ ! -e "$NETWORK_TARGET" ]; then
    ln -sfn "$DEPLOY_DIR/npm-network.network" "$NETWORK_TARGET"
    log "linked npm-network.network"
else
    log "npm-network.network already present"
fi

for unit in "${UNITS[@]}"; do
    target="$QUADLET_DIR/$unit"
    if [ -e "$target" ] && [ ! -L "$target" ]; then
        mv -f "$target" "$target.bak"
        log "backed up existing $unit -> $unit.bak"
    fi
    ln -sfn "$DEPLOY_DIR/$unit" "$target"
    log "linked $unit"
done

# 4. reload systemd so the generator re-reads the units -------------------
if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload
    log "systemd --user reloaded"
fi

cat <<EOF

Done. Next:
  ./deploy.sh                                  # install, build, restart, wait healthy
  # or, units only:
  systemctl --user start bitcoin-node-mcp.service

Open the published port on the LAN:
  sudo firewall-cmd --permanent --add-port=8101/tcp && sudo firewall-cmd --reload

Clients must send the bearer token from
  $TOKEN_FILE
as \`Authorization: Bearer <token>\`, pointed at
  http://<host>:8101/mcp
EOF
