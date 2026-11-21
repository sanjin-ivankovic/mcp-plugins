---
name: homelab-ops
description: Use when the user asks about the live state of the homelab — is something down, out of sync, drifted, backed up, alerting, blocked by DNS filtering, failing to deploy, or what a pipeline or a notification said — or before changing infrastructure, to look first; when searching the web for an exact error message, GitHub issue, changelog or release notes; when looking something up in, or updating, the homelab handbook in Docmost; or when getting a script, commands or PR text onto the work machine (OpenGist gist or SilverBullet scratch page). Maps each question to its MCP tool, says what still needs Bash, and hands the fix to the repo skill that owns it.
---

# Homelab operations: look with MCP, change with the repos

The servers that show live state are read-only, each by its own guard: the
`homelab` server by credential and code (PVEAuditor, PBS Audit, Unraid
VIEWER, Komodo Read user, Semaphore Guest, ntfy read on four topics;
Technitium through an allowlist of read endpoints, Pi-hole by GET plus its
login; Traefik and Alertmanager by GET only, with no credential on the
LAN), `argocd` by
`MCP_READ_ONLY` and the `mcp` account, `kubernetes` by the `mcp-reader`
ServiceAccount, `longhorn` by its `--read-only` flag (no credential),
`grafana` by a Viewer service account, `--disable-write` and
`--disable-api`. Four servers write. `gitlab` is GitLab's own MCP
endpoint, acts as you, and has write tools (`accept_merge_request`,
`add_commit`, `add_branch`, `fork_repository`, `save_*`,
`link_work_items`). Keep those on "ask" and use them only when the user
asks for that exact action.
The `opengist` server also acts as you: `opengist_create_gist` writes a
gist on the homelab OpenGist, the way to get a script onto the work machine.
The `docmost` server acts as the Docmost member `claude`:
`docmost_create_page`, `docmost_update_page` and `docmost_set_labels`
change the handbook; keep them on "ask".
The `silverbullet` server writes pages to the SilverBullet space the user
keeps open on the work machine: `silverbullet_write_page` and
`silverbullet_append_page`, kept on "ask".
Everything else changes through Git and the repo skills.

## Pick the tool

