"""One test per tool that test_systems.py does not cover, plus every overview summary."""

import asyncio
import json
import time

import httpx
import pytest
from mcp import Client

from homelab_mcp import server
from homelab_mcp.systems import argocd, komodo, pbs, pihole, pve, semaphore, technitium, unraid

pytestmark = pytest.mark.anyio

PVE = "https://pve.test:8006/api2/json"
PBS = "https://pbs.test:8007/api2/json"
NS1 = "http://ns1.test:5380"
NS2 = "http://ns2.test:5380"
PH1 = "http://pihole-1.test/api"
PH2 = "http://pihole-2.test/api"
KOMODO = "http://komodo.test:9120/read"
SEM = "http://semaphore.test:3000/api"
GITLAB = "https://git.test/api/v4"
UNRAID = "http://tower.test/graphql"

GUESTS = [
    {"vmid": 201, "name": "scratch-a", "type": "lxc", "node": "node-a", "status": "running"},
    {"vmid": 301, "name": "vm-a", "type": "qemu", "node": "node-a", "status": "stopped"},
]


async def call(cfg, name, args=None):
    async with Client(server.build(cfg)) as c:
        r = await c.call_tool(name, args or {})
    assert not r.is_error, r.content[0].text
    return r.structured_content


async def call_error(cfg, name, args=None) -> str:
    async with Client(server.build(cfg)) as c:
        r = await c.call_tool(name, args or {})
    assert r.is_error
    return r.content[0].text


def ok(response):
    return {"status": "ok", "response": response}


def auth(sid):
    return {"session": {"valid": True, "sid": sid, "csrf": f"csrf-{sid}"}}


def komodo_reply(data):
    def reply(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=data[json.loads(request.content)["type"]])

    return reply


# --- Proxmox VE ------------------------------------------------------------


async def test_pve_nodes_reads_version_of_online_nodes_only(full_cfg, respx_mock):
    respx_mock.get(f"{PVE}/nodes").respond(
        json={
            "data": [
                {"node": "node-a", "status": "online", "cpu": 0.25, "mem": 1, "maxmem": 4},
                {"node": "node-b", "status": "offline"},
            ]
        }
    )
    version = respx_mock.get(f"{PVE}/nodes/node-a/version").respond(
        json={"data": {"version": "9.1.0"}}
    )
    out = await call(full_cfg, "pve_nodes")
    assert out["nodes"][0] == {
        "node": "node-a",
        "status": "online",
        "cpu_pct": 25.0,
        "mem_pct": 25.0,
        "version": "9.1.0",
    }
    assert "version" not in out["nodes"][1]
    assert version.call_count == 1


async def test_pve_guest_and_unknown_vmid(full_cfg, respx_mock):
    respx_mock.get(f"{PVE}/cluster/resources").respond(json={"data": GUESTS})
    base = f"{PVE}/nodes/node-a/lxc/201"
    respx_mock.get(f"{base}/config").respond(json={"data": {"cores": 2, "digest": "d"}})
    respx_mock.get(f"{base}/status/current").respond(
        json={"data": {"status": "running", "uptime": 5, "netin": 1}}
    )
    respx_mock.get(f"{base}/snapshot").respond(
        json={"data": [{"name": "pre-upgrade", "snaptime": 1}, {"name": "current"}]}
    )
    out = await call(full_cfg, "pve_guest", {"vmid": 201})
    assert out["config"] == {"cores": 2}
    assert out["status"] == {"status": "running", "uptime": 5}
    assert [s["name"] for s in out["snapshots"]] == ["pre-upgrade"]
    assert "no guest with vmid 999" in await call_error(full_cfg, "pve_guest", {"vmid": 999})


async def test_pve_storage(full_cfg, respx_mock):
    respx_mock.get(f"{PVE}/cluster/resources", params={"type": "storage"}).respond(
        json={
            "data": [
                {"storage": "local", "node": "node-a", "disk": 2**30, "maxdisk": 4 * 2**30},
            ]
        }
    )
    out = await call(full_cfg, "pve_storage")
    assert out["storage"][0]["used_pct"] == 25.0 and out["storage"][0]["total_gib"] == 4.0


