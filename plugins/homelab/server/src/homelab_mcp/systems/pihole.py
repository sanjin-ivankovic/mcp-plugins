"""Pi-hole v6 pair (pihole-1/pihole-2). GET only, plus the session login.

Pi-hole has no read-only role. The credential is the app password (Settings,
Web interface / API), whose sessions cannot change settings while
webserver.api.app_sudo is false; the server's own guarantee is that it only
issues GET requests besides POST /api/auth, the one allowlisted POST. One
session per node is cached and renewed on 401, so the server never piles up
sessions against webserver.api.max_sessions and never needs to log out.

The credentials file holds ``<node> <password>`` lines, or one bare
password line.
"""

import asyncio
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlparse

from mcp.server import MCPServer

from .. import redact
from ..common import ApiError, HttpClient, clip, pick, tool
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
    """One Pi-hole with a cached API session."""

    _sessions: dict[str, str] = {}
    _locks: dict[str, asyncio.Lock] = {}

    def __init__(self, name: str, url: str, password: str) -> None:
        self.name = name
        self.key = f"{name} {url}"
        self.password = password
        self.http = HttpClient(
            f"pihole/{name}", url.rstrip("/") + "/api", headers={}, read_posts=("/auth",)
        )

    async def _login(self) -> None:
        resp = await self.http.post_read("/auth", json={"password": self.password})
        session = resp.get("session") or {}
        if not session.get("valid") or not session.get("sid"):
            raise ApiError(f"pihole/{self.name}: login refused: {session.get('message')}")
        for value in (session["sid"], session.get("csrf")):
            redact.register(value)
        Node._sessions[self.key] = session["sid"]

    async def get(self, path: str, **params: Any) -> Any:
        lock = Node._locks.setdefault(self.key, asyncio.Lock())
        for attempt in (1, 2):
            async with lock:
                if self.key not in Node._sessions:
                    await self._login()
                self.http.headers["X-FTL-SID"] = Node._sessions[self.key]
            try:
                return await self.http.get(path, **params)
            except ApiError as exc:
                if exc.status != 401 or attempt == 2:
                    raise
                Node._sessions.pop(self.key, None)
        raise AssertionError("unreachable")


def nodes(cfg: Config) -> list[Node]:
    sec = cfg.section("pihole")
    out = []
    for n in sec.get("nodes") or []:
        if "credentials_file" in n:
            pw = _password(cfg.resolve_path(n["credentials_file"]), n["url"], n["name"])
        else:
            pw = cfg.secret(n, "credentials", f"pihole/{n['name']}")
        out.append(Node(n["name"], n["url"], pw))
    if not out:
        raise ConfigError("[pihole] has no nodes")
    return out


def pick_nodes(cfg: Config, node: str | None) -> list[Node]:
    ns = nodes(cfg)
    if node is None:
        return ns
    match = [n for n in ns if n.name == node]
    if not match:
        raise ConfigError(f"unknown pihole node '{node}'; known: {[n.name for n in ns]}")
    return match


