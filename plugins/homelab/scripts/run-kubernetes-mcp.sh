#!/bin/sh
# Starts kubernetes-mcp-server against the read-only kubeconfig.
#
# Read-only is enforced three times: the mcp-reader ServiceAccount's RBAC
# (the real boundary), the TOML read_only flag, and the `core` toolset only.
# The `config` toolset is left out on purpose: its configuration_view tool
# can print the kubeconfig in use (containers/kubernetes-mcp-server#1415).
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

root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
exec npx -y kubernetes-mcp-server@0.1.2 \
  --config "$root/config/kubernetes-mcp.toml" \
  --kubeconfig "$kubeconfig" \
  --toolsets core \
  --read-only \
  --disable-multi-cluster
