"""The registration contract every tool of the listed servers must meet.

Under Claude Code's tool search a tool is found by its name, title and
description, and called correctly only if each parameter says what it takes.
"""

from types import ModuleType

import pytest
from mcp import Client

from homelab_mcp import common, config, docmost, opengist, searxng, server, silverbullet

pytestmark = pytest.mark.anyio

SERVERS: dict[str, ModuleType] = {
    "docmost": docmost,
    "homelab": server,
    "opengist": opengist,
    "searxng": searxng,
    "silverbullet": silverbullet,
}
# The homelab server names each tool after the config section of its system
# (technitium_zones reads [technitium]); cross-system tools use homelab_.
PREFIXES: dict[str, set[str]] = {"homelab": {"homelab", *config.SECTIONS}}
ANNOTATIONS = (common.READ_ONLY, common.WRITE, common.DESTRUCTIVE)
# Claude Code cuts server instructions and upfront tool descriptions at 4,096
# characters; stay well inside so nothing that matters is cut.
MAX_INSTRUCTIONS = 2000
# Servers whose tools declare a result type, so tools/list carries a real
# outputSchema instead of an open object.
TYPED = {"docmost", "homelab", "opengist", "searxng", "silverbullet"}
MAX_DESCRIPTION = 1000


@pytest.mark.parametrize("name", sorted(SERVERS))
async def test_registration_contract(name, full_cfg):
    async with Client(SERVERS[name].build(full_cfg)) as c:
        instructions = c.instructions or ""
        tools = (await c.list_tools()).tools
    assert tools
    assert 0 < len(instructions) <= MAX_INSTRUCTIONS
    for t in tools:
        assert t.name.split("_")[0] in PREFIXES.get(name, {name}), t.name
        assert t.title, f"{t.name}: no title"
        assert t.description and len(t.description) <= MAX_DESCRIPTION, t.name
        assert t.annotations in ANNOTATIONS, f"{t.name}: annotations not a shared constant"
        for param, schema in t.input_schema.get("properties", {}).items():
            assert schema.get("description"), f"{t.name}({param}): no description"
        if name in TYPED:
            schema = t.output_schema or {}
            typed = (
                schema.get("properties")
                or schema.get("$ref")
                or isinstance(schema.get("additionalProperties"), dict)
            )
            assert typed, f"{t.name}: open outputSchema"
            assert "result" not in schema.get("properties", {}), f"{t.name}: wrapped result"
        limit = (t.meta or {}).get(common.MAX_RESULT_CHARS)
        assert limit is None or 50_000 < limit <= 500_000, t.name
        if (t.meta or {}).get(common.ALWAYS_LOAD):
            assert t.name == "homelab_overview", f"{t.name}: only the entry point loads upfront"


@pytest.mark.parametrize("name", sorted(SERVERS))
async def test_server_closes_its_http_pools_on_exit(name, full_cfg):
    async with Client(SERVERS[name].build(full_cfg)):
        common.transport(True)
        assert common._TRANSPORTS
    assert not common._TRANSPORTS


async def test_the_guard_catches_a_field_a_result_type_drops():
    from typing import TypedDict

    from mcp.server import MCPServer

    class Narrow(TypedDict):
        kept: int

    app = MCPServer("guard")

    @app.tool(name="narrow", description="returns a field its type does not declare")
    async def narrow() -> Narrow:
        return {"kept": 1, "dropped": 2}  # type: ignore[typeddict-unknown-key]

    async with Client(app) as c:
        with pytest.raises(AssertionError, match="structured content differs"):
            await c.call_tool("narrow", {})
