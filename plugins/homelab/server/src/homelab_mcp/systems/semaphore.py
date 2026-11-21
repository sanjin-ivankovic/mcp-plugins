"""Semaphore UI (token of a user with the Guest project role). GET only.

Targets the host directly: the ingress serves the Authentik OIDC login with
a Secure session cookie, which a bearer token cannot use.
"""

from typing import Annotated, Any, TypedDict, cast

from mcp.server import MCPServer
from pydantic import Field

from ..common import (
    HttpClient,
    InputError,
    Listing,
    arr,
    client_from,
    clip,
    gather_map,
    pick,
    tail_lines,
    tool,
)
from ..config import Config

FAILED = {"error", "failed", "stopped"}


def client(cfg: Config) -> HttpClient:
    token = cfg.secret(cfg.section("semaphore"), "token", "semaphore")
    return client_from(cfg, "semaphore", path="/api", headers={"Authorization": f"Bearer {token}"})


async def projects(cfg: Config, name: str | None) -> list[dict[str, Any]]:
    ps = arr(await client(cfg).get("/projects"))
    if name is None:
        return ps
    match = [p for p in ps if p.get("name") == name or str(p.get("id")) == name]
    if not match:
        raise InputError(f"no semaphore project '{name}'")
    return match


async def tasks(cfg: Config, project: str | None = None) -> list[dict[str, Any]]:
    http = client(cfg)

    async def of_project(p: dict[str, Any]) -> list[dict[str, Any]]:
        templates, last = await gather_map(
            ("templates", "tasks/last"), lambda kind: http.get(f"/project/{p['id']}/{kind}")
        )
        names = {t.get("id"): t.get("name") for t in arr(templates)}
        rows: list[dict[str, Any]] = []
        for t in arr(last):
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
        return rows

    rows = [
        r for found in await gather_map(await projects(cfg, project), of_project) for r in found
    ]
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


class TaskOutput(TypedDict):
    project: Any
    task_id: int
    lines_total: int
    truncated: bool
    output: str


def register(app: MCPServer, cfg: Config) -> None:
    @tool(
        app,
        "Recent Semaphore task runs (newest first) with template, status, who ran it and "
        "commit. failed_only keeps error/failed/stopped runs.",
        title="Semaphore task runs",
    )
    async def semaphore_tasks(
        project: Annotated[
            str | None, Field(description="Project name; every project when absent")
        ] = None,
        template: Annotated[str | None, Field(description="Template name, exact")] = None,
        failed_only: Annotated[
            bool, Field(description="Only error, failed or stopped runs")
        ] = False,
        limit: Annotated[int, Field(description="Rows to return, 1-1000")] = 20,
    ) -> Listing:
        rows = [
            r
            for r in await tasks(cfg, project)
            if (template is None or r["template"] == template)
            and (not failed_only or r.get("status") in FAILED)
        ]
        return clip(rows, limit)

    @tool(
        app,
        "Output of one Semaphore task (last `tail` lines, secrets redacted).",
        title="Semaphore task output",
    )
    async def semaphore_task_output(
        task_id: Annotated[int, Field(description="Task id from semaphore_tasks")],
        project: Annotated[
            str | None, Field(description="Project name; every project is tried when absent")
        ] = None,
        tail: Annotated[int, Field(description="Last lines to return, 1-2000")] = 200,
    ) -> TaskOutput:
        http = client(cfg)
        for p in await projects(cfg, project):
            lines = await http.get(f"/project/{p['id']}/tasks/{task_id}/output")
            if lines:
                text = [ln.get("output", "") for ln in lines]
                return cast(
                    TaskOutput,
                    {"project": p.get("name"), "task_id": task_id, **tail_lines(text, tail)},
                )
        raise InputError(f"semaphore task {task_id} not found or has no output")
