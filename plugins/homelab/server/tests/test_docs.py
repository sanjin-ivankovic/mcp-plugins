"""docs/TOOLS.md and the eval mocks' _tools.json are generated; fail when stale."""

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.anyio

GEN = Path(__file__).resolve().parents[1] / "tools" / "gen_tools_doc.py"


def load_generator():
    spec = importlib.util.spec_from_file_location("gen_tools_doc", GEN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_tools_md_is_fresh():
    gen = load_generator()
    text, _ = await gen.render()
    assert gen.OUT.read_text() == text, (
        "docs/TOOLS.md is stale: run `uv run python tools/gen_tools_doc.py`"
    )


async def test_eval_mock_tool_lists_are_fresh():
    gen = load_generator()
    for path, listing in (await gen.tool_lists()).items():
        assert path.is_file() and path.read_text() == listing, (
            f"{path} is stale: run `uv run python tools/gen_tools_doc.py`"
        )