async def test_pve_tasks_errors_only_and_window(full_cfg, respx_mock):
    now = int(time.time())
    respx_mock.get(f"{PVE}/cluster/tasks").respond(
        json={
            "data": [
                {"upid": "a", "type": "vzdump", "starttime": now - 60, "status": "OK"},
                {"upid": "b", "type": "vzdump", "starttime": now - 120, "status": "job errors"},
                {"upid": "c", "type": "vzdump", "starttime": now - 9 * 86400, "status": "err"},
            ]
        }
    )
    out = await call(full_cfg, "pve_tasks", {"errors_only": True, "since_hours": 24})
    assert [t["upid"] for t in out["items"]] == ["b"]


async def test_pve_backup_jobs(full_cfg, respx_mock):
    respx_mock.get(f"{PVE}/cluster/backup").respond(
        json={"data": [{"id": "job-a", "schedule": "02:00", "storage": "pbs", "all": 1}]}
    )
    respx_mock.get(f"{PVE}/cluster/backup-info/not-backed-up").respond(
        json={"data": [{"vmid": 301, "name": "vm-a", "type": "qemu"}]}
    )
    out = await call(full_cfg, "pve_backup_jobs")
    assert out["jobs"][0]["id"] == "job-a"
    assert out["not_backed_up"] == [{"vmid": 301, "name": "vm-a", "type": "qemu"}]


# --- Proxmox Backup Server -------------------------------------------------


async def test_pbs_datastores(full_cfg, respx_mock):
    respx_mock.get(f"{PBS}/status/datastore-usage").respond(
        json={
            "data": [
                {
                    "store": "main",
                    "total": 100,
                    "used": 40,
                    "gc-status": {"removed-bytes": 5, "noise": 1},
                }
            ]
        }
    )
    out = await call(full_cfg, "pbs_datastores")
    assert out["datastores"][0]["used_pct"] == 40.0
    assert out["datastores"][0]["gc"] == {"removed-bytes": 5}


async def test_pbs_snapshots_groups_then_one_group(full_cfg, respx_mock):
    respx_mock.get(f"{PBS}/admin/datastore").respond(json={"data": [{"store": "main"}]})
    respx_mock.get(f"{PBS}/admin/datastore/main/namespace").respond(json={"data": [{"ns": ""}]})
    respx_mock.get(f"{PBS}/admin/datastore/main/groups").respond(
        json={
            "data": [
                {"backup-type": "ct", "backup-id": "201", "last-backup": 2},
                {"backup-type": "vm", "backup-id": "301", "last-backup": 1},
            ]
        }
    )
    snaps = respx_mock.get(f"{PBS}/admin/datastore/main/snapshots").respond(
        json={
            "data": [
                {"backup-type": "ct", "backup-id": "201", "backup-time": 1, "size": 10},
                {
                    "backup-type": "ct",
                    "backup-id": "201",
                    "backup-time": 2,
                    "verification": {"state": "ok"},
                },
            ]
        }
    )
    groups = await call(full_cfg, "pbs_snapshots", {"backup_type": "ct"})
    assert [g["backup-id"] for g in groups["items"]] == ["201"]
    out = await call(full_cfg, "pbs_snapshots", {"backup_type": "ct", "backup_id": "201"})
    assert [(s["backup-time"], s["verify"]) for s in out["items"]] == [(2, "ok"), (1, "none")]
    params = snaps.calls[0].request.url.params
    assert (params["backup-type"], params["backup-id"]) == ("ct", "201")


async def test_pbs_coverage_and_snapshots_read_every_namespace(full_cfg, respx_mock):
    """A guest backed up in a namespace is covered; an older copy in the root does not win."""
    now = int(time.time())
    respx_mock.get(f"{PVE}/cluster/resources").respond(
        json={"data": [{"vmid": 301, "name": "vm-a", "type": "qemu", "status": "running"}]}
    )
    respx_mock.get(f"{PBS}/admin/datastore").respond(json={"data": [{"store": "main"}]})
    respx_mock.get(f"{PBS}/admin/datastore/main/namespace").respond(
        json={"data": [{"ns": ""}, {"ns": "cluster"}]}
    )

    def groups(request: httpx.Request) -> httpx.Response:
        ns = request.url.params.get("ns")
        age = 3600 if ns == "cluster" else 5 * 86400
        return httpx.Response(
            200,
            json={"data": [{"backup-type": "vm", "backup-id": "301", "last-backup": now - age}]},
        )

    route = respx_mock.get(f"{PBS}/admin/datastore/main/groups").mock(side_effect=groups)
    out = await call(full_cfg, "pbs_backup_coverage")
    assert out == {"ok": 1, "missing": [], "stale": [], "max_age_hours": 36}
    asked = sorted(c.request.url.params.get("ns", "<root>") for c in route.calls)
    assert asked == ["<root>", "cluster"]

    listed = await call(full_cfg, "pbs_snapshots", {"backup_type": "vm"})
    assert [g.get("ns") for g in listed["items"]] == [None, "cluster"]
    only = await call(full_cfg, "pbs_snapshots", {"backup_type": "vm", "ns": "cluster"})
    assert [g["ns"] for g in only["items"]] == ["cluster"]


