# Setup

## Prerequisites

- `uv` (builds the Python servers' venv), Node.js 22 or later with `npm`
  (installs the locked third-party servers),
  `mcp-grafana` from Homebrew (the `grafana` server), `kubectl`, and for
  drift: `kustomize`, `make`, `terragrunt`.
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

Inside a session the same two steps work as `/plugin marketplace add <url>`
and `/plugin install homelab@homelab`.

The defaults in the plugin options match the paths above. Change them in
`/plugin`, homelab, Configure, if your layout differs.

## 3. Permissions

Six servers are read-only at the credential level; allow them as whole
servers in `~/.claude/settings.json`:

```json
"allow": [
  "mcp__plugin_homelab_homelab",
  "mcp__plugin_homelab_argocd",
  "mcp__plugin_homelab_kubernetes",
  "mcp__plugin_homelab_longhorn",
  "mcp__plugin_homelab_grafana",
  "mcp__plugin_homelab_searxng"
]
```

A `grafana` server you configured by hand before the plugin shipped one
loads next to it, so every Grafana tool appears twice. Remove the old entry
from `~/.claude.json` (`mcpServers.grafana`) and `mcp__grafana` from
`permissions.allow`.

GitLab tools act as you. After the first `/mcp` connection, allow its read
tools by name and leave create, update and merge tools on "ask".

The `opengist` server also acts as you. Allow its reads by name and leave
`opengist_create_gist` on "ask":

```json
"allow": [
  "mcp__plugin_homelab_opengist__opengist_get_gist",
  "mcp__plugin_homelab_opengist__opengist_list_gists"
]
```

The `docmost` server writes as the Docmost member `claude`. Allow its reads
by name and leave `docmost_create_page`, `docmost_update_page` and
`docmost_set_labels` on "ask":

```json
"allow": [
  "mcp__plugin_homelab_docmost__docmost_list_spaces",
  "mcp__plugin_homelab_docmost__docmost_browse_tree",
  "mcp__plugin_homelab_docmost__docmost_search_pages",
  "mcp__plugin_homelab_docmost__docmost_get_page",
  "mcp__plugin_homelab_docmost__docmost_recent_changes",
  "mcp__plugin_homelab_docmost__docmost_list_versions",
  "mcp__plugin_homelab_docmost__docmost_get_version",
  "mcp__plugin_homelab_docmost__docmost_get_backlinks"
]
```

The `silverbullet` server writes as the SilverBullet account `claude`.
Allow its reads by name and leave `silverbullet_write_page` and
`silverbullet_append_page` on "ask":

```json
"allow": [
  "mcp__plugin_homelab_silverbullet__silverbullet_read_page",
  "mcp__plugin_homelab_silverbullet__silverbullet_list_pages"
]
```

## 4. Verify

```text
/mcp                    ten homelab servers connected
searxng_instance        version and enabled engines of the SearXNG instance
homelab_overview        every configured system answers
homelab_drift_report            argocd, externalsecrets, vault, komodo, dns, semaphore, pbs
```

The SessionStart hook prints a notice when the config, a secret file, the
ArgoCD token, the Grafana token or the kubeconfig is missing or not mode
600 or 400, when a `*_command` secret's program or a tool the servers run
(`uv`, `npm`, `node`, `mcp-grafana`, `kubectl`, `kustomize`, `make`) is not on
PATH, when a `[repos]` checkout is missing, or when the config has a section
or key no server reads, with the closest known name (`verfy_tls`: did you mean
`verify_tls`?). It prints nothing when the setup is complete. The same key
check runs on demand:

```bash
uv run --project plugins/homelab/server homelab-mcp --check-config
```

## Migrating to 1.0

1.0.0 names every `homelab` server tool after the config section of its
system. Ten tools changed; the other 60, and every other server, did not:

- `dns_zones` is now `technitium_zones`
- `dns_records` is now `technitium_records`
- `dns_dhcp_scopes` is now `technitium_dhcp_scopes`
- `dns_dhcp_leases` is now `technitium_dhcp_leases`
- `dns_stats` is now `technitium_stats`
- `dns_query_log` is now `technitium_query_log`
- `alerts_active` is now `alertmanager_alerts`
- `alerts_silences` is now `alertmanager_silences`
- `drift_report` is now `homelab_drift_report`
- `drift_render_check` is now `homelab_drift_render_check`

The `permissions.allow` entries above allow the `homelab` server as a whole,
so they need no change. An entry that names one of the old tools
(`mcp__plugin_homelab_homelab__dns_zones`) no longer matches anything;
rename it. Skills and rules in other repos that name an old tool get the new
name in their own MRs. At session start the plugin lists every file in the
open project's `.claude/` and `CLAUDE.md`, and in `~/.claude` (`CLAUDE.md`,
`settings.json`, `rules/`, `skills/`), that still names an old tool.

## Troubleshooting

- **`homelab` fails to start**: run it the way Claude Code does to see the
  error. `scripts/run-server.sh` keeps one venv for the five Python servers in
  `~/.claude/plugins/data/homelab-homelab/venv` and re-syncs it with `uv` only
  when `uv.lock` changes; delete that `venv` directory to force a rebuild.

  ```bash
  CLAUDE_PLUGIN_DATA=~/.claude/plugins/data/homelab-homelab \
    sh <plugin-dir>/scripts/run-server.sh server \
    --config ~/.config/homelab-mcp/config.toml
  ```

- **`searxng_search` answers 403**: the SearXNG instance does not list
  `json` in `search.formats` (helm-charts searxng chart). Check with:

  ```bash
  curl -s -o /dev/null -w '%{http_code}\n' 'https://search.example.com/search?q=x&format=json'
  ```

- **A drift check says error, "not found on PATH"**: the tool is missing
  from the PATH Claude Code was started with.
- **Doctor checks fail with PyYAML errors**: the repo scripts run with the
  first `python3` on PATH. Install PyYAML for it (`python3 -m pip install
  --user pyyaml`).
- **`kubernetes` refuses to start "holds a client certificate"**: the
  configured kubeconfig is an admin one. Point it at the read-only file.
