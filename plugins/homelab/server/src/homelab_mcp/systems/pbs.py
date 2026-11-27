"""Proxmox Backup Server (token with the Audit role). GET only."""

import time
from typing import Annotated, Any, TypedDict

from mcp.server import MCPServer
from pydantic import Field

from ..common import Listing, arr, clamp, clip, gather_map, obj, pct, pick, tool
from ..config import Config
from . import _proxmox, pve


async def data(cfg: Config, path: str, **params: Any) -> Any:
    return await _proxmox.data(cfg, "pbs", path, **params)


async def datastore_names(cfg: Config) -> list[str]:
    return [d["store"] for d in arr(await data(cfg, "/admin/datastore")) if "store" in d]


async def groups(cfg: Config, store: str, ns: str | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for g in arr(await data(cfg, f"/admin/datastore/{store}/groups", ns=ns or None)):
        item = pick(
            g, "backup-type", "backup-id", "last-backup", "backup-count", "owner", "comment"
        )
        item["datastore"] = store
        if ns:
            item["ns"] = ns
        out.append(item)
    return out


async def namespaces(cfg: Config, store: str) -> list[str]:
    """Every namespace of a datastore, the root one as ""."""
    listed = arr(await data(cfg, f"/admin/datastore/{store}/namespace"))
    return [n.get("ns", "") for n in listed] or [""]


async def spots(cfg: Config, stores: list[str], ns: str | None = None) -> list[tuple[str, str]]:
    """(datastore, namespace) pairs: every namespace of each store, or just ``ns``."""
    if ns is not None:
        return [(store, ns) for store in stores]
    return [
        (store, found_ns)
        for store, found in zip(
            stores, await gather_map(stores, lambda st: namespaces(cfg, st)), strict=True
        )
        for found_ns in found
    ]


async def all_groups(cfg: Config) -> list[dict[str, Any]]:
    """Backup groups of every namespace on every datastore."""
    places = await spots(cfg, await datastore_names(cfg))
    return [g for found in await gather_map(places, lambda s: groups(cfg, *s)) for g in found]


async def failed_tasks(cfg: Config, since: float, limit: int = 100) -> list[dict[str, Any]]:
    tasks = arr(
        await data(cfg, "/nodes/localhost/tasks", errors=True, since=int(since), limit=limit)
    )
    return [
        pick(t, "upid", "worker_type", "worker_id", "user", "starttime", "endtime", "status")
        for t in tasks
    ]


async def summary(cfg: Config) -> dict[str, Any]:
    usage = arr(await data(cfg, "/status/datastore-usage"))
    return {
        "datastores": {u.get("store"): pct(u.get("used"), u.get("total")) for u in usage},
        "failed_tasks_24h": len(await failed_tasks(cfg, time.time() - 86400)),
    }


class Datastores(TypedDict):
    datastores: list[dict[str, Any]]


class Coverage(TypedDict):
    ok: int
    missing: list[dict[str, Any]]
    stale: list[dict[str, Any]]
    max_age_hours: int


def register(app: MCPServer, cfg: Config) -> None:
    @tool(
        app,
        "PBS datastores: usage, estimated full date, last garbage collection.",
        title="PBS datastores",
    )
    async def pbs_datastores() -> Datastores:
        out: list[dict[str, Any]] = []
        for u in arr(await data(cfg, "/status/datastore-usage")):
            item = pick(u, "store", "total", "used", "avail", "estimated-full-date", "error")
            item["used_pct"] = pct(u.get("used"), u.get("total"))
            gc = obj(u.get("gc-status"))
            item["gc"] = pick(gc, "upid", "removed-bytes", "pending-bytes", "disk-chunks")
            out.append(item)
        return {"datastores": out}

    @tool(
        app,
        'PBS backups in every namespace unless ns names one ("" is the root). Without '
        "backup_id: one row per backup group (last backup time, count, namespace). With "
        "backup_type (vm|ct|host) and backup_id (e.g. the VMID): that group's snapshots with "
        "size and verify state.",
        title="PBS backups",
    )
    async def pbs_snapshots(
        datastore: Annotated[
            str | None, Field(description="Datastore name; every datastore when absent")
        ] = None,
        backup_type: Annotated[str | None, Field(description="vm, ct or host")] = None,
        backup_id: Annotated[
            str | None, Field(description="Backup id, e.g. the VMID; lists that group's snapshots")
        ] = None,
        ns: Annotated[
            str | None, Field(description='Namespace; every namespace when absent, "" for the root')
        ] = None,
        limit: Annotated[int, Field(description="Rows to return, 1-1000")] = 100,
    ) -> Listing:
        stores = [datastore] if datastore else await datastore_names(cfg)
        places = await spots(cfg, stores, ns)
        if backup_id is None:
            per_place = await gather_map(places, lambda s: groups(cfg, *s))
            rows = [
                g
                for found in per_place
                for g in found
                if backup_type is None or g.get("backup-type") == backup_type
            ]
            rows.sort(key=lambda g: g.get("last-backup") or 0)
            return clip(rows, limit)

        async def place_snaps(place: tuple[str, str]) -> list[dict[str, Any]]:
            store, where = place
            out: list[dict[str, Any]] = []
            params = {"backup-type": backup_type, "backup-id": backup_id}
            path = f"/admin/datastore/{store}/snapshots"
            for s in arr(await data(cfg, path, ns=where or None, **params)):
                item = pick(
                    s, "backup-type", "backup-id", "backup-time", "size", "protected", "comment"
                )
                item["datastore"] = store
                if where:
                    item["ns"] = where
                item["verify"] = (obj(s.get("verification"))).get("state", "none")
                out.append(item)
            return out

        snaps = [s for found in await gather_map(places, place_snaps) for s in found]
        snaps.sort(key=lambda s: s.get("backup-time") or 0, reverse=True)
        return clip(snaps, limit)

    @tool(
        app,
        "PBS tasks (backup, verify, prune, garbage collection, sync). errors_only by default.",
        title="PBS tasks",
    )
    async def pbs_tasks(
        errors_only: Annotated[bool, Field(description="Only tasks that did not end OK")] = True,
        since_hours: Annotated[int, Field(description="How far back to look, in hours")] = 72,
        limit: Annotated[int, Field(description="Rows to return, 1-1000")] = 50,
    ) -> Listing:
        since = time.time() - since_hours * 3600
        # One row more than asked for, so clip() can tell whether more exist.
        ask = clamp(limit, 1, 1000) + 1
        if errors_only:
            return clip(await failed_tasks(cfg, since, ask), limit)
        tasks = arr(await data(cfg, "/nodes/localhost/tasks", since=int(since), limit=ask))
        rows = [
            pick(t, "upid", "worker_type", "worker_id", "user", "starttime", "endtime", "status")
            for t in tasks
        ]
        return clip(rows, limit)

    @tool(
        app,
        "Backup coverage: every PVE guest compared with the PBS backup groups of every "
        "datastore and namespace. Lists guests with no backup at all and guests whose newest "
        "backup is older than max_age_hours.",
        title="Backup coverage",
    )
    async def pbs_backup_coverage(
        max_age_hours: Annotated[
            int, Field(description="Hours after which a guest's newest backup counts as too old")
        ] = 36,
    ) -> Coverage:
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for g in await all_groups(cfg):
            key = (g.get("backup-type", ""), str(g.get("backup-id", "")))
            if (g.get("last-backup") or 0) > (latest.get(key, {}).get("last-backup") or 0):
                latest[key] = g
        now = time.time()
        missing: list[dict[str, Any]] = []
        stale: list[dict[str, Any]] = []
        ok = 0
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
                        **({"ns": g["ns"]} if g.get("ns") else {}),
                        "age_hours": round((now - g["last-backup"]) / 3600, 1),
                    }
                )
            else:
                ok += 1
        return {"ok": ok, "missing": missing, "stale": stale, "max_age_hours": max_age_hours}