async def test_pbs_tasks_errors_only_passes_the_filter(full_cfg, respx_mock):
    route = respx_mock.get(f"{PBS}/nodes/localhost/tasks").respond(
        json={"data": [{"upid": "u", "worker_type": "verify", "status": "error"}]}
    )
    out = await call(full_cfg, "pbs_tasks")
    assert out["items"][0]["worker_type"] == "verify"
    assert route.calls[0].request.url.params["errors"] == "true"


async def test_pbs_tasks_reports_truncation(full_cfg, respx_mock):
    rows = [{"upid": f"u{i}", "status": "error"} for i in range(3)]
    route = respx_mock.get(f"{PBS}/nodes/localhost/tasks").respond(json={"data": rows})
    out = await call(full_cfg, "pbs_tasks", {"limit": 2})
    assert out["truncated"] and len(out["items"]) == 2
    assert route.calls[0].request.url.params["limit"] == "3"


# --- Unraid ----------------------------------------------------------------


def unraid_reply(data_by_query):
    def reply(request: httpx.Request) -> httpx.Response:
        doc = json.loads(request.content)["query"]
        for query, data in data_by_query.items():
            if doc == query:
                return httpx.Response(200, json={"data": data})
        return httpx.Response(200, json={"errors": [{"message": "unexpected query"}]})

    return reply


async def test_unraid_system_docker_vms_shares_notifications(full_cfg, respx_mock):
    respx_mock.post(UNRAID).mock(
        side_effect=unraid_reply(
            {
                unraid.Q_SYSTEM: {"info": {"os": {"hostname": "tower"}}},
                unraid.Q_METRICS: {"metrics": {"cpu": {"percentTotal": 7}}},
                unraid.Q_DOCKER: {
                    "docker": {
                        "containers": [
                            {"names": ["/b"], "state": "EXITED", "image": "i", "autoStart": 1},
                            {"names": ["/a"], "state": "RUNNING", "image": "i"},
                        ]
                    }
                },
                unraid.Q_VMS: {"vms": {"domains": [{"id": "1", "name": "vm", "state": "SHUTOFF"}]}},
                unraid.Q_SHARES: {"shares": [{"name": "media", "free": 1, "used": 2}]},
                unraid.Q_NOTIFICATIONS: {
                    "notifications": {
                        "overview": {"unread": {"alert": 1, "warning": 0}},
                        "list": [{"id": "n1", "subject": "disk hot"}],
                    }
                },
            }
        )
    )
    system = await call(full_cfg, "unraid_system")
    assert system["info"]["os"]["hostname"] == "tower" and system["metrics"]["cpu"]
    docker = await call(full_cfg, "unraid_docker", {"state": "exited"})
    assert [c["name"] for c in docker["items"]] == ["b"]
    assert (await call(full_cfg, "unraid_vms"))["domains"][0]["state"] == "SHUTOFF"
    assert (await call(full_cfg, "unraid_shares"))["shares"][0]["name"] == "media"
    notes = await call(full_cfg, "unraid_notifications", {"limit": 500})
    assert notes["unread"]["alert"] == 1 and notes["items"][0]["id"] == "n1"


# --- Technitium ------------------------------------------------------------


async def test_dns_records_reads_the_named_node(full_cfg, respx_mock):
    route = respx_mock.post(f"{NS2}/api/zones/records/get").respond(
        json=ok({"records": [{"name": "a.lab.test", "type": "A", "rData": {"ipAddress": "x"}}]})
    )
    out = await call(full_cfg, "technitium_records", {"zone": "lab.test", "node": "ns2"})
    assert out["node"] == "ns2" and out["items"][0]["type"] == "A"
    form = dict(httpx.QueryParams(route.calls[0].request.content.decode()))
    assert form == {"zone": "lab.test", "domain": "lab.test", "listZone": "true"}
    assert "unknown technitium node 'ns9'" in await call_error(
        full_cfg, "technitium_records", {"zone": "lab.test", "node": "ns9"}
    )


