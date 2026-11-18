"""cluster usage / pool usage: what is counted, what is counted once, what is left out.

The resources below follow /cluster/resources as PVE answers it: one storage
entry per (node, storage) pair, a shared storage repeating the same figures on
every node, a PBS datastore with maxdisk 0, and RADOS pools each publishing
"own used bytes + free space of the OSDs behind the pool's CRUSH rule".
"""

from __future__ import annotations

import json
from typing import Any

from tests.fake_api import Call, FakeAPI

GIB = 1024**3
MONS_A = "10.0.0.1 10.0.0.2 10.0.0.3"
MONS_B = "10.9.0.1,10.9.0.2"


def node(name: str, cpu: float, maxcpu: int, mem: int, maxmem: int) -> dict[str, Any]:
    return {"type": "node", "node": name, "status": "online", "cpu": cpu, "maxcpu": maxcpu,
            "mem": mem, "maxmem": maxmem, "disk": 5 * GIB, "maxdisk": 50 * GIB, "uptime": 3600}


def guest(vmid: int, kind: str, node_name: str, status: str, maxcpu: int, cpu: float,
          mem: int, maxmem: int, pool: str | None = None, template: int = 0) -> dict[str, Any]:
    entry = {"type": kind, "vmid": vmid, "id": f"{kind}/{vmid}", "name": f"g{vmid}", "node": node_name,
             "status": status, "maxcpu": maxcpu, "cpu": cpu, "mem": mem, "maxmem": maxmem,
             "maxdisk": 1 * GIB, "disk": 0, "template": template}
    if pool:
        entry["pool"] = pool
    return entry


def storage(name: str, node_name: str, plugintype: str, content: str, used: int, total: int,
            shared: int = 0, status: str = "available") -> dict[str, Any]:
    return {"type": "storage", "id": f"storage/{node_name}/{name}", "storage": name, "node": node_name,
            "plugintype": plugintype, "content": content, "disk": used, "maxdisk": total,
            "shared": shared, "status": status}


def rbd(name: str, used: int, free: int) -> list[dict[str, Any]]:
    """A RADOS pool seen from both nodes: maxdisk = own used + shared free."""
    return [storage(name, n, "rbd", "images,rootdir", used, used + free, shared=1) for n in ("pve1", "pve2")]


RESOURCES: list[dict[str, Any]] = [
    node("pve1", 0.5, 8, 32 * GIB, 64 * GIB),
    node("pve2", 0.25, 8, 16 * GIB, 64 * GIB),
    guest(100, "qemu", "pve1", "running", 4, 0.5, 4 * GIB, 8 * GIB, pool="web"),
    guest(101, "lxc", "pve2", "stopped", 2, 0.0, 0, 2 * GIB, pool="web"),
    guest(102, "qemu", "pve2", "running", 2, 0.25, 1 * GIB, 4 * GIB),
    guest(9000, "qemu", "pve1", "stopped", 1, 0.0, 0, 1 * GIB, pool="web", template=1),
    # Local storage without guest disk content: never a capacity
    storage("local", "pve1", "dir", "iso,vztmpl,backup", 10 * GIB, 100 * GIB),
    storage("local", "pve2", "dir", "iso,vztmpl,backup", 10 * GIB, 100 * GIB),
    # Local thin pool on each node: two distinct capacities, summed
    storage("local-lvm", "pve1", "lvmthin", "images,rootdir", 10 * GIB, 100 * GIB),
    storage("local-lvm", "pve2", "lvmthin", "images,rootdir", 20 * GIB, 100 * GIB),
    # One NFS share seen by both nodes: one capacity
    storage("nfs", "pve1", "nfs", "images,rootdir,iso", 100 * GIB, 1000 * GIB, shared=1),
    storage("nfs", "pve2", "nfs", "images,rootdir,iso", 100 * GIB, 1000 * GIB, shared=1),
    # PBS: maxdisk 0, backups only
    storage("pbs", "pve1", "pbs", "backup", 0, 0, shared=1),
    storage("pbs", "pve2", "pbs", "backup", 0, 0, shared=1),
    # Ceph cluster A, class ssd: two pools, one capacity (900 GiB free shared)
    *rbd("rbd-a", 100 * GIB, 900 * GIB),
    *rbd("rbd-b", 50 * GIB, 900 * GIB),
    # Ceph cluster A, class hdd: other OSDs, other free space, its own capacity
    *rbd("rbd-hdd", 1000 * GIB, 5000 * GIB),
    # Another Ceph cluster filled alike: same free space, distinct capacity
    *rbd("rbd-other", 10 * GIB, 900 * GIB),
]

