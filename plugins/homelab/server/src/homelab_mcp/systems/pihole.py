"""Pi-hole v6 nodes. GET only, plus the session login.

Pi-hole has no read-only role. The credential is the app password (Settings,
Web interface / API), whose sessions cannot change settings while
webserver.api.app_sudo is false; the server's own guarantee is that it only
issues GET requests besides POST /api/auth, the one allowlisted POST. One
session per node is cached and renewed on 401, so the server never piles up
sessions against webserver.api.max_sessions and never needs to log out.

The credentials file holds ``<node> <password>`` lines, or one bare
password line.
"""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, Literal, NotRequired, TypedDict
from urllib.parse import quote, urlparse

from mcp.server import MCPServer
from pydantic import Field

from .. import redact
from ..common import (
    ApiError,
    HttpClient,
    Listing,
    Session,
    arr,
    clamp,
    clip,
    gather_map,
    obj,
    pick,
    select,
    tool,
)
from ..config import Config, ConfigError

# Paths that differ between the nodes by design; config_diff ignores them.
PER_NODE_CONFIG = ("webserver.domain",)


def _password(path: Path, url: str, name: str) -> str:
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        raise ConfigError(f"pihole/{name}: cannot read {path}: {exc.strerror}") from None
    by_key: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, secret = line.partition(" ") if " " in line else ("*", "", line)
        by_key[key] = secret.strip()
    host = urlparse(url).hostname or ""
    pw = by_key.get(name) or by_key.get(host) or by_key.get("*")
    if not pw:
        raise ConfigError(f"pihole/{name}: no password for {name} in {path}")
    redact.register(pw)
    return pw


class Node:
    """One Pi-hole whose API session lives in the config cache for the process."""

    def __init__(self, cfg: Config, name: str, url: str, password: str, verify: bool) -> None:
        self.name = name
        self.password = password
        self.verify = verify
        self.base = url.rstrip("/") + "/api"
        self.session = Session(cfg.cache, ("pihole-sid", name, url), self._login)

    def _http(self, sid: str | None = None) -> HttpClient:
        return HttpClient(
            f"pihole/{self.name}",
            self.base,
            headers={"X-FTL-SID": sid} if sid else {},
            verify=self.verify,
            read_posts=("/auth",),
        )

    async def _login(self) -> str:
        resp = await self._http().post_read("/auth", json={"password": self.password})
        session = obj(resp.get("session"))
        if not session.get("valid") or not session.get("sid"):
            raise ApiError(f"pihole/{self.name}: login refused: {session.get('message')}")
        for value in (session["sid"], session.get("csrf")):
            redact.register(value)
        return session["sid"]

    async def get(self, path: str, **params: Any) -> Any:
        return await self.session.call(lambda sid: self._http(sid).get(path, **params))


def nodes(cfg: Config) -> list[Node]:
    sec = cfg.section("pihole")
    out: list[Node] = []
    for n in arr(sec.get("nodes")):
        name, url = cfg.require(n, "name", "pihole"), cfg.require(n, "url", "pihole")
        if "credentials_file" in n:
            key = ("pihole-password", name, str(n["credentials_file"]))
            if key not in cfg.cache:
                cfg.cache[key] = _password(cfg.resolve_path(n["credentials_file"]), url, name)
            pw = cfg.cache[key]
        else:
            pw = cfg.secret(n, "credentials", f"pihole/{name}")
        verify = bool(n.get("verify_tls", sec.get("verify_tls", True)))
        out.append(Node(cfg, name, url, pw, verify))
    if not out:
        raise ConfigError("[pihole] has no nodes")
    return out


def pick_nodes(cfg: Config, node: str | None) -> list[Node]:
    return select(nodes(cfg), node, lambda n: n.name, "pihole node")


async def per_node[R](ns: list[Node], fn: Callable[[Node], Awaitable[R]]) -> dict[str, R]:
    """``fn`` on every node in parallel, keyed by node name."""
    results = await gather_map(ns, fn)
    return {n.name: r for n, r in zip(ns, results, strict=True)}


