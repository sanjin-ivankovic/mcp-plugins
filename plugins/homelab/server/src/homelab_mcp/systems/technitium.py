"""Technitium DNS (a primary that also serves DHCP, plus secondaries).

Talks to each node directly, never through Traefik, like
proxmox-infra/ansible/scripts/technitium_bootstrap.py. The API is form
POST; only the read paths in READ_PATHS can be called.
"""

from typing import Annotated, Any, Literal, TypedDict, cast

from mcp.server import MCPServer
from pydantic import Field

from ..common import ApiError, HttpClient, arr, clamp, clip, gather_map, obj, pick, select, tool
from ..config import Config, ConfigError

READ_PATHS = (
    "/api/zones/list",
    "/api/zones/records/get",
    "/api/dhcp/scopes/list",
    "/api/dhcp/leases/list",
    "/api/dashboard/stats/get",
    "/api/apps/list",
    "/api/logs/query",
)


def nodes(cfg: Config) -> list[dict[str, Any]]:
    sec = cfg.section("technitium")
    out: list[dict[str, Any]] = []
    for n in arr(sec.get("nodes")):
        token_src = n if ("token_file" in n or "token_command" in n) else sec
        out.append(
            {
                "name": cfg.require(n, "name", "technitium"),
                "url": cfg.require(n, "url", "technitium"),
                "token": cfg.secret(token_src, "token", "technitium"),
                "dhcp": bool(n.get("dhcp", False)),
                "verify": bool(n.get("verify_tls", sec.get("verify_tls", True))),
            }
        )
    if not out:
        raise ConfigError("[technitium] has no nodes")
    return out


def pick_nodes(cfg: Config, node: str | None) -> list[dict[str, Any]]:
    return select(nodes(cfg), node, lambda n: n["name"], "technitium node")


async def call(node: dict[str, Any], path: str, **params: Any) -> dict[str, Any]:
    http = HttpClient(
        f"technitium/{node['name']}",
        node["url"],
        headers={"Authorization": f"Bearer {node['token']}"},
        verify=node["verify"],
        read_posts=READ_PATHS,
    )
    form = {
        k: (str(v).lower() if isinstance(v, bool) else str(v))
        for k, v in params.items()
        if v is not None
    }
    body = await http.post_read(path, data=form)
    if body.get("status") != "ok":
        raise ApiError(
            f"technitium/{node['name']} {path} -> {body.get('status')}: "
            f"{body.get('errorMessage', '')}"
        )
    return obj(body.get("response"))


async def zone_table(cfg: Config) -> dict[str, Any]:
    per_node: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []

    async def listing(n: dict[str, Any]) -> tuple[dict[str, Any], Any]:
        try:
            return n, arr((await call(n, "/api/zones/list")).get("zones"))
        except ApiError as exc:
            return n, exc

    for n, zones in await gather_map(nodes(cfg), listing):
        if isinstance(zones, ApiError):
            warnings.append(str(zones))
            continue
        for z in zones:
            per_node.setdefault(z["name"], {})[n["name"]] = pick(
                z,
                "type",
                "soaSerial",
                "disabled",
                "dnssecStatus",
                "syncFailed",
                "isExpired",
                "internal",
            )
    rows: list[dict[str, Any]] = []
    mismatched: list[str] = []
    for name, by_node in sorted(per_node.items()):
        if any(v.get("internal") for v in by_node.values()):
            continue
        serials = {v.get("soaSerial") for v in by_node.values() if v.get("soaSerial") is not None}
        row = {"zone": name, "nodes": by_node, "serials_agree": len(serials) <= 1}
        if len(serials) > 1 or any(
            v.get("syncFailed") or v.get("isExpired") for v in by_node.values()
        ):
            mismatched.append(name)
        rows.append(row)
    return {"zones": rows, "problem_zones": mismatched, "warnings": warnings}


async def summary(cfg: Config) -> dict[str, Any]:
    t = await zone_table(cfg)
    return {
        "zones": len(t["zones"]),
        "problem_zones": t["problem_zones"],
        "warnings": t["warnings"],
    }


class Zones(TypedDict):
    zones: list[dict[str, Any]]
    problem_zones: list[str]
    warnings: list[str]


class Records(TypedDict):
    node: str
    count: int
    truncated: bool
    items: list[Any]


class Scopes(TypedDict):
    node: str
    scopes: list[dict[str, Any]]


class Leases(TypedDict):
    node: str
    count: int
    truncated: bool
    items: list[Any]


class DnsQueryLog(TypedDict):
    node: str
    logger: Any
    total: Any
    entries: list[dict[str, Any]]