STORAGE_CONFIG = [
    {"storage": "local", "type": "dir"},
    {"storage": "local-lvm", "type": "lvmthin"},
    {"storage": "nfs", "type": "nfs"},
    {"storage": "pbs", "type": "pbs"},
    {"storage": "rbd-a", "type": "rbd", "monhost": MONS_A},
    # Same monitors, other spelling: ports and separators do not matter
    {"storage": "rbd-b", "type": "rbd", "monhost": "10.0.0.3:6789;10.0.0.1:6789,10.0.0.2"},
    {"storage": "rbd-hdd", "type": "rbd", "monhost": MONS_A},
    {"storage": "rbd-other", "type": "rbd", "monhost": MONS_B},
]

# Volumes as /nodes/{node}/storage/{storage}/content lists them
CONTENT: dict[str, list[dict[str, Any]]] = {
    "rbd-a": [{"volid": "rbd-a:vm-100-disk-0", "vmid": 100, "content": "images", "size": 32 * GIB}],
    "local-lvm@pve1": [
        {"volid": "local-lvm:vm-100-disk-1", "vmid": 100, "content": "images", "size": 8 * GIB},
        {"volid": "local-lvm:base-9000-disk-0", "vmid": 9000, "content": "images", "size": 2 * GIB},
    ],
    "local-lvm@pve2": [],
    "nfs": [
        {"volid": "nfs:101/vm-101-disk-0.raw", "vmid": 101, "content": "rootdir", "size": 4 * GIB},
        {"volid": "nfs:iso/debian.iso", "content": "iso", "size": 700 * 1024**2},
    ],
}

TOTAL_CAPACITY = (200 + 1000 + (150 + 900) + 6000 + 910) * GIB


def content(call: Call) -> list[dict[str, Any]]:
    name, host = call.match["storage"], call.match["node"]
    return CONTENT.get(f"{name}@{host}", CONTENT.get(name, []))


def setup(api: FakeAPI, resources: list[dict[str, Any]] | None = None) -> None:
    api.route("GET", "/cluster/resources", resources if resources is not None else RESOURCES)
    api.route("GET", "/storage", STORAGE_CONFIG)
    api.route("GET", "/nodes/{node}/storage/{storage}/content", content)
    api.route("GET", "/pools/{poolid}", lambda c: {"poolid": c.match["poolid"], "comment": "Web tier", "members": []}
              if c.match["poolid"] == "web" else api.error(500, ""))


def usage_json(cli: Any, *args: str) -> dict[str, Any]:
    r = cli(*args, "--json", "-p", "p1")
    assert r.exit_code == 0, r.output
    return json.loads(r.stdout)


def by_name(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["storage"]: s for s in doc["storages"]}


# ── storage capacity ─────────────────────────────────────────────────────

def test_shared_storage_counted_once_local_storage_summed(cli, api):
    setup(api)
    s = by_name(usage_json(cli, "cluster", "usage"))
    assert (s["nfs"]["total"], s["nfs"]["used"], s["nfs"]["counted"]) == (1000 * GIB, 100 * GIB, True)
    assert (s["local-lvm"]["total"], s["local-lvm"]["used"]) == (200 * GIB, 30 * GIB)
    assert s["local-lvm"]["nodes"] == ["pve1", "pve2"]


def test_pbs_and_non_disk_storages_are_excluded(cli, api):
    setup(api)
    s = by_name(usage_json(cli, "cluster", "usage"))
    assert s["pbs"]["counted"] is False and s["pbs"]["reason"] == "backup datastore"
    assert s["local"]["counted"] is False and s["local"]["reason"] == "no guest disk content"


def test_rados_pools_of_one_cluster_and_one_class_are_one_capacity(cli, api):
    setup(api)
    s = by_name(usage_json(cli, "cluster", "usage"))
    # Exact capacity: used bytes of both pools plus the free space they share
    assert (s["rbd-a"]["used"], s["rbd-a"]["total"], s["rbd-a"]["counted"]) == (150 * GIB, 1050 * GIB, True)
    assert s["rbd-b"]["counted"] is False
    assert "rbd-a" in s["rbd-b"]["reason"]


def test_two_disk_classes_of_one_ceph_cluster_stay_apart(cli, api):
    setup(api)
    s = by_name(usage_json(cli, "cluster", "usage"))
    assert s["rbd-hdd"]["counted"] is True
    assert (s["rbd-hdd"]["used"], s["rbd-hdd"]["total"]) == (1000 * GIB, 6000 * GIB)


