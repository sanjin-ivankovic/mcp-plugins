"""Config, redaction, HTTP guard and the read-only contract of the whole server."""

import asyncio

import httpx
import pytest
from mcp import Client

from homelab_mcp import common, config, docmost, opengist, redact, searxng, server
from homelab_mcp.common import (
    ApiError,
    HttpClient,
    InputError,
    clamp,
    failure_message,
    log_tail,
    select,
    tail_lines,
)

pytestmark = pytest.mark.anyio


def test_secret_from_file_and_command(make_cfg):
    cfg = make_cfg(
        '[a]\ntoken_file = "secrets/t"\n[b]\ntoken_command = ["echo", "from-cmd"]\n',
        {"t": "from-file"},
    )
    assert cfg.secret(cfg.section("a"), "token", "a") == "from-file"
    assert cfg.secret(cfg.section("b"), "token", "b") == "from-cmd"


def test_inline_secret_refused(make_cfg):
    cfg = make_cfg('[a]\ntoken = "plaintext"\n')
    with pytest.raises(config.ConfigError, match="inline 'token' is refused"):
        cfg.secret(cfg.section("a"), "token", "a")


def test_missing_config_does_not_raise_on_load(tmp_path):
    cfg = config.load(tmp_path / "nope.toml")
    assert cfg.load_error
    with pytest.raises(config.ConfigError):
        cfg.section("pve")


def test_redact_masks_registered_and_pattern_secrets():
    redact.register("s3cr3t-value")
    text = "failed with s3cr3t-value; Authorization: Bearer abc; PVEAPIToken=u@pve!t=xyz; ?token=q"
    out = redact.redact(text)
    for leaked in ("s3cr3t-value", "abc", "xyz", "=q"):
        assert leaked not in out


async def test_post_outside_allowlist_is_refused():
    http = HttpClient("t", "http://x.test", read_posts={"/read"})
    with pytest.raises(ApiError, match="not an allowlisted read endpoint"):
        await http.post_read("/write", json={})


async def test_every_tool_is_annotated_read_only(full_cfg):
    async with Client(server.build(full_cfg)) as c:
        tools = (await c.list_tools()).tools
    # Exact, so a tool that silently fails to register is caught; a new tool
    # moves this number in the same change.
    assert len(tools) == 50
    for t in tools:
        assert t.annotations.read_only_hint is True, t.name
        assert t.annotations.destructive_hint is False, t.name


async def test_overview_isolates_failures(full_cfg, monkeypatch):
    async def boom(cfg):
        raise ApiError("pve: GET /nodes -> HTTP 500 token pve-secret-uuid")

    async def fine(cfg):
        return {"ok": True}

    monkeypatch.setitem(server.SUMMARIES, "pve", (boom, "pve"))
    for name in list(server.SUMMARIES):
        if name != "pve":
            monkeypatch.setitem(server.SUMMARIES, name, (fine, server.SUMMARIES[name][1]))
    full_cfg.secret(full_cfg.section("pve"), "token_secret", "pve")  # registers the secret
    out = await server.overview(full_cfg)
    assert out["errors"] == ["pve"]
    assert "pve-secret-uuid" not in out["systems"]["pve"]["error"]
    assert out["systems"]["pbs"] == {"ok": True}


async def test_tool_error_is_redacted(full_cfg, respx_mock):
    respx_mock.get("https://pve.test:8006/api2/json/nodes").respond(
        401, text="bad token pve-secret-uuid"
    )
    async with Client(server.build(full_cfg)) as c:
        r = await c.call_tool("pve_nodes", {})
    assert r.is_error
    assert "HTTP 401" in r.content[0].text
    assert "pve-secret-uuid" not in r.content[0].text


def test_secret_command_runs_once(make_cfg, tmp_path):
    counter = tmp_path / "count"
    script = tmp_path / "secret.sh"
    script.write_text(f'#!/bin/sh\necho x >> "{counter}"\necho cmd-secret-value\n')
    script.chmod(0o700)
    cfg = make_cfg(f'[a]\ntoken_command = ["{script}"]\n')
    for _ in range(3):
        assert cfg.secret(cfg.section("a"), "token", "a") == "cmd-secret-value"
    assert counter.read_text().count("x") == 1


def test_require_names_the_missing_key(make_cfg):
    cfg = make_cfg("[a]\nurl = ''\n")
    with pytest.raises(config.ConfigError, match=r"\[a\] is missing 'url'"):
        cfg.require(cfg.section("a"), "url", "a")