| Question | Tool |
|---|---|
| "Is anything broken?" | `homelab_overview`, then `homelab_drift_report` |
| Known trap or expected noise? | `docmost` server: `docmost_search_pages` for the symptom, then `docmost_get_page` on hits under *Known issues and troubleshooting* (label `troubleshooting`) |
| Where a handbook page belongs, what links to it | `docmost_list_spaces`, `docmost_browse_tree`, `docmost_get_backlinks` |
| What changed in the handbook, an older page body | `docmost_recent_changes`, `docmost_list_versions`, `docmost_get_version` |
| ArgoCD app state, events, diff, pod logs | `argocd` server: `list_applications`, `get_application`, `get_application_events`, `get_application_resource_tree`, `get_application_managed_resources`, `get_resource_events`, `get_application_workload_logs` |
| Pods, events, any CRD, `top` | `kubernetes` server: `pods_list`, `pods_list_in_namespace`, `pods_get`, `pods_log`, `events_list`, `resources_list`, `resources_get`, `pods_top`, `nodes_top` |
| Volume degraded, replica rebuild, backups | `longhorn` server: `volume_list`, `volume_get`, `volumeattachment_list`, `node_list`, `backupvolume_list`, `longhorn_events` |
| PVE nodes, storage, LXC/VM status and config | `pve_nodes`, `pve_storage`, `pve_guests`, `pve_guest` |
| Failed vzdump, scheduled backups | `pve_tasks`, `pve_backup_jobs` |
| "Is X backed up?", stale backups, datastores | `pbs_backup_coverage`, `pbs_snapshots`, `pbs_tasks`, `pbs_datastores` |
| Unraid array, disks, containers, VMs, shares | `unraid_system`, `unraid_array`, `unraid_docker`, `unraid_vms`, `unraid_shares`, `unraid_notifications` |
| Authoritative records, DHCP, ns1/ns2 agreement | `technitium_zones`, `technitium_records`, `technitium_dhcp_scopes`, `technitium_dhcp_leases` |
| DNS traffic and failures | `technitium_stats`, `technitium_query_log` |
| "Why is this domain blocked?" | `pihole_search`, then `pihole_query_log` |
| Pi-hole state, lists, settings, warnings | `pihole_status`, `pihole_stats`, `pihole_lists`, `pihole_config`, `pihole_config_diff`, `pihole_messages`, `pihole_logs`, `pihole_history` |
| Compose stack down, what Komodo deployed | `komodo_stacks`, `komodo_stack`, `komodo_servers` |
| Failed deploys, pending resource sync | `komodo_updates`, `komodo_sync_status` |
| Patch or playbook run failed | `semaphore_tasks` (`failed_only`), `semaphore_task_output` |
| Route 404/502, which service and middlewares a hostname gets | `traefik_routers` (`search` a hostname), `traefik_services` (`down_only`) |
| What is alerting now, is it silenced | `alertmanager_alerts`, `alertmanager_silences` |
| What did ntfy send, the last notification and why | `ntfy_messages` (`topic`, `min_priority`; kept 12 h) |
| Does a Vault path exist, when did it change | `vault_kv_paths` (`prefix`, default `k8s/`); never values. Sealed or not: `homelab_overview` |
| Chart renders differently each time | `homelab_drift_render_check` |
| A pipeline's jobs, a job's log (grep, tail) | `gitlab_pipeline_jobs`, `gitlab_job_log` (homelab server) |
| Pipeline, MR, release, file at a ref | `gitlab` server reads: `list_pipelines`, `get_pipeline`, `get_job`, `list_merge_requests`, `get_merge_request`, `list_releases`, `list_commits`, `get_commit`, `get_repository_file`, `list_repository_tree` |
| Metrics, PromQL, dashboards | `grafana` server: `query_prometheus`, `list_prometheus_metric_names`, `search_dashboards`, `get_dashboard_summary`, `get_dashboard_panel_queries` |
| Logs (Loki) | `grafana` server: `list_loki_label_values`, `query_loki_logs`, `query_loki_patterns` |
| Alert rules and their state | `grafana` server: `alerting_rules_read`; firing alerts: `alertmanager_alerts` |
| Firewall, clients, Wi-Fi, IPS | `unifi-network` plugin |
| Get a script to the work machine | `opengist` server: `opengist_create_gist` (unlisted, 7-day expiry by default; never a secret), then give the user the page or raw URL |
| Find or reread a gist | `opengist_list_gists`, `opengist_get_gist` |
| Commands or PR text for the user to copy at work now | `silverbullet` server: `silverbullet_write_page` (new page) or `silverbullet_append_page`; commands in fenced code blocks; never a secret; give the user the page URL |
| Reread a scratch page | `silverbullet_list_pages`, `silverbullet_read_page` |

## The web: errors, issues, changelogs

The `searxng` server searches the web through the homelab SearXNG.
`searxng_search` finds exact error messages, GitHub issues, changelogs and
release notes; open the best hits with `searxng_fetch` (Markdown in chunks,
follow `next_index`) instead of answering from snippets.
`searxng_instance` lists the enabled engines and categories, to aim a
search at one. Library and API documentation: the `Ref` server when it is
configured. Never put internal
hostnames, IPs, domains or employer codes in a query: it leaves the LAN.
`searxng_fetch` also reads LAN pages, which WebFetch refuses.

## What still needs Bash or SSH

- Kubernetes Secrets: `mcp-reader` cannot read them by design.
- Kubelet logs and node stats: `mcp-reader` has no `nodes/proxy` (that verb
  reaches the kubelet exec endpoint). proxmox-infra `talos-cluster`,
  `talosctl logs kubelet`.
- Container logs and `docker exec` on Komodo hosts: proxmox-infra
  `host-ssh-access`.
