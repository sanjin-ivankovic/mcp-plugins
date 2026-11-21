"""The searxng server: search parameters and shaping, fetch to Markdown, guards."""

import pytest
from mcp import Client

from homelab_mcp import searxng

pytestmark = pytest.mark.anyio

SX = "https://search.test"

ARTICLE = """<html><head><title>Release notes</title></head><body>
<nav>Home | Docs | Blog</nav>
<article>
<h1>Release 2.0</h1>
<p>This release renames the redis setting to valkey and adds a contact URL that
several engines now require before they load.
See <a href="https://example.test/upgrade">the upgrade guide</a> for details.</p>
<table><tr><th>Key</th><th>Status</th></tr><tr><td>redis.url</td><td>deprecated</td></tr></table>
</article>
<footer>Copyright</footer>
</body></html>"""


async def call(cfg, name, args=None):
    async with Client(searxng.build(cfg)) as c:
        return await c.call_tool(name, args or {})


async def ok(cfg, name, args=None):
    r = await call(cfg, name, args)
    assert not r.is_error, r.content[0].text
    return r.structured_content


async def error(cfg, name, args=None) -> str:
    r = await call(cfg, name, args)
    assert r.is_error
    return r.content[0].text


async def test_search_parameters_and_shaping(full_cfg, respx_mock):
    route = respx_mock.get(f"{SX}/search").respond(
        json={
            "query": "valkey url",
            "results": [
                {
                    "url": "https://a.test",
                    "title": "A",
                    "content": "  first\n result " + "x" * 400,
                    "engines": ["braveapi", "google cse"],
                    "score": 2.3333,
                    "publishedDate": "2026-11-01T00:00:00",
                    "category": "general",
                },
                {"url": "https://b.test", "title": "B", "engine": "duckduckgo", "content": ""},
            ],
            "answers": ["42", {"answer": "valkey.url", "url": "https://docs.test"}],
            "infoboxes": [{"infobox": "Valkey", "content": "A store", "urls": [{"url": "u"}]}],
            "suggestions": ["valkey settings"],
            "corrections": [],
            "unresponsive_engines": [["duckduckgo", "CAPTCHA"]],
        }
    )
    out = await ok(
        full_cfg,
        "searxng_search",
        {
            "query": " valkey url ",
            "categories": ["it", "general"],
            "engines": ["braveapi"],
            "time_range": "week",
            "page": 2,
            "limit": 1,
        },
    )
    params = route.calls[0].request.url.params
    assert dict(params) == {
        "q": "valkey url",
        "format": "json",
        "categories": "it,general",
        "engines": "braveapi",
        "time_range": "week",
        "pageno": "2",
        "safesearch": "0",
    }
    assert "text/html" in route.calls[0].request.headers["Accept"]
    assert out["count"] == 2 and out["truncated"]
    first = out["items"][0]
    assert first["snippet"].startswith("first result x") and first["snippet"].endswith("…")
    assert len(first["snippet"]) == 301 and first["score"] == 2.33
    assert out["answers"] == [
        {"answer": "42"},
        {"answer": "valkey.url", "url": "https://docs.test"},
    ]
    assert out["infoboxes"] == [{"name": "Valkey", "content": "A store", "urls": ["u"]}]
    assert out["unresponsive_engines"] == [{"engine": "duckduckgo", "reason": "CAPTCHA"}]
    assert "corrections" not in out


async def test_search_uses_the_configured_language(make_cfg, respx_mock):
    cfg = make_cfg(f'[searxng]\nurl = "{SX}"\nlanguage = "de"\n')
    route = respx_mock.get(f"{SX}/search").respond(json={"results": []})
    await ok(cfg, "searxng_search", {"query": "x"})
    assert route.calls[0].request.url.params["language"] == "de"


async def test_search_errors_explain_the_fix(full_cfg, respx_mock):
    route = respx_mock.get(f"{SX}/search")
    route.respond(403, text="Forbidden")
    assert "search.formats" in await error(full_cfg, "searxng_search", {"query": "x"})
    route.respond(429, text="Too Many Requests")
    assert "pass_ip" in await error(full_cfg, "searxng_search", {"query": "x"})
    calls = route.call_count
    assert "query is empty" in await error(full_cfg, "searxng_search", {"query": "  "})
    assert route.call_count == calls


