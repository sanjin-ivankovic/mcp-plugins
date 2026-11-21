"""Alertmanager API v2 (no credential). GET only.

Alertmanager is published on the `lan` entrypoint without authentication;
the LAN boundary is the guard. Silences are listed, never created.
"""

from collections import Counter
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from pydantic import Field

from ..common import HttpClient, Listing, arr, client_from, clip, obj, pick, tool
from ..config import Config


def client(cfg: Config) -> HttpClient:
    return client_from(cfg, "alertmanager", path="/api/v2")


def shape(a: dict[str, Any]) -> dict[str, Any]:
    labels = obj(a.get("labels"))
    ann = obj(a.get("annotations"))
    status = obj(a.get("status"))
    return {
        "alertname": labels.get("alertname"),
        "severity": labels.get("severity"),
        "namespace": labels.get("namespace"),
        "state": status.get("state"),
        "silenced_by": status.get("silencedBy") or None,
        "starts_at": a.get("startsAt"),
        "summary": ann.get("summary") or ann.get("description"),
        "runbook": ann.get("runbook_url"),
        "labels": {
            k: v
            for k, v in labels.items()
            if k not in ("alertname", "severity", "namespace", "prometheus")
        },
    }


async def summary(cfg: Config) -> dict[str, Any]:
    alerts = await client(cfg).get("/alerts", active=True, silenced=False, inhibited=False)
    by = Counter((obj(a.get("labels"))).get("severity", "none") for a in arr(alerts))
    return {"firing_by_severity": dict(sorted(by.items()))}


def register(app: MCPServer, cfg: Config) -> None:
    @tool(
        app,
        "Alerts firing in Alertmanager now, with severity, namespace, summary and runbook. "
        "Watchdog and InfoInhibitor always fire by design. include_silenced also lists "
        "silenced and inhibited alerts.",
        title="Firing alerts",
    )
    async def alertmanager_alerts(
        severity: Annotated[
            str | None, Field(description="Severity label, e.g. critical or warning")
        ] = None,
        namespace: Annotated[str | None, Field(description="Kubernetes namespace label")] = None,
        include_silenced: Annotated[
            bool, Field(description="Also list silenced and inhibited alerts")
        ] = False,
        limit: Annotated[int, Field(description="Rows to return, 1-1000")] = 100,
    ) -> Listing:
        alerts = await client(cfg).get(
            "/alerts",
            active=True,
            silenced=include_silenced,
            inhibited=include_silenced,
        )
        rows = [
            shape(a)
            for a in arr(alerts)
            if (severity is None or (obj(a.get("labels"))).get("severity") == severity)
            and (namespace is None or (obj(a.get("labels"))).get("namespace") == namespace)
        ]
        rows.sort(key=lambda r: r["starts_at"] or "", reverse=True)
        return clip([{k: v for k, v in r.items() if v} for r in rows], limit)

    @tool(
        app,
        "Alertmanager silences (active by default): matchers, author, comment, end time.",
        title="Alertmanager silences",
    )
    async def alertmanager_silences(
        state: Annotated[
            Literal["active", "pending", "expired"], Field(description="Silence state")
        ] = "active",
        limit: Annotated[int, Field(description="Rows to return, 1-1000")] = 50,
    ) -> Listing:
        rows = [
            {
                **pick(s, "id", "createdBy", "comment", "startsAt", "endsAt"),
                "matchers": [
                    f"{m.get('name')}{'=~' if m.get('isRegex') else '='}{m.get('value')}"
                    for m in arr(s.get("matchers"))
                ],
            }
            for s in arr(await client(cfg).get("/silences"))
            if (obj(s.get("status"))).get("state") == state
        ]
        return clip(rows, limit)
