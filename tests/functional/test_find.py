"""pvecli find: IPs, MACs, volumes, networks, filters and ACLs on a recorded lab."""

from __future__ import annotations

import json

import pytest

from tests.fake_api import fixture

# The lab of pve-9.1: 300 guest-sdn (pve-lab2, stopped), 301 dhcp-test,
# 311 fixct (lxc, running), 9000 deb13-cloud (template). VM 320 is the
# running Debian VM with the guest agent the pve-9.2 agent fixture comes from.
VM_320 = {
    "id": "qemu/320", "vmid": 320, "name": "agentvm", "node": "pve-lab1", "type": "qemu",
    "status": "running", "template": 0, "maxcpu": 2, "maxmem": 2147483648,
}

CONFIGS = {
    300: fixture("pve-9.1/qemu_config"),
    301: {
        "name": "dhcp-test", "memory": "1024", "ide2": "local-lvm:vm-301-cloudinit,media=cdrom",
        "ipconfig0": "ip=10.20.3.14/24,gw=10.20.3.1",
        "net0": "virtio=BC:24:11:01:03:01,bridge=vmbr0,tag=42",
        "scsi0": "local-lvm:vm-301-disk-0,size=8G",
    },
    311: fixture("pve-9.1/lxc_config"),
    320: {
        "name": "agentvm", "agent": "1", "net0": "virtio=BC:24:11:D8:C9:E0,bridge=vsimple",
        "scsi0": "labpool:vm-320-disk-0,size=32G",
    },
    9000: {"name": "deb13-cloud", "net0": "virtio=BC:24:11:96:77:50,bridge=vsimple",
           "scsi0": "local-lvm:base-9000-disk-0,size=3G"},
}


def _config(call):
    return CONFIGS[int(call.match["vmid"])]


@pytest.fixture
def lab(api):
    api.route("GET", "/cluster/resources", fixture("pve-9.1/cluster_resources_vm") + [VM_320])
    api.route("GET", "/nodes/{node}/qemu/{vmid}/config", _config)
    api.route("GET", "/nodes/{node}/lxc/{vmid}/config", _config)
    api.route("GET", "/nodes/{node}/lxc/{vmid}/interfaces", fixture("pve-9.1/lxc_interfaces"))
    api.route("GET", "/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces",
              fixture("pve-9.2/qemu_agent_interfaces_running"))
    api.route("GET", "/cluster/sdn/ipams", fixture("pve-9.1/sdn_ipams"))
    api.route("GET", "/cluster/sdn/ipams/{ipam}/status", fixture("pve-9.1/sdn_ipam_pve_status"))
    api.route("GET", "/cluster/status", fixture("pve-9.1/cluster_status"))
    api.route("GET", "/storage", fixture("pve-9.1/storage"))
    api.route("GET", "/cluster/firewall/aliases", fixture("pve-9.1/firewall_aliases"))
    api.route("GET", "/cluster/firewall/ipset", [])
    api.route("GET", "/cluster/backup-info/not-backed-up", fixture("pve-9.1/backup_not_backed_up"))
    api.route("GET", "/access/permissions", lambda c: {c.params["path"]: {"VM.Audit": 1, "Sys.Audit": 1}})
    return api


def find_json(cli, *args):
    r = cli("find", *args, "-p", "p1", "--json")
    assert r.exit_code == 0, r.output
    return json.loads(r.stdout)


def hits(rows):
    """{(vmid or name, source)} of the rows."""
    return {(r["vmid"] if r["vmid"] is not None else r["name"], r["source"]) for r in rows}


# ── IP search, by source ──────────────────────────────────────────────


def test_ip_from_cloud_init_config(cli, lab):
    rows = find_json(cli, "10.20.3.14")
    assert hits(rows) == {(301, "config")}
    assert rows[0]["match"] == "ipconfig0 10.20.3.14/24"
    assert rows[0]["type"] == "VM" and rows[0]["name"] == "dhcp-test"
    assert lab.writes() == []


