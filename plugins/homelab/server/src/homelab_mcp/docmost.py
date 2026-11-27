"""Docmost server: read and write the homelab handbook as a Docmost member.

Separate from the read-only ``homelab`` server because it writes. Docmost
Community has no API keys (a paid feature), so the server logs in with the
email and password of a dedicated member (``POST /api/auth/login``), keeps the
returned JWT in memory and sends it as a Bearer token, logging in again once
when a call answers 401. The account must not use MFA.

Every Docmost endpoint is a POST, reads included, so each call goes through
``read_posts`` or ``write_posts``. The writes are page create, page update and
label add/remove; there is no delete or move tool. The account can edit every
page in the spaces it belongs to, so keep the write tools on "ask".

This is Docmost's internal API, not a documented contract: after a Docmost
version bump, run search, get and one update on a scratch page.
"""

import asyncio
import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Required, TypedDict, cast

import httpx
from mcp.server import MCPServer
from pydantic import Field

from . import redact
from .common import (
    DESTRUCTIVE,
    MAX_RESULT_CHARS,
    WRITE,
    ApiError,
    HttpClient,
    InputError,
    Session,
    arr,
    clamp,
    clip,
    gather_map,
    http_client,
    make_app,
    obj,
    pick,
    serve,
    tool,
)
from .config import Config, ConfigError

INSTRUCTIONS = """The homelab operator handbook in Docmost (one space, `handbook` by
default): hardware, network, hosts, cluster, applications, runbooks, decisions
and known issues. Search it before answering a question about the homelab's own
systems, open hits with docmost_get_page, and cite pages by their url.
docmost_create_page, docmost_update_page and docmost_set_labels write as the
Docmost member `claude` (keep them on ask): read a page before changing it,
change only what is wrong or missing, and never put a secret value in a page."""

READS = frozenset(
    {
        "/spaces",
        "/pages/sidebar-pages",
        "/pages/info",
        "/pages/breadcrumbs",
        "/pages/labels",
        "/pages/recent",
        "/pages/history",
        "/pages/history/info",
        "/pages/backlinks",
        "/search",
    }
)
WRITES = frozenset({"/pages/create", "/pages/update", "/pages/labels/add", "/pages/labels/remove"})

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")

Operation = Literal["replace", "append", "prepend"]
PAGE = "Page id, slug id or URL"


class PageRow(TypedDict, total=False):
    """A page; backend fields appear only when the page has them."""

    id: Any
    slugId: Any
    title: Any
    parentPageId: Any
    hasChildren: Any
    updatedAt: Any
    url: Required[str]
    children: list[PageRow]


class PageList(TypedDict):
    count: int
    truncated: bool
    items: list[PageRow]


class Tree(TypedDict):
    space: str
    items: list[PageRow]


class Hit(PageRow, total=False):
    space: Required[Any]
    snippet: Required[str]


class HitList(TypedDict):
    count: int
    truncated: bool
    items: list[Hit]


class Space(TypedDict, total=False):
    id: Any
    slug: Any
    name: Any
    description: Any


class SpaceList(TypedDict):
    count: int
    truncated: bool
    items: list[Space]


class Chunk(TypedDict):
    offset: int
    totalChars: int
    nextOffset: int | None
    markdown: str


class PageDoc(TypedDict, total=False):
    id: Any
    slugId: Any
    title: Any
    parentPageId: Any
    createdAt: Any
    updatedAt: Any
    url: Required[str]
    space: Required[Any]
    path: Required[list[Any]]
    labels: Required[list[Any]]
    lastUpdatedBy: Required[Any]
    outline: Required[list[dict[str, Any]]]
    content: Required[Chunk]


class Version(TypedDict, total=False):
    id: Any
    title: Any
    createdAt: Any
    lastUpdatedBy: Required[Any]


class VersionList(TypedDict):
    count: int
    truncated: bool
    items: list[Version]


class VersionText(Version, total=False):
    pageId: Any
    text: Required[str]


class Created(PageRow, total=False):
    labels: Required[list[str]]


class Updated(PageRow, total=False):
    operation: Required[str | None]


class Labels(TypedDict):
    id: str
    labels: list[Any]


SPACE = "Space slug; the configured space when absent"
Direction = Literal["incoming", "outgoing"]


