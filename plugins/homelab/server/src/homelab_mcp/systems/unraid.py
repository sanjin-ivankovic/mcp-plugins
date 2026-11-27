"""Unraid GraphQL API (API key with the VIEWER role).

Only the fixed query documents below are ever sent. No document is built
from tool input, so a mutation cannot be expressed through this module.
"""

from typing import Annotated, Any, NotRequired, TypedDict, cast

from mcp.server import MCPServer
from pydantic import Field

from ..common import ApiError, Listing, arr, clamp, client_from, clip, gather_map, obj, pct, tool
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
    base, _, path = cfg.require(sec, "url", "unraid").rstrip("/").rpartition("/")
    http = client_from(cfg, "unraid", url=base, headers={"x-api-key": key}, read_posts={"/" + path})
    body = await http.post_read("/" + path, json={"query": document, "variables": obj(variables)})
    errors = [e.get("message", str(e)) for e in arr(body.get("errors"))]
    if errors and not body.get("data"):
        raise ApiError("unraid: " + "; ".join(errors))
    result = obj(body.get("data"))
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
    array, notes = await gather_map(
        ((Q_ARRAY, None), (Q_NOTIFICATIONS, {"limit": 1})), lambda q: query(cfg, *q)
    )
    array = obj(array.get("array"))
    notes = obj(notes.get("notifications"))
    unread = obj((obj(notes.get("overview"))).get("unread"))
    bad = [
        d["name"]
        for d in (arr(array.get("disks"))) + (arr(array.get("parities")))
        if d.get("status") not in (None, "DISK_OK")
    ]
    return {
        "array_state": array.get("state"),
        "disks_not_ok": bad,
        "unread_alerts": unread.get("alert"),
        "unread_warnings": unread.get("warning"),
    }


class System(TypedDict):
    info: Any
    metrics: NotRequired[Any]
    warnings: NotRequired[list[str]]


class Array(TypedDict):
    state: Any
    capacity_used_pct: float | None
    parities: list[dict[str, Any]]
    disks: list[dict[str, Any]]
    caches: list[dict[str, Any]]


class Vms(TypedDict):
    domains: list[dict[str, Any]]


class Shares(TypedDict):
    shares: list[dict[str, Any]]


class Notifications(TypedDict):
    unread: Any
    items: list[Any]


def register(app: MCPServer, cfg: Config) -> None:
    @tool(
        app,
        "Unraid host: OS and Unraid/API versions, CPU, uptime, current CPU and memory use.",
        title="Unraid system",
    )
    async def unraid_system() -> System:
        out = await query(cfg, Q_SYSTEM)
        try:
            out.update(await query(cfg, Q_METRICS))
        except ApiError as exc:
            out.setdefault("warnings", []).append(f"metrics unavailable: {exc}")
        return cast(System, out)

    @tool(
        app,
        "Unraid array: state, capacity, and per-disk status, temperature and fill level "
        "for parity, data and cache disks.",
        title="Unraid array",
    )
    async def unraid_array() -> Array:
        array = obj((await query(cfg, Q_ARRAY)).get("array"))
        kb = obj((obj(array.get("capacity"))).get("kilobytes"))
        return {
            "state": array.get("state"),
            "capacity_used_pct": pct(kb.get("used"), kb.get("total")),
            "parities": [_disk(d) for d in arr(array.get("parities"))],
            "disks": [_disk(d) for d in arr(array.get("disks"))],
            "caches": [_disk(d) for d in arr(array.get("caches"))],
        }

    @tool(
        app,
        "Docker containers on Unraid with state, image and autostart. "
        "state filters, e.g. RUNNING or EXITED.",
        title="Unraid containers",
    )
    async def unraid_docker(
        state: Annotated[
            str | None, Field(description="Container state, e.g. RUNNING or EXITED")
        ] = None,
        limit: Annotated[int, Field(description="Rows to return, 1-1000")] = 200,
    ) -> Listing:
        rows = arr((obj((await query(cfg, Q_DOCKER)).get("docker"))).get("containers"))
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

    @tool(app, "Virtual machines defined on Unraid and their state.", title="Unraid VMs")
    async def unraid_vms() -> Vms:
        return {"domains": arr((obj((await query(cfg, Q_VMS)).get("vms"))).get("domains"))}

    @tool(app, "Unraid user shares with size, used and free space.", title="Unraid shares")
    async def unraid_shares() -> Shares:
        return {"shares": arr((await query(cfg, Q_SHARES)).get("shares"))}

    @tool(
        app,
        "Unread Unraid notifications (alerts, warnings, info) with counts.",
        title="Unraid notifications",
    )
    async def unraid_notifications(
        limit: Annotated[int, Field(description="Notifications to return, 1-200")] = 20,
    ) -> Notifications:
        n = obj(
            (await query(cfg, Q_NOTIFICATIONS, {"limit": clamp(limit, 1, 200)})).get(
                "notifications"
            )
        )
        return {"unread": (obj(n.get("overview"))).get("unread"), "items": arr(n.get("list"))}
