"""A small fake cluster for the `pvecli backup` tests.

Shapes copied from answers recorded on the lab (tests/fixtures/pve-9.1/
backup_*.json): a backup content item carries `subtype`, `ctime`, `notes`
and `protected` only when set; a vzdump task covering several guests has
an empty `id`, and its per guest outcome is only in the task log.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from tests.fake_api import FakeAPI, fixture

NOW = 1_800_000_000  # FakeClock start
DAY = 86400

NODES = [{"node": "pve1", "status": "online"}, {"node": "pve2", "status": "online"},
         {"node": "pve3", "status": "offline"}]

GUESTS = [
    {"vmid": 100, "type": "qemu", "node": "pve1", "name": "web1", "status": "running", "tags": "web", "pool": "P"},
    {"vmid": 101, "type": "qemu", "node": "pve2", "name": "web2", "status": "stopped", "tags": "web;db"},
    {"vmid": 200, "type": "lxc", "node": "pve1", "name": "dns", "status": "running", "pool": "P"},
    {"vmid": 9000, "type": "qemu", "node": "pve2", "name": "tpl", "status": "stopped", "template": 1},
]

STORAGES = [
    {"storage": "local", "type": "dir", "content": "backup,iso,vztmpl", "path": "/var/lib/vz"},
    {"storage": "local-lvm", "type": "lvmthin", "content": "images,rootdir"},
    {"storage": "pbs", "type": "pbs", "content": "backup", "shared": 1, "datastore": "ds"},
    {"storage": "old-nfs", "type": "nfs", "content": "backup", "shared": 1, "disable": 1},
]


def vol(storage: str, vmid: int, kind: str, ctime: int, **extra: Any) -> dict[str, Any]:
    """A backup content item as PVE lists it."""
    if storage == "pbs":
        stamp = f"2027-01-{(ctime // DAY) % 28 + 1:02d}T00:00:{ctime % 60:02d}Z"
        volid = f"pbs:backup/{'vm' if kind == 'qemu' else 'ct'}/{vmid}/{stamp}"
        fmt = "pbs-vm" if kind == "qemu" else "pbs-ct"
    else:
        volid = f"{storage}:backup/vzdump-{kind}-{vmid}-{ctime}.{'vma' if kind == 'qemu' else 'tar'}.zst"
        fmt = "vma.zst" if kind == "qemu" else "tar.zst"
    return {"content": "backup", "ctime": ctime, "format": fmt, "size": 1_000_000_000,
            "subtype": kind, "vmid": vmid, "volid": volid, **extra}


def enc(volid: str) -> str:
    """How a volid travels in a URL path (as the web interface sends it)."""
    return quote(volid, safe="")


# Local storage differs per node; pbs is shared
CONTENT: dict[tuple[str, str], list[dict[str, Any]]] = {
    ("pve1", "local"): [
        vol("local", 100, "qemu", NOW - 1 * DAY, notes="nightly web1"),
        vol("local", 100, "qemu", NOW - 8 * DAY, protected=1),
        vol("local", 200, "lxc", NOW - 3 * DAY),
    ],
    ("pve2", "local"): [
        vol("local", 101, "qemu", NOW - 2 * DAY - 3600),
        vol("local", 555, "qemu", NOW - 40 * DAY, notes="guest removed since"),
    ],
    ("*", "pbs"): [
        vol("pbs", 100, "qemu", NOW - 3600),
        vol("pbs", 200, "lxc", NOW - 5 * DAY),
    ],
}


def cluster(api: FakeAPI, content: dict[tuple[str, str], list[dict]] | None = None) -> FakeAPI:
    """Route the reads every backup command starts with."""
    data = CONTENT if content is None else content
    api.route("GET", "/nodes", NODES)
    api.route("GET", "/cluster/resources", GUESTS)
    api.route("GET", "/storage", STORAGES)

    def listing(call: Any) -> Any:
        node, storage = call.match["node"], call.match["storage"]
        if node == "pve3":
            return api.error(595, "no route to host")
        items = data.get((node, storage), data.get(("*", storage), []))
        vmid = (call.params or {}).get("vmid")
        return [i for i in items if vmid is None or i["vmid"] == int(vmid)]

    api.route("GET", "/nodes/{node}/storage/{storage}/content", listing)
    return api


def recorded(name: str) -> Any:
    """A response recorded on lab1 (PVE 9.1.1); an error becomes the APIError."""
    data = fixture(f"pve-9.1/{name}")
    if isinstance(data, dict) and "__error__" in data:
        return FakeAPI.error(data["__status__"], data["__error__"])
    return data


def log(*lines: str) -> list[dict[str, Any]]:
    """A task log as GET .../tasks/{upid}/log returns it."""
    return [{"n": i + 1, "t": t} for i, t in enumerate(lines)]
