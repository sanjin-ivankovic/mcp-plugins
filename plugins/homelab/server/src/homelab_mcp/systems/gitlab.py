"""GitLab job logs and pipeline jobs (token with read_api only). GET only.

GitLab's own MCP server, which this plugin also ships, has no tool for a job's
log or a pipeline's job list; these two fill that gap. Logs go through
redact() before they leave the server, on top of GitLab's own masking.
"""

import asyncio
import re
import time
from typing import Annotated, Any, TypedDict, cast
from urllib.parse import quote

import regex
from mcp.server import MCPServer
from pydantic import Field

from ..common import HttpClient, InputError, Listing, arr, clamp, client_from, clip, pick, tool
from ..config import Config
from ..redact import redact

# ANSI colour codes, and GitLab's collapsible-section markers
# ("section_start:<ts>:<name>[collapsed=true]\r\x1b[0K").
_NOISE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|section_(?:start|end):\d+:[^\r\n]*?\r|\r")
_MAX_LINES = 2000
# Wall-clock budget for one grep over a whole log. The stdlib re cannot be
# interrupted, so a pattern with catastrophic backtracking would hang the
# server; the regex module's per-search timeout stops it instead.
_GREP_SECONDS = 2.0


def client(cfg: Config) -> HttpClient:
    token = cfg.secret(cfg.section("gitlab"), "token", "gitlab")
    return client_from(cfg, "gitlab", path="/api/v4", headers={"PRIVATE-TOKEN": token})


def project_path(project: str) -> str:
    """`homelab/argo-apps`, or a bare name inside the default group."""
    full = project if "/" in project else f"homelab/{project}"
    return quote(full, safe="")


def clean_log(raw: str, grep: str | None, tail: int) -> dict[str, Any]:
    lines = [ln for ln in _NOISE.sub("", raw).splitlines() if ln.strip()]
    total = len(lines)
    if grep:
        if len(grep) > 200:
            raise InputError("grep pattern longer than 200 characters")
        try:
            pattern = regex.compile(grep, regex.IGNORECASE)
        except regex.error as exc:
            raise InputError(f"invalid grep pattern: {exc}") from None
        deadline = time.monotonic() + _GREP_SECONDS
        matched: list[str] = []
        try:
            for ln in lines:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError
                if pattern.search(ln, timeout=left):
                    matched.append(ln)
        except TimeoutError:
            raise InputError(
                f"grep pattern took over {_GREP_SECONDS:g}s on this log; use a simpler one"
            ) from None
        lines = matched
    keep = clamp(tail, 1, _MAX_LINES)
    return {
        "lines_total": total,
        "lines_matched": len(lines),
        "truncated": len(lines) > keep,
        "output": redact("\n".join(lines[-keep:])),
    }


class JobLog(TypedDict):
    project: str
    job_id: int
    lines_total: int
    lines_matched: int
    truncated: bool
    output: str


def register(app: MCPServer, cfg: Config) -> None:
    @tool(
        app,
        "Jobs of one GitLab pipeline: id, name, stage, status, duration, failure reason. "
        "project is `homelab/<repo>` or just `<repo>`.",
        title="GitLab pipeline jobs",
    )
    async def gitlab_pipeline_jobs(
        project: Annotated[str, Field(description="`homelab/<repo>` or just `<repo>`")],
        pipeline_id: Annotated[int, Field(description="Pipeline id")],
        limit: Annotated[int, Field(description="Rows to return, 1-1000")] = 100,
    ) -> Listing:
        jobs = await client(cfg).get_all(
            f"/projects/{project_path(project)}/pipelines/{pipeline_id}/jobs"
        )
        rows = [
            pick(j, "id", "name", "stage", "status", "duration", "failure_reason", "allow_failure")
            for j in arr(jobs)
        ]
        return clip(rows, limit)

    @tool(
        app,
        "Log of one GitLab CI job, colour codes and section markers removed, secrets "
        "redacted. grep keeps matching lines (case-insensitive regex); tail keeps the last "
        "N lines (max 2000).",
        title="GitLab job log",
    )
    async def gitlab_job_log(
        project: Annotated[str, Field(description="`homelab/<repo>` or just `<repo>`")],
        job_id: Annotated[int, Field(description="Job id")],
        grep: Annotated[
            str | None, Field(description="Regular expression; keep matching lines only")
        ] = None,
        tail: Annotated[int, Field(description="Last lines to return, 1-2000")] = 200,
    ) -> JobLog:
        raw = await client(cfg).get_text(f"/projects/{project_path(project)}/jobs/{job_id}/trace")
        # Off the event loop: a slow pattern must not stall the server's other calls.
        cleaned = await asyncio.to_thread(clean_log, raw, grep, tail)
        return cast(JobLog, {"project": project, "job_id": job_id, **cleaned})
