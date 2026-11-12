"""ArgoCD REST (token of the `mcp` account, role:readonly).

Used by the drift report and the overview. Rich ArgoCD tooling comes from
the official argocd-mcp server that the plugin also starts.
"""

from typing import Any

from ..common import HttpClient
from ..config import Config

HEALTHY = {"Healthy"}
SYNCED = {"Synced"}


async def applications(cfg: Config) -> list[dict[str, Any]]:
    sec = cfg.section("argocd")
    http = HttpClient(
        "argocd",
        sec["url"],
        headers={"Authorization": f"Bearer {cfg.secret(sec, 'token', 'argocd')}"},
        verify=bool(sec.get("verify_tls", True)),
    )
    out = []
    for a in (await http.get("/api/v1/applications")).get("items") or []:
        st = a.get("status") or {}
        op = st.get("operationState") or {}
        out.append(
            {
                "name": (a.get("metadata") or {}).get("name"),
                "sync": (st.get("sync") or {}).get("status"),
                "health": (st.get("health") or {}).get("status"),
                "operation": op.get("phase"),
                "operation_message": (op.get("message") or "")[:300] or None,
                "revision": ((st.get("sync") or {}).get("revision") or "")[:12] or None,
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
