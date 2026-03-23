#!/usr/bin/env bash
# start.sh — source OpenStack credentials and launch the Swift MCP server.
#
# Usage: start.sh [CREDENTIALS_FILE] [PORT]
#
# CREDENTIALS_FILE defaults to /etc/openstack/openrc.sh (bind-mount inside LXD container)
# PORT defaults to 8000; the server listens on 0.0.0.0:<PORT> (SSE/HTTP transport)

set -euo pipefail

DEFAULT_OPENRC="/etc/openstack/openrc.sh"
OPENRC_PATH="${1:-$DEFAULT_OPENRC}"
export MCP_PORT="${2:-${MCP_PORT:-8000}}"
export MCP_HOST="${MCP_HOST:-0.0.0.0}"
# MCP_HOST_ADDR overrides the IP returned in staging URLs (auto-detected if unset)

SERVER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f "$OPENRC_PATH" ]]; then
    echo "ERROR: credentials file not found at '$OPENRC_PATH'" >&2
    echo "       Usage: $0 [CREDENTIALS_FILE] [PORT]" >&2
    exit 1
fi

# Export every variable defined in the openrc.sh
set -a
# shellcheck source=/dev/null
source "$OPENRC_PATH"
set +a

# Sanity-check that the essential Keystone variables are present
for var in OS_AUTH_URL OS_USERNAME OS_PASSWORD OS_PROJECT_NAME; do
    if [[ -z "${!var:-}" ]]; then
        echo "ERROR: '$var' is not set in $OPENRC_PATH" >&2
        exit 1
    fi
done

echo "Starting Swift MCP server on ${MCP_HOST}:${MCP_PORT}" >&2
cd "$SERVER_DIR"
exec uv run python server.py
