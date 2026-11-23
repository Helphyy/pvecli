"""`storage add`: what reaches POST /storage for each backend, and the scan
that precedes it.

Payload rules come from the PVE storage API: nfs needs server and export,
cifs server and share (plus username and password for a protected share),
iscsi portal and target, pbs server, datastore, username, password and the
fingerprint of a self-signed certificate. For an external Ceph cluster, rbd
stores the whole keyring file while cephfs wants the bare secret key.
"""

import pytest

from tests.fake_api import fixture

NODES = [{"node": "pve1", "status": "online"}, {"node": "pve2", "status": "online"},
         {"node": "pve3", "status": "offline"}]
ACTIVE = {"active": 1, "enabled": 1, "total": 10 * 2**40, "avail": 4 * 2**40, "used": 6 * 2**40}

KEY = "AQBvR0xmAAAAABAAx1Yc1Wq0uQ2c8b1Yj1n4kQ=="
KEYRING = f"[client.admin]\n\tkey = {KEY}\n\tcaps mds = \"allow *\"\n\tcaps mon = \"allow *\"\n"


@pytest.fixture
def cluster(api):
    api.route("GET", "/nodes", NODES)
    api.route("GET", "/storage", fixture("pve-9.1/storage"))
    api.route("POST", "/storage", None)
    api.route("GET", "/nodes/{node}/storage/{storage}/status", ACTIVE)
    return api


def created(api):
    posts = api.called("POST", "/storage")
    assert len(posts) == 1, api.writes()
    return posts[0].data


# ── NFS ──────────────────────────────────────────────────────────────────