async def test_dhcp_scopes_and_leases_use_the_dhcp_node(full_cfg, respx_mock):
    respx_mock.post(f"{NS1}/api/dhcp/scopes/list").respond(
        json=ok({"scopes": [{"name": "lan", "enabled": True}]})
    )
    respx_mock.post(f"{NS1}/api/dhcp/leases/list").respond(
        json=ok(
            {
                "leases": [
                    {"scope": "lan", "address": "10.100.0.5", "hostName": "Printer-A"},
                    {"scope": "lan", "address": "10.100.0.6", "hostName": "laptop"},
                ]
            }
        )
    )
    assert (await call(full_cfg, "technitium_dhcp_scopes"))["node"] == "ns1"
    leases = await call(full_cfg, "technitium_dhcp_leases", {"search": "printer"})
    assert [x["address"] for x in leases["items"]] == ["10.100.0.5"]


async def test_dns_stats_on_every_node(full_cfg, respx_mock):
    for base in (NS1, NS2):
        respx_mock.post(f"{base}/api/dashboard/stats/get").respond(
            json=ok({"stats": {"totalQueries": 9}, "topClients": [1, 2, 3]})
        )
    out = await call(full_cfg, "technitium_stats", {"top": 2})
    assert set(out) == {"ns1", "ns2"} and out["ns1"]["top_clients"] == [1, 2]


async def test_dns_query_log_and_missing_logger(full_cfg, respx_mock):
    apps = respx_mock.post(f"{NS1}/api/apps/list")
    apps.respond(
        json=ok(
            {
                "apps": [
                    {"name": "Query Logs", "dnsApps": [{"classPath": "Q", "isQueryLogger": True}]}
                ]
            }
        )
    )
    logs = respx_mock.post(f"{NS1}/api/logs/query").respond(
        json=ok({"totalEntries": 1, "entries": [{"qname": "a.lab.test", "rcode": "NxDomain"}]})
    )
    out = await call(full_cfg, "technitium_query_log", {"rcode": "NxDomain", "limit": 9999})
    assert out["logger"] == "Query Logs" and out["entries"][0]["rcode"] == "NxDomain"
    form = dict(httpx.QueryParams(logs.calls[0].request.content.decode()))
    assert form["entriesPerPage"] == "500" and form["classPath"] == "Q"

    apps.respond(json=ok({"apps": []}))
    assert "no query-logger app" in await call_error(full_cfg, "technitium_query_log")


# --- Pi-hole ---------------------------------------------------------------


def pihole_login(respx_mock, *bases):
    for base in bases:
        respx_mock.post(f"{base}/auth").respond(json=auth(f"sid-{base[-12:]}"))


async def test_pihole_query_log(full_cfg, respx_mock):
    pihole_login(respx_mock, PH1)
    route = respx_mock.get(f"{PH1}/queries").respond(
        json={
            "recordsFiltered": 1,
            "queries": [
                {
                    "time": 1,
                    "domain": "ads.example",
                    "status": "GRAVITY",
                    "client": {"ip": "10.100.0.5"},
                    "reply": {"type": "BLOB", "time": 0.0012},
                }
            ],
        }
    )
    out = await call(full_cfg, "pihole_query_log", {"status": "GRAVITY"})
    assert out["node"] == "pihole-1" and out["entries"][0]["reply_ms"] == 1.2
    assert route.calls[0].request.url.params["status"] == "GRAVITY"


async def test_pihole_messages_on_both_nodes(full_cfg, respx_mock):
    pihole_login(respx_mock, PH1, PH2)
    for base in (PH1, PH2):
        respx_mock.get(f"{base}/info/messages").respond(
            json={"messages": [{"id": 1, "type": "LIST", "plain": "download failed"}]}
        )
    out = await call(full_cfg, "pihole_messages")
    assert out["pihole-2"][0]["plain"] == "download failed"


