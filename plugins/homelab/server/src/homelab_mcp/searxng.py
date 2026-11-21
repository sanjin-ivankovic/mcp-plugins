"""SearXNG server: web search through the homelab SearXNG and pages as Markdown.

Separate from the ``homelab`` server because it reaches beyond the lab: the
search query leaves the LAN for the engines SearXNG enables, and the fetch
tool downloads any http(s) URL. Both are reads; nothing is written anywhere.

The instance has no login and is published on the ``lan`` entrypoint only.
``?format=json`` answers 403 unless the instance lists ``json`` in
``search.formats`` (helm-charts searxng chart, ``search.formats``). The
client sends ``Accept: text/html``: SearXNG's limiter, when enabled, rejects
requests whose Accept header lacks it, even for the JSON format.
"""

import asyncio
from typing import Annotated, Any, Literal, NotRequired, TypedDict, cast
from urllib.parse import urlsplit

import httpx
import trafilatura
from mcp.server import MCPServer
from pydantic import Field

from . import __version__
from .common import (
    MAX_RESULT_CHARS,
    ApiError,
    HttpClient,
    InputError,
    arr,
    clamp,
    client_from,
    clip,
    http_client,
    make_app,
    obj,
    serve,
    tool,
)
from .config import Config

INSTRUCTIONS = """Web search through the homelab SearXNG instance (no account; results
come from the engines the instance enables). Use searxng_search for exact error
messages, GitHub issues, changelogs and release notes, then open the best hits
with searxng_fetch, which returns the page as Markdown in chunks (follow
next_index). Never put internal hostnames, IP addresses, domains or employer
codes in a query: the query leaves the LAN. searxng_fetch also reaches LAN
hosts and does not consult robots.txt. For library and API documentation prefer
the Ref server when it is configured."""

USER_AGENT = f"homelab-mcp/{__version__}"
MAX_REDIRECTS = 5
TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml+xml")

TimeRange = Literal["day", "week", "month", "year"]


def client(cfg: Config) -> HttpClient:
    return client_from(
        cfg,
        "searxng",
        headers={"Accept": "text/html,application/json;q=0.9,*/*;q=0.8", "User-Agent": USER_AGENT},
    )


def search_error(exc: ApiError) -> Exception:
    if exc.status == 403:
        return ApiError(
            "searxng refused format=json (HTTP 403): the instance must list json in "
            "search.formats (helm-charts searxng chart value search.formats)",
            status=403,
        )
    if exc.status == 429:
        return ApiError(
            "searxng rate-limited the request (HTTP 429): its limiter is on; allow this "
            "client in botdetection pass_ip or turn server.limiter off",
            status=429,
        )
    return exc


def answer_text(a: Any) -> dict[str, Any]:
    if isinstance(a, dict):
        d = obj(a)
        return {k: d[k] for k in ("answer", "url") if d.get(k)}
    return {"answer": str(a)}


class SearchHit(TypedDict, total=False):
    """One result; empty fields are left out."""

    url: Any
    title: Any
    snippet: str
    engines: list[Any]
    score: float
    published: Any
    category: Any


class Infobox(TypedDict):
    name: Any
    content: str
    urls: list[Any]


class Search(TypedDict):
    query: Any
    page: int
    count: int
    truncated: bool
    items: list[SearchHit]
    answers: NotRequired[list[dict[str, Any]]]
    infoboxes: NotRequired[list[Infobox]]
    suggestions: NotRequired[list[Any]]
    corrections: NotRequired[list[Any]]
    unresponsive_engines: NotRequired[list[dict[str, Any]]]


class Fetched(TypedDict):
    url: str
    final_url: str
    status: int
    content_type: str
    title: NotRequired[str]
    total_chars: int
    start_index: int
    next_index: NotRequired[int]
    truncated_download: NotRequired[bool]
    content: str


class Engine(TypedDict):
    name: Any
    shortcut: Any
    categories: Any
    time_range: Any


class Instance(TypedDict):
    version: Any
    instance_name: Any
    limiter: Any
    safe_search: Any
    categories: Any
    engines: list[Engine]


