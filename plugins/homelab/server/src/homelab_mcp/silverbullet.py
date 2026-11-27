"""SilverBullet server: write scratch pages the user reads on the work machine.

Separate from the read-only ``homelab`` server because it writes. The
credential is an API token of a dedicated non-admin SilverBullet account with
the write role on one space; it can read, write and delete that space's
files. The code writes only Markdown pages with ``PUT /.fs/<page>.md`` (the
only ``write_put_prefixes`` path) and has no delete tool. A new page is
created only if it does not exist yet, an overwrite is explicit, and an
append is a conditional write on the page's ETag. Keep the write tools on
"ask".
"""

from typing import Annotated, Any, Literal, TypedDict, cast
from urllib.parse import quote

from mcp.server import MCPServer
from pydantic import Field

from .common import (
    DESTRUCTIVE,
    WRITE,
    ApiError,
    HttpClient,
    InputError,
    arr,
    clamp,
    client_from,
    clip,
    make_app,
    serve,
    tool,
)
from .config import Config

INSTRUCTIONS = """Writes scratch pages to the homelab SilverBullet space the user keeps
open on the work machine; a page written here shows up there within seconds.
Use it for terminal commands and PR text the user will copy at work.
silverbullet_write_page and silverbullet_append_page write (keep them on ask);
silverbullet_read_page and silverbullet_list_pages read. Put commands in
fenced code blocks so they get a copy button. Never put secrets in a page."""

PAGE_NAME = "Page name: folders separated by /, no .md"
FS = "/.fs/"
SYNC = {"X-Sync-Mode": "true"}


def client(cfg: Config) -> HttpClient:
    token = cfg.secret(cfg.section("silverbullet"), "token", "silverbullet")
    return client_from(
        cfg,
        "silverbullet",
        headers={"Authorization": f"Bearer {token}", **SYNC},
        write_put_prefixes=(FS,),
    )


def page_name(page: str) -> str:
    """A space-relative page name without the .md suffix, refused if it escapes the space."""
    name = page.strip().strip("/")
    if name.endswith(".md"):
        name = name[:-3]
    parts = name.split("/")
    if not name or any(not p or p.startswith(".") for p in parts):
        raise InputError(f"silverbullet: invalid page name {page!r}")
    return name


def fs_path(name: str) -> str:
    return FS + quote(f"{name}.md")


def page_url(cfg: Config, name: str) -> str:
    base = str(cfg.section("silverbullet").get("url", "")).rstrip("/")
    return f"{base}/{quote(name)}"


class PageWrite(TypedDict):
    page: str
    url: str
    action: Literal["created", "overwritten", "appended"]
    etag: str | None


class Page(TypedDict):
    page: str
    url: str
    last_modified: str | None
    content: str


class PageRow(TypedDict):
    page: str
    last_modified: Any


class PageList(TypedDict):
    count: int
    truncated: bool
    items: list[PageRow]


def result(
    cfg: Config, name: str, resp: Any, action: Literal["created", "overwritten", "appended"]
) -> PageWrite:
    return {
        "page": name,
        "url": page_url(cfg, name),
        "action": action,
        "etag": resp.headers.get("etag"),
    }


def build(cfg: Config) -> MCPServer[None]:
    app = make_app("silverbullet", INSTRUCTIONS)

    @tool(
        app,
        "Write a Markdown page to the SilverBullet space and return its URL. page is the "
        "page name (folders with /, no .md needed). Refuses to replace an existing page "
        "unless overwrite is true.",
        title="Write scratch page",
        annotations=DESTRUCTIVE,
    )
    async def silverbullet_write_page(
        page: Annotated[str, Field(description=PAGE_NAME)],
        content: Annotated[str, Field(description="The whole page as Markdown")],
        overwrite: Annotated[
            bool, Field(description="Replace the page if it exists; it is not kept")
        ] = False,
    ) -> PageWrite:
        name = page_name(page)
        if not content.strip():
            raise InputError("silverbullet: content is empty")
        headers = {} if overwrite else {"If-None-Match": "*"}
        try:
            resp = await client(cfg).put_write(
                fs_path(name), content=content.encode(), headers=headers
            )
        except ApiError as exc:
            if exc.status == 412:
                raise InputError(
                    f"silverbullet: page {name!r} exists; pass overwrite=true or append"
                ) from None
            raise
        return result(cfg, name, resp, "overwritten" if overwrite else "created")

    @tool(
        app,
        "Append Markdown to the end of a SilverBullet page (created if missing) and return "
        "its URL. The write is conditional on the page not having changed since it was read.",
        title="Append to scratch page",
        annotations=WRITE,
    )
    async def silverbullet_append_page(
        page: Annotated[str, Field(description=PAGE_NAME)],
        content: Annotated[str, Field(description="Markdown to add at the end")],
    ) -> PageWrite:
        name = page_name(page)
        if not content.strip():
            raise InputError("silverbullet: content is empty")
        http = client(cfg)
        try:
            current = await http.get_response(fs_path(name))
        except ApiError as exc:
            if exc.status != 404:
                raise
            resp = await http.put_write(
                fs_path(name), content=content.encode(), headers={"If-None-Match": "*"}
            )
            return result(cfg, name, resp, "created")
        etag = current.headers.get("etag")
        if not etag:
            raise ApiError(f"silverbullet: GET {fs_path(name)} returned no ETag")
        body = current.text.rstrip("\n") + "\n\n" + content
        try:
            resp = await http.put_write(
                fs_path(name), content=body.encode(), headers={"If-Match": etag}
            )
        except ApiError as exc:
            if exc.status == 412:
                raise InputError(
                    f"silverbullet: page {name!r} changed while appending; try again"
                ) from None
            raise
        return result(cfg, name, resp, "appended")

    @tool(app, "One SilverBullet page as Markdown, by page name.", title="Read scratch page")
    async def silverbullet_read_page(
        page: Annotated[str, Field(description=PAGE_NAME)],
    ) -> Page:
        name = page_name(page)
        resp = await client(cfg).get_response(fs_path(name))
        return {
            "page": name,
            "url": page_url(cfg, name),
            "last_modified": resp.headers.get("x-last-modified"),
            "content": resp.text,
        }

    @tool(
        app,
        "Pages in the SilverBullet space, most recently modified first.",
        title="List scratch pages",
    )
    async def silverbullet_list_pages(
        limit: Annotated[int, Field(description="Pages to return, 1-500")] = 30,
    ) -> PageList:
        files = await client(cfg).get("/.fs")
        pages: list[PageRow] = [
            {"page": str(f["name"])[:-3], "last_modified": f.get("lastModified")}
            for f in arr(files)
            if str(f.get("name", "")).endswith(".md")
        ]
        pages.sort(key=lambda p: p["last_modified"] or 0, reverse=True)
        return cast(PageList, clip(pages, clamp(limit, 1, 500)))

    return app


def main() -> None:
    serve("silverbullet-mcp", __doc__, build, 8769)


if __name__ == "__main__":
    main()
