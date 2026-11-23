"""`storage content remove` waits for the deletion task of each volume.

PVE answers DELETE .../content/{volume} with the UPID of an imgdel task.
Sent back to back, the second task can fail to start ("got no worker upid
- start worker failed"), so each volume waits for the task of the previous
one, and a failed task is an error, not a deleted volume.
"""

import pytest

from tests.fake_api import upid

NODES = [{"node": "pve1", "status": "online"}]
NODE_STORAGES = [{"storage": "local", "type": "dir", "content": "iso", "active": 1, "enabled": 1}]
CONTENT = [
    {"volid": "local:iso/a.iso", "content": "iso", "format": "iso", "size": 2**20},
    {"volid": "local:iso/b.iso", "content": "iso", "format": "iso", "size": 2**20},
    {"volid": "local:iso/c.iso", "content": "iso", "format": "iso", "size": 2**20},
]


@pytest.fixture
def lab(api):
    api.route("GET", "/nodes", NODES)
    api.route("GET", "/nodes/{node}/storage", NODE_STORAGES)
    api.route("GET", "/nodes/{node}/storage/{storage}/content", CONTENT)
    api.route("DELETE", "/nodes/pve1/storage/local/content/{a}/{b}",
              lambda call: upid(kind="imgdel", vmid="local"))
    return api


def _order(api):
    """The calls of the deletions, in order: DELETE, then its task status."""
    return [("DELETE" if c.method == "DELETE" else "status") for c in api.calls
            if c.method == "DELETE" or c.path.endswith("/status")]


def test_each_deletion_waits_for_the_previous_task(cli, lab, ui):
    ui.multi(["a.iso", "b.iso", "c.iso"])
    ui.confirm(True)
    r = cli("storage", "content", "remove", "pve1", "local", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert _order(lab) == ["DELETE", "status"] * 3
    assert "3 volumes deleted" in r.output


def test_a_failed_task_stops_and_says_what_was_deleted(cli, lab, ui):
    statuses = iter([{"status": "stopped", "exitstatus": "OK"},
                     {"status": "stopped", "exitstatus": "unexpected status"}])
    lab.route("GET", "/nodes/{node}/tasks/{upid}/status", lambda call: next(statuses))
    ui.multi(["a.iso", "b.iso", "c.iso"])
    ui.confirm(True)
    r = cli("storage", "content", "remove", "pve1", "local", "-p", "p1")
    assert r.exit_code == 1
    assert "1 of 3 volumes deleted before 'local:iso/b.iso'" in r.output
    assert "Failed to delete 'local:iso/b.iso'" in r.output
    assert "unexpected status" in r.output
    # The third volume is not sent after the failure
    assert len(lab.called("DELETE", "/nodes/pve1/storage/local/content/{a}/{b}")) == 2
    assert "\n3 volumes deleted" not in r.output
