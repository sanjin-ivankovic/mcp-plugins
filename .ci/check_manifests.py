"""Fail when the plugin manifests break the install id or drift in version."""

import json
import re
import sys
import tomllib
from pathlib import Path

root = Path(__file__).resolve().parents[1]
market = json.loads((root / ".claude-plugin/marketplace.json").read_text())
plugin = json.loads((root / "plugins/homelab/.claude-plugin/plugin.json").read_text())
project = tomllib.loads((root / "plugins/homelab/server/pyproject.toml").read_text())["project"]

errors = []
if market["plugins"][0]["name"] != plugin["name"]:
    errors.append("marketplace entry name differs from plugin.json name")
if plugin["version"] != project["version"]:
    errors.append(f"plugin.json {plugin['version']} != pyproject {project['version']}")
for name, server in plugin["mcpServers"].items():
    if any("@latest" in str(a) for a in server.get("args", [])):
        errors.append(f"{name}: unpinned @latest")
init = (root / "plugins/homelab/server/src/homelab_mcp/__init__.py").read_text()
match = re.search(r'^__version__ = "([^"]+)"', init, re.M)
if not match or match.group(1) != plugin["version"]:
    found = match.group(1) if match else "missing"
    errors.append(f"plugin.json {plugin['version']} != __init__.__version__ {found}")
if errors:
    sys.exit("\n".join(errors))
print(f"manifests ok, version {plugin['version']}")
