"""Technitium DNS (ns1 primary + DHCP, ns2 secondary).

Talks to each node directly, never through Traefik, like
proxmox-infra/ansible/scripts/technitium_bootstrap.py. The API is form
POST; only the read paths in READ_PATHS can be called.
"""

from typing import Any, Literal

from mcp.server import MCPServer

from ..common import ApiError, HttpClient, clip, pick, tool
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
    out = []
    for n in sec.get("nodes") or []:
        token_src = n if ("token_file" in n or "token_command" in n) else sec
        out.append(
            {
                "name": n["name"],
                "url": n["url"],
                "token": cfg.secret(token_src, "token", "technitium"),
                "dhcp": bool(n.get("dhcp", False)),
            }
        )
    if not out:
        raise ConfigError("[technitium] has no nodes")
    return out


def pick_nodes(cfg: Config, node: str | None) -> list[dict[str, Any]]:
    ns = nodes(cfg)
    if node is None:
        return ns
    match = [n for n in ns if n["name"] == node]
    if not match:
        raise ConfigError(f"unknown technitium node '{node}'; known: {[n['name'] for n in ns]}")
    return match


async def call(node: dict[str, Any], path: str, **params: Any) -> dict[str, Any]:
    http = HttpClient(
        f"technitium/{node['name']}",
        node["url"],
        headers={"Authorization": f"Bearer {node['token']}"},
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
    return body.get("response") or {}


async def zone_table(cfg: Config) -> dict[str, Any]:
    per_node: dict[str, dict[str, Any]] = {}
    warnings = []
    for n in nodes(cfg):
        try:
            zones = (await call(n, "/api/zones/list")).get("zones") or []
        except ApiError as exc:
            warnings.append(str(exc))
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
    rows, mismatched = [], []
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


def register(app: MCPServer, cfg: Config) -> None:
    @tool(
        app,
        "Technitium zones on every node with SOA serials, DNSSEC and sync state. "
        "problem_zones lists zones whose serials disagree or whose secondary sync failed.",
    )
    async def dns_zones() -> dict[str, Any]:
        return await zone_table(cfg)

    @tool(
        app,
        "Records of a zone (or of one name inside it) as stored on a node. Comments show "
        "ownership: 'Via ...' (DHCP) and 'Cluster managed record' are Technitium-owned.",
    )
    async def dns_records(
        zone: str, name: str | None = None, node: str | None = None, limit: int = 500
    ) -> dict[str, Any]:
        n = pick_nodes(cfg, node)[0]
        resp = await call(
            n, "/api/zones/records/get", zone=zone, domain=name or zone, listZone=name is None
        )
        rows = [
            pick(r, "name", "type", "ttl", "rData", "disabled", "comments")
            for r in resp.get("records") or []
        ]
        return {"node": n["name"], **clip(rows, limit)}

    def dhcp_node(node: str | None) -> dict[str, Any]:
        ns = pick_nodes(cfg, node)
        return next((n for n in ns if n["dhcp"]), ns[0])

    @tool(app, "DHCP scopes on the DHCP node (ns1 by default) with ranges and enabled state.")
    async def dns_dhcp_scopes(node: str | None = None) -> dict[str, Any]:
        n = dhcp_node(node)
        return {
            "node": n["name"],
            "scopes": (await call(n, "/api/dhcp/scopes/list")).get("scopes") or [],
        }

    @tool(app, "DHCP leases. search matches hostname, IP or MAC (case-insensitive substring).")
    async def dns_dhcp_leases(
        scope: str | None = None,
        search: str | None = None,
        node: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        n = dhcp_node(node)
        rows = []
        for lease in (await call(n, "/api/dhcp/leases/list")).get("leases") or []:
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
    )
    async def dns_stats(
        period: Literal["LastHour", "LastDay", "LastWeek", "LastMonth"] = "LastHour",
        node: str | None = None,
        top: int = 10,
    ) -> dict[str, Any]:
        out = {}
        for n in pick_nodes(cfg, node):
            resp = await call(n, "/api/dashboard/stats/get", type=period, utc=True)
            out[n["name"]] = {
                "stats": resp.get("stats"),
                "top_clients": (resp.get("topClients") or [])[:top],
                "top_domains": (resp.get("topDomains") or [])[:top],
                "top_blocked": (resp.get("topBlockedDomains") or [])[:top],
            }
        return out

    @tool(
        app,
        "Query log of a node via its installed query-logger app. Filter by domain (qname), "
        "client IP, or rcode (e.g. NxDomain, ServerFailure, Refused).",
    )
    async def dns_query_log(
        node: str | None = None,
        domain: str | None = None,
        client: str | None = None,
        rcode: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        n = pick_nodes(cfg, node)[0]
        logger = None
        for a in (await call(n, "/api/apps/list")).get("apps") or []:
            for dns_app in a.get("dnsApps") or []:
                if dns_app.get("isQueryLogger"):
                    logger = (a["name"], dns_app["classPath"])
                    break
            if logger:
                break
        if logger is None:
            return {"node": n["name"], "error": "no query-logger app installed on this node"}
        resp = await call(
            n,
            "/api/logs/query",
            name=logger[0],
            classPath=logger[1],
            pageNumber=1,
            entriesPerPage=max(1, min(limit, 500)),
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
            for e in resp.get("entries") or []
        ]
        return {
            "node": n["name"],
            "logger": logger[0],
            "total": resp.get("totalEntries"),
            "entries": rows,
        }
