#!/usr/bin/env bash
# lxd_setup.sh — Create and configure the LXD container for the Swift MCP server.
#
# Usage:
#   ./lxd_setup.sh [CONTAINER_NAME] [OPENRC_HOST_PATH]
#
# Defaults:
#   CONTAINER_NAME   = swift-mcp
#   OPENRC_HOST_PATH = /etc/openstack/openrc.sh  (path on the HOST)
#
# The openrc.sh is bind-mounted read-only into the container at
#   /etc/openstack/openrc.sh
# so that credentials never have to be copied into the image.

set -euo pipefail

CONTAINER="${1:-swift-mcp-server}"
OPENRC_HOST="${2:-$HOME/external_env_files/ps5_swift}"
INSTALL_DIR="/opt/swift-mcp"

# ── Preflight ────────────────────────────────────────────────────────────────
if ! command -v lxc &>/dev/null; then
    echo "ERROR: lxc not found. Install LXD first." >&2
    exit 1
fi

if [[ ! -f "$OPENRC_HOST" ]]; then
    echo "ERROR: openrc.sh not found at '$OPENRC_HOST'" >&2
    echo "       Pass the correct path as the second argument." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if lxc info "$CONTAINER" &>/dev/null; then
    echo "==> Container '$CONTAINER' already exists — ensuring it is running..."
    lxc start "$CONTAINER" 2>/dev/null || true
else
    echo "==> Creating LXD container '$CONTAINER' (Ubuntu 24.04)..."
    lxc launch ubuntu:24.04 "$CONTAINER"
fi

echo "==> Waiting for container to be ready..."
lxc exec "$CONTAINER" -- cloud-init status --wait --long

# ── Bind-mount the credentials file (read-only) ──────────────────────────────
echo "==> Bind-mounting openrc.sh (read-only) from host: $OPENRC_HOST"
lxc config device add "$CONTAINER" openrc disk \
    source="$OPENRC_HOST" \
    path=/etc/openstack/openrc.sh \
    readonly=true 2>/dev/null || \
    echo "    Device 'openrc' already configured — skipping."

# ── Install uv inside the container ──────────────────────────────────────────
echo "==> Installing uv..."
lxc exec "$CONTAINER" -- bash -c \
    'command -v uv &>/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh'
lxc exec "$CONTAINER" -- ln -sf /root/.local/bin/uv /usr/local/bin/uv

# ── Copy project files into container ────────────────────────────────────────
echo "==> Copying project files to $INSTALL_DIR..."
lxc exec "$CONTAINER" -- mkdir -p "$INSTALL_DIR"
for f in server.py pyproject.toml start.sh; do
    lxc file push "$SCRIPT_DIR/$f" "$CONTAINER$INSTALL_DIR/$f"
done
lxc exec "$CONTAINER" -- chmod +x "$INSTALL_DIR/start.sh"

# ── Pre-install dependencies ──────────────────────────────────────────────────
echo "==> Installing Python dependencies (uv sync)..."
lxc exec "$CONTAINER" --cwd "$INSTALL_DIR" -- uv sync

# ── Summary ───────────────────────────────────────────────────────────────────
CONTAINER_IP=$(lxc list "$CONTAINER" -c 4 --format csv | cut -d' ' -f1)

cat <<EOF

==> Done. Container '$CONTAINER' is ready.

Start the MCP server (runs in foreground, listens on port 8000):
  lxc exec $CONTAINER -- $INSTALL_DIR/start.sh

The server will be reachable at:
  http://${CONTAINER_IP:-<container-ip>}:8000/sse

To use a different port:
  lxc exec $CONTAINER -- bash -c 'MCP_PORT=9000 $INSTALL_DIR/start.sh'

To open a shell for debugging:
  lxc exec $CONTAINER -- bash

To check the container IP:
  lxc list $CONTAINER

To stop the container:
  lxc stop $CONTAINER
EOF
