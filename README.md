# mcp-plugins

A private Claude Code plugin marketplace for the homelab. It ships one
plugin, `homelab`, that gives Claude a read-only view of the lab through MCP.
Changes still go through Git and the repo skills in argo-apps and
proxmox-infra.

- **`homelab`** (this repo): PVE, PBS, Unraid, Technitium, Pi-hole, Komodo,
  Semaphore, Traefik, Alertmanager, ntfy, Vault metadata, GitLab job logs and
  drift. Only GET or allowlisted read calls, each with the narrowest
  credential the backend offers (Pi-hole has no read-only role).
- **`argocd`**: applications, resource trees, events, workload logs.
  `MCP_READ_ONLY=true` and an account bound to `role:readonly`.
- **`kubernetes`**: pods, events, CRDs, `top`. The `mcp-reader`
  ServiceAccount's RBAC, plus `read_only` and the `core` toolset.
- **`longhorn`**: volumes, replicas, backups, nodes. `--read-only`, so
  non-GET requests are refused.
- **`grafana`**: PromQL, Loki logs, dashboards and alert rules. The
  Homebrew `mcp-grafana` with `--disable-write` and a Viewer service account.
- **`gitlab`**: MRs, pipelines and issues on source.example.com, as your OAuth
  identity. Its write tools stay on "ask".
- **`opengist`** (this repo): pushes a snippet to gist.example.com as you,
  so it can be opened on the work machine. One write tool
  (`opengist_create_gist`, kept on "ask") and two reads.
- **`docmost`** (this repo): the operator handbook on docmost.example.com,
  as the Docmost member `claude`. Reads (search, page as Markdown, tree,
  versions) and three writes (create, update, labels), kept on "ask".
- **`silverbullet`** (this repo): scratch pages in the SilverBullet space
  the user keeps open on the work machine, as a dedicated non-admin
  account. Two writes (create or overwrite, append), kept on "ask", and
  two reads.
- **`searxng`** (this repo): web search through the homelab SearXNG
  (search.example.com, no account) and any page as Markdown in chunks,
  LAN hosts included. Read-only; it replaces a paid search API.

The UniFi plugin stays separate and keeps its own policy flags.

## Layout

```text
.claude-plugin/marketplace.json   marketplace "homelab"
plugins/homelab/
  .claude-plugin/plugin.json      MCP servers, userConfig, SessionStart check
  server/                         Python package homelab-mcp (uv project)
  scripts/                        run-server.sh, run-node-server.sh, with-secret.sh, ...
  third-party/                    package.json + lockfile of the npm MCP servers
  config/kubernetes-mcp.toml      kubernetes-mcp-server settings
  skills/homelab-ops/            which tool answers which question
  skills/git-commit/, docs/      shared by every homelab repo
.claude/                          skills and rules for developing this repo
config.example.toml               template for ~/.config/homelab-mcp/config.toml
docs/                             SETUP, CREDENTIALS, TOOLS
```

## Quick start

1. [docs/CREDENTIALS.md](docs/CREDENTIALS.md): create the read-only
   identities.
2. [docs/SETUP.md](docs/SETUP.md): config directory, install, verify.
3. Install the plugin:

   ```bash
   claude plugin marketplace add https://source.example.com/example-org/mcp-plugins.git
   claude plugin install homelab@homelab
   ```

## Develop

```bash
cd plugins/homelab/server
uv sync
uv run ruff check src tests && uv run ruff format --check src tests
uv run pyright
uv run pytest --cov
claude plugin validate --strict ../../..   # from the server dir: the repo root
```

`make release` runs the live smoke test and the evals, then moves the
version in its four places (`plugin.json`, `pyproject.toml`, `__init__.py`,
`uv.lock`) by the bump the Conventional Commits call for (`PART=` overrides
it) and writes the `CHANGELOG.md` section; installed copies update on the
next `claude plugin marketplace update homelab` and `claude plugin update
homelab@homelab`.

## Documentation

[docs/README.md](docs/README.md) indexes SETUP, CREDENTIALS and the
generated tool reference, [docs/TOOLS.md](docs/TOOLS.md).

## License

MIT, see [LICENSE](LICENSE).
