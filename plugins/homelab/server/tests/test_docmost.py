"""The docmost server: session login, POST allowlists, Markdown in and out, redaction."""

import json

import httpx
import pytest
from mcp import Client

from homelab_mcp import docmost
from homelab_mcp.common import ApiError

pytestmark = pytest.mark.anyio

API = "https://docmost.test/api"
JWT = "eyJ.docmost-jwt-value.sig"
SPACE = {"id": "0190a000-0000-7000-8000-00000000000a", "slug": "handbook", "name": "Handbook"}
PAGE_ID = "0190a000-0000-7000-8000-0000000000b1"
PAGE = {
    "id": PAGE_ID,
    "slugId": "AbC123xyZ9",
    "title": "Kubernetes known issues",
    "parentPageId": None,
    "updatedAt": "2026-11-13T09:00:00Z",
    "space": {"id": SPACE["id"], "slug": "handbook"},
    "lastUpdatedBy": {"name": "Claude"},
    "content": "Intro.\n\n## Longhorn\n\nA trap.\n\n### Detail\n\nMore.\n\n## Traefik\n\nOther.",
}
WRAP = {"success": True, "status": 200}


def ok(data):
    return {"data": data, **WRAP}


def login_route(respx_mock):
    return respx_mock.post(f"{API}/auth/login").respond(
        200, json=WRAP, headers={"set-cookie": f"authToken={JWT}; Path=/; HttpOnly"}
    )


async def call(cfg, name, args=None):
    async with Client(docmost.build(cfg)) as c:
        return await c.call_tool(name, args or {})


async def test_login_once_then_bearer(full_cfg, respx_mock):
    login = login_route(respx_mock)
    spaces = respx_mock.post(f"{API}/spaces").respond(json=ok({"items": [SPACE]}))
    await call(full_cfg, "docmost_list_spaces")
    r = await call(full_cfg, "docmost_list_spaces")
    assert r.structured_content["items"][0]["slug"] == "handbook"
    assert login.call_count == 1
    assert json.loads(login.calls[0].request.content) == {
        "email": "claude@docmost.test",
        "password": "docmost-password-value",
    }
    assert spaces.calls[1].request.headers["Authorization"] == f"Bearer {JWT}"


async def test_relogin_once_on_401(full_cfg, respx_mock):
    login = login_route(respx_mock)
    respx_mock.post(f"{API}/spaces").mock(
        side_effect=[
            httpx.Response(401, json={"message": "Unauthorized"}),
            httpx.Response(200, json=ok({"items": [SPACE]})),
        ]
    )
    full_cfg.cache[("docmost-token", "https://docmost.test")] = "expired"
    r = await call(full_cfg, "docmost_list_spaces")
    assert not r.is_error, r.content[0].text
    assert login.call_count == 1


async def test_login_failure_is_redacted(full_cfg, respx_mock):
    respx_mock.post(f"{API}/auth/login").respond(
        401, text="bad credentials for docmost-password-value"
    )
    r = await call(full_cfg, "docmost_list_spaces")
    assert r.is_error
    assert "docmost-password-value" not in r.content[0].text
    assert "401" in r.content[0].text


async def test_login_without_cookie_is_an_error(full_cfg, respx_mock):
    respx_mock.post(f"{API}/auth/login").respond(200, json=WRAP)
    r = await call(full_cfg, "docmost_list_spaces")
    assert r.is_error and "authToken" in r.content[0].text


async def test_get_page_markdown_outline_and_section(full_cfg, respx_mock):
    login_route(respx_mock)
    info = respx_mock.post(f"{API}/pages/info").respond(json=ok(PAGE))
    respx_mock.post(f"{API}/pages/breadcrumbs").respond(
        json=ok([{"title": "Known issues"}, {"title": PAGE["title"]}])
    )
    respx_mock.post(f"{API}/pages/labels").respond(json=ok({"items": [{"name": "k8s"}]}))
    url = "https://docmost.test/s/handbook/p/kubernetes-known-issues-AbC123xyZ9"
    r = await call(full_cfg, "docmost_get_page", {"page": url, "section": "longhorn"})
    assert not r.is_error, r.content[0].text
    body = json.loads(info.calls[0].request.content)
    assert body["pageId"] == "AbC123xyZ9" and body["format"] == "markdown"
    out = r.structured_content
    assert out["url"] == "https://docmost.test/s/handbook/p/kubernetes-known-issues-AbC123xyZ9"
    assert out["path"] == ["Known issues", "Kubernetes known issues"]
    assert out["labels"] == ["k8s"]
    assert [h["anchor"] for h in out["outline"]] == ["longhorn", "detail", "traefik"]
    assert out["content"]["markdown"] == "## Longhorn\n\nA trap.\n\n### Detail\n\nMore."


