#!/bin/sh
# Starts kubernetes-mcp-server against the read-only kubeconfig.
#
# Read-only is enforced twice: the mcp-reader ServiceAccount's RBAC (the real
# boundary), then read_only and the `core` toolset in config/kubernetes-mcp.toml.
# The `config` toolset is left out on purpose: its configuration_view tool
# can print the kubeconfig in use (containers/kubernetes-mcp-server#1415).
#
# The server accepts only --config and --config-dir; any other flag exits with
# "unknown flag". The kubeconfig path is per user, so it goes into a one-line
# drop-in under the cache dir, rewritten on every start.
set -eu

kubeconfig=${1:?usage: run-kubernetes-mcp.sh <kubeconfig>}
case "$kubeconfig" in \~/*) kubeconfig="$HOME/${kubeconfig#\~/}" ;; esac
if [ ! -r "$kubeconfig" ]; then
  echo "kubernetes-mcp: cannot read $kubeconfig; run scripts/make-readonly-kubeconfig.sh first" >&2
  exit 66
fi
if grep -q 'client-key-data' "$kubeconfig"; then
  echo "kubernetes-mcp: $kubeconfig holds a client certificate; refusing (admin kubeconfig?)" >&2
  exit 65
fi
case "$kubeconfig" in
  *\"* | *\\*)
    echo "kubernetes-mcp: $kubeconfig contains a quote or backslash; rename it" >&2
    exit 64
    ;;
esac

root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
dropin="${XDG_CACHE_HOME:-$HOME/.cache}/homelab-mcp/kubernetes-mcp.d"
mkdir -p "$dropin"
# Write then rename, so a second session starting at the same time never
# reads a half-written file.
tmp=$(mktemp "$dropin/.kubeconfig.XXXXXX")
printf 'kubeconfig = "%s"\n' "$kubeconfig" >"$tmp"
mv -f "$tmp" "$dropin/kubeconfig.toml"

exec npx -y kubernetes-mcp-server@0.1.2 \
  --config "$root/config/kubernetes-mcp.toml" \
  --config-dir "$dropin"