def _versions(v: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for part in ("core", "web", "ftl"):
        local = obj((obj(v.get(part))).get("local"))
        out[part] = local.get("version")
    out["docker"] = (obj(v.get("docker"))).get("local")
    return out


def _flatten(config: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in config.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and "value" not in value:
            out.update(_flatten(obj(value), path + "."))
        else:
            out[path] = value
    return out


async def summary(cfg: Config) -> dict[str, Any]:
    async def one(n: Node) -> dict[str, Any]:
        version, blocking, messages = await gather_map(
            ("/info/version", "/dns/blocking", "/info/messages"), n.get
        )
        return {
            "version": _versions(obj(version.get("version")))["core"],
            "blocking": blocking.get("blocking"),
            "messages": len(arr(messages.get("messages"))),
        }

    return await per_node(nodes(cfg), one)


class QueryLog(TypedDict):
    node: str
    total: Any
    entries: list[dict[str, Any]]


class Lists(TypedDict):
    node: str
    lists: Listing
    domains: Listing


class History(TypedDict):
    node: str
    history: Listing
    clients: NotRequired[Any]


class Logs(TypedDict):
    node: str
    source: str
    next_id: Any
    lines: Any
    truncated: bool


class Settings(TypedDict):
    node: str
    count: int
    settings: dict[str, Any]


class SettingsDiff(TypedDict):
    nodes: list[str]
    differences: list[dict[str, Any]]


def register(app: MCPServer, cfg: Config) -> None:
    @tool(
        app,
        "Pi-hole status per node: blocking state, versions (core/web/FTL/docker), gravity "
        "size, list and domain counts, FTL uptime and the number of diagnosis messages.",
        title="Pi-hole status",
    )
    async def pihole_status() -> dict[str, dict[str, Any]]:
        async def one(n: Node) -> dict[str, Any]:
            version, blocking, ftl, messages = await gather_map(
                ("/info/version", "/dns/blocking", "/info/ftl", "/info/messages"), n.get
            )
            ftl = obj(ftl.get("ftl"))
            # FTL reports uptime in milliseconds.
            uptime_ms = ftl.get("uptime")
            return {
                "blocking": blocking.get("blocking"),
                "blocking_timer": blocking.get("timer"),
                "versions": _versions(obj(version.get("version"))),
                "database": ftl.get("database"),
                "uptime_s": round(uptime_ms / 1000) if uptime_ms is not None else None,
                "messages": len(arr(messages.get("messages"))),
            }

        return await per_node(nodes(cfg), one)

    @tool(
        app,
        "Pi-hole statistics per node: total/blocked/forwarded/cached queries, reply types, "
        "top queried and blocked domains, top clients and upstreams with response times.",
        title="Pi-hole statistics",
    )
    async def pihole_stats(
        node: Annotated[
            str | None, Field(description="Node name from the config; every node when absent")
        ] = None,
        top: Annotated[int, Field(description="Entries in each top list, 1-100")] = 10,
    ) -> dict[str, dict[str, Any]]:
        top = clamp(top, 1, 100)

        async def one(n: Node) -> dict[str, Any]:
            requests: tuple[tuple[str, dict[str, Any]], ...] = (
                ("/stats/summary", {}),
                ("/stats/top_domains", {"count": top}),
                ("/stats/top_domains", {"count": top, "blocked": True}),
                ("/stats/top_clients", {"count": top}),
                ("/stats/upstreams", {}),
            )
            s, queried, blocked, clients, upstreams = await gather_map(
                requests, lambda req: n.get(req[0], **req[1])
            )
            return {
                "queries": pick(
                    obj(s.get("queries")),
                    "total",
                    "blocked",
                    "percent_blocked",
                    "unique_domains",
                    "forwarded",
                    "cached",
                    "frequency",
                    "replies",
                ),
                "clients": s.get("clients"),
                "gravity": s.get("gravity"),
                "top_domains": queried.get("domains"),
                "top_blocked": blocked.get("domains"),
                "top_clients": clients.get("clients"),
                "upstreams": [
                    pick(u, "name", "ip", "port", "count", "statistics")
                    for u in arr(upstreams.get("upstreams"))
                ],
            }

        return await per_node(pick_nodes(cfg, node), one)

    @tool(
        app,
        "Pi-hole query log of one node, newest first. Filter by domain, client IP, upstream "
        "or status (e.g. GRAVITY, REGEX, DENYLIST for blocked; FORWARDED, CACHE).",
        title="Pi-hole query log",
    )
    async def pihole_query_log(
        node: Annotated[
            str | None, Field(description="Node name from the config; the first node when absent")
        ] = None,
        domain: Annotated[str | None, Field(description="Domain to match")] = None,
        client: Annotated[str | None, Field(description="Client IP address")] = None,
        upstream: Annotated[str | None, Field(description="Upstream server that answered")] = None,
        status: Annotated[
            str | None,
            Field(description="Query status, e.g. GRAVITY, REGEX, DENYLIST, FORWARDED or CACHE"),
        ] = None,
        limit: Annotated[int, Field(description="Entries to return, 1-500")] = 50,
    ) -> QueryLog:
        n = pick_nodes(cfg, node)[0]
        resp = await n.get(
            "/queries",
            domain=domain,
            client_ip=client,
            upstream=upstream,
            status=status,
            length=clamp(limit, 1, 500),
        )
        rows: list[dict[str, Any]] = []
        for q in arr(resp.get("queries")):
            reply = obj(q.get("reply"))
            c = obj(q.get("client"))
            rows.append(
                {
                    "time": q.get("time"),
                    "client": c.get("ip"),
                    "client_name": c.get("name"),
                    "domain": q.get("domain"),
                    "type": q.get("type"),
                    "status": q.get("status"),
                    "reply": reply.get("type"),
                    "reply_ms": round(reply["time"] * 1000, 1) if reply.get("time") else None,
                    "upstream": q.get("upstream"),
                    "list_id": q.get("list_id"),
                    "dnssec": q.get("dnssec"),
                }
            )
        return {"node": n.name, "total": resp.get("recordsFiltered"), "entries": rows}

    @tool(
        app,
        "Lists and domain entries of a node: adlists/allowlists (address, enabled, domain "
        "count, download status, last update) and exact/regex allow and deny entries.",
        title="Pi-hole lists",
    )
    async def pihole_lists(
        node: Annotated[
            str | None, Field(description="Node name from the config; the first node when absent")
        ] = None,
        limit: Annotated[int, Field(description="Rows to return, 1-1000")] = 200,
    ) -> Lists:
        n = pick_nodes(cfg, node)[0]
        lists, domains = await gather_map(("/lists", "/domains"), n.get)
        lists, domains = arr(lists.get("lists")), arr(domains.get("domains"))
        return {
            "node": n.name,
            "lists": clip(
                [
                    pick(
                        x,
                        "id",
                        "address",
                        "type",
                        "enabled",
                        "comment",
                        "number",
                        "invalid_domains",
                        "status",
                        "date_updated",
                    )
                    for x in lists
                ],
                limit,
            ),
            "domains": clip(
                [pick(x, "domain", "type", "kind", "enabled", "comment") for x in domains], limit
            ),
        }

    @tool(
        app,
        "Why is a domain blocked or allowed? Searches gravity lists and allow/deny entries "
        "on every node unless node is given. partial=true also matches subdomains.",
        title="Why a domain is blocked",
    )
    async def pihole_search(
        domain: Annotated[str, Field(description="Domain to look up")],
        partial: Annotated[bool, Field(description="Also match subdomains")] = False,
        node: Annotated[
            str | None, Field(description="Node name from the config; every node when absent")
        ] = None,
        limit: Annotated[int, Field(description="Maximum matches Pi-hole returns")] = 20,
    ) -> dict[str, dict[str, Any]]:
        async def one(n: Node) -> dict[str, Any]:
            path = f"/search/{quote(domain, safe='')}"
            s = obj((await n.get(path, partial=partial, N=limit)).get("search"))
            return {
                "results": s.get("results"),
                "entries": [
                    pick(x, "domain", "type", "kind", "enabled", "comment")
                    for x in arr(s.get("domains"))
                ],
                "lists": [
                    pick(x, "domain", "address", "type", "enabled", "comment")
                    for x in arr(s.get("gravity"))
                ],
            }

        return await per_node(pick_nodes(cfg, node), one)

    @tool(
        app,
        "Pi-hole diagnosis messages per node: list download failures, rate-limited clients, "
        "DNSSEC or upstream problems, load and disk warnings.",
        title="Pi-hole messages",
    )
    async def pihole_messages() -> dict[str, list[dict[str, Any]]]:
        async def one(n: Node) -> list[dict[str, Any]]:
            msgs = arr((await n.get("/info/messages")).get("messages"))
            return [pick(m, "id", "timestamp", "type", "plain", "html") for m in msgs]

        return await per_node(nodes(cfg), one)

    @tool(
        app,
        "Query history of one node over the last 24 h in 10-minute slots (total, blocked, "
        "cached, forwarded). per_client=true returns the per-client series instead.",
        title="Pi-hole query history",
    )
    async def pihole_history(
        node: Annotated[
            str | None, Field(description="Node name from the config; the first node when absent")
        ] = None,
        per_client: Annotated[bool, Field(description="Split each slot by client")] = False,
        limit: Annotated[int, Field(description="Ten-minute slots to return, 1-1000")] = 150,
    ) -> History:
        n = pick_nodes(cfg, node)[0]
        if per_client:
            h = await n.get("/history/clients")
            return {
                "node": n.name,
                "clients": h.get("clients"),
                "history": clip(arr(h.get("history")), limit),
            }
        h = await n.get("/history")
        return {"node": n.name, "history": clip(arr(h.get("history")), limit)}

    @tool(
        app,
        "Tail of a Pi-hole log: ftl (FTL warnings), dnsmasq (per-query log) or webserver. "
        "Pass the returned next_id to read only newer lines.",
        title="Pi-hole logs",
    )
    async def pihole_logs(
        node: Annotated[
            str | None, Field(description="Node name from the config; the first node when absent")
        ] = None,
        source: Annotated[
            Literal["ftl", "dnsmasq", "webserver"],
            Field(description="ftl warnings, the dnsmasq per-query log or the webserver log"),
        ] = "ftl",
        next_id: Annotated[
            int | None,
            Field(description="next_id from the previous call, to read only newer lines"),
        ] = None,
        limit: Annotated[int, Field(description="Rows to return, 1-1000")] = 100,
    ) -> Logs:
        n = pick_nodes(cfg, node)[0]
        resp = await n.get(f"/logs/{source}", nextID=next_id)
        lines = [pick(x, "timestamp", "prio", "message") for x in arr(resp.get("log"))]
        limit = clamp(limit, 1, 1000)
        return {
            "node": n.name,
            "source": source,
            "next_id": resp.get("nextID"),
            "lines": lines[-limit:],
            "truncated": len(lines) > limit,
        }

    @tool(
        app,
        "Pi-hole settings of one node. By default only the keys that differ from Pi-hole's "
        "defaults, with whether each is pinned by an FTLCONF_ variable. prefix narrows to a "
        "section (e.g. dns, webserver.api).",
        title="Pi-hole settings",
    )
    async def pihole_config(
        node: Annotated[
            str | None, Field(description="Node name from the config; the first node when absent")
        ] = None,
        modified_only: Annotated[
            bool, Field(description="Only settings that differ from Pi-hole's defaults")
        ] = True,
        prefix: Annotated[
            str | None, Field(description="Only keys in this section, e.g. dns or webserver.api")
        ] = None,
    ) -> Settings:
        n = pick_nodes(cfg, node)[0]
        leaves = _flatten(obj((await n.get("/config", detailed=True)).get("config")))
        out: dict[str, Any] = {}
        for path, leaf in sorted(leaves.items()):
            if prefix and not (path == prefix or path.startswith(prefix + ".")):
                continue
            if modified_only and not leaf.get("modified"):
                continue
            out[path] = {
                "value": leaf.get("value"),
                "default": leaf.get("default"),
                "env_var": (obj(leaf.get("flags"))).get("env_var"),
            }
        return {"node": n.name, "count": len(out), "settings": out}

    @tool(
        app,
        "Settings that differ between the Pi-hole nodes (webserver.domain, which is per "
        "node by design, is ignored). Empty differences means the pair is consistent.",
        title="Pi-hole settings drift",
    )
    async def pihole_config_diff() -> SettingsDiff:
        async def one(n: Node) -> dict[str, Any]:
            return _flatten(obj((await n.get("/config")).get("config")))

        per = await per_node(nodes(cfg), one)
        names = sorted(per)
        differences: list[dict[str, Any]] = []
        for path in sorted(set[str]().union(*(v.keys() for v in per.values()))):
            if path in PER_NODE_CONFIG:
                continue
            values = {name: per[name].get(path) for name in names}
            if len({repr(v) for v in values.values()}) > 1:
                differences.append({"path": path, "values": values})
        return {"nodes": names, "differences": differences}