async def test_pihole_history_and_per_client(full_cfg, respx_mock):
    pihole_login(respx_mock, PH2)
    respx_mock.get(f"{PH2}/history").respond(json={"history": [{"total": 1}] * 3})
    respx_mock.get(f"{PH2}/history/clients").respond(
        json={"clients": {"10.100.0.5": {}}, "history": [{"data": {}}]}
    )
    out = await call(full_cfg, "pihole_history", {"node": "pihole-2", "limit": 2})
    assert out["history"]["count"] == 3 and out["history"]["truncated"]
    per = await call(full_cfg, "pihole_history", {"node": "pihole-2", "per_client": True})
    assert "10.100.0.5" in per["clients"]


async def test_pihole_logs_tail_and_next_id(full_cfg, respx_mock):
    pihole_login(respx_mock, PH1)
    route = respx_mock.get(f"{PH1}/logs/ftl").respond(
        json={"nextID": 42, "log": [{"timestamp": i, "message": f"m{i}"} for i in range(5)]}
    )
    out = await call(full_cfg, "pihole_logs", {"next_id": 7, "limit": 2})
    assert out["next_id"] == 42 and [x["message"] for x in out["lines"]] == ["m3", "m4"]
    assert out["truncated"] and route.calls[0].request.url.params["nextID"] == "7"


# --- Komodo ----------------------------------------------------------------


async def test_komodo_servers_stack_updates_and_syncs(full_cfg, respx_mock):
    respx_mock.post(KOMODO).mock(
        side_effect=komodo_reply(
            {
                "ListServers": [{"id": "s1", "name": "host-a", "info": {"state": "Ok"}}],
                "GetStack": {
                    "name": "stack-a",
                    "config": {"server_id": "s1", "secret": "no"},
                    "info": {"deployed_hash": "a", "missing_files": []},
                },
                "ListStackServices": [
                    {"service": "web", "image": "img", "container": {"state": "running"}}
                ],
                "ListUpdates": {
                    "updates": [
                        {"operation": "DeployStack", "success": True},
                        {"operation": "RunSync", "success": False, "target": {"id": "x"}},
                    ]
                },
                "ListResourceSyncs": [{"id": "y1", "name": "sync-a", "info": {"state": "Ok"}}],
                "GetResourceSync": {
                    "name": "sync-a",
                    "info": {"resource_updates": [{"target": "t", "data": "d"}]},
                },
            }
        )
    )
    assert (await call(full_cfg, "komodo_servers"))["servers"][0]["state"] == "Ok"
    stack = await call(full_cfg, "komodo_stack", {"stack": "stack-a"})
    assert "secret" not in stack["config"] and stack["services"][0]["state"] == "running"
    failed = await call(full_cfg, "komodo_updates", {"failed_only": True})
    assert [u["operation"] for u in failed["items"]] == ["RunSync"]
    syncs = (await call(full_cfg, "komodo_sync_status"))["syncs"]
    assert syncs[0]["has_pending"] and syncs[0]["pending_updates"] == [{"target": "t", "data": "d"}]


# --- Semaphore and GitLab --------------------------------------------------


async def test_semaphore_task_output_is_redacted_and_missing_task_errors(full_cfg, respx_mock):
    respx_mock.get(f"{SEM}/projects").respond(json=[{"id": 1, "name": "project-a"}])
    respx_mock.get(f"{SEM}/project/1/tasks/9/output").respond(
        json=[{"output": "TASK [x]"}, {"output": "token=leaky-value"}, {"output": "ok"}]
    )
    respx_mock.get(f"{SEM}/project/1/tasks/8/output").respond(json=[])
    out = await call(full_cfg, "semaphore_task_output", {"task_id": 9, "tail": 2})
    assert out["output"] == "token=***\nok" and out["lines_total"] == 3 and out["truncated"]
    assert "task 8 not found" in await call_error(full_cfg, "semaphore_task_output", {"task_id": 8})


async def test_gitlab_pipeline_jobs_reads_every_page(full_cfg, respx_mock):
    def page(request: httpx.Request) -> httpx.Response:
        n = int(request.url.params["page"])
        size = 100 if n == 1 else 3
        return httpx.Response(200, json=[{"id": n * 1000 + i, "name": "j"} for i in range(size)])

    respx_mock.get(f"{GITLAB}/projects/homelab%2Frepo-a/pipelines/5/jobs").mock(side_effect=page)
    out = await call(
        full_cfg, "gitlab_pipeline_jobs", {"project": "repo-a", "pipeline_id": 5, "limit": 500}
    )
    assert out["count"] == 103


# --- Overview and summaries ------------------------------------------------


