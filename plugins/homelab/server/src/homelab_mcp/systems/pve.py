"""Proxmox VE (token with PVEAuditor). GET only."""

import time
from typing import Any, Literal

from mcp.server import MCPServer

from ..common import HttpClient, clip, pct, pick, tool
from ..config import Config


def client(cfg: Config) -> HttpClient:
    sec = cfg.section("pve")
    secret = cfg.secret(sec, "token_secret", "pve")
    return HttpClient(
        "pve",
        f"{sec['url'].rstrip('/')}/api2/json",
        headers={"Authorization": f"PVEAPIToken={sec['token_id']}={secret}"},
        verify=bool(sec.get("verify_tls", True)),
    )


async def data(cfg: Config, path: str, **params: Any) -> Any:
    return (await client(cfg).get(path, **params)).get("data")


async def guests(cfg: Config, include_templates: bool = False) -> list[dict[str, Any]]:
    out = []
    for r in await data(cfg, "/cluster/resources", type="vm") or []:
        if r.get("template") and not include_templates:
            continue
        out.append(
            {
                "vmid": r.get("vmid"),
                "name": r.get("name"),
                "type": r.get("type"),
                "node": r.get("node"),
                "status": r.get("status"),
                "tags": [t for t in (r.get("tags") or "").split(";") if t],
                "cpu_pct": round(100 * r["cpu"], 1) if r.get("cpu") is not None else None,
                "mem_pct": pct(r.get("mem"), r.get("maxmem")),
                "disk_pct": pct(r.get("disk"), r.get("maxdisk")),
                "uptime_s": r.get("uptime"),
                "template": bool(r.get("template")),
            }
        )
    return sorted(out, key=lambda g: g["vmid"] or 0)


async def summary(cfg: Config) -> dict[str, Any]:
    nodes = await data(cfg, "/nodes") or []
    gs = await guests(cfg)
    since = time.time() - 86400
    tasks = await data(cfg, "/cluster/tasks") or []
    failed = [
        t
        for t in tasks
        if (t.get("starttime") or 0) >= since and t.get("status") not in (None, "OK")
    ]
    return {
        "nodes_online": sum(1 for n in nodes if n.get("status") == "online"),
        "nodes_total": len(nodes),
        "guests_running": sum(1 for g in gs if g["status"] == "running"),
        "guests_stopped": [f"{g['vmid']} {g['name']}" for g in gs if g["status"] != "running"],
        "failed_tasks_24h": len(failed),
    }


def register(app: MCPServer, cfg: Config) -> None:
    @tool(app, "Proxmox VE nodes: status, CPU/memory use, uptime, PVE version.")
    async def pve_nodes() -> dict[str, Any]:
        out = []
        for n in await data(cfg, "/nodes") or []:
            item = pick(n, "node", "status", "uptime")
            item["cpu_pct"] = round(100 * n["cpu"], 1) if n.get("cpu") is not None else None
            item["mem_pct"] = pct(n.get("mem"), n.get("maxmem"))
            if n.get("status") == "online":
                ver = await data(cfg, f"/nodes/{n['node']}/version")
                item["version"] = (ver or {}).get("version")
            out.append(item)
        return {"nodes": out}

    @tool(
        app,
        "List LXC containers and VMs with status, tags and resource use. "
        "Filter by type (lxc|qemu), status (running|stopped) or tag.",
    )
    async def pve_guests(
        type: Literal["lxc", "qemu"] | None = None,
        status: str | None = None,
        tag: str | None = None,
        include_templates: bool = False,
        limit: int = 200,
    ) -> dict[str, Any]:
        gs = await guests(cfg, include_templates)
        gs = [
            g
            for g in gs
            if (type is None or g["type"] == type)
            and (status is None or g["status"] == status)
            and (tag is None or tag in g["tags"])
        ]
        return clip(gs, limit)

    @tool(app, "One guest by VMID: config, live status and snapshots.")
    async def pve_guest(vmid: int) -> dict[str, Any]:
        match = next((g for g in await guests(cfg, True) if g["vmid"] == vmid), None)
        if match is None:
            return {"error": f"no guest with vmid {vmid}"}
        base = f"/nodes/{match['node']}/{match['type']}/{vmid}"
        conf = await data(cfg, f"{base}/config") or {}
        conf = {k: v for k, v in conf.items() if k not in ("digest",)}
        status = await data(cfg, f"{base}/status/current") or {}
        snaps = await data(cfg, f"{base}/snapshot") or []
        return {
            "guest": match,
            "config": conf,
            "status": pick(status, "status", "uptime", "pid", "ha", "lock", "qmpstatus"),
            "snapshots": [
                pick(s, "name", "description", "snaptime", "parent")
                for s in snaps
                if s.get("name") != "current"
            ],
        }

    @tool(app, "Storage pools on every node with usage percentage and content types.")
    async def pve_storage() -> dict[str, Any]:
        out = []
        for s in await data(cfg, "/cluster/resources", type="storage") or []:
            item = pick(s, "storage", "node", "status", "plugintype", "content", "shared")
            item["used_pct"] = pct(s.get("disk"), s.get("maxdisk"))
            item["total_gib"] = round(s["maxdisk"] / 2**30, 1) if s.get("maxdisk") else None
            out.append(item)
        return {"storage": out}

    @tool(
        app, "Recent cluster tasks (vzdump, start/stop, snapshots). errors_only keeps non-OK ones."
    )
    async def pve_tasks(
        errors_only: bool = False, since_hours: int = 48, limit: int = 50
    ) -> dict[str, Any]:
        since = time.time() - since_hours * 3600
        tasks = [
            pick(t, "upid", "node", "type", "id", "user", "starttime", "endtime", "status")
            for t in await data(cfg, "/cluster/tasks") or []
            if (t.get("starttime") or 0) >= since
        ]
        if errors_only:
            tasks = [t for t in tasks if t.get("status") not in (None, "OK")]
        tasks.sort(key=lambda t: t.get("starttime") or 0, reverse=True)
        return clip(tasks, limit)

    @tool(app, "Scheduled vzdump backup jobs and the guests that no backup job covers.")
    async def pve_backup_jobs() -> dict[str, Any]:
        jobs = [
            pick(
                j,
                "id",
                "enabled",
                "schedule",
                "storage",
                "mode",
                "vmid",
                "all",
                "exclude",
                "next-run",
                "notes-template",
                "prune-backups",
                "comment",
            )
            for j in await data(cfg, "/cluster/backup") or []
        ]
        uncovered = await data(cfg, "/cluster/backup-info/not-backed-up") or []
        return {"jobs": jobs, "not_backed_up": [pick(g, "vmid", "name", "type") for g in uncovered]}
