"""Proxmox VE (token with PVEAuditor). GET only."""

import time
from typing import Annotated, Any, Literal, TypedDict

from mcp.server import MCPServer
from pydantic import Field

from ..common import InputError, Listing, arr, clip, gather_map, obj, pct, pick, tool
from ..config import Config
from . import _proxmox


async def data(cfg: Config, path: str, **params: Any) -> Any:
    return await _proxmox.data(cfg, "pve", path, **params)


async def guests(cfg: Config, include_templates: bool = False) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in arr(await data(cfg, "/cluster/resources", type="vm")):
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
    nodes = arr(await data(cfg, "/nodes"))
    gs = await guests(cfg)
    since = time.time() - 86400
    tasks = arr(await data(cfg, "/cluster/tasks"))
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


class Nodes(TypedDict):
    nodes: list[dict[str, Any]]


class Guest(TypedDict):
    guest: dict[str, Any]
    config: dict[str, Any]
    status: dict[str, Any]
    snapshots: list[dict[str, Any]]


class Storage(TypedDict):
    storage: list[dict[str, Any]]


class BackupJobs(TypedDict):
    jobs: list[dict[str, Any]]
    not_backed_up: list[dict[str, Any]]


def register(app: MCPServer, cfg: Config) -> None:
    @tool(
        app, "Proxmox VE nodes: status, CPU/memory use, uptime, PVE version.", title="Proxmox nodes"
    )
    async def pve_nodes() -> Nodes:
        async def one(n: dict[str, Any]) -> dict[str, Any]:
            item = pick(n, "node", "status", "uptime")
            item["cpu_pct"] = round(100 * n["cpu"], 1) if n.get("cpu") is not None else None
            item["mem_pct"] = pct(n.get("mem"), n.get("maxmem"))
            if n.get("status") == "online":
                ver = await data(cfg, f"/nodes/{n['node']}/version")
                item["version"] = (obj(ver)).get("version")
            return item

        return {"nodes": await gather_map(arr(await data(cfg, "/nodes")), one)}

    @tool(
        app,
        "List LXC containers and VMs with status, tags and resource use. "
        "Filter by type (lxc|qemu), status (running|stopped) or tag.",
        title="Proxmox guests",
    )
    async def pve_guests(
        type: Annotated[
            Literal["lxc", "qemu"] | None, Field(description="lxc or qemu; both when absent")
        ] = None,
        status: Annotated[
            str | None, Field(description="Guest status, e.g. running or stopped")
        ] = None,
        tag: Annotated[
            str | None, Field(description="Only guests carrying this Proxmox tag")
        ] = None,
        include_templates: Annotated[bool, Field(description="Also list templates")] = False,
        limit: Annotated[int, Field(description="Rows to return, 1-1000")] = 200,
    ) -> Listing:
        gs = await guests(cfg, include_templates)
        gs = [
            g
            for g in gs
            if (type is None or g["type"] == type)
            and (status is None or g["status"] == status)
            and (tag is None or tag in g["tags"])
        ]
        return clip(gs, limit)

    @tool(app, "One guest by VMID: config, live status and snapshots.", title="Proxmox guest")
    async def pve_guest(vmid: Annotated[int, Field(description="Guest VMID")]) -> Guest:
        match = next((g for g in await guests(cfg, True) if g["vmid"] == vmid), None)
        if match is None:
            raise InputError(f"no guest with vmid {vmid}")
        base = f"/nodes/{match['node']}/{match['type']}/{vmid}"
        conf, status, snaps = await gather_map(
            ("config", "status/current", "snapshot"), lambda p: data(cfg, f"{base}/{p}")
        )
        conf = {k: v for k, v in (obj(conf)).items() if k != "digest"}
        status, snaps = obj(status), arr(snaps)
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

    @tool(
        app,
        "Storage pools on every node with usage percentage and content types.",
        title="Proxmox storage",
    )
    async def pve_storage() -> Storage:
        out: list[dict[str, Any]] = []
        for s in arr(await data(cfg, "/cluster/resources", type="storage")):
            item = pick(s, "storage", "node", "status", "plugintype", "content", "shared")
            item["used_pct"] = pct(s.get("disk"), s.get("maxdisk"))
            item["total_gib"] = round(s["maxdisk"] / 2**30, 1) if s.get("maxdisk") else None
            out.append(item)
        return {"storage": out}

    @tool(
        app,
        "Recent cluster tasks (vzdump, start/stop, snapshots). errors_only keeps non-OK ones.",
        title="Proxmox tasks",
    )
    async def pve_tasks(
        errors_only: Annotated[bool, Field(description="Only tasks that did not end OK")] = False,
        since_hours: Annotated[int, Field(description="How far back to look, in hours")] = 48,
        limit: Annotated[int, Field(description="Rows to return, 1-1000")] = 50,
    ) -> Listing:
        since = time.time() - since_hours * 3600
        tasks = [
            pick(t, "upid", "node", "type", "id", "user", "starttime", "endtime", "status")
            for t in arr(await data(cfg, "/cluster/tasks"))
            if (t.get("starttime") or 0) >= since
        ]
        if errors_only:
            tasks = [t for t in tasks if t.get("status") not in (None, "OK")]
        tasks.sort(key=lambda t: t.get("starttime") or 0, reverse=True)
        return clip(tasks, limit)

    @tool(
        app,
        "Scheduled vzdump backup jobs and the guests that no backup job covers.",
        title="Proxmox backup jobs",
    )
    async def pve_backup_jobs() -> BackupJobs:
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
            for j in arr(await data(cfg, "/cluster/backup"))
        ]
        uncovered = arr(await data(cfg, "/cluster/backup-info/not-backed-up"))
        return {"jobs": jobs, "not_backed_up": [pick(g, "vmid", "name", "type") for g in uncovered]}
