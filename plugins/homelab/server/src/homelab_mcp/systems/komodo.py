"""Komodo Core (API key of a user with base permission Read).

Only ``POST /read`` is reachable, and only with request types in READ_TYPES.
"""

from typing import Any

from mcp.server import MCPServer

from ..common import ApiError, HttpClient, clip, pick, tool
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
    http = HttpClient(
        "komodo",
        sec["url"],
        headers={
            "X-Api-Key": cfg.secret(sec, "key", "komodo"),
            "X-Api-Secret": cfg.secret(sec, "secret", "komodo"),
        },
        verify=bool(sec.get("verify_tls", True)),
        read_posts={"/read"},
    )
    return await http.post_read("/read", json={"type": type_, "params": params or {}})


async def server_names(cfg: Config) -> dict[str, str]:
    return {s.get("id"): s.get("name") for s in await read(cfg, "ListServers") or []}


async def stacks(cfg: Config) -> list[dict[str, Any]]:
    servers = await server_names(cfg)
    out = []
    for s in await read(cfg, "ListStacks") or []:
        info = s.get("info") or {}
        deployed, latest = info.get("deployed_hash"), info.get("latest_hash")
        out.append(
            {
                "name": s.get("name"),
                "state": info.get("state"),
                "status": info.get("status"),
                "server": servers.get(info.get("server_id"), info.get("server_id")),
                "deployed_hash": deployed,
                "latest_hash": latest,
                # Komodo compares against repo HEAD, not the stack's own path, and
                # deploys via BatchDeployStackIfChanged, so a differing hash is the
                # normal state for most stacks. Informational only, never drift.
                "head_differs": bool(deployed and latest and deployed != latest),
                "project_missing": info.get("project_missing"),
                "updates_available": [
                    sv.get("service")
                    for sv in info.get("services") or []
                    if sv.get("update_available")
                ],
            }
        )
    return sorted(out, key=lambda r: r["name"] or "")


async def syncs(cfg: Config) -> list[dict[str, Any]]:
    out = []
    for item in await read(cfg, "ListResourceSyncs") or []:
        full = await read(cfg, "GetResourceSync", {"sync": item.get("id") or item.get("name")})
        info = full.get("info") or {}
        pending = info.get("resource_updates") or []
        out.append(
            {
                "name": full.get("name") or item.get("name"),
                "state": (item.get("info") or {}).get("state"),
                "last_sync_ts": info.get("last_sync_ts"),
                "last_sync_hash": info.get("last_sync_hash"),
                "pending_hash": info.get("pending_hash"),
                "pending_error": info.get("pending_error"),
                "pending_updates": [pick(u, "target", "data") for u in pending][:50],
                "pending_deploy": (info.get("pending_deploy") or {}).get("to_deploy"),
                "has_pending": bool(pending or info.get("pending_error")),
            }
        )
    return out


async def summary(cfg: Config) -> dict[str, Any]:
    st = await stacks(cfg)
    sy = await syncs(cfg)
    return {
        "stacks_total": len(st),
        "stacks_not_running": [s["name"] for s in st if s["state"] != "running"],
        "syncs_pending": [s["name"] for s in sy if s["has_pending"]],
    }


def register(app: MCPServer, cfg: Config) -> None:
    @tool(app, "Komodo servers (Periphery agents) with connection state and version.")
    async def komodo_servers() -> dict[str, Any]:
        rows = []
        for s in await read(cfg, "ListServers") or []:
            info = s.get("info") or {}
            rows.append(
                {"name": s.get("name"), **pick(info, "state", "address", "version", "region")}
            )
        return {"servers": rows}

    @tool(
        app,
        "Komodo stacks: state, server, deployed commit vs repo HEAD (head_differs is "
        "informational: unrelated commits move HEAD without a redeploy), "
        "services with image updates available.",
    )
    async def komodo_stacks(only_problems: bool = False, limit: int = 200) -> dict[str, Any]:
        rows = await stacks(cfg)
        if only_problems:
            rows = [r for r in rows if r["state"] != "running" or r["project_missing"]]
        return clip(rows, limit)

    @tool(
        app,
        "One Komodo stack: deploy info, commit messages, missing files, remote errors and "
        "per-service container state.",
    )
    async def komodo_stack(stack: str) -> dict[str, Any]:
        full = await read(cfg, "GetStack", {"stack": stack})
        info = full.get("info") or {}
        services = await read(cfg, "ListStackServices", {"stack": stack}) or []
        return {
            "name": full.get("name"),
            "config": pick(
                full.get("config") or {},
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
                    "state": (s.get("container") or {}).get("state"),
                    "status": (s.get("container") or {}).get("status"),
                    "update_available": s.get("update_available"),
                }
                for s in services
            ],
        }

    @tool(app, "Recent Komodo updates (deploys, syncs, procedure runs), newest first.")
    async def komodo_updates(failed_only: bool = False, limit: int = 30) -> dict[str, Any]:
        resp = await read(cfg, "ListUpdates", {"page": 0})
        rows = []
        for u in (resp.get("updates") if isinstance(resp, dict) else resp) or []:
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
    )
    async def komodo_sync_status() -> dict[str, Any]:
        return {"syncs": await syncs(cfg)}
