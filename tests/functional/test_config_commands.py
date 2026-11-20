"""pvecli config: profiles in ~/.config/pvecli/config.yaml.

Every test works on the isolated HOME of the `home` fixture. What is checked
is the file actually written: secrets encrypted, fields nobody edited kept,
nothing written when the user backs out.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.fake_api import fixture


@pytest.fixture(autouse=True)
def private_identity(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep the age identity in the test HOME.

    src.crypto computes its identity path at import, which happens at test
    collection with the real HOME: without this, encrypting a test password
    would use (or create) the user's real ~/.config/pvecli/.age-identity.
    """
    import src.crypto

    path = home / ".config" / "pvecli" / ".age-identity"
    monkeypatch.setattr(src.crypto, "_IDENTITY_FILE", path)
    return path


def cfg_file(home: Path) -> Path:
    return home / ".config" / "pvecli" / "config.yaml"


def read_cfg(home: Path) -> dict[str, Any]:
    return yaml.safe_load(cfg_file(home).read_text())


def write_cfg(home: Path, data: dict[str, Any]) -> None:
    cfg_file(home).write_text(yaml.safe_dump(data))


def settle(home: Path) -> str:
    """Load once so the plain test passwords get encrypted, then return the
    file: from here on, a command that writes nothing leaves it identical."""
    from src.config import ConfigManager

    ConfigManager().get()
    return cfg_file(home).read_text()


def decrypt(value: str) -> str:
    from src.crypto import decrypt as real_decrypt

    assert value.startswith("AGE:"), value
    return real_decrypt(value)


# ── config add ──────────────────────────────────────────────────────────


def test_add_with_password_writes_it_encrypted(cli, ui, home, private_identity):
    ui.confirm(True, False)  # save, not as default
    r = cli("config", "add", "lab9", "--host", "10.0.0.9", "--user", "admin@pve",
            "--password", "S3cr3t-pass", "--port", "8443")
    assert r.exit_code == 0, r.output
    assert not ui.left()

    text = cfg_file(home).read_text()
    assert "S3cr3t-pass" not in text
    # Every password in the file is encrypted, the new one and the old ones
    assert "secret" not in text
    data = yaml.safe_load(text)
    new = data["profiles"]["lab9"]
    assert new["host"] == "10.0.0.9" and new["port"] == 8443
    assert new["auth"]["type"] == "password" and new["auth"]["user"] == "admin@pve"
    assert decrypt(new["auth"]["password"]) == "S3cr3t-pass"
    assert new["verify_ssl"] is False
    assert private_identity.exists()
    # The rest of the config is untouched
    assert data["default_profile"] == "p1"
    assert data["groups"] == {"labs": ["p1", "p2"], "withprod": ["p1", "prod"]}
    assert data["profiles"]["p1"]["sdn_write"] is True
    assert data["profiles"]["prod"]["fleet"] is False


def test_add_with_token_encrypts_the_token_value(cli, ui, home):
    ui.confirm(True, True)  # save, and make it the default
    token = "0b8f2a6e-1111-2222-3333-444455556666"
    r = cli("config", "add", "tok", "--host", "pve.example", "--user", "root@pam",
            "--token-name", "cli", "--token-value", token, "--port", "8006")
    assert r.exit_code == 0, r.output
    text = cfg_file(home).read_text()
    assert token not in text
    data = yaml.safe_load(text)
    auth = data["profiles"]["tok"]["auth"]
    assert auth["type"] == "token" and auth["token_name"] == "cli"
    assert decrypt(auth["token_value"]) == token
    assert "password" not in auth
    assert data["default_profile"] == "tok"


def test_add_cancelled_writes_nothing(cli, ui, home):
    before = settle(home)
    ui.confirm(False)
    r = cli("config", "add", "lab9", "--host", "h9", "--user", "root@pam",
            "--password", "pw", "--port", "8006")
    assert r.exit_code == 0
    assert "Cancelled" in r.output
    assert cfg_file(home).read_text() == before


def test_add_existing_name_is_refused(cli, ui, home):
    before = settle(home)
    r = cli("config", "add", "p2", "--host", "h9", "--user", "root@pam",
            "--password", "pw", "--port", "8006")
    assert r.exit_code == 1
    assert "already exists" in r.stderr
    assert cfg_file(home).read_text() == before
    assert ui.asked == []


