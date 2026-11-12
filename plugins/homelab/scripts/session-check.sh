#!/bin/sh
# SessionStart notice: say which homelab MCP inputs are missing or readable by
# other users. Prints paths and modes only, never file contents.
#
# Paths come from CLAUDE_PLUGIN_OPTION_* (Claude Code exports one per
# userConfig option), falling back to the manifest defaults. The hook must not
# pass ${user_config.*} in args: that substitution fails with "Plugin option
# isn't set" until the user saves the option in /plugin manage, while MCP
# servers get the default. Positional args override both, for manual runs.
# Usage: session-check.sh [config.toml] [argocd token file] [kubeconfig]

expand() { case "$1" in \~/*) printf '%s\n' "$HOME/${1#\~/}" ;; *) printf '%s\n' "$1" ;; esac; }

problems=""
note() { problems="$problems
  - $1"; }

check_file() {
  f=$(expand "$1")
  [ -n "$1" ] || return 0
  if [ ! -e "$f" ]; then
    note "$2 missing: $f"
    return 0
  fi
  mode=$(stat -f '%Lp' "$f" 2>/dev/null || stat -c '%a' "$f" 2>/dev/null)
  case "$mode" in
    *00) ;;
    *) note "$2 is mode $mode, expected 600: $f" ;;
  esac
}

config_file=${1:-${CLAUDE_PLUGIN_OPTION_CONFIG_FILE:-~/.config/homelab-mcp/config.toml}}
argocd_token_file=${2:-${CLAUDE_PLUGIN_OPTION_ARGOCD_TOKEN_FILE:-~/.config/homelab-mcp/secrets/argocd}}
kubeconfig=${3:-${CLAUDE_PLUGIN_OPTION_KUBECONFIG:-~/.config/homelab-mcp/kubeconfig-readonly}}

check_file "$config_file" "config"
check_file "$argocd_token_file" "ArgoCD token"
check_file "$kubeconfig" "read-only kubeconfig"

cfg=$(expand "$config_file")
if [ -r "$cfg" ]; then
  dir=$(dirname "$cfg")
  # Secret files referenced as *_file = "..." in the config.
  sed -n 's/^[^#]*_file[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$cfg" | while IFS= read -r ref; do
    case "$ref" in /*|\~/*) path=$(expand "$ref") ;; *) path="$dir/$ref" ;; esac
    [ -e "$path" ] || echo "  - secret file missing: $path"
  done > "${TMPDIR:-/tmp}/homelab-mcp-check.$$"
  if [ -s "${TMPDIR:-/tmp}/homelab-mcp-check.$$" ]; then
    problems="$problems
$(cat "${TMPDIR:-/tmp}/homelab-mcp-check.$$")"
  fi
  rm -f "${TMPDIR:-/tmp}/homelab-mcp-check.$$"
fi

if [ -n "$problems" ]; then
  printf 'homelab plugin: setup incomplete (tools for these systems will report errors):%s\nSee docs/SETUP.md in example-org/mcp-plugins.\n' "$problems"
fi
exit 0
