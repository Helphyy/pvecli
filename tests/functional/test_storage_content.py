"""`storage content list/add/download/remove`.

PVE endpoints: GET /nodes/{node}/storage/{storage}/content, multipart POST
.../upload (content + filename), POST .../download-url (url, content,
filename, checksum, checksum-algorithm), DELETE .../content/{volume}.
"""

from urllib.parse import unquote

import httpx
import pytest

from tests.fake_api import upid

NODES = [{"node": "pve1", "status": "online"}, {"node": "pve2", "status": "online"}]
NODE_STORAGES = [
    {"storage": "local", "type": "dir", "content": "iso,vztmpl,backup", "active": 1, "enabled": 1},
    {"storage": "nas", "type": "nfs", "content": "iso,backup", "active": 1, "enabled": 1, "shared": 1},
]
CONTENT = [
    {"volid": "local:iso/debian-13.iso", "content": "iso", "format": "iso", "size": 700 * 2**20},
    {"volid": "local:backup/vzdump-qemu-100-2026_11_01-00_00_00.vma.zst", "content": "backup",
     "format": "vma.zst", "size": 2 * 2**30, "vmid": 100},
    {"volid": "local:vztmpl/debian-13-standard_13.1-1_amd64.tar.zst", "content": "vztmpl",
     "format": "tzst", "size": 120 * 2**20},
]


@pytest.fixture
def lab(api):
    api.route("GET", "/nodes", NODES)
    api.route("GET", "/nodes/{node}/storage", NODE_STORAGES)
    api.route("GET", "/nodes/{node}/storage/{storage}/content", CONTENT)
    return api


def deleted(api):
    return [unquote(c.path.split("/content/", 1)[1]) for c in api.writes() if c.method == "DELETE"]


# ── list ─────────────────────────────────────────────────────────────────


