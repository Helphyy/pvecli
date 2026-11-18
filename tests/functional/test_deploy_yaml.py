"""pvecli deploy <file.yaml>: VMs and containers on one or several clusters.

Every check runs before the first write, the plan says what will be created
or skipped, nothing is overwritten, a second run creates nothing, parallel
clones of one CT template never meet on its lock, and a failing guest does
not stop the others.
"""

from __future__ import annotations

import textwrap
from typing import Any

from tests.fake_api import FakeAPI, fixture

TPL = "local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst"


class Cluster:
    """A fake cluster behind one host: what PVE answers, and what it holds
    after the creations (a created guest shows in /cluster/resources)."""

    def __init__(self, api: FakeAPI, host: str, name: str, node: str, first_id: int = 120) -> None:
        self.api, self.host, self.node = api, host, node
        self.guests: list[dict[str, Any]] = []
        self.first_id = first_id
        self.clone_busy = False
        self.polled: set[str] = set()
        self.count = 0
        r = lambda m, p, v: api.route(m, p, v, host=host)  # noqa: E731
        r("GET", "/cluster/status", [{"type": "cluster", "name": name, "id": "cluster"},
                                     {"type": "node", "name": node, "ip": "10.0.0.1", "online": 1}])
        r("GET", "/cluster/resources", lambda c: list(self.guests))
        r("GET", "/nodes", [{"node": node, "status": "online"}])
        r("GET", "/pools", [])
        r("GET", f"/nodes/{node}/storage", fixture("pve-9.1/deploy_node_storage"))
        r("GET", f"/nodes/{node}/storage/local/content", fixture("pve-9.1/deploy_vztmpl_content"))
        r("GET", f"/nodes/{node}/network", fixture("pve-9.1/deploy_network_any_bridge"))
        r("GET", "/cluster/nextid", self.nextid)
        r("POST", f"/nodes/{node}/lxc", self.create)
        r("POST", f"/nodes/{node}/lxc/{{vmid}}/clone", self.clone)
        r("POST", f"/nodes/{node}/qemu/{{vmid}}/clone", self.clone)
        r("GET", f"/nodes/{node}/tasks/{{upid}}/status", self.task)
        r("GET", f"/nodes/{node}/{{kind}}/{{vmid}}/config", self.config)
        r("PUT", f"/nodes/{node}/{{kind}}/{{vmid}}/config", None)
        r("PUT", f"/nodes/{node}/qemu/{{vmid}}/cloudinit", None)
        r("PUT", f"/nodes/{node}/{{kind}}/{{vmid}}/resize", lambda c: self.upid("resize", c.match["vmid"]))
        r("POST", f"/nodes/{node}/{{kind}}/{{vmid}}/status/start", lambda c: self.upid("start", c.match["vmid"]))
        r("GET", f"/nodes/{node}/lxc/{{vmid}}/status/current", {"status": "running"})
        r("GET", f"/nodes/{node}/lxc/{{vmid}}/interfaces", fixture("pve-9.1/lxc_interfaces"))

    def add(self, vmid: int, kind: str, name: str, template: int = 0) -> Cluster:
        self.guests.append({"vmid": vmid, "type": kind, "name": name, "node": self.node,
                            "template": template, "status": "stopped"})
        return self

    def upid(self, kind: str, vmid: Any) -> str:
        # unique per task, as on PVE (pid and start time differ)
        self.count += 1
        return f"UPID:{self.node}:{self.count:08X}:0000BBBB:6AFD0000:{kind}:{vmid}:root@pam:"

    def taken(self) -> set[int]:
        return {g["vmid"] for g in self.guests}

    def nextid(self, call):
        vmid = (call.params or {}).get("vmid")
        if vmid is None:
            # the lowest free ID: PVE knows nothing of IDs handed out but not created yet
            return str(next(i for i in range(self.first_id, 10**6) if i not in self.taken()))
        if int(vmid) in self.taken():
            return FakeAPI.error(400, f"VM {vmid} already exists")
        return str(vmid)

    def create(self, call):
        self.add(int(call.data["vmid"]), "lxc", call.data.get("hostname", ""))
        return self.upid("vzcreate", call.data["vmid"])

    def clone(self, call):
        kind = "lxc" if "/lxc/" in call.path else "qemu"
        if kind == "lxc":
            # what PVE 9.1 answers to a second clone of a CT template in flight
            if self.clone_busy:
                return FakeAPI.error(500, "CT is locked (disk)")
            self.clone_busy = True
        self.add(int(call.data["newid"]), kind, call.data.get("hostname") or call.data.get("name", ""))
        return self.upid("vzclone" if kind == "lxc" else "qmclone", call.match["vmid"])

    def task(self, call):
        # the first poll finds every task still running: the caller sleeps,
        # and the other creations in flight get their turn meanwhile
        if call.match["upid"] not in self.polled:
            self.polled.add(call.match["upid"])
            return {"status": "running"}
        if ":vzclone:" in call.match["upid"]:
            self.clone_busy = False
        return {"status": "stopped", "exitstatus": "OK"}

    def config(self, call):
        vmid, kind = int(call.match["vmid"]), call.match["kind"]
        if kind == "qemu" and vmid == 9000:
            return {"template": 1, "name": "deb13-cloud", "boot": "order=scsi0",
                    "scsi0": "local-lvm:base-9000-disk-0,size=3G", "net0": "virtio=BC:24:11:96:77:50,bridge=vmbr0"}
        if kind == "lxc" and vmid == 920:
            return fixture("pve-9.1/deploy_lxc_template_config")
        if kind == "qemu":
            return {"boot": "order=scsi0", "scsi0": f"local-lvm:vm-{vmid}-disk-0,size=3G",
                    "ide2": f"local-lvm:vm-{vmid}-cloudinit,media=cdrom"}
        return {"net0": "name=eth0,bridge=vmbr0,hwaddr=BC:24:11:00:00:01,ip=dhcp,type=veth",
                "rootfs": f"local-lvm:vm-{vmid}-disk-0,size=2G", "unprivileged": 1}


