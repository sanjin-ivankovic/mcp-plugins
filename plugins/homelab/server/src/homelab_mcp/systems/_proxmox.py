"""Shared API-token client for Proxmox VE and Proxmox Backup Server (GET only)."""

from typing import Any

from ..common import HttpClient, client_from
from ..config import Config

# Token header per product: PVE joins id and secret with '=', PBS with ':'.
SCHEMES = {"pve": ("PVEAPIToken", "="), "pbs": ("PBSAPIToken", ":")}


def client(cfg: Config, name: str) -> HttpClient:
    sec = cfg.section(name)
    prefix, sep = SCHEMES[name]
    token_id = cfg.require(sec, "token_id", name)
    secret = cfg.secret(sec, "token_secret", name)
    return client_from(
        cfg, name, path="/api2/json", headers={"Authorization": f"{prefix}={token_id}{sep}{secret}"}
    )


async def data(cfg: Config, name: str, path: str, **params: Any) -> Any:
    return (await client(cfg, name).get(path, **params)).get("data")
