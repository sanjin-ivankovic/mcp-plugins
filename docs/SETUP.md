# Setup

## Prerequisites

- `uv` (runs the Python server), Node 22 with `npx` (third-party servers),
  `kubectl`, and for drift: `kryptos`, `kustomize`, `make`, `terragrunt`.
- Local checkouts of argo-apps and proxmox-infra for the drift tools.
- The read-only identities from [CREDENTIALS.md](CREDENTIALS.md).

## 1. Config directory

```bash
mkdir -p -m 700 ~/.config/homelab-mcp/secrets
cp config.example.toml ~/.config/homelab-mcp/config.toml
chmod 600 ~/.config/homelab-mcp/config.toml
```

Edit the URLs if they differ. Drop any section you do not want: its tools
then report "not configured" and `homelab_overview` skips it.

## 2. Install the plugin

```bash
claude plugin marketplace add https://source.example.com/example-org/mcp-plugins.git
claude plugin install homelab@homelab
```

Inside a session the same is one step:

```text
/plugin install homelab --marketplace https://source.example.com/example-org/mcp-plugins.git
```

The defaults in the plugin options match the paths above. Change them in
`/plugin`, homelab, Configure, if your layout differs.

## 3. Permissions

Every server in this plugin is read-only at the credential level, so allow
them as whole servers in `~/.claude/settings.json`:

```json
"allow": [
  "mcp__plugin_homelab_homelab",
  "mcp__plugin_homelab_argocd",
  "mcp__plugin_homelab_kubernetes",
  "mcp__plugin_homelab_longhorn"
]
```

GitLab tools act as you. After the first `/mcp` connection, allow its read
tools by name and leave create, update and merge tools on "ask".

## 4. Verify

```text
/mcp                    five homelab servers connected
homelab_overview        every configured system answers
drift_report            argocd, sealedsecrets, komodo, kryptos, dns
```

The SessionStart hook prints a notice when the config, a secret file, the
ArgoCD token or the kubeconfig is missing or readable by other users.

## Troubleshooting

- **`homelab` fails to start**: run the server by hand to see the error.

  ```bash
  uv run --project <plugin-dir>/server homelab-mcp \
    --config ~/.config/homelab-mcp/config.toml
  ```

- **A drift check says error, "not found on PATH"**: the tool is missing
  from the PATH Claude Code was started with.
- **Doctor checks fail with PyYAML errors**: the repo scripts run with the
  first `python3` on PATH. Install PyYAML for it (`python3 -m pip install
  --user pyyaml`).
- **`kubernetes` refuses to start "holds a client certificate"**: the
  configured kubeconfig is an admin one. Point it at the read-only file.
