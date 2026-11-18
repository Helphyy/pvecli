"""pool add / remove / list / info / export / import against the fake API.

What Proxmox does (PVE::API2::Pool):
- POST /pools creates one pool (poolid, optional comment), and refuses an
  existing id ("pool '<id>' already exists").
- DELETE /pools/{poolid} refuses a pool that still has members
  ("pool '<id>' is not empty"): there is no force flag on the API side.
- GET /pools lists {poolid, comment}; GET /pools/{poolid} returns
  {comment, members}, and answers 500 with an empty body for an unknown pool.
"""

from __future__ import annotations

import json

from tests.fake_api import Call

POOLS = [{"poolid": "staging", "comment": "pre-prod"}, {"poolid": "dev"}]

DEV_MEMBERS = [
    {"id": "qemu/101", "type": "qemu", "vmid": 101, "name": "db", "node": "pve1", "status": "running"},
    {"id": "qemu/100", "type": "qemu", "vmid": 100, "name": "web", "node": "pve1", "status": "running"},
    {"id": "lxc/200", "type": "lxc", "vmid": 200, "name": "dns", "node": "pve2", "status": "stopped"},
    {"id": "storage/pve1/local", "type": "storage", "storage": "local", "node": "pve1"},
]


def _pools(api, pools=POOLS):
    api.route("GET", "/pools", pools)


# ── pool list ────────────────────────────────────────────────────────────


