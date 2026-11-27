#!/bin/sh
# Start one of the plugin's Python MCP servers from a shared venv.
#
# The venv lives in CLAUDE_PLUGIN_DATA, which survives plugin updates, and holds
# the locked runtime dependencies only (no dev group, no build of the project):
# the server code runs from this plugin version's src/ through PYTHONPATH. The
# venv is re-synced only when uv.lock or .python-version changes, so a normal
# start runs no uv at all.
#
# Usage: run-server.sh <module> [server args...]   e.g. run-server.sh server --config ...
set -eu

module=$1
shift
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
# shellcheck source=sync-once.sh
. "$root/scripts/sync-once.sh"
server="$root/server"
data=$(plugin_data)
venv="$data/venv"

hash=$(cat "$server/uv.lock" "$server/.python-version" | shasum -a 256 | cut -d' ' -f1)
[ -x "$venv/bin/python" ] || rm -f "$venv/.homelab-lock-hash"
sync_once "$venv" "$hash" env UV_PROJECT_ENVIRONMENT="$venv" \
  uv sync --quiet --frozen --no-dev --no-install-project --project "$server"

PYTHONPATH="$server/src${PYTHONPATH:+:$PYTHONPATH}" exec "$venv/bin/python" -m "homelab_mcp.$module" "$@"
