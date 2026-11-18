"""pvecli ct image list/add/remove: which image volume is listed, downloaded or deleted."""

from __future__ import annotations

import subprocess

import pytest

from tests.fake_api import upid

DEBIAN = "local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst"
ALPINE = "local:vztmpl/alpine-3.20-default_20240908_amd64.tar.xz"
CONTENT = [
    {"volid": ALPINE, "content": "vztmpl", "format": "txz", "size": 3000000, "ctime": 1725800000},
    {"volid": DEBIAN, "content": "vztmpl", "format": "tzst", "size": 120000000, "ctime": 1725800000},
]
NODE_STORAGE = [
    {"storage": "local", "type": "dir", "content": "backup,import,vztmpl,iso", "active": 1, "enabled": 1},
    {"storage": "local-lvm", "type": "lvmthin", "content": "rootdir,images", "active": 1, "enabled": 1},
]
# GET /nodes/{node}/aplinfo: one entry per image of the Proxmox index
APLINFO = [
    {"template": "debian-12-standard_12.7-1_amd64.tar.zst", "package": "debian-12-standard", "section": "system",
     "os": "debian-12", "type": "lxc", "version": "12.7-1",
     "location": "http://download.proxmox.com/images/system/debian-12-standard_12.7-1_amd64.tar.zst",
     "sha512sum": "39f6d06e082d6a418438483da4f76092ebd0370a91bad30b82ab6d0f442234d63fe27a15569895e34d6d1e5e4e6d4b2e"},
    {"template": "alpine-3.20-default_20240908_amd64.tar.xz", "package": "alpine-3.20-default", "section": "system",
     "os": "alpine", "type": "lxc", "version": "3.20-0",
     "location": "http://download.proxmox.com/images/system/alpine-3.20-default_20240908_amd64.tar.xz",
     "sha512sum": "aa"},
]


def setup(api, nodes=("pve1",), storage=None, content=None) -> None:
    api.route("GET", "/nodes", [{"node": n, "status": "online"} for n in nodes])
    api.route("GET", "/nodes/{node}/storage", storage if storage is not None else NODE_STORAGE)
    api.route("GET", "/nodes/{node}/storage/{storage}/content", content if content is not None else CONTENT)
    api.route("GET", "/nodes/{node}/aplinfo", APLINFO)
    api.route("POST", "/nodes/{node}/aplinfo", lambda c: upid(c.match["node"], "download"))
    api.route("POST", "/nodes/{node}/storage/{storage}/download-url", lambda c: upid(c.match["node"], "download"))
    # The volume id holds a slash: route the exact paths
    for vol in (DEBIAN, ALPINE):
        api.route("DELETE", f"/nodes/pve1/storage/local/content/{vol}", None)


@pytest.fixture
def fzf(monkeypatch):
    """Replace the fzf picker: set .stdout / .returncode / .missing before the run."""
    class Fzf:
        stdout = ""
        returncode = 0
        missing = False
        seen: list[str] = []

    def run(cmd, *a, input=None, **k):
        assert cmd[0] == "fzf", cmd
        if Fzf.missing:
            raise FileNotFoundError("fzf")
        Fzf.seen = input.splitlines()
        return subprocess.CompletedProcess(cmd, Fzf.returncode, Fzf.stdout, "")

    monkeypatch.setattr(subprocess, "run", run)
    return Fzf


# ── list ────────────────────────────────────────────────────────────────


