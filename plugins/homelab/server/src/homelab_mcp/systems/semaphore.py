"""Semaphore UI (token of a user with the Guest project role). GET only.

Targets the host directly: the ingress is behind Authentik forward-auth,
which an API token cannot pass.
"""

from typing import Any

from mcp.server import MCPServer

from ..common import HttpClient, clip, pick, tool
from ..config import Config, ConfigError

FAILED = {"error", "failed", "stopped"}


def client(cfg: Config) -> HttpClient:
    sec = cfg.section("semaphore")
    return HttpClient(
        "semaphore",
        sec["url"].rstrip("/") + "/api",
        headers={"Authorization": f"Bearer {cfg.secret(sec, 'token', 'semaphore')}"},
    )


async def projects(cfg: Config, name: str | None) -> list[dict[str, Any]]:
    ps = await client(cfg).get("/projects") or []
    if name is None:
        return ps
    match = [p for p in ps if p.get("name") == name or str(p.get("id")) == name]
    if not match:
        raise ConfigError(f"no semaphore project '{name}'")
    return match


async def tasks(cfg: Config, project: str | None = None) -> list[dict[str, Any]]:
    http = client(cfg)
    rows = []
    for p in await projects(cfg, project):
        names = {
            t.get("id"): t.get("name")
            for t in await http.get(f"/project/{p['id']}/templates") or []
        }
        for t in await http.get(f"/project/{p['id']}/tasks/last") or []:
            rows.append(
                {
                    "project": p.get("name"),
                    "id": t.get("id"),
                    "template": names.get(t.get("template_id"), t.get("tpl_alias")),
                    **pick(
                        t,
                        "status",
                        "created",
                        "start",
                        "end",
                        "user_name",
                        "message",
                        "commit_hash",
                        "playbook",
                    ),
                }
            )
    rows.sort(key=lambda r: r.get("created") or "", reverse=True)
    return rows


async def summary(cfg: Config) -> dict[str, Any]:
    rows = await tasks(cfg)
    latest: dict[str, dict[str, Any]] = {}
    for r in rows:
        latest.setdefault(r["template"] or "?", r)
    return {
        "templates_last_failed": sorted(k for k, v in latest.items() if v.get("status") in FAILED)
    }


def register(app: MCPServer, cfg: Config) -> None:
    @tool(
        app,
        "Recent Semaphore task runs (newest first) with template, status, who ran it and "
        "commit. failed_only keeps error/failed/stopped runs.",
    )
    async def semaphore_tasks(
        project: str | None = None,
        template: str | None = None,
        failed_only: bool = False,
        limit: int = 20,
    ) -> dict[str, Any]:
        rows = [
            r
            for r in await tasks(cfg, project)
            if (template is None or r["template"] == template)
            and (not failed_only or r.get("status") in FAILED)
        ]
        return clip(rows, limit)

    @tool(app, "Output of one Semaphore task (last `tail` lines).")
    async def semaphore_task_output(
        task_id: int, project: str | None = None, tail: int = 200
    ) -> dict[str, Any]:
        http = client(cfg)
        for p in await projects(cfg, project):
            lines = await http.get(f"/project/{p['id']}/tasks/{task_id}/output")
            if lines:
                text = [ln.get("output", "") for ln in lines]
                return {
                    "project": p.get("name"),
                    "task_id": task_id,
                    "lines_total": len(text),
                    "output": "\n".join(text[-max(1, min(tail, 2000)) :]),
                }
        return {"error": f"task {task_id} not found or has no output"}