class Docmost:
    def __init__(self, cfg: Config) -> None:
        sec = cfg.section("docmost")
        if not sec.get("url") or not sec.get("email"):
            raise ConfigError("docmost: url and email are required")
        self.url = str(sec["url"]).rstrip("/")
        self.email = str(sec["email"])
        self.space_slug = str(sec.get("space", "handbook"))
        self.verify = bool(sec.get("verify_tls", True))
        self._password = cfg.secret(sec, "password", "docmost")
        # One JWT and one slug->id map per Docmost URL for the life of the process.
        self.session = Session(cfg.cache, ("docmost-token", self.url), self.login)
        self.spaces: dict[str, str] = cfg.cache.setdefault(("docmost-spaces", self.url), {})

    async def login(self) -> str:
        client = http_client(verify=self.verify)
        try:
            resp = await client.post(
                self.url + "/api/auth/login",
                json={"email": self.email, "password": self._password},
            )
        except httpx.HTTPError as exc:
            raise ApiError(f"docmost: login failed: {type(exc).__name__}: {exc}") from None
        if resp.status_code >= 400:
            raise ApiError(
                f"docmost: login -> HTTP {resp.status_code}: {resp.text[:300]}",
                status=resp.status_code,
            )
        token = resp.cookies.get("authToken")
        if not token:
            raise ApiError("docmost: login returned no authToken cookie (MFA on the account?)")
        redact.register(token)
        return token

    def client(self, token: str) -> HttpClient:
        return HttpClient(
            "docmost",
            self.url + "/api",
            headers={"Authorization": f"Bearer {token}"},
            verify=self.verify,
            read_posts=READS,
            write_posts=WRITES,
        )

    async def call(self, path: str, body: dict[str, Any], *, write: bool = False) -> Any:
        async def send(token: str) -> Any:
            http = self.client(token)
            if write:
                return await http.post_write(path, json=body)
            return await http.post_read(path, json=body)

        res: Any = await self.session.call(send)
        return obj(res).get("data", res) if isinstance(res, dict) else res

    async def space_id(self, slug: str | None = None) -> str:
        slug = slug or self.space_slug
        known = self.spaces
        if slug not in known:
            data = await self.call("/spaces", {"limit": 100})
            known.update({s["slug"]: s["id"] for s in data.get("items", [])})
        if slug not in known:
            raise InputError(f"docmost: no space with slug '{slug}' visible to {self.email}")
        return known[slug]

    def page_url(self, page: dict[str, Any], space_slug: str | None = None) -> str:
        space = space_slug or (obj(page.get("space"))).get("slug") or self.space_slug
        slug = re.sub(r"[^a-z0-9]+", "-", str(page.get("title") or "untitled").lower()).strip("-")
        return f"{self.url}/s/{space}/p/{slug[:70] or 'untitled'}-{page.get('slugId')}"


def page_ref(page: str) -> str:
    """A page id, slug id, or Docmost page URL -> what /pages/* accepts as pageId."""
    page = page.strip()
    if "/p/" in page:
        page = page.rstrip("/").rsplit("/p/", 1)[1].split("?")[0].split("#")[0]
        if not UUID.match(page):
            page = page.rsplit("-", 1)[-1]
    if not page:
        raise InputError("docmost: page is empty")
    return page


# Spans a mention's label must not be linked inside: links, images, inline code.
PROTECTED = re.compile(r"!?\[[^\]]*\]\([^)]*\)|`[^`]*`")


def mentions(node: Any) -> list[tuple[str, str, str]]:
    """(label, slugId, text before it in its block) of every page mention, in order."""
    out: list[tuple[str, str, str]] = []

    def walk(node: Any, before: list[str]) -> None:
        if not isinstance(node, dict):
            return
        n = obj(node)
        attrs = obj(n.get("attrs"))
        if n.get("type") == "text":
            before.append(str(n.get("text", "")))
        elif n.get("type") == "mention":
            if attrs.get("entityType") == "page":
                out.append(
                    (str(attrs.get("label") or ""), str(attrs.get("slugId") or ""), "".join(before))
                )
            before.append(str(attrs.get("label") or ""))
        children = arr(n.get("content"))
        # A block holding inline nodes (paragraph, heading, cell) starts its own context.
        inline = any(obj(c).get("type") in ("text", "mention") for c in children)
        context: list[str] = [] if inline else before
        for child in children:
            walk(child, context)

    walk(node, [])
    return out


def _plain(text: str) -> str:
    """Markdown with link targets, emphasis and code marks removed, spaces collapsed."""
    text = re.sub(r"\]\([^)]*\)", "", text)
    return " ".join(re.sub(r"[*_`\[\]]", "", text).split())


