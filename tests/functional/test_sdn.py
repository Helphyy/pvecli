"""SDN: opt-in writes, DHCP zones need an IPAM, apply previews, locks and reloads."""

from __future__ import annotations

import json

import pytest

from tests.fake_api import FakeAPI, fixture, upid


def recorded(api: FakeAPI, name: str):
    """A recorded response, or the recorded error of that PVE version."""
    data = fixture(name)
    if isinstance(data, dict) and "__error__" in data:
        return api.error(data["__status__"], data["__error__"])
    return data


def route_pending(api: FakeAPI, zones=(), vnets=(), subnets=None) -> None:
    """Pending view and declared objects: zones, vnets and their subnets."""
    api.route("GET", "/cluster/sdn/zones", list(zones))
    api.route("GET", "/cluster/sdn/vnets", list(vnets))
    api.route("GET", "/cluster/sdn/vnets/{vnet}/subnets", lambda c: (subnets or {}).get(c.match["vnet"], []))


def route_apply(api: FakeAPI, version: str, cluster: str = "lab") -> None:
    """A cluster with one pending zone, ready to be applied."""
    route_pending(api, zones=[{"zone": "z1", "type": "simple", "state": "new"}])
    api.route("GET", "/cluster/config/totem", {"cluster_name": cluster})
    api.route("GET", "/nodes", fixture(f"{version}/nodes"))
    api.route("GET", "/cluster/sdn/dry-run", recorded(api, f"{version}/sdn_dry_run"))
    api.route("GET", "/nodes/{node}/sdn/zones", [])
    api.route("PUT", "/cluster/sdn", upid("pve-lab1", "reloadnetworkall"))


def flat(text: str) -> str:
    """Output with the terminal line wrapping undone."""
    return " ".join(text.split())


def index_of(api: FakeAPI, method: str, path: str) -> int:
    return next(i for i, c in enumerate(api.calls) if c.method == method and c.path == path)


# ── opt-in per profile ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "args",
    [
        ("zone", "add", "z1", "--type", "simple", "--dhcp", "-y"),
        ("zone", "set", "zsimple", "--mtu", "1400", "-y"),
        ("apply", "-y"),
        ("unlock", "-y"),
    ],
    ids=["zone-add", "zone-set", "apply", "unlock"],
)
def test_writes_refused_without_sdn_write(cli, api, args):
    route_apply(api, "pve-9.2")
    api.route("GET", "/cluster/sdn/zones", fixture("pve-9.1/sdn_zones"))
    api.route("POST", "/cluster/sdn/zones", None)
    api.route("PUT", "/cluster/sdn/zones/{zone}", None)
    api.route("POST", "/cluster/sdn/lock", "tok")
    api.route("DELETE", "/cluster/sdn/lock", None)

    r = cli("sdn", *args, "-p", "p2")

    assert r.exit_code == 1
    assert "SDN writes are disabled for profile 'p2'" in r.output
    assert "sdn_write: true" in r.output
    assert api.writes() == []


# ── DHCP needs an IPAM ───────────────────────────────────────────────────


def test_zone_add_dhcp_gets_the_pve_ipam_by_default(cli, api):
    api.route("GET", "/cluster/sdn/zones", fixture("pve-9.1/sdn_zones"))
    api.route("POST", "/cluster/sdn/zones", None)

    r = cli("sdn", "zone", "add", "z1", "--type", "simple", "--dhcp", "-y", "-p", "p1")

    assert r.exit_code == 0, r.output
    [w] = api.writes()
    assert (w.method, w.path) == ("POST", "/cluster/sdn/zones")
    assert w.data == {"zone": "z1", "type": "simple", "ipam": "pve", "dhcp": "dnsmasq"}


def test_zone_add_keeps_an_explicit_ipam(cli, api):
    api.route("GET", "/cluster/sdn/zones", [])
    api.route("POST", "/cluster/sdn/zones", None)

    r = cli("sdn", "zone", "add", "z1", "--type", "simple", "--dhcp", "--ipam", "netbox", "-y", "-p", "p1")

    assert r.exit_code == 0, r.output
    [w] = api.writes()
    assert w.data["ipam"] == "netbox"
    assert w.data["dhcp"] == "dnsmasq"


def test_zone_set_dhcp_refused_on_zone_without_ipam_that_has_subnets(cli, api):
    route_pending(
        api,
        zones=[{"zone": "z1", "type": "simple"}],
        vnets=[{"vnet": "v1", "zone": "z1"}],
        subnets={"v1": [{"cidr": "10.0.0.0/24", "subnet": "z1-10.0.0.0-24", "vnet": "v1"}]},
    )
    api.route("PUT", "/cluster/sdn/zones/{zone}", None)

    r = cli("sdn", "zone", "set", "z1", "--dhcp", "-y", "-p", "p1")

    assert r.exit_code == 1
    assert "no IPAM" in r.output
    assert "10.0.0.0/24 on v1" in r.output
    assert "Remove the subnets" in r.output
    assert api.writes() == []


def test_zone_set_dhcp_adds_the_ipam_on_a_zone_without_subnets(cli, api):
    route_pending(api, zones=[{"zone": "z1", "type": "simple"}], vnets=[{"vnet": "v1", "zone": "z1"}])
    api.route("PUT", "/cluster/sdn/zones/{zone}", None)

    r = cli("sdn", "zone", "set", "z1", "--dhcp", "-y", "-p", "p1")

    assert r.exit_code == 0, r.output
    [w] = api.writes()
    assert (w.method, w.path) == ("PUT", "/cluster/sdn/zones/z1")
    assert w.data == {"dhcp": "dnsmasq", "ipam": "pve"}


