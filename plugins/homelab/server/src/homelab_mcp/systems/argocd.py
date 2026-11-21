"""ArgoCD REST (token of the `mcp` account, role:readonly).

Used by the drift report and the overview. Rich ArgoCD tooling comes from
the official argocd-mcp server that the plugin also starts.
"""

from typing import Any

from ..common import arr, client_from, obj
from ..config import Config

HEALTHY = {"Healthy"}
SYNCED = {"Synced"}


async def applications(cfg: Config) -> list[dict[str, Any]]:
    token = cfg.secret(cfg.section("argocd"), "token", "argocd")
    http = client_from(cfg, "argocd", headers={"Authorization": f"Bearer {token}"})
    out: list[dict[str, Any]] = []
    for a in arr((await http.get("/api/v1/applications")).get("items")):
        st = obj(a.get("status"))
        op = obj(st.get("operationState"))
        out.append(
            {
                "name": (obj(a.get("metadata"))).get("name"),
                "sync": (obj(st.get("sync"))).get("status"),
                "health": (obj(st.get("health"))).get("status"),
                "operation": op.get("phase"),
                "operation_message": (op.get("message") or "")[:300] or None,
                "revision": ((obj(st.get("sync"))).get("revision") or "")[:12] or None,
            }
        )
    return sorted(out, key=lambda r: r["name"] or "")


def problems(apps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        a
        for a in apps
        if a["sync"] not in SYNCED
        or a["health"] not in HEALTHY
        or a["operation"] in ("Failed", "Error")
    ]


async def summary(cfg: Config) -> dict[str, Any]:
    apps = await applications(cfg)
    return {
        "apps": len(apps),
        "out_of_sync": [a["name"] for a in apps if a["sync"] not in SYNCED],
        "unhealthy": [f"{a['name']} ({a['health']})" for a in apps if a["health"] not in HEALTHY],
    }
