"""Per-system behaviour against mocked APIs (shapes taken from the live lab)."""

import time

import httpx
import pytest
from mcp import Client

from homelab_mcp import server
from homelab_mcp.common import ApiError
from homelab_mcp.systems import komodo, pihole, unraid

pytestmark = pytest.mark.anyio

PVE = "https://pve.test:8006/api2/json"
PBS = "https://pbs.test:8007/api2/json"


async def call(cfg, name, args=None):
    async with Client(server.build(cfg)) as c:
        r = await c.call_tool(name, args or {})
    assert not r.is_error, r.content[0].text
    return r.structured_content


async def test_pve_auth_header_and_guests(full_cfg, respx_mock):
    route = respx_mock.get(f"{PVE}/cluster/resources", params={"type": "vm"}).respond(
        json={
            "data": [
                {
                    "vmid": 111,
                    "name": "technitium-2",
                    "type": "lxc",
                    "node": "pve",
                    "status": "running",
                    "tags": "dns;docker",
                    "cpu": 0.01,
                    "mem": 1,
                    "maxmem": 4,
                },
                {
                    "vmid": 9000,
                    "name": "tmpl",
                    "type": "qemu",
                    "node": "pve",
                    "status": "stopped",
                    "template": 1,
                },
                {"vmid": 417, "name": "scratch", "type": "lxc", "node": "pve", "status": "stopped"},
            ]
        }
    )
    out = await call(full_cfg, "pve_guests", {"type": "lxc"})
    assert [g["vmid"] for g in out["items"]] == [111, 417]
    assert out["items"][0]["tags"] == ["dns", "docker"]
    assert route.calls[0].request.headers["Authorization"] == (
        "PVEAPIToken=mcp@pve!readonly=pve-secret-uuid"
    )


async def test_pbs_backup_coverage(full_cfg, respx_mock):
    now = int(time.time())
    respx_mock.get(f"{PVE}/cluster/resources").respond(
        json={
            "data": [
                {"vmid": 110, "name": "technitium-1", "type": "lxc", "status": "running"},
                {"vmid": 441, "name": "talos-cp-1", "type": "qemu", "status": "running"},
                {"vmid": 417, "name": "scratch", "type": "lxc", "status": "running"},
            ]
        }
    )
    respx_mock.get(f"{PBS}/admin/datastore").respond(json={"data": [{"store": "main"}]})
    groups = respx_mock.get(f"{PBS}/admin/datastore/main/groups").respond(
        json={
            "data": [
                {
                    "backup-type": "ct",
                    "backup-id": "110",
                    "last-backup": now - 3600,
                    "backup-count": 9,
                },
                {"backup-type": "vm", "backup-id": "441", "last-backup": now - 5 * 86400},
            ]
        }
    )
    out = await call(full_cfg, "pbs_backup_coverage", {"max_age_hours": 36})
    assert out["ok"] == 1
    assert [m["vmid"] for m in out["missing"]] == [417]
    assert [s["vmid"] for s in out["stale"]] == [441]
    assert groups.calls[0].request.headers["Authorization"] == (
        "PBSAPIToken=mcp@pbs!readonly:pbs-secret-uuid"
    )


async def test_technitium_flags_serial_mismatch(full_cfg, respx_mock):
    def zones(serial):
        return {
            "status": "ok",
            "response": {
                "zones": [
                    {"name": "example.com", "type": "Primary", "soaSerial": serial},
                    {"name": "0.110.10.in-addr.arpa", "type": "Primary", "soaSerial": 7},
                ]
            },
        }

    respx_mock.post("http://ns1.test:5380/api/zones/list").respond(json=zones(42))
    respx_mock.post("http://ns2.test:5380/api/zones/list").respond(json=zones(41))
    out = await call(full_cfg, "dns_zones")
    assert out["problem_zones"] == ["example.com"]


async def test_technitium_error_envelope(full_cfg, respx_mock):
    respx_mock.post("http://ns1.test:5380/api/zones/records/get").respond(
        json={"status": "invalid-token", "errorMessage": "Invalid token."}
    )
    async with Client(server.build(full_cfg)) as c:
        r = await c.call_tool("dns_records", {"zone": "example.com"})
    assert r.is_error and "invalid-token" in r.content[0].text


PH1 = "http://pihole-1.test/api"
PH2 = "http://pihole-2.test/api"


@pytest.fixture(autouse=True)
def _fresh_pihole_sessions():
    pihole.Node._sessions.clear()
    yield
    pihole.Node._sessions.clear()


