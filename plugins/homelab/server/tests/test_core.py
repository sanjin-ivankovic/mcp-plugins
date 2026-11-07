"""Config, redaction, HTTP guard and the read-only contract of the whole server."""

import pytest
from mcp import Client

from homelab_mcp import config, redact, server
from homelab_mcp.common import ApiError, HttpClient

pytestmark = pytest.mark.anyio


def test_secret_from_file_and_command(make_cfg):
    cfg = make_cfg(
        '[a]\ntoken_file = "secrets/t"\n[b]\ntoken_command = ["echo", "from-cmd"]\n',
        {"t": "from-file"},
    )
    assert cfg.secret(cfg.section("a"), "token", "a") == "from-file"
    assert cfg.secret(cfg.section("b"), "token", "b") == "from-cmd"


def test_inline_secret_refused(make_cfg):
    cfg = make_cfg('[a]\ntoken = "plaintext"\n')
    with pytest.raises(config.ConfigError, match="inline 'token' is refused"):
        cfg.secret(cfg.section("a"), "token", "a")


def test_missing_config_does_not_raise_on_load(tmp_path):
    cfg = config.load(tmp_path / "nope.toml")
    assert cfg.load_error
    with pytest.raises(config.ConfigError):
        cfg.section("pve")


def test_redact_masks_registered_and_pattern_secrets():
    redact.register("s3cr3t-value")
    text = "failed with s3cr3t-value; Authorization: Bearer abc; PVEAPIToken=u@pve!t=xyz; ?token=q"
    out = redact.redact(text)
    for leaked in ("s3cr3t-value", "abc", "xyz", "=q"):
        assert leaked not in out


async def test_post_outside_allowlist_is_refused():
    http = HttpClient("t", "http://x.test", read_posts={"/read"})
    with pytest.raises(ApiError, match="not an allowlisted read endpoint"):
        await http.post_read("/write", json={})


async def test_every_tool_is_annotated_read_only(full_cfg):
    async with Client(server.build(full_cfg)) as c:
        tools = (await c.list_tools()).tools
    assert len(tools) >= 42
    for t in tools:
        assert t.annotations.read_only_hint is True, t.name
        assert t.annotations.destructive_hint is False, t.name


async def test_overview_isolates_failures(full_cfg, monkeypatch):
    async def boom(cfg):
        raise ApiError("pve: GET /nodes -> HTTP 500 token pve-secret-uuid")

    async def fine(cfg):
        return {"ok": True}

    monkeypatch.setitem(server.SUMMARIES, "pve", (boom, "pve"))
    for name in list(server.SUMMARIES):
        if name != "pve":
            monkeypatch.setitem(server.SUMMARIES, name, (fine, server.SUMMARIES[name][1]))
    full_cfg.secret(full_cfg.section("pve"), "token_secret", "pve")  # registers the secret
    out = await server.overview(full_cfg)
    assert out["errors"] == ["pve"]
    assert "pve-secret-uuid" not in out["systems"]["pve"]["error"]
    assert out["systems"]["pbs"] == {"ok": True}


async def test_tool_error_is_redacted(full_cfg, respx_mock):
    respx_mock.get("https://pve.test:8006/api2/json/nodes").respond(
        401, text="bad token pve-secret-uuid"
    )
    async with Client(server.build(full_cfg)) as c:
        r = await c.call_tool("pve_nodes", {})
    assert r.is_error
    assert "HTTP 401" in r.content[0].text
    assert "pve-secret-uuid" not in r.content[0].text
