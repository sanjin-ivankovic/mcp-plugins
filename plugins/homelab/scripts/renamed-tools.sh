#!/bin/sh
# SessionStart notice: list the files in the open project and in ~/.claude that
# still name a homelab tool that was renamed (config/removed_tools.txt). Skills,
# rules and permission entries with an old name match nothing, silently.
#
# Scans the project's .claude/ and CLAUDE.md, and the user's CLAUDE.md,
# settings.json, rules/ and skills/. Prints file, line and the rename, never
# the line itself. Always exits 0.
# Usage: renamed-tools.sh [project dir]

root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
list="$root/config/removed_tools.txt"
project=${1:-${CLAUDE_PROJECT_DIR:-$PWD}}
[ -r "$list" ] || exit 0

set --
for path in "$project/.claude" "$project/CLAUDE.md" "$HOME/.claude/CLAUDE.md" \
  "$HOME/.claude/settings.json" "$HOME/.claude/rules" "$HOME/.claude/skills"; do
  [ -e "$path" ] && set -- "$@" "$path"
done
[ $# -gt 0 ] || exit 0

found=""
tilde='~' # printed for $HOME, not expanded
while read -r old new; do
  case "$old" in '' | \#*) continue ;; esac
  # A single "_" before the old name makes a longer name
  # (homelab_drift_report); "__" is the separator in a full mcp__ tool name.
  while IFS= read -r hit; do
    [ -n "$hit" ] || continue
    case "$hit" in
      "$project"/*) hit=${hit#"$project"/} ;;
      "$HOME"/*) hit="$tilde/${hit#"$HOME"/}" ;;
    esac
    found="$found
  - $hit: $old is now $new"
  done <<EOF
$(grep -rnIE --exclude-dir=worktrees -- "(^|[^a-z0-9_]|__)${old}([^a-z0-9_]|$)" "$@" 2>/dev/null |
    sed 's/^\([^:]*:[0-9]*\):.*/\1/')
EOF
done <"$list"

# Thirty lines are enough to act on; the next session lists the rest.
if [ -n "$found" ]; then
  printf 'homelab plugin: renamed MCP tools still named here (the old names match nothing; rename them):%s\n' \
    "$(printf '%s\n' "$found" | head -n 31)"
fi
exit 0
