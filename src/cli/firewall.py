"""The Proxmox VE firewall, under each resource that has one.

    pvecli vm firewall ...        a VM
    pvecli ct firewall ...        a container
    pvecli node firewall ...      a node (the host itself)
    pvecli sdn vnet firewall ...  a vnet (forwarded traffic)
    pvecli cluster firewall ...   the datacenter: policies, security groups,
                                  IP sets and aliases, and the view and
                                  export of the whole cluster

One implementation serves them all: a Scope says where the firewall of a
resource lives in the API and what it has (a log, IP sets...), and
build_app() makes the same commands for each resource.

A rule of a guest is enforced only when three switches are on: the
datacenter, the guest, and the network card (firewall=1 on netN). `status`
says which one is off, since Proxmox says nothing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import typer
import yaml
from rich.table import Table

from ..api.client import ProxmoxClient, is_not_implemented
from ..api.exceptions import PVECliError
from ..config import ConfigManager
from ..utils import (
    JSON_OPTION,
    console,
    emit_json,
    print_cancelled,
    print_error,
    print_info,
    print_success,
    print_warning,
)
from ..utils.fleet import fleet
from ..utils.helpers import async_to_sync, ordered_group
from ..utils.menu import select_menu

# What Proxmox applies when an option is not set (pve-firewall defaults)
DEFAULTS: dict[str, dict[str, Any]] = {
    "cluster": {"enable": 0, "policy_in": "DROP", "policy_out": "ACCEPT", "policy_forward": "ACCEPT"},
    "node": {"enable": 1, "nftables": 0},
    "vm": {"enable": 0, "policy_in": "DROP", "policy_out": "ACCEPT"},
    "ct": {"enable": 0, "policy_in": "DROP", "policy_out": "ACCEPT"},
    "vnet": {"enable": 0, "policy_forward": "ACCEPT"},
}

DATACENTER_OFF = "the datacenter firewall is off: nothing on this level is enforced"

# Fields Proxmox adds to what it lists: they describe the listing (pos,
# digest), derive from the rest (ipversion) or report a broken reference
# (errors). None of them is configuration.
_RULE_META = ("pos", "digest", "ipversion", "errors")


@dataclass(frozen=True)
class Scope:
    """Where the firewall of one resource lives in the API."""

    kind: str  # cluster, group, node, vm, ct, vnet
    base: str  # .../firewall (for a group: /cluster/firewall/groups/<name>)
    label: str  # for messages: "datacenter", "node pve1", "VM 101"...
    node: str | None = None
    vmid: int | None = None
    name: str | None = None  # node, vnet or group name

    @property
    def rules_path(self) -> str:
        # A security group is its list of rules
        return self.base if self.kind == "group" else f"{self.base}/rules"

    @property
    def options_path(self) -> str:
        return f"{self.base}/options"

    @property
    def has_log(self) -> bool:
        return self.kind in ("node", "vm", "ct")

    @property
    def has_objects(self) -> bool:
        """IP sets and aliases of its own."""
        return self.kind in ("cluster", "vm", "ct")

    @property
    def is_guest(self) -> bool:
        return self.kind in ("vm", "ct")

    @property
    def directions(self) -> tuple[str, ...]:
        if self.kind == "vnet":
            return ("forward",)
        if self.is_guest:
            return ("in", "out")
        return ("in", "out", "forward")


def cluster_scope() -> Scope:
    return Scope("cluster", "/cluster/firewall", "datacenter")


def group_scope(group: str) -> Scope:
    return Scope("group", f"/cluster/firewall/groups/{group}", f"security group {group}", name=group)


def node_scope(node: str) -> Scope:
    return Scope("node", f"/nodes/{node}/firewall", f"node {node}", node=node, name=node)


def guest_scope(kind: str, node: str, vmid: int) -> Scope:
    api = "qemu" if kind == "vm" else "lxc"
    label = f"{'VM' if kind == 'vm' else 'CT'} {vmid}"
    return Scope(kind, f"/nodes/{node}/{api}/{vmid}/firewall", label, node=node, vmid=vmid)


def vnet_scope(vnet: str) -> Scope:
    return Scope("vnet", f"/cluster/sdn/vnets/{vnet}/firewall", f"vnet {vnet}", name=vnet)


# ── reading ──────────────────────────────────────────────────────────────


async def read_options(client: ProxmoxClient, scope: Scope) -> dict[str, Any]:
    """The options set on this level, without the digest."""
    opts = await client.get(scope.options_path) or {}
    return {k: v for k, v in opts.items() if k != "digest"}


async def read_rules(client: ProxmoxClient, scope: Scope) -> list[dict[str, Any]]:
    rules = await client.get(scope.rules_path) or []
    return sorted(rules, key=lambda r: r.get("pos", 0))


def effective(scope_kind: str, opts: dict[str, Any], key: str) -> Any:
    """The value Proxmox applies: the one set, else its default."""
    if key in opts:
        return opts[key]
    return DEFAULTS.get(scope_kind, {}).get(key)


def is_on(value: Any) -> bool:
    return str(value) in ("1", "True", "true")


def guest_nics(config: dict[str, Any]) -> dict[str, bool]:
    """Each network card of a guest config, and whether it carries firewall=1."""
    nics = {}
    for key, value in config.items():
        if key.startswith("net") and key[3:].isdigit():
            parts = dict(p.split("=", 1) for p in str(value).split(",") if "=" in p)
            nics[key] = parts.get("firewall") == "1"
    return dict(sorted(nics.items(), key=lambda kv: int(kv[0][3:])))


def pitfalls(
    kind: str,
    datacenter_on: bool,
    level_on: bool,
    rules: list[dict[str, Any]],
    nics: dict[str, bool] | None = None,
    nftables_nodes: tuple[int, int] | None = None,
) -> list[str]:
    """Why the rules of a level are not what they seem to be.

    Proxmox accepts every one of these states without a word: a rule that
    no traffic ever meets looks exactly like one that works.
    """
    found: list[str] = []
    for r in rules:
        if r.get("errors"):
            why = "; ".join(f"{k}: {v}" for k, v in sorted(r["errors"].items()))
            found.append(f"rule {r.get('pos')} is broken ({why}): Proxmox ignores it")
    if kind == "cluster":
        # The datacenter switch is said above the levels it governs
        return found
    active = [r for r in rules if is_on(r.get("enable", 1))]
    if not datacenter_on and (level_on or active):
        found.append(DATACENTER_OFF)
    if kind in ("vm", "ct"):
        flagged = [n for n, on in (nics or {}).items() if on]
        if level_on and nics is not None and not flagged:
            found.append("no network card has firewall=1: the rules apply to no interface")
        if active and not level_on:
            found.append(f"{len(active)} rule(s) but the firewall of this guest is off")
        unflagged = [n for n, on in (nics or {}).items() if not on]
        if level_on and flagged and unflagged:
            found.append(f"not filtered: {', '.join(unflagged)} (no firewall=1)")
    elif kind == "node":
        if not level_on and datacenter_on:
            found.append("the firewall of this node is off: its host rules are not enforced")
    elif kind == "vnet":
        if active and not level_on:
            found.append(f"{len(active)} rule(s) but the firewall of this vnet is off")
        if nftables_nodes and level_on and nftables_nodes[0] < nftables_nodes[1]:
            found.append(
                f"vnet rules need the nftables firewall: on {nftables_nodes[0]} of "
                f"{nftables_nodes[1]} node(s) only"
            )
    return found


@dataclass
class Status:
    scope: Scope
    datacenter_on: bool
    level_on: bool
    options: dict[str, Any]
    rules: list[dict[str, Any]]
    nics: dict[str, bool] | None = None
    nftables: bool | None = None
    nftables_nodes: tuple[int, int] | None = None
    issues: list[str] = field(default_factory=list)

    @property
    def enforced(self) -> bool:
        if not (self.datacenter_on and self.level_on):
            return False
        if self.scope.is_guest:
            return any((self.nics or {}).values())
        return True

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "level": self.scope.kind,
            "target": self.scope.vmid if self.scope.is_guest else self.scope.name,
            "datacenter_enabled": self.datacenter_on,
            "enabled": self.level_on,
            "enforced": self.enforced,
            "options": self.options,
            "rules": len(self.rules),
            "rules_enabled": sum(1 for r in self.rules if is_on(r.get("enable", 1))),
            "issues": self.issues,
        }
        if self.nics is not None:
            out["nics"] = self.nics
        if self.nftables is not None:
            out["nftables"] = self.nftables
        return out


async def _nftables_count(client: ProxmoxClient) -> tuple[int, int]:
    nodes = [n["node"] for n in await client.get_nodes() or [] if n.get("status") == "online"]
    opts = await asyncio.gather(*(read_options(client, node_scope(n)) for n in nodes))
    return sum(1 for o in opts if is_on(effective("node", o, "nftables"))), len(nodes)


async def read_status(client: ProxmoxClient, scope: Scope) -> Status:
    """The switches, options and rules of one level, and its pitfalls."""
    dc_opts, opts, rules = await asyncio.gather(
        read_options(client, cluster_scope()),
        read_options(client, scope) if scope.kind != "cluster" else asyncio.sleep(0, {}),
        read_rules(client, scope),
    )
    if scope.kind == "cluster":
        opts = dc_opts
    dc_on = is_on(effective("cluster", dc_opts, "enable"))
    level_on = is_on(effective(scope.kind, opts, "enable"))
    st = Status(scope, dc_on, level_on, opts, rules)
    if scope.is_guest:
        assert scope.node is not None and scope.vmid is not None
        config = await (
            client.get_vm_config(scope.node, scope.vmid) if scope.kind == "vm"
            else client.get_container_config(scope.node, scope.vmid)
        )
        st.nics = guest_nics(config or {})
    elif scope.kind == "node":
        st.nftables = is_on(effective("node", opts, "nftables"))
    elif scope.kind == "vnet":
        st.nftables_nodes = await _nftables_count(client)
    st.issues = pitfalls(scope.kind, dc_on, level_on, rules, st.nics, st.nftables_nodes)
    return st


# ── showing ──────────────────────────────────────────────────────────────


def _onoff(on: bool, default: bool = False) -> str:
    text = "[green]on[/green]" if on else "[dim]off[/dim]"
    return text + (" [dim](default)[/dim]" if default else "")


def _value(scope_kind: str, opts: dict[str, Any], key: str) -> str:
    value = effective(scope_kind, opts, key)
    if value is None:
        return "-"
    if isinstance(value, bool) or (isinstance(value, int) and key not in ("log_ratelimit",)):
        text = "yes" if is_on(value) else "no"
    else:
        text = str(value)
    return text + ("" if key in opts else " [dim](default)[/dim]")


def describe_rule(rule: dict[str, Any]) -> str:
    """One rule on one line, the way it reads in the web interface."""
    if rule.get("type") == "group":
        text = f"group {rule.get('action')}"
    else:
        text = f"{str(rule.get('type', '')).upper()} {rule.get('action', '')}"
        if rule.get("macro"):
            text += f" {rule['macro']}"
        if rule.get("proto"):
            text += f" {rule['proto']}"
    if rule.get("source"):
        text += f" from {rule['source']}"
    if rule.get("sport"):
        text += f" port {rule['sport']}"
    if rule.get("dest"):
        text += f" to {rule['dest']}"
    if rule.get("dport"):
        text += f" dport {rule['dport']}"
    if rule.get("icmp-type"):
        text += f" icmp {rule['icmp-type']}"
    if rule.get("iface"):
        text += f" on {rule['iface']}"
    if rule.get("log") and rule["log"] != "nolog":
        text += f" log {rule['log']}"
    if not is_on(rule.get("enable", 1)):
        text += " (disabled)"
    if rule.get("comment"):
        text += f"  # {rule['comment']}"
    return text


def service(rule: dict[str, Any]) -> str:
    """What a rule matches besides addresses: a macro, or a protocol and ports."""
    if rule.get("macro"):
        return str(rule["macro"])
    parts = [str(rule["proto"])] if rule.get("proto") else []
    if rule.get("dport"):
        parts.append(str(rule["dport"]))
    if rule.get("sport"):
        parts.append(f"from port {rule['sport']}")
    if rule.get("icmp-type"):
        parts.append(str(rule["icmp-type"]))
    return " ".join(parts)


def rules_table(rules: list[dict[str, Any]], title: str) -> Table:
    """The rules in evaluation order; only the columns some rule uses.

    A disabled rule is dimmed and says so in its action.
    """
    optional = [("Service", service), ("Source", lambda r: r.get("source")), ("Dest", lambda r: r.get("dest")),
                ("Iface", lambda r: r.get("iface")),
                ("Log", lambda r: r.get("log") if r.get("log") != "nolog" else None),
                ("Comment", lambda r: r.get("comment"))]
    used = [(h, f) for h, f in optional if any(f(r) for r in rules)]
    broken = any(r.get("errors") for r in rules)
    table = Table(title=title, show_header=True, header_style="bold cyan")
    table.add_column("Pos", justify="right")
    table.add_column("Dir")
    table.add_column("Action", no_wrap=True)
    for header, _ in used:
        # Wrapped rather than cut: an address cut short reads as another one
        table.add_column(header, overflow="fold")
    if broken:
        table.add_column("Broken", style="red", overflow="fold")
    for r in rules:
        on = is_on(r.get("enable", 1))
        action = str(r.get("action", ""))
        style = {"ACCEPT": "green", "DROP": "red", "REJECT": "yellow"}.get(action, "magenta")
        cells = [
            str(r.get("pos", "")),
            str(r.get("type", "")),
            f"[{style}]{action}[/{style}]" if on else f"{action} (off)",
        ]
        cells += [str(f(r) or "") for _, f in used]
        if broken:
            cells.append("; ".join(f"{k}: {v}" for k, v in sorted((r.get("errors") or {}).items())))
        table.add_row(*cells, style=None if on else "dim")
    return table


def print_status(st: Status) -> None:
    s = st.scope
    rows: list[tuple[str, str]] = []
    if s.kind != "cluster":
        rows.append(("Datacenter", _onoff(st.datacenter_on)))
    rows.append(("Enabled", _onoff(st.level_on, "enable" not in st.options)))
    if st.nics is not None:
        rows.append(("Cards", ", ".join(
            f"{n} [green]firewall=1[/green]" if on else f"{n} [dim]not filtered[/dim]"
            for n, on in st.nics.items()) or "[dim]none[/dim]"))
    if st.nftables is not None:
        rows.append(("Backend", "nftables" if st.nftables else "iptables"))
    for key in ("policy_in", "policy_out", "policy_forward"):
        if effective(s.kind, st.options, key) is not None:
            rows.append((key.replace("_", " ").capitalize(), _value(s.kind, st.options, key)))
    enabled = sum(1 for r in st.rules if is_on(r.get("enable", 1)))
    rows.append(("Rules", f"{len(st.rules)} ({enabled} enabled)"))
    if s.kind != "cluster":
        rows.append(("Enforced", "[green]yes[/green]" if st.enforced else "[yellow]no[/yellow]"))
    console.print(f"\n[bold]Firewall of {s.label}[/bold]")
    for label, value in rows:
        console.print(f"  {label + ':':<16}{value}")
    for issue in st.issues:
        print_warning(issue)


def options_table(scope: Scope, opts: dict[str, Any]) -> Table:
    table = Table(title=f"Firewall options of {scope.label}", show_header=True, header_style="bold cyan")
    table.add_column("Option", style="cyan")
    table.add_column("Value")
    keys = sorted(set(opts) | set(DEFAULTS.get(scope.kind, {})))
    for key in keys:
        table.add_row(key, _value(scope.kind, opts, key))
    return table


# ── the whole cluster ────────────────────────────────────────────────────


async def _guests(client: ProxmoxClient) -> list[dict[str, Any]]:
    res = await client.get_cluster_resources(resource_type="vm") or []
    return sorted(
        (r for r in res if r.get("type") in ("qemu", "lxc") and not r.get("template")),
        key=lambda r: r.get("vmid", 0),
    )


async def _vnets(client: ProxmoxClient) -> list[str]:
    try:
        return sorted(v.get("vnet", "") for v in await client.get_sdn_vnets() or [])
    except PVECliError as e:
        if is_not_implemented(e):
            return []
        raise


async def cluster_overview(client: ProxmoxClient) -> dict[str, Any]:
    """Every level of the cluster: its switch, its rules, its pitfalls."""
    nodes = sorted(n["node"] for n in await client.get_nodes() or [] if n.get("status") == "online")
    guests = await _guests(client)
    vnets = await _vnets(client)
    dc = await read_status(client, cluster_scope())

    async def safe(scope: Scope) -> Status | None:
        try:
            return await read_status(client, scope)
        except PVECliError as e:
            # A vnet firewall needs PVE 8.3; a guest can vanish meanwhile
            if is_not_implemented(e) or getattr(e, "status_code", None) in (404, 500):
                return None
            raise

    scopes = (
        [node_scope(n) for n in nodes]
        + [guest_scope("vm" if g["type"] == "qemu" else "ct", g["node"], g["vmid"]) for g in guests]
        + [vnet_scope(v) for v in vnets]
    )
    statuses = [s for s in await asyncio.gather(*(safe(sc) for sc in scopes)) if s]
    names = {g["vmid"]: g.get("name", "") for g in guests}
    return {"datacenter": dc, "levels": statuses, "names": names}


def print_overview(view: dict[str, Any]) -> None:
    dc: Status = view["datacenter"]
    print_status(dc)
    if not dc.level_on:
        print_warning("the datacenter firewall is off: no rule below is enforced")
    table = Table(title="Firewall of each level", show_header=True, header_style="bold cyan")
    table.add_column("Level", style="cyan")
    table.add_column("Target")
    table.add_column("Firewall")
    table.add_column("Cards")
    table.add_column("Rules", justify="right")
    table.add_column("Enforced")
    table.add_column("Issues")
    for st in view["levels"]:
        s = st.scope
        if s.is_guest:
            name = view["names"].get(s.vmid, "")
            target = f"{s.vmid} {name}".strip()
        else:
            target = s.name or ""
        cards = "-"
        if st.nics is not None:
            flagged = [n for n, on in st.nics.items() if on]
            cards = f"{len(flagged)}/{len(st.nics)}"
        # Said once above the table, not on every line
        issues = [i for i in st.issues if i != DATACENTER_OFF]
        table.add_row(
            {"vm": "VM", "ct": "CT"}.get(s.kind, s.kind),
            target,
            _onoff(st.level_on, "enable" not in st.options),
            cards,
            str(len(st.rules)),
            "[green]yes[/green]" if st.enforced else "[dim]no[/dim]",
            "\n".join(issues),
        )
    console.print(table)


# ── export ───────────────────────────────────────────────────────────────


def _clean_rule(rule: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in rule.items() if k not in _RULE_META}


def _clean(items: list[dict[str, Any]], keep: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    return [{k: v for k, v in i.items() if k not in _RULE_META or k in keep} for i in items]


async def _objects(client: ProxmoxClient, scope: Scope, keep: tuple[str, ...] = ()) -> dict[str, Any]:
    """The aliases and IP sets of a level, the IP sets with their entries.

    `keep` keeps some of the fields that are not configuration, such as
    the errors of a broken entry to show it.
    """
    aliases, ipsets = await asyncio.gather(
        client.get(f"{scope.base}/aliases"), client.get(f"{scope.base}/ipset"),
    )
    sets = []
    for s in sorted(ipsets or [], key=lambda x: x.get("name", "")):
        entries = await client.get(f"{scope.base}/ipset/{s['name']}") or []
        sets.append({**{k: v for k, v in s.items() if k not in _RULE_META},
                     "entries": sorted(_clean(entries, keep), key=lambda e: e.get("cidr", ""))})
    out: dict[str, Any] = {}
    if aliases:
        out["aliases"] = sorted(_clean(aliases, keep), key=lambda a: a.get("name", ""))
    if sets:
        out["ipsets"] = sets
    return out


async def export_cluster(client: ProxmoxClient) -> dict[str, Any]:
    """The whole firewall of a cluster, in a form import can read back."""
    dc = cluster_scope()
    dc_opts, dc_rules, groups, dc_objects = await asyncio.gather(
        read_options(client, dc), read_rules(client, dc),
        client.get("/cluster/firewall/groups"), _objects(client, dc),
    )
    datacenter: dict[str, Any] = {"options": dc_opts, **dc_objects}
    if groups:
        datacenter["groups"] = []
        for g in sorted(groups, key=lambda x: x.get("group", "")):
            rules = await read_rules(client, group_scope(g["group"]))
            entry = {"name": g["group"], "rules": [_clean_rule(r) for r in rules]}
            if g.get("comment"):
                entry["comment"] = g["comment"]
            datacenter["groups"].append(entry)
    if dc_rules:
        datacenter["rules"] = [_clean_rule(r) for r in dc_rules]

    async def level(scope: Scope, objects: bool) -> dict[str, Any]:
        opts, rules = await asyncio.gather(read_options(client, scope), read_rules(client, scope))
        out: dict[str, Any] = {}
        if opts:
            out["options"] = opts
        if rules:
            out["rules"] = [_clean_rule(r) for r in rules]
        if objects:
            out.update(await _objects(client, scope))
        return out

    nodes = sorted(n["node"] for n in await client.get_nodes() or [] if n.get("status") == "online")
    doc: dict[str, Any] = {"pvecli_firewall": 1, "datacenter": datacenter}
    node_levels = {n: await level(node_scope(n), False) for n in nodes}
    doc["nodes"] = {n: v for n, v in node_levels.items() if v}
    guests: dict[int, Any] = {}
    for g in await _guests(client):
        kind = "vm" if g["type"] == "qemu" else "ct"
        content = await level(guest_scope(kind, g["node"], g["vmid"]), True)
        if content:
            guests[g["vmid"]] = {"type": kind, **({"name": g["name"]} if g.get("name") else {}), **content}
    doc["guests"] = guests
    vnets: dict[str, Any] = {}
    for v in await _vnets(client):
        try:
            content = await level(vnet_scope(v), False)
        except PVECliError as e:
            if is_not_implemented(e):
                break
            raise
        if content:
            vnets[v] = content
    doc["vnets"] = vnets
    return doc


# ── resolving the target of a command ────────────────────────────────────


async def resolve(client: ProxmoxClient, kind: str, target: str | None) -> Scope | None:
    """The scope a command acts on; a menu when the target is omitted.

    Returns None when the menu is cancelled.
    """
    if kind == "cluster":
        return cluster_scope()
    if kind == "node":
        from ._shared import pick_node

        node = target or await pick_node(client)
        if not node:
            return None
        known = {n.get("node") for n in await client.get_nodes() or []}
        if node not in known:
            print_error(f"Node '{node}' not found")
            raise typer.Exit(1)
        return node_scope(node)
    if kind == "vnet":
        vnets = await _vnets(client)
        if not target:
            if not vnets:
                print_info("No SDN vnet defined")
                raise typer.Exit(0)
            idx = select_menu(vnets, "  Select a vnet:")
            if idx is None:
                return None
            target = vnets[idx]
        if target not in vnets:
            print_error(f"Vnet '{target}' not found")
            raise typer.Exit(1)
        return vnet_scope(target)
    # A guest
    select: Any
    locate: Any
    if kind == "vm":
        from .vm import _get_vm_node, _select_vm

        select, locate = _select_vm, _get_vm_node
    else:
        from .ct import _get_container_node, _select_ct

        select, locate = _select_ct, _get_container_node
    if target is None:
        vmid = await select(client)
        if vmid is None:
            return None
    else:
        try:
            vmid = int(target)
        except ValueError:
            print_error(f"Not a {'VM' if kind == 'vm' else 'container'} ID: {target}")
            raise typer.Exit(1)
    node, _ = await locate(client, vmid)
    return guest_scope(kind, node, vmid)


# ── the commands ─────────────────────────────────────────────────────────

_TARGET_HELP = {
    "vm": "VM ID",
    "ct": "Container ID",
    "node": "Node name",
    "vnet": "Vnet name",
}
_NOUN = {"cluster": "the datacenter", "vm": "a VM", "ct": "a container", "node": "a node", "vnet": "a vnet"}


_METAVAR = {"vm": "vmid", "ct": "ctid", "node": "node", "vnet": "vnet"}


def _target_arg(kind: str) -> Any:
    return typer.Argument(None, help=_TARGET_HELP[kind], show_default=False, metavar=_METAVAR[kind])


async def _run(profile: str | None, kind: str, target: str | None, body: Any) -> None:
    """Open the client, resolve the target, run body(client, scope)."""
    async with ProxmoxClient(ConfigManager().get_profile(profile)) as client:
        scope = await resolve(client, kind, target)
        if scope is None:
            print_cancelled()
            return
        await body(client, scope)


def _drop(*names: str) -> Any:
    """Take parameters out of the signature Typer reads.

    The same command serves every kind of resource; the datacenter has no
    target, and only the datacenter has security groups.
    """

    def deco(fn: Any) -> Any:
        if names:
            import inspect

            sig = inspect.signature(fn)
            fn.__signature__ = sig.replace(
                parameters=[p for n, p in sig.parameters.items() if n not in names])
        return fn

    return deco


def build_app(kind: str) -> typer.Typer:
    """The `firewall` group of one kind of resource."""
    noun = _NOUN[kind]
    order = ["status", "enable", "disable", "set", "options", "rule", "group", "ipset", "alias", "log",
             "macros", "refs", "export"]
    app = typer.Typer(
        help=f"Manage the firewall of {noun}",
        no_args_is_help=True,
        cls=ordered_group(order),
    )
    rule_app = typer.Typer(help=f"Firewall rules of {noun}", no_args_is_help=True)
    app.add_typer(rule_app, name="rule")
    has_target = kind != "cluster"
    scope_has_objects = kind in ("cluster", "vm", "ct")
    scope_has_log = kind in ("node", "vm", "ct")

    with_target = _drop() if has_target else _drop("target")

    def fail(e: PVECliError) -> None:
        print_error(str(e))
        raise typer.Exit(1)

    # ── status ──
    @app.command("status")
    @with_target
    @fleet(parallel=True)
    @async_to_sync
    async def status(
        target: str = _target_arg(kind) if has_target else None,  # type: ignore[assignment]
        profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
        json_output: bool = JSON_OPTION,
    ) -> None:
        """Show whether the firewall is enforced, and why not."""

        async def body(client: ProxmoxClient, scope: Scope) -> None:
            if kind == "cluster":
                view = await cluster_overview(client)
                if json_output:
                    emit_json({"datacenter": view["datacenter"].as_json(),
                               "levels": [s.as_json() for s in view["levels"]]})
                    return
                print_overview(view)
                return
            st = await read_status(client, scope)
            if json_output:
                emit_json(st.as_json())
                return
            print_status(st)

        try:
            await _run(profile, kind, target, body)
        except PVECliError as e:
            fail(e)

    status.__doc__ = (
        "Show the firewall of every level of the cluster, and what is not enforced."
        if kind == "cluster" else "Show whether the firewall is enforced here, and why not."
    )

    # ── rule list ──
    @rule_app.command("list")
    @_drop("target") if kind == "cluster" else _drop("group")
    @fleet(parallel=True)
    @async_to_sync
    async def rule_list(
        target: str = _target_arg(kind) if has_target else None,  # type: ignore[assignment]
        group: str = typer.Option(None, "--group", "-g", help="Rules of this security group")
        if kind == "cluster" else None,  # type: ignore[assignment]
        profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
        json_output: bool = JSON_OPTION,
    ) -> None:
        """List the firewall rules, in the order they are evaluated."""

        async def body(client: ProxmoxClient, scope: Scope) -> None:
            if group:
                scope = group_scope(group)
            rules = await read_rules(client, scope)
            if json_output:
                emit_json(rules)
                return
            if not rules:
                print_info(f"No firewall rule on {scope.label}")
                return
            console.print(rules_table(rules, f"Firewall rules of {scope.label}"))

        try:
            await _run(profile, kind, target, body)
        except PVECliError as e:
            fail(e)

    # ── options ──
    @app.command("options")
    @with_target
    @fleet(parallel=True)
    @async_to_sync
    async def options(
        target: str = _target_arg(kind) if has_target else None,  # type: ignore[assignment]
        profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
        json_output: bool = JSON_OPTION,
    ) -> None:
        """Show the firewall options, with the defaults Proxmox applies."""

        async def body(client: ProxmoxClient, scope: Scope) -> None:
            opts = await read_options(client, scope)
            if json_output:
                emit_json({k: effective(scope.kind, opts, k)
                           for k in sorted(set(opts) | set(DEFAULTS.get(scope.kind, {})))})
                return
            console.print(options_table(scope, opts))

        try:
            await _run(profile, kind, target, body)
        except PVECliError as e:
            fail(e)

    # ── IP sets and aliases ──
    if scope_has_objects:
        ipset_app = typer.Typer(help=f"IP sets of {noun}", no_args_is_help=True)
        alias_app = typer.Typer(help=f"Aliases of {noun}", no_args_is_help=True)
        app.add_typer(ipset_app, name="ipset")
        app.add_typer(alias_app, name="alias")

        @ipset_app.command("list")
        @with_target
        @fleet(parallel=True)
        @async_to_sync
        async def ipset_list(
            target: str = _target_arg(kind) if has_target else None,  # type: ignore[assignment]
            profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
            json_output: bool = JSON_OPTION,
        ) -> None:
            """List the IP sets and their entries."""

            async def body(client: ProxmoxClient, scope: Scope) -> None:
                sets = (await _objects(client, scope, keep=("errors",))).get("ipsets", [])
                if json_output:
                    emit_json(sets)
                    return
                if not sets:
                    print_info(f"No IP set on {scope.label}")
                    return
                table = Table(title=f"IP sets of {scope.label}", show_header=True, header_style="bold cyan")
                table.add_column("Name", style="cyan")
                table.add_column("Entries")
                table.add_column("Comment")
                for s in sets:
                    entries = "\n".join(
                        ("!" if e.get("nomatch") else "") + e.get("cidr", "")
                        + (f"  [dim]{e['comment']}[/dim]" if e.get("comment") else "")
                        + (f"  [red]broken: {'; '.join(e['errors'].values())}[/red]" if e.get("errors") else "")
                        for e in s["entries"]
                    ) or "[dim]empty[/dim]"
                    table.add_row(s["name"], entries, s.get("comment", "") or "")
                console.print(table)

            try:
                await _run(profile, kind, target, body)
            except PVECliError as e:
                fail(e)

        @alias_app.command("list")
        @with_target
        @fleet(parallel=True)
        @async_to_sync
        async def alias_list(
            target: str = _target_arg(kind) if has_target else None,  # type: ignore[assignment]
            profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
            json_output: bool = JSON_OPTION,
        ) -> None:
            """List the aliases: names for an address or a network."""

            async def body(client: ProxmoxClient, scope: Scope) -> None:
                aliases = (await _objects(client, scope)).get("aliases", [])
                if json_output:
                    emit_json(aliases)
                    return
                if not aliases:
                    print_info(f"No alias on {scope.label}")
                    return
                table = Table(title=f"Aliases of {scope.label}", show_header=True, header_style="bold cyan")
                table.add_column("Name", style="cyan")
                table.add_column("Address")
                table.add_column("Comment")
                for a in aliases:
                    table.add_row(a.get("name", ""), a.get("cidr", ""), a.get("comment", "") or "")
                console.print(table)

            try:
                await _run(profile, kind, target, body)
            except PVECliError as e:
                fail(e)

    # ── log ──
    if scope_has_log:

        @app.command("log")
        @async_to_sync
        async def log(
            target: str = _target_arg(kind),
            since: int = typer.Option(None, "--since", help="Only lines after this UNIX time"),
            limit: int = typer.Option(50, "--limit", "-l", help="Number of lines"),
            profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
            json_output: bool = JSON_OPTION,
        ) -> None:
            """Show the last lines of the firewall log."""

            async def body(client: ProxmoxClient, scope: Scope) -> None:
                params: dict[str, Any] = {"limit": limit}
                if since is not None:
                    params["since"] = since
                lines = await client.get(f"{scope.base}/log", params=params) or []
                if json_output:
                    emit_json(lines)
                    return
                text = [ln.get("t", "") for ln in sorted(lines, key=lambda x: x.get("n", 0))]
                text = [t for t in text if t and t != "no content"]
                if not text:
                    print_info(f"Nothing in the firewall log of {scope.label}")
                    return
                for t in text:
                    console.print(t, markup=False, highlight=False)

            try:
                await _run(profile, kind, target, body)
            except PVECliError as e:
                fail(e)

    # The commands that change something
    from .firewall_edit import add_object_commands, add_rule_commands, add_switch_commands

    add_rule_commands(rule_app, kind, _target_arg(kind) if has_target else None, not has_target)
    add_switch_commands(app, kind, _target_arg(kind) if has_target else None, with_target)
    if scope_has_objects:
        add_object_commands(alias_app, ipset_app, kind, _target_arg(kind) if has_target else None, with_target)
    if kind == "cluster":
        _add_cluster_only(app, fail)
    return app


def _add_cluster_only(app: typer.Typer, fail: Any) -> None:
    """Security groups, macros, references and export: datacenter only."""
    group_app = typer.Typer(help="Security groups: named lists of rules", no_args_is_help=True)
    app.add_typer(group_app, name="group")
    from .firewall_edit import add_group_commands

    add_group_commands(group_app)

    @group_app.command("list")
    @fleet(parallel=True)
    @async_to_sync
    async def group_list(
        profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
        json_output: bool = JSON_OPTION,
    ) -> None:
        """List the security groups and their number of rules."""
        try:
            async with ProxmoxClient(ConfigManager().get_profile(profile)) as client:
                groups = sorted(await client.get("/cluster/firewall/groups") or [],
                                key=lambda g: g.get("group", ""))
                counts = await asyncio.gather(*(read_rules(client, group_scope(g["group"])) for g in groups))
                if json_output:
                    emit_json([{"group": g["group"], "comment": g.get("comment", ""), "rules": len(r)}
                               for g, r in zip(groups, counts, strict=True)])
                    return
                if not groups:
                    print_info("No security group")
                    return
                table = Table(title="Security groups", show_header=True, header_style="bold cyan")
                table.add_column("Group", style="cyan")
                table.add_column("Rules", justify="right")
                table.add_column("Comment")
                for g, r in zip(groups, counts, strict=True):
                    table.add_row(g["group"], str(len(r)), g.get("comment", "") or "")
                console.print(table)
        except PVECliError as e:
            fail(e)

    @group_app.command("info")
    @async_to_sync
    async def group_info(
        group: str = typer.Argument(None, help="Security group"),
        profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
        json_output: bool = JSON_OPTION,
    ) -> None:
        """Show the rules of a security group and where it is used."""
        try:
            async with ProxmoxClient(ConfigManager().get_profile(profile)) as client:
                groups = sorted(g.get("group", "") for g in await client.get("/cluster/firewall/groups") or [])
                if not group:
                    if not groups:
                        print_info("No security group")
                        return
                    idx = select_menu(groups, "  Select a security group:")
                    if idx is None:
                        print_cancelled()
                        return
                    group = groups[idx]
                if group not in groups:
                    print_error(f"Security group '{group}' not found")
                    raise typer.Exit(1)
                rules = await read_rules(client, group_scope(group))
                used = await group_users(client, group)
                if json_output:
                    emit_json({"group": group, "rules": rules, "used_by": used})
                    return
                if rules:
                    console.print(rules_table(rules, f"Security group {group}"))
                else:
                    print_info(f"Security group {group} has no rule")
                console.print(f"  Used by: {', '.join(used) if used else '[dim]nothing[/dim]'}")
        except PVECliError as e:
            fail(e)

    @app.command("macros")
    @async_to_sync
    async def macros(
        profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
        json_output: bool = JSON_OPTION,
    ) -> None:
        """List the macros a rule can use (SSH, HTTPS, Ceph...)."""
        try:
            async with ProxmoxClient(ConfigManager().get_profile(profile)) as client:
                items = sorted(await client.get("/cluster/firewall/macros") or [], key=lambda m: m.get("macro", ""))
                if json_output:
                    emit_json(items)
                    return
                table = Table(title="Firewall macros", show_header=True, header_style="bold cyan")
                table.add_column("Macro", style="cyan")
                table.add_column("Description")
                for m in items:
                    table.add_row(m.get("macro", ""), m.get("descr", ""))
                console.print(table)
        except PVECliError as e:
            fail(e)

    @app.command("refs")
    @async_to_sync
    async def refs(
        profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
        json_output: bool = JSON_OPTION,
    ) -> None:
        """List the aliases and IP sets a rule can name as source or destination."""
        try:
            async with ProxmoxClient(ConfigManager().get_profile(profile)) as client:
                items = await client.get("/cluster/firewall/refs") or []
                if json_output:
                    emit_json(items)
                    return
                if not items:
                    print_info("No alias or IP set defined")
                    return
                table = Table(title="References for --source and --dest", show_header=True, header_style="bold cyan")
                table.add_column("Write")
                table.add_column("Type")
                table.add_column("Scope")
                table.add_column("Comment")
                for r in sorted(items, key=lambda x: (x.get("type", ""), x.get("name", ""))):
                    table.add_row(r.get("ref", ""), r.get("type", ""), r.get("scope", ""), r.get("comment", "") or "")
                console.print(table)
        except PVECliError as e:
            fail(e)

    @app.command("export")
    @async_to_sync
    async def export(
        output: str = typer.Option(None, "--output", "-ou", help="Write to this file instead of the terminal"),
        profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
        json_output: bool = JSON_OPTION,
    ) -> None:
        """Export the whole firewall of the cluster (YAML, or JSON with --json)."""
        try:
            async with ProxmoxClient(ConfigManager().get_profile(profile)) as client:
                doc = await export_cluster(client)
        except PVECliError as e:
            fail(e)
            return
        if json_output and not output:
            emit_json(doc)
            return
        import json

        text = (json.dumps(doc, indent=2, sort_keys=True) + "\n") if json_output else yaml.safe_dump(
            doc, sort_keys=False, default_flow_style=False, allow_unicode=True)
        if output:
            Path(output).write_text(text)
            print_success(f"Firewall exported to {output}")
        else:
            import sys

            sys.stdout.write(text)


async def group_users(client: ProxmoxClient, group: str) -> list[str]:
    """The levels whose rules insert this security group."""
    used = []
    nodes = sorted(n["node"] for n in await client.get_nodes() or [] if n.get("status") == "online")
    scopes = [cluster_scope()] + [node_scope(n) for n in nodes] + [
        guest_scope("vm" if g["type"] == "qemu" else "ct", g["node"], g["vmid"]) for g in await _guests(client)
    ]
    all_rules = await asyncio.gather(*(read_rules(client, s) for s in scopes), return_exceptions=True)
    for scope, rules in zip(scopes, all_rules, strict=True):
        if isinstance(rules, BaseException):
            continue
        if any(r.get("type") == "group" and r.get("action") == group for r in rules):
            used.append(scope.label)
    return used


def summary(st: Status) -> str:
    """The firewall of a guest in one line, for `vm info` and `ct info`."""
    n = sum(1 for r in st.rules if is_on(r.get("enable", 1)))
    rules = f"{n} rule{'' if n == 1 else 's'}"
    if not st.level_on:
        return "off" + (f" [yellow]({rules} not enforced)[/yellow]" if n else "")
    if not st.datacenter_on:
        return f"on, {rules} [yellow](not enforced: the datacenter firewall is off)[/yellow]"
    flagged = [nic for nic, on in (st.nics or {}).items() if on]
    if not flagged:
        return f"on, {rules} [yellow](not enforced: no card has firewall=1)[/yellow]"
    text = f"[green]enforced[/green] on {', '.join(flagged)}, {rules}"
    unflagged = [nic for nic, on in (st.nics or {}).items() if not on]
    if unflagged:
        text += f" [yellow](not on {', '.join(unflagged)})[/yellow]"
    return text


async def info_line(client: ProxmoxClient, kind: str, node: str, vmid: int) -> str | None:
    """The Firewall line of `vm info` / `ct info`, None when unreadable."""
    try:
        return summary(await read_status(client, guest_scope(kind, node, vmid)))
    except PVECliError:
        # A token without Sys.Audit on / cannot read the datacenter options
        return None
