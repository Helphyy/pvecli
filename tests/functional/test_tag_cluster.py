"""Cluster wide tags: list, add, edit (rename, color), remove, export and import.

Proxmox keeps guest tags in each guest config (`tags: a;b`) and the colors
in the single datacenter option `tag-style`, returned parsed as an object
(`{"color-map": "web:ff0000;db:00ff00:000000", "shape": "circle"}`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

RES = [
    {"vmid": 100, "type": "qemu", "node": "pve1", "status": "running", "name": "a", "tags": "web"},
    {"vmid": 101, "type": "qemu", "node": "pve1", "status": "running", "name": "b", "tags": "db;web"},
    {"vmid": 200, "type": "lxc", "node": "pve2", "status": "running", "name": "c", "tags": "web;prod"},
    {"vmid": 201, "type": "lxc", "node": "pve2", "status": "stopped", "name": "d"},
]


@pytest.fixture(autouse=True)
def _palette_in_home(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The palette path is computed at import time: point it at the test HOME
    so no test can ever touch the real ~/.config/pvecli/tag_color.yml."""
    from src.cli import tag

    path = home / ".config" / "pvecli" / "tag_color.yml"
    monkeypatch.setattr(tag, "_COLOR_FILE", path)
    return path


def _style(value: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in value.split(","))


def _colors(value: str) -> set[str]:
    return set(_style(value)["color-map"].split(";"))


def _guest_writes(api) -> dict[str, dict]:
    return {c.path: c.data for c in api.writes() if c.path != "/cluster/options"}


def _options_writes(api) -> list[dict]:
    return [c.data for c in api.writes() if c.path == "/cluster/options"]


def _routes(api, resources=RES, style=None) -> None:
    api.route("GET", "/cluster/resources", resources)
    # Each guest's own config: what a removal rewrites from
    by_id = {r["vmid"]: r for r in resources}
    api.route("GET", "/nodes/{node}/{kind}/{vmid}/config",
              lambda c: {k: v for k, v in by_id[int(c.match["vmid"])].items() if k == "tags"})
    api.route("GET", "/cluster/options", {"keyboard": "fr", **({"tag-style": style} if style else {})})
    api.route("PUT", "/cluster/options", None)
    api.route("PUT", "/nodes/{node}/{kind}/{vmid}/config", None)


# ── list ─────────────────────────────────────────────────────────────────


def test_list_counts_vms_and_cts_and_shows_colored_but_unused_tags(cli, api):
    _routes(api, style={"color-map": "web:ff0000;spare:00ff00:000000"})
    r = cli("tag", "list", "-p", "p1")
    assert r.exit_code == 0, r.output
    rows = {line.split()[1]: line.split() for line in r.stdout.splitlines() if line.startswith("│")}
    # web: VM 100, VM 101, CT 200
    assert "#ff0000" in rows["web"]
    assert [x for x in rows["web"] if x.isdigit()] == ["2", "1", "3"]
    assert [x for x in rows["db"] if x.isdigit()] == ["1", "0", "1"]
    assert [x for x in rows["prod"] if x.isdigit()] == ["0", "1", "1"]
    # A color without any guest is still a tag of the cluster
    assert [x for x in rows["spare"] if x.isdigit()] == ["0", "0", "0"]
    assert "#00ff00" in " ".join(rows["spare"])
    assert api.writes() == []


