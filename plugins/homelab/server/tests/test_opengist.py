"""The opengist server: one allowlisted write, two reads, redacted failures."""

import json

import pytest
from mcp import Client

from homelab_mcp import opengist
from homelab_mcp.common import ApiError

pytestmark = pytest.mark.anyio

API = "https://gist.test/api"
GIST = {
    "id": "d1e1ce94e7be4e548d7dabd25330559f",
    "title": "probe",
    "description": "",
    "visibility": "unlisted",
    "html_url": "https://gist.test/me/d1e1ce94e7be4e548d7dabd25330559f",
    "expires_at": "2026-11-20T08:00:00Z",
    "updated_at": "2026-11-13T08:00:00Z",
    "files": {
        "run.sh": {"filename": "run.sh", "language": "Shell", "size": 13, "content": "echo hi"},
        "read me.md": {"filename": "read me.md", "language": "Markdown", "size": 2, "content": "x"},
    },
}


async def call(cfg, name, args=None):
    async with Client(opengist.build(cfg)) as c:
        return await c.call_tool(name, args or {})


async def test_create_posts_body_and_returns_raw_urls(full_cfg, respx_mock):
    route = respx_mock.post(f"{API}/gists").respond(201, json=GIST)
    r = await call(
        full_cfg,
        "opengist_create_gist",
        {"files": {"run.sh": "echo hi", "empty.txt": ""}, "title": "probe"},
    )
    assert not r.is_error, r.content[0].text
    req = route.calls[0].request
    assert req.headers["Authorization"] == "Bearer og_opengist-token-value"
    body = json.loads(req.content)
    assert body == {
        "visibility": "unlisted",
        "expire": "7days",
        "files": {"run.sh": {"content": "echo hi"}},
        "title": "probe",
    }
    out = r.structured_content
    assert out["html_url"] == GIST["html_url"]
    assert out["raw_urls"]["run.sh"] == f"{GIST['html_url']}/raw/HEAD/run.sh"
    assert out["raw_urls"]["read me.md"] == f"{GIST['html_url']}/raw/HEAD/read%20me.md"


async def test_create_refuses_only_empty_files(full_cfg, respx_mock):
    r = await call(full_cfg, "opengist_create_gist", {"files": {"a.txt": ""}})
    assert r.is_error
    assert not respx_mock.calls


async def test_failure_is_redacted(full_cfg, respx_mock):
    respx_mock.post(f"{API}/gists").respond(401, text="bad token og_opengist-token-value")
    r = await call(full_cfg, "opengist_create_gist", {"files": {"a.txt": "x"}})
    assert r.is_error
    assert "og_opengist-token-value" not in r.content[0].text
    assert "401" in r.content[0].text


async def test_get_and_list(full_cfg, respx_mock):
    respx_mock.get(f"{API}/gists/{GIST['id']}").respond(json=GIST)
    respx_mock.get(f"{API}/gists", params={"per_page": 5}).respond(json=[GIST, GIST])
    got = (await call(full_cfg, "opengist_get_gist", {"id": GIST["id"]})).structured_content
    assert got["files"]["run.sh"]["content"] == "echo hi"
    listed = (await call(full_cfg, "opengist_list_gists", {"limit": 5})).structured_content
    assert listed["count"] == 2 and listed["items"][0]["id"] == GIST["id"]


async def test_annotations_mark_only_create_as_write(full_cfg):
    async with Client(opengist.build(full_cfg)) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools) == {"opengist_create_gist", "opengist_get_gist", "opengist_list_gists"}
    assert tools["opengist_create_gist"].annotations.read_only_hint is False
    assert tools["opengist_create_gist"].annotations.destructive_hint is False
    assert tools["opengist_get_gist"].annotations.read_only_hint is True
    assert tools["opengist_list_gists"].annotations.read_only_hint is True


async def test_write_outside_allowlist_is_refused(full_cfg):
    http = opengist.client(full_cfg)
    with pytest.raises(ApiError, match="not an allowlisted write endpoint"):
        await http.post_write("/gists/abc/forks", json={})