async def test_search_turns_highlight_into_markdown(full_cfg, respx_mock):
    login_route(respx_mock)
    route = respx_mock.post(f"{API}/search").respond(
        json=ok({"items": [{**PAGE, "highlight": "a <b>Longhorn</b> trap"}]})
    )
    r = await call(full_cfg, "docmost_search_pages", {"query": "longhorn", "limit": 5})
    assert json.loads(route.calls[0].request.content) == {"query": "longhorn", "limit": 5}
    assert r.structured_content["items"][0]["snippet"] == "a **Longhorn** trap"


async def test_create_page_resolves_space_and_adds_labels(full_cfg, respx_mock):
    login_route(respx_mock)
    respx_mock.post(f"{API}/spaces").respond(json=ok({"items": [SPACE]}))
    create = respx_mock.post(f"{API}/pages/create").respond(json=ok(PAGE))
    labels = respx_mock.post(f"{API}/pages/labels/add").respond(json=ok([]))
    r = await call(
        full_cfg,
        "docmost_create_page",
        {"title": "New", "markdown": "Body **x**", "labels": ["k8s", " "]},
    )
    assert not r.is_error, r.content[0].text
    assert json.loads(create.calls[0].request.content) == {
        "spaceId": SPACE["id"],
        "title": "New",
        "content": "Body **x**",
        "format": "markdown",
    }
    assert json.loads(labels.calls[0].request.content) == {"pageId": PAGE_ID, "names": ["k8s"]}


async def test_update_sends_markdown_with_operation(full_cfg, respx_mock):
    login_route(respx_mock)
    route = respx_mock.post(f"{API}/pages/update").respond(json=ok(PAGE))
    r = await call(
        full_cfg,
        "docmost_update_page",
        {"page": PAGE_ID, "markdown": "Fixed.", "operation": "append"},
    )
    assert not r.is_error, r.content[0].text
    assert json.loads(route.calls[0].request.content) == {
        "pageId": PAGE_ID,
        "content": "Fixed.",
        "format": "markdown",
        "operation": "append",
    }


async def test_update_needs_something_to_change(full_cfg, respx_mock):
    r = await call(full_cfg, "docmost_update_page", {"page": PAGE_ID})
    assert r.is_error
    assert not respx_mock.calls


async def test_set_labels_removes_by_id(full_cfg, respx_mock):
    login_route(respx_mock)
    respx_mock.post(f"{API}/pages/info").respond(json=ok(PAGE))
    respx_mock.post(f"{API}/pages/labels").respond(
        json=ok({"items": [{"id": "lbl-1", "name": "old"}]})
    )
    remove = respx_mock.post(f"{API}/pages/labels/remove").respond(json=ok({}))
    r = await call(full_cfg, "docmost_set_labels", {"page": PAGE_ID, "remove": ["old", "absent"]})
    assert not r.is_error, r.content[0].text
    assert remove.call_count == 1
    assert json.loads(remove.calls[0].request.content) == {"pageId": PAGE_ID, "labelId": "lbl-1"}


async def test_get_version_flattens_editor_json(full_cfg, respx_mock):
    login_route(respx_mock)
    doc = {
        "type": "doc",
        "content": [
            {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "T"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "Body"}]},
        ],
    }
    respx_mock.post(f"{API}/pages/history/info").respond(
        json=ok({"id": "h1", "pageId": PAGE_ID, "title": "x", "content": doc})
    )
    r = await call(full_cfg, "docmost_get_version", {"version_id": "h1"})
    assert r.structured_content["text"] == "## T\n\nBody"


