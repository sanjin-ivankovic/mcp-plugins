"""Drift report: wraps the checks the repos already have. Reports, never fixes.

Every subprocess is a fixed argv from this file, run in a configured repo
checkout with a minimal environment, a hard timeout, and redacted, capped
output. Nothing here builds a command from tool input except the app name
of drift_render_check, which must match an existing apps/<tier>/<app> dir.
"""

import asyncio
import difflib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Literal

from mcp.server import MCPServer

from ..common import failure_message, tool
from ..config import Config, ConfigError
from ..redact import redact
from . import argocd, komodo

Scope = Literal["argocd", "sealedsecrets", "komodo", "kryptos", "dns", "terraform"]
DEFAULT_SCOPES: tuple[Scope, ...] = ("argocd", "sealedsecrets", "komodo", "kryptos", "dns")
ENV_KEEP = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE")
ENV_PREFIXES = (
    "AWS_",
    "TF_",
    "TG_",
    "TERRAGRUNT_",
    "TECHNITIUM_",
    "PIHOLE_",
    "SEMAPHORE_",
)
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
APP_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
MAX_OUTPUT = 4000


def repo(cfg: Config, key: str) -> Path:
    path = cfg.section("repos").get(key) if cfg.has("repos") else None
    if not path:
        raise ConfigError(f"[repos] {key} is not configured")
    p = Path(os.path.expanduser(path))
    if not p.is_dir():
        raise ConfigError(f"[repos] {key} = {p} is not a directory")
    return p


def base_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in ENV_KEEP or k.startswith(ENV_PREFIXES)}
    # `uv run` puts this server's venv first on PATH. Left there, the repo
    # scripts' `#!/usr/bin/env python3` resolves to a Python without PyYAML.
    own = {str(Path(sys.prefix) / "bin"), os.environ.get("VIRTUAL_ENV", "") + "/bin"}
    env["PATH"] = os.pathsep.join(
        d for d in env.get("PATH", os.defpath).split(os.pathsep) if d and d not in own
    )
    env.update(extra or {})
    return env


async def run(
    argv: list[str], cwd: Path, timeout: int, env: dict[str, str] | None = None
) -> tuple[int, str]:
    if shutil.which(argv[0], path=(env or base_env()).get("PATH")) is None:
        raise ConfigError(f"{argv[0]} not found on PATH")
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env or base_env(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"timed out after {timeout}s"
    return proc.returncode or 0, out.decode(errors="replace")


def tail(text: str) -> str:
    text = redact(ANSI.sub("", text)).strip()
    return text if len(text) <= MAX_OUTPUT else "…" + text[-MAX_OUTPUT:]


def result(
    name: str, status: str, summary: str, started: float, details: Any = None
) -> dict[str, Any]:
    out = {
        "check": name,
        "status": status,
        "summary": summary,
        "seconds": round(time.monotonic() - started, 1),
    }
    if details:
        out["details"] = details
    return out


async def check_argocd(cfg: Config) -> list[dict[str, Any]]:
    t = time.monotonic()
    bad = argocd.problems(await argocd.applications(cfg))
    return [
        result(
            "argocd",
            "drift" if bad else "clean",
            f"{len(bad)} application(s) not Synced+Healthy"
            if bad
            else "all applications Synced+Healthy",
            t,
            bad,
        )
    ]


async def check_sealedsecrets(cfg: Config) -> list[dict[str, Any]]:
    import json

    t = time.monotonic()
    kubeconfig = cfg.resolve_path(cfg.section("kubernetes")["kubeconfig"])
    code, out = await run(
        [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig),
            "get",
            "sealedsecrets.bitnami.com",
            "-A",
            "-o",
            "json",
        ],
        Path.home(),
        60,
    )
    if code != 0:
        return [result("sealedsecrets", "error", "kubectl failed", t, tail(out))]
    bad = []
    for item in json.loads(out).get("items") or []:
        conds = {c.get("type"): c for c in (item.get("status") or {}).get("conditions") or []}
        synced = conds.get("Synced") or {}
        if synced.get("status") != "True":
            md = item.get("metadata") or {}
            bad.append(
                {
                    "namespace": md.get("namespace"),
                    "name": md.get("name"),
                    "message": (synced.get("message") or "no Synced condition")[:300],
                }
            )
    return [
        result(
            "sealedsecrets",
            "drift" if bad else "clean",
            f"{len(bad)} SealedSecret(s) not unsealed" if bad else "all SealedSecrets unsealed",
            t,
            bad,
        )
    ]


