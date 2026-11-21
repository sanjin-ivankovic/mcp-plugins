"""scripts/changelog.py: the bump make release takes, and the section's line width."""

import importlib.util
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parents[4] / "scripts/changelog.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("changelog", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def commit(
    type_: str, bang: str | None = None, text: str = "x", scope: str | None = None
) -> dict[str, str | None]:
    return {"type": type_, "scope": scope, "bang": bang, "text": text}


def test_part_follows_the_highest_conventional_commit():
    changelog = load()
    assert changelog.part([]) is None
    assert changelog.part([commit("fix"), commit("ci"), commit("chore")]) == "patch"
    assert changelog.part([commit("fix"), commit("feat")]) == "minor"
    assert changelog.part([commit("feat"), commit("fix", bang="!")]) == "major"


def test_section_lines_wrap_at_80_columns(monkeypatch):
    changelog = load()
    # 81 columns unwrapped: the 1.1.0 line that failed markdownlint.
    long = commit(
        "fix",
        scope="docmost",
        text="indent ordered-list content so get_page output imports back intact",
    )
    monkeypatch.setattr(changelog, "commits_between", lambda *_: [long])
    text = changelog.section("9.9.9", None, "HEAD")
    assert max(len(line) for line in text.splitlines()) <= 80
    assert (
        "- **docmost**: indent ordered-list content so get_page output imports back\n  intact"
        in text
    )


def test_bump_and_release_pending_agree_on_what_ships():
    spec = importlib.util.spec_from_file_location("release", SCRIPT.parents[1] / ".ci/release.py")
    assert spec and spec.loader
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    assert load().SHIPPED == release.SHIPPED
