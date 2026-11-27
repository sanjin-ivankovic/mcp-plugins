#!/usr/bin/env python3
"""Write a CHANGELOG.md section from the Conventional Commits of one release.

Usage:
  python3 scripts/changelog.py 0.18.0              prepend the section for HEAD
  python3 scripts/changelog.py 0.17.0 --from A --to B --print
                                                    one section for a commit range
  python3 scripts/changelog.py --part              the bump the commits since
                                                    the last release call for

Without --from, the range starts after the newest commit that changed the
version line of plugin.json before --to, the previous release. Merge commits
and the release commit itself are left out. --part looks only at commits that
change what installed copies run (SHIPPED) and prints major for a breaking
change (type!), minor for a feat, patch otherwise; it fails when there is
nothing to release.
"""

import argparse
import datetime
import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = ROOT / "CHANGELOG.md"
PLUGIN = "plugins/homelab/.claude-plugin/plugin.json"
HEADER = (
    "# Changelog\n\n"
    "Releases of the `homelab` plugin. `make release PART=patch|minor|major` adds\n"
    "each section; the CI `release` job publishes it as the GitLab release of the\n"
    "matching `vX.Y.Z` tag.\n"
)
CONVENTIONAL = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]+)\))?(?P<bang>!)?: (?P<text>.+)$")
# What installed copies run, as in .ci/release.py: the bump counts only these.
SHIPPED = [
    "plugins/homelab",
    ":(exclude)plugins/homelab/evals",
    ":(exclude)plugins/homelab/server/tests",
]
GROUPS = (
    ("Breaking changes", lambda c: c["bang"]),
    ("Features", lambda c: c["type"] == "feat"),
    ("Fixes", lambda c: c["type"] == "fix"),
    ("Dependencies", lambda c: c["type"] == "chore" and c["scope"] == "deps"),
    ("Other", lambda c: True),
)


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def previous_release(before: str) -> str | None:
    """The newest commit before ``before`` that changed plugin.json's version line."""
    out = git("log", "--no-merges", "--format=%H", "-G", r'"version": "', f"{before}~1", "--", PLUGIN)
    return out.splitlines()[0] if out else None


def commits_between(
    start: str | None, end: str, paths: list[str] | None = None
) -> list[dict[str, str | None]]:
    span = f"{start}..{end}" if start else end
    subjects = git(
        "log", "--no-merges", "--reverse", "--format=%s", span, *(["--", *paths] if paths else [])
    ).splitlines()
    commits: list[dict[str, str | None]] = []
    for subject in subjects:
        match = CONVENTIONAL.match(subject)
        if not match or (match["type"] == "chore" and match["scope"] == "release"):
            continue
        commits.append(match.groupdict())
    return commits


def part(commits: list[dict[str, str | None]]) -> str | None:
    if not commits:
        return None
    if any(c["bang"] for c in commits):
        return "major"
    return "minor" if any(c["type"] == "feat" for c in commits) else "patch"


def section(version: str, start: str | None, end: str) -> str:
    commits = commits_between(start, end)
    lines = [f"## {version} - {datetime.date.today().isoformat()}", ""]
    placed: set[int] = set()
    for title, wanted in GROUPS:
        picked = [i for i, c in enumerate(commits) if i not in placed and wanted(c)]
        if not picked:
            continue
        placed.update(picked)
        lines += [f"### {title}", ""]
        for i in picked:
            c = commits[i]
            scope = f"**{c['scope']}**: " if c["scope"] else ""
            # markdownlint (MD013) holds CHANGELOG.md to 80 columns.
            lines.append(
                textwrap.fill(
                    f"- {scope}{c['text']}",
                    width=80,
                    subsequent_indent="  ",
                    break_long_words=False,
                    break_on_hyphens=False,
                )
            )
        lines.append("")
    if not commits:
        lines += ["No changes.", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("version", nargs="?", help="default: the version in plugin.json")
    parser.add_argument("--from", dest="start", help="exclusive start commit")
    parser.add_argument("--to", dest="end", default="HEAD", help="inclusive end commit")
    parser.add_argument("--print", action="store_true", help="print, do not write CHANGELOG.md")
    parser.add_argument(
        "--part", action="store_true", help="print the bump the commits call for, then exit"
    )
    args = parser.parse_args()
    if args.part:
        # The last version change reachable from --to, itself included: right
        # after a release merge that is the release commit, so nothing is due.
        last = git("log", "--no-merges", "-1", "--format=%H", "-G", r'"version": "', args.end, "--", PLUGIN)
        bump = part(commits_between(last or None, args.end, SHIPPED))
        if bump is None:
            print("nothing to release since the last version change", file=sys.stderr)
            return 1
        print(bump)
        return 0
    args.version = args.version or json.loads((ROOT / PLUGIN).read_text())["version"]
    start = args.start or previous_release(args.end)
    text = section(args.version, start, args.end)
    if args.print:
        print(text)
        return 0
    body = CHANGELOG.read_text().removeprefix(HEADER).lstrip("\n") if CHANGELOG.exists() else ""
    if re.search(rf"^## {re.escape(args.version)} ", body, re.MULTILINE):
        print(f"CHANGELOG.md already has {args.version}", file=sys.stderr)
        return 1
    CHANGELOG.write_text(HEADER + "\n" + text + ("\n" + body if body else ""))
    print(f"CHANGELOG.md: added {args.version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
