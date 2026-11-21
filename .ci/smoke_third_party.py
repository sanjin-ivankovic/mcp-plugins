"""Start the third-party MCP servers the way plugin.json does and check their tools.

Fails when a tool named in .ci/third_party_tools.txt no longer exists on its
server (the consumer skills would name a missing tool), and lists every tool a
server does not annotate readOnlyHint=true. Needs the live credentials and
backends, so it runs locally (make smoke-live), not in CI. gitlab is GitLab's
own HTTP endpoint behind OAuth and is not started.

Usage: uv run --project plugins/homelab/server python .ci/smoke_third_party.py
"""

import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/homelab"
SERVERS = ("argocd", "kubernetes", "longhorn", "grafana")


def expected() -> dict[str, set[str]]:
    """Tool names per server, from the '# <server> (...)' sections."""
    out: dict[str, set[str]] = {}
    section = ""
    for line in (ROOT / ".ci/third_party_tools.txt").read_text().splitlines():
        header = re.match(r"#\s*([a-z]+)\s*\(", line)
        if header:
            section = header.group(1)
        elif line.strip() and not line.startswith("#") and section:
            out.setdefault(section, set()).add(line.strip())
    return out


def resolve(value: str, options: dict[str, str]) -> str:
    value = value.replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN))
    return re.sub(r"\$\{user_config\.(\w+)\}", lambda m: options[m.group(1)], value)


async def tools_of(entry: dict, options: dict[str, str], data: str) -> list:
    env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(PLUGIN), "CLAUDE_PLUGIN_DATA": data}
    env.update({k: resolve(v, options) for k, v in entry.get("env", {}).items()})
    params = StdioServerParameters(
        command=resolve(entry["command"], options),
        args=[resolve(a, options) for a in entry.get("args", [])],
        env=env,
    )
    async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
        await session.initialize()
        return (await session.list_tools()).tools


async def main() -> int:
    plugin = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text())
    options = {k: str(v.get("default", "")) for k, v in plugin["userConfig"].items()}
    options = {k: os.path.expanduser(v) if v.startswith("~/") else v for k, v in options.items()}
    want = expected()
    data = os.environ.get("CLAUDE_PLUGIN_DATA") or tempfile.mkdtemp(prefix="homelab-smoke-")
    failed = False
    for name in SERVERS:
        try:
            tools = await asyncio.wait_for(tools_of(plugin["mcpServers"][name], options, data), 300)
        except Exception as exc:  # noqa: BLE001 - report every server, then fail
            print(f"{name}: did not start: {type(exc).__name__}: {exc}")
            failed = True
            continue
        names = {t.name for t in tools}
        missing = sorted(want.get(name, set()) - names)
        # Some servers publish no annotations at all; their read-only guard is a
        # flag (MCP_READ_ONLY, --read-only), so only an explicit false is listed.
        bare = sum(1 for t in tools if t.annotations is None)
        writes = sorted(
            t.name for t in tools if t.annotations and t.annotations.read_only_hint is False
        )
        print(f"{name}: {len(tools)} tools, {len(want.get(name, ()))} named in skills")
        if missing:
            failed = True
            print(f"  missing (named in third_party_tools.txt): {', '.join(missing)}")
        if bare:
            print(f"  {bare} tools carry no annotations")
        if writes:
            print(f"  readOnlyHint false: {', '.join(writes)}")
    print("gitlab: not started (HTTP endpoint behind OAuth)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