async def check_komodo(cfg: Config) -> list[dict[str, Any]]:
    t = time.monotonic()
    stacks = await komodo.stacks(cfg)
    syncs = await komodo.syncs(cfg)
    bad = {
        "stacks_not_running": [s["name"] for s in stacks if s["state"] != "running"],
        "stacks_project_missing": [s["name"] for s in stacks if s["project_missing"]],
        "syncs_pending": [
            {"name": s["name"], "updates": s["pending_updates"], "error": s["pending_error"]}
            for s in syncs
            if s["has_pending"]
        ],
    }
    n = sum(len(v) for v in bad.values())
    return [
        result(
            "komodo",
            "drift" if n else "clean",
            f"{n} Komodo finding(s)" if n else "stacks running, syncs clean",
            t,
            {k: v for k, v in bad.items() if v},
        )
    ]


async def exit_code_check(
    name: str,
    argv: list[str],
    cwd: Path,
    timeout: int,
    env: dict[str, str] | None = None,
    drift_marker: str | None = None,
) -> dict[str, Any]:
    """0 is clean. make exits 2 for any failed recipe, so a non-zero exit is
    classified from the output: failure lines mean error; otherwise drift,
    provided ``drift_marker`` (when given) appears in the output."""
    t = time.monotonic()
    code, out = await run(argv, cwd, timeout, env)
    if code == 0:
        return result(name, "clean", "no drift", t)
    if looks_like_failure(out) or (drift_marker and drift_marker not in out):
        return result(name, "error", f"exit {code}", t, tail(out))
    return result(name, "drift", f"exit {code}", t, tail(out))


# The doctor scripts exit 1 both for drift and for "could not check". These
# lines mark the second case; make's own "make: *** ... Error N" is ignored.
FAILURE_LINE = re.compile(
    r"^(error:|traceback|timed out after|.*must be set|.*unreachable|.*not available"
    r"|.*no such file"
    r"|.*-> http \d{3})",
    re.I,
)


def looks_like_failure(output: str) -> bool:
    lines = (ln.strip() for ln in ANSI.sub("", output).splitlines())
    return any(FAILURE_LINE.match(ln) for ln in lines if ln and not ln.startswith("make"))


def kryptos_result(code: int, out: str, started: float) -> dict[str, Any]:
    """Classify `kryptos status --output json`. kryptos prints its summary error
    to stderr, which run() merges into the output, so decode the first JSON
    document and ignore the rest. No document means kryptos itself failed (or
    is older than the status command)."""
    start = out.find("{")
    try:
        st = json.JSONDecoder().raw_decode(out[start:])[0] if start >= 0 else None
    except ValueError:
        st = None
    if not isinstance(st, dict) or "ok" not in st:
        return result("kryptos status", "error", f"exit {code}", started, tail(out))
    secrets = st.get("secrets") or []
    if st["ok"]:
        return result(
            "kryptos status",
            "clean",
            f"{st.get('configs', 0)} config(s), {len(secrets)} secret(s) sealed, no drift",
            started,
        )
    details = {
        "secrets": [
            {k: v for k, v in s.items() if k != "immutable"}
            for s in secrets
            if not s.get("sealed")
            or s.get("error")
            or s.get("missing_keys")
            or s.get("orphaned_keys")
        ],
        "orphaned_files": st.get("orphaned_files") or [],
        "invalid_configs": st.get("invalid_configs") or [],
    }
    return result(
        "kryptos status",
        "drift",
        f"{st.get('problems', 0)} problem(s)",
        started,
        {k: v for k, v in details.items() if v},
    )


async def check_kryptos(cfg: Config) -> list[dict[str, Any]]:
    """One pass over the argo-apps configs and sealed files (validate, audit and
    diff), read from kryptos' JSON report."""
    root = repo(cfg, "argo_apps")
    t = time.monotonic()
    code, out = await run(["kryptos", "status", "--output", "json"], root, 120)
    return [kryptos_result(code, out, t)]


async def check_dns(cfg: Config) -> list[dict[str, Any]]:
    root = repo(cfg, "proxmox_infra")
    checks = [
        exit_code_check(
            "technitium-doctor", ["make", "-C", "ansible", "technitium-doctor"], root, 180
        ),
        exit_code_check("pihole-doctor", ["make", "-C", "ansible", "pihole-doctor"], root, 120),
    ]
    semaphore = semaphore_doctor(cfg, root)
    results = await asyncio.gather(*checks, semaphore)
    return list(results)


async def semaphore_doctor(cfg: Config, root: Path) -> dict[str, Any]:
    t = time.monotonic()
    if "SEMAPHORE_API_TOKEN" in os.environ:
        env = base_env()
    elif cfg.has("semaphore"):
        sec = cfg.section("semaphore")
        env = base_env(
            {
                "SEMAPHORE_URL": sec["url"],
                "SEMAPHORE_API_TOKEN": cfg.secret(sec, "token", "semaphore"),
            }
        )
    else:
        return result("semaphore-doctor", "skipped", "[semaphore] is not configured", t)
    return await exit_code_check(
        "semaphore-doctor", ["make", "-C", "ansible", "semaphore-doctor"], root, 120, env=env
    )