def test_two_ceph_clusters_with_the_same_free_space_stay_apart(cli, api):
    setup(api)
    s = by_name(usage_json(cli, "cluster", "usage"))
    assert s["rbd-other"]["counted"] is True
    assert s["rbd-other"]["total"] == 910 * GIB


def test_storage_totals(cli, api):
    setup(api)
    doc = usage_json(cli, "cluster", "usage")
    st = doc["storage_totals"]
    assert st["storages"] == 5  # local-lvm, nfs, rbd-a(+b), rbd-hdd, rbd-other
    assert st["total"] == TOTAL_CAPACITY
    assert st["used"] == (30 + 100 + 150 + 1000 + 10) * GIB
    assert doc["reference_storage"] == "rbd-hdd"  # shared, then the largest


def test_storage_config_unreadable_falls_back_with_a_warning(cli, api):
    setup(api)
    api.route("GET", "/storage", api.forbidden("/storage", "Datastore.Audit"))
    r = cli("cluster", "usage", "--json", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "approximate" in r.stderr
    s = by_name(json.loads(r.stdout))
    # Free space alone still folds the two pools of one class (and, as
    # documented, cannot tell rbd-other apart: that is why it warns)
    assert s["rbd-a"]["counted"] is True and s["rbd-b"]["counted"] is False
    # and still keeps the hdd class, whose free space differs
    assert s["rbd-hdd"]["counted"] is True


# ── workload, nodes, overhead ────────────────────────────────────────────

def test_node_and_guest_totals_templates_excluded(cli, api):
    setup(api)
    doc = usage_json(cli, "cluster", "usage")
    nt, gt = doc["node_totals"], doc["guest_totals"]
    assert (nt["nodes"], nt["online"], nt["cpu_allocated"]) == (2, 2, 16)
    assert nt["cpu_used"] == 6.0  # 0.5 * 8 + 0.25 * 8
    assert nt["mem_used"] == 48 * GIB
    assert (gt["guests"], gt["running"], gt["stopped"], gt["templates"]) == (3, 2, 1, 1)
    assert gt["cpu_allocated"] == 8 and gt["cpu_used"] == 2.5  # 0.5*4 + 0.25*2
    assert gt["mem_allocated"] == 14 * GIB
    # Overhead: node load no guest accounts for
    assert doc["overhead"]["cpu_used"] == 3.5
    assert doc["overhead"]["mem_used"] == 43 * GIB
    assert doc["cpu_overcommit"] == 0.5


def test_provisioned_disk_comes_from_storage_content(cli, api):
    setup(api)
    doc = usage_json(cli, "cluster", "usage")
    gt = doc["guest_totals"]
    # 32 (rbd-a) + 8 (local-lvm) for 100, 4 (nfs) for 101; the ISO is no disk
    assert gt["disk_provisioned"] == 44 * GIB
    assert gt["templates_disk_provisioned"] == 2 * GIB
    assert doc["disk_exact"] is True
    # A shared storage is listed once, a local one once per node, PBS never
    listed = sorted((c.path.split("/")[2], c.path.split("/")[4]) for c in api.called(
        "GET", "/nodes/{node}/storage/{storage}/content"))
    names = [s for _, s in listed]
    assert names.count("nfs") == 1 and names.count("local-lvm") == 2
    assert "pbs" not in names and "local" not in names


def test_pools_grouped_with_no_pool_last(cli, api):
    setup(api)
    pools = usage_json(cli, "cluster", "usage")["pools"]
    assert [p["poolid"] for p in pools] == ["web", "(no pool)"]
    web = pools[0]
    assert (web["guests"], web["templates"], web["cpu_allocated"]) == (2, 1, 6)
    assert web["disk_provisioned"] == 44 * GIB


def test_no_storage_skips_every_content_listing(cli, api):
    setup(api)
    doc = usage_json(cli, "cluster", "usage", "--no-storage")
    assert not api.called("GET", "/nodes/{node}/storage/{storage}/content")
    assert doc["storage_included"] is False and doc["disk_exact"] is False
    assert doc["guest_totals"]["disk_provisioned"] == 0


def test_storage_filter_scans_one_storage_and_is_not_exact(cli, api):
    setup(api)
    doc = usage_json(cli, "cluster", "usage", "--storage", "nfs")
    assert doc["storage_scanned"] == ["nfs"]
    assert doc["disk_exact"] is False
    assert doc["reference_storage"] == "nfs"


def test_unknown_storage_filter_is_refused(cli, api):
    setup(api)
    r = cli("cluster", "usage", "--storage", "nope", "-p", "p1")
    assert r.exit_code == 1
    assert "nope" in r.output
    assert not api.called("GET", "/nodes/{node}/storage/{storage}/content")


def test_shared_storage_listing_falls_back_on_the_next_node(cli, api):
    setup(api)

    def flaky(call: Call) -> Any:
        if call.match["storage"] == "nfs" and call.match["node"] == "pve1":
            return api.error(595, "no route to host")
        return content(call)

    api.route("GET", "/nodes/{node}/storage/{storage}/content", flaky)
    doc = usage_json(cli, "cluster", "usage")
    assert doc["storage_failed"] == []
    assert doc["guest_totals"]["disk_provisioned"] == 44 * GIB


def test_failed_listing_is_reported_and_not_exact(cli, api):
    setup(api)

    def broken(call: Call) -> Any:
        if call.match["storage"] == "nfs":
            return api.error(500, "storage is not online")
        return content(call)

    api.route("GET", "/nodes/{node}/storage/{storage}/content", broken)
    r = cli("cluster", "usage", "--json", "-p", "p1")
    assert r.exit_code == 0, r.output
    doc = json.loads(r.stdout)
    assert doc["storage_failed"] == ["nfs"]
    assert doc["disk_exact"] is False
    assert "storage listing failed on nfs" in r.stderr


def test_cluster_usage_text_render(cli, api):
    setup(api)
    r = cli("cluster", "usage", "-p", "p1")
    assert r.exit_code == 0, r.output
    for text in ("Cluster usage", "2 online / 2", "3 (2 running, 1 stopped, 1 templates)",
                 "6.0 cores used of 16 installed", "5 storages counted", "Provisioned",
                 "backup datastore", "same Ceph disks as 'rbd-a'"):
        assert text in r.output, text


def test_negative_overhead_is_explained(cli, api):
    resources = [node("pve1", 0.1, 4, 1 * GIB, 8 * GIB),
                 guest(100, "qemu", "pve1", "running", 4, 0.5, 2 * GIB, 4 * GIB)]
    setup(api, resources)
    r = cli("cluster", "usage", "--no-storage", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "A negative overhead is not an error" in r.output


# ── pool usage ───────────────────────────────────────────────────────────

def test_pool_usage_json_and_shares(cli, api):
    setup(api)
    doc = usage_json(cli, "pool", "usage", "web")
    assert doc["scope"] == "pool" and doc["poolid"] == "web" and doc["comment"] == "Web tier"
    assert (doc["guests"], doc["running"], doc["stopped"], doc["templates"]) == (2, 1, 1, 1)
    assert [m["vmid"] for m in doc["members"]] == [100, 101, 9000]
    share = doc["share"]
    assert share["cpu_allocated_percent_of_physical"] == 6 / 16 * 100
    assert share["cpu_allocated_percent_of_guests"] == 6 / 8 * 100
    assert share["disk_percent_of_guests"] == 100.0
    assert share["disk_percent_of_capacity"] == 44 * GIB / TOTAL_CAPACITY * 100


def test_pool_usage_unknown_pool(cli, api):
    setup(api)
    r = cli("pool", "usage", "nope", "-p", "p1")
    assert r.exit_code == 1
    assert "nope" in r.output


def test_pool_usage_existing_pool_without_members(cli, api):
    setup(api)
    api.route("GET", "/pools/{poolid}", {"poolid": "empty", "members": []})
    r = cli("pool", "usage", "empty", "--no-storage", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "No guests in this pool" in r.output


def test_pool_usage_text_render(cli, api):
    setup(api)
    r = cli("pool", "usage", "web", "-p", "p1")
    assert r.exit_code == 0, r.output
    for text in ("Pool usage: web", "Web tier", "Share of the cluster hardware (2 nodes)",
                 "Share of all guests (3 guests, templates excluded)", "Guests in web",
                 "template", "in templates", "guest capacity"):
        assert text in r.output, text


def test_pool_usage_json_without_pool_is_refused(cli, api):
    setup(api)
    r = cli("pool", "usage", "--json", "-p", "p1")
    assert r.exit_code == 1
    assert not api.called("GET", "/cluster/resources")


def test_pool_usage_menu_picks_the_pool(cli, api, ui):
    setup(api)
    api.route("GET", "/pools", [{"poolid": "web"}, {"poolid": "db"}])
    ui.menu("web")
    r = cli("pool", "usage", "--no-storage", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "Pool usage: web" in r.output
    assert ui.left() == {}


def test_pool_usage_menu_cancelled_reads_nothing_more(cli, api, ui):
    setup(api)
    api.route("GET", "/pools", [{"poolid": "web"}])
    ui.menu(None)
    r = cli("pool", "usage", "-p", "p1")
    assert r.exit_code == 0
    assert not api.called("GET", "/cluster/resources")
