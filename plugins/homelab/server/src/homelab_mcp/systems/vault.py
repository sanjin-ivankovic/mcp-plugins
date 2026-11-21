"""HashiCorp Vault (AppRole `mcp-reader`, policy `mcp-reader`). Metadata only.

The policy grants `list` and `read` on `kv/metadata/*` and nothing else, so
this module can see which secret paths exist and their version history, never
a secret value: `kv/data/*` is not granted. Besides the AppRole login (the one
allowlisted POST), every call is a GET. The seal status needs no token.
"""

from typing import Annotated, Any, TypedDict

from mcp.server import MCPServer
from pydantic import Field

from .. import redact
from ..common import (
    ApiError,
    HttpClient,
    Session,
    arr,
    client_from,
    clip,
    gather_map,
    obj,
    pick,
    tool,
)
from ..config import Config

LOGIN = "/auth/approle/login"


def mount(cfg: Config) -> str:
    return str(cfg.section("vault").get("mount", "kv")).strip("/")


def anonymous(cfg: Config) -> HttpClient:
    return client_from(cfg, "vault", path="/v1", read_posts=(LOGIN,))


async def login(cfg: Config) -> str:
    sec = cfg.section("vault")
    body = {
        "role_id": cfg.require(sec, "role_id", "vault"),
        "secret_id": cfg.secret(sec, "secret_id", "vault"),
    }
    resp = await anonymous(cfg).post_read(LOGIN, json=body)
    token = (obj(resp.get("auth"))).get("client_token")
    if not token:
        raise ApiError("vault: AppRole login returned no token")
    redact.register(token)
    return token


def session(cfg: Config) -> Session:
    return Session(cfg.cache, ("vault-token", cfg.section("vault").get("url")), lambda: login(cfg))


async def get(cfg: Config, path: str, **params: Any) -> Any:
    async def send(token: str) -> Any:
        http = client_from(cfg, "vault", path="/v1", headers={"X-Vault-Token": token})
        return await http.get(path, **params)

    return await session(cfg).call(send)


async def list_keys(cfg: Config, prefix: str) -> list[str]:
    """Keys directly under ``prefix`` (folders end in "/"); an empty folder answers 404."""
    try:
        resp = await get(cfg, f"/{mount(cfg)}/metadata/{prefix}", list="true")
    except ApiError as exc:
        if exc.status == 404:
            return []
        raise
    return [str(k) for k in arr(obj(resp.get("data")).get("keys"))]


async def walk(cfg: Config, prefix: str) -> list[str]:
    """Every secret path below ``prefix``, relative to the mount, one level per round."""
    paths: list[str] = []
    folders: list[str] = [prefix]
    while folders:
        listed = await gather_map(folders, lambda f: list_keys(cfg, f))
        found = [base + key for base, keys in zip(folders, listed, strict=True) for key in keys]
        paths += [f for f in found if not f.endswith("/")]
        folders = [f for f in found if f.endswith("/")]
    return sorted(paths)


async def metadata(cfg: Config, path: str) -> dict[str, Any]:
    data = obj((await get(cfg, f"/{mount(cfg)}/metadata/{path}")).get("data"))
    current = data.get("current_version")
    version = obj((obj(data.get("versions"))).get(str(current)))
    return {
        "path": path,
        "current_version": current,
        "updated_time": data.get("updated_time"),
        "current_deleted": bool(version.get("deletion_time")),
        "current_destroyed": bool(version.get("destroyed")),
    }


async def seal_status(cfg: Config) -> dict[str, Any]:
    return pick(
        await anonymous(cfg).get("/sys/seal-status"), "sealed", "initialized", "version", "progress"
    )


async def summary(cfg: Config) -> dict[str, Any]:
    status = await seal_status(cfg)
    return {"sealed": status.get("sealed"), "version": status.get("version")}


class KvPaths(TypedDict):
    mount: str
    count: int
    truncated: bool
    items: list[dict[str, Any]]


def register(app: MCPServer, cfg: Config) -> None:
    @tool(
        app,
        "Secret paths in HashiCorp Vault's KV store under a prefix (default k8s/, the "
        "External Secrets Operator's area), with current version, last update and whether "
        "that version is deleted or destroyed. Metadata only: never a secret value.",
        title="Vault KV paths",
    )
    async def vault_kv_paths(
        prefix: Annotated[str, Field(description="KV path prefix, e.g. k8s/")] = "k8s/",
        limit: Annotated[int, Field(description="Rows to return, 1-1000")] = 200,
    ) -> KvPaths:
        base = prefix.strip("/") + "/" if prefix.strip("/") else ""
        paths = await walk(cfg, base)
        # Metadata costs one request per path: fetch it only for the rows returned.
        listed = clip(paths, limit)
        listed["items"] = await gather_map(listed["items"], lambda p: metadata(cfg, p))
        return {"mount": mount(cfg), **listed}
