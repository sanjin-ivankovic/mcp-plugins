"""Fail when a consumer skill's frontmatter or line width breaks the repo standard.

Every plugin skill loads in every homelab project: its name and description
are what Claude matches a request against, so both must be present and the
name must match the directory. Skill bodies keep to 80 columns outside code
blocks and tables (markdownlint does not cover plugins/*/skills/).
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS = sorted((ROOT / "plugins/homelab/skills").glob("*/SKILL.md"))
MAX_DESCRIPTION = 1024
WIDTH = 80

errors: list[str] = []
for path in SKILLS:
    rel = path.relative_to(ROOT)
    text = path.read_text()
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not match:
        errors.append(f"{rel}: no frontmatter")
        continue
    front, body = match.group(1), match.group(2)
    fields = dict(re.findall(r"^([a-z-]+):\s*(.*)$", front, re.M))
    if fields.get("name") != path.parent.name:
        errors.append(f"{rel}: name {fields.get('name')!r} differs from its directory")
    description = fields.get("description", "")
    if not description:
        errors.append(f"{rel}: no description")
    elif len(description) > MAX_DESCRIPTION:
        errors.append(f"{rel}: description is {len(description)} characters (max {MAX_DESCRIPTION})")
    fenced = False
    offset = text[: match.start(2)].count("\n")
    for number, line in enumerate(body.splitlines(), start=offset + 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced or line.lstrip().startswith("|"):
            continue
        if len(line) > WIDTH:
            errors.append(f"{rel}:{number}: {len(line)} columns (max {WIDTH})")

if errors:
    sys.exit("\n".join(errors))
print(f"skills ok: {len(SKILLS)} skills")
