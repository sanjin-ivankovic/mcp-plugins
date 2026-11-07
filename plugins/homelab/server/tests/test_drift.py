"""Drift wrappers: classification of exit codes and output, env hygiene, ArgoCD scope."""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

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
        tmp_path, "echo '  ~ ns1 record a.example.com'; echo 'make: *** [x] Error 1'; exit 2"
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
                    "metadata": {"name": "memos"},
                    "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}},
                },
                {
                    "metadata": {"name": "harbor"},
                    "status": {"sync": {"status": "OutOfSync"}, "health": {"status": "Healthy"}},
                },
            ]
        }
    )
    out = await drift.report(full_cfg, ["argocd"])
    assert out["overall"] == "drift"
    assert [a["name"] for a in out["checks"][0]["details"]] == ["harbor"]

    empty = make_cfg("[pve]\nurl='x'\n")
    out = await drift.report(empty, ["argocd", "kryptos"])
    assert {c["status"] for c in out["checks"]} == {"skipped"}


GOOD = {"app": "a", "secret": "a-secret", "file": "a-sealed-secret.yaml", "sealed": True}
STATUS_OK = json.dumps(
    {
        "ok": True,
        "problems": 0,
        "configs": 2,
        "secrets": [dict(GOOD, immutable=["KEY"]), dict(GOOD, app="b", secret="b-secret")],
    }
)
STATUS_DRIFT = json.dumps(
    {
        "ok": False,
        "problems": 2,
        "configs": 2,
        "secrets": [
            dict(GOOD, immutable=["KEY"]),
            dict(GOOD, app="b", secret="b-secret", missing_keys=["NEW"]),
        ],
        "orphaned_files": ["a/old-sealed-secret.yaml"],
    }
)


def test_kryptos_clean():
    out = drift.kryptos_result(0, STATUS_OK, 0.0)
    assert out["status"] == "clean" and "2 secret(s)" in out["summary"]


def test_kryptos_drift_lists_only_problems():
    # kryptos' summary error lands on the merged stream after the JSON.
    out = drift.kryptos_result(1, STATUS_DRIFT + "\n2 problem(s)\n", 0.0)
    assert out["status"] == "drift" and out["summary"] == "2 problem(s)"
    assert [s["app"] for s in out["details"]["secrets"]] == ["b"]
    assert out["details"]["orphaned_files"] == ["a/old-sealed-secret.yaml"]
    assert "invalid_configs" not in out["details"]


def test_kryptos_without_json_is_error():
    out = drift.kryptos_result(1, 'Error: unknown command "status" for "kryptos"\n', 0.0)
    assert out["status"] == "error" and "unknown command" in out["details"]