async def test_overview_times_out_one_system(full_cfg, monkeypatch):
    async def slow(cfg):
        await asyncio.sleep(5)

    async def fine(cfg):
        return {"ok": True}

    for name, (_, section) in list(server.SUMMARIES.items()):
        monkeypatch.setitem(server.SUMMARIES, name, (fine, section))
    monkeypatch.setitem(server.SUMMARIES, "komodo", (slow, "komodo"))
    out = await server.overview(full_cfg, timeout=0.05)
    assert out["errors"] == ["komodo"] and "timed out" in out["systems"]["komodo"]["error"]


async def test_overview_tool_reports_unconfigured_systems(make_cfg):
    out = await call(make_cfg("[repos]\n"), "homelab_overview")
    assert out["systems"]["pve"] == {"status": "not configured"}


async def test_summaries(full_cfg, respx_mock):
    now = int(time.time())
    respx_mock.get(f"{PVE}/nodes").respond(json={"data": [{"node": "n", "status": "online"}]})
    respx_mock.get(f"{PVE}/cluster/resources").respond(json={"data": GUESTS})
    respx_mock.get(f"{PVE}/cluster/tasks").respond(
        json={"data": [{"starttime": now, "status": "err"}]}
    )
    assert await pve.summary(full_cfg) == {
        "nodes_online": 1,
        "nodes_total": 1,
        "guests_running": 1,
        "guests_stopped": ["301 vm-a"],
        "failed_tasks_24h": 1,
    }

    respx_mock.get(f"{PBS}/status/datastore-usage").respond(
        json={"data": [{"store": "main", "used": 1, "total": 4}]}
    )
    respx_mock.get(f"{PBS}/nodes/localhost/tasks").respond(json={"data": []})
    assert await pbs.summary(full_cfg) == {"datastores": {"main": 25.0}, "failed_tasks_24h": 0}

    respx_mock.post(UNRAID).mock(
        side_effect=unraid_reply(
            {
                unraid.Q_ARRAY: {
                    "array": {"state": "STARTED", "disks": [{"name": "d1", "status": "DISK_DSBL"}]}
                },
                unraid.Q_NOTIFICATIONS: {"notifications": {"overview": {"unread": {"alert": 2}}}},
            }
        )
    )
    u = await unraid.summary(full_cfg)
    assert u["disks_not_ok"] == ["d1"] and u["unread_alerts"] == 2

    for base in (NS1, NS2):
        respx_mock.post(f"{base}/api/zones/list").respond(
            json=ok({"zones": [{"name": "lab.test", "soaSerial": 1}]})
        )
    assert (await technitium.summary(full_cfg))["problem_zones"] == []

    pihole_login(respx_mock, PH1, PH2)
    for base in (PH1, PH2):
        respx_mock.get(f"{base}/info/version").respond(
            json={"version": {"core": {"local": {"version": "v6"}}}}
        )
        respx_mock.get(f"{base}/dns/blocking").respond(json={"blocking": "enabled"})
        respx_mock.get(f"{base}/info/messages").respond(json={"messages": [1]})
    assert (await pihole.summary(full_cfg))["pihole-1"] == {
        "version": "v6",
        "blocking": "enabled",
        "messages": 1,
    }

    respx_mock.post(KOMODO).mock(
        side_effect=komodo_reply(
            {
                "ListServers": [],
                "ListStacks": [{"name": "stack-a", "info": {"state": "down"}}],
                "ListResourceSyncs": [],
            }
        )
    )
    assert (await komodo.summary(full_cfg))["stacks_not_running"] == ["stack-a"]

    respx_mock.get(f"{SEM}/projects").respond(json=[{"id": 1, "name": "p"}])
    respx_mock.get(f"{SEM}/project/1/templates").respond(json=[{"id": 5, "name": "Template A"}])
    respx_mock.get(f"{SEM}/project/1/tasks/last").respond(
        json=[{"id": 2, "template_id": 5, "status": "error", "created": "b"}]
    )
    assert (await semaphore.summary(full_cfg)) == {"templates_last_failed": ["Template A"]}

    respx_mock.get("https://argo.test/api/v1/applications").respond(
        json={
            "items": [
                {
                    "metadata": {"name": "app-a"},
                    "status": {"sync": {"status": "OutOfSync"}, "health": {"status": "Healthy"}},
                }
            ]
        }
    )
    assert (await argocd.summary(full_cfg))["out_of_sync"] == ["app-a"]
