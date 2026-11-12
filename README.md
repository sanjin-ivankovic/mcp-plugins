# mcp-plugins

A private Claude Code plugin marketplace for the homelab. It ships one
plugin, `homelab`, that gives Claude a read-only view of the lab through MCP.
Changes still go through Git and the repo skills in argo-apps and
proxmox-infra.

- **`homelab`** (this repo): PVE, PBS, Unraid, Technitium, Pi-hole, Komodo,
  Semaphore and drift. Only GET or allowlisted read calls, each with the
  narrowest credential the backend offers (Pi-hole has no read-only role).
- **`argocd`**: applications, resource trees, events, workload logs.
  `MCP_READ_ONLY=true` and an account bound to `role:readonly`.
- **`kubernetes`**: pods, events, CRDs, `top`. The `mcp-reader`
  ServiceAccount's RBAC, plus `read_only` and the `core` toolset.
- **`longhorn`**: volumes, replicas, backups, nodes. `--read-only`, so
  non-GET requests are refused.
- **`gitlab`**: MRs, pipelines and issues on source.example.com, as your OAuth
  identity. Its write tools stay on "ask".

The UniFi plugin stays separate and keeps its own policy flags.

## Layout

```text
.claude-plugin/marketplace.json   marketplace "homelab"
plugins/homelab/
  .claude-plugin/plugin.json      MCP servers, userConfig, SessionStart check
  server/                         Python package homelab-mcp (uv project)
  scripts/                        with-secret.sh, run-kubernetes-mcp.sh, ...
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

   ```text
   /plugin install homelab --marketplace https://source.example.com/example-org/mcp-plugins.git
   ```

## Develop

```bash
cd plugins/homelab/server
uv sync
uv run ruff check src tests && uv run ruff format --check src tests
uv run pytest
claude plugin validate ../../..   # from the server dir: the repo root
```

Bump `version` in `plugins/homelab/.claude-plugin/plugin.json`,
`server/pyproject.toml` and `server/src/homelab_mcp/__init__.py` together;
installed copies update on the next `claude plugin marketplace update
homelab` and `claude plugin update homelab@homelab`.

## Documentation

[docs/README.md](docs/README.md) indexes SETUP, CREDENTIALS and the
generated tool reference, [docs/TOOLS.md](docs/TOOLS.md).

## License

MIT, see [LICENSE](LICENSE).