async def test_annotations_mark_exactly_the_writes(full_cfg):
    async with Client(docmost.build(full_cfg)) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    writes = {"docmost_create_page", "docmost_update_page", "docmost_set_labels"}
    # update can replace a body and set_labels can remove one; create only adds.
    destructive = {"docmost_update_page", "docmost_set_labels"}
    assert writes < set(tools)
    for name, t in tools.items():
        assert t.annotations.read_only_hint is (name not in writes), name
        assert t.annotations.destructive_hint is (name in destructive), name


async def test_paths_outside_the_allowlists_are_refused(full_cfg):
    http = docmost.Docmost(full_cfg).client("t")
    with pytest.raises(ApiError, match="not an allowlisted write endpoint"):
        await http.post_write("/pages/delete", json={})
    with pytest.raises(ApiError, match="not an allowlisted read endpoint"):
        await http.post_read("/pages/create", json={})


def test_page_ref_accepts_ids_slugs_and_urls():
    assert docmost.page_ref(PAGE_ID) == PAGE_ID
    assert docmost.page_ref("AbC123xyZ9") == "AbC123xyZ9"
    assert docmost.page_ref("https://d.test/s/handbook/p/a-b-c-AbC123xyZ9#x") == "AbC123xyZ9"


def test_link_mentions_uses_the_text_before_each_mention():
    md = (
        "Owns: vendors, history and decisions.\n\n"
        "See `history` and [history](/x).\n\n"
        "- **Decisions and history**: records\n\n"
        "Then a history row, and History again."
    )
    found = [
        ("history", "AAA", "Decisions and "),
        ("history", "AAA", "Then a "),
        ("History", "BBB", "Then a history row, and "),
    ]
    assert docmost.link_mentions(md, found, "handbook") == (
        "Owns: vendors, history and decisions.\n\n"
        "See `history` and [history](/x).\n\n"
        "- **Decisions and [history](/s/handbook/p/history-AAA)**: records\n\n"
        "Then a [history](/s/handbook/p/history-AAA) row, and "
        "[History](/s/handbook/p/history-BBB) again."
    )


def test_link_mentions_skips_partial_words_and_empty_targets():
    md = "histories and history"
    out = docmost.link_mentions(md, [("history", "AAA", ""), ("", "X", ""), ("y", "", "")], "h")
    assert out == "histories and [history](/s/h/p/history-AAA)"


def test_mentions_carries_the_text_before_it_in_its_block():
    doc = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Other block"}]},
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [
                                    {"type": "text", "text": "Decisions and "},
                                    {
                                        "type": "mention",
                                        "attrs": {
                                            "label": "history",
                                            "slugId": "AAA",
                                            "entityType": "page",
                                        },
                                    },
                                ],
                            }
                        ],
                    }
                ],
            },
        ],
    }
    assert docmost.mentions(doc) == [("history", "AAA", "Decisions and ")]


async def test_get_page_turns_mentions_back_into_links(full_cfg, respx_mock):
    login_route(respx_mock)
    doc = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "See "},
                    {
                        "type": "mention",
                        "attrs": {"label": "ntfy", "slugId": "Nt1", "entityType": "page"},
                    },
                ],
            }
        ],
    }
    respx_mock.post(f"{API}/pages/info").mock(
        side_effect=[
            httpx.Response(200, json=ok({**PAGE, "content": "See ntfy"})),
            httpx.Response(200, json=ok({**PAGE, "content": doc})),
        ]
    )
    respx_mock.post(f"{API}/pages/breadcrumbs").respond(json=ok([]))
    respx_mock.post(f"{API}/pages/labels").respond(json=ok({"items": []}))
    r = await call(full_cfg, "docmost_get_page", {"page": PAGE_ID})
    assert not r.is_error, r.content[0].text
    assert r.structured_content["content"]["markdown"] == "See [ntfy](/s/handbook/p/ntfy-Nt1)"


