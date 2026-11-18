"""ceph set / unset: maintenance flags through the API, no SSH.

cluster shutdown/reboot leave noout and friends set on purpose; clearing
them must not need `ceph osd unset` on a node (seen on lab1, 2026-11-18).
"""

from tests.fake_api import fixture


def _flags(api):
    api.route("GET", "/cluster/ceph/flags", fixture("pve-9.1/ceph_flags"))
    api.route("PUT", "/cluster/ceph/flags/{flag}", None)


def test_unset_several_flags_in_one_command(cli, api):
    _flags(api)
    r = cli("ceph", "unset", "noout,nobackfill", "-p", "p1")
    assert r.exit_code == 0, r.output
    calls = [(c.path, c.data) for c in api.called("PUT", "/cluster/ceph/flags/{flag}")]
    assert calls == [
        ("/cluster/ceph/flags/noout", {"value": 0}),
        ("/cluster/ceph/flags/nobackfill", {"value": 0}),
    ]


def test_set_a_flag(cli, api):
    _flags(api)
    r = cli("ceph", "set", "noout", "-p", "p1")
    assert r.exit_code == 0, r.output
    (call,) = api.called("PUT", "/cluster/ceph/flags/{flag}")
    assert (call.path, call.data) == ("/cluster/ceph/flags/noout", {"value": 1})


def test_unknown_flag_is_refused_before_any_write(cli, api):
    _flags(api)
    r = cli("ceph", "unset", "noout,nooot", "-p", "p1")
    assert r.exit_code == 1
    assert "nooot" in r.output
    assert api.writes() == []

