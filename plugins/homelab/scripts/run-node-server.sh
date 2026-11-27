#!/bin/sh
# Start a third-party npm MCP server from third-party/package-lock.json.
#
# npx resolved each server's dependency tree at every launch with nothing
# pinning the transitive packages. Here npm ci installs exactly the locked tree
# (integrity-checked, install scripts off) into CLAUDE_PLUGIN_DATA/node, once
# per change of the lockfile, and the server's own bin runs from there.
#
# Usage: run-node-server.sh <bin> [server args...]   e.g. run-node-server.sh longhorn-mcp --read-only
set -eu

bin=$1
shift
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
# shellcheck source=sync-once.sh
. "$root/scripts/sync-once.sh"
locked="$root/third-party"
data=$(plugin_data)
node_dir="$data/node"

install() {
  mkdir -p "$node_dir"
  cp "$locked/package.json" "$locked/package-lock.json" "$node_dir/"
  # From inside the real path, not --prefix: given a path through a symlink
  # (macOS /var -> /private/var), npm ci treats the root as a link named after
  # the directory and refuses with "Missing: node@ from lock file".
  (cd -P "$node_dir" && npm ci --ignore-scripts --omit=dev --no-audit --no-fund --loglevel=error)
}

hash=$(shasum -a 256 <"$locked/package-lock.json" | cut -d' ' -f1)
[ -x "$node_dir/node_modules/.bin/$bin" ] || rm -f "$node_dir/.homelab-lock-hash"
sync_once "$node_dir" "$hash" install

exec "$node_dir/node_modules/.bin/$bin" "$@"
