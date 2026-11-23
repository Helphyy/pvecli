"""Keeping the access to the cluster while the firewall changes.

pvecli talks to one node through its API (port 8006). A firewall change can
close that port to the machine pvecli runs on, and then nothing can be
undone through the API. How the node decides, read from the rules
pve-firewall loads on PVE 9.1 (iptables-save, 2026-11-23):

    1. connections already open            accepted
    2. the rules of the node, in order     a security group in its place
    3. the rules of the datacenter         in order
    4. management hosts to 8006, 22...     accepted: the 'management' IP set,
                                           or the cluster network without it
    5. the input policy                    DROP by default

So a DROP rule of the user wins over the management hosts. This is only
when the datacenter firewall is on and so is the node's (its default).

Two guards:

- before: judge() follows that order for TCP 8006 from this machine, with
  the configuration the change would leave. A change that closes the API
  is refused, unless --force;
- after: check_after() opens a new connection once the change is applied.
  It cannot undo: Proxmox closes the open connections of a source it now
  drops (seen on the lab), so the undo would not get through. It says how
  to get back in instead.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import Any

from ..api.client import ProxmoxClient
from ..api.exceptions import PVECliError
from ..utils import console, print_error, print_success, print_warning
from .firewall import effective, is_on

# pve-firewall compiles and loads the rules every 10 seconds
APPLY_DELAY = 12
API_PORT = 8006


@dataclass
class Access:
    kept: bool | None  # None when pvecli cannot tell
    reason: str


def source_address(host: str, port: int) -> str | None:
    """The address this machine uses to reach the host (no packet is sent).

    Behind a NAT the node sees another address: the guard then says so.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((socket.gethostbyname(host), port))
            return str(s.getsockname()[0])
    except OSError:
        return None


def _resolve(token: str, aliases: dict[str, str]) -> str:
    return aliases.get(token.removeprefix("dc/"), token)


def in_set(ip: str, entries: list[dict[str, Any]], aliases: dict[str, str]) -> bool:
    """Whether an address is in an IP set: an entry matches, no nomatch entry does."""
    addr = ipaddress.ip_address(ip)
    match = nomatch = False
    for e in entries:
        try:
            net = ipaddress.ip_network(_resolve(str(e.get("cidr", "")), aliases), strict=False)
        except ValueError:
            continue
        if addr in net:
            if e.get("nomatch"):
                nomatch = True
            else:
                match = True
    return match and not nomatch


def _address_in(field: str, ip: str | None, aliases: dict[str, str],
                ipsets: dict[str, list[dict[str, Any]]]) -> bool | None:
    """Whether a source or dest field covers the address (None: cannot tell)."""
    if not field:
        return True
    if ip is None:
        return None
    unknown = False
    for token in str(field).split(","):
        token = token.strip()
        if token.startswith("+"):
            name = token[1:].removeprefix("dc/")
            if name not in ipsets:
                unknown = True
            elif in_set(ip, ipsets[name], aliases):
                return True
            continue
        try:
            if ipaddress.ip_address(ip) in ipaddress.ip_network(_resolve(token, aliases), strict=False):
                return True
        except ValueError:
            unknown = True
    return None if unknown else False


def _port_in(dport: str) -> bool:
    if not dport:
        return True
    for part in str(dport).split(","):
        lo, _, hi = part.strip().partition(":")
        try:
            if int(lo) <= API_PORT <= int(hi or lo):
                return True
        except ValueError:
            continue
    return False


def meets_api(rule: dict[str, Any], ip: str, node_ip: str | None, aliases: dict[str, str],
              ipsets: dict[str, list[dict[str, Any]]]) -> bool | None:
    """Whether a rule applies to TCP 8006 from ip to the node.

    None when pvecli cannot tell (an interface, an unknown set...): the
    caller then reads a DROP as closing and an ACCEPT as not opening.
    """
    if rule.get("type") != "in" or not is_on(rule.get("enable", 1)) or rule.get("errors"):
        return False
    if rule.get("macro") or rule.get("sport") or rule.get("icmp-type"):
        # No macro of Proxmox opens 8006; a source port never is one fixed in advance
        return False
    if rule.get("proto") not in (None, "", "tcp", "6"):
        return False
    if not _port_in(str(rule.get("dport") or "")):
        return False
    src = _address_in(str(rule.get("source") or ""), ip, aliases, ipsets)
    dst = _address_in(str(rule.get("dest") or ""), node_ip, aliases, ipsets)
    if src is False or dst is False:
        return False
    if src is None or dst is None or rule.get("iface"):
        return None
    return True