def shape_result(r: dict[str, Any]) -> SearchHit:
    content = " ".join(str(r.get("content") or "").split())
    row = {
        "url": r.get("url"),
        "title": r.get("title"),
        "snippet": content[:300] + ("…" if len(content) > 300 else ""),
        "engines": r.get("engines") or ([r["engine"]] if r.get("engine") else []),
        "score": round(float(r["score"]), 2) if r.get("score") is not None else None,
        "published": r.get("publishedDate"),
        "category": r.get("category"),
    }
    return cast(SearchHit, {k: v for k, v in row.items() if v not in (None, "", [])})


def check_url(url: str) -> str:
    """http(s) only, no credentials in the URL."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise InputError("searxng_fetch reads http and https URLs only")
    if parts.username or parts.password:
        raise InputError("searxng_fetch refuses URLs with credentials in them")
    return url.strip()


async def fetch(url: str, *, max_bytes: int, verify: bool, timeout: float) -> dict[str, Any]:
    """GET ``url`` following redirects, reading at most ``max_bytes`` of the body."""
    client = http_client(
        verify=verify, timeout=timeout, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}
    )
    target = url
    try:
        for _ in range(MAX_REDIRECTS + 1):
            async with client.stream("GET", target) as resp:
                if resp.is_redirect:
                    target = check_url(str(resp.url.join(resp.headers.get("location", ""))))
                    continue
                body, truncated = bytearray(), False
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > max_bytes:
                        body, truncated = body[:max_bytes], True
                        break
                return {
                    "final_url": str(resp.url),
                    "status": resp.status_code,
                    "content_type": resp.headers.get("content-type", "").split(";")[0].strip(),
                    "encoding": resp.encoding or "utf-8",
                    "body": bytes(body),
                    "truncated": truncated,
                }
    except httpx.HTTPError as exc:
        raise ApiError(f"searxng_fetch: GET {target} failed: {type(exc).__name__}: {exc}") from None
    raise ApiError(f"searxng_fetch: more than {MAX_REDIRECTS} redirects from {url}")


def to_text(page: dict[str, Any], raw: bool) -> tuple[str, str | None]:
    """The page body as Markdown (HTML) or as is (other text types), and its title."""
    ctype = page["content_type"]
    if ctype and not ctype.startswith(TEXT_TYPES):
        raise InputError(f"searxng_fetch reads text and HTML, not {ctype}")
    text = page["body"].decode(page["encoding"], errors="replace")
    if raw or ctype not in ("text/html", "application/xhtml+xml", ""):
        return text, None
    markdown = trafilatura.extract(
        text,
        url=page["final_url"],
        output_format="markdown",
        include_links=True,
        include_tables=True,
        include_comments=False,
        include_images=False,
    )
    meta = trafilatura.extract_metadata(text, default_url=page["final_url"])
    title = meta.title if meta else None
    if not markdown:
        markdown = trafilatura.html2txt(text) or ""
    return markdown, title


def build(cfg: Config) -> MCPServer[None]:
    app = make_app("searxng", INSTRUCTIONS)

    @tool(
        app,
        "Web search through the homelab SearXNG. Returns ranked results (url, title, "
        "snippet, engines, score, published date) plus any direct answers, infoboxes, "
        "suggestions and engines that did not answer. Open promising results with "
        "searxng_fetch instead of answering from snippets.",
        title="Web search",
    )
    async def searxng_search(
        query: Annotated[str, Field(description="Search terms; never internal names or IPs")],
        categories: Annotated[
            list[str] | None,
            Field(description="SearXNG categories, e.g. general, it, news, science, repos"),
        ] = None,
        engines: Annotated[
            list[str] | None, Field(description="Engine names to use instead of the defaults")
        ] = None,
        language: Annotated[str | None, Field(description="Language code, e.g. en or de")] = None,
        time_range: Annotated[TimeRange | None, Field(description="Only recent results")] = None,
        page: Annotated[int, Field(description="Result page, from 1")] = 1,
        safesearch: Annotated[
            Literal[0, 1, 2], Field(description="0 off, 1 moderate, 2 strict")
        ] = 0,
        limit: Annotated[int, Field(description="Results to return, 1-50")] = 10,
    ) -> Search:
        if not query.strip():
            raise InputError("searxng: query is empty")
        sec = cfg.section("searxng")
        try:
            data = await client(cfg).get(
                "/search",
                q=query.strip(),
                format="json",
                categories=",".join(categories) if categories else None,
                engines=",".join(engines) if engines else None,
                language=language or sec.get("language"),
                time_range=time_range,
                pageno=max(1, page),
                safesearch=safesearch,
            )
        except ApiError as exc:
            raise search_error(exc) from None
        results = [shape_result(r) for r in arr(data.get("results"))]
        out: dict[str, Any] = {"query": data.get("query", query), "page": max(1, page)}
        out.update(clip(results, clamp(limit, 1, 50)))
        extras: dict[str, list[Any]] = {
            "answers": [answer_text(a) for a in arr(data.get("answers"))],
            "infoboxes": [
                {
                    "name": i.get("infobox"),
                    "content": " ".join(str(i.get("content") or "").split())[:500],
                    "urls": [u.get("url") for u in arr(i.get("urls")) if u.get("url")][:5],
                }
                for i in arr(data.get("infoboxes"))
            ],
            "suggestions": arr(data.get("suggestions")),
            "corrections": arr(data.get("corrections")),
            "unresponsive_engines": [
                {"engine": e[0], "reason": e[1]} if isinstance(e, list | tuple) else {"engine": e}
                for e in arr(data.get("unresponsive_engines"))
            ],
        }
        out.update({k: v for k, v in extras.items() if v})
        return cast(Search, out)

    @tool(
        app,
        "A web page as Markdown (headings, links and tables kept; navigation and ads "
        "dropped), read in chunks: max_length characters from start_index, and "
        "next_index when more remains. raw=true returns the body unconverted. Text, "
        "JSON and XML come back as they are; binary types are refused.",
        title="Fetch web page",
        # A 50,000-character chunk plus its JSON envelope stays inline.
        meta={MAX_RESULT_CHARS: 70_000},
    )
    async def searxng_fetch(
        url: Annotated[str, Field(description="http or https URL")],
        max_length: Annotated[int, Field(description="Characters to return, 100-50000")] = 5000,
        start_index: Annotated[int, Field(description="Character offset to start at")] = 0,
        raw: Annotated[bool, Field(description="Return HTML unconverted")] = False,
    ) -> Fetched:
        target = check_url(url)
        sec = cfg.section("searxng")
        page = await fetch(
            target,
            max_bytes=int(sec.get("fetch_max_bytes", 5 * 2**20)),
            verify=bool(sec.get("verify_tls", True)),
            timeout=float(sec.get("timeout", 20.0)),
        )
        if page["status"] >= 400:
            raise ApiError(f"searxng_fetch: GET {page['final_url']} -> HTTP {page['status']}")
        # HTML extraction is CPU-bound; keep it off the event loop.
        text, title = await asyncio.to_thread(to_text, page, raw)
        start = max(0, start_index)
        chunk = text[start : start + clamp(max_length, 100, 50000)]
        end = start + len(chunk)
        out = {
            "url": target,
            "final_url": page["final_url"],
            "status": page["status"],
            "content_type": page["content_type"],
            "title": title,
            "total_chars": len(text),
            "start_index": start,
            "next_index": end if end < len(text) else None,
            "truncated_download": page["truncated"] or None,
            "content": chunk,
        }
        return cast(Fetched, {k: v for k, v in out.items() if v is not None})

    @tool(
        app,
        "The SearXNG instance: version, categories, enabled engines (with the categories "
        "they serve) and whether the limiter is on. Use it to pick categories or engines "
        "for searxng_search.",
        title="SearXNG engines",
    )
    async def searxng_instance(
        enabled_only: Annotated[bool, Field(description="Leave out disabled engines")] = True,
    ) -> Instance:
        data = await client(cfg).get("/config")
        engines: list[Engine] = [
            {
                "name": e.get("name"),
                "shortcut": e.get("shortcut"),
                "categories": e.get("categories"),
                "time_range": e.get("time_range_support"),
            }
            for e in arr(data.get("engines"))
            if e.get("enabled") or not enabled_only
        ]
        return {
            "version": data.get("version"),
            "instance_name": data.get("instance_name"),
            "limiter": data.get("limiter"),
            "safe_search": data.get("safe_search"),
            "categories": data.get("categories"),
            "engines": engines,
        }

    return app


def main() -> None:
    serve("searxng-mcp", __doc__, build, 8768)


if __name__ == "__main__":
    main()
