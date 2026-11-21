"""Fail when a consumer skill names an MCP tool that does not exist.

Every backticked snake_case name in plugins/homelab/skills/*/SKILL.md must be
a tool in docs/TOOLS.md, a third-party tool in .ci/third_party_tools.txt, or
a known parameter or field name. Every homelab-server tool must also appear
in the homelab-ops skill, so no tool is unreachable from its routing table.
A renamed tool's old name (plugins/homelab/config/removed_tools.txt) fails
anywhere in a skill, bare or inside a full mcp__plugin_homelab_<server>__ name.
"""

import re
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
skills = sorted((root / "plugins/homelab/skills").glob("*/SKILL.md"))
tools_md = (root / "docs/TOOLS.md").read_text()
third_party = {
    line.strip()
    for line in (root / ".ci/third_party_tools.txt").read_text().splitlines()
    if line.strip() and not line.startswith("#")
}

renamed = dict(
    line.split()
    for line in (root / "plugins/homelab/config/removed_tools.txt").read_text().splitlines()
    if line.strip() and not line.startswith("#")
)

# Parameters and result fields the skills mention in backticks.
FIELDS = {
    "backup_id",
    "backup_type",
    "config_error",
    "down_only",
    "failed_only",
    "head_differs",
    "include_terraform",
    "min_priority",
    "next_index",
}

servers: dict[str, set[str]] = {}
current = ""
for line in tools_md.splitlines():
    if m := re.match(r"^## `([a-z]+)` server", line):
        current = m.group(1)
        servers[current] = set()
    elif m := re.match(r"^### `([a-z0-9_]+)`", line):
        servers[current].add(m.group(1))
ours = set().union(*servers.values())

NAME = re.compile(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`")
FULL = re.compile(r"mcp__plugin_homelab_([a-z]+)__([a-z0-9_]+)")
errors = []
for skill in skills:
    text = skill.read_text()
    rel = skill.relative_to(root)
    for name in sorted(set(NAME.findall(text))):
        if name not in ours | third_party | FIELDS:
            errors.append(f"{rel}: unknown tool `{name}`")
    for old, new in renamed.items():
        # "_" is a word character, so \b misses the old name after the "__" of a
        # full tool name; a single "_" before it is a longer name (homelab_drift_report).
        if re.search(rf"(?:^|[^a-z0-9_]|__){old}(?![a-z0-9_])", text, re.M):
            errors.append(f"{rel}: `{old}` was renamed to `{new}`")
    for server, name in sorted(set(FULL.findall(text))):
        known = servers.get(server)
        if known is not None and name not in known:
            errors.append(f"{rel}: {server} server has no tool {name}")

ops = (root / "plugins/homelab/skills/homelab-ops/SKILL.md").read_text()
for name in sorted(servers.get("homelab", set())):
    if f"`{name}`" not in ops:
        errors.append(f"homelab-ops does not route to homelab tool `{name}`")

if errors:
    sys.exit("\n".join(errors))
print(f"skill tools ok: {len(skills)} skills, {len(ours)} custom tools")