def test_nfs_scanned_from_every_online_node_then_created(cli, cluster):
    cluster.route("GET", "/nodes/{node}/scan/nfs", [{"path": "/export/pve", "options": "10.0.0.0/24"}])
    r = cli("storage", "add", "nas1", "-ty", "nfs", "--server", "10.0.0.5", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    scans = cluster.called("GET", "/nodes/{node}/scan/nfs")
    # The offline node is never asked
    assert sorted(c.match["node"] for c in scans) == ["pve1", "pve2"]
    assert all(c.params == {"server": "10.0.0.5"} for c in scans)
    assert created(cluster) == {
        "storage": "nas1", "type": "nfs", "server": "10.0.0.5", "export": "/export/pve",
        "content": "images,rootdir,vztmpl,iso,backup",
    }
    # Shared backends never carry 'shared': PVE rejects it for them
    assert "shared" not in created(cluster)
    assert "active on all 2 node(s)" in r.output


def test_nfs_several_exports_with_yes_refuses_instead_of_guessing(cli, cluster):
    cluster.route("GET", "/nodes/{node}/scan/nfs", [{"path": "/a"}, {"path": "/b"}])
    r = cli("storage", "add", "nas1", "-ty", "nfs", "--server", "10.0.0.5", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "--export" in r.output
    assert cluster.writes() == []


def test_nfs_export_flag_kept_even_when_the_scan_does_not_list_it(cli, cluster):
    cluster.route("GET", "/nodes/{node}/scan/nfs", [{"path": "/other"}])
    r = cli("storage", "add", "nas1", "-ty", "nfs", "--server", "10.0.0.5", "--export", "/mine",
            "-cn", "iso,backup", "-ns", "pve1", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert created(cluster) == {
        "storage": "nas1", "type": "nfs", "server": "10.0.0.5", "export": "/mine",
        "content": "iso,backup", "nodes": "pve1",
    }
    # Verification only on the restricted node
    checked = {c.match["node"] for c in cluster.called("GET", "/nodes/{node}/storage/{storage}/status")}
    assert checked == {"pve1"}


def test_nfs_unreachable_from_every_node_creates_nothing(cli, cluster):
    cluster.route("GET", "/nodes/{node}/scan/nfs", cluster.error(500, "mount error: timed out"))
    r = cli("storage", "add", "nas1", "-ty", "nfs", "--server", "10.9.9.9", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "No node could reach 10.9.9.9" in r.output
    assert cluster.writes() == []


def test_nfs_reachable_from_some_nodes_restricts_on_request(cli, cluster, ui):
    def scan(call):
        if call.match["node"] == "pve2":
            raise cluster.error(500, "clnt_create: RPC: Unable to receive")
        return [{"path": "/export/pve"}]

    cluster.route("GET", "/nodes/{node}/scan/nfs", scan)
    ui.confirm(True, True)  # restrict to reachable nodes, then create
    r = cli("storage", "add", "nas1", "-ty", "nfs", "--server", "10.0.0.5", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert created(cluster)["nodes"] == "pve1"
    assert not ui.left()


def test_nfs_declined_summary_writes_nothing(cli, cluster, ui):
    cluster.route("GET", "/nodes/{node}/scan/nfs", [{"path": "/export/pve"}])
    ui.confirm(False)
    r = cli("storage", "add", "nas1", "-ty", "nfs", "--server", "10.0.0.5", "-p", "p1")
    assert r.exit_code == 0
    assert cluster.writes() == []


def test_created_but_inactive_everywhere_is_an_error(cli, cluster):
    cluster.route("GET", "/nodes/{node}/scan/nfs", [{"path": "/export/pve"}])
    cluster.route("GET", "/nodes/{node}/storage/{storage}/status", {"active": 0, "enabled": 1})
    r = cli("storage", "add", "nas1", "-ty", "nfs", "--server", "10.0.0.5", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "active on no node" in r.output
    assert "pvecli storage remove nas1" in r.output


def test_created_but_inactive_on_one_node_warns(cli, cluster):
    cluster.route("GET", "/nodes/{node}/scan/nfs", [{"path": "/export/pve"}])

    def status(call):
        return ACTIVE if call.match["node"] == "pve1" else {"active": 0, "enabled": 1}

    cluster.route("GET", "/nodes/{node}/storage/{storage}/status", status)
    r = cli("storage", "add", "nas1", "-ty", "nfs", "--server", "10.0.0.5", "-y", "-p", "p1")
    assert r.exit_code == 0
    assert "not active on: pve2" in r.output


# ── Refusals before any write ────────────────────────────────────────────


@pytest.mark.parametrize("args, message", [
    (("nas1", "-ty", "zfs"), "Unsupported type 'zfs'"),
    (("nas1", "-ty", "pbs", "-cn", "iso"), "not supported by 'pbs'"),
    (("nas1", "-ty", "nfs", "-cn", ","), "At least one content type"),
    (("1bad", "-ty", "nfs"), "Invalid storage ID '1bad'"),
    (("x", "-ty", "nfs"), "Invalid storage ID 'x'"),
    (("local", "-ty", "nfs"), "'local' already exists"),
    (("nas1", "-ty", "nfs", "-ns", "pve9"), "Unknown node(s): pve9"),
])
def test_bad_input_refused_without_writing(cli, cluster, args, message):
    r = cli("storage", "add", *args, "--server", "10.0.0.5", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert message in r.output
    assert cluster.writes() == []
    assert not cluster.called("GET", "/nodes/{node}/scan/{kind}")


def test_no_online_node(cli, cluster):
    cluster.route("GET", "/nodes", [{"node": "pve1", "status": "offline"}])
    r = cli("storage", "add", "nas1", "-ty", "nfs", "--server", "x", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "No online node" in r.output


def test_create_refused_by_pve_is_reported(cli, cluster):
    cluster.route("GET", "/nodes/{node}/scan/nfs", [{"path": "/export/pve"}])
    cluster.route("POST", "/storage", cluster.error(400, "storage ID 'nas1' already defined"))
    r = cli("storage", "add", "nas1", "-ty", "nfs", "--server", "10.0.0.5", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "already defined" in r.output


# ── CIFS ─────────────────────────────────────────────────────────────────


def test_cifs_password_from_env_with_username(cli, cluster, monkeypatch):
    monkeypatch.setenv("SMBPW", "s3cret")
    cluster.route("GET", "/nodes/{node}/scan/cifs", [{"share": "data", "description": ""}])
    r = cli("storage", "add", "smb1", "-ty", "cifs", "--server", "10.0.0.6", "--share", "data",
            "--username", "bob", "--password-env", "SMBPW", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert created(cluster) == {
        "storage": "smb1", "type": "cifs", "server": "10.0.0.6", "share": "data",
        "username": "bob", "password": "s3cret", "content": "images,rootdir,vztmpl,iso,backup",
    }
    # The password never shows in the summary
    assert "s3cret" not in r.output


def test_cifs_scan_of_a_protected_share_carries_the_credentials(cli, cluster, monkeypatch):
    monkeypatch.setenv("SMBPW", "s3cret")

    def scan(call):
        # A share without guest access: smbclient -N is refused
        if (call.params or {}).get("password") != "s3cret":
            raise cluster.error(500, "NT_STATUS_ACCESS_DENIED")
        return [{"share": "data"}]

    cluster.route("GET", "/nodes/{node}/scan/cifs", scan)
    r = cli("storage", "add", "smb1", "-ty", "cifs", "--server", "10.0.0.6", "--share", "data",
            "--username", "bob", "--password-env", "SMBPW", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert all(c.params.get("username") == "bob" for c in cluster.called("GET", "/nodes/{node}/scan/cifs"))
    assert created(cluster)["password"] == "s3cret"


def test_password_env_unset_refused(cli, cluster, monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    cluster.route("GET", "/nodes/{node}/scan/cifs", [{"share": "data"}])
    r = cli("storage", "add", "smb1", "-ty", "cifs", "--server", "10.0.0.6", "--share", "data",
            "--password-env", "NOPE", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "'NOPE' is empty or unset" in r.output
    assert cluster.writes() == []


def test_cifs_interactive_password_prompt(cli, cluster, ui):
    cluster.route("GET", "/nodes/{node}/scan/cifs", [{"share": "data"}])
    ui.menu("cifs")                       # type
    ui.text("smb1", "10.0.0.6", "pw", "alice")  # id, server, password, username
    ui.confirm(True, True)                # needs a password, create
    ui.multi(["iso"])
    r = cli("storage", "add", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert created(cluster) == {
        "storage": "smb1", "type": "cifs", "server": "10.0.0.6", "share": "data",
        "password": "pw", "username": "alice", "content": "iso",
    }
    assert not ui.left()


# ── iSCSI ────────────────────────────────────────────────────────────────


def test_iscsi_scan_by_portal_single_target(cli, cluster):
    cluster.route("GET", "/nodes/{node}/scan/iscsi",
                  [{"target": "iqn.2003-01.org.linux-iscsi:san", "portal": "10.0.0.9:3260"}])
    r = cli("storage", "add", "san1", "-ty", "iscsi", "--portal", "10.0.0.9", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert all(c.params == {"portal": "10.0.0.9"} for c in cluster.called("GET", "/nodes/{node}/scan/iscsi"))
    assert created(cluster) == {
        "storage": "san1", "type": "iscsi", "portal": "10.0.0.9",
        "target": "iqn.2003-01.org.linux-iscsi:san", "content": "images",
    }


def test_iscsi_choice_among_targets_in_menu(cli, cluster, ui):
    def scan(call):
        targets = [{"target": "iqn.a"}]
        if call.match["node"] == "pve1":
            targets.append({"target": "iqn.b"})
        return targets

    cluster.route("GET", "/nodes/{node}/scan/iscsi", scan)
    ui.menu("iqn.b")
    ui.confirm(True)
    r = cli("storage", "add", "san1", "-ty", "iscsi", "--portal", "10.0.0.9", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert created(cluster)["target"] == "iqn.b"
    # The target only pve1 sees is flagged in the menu
    menu = [q for kind, q in ui.asked if kind == "menu"][0]
    assert "iqn.b   [seen on some nodes only]" in menu


def test_iscsi_cancelled_menu_writes_nothing(cli, cluster, ui):
    cluster.route("GET", "/nodes/{node}/scan/iscsi", [{"target": "iqn.a"}, {"target": "iqn.b"}])
    ui.menu(None)
    cli("storage", "add", "san1", "-ty", "iscsi", "--portal", "10.0.0.9", "-p", "p1")
    assert cluster.writes() == []


# ── PBS ──────────────────────────────────────────────────────────────────

FP = "cd:6e:f4:9a:2f:82:d9:a8:e7:04:12:a8:23:5e:1f:43:07:10:b4:2b:7f:c3:af:b0:f8:30:6d:75:98:26:b6:a5"


def test_pbs_with_fingerprint_and_credentials(cli, cluster, monkeypatch):
    monkeypatch.setenv("PBSPW", "pbspass")
    cluster.route("GET", "/nodes/{node}/scan/pbs", [{"store": "ds1"}])
    r = cli("storage", "add", "pbs2", "-ty", "pbs", "--server", "10.0.0.70", "--username", "backup@pbs",
            "--password-env", "PBSPW", "--fingerprint", FP, "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    scan = cluster.called("GET", "/nodes/{node}/scan/pbs")[0]
    assert scan.params == {"server": "10.0.0.70", "username": "backup@pbs", "password": "pbspass",
                           "fingerprint": FP}
    assert created(cluster) == {
        "storage": "pbs2", "type": "pbs", "server": "10.0.0.70", "fingerprint": FP,
        "username": "backup@pbs", "password": "pbspass", "datastore": "ds1", "content": "backup",
    }
    assert "pbspass" not in r.output


def test_pbs_untrusted_certificate_names_the_fingerprint(cli, cluster, monkeypatch):
    monkeypatch.setenv("PBSPW", "pbspass")
    cluster.route("GET", "/nodes/{node}/scan/pbs", cluster.error(
        500, f"error fetching datastores - fingerprint '{FP.upper()}' not verified, abort!"))
    r = cli("storage", "add", "pbs2", "-ty", "pbs", "--server", "10.0.0.70", "--username", "backup@pbs",
            "--password-env", "PBSPW", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert f"--fingerprint {FP}" in r.output
    assert cluster.writes() == []


def test_pbs_without_credentials_skips_discovery_and_needs_datastore(cli, cluster):
    r = cli("storage", "add", "pbs2", "-ty", "pbs", "--server", "10.0.0.70", "--datastore", "ds1",
            "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "Discovery skipped" in r.output
    assert not cluster.called("GET", "/nodes/{node}/scan/pbs")
    assert created(cluster) == {"storage": "pbs2", "type": "pbs", "server": "10.0.0.70",
                                "datastore": "ds1", "content": "backup"}


# ── External Ceph: rbd keeps the keyring, cephfs the bare key ────────────


def test_rbd_sends_the_whole_keyring(cli, cluster, tmp_path):
    kf = tmp_path / "ceph.client.admin.keyring"
    kf.write_text(KEYRING)
    r = cli("storage", "add", "ext-rbd", "-ty", "rbd", "--monhost", "10.1.0.1,10.1.0.2",
            "--pool", "vms", "--keyring-file", str(kf), "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert created(cluster) == {
        "storage": "ext-rbd", "type": "rbd", "monhost": "10.1.0.1,10.1.0.2", "username": "admin",
        "pool": "vms", "keyring": KEYRING.strip(), "content": "images,rootdir",
    }
    assert KEY not in r.output
    # No scan endpoint for Ceph
    assert not [c for c in cluster.calls if "/scan/" in c.path]


def test_cephfs_extracts_the_bare_key_from_a_keyring(cli, cluster, tmp_path):
    kf = tmp_path / "ceph.client.pve.keyring"
    kf.write_text(KEYRING.replace("admin", "pve"))
    r = cli("storage", "add", "ext-fs", "-ty", "cephfs", "--monhost", "10.1.0.1", "--username", "pve",
            "--fs-name", "data", "--keyring-file", str(kf), "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert created(cluster) == {
        "storage": "ext-fs", "type": "cephfs", "monhost": "10.1.0.1", "username": "pve",
        "fs-name": "data", "keyring": KEY, "content": "vztmpl,iso,backup",
    }


def test_cephfs_accepts_a_bare_key_file_as_is(cli, cluster, tmp_path):
    kf = tmp_path / "pve.secret"
    kf.write_text(KEY + "\n")
    r = cli("storage", "add", "ext-fs", "-ty", "cephfs", "--monhost", "10.1.0.1",
            "--keyring-file", str(kf), "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert created(cluster)["keyring"] == KEY
    assert created(cluster)["fs-name"] == "cephfs"


def test_ceph_missing_keyring_file_refused(cli, cluster, tmp_path):
    r = cli("storage", "add", "ext-rbd", "-ty", "rbd", "--monhost", "10.1.0.1",
            "--keyring-file", str(tmp_path / "missing"), "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "Keyring file not found" in r.output
    assert cluster.writes() == []


def test_ceph_without_keyring_warns_and_creates(cli, cluster):
    r = cli("storage", "add", "ext-rbd", "-ty", "rbd", "--monhost", "10.1.0.1", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "No keyring provided" in r.output
    assert "keyring" not in created(cluster)
    assert created(cluster)["pool"] == "rbd"


def test_rbd_interactive_wizard(cli, cluster, ui, tmp_path):
    kf = tmp_path / "k"
    kf.write_text(KEYRING)
    ui.menu("rbd")
    ui.text("ext-rbd", "10.1.0.1 10.1.0.2", None, "vms", str(kf))  # id, monhost, client id (default), pool, keyring
    ui.multi(["images"])
    ui.confirm(True)
    r = cli("storage", "add", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert created(cluster) == {
        "storage": "ext-rbd", "type": "rbd", "monhost": "10.1.0.1 10.1.0.2", "username": "admin",
        "pool": "vms", "keyring": KEYRING.strip(), "content": "images",
    }
    assert not ui.left()


def test_wizard_cancelled_at_type_writes_nothing(cli, cluster, ui):
    ui.menu(None)
    r = cli("storage", "add", "-p", "p1")
    assert r.exit_code == 0
    assert cluster.writes() == []


def test_cephfs_takes_the_key_of_the_requested_client(cli, cluster, tmp_path):
    other = "AQAAAAAAAAAAABAAadminadminadminadminad=="
    kf = tmp_path / "ceph.keyring"
    # A keyring file can hold several entities (ceph auth export)
    kf.write_text(f"[client.admin]\n\tkey = {other}\n[client.pve]\n\tkey = {KEY}\n")
    r = cli("storage", "add", "ext-fs", "-ty", "cephfs", "--monhost", "10.1.0.1", "--username", "pve",
            "--keyring-file", str(kf), "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert created(cluster)["keyring"] == KEY
