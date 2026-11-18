"""The local color palette of `tag color` (~/.config/pvecli/tag_color.yml).

The palette never reaches Proxmox: no command of this file calls the API.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.fixture(autouse=True)
def palette(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The palette path is computed at import time: point it at the test HOME
    so no test can ever touch the real ~/.config/pvecli/tag_color.yml."""
    from src.cli import tag

    path = home / ".config" / "pvecli" / "tag_color.yml"
    monkeypatch.setattr(tag, "_COLOR_FILE", path)
    return path


def _read(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _seed(path: Path, entries: dict) -> None:
    path.write_text(yaml.safe_dump(entries))


def test_init_writes_the_default_palette(cli, api, palette):
    r = cli("tag", "color", "init")
    assert r.exit_code == 0, r.output
    data = _read(palette)
    assert len(data) == 9
    assert data["Red"] == {"hex": "cc3333", "font": "white"}
    assert api.calls == []


def test_init_over_an_existing_palette_declined_keeps_it(cli, api, ui, palette):
    _seed(palette, {"Mine": {"hex": "123456", "font": "black"}})
    ui.confirm(False)
    r = cli("tag", "color", "init")
    assert r.exit_code == 0, r.output
    assert _read(palette) == {"Mine": {"hex": "123456", "font": "black"}}


def test_init_over_an_existing_palette_accepted(cli, api, ui, palette):
    _seed(palette, {"Mine": {"hex": "123456", "font": "black"}})
    ui.confirm(True)
    assert cli("tag", "color", "init").exit_code == 0
    assert "Mine" not in _read(palette)


def test_init_force_overwrites_without_asking(cli, api, ui, palette):
    _seed(palette, {"Mine": {"hex": "123456", "font": "black"}})
    assert cli("tag", "color", "init", "--force").exit_code == 0
    assert "Mine" not in _read(palette)
    assert ui.asked == []


def test_list_shows_hex_and_font(cli, api, palette):
    _seed(palette, {"Cyan": {"hex": "#00bcd4", "font": "black"}, "Red": {"hex": "cc3333", "font": "white"}})
    r = cli("tag", "color", "list")
    assert r.exit_code == 0, r.output
    rows = [line for line in r.stdout.splitlines() if "#" in line]
    assert any("Cyan" in x and "#00bcd4" in x and "black" in x for x in rows)
    assert any("Red" in x and "#cc3333" in x and "white" in x for x in rows)


def test_list_without_palette(cli, api, palette):
    r = cli("tag", "color", "list")
    assert r.exit_code == 0
    assert "tag color init" in r.output
    assert not palette.exists()


def test_add_with_arguments(cli, api, palette):
    _seed(palette, {"Red": {"hex": "cc3333", "font": "white"}})
    r = cli("tag", "color", "add", "Cyan", "#00BCD4", "--font", "black")
    assert r.exit_code == 0, r.output
    assert _read(palette) == {
        "Red": {"hex": "cc3333", "font": "white"},
        "Cyan": {"hex": "00BCD4", "font": "black"},
    }


@pytest.mark.parametrize("hex_color", ["00bcd", "00bcd4ff", "zzzzzz"])
def test_add_refuses_an_invalid_hex(cli, api, palette, hex_color):
    _seed(palette, {"Red": {"hex": "cc3333", "font": "white"}})
    before = palette.read_text()
    r = cli("tag", "color", "add", "Cyan", hex_color, "--font", "white")
    assert r.exit_code == 1
    assert palette.read_text() == before


def test_add_refuses_an_unknown_font(cli, api, palette):
    _seed(palette, {"Red": {"hex": "cc3333", "font": "white"}})
    before = palette.read_text()
    r = cli("tag", "color", "add", "Cyan", "00bcd4", "--font", "blue")
    assert r.exit_code == 1
    assert "white' or 'black" in r.output
    assert palette.read_text() == before


def test_add_without_palette_asks_for_init(cli, api, palette):
    r = cli("tag", "color", "add", "Cyan", "00bcd4", "--font", "white")
    assert r.exit_code == 1
    assert "tag color init" in r.output
    assert not palette.exists()


def test_add_interactive_retries_an_invalid_hex(cli, api, ui, palette):
    _seed(palette, {})
    palette.write_text("")  # an empty file is a palette with no color
    ui.text("Cyan", "nothex", "#00bcd4").menu("black")
    r = cli("tag", "color", "add")
    assert r.exit_code == 0, r.output
    assert "Invalid hex color" in r.output
    assert _read(palette) == {"Cyan": {"hex": "00bcd4", "font": "black"}}


def test_add_interactive_escape_on_font_writes_nothing(cli, api, ui, palette):
    _seed(palette, {"Red": {"hex": "cc3333", "font": "white"}})
    before = palette.read_text()
    ui.menu(None)
    r = cli("tag", "color", "add", "Cyan", "00bcd4")
    assert r.exit_code == 0, r.output
    assert palette.read_text() == before


def test_add_interactive_refuses_an_empty_name(cli, api, ui, palette):
    _seed(palette, {"Red": {"hex": "cc3333", "font": "white"}})
    before = palette.read_text()
    ui.text("  ")
    r = cli("tag", "color", "add")
    assert r.exit_code == 1
    assert palette.read_text() == before


def test_add_then_remove_a_color_named_like_a_yaml_boolean(cli, api, palette):
    _seed(palette, {"Red": {"hex": "cc3333", "font": "white"}})
    assert cli("tag", "color", "add", "No", "000000", "--font", "white").exit_code == 0
    assert "No" in _read(palette)
    assert cli("tag", "color", "remove", "No").exit_code == 0


def test_remove_by_name(cli, api, palette):
    _seed(palette, {"Red": {"hex": "cc3333", "font": "white"}, "Blue": {"hex": "2266cc", "font": "white"}})
    r = cli("tag", "color", "remove", "Red")
    assert r.exit_code == 0, r.output
    assert _read(palette) == {"Blue": {"hex": "2266cc", "font": "white"}}


def test_remove_unknown_name_keeps_the_palette(cli, api, palette):
    _seed(palette, {"Red": {"hex": "cc3333", "font": "white"}})
    before = palette.read_text()
    r = cli("tag", "color", "remove", "Green")
    assert r.exit_code == 1
    assert "not found" in r.output
    assert palette.read_text() == before


def test_remove_several_from_the_menu(cli, api, ui, palette):
    _seed(palette, {
        "Red": {"hex": "cc3333", "font": "white"},
        "Blue": {"hex": "2266cc", "font": "white"},
        "Teal": {"hex": "1a8a8a", "font": "white"},
    })
    ui.multi(["Red", "Teal"])
    r = cli("tag", "color", "remove")
    assert r.exit_code == 0, r.output
    assert _read(palette) == {"Blue": {"hex": "2266cc", "font": "white"}}
    assert "2 colors removed" in r.output


def test_remove_menu_escape_or_nothing_selected_keeps_the_palette(cli, api, ui, palette):
    _seed(palette, {"Red": {"hex": "cc3333", "font": "white"}})
    before = palette.read_text()
    ui.multi(None, [])
    assert cli("tag", "color", "remove").exit_code == 0
    assert cli("tag", "color", "remove").exit_code == 0
    assert palette.read_text() == before


def test_remove_without_palette(cli, api, palette):
    r = cli("tag", "color", "remove", "Red")
    assert r.exit_code == 0
    assert "tag color init" in r.output
    assert not palette.exists()


def test_add_tag_with_an_empty_palette_warns_and_writes_nothing(cli, api, palette):
    # The picker of `tag add` reads this palette: empty, it cannot offer anything
    _seed(palette, {})
    palette.write_text("")
    api.route("GET", "/cluster/options", {})
    r = cli("tag", "add", "web", "-p", "p1")
    assert r.exit_code == 0
    assert "tag color init" in r.output
    assert api.writes() == []