def link_mentions(markdown: str, found: list[tuple[str, str, str]], space: str) -> str:
    """Docmost's Markdown export prints a page mention as its bare label, so an
    edit sent back with replace would drop the link. Turn each mention back into
    a link: the occurrence of its label whose preceding text matches the text
    before the mention in the stored document, in document order."""
    pos = 0
    for label, slug_id, before in found:
        if not label or not slug_id:
            continue
        tail = _plain(before)[-24:]
        pattern = re.compile(rf"(?<![\w/]){re.escape(label)}(?![\w])")
        for m in pattern.finditer(markdown, pos):
            if any(p.start() <= m.start() < p.end() for p in PROTECTED.finditer(markdown)):
                continue
            if tail and not _plain(markdown[max(0, m.start() - 200) : m.start()]).endswith(tail):
                continue
            slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:70] or "untitled"
            link = f"[{label}](/s/{space}/p/{slug}-{slug_id})"
            markdown = markdown[: m.start()] + link + markdown[m.end() :]
            pos = m.start() + len(link)
            break
    return markdown


ORDERED = re.compile(r"^( *)(\d{1,9}[.)] )")


@dataclass
class _Item:
    """An open ordered list item: its marker's indent and width, and the spaces
    its continuation lines gain (unknown until the first one)."""

    lead: int
    width: int
    extra: int | None = None


def indent_ordered_items(markdown: str) -> str:
    """Docmost's Markdown export indents everything under an ordered list item
    (a code block, a second paragraph, a nested list) by two spaces, but its
    importer, like CommonMark, needs the width of the marker (three for "1. ").
    Sent back unchanged with replace, such a page loses its list: the block
    becomes top-level and the following items turn into "2\\." paragraphs.
    Shift each item's continuation lines up to the marker width; an item
    already indented that far is left alone."""
    out: list[str] = []
    open_items: list[_Item] = []  # outermost first
    fenced = False
    for line in markdown.split("\n"):
        if not line.strip():
            out.append("")
            continue
        lead = len(line) - len(line.lstrip(" "))
        if not fenced:
            while open_items and lead <= open_items[-1].lead:
                open_items.pop()
            if open_items and open_items[-1].extra is None:
                item = open_items[-1]
                item.extra = max(0, item.width - (lead - item.lead))
        shift = sum(item.extra or 0 for item in open_items)
        out.append(" " * shift + line)
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced and (m := ORDERED.match(line)):
            open_items.append(_Item(lead, len(m.group(2))))
    return "\n".join(out)