def test_list_sorted_with_comments(cli, api):
    _pools(api)
    r = cli("pool", "list", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert r.stdout.index("dev") < r.stdout.index("staging")
    assert "pre-prod" in r.stdout


def test_list_json_is_raw_and_sorted(cli, api):
    _pools(api)
    r = cli("pool", "list", "-p", "p1", "--json")
    assert r.exit_code == 0, r.output
    doc = json.loads(r.stdout)
    assert doc["profile"] == "p1"
    assert [p["poolid"] for p in doc["pools"]] == ["dev", "staging"]


def test_list_json_empty_is_valid(cli, api):
    _pools(api, [])
    r = cli("pool", "list", "-p", "p1", "--json")
    assert r.exit_code == 0
    assert json.loads(r.stdout)["pools"] == []


def test_list_empty_text(cli, api):
    _pools(api, [])
    r = cli("pool", "list", "-p", "p1")
    assert r.exit_code == 0
    assert "No pools found" in r.output


def test_list_api_error_exits_1(cli, api):
    api.route("GET", "/pools", api.forbidden("/pool", "Pool.Audit"))
    r = cli("pool", "list", "-p", "p1")
    assert r.exit_code == 1
    assert "Pool.Audit" in r.output


# ── pool info ────────────────────────────────────────────────────────────


def test_info_groups_members_by_type(cli, api):
    api.route("GET", "/pools/dev", {"comment": "team dev", "members": DEV_MEMBERS})
    r = cli("pool", "info", "dev", "-p", "p1")
    assert r.exit_code == 0, r.output
    out = r.stdout
    assert "team dev" in out
    assert "VMs (2)" in out and "Containers (1)" in out and "Storages (1)" in out
    # VMs sorted by vmid
    assert out.index("100 - web") < out.index("101 - db")
    assert "200 - dns (node: pve2)" in out


def test_info_json(cli, api):
    api.route("GET", "/pools/dev", {"members": DEV_MEMBERS})
    r = cli("pool", "info", "dev", "-p", "p1", "--json")
    assert r.exit_code == 0, r.output
    doc = json.loads(r.stdout)
    assert doc["poolid"] == "dev"
    assert doc["comment"] is None
    assert len(doc["members"]) == 4


def test_info_empty_pool(cli, api):
    api.route("GET", "/pools/dev", {"members": []})
    r = cli("pool", "info", "dev", "-p", "p1")
    assert r.exit_code == 0
    assert "No members" in r.stdout


def test_info_unknown_pool_is_an_error(cli, api):
    # PVE answers 500 with an empty body for an unknown pool
    api.route("GET", "/pools/nope", api.error(500, ""))
    r = cli("pool", "info", "nope", "-p", "p1")
    assert r.exit_code == 1
    assert "nope" in r.output


def test_info_json_without_id_refuses_menu(cli, api):
    _pools(api)
    r = cli("pool", "info", "-p", "p1", "--json")
    assert r.exit_code == 1
    assert "--json requires a pool ID" in r.output


def test_info_menu_selects_pool(cli, api, ui):
    _pools(api)
    api.route("GET", "/pools/staging", {"members": []})
    ui.menu("staging")
    r = cli("pool", "info", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "Pool: staging" in r.stdout
    assert not ui.left()


def test_info_menu_cancel(cli, api, ui):
    _pools(api)
    ui.menu(None)
    r = cli("pool", "info", "-p", "p1")
    assert r.exit_code == 0
    assert not api.called("GET", "/pools/{poolid}")


# ── pool add ─────────────────────────────────────────────────────────────


def test_add_several_with_comment(cli, api):
    api.route("POST", "/pools", None)
    r = cli("pool", "add", "dev, staging;prod", "-c", "team", "-p", "p1")
    assert r.exit_code == 0, r.output
    sent = [c.data for c in api.called("POST", "/pools")]
    assert sent == [
        {"poolid": "dev", "comment": "team"},
        {"poolid": "staging", "comment": "team"},
        {"poolid": "prod", "comment": "team"},
    ]


def test_add_without_comment_sends_no_comment(cli, api):
    api.route("POST", "/pools", None)
    r = cli("pool", "add", "dev", "-p", "p1")
    assert r.exit_code == 0
    assert api.called("POST", "/pools")[0].data == {"poolid": "dev"}


def test_add_existing_pool_reports_pve_error(cli, api):
    api.route("POST", "/pools", api.error(500, "create pool failed: pool 'dev' already exists"))
    r = cli("pool", "add", "dev", "-p", "p1")
    assert r.exit_code == 1
    assert "already exists" in r.output


def test_add_interactive_loop(cli, api, ui):
    api.route("POST", "/pools", None)
    ui.text("dev", "qa").confirm(True, False)
    r = cli("pool", "add", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [c.data["poolid"] for c in api.called("POST", "/pools")] == ["dev", "qa"]
    assert not ui.left()


def test_add_interactive_empty_name_writes_nothing(cli, api, ui):
    ui.text("")
    r = cli("pool", "add", "-p", "p1")
    assert r.exit_code == 1
    assert api.writes() == []


def test_add_only_separators_is_refused(cli, api):
    r = cli("pool", "add", ",;", "-p", "p1")
    assert r.exit_code == 1
    assert api.writes() == []


# ── pool remove ──────────────────────────────────────────────────────────


def _stateful_pools(api, members):
    """A fake PVE that refuses DELETE on a pool with members, like the real one."""
    state = {pid: list(m) for pid, m in members.items()}
    api.route("GET", "/pools", lambda c: [{"poolid": p} for p in state])
    api.route("GET", "/pools/{poolid}", lambda c: {"members": state[c.match["poolid"]]})

    def delete(c: Call):
        pid = c.match["poolid"]
        if state[pid]:
            return api.error(500, f"pool '{pid}' is not empty\n")
        del state[pid]
        return None

    def put(c: Call):
        pid = c.match["poolid"]
        data = c.data or {}
        if data.get("delete"):
            vms = {int(v) for v in str(data.get("vms", "")).split(",") if v}
            sts = {s for s in str(data.get("storage", "")).split(",") if s}
            state[pid] = [
                m for m in state[pid]
                if not (m.get("vmid") in vms or m.get("storage") in sts)
            ]
        return None

    api.route("DELETE", "/pools/{poolid}", delete)
    api.route("PUT", "/pools/{poolid}", put)
    return state


def test_remove_empty_pool_with_yes(cli, api):
    state = _stateful_pools(api, {"dev": [], "staging": []})
    r = cli("pool", "remove", "dev", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [c.path for c in api.writes()] == ["/pools/dev"]
    assert "dev" not in state


def test_remove_confirm_refused_writes_nothing(cli, api, ui):
    _stateful_pools(api, {"dev": []})
    ui.confirm(False)
    r = cli("pool", "remove", "dev", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []
    assert "dev" in ui.asked[0][1]


def test_remove_non_empty_pool_is_blocked_without_force(cli, api):
    _stateful_pools(api, {"dev": DEV_MEMBERS})
    r = cli("pool", "remove", "dev", "-y", "-p", "p1")
    assert api.writes() == []
    assert "2 VM(s)" in r.output and "1 CT(s)" in r.output and "1 storage(s)" in r.output
    assert "--force" in r.output


def test_remove_force_empties_then_deletes(cli, api):
    state = _stateful_pools(api, {"dev": DEV_MEMBERS})
    r = cli("pool", "remove", "dev", "--force", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "dev" not in state
    # The guests are only detached from the pool, never destroyed
    assert not [c for c in api.writes() if c.path.startswith("/nodes/")]


def test_remove_multi_select_only_deletes_empty_ones(cli, api, ui):
    _stateful_pools(api, {"a": [], "b": DEV_MEMBERS, "c": []})
    ui.multi(["a", "b", "c"]).confirm(True)
    r = cli("pool", "remove", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert sorted(c.path for c in api.called("DELETE", "/pools/{poolid}")) == ["/pools/a", "/pools/c"]
    assert "2 pools deleted" in r.output
    assert "2 pools (a, c)" in ui.asked[-1][1]


def test_remove_menu_cancel_writes_nothing(cli, api, ui):
    _stateful_pools(api, {"a": []})
    ui.multi(None)
    r = cli("pool", "remove", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []


def test_remove_empty_selection_writes_nothing(cli, api, ui):
    _stateful_pools(api, {"a": []})
    ui.multi([])
    r = cli("pool", "remove", "-p", "p1")
    assert api.writes() == []
    assert r.exit_code == 0


def test_remove_no_pools(cli, api):
    _pools(api, [])
    r = cli("pool", "remove", "dev", "-y", "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []
    assert "No pools found" in r.output


def test_remove_delete_error_exits_1(cli, api):
    _pools(api, [{"poolid": "dev"}])
    api.route("GET", "/pools/dev", {"members": []})
    api.route("DELETE", "/pools/dev", api.forbidden("/pool/dev", "Pool.Allocate"))
    r = cli("pool", "remove", "dev", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "Pool.Allocate" in r.output


# ── pool export / import ─────────────────────────────────────────────────


def test_export_writes_ids_and_comments(cli, api, tmp_path):
    _pools(api)
    out = tmp_path / "out.json"
    r = cli("pool", "export", "-o", str(out), "-p", "p1")
    assert r.exit_code == 0, r.output
    assert json.loads(out.read_text()) == {
        "pools": [{"poolid": "dev"}, {"poolid": "staging", "comment": "pre-prod"}]
    }
    assert api.writes() == []


def test_export_nothing_writes_no_file(cli, api, tmp_path):
    _pools(api, [])
    out = tmp_path / "out.json"
    r = cli("pool", "export", "-o", str(out), "-p", "p1")
    assert r.exit_code == 0
    assert not out.exists()


def test_export_then_import_roundtrip(cli, api, tmp_path):
    _pools(api)
    out = tmp_path / "pools.json"
    assert cli("pool", "export", "-o", str(out), "-p", "p1").exit_code == 0
    # Another cluster, with no pool yet
    api.route("GET", "/pools", [], host="h2")
    api.route("POST", "/pools", None)
    r = cli("pool", "import", str(out), "-p", "p2")
    assert r.exit_code == 0, r.output
    posts = api.called("POST", "/pools")
    assert [c.host for c in posts] == ["h2", "h2"]
    assert [c.data for c in posts] == [{"poolid": "dev"}, {"poolid": "staging", "comment": "pre-prod"}]


def test_import_force_skips_existing_silently(cli, api, tmp_path):
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"pools": [{"poolid": "dev"}, {"poolid": "new", "comment": "x"}, {"comment": "no id"}]}))
    _pools(api, [{"poolid": "dev"}])
    api.route("POST", "/pools", None)
    r = cli("pool", "import", str(f), "--force", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert [c.data for c in api.called("POST", "/pools")] == [{"poolid": "new", "comment": "x"}]
    assert "1 pool(s) skipped" in r.output


def test_import_existing_asks_and_skips(cli, api, tmp_path, ui):
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"pools": [{"poolid": "dev"}]}))
    _pools(api, [{"poolid": "dev"}])
    ui.confirm(True)
    r = cli("pool", "import", str(f), "-p", "p1")
    assert r.exit_code == 0
    assert api.writes() == []
    assert "already exists" in r.output


def test_import_existing_not_skipped_updates_comment(cli, api, tmp_path, ui):
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"pools": [{"poolid": "dev", "comment": "from file"}]}))
    _pools(api, [{"poolid": "dev", "comment": "old"}])
    api.route("PUT", "/pools/{poolid}", None)
    ui.confirm(False)
    r = cli("pool", "import", str(f), "-p", "p1")
    assert r.exit_code == 0
    assert [(c.method, c.path, c.data) for c in api.writes()] == [
        ("PUT", "/pools/dev", {"comment": "from file"})
    ]


def test_import_missing_file(cli, api, tmp_path):
    r = cli("pool", "import", str(tmp_path / "absent.json"), "-p", "p1")
    assert r.exit_code == 1
    assert "File not found" in r.output
    assert api.calls == []


def test_import_invalid_json(cli, api, tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("{not json")
    r = cli("pool", "import", str(f), "-p", "p1")
    assert r.exit_code == 1
    assert "Invalid JSON" in r.output
    assert api.writes() == []


def test_import_empty_file(cli, api, tmp_path):
    f = tmp_path / "e.json"
    f.write_text(json.dumps({"pools": []}))
    r = cli("pool", "import", str(f), "-p", "p1")
    assert r.exit_code == 0
    assert "No pools found in file" in r.output
    assert api.writes() == []
