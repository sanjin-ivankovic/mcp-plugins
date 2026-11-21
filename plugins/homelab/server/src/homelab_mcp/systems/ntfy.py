"""ntfy (token of the ntfy user mcp, read-only on four topics). GET only.

The server's ACL grants `mcp` read on alerts, backups, ci and deploys and
nothing else: any other topic and every publish answer 403. Messages are
read with the poll API (`/<topics>/json?poll=1`), which returns NDJSON and
never holds the connection open. The server keeps messages for 12 h.
"""

import json
from collections import Counter
from datetime import UTC, datetime
from typing import Annotated, Any

from mcp.server import MCPServer
from pydantic import Field

from ..common import ApiError, HttpClient, InputError, Listing, client_from, clip, obj, tool
from ..config import Config
from ..redact import redact

TOPICS = ("alerts", "backups", "ci", "deploys")
DEFAULT_PRIORITY = 3
MAX_MESSAGE = 1000


def client(cfg: Config) -> HttpClient:
    token = cfg.secret(cfg.section("ntfy"), "token", "ntfy")
    return client_from(cfg, "ntfy", headers={"Authorization": f"Bearer {token}"})


def iso(ts: Any) -> str | None:
    if not isinstance(ts, int | float):
        return None
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


def parse(body: str) -> list[dict[str, Any]]:
    """The ``message`` events of an NDJSON poll body; open, keepalive and poll_request are not."""
    events: list[dict[str, Any]] = []
    for line in body.splitlines():
        if not line.strip():
            continue
        try:
            event = obj(json.loads(line))
        except ValueError:
            raise ApiError("ntfy: poll returned a line that is not JSON") from None
        if event.get("event") == "message":
            events.append(event)
    return events


def shape(m: dict[str, Any]) -> dict[str, Any]:
    text = redact(str(m.get("message") or ""))
    if len(text) > MAX_MESSAGE:
        text = text[:MAX_MESSAGE] + "…"
    row = {
        "time": iso(m.get("time")),
        "topic": m.get("topic"),
        "priority": m.get("priority") or DEFAULT_PRIORITY,
        "title": m.get("title"),
        "message": text,
        "tags": m.get("tags"),
        "click": m.get("click"),
        "id": m.get("id"),
    }
    return {k: v for k, v in row.items() if v not in (None, "", [])}


async def messages(cfg: Config, topic: str | None, since: str) -> list[dict[str, Any]]:
    """Messages of ``topic`` (every readable topic when None), newest first."""
    if topic is not None and topic not in TOPICS:
        raise InputError(f"ntfy topic '{topic}' is not readable; readable: {list(TOPICS)}")
    path = "/" + (topic or ",".join(TOPICS)) + "/json"
    body = await client(cfg).get_text(path, poll=1, since=since)
    events = parse(body)
    events.sort(key=lambda m: m.get("time") or 0, reverse=True)
    return events


async def summary(cfg: Config) -> dict[str, Any]:
    events = await messages(cfg, None, "12h")
    counts = Counter(m.get("topic") for m in events)
    out: dict[str, Any] = {"messages_12h": {t: counts.get(t, 0) for t in TOPICS}}
    if events:
        newest = events[0]
        out["newest"] = {
            k: v
            for k, v in {
                "time": iso(newest.get("time")),
                "topic": newest.get("topic"),
                "title": newest.get("title"),
            }.items()
            if v
        }
    return out


def register(app: MCPServer, cfg: Config) -> None:
    @tool(
        app,
        "Recent ntfy notifications (newest first) with time, topic, priority, title, message "
        "and tags. Only the topics alerts, backups, ci and deploys are readable; the server "
        "keeps messages for 12 h, so nothing older comes back.",
        title="ntfy messages",
    )
    async def ntfy_messages(
        topic: Annotated[
            str | None,
            Field(description="alerts, backups, ci or deploys; all four when absent"),
        ] = None,
        since: Annotated[
            str,
            Field(description="Duration (30m, 12h), Unix time, message id or all; at most 12 h"),
        ] = "12h",
        limit: Annotated[int, Field(description="Rows to return, 1-1000")] = 30,
        min_priority: Annotated[
            int | None, Field(description="Lowest priority to keep, 1 (min) to 5 (urgent)")
        ] = None,
    ) -> Listing:
        if min_priority is not None and not 1 <= min_priority <= 5:
            raise InputError("min_priority must be 1-5")
        if not since.strip():
            raise InputError("since must not be empty")
        rows = [shape(m) for m in await messages(cfg, topic, since.strip())]
        if min_priority is not None:
            rows = [r for r in rows if r["priority"] >= min_priority]
        return clip(rows, limit)