def test_select_clamp_and_input_error_redaction():
    items = [{"n": "a"}, {"n": "b"}]
    assert select(items, None, lambda i: i["n"], "node") == items
    assert select(items, "b", lambda i: i["n"], "node") == [{"n": "b"}]
    with pytest.raises(InputError, match=r"unknown node 'c'; known: \['a', 'b'\]"):
        select(items, "c", lambda i: i["n"], "node")
    assert (clamp(0, 1, 5), clamp(9, 1, 5), clamp(3, 1, 5)) == (1, 5, 3)
    redact.register("input-secret-value")
    assert "input-secret-value" not in failure_message(InputError("bad input-secret-value"))


def test_tail_lines_and_log_tail_redact():
    redact.register("tail-secret-value")
    out = tail_lines(["a", "b tail-secret-value", "c"], 2)
    assert out == {"lines_total": 3, "truncated": True, "output": "b ***\nc"}
    assert log_tail("\x1b[31mred\x1b[0m " + "x" * 20, max_chars=10) == "…" + "x" * 10


async def test_requests_share_one_pool(respx_mock):
    respx_mock.get("https://a.test/x").respond(json={"ok": 1})
    respx_mock.get("https://b.test/y").respond(json={"ok": 2})
    await HttpClient("a", "https://a.test").get("/x")
    await HttpClient("b", "https://b.test").get("/y")
    await HttpClient("c", "https://b.test", verify=False).get("/y")
    assert set(common._TRANSPORTS) == {True, False}


async def test_get_all_follows_pages(respx_mock):
    route = respx_mock.get("https://a.test/items").mock(
        side_effect=lambda req: httpx.Response(
            200, json=[{"p": req.url.params["page"]}] * (2 if req.url.params["page"] != "3" else 1)
        )
    )
    items = await HttpClient("a", "https://a.test").get_all("/items", per_page=2)
    assert [i["p"] for i in items] == ["1", "1", "2", "2", "3"]
    assert route.call_count == 3


async def test_session_logs_in_once_under_concurrency_and_renews_on_401():
    logins = []

    async def login() -> str:
        logins.append(1)
        await asyncio.sleep(0)
        return f"tok-{len(logins)}"

    cache: dict = {}
    session = common.Session(cache, "k", login)

    async def ok(token: str) -> str:
        return token

    assert set(await asyncio.gather(*(session.call(ok) for _ in range(5)))) == {"tok-1"}
    assert len(logins) == 1

    async def expires_once(token: str) -> str:
        if token == "tok-1":
            raise ApiError("expired", status=401)
        return token

    assert await session.call(expires_once) == "tok-2"


async def test_instructions_and_descriptions_fit_tool_search(full_cfg):
    # Claude Code truncates server instructions and tool descriptions at 4096 characters.
    for build in (server.build, docmost.build, opengist.build, searxng.build):
        app = build(full_cfg)
        async with Client(app) as c:
            tools = (await c.list_tools()).tools
        for t in tools:
            assert len(t.description) < 4096, t.name
    for text in (
        server.INSTRUCTIONS,
        docmost.INSTRUCTIONS,
        opengist.INSTRUCTIONS,
        searxng.INSTRUCTIONS,
    ):
        assert len(text) < 4096


async def test_get_is_retried_after_a_gateway_error(respx_mock):
    route = respx_mock.get("http://x.test/items").mock(
        side_effect=[httpx.Response(503), httpx.Response(502), httpx.Response(200, json=[1])]
    )
    assert await HttpClient("t", "http://x.test").get("/items") == [1]
    assert route.call_count == 3


async def test_get_gives_up_after_the_retries(respx_mock):
    route = respx_mock.get("http://x.test/items").respond(504)
    with pytest.raises(ApiError, match="HTTP 504"):
        await HttpClient("t", "http://x.test").get("/items")
    assert route.call_count == 1 + len(common.RETRY_DELAYS)


async def test_get_is_retried_after_a_dropped_connection(respx_mock):
    route = respx_mock.get("http://x.test/items").mock(
        side_effect=[httpx.RemoteProtocolError("dropped"), httpx.Response(200, json={})]
    )
    assert await HttpClient("t", "http://x.test").get("/items") == {}
    assert route.call_count == 2


async def test_writes_and_client_errors_are_not_retried(respx_mock):
    post = respx_mock.post("http://x.test/gists").respond(503)
    get = respx_mock.get("http://x.test/missing").respond(404)
    http = HttpClient("t", "http://x.test", write_posts={"/gists"})
    with pytest.raises(ApiError, match="HTTP 503"):
        await http.post_write("/gists", json={})
    with pytest.raises(ApiError, match="HTTP 404"):
        await http.get("/missing")
    assert post.call_count == 1 and get.call_count == 1


def test_obj_and_arr_keep_the_right_json_type_only():
    assert common.obj({"a": 1}) == {"a": 1} and common.arr([1]) == [1]
    for wrong in (None, "x", 3, [1], {"a": 1}):
        assert common.obj(wrong) == ({"a": 1} if isinstance(wrong, dict) else {})
        assert common.arr(wrong) == ([1] if isinstance(wrong, list) else [])