def test_ip_from_sdn_ipam_of_a_stopped_vm(cli, lab):
    rows = find_json(cli, "10.77.30.100")
    assert hits(rows) == {(300, "IPAM pve")}
    assert rows[0]["match"] == "10.77.30.100 on vsimple"


def test_ip_from_qemu_guest_agent(cli, lab):
    rows = find_json(cli, "10.77.30.151")
    assert hits(rows) == {(320, "agent")}
    assert rows[0]["match"] == "10.77.30.151 on eth0 (BC:24:11:D8:C9:E0)"
    assert lab.called("GET", "/nodes/pve-lab1/qemu/320/agent/network-get-interfaces")


def test_agent_not_queried_when_disabled_in_config(cli, lab):
    CONFIGS_320 = dict(CONFIGS[320], agent="0")
    lab.route("GET", "/nodes/{node}/qemu/320/config", CONFIGS_320)
    rows = find_json(cli, "10.77.30.151")
    assert rows == []
    assert not lab.called("GET", "/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces")


def test_ip_from_lxc_interfaces_with_prefix(cli, lab):
    # PVE gives inet as '10.77.30.101/24': find used to crash on it
    rows = find_json(cli, "10.77.30.101")
    assert hits(rows) == {(311, "lxc"), (311, "IPAM pve")}
    lxc_row = next(r for r in rows if r["source"] == "lxc")
    assert lxc_row["match"] == "10.77.30.101 on eth0 (BC:24:11:A6:9E:69)"
    assert lxc_row["type"] == "CT"


def test_loopback_of_guests_is_never_an_answer(cli, lab):
    assert find_json(cli, "127.0.0.1") == []


def test_lxc_secondary_ip_from_ip_addresses(cli, lab):
    ifaces = fixture("pve-9.1/lxc_interfaces")
    eth0 = ifaces[1]
    eth0["ip-addresses"].insert(1, {"ip-address": "10.77.30.120", "ip-address-type": "inet", "prefix": "24"})
    lab.route("GET", "/nodes/{node}/lxc/{vmid}/interfaces", ifaces)
    rows = find_json(cli, "10.77.30.120")
    assert hits(rows) == {(311, "lxc")}


def test_sdn_gateway_is_reported_as_infrastructure(cli, lab):
    rows = find_json(cli, "10.77.30.1")
    assert rows == [{"type": "SDN gateway", "vmid": None, "name": "vsimple", "node": "",
                     "status": "", "match": "10.77.30.1", "source": "IPAM pve"}]


def test_ip_of_a_node(cli, lab):
    rows = find_json(cli, "10.199.32.52")
    assert rows == [{"type": "node", "vmid": None, "name": "pve-lab2", "node": "", "status": "",
                     "match": "10.199.32.52", "source": "cluster"}]


def test_ip_of_a_storage_server(cli, lab):
    rows = find_json(cli, "10.199.32.70")
    assert [(r["type"], r["name"], r["source"]) for r in rows] == [("storage (pbs)", "pbs-lab", "storage")]


def test_iscsi_portal_with_port_and_ceph_monhost_list(cli, lab):
    lab.route("GET", "/storage", [
        {"storage": "san", "type": "iscsi", "portal": "10.9.9.9:3260", "target": "iqn.x"},
        {"storage": "ext-ceph", "type": "rbd", "monhost": "10.9.9.1 10.9.9.2 10.9.9.3", "pool": "rbd"},
    ])
    assert {r["name"] for r in find_json(cli, "10.9.9.9")} == {"san"}
    assert {r["name"] for r in find_json(cli, "10.9.9.2")} == {"ext-ceph"}


def test_firewall_alias_containing_the_ip(cli, lab):
    lab.route("GET", "/cluster/firewall/aliases", [{"name": "lab-net", "cidr": "10.77.30.0/24", "ipversion": 4}])
    rows = find_json(cli, "10.77.30.100")
    assert ("lab-net", "firewall") in hits(rows)
    assert (300, "IPAM pve") in hits(rows)