def register(app: MCPServer, cfg: Config) -> None:
    @tool(
        app,
        "Technitium zones on every node with SOA serials, DNSSEC and sync state. "
        "problem_zones lists zones whose serials disagree or whose secondary sync failed.",
        title="DNS zones",
    )
    async def technitium_zones() -> Zones:
        return cast(Zones, await zone_table(cfg))

    @tool(
        app,
        "Records of a zone (or of one name inside it) as stored on a node. Comments show "
        "ownership: 'Via ...' (DHCP) and 'Cluster managed record' are Technitium-owned.",
        title="DNS records",
    )
    async def technitium_records(
        zone: Annotated[str, Field(description="Zone name, e.g. example.test")],
        name: Annotated[
            str | None, Field(description="One name inside the zone; the whole zone when absent")
        ] = None,
        node: Annotated[
            str | None, Field(description="Node name from the config; the first node when absent")
        ] = None,
        limit: Annotated[int, Field(description="Rows to return, 1-1000")] = 500,
    ) -> Records:
        n = pick_nodes(cfg, node)[0]
        resp = await call(
            n, "/api/zones/records/get", zone=zone, domain=name or zone, listZone=name is None
        )
        rows = [
            pick(r, "name", "type", "ttl", "rData", "disabled", "comments")
            for r in arr(resp.get("records"))
        ]
        return {"node": n["name"], **clip(rows, limit)}

    def dhcp_node(node: str | None) -> dict[str, Any]:
        ns = pick_nodes(cfg, node)
        return next((n for n in ns if n["dhcp"]), ns[0])

    @tool(
        app,
        "DHCP scopes on the DHCP node (the one marked dhcp) with ranges and enabled state.",
        title="DHCP scopes",
    )
    async def technitium_dhcp_scopes(
        node: Annotated[
            str | None,
            Field(description="Node name from the config; the node marked dhcp when absent"),
        ] = None,
    ) -> Scopes:
        n = dhcp_node(node)
        return {
            "node": n["name"],
            "scopes": arr((await call(n, "/api/dhcp/scopes/list")).get("scopes")),
        }

    @tool(
        app,
        "DHCP leases. search matches hostname, IP or MAC (case-insensitive substring).",
        title="DHCP leases",
    )
    async def technitium_dhcp_leases(
        scope: Annotated[
            str | None, Field(description="Scope name; every scope when absent")
        ] = None,
        search: Annotated[
            str | None, Field(description="Case-insensitive substring of hostname, IP or MAC")
        ] = None,
        node: Annotated[
            str | None,
            Field(description="Node name from the config; the node marked dhcp when absent"),
        ] = None,
        limit: Annotated[int, Field(description="Rows to return, 1-1000")] = 200,
    ) -> Leases:
        n = dhcp_node(node)
        rows: list[dict[str, Any]] = []
        for lease in arr((await call(n, "/api/dhcp/leases/list")).get("leases")):
            if scope and lease.get("scope") != scope:
                continue
            hay = " ".join(
                str(lease.get(k, "")) for k in ("hostName", "address", "hardwareAddress")
            ).lower()
            if search and search.lower() not in hay:
                continue
            rows.append(
                pick(
                    lease,
                    "scope",
                    "type",
                    "address",
                    "hostName",
                    "hardwareAddress",
                    "leaseObtained",
                    "leaseExpires",
                )
            )
        return {"node": n["name"], **clip(rows, limit)}

    @tool(
        app,
        "Dashboard statistics per node: query totals, NXDOMAIN/SERVFAIL counts, top clients "
        "and domains for the period.",
        title="DNS statistics",
    )
    async def technitium_stats(
        period: Annotated[
            Literal["LastHour", "LastDay", "LastWeek", "LastMonth"],
            Field(description="Time window of the statistics"),
        ] = "LastHour",
        node: Annotated[
            str | None, Field(description="Node name from the config; every node when absent")
        ] = None,
        top: Annotated[int, Field(description="Entries in each top list")] = 10,
    ) -> dict[str, dict[str, Any]]:
        async def one(n: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            resp = await call(n, "/api/dashboard/stats/get", type=period, utc=True)
            return n["name"], {
                "stats": resp.get("stats"),
                "top_clients": (arr(resp.get("topClients")))[:top],
                "top_domains": (arr(resp.get("topDomains")))[:top],
                "top_blocked": (arr(resp.get("topBlockedDomains")))[:top],
            }

        return dict(await gather_map(pick_nodes(cfg, node), one))

    @tool(
        app,
        "Query log of a node via its installed query-logger app. Filter by domain (qname), "
        "client IP, or rcode (e.g. NxDomain, ServerFailure, Refused).",
        title="DNS query log",
    )
    async def technitium_query_log(
        node: Annotated[
            str | None, Field(description="Node name from the config; the first node when absent")
        ] = None,
        domain: Annotated[str | None, Field(description="Query name to match")] = None,
        client: Annotated[str | None, Field(description="Client IP address")] = None,
        rcode: Annotated[
            str | None, Field(description="Response code, e.g. NxDomain, ServerFailure or Refused")
        ] = None,
        limit: Annotated[int, Field(description="Entries to return, 1-500")] = 50,
    ) -> DnsQueryLog:
        n = pick_nodes(cfg, node)[0]
        logger = None
        for a in arr((await call(n, "/api/apps/list")).get("apps")):
            for dns_app in arr(a.get("dnsApps")):
                if dns_app.get("isQueryLogger"):
                    logger = (a["name"], dns_app["classPath"])
                    break
            if logger:
                break
        if logger is None:
            raise ApiError(f"technitium/{n['name']}: no query-logger app installed")
        resp = await call(
            n,
            "/api/logs/query",
            name=logger[0],
            classPath=logger[1],
            pageNumber=1,
            entriesPerPage=clamp(limit, 1, 500),
            descendingOrder=True,
            qname=domain,
            clientIpAddress=client,
            rcode=rcode,
        )
        rows = [
            pick(
                e,
                "timestamp",
                "clientIpAddress",
                "protocol",
                "responseType",
                "rcode",
                "qname",
                "qtype",
                "answer",
            )
            for e in arr(resp.get("entries"))
        ]
        return {
            "node": n["name"],
            "logger": logger[0],
            "total": resp.get("totalEntries"),
            "entries": rows,
        }