def _auth(sid):
    return {"session": {"valid": True, "sid": sid, "csrf": f"csrf-{sid}", "validity": 1800}}


async def test_pihole_logs_in_once_and_reuses_the_session(full_cfg, respx_mock):
    login = respx_mock.post(f"{PH2}/auth").respond(json=_auth("sid-two-abcdef"))
    blocking = respx_mock.get(f"{PH2}/dns/blocking").respond(json={"blocking": "enabled"})
    respx_mock.get(f"{PH2}/stats/summary").respond(json={"queries": {"total": 5}})
    respx_mock.get(f"{PH2}/stats/top_domains").respond(json={"domains": []})
    respx_mock.get(f"{PH2}/stats/top_clients").respond(json={"clients": []})
    respx_mock.get(f"{PH2}/stats/upstreams").respond(json={"upstreams": []})
    out = await call(full_cfg, "pihole_stats", {"node": "pihole-2"})
    assert out["pihole-2"]["queries"] == {"total": 5}
    await call(full_cfg, "pihole_stats", {"node": "pihole-2"})
    assert login.call_count == 1
    assert login.calls[0].request.content == b'{"password":"pihole2-app-password"}'
    assert respx_mock.calls.last.request.headers["X-FTL-SID"] == "sid-two-abcdef"
    assert not blocking.called


async def test_pihole_renews_an_expired_session(full_cfg, respx_mock):
    respx_mock.post(f"{PH2}/auth").mock(
        side_effect=[
            httpx.Response(200, json=_auth("sid-old-abcdef")),
            httpx.Response(200, json=_auth("sid-new-abcdef")),
        ]
    )
    lists = respx_mock.get(f"{PH2}/lists").mock(
        side_effect=[
            httpx.Response(401, json={"error": {"key": "unauthorized"}}),
            httpx.Response(200, json={"lists": [{"address": "a", "number": 3}]}),
        ]
    )
    respx_mock.get(f"{PH2}/domains").respond(json={"domains": []})
    out = await call(full_cfg, "pihole_lists", {"node": "pihole-2"})
    assert out["lists"]["items"] == [{"address": "a", "number": 3}]
    assert lists.calls[1].request.headers["X-FTL-SID"] == "sid-new-abcdef"
    assert not any(c.request.method == "DELETE" for c in respx_mock.calls)


async def test_pihole_login_refused_is_redacted(full_cfg, respx_mock):
    respx_mock.post(f"{PH2}/auth").respond(
        json={"session": {"valid": False, "message": "password pihole2-app-password wrong"}}
    )
    async with Client(server.build(full_cfg)) as c:
        r = await c.call_tool("pihole_lists", {"node": "pihole-2"})
    assert r.is_error
    assert "login refused" in r.content[0].text
    assert "pihole2-app-password" not in r.content[0].text


async def test_pihole_config_diff_ignores_per_node_domain(full_cfg, respx_mock):
    for base, domain, cache in ((PH1, "pihole1.example.com", 10000), (PH2, "pihole2.example.com", 5)):
        respx_mock.post(f"{base}/auth").respond(json=_auth(f"sid-{cache}-abcdef"))
        respx_mock.get(f"{base}/config").respond(
            json={"config": {"webserver": {"domain": domain}, "dns": {"cache": {"size": cache}}}}
        )
    out = await call(full_cfg, "pihole_config_diff")
    assert out["differences"] == [
        {"path": "dns.cache.size", "values": {"pihole-1": 10000, "pihole-2": 5}}
    ]


async def test_pihole_config_shows_only_modified_keys(full_cfg, respx_mock):
    respx_mock.post(f"{PH2}/auth").respond(json=_auth("sid-cfg-abcdef"))
    route = respx_mock.get(f"{PH2}/config", params={"detailed": "true"}).respond(
        json={
            "config": {
                "dns": {
                    "upstreams": {
                        "value": ["127.0.0.1#5335"],
                        "default": [],
                        "modified": True,
                        "flags": {"env_var": True},
                        "description": "long text",
                    },
                    "port": {"value": 53, "default": 53, "modified": False, "flags": {}},
                }
            }
        }
    )
    out = await call(full_cfg, "pihole_config", {"node": "pihole-2", "prefix": "dns"})
    assert route.called
    assert out["settings"] == {
        "dns.upstreams": {"value": ["127.0.0.1#5335"], "default": [], "env_var": True}
    }


