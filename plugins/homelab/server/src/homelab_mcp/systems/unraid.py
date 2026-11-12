"""Unraid GraphQL API (API key with the VIEWER role).

Only the fixed query documents below are ever sent. No document is built
from tool input, so a mutation cannot be expressed through this module.
"""

from typing import Any

from mcp.server import MCPServer

from ..common import ApiError, HttpClient, clip, pct, tool
from ..config import Config

Q_SYSTEM = """query SystemInfo {
  info {
    time
    os { platform distro release kernel hostname uptime }
    versions { core { unraid api kernel } }
    cpu { manufacturer brand cores threads }
  }
}"""

Q_METRICS = """query SystemMetrics {
  metrics { cpu { percentTotal } memory { total used available percentTotal } }
}"""

Q_ARRAY = """query ArrayStatus {
  array {
    state
    capacity { kilobytes { free used total } }
    parities { idx name status temp fsFree fsSize }
    disks { idx name status temp fsFree fsSize }
    caches { idx name status temp fsFree fsSize }
  }
}"""

Q_DOCKER = """query DockerContainers {
  docker { containers { id names state image autoStart } }
}"""

Q_VMS = """query VmDomains { vms { domains { id name state } } }"""

Q_SHARES = """query Shares { shares { id name free used size } }"""

Q_NOTIFICATIONS = """query Notifications($limit: Int!) {
  notifications {
    overview { unread { info warning alert total } }
    list(filter: { type: UNREAD, offset: 0, limit: $limit }) {
      id title subject importance timestamp
    }
  }
}"""

QUERIES = (Q_SYSTEM, Q_METRICS, Q_ARRAY, Q_DOCKER, Q_VMS, Q_SHARES, Q_NOTIFICATIONS)
assert all(q.lstrip().startswith("query ") for q in QUERIES)  # noqa: S101 - import-time invariant


async def query(
    cfg: Config, document: str, variables: dict[str, Any] | None = None
) -> dict[str, Any]:
    if document not in QUERIES:
        raise ApiError("unraid: refusing a query document that is not built in")
    sec = cfg.section("unraid")
    key = cfg.secret(sec, "api_key", "unraid")
    url = sec["url"].rstrip("/")
    base, _, path = url.rpartition("/")
    http = HttpClient(
        "unraid",
        base,
        headers={"x-api-key": key},
        verify=bool(sec.get("verify_tls", True)),
        read_posts={"/" + path},
    )
    body = await http.post_read("/" + path, json={"query": document, "variables": variables or {}})
    errors = [e.get("message", str(e)) for e in body.get("errors") or []]
    if errors and not body.get("data"):
        raise ApiError("unraid: " + "; ".join(errors))
    result = body.get("data") or {}
    if errors:
        result["warnings"] = errors
    return result


def _disk(d: dict[str, Any]) -> dict[str, Any]:
    used = (d.get("fsSize") or 0) - (d.get("fsFree") or 0) if d.get("fsSize") else None
    return {
        "name": d.get("name"),
        "status": d.get("status"),
        "temp_c": d.get("temp"),
        "used_pct": pct(used, d.get("fsSize")),
    }


async def summary(cfg: Config) -> dict[str, Any]:
    array = (await query(cfg, Q_ARRAY)).get("array") or {}
    notes = (await query(cfg, Q_NOTIFICATIONS, {"limit": 1})).get("notifications") or {}
    unread = ((notes.get("overview") or {}).get("unread")) or {}
    bad = [
        d["name"]
        for d in (array.get("disks") or []) + (array.get("parities") or [])
        if d.get("status") not in (None, "DISK_OK")
    ]
    return {
        "array_state": array.get("state"),
        "disks_not_ok": bad,
        "unread_alerts": unread.get("alert"),
        "unread_warnings": unread.get("warning"),
    }


def register(app: MCPServer, cfg: Config) -> None:
    @tool(app, "Unraid host: OS and Unraid/API versions, CPU, uptime, current CPU and memory use.")
    async def unraid_system() -> dict[str, Any]:
        out = await query(cfg, Q_SYSTEM)
        try:
            out.update(await query(cfg, Q_METRICS))
        except ApiError as exc:
            out.setdefault("warnings", []).append(f"metrics unavailable: {exc}")
        return out

    @tool(
        app,
        "Unraid array: state, capacity, and per-disk status, temperature and fill level "
        "for parity, data and cache disks.",
    )
    async def unraid_array() -> dict[str, Any]:
        array = (await query(cfg, Q_ARRAY)).get("array") or {}
        kb = (array.get("capacity") or {}).get("kilobytes") or {}
        return {
            "state": array.get("state"),
            "capacity_used_pct": pct(kb.get("used"), kb.get("total")),
            "parities": [_disk(d) for d in array.get("parities") or []],
            "disks": [_disk(d) for d in array.get("disks") or []],
            "caches": [_disk(d) for d in array.get("caches") or []],
        }

    @tool(
        app,
        "Docker containers on Unraid with state, image and autostart. "
        "state filters, e.g. RUNNING or EXITED.",
    )
    async def unraid_docker(state: str | None = None, limit: int = 200) -> dict[str, Any]:
        rows = ((await query(cfg, Q_DOCKER)).get("docker") or {}).get("containers") or []
        rows = [
            {
                "name": (c.get("names") or ["?"])[0].lstrip("/"),
                "state": c.get("state"),
                "image": c.get("image"),
                "autostart": c.get("autoStart"),
            }
            for c in rows
            if state is None or str(c.get("state", "")).upper() == state.upper()
        ]
        return clip(sorted(rows, key=lambda r: r["name"]), limit)

    @tool(app, "Virtual machines defined on Unraid and their state.")
    async def unraid_vms() -> dict[str, Any]:
        return {"domains": ((await query(cfg, Q_VMS)).get("vms") or {}).get("domains") or []}

    @tool(app, "Unraid user shares with size, used and free space.")
    async def unraid_shares() -> dict[str, Any]:
        return {"shares": (await query(cfg, Q_SHARES)).get("shares") or []}

    @tool(app, "Unread Unraid notifications (alerts, warnings, info) with counts.")
    async def unraid_notifications(limit: int = 20) -> dict[str, Any]:
        n = (await query(cfg, Q_NOTIFICATIONS, {"limit": max(1, min(limit, 200))})).get(
            "notifications"
        ) or {}
        return {"unread": (n.get("overview") or {}).get("unread"), "items": n.get("list") or []}
