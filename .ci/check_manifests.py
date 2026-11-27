"""Fail when the plugin manifests break the install id, drift in version, or float a pin."""

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
# Third-party MCP servers run from third-party/package-lock.json through
# run-node-server.sh; npx would resolve their dependency tree at every launch.
scripts = sorted((root / "plugins/homelab/scripts").glob("*.sh"))
for name, server in plugin["mcpServers"].items():
    argv = [str(server.get("command", ""))] + [str(a) for a in server.get("args", [])]
    if "npx" in argv:
        errors.append(f"{name}: starts through npx; use run-node-server.sh")
for script in scripts:
    if re.search(r"^[^#]*\bnpx\b", script.read_text(), re.M):
        errors.append(f"{script.name}: calls npx; use run-node-server.sh")
third = root / "plugins/homelab/third-party"
npm_pkg = json.loads((third / "package.json").read_text())
npm_lock = json.loads((third / "package-lock.json").read_text())
deps = npm_pkg.get("dependencies", {})
for dep, version in deps.items():
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        errors.append(f"third-party/package.json: {dep} {version!r} is not an exact X.Y.Z")
    locked_version = npm_lock.get("packages", {}).get(f"node_modules/{dep}", {}).get("version")
    if locked_version != version:
        errors.append(f"third-party: {dep} is {version} in package.json, {locked_version} in the lock")
if npm_lock.get("packages", {}).get("", {}).get("dependencies") != deps:
    errors.append("third-party/package-lock.json does not match package.json; run npm install --package-lock-only")
init = (root / "plugins/homelab/server/src/homelab_mcp/__init__.py").read_text()
match = re.search(r'^__version__ = "([^"]+)"', init, re.M)
if not match or match.group(1) != plugin["version"]:
    found = match.group(1) if match else "missing"
    errors.append(f"plugin.json {plugin['version']} != __init__.__version__ {found}")
lock = tomllib.loads((root / "plugins/homelab/server/uv.lock").read_text())
locked = next((pkg["version"] for pkg in lock["package"] if pkg["name"] == project["name"]), None)
if locked != plugin["version"]:
    errors.append(f"plugin.json {plugin['version']} != uv.lock {project['name']} {locked}")
# The Python servers start through run-server.sh with a module name; a wrong
# name only shows when that server fails to start in a session.
src = root / "plugins/homelab/server/src/homelab_mcp"
for name, server in plugin["mcpServers"].items():
    args = [str(a) for a in server.get("args", [])]
    for i, arg in enumerate(args):
        if arg.endswith("/scripts/run-server.sh"):
            module = args[i + 1] if i + 1 < len(args) else ""
            if not (src / f"{module}.py").is_file():
                errors.append(f"{name}: run-server.sh module {module!r} has no {module}.py")
            # Claude Code exports CLAUDE_PLUGIN_DATA itself; an env entry would
            # replace it with the unsubstituted literal "${CLAUDE_PLUGIN_DATA}".
            if "CLAUDE_PLUGIN_DATA" in (server.get("env") or {}):
                errors.append(f"{name}: do not set CLAUDE_PLUGIN_DATA in env")
if errors:
    sys.exit("\n".join(errors))
print(f"manifests ok, version {plugin['version']}")
