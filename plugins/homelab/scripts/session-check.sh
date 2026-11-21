#!/bin/sh
# SessionStart notice: say which homelab MCP inputs are missing, readable by
# other users, or need a tool that is not on PATH. Prints paths, modes and
# command names only, never file contents.
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

# Secrets and the config must be owner-only: 600 or 400.
check_mode() {
  mode=$(stat -f '%Lp' "$1" 2>/dev/null || stat -c '%a' "$1" 2>/dev/null)
  case "$mode" in
    600|400) ;;
    *) note "$2 is mode $mode, expected 600: $1" ;;
  esac
}

check_file() {
  [ -n "$1" ] || return 0
  f=$(expand "$1")
  if [ ! -f "$f" ]; then
    note "$2 missing: $f"
    return 0
  fi
  check_mode "$f" "$2"
}

config_file=${1:-${CLAUDE_PLUGIN_OPTION_CONFIG_FILE:-~/.config/homelab-mcp/config.toml}}
argocd_token_file=${2:-${CLAUDE_PLUGIN_OPTION_ARGOCD_TOKEN_FILE:-~/.config/homelab-mcp/secrets/argocd}}
kubeconfig=${3:-${CLAUDE_PLUGIN_OPTION_KUBECONFIG:-~/.config/homelab-mcp/kubeconfig-readonly}}
grafana_token_file=${CLAUDE_PLUGIN_OPTION_GRAFANA_TOKEN_FILE:-~/.config/homelab-mcp/secrets/grafana}

check_file "$config_file" "config"
check_file "$argocd_token_file" "ArgoCD token"
check_file "$kubeconfig" "read-only kubeconfig"
check_file "$grafana_token_file" "Grafana token"

# uv builds the custom servers' venv, npm installs the locked third-party ones,
# node runs them, mcp-grafana (Homebrew) is the grafana server; drift shells
# out to the rest.
for tool in uv npm node mcp-grafana kubectl kustomize make; do
  command -v "$tool" >/dev/null 2>&1 || note "$tool not on PATH"
done

cfg=$(expand "$config_file")
if [ -r "$cfg" ]; then
  dir=$(dirname "$cfg")
  resolve() { case "$1" in /*|\~/*) expand "$1" ;; *) printf '%s\n' "$dir/$1" ;; esac; }

  # *_file = "..." entries are secret files (relative to the config dir).
  while IFS= read -r ref; do
    [ -n "$ref" ] || continue
    path=$(resolve "$ref")
    if [ -f "$path" ]; then
      check_mode "$path" "secret file"
    else
      note "secret file missing: $path"
    fi
  done <<EOF
$(sed -n 's/^[^#]*_file[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$cfg")
EOF

  # *_command = ["prog", ...] entries: the program must exist.
  while IFS= read -r prog; do
    [ -n "$prog" ] || continue
    command -v "$prog" >/dev/null 2>&1 || note "secret command not on PATH: $prog"
  done <<EOF
$(sed -n 's/^[^#]*_command[[:space:]]*=[[:space:]]*\[[[:space:]]*"\([^"]*\)".*/\1/p' "$cfg")
EOF

  # [repos] checkouts used by homelab_drift_report and homelab_drift_render_check.
  while IFS= read -r ref; do
    [ -n "$ref" ] || continue
    path=$(expand "$ref")
    [ -d "$path" ] || note "repo checkout missing: $path"
  done <<EOF
$(sed -n '/^\[repos\]/,/^\[/s/^[a-z_]*[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$cfg")
EOF

  # Unknown sections and keys (typos), from the server's own key table. Only
  # when run-server.sh has already built the shared venv: building one here
  # would outlast the hook.
  python="${CLAUDE_PLUGIN_DATA:-}/venv/bin/python"
  src="${CLAUDE_PLUGIN_ROOT:-$(dirname "$0")/..}/server/src"
  if [ -n "${CLAUDE_PLUGIN_DATA:-}" ] && [ -x "$python" ]; then
    while IFS= read -r line; do
      [ -n "$line" ] && note "${line#"$cfg": }"
    done <<EOF
$(PYTHONPATH="$src" "$python" -m homelab_mcp.server --config "$cfg" --check-config 2>/dev/null)
EOF
  fi
fi

if [ -n "$problems" ]; then
  printf 'homelab plugin: setup incomplete (tools for these systems will report errors):%s\nSee docs/SETUP.md in example-org/mcp-plugins.\n' "$problems"
fi
sh "$(dirname "$0")/renamed-tools.sh"
exit 0