async def test_pihole_search_quotes_the_domain_on_every_node(full_cfg, respx_mock):
    for base in (PH1, PH2):
        respx_mock.post(f"{base}/auth").respond(json=_auth("sid-search-abcdef"))
        respx_mock.get(f"{base}/search/ads.example%2Fx").respond(
            json={
                "search": {
                    "results": {"total": 1},
                    "domains": [],
                    "gravity": [{"domain": "ads.example", "address": "https://l"}],
                }
            }
        )
    out = await call(full_cfg, "pihole_search", {"domain": "ads.example/x"})
    assert set(out) == {"pihole-1", "pihole-2"}
    assert out["pihole-1"]["lists"] == [{"domain": "ads.example", "address": "https://l"}]


async def test_pihole_status_reports_uptime_in_seconds(full_cfg, respx_mock):
    for base in (PH1, PH2):
        respx_mock.post(f"{base}/auth").respond(json=_auth("sid-status-abcdef"))
        respx_mock.get(f"{base}/info/version").respond(
            json={"version": {"core": {"local": {"version": "v6.5"}}}}
        )
        respx_mock.get(f"{base}/dns/blocking").respond(json={"blocking": "enabled"})
        respx_mock.get(f"{base}/info/ftl").respond(json={"ftl": {"uptime": 172030.2}})
        respx_mock.get(f"{base}/info/messages").respond(json={"messages": []})
    out = await call(full_cfg, "pihole_status")
    assert out["pihole-1"]["uptime_s"] == 172
    assert out["pihole-1"]["versions"]["core"] == "v6.5"


async def test_komodo_only_allowlisted_reads(full_cfg):
    with pytest.raises(ApiError, match="not an allowlisted read request"):
        await komodo.read(full_cfg, "DeployStack", {"stack": "x"})


async def test_komodo_head_difference_is_not_a_problem(full_cfg, respx_mock):
    def reply(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        assert request.headers["X-Api-Key"] == "komodo-key-value"
        data = {
            "ListServers": [{"id": "s1", "name": "gitlab"}],
            "ListStacks": [
                {
                    "name": "gitlab",
                    "info": {
                        "state": "running",
                        "server_id": "s1",
                        "deployed_hash": "a",
                        "latest_hash": "b",
                    },
                }
            ],
        }[body["type"]]
        return httpx.Response(200, json=data)

    respx_mock.post("http://komodo.test:9120/read").mock(side_effect=reply)
    out = await call(full_cfg, "komodo_stacks", {"only_problems": True})
    assert out["count"] == 0


async def test_unraid_refuses_foreign_documents(full_cfg):
    with pytest.raises(ApiError, match="not built in"):
        await unraid.query(full_cfg, "mutation { array { stop } }")
    assert all(q.lstrip().startswith("query ") for q in unraid.QUERIES)


async def test_unraid_array(full_cfg, respx_mock):
    route = respx_mock.post("http://tower.test/graphql").respond(
        json={
            "data": {
                "array": {
                    "state": "STARTED",
                    "capacity": {"kilobytes": {"used": "50", "total": "100", "free": "50"}},
                    "disks": [
                        {
                            "name": "disk1",
                            "status": "DISK_OK",
                            "temp": 34,
                            "fsSize": 100,
                            "fsFree": 25,
                        }
                    ],
                    "parities": [],
                    "caches": [],
                }
            }
        }
    )
    out = await call(full_cfg, "unraid_array")
    assert out["state"] == "STARTED" and out["capacity_used_pct"] == 50.0
    assert out["disks"][0]["used_pct"] == 75.0
    assert route.calls[0].request.headers["x-api-key"] == "unraid-key-value"


async def test_semaphore_failed_only(full_cfg, respx_mock):
    api = "http://semaphore.test:3000/api"
    respx_mock.get(f"{api}/projects").respond(json=[{"id": 1, "name": "homelab"}])
    respx_mock.get(f"{api}/project/1/templates").respond(json=[{"id": 5, "name": "Patch — Fleet"}])
    respx_mock.get(f"{api}/project/1/tasks/last").respond(
        json=[
            {"id": 9, "template_id": 5, "status": "error", "created": "2026-10-28T02:00:00Z"},
            {"id": 8, "template_id": 5, "status": "success", "created": "2026-10-27T02:00:00Z"},
        ]
    )
    out = await call(full_cfg, "semaphore_tasks", {"failed_only": True})
    assert [t["id"] for t in out["items"]] == [9]
    assert out["items"][0]["template"] == "Patch — Fleet"