def test_add_interactive_wizard(cli, ui, home):
    ui.text("lab9", "10.1.2.3", "8006", "root@pam", "Pa55word", "ops", "Administrator")
    ui.menu("Password")
    ui.confirm(True, True, False)  # verify SSL, save, not default
    r = cli("config", "add")
    assert r.exit_code == 0, r.output
    assert not ui.left()
    p = read_cfg(home)["profiles"]["lab9"]
    assert p["host"] == "10.1.2.3" and p["port"] == 8006 and p["verify_ssl"] is True
    assert decrypt(p["auth"]["password"]) == "Pa55word"
    assert p["ssh_user"] == "ops"
    # The default RDP user is not stored
    assert "rdp_user" not in p


def test_add_interactive_cancel_at_auth_method(cli, ui, home):
    before = settle(home)
    ui.text("lab9", "10.1.2.3", "8006", "root@pam")
    ui.menu(None)
    r = cli("config", "add")
    assert r.exit_code == 0
    assert cfg_file(home).read_text() == before


def test_add_first_profile_becomes_default(cli, ui, home):
    cfg_file(home).unlink()
    ui.confirm(True)  # save; no "set as default" question for the first one
    r = cli("config", "add", "only", "--host", "h", "--user", "root@pam",
            "--password", "pw", "--port", "8006")
    assert r.exit_code == 0, r.output
    data = read_cfg(home)
    assert data["default_profile"] == "only"
    assert list(data["profiles"]) == ["only"]
    assert decrypt(data["profiles"]["only"]["auth"]["password"]) == "pw"


# ── config edit ─────────────────────────────────────────────────────────


def _rich_p1(home: Path) -> dict[str, Any]:
    """p1 carries every field the edit menu does not show, and keys this
    version does not know."""
    data = read_cfg(home)
    data["profiles"]["p1"].update(
        timeout=90, sdn_write=True, fleet=False, ssh_key="~/.ssh/lab",
        future_key="keep-me", nested_future={"a": 1},
    )
    data["top_level_future"] = {"x": 1}
    write_cfg(home, data)
    return data


def test_edit_host_keeps_every_other_field(cli, ui, home):
    before = _rich_p1(home)
    ui.menu("Host", "Apply")
    ui.text("10.9.9.9")
    ui.confirm(True)
    r = cli("config", "edit", "p1")
    assert r.exit_code == 0, r.output
    assert not ui.left()

    after = read_cfg(home)
    p1 = after["profiles"]["p1"]
    assert p1["host"] == "10.9.9.9"
    # Every field present before is still there with its value (defaults
    # written out explicitly are fine, a lost field is not)
    expected = dict(before["profiles"]["p1"], host="10.9.9.9")
    assert {k: p1.get(k) for k in expected if k != "auth"} == {k: v for k, v in expected.items() if k != "auth"}
    assert p1["auth"]["user"] == "root@pam" and p1["auth"]["type"] == "password"
    assert decrypt(p1["auth"]["password"]) == "secret"
    # Other profiles and top-level keys untouched
    for name in ("p2", "p3", "prod"):
        theirs = before["profiles"][name]
        assert {k: after["profiles"][name].get(k) for k in theirs if k != "auth"} == \
            {k: v for k, v in theirs.items() if k != "auth"}
    assert after["groups"] == before["groups"]
    assert after["default_profile"] == "p1"
    assert after["top_level_future"] == {"x": 1}


def test_edit_port_and_password(cli, ui, home):
    _rich_p1(home)
    ui.menu("Port", "Password", "Apply")
    ui.text("8443", "n3w-pass")
    ui.confirm(True)
    r = cli("config", "edit", "p1")
    assert r.exit_code == 0, r.output
    p1 = read_cfg(home)["profiles"]["p1"]
    assert p1["port"] == 8443
    assert decrypt(p1["auth"]["password"]) == "n3w-pass"
    assert p1["timeout"] == 90 and p1["future_key"] == "keep-me"
    assert "n3w-pass" not in cfg_file(home).read_text()


def test_edit_switch_to_token(cli, ui, home):
    _rich_p1(home)
    ui.menu("Auth type", "token", "Token name", "Token value", "Apply")
    ui.text("cli", "aaaabbbb-cccc-dddd-eeee-ffff00001111")
    ui.confirm(True)
    r = cli("config", "edit", "p1")
    assert r.exit_code == 0, r.output
    auth = read_cfg(home)["profiles"]["p1"]["auth"]
    assert auth["type"] == "token" and auth["token_name"] == "cli"
    assert decrypt(auth["token_value"]) == "aaaabbbb-cccc-dddd-eeee-ffff00001111"
    # The old password is not left behind in a token profile
    assert "password" not in auth


def test_edit_switch_to_token_without_token_is_refused(cli, ui, home):
    before = settle(home)
    ui.menu("Auth type", "token", "Apply")
    ui.confirm(True)
    r = cli("config", "edit", "p1")
    assert r.exit_code != 0
    assert cfg_file(home).read_text() == before