def test_list_without_any_tag(cli, api):
    _routes(api, resources=[RES[3]])
    r = cli("tag", "list", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "No tags found" in r.output


def test_list_order_by_unknown_column_fails(cli, api):
    _routes(api)
    r = cli("tag", "list", "-o", "nope", "-p", "p1")
    assert r.exit_code == 1


def test_list_reads_tag_style_given_as_a_string(cli, api):
    # Older answers and datacenter.cfg itself carry the property string
    _routes(api, resources=[], style="shape=circle,color-map=web:123456")
    r = cli("tag", "list", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "web" in r.stdout and "#123456" in r.stdout


# ── add ──────────────────────────────────────────────────────────────────


def test_add_with_color_on_a_cluster_without_tag_style(cli, api):
    _routes(api)
    r = cli("tag", "add", "web", "-c", "ff4444", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert _options_writes(api) == [{"tag-style": "color-map=web:ff4444"}]
    assert _guest_writes(api) == {}


def test_add_replaces_the_color_of_an_existing_tag(cli, api):
    _routes(api, style={"color-map": "web:ff0000;db:00ff00"})
    r = cli("tag", "add", "web", "-c", "#0000ff", "-p", "p1")
    assert r.exit_code == 0, r.output
    [data] = _options_writes(api)
    assert _colors(data["tag-style"]) == {"web:0000ff", "db:00ff00"}


def test_add_prompts_for_the_name_and_cancels_without_a_palette(cli, api, ui):
    # No --color and no palette file: nothing to pick from, nothing written
    _routes(api)
    ui.text("web")
    r = cli("tag", "add", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "tag color init" in r.output
    assert api.writes() == []
    assert ui.left() == {}


def test_add_refuses_an_empty_name(cli, api, ui):
    _routes(api)
    ui.text("   ")
    r = cli("tag", "add", "-p", "p1")
    assert r.exit_code == 1
    assert "cannot be empty" in r.output
    assert api.writes() == []


def test_add_with_a_picked_color(cli, api, monkeypatch):
    from src.cli import tag

    monkeypatch.setattr(tag, "_pick_color", lambda name: "2266cc")
    _routes(api)
    r = cli("tag", "add", "web", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert _options_writes(api) == [{"tag-style": "color-map=web:2266cc"}]


def test_add_reports_an_api_error(cli, api):
    _routes(api)
    api.route("PUT", "/cluster/options", api.forbidden("/", "Sys.Modify"))
    r = cli("tag", "add", "web", "-c", "ff4444", "-p", "p1")
    assert r.exit_code == 1
    assert "Permission" in r.output


# ── edit ─────────────────────────────────────────────────────────────────


def test_edit_rename_rewrites_every_vm_and_ct_and_moves_the_color(cli, api):
    _routes(api, style={"color-map": "web:ff0000:000000;db:00ff00", "shape": "circle"})
    r = cli("tag", "edit", "web", "--name", "front", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert _guest_writes(api) == {
        "/nodes/pve1/qemu/100/config": {"tags": "front"},
        # The position of the tag among the others is kept
        "/nodes/pve1/qemu/101/config": {"tags": "db;front"},
        "/nodes/pve2/lxc/200/config": {"tags": "front;prod"},
    }
    [data] = _options_writes(api)
    style = _style(data["tag-style"])
    assert style["shape"] == "circle"
    # The foreground color travels with the tag
    assert set(style["color-map"].split(";")) == {"front:ff0000:000000", "db:00ff00"}
    assert "renamed to 'front' on 3 resource(s)" in r.output


def test_edit_rename_into_an_existing_tag_merges_after_confirmation(cli, api, ui):
    _routes(api, style={"color-map": "web:ff0000;db:00ff00"})
    ui.confirm(True)
    r = cli("tag", "edit", "web", "--name", "db", "-p", "p1")
    assert r.exit_code == 0, r.output
    writes = _guest_writes(api)
    # VM 101 carried both: the tag appears once
    assert writes["/nodes/pve1/qemu/101/config"] == {"tags": "db"}
    assert writes["/nodes/pve1/qemu/100/config"] == {"tags": "db"}
    assert writes["/nodes/pve2/lxc/200/config"] == {"tags": "db;prod"}
    # The target keeps its own color, the old one disappears
    [data] = _options_writes(api)
    assert _colors(data["tag-style"]) == {"db:00ff00"}


def test_edit_merge_declined_writes_nothing(cli, api, ui):
    _routes(api, style={"color-map": "web:ff0000"})
    ui.confirm(False)
    r = cli("tag", "edit", "web", "--name", "db", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert api.writes() == []
    assert "Merge 'web'" in ui.asked[0][1]


def test_edit_color_only_touches_no_guest(cli, api):
    _routes(api, style={"color-map": "web:ff0000;db:00ff00"})
    r = cli("tag", "edit", "web", "--color", "#00ffff", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert _guest_writes(api) == {}
    [data] = _options_writes(api)
    assert _colors(data["tag-style"]) == {"web:00ffff", "db:00ff00"}


def test_edit_rename_and_color_at_once(cli, api):
    _routes(api, style={"color-map": "web:ff0000"})
    r = cli("tag", "edit", "web", "-n", "front", "-c", "123456", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert len(_guest_writes(api)) == 3
    assert _options_writes(api) == [{"tag-style": "color-map=front:123456"}]


@pytest.mark.parametrize("bad", ["a b", "a,b", "a;b"])
def test_edit_refuses_an_invalid_new_name(cli, api, bad):
    _routes(api)
    r = cli("tag", "edit", "web", "--name", bad, "-p", "p1")
    assert r.exit_code == 1
    assert "Invalid tag name" in r.output
    assert api.writes() == []


def test_edit_interactive_picks_the_tag_and_renames_it(cli, api, ui, monkeypatch):
    from src.cli import tag

    # Escape in the color picker keeps the current color
    monkeypatch.setattr(tag, "_pick_color", lambda name: None)
    _routes(api, style={"color-map": "prod:111111"})
    ui.menu("prod").text("production")
    r = cli("tag", "edit", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert _guest_writes(api) == {"/nodes/pve2/lxc/200/config": {"tags": "web;production"}}
    assert _options_writes(api) == [{"tag-style": "color-map=production:111111"}]


def test_edit_interactive_same_name_and_escape_writes_nothing(cli, api, ui, monkeypatch):
    from src.cli import tag

    monkeypatch.setattr(tag, "_pick_color", lambda name: None)
    _routes(api)
    ui.text(None)  # Enter on the prefilled current name
    r = cli("tag", "edit", "web", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert api.writes() == []


def test_edit_menu_escape_writes_nothing(cli, api, ui):
    _routes(api)
    ui.menu(None)
    r = cli("tag", "edit", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert api.writes() == []


def test_edit_on_a_cluster_without_tags(cli, api):
    _routes(api, resources=[RES[3]])
    r = cli("tag", "edit", "web", "--name", "x", "-p", "p1")
    assert r.exit_code == 0
    assert "No tags found" in r.output
    assert api.writes() == []


def test_edit_rename_of_an_unknown_tag_is_refused(cli, api):
    _routes(api)
    r = cli("tag", "edit", "wbe", "--name", "front", "-p", "p1")
    assert api.writes() == []
    assert r.exit_code == 1
    assert "renamed" not in r.output


# ── remove ───────────────────────────────────────────────────────────────


def test_remove_cancelled_writes_nothing(cli, api, ui):
    _routes(api, style={"color-map": "web:ff0000"})
    ui.confirm(False)
    r = cli("tag", "remove", "web", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert api.writes() == []
    # The summary lists every guest before asking
    for line in ("VM 100 (a)", "VM 101 (b)", "CT 200 (c)"):
        assert line in r.output


def test_remove_unknown_tag_writes_nothing(cli, api):
    _routes(api)
    r = cli("tag", "remove", "nope", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "not found anywhere" in r.output
    assert api.writes() == []


def test_remove_picked_in_the_menu_on_every_guest_and_its_color(cli, api, ui):
    _routes(api, style={"color-map": "prod:111111;web:ff0000"})
    ui.multi(["prod"]).confirm(True)
    r = cli("tag", "remove", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert _guest_writes(api) == {"/nodes/pve2/lxc/200/config": {"tags": "web"}}
    assert _options_writes(api) == [{"tag-style": "color-map=web:ff0000"}]


def test_remove_menu_escape_or_empty_selection_writes_nothing(cli, api, ui):
    _routes(api)
    ui.multi(None, [])
    assert cli("tag", "remove", "-p", "p1").exit_code == 0
    assert cli("tag", "remove", "-p", "p1").exit_code == 0
    assert api.writes() == []


def test_remove_two_tags_on_guests_carrying_one_of_them(cli, api):
    _routes(api, style={"color-map": "prod:111111;db:222222;web:ff0000"})
    r = cli("tag", "remove", "prod;db", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert _guest_writes(api) == {
        "/nodes/pve1/qemu/101/config": {"tags": "web"},
        "/nodes/pve2/lxc/200/config": {"tags": "web"},
    }
    # The color map is written once, with both colors gone
    assert _options_writes(api) == [{"tag-style": "color-map=web:ff0000"}]
    assert "2 tags removed" in r.output


def test_remove_two_tags_carried_by_the_same_guest(cli, api):
    _routes(api)
    r = cli("tag", "remove", "web,db", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    # VM 101 has "db;web": after the command it must carry neither
    last = [c.data for c in api.writes() if c.path == "/nodes/pve1/qemu/101/config"][-1]
    assert last == {"delete": "tags"}
    # Three guests carried them (seen on the lab: 2 guests read "from 4 resource(s)")
    assert "from 3 resource(s)" in r.output


def test_remove_refused_by_proxmox_reports_the_error(cli, api):
    _routes(api)
    api.route("PUT", "/nodes/{node}/{kind}/{vmid}/config", api.forbidden("/vms/100", "VM.Config.Options"))
    r = cli("tag", "remove", "web", "-y", "-p", "p1")
    assert r.exit_code == 1
    assert "Permission" in r.output


# ── export / import ──────────────────────────────────────────────────────


def test_export_writes_every_tag_with_its_color(cli, api, tmp_path):
    _routes(api, style={"color-map": "web:ff0000:000000;spare:00ff00"})
    out = tmp_path / "tags.json"
    r = cli("tag", "export", "-o", str(out), "-p", "p1")
    assert r.exit_code == 0, r.output
    assert json.loads(out.read_text()) == {"tags": [
        {"name": "db"},
        {"name": "prod"},
        {"name": "spare", "color": "00ff00"},
        {"name": "web", "color": "ff0000:000000"},
    ]}
    assert api.writes() == []


def test_export_without_tags_creates_no_file(cli, api, tmp_path):
    _routes(api, resources=[])
    out = tmp_path / "tags.json"
    r = cli("tag", "export", "-o", str(out), "-p", "p1")
    assert r.exit_code == 0
    assert not out.exists()


def test_export_then_import_on_another_cluster(cli, api, tmp_path):
    out = tmp_path / "tags.json"
    api.route("GET", "/cluster/resources", RES, host="h1")
    api.route("GET", "/cluster/options", {"tag-style": {"color-map": "web:ff0000:000000;db:00ff00"}}, host="h1")
    api.route("GET", "/cluster/options", {"tag-style": {"shape": "dense"}}, host="h2")
    api.route("PUT", "/cluster/options", None, host="h2")
    assert cli("tag", "export", "-o", str(out), "-p", "p1").exit_code == 0
    r = cli("tag", "import", str(out), "-p", "p2")
    assert r.exit_code == 0, r.output
    [put] = api.writes()
    assert put.host == "h2"
    style = _style(put.data["tag-style"])
    assert style["shape"] == "dense"
    assert set(style["color-map"].split(";")) == {"web:ff0000:000000", "db:00ff00"}
    assert "2 tag(s) imported" in r.output


def _write(tmp_path: Path, tags: list[dict]) -> Path:
    f = tmp_path / "in.json"
    f.write_text(json.dumps({"tags": tags}))
    return f


def test_import_asks_before_overwriting_and_keeps_a_declined_color(cli, api, ui, tmp_path):
    _routes(api, style={"color-map": "web:ff0000"})
    f = _write(tmp_path, [{"name": "web", "color": "0000ff"}, {"name": "db", "color": "00ff00"}])
    ui.confirm(False)
    r = cli("tag", "import", str(f), "-p", "p1")
    assert r.exit_code == 0, r.output
    [data] = _options_writes(api)
    assert _colors(data["tag-style"]) == {"web:ff0000", "db:00ff00"}
    assert "1 tag(s) skipped" in r.output


def test_import_overwrite_accepted(cli, api, ui, tmp_path):
    _routes(api, style={"color-map": "web:ff0000"})
    f = _write(tmp_path, [{"name": "web", "color": "0000ff"}])
    ui.confirm(True)
    r = cli("tag", "import", str(f), "-p", "p1")
    assert r.exit_code == 0, r.output
    assert _options_writes(api) == [{"tag-style": "color-map=web:0000ff"}]


def test_import_force_skips_existing_tags_without_asking(cli, api, ui, tmp_path):
    _routes(api, style={"color-map": "web:ff0000"})
    f = _write(tmp_path, [{"name": "web", "color": "0000ff"}])
    r = cli("tag", "import", str(f), "--force", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert api.writes() == []
    assert ui.asked == []


def test_import_entries_without_color_or_name_write_nothing(cli, api, tmp_path):
    _routes(api)
    f = _write(tmp_path, [{"name": "web"}, {"color": "ff0000"}])
    r = cli("tag", "import", str(f), "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "Nothing to import" in r.output
    assert api.writes() == []


def test_import_missing_file(cli, api, tmp_path):
    r = cli("tag", "import", str(tmp_path / "absent.json"), "-p", "p1")
    assert r.exit_code == 1
    assert "File not found" in r.output
    assert api.calls == []


def test_import_invalid_json(cli, api, tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("{not json")
    r = cli("tag", "import", str(f), "-p", "p1")
    assert r.exit_code == 1
    assert "Invalid JSON" in r.output
    assert api.writes() == []


def test_import_empty_file(cli, api, tmp_path):
    f = _write(tmp_path, [])
    r = cli("tag", "import", str(f), "-p", "p1")
    assert r.exit_code == 0
    assert "No tags found in file" in r.output
    assert api.calls == []


def test_remove_keeps_a_tag_added_after_the_cluster_list_was_built(cli, api):
    # /cluster/resources lags: VM 101 got 'new' seconds ago, its config has it
    _routes(api)
    api.route("GET", "/nodes/{node}/{kind}/{vmid}/config",
              lambda c: {"tags": "db;new;web"} if c.match["vmid"] == "101" else
              {"tags": {"100": "web", "200": "web;prod"}[c.match["vmid"]]})
    r = cli("tag", "remove", "web", "-y", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert _guest_writes(api)["/nodes/pve1/qemu/101/config"] == {"tags": "db;new"}
