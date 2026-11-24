"""Pure helpers of the deploy commands: the YAML schema, series, file checks,
the LXC net0 value, rootfs sizes and the reproducible ct deploy command.

Protects what reaches PVE from a file: an unknown key or a password is an
error naming the entry, a series never leaves its subnet, two guests of a
file never share a name, an ID or an address.
"""

from __future__ import annotations

import pytest

from src.api.exceptions import PVECliError
from src.cli.cloudinit import GuestSpec, size_bytes
from src.cli.ct_deploy import ct_command, lxc_net0, prepare_ct, rootfs_gib
from src.cli.deploy import file_duplicates, parse_document, series_address

TPL = "local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst"


def parse(guests, defaults=None, cli_profile=None):
    doc = {"guests": guests}
    if defaults is not None:
        doc["defaults"] = defaults
    return parse_document(doc, cli_profile, "p1")


# ── series ───────────────────────────────────────────────────────────────


def test_series_numbers_names_addresses_and_ids():
    specs, errors = parse([{"type": "ct", "name": "web{n}", "count": 3, "template": TPL,
                            "ip": "10.0.0.21/24", "id": 930}])

    assert errors == []
    assert [(s.name, s.ip, s.vmid) for s in specs] == [
        ("web1", "10.0.0.21/24", 930), ("web2", "10.0.0.22/24", 931), ("web3", "10.0.0.23/24", 932),
    ]


def test_series_start_and_zero_padding():
    specs, errors = parse([{"type": "vm", "name": "db{n:02}", "count": 2, "start": 9, "template": 9000}])

    assert errors == []
    assert [s.name for s in specs] == ["db09", "db10"]
    assert all(s.vmid is None for s in specs)  # auto IDs, allocated later


def test_series_increments_ipv6_too():
    specs, errors = parse([{"type": "ct", "name": "w{n}", "count": 2, "template": TPL, "ip6": "2001:db8::10/64"}])

    assert errors == []
    assert [s.ip6 for s in specs] == ["2001:db8::10/64", "2001:db8::11/64"]


def test_series_refuses_to_leave_its_subnet():
    specs, errors = parse([{"type": "ct", "name": "w{n}", "count": 3, "template": TPL, "ip": "10.0.0.253/24"}])

    # .253, .254, then .255 is the broadcast address of 10.0.0.0/24
    assert len(errors) == 1
    assert "10.0.0.0/24" in errors[0] and "guests[0]" in errors[0]


def test_series_address_keeps_dhcp_and_steps_static_ones():
    assert series_address("dhcp", 4) == "dhcp"
    assert series_address("10.0.0.21/24", 0) == "10.0.0.21/24"
    assert series_address("10.0.0.21/24", 2) == "10.0.0.23/24"
    with pytest.raises(PVECliError, match="10.0.0.0/30"):
        series_address("10.0.0.1/30", 2)  # .3 is the broadcast


def test_a_series_needs_the_number_in_its_name():
    _, errors = parse([{"type": "vm", "name": "web", "count": 2, "template": 9000}])

    assert len(errors) == 1 and "{n}" in errors[0]


# ── schema ───────────────────────────────────────────────────────────────


def test_unknown_key_names_the_key_and_the_entry():
    _, errors = parse([{"type": "vm", "name": "ok", "template": 9000},
                       {"type": "vm", "name": "web01", "template": 9000, "colour": "red"}])

    assert len(errors) == 1
    assert "'colour'" in errors[0] and "guests[1]" in errors[0] and "web01" in errors[0]


def test_a_password_in_the_file_is_refused_with_the_way_out():
    _, errors = parse([{"type": "ct", "name": "a", "template": TPL, "password": "hunter22"}],
                      defaults={"password": "x"})

    assert len(errors) == 2
    assert all("password_env" in e for e in errors)
    assert not any("hunter22" in e for e in errors)


def test_every_error_is_listed_at_once():
    _, errors = parse([
        {"name": "notype", "template": 9000},
        {"type": "lxc", "name": "badtype", "template": 9000},
        {"type": "vm", "template": 9000},
        {"type": "vm", "name": "x", "template": 9000, "cores": "two"},
    ])

    assert len(errors) == 4
    assert "type" in errors[0] and "'lxc'" in errors[1] and "name" in errors[2] and "cores" in errors[3]


def test_a_key_of_the_other_type_is_an_error_on_an_entry():
    _, errors = parse([{"type": "vm", "name": "x", "template": 9000, "swap": "512M"},
                       {"type": "ct", "name": "y", "template": TPL, "user": "debian"}])

    assert len(errors) == 2
    assert "'swap'" in errors[0] and "vm" in errors[0]
    assert "'user'" in errors[1] and "ct" in errors[1]