def test_edit_cancel_menu_writes_nothing(cli, ui, home):
    _rich_p1(home)
    before = settle(home)
    ui.menu("Host", "Cancel")
    ui.text("10.9.9.9")
    r = cli("config", "edit", "p1")
    assert r.exit_code == 0
    assert cfg_file(home).read_text() == before


def test_edit_refused_at_confirmation_writes_nothing(cli, ui, home):
    before = settle(home)
    ui.menu("Host", "Apply")
    ui.text("10.9.9.9")
    ui.confirm(False)
    r = cli("config", "edit", "p1")
    assert r.exit_code == 0
    assert cfg_file(home).read_text() == before


def test_edit_value_set_back_is_not_a_change(cli, ui, home):
    before = settle(home)
    # Host changed then restored: the menu has nothing to apply, Escape leaves
    ui.menu("Host", "Host", None)
    ui.text("10.9.9.9", "h1")
    r = cli("config", "edit", "p1")
    assert r.exit_code == 0
    assert cfg_file(home).read_text() == before
    assert "(no changes)" in ui.asked[-1][1]


def test_edit_picks_profile_from_menu(cli, ui, home):
    ui.menu("p2", "SSH user", "Apply")
    ui.text("debian")
    ui.confirm(True)
    r = cli("config", "edit")
    assert r.exit_code == 0, r.output
    assert read_cfg(home)["profiles"]["p2"]["ssh_user"] == "debian"


def test_edit_rename_moves_the_profile(cli, ui, home):
    _rich_p1(home)
    ui.menu("Profile name", "Apply")
    ui.text("lab1")
    ui.confirm(True)
    r = cli("config", "edit", "p1")
    assert r.exit_code == 0, r.output
    data = read_cfg(home)
    assert "p1" not in data["profiles"]
    lab1 = data["profiles"]["lab1"]
    assert lab1["host"] == "h1" and lab1["timeout"] == 90 and lab1["future_key"] == "keep-me"
    assert decrypt(lab1["auth"]["password"]) == "secret"


def test_edit_rename_default_profile_stays_default(cli, ui, home):
    ui.menu("Profile name", "Apply")
    ui.text("lab1")
    ui.confirm(True)
    r = cli("config", "edit", "p1")
    assert r.exit_code == 0, r.output
    assert read_cfg(home)["default_profile"] == "lab1"


def test_edit_rename_updates_groups(cli, ui, home):
    ui.menu("Profile name", "Apply")
    ui.text("lab2")
    ui.confirm(True)
    r = cli("config", "edit", "p2")
    assert r.exit_code == 0, r.output
    assert read_cfg(home)["groups"]["labs"] == ["p1", "lab2"]


def test_edit_unknown_profile(cli, ui, home):
    r = cli("config", "edit", "nope")
    assert r.exit_code == 1
    assert "not found" in r.stderr


# ── config remove ───────────────────────────────────────────────────────


def test_remove_one_profile(cli, ui, home):
    ui.confirm(True)
    r = cli("config", "remove", "p3")
    assert r.exit_code == 0, r.output
    data = read_cfg(home)
    assert set(data["profiles"]) == {"p1", "p2", "prod"}
    assert data["default_profile"] == "p1"
    assert data["profiles"]["p1"]["sdn_write"] is True


def test_remove_cancelled_writes_nothing(cli, ui, home):
    before = settle(home)
    ui.confirm(False)
    r = cli("config", "remove", "p3")
    assert r.exit_code == 0
    assert cfg_file(home).read_text() == before


def test_remove_from_menu(cli, ui, home):
    ui.multi(["p2", "p3"])
    ui.confirm(True)
    r = cli("config", "remove")
    assert r.exit_code == 0, r.output
    assert set(read_cfg(home)["profiles"]) == {"p1", "prod"}


def test_remove_menu_escape_or_empty_writes_nothing(cli, ui, home):
    before = settle(home)
    ui.multi(None, [])
    assert cli("config", "remove").exit_code == 0
    assert cli("config", "remove").exit_code == 0
    assert cfg_file(home).read_text() == before


def test_remove_all(cli, ui, home):
    ui.confirm(True)
    r = cli("config", "remove", "--all")
    assert r.exit_code == 0, r.output
    data = read_cfg(home)
    assert data.get("profiles", {}) == {}
    assert "default_profile" not in data
    assert "4 profile" in r.output


def test_remove_all_cancelled(cli, ui, home):
    before = settle(home)
    ui.confirm(False)
    assert cli("config", "remove", "--all").exit_code == 0
    assert cfg_file(home).read_text() == before