def _versions(v: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for part in ("core", "web", "ftl"):
        local = (v.get(part) or {}).get("local") or {}
        out[part] = local.get("version")
    out["docker"] = (v.get("docker") or {}).get("local")
    return out


def _flatten(config: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in config.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and "value" not in value:
            out.update(_flatten(value, path + "."))
        else:
            out[path] = value
    return out


async def summary(cfg: Config) -> dict[str, Any]:
    out = {}
    for n in nodes(cfg):
        version = await n.get("/info/version")
        blocking = await n.get("/dns/blocking")
        messages = await n.get("/info/messages")
        out[n.name] = {
            "version": _versions(version.get("version") or {})["core"],
            "blocking": blocking.get("blocking"),
            "messages": len(messages.get("messages") or []),
        }
    return out


def register(app: MCPServer, cfg: Config) -> None:
    @tool(
        app,
        "Pi-hole status per node: blocking state, versions (core/web/FTL/docker), gravity "
        "size, list and domain counts, FTL uptime and the number of diagnosis messages.",
    )
    async def pihole_status() -> dict[str, Any]:
        out = {}
        for n in nodes(cfg):
            version = await n.get("/info/version")
            blocking = await n.get("/dns/blocking")
            ftl = (await n.get("/info/ftl")).get("ftl") or {}
            messages = await n.get("/info/messages")
            # FTL reports uptime in milliseconds.
            uptime_ms = ftl.get("uptime")
            out[n.name] = {
                "blocking": blocking.get("blocking"),
                "blocking_timer": blocking.get("timer"),
                "versions": _versions(version.get("version") or {}),
                "database": ftl.get("database"),
                "uptime_s": round(uptime_ms / 1000) if uptime_ms is not None else None,
                "messages": len(messages.get("messages") or []),
            }
        return out

    @tool(
        app,
        "Pi-hole statistics per node: total/blocked/forwarded/cached queries, reply types, "
        "top queried and blocked domains, top clients and upstreams with response times.",
    )
    async def pihole_stats(node: str | None = None, top: int = 10) -> dict[str, Any]:
        top = max(1, min(top, 100))
        out = {}
        for n in pick_nodes(cfg, node):
            s = await n.get("/stats/summary")
            queried = await n.get("/stats/top_domains", count=top)
            blocked = await n.get("/stats/top_domains", count=top, blocked=True)
            clients = await n.get("/stats/top_clients", count=top)
            upstreams = await n.get("/stats/upstreams")
            out[n.name] = {
                "queries": pick(
                    s.get("queries") or {},
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
                    for u in upstreams.get("upstreams") or []
                ],
            }
        return out

    @tool(
        app,
        "Pi-hole query log of one node, newest first. Filter by domain, client IP, upstream "
        "or status (e.g. GRAVITY, REGEX, DENYLIST for blocked; FORWARDED, CACHE).",
    )
    async def pihole_query_log(
        node: str | None = None,
        domain: str | None = None,
        client: str | None = None,
        upstream: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        n = pick_nodes(cfg, node)[0]
        resp = await n.get(
            "/queries",
            domain=domain,
            client_ip=client,
            upstream=upstream,
            status=status,
            length=max(1, min(limit, 500)),
        )
        rows = []
        for q in resp.get("queries") or []:
            reply = q.get("reply") or {}
            c = q.get("client") or {}
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
    )
    async def pihole_lists(node: str | None = None, limit: int = 200) -> dict[str, Any]:
        n = pick_nodes(cfg, node)[0]
        lists = (await n.get("/lists")).get("lists") or []
        domains = (await n.get("/domains")).get("domains") or []
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
    )
    async def pihole_search(
        domain: str, partial: bool = False, node: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        out = {}
        for n in pick_nodes(cfg, node):
            s = (await n.get(f"/search/{quote(domain, safe='')}", partial=partial, N=limit)).get(
                "search"
            ) or {}
            out[n.name] = {
                "results": s.get("results"),
                "entries": [
                    pick(x, "domain", "type", "kind", "enabled", "comment")
                    for x in s.get("domains") or []
                ],
                "lists": [
                    pick(x, "domain", "address", "type", "enabled", "comment")
                    for x in s.get("gravity") or []
                ],
            }
        return out

    @tool(
        app,
        "Pi-hole diagnosis messages per node: list download failures, rate-limited clients, "
        "DNSSEC or upstream problems, load and disk warnings.",
    )
    async def pihole_messages() -> dict[str, Any]:
        out = {}
        for n in nodes(cfg):
            msgs = (await n.get("/info/messages")).get("messages") or []
            out[n.name] = [pick(m, "id", "timestamp", "type", "plain", "html") for m in msgs]
        return out

    @tool(
        app,
        "Query history of one node over the last 24 h in 10-minute slots (total, blocked, "
        "cached, forwarded). per_client=true returns the per-client series instead.",
    )
    async def pihole_history(
        node: str | None = None, per_client: bool = False, limit: int = 150
    ) -> dict[str, Any]:
        n = pick_nodes(cfg, node)[0]
        if per_client:
            h = await n.get("/history/clients")
            return {
                "node": n.name,
                "clients": h.get("clients"),
                "history": clip(h.get("history") or [], limit),
            }
        h = await n.get("/history")
        return {"node": n.name, "history": clip(h.get("history") or [], limit)}

    @tool(
        app,
        "Tail of a Pi-hole log: ftl (FTL warnings), dnsmasq (per-query log) or webserver. "
        "Pass the returned next_id to read only newer lines.",
    )
    async def pihole_logs(
        node: str | None = None,
        source: Literal["ftl", "dnsmasq", "webserver"] = "ftl",
        next_id: int | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        n = pick_nodes(cfg, node)[0]
        resp = await n.get(f"/logs/{source}", nextID=next_id)
        lines = [pick(x, "timestamp", "prio", "message") for x in resp.get("log") or []]
        limit = max(1, min(limit, 1000))
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
    )
    async def pihole_config(
        node: str | None = None, modified_only: bool = True, prefix: str | None = None
    ) -> dict[str, Any]:
        n = pick_nodes(cfg, node)[0]
        leaves = _flatten((await n.get("/config", detailed=True)).get("config") or {})
        out = {}
        for path, leaf in sorted(leaves.items()):
            if prefix and not (path == prefix or path.startswith(prefix + ".")):
                continue
            if modified_only and not leaf.get("modified"):
                continue
            out[path] = {
                "value": leaf.get("value"),
                "default": leaf.get("default"),
                "env_var": (leaf.get("flags") or {}).get("env_var"),
            }
        return {"node": n.name, "count": len(out), "settings": out}

    @tool(
        app,
        "Settings that differ between the Pi-hole nodes (webserver.domain, which is per "
        "node by design, is ignored). Empty differences means the pair is consistent.",
    )
    async def pihole_config_diff() -> dict[str, Any]:
        per: dict[str, dict[str, Any]] = {}
        for n in nodes(cfg):
            per[n.name] = _flatten((await n.get("/config")).get("config") or {})
        names = sorted(per)
        differences = []
        for path in sorted(set().union(*(v.keys() for v in per.values()))):
            if path in PER_NODE_CONFIG:
                continue
            values = {name: per[name].get(path) for name in names}
            if len({repr(v) for v in values.values()}) > 1:
                differences.append({"path": path, "values": values})
        return {"nodes": names, "differences": differences}
