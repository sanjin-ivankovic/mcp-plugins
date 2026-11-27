"""Komodo Core (API key of a user with base permission Read).

Only ``POST /read`` is reachable, and only with request types in READ_TYPES.
"""

import asyncio
from typing import Annotated, Any, TypedDict

from mcp.server import MCPServer
from pydantic import Field

from ..common import ApiError, Listing, arr, client_from, clip, gather_map, obj, pick, tool
from ..config import Config

READ_TYPES = frozenset(
    {
        "GetVersion",
        "ListServers",
        "ListStacks",
        "GetStack",
        "ListStackServices",
        "ListUpdates",
        "ListResourceSyncs",
        "GetResourceSync",
    }
)


async def read(cfg: Config, type_: str, params: dict[str, Any] | None = None) -> Any:
    if type_ not in READ_TYPES:
        raise ApiError(f"komodo: {type_} is not an allowlisted read request")
    sec = cfg.section("komodo")
    headers = {
        "X-Api-Key": cfg.secret(sec, "key", "komodo"),
        "X-Api-Secret": cfg.secret(sec, "secret", "komodo"),
    }
    http = client_from(cfg, "komodo", headers=headers, read_posts={"/read"})
    return await http.post_read("/read", json={"type": type_, "params": obj(params)})


async def server_names(cfg: Config) -> dict[str, str]:
    return {s.get("id"): s.get("name") for s in arr(await read(cfg, "ListServers"))}


async def stacks(cfg: Config) -> list[dict[str, Any]]:
    servers, listed = await asyncio.gather(server_names(cfg), read(cfg, "ListStacks"))
    out: list[dict[str, Any]] = []
    for s in arr(listed):
        info: dict[str, Any] = obj(s.get("info"))
        deployed, latest = info.get("deployed_hash"), info.get("latest_hash")
        sid = info.get("server_id")
        out.append(
            {
                "name": s.get("name"),
                "state": info.get("state"),
                "status": info.get("status"),
                "server": servers.get(sid, sid) if isinstance(sid, str) else sid,
                "deployed_hash": deployed,
                "latest_hash": latest,
                # Komodo compares against repo HEAD, not the stack's own path, and
                # deploys via BatchDeployStackIfChanged, so a differing hash is the
                # normal state for most stacks. Informational only, never drift.
                "head_differs": bool(deployed and latest and deployed != latest),
                "project_missing": info.get("project_missing"),
                "updates_available": [
                    sv.get("service")
                    for sv in arr(info.get("services"))
                    if sv.get("update_available")
                ],
            }
        )
    return sorted(out, key=lambda r: r["name"] or "")


async def syncs(cfg: Config) -> list[dict[str, Any]]:
    async def one(item: dict[str, Any]) -> dict[str, Any]:
        full = await read(cfg, "GetResourceSync", {"sync": item.get("id") or item.get("name")})
        info = obj(full.get("info"))
        pending = arr(info.get("resource_updates"))
        return {
            "name": full.get("name") or item.get("name"),
            "state": (obj(item.get("info"))).get("state"),
            "last_sync_ts": info.get("last_sync_ts"),
            "last_sync_hash": info.get("last_sync_hash"),
            "pending_hash": info.get("pending_hash"),
            "pending_error": info.get("pending_error"),
            "pending_updates": [pick(u, "target", "data") for u in pending][:50],
            "pending_deploy": (obj(info.get("pending_deploy"))).get("to_deploy"),
            "has_pending": bool(pending or info.get("pending_error")),
        }

    return await gather_map(arr(await read(cfg, "ListResourceSyncs")), one)


async def summary(cfg: Config) -> dict[str, Any]:
    st, sy = await gather_map((stacks(cfg), syncs(cfg)), lambda coro: coro)
    return {
        "stacks_total": len(st),
        "stacks_not_running": [s["name"] for s in st if s["state"] != "running"],
        "syncs_pending": [s["name"] for s in sy if s["has_pending"]],
    }


class Servers(TypedDict):
    servers: list[dict[str, Any]]


class Stack(TypedDict):
    name: str
    config: Any
    info: Any
    services: Any


class SyncStatus(TypedDict):
    syncs: list[dict[str, Any]]


def register(app: MCPServer, cfg: Config) -> None:
    @tool(
        app,
        "Komodo servers (Periphery agents) with connection state and version.",
        title="Komodo servers",
    )
    async def komodo_servers() -> Servers:
        rows: list[dict[str, Any]] = []
        for s in arr(await read(cfg, "ListServers")):
            info = obj(s.get("info"))
            rows.append(
                {"name": s.get("name"), **pick(info, "state", "address", "version", "region")}
            )
        return {"servers": rows}

    @tool(
        app,
        "Komodo stacks: state, server, deployed commit vs repo HEAD (head_differs is "
        "informational: unrelated commits move HEAD without a redeploy), "
        "services with image updates available.",
        title="Komodo stacks",
    )
    async def komodo_stacks(
        only_problems: Annotated[
            bool, Field(description="Only stacks not running or whose project is missing")
        ] = False,
        limit: Annotated[int, Field(description="Rows to return, 1-1000")] = 200,
    ) -> Listing:
        rows = await stacks(cfg)
        if only_problems:
            rows = [r for r in rows if r["state"] != "running" or r["project_missing"]]
        return clip(rows, limit)

    @tool(
        app,
        "One Komodo stack: deploy info, commit messages, missing files, remote errors and "
        "per-service container state.",
        title="Komodo stack",
    )
    async def komodo_stack(
        stack: Annotated[str, Field(description="Stack name")],
    ) -> Stack:
        full, services = await gather_map(
            ("GetStack", "ListStackServices"), lambda kind: read(cfg, kind, {"stack": stack})
        )
        info = obj(full.get("info"))
        services = arr(services)
        return {
            "name": full.get("name"),
            "config": pick(
                obj(full.get("config")),
                "server_id",
                "linked_repo",
                "run_directory",
                "file_paths",
                "branch",
            ),
            "info": pick(
                info,
                "deployed_hash",
                "deployed_message",
                "latest_hash",
                "latest_message",
                "missing_files",
                "remote_errors",
                "deployed_services",
            ),
            "services": [
                {
                    "service": s.get("service"),
                    "image": s.get("image"),
                    "state": (obj(s.get("container"))).get("state"),
                    "status": (obj(s.get("container"))).get("status"),
                    "update_available": s.get("update_available"),
                }
                for s in services
            ],
        }

    @tool(
        app,
        "Recent Komodo updates (deploys, syncs, procedure runs), newest first.",
        title="Komodo updates",
    )
    async def komodo_updates(
        failed_only: Annotated[bool, Field(description="Only updates that failed")] = False,
        limit: Annotated[int, Field(description="Rows to return, 1-1000")] = 30,
    ) -> Listing:
        resp = await read(cfg, "ListUpdates", {"page": 0})
        rows: list[dict[str, Any]] = []
        for u in arr(obj(resp).get("updates") if isinstance(resp, dict) else resp):
            if failed_only and u.get("success", True):
                continue
            rows.append(
                {
                    **pick(u, "operation", "start_ts", "success", "username", "status"),
                    "target": u.get("target"),
                }
            )
        return clip(rows, limit)

    @tool(
        app,
        "Komodo ResourceSync state: last sync, pending resource changes and pending "
        "deploys that a sync would execute. Reports only, never runs the sync.",
        title="Komodo sync status",
    )
    async def komodo_sync_status() -> SyncStatus:
        return {"syncs": await syncs(cfg)}
