"""Drift report: wraps the checks the repos already have. Reports, never fixes.

Every subprocess is a fixed argv from this file, run in a configured repo
checkout with a minimal environment, a hard timeout, and redacted, capped
output. Nothing here builds a command from tool input except the app name
of homelab_drift_render_check, which must match an existing apps/<tier>/<app> dir.
"""

import asyncio
import difflib
import json
import os
import re
import shutil
import sys
import time
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from pathlib import Path
from typing import Annotated, Any, Literal, NotRequired, TypedDict, cast

from mcp.server import MCPServer
from pydantic import Field

from ..common import InputError, arr, failure_message, gather_map, log_tail, obj, strip_ansi, tool
from ..config import Config, ConfigError
from . import argocd, komodo, pihole, vault

Scope = Literal[
    "argocd",
    "externalsecrets",
    "vault",
    "komodo",
    "dns",
    "semaphore",
    "pbs",
    "inventory",
    "terraform",
]
DEFAULT_SCOPES: tuple[Scope, ...] = (
    "argocd",
    "externalsecrets",
    "vault",
    "komodo",
    "dns",
    "semaphore",
    "pbs",
)
ENV_KEEP = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE")
ENV_PREFIXES = (
    "AWS_",
    "TF_",
    "TG_",
    "TERRAGRUNT_",
    "TECHNITIUM_",
    "PIHOLE_",
    "SEMAPHORE_",
    "PBS_",
)
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
    env.update(obj(extra))
    return env


