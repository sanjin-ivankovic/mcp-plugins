"""Traefik API (no credential). GET only.

The `api@internal` route is published on the `lan` entrypoint only, with no
authentication, so the LAN boundary is the guard here, as with Longhorn.
"""

from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from pydantic import Field

from ..common import HttpClient, Listing, arr, client_from, clip, obj, pick, tool
from ..config import Config

Status = Literal["enabled", "disabled", "warning"]


def client(cfg: Config) -> HttpClient:
    return client_from(cfg, "traefik", path="/api")


def matches(item: dict[str, Any], search: str | None, *fields: str) -> bool:
    if not search:
        return True
    hay = " ".join(str(item.get(f, "")) for f in fields).lower()
    return search.lower() in hay


async def summary(cfg: Config) -> dict[str, Any]:
    overview = await client(cfg).get("/overview")
    http = obj(overview.get("http"))
    return {
        kind: {
            k: v for k, v in (obj(http.get(kind))).items() if k in ("total", "warnings", "errors")
        }
        for kind in ("routers", "services", "middlewares")
    }


def register(app: MCPServer, cfg: Config) -> None:
    @tool(
        app,
        "Traefik HTTP routers: rule (the hosts and paths a route matches), entry points "
        "(lan, public), middlewares (the SSO gate shows here), backing service, TLS and "
        "status. search matches rule, name and service, e.g. a hostname.",
        title="Traefik routers",
    )
    async def traefik_routers(
        search: Annotated[
            str | None, Field(description="Substring of the rule, name or service, e.g. a hostname")
        ] = None,
        status: Annotated[Status | None, Field(description="Router status")] = None,
        provider: Annotated[
            str | None, Field(description="Provider name, exactly as the router list shows it")
        ] = None,
        limit: Annotated[int, Field(description="Rows to return, 1-1000")] = 200,
    ) -> Listing:
        rows = [
            {
                **pick(r, "name", "entryPoints", "middlewares", "service", "status", "provider"),
                "rule": " ".join(str(r.get("rule", "")).split()),
                "tls": bool(r.get("tls")),
                "error": r.get("error"),
            }
            for r in arr(await client(cfg).get("/http/routers", per_page=1000))
            if (status is None or r.get("status") == status)
            and (provider is None or r.get("provider") == provider)
            and matches(r, search, "rule", "name", "service")
        ]
        return clip([{k: v for k, v in r.items() if v is not None} for r in rows], limit)

    @tool(
        app,
        "Traefik HTTP services: backend servers with their health (serverStatus UP/DOWN), "
        "the Kubernetes service behind them and the routers using them. down_only keeps "
        "services with a server that is not UP.",
        title="Traefik services",
    )
    async def traefik_services(
        search: Annotated[
            str | None, Field(description="Substring of the service name or of a router using it")
        ] = None,
        down_only: Annotated[
            bool, Field(description="Only services with a server that is not UP")
        ] = False,
        limit: Annotated[int, Field(description="Rows to return, 1-1000")] = 200,
    ) -> Listing:
        rows: list[dict[str, Any]] = []
        for s in arr(await client(cfg).get("/http/services", per_page=1000)):
            health = obj(s.get("serverStatus"))
            if down_only and all(v == "UP" for v in health.values()):
                continue
            if not matches(s, search, "name", "usedBy"):
                continue
            k8s = (obj((obj(s.get("observability"))).get("metadata"))).get("kubernetes")
            rows.append(
                {
                    **pick(s, "name", "type", "status", "provider", "usedBy"),
                    "servers": health
                    or {
                        x.get("url"): None for x in arr((obj(s.get("loadBalancer"))).get("servers"))
                    },
                    **({"kubernetes": k8s} if k8s else {}),
                }
            )
        return clip(rows, limit)
