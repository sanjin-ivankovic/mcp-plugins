#!/bin/sh
# One-off: build the read-only kubeconfig for the kubernetes MCP server and
# the drift report from the mcp-reader ServiceAccount token.
#
# Usage: make-readonly-kubeconfig.sh [admin-context] [output]
#   admin-context  context used once to read the token (default admin@example)
#   output         default ~/.config/homelab-mcp/kubeconfig-readonly
#
# Needs the argo-apps manifests for mcp-reader (kube-system) to be synced.
# The token is written straight to the file and never printed.
set -eu

ctx=${1:-admin@example}
out=${2:-$HOME/.config/homelab-mcp/kubeconfig-readonly}
ns=kube-system
secret=mcp-reader-token

umask 077
mkdir -p "$(dirname "$out")"

server=$(kubectl config view --raw --minify --context "$ctx" -o jsonpath='{.clusters[0].cluster.server}')
ca=$(kubectl config view --raw --minify --context "$ctx" -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')
token=$(kubectl --context "$ctx" -n "$ns" get secret "$secret" -o jsonpath='{.data.token}' | base64 -d)
[ -n "$token" ] || { echo "no token in $ns/$secret yet; is it synced?" >&2; exit 1; }

tmp=$(mktemp "$out.XXXXXX")
cat > "$tmp" <<EOF
apiVersion: v1
kind: Config
clusters:
  - name: example
    cluster:
      server: $server
      certificate-authority-data: $ca
users:
  - name: mcp-reader
    user:
      token: $token
contexts:
  - name: mcp-reader@example
    context:
      cluster: example
      user: mcp-reader
current-context: mcp-reader@example
EOF
mv "$tmp" "$out"
unset token

echo "wrote $out"
for check in "get pods -A" "get applications.argoproj.io -A" "get secrets -A" "delete pods -A" "create deployments -n default"; do
  # shellcheck disable=SC2086 # word splitting of the verb/resource pair is intended
  printf '  can-i %-32s %s\n' "$check" "$(kubectl --kubeconfig "$out" auth can-i $check 2>/dev/null || true)"
done
echo "expected: yes, yes, no, no, no"