async def test_fetch_html_as_markdown_in_chunks(full_cfg, respx_mock):
    respx_mock.get("https://docs.test/old").respond(
        301, headers={"location": "https://docs.test/release"}
    )
    respx_mock.get("https://docs.test/release").respond(
        200, text=ARTICLE, headers={"content-type": "text/html; charset=utf-8"}
    )
    out = await ok(full_cfg, "searxng_fetch", {"url": "https://docs.test/old"})
    assert out["final_url"] == "https://docs.test/release"
    assert out["content_type"] == "text/html"
    md = out["content"]
    assert "# Release 2.0" in md
    assert "[the upgrade guide](https://example.test/upgrade)" in md
    assert "| redis.url | deprecated |" in md
    assert "Home | Docs" not in md and "next_index" not in out

    first = await ok(
        full_cfg, "searxng_fetch", {"url": "https://docs.test/release", "max_length": 100}
    )
    assert len(first["content"]) == 100 and first["next_index"] == 100
    rest = await ok(
        full_cfg,
        "searxng_fetch",
        {"url": "https://docs.test/release", "max_length": 100, "start_index": 100},
    )
    assert rest["start_index"] == 100 and first["content"] + rest["content"] == md[:200]


async def test_fetch_text_types_raw_and_binary_refused(full_cfg, respx_mock):
    respx_mock.get("https://x.test/a.txt").respond(
        200, text="plain", headers={"content-type": "text/plain"}
    )
    respx_mock.get("https://x.test/a.pdf").respond(
        200, content=b"%PDF", headers={"content-type": "application/pdf"}
    )
    respx_mock.get("https://x.test/page").respond(
        200, text=ARTICLE, headers={"content-type": "text/html"}
    )
    assert (await ok(full_cfg, "searxng_fetch", {"url": "https://x.test/a.txt"}))[
        "content"
    ] == "plain"
    assert "not application/pdf" in await error(
        full_cfg, "searxng_fetch", {"url": "https://x.test/a.pdf"}
    )
    raw = await ok(
        full_cfg, "searxng_fetch", {"url": "https://x.test/page", "raw": True, "max_length": 50000}
    )
    assert raw["content"].startswith("<html>")


async def test_fetch_guards_refuse_before_any_request(full_cfg, respx_mock):
    for bad in ("ftp://x.test/f", "file:///etc/passwd", "https://user:pw@x.test/", "not a url"):
        text = await error(full_cfg, "searxng_fetch", {"url": bad})
        assert "searxng_fetch" in text
    assert not respx_mock.calls


async def test_fetch_caps_the_download_and_reports_http_errors(make_cfg, respx_mock):
    cfg = make_cfg(f'[searxng]\nurl = "{SX}"\nfetch_max_bytes = 10\n')
    respx_mock.get("https://x.test/big").respond(
        200, text="y" * 1000, headers={"content-type": "text/plain"}
    )
    respx_mock.get("https://x.test/gone").respond(404)
    out = await ok(cfg, "searxng_fetch", {"url": "https://x.test/big"})
    assert out["content"] == "y" * 10 and out["truncated_download"] is True
    assert "HTTP 404" in await error(cfg, "searxng_fetch", {"url": "https://x.test/gone"})


async def test_fetch_redirect_to_a_refused_scheme(full_cfg, respx_mock):
    respx_mock.get("https://x.test/r").respond(302, headers={"location": "file:///etc/passwd"})
    assert "http and https" in await error(full_cfg, "searxng_fetch", {"url": "https://x.test/r"})


async def test_instance_lists_enabled_engines(full_cfg, respx_mock):
    respx_mock.get(f"{SX}/config").respond(
        json={
            "version": "2026.11.12",
            "limiter": False,
            "categories": ["general", "it"],
            "engines": [
                {
                    "name": "braveapi",
                    "shortcut": "brapi",
                    "categories": ["general"],
                    "enabled": True,
                },
                {"name": "bing", "categories": ["general"], "enabled": False},
            ],
        }
    )
    out = await ok(full_cfg, "searxng_instance")
    assert out["version"] == "2026.11.12" and out["limiter"] is False
    assert [e["name"] for e in out["engines"]] == ["braveapi"]
    every = await ok(full_cfg, "searxng_instance", {"enabled_only": False})
    assert len(every["engines"]) == 2


async def test_every_tool_is_read_only(full_cfg):
    async with Client(searxng.build(full_cfg)) as c:
        tools = (await c.list_tools()).tools
    assert sorted(t.name for t in tools) == ["searxng_fetch", "searxng_instance", "searxng_search"]
    for t in tools:
        assert t.annotations.read_only_hint is True and t.annotations.destructive_hint is False
    search = next(t for t in tools if t.name == "searxng_search")
    assert "never internal" in search.input_schema["properties"]["query"]["description"]