async def test_browse_tree_expands_children(full_cfg, respx_mock):
    login_route(respx_mock)
    respx_mock.post(f"{API}/spaces").respond(json=ok({"items": [SPACE]}))

    def sidebar(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "pageId" not in body:
            items = [{"id": "p1", "slugId": "s1", "title": "Cluster", "hasChildren": True}]
        else:
            items = [{"id": "p2", "slugId": "s2", "title": "Talos", "hasChildren": False}]
        return httpx.Response(200, json=ok({"items": items}))

    route = respx_mock.post(f"{API}/pages/sidebar-pages").mock(side_effect=sidebar)
    r = await call(full_cfg, "docmost_browse_tree", {"depth": 2})
    items = r.structured_content["items"]
    assert items[0]["title"] == "Cluster" and items[0]["children"][0]["title"] == "Talos"
    assert items[0]["url"].endswith("/s/handbook/p/cluster-s1")
    assert route.call_count == 2


async def test_recent_versions_and_backlinks(full_cfg, respx_mock):
    login_route(respx_mock)
    respx_mock.post(f"{API}/spaces").respond(json=ok({"items": [SPACE]}))
    respx_mock.post(f"{API}/pages/recent").respond(
        json=ok({"items": [{"id": "p1", "slugId": "s1", "title": "Recent"}]})
    )
    history = respx_mock.post(f"{API}/pages/history").respond(
        json=ok({"items": [{"id": "h1", "title": "Old", "lastUpdatedBy": {"name": "Claude"}}]})
    )
    backlinks = respx_mock.post(f"{API}/pages/backlinks").respond(
        json=ok({"items": [{"id": "p3", "slugId": "s3", "title": "Linker"}]})
    )
    recent = await call(full_cfg, "docmost_recent_changes", {"limit": 500})
    assert recent.structured_content["items"][0]["title"] == "Recent"
    versions = await call(full_cfg, "docmost_list_versions", {"page": PAGE_ID})
    assert versions.structured_content["items"][0]["lastUpdatedBy"] == "Claude"
    assert json.loads(history.calls[0].request.content)["pageId"] == PAGE_ID
    links = await call(
        full_cfg,
        "docmost_get_backlinks",
        {"page": f"https://docmost.test/s/handbook/p/x-{PAGE['slugId']}", "direction": "outgoing"},
    )
    assert links.structured_content["items"][0]["title"] == "Linker"
    body = json.loads(backlinks.calls[0].request.content)
    assert body == {"pageId": PAGE["slugId"], "direction": "outgoing", "limit": 50}


# Docmost's own export of a step with a command under it: two-space continuation,
# and blank lines that hold the two spaces.
EXPORTED_STEPS = "\n".join(
    [
        "1. **Run** the check:",
        "  ",
        "  ```bash",
        "  scratch-check --all \\",
        "    --verbose",
        "  ```",
        "  ",
        "  Expect `ok`.",
        "2. Read the result.",
        "3. Done.",
    ]
)

IMPORTABLE_STEPS = """1. **Run** the check:

   ```bash
   scratch-check --all \\
     --verbose
   ```

   Expect `ok`.
2. Read the result.
3. Done."""


def test_ordered_item_content_moves_to_the_marker_width():
    assert docmost.indent_ordered_items(EXPORTED_STEPS) == IMPORTABLE_STEPS


def test_already_importable_markdown_is_unchanged():
    assert docmost.indent_ordered_items(IMPORTABLE_STEPS) == IMPORTABLE_STEPS


def test_nested_and_wide_markers_and_untouched_blocks():
    exported = "1. outer\n  1. inner\n    detail\n2. next\n\n10. ten\n  body\n\n- bullet\n  text"
    assert docmost.indent_ordered_items(exported) == (
        "1. outer\n   1. inner\n      detail\n2. next\n\n10. ten\n    body\n\n- bullet\n  text"
    )
    fenced = "```text\n1. not a list\n  still code\n```\nafter"
    assert docmost.indent_ordered_items(fenced) == fenced


async def test_get_page_returns_markdown_the_importer_reads_back(full_cfg, respx_mock):
    login_route(respx_mock)
    respx_mock.post(f"{API}/pages/info").respond(json=ok({**PAGE, "content": EXPORTED_STEPS}))
    respx_mock.post(f"{API}/pages/breadcrumbs").respond(json=ok([]))
    respx_mock.post(f"{API}/pages/labels").respond(json=ok({"items": []}))
    r = await call(full_cfg, "docmost_get_page", {"page": PAGE_ID})
    assert not r.is_error, r.content[0].text
    assert r.structured_content["content"]["markdown"] == IMPORTABLE_STEPS