def test_zone_set_dhcp_keeps_an_existing_ipam(cli, api):
    route_pending(
        api,
        zones=[{"zone": "z1", "type": "simple", "ipam": "pve"}],
        vnets=[{"vnet": "v1", "zone": "z1"}],
        subnets={"v1": [{"cidr": "10.0.0.0/24", "vnet": "v1"}]},
    )
    api.route("PUT", "/cluster/sdn/zones/{zone}", None)

    r = cli("sdn", "zone", "set", "z1", "--dhcp", "-y", "-p", "p1")

    assert r.exit_code == 0, r.output
    [w] = api.writes()
    assert w.data == {"dhcp": "dnsmasq"}


def test_health_flags_a_dhcp_zone_without_ipam(cli, api):
    zones = fixture("pve-9.1/sdn_zones")
    zones[0].pop("ipam")  # zsimple: dnsmasq, no IPAM
    route_pending(api, zones=zones)
    api.route("GET", "/nodes", fixture("pve-9.1/nodes"))
    api.route("GET", "/nodes/{node}/sdn/zones", [])

    r = cli("sdn", "health", "--json", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["dhcp_without_ipam"] == ["zsimple"]

    r = cli("sdn", "health", "-p", "p1")
    assert r.exit_code == 0, r.output
    assert "no IPAM" in r.stdout
    assert "zsimple: DHCP enabled without IPAM" in r.stdout


def test_health_is_quiet_when_dhcp_zones_have_an_ipam(cli, api):
    route_pending(api, zones=fixture("pve-9.1/sdn_zones"))
    api.route("GET", "/nodes", fixture("pve-9.1/nodes"))
    api.route("GET", "/nodes/{node}/sdn/zones", [])

    r = cli("sdn", "health", "--json", "-p", "p1")

    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["dhcp_without_ipam"] == []


# ── apply ────────────────────────────────────────────────────────────────


def test_apply_on_92_previews_then_locks_and_applies_with_the_token(cli, api):
    route_apply(api, "pve-9.2")
    api.route("POST", "/cluster/sdn/lock", "tok-123")

    r = cli("sdn", "apply", "-p", "p1", input="lab\n")

    assert r.exit_code == 0, r.output
    assert "FRR routing on pve-lab9" in r.stdout
    assert "+frr version 10.6.1" in r.stdout
    assert api.called("GET", "/cluster/sdn/dry-run")[0].params == {"node": "pve-lab9"}
    dry = index_of(api, "GET", "/cluster/sdn/dry-run")
    lock = index_of(api, "POST", "/cluster/sdn/lock")
    put = index_of(api, "PUT", "/cluster/sdn")
    assert dry < lock < put
    assert api.calls[lock].data == {"allow-pending": 1}
    assert api.calls[put].data == {"lock-token": "tok-123", "release-lock": 1}
    assert "SDN configuration applied" in r.stdout


def test_apply_shows_the_preview_before_the_confirmation(cli, api):
    route_apply(api, "pve-9.2")
    api.route("POST", "/cluster/sdn/lock", "tok-123")

    r = cli("sdn", "apply", "-p", "p1", input="wrong\n")

    assert r.exit_code == 1
    assert "+frr version 10.6.1" in r.stdout
    assert r.stdout.index("+frr version 10.6.1") < r.stdout.index("Type the cluster name")
    assert api.writes() == []


def test_apply_on_91_skips_the_preview_without_lock(cli, api):
    route_apply(api, "pve-9.1")
    api.route("POST", "/cluster/sdn/lock", api.not_implemented("POST", "/cluster/sdn/lock"))

    r = cli("sdn", "apply", "-y", "-p", "p1")

    assert r.exit_code == 0, r.output
    assert "Could not preview" not in r.output
    assert "FRR routing" not in r.stdout
    [put] = api.called("PUT", "/cluster/sdn")
    assert put.data is None
    assert "SDN configuration applied" in r.stdout


def test_apply_lock_token_error_names_sdn_unlock_and_gives_the_lock_back(cli, api):
    route_apply(api, "pve-9.2")
    api.route("POST", "/cluster/sdn/lock", "tok-123")
    api.route("DELETE", "/cluster/sdn/lock", None)
    api.route("PUT", "/cluster/sdn", api.error(500, "invalid lock token"))

    r = cli("sdn", "apply", "-y", "-p", "p1")

    assert r.exit_code == 1
    assert "pvecli sdn unlock" in flat(r.output)
    [unlock] = api.called("DELETE", "/cluster/sdn/lock")
    assert unlock.params == {"lock-token": "tok-123"}


def test_apply_lock_held_elsewhere_is_refused_before_any_apply(cli, api):
    route_apply(api, "pve-9.2")
    api.route("POST", "/cluster/sdn/lock", api.error(500, "could not acquire lock for SDN config"))

    r = cli("sdn", "apply", "-y", "-p", "p1")

    assert r.exit_code == 1
    assert "pvecli sdn unlock" in flat(r.output)
    assert api.called("PUT", "/cluster/sdn") == []


def test_apply_with_nothing_pending_writes_nothing(cli, api):
    route_apply(api, "pve-9.2")
    route_pending(api)

    r = cli("sdn", "apply", "-y", "-p", "p1")

    assert r.exit_code == 0, r.output
    assert "Nothing pending" in r.stdout
    assert api.writes() == []