def two_clusters(api: FakeAPI) -> tuple[Cluster, Cluster]:
    """h1 (profiles p1 and p3) holds VM template 9000, CT template 920 and a
    CT named 'fixct'; h2 (profile p2) is another cluster."""
    h1 = Cluster(api, "h1", "sdnlab", "pve-lab1").add(9000, "qemu", "deb13-cloud", 1).add(920, "lxc", "tpl920", 1)
    h1.add(311, "lxc", "fixct")
    h2 = Cluster(api, "h2", "other", "pve-b1", first_id=500).add(920, "lxc", "tpl920", 1)
    return h1, h2


def write(tmp_path, text: str):
    f = tmp_path / "deploy.yaml"
    f.write_text(textwrap.dedent(text))
    return str(f)


MIXED = """
    defaults:
      profile: p1
      storage: local-lvm
      ct:
        template: local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst
        node: pve-lab1
    guests:
      - type: vm
        name: web01
        template: 9000
        ip: 10.0.0.21/24
        gw: 10.0.0.1
      - type: ct
        name: fixct
      - type: ct
        name: db01
        profile: p2
        node: pve-b1
        ip: 10.0.1.5/24
"""


def creations(api: FakeAPI) -> list[tuple[str, str]]:
    return [(c.host, c.path) for c in api.writes() if c.path.endswith(("/clone", "/lxc"))]


# ── plan ─────────────────────────────────────────────────────────────────


def test_dry_run_shows_the_plan_and_writes_nothing(cli, api, tmp_path):
    two_clusters(api)

    r = cli("deploy", write(tmp_path, MIXED), "--dry-run")

    assert r.exit_code == 0, r.output
    assert api.writes() == []
    out = r.output
    assert "sdnlab" in out and "other" in out
    for name in ("web01", "fixct", "db01"):
        assert name in out
    assert "present" in out and "create" in out
    assert "2 guest(s) to create" in out


def test_declining_the_confirmation_writes_nothing(cli, api, tmp_path):
    two_clusters(api)

    r = cli("deploy", write(tmp_path, MIXED), input="n\n")

    assert r.exit_code == 0
    assert api.writes() == []
    assert "Cancelled" in r.output


def test_creates_on_two_clusters_and_skips_what_exists(cli, api, tmp_path):
    two_clusters(api)

    r = cli("deploy", write(tmp_path, MIXED), "-y")

    assert r.exit_code == 0, r.output
    assert sorted(creations(api)) == [("h1", "/nodes/pve-lab1/qemu/9000/clone"), ("h2", "/nodes/pve-b1/lxc")]
    [ct] = [c for c in api.writes() if c.path == "/nodes/pve-b1/lxc"]
    assert ct.data["vmid"] == 500 and ct.data["hostname"] == "db01"
    assert ct.data["net0"] == "name=eth0,bridge=vmbr0,ip=10.0.1.5/24"
    assert "fixct" in r.output and "skipped" in r.output
    # nothing at all is written for the guest already there
    assert not [c for c in api.writes() if "311" in c.path]