def test_defaults_apply_by_type_and_entries_override_them():
    specs, errors = parse(
        [{"type": "vm", "name": "v"}, {"type": "ct", "name": "c", "cores": 4}],
        defaults={"cores": 2, "swap": 0, "user": "debian", "gw": "10.0.0.1",
                  "vm": {"template": 9000}, "ct": {"template": TPL, "bridge": "vmbr1"}},
    )

    assert errors == []
    vm, ct = specs
    assert (vm.template, vm.cores, vm.user, vm.gw, vm.swap, vm.bridge) == ("9000", 2, "debian", "10.0.0.1", None, None)
    assert (ct.template, ct.cores, ct.swap, ct.bridge, ct.user) == (TPL, 4, "0", "vmbr1", None)


def test_null_clears_a_default_but_not_a_required_key():
    specs, errors = parse([{"type": "ct", "name": "c", "template": "920", "storage": None, "ssh_key": None},
                           {"type": "ct", "name": None, "template": "920"}],
                          defaults={"storage": "local-lvm", "ssh_key": "~/k.pub"})

    assert len(errors) == 1 and "'name'" in errors[0]
    assert (specs[0].storage, specs[0].ssh_keys) == (None, [])


def test_unknown_default_key_is_an_error():
    _, errors = parse([{"type": "vm", "name": "v", "template": 9000}], defaults={"cpus": 2})

    assert len(errors) == 1 and "'cpus'" in errors[0] and "defaults" in errors[0]


def test_profile_precedence_entry_defaults_cli_then_default_profile():
    guests = [{"type": "vm", "name": "a", "template": 9000, "profile": "p2"}, {"type": "vm", "name": "b", "template": 9000}]

    assert [s.profile for s in parse(guests, defaults={"profile": "p3"}, cli_profile="prod")[0]] == ["p2", "p3"]
    assert [s.profile for s in parse(guests, cli_profile="prod")[0]] == ["p2", "prod"]
    assert [s.profile for s in parse(guests)[0]] == ["p2", "p1"]


def test_lists_and_scalars_are_both_accepted_where_it_makes_sense():
    specs, errors = parse([{"type": "ct", "name": "c", "template": TPL, "tags": "a, b", "ssh_key": "~/k.pub",
                            "features": {"nesting": True, "keyctl": 1}, "disk_size": 8, "memory": 1024}])

    assert errors == []
    [c] = specs
    assert (c.tags, c.ssh_keys, c.features, c.disk_size, c.memory) == (["a", "b"], ["~/k.pub"], "nesting=1,keyctl=1", "8G", "1024")


def test_the_file_must_have_a_guest_list():
    assert parse_document({"defaults": {}}, None, "p1")[1]
    assert parse_document({"guests": []}, None, "p1")[1]
    assert parse_document({"guests": [], "extra": 1}, None, "p1")[1]


# ── file checks ──────────────────────────────────────────────────────────


def spec(name, profile="p1", vmid=None, ip=None, ip6=None, label=""):
    return GuestSpec(kind="ct", name=name, template=TPL, profile=profile, vmid=vmid, ip=ip, ip6=ip6, label=label or name)


def same_cluster(*groups):
    """cluster_of for profiles: p1 and p3 point at one cluster."""
    return lambda profile: next((g[0] for g in groups if profile in g), profile)


def test_duplicate_names_count_per_cluster_not_per_file():
    cluster_of = same_cluster(("p1", "p3"))

    assert file_duplicates([spec("web", "p1"), spec("web", "p2")], cluster_of) == []
    errors = file_duplicates([spec("web", "p1"), spec("web", "p3")], cluster_of)
    assert len(errors) == 1 and "web" in errors[0]


def test_duplicate_ids_on_one_cluster():
    errors = file_duplicates([spec("a", vmid=930), spec("b", vmid=930), spec("c", "p2", vmid=930)], same_cluster())

    assert len(errors) == 1 and "930" in errors[0]


def test_duplicate_addresses_count_across_the_whole_file():
    errors = file_duplicates([spec("a", ip="10.0.0.5/24"), spec("b", "p2", ip="10.0.0.5/16"),
                              spec("c", ip="dhcp"), spec("d", ip="dhcp")], same_cluster())

    # the same address under two prefixes is still one address; dhcp is not one
    assert len(errors) == 1 and "10.0.0.5" in errors[0]


# ── LXC values ───────────────────────────────────────────────────────────


def test_net0_is_built_with_the_default_bridge():
    s = GuestSpec(kind="ct", name="c", template=TPL, ip="10.0.0.5/24", gw="10.0.0.1", vlan=42)

    assert lxc_net0(None, s) == "name=eth0,bridge=vmbr0,ip=10.0.0.5/24,gw=10.0.0.1,tag=42"


def test_net0_of_a_clone_keeps_its_mac_and_type():
    current = "name=eth0,bridge=vmbr0,hwaddr=BC:24:11:28:9D:61,ip=dhcp,type=veth"
    s = GuestSpec(kind="ct", name="c", template="920", ip="10.0.0.6/24", gw="10.0.0.1", bridge="vsimple")

    assert lxc_net0(current, s) == "name=eth0,bridge=vsimple,hwaddr=BC:24:11:28:9D:61,ip=10.0.0.6/24,type=veth,gw=10.0.0.1"


