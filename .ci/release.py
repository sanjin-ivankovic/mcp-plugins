"""Publish plugin releases and flag changes that never got one.

release   On a main push: when plugin.json's version has no GitLab release yet,
          create release vX.Y.Z at this commit (GitLab creates the tag) with
          that version's CHANGELOG.md section as notes. Idempotent.
pending   On the schedule: fail when a commit that changes what installed
          copies run has waited on main more than MAX_AGE_DAYS without a
          version bump (a merged Renovate update reaches no one until then).

Both use CI_JOB_TOKEN (the Releases API accepts it) and the standard library
only. --dry-run prints the API call instead of making it.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = "plugins/homelab/.claude-plugin/plugin.json"
# What installed copies run; a change elsewhere (CI, docs, evals, tests) needs no
# release. scripts/changelog.py keeps the same list for make release's bump.
SHIPPED = [
    "plugins/homelab",
    ":(exclude)plugins/homelab/evals",
    ":(exclude)plugins/homelab/server/tests",
]
MAX_AGE_DAYS = 7


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def version() -> str:
    return json.loads((ROOT / PLUGIN).read_text())["version"]


def release_commit() -> str:
    """The commit that set the current version: the tag points there, not at a
    later merge that carries changes this version does not have."""
    return git("log", "-1", "--format=%H", "-G", r'"version": "', "--", PLUGIN)


def notes(ver: str) -> str:
    text = (ROOT / "CHANGELOG.md").read_text()
    match = re.search(rf"^## {re.escape(ver)} .*?\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    if not match:
        sys.exit(f"CHANGELOG.md has no section for {ver}; run make release")
    return match.group(1).strip()


def api(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    url = f"{os.environ['CI_API_V4_URL']}/projects/{os.environ['CI_PROJECT_ID']}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("JOB-TOKEN", os.environ["CI_JOB_TOKEN"])
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def release(dry_run: bool) -> int:
    ver = version()
    tag = f"v{ver}"
    body = {
        "name": tag,
        "tag_name": tag,
        "ref": release_commit(),
        "description": notes(ver),
    }
    if dry_run:
        print(f"would create release {tag} at {body['ref']}:\n{body['description']}")
        return 0
    status, _ = api("GET", f"/releases/{urllib.parse.quote(tag, safe='')}")
    if status == 200:
        print(f"{tag} is already released")
        return 0
    status, created = api("POST", "/releases", body)
    if status != 201:
        print(f"creating {tag} failed: HTTP {status}", file=sys.stderr)
        return 1
    print(f"released {tag}: {created.get('_links', {}).get('self', '')}")
    return 0


def pending() -> int:
    """Commits on SHIPPED paths since the last version bump, oldest first."""
    out = git(
        "log", "--no-merges", "--reverse", "--format=%ct %h %s", f"{release_commit()}..HEAD", "--", *SHIPPED
    )
    rows = [line.split(" ", 2) for line in out.splitlines() if line]
    if not rows:
        print(f"nothing unreleased since {version()}")
        return 0
    age_days = (time.time() - int(rows[0][0])) / 86400
    print(f"{len(rows)} commit(s) on main since {version()} that installed copies do not have:")
    for _, sha, subject in rows:
        print(f"  {sha} {subject}")
    if age_days > MAX_AGE_DAYS:
        print(f"the oldest has waited {age_days:.0f} days: cut a release (make release)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "release":
        sys.exit(release("--dry-run" in sys.argv))
    if mode == "pending":
        sys.exit(pending())
    sys.exit(__doc__)