def host_chain(node_rules: list[dict[str, Any]], dc_rules: list[dict[str, Any]],
               groups: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """The rules of the node then of the datacenter, groups in their place."""
    chain = []
    for level, rules in (("node", node_rules), ("datacenter", dc_rules)):
        for r in sorted(rules, key=lambda x: x.get("pos", 0)):
            if r.get("type") == "group":
                if not is_on(r.get("enable", 1)):
                    continue
                for g in sorted(groups.get(str(r.get("action")), []), key=lambda x: x.get("pos", 0)):
                    chain.append({**g, "_where": f"security group {r.get('action')} (rule {r.get('pos')} "
                                                 f"of the {level})"})
            else:
                chain.append({**r, "_where": f"rule {r.get('pos')} of the {level}"})
    return chain


def judge(ip: str | None, dc_opts: dict[str, Any], node_opts: dict[str, Any],
          ipsets: dict[str, list[dict[str, Any]]], aliases: dict[str, str],
          local_networks: list[str], node_rules: list[dict[str, Any]],
          dc_rules: list[dict[str, Any]], groups: dict[str, list[dict[str, Any]]],
          node_ip: str | None = None) -> Access:
    """Whether this machine reaches the API, under this configuration."""
    if not is_on(effective("cluster", dc_opts, "enable")):
        return Access(True, "the datacenter firewall is off")
    if not is_on(effective("node", node_opts, "enable")):
        return Access(True, "the firewall of the node pvecli talks to is off")
    if ip is None:
        return Access(None, "pvecli cannot tell its own address towards the node")
    for r in host_chain(node_rules, dc_rules, groups):
        meets = meets_api(r, ip, node_ip, aliases, ipsets)
        action = r.get("action")
        if meets is None and action in ("DROP", "REJECT"):
            return Access(None, f"{r['_where']} ({action}) may close it: pvecli cannot tell whether it "
                                f"applies to {ip}")
        if meets:
            if action == "ACCEPT":
                return Access(True, f"{r['_where']} lets {ip} in")
            return Access(False, f"{r['_where']} {'drops' if action == 'DROP' else 'rejects'} {ip}, before "
                                 "the management hosts are let in")
    if "management" in ipsets:
        if in_set(ip, ipsets["management"], aliases):
            return Access(True, f"{ip} is in the 'management' IP set")
        where = f"{ip} is not in the 'management' IP set"
    else:
        addr = ipaddress.ip_address(ip)
        nets = [ipaddress.ip_network(n, strict=False) for n in local_networks]
        if any(addr in n for n in nets):
            return Access(True, f"{ip} is in the cluster network {', '.join(map(str, nets))}, which "
                                "Proxmox treats as management while no 'management' IP set exists")
        where = (f"{ip} is outside the cluster network ({', '.join(map(str, nets)) or 'unknown'}) "
                 "and there is no 'management' IP set")
    if effective("cluster", dc_opts, "policy_in") == "ACCEPT":
        return Access(True, f"{where}, but the input policy is ACCEPT")
    return Access(False, where)


async def read_access_inputs(client: ProxmoxClient, node: str) -> dict[str, Any]:
    """What judge() needs, read from the cluster as it is now."""
    dc_opts = await client.get("/cluster/firewall/options") or {}
    node_opts = await client.get(f"/nodes/{node}/firewall/options") or {}
    sets = await client.get("/cluster/firewall/ipset") or []
    ipsets = {s["name"]: await client.get(f"/cluster/firewall/ipset/{s['name']}") or [] for s in sets}
    aliases = {a["name"]: a["cidr"] for a in await client.get("/cluster/firewall/aliases") or []}
    node_rules = await client.get(f"/nodes/{node}/firewall/rules") or []
    dc_rules = await client.get("/cluster/firewall/rules") or []
    groups = {g["group"]: await client.get(f"/cluster/firewall/groups/{g['group']}") or []
              for g in await client.get("/cluster/firewall/groups") or []}
    node_ip, networks = await _local_networks(client, node)
    return {"dc_opts": dc_opts, "node_opts": node_opts, "ipsets": ipsets, "aliases": aliases,
            "node_rules": node_rules, "dc_rules": dc_rules, "groups": groups,
            "local_networks": networks, "node_ip": node_ip}


async def _local_networks(client: ProxmoxClient, node: str) -> tuple[str | None, list[str]]:
    """The cluster address of the node, and its network: Proxmox's local_network."""
    status = await client.get_cluster_status() or []
    ip = next((n.get("ip") for n in status if n.get("type") == "node" and n.get("name") == node), None)
    try:
        addr = ipaddress.ip_address(ip or "")
    except ValueError:
        return None, []
    out = []
    for iface in await client.get(f"/nodes/{node}/network") or []:
        cidr = iface.get("cidr")
        if not cidr:
            continue
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if addr in net:
            out.append(str(net))
    return str(addr), out


async def api_node(client: ProxmoxClient, host: str) -> str | None:
    from ._shared import detect_connected_node

    return await detect_connected_node(client, host)


def check_access(access: Access, force: bool) -> None:
    """Refuse a change that closes the API to this machine, unless --force."""
    if access.kept:
        console.print(f"  [green]Access kept:[/green] {access.reason}")
        return
    if access.kept is None:
        print_warning(f"pvecli cannot check it keeps its access: {access.reason}")
        if force:
            return
        import typer

        console.print("  Pass --force to go on anyway.")
        raise typer.Exit(1)
    print_error(f"This change closes the API to this machine: {access.reason}.")
    console.print("  Add this machine to the management hosts first:  pvecli cluster firewall ipset add "
                  "management <your address>", highlight=False)
    if not force:
        import typer

        console.print("  Or pass --force, if another way in is ready (the console of the node).")
        raise typer.Exit(1)
    print_warning("--force: going on, though pvecli will lose the API")


RECOVERY = (
    "To get back in: open the console of the node (the web interface of another node, IPMI, "
    "a screen) and run `pve-firewall stop`, then fix the rule and `pve-firewall start`."
)


async def check_after(profile_config: Any, what: str) -> bool:
    """Once the change is applied, whether a new connection still reaches the API."""
    console.print(f"  Checking the API is still reachable once the change is applied (~{APPLY_DELAY} s)...")
    await asyncio.sleep(APPLY_DELAY)
    probe = profile_config.model_copy(update={"timeout": 5})
    for _ in range(3):
        try:
            async with ProxmoxClient(probe) as fresh:
                await fresh.get("/version")
            print_success("A new connection to the API still works")
            return True
        except PVECliError:
            await asyncio.sleep(2)
    print_error(f"A new connection to the API fails since {what}.")
    console.print(f"  {RECOVERY}", highlight=False)
    return False
