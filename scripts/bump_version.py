#!/usr/bin/env python3
"""Bump the homelab plugin version everywhere it is recorded, then check it.

Usage: python3 scripts/bump_version.py patch|minor|major
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "plugins/homelab/server"
# file -> pattern whose group 1 is the version; each must match exactly once.
PLACES = {
    ROOT / "plugins/homelab/.claude-plugin/plugin.json": r'^  "version": "([0-9.]+)"',
    SERVER / "pyproject.toml": r'^version = "([0-9.]+)"',
    SERVER / "src/homelab_mcp/__init__.py": r'^__version__ = "([0-9.]+)"',
    SERVER / "uv.lock": r'^name = "homelab-mcp"\nversion = "([0-9.]+)"',
}


def bump(version: str, part: str) -> str:
    major, minor, patch = (int(x) for x in version.split("."))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("patch", "minor", "major"):
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    texts = {path: path.read_text() for path in PLACES}
    found = {
        re.findall(pat, texts[path], re.M)[0]
        for path, pat in PLACES.items()
        if len(re.findall(pat, texts[path], re.M)) == 1
    }
    if len(found) != 1:
        print(f"versions disagree or are missing: {sorted(found)}", file=sys.stderr)
        return 1
    old = found.pop()
    new = bump(old, sys.argv[1])
    for path, pat in PLACES.items():
        span = re.search(pat, texts[path], re.M).span(1)
        path.write_text(texts[path][: span[0]] + new + texts[path][span[1] :])
    print(f"{old} -> {new}", flush=True)
    return subprocess.run([sys.executable, str(ROOT / ".ci/check_manifests.py")]).returncode


if __name__ == "__main__":
    sys.exit(main())
