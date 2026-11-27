# shellcheck shell=sh
# Sourced by run-server.sh and run-node-server.sh: keep a directory in
# CLAUDE_PLUGIN_DATA in step with a lockfile, syncing at most once while
# several servers start at the same time.
#
# sync_once <dir> <hash> <command...>
#   Runs <command...> when <dir>/.homelab-lock-hash is not <hash>, under a mkdir
#   lock that other starts wait for, then records <hash>. A lock older than ten
#   minutes belongs to a start that died mid-sync and is cleared.

plugin_data() {
  data=${CLAUDE_PLUGIN_DATA:?CLAUDE_PLUGIN_DATA is not set; start this through Claude Code}
  # Claude Code exports it to MCP processes but does not substitute
  # ${CLAUDE_PLUGIN_DATA} inside a server's env block; a literal or relative
  # value would build in a directory this script and its tools disagree about.
  case "$data" in
    /*) printf '%s\n' "$data" ;;
    *) echo "CLAUDE_PLUGIN_DATA must be an absolute path, got: $data" >&2; return 1 ;;
  esac
}

sync_once() {
  dir=$1 hash=$2
  shift 2
  stamp="$dir/.homelab-lock-hash"
  [ "$(cat "$stamp" 2>/dev/null)" = "$hash" ] && return 0
  lock="$dir.lock"
  mkdir -p "$(dirname "$dir")"
  waited=0
  until mkdir "$lock" 2>/dev/null; do
    if [ -n "$(find "$lock" -maxdepth 0 -mmin +10 2>/dev/null)" ]; then
      rmdir "$lock" 2>/dev/null || true
      continue
    fi
    sleep 1
    waited=$((waited + 1))
    [ "$waited" -lt 300 ] || { echo "timed out waiting for $lock" >&2; return 1; }
  done
  if [ "$(cat "$stamp" 2>/dev/null)" != "$hash" ]; then
    if ! "$@" >&2; then
      rmdir "$lock" 2>/dev/null || true
      return 1
    fi
    mkdir -p "$dir"
    printf '%s\n' "$hash" >"$stamp"
  fi
  rmdir "$lock" 2>/dev/null || true
}