def test_a_second_run_creates_nothing(cli, api, tmp_path):
    two_clusters(api)
    f = write(tmp_path, MIXED)
    assert cli("deploy", f, "-y").exit_code == 0
    first = len(api.writes())

    r = cli("deploy", f, "-y")

    assert r.exit_code == 0, r.output
    assert len(api.writes()) == first
    assert "Nothing to create" in r.output


# ── validation before anything ───────────────────────────────────────────


def test_every_error_of_the_file_is_listed_and_nothing_is_written(cli, api, tmp_path, monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    two_clusters(api)
    f = write(tmp_path, """
        guests:
          - {type: vm, name: a, template: 9000, colour: red}
          - {type: ct, name: b, template: "local:vztmpl/missing.tar.zst", node: pve-lab1, storage: local-lvm}
          - {type: ct, name: c, template: 920, ip: 2001:db8::1/64}
          - {type: vm, name: d, template: 9000, password: hunter22}
          - {type: vm, name: e, template: 9000, password_env: NOPE}
          - {type: vm, name: f, template: 9999}
          - {type: ct, name: g, template: 920, profile: nowhere}
    """)

    r = cli("deploy", f, "-y", "-p", "p1")

    assert r.exit_code == 1
    out = " ".join(r.output.split())
    for part in ("'colour'", "missing.tar.zst", "IPv4", "password_env", "NOPE", "9999", "nowhere"):
        assert part in out, part
    assert "hunter22" not in r.output
    assert api.writes() == []


def test_an_entry_with_a_bad_key_is_still_checked_and_conflicts_are_listed_too(cli, api, tmp_path):
    two_clusters(api)
    f = write(tmp_path, """
        guests:
          - {type: ct, name: x1, template: 920, ip: 10.0.0.5, colour: red}
          - {type: vm, name: x3, template: 9999, password: nope}
          - {type: ct, name: x2, template: 920, id: 311}
    """)

    r = cli("deploy", f, "-y", "-p", "p1")

    # found on the lab: the prefix-less IP, the missing template and the
    # taken ID were only reported on a second attempt
    assert r.exit_code == 1
    out = " ".join(r.output.split())
    for part in ("'colour'", "prefix length", "password_env", "VM 9999 not found", "ID 311 is taken by fixct"):
        assert part in out, part
    assert api.writes() == []


def test_duplicates_in_the_file_are_refused(cli, api, tmp_path):
    two_clusters(api)
    f = write(tmp_path, """
        guests:
          - {type: ct, name: web, template: 920, ip: 10.0.0.5/24, id: 930}
          - {type: ct, name: web, template: 920, profile: p3, id: 931}
          - {type: ct, name: other, template: 920, ip: 10.0.0.5/24, id: 930}
    """)

    r = cli("deploy", f, "-y", "-p", "p1")

    # p3 is a second profile on the cluster of p1: 'web' twice on one cluster
    assert r.exit_code == 1
    out = " ".join(r.output.split())
    assert "'web'" in out and "930" in out and "10.0.0.5" in out
    assert api.writes() == []


def test_a_series_overflowing_its_subnet_is_refused(cli, api, tmp_path):
    two_clusters(api)
    f = write(tmp_path, """
        guests:
          - {type: ct, name: "w{n}", count: 3, template: 920, ip: 10.0.0.253/24}
    """)

    r = cli("deploy", f, "-y", "-p", "p1")

    assert r.exit_code == 1
    assert "10.0.0.0/24" in r.output
    assert api.writes() == []


def test_a_name_held_by_the_other_type_is_a_conflict(cli, api, tmp_path):
    two_clusters(api)
    f = write(tmp_path, """
        guests:
          - {type: vm, name: fixct, template: 9000}
          - {type: ct, name: fresh, template: 920, id: 311}
          - {type: ct, name: fine, template: 920}
    """)

    r = cli("deploy", f, "-y", "-p", "p1")

    # fixct is a container, 311 belongs to fixct: both conflicts, shown in the plan
    assert r.exit_code == 1
    out = " ".join(r.output.split())
    assert "conflict" in out and "fixct" in out and "311" in out
    assert api.writes() == []


def test_a_password_comes_from_the_environment_only(cli, api, tmp_path, monkeypatch):
    monkeypatch.setenv("CT_PASS", "S3cr3t-pw")
    two_clusters(api)
    f = write(tmp_path, f"""
        guests:
          - {{type: ct, name: c1, template: "{TPL}", node: pve-lab1, storage: local-lvm, password_env: CT_PASS}}
    """)

    r = cli("deploy", f, "-y", "-p", "p1")

    assert r.exit_code == 0, r.output
    [create] = [c for c in api.writes() if c.path == "/nodes/pve-lab1/lxc"]
    assert create.data["password"] == "S3cr3t-pw"
    assert "S3cr3t-pw" not in r.output


# ── execution ────────────────────────────────────────────────────────────


def test_parallel_clones_of_one_ct_template_wait_for_each_other(cli, api, tmp_path):
    h1, _ = two_clusters(api)
    f = write(tmp_path, """
        guests:
          - {type: ct, name: "w{n}", count: 4, template: 920, ip: 10.0.0.21/24}
          - {type: vm, name: "v{n}", count: 2, template: 9000}
    """)

    r = cli("deploy", f, "-y", "-p", "p1", "--parallel", "4")

    assert r.exit_code == 0, r.output
    assert "locked" not in r.output
    ids = sorted(int(c.data["newid"]) for c in api.writes() if c.path.endswith("/clone"))
    assert ids == [120, 121, 122, 123, 124, 125]  # distinct IDs, none reused


def test_ids_handed_out_are_reserved_and_checked(cli, api, tmp_path):
    two_clusters(api)
    f = write(tmp_path, """
        guests:
          - {type: ct, name: a, template: 920, id: 121}
          - {type: ct, name: b, template: 920}
          - {type: ct, name: c, template: 920}
    """)

    r = cli("deploy", f, "-y", "-p", "p1")

    assert r.exit_code == 0, r.output
    got = {c.data["hostname"]: c.data["newid"] for c in api.writes() if c.path.endswith("/clone")}
    # 121 is claimed by 'a' in the file: the others get 120 and 122
    assert got == {"a": 121, "b": 120, "c": 122}


def test_a_failure_does_not_stop_the_others_and_is_reported(cli, api, tmp_path):
    h1, _ = two_clusters(api)
    # the start of 121 fails after its creation: something is left behind
    api.route("POST", "/nodes/pve-lab1/lxc/121/status/start", FakeAPI.error(500, "startup for container '121' failed"), host="h1")
    f = write(tmp_path, f"""
        defaults: {{node: pve-lab1, storage: local-lvm, template: "{TPL}"}}
        guests:
          - {{type: ct, name: a}}
          - {{type: ct, name: b}}
          - {{type: ct, name: c}}
    """)

    r = cli("deploy", f, "-y", "-p", "p1", "--parallel", "2")

    assert r.exit_code == 1
    assert len([c for c in api.writes() if c.path == "/nodes/pve-lab1/lxc"]) == 3
    out = " ".join(r.output.split())
    assert "startup for container '121' failed" in out
    assert "pvecli ct remove 121 -p p1" in out
    # nothing is deleted automatically
    assert not [c for c in api.writes() if c.method == "DELETE"]


def test_wait_ip_shows_the_address_in_the_summary(cli, api, tmp_path):
    two_clusters(api)
    f = write(tmp_path, """
        guests:
          - {type: ct, name: a, template: 920}
    """)

    r = cli("deploy", f, "-y", "-p", "p1", "--wait-ip", "30")

    assert r.exit_code == 0, r.output
    assert "10.77.30.101" in r.output


def test_an_unreadable_file_is_an_error(cli, api, tmp_path):
    f = tmp_path / "bad.yaml"
    f.write_text("guests: [unclosed\n")

    r = cli("deploy", str(f), "-p", "p1")

    assert r.exit_code == 1
    assert api.calls == []
    assert cli("deploy", str(tmp_path / "none.yaml"), "-p", "p1").exit_code == 1


def test_a_vm_goes_to_another_node_only_from_shared_storage(cli, api, tmp_path):
    two_clusters(api)
    api.route("GET", "/nodes", [{"node": "pve-lab1", "status": "online"}, {"node": "pve-lab2", "status": "online"}],
              host="h1")
    f = write(tmp_path, """
        guests:
          - {type: vm, name: v1, template: 9000, node: pve-lab2}
          - {type: vm, name: v2, template: 9000, node: pve-lab9}
    """)

    r = cli("deploy", f, "-y", "-p", "p1")

    # 9000 sits on local-lvm of pve-lab1: PVE only clones to another node from shared storage
    assert r.exit_code == 1
    out = " ".join(r.output.split())
    assert "Template 9000 is on 'local-lvm', local to pve-lab1" in out
    assert "Node 'pve-lab9' not found" in out
    assert api.writes() == []
