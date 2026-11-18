"""The test harness itself: fake API, isolated config, CLI runner."""

from tests.fake_api import upid


def test_node_list_goes_through_the_fake_api(cli, api):
    api.route("GET", "/nodes", [{"node": "pve1", "status": "online", "cpu": 0.1, "maxcpu": 4,
                                 "mem": 1, "maxmem": 2, "disk": 1, "maxdisk": 2, "uptime": 60}])
    r = cli("node", "list", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "pve1" in r.stdout
    assert api.called("GET", "/nodes")


def test_unknown_route_is_a_clear_failure(cli, api):
    r = cli("node", "list", "-p", "p1")
    assert r.exit_code == 1
    assert "no fake route" in r.output


def test_real_profiles_are_never_used(home):
    from src.config import ConfigManager

    assert set(ConfigManager().list_profiles()) == {"p1", "p2", "p3", "prod"}


def test_task_wait_uses_the_fake_status(cli, api):
    api.route("GET", "/cluster/resources", [{"vmid": 100, "type": "qemu", "node": "pve1", "status": "stopped", "name": "a"}])
    api.route("GET", "/nodes/{node}/qemu/{vmid}/status/current", {"status": "stopped"})
    api.route("POST", "/nodes/{node}/qemu/{vmid}/status/start", upid("pve1", "qmstart", 100))
    r = cli("vm", "start", "100", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert len(api.called("POST", "/nodes/pve1/qemu/100/status/start")) == 1
