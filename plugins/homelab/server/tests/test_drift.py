"""Drift wrappers: classification of exit codes and output, env hygiene, ArgoCD scope."""

import json
import os
import stat
import sys
from pathlib import Path

import pytest
from conftest import FULL, SECRETS
from mcp import Client

from homelab_mcp import server
from homelab_mcp.systems import drift

pytestmark = pytest.mark.anyio


def script(tmp_path: Path, name: str, body: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    p = bin_dir / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return bin_dir


async def classify(tmp_path, body, marker=None):
    bin_dir = script(tmp_path, "fake", body)
    env = drift.base_env({"PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin"})
    return await drift.exit_code_check("t", ["fake"], tmp_path, 10, env=env, drift_marker=marker)


async def test_clean_on_zero(tmp_path):
    assert (await classify(tmp_path, "exit 0"))["status"] == "clean"


async def test_drift_when_make_fails_without_failure_lines(tmp_path):
    out = await classify(
        tmp_path, "echo '  ~ ns1 record a.lab.test'; echo 'make: *** [x] Error 1'; exit 2"
    )
    assert out["status"] == "drift"


async def test_error_on_failure_lines(tmp_path):
    out = await classify(tmp_path, "echo 'error: PyYAML not available'; exit 2")
    assert out["status"] == "error"


async def test_marker_required_for_terraform(tmp_path):
    assert (await classify(tmp_path, "echo boom; exit 2", "drift detected"))["status"] == "error"
    hit = await classify(tmp_path, "echo 'Configuration drift detected!'; exit 2", "drift detected")
    assert hit["status"] == "drift"


async def test_timeout_is_error(tmp_path):
    bin_dir = script(tmp_path, "slow", "sleep 5")
    env = drift.base_env({"PATH": f"{bin_dir}:/usr/bin:/bin"})
    out = await drift.exit_code_check("t", ["slow"], tmp_path, 1, env=env)
    assert out["status"] == "error" and "timed out" in out["details"]


def test_base_env_drops_own_venv_and_unrelated_vars(monkeypatch):
    own = str(Path(sys.prefix) / "bin")
    monkeypatch.setenv("PATH", f"{own}:/opt/homebrew/bin:/usr/bin")
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("AWS_PROFILE", "r2")
    env = drift.base_env()
    assert own not in env["PATH"].split(":")
    assert "/opt/homebrew/bin" in env["PATH"]
    assert "GITHUB_TOKEN" not in env and env["AWS_PROFILE"] == "r2"


async def test_argocd_scope_and_skip_when_unconfigured(full_cfg, make_cfg, respx_mock):
    respx_mock.get("https://argo.test/api/v1/applications").respond(
        json={
            "items": [
                {
                    "metadata": {"name": "app-a"},
                    "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}},
                },
                {
                    "metadata": {"name": "app-b"},
                    "status": {"sync": {"status": "OutOfSync"}, "health": {"status": "Healthy"}},
                },
            ]
        }
    )
    out = await drift.report(full_cfg, ["argocd"])
    assert out["overall"] == "drift"
    assert [a["name"] for a in out["checks"][0]["details"]] == ["app-b"]

    empty = make_cfg("[pve]\nurl='x'\n")
    out = await drift.report(empty, ["argocd", "externalsecrets"])
    assert {c["status"] for c in out["checks"]} == {"skipped"}
    assert out["overall"] == "skipped"


def es(kind, name, status, namespace=None, reason="SecretSynced"):
    md = {"name": name, **({"namespace": namespace} if namespace else {})}
    cond = {"type": "Ready", "status": status, "reason": reason, "message": "m"}
    return {"kind": kind, "metadata": md, "status": {"conditions": [cond]}}


def test_not_ready_lists_only_failures():
    items = [
        es("ExternalSecret", "app-a-secret", "True", "app-a"),
        es("ExternalSecret", "app-b-secret", "False", "app-b", "SecretSyncedError"),
        es("ClusterSecretStore", "vault", "False", reason="InvalidProviderConfig"),
        {"kind": "ExternalSecret", "metadata": {"name": "new", "namespace": "x"}},
    ]
    bad = drift.not_ready(items)
    assert [(b["kind"], b["name"]) for b in bad] == [
        ("ExternalSecret", "app-b-secret"),
        ("ClusterSecretStore", "vault"),
        ("ExternalSecret", "new"),
    ]
    assert bad[0]["namespace"] == "app-b" and bad[0]["reason"] == "SecretSyncedError"
    assert "namespace" not in bad[1]
    assert bad[2]["reason"] == "no Ready condition"


async def test_externalsecrets_scope(make_cfg, tmp_path, monkeypatch):
    cfg = make_cfg("[kubernetes]\nkubeconfig = 'kc'\n")
    (tmp_path / "kc").write_text("users:\n- user:\n    token: t\n")
    doc = {
        "items": [
            es("ExternalSecret", "a", "True", "a"),
            es("ClusterSecretStore", "vault", "True"),
        ]
    }
    bin_dir = script(tmp_path, "kubectl", f"cat <<'EOF'\n{json.dumps(doc)}\nEOF\n")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin:/bin")
    out = await drift.report(cfg, ["externalsecrets"])
    assert out["overall"] == "clean" and out["checks"][0]["summary"] == "all 2 Ready"


def fake_repo(tmp_path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    return root


def repos_cfg(make_cfg, tmp_path, extra: str = "") -> object:
    infra, argo = fake_repo(tmp_path, "infra"), fake_repo(tmp_path, "argo")
    return make_cfg(f'[repos]\nproxmox_infra = "{infra}"\nargo_apps = "{argo}"\n' + extra, SECRETS)


# A fake make that reports, per target, which credential variables it got:
# presence only, never the value.
FAKE_MAKE = """
target="$3"
for v in PIHOLE_CREDENTIALS TECHNITIUM_API_TOKEN PBS_API_TOKEN_ID PBS_API_TOKEN_SECRET \\
         SEMAPHORE_API_TOKEN; do
  eval "val=\\${$v:-}"
  [ -n "$val" ] && echo "$target has $v"
done
case "$2" in terraform/*) echo "unit $2"; exit 0;; esac
exit 0
"""


async def test_doctors_get_mcp_credentials_except_technitium(make_cfg, tmp_path, monkeypatch):
    cfg = repos_cfg(make_cfg, tmp_path, FULL)
    bin_dir = script(tmp_path, "make", FAKE_MAKE)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin:/bin")
    for var in ("PIHOLE_CREDENTIALS", "TECHNITIUM_API_TOKEN", "PBS_API_TOKEN_ID"):
        monkeypatch.delenv(var, raising=False)
    out = await drift.report(cfg, ["dns", "semaphore", "pbs"])
    by = {c["check"]: c for c in out["checks"]}
    assert set(by) == {"technitium-doctor", "pihole-doctor", "semaphore-doctor", "pbs-doctor"}
    assert all(c["status"] == "clean" for c in by.values())
    assert out["overall"] == "clean"

    # Same fake, but failing, so the output lands in details.
    script(tmp_path, "make", FAKE_MAKE.replace("exit 0\n", "exit 2\n"))
    out = await drift.report(cfg, ["dns", "semaphore", "pbs"])
    details = {c["check"]: c.get("details", "") for c in out["checks"]}
    assert "TECHNITIUM_API_TOKEN" not in details["technitium-doctor"]
    assert "has PIHOLE_CREDENTIALS" in details["pihole-doctor"]
    assert "has PBS_API_TOKEN_SECRET" in details["pbs-doctor"]
    assert "has SEMAPHORE_API_TOKEN" in details["semaphore-doctor"]
    assert "pbs-secret-uuid" not in json.dumps(out)


async def test_terraform_scope_checks_every_unit(make_cfg, tmp_path, monkeypatch):
    cfg = repos_cfg(make_cfg, tmp_path)
    body = 'case "$2" in terraform/unifi) echo "No changes.";; esac\nexit 0\n'
    monkeypatch.setenv("PATH", f"{script(tmp_path, 'make', body)}{os.pathsep}/usr/bin:/bin")
    out = await drift.report(cfg, ["terraform"])
    assert [c["check"] for c in out["checks"]] == [
        "terraform lxc",
        "terraform talos",
        "terraform backup",
        "terraform vault",
        "terraform unifi",
    ]
    assert out["overall"] == "clean"


async def test_externalsecrets_refuses_an_admin_kubeconfig(make_cfg, tmp_path):
    cfg = make_cfg("[kubernetes]\nkubeconfig = 'kc'\n")
    (tmp_path / "kc").write_text("users:\n- user:\n    client-key-data: abc\n")
    out = await drift.report(cfg, ["externalsecrets"])
    check = out["checks"][0]
    assert check["status"] == "skipped" and "client certificate" in check["summary"]


async def test_externalsecrets_ignores_kubectl_warnings_on_stderr(make_cfg, tmp_path, monkeypatch):
    cfg = make_cfg("[kubernetes]\nkubeconfig = 'kc'\n")
    (tmp_path / "kc").write_text("users:\n- user:\n    token: t\n")
    doc = {"items": [es("ExternalSecret", "a", "True", "a")]}
    body = f"echo 'Warning: deprecated' >&2\ncat <<'EOF'\n{json.dumps(doc)}\nEOF\n"
    monkeypatch.setenv("PATH", f"{script(tmp_path, 'kubectl', body)}{os.pathsep}/usr/bin:/bin")
    out = await drift.report(cfg, ["externalsecrets"])
    assert out["overall"] == "clean"


async def test_render_check_ignores_stderr_and_flags_real_differences(
    make_cfg, tmp_path, monkeypatch
):
    cfg = repos_cfg(make_cfg, tmp_path)
    app = tmp_path / "argo" / "apps" / "user" / "app-a"
    app.mkdir(parents=True)
    (app / "config.yaml").write_text("{}\n")
    counter = tmp_path / "n"
    stable = "echo 'walk.go: warning' >&2\necho 'kind: ConfigMap'\n"
    monkeypatch.setenv("PATH", f"{script(tmp_path, 'kustomize', stable)}{os.pathsep}/usr/bin:/bin")
    async with Client(server.build(cfg)) as c:
        r = await c.call_tool("homelab_drift_render_check", {"app_name": "app-a"})
        assert r.structured_content["status"] == "clean"

        flaky = f'echo x >> "{counter}"\necho "rand: $(wc -l < "{counter}")"\n'
        script(tmp_path, "kustomize", flaky)
        r = await c.call_tool("homelab_drift_render_check", {"app_name": "app-a"})
        assert r.structured_content["status"] == "drift"

        r = await c.call_tool("homelab_drift_render_check", {"app_name": "../etc"})
        assert r.is_error and "plain app directory name" in r.content[0].text
        r = await c.call_tool("homelab_drift_render_check", {"app_name": "app-z"})
        assert r.is_error and "found 0" in r.content[0].text


async def test_drift_report_tool_skips_unconfigured_scopes(make_cfg):
    async with Client(server.build(make_cfg(""))) as c:
        r = await c.call_tool("homelab_drift_report", {"scopes": ["argocd", "komodo"]})
    assert not r.is_error, r.content[0].text
    out = r.structured_content
    assert out["overall"] == "skipped" and out["counts"] == {"skipped": 2}
