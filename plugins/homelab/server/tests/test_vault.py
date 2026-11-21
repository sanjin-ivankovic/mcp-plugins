"""Vault: AppRole login, metadata walk, seal state, and the drift scope `vault`."""

import json
import os
import stat
from pathlib import Path

import httpx
import pytest
from conftest import FULL, SECRETS
from mcp import Client

from homelab_mcp import server
from homelab_mcp.systems import drift, vault

pytestmark = pytest.mark.anyio

V = "https://vault.test:8200/v1"
TOKEN = "hvs.vault-client-token"


def vault_api(respx_mock, tree: dict[str, list[str]], deleted: tuple[str, ...] = ()):
    """Mock login, LIST on every folder in ``tree`` and metadata on every leaf."""
    login = respx_mock.post(f"{V}/auth/approle/login").respond(
        json={"auth": {"client_token": TOKEN, "lease_duration": 3600}}
    )

    def metadata(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Vault-Token"] == TOKEN
        path = request.url.path.removeprefix("/v1/kv/metadata/")
        if request.url.params.get("list") == "true":
            if path not in tree:
                return httpx.Response(404, json={"errors": []})
            return httpx.Response(200, json={"data": {"keys": tree[path]}})
        version = {"created_time": "t", "deletion_time": "t2" if path in deleted else ""}
        body = {"current_version": 2, "updated_time": "u", "versions": {"2": version}}
        return httpx.Response(200, json={"data": body})

    respx_mock.get(url__regex=rf"^{V}/kv/metadata/.*").mock(side_effect=metadata)
    return login


TREE = {
    "k8s/": ["app-a/", "app-b/"],
    "k8s/app-a/": ["app-a-secret", "nested/"],
    "k8s/app-a/nested/": ["deep"],
    "k8s/app-b/": ["app-b-secret"],
}


async def test_login_walk_metadata_and_token_redaction(full_cfg, respx_mock):
    login = vault_api(respx_mock, TREE, deleted=("k8s/app-b/app-b-secret",))
    async with Client(server.build(full_cfg)) as c:
        r = await c.call_tool("vault_kv_paths", {})
        r2 = await c.call_tool("vault_kv_paths", {"prefix": "k8s/app-a"})
    assert not r.is_error, r.content[0].text
    rows = r.structured_content["items"]
    assert [x["path"] for x in rows] == [
        "k8s/app-a/app-a-secret",
        "k8s/app-a/nested/deep",
        "k8s/app-b/app-b-secret",
    ]
    assert rows[2]["current_deleted"] and not rows[0]["current_deleted"]
    assert [x["path"] for x in r2.structured_content["items"]] == [
        "k8s/app-a/app-a-secret",
        "k8s/app-a/nested/deep",
    ]
    assert login.call_count == 1
    assert json.loads(login.calls[0].request.content) == {
        "role_id": "role-id-uuid",
        "secret_id": "vault-secret-id-value",
    }
    assert "vault-secret-id-value" not in json.dumps(r.structured_content)


async def test_login_failure_is_redacted(full_cfg, respx_mock):
    respx_mock.post(f"{V}/auth/approle/login").respond(
        400, json={"errors": ["invalid secret id vault-secret-id-value"]}
    )
    async with Client(server.build(full_cfg)) as c:
        r = await c.call_tool("vault_kv_paths", {})
    assert r.is_error and "HTTP 400" in r.content[0].text
    assert "vault-secret-id-value" not in r.content[0].text


async def test_seal_status_summary_needs_no_token(full_cfg, respx_mock):
    route = respx_mock.get(f"{V}/sys/seal-status").respond(
        json={"sealed": True, "initialized": True, "version": "1.21.0", "t": 3}
    )
    assert await vault.summary(full_cfg) == {"sealed": True, "version": "1.21.0"}
    assert "X-Vault-Token" not in route.calls[0].request.headers


def es(name, ns, keys=(), extract=(), store="vault"):
    return {
        "kind": "ExternalSecret",
        "metadata": {"name": name, "namespace": ns},
        "spec": {
            "secretStoreRef": {"kind": "ClusterSecretStore", "name": store},
            "data": [{"secretKey": "k", "remoteRef": {"key": k, "property": "p"}} for k in keys],
            "dataFrom": [{"extract": {"key": k}} for k in extract],
        },
    }


def test_vault_refs_collects_data_and_datafrom_of_the_vault_store_only():
    items = [
        es("a", "ns-a", keys=["k8s/app-a/app-a-secret", "k8s/shared/s"]),
        es("b", "ns-b", extract=["k8s/app-b/app-b-secret"], keys=["k8s/shared/s"]),
        es("c", "ns-c", keys=["k8s/other"], store="elsewhere"),
        {"kind": "ClusterSecretStore", "metadata": {"name": "vault"}},
    ]
    assert drift.vault_refs(items) == {
        "k8s/app-a/app-a-secret": ["ns-a/a"],
        "k8s/shared/s": ["ns-a/a", "ns-b/b"],
        "k8s/app-b/app-b-secret": ["ns-b/b"],
    }


def kubectl(tmp_path: Path, items: list) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    p = bin_dir / "kubectl"
    p.write_text(f"#!/bin/sh\ncat <<'EOF'\n{json.dumps({'items': items})}\nEOF\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return bin_dir


async def drift_cfg(make_cfg, tmp_path, monkeypatch, items):
    cfg = make_cfg(FULL + "\n[kubernetes]\nkubeconfig = 'kc'\n", SECRETS)
    (tmp_path / "kc").write_text("users:\n- user:\n    token: t\n")
    monkeypatch.setenv("PATH", f"{kubectl(tmp_path, items)}{os.pathsep}/usr/bin:/bin")
    return cfg


async def test_vault_scope_clean(make_cfg, tmp_path, monkeypatch, respx_mock):
    items = [
        es("a", "ns-a", keys=["k8s/app-a/app-a-secret", "k8s/app-a/nested/deep"]),
        es("b", "ns-b", extract=["k8s/app-b/app-b-secret"]),
    ]
    cfg = await drift_cfg(make_cfg, tmp_path, monkeypatch, items)
    vault_api(respx_mock, TREE)
    out = await drift.report(cfg, ["vault"])
    check = out["checks"][0]
    assert check["status"] == "clean", check
    assert check["summary"] == "all 3 referenced paths exist, none unreferenced under k8s/"


async def test_vault_scope_reports_missing_deleted_and_unreferenced(
    make_cfg, tmp_path, monkeypatch, respx_mock
):
    items = [
        es("a", "ns-a", keys=["k8s/app-a/app-a-secret", "k8s/app-z/typo"]),
        es("b", "ns-b", extract=["k8s/app-b/app-b-secret"]),
    ]
    cfg = await drift_cfg(make_cfg, tmp_path, monkeypatch, items)
    vault_api(respx_mock, TREE, deleted=("k8s/app-b/app-b-secret",))
    out = await drift.report(cfg, ["vault"])
    check = out["checks"][0]
    assert out["overall"] == "drift" and check["status"] == "drift"
    assert check["details"] == {
        "missing": [{"path": "k8s/app-z/typo", "read_by": ["ns-a/a"]}],
        "current_version_deleted": [{"path": "k8s/app-b/app-b-secret", "read_by": ["ns-b/b"]}],
        "unreferenced": ["k8s/app-a/nested/deep"],
    }


async def test_vault_scope_skipped_without_config(make_cfg):
    out = await drift.report(make_cfg("[pve]\nurl = 'x'\n"), ["vault"])
    assert out["checks"][0]["status"] == "skipped" and out["overall"] == "skipped"


async def test_every_vault_tool_is_read_only_and_posts_only_the_login(full_cfg):
    http = vault.anonymous(full_cfg)
    assert http.read_posts == frozenset({"/auth/approle/login"}) and not http.write_posts
    async with Client(server.build(full_cfg)) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert tools["vault_kv_paths"].annotations.read_only_hint is True


async def test_kv_paths_fetches_metadata_only_for_returned_rows(full_cfg, respx_mock):
    vault_api(respx_mock, TREE)
    async with Client(server.build(full_cfg)) as c:
        r = await c.call_tool("vault_kv_paths", {"limit": 1})
    assert r.structured_content["count"] == 3 and r.structured_content["truncated"]
    assert [x["path"] for x in r.structured_content["items"]] == ["k8s/app-a/app-a-secret"]
    leaf_reads = [
        call
        for call in respx_mock.calls
        if "/metadata/" in call.request.url.path and "list" not in call.request.url.params
    ]
    assert len(leaf_reads) == 1


async def test_vault_and_externalsecrets_scopes_share_one_kubectl_run(
    make_cfg, tmp_path, monkeypatch, respx_mock
):
    items = [es("a", "ns-a", keys=["k8s/app-a/app-a-secret"])]
    cfg = await drift_cfg(make_cfg, tmp_path, monkeypatch, items)
    runs = tmp_path / "kubectl-runs"
    kubectl_bin = tmp_path / "bin" / "kubectl"
    kubectl_bin.write_text(f"#!/bin/sh\necho run >> {runs}\n" + kubectl_bin.read_text()[10:])
    vault_api(respx_mock, TREE)
    out = await drift.report(cfg, ["vault", "externalsecrets"])
    assert {c["check"] for c in out["checks"]} == {"vault", "externalsecrets"}
    assert runs.read_text().count("run") == 1