def test_list_content(cli, lab):
    r = cli("storage", "content", "list", "pve1", "local", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "local:iso/debian-13.iso" in r.output
    assert "100" in r.output
    call = lab.called("GET", "/nodes/pve1/storage/local/content")[0]
    assert call.params is None


def test_list_content_filtered_by_type(cli, lab):
    cli("storage", "content", "list", "pve1", "local", "-t", "iso", "-p", "p1")
    assert lab.called("GET", "/nodes/pve1/storage/local/content")[0].params == {"content": "iso"}


def test_list_empty(cli, lab):
    lab.route("GET", "/nodes/{node}/storage/{storage}/content", [])
    r = cli("storage", "content", "list", "pve1", "local", "-p", "p1")
    assert r.exit_code == 0
    assert "No content found" in r.output


def test_list_picks_node_and_storage_in_menus(cli, lab, ui):
    ui.menu("pve2", "nas")
    r = cli("storage", "content", "list", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert lab.called("GET", "/nodes/pve2/storage/nas/content")


def test_list_unknown_storage_is_an_error(cli, lab):
    lab.route("GET", "/nodes/{node}/storage/{storage}/content",
              lab.error(500, "storage 'nope' does not exist"))
    r = cli("storage", "content", "list", "pve1", "nope", "-p", "p1")
    assert r.exit_code == 1
    assert "does not exist" in r.output


# ── remove (destructive) ─────────────────────────────────────────────────


def test_remove_one_volume_with_yes(cli, lab):
    lab.route("DELETE", "/nodes/pve1/storage/local/content/local:iso/debian-13.iso", None)
    r = cli("storage", "content", "remove", "pve1", "local", "local:iso/debian-13.iso", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert deleted(lab) == ["local:iso/debian-13.iso"]
    assert all(c.path.startswith("/nodes/pve1/storage/local/content/") for c in lab.writes())


def test_remove_declined_writes_nothing(cli, lab, ui):
    ui.confirm(False)
    r = cli("storage", "content", "remove", "pve1", "local", "local:iso/debian-13.iso", "-p", "p1")
    assert r.exit_code == 0
    assert lab.writes() == []


def test_remove_several_from_menu(cli, lab, ui):
    lab.route("DELETE", "/nodes/pve1/storage/local/content/{a}/{b}", None)
    ui.multi(["debian-13.iso", "vztmpl"])
    ui.confirm(True)
    r = cli("storage", "content", "remove", "pve1", "local", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert deleted(lab) == ["local:iso/debian-13.iso", "local:vztmpl/debian-13-standard_13.1-1_amd64.tar.zst"]
    assert "2 volumes deleted" in r.output
    # The backup was not selected and stays
    assert not any("vzdump" in v for v in deleted(lab))


@pytest.mark.parametrize("answer", [None, []])
def test_remove_menu_cancelled_or_empty_writes_nothing(cli, lab, ui, answer):
    ui.multi(answer)
    r = cli("storage", "content", "remove", "pve1", "local", "-p", "p1")
    assert r.exit_code == 0
    assert lab.writes() == []


def test_remove_on_empty_storage(cli, lab):
    lab.route("GET", "/nodes/{node}/storage/{storage}/content", [])
    r = cli("storage", "content", "remove", "pve1", "local", "-p", "p1")
    assert r.exit_code == 0
    assert "No content" in r.output
    assert lab.writes() == []


def test_remove_refused_by_pve(cli, lab):
    lab.route("DELETE", "/nodes/{node}/storage/{storage}/content/{volume}",
              lab.error(500, "volume is in use"))
    r = cli("storage", "content", "remove", "pve1", "local", "local-lvm:vm-100-disk-0", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "volume is in use" in r.output


def test_remove_node_menu_cancelled(cli, lab, ui):
    ui.menu(None)
    cli("storage", "content", "remove", "-p", "p1")
    assert lab.writes() == []


# ── add (upload) ─────────────────────────────────────────────────────────


class FakeHTTP:
    """Stands for the httpx client: upload_storage_content posts multipart itself."""

    def __init__(self, status=200, body=None):
        self.status = status
        self.body = body if body is not None else {"data": upid("pve1", "imgcopy", "")}
        self.requests = []

    async def request(self, method, url, headers=None, data=None, files=None):
        name, fh, _ = files["filename"]
        self.requests.append({"method": method, "url": url, "data": dict(data), "name": name,
                              "bytes": fh.read()})
        return httpx.Response(self.status, json=self.body, request=httpx.Request(method, url))


@pytest.fixture
def http(monkeypatch):
    from src.api.client import ProxmoxClient

    fake = FakeHTTP()
    monkeypatch.setattr(ProxmoxClient, "_ensure_connected", lambda self: fake)
    return fake


@pytest.fixture
def iso(tmp_path):
    f = tmp_path / "alpine-3.21.iso"
    f.write_bytes(b"\x00ISO" * 256)
    return f


def test_upload_iso(cli, lab, http, iso):
    r = cli("storage", "content", "add", "pve1", "local", "-s", str(iso), "-t", "iso", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    [req] = http.requests
    assert req["method"] == "POST"
    assert req["url"].endswith("/nodes/pve1/storage/local/upload")
    assert req["data"] == {"content": "iso"}
    assert req["name"] == "alpine-3.21.iso"
    assert req["bytes"] == iso.read_bytes()
    # The task is followed to its end
    assert lab.called("GET", "/nodes/pve1/tasks/{upid}/status")
    assert "uploaded to 'local'" in r.output


def test_upload_proxied_by_another_node_polls_that_node(cli, lab, http, iso):
    http.body = {"data": upid("pve2", "imgcopy", "")}
    r = cli("storage", "content", "add", "pve1", "nas", "-s", str(iso), "-t", "iso", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "executed by 'pve2'" in r.output
    assert lab.called("GET", "/nodes/pve2/tasks/{upid}/status")


def test_upload_task_failure_is_an_error(cli, lab, http, iso):
    lab.route("GET", "/nodes/{node}/tasks/{upid}/status",
              {"status": "stopped", "exitstatus": "import failed: no space left on device"})
    r = cli("storage", "content", "add", "pve1", "local", "-s", str(iso), "-t", "iso", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "no space left" in r.output


def test_upload_rejected_by_pve(cli, lab, http, iso):
    http.status, http.body = 403, {"data": None}
    r = cli("storage", "content", "add", "pve1", "local", "-s", str(iso), "-t", "iso", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "Permission denied" in r.output


def test_upload_missing_file_sends_nothing(cli, lab, http, tmp_path):
    r = cli("storage", "content", "add", "pve1", "local", "-s", str(tmp_path / "nope.iso"),
            "-t", "iso", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "File not found" in r.output
    assert http.requests == []


def test_upload_bad_type_sends_nothing(cli, lab, http, iso):
    r = cli("storage", "content", "add", "pve1", "local", "-s", str(iso), "-t", "backup", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "Invalid content type 'backup'" in r.output
    assert http.requests == []


def test_upload_interactive_type_and_confirm(cli, lab, http, iso, ui):
    ui.text(str(iso))
    ui.menu("vztmpl")
    ui.confirm(True)
    r = cli("storage", "content", "add", "pve1", "local", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert http.requests[0]["data"] == {"content": "vztmpl"}


def test_upload_declined_sends_nothing(cli, lab, http, iso, ui):
    ui.confirm(False)
    r = cli("storage", "content", "add", "pve1", "local", "-s", str(iso), "-t", "iso", "-p", "p1")
    assert r.exit_code == 0
    assert http.requests == []
    assert lab.writes() == []


def test_upload_empty_path_refused(cli, lab, http, ui):
    ui.text("   ")
    r = cli("storage", "content", "add", "pve1", "local", "-p", "p1")
    assert r.exit_code == 1
    assert http.requests == []


# ── download from URL ────────────────────────────────────────────────────

URL = "https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/debian-13.2.0-amd64-netinst.iso"
SHA = "a" * 64


def test_download_iso_with_checksum(cli, lab):
    lab.route("POST", "/nodes/{node}/storage/{storage}/download-url", upid("pve1", "download", ""))
    r = cli("storage", "content", "download", "pve1", "local", "-u", URL, "-f", "netinst.iso",
            "-c", SHA, "-ca", "sha256", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    [call] = lab.writes()
    assert call.path == "/nodes/pve1/storage/local/download-url"
    assert call.data == {"url": URL, "filename": "netinst.iso", "content": "iso",
                         "checksum": SHA, "checksum-algorithm": "sha256"}
    assert lab.called("GET", "/nodes/pve1/tasks/{upid}/status")


def test_download_filename_from_url_and_template_type_detected(cli, lab, ui):
    lab.route("POST", "/nodes/{node}/storage/{storage}/download-url", upid("pve1", "download", ""))
    ui.text(None)  # accept the filename taken from the URL
    url = "http://download.proxmox.com/images/system/debian-13-standard_13.1-1_amd64.tar.zst"
    r = cli("storage", "content", "download", "pve1", "local", "-u", url, "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert lab.writes()[0].data == {"url": url, "filename": "debian-13-standard_13.1-1_amd64.tar.zst",
                                    "content": "vztmpl"}


def test_download_url_encoded_filename_is_decoded(cli, lab, ui):
    lab.route("POST", "/nodes/{node}/storage/{storage}/download-url", upid("pve1", "download", ""))
    ui.text(None)
    r = cli("storage", "content", "download", "pve1", "local", "-u", "https://x.org/my%20disk.iso",
            "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert lab.writes()[0].data["filename"] == "my disk.iso"


def test_download_background_does_not_wait(cli, lab):
    lab.route("POST", "/nodes/{node}/storage/{storage}/download-url", upid("pve1", "download", ""))
    r = cli("storage", "content", "download", "pve1", "local", "-u", URL, "-f", "a.iso", "-b", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "background" in r.output
    assert not lab.called("GET", "/nodes/{node}/tasks/{upid}/status")


def test_download_unknown_extension_asks_the_type(cli, lab, ui):
    lab.route("POST", "/nodes/{node}/storage/{storage}/download-url", upid("pve1", "download", ""))
    ui.menu("iso")
    r = cli("storage", "content", "download", "pve1", "local", "-u", "https://x.org/get?id=3",
            "-f", "image.bin", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert lab.writes()[0].data["content"] == "iso"


def test_download_declined_writes_nothing(cli, lab, ui):
    ui.confirm(False)
    r = cli("storage", "content", "download", "pve1", "local", "-u", URL, "-f", "a.iso", "-p", "p1")
    assert r.exit_code == 0
    assert lab.writes() == []


def test_download_bad_type_writes_nothing(cli, lab):
    r = cli("storage", "content", "download", "pve1", "local", "-u", URL, "-f", "a.iso", "-t", "backup",
            "-y", "-p", "p1")
    assert r.exit_code == 1
    assert lab.writes() == []


def test_download_task_failure_is_an_error(cli, lab):
    lab.route("POST", "/nodes/{node}/storage/{storage}/download-url", upid("pve1", "download", ""))
    lab.route("GET", "/nodes/{node}/tasks/{upid}/status",
              {"status": "stopped", "exitstatus": "checksum mismatch"})
    r = cli("storage", "content", "download", "pve1", "local", "-u", URL, "-f", "a.iso", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "checksum mismatch" in r.output


def test_download_checksum_never_sent_without_its_algorithm(cli, lab):
    lab.route("POST", "/nodes/{node}/storage/{storage}/download-url", upid("pve1", "download", ""))
    r = cli("storage", "content", "download", "pve1", "local", "-u", URL, "-f", "a.iso", "-c", SHA,
            "-y", "-p", "p1")
    posts = lab.writes()
    if posts:
        assert posts[0].data.get("checksum-algorithm") == "sha256"
    else:
        assert r.exit_code != 0


# Found on a PVE 8.4 lab: `-y` still prompted for the filename (a script
# aborted on it), and `import` was refused although PVE 8.4 and 9 accept it
# for download-url (the way to fetch a cloud image for import-from).

def test_download_yes_takes_the_url_filename_without_asking(cli, lab):
    lab.route("POST", "/nodes/{node}/storage/{storage}/download-url", upid("pve1", "download", ""))
    r = cli("storage", "content", "download", "pve1", "local", "-u", URL, "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert lab.writes()[0].data["filename"] == "debian-13.2.0-amd64-netinst.iso"


@pytest.mark.parametrize("name", ["debian-12-genericcloud-amd64.qcow2", "disk.raw", "disk.vmdk", "appliance.ova"])
def test_download_disk_image_is_detected_as_import(cli, lab, name):
    lab.route("POST", "/nodes/{node}/storage/{storage}/download-url", upid("pve1", "download", ""))
    r = cli("storage", "content", "download", "pve1", "local", "-u", f"https://x.org/{name}", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert lab.writes()[0].data == {"url": f"https://x.org/{name}", "filename": name, "content": "import"}


def test_download_import_type_is_accepted(cli, lab):
    lab.route("POST", "/nodes/{node}/storage/{storage}/download-url", upid("pve1", "download", ""))
    r = cli("storage", "content", "download", "pve1", "local", "-u", "https://x.org/get?id=3",
            "-f", "img.qcow2", "-t", "import", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert lab.writes()[0].data["content"] == "import"
