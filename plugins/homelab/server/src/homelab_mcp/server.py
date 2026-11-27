"""Entry point: build the MCPServer, register every system, run stdio (default) or HTTP."""

import asyncio
from typing import Any, NotRequired, TypedDict, cast

from mcp.server import MCPServer

from .common import ALWAYS_LOAD, failure_message, make_app, obj, serve, tool
from .config import Config
from .systems import (
    MODULES,
    alertmanager,
    argocd,
    komodo,
    ntfy,
    pbs,
    pihole,
    pve,
    semaphore,
    technitium,
    traefik,
    unraid,
    vault,
)

INSTRUCTIONS = """Read-only view of the homelab. Nothing here changes state.
Start with homelab_overview, then drill into a system's tools. For "is anything
out of sync or broken" use homelab_drift_report. Changes go through the repo
skills and Bash (argo-apps, proxmox-infra), never through this server.
Each tool is named after its system: pve_ (Proxmox VE), pbs_ (backups),
unraid_, technitium_ (DNS and DHCP), pihole_, komodo_ (Docker stacks),
semaphore_ (Ansible runs), traefik_ (routes), alertmanager_, ntfy_
(notifications), vault_ (KV metadata), gitlab_ (CI jobs and logs), homelab_
(overview and drift)."""

SUMMARIES = {
    "pve": (pve.summary, "pve"),
    "pbs": (pbs.summary, "pbs"),
    "unraid": (unraid.summary, "unraid"),
    "technitium": (technitium.summary, "technitium"),
    "pihole": (pihole.summary, "pihole"),
    "komodo": (komodo.summary, "komodo"),
    "semaphore": (semaphore.summary, "semaphore"),
    "argocd": (argocd.summary, "argocd"),
    "traefik": (traefik.summary, "traefik"),
    "alertmanager": (alertmanager.summary, "alertmanager"),
    "ntfy": (ntfy.summary, "ntfy"),
    "vault": (vault.summary, "vault"),
}


async def overview(cfg: Config, timeout: float = 25.0) -> dict[str, Any]:
    async def one(name: str) -> tuple[str, Any]:
        fn, section = SUMMARIES[name]
        if not cfg.has(section):
            return name, {"status": "not configured"}
        try:
            return name, await asyncio.wait_for(fn(cfg), timeout)
        except TimeoutError:
            return name, {"status": "error", "error": f"timed out after {timeout}s"}
        except Exception as exc:  # noqa: BLE001 - one backend must not hide the rest
            return name, {"status": "error", "error": failure_message(exc)}

    results = dict(await asyncio.gather(*(one(n) for n in SUMMARIES)))
    return {
        "systems": results,
        "errors": sorted(k for k, v in results.items() if obj(v).get("status") == "error"),
    }


class Overview(TypedDict):
    systems: dict[str, Any]
    errors: list[str]
    config_error: NotRequired[str]
    config_warnings: NotRequired[list[str]]


def build(cfg: Config) -> MCPServer[None]:
    app = make_app("homelab", INSTRUCTIONS)

    @tool(
        app,
        "One-call health summary of every configured system (PVE, PBS, Unraid, Technitium, "
        "Pi-hole, Komodo, Semaphore, ArgoCD, Traefik, Alertmanager, ntfy, Vault). Each system is "
        "queried in parallel with a timeout; a failing system shows its error without "
        "hiding the others.",
        title="Homelab health overview",
        meta={ALWAYS_LOAD: True},
    )
    async def homelab_overview() -> Overview:
        out = await overview(cfg)
        if cfg.load_error:
            out["config_error"] = cfg.load_error
        if cfg.warnings:
            out["config_warnings"] = cfg.warnings
        return cast(Overview, out)

    for module in MODULES:
        module.register(app, cfg)
    return app


def main() -> None:
    serve("homelab-mcp", __doc__, build, 8765)


if __name__ == "__main__":
    main()