- Public DNS at Cloudflare (ACME challenges): `dig`.
- Render and dry-run commands.
- Vault: secret values, `vault kv` reads and writes (the user's login).
- Every write: sync, apply, deploy, push.

## Drift workflow

1. `homelab_drift_report` (default scopes: argocd, externalsecrets, vault,
   komodo, dns, semaphore, pbs). `vault` compares the Vault paths live
   ExternalSecrets read with what exists under `kv/k8s/`: missing paths, deleted
   current versions, and paths nothing reads. The pihole, semaphore and pbs
   doctors run with the plugin's read-only credentials; technitium-doctor reads
   server settings the MCP token cannot, so it still needs a valid `vault login`
   on the laptop. Add `include_terraform: true` (or the `terraform` scope) only
   when the user asks about Terraform: it takes minutes and needs the same
   login. `inventory` (inventory-doctor) is opt-in too. An expired login shows
   as `error`, not drift. The `externalsecrets` scope lists every ExternalSecret
   and ClusterSecretStore that is not Ready; a sealed Vault shows there as the
   `vault` store not Ready.
2. Read `overall` and each scope's status:
   - `drift`: real difference. Explain it, then hand off (step 3).
   - `error`: the check ran and failed. Report it; never call it clean.
   - `skipped`: a tool, credential or repo path is missing. Say which.
     `overall` is `skipped` when nothing ran; never report that as clean.
   - `homelab_overview` adds `config_error` when the config file itself
     failed to load; every system then shows "not configured".
3. Hand off to the skill that owns the fix:
   - argocd → argo-apps `cluster-troubleshooting`; its rules file says
     which apps auto-sync and which are parked on purpose.
   - externalsecrets → argo-apps `external-secrets`; a sealed Vault →
     proxmox-infra `docs/VAULT.md` (unseal).
   - vault → a missing path or deleted version: the user writes the value
     (`vault kv put`); an unreferenced path: ask before anyone deletes it.
     Manifests: argo-apps `external-secrets`.
   - dns (technitium, pihole) → proxmox-infra `network-as-code` (it has the
     preview and doctor commands).
   - semaphore → proxmox-infra `fleet-patching`.
   - pbs → proxmox-infra `ansible/configs/pbs.yml`; preview the fix with
     `make -C ansible pbs-bootstrap ARGS=--dry-run` (`repo-quality-gates`).
   - inventory → proxmox-infra `host-lifecycle`.
   - terraform lxc / talos / backup / vault → proxmox-infra
     `terraform-state` or `talos-cluster`; terraform unifi →
     `network-as-code`.
   - komodo → proxmox-infra `komodo-service-stack`.

## Facts that prevent wrong conclusions

- Komodo `head_differs` is normal. Komodo compares the deployed commit with
  repo HEAD, and deploys only stacks whose own files changed.
- Technitium records commented `Via '...'` (DHCP) or `Cluster managed
  record` belong to Technitium; never propose deleting them.
- An empty Technitium zone list for a zone that exists means the MCP reader
  lacks View on that zone, on that node.
- Pi-hole settings and lists are code in proxmox-infra (`network-as-code`);
  UI changes are overwritten. `pihole_config` shows what differs from the
  defaults and whether an env var pins it.
- An ArgoCD app showing OutOfSync is not a sync loop; check
  `operationState` before calling it one.
- Talos workers with `on_boot=false` in PVE are intentional.
- Tool arguments that trip first calls: `technitium_records` needs a `zone` and
  reads ns1 unless `node` is given; `pbs_snapshots` lists backup groups
  until `backup_type` and `backup_id` (the VMID) name one, across every
  namespace unless `ns` names one (Talos VMs back up into `talos`);
  Pi-hole tools may answer for one node, so check both (`pihole-1`,
  `pihole-2`). GitLab project paths are `homelab/<repo>`.
- After a host IP move, look for the stale PTR of the old address and the
  old backup group in PBS; neither is cleaned up automatically.
