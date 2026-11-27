"""The silverbullet server: conditional PUT writes under /.fs/, two reads, redacted failures."""

import pytest
from mcp import Client

from homelab_mcp import silverbullet
from homelab_mcp.common import ApiError

pytestmark = pytest.mark.anyio

SPACE = "https://notes.test/scratch"
PAGE = f"{SPACE}/.fs/Inbox/deploy%20notes.md"


async def call(cfg, name, args=None):
    async with Client(silverbullet.build(cfg)) as c:
        return await c.call_tool(name, args or {})


async def test_write_creates_only_when_missing(full_cfg, respx_mock):
    route = respx_mock.put(PAGE).respond(200, headers={"ETag": '"sha256:a"'})
    r = await call(
        full_cfg,
        "silverbullet_write_page",
        {"page": "Inbox/deploy notes.md", "content": "```bash\nls\n```\n"},
    )
    assert not r.is_error, r.content[0].text
    req = route.calls[0].request
    assert req.headers["Authorization"] == "Bearer sb_silverbullet-token-value"
    assert req.headers["X-Sync-Mode"] == "true"
    assert req.headers["If-None-Match"] == "*"
    assert req.content == b"```bash\nls\n```\n"
    out = r.structured_content
    assert out["action"] == "created"
    assert out["url"] == f"{SPACE}/Inbox/deploy%20notes"


async def test_write_existing_page_needs_overwrite(full_cfg, respx_mock):
    respx_mock.put(PAGE).respond(412, text="precondition failed")
    r = await call(
        full_cfg, "silverbullet_write_page", {"page": "Inbox/deploy notes", "content": "x"}
    )
    assert r.is_error
    assert "overwrite" in r.content[0].text


async def test_overwrite_sends_no_precondition(full_cfg, respx_mock):
    route = respx_mock.put(PAGE).respond(200)
    r = await call(
        full_cfg,
        "silverbullet_write_page",
        {"page": "Inbox/deploy notes", "content": "x", "overwrite": True},
    )
    assert not r.is_error, r.content[0].text
    assert "If-None-Match" not in route.calls[0].request.headers
    assert r.structured_content["action"] == "overwritten"


async def test_append_is_conditional_on_etag(full_cfg, respx_mock):
    respx_mock.get(PAGE).respond(200, text="# Notes\n", headers={"ETag": '"sha256:old"'})
    route = respx_mock.put(PAGE).respond(200, headers={"ETag": '"sha256:new"'})
    r = await call(
        full_cfg, "silverbullet_append_page", {"page": "Inbox/deploy notes", "content": "more"}
    )
    assert not r.is_error, r.content[0].text
    req = route.calls[0].request
    assert req.headers["If-Match"] == '"sha256:old"'
    assert req.content == b"# Notes\n\nmore"
    assert r.structured_content["action"] == "appended"


async def test_append_creates_missing_page(full_cfg, respx_mock):
    respx_mock.get(PAGE).respond(404)
    route = respx_mock.put(PAGE).respond(200)
    r = await call(
        full_cfg, "silverbullet_append_page", {"page": "Inbox/deploy notes", "content": "first"}
    )
    assert not r.is_error, r.content[0].text
    assert route.calls[0].request.headers["If-None-Match"] == "*"
    assert r.structured_content["action"] == "created"


async def test_append_conflict_is_reported(full_cfg, respx_mock):
    respx_mock.get(PAGE).respond(200, text="a", headers={"ETag": '"sha256:old"'})
    respx_mock.put(PAGE).respond(412)
    r = await call(
        full_cfg, "silverbullet_append_page", {"page": "Inbox/deploy notes", "content": "b"}
    )
    assert r.is_error
    assert "changed while appending" in r.content[0].text


@pytest.mark.parametrize("bad", ["", "/", "../x", "a/../b", ".config", "a/.hidden", "a//b"])
async def test_page_names_cannot_escape_the_space(full_cfg, respx_mock, bad):
    r = await call(full_cfg, "silverbullet_write_page", {"page": bad, "content": "x"})
    assert r.is_error
    assert not respx_mock.calls


async def test_read_and_list(full_cfg, respx_mock):
    respx_mock.get(PAGE).respond(200, text="hello", headers={"X-Last-Modified": "1700"})
    respx_mock.get(f"{SPACE}/.fs").respond(
        json=[
            {"name": "old.md", "lastModified": 1},
            {"name": "Inbox/deploy notes.md", "lastModified": 9},
            {"name": "image.png", "lastModified": 5},
        ]
    )
    got = (
        await call(full_cfg, "silverbullet_read_page", {"page": "Inbox/deploy notes"})
    ).structured_content
    assert got["content"] == "hello" and got["last_modified"] == "1700"
    listed = (await call(full_cfg, "silverbullet_list_pages", {"limit": 5})).structured_content
    assert [p["page"] for p in listed["items"]] == ["Inbox/deploy notes", "old"]
    one = (await call(full_cfg, "silverbullet_list_pages", {"limit": 1})).structured_content
    assert one["count"] == 2 and one["truncated"]
    assert [p["page"] for p in one["items"]] == ["Inbox/deploy notes"]


async def test_failure_is_redacted(full_cfg, respx_mock):
    respx_mock.put(PAGE).respond(401, text="bad token sb_silverbullet-token-value")
    r = await call(
        full_cfg, "silverbullet_write_page", {"page": "Inbox/deploy notes", "content": "x"}
    )
    assert r.is_error
    assert "sb_silverbullet-token-value" not in r.content[0].text
    assert "401" in r.content[0].text


async def test_annotations_mark_only_writes(full_cfg):
    async with Client(silverbullet.build(full_cfg)) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools) == {
        "silverbullet_write_page",
        "silverbullet_append_page",
        "silverbullet_read_page",
        "silverbullet_list_pages",
    }
    for name in ("silverbullet_write_page", "silverbullet_append_page"):
        assert tools[name].annotations.read_only_hint is False
    # overwrite=true replaces a page; append only adds.
    assert tools["silverbullet_write_page"].annotations.destructive_hint is True
    assert tools["silverbullet_append_page"].annotations.destructive_hint is False
    for name in ("silverbullet_read_page", "silverbullet_list_pages"):
        assert tools[name].annotations.read_only_hint is True


async def test_put_outside_allowlist_is_refused(full_cfg):
    http = silverbullet.client(full_cfg)
    with pytest.raises(ApiError, match="not an allowlisted write endpoint"):
        await http.put_write("/.shell", content=b"x")


async def test_other_clients_cannot_put(full_cfg):
    from homelab_mcp import opengist

    with pytest.raises(ApiError, match="not an allowlisted write endpoint"):
        await opengist.client(full_cfg).put_write("/gists/x", content=b"x")
