"""Unknown sections and keys are reported, not silently ignored."""

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from mcp import Client

from homelab_mcp import config, server

pytestmark = pytest.mark.anyio

EXAMPLE = Path(__file__).resolve().parents[4] / "config.example.toml"


def test_a_typo_names_the_closest_key():
    data = {"pve": {"url": "https://pve.test", "verfy_tls": False, "token_secret_file": "s"}}
    assert config.check(data) == ["[pve]: unknown key 'verfy_tls', did you mean 'verify_tls'?"]


def test_an_inline_secret_points_at_its_file_form():
    (problem,) = config.check({"gitlab": {"url": "https://git.test", "token": "x"}})
    assert problem == "[gitlab]: unknown key 'token', did you mean 'token_file'?"


def test_unknown_sections_and_node_keys():
    data = {
        "pbx": {"url": "u"},
        "pihole": {"nodes": [{"name": "a", "url": "u", "credential_file": "p"}]},
    }
    assert config.check(data) == [
        "unknown section [pbx], did you mean [pbs]?",
        "[pihole] nodes[0]: unknown key 'credential_file', did you mean 'credentials_file'?",
    ]


def test_the_example_config_is_clean_and_documents_every_section():
    data = tomllib.loads(EXAMPLE.read_text())
    assert config.check(data) == []
    assert set(data) == set(config.SECTIONS)


async def test_overview_reports_the_warnings(make_cfg):
    cfg = make_cfg('[traefik]\nurl = "https://traefik.test"\ntimout = 5\n')
    async with Client(server.build(cfg)) as c:
        out = (await c.call_tool("homelab_overview", {})).structured_content
    assert out["config_warnings"] == ["[traefik]: unknown key 'timout', did you mean 'timeout'?"]


@pytest.mark.parametrize(
    ("body", "code", "printed"),
    [
        ('[traefik]\nurl = "https://traefik.test"\n', 0, ""),
        ('[traefik]\nurl = "https://traefik.test"\nverify = false\n', 1, "unknown key 'verify'"),
        ("[traefik\n", 1, "cannot parse"),
    ],
)
def test_check_config_exit_code(tmp_path, body, code, printed):
    path = tmp_path / "config.toml"
    path.write_text(body)
    run = subprocess.run(
        [sys.executable, "-m", "homelab_mcp.server", "--config", str(path), "--check-config"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == code, run.stderr
    assert printed in run.stdout