def test_remove_unknown_profile(cli, ui, home):
    before = settle(home)
    ui.confirm(True)
    r = cli("config", "remove", "nope")
    assert r.exit_code == 1
    assert "not found" in r.stderr
    assert cfg_file(home).read_text() == before


# ── config list / info / default ────────────────────────────────────────


def test_list_shows_every_profile_and_the_default(cli, home):
    r = cli("config", "list")
    assert r.exit_code == 0, r.output
    rows = {line.split()[1]: line for line in r.stdout.splitlines()
            if line.split()[1:2] and line.split()[1] in {"p1", "p2", "p3", "prod"}}
    assert set(rows) == {"p1", "p2", "p3", "prod"}
    assert "h1:8006" in rows["p1"] and "✓" in rows["p1"]
    assert "✓" not in rows["p2"]
    assert "hp:8006" in rows["prod"]
    # Never a secret on screen
    assert "secret" not in r.output


def test_list_empty(cli, home):
    write_cfg(home, {"profiles": {}})
    r = cli("config", "list")
    assert r.exit_code == 0
    assert "No profiles configured" in r.output


def test_info_one_profile(cli, home):
    r = cli("config", "info", "p1")
    assert r.exit_code == 0, r.output
    assert "Profile: p1" in r.stdout
    assert "h1:8006" in r.stdout
    assert "Default profile" in r.stdout
    assert "Timeout:     30s" in r.stdout
    assert "secret" not in r.output
    sdn_line = next(line for line in r.stdout.splitlines() if "SDN write" in line)
    assert "Yes" in sdn_line


def test_info_all_and_from_menu(cli, ui, home):
    r = cli("config", "info", "--all")
    assert r.exit_code == 0
    for name in ("p1", "p2", "p3", "prod"):
        assert f"Profile: {name}" in r.stdout
    ui.menu("prod")
    r = cli("config", "info")
    assert r.exit_code == 0
    assert "Profile: prod" in r.stdout and "Default profile" not in r.stdout


def test_info_unknown(cli, home):
    r = cli("config", "info", "nope")
    assert r.exit_code == 1 and "not found" in r.stderr


def test_default_sets_it(cli, home):
    r = cli("config", "default", "p2")
    assert r.exit_code == 0, r.output
    data = read_cfg(home)
    assert data["default_profile"] == "p2"
    assert data["profiles"]["p1"]["sdn_write"] is True


def test_default_from_menu_and_cancel(cli, ui, home):
    ui.menu("p3")
    assert cli("config", "default").exit_code == 0
    assert read_cfg(home)["default_profile"] == "p3"
    before = cfg_file(home).read_text()
    ui.menu(None)
    assert cli("config", "default").exit_code == 0
    assert cfg_file(home).read_text() == before


def test_default_unknown_keeps_the_old_one(cli, home):
    before = settle(home)
    r = cli("config", "default", "nope")
    assert r.exit_code == 1 and "not found" in r.stderr
    assert cfg_file(home).read_text() == before


def test_no_config_file(cli, ui, home):
    cfg_file(home).unlink()
    r = cli("config", "list")
    assert r.exit_code == 1
    assert "config add" in r.stderr
    r = cli("config", "default")
    assert r.exit_code == 0 and "No configuration found" in r.output


# ── config test ─────────────────────────────────────────────────────────


def test_test_connection_ok(cli, api, home):
    api.route("GET", "/version", fixture("pve-9.1/version"))
    r = cli("config", "test", "-p", "p2")
    assert r.exit_code == 0, r.output
    assert "h2:8006" in r.output
    assert "Connection successful to 'p2'" in r.output
    assert "9.1.1" in r.output
    assert api.called("GET", "/version")[0].host == "h2"
    assert api.writes() == []


def test_test_default_profile(cli, api, home):
    api.route("GET", "/version", fixture("pve-9.1/version"))
    r = cli("config", "test")
    assert r.exit_code == 0
    assert "'p1'" in r.output
    assert api.called("GET", "/version")[0].host == "h1"


def test_test_connection_refused(cli, api, home):
    api.route("GET", "/version", api.forbidden("/", "Sys.Audit"))
    r = cli("config", "test", "-p", "p1")
    assert r.exit_code == 1
    assert "Connection failed" in r.stderr


def test_ctrl_c_during_config_add_exits_130(cli, monkeypatch):
    import src.cli.config as config_cli

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(config_cli, "prompt", interrupted)
    monkeypatch.setattr(config_cli, "select_menu", interrupted, raising=False)
    r = cli("config", "add")
    assert r.exit_code == 130, r.output
    assert "Cancelled" in r.output
