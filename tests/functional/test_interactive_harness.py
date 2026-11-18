"""The scripted UI drives menus and prompts of the real commands."""

import pytest

from tests.fake_api import upid
from tests.interactive import UnexpectedQuestion

VMS = [{"vmid": 100, "type": "qemu", "node": "pve1", "status": "stopped", "name": "web"},
       {"vmid": 101, "type": "qemu", "node": "pve1", "status": "stopped", "name": "db"}]


def _routes(api):
    api.route("GET", "/cluster/resources", VMS)
    api.route("GET", "/nodes/{node}/qemu", VMS)
    api.route("GET", "/nodes", [{"node": "pve1", "status": "online"}])
    api.route("GET", "/nodes/{node}/qemu/{vmid}/status/current", {"status": "stopped"})
    api.route("POST", "/nodes/{node}/qemu/{vmid}/status/start", upid("pve1", "qmstart", 101))


def test_menu_answer_by_label(cli, api, ui):
    _routes(api)
    ui.multi(["db"])
    r = cli("vm", "start", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert api.called("POST", "/nodes/pve1/qemu/101/status/start")
    assert not ui.left()


def test_cancelled_menu_writes_nothing(cli, api, ui):
    _routes(api)
    ui.multi(None)
    cli("vm", "start", "-p", "p1")
    assert api.writes() == []


def test_unexpected_question_fails_loudly(cli, api, ui):
    _routes(api)
    with pytest.raises(UnexpectedQuestion):
        cli("vm", "start", "-p", "p1")