async def check_terraform(cfg: Config) -> list[dict[str, Any]]:
    root = repo(cfg, "proxmox_infra")

    async def unifi() -> dict[str, Any]:
        t = time.monotonic()
        code, out = await run(["make", "-C", "terraform/unifi", "plan"], root, 900)
        clean = ANSI.sub("", out)
        if code == 0 and "No changes." in clean:
            return result("terraform unifi", "clean", "no changes", t)
        if code == 0 and re.search(r"^Plan: ", clean, re.M):
            return result(
                "terraform unifi",
                "drift",
                re.search(r"^Plan: .*$", clean, re.M).group(0),
                t,
                tail(out),
            )
        return result("terraform unifi", "error", f"exit {code}", t, tail(out))

    return list(
        await asyncio.gather(
            exit_code_check(
                "terraform lxc",
                ["make", "-C", "terraform/lxc", "drift-detect"],
                root,
                900,
                drift_marker="drift detected",
            ),
            exit_code_check(
                "terraform talos",
                ["make", "-C", "terraform/talos", "drift-detect"],
                root,
                900,
                drift_marker="drift detected",
            ),
            unifi(),
        )
    )


CHECKS = {
    "argocd": check_argocd,
    "sealedsecrets": check_sealedsecrets,
    "komodo": check_komodo,
    "kryptos": check_kryptos,
    "dns": check_dns,
    "terraform": check_terraform,
}


async def report(cfg: Config, scopes: list[str]) -> dict[str, Any]:
    async def one(scope: str) -> list[dict[str, Any]]:
        t = time.monotonic()
        try:
            return await CHECKS[scope](cfg)
        except ConfigError as exc:
            return [result(scope, "skipped", failure_message(exc), t)]
        except Exception as exc:  # noqa: BLE001 - one failing check must not hide the others
            return [result(scope, "error", failure_message(exc), t)]

    checks = [c for group in await asyncio.gather(*(one(s) for s in scopes)) for c in group]
    counts: dict[str, int] = {}
    for c in checks:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    return {
        "overall": "drift" if counts.get("drift") else "error" if counts.get("error") else "clean",
        "counts": counts,
        "checks": checks,
    }


def register(app: MCPServer, cfg: Config) -> None:
    @tool(
        app,
        "Drift report across the homelab. Scopes: argocd (apps not Synced+Healthy), "
        "sealedsecrets (not unsealed in-cluster), komodo (stacks down or missing files, "
        "pending ResourceSync), kryptos "
        "(status: validate+audit+diff in argo-apps), dns (technitium/pihole/semaphore --doctor in "
        "proxmox-infra), "
        "terraform (lxc/talos drift-detect + unifi plan; slow, opt-in). Each check reports "
        "clean|drift|error|skipped. Reports only: fixes go through the repo skills.",
    )
    async def drift_report(
        scopes: list[Scope] | None = None, include_terraform: bool = False
    ) -> dict[str, Any]:
        chosen = list(scopes or DEFAULT_SCOPES)
        if include_terraform and "terraform" not in chosen:
            chosen.append("terraform")
        return await report(cfg, chosen)

    @tool(
        app,
        "Render an argo-apps app twice with `kustomize build --enable-helm` and diff the output. "
        "Any difference is non-determinism that ArgoCD will show as permanent drift.",
    )
    async def drift_render_check(app_name: str) -> dict[str, Any]:
        if not APP_NAME.match(app_name):
            return {"error": "app_name must be a plain app directory name"}
        root = repo(cfg, "argo_apps")
        dirs = [p for p in (root / "apps").glob(f"*/{app_name}") if (p / "config.yaml").is_file()]
        if len(dirs) != 1:
            return {
                "error": f"expected one apps/<tier>/{app_name} with config.yaml, found {len(dirs)}"
            }
        rel = str(dirs[0].relative_to(root))
        renders = []
        for _ in range(2):
            code, out = await run(["kustomize", "build", "--enable-helm", rel], root, 300)
            if code != 0:
                return {"app": rel, "status": "error", "details": tail(out)}
            renders.append(out.splitlines())
        diff = list(
            difflib.unified_diff(renders[0], renders[1], "render-1", "render-2", lineterm="", n=2)
        )
        return {
            "app": rel,
            "status": "drift" if diff else "clean",
            "lines": len(renders[0]),
            "diff": tail("\n".join(diff)) if diff else None,
        }
