"""Proxmox Backup Server (token with the Audit role). GET only."""

import time
from typing import Any

from mcp.server import MCPServer

from ..common import HttpClient, clip, pct, pick, tool
from ..config import Config
from . import pve


def client(cfg: Config) -> HttpClient:
    sec = cfg.section("pbs")
    secret = cfg.secret(sec, "token_secret", "pbs")
    # PBS separates token id and secret with ':' (PVE uses '=').
    return HttpClient(
        "pbs",
        f"{sec['url'].rstrip('/')}/api2/json",
        headers={"Authorization": f"PBSAPIToken={sec['token_id']}:{secret}"},
        verify=bool(sec.get("verify_tls", True)),
    )


async def data(cfg: Config, path: str, **params: Any) -> Any:
    return (await client(cfg).get(path, **params)).get("data")


async def datastore_names(cfg: Config) -> list[str]:
    return [d["store"] for d in await data(cfg, "/admin/datastore") or [] if "store" in d]


async def groups(cfg: Config, store: str, ns: str | None = None) -> list[dict[str, Any]]:
    out = []
    for g in await data(cfg, f"/admin/datastore/{store}/groups", ns=ns) or []:
        item = pick(
            g, "backup-type", "backup-id", "last-backup", "backup-count", "owner", "comment"
        )
        item["datastore"] = store
        out.append(item)
    return out


async def failed_tasks(cfg: Config, since: float, limit: int = 100) -> list[dict[str, Any]]:
    tasks = (
        await data(cfg, "/nodes/localhost/tasks", errors=True, since=int(since), limit=limit) or []
    )
    return [
        pick(t, "upid", "worker_type", "worker_id", "user", "starttime", "endtime", "status")
        for t in tasks
    ]


async def summary(cfg: Config) -> dict[str, Any]:
    usage = await data(cfg, "/status/datastore-usage") or []
    return {
        "datastores": {u.get("store"): pct(u.get("used"), u.get("total")) for u in usage},
        "failed_tasks_24h": len(await failed_tasks(cfg, time.time() - 86400)),
    }


def register(app: MCPServer, cfg: Config) -> None:
    @tool(app, "PBS datastores: usage, estimated full date, last garbage collection.")
    async def pbs_datastores() -> dict[str, Any]:
        out = []
        for u in await data(cfg, "/status/datastore-usage") or []:
            item = pick(u, "store", "total", "used", "avail", "estimated-full-date", "error")
            item["used_pct"] = pct(u.get("used"), u.get("total"))
            gc = u.get("gc-status") or {}
            item["gc"] = pick(gc, "upid", "removed-bytes", "pending-bytes", "disk-chunks")
            out.append(item)
        return {"datastores": out}

    @tool(
        app,
        "PBS backups. Without backup_id: one row per backup group (last backup time, count). "
        "With backup_type (vm|ct|host) and backup_id (e.g. the VMID): that group's snapshots "
        "with size and verify state.",
    )
    async def pbs_snapshots(
        datastore: str | None = None,
        backup_type: str | None = None,
        backup_id: str | None = None,
        ns: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        stores = [datastore] if datastore else await datastore_names(cfg)
        if backup_id is None:
            rows: list[dict[str, Any]] = []
            for store in stores:
                rows += [
                    g
                    for g in await groups(cfg, store, ns)
                    if backup_type is None or g.get("backup-type") == backup_type
                ]
            rows.sort(key=lambda g: g.get("last-backup") or 0)
            return clip(rows, limit)
        snaps: list[dict[str, Any]] = []
        for store in stores:
            for s in (
                await data(
                    cfg,
                    f"/admin/datastore/{store}/snapshots",
                    ns=ns,
                    **{"backup-type": backup_type, "backup-id": backup_id},
                )
                or []
            ):
                item = pick(
                    s, "backup-type", "backup-id", "backup-time", "size", "protected", "comment"
                )
                item["datastore"] = store
                item["verify"] = (s.get("verification") or {}).get("state", "none")
                snaps.append(item)
        snaps.sort(key=lambda s: s.get("backup-time") or 0, reverse=True)
        return clip(snaps, limit)

    @tool(
        app, "PBS tasks (backup, verify, prune, garbage collection, sync). errors_only by default."
    )
    async def pbs_tasks(
        errors_only: bool = True, since_hours: int = 72, limit: int = 50
    ) -> dict[str, Any]:
        since = time.time() - since_hours * 3600
        if errors_only:
            return clip(await failed_tasks(cfg, since, limit), limit)
        tasks = await data(cfg, "/nodes/localhost/tasks", since=int(since), limit=limit) or []
        rows = [
            pick(t, "upid", "worker_type", "worker_id", "user", "starttime", "endtime", "status")
            for t in tasks
        ]
        return clip(rows, limit)

    @tool(
        app,
        "Backup coverage: every PVE guest compared with PBS backup groups. Lists guests with no "
        "backup at all and guests whose newest backup is older than max_age_hours.",
    )
    async def pbs_backup_coverage(max_age_hours: int = 36) -> dict[str, Any]:
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for store in await datastore_names(cfg):
            for g in await groups(cfg, store):
                key = (g.get("backup-type", ""), str(g.get("backup-id", "")))
                if (g.get("last-backup") or 0) > (latest.get(key, {}).get("last-backup") or 0):
                    latest[key] = g
        now = time.time()
        missing, stale, ok = [], [], 0
        for guest in await pve.guests(cfg):
            kind = "ct" if guest["type"] == "lxc" else "vm"
            g = latest.get((kind, str(guest["vmid"])))
            label = {"vmid": guest["vmid"], "name": guest["name"], "type": guest["type"]}
            if g is None:
                missing.append(label)
            elif now - (g.get("last-backup") or 0) > max_age_hours * 3600:
                stale.append(
                    {
                        **label,
                        "last_backup": g.get("last-backup"),
                        "datastore": g["datastore"],
                        "age_hours": round((now - g["last-backup"]) / 3600, 1),
                    }
                )
            else:
                ok += 1
        return {"ok": ok, "missing": missing, "stale": stale, "max_age_hours": max_age_hours}
