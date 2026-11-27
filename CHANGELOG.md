# Changelog

Releases of the `homelab` plugin. `make release PART=patch|minor|major` adds
each section; the CI `release` job publishes it as the GitLab release of the
matching `vX.Y.Z` tag.

## 1.2.0 - 2026-11-20

### Features

- **homelab**: read ntfy notifications with ntfy_messages

### Dependencies

- **deps**: Update registry.example.com/homelab/ci-components/ci:latest Docker
  digest to e54c6a0
- **deps**: Update ruff to v0.17.4

## 1.1.2 - 2026-11-19

### Fixes

- **grafana**: turn off mcp-grafana's anonymous usage statistics

## 1.1.1 - 2026-11-19

### Features

- **renovate**: automerge soaked patch and minor updates

### Fixes

- **release**: count only shipped paths for the bump and the pending check

### Other

- **release**: infer the bump and run smoke-live and eval before releasing
- **lint**: validate the plugin when the CI file changes
- **image**: pin the CI base image by digest
- **components**: move the component pins to 4.6.0
- **skills**: route web search and the handbook to homelab-ops, add evals
- **skills**: refresh the ci-components tool list when a release changes tools

## 1.1.0 - 2026-11-19

### Features

- **hooks**: list files that still name a renamed tool at session start

### Fixes

- **docmost**: indent ordered-list content so get_page output imports back
  intact

### Other

- **ci**: test:python runs in the digest-pinned uv image, with uv's GitLab
  cache setup and a 10-minute timeout

## 1.0.0 - 2026-11-18

### Breaking changes

- **homelab**: name every homelab tool after its system
  (ten tools renamed; see `docs/SETUP.md`, Migrating to 1.0)

### Features

- **skills**: add a plugin eval suite and correct the homelab-ops description

### Other

- tag releases from CI, keep a changelog and soak every Python dependency

## 0.17.0 - 2026-11-18

### Other

- **manifests**: start the Python servers from one venv that survives updates
- run the npm MCP servers from a lockfile, drop grafana's API tool

## 0.16.0 - 2026-11-18

### Features

- give every opengist, docmost, silverbullet and searxng tool a title
- **homelab**: give every homelab tool a title and parameter descriptions
- **homelab**: report config sections and keys no server reads
- declare result types for the opengist, docmost, silverbullet and searxng tools
- **homelab**: declare result types for every homelab tool

### Fixes

- **homelab**: bound the work and requests of five tools

### Other

- bring guidance and descriptions up to date with silverbullet
- tighten the version, pin and secret-scan guards
- add pyright, a coverage floor and a dependency audit
- **homelab**: retry idempotent reads and reuse the PBS namespace walk
- **homelab**: type-check the whole server in pyright strict mode

Releases before 0.16.0 are in the git history only (`chore(release)` commits).
