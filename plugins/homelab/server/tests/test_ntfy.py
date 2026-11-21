"""ntfy: NDJSON poll parsing, ordering, filters, topic guard and the overview summary."""

import json

import pytest
from mcp import Client

from homelab_mcp import server
from homelab_mcp.systems import ntfy

pytestmark = pytest.mark.anyio

NTFY = "https://ntfy.test"
ALL = f"{NTFY}/alerts,backups,ci,deploys/json"


def ndjson(*events):
    return "\n".join(json.dumps(e) for e in events) + "\n"


def msg(id_, time, topic, **extra):
    base = {"id": id_, "time": time, "expires": time + 43200, "event": "message", "topic": topic}
    return base | extra


BODY = ndjson(
    {"id": "o1", "time": 1795166000, "event": "open", "topic": "alerts,backups,ci,deploys"},
    msg("m1", 1795166100, "backups", title="PBS job ok", message="done", tags=["white_check_mark"]),
    {"id": "k1", "time": 1795166200, "event": "keepalive", "topic": "alerts"},
    msg("m3", 1795166511, "alerts", title="DiskFull", message="disk 95%", priority=5),
    msg("m2", 1795166300, "ci", message="pipeline passed", priority=2),
)


async def call(cfg, name, args=None):
    async with Client(server.build(cfg)) as c:
        r = await c.call_tool(name, args or {})
    assert not r.is_error, r.content[0].text
    return r.structured_content


async def call_error(cfg, name, args):
    async with Client(server.build(cfg)) as c:
        r = await c.call_tool(name, args)
    assert r.is_error
    return r.content[0].text


async def test_messages_all_topics_newest_first(full_cfg, respx_mock):
    route = respx_mock.get(ALL).respond(text=BODY)
    out = await call(full_cfg, "ntfy_messages")
    assert out["count"] == 3 and not out["truncated"]
    assert [m["id"] for m in out["items"]] == ["m3", "m2", "m1"]
    assert out["items"][0] == {
        "time": "2026-11-20T09:21:51Z",
        "topic": "alerts",
        "priority": 5,
        "title": "DiskFull",
        "message": "disk 95%",
        "id": "m3",
    }
    assert out["items"][2]["priority"] == 3
    assert out["items"][2]["tags"] == ["white_check_mark"]
    request = route.calls[0].request
    assert request.url.params["poll"] == "1" and request.url.params["since"] == "12h"
    assert request.headers["Authorization"] == "Bearer tk_ntfy-token-value"


async def test_messages_one_topic_since_limit_and_priority(full_cfg, respx_mock):
    route = respx_mock.get(f"{NTFY}/alerts/json").respond(text=BODY)
    out = await call(full_cfg, "ntfy_messages", {"topic": "alerts", "since": "30m", "limit": 1})
    assert out["count"] == 3 and out["truncated"] and [m["id"] for m in out["items"]] == ["m3"]
    assert route.calls[0].request.url.params["since"] == "30m"
    high = await call(full_cfg, "ntfy_messages", {"topic": "alerts", "min_priority": 3})
    assert [m["id"] for m in high["items"]] == ["m3", "m1"]


async def test_long_message_is_clipped(full_cfg, respx_mock):
    respx_mock.get(ALL).respond(text=ndjson(msg("m1", 1795166100, "ci", message="x" * 5000)))
    out = await call(full_cfg, "ntfy_messages")
    assert len(out["items"][0]["message"]) == ntfy.MAX_MESSAGE + 1


async def test_unknown_topic_and_bad_arguments_rejected(full_cfg, respx_mock):
    text = await call_error(full_cfg, "ntfy_messages", {"topic": "secrets"})
    assert "not readable" in text and "deploys" in text
    assert "min_priority" in await call_error(full_cfg, "ntfy_messages", {"min_priority": 6})
    assert "since" in await call_error(full_cfg, "ntfy_messages", {"since": " "})
    assert not respx_mock.calls


async def test_forbidden_and_garbage_bodies_surface_as_errors(full_cfg, respx_mock):
    route = respx_mock.get(ALL).respond(403, json={"code": 40301, "error": "forbidden"})
    assert "HTTP 403" in await call_error(full_cfg, "ntfy_messages", {})
    route.respond(text="<html>login</html>\n")
    assert "not JSON" in await call_error(full_cfg, "ntfy_messages", {})


async def test_summary(full_cfg, respx_mock):
    route = respx_mock.get(ALL).respond(text=BODY)
    assert await ntfy.summary(full_cfg) == {
        "messages_12h": {"alerts": 1, "backups": 1, "ci": 1, "deploys": 0},
        "newest": {"time": "2026-11-20T09:21:51Z", "topic": "alerts", "title": "DiskFull"},
    }
    route.respond(text="")
    assert await ntfy.summary(full_cfg) == {
        "messages_12h": {"alerts": 0, "backups": 0, "ci": 0, "deploys": 0}
    }