def test_net0_of_a_clone_is_left_alone_without_network_options():
    assert lxc_net0("name=eth0,bridge=vmbr0,ip=dhcp", GuestSpec(kind="ct", name="c", template="920")) is None


def test_rootfs_sizes_are_whole_gib():
    assert rootfs_gib("8G") == 8
    assert rootfs_gib("1T") == 1024
    assert rootfs_gib("2048M") == 2
    for bad in ("512M", "8", "8X"):
        with pytest.raises(PVECliError):
            rootfs_gib(bad)


def test_size_bytes():
    assert size_bytes("2G") == 2 * 1024**3
    assert size_bytes("512M") == 512 * 1024**2
    assert size_bytes("1.5T") == int(1.5 * 1024**4)


def test_prepare_ct_collects_every_problem(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    s = GuestSpec(kind="ct", name="bad_name", template=TPL, ip="2001:db8::1/64", vlan=5000, memory="lots",
                  features="nesting=1,turbo=1", password_env="NOPE")

    errors = prepare_ct(s)

    assert len(errors) == 6, errors
    joined = " | ".join(errors)
    for part in ("bad_name", "IPv4", "4094", "lots", "turbo", "NOPE"):
        assert part in joined


def test_prepare_ct_reads_the_password_and_keeps_it_out_of_the_repr(monkeypatch):
    monkeypatch.setenv("CT_PASS", "S3cr3t-pw")
    s = GuestSpec(kind="ct", name="c", template=TPL, password_env="CT_PASS")

    assert prepare_ct(s) == []
    assert s.params["password"] == "S3cr3t-pw"
    assert "S3cr3t-pw" not in repr(s)


def test_prepare_ct_refuses_a_short_password(monkeypatch):
    monkeypatch.setenv("CT_PASS", "abc")

    errors = prepare_ct(GuestSpec(kind="ct", name="c", template=TPL, password_env="CT_PASS"))

    assert len(errors) == 1 and "5" in errors[0] and "abc" not in errors[0]


def test_reproducible_command_never_carries_the_password(monkeypatch):
    monkeypatch.setenv("CT_PASS", "S3cr3t-pw")
    s = GuestSpec(kind="ct", name="web01", template=TPL, vmid=930, node="pve-lab1", storage="local-lvm",
                  disk_size="8G", swap="0", ip="10.0.0.5/24", gw="10.0.0.1", dns="10.0.0.1 10.0.0.2",
                  ssh_keys=["/home/me/my keys/id.pub"], password_env="CT_PASS", unprivileged=False,
                  features="nesting=1", tags=["a", "b"], start=False)
    prepare_ct(s)

    cmd = ct_command(s, "lab1")

    assert "S3cr3t-pw" not in cmd
    flat = " ".join(cmd.replace("\\\n", " ").split())
    assert flat.startswith(f"pvecli ct deploy {TPL} --hostname web01 --ctid 930 --node pve-lab1")
    for part in ("--storage local-lvm", "--disk-size 8G", "--swap 0", "--ip 10.0.0.5/24", "--gw 10.0.0.1",
                 "--dns '10.0.0.1 10.0.0.2'", "--ssh-key '/home/me/my keys/id.pub'", "--password-env CT_PASS",
                 "--privileged", "--features nesting=1", "--tags a,b", "--no-start", "-p lab1"):
        assert part in flat, part


def test_the_shipped_example_is_a_valid_file():
    import yaml

    from tests.static.test_repo_hygiene import ROOT

    doc = yaml.safe_load((ROOT / "examples" / "deploy.yaml").read_text())
    specs, errors = parse_document(doc, None, "p1")

    assert errors == []
    assert [s.name for s in specs] == ["web1", "web2", "web3", "db01", "build01", "cache1", "cache2", "proxy01",
                                       "mon01"]
    build = specs[4]
    assert (build.iso, build.template, build.user) == ("local:iso/debian-13.8.0-amd64-netinst.iso", "", None)
    proxy = specs[7]
    assert (proxy.ssh_keys, proxy.storage, proxy.template) == ([], None, "920")
    assert file_duplicates(specs, lambda p: p) == []


def test_errors_name_the_file_key_not_the_option():
    from src.cli.deploy import _in_file_terms

    assert _in_file_terms("2 nodes are online: pass --node") == "2 nodes are online: set node:"
    assert _in_file_terms("SSH keys (--ssh-key) are only taken") == "SSH keys (ssh_key:) are only taken"
    # No such key in a deploy file: the option stays
    assert _in_file_terms("pass --drive-storage") == "pass --drive-storage"
    assert _in_file_terms("no profile: pass -p") == "no profile: pass -p"
