"""Entry point: build the MCPServer, register every system, run stdio (default) or HTTP."""

import argparse
import asyncio
import logging
from typing import Any

from mcp.server import MCPServer

from . import __version__
from . import config as config_mod
from .common import failure_message, tool
from .config import Config
from .systems import (
    MODULES,
    argocd,
    komodo,
    pbs,
    pihole,
    pve,
    semaphore,
    technitium,
    unraid,
)

INSTRUCTIONS = """Read-only view of the homelab. Nothing here changes state.
Start with homelab_overview, then drill into a system's tools. For "is anything
out of sync or broken" use drift_report. Changes go through the repo skills and
Bash (argo-apps, proxmox-infra), never through this server."""

SUMMARIES = {
    "pve": (pve.summary, "pve"),
    "pbs": (pbs.summary, "pbs"),
    "unraid": (unraid.summary, "unraid"),
    "technitium": (technitium.summary, "technitium"),
    "pihole": (pihole.summary, "pihole"),
    "komodo": (komodo.summary, "komodo"),
    "semaphore": (semaphore.summary, "semaphore"),
    "argocd": (argocd.summary, "argocd"),
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
        "errors": sorted(
            k for k, v in results.items() if isinstance(v, dict) and v.get("status") == "error"
        ),
    }


def build(cfg: Config) -> MCPServer:
    app = MCPServer("homelab", version=__version__, instructions=INSTRUCTIONS)

    @tool(
        app,
        "One-call health summary of every configured system (PVE, PBS, Unraid, Technitium, "
        "Pi-hole, Komodo, Semaphore, ArgoCD). Each system is queried in parallel with a "
        "timeout; a failing system shows its error without hiding the others.",
    )
    async def homelab_overview() -> dict[str, Any]:
        out = await overview(cfg)
        if cfg.load_error:
            out["config_error"] = cfg.load_error
        return out

    for module in MODULES:
        module.register(app, cfg)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(prog="homelab-mcp", description=__doc__)
    parser.add_argument(
        "--config",
        help="config.toml path (default: $HOMELAB_MCP_CONFIG or ~/.config/homelab-mcp/config.toml)",
    )
    parser.add_argument(
        "--http", action="store_true", help="serve streamable HTTP instead of stdio"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    # httpx logs every request URL at INFO; keep stderr for real problems.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = build(config_mod.load(args.config))
    if args.http:
        app.run("streamable-http", host=args.host, port=args.port)
    else:
        app.run("stdio")


if __name__ == "__main__":
    main()
