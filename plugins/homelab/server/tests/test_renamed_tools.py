"""scripts/renamed-tools.sh: the SessionStart list of files that still name a renamed tool."""

import subprocess
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]
SCRIPT = PLUGIN / "scripts/renamed-tools.sh"


def scan(project: Path, home: Path) -> str:
    done = subprocess.run(
        ["/bin/sh", str(SCRIPT), str(project)],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


def test_lists_old_names_bare_and_in_full_tool_names(tmp_path: Path):
    skill = tmp_path / "proj/.claude/skills/with space/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nallowed-tools: mcp__plugin_homelab_homelab__drift_report\n---\nRun `dns_zones`.\n"
    )
    settings = tmp_path / "home/.claude/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"allow": ["mcp__plugin_homelab_homelab__alerts_active"]}\n')

    out = scan(tmp_path / "proj", tmp_path / "home")

    assert ".claude/skills/with space/SKILL.md:2: drift_report is now homelab_drift_report" in out
    assert ".claude/skills/with space/SKILL.md:4: dns_zones is now technitium_zones" in out
    assert "~/.claude/settings.json:1: alerts_active is now alertmanager_alerts" in out


def test_new_names_and_worktrees_are_not_reported(tmp_path: Path):
    rules = tmp_path / "proj/.claude/rules/ops.md"
    rules.parent.mkdir(parents=True)
    rules.write_text(
        "Use `homelab_drift_report` and mcp__plugin_homelab_homelab__homelab_drift_render_check.\n"
    )
    copy = tmp_path / "proj/.claude/worktrees/old/CLAUDE.md"
    copy.parent.mkdir(parents=True)
    copy.write_text("drift_report\n")
    (tmp_path / "home").mkdir()

    assert scan(tmp_path / "proj", tmp_path / "home") == ""


def test_every_old_name_maps_to_a_new_one():
    lines = (PLUGIN / "config/removed_tools.txt").read_text().splitlines()
    pairs = [line.split() for line in lines if line.strip() and not line.startswith("#")]
    assert pairs and all(len(pair) == 2 for pair in pairs)