def anchor(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def outline(markdown: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    fenced = False
    for line in markdown.splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        m = None if fenced else HEADING.match(line)
        if m:
            title = m.group(2)
            out.append({"level": len(m.group(1)), "title": title, "anchor": anchor(title)})
    return out


def section_of(markdown: str, wanted: str) -> str | None:
    """The lines under the heading whose text or anchor is ``wanted``, up to the next peer."""
    lines, fenced, start, level = markdown.splitlines(), False, None, 0
    for i, line in enumerate(lines):
        if FENCE.match(line):
            fenced = not fenced
            continue
        m = None if fenced else HEADING.match(line)
        if not m:
            continue
        if start is not None and len(m.group(1)) <= level:
            return "\n".join(lines[start:i]).strip("\n")
        if start is None and wanted.strip().lower() in (m.group(2).lower(), anchor(m.group(2))):
            start, level = i, len(m.group(1))
    return "\n".join(lines[start:]).strip("\n") if start is not None else None


def prosemirror_text(node: Any) -> str:
    """Plain text of a stored Docmost document (history entries carry editor JSON)."""
    if not isinstance(node, dict):
        return ""
    node = obj(node)
    kind, children = node.get("type"), arr(node.get("content"))
    if kind == "text":
        return str(node.get("text", ""))
    if kind == "hardBreak":
        return "\n"
    inner = "".join(prosemirror_text(c) for c in children)
    if kind == "heading":
        return "#" * int((obj(node.get("attrs"))).get("level", 1)) + " " + inner + "\n\n"
    if kind == "codeBlock":
        lang = (obj(node.get("attrs"))).get("language") or ""
        return f"```{lang}\n{inner}\n```\n\n"
    if kind == "listItem":
        return "- " + inner.strip() + "\n"
    if kind in ("paragraph", "blockquote", "tableRow"):
        return inner.rstrip() + "\n\n" if kind != "tableRow" else inner + "\n"
    if kind in ("tableCell", "tableHeader"):
        return inner.strip() + " | "
    return inner


def build(cfg: Config) -> MCPServer[None]:
    app = make_app("docmost", INSTRUCTIONS)

    def dm() -> Docmost:
        return Docmost(cfg)

    def row(d: Docmost, page: dict[str, Any], space_slug: str | None = None) -> PageRow:
        return cast(
            PageRow,
            {
                **pick(page, "id", "slugId", "title", "parentPageId", "hasChildren", "updatedAt"),
                "url": d.page_url(page, space_slug),
            },
        )

    @tool(
        app, "The Docmost spaces the member can see, with id, slug and name.", title="List spaces"
    )
    async def docmost_list_spaces() -> SpaceList:
        data = await dm().call("/spaces", {"limit": 100})
        rows = [pick(s, "id", "slug", "name", "description") for s in data.get("items", [])]
        return cast(SpaceList, clip(rows, 100))

    @tool(
        app,
        "What a part of the handbook holds: the child pages of `page` (id, slug id or URL), "
        "or the root pages of the space when `page` is absent, expanded `depth` levels "
        "(1-3). Use it to find where a page belongs; use docmost_search_pages to find "
        "something by what it says.",
        title="Browse handbook tree",
    )
    async def docmost_browse_tree(
        page: Annotated[
            str | None, Field(description=PAGE + "; the space root when absent")
        ] = None,
        depth: Annotated[int, Field(description="Levels to expand, 1-3")] = 1,
        space: Annotated[str | None, Field(description=SPACE)] = None,
    ) -> Tree:
        d = dm()
        slug = space or d.space_slug
        space_id = await d.space_id(slug)
        depth = clamp(depth, 1, 3)

        # Subtrees load in parallel; one semaphore bounds the requests in flight
        # across every level, so depth 3 cannot fan out to hundreds at once.
        sem = asyncio.Semaphore(6)

        async def level(parent: str | None, left: int) -> list[PageRow]:
            body: dict[str, Any] = {"spaceId": space_id, "limit": 100}
            if parent:
                body["pageId"] = parent
            async with sem:
                pages = (await d.call("/pages/sidebar-pages", body)).get("items", [])
            items = [row(d, p, slug) for p in pages]
            expand = [
                (item, p["id"])
                for item, p in zip(items, pages, strict=True)
                if p.get("hasChildren") and left > 1
            ]
            subtrees = await asyncio.gather(*(level(pid, left - 1) for _, pid in expand))
            for (item, _), children in zip(expand, subtrees, strict=True):
                item["children"] = children
            return items

        return {"space": slug, "items": await level(page_ref(page) if page else None, depth)}

    @tool(
        app,
        "Full-text search over the handbook pages, ranked, each hit with a highlighted "
        "snippet. Search every space unless `space` (a slug) narrows it. Open promising "
        "hits with docmost_get_page instead of answering from snippets.",
        title="Search handbook",
    )
    async def docmost_search_pages(
        query: Annotated[str, Field(description="Words to search for")],
        limit: Annotated[int, Field(description="Hits to return, 1-50")] = 10,
        space: Annotated[
            str | None, Field(description="Space slug; every space when absent")
        ] = None,
        title_only: Annotated[bool, Field(description="Match page titles only")] = False,
    ) -> HitList:
        if not query.strip():
            raise InputError("docmost: query is empty")
        d = dm()
        body: dict[str, Any] = {"query": query, "limit": clamp(limit, 1, 50)}
        if space:
            body["spaceId"] = await d.space_id(space)
        if title_only:
            body["titleOnly"] = True
        hits = (await d.call("/search", body)).get("items", [])
        rows = [
            {
                **row(d, h),
                "space": (obj(h.get("space"))).get("slug"),
                "snippet": re.sub(r"</?b>", "**", str(h.get("highlight") or "")),
            }
            for h in hits
        ]
        return cast(HitList, clip(rows, limit))

    @tool(
        app,
        "One handbook page as Markdown, with its title, URL, breadcrumb path, labels, "
        "last update and outline of headings. Name it by id, slug id or URL. For a long "
        "page read the outline first and ask for one `section` (heading text or anchor), "
        "or page through with `offset` and `length`.",
        title="Read handbook page",
        # length goes up to 100,000 characters; keep that inline with its envelope.
        meta={MAX_RESULT_CHARS: 130_000},
    )
    async def docmost_get_page(
        page: Annotated[str, Field(description=PAGE)],
        section: Annotated[
            str | None, Field(description="Heading text or anchor; only that section")
        ] = None,
        offset: Annotated[int, Field(description="Character offset into the body")] = 0,
        length: Annotated[int, Field(description="Characters to return, 100-100000")] = 20000,
    ) -> PageDoc:
        d = dm()
        ref = page_ref(page)
        body = {"pageId": ref, "format": "markdown", "includeSpace": True}
        info = await d.call("/pages/info", body)
        markdown = str(info.get("content") or "")
        # The stored editor JSON (for mention links), breadcrumbs and labels.
        stored, crumbs, labels = await gather_map(
            ("/pages/info", "/pages/breadcrumbs", "/pages/labels"),
            lambda path: d.call(path, {"pageId": info["id"]}),
        )
        space = (obj(info.get("space"))).get("slug") or d.space_slug
        markdown = link_mentions(markdown, mentions(stored.get("content")), space)
        markdown = indent_ordered_items(markdown)
        out = {
            **pick(info, "id", "slugId", "title", "parentPageId", "createdAt", "updatedAt"),
            "url": d.page_url(info),
            "space": (obj(info.get("space"))).get("slug"),
            "path": [c.get("title") for c in arr(crumbs)],
            "labels": [lb.get("name") for lb in (obj(labels)).get("items", [])],
            "lastUpdatedBy": ((obj(info.get("lastUpdatedBy"))).get("name")),
            "outline": outline(markdown),
        }
        if section is not None:
            part = section_of(markdown, section)
            if part is None:
                raise InputError(f"docmost: no heading '{section}' on this page")
            markdown = part
        offset, length = max(0, offset), clamp(length, 100, 100000)
        chunk = markdown[offset : offset + length]
        out["content"] = {
            "offset": offset,
            "totalChars": len(markdown),
            "nextOffset": offset + len(chunk) if offset + len(chunk) < len(markdown) else None,
            "markdown": chunk,
        }
        return cast(PageDoc, out)

    @tool(app, "Recently updated handbook pages, newest first.", title="Recent handbook changes")
    async def docmost_recent_changes(
        limit: Annotated[int, Field(description="Pages to return, 1-100")] = 20,
        space: Annotated[str | None, Field(description=SPACE)] = None,
    ) -> PageList:
        d = dm()
        slug = space or d.space_slug
        body = {"spaceId": await d.space_id(slug), "limit": clamp(limit, 1, 100)}
        items = (await d.call("/pages/recent", body)).get("items", [])
        return cast(PageList, clip([row(d, p, slug) for p in items], limit))

    @tool(
        app,
        "Saved versions of a page, newest first. Docmost saves a version a few minutes "
        "after an edit settles, so a fresh change may not be listed yet.",
        title="List page versions",
    )
    async def docmost_list_versions(
        page: Annotated[str, Field(description=PAGE)],
        limit: Annotated[int, Field(description="Versions to return, 1-100")] = 20,
    ) -> VersionList:
        d = dm()
        body = {"pageId": page_ref(page), "limit": clamp(limit, 1, 100)}
        items = (await d.call("/pages/history", body)).get("items", [])
        rows = [
            {
                **pick(h, "id", "title", "createdAt"),
                "lastUpdatedBy": (obj(h.get("lastUpdatedBy"))).get("name"),
            }
            for h in items
        ]
        return cast(VersionList, clip(rows, limit))

    @tool(
        app,
        "One saved version of a page (id from docmost_list_versions), as plain text.",
        title="Read page version",
    )
    async def docmost_get_version(
        version_id: Annotated[str, Field(description="Version id from docmost_list_versions")],
    ) -> VersionText:
        h = await dm().call("/pages/history/info", {"historyId": version_id.strip()})
        return cast(
            VersionText,
            {
                **pick(h, "id", "pageId", "title", "createdAt"),
                "lastUpdatedBy": (obj(h.get("lastUpdatedBy"))).get("name"),
                "text": prosemirror_text(h.get("content")).strip(),
            },
        )

    @tool(
        app,
        "Pages that link to this page (incoming) or that it links to (outgoing).",
        title="Page links",
    )
    async def docmost_get_backlinks(
        page: Annotated[str, Field(description=PAGE)],
        direction: Annotated[
            Direction, Field(description="incoming: pages linking here; outgoing: its links")
        ] = "incoming",
        limit: Annotated[int, Field(description="Pages to return, 1-100")] = 50,
    ) -> PageList:
        d = dm()
        body = {"pageId": page_ref(page), "direction": direction, "limit": clamp(limit, 1, 100)}
        items = (await d.call("/pages/backlinks", body)).get("items", [])
        return cast(PageList, clip([row(d, p) for p in items], limit))

    @tool(
        app,
        "Create a handbook page from Markdown, under `parent` (id, slug id or URL) or at "
        "the space root, with optional labels. The title is the page title; do not repeat "
        "it as a heading in the Markdown. Returns the new page's id and URL.",
        title="Create handbook page",
        annotations=WRITE,
    )
    async def docmost_create_page(
        title: Annotated[str, Field(description="Page title")],
        markdown: Annotated[str, Field(description="Page body; no title heading")],
        parent: Annotated[
            str | None, Field(description="Parent page id, slug id or URL; space root when absent")
        ] = None,
        labels: Annotated[list[str] | None, Field(description="Labels to set")] = None,
        space: Annotated[str | None, Field(description=SPACE)] = None,
    ) -> Created:
        if not title.strip():
            raise InputError("docmost: title is empty")
        d = dm()
        slug = space or d.space_slug
        body: dict[str, Any] = {
            "spaceId": await d.space_id(slug),
            "title": title.strip(),
            "content": markdown,
            "format": "markdown",
        }
        if parent:
            parent_info = await d.call("/pages/info", {"pageId": page_ref(parent)})
            body["parentPageId"] = parent_info["id"]
        page = await d.call("/pages/create", body, write=True)
        names = [n.strip() for n in arr(labels) if n.strip()]
        if names:
            await d.call("/pages/labels/add", {"pageId": page["id"], "names": names}, write=True)
        return cast(Created, {**row(d, page, slug), "labels": names})

    @tool(
        app,
        "Change a handbook page: its Markdown body (`operation` replace, append or "
        "prepend) and/or its title. Read the page with docmost_get_page first; for a "
        "small fix send the whole corrected body with replace. Docmost keeps the old "
        "body in the page history.",
        title="Update handbook page",
        annotations=DESTRUCTIVE,
    )
    async def docmost_update_page(
        page: Annotated[str, Field(description=PAGE)],
        markdown: Annotated[str | None, Field(description="New body content")] = None,
        operation: Annotated[
            Operation, Field(description="replace the body, or append or prepend to it")
        ] = "replace",
        title: Annotated[str | None, Field(description="New page title")] = None,
    ) -> Updated:
        if markdown is None and not (title and title.strip()):
            raise InputError("docmost: give markdown, title, or both")
        d = dm()
        body: dict[str, Any] = {"pageId": page_ref(page)}
        if markdown is not None:
            body.update({"content": markdown, "format": "markdown", "operation": operation})
        if title and title.strip():
            body["title"] = title.strip()
        updated = await d.call("/pages/update", body, write=True)
        return cast(
            Updated, {**row(d, updated), "operation": operation if markdown is not None else None}
        )

    @tool(
        app,
        "Add and/or remove labels (tags) on a handbook page by name.",
        title="Set page labels",
        annotations=DESTRUCTIVE,
    )
    async def docmost_set_labels(
        page: Annotated[str, Field(description=PAGE)],
        add: Annotated[list[str] | None, Field(description="Label names to add")] = None,
        remove: Annotated[list[str] | None, Field(description="Label names to remove")] = None,
    ) -> Labels:
        d = dm()
        info = await d.call("/pages/info", {"pageId": page_ref(page)})
        pid = info["id"]
        names = [n.strip() for n in arr(add) if n.strip()]
        if names:
            await d.call("/pages/labels/add", {"pageId": pid, "names": names}, write=True)
        if remove:
            current = (await d.call("/pages/labels", {"pageId": pid})).get("items", [])
            ids = {lb["name"]: lb["id"] for lb in current}
            for name in remove:
                if name.strip() in ids:
                    body = {"pageId": pid, "labelId": ids[name.strip()]}
                    await d.call("/pages/labels/remove", body, write=True)
        after = (await d.call("/pages/labels", {"pageId": pid})).get("items", [])
        return {"id": pid, "labels": [lb.get("name") for lb in after]}

    return app


def main() -> None:
    serve("docmost-mcp", __doc__, build, 8767)


if __name__ == "__main__":
    main()