def test_list_named_storage(cli, api):
    setup(api)
    r = cli("ct", "image", "list", "pve1", "local", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "debian-12-standard_12.7-1_amd64.tar.zst" in r.output
    assert "alpine-3.20-default_20240908_amd64.tar.xz" in r.output
    [get] = api.called("GET", "/nodes/pve1/storage/local/content")
    assert get.params == {"content": "vztmpl"}


def test_list_single_node_and_single_image_storage_are_implied(cli, api, ui):
    setup(api)
    r = cli("ct", "image", "list", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert api.called("GET", "/nodes/pve1/storage/local/content")
    assert ui.asked == []


def test_list_asks_node_and_storage_when_several(cli, api, ui):
    storage = [*NODE_STORAGE, {"storage": "nfs", "type": "nfs", "content": "vztmpl,backup", "active": 1}]
    setup(api, nodes=("pve2", "pve1"), storage=storage)
    ui.menu("pve2", "nfs")
    r = cli("ct", "image", "list", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert api.called("GET", "/nodes/pve2/storage/nfs/content")
    assert "local-lvm" not in ui.asked[1][1]


def test_list_empty_storage(cli, api):
    setup(api, content=[])
    r = cli("ct", "image", "list", "pve1", "local", "-p", "p1")
    assert r.exit_code == 0
    assert "No images found" in r.output


def test_list_node_without_image_storage(cli, api):
    setup(api, storage=[NODE_STORAGE[1]])
    r = cli("ct", "image", "list", "pve1", "-p", "p1")
    assert r.exit_code == 0
    assert "No image storage" in r.output


# ── add ─────────────────────────────────────────────────────────────────


def test_add_by_name_lets_the_node_fetch_and_verify(cli, api):
    setup(api)
    r = cli("ct", "image", "add", "pve1", "local", "--name", "debian-12-standard_12.7-1_amd64.tar.zst", "-p", "p1")
    assert r.exit_code == 0, r.output
    [post] = api.writes()
    assert post.path == "/nodes/pve1/aplinfo"
    assert post.data == {"storage": "local", "template": "debian-12-standard_12.7-1_amd64.tar.zst"}


def test_add_picked_with_fzf_downloads_each_selection(cli, api, fzf):
    setup(api)
    fzf.stdout = "debian-12-standard_12.7-1_amd64.tar.zst\nalpine-3.20-default_20240908_amd64.tar.xz\n"
    r = cli("ct", "image", "add", "pve1", "local", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert fzf.seen == [a["template"] for a in APLINFO]
    assert [(c.path, c.data) for c in api.writes()] == [
        ("/nodes/pve1/storage/local/download-url",
         {"url": APLINFO[0]["location"], "content": "vztmpl", "filename": APLINFO[0]["template"]}),
        ("/nodes/pve1/storage/local/download-url",
         {"url": APLINFO[1]["location"], "content": "vztmpl", "filename": APLINFO[1]["template"]}),
    ]


def test_add_fzf_escape_writes_nothing(cli, api, fzf):
    setup(api)
    fzf.returncode = 130
    r = cli("ct", "image", "add", "pve1", "local", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


def test_add_without_fzf_is_an_error(cli, api, fzf):
    setup(api)
    fzf.missing = True
    r = cli("ct", "image", "add", "pve1", "local", "-p", "p1")
    assert r.exit_code == 1
    assert "fzf is not installed" in r.output
    assert api.writes() == []


def test_add_index_unreachable_asks_the_file_name(cli, api, ui):
    setup(api)
    api.route("GET", "/nodes/{node}/aplinfo", api.error(500, "unable to read appliance index"))
    ui.text("debian-12-standard_12.7-1_amd64.tar.zst")
    r = cli("ct", "image", "add", "pve1", "local", "-p", "p1")
    assert r.exit_code == 0, r.output
    [post] = api.writes()
    assert post.path == "/nodes/pve1/aplinfo"
    assert post.data["template"] == "debian-12-standard_12.7-1_amd64.tar.zst"


def test_add_index_unreachable_and_no_name(cli, api, ui):
    setup(api)
    api.route("GET", "/nodes/{node}/aplinfo", [])
    ui.text("")
    r = cli("ct", "image", "add", "pve1", "local", "-p", "p1")
    assert r.exit_code == 1
    assert api.writes() == []


def test_add_picks_node_from_menu(cli, api, ui):
    setup(api, nodes=("pve1", "pve2"))
    ui.menu("pve2")
    r = cli("ct", "image", "add", "--name", "x.tar.zst", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [c.path for c in api.writes()] == ["/nodes/pve2/aplinfo"]


def test_add_node_menu_cancelled(cli, api, ui):
    setup(api, nodes=("pve1", "pve2"))
    ui.menu(None)
    r = cli("ct", "image", "add", "--name", "x.tar.zst", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


def test_add_failed_download_exits_non_zero(cli, api):
    setup(api)
    api.route("GET", "/nodes/{node}/tasks/{upid}/status", {"status": "stopped", "exitstatus": "checksum mismatch"})
    r = cli("ct", "image", "add", "pve1", "local", "--name", "x.tar.zst", "-p", "p1")
    assert r.exit_code == 1
    assert "checksum mismatch" in r.output


# ── remove ──────────────────────────────────────────────────────────────


def test_remove_by_name_deletes_the_volume(cli, api):
    setup(api)
    r = cli("ct", "image", "remove", "pve1", "local", "--name", "debian-12-standard_12.7-1_amd64.tar.zst",
            "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    [delete] = api.writes()
    assert (delete.method, delete.path) == ("DELETE", f"/nodes/pve1/storage/local/content/{DEBIAN}")


def test_remove_unknown_name_writes_nothing(cli, api):
    setup(api)
    r = cli("ct", "image", "remove", "pve1", "local", "--name", "debian-12", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "not found" in r.output
    assert api.writes() == []


def test_remove_declined_writes_nothing(cli, api, ui):
    setup(api)
    ui.confirm(False)
    r = cli("ct", "image", "remove", "pve1", "local", "--name", "debian-12-standard_12.7-1_amd64.tar.zst", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []
    assert "cannot be undone" in ui.asked[0][1]


def test_remove_from_menu(cli, api, ui):
    setup(api)
    ui.menu("alpine")
    ui.confirm(True)
    r = cli("ct", "image", "remove", "pve1", "local", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [c.path for c in api.writes()] == [f"/nodes/pve1/storage/local/content/{ALPINE}"]


def test_remove_menu_cancelled(cli, api, ui):
    setup(api)
    ui.menu(None)
    r = cli("ct", "image", "remove", "pve1", "local", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


def test_remove_from_empty_storage(cli, api):
    setup(api, content=[])
    r = cli("ct", "image", "remove", "pve1", "local", "--name", "x", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert api.writes() == []