async def run(
    argv: list[str], cwd: Path, timeout: int, env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Exit code, stdout and stderr. Kept apart so a warning on stderr never
    corrupts output that is parsed (kubectl JSON) or compared (renders)."""
    if shutil.which(argv[0], path=(env or base_env()).get("PATH")) is None:
        raise ConfigError(f"{argv[0]} not found on PATH")
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env or base_env(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"timed out after {timeout}s"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def tail(text: str) -> str:
    return log_tail(text, MAX_OUTPUT)


def refuse_admin_kubeconfig(path: Path) -> None:
    """Same guard as scripts/run-kubernetes-mcp.sh: a client certificate means admin."""
    try:
        text = path.read_text()
    except OSError as exc:
        raise ConfigError(f"[kubernetes] kubeconfig {path}: {exc.strerror}") from None
    if "client-key-data" in text:
        raise ConfigError(
            f"[kubernetes] kubeconfig {path} holds a client certificate; "
            "use the read-only mcp-reader kubeconfig"
        )


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


def not_ready(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """ExternalSecrets and ClusterSecretStores whose Ready condition is not True.
    A sealed Vault shows up here: the store goes not-Ready and refreshes fail,
    while the Secrets already built stay in place."""
    bad: list[dict[str, Any]] = []
    for item in items:
        conds = {c.get("type"): c for c in arr((obj(item.get("status"))).get("conditions"))}
        ready = obj(conds.get("Ready"))
        if ready.get("status") != "True":
            md = obj(item.get("metadata"))
            entry = {"kind": item.get("kind"), "name": md.get("name")}
            if md.get("namespace"):
                entry["namespace"] = md["namespace"]
            entry["reason"] = ready.get("reason") or "no Ready condition"
            entry["message"] = (ready.get("message") or "")[:300]
            bad.append(entry)
    return bad


class KubectlFailed(Exception):
    """kubectl ran and failed; the message is its redacted output."""


# Reads several checks share within one report, keyed by name. The vault and
# externalsecrets scopes both need the ExternalSecrets list; one kubectl run
# serves both instead of two concurrent 60-second ones.
_SHARED: ContextVar[dict[str, asyncio.Task[Any]] | None] = ContextVar("drift_shared", default=None)


async def external_secrets(cfg: Config) -> list[dict[str, Any]]:
    """Every ExternalSecret and ClusterSecretStore, read once per report."""
    shared = _SHARED.get()
    if shared is None:
        return await _read_external_secrets(cfg)
    if "externalsecrets" not in shared:
        shared["externalsecrets"] = asyncio.ensure_future(_read_external_secrets(cfg))
    # shield: one check being cancelled must not cancel the run the other awaits.
    return await asyncio.shield(shared["externalsecrets"])


async def _read_external_secrets(cfg: Config) -> list[dict[str, Any]]:
    """Every ExternalSecret and ClusterSecretStore, read with the mcp-reader kubeconfig."""
    sec = cfg.section("kubernetes")
    kubeconfig = cfg.resolve_path(cfg.require(sec, "kubeconfig", "kubernetes"))
    refuse_admin_kubeconfig(kubeconfig)
    code, out, err = await run(
        [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig),
            "get",
            "externalsecrets.external-secrets.io,clustersecretstores.external-secrets.io",
            "-A",
            "-o",
            "json",
        ],
        Path.home(),
        60,
    )
    if code != 0:
        raise KubectlFailed(tail(out + err))
    return arr(json.loads(out).get("items"))


async def check_externalsecrets(cfg: Config) -> list[dict[str, Any]]:
    t = time.monotonic()
    try:
        items = await external_secrets(cfg)
    except KubectlFailed as exc:
        return [result("externalsecrets", "error", "kubectl failed", t, str(exc))]
    bad = not_ready(items)
    return [
        result(
            "externalsecrets",
            "drift" if bad else "clean",
            f"{len(bad)} of {len(items)} not Ready" if bad else f"all {len(items)} Ready",
            t,
            bad,
        )
    ]


def vault_refs(items: list[dict[str, Any]], store: str = "vault") -> dict[str, list[str]]:
    """Vault paths the ExternalSecrets of ClusterSecretStore ``store`` read, with the
    ExternalSecrets that read each (namespace/name)."""
    refs: dict[str, list[str]] = {}
    for item in items:
        if item.get("kind") != "ExternalSecret":
            continue
        spec = obj(item.get("spec"))
        ref = obj(spec.get("secretStoreRef"))
        if ref.get("kind") != "ClusterSecretStore" or ref.get("name") != store:
            continue
        md = obj(item.get("metadata"))
        owner = f"{md.get('namespace')}/{md.get('name')}"
        keys = [(obj(d.get("remoteRef"))).get("key") for d in arr(spec.get("data"))]
        keys += [(obj(d.get("extract"))).get("key") for d in arr(spec.get("dataFrom"))]
        for key in filter(None, keys):
            owners = refs.setdefault(key.strip("/"), [])
            if owner not in owners:
                owners.append(owner)
    return refs


async def check_vault(cfg: Config) -> list[dict[str, Any]]:
    """Vault paths ExternalSecrets read that do not exist (or whose current version is
    deleted), and paths under the same prefix that no ExternalSecret reads."""
    t = time.monotonic()
    prefix = str(cfg.section("vault").get("eso_prefix", "k8s/")).strip("/") + "/"
    try:
        items = await external_secrets(cfg)
    except KubectlFailed as exc:
        return [result("vault", "error", "kubectl failed", t, str(exc))]
    refs = vault_refs(items)
    paths = await vault.walk(cfg, prefix)
    meta = await gather_map(paths, lambda p: vault.metadata(cfg, p))
    gone = {m["path"] for m in meta if m["current_deleted"] or m["current_destroyed"]}
    details = {
        "missing": [{"path": p, "read_by": refs[p]} for p in sorted(set(refs) - set(paths))],
        "current_version_deleted": [{"path": p, "read_by": refs.get(p, [])} for p in sorted(gone)],
        "unreferenced": sorted(p for p in set(paths) - set(refs) - gone if p.startswith(prefix)),
    }
    n = sum(len(v) for v in details.values())
    summary = (
        f"{n} finding(s) across {len(refs)} referenced and {len(paths)} stored paths"
        if n
        else f"all {len(refs)} referenced paths exist, none unreferenced under {prefix}"
    )
    return [
        result(
            "vault", "drift" if n else "clean", summary, t, {k: v for k, v in details.items() if v}
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
    code, stdout, stderr = await run(argv, cwd, timeout, env)
    out = stdout + stderr
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
    lines = (ln.strip() for ln in strip_ansi(output).splitlines())
    return any(FAILURE_LINE.match(ln) for ln in lines if ln and not ln.startswith("make"))


async def doctor(
    cfg: Config,
    name: str,
    timeout: int,
    env_from: Callable[[Config], dict[str, str]] | None = None,
) -> dict[str, Any]:
    """``make -C ansible <name>`` in proxmox-infra. ``env_from`` hands the doctor
    this server's read-only credentials so it skips its Vault lookup; without it
    the doctor reads Vault and needs the user's ``vault login``."""
    t = time.monotonic()
    root = repo(cfg, "proxmox_infra")
    try:
        env = base_env(env_from(cfg)) if env_from else base_env()
    except ConfigError as exc:
        return result(name, "skipped", failure_message(exc), t)
    return await exit_code_check(name, ["make", "-C", "ansible", name], root, timeout, env=env)


def pihole_env(cfg: Config) -> dict[str, str]:
    lines = [f"{n.name} {n.password}" for n in pihole.nodes(cfg)]
    return {"PIHOLE_CREDENTIALS": "\n".join(lines)}


def semaphore_env(cfg: Config) -> dict[str, str]:
    sec = cfg.section("semaphore")
    return {
        "SEMAPHORE_URL": cfg.require(sec, "url", "semaphore"),
        "SEMAPHORE_API_TOKEN": cfg.secret(sec, "token", "semaphore"),
    }


def pbs_env(cfg: Config) -> dict[str, str]:
    sec = cfg.section("pbs")
    return {
        "PBS_URL": cfg.require(sec, "url", "pbs"),
        "PBS_API_TOKEN_ID": cfg.require(sec, "token_id", "pbs"),
        "PBS_API_TOKEN_SECRET": cfg.secret(sec, "token_secret", "pbs"),
    }


async def check_dns(cfg: Config) -> list[dict[str, Any]]:
    # technitium-doctor reads server settings, which the MCP token (zone View
    # only) is denied, so it keeps the Vault token. Pi-hole's app password is
    # the same one the doctor would read from Vault.
    return list(
        await asyncio.gather(
            doctor(cfg, "technitium-doctor", 180),
            doctor(cfg, "pihole-doctor", 120, pihole_env),
        )
    )


async def check_semaphore(cfg: Config) -> list[dict[str, Any]]:
    return [await doctor(cfg, "semaphore-doctor", 120, semaphore_env)]


async def check_pbs(cfg: Config) -> list[dict[str, Any]]:
    return [await doctor(cfg, "pbs-doctor", 120, pbs_env)]


async def check_inventory(cfg: Config) -> list[dict[str, Any]]:
    return [await doctor(cfg, "inventory-doctor", 300)]


async def check_terraform(cfg: Config) -> list[dict[str, Any]]:
    root = repo(cfg, "proxmox_infra")

    async def unifi() -> dict[str, Any]:
        t = time.monotonic()
        code, stdout, stderr = await run(["make", "-C", "terraform/unifi", "plan"], root, 900)
        out = stdout + stderr
        clean = strip_ansi(out)
        if code == 0 and "No changes." in clean:
            return result("terraform unifi", "clean", "no changes", t)
        plan = re.search(r"^Plan: .*$", clean, re.M)
        if code == 0 and plan:
            return result(
                "terraform unifi",
                "drift",
                plan.group(0),
                t,
                tail(out),
            )
        return result("terraform unifi", "error", f"exit {code}", t, tail(out))

    units = [
        exit_code_check(
            f"terraform {unit}",
            ["make", "-C", f"terraform/{unit}", "drift-detect"],
            root,
            900,
            drift_marker="drift detected",
        )
        for unit in ("lxc", "talos", "backup", "vault")
    ]
    return list(await asyncio.gather(*units, unifi()))


CHECKS = {
    "argocd": check_argocd,
    "externalsecrets": check_externalsecrets,
    "vault": check_vault,
    "komodo": check_komodo,
    "dns": check_dns,
    "semaphore": check_semaphore,
    "pbs": check_pbs,
    "inventory": check_inventory,
    "terraform": check_terraform,
}


async def report(cfg: Config, scopes: Sequence[str]) -> dict[str, Any]:
    async def one(scope: str) -> list[dict[str, Any]]:
        t = time.monotonic()
        try:
            return await CHECKS[scope](cfg)
        except ConfigError as exc:
            return [result(scope, "skipped", failure_message(exc), t)]
        except Exception as exc:  # noqa: BLE001 - one failing check must not hide the others
            return [result(scope, "error", failure_message(exc), t)]

    token = _SHARED.set({})
    try:
        groups = await asyncio.gather(*(one(s) for s in scopes))
    finally:
        _SHARED.reset(token)
    checks = [c for group in groups for c in group]
    counts: dict[str, int] = {}
    for c in checks:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    # Nothing that ran found a problem, but "clean" needs at least one clean check.
    overall = (
        "drift"
        if counts.get("drift")
        else "error"
        if counts.get("error")
        else "clean"
        if counts.get("clean")
        else "skipped"
    )
    return {
        "overall": overall,
        "counts": counts,
        "checks": checks,
    }


class DriftReport(TypedDict):
    overall: str
    counts: dict[str, int]
    checks: list[dict[str, Any]]


class RenderCheck(TypedDict):
    app: str
    status: str
    lines: NotRequired[int]
    diff: NotRequired[str | None]
    details: NotRequired[Any]


def register(app: MCPServer, cfg: Config) -> None:
    @tool(
        app,
        "Drift report across the homelab. Default scopes: argocd (apps not Synced+Healthy), "
        "externalsecrets (ExternalSecrets or ClusterSecretStores not Ready), vault (Vault "
        "paths ExternalSecrets read that are missing or deleted, and unreferenced ones), "
        "komodo (stacks "
        "down or missing files, pending ResourceSync), dns (technitium and pihole doctors in "
        "proxmox-infra; technitium needs a vault login), semaphore and pbs (their doctors). "
        "Opt-in: inventory (inventory-doctor) and terraform (lxc/talos/backup/vault "
        "drift-detect + unifi plan; slow, needs a vault login). Each check reports "
        "clean|drift|error|skipped. Reports only: fixes go through the repo skills.",
        title="Drift report",
    )
    async def homelab_drift_report(
        scopes: Annotated[
            list[Scope] | None, Field(description="Scopes to check; the default set when absent")
        ] = None,
        include_terraform: Annotated[
            bool, Field(description="Also run the slow Terraform plans")
        ] = False,
    ) -> DriftReport:
        chosen = list(scopes or DEFAULT_SCOPES)
        if include_terraform and "terraform" not in chosen:
            chosen.append("terraform")
        return cast(DriftReport, await report(cfg, chosen))

    @tool(
        app,
        "Render an argo-apps app twice with `kustomize build --enable-helm` and diff the output. "
        "Any difference is non-determinism that ArgoCD will show as permanent drift.",
        title="Render determinism check",
    )
    async def homelab_drift_render_check(
        app_name: Annotated[str, Field(description="argo-apps app directory name, e.g. searxng")],
    ) -> RenderCheck:
        if not APP_NAME.match(app_name):
            raise InputError("app_name must be a plain app directory name")
        root = repo(cfg, "argo_apps")
        dirs = [p for p in (root / "apps").glob(f"*/{app_name}") if (p / "config.yaml").is_file()]
        if len(dirs) != 1:
            raise InputError(
                f"expected one apps/<tier>/{app_name} with config.yaml, found {len(dirs)}"
            )
        rel = str(dirs[0].relative_to(root))
        renders: list[list[str]] = []
        for _ in range(2):
            code, out, err = await run(["kustomize", "build", "--enable-helm", rel], root, 300)
            if code != 0:
                return {"app": rel, "status": "error", "details": tail(out + err)}
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