def test_unknown_ip_finds_nothing(cli, lab):
    r = cli("find", "192.0.2.77", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "Nothing found" in r.output


# ── MAC, volume, network ──────────────────────────────────────────────


def test_mac_of_a_qemu_nic_any_case(cli, lab):
    rows = find_json(cli, "bc:24:11:ee:5a:c4")
    assert hits(rows) == {(300, "config"), (300, "IPAM pve")}
    assert next(r for r in rows if r["source"] == "config")["match"] == "net0 BC:24:11:EE:5A:C4"


def test_mac_with_dashes(cli, lab):
    assert (300, "config") in hits(find_json(cli, "BC-24-11-EE-5A-C4"))


def test_mac_of_an_lxc_nic(cli, lab):
    rows = find_json(cli, "BC:24:11:A6:9E:69")
    assert hits(rows) == {(311, "config"), (311, "IPAM pve")}
    assert next(r for r in rows if r["source"] == "config")["match"] == "net0 BC:24:11:A6:9E:69"


def test_volume_owner(cli, lab):
    rows = find_json(cli, "vm-301-disk-0")
    assert hits(rows) == {(301, "config")}
    assert rows[0]["match"] == "scsi0 local-lvm:vm-301-disk-0"


def test_lxc_rootfs_volume_with_storage_prefix(cli, lab):
    rows = find_json(cli, "local-lvm:vm-311-disk-0")
    assert hits(rows) == {(311, "config")}
    assert rows[0]["match"].startswith("rootfs ")


def test_unused_volume_is_reported_orphaned(cli, lab):
    r = cli("find", "vm-555-disk-0", "-p", "p1")
    assert r.exit_code == 0
    assert "orphaned" in r.output


def test_volume_name_is_matched_exactly(cli, lab):
    CONFIGS_301 = dict(CONFIGS[301], scsi1="local-lvm:vm-301-disk-10,size=4G")
    lab.route("GET", "/nodes/{node}/qemu/301/config", CONFIGS_301)
    assert find_json(cli, "vm-301-disk-1") == []


def test_network_search(cli, lab):
    rows = find_json(cli, "10.77.30.0/24")
    assert hits(rows) == {
        (300, "IPAM pve"), (311, "IPAM pve"), (311, "lxc"), (320, "agent"),
        (9000, "IPAM pve"), ("vsimple", "IPAM pve"),
    }
    # Guests first by ID, infrastructure last
    assert [r["vmid"] for r in rows] == [300, 311, 311, 320, 9000, None]


# ── filters ───────────────────────────────────────────────────────────


def test_filter_on_vnet(cli, lab):
    rows = find_json(cli, "--on-vnet", "vsimple")
    assert {r["vmid"] for r in rows} == {300, 311, 320, 9000}


def test_filter_stopped(cli, lab):
    assert {r["vmid"] for r in find_json(cli, "--stopped")} == {300, 301, 9000}


def test_filters_combine_with_a_query(cli, lab):
    rows = find_json(cli, "10.77.30.0/24", "--stopped")
    # Infrastructure rows drop out as soon as a guest filter applies
    assert hits(rows) == {(300, "IPAM pve"), (9000, "IPAM pve")}


def test_filter_no_backup(cli, lab):
    # 320 is covered by a job: not in the recorded not-backed-up list
    assert {r["vmid"] for r in find_json(cli, "--no-backup")} == {300, 301, 311, 9000}
    assert lab.called("GET", "/cluster/backup-info/not-backed-up")


def test_filter_on_storage_names_the_disks(cli, lab):
    rows = find_json(cli, "--on-storage", "local-lvm")
    assert {r["vmid"]: r["match"] for r in rows} == {
        301: "disks ide2, scsi0", 311: "disks rootfs", 9000: "disks scsi0",
    }
    assert {r["vmid"] for r in find_json(cli, "--on-storage", "labpool")} == {320}


def test_no_query_and_no_filter_is_refused(cli, lab):
    r = cli("find", "-p", "p1")
    assert r.exit_code == 1
    assert "at least one filter" in r.output
    assert lab.calls == []


# ── access ────────────────────────────────────────────────────────────

ACL = [
    {"path": "/vms/301", "type": "user", "ugid": "alice@pve", "roleid": "PVEVMUser", "propagate": 0},
    {"path": "/vms", "type": "group", "ugid": "ops", "roleid": "PVEVMAdmin", "propagate": 1},
    {"path": "/", "type": "user", "ugid": "bob@pve", "roleid": "PVEAuditor", "propagate": 0},
    {"path": "/vms/300", "type": "token", "ugid": "alice@pve!ci", "roleid": "PVEVMUser", "propagate": 1},
    {"path": "/storage", "type": "group", "ugid": "ops", "roleid": "PVEDatastoreUser", "propagate": 1},
]


def test_access_lists_direct_and_inherited_acls(cli, lab):
    lab.route("GET", "/access/acl", ACL)
    rows = find_json(cli, "--access", "301")
    # bob on / does not propagate: it does not reach /vms/301
    assert {(r["who"], r["path"]) for r in rows} == {("alice@pve", "/vms/301"), ("ops", "/vms")}
    assert lab.writes() == []


def test_access_on_unknown_guest_fails(cli, lab):
    lab.route("GET", "/access/acl", ACL)
    r = cli("find", "--access", "4242", "-p", "p1")
    assert r.exit_code == 1
    assert "Guest 4242 not found" in r.output


def test_user_acl_direct_and_through_group(cli, lab):
    lab.route("GET", "/access/acl", ACL)
    # GET /access/users/{userid} answers groups as a list
    lab.route("GET", "/access/users/alice@pve", {"userid": "alice@pve", "enable": 1, "groups": ["ops"]})
    rows = find_json(cli, "--user", "alice@pve")
    assert {(r["who"], r["kind"], r["path"]) for r in rows} == {
        ("alice@pve", "user", "/vms/301"), ("ops", "group", "/vms"), ("ops", "group", "/storage"),
    }


def test_user_token_acl(cli, lab):
    lab.route("GET", "/access/acl", ACL)
    lab.route("GET", "/access/users/alice@pve", {"userid": "alice@pve", "enable": 1, "groups": ["ops"]})
    rows = find_json(cli, "--user", "alice@pve!ci")
    assert {(r["who"], r["kind"], r["path"]) for r in rows} == {("alice@pve!ci", "token", "/vms/300")}


def test_access_table_output(cli, lab):
    lab.route("GET", "/access/acl", fixture("pve-9.1/access_acl"))
    r = cli("find", "--access", "300", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "No ACL entry found" in r.output


# ── output ────────────────────────────────────────────────────────────


def test_table_output(cli, lab):
    r = cli("find", "10.77.30.101", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "fixct" in r.stdout and "IPAM pve" in r.stdout and "eth0" in r.stdout


def test_json_is_one_valid_document(cli, lab):
    r = cli("find", "bc:24:11:ee:5a:c4", "-p", "p1", "--json")
    assert r.exit_code == 0
    doc = json.loads(r.stdout)
    assert isinstance(doc, list) and doc
    assert set(doc[0]) == {"type", "vmid", "name", "node", "status", "match", "source"}


# ── what this user could not search ──────────────────────────────────


def test_pool_limited_user_is_told_what_was_not_searched(cli, lab):
    # Seen on the lab with PVEAuditor on one pool: the node address and a
    # guest outside the pool both gave a bare "Nothing found"
    lab.route("GET", "/access/permissions", lambda c: {c.params["path"]: {}})
    lab.route("GET", "/cluster/status", lambda c: lab.forbidden("/", "Sys.Audit"))
    r = cli("find", "10.199.32.51", "-p", "p1", "--json")
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout) == []
    assert "VM.Audit on /vms" in r.stderr
    assert "/cluster/status" in r.stderr and "Sys.Audit" in r.stderr


def test_full_reader_gets_no_scope_warning(cli, lab):
    r = cli("find", "10.20.3.14", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "not searched" not in r.stderr.lower()
