"""OpenGist server: push snippets to the homelab OpenGist as the user.

Separate from the read-only ``homelab`` server because it writes. The
credential is a personal access token with the ``gist`` scope at read/write
and no ``user`` scope; it can create, edit and delete the user's gists. The
code exposes one write, ``POST /gists`` (the only ``write_posts`` path), and
no edit or delete tool. Keep ``opengist_create_gist`` on "ask".
"""

from typing import Annotated, Any, Literal, Required, TypedDict, cast
from urllib.parse import quote

from mcp.server import MCPServer
from pydantic import Field

from .common import (
    WRITE,
    HttpClient,
    InputError,
    arr,
    clamp,
    client_from,
    clip,
    make_app,
    obj,
    pick,
    serve,
    tool,
)
from .config import Config

INSTRUCTIONS = """Pushes snippets to the homelab OpenGist as the user, so they can be
opened on the work machine. opengist_create_gist writes (keep it on ask);
opengist_get_gist and opengist_list_gists read. Default to unlisted gists
with a 7-day expiry; never put secrets in a gist."""

Visibility = Literal["public", "unlisted", "private"]
Expire = Literal["never", "1hour", "12hours", "1day", "7days", "15days"]


def client(cfg: Config) -> HttpClient:
    token = cfg.secret(cfg.section("opengist"), "token", "opengist")
    return client_from(
        cfg,
        "opengist",
        path="/api",
        headers={"Authorization": f"Bearer {token}"},
        write_posts={"/gists"},
    )


def raw_urls(gist: dict[str, Any]) -> dict[str, str]:
    base = str(gist.get("html_url", "")).rstrip("/")
    return {name: f"{base}/raw/HEAD/{quote(name)}" for name in obj(gist.get("files"))}


class Gist(TypedDict, total=False):
    """A gist; backend fields appear only when the gist has them."""

    id: Any
    title: Any
    description: Any
    visibility: Any
    html_url: Any
    expires_at: Any
    updated_at: Required[Any]
    raw_urls: Required[dict[str, str]]
    files: dict[str, dict[str, Any]]


class GistRow(TypedDict, total=False):
    id: Any
    title: Any
    visibility: Any
    html_url: Any
    updated_at: Any
    expires_at: Any


class GistList(TypedDict):
    count: int
    truncated: bool
    items: list[GistRow]


def summary_of(gist: dict[str, Any]) -> Gist:
    return cast(
        Gist,
        {
            **pick(gist, "id", "title", "description", "visibility", "html_url", "expires_at"),
            "updated_at": gist.get("updated_at"),
            "raw_urls": raw_urls(gist),
        },
    )


def build(cfg: Config) -> MCPServer[None]:
    app = make_app("opengist", INSTRUCTIONS)

    @tool(
        app,
        "Create a gist on the homelab OpenGist and return its page URL and one raw URL "
        "per file. files maps filename to content (the extension picks the syntax "
        "highlighting). visibility defaults to unlisted, expire to 7days.",
        title="Create gist",
        annotations=WRITE,
    )
    async def opengist_create_gist(
        files: Annotated[
            dict[str, str],
            Field(description="Filename to content; the extension picks the highlighting"),
        ],
        title: Annotated[str | None, Field(description="Gist title")] = None,
        description: Annotated[str | None, Field(description="One-line description")] = None,
        visibility: Annotated[
            Visibility, Field(description="unlisted (link only), private or public")
        ] = "unlisted",
        expire: Annotated[Expire, Field(description="How long the gist lives")] = "7days",
    ) -> Gist:
        body_files = {name: {"content": text} for name, text in files.items() if text}
        if not body_files:
            raise InputError("opengist: files needs at least one non-empty file")
        body: dict[str, Any] = {"visibility": visibility, "expire": expire, "files": body_files}
        if title:
            body["title"] = title
        if description:
            body["description"] = description
        return summary_of(await client(cfg).post_write("/gists", json=body))

    @tool(
        app,
        "One gist with its files and contents, by id (the 32-hex id in its URL).",
        title="Get gist",
    )
    async def opengist_get_gist(
        id: Annotated[str, Field(description="The 32-hex gist id from its URL")],
    ) -> Gist:
        gist = await client(cfg).get(f"/gists/{quote(id, safe='')}")
        out = summary_of(gist)
        out["files"] = {
            name: pick(f, "language", "size", "truncated", "content")
            for name, f in (obj(gist.get("files"))).items()
        }
        return out

    @tool(
        app,
        "The user's own gists (public, unlisted and private), newest first.",
        title="List my gists",
    )
    async def opengist_list_gists(
        limit: Annotated[int, Field(description="Gists to return, 1-100")] = 20,
    ) -> GistList:
        gists = await client(cfg).get("/gists", per_page=clamp(limit, 1, 100))
        rows = [
            pick(g, "id", "title", "visibility", "html_url", "updated_at", "expires_at")
            for g in arr(gists)
        ]
        return cast(GistList, clip(rows, limit))

    return app


def main() -> None:
    serve("opengist-mcp", __doc__, build, 8766)


if __name__ == "__main__":
    main()
