"""Traefik and Alertmanager: shaping, filters, and the overview summaries."""

import pytest
from mcp import Client

from homelab_mcp import server
from homelab_mcp.systems import alertmanager, traefik

pytestmark = pytest.mark.anyio

TRAEFIK = "https://traefik.test/api"
AM = "https://alertmanager.test/api/v2"


async def call(cfg, name, args=None):
    async with Client(server.build(cfg)) as c:
        r = await c.call_tool(name, args or {})
    assert not r.is_error, r.content[0].text
    return r.structured_content


async def test_routers_search_by_host_and_status(full_cfg, respx_mock):
    respx_mock.get(f"{TRAEFIK}/http/routers").respond(
        json=[
            {
                "name": "app-a@kubernetescrd",
                "rule": "Host(`a.lab.test`)\n",
                "entryPoints": ["lan"],
                "middlewares": ["chain@kubernetescrd"],
                "service": "svc-a",
                "status": "enabled",
                "provider": "kubernetescrd",
                "tls": {"options": "default"},
            },
            {
                "name": "app-b@kubernetescrd",
                "rule": "Host(`b.lab.test`)",
                "status": "disabled",
                "error": ["middleware missing"],
                "provider": "kubernetescrd",
            },
        ]
    )
    out = await call(full_cfg, "traefik_routers", {"search": "A.LAB.TEST"})
    assert out["items"] == [
        {
            "name": "app-a@kubernetescrd",
            "entryPoints": ["lan"],
            "middlewares": ["chain@kubernetescrd"],
            "service": "svc-a",
            "status": "enabled",
            "provider": "kubernetescrd",
            "rule": "Host(`a.lab.test`)",
            "tls": True,
        }
    ]
    broken = await call(full_cfg, "traefik_routers", {"status": "disabled"})
    assert broken["items"][0]["error"] == ["middleware missing"]


async def test_services_down_only(full_cfg, respx_mock):
    respx_mock.get(f"{TRAEFIK}/http/services").respond(
        json=[
            {"name": "up", "serverStatus": {"http://10.100.0.1:80": "UP"}},
            {
                "name": "down",
                "usedBy": ["app-b@kubernetescrd"],
                "serverStatus": {"http://10.100.0.2:80": "DOWN"},
                "observability": {"metadata": {"kubernetes": {"namespace": "ns", "name": "b"}}},
            },
            {"name": "noop@internal", "type": "noop"},
        ]
    )
    out = await call(full_cfg, "traefik_services", {"down_only": True})
    assert [s["name"] for s in out["items"]] == ["down"]
    assert out["items"][0]["kubernetes"] == {"namespace": "ns", "name": "b"}
    assert out["items"][0]["servers"] == {"http://10.100.0.2:80": "DOWN"}


def alert(name, severity, namespace=None, silenced=()):
    labels = {"alertname": name, "severity": severity, "prometheus": "p"}
    if namespace:
        labels["namespace"] = namespace
    return {
        "labels": labels,
        "annotations": {"summary": f"{name} fired"},
        "status": {"state": "suppressed" if silenced else "active", "silencedBy": list(silenced)},
        "startsAt": f"2026-11-1{len(name) % 9}T00:00:00Z",
    }


async def test_alerts_active_filters_and_hides_silenced_by_default(full_cfg, respx_mock):
    route = respx_mock.get(f"{AM}/alerts").respond(
        json=[alert("DiskFull", "critical", "ns-a"), alert("Watchdog", "none")]
    )
    out = await call(full_cfg, "alertmanager_alerts", {"severity": "critical"})
    assert out["items"] == [
        {
            "alertname": "DiskFull",
            "severity": "critical",
            "namespace": "ns-a",
            "state": "active",
            "starts_at": "2026-11-18T00:00:00Z",
            "summary": "DiskFull fired",
        }
    ]
    params = route.calls[0].request.url.params
    assert (params["active"], params["silenced"], params["inhibited"]) == ("true", "false", "false")
    await call(full_cfg, "alertmanager_alerts", {"include_silenced": True})
    assert route.calls[1].request.url.params["silenced"] == "true"


async def test_silences_by_state(full_cfg, respx_mock):
    respx_mock.get(f"{AM}/silences").respond(
        json=[
            {
                "id": "s1",
                "createdBy": "admin",
                "comment": "maintenance",
                "status": {"state": "active"},
                "matchers": [
                    {"name": "alertname", "value": "Disk.*", "isRegex": True},
                    {"name": "namespace", "value": "ns-a", "isRegex": False},
                ],
            },
            {"id": "s2", "status": {"state": "expired"}, "matchers": []},
        ]
    )
    out = await call(full_cfg, "alertmanager_silences")
    assert [s["id"] for s in out["items"]] == ["s1"]
    assert out["items"][0]["matchers"] == ["alertname=~Disk.*", "namespace=ns-a"]


async def test_summaries(full_cfg, respx_mock):
    respx_mock.get(f"{TRAEFIK}/overview").respond(
        json={"http": {"routers": {"total": 3, "warnings": 1, "errors": 0}}}
    )
    assert (await traefik.summary(full_cfg))["routers"] == {"total": 3, "warnings": 1, "errors": 0}
    respx_mock.get(f"{AM}/alerts").respond(
        json=[alert("A", "warning"), alert("B", "warning"), alert("Watchdog", "none")]
    )
    assert await alertmanager.summary(full_cfg) == {"firing_by_severity": {"none": 1, "warning": 2}}
