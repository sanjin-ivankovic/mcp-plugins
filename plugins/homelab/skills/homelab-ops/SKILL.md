---
name: homelab-ops
description: Use when the user asks about the live state of the homelab — is something down, out of sync, drifted, backed up, blocked by DNS filtering, failing to deploy, or what a pipeline did — or before changing infrastructure, to look first. Covers which read-only MCP tool answers which question (homelab, argocd, kubernetes, longhorn, gitlab, unifi servers), what still needs Bash, and which repo skill owns the fix.
---

# Homelab operations: look with MCP, change with the repos

All homelab MCP servers in this plugin are read-only, enforced by the
credential each one carries (PVEAuditor, PBS Audit, Unraid VIEWER, ArgoCD
`role:readonly`, the `mcp-reader` ServiceAccount, Komodo Read user, Semaphore
Guest). A write attempt fails at the target system. Do not look for a way
around that: changes go through Git and the repo skills.

## Pick the tool

| Question | Tool |
|---|---|
| "Is anything broken?" | `homelab_overview`, then `drift_report` |
| ArgoCD app state, events, resources, pod logs | `argocd` server: `list_applications`, `get_application`, `get_application_events`, `get_application_resource_tree`, `get_application_workload_logs` |
| Pods, events, any CRD, `top` | `kubernetes` server: `pods_list`, `pods_log`, `events_list`, `resources_list`, `resources_get`, `pods_top`, `nodes_top` |
| Volume degraded, replica rebuild, backups | `longhorn` server: `volume_list`, `volume_get`, `node_list`, `backup_list`, `longhorn_events` |
| PVE nodes, storage, LXC/VM status and config | `pve_nodes`, `pve_storage`, `pve_guests`, `pve_guest` |
| Failed vzdump, scheduled backups | `pve_tasks`, `pve_backup_jobs` |
| "Is X backed up?", stale backups, datastores | `pbs_backup_coverage`, `pbs_snapshots`, `pbs_tasks`, `pbs_datastores` |
| Unraid array, disks, containers, VMs, shares | `unraid_system`, `unraid_array`, `unraid_docker`, `unraid_vms`, `unraid_shares`, `unraid_notifications` |
| Authoritative records, DHCP, ns1/ns2 agreement | `dns_zones`, `dns_records`, `dns_dhcp_scopes`, `dns_dhcp_leases` |
| DNS traffic and failures | `dns_stats`, `dns_query_log` |
| "Why is this domain blocked?" | `pihole_search`, then `pihole_query_log` |
| Pi-hole state, lists, settings, warnings | `pihole_status`, `pihole_stats`, `pihole_lists`, `pihole_config`, `pihole_config_diff`, `pihole_messages`, `pihole_logs`, `pihole_history` |
| Compose stack down, what Komodo deployed | `komodo_stacks`, `komodo_stack`, `komodo_servers` |
| Failed deploys, pending resource sync | `komodo_updates`, `komodo_sync_status` |
| Patch or playbook run failed | `semaphore_tasks` (`failed_only`), `semaphore_task_output` |
| Chart renders differently each time | `drift_render_check` |
| Pipeline, job log, MR, release on source.example.com | `gitlab` server: `list_pipelines`, `get_pipeline`, `get_job`, `list_merge_requests`, `get_merge_request`, `list_releases` |
| Firewall, clients, Wi-Fi, IPS | `unifi-network` plugin |

## What still needs Bash or SSH

- Kubernetes Secrets: `mcp-reader` cannot read them by design.
- Container logs and `docker exec` on Komodo hosts: proxmox-infra
  `host-ssh-access`.
- Public DNS at Cloudflare (ACME challenges): `dig`.
- `kubeseal --fetch-cert`, render and dry-run commands.
- Every write: sync, apply, seal, deploy, push.

## Drift workflow

1. `drift_report` (default scopes). Add `include_terraform: true` only when
   the user asks about Terraform; it takes minutes.
2. Read `overall` and each scope's status:
   - `drift`: real difference. Explain it, then hand off (step 3).
   - `error`: the check ran and failed. Report it; never call it clean.
   - `skipped`: a tool, credential or repo path is missing. Say which.
   - `homelab_overview` adds `config_error` when the config file itself
     failed to load; every system then shows "not configured".
3. Hand off to the skill that owns the fix:
   - argocd / sealedsecrets → argo-apps `cluster-troubleshooting`. User-tier
     apps auto-sync; infra, data and services need a manual sync.
   - kryptos → argo-apps `kryptos-sealed-secrets`.
   - dns (technitium, pihole) → proxmox-infra `network-as-code`;
     `make -C ansible technitium-bootstrap ARGS=--dry-run` previews a
     Technitium fix.
   - semaphore doctor → proxmox-infra `fleet-patching`.
   - terraform lxc / talos → proxmox-infra `terraform-state` or
     `talos-cluster`; terraform unifi → `network-as-code`.
   - komodo → proxmox-infra `komodo-service-stack`.

## Facts that prevent wrong conclusions

- Komodo `head_differs` is normal. Komodo compares the deployed commit with
  repo HEAD, and deploys only stacks whose own files changed.
- Technitium records commented `Via '...'` (DHCP) or `Cluster managed
  record` belong to Technitium; never propose deleting them.
- An empty Technitium zone list for a zone that exists means the MCP reader
  lacks View on that zone, on that node.
- Pi-hole settings are `FTLCONF_*` in proxmox-infra
  `services/pihole/docker-compose.yml` and its lists come from
  `services/pihole/`; UI changes are overwritten. `pihole_config` shows
  what differs from the defaults and whether an env var pins it.
- An ArgoCD app showing OutOfSync is not a sync loop; check
  `operationState` before calling it one.
- Talos workers with `on_boot=false` in PVE are intentional.
- An app at 0 replicas in argo-apps is parked on purpose (`replicaCount: 0`).
